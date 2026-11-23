"""Step 6: sheets, mask sheet, previews, motion table, report and checks; plus verify."""

from __future__ import annotations

import tempfile
from pathlib import Path

import numpy as np

from . import checks
from .assemble import Assembled, assemble_unit, measure_height, stand_anchor
from .common import (Context, SpriteError, Unit, json_bytes, list_units, load_unit, opaque, parse_hex, read_json,
                     render_rgba, write_new)
from .imaging import flatten, gif_bytes, png_bytes, same_png, zoom
from .parts import Art, load_art

OUTPUT_SUFFIXES = ("_sheet.png", "_mask.png", "_zoom.png", "_preview.gif", "_motion.json", "_report.json")


def generation_calls(unit: Unit) -> tuple[int, int]:
    """(image generation calls, candidates) recorded in generation.json."""
    path = unit.source / "generation.json"
    if not path.is_file():
        return 0, 0
    entries = read_json(path).get("generations", [])
    return sum(int(e.get("image_generation_calls", 0)) for e in entries), sum(1 for e in entries if e.get("candidate"))


def has_sprite(unit: Unit) -> bool:
    """A unit without actions (and not a palette swap) has only card art so far:
    sprite build and verify skip it."""
    return bool(unit.data.get("actions")) or bool(unit.data.get("palette_swap"))


def reference_height(ctx: Context, unit: Unit) -> tuple[int, str]:
    # A unit whose body does not fit its kind's height (long ear tufts, a low shell) states its own.
    if "height" in unit.data:
        return unit.data["height"], f"unit {unit.id}'s own height"
    kind = ctx.project["kinds"][unit.kind]
    ref = kind.get("reference_unit")
    if unit.data.get("height_reference") == "kind" or not ref or ref == unit.id:
        return kind["height"], f"kind {unit.kind}"
    report = ctx.output_dir / ref / f"{ref}_report.json"
    if not report.is_file():
        return kind["height"], f"kind {unit.kind}; reference unit {ref} not built yet"
    return int(read_json(report)["height"]), f"reference unit {ref}"


def render_outputs(ctx: Context, unit: Unit, art: Art | None = None,
                   assembled: list[Assembled] | None = None) -> tuple[dict[str, bytes], list]:
    """Builds every output in memory. Returns ({file name: bytes}, check results)."""
    art = art or load_art(ctx, unit)
    assembled = assembled or assemble_unit(ctx, unit, art)
    kind = ctx.project["kinds"][unit.kind]
    cw, ch = kind["cell"]
    foot_x, foot_y = kind["foot"]
    uid = unit.id

    cols = max(len(a.frames) for a in assembled)
    sheet = np.zeros((ch * len(assembled), cw * cols, 4), dtype=np.uint8)
    rgba_frames, standing, motion_actions = [], [], []
    for row, a in enumerate(assembled):
        frames_json = []
        for i, frame in enumerate(a.frames):
            rgba = render_rgba(frame, art.palette)
            rgba_frames.append(rgba)
            x, y = i * cw, row * ch
            sheet[y:y + ch, x:x + cw] = rgba
            frames_json.append({"name": f"{uid}_{a.action.name}_{i}", "x": x, "y": y})
            if a.action.name not in ctx.project["feet_exempt_actions"] and i not in a.action.airborne:
                standing.append((a.action.name, i, a.foot_bottoms[i]))
        # The template names the kind of big action, so a consumer can pick a sound or effect by it.
        motion_actions.append({"name": a.action.name, "template": a.action.template, "frame_ms": a.action.frame_ms,
                               "strike_frame": a.action.strike_frame, "frames": frames_json})

    measured = measure_height(ctx, unit, art, assembled)
    ref_value, ref_source = reference_height(ctx, unit)
    rig = ctx.project["rigs"][art.rig_name]
    pose = "idle frame 0 base pose" if "air_height" in rig else "idle frame 0"
    results = [
        checks.no_semi_transparent(sheet),
        checks.palette_cap(sheet, ctx.project["palette"]["max_colors"]),
        checks.transparent_background(rgba_frames),
        checks.equal_cells(rgba_frames, (cw, ch)),
        checks.height(measured, ref_value, ctx.project["height_tolerance"], ref_source, pose),
    ]
    if "air_height" in rig:
        # A flyer never stands: its body keeps the air point instead, give or take the wing-beat bob.
        results.append(checks.air_point(standing, stand_anchor(kind, rig, f"unit '{uid}'")[1], rig.get("air_bob", 0)))
    else:
        results.append(checks.feet_on_baseline(standing, foot_y))

    mask = np.zeros_like(sheet)
    mask[sheet[..., 3] == 255] = 255

    preview = ctx.project["preview"]
    bg = parse_hex(preview["background"], "project preview background")
    zoomed = flatten(zoom(sheet, preview["zoom"]), bg)
    zoomed_rgba = np.concatenate([zoomed, np.full(zoomed.shape[:2] + (1,), 255, np.uint8)], axis=2)

    gif_frames, durations = [], []
    for a in assembled:
        repeats = preview["gif_idle_loops"] if a.action.name == "idle" else 1
        for _ in range(repeats):
            for i, frame in enumerate(a.frames):
                gif_frames.append(flatten(zoom(render_rgba(frame, art.palette), preview["gif_scale"]), bg))
                hold = preview["gif_hold_last_ms"] if i == len(a.frames) - 1 and a.action.name != "idle" else 0
                durations.append(a.action.frame_ms + hold)

    motion = {
        "unit": uid,
        "kind": unit.kind,
        "sheet": f"{uid}_sheet.png",
        "mask": f"{uid}_mask.png",
        "sheet_size": {"width": int(sheet.shape[1]), "height": int(sheet.shape[0])},
        "cell": {"width": cw, "height": ch},
        "foot": {"x": foot_x, "y": foot_y},
        "display_scale": ctx.project["display_scale"],
        "actions": motion_actions,
    }

    opaque_cells = int(opaque(art.fixed).sum())
    fixed_total = sum(art.fix_counts.values())
    calls, candidates = generation_calls(unit)
    report = {
        "unit": uid,
        "palette_swap_of": unit.data.get("palette_swap", {}).get("base"),
        "colors": int(len(np.unique(sheet[sheet[..., 3] == 255][:, 0:3], axis=0))),
        "height": measured,
        "hand_fixes": {
            "opaque_cells": opaque_cells,
            "changed_cells": fixed_total,
            "percent": _percent(fixed_total, opaque_cells),
            "by_category": {c: {"cells": n, "percent": _percent(n, opaque_cells)}
                            for c, n in art.fix_counts.items() if n},
        },
        "new_cells": {**art.new_cells, "total": sum(sum(v.values()) for v in art.new_cells.values())},
        "generation": {"image_generation_calls": calls, "candidates": candidates},
        "checks": [{"name": n, "passed": ok, "detail": d} for n, ok, d in results],
    }

    files = {
        f"{uid}_sheet.png": png_bytes(sheet),
        f"{uid}_mask.png": png_bytes(mask),
        f"{uid}_zoom.png": png_bytes(zoomed_rgba),
        f"{uid}_preview.gif": gif_bytes(gif_frames, durations, bg),
        f"{uid}_motion.json": json_bytes(motion),
        f"{uid}_report.json": json_bytes(report),
    }
    return files, results


def _percent(n: int, total: int) -> float:
    return round(100.0 * n / total, 2) if total else 0.0


def failed(results) -> list[str]:
    return [f"{n}: {d}" for n, ok, d in results if not ok]


def build_unit(ctx: Context, unit: Unit, out_dir: Path, force: bool) -> list[Path]:
    files, results = render_outputs(ctx, unit)
    bad = failed(results)
    if bad:
        raise SpriteError(f"unit '{unit.id}' failed checks, nothing written:\n  " + "\n  ".join(bad))
    if not force:
        existing = [name for name in files if (out_dir / name).exists()]
        if existing:
            raise SpriteError(f"refusing to overwrite {existing} in {out_dir} (pass --force to replace them)")
    written = []
    for name, data in files.items():
        write_new(out_dir / name, data, force=True)
        written.append(out_dir / name)
    return written


def verify(ctx: Context, unit_paths: list[str] | None) -> list[str]:
    """Rebuilds units from source into a temp dir and compares them with the
    committed outputs (PNG by pixels, see same_png). Returns problems; touches nothing in the repo."""
    explicit = unit_paths is not None
    unit_paths = unit_paths if explicit else list_units(ctx)
    problems = []
    with tempfile.TemporaryDirectory(prefix="dotomata-verify-") as tmp:
        for path in unit_paths:
            unit = load_unit(ctx, path)
            if not has_sprite(unit):
                print(f"skip {path}: no sprite (no actions in unit.json)")
                continue
            committed = ctx.output_dir / path
            # Sidecar *.meta files an engine keeps beside imported files are not outputs.
            names = sorted(p.name for p in committed.glob("*") if p.is_file() and p.suffix != ".meta") \
                if committed.is_dir() else []
            if not names:
                if explicit:
                    problems.append(f"{path}: no committed outputs in {committed}")
                else:
                    print(f"skip {path}: not built yet")
                continue
            try:
                files, results = render_outputs(ctx, unit)
            except SpriteError as e:
                problems.append(f"{path}: rebuild failed: {e}")
                continue
            problems += [f"{path}: check {b}" for b in failed(results)]
            for name, data in files.items():
                (Path(tmp) / name).write_bytes(data)
            if sorted(files) != names:
                problems.append(f"{path}: file set differs: committed {names}, rebuilt {sorted(files)}")
            for name in sorted(set(files) & set(names)):
                if not same_png((committed / name).read_bytes(), (Path(tmp) / name).read_bytes()):
                    problems.append(f"{path}: {name} differs from the rebuild")
            if not any(p.startswith(f"{path}:") for p in problems):
                print(f"ok {path}: {len(files)} files match")
    return problems
