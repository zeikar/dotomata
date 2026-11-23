"""Fallback when the size pass keeps failing: the user
may adopt a code-reduced reference (ref_<h>px_x<b>.png written by `reduce`) as
the unit's sprite source at checkpoint 1 instead of an AI candidate.

The reference is already a clean block image of the reduced grid, so convert
reads it at its recorded block size and grid size instead of estimating a grid
and clustering colours. Symbols come from the source candidate's native
palette, which is what the reduced grid uses, so the converted grid is the
reduced grid byte for byte.
"""

from __future__ import annotations

import datetime
from pathlib import Path

import numpy as np

from .common import TRANSPARENT, SpriteError, Unit, grid_bytes, parse_hex, read_json, sha256_file, to_hex
from .convert import convert_image, pink_edge_cells
from .imaging import load_rgba, png_bytes, same_png
from .reduce import reduce_grid, reference_image

KIND = "reduced-ref"


def _record_path(unit: Unit, name: str) -> Path:
    if Path(name).name != name or not (name.startswith("ref_") and name.endswith(".png")):
        raise SpriteError(f"unit '{unit.id}': {name} is not a ref_*.png file name in {unit.source}")
    return unit.source / (name[:-len(".png")] + ".json")


def reproduce(unit: Unit, name: str) -> dict:
    """Checks that `name` is an intact `reduce` output of a recorded candidate and
    returns its record with the reduced grid and the native palette.

    Beyond the two sha256 values, the reference is rebuilt from its source with the
    recorded settings: a hand-edited picture whose record was edited to match is
    refused too."""
    what = f"unit '{unit.id}'"
    image, record_path = unit.source / name, _record_path(unit, name)
    if not image.is_file():
        raise SpriteError(f"{what}: {name} does not exist in {unit.source}")
    if not record_path.is_file():
        raise SpriteError(f"{what}: {name} has no reduce record {record_path.name} (only reduce output can be adopted)")
    rec = read_json(record_path)
    if rec.get("image") != name:
        raise SpriteError(f"{what}: {record_path.name} records {rec.get('image')}, not {name}")
    if sha256_file(image) != rec.get("sha256"):
        raise SpriteError(f"{what}: {name} does not match the sha256 in {record_path.name} (edited after reduce)")
    source = unit.source / str(rec.get("source"))
    gen_path = unit.source / "generation.json"
    recorded = read_json(gen_path)["generations"] if gen_path.is_file() else []
    if not any(g.get("candidate") == rec.get("source") for g in recorded) or not source.is_file():
        raise SpriteError(f"{what}: the reduce source {rec.get('source')} is not a recorded candidate in {unit.source}")
    if sha256_file(source) != rec.get("source_sha256"):
        raise SpriteError(f"{what}: {rec['source']} does not match the source_sha256 in {record_path.name}")
    rgb, alpha = load_rgba(source)
    native, palette, merged_from, _ = convert_image(rgb, alpha, rec["convert"], rec["palette"])
    reduced = reduce_grid(native, palette, rec["height"], rec["reduce"])
    if not same_png(png_bytes(reference_image(reduced, palette, rec["reduce"])), image.read_bytes()):
        raise SpriteError(f"{what}: reducing {rec['source']} with the settings in {record_path.name} no longer gives "
                          f"{name} (recorded tools {rec.get('tool_versions')})")
    return {"record": rec, "reduced": reduced, "palette": palette, "merged_from": merged_from}


def read_blocks(image: Path, rec: dict, palette: dict) -> np.ndarray:
    """The reference read on its own block grid: one cell per ref_block square inside
    the margin, background -> transparent, every other colour -> its palette symbol."""
    cfg = rec["reduce"]
    block, margin = cfg["ref_block"], cfg["ref_block"] * cfg["ref_margin_blocks"]
    w, h = rec["grid_size"]
    rgb, _ = load_rgba(image)
    if rgb.shape[:2] != (h * block + 2 * margin, w * block + 2 * margin):
        raise SpriteError(f"{image.name}: {rgb.shape[1]}x{rgb.shape[0]} is not a {w}x{h} grid of {block}px blocks "
                          f"with a {cfg['ref_margin_blocks']}-block margin")
    blocks = rgb[margin:margin + h * block, margin:margin + w * block].reshape(h, block, w, block, 3)
    cells = blocks[:, 0, :, 0]
    if not (blocks == cells[:, None, :, None]).all():
        raise SpriteError(f"{image.name}: not every {block}px block is one colour")
    bg = parse_hex(cfg["ref_background"], "reduce.ref_background")
    by_color = {tuple(int(v) for v in c): s for s, c in palette.items()}
    grid = np.full((h, w), TRANSPARENT, dtype="<U1")
    for y in range(h):
        for x in range(w):
            c = tuple(int(v) for v in cells[y, x])
            if c == bg:
                continue
            if c not in by_color:
                raise SpriteError(f"{image.name}: cell ({x},{y}) colour {to_hex(c)} is not in the source palette")
            grid[y, x] = by_color[c]
    return grid


def convert_ref(unit: Unit, name: str) -> tuple[np.ndarray, dict, dict, dict, dict]:
    """(grid, palette, merged_from, info, reduce record) for an adopted reduced reference."""
    found = reproduce(unit, name)
    rec = found["record"]
    grid = read_blocks(unit.source / name, rec, found["palette"])
    if grid_bytes(grid) != grid_bytes(found["reduced"]):
        raise SpriteError(f"unit '{unit.id}': {name} read at block {rec['reduce']['ref_block']} is not the reduced grid")
    used = sorted(set(grid.ravel().tolist()) - {TRANSPARENT})
    palette = {s: found["palette"][s] for s in used}
    merged_from = {s: found["merged_from"][s] for s in used}
    block = rec["reduce"]["ref_block"]
    info = {"pixel_size": block, "grid_offset": [0, 0], "crop_cells": [rec["reduce"]["ref_margin_blocks"]] * 2,
            "grid_size": [int(grid.shape[1]), int(grid.shape[0])], "colors": len(palette)}
    info["pink_edge_cells"], info["edge_cells"] = pink_edge_cells(grid, palette, rec["convert"])
    return grid, palette, merged_from, info, rec


def checkpoint_entry(unit: Unit, name: str, note: str) -> dict:
    """The checkpoint 1 approval that adopts `name`; generation.json is left as it is."""
    if not note.strip():
        raise SpriteError("a checkpoint needs a note (the user's reason)")
    if unit.data.get("palette_swap"):
        raise SpriteError(f"unit '{unit.id}' is a palette swap; only checkpoint 3 applies")
    rec = reproduce(unit, name)["record"]
    return {"checkpoint": 1, "decision": "approved", "candidate": None, "kind": KIND, "reference": name,
            "reference_sha256": rec["sha256"], "reference_source": rec["source"], "note": note,
            "date": datetime.date.today().isoformat()}
