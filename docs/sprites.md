# Sprites

This is the reference for making an animated pixel sprite with dotomata: the files, the commands, the rules the code enforces and the workflow around them. Card art and backgrounds have their own page ([card-art.md](card-art.md)); every config key is in [config.md](config.md).

- [How it works](#how-it-works)
- [Running the commands](#running-the-commands)
- [Your first unit](#your-first-unit)
- [Folders and files](#folders-and-files)
- [`unit.json`](#unitjson)
- [Grid and part files](#grid-and-part-files)
- [Rigs and motion templates](#rigs-and-motion-templates)
- [The motion JSON](#the-motion-json)
- [Commands](#commands)
- [Convert](#convert)
- [Build checks](#build-checks)
- [Segments and checkpoints](#segments-and-checkpoints)
- [Generation guardrails](#generation-guardrails)
- [Reproduction](#reproduction)
- [Selftest](#selftest)
- [Following the workflow from any tool](#following-the-workflow-from-any-tool)

## How it works

- The image model does one thing: it draws the character's design as a single standing pose. Everything after that (the pixel grid, the palette, the motions, the sheet format and every check) is code.
- Everything an output is made from is a file in the source folder: the generated image, the grid, the hand-fix list, the part data. `verify` rebuilds every output from those files and compares it, pixel for pixel, with the one on disk. The same sources give the same pixels on any machine.
- Rigs, motion templates and equipment layers are shared data. A new unit reuses them, so a new character or a new piece of equipment costs a drawing, not new animation code.

The steps for one unit:

1. **Base pose.** `generate` asks the Codex CLI for one standing pose per run, on a flat magenta background. A size pass then redraws the chosen design at the target height.
2. **Grid and palette.** `convert` keys out the background, finds the pixel grid the model drew on and writes `grid.txt` and `palette.json`.
3. **Hand fixes.** Cell edits go in `fixes.json`; `grid.txt` itself is never edited.
4. **Parts.** `parts_mask.txt` assigns every cell to a part of the rig (head, torso, arms, legs, weapon...), and `parts.json` places the joints and any extra drawings.
5. **Motions.** Templates move the parts by whole pixels, swap in alternative drawings or replace the whole figure (the fallen pose). No rotation, no scaling: pixels stay crisp.
6. **Build.** `build` checks the result and writes the sheet, a white mask, a motion JSON, previews and a report.

## Running the commands

dotomata needs [uv](https://docs.astral.sh/uv/). Generation (`generate`, `art generate`, `bg generate`) also needs the [Codex CLI](https://github.com/openai/codex), logged in (`codex login status`) with image generation enabled (`codex features list` shows `image_generation` as `true`). Nothing else needs codex.

Run every command from your project root:

```sh
uv run --project /path/to/dotomata dotomata <command> ...   # a clone of the repo, or the installed plugin's folder
uvx --from git+https://github.com/zeikar/dotomata dotomata <command> ...   # without a clone
```

The examples below write `dotomata` for whichever you use. `dotomata --help` and `dotomata <command> --help` list every option. Global options: `--root DIR` (the project root; default the current directory) and `--config FILE` (default `<root>/dotomata.json`).

`dotomata init` writes the default config and a `.gitignore` that keeps the preview folder out of git. Paths below use the defaults: sources in `sprites/source/`, previews in `sprites/out/`, sprite outputs in `build/sprites/`.

## Your first unit

A walk-through for a humanoid with a staff, using the CLI directly. In Claude Code, the `sprite` skill runs the same steps for you and stops at the two points where you decide (see [Segments and checkpoints](#segments-and-checkpoints)).

**1. Describe the unit.** Create `sprites/source/botanist/unit.json`:

```json
{
  "id": "botanist",
  "kind": "humanoid",
  "name": "Botanist",
  "colors": ["#4f7d3a", "#c98b4b", "#f1d9b5"],
  "equipment": ["staff"],
  "design": "A young field botanist with short chestnut hair, a moss-green hooded coat over a cream shirt, brown boots and a leather satchel at the hip. She holds a tall wooden staff topped with a large glowing seed pod, her signature item.",
  "actions": {
    "idle": "humanoid_idle",
    "attack": "staff_cast",
    "hit": "humanoid_hit",
    "down": "humanoid_down"
  }
}
```

The folder name usually equals the `id`; a unit may also live in a subfolder (e.g. `heroes/botanist`), and outputs are still named by its `id`.

**2. Design pass.** Generate three candidates at once and pick the design you like, whatever its size:

```sh
dotomata generate botanist --batch 3
```

Candidates land in the unit folder (`candidate_1.png` ...); the comparison sheet is `sprites/out/botanist/batch_1-3.png`. Each candidate gets a height check (`PASS`, `REVIEW` or `FAIL`).

**3. Size pass.** The model draws on its own grid, so the height is usually off. Record the pick, shrink it to the target height in code, and have the model clean up that small version:

```sh
dotomata checkpoint botanist --record 1 --reject --candidate candidate_2.png --note "Design pick, size pass next: closest to the description"
dotomata reduce botanist --candidate candidate_2.png
dotomata generate botanist --batch 3 --design-from-file ref_80px_x12.png \
  --variant "Match the reference grid exactly: the sprite is 80 art pixels tall; every art pixel is a 12x12 square block in the output." \
  --variant "Keep the reference's block size and silhouette; only clean up and restore details. Do not resample to a finer grid." \
  --variant "Treat this as a pixel-art cleanup pass of a 34x80 sprite: same canvas of blocks, same pose, fix the face, outline and small metal details."
```

(`34` is the width from `ref_80px_x12.json`'s `grid_size`.) If a candidate already passes the height check in step 2, skip this step.

**4. Adopt** a candidate that passed (checkpoint 1):

```sh
dotomata checkpoint botanist --record 1 --approve --candidate candidate_5.png --note "Clean face, staff reads well"
```

**5. Convert and fix.** `convert` writes `grid.txt`, `palette.json` and `convert.json`; `fix` applies `fixes.json` and writes previews:

```sh
dotomata convert botanist
dotomata fix botanist
dotomata checkpoint botanist --record 2 --approve --note "Eye and outline fixed"
```

Look at `sprites/out/botanist/convert_zoom.png`, write cell edits into `sprites/source/botanist/fixes.json` (format below), run `fix` again and look at `fixed_zoom.png`. To paint fixes instead, edit `sprites/out/botanist/fixed_edit.png` in any pixel editor using only palette colours and run `dotomata fix botanist --from-png <edited.png> --category face --reason "..."`.

**6. Parts, motions, build.**

```sh
dotomata parts botanist --init
```

Replace every `?` in `parts_mask.txt` with a part symbol (`h` head, `t` torso, `f` front arm, `b` back arm, `l` legs, `T` staff), fill in the joints in `parts.json`, and draw the fallen pose as a grid file referenced from `variants.down`. Then:

```sh
dotomata parts botanist       # parts_zoom.png: every layer side by side
dotomata assemble botanist    # all frames and checks into the preview folder
dotomata build botanist       # writes build/sprites/botanist/
dotomata checkpoint botanist --record 3 --approve --note "Motions look right"
dotomata verify botanist
```

Open `build/sprites/botanist/botanist_preview.gif` to see every action.

## Folders and files

```
dotomata.json                     config (dotomata init)
sprites/source/<unit>/            sources of one unit: commit them
sprites/source/<unit>/art/        card art sources (card-art.md)
sprites/source/backgrounds/<id>/  background sources (card-art.md)
sprites/out/                      previews and unadopted card art and background candidates (ignored)
build/sprites/<unit>/             sprite outputs: point paths.output at your engine's assets
```

- Commit the sources and the outputs, including every generated image and record. No Git LFS is needed at these sizes.
- `<unit>` is the folder path under the source folder. File and frame names use the `id` from `unit.json`. Sprite outputs go under the folder path; card art output folders and `kinds.*.reference_unit` use the id.
- codex session logs are never kept in the project: each run works in its own temp folder outside it.
- Outputs contain no dates, absolute paths or tool versions, so they rebuild identically elsewhere.

| Source (`sprites/source/<unit>/`) | Written by | Content |
|---|---|---|
| `unit.json` | you | See [`unit.json`](#unitjson). |
| `candidate_<N>.png` | `generate` | Generated images. Never overwritten. |
| `generation.json` | `generate` | Every run: prompt, reference, generation count, sha256, height check. `checkpoint` adds adoption and reason; `recheck` rewrites the three height fields. |
| `ref_*.png`, `ref_*.json` | `reduce` | The size pass's design reference and its record (source candidate, settings, sha256). Commit them: generation records point at them. |
| `checkpoints.json` | `checkpoint` | Every checkpoint decision with its note and date. |
| `grid.txt`, `palette.json`, `convert.json` | `convert` | The converted grid, palette and the settings used. Never edited by hand. |
| `fixes.json` | you, `fix --from-png` | Hand fixes. |
| `parts.json`, `parts_mask.txt`, extra grid files | `parts --init`, then you | Parts, joints and the drawings motions need. |
| `palette_map.json` | you | Palette-swap units only: base colour to new colour. |

| Output (`build/sprites/<unit>/`, written by `build`) | Content |
|---|---|
| `<id>_sheet.png` | Transparent sheet: one row per action (`action_order`), one cell per frame. |
| `<id>_mask.png` | Same layout, white where the sheet is opaque. For hit flashes and tinted silhouettes. |
| `<id>_motion.json` | Where every frame is and how the actions play. See [The motion JSON](#the-motion-json). |
| `<id>_zoom.png` | The sheet enlarged on a solid background, for viewing. |
| `<id>_preview.gif` | Every action, idle looped first. |
| `<id>_report.json` | Colours, height, hand-fixed cells per category, newly drawn cells, generation count, check results. |

## `unit.json`

| Field | Required | Meaning |
|---|---|---|
| `id` | yes | Used in file and frame names. |
| `kind` | yes | A key of `kinds` in the config: `humanoid`, `beast`, `slime`, `boss` by default. Sets the cell, target height and default rig. |
| `rig` | no | A rig other than the kind's default, e.g. `"flyer"` for a flying creature. |
| `design` | for generation | The character in English, without a name: silhouette, 3 to 5 main colours, one signature item, and what sets it apart from your other characters (hair, outfit, equipment, build). The sprite and the card art share it, so leave out anything about the medium (pixel size, head count). `generate` and `art generate` refuse a unit without it. |
| `sprite_note` | no | Text only the sprite prompt needs (e.g. "reads as a round blob"); goes right before `design`, never into card art. |
| `restore_details` | no | Unit-specific details to bring back in the size pass (shield rivets, a staff crystal). Added as `Also restore: ...` with `--design-from-file` only. |
| `height` | no | This unit's own target height (whole art px, 1 up to the kind's foot y). Replaces the kind's height (and its reference unit) in the height check, `reduce`, the size sentence and the `--from-art` creature sentence. For a body that does not fit its kind's height. |
| `height_reference` | no | `"kind"`: always compare with the kind's `height`, never with its reference unit. |
| `equipment` | no | Equipment layers the unit carries, from its rig's `equipment` (humanoid: `weapon`, `shield`, `staff`, `bow`, `helmet`). Must match the mask: a listed layer needs cells, and cells need a listing. |
| `actions` | for a sprite | `{action: template}`. A unit without `actions` (and not a palette swap) has card art only; sprite `build` and `verify` skip it. |
| `overrides` | no | Per-action changes to a template, see below. |
| `airborne` | no | `{action: [frame indices]}` left out of the foot-row check (a jump). |
| `name` | for the card art placeholder | English name, or a key into `paths.strings`. |
| `colors` | for the card art placeholder | Main colours as `#rrggbb`; the placeholder uses the first. |
| `palette_swap` | palette swaps only | `{"base": "<unit path>", "map": "palette_map.json"}`. Such a unit has only `id`, `kind` (same as the base's) and this. |

Other fields (a description, your own metadata) are ignored.

**`overrides`** changes part of a template for one unit:

```json
"overrides": {
  "attack": {
    "frame_ms": 80,
    "strike_frame": 1,
    "frames": {
      "3": {"offsets": {"weapon": [0, 0]}, "swap": {"front_arm": "raise"}},
      "4": {"replace": null}
    }
  }
}
```

`offsets` and `swap` merge into the template frame layer by layer; `replace` sets or (with `null`) removes the whole-figure variant. Frame numbers must exist. The frame count and strike rules still apply after the override.

**`palette_map.json`** (palette swaps): `{"colors": {"#base_colour": "#new_colour", ...}}`. Every source colour must be in the base unit's palette; unlisted colours stay.

## Grid and part files

- **Grid files (`*.txt`):** one line per row, one character per cell, every line the same length. `.` is transparent; every other character is a palette symbol.
- **`palette.json`:** `{"colors": {"a": "#rrggbb", ...}, "merged_from": {...}}`. Symbols are `a-z`, `A-Z`, assigned dark to light. `merged_from` shows which k-means colours were merged into each symbol.
- **`fixes.json`:** `{"edits": [{"cells": [[x, y], ...], "color": "a", "reason": "why", "category": "face"}]}`. `color` is a palette symbol or `.` (erase). `category` is one of `fix_categories`. A cell edited several times counts once, under its last edit's category, and only if it ends up different from the converted grid.
- **`parts_mask.txt`:** same size as the fixed grid. Every opaque cell holds the symbol of the layer it belongs to; transparent cells hold `.`. `?` means not assigned yet and fails the build.

| Rig | Mask symbols |
|---|---|
| `humanoid` | `h` head, `t` torso, `f` front arm, `b` back arm, `l` legs, `W` weapon, `S` shield, `T` staff, `B` bow, `H` helmet |
| `slime` | `b` body, `l` base |
| `beast` | `h` head, `t` torso, `f` front legs, `b` back legs, `w` tail |
| `flyer` | `t` torso, `f` front wing, `b` back wing |
| `silhouette` | `l` legs, `b` body |

- **`parts.json`:**
  - `mask`: the mask file name (`parts_mask.txt`).
  - `joints`: `{joint: [x, y]}` in grid cells. Every joint of the rig must be present (humanoid: `neck`, `shoulder`, `hand`, `hip`, `foot`; the other default rigs: `foot`). The foot's y must be the bottom row of the rig's `foot_part`; the foot point is where the figure is placed in the cell. The other joints must be cells inside the grid; they name where equipment hangs. `parts --init` writes the bottom row of the whole figure as the foot, which is wrong for a beast (its foot part is the back legs) and a flyer (its foot part is the torso): correct it.
  - `added`: `{layer: grid file}`. Cells hidden in the generated pose that a motion reveals (the torso behind a shield). Same size as the grid; must not overlap the layer's existing cells. Counted as newly drawn cells.
  - `swaps`: `{"layer@name": grid file}`. Alternative drawings of one layer that a template or override switches to (a raised arm, wings up). Same size as the grid.
  - `variants`: `{name: {"file": grid file, "foot": [x, y]}}`. Whole-figure drawings for `replace` frames (the fallen pose, `down`). Any size, with their own foot point.

## Rigs and motion templates

A rig is data in the config (`rigs`): its parts, joints, parent chain, draw order, foot part and the layers left out of the height.

- Every part has a parent. In the humanoid, legs and torso hang on `root`, head and both arms on the torso. Equipment hangs on a joint: weapon, shield, staff and bow on the hand (so on the front arm), the helmet on the neck (so on the head). Lowering the torso lowers the head, arms, helmet and shield with it.
- A template frame only uses whole-pixel `offsets` per layer (`root` moves everything), `swap` and `replace`. Offsets add up along the parent chain. Rotation and scaling are not supported: they break pixels.
- A template is for one action of one rig, and only that action may use it.
- The draw order is fixed per rig. Humanoid: `back_arm, legs, torso, head, helmet, weapon, staff, bow, front_arm, shield`. The hand is drawn over the weapon, staff and bow so the grip shows; the shield sits outside the front arm.
- `height_excludes` (humanoid: weapon, staff, bow) are left out of the measured height, and `equipment_allowance` is how much taller a candidate holding one may be before it fails the [height check](#the-height-check).
- A unit with both a shield and a weapon holds the weapon in the back hand. Because the rig hangs the weapon on the front (shield) hand, `guard_raise` and `shield_bash` move `weapon` opposite to `front_arm` so it stays with the torso. If your unit holds the weapon in the front hand, override those `weapon` offsets to `[0, 0]`.
- The output is a pre-baked sheet: a new equipment combination is a new build.

**Frame counts and timing.** Every action has a fixed frame count (`frames`), and every rig has required actions (`required_actions`). A strike action's strike frame (where the hit lands) must be within `max_windup_frames`. Frame length is `frame_ms` unless the template or an override sets its own; the idle templates use a slower 200 ms, because four breathing frames at the shared length looked hurried.

### Templates

**Humanoid** (`humanoid`)

- `humanoid_idle` (torso breathes), `humanoid_hit`, `humanoid_down` (the `down` variant), `guard_raise` (shield up, then held; for units with a shield).
- One big action: `sword_slash`, `shield_bash`, `staff_cast` or `bow_shot`.
- `shield_bash` and `staff_cast` move the body and front arm together; `shield_bash` pushes `root` forward (+x) to strike with the shield.
- `sword_slash` moves only the body (`root`, `torso`). The arm and blade poses (raised, diagonal, extended) differ too much between characters for offsets to make a swing, so a sword unit draws them as `swaps` and puts them in its `overrides`.
- `bow_shot`: the bow is in the front hand, drawn under the front arm like a staff; the back arm draws the string (`back_arm` offsets): nock, draw, release, with the strike on the release. `root` recoils slightly instead of stepping forward. No arrow is drawn.

**Slime** (`slime`)

- Parts `body` and `base`; the foot row is the bottom of `base`.
- `slime_idle`, `slime_attack` (a body slam, moved with `root` x only so the base stays on the foot row), `slime_hit`, `slime_down`.

**Beast** (`beast`, also the `boss` kind's default)

- Parts `head`, `torso`, `front_legs`, `back_legs`, `tail`. Legs and torso hang on `root`, head and tail on the torso. Draw order: back legs, tail, torso, front legs, head.
- The foot part is the back legs: they stay on the ground when the front legs lift in a pounce.
- `beast_idle` (torso breathes, head and tail follow, legs stay), `beast_attack` (crouch, then pounce and bite, moved with `root` x), `beast_hit`, `beast_down`.
- A body nearly as big as its cell has little room to lunge forward; placing its foot joint under the middle of the body keeps the attack inside the cell.

**Flyer** (`flyer`, set with `"rig": "flyer"` on any kind)

- Parts `torso`, `front_wing`, `back_wing`; the wings hang on the torso. Draw order: back wing, torso, front wing.
- Instead of the foot row it hovers: the torso's bottom row sits `air_height` rows above the cell bottom, within `air_bob`. Only the fallen pose lands on the kind's foot point. The motion JSON's `foot` is still the ground foot point, so a consumer places a flyer like any other unit and it appears airborne.
- `flyer_idle`: wings up (swap), middle (the base drawing), down (swap), middle, with the body bobbing. A flyer therefore draws `front_wing@up`, `front_wing@down`, `back_wing@up` and `back_wing@down` in `swaps`.
- `flyer_attack`: wings folded up, a dash with `root` x and a little `root` y to dive. `flyer_hit` is pushed back with wings up. `flyer_down` is a variant on the ground.

**Silhouette** (`silhouette`)

- Parts `legs` and `body`; only `silhouette_idle` and `silhouette_down`. For flat stand-in figures a game can tint through the mask (use `"height_reference": "kind"`).

**Changing templates.** Template numbers were tuned on real drawings. Change a template in the config only when you mean to change every unit that uses it; a difference one unit needs always goes in that unit's `overrides`.

## The motion JSON

`<id>_motion.json` is everything an engine needs to cut and play the sheet:

```json
{
  "unit": "botanist",
  "kind": "humanoid",
  "sheet": "botanist_sheet.png",
  "mask": "botanist_mask.png",
  "sheet_size": {"width": 768, "height": 384},
  "cell": {"width": 128, "height": 96},
  "foot": {"x": 52, "y": 92},
  "display_scale": 3,
  "actions": [
    {"name": "idle", "template": "humanoid_idle", "frame_ms": 200, "strike_frame": -1,
     "frames": [{"name": "botanist_idle_0", "x": 0, "y": 0}, ...]},
    ...
  ]
}
```

- Coordinates have a top-left origin, y down. `x`, `y` are the top-left corner of a frame's cell in the sheet. Every cell of a unit has the same size. For an engine with a bottom-left origin: `y_bottom = sheet_size.height - y - cell.height`.
- `foot` is the foot point inside the cell, the same in every frame: place the unit by it.
- `strike_frame` is `-1` for actions without a strike. `template` names the template that made the action, so a consumer can choose a sound or effect by the kind of attack.
- `display_scale` is the config's on-screen enlargement hint.
- Import the sheet with nearest-neighbour filtering and no compression so the pixels stay crisp.

## Commands

| Command | What it does | Writes to |
|---|---|---|
| `init [--force]` | Writes the default config and a `.gitignore` for the preview folder. | project root |
| `generate <unit> [--batch N] [--variant "..."]... [--from-art \| --reference <png> \| --design-from candidate_N.png \| --design-from-file ref_*.png] [--note "..."]...` | Starts N codex runs at once, one standing pose each. Comparison sheet `sprites/out/<unit>/batch_<first>-<last>.png`. | source |
| `recheck <unit> candidate_N.png...` | Measures recorded candidates again with the current settings and rewrites only their three height fields. | source |
| `reduce <unit> --candidate candidate_N.png [--height H] [--force]` | Size pass: converts the candidate at its own grid, shrinks it to the target height and writes `ref_<H>px_x<block>.png` and its record. The reduced grid goes to `sprites/out/<unit>/ref_*_grid.txt`. | source |
| `convert <unit> [--candidate f \| --input path] [--preview] [--force]` | Image to `grid.txt`, `palette.json`, `convert.json`. The default input is the image adopted at checkpoint 1. `--preview` writes to the preview folder instead. | source |
| `fix <unit> [--from-png <png> --category c --reason r]` | Applies `fixes.json` and counts fixed cells per category. Writes `fixed_zoom.png` (on a solid background, to look at), `fixed_edit.png` (transparent, enlarged, to edit and hand back) and `fixed.txt`. `--from-png` turns an edited `fixed_edit.png` into edits appended to `fixes.json`. | preview (and `fixes.json`) |
| `parts <unit> [--init] [--force]` | Splits the layers and checks them (`parts_zoom.png`). `--init` writes `parts_mask.txt` (all `?`) and a `parts.json` skeleton. | preview (`--init`: source) |
| `assemble <unit>` | Builds every frame and runs every check, writing all outputs to the preview folder only. | preview |
| `build <unit>... [--force]` | Writes the six outputs if every check passes. Also refreshes the unit's `art/dot_ref.png` if it has one. | output |
| `palette-swap <unit>... [--force]` | `build` for a palette-swap unit. | output |
| `verify [<unit>... \| backgrounds/<id>...] [--convert]` | Rebuilds sprites, card art and backgrounds from source and compares them with the files on disk. No arguments: everything. Read only. | nothing |
| `checkpoint <unit> [--record N --approve\|--reject --note "..." [--candidate f \| --reference ref_*.png]] [--require N]` | Shows, records or requires a checkpoint. | source |
| `selftest` | The pipeline's own checks; exits non-zero on failure. | temp folder |
| `art ...`, `bg ...` | Card art and backgrounds ([card-art.md](card-art.md)). | |

- Nothing is overwritten silently. `convert`, `reduce`, `parts --init`, `build`, `palette-swap`, `art build` and `bg build` refuse to replace existing files unless given `--force`.
- `generate` saves to the next free number. Preview files are rewritten every time.

## Convert

How `convert` turns a generated image into a grid. The settings are `convert` and `palette` in the config.

1. **Key the background.** A pixel within `background_tolerance` of `background` on every channel, or with alpha under half, is background. The model's magenta is never exactly #FF00FF, hence the tolerance. Then pink pixels touching the background (red and blue at least `min_rb_over_g` above green and within `max_distance` of the background) are removed too, up to `passes` times.
2. **Find the grid.** One square cell size within `pixel_size_range` and its offset, the same for both axes: the size and position whose grid lines catch the largest share of colour edges, minus the share random lines would catch. Without the square constraint the axes drift apart. The range's top is generous on purpose: a low limit once read 12-15 px blocks as half blocks and doubled the measured height.
3. **Sample cells.** Each cell is coloured from its middle (inside `cell_sample_margin`): transparent if background pixels win, otherwise the mean of its most common coarse colour bin.
4. **Quantize.** Seeded k-means (`kmeans_k`, `seed`), then the closest pair of colours is merged until there are at most `max_colors` and no pair is closer than `merge_distance`.
5. Isolated single cells are kept (`remove_isolated_cells` off): removing them erased pupils.
6. **Crop** to the opaque cells. `grid.txt` is written exactly as converted; every fix goes into `fixes.json`, so `verify --convert` can check the conversion on its own.
7. **Count pink edge cells**: opaque cells touching transparency or the grid edge whose colour still matches the pink rule. Printed as `pink edge cells N/M` and kept in `convert.json`. If N is not 0, fix those cells to the outline colour in the hand fixes.

## Build checks

`assemble` and `build` run these. `build` writes nothing if any fails.

- No semi-transparent pixels.
- At most `palette.max_colors` colours.
- Transparent background: every cell's 1-px border is empty. This also catches a drawing cut off at the cell edge.
- Every frame is the kind's cell size.
- **Height:** the opaque height of idle frame 0, without the rig's `height_excludes` layers (a flyer: without its wing swaps), is within `height_tolerance` of the target. The target is the unit's own `height` if set; else the kind's reference unit's built height if set, built, and not the unit itself (and the unit has no `height_reference: "kind"`); else the kind's `height`.
- **Feet on the foot row:** in every frame except `feet_exempt_actions` and `airborne` frames, the bottom row of the foot part is the foot point's y.
- **Air point** (flyer, instead of the foot row): in the same frames, the torso's bottom row is within `air_bob` of the air point.
- A drawing that leaves the cell while assembling is an error; the big action's forward movement must fit inside the cell too.
- **Motion rules:** each template's `action` and `rig` match, frame counts, strike frame range, airborne frame range and override frame numbers are valid, and every required action of the rig is present.

## Segments and checkpoints

The workflow splits one unit into three segments with checkpoints between them. Checkpoints are records in `checkpoints.json`; each segment starts by requiring the previous one (`checkpoint <unit> --require N`), so nothing moves on without a decision on file.

There are three numbered checkpoints, plus 1a when the size pass is used. **The person sees two of them: checkpoint 1 (adopting a candidate) and checkpoint 3 (the final GIF).** Checkpoint 1a (picking the design for the size pass) and checkpoint 2 (the hand fixes) are made and recorded by whoever runs the pipeline (in Claude Code, the main conversation), and shown to the person together with the next checkpoint. Showing all four to the person makes every unit wait on them twice as often; anything the person later disagrees with can still be redone. When a person runs the CLI alone, they make all four calls themselves.

- Checkpoints 1 and 3 are never approved in the person's place. Choices outside the spec (a unit's own height), reaching a generation cap and the size-pass fallback below are also the person's.
- While a unit is in progress, the pipeline code is not changed; a tool that blocks is reported.

| Segment | Work | Shown at its end | Checkpoint |
|---|---|---|---|
| 1 | Write `unit.json`. Design pass and size pass (below). | Design candidate sheet, size-pass candidates and their sheet, `ref_*.png`; measured height, target, height check, generation count. | 1a design pick (worker), 1 adopt a candidate (person). Check that it does not resemble a known character or trademark, and does not share face, hair or equipment with your other characters. |
| 2 | `convert`, write `fixes.json`, `fix`. | Shown with checkpoint 3. | 2 hand fixes (worker). |
| 3 | `parts --init`, mask and joints, extra drawings, swaps and variants, `assemble`, `build`, `verify`. | `<id>_zoom.png`, `<id>_preview.gif`, check results, newly drawn cells; also segment 2's `fixed_zoom.png` and the count and share of fixed cells. To fix cells yourself, edit `fixed_edit.png` and hand it back. | 3 final GIF (person). |

A palette-swap unit has only segment 3 (`palette_map.json`, `palette-swap`, `verify`) and only checkpoint 3; show its GIF next to the base unit's.

### Segment 1: design pass and size pass

Image models draw on their own pixel grid whatever size the prompt asks for (an 80 px humanoid came back 116 to 166 px tall), so segment 1 has two passes.

1. **Design pass.** `generate` (usually `--batch 3`) to choose a design, size aside. A unit with adopted card art uses `--from-art` ([below](#units-with-card-art-art-first)). Candidates that `FAIL` the height check are still shown as designs here.
   - **Checkpoint 1a:** pick the design closest to the description (and the card art, if any). It is not adopted as is because of its size, so record it as a rejection that names the size pass: `checkpoint <unit> --record 1 --reject --candidate candidate_N.png --note "Design pick, size pass next: <why this one>"`.
   - A candidate that already `PASS`es and is good to use is adopted directly at checkpoint 1, with no size pass.
2. **Size pass.**
   - `reduce <unit> --candidate candidate_N.png` converts the picked candidate at its own grid (same settings as `convert`, in memory) and shrinks it to the height check's target. If the figure raises a weapon or staff above its head, pass the whole figure's height with `--height` so that head to foot comes out at the target.
   - Each reduced cell takes the symbol covering most of it, except that when dark outline colours (luma under `outline_max_luminance`) cover at least `outline_min_share` of a non-transparent cell, the outline colour wins. A plain majority erased 1-px outlines when two cells merged.
   - The result is enlarged in `ref_block` squares with a `ref_margin_blocks` magenta margin into `ref_<H>px_x<block>.png`; `ref_*.json` records the source candidate's sha256, the convert and reduce settings, the tool versions and the image's sha256.
   - Then `generate <unit> --batch 3 --design-from-file ref_<H>px_x<block>.png` with these three variants (`<W>` is `grid_size[0]` in `ref_*.json`):
     - `Match the reference grid exactly: the sprite is <H> art pixels tall; every art pixel is a <block>x<block> square block in the output.`
     - `Keep the reference's block size and silhouette; only clean up and restore details. Do not resample to a finer grid.`
     - `Treat this as a pixel-art cleanup pass of a <W>x<H> sprite: same canvas of blocks, same pose, fix the face, outline and small metal details.`
   - Only candidates that pass the height check are shown; **checkpoint 1** adopts one (`--record 1 --approve --candidate ...`). The model's block size may differ a little from the reference: `convert` finds the grid again.

### The height check

`generate` measures every candidate (converted with the current settings), prints the result and writes `measured_height`, `height_target` and `height_check` into `generation.json`. The candidate is measured whole, including any weapon, staff or bow; the target leaves those out.

- `PASS`: within `height_tolerance` of the target.
- `REVIEW`: taller, but the unit carries a layer of `height_excludes` and the measurement is at most target + `equipment_allowance`. The measurement includes the held item; judge head to foot at checkpoint 1.
- `FAIL`: everything else. Parts are not split yet, so extra height cannot be traced to a held item; past the allowance the figure itself is too big. Not shown (except in the design pass), but counted.

`recheck` measures recorded candidates again after you change the convert settings.

### Rejecting and redoing

- After a rejection, put the reason in `--note` for the next run. If only the size or pixel scale was wrong and the design is fine, go to the size pass: `--design-from` (the candidate itself as reference) did not hold the size reliably.
- A reference you prepared yourself goes into the unit folder as `ref_*.png` (no record needed) and is used with `--design-from-file`.
- At the generation cap, stop and ask (see [Caps](#caps)).

### When the size pass keeps failing

If several size passes give no usable candidate, the `ref_<H>px_x<block>.png` that `reduce` wrote (an exact block image at the target height) can be adopted as the unit's source instead of a generated candidate.

- It is the person's choice: the height and design are exact, but code shrinking blurs the face and small details, so segment 2 needs more hand fixes. Offer it; do not pick it for them.
- Record it with `checkpoint <unit> --record 1 --approve --reference ref_<H>px_x<block>.png --note "<the person's words>"`. It needs `--approve` and cannot be combined with `--candidate`.
- Only an untouched `reduce` output in the unit folder is accepted: its record and its source candidate's sha256 must match, and reducing again with the recorded settings must give the same pixels. A picture without a record, an edited picture, or a record edited to match are refused.
- The checkpoint entry has `kind: "reduced-ref"`, the image and its sha256, the source candidate and the note. `generation.json` is not changed.
- `convert` then reads that image block by block at its recorded size, without grid estimation or colour clustering, with the source candidate's palette symbols, so `grid.txt` is the reduced grid byte for byte. `convert.json` records `kind: "reduced-ref"`. A file that no longer matches the sha256 adopted at checkpoint 1 is refused.
- Segments 2 and 3 are unchanged.

### Units with card art: art first

A unit with adopted card art (`<unit>/art/art.json`) gets its sprite from that art. Making the card art first is optional, but it gives the sprite a strong design anchor and makes the two match ([card-art.md](card-art.md)).

1. **Design pass:** `generate <unit> --batch 3 --from-art [--variant "..."]...`. The adopted card art (the latest `base` adoption in `art.json`) is attached, and the prompt gets `generation.art_reference_sentences` (`humanoid` for humanoids, `creature` for every other kind).
2. Without adopted art, or when the art file no longer matches its sha256 in `art.json`, it stops before codex runs. A unit with adopted art cannot run a design pass without a reference.
3. **Size pass:** as above.
4. **Checkpoints 1a, 1 and 3:** compare candidates and the final GIF with the card art: does it look like the same character?

### Recording checkpoints

```sh
dotomata checkpoint <unit>                                          # status as JSON
dotomata checkpoint <unit> --record 1 --approve --candidate candidate_2.png --note "<reason>"
dotomata checkpoint <unit> --record 1 --reject --candidate candidate_1.png --note "<reason>"
dotomata checkpoint <unit> --record 2 --approve --note "<reason>"
dotomata checkpoint <unit> --record 3 --approve --note "<reason>"
dotomata checkpoint <unit> --require 2                              # exit 1 unless 2 is approved
```

- Every record needs a note. For checkpoints 1 and 3, the note is a short paraphrase of what the person said. For 1a and 2, it says what the worker looked at and why it decided, and includes anything the person said about it.
- Checkpoint 1's decision and note are also written into that candidate's entry in `generation.json` (`adopted`, `reason`). Checkpoint 1 needs `--candidate` (or `--reference`), and the candidate must be recorded in `generation.json`.
- `--record 2` and `--record 3` need the previous checkpoint approved.
- An approval counts only while it is the latest decision for its number and was recorded after the previous number's latest approval. Redoing checkpoint 1 therefore voids checkpoints 2 and 3 until they are made again. The status's `approved` shows exactly this, and decides the next segment.
- A palette-swap unit records only checkpoint 3, with nothing before it.

## Generation guardrails

The rules every `codex exec` run follows, for sprites, card art and backgrounds alike (`codexrun.py`).

### Caps

- Each kind has its own cap and record: sprites `generation.cap_per_unit` (`generation.json`), card art `art.cap_per_unit` (`art_generation.json`), backgrounds `bg.cap_per_background` (`bg_generation.json`). The batch size is limited by each kind's `batch_max`.
- The count is the sum of `image_generation_calls` in the record. If used + N would pass the cap, the command stops before any codex run. Then stop and ask the person; do not raise the cap or edit the record down.

### One image per run

- The first prompt line is `generation.tool_instruction`: call the image tool exactly once, do not retry, do not save, copy or move an image file, do not call the tool again to fetch or show the picture, reply with one word. Without a stated count, runs generated more than asked. When told to save the picture, a run that failed to save it generated again. When not told to save, a run would sometimes call the tool once more to "hand over" its picture, and that second call draws a different picture from a throwaway prompt. So nothing is saved by the model; the tool reads the picture from the session record ([Provenance](#provenance)).
- The runs of a batch run at the same time, each in its own temp folder outside the project. Candidate numbers are reserved by the parent before any run starts (a number on disk or in the record is skipped). Only the parent writes the record, after every run has finished; failed runs are recorded and counted too.
- The i-th `--variant` is added to the i-th run as `For this candidate: ...`; more variants than runs is refused. `--note` goes to every run as `Changes from the previous candidate: ...`.
- The sprite comparison sheet shows the candidates side by side at one height (`preview.batch_sheet_height`) with file name and height check. A run without an image is an empty panel with its error.

### Prompts

- Sprite prompt: `Image: ` + `common_prompt` + size sentence + `sprite_note` + `design`, then `--note`, the reference sentence, `Also restore: ...` and the variant. The size sentence uses the unit's target height and cell; without it, a first candidate came back about 132 px tall against 80. The model still draws on its own grid, which is what the size pass fixes.
- Never put the name of an existing game, studio or artist in a prompt. A prompt (variants included) containing a `banned_terms` entry is refused before codex runs.

### Reference images

A sprite run takes at most one reference, and every run of a batch gets the same one. `generation.json` records its `mode`, path and sha256.

| Option | `mode` | Takes | Prompt sentence |
|---|---|---|---|
| `--from-art` | `art` | The adopted card art | `art_reference_sentences` |
| `--reference <png>` | `scale` | Another unit's image, as a scale reference | `scale_reference_sentence` |
| `--design-from candidate_N.png` | `design` | One of this unit's recorded candidates | `design_reference_sentence` |
| `--design-from-file ref_*.png` | `design-file` | A prepared image inside the unit folder (not a recorded candidate) | `design_file_sentence` (+ `restore_details`) |

- Do not use a scale reference for a new character's design: a new character given an existing one as reference came out with the existing one's face. Put what differs from your other characters (hair shape and colour, outfit, equipment, build) into `design` instead.

### The codex run

- Each run is `codex exec --skip-git-repo-check -c project_doc_max_bytes=0 --json -m <model> -c model_reasoning_effort=<effort> --sandbox workspace-write -C <temp folder>`, started in a temp folder outside the project so no project instructions file reaches the prompt. References go in with `-i`.
- The prompt goes through standard input, because `-i` takes several values and would swallow a positional prompt.
- Model, effort and timeout are `generation.model`, `generation.effort` and `generation.timeout_seconds`. Each run's `--json` events are kept in its temp folder (`codex_events.jsonl`), never in the project.

### Counting generations

- `codex exec --json` has no image item (in the codex-cli 0.162 to 0.167 formats this was written against), so the thread id from `--json` is used to find the session record (`$CODEX_HOME/sessions/.../rollout-*-<thread id>.jsonl`), and generations are counted there.
- A generation is either a `response_item` of type `image_generation_call`, or an `event_msg` `item_completed` whose item is an `Extension` of kind `image_gen.generation`. The same generation may appear in both shapes, so the larger count is used.
- That count is compared with the number of files in `$CODEX_HOME/generated_images/<thread id>/` and any image items in `--json`; the largest is recorded.
- A run counts at least 1 if it produced an image, if codex failed, if the session record is missing, or if the record shows a call to the image tool (`custom_tool_call` naming `image_gen`). Only a session record with neither generations nor tool calls counts 0, so the cap cannot be dodged, even if the record format changes again.

### Provenance

A run's picture is taken only from that run's own codex thread. When models were told to save into a working folder, one run of a parallel batch could not find its file and copied the picture of another run running at the same time. So:

- The picture is the first generation in the session record (in the order generation ids first appear) whose `status` is `completed` and whose base64 `result` starts with the PNG signature. Extra pictures from a run that broke the one-call rule are not used (`note`: the run generated more than once); they still count.
- A `result` that is not valid base64 or not a PNG is skipped and counted in the `note`.
- With no such record, the first file (by write time) of the thread's `generated_images/<thread id>/` folder is used, for record formats that carry no image bytes.
- With neither, the run has no image. A file the model left in its working folder is never read, nor taken as evidence of an image.
- The run output's `image from` says which source was used.
- A new image whose sha256 is already recorded in any generation record in the source folder (any kind, any owner), or earlier in the same batch, is refused and still counted.
- A recorded card art or background candidate later found not to be its run's picture is marked with `art invalidate <unit> --candidate <file> --reason "..."` or `bg invalidate <id> ...`. The record gets `invalid`, the count stays, the preview file is deleted, and adopting it is refused. An adopted candidate can only be marked after another is adopted.

## Reproduction

- `verify` rebuilds outputs from the sources in a temp folder and compares them with the files on disk, without touching them: sprites, card art (with `dot_ref.png`) and backgrounds. A different file set, or a missing file, is a failure too.
- PNGs are compared by decoded size, mode and pixels, not bytes: Pillow's zlib-ng compresses differently per CPU (macOS arm64 and Linux x86_64 give different bytes for the same pixels). A damaged PNG, a non-8-bit PNG or one with extra chunks (`tRNS`, text) counts as different, since the outputs never contain those and decoded pixels would not show them. JSON and GIF files are compared byte for byte.
- Units without outputs yet are skipped, unless named on the command line.
- `verify --convert` also re-runs `convert` from the image and settings recorded in `convert.json` and checks that `grid.txt` and `palette.json` come out the same. This is only promised on the same pinned versions, so a different recorded tool version fails. A reduced reference adopted as fallback is re-read block by block and its records are checked again.
- Python, Pillow and numpy are pinned exactly. Changing them can change the convert results the selftest pins: if so, confirm the change is intended, update the selftest's expected values and run `verify` on everything.

## Selftest

`dotomata selftest` runs the pipeline's own checks on small fixed inputs in temp folders. It never generates an image: a fake codex (`selftest_data/fake_codex.py`, through `DOTOMATA_CODEX_CMD`) replays recorded session events. `dotomata --config dotomata.json selftest` also checks your own config's templates, rigs and kinds. Run it in CI next to `verify`.

| File | Covers |
|---|---|
| `selftest.py` | Convert expected values; grid estimation on random figures of known block size; each check against a broken input; assembly pixels; the motion rules of the real templates; `reduce`, `recheck`, hand fixes; a small unit's `build`, `verify`, `palette-swap`; height and foot checks; `generate` and batches with the fake codex (counting, references, height check, cap, banned names, number reservation, variants, partial failure); checkpoint voiding; PNG pixel comparison; the committed silhouette units; `init` and `--config`. |
| `selftest_refadopt.py` | Adopting a reduced reference: refusals, records, `convert` and `verify --convert`. |
| `selftest_rigs.py` | Beast, flyer and bow with the real rigs and templates: draw order, breathing and wing beat, strike frames, the air point, height. |
| `selftest_art.py` | Card art and backgrounds: prompts, `dot_ref.png`, `--from-art`, separate counts and caps, adoption and bust refusals, build and `verify`, placeholders, recolour, cover-fit. |
| `selftest_provenance.py` | Which picture a run gets, and `invalidate`. |

## Following the workflow from any tool

These docs and the CLI are the whole procedure. The Claude Code plugin adds only the conversation around it: the `sprite` skill dispatches the `sprite-artist` agent once per segment, shows results, and records checkpoints 1a and 2 itself and checkpoints 1 and 3 from the person's answer. The agent never asks the person anything, never records a checkpoint and never commits. The `card-art` skill runs card art and backgrounds in the main conversation, without an agent.

Any other tool follows the same segments and checkpoints: before checkpoints 1 and 3, give the person the paths of the files to look at and stop; then record their words with `checkpoint --record`.

- Change sources only through commands (`generate`, `recheck`, `reduce`, `convert`, `checkpoint`, `fix --from-png`, `parts --init`, `build` for `art/dot_ref.png`, `art generate/dot-ref/adopt/bust/invalidate`, `bg generate/adopt/invalidate`). Edit by hand only the files people write: `unit.json`, `fixes.json`, `parts.json`, the mask and the added, swap and variant grids, `palette_map.json`, `recolor.json`, `background.json`.
- Make outputs only with `build`, `palette-swap`, `art build` and `bg build`.
- When done, `verify` and `selftest` pass.
