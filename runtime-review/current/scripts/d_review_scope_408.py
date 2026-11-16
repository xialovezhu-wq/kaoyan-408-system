"""Local-only, explicit user narrowing of a frozen D preparation scope."""
from __future__ import annotations

import hashlib
import json
from pathlib import Path

SCHEMA = "cs408-D-local-subset-decision-v1"
DECISIONS_REL = Path(".private/study-bridge/d-scope-decisions")


def load_scope(repo, reference, body, full_concepts):
    if reference is None:
        return None
    if not isinstance(reference, dict) or set(reference) != {"path", "sha256"}:
        raise ValueError("D_local_subset_reference_invalid")
    path = Path(reference["path"])
    allowed = (Path(repo) / DECISIONS_REL).resolve()
    if path.is_symlink() or not path.is_file() or path.resolve().parent != allowed:
        raise ValueError("D_local_subset_requires_local_decision")
    raw = path.read_bytes()
    if hashlib.sha256(raw).hexdigest() != reference["sha256"]:
        raise ValueError("D_local_subset_decision_drift")
    value = json.loads(raw)
    if (value.get("schema") != SCHEMA
            or value.get("authorization_kind") not in {"explicit_user_completed_and_accepted_only",
                                                       "explicit_user_true_exams_only",
                                                       "explicit_user_question_limit"}
            or not value.get("user_request") or not value.get("user_message_id")):
        raise ValueError("D_local_subset_user_request_required")
    if (value.get("authorization_kind") == "explicit_user_true_exams_only"
            and value.get("question_policy") != "canonical_true_exams_only"):
        raise ValueError("D_local_subset_true_exam_policy_required")
    for key in ("task_id", "source_commit", "target_date", "due_version", "concept_priority_version"):
        if value.get(key) != body.get(key):
            raise ValueError("D_local_subset_binding_drift:" + key)
    full = list(full_concepts)
    admitted = value.get("admitted_concept_ids")
    deferred = value.get("deferred_concepts")
    if (value.get("full_review_concept_ids") != full or not isinstance(admitted, list)
            or not admitted or len(admitted) != len(set(admitted))
            or not isinstance(deferred, list) or not deferred
            or any(not isinstance(row, dict) or not row.get("reason") for row in deferred)):
        raise ValueError("D_local_subset_scope_invalid")
    deferred_ids = [row.get("concept_id") for row in deferred]
    if (len(deferred_ids) != len(set(deferred_ids)) or set(admitted) & set(deferred_ids)
            or set(admitted) | set(deferred_ids) != set(full)):
        raise ValueError("D_local_subset_scope_mismatch")
    if value.get("authorization_kind") == "explicit_user_question_limit":
        limit = value.get("question_limit")
        if not isinstance(limit, int) or isinstance(limit, bool) or not 1 <= len(admitted) <= limit:
            raise ValueError("D_local_subset_question_limit_invalid")
    return value


def stage_source_order(selected, supplemental, substitutes, pre_campaign_concepts):
    """Keep canonical substitute prelude separate from unstarted due campaign."""
    from morning_question_policy_408 import source_class
    wanted = set(pre_campaign_concepts or [])
    before, after = [], []
    for item in supplemental + substitutes:
        points = item.get("review_target_concept_ids", [])
        if wanted.intersection(points):
            if len(points) != 1 or source_class(item) != "canonical_true_exam":
                raise ValueError("D_pre_campaign_requires_verified_canonical_identity")
            before.append(item)
        else:
            after.append(item)
    if {p for item in before for p in item["review_target_concept_ids"]} != wanted:
        raise ValueError("D_pre_campaign_concept_binding_invalid")
    ranks = {"canonical_true_exam": 0, "formal_source_original": 1, "variant": 2}
    stages = [before, selected, after]
    previous_max = -1
    for stage in stages:
        known = [ranks[source_class(item)] for item in stage if source_class(item) in ranks]
        if known:
            if previous_max > min(known):
                raise ValueError("D_source_order_cross_stage_inversion")
            previous_max = max(known)
    return before, after


def pending_pre_campaign(repo, import_receipt):
    wanted = import_receipt.get("pre_campaign_supplemental_request_ids", [])
    if not wanted:
        return []
    import study_episode_capture_408 as episodes
    completed = set()
    root = episodes._operation_root(Path(repo)) / "d-supplement-progress"
    for path in root.glob("*.json"):
        if path.is_symlink():
            continue
        value = json.loads(path.read_bytes())
        if (value.get("schema") != "cs408-d-concept-supplement-progress-receipt-v2"
                or value.get("preparation_date", value.get("study_date")) != import_receipt["target_date"]):
            continue
        for row in value.get("episode_receipts", []):
            rid = row.get("request_id")
            if rid not in wanted or rid in completed:
                continue
            receipt_path = Path(row["receipt_ref"])
            if hashlib.sha256(receipt_path.read_bytes()).hexdigest() != row["receipt_sha256"]:
                raise ValueError("D_pre_campaign_episode_receipt_drift")
            verified = episodes.verify_episode_capture(repo, receipt_path)
            if value.get("study_date") != import_receipt["target_date"]:
                # Cross-day progress must belong to this frozen D import.
                opened = episodes.packages.verify_package_binding(
                    verified["conversation_package_locator"],
                    expected_canonical_sha256=verified["conversation_package_sha256"],
                    require_sealed=True, private_root=episodes._default_package_root(Path(repo)))
                binding = opened["source"]["source"]["display_binding"]
                from exam_tutoring_context_408 import _canonical_bytes
                if (binding.get("request_id") != rid
                        or binding.get("preparation_date") != import_receipt["target_date"]
                        or binding.get("native_import_receipt_sha256") != hashlib.sha256(
                            _canonical_bytes(import_receipt)).hexdigest()):
                    raise ValueError("D_pre_campaign_cross_day_binding_invalid")
            completed.add(rid)
    return [rid for rid in wanted if rid not in completed]
