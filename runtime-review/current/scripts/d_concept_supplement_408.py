"""D point supplements: verified displays, then one Capture per real episode."""
from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import re
import sys
from contextlib import contextmanager
from contextvars import ContextVar
from pathlib import Path
from zoneinfo import ZoneInfo

import concept_review_408 as writer
import local_concept_review_408 as local
import study_episode_capture_408 as episode_capture
import study_bridge_408 as bridge
from study_timing_408 import measure

SCHEMA = "cs408-D-supplement-display-v1"
SESSION_SCHEMA = "cs408-d-concept-supplement-session-v2"
SESSION_CLOSURE_SCHEMA = "cs408-d-session-closure-v1"
_SESSION_LOCKS = ContextVar("d_session_locks", default=frozenset())


def session_closure_path(repo, session_id):
    if not isinstance(session_id, str) or not session_id:
        raise ValueError("D_learning_session_required")
    return writer.private_root(repo) / "d-session-closures" / (writer.digest(session_id) + ".json")


def read_session_closure(repo, session_id):
    path = session_closure_path(repo, session_id)
    if not path.exists():
        return None
    if path.is_symlink() or not path.is_file():
        raise ValueError("D_learning_session_closure_invalid")
    value = json.loads(path.read_bytes())
    material = {k: v for k, v in value.items() if k != "receipt_sha256"}
    if (value.get("schema") != SESSION_CLOSURE_SCHEMA or value.get("session_id") != session_id
            or value.get("status") != "closed_by_user" or value.get("receipt_sha256") != writer.digest(material)):
        raise ValueError("D_learning_session_closure_drift")
    return value


def assert_session_open(repo, session_id):
    if read_session_closure(repo, session_id) is not None:
        raise ValueError("D_learning_session_closed_by_user")


@contextmanager
def session_lock(repo, session_id):
    path = session_closure_path(repo, session_id)
    key = str(path.resolve())
    held = _SESSION_LOCKS.get()
    if key in held:
        yield
        return
    with episode_capture._operation_lock(path.parent, writer.digest(session_id)):
        token = _SESSION_LOCKS.set(held | {key})
        try:
            yield
        finally:
            _SESSION_LOCKS.reset(token)


def receipt_root(repo):
    return writer.private_root(repo) / "d-displays"


def read_receipt(path):
    path = Path(path)
    value = json.loads(path.read_bytes())
    material = {k: v for k, v in value.items() if k != "receipt_sha256"}
    if value.get("schema") != SCHEMA or value.get("receipt_sha256") != writer.digest(material):
        raise ValueError("D_supplement_display_receipt_drift")
    return value


def _verified_continuation(repo, plan, request_id, key, request_file):
    """Authorize only a sealed episode's frozen immediate successor; never a stale bypass."""
    repo = Path(repo).resolve()
    path = Path(request_file)
    if path.is_symlink() or path.name != "request.json":
        raise ValueError("D_continuation_request_invalid")
    path = path.resolve()
    path.relative_to((writer.private_root(repo) / "d-turns").resolve())
    saved = json.loads(path.read_bytes())
    args, payload = saved["arguments"], saved["frozen_payload"]
    rows = payload.get("episodes", [])
    if (len(rows) != 1 or payload.get("schema") != SESSION_SCHEMA
            or args.get("repo") != str(repo)
            or args.get("payload", {}).get("session_id") != payload.get("session_id")
            or args.get("next_request_id") != request_id or args.get("next_key") != key):
        raise ValueError("D_continuation_request_binding_invalid")
    prior_path = Path(args["receipt_path"]).resolve()
    prior_path.relative_to(receipt_root(repo).resolve())
    prior = read_receipt(prior_path)
    row = rows[0]
    session = payload["session_id"]
    assert_session_open(repo, session)
    identity = writer.digest({"session_id": session, "episode_id": row["episode_id"],
                              "display_sha256": prior["receipt_sha256"]})
    if path != (writer.private_root(repo) / "d-turns" / identity / "request.json").resolve():
        raise ValueError("D_continuation_identity_invalid")
    imported = bridge.load_imported_d_receipt(repo, plan["target_date"])
    pointer = bridge._json(repo / ".private/study-bridge/d-current" / (plan["target_date"] + ".json"))
    if (imported is None or imported != plan
            or Path(args["plan_path"]).resolve() != Path(pointer["receipt_path"]).resolve()
            or prior.get("native_import_receipt_sha256") != pointer["receipt_sha256"]):
        raise ValueError("D_continuation_plan_binding_invalid")
    items = plan.get("learning_items", [])
    ids = [item["request_id"] for item in items]
    if len(ids) != len(set(ids)) or prior["request_id"] not in ids:
        raise ValueError("D_continuation_queue_invalid")
    index = ids.index(prior["request_id"])
    if index + 1 >= len(ids) or ids[index + 1] != request_id or row["request_id"] != ids[index]:
        raise ValueError("D_continuation_requires_immediate_successor")
    item = items[index]
    if (any(item["selection"][k] != prior[k] for k in ("true_item_id", "surface_sha256"))
            or item["question_id"] != prior["source_formal_id"]
            or item["concept_id"] != prior["concept_id"]):
        raise ValueError("D_continuation_current_display_binding_invalid")
    progress_path = episode_capture._operation_root(repo) / "d-supplement-progress" / (
        writer.digest({"session_id": session, "request_ids": [prior["request_id"]]}) + ".json")
    progress = json.loads(progress_path.read_bytes())
    records = progress.get("episode_receipts", [])
    if (progress.get("session_id") != session or len(records) != 1
            or records[0].get("request_id") != prior["request_id"]
            or records[0].get("episode_id") != row["episode_id"]):
        raise ValueError("D_continuation_progress_binding_invalid")
    record = records[0]
    try:
        verified = episode_capture.verify_episode_capture(repo, record["receipt_ref"])
    except episode_capture.StudyEpisodeCaptureError as exc:
        raise ValueError("D_continuation_capture_verification_failed:" + str(exc)) from exc
    if (not verified.get("capture_verified") or not verified.get("package_verified")
            or any(verified.get(k) != record.get(k) for k in
                   ("receipt_sha256", "capture_id", "capture_payload_sha256", "episode_id"))):
        raise ValueError("D_continuation_capture_binding_invalid")
    opened = episode_capture.packages.verify_package_binding(
        verified["conversation_package_locator"],
        expected_canonical_sha256=verified["conversation_package_sha256"], require_sealed=True,
        private_root=episode_capture._default_package_root(repo))
    source = opened["source"]["source"]
    authorization = source.get("authorization", {})
    if (source.get("display_binding") != prior or source.get("episode_id") != row["episode_id"]
            or authorization.get("session_id") != session
            or authorization.get("request_id") != prior["request_id"]):
        raise ValueError("D_continuation_episode_binding_invalid")
    return pointer


def check_continuation(repo, request_file):
    """Read-only check of a real saved turn; no commit and no new display exposure."""
    saved = json.loads(Path(request_file).read_bytes())
    args = saved["arguments"]
    plan = json.loads(Path(args["plan_path"]).read_bytes())
    return display(Path(repo), plan, args["next_request_id"], args["next_key"],
                   Path(args["details"]), Path(args["release"]),
                   review_date=dt.datetime.now(ZoneInfo("Asia/Shanghai")).date().isoformat(),
                   check_only=True, _continuation_request=request_file)


def _verified_ordered_start(repo, plan, request_id, key, execution_date, binding):
    """Bind fresh ordered startup to the unchanged admitted queue, not live ranks.

    The live request/candidate membership and exact-source checks still run in
    _display. Only the full-library scheduling version may have moved since
    preparation; this proof cannot start an arbitrary item or reorder a queue.
    """
    raw = Path(binding["handoff"]).read_bytes()
    if hashlib.sha256(raw).hexdigest() != binding["handoff_sha256"]:
        raise ValueError("D_learning_handoff_hash_drift")
    handoff = json.loads(raw)
    assert_session_open(repo, binding["session_id"])
    imported = bridge.load_imported_d_receipt(repo, plan["target_date"])
    pointer = bridge._json(Path(repo) / ".private/study-bridge/d-current" / (plan["target_date"] + ".json"))
    items = plan.get("learning_items", [])
    ids = [item["request_id"] for item in items]
    true_ids = [item["selection"]["true_item_id"] for item in items]
    if (handoff.get("learning_mode") != "ordered_native_D" or imported != plan
            or handoff.get("source_basis") != plan.get("source_basis")
            or not ids or ids != handoff.get("learning_request_ids")
            or len(ids) != len(set(ids)) or len(true_ids) != len(set(true_ids))
            or any(not re.fullmatch(r"EXAM408-\d{4}-Q\d{2}(?:-P\d{2})?", tid) for tid in true_ids)
            or Path(handoff["native_import_receipt"]).resolve() != Path(pointer["receipt_path"]).resolve()
            or handoff["native_import_sha256"] != pointer["receipt_sha256"]
            or binding["native_import_sha256"] != pointer["receipt_sha256"]
            or request_id != ids[0] or key != f"{execution_date}-ordered-{ids[0]}"):
        raise ValueError("D_ordered_start_binding_invalid")
    return pointer


def display(repo, plan, request_id, key, details, release, *, review_date=None, check_only=False,
            _continuation_request=None, _ordered_start=None, user_requested_exact_review=False, timings=None):
    with measure(timings, "display_ms"):
        return _display(repo, plan, request_id, key, details, release, review_date=review_date,
                        check_only=check_only, _continuation_request=_continuation_request,
                        _ordered_start=_ordered_start,
                        user_requested_exact_review=user_requested_exact_review, timings=timings)


def _display(repo, plan, request_id, key, details, release, *, review_date=None, check_only=False,
             _continuation_request=None, _ordered_start=None, user_requested_exact_review=False, timings=None):
    """Selection is read-only; only this actual delivery records an exposure."""
    local.assert_no_active(repo)
    if plan.get("native_release_dir"):
        release = Path(plan["native_release_dir"]).resolve()
        if release.parent != (Path(details) / "corpus/408统考真题/style-releases").resolve():
            raise ValueError("D_native_release_outside_managed_corpus")
        local.control.load_style_release(release)
    target_date = plan["target_date"]
    execution_date = review_date or target_date
    if execution_date != dt.datetime.now(ZoneInfo("Asia/Shanghai")).date().isoformat():
        raise ValueError("D_supplement_display_must_be_today")
    if dt.date.fromisoformat(execution_date) < dt.date.fromisoformat(target_date):
        raise ValueError("D_supplement_execution_predates_preparation")
    continuing = _continuation_request is not None
    ordered_start = _ordered_start is not None
    if continuing and ordered_start:
        raise ValueError("D_ordered_start_cannot_replace_continuation")
    if continuing:
        pointer = _verified_continuation(repo, plan, request_id, key, _continuation_request)
    elif ordered_start:
        pointer = _verified_ordered_start(repo, plan, request_id, key, execution_date, _ordered_start)
    item = next((r for r in plan.get("learning_items", plan.get("supplemental_items", []))
                 if r["request_id"] == request_id), None)
    if item is None:
        raise ValueError("D_supplement_request_binding_invalid")
    from morning_question_policy_408 import user_excluded_exams
    if item.get("selection", {}).get("true_item_id") in user_excluded_exams(repo):
        raise ValueError("D_true_exam_user_permanently_excluded")
    target = receipt_root(repo) / (writer.digest(key) + ".json")
    if target.exists():
        previous = read_receipt(target)
        if previous["request_id"] != request_id or previous["review_date"] != execution_date:
            raise ValueError("D_display_idempotency_conflict")
        if continuing:
            if (any(previous[k] != item["selection"][k] for k in ("true_item_id", "surface_sha256"))
                    or previous["source_formal_id"] != item["question_id"]
                    or previous.get("native_import_receipt_sha256") != pointer["receipt_sha256"]):
                raise ValueError("D_continuation_existing_display_binding_invalid")
            if item["question_id"] not in local.control._node_snapshot(repo):
                raise ValueError("D_canonical_concept_anchor_missing")
            if check_only:
                return {"status": "verified_existing_display", "request_id": request_id,
                        "review_date": execution_date, "receipt_sha256": previous["receipt_sha256"],
                        "continuation_verified": True, "exposure_write_count": 0}
        return previous
    if not continuing:
        from exam_year_plan_408 import require_learning_plan
        require_learning_plan(repo, plan, execution_date)
    with measure(timings, "due_projection_ms"):
        current = plan if continuing else bridge.due(repo=repo, target_date=target_date)
    context = plan.get("context", plan)
    if not ordered_start and context.get("concept_priority_version") != current["concept_priority_version"]:
        raise ValueError("D_supplement_priority_stale")
    request = next((r for r in current["concept_requests"] if r["request_id"] == request_id), None)
    if ordered_start and request is None:
        request = next((r for r in current["concept_requests"] if r["concept_id"] == item["concept_id"]), None)
    is_substitute = item.get("request_kind") == "due_representative_substitute"
    is_original = item.get("request_kind") == "due_representative_original"
    is_canonical = item.get("selection", {}).get("surface_kind") == "canonical_original"
    from native_morning_sources_408 import SOURCE_BASIS
    native_plan = plan.get("source_basis") == SOURCE_BASIS
    contact_options = {}
    exact_options = {}
    if native_plan:
        from native_morning_sources_408 import contact_proof
        contact_options["hot_capture_proof"] = contact_proof(repo, execution_date, Path(details))
    if is_substitute or is_canonical or is_original:
        # A derived replacement exists only in the current sealed D execution
        # receipt. Never accept it as a fabricated native concept request.
        if not continuing and not ordered_start:
            with measure(timings, "import_verification_ms"):
                bridge.load_imported_d_run(repo, target_date, _current_context=current)
        pointer = bridge._json(Path(repo) / ".private/study-bridge/d-current" / (target_date + ".json"))
        saved = bridge._json(Path(pointer["receipt_path"]))
        if bridge._digest(saved) != bridge._digest(plan):
            raise ValueError("D_substitute_plan_not_current_import")
    if is_original:
        source_item = next((row for row in plan.get("selected_items", [])
                           if row["question_id"] == item["question_id"]
                           and row["review_target_concept_ids"] == [item["concept_id"]]), None)
        nodes = local.control._node_snapshot(repo)
        node = nodes.get(item["question_id"], {})
        if native_plan:
            from native_morning_sources_408 import exact_ids_for_node
            exact_sources = exact_ids_for_node(Path(details), node, item["question_id"]) if node else set()
            exact_options["exact_original_ids"] = exact_sources
        else:
            from morning_question_policy_408 import verified_exact_original_ids
            library = bridge._published_library(plan["source_commit"], repo, original_mappings_only=True)
            exact_sources = verified_exact_original_ids(node, item["question_id"], library)
            exact_options["exact_original_source_commit"] = plan["source_commit"]
        expected = item["selection"]
        due_ids = (plan["full_due_formal_node_ids"] if continuing else
                   next((r["formal_node_ids"] for r in current["due_concepts"]
                         if r["concept_id"] == item["concept_id"]), []))
        if (source_item is None or item["question_id"] not in due_ids
                or item.get("execution_kind") != "original_review"
                or expected["true_item_id"] not in exact_sources
                or source_item.get("preferred_true_item_id") != expected["true_item_id"]
                or source_item.get("surface_sha256") != expected["surface_sha256"]
                or (not ordered_start and plan.get("due_version") != current["due_version"])):
            raise ValueError("D_ordered_original_binding_invalid")
        request = {"request_id": request_id, "concept_id": item["concept_id"]}
        selected, reasons = local.select_canonical_concept(
            repo, execution_date, details, release, item["question_id"], item["concept_id"],
            expected["true_item_id"], expected["surface_sha256"], set(), request_id,
            **exact_options, **contact_options)
    elif is_substitute:
        source_item = next((row for row in plan.get("substitute_items", [])
                            if row["question_id"] == item["question_id"]
                            and row["review_target_concept_ids"] == [item["concept_id"]]), None)
        if continuing or ordered_start:
            fields = ("request_kind", "concept_id", "question_id", "formal_node_sha256", "true_item_id",
                      "surface_sha256", "substituted_due_formal_node_ids", "due_version", "substitution_reason")
            request = {k: item[k] for k in fields}
            request["request_id"] = "DREP-" + bridge._digest(request)[:24].upper()
            if (source_item is None or request["request_id"] != request_id
                    or item["due_version"] != plan["due_version"]
                    or not item["substituted_due_formal_node_ids"]
                    or not set(item["substituted_due_formal_node_ids"]) <= set(plan["full_due_formal_node_ids"])
                    or set(source_item["suppressed_same_concept_question_ids"]) != set(item["substituted_due_formal_node_ids"])
                    or any(source_item[k] != item[k] for k in ("true_item_id", "surface_sha256"))
                    or (ordered_start and not set(item["substituted_due_formal_node_ids"])
                        <= set(current["coverage"]["due_formal_node_ids"]))
                    or not is_canonical):
                raise ValueError("D_substitute_request_binding_invalid")
            selected, reasons = local.select_canonical_concept(
                repo, execution_date, details, release, item["question_id"], item["concept_id"],
                item["true_item_id"], item["surface_sha256"], set(), request_id, **contact_options)
        else:
            request = local.substitute_request(source_item, current, local.control._node_snapshot(repo))
            if request["request_id"] != request_id or any(item.get(k) != v for k, v in request.items()):
                raise ValueError("D_substitute_request_binding_invalid")
            selected, reasons = local.select_substitute(repo, execution_date, details, release, current, source_item, set(),
                                                        **contact_options)
    else:
        if request is None or item["question_id"] not in request["candidate_formal_node_ids"]:
            raise ValueError("D_supplement_request_binding_invalid")
        if is_canonical:
            expected = item["selection"]
            selected, reasons = local.select_canonical_concept(
                repo, execution_date, details, release, item["question_id"], request["concept_id"],
                expected["true_item_id"], expected["surface_sha256"], set(), request_id, **contact_options)
        else:
            _, select = local.selectors(repo, execution_date, details, release, current)
            selected, reasons = select(item["question_id"], request["concept_id"], set(), item["selection"].get("variant_id"))
    expected = item["selection"]
    if (selected is None and user_requested_exact_review and is_canonical
            and reasons and all(
                reason in {"recent_exact_original_without_verified_variant", "D_canonical_concept_surface_drift"}
                or (isinstance(reason, str) and re.fullmatch(r"D_exact_true_exam_used_within_[1-9]\d*_days", reason))
                for reason in reasons)):
        selected, reasons = local.select_canonical_concept(
            repo, execution_date, details, release, item["question_id"], item["concept_id"],
            expected["true_item_id"], expected["surface_sha256"], set(), request_id,
            user_requested_exact_review=True, **exact_options, **contact_options)
    if selected is None:
        raise ValueError("D_supplement_blocked:" + str(reasons))
    if any(selected.get(k) != expected.get(k) for k in ("true_item_id", "surface_sha256")):
        raise ValueError("D_supplement_surface_changed")
    from d_asset_delivery_408 import prepared_question_assets
    prepared_assets = prepared_question_assets(plan, item)
    if check_only:
        return {"status": "ready_for_display", "request_id": request_id,
                "preparation_date": target_date, "review_date": execution_date,
                "true_item_id": selected["true_item_id"], "exposure_write_count": 0,
                "user_requested_exact_review": user_requested_exact_review,
                "prepared_image_count": len(prepared_assets)}
    import morning_campaign_display_408 as surfaces
    kind = selected.get("surface_kind", "verified_variant")
    if kind == "canonical_original":
        public = surfaces._canonical_surface(
            details_root=details, release_dir=release,
            release_id=selected["exam_style_release_id"],
            item={"source_id": selected["true_item_id"], "surface_sha256": selected["surface_sha256"],
                  "grading_sha256": selected["answer_capsule_commitment_sha256"]}, selection=selected)
        acceptance_kind = "verified_canonical_source"
        source_label = ("统考真题原题复做｜" if is_original else "同知识点统考真题补充｜") + selected["true_item_id"] + "｜知识点 " + request["concept_id"]
    else:
        pool = local.control.variant_bank.pool_status(details, release)
        public = surfaces._variant_surface(details_root=details,
            item={"variant_id": selected["variant_id"], "surface_sha256": selected["surface_sha256"],
                  "grading_sha256": selected["answer_capsule_id"]}, selection=selected, pool=pool)
        acceptance_kind = selected.get("acceptance_kind", "verified_ready")
        source_prefix = "网页自检变式" if acceptance_kind == "web_self_checked_ready" else "变式"
        source_label = source_prefix + "｜知识点 " + request["concept_id"] + "｜原型 " + selected["blocked_original_true_item_id"]
    value = {"schema": SCHEMA, "review_date": execution_date, "request_id": request_id,
             "concept_id": request["concept_id"], "source_formal_id": item["question_id"],
             "true_item_id": selected["true_item_id"], "surface_sha256": selected["surface_sha256"],
             "surface_kind": kind, "concept_priority_version": current["concept_priority_version"],
             "acceptance_kind": acceptance_kind,
             "source_label": source_label,
             "learner_surface": {"stem": public["stem"], "options": public["options"]},
             "displayed_at": dt.datetime.now(dt.timezone.utc).isoformat(),
             "execution_kind": "original_review" if is_original else "concept_supplement", "question_fsrs_write_count": 0,
             "original_question_redo_write_count": 0}
    if execution_date != target_date:
        value["preparation_date"] = target_date
    if user_requested_exact_review:
        value["user_requested_exact_review"] = True
    if is_substitute:
        value["request_kind"] = "due_representative_substitute"
        value["substituted_due_formal_node_ids"] = request["substituted_due_formal_node_ids"]
        value["due_version"] = request["due_version"]
        value["native_import_receipt_sha256"] = pointer["receipt_sha256"]
    if is_original:
        value["request_kind"] = "due_representative_original"
        value["due_version"] = plan["due_version"]
        value["native_import_receipt_sha256"] = pointer["receipt_sha256"]
    if plan.get("learning_items") and is_canonical:
        value["native_import_receipt_sha256"] = pointer["receipt_sha256"]
    if kind == "canonical_original":
        value["question_assets"] = prepared_assets or public["question_assets"]
        value["terminal_evidence_assets"] = list(public["terminal_evidence_assets"])
        if prepared_assets:
            value["question_presentation"] = plan["question_presentation"]
    value["receipt_sha256"] = writer.digest(value)
    from exam_tutoring_context_408 import _atomic_write
    _atomic_write(target, writer.encoded(value), mode=0o600)
    return value


def commit(repo, payload, receipt_paths, *, followup=True, timings=None):
    with session_lock(repo, payload.get("session_id")):
        assert_session_open(repo, payload.get("session_id"))
        return _commit(repo, payload, receipt_paths, followup=followup, timings=timings)


def _commit(repo, payload, receipt_paths, *, followup=True, timings=None):
    payload = json.loads(writer.encoded(payload))
    repo = Path(repo).resolve()
    session_id = payload.get("session_id")
    if isinstance(session_id, str) and session_id:
        legacy_path = (
            writer.private_root(repo)
            / "sessions"
            / writer.digest(session_id)
            / "session.json"
        )
        if legacy_path.is_file() and payload.get("schema") == writer.SCHEMA:
            # Sessions committed under the previous contract remain native
            # concept-review evidence.  Recovery must finish that exact
            # postcommit chain and must not manufacture episode Captures.
            return writer.recover(
                repo,
                "CR408-" + writer.digest(session_id),
                followup=followup,
            )
    if payload.get("schema") != SESSION_SCHEMA or set(payload) != {
        "schema", "subject", "session_id", "study_date", "started_at",
        "ended_at", "episodes",
    }:
        raise ValueError("D_supplement_episode_session_v2_required")
    if payload.get("subject") != "408" or not isinstance(session_id, str) or not session_id:
        raise ValueError("D_supplement_session_identity_invalid")
    try:
        started = dt.datetime.fromisoformat(payload["started_at"].replace("Z", "+00:00"))
        ended = dt.datetime.fromisoformat(payload["ended_at"].replace("Z", "+00:00"))
        day = dt.date.fromisoformat(payload["study_date"])
    except (AttributeError, TypeError, ValueError) as exc:
        raise ValueError("D_supplement_session_time_invalid") from exc
    if (
        started.tzinfo is None
        or ended.tzinfo is None
        or ended < started
        or started.astimezone(ZoneInfo("Asia/Shanghai")).date() != day
    ):
        raise ValueError("D_supplement_session_time_invalid")
    episodes = payload.get("episodes")
    if not isinstance(episodes, list) or not episodes:
        raise ValueError("D_supplement_episodes_required")
    receipts = []
    for raw in receipt_paths:
        path = Path(raw).resolve()
        path.relative_to(receipt_root(repo).resolve())
        receipts.append(read_receipt(path))
    if not receipts:
        raise ValueError("D_supplement_display_evidence_required")
    by_request = {r["request_id"]: r for r in receipts}
    if len(by_request) != len(receipts):
        raise ValueError("D_supplement_duplicate_display_receipt")
    episode_by_request = {}
    required_episode = {
        "request_id", "episode_id", "turns", "attachments",
        "observed_outcome", "missing_fields", "frozen_feedback",
    }
    for row in episodes:
        if not isinstance(row, dict) or set(row) != required_episode:
            raise ValueError("D_supplement_episode_fields_invalid")
        request_id = row.get("request_id")
        if not isinstance(request_id, str) or request_id in episode_by_request:
            raise ValueError("D_supplement_episode_request_invalid")
        receipt = by_request.get(request_id)
        if receipt is None or payload["study_date"] != receipt["review_date"]:
            raise ValueError("D_supplement_episode_display_binding_invalid")
        if ended < dt.datetime.fromisoformat(receipt["displayed_at"]):
            raise ValueError("D_supplement_outcome_predates_display")
        episode_by_request[request_id] = row
    if set(by_request) != set(episode_by_request):
        raise ValueError("D_supplement_all_displays_require_real_episode")
    preparation_dates = {r.get("preparation_date", r["review_date"]) for r in receipts}
    if len(preparation_dates) != 1:
        raise ValueError("D_supplement_mixed_preparations")
    preparation_date = next(iter(preparation_dates))

    results = []
    for request_id in sorted(by_request):
        display_receipt = by_request[request_id]
        row = episode_by_request[request_id]
        receipt_path = next(
            Path(raw).resolve()
            for raw in receipt_paths
            if read_receipt(raw)["request_id"] == request_id
        )
        attachments = list(row["attachments"])
        for asset in display_receipt.get("question_assets", []):
            attachments.append({"path": asset["path"], "sha256": asset["sha256"],
                                "role": "question_image", "label": "verified canonical question image"})
        attachments.extend(display_receipt.get("terminal_evidence_assets", []))
        attachments.append(
            {
                "path": str(receipt_path),
                "role": "other_attachment",
                "mime_type": "application/json",
                "label": "verified_question_display",
            }
        )
        source = {
            "source_id": display_receipt["true_item_id"],
            "session_id": session_id,
            "item_id": request_id,
            "subject": display_receipt["concept_id"][:2],
            "question_type": "D_original_review" if display_receipt.get("execution_kind") == "original_review" else "D_concept_supplement",
            "source_formal_id": display_receipt["source_formal_id"],
            "concept_id": display_receipt["concept_id"],
            "surface_kind": display_receipt["surface_kind"],
            "acceptance_kind": display_receipt.get("acceptance_kind"),
            "source_label": display_receipt["source_label"],
            "displayed_at": display_receipt.get("displayed_at"),
            "question": display_receipt["learner_surface"]["stem"],
            "options": display_receipt["learner_surface"]["options"],
        }
        result = episode_capture.record_episode(
            repo,
            {
                "schema": episode_capture.SCHEMA,
                "episode_id": row["episode_id"],
                "execution_kind": display_receipt.get("execution_kind", "concept_supplement"),
                "study_date": payload["study_date"],
                "source": source,
                "turns": row["turns"],
                "attachments": attachments,
                "observed_outcome": row["observed_outcome"],
                "authorization": {
                    "kind": "d_concept_supplement_session",
                    "session_id": session_id,
                    "request_id": request_id,
                    "started_at": payload["started_at"],
                    "ended_at": payload["ended_at"],
                },
                "display_binding": display_receipt,
                "missing_fields": row["missing_fields"],
                "frozen_feedback": row["frozen_feedback"],
                "identity_hint": ({
                    "status": "existing", "mode": "redo",
                    "formal_id": display_receipt["source_formal_id"],
                    "basis": "native-verified-exact-due-original",
                } if display_receipt.get("execution_kind") == "original_review" else {
                    "status": "unknown",
                    "mode": "unknown",
                    "basis": "D-concept-supplement-does-not-create-or-redo-formal-question",
                }),
            },
            timings=timings,
        )
        results.append(result)

    progress = {
        "schema": "cs408-d-concept-supplement-progress-receipt-v2",
        "status": "awaiting_web_enhancement",
        "session_id": session_id,
        "submission_scope": "provided_display_receipts",
        "study_date": payload["study_date"],
        "display_count": len(receipts),
        "episode_capture_count": len(results),
        "episode_receipts": [
            {
                "request_id": request_id,
                "episode_id": episode_by_request[request_id]["episode_id"],
                "receipt_ref": result["receipt_ref"],
                "receipt_sha256": result["receipt_sha256"],
                "capture_id": result["capture_id"],
                "capture_payload_sha256": result["capture_payload_sha256"],
            }
            for request_id, result in zip(sorted(by_request), results)
        ],
        "concept_review_commit_count": 0,
        "question_write_count": 0,
        "fsrs_write_count": 0,
        "original_question_fsrs_write_count": 0,
        "formal_write_count": 0,
        "personal_summary_write_count": 0,
        "background_processing": "none",
        "mastery_claimed": False,
    }
    if preparation_date != payload["study_date"]:
        progress["preparation_date"] = preparation_date
    progress_root = episode_capture._operation_root(repo) / "d-supplement-progress"
    # One question may be sealed immediately while the same learning session
    # continues. Distinct submitted display sets must not collide or require
    # replaying all earlier questions to save the next one.
    progress_path = progress_root / (writer.digest({
        "session_id": session_id, "request_ids": sorted(by_request),
    }) + ".json")
    raw = json.dumps(progress, ensure_ascii=False, sort_keys=True, indent=2).encode() + b"\n"
    if progress_path.exists():
        if progress_path.is_symlink() or not progress_path.is_file() or progress_path.read_bytes() != raw:
            raise ValueError("D_supplement_progress_receipt_conflict")
    else:
        episode_capture._atomic_write(progress_path, raw)
    return {
        **progress,
        "capture_verified": all(row.get("capture_verified") is True for row in results),
        "package_verified": all(row.get("package_verified") is True for row in results),
        "progress_receipt_ref": str(progress_path),
        "progress_receipt_sha256": __import__("hashlib").sha256(raw).hexdigest(),
    }


def exposure_rows(repo, day, window_days):
    rows = []
    for path in sorted(receipt_root(repo).glob("*.json")):
        value = read_receipt(path)
        gap = (day - dt.date.fromisoformat(value["review_date"])).days
        if 0 <= gap < window_days:
            rows.append({"receipt_sha256": value["receipt_sha256"], "review_date": value["review_date"],
                "gap_days": gap, "session_id": "D-supplement-" + path.stem, "item_id": value["request_id"],
                "true_item_id": value["true_item_id"], "surface_kind": value.get("surface_kind", "verified_variant"),
                "acceptance_kind": value.get("acceptance_kind", "verified_ready"),
                "surface_sha256": value["surface_sha256"], "surface_identity_kind": "campaign_true_item_id",
                "surface_identity_sha256": writer.digest({"kind": "campaign_true_item_id", "true_item_id": value["true_item_id"]}),
                "question_asset_sha256": next((asset["sha256"] for asset in value.get("question_assets", [])), None)})
    return rows


def delivery(receipt, path, *, repo=None):
    assets = receipt.get("question_assets", [])
    surface = receipt["learner_surface"]
    if assets:
        body = "\n\n".join(f"![题目 {i}]({asset['path']})" for i, asset in enumerate(assets, 1))
    else:
        body = surface["stem"] + "\n\n" + "\n\n".join(
            f"{letter}. {text}" for letter, text in surface["options"].items())
    result = {
        **receipt,
        "receipt_path": str(path),
        "presentation_markdown": receipt["source_label"] + "\n\n" + body,
        "presentation_mode": "images_only" if assets else "text",
        # Already verified by the native surface loader; never guess unit paths.
        "private_material_refs": receipt.get("terminal_evidence_assets", []),
    }
    if repo is None:
        # Preserve the historical pure two-argument projection for external
        # callers; native runtime paths pass their verified repository below.
        return result
    from study_visual_http_408 import attach_delivery
    return attach_delivery(Path(repo), result)


def turn(repo, payload, receipt_path, *, plan_path=None, next_request_id=None,
         next_key=None, details=None, release=None, review_date=None, timings=None):
    with session_lock(repo, payload.get("session_id")):
        assert_session_open(repo, payload.get("session_id"))
        return _turn(repo, payload, receipt_path, plan_path=plan_path, next_request_id=next_request_id,
                     next_key=next_key, details=details, release=release,
                     review_date=review_date, timings=timings)


def _turn(repo, payload, receipt_path, *, plan_path=None, next_request_id=None,
          next_key=None, details=None, release=None, review_date=None, timings=None):
    """Freeze the clock once, seal one episode, then deliver its authorized successor."""
    repo = Path(repo).resolve()
    receipt_path = Path(receipt_path).resolve()
    receipt_path.relative_to(receipt_root(repo).resolve())
    current = read_receipt(receipt_path)
    episodes = payload.get("episodes", [])
    if (payload.get("schema") != SESSION_SCHEMA or len(episodes) != 1
            or episodes[0].get("request_id") != current["request_id"]):
        raise ValueError("D_turn_requires_one_current_episode")
    if bool(next_request_id) != bool(next_key) or (next_request_id and not plan_path):
        raise ValueError("D_turn_next_requires_plan_request_and_key")
    if next_request_id == current["request_id"]:
        raise ValueError("D_turn_successor_must_differ")
    arguments = dict(repo=str(repo), payload=payload, receipt_path=str(receipt_path),
        plan_path=str(Path(plan_path).resolve()) if plan_path else None,
        next_request_id=next_request_id, next_key=next_key,
        details=str(Path(details or local.control.DEFAULT_DETAILS_ROOT).resolve()),
        release=str(Path(release or local.control.DEFAULT_RELEASE).resolve()), review_date=review_date)
    identity = writer.digest({"session_id": payload.get("session_id"),
        "episode_id": episodes[0].get("episode_id"), "display_sha256": current["receipt_sha256"]})
    root = writer.private_root(repo) / "d-turns"
    request_path = root / identity / "request.json"
    result_path = root / identity / "result.json"
    with episode_capture._operation_lock(root, identity):
        if request_path.exists():
            saved = json.loads(request_path.read_bytes())
            if saved["arguments"] != arguments:
                raise ValueError("D_turn_request_conflict_use_original_request")
            frozen = saved["frozen_payload"]
        else:
            frozen = json.loads(writer.encoded(payload))
            frozen.setdefault("study_date", current["review_date"])
            frozen.setdefault("started_at", current["displayed_at"])
            frozen.setdefault("ended_at", dt.datetime.now(dt.timezone.utc).isoformat())
            saved = {"arguments": arguments, "frozen_payload": frozen,
                     "time_source": "display_receipt_and_first_submission_clock"}
            episode_capture._atomic_write(request_path, writer.encoded(saved))

        with measure(timings, "capture_ms"):
            progress = commit(repo, frozen, [receipt_path], timings=timings)
        if not progress.get("capture_verified") or not progress.get("package_verified"):
            raise ValueError("D_turn_capture_verification_failed")
        result = {"schema": "cs408-d-supplement-turn-v1", "capture": progress,
                  "capture_verified": True, "package_verified": True,
                  "request_file": str(request_path), "result_file": str(result_path),
                  "next_action": "hold", "next_display": None}
        if next_request_id:
            try:
                plan = json.loads(Path(plan_path).read_bytes())
                ordered = plan.get("schema") == "cs408-D-import-receipt-v1" and bool(plan.get("learning_items"))
                next_path = receipt_root(repo) / (writer.digest(next_key) + ".json")
                # A completed handoff can be reopened without another exposure or clock check.
                previous = json.loads(result_path.read_bytes()) if result_path.exists() else {}
                if previous.get("next_action") == "present_next":
                    if ordered:
                        _verified_continuation(repo, plan, next_request_id, next_key, request_path)
                    shown = read_receipt(next_path)
                    if shown["receipt_sha256"] != previous["next_display"]["receipt_sha256"]:
                        raise ValueError("D_turn_next_receipt_drift")
                else:
                    if not any(row["request_id"] == current["request_id"]
                               for row in plan.get("learning_items", plan.get("supplemental_items", []))):
                        raise ValueError("D_turn_current_not_in_plan")
                    with measure(timings, "navigation_ms"):
                        shown = display(repo, plan, next_request_id, next_key,
                            Path(arguments["details"]), Path(arguments["release"]),
                            review_date=review_date or current["review_date"], timings=timings,
                            **({"_continuation_request": request_path} if ordered else {}))
                result.update(next_action="present_next", next_display=delivery(shown, next_path, repo=repo))
            except (ValueError, OSError, KeyError, RuntimeError) as exc:
                result.update(next_action="recover_delivery", delivery_error=str(exc))
        from syllabus_topic_review_408 import attach_terminal_review, display_review_identity
        identity=display_review_identity(current)
        if identity:
            terminal = frozen["episodes"][0].get("observed_outcome", {}).get("terminal_state")
            result = attach_terminal_review(
                {**result, "terminal_state": terminal}, identity,
                successor=result.get("next_display"),
            )
        episode_capture._atomic_write(result_path, writer.encoded(result))
        return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("operation", choices=["display", "commit", "turn", "check-continuation"])
    parser.add_argument("--repo", type=Path, default=bridge.REPO)
    parser.add_argument("--plan", type=Path)
    parser.add_argument("--request-id")
    parser.add_argument("--idempotency-key")
    parser.add_argument("--review-date", help="Explicit execution date for a postponed prepared review")
    parser.add_argument("--check-only", action="store_true", help="Validate selection without displaying or recording exposure")
    parser.add_argument("--user-requested-exact-review", action="store_true",
                        help="Only for an explicit user request to study this exact prepared item; preserve exposure history")
    parser.add_argument("--details-root", type=Path, default=local.control.DEFAULT_DETAILS_ROOT)
    parser.add_argument("--release", type=Path, default=local.control.DEFAULT_RELEASE)
    parser.add_argument("--input", type=Path, help="Episode JSON path, or - for standard input")
    parser.add_argument("--display-receipt", type=Path, action="append", default=[])
    parser.add_argument("--next-request-id")
    parser.add_argument("--next-idempotency-key")
    parser.add_argument("--resume-request", type=Path)
    args = parser.parse_args()
    if args.operation == "check-continuation":
        if not args.resume_request:
            parser.error("check-continuation requires --resume-request")
        result = check_continuation(args.repo, args.resume_request)
    elif args.operation == "turn":
        if args.resume_request:
            saved = json.loads(args.resume_request.read_bytes())
            result = turn(**saved["arguments"])
        else:
            if not args.input or len(args.display_receipt) != 1:
                parser.error("turn requires --input and exactly one --display-receipt")
            raw = sys.stdin.buffer.read() if str(args.input) == "-" else args.input.read_bytes()
            result = turn(args.repo, json.loads(raw), args.display_receipt[0],
                plan_path=args.plan, next_request_id=args.next_request_id,
                next_key=args.next_idempotency_key, details=args.details_root,
                release=args.release, review_date=args.review_date)
    elif args.operation == "display":
        if not args.plan or not args.request_id or not args.idempotency_key:
            parser.error("display requires --plan --request-id --idempotency-key")
        result = display(args.repo, json.loads(args.plan.read_bytes()), args.request_id,
                         args.idempotency_key, args.details_root, args.release,
                         review_date=args.review_date, check_only=args.check_only,
                         user_requested_exact_review=args.user_requested_exact_review)
        if not args.check_only:
            result = delivery(result, receipt_root(args.repo) / (writer.digest(args.idempotency_key) + ".json"), repo=args.repo)
    else:
        if not args.input or not args.display_receipt:
            parser.error("commit requires --input and --display-receipt")
        raw = sys.stdin.buffer.read() if str(args.input) == "-" else args.input.read_bytes()
        result = commit(args.repo, json.loads(raw), args.display_receipt)
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
