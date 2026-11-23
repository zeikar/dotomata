"""Selftest cases for card art and backgrounds.
They run against the fake codex in a temp tree, like the sprite cases in selftest.py."""

from __future__ import annotations

import copy
import hashlib
import json
from pathlib import Path

import numpy as np
from PIL import Image, PngImagePlugin

from .art import english_name, recolor
from .common import Context, Unit, json_bytes, sha256_file
from .generate import build_prompt
from .selftest import expect, expect_error, fake_codex_env, run_cli, tiny_project, write_project, write_tiny_tree

# A string table with another language column beside `en`, which is the one read.
STRINGS = ("key,context,fr,en\n"
           "unit.T001.name,test,Chevalier de test,Test Knight\n"
           "unit.T003.name,test,Pretre de test,Test Priest\n")
STRINGS_PATH, STYLE_PATH = "data/strings.csv", "refs/style_reference.jpg"
PAPER, GREEN, RED = (230, 215, 180), (60, 170, 50), (200, 40, 40)


def art_fixture(path: Path, size=(1024, 1536)) -> None:
    """Paper, a green blob (the part a recolour turns) and a red block, with a text
    chunk standing in for the generator's metadata."""
    w, h = size
    img = np.zeros((h, w, 3), np.uint8)
    img[:] = PAPER
    yy, xx = np.mgrid[0:h, 0:w]
    img[((xx - w // 2) / (w / 4)) ** 2 + ((yy - h // 2) / (h / 5)) ** 2 <= 1] = GREEN
    img[h // 10:h // 10 + h // 8, w // 10:w // 10 + w // 6] = RED
    info = PngImagePlugin.PngInfo()
    info.add_text("Comment", "made by a generator")
    Image.fromarray(img, "RGB").save(path, pnginfo=info)


def art_tree(tmp: Path, real: dict) -> tuple[Path, dict]:
    root = tmp / "art"
    project = tiny_project(real)
    project["art"]["cap_per_unit"] = 4
    project["bg"]["cap_per_background"] = 2
    # The default config has neither; these trees cover both, the optional cases are in test_art_optional.
    project["paths"]["strings"] = STRINGS_PATH
    project["art"]["style_reference"] = STYLE_PATH
    write_tiny_tree(root, project)
    strings = root / project["paths"]["strings"]
    strings.parent.mkdir(parents=True, exist_ok=True)
    strings.write_text(STRINGS, encoding="utf-8")
    source = root / "sprites/source"
    t001 = json.loads((source / "T001/unit.json").read_text())
    t001.update(name="unit.T001.name", colors=["#3050a0"], sprite_note="SPRITE-ONLY NOTE.")
    (source / "T001/unit.json").write_bytes(json_bytes(t001))
    for uid, extra in (("T003", {"design": "a tiny test priest", "name": "unit.T003.name", "colors": ["#e8e0c8"]}),
                       ("T004", {"name": "unit.T003.name"})):
        (source / uid).mkdir()
        (source / uid / "unit.json").write_bytes(json_bytes({"id": uid, "kind": "tiny", **extra}))
    style = root / project["art"]["style_reference"]
    style.parent.mkdir(parents=True, exist_ok=True)
    Image.new("RGB", (16, 9), (240, 225, 200)).save(style, "JPEG")
    return root, project


def png_chunks(data: bytes) -> list[bytes]:
    chunks, i = [], 8
    while i < len(data):
        n = int.from_bytes(data[i:i + 4], "big")
        chunks.append(data[i + 4:i + 8])
        i += 12 + n
    return chunks


def test_prompts(real_ctx: Context) -> None:
    ctx = Context(root=Path("."), project=tiny_project(real_ctx.project))
    unit = Unit("X", {"id": "X", "kind": "tiny", "design": "THE DESIGN.", "sprite_note": "THE NOTE."}, Path("."))
    prompt = build_prompt(ctx, unit, [], None)
    expect("THE NOTE. THE DESIGN." in prompt, f"sprite prompt does not put the sprite note before the design: {prompt}")
    # The size sentence and the card art sentence state the unit's own height when it has one.
    gen, kind = ctx.project["generation"], ctx.project["kinds"]["tiny"]
    for own, height in ((None, kind["height"]), (kind["height"] + 3, kind["height"] + 3)):
        data = {"id": "X", "kind": "tiny", "design": "THE DESIGN.", **({} if own is None else {"height": own})}
        prompt = build_prompt(ctx, Unit("X", data, Path(".")), [], "art")
        size = gen["size_sentence"].format(height=height, cell_width=kind["cell"][0], cell_height=kind["cell"][1])
        expect(size in prompt and gen["art_reference_sentences"]["creature"].format(height=height) in prompt,
               f"own height {own}: the prompt does not ask for {height} px: {prompt}")
    expect_error(lambda: build_prompt(ctx, Unit("X", {"id": "X", "kind": "tiny", "prompt": "old"}, Path(".")), [], None),
                 "has no 'design'")


def test_art(tmp: Path, real: dict) -> None:
    root, project = art_tree(tmp, real)
    env, log, _, _ = fake_codex_env(tmp)
    fixture = tmp / "art_fixture.png"
    art_fixture(fixture)
    env.update(FAKE_CODEX_IMAGE=str(fixture), FAKE_CODEX_SCENARIO="json")
    source, out = root / "sprites/source", root / project["paths"]["preview"]
    art_src, art_out = source / "T001/art", out / "T001/art"
    outputs = root / project["paths"]["art_output"]
    a = real["art"]

    def calls() -> list[dict]:
        return [json.loads(line) for line in log.read_text().splitlines()] if log.exists() else []

    def cli(*args: str):
        return run_cli(root, *args, env=env)

    # A unit without actions has card art only: sprite build and verify skip it.
    r = cli("build", "T003")
    expect(r.returncode == 0 and "skip T003" in r.stdout and not (root / project["paths"]["output"] / "T003").exists(),
           f"sprite build did not skip a unit without actions: {r.stdout}{r.stderr}")
    r = cli("verify", "T003")
    expect(r.returncode == 0 and "skip T003" in r.stdout, f"sprite verify did not skip T003: {r.stdout}")
    r = cli("art", "generate", "T004")
    expect(r.returncode != 0 and "has no 'design'" in r.stderr and not calls(), f"art without design: {r.stderr}")
    style = root / project["art"]["style_reference"]
    style.rename(style.with_suffix(".bak"))
    r = cli("art", "generate", "T003")
    expect(r.returncode != 0 and "style reference" in r.stderr and not calls(), f"missing style reference: {r.stderr}")
    style.with_suffix(".bak").rename(style)

    # With a built sprite the dot reference is required, then sent with every run.
    expect(cli("build", "T001").returncode == 0, "tiny build failed")
    r = cli("art", "generate", "T001")
    expect(r.returncode != 0 and "dot_ref.png" in r.stderr and not calls(), f"missing dot reference accepted: {r.stderr}")
    expect(cli("art", "dot-ref", "T001").returncode == 0, "dot-ref failed")
    dot = art_src / "dot_ref.png"
    with Image.open(dot) as im:
        ref = np.array(im)
    block, margin = real["reduce"]["ref_block"], real["reduce"]["ref_margin_blocks"]
    # The tiny idle frame 0 is 4x6 opaque cells (columns 1-4 of the 5-wide grid).
    expect(ref.shape == ((6 + 2 * margin) * block, (4 + 2 * margin) * block, 3) and (ref[0, 0] == (255, 0, 255)).all(),
           f"dot reference shape {ref.shape}")
    r = cli("art", "generate", "T001", "--batch", "3", "--variant", "V-ONE", "--variant", "V-TWO")
    expect(r.returncode == 0, f"art batch failed: {r.stdout}{r.stderr}")
    runs = calls()
    expect(len(runs) == 3 and all(str(dot.resolve()) in c["args"] and str(style.resolve()) in c["args"] for c in runs),
           "dot and style references not sent with -i")
    record = json.loads((art_src / "art_generation.json").read_text())["generations"]
    expect([e["candidate"] for e in record] == [f"art_candidate_{i}.png" for i in (1, 2, 3)], f"art record {record}")
    p = record[0]["prompt"]
    for part in (a["common_prompt"], a["composition_sentence"], a["design_rule_sentence"], "a tiny test knight",
                 a["style_reference_sentence"], a["dot_reference_sentence"], "For this candidate: V-ONE"):
        expect(part in p, f"art prompt lacks '{part[:40]}': {p}")
    expect("V-TWO" not in p and "V-TWO" in record[1]["prompt"] and "For this candidate" not in record[2]["prompt"],
           "variants landed in the wrong runs")
    expect("SPRITE-ONLY" not in p and real["generation"]["common_prompt"] not in p, "sprite text in the art prompt")
    expect(record[0]["reference"]["mode"] == "dot", f"reference record {record[0]['reference']}")
    expect(record[0]["style_reference"] == {"path": project["art"]["style_reference"], "sha256": sha256_file(style)},
           f"style reference record {record[0].get('style_reference')}")
    expect(all((art_out / e["candidate"]).is_file() and not (art_src / e["candidate"]).exists() for e in record),
           "candidates are not in the preview folder only")
    with Image.open(art_src / "art_batch_1-3.jpg") as im:
        expect(im.format == "JPEG" and max(im.size) <= real["comparison_sheet"]["long_side"],
               f"comparison sheet {im.format} {im.size}")
    # Counts per kind: the sprite record is untouched, and the art cap refuses before codex runs.
    status = json.loads(cli("checkpoint", "T001").stdout)
    expect(status["image_generation_calls"] == 0, f"art counted as sprite generations: {status}")
    r = cli("art", "generate", "T001", "--batch", "2")
    expect(r.returncode != 0 and "3 of 4" in r.stderr and len(calls()) == 3, f"art cap did not refuse: {r.stderr}")
    r = cli("art", "generate", "T001", "--variant", "in the style of Zelda")
    expect(r.returncode != 0 and "game or artist names" in r.stderr and len(calls()) == 3, f"banned name: {r.stderr}")

    # Adoption copies the pick into the source; the rest stays in the preview folder.
    r = cli("art", "adopt", "T001", "--candidate", "art_candidate_2.png", "--note", "clear face")
    expect(r.returncode == 0 and "art bust" in r.stdout, f"adopt failed: {r.stdout}{r.stderr}")
    expect((art_src / "art_candidate_2.png").is_file() and not (art_src / "art_candidate_1.png").exists(),
           "adoption did not copy only the pick into the source")
    r = cli("art", "build", "T001")
    expect(r.returncode != 0 and "no bust crop" in r.stderr, f"build without a bust: {r.stderr}")
    adoption = (art_src / "art.json").read_bytes()
    for rect, message in ((["600", "1000", "600", "600"], "outside"), (["0", "0", "600", "601"], "not square"),
                          (["0", "0", "511", "511"], "smaller than 512")):
        r = cli("art", "bust", "T001", "--rect", *rect)
        expect(r.returncode != 0 and message in r.stderr and (art_src / "art.json").read_bytes() == adoption,
               f"bust {rect} not refused with '{message}': {r.stderr}")
    expect(cli("art", "bust", "T001", "--rect", "212", "300", "600", "600").returncode == 0, "good bust refused")
    adoption = (art_src / "art.json").read_bytes()
    r = cli("art", "adopt", "T001", "--candidate", "art_candidate_3.png", "--note", "x", "--bust", "600", "1000", "600", "600")
    expect(r.returncode != 0 and "outside" in r.stderr and not (art_src / "art_candidate_3.png").exists()
           and (art_src / "art.json").read_bytes() == adoption,
           f"adopt with a bad bust left a candidate in the source or changed art.json: {r.stderr}")
    r = cli("art", "adopt", "T001", "--candidate", "art_candidate_9.png", "--note", "x")
    expect(r.returncode != 0 and "not in art_generation.json" in r.stderr, f"unrecorded candidate adopted: {r.stderr}")
    (art_out / "art_candidate_3.png").write_bytes(b"changed")
    r = cli("art", "adopt", "T001", "--candidate", "art_candidate_3.png", "--note", "x")
    expect(r.returncode != 0 and "does not match the sha256" in r.stderr, f"changed candidate adopted: {r.stderr}")

    r = cli("art", "build", "T001")
    expect(r.returncode == 0, f"art build failed: {r.stdout}{r.stderr}")
    full_path, bust_path = outputs / "T001/T001_base_full.png", outputs / "T001/T001_base_bust.png"
    with Image.open(fixture) as im:
        pixels = np.array(im.convert("RGB"))
    with Image.open(full_path) as im:
        expect(im.mode == "RGB" and (np.array(im) == pixels).all(), "full is not the candidate's pixels")
    expect(set(png_chunks(full_path.read_bytes())) == {b"IHDR", b"IDAT", b"IEND"}, "full keeps metadata chunks")
    expected_bust = np.array(Image.fromarray(pixels[300:900, 212:812]).resize((512, 512), Image.Resampling.LANCZOS))
    with Image.open(bust_path) as im:
        expect(im.size == (512, 512) and (np.array(im) == expected_bust).all(), "bust is not the LANCZOS crop")
    r = cli("verify")
    expect(r.returncode == 0 and "T001: card art 2 files match (adopted)" in r.stdout, f"verify: {r.stdout}")
    data = bytearray(bust_path.read_bytes())
    data[-20] ^= 0xFF
    bust_path.write_bytes(bytes(data))
    r = cli("verify", "T001")
    expect(r.returncode != 0 and "T001_base_bust.png differs" in r.stdout, f"verify missed a changed byte: {r.stdout}")
    expect(cli("art", "build", "T001").returncode != 0, "art build overwrote without --force")

    # Adopting again: the new pick becomes the source, the old one only stays in the history.
    r = cli("art", "adopt", "T001", "--candidate", "art_candidate_1.png", "--note", "calmer pose",
            "--bust", "112", "200", "800", "800")
    expect(r.returncode == 0, f"second adoption failed: {r.stderr}")
    expect((art_src / "art_candidate_1.png").is_file() and not (art_src / "art_candidate_2.png").exists()
           and (art_out / "art_candidate_2.png").is_file(), "the earlier pick did not leave the source")
    history = json.loads((art_src / "art.json").read_text())["stages"]["base"]["adoptions"]
    expect([h["candidate"] for h in history] == ["art_candidate_2.png", "art_candidate_1.png"], f"history {history}")
    expect(cli("art", "build", "T001", "--force").returncode == 0 and cli("verify").returncode == 0,
           "rebuild after the second adoption")

    # A sprite rebuild from a changed source refreshes the existing dot reference, so verify still passes.
    old_dot = dot.read_bytes()
    palette_path = source / "T001/palette.json"
    palette = json.loads(palette_path.read_text())
    palette["colors"]["c"] = "#a0a0b0"
    palette_path.write_bytes(json_bytes(palette))
    r = cli("build", "T001", "--force")
    expect(r.returncode == 0 and "refreshed" in r.stdout and dot.read_bytes() != old_dot,
           f"sprite build did not refresh dot_ref.png: {r.stdout}{r.stderr}")
    r = cli("verify")
    expect(r.returncode == 0, f"verify after the sprite rebuild: {r.stdout}")

    # Placeholder until an adoption: deterministic, 2:3 with a 512 bust.
    expect(cli("art", "build", "T003").returncode == 0, "placeholder build failed")
    first = (outputs / "T003/T003_base_full.png").read_bytes()
    expect(cli("art", "build", "T003", "--force").returncode == 0, "placeholder rebuild failed")
    expect((outputs / "T003/T003_base_full.png").read_bytes() == first, "placeholder is not deterministic")
    with Image.open(outputs / "T003/T003_base_full.png") as im, Image.open(outputs / "T003/T003_base_bust.png") as b:
        expect(im.size == tuple(a["size"]) and b.size == (512, 512), f"placeholder sizes {im.size} {b.size}")

    # A palette-swap unit is a recolour unit before its rule exists: status says so and no dot reference is made.
    expect(cli("palette-swap", "T002").returncode == 0, "tiny palette swap failed")
    status = json.loads(cli("art", "status", "T002").stdout)
    expect(status["recolor"] is True and status["recolor_rule"] is False, f"palette-swap status {status}")
    r = cli("art", "dot-ref", "T002")
    expect(r.returncode != 0 and "recolour" in r.stderr and not (source / "T002/art/dot_ref.png").exists(),
           f"dot reference made for a recolour unit: {r.stderr}")

    # The recolour unit: the base's adopted art with the green range turned, nothing else touched.
    (source / "T002/art").mkdir()
    rule = {"base": "T001", "hue_from": [60, 170], "hue_to": 265, "min_saturation": 20}
    (source / "T002/art/recolor.json").write_bytes(json_bytes(rule))
    expect(json.loads(cli("art", "status", "T002").stdout)["recolor_rule"] is True, "recolor_rule after recolor.json")
    r = cli("art", "generate", "T002")
    expect(r.returncode != 0 and "recolour" in r.stderr, f"recolour unit generated: {r.stderr}")
    r = cli("art", "adopt", "T002", "--candidate", "art_candidate_1.png", "--note", "x")
    expect(r.returncode != 0 and "recolour" in r.stderr, f"recolour unit adopted: {r.stderr}")
    expect(cli("art", "build", "T002").returncode == 0, "recolour build failed")
    with Image.open(outputs / "T002/T002_base_full.png") as im:
        turned = np.array(im)
    green = (pixels == GREEN).all(axis=2)
    expect((turned[~green] == pixels[~green]).all(), "recolour changed pixels outside the hue range")
    hue = np.array(Image.fromarray(turned).convert("HSV"))[..., 0][green]
    expect(green.any() and abs(int(hue.mean()) * 360 // 255 - 265) <= 10, f"green turned to hue {hue.mean()}")
    r = cli("verify")
    expect(r.returncode == 0 and "T002: card art 2 files match (recolour)" in r.stdout, f"verify: {r.stdout}")
    expect_error(lambda: recolor(pixels[:4, :4], dict(rule, hue_from=[170, 60]), "t"), "0 <= lo < hi")

    # Optional saturation and value scale only the turned pixels, capped at full; 100 equals leaving them out.
    expect((recolor(pixels, dict(rule, saturation=100, value=100), "t") == turned).all(),
           "saturation and value 100 changed the recolour")
    scaled = recolor(pixels, dict(rule, saturation=120, value=200), "t")
    expect((scaled[~green] == pixels[~green]).all(), "saturation and value touched pixels outside the hue range")
    before, after = (np.array(Image.fromarray(a).convert("HSV")).astype(np.int64)[green] for a in (turned, scaled))
    expect((after[:, 2] == 255).all() and (after[:, 1] > before[:, 1]).all()
           and abs(int(after[:, 0].mean()) * 360 // 255 - 265) <= 10,
           f"scaled recolour HSV {after.mean(axis=0)} from {before.mean(axis=0)}")
    expect_error(lambda: recolor(pixels[:4, :4], dict(rule, saturation=0), "t"), "saturation > 0")
    expect_error(lambda: recolor(pixels[:4, :4], dict(rule, value=1.5), "t"), "whole numbers")


def test_backgrounds(tmp: Path, real: dict) -> None:
    root, project = art_tree(tmp, real)
    env, log, _, _ = fake_codex_env(tmp)
    fixture = tmp / "bg_fixture.png"
    art_fixture(fixture, (1672, 941))
    env.update(FAKE_CODEX_IMAGE=str(fixture), FAKE_CODEX_SCENARIO="json")
    bg_src = root / "sprites/source/backgrounds/field"
    bg_src.mkdir(parents=True)
    definition = {"id": "field", "layout": "battle", "colors": ["#7c9455"], "design": "a quiet test meadow"}
    (bg_src / "background.json").write_bytes(json_bytes(definition))
    out = root / project["paths"]["bg_output"] / "field.png"

    def cli(*args: str):
        return run_cli(root, *args, env=env)

    expect(cli("bg", "build", "field").returncode == 0 and cli("verify").returncode == 0, "placeholder background")
    with Image.open(out) as im:
        expect(im.size == (1920, 1080), f"placeholder size {im.size}")
    r = cli("bg", "generate", "field", "--batch", "2")
    expect(r.returncode == 0, f"bg batch failed: {r.stdout}{r.stderr}")
    record = json.loads((bg_src / "bg_generation.json").read_text())["generations"]
    b = real["bg"]
    p = record[0]["prompt"]
    expect(b["layouts"]["battle"].format(design="a quiet test meadow") in p and b["common_prompt"] in p
           and "lower 55%" in p and "shallow diagonal from upper left to lower middle" in p,
           f"bg prompt {p}")
    calls = [json.loads(line) for line in log.read_text().splitlines()]
    expect(len(calls) == 2 and not any("-i" in c["args"] for c in calls), "background runs got a reference")
    expect(not (bg_src / "bg_candidate_1.png").exists(), "a candidate went into the source before adoption")
    with Image.open(bg_src / "bg_batch_1-2.jpg") as im:
        expect(im.format == "JPEG" and max(im.size) <= real["comparison_sheet"]["long_side"], f"sheet {im.size}")
    r = cli("bg", "generate", "field")
    expect(r.returncode != 0 and "2 of 2" in r.stderr and len(log.read_text().splitlines()) == 2,
           f"bg cap did not refuse: {r.stderr}")
    expect(json.loads(cli("art", "status", "T001").stdout)["image_generation_calls"] == 0, "bg counted as card art")

    expect(cli("bg", "adopt", "field", "--candidate", "bg_candidate_1.png", "--note", "calm ground").returncode == 0,
           "bg adopt failed")
    r = cli("bg", "build", "field", "--force")
    expect(r.returncode == 0, f"bg build failed: {r.stderr}")
    with Image.open(fixture) as im:
        expected = np.array(im.convert("RGB").resize((1920, 1081), Image.Resampling.LANCZOS))[0:1080]
    with Image.open(out) as im:
        expect(im.size == (1920, 1080) and (np.array(im) == expected).all(), "background is not the cover-fit crop")
    r = cli("verify", "backgrounds/field")
    expect(r.returncode == 0 and "matches (adopted)" in r.stdout, f"bg verify: {r.stdout}")
    data = bytearray(out.read_bytes())
    data[-20] ^= 0xFF
    out.write_bytes(bytes(data))
    r = cli("verify")
    expect(r.returncode != 0 and "field.png differs" in r.stdout, f"bg verify missed a changed byte: {r.stdout}")
    (bg_src / "background.json").write_bytes(json_bytes(dict(definition, layout="sideways")))
    r = cli("bg", "generate", "field")
    expect(r.returncode != 0 and "unknown layout" in r.stderr, f"unknown layout accepted: {r.stderr}")


def test_sprite_from_art(tmp: Path, real: dict) -> None:
    """generate --from-art: the adopted card art is the design reference (art first)."""
    gen = real["generation"]
    ctx = Context(root=Path("."), project=tiny_project(real))
    person = build_prompt(ctx, Unit("X", {"id": "X", "kind": "humanoid", "design": "D."}, Path(".")), [], "art")
    creature = build_prompt(ctx, Unit("X", {"id": "X", "kind": "tiny", "design": "D."}, Path(".")), [], "art")
    expect(gen["art_reference_sentences"]["humanoid"] in person and "{height}" not in creature
           and gen["art_reference_sentences"]["creature"].format(height=6) in creature,
           f"art reference sentences: {person} / {creature}")

    root, _ = art_tree(tmp, real)
    env, log, sprite_image, _ = fake_codex_env(tmp)
    art_image = tmp / "art_fixture.png"
    art_fixture(art_image)
    source = root / "sprites/source/T003"

    def calls() -> int:
        return len(log.read_text().splitlines()) if log.exists() else 0

    def cli(*args: str, image: Path = sprite_image):
        return run_cli(root, *args, env={**env, "FAKE_CODEX_IMAGE": str(image), "FAKE_CODEX_SCENARIO": "json"})

    r = cli("generate", "T003", "--from-art")
    expect(r.returncode != 0 and "no adopted card art" in r.stderr and not calls(), f"--from-art without art: {r.stderr}")
    expect(cli("art", "generate", "T003", image=art_image).returncode == 0, "art generate failed")
    expect(cli("art", "adopt", "T003", "--candidate", "art_candidate_1.png", "--note", "x").returncode == 0, "adopt failed")
    art = source / "art/art_candidate_1.png"
    before = calls()
    r = cli("generate", "T003")
    expect(r.returncode != 0 and "--from-art" in r.stderr, f"design pass without a reference for a unit with art: {r.stderr}")
    (source / "ref_x12.png").write_bytes(sprite_image.read_bytes())
    for extra in (["--design-from-file", "ref_x12.png"], ["--design-from", "candidate_1.png"],
                  ["--reference", str(sprite_image)]):
        r = cli("generate", "T003", "--from-art", *extra)
        expect(r.returncode != 0 and "not allowed with" in r.stderr, f"--from-art with {extra} accepted: {r.stderr}")
    expect(calls() == before, "codex was called for a refused --from-art")

    r = cli("generate", "T003", "--from-art", "--batch", "2")
    expect(r.returncode == 0, f"generate --from-art failed: {r.stdout}{r.stderr}")
    entries = json.loads((source / "generation.json").read_text())["generations"]
    digest = hashlib.sha256(art.read_bytes()).hexdigest()
    for e in entries:
        expect(e["reference"] == {"mode": "art", "path": "sprites/source/T003/art/art_candidate_1.png",
                                  "sha256": digest}, f"art reference record {e['reference']}")
        expect(gen["art_reference_sentences"]["creature"].format(height=6) in e["prompt"]
               and gen["design_file_sentence"] not in e["prompt"], f"--from-art prompt {e['prompt']}")
    runs = [json.loads(line)["args"] for line in log.read_text().splitlines()][before:]
    expect(len(runs) == 2 and all(str(art.resolve()) in a for a in runs), "the card art was not sent with -i")

    # The adopted file must still be the one art.json recorded.
    art.write_bytes(art.read_bytes() + b"x")
    before = calls()
    r = cli("generate", "T003", "--from-art")
    expect(r.returncode != 0 and "does not match the sha256" in r.stderr and calls() == before,
           f"changed card art accepted: {r.stderr}")


def test_art_optional(tmp: Path, real: dict) -> None:
    """paths.strings and art.style_reference are optional: without the table the unit's name is the
    English name itself; without a style reference no style image or sentence is sent."""
    root, project = art_tree(tmp, real)
    env, log, _, _ = fake_codex_env(tmp)
    fixture = tmp / "art_fixture.png"
    art_fixture(fixture)
    env.update(FAKE_CODEX_IMAGE=str(fixture), FAKE_CODEX_SCENARIO="json")
    full = root / project["paths"]["art_output"] / "T003/T003_base_full.png"
    t003 = root / project["paths"]["source"] / "T003"

    # With paths.strings the name is a key into the table's en column.
    got = english_name(Context(root, project), "unit.T003.name", "t")
    expect(got == "Test Priest", f"name from the string table: {got}")
    expect(run_cli(root, "art", "build", "T003").returncode == 0, "placeholder build with the string table failed")
    looked_up = full.read_bytes()

    # Without it the name is used as it is: the literal English name draws the same card.
    plain = copy.deepcopy(project)
    del plain["paths"]["strings"]
    plain["art"]["style_reference"] = None
    write_project(root, plain)
    got = english_name(Context(root, plain), "unit.T003.name", "t")
    expect(got == "unit.T003.name", f"name without a string table: {got}")
    data = json.loads((t003 / "unit.json").read_text())
    data["name"] = "Test Priest"
    (t003 / "unit.json").write_bytes(json_bytes(data))
    r = run_cli(root, "art", "build", "T003", "--force")
    expect(r.returncode == 0 and full.read_bytes() == looked_up,
           f"placeholder from the literal name differs from the looked-up one: {r.stdout}{r.stderr}")

    # Without art.style_reference: no image attached, no style sentence, the record says null.
    (root / STYLE_PATH).unlink()
    r = run_cli(root, "art", "generate", "T003", env=env)
    expect(r.returncode == 0, f"art generate without a style reference failed: {r.stdout}{r.stderr}")
    runs = [json.loads(line) for line in log.read_text().splitlines()]
    expect(len(runs) == 1 and "-i" not in runs[0]["args"], f"an image was attached: {runs[-1]['args']}")
    entry = json.loads((t003 / "art/art_generation.json").read_text())["generations"][-1]
    expect(project["art"]["style_reference_sentence"] not in entry["prompt"], f"style sentence sent: {entry['prompt']}")
    expect(entry["style_reference"] is None and entry["candidate"] == "art_candidate_1.png",
           f"style reference record {entry.get('style_reference')}")


TESTS = [
    ("design: sprite prompt from design and sprite_note, no design refused", lambda real_ctx, tmp: test_prompts(real_ctx)),
    ("generate --from-art: adopted card art as the reference, record, exclusive, refused without art",
     lambda real_ctx, tmp: test_sprite_from_art(tmp, real_ctx.project)),
    ("art: skip without actions, design, dot reference, prompt, counts, cap, adoption, bust, build, verify, "
     "placeholder, recolour", lambda real_ctx, tmp: test_art(tmp, real_ctx.project)),
    ("art: no string table (literal name), no style reference (no image, no sentence, null record)",
     lambda real_ctx, tmp: test_art_optional(tmp, real_ctx.project)),
    ("bg: placeholder, prompt, counts, cap, adoption, cover-fit build, verify",
     lambda real_ctx, tmp: test_backgrounds(tmp, real_ctx.project)),
]
