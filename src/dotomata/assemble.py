"""Step 5: build frames from motion templates (integer offsets and swaps, no rotation).

Offsets chain through the rig: a part moves with its parent part and an
equipment layer moves with the part that carries its joint, so a template that
lowers the torso also lowers the head, arms, helmet and shield.
"""

from __future__ import annotations

import copy
from dataclasses import dataclass

import numpy as np

from .common import TRANSPARENT, Context, SpriteError, Unit, load_unit, opaque
from .parts import Art

FRAME_KEYS = {"offsets", "swap", "replace"}


@dataclass
class Action:
    name: str
    template: str
    frames: list[dict]
    frame_ms: int
    strike_frame: int      # -1 when the action has no strike
    airborne: set[int]


def motion_source(ctx: Context, unit: Unit) -> Unit:
    """A palette swap moves exactly like its base unit."""
    swap = unit.data.get("palette_swap")
    return load_unit(ctx, swap["base"]) if swap else unit


def resolve_actions(ctx: Context, unit: Unit, rig_name: str) -> list[Action]:
    src = motion_source(ctx, unit)
    what = f"unit '{unit.id}'"
    chosen = src.data.get("actions", {})
    overrides = src.data.get("overrides", {})
    airborne = src.data.get("airborne", {})
    actions = []
    for name in ctx.project["action_order"]:
        if name in chosen:
            actions.append(resolve_action(ctx, name, chosen[name], rig_name, overrides.get(name, {}),
                                          airborne.get(name, []), what))
    for name in ctx.project["required_actions"].get(rig_name, []):
        if name not in chosen:
            raise SpriteError(f"{what}: rig '{rig_name}' requires action '{name}'")
    return actions


def resolve_action(ctx: Context, name: str, tname: str, rig_name: str, ov: dict, airborne: list,
                   what: str) -> Action:
    """One action from its template plus the unit's override, with every frame-count and strike rule checked."""
    template = ctx.project["templates"].get(tname)
    if template is None:
        raise SpriteError(f"{what}: action '{name}' uses unknown template '{tname}'")
    if template.get("action") != name:
        raise SpriteError(f"{what}: template '{tname}' is for action '{template.get('action')}', not '{name}'")
    if template["rig"] != rig_name:
        raise SpriteError(f"{what}: template '{tname}' is for rig '{template['rig']}', unit uses '{rig_name}'")
    counts = ctx.project["frames"]
    frames = copy.deepcopy(template["frames"])
    frame_ms = ov.get("frame_ms", template.get("frame_ms", ctx.project["frame_ms"]))
    strike = ov.get("strike_frame", template.get("strike_frame", -1))
    for index, patch in ov.get("frames", {}).items():
        i = int(index)
        if not 0 <= i < len(frames):
            raise SpriteError(f"{what}: override for {name} frame {i} is out of range")
        _merge_frame(frames[i], patch, f"{what} override {name} frame {i}")
    if len(frames) != counts[name]:
        raise SpriteError(f"{what}: action '{name}' has {len(frames)} frames, expected {counts[name]}")
    for i, frame in enumerate(frames):
        extra = set(frame) - FRAME_KEYS
        if extra:
            raise SpriteError(f"{what}: {name} frame {i} has unknown keys {sorted(extra)}")
    if name in ctx.project["strike_actions"]:
        if not 0 <= strike <= ctx.project["max_windup_frames"]:
            raise SpriteError(f"{what}: action '{name}' strike frame {strike} must be 0..{ctx.project['max_windup_frames']}"
                              " (the target reacts on the strike frame, so wind-up stays short)")
    elif strike != -1:
        raise SpriteError(f"{what}: action '{name}' must not have a strike frame")
    air = set(airborne)
    if any(not 0 <= i < len(frames) for i in air):
        raise SpriteError(f"{what}: airborne frames {sorted(air)} for '{name}' are out of range")
    return Action(name, tname, frames, int(frame_ms), int(strike), air)


def _merge_frame(frame: dict, patch: dict, what: str) -> None:
    for key, value in patch.items():
        if key == "offsets" or key == "swap":
            frame.setdefault(key, {}).update(value)
        elif key == "replace":
            if value is None:
                frame.pop("replace", None)
            else:
                frame["replace"] = value
        else:
            raise SpriteError(f"{what}: unknown key '{key}'")


def total_offsets(rig: dict, offsets: dict, what: str) -> dict[str, tuple[int, int]]:
    known = {"root"} | set(rig["parts"]) | set(rig["equipment"])
    for key, value in offsets.items():
        if key not in known:
            raise SpriteError(f"{what}: offset for unknown layer '{key}'")
        if not (isinstance(value, list) and len(value) == 2 and all(isinstance(v, int) for v in value)):
            raise SpriteError(f"{what}: offset for '{key}' must be two integers (got {value})")
    totals: dict[str, tuple[int, int]] = {"root": tuple(offsets.get("root", (0, 0)))}

    def resolve(layer: str) -> tuple[int, int]:
        if layer in totals:
            return totals[layer]
        if layer in rig["parts"]:
            parent = rig["parts"][layer]
        else:
            parent = rig["joints"][rig["equipment"][layer]]
        px, py = resolve(parent)
        ox, oy = offsets.get(layer, (0, 0))
        totals[layer] = (px + ox, py + oy)
        return totals[layer]

    for layer in list(rig["parts"]) + list(rig["equipment"]):
        resolve(layer)
    return totals


def compose_frame(art: Art, rig: dict, frame: dict, cell: tuple[int, int], anchor: tuple[int, int],
                  exclude=(), what: str = "") -> tuple[np.ndarray, int | None]:
    """Returns (cell-sized symbol grid, bottom row of the foot part or None for replace frames)."""
    cw, ch = cell
    canvas = np.full((ch, cw), TRANSPARENT, dtype="<U1")
    totals = total_offsets(rig, frame.get("offsets", {}), what)
    if "replace" in frame:
        name = frame["replace"]
        if name not in art.variants:
            raise SpriteError(f"{what}: unit has no variant '{name}' in parts.json")
        drawing, (fx, fy) = art.variants[name]
        rx, ry = totals["root"]
        _paint(canvas, drawing, anchor[0] - fx + rx, anchor[1] - fy + ry, what)
        return canvas, None

    swaps = frame.get("swap", {})
    for layer in swaps:
        if f"{layer}@{swaps[layer]}" not in art.swaps:
            raise SpriteError(f"{what}: unit has no swap '{layer}@{swaps[layer]}' in parts.json")
    fx, fy = art.joints["foot"]
    foot_bottom = None
    for layer in rig["draw_order"]:
        if layer in exclude:
            continue
        drawing = art.swaps[f"{layer}@{swaps[layer]}"] if layer in swaps else art.layers.get(layer)
        if drawing is None:
            continue
        dx, dy = totals[layer]
        ox, oy = anchor[0] - fx + dx, anchor[1] - fy + dy
        _paint(canvas, drawing, ox, oy, f"{what} layer '{layer}'")
        if layer == rig["foot_part"]:
            rows = np.nonzero(opaque(drawing).any(axis=1))[0]
            foot_bottom = int(rows.max()) + oy
    return canvas, foot_bottom


def _paint(canvas: np.ndarray, drawing: np.ndarray, ox: int, oy: int, what: str) -> None:
    ys, xs = np.nonzero(opaque(drawing))
    if not len(ys):
        return
    cy, cx = ys + oy, xs + ox
    h, w = canvas.shape
    if cy.min() < 0 or cx.min() < 0 or cy.max() >= h or cx.max() >= w:
        raise SpriteError(f"{what}: drawing leaves the {w}x{h} cell (spans x {cx.min()}..{cx.max()}, y {cy.min()}..{cy.max()})")
    canvas[cy, cx] = drawing[ys, xs]


@dataclass
class Assembled:
    action: Action
    frames: list[np.ndarray]
    foot_bottoms: list[int | None]


def stand_anchor(kind: dict, rig: dict, what: str) -> tuple[int, int]:
    """Where the foot joint goes in every frame but the down pose: the kind's foot
    point, or for a rig with an air_height (the flyer) the air point that many rows
    above the cell bottom, so it hovers. Its down pose still lands on the foot point."""
    if "air_height" not in rig:
        return tuple(kind["foot"])
    air = rig["air_height"]
    foot_x, foot_y = kind["foot"]
    if not isinstance(air, int) or not 0 < kind["cell"][1] - air < foot_y:
        raise SpriteError(f"{what}: air_height {air} must put the air point inside the cell, above the foot point "
                          f"(cell height {kind['cell'][1]}, foot y {foot_y})")
    return foot_x, kind["cell"][1] - air


def action_anchor(ctx: Context, action_name: str, kind: dict, rig: dict, what: str) -> tuple[int, int]:
    return tuple(kind["foot"]) if action_name in ctx.project["feet_exempt_actions"] else stand_anchor(kind, rig, what)


def assemble_unit(ctx: Context, unit: Unit, art: Art) -> list[Assembled]:
    rig = ctx.project["rigs"][art.rig_name]
    kind = ctx.project["kinds"][unit.kind]
    cell = tuple(kind["cell"])
    result = []
    for action in resolve_actions(ctx, unit, art.rig_name):
        anchor = action_anchor(ctx, action.name, kind, rig, f"unit '{unit.id}'")
        frames, bottoms = [], []
        for i, frame in enumerate(action.frames):
            canvas, bottom = compose_frame(art, rig, frame, cell, anchor, what=f"unit '{unit.id}' {action.name} frame {i}")
            frames.append(canvas)
            bottoms.append(bottom)
        result.append(Assembled(action, frames, bottoms))
    return result


def measure_height(ctx: Context, unit: Unit, art: Art, assembled: list[Assembled]) -> int:
    """Opaque height of idle frame 0 without the rig's height_excludes layers
    (weapon, staff, bow). A flyer (a rig with air_height) is measured without that
    frame's wing swaps, on the mid-wing base pose the size pass fits to the kind height."""
    rig = ctx.project["rigs"][art.rig_name]
    kind = ctx.project["kinds"][unit.kind]
    idle = next((a for a in assembled if a.action.name == "idle"), None)
    if idle is None:
        raise SpriteError(f"unit '{unit.id}' has no idle action to measure")
    frame = idle.action.frames[0]
    if "air_height" in rig:
        frame = {k: v for k, v in frame.items() if k != "swap"}
    canvas, _ = compose_frame(art, rig, frame, tuple(kind["cell"]),
                              stand_anchor(kind, rig, f"unit '{unit.id}'"),
                              exclude=set(rig["height_excludes"]), what=f"unit '{unit.id}' idle frame 0")
    rows = np.nonzero(opaque(canvas).any(axis=1))[0]
    return int(rows.max() - rows.min() + 1)
