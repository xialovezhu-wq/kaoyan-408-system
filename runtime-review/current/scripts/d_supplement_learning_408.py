#!/usr/bin/env python3
"""Bound D learning: transcript-backed Capture, native successor, compact delivery."""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import re
import sys
import tempfile
from time import perf_counter

import d_concept_supplement_408 as native
import exam_tutoring_context_408 as tutoring
import d_prepared_visuals_408 as prepared_visuals
from study_message_source_408 import resolve_display_episode
from study_timing_408 import measure, finish
from morning_local_followup_408 import terminal_delivery

SCHEMA = "cs408-d-supplement-learning-v1"
REPO = native.bridge.REPO


def _read(path):
    return json.loads(Path(path).read_bytes())


def _bound(body, repo):
    native.assert_session_open(repo, body["session_id"])
    path = Path(body["display_receipt"]).resolve()
    path.relative_to(native.receipt_root(repo).resolve())
    display = native.read_receipt(path)
    if display["receipt_sha256"] != body["display_receipt_sha256"]:
        raise ValueError("D_learning_current_display_changed")
    handoff_raw = Path(body["handoff"]).read_bytes()
    handoff = json.loads(handoff_raw)
    if (body.get("handoff_sha256") or handoff.get("prepared_visual_contract") == prepared_visuals.HTML_SCHEMA) and (
            body.get("handoff_sha256") != hashlib.sha256(handoff_raw).hexdigest()):
        raise ValueError("D_learning_prepared_handoff_hash_drift")
    plan_path = Path(handoff["native_import_receipt"])
    raw = plan_path.read_bytes()
    if hashlib.sha256(raw).hexdigest() != handoff["native_import_sha256"]:
        raise ValueError("D_learning_import_hash_drift")
    plan = json.loads(raw)
    items = plan.get("learning_items") or plan["supplemental_items"]
    ids = [item["request_id"] for item in items]
    if len(set(ids)) != len(ids) or ids != (handoff.get("learning_request_ids") or handoff["supplemental_request_ids"]):
        raise ValueError("D_learning_order_binding_drift")
    index = ids.index(display["request_id"])
    selected = items[index]["selection"]
    if any(selected[key] != display[key] for key in ("true_item_id", "surface_sha256")):
        raise ValueError("D_learning_surface_binding_drift")
    if (handoff.get("prepared_visual_contract") == prepared_visuals.HTML_SCHEMA and
            display.get("native_import_receipt_sha256") != handoff["native_import_sha256"]):
        raise ValueError("D_learning_display_import_binding_drift")
    progress_key = native.writer.digest({"session_id": body["session_id"],
                                        "request_ids": [display["request_id"]]})
    progress = native.episode_capture._operation_root(repo) / "d-supplement-progress" / (progress_key + ".json")
    if progress.exists():
        raise ValueError("D_learning_display_already_captured_use_latest_result")
    return display, handoff, plan_path, ids, index


def _teaching_mechanisms(body):
    """Keep an explicit selection stable without guessing missing scope."""
    if "teaching_mechanisms" not in body:
        return None
    values = body["teaching_mechanisms"]
    if not isinstance(values, list) or any(not isinstance(value, str) for value in values):
        raise ValueError("D_learning_teaching_mechanisms_must_be_string_list")
    return list(dict.fromkeys(value.strip() for value in values if value.strip()))


def _input_context(body, display):
    current = dict(schema=SCHEMA, handoff=body["handoff"], session_id=body["session_id"],
                   display_receipt=display["receipt_path"], display_receipt_sha256=display["receipt_sha256"])
    if body.get("handoff_sha256"):
        current["handoff_sha256"] = body["handoff_sha256"]
    if body.get("display_receipt_sha256") == display["receipt_sha256"]:
        mechanisms = _teaching_mechanisms(body)
        if mechanisms is not None:
            current["teaching_mechanisms"] = mechanisms
    return current


def _teaching_entry(handoff, display):
    raw = Path(handoff["teaching_index_path"]).read_bytes()
    if hashlib.sha256(raw).hexdigest() != handoff["teaching_index_sha256"]:
        raise ValueError("D_learning_teaching_index_drift")
    return json.loads(raw).get(display["request_id"], {})


def _materials(handoff, display, *, entry=None):
    if entry is None:
        entry = _teaching_entry(handoff, display)
    root = Path(handoff["output_root"]).resolve()
    refs = []
    for label, row in entry.get("private_after_attempt", {}).items():
        path = (root / row["relative_path"]).resolve()
        path.relative_to(root)
        refs.append(dict(path=str(path), sha256=row["sha256"], label=label))
    return refs


def _grading_reference(display, view):
    """Read only the current display's hash-bound answer after an attempt."""
    if view not in {"after_attempt", "direct"}:
        return {"status": "protected"}
    for ref in display.get("terminal_evidence_assets", []):
        if ref.get("label") != "verified grading and explanation capsule":
            continue
        try:
            path = Path(ref["path"])
            if path.is_symlink() or path.stat().st_size > 2 * 1024 * 1024:
                raise ValueError("D_grading_reference_invalid_file")
            raw = path.read_bytes()
            if hashlib.sha256(raw).hexdigest() != ref["sha256"]:
                raise ValueError("D_grading_reference_hash_drift")
            value = json.loads(raw)
            if isinstance(value, dict) and value.get("schema") == "cs408-exam-practice-reference-v1":
                if (value.get("true_item_id") != display["true_item_id"]
                        or value.get("surface_sha256") != display.get("surface_sha256")
                        or value.get("grading_mode") != "semantic_current_answer_review"):
                    raise ValueError("D_practice_grading_reference_binding_drift")
                text = str(value.get("solution_ocr") or "")
                encoded = text.encode("utf-8")
                return dict(status="available", true_item_id=display["true_item_id"],
                    parent_true_item_id=value.get("parent_true_item_id"), selected_part=value.get("selected_part"),
                    grading_mode="semantic_current_answer_review", reference_scope=value.get("reference_scope"),
                    reference_text=encoded[:9000].decode("utf-8", errors="ignore"), truncated=len(encoded)>9000,
                    solution_asset_refs=value.get("solution_asset_refs", []),
                    path=str(path), sha256=ref["sha256"], private=True,
                    use="current_subquestion_reference_only_no_automatic_option_scoring")
            if not isinstance(value, dict) or value.get("canonical_id") != display["true_item_id"]:
                raise ValueError("D_grading_reference_identity_drift")
            answer = value.get("correct_option")
            if answer not in {"A", "B", "C", "D"}:
                raise ValueError("D_grading_reference_option_unavailable")
            return dict(status="available", true_item_id=display["true_item_id"],
                        correct_option=answer, path=str(path), sha256=ref["sha256"],
                        private=True, use="current_answer_verification_only")
        except (ValueError, OSError, KeyError, TypeError) as exc:
            return dict(status="unavailable", reason=str(exc), private=True)
    return dict(status="unavailable", reason="current_answer_reference_missing", private=True)


def _prepared_context(refs, display, view):
    """Return current-item teaching material in the same read-only answer query."""
    if view not in {"after_attempt", "direct"}:
        return {"status": "protected"}
    result = dict(true_item_id=display["true_item_id"], private=True,
                  use="current_question_material_not_current_personal_history", missing=[])
    for label, key, budget in (("solution_path", "solution", 5000),
                                ("teaching_path", "teaching", 3000)):
        ref = next((row for row in refs if row["label"] == label), None)
        if ref is None:
            result["missing"].append(dict(material=key, reason="missing_bound_reference"))
            continue
        try:
            path = Path(ref["path"])
            if path.is_symlink() or path.stat().st_size > 65536:
                raise ValueError("D_prepared_material_invalid_file")
            raw = path.read_bytes()
            if hashlib.sha256(raw).hexdigest() != ref["sha256"]:
                raise ValueError("D_prepared_material_hash_drift")
            # Keep the argument and its conditions; immutable source manifests
            # remain on disk. Never read history or a successor's explanation.
            material = raw.decode("utf-8").split("\n## 固定版来源", 1)[0]
            encoded = material.encode("utf-8")
            result[key] = dict(path=str(path), sha256=ref["sha256"],
                               text=encoded[:budget].decode("utf-8", errors="ignore"),
                               truncated=len(encoded) > budget)
        except (ValueError, OSError, KeyError, TypeError) as exc:
            result["missing"].append(dict(material=key, reason=str(exc)))
    count = sum(key in result for key in ("solution", "teaching"))
    result["status"] = "available" if count == 2 else "partial" if count else "unavailable"
    return result


def _material_version(display, prepared, grading, visual=None):
    """A reuse token never bypasses the file/hash checks performed above."""
    if prepared.get("status") != "available" or grading.get("status") != "available":
        return None
    value = {
        "schema": "cs408-current-teaching-material-v1",
        "display_sha256": display["receipt_sha256"],
        "prepared": prepared,
        "grading": grading,
    }
    if visual and visual.get('preprocessed'):
        value['prepared_visual_html'] = visual
    elif visual is not None and visual.get('status') != 'not_prepared':
        commitment = prepared_visuals.material_commitment(visual)
        if commitment is None:
            return None
        value['prepared_visual'] = commitment
    return native.writer.digest(value)


def context(body, repo=REPO):
    started, timings = perf_counter(), {}
    mechanisms = _teaching_mechanisms(body)
    with measure(timings, "binding_ms"):
        display, handoff, _, ids, index = _bound(body, repo)
    shown = native.delivery(display, body["display_receipt"], repo=repo)
    view = body.get("view", "protected")
    query = dict(schema=tutoring.QUERY_INPUT_SCHEMA,
                 stage="pre_answer" if view == "protected" else "post_answer",
                 knowledge_points=[display["concept_id"]], weak_knowledge_points=[],
                 max_profiles=0, read_stats=False)
    if mechanisms:
        query["mechanism_terms"] = mechanisms
    # A D representative can replace source_formal_id. Only the verified
    # displayed true item identifies this question's precomputed exam opening.
    current_exam = display.get("true_item_id", "")
    query["current_question_id"] = (current_exam or None) if display.get("surface_kind") != "verified_variant" else None
    if display.get("surface_kind") != "verified_variant" and re.fullmatch(r"EXAM408-\d{4}-Q\d{2}(?:-P\d{2})?", current_exam):
        query["exam_question_id"] = current_exam
    elif display.get("surface_kind") == "verified_variant":
        query["exam_question_id"] = None
    with measure(timings, "personal_context_ms"):
        personal = tutoring.query_context(repo, query, teaching_view=view,
                                          known_version=body.get("known_version"),
                                          personal_scope="teaching", materials="core")
    with measure(timings, "question_material_ms"):
        refs = []
        if view in {"after_attempt", "direct"}:
            entry = _teaching_entry(handoff, display)
            refs = _materials(handoff, display, entry=entry)
        prepared = _prepared_context(refs, display, view)
        grading = _grading_reference(display, view)
        if view in {"after_attempt", "direct"} and handoff.get("prepared_visual_contract") == prepared_visuals.HTML_SCHEMA:
            visual = prepared_visuals.load_current_html(Path(handoff["output_root"]), entry.get("prepared_visual_html", {}),
                display, view=view, hint_level=body.get("hint_level", 5 if view == "direct" else None), teaching_context=personal)
        else:
            visual = prepared_visuals.load_current(handoff, display, view=view, teaching_context=personal)
        material_version = _material_version(display, prepared, grading, visual)
    current = _input_context(body, shown)
    if personal.get("version"):
        current["known_version"] = personal["version"]
    if material_version:
        current["known_material_version"] = material_version
        if body.get("known_material_version") == material_version:
            # Only immutable, already delivered material is omitted. The latest
            # personal context is independently queried on every teaching turn.
            prepared = dict(status="unchanged", version=material_version,
                            true_item_id=display["true_item_id"], private=True)
            grading = dict(status="unchanged", version=material_version,
                          true_item_id=display["true_item_id"], private=True)
            if visual.get('status') == 'available' and 'source_version' in visual:
                visual = dict(status='unchanged', version=material_version,
                              request_id=display['request_id'], true_item_id=display['true_item_id'],
                              source_version=visual['source_version'], private=True)
    result = dict(status="ready_for_teaching", next_action="continue_current_item",
                input_context=current, current_display=shown,
                current_image_paths=[asset["path"] for asset in shown.get("question_assets", [])]
                    if view == "protected" else [],
                progress=dict(position=index+1, total=len(ids)), teaching_context=personal,
                prepared_material_refs=refs,
                prepared_context=prepared, prepared_visual_context=visual,
                grading_reference=grading, material_version=material_version,
                timing_ms=finish(timings, started), timing_scope="current_invocation",
                image_delivery="prepared_markdown", image_inspection_required=False, write_count=0)
    if "cached_visuals" in shown:
        # after_attempt only grants teacher context; this pointer remains the
        # question stage until the learner explicitly requests another view.
        result["cached_visuals"] = shown["cached_visuals"]
    if "automatic_visual_teaching" in shown:
        result["automatic_visual_teaching"] = shown["automatic_visual_teaching"]
    if "teaching_screen" in body:
        result["teaching_screen_result"] = _visual_feedback_for_display(body, display, repo)
        # Existing one-call helpers already preserve verification; expose the
        # native UI receipt there without changing an installed Skill/helper.
        result.setdefault("verification", {})["automatic_3d_display"] = result["teaching_screen_result"]
        result["verification"]["write_count_scope"] = "learning_records_only; optional_decision_cache_and_owned_window_state_are_separate"
    return result


def _visual_feedback_for_display(body, display, repo):
    from study_visual_display_408 import feedback
    request = body.get("teaching_screen")
    if not isinstance(request, dict):
        raise ValueError("current_teaching_screen_request_required")
    if request.get("decision") == "wrong" and body.get("view", "after_attempt") == "protected":
        raise ValueError("protected_pre_answer_view_cannot_trigger_wrong_answer_explanation")
    return feedback(repo, display["true_item_id"], request.get("decision"),
                    stage=request.get("stage", "hint"), hint_level=request.get("hint_level", 1),
                    expected_surface_sha256=display["surface_sha256"],
                    focus_frame=request.get("focus_frame"),
                    tutor_presentation=request.get("tutor_presentation"),
                    teaching_goal=request.get("teaching_goal"),
                    current_difficulty=request.get("current_difficulty", ""),
                    solution_authorized=request.get("solution_authorized") is True or body.get("view") == "direct")


def visual_feedback(body, repo=REPO):
    """Current bound answer feedback; the UI state is separate from learning writes."""
    display, _, _, _, _ = _bound(body, repo)
    return _visual_feedback_for_display(body, display, repo)


def start(body, repo=REPO):
    """Verify an ordered handoff and follow only its saved, contiguous frontier.

    Fresh startup is the only display mutation. Recovery reads saved receipts;
    it never replays a completed answer, an exposure or a pending save.
    """
    repo = Path(repo).resolve()
    native.assert_session_open(repo, body.get("session_id"))
    started, timings = perf_counter(), {}
    with measure(timings, "startup_binding_ms"):
        raw = Path(body["handoff"]).read_bytes()
        if hashlib.sha256(raw).hexdigest() != body["handoff_sha256"]:
            raise ValueError("D_learning_handoff_hash_drift")
        handoff = json.loads(raw)
        if handoff.get("learning_mode") != "ordered_native_D" or not body.get("session_id"):
            raise ValueError("D_learning_ordered_session_required")
        plan_path = Path(handoff["native_import_receipt"]).resolve()
        raw = plan_path.read_bytes()
        if (hashlib.sha256(raw).hexdigest() != handoff["native_import_sha256"]
                or body.get("native_import_sha256", handoff["native_import_sha256"]) != handoff["native_import_sha256"]):
            raise ValueError("D_learning_import_hash_drift")
        plan = json.loads(raw)
        items = plan["learning_items"]
        ids = [row["request_id"] for row in items]
        true_ids = [row["selection"]["true_item_id"] for row in items]
        if (not ids or ids != handoff["learning_request_ids"] or len(set(ids)) != len(ids)
                or len(set(true_ids)) != len(true_ids)
                or any(not re.fullmatch(r"EXAM408-\d{4}-Q\d{2}(?:-P\d{2})?", item) for item in true_ids)):
            raise ValueError("D_learning_order_binding_drift")
        pointer = _read(repo / ".private/study-bridge/d-current" / (plan["target_date"] + ".json"))
        if (Path(pointer["receipt_path"]).resolve() != plan_path
                or pointer["receipt_sha256"] != handoff["native_import_sha256"]):
            raise ValueError("D_learning_import_pointer_drift")
        native.local.assert_no_active(repo)

    today = native.dt.datetime.now(native.ZoneInfo("Asia/Shanghai")).date()
    day = native.dt.date.fromisoformat(plan["target_date"])
    if day > today:
        raise ValueError("D_supplement_execution_predates_preparation")
    postponed = None
    if "postponed_review_date" in body:
        postponed = native.dt.date.fromisoformat(body["postponed_review_date"])
        if postponed <= day:
            raise ValueError("D_learning_postponement_must_follow_preparation")
        if postponed > today:
            raise ValueError("D_learning_postponed_review_date_is_future")
    # All ordered prompts use this deterministic first-display key. An already
    # known input_context should use context directly, including legacy keys.
    paths = []
    while day <= today:
        key = f"{day.isoformat()}-ordered-{ids[0]}"
        path = native.receipt_root(repo) / (native.writer.digest(key) + ".json")
        if path.is_file():
            paths.append(path)
        day += native.dt.timedelta(days=1)
    if postponed is not None:
        # A displayed-but-unanswered first item may move to the explicitly
        # requested day. Saved work always owns the frontier, including a
        # request frozen before its Capture completed. Keep earlier exposures.
        saved_paths = []
        for candidate in paths:
            shown = native.read_receipt(candidate)
            identity = native.writer.digest([body["session_id"], shown["receipt_sha256"]])
            root = native.writer.private_root(repo) / "d-learning" / identity
            if (root / "request.json").exists() or (root / "result.json").exists():
                saved_paths.append(candidate)
        if saved_paths:
            paths = saved_paths
        else:
            progress_root = native.episode_capture._operation_root(repo) / "d-supplement-progress"
            if any((progress_root / (native.writer.digest({"session_id": body["session_id"],
                    "request_ids": [request]}) + ".json")).exists() for request in ids):
                raise ValueError("D_learning_prior_progress_requires_known_context")
            key = f"{postponed.isoformat()}-ordered-{ids[0]}"
            requested_path = native.receipt_root(repo) / (native.writer.digest(key) + ".json")
            paths = [requested_path] if requested_path.is_file() else []
            if not paths and postponed != today:
                raise ValueError("D_learning_postponed_start_date_passed_without_display")
    if len(paths) > 1:
        raise ValueError("D_learning_multiple_initial_displays_use_known_context")
    created = not paths
    if created:
        from exam_year_plan_408 import require_learning_plan
        require_learning_plan(repo, plan, today.isoformat())
        progress_root = native.episode_capture._operation_root(repo) / "d-supplement-progress"
        if any((progress_root / (native.writer.digest({"session_id": body["session_id"],
                "request_ids": [request]}) + ".json")).exists() for request in ids):
            raise ValueError("D_learning_prior_progress_requires_known_context")
        key = f"{today.isoformat()}-ordered-{ids[0]}"
        path = native.receipt_root(repo) / (native.writer.digest(key) + ".json")
        native.display(repo, plan, ids[0], key, native.local.control.DEFAULT_DETAILS_ROOT,
                       native.local.control.DEFAULT_RELEASE, review_date=today.isoformat(), timings=timings,
                       _ordered_start={**{name: body[name] for name in
                           ("handoff", "handoff_sha256", "session_id")},
                           "native_import_sha256": handoff["native_import_sha256"]},
                       **({"user_requested_exact_review": True} if postponed is not None else {}))
    else:
        path = paths[0]

    last_verified = None
    for index, item in enumerate(items):
        shown = native.delivery(native.read_receipt(path), path, repo=repo)
        if (shown["request_id"] != item["request_id"]
                or shown.get("native_import_receipt_sha256") != handoff["native_import_sha256"]
                or shown["source_formal_id"] != item["question_id"]
                or any(shown[k] != item["selection"][k] for k in ("true_item_id", "surface_sha256"))):
            raise ValueError("D_learning_surface_binding_drift")
        identity = native.writer.digest([body["session_id"], shown["receipt_sha256"]])
        root = native.writer.private_root(repo) / "d-learning" / identity
        request_file, result_file = root / "request.json", root / "result.json"
        if not result_file.is_file():
            if request_file.exists():
                return dict(status="blocked", next_action="recover_same_request",
                            reason="D_learning_pending_save", learning_request_file=str(request_file))
            current = _input_context(body, shown)
            _bound(current, repo)
            if last_verified:
                _verify_start_capture(repo, *last_verified)
            return dict(status="ready_for_teaching", next_action="continue_current_item",
                        input_context=current, current_display=shown,
                        current_image_paths=[asset["path"] for asset in shown.get("question_assets", [])],
                        progress=dict(position=index+1, total=len(ids)), startup_recovered=not created,
                        verification=dict(handoff_sha256=body["handoff_sha256"],
                            native_import_sha256=handoff["native_import_sha256"], order_verified=True),
                        timing_ms=finish(timings, started), timing_scope="current_invocation",
                        write_count=1 if created else 0, formal_write_count=0,
                        image_delivery="prepared_markdown", image_inspection_required=False)
        request, result = _read(request_file), _read(result_file)
        original = request["input"]
        if (original["session_id"] != body["session_id"]
                or Path(original["handoff"]).resolve() != Path(body["handoff"]).resolve()
                or Path(original["display_receipt"]).resolve() != path.resolve()
                or original["display_receipt_sha256"] != shown["receipt_sha256"]
                or not result.get("capture_verified") or not result.get("package_verified")):
            raise ValueError("D_learning_saved_frontier_binding_invalid")
        expected_episode = body["session_id"] + ":" + item["request_id"]
        progress_path = native.episode_capture._operation_root(repo) / "d-supplement-progress" / (
            native.writer.digest({"session_id": body["session_id"], "request_ids": [item["request_id"]]}) + ".json")
        progress = _read(progress_path)
        if (progress.get("session_id") != body["session_id"]
                or progress.get("episode_receipts") != result["capture"].get("episode_receipts")):
            raise ValueError("D_learning_saved_progress_drift")
        last_verified = (result, expected_episode)
        if result.get("next_action") != "present_next":
            _verify_start_capture(repo, result, expected_episode)
            return dict(status="learning_complete" if result.get("supplemental_complete") else "navigation_held",
                        next_action="report_completion" if result.get("supplemental_complete") else result.get("next_action", "hold"),
                        progress=result["progress"], learning_request_file=str(request_file), result_file=str(result_file),
                        supplemental_complete=bool(result.get("supplemental_complete")), write_count=0,
                        timing_ms=finish(timings, started), timing_scope="current_invocation")
        if index+1 >= len(ids) or result["next_display"]["request_id"] != ids[index+1]:
            raise ValueError("D_learning_saved_successor_order_drift")
        nxt = result["next_input_context"]
        path = Path(nxt["display_receipt"]).resolve()
        path.relative_to(native.receipt_root(repo).resolve())
        if (nxt["session_id"] != body["session_id"]
                or native.read_receipt(path)["receipt_sha256"] != nxt["display_receipt_sha256"]):
            raise ValueError("D_learning_next_display_drift")
    raise ValueError("D_learning_frontier_unavailable")


def _verify_start_capture(repo, result, expected_episode):
    records = result["capture"]["episode_receipts"]
    if len(records) != 1 or records[0].get("episode_id") != expected_episode:
        raise ValueError("D_learning_saved_episode_drift")
    record = records[0]
    verified = native.episode_capture.verify_episode_capture(repo, record["receipt_ref"])
    if (not verified.get("capture_verified") or not verified.get("package_verified")
            or any(verified.get(k) != record.get(k) for k in
                   ("receipt_sha256", "capture_id", "capture_payload_sha256", "episode_id"))):
        raise ValueError("D_learning_saved_capture_drift")


def submit(body, repo=REPO):
    with native.session_lock(repo, body.get("session_id")):
        native.assert_session_open(repo, body.get("session_id"))
        return _submit(body, repo)


def _submit(body, repo=REPO):
    started, timings = perf_counter(), {}
    repo = Path(repo).resolve()
    if body.get("schema") != SCHEMA or body.get("intent", "complete") not in {"complete", "end", "skip"}:
        raise ValueError("D_learning_requires_real_terminal_intent")
    if not isinstance(body.get("session_id"), str) or not body["session_id"]:
        raise ValueError("D_learning_session_required")
    identity = native.writer.digest([body["session_id"], body["display_receipt_sha256"]])
    root = native.writer.private_root(repo) / "d-learning"
    request_file, result_file = root / identity / "request.json", root / identity / "result.json"
    with native.episode_capture._operation_lock(root, identity):
        if request_file.exists():
            saved = _read(request_file)
            if saved["input"] != body:
                raise ValueError("D_learning_request_conflict_use_original_request")
        else:
            # Reject malformed model fields before extracting/freezing evidence.
            # An invalid enum must not poison this episode's idempotency key.
            raw_outcome = body.get("observed_outcome")
            if not isinstance(raw_outcome, dict):
                raise ValueError("observed_outcome must be an object")
            provisional = dict(raw_outcome)
            # Check judgments before extracting evidence. Missing text fields
            # are filled from the verified transcript below, never generated.
            provisional.setdefault("first_response", None if raw_outcome.get("first_result") == "not_observed"
                                   else body.get("expected_user_text"))
            provisional.setdefault("final_response", body.get("expected_user_text"))
            native.episode_capture._normalize_outcome(provisional)
            native.episode_capture._normalize_attachments(body.get("attachments", []))
            with measure(timings, "binding_ms"):
                display, handoff, plan_path, ids, index = _bound(body, repo)
            with measure(timings, "transcript_extract_ms"):
                rows, refs, source_assets = resolve_display_episode(display, body["expected_user_text"],
                    evidence_dir=request_file.parent / "source-evidence")
            outcome = dict(raw_outcome)
            user_turns = [row["text"] for row in rows if row.get("role") == "user"]
            if not user_turns:
                raise ValueError("D_learning_verified_user_turn_missing")
            outcome.setdefault("first_response", None if outcome.get("first_result") == "not_observed"
                               else user_turns[0])
            outcome.setdefault("final_response", user_turns[-1])
            native.episode_capture._normalize_outcome(outcome)
            if body.get("intent", "complete") == "complete" and (
                outcome.get("terminal_state") not in {"correct", "prompted_correct"}
                or outcome.get("reasoning_result") != "sound"
                or outcome.get("prompt_dependency") == "not_observed"
            ):
                raise ValueError("D_learning_completion_requires_sound_current_reasoning")
            if body.get("intent") in {"end", "skip"} and outcome.get("terminal_state") != "unresolved":
                raise ValueError("D_learning_end_requires_unresolved")
            payload = dict(schema=native.SESSION_SCHEMA, subject="408", session_id=body["session_id"],
                episodes=[dict(request_id=display["request_id"],
                    episode_id=body["session_id"] + ":" + display["request_id"],
                    turns=rows, attachments=body.get("attachments", []) + source_assets,
                    missing_fields=body.get("missing_fields", []), observed_outcome=outcome,
                    frozen_feedback="")])
            next_id = ids[index+1] if index+1 < len(ids) and body.get("intent") != "end" else None
            arguments = dict(repo=str(repo), payload=payload, receipt_path=body["display_receipt"],
                plan_path=str(plan_path), next_request_id=next_id,
                next_key=f"{display['review_date']}-supp-{next_id}" if next_id else None)
            saved = dict(input=body, arguments=arguments, source_message_refs=refs,
                         progress=dict(position=index+1, total=len(ids)))
            native.episode_capture._atomic_write(request_file, native.writer.encoded(saved))
        replayed = result_file.exists() and _read(result_file).get("next_action") in {"present_next", "hold"}
        result = native.turn(**saved["arguments"], timings=timings)
        capture = result["capture"]
        result.update(status="episode_captured", capture_id=capture["episode_receipts"][0]["capture_id"],
            learning_request_file=str(request_file), result_file=str(result_file),
            evidence_source="verified_transcript", source_message_refs=saved["source_message_refs"],
            formal_write_count=0, fsrs_write_count=0, personal_summary_write_count=0,
            terminal_state=body["observed_outcome"]["terminal_state"], replayed=replayed,
            celebration_eligible=not replayed and body["observed_outcome"]["terminal_state"] in {"correct", "prompted_correct"})
        result["frontier_advanced"] = result["next_action"] == "present_next"
        shown = result["next_display"]
        result["next_image_paths"] = [asset["path"] for asset in shown.get("question_assets", [])] if shown else []
        result["image_preload_status"] = "ready" if result["next_image_paths"] else "not_applicable"
        result["image_delivery"] = "prepared_markdown"
        result["image_inspection_required"] = False
        result["progress"] = {**saved["progress"], "completed_position": saved["progress"]["position"]}
        result["supplemental_complete"] = saved["progress"]["position"] == saved["progress"]["total"]
        if shown:
            result["next_input_context"] = _input_context(body, shown)
            result["progress"]["position"] += 1
            try:
                handoff = _read(body["handoff"])
                if handoff.get("prepared_visual_contract") != prepared_visuals.HTML_SCHEMA:
                    result["next_display"]["prepared_material_refs"] = _materials(handoff, shown)
            except (ValueError, OSError, KeyError) as exc:
                result["prepared_materials_unavailable"] = str(exc)
        result.update(timing_ms=finish(timings, started), timing_scope="current_invocation")
        native.episode_capture._atomic_write(result_file, native.writer.encoded(result))
        return result


def session_status(body, repo=REPO):
    receipt = native.read_session_closure(repo, body["session_id"])
    return dict(status="session_closed" if receipt else "session_open", next_action="stop" if receipt else "resume",
                closure_receipt=receipt, closure_receipt_path=str(native.session_closure_path(repo, body["session_id"])),
                learning_write_count=0, formal_write_count=0, fsrs_write_count=0, personal_summary_write_count=0)


def image_handoff(result, repo=REPO):
    if result.get("schema") != "cs408-d-supplement-turn-v1" or not result.get("package_verified") or not result.get("capture_verified"):
        raise ValueError("D_learning_verified_result_required")
    session = result.get("capture", {}).get("session_id")
    context_session = result.get("next_input_context", {}).get("session_id")
    if context_session and context_session != session:
        raise ValueError("D_learning_image_handoff_session_drift")
    native.assert_session_open(repo, session)
    shown = result.get("next_display")
    if shown and native.read_receipt(shown["receipt_path"])["receipt_sha256"] != shown["receipt_sha256"]:
        raise ValueError("D_learning_next_display_drift")
    return {**result, "replayed": True, "celebration_eligible": False}


def close_session(body, repo=REPO):
    """Administrative stop: preserve saved work and exposure, leave remaining items unlearned."""
    repo = Path(repo).resolve()
    if body.get("reason") != "user_requested_stop" or not str(body.get("authorization_text", "")).strip():
        raise ValueError("D_learning_close_requires_explicit_user_request")
    with native.session_lock(repo, body["session_id"]):
        old = native.read_session_closure(repo, body["session_id"])
        if old is not None:
            if (old["handoff_sha256"] != body["handoff_sha256"]
                    or old["current_display_sha256"] != body["display_receipt_sha256"]):
                raise ValueError("D_learning_close_request_conflict")
            return dict(session_status(body, repo), replayed=True, administrative_write_count=0)
        if hashlib.sha256(Path(body["handoff"]).read_bytes()).hexdigest() != body["handoff_sha256"]:
            raise ValueError("D_learning_handoff_hash_drift")
        display, handoff, plan_path, ids, index = _bound(body, repo)
        if handoff.get("learning_mode") != "ordered_native_D":
            raise ValueError("D_learning_ordered_session_required")
        current_root = native.writer.private_root(repo) / "d-learning" / native.writer.digest(
            [body["session_id"], display["receipt_sha256"]])
        if (current_root / "request.json").exists() or (current_root / "result.json").exists():
            raise ValueError("D_learning_close_frontier_changed_or_pending_save")
        completed = []
        previous_next = None
        progress_root = native.episode_capture._operation_root(repo) / "d-supplement-progress"
        for request_id in ids[:index]:
            path = progress_root / (native.writer.digest({"session_id": body["session_id"], "request_ids": [request_id]}) + ".json")
            raw = path.read_bytes()
            progress = json.loads(raw)
            rows = progress.get("episode_receipts", [])
            if progress.get("session_id") != body["session_id"] or len(rows) != 1 or rows[0]["request_id"] != request_id:
                raise ValueError("D_learning_close_saved_progress_drift")
            verified = native.episode_capture.verify_episode_capture(repo, rows[0]["receipt_ref"])
            if (not verified.get("capture_verified") or not verified.get("package_verified")
                    or any(verified.get(k) != rows[0].get(k) for k in ("capture_id", "capture_payload_sha256", "episode_id", "receipt_sha256"))):
                raise ValueError("D_learning_close_saved_capture_drift")
            package = native.episode_capture.packages.verify_package_binding(
                verified["conversation_package_locator"], expected_canonical_sha256=verified["conversation_package_sha256"],
                require_sealed=True, private_root=native.episode_capture._default_package_root(repo))
            source = package["source"]["source"]
            prior = source["display_binding"]
            saved_root = native.writer.private_root(repo) / "d-learning" / native.writer.digest(
                [body["session_id"], prior["receipt_sha256"]])
            request_path, result_path = saved_root / "request.json", saved_root / "result.json"
            request, result = _read(request_path), _read(result_path)
            original = request["input"]
            next_context = result.get("next_input_context", {})
            if (source["authorization"]["session_id"] != body["session_id"]
                    or prior["request_id"] != request_id or original["session_id"] != body["session_id"]
                    or Path(original["handoff"]).resolve() != Path(body["handoff"]).resolve()
                    or original["display_receipt_sha256"] != prior["receipt_sha256"]
                    or native.read_receipt(original["display_receipt"]) != prior
                    or prior.get("native_import_receipt_sha256") != handoff["native_import_sha256"]
                    or result.get("capture_id") != rows[0]["capture_id"]
                    or result.get("next_action") != "present_next"
                    or result.get("capture", {}).get("episode_receipts") != rows
                    or not result.get("capture_verified") or not result.get("package_verified")
                    or next_context.get("session_id") != body["session_id"]
                    or result.get("next_display", {}).get("request_id") != ids[len(completed) + 1]
                    or (previous_next is not None and previous_next["display_receipt_sha256"] != prior["receipt_sha256"])):
                raise ValueError("D_learning_close_saved_frontier_binding_drift")
            previous_next = next_context
            completed.append(dict(rows[0], progress_receipt_path=str(path), progress_receipt_sha256=hashlib.sha256(raw).hexdigest(),
                result_path=str(result_path), result_sha256=hashlib.sha256(result_path.read_bytes()).hexdigest()))
        if previous_next is not None and (previous_next["display_receipt_sha256"] != display["receipt_sha256"]
                or Path(previous_next["display_receipt"]).resolve() != Path(body["display_receipt"]).resolve()):
            raise ValueError("D_learning_close_frontier_changed_or_pending_save")
        remaining = ids[index:]
        if any((progress_root / (native.writer.digest({"session_id": body["session_id"], "request_ids": [rid]}) + ".json")).exists()
               for rid in remaining):
            raise ValueError("D_learning_close_noncontiguous_saved_work")
        receipt = dict(schema=native.SESSION_CLOSURE_SCHEMA, status="closed_by_user", subject="408",
            session_id=body["session_id"], reason=body["reason"], authorization_text=body["authorization_text"],
            closed_at=native.dt.datetime.now(native.dt.timezone.utc).isoformat(),
            handoff=str(Path(body["handoff"]).resolve()), handoff_sha256=body["handoff_sha256"],
            native_import_sha256=handoff["native_import_sha256"], preparation_date=display.get("preparation_date", _read(plan_path)["target_date"]),
            last_display_date=display["review_date"], current_display_path=body["display_receipt"],
            current_display_sha256=display["receipt_sha256"], total=len(ids), saved_count=len(completed),
            completed_episodes=completed, remaining_count=len(remaining), remaining_request_ids=remaining,
            remaining_learning_status="not_completed", current_exposure_preserved=True,
            supplemental_complete=False, mastery_claimed=False, learning_write_count=0,
            formal_write_count=0, fsrs_write_count=0, personal_summary_write_count=0)
        receipt["receipt_sha256"] = native.writer.digest(receipt)
        native.episode_capture._atomic_write(native.session_closure_path(repo, body["session_id"]), native.writer.encoded(receipt))
        return dict(session_status(body, repo), replayed=False, administrative_write_count=1)


def _context_transport(result):
    """Use a temporary transport file when complete JSON exceeds one tool chunk."""
    encoded = json.dumps(result, ensure_ascii=False, indent=1)
    raw = encoded.encode("utf-8")
    if len(raw) <= 16000:
        return result
    with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", prefix="cs408-context-",
                                     suffix=".json", delete=False) as handle:
        handle.write(encoded)
    return dict(transport="cs408-context-file-v1", path=handle.name, characters=len(encoded),
                bytes=len(raw), sha256=hashlib.sha256(raw).hexdigest())


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--operation", choices=["start", "turn", "context", "visual-feedback", "correct-outcome", "supplement-evidence", "image-handoff", "close-session", "session-status"], default="turn")
    parser.add_argument("--input-json", required=True)
    parser.add_argument("--context-transport", action="store_true")
    args = parser.parse_args()
    try:
        body = json.load(sys.stdin) if args.input_json == "-" else _read(args.input_json)
        if args.operation == "image-handoff":
            result = image_handoff(body)
        elif args.operation == "start":
            result = start(body)
        elif args.operation == "context":
            result = context(body)
        elif args.operation == "visual-feedback":
            result = visual_feedback(body)
        elif args.operation == "correct-outcome":
            from d_episode_correction_408 import correct_outcome
            result = correct_outcome(body, REPO)
        elif args.operation == "supplement-evidence":
            from d_episode_evidence_408 import supplement_evidence
            result = supplement_evidence(body, REPO)
        elif args.operation == "close-session":
            result = close_session(body)
        elif args.operation == "session-status":
            result = session_status(body)
        else:
            result = submit(body["input"] if "input" in body and "arguments" in body else body)
    except (ValueError, OSError, KeyError, RuntimeError) as exc:
        result = dict(status="blocked", next_action="recover_same_request", reason=str(exc))
        if str(exc) == "D_learning_session_closed_by_user":
            result.update(status="session_closed", next_action="stop")
        if "observed_outcome" in str(exc):
            result.update(next_action="correct_invalid_input_before_save", allowed_outcome_values={
                "first_result": sorted(native.episode_capture.FIRST_RESULTS),
                "terminal_state": sorted(native.episode_capture.TERMINAL_STATES),
                "prompt_dependency": sorted(native.episode_capture.PROMPT_DEPENDENCIES),
                "reasoning_result": sorted(native.episode_capture.REASONING_RESULTS)})
        if getattr(exc, "diagnostic", None):
            result.update(next_action="check_current_message", diagnostic=exc.diagnostic)
        if result.get("status") != "session_closed" and args.operation == "turn" and isinstance(locals().get("body"), dict):
            original = body.get("input", body)
            if original.get("session_id") and original.get("display_receipt_sha256"):
                key = native.writer.digest([original["session_id"], original["display_receipt_sha256"]])
                request_file = native.writer.private_root(REPO) / "d-learning" / key / "request.json"
                if request_file.is_file():
                    result["learning_request_file"] = str(request_file)
                    result["next_action"] = "recover_same_request"
    result = terminal_delivery(result, restored=args.operation == "start")
    delivered = _context_transport(result) if args.operation == "context" and args.context_transport else result
    print(json.dumps(delivered, ensure_ascii=False, indent=1))
    return 2 if result.get("status") == "blocked" else 0


if __name__ == "__main__":
    sys.exit(main())
