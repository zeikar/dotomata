"""Stand-in for the codex CLI used by the selftest. It never generates anything.

Every run gets its own thread id, and the image it "generates" is
FAKE_CODEX_IMAGE with a text chunk naming that thread inserted, so the pixels
are the fixture's and the bytes (sha256) differ per run, like real generations.
FAKE_CODEX_FIXED=1 writes the fixture's exact bytes instead (duplicate tests).

Scenario (FAKE_CODEX_SCENARIO):
  rollout   prints exec_events_rollout.jsonl, writes rollout_two_calls.jsonl (no
            image bytes in it) as the thread's session log and leaves the image
            only in generated_images/
  record    prints exec_events_rollout.jsonl and writes rollout_extension_two_calls.jsonl
            as the session log with image bytes in both generation records: the
            run's image first, a later picture second. generated_images/ holds a
            third picture, so the three sources can be told apart
  recordbroken  like record, but the first record's result is not a PNG
  callonly  prints exec_events_rollout.jsonl and writes rollout_extension_two_calls.jsonl
            without its generation records: the session log shows a call to the
            image tool and no generation, and there is no image anywhere
  json      prints exec_events_json_item.jsonl (image items in --json), leaves the
            image in generated_images/<thread>/ and saves the same bytes to
            ./candidate.png in the working directory (a file nothing reads)
  badpng    like json, but the image is not one (a truncated file) in both places
  foreign   like json, but ./candidate.png is FAKE_CODEX_FOREIGN_IMAGE (a file
            copied from another run's folder)
  untraced  prints exec_events_rollout.jsonl, writes a session log with no
            generation and nothing in generated_images/, and saves the image to
            ./candidate.png (a file nothing reads, so the run has no image)
  noimage   prints exec_events_rollout.jsonl and exits 0 with no image and no
            session log, so the count cannot be confirmed
FAKE_CODEX_SLEEP (seconds) holds each run open, so concurrent runs overlap.
FAKE_CODEX_FAIL_ON: a run whose prompt contains this text exits 1 with no output.
Every exec call appends one line to FAKE_CODEX_LOG with its args, prompt and
start/end times.
"""
import base64
import json
import os
import shutil
import sys
import time
import uuid
import zlib
from pathlib import Path

HERE = Path(__file__).resolve().parent


def tagged_png(data: bytes, tag: str) -> bytes:
    """data with a tEXt chunk inserted after IHDR; everything else stays byte for byte."""
    body = b"tEXt" + b"fake-run\0" + tag.encode()
    chunk = (len(body) - 4).to_bytes(4, "big") + body + zlib.crc32(body).to_bytes(4, "big")
    end_of_ihdr = 8 + 12 + int.from_bytes(data[8:12], "big")
    return data[:end_of_ihdr] + chunk + data[end_of_ihdr:]


def main() -> int:
    args = sys.argv[1:]
    if args == ["--version"]:
        print("codex-cli 0.0.0-selftest")
        return 0
    if not args or args[0] != "exec":
        print(f"fake codex: unexpected args {args}", file=sys.stderr)
        return 2
    workdir = Path(args[args.index("-C") + 1])
    prompt = sys.stdin.read()
    start = time.time()
    time.sleep(float(os.environ.get("FAKE_CODEX_SLEEP", "0")))
    with open(os.environ["FAKE_CODEX_LOG"], "a", encoding="utf-8") as log:
        log.write(json.dumps({"args": args, "prompt": prompt, "start": start, "end": time.time()}) + "\n")
    fail_on = os.environ.get("FAKE_CODEX_FAIL_ON")
    if fail_on and fail_on in prompt:
        print("fake codex: failing on purpose", file=sys.stderr)
        return 1
    scenario = os.environ["FAKE_CODEX_SCENARIO"]
    home = Path(os.environ["CODEX_HOME"])
    from_record = scenario in ("record", "recordbroken")
    events = HERE / ("exec_events_rollout.jsonl"
                     if from_record or scenario in ("rollout", "noimage", "untraced", "callonly")
                     else "exec_events_json_item.jsonl")
    text = events.read_text(encoding="utf-8")
    fixture_thread = json.loads(text.splitlines()[0])["thread_id"]
    thread = fixture_thread[:-12] + uuid.uuid4().hex[:12]
    text = text.replace(fixture_thread, thread)
    fixture = Path(os.environ["FAKE_CODEX_IMAGE"]).read_bytes()
    image = fixture if os.environ.get("FAKE_CODEX_FIXED") == "1" else tagged_png(fixture, thread)
    if scenario == "badpng":
        image = b"\x89PNG truncated " + thread.encode()
    own = home / "generated_images" / thread
    if scenario in ("rollout", "json", "badpng", "foreign") or from_record:
        own.mkdir(parents=True)
        (own / "ig_fixture_2.png").write_bytes(tagged_png(fixture, thread + "-folder") if from_record else image)
    session = home / "sessions" / "2026" / "10" / "16"
    if from_record:
        session.mkdir(parents=True, exist_ok=True)
        rollout = (HERE / "rollout_extension_two_calls.jsonl").read_text(encoding="utf-8")
        placeholder = '"result":"iVBORw0KGgo="'
        first = b"not a png" if scenario == "recordbroken" else image
        for data in (first, tagged_png(fixture, thread + "-later")):
            rollout = rollout.replace(placeholder, '"result":"%s"' % base64.b64encode(data).decode(), 1)
        (session / f"rollout-2026-10-16T00-00-00-{thread}.jsonl").write_text(rollout, encoding="utf-8")
    if scenario == "callonly":
        session.mkdir(parents=True, exist_ok=True)
        lines = (HERE / "rollout_extension_two_calls.jsonl").read_text(encoding="utf-8").splitlines(keepends=True)
        (session / f"rollout-2026-10-16T00-00-00-{thread}.jsonl").write_text(
            "".join(line for line in lines if '"kind":"image_gen.generation"' not in line), encoding="utf-8")
    if scenario in ("rollout", "untraced"):
        session.mkdir(parents=True, exist_ok=True)
        rollout = (HERE / "rollout_two_calls.jsonl").read_text(encoding="utf-8")
        # untraced keeps only the session_meta line: the log proves no generation happened.
        rollout = rollout if scenario == "rollout" else rollout.splitlines(keepends=True)[0]
        (session / f"rollout-2026-10-16T00-00-00-{thread}.jsonl").write_text(rollout, encoding="utf-8")
    if scenario in ("json", "badpng", "untraced"):
        (workdir / "candidate.png").write_bytes(image)
    elif scenario == "foreign":
        shutil.copyfile(os.environ["FAKE_CODEX_FOREIGN_IMAGE"], workdir / "candidate.png")
    sys.stdout.write(text)
    return 0


if __name__ == "__main__":
    sys.exit(main())
