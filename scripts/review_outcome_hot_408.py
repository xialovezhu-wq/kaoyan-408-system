#!/usr/bin/env python3
"""Construct and commit one canonical 408 outcome on the bounded hot path.

The legacy :func:`review_feedback_loop.record_outcome` remains the cold/recovery
implementation.  This module preserves its event semantics while replacing its
full-ledger reducer and formal Markdown scans with:

* one exact ``review_hot_state_408.begin_view``;
* on idempotent replay only, one exact-range ``lookup_event``;
* at most one hash-bound review-unit projection object; and
* one append/fsync + SQLite transaction with an exact commit receipt.

It never updates the legacy ``state.json`` or dashboard projections.  Those are
cold, rebuildable views and are deliberately outside a single-question turn.
"""

from __future__ import annotations

from study_commit_followup_408 import study_commit_boundary

import datetime as dt
import hashlib
import re
from pathlib import Path
from typing import Any

import review_feedback_loop as canonical
import review_hot_projection_408 as formal_hot
import review_hot_state_408 as state_hot


SCHEMA = "review_outcome_hot_v1"
CAMPAIGN_SCHEMA = "campaign_review_outcome_hot_v1"
CAMPAIGN_SCHEDULER_RECEIPT_SCHEMA = "campaign_scheduler_outcome_receipt_v1"
CAMPAIGN_RESPONSE_MODES = {"choice_only", "choice_plus_reason"}
CAMPAIGN_RATINGS = {"Again", "Hard", "Good"}


class HotOutcomeError(RuntimeError):
    """Stable fail-closed error for one managed outcome transaction."""


def _campaign_scheduler_receipt(
    event: dict[str, Any],
    *,
    event_sha256: str,
    commit_receipt_sha256: str,
    campaign_rating: str,
) -> dict[str, Any]:
    core = {
        "schema": CAMPAIGN_SCHEDULER_RECEIPT_SCHEMA,
        "campaign_run_id": event["campaign_run_id"],
        "campaign_segment_id": event["campaign_segment_id"],
        "item_id": event["item_id"],
        "formal_node_id": event["formal_node_id"],
        "campaign_item_commitment_sha256": event[
            "campaign_item_commitment_sha256"
        ],
        "canonical_event_id": event["event_id"],
        "canonical_event_sha256": event_sha256,
        "canonical_commit_receipt_sha256": commit_receipt_sha256,
        "campaign_rating": campaign_rating,
        "formal_write_count": 0,
    }
    return {
        **core,
        "receipt_sha256": hashlib.sha256(
            canonical.canonical_json(core).encode("utf-8")
        ).hexdigest(),
    }


def _campaign_first_result(
    *,
    response_mode: str,
    screened_correct: bool,
    reason_audited: bool,
    recognized_original: bool,
    variant_id: str | None,
    choice_result: str,
    reasoning_result: str,
    confidence: str,
    prompt_level: str,
    campaign_rating: str,
    capture_required: bool,
) -> str:
    """Validate one rapid-review classification and return its canonical result.

    Campaign ``Hard`` is intentionally not equivalent to independent mastery.
    An unaudited correct choice is persisted as canonical ``partial`` plus the
    explicit ``screened_correct`` field.  Only an audited, sound answer may be
    represented as ``independent_correct``.
    """

    if response_mode not in CAMPAIGN_RESPONSE_MODES:
        raise HotOutcomeError("campaign_response_mode_invalid")
    if campaign_rating not in CAMPAIGN_RATINGS:
        raise HotOutcomeError("campaign_rating_invalid")
    for value, label in (
        (screened_correct, "screened_correct"),
        (reason_audited, "reason_audited"),
        (recognized_original, "recognized_original"),
        (capture_required, "capture_required"),
    ):
        if not isinstance(value, bool):
            raise HotOutcomeError(f"campaign_{label}_must_be_boolean")
    if choice_result not in canonical.CHOICE_RESULTS - {"not_applicable"}:
        raise HotOutcomeError("campaign_choice_result_invalid")
    if reasoning_result not in canonical.REASONING_RESULTS:
        raise HotOutcomeError("campaign_reasoning_result_invalid")
    if confidence not in canonical.CONFIDENCE_LEVELS:
        raise HotOutcomeError("campaign_confidence_invalid")
    if prompt_level not in canonical.PROMPT_LEVELS:
        raise HotOutcomeError("campaign_prompt_level_invalid")
    if (response_mode == "choice_plus_reason") is not reason_audited:
        raise HotOutcomeError("campaign_response_mode_reason_audit_drifted")
    if reason_audited and reasoning_result == "not_observed":
        raise HotOutcomeError("campaign_reason_audit_requires_observed_reasoning")
    if recognized_original and variant_id is not None:
        raise HotOutcomeError("campaign_recognized_original_requires_original_reason_audit")
    if (
        recognized_original and choice_result == "correct" and not reason_audited
        and prompt_level == "none" and reasoning_result not in {"partial", "diverged"}
    ):
        raise HotOutcomeError("campaign_recognized_original_requires_original_reason_audit")

    if screened_correct:
        if (
            response_mode != "choice_only"
            or reason_audited
            or recognized_original
            or choice_result != "correct"
            or reasoning_result != "not_observed"
            or confidence == "low"
            or prompt_level != "none"
            or campaign_rating != "Hard"
            or capture_required
        ):
            raise HotOutcomeError("campaign_screened_correct_contract_invalid")
        return "partial"

    if campaign_rating == "Good":
        if (
            response_mode != "choice_plus_reason"
            or not reason_audited
            or choice_result != "correct"
            or reasoning_result != "sound"
            or confidence not in {"medium", "high"}
            or prompt_level != "none"
            or capture_required
        ):
            raise HotOutcomeError("campaign_good_contract_invalid")
        return "independent_correct"

    if campaign_rating == "Hard":
        if (
            response_mode != "choice_plus_reason"
            or not reason_audited
            or choice_result != "correct"
            or reasoning_result != "sound"
            or confidence != "low"
            or prompt_level != "none"
            or capture_required
        ):
            raise HotOutcomeError("campaign_audited_hard_contract_invalid")
        return "independent_correct"

    failure = (
        choice_result != "correct"
        or reasoning_result in {"partial", "diverged"}
        or prompt_level != "none"
    )
    if not failure or not capture_required:
        raise HotOutcomeError("campaign_again_contract_invalid")
    if choice_result == "incorrect":
        return "wrong"
    if choice_result == "uncertain":
        return "uncertain"
    return "partial"


def _request_hash_compatible(
    existing: dict[str, Any],
    request_payload: dict[str, Any],
    *,
    first_break_provenance: str,
    choice_result: str,
    reasoning_result: str,
    presentation: str | None,
) -> bool:
    request_hash = hashlib.sha256(
        canonical.canonical_json(request_payload).encode("utf-8")
    ).hexdigest()
    if existing.get("request_hash") == request_hash:
        return True
    legacy_payload = dict(request_payload)
    inferred = canonical.normalize_first_break_provenance(
        None, existing.get("first_break")
    )
    if "first_break_provenance" not in existing:
        if first_break_provenance != inferred:
            return False
        legacy_payload.pop("first_break_provenance", None)
    if "choice_result" not in existing and choice_result == "not_applicable":
        legacy_payload.pop("choice_result", None)
    if "reasoning_result" not in existing and reasoning_result == "not_observed":
        legacy_payload.pop("reasoning_result", None)
    compatible_payloads = [legacy_payload]
    session_review_date = request_payload.get("session_review_date")
    if (
        existing.get("source") == "morning_review"
        and session_review_date is not None
        and "session_review_date" not in existing
        and existing.get("observed_date") == session_review_date
        and canonical._event_local_date(str(existing.get("event_time") or ""))
        == canonical._parse_date(
            str(request_payload.get("observed_date") or ""), "observed_date"
        )
    ):
        historical = dict(legacy_payload)
        historical.pop("session_review_date", None)
        historical["observed_date"] = session_review_date
        compatible_payloads.append(historical)
    hashes: set[str] = set()
    for compatible in compatible_payloads:
        hashes.add(
            hashlib.sha256(
                canonical.canonical_json(compatible).encode("utf-8")
            ).hexdigest()
        )
        if existing.get("presentation_kind") == presentation:
            no_presentation = dict(compatible)
            no_presentation.pop("presentation_kind", None)
            hashes.add(
                hashlib.sha256(
                    canonical.canonical_json(no_presentation).encode("utf-8")
                ).hexdigest()
            )
    return existing.get("request_hash") in hashes


def _load_existing_event_once(
    repo: Path,
    event_ref: dict[str, Any],
    *,
    state_dir: str | Path | None,
) -> tuple[dict[str, Any], dict[str, Any]]:
    event_id = str(event_ref.get("event_id") or "")
    if event_ref.get("schema") != state_hot.EVENT_REF_SCHEMA or not event_id:
        raise HotOutcomeError("existing_event_ref_invalid")
    try:
        found = state_hot.lookup_event(
            repo,
            event_id=event_id,
            state_dir=state_dir,
        )
    except state_hot.HotStateError as exc:
        raise HotOutcomeError("existing_event_exact_lookup_failed") from exc
    event = found.get("event")
    if found.get("status") != "found" or not isinstance(event, dict):
        raise HotOutcomeError("existing_event_exact_lookup_failed")
    expected = {
        "event_id": event.get("event_id"),
        "idempotency_key": event.get("idempotency_key"),
        "event_kind": event.get("event_kind"),
        "source": event.get("source"),
        "session_id": event.get("session_id"),
        "item_id": event.get("item_id"),
        "identity": canonical._identity(event),
        "observed_date": event.get("observed_date"),
        "event_sha256": found.get("event_sha256"),
        "line_sha256": found.get("line_sha256"),
        "byte_offset": found.get("byte_offset"),
        "byte_length": found.get("byte_length"),
    }
    if any(event_ref.get(key) != value for key, value in expected.items()):
        raise HotOutcomeError("existing_event_ref_exact_lookup_mismatch")
    evidence = {
        "status": "found",
        "event_id": event_id,
        "event_sha256": found["event_sha256"],
        "byte_offset": found["byte_offset"],
        "byte_length": found["byte_length"],
        "scan_stats": found.get("scan_stats"),
    }
    return event, evidence


def _prepare(
    *,
    source: str,
    session_id: str,
    item_id: str,
    source_id: str,
    mechanism_key: str,
    knowledge_point: str,
    first_result: str,
    choice_result: str,
    reasoning_result: str,
    confidence: str,
    prompt_level: str,
    first_break: str,
    event_time: str,
    observed_date: str,
    session_review_date: str | None,
    idempotency_key: str,
    correction_result: str,
    response_seconds: float | None,
    latency_state: str,
    cross_day: bool | None,
    question_valid: bool,
    formal_node_id: str | None,
    review_unit_id: str | None,
    trigger_action_id: str | None,
    legacy_import: bool,
    fragile_override: bool,
    first_break_provenance: str,
    practice_mode: str | None,
    anchor_formal_node_id: str | None,
    variant_of_formal_node_id: str | None,
    followup_event_id: str | None,
    parent_trigger_event_id: str | None,
    context_slice_id: str | None,
    candidate_id: str | None,
    source_ref: str | None,
    source_sha256: str | None,
    selection_receipt_ref: str | None,
    selection_receipt_sha256: str | None,
    presentation_kind: str | None,
    managed_user_reply_binding_sha256: str | None,
    from_session_sync: bool,
) -> dict[str, Any]:
    if source not in canonical.SOURCES:
        raise HotOutcomeError(f"invalid source: {source}")
    if (
        first_result not in canonical.FIRST_RESULTS
        or correction_result not in canonical.CORRECTION_RESULTS
    ):
        raise HotOutcomeError("invalid first/correction result")
    if (
        choice_result not in canonical.CHOICE_RESULTS
        or reasoning_result not in canonical.REASONING_RESULTS
    ):
        raise HotOutcomeError("invalid choice or reasoning result")
    if first_result == "independent_correct" and (
        choice_result in {"incorrect", "partial", "uncertain"}
        or reasoning_result in {"partial", "diverged"}
    ):
        raise HotOutcomeError(
            "independent_correct conflicts with choice or reasoning result"
        )
    if (
        confidence not in canonical.CONFIDENCE_LEVELS
        or prompt_level not in canonical.PROMPT_LEVELS
    ):
        raise HotOutcomeError("invalid confidence or prompt level")
    if first_break_provenance not in canonical.FIRST_BREAK_PROVENANCES:
        raise HotOutcomeError("invalid first_break provenance")
    if latency_state not in canonical.LATENCY_STATES or cross_day not in canonical.CROSS_DAY_STATES:
        raise HotOutcomeError("invalid latency or cross-day state")
    if first_result == "independent_correct" and prompt_level != "none":
        raise HotOutcomeError("independent_correct requires prompt_level=none")
    if response_seconds is not None and not 0 < response_seconds <= 7200:
        raise HotOutcomeError(
            "response_seconds must be > 0 and <= 7200; use null when unmeasured"
        )
    try:
        parsed_event_time = canonical._parse_datetime(event_time)
        requested_day = canonical._parse_date(observed_date, "observed_date")
        session = canonical._safe_id(session_id, "session_id", required=True)
        item = canonical._safe_id(item_id, "item_id", required=True)
        source_value = canonical._safe_text(
            source_id, "source_id", required=True, limit=512
        )
        mechanism = canonical._safe_text(
            mechanism_key, "mechanism_key", required=True
        )
        knowledge = canonical._safe_text(knowledge_point, "knowledge_point")
        break_value = canonical._safe_text(
            first_break, "first_break", required=True, limit=200
        )
        formal_node = canonical._safe_id(formal_node_id, "formal_node_id")
        review_unit = canonical._safe_id(review_unit_id, "review_unit_id")
        trigger_action = canonical._safe_id(
            trigger_action_id, "trigger_action_id"
        )
        presentation = canonical._safe_id(
            presentation_kind, "presentation_kind"
        )
        key = canonical._safe_id(
            idempotency_key, "idempotency_key", required=True
        )
    except canonical.ReviewLoopError as exc:
        raise HotOutcomeError(str(exc)) from exc
    assert session and item and source_value and mechanism and break_value and key
    local_day = parsed_event_time.astimezone(canonical.LOCAL_TZ).date()
    session_day: dt.date | None = None
    if source == "morning_review":
        session_day = canonical._parse_date(
            session_review_date or requested_day.isoformat(),
            "session_review_date",
        )
        day = local_day
    else:
        if session_review_date is not None:
            raise HotOutcomeError(
                "session_review_date is only valid for morning_review"
            )
        day = requested_day
    managed_reply_binding = None
    if managed_user_reply_binding_sha256 is not None:
        candidate = str(managed_user_reply_binding_sha256)
        if not re.fullmatch(r"[0-9a-f]{64}", candidate):
            raise HotOutcomeError(
                "managed_user_reply_binding_sha256 must be lowercase SHA-256"
            )
        managed_reply_binding = candidate
    if (
        source in {"daily_practice", "evening_d0"}
        and not legacy_import
        and local_day != day
    ):
        raise HotOutcomeError(
            "event_time Asia/Shanghai date must equal observed_date"
        )
    if formal_node and not canonical.FORMAL_NODE_RE.fullmatch(formal_node):
        raise HotOutcomeError("formal_node_id is invalid")
    if review_unit and not canonical.REVIEW_UNIT_RE.fullmatch(review_unit):
        raise HotOutcomeError("review_unit_id is invalid")
    if review_unit is None and canonical.REVIEW_UNIT_RE.fullmatch(source_value):
        review_unit = source_value
    if formal_node is None and canonical.FORMAL_NODE_RE.fullmatch(source_value):
        formal_node = source_value
    if source == "morning_review" and not from_session_sync:
        # The legacy check scans every session directory.  Managed morning
        # writes must instead arrive through the session transaction.
        raise HotOutcomeError("direct_morning_hot_write_requires_session_sync")

    followup_payload: dict[str, Any] | None = None
    if practice_mode is not None:
        if source != "morning_review" or not from_session_sync:
            raise HotOutcomeError(
                "linked practice must be synchronized from a morning session"
            )
        if practice_mode not in canonical.FOLLOWUP_PRACTICE_MODES:
            raise HotOutcomeError("invalid linked-practice mode")
        try:
            variant_origin = canonical._safe_id(
                variant_of_formal_node_id, "variant_of_formal_node_id"
            )
            anchor_formal = canonical._safe_id(
                anchor_formal_node_id, "anchor_formal_node_id"
            )
            followup_event = canonical._safe_id(
                followup_event_id, "followup_event_id", required=True
            )
            parent_trigger = canonical._safe_id(
                parent_trigger_event_id,
                "parent_trigger_event_id",
                required=True,
            )
            slice_id = canonical._safe_id(
                context_slice_id, "context_slice_id", required=True
            )
            candidate = canonical._safe_id(
                candidate_id, "candidate_id", required=True
            )
            source_reference = canonical._safe_text(
                source_ref, "source_ref", required=True, limit=1024
            )
            source_digest = canonical._safe_text(
                source_sha256, "source_sha256", required=True, limit=64
            )
            selection_reference = canonical._safe_text(
                selection_receipt_ref,
                "selection_receipt_ref",
                required=True,
                limit=1024,
            )
            selection_digest = canonical._safe_text(
                selection_receipt_sha256,
                "selection_receipt_sha256",
                required=True,
                limit=64,
            )
        except canonical.ReviewLoopError as exc:
            raise HotOutcomeError(str(exc)) from exc
        if anchor_formal and not canonical.FORMAL_NODE_RE.fullmatch(anchor_formal):
            raise HotOutcomeError("anchor_formal_node_id is invalid")
        if not re.fullmatch(r"[0-9a-f]{64}", str(source_digest)):
            raise HotOutcomeError("source_sha256 must be lowercase SHA-256")
        if not re.fullmatch(r"[0-9a-f]{64}", str(selection_digest)):
            raise HotOutcomeError(
                "selection_receipt_sha256 must be lowercase SHA-256"
            )
        if practice_mode == "original":
            if not formal_node or variant_origin:
                raise HotOutcomeError(
                    "original linked practice must bind one formal_node_id only"
                )
        elif formal_node or not variant_origin or not canonical.FORMAL_NODE_RE.fullmatch(
            variant_origin
        ):
            raise HotOutcomeError(
                "variant linked practice must not bind the original as formal_node_id"
            )
        if practice_mode == "variant" and anchor_formal != variant_origin:
            raise HotOutcomeError(
                "variant linked-practice anchor is inconsistent"
            )
        followup_payload = {
            "practice_mode": practice_mode,
            "anchor_formal_node_id": anchor_formal,
            "variant_of_formal_node_id": variant_origin,
            "followup_event_id": followup_event,
            "parent_trigger_event_id": parent_trigger,
            "context_slice_id": slice_id,
            "candidate_id": candidate,
            "source_ref": source_reference,
            "source_sha256": source_digest,
            "selection_receipt_ref": selection_reference,
            "selection_receipt_sha256": selection_digest,
        }

    request_payload = {
        "source": source,
        "session_id": session,
        "item_id": item,
        "source_id": source_value,
        "mechanism_key": mechanism,
        "knowledge_point": knowledge,
        "first_result": first_result,
        "choice_result": choice_result,
        "reasoning_result": reasoning_result,
        "confidence": confidence,
        "prompt_level": prompt_level,
        "first_break": break_value,
        "event_time": event_time,
        "observed_date": day.isoformat(),
        "correction_result": correction_result,
        "response_seconds": response_seconds,
        "latency_state": latency_state,
        "cross_day": cross_day,
        "question_valid": bool(question_valid),
        "formal_node_id": formal_node,
        "review_unit_id": review_unit,
        "trigger_action_id": trigger_action,
        "presentation_kind": presentation,
        "legacy_import": bool(legacy_import),
        "fragile_override": bool(fragile_override),
    }
    if session_day is not None:
        request_payload["session_review_date"] = session_day.isoformat()
    if first_break_provenance != "derived_classification":
        request_payload["first_break_provenance"] = first_break_provenance
    if followup_payload is not None:
        request_payload.update(followup_payload)
    if managed_reply_binding is not None:
        request_payload["managed_user_reply_binding_sha256"] = (
            managed_reply_binding
        )
    return {
        "source": source,
        "session_id": session,
        "item_id": item,
        "source_id": source_value,
        "mechanism_key": mechanism,
        "knowledge_point": knowledge,
        "first_result": first_result,
        "choice_result": choice_result,
        "reasoning_result": reasoning_result,
        "confidence": confidence,
        "prompt_level": prompt_level,
        "first_break": break_value,
        "first_break_provenance": first_break_provenance,
        "event_time": event_time,
        "day": day,
        "session_review_date": (
            session_day.isoformat() if session_day is not None else None
        ),
        "key": key,
        "correction_result": correction_result,
        "response_seconds": response_seconds,
        "latency_state": latency_state,
        "cross_day": cross_day,
        "question_valid": bool(question_valid),
        "formal_node": formal_node,
        "review_unit": review_unit,
        "trigger_action": trigger_action,
        "presentation": presentation,
        "legacy_import": bool(legacy_import),
        "fragile_override": bool(fragile_override),
        "practice_mode": practice_mode,
        "followup_payload": followup_payload,
        "request_payload": request_payload,
        "managed_user_reply_binding_sha256": managed_reply_binding,
        "request_hash": hashlib.sha256(
            canonical.canonical_json(request_payload).encode("utf-8")
        ).hexdigest(),
    }


def _formal_context(
    repo: Path,
    *,
    source_id: str,
    review_unit_id: str | None,
    formal_node_id: str | None,
    practice_mode: str | None,
    formal_projection_dir: str | Path | None,
) -> tuple[list[str], list[dict[str, Any]], dict[str, Any]]:
    if practice_mode == "variant":
        return [], [], {"status": "not_applicable", "files_read": 0}
    direct = (
        [source_id] if canonical.FORMAL_NODE_RE.fullmatch(source_id) else []
    )
    if not review_unit_id:
        mapped = sorted({*direct, *([formal_node_id] if formal_node_id else [])})
        return mapped, [], {"status": "not_needed", "files_read": 0}
    projection_dir = (
        Path(formal_projection_dir).expanduser().resolve()
        if formal_projection_dir is not None
        else (repo / formal_hot.DEFAULT_OUTPUT_RELATIVE).resolve()
    )
    result = formal_hot.safe_query_projection(
        projection_dir,
        review_unit_id=review_unit_id,
        max_bytes=formal_hot.DEFAULT_MAX_BYTES,
    )
    if result.get("status") != "ready":
        raise HotOutcomeError(
            "formal_hot_projection_unavailable:"
            + str(result.get("reason_code") or "unknown")
        )
    record = result.get("record") or {}
    mapped = direct or list(record.get("covered_formal_node_ids") or [])
    if formal_node_id:
        mapped = sorted({*mapped, formal_node_id})
    else:
        mapped = sorted(set(mapped))
    tracks = list(record.get("primary_original_tracks") or [])
    return mapped, tracks, {
        "status": "ready",
        "build_id": result.get("build_id"),
        "object_sha256": result.get("object_sha256"),
        "scan_stats": result.get("scan_stats"),
    }


def _original_actions_from_tracks(
    tracks: list[dict[str, Any]],
    *,
    event_id: str,
    observed_date: dt.date,
    review_unit_id: str | None,
    source_id: str,
    first_result: str,
    hint_used: bool,
    question_valid: bool,
    fragile: bool,
    current_formal_node_id: str | None = None,
) -> list[dict[str, Any]]:
    if (
        not review_unit_id
        or first_result != "independent_correct"
        or hint_used
        or not question_valid
        or fragile
    ):
        return []
    current_node = (
        current_formal_node_id
        if current_formal_node_id
        and canonical.FORMAL_NODE_RE.fullmatch(current_formal_node_id)
        else source_id if canonical.FORMAL_NODE_RE.fullmatch(source_id) else None
    )
    actions: list[dict[str, Any]] = []
    for row in tracks:
        if str(row.get("track_status") or "").startswith("blocked"):
            continue
        node_id = str(row.get("formal_node_id") or "")
        if not canonical.FORMAL_NODE_RE.fullmatch(node_id):
            raise HotOutcomeError("formal_hot_track_identity_invalid")
        eligibility = str(row.get("seven_day_eligibility") or "")
        if eligibility not in formal_hot.VALID_TRACK_ELIGIBILITY:
            raise HotOutcomeError("formal_hot_track_eligibility_invalid")
        try:
            last = canonical._parse_optional_date(
                str(row.get("last_attempt_date") or "")
            )
            earliest = canonical._parse_optional_date(
                str(row.get("earliest_allowed_date") or "")
            )
        except canonical.ReviewLoopError as exc:
            raise HotOutcomeError(str(exc)) from exc
        if last is None:
            if earliest is not None or eligibility != "eligible_unrecorded_date":
                raise HotOutcomeError(
                    f"{node_id} has inconsistent unrecorded 7-day original-track gate"
                )
        else:
            expected = last + dt.timedelta(days=7)
            if earliest != expected:
                raise HotOutcomeError(
                    f"{node_id} has inconsistent 7-day original-track gate"
                )
            expected_eligibility = (
                "protected_until" if observed_date < expected else "eligible"
            )
            # A projection can legitimately age from protected to eligible
            # between cold rebuilds.  The inverse would mean the event date is
            # earlier than the gate represented by an already-eligible row.
            if eligibility == "eligible" and expected_eligibility != "eligible":
                raise HotOutcomeError(
                    f"{node_id} has an eligible gate before its earliest date"
                )
        if node_id == current_node:
            due = observed_date + dt.timedelta(days=7)
            action_reason = "current original attempt resets the 7-day gate"
        elif last is not None:
            due = max(observed_date, earliest)
            action_reason = (
                "organic or retained unhinted mechanism evidence; "
                "7-day gate satisfied"
            )
        else:
            due = observed_date
            action_reason = (
                "organic or retained unhinted mechanism evidence; "
                "7-day gate satisfied"
            )
        actions.append(
            canonical._make_action(
                action_type="original_question",
                identity=node_id,
                source_id=node_id,
                due_date=due,
                stage="original_track",
                trigger_event_id=event_id,
                origin_date=observed_date,
                reason=action_reason,
                first_break="none",
                formal_node_id=node_id,
                review_unit_id=review_unit_id,
            )
        )
    return actions


@study_commit_boundary
def record_outcome_hot(
    repo: str | Path,
    *,
    source: str,
    session_id: str,
    item_id: str,
    source_id: str,
    mechanism_key: str,
    knowledge_point: str,
    first_result: str,
    confidence: str,
    prompt_level: str,
    first_break: str,
    event_time: str,
    observed_date: str,
    idempotency_key: str,
    session_review_date: str | None = None,
    choice_result: str = "not_applicable",
    reasoning_result: str = "not_observed",
    correction_result: str = "not_attempted",
    response_seconds: float | None = None,
    latency_state: str = "unmeasured",
    cross_day: bool | None = None,
    question_valid: bool = True,
    formal_node_id: str | None = None,
    review_unit_id: str | None = None,
    trigger_action_id: str | None = None,
    legacy_import: bool = False,
    fragile_override: bool = False,
    first_break_provenance: str = "derived_classification",
    practice_mode: str | None = None,
    anchor_formal_node_id: str | None = None,
    variant_of_formal_node_id: str | None = None,
    followup_event_id: str | None = None,
    parent_trigger_event_id: str | None = None,
    context_slice_id: str | None = None,
    candidate_id: str | None = None,
    source_ref: str | None = None,
    source_sha256: str | None = None,
    selection_receipt_ref: str | None = None,
    selection_receipt_sha256: str | None = None,
    presentation_kind: str | None = None,
    managed_user_reply_binding_sha256: str | None = None,
    _from_session_sync: bool = False,
    review_state_dir: str | Path | None = None,
    formal_projection_dir: str | Path | None = None,
    current_question_minimal: bool = False,
) -> dict[str, Any]:
    root = Path(repo).expanduser().resolve()
    values = _prepare(
        source=source,
        session_id=session_id,
        item_id=item_id,
        source_id=source_id,
        mechanism_key=mechanism_key,
        knowledge_point=knowledge_point,
        first_result=first_result,
        choice_result=choice_result,
        reasoning_result=reasoning_result,
        confidence=confidence,
        prompt_level=prompt_level,
        first_break=first_break,
        event_time=event_time,
        observed_date=observed_date,
        session_review_date=session_review_date,
        idempotency_key=idempotency_key,
        correction_result=correction_result,
        response_seconds=response_seconds,
        latency_state=latency_state,
        cross_day=cross_day,
        question_valid=question_valid,
        formal_node_id=formal_node_id,
        review_unit_id=review_unit_id,
        trigger_action_id=trigger_action_id,
        legacy_import=legacy_import,
        fragile_override=fragile_override,
        first_break_provenance=first_break_provenance,
        practice_mode=practice_mode,
        anchor_formal_node_id=anchor_formal_node_id,
        variant_of_formal_node_id=variant_of_formal_node_id,
        followup_event_id=followup_event_id,
        parent_trigger_event_id=parent_trigger_event_id,
        context_slice_id=context_slice_id,
        candidate_id=candidate_id,
        source_ref=source_ref,
        source_sha256=source_sha256,
        selection_receipt_ref=selection_receipt_ref,
        selection_receipt_sha256=selection_receipt_sha256,
        presentation_kind=presentation_kind,
        managed_user_reply_binding_sha256=(
            managed_user_reply_binding_sha256
        ),
        from_session_sync=_from_session_sync,
    )
    identity = values["review_unit"] or values["formal_node"] or values["source_id"]
    identity_candidates = sorted(
        {
            value
            for value in (
                values["review_unit"],
                values["formal_node"],
                values["source_id"],
            )
            if value
        }
    )
    try:
        view = state_hot.begin_view(
            root,
            idempotency_key=values["key"],
            session_id=values["session_id"],
            item_id=values["item_id"],
            identity=identity,
            identity_candidates=identity_candidates,
            observed_date=values["day"].isoformat(),
            trigger_action_id=values["trigger_action"],
            state_dir=review_state_dir,
        )
    except state_hot.HotStateError as exc:
        raise HotOutcomeError(f"review_hot_state_unavailable:{exc}") from exc

    existing_ref = view.get("existing_event_ref")
    if existing_ref is not None:
        if not isinstance(existing_ref, dict):
            raise HotOutcomeError("existing_event_ref_invalid")
        existing, event_lookup = _load_existing_event_once(
            root,
            existing_ref,
            state_dir=review_state_dir,
        )
        if not _request_hash_compatible(
            existing,
            values["request_payload"],
            first_break_provenance=values["first_break_provenance"],
            choice_result=values["choice_result"],
            reasoning_result=values["reasoning_result"],
            presentation=values["presentation"],
        ):
            raise HotOutcomeError(
                "idempotency key was reused with different review evidence"
            )
        try:
            receipt = state_hot.lookup_commit_receipt(
                root,
                event_id=str(existing["event_id"]),
                state_dir=review_state_dir,
            )
            if receipt.get("status") != "found":
                raise HotOutcomeError(
                    "existing_event_hot_commit_proof_unavailable"
                )
            verified = state_hot.verify_commit(
                root,
                receipt_sha256=str(receipt["receipt_sha256"]),
                state_dir=review_state_dir,
            )
        except HotOutcomeError:
            raise
        except state_hot.HotStateError as exc:
            raise HotOutcomeError(
                "existing_event_hot_commit_proof_unavailable"
            ) from exc
        if verified.get("status") != "verified":
            raise HotOutcomeError("existing_event_commit_verification_failed")
        return {
            "schema": SCHEMA,
            "created": False,
            "event": existing,
            "commit_status": "already_committed",
            "commit_receipt_ref": receipt.get("receipt_ref"),
            "commit_receipt_sha256": receipt.get("receipt_sha256"),
            "verification": verified,
            "event_lookup": event_lookup,
            "review_scan_stats": view.get("scan_stats"),
            "formal_projection": {"status": "not_read_for_replay"},
            "formal_write_count": 0,
        }
    same_morning_ref = view.get("same_morning_item_ref")
    if source == "morning_review" and same_morning_ref is not None:
        if not isinstance(same_morning_ref, dict):
            raise HotOutcomeError("same_morning_item_ref_invalid")
        raise HotOutcomeError(
            "morning item already has a canonical outcome: "
            + str(same_morning_ref.get("event_id"))
        )

    if current_question_minimal:
        mapped: list[str] = []
        tracks: list[dict[str, Any]] = []
        formal_evidence = {"status": "skipped_current_question_minimal"}
    else:
        mapped, tracks, formal_evidence = _formal_context(
            root,
            source_id=values["source_id"],
            review_unit_id=values["review_unit"],
            formal_node_id=values["formal_node"],
            practice_mode=values["practice_mode"],
            formal_projection_dir=formal_projection_dir,
        )
    # ``current_question_minimal`` is a read-scope boundary, not a scheduling
    # opt-out.  A managed morning item may carry one exact action coordinate
    # that was frozen into the session.  Consuming that explicit action is a
    # bounded hot-state operation and does not require formal projections,
    # personalization, a ledger scan, or any Luna state.  Never enable the
    # implicit identity fallback for current-only questions: without an exact
    # action id there is no scheduling authority on this path.
    bound_current_action = bool(
        current_question_minimal and values["trigger_action"] is not None
    )
    scheduling_enabled = bool(not current_question_minimal or bound_current_action)
    trigger = view.get("eligible_trigger") if scheduling_enabled else None
    effective_identity = str(trigger["identity"]) if trigger else identity
    effective_trigger_id = str(trigger["action_id"]) if trigger else None
    derived_cross_day = values["cross_day"]
    if derived_cross_day is None and trigger:
        derived_cross_day = (
            canonical._parse_date(trigger["origin_date"], "origin_date")
            < values["day"]
        )
    if derived_cross_day is None and not current_question_minimal:
        derived_cross_day = view.get("prior_cross_day")
    hint_used = values["prompt_level"] != "none"
    fragile = (
        values["fragile_override"]
        or values["confidence"] == "low"
        or values["latency_state"] == "abnormal"
        or (source == "morning_review" and derived_cross_day is not True)
    )
    reasoning_break = bool(
        values["reasoning_result"] in {"partial", "diverged"}
        or values["first_break"].lower() not in canonical.NONE_BREAKS
    )
    ordinary_no_schedule = canonical.ordinary_independent_correct_needs_no_schedule(
        source=source,
        first_result=values["first_result"],
        confidence=values["confidence"],
        prompt_level=values["prompt_level"],
        reasoning_break=reasoning_break,
        correction_result=values["correction_result"],
        fragile=bool(
            values["fragile_override"]
            or values["confidence"] == "low"
            or values["latency_state"] == "abnormal"
        ),
    )
    stable_retention = scheduling_enabled and (
        source == "morning_review"
        and derived_cross_day is True
        and values["first_result"] == "independent_correct"
        and not hint_used
        and values["confidence"] in {"medium", "high"}
        and values["latency_state"] != "abnormal"
        and not values["fragile_override"]
    )
    if stable_retention:
        evidence_kind = "retention"
    elif values["first_result"] == "independent_correct" and (
        hint_used or values["correction_result"] != "not_attempted"
    ):
        evidence_kind = "repair"
    elif values["first_result"] == "independent_correct" and fragile:
        evidence_kind = "fragile"
    elif values["first_result"] == "independent_correct":
        evidence_kind = "practice"
    else:
        evidence_kind = "failure"
    organic_baseline = (not current_question_minimal) and bool(
        values["question_valid"] and not hint_used and not view.get("has_baseline")
    )
    event_id = canonical.content_id("RE", values["key"])
    route = canonical._route_candidate(
        event_id=event_id,
        first_result=values["first_result"],
        first_break=values["first_break"],
        mechanism_key=values["mechanism_key"],
        mapped=mapped,
        formal_node_id=values["formal_node"],
        review_unit_id=values["review_unit"],
    )
    generated: list[dict[str, Any]] = []
    if ordinary_no_schedule:
        next_stage = None
        due = None
        origin = None
        reason = None
    elif stable_retention:
        current_stage = trigger.get("stage") if trigger else None
        next_stage = canonical._next_stage(
            current_stage, int(view.get("retention_count") or 0)
        )
        origin = (
            canonical._parse_date(trigger["origin_date"], "origin_date")
            if trigger
            else values["day"]
        )
        due = origin + dt.timedelta(days=canonical.STAGE_DAYS[next_stage])
        if due <= values["day"]:
            due = values["day"] + dt.timedelta(
                days=max(1, canonical.STAGE_DAYS[next_stage])
            )
        reason = "cross-day unhinted independent retrieval retained"
    else:
        next_stage = (
            "D1" if source in {"daily_practice", "evening_d0"} else "D1_rescue"
        )
        due = values["day"] + dt.timedelta(days=1)
        origin = (
            values["day"]
            if trigger is None
            else canonical._parse_date(trigger["origin_date"], "origin_date")
        )
        if values["first_result"] == "independent_correct" and fragile:
            reason = "fragile retrieval needs a short unhinted retest"
        elif values["first_result"] == "independent_correct":
            reason = "same-day practice requires cross-day verification"
        else:
            reason = "unstable first response requires a short retry"
    if not ordinary_no_schedule:
        assert next_stage is not None and due is not None and origin is not None
        assert reason is not None
        generated.append(
            canonical._make_action(
                action_type="review",
                identity=effective_identity,
                source_id=values["source_id"],
                due_date=due,
                stage=next_stage,
                trigger_event_id=event_id,
                origin_date=origin,
                reason=reason,
                first_break=values["first_break"],
                formal_node_id=values["formal_node"],
                review_unit_id=values["review_unit"],
            )
        )
    if values["practice_mode"] != "variant" and not ordinary_no_schedule:
        generated.extend(
            _original_actions_from_tracks(
                tracks,
                event_id=event_id,
                observed_date=values["day"],
                review_unit_id=values["review_unit"],
                source_id=values["source_id"],
                first_result=values["first_result"],
                hint_used=hint_used,
                question_valid=values["question_valid"],
                fragile=(
                    values["fragile_override"]
                    or values["confidence"] == "low"
                    or values["latency_state"] == "abnormal"
                ),
                current_formal_node_id=values["formal_node"],
            )
        )
    if current_question_minimal and not bound_current_action:
        generated = []
    event = {
        "schema": canonical.LEDGER_SCHEMA,
        "event_kind": "outcome",
        "event_id": event_id,
        "idempotency_key": values["key"],
        "request_hash": values["request_hash"],
        "event_time": values["event_time"],
        "observed_date": values["day"].isoformat(),
        "source": source,
        "session_id": values["session_id"],
        "item_id": values["item_id"],
        "source_id": values["source_id"],
        "presentation_kind": values["presentation"],
        "mechanism_key": values["mechanism_key"],
        "knowledge_point": values["knowledge_point"] or None,
        "formal_node_id": values["formal_node"],
        "mapped_formal_node_ids": mapped,
        "review_unit_id": values["review_unit"],
        "first_result": values["first_result"],
        "choice_result": values["choice_result"],
        "reasoning_result": values["reasoning_result"],
        "confidence": values["confidence"],
        "hint_used": hint_used,
        "prompt_level": values["prompt_level"],
        "first_break": values["first_break"],
        "first_break_provenance": values["first_break_provenance"],
        "correction_result": values["correction_result"],
        "response_seconds": values["response_seconds"],
        "latency_state": values["latency_state"],
        "cross_day": derived_cross_day,
        "question_valid": values["question_valid"],
        "organic_baseline": organic_baseline,
        "evidence_kind": evidence_kind,
        "stable_retention": stable_retention,
        "fragile_mastery": fragile,
        "trigger_action_id": effective_trigger_id,
        "generated_actions": generated,
        "route": route,
        "legacy_import": values["legacy_import"],
        "formal_write_authorized": False,
    }
    if current_question_minimal:
        event["scheduling_mode"] = (
            "bound_morning_action"
            if bound_current_action
            else "deferred_current_question"
        )
    elif ordinary_no_schedule:
        event["scheduling_mode"] = "ordinary_no_schedule"
    if values["session_review_date"] is not None:
        event["session_review_date"] = values["session_review_date"]
    if values["managed_user_reply_binding_sha256"] is not None:
        event["managed_user_reply_binding_sha256"] = values[
            "managed_user_reply_binding_sha256"
        ]
    if values["followup_payload"] is not None:
        event.update(values["followup_payload"])
        event["actual_question_formal_node_id"] = (
            values["formal_node"]
            if values["practice_mode"] == "original"
            else None
        )
    try:
        with canonical.runtime_lock(root):
            committed = state_hot.append_commit(
                root,
                event=event,
                expected_precondition=view["precondition"],
                state_dir=review_state_dir,
                runtime_lock_held=True,
            )
        verified = state_hot.verify_commit(
            root,
            receipt_sha256=committed["receipt_sha256"],
            state_dir=review_state_dir,
        )
    except state_hot.HotStateError as exc:
        raise HotOutcomeError(f"review_hot_commit_failed:{exc}") from exc
    if verified.get("status") != "verified":
        raise HotOutcomeError("review_hot_commit_verification_failed")
    return {
        "schema": SCHEMA,
        "created": committed.get("status") == "committed",
        "event": event,
        "commit_status": committed.get("status"),
        "commit_receipt_ref": committed.get("receipt_ref"),
        "commit_receipt_sha256": committed.get("receipt_sha256"),
        "verification": verified,
        "review_scan_stats": view.get("scan_stats"),
        "formal_projection": formal_evidence,
        "legacy_state_projection": "deferred_cold_rebuild",
        "formal_write_count": 0,
    }


@study_commit_boundary
def record_campaign_repair_hot(
    repo: str | Path, *, run_id: str, item_id: str, formal_node_id: str,
    source_id: str, confidence: str, prompt_dependency: str, event_time: str,
    session_review_date: str, capture_id: str, package_locator: str,
    package_sha256: str, review_state_dir: str | Path | None = None,
) -> dict[str, Any]:
    """One verified terminal correction, distinct from its unchanged first outcome."""
    root = Path(repo).expanduser().resolve()
    if not canonical.FORMAL_NODE_RE.fullmatch(formal_node_id):
        raise HotOutcomeError("campaign_repair_formal_node_invalid")
    if not re.fullmatch(r"[0-9a-f]{64}", package_sha256) or not package_locator.startswith("cs408-conversation-package://"):
        raise HotOutcomeError("campaign_repair_package_binding_invalid")
    key = "campaign-final-repair:" + str(capture_id)
    event = canonical.build_repair_event(
        session_id=run_id, item_id=item_id, source_id=source_id,
        result="independent_correct" if prompt_dependency == "none" else "partial",
        confidence=confidence, prompt_level=prompt_dependency,
        event_time=event_time, observed_date=session_review_date,
        session_review_date=session_review_date, idempotency_key=key, attempt=1,
    )
    event.update(formal_node_id=formal_node_id, correction_conclusion="correct",
                 reasoning_result="sound", repair_contract="campaign_final_capture_v1",
                 capture_id=capture_id,
                 source_package={"locator": package_locator, "sha256": package_sha256},
                 event_time_basis="verified_capture_commit")
    event["request_hash"] = hashlib.sha256(canonical.canonical_json(
        {k: v for k, v in event.items() if k != "request_hash"}).encode()).hexdigest()
    view = state_hot.begin_view(
        root, idempotency_key=key, session_id=run_id, item_id=item_id,
        identity=canonical._identity(event), identity_candidates=state_hot._event_identity_candidates(event),
        observed_date=event["observed_date"], event_kind="repair", repair_attempt=1,
        state_dir=review_state_dir,
    )
    if view.get("existing_event_ref"):
        prior, _ = _load_existing_event_once(root, view["existing_event_ref"], state_dir=review_state_dir)
        if prior != event:
            raise HotOutcomeError("campaign_repair_idempotency_conflict")
        committed = state_hot.lookup_commit_receipt(root, event_id=event["event_id"], state_dir=review_state_dir)
        if committed.get("status") != "found":
            raise HotOutcomeError("campaign_repair_receipt_missing")
        created = False
    else:
        with canonical.runtime_lock(root):
            committed = state_hot.append_commit(root, event=event,
                expected_precondition=view["precondition"], state_dir=review_state_dir, runtime_lock_held=True)
        created = True
    verified = state_hot.verify_commit(root, receipt_sha256=committed["receipt_sha256"], state_dir=review_state_dir)
    if verified.get("status") != "verified":
        raise HotOutcomeError("campaign_repair_commit_not_verified")
    return {"status": "verified", "created": created, "event": event,
            "verification": verified, "commit_receipt_sha256": committed["receipt_sha256"],
            "formal_write_count": 0}


@study_commit_boundary
def record_campaign_outcome_hot(
    repo: str | Path,
    *,
    run_id: str,
    segment_id: str,
    item_id: str,
    source_id: str,
    formal_node_id: str,
    true_item_id: str,
    variant_id: str | None,
    surface_kind: str,
    campaign_item_commitment_sha256: str,
    choice_result: str,
    reasoning_result: str,
    confidence: str,
    prompt_dependency: str,
    response_mode: str,
    screened_correct: bool,
    reason_audited: bool,
    recognized_original: bool,
    response_seconds: float | None,
    campaign_rating: str,
    capture_required: bool,
    event_time: str,
    observed_date: str,
    idempotency_key: str,
    review_state_dir: str | Path | None = None,
) -> dict[str, Any]:
    """Commit one campaign result to the canonical review ledger.

    This is deliberately separate from :func:`record_outcome_hot`.  Campaign
    scheduling is owned by the FSRS successor, so this writer never consumes a
    legacy review action, generates a D1-D90 successor, creates a candidate, or
    invokes any session or Capture writer.
    """

    root = Path(repo).expanduser().resolve()
    try:
        run = canonical._safe_id(run_id, "run_id", required=True)
        segment = canonical._safe_id(segment_id, "segment_id", required=True)
        item = canonical._safe_id(item_id, "item_id", required=True)
        source_value = canonical._safe_id(source_id, "source_id", required=True)
        formal_node = canonical._safe_id(
            formal_node_id, "formal_node_id", required=True
        )
        true_item = canonical._safe_id(
            true_item_id, "true_item_id", required=True
        )
        variant = canonical._safe_id(variant_id, "variant_id")
        key = canonical._safe_id(
            idempotency_key, "idempotency_key", required=True
        )
        day = canonical._parse_date(observed_date, "observed_date")
        parsed_event_time = canonical._parse_datetime(event_time)
    except canonical.ReviewLoopError as exc:
        raise HotOutcomeError(str(exc)) from exc
    assert run and segment and item and source_value and formal_node and true_item and key
    if not canonical.FORMAL_NODE_RE.fullmatch(formal_node):
        raise HotOutcomeError("campaign_formal_node_id_invalid")
    run_match = re.fullmatch(r"MCR-(\d{8})-[0-9A-F]{16}", run)
    if run_match is None:
        raise HotOutcomeError("campaign_run_id_invalid")
    run_date_text = run_match.group(1)
    try:
        run_day = canonical._parse_date(
            f"{run_date_text[:4]}-{run_date_text[4:6]}-{run_date_text[6:8]}",
            "campaign_date",
        )
    except canonical.ReviewLoopError as exc:
        raise HotOutcomeError(str(exc)) from exc
    if not re.fullmatch(re.escape(run) + r"-S\d{3}", segment):
        raise HotOutcomeError("campaign_segment_id_invalid")
    if (
        not isinstance(campaign_item_commitment_sha256, str)
        or not re.fullmatch(r"[0-9a-f]{64}", campaign_item_commitment_sha256)
    ):
        raise HotOutcomeError("campaign_item_commitment_sha256_invalid")
    if surface_kind not in {"canonical_original", "formal_original", "verified_variant"}:
        raise HotOutcomeError("campaign_surface_kind_invalid")
    if surface_kind == "canonical_original":
        if variant is not None or not re.fullmatch(
            r"EXAM408-\d{4}-Q(?:0[1-9]|[1-3]\d|40)", true_item
        ):
            raise HotOutcomeError("campaign_original_identity_invalid")
    elif surface_kind == "formal_original":
        if variant is not None or not re.fullmatch(r"FOS-[0-9A-F]{24}", true_item):
            raise HotOutcomeError("campaign_formal_original_identity_invalid")
    elif variant is None or variant != true_item:
        raise HotOutcomeError("campaign_variant_true_item_identity_drifted")
    elif choice_result == "correct" and not reason_audited:
        raise HotOutcomeError("campaign_variant_requires_reason_audit")
    if run_day > day:
        raise HotOutcomeError("campaign_date_after_observed_date")
    if parsed_event_time.astimezone(canonical.LOCAL_TZ).date() != day:
        raise HotOutcomeError("campaign_event_time_observed_date_drifted")
    if response_seconds is not None:
        if (
            isinstance(response_seconds, bool)
            or not isinstance(response_seconds, (int, float))
            or not 0 < float(response_seconds) <= 7200
        ):
            raise HotOutcomeError("campaign_response_seconds_invalid")
        normalized_response_seconds: float | None = float(response_seconds)
    else:
        normalized_response_seconds = None

    first_result = _campaign_first_result(
        response_mode=response_mode,
        screened_correct=screened_correct,
        reason_audited=reason_audited,
        recognized_original=recognized_original,
        variant_id=variant,
        choice_result=choice_result,
        reasoning_result=reasoning_result,
        confidence=confidence,
        prompt_level=prompt_dependency,
        campaign_rating=campaign_rating,
        capture_required=capture_required,
    )
    request_payload = {
        "source": "morning_review",
        "session_id": run,
        "session_review_date": run_day.isoformat(),
        "item_id": item,
        "source_id": source_value,
        "formal_node_id": formal_node,
        "event_time": event_time,
        "observed_date": day.isoformat(),
        "response_mode": response_mode,
        "screened_correct": screened_correct,
        "reason_audited": reason_audited,
        "recognized_original": recognized_original,
        "true_item_id": true_item,
        "variant_id": variant,
        "surface_kind": surface_kind,
        "response_seconds": normalized_response_seconds,
        "choice_result": choice_result,
        "reasoning_result": reasoning_result,
        "confidence": confidence,
        "prompt_dependency": prompt_dependency,
        "capture_required": capture_required,
        "campaign_run_id": run,
        "campaign_segment_id": segment,
        "campaign_item_commitment_sha256": campaign_item_commitment_sha256,
    }
    request_hash = hashlib.sha256(
        canonical.canonical_json(request_payload).encode("utf-8")
    ).hexdigest()
    try:
        view = state_hot.begin_view(
            root,
            idempotency_key=key,
            session_id=run,
            item_id=item,
            identity=formal_node,
            identity_candidates=[formal_node, source_value],
            observed_date=day.isoformat(),
            state_dir=review_state_dir,
        )
    except state_hot.HotStateError as exc:
        raise HotOutcomeError(f"review_hot_state_unavailable:{exc}") from exc

    existing_ref = view.get("existing_event_ref")
    if existing_ref is not None:
        if not isinstance(existing_ref, dict):
            raise HotOutcomeError("existing_event_ref_invalid")
        existing, event_lookup = _load_existing_event_once(
            root, existing_ref, state_dir=review_state_dir
        )
        if (
            existing.get("request_hash") != request_hash
            or existing.get("scheduling_mode") != "campaign_fsrs_successor"
            or existing.get("campaign_item_commitment_sha256")
            != campaign_item_commitment_sha256
        ):
            raise HotOutcomeError(
                "idempotency key was reused with different campaign review evidence"
            )
        try:
            receipt = state_hot.lookup_commit_receipt(
                root,
                event_id=str(existing["event_id"]),
                state_dir=review_state_dir,
            )
            if receipt.get("status") != "found":
                raise HotOutcomeError(
                    "existing_campaign_event_hot_commit_proof_unavailable"
                )
            verified = state_hot.verify_commit(
                root,
                receipt_sha256=str(receipt["receipt_sha256"]),
                state_dir=review_state_dir,
            )
        except HotOutcomeError:
            raise
        except state_hot.HotStateError as exc:
            raise HotOutcomeError(
                "existing_campaign_event_hot_commit_proof_unavailable"
            ) from exc
        if verified.get("status") != "verified":
            raise HotOutcomeError(
                "existing_campaign_event_commit_verification_failed"
            )
        scheduler_receipt = _campaign_scheduler_receipt(
            existing,
            event_sha256=str(event_lookup["event_sha256"]),
            commit_receipt_sha256=str(receipt["receipt_sha256"]),
            campaign_rating=campaign_rating,
        )
        return {
            "schema": CAMPAIGN_SCHEMA,
            "created": False,
            "event": existing,
            "commit_status": "already_committed",
            "commit_receipt_ref": receipt.get("receipt_ref"),
            "commit_receipt_sha256": receipt.get("receipt_sha256"),
            "verification": verified,
            "event_lookup": event_lookup,
            "review_scan_stats": view.get("scan_stats"),
            "scheduler_receipt": scheduler_receipt,
            "capture_write_count": 0,
            "formal_write_count": 0,
        }
    if view.get("same_morning_item_ref") is not None:
        raise HotOutcomeError("campaign item already has a canonical outcome")

    event_id = canonical.content_id("RE", key)
    event = {
        "schema": canonical.LEDGER_SCHEMA,
        "event_kind": "outcome",
        "event_id": event_id,
        "idempotency_key": key,
        "request_hash": request_hash,
        "event_time": event_time,
        "observed_date": day.isoformat(),
        "session_review_date": run_day.isoformat(),
        "source": "morning_review",
        "session_id": run,
        "item_id": item,
        "source_id": source_value,
        "presentation_kind": (
            "derived_variant"
            if surface_kind == "verified_variant"
            else "original_question"
        ),
        "mechanism_key": formal_node,
        "knowledge_point": None,
        "formal_node_id": formal_node,
        "mapped_formal_node_ids": [],
        "review_unit_id": None,
        "first_result": first_result,
        "choice_result": choice_result,
        "reasoning_result": reasoning_result,
        "confidence": confidence,
        "hint_used": prompt_dependency != "none",
        "prompt_level": prompt_dependency,
        "first_break": "not_observed",
        "first_break_provenance": "not_observed",
        "correction_result": "not_attempted",
        "response_seconds": normalized_response_seconds,
        "latency_state": "unmeasured",
        "cross_day": run_day < day,
        "question_valid": True,
        "organic_baseline": False,
        "evidence_kind": "campaign_rapid_review",
        "stable_retention": False,
        "fragile_mastery": False,
        "trigger_action_id": None,
        "generated_actions": [],
        "route": {
            "kind": "campaign_evidence_only",
            "formal_write_authorized": False,
        },
        "legacy_import": False,
        "scheduling_mode": "campaign_fsrs_successor",
        "response_mode": response_mode,
        "screened_correct": screened_correct,
        "reason_audited": reason_audited,
        "recognized_original": recognized_original,
        "true_item_id": true_item,
        "variant_id": variant,
        "campaign_run_id": run,
        "campaign_segment_id": segment,
        "campaign_item_commitment_sha256": campaign_item_commitment_sha256,
        "capture_required": capture_required,
        "formal_write_authorized": False,
    }
    try:
        with canonical.runtime_lock(root):
            committed = state_hot.append_commit(
                root,
                event=event,
                expected_precondition=view["precondition"],
                state_dir=review_state_dir,
                runtime_lock_held=True,
            )
        verified = state_hot.verify_commit(
            root,
            receipt_sha256=str(committed["receipt_sha256"]),
            state_dir=review_state_dir,
        )
    except state_hot.HotStateError as exc:
        raise HotOutcomeError(f"review_hot_commit_failed:{exc}") from exc
    if verified.get("status") != "verified":
        raise HotOutcomeError("review_hot_commit_verification_failed")
    scheduler_receipt = _campaign_scheduler_receipt(
        event,
        event_sha256=str(committed["event_sha256"]),
        commit_receipt_sha256=str(committed["receipt_sha256"]),
        campaign_rating=campaign_rating,
    )
    return {
        "schema": CAMPAIGN_SCHEMA,
        "created": committed.get("status") == "committed",
        "event": event,
        "commit_status": committed.get("status"),
        "commit_receipt_ref": committed.get("receipt_ref"),
        "commit_receipt_sha256": committed.get("receipt_sha256"),
        "verification": verified,
        "review_scan_stats": view.get("scan_stats"),
        "scheduler_receipt": scheduler_receipt,
        "capture_write_count": 0,
        "formal_write_count": 0,
    }
