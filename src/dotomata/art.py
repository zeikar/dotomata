"""Card art: generation through the shared codex
runner, the user's adoption, the bust crop, the recolour of a palette-swap unit, placeholders,
the build into paths.art_output and verify.

Only the adopted candidate is kept in the source (source/<unit>/art/); the
other candidates stay in the ignored preview folder, and the generation record
keeps every candidate's prompt and sha256."""

from __future__ import annotations

import csv
import datetime
import shutil
from pathlib import Path
from typing import Callable

import numpy as np
from PIL import Image, ImageDraw, ImageFont

from .codexrun import Target, calls_used, check_banned, check_batch, display_path, load_record, run_batch, write_sheet
from .common import (Context, SpriteError, Unit, json_bytes, load_unit, parse_hex, read_json, sha256_file,
                     unit_paths_by_id)
from .generate import require_design
from .imaging import png_bytes, same_png
from .reduce import block_image

STAGE = "base"
GENERATION = "art_generation.json"
ADOPTION = "art.json"
RECOLOR = "recolor.json"
DOT_REF = "dot_ref.png"
PREFIX = "art_candidate"


def art_source(unit: Unit) -> Path:
    return unit.source / "art"


def art_out(ctx: Context, unit: Unit) -> Path:
    return ctx.preview_dir / unit.path / "art"


def art_output_dir(ctx: Context, unit: Unit) -> Path:
    return ctx.root / ctx.project["paths"]["art_output"] / unit.id


def output_names(unit_id: str, stage: str) -> tuple[str, str]:
    return f"{unit_id}_{stage}_full.png", f"{unit_id}_{stage}_bust.png"


def art_target(ctx: Context, unit: Unit) -> Target:
    a = ctx.project["art"]
    return Target(label=f"card art of '{unit.id}'", header={"unit": unit.id}, record=art_source(unit) / GENERATION,
                  out_dir=art_out(ctx, unit), prefix=PREFIX, cap=a["cap_per_unit"], batch_max=a["batch_max"],
                  keep_dirs=[art_source(unit)])


def is_recolor(unit: Unit) -> bool:
    return (art_source(unit) / RECOLOR).is_file()


def _refuse_recolor(unit: Unit) -> None:
    # A palette-swap unit is recoloured from its base's art, never drawn on its own.
    if is_recolor(unit) or unit.data.get("palette_swap"):
        raise SpriteError(f"unit '{unit.id}' gets its card art by recolouring its base unit's art "
                          f"({RECOLOR}); it is never generated, adopted or given a {DOT_REF}")


# ---------------------------------------------------------------- dot reference


def has_built_sheet(ctx: Context, unit: Unit) -> bool:
    return (ctx.output_dir / unit.path / f"{unit.id}_motion.json").is_file()


def dot_reference_bytes(ctx: Context, unit: Unit) -> bytes:
    """Idle frame 0 of the built sheet, cropped to its opaque pixels and enlarged
    in ref_block squares on the reference magenta, like the size-pass reference."""
    folder = ctx.output_dir / unit.path
    motion_path = folder / f"{unit.id}_motion.json"
    if not motion_path.is_file():
        raise SpriteError(f"unit '{unit.id}' has no built sheet ({motion_path}); build its sprite first")
    motion = read_json(motion_path)
    idle = [a for a in motion["actions"] if a["name"] == "idle"]
    if not idle or not idle[0]["frames"]:
        raise SpriteError(f"unit '{unit.id}': {motion_path.name} has no idle frame")
    frame = idle[0]["frames"][0]
    cw, ch = motion["cell"]["width"], motion["cell"]["height"]
    with Image.open(folder / motion["sheet"]) as im:
        sheet = np.array(im.convert("RGBA"))
    cell = sheet[frame["y"]:frame["y"] + ch, frame["x"]:frame["x"] + cw]
    ys, xs = np.nonzero(cell[..., 3] == 255)
    if not len(ys):
        raise SpriteError(f"unit '{unit.id}': idle frame 0 is empty")
    return png_bytes(block_image(cell[ys.min():ys.max() + 1, xs.min():xs.max() + 1], ctx.project["reduce"]))


def write_dot_reference(ctx: Context, unit: Unit, force: bool) -> Path:
    _refuse_recolor(unit)
    path = art_source(unit) / DOT_REF
    if path.exists() and not force:
        raise SpriteError(f"refusing to overwrite {path} (pass --force to replace it)")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(dot_reference_bytes(ctx, unit))
    return path


def refresh_dot_reference(ctx: Context, unit: Unit) -> Path | None:
    """After a sprite build: rewrites an existing dot_ref.png from the new sheet (the
    build makes it), so verify and art generate keep
    matching. Returns the path when the pixels changed."""
    path = art_source(unit) / DOT_REF
    if not path.is_file():
        return None
    data = dot_reference_bytes(ctx, unit)
    if same_png(path.read_bytes(), data):
        return None
    path.write_bytes(data)
    return path


def _current_dot_reference(ctx: Context, unit: Unit) -> Path | None:
    """The dot reference a unit with a built sprite must send; None for a unit without one."""
    if not has_built_sheet(ctx, unit):
        return None
    path = art_source(unit) / DOT_REF
    if not path.is_file():
        raise SpriteError(f"unit '{unit.id}' has a sprite but no {DOT_REF}; run: art dot-ref {unit.path}")
    if not same_png(path.read_bytes(), dot_reference_bytes(ctx, unit)):
        raise SpriteError(f"unit '{unit.id}': {DOT_REF} is older than the built sheet; "
                          f"run: art dot-ref {unit.path} --force")
    return path


def style_reference(ctx: Context) -> Path | None:
    """The picture every card art run gets as its style reference (art.style_reference);
    None when the config sets none, and then no style image or sentence is sent."""
    configured = ctx.project["art"].get("style_reference")
    if not configured:
        return None
    path = ctx.root / configured
    if not path.is_file():
        raise SpriteError(f"card art: the style reference {path} is missing (art.style_reference)")
    return path


# ---------------------------------------------------------------- generate


def art_prompt(ctx: Context, unit: Unit, notes: list[str], dot_reference: bool, variant: str | None = None) -> str:
    gen, a = ctx.project["generation"], ctx.project["art"]
    design = require_design(unit)
    lines = [gen["tool_instruction"],
             "Image: " + " ".join([a["common_prompt"], a["composition_sentence"], a["design_rule_sentence"], design])]
    if notes:
        lines.append("Changes from the previous candidate: " + " ".join(notes))
    if a.get("style_reference"):
        lines.append(a["style_reference_sentence"])
    if dot_reference:
        lines.append(a["dot_reference_sentence"])
    if variant:
        lines.append("For this candidate: " + variant)
    prompt = "\n".join(lines)
    check_banned(ctx, f"card art of '{unit.id}'", prompt)
    return prompt


def generate_art(ctx: Context, unit: Unit, notes: list[str], batch: int = 1, variants: list[str] | None = None) -> dict:
    """One codex run per candidate, all at once. Every run sends the style reference
    when the config sets one; a unit with a built sprite also sends its dot reference."""
    _refuse_recolor(unit)
    require_design(unit)
    variants = list(variants or [])
    target = art_target(ctx, unit)
    used = check_batch(target, batch, variants)
    reference = _current_dot_reference(ctx, unit)
    style = style_reference(ctx)
    run_variants = [variants[i] if i < len(variants) else None for i in range(batch)]
    prompts = [art_prompt(ctx, unit, notes, reference is not None, v) for v in run_variants]
    ref_record = ({"mode": "dot", "path": display_path(ctx, reference), "sha256": sha256_file(reference)}
                  if reference is not None else None)
    references = ([style] if style is not None else []) + ([reference] if reference is not None else [])
    style_record = {"path": display_path(ctx, style), "sha256": sha256_file(style)} if style is not None else None
    result = run_batch(ctx, target, prompts, run_variants, references, ref_record, {"style_reference": style_record})
    sheet = write_comparison_sheet(ctx, target, [r["entry"] for r in result["runs"]],
                                   art_source(unit) / f"art_batch_{result['first']}-{result['last']}.jpg")
    return {"runs": result["runs"], "sheet": str(sheet), "cap": target.cap,
            "used": used + sum(r["entry"]["image_generation_calls"] for r in result["runs"])}


def _size_label(folder: Path) -> Callable[[dict], str]:
    def label(e: dict) -> str:
        if e["candidate"]:
            try:
                with Image.open(folder / e["candidate"]) as im:
                    return f"{im.width}x{im.height}" + (f" - {e['variant']}" if e["variant"] else "")
            except OSError:
                return "unreadable image"
        return e["error"] or "no image"
    return label


def write_comparison_sheet(ctx: Context, target: Target, entries: list[dict], out: Path) -> Path:
    """The candidates the user chose from, as a small JPEG kept in the source."""
    s = ctx.project["comparison_sheet"]
    return write_sheet(ctx, target.out_dir, entries, out, s["panel_height"], _size_label(target.out_dir),
                       long_side=s["long_side"], jpeg_quality=s["jpeg_quality"])


# ---------------------------------------------------------------- adoption


def load_image_rgb(path: Path, what: str) -> np.ndarray:
    try:
        with Image.open(path) as im:
            im.load()
            rgba = np.array(im.convert("RGBA"))
    except OSError as e:
        raise SpriteError(f"{what}: cannot read {path.name}: {e}") from e
    if (rgba[..., 3] < 255).any():
        raise SpriteError(f"{what}: {path.name} has transparent pixels; card art and backgrounds are opaque")
    return rgba[..., :3].copy()


def adopt_candidate(target: Target, source: Path, name: str, check: Callable[[Path], None]) -> dict:
    """Checks a recorded candidate against its record, copies it into the source
    and returns the start of an adoption entry."""
    entries = [g for g in load_record(target)["generations"] if g.get("candidate") == name]
    if not entries:
        raise SpriteError(f"{target.label}: {name} is not in {target.record.name}")
    if entries[-1].get("invalid"):
        raise SpriteError(f"{target.label}: {name} is marked invalid in {target.record.name}: {entries[-1]['invalid']}")
    found = [d / name for d in (source, target.out_dir) if (d / name).is_file()]
    if not found:
        raise SpriteError(f"{target.label}: {name} is in neither {source} nor {target.out_dir}")
    digest = sha256_file(found[0])
    if digest != entries[-1]["output_sha256"]:
        raise SpriteError(f"{target.label}: {found[0]} does not match the sha256 recorded for {name}")
    check(found[0])
    if found[0].parent != source:
        source.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(found[0], source / name)
    return {"candidate": name, "sha256": digest}


def retire_candidate(target: Target, source: Path, name: str, still_used: set[str]) -> None:
    """An earlier adopted candidate leaves the source; its record stays. It goes
    back to the preview folder so it can still be looked at on this machine."""
    if name in still_used or not (source / name).is_file():
        return
    target.out_dir.mkdir(parents=True, exist_ok=True)
    if (target.out_dir / name).exists():
        (source / name).unlink()
    else:
        shutil.move(str(source / name), str(target.out_dir / name))


def load_adoption(unit: Unit) -> dict:
    path = art_source(unit) / ADOPTION
    return read_json(path) if path.is_file() else {"unit": unit.id, "stages": {}}


def current_adoption(data: dict, stage: str, what: str) -> dict:
    adoptions = data.get("stages", {}).get(stage, {}).get("adoptions") or []
    if not adoptions:
        raise SpriteError(f"{what}: stage '{stage}' has no adoption in {ADOPTION}")
    return adoptions[-1]


def check_art_image(ctx: Context, what: str) -> Callable[[Path], None]:
    def check(path: Path) -> None:
        rgb = load_image_rgb(path, what)
        h, w = rgb.shape[:2]
        if w * 3 != h * 2:
            raise SpriteError(f"{what}: {path.name} is {w}x{h}, not a 2:3 portrait")
    return check


def check_bust(rect, width: int, height: int, min_side: int, what: str) -> dict:
    """The bust crop: whole pixels, inside the image, square, at least min_side."""
    keys = ("x", "y", "width", "height")
    if not isinstance(rect, dict) or sorted(rect) != sorted(keys):
        raise SpriteError(f"{what}: bust must be {{x, y, width, height}} (got {rect})")
    if not all(isinstance(rect[k], int) and not isinstance(rect[k], bool) for k in keys):
        raise SpriteError(f"{what}: bust values must be whole pixels (got {rect})")
    x, y, w, h = (rect[k] for k in keys)
    if x < 0 or y < 0 or x + w > width or y + h > height:
        raise SpriteError(f"{what}: bust {rect} is outside the {width}x{height} image")
    if w != h:
        raise SpriteError(f"{what}: bust {rect} is not square")
    if w < min_side:
        raise SpriteError(f"{what}: bust side {w} is smaller than {min_side} px (the bust is never enlarged)")
    return {k: rect[k] for k in keys}


def rect_from(values: list[int] | None) -> dict | None:
    return None if values is None else dict(zip(("x", "y", "width", "height"), values))


def adopt_art(ctx: Context, unit: Unit, name: str, note: str, bust: dict | None, stage: str = STAGE) -> dict:
    _refuse_recolor(unit)
    if not note.strip():
        raise SpriteError("an adoption needs a note (the user's reason)")
    target, source = art_target(ctx, unit), art_source(unit)
    what = f"card art of '{unit.id}'"
    checked_bust = [None]

    # The bust is checked with the picture, before adopt_candidate copies it, so a bad
    # rectangle leaves no unadopted candidate in the source.
    def check(path: Path) -> None:
        check_art_image(ctx, what)(path)
        if bust is not None:
            rgb = load_image_rgb(path, what)
            checked_bust[0] = check_bust(bust, rgb.shape[1], rgb.shape[0], ctx.project["art"]["bust_min_side"], what)

    entry = adopt_candidate(target, source, name, check)
    bust = checked_bust[0]
    data = load_adoption(unit)
    adoptions = data.setdefault("stages", {}).setdefault(stage, {}).setdefault("adoptions", [])
    previous = adoptions[-1]["candidate"] if adoptions else None
    adoptions.append({**entry, "note": note, "date": datetime.date.today().isoformat(), "bust": bust})
    (source / ADOPTION).write_bytes(json_bytes(data))
    if previous and previous != name:
        in_use = {current_adoption(data, s, what)["candidate"] for s in data["stages"]}
        retire_candidate(target, source, previous, in_use)
    return adoptions[-1]


def set_bust(ctx: Context, unit: Unit, bust: dict, stage: str = STAGE) -> dict:
    """Sets the bust crop of the current adoption (a later adoption needs its own)."""
    what = f"card art of '{unit.id}'"
    data = load_adoption(unit)
    current = current_adoption(data, stage, what)
    rgb = load_image_rgb(art_source(unit) / current["candidate"], what)
    current["bust"] = check_bust(bust, rgb.shape[1], rgb.shape[0], ctx.project["art"]["bust_min_side"], what)
    (art_source(unit) / ADOPTION).write_bytes(json_bytes(data))
    return current


def art_status(ctx: Context, unit: Unit) -> dict:
    calls, candidates = calls_used(art_target(ctx, unit))
    data = load_adoption(unit)
    stages = {s: v["adoptions"][-1] for s, v in sorted(data.get("stages", {}).items()) if v.get("adoptions")}
    return {"unit": unit.id, "image_generation_calls": calls, "cap": ctx.project["art"]["cap_per_unit"],
            # recolor: never generated (a palette swap also before its rule exists); recolor_rule: recolor.json is written.
            "candidates": candidates, "recolor": is_recolor(unit) or bool(unit.data.get("palette_swap")),
            "recolor_rule": is_recolor(unit), "dot_reference": has_built_sheet(ctx, unit),
            "adopted": stages}


# ---------------------------------------------------------------- build


def bust_image(rgb: np.ndarray, rect: dict, size: int) -> np.ndarray:
    """The square crop scaled down to size x size (never up: check_bust keeps the side >= size)."""
    x, y, side = rect["x"], rect["y"], rect["width"]
    crop = Image.fromarray(rgb[y:y + side, x:x + side], "RGB")
    if side != size:
        crop = crop.resize((size, size), Image.Resampling.LANCZOS)
    return np.array(crop)


def recolor(rgb: np.ndarray, rule: dict, what: str) -> np.ndarray:
    """Turns the hues in rule["hue_from"] (degrees, inclusive) so that the range's
    middle lands on rule["hue_to"], for pixels at least rule["min_saturation"]
    percent saturated; everything else keeps its exact colour. The optional
    rule["saturation"] and rule["value"] (percent, 100 = unchanged) scale the
    turned pixels, capped at full."""
    try:
        lo, hi = rule["hue_from"]
        to, min_s = rule["hue_to"], rule["min_saturation"]
        sat, val = rule.get("saturation", 100), rule.get("value", 100)
    except (KeyError, TypeError, ValueError) as e:
        raise SpriteError(f"{what}: {RECOLOR} needs hue_from [lo, hi], hue_to and min_saturation") from e
    if not all(isinstance(v, int) and not isinstance(v, bool) for v in (lo, hi, to, min_s, sat, val)):
        raise SpriteError(f"{what}: {RECOLOR} values must be whole numbers")
    if not (0 <= lo < hi <= 360 and 0 <= to < 360 and 0 <= min_s <= 100):
        raise SpriteError(f"{what}: {RECOLOR} needs 0 <= lo < hi <= 360, 0 <= hue_to < 360, 0 <= min_saturation <= 100")
    if not (0 < sat and 0 < val):
        raise SpriteError(f"{what}: {RECOLOR} needs saturation > 0 and value > 0 (percent, 100 = unchanged)")
    # Pillow's HSV keeps hue, saturation and value in 0..255.
    hsv = np.array(Image.fromarray(rgb, "RGB").convert("HSV")).astype(np.int64)
    hue = (hsv[..., 0] * 360 + 127) // 255
    mask = (hue >= lo) & (hue <= hi) & (hsv[..., 1] * 100 >= min_s * 255)
    turned = (hue + to - (lo + hi) // 2) % 360
    shifted = hsv.copy()
    shifted[..., 0] = np.where(mask, (turned * 255 + 180) // 360 % 256, hsv[..., 0])
    # At 100 the rounded scale gives back the same channel, so a rule without the keys is byte-identical.
    shifted[..., 1] = np.where(mask, np.minimum(255, (hsv[..., 1] * sat + 50) // 100), hsv[..., 1])
    shifted[..., 2] = np.where(mask, np.minimum(255, (hsv[..., 2] * val + 50) // 100), hsv[..., 2])
    back = np.array(Image.fromarray(shifted.astype(np.uint8), "HSV").convert("RGB"))
    return np.where(mask[..., None], back, rgb).astype(np.uint8)


def _stage_outputs(ctx: Context, unit_id: str, stage: str, full: np.ndarray, bust: dict, what: str) -> dict[str, bytes]:
    a = ctx.project["art"]
    rect = check_bust(bust, full.shape[1], full.shape[0], a["bust_min_side"], what)
    full_name, bust_name = output_names(unit_id, stage)
    # The full picture is the candidate's pixels as they are (no crop, no resampling),
    # written without the generator's metadata chunks.
    return {full_name: png_bytes(full), bust_name: png_bytes(bust_image(full, rect, a["bust_size"]))}


def adopted_outputs(ctx: Context, unit: Unit) -> dict[str, bytes]:
    what = f"card art of '{unit.id}'"
    data = load_adoption(unit)
    if not data.get("stages"):
        raise SpriteError(f"{what}: {ADOPTION} has no stage")
    files = {}
    for stage in sorted(data["stages"]):
        current = current_adoption(data, stage, what)
        path = art_source(unit) / current["candidate"]
        if not path.is_file():
            raise SpriteError(f"{what}: the adopted {current['candidate']} is not in {art_source(unit)}")
        if sha256_file(path) != current["sha256"]:
            raise SpriteError(f"{what}: {current['candidate']} does not match the sha256 in {ADOPTION}")
        if current.get("bust") is None:
            raise SpriteError(f"{what}: stage '{stage}' has no bust crop; run: art bust {unit.path} --rect X Y W H")
        files.update(_stage_outputs(ctx, unit.id, stage, load_image_rgb(path, what), current["bust"], what))
    return files


def recolor_outputs(ctx: Context, unit: Unit) -> dict[str, bytes]:
    """A recolour unit: the base unit's adopted art with one hue range turned, same bust crop."""
    what = f"card art of '{unit.id}'"
    rule = read_json(art_source(unit) / RECOLOR)
    base = load_unit(ctx, rule.get("base") or "")
    if is_recolor(base):
        raise SpriteError(f"{what}: the recolour base '{base.id}' is itself a recolour")
    data = load_adoption(base)
    current = current_adoption(data, STAGE, f"{what} (base '{base.id}')")
    path = art_source(base) / current["candidate"]
    if sha256_file(path) != current["sha256"]:
        raise SpriteError(f"{what}: the base's {current['candidate']} does not match its {ADOPTION}")
    if current.get("bust") is None:
        raise SpriteError(f"{what}: the base '{base.id}' has no bust crop yet")
    full = recolor(load_image_rgb(path, what), rule, what)
    return _stage_outputs(ctx, unit.id, STAGE, full, current["bust"], what)


# ---------------------------------------------------------------- placeholders


def english_name(ctx: Context, key: str | None, what: str) -> str:
    """unit.json `name`: a key into the paths.strings CSV (its `en` column) when the
    config sets one, else the English name itself."""
    if not key:
        raise SpriteError(f"{what}: unit.json has no 'name'")
    strings = ctx.project["paths"].get("strings")
    if not strings:
        return key
    path = ctx.root / strings
    try:
        with open(path, encoding="utf-8-sig", newline="") as f:
            for row in csv.DictReader(f):
                if row.get("key") == key:
                    return row["en"]
    except OSError as e:
        raise SpriteError(f"{what}: cannot read the string table {path}: {e}") from e
    raise SpriteError(f"{what}: string key '{key}' is not in {path.name}")


def main_color(unit: Unit, what: str) -> tuple[int, int, int]:
    colors = unit.data.get("colors")
    if not colors:
        raise SpriteError(f"{what}: unit.json has no 'colors'; the placeholder uses the first one")
    return parse_hex(colors[0], f"{what} colors[0]")


def placeholder_canvas(size: tuple[int, int], color: tuple[int, int, int], lines: list[tuple[str, int, int]]) -> np.ndarray:
    """A flat colour with centred text lines (text, font size, centre y), drawn by code, never by AI."""
    img = Image.new("RGB", size, color)
    draw = ImageDraw.Draw(img)
    ink = (24, 22, 28) if sum(c * w for c, w in zip(color, (299, 587, 114))) > 140_000 else (246, 242, 232)
    for text, font_size, y in lines:
        font = ImageFont.load_default(size=font_size)
        draw.text((size[0] // 2, y), text, fill=ink, font=font, anchor="mm")
    return np.array(img)


def wrap(text: str, font_size: int, width: int) -> list[str]:
    font = ImageFont.load_default(size=font_size)
    lines, line = [], ""
    for word in text.split():
        trial = f"{line} {word}".strip()
        if line and font.getlength(trial) > width:
            lines.append(line)
            line = word
        else:
            line = trial
    return lines + ([line] if line else [])


def placeholder_outputs(ctx: Context, unit: Unit) -> dict[str, bytes]:
    """A 2:3 flat-colour card with the unit id and English name, until an adoption replaces it."""
    what = f"card art placeholder of '{unit.id}'"
    a = ctx.project["art"]
    w, h = a["size"]
    name_lines = wrap(english_name(ctx, unit.data.get("name"), what), 88, w - 128)
    lines = [(unit.id, 240, 330)] + [(t, 88, 560 + 100 * i) for i, t in enumerate(name_lines)] + [("PLACEHOLDER", 56, 1380)]
    full = placeholder_canvas((w, h), main_color(unit, what), lines)
    return _stage_outputs(ctx, unit.id, STAGE, full, a["placeholder_bust"], what)


# ---------------------------------------------------------------- build and verify


def art_outputs(ctx: Context, unit: Unit) -> tuple[dict[str, bytes], str]:
    """(files, how): from the recolour rule, the adoption, or a placeholder."""
    if is_recolor(unit):
        return recolor_outputs(ctx, unit), "recolour"
    if (art_source(unit) / ADOPTION).is_file():
        return adopted_outputs(ctx, unit), "adopted"
    return placeholder_outputs(ctx, unit), "placeholder (no art.json yet)"


def build_art(ctx: Context, unit: Unit, force: bool) -> tuple[list[Path], str]:
    files, how = art_outputs(ctx, unit)
    out = art_output_dir(ctx, unit)
    existing = [n for n in files if (out / n).exists()]
    if existing and not force:
        raise SpriteError(f"refusing to overwrite {existing} in {out} (pass --force to replace them)")
    out.mkdir(parents=True, exist_ok=True)
    written = []
    for name, data in files.items():
        (out / name).write_bytes(data)
        written.append(out / name)
    return written, how


def art_units(ctx: Context) -> list[str]:
    """Unit paths that have card art: committed outputs or an art source."""
    root = ctx.root / ctx.project["paths"]["art_output"]
    built = {p.name for p in root.iterdir() if p.is_dir()} if root.is_dir() else set()
    # Card art folders are named by unit id, which may differ from the unit's source path.
    paths, ids = [], set()
    for uid, path in unit_paths_by_id(ctx).items():
        source = ctx.source_dir / path / "art"
        if uid in built or (source / ADOPTION).is_file() or (source / RECOLOR).is_file():
            paths.append(path)
            ids.add(uid)
    unknown = built - ids
    if unknown:
        raise SpriteError(f"card art folders without a unit.json: {sorted(unknown)}")
    return paths


def verify_art(ctx: Context, unit_paths: list[str] | None) -> list[str]:
    """Rebuilds card art (and dot references) in memory and compares pixels with the committed files."""
    problems = []
    for path in unit_paths if unit_paths is not None else art_units(ctx):
        unit = load_unit(ctx, path)
        before = len(problems)
        dot = art_source(unit) / DOT_REF
        if dot.is_file():
            try:
                if not same_png(dot.read_bytes(), dot_reference_bytes(ctx, unit)):
                    problems.append(f"{path}: {DOT_REF} differs from the rebuild; run: art dot-ref {path} --force")
            except SpriteError as e:
                problems.append(f"{path}: {DOT_REF} rebuild failed: {e}")
        try:
            files, how = art_outputs(ctx, unit)
        except SpriteError as e:
            problems.append(f"{path}: card art rebuild failed: {e}")
            continue
        out = art_output_dir(ctx, unit)
        names = sorted(p.name for p in out.glob("*.png")) if out.is_dir() else []
        if sorted(files) != names:
            problems.append(f"{path}: card art file set differs: committed {names}, rebuilt {sorted(files)}")
        for name in sorted(set(files) & set(names)):
            if not same_png((out / name).read_bytes(), files[name]):
                problems.append(f"{path}: {name} differs from the rebuild ({how})")
        if len(problems) == before:
            print(f"ok {path}: card art {len(files)} files match ({how})")
    return problems
