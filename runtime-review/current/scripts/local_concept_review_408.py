"""Prepare a local concept review using native due and verified surface owners.

No GitHub/ChatGPT transport, authored questions, or learning outcomes are needed.
Unresolved concepts require the existing variant author/verification path before
preparation. No partial run is published.
"""
from __future__ import annotations

import argparse
import copy
import datetime as dt
import json
import re
from pathlib import Path

import start_morning_coverage_campaign_408 as control
import study_bridge_408 as bridge


def substitute_request(item, context, nodes):
    """Derive an execution request, never a native due or learner-evidence row."""
    if not isinstance(item, dict) or item.get("execution_kind") != "due_representative_substitute":
        raise ValueError("D_substitute_kind_required")
    points = item.get("review_target_concept_ids")
    fid = item.get("question_id")
    if not isinstance(points, list) or len(points) != 1 or fid not in nodes:
        raise ValueError("D_substitute_single_point_required")
    point = points[0]
    group = next((row for row in context.get("review_concepts", []) if row["concept_id"] == point), None)
    due = set((group or {}).get("formal_node_ids", []))
    canonical = bool(control.CANONICAL_RE.fullmatch(str(item.get("true_item_id") or "")))
    if (not due or fid not in (group or {}).get("candidate_formal_node_ids", [])
            or (not canonical and (fid in context["coverage"]["due_formal_node_ids"]
                                    or nodes[fid]["main_knowledge"] != point))):
        raise ValueError("D_substitute_candidate_binding_invalid")
    # A due node may supply historical context for a different canonical exam.
    # It stays in the full carry-over set; this is never its original redo.
    if canonical and fid in due and item["true_item_id"] in nodes[fid].get("exact_sources", []):
        raise ValueError("D_substitute_exact_original_requires_due_item")
    suppressed = item.get("suppressed_same_concept_question_ids")
    if not isinstance(suppressed, list) or len(suppressed) != len(set(suppressed)) or set(suppressed) != due:
        raise ValueError("D_substitute_due_carryover_binding_invalid")
    true_id = item.get("true_item_id")
    if (not isinstance(true_id, str)
            or not (control.CANONICAL_RE.fullmatch(true_id) or re.fullmatch(r"EVB-[0-9A-F]{24}", true_id))
            or not control.SHA_RE.fullmatch(str(item.get("surface_sha256") or ""))
            or item.get("preferred_true_item_id") not in (None, true_id)
            or item.get("web_variant_id") or item.get("request_id")):
        raise ValueError("D_substitute_exact_existing_surface_required")
    reason = item.get("substitution_reason")
    if not isinstance(reason, str) or not reason.strip():
        raise ValueError("D_substitution_reason_required")
    material = {"request_kind": "due_representative_substitute", "concept_id": point,
                "question_id": fid, "formal_node_sha256": nodes[fid]["formal_node_sha256"],
                "true_item_id": true_id, "surface_sha256": item["surface_sha256"],
                "substituted_due_formal_node_ids": sorted(due), "due_version": context["due_version"],
                "substitution_reason": reason}
    return {"request_id": "DREP-" + bridge._digest(material)[:24].upper(), **material}


def select_substitute(repo, date, details, release_dir, context, item, used, *, hot_capture_proof=None):
    """Select the exact same-point source while retaining concept-only semantics."""
    nodes = control._node_snapshot(repo)
    request = substitute_request(item, context, nodes)
    fid, point, true_id = request["question_id"], request["concept_id"], request["true_item_id"]
    if true_id in used:
        return None, ["D_substitute_surface_reserved"]
    if true_id.startswith("EVB-"):
        _, select = selectors(repo, date, details, release_dir, context)
        selected, reasons = select(fid, point, used, true_id)
    else:
        selected, reasons = select_canonical_concept(
            repo, date, details, release_dir, fid, point, true_id,
            request["surface_sha256"], used, request["request_id"], hot_capture_proof=hot_capture_proof)
    if (selected is None or selected.get("status") != "selected"
            or selected.get("true_item_id") != true_id
            or selected.get("surface_sha256") != request["surface_sha256"]):
        return None, reasons or ["D_substitute_surface_drift"]
    return selected, []


def recommendation_cooldown_days(repo):
    """New exact-question selection respects the shared rule and stricter policy."""
    from morning_selection_408 import current_cooldown_days
    from recent_contact_408 import RECOMMENDATION_COOLDOWN_DAYS
    return max(RECOMMENDATION_COOLDOWN_DAYS, current_cooldown_days(repo))


def select_canonical_concept(repo, date, details, release_dir, fid, point, true_id,
                             surface_sha256, used, commitment_id, *,
                             exact_original_source_commit=None, exact_original_ids=None,
                             hot_capture_proof=None, user_requested_exact_review=False):
    """Resolve one exact exam for a concept, without claiming an anchor redo."""
    if not control.CANONICAL_RE.fullmatch(str(true_id)) or true_id in used:
        return None, ["D_canonical_concept_identity_invalid_or_reserved"]
    nodes = control._node_snapshot(repo)
    if fid not in nodes:
        return None, ["D_canonical_concept_anchor_missing"]
    release = control.load_style_release(release_dir)
    exposure = release["exposure_by_id"].get(true_id, {})
    corpus_point = exposure.get("main_knowledge")
    practice_point_match = bool(exposure.get("practice_unit_id")
                                and point in exposure.get("knowledge_points", []))
    if corpus_point != point and not practice_point_match:
        exact_bound_secondary = False
        node = nodes[fid]
        if exact_original_source_commit is not None:
            from morning_question_policy_408 import verified_exact_original_ids
            library = bridge._published_library(exact_original_source_commit, repo,
                                                original_mappings_only=True)
            exact_original_ids = verified_exact_original_ids(node, fid, library)
        if exact_original_ids is not None:
            related = {node.get("main_knowledge")}
            for field in ("副知识点", "命中知识点"):
                related.update(re.findall(r"(?:DS|CO|OS|CN)\d{2}-\d{2}",
                    str((node.get("row") or {}).get(field) or "")))
            exact_bound_secondary = (point == node.get("main_knowledge")
                and true_id in exact_original_ids
                and corpus_point in related)
        if not exact_bound_secondary:
            return None, ["D_substitute_canonical_point_mismatch"]
    from recent_contact_408 import gap_days, real_contacts
    day = dt.date.fromisoformat(date)
    window = recommendation_cooldown_days(repo)
    if not user_requested_exact_review:
        # A concept-only answer or independent correct review may not change
        # the formal redo date. Gate the actual selected question identity.
        options = {"hot_capture_proof": hot_capture_proof} if hot_capture_proof is not None else {}
        contact = real_contacts(repo, day, strict=True, **options)["by_identity"].get(true_id)
        gap = gap_days(contact, day)
        if gap is not None and gap < window:
            return None, [f"D_exact_true_exam_used_within_{window}_days"]
    pool = control.variant_bank.pool_status(details, release_dir,
                                           **({"persist_index": False} if hot_capture_proof is not None else {}))
    formal = control.formal_original.pool_status(details)
    raw = control.question_exposure.recent_exposure_set(repo, day, window_days=window)
    mapped = control.map_recent_exposures(raw, release, variant_entries=pool.get("entries", []),
                                         formal_original_entries=formal.get("entries", []))
    if user_requested_exact_review:
        # Explicit user selection is exempt from recommendation cooldown, never
        # from source/point/hash verification. Keep the stored exposure intact.
        mapped = {**mapped, "rows": [row for row in mapped.get("rows", [])
                                     if row.get("true_item_id") != true_id]}
    selected = control.variant_bank.select_surface(
        details, release_dir, review_date=date, formal_node_id=fid,
        formal_node_sha256=nodes[fid]["formal_node_sha256"], true_item_id=true_id,
        original_canonical_id=true_id, exposure_set=mapped, consume=False,
        campaign_commitment_id=commitment_id, formal_node_main_knowledge=point)
    if (selected.get("status") != "selected" or selected.get("surface_kind") != "canonical_original"
            or selected.get("true_item_id") != true_id
            or selected.get("surface_sha256") != surface_sha256):
        return None, [selected.get("reason") or "D_canonical_concept_surface_drift"]
    return selected, []


def choose_representatives(context, select):
    """Keep native due order, trying alternatives only within the same concept."""
    groups = {g["concept_id"]: g["formal_node_ids"] for g in context["due_concepts"]}
    concept_for = {fid: c for c, ids in groups.items() for fid in ids}
    if len(concept_for) != sum(map(len, groups.values())):
        raise ValueError("ambiguous_primary_concept")
    selected, failures, used, covered = [], {}, set(), set()
    for row in context["native_due_run"]["due_items"]:
        fid = row["formal_node_id"]
        concept = concept_for.get(fid)
        if not concept:
            raise ValueError("unmapped_due_node:" + fid)
        if concept in covered:
            continue
        surface, reasons = select(fid, used)
        if surface is None:
            failures[fid] = reasons
            continue
        identity = surface["true_item_id"]
        if identity in used:
            raise ValueError("duplicate_selected_surface")
        used.add(identity)
        covered.add(concept)
        selected.append({"question_id": fid, "concept_id": concept,
                         "suppressed_same_concept_question_ids": [i for i in groups[concept] if i != fid],
                         "selection": surface})
    unresolved = [{"concept_id": c, "formal_node_ids": ids,
                   "reasons": {fid: failures.get(fid, []) for fid in ids}}
                  for c, ids in groups.items() if c not in covered]
    return selected, unresolved


def choose_supplements(context, selected, select):
    """Only primary due representatives discharge their own concept task.

    A side-point request receives an explicitly point-targeted verified variant;
    mere question membership never proves the displayed variant covers it.
    """
    covered = {row["concept_id"] for row in selected}
    used = {row["selection"]["true_item_id"] for row in selected}
    supplemental, unresolved = [], []
    for request in context.get("concept_requests", []):
        if request["concept_id"] in covered:
            continue
        failures = {}
        for fid in request["candidate_formal_node_ids"]:
            surface, reasons = select(fid, request["concept_id"], used)
            if surface is None:
                failures[fid] = reasons
                continue
            if surface["true_item_id"] in used:
                raise ValueError("duplicate_supplemental_surface")
            used.add(surface["true_item_id"])
            supplemental.append({"request_id": request["request_id"], "concept_id": request["concept_id"],
                "question_id": fid, "selection": surface, "execution_kind": "independent_concept_review",
                "question_due": fid in context["coverage"]["due_formal_node_ids"],
                "question_fsrs_write_count": 0})
            break
        else:
            unresolved.append({"concept_id": request["concept_id"], "request_id": request["request_id"],
                "status": "blocked", "formal_node_ids": request["candidate_formal_node_ids"], "reasons": failures,
                "next_action": "exam_variant_bank_prebuild_author_blind_adversarial_publish"})
    return supplemental, unresolved


def execution_run(context, selected, unresolved, supplemental_items=()):
    if unresolved:
        raise ValueError("verified_variant_generation_required")
    original = context["native_due_run"]
    run = copy.deepcopy(original)
    rows = {r["formal_node_id"]: r for r in original["due_items"]}
    ids = [r["question_id"] for r in selected]
    concepts = [r["concept_id"] for r in selected]
    unresolved_ids = [r["concept_id"] for r in unresolved]
    if len(ids) != len(set(ids)) or len(concepts) != len(set(concepts)):
        raise ValueError("duplicate_representative")
    if set(concepts) & set(unresolved_ids) or set(concepts + unresolved_ids) != set(context["due_concept_ids"]):
        raise ValueError("concept_coverage_mismatch")
    run.pop("run_sha256", None)
    chosen = [rows[fid] for fid in ids]
    segments = []
    for offset in range(0, len(chosen), original["segment_size"]):
        chunk = chosen[offset:offset + original["segment_size"]]
        segment = {"segment_index": len(segments) + 1, "segment_size": len(chunk),
                   "formal_node_ids": [r["formal_node_id"] for r in chunk],
                   "item_set_sha256": control.scheduler.canonical_sha256(chunk)}
        segment["segment_sha256"] = control.scheduler.canonical_sha256(segment)
        segments.append(segment)
    run.update(due_items=chosen, due_count=len(chosen), scheduled_count=len(chosen), segments=segments,
               execution_scope="D_concept_representatives", preparation_origin="local_native",
               full_due_count=original["due_count"], full_due_run_sha256=original["run_sha256"],
               full_due_concept_ids=context["due_concept_ids"],
               selected_concept_ids=concepts, unresolved_concept_ids=unresolved_ids,
               suppressed_carryover_formal_node_ids=[fid for fid in rows if fid not in ids])
    run["independent_supplemental_request_ids"] = [row["request_id"] for row in supplemental_items]
    run["preferred_variant_ids_by_formal_node"] = {
        row["question_id"]: row["selection"]["variant_id"]
        for row in selected
        if row["selection"].get("variant_id")
    }
    run["run_sha256"] = control.scheduler.canonical_sha256(run)
    return run


def assert_no_active(repo):
    gate = control.legacy_session_gate(repo)
    if gate["status"] != "clear":
        raise ValueError("active_legacy_session_requires_resume")
    runtime_root = repo / control.DEFAULT_RUNTIME_REL
    for state_path in (runtime_root / "runs").glob("*/active-state.json"):
        state = control._regular_json(state_path, "existing_campaign_state")
        if state.get("status") in {"active", "paused"}:
            raise ValueError("existing_campaign_requires_resume:" + state_path.parent.name)


def selectors(repo, date, details, release_dir, context):
    release = control.load_style_release(release_dir)
    pool = control.variant_bank.pool_status(details, release_dir)
    formal = control.formal_original.pool_status(details)
    raw = control.question_exposure.recent_exposure_set(repo, dt.date.fromisoformat(date), window_days=7)
    mapped = control.map_recent_exposures(raw, release, variant_entries=pool.get("entries", []),
                                         formal_original_entries=formal.get("entries", []))
    nodes = control._node_snapshot(repo)
    def select(fid, used, preferred_variant_id=None):
        return control._selection(details_root=details, release_dir=release_dir,
            review_date=date, formal_node_id=fid, node=nodes[fid], release=release,
            mapped_exposures=mapped, used_true_items=used,
            selector=control.variant_bank.select_surface,
            formal_original_selector=control.formal_original.select_surface,
            variant_entries=pool.get("entries", []),
            campaign_commitment_id="LOCAL-PLAN-" + context["due_version"][:24],
            preferred_variant_id=preferred_variant_id)
    def supplement(fid, point, used, preferred_variant_id=None):
        matches = [row for row in pool.get("entries", []) if row.get("formal_node_id") == fid
                   and row.get("main_knowledge") == point and row.get("pool_status") == "verified"
                   and (preferred_variant_id is None or row.get("variant_id") == preferred_variant_id)]
        reasons = []
        for entry in matches:
            anchor = entry["canonical_anchor_ids"][0]
            result = control.variant_bank.select_surface(details, release_dir, review_date=date,
                formal_node_id=fid, formal_node_sha256=nodes[fid]["formal_node_sha256"],
                true_item_id=anchor, original_canonical_id=anchor, exposure_set=mapped,
                variant_only=True, formal_node_main_knowledge=point, required_target_knowledge=point,
                seed=date + ":" + point, consume=False,
                campaign_commitment_id="D-SUPPLEMENT-" + context["concept_priority_version"][:24],
                preferred_variant_id=preferred_variant_id)
            if result.get("status") == "selected" and result["true_item_id"] not in used:
                return result, reasons
            reasons.append(result.get("reason", "surface_reserved_or_unavailable"))
        return None, reasons or ["verified_point_target_variant_required"]
    return select, supplement


def plan(repo, date, details, release_dir):
    assert_no_active(repo)
    context = bridge.due(repo=repo, target_date=date)
    select, supplement = selectors(repo, date, details, release_dir, context)
    selected, unresolved = choose_representatives(context, select)
    supplemental, supplemental_unresolved = choose_supplements(context, selected, supplement)
    return {"schema": "local-concept-review-plan-v1", "target_date": date,
            "context": context, "selected": selected, "unresolved": unresolved,
            "supplemental_items": supplemental, "supplemental_unresolved": supplemental_unresolved,
            "formal_write_count": 0, "learner_evidence_write_count": 0}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("operation", choices=["plan", "prepare"])
    parser.add_argument("--repo", type=Path, default=bridge.REPO)
    parser.add_argument("--date", required=True)
    parser.add_argument("--details-root", type=Path, default=control.DEFAULT_DETAILS_ROOT)
    parser.add_argument("--release", type=Path, default=control.DEFAULT_RELEASE)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    # Previewed variants can contain complete learner text. Keep the delivery
    # outside the formal repository, like other question authoring materials.
    if args.output.resolve().is_relative_to(args.repo.resolve()):
        raise ValueError("output_must_be_outside_formal_repository")
    result = plan(args.repo, args.date, args.details_root, args.release)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n")
    if args.operation == "prepare":
        if result["supplemental_unresolved"]:
            raise ValueError("supplemental_verified_variant_generation_required")
        run = execution_run(result["context"], result["selected"], result["unresolved"], result["supplemental_items"])
        fresh = bridge.due(repo=args.repo, target_date=args.date)
        if fresh["due_version"] != result["context"]["due_version"]:
            raise ValueError("due_changed_during_selection")
        prepared = control.prepare_campaign(args.repo, review_date=args.date,
            details_root=args.details_root, release_dir=args.release, scheduler_run=run)
        result["preparation"] = prepared
        args.output.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n")
    print(json.dumps({"output": str(args.output), "selected_count": len(result["selected"]),
        "unresolved_count": len(result["unresolved"]), "preparation": result.get("preparation")}, ensure_ascii=False))


if __name__ == "__main__":
    main()
