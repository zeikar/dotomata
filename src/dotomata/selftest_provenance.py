"""Selftest cases for where a generated image came from: the shared runner reads a run's image from that run's own codex
thread (the session record's bytes first, then the thread's generated_images
folder), never from the working folder, keeps it only if it was never recorded
before, and adoption refuses a candidate marked invalid. A parallel batch once recorded one unit's card art as
another's (a codex run made its own image, did not find its own file and copied
another thread's newest file instead)."""

from __future__ import annotations

import base64
import hashlib
import json
from pathlib import Path

from .selftest import expect, fake_codex_env, own_image, run_cli, write_project
from .selftest_art import art_fixture, art_tree


def sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def test_provenance(tmp: Path, real: dict) -> None:
    root, project = art_tree(tmp, real)
    project["art"]["cap_per_unit"] = 9
    write_project(root, project)
    env, log, _, _ = fake_codex_env(tmp)
    home = Path(env["CODEX_HOME"])
    fixture, foreign, fresh = tmp / "art_fixture.png", tmp / "foreign.png", tmp / "fresh.png"
    art_fixture(fixture)
    art_fixture(foreign, (1024, 1530))
    art_fixture(fresh, (1020, 1530))
    env["FAKE_CODEX_IMAGE"] = str(fixture)
    source, out = root / "sprites/source", root / project["paths"]["preview"]
    art_src, art_out = source / "T003/art", out / "T003/art"

    def cli(*args: str, **extra: str):
        return run_cli(root, *args, env={**env, **extra})

    def record(path: Path = art_src / "art_generation.json") -> list[dict]:
        return json.loads(path.read_text())["generations"]

    def runs() -> int:
        return len(log.read_text().splitlines())

    # The workdir file is another run's image: it is never read, the thread's own file is kept.
    r = cli("art", "generate", "T003", FAKE_CODEX_SCENARIO="foreign", FAKE_CODEX_FOREIGN_IMAGE=str(foreign))
    expect(r.returncode == 0 and "image from generated_images" in r.stdout, f"foreign run: {r.stdout}{r.stderr}")
    e = record()[-1]
    expect(e["candidate"] == "art_candidate_1.png" and e["error"] is None and e["note"] is None
           and e["output_sha256"] == sha(own_image(log, runs() - 1, home)) == sha(art_out / "art_candidate_1.png")
           and e["output_sha256"] != sha(foreign), f"foreign workdir image kept: {e}")

    # A workdir file, and a session record that shows no generation: no candidate and nothing to charge.
    r = cli("art", "generate", "T003", FAKE_CODEX_SCENARIO="untraced")
    e = record()[-1]
    expect(r.returncode != 0 and e["candidate"] is None and e["output_sha256"] is None
           and "without an image" in (e["error"] or "") and e["image_generation_calls"] == 0
           and not (art_out / "art_candidate_2.png").exists(), f"untraced image accepted: {e} {r.stdout}")
    status = json.loads(cli("art", "status", "T003").stdout)
    expect(status["image_generation_calls"] == 1 and status["candidates"] == 1, f"status after untraced {status}")

    # The same bytes in another kind's record: refused as a duplicate, charged, naming the record.
    bg_src = source / "backgrounds/field"
    bg_src.mkdir(parents=True)
    (bg_src / "background.json").write_text(json.dumps(
        {"id": "field", "layout": "battle", "colors": ["#7c9455"], "design": "a quiet test meadow"}))
    r = cli("bg", "generate", "field", FAKE_CODEX_SCENARIO="json", FAKE_CODEX_FIXED="1")
    expect(r.returncode == 0 and record(bg_src / "bg_generation.json")[-1]["output_sha256"] == sha(fixture),
           f"first fixed run: {r.stdout}{r.stderr}")
    for args, path in ((("art", "generate", "T003"), art_src / "art_generation.json"),
                       (("generate", "T001"), source / "T001/generation.json")):
        r = cli(*args, FAKE_CODEX_SCENARIO="json", FAKE_CODEX_FIXED="1")
        e = record(path)[-1]
        expect(r.returncode != 0 and e["candidate"] is None and e["image_generation_calls"] == 1
               and "duplicate image" in (e["error"] or "") and "bg_generation.json (bg_candidate_1.png)" in e["error"],
               f"{args}: duplicate across records accepted: {e}")
    # The untraced run left art_candidate_2.png free, so the refused art duplicate had reserved it.
    art_files = sorted(p.name for p in art_out.glob("art_candidate_*.png"))
    expect(art_files == ["art_candidate_1.png"] and not (source / "T001/candidate_1.png").exists(),
           f"a refused duplicate left a candidate file: {art_files}")

    # Two runs of one batch with the same bytes: the first is kept, the second refused.
    r = cli("art", "generate", "T003", "--batch", "2", FAKE_CODEX_SCENARIO="json", FAKE_CODEX_FIXED="1",
            FAKE_CODEX_IMAGE=str(fresh))
    first, second = record()[-2:]
    expect(r.returncode != 0 and first["output_sha256"] == sha(fresh) and first["error"] is None
           and second["candidate"] is None and "duplicate image" in second["error"] and "this batch" in second["error"],
           f"duplicate inside a batch: {first} {second}")

    # Marking a candidate invalid: kept in the record and the count, removed from the preview, refused by adopt.
    kept = first["candidate"]
    r = cli("art", "invalidate", "T003", "--candidate", "art_candidate_1.png", "--reason", " ")
    expect(r.returncode != 0 and "needs a reason" in r.stderr, f"invalidate without a reason: {r.stderr}")
    preview = art_out / "art_candidate_1.png"
    data = preview.read_bytes()
    r = cli("art", "invalidate", "T003", "--candidate", "art_candidate_1.png", "--reason", "copied from another run")
    expect(r.returncode == 0 and record()[0]["invalid"] == "copied from another run" and not preview.exists(),
           f"invalidate: {r.stdout}{r.stderr}")
    r = cli("art", "invalidate", "T003", "--candidate", "art_candidate_1.png", "--reason", "again")
    expect(r.returncode != 0 and "already marked invalid" in r.stderr, f"invalidated twice: {r.stderr}")
    preview.write_bytes(data)  # even with the file back, adoption refuses it
    r = cli("art", "adopt", "T003", "--candidate", "art_candidate_1.png", "--note", "x")
    expect(r.returncode != 0 and "marked invalid" in r.stderr and not (art_src / "art.json").exists(),
           f"invalid candidate adopted: {r.stderr}")
    expect(json.loads(cli("art", "status", "T003").stdout)["image_generation_calls"] == 4, "invalid changed the count")
    expect(cli("art", "adopt", "T003", "--candidate", kept, "--note", "x").returncode == 0, "valid adoption refused")
    r = cli("art", "invalidate", "T003", "--candidate", kept, "--reason", "x")
    expect(r.returncode != 0 and "adopt another candidate first" in r.stderr, f"adopted candidate invalidated: {r.stderr}")

    # The session record carries the bytes: its first completed generation is the run's image,
    # not a later one, not the file in the thread's folder and not the working folder's file.
    def record_results(index: int) -> list[bytes]:
        thread = own_image(log, index, home).parent.name
        rollout = next((home / "sessions").glob(f"*/*/*/rollout-*{thread}.jsonl"))
        items = [json.loads(line).get("payload", {}).get("item") or {} for line in rollout.read_text().splitlines()]
        return [base64.b64decode(i["result"]) for i in items if i.get("kind") == "image_gen.generation"]

    r = cli("art", "generate", "T003", FAKE_CODEX_SCENARIO="record")
    e = record()[-1]
    first, later = record_results(runs() - 1)
    folder = own_image(log, runs() - 1, home)
    expect(r.returncode == 0 and "image from session record" in r.stdout and e["error"] is None
           and e["output_sha256"] == hashlib.sha256(first).hexdigest() == sha(art_out / e["candidate"])
           and len({e["output_sha256"], hashlib.sha256(later).hexdigest(), sha(folder)}) == 3
           and e["image_generation_calls"] == 2 and "kept the first complete picture" in (e["note"] or ""),
           f"session record image: {e} {r.stdout}{r.stderr}")

    # A record whose result is not a PNG is skipped, with a note: the next record's picture is used.
    r = cli("art", "generate", "T003", FAKE_CODEX_SCENARIO="recordbroken")
    e = record()[-1]
    first, later = record_results(runs() - 1)
    expect(r.returncode == 0 and first == b"not a png" and e["error"] is None
           and e["output_sha256"] == hashlib.sha256(later).hexdigest() == sha(art_out / e["candidate"])
           and "skipped 1 generation record" in (e["note"] or ""), f"broken record result: {e} {r.stdout}{r.stderr}")

    # The session record shows a call to the image tool but no generation this code knows (a new
    # record shape would look like this): no candidate, and one call is charged all the same.
    r = cli("art", "generate", "T003", FAKE_CODEX_SCENARIO="callonly")
    e = record()[-1]
    expect(r.returncode != 0 and e["candidate"] is None and "without an image" in (e["error"] or "")
           and e["image_generation_calls"] == 1, f"image tool call without a known generation: {e} {r.stdout}")

    # Backgrounds share the adoption check.
    r = cli("bg", "invalidate", "field", "--candidate", "bg_candidate_1.png", "--reason", "copied")
    expect(r.returncode == 0, f"bg invalidate: {r.stderr}")
    r = cli("bg", "adopt", "field", "--candidate", "bg_candidate_1.png", "--note", "x")
    expect(r.returncode != 0 and "marked invalid" in r.stderr, f"invalid background adopted: {r.stderr}")


TESTS = [
    ("provenance: own thread's image (session record, then its folder), workdir file never read, a broken record "
     "skipped, an image tool call with no known generation charged, duplicates across records and in a batch, "
     "invalid not adopted",
     lambda real_ctx, tmp: test_provenance(tmp, real_ctx.project)),
]
