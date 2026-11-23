# dotomata

**dotomata** — *dot + automata.* An agent pipeline that takes a character from concept art to a validated, game-ready animated pixel sprite sheet.

<!-- demo GIF: added with the example units -->

## What makes it different

- **Every build is checked.** A sheet is written only if the figure is the target height, its feet sit on the foot row in every standing frame, it stays within the palette cap, it has no semi-transparent pixels, nothing touches the cell border, and every required action has its exact frame count.
- **Reproducible rebuilds.** Every output is rebuilt from committed sources (the generated image, the grid, the hand-fix list, the part data). Outputs carry no dates, paths or tool versions, and the dependencies are pinned, so the same sources give the same pixels on any machine. `dotomata verify` rebuilds everything and compares it with what is on disk.
- **Provenance for every generated image.** Each run records its prompt, references, model, generation count and the image's sha256. The image is read from the run's own Codex session, never from a file the model saved, and a duplicate of any recorded image is refused.
- **Human checkpoints.** The person adopts the candidate and approves the final animation. Every decision is recorded with its reason, and each segment starts only once the previous checkpoint is on file. Generation has a hard cap per character.
- **Rigged motions, not generated frames.** The image model draws one standing pose. Parts (head, torso, arms, legs, weapon, shield...) are split on a shared rig, and motion templates move them by whole pixels, so every frame stays on the same grid and palette, and a new character reuses every animation.

## How it works

1. **Concept art** (optional): a card illustration of the character, which the sprite is then drawn from.
2. **Base pose**: the Codex CLI draws one standing pose on magenta. A size pass shrinks the chosen design to the target height in code and has the model clean it up on that grid.
3. **Pixel grid and palette**: the background is keyed out, the model's pixel grid is found and colours are reduced to a small palette.
4. **Hand fixes**: cell-level edits in a list (from an agent, or painted by you), counted per category.
5. **Parts and rig**: each cell is assigned to a part; joints, hidden cells and alternative drawings are added.
6. **Motions**: idle, attack, hit, guard and down are assembled from templates.
7. **Output**: sheet, mask, motion JSON and previews, after every check passes.

## Requirements

- [uv](https://docs.astral.sh/uv/). It installs the pinned Python 3.12, Pillow and numpy on first run.
- The [Codex CLI](https://github.com/openai/codex), logged in, with image generation available. Image generation in Codex needs a ChatGPT plan that includes it. Only the generate commands use Codex; converting, building and verifying work without it.

## Install as a Claude Code plugin

```
/plugin marketplace add zeikar/dotomata
/plugin install dotomata@dotomata
```

On Claude Code 2.1.275 or later, one line does both:

```
/plugin install dotomata --marketplace zeikar/dotomata
```

The plugin adds two skills and an agent:

- `/dotomata:sprite <unit>`: makes an animated sprite through three segments, stopping for you to adopt a candidate and to approve the final GIF.
- `/dotomata:card-art <unit>` or `/dotomata:card-art backgrounds/<id>`: card art (2:3 portrait and bust) or a 16:9 background, from one batch you pick from.
- `sprite-artist`: the agent the sprite skill dispatches for each segment.

The skills check the prerequisites on first use and offer to create `dotomata.json` in your project.

## Use the CLI without Claude Code

```sh
uvx --from git+https://github.com/zeikar/dotomata dotomata --help
```

or clone the repository and run it from your project root:

```sh
git clone https://github.com/zeikar/dotomata
uv run --project /path/to/dotomata dotomata --help
```

Every path in the config is relative to the directory you run it from (or `--root`).

## Quick start

In your project (`dotomata` stands for either command above):

```sh
dotomata init        # writes dotomata.json and sprites/.gitignore
```

Describe a character in `sprites/source/knight/unit.json`:

```json
{
  "id": "knight",
  "kind": "humanoid",
  "name": "Knight",
  "colors": ["#3f5a8c", "#c9cdd4", "#8c2f2f"],
  "equipment": ["shield", "weapon"],
  "design": "A stocky young knight with short copper hair, a dented steel breastplate over a navy tunic and a red scarf. He carries a round wooden shield with a white star, his signature item, and a short sword.",
  "actions": {"idle": "humanoid_idle", "attack": "shield_bash", "hit": "humanoid_hit", "guard": "guard_raise", "down": "humanoid_down"}
}
```

Then, in Claude Code, run `/dotomata:sprite knight`. Without Claude Code, follow [Your first unit](docs/sprites.md#your-first-unit): `generate`, `reduce`, `checkpoint`, `convert`, `fix`, `parts`, `assemble`, `build`, `verify`.

## Outputs

`dotomata build knight` writes to `build/sprites/knight/` (configurable):

| File | Content |
|---|---|
| `knight_sheet.png` | Transparent sprite sheet: one row per action, one cell per frame. |
| `knight_mask.png` | White silhouette of the same layout, for hit flashes and tints. |
| `knight_motion.json` | Cell size, foot point, and per action the frame positions, frame time and strike frame. |
| `knight_preview.gif`, `knight_zoom.png` | Previews. |
| `knight_report.json` | Height, colours, hand-fixed and newly drawn cells, generations used, check results. |

The sheet and the motion JSON are all an engine needs; nothing is tied to a particular engine. Card art builds a full portrait and a square bust PNG; a background builds one PNG at the configured size.

## Docs

- [Sprites](docs/sprites.md): the workflow, file formats, rigs and templates, commands, checks, guardrails and reproduction.
- [Card art and backgrounds](docs/card-art.md)
- [Configuration](docs/config.md): every key of `dotomata.json`.

## License

[MIT](LICENSE)
