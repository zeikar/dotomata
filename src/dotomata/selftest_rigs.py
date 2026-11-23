"""Selftest cases for the beast and flyer rigs and the humanoid bow layer.
They use the real rigs and templates from the default config on small fixed grids, like the tiny humanoid and
slime cases in selftest.py."""

from __future__ import annotations

import copy
import json
from pathlib import Path

import numpy as np
from PIL import Image

from .assemble import compose_frame, resolve_action, stand_anchor
from .common import Context, json_bytes
from .parts import Art
from .selftest import as_rows, expect, expect_error, run_cli, tiny_project, write_project

PALETTE = {"a": "#c0a080", "b": "#606870", "c": "#d0d8e0", "d": "#8a5030", "e": "#303438", "g": "#e8dcc4"}

# A beast facing right: tail d, torso b, head c, back legs e, front legs g.
BEAST_GRID = [".....cc", "dbbbbcc", ".bbbbc.", ".e..g..", ".e..g.."]
BEAST_MASK = [".....hh", "wtttthh", ".tttth.", ".b..f..", ".b..f.."]
BEAST_DOWN = ["dbbbbcc", "eeggbc."]

# A flyer facing right: back wing b, torso a, front wing c; the top row is room for the raised wings.
FLYER_GRID = ["...", "b.c", "bac", ".a."]
FLYER_MASK = ["...", "b.f", "btf", ".t."]
FLYER_SWAPS = {
    "front_wing@up": ["..c", "..c", "...", "..."], "front_wing@down": ["...", "...", "..c", "..c"],
    "back_wing@up": ["b..", "b..", "...", "..."], "back_wing@down": ["...", "...", "b..", "b.."],
}
FLYER_DOWN = ["bac"]

# A small humanoid holding a bow (d) in its front hand (e): head a, torso b, legs g.
BOW_GRID = [".aa..", ".bbed", ".bbed", ".gg.d", ".g.g."]
BOW_MASK = [".hh..", ".ttfB", ".ttfB", ".ll.B", ".l.l."]


def grid(rows: list[str]) -> np.ndarray:
    return np.array([list(r) for r in rows])


def art_from(rig: dict, rig_name: str, rows: list[str], mask_rows: list[str], joints: dict,
             swaps: dict | None = None) -> Art:
    g, mask = grid(rows), grid(mask_rows)
    layers = {name: np.where(mask == sym, g, ".") for sym, name in rig["mask_symbols"].items() if (mask == sym).any()}
    return Art(unit_id="X", rig_name=rig_name, palette={}, fixed=g, layers=layers, joints=joints,
               swaps={k: grid(v) for k, v in (swaps or {}).items()})


def test_beast_assembly(real_ctx: Context) -> None:
    rig = real_ctx.project["rigs"]["beast"]
    art = art_from(rig, "beast", BEAST_GRID, BEAST_MASK, {"foot": (1, 4)})
    cell, anchor = (10, 8), (2, 6)

    def frame(offsets: dict) -> np.ndarray:
        canvas, bottom = compose_frame(art, rig, {"offsets": offsets}, cell, anchor, what="beast")
        return canvas, bottom

    canvas, bottom = frame({})
    expect(as_rows(canvas)[2:7] == ["......cc..", ".dbbbbcc..", "..bbbbc...", "..e..g....", "..e..g...."]
           and bottom == 6, "beast base frame:\n" + "\n".join(as_rows(canvas)))
    # Breathing: the torso carries the head and tail down, the legs stay on the ground.
    idle = resolve_action(real_ctx, "idle", "beast_idle", "beast", {}, [], "beast_idle")
    canvas, bottom = frame(idle.frames[2]["offsets"])
    expect(as_rows(canvas)[3:7] == ["..........", "......cc..", ".dbbbgcc..", "..bbbgc..."] and bottom == 6,
           "beast idle frame 2 (torso +2, front legs over the torso):\n" + "\n".join(as_rows(canvas)))
    # Draw order back legs -> tail -> torso -> front legs -> head.
    canvas, _ = frame({"back_legs": [0, -1], "front_legs": [0, -1], "tail": [1, 0]})
    expect(canvas[4, 2] == "b" and canvas[4, 5] == "g" and canvas[3, 2] == "b",
           "torso over back legs and tail, front legs over torso:\n" + "\n".join(as_rows(canvas)))
    canvas, _ = frame({"tail": [1, 2]})
    expect(canvas[5, 2] == "d", "tail over back legs:\n" + "\n".join(as_rows(canvas)))
    canvas, _ = frame({"front_legs": [1, -2]})
    expect(canvas[3, 6] == "c" and canvas[4, 6] == "c", "head over front legs:\n" + "\n".join(as_rows(canvas)))
    attack = resolve_action(real_ctx, "attack", "beast_attack", "beast", {}, [], "beast_attack")
    expect(attack.strike_frame == 2 and len(attack.frames) == 6
           and all(f.get("offsets", {}).get("root", [0, 0])[1] == 0 for f in attack.frames),
           "beast_attack: strike frame 2, six frames, travel on root x only")


def test_flyer_and_bow_assembly(real_ctx: Context) -> None:
    p = real_ctx.project
    flyer = p["rigs"]["flyer"]
    # The real air point fits the beast cell, above its foot point.
    _, air_y = stand_anchor(p["kinds"]["beast"], flyer, "flyer")
    expect(air_y == p["kinds"]["beast"]["cell"][1] - flyer["air_height"] < p["kinds"]["beast"]["foot"][1],
           f"flyer air point y {air_y}")
    expect_error(lambda: stand_anchor({"cell": [20, 14], "foot": [6, 12]}, dict(flyer, air_height=1), "f"),
                 "above the foot point")
    expect_error(lambda: stand_anchor({"cell": [20, 14], "foot": [6, 12]}, dict(flyer, air_height=14), "f"),
                 "inside the cell")
    expect_error(lambda: stand_anchor({"cell": [20, 14], "foot": [6, 12]}, dict(flyer, air_height=6.5), "f"),
                 "inside the cell")
    art = art_from(flyer, "flyer", FLYER_GRID, FLYER_MASK, {"foot": (1, 3)}, FLYER_SWAPS)
    idle = resolve_action(real_ctx, "idle", "flyer_idle", "flyer", {}, [], "flyer_idle")
    tips, bottoms = [], []
    for f in idle.frames:
        canvas, bottom = compose_frame(art, flyer, f, (8, 10), (3, 6), what="flyer")
        tips.append(int(np.nonzero(canvas == "c")[0].min()) - bottom)
        bottoms.append(bottom)
    # Wing up -> mid -> down -> mid (tip row against the body) while the body bobs one row.
    expect(tips == [-3, -2, -1, -2] and bottoms == [6, 5, 5, 6], f"flyer idle wing tips {tips}, body bottoms {bottoms}")
    # Draw order back wing -> torso -> front wing: each wing is moved onto the torso pixel at (5, 3).
    canvas, _ = compose_frame(art, flyer, {"offsets": {"back_wing": [1, 0]}}, (8, 10), (3, 6), what="flyer")
    expect(canvas[5, 3] == "a" and canvas[4, 3] == "b", "torso over back wing:\n" + "\n".join(as_rows(canvas)))
    canvas, _ = compose_frame(art, flyer, {"offsets": {"front_wing": [-1, 0]}}, (8, 10), (3, 6), what="flyer")
    expect(canvas[5, 3] == "c", "front wing over torso:\n" + "\n".join(as_rows(canvas)))

    humanoid = p["rigs"]["humanoid"]
    art = art_from(humanoid, "humanoid", BOW_GRID, BOW_MASK,
                   {"neck": (1, 0), "shoulder": (2, 1), "hand": (3, 2), "hip": (2, 3), "foot": (1, 4)})
    cell, anchor = (10, 8), (3, 6)
    canvas, _ = compose_frame(art, humanoid, {"offsets": {"bow": [-1, 0]}}, cell, anchor, what="bow")
    expect(canvas[3, 5] == "e" and canvas[4, 5] == "e" and canvas[5, 5] == "d",
           "the bow is drawn under the front arm:\n" + "\n".join(as_rows(canvas)))
    canvas, _ = compose_frame(art, humanoid, {"offsets": {"front_arm": [1, 0]}}, cell, anchor, what="bow")
    expect(canvas[5, 7] == "d" and canvas[3, 6] == "e", "the bow follows the front hand:\n" + "\n".join(as_rows(canvas)))
    canvas, _ = compose_frame(art, humanoid, {}, cell, anchor, exclude=set(humanoid["height_excludes"]), what="bow")
    expect(not (canvas == "d").any(), "the height leaves out the bow")
    shot = resolve_action(real_ctx, "attack", "bow_shot", "humanoid", {}, [], "bow_shot")
    expect(shot.strike_frame == 2 and len(shot.frames) == 6, "bow_shot: release on frame 2 of six")


def write_unit(root: Path, uid: str, kind: str, rows: list[str], mask: list[str], joints: dict, actions: dict,
               down: list[str], down_foot: list[int], swaps: dict | None = None, extra: dict | None = None) -> Path:
    unit = root / "sprites/source" / uid
    unit.mkdir(parents=True)
    files = {"grid.txt": rows, "parts_mask.txt": mask, "down.txt": down}
    files.update({f"swap_{k.replace('@', '_')}.txt": v for k, v in (swaps or {}).items()})
    for name, content in files.items():
        (unit / name).write_text("\n".join(content) + "\n", encoding="utf-8")
    (unit / "palette.json").write_bytes(json_bytes({"colors": PALETTE}))
    (unit / "unit.json").write_bytes(json_bytes({"id": uid, "kind": kind, "equipment": [], "design": "a test animal",
                                                 "actions": actions, **(extra or {})}))
    (unit / "parts.json").write_bytes(json_bytes({
        "mask": "parts_mask.txt", "joints": joints, "added": {},
        "swaps": {k: f"swap_{k.replace('@', '_')}.txt" for k in (swaps or {})},
        "variants": {"down": {"file": "down.txt", "foot": down_foot}}}))
    return unit


def rig_actions(rig: str) -> dict:
    return {a: f"{rig}_{a}" for a in ("idle", "attack", "hit", "down")}


def test_rig_builds(tmp: Path, real: dict) -> None:
    root = tmp / "rigs"
    project = tiny_project(real)
    for key in ("rigs", "templates", "required_actions"):
        project[key].update(copy.deepcopy(real[key]))
    project["kinds"].update(
        tinybeast={"cell": [24, 10], "height": 5, "foot": [6, 8], "reference_unit": None, "rig": "beast"},
        tinyflyer={"cell": [20, 14], "height": 3, "foot": [6, 12], "reference_unit": None, "rig": "flyer"})
    project["rigs"]["flyer"]["air_height"] = 6  # the air point at row 8 of the 14-row test cell
    write_project(root, project)
    write_unit(root, "TB1", "tinybeast", BEAST_GRID, BEAST_MASK, {"foot": [1, 4]}, rig_actions("beast"),
               BEAST_DOWN, [1, 1])
    flyer = write_unit(root, "TF1", "tinyflyer", FLYER_GRID, FLYER_MASK, {"foot": [1, 3]}, rig_actions("flyer"),
                       FLYER_DOWN, [1, 0], FLYER_SWAPS)
    out = root / real["paths"]["output"]

    def built(uid: str) -> tuple[dict, dict, np.ndarray]:
        r = run_cli(root, "build", uid, "--force")
        expect(r.returncode == 0, f"build {uid} failed: {r.stdout}{r.stderr}")
        motion = json.loads((out / uid / f"{uid}_motion.json").read_text())
        report = json.loads((out / uid / f"{uid}_report.json").read_text())
        return motion, report, np.array(Image.open(out / uid / f"{uid}_sheet.png"))

    motion, report, _ = built("TB1")
    timing = {a["name"]: (len(a["frames"]), a["strike_frame"], a["frame_ms"]) for a in motion["actions"]}
    expect(timing == {"idle": (4, -1, 200), "attack": (6, 2, 70), "hit": (2, -1, 70), "down": (1, -1, 70)},
           f"beast motion table {timing}")
    checks = {c["name"]: c["passed"] for c in report["checks"]}
    expect(checks.get("feet_on_baseline") is True and "air_point" not in checks, f"beast checks {checks}")
    unit_json = root / "sprites/source/TB1/unit.json"
    data = json.loads(unit_json.read_text())
    unit_json.write_bytes(json_bytes({**data, "overrides": {"idle": {"frames": {"1": {"offsets": {"back_legs": [0, -1]}}}}}}))
    r = run_cli(root, "build", "TB1", "--force")
    expect(r.returncode != 0 and "feet_on_baseline" in r.stderr and "idle_1=7" in r.stderr,
           f"lifted back legs not caught: {r.stderr}")

    motion, report, sheet = built("TF1")
    timing = {a["name"]: (len(a["frames"]), a["strike_frame"], a["frame_ms"]) for a in motion["actions"]}
    expect(timing == {"idle": (4, -1, 200), "attack": (6, 2, 70), "hit": (2, -1, 70), "down": (1, -1, 70)},
           f"flyer motion table {timing}")
    # Measured on the mid-wing base pose (3 rows), not idle frame 0's raised wings (4 rows).
    expect(report["height"] == 3, f"flyer height {report['height']}")
    cw, ch = motion["cell"]["width"], motion["cell"]["height"]
    checks = {c["name"]: c["passed"] for c in report["checks"]}
    expect(checks.get("air_point") is True and "feet_on_baseline" not in checks, f"flyer checks {checks}")
    rows = {a["name"]: a for a in motion["actions"]}

    def lowest(action: str, i: int, color) -> int:
        f = rows[action]["frames"][i]
        cell = sheet[f["y"]:f["y"] + ch, f["x"]:f["x"] + cw]
        return int(np.nonzero((cell[..., :3] == color).all(axis=2) & (cell[..., 3] == 255))[0].max())

    torso = tuple(int(PALETTE["a"][i:i + 2], 16) for i in (1, 3, 5))
    # The body hovers at the air point (14 - 6 = row 8), one row up on the bob frames; the down pose lands on the foot.
    expect([lowest("idle", i, torso) for i in range(4)] == [8, 7, 7, 8], "flyer idle body rows")
    expect(lowest("down", 0, torso) == 12, "the flyer's down pose does not land on the foot point")
    data = json.loads((flyer / "unit.json").read_text())
    (flyer / "unit.json").write_bytes(json_bytes({**data, "overrides": {"idle": {"frames": {"1": {"offsets": {"torso": [0, -2]}}}}}}))
    r = run_cli(root, "build", "TF1", "--force")
    expect(r.returncode != 0 and "air_point" in r.stderr and "idle_1=6" in r.stderr,
           f"a body off the air point not caught: {r.stderr}")


TESTS = [
    ("rigs: beast parts, draw order, breathing and attack template on a fixed grid",
     lambda real_ctx, tmp: test_beast_assembly(real_ctx)),
    ("rigs: flyer draw order, wing swaps and bob, humanoid bow under the front hand and out of the height",
     lambda real_ctx, tmp: test_flyer_and_bow_assembly(real_ctx)),
    ("rigs: beast and flyer build, feet baseline vs air point, flyer lands when down",
     lambda real_ctx, tmp: test_rig_builds(tmp, real_ctx.project)),
]
