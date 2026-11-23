# Configuration (`dotomata.json`)

`dotomata init` writes the default config to `<root>/dotomata.json`. Every command reads it from there, or from the file `--config` names (relative to the current directory). Every path inside the config is relative to `--root` (default: the current directory). Sections the tool does not know are ignored, so you can keep your own keys in the same file.

`dotomata --config dotomata.json selftest` runs the pipeline's own checks and, in addition, checks the templates, rigs and kinds of your config against the motion rules below.

Units used below:

- **art px**: one pixel of the finished sprite (one cell of a grid file).
- **image px**: one pixel of a generated image.
- **ms**: milliseconds.

Changing a value that a build reads changes what `build` writes, so `verify` fails for every unit built with the old value until you rebuild it with `--force`. `convert` and `reduce` record the settings they used, so changing `convert` or `palette` does not invalidate already converted units (see [`verify --convert`](sprites.md#reproduction)).

## `schema`

Config format version. `1`.

## `paths`

| Key | Default | Meaning |
|---|---|---|
| `source` | `sprites/source` | Sources: one folder per unit (`<source>/<unit>/`) and per background (`<source>/backgrounds/<id>/`). Commit it. |
| `output` | `build/sprites` | Sprite outputs, one folder per unit path. |
| `preview` | `sprites/out` | Previews, comparison sheets of sprite batches, card art and background candidates that were not adopted. Regenerable; `init` writes a `.gitignore` next to it that ignores it. |
| `art_output` | `build/art` | Card art outputs, one folder per unit id. |
| `bg_output` | `build/backgrounds` | Background outputs, one `<id>.png` each. |
| `strings` | (not set) | Optional. A CSV string table with `key` and `en` columns. When set, a unit's `name` is a key looked up in it (only the card art placeholder uses the name). When not set, `name` is the English name itself. |

Point `output`, `art_output` and `bg_output` wherever your engine reads assets from.

## Sprite cells and timing

| Key | Default | Meaning |
|---|---|---|
| `display_scale` | `3` | Written into every motion JSON as a hint for how much to enlarge the sprite on screen. The tool itself does not use it. |
| `frame_ms` | `70` | Frame duration (ms) for a template that sets no `frame_ms` of its own. |
| `height_tolerance` | `2` | How far (art px) a measured height may be from its target and still pass, both in the build check and in the candidate height check. |

### `kinds`

A kind sets the cell every frame of a unit is drawn in, the height the unit should be and its default rig. Every unit names its kind in `unit.json`.

| Field | Meaning |
|---|---|
| `cell` | `[width, height]` of every frame (art px). |
| `height` | Target height (art px) of the figure in idle frame 0, without the rig's `height_excludes` layers. |
| `foot` | `[x, y]` of the foot point inside the cell (top-left origin, y down). Every standing frame puts the bottom row of the foot part on this row. Keep `y` below the top row: the cell's border must stay empty. |
| `reference_unit` | `null`, or the id of a built unit of this kind. When set, the height target of the kind's other units is that unit's built height (from its report) instead of `height`. The unit is found by its id wherever its folder is. |
| `rig` | Default rig for units of this kind. |

Defaults: `humanoid` (128x96 cell, height 80, foot [52, 92]), `beast` (128x96, 52, [60, 92]), `slime` (128x96, 40, [60, 92]), `boss` (128x128, 112, [60, 124], rig `beast`). A unit's own `height` must be between 1 and its kind's foot y.

Bigger cells cost more texture memory; the cell has to hold the farthest a big action moves the figure forward.

### Actions and frames

| Key | Default | Meaning |
|---|---|---|
| `action_order` | `idle, attack, hit, guard, down` | Row order of the actions in the sheet and the motion JSON. An action a unit does not have is skipped. |
| `frames` | idle 4, attack 6, hit 2, guard 2, down 1 | Required frame count per action. A template (with the unit's overrides) must have exactly this many. |
| `required_actions` | per rig | Actions every unit of that rig must have. `guard` is optional everywhere (for units with a shield). |
| `strike_actions` | `["attack"]` | Actions that have a strike frame (the frame where the hit lands). Other actions must have none. |
| `max_windup_frames` | `2` | A strike frame must be `0..max_windup_frames`, so the hit lands quickly. |
| `feet_exempt_actions` | `["down"]` | Actions whose frames are not held to the foot row (a fallen pose), and whose frames a flyer draws on the ground foot point. |

## `palette`

How `convert` reduces the colours of a generated image. `convert` records these values in each unit's `convert.json`.

| Key | Default | Meaning |
|---|---|---|
| `max_colors` | `24` | Palette cap. `convert` merges colours down to it, and the build check fails a sheet with more distinct colours. |
| `kmeans_k` | `26` | Number of k-means clusters before merging. |
| `seed` | `20261015` | k-means seed. Fixed so the same image always gives the same palette. |
| `kmeans_iterations` | `100` | k-means iterations. |
| `merge_distance` | `20` | After k-means, the two closest colours are merged until there are at most `max_colors` and no pair is closer than this (RGB distance). Higher gives fewer, flatter colours. |

## `convert`

How `convert` reads a generated image into a grid. See [Convert](sprites.md#convert) for the steps.

| Key | Default | Meaning |
|---|---|---|
| `background` | `[249, 5, 249]` | The magenta the model paints as background (it is not exact #FF00FF). |
| `background_tolerance` | `48` | A pixel within this much of `background` on every channel is background. Pixels with alpha below 128 are background too. |
| `fringe.min_rb_over_g` | `64` | A "pink" pixel has both red and blue at least this much above green. |
| `fringe.max_distance` | `150` | ...and is within this much of `background` on every channel. |
| `fringe.passes` | `2` | How many times pink pixels touching the background are removed as well. |
| `pixel_size_range` | `[3.0, 24.0]` | Range (image px) searched for the size of one art pixel. Keep the top generous: a too-low limit reads big blocks as two cells each and doubles the measured height. |
| `edge_threshold` | `48` | Colour change between neighbouring image pixels (sum of the three channel differences) that counts as an edge when fitting the grid. |
| `cell_sample_margin` | `0.2` | Fraction of each cell's edge ignored when sampling its colour, so only the cell's middle decides. |
| `mode_bin_shift` | `3` | Colours are binned by dropping this many low bits per channel; a cell takes the average colour of its most common bin. |
| `remove_isolated_cells` | `false` | Remove single opaque cells with no opaque neighbour. Off: it removed pupils and the tips of thin shapes. |

## `reduce`

The size pass (`reduce`) and the block images it and `art dot-ref` write.

| Key | Default | Meaning |
|---|---|---|
| `outline_max_luminance` | `45` | A colour darker than this (luma 0-255) counts as outline. |
| `outline_min_share` | `0.3333` | When outline colours cover at least this share of a reduced cell, the cell takes the outline colour, so 1-px outlines survive the shrink. |
| `ref_block` | `12` | Size (image px) of one art pixel in the reference image. Part of the file name (`ref_<height>px_x<block>.png`). |
| `ref_margin_blocks` | `10` | Background margin around the figure, in blocks. |
| `ref_background` | `#ff00ff` | Background colour of the reference image. |

## `fix_categories`

The categories a hand fix in `fixes.json` may use. The report counts fixed cells per category. Default: `face, hair, outline, armor, weapon, shield, staff, emblem, noise, other`.

## `preview`

| Key | Default | Meaning |
|---|---|---|
| `zoom` | `4` | Enlargement of `*_zoom.png` previews and of `fixed_edit.png`. |
| `gif_scale` | `3` | Enlargement of `<id>_preview.gif`. |
| `gif_idle_loops` | `2` | How many times the idle plays before the other actions in the GIF. |
| `gif_hold_last_ms` | `280` | Extra time (ms) on the last frame of every non-idle action in the GIF. |
| `background` | `#2b2b33` | Background of previews and comparison sheets. |
| `batch_sheet_height` | `512` | Height (image px) each candidate is shown at on a sprite batch's comparison sheet. |

Changing `preview` changes `<id>_zoom.png` and `<id>_preview.gif`, which are build outputs.

## `generation`

Sprite generation and everything every kind of generation shares (codex command, model, banned terms).

| Key | Default | Meaning |
|---|---|---|
| `cap_per_unit` | `15` | Most image generations one unit's sprite may use, over its whole history. A batch that would exceed it is refused before codex runs. |
| `batch_max` | `3` | Most codex runs one `generate` may start at once. |
| `codex_command` | `["codex"]` | Command that runs the Codex CLI. The environment variable `DOTOMATA_CODEX_CMD` replaces it (the selftest uses this for its fake codex). |
| `model` | `gpt-6-luna` | Model passed to `codex exec -m`. It must be one your Codex account can use with image generation. |
| `effort` | `medium` | Passed as `model_reasoning_effort`. |
| `timeout_seconds` | `600` | A codex run that takes longer is stopped and recorded as failed (it still counts). |
| `tool_instruction` | (text) | First line of every prompt, for every kind: call the image tool exactly once, save nothing, reply with one word. See [one image per run](sprites.md#one-image-per-run). |
| `common_prompt` | (text) | The sprite style: pixel art, proportions, side view facing right, outline, palette size, flat magenta background. |
| `size_sentence` | (text) | Asks for the target height and cell. Placeholders `{height}`, `{cell_width}`, `{cell_height}`. |
| `scale_reference_sentence` | (text) | Sent with `generate --reference`. |
| `design_reference_sentence` | (text) | Sent with `generate --design-from`. |
| `design_file_sentence` | (text) | Sent with `generate --design-from-file` (the size pass). |
| `art_reference_sentences` | `humanoid`, `creature` | Sent with `generate --from-art`: `humanoid` for humanoid units, `creature` (placeholder `{height}`) for every other kind. |
| `banned_terms` | game and artist names | A prompt containing one of these (case-insensitive), from any part including `--note` and `--variant`, is refused before codex runs. |

The sprite prompt is: `tool_instruction`, then `Image: ` + `common_prompt` + `size_sentence` + the unit's `sprite_note` + `design`, then optional lines for `--note`, the reference sentence, `restore_details` and `--variant`. Edit `common_prompt` to change the sprite style; keep the magenta background, which `convert` keys out.

## `art`

Card art. See [card-art.md](card-art.md).

| Key | Default | Meaning |
|---|---|---|
| `cap_per_unit` | `9` | Most card art generations per unit, counted apart from the sprite's. |
| `batch_max` | `3` | Most runs per `art generate`. |
| `size` | `[1024, 1536]` | Size (px) of the placeholder card. Adopted art keeps the size the model gave; it only has to be 2:3. |
| `bust_min_side` | `512` | Smallest side (px) of the bust crop. The bust is only ever shrunk. |
| `bust_size` | `512` | Side (px) of the written bust PNG. |
| `placeholder_bust` | x 128, y 96, 768x768 | Bust crop of the placeholder card. |
| `common_prompt` | (text) | Card art style: 2:3 portrait, full body, proportions, line and colour, plain background, no text. |
| `composition_sentence` | (text) | Pose and where the face sits. |
| `design_rule_sentence` | (text) | Asks for a design that still reads as a small pixel sprite. |
| `style_reference` | `null` | Optional path to a style image sent with every card art run (with `style_reference_sentence`). The path and sha256 go into the record. If set and missing, generation stops. |
| `style_reference_sentence` | (text) | Tells the model to take only the style from that image. |
| `dot_reference_sentence` | (text) | Sent when the unit has a built sprite: its `dot_ref.png` is a design reference only. |

## `bg`

Backgrounds. See [card-art.md](card-art.md#backgrounds).

| Key | Default | Meaning |
|---|---|---|
| `cap_per_background` | `9` | Most generations per background. |
| `batch_max` | `3` | Most runs per `bg generate`. |
| `size` | `[1920, 1080]` | Output size (px). The adopted image is scaled to cover it and cropped in the middle. |
| `layouts` | `battle`, `backdrop` | Prompt per layout; a background picks one with `layout`. `{design}` is replaced by its description. Add your own layouts here. |
| `common_prompt` | (text) | Style shared by every background. |

## `comparison_sheet`

The JPEG sheet a card art or background batch writes into the source as the record of what was chosen from.

| Key | Default | Meaning |
|---|---|---|
| `panel_height` | `768` | Height (px) each candidate is shown at. |
| `long_side` | `1600` | The whole sheet is scaled down to fit this. |
| `jpeg_quality` | `85` | JPEG quality. |

## `rigs`

A rig is the skeleton of a unit as data. See [Rigs and motion templates](sprites.md#rigs-and-motion-templates).

| Field | Meaning |
|---|---|
| `mask_symbols` | `{symbol: layer}`: the characters `parts_mask.txt` uses. |
| `parts` | `{part: parent}`. `root` moves the whole figure. |
| `joints` | `{joint: part it belongs to}`. Every joint must be given in `parts.json`. |
| `equipment` | `{layer: joint}`: an equipment layer moves with the part that owns its joint. |
| `draw_order` | Back to front. Fixed per rig. |
| `foot_part` | The part whose bottom row stands on the foot row. |
| `height_excludes` | Layers left out of the height (held items that may stick up). |
| `equipment_allowance` | (humanoid) How much (art px) taller than the target a candidate carrying an excluded layer may be and still go to review instead of failing. |
| `air_height`, `air_bob` | (flyer) The body hovers with its bottom row `air_height` rows above the cell bottom, give or take `air_bob`. |

Default rigs: `humanoid`, `slime`, `beast`, `flyer`, `silhouette`.

## `templates`

Motion templates. Each has `action` (the action it is for), `rig`, optional `frame_ms`, optional `strike_frame` (for strike actions) and `frames`: a list of frames, each with any of `offsets` (`{layer or "root": [dx, dy]}` in art px), `swap` (`{layer: name}`, drawn from the unit's `swaps`) and `replace` (the name of a whole-figure variant). See [the template list](sprites.md#templates).

Change a template only when you mean to change every unit that uses it; put a difference one unit needs in that unit's `overrides`.
