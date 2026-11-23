"""Shared data model: project config, unit folders, text grids and palettes."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from pathlib import Path

import numpy as np

TRANSPARENT = "."
# Palette symbols in assignment order. '.' is reserved for transparent and '?'
# marks an unassigned cell in a part mask.
PALETTE_SYMBOLS = "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ"


class SpriteError(Exception):
    """Bad data or a refused operation. The CLI prints it without a traceback."""


@dataclass
class Context:
    root: Path
    project: dict

    @property
    def source_dir(self) -> Path:
        return self.root / self.project["paths"]["source"]

    @property
    def output_dir(self) -> Path:
        return self.root / self.project["paths"]["output"]

    @property
    def preview_dir(self) -> Path:
        return self.root / self.project["paths"]["preview"]


CONFIG_NAME = "dotomata.json"


def load_context(root: Path, config: Path | None = None) -> Context:
    """The config is <root>/dotomata.json unless given. Every path inside it is relative to root;
    sections the tool does not know are ignored."""
    path = config if config is not None else root / CONFIG_NAME
    if not path.is_file():
        raise SpriteError(f"config not found: {path} (run `dotomata init` to write the default one)")
    return Context(root=root, project=read_json(path))


def read_json(path: Path) -> dict:
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except json.JSONDecodeError as e:
        raise SpriteError(f"invalid JSON in {path}: {e}") from e


def json_bytes(data) -> bytes:
    # Fixed formatting so rebuilt files compare byte for byte.
    return (json.dumps(data, ensure_ascii=False, indent=2) + "\n").encode("utf-8")


def sha256_file(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def write_new(path: Path, data: bytes, force: bool) -> None:
    """Writes a file, refusing to replace an existing one unless forced."""
    if path.exists() and not force:
        raise SpriteError(f"refusing to overwrite {path} (pass --force to replace it)")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)


# ---------------------------------------------------------------- units


@dataclass
class Unit:
    path: str          # folder relative to the source/output roots, e.g. "hero" or "monsters/slime"
    data: dict         # unit.json
    source: Path

    @property
    def id(self) -> str:
        return self.data["id"]

    @property
    def kind(self) -> str:
        return self.data["kind"]


def load_unit(ctx: Context, unit_path: str) -> Unit:
    source = ctx.source_dir / unit_path
    path = source / "unit.json"
    if not path.is_file():
        raise SpriteError(f"unit '{unit_path}' has no unit.json at {path}")
    data = read_json(path)
    for key in ("id", "kind"):
        if key not in data:
            raise SpriteError(f"unit '{unit_path}' unit.json is missing '{key}'")
    if data["kind"] not in ctx.project["kinds"]:
        raise SpriteError(f"unit '{data['id']}' has unknown kind '{data['kind']}'")
    if "height" in data:
        # A figure stands with its lowest row on the foot row and needs the cell's empty top row.
        tallest = ctx.project["kinds"][data["kind"]]["foot"][1]
        height = data["height"]
        if isinstance(height, bool) or not isinstance(height, int) or not 1 <= height <= tallest:
            raise SpriteError(f"unit '{data['id']}': height must be a whole number from 1 to {tallest} "
                              f"(the {data['kind']} cell from its foot row up, less the empty top row), got {height!r}")
    return Unit(path=unit_path, data=data, source=source)


def list_units(ctx: Context) -> list[str]:
    units = []
    for p in sorted(ctx.source_dir.rglob("unit.json")):
        units.append(p.parent.relative_to(ctx.source_dir).as_posix())
    return units


def kind_config(ctx: Context, unit: Unit) -> dict:
    return ctx.project["kinds"][unit.kind]


def drawn_height(ctx: Context, unit: Unit) -> int:
    """The height a unit is asked to be drawn at: its own `height`, or its kind's."""
    return unit.data.get("height", kind_config(ctx, unit)["height"])


# ---------------------------------------------------------------- grids


def read_grid(path: Path, what: str) -> np.ndarray:
    """Reads a text grid: one row per line, one symbol per cell."""
    if not path.is_file():
        raise SpriteError(f"{what}: missing grid file {path}")
    lines = path.read_text(encoding="utf-8").splitlines()
    if not lines:
        raise SpriteError(f"{what}: grid {path.name} is empty")
    width = len(lines[0])
    for y, line in enumerate(lines):
        if len(line) != width:
            raise SpriteError(f"{what}: grid {path.name} row {y} has {len(line)} cells, expected {width}")
    return np.array([list(line) for line in lines], dtype="<U1")


def grid_bytes(grid: np.ndarray) -> bytes:
    return ("\n".join("".join(row) for row in grid) + "\n").encode("utf-8")


def parse_hex(value: str, what: str) -> tuple[int, int, int]:
    if not (isinstance(value, str) and len(value) == 7 and value[0] == "#"):
        raise SpriteError(f"{what}: bad color '{value}', expected #rrggbb")
    try:
        return int(value[1:3], 16), int(value[3:5], 16), int(value[5:7], 16)
    except ValueError as e:
        raise SpriteError(f"{what}: bad color '{value}'") from e


def to_hex(rgb) -> str:
    return "#{:02x}{:02x}{:02x}".format(*(int(c) for c in rgb))


def read_palette(path: Path, what: str) -> dict[str, tuple[int, int, int]]:
    data = read_json(path) if path.is_file() else None
    if data is None:
        raise SpriteError(f"{what}: missing palette {path}")
    colors = {}
    for sym, value in data["colors"].items():
        if len(sym) != 1 or sym not in PALETTE_SYMBOLS:
            raise SpriteError(f"{what}: bad palette symbol '{sym}' in {path.name}")
        colors[sym] = parse_hex(value, f"{what} palette '{sym}'")
    return colors


def check_grid_symbols(grid: np.ndarray, palette: dict, what: str) -> None:
    allowed = set(palette) | {TRANSPARENT}
    bad = sorted(set(np.unique(grid).tolist()) - allowed)
    if bad:
        ys, xs = np.nonzero(np.isin(grid, bad))
        raise SpriteError(f"{what}: symbols {bad} are not in the palette (first at x={xs[0]}, y={ys[0]})")


def opaque(grid: np.ndarray) -> np.ndarray:
    return grid != TRANSPARENT


def render_rgba(grid: np.ndarray, palette: dict) -> np.ndarray:
    out = np.zeros(grid.shape + (4,), dtype=np.uint8)
    for sym, rgb in palette.items():
        mask = grid == sym
        out[mask, 0:3] = rgb
        out[mask, 3] = 255
    return out
