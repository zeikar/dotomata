"""Step 6 checks. Each returns (name, passed, detail) and never raises, so a
build can report every failure at once."""

from __future__ import annotations

import numpy as np


def no_semi_transparent(sheet: np.ndarray):
    alpha = sheet[..., 3]
    bad = int(((alpha != 0) & (alpha != 255)).sum())
    return "no_semi_transparent", bad == 0, f"{bad} semi-transparent pixels"


def palette_cap(sheet: np.ndarray, cap: int):
    rgb = sheet[sheet[..., 3] == 255][:, 0:3]
    n = len(np.unique(rgb, axis=0)) if len(rgb) else 0
    return "palette_cap", n <= cap, f"{n} colors (cap {cap})"


def transparent_background(frames: list[np.ndarray]):
    """Every cell's outermost rows and columns are empty: the background is
    transparent and nothing is clipped at the cell edge."""
    bad = []
    for i, f in enumerate(frames):
        a = f[..., 3]
        if a[0].any() or a[-1].any() or a[:, 0].any() or a[:, -1].any():
            bad.append(i)
    return "transparent_background", not bad, f"opaque pixels on the cell border in frames {bad}" if bad else "ok"


def equal_cells(frames: list[np.ndarray], cell: tuple[int, int]):
    sizes = sorted({(f.shape[1], f.shape[0]) for f in frames})
    ok = sizes == [tuple(cell)]
    return "equal_cell_size", ok, f"frame sizes {sizes}, expected {tuple(cell)}"


def height(measured: int, reference: int, tolerance: int, source: str, pose: str = "idle frame 0"):
    ok = abs(measured - reference) <= tolerance
    return "height", ok, f"{pose} height {measured}, reference {reference} ({source}) +-{tolerance}"


def feet_on_baseline(bottoms: list[tuple[str, int, int | None]], foot_y: int):
    """bottoms: (action, frame index, bottom row of the foot part) for frames that must stand."""
    bad = [f"{a}_{i}={b}" for a, i, b in bottoms if b != foot_y]
    return "feet_on_baseline", not bad, f"foot part bottom row != {foot_y}: {bad}" if bad else "ok"


def air_point(bottoms: list[tuple[str, int, int | None]], air_y: int, bob: int):
    """bottoms as for feet_on_baseline, for a flyer's body: its bottom row stays within bob rows of the air point."""
    bad = [f"{a}_{i}={b}" for a, i, b in bottoms if b is None or abs(b - air_y) > bob]
    return "air_point", not bad, f"body bottom row not within {air_y}+-{bob}: {bad}" if bad else "ok"
