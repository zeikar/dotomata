"""Step 4: split the fixed grid into rig parts and equipment layers.

parts.json in the unit folder:
  mask      text grid, same size as the fixed grid; each opaque cell names its
            layer with the rig's mask symbol ('.' = empty, '?' = not yet assigned)
  joints    {joint: [x, y]} in grid cells; every joint of the rig is required
  added     {layer: grid file} newly drawn cells hidden in the AI pose (e.g. the
            torso behind a shield), same size as the fixed grid
  swaps     {"layer@name": grid file} alternative drawings of one layer that a
            motion template can switch to, same size as the fixed grid
  variants  {name: {"file": grid file, "foot": [x, y]}} whole-figure drawings
            used by "replace" frames (the down pose); any size, own foot point
"""

from __future__ import annotations

from dataclasses import dataclass, field

import numpy as np

from .common import (TRANSPARENT, Context, SpriteError, Unit, check_grid_symbols, grid_bytes, json_bytes, load_unit,
                     opaque, parse_hex, read_grid, read_json, read_palette, write_new)
from .fixes import apply_fixes, load_fixes


@dataclass
class Art:
    unit_id: str
    rig_name: str
    palette: dict
    fixed: np.ndarray
    layers: dict[str, np.ndarray]
    joints: dict[str, tuple[int, int]]
    swaps: dict[str, np.ndarray] = field(default_factory=dict)
    variants: dict[str, tuple[np.ndarray, tuple[int, int]]] = field(default_factory=dict)
    fix_counts: dict[str, int] = field(default_factory=dict)
    new_cells: dict[str, dict[str, int]] = field(default_factory=dict)


def rig_for(ctx: Context, unit: Unit) -> tuple[str, dict]:
    name = unit.data.get("rig") or ctx.project["kinds"][unit.kind].get("rig")
    if not name or name not in ctx.project["rigs"]:
        raise SpriteError(f"unit '{unit.id}' has no valid rig (got '{name}')")
    return name, ctx.project["rigs"][name]


def rig_layers(rig: dict) -> list[str]:
    return list(rig["parts"]) + list(rig["equipment"])


def load_fixed(ctx: Context, unit: Unit):
    """Returns (fixed grid, palette, fix counts by category)."""
    what = f"unit '{unit.id}'"
    palette = read_palette(unit.source / "palette.json", what)
    grid = read_grid(unit.source / "grid.txt", f"{what} grid.txt")
    check_grid_symbols(grid, palette, f"{what} grid.txt")
    fixed, counts = apply_fixes(grid, palette, load_fixes(unit.source / "fixes.json"),
                                ctx.project["fix_categories"], what)
    return fixed, palette, counts


def init_parts(ctx: Context, unit: Unit, force: bool) -> list[str]:
    """Writes a parts_mask.txt with every opaque cell unassigned, and a parts.json skeleton."""
    fixed, _, _ = load_fixed(ctx, unit)
    _, rig = rig_for(ctx, unit)
    mask = np.where(opaque(fixed), "?", TRANSPARENT)
    existing = [n for n in ("parts_mask.txt", "parts.json") if (unit.source / n).exists()]
    if existing and not force:
        raise SpriteError(f"unit '{unit.id}': refusing to overwrite {existing} (pass --force to replace them)")
    written = []
    write_new(unit.source / "parts_mask.txt", grid_bytes(mask), force)
    written.append("parts_mask.txt")
    ys, xs = np.nonzero(opaque(fixed))
    bottom = int(ys.max())
    foot_x = int(np.median(xs[ys == bottom]))
    joints = {j: None for j in rig["joints"]}
    joints["foot"] = [foot_x, bottom]
    write_new(unit.source / "parts.json",
              json_bytes({"mask": "parts_mask.txt", "joints": joints, "added": {}, "swaps": {}, "variants": {}}), force)
    written.append("parts.json")
    return written


def load_art(ctx: Context, unit: Unit) -> Art:
    swap = unit.data.get("palette_swap")
    if swap:
        return _load_palette_swap(ctx, unit, swap)
    what = f"unit '{unit.id}'"
    rig_name, rig = rig_for(ctx, unit)
    fixed, palette, fix_counts = load_fixed(ctx, unit)
    parts_path = unit.source / "parts.json"
    if not parts_path.is_file():
        raise SpriteError(f"{what}: no parts.json (run 'parts {unit.path} --init' and assign the mask)")
    spec = read_json(parts_path)

    mask = read_grid(unit.source / spec["mask"], f"{what} {spec['mask']}")
    if mask.shape != fixed.shape:
        raise SpriteError(f"{what}: part mask is {mask.shape[1]}x{mask.shape[0]}, grid is {fixed.shape[1]}x{fixed.shape[0]}")
    symbols = rig["mask_symbols"]
    unassigned = np.argwhere(mask == "?")
    if len(unassigned):
        y, x = unassigned[0]
        raise SpriteError(f"{what}: {len(unassigned)} mask cells are unassigned '?' (first at x={x}, y={y})")
    unknown = sorted(set(np.unique(mask).tolist()) - set(symbols) - {TRANSPARENT})
    if unknown:
        raise SpriteError(f"{what}: unknown mask symbols {unknown} for rig '{rig_name}'")
    mismatch = np.argwhere((mask != TRANSPARENT) != opaque(fixed))
    if len(mismatch):
        y, x = mismatch[0]
        raise SpriteError(f"{what}: mask and grid disagree on opaque cells (first at x={x}, y={y})")

    layers = {}
    for sym, layer in symbols.items():
        cells = mask == sym
        if cells.any():
            layers[layer] = np.where(cells, fixed, TRANSPARENT)

    equipment = set(unit.data.get("equipment", []))
    for name in rig["equipment"]:
        if name in layers and name not in equipment:
            raise SpriteError(f"{what}: mask has '{name}' cells but unit.json does not list it as equipment")
    for name in equipment:
        if name not in rig["equipment"]:
            raise SpriteError(f"{what}: equipment '{name}' is not a layer of rig '{rig_name}'")

    new_cells = {"occluded": {}, "swaps": {}, "variants": {}}
    for layer, file in spec.get("added", {}).items():
        if layer not in rig_layers(rig):
            raise SpriteError(f"{what}: added cells for unknown layer '{layer}'")
        added = _same_size_grid(unit, file, fixed.shape, palette)
        base = layers.get(layer, np.full(fixed.shape, TRANSPARENT, dtype="<U1"))
        overlap = np.argwhere(opaque(added) & opaque(base))
        if len(overlap):
            y, x = overlap[0]
            raise SpriteError(f"{what}: added '{layer}' cell ({x},{y}) is already part of that layer")
        layers[layer] = np.where(opaque(added), added, base)
        new_cells["occluded"][layer] = int(opaque(added).sum())

    for name in equipment:
        if name not in layers:
            raise SpriteError(f"{what}: equipment '{name}' has no cells in the mask")
    foot_part = rig["foot_part"]
    if foot_part not in layers:
        raise SpriteError(f"{what}: rig '{rig_name}' needs a '{foot_part}' layer")

    joints = {}
    for joint in rig["joints"]:
        value = spec.get("joints", {}).get(joint)
        if not (isinstance(value, list) and len(value) == 2 and all(isinstance(v, int) for v in value)):
            raise SpriteError(f"{what}: joint '{joint}' must be [x, y] in grid cells (got {value})")
        x, y = value
        if not (0 <= x < fixed.shape[1] and 0 <= y < fixed.shape[0]):
            raise SpriteError(f"{what}: joint '{joint}' ({x},{y}) is outside the grid")
        joints[joint] = (x, y)
    legs_bottom = int(np.nonzero(opaque(layers[foot_part]).any(axis=1))[0].max())
    if joints["foot"][1] != legs_bottom:
        raise SpriteError(f"{what}: foot joint y={joints['foot'][1]} is not the bottom row of '{foot_part}' (y={legs_bottom})")

    swaps = {}
    for key, file in spec.get("swaps", {}).items():
        layer, _, name = key.partition("@")
        if layer not in rig_layers(rig) or not name:
            raise SpriteError(f"{what}: bad swap key '{key}' (expected layer@name)")
        drawing = _same_size_grid(unit, file, fixed.shape, palette)
        base = layers.get(layer, np.full(fixed.shape, TRANSPARENT, dtype="<U1"))
        swaps[key] = drawing
        # Only cells the swap draws; cells it leaves empty were not drawn.
        new_cells["swaps"][key] = int((opaque(drawing) & (drawing != base)).sum())

    variants = {}
    for name, entry in spec.get("variants", {}).items():
        drawing = read_grid(unit.source / entry["file"], f"{what} {entry['file']}")
        check_grid_symbols(drawing, palette, f"{what} {entry['file']}")
        fx, fy = entry["foot"]
        if not (0 <= fx < drawing.shape[1] and 0 <= fy < drawing.shape[0]):
            raise SpriteError(f"{what}: variant '{name}' foot ({fx},{fy}) is outside its grid")
        variants[name] = (drawing, (fx, fy))
        new_cells["variants"][name] = int(opaque(drawing).sum())

    return Art(unit_id=unit.id, rig_name=rig_name, palette=palette, fixed=fixed, layers=layers, joints=joints,
               swaps=swaps, variants=variants, fix_counts=fix_counts, new_cells=new_cells)


def _same_size_grid(unit: Unit, file: str, shape, palette) -> np.ndarray:
    what = f"unit '{unit.id}' {file}"
    g = read_grid(unit.source / file, what)
    if g.shape != shape:
        raise SpriteError(f"{what}: is {g.shape[1]}x{g.shape[0]}, expected the grid size {shape[1]}x{shape[0]}")
    check_grid_symbols(g, palette, what)
    return g


def _load_palette_swap(ctx: Context, unit: Unit, swap: dict) -> Art:
    """A recolor of another unit: same cells, colors replaced through palette_map.json."""
    what = f"unit '{unit.id}'"
    base_unit = load_unit(ctx, swap["base"])
    if base_unit.kind != unit.kind:
        raise SpriteError(f"{what}: palette swap base '{base_unit.id}' is kind '{base_unit.kind}', not '{unit.kind}'")
    art = load_art(ctx, base_unit)
    mapping = read_json(unit.source / swap["map"]).get("colors", {})
    by_rgb = {rgb: sym for sym, rgb in art.palette.items()}
    palette = dict(art.palette)
    for src, dst in mapping.items():
        rgb = parse_hex(src, f"{what} palette map")
        if rgb not in by_rgb:
            raise SpriteError(f"{what}: palette map color {src} is not in {base_unit.id}'s palette")
        palette[by_rgb[rgb]] = parse_hex(dst, f"{what} palette map")
    art.unit_id = unit.id
    art.palette = palette
    return art
