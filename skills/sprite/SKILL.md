---
name: sprite
description: This skill should be used when the user wants to make, redo or continue an animated pixel art sprite (a game character or creature sprite sheet) with dotomata, for example "make a pixel sprite for my knight", "/dotomata:sprite slime", "turn this character into an animated sprite sheet", "redo the hero sprite with a new palette", or to continue a unit waiting at a sprite checkpoint. It runs the unit through dotomata's three segments with the sprite-artist agent and handles the checkpoints in the main conversation. The user decides checkpoint 1 (adopting a candidate) and checkpoint 3 (the final GIF); the main conversation makes checkpoint 1a (picking the design) and checkpoint 2 (the hand fixes) itself and records them, and never approves checkpoint 1 or 3 in the user's place.
argument-hint: "<unit>"
---

# Make a pixel sprite (dotomata)

The procedure, file formats and rules are in `${CLAUDE_PLUGIN_ROOT}/docs/sprites.md`; read it when starting. This skill adds only the conversation around it: it dispatches the `sprite-artist` agent once per segment, shows the results to the user and records the checkpoints.

Argument: the unit's folder path under the source folder (e.g. `knight`). If missing, ask the user which unit, or what character they want.

## Before the first command (once per session)

The CLI is the Python project at the plugin root. Run every command from the user's project root as:

```sh
uv run --project "${CLAUDE_PLUGIN_ROOT}" dotomata <command> ...
```

Resolve the plugin root to an absolute path once (`echo "${CLAUDE_PLUGIN_ROOT}"`) and call that full invocation `DOTOMATA` below. Then check the prerequisites:

```sh
uv --version
codex --version
codex login status
codex features list | grep -E '^image_generation[[:space:]]'
test -f dotomata.json && echo "config found"
```

- No `uv`: point the user to https://docs.astral.sh/uv/ and stop.
- No `codex`, not logged in, or `image_generation` not `true`: generation needs the Codex CLI logged in with a ChatGPT plan that includes Codex image generation. Tell the user what is missing (`npm install -g @openai/codex`, `codex login`, `codex features enable image_generation`) and stop before any `generate`. Commands that do not generate (convert, fix, parts, build, verify, checkpoint) still work.
- No `dotomata.json`: offer to run `DOTOMATA init`, which writes the default config and a `.gitignore` for the preview folder. Do not write it without the user's yes. Read `paths` in `dotomata.json` to know where units live (`<source>/<unit>/`, previews in `<preview>/<unit>/`).

## Start

1. `DOTOMATA checkpoint <unit>` shows generation count, cap and the checkpoint records. (If `<source>/<unit>/unit.json` does not exist yet, segment 1 starts by writing it.)
2. Its `approved` field (the approvals that still count) decides the next segment. Redoing checkpoint 1 voids the earlier approvals of 2 and 3.
   - Checkpoint 1 not approved: segment 1. The status shows only the latest record per checkpoint, so read the full history in `<source>/<unit>/checkpoints.json` (`entries`, oldest first). If there is a 1a record (a checkpoint 1 rejection whose note starts with `Design pick, size pass next`) and no checkpoint 1 approval after it, run the size pass with that record's candidate, even when the user has rejected size-pass candidates since; otherwise the design pass. If the user's rejection asks for another design, run the design pass instead.
   - Only checkpoint 1 approved: segment 2.
   - Checkpoints 1 and 2 approved: segment 3.
   - All three approved: say it is done and ask whether to redo anything.
   - A palette-swap unit (`palette_swap` in `unit.json`) has only segment 3.
3. For a new unit, agree with the user on what the agent needs for `unit.json`: kind (humanoid, beast, slime, boss, or a flyer rig), equipment, the big attack (for a humanoid: `sword_slash`, `shield_bash`, `staff_cast` or `bow_shot`), main colours and the design. If the user gave concept art, a reference image or card art, note its path.

## Running a segment

- Dispatch the `sprite-artist` agent of this plugin (`dotomata:sprite-artist`) once per segment. Its prompt must contain:
  - the exact CLI invocation (`DOTOMATA` with the absolute plugin root) and the absolute path of `docs/sprites.md`;
  - the project root (absolute) and the unit path;
  - the segment number, and for segment 1 whether it is the design pass or the size pass (with the candidate picked at 1a and any `--height`);
  - the user's words from the last checkpoint (rejection reasons, things to fix), verbatim; for a new unit, the agreed `unit.json` contents;
  - for a redo, what changed (pass every cue that applies): "checkpoint 1 was redone" (segment 2 converts again with `--force` and starts the hand fixes over; segment 3 re-inits the mask with `--force`) and/or "rebuild of existing outputs" (segment 3 whenever the unit already has built outputs: `build --force`).
- **Design pass.** A unit with adopted card art (`<source>/<unit>/art/art.json`) uses `--from-art` (usually `--batch 3 --from-art`); `generate` refuses a design pass without a reference for it. A unit without card art runs without a reference image: the height is fixed by the size pass, and a scale reference makes a new character copy the reference's face. Tell the agent to write in `design` what sets this character apart from the user's other characters.
- **Checkpoint 1a** is yours. Open the design candidate sheet (and the card art, if any) with Read and pick the candidate closest to the card art and `design` in hair, face, outfit, equipment, colours and signature item. Do not ask the user; go straight to the size pass. Record it, if the pick does not already `PASS`, as `DOTOMATA checkpoint <unit> --record 1 --reject --candidate candidate_N.png --note "Design pick, size pass next. Agent check: <what you compared and why this one>"`, then dispatch segment 1 again as the size pass with that candidate. If the figure holds a weapon or staff above its head, give the agent the whole-figure height for `--height` so that head to foot comes out at the target. If the pick already `PASS`es and is good to use, skip the size pass and show it at checkpoint 1.
- **Size pass.** The agent runs `reduce` and one `--design-from-file` batch (3 runs with the three cleanup variants from the docs). Candidates that pass go to checkpoint 1.
- **Size pass keeps failing** (a rerun still gives no passing or usable candidate): offer the fallback from the docs ("When the size pass keeps failing"): adopt the `ref_*.png` that `reduce` wrote as the unit's source. Show it with Read and say plainly that its height and design are exact but code shrinking blurs the face and small details, so segment 2 needs more hand fixes. The choice is the user's: do not choose it for them and do not recommend it first.
- To compare more options at once, give the agent a batch size (`--batch N`, up to `generation.batch_max`) and one `--variant` per run. A batch is still one `generate` call; every run counts toward the cap.
- A reference image the user prepared goes in with `--design-from-file`; the agent copies it into the unit folder as `ref_*.png`.
- When the agent returns, check the file paths in its report. The agent never asks the user anything and never commits.

## Showing results

- Open images (candidates, comparison sheets, `fixed_zoom.png`, `<id>_zoom.png`) with Read so they appear in the conversation, and give their paths. Give the GIF's path for the user to open.
- Give the numbers: segment 1: measured height and target, height check, generations this time and in total, the cap. For a batch, show the comparison sheet first, then each passing candidate. Do not show `FAIL` candidates, except in the design pass, where they are shown as designs with their measured height. In a size pass, also show `ref_*.png`. For a `REVIEW` candidate, say the measured height includes the held weapon, staff or bow and ask the user to judge head to foot. Segment 2: fixed cells and their share, per category. Segment 3: the report's checks, newly drawn cells, generations used.
- **Checkpoint 1:** also show the design candidate sheet and say which design you picked at 1a and why; if the user prefers another design, redo the size pass with it. Ask the user to check that the candidate does not resemble a known character or trademark and does not share face, hair or equipment with their other characters. For a unit with card art, show the art beside it and ask whether it reads as the same character (also at checkpoint 3).
- **Checkpoint 2** is yours. Open `<preview>/<unit>/fixed_zoom.png` and look at the face, outline, equipment, stray cells and the fixed-cell count. If something needs fixing, dispatch segment 2 again with what to fix; otherwise record the approval and dispatch segment 3.
- **Checkpoint 3:** show the final GIF together with segment 2's `fixed_zoom.png` and its fixed-cell count and share. If the user wants to fix cells themselves, they edit `<preview>/<unit>/fixed_edit.png` (transparent, enlarged) and hand it back; `fixed_zoom.png` has a solid background and cannot be read back. If the user names hand-fix problems, record checkpoint 2 as rejected and restart from segment 2.
- A palette-swap unit: show its GIF next to its base unit's GIF.

## Recording checkpoints

For checkpoints 1 and 3, record only after the user answers; the note is a short paraphrase of their words. For 1a and 2, which you make, the note says what you looked at and why you decided, plus anything the user said about it: a 1a note starts with `Design pick, size pass next. Agent check:` (the start of the next session reads that prefix), a checkpoint 2 note with `Agent check:`.

```sh
DOTOMATA checkpoint <unit> --record 1 --approve --candidate candidate_2.png --note "<reason>"
DOTOMATA checkpoint <unit> --record 1 --reject --candidate candidate_1.png --note "<reason>"
DOTOMATA checkpoint <unit> --record 1 --approve --reference ref_80px_x12.png --note "Size pass failed, reduced reference adopted: <user's reason>"
DOTOMATA checkpoint <unit> --record 2 --approve --note "Agent check: <what you looked at>"
DOTOMATA checkpoint <unit> --record 3 --approve --note "<reason>"
```

- After a rejection, dispatch the same segment again with the reason. Record rejections of 2 and 3 with `--reject` and pass the points to fix to the agent.
- Do not dispatch the next segment before its checkpoint is recorded.

## Guardrails

- The generation cap is `generation.cap_per_unit` per unit, at most `generation.batch_max` runs per `generate`. When `generate` refuses, stop and ask the user. Never raise the cap or edit `generation.json` down.
- No names of existing games, studios or artists in a prompt, `design`, `--note` or `--variant`.
- Never overwrite existing candidates or outputs. Use `--force` only on outputs the user decided to rebuild.
- Do not change the dotomata code while making a unit. If the tool blocks, report the problem to the user.
- Do not commit on the user's behalf unless they ask. When they do, the unit's source folder and its output folder are what to commit; never the preview folder.
- At the end, `DOTOMATA verify <unit>` must pass.
