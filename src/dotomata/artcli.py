"""The `art` and `bg` commands of the dotomata CLI."""

from __future__ import annotations

from . import art as art_mod
from . import background as bg_mod
from .codexrun import mark_invalid
from .common import json_bytes, load_unit


def _print_runs(result: dict) -> int:
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
            print(f"  candidate: {run['candidate']} (preview folder, not committed until adopted)")
        if e["note"]:
            print(f"  note: {e['note']}")
        if e["error"]:
            print(f"  error: {e['error']}")
            failed = True
    print(f"total: {result['used']} of {result['cap']}")
    print(f"comparison sheet: {result['sheet']} (kept in the source)")
    return 1 if failed else 0


def cmd_art(ctx, args) -> int:
    unit = load_unit(ctx, args.unit) if hasattr(args, "unit") else None
    if args.art_command == "generate":
        return _print_runs(art_mod.generate_art(ctx, unit, args.note or [], args.batch, args.variant or []))
    if args.art_command == "dot-ref":
        print(f"wrote {art_mod.write_dot_reference(ctx, unit, args.force).relative_to(ctx.root)}")
        return 0
    if args.art_command == "adopt":
        entry = art_mod.adopt_art(ctx, unit, args.candidate, args.note or "", art_mod.rect_from(args.bust), args.stage)
        print(json_bytes(entry).decode(), end="")
        if entry["bust"] is None:
            print(f"next: art bust {unit.path} --rect X Y W H (a square of at least "
                  f"{ctx.project['art']['bust_min_side']} px with the face in the middle)")
        return 0
    if args.art_command == "invalidate":
        print(json_bytes(mark_invalid(art_mod.art_target(ctx, unit), args.candidate, args.reason)).decode(), end="")
        return 0
    if args.art_command == "bust":
        print(json_bytes(art_mod.set_bust(ctx, unit, art_mod.rect_from(args.rect), args.stage)).decode(), end="")
        return 0
    if args.art_command == "status":
        print(json_bytes(art_mod.art_status(ctx, unit)).decode(), end="")
        return 0
    if args.art_command == "build":
        for path in args.units:
            u = load_unit(ctx, path)
            written, how = art_mod.build_art(ctx, u, args.force)
            for p in written:
                print(f"wrote {p.relative_to(ctx.root)} ({how})")
        return 0
    raise AssertionError(args.art_command)


def cmd_bg(ctx, args) -> int:
    if args.bg_command == "build":
        for bg_id in args.ids:
            written, how = bg_mod.build_bg(ctx, bg_mod.load_background(ctx, bg_id), args.force)
            for p in written:
                print(f"wrote {p.relative_to(ctx.root)} ({how})")
        return 0
    bg = bg_mod.load_background(ctx, args.id)
    if args.bg_command == "generate":
        return _print_runs(bg_mod.generate_bg(ctx, bg, args.note or [], args.batch, args.variant or []))
    if args.bg_command == "adopt":
        print(json_bytes(bg_mod.adopt_bg(ctx, bg, args.candidate, args.note or "")).decode(), end="")
        return 0
    if args.bg_command == "invalidate":
        print(json_bytes(mark_invalid(bg_mod.bg_target(ctx, bg), args.candidate, args.reason)).decode(), end="")
        return 0
    if args.bg_command == "status":
        print(json_bytes(bg_mod.bg_status(ctx, bg)).decode(), end="")
        return 0
    raise AssertionError(args.bg_command)


def _generate_options(p) -> None:
    p.add_argument("--batch", type=int, default=1, metavar="N", help="run N codex executions at once, one image each")
    p.add_argument("--variant", action="append",
                   help="extra prompt line for one run: the i-th --variant goes to run i (repeatable)")
    p.add_argument("--note", action="append", help="what to change after a rejected batch (repeatable)")


def _invalidate_options(p, owner: str) -> None:
    p.add_argument(owner)
    p.add_argument("--candidate", required=True, help="the recorded candidate, e.g. art_candidate_2.png")
    p.add_argument("--reason", required=True, help="why it is not this run's image (kept in the record)")


def add_commands(sub) -> None:
    a = sub.add_parser("art", help="card art: generate, dot-ref, adopt, invalidate, bust, status, build")
    asub = a.add_subparsers(dest="art_command", required=True)
    g = asub.add_parser("generate", help="card art candidates through codex (preview folder), sheet in the source")
    g.add_argument("unit")
    _generate_options(g)
    d = asub.add_parser("dot-ref", help="source/<unit>/art/dot_ref.png from idle frame 0 of the built sheet")
    d.add_argument("unit")
    d.add_argument("--force", action="store_true")
    ad = asub.add_parser("adopt", help="record the user's pick in art.json and copy it into the source")
    ad.add_argument("unit")
    ad.add_argument("--candidate", required=True, help="e.g. art_candidate_2.png")
    ad.add_argument("--note", required=True, help="the user's reason")
    ad.add_argument("--bust", type=int, nargs=4, metavar=("X", "Y", "W", "H"), help="square bust crop in art pixels")
    ad.add_argument("--stage", default=art_mod.STAGE)
    iv = asub.add_parser("invalidate", help="mark a recorded candidate as not its run's image; adopt then refuses it")
    _invalidate_options(iv, "unit")
    b = asub.add_parser("bust", help="set the bust crop of the current adoption")
    b.add_argument("unit")
    b.add_argument("--rect", type=int, nargs=4, metavar=("X", "Y", "W", "H"), required=True)
    b.add_argument("--stage", default=art_mod.STAGE)
    s = asub.add_parser("status", help="generation count, cap and current adoption")
    s.add_argument("unit")
    bu = asub.add_parser("build", help="write full and bust PNGs to paths.art_output (placeholder without art.json)")
    bu.add_argument("units", nargs="+")
    bu.add_argument("--force", action="store_true")

    bg = sub.add_parser("bg", help="backgrounds: generate, adopt, invalidate, status, build")
    bsub = bg.add_subparsers(dest="bg_command", required=True)
    g = bsub.add_parser("generate", help="background candidates through codex (preview folder), sheet in the source")
    g.add_argument("id")
    _generate_options(g)
    ad = bsub.add_parser("adopt", help="record the user's pick in bg.json and copy it into the source")
    ad.add_argument("id")
    ad.add_argument("--candidate", required=True, help="e.g. bg_candidate_1.png")
    ad.add_argument("--note", required=True, help="the user's reason")
    iv = bsub.add_parser("invalidate", help="mark a recorded candidate as not its run's image; adopt then refuses it")
    _invalidate_options(iv, "id")
    s = bsub.add_parser("status", help="generation count, cap and current adoption")
    s.add_argument("id")
    bu = bsub.add_parser("build", help="cover-fit to bg.size into paths.bg_output (placeholder without bg.json)")
    bu.add_argument("ids", nargs="+")
    bu.add_argument("--force", action="store_true")
