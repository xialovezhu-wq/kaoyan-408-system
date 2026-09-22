#!/usr/bin/env python3
"""Reveal one compiled review-unit feedback object after durable first-answer proof.

Pre-answer queries cannot resolve the post-answer namespace.  This adapter first
verifies the canonical review outcome receipt and, for morning review, the
session binding receipt.  Only then does it read the separate feedback object.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import review_hot_projection_408 as formal_hot
import review_hot_state_408 as review_hot


SCHEMA = "postanswer_feedback_gate_v1"
DEFAULT_MAX_BYTES = 12_288


class PostanswerFeedbackError(RuntimeError):
    """A receipt, identity, or post-answer artifact failed closed."""


def _review_event(
    repo: Path,
    *,
    event_id: str,
    receipt_sha256: str,
    review_state_dir: str | Path | None,
    review_unit_id: str,
) -> tuple[dict[str, Any], dict[str, Any]]:
    try:
        verified = review_hot.verify_commit(
            repo,
            receipt_sha256=receipt_sha256,
            state_dir=review_state_dir,
        )
        found = review_hot.lookup_event(
            repo, event_id=event_id, state_dir=review_state_dir
        )
    except review_hot.HotStateError as exc:
        raise PostanswerFeedbackError("review_commit_proof_failed") from exc
    event = found.get("event") if found.get("status") == "found" else None
    if (
        verified.get("status") != "verified"
        or verified.get("event_id") != event_id
        or not isinstance(event, dict)
        or event.get("schema") != "review_event_v1"
        or event.get("event_kind") != "outcome"
        or event.get("event_id") != event_id
        or event.get("review_unit_id") != review_unit_id
        or event.get("formal_write_authorized") is not False
    ):
        raise PostanswerFeedbackError("review_commit_proof_mismatch")
    return event, verified


def _morning_binding(
    event: dict[str, Any],
    *,
    session_dir: str | Path | None,
    receipt_sha256: str | None,
    hot_dir: str | Path | None,
) -> dict[str, Any] | None:
    source = event.get("source")
    if session_dir is not None and source != "morning_review":
        raise PostanswerFeedbackError("morning_source_identity_mismatch")
    if source != "morning_review":
        return None
    if session_dir is None or not receipt_sha256:
        raise PostanswerFeedbackError("morning_session_binding_proof_required")
    # Dynamic import avoids a cycle when morning_review_session imports this
    # adapter at command execution time.
    try:
        import morning_session_hot_state_408 as session_hot

        session = Path(session_dir).expanduser().resolve()
        if session.name != event.get("session_id"):
            raise PostanswerFeedbackError("morning_session_identity_mismatch")
        verified = session_hot.verify_commit(
            session, receipt_sha256, hot_dir=hot_dir
        )
        if verified.get("status") != "pass":
            raise PostanswerFeedbackError("morning_session_receipt_invalid")
        found = session_hot.lookup_session_event(
            session, str(verified.get("event_id") or ""), hot_dir=hot_dir
        )
    except PostanswerFeedbackError:
        raise
    except Exception as exc:
        raise PostanswerFeedbackError("morning_session_binding_proof_failed") from exc
    binding = found.get("event") if isinstance(found, dict) else None
    payload = binding.get("payload") if isinstance(binding, dict) else None
    if (
        not isinstance(binding, dict)
        or binding.get("event_type") != "first_bound_to_loop"
        or not isinstance(payload, dict)
        or payload.get("canonical_event_id") != event.get("event_id")
        or payload.get("item_id") != event.get("item_id")
    ):
        raise PostanswerFeedbackError("morning_session_binding_proof_mismatch")
    return verified


def _feedback_object(
    projection_dir: Path, review_unit_id: str
) -> tuple[dict[str, Any], dict[str, Any]]:
    stats = {
        "file_read_attempt_count": 0,
        "files_read": 0,
        "bytes_read": 0,
        "paths_read": [],
        "formal_table_reads": 0,
        "ledger_reads": 0,
    }
    try:
        pointer = formal_hot._read_json(
            projection_dir / "manifest.json",
            expected_sha256=None,
            stats=stats,
            max_bytes=formal_hot.MAX_POINTER_INPUT_BYTES,
        )
        manifest_ref = formal_hot._validate_ref(
            pointer.get("manifest_ref"),
            "manifests",
            pointer.get("manifest_sha256"),
        )
        manifest = formal_hot._read_json(
            projection_dir / manifest_ref,
            expected_sha256=pointer["manifest_sha256"],
            stats=stats,
            max_bytes=formal_hot.MAX_MANIFEST_INPUT_BYTES,
        )
        index_ref = formal_hot._validate_ref(
            manifest.get("index_ref"), "indexes", manifest.get("index_sha256")
        )
        index = formal_hot._read_json(
            projection_dir / index_ref,
            expected_sha256=manifest["index_sha256"],
            stats=stats,
            max_bytes=formal_hot.MAX_INDEX_INPUT_BYTES,
        )
        formal_hot._validate_projection_chain(pointer, manifest, index)
        entry = (index.get("postanswer_by_review_unit_id") or {}).get(
            review_unit_id
        )
        if not isinstance(entry, dict):
            raise PostanswerFeedbackError("postanswer_feedback_identity_not_found")
        object_ref = formal_hot._validate_ref(
            entry.get("object_ref"), "objects", entry.get("object_sha256")
        )
        obj = formal_hot._read_json(
            projection_dir / object_ref,
            expected_sha256=entry["object_sha256"],
            stats=stats,
            max_bytes=formal_hot.MAX_OBJECT_BYTES,
        )
        formal_hot._validate_object_binding(
            obj,
            manifest=manifest,
            expected_kind="review_hot_postanswer_feedback_object",
            identity_field="review_unit_id",
            identity_value=review_unit_id,
        )
    except formal_hot.ProjectionError as exc:
        raise PostanswerFeedbackError("postanswer_projection_hash_failed") from exc
    feedback = obj.get("feedback")
    if (
        obj.get("exposure") != "postanswer_only_after_verified_first_answer"
        or obj.get("answer_safe_question_surface") is not True
        or not isinstance(feedback, str)
        or not feedback.strip()
        or formal_hot.POSTANSWER_PROTECTED_RE.search(feedback)
    ):
        raise PostanswerFeedbackError("postanswer_feedback_object_invalid")
    return obj, stats


def reveal_feedback(
    repo: str | Path,
    *,
    review_unit_id: str,
    canonical_event_id: str,
    review_receipt_sha256: str,
    review_state_dir: str | Path | None = None,
    formal_projection_dir: str | Path | None = None,
    session_dir: str | Path | None = None,
    session_binding_receipt_sha256: str | None = None,
    session_hot_dir: str | Path | None = None,
    max_bytes: int = DEFAULT_MAX_BYTES,
) -> dict[str, Any]:
    if not formal_hot.RU_ID_RE.fullmatch(str(review_unit_id or "")):
        raise PostanswerFeedbackError("invalid_review_unit_id")
    if not isinstance(max_bytes, int) or not 1_024 <= max_bytes <= 65_536:
        raise PostanswerFeedbackError("invalid_feedback_byte_budget")
    root = Path(repo).expanduser().resolve()
    event, review_verification = _review_event(
        root,
        event_id=canonical_event_id,
        receipt_sha256=review_receipt_sha256,
        review_state_dir=review_state_dir,
        review_unit_id=review_unit_id,
    )
    session_verification = _morning_binding(
        event,
        session_dir=session_dir,
        receipt_sha256=session_binding_receipt_sha256,
        hot_dir=session_hot_dir,
    )
    projection = (
        Path(formal_projection_dir).expanduser().resolve()
        if formal_projection_dir is not None
        else (root / formal_hot.DEFAULT_OUTPUT_RELATIVE).resolve()
    )
    obj, stats = _feedback_object(projection, review_unit_id)
    result = {
        "schema": SCHEMA,
        "status": "ready",
        "review_unit_id": review_unit_id,
        "canonical_event_id": canonical_event_id,
        "feedback": obj["feedback"],
        "feedback_source": "receipt_gated_review_hot_projection",
        "review_commit_verified": review_verification["status"],
        "session_binding_verified": (
            session_verification.get("status")
            if session_verification is not None
            else "not_applicable"
        ),
        "projection_scan_stats": stats,
        "formal_write_count": 0,
        "byte_budget": max_bytes,
        "byte_count": 0,
    }
    for _ in range(6):
        size = len(
            (json.dumps(result, ensure_ascii=False, sort_keys=True) + "\n").encode(
                "utf-8"
            )
        )
        if result.get("byte_count") == size:
            break
        result["byte_count"] = size
    if len(
        (json.dumps(result, ensure_ascii=False, sort_keys=True) + "\n").encode(
            "utf-8"
        )
    ) > max_bytes:
        raise PostanswerFeedbackError("postanswer_feedback_result_exceeds_budget")
    return result


def safe_reveal_feedback(*args: Any, **kwargs: Any) -> dict[str, Any]:
    try:
        return reveal_feedback(*args, **kwargs)
    except PostanswerFeedbackError as exc:
        return {
            "schema": SCHEMA,
            "status": "unavailable",
            "reason_code": str(exc),
            "formal_write_count": 0,
        }
