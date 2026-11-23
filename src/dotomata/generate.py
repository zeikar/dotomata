"""Step 1: AI base poses through `codex exec` (codexrun: one image per
codex run, up to batch_max runs at once, a hard cap per unit), plus the user
checkpoint log (checkpoints.json)."""

from __future__ import annotations

import datetime
from pathlib import Path

import numpy as np

from .build import reference_height
from .codexrun import Target, check_banned, check_batch, display_path, run_batch, write_sheet
from .common import Context, SpriteError, Unit, drawn_height, json_bytes, read_json, sha256_file
from .convert import convert_image
from .imaging import load_rgba

# --reference is an optional scale reference (not for a new character's design pass); --design-from redraws this unit from its own candidate;
# --design-from-file redraws it from a prepared image (e.g. an upscaled small sprite) in its folder; --from-art
# draws it from its adopted card art (its sentence is picked per kind, see reference_sentence).
REFERENCE_SENTENCES = {"scale": "scale_reference_sentence", "design": "design_reference_sentence",
                       "design-file": "design_file_sentence", "art": "art_reference_sentences"}


def sprite_target(ctx: Context, unit: Unit) -> Target:
    gen = ctx.project["generation"]
    return Target(label=f"unit '{unit.id}'", header={"unit": unit.id}, record=unit.source / "generation.json",
                  out_dir=unit.source, prefix="candidate", cap=gen["cap_per_unit"], batch_max=gen["batch_max"])


def require_design(unit: Unit) -> str:
    design = unit.data.get("design")
    if not isinstance(design, str) or not design.strip():
        raise SpriteError(f"unit '{unit.id}' unit.json has no 'design' (the character description)")
    return design.strip()


def build_prompt(ctx: Context, unit: Unit, notes: list[str], reference_mode: str | None,
                 variant: str | None = None) -> str:
    gen = ctx.project["generation"]
    design = require_design(unit)
    kind = ctx.project["kinds"][unit.kind]
    # Without a stated size the AI drew a humanoid about 132 px tall against the 80 px target.
    size = gen["size_sentence"].format(height=drawn_height(ctx, unit), cell_width=kind["cell"][0],
                                       cell_height=kind["cell"][1])
    # sprite_note holds what only the pixel sprite needs (chibi proportions, a slime
    # read as a blob), so the shared design stays medium-neutral for the card art.
    note = unit.data.get("sprite_note")
    if note is not None and (not isinstance(note, str) or not note.strip()):
        raise SpriteError(f"unit '{unit.id}': sprite_note must be a non-empty string")
    description = (note.strip() + " " + design) if note else design
    lines = [gen["tool_instruction"],
             "Image: " + gen["common_prompt"] + " " + size + " " + description]
    if notes:
        lines.append("Changes from the previous candidate: " + " ".join(notes))
    if reference_mode is not None:
        lines.append(reference_sentence(ctx, unit, reference_mode))
    if reference_mode == "design-file" and "restore_details" in unit.data:
        # The shared sentence stays generic; what to bring back (shield rivets,
        # a staff crystal) differs per unit.
        details = unit.data["restore_details"]
        if not isinstance(details, str) or not details.strip():
            raise SpriteError(f"unit '{unit.id}': restore_details must be a non-empty string")
        lines.append("Also restore: " + details.strip().rstrip(".") + ".")
    if variant:
        lines.append("For this candidate: " + variant)
    prompt = "\n".join(lines)
    check_banned(ctx, f"unit '{unit.id}'", prompt)
    return prompt


def reference_sentence(ctx: Context, unit: Unit, mode: str) -> str:
    gen = ctx.project["generation"]
    if mode != "art":
        return gen[REFERENCE_SENTENCES[mode]]
    # The card art has normal proportions and faces the viewer: a person is redrawn
    # as the SD battle sprite, a creature at its height (its own, or its kind's).
    if unit.kind == "humanoid":
        return gen["art_reference_sentences"]["humanoid"]
    return gen["art_reference_sentences"]["creature"].format(height=drawn_height(ctx, unit))


def adopted_art(unit: Unit) -> Path | None:
    """The unit's adopted card art (the latest base adoption in art/art.json), checked
    against its recorded sha256; None when the unit has no adoption."""
    # Imported here: the art module builds on this one (require_design).
    from .art import ADOPTION, STAGE, art_source, current_adoption, load_adoption
    if not (art_source(unit) / ADOPTION).is_file():
        return None
    what = f"unit '{unit.id}'"
    entry = current_adoption(load_adoption(unit), STAGE, what)
    path = art_source(unit) / entry["candidate"]
    if not path.is_file():
        raise SpriteError(f"{what}: the adopted card art {path} is missing")
    if sha256_file(path) != entry["sha256"]:
        raise SpriteError(f"{what}: {path.name} does not match the sha256 adopted in {ADOPTION}")
    return path


def _load_generation(unit: Unit) -> dict:
    path = unit.source / "generation.json"
    return read_json(path) if path.is_file() else {"unit": unit.id, "generations": []}


def _check_reference(unit: Unit, reference: Path, mode: str) -> None:
    if not reference.is_file():
        raise SpriteError(f"reference image not found: {reference}")
    if mode == "art":
        art = adopted_art(unit)
        if art is None:
            raise SpriteError(f"unit '{unit.id}' has no adopted card art (art/art.json); adopt its art first")
        if reference.resolve() != art.resolve():
            raise SpriteError(f"unit '{unit.id}': --from-art takes the adopted card art {art}, not {reference}")
        return
    in_unit = reference.resolve().parent == unit.source.resolve()
    recorded = any(g.get("candidate") == reference.name for g in _load_generation(unit)["generations"])
    if mode == "design" and not (in_unit and recorded):
        raise SpriteError(f"unit '{unit.id}': --design-from must be one of this unit's recorded candidates "
                          f"(got {reference})")
    if mode == "design-file":
        if not in_unit:
            raise SpriteError(f"unit '{unit.id}': --design-from-file must be inside {unit.source} "
                              f"(copy it there first, e.g. as ref_*.png; got {reference})")
        if recorded:
            raise SpriteError(f"unit '{unit.id}': {reference.name} is a recorded candidate; use --design-from")


def generate(ctx: Context, unit: Unit, reference: Path | None, notes: list[str],
             reference_mode: str | None = None, batch: int = 1, variants: list[str] | None = None) -> dict:
    """Runs `batch` codex executions at once, one image each. reference_mode is
    "scale" (another unit as a scale reference), "design" (this unit redrawn from its
    own earlier candidate), "design-file" (redrawn from a prepared image in its
    folder) or "art" (drawn from its adopted card art); it is required with a
    reference. The i-th variant is added to the i-th run's prompt."""
    variants = list(variants or [])
    if (reference is None) != (reference_mode is None) or (reference_mode and reference_mode not in REFERENCE_SENTENCES):
        raise SpriteError(f"a reference image needs a mode from {sorted(REFERENCE_SENTENCES)} (got {reference_mode})")
    if unit.data.get("palette_swap"):
        raise SpriteError(f"unit '{unit.id}' is a palette swap; it is never generated")
    target = sprite_target(ctx, unit)
    used = check_batch(target, batch, variants)
    if reference is not None:
        _check_reference(unit, reference, reference_mode)
    elif adopted_art(unit) is not None:
        # A unit with card art gets its sprite from that art (art first).
        raise SpriteError(f"unit '{unit.id}' has adopted card art; draw its sprite from it with --from-art "
                          "(a design pass without a reference is only for units without card art)")
    run_variants = [variants[i] if i < len(variants) else None for i in range(batch)]
    prompts = [build_prompt(ctx, unit, notes, reference_mode, v) for v in run_variants]
    ref_record = ({"mode": reference_mode, "path": display_path(ctx, reference), "sha256": sha256_file(reference)}
                  if reference is not None else None)
    references = [reference] if reference is not None else []
    batch_result = run_batch(ctx, target, prompts, run_variants, references, ref_record,
                             {"measured_height": None, "height_target": None, "height_check": None,
                              "adopted": None, "reason": None})
    results = batch_result["runs"]
    # The record is written before the height checks, so a candidate a check cannot
    # read still counts toward the cap and can still be adopted or rejected.
    data = _load_generation(unit)
    recorded = data["generations"][-len(results):]
    for r, entry in zip(results, recorded):
        r["entry"] = entry
        if r["candidate"]:
            try:
                _check_height(ctx, unit, Path(r["candidate"]), entry)
            except Exception as e:  # a broken or non-image candidate must not lose the record
                entry["error"] = f"height check failed: {type(e).__name__}: {e}"
    (unit.source / "generation.json").write_bytes(json_bytes(data))
    sheet = write_sheet(ctx, unit.source, [r["entry"] for r in results],
                        ctx.preview_dir / unit.path / f"batch_{batch_result['first']}-{batch_result['last']}.png",
                        ctx.project["preview"]["batch_sheet_height"], _height_label)
    return {"runs": results, "sheet": str(sheet), "used": used + sum(r["entry"]["image_generation_calls"] for r in results),
            "cap": target.cap}


def _height_label(e: dict) -> str:
    if e["measured_height"] is not None:
        return f"height {e['measured_height']} / {e['height_target']} {e['height_check']}"
    return e["error"] or "no height"


def _check_height(ctx: Context, unit: Unit, candidate: Path, entry: dict) -> None:
    """Fills measured_height, height_target and height_check (PASS, FAIL or REVIEW).

    The candidate is measured whole, while the target leaves out the rig's
    height_excludes layers (weapon, staff, bow). Parts are not split yet, so the excess cannot be traced to a
    layer; instead a too-tall candidate that carries one of them is shown for
    review only while the excess fits the rig's equipment_allowance (a raised
    weapon). Taller than that, the figure itself is too big and is dropped.
    """
    target, _ = reference_height(ctx, unit)
    measured = candidate_height(ctx, candidate)
    tol = ctx.project["height_tolerance"]
    rig = ctx.project["rigs"].get(unit.data.get("rig") or ctx.project["kinds"][unit.kind].get("rig") or "", {})
    carries_excluded = bool(set(unit.data.get("equipment", [])) & set(rig.get("height_excludes", [])))
    allowance = rig["equipment_allowance"] if carries_excluded else 0
    if abs(measured - target) <= tol:
        status = "PASS"
    elif carries_excluded and target + tol < measured <= target + allowance:
        status = "REVIEW"
    else:
        status = "FAIL"
    entry["measured_height"] = measured
    entry["height_target"] = target
    entry["height_check"] = status
    if status == "FAIL":
        entry["adopted"] = False
        entry["reason"] = f"height {measured} vs {target}+-{tol}"
        if carries_excluded and measured > target:
            entry["reason"] += f", over the equipment allowance +{allowance}"


HEIGHT_FIELDS = ("measured_height", "height_target", "height_check")


def recheck_heights(ctx: Context, unit: Unit, names: list[str]) -> list[tuple[str, dict, dict]]:
    """Measures recorded candidates again with the current settings and rewrites
    only their height fields; counts, adoption and reasons stay as recorded.
    Returns (candidate, old fields, new fields) per candidate."""
    data = _load_generation(unit)
    entries = []
    for name in names:
        hit = [g for g in data["generations"] if g.get("candidate") == name]
        if not hit:
            raise SpriteError(f"unit '{unit.id}': {name} is not in generation.json")
        if not (unit.source / name).is_file():
            raise SpriteError(f"unit '{unit.id}': {name} does not exist")
        entries.append((name, hit[-1]))
    results = []
    for name, entry in entries:
        probe = dict(entry)
        try:
            _check_height(ctx, unit, unit.source / name, probe)
        except OSError as e:
            raise SpriteError(f"unit '{unit.id}': cannot read {name}: {e}") from e
        old = {k: entry.get(k) for k in HEIGHT_FIELDS}
        entry.update({k: probe[k] for k in HEIGHT_FIELDS})
        results.append((name, old, {k: entry[k] for k in HEIGHT_FIELDS}))
    (unit.source / "generation.json").write_bytes(json_bytes(data))
    return results


def candidate_height(ctx: Context, path: Path) -> int:
    """Grid height of a candidate after conversion: the whole figure, including any raised weapon."""
    rgb, alpha = load_rgba(path)
    grid, _, _, _ = convert_image(rgb, alpha, ctx.project["convert"], ctx.project["palette"])
    rows = np.nonzero((grid != ".").any(axis=1))[0]
    return int(rows.max() - rows.min() + 1)


# ---------------------------------------------------------------- checkpoints


def load_checkpoints(unit: Unit) -> dict:
    path = unit.source / "checkpoints.json"
    return read_json(path) if path.is_file() else {"unit": unit.id, "entries": []}


def checkpoint_status(unit: Unit) -> dict[int, dict]:
    """Latest entry per checkpoint number."""
    return {n: e for n, (_, e) in _latest_checkpoints(unit).items()}


def _latest_checkpoints(unit: Unit) -> dict[int, tuple[int, dict]]:
    """Latest (position in checkpoints.json, entry) per checkpoint number."""
    latest = {}
    for i, e in enumerate(load_checkpoints(unit)["entries"]):
        latest[int(e["checkpoint"])] = (i, e)
    return latest


def is_approved(unit: Unit, number: int) -> bool:
    """The latest decision for this checkpoint is an approval made after the
    previous checkpoint's approval: redoing an earlier checkpoint voids the
    later ones until they are shown and approved again."""
    latest = _latest_checkpoints(unit)
    if number not in latest:
        return False
    position, e = latest[number]
    if e["decision"] != "approved":
        return False
    if number == 1 or (unit.data.get("palette_swap") and number == 3):
        return True
    return is_approved(unit, number - 1) and position > latest[number - 1][0]


def adopted_source(unit: Unit) -> tuple[Path, dict]:
    """The image adopted at checkpoint 1 and its entry: an AI candidate, or a
    reduced reference adopted as the fallback (entry kind "reduced-ref")."""
    e = checkpoint_status(unit).get(1)
    if not (e and e["decision"] == "approved" and (e.get("candidate") or e.get("reference"))):
        raise SpriteError(f"unit '{unit.id}': checkpoint 1 has no approved candidate in checkpoints.json")
    return unit.source / (e.get("candidate") or e["reference"]), e


def record_checkpoint(unit: Unit, number: int, decision: str, note: str, candidate: str | None) -> dict:
    if number not in (1, 2, 3):
        raise SpriteError(f"checkpoint must be 1, 2 or 3 (got {number})")
    if not note.strip():
        raise SpriteError("a checkpoint needs a note (the user's reason)")
    if unit.data.get("palette_swap"):
        # A recolor has no AI image and no hand fixes: only the final GIF is checked.
        if number != 3:
            raise SpriteError(f"unit '{unit.id}' is a palette swap; only checkpoint 3 applies")
    elif number > 1 and not is_approved(unit, number - 1):
        raise SpriteError(f"unit '{unit.id}': checkpoint {number - 1} is not approved yet")
    if number == 1:
        if not candidate:
            raise SpriteError("checkpoint 1 needs --candidate (e.g. candidate_2.png)")
        if not (unit.source / candidate).is_file():
            raise SpriteError(f"unit '{unit.id}': {candidate} does not exist")
        gen_path = unit.source / "generation.json"
        data = _load_generation(unit)
        hit = [g for g in data["generations"] if g.get("candidate") == candidate]
        if not hit:
            raise SpriteError(f"unit '{unit.id}': {candidate} is not in generation.json")
        hit[-1]["adopted"] = decision == "approved"
        hit[-1]["reason"] = note
        gen_path.write_bytes(json_bytes(data))
    return append_checkpoint(unit, {"checkpoint": number, "decision": decision, "candidate": candidate,
                                    "note": note, "date": datetime.date.today().isoformat()})


def append_checkpoint(unit: Unit, entry: dict) -> dict:
    data = load_checkpoints(unit)
    data["entries"].append(entry)
    (unit.source / "checkpoints.json").write_bytes(json_bytes(data))
    return entry
