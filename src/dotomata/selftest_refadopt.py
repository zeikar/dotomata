"""Selftest cases for the size-pass fallback (2026-10-17): adopting a reduced
reference (ref_*.png from `reduce`) at checkpoint 1 instead of an AI candidate."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

import numpy as np
from PIL import Image

from .common import json_bytes
from .convert import convert_image
from .imaging import load_rgba
from .selftest import SOURCE_COLORS, SOURCE_SPRITE, expect, run_cli, synthetic_ai_image, tiny_project, write_project

UNIT = "T003"
REF = "ref_10px_x12.png"
# One green cell inside the red tunic: the native palette has it, the reduced grid
# loses it, so the reduced grid's symbols have a gap that a fresh palette would close.
SPRITE = [row[:4] + "g" + row[5:] if i == 8 else row for i, row in enumerate(SOURCE_SPRITE)]
COLORS = dict(SOURCE_COLORS, g=(40, 160, 60))


def _setup(tmp: Path, real: dict) -> tuple[Path, Path]:
    root = tmp / "refadopt"
    write_project(root, tiny_project(real))
    unit = root / "sprites/source" / UNIT
    unit.mkdir(parents=True)
    (unit / "unit.json").write_bytes(json_bytes({"id": UNIT, "kind": "tiny", "design": "a tiny test knight"}))
    Image.fromarray(synthetic_ai_image(SPRITE, COLORS), "RGB").save(unit / "candidate_1.png")
    (unit / "generation.json").write_bytes(json_bytes({"unit": UNIT, "generations": [
        {"candidate": "candidate_1.png", "image_generation_calls": 1, "height_check": "FAIL", "adopted": False,
         "reason": "design adopted, size pass"}]}))
    r = run_cli(root, "reduce", UNIT, "--candidate", "candidate_1.png", "--height", "10")
    expect(r.returncode == 0, f"reduce failed: {r.stdout}{r.stderr}")
    return root, unit


def _adopt(root: Path, name: str, *extra: str):
    return run_cli(root, "checkpoint", UNIT, "--record", "1", "--approve", "--reference", name,
                   "--note", "size pass kept failing; user adopts the reduced ref", *extra)


def _refused(root: Path, unit: Path, name: str, contains: str, *extra: str) -> None:
    r = _adopt(root, name, *extra)
    expect(r.returncode != 0 and contains in r.stderr, f"adopting {name} {extra} not refused with '{contains}': "
                                                      f"{r.stdout}{r.stderr}")
    expect(not (unit / "checkpoints.json").exists(), f"a refused adoption of {name} wrote checkpoints.json")


def _edit_pixel(path: Path, block: int, margin: int) -> None:
    img = np.array(Image.open(path))
    img[margin:margin + block, margin:margin + block] = (10, 200, 30)
    Image.fromarray(img, "RGB").save(path)


def test_adopt_refusals(real_ctx, tmp: Path) -> None:
    root, unit = _setup(tmp, real_ctx.project)
    cfg = real_ctx.project["reduce"]
    block, margin = cfg["ref_block"], cfg["ref_block"] * cfg["ref_margin_blocks"]
    ref, side = unit / REF, unit / (REF[:-4] + ".json")
    good_png, good_json = ref.read_bytes(), side.read_bytes()

    _refused(root, unit, "ref_99px_x12.png", "does not exist")
    _refused(root, unit, "candidate_1.png", "not a ref_*.png")
    _refused(root, unit, REF, "--record 1 --approve", "--candidate", "candidate_1.png")
    r = run_cli(root, "checkpoint", UNIT, "--record", "1", "--reject", "--reference", REF, "--note", "no")
    expect(r.returncode != 0 and "--record 1 --approve" in r.stderr, f"--reject with --reference: {r.stderr}")
    # A ref without its reduce record (a hand-made reference) is not reduce output.
    (unit / "ref_hand.png").write_bytes(good_png)
    _refused(root, unit, "ref_hand.png", "no reduce record")
    # An edited ref no longer matches its record.
    _edit_pixel(ref, block, margin)
    _refused(root, unit, REF, "sha256")
    # Edited ref and a record rewritten to match: the rebuild from the source catches it.
    rec = json.loads(good_json)
    side.write_bytes(json_bytes(dict(rec, sha256=hashlib.sha256(ref.read_bytes()).hexdigest())))
    _refused(root, unit, REF, "no longer gives")
    ref.write_bytes(good_png)
    side.write_bytes(good_json)
    # The source candidate is part of the record.
    cand = unit / "candidate_1.png"
    original = cand.read_bytes()
    cand.write_bytes(original + b"x")
    _refused(root, unit, REF, "source_sha256")
    cand.write_bytes(original)
    side.unlink()
    _refused(root, unit, REF, "no reduce record")


def test_adopt_convert_verify(real_ctx, tmp: Path) -> None:
    real = real_ctx.project
    root, unit = _setup(tmp, real)
    generation = (unit / "generation.json").read_bytes()
    ref_sha = json.loads((unit / (REF[:-4] + ".json")).read_text())["sha256"]

    r = _adopt(root, REF)
    expect(r.returncode == 0, f"adoption of a valid reduced ref refused: {r.stdout}{r.stderr}")
    entry = json.loads((unit / "checkpoints.json").read_text())["entries"][-1]
    expect((entry["checkpoint"], entry["decision"], entry["kind"], entry["reference"], entry["reference_sha256"],
            entry["reference_source"], entry["candidate"])
           == (1, "approved", "reduced-ref", REF, ref_sha, "candidate_1.png", None)
           and "user adopts" in entry["note"], f"checkpoint entry {entry}")
    expect((unit / "generation.json").read_bytes() == generation, "adoption changed generation.json")
    r = run_cli(root, "checkpoint", UNIT, "--require", "1")
    expect(r.returncode == 0, f"checkpoint 1 not approved after adoption: {r.stdout}")

    r = run_cli(root, "convert", UNIT)
    expect(r.returncode == 0, f"convert of the adopted ref failed: {r.stdout}{r.stderr}")
    reduced = (root / real["paths"]["preview"] / UNIT / "ref_10px_x12_grid.txt").read_bytes()
    native = convert_image(*load_rgba(unit / "candidate_1.png"), real["convert"], real["palette"])[1]
    expect(set(native) - set(reduced.decode()), "fixture: the reduced grid should drop a native colour")
    expect((unit / "grid.txt").read_bytes() == reduced,
           "convert of the adopted ref is not the reduced grid byte for byte:\n"
           + (unit / "grid.txt").read_text() + "vs\n" + reduced.decode())
    conv = json.loads((unit / "convert.json").read_text())
    expect((conv["kind"], conv["source"], conv["source_sha256"], conv["reduced_from"], conv["result"]["pixel_size"])
           == ("reduced-ref", REF, ref_sha, "candidate_1.png", real["reduce"]["ref_block"]), f"convert.json {conv}")
    used = set((unit / "grid.txt").read_text()) - {".", "\n"}
    colors = json.loads((unit / "palette.json").read_text())["colors"]
    expect(set(colors) == used, f"palette {sorted(colors)} is not the used symbols {sorted(used)}")

    r = run_cli(root, "verify", "--convert")
    expect(r.returncode == 0 and f"ok {UNIT}: convert reproduces" in r.stdout, f"verify --convert: {r.stdout}{r.stderr}")
    grid = unit / "grid.txt"
    good = grid.read_bytes()
    grid.write_bytes(good.replace(b".", b"a", 1))
    r = run_cli(root, "verify", "--convert")
    expect(r.returncode != 0 and "no longer gives grid.txt" in r.stdout, f"verify missed an edited grid: {r.stdout}")
    grid.write_bytes(good)
    ref = unit / REF
    _edit_pixel(ref, real["reduce"]["ref_block"], real["reduce"]["ref_block"] * real["reduce"]["ref_margin_blocks"])
    r = run_cli(root, "verify", "--convert")
    expect(r.returncode != 0 and "sha256" in r.stdout, f"verify missed an edited ref: {r.stdout}")
    r = run_cli(root, "convert", UNIT, "--force")
    expect(r.returncode != 0 and "sha256" in r.stderr, f"convert accepted an edited ref: {r.stdout}{r.stderr}")
    # An intact ref of the same name reduced again from another candidate is not the one adopted.
    Image.fromarray(synthetic_ai_image(SPRITE, dict(COLORS, r=(200, 120, 40))), "RGB").save(unit / "candidate_2.png")
    gen = json.loads(generation)
    gen["generations"].append(dict(gen["generations"][0], candidate="candidate_2.png"))
    (unit / "generation.json").write_bytes(json_bytes(gen))
    r = run_cli(root, "reduce", UNIT, "--candidate", "candidate_2.png", "--height", "10", "--force")
    expect(r.returncode == 0, f"reduce of candidate_2: {r.stdout}{r.stderr}")
    r = run_cli(root, "convert", UNIT, "--force")
    expect(r.returncode != 0 and "adopted at checkpoint 1" in r.stderr,
           f"convert used a re-reduced ref that was not adopted: {r.stdout}{r.stderr}")


TESTS = [
    ("ref adoption: refused for a missing, hand-made or edited ref, an edited record or source, wrong flags",
     test_adopt_refusals),
    ("ref adoption: checkpoint entry, generation.json untouched, convert = reduced grid, verify --convert",
     test_adopt_convert_verify),
]
