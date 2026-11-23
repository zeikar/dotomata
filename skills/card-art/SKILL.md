---
name: card-art
description: This skill should be used when the user wants to make, redo or replace a character's card illustration (a 2:3 full-body portrait with a bust crop) or a 16:9 background picture with dotomata, for example "make card art for my knight", "/dotomata:card-art knight", "draw a portrait of the slime for its card", "make a forest battle background", "/dotomata:card-art backgrounds/forest_path", or recolour a variant unit's art. It runs one batch, shows the comparison sheet, records the user's pick and builds the outputs, all in the main conversation.
argument-hint: "<unit> | backgrounds/<id>"
---

# Make card art or a background (dotomata)

The procedure and file formats are in `${CLAUDE_PLUGIN_ROOT}/docs/card-art.md`; read the "Card art" or "Backgrounds" section when starting. This skill adds only the conversation around it: show the comparison sheet, record the user's pick, build. It is one segment with one decision, so no agent is dispatched.

Argument: a unit's folder path under the source folder (e.g. `knight`), or `backgrounds/<id>`. If missing, ask.

## Before the first command (once per session)

Run every command from the user's project root as:

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
- No `codex`, not logged in, or `image_generation` not `true`: generation needs the Codex CLI logged in with a ChatGPT plan that includes Codex image generation. Tell the user what is missing (`npm install -g @openai/codex`, `codex login`, `codex features enable image_generation`) and stop before any `generate`.
- No `dotomata.json`: offer to run `DOTOMATA init`. Do not write it without the user's yes. Read `paths` in `dotomata.json` for the source, preview and output folders.

## Card art (a unit)

1. If `<source>/<unit>/unit.json` does not exist, write it with the user: `id` (same as the folder name), `kind`, `name` (English), `colors` (main colours, `#rrggbb`) and `design` (English, no name: silhouette, 3 to 5 main colours, one signature item, what sets it apart from their other characters; no names of existing games, studios or artists). Leave `actions` out unless a sprite follows; the sprite skill adds them.
2. `DOTOMATA art status <unit>`: generation count, cap (`art.cap_per_unit`), current adoption.
   - `recolor` true: never generated and never given a `dot_ref.png`. The base unit (`base` in `recolor.json`) needs an adoption and a bust first; if not, stop and say to do the base unit first. If `recolor_rule` is false, write `recolor.json` in the format from the docs, choosing the hue range and target with the user. Then `art build <unit> --force`, show the result beside the base unit's art, and stop.
3. Read `design`. If the signature item is unclear, ask the user whether to sharpen it. If `dot_reference` is true (the unit's sprite is built), run `art dot-ref <unit>` unless a current `dot_ref.png` exists (`art generate` says when it is missing or stale).
4. `DOTOMATA art generate <unit> --batch 3`, with `--variant` per run when the user wants specific differences. After a rejection, pass the user's words as `--note`. At the cap, stop and ask; never raise the cap or edit the record.
5. Open the comparison sheet (`<source>/<unit>/art/art_batch_*.jpg`) with Read and give its path. To see a candidate larger, open `<preview>/<unit>/art/art_candidate_N.png`. Check together: no resemblance to a known character or trademark; with a sprite, the same person; the signature item as described; no text or frame; a clear face for the bust crop.
6. When the user picks: `DOTOMATA art adopt <unit> --candidate art_candidate_N.png --note "<the user's words, short>"`. Then propose a bust square with the face in the middle (at least `art.bust_min_side` px, inside the picture), show that crop, and record it with `DOTOMATA art bust <unit> --rect X Y W H`. Redo it if the user wants it moved.
7. `DOTOMATA art build <unit> --force`, then show where the outputs are (`<art_output>/<unit id>/`).

## Backgrounds

1. If `<source>/backgrounds/<id>/background.json` does not exist, write it with the user: `id` (same as the folder), `layout` (a key of `bg.layouts`: `battle` for a side-view stage characters stand on, `backdrop` for a calm menu backdrop, or one the user adds), `colors` (first one for the placeholder) and `design` (the scene in English).
2. `DOTOMATA bg status <id>`, then `DOTOMATA bg generate <id> --batch 3`.
3. Show the comparison sheet (`<source>/backgrounds/<id>/bg_batch_*.jpg`). For `battle`, check the ground band is flat and calm where characters will stand, with little detail behind them and large shapes kept to the sides.
4. When the user picks: `DOTOMATA bg adopt <id> --candidate bg_candidate_N.png --note "<the user's words>"`, then `DOTOMATA bg build <id> --force`.

## Guardrails

- Adopt only what the user picked. Never run `art adopt` or `bg adopt` before they choose.
- Report each run's `note` and `error` (more than one generation, a duplicate image, no image) to the user. A candidate marked `invalid` cannot be adopted even if it is on the sheet. If a candidate looks like another unit's picture, compare sha256 values and mark it with `art invalidate` / `bg invalidate` as the docs describe.
- Do not commit on the user's behalf unless they ask. When they do, commit the source folder's adopted file, records, comparison sheet and `dot_ref.png`, and the outputs; never the preview folder.
- At the end, `DOTOMATA verify <unit>` (or `verify backgrounds/<id>`) must pass.
