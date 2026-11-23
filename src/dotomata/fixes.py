"""Step 3: hand fixes as a cell edit list (fixes.json) applied to the grid source."""

from __future__ import annotations

from pathlib import Path

import numpy as np

from .common import TRANSPARENT, SpriteError, json_bytes, read_json


def load_fixes(path: Path) -> list[dict]:
    if not path.is_file():
        return []
    return read_json(path).get("edits", [])


def apply_fixes(grid: np.ndarray, palette: dict, edits: list[dict], categories: list[str], what: str):
    """Returns (fixed grid, {category: changed cell count}).

    A cell counts once, under the category of the last edit that touched it,
    and only if it ends up different from the converted grid.
    """
    fixed = grid.copy()
    last_category = {}
    h, w = grid.shape
    for n, edit in enumerate(edits):
        label = f"{what} fixes.json edit {n}"
        for key in ("cells", "color", "reason", "category"):
            if key not in edit:
                raise SpriteError(f"{label}: missing '{key}'")
        if edit["category"] not in categories:
            raise SpriteError(f"{label}: unknown category '{edit['category']}' (allowed: {categories})")
        if not str(edit["reason"]).strip():
            raise SpriteError(f"{label}: empty reason")
        color = edit["color"]
        if color != TRANSPARENT and color not in palette:
            raise SpriteError(f"{label}: color '{color}' is not in the palette")
        for cell in edit["cells"]:
            x, y = cell
            if not (0 <= x < w and 0 <= y < h):
                raise SpriteError(f"{label}: cell ({x},{y}) is outside the {w}x{h} grid")
            fixed[y, x] = color
            last_category[(x, y)] = edit["category"]
    counts = {c: 0 for c in categories}
    for (x, y), category in last_category.items():
        if fixed[y, x] != grid[y, x]:
            counts[category] += 1
    return fixed, counts


def edits_from_png(fixed: np.ndarray, palette: dict, rgba: np.ndarray, category: str, reason: str, what: str) -> list[dict]:
    """Turns a user-edited PNG (1 or k pixels per cell) into edit entries."""
    h, w = fixed.shape
    ih, iw = rgba.shape[:2]
    if ih % h or iw % w or ih // h != iw // w:
        raise SpriteError(f"{what}: edited PNG is {iw}x{ih}, not an integer multiple of the {w}x{h} grid")
    k = ih // h
    cells = rgba[k // 2::k, k // 2::k]
    if np.any((cells[..., 3] != 0) & (cells[..., 3] != 255)):
        raise SpriteError(f"{what}: edited PNG has semi-transparent pixels")
    lookup = {rgb: sym for sym, rgb in palette.items()}
    target = np.full((h, w), TRANSPARENT, dtype="<U1")
    for y in range(h):
        for x in range(w):
            if cells[y, x, 3] == 255:
                rgb = tuple(int(v) for v in cells[y, x, :3])
                if rgb not in lookup:
                    raise SpriteError(f"{what}: edited PNG cell ({x},{y}) color {rgb} is not in the palette")
                target[y, x] = lookup[rgb]
    by_color: dict[str, list] = {}
    ys, xs = np.nonzero(target != fixed)
    for y, x in zip(ys.tolist(), xs.tolist()):
        by_color.setdefault(str(target[y, x]), []).append([x, y])
    return [{"cells": cells_, "color": color, "reason": reason, "category": category}
            for color, cells_ in sorted(by_color.items())]


def append_edits(path: Path, new_edits: list[dict]) -> None:
    data = read_json(path) if path.is_file() else {"edits": []}
    data["edits"].extend(new_edits)
    path.write_bytes(json_bytes(data))
