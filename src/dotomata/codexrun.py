"""Image generation through `codex exec`, shared by battle sprites, card art,
backgrounds and any kind a caller adds: one image per codex run, up to batch_max runs at once, a hard cap
per owner, banned-name checks, provenance checks (a run's image is read from
its own codex thread and must not be recorded already), generation records and
comparison sheets.

What differs per kind (where candidates go, which record counts toward which
cap, where the sheet is written) comes in as a Target."""

from __future__ import annotations

import base64
import binascii
import datetime
import hashlib
import json
import math
import os
import shlex
import subprocess
import tempfile
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

from PIL import Image, ImageDraw, ImageFont

from .common import Context, SpriteError, json_bytes, parse_hex, read_json

PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"


@dataclass
class Target:
    """One owner of generations: a unit's sprites, a unit's card art or a background.

    Each kind keeps its own record file, so its count and cap never mix with
    another kind's."""
    label: str                 # for messages, e.g. "unit 'hero'" or "art of 'hero'"
    header: dict               # the record's first keys, e.g. {"unit": "hero"}
    record: Path               # generation record (generation.json, art_generation.json, ...)
    out_dir: Path              # where new candidates are saved
    prefix: str                # candidate file prefix: candidate_N.png, art_candidate_N.png, ...
    cap: int
    batch_max: int
    # Folders whose candidate files also hold a number (art keeps adopted ones in the source).
    keep_dirs: list[Path] = field(default_factory=list)


def codex_command(ctx: Context) -> list[str]:
    # DOTOMATA_CODEX_CMD swaps in a fake codex for the selftest; normal runs use the config.
    override = os.environ.get("DOTOMATA_CODEX_CMD")
    return shlex.split(override) if override else list(ctx.project["generation"]["codex_command"])


def codex_home() -> Path:
    return Path(os.environ.get("CODEX_HOME") or Path.home() / ".codex")


def check_banned(ctx: Context, label: str, prompt: str) -> None:
    lowered = prompt.lower()
    banned = [t for t in ctx.project["generation"]["banned_terms"] if t.lower() in lowered]
    if banned:
        raise SpriteError(f"{label}: prompt names {banned}; do not use game or artist names")


def load_record(target: Target) -> dict:
    return read_json(target.record) if target.record.is_file() else {**target.header, "generations": []}


def calls_used(target: Target) -> tuple[int, int]:
    """(image generation calls, candidates) in the target's record."""
    entries = load_record(target)["generations"]
    return sum(int(e.get("image_generation_calls", 0)) for e in entries), sum(1 for e in entries if e.get("candidate"))


def check_batch(target: Target, batch: int, variants: list[str]) -> int:
    """Refuses a batch before any codex run; returns the calls already used."""
    if not 1 <= batch <= target.batch_max:
        raise SpriteError(f"--batch must be 1..{target.batch_max} (got {batch})")
    if len(variants) > batch:
        raise SpriteError(f"{len(variants)} variants for a batch of {batch}; give at most one per run")
    used, _ = calls_used(target)
    if used + batch > target.cap:
        raise SpriteError(f"{target.label} already used {used} of {target.cap} image generations; "
                          f"a batch of {batch} would exceed the cap; stop and ask the user")
    return used


def reserve_candidates(target: Target, n: int) -> list[Path]:
    """The next n free candidate files, picked by the parent before any run starts,
    so concurrent runs never share a number. A number recorded in the record
    stays taken even if its file is gone."""
    recorded = {g.get("candidate") for g in load_record(target)["generations"]}
    reserved, k = [], 1
    while len(reserved) < n:
        name = f"{target.prefix}_{k}.png"
        taken = name in recorded or any((d / name).exists() for d in [target.out_dir, *target.keep_dirs])
        if not taken:
            reserved.append(target.out_dir / name)
        k += 1
    return reserved


def candidate_number(path: Path) -> int:
    return int(path.stem.rsplit("_", 1)[1])


def _result_png(record: dict) -> tuple[bytes | None, bool]:
    """(PNG bytes, broken) of a generation record: the picture a completed record carries in `result`."""
    result = record.get("result")
    if record.get("status") != "completed" or not isinstance(result, str) or not result:
        return None, False
    try:
        data = base64.b64decode(result, validate=True)
    except (binascii.Error, ValueError):
        return None, True
    return (data, False) if data.startswith(PNG_SIGNATURE) else (None, True)


def count_image_calls(stdout_text: str, home: Path) -> dict:
    """Counts image generations of one codex exec run and picks the run's picture.

    codex-cli (0.162 to 0.167) does not emit an image item in `exec --json` (its
    item types are agent_message, reasoning, command_execution, file_change,
    mcp_tool_call, web_search, todo_list), so the --json output gives the
    thread id and the count comes from that thread's session rollout and its
    generated_images folder. The rollout records a generation either as an
    `image_generation_call` response item (older codex) or, since 0.162.1, as an
    `item_completed` event whose item is an `Extension` of kind
    `image_gen.generation`. Each shape is counted by unique id. Any image item
    that does show up in --json is counted too; the largest count wins so a
    looping session is never under-counted.

    The picture ("image", bytes or None) is the first completed generation
    record's `result`, so nothing the model saved or copied is read. The first,
    because the prompt asks for one call: a model that calls the tool again to
    "hand over" its picture draws a different one from a throwaway prompt (a
    labelled sprite sheet came back that way), and that later picture is not
    what was asked for. A record whose result is not a PNG is skipped and
    counted in "broken_results". When no record carries bytes, the thread's own
    first generated_images file is used.

    "tool_calls" counts the session's calls to the image tool themselves (a
    `custom_tool_call` whose input names image_gen). A call may fail without a
    generation, so it is not added to the count; run_batch charges one call
    for a run that shows such a call and nothing else, so a new record shape
    for generations cannot make runs free.
    """
    thread_id, json_ids = None, set()
    for line in stdout_text.splitlines():
        line = line.strip()
        if not line.startswith("{"):
            continue
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        if event.get("type") == "thread.started":
            thread_id = event.get("thread_id")
        item = event.get("item")
        if isinstance(item, dict) and "image_gen" in str(item.get("type", "")):
            json_ids.add(item.get("id") or f"anon-{len(json_ids)}")
    call_ids, extension_ids, rollout_file, generated = set(), set(), None, []
    record_images, broken = {}, set()  # by generation id
    seen = {}  # generation ids in the order they first appear, whatever their state
    tool_calls = 0
    if thread_id:
        matches = sorted((home / "sessions").glob(f"*/*/*/rollout-*{thread_id}.jsonl"))
        if matches:
            rollout_file = matches[-1]
            for line in rollout_file.read_text(encoding="utf-8", errors="replace").splitlines():
                try:
                    rec = json.loads(line)
                except json.JSONDecodeError:
                    continue
                payload = rec.get("payload") or {}
                generation = None
                if (rec.get("type") == "response_item" and payload.get("type") == "custom_tool_call"
                        and "image_gen" in str(payload.get("input", ""))):
                    tool_calls += 1
                if rec.get("type") == "response_item" and payload.get("type") == "image_generation_call":
                    generation = payload
                    call_ids.add(payload.get("id") or f"anon-{len(call_ids)}")
                item = payload.get("item") if rec.get("type") == "event_msg" else None
                if (payload.get("type") == "item_completed" and isinstance(item, dict)
                        and item.get("type") == "Extension" and item.get("kind") == "image_gen.generation"):
                    generation = item
                    extension_ids.add(item.get("id") or f"anon-{len(extension_ids)}")
                if generation is not None:
                    key = generation.get("id") or f"anon-{len(seen)}"
                    seen.setdefault(key)
                    data, is_broken = _result_png(generation)
                    if data is not None:
                        record_images[key] = data
                    elif is_broken:
                        broken.add(key)
        gen_dir = home / "generated_images" / thread_id
        if gen_dir.is_dir():
            # File names are random ids, so the order (and with it "the first one") comes from the write time.
            generated = sorted((p for p in gen_dir.iterdir() if p.suffix.lower() == ".png"),
                               key=lambda p: (p.stat().st_mtime_ns, p.name))
    # The two rollout shapes may describe the same generations, so they are not added up.
    rollout_calls = max(len(call_ids), len(extension_ids))
    if record_images:
        # The earliest generation that has a good copy, in the order the generations first appeared.
        image, source = next(record_images[k] for k in seen if k in record_images), "session record"
    elif generated:
        image, source = generated[0].read_bytes(), "generated_images"
    else:
        image, source = None, None
    return {
        "thread_id": thread_id,
        "json_items": len(json_ids),
        "rollout_calls": rollout_calls,
        "rollout_file": str(rollout_file) if rollout_file else None,
        "generated_files": [str(p) for p in generated],
        "calls": max(len(json_ids), rollout_calls, len(generated)),
        "image": image,
        "image_source": source,
        "broken_results": len(broken - record_images.keys()),  # a generation with a good copy was not skipped
        "tool_calls": tool_calls,
    }


def _run_codex(ctx: Context, name: str, cmd: list[str], references: list[Path], prompt: str) -> dict:
    """One codex exec in its own temp working dir. Never raises: a failure is returned as `error`."""
    gen = ctx.project["generation"]
    # Outside the project so its AGENTS.md never reaches the prompt.
    workdir = Path(tempfile.mkdtemp(prefix=f"dotomata-gen-{name}-"))
    args = cmd + ["exec", "--skip-git-repo-check", "-c", "project_doc_max_bytes=0", "--json",
                  "-m", gen["model"], "-c", f"model_reasoning_effort={gen['effort']}",
                  "--sandbox", "workspace-write", "-C", str(workdir)]
    if references:
        args += ["-i", *(str(r.resolve()) for r in references)]
    # No positional prompt: -i takes several values and would swallow it. Without
    # one, codex exec reads the prompt from stdin.
    error, stdout = None, ""
    try:
        proc = subprocess.run(args, input=prompt, capture_output=True, text=True, cwd=workdir,
                              timeout=gen["timeout_seconds"])
        stdout = proc.stdout
        if proc.returncode != 0:
            error = f"codex exited {proc.returncode}: {proc.stderr.strip()[-400:]}"
    except subprocess.TimeoutExpired as e:
        stdout = (e.stdout or b"").decode("utf-8", "replace") if isinstance(e.stdout, bytes) else (e.stdout or "")
        error = f"codex timed out after {gen['timeout_seconds']} s"
    except OSError as e:
        error = f"codex could not run: {e}"
    (workdir / "codex_events.jsonl").write_text(stdout, encoding="utf-8")
    return {"workdir": workdir, "stdout": stdout, "error": error}


RECORD_GLOB = "*generation.json"  # generation.json, art_generation.json, bg_generation.json and any caller kind's


def recorded_shas(ctx: Context) -> dict[str, str]:
    """sha256 -> where it is recorded, over every generation record of every kind and owner."""
    known = {}
    for path in sorted(ctx.source_dir.rglob(RECORD_GLOB)):
        for g in read_json(path).get("generations", []):
            if g.get("output_sha256"):
                known.setdefault(g["output_sha256"], f"{display_path(ctx, path)} ({g.get('candidate')})")
    return known


def display_path(ctx: Context, path: Path) -> str:
    try:
        return path.resolve().relative_to(ctx.root.resolve()).as_posix()
    except ValueError:
        return str(path)


def run_batch(ctx: Context, target: Target, prompts: list[str], run_variants: list[str | None],
              references: list[Path], ref_record: dict | None, extra_fields: dict | None = None) -> dict:
    """Runs one codex execution per prompt at once and records every run in the
    target's record (only this process writes it, after every run has finished).
    extra_fields are kind-specific record keys, placed before `error`.
    Returns {"runs": [...], "first": N, "last": M}."""
    for prompt in prompts:
        check_banned(ctx, target.label, prompt)
    gen = ctx.project["generation"]
    cmd = codex_command(ctx)
    try:
        version = subprocess.run(cmd + ["--version"], capture_output=True, text=True, timeout=60).stdout.strip()
    except FileNotFoundError as e:
        raise SpriteError(f"codex command not found ({cmd[0]}); install codex or set generation.codex_command") from e
    targets = reserve_candidates(target, len(prompts))
    first, last = candidate_number(targets[0]), candidate_number(targets[-1])
    name = target.header.get("unit") or target.header.get("background") or "x"
    with ThreadPoolExecutor(max_workers=len(prompts)) as pool:
        runs = list(pool.map(lambda p: _run_codex(ctx, name, cmd, references, p), prompts))

    results = []
    target.out_dir.mkdir(parents=True, exist_ok=True)
    # Read after every run has finished. Across processes this is best effort: it sees records
    # that other processes finished writing before this point, not batches still running.
    known = recorded_shas(ctx)
    for run, prompt, variant, path in zip(runs, prompts, run_variants, targets):
        counted = count_image_calls(run["stdout"], codex_home())
        image = counted.pop("image")  # bytes; the rest of `counted` is reported as it is
        had_image = image is not None
        notes = []
        if counted["broken_results"]:
            notes.append(f"skipped {counted['broken_results']} generation record(s) whose result is not a PNG")
        if had_image and counted["calls"] > 1:
            notes.append("the run generated more than once; kept the first complete picture")
        note = "; ".join(notes) or None
        digest = hashlib.sha256(image).hexdigest() if had_image else None
        refusal = None
        if digest in known:
            refusal = f"duplicate image: sha256 {digest} is already recorded in {known[digest]}"
            image = None
        calls = counted["calls"]
        if had_image or run["error"] or counted["rollout_file"] is None or counted["tool_calls"]:
            # An image (even a refused one) means at least one call. Without one, the
            # session record is the only proof that nothing was generated; if codex
            # failed, that record is missing, or it shows a call to the image tool with
            # no generation this code knows, charge one call so the cap cannot be bypassed.
            # A file the model left in its working folder proves nothing and is never read.
            calls = max(calls, 1)
        entry = {
            "candidate": None,
            "date": datetime.date.today().isoformat(),
            "codex_version": version,
            "model": gen["model"],
            "effort": gen["effort"],
            "batch": f"{first}-{last}",
            "variant": variant,
            "prompt": prompt,
            "reference": ref_record,
            "image_generation_calls": calls,
            "output_sha256": None,
            **(extra_fields or {}),
            "note": note,
            "error": "; ".join(e for e in (run["error"], refusal) if e) or None,
        }
        if image is not None:
            with open(path, "xb") as f:  # "x": an existing candidate is never overwritten
                f.write(image)
            entry["candidate"] = path.name
            entry["output_sha256"] = digest
            known[entry["output_sha256"]] = f"this batch ({path.name})"
        elif not entry["error"]:
            entry["error"] = "codex finished without an image"
        results.append({"entry": entry, "workdir": str(run["workdir"]), "counted": counted,
                        "candidate": str(path) if entry["candidate"] else None})

    write_record(target, [r["entry"] for r in results])
    return {"runs": results, "first": first, "last": last}


def mark_invalid(target: Target, name: str, reason: str) -> dict:
    """Marks a recorded candidate as not a usable result of its run (it stays in the
    record and in the count) and removes its preview file so it cannot be adopted."""
    if not reason.strip():
        raise SpriteError("marking a candidate invalid needs a reason")
    data = load_record(target)
    entries = [g for g in data["generations"] if g.get("candidate") == name]
    if not entries:
        raise SpriteError(f"{target.label}: {name} is not in {target.record.name}")
    if entries[-1].get("invalid"):
        raise SpriteError(f"{target.label}: {name} is already marked invalid: {entries[-1]['invalid']}")
    kept = [d / name for d in target.keep_dirs if (d / name).is_file()]
    if kept:
        raise SpriteError(f"{target.label}: {name} is adopted ({kept[0]}); adopt another candidate first")
    entries[-1]["invalid"] = reason.strip()
    _write_record_file(target.record, data)
    (target.out_dir / name).unlink(missing_ok=True)
    return entries[-1]


def write_record(target: Target, new_entries: list[dict]) -> None:
    data = load_record(target)
    data["generations"] += new_entries
    target.record.parent.mkdir(parents=True, exist_ok=True)
    _write_record_file(target.record, data)


def _write_record_file(path: Path, data: dict) -> None:
    # Another process's run_batch reads every record (recorded_shas), so a record is
    # never seen half written: write a temp file beside it, then swap it in. The temp
    # name ends in .tmp, so RECORD_GLOB never picks it up.
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_bytes(json_bytes(data))
    os.replace(tmp, path)


def write_sheet(ctx: Context, folder: Path, entries: list[dict], out: Path, panel_height: int,
                label: Callable[[dict], str], long_side: int | None = None, jpeg_quality: int | None = None) -> Path:
    """Candidates of one batch side by side at one display height, each labelled
    with its file name and one more line. With long_side, the whole sheet is
    scaled down to fit; with jpeg_quality, it is saved as JPEG."""
    bg = parse_hex(ctx.project["preview"]["background"], "preview background")
    font = ImageFont.load_default(size=16)
    label_h, gap = 48, 16
    panels = []
    for e in entries:
        img = None
        if e["candidate"]:
            try:
                with Image.open(folder / e["candidate"]) as im:
                    img = im.convert("RGBA")
            except OSError:  # an unreadable candidate gets an empty panel, like a failed run
                img = None
        if img is not None:
            width = max(1, round(img.width * panel_height / img.height))
            img = img.resize((width, panel_height), Image.NEAREST if panel_height >= img.height else Image.LANCZOS)
        else:
            img = Image.new("RGBA", (panel_height * 2 // 3, panel_height), bg + (255,))
        lines = [e["candidate"] or "no image", label(e)[:48]]
        panels.append((img, lines, max([img.width] + [math.ceil(font.getlength(t)) for t in lines])))
    sheet = Image.new("RGB", (gap + sum(w + gap for _, _, w in panels), gap + panel_height + label_h), bg)
    draw = ImageDraw.Draw(sheet)
    x = gap
    for img, lines, w in panels:
        sheet.paste(img, (x, gap), img)
        for i, line in enumerate(lines):
            draw.text((x, gap + panel_height + 4 + 20 * i), line, fill=(235, 235, 235), font=font)
        x += w + gap
    if long_side is not None and max(sheet.size) > long_side:
        scale = long_side / max(sheet.size)
        sheet = sheet.resize((max(1, round(sheet.width * scale)), max(1, round(sheet.height * scale))), Image.LANCZOS)
    out.parent.mkdir(parents=True, exist_ok=True)
    if jpeg_quality is not None:
        sheet.save(out, format="JPEG", quality=jpeg_quality)
    else:
        sheet.save(out)
    return out
