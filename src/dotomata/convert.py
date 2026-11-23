"""Step 2: AI pose image -> grid source + palette.

Keys out the magenta background, estimates the square pixel grid the AI drew
on, takes one color per cell and quantizes to a small palette.
"""

from __future__ import annotations

import numpy as np

from .common import PALETTE_SYMBOLS, TRANSPARENT, SpriteError, to_hex


def key_background(rgb: np.ndarray, alpha: np.ndarray | None, cfg: dict) -> np.ndarray:
    """Returns a bool mask of background pixels (keyed color plus pink fringe)."""
    c = rgb.astype(np.int16)
    bg = np.array(cfg["background"], dtype=np.int16)
    dist = np.abs(c - bg).max(axis=2)
    is_bg = dist <= cfg["background_tolerance"]
    if alpha is not None:
        is_bg |= alpha < 128
    fringe = cfg["fringe"]
    pinkish = _pinkish(c, bg, fringe)
    for _ in range(fringe["passes"]):
        near = _touches(is_bg)
        newly = pinkish & near & ~is_bg
        if not newly.any():
            break
        is_bg |= newly
    return is_bg


def _pinkish(c: np.ndarray, bg: np.ndarray, fringe: dict) -> np.ndarray:
    # Edge pixels blended with magenta keep red and blue well above green.
    r, g, b = c[..., 0], c[..., 1], c[..., 2]
    dist = np.abs(c - bg).max(axis=-1)
    return (np.minimum(r, b) - g >= fringe["min_rb_over_g"]) & (dist <= fringe["max_distance"])


def pink_edge_cells(grid: np.ndarray, palette: dict, cfg: dict) -> tuple[int, int]:
    """(pink, total) opaque cells touching transparency or the grid border.

    Keying removes the fringe before sampling; this counts what survived it.
    """
    op = grid != TRANSPARENT
    p = np.pad(op, 1, constant_values=False)
    edge = op & ~(p[:-2, 1:-1] & p[2:, 1:-1] & p[1:-1, :-2] & p[1:-1, 2:])
    syms = sorted(palette)
    colors = np.array([palette[s] for s in syms], dtype=np.int16).reshape(-1, 3)
    pink_syms = {s for s, is_pink in zip(syms, _pinkish(colors, np.array(cfg["background"], dtype=np.int16),
                                                         cfg["fringe"])) if is_pink}
    pink = edge & np.isin(grid, list(pink_syms)) if pink_syms else np.zeros_like(edge)
    return int(pink.sum()), int(edge.sum())


def _touches(mask: np.ndarray) -> np.ndarray:
    p = np.pad(mask, 1, constant_values=False)
    return p[:-2, 1:-1] | p[2:, 1:-1] | p[1:-1, :-2] | p[1:-1, 2:]


def _edge_profile(rgb: np.ndarray, is_bg: np.ndarray, threshold: int, axis: int) -> np.ndarray:
    """Counts color changes between neighbouring columns (axis=1) or rows (axis=0)."""
    c = rgb.astype(np.int16)
    if axis == 1:
        diff = np.abs(c[:, 1:] - c[:, :-1]).sum(axis=2)
        a, b = is_bg[:, 1:], is_bg[:, :-1]
    else:
        diff = np.abs(c[1:] - c[:-1]).sum(axis=2)
        a, b = is_bg[1:], is_bg[:-1]
    # Background noise is not an edge; the outline against the background is.
    edge = ((diff > threshold) & ~(a & b)) | (a != b)
    return edge.sum(axis=0 if axis == 1 else 1).astype(np.float64)


def _axis_scores(profile: np.ndarray, sizes: np.ndarray, offset_step: float) -> tuple[np.ndarray, np.ndarray]:
    """For each pixel size, the best grid-line score over offsets and that offset."""
    n = len(profile) + 1
    centers = np.arange(n) + 0.5
    best = np.full(len(sizes), -np.inf)
    best_off = np.zeros(len(sizes))
    total = profile.sum()
    for i, s in enumerate(sizes):
        offsets = np.arange(0.0, s, offset_step)
        cells = np.floor((centers[None, :] - offsets[:, None]) / s)
        boundary = cells[:, 1:] != cells[:, :-1]
        nb = boundary.sum(axis=1)
        on = (boundary * profile[None, :]).sum(axis=1)
        # Share of the edges that fall on grid lines, minus the share a random
        # set of that many lines would catch. Halving or doubling the size
        # loses to the true size: one catches no more edges with twice the
        # lines, the other misses half of them.
        score = on / max(total, 1.0) - nb / len(profile)
        j = int(np.argmax(score))
        best[i], best_off[i] = score[j], offsets[j]
    return best, best_off


def estimate_grid(rgb: np.ndarray, is_bg: np.ndarray, cfg: dict) -> tuple[float, float, float]:
    """Finds one square pixel size and the x/y offsets of the grid.

    Using the same size on both axes stops one axis locking onto a wrong
    spacing (5.28 instead of 5.80 in the first attempt).
    """
    fg = ~is_bg
    if not fg.any():
        raise SpriteError("convert: the image has no foreground after background keying")
    ys, xs = np.nonzero(fg)
    lo, hi = cfg["pixel_size_range"]
    pad = int(np.ceil(hi)) + 1
    y0, y1 = max(ys.min() - pad, 0), min(ys.max() + pad + 1, rgb.shape[0])
    x0, x1 = max(xs.min() - pad, 0), min(xs.max() + pad + 1, rgb.shape[1])
    crop_rgb, crop_bg = rgb[y0:y1, x0:x1], is_bg[y0:y1, x0:x1]
    px = _edge_profile(crop_rgb, crop_bg, cfg["edge_threshold"], axis=1)
    py = _edge_profile(crop_rgb, crop_bg, cfg["edge_threshold"], axis=0)

    coarse = np.round(np.arange(lo, hi + 1e-9, 0.02), 4)
    sx, _ = _axis_scores(px, coarse, 0.25)
    sy, _ = _axis_scores(py, coarse, 0.25)
    s0 = coarse[int(np.argmax(sx + sy))]
    fine = np.round(np.arange(s0 - 0.03, s0 + 0.03 + 1e-9, 0.002), 4)
    fx, ox = _axis_scores(px, fine, 0.05)
    fy, oy = _axis_scores(py, fine, 0.05)
    i = int(np.argmax(fx + fy))
    # Offsets are reported in full-image coordinates.
    return float(fine[i]), float((ox[i] + x0) % fine[i]), float((oy[i] + y0) % fine[i])


def sample_cells(rgb: np.ndarray, is_bg: np.ndarray, size: float, ox: float, oy: float, cfg: dict):
    """Returns (cell colors HxWx3 int, cell opaque mask HxW) from the cell centres."""
    h, w = is_bg.shape
    margin = cfg["cell_sample_margin"]
    fx = (np.arange(w) + 0.5 - ox) / size
    fy = (np.arange(h) + 0.5 - oy) / size
    cx, cy = np.floor(fx).astype(np.int64), np.floor(fy).astype(np.int64)
    inner_x = (fx - cx >= margin) & (fx - cx <= 1 - margin) & (cx >= 0)
    inner_y = (fy - cy >= margin) & (fy - cy <= 1 - margin) & (cy >= 0)
    gw, gh = int(cx.max()) + 1, int(cy.max()) + 1

    yy, xx = np.nonzero(inner_y[:, None] & inner_x[None, :])
    cell = cy[yy] * gw + cx[xx]
    bg = is_bg[yy, xx]
    n_cells = gw * gh
    bg_count = np.bincount(cell[bg], minlength=n_cells)
    fg_count = np.bincount(cell[~bg], minlength=n_cells)
    cell_opaque = (fg_count > bg_count).reshape(gh, gw)

    # Mode on coarse bins absorbs the AI's per-pixel noise; the cell color is
    # the mean of the pixels in the winning bin.
    shift = cfg["mode_bin_shift"]
    fg_cell = cell[~bg]
    fg_rgb = rgb[yy[~bg], xx[~bg]].astype(np.int64)
    q = fg_rgb >> shift
    key = ((fg_cell * 256 + q[:, 0]) * 256 + q[:, 1]) * 256 + q[:, 2]
    uniq, inverse, counts = np.unique(key, return_inverse=True, return_counts=True)
    sums = np.zeros((len(uniq), 3), dtype=np.int64)
    np.add.at(sums, inverse, fg_rgb)
    bin_cell = uniq // (256 ** 3)
    # Largest count first, then the smaller key: a deterministic tie-break.
    order = np.lexsort((uniq, -counts, bin_cell))
    first = np.ones(len(order), dtype=bool)
    first[1:] = bin_cell[order][1:] != bin_cell[order][:-1]
    winners = order[first]
    colors = np.zeros((n_cells, 3), dtype=np.int64)
    colors[bin_cell[winners]] = (sums[winners] * 2 + counts[winners, None]) // (2 * counts[winners, None])
    return colors.reshape(gh, gw, 3), cell_opaque


def kmeans(points: np.ndarray, k: int, seed: int, iterations: int) -> tuple[np.ndarray, np.ndarray]:
    """Seeded k-means++ / Lloyd on RGB points. Returns (centers, labels)."""
    rng = np.random.default_rng(seed)
    pts = points.astype(np.float64)
    k = min(k, len(np.unique(points, axis=0)))
    centers = [pts[int(rng.integers(len(pts)))]]
    for _ in range(1, k):
        d = np.min(((pts[:, None, :] - np.array(centers)[None]) ** 2).sum(axis=2), axis=1)
        if d.sum() == 0:
            break
        centers.append(pts[int(rng.choice(len(pts), p=d / d.sum()))])
    centers = np.array(centers)
    labels = np.full(len(pts), -1)
    for _ in range(iterations):
        new = np.argmin(((pts[:, None, :] - centers[None]) ** 2).sum(axis=2), axis=1)
        if np.array_equal(new, labels):
            break
        labels = new
        for j in range(len(centers)):
            members = pts[labels == j]
            if len(members):
                centers[j] = members.mean(axis=0)
    return centers, labels


def merge_colors(centers: np.ndarray, weights: np.ndarray, max_colors: int, merge_distance: float):
    """Merges the closest pair until under the cap and no pair is closer than merge_distance.

    Returns (merged centers, groups: list of original center indices per merged color).
    """
    cs = [c.astype(np.float64) for c in centers]
    ws = [float(w) for w in weights]
    groups = [[i] for i in range(len(cs))]
    while len(cs) > 1:
        arr = np.array(cs)
        d = np.sqrt(((arr[:, None] - arr[None]) ** 2).sum(axis=2))
        np.fill_diagonal(d, np.inf)
        i, j = np.unravel_index(int(np.argmin(d)), d.shape)
        if len(cs) <= max_colors and d[i, j] >= merge_distance:
            break
        i, j = min(i, j), max(i, j)
        w = ws[i] + ws[j]
        cs[i] = (cs[i] * ws[i] + cs[j] * ws[j]) / w if w > 0 else cs[i]
        ws[i] = w
        groups[i] += groups[j]
        del cs[j], ws[j], groups[j]
    return np.array(cs), groups


def remove_isolated(opaque_mask: np.ndarray) -> np.ndarray:
    p = np.pad(opaque_mask, 1, constant_values=False)
    neighbours = p[:-2, 1:-1] | p[2:, 1:-1] | p[1:-1, :-2] | p[1:-1, 2:]
    return opaque_mask & neighbours


def convert_image(rgb: np.ndarray, alpha: np.ndarray | None, convert_cfg: dict, palette_cfg: dict):
    """Returns (grid, palette {sym: rgb}, merged_from {sym: [hex]}, info dict)."""
    is_bg = key_background(rgb, alpha, convert_cfg)
    size, ox, oy = estimate_grid(rgb, is_bg, convert_cfg)
    colors, cell_opaque = sample_cells(rgb, is_bg, size, ox, oy, convert_cfg)
    if convert_cfg["remove_isolated_cells"]:
        cell_opaque = remove_isolated(cell_opaque)
    if not cell_opaque.any():
        raise SpriteError("convert: no opaque cells after sampling")

    points = colors[cell_opaque]
    centers, labels = kmeans(points, palette_cfg["kmeans_k"], palette_cfg["seed"], palette_cfg["kmeans_iterations"])
    weights = np.bincount(labels, minlength=len(centers))
    merged, groups = merge_colors(centers, weights, palette_cfg["max_colors"], palette_cfg["merge_distance"])
    final = np.clip(np.round(merged), 0, 255).astype(np.int64)

    # Stable symbol order: dark to light, then by RGB.
    lum = final @ np.array([299, 587, 114])
    order = sorted(range(len(final)), key=lambda i: (int(lum[i]), tuple(int(v) for v in final[i])))
    if len(order) > len(PALETTE_SYMBOLS):
        raise SpriteError(f"convert: {len(order)} colors exceed the {len(PALETTE_SYMBOLS)} palette symbols")
    final = final[order]
    groups = [groups[i] for i in order]

    nearest = np.argmin(((points[:, None, :] - final[None]) ** 2).sum(axis=2), axis=1)
    grid = np.full(cell_opaque.shape, TRANSPARENT, dtype="<U1")
    grid[cell_opaque] = np.array(list(PALETTE_SYMBOLS))[nearest]

    ys, xs = np.nonzero(cell_opaque)
    y0, y1, x0, x1 = ys.min(), ys.max() + 1, xs.min(), xs.max() + 1
    grid = grid[y0:y1, x0:x1]
    palette = {PALETTE_SYMBOLS[i]: tuple(int(v) for v in final[i]) for i in range(len(final))}
    rounded_centers = np.clip(np.round(centers), 0, 255).astype(int)
    merged_from = {PALETTE_SYMBOLS[i]: sorted(to_hex(rounded_centers[j]) for j in g) for i, g in enumerate(groups)}
    info = {
        "pixel_size": round(size, 4),
        "grid_offset": [round(ox, 4), round(oy, 4)],
        "crop_cells": [int(x0), int(y0)],
        "grid_size": [int(grid.shape[1]), int(grid.shape[0])],
        "colors": len(palette),
    }
    info["pink_edge_cells"], info["edge_cells"] = pink_edge_cells(grid, palette, convert_cfg)
    return grid, palette, merged_from, info
