#!/usr/bin/env python3
"""Bound D learning: transcript-backed Capture, native successor, compact delivery."""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import sys

import d_concept_supplement_408 as native
import exam_tutoring_context_408 as tutoring
from study_message_source_408 import resolve_display_episode

SCHEMA = "cs408-d-supplement-learning-v1"
REPO = native.bridge.REPO


def _read(path):
    return json.loads(Path(path).read_bytes())


def _bound(body, repo):
    path = Path(body["display_receipt"]).resolve()
    path.relative_to(native.receipt_root(repo).resolve())
    display = native.read_receipt(path)
    if display["receipt_sha256"] != body["display_receipt_sha256"]:
        raise ValueError("D_learning_current_display_changed")
    handoff = _read(body["handoff"])
    plan_path = Path(handoff["native_import_receipt"])
    raw = plan_path.read_bytes()
    if hashlib.sha256(raw).hexdigest() != handoff["native_import_sha256"]:
        raise ValueError("D_learning_import_hash_drift")
    plan = json.loads(raw)
    items = plan.get("learning_items", plan["supplemental_items"])
    ids = [item["request_id"] for item in items]
    if len(set(ids)) != len(ids) or ids != handoff.get("learning_request_ids", handoff["supplemental_request_ids"]):
        raise ValueError("D_learning_order_binding_drift")
    index = ids.index(display["request_id"])
    selected = items[index]["selection"]
    if any(selected[key] != display[key] for key in ("true_item_id", "surface_sha256")):
        raise ValueError("D_learning_surface_binding_drift")
    progress_key = native.writer.digest({"session_id": body["session_id"],
                                        "request_ids": [display["request_id"]]})
    progress = native.episode_capture._operation_root(repo) / "d-supplement-progress" / (progress_key + ".json")
    if progress.exists():
        raise ValueError("D_learning_display_already_captured_use_latest_result")
    return display, handoff, plan_path, ids, index


def _input_context(body, display):
    return dict(schema=SCHEMA, handoff=body["handoff"], session_id=body["session_id"],
                display_receipt=display["receipt_path"], display_receipt_sha256=display["receipt_sha256"])


def _materials(handoff, display):
    raw = Path(handoff["teaching_index_path"]).read_bytes()
    if hashlib.sha256(raw).hexdigest() != handoff["teaching_index_sha256"]:
        raise ValueError("D_learning_teaching_index_drift")
    entry = json.loads(raw).get(display["request_id"], {})
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
            if path.is_symlink() or path.stat().st_size > 65536:
                raise ValueError("D_grading_reference_invalid_file")
            raw = path.read_bytes()
            if hashlib.sha256(raw).hexdigest() != ref["sha256"]:
                raise ValueError("D_grading_reference_hash_drift")
            value = json.loads(raw)
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


def context(body, repo=REPO):
    display, handoff, _, ids, index = _bound(body, repo)
    shown = native.delivery(display, body["display_receipt"])
    view = body.get("view", "protected")
    query = dict(schema=tutoring.QUERY_INPUT_SCHEMA,
                 stage="pre_answer" if view == "protected" else "post_answer",
                 knowledge_points=[display["concept_id"]], weak_knowledge_points=[],
                 max_profiles=0, max_bytes=4500, read_stats=False)
    if body.get("teaching_mechanisms"):
        query["mechanism_terms"] = body["teaching_mechanisms"]
    personal = tutoring.query_context(repo, query, max_bytes=4500, teaching_view=view,
                                      known_version=body.get("known_version"))
    refs = _materials(handoff, display)
    return dict(status="ready_for_teaching", next_action="continue_current_item",
                input_context=_input_context(body, shown), current_display=shown,
                current_image_paths=[asset["path"] for asset in shown.get("question_assets", [])]
                    if view == "protected" else [],
                progress=dict(position=index+1, total=len(ids)), teaching_context=personal,
                prepared_material_refs=refs,
                prepared_context=_prepared_context(refs, display, view),
                grading_reference=_grading_reference(display, view),
                image_delivery="prepared_markdown", image_inspection_required=False, write_count=0)


def submit(body, repo=REPO):
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
            display, handoff, plan_path, ids, index = _bound(body, repo)
            rows, refs = resolve_display_episode(display, body["expected_user_text"])
            if any(ref["has_images"] for ref in refs) and not body.get("attachments"):
                raise ValueError("D_learning_message_images_require_original_attachments")
            outcome = body["observed_outcome"]
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
                    turns=rows, attachments=body.get("attachments", []),
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
        result = native.turn(**saved["arguments"])
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
                result["next_display"]["prepared_material_refs"] = _materials(_read(body["handoff"]), shown)
            except (ValueError, OSError, KeyError) as exc:
                result["prepared_materials_unavailable"] = str(exc)
        native.episode_capture._atomic_write(result_file, native.writer.encoded(result))
        return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--operation", choices=["turn", "context", "image-handoff"], default="turn")
    parser.add_argument("--input-json", required=True)
    args = parser.parse_args()
    try:
        body = json.load(sys.stdin) if args.input_json == "-" else _read(args.input_json)
        if args.operation == "image-handoff":
            result = body
            if result.get("schema") != "cs408-d-supplement-turn-v1" or not result.get("package_verified") or not result.get("capture_verified"):
                raise ValueError("D_learning_verified_result_required")
            shown = result.get("next_display")
            if shown and native.read_receipt(shown["receipt_path"])["receipt_sha256"] != shown["receipt_sha256"]:
                raise ValueError("D_learning_next_display_drift")
            result = {**result, "replayed": True, "celebration_eligible": False}
        elif args.operation == "context":
            result = context(body)
        else:
            result = submit(body["input"] if "input" in body and "arguments" in body else body)
    except (ValueError, OSError, KeyError, RuntimeError) as exc:
        result = dict(status="blocked", next_action="recover_same_request", reason=str(exc))
        if getattr(exc, "diagnostic", None):
            result.update(next_action="check_current_message", diagnostic=exc.diagnostic)
        if args.operation == "turn" and isinstance(locals().get("body"), dict):
            original = body.get("input", body)
            if original.get("session_id") and original.get("display_receipt_sha256"):
                key = native.writer.digest([original["session_id"], original["display_receipt_sha256"]])
                request_file = native.writer.private_root(REPO) / "d-learning" / key / "request.json"
                if request_file.is_file():
                    result["learning_request_file"] = str(request_file)
    print(json.dumps(result, ensure_ascii=False, indent=1))
    return 2 if result.get("status") == "blocked" else 0


if __name__ == "__main__":
    sys.exit(main())
