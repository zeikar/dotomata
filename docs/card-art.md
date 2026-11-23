# Card art and backgrounds

dotomata also makes two kinds of painted pictures with the same generation runner as the sprites: **card art** (a 2:3 full-body portrait of a unit, plus a square bust crop) and **backgrounds** (16:9 scenes). They share the sprite pipeline's [generation guardrails](sprites.md#generation-guardrails) (one image per run, batches, variants, banned names, counting, provenance, caps counted per kind), [`verify`](sprites.md#reproduction) and the [command conventions](sprites.md#commands). Config keys are under `art`, `bg` and `comparison_sheet` in [config.md](config.md).

The image model's picture is used as is, without hand fixes, so both have a single decision: the person picks one candidate from a comparison sheet. They can generate more within the cap and adopt again; adoptions accumulate and the latest one is used.

## Card art

Card art belongs to a unit: it lives in `<source>/<unit>/art/` and uses the unit's `design` from `unit.json`. A unit can have card art only (no `actions`), a sprite only, or both. When it has both, the two are tied together:

- A unit with adopted card art draws its sprite from it (`generate --from-art`, see [art first](sprites.md#units-with-card-art-art-first)). For a new character, making the card art first is the recommended order.
- A unit whose sprite is already built sends a picture of that sprite (`dot_ref.png`) with every card art run as a design reference.

### Files

| Source (`<source>/<unit>/art/`) | Written by | Content |
|---|---|---|
| `art_candidate_<N>.png` | `art adopt` | The adopted candidate only. The others stay in `<preview>/<unit>/art/` and are not committed. |
| `art_generation.json` | `art generate` | Every candidate's prompt, references, generation count, sha256 and `note`, including discarded ones. `art invalidate` adds `invalid`. |
| `art_batch_<first>-<last>.jpg` | `art generate` | The batch's comparison sheet (small JPEG, `comparison_sheet`): the record of what was chosen from. |
| `art.json` | `art adopt`, `art bust` | Adoptions per stage; the last one is used. |
| `dot_ref.png` | `art dot-ref`, sprite `build` | The built sprite as a design reference. |
| `recolor.json` | you | Recolour units only (below). |

| Output (`<art_output>/<unit id>/`, written by `art build`) | Content |
|---|---|
| `<id>_<stage>_full.png` | The full picture: the adopted candidate's pixels as they are (no crop, no resampling). |
| `<id>_<stage>_bust.png` | The bust: the square from `art.json`, shrunk with LANCZOS to `art.bust_size`. |

- Both PNGs are RGB with fixed compression and no metadata. Generated images carry the generator's metadata chunks, so the full picture is written from the pixels rather than copied; the original stays in the source.
- The stage is `base`. `art adopt` and `art bust` take `--stage` for other stages (a different look later in a story, say); `--from-art` always uses `base`.
- `art.json`: `{"unit": "<id>", "stages": {"base": {"adoptions": [{"candidate": "art_candidate_1.png", "sha256": "...", "note": "the person's reason", "date": "...", "bust": {"x": ..., "y": ..., "width": ..., "height": ...}}]}}}`.
- The bust rectangle is in whole pixels of the adopted picture, chosen by a person so the face is in the middle. It must be inside the picture, square, and at least `art.bust_min_side` on a side; it is only ever shrunk.

**Recolour units.** A unit with `recolor.json` (or a sprite palette swap) gets its card art by turning one hue range of its base unit's adopted art. It is never generated, adopted or given a `dot_ref.png`; the base unit needs its adoption and bust first, and the bust rectangle is the base's.

```json
{"base": "<base unit path>", "hue_from": [70, 160], "hue_to": 280, "min_saturation": 20, "saturation": 100, "value": 100}
```

Pixels whose hue (degrees) is within `hue_from` and whose saturation is at least `min_saturation` percent are turned so the middle of the range lands on `hue_to`; differences within the range are kept. Every other pixel stays byte for byte. `saturation` and `value` are optional whole percentages (default 100, unchanged) that scale the turned pixels, capped at full; 0 or less is refused.

**Placeholders.** `art build` on a unit with neither `art.json` nor `recolor.json` writes a placeholder drawn in code: the first of the unit's `colors`, its id and English `name` in large text, and a bust cut at `art.placeholder_bust`. The text uses Pillow's built-in font, so its pixels match only on the same pinned version.

### Prompt

- Line 1 is `generation.tool_instruction`.
- Line 2 is `Image: ` + `art.common_prompt` + `art.composition_sentence` + `art.design_rule_sentence` + `design`. The sprite's `common_prompt`, size sentence and `sprite_note` are not used.
- Then: `--note` (`Changes from the previous candidate: ...`), `art.style_reference_sentence` if `art.style_reference` is set, `art.dot_reference_sentence` if the unit has a built sprite, and the i-th `--variant` (`For this candidate: ...`) on run i.
- Attached images: the style reference (if set; its path and sha256 go into the record's `style_reference`, and a missing file stops the run) and the unit's `dot_ref.png` (if it has a built sprite; recorded as `reference` with mode `dot`).
- Edit `art.common_prompt` to set your own card art style, or set `art.style_reference` to a picture in that style.

### Steps

1. `art status <unit>`: generation count and cap, current adoption, whether it is a recolour (`recolor`), whether its rule exists yet (`recolor_rule`), and whether it has a built sprite (`dot_reference`). For a recolour, write `recolor.json` if needed and go to step 6.
2. Check `design` in `unit.json`: is the signature item clear? If the unit has a built sprite, it needs a `dot_ref.png`: `art dot-ref <unit>` makes one the first time (idle frame 0, cropped to its opaque pixels, enlarged in `reduce.ref_block` blocks on a magenta margin, like the size-pass reference). After that, every sprite `build` refreshes it (commit the changed file too).
3. `art generate <unit> --batch 3 [--variant "..."]...`. A unit with a built sprite is refused if `dot_ref.png` is missing or older than the sheet. Candidates go to `<preview>/<unit>/art/`; the comparison sheet goes into the source. At the cap, stop and ask.
4. Show the comparison sheet and let the person pick. Check together: no resemblance to a known character or trademark; if it has a sprite, the same person; the signature item as described; no text or frame; a face suited to a bust crop. If nothing fits, put their words into `--note` and generate again.
5. `art adopt <unit> --candidate art_candidate_N.png --note "<the person's reason>" [--bust X Y W H]`. It checks the candidate is recorded, not `invalid`, matches its sha256 and is an opaque 2:3 picture, then copies it into the source. Set the bust here or later with `art bust <unit> --rect X Y W H`; without a bust, `art build` refuses.
6. `art build <unit> --force` (`--force` because it replaces the placeholder or the previous build).
7. To adopt again, generate more within the cap and repeat from step 5. The previous adopted file moves back to the preview folder and stays in the record. Set a new bust for the new adoption.

```sh
dotomata art status botanist
dotomata art dot-ref botanist          # only if the unit's sprite is already built
dotomata art generate botanist --batch 3 --variant "front view" --variant "slight turn to the left"
dotomata art adopt botanist --candidate art_candidate_2.png --note "<the person's reason>"
dotomata art bust botanist --rect 240 110 660 660
dotomata art build botanist --force
dotomata art invalidate botanist --candidate art_candidate_3.png --reason "<why it is not that run's picture>"
```

## Backgrounds

A background is a 16:9 picture defined by `<source>/backgrounds/<id>/background.json`. Generation, records, comparison sheet, adoption and `invalidate` work like card art, without a bust or stages.

| Source (`<source>/backgrounds/<id>/`) | Written by | Content |
|---|---|---|
| `background.json` | you | `id` (same as the folder), `layout` (a key of `bg.layouts`: `battle` or `backdrop` by default), `colors` (the first is the placeholder colour), `design` (the scene in English). |
| `bg_candidate_<N>.png` | `bg adopt` | The adopted candidate only; the others stay in `<preview>/backgrounds/<id>/`. |
| `bg_generation.json`, `bg_batch_*.jpg` | `bg generate` | As for card art. |
| `bg.json` | `bg adopt` | Adoptions; the last one is used. |

```json
{"id": "forest_path", "layout": "battle", "colors": ["#4a6b3c"], "design": "a sunlit forest path between tall birches, ferns along the edges"}
```

- The output is `<bg_output>/<id>.png`: the adopted image scaled with LANCZOS to cover `bg.size` (rounding up) and cropped in the middle, because the model never returns exactly that size. RGB, no metadata. Without an adoption, a placeholder in the first colour with the id.
- Prompt: `Image: ` + `bg.layouts[layout]` (with the description in place of `{design}`) + ` ` + `bg.common_prompt`, then `--note` and the variant. No reference images.
  - `battle`: a side view with a flat ground band across the lower part where small characters stand in a shallow diagonal, the middle of the ground kept simple, so sprites read clearly on it.
  - `backdrop`: a calm, evenly lit scene seen straight on, for menu screens that panels will mostly cover.
  - Add your own layouts in `bg.layouts`.
- Cap per background `bg.cap_per_background`, batch `bg.batch_max`.

```sh
dotomata bg status forest_path
dotomata bg generate forest_path --batch 3
dotomata bg adopt forest_path --candidate bg_candidate_1.png --note "<the person's reason>"
dotomata bg build forest_path --force
dotomata verify backgrounds/forest_path
```
