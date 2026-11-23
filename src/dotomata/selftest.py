"""The pipeline's own checks: `dotomata selftest`.

Everything runs on small fixed inputs in a temp tree, with default_config.json
as the base config. No image is ever generated: a fake codex
(selftest_data/fake_codex.py) stands in. The card art, background and
--from-art cases are in selftest_art.py, the beast, flyer and bow cases in
selftest_rigs.py.
"""

from __future__ import annotations

import base64
import copy
import hashlib
import json
import os
import shlex
import shutil
import subprocess
import sys
import tempfile
import traceback
from pathlib import Path

import numpy as np
from PIL import Image

from . import checks
from .assemble import compose_frame, resolve_action, resolve_actions, total_offsets
from .common import (CONFIG_NAME, PALETTE_SYMBOLS, Context, SpriteError, Unit, grid_bytes, json_bytes, list_units, load_context,
                     load_unit, opaque, read_json, render_rgba, to_hex)
from .build import verify
from .convert import convert_image
from .fixes import apply_fixes, edits_from_png
from .imaging import png_bytes, same_png
from .codexrun import _result_png, count_image_calls
from .parts import Art, load_art
from .reduce import _overlaps, reduce_grid, reference_image

DATA = Path(__file__).resolve().parent / "selftest_data"

# Expected output of convert on the synthetic image below with the pinned
# Python/Pillow/numpy and the project's convert settings. A version bump that
# changes it must be a deliberate update here.
CONVERT_GOLDEN_SHA256 = "40389568a402700100d31d986e61822b3815896820edfac521fe733cc2cf2bd2"

SOURCE_SPRITE = [
    "...kkkkk....",
    "..kyyyyyk...",
    ".kyyyyyyyk..",
    ".kssssssk...",
    ".kskssksk...",
    ".kssssssk...",
    "..kkkkkk....",
    ".krrrrrrk...",
    "krrrrrrrrk..",
    "krbrrrrbrk..",
    "kssrrrrssk..",
    ".kbbbbbbk...",
    ".kbbkkbbk...",
    ".kbbk.kbbk..",
    ".kkkk.kkkk..",
    "............",
]
SOURCE_COLORS = {"k": (34, 32, 52), "y": (240, 200, 64), "s": (236, 196, 160), "r": (200, 48, 40),
                 "b": (56, 96, 200)}
PIXEL, OFFSET = 5.8, (2.3, 3.1)

TINY_GRID = [".cc..", ".aa.d", ".bbed", ".bbe.", ".g.g.", ".g.g."]
TINY_MASK = [".HH..", ".hh.W", ".ttfW", ".ttf.", ".l.l.", ".l.l."]
TINY_ARM_UP = ["...e.", "...e.", ".....", ".....", ".....", "....."]
TINY_DOWN = ["bbbaa"]
# Torso cells hidden behind the front arm, revealed when the arm swaps up.
TINY_TORSO_ADDED = [".....", ".....", "...b.", "...b.", ".....", "....."]
TINY_PALETTE = {"a": "#e0b090", "b": "#3050a0", "c": "#909090", "d": "#d0d0e0", "e": "#c08060", "g": "#403028"}
TINY_RIG = {
    "mask_symbols": {"h": "head", "t": "torso", "f": "front_arm", "l": "legs", "W": "weapon", "H": "helmet"},
    "parts": {"legs": "root", "torso": "root", "head": "torso", "front_arm": "torso"},
    "joints": {"neck": "head", "hand": "front_arm", "foot": "legs"},
    "equipment": {"weapon": "hand", "helmet": "neck"},
    "draw_order": ["legs", "torso", "head", "helmet", "weapon", "front_arm"],
    "foot_part": "legs",
    "height_excludes": ["weapon"],
    "equipment_allowance": 4,
}


class Failure(AssertionError):
    pass


def expect(cond: bool, message: str) -> None:
    if not cond:
        raise Failure(message)


def expect_error(fn, contains: str) -> None:
    try:
        fn()
    except SpriteError as e:
        expect(contains in str(e), f"error '{e}' does not mention '{contains}'")
        return
    raise Failure(f"expected an error mentioning '{contains}', got none")


def synthetic_ai_image(sprite: list[str] = SOURCE_SPRITE, colors: dict = SOURCE_COLORS, pixel: float = PIXEL,
                       offset: tuple[float, float] = OFFSET) -> np.ndarray:
    """An upscaled sprite on a noisy magenta background with a pink fringe,
    drawn at a non-integer pixel size like the AI output."""
    rng = np.random.default_rng(7)
    rows, cols = len(sprite), len(sprite[0])
    w = int(np.ceil(offset[0] + cols * pixel)) + 6
    h = int(np.ceil(offset[1] + rows * pixel)) + 6
    img = np.zeros((h, w, 3), dtype=np.int16)
    fg = np.zeros((h, w), dtype=bool)
    for y in range(h):
        for x in range(w):
            cx = int(np.floor((x + 0.5 - offset[0]) / pixel))
            cy = int(np.floor((y + 0.5 - offset[1]) / pixel))
            sym = sprite[cy][cx] if 0 <= cy < rows and 0 <= cx < cols else "."
            if sym == ".":
                img[y, x] = (249, 5, 249)
            else:
                img[y, x] = colors[sym]
                fg[y, x] = True
    noise_bg = rng.integers(-20, 21, size=img.shape)
    noise_fg = rng.integers(-6, 7, size=img.shape)
    img = np.where(fg[..., None], img + noise_fg, img + noise_bg)
    # Pink fringe: background pixels next to the figure blend half and half.
    p = np.pad(fg, 1)
    near = (p[:-2, 1:-1] | p[2:, 1:-1] | p[1:-1, :-2] | p[1:-1, 2:]) & ~fg
    shifted = np.zeros_like(img)
    for dy, dx in ((1, 0), (-1, 0), (0, 1), (0, -1)):
        src = np.roll(np.roll(img, dy, axis=0), dx, axis=1)
        srcfg = np.roll(np.roll(fg, dy, axis=0), dx, axis=1)
        shifted = np.where((srcfg & near)[..., None] & (shifted == 0).all(axis=2)[..., None], src, shifted)
    img = np.where(near[..., None], (img + shifted) // 2, img)
    return np.clip(img, 0, 255).astype(np.uint8)


# ---------------------------------------------------------------- tests


def test_convert(ctx: Context) -> None:
    rgb = synthetic_ai_image()
    grid, palette, merged_from, info = convert_image(rgb, None, ctx.project["convert"], ctx.project["palette"])
    expect(abs(info["pixel_size"] - PIXEL) < 0.02, f"pixel size {info['pixel_size']}, expected {PIXEL}")
    expect(len(palette) == len(SOURCE_COLORS), f"{len(palette)} colors, expected {len(SOURCE_COLORS)}")
    # Map each palette symbol back to the nearest source color and compare cell by cell.
    back = {}
    for sym, rgb_ in palette.items():
        name, color = min(SOURCE_COLORS.items(), key=lambda kv: sum((a - b) ** 2 for a, b in zip(kv[1], rgb_)))
        expect(max(abs(a - b) for a, b in zip(color, rgb_)) <= 3, f"palette {sym}={rgb_} is far from {name}={color}")
        back[sym] = name
    expected = np.array([list(r) for r in SOURCE_SPRITE[:-1]])
    expected = expected[:, :np.nonzero((expected != ".").any(axis=0))[0].max() + 1]
    got = np.vectorize(lambda s: back.get(s, "."))(grid)
    expect(got.shape == expected.shape, f"grid {got.shape}, expected {expected.shape}")
    diff = np.argwhere(got != expected)
    expect(len(diff) == 0, f"{len(diff)} cells differ from the source sprite, first at {diff[:1].tolist()}")
    pal_json = {"colors": {s: to_hex(c) for s, c in palette.items()}, "merged_from": merged_from}
    digest = hashlib.sha256(grid_bytes(grid) + json_bytes(pal_json)).hexdigest()
    again = convert_image(rgb, None, ctx.project["convert"], ctx.project["palette"])
    expect(grid_bytes(again[0]) == grid_bytes(grid), "convert is not deterministic within one run")
    expect(digest == CONVERT_GOLDEN_SHA256, f"convert output hash {digest} != golden {CONVERT_GOLDEN_SHA256}")
    expect(info["pink_edge_cells"] == 0 and info["edge_cells"] > 0,
           f"clean image: pink edge cells {info['pink_edge_cells']}/{info['edge_cells']}")
    # Three right-edge cells in a magenta-blended color survive keying as opaque
    # cells; the same color inside the figure is not fringe.
    sprite = SOURCE_SPRITE[:7] + [".krrrrrrp...", "krrrprrrrp..", "krbrrrrbrp.."] + SOURCE_SPRITE[10:]
    _, _, _, info = convert_image(synthetic_ai_image(sprite, {**SOURCE_COLORS, "p": (210, 100, 210)}), None,
                                  ctx.project["convert"], ctx.project["palette"])
    expect(info["pink_edge_cells"] == 3, f"pink edge cells {info['pink_edge_cells']}, expected 3")
    # A dark violet passes the r/b-over-g test but is far from the background.
    _, _, _, info = convert_image(synthetic_ai_image(sprite, {**SOURCE_COLORS, "p": (90, 10, 200)}), None,
                                  ctx.project["convert"], ctx.project["palette"])
    expect(info["pink_edge_cells"] == 0, f"dark violet counted as pink: {info['pink_edge_cells']}")


def test_convert_big_blocks(ctx: Context) -> None:
    # A humanoid's size-pass redraws came back with 12.3 and 14.9 px blocks, the
    # slime's with about 20.6. With the range capped below the block size they
    # read as half blocks, twice the height.
    expected = np.array([list(r) for r in SOURCE_SPRITE[:-1]]) != "."
    expected = expected[:, :np.nonzero(expected.any(axis=0))[0].max() + 1]
    for pixel, offset in ((12.3, (5.1, 7.7)), (14.9, (3.4, 9.2)), (20.6, (2.3, 11.4))):
        grid, _, _, info = convert_image(synthetic_ai_image(pixel=pixel, offset=offset), None, ctx.project["convert"],
                                         ctx.project["palette"])
        got = opaque(grid)
        expect(abs(info["pixel_size"] - pixel) < 0.05 and got.shape == expected.shape,
               f"{pixel} px blocks read as {info['pixel_size']} px, grid {got.shape[1]}x{got.shape[0]}, "
               f"expected {expected.shape[1]}x{expected.shape[0]}")
        expect((got == expected).all(), f"{pixel} px blocks: the opaque cells differ from the source sprite")


def random_sprite(seed: int, rows: int = 16, cols: int = 12) -> list[str]:
    """A blob with a dark outline and patches of the source colours, with an empty row and column around it."""
    rng = np.random.default_rng(seed)
    yy, xx = np.mgrid[0:rows, 0:cols]
    r = np.hypot((yy - (rows - 1) / 2) / (rows / 2 - 1.5), (xx - (cols - 1) / 2) / (cols / 2 - 1.5))
    mask = r + rng.normal(0, 0.18, size=r.shape) < 1.0
    mask[0] = mask[-1] = mask[:, 0] = mask[:, -1] = False
    p = np.pad(mask, 1)
    inner = p[:-2, 1:-1] & p[2:, 1:-1] & p[1:-1, :-2] & p[1:-1, 2:]
    patches = np.array(["y", "s", "r", "b"])[rng.integers(0, 4, size=(rows // 3 + 1, cols // 3 + 1))]
    out = np.full((rows, cols), ".")
    out[mask] = "k"
    out[mask & inner] = patches[yy // 3, xx // 3][mask & inner]
    return ["".join(row) for row in out]


def test_convert_block_sizes(ctx: Context) -> None:
    # Pictures whose block size is known: random figures drawn at both ends of pixel_size_range and at
    # whole and fractional sizes between, the 12 to 21 px blocks that were once read at half size among them.
    lo, hi = ctx.project["convert"]["pixel_size_range"]
    between = (4.0, 5.5, 8.0, 10.0, 12.0, 14.35, 16.0, 17.24, 19.0, 21.0, 23.7)
    expect(lo < min(between) and max(between) < hi, f"test sizes leave pixel_size_range {lo}..{hi}")
    for seed in (1, 2, 3):
        sprite = random_sprite(seed)
        want = np.array([list(r) for r in sprite])
        ys, xs = np.nonzero(want != ".")
        want = want[ys.min():ys.max() + 1, xs.min():xs.max() + 1]
        for i, size in enumerate((lo, *between, hi)):
            # The grid starts at a different part of a block each time.
            offset = (2.0 + (i * 0.37 * size) % size, 3.0 + (i * 0.61 * size) % size)
            grid, palette, _, info = convert_image(synthetic_ai_image(sprite, pixel=size, offset=offset), None,
                                                   ctx.project["convert"], ctx.project["palette"])
            expect(abs(info["pixel_size"] - size) <= 0.1, f"figure {seed}: {size} px blocks read as {info['pixel_size']}")
            # Each palette colour is named after the nearest drawn colour, then the cells are compared.
            back = {sym: min(SOURCE_COLORS, key=lambda n: sum((a - b) ** 2 for a, b in zip(SOURCE_COLORS[n], rgb)))
                    for sym, rgb in palette.items()}
            got = np.vectorize(lambda c: back.get(c, "."))(grid)
            expect(got.shape == want.shape, f"figure {seed} at {size} px: grid {got.shape}, drawn {want.shape}")
            diff = np.argwhere(got != want)
            expect(len(diff) == 0, f"figure {seed} at {size} px: {len(diff)} cells differ from the drawn figure, "
                                   f"first at {diff[:1].tolist()}")


def test_reduce(ctx: Context) -> None:
    cfg = ctx.project["reduce"]
    expect(np.allclose(_overlaps(5, 2, 2.5), [[1, 1, 0.5, 0, 0], [0, 0, 0.5, 1, 1]]), "fractional overlaps")
    pal = {"a": (200, 180, 160), "b": (60, 90, 200), "k": (20, 20, 20)}  # only k is dark (luminance 20)
    native = np.array([list(r) for r in ["kaaaak", "kaaabk", "kaaaba", ".k.bbb", ".k.bbb", "kk.bbb"]])
    # 3x3 blocks: top left k 3 of 9 beats a 6 of 9 (outline share 1/3); top right k 2 of 9 loses to a;
    # bottom left stays transparent although k covers 4 of 9; bottom right is plain b.
    small = reduce_grid(native, pal, 2, cfg)
    expect(as_rows(small) == ["ka", ".b"], f"reduced grid {as_rows(small)}")
    no_outline = reduce_grid(native, pal, 2, dict(cfg, outline_min_share=1.01))
    expect(as_rows(no_outline) == ["aa", ".b"], f"plain mode grid {as_rows(no_outline)}")
    expect_error(lambda: reduce_grid(native, pal, 6, cfg), "must be between")
    img = reference_image(small, pal, dict(cfg, ref_block=3, ref_margin_blocks=1, ref_background="#ff00ff"))
    expect(img.shape == (12, 12, 3), f"reference image shape {img.shape}")
    expect((img[0, 0] == (255, 0, 255)).all() and (img[3:6, 3:6] == pal["k"]).all()
           and (img[3:6, 6:9] == pal["a"]).all() and (img[6:9, 3:6] == (255, 0, 255)).all()
           and (img[6:9, 6:9] == pal["b"]).all(), "reference image blocks")


def test_convert_alpha_background(ctx: Context) -> None:
    rgb = synthetic_ai_image()
    alpha = np.full(rgb.shape[:2], 255, np.uint8)
    # A transparent background over black RGB, as some PNGs store it: only the
    # alpha channel tells it apart from a black figure.
    for band in (np.s_[:, -6:], np.s_[-6:, :]):
        rgb[band] = 0
        alpha[band] = 0
    grid, _, _, _ = convert_image(rgb, alpha, ctx.project["convert"], ctx.project["palette"])
    expect(opaque(grid).sum() == sum(c != "." for r in SOURCE_SPRITE for c in r), "alpha keying changed the figure")


def test_convert_color_cap(ctx: Context) -> None:
    # 30 well separated colors, none close to the magenta key or pink fringe.
    levels = (20, 100, 180, 250)
    picks = [(r, g, b) for r in levels for g in levels for b in levels if min(r, b) - g < 64][:30]
    expect(len(picks) == 30, f"only {len(picks)} test colors")
    colors = {PALETTE_SYMBOLS[i]: c for i, c in enumerate(picks)}
    syms = list(colors)
    sprite = ["." * 8] + ["." + "".join(syms[r * 6:r * 6 + 6]) + "." for r in range(5)] + ["." * 8]
    _, palette, _, _ = convert_image(synthetic_ai_image(sprite, colors), None, ctx.project["convert"],
                                     ctx.project["palette"])
    cap = ctx.project["palette"]["max_colors"]
    expect(len(palette) <= cap, f"{len(palette)} colors after convert, cap {cap}")


def test_convert_keeps_isolated(ctx: Context) -> None:
    # One opaque cell with no opaque neighbour, as a lone highlight in an 80px sprite.
    sprite = SOURCE_SPRITE[:-1] + ["...........y"]
    grid, _, _, _ = convert_image(synthetic_ai_image(sprite), None, ctx.project["convert"], ctx.project["palette"])
    expected = sum(c != "." for r in sprite for c in r)
    expect(opaque(grid).sum() == expected, f"{opaque(grid).sum()} opaque cells, expected {expected} with the lone cell")


def good_frames() -> list[np.ndarray]:
    frames = []
    for shift in range(2):
        f = np.zeros((12, 10, 4), np.uint8)
        f[3:11, 3 + shift:6 + shift] = (40, 50, 60, 255)
        frames.append(f)
    return frames


def test_checks(ctx: Context) -> None:
    frames = good_frames()
    sheet = np.concatenate(frames, axis=1)
    for result in (checks.no_semi_transparent(sheet), checks.palette_cap(sheet, 24),
                   checks.transparent_background(frames), checks.equal_cells(frames, (10, 12)),
                   checks.height(80, 80, 2, "t"), checks.height(82, 80, 2, "t"),
                   checks.feet_on_baseline([("idle", 0, 10), ("idle", 1, 10)], 10)):
        expect(result[1], f"good input failed {result[0]}: {result[2]}")

    semi = sheet.copy()
    semi[5, 4, 3] = 128
    expect(not checks.no_semi_transparent(semi)[1], "semi-transparent pixel not caught")
    for n in (24, 25):
        colored = np.zeros((12, 10, 4), np.uint8)
        for i in range(n):
            colored[1 + i // 8, 1 + i % 8] = (i * 9, 255 - i * 9, 100, 255)
        count = len(np.unique(colored[colored[..., 3] == 255][:, :3], axis=0))
        expect(count == n, f"test sheet has {count} colors, meant {n}")
        expect(checks.palette_cap(colored, 24)[1] == (n <= 24), f"palette cap 24 wrong for {n} colors")
    edge = [f.copy() for f in frames]
    edge[1][0, 5] = (1, 2, 3, 255)
    expect(not checks.transparent_background(edge)[1], "opaque cell border not caught")
    expect(not checks.equal_cells(frames + [np.zeros((12, 11, 4), np.uint8)], (10, 12))[1], "unequal cell not caught")
    expect(not checks.height(83, 80, 2, "t")[1], "height +3 not caught")
    expect(not checks.height(77, 80, 2, "t")[1], "height -3 not caught")
    expect(not checks.feet_on_baseline([("idle", 0, 10), ("hit", 1, 9)], 10)[1], "lifted foot not caught")


def tiny_art() -> Art:
    pal = {k: tuple(int(v[i:i + 2], 16) for i in (1, 3, 5)) for k, v in TINY_PALETTE.items()}
    grid = np.array([list(r) for r in TINY_GRID])
    mask = np.array([list(r) for r in TINY_MASK])
    layers = {}
    for sym, name in TINY_RIG["mask_symbols"].items():
        if (mask == sym).any():
            layers[name] = np.where(mask == sym, grid, ".")
    return Art(unit_id="T001", rig_name="tiny", palette=pal, fixed=grid, layers=layers,
               joints={"neck": (1, 1), "hand": (3, 2), "foot": (2, 5)},
               swaps={"front_arm@up": np.array([list(r) for r in TINY_ARM_UP])},
               variants={"down": (np.array([list(r) for r in TINY_DOWN]), (2, 0))})


def as_rows(canvas: np.ndarray) -> list[str]:
    return ["".join(r) for r in canvas]


def test_assembly(ctx: Context) -> None:
    art = tiny_art()
    cell, anchor = (10, 9), (4, 7)
    totals = total_offsets(TINY_RIG, {"torso": [0, 1], "front_arm": [1, -1], "weapon": [1, 0]}, "t")
    expect(totals["head"] == (0, 1) and totals["helmet"] == (0, 1), f"head/helmet do not follow the torso: {totals}")
    expect(totals["front_arm"] == (1, 0) and totals["weapon"] == (2, 0), f"weapon does not follow the hand: {totals}")

    canvas, bottom = compose_frame(art, TINY_RIG, {"offsets": {"torso": [0, 1], "front_arm": [1, -1], "weapon": [1, 0]}},
                                   cell, anchor, what="t")
    expected = ["..........", "..........", "..........", "...cc...d.", "...aa.e.d.",
                "...bb.e...", "...bbg....", "...g.g....", ".........."]
    expect(as_rows(canvas) == expected, "offset chain frame:\n" + "\n".join(as_rows(canvas)))
    expect(bottom == 7, f"legs bottom {bottom}, expected 7")

    canvas, _ = compose_frame(art, TINY_RIG, {"offsets": {"weapon": [-2, 0]}}, cell, anchor, what="t")
    expected = ["..........", "..........", "...cc.....", "...ad.....", "...bde....",
                "...bbe....", "...g.g....", "...g.g....", ".........."]
    expect(as_rows(canvas) == expected, "draw order frame (weapon over head, hand last):\n" + "\n".join(as_rows(canvas)))

    canvas, _ = compose_frame(art, TINY_RIG, {"swap": {"front_arm": "up"}}, cell, anchor, what="t")
    expect(canvas[2, 5] == "e" and canvas[3, 5] == "e" and canvas[4, 5] == "." and canvas[5, 5] == ".",
           "swap did not replace the arm")

    canvas, bottom = compose_frame(art, TINY_RIG, {"replace": "down"}, cell, anchor, what="t")
    expect(as_rows(canvas)[7] == "..bbbaa..." and bottom is None, "replace frame is wrong")

    expect_error(lambda: compose_frame(art, TINY_RIG, {"offsets": {"root": [6, 0]}}, cell, anchor, what="t"),
                 "leaves the 10x9 cell")
    expect_error(lambda: compose_frame(art, TINY_RIG, {"swap": {"front_arm": "nope"}}, cell, anchor, what="t"),
                 "no swap")
    expect_error(lambda: total_offsets(TINY_RIG, {"tail": [0, 1]}, "t"), "unknown layer")
    _, lifted = compose_frame(art, TINY_RIG, {"offsets": {"root": [0, -1]}}, cell, anchor, what="t")
    expect(not checks.feet_on_baseline([("hit", 0, lifted)], anchor[1])[1], "lifted root not caught by feet check")


def test_fixes(ctx: Context) -> None:
    grid = np.array([list(r) for r in TINY_GRID])
    pal = tiny_art().palette
    cats = ctx.project["fix_categories"]
    edits = [
        {"cells": [[1, 1], [2, 1]], "color": "b", "reason": "eye", "category": "face"},
        {"cells": [[2, 1]], "color": "a", "reason": "undo one", "category": "noise"},
        {"cells": [[0, 0]], "color": ".", "reason": "already empty", "category": "noise"},
        {"cells": [[4, 1]], "color": ".", "reason": "remove tip", "category": "weapon"},
    ]
    fixed, counts = apply_fixes(grid, pal, edits, cats, "t")
    expect(counts["face"] == 1 and counts["noise"] == 0 and counts["weapon"] == 1, f"fix counts {counts}")
    expect(fixed[1, 1] == "b" and fixed[1, 2] == "a" and fixed[1, 4] == ".", "fixes not applied")
    expect_error(lambda: apply_fixes(grid, pal, [{"cells": [[9, 9]], "color": "a", "reason": "x", "category": "face"}],
                                     cats, "t"), "outside")
    expect_error(lambda: apply_fixes(grid, pal, [{"cells": [[0, 0]], "color": "z", "reason": "x", "category": "face"}],
                                     cats, "t"), "not in the palette")
    expect_error(lambda: apply_fixes(grid, pal, [{"cells": [[0, 0]], "color": "a", "reason": "x", "category": "?"}],
                                     cats, "t"), "unknown category")
    edited = render_rgba(fixed, pal)
    edited = np.repeat(np.repeat(edited, 3, axis=0), 3, axis=1)
    edited[0:3, 0:3] = (*pal["c"], 255)
    new = edits_from_png(fixed, pal, edited, "other", "user edit", "t")
    expect(new == [{"cells": [[0, 0]], "color": "c", "reason": "user edit", "category": "other"}], f"png edits {new}")


# ---------------------------------------------------------------- temp tree


def tiny_project(real: dict) -> dict:
    p = copy.deepcopy(real)
    p["kinds"] = {"tiny": {"cell": [10, 9], "height": 6, "foot": [4, 7], "reference_unit": None, "rig": "tiny"},
                  "humanoid": real["kinds"]["humanoid"]}
    p["rigs"] = {"tiny": TINY_RIG}
    p["required_actions"] = {"tiny": ["idle", "down"]}
    p["templates"] = {
        "tiny_idle": {"action": "idle", "rig": "tiny",
                      "frames": [{}, {"offsets": {"torso": [0, 1]}}, {"offsets": {"torso": [0, 1]}}, {}]},
        "tiny_attack": {"action": "attack", "rig": "tiny", "strike_frame": 2, "frame_ms": 80,
                        "frames": [{"offsets": {"root": [-1, 0]}}, {"offsets": {"root": [1, 0]}},
                                   {"offsets": {"root": [2, 0]}}, {"offsets": {"root": [2, 0]}},
                                   {"offsets": {"root": [1, 0]}}, {}]},
        "tiny_down": {"action": "down", "rig": "tiny", "frames": [{"replace": "down"}]},
    }
    p["preview"] = dict(real["preview"], zoom=2, gif_scale=2)
    return p


def write_project(root: Path, project: dict) -> None:
    root.mkdir(parents=True, exist_ok=True)
    (root / CONFIG_NAME).write_bytes(json_bytes(project))


def write_tiny_unit(root: Path, uid: str, grid: list[str], mask: list[str], joints: dict,
                    unit_extra: dict | None = None, grids: dict | None = None, parts_extra: dict | None = None) -> Path:
    """A tiny-kind unit with idle and down; extra keys and grid files layer on top."""
    unit = root / "sprites/source" / uid
    unit.mkdir(parents=True)
    for name, rows in {"grid.txt": grid, "parts_mask.txt": mask, "down.txt": TINY_DOWN, **(grids or {})}.items():
        (unit / name).write_text("\n".join(rows) + "\n", encoding="utf-8")
    (unit / "palette.json").write_bytes(json_bytes({"colors": TINY_PALETTE}))
    (unit / "unit.json").write_bytes(json_bytes({
        "id": uid, "kind": "tiny", "equipment": ["weapon", "helmet"], "design": "a tiny test knight in a grey helmet",
        "actions": {"idle": "tiny_idle", "down": "tiny_down"}, **(unit_extra or {})}))
    (unit / "parts.json").write_bytes(json_bytes({
        "mask": "parts_mask.txt", "joints": joints, "added": {}, "swaps": {},
        "variants": {"down": {"file": "down.txt", "foot": [2, 0]}}, **(parts_extra or {})}))
    return unit


TINY_JOINTS = {"neck": [1, 1], "hand": [3, 2], "foot": [2, 5]}


def write_tiny_tree(root: Path, project: dict) -> None:
    write_project(root, project)
    write_tiny_unit(
        root, "T001", TINY_GRID, TINY_MASK, TINY_JOINTS,
        unit_extra={"actions": {"idle": "tiny_idle", "attack": "tiny_attack", "down": "tiny_down"},
                    "overrides": {"attack": {"strike_frame": 1, "frames": {"3": {"offsets": {"root": [1, 0]}}}}}},
        grids={"arm_up.txt": TINY_ARM_UP, "torso_added.txt": TINY_TORSO_ADDED},
        parts_extra={"added": {"torso": "torso_added.txt"}, "swaps": {"front_arm@up": "arm_up.txt"}})
    swap = root / "sprites/source/T002"
    swap.mkdir(parents=True)
    (swap / "unit.json").write_bytes(json_bytes({
        "id": "T002", "kind": "tiny", "palette_swap": {"base": "T001", "map": "palette_map.json"}}))
    (swap / "palette_map.json").write_bytes(json_bytes({"colors": {"#3050a0": "#a03050"}}))


def test_motion_rules(real_ctx: Context) -> None:
    ctx = Context(root=Path("."), project=tiny_project(real_ctx.project))
    base = {"id": "X", "kind": "tiny", "actions": {"idle": "tiny_idle", "attack": "tiny_attack", "down": "tiny_down"}}

    def resolve(project: dict | None = None, **extra):
        c = Context(root=Path("."), project=project) if project else ctx
        return resolve_actions(c, Unit("X", {**base, **extra}, Path(".")), "tiny")

    got = {a.name: (a.strike_frame, a.frame_ms) for a in resolve()}
    expect(got == {"idle": (-1, 70), "attack": (2, 80), "down": (-1, 70)}, f"strike/frame_ms from templates {got}")
    expect_error(lambda: resolve(overrides={"attack": {"strike_frame": 3}}), "strike frame 3 must be 0..2")
    short = copy.deepcopy(ctx.project)
    short["templates"]["tiny_attack"]["frames"] = short["templates"]["tiny_attack"]["frames"][:5]
    expect_error(lambda: resolve(short), "has 5 frames, expected 6")
    expect_error(lambda: resolve(overrides={"attack": {"frames": {"6": {"offsets": {"root": [1, 0]}}}}}),
                 "attack frame 6 is out of range")
    expect_error(lambda: resolve(airborne={"idle": [4]}), "airborne frames [4]")
    expect_error(lambda: resolve(overrides={"idle": {"strike_frame": 1}}), "'idle' must not have a strike frame")
    expect_error(lambda: resolve(actions={"idle": "tiny_idle"}), "requires action 'down'")
    expect_error(lambda: resolve(actions={"idle": "tiny_down", "down": "tiny_down"}), "is for action 'down', not 'idle'")

    # Every real template must resolve as the action it is for, and every rig
    # must have a template for each action it requires, so a bad edit to
    # the config fails here before any unit is built.
    p = real_ctx.project
    for tname, t in p["templates"].items():
        expect(t.get("action") in p["frames"], f"template {tname} has no known 'action'")
        expect(t["rig"] in p["rigs"], f"template {tname} uses unknown rig {t['rig']}")
        try:
            action = resolve_action(real_ctx, t["action"], tname, t["rig"], {}, [], f"template {tname}")
            for i, frame in enumerate(action.frames):
                total_offsets(p["rigs"][t["rig"]], frame.get("offsets", {}), f"template {tname} frame {i}")
        except SpriteError as e:
            raise Failure(f"config: {e}") from e
    for rig, required in p["required_actions"].items():
        expect(rig in p["rigs"], f"required_actions names unknown rig {rig}")
        for name in required:
            expect(any(t["rig"] == rig and t["action"] == name for t in p["templates"].values()),
                   f"rig {rig} requires {name} but has no template for it")
    for kind, k in p["kinds"].items():
        expect(k["rig"] is None or k["rig"] in p["rigs"], f"kind {kind} uses unknown rig {k['rig']}")


def run_cli(root: Path, *args: str, env: dict | None = None) -> subprocess.CompletedProcess:
    return subprocess.run([sys.executable, "-m", __package__, "--root", str(root), *args], capture_output=True, text=True,
                          env={**os.environ, **(env or {})}, timeout=300)


def test_build_verify(tmp: Path, real: dict) -> None:
    root = tmp / "repo"
    write_tiny_tree(root, tiny_project(real))
    r = run_cli(root, "build", "T001")
    expect(r.returncode == 0, f"build failed: {r.stdout}{r.stderr}")
    out = root / real["paths"]["output"] / "T001"
    names = sorted(p.name for p in out.iterdir())
    expect(len(names) == 6, f"outputs {names}")
    motion = json.loads((out / "T001_motion.json").read_text())
    expect([a["name"] for a in motion["actions"]] == ["idle", "attack", "down"], "motion table actions")
    expect(motion["actions"][0]["frames"][3]["name"] == "T001_idle_3", "frame names")
    templates = [a["template"] for a in motion["actions"]]
    expect(templates == ["tiny_idle", "tiny_attack", "tiny_down"], f"motion table templates {templates} (consumers pick sounds by them)")
    timing = {a["name"]: (a["strike_frame"], a["frame_ms"]) for a in motion["actions"]}
    expect(timing == {"idle": (-1, 70), "attack": (1, 80), "down": (-1, 70)},
           f"motion table strike_frame/frame_ms {timing} (unit override sets attack strike 1)")
    sheet = np.array(Image.open(out / "T001_sheet.png"))
    mask = np.array(Image.open(out / "T001_mask.png"))
    expect(sheet.shape == (27, 60, 4), f"sheet shape {sheet.shape}")
    # attack frame 3 is overridden from root +2 to +1: the idle pose moved one pixel right.
    expect((sheet[9:18, 30:40] == np.roll(sheet[0:9, 0:10], 1, axis=1)).all(), "attack frame 3 override not applied")
    report = json.loads((out / "T001_report.json").read_text())
    expect(report["new_cells"] == {"occluded": {"torso": 2}, "swaps": {"front_arm@up": 2}, "variants": {"down": 5},
                                   "total": 9}, f"new cells {report['new_cells']}")
    expect(((mask[..., 3] == 255) == (sheet[..., 3] == 255)).all() and (mask[mask[..., 3] == 255][:, :3] == 255).all(),
           "mask sheet is not white over exactly the opaque pixels")

    r = run_cli(root, "build", "T001")
    expect(r.returncode != 0 and "refusing to overwrite" in r.stderr, "second build did not refuse to overwrite")
    r = run_cli(root, "verify")
    expect(r.returncode == 0, f"verify failed on a fresh build: {r.stdout}")

    r = run_cli(root, "build", "T002")
    expect(r.returncode != 0 and "palette-swap" in r.stderr, "build accepted a palette swap unit")
    r = run_cli(root, "palette-swap", "T002")
    expect(r.returncode == 0, f"palette-swap failed: {r.stdout}{r.stderr}")
    swapped = np.array(Image.open(root / real["paths"]["output"] / "T002" / "T002_sheet.png"))
    expect(((sheet[..., :3] == (0x30, 0x50, 0xa0)).all(axis=2) == (swapped[..., :3] == (0xa0, 0x30, 0x50)).all(axis=2)).all()
           and (sheet[..., 3] == swapped[..., 3]).all(), "palette swap did not recolor exactly the mapped color")
    bad_map = root / "sprites/source/T002/palette_map.json"
    bad_map.write_bytes(json_bytes({"colors": {"#123456": "#000000"}}))
    expect_error(lambda: load_art(load_context(root), load_unit(load_context(root), "T002")), "not in T001's palette")

    sheet_path = out / "T001_sheet.png"
    data = bytearray(sheet_path.read_bytes())
    data[-20] ^= 0xFF
    sheet_path.write_bytes(bytes(data))
    r = run_cli(root, "verify", "T001")
    expect(r.returncode != 0 and "T001_sheet.png differs" in r.stdout, "verify did not catch a changed byte")

    mask_path = root / "sprites/source/T001/parts_mask.txt"
    mask_path.write_text(mask_path.read_text().replace(".l.l.", ".l.?.", 1))
    ctx = load_context(root)
    expect_error(lambda: load_art(ctx, load_unit(ctx, "T001")), "unassigned")
    mask_path.write_text("\n".join(TINY_MASK) + "\n")
    parts = root / "sprites/source/T001/parts.json"
    spec = json.loads(parts.read_text())
    spec["joints"]["foot"] = [2, 4]
    parts.write_bytes(json_bytes(spec))
    expect_error(lambda: load_art(ctx, load_unit(ctx, "T001")), "not the bottom row")
    spec["joints"]["foot"] = [2, 5]
    parts.write_bytes(json_bytes(spec))
    added = root / "sprites/source/T001/torso_added.txt"
    added.write_text("\n".join([".....", ".....", ".b.b.", "...b.", ".....", "....."]) + "\n")
    expect_error(lambda: load_art(ctx, load_unit(ctx, "T001")), "added 'torso' cell (1,2) is already part of that layer")


def test_build_checks(tmp: Path, real: dict) -> None:
    """The checks wired into build: a failing unit writes nothing."""
    root = tmp / "checks"
    project = tiny_project(real)
    project["kinds"]["tiny"].update(cell=[10, 13], foot=[4, 11])
    write_project(root, project)
    write_tiny_unit(root, "T001", TINY_GRID, TINY_MASK, TINY_JOINTS)
    # T003's weapon reaches three rows above the helmet; T004 has legs three rows longer.
    write_tiny_unit(root, "T003", ["....d"] * 3 + TINY_GRID, ["....W"] * 3 + TINY_MASK,
                    {"neck": [1, 4], "hand": [3, 5], "foot": [2, 8]})
    write_tiny_unit(root, "T004", TINY_GRID[:4] + [".g.g."] * 5, TINY_MASK[:4] + [".l.l."] * 5,
                    {"neck": [1, 1], "hand": [3, 2], "foot": [2, 8]})
    out = root / real["paths"]["output"]

    r = run_cli(root, "build", "T003")
    expect(r.returncode == 0, f"build with a raised weapon failed: {r.stdout}{r.stderr}")
    sheet = np.array(Image.open(out / "T003" / "T003_sheet.png"))
    rows = np.nonzero(sheet[0:13, 0:10, 3].any(axis=1))[0]
    expect(rows.max() - rows.min() + 1 == 9, "test figure should be 9 rows with the weapon")
    expect(json.loads((out / "T003" / "T003_report.json").read_text())["height"] == 6, "height includes the weapon")
    shutil.rmtree(out / "T003")
    no_exclude = copy.deepcopy(project)
    no_exclude["rigs"]["tiny"]["height_excludes"] = []
    write_project(root, no_exclude)
    r = run_cli(root, "build", "T003")
    expect(r.returncode != 0 and "height: idle frame 0 height 9" in r.stderr, f"weapon height not caught: {r.stderr}")
    expect(not (out / "T003").exists(), "a unit that failed its checks wrote outputs")
    write_project(root, project)

    t001 = root / "sprites/source/T001/unit.json"
    data = json.loads(t001.read_text())
    data["overrides"] = {"idle": {"frames": {"1": {"offsets": {"root": [0, -1]}}}}}
    t001.write_bytes(json_bytes(data))
    r = run_cli(root, "build", "T001")
    expect(r.returncode != 0 and "feet_on_baseline" in r.stderr and "idle_1=10" in r.stderr,
           f"lifted idle frame not caught: {r.stderr}")
    expect(not (out / "T001").exists(), "a unit that failed its checks wrote outputs")
    data["airborne"] = {"idle": [1]}
    t001.write_bytes(json_bytes(data))
    r = run_cli(root, "build", "T001")
    expect(r.returncode == 0, f"airborne frame still checked: {r.stdout}{r.stderr}")

    # The kind height alone would accept T004; the built reference unit T001 must not.
    project["kinds"]["tiny"].update(height=9, reference_unit="T001")
    write_project(root, project)
    r = run_cli(root, "build", "T004")
    expect(r.returncode != 0 and "reference 6 (reference unit T001)" in r.stderr,
           f"height not compared with the reference unit: {r.stderr}")
    project["kinds"]["tiny"]["reference_unit"] = None
    write_project(root, project)
    r = run_cli(root, "build", "T004")
    expect(r.returncode == 0, f"T004 should pass against the kind height 9: {r.stdout}{r.stderr}")

    # A unit's own height replaces both: with the kind back at 6 and T001 as the reference, the
    # 9-row T004 passes only when its own height says 9.
    shutil.rmtree(out / "T004")
    project["kinds"]["tiny"].update(height=6, reference_unit="T001")
    write_project(root, project)
    t004 = root / "sprites/source/T004/unit.json"
    data = json.loads(t004.read_text())
    data["height"] = 5
    t004.write_bytes(json_bytes(data))
    r = run_cli(root, "build", "T004")
    expect(r.returncode != 0 and "reference 5 (unit T004's own height)" in r.stderr,
           f"height not compared with the unit's own height: {r.stderr}")
    data["height"] = 9
    t004.write_bytes(json_bytes(data))
    r = run_cli(root, "build", "T004")
    expect(r.returncode == 0, f"T004 should pass against its own height 9: {r.stdout}{r.stderr}")
    # The foot row is 11 and the top row stays empty, so the tallest figure is 11; a height is a whole number.
    for bad in (0, 12, "9", 9.0, True):
        data["height"] = bad
        t004.write_bytes(json_bytes(data))
        r = run_cli(root, "build", "T004")
        expect(r.returncode != 0 and "height must be a whole number from 1 to 11" in r.stderr,
               f"own height {bad!r} accepted: {r.stdout}{r.stderr}")
    data["height"] = 11
    t004.write_bytes(json_bytes(data))
    r = run_cli(root, "checkpoint", "T004")
    expect(r.returncode == 0, f"own height 11 refused with the foot row at 11: {r.stderr}")


def test_fix_roundtrip(tmp: Path, real: dict) -> None:
    """The PNG that fix hands out for editing comes back through --from-png."""
    root = tmp / "fix"
    write_tiny_tree(root, tiny_project(real))
    r = run_cli(root, "fix", "T001")
    expect(r.returncode == 0, f"fix failed: {r.stdout}{r.stderr}")
    edit_png = root / real["paths"]["preview"] / "T001" / "fixed_edit.png"
    img = np.array(Image.open(edit_png))
    expect(img.shape == (12, 10, 4) and (img[0:2, 0:2, 3] == 0).all(), "fixed_edit.png is not a transparent 2x render")
    r = run_cli(root, "fix", "T001", "--from-png", str(edit_png), "--category", "other", "--reason", "none")
    expect(r.returncode == 0 and "appended 0 cell edits" in r.stdout, f"unedited PNG made edits: {r.stdout}{r.stderr}")
    img[0:2, 0:2] = (0x90, 0x90, 0x90, 255)  # cell (0,0) painted with palette color c
    Image.fromarray(img, "RGBA").save(edit_png)
    r = run_cli(root, "fix", "T001", "--from-png", str(edit_png), "--category", "other", "--reason", "user edit")
    expect(r.returncode == 0, f"fix --from-png rejected the edited fixed_edit.png: {r.stdout}{r.stderr}")
    edits = json.loads((root / "sprites/source/T001/fixes.json").read_text())["edits"]
    expect(edits == [{"cells": [[0, 0]], "color": "c", "reason": "user edit", "category": "other"}], f"edits {edits}")


def test_reduce_recheck_cli(tmp: Path, real: dict) -> None:
    root = tmp / "reduce"
    project = tiny_project(real)
    project["kinds"]["tiny"]["height"] = 15  # the fixture image converts to a 15-row figure
    write_tiny_tree(root, project)
    unit_dir = root / "sprites/source/T001"
    Image.fromarray(synthetic_ai_image(), "RGB").save(unit_dir / "candidate_1.png")
    stale = {"candidate": "candidate_1.png", "image_generation_calls": 1, "measured_height": 30, "height_target": 12,
             "height_check": "FAIL", "adopted": True, "reason": "adopted; misread"}
    other = {"candidate": "candidate_2.png", "image_generation_calls": 2, "measured_height": 99, "height_target": 12,
             "height_check": "FAIL", "adopted": False, "reason": "too tall"}
    (unit_dir / "generation.json").write_bytes(json_bytes({"unit": "T001", "generations": [stale, other]}))

    r = run_cli(root, "recheck", "T001", "candidate_1.png")
    expect(r.returncode == 0 and "now 15 vs 15 +-2: PASS" in r.stdout, f"recheck: {r.stdout}{r.stderr}")
    gen = json.loads((unit_dir / "generation.json").read_text())["generations"]
    expect(gen == [dict(stale, measured_height=15, height_target=15, height_check="PASS"), other],
           f"recheck changed more than the height fields: {gen}")
    # A recheck that now fails must not drop the adoption or replace the reason.
    project["kinds"]["tiny"]["height"] = 30
    write_project(root, project)
    r = run_cli(root, "recheck", "T001", "candidate_1.png")
    expect(r.returncode == 0 and "now 15 vs 30 +-2: FAIL" in r.stdout, f"recheck to FAIL: {r.stdout}{r.stderr}")
    gen = json.loads((unit_dir / "generation.json").read_text())["generations"]
    expect(gen == [dict(stale, measured_height=15, height_target=30, height_check="FAIL"), other],
           f"a failing recheck changed more than the height fields: {gen}")
    project["kinds"]["tiny"]["height"] = 15
    write_project(root, project)
    r = run_cli(root, "recheck", "T001", "candidate_9.png")
    expect(r.returncode != 0 and "not in generation.json" in r.stderr, f"unknown candidate rechecked: {r.stderr}")

    r = run_cli(root, "reduce", "T001", "--candidate", "candidate_1.png", "--height", "10")
    expect(r.returncode == 0, f"reduce failed: {r.stdout}{r.stderr}")
    ref, side = unit_dir / "ref_10px_x12.png", unit_dir / "ref_10px_x12.json"
    rec = json.loads(side.read_text())
    w, h = rec["grid_size"]
    block, margin = real["reduce"]["ref_block"], real["reduce"]["ref_block"] * real["reduce"]["ref_margin_blocks"]
    img = np.array(Image.open(ref))
    expect(h == 10 and img.shape == (h * block + 2 * margin, w * block + 2 * margin, 3), f"ref {img.shape}, {rec}")
    expect((img[0, 0] == (255, 0, 255)).all(), "ref margin is not magenta")
    expect(rec["sha256"] == hashlib.sha256(ref.read_bytes()).hexdigest()
           and rec["source_sha256"] == hashlib.sha256((unit_dir / "candidate_1.png").read_bytes()).hexdigest()
           and (rec["source"], rec["height"], rec["reduce"]) == ("candidate_1.png", 10, real["reduce"])
           and abs(rec["native"]["pixel_size"] - PIXEL) < 0.02, f"reduce record {rec}")
    first = ref.read_bytes()
    r = run_cli(root, "reduce", "T001", "--candidate", "candidate_1.png", "--height", "10")
    expect(r.returncode != 0 and "refusing to overwrite" in r.stderr, f"reduce overwrote: {r.stderr}")
    r = run_cli(root, "reduce", "T001", "--candidate", "candidate_1.png", "--height", "10", "--force")
    expect(r.returncode == 0 and ref.read_bytes() == first, "reduce is not reproducible byte for byte")
    (unit_dir / "stray.png").write_bytes((unit_dir / "candidate_1.png").read_bytes())
    r = run_cli(root, "reduce", "T001", "--candidate", "stray.png")
    expect(r.returncode != 0 and "not a recorded candidate" in r.stderr, f"unrecorded source reduced: {r.stderr}")


def fake_codex_env(tmp: Path) -> tuple[dict, Path, Path, Path]:
    """(env, call log, fixture image, fake codex script) for running generate against the fake codex."""
    home = tmp / "codex_home"
    home.mkdir()
    image = tmp / "fixture_ai.png"
    Image.fromarray(synthetic_ai_image(), "RGB").save(image)
    log = tmp / "fake_codex_calls.jsonl"
    fake = DATA / "fake_codex.py"
    env = {"DOTOMATA_CODEX_CMD": f"{shlex.quote(sys.executable)} {shlex.quote(str(fake))}", "CODEX_HOME": str(home),
           "FAKE_CODEX_IMAGE": str(image), "FAKE_CODEX_LOG": str(log)}
    return env, log, image, fake


def own_image(log: Path, index: int, home: Path) -> Path:
    """The file the index-th fake codex run left in its own thread's generated_images folder."""
    args = json.loads(log.read_text().splitlines()[index])["args"]
    events = Path(args[args.index("-C") + 1]) / "codex_events.jsonl"
    thread = json.loads(events.read_text().splitlines()[0])["thread_id"]
    return home / "generated_images" / thread / "ig_fixture_2.png"


def test_generate(tmp: Path, real: dict) -> None:
    root = tmp / "gen"
    project = tiny_project(real)
    project["kinds"]["tiny"]["height"] = 15  # the fixture image converts to a 15-row figure
    project["generation"]["cap_per_unit"] = 3  # small cap so single runs reach it
    write_tiny_tree(root, project)
    env, log, image, fake = fake_codex_env(tmp)
    home = Path(env["CODEX_HOME"])

    def status(uid: str) -> dict:
        r = run_cli(root, "checkpoint", uid)
        expect(r.returncode == 0, f"checkpoint status failed: {r.stderr}")
        return json.loads(r.stdout)

    counted = count_image_calls((fake.parent / "exec_events_json_item.jsonl").read_text(), home)
    expect(counted["json_items"] == 1 and counted["calls"] == 1, f"--json fixture counted {counted}")

    # codex-cli 0.162.1 logs each generation as an item_completed event whose item is an
    # Extension of kind image_gen.generation (structure copied from a real run).
    ext_thread = "019f0000-0000-7000-8000-00000000c003"
    ext_text = (fake.parent / "rollout_extension_two_calls.jsonl").read_text()
    session = home / "sessions" / "2026" / "10" / "16"
    session.mkdir(parents=True)
    ext_rollout = session / f"rollout-2026-10-16T00-00-00-{ext_thread}.jsonl"
    ext_events = json.dumps({"type": "thread.started", "thread_id": ext_thread})
    ext_rollout.write_text(ext_text)
    counted = count_image_calls(ext_events, home)
    expect((counted["rollout_calls"], counted["calls"], counted["generated_files"]) == (2, 2, []),
           f"Extension fixture counted {counted}")
    # Both fixture records carry a result (only the PNG signature): the picture comes from the record.
    expect((counted["image"], counted["image_source"], counted["broken_results"])
           == (b"\x89PNG\r\n\x1a\n", "session record", 0), f"Extension fixture image {counted}")
    for rec, want in (({"status": "completed", "result": "###"}, (None, True)),       # not base64
                      ({"status": "completed", "result": "bm90IGEgcG5n"}, (None, True)),  # base64 of "not a png"
                      ({"status": "generating", "result": "iVBORw0KGgo="}, (None, False)),
                      ({"status": "completed"}, (None, False))):
        expect(_result_png(rec) == want, f"_result_png({rec}) = {_result_png(rec)}")

    # Which record's bytes are the run's picture, on session records written here.
    def png(tag: bytes) -> bytes:
        return b"\x89PNG\r\n\x1a\n" + tag

    def old_shape(gen_id: str, status: str, data: bytes) -> dict:
        return {"type": "response_item", "payload": {"type": "image_generation_call", "id": gen_id, "status": status,
                                                     "result": base64.b64encode(data).decode()}}

    def extension(gen_id: str, status: str, data: bytes) -> dict:
        return {"type": "event_msg", "payload": {"type": "item_completed", "item": {
            "type": "Extension", "kind": "image_gen.generation", "id": gen_id, "status": status,
            "result": base64.b64encode(data).decode()}}}

    def picked(thread: str, records: list[dict], folder_pngs: tuple[bytes, ...] = ()) -> dict:
        (session / f"rollout-2026-10-16T00-00-00-{thread}.jsonl").write_text("\n".join(json.dumps(r) for r in records))
        for i, data in enumerate(folder_pngs):
            (home / "generated_images" / thread).mkdir(parents=True, exist_ok=True)
            # Named against the order they are written in: the order comes from the write time.
            path = home / "generated_images" / thread / f"ig_{9 - i}.png"
            path.write_bytes(data)
            os.utime(path, (1_000_000 + i, 1_000_000 + i))
        return count_image_calls(json.dumps({"type": "thread.started", "thread_id": thread}), home)

    c = picked("thread-old-shape", [old_shape("ig_1", "completed", png(b"old"))])
    expect((c["image"], c["image_source"], c["calls"]) == (png(b"old"), "session record", 1), f"old shape result: {c}")
    c = picked("thread-all-broken", [extension("e1", "completed", b"junk")],
               folder_pngs=(png(b"folder first"), png(b"folder later")))
    expect((c["image"], c["image_source"], c["broken_results"]) == (png(b"folder first"), "generated_images", 1),
           f"every record broken, the folder's first file not used: {c}")
    c = picked("thread-failed-last", [extension("e1", "completed", png(b"one")), extension("e2", "failed", png(b"two"))])
    expect((c["image"], c["calls"]) == (png(b"one"), 2), f"a failed record's result was used: {c}")
    c = picked("thread-broken-then-good", [extension("e1", "completed", b"junk"), extension("e1", "completed", png(b"ok"))])
    expect((c["image"], c["broken_results"]) == (png(b"ok"), 0), f"a generation with a good copy counted as skipped: {c}")
    c = picked("thread-two", [extension("e1", "completed", png(b"one")), extension("e2", "completed", png(b"two"))])
    expect((c["image"], c["calls"]) == (png(b"one"), 2), f"a second generation replaced the first: {c}")
    c = picked("thread-failed-first", [extension("e1", "failed", png(b"one")), extension("e2", "completed", png(b"two"))])
    expect(c["image"] == png(b"two"), f"a failed first record's result was used: {c}")
    # The order is where a generation first appears, even if its good copy is written later.
    c = picked("thread-late-copy", [extension("e1", "completed", b"junk"), extension("e2", "completed", png(b"two")),
                                    extension("e1", "completed", png(b"one"))])
    expect((c["image"], c["broken_results"]) == (png(b"one"), 0), f"a later generation went before the first: {c}")

    # Only a call that names the image tool counts as one: a shell call in the same shape does not.
    def tool_call(source: str) -> dict:
        return {"type": "response_item", "payload": {"type": "custom_tool_call", "name": "exec", "input": source}}

    c = picked("thread-tool-calls", [tool_call("const r = await tools.image_gen__imagegen({prompt: 'x'});"),
                                     tool_call("const r = await tools.shell({command: ['ls']});")])
    expect((c["tool_calls"], c["calls"], c["image"]) == (1, 0, None), f"image tool calls counted {c}")
    ext_rollout.write_text(ext_text + ext_text)  # the same item ids again count once
    counted = count_image_calls(ext_events, home)
    expect(counted["calls"] == 2, f"repeated Extension items counted {counted}")

    unit_dir = root / "sprites/source/T001"
    unit_json = json.loads((unit_dir / "unit.json").read_text())
    unit_json["design"] = "a knight in the style of MapleStory"
    (unit_dir / "unit.json").write_bytes(json_bytes(unit_json))
    r = run_cli(root, "generate", "T001", env={**env, "FAKE_CODEX_SCENARIO": "rollout"})
    expect(r.returncode != 0 and "game or artist names" in r.stderr, f"banned name not refused: {r.stderr}")
    expect(not log.exists(), "codex was called for a refused prompt")
    unit_json["design"] = "a tiny test knight in a grey helmet"
    (unit_dir / "unit.json").write_bytes(json_bytes(unit_json))

    ref = tmp / "reference.png"
    Image.fromarray(synthetic_ai_image(), "RGB").save(ref)
    r = run_cli(root, "generate", "T001", "--reference", str(ref), env={**env, "FAKE_CODEX_SCENARIO": "rollout"})
    expect(r.returncode == 0, f"generate 1 failed: {r.stdout}{r.stderr}")
    expect("measured height 15 vs target 15 +-2: PASS" in r.stdout, f"height line: {r.stdout}")
    gen = json.loads((unit_dir / "generation.json").read_text())
    e = gen["generations"][0]
    expect(e["image_generation_calls"] == 2, f"rollout fixture counted {e['image_generation_calls']}, expected 2")
    # Two generations in the session record and one file in the thread's folder: kept, with the note.
    expect("generated more than once" in (e["note"] or ""), f"no note on a run that generated twice: {e['note']}")
    expect(e["candidate"] == "candidate_1.png" and (unit_dir / "candidate_1.png").is_file(), "candidate_1 not saved")
    # The candidate is this run's own generated file (the fake tags each run's bytes).
    expect(e["output_sha256"] == hashlib.sha256(own_image(log, 0, home).read_bytes()).hexdigest()
           == hashlib.sha256((unit_dir / "candidate_1.png").read_bytes()).hexdigest(), "output sha256")
    expect(e["reference"]["sha256"] == hashlib.sha256(ref.read_bytes()).hexdigest(), "reference sha256")
    expect(e["reference"]["mode"] == "scale", f"reference mode {e['reference']}")
    size = real["generation"]["size_sentence"].format(height=15, cell_width=10, cell_height=9)
    expect(size in e["prompt"] and "{" not in size, f"size sentence missing from the prompt: {e['prompt']}")
    expect(real["generation"]["design_reference_sentence"] not in e["prompt"], "design sentence in a scale prompt")
    expect((e["measured_height"], e["height_target"], e["height_check"], e["adopted"]) == (15, 15, "PASS", None),
           f"height fields {e}")
    expect(real["generation"]["scale_reference_sentence"] in e["prompt"], "scale sentence missing with a reference")
    expect(e["codex_version"] == "codex-cli 0.0.0-selftest" and e["model"] == real["generation"]["model"], "codex info")
    call = json.loads(log.read_text().splitlines()[0])
    for flag in ("--skip-git-repo-check", "--json", "project_doc_max_bytes=0", "-i"):
        expect(flag in call["args"], f"codex exec args lack {flag}: {call['args']}")
    workdir = Path(call["args"][call["args"].index("-C") + 1])
    expect(root.resolve() not in workdir.resolve().parents, "codex ran inside the repo")

    # Too tall for a 12 target, but T001 carries a weapon and 15 is within the allowance
    # (12 + 4): shown for review, not dropped.
    project["kinds"]["tiny"]["height"] = 12
    write_project(root, project)
    (unit_dir / "candidate_2.png").write_bytes(b"placeholder")  # an existing file is never overwritten
    r = run_cli(root, "generate", "T001", env={**env, "FAKE_CODEX_SCENARIO": "json"})
    expect(r.returncode == 0, f"generate 2 failed: {r.stdout}{r.stderr}")
    expect("measured height 15 vs target 12 +-2: REVIEW" in r.stdout, f"height line: {r.stdout}")
    gen = json.loads((unit_dir / "generation.json").read_text())
    e = gen["generations"][1]
    expect(e["candidate"] == "candidate_3.png", "existing candidate_2.png was not skipped")
    expect((unit_dir / "candidate_2.png").read_bytes() == b"placeholder", "candidate_2.png was overwritten")
    expect(e["image_generation_calls"] == 1, "json fixture count")
    expect((e["height_check"], e["adopted"]) == ("REVIEW", None), f"weapon carrier height fields {e}")
    st = status("T001")
    expect(st["candidates"] == 2 and st["image_generation_calls"] == 3, f"status counts {st}")

    r = run_cli(root, "generate", "T001", env={**env, "FAKE_CODEX_SCENARIO": "json"})
    expect(r.returncode != 0 and "3 of 3" in r.stderr, f"cap did not refuse: {r.stdout}{r.stderr}")
    expect(len(log.read_text().splitlines()) == 2, "codex was called after the cap")

    # T003 carries no weapon or staff, so the same too-tall figure is dropped.
    t003 = root / "sprites/source/T003"
    t003.mkdir()
    (t003 / "unit.json").write_bytes(json_bytes({"id": "T003", "kind": "tiny", "equipment": ["helmet"],
                                                 "design": "a tiny test squire", "actions": {}}))
    r = run_cli(root, "generate", "T003", env={**env, "FAKE_CODEX_SCENARIO": "json"})
    expect(r.returncode == 0 and "measured height 15 vs target 12 +-2: FAIL" in r.stdout, f"FAIL line: {r.stdout}")
    e = json.loads((t003 / "generation.json").read_text())["generations"][0]
    expect((e["height_check"], e["adopted"], e["reason"]) == ("FAIL", False, "height 15 vs 12+-2"),
           f"dropped candidate fields {e}")

    # A candidate that is not an image is still recorded and charged.
    r = run_cli(root, "generate", "T003", env={**env, "FAKE_CODEX_SCENARIO": "badpng"})
    expect(r.returncode != 0 and "height check failed" in r.stdout, f"bad candidate: {r.stdout}{r.stderr}")
    e = json.loads((t003 / "generation.json").read_text())["generations"][-1]
    expect(e["candidate"] == "candidate_2.png" and e["image_generation_calls"] >= 1, f"bad candidate entry {e}")
    expect(status("T003")["image_generation_calls"] == 2, "bad candidate not counted toward the cap")

    # No image and no session record: the count cannot be confirmed, so one call is charged.
    empty_home = tmp / "codex_home_empty"
    empty_home.mkdir()
    r = run_cli(root, "generate", "T003", env={**env, "CODEX_HOME": str(empty_home), "FAKE_CODEX_SCENARIO": "noimage"})
    expect(r.returncode != 0 and "without an image" in r.stdout, f"no-image run: {r.stdout}{r.stderr}")
    e = json.loads((t003 / "generation.json").read_text())["generations"][-1]
    expect(e["candidate"] is None and e["image_generation_calls"] == 1, f"unconfirmed run charged {e}")

    # T006 states its own height: the same 15-row figure that failed the kind's 12 passes its own 15,
    # and the size pass reduces to the own height without --height. An own height must fit the cell,
    # so the tiny cell grows for this case only.
    small_cell = dict(project["kinds"]["tiny"])
    project["kinds"]["tiny"].update(cell=[10, 20], foot=[4, 18])
    write_project(root, project)
    t006 = root / "sprites/source/T006"
    t006.mkdir()
    own = {"id": "T006", "kind": "tiny", "equipment": ["helmet"], "design": "a tiny tall test squire", "actions": {},
           "height": 15}
    (t006 / "unit.json").write_bytes(json_bytes(own))
    r = run_cli(root, "generate", "T006", env={**env, "FAKE_CODEX_SCENARIO": "json"})
    e = json.loads((t006 / "generation.json").read_text())["generations"][0]
    expect(r.returncode == 0 and (e["measured_height"], e["height_target"], e["height_check"]) == (15, 15, "PASS"),
           f"own height not the generate-time target: {e} {r.stdout}{r.stderr}")
    (t006 / "unit.json").write_bytes(json_bytes({**own, "height": 10}))
    r = run_cli(root, "reduce", "T006", "--candidate", "candidate_1.png")
    expect(r.returncode == 0 and len(list(t006.glob("ref_10px_x*.png"))) == 1,
           f"reduce did not take the unit's own height: {r.stdout}{r.stderr}")
    project["kinds"]["tiny"] = small_cell

    # T005 carries a weapon, but 15 is far over the target 8 + allowance 4: the figure itself is
    # too big, so it is dropped instead of reviewed.
    project["kinds"]["tiny"]["height"] = 8
    write_project(root, project)
    t005 = root / "sprites/source/T005"
    t005.mkdir()
    (t005 / "unit.json").write_bytes(json_bytes({"id": "T005", "kind": "tiny", "equipment": ["weapon"],
                                                 "design": "a tiny test swordsman", "actions": {}}))
    r = run_cli(root, "generate", "T005", env={**env, "FAKE_CODEX_SCENARIO": "json"})
    expect(r.returncode == 0 and "measured height 15 vs target 8 +-2: FAIL" in r.stdout, f"weapon FAIL line: {r.stdout}")
    e = json.loads((t005 / "generation.json").read_text())["generations"][0]
    expect((e["height_check"], e["adopted"], e["reason"]) ==
           ("FAIL", False, "height 15 vs 8+-2, over the equipment allowance +4"), f"weapon carrier dropped fields {e}")

    # --design-from redraws the same unit from its own candidate; it excludes --reference.
    calls_before = len(log.read_text().splitlines())
    r = run_cli(root, "generate", "T005", "--design-from", "candidate_1.png", "--reference", str(ref),
                env={**env, "FAKE_CODEX_SCENARIO": "json"})
    expect(r.returncode != 0 and "not allowed with" in r.stderr, f"both reference modes accepted: {r.stderr}")
    (t005 / "stray.png").write_bytes((t005 / "candidate_1.png").read_bytes())
    r = run_cli(root, "generate", "T005", "--design-from", "stray.png", env={**env, "FAKE_CODEX_SCENARIO": "json"})
    expect(r.returncode != 0 and "recorded candidates" in r.stderr, f"unrecorded design source accepted: {r.stderr}")
    expect(len(log.read_text().splitlines()) == calls_before, "codex was called for a refused reference")
    r = run_cli(root, "generate", "T005", "--design-from", "candidate_1.png", env={**env, "FAKE_CODEX_SCENARIO": "json"})
    expect(r.returncode == 0, f"generate --design-from failed: {r.stdout}{r.stderr}")
    e = json.loads((t005 / "generation.json").read_text())["generations"][1]
    expect(e["reference"] == {"mode": "design", "path": "sprites/source/T005/candidate_1.png",
                              "sha256": hashlib.sha256((t005 / "candidate_1.png").read_bytes()).hexdigest()},
           f"design reference record {e['reference']}")
    expect(real["generation"]["design_reference_sentence"] in e["prompt"]
           and real["generation"]["scale_reference_sentence"] not in e["prompt"], f"design prompt {e['prompt']}")
    call = json.loads(log.read_text().splitlines()[-1])
    expect(str((t005 / "candidate_1.png").resolve()) in call["args"], f"design source not passed with -i: {call['args']}")

    def checkpoint(*args: str) -> subprocess.CompletedProcess:
        return run_cli(root, "checkpoint", "T001", *args)

    def adopted(index: int):
        return json.loads((unit_dir / "generation.json").read_text())["generations"][index]["adopted"]

    expect(checkpoint("--require", "1").returncode != 0, "require passed without a checkpoint")
    r = checkpoint("--record", "2", "--approve", "--note", "x")
    expect(r.returncode != 0 and "checkpoint 1 is not approved" in r.stderr, "checkpoint 2 recorded before 1")
    r = checkpoint("--record", "1", "--reject", "--candidate", "candidate_1.png", "--note", "face too dark")
    expect(r.returncode == 0, f"reject 1 failed: {r.stderr}")
    expect(checkpoint("--require", "1").returncode != 0, "require 1 passed after a rejection")
    r = checkpoint("--record", "2", "--approve", "--note", "x")
    expect(r.returncode != 0 and "checkpoint 1 is not approved" in r.stderr, "checkpoint 2 recorded after a rejected 1")
    expect(adopted(0) is False, "rejection not written to generation.json")
    r = checkpoint("--record", "1", "--approve", "--candidate", "candidate_1.png", "--note", "clean silhouette")
    expect(r.returncode == 0, f"checkpoint 1 failed: {r.stderr}")
    gen = json.loads((unit_dir / "generation.json").read_text())
    expect(gen["generations"][0]["adopted"] is True and gen["generations"][0]["reason"] == "clean silhouette",
           "adoption not written to generation.json")
    expect(checkpoint("--require", "1").returncode == 0, "require failed after approval")
    expect(checkpoint("--record", "2", "--approve", "--note", "fixes ok").returncode == 0, "checkpoint 2 failed")
    expect(checkpoint("--require", "2").returncode == 0, "require 2 failed after approval")
    # Redoing checkpoint 1 voids the approval of 2 until it is shown again.
    r = checkpoint("--record", "1", "--approve", "--candidate", "candidate_3.png", "--note", "taller one after all")
    expect(r.returncode == 0, f"re-approve 1 failed: {r.stderr}")
    expect(checkpoint("--require", "1").returncode == 0, "require 1 failed after re-approval")
    expect(checkpoint("--require", "2").returncode != 0, "checkpoint 2 still counts after 1 was redone")
    expect(status("T001")["approved"] == {"1": True, "2": False, "3": False}, f"status approved {status('T001')}")
    r = checkpoint("--record", "1", "--reject", "--candidate", "candidate_3.png", "--note", "no, too tall")
    expect(r.returncode == 0 and checkpoint("--require", "1").returncode != 0, "approve then reject still approved")

    r = run_cli(root, "checkpoint", "T002", "--record", "1", "--approve", "--candidate", "x.png", "--note", "x")
    expect(r.returncode != 0 and "only checkpoint 3" in r.stderr, "palette swap accepted checkpoint 1")
    r = run_cli(root, "checkpoint", "T002", "--record", "3", "--approve", "--note", "matches T001 side by side")
    expect(r.returncode == 0, f"palette swap checkpoint 3 failed: {r.stderr}")
    expect(run_cli(root, "checkpoint", "T002", "--require", "3").returncode == 0, "palette swap 3 not approved")


def test_generate_batch(tmp: Path, real: dict) -> None:
    root = tmp / "gen"
    project = tiny_project(real)
    project["kinds"]["tiny"]["height"] = 15
    project["generation"]["cap_per_unit"] = 5
    project["generation"]["batch_max"] = 3
    write_tiny_tree(root, project)
    env, log, image, _ = fake_codex_env(tmp)
    env["FAKE_CODEX_SCENARIO"] = "json"
    unit_dir = root / "sprites/source/T001"

    def calls() -> list[dict]:
        return [json.loads(line) for line in log.read_text().splitlines()] if log.exists() else []

    def entries(uid: str = "T001") -> list[dict]:
        path = root / "sprites/source" / uid / "generation.json"
        return json.loads(path.read_text())["generations"] if path.exists() else []

    r = run_cli(root, "generate", "T001", "--batch", "4", env=env)
    expect(r.returncode != 0 and "1..3" in r.stderr, f"batch over batch_max accepted: {r.stdout}{r.stderr}")
    r = run_cli(root, "generate", "T001", "--variant", "a", "--variant", "b", env=env)
    expect(r.returncode != 0 and "at most one per run" in r.stderr, f"extra variants accepted: {r.stderr}")
    expect(not calls(), "codex was called for a refused batch")

    # Three runs at once; candidate_2.png exists, so the parent reserves 1, 3 and 4.
    (unit_dir / "candidate_2.png").write_bytes(b"placeholder")
    r = run_cli(root, "generate", "T001", "--batch", "3", "--variant", "VARIANT-ONE", "--variant", "VARIANT-TWO",
                env={**env, "FAKE_CODEX_SLEEP": "1"})
    expect(r.returncode == 0, f"batch of 3 failed: {r.stdout}{r.stderr}")
    runs = calls()
    expect(len(runs) == 3, f"{len(runs)} codex runs for a batch of 3")
    expect(max(c["start"] for c in runs) < min(c["end"] for c in runs), "batch runs did not overlap")
    workdirs = {c["args"][c["args"].index("-C") + 1] for c in runs}
    expect(len(workdirs) == 3 and all(root.resolve() not in Path(w).resolve().parents for w in workdirs),
           f"runs did not get their own temp dirs outside the repo: {workdirs}")
    es = entries()
    expect([e["candidate"] for e in es] == ["candidate_1.png", "candidate_3.png", "candidate_4.png"],
           f"reserved candidates {[e['candidate'] for e in es]}")
    expect((unit_dir / "candidate_2.png").read_bytes() == b"placeholder", "candidate_2.png was overwritten")
    for e in es:
        expect(e["output_sha256"] == hashlib.sha256((unit_dir / e["candidate"]).read_bytes()).hexdigest()
               and e["image_generation_calls"] == 1 and e["batch"] == "1-4"
               and e["height_check"] == "PASS" and e["error"] is None, f"batch entry {e}")
    expect(len({e["output_sha256"] for e in es}) == 3, "the runs of one batch did not keep their own images")
    expect([e["variant"] for e in es] == ["VARIANT-ONE", "VARIANT-TWO", None], f"variants {[e['variant'] for e in es]}")
    expect("VARIANT-ONE" in es[0]["prompt"] and "VARIANT-TWO" not in es[0]["prompt"]
           and "VARIANT-TWO" in es[1]["prompt"] and "VARIANT-ONE" not in es[1]["prompt"]
           and "VARIANT" not in es[2]["prompt"], "variants landed in the wrong runs")
    expect(sorted(c["prompt"] for c in runs) == sorted(e["prompt"] for e in es), "recorded prompts differ from sent ones")
    sheet = root / project["paths"]["preview"] / "T001" / "batch_1-4.png"
    expect(sheet.is_file(), f"no comparison sheet at {sheet}")
    with Image.open(sheet) as im:
        expect(im.height > project["preview"]["batch_sheet_height"], f"sheet size {im.size}")

    # 3 of 5 used: a batch of 3 would exceed the cap and is refused before launching.
    r = run_cli(root, "generate", "T001", "--batch", "3", env=env)
    expect(r.returncode != 0 and "3 of 5" in r.stderr and "batch of 3" in r.stderr, f"cap did not refuse: {r.stderr}")
    expect(len(calls()) == 3, "codex was called for a batch over the cap")

    # A batch that partly fails records every run; the failed one is charged.
    r = run_cli(root, "generate", "T001", "--batch", "2", "--variant", "fine", "--variant", "BREAK-THIS",
                env={**env, "FAKE_CODEX_FAIL_ON": "BREAK-THIS"})
    expect(r.returncode != 0 and "codex exited 1" in r.stdout, f"partial failure: {r.stdout}{r.stderr}")
    es = entries()
    expect(len(es) == 5, f"{len(es)} entries after a partly failed batch")
    expect(es[3]["candidate"] == "candidate_5.png" and es[3]["error"] is None and es[3]["batch"] == "5-6",
           f"good run of the failed batch {es[3]}")
    expect(es[4]["candidate"] is None and es[4]["image_generation_calls"] == 1 and "codex exited 1" in es[4]["error"]
           and es[4]["variant"] == "BREAK-THIS", f"failed run {es[4]}")
    expect(not (unit_dir / "candidate_6.png").exists(), "a failed run left a candidate")
    r = run_cli(root, "checkpoint", "T001")
    expect(json.loads(r.stdout)["image_generation_calls"] == 5, f"status after the failed batch {r.stdout}")
    r = run_cli(root, "generate", "T001", env=env)
    expect(r.returncode != 0 and "5 of 5" in r.stderr, f"cap did not refuse at 5 of 5: {r.stderr}")

    # --design-from-file: a prepared image inside the unit folder, not a candidate.
    t006 = root / "sprites/source/T006"
    t006.mkdir()
    (t006 / "unit.json").write_bytes(json_bytes({"id": "T006", "kind": "tiny", "equipment": ["weapon"],
                                                 "design": "a tiny test guard", "actions": {}}))
    outside = tmp / "ref_outside.png"
    Image.fromarray(synthetic_ai_image(), "RGB").save(outside)
    shutil.copyfile(outside, t006 / "ref_x12.png")
    before = len(calls())
    for extra, message in ((["--reference", str(outside)], "not allowed with"),
                           (["--design-from", "candidate_1.png"], "not allowed with")):
        r = run_cli(root, "generate", "T006", "--design-from-file", "ref_x12.png", *extra, env=env)
        expect(r.returncode != 0 and message in r.stderr, f"--design-from-file with {extra} accepted: {r.stderr}")
    r = run_cli(root, "generate", "T006", "--design-from-file", str(outside), env=env)
    expect(r.returncode != 0 and "inside" in r.stderr, f"reference outside the unit folder accepted: {r.stderr}")
    expect(len(calls()) == before, "codex was called for a refused design file")
    r = run_cli(root, "generate", "T006", "--design-from-file", "ref_x12.png", env=env)
    expect(r.returncode == 0, f"generate --design-from-file failed: {r.stdout}{r.stderr}")
    e = entries("T006")[0]
    expect(e["reference"] == {"mode": "design-file", "path": "sprites/source/T006/ref_x12.png",
                              "sha256": hashlib.sha256((t006 / "ref_x12.png").read_bytes()).hexdigest()},
           f"design-file record {e['reference']}")
    gen = real["generation"]
    expect(gen["design_file_sentence"] in e["prompt"] and gen["design_reference_sentence"] not in e["prompt"]
           and gen["scale_reference_sentence"] not in e["prompt"], f"design-file prompt {e['prompt']}")
    expect(str((t006 / "ref_x12.png").resolve()) in calls()[-1]["args"], "design file not passed with -i")
    r = run_cli(root, "generate", "T006", "--design-from-file", "candidate_1.png", env=env)
    expect(r.returncode != 0 and "use --design-from" in r.stderr, f"candidate accepted as a design file: {r.stderr}")
    expect("Also restore:" not in e["prompt"] and "rivets" not in e["prompt"], f"design-file prompt {e['prompt']}")
    unit6 = json.loads((t006 / "unit.json").read_text())
    before = len(calls())
    (t006 / "unit.json").write_bytes(json_bytes({**unit6, "restore_details": " "}))
    r = run_cli(root, "generate", "T006", "--design-from-file", "ref_x12.png", env=env)
    expect(r.returncode != 0 and "restore_details" in r.stderr and len(calls()) == before,
           f"blank restore_details accepted: {r.stderr}")
    (t006 / "unit.json").write_bytes(json_bytes({**unit6, "restore_details": "the staff crystal"}))
    r = run_cli(root, "generate", "T006", "--design-from-file", "ref_x12.png", env=env)
    expect(r.returncode == 0, f"generate with restore_details failed: {r.stdout}{r.stderr}")
    lines = entries("T006")[-1]["prompt"].split("\n")
    expect(lines[lines.index(gen["design_file_sentence"]) + 1] == "Also restore: the staff crystal.",
           f"restore line missing after the design-file sentence: {lines}")
    r = run_cli(root, "generate", "T006", "--design-from", "candidate_1.png", env=env)
    expect(r.returncode == 0 and "Also restore:" not in entries("T006")[-1]["prompt"],
           f"restore_details leaked into another reference mode: {r.stderr}")
    r = run_cli(root, "generate", "T006", env=env)
    expect(r.returncode == 0 and "Also restore:" not in entries("T006")[-1]["prompt"],
           f"restore_details leaked into a generate without a reference: {r.stderr}")


def test_same_png() -> None:
    import io
    import struct
    import zlib

    from PIL.PngImagePlugin import PngInfo
    rgba = np.zeros((6, 5, 4), np.uint8)
    rgba[1:4, 1:3] = (0x30, 0x50, 0xa0, 255)

    def encoded(pixels: np.ndarray, mode: str = "RGBA", level: int = 9) -> bytes:
        buf = io.BytesIO()
        Image.fromarray(pixels, mode).save(buf, format="PNG", compress_level=level)
        return buf.getvalue()

    base = png_bytes(rgba)
    other_encoder = encoded(rgba, level=1)
    expect(other_encoder != base and same_png(base, other_encoder), "same pixels, other compression must match")
    moved = rgba.copy()
    moved[0, 0] = (0x30, 0x50, 0xa0, 255)
    expect(not same_png(base, png_bytes(moved)), "a changed pixel must not match")
    expect(not same_png(base, encoded(np.ascontiguousarray(rgba[..., :3]), "RGB")), "another mode must not match")
    expect(not same_png(base, base[:-20]) and not same_png(base, b"x"), "an unreadable file must not match")
    rgb = np.ascontiguousarray(rgba[..., :3])
    buf = io.BytesIO()
    Image.fromarray(rgb, "RGB").save(buf, format="PNG", transparency=(0, 0, 0))
    expect(not same_png(png_bytes(rgb), buf.getvalue()), "a tRNS chunk must not match")
    info = PngInfo()
    info.add_text("note", "x")
    buf = io.BytesIO()
    Image.fromarray(rgb, "RGB").save(buf, format="PNG", pnginfo=info)
    expect(not same_png(png_bytes(rgb), buf.getvalue()), "a text chunk must not match")

    # Pillow cannot write 16-bit RGB but reads it as 8-bit RGB, so build one by hand.
    def chunk(kind: bytes, body: bytes) -> bytes:
        return struct.pack(">I", len(body)) + kind + body + struct.pack(">I", zlib.crc32(kind + body))

    wide = (rgb.astype(">u2") * 257).reshape(rgb.shape[0], -1).tobytes()
    rows = b"".join(b"\0" + wide[y * rgb.shape[1] * 6:(y + 1) * rgb.shape[1] * 6] for y in range(rgb.shape[0]))
    deep = (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", rgb.shape[1], rgb.shape[0], 16, 2, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress(rows)) + chunk(b"IEND", b""))
    with Image.open(io.BytesIO(deep)) as im:
        expect(im.mode == "RGB" and np.array_equal(np.asarray(im), rgb), "hand-made 16-bit PNG did not read as the 8-bit pixels")
    expect(not same_png(png_bytes(rgb), deep), "a 16-bit file must not match")


def test_golden(tmp: Path, real: dict) -> None:
    """Rebuilds the four committed silhouette units and compares them with their committed
    outputs, the one check against outputs made by an earlier version of the tool."""
    root = tmp / "repo"
    ctx = Context(root=root, project=real)
    shutil.copytree(DATA / "golden" / "source", ctx.source_dir)
    shutil.copytree(DATA / "golden" / "output", ctx.output_dir)
    units = [u for u in list_units(ctx) if u.startswith("Silhouettes/")]
    expect(len(units) == 4, f"expected 4 silhouette sources, found {units}")
    problems = verify(ctx, units)
    expect(not problems, "silhouettes do not rebuild: " + "; ".join(problems))
    for path in units:
        motion = json.loads((ctx.output_dir / path / f"{load_unit(ctx, path).id}_motion.json").read_text())
        counts = {a["name"]: len(a["frames"]) for a in motion["actions"]}
        expect(counts == {"idle": 4, "down": 1}, f"{path} actions {counts}")


def test_init_and_config(tmp: Path, real: dict) -> None:
    """init writes the default config and keeps previews out of git; --config is read from
    the current directory and unknown sections in it are ignored."""
    root = tmp / "proj"
    root.mkdir()
    r = run_cli(root, "init")
    expect(r.returncode == 0, f"init failed: {r.stdout}{r.stderr}")
    expect((root / CONFIG_NAME).read_bytes() == (Path(__file__).resolve().parent / "default_config.json").read_bytes(),
           "init must write the default config unchanged")
    preview = Path(real["paths"]["preview"])
    ignore = root / preview.parent / ".gitignore"
    expect(ignore.read_text() == f"{preview.name}/\n", f"init must ignore the preview folder: {ignore}")
    r = run_cli(root, "init")
    expect(r.returncode != 0 and "refusing to overwrite" in r.stderr, "a second init must refuse to overwrite")
    other = copy.deepcopy(real)
    other["ui"] = {"anything": 1}
    other["paths"]["ui_output"] = "elsewhere"
    conf = tmp / "conf" / "other.json"
    conf.parent.mkdir()
    conf.write_bytes(json_bytes(other))
    r = subprocess.run([sys.executable, "-m", __package__, "--root", str(root), "--config", "conf/other.json", "verify"],
                       capture_output=True, text=True, cwd=tmp, timeout=300)
    expect(r.returncode == 0 and "verify: OK" in r.stdout, f"verify with --config from cwd failed: {r.stdout}{r.stderr}")


def default_project() -> dict:
    return json.loads((Path(__file__).resolve().parent / "default_config.json").read_text(encoding="utf-8"))


def run(config: Path | None = None) -> int:
    # The tests read only the project dict from this context; each builds its own temp tree.
    real_ctx = Context(root=Path("."), project=default_project())
    tests = [
        ("convert: synthetic AI image -> expected grid and palette", lambda tmp: test_convert(real_ctx)),
        ("convert: 12.3 and 14.9 px blocks read at the true size, not half", lambda tmp: test_convert_big_blocks(real_ctx)),
        ("convert: random figures at 13 block sizes across the range read at the drawn size, cell for cell",
         lambda tmp: test_convert_block_sizes(real_ctx)),
        ("reduce: outline-priority mode on a fixed grid, reference image blocks", lambda tmp: test_reduce(real_ctx)),
        ("reduce/recheck CLI: ref image and record, no overwrite, height fields only", lambda tmp: test_reduce_recheck_cli(tmp, real_ctx.project)),
        ("convert: alpha channel keys the background", lambda tmp: test_convert_alpha_background(real_ctx)),
        ("convert: merges down to the palette cap", lambda tmp: test_convert_color_cap(real_ctx)),
        ("convert: keeps an isolated cell", lambda tmp: test_convert_keeps_isolated(real_ctx)),
        ("checks: each check catches a broken input", lambda tmp: test_checks(real_ctx)),
        ("assemble: offsets, equipment, draw order, swap, replace", lambda tmp: test_assembly(real_ctx)),
        ("motion: templates, overrides, strike and frame rules, default config", lambda tmp: test_motion_rules(real_ctx)),
        ("fix: edit list counts and edited PNG", lambda tmp: test_fixes(real_ctx)),
        ("fix: fixed_edit.png round trip through --from-png", lambda tmp: test_fix_roundtrip(tmp, real_ctx.project)),
        ("build/verify/palette-swap/parts on a tiny unit", lambda tmp: test_build_verify(tmp, real_ctx.project)),
        ("build: height excludes, feet and airborne, reference unit", lambda tmp: test_build_checks(tmp, real_ctx.project)),
        ("generate: counts, prompt size, reference modes, height status, no overwrite, cap, checkpoint gating", lambda tmp: test_generate(tmp, real_ctx.project)),
        ("generate: batch reservation, concurrency, variants, cap, partial failure, design file", lambda tmp: test_generate_batch(tmp, real_ctx.project)),
        ("verify: same pixels under another PNG compression match; a changed pixel, mode, depth or extra chunk does not", lambda tmp: test_same_png()),
        ("golden: the committed silhouette units rebuild to their committed outputs", lambda tmp: test_golden(tmp, real_ctx.project)),
        ("init and --config: default config, ignored previews, no overwrite, unknown sections, paths from cwd",
         lambda tmp: test_init_and_config(tmp, real_ctx.project)),
    ]
    if config is not None:
        # The project's own config gets the same structural check as the default one.
        own = Context(root=Path("."), project=read_json(config))
        tests.append((f"motion: templates, rigs and kinds of {config}", lambda tmp: test_motion_rules(own)))
    # Imported here: they build on this module's helpers.
    from .selftest_art import TESTS as ART_TESTS
    from .selftest_provenance import TESTS as PROVENANCE_TESTS
    from .selftest_refadopt import TESTS as REFADOPT_TESTS
    from .selftest_rigs import TESTS as RIG_TESTS
    tests += [(name, lambda tmp, fn=fn: fn(real_ctx, tmp))
              for name, fn in RIG_TESTS + ART_TESTS + PROVENANCE_TESTS + REFADOPT_TESTS]
    failures = 0
    for name, fn in tests:
        with tempfile.TemporaryDirectory(prefix="dotomata-selftest-") as tmp:
            try:
                fn(Path(tmp))
                print(f"PASS {name}")
            except Failure as e:
                failures += 1
                print(f"FAIL {name}: {e}")
            except Exception:  # an unexpected crash is a failure too, with its traceback
                failures += 1
                print(f"FAIL {name}: crashed")
                traceback.print_exc()
    print(f"selftest: {len(tests) - failures} passed, {failures} failed")
    return 1 if failures else 0
