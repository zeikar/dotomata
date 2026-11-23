"""Deterministic PNG/GIF encoding, and the pixel comparison verify uses for PNGs."""

from __future__ import annotations

import io

import numpy as np
from PIL import Image


def png_bytes(pixels: np.ndarray) -> bytes:
    """RGBA, or RGB for an opaque image (the design reference on magenta)."""
    buf = io.BytesIO()
    # Fixed settings and no text/time chunks: same pixels give the same bytes.
    mode = "RGBA" if pixels.shape[2] == 4 else "RGB"
    Image.fromarray(pixels, mode).save(buf, format="PNG", optimize=False, compress_level=9)
    return buf.getvalue()


def same_png(a: bytes, b: bytes) -> bool:
    """Equal size, mode and pixels. Pillow's zlib-ng compresses differently per CPU
    (macOS arm64 and Linux x86_64 give different bytes for the same pixels), so
    verify compares decoded images rather than file bytes."""
    if a == b:
        return True
    try:
        ia, ib = _checked_png(a), _checked_png(b)
    except (OSError, SyntaxError, ValueError):  # Pillow raises SyntaxError for a bad chunk CRC
        return False
    return ia.mode == ib.mode and ia.size == ib.size and np.array_equal(np.asarray(ia), np.asarray(ib))


def _checked_png(data: bytes) -> Image.Image:
    # verify() reads every chunk and its CRC, so a truncated or damaged file that
    # would still decode is not taken for its pixels.
    with Image.open(io.BytesIO(data)) as im:
        if im.format != "PNG":
            raise ValueError(f"not a PNG: {im.format}")
        im.verify()
    im = Image.open(io.BytesIO(data))
    im.load()
    # Decoding drops what png_bytes never writes: other bit depths (16-bit reads as
    # 8-bit) and extra chunks such as tRNS, which would make a colour transparent.
    if data[24] != 8 or im.info:
        raise ValueError(f"not a plain 8-bit PNG: depth {data[24]}, chunks {sorted(im.info)}")
    return im


def zoom(rgba: np.ndarray, factor: int) -> np.ndarray:
    return np.repeat(np.repeat(rgba, factor, axis=0), factor, axis=1)


def flatten(rgba: np.ndarray, background) -> np.ndarray:
    """Puts transparent pixels on an opaque background for previews."""
    out = np.empty(rgba.shape[:2] + (3,), dtype=np.uint8)
    out[:] = background
    mask = rgba[..., 3] == 255
    out[mask] = rgba[mask, 0:3]
    return out


def gif_bytes(frames: list[np.ndarray], durations_ms: list[int], background) -> bytes:
    """Encodes flattened RGB frames with one shared, explicit palette."""
    def keys(a: np.ndarray) -> np.ndarray:
        a = a.astype(np.int32)
        return (a[..., 0] << 16) | (a[..., 1] << 8) | a[..., 2]

    bg_key = int(keys(np.array(background, dtype=np.uint8)))
    found = np.unique(np.concatenate([keys(f).ravel() for f in frames]))
    # Background first, then the rest in sorted order: a fixed palette layout.
    ordered = np.concatenate([[bg_key], found[found != bg_key]]).astype(np.int32)
    if len(ordered) > 256:
        raise ValueError(f"preview GIF needs {len(ordered)} colors, more than 256")
    order = np.argsort(ordered, kind="stable")
    sorted_keys = ordered[order]
    flat_palette = []
    for k in ordered.tolist():
        flat_palette += [(k >> 16) & 255, (k >> 8) & 255, k & 255]
    flat_palette += [0] * (3 * (256 - len(ordered)))
    images = []
    for frame in frames:
        idx = order[np.searchsorted(sorted_keys, keys(frame))].astype(np.uint8)
        im = Image.fromarray(idx, "P")
        im.putpalette(flat_palette)
        images.append(im)
    buf = io.BytesIO()
    images[0].save(buf, format="GIF", save_all=True, append_images=images[1:], duration=durations_ms,
                   loop=0, disposal=1, optimize=False)
    return buf.getvalue()


def load_rgba(path) -> tuple[np.ndarray, np.ndarray | None]:
    """Returns (rgb uint8, alpha uint8 or None)."""
    with Image.open(path) as im:
        im.load()
        has_alpha = im.mode in ("RGBA", "LA") or (im.mode == "P" and "transparency" in im.info)
        rgba = np.array(im.convert("RGBA"))
    return rgba[..., 0:3].copy(), (rgba[..., 3].copy() if has_alpha else None)
