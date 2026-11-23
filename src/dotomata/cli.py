"""dotomata: battle sprite, card art and background pipeline. Usage: dotomata [--root DIR] [--config FILE] <command> ...

Every path in the config is relative to --root (default: the current directory); the config is
<root>/dotomata.json unless --config names another file. `dotomata init` writes the default config.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from importlib import resources
from pathlib import Path

import numpy as np

from . import art as art_mod
from . import artcli
from . import background as bg_mod
from . import build as build_mod
from . import generate as gen_mod
from . import refadopt
from .assemble import assemble_unit
from .common import (CONFIG_NAME, Context, SpriteError, grid_bytes, json_bytes, list_units, load_context, load_unit,
                     opaque, parse_hex, read_json, render_rgba, sha256_file, to_hex, write_new)
from .convert import convert_image
from .fixes import append_edits, edits_from_png
from .imaging import flatten, load_rgba, png_bytes, zoom
from .parts import init_parts, load_art, load_fixed
from .reduce import reduce_grid, reference_image


def preview_dir(ctx: Context, unit_path: str) -> Path:
    d = ctx.preview_dir / unit_path
    d.mkdir(parents=True, exist_ok=True)
    return d


def zoom_png(ctx: Context, rgba: np.ndarray) -> bytes:
    p = ctx.project["preview"]
    flat = flatten(zoom(rgba, p["zoom"]), parse_hex(p["background"], "preview background"))
    return png_bytes(np.concatenate([flat, np.full(flat.shape[:2] + (1,), 255, np.uint8)], axis=2))


# ---------------------------------------------------------------- commands


def cmd_generate(ctx, args):
    unit = load_unit(ctx, args.unit)
    if args.design_from:
        reference, mode = unit.source / args.design_from, "design"
    elif args.design_from_file:
        given = Path(args.design_from_file)
        # A bare file name means the unit folder, like --design-from.
        reference, mode = (given if given.is_absolute() or given.exists() else unit.source / given), "design-file"
    elif args.from_art:
        reference = gen_mod.adopted_art(unit)
        if reference is None:
            raise SpriteError(f"unit '{unit.id}' has no adopted card art (art/art.json); adopt its art first")
        mode = "art"
    elif args.reference:
        reference, mode = Path(args.reference), "scale"
    else:
        reference, mode = None, None
    result = gen_mod.generate(ctx, unit, reference, args.note or [], mode, args.batch, args.variant or [])
    failed = False
    for i, run in enumerate(result["runs"], 1):
        e = run["entry"]
        print(f"run {i}" + (f" (variant: {e['variant']})" if e["variant"] else "") + ":")
        print(f"  image generation calls: {e['image_generation_calls']} "
              f"(json items {run['counted']['json_items']}, rollout {run['counted']['rollout_calls']}, "
              f"generated files {len(run['counted']['generated_files'])}; "
              f"image from {run['counted']['image_source'] or 'nowhere'})")
        print(f"  codex session events: {run['workdir']}/codex_events.jsonl (not committed)")
        if run["candidate"]:
            print(f"  candidate: {run['candidate']}")
            if e["height_check"] is not None:
                note = {"PASS": "", "FAIL": " (drop before showing)",
                        "REVIEW": " (taller, but within the equipment allowance and the full height includes the "
                                  "weapon/staff/bow (the rig's height_excludes); judge head-to-foot at checkpoint 1)"}
                print(f"  measured height {e['measured_height']} vs target {e['height_target']} "
                      f"+-{ctx.project['height_tolerance']}: {e['height_check']}{note[e['height_check']]}")
        if e["note"]:
            print(f"  note: {e['note']}")
        if e["error"]:
            print(f"  error: {e['error']}")
            failed = True
    print(f"unit total: {result['used']} of {result['cap']}")
    print(f"comparison sheet: {result['sheet']}")
    return 1 if failed else 0


def cmd_convert(ctx, args):
    unit = load_unit(ctx, args.unit)
    adopted = None
    if args.input:
        src = Path(args.input)
    elif args.candidate:
        src = unit.source / args.candidate
    else:
        src, adopted = gen_mod.adopted_source(unit)
    if not src.is_file():
        raise SpriteError(f"input image not found: {src}")
    if adopted is not None and adopted.get("kind") == refadopt.KIND:
        if sha256_file(src) != adopted["reference_sha256"]:
            raise SpriteError(f"unit '{unit.id}': {src.name} does not match the sha256 adopted at checkpoint 1")
        grid, palette, merged_from, info, conv_json = convert_reduced_ref(unit, src.name)
    else:
        rgb, alpha = load_rgba(src)
        grid, palette, merged_from, info = convert_image(rgb, alpha, ctx.project["convert"], ctx.project["palette"])
        conv_json = {
            "source": src.name if src.parent == unit.source else str(src),
            "source_sha256": sha256_file(src),
            "convert": ctx.project["convert"],
            "palette": ctx.project["palette"],
            "result": info,
            "tool_versions": tool_versions(),
        }
    pal_json = {"colors": {s: to_hex(c) for s, c in palette.items()}, "merged_from": merged_from}
    out = preview_dir(ctx, unit.path) if args.preview else unit.source
    force = args.force or args.preview
    existing = [n for n in ("grid.txt", "palette.json", "convert.json") if (out / n).exists()]
    if existing and not force:
        raise SpriteError(f"refusing to overwrite {existing} in {out} (pass --force to replace them)")
    write_new(out / "grid.txt", grid_bytes(grid), force)
    write_new(out / "palette.json", json_bytes(pal_json), force)
    write_new(out / "convert.json", json_bytes(conv_json), force)
    rows = np.nonzero(opaque(grid).any(axis=1))[0]
    zp = preview_dir(ctx, unit.path) / "convert_zoom.png"
    zp.write_bytes(zoom_png(ctx, render_rgba(grid, palette)))
    print(f"pixel size {info['pixel_size']}, grid {info['grid_size'][0]}x{info['grid_size'][1]}, "
          f"{info['colors']} colors, height {rows.max() - rows.min() + 1}, "
          f"pink edge cells {info['pink_edge_cells']}/{info['edge_cells']}")
    print(f"wrote {out}/grid.txt, palette.json, convert.json; preview {zp}")
    return 0


def convert_reduced_ref(unit, name: str):
    """Convert of an adopted reduced reference: read at its block size, symbols of its source."""
    grid, palette, merged_from, info, rec = refadopt.convert_ref(unit, name)
    conv_json = {
        "kind": refadopt.KIND,
        "source": name,
        "source_sha256": rec["sha256"],
        "reduce_record": name[:-len(".png")] + ".json",
        "reduced_from": rec["source"],
        "reduced_from_sha256": rec["source_sha256"],
        "convert": rec["convert"],
        "palette": rec["palette"],
        "reduce": rec["reduce"],
        "result": info,
        "tool_versions": tool_versions(),
    }
    return grid, palette, merged_from, info, conv_json


def cmd_recheck(ctx, args):
    unit = load_unit(ctx, args.unit)
    for name, old, new in gen_mod.recheck_heights(ctx, unit, args.candidates):
        print(f"{name}: was {old['measured_height']} {old['height_check']}, now {new['measured_height']} "
              f"vs {new['height_target']} +-{ctx.project['height_tolerance']}: {new['height_check']}")
    print(f"rewrote only {', '.join(gen_mod.HEIGHT_FIELDS)} in {unit.source / 'generation.json'}")
    return 0


def cmd_reduce(ctx, args):
    """Size pass: candidate -> native grid (convert, in memory) -> target height -> ref_*.png + ref_*.json."""
    unit = load_unit(ctx, args.unit)
    src = unit.source / args.candidate
    gen_path = unit.source / "generation.json"
    recorded = read_json(gen_path)["generations"] if gen_path.is_file() else []
    if not any(g.get("candidate") == args.candidate for g in recorded) or not src.is_file():
        raise SpriteError(f"unit '{unit.id}': {args.candidate} is not a recorded candidate in {unit.source}")
    # Same target as the height check: the unit's own height, the kind's, or the reference unit's built height.
    height = args.height if args.height is not None else build_mod.reference_height(ctx, unit)[0]
    cfg = ctx.project["reduce"]
    try:
        rgb, alpha = load_rgba(src)
    except OSError as e:
        raise SpriteError(f"unit '{unit.id}': cannot read {args.candidate}: {e}") from e
    grid, palette, _, info = convert_image(rgb, alpha, ctx.project["convert"], ctx.project["palette"])
    small = reduce_grid(grid, palette, height, cfg)
    image = reference_image(small, palette, cfg)
    stem = f"ref_{height}px_x{cfg['ref_block']}"
    data = png_bytes(image)
    used = sorted(set(small.ravel().tolist()) - {"."})
    record = {
        "image": f"{stem}.png",
        "sha256": hashlib.sha256(data).hexdigest(),
        "image_size": [int(image.shape[1]), int(image.shape[0])],
        "source": args.candidate,
        "source_sha256": sha256_file(src),
        "height": height,
        "native": {"pixel_size": info["pixel_size"], "grid_size": info["grid_size"], "colors": info["colors"]},
        "grid_size": [int(small.shape[1]), int(small.shape[0])],
        "colors": len(used),
        "convert": ctx.project["convert"],
        "palette": ctx.project["palette"],
        "reduce": cfg,
        "tool_versions": tool_versions(),
    }
    targets = [unit.source / f"{stem}.png", unit.source / f"{stem}.json"]
    existing = [t.name for t in targets if t.exists()]
    if existing and not args.force:
        raise SpriteError(f"refusing to overwrite {existing} in {unit.source} (pass --force to replace them)")
    write_new(targets[0], data, args.force)
    write_new(targets[1], json_bytes(record), args.force)
    out = preview_dir(ctx, unit.path)
    (out / f"{stem}_grid.txt").write_bytes(grid_bytes(small))
    print(f"native: pixel size {info['pixel_size']}, grid {info['grid_size'][0]}x{info['grid_size'][1]}, "
          f"{info['colors']} colors")
    print(f"reduced: grid {small.shape[1]}x{small.shape[0]} (target height {height}), {len(used)} colors")
    if small.shape[0] != height:
        print(f"warning: the reduced figure is {small.shape[0]} rows tall, not {height} (edge rows went transparent)")
    print(f"wrote {targets[0]} ({image.shape[1]}x{image.shape[0]}, block {cfg['ref_block']}) and {targets[1].name}; "
          f"grid {out / (stem + '_grid.txt')}")
    print(f"next: generate {unit.path} --batch N --design-from-file {stem}.png --variant ...")
    return 0


def tool_versions() -> dict:
    import PIL
    return {"python": f"{sys.version_info.major}.{sys.version_info.minor}", "pillow": PIL.__version__,
            "numpy": np.__version__}


def cmd_fix(ctx, args):
    unit = load_unit(ctx, args.unit)
    if args.from_png:
        if not (args.category and args.reason):
            raise SpriteError("--from-png needs --category and --reason")
        fixed, palette, _ = load_fixed(ctx, unit)
        rgb, alpha = load_rgba(Path(args.from_png))
        a = alpha if alpha is not None else np.full(rgb.shape[:2], 255, np.uint8)
        rgba = np.concatenate([rgb, a[..., None]], axis=2)
        edits = edits_from_png(fixed, palette, rgba, args.category, args.reason, f"unit '{unit.id}'")
        append_edits(unit.source / "fixes.json", edits)
        print(f"appended {sum(len(e['cells']) for e in edits)} cell edits to fixes.json")
    fixed, palette, counts = load_fixed(ctx, unit)
    total = int(opaque(fixed).sum())
    out = preview_dir(ctx, unit.path)
    (out / "fixed.txt").write_bytes(grid_bytes(fixed))
    (out / "fixed_zoom.png").write_bytes(zoom_png(ctx, render_rgba(fixed, palette)))
    # fixed_zoom.png sits on an opaque background for viewing; the transparent
    # copy is the one a user can edit and hand back to --from-png.
    (out / "fixed_edit.png").write_bytes(png_bytes(zoom(render_rgba(fixed, palette), ctx.project["preview"]["zoom"])))
    changed = sum(counts.values())
    print(f"hand fixes: {changed} of {total} opaque cells ({100.0 * changed / max(total, 1):.2f}%)")
    for c, n in counts.items():
        if n:
            print(f"  {c}: {n} ({100.0 * n / max(total, 1):.2f}%)")
    print(f"preview {out}/fixed_zoom.png, edit and hand back {out}/fixed_edit.png, grid {out}/fixed.txt")
    return 0


def cmd_parts(ctx, args):
    unit = load_unit(ctx, args.unit)
    if args.init:
        for name in init_parts(ctx, unit, args.force):
            print(f"wrote {unit.source / name}")
        return 0
    art = load_art(ctx, unit)
    out = preview_dir(ctx, unit.path)
    h, w = art.fixed.shape
    names = [n for n in ctx.project["rigs"][art.rig_name]["draw_order"] if n in art.layers]
    strip = np.zeros((h, (w + 2) * len(names), 4), dtype=np.uint8)
    for i, name in enumerate(names):
        strip[:, i * (w + 2): i * (w + 2) + w] = render_rgba(art.layers[name], art.palette)
    (out / "parts_zoom.png").write_bytes(zoom_png(ctx, strip))
    print("layers (left to right): " + ", ".join(names))
    for group, entries in art.new_cells.items():
        for name, n in entries.items():
            print(f"  new cells {group} {name}: {n}")
    print(f"preview {out}/parts_zoom.png")
    return 0


def cmd_assemble(ctx, args):
    unit = load_unit(ctx, args.unit)
    art = load_art(ctx, unit)
    files, results = build_mod.render_outputs(ctx, unit, art, assemble_unit(ctx, unit, art))
    out = preview_dir(ctx, unit.path)
    for name, data in files.items():
        (out / name).write_bytes(data)
    for name, ok, detail in results:
        print(f"  {'PASS' if ok else 'FAIL'} {name}: {detail}")
    print(f"preview {out}/{unit.id}_zoom.png and {out}/{unit.id}_preview.gif (nothing written to paths.output)")
    return 0 if all(ok for _, ok, _ in results) else 1


def cmd_build(ctx, args, swap: bool):
    for path in args.units:
        unit = load_unit(ctx, path)
        if not build_mod.has_sprite(unit):
            print(f"skip {unit.id}: no sprite (no actions in unit.json)")
            continue
        is_swap = bool(unit.data.get("palette_swap"))
        if is_swap != swap:
            raise SpriteError(f"unit '{unit.id}' " + ("is a palette swap: use palette-swap" if is_swap
                                                       else "is not a palette swap: use build"))
        for p in build_mod.build_unit(ctx, unit, ctx.output_dir / unit.path, args.force):
            print(f"wrote {p.relative_to(ctx.root)}")
        # A recolour unit (palette swap) has no dot reference.
        dot = None if swap else art_mod.refresh_dot_reference(ctx, unit)
        if dot is not None:
            print(f"refreshed {dot.relative_to(ctx.root)} from the new sheet")
    return 0


def cmd_verify(ctx, args):
    """Sprites, card art and backgrounds. Unit paths limit it to those units (sprites and
    card art); backgrounds/<id> picks a background."""
    units = [u for u in args.units if not u.startswith(bg_mod.FOLDER + "/")]
    if args.units:
        bg_ids = [u.split("/", 1)[1] for u in args.units if u.startswith(bg_mod.FOLDER + "/")]
        with_art = set(art_mod.art_units(ctx))
        problems = build_mod.verify(ctx, units) if units else []
        problems += art_mod.verify_art(ctx, [u for u in units if u in with_art])
        problems += bg_mod.verify_bg(ctx, bg_ids)
    else:
        problems = build_mod.verify(ctx, None) + art_mod.verify_art(ctx, None) + bg_mod.verify_bg(ctx, None)
    if args.convert:
        problems += verify_convert(ctx, units)
    for p in problems:
        print(f"FAIL {p}")
    print("verify: " + ("OK" if not problems else f"{len(problems)} problem(s)"))
    return 0 if not problems else 1


def verify_convert(ctx, unit_paths) -> list[str]:
    """Re-runs convert from the committed AI image and compares the grid source."""
    problems = []
    for path in unit_paths or list_units(ctx):
        before = len(problems)
        unit = load_unit(ctx, path)
        conv = unit.source / "convert.json"
        if not conv.is_file():
            continue
        data = read_json(conv)
        if data.get("kind") == refadopt.KIND:
            try:
                grid, palette, merged_from, _, _ = convert_reduced_ref(unit, data["source"])
            except SpriteError as e:
                problems.append(f"{path}: {e}")
                continue
        else:
            rgb, alpha = load_rgba(unit.source / data["source"])
            grid, palette, merged_from, info = convert_image(rgb, alpha, data["convert"], data["palette"])
        if grid_bytes(grid) != (unit.source / "grid.txt").read_bytes():
            problems.append(f"{path}: convert of {data['source']} no longer gives grid.txt")
        pal_json = {"colors": {s: to_hex(c) for s, c in palette.items()}, "merged_from": merged_from}
        if json_bytes(pal_json) != (unit.source / "palette.json").read_bytes():
            problems.append(f"{path}: convert of {data['source']} no longer gives palette.json")
        if data["convert"] != ctx.project["convert"] or data["palette"] != ctx.project["palette"]:
            print(f"note {path}: converted with older settings than the config (recorded in convert.json)")
        if data.get("tool_versions") != tool_versions():
            problems.append(f"{path}: converted with {data.get('tool_versions')}, running {tool_versions()}")
        if len(problems) == before:
            print(f"ok {path}: convert reproduces grid.txt and palette.json")
    return problems


def cmd_checkpoint(ctx, args):
    unit = load_unit(ctx, args.unit)
    if args.require is not None:
        if not gen_mod.is_approved(unit, args.require):
            print(f"unit '{unit.id}': checkpoint {args.require} is not approved; stop and ask the main conversation")
            return 1
        print(f"unit '{unit.id}': checkpoint {args.require} approved")
        return 0
    if args.record is not None:
        if args.approve == args.reject:
            raise SpriteError("pass exactly one of --approve or --reject")
        if args.reference:
            # The size-pass fallback: the user adopts a reduced reference instead of a candidate.
            if args.record != 1 or not args.approve or args.candidate:
                raise SpriteError("--reference adopts a reduced reference: use it with --record 1 --approve "
                                  "and without --candidate")
            entry = gen_mod.append_checkpoint(unit, refadopt.checkpoint_entry(unit, args.reference, args.note or ""))
            print(json_bytes(entry).decode())
            return 0
        entry = gen_mod.record_checkpoint(unit, args.record, "approved" if args.approve else "rejected",
                                          args.note or "", args.candidate)
        print(json_bytes(entry).decode())
        return 0
    calls, candidates = build_mod.generation_calls(unit)
    status = {str(k): v for k, v in sorted(gen_mod.checkpoint_status(unit).items())}
    # "approved" is what gates the next segment: a later approval made before an
    # earlier checkpoint was redone no longer counts.
    approved = {str(n): gen_mod.is_approved(unit, n) for n in (1, 2, 3)}
    print(json_bytes({"unit": unit.id, "image_generation_calls": calls,
                      "cap": ctx.project["generation"]["cap_per_unit"], "candidates": candidates,
                      "approved": approved, "checkpoints": status}).decode())
    return 0


def cmd_selftest(args):
    from .selftest import run
    return run(args.config.resolve() if args.config is not None else None)


def default_config_bytes() -> bytes:
    return resources.files(__package__).joinpath("default_config.json").read_bytes()


def cmd_init(args):
    path = args.config if args.config is not None else args.root / CONFIG_NAME
    write_new(path, default_config_bytes(), args.force)
    print(f"wrote {path}")
    # Candidates and previews are large and regenerable; keep them out of git.
    preview = Path(json.loads(default_config_bytes())["paths"]["preview"])
    ignore = args.root / preview.parent / ".gitignore"
    if not ignore.exists():
        write_new(ignore, f"{preview.name}/\n".encode(), False)
        print(f"wrote {ignore}")
    return 0


def main(argv=None) -> int:
    p = argparse.ArgumentParser(prog="dotomata", description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--root", type=Path, default=Path("."),
                   help="project root; every path in the config is relative to it (default: the current directory)")
    p.add_argument("--config", type=Path, help=f"config file, relative to the current directory (default: <root>/{CONFIG_NAME})")
    sub = p.add_subparsers(dest="command", required=True)

    i = sub.add_parser("init", help=f"write the default config to <root>/{CONFIG_NAME} (or --config)")
    i.add_argument("--force", action="store_true", help="replace an existing config")

    g = sub.add_parser("generate", help="segment 1: AI base poses through codex, one image per run (cap per unit)")
    g.add_argument("unit")
    ref = g.add_mutually_exclusive_group()
    ref.add_argument("--reference", help="optional scale reference image (not used for a new character's design pass)")
    ref.add_argument("--design-from", metavar="CANDIDATE",
                     help="redraw this unit from one of its own candidates (e.g. candidate_1.png), same design")
    ref.add_argument("--design-from-file", metavar="PATH",
                     help="redraw this unit from a prepared image inside its source folder (e.g. ref_80px_x12.png)")
    ref.add_argument("--from-art", action="store_true",
                     help="design pass for a unit with card art: draw it from its adopted card art (art/art.json)")
    g.add_argument("--note", action="append", help="what to change after a rejected candidate (repeatable)")
    g.add_argument("--batch", type=int, default=1, metavar="N",
                   help="run N codex executions at once, one image each (1..generation.batch_max)")
    g.add_argument("--variant", action="append",
                   help="extra prompt line for one run: the i-th --variant goes to run i (repeatable)")

    c = sub.add_parser("convert", help="AI image -> grid.txt, palette.json, convert.json")
    c.add_argument("unit")
    c.add_argument("--candidate", help="candidate file in the unit folder (default: adopted at checkpoint 1)")
    c.add_argument("--input", help="any image path (testing)")
    c.add_argument("--preview", action="store_true", help="write to the preview folder instead of the source")
    c.add_argument("--force", action="store_true")

    f = sub.add_parser("fix", help="apply fixes.json and count hand edits; --from-png turns an edited PNG into edits")
    f.add_argument("unit")
    f.add_argument("--from-png")
    f.add_argument("--category")
    f.add_argument("--reason")

    pa = sub.add_parser("parts", help="split into rig parts and equipment layers (--init writes a mask skeleton)")
    pa.add_argument("unit")
    pa.add_argument("--init", action="store_true")
    pa.add_argument("--force", action="store_true")

    a = sub.add_parser("assemble", help="build frames and previews into the preview folder, run checks")
    a.add_argument("unit")

    b = sub.add_parser("build", help="write sheets, mask, previews, motion table and report to paths.output")
    b.add_argument("units", nargs="+")
    b.add_argument("--force", action="store_true")

    s = sub.add_parser("palette-swap", help="build a recolor unit (palette_map.json over its base unit)")
    s.add_argument("units", nargs="+")
    s.add_argument("--force", action="store_true")

    v = sub.add_parser("verify", help="rebuild sprites, card art and backgrounds and compare pixels with committed outputs")
    v.add_argument("units", nargs="*")
    v.add_argument("--convert", action="store_true", help="also re-run convert from the committed AI image")

    k = sub.add_parser("checkpoint", help="show, record or require a user checkpoint")
    k.add_argument("unit")
    k.add_argument("--record", type=int, metavar="N")
    k.add_argument("--approve", action="store_true")
    k.add_argument("--reject", action="store_true")
    k.add_argument("--note")
    k.add_argument("--candidate")
    k.add_argument("--reference", metavar="REF_PNG",
                   help="with --record 1 --approve: adopt a reduce output (ref_*.png) instead of a candidate, "
                        "when the size pass keeps failing and the user chooses it")
    k.add_argument("--require", type=int, metavar="N")

    rc = sub.add_parser("recheck", help="measure recorded candidates again; rewrites only their height fields")
    rc.add_argument("unit")
    rc.add_argument("candidates", nargs="+", metavar="CANDIDATE", help="e.g. candidate_4.png")

    r = sub.add_parser("reduce", help="size pass: a design candidate at its native grid -> target height -> "
                                      "ref_<height>px_x<block>.png for generate --design-from-file")
    r.add_argument("unit")
    r.add_argument("--candidate", required=True, help="the design picked at checkpoint 1a (e.g. candidate_1.png)")
    r.add_argument("--height", type=int,
                   help="target height in art px for the whole figure (default: the height check's target)")
    r.add_argument("--force", action="store_true")

    sub.add_parser("selftest", help="run the pipeline's own checks (exits non-zero on failure)")
    artcli.add_commands(sub)

    args = p.parse_args(argv)
    try:
        # init and selftest need no config: init writes it, selftest builds its own temp trees.
        if args.command == "init":
            return cmd_init(args)
        if args.command == "selftest":
            return cmd_selftest(args)
        root = args.root.resolve()
        ctx = load_context(root, args.config.resolve() if args.config is not None else None)
        handlers = {
            "generate": cmd_generate, "convert": cmd_convert, "fix": cmd_fix, "parts": cmd_parts,
            "assemble": cmd_assemble, "verify": cmd_verify, "checkpoint": cmd_checkpoint,
            "recheck": cmd_recheck, "reduce": cmd_reduce, "art": artcli.cmd_art, "bg": artcli.cmd_bg,
            "build": lambda c_, a_: cmd_build(c_, a_, swap=False),
            "palette-swap": lambda c_, a_: cmd_build(c_, a_, swap=True),
        }
        return handlers[args.command](ctx, args)
    except SpriteError as e:
        print(f"error: {e}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
