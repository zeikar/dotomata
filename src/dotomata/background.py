"""Backgrounds: generation through the shared codex
runner, adoption like card art, a cover-fit build into paths.bg_output,
placeholders and verify. Sources are in source/backgrounds/<id>/."""

from __future__ import annotations

import datetime
from dataclasses import dataclass
from pathlib import Path

import numpy as np
from PIL import Image

from .art import adopt_candidate, load_image_rgb, placeholder_canvas, retire_candidate, write_comparison_sheet
from .codexrun import Target, calls_used, check_banned, check_batch, run_batch
from .common import Context, SpriteError, json_bytes, parse_hex, read_json, sha256_file
from .imaging import png_bytes, same_png

FOLDER = "backgrounds"
DEFINITION = "background.json"
GENERATION = "bg_generation.json"
ADOPTION = "bg.json"
PREFIX = "bg_candidate"


@dataclass
class Background:
    id: str
    data: dict      # background.json, written by people
    source: Path    # source/backgrounds/<id>/


def load_background(ctx: Context, bg_id: str) -> Background:
    source = ctx.source_dir / FOLDER / bg_id
    path = source / DEFINITION
    if not path.is_file():
        raise SpriteError(f"background '{bg_id}' has no {DEFINITION} at {path}")
    data = read_json(path)
    if data.get("id") != bg_id:
        raise SpriteError(f"background '{bg_id}': {DEFINITION} has id '{data.get('id')}'")
    layout = data.get("layout")
    if layout not in ctx.project["bg"]["layouts"]:
        raise SpriteError(f"background '{bg_id}' has unknown layout '{layout}' "
                          f"(expected one of {sorted(ctx.project['bg']['layouts'])})")
    return Background(id=bg_id, data=data, source=source)


def list_backgrounds(ctx: Context) -> list[str]:
    root = ctx.source_dir / FOLDER
    return sorted(p.parent.name for p in root.glob(f"*/{DEFINITION}")) if root.is_dir() else []


def bg_target(ctx: Context, bg: Background) -> Target:
    b = ctx.project["bg"]
    return Target(label=f"background '{bg.id}'", header={"background": bg.id}, record=bg.source / GENERATION,
                  out_dir=ctx.preview_dir / FOLDER / bg.id, prefix=PREFIX, cap=b["cap_per_background"],
                  batch_max=b["batch_max"], keep_dirs=[bg.source])


def bg_prompt(ctx: Context, bg: Background, notes: list[str], variant: str | None = None) -> str:
    gen, b = ctx.project["generation"], ctx.project["bg"]
    design = bg.data.get("design")
    if not isinstance(design, str) or not design.strip():
        raise SpriteError(f"background '{bg.id}': {DEFINITION} has no 'design' (the scene description)")
    scene = b["layouts"][bg.data["layout"]].format(design=design.strip().rstrip("."))
    lines = [gen["tool_instruction"], "Image: " + scene + " " + b["common_prompt"]]
    if notes:
        lines.append("Changes from the previous candidate: " + " ".join(notes))
    if variant:
        lines.append("For this candidate: " + variant)
    prompt = "\n".join(lines)
    check_banned(ctx, f"background '{bg.id}'", prompt)
    return prompt


def generate_bg(ctx: Context, bg: Background, notes: list[str], batch: int = 1, variants: list[str] | None = None) -> dict:
    variants = list(variants or [])
    target = bg_target(ctx, bg)
    used = check_batch(target, batch, variants)
    run_variants = [variants[i] if i < len(variants) else None for i in range(batch)]
    prompts = [bg_prompt(ctx, bg, notes, v) for v in run_variants]
    result = run_batch(ctx, target, prompts, run_variants, [], None)
    sheet = write_comparison_sheet(ctx, target, [r["entry"] for r in result["runs"]],
                                   bg.source / f"bg_batch_{result['first']}-{result['last']}.jpg")
    return {"runs": result["runs"], "sheet": str(sheet), "cap": target.cap,
            "used": used + sum(r["entry"]["image_generation_calls"] for r in result["runs"])}


def load_adoption(bg: Background) -> dict:
    path = bg.source / ADOPTION
    return read_json(path) if path.is_file() else {"background": bg.id, "adoptions": []}


def adopt_bg(ctx: Context, bg: Background, name: str, note: str) -> dict:
    if not note.strip():
        raise SpriteError("an adoption needs a note (the user's reason)")
    target = bg_target(ctx, bg)
    what = f"background '{bg.id}'"
    entry = adopt_candidate(target, bg.source, name, lambda p: load_image_rgb(p, what))
    data = load_adoption(bg)
    previous = data["adoptions"][-1]["candidate"] if data["adoptions"] else None
    data["adoptions"].append({**entry, "note": note, "date": datetime.date.today().isoformat()})
    (bg.source / ADOPTION).write_bytes(json_bytes(data))
    if previous and previous != name:
        retire_candidate(target, bg.source, previous, {name})
    return data["adoptions"][-1]


def bg_status(ctx: Context, bg: Background) -> dict:
    calls, candidates = calls_used(bg_target(ctx, bg))
    adoptions = load_adoption(bg)["adoptions"]
    return {"background": bg.id, "image_generation_calls": calls, "cap": ctx.project["bg"]["cap_per_background"],
            "candidates": candidates, "adopted": adoptions[-1] if adoptions else None}


def cover_fit(rgb: np.ndarray, size: tuple[int, int]) -> np.ndarray:
    """Scales the picture to cover size exactly (rounding up) and crops the middle."""
    width, height = size
    h, w = rgb.shape[:2]
    if w * height >= h * width:
        nw, nh = -(-w * height // h), height
    else:
        nw, nh = width, -(-h * width // w)
    big = Image.fromarray(rgb, "RGB").resize((nw, nh), Image.Resampling.LANCZOS)
    left, top = (nw - width) // 2, (nh - height) // 2
    return np.array(big.crop((left, top, left + width, top + height)))


def bg_outputs(ctx: Context, bg: Background) -> tuple[dict[str, bytes], str]:
    """({file: bytes}, how): from the adoption, or a placeholder until one exists."""
    what = f"background '{bg.id}'"
    size = tuple(ctx.project["bg"]["size"])
    adoptions = load_adoption(bg)["adoptions"]
    if not adoptions:
        colors = bg.data.get("colors")
        if not colors:
            raise SpriteError(f"{what}: {DEFINITION} has no 'colors'; the placeholder uses the first one")
        color = parse_hex(colors[0], f"{what} colors[0]")
        canvas = placeholder_canvas(size, color, [(bg.id, 160, size[1] // 2 - 60), ("PLACEHOLDER", 64, size[1] // 2 + 100)])
        return {f"{bg.id}.png": png_bytes(canvas)}, "placeholder (no bg.json yet)"
    current = adoptions[-1]
    path = bg.source / current["candidate"]
    if not path.is_file():
        raise SpriteError(f"{what}: the adopted {current['candidate']} is not in {bg.source}")
    if sha256_file(path) != current["sha256"]:
        raise SpriteError(f"{what}: {current['candidate']} does not match the sha256 in {ADOPTION}")
    return {f"{bg.id}.png": png_bytes(cover_fit(load_image_rgb(path, what), size))}, "adopted"


def bg_output_dir(ctx: Context) -> Path:
    return ctx.root / ctx.project["paths"]["bg_output"]


def build_bg(ctx: Context, bg: Background, force: bool) -> tuple[list[Path], str]:
    files, how = bg_outputs(ctx, bg)
    out = bg_output_dir(ctx)
    existing = [n for n in files if (out / n).exists()]
    if existing and not force:
        raise SpriteError(f"refusing to overwrite {existing} in {out} (pass --force to replace them)")
    out.mkdir(parents=True, exist_ok=True)
    for name, data in files.items():
        (out / name).write_bytes(data)
    return [out / n for n in files], how


def verify_bg(ctx: Context, bg_ids: list[str] | None) -> list[str]:
    out = bg_output_dir(ctx)
    committed = sorted(p.stem for p in out.glob("*.png")) if out.is_dir() else []
    ids = bg_ids if bg_ids is not None else sorted(set(list_backgrounds(ctx)) | set(committed))
    problems = []
    for bg_id in ids:
        try:
            files, how = bg_outputs(ctx, load_background(ctx, bg_id))
        except SpriteError as e:
            problems.append(f"background {bg_id}: rebuild failed: {e}")
            continue
        for name, data in files.items():
            if not (out / name).is_file():
                problems.append(f"background {bg_id}: {name} is not built")
            elif not same_png((out / name).read_bytes(), data):
                problems.append(f"background {bg_id}: {name} differs from the rebuild ({how})")
            else:
                print(f"ok background {bg_id}: matches ({how})")
    return problems
