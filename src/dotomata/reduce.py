"""Size pass: a design candidate's native grid -> the target height, and the
design reference image that `generate --design-from-file` redraws.

The image model keeps its own fine pixel grid whatever size the prompt asks
for (one humanoid came out 116-166 art px tall against 80). Reducing the adopted
design in code and asking the model to redraw that block image on the same
grid is what brought it to 80 px.
"""

from __future__ import annotations

import numpy as np

from .common import TRANSPARENT, SpriteError, check_grid_symbols, parse_hex, render_rgba


def _overlaps(n_native: int, n_target: int, factor: float) -> np.ndarray:
    """m[t, k]: how much of native cell k falls in target cell t (in native cells)."""
    m = np.zeros((n_target, n_native))
    for t in range(n_target):
        a, b = t * factor, (t + 1) * factor
        for k in range(int(np.floor(a)), min(int(np.ceil(b)), n_native)):
            m[t, k] = max(0.0, min(b, k + 1) - max(a, k))
    return m


def reduce_grid(grid: np.ndarray, palette: dict, height: int, cfg: dict) -> np.ndarray:
    """Shrinks a grid to `height` rows (width in proportion), cropped to its opaque cells.

    Each target cell takes the symbol covering the most area. When the dark
    (outline) colours cover at least `outline_min_share` of an opaque target
    cell, the most covering dark colour wins instead: a plain mode thins the
    1px outline away as soon as two native cells merge.
    """
    check_grid_symbols(grid, palette, "reduce")
    h, w = grid.shape
    if not 0 < height < h:
        raise SpriteError(f"reduce: target height {height} must be between 1 and the native height {h} (exclusive)")
    factor = h / height
    width = max(1, int(round(w / factor)))
    syms = [TRANSPARENT] + sorted(palette)
    index = {s: i for i, s in enumerate(syms)}
    g = np.vectorize(index.__getitem__)(grid)
    votes = np.einsum("yh,hws,xw->yxs", _overlaps(h, height, factor), np.eye(len(syms))[g],
                      _overlaps(w, width, factor))
    choice = np.argmax(votes, axis=2)
    limit = cfg["outline_max_luminance"] * 1000
    dark = [index[s] for s in sorted(palette) if np.dot(palette[s], (299, 587, 114)) < limit]
    if dark:
        dark_votes = votes[..., dark]
        use = (dark_votes.sum(axis=2) >= votes.sum(axis=2) * cfg["outline_min_share"]) & (choice != 0)
        choice = np.where(use, np.array(dark)[np.argmax(dark_votes, axis=2)], choice)
    out = np.array(syms)[choice]
    ys, xs = np.nonzero(out != TRANSPARENT)
    if not len(ys):
        raise SpriteError("reduce: nothing opaque is left at the target height")
    return out[ys.min():ys.max() + 1, xs.min():xs.max() + 1]


def reference_image(grid: np.ndarray, palette: dict, cfg: dict) -> np.ndarray:
    """RGB block image: each cell a ref_block square, ref_margin_blocks of background around it."""
    return block_image(render_rgba(grid, palette), cfg)


def block_image(rgba: np.ndarray, cfg: dict) -> np.ndarray:
    """An opaque-or-transparent RGBA sprite as an RGB block image on the reference
    background (also the card art's dot reference)."""
    block, margin = cfg["ref_block"], cfg["ref_margin_blocks"] * cfg["ref_block"]
    big = np.repeat(np.repeat(rgba, block, axis=0), block, axis=1)
    h, w = big.shape[:2]
    out = np.empty((h + 2 * margin, w + 2 * margin, 3), dtype=np.uint8)
    out[:] = parse_hex(cfg["ref_background"], "reduce.ref_background")
    inner = out[margin:margin + h, margin:margin + w]
    mask = big[..., 3] == 255
    inner[mask] = big[mask, :3]
    return out
