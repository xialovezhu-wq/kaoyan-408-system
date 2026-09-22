#!/usr/bin/env python3
"""Persist one explicitly authorized non-morning 408 study answer.

This is the generic counterpart of ``morning_review_session.py answer-turn`` for
ordinary single-question study, true-exam review, and evening D0.  It keeps the
transaction small and answer-safe:

1. append one canonical ``daily_practice`` or ``evening_d0`` outcome;
2. verify its exact commit receipt before the caller reveals feedback;
3. conditionally commit and independently authorize an answer-safe ordinary
   failure capture;
4. apply a lightweight personalization overlay without ranking a next item on
   the intake confirmation turn; and
5. retain a diagnostic compatibility mode that may prepare a next action.

Fact capture and ``capture_authorized`` remain separate receipt-bound ledger
events.  This public entrypoint orchestrates both only because the harness calls
it after a current-message explicit intake request.  It never writes a formal
node, relation, mastery field, dated review artifact, or intake batch.
"""

from __future__ import annotations

import argparse
import copy
import hashlib
import json
import math
import os
import re
import stat
import sys
import tempfile
from pathlib import Path
from typing import Any

from linked_practice_source_408 import (
    LinkedPracticeSourceError,
    validate_selection_receipt,
    validate_source_for_practice,
)
from ordinary_capture_hot_adapter_408 import (
    OrdinaryCaptureHotError,
    capture_managed_ordinary_outcome,
)
import personalized_context_408 as personalization_context
from personalized_context_408 import DEFAULT_MAX_BYTES, safe_context_slice
from review_feedback_loop import (
    CHOICE_RESULTS,
    CONFIDENCE_LEVELS,
    CORRECTION_RESULTS,
    FIRST_BREAK_PROVENANCES,
    FIRST_RESULTS,
    PROMPT_LEVELS,
    REASONING_RESULTS,
)
from review_outcome_hot_408 import HotOutcomeError, record_outcome_hot


SCHEMA = "personalized_study_turn_v1"
INTAKE_OVERLAY_SCHEMA = "personalization_intake_overlay_v1"
PACKAGE_SCHEMA = "personalized_study_answer_v1"
ALLOWED_SOURCES = {"daily_practice", "evening_d0"}
SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
REQUEST_ID_RE = re.compile(r"^[A-Za-z0-9_.:@-]{1,160}$")
FAST_INTENT_SCHEMA = "managed_408_fast_intake_intent_v1"
AUTHORIZATION_BINDING_SCHEMA = "managed_408_explicit_authorization_binding_v1"
MANAGED_AUTHORIZATION_RECEIPT_SCHEMA = (
    "managed_408_study_authorization_receipt_v1"
)
MANAGED_AUTHORIZATION_RECEIPT_KIND = "verified_current_message_intent"
RICH_INTENT_REL = Path(
    "wiki/study_vaults/408-full/state/managed-408-rich-intake-turn/objects/intent"
)
MANAGED_AUTHORIZATION_REL = Path(
    "wiki/study_vaults/408-full/state/managed-408-study-authorizations/objects"
)
MAX_AUTHORIZATION_OBJECT_BYTES = 64 * 1024
ALLOWED_RESULTS = FIRST_RESULTS
ALLOWED_CONFIDENCE = CONFIDENCE_LEVELS
ALLOWED_PROMPT_LEVELS = PROMPT_LEVELS
ALLOWED_PROVENANCE = FIRST_BREAK_PROVENANCES
ALLOWED_PACKAGE_KEYS = {
    "schema",
    "source",
    "session_id",
    "item_id",
    "source_id",
    "mechanism_key",
    "knowledge_point",
    "first_result",
    "choice_result",
    "reasoning_result",
    "confidence",
    "prompt_level",
    "first_break",
    "first_break_provenance",
    "event_time",
    "observed_date",
    "idempotency_key",
    "correction_result",
    "response_seconds",
    "latency_state",
    "cross_day",
    "question_valid",
    "formal_node_id",
    "review_unit_id",
    "trigger_action_id",
    "presentation_kind",
    "fragile_override",
}
PROTECTED_PACKAGE_KEYS = {
    "question",
    "stem",
    "options",
    "answer",
    "correct_answer",
    "solution",
    "analysis",
    "full_response",
    "learner_response",
    "feedback",
    "image",
    "attachment",
}


class PersonalizedStudyTurnError(RuntimeError):
    """A controlled package or transaction failure."""


def _canonical_json_bytes(value: Any, *, pretty: bool = False) -> bytes:
    options: dict[str, Any] = {"ensure_ascii": False, "sort_keys": True}
    if pretty:
        options["indent"] = 2
    else:
        options["separators"] = (",", ":")
    return (json.dumps(value, **options) + "\n").encode("utf-8")


def _sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _safe_content_object(
    repo: Path,
    *,
    relative_ref: str,
    expected_path: Path,
    expected_sha256: str,
    label: str,
) -> tuple[dict[str, Any], bytes]:
    if not SHA256_RE.fullmatch(str(expected_sha256 or "")):
        raise PersonalizedStudyTurnError(f"{label}_sha256_invalid")
    if not isinstance(relative_ref, str) or not relative_ref:
        raise PersonalizedStudyTurnError(f"{label}_ref_invalid")
    ref = Path(relative_ref)
    if ref.is_absolute() or ".." in ref.parts or ref != expected_path:
        raise PersonalizedStudyTurnError(f"{label}_ref_invalid")
    path = repo / ref
    cursor = repo
    for part in ref.parts:
        cursor = cursor / part
        if cursor.exists() and stat.S_ISLNK(cursor.lstat().st_mode):
            raise PersonalizedStudyTurnError(f"{label}_symlink_rejected")
    try:
        info = path.lstat()
        if not stat.S_ISREG(info.st_mode) or info.st_size > MAX_AUTHORIZATION_OBJECT_BYTES:
            raise PersonalizedStudyTurnError(f"{label}_invalid")
        raw = path.read_bytes()
        value = json.loads(raw.decode("utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise PersonalizedStudyTurnError(f"{label}_invalid") from exc
    if len(raw) != info.st_size or _sha256_bytes(raw) != expected_sha256:
        raise PersonalizedStudyTurnError(f"{label}_sha256_mismatch")
    if not isinstance(value, dict):
        raise PersonalizedStudyTurnError(f"{label}_invalid")
    return value, raw


def _validate_managed_fast_intake_intent(
    repo: Path,
    values: dict[str, Any],
    *,
    intent_ref: str | None,
    intent_sha256: str | None,
) -> dict[str, Any]:
    digest = str(intent_sha256 or "")
    expected_ref = RICH_INTENT_REL / f"{digest}.json"
    intent, _ = _safe_content_object(
        repo,
        relative_ref=str(intent_ref or ""),
        expected_path=expected_ref,
        expected_sha256=digest,
        label="managed_authorization_intent",
    )
    if (
        intent.get("schema") != FAST_INTENT_SCHEMA
        or intent.get("kind") != "explicit_fast_intake_intent"
        or intent.get("authorization_body_stored") is not False
        or intent.get("formal_write_count") != 0
        or not REQUEST_ID_RE.fullmatch(str(intent.get("request_id") or ""))
        or not isinstance(intent.get("authorization_message_size_bytes"), int)
        or not 0 < intent["authorization_message_size_bytes"] <= 4096
    ):
        raise PersonalizedStudyTurnError("managed_authorization_intent_invalid")
    for field in (
        "display_receipt_sha256",
        "authorization_message_sha256",
        "authorization_binding_sha256",
    ):
        if not SHA256_RE.fullmatch(str(intent.get(field) or "")):
            raise PersonalizedStudyTurnError("managed_authorization_intent_invalid")
    binding_material = {
        "schema": AUTHORIZATION_BINDING_SCHEMA,
        "request_id": intent["request_id"],
        "display_receipt_sha256": intent["display_receipt_sha256"],
        "authorization_message_sha256": intent[
            "authorization_message_sha256"
        ],
        "authorization_message_size_bytes": intent[
            "authorization_message_size_bytes"
        ],
    }
    if intent["authorization_binding_sha256"] != _sha256_bytes(
        _canonical_json_bytes(binding_material)
    ):
        raise PersonalizedStudyTurnError(
            "managed_authorization_binding_invalid"
        )
    expected = {
        "session_id": values["session_id"],
        "item_id": values["item_id"],
        "canonical_first_result": values["first_result"],
        "choice_result": values["choice_result"],
        "confidence": values["confidence"],
        "reasoning_result": values["reasoning_result"],
        "first_break_provenance": values["first_break_provenance"],
        "event_time": values["event_time"],
        "observed_date": values["observed_date"],
        "idempotency_key": values["idempotency_key"],
    }
    for field, expected_value in expected.items():
        if intent.get(field) != expected_value:
            raise PersonalizedStudyTurnError(
                f"managed_authorization_intent_{field}_mismatch"
            )
    return intent


def _atomic_write_authorization_object(path: Path, raw: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    descriptor, temporary = tempfile.mkstemp(
        prefix=f".{path.name}.", dir=path.parent
    )
    tmp = Path(temporary)
    try:
        os.fchmod(descriptor, 0o600)
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(raw)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp, path)
        directory_fd = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
    finally:
        if tmp.exists():
            tmp.unlink()


def _publish_managed_authorization_receipt(
    repo: Path,
    *,
    intent_ref: str,
    intent_sha256: str,
    intent: dict[str, Any],
    event: dict[str, Any],
    event_sha256: str,
    review_commit_receipt_sha256: str,
) -> tuple[str, str, dict[str, Any]]:
    material = {
        "schema": MANAGED_AUTHORIZATION_RECEIPT_SCHEMA,
        "kind": MANAGED_AUTHORIZATION_RECEIPT_KIND,
        "intent_receipt_ref": intent_ref,
        "intent_receipt_sha256": intent_sha256,
        "request_id": intent["request_id"],
        "display_receipt_sha256": intent["display_receipt_sha256"],
        "authorization_message_sha256": intent[
            "authorization_message_sha256"
        ],
        "authorization_message_size_bytes": intent[
            "authorization_message_size_bytes"
        ],
        "authorization_binding_sha256": intent[
            "authorization_binding_sha256"
        ],
        "review_event_id": event["event_id"],
        "review_event_sha256": event_sha256,
        "review_commit_receipt_sha256": review_commit_receipt_sha256,
        "authorization_body_stored": False,
        "formal_write_count": 0,
    }
    core_sha = _sha256_bytes(_canonical_json_bytes(material))
    value = {**material, "receipt_id": f"MSAR-{core_sha[:24].upper()}"}
    raw = _canonical_json_bytes(value, pretty=True)
    digest = _sha256_bytes(raw)
    relative = MANAGED_AUTHORIZATION_REL / f"{digest}.json"
    path = repo / relative
    if path.exists():
        existing = path.read_bytes()
        if existing != raw:
            raise PersonalizedStudyTurnError(
                "managed_authorization_receipt_content_conflict"
            )
    else:
        _atomic_write_authorization_object(path, raw)
    return relative.as_posix(), digest, value


def _required_text(package: dict[str, Any], field: str) -> str:
    raw = package.get(field)
    if not isinstance(raw, str):
        raise PersonalizedStudyTurnError(f"invalid_{field}_type")
    value = raw.strip()
    if not value:
        raise PersonalizedStudyTurnError(f"missing_{field}")
    return value


def _optional_text(package: dict[str, Any], field: str) -> str | None:
    raw = package.get(field)
    if raw is None:
        return None
    if not isinstance(raw, str):
        raise PersonalizedStudyTurnError(f"invalid_{field}_type")
    value = raw.strip()
    return value or None


def _contains_protected_nested_key(value: Any) -> bool:
    if isinstance(value, dict):
        return any(
            str(key).lower() in PROTECTED_PACKAGE_KEYS
            or _contains_protected_nested_key(item)
            for key, item in value.items()
        )
    if isinstance(value, (list, tuple)):
        return any(_contains_protected_nested_key(item) for item in value)
    return False


def _validate_package(package: dict[str, Any]) -> dict[str, Any]:
    keys = set(package)
    if keys & PROTECTED_PACKAGE_KEYS:
        raise PersonalizedStudyTurnError("protected_content_in_package")
    if _contains_protected_nested_key(package):
        raise PersonalizedStudyTurnError("protected_content_in_package")
    if any(
        isinstance(value, (dict, list, tuple, set))
        for value in package.values()
    ):
        raise PersonalizedStudyTurnError("non_scalar_package_field")
    if keys - ALLOWED_PACKAGE_KEYS:
        raise PersonalizedStudyTurnError("unknown_package_field")
    if package.get("schema") != PACKAGE_SCHEMA:
        raise PersonalizedStudyTurnError("invalid_package_schema")
    source = _required_text(package, "source")
    if source not in ALLOWED_SOURCES:
        raise PersonalizedStudyTurnError("invalid_source")
    first_result = _required_text(package, "first_result")
    if first_result not in ALLOWED_RESULTS:
        raise PersonalizedStudyTurnError("invalid_first_result")
    choice_result = _required_text(package, "choice_result")
    if choice_result not in CHOICE_RESULTS:
        raise PersonalizedStudyTurnError("invalid_choice_result")
    reasoning_result = _required_text(package, "reasoning_result")
    if reasoning_result not in REASONING_RESULTS:
        raise PersonalizedStudyTurnError("invalid_reasoning_result")
    if first_result == "independent_correct" and (
        choice_result in {"incorrect", "partial", "uncertain"}
        or reasoning_result in {"partial", "diverged"}
    ):
        raise PersonalizedStudyTurnError("inconsistent_first_result")
    confidence = _required_text(package, "confidence")
    if confidence not in ALLOWED_CONFIDENCE:
        raise PersonalizedStudyTurnError("invalid_confidence")
    prompt_level = _required_text(package, "prompt_level")
    if prompt_level not in ALLOWED_PROMPT_LEVELS:
        raise PersonalizedStudyTurnError("invalid_prompt_level")
    provenance = str(
        package.get("first_break_provenance") or "derived_classification"
    ).strip()
    if provenance not in ALLOWED_PROVENANCE:
        raise PersonalizedStudyTurnError("invalid_first_break_provenance")
    correction_result = str(
        package.get("correction_result") or "not_attempted"
    ).strip()
    if correction_result not in CORRECTION_RESULTS:
        raise PersonalizedStudyTurnError("invalid_correction_result")
    question_valid = package.get("question_valid", True)
    if not isinstance(question_valid, bool):
        raise PersonalizedStudyTurnError("invalid_question_valid")
    response_seconds = package.get("response_seconds")
    if response_seconds is not None and (
        isinstance(response_seconds, bool)
        or not isinstance(response_seconds, (int, float))
        or not math.isfinite(float(response_seconds))
    ):
        raise PersonalizedStudyTurnError("invalid_response_seconds")
    cross_day = package.get("cross_day")
    if cross_day is not None and not isinstance(cross_day, bool):
        raise PersonalizedStudyTurnError("invalid_cross_day")
    fragile_override = package.get("fragile_override", False)
    if not isinstance(fragile_override, bool):
        raise PersonalizedStudyTurnError("invalid_fragile_override")
    if fragile_override and not (
        first_result == "independent_correct"
        and choice_result == "correct"
        and confidence in {"low", "medium"}
    ):
        raise PersonalizedStudyTurnError("inconsistent_fragile_override")
    return {
        "source": source,
        "session_id": _required_text(package, "session_id"),
        "item_id": _required_text(package, "item_id"),
        "source_id": _required_text(package, "source_id"),
        "mechanism_key": _required_text(package, "mechanism_key"),
        "knowledge_point": _optional_text(package, "knowledge_point") or "",
        "first_result": first_result,
        "choice_result": choice_result,
        "reasoning_result": reasoning_result,
        "confidence": confidence,
        "prompt_level": prompt_level,
        "first_break": _required_text(package, "first_break"),
        "first_break_provenance": provenance,
        "event_time": _required_text(package, "event_time"),
        "observed_date": _required_text(package, "observed_date"),
        "idempotency_key": _required_text(package, "idempotency_key"),
        "correction_result": correction_result,
        "response_seconds": response_seconds,
        "latency_state": _optional_text(package, "latency_state") or "unmeasured",
        "cross_day": cross_day,
        "question_valid": question_valid,
        "formal_node_id": _optional_text(package, "formal_node_id"),
        "review_unit_id": _optional_text(package, "review_unit_id"),
        "trigger_action_id": _optional_text(package, "trigger_action_id"),
        "presentation_kind": _optional_text(package, "presentation_kind"),
        "fragile_override": fragile_override,
    }


def _display_candidate(
    repo: Path,
    personalization: dict[str, Any],
    *,
    state_dir: str | Path | None,
) -> dict[str, Any]:
    if personalization.get("status") != "ready":
        return {
            "status": "not_available",
            "reason_code": "personalization_unavailable",
        }
    selected = ((personalization.get("context") or {}).get("selected_practice"))
    if not isinstance(selected, dict) or selected.get("schedule_allowed") is not True:
        return {
            "status": "not_scheduled",
            "reason_code": str(
                (selected or {}).get("reason_code")
                or (selected or {}).get("status")
                or "no_verified_selection"
            ),
        }
    practice_mode = str(selected.get("practice_mode") or "")
    formal_node_id = _optional_text(selected, "formal_node_id")
    anchor = _optional_text(selected, "anchor_formal_node_id")
    try:
        source = validate_source_for_practice(
            repo,
            selected.get("source_ref"),
            selected.get("source_sha256"),
            practice_mode=practice_mode,
            formal_node_id=formal_node_id,
            variant_of_formal_node_id=(anchor if practice_mode == "variant" else None),
            personalization_state_dir=state_dir,
        )
        expected = dict(selected)
        expected["slice_id"] = personalization.get("slice_id")
        expected["trigger_event_id"] = personalization.get("trigger_event_id")
        receipt = validate_selection_receipt(
            repo,
            personalization.get("selection_receipt_ref"),
            personalization.get("selection_receipt_sha256"),
            expected=expected,
            source_path=source["path"],
            personalization_state_dir=state_dir,
        )
    except LinkedPracticeSourceError:
        return {
            "status": "not_available",
            "reason_code": "verified_selection_revalidation_failed",
        }
    return {
        "status": "ready",
        "candidate_id": selected.get("candidate_id"),
        "practice_mode": practice_mode,
        "formal_node_id": formal_node_id,
        "anchor_formal_node_id": anchor,
        "source_id": selected.get("source_id"),
        "source_ref": source["source_ref"],
        "source_sha256": source["source_sha256"],
        "selection_receipt_ref": receipt["selection_receipt_ref"],
        "selection_receipt_sha256": receipt["selection_receipt_sha256"],
        "knowledge_point": selected.get("knowledge_point"),
        "mechanism_key": selected.get("mechanism_key"),
        "review_unit_id": selected.get("review_unit_id"),
        "cross_edge_id": selected.get("cross_edge_id"),
        "gate_decision": selected.get("gate_decision"),
        "answer_safe": True,
    }


def _safe_intake_overlay(
    repo: Path,
    current_event: dict[str, Any],
    *,
    state_dir: str | Path | None,
) -> dict[str, Any]:
    """Apply only the current-event overlay, with no candidate ranking.

    The overlay keeps the just-recorded failure visible to the next managed
    turn.  It deliberately skips variant cooldown derivation, practice ranking,
    nightly-corpus matching, response-plan construction, selection receipts,
    and display-candidate revalidation.
    """

    event_id = str(current_event.get("event_id") or "").strip()
    try:
        root = repo.expanduser().resolve()
        state = (
            Path(state_dir).expanduser()
            if state_dir is not None
            else root / personalization_context.DEFAULT_STATE_RELATIVE
        )
        if not state.is_absolute():
            state = root / state
        state = state.resolve()
        state.relative_to(root)
        if not state.is_dir():
            raise personalization_context.ContextSliceError(
                "missing_projection"
            )
        current_date = personalization_context._event_date(current_event)
        with personalization_context._projection_lock(state):
            projection = personalization_context._load_hot_projection(
                state, current_date
            )
            old_manifest = projection["manifest"]
            learner = copy.deepcopy(projection["learner_state"])
            questions = copy.deepcopy(projection["question_index"])
            try:
                overlay = personalization_context.apply_event_to_projection(
                    current_event,
                    catalog=projection["catalog"],
                    learner_state=learner,
                    question_index=questions,
                    as_of=current_date,
                )
            except personalization_context.PersonalizationIndexError as exc:
                raise personalization_context.ContextSliceError(
                    "event_mapping_unavailable"
                ) from exc
            mapping = overlay["mapping"]
            signatures = (
                personalization_context.apply_event_to_signature_index(
                    copy.deepcopy(projection["failure_signatures"]),
                    current_event,
                    mapping,
                    as_of=current_date,
                )
            )
            changed = overlay.get("status") != "already_applied"
            if changed:
                new_manifest, _, _, _ = personalization_context._updated_manifest(
                    old_manifest,
                    learner,
                    questions,
                    signatures,
                    event_id,
                )
                new_manifest["last_personalization_selection"] = {
                    "event_id": event_id,
                    "mode": "intake_only",
                    "selected_practice": {
                        "status": "not_scheduled",
                        "schedule_allowed": False,
                        "reason_code": "intake_confirmation_defers_ranking",
                    },
                }
                personalization_context._persist_overlay(
                    state,
                    old_manifest=old_manifest,
                    new_manifest=new_manifest,
                    learner=learner,
                    questions=questions,
                    signatures=signatures,
                )
            else:
                new_manifest = old_manifest
            return {
                "schema": INTAKE_OVERLAY_SCHEMA,
                "status": "ready",
                "trigger_event_id": event_id,
                "projection_id": str(
                    new_manifest.get("runtime_projection_id")
                    or new_manifest.get("build_id")
                    or ""
                ),
                "manifest_sha256": personalization_context._sha256_bytes(
                    personalization_context._json_bytes(
                        new_manifest, pretty=True
                    )
                ),
                "update_mode": overlay.get("update_mode"),
                "candidate_ranking_count": 0,
                "selection_receipt_write_count": 0,
                "formal_write_count": 0,
            }
    except personalization_context.ContextSliceError as exc:
        return {
            "schema": INTAKE_OVERLAY_SCHEMA,
            "status": "unavailable",
            "reason_code": exc.reason_code,
            "trigger_event_id": event_id or None,
            "candidate_ranking_count": 0,
            "selection_receipt_write_count": 0,
            "formal_write_count": 0,
        }
    except Exception:
        return {
            "schema": INTAKE_OVERLAY_SCHEMA,
            "status": "unavailable",
            "reason_code": "internal_projection_error",
            "trigger_event_id": event_id or None,
            "candidate_ranking_count": 0,
            "selection_receipt_write_count": 0,
            "formal_write_count": 0,
        }


def record_study_answer(
    repo: str | Path,
    package: dict[str, Any],
    *,
    state_dir: str | Path | None = None,
    review_state_dir: str | Path | None = None,
    formal_projection_dir: str | Path | None = None,
    capture_hot_root: str | Path | None = None,
    max_bytes: int = DEFAULT_MAX_BYTES,
    personalization_mode: str = "verified",
    formalization_authorized: bool = False,
    authorization_intent_ref: str | None = None,
    authorization_intent_sha256: str | None = None,
    intake_only: bool = False,
    compact: bool = False,
) -> dict[str, Any]:
    raise PersonalizedStudyTurnError(
        "legacy_personalized_writer_retired_use_managed_current_question_turn"
    )

    root = Path(repo).expanduser().resolve()
    if any(
        not isinstance(value, bool)
        for value in (formalization_authorized, intake_only, compact)
    ):
        raise PersonalizedStudyTurnError("invalid_study_turn_mode")
    values = _validate_package(package)
    managed_intent: dict[str, Any] | None = None
    if formalization_authorized:
        managed_intent = _validate_managed_fast_intake_intent(
            root,
            values,
            intent_ref=authorization_intent_ref,
            intent_sha256=authorization_intent_sha256,
        )
    elif authorization_intent_ref is not None or authorization_intent_sha256 is not None:
        raise PersonalizedStudyTurnError(
            "managed_authorization_intent_without_authorization"
        )
    try:
        recorded = record_outcome_hot(
            root,
            **values,
            review_state_dir=review_state_dir,
            formal_projection_dir=formal_projection_dir,
        )
    except HotOutcomeError as exc:
        raise PersonalizedStudyTurnError(str(exc)) from exc
    event = recorded["event"]
    verification = recorded.get("verification") or {}
    commit_receipt_sha256 = recorded.get("commit_receipt_sha256")
    review_event_sha256 = verification.get("event_sha256")
    if (
        verification.get("status") != "verified"
        or not isinstance(commit_receipt_sha256, str)
        or not SHA256_RE.fullmatch(commit_receipt_sha256)
        or not isinstance(review_event_sha256, str)
        or not SHA256_RE.fullmatch(review_event_sha256)
        or verification.get("receipt_sha256") != commit_receipt_sha256
        or verification.get("event_id") != event.get("event_id")
        or int(recorded.get("formal_write_count", -1)) != 0
    ):
        raise PersonalizedStudyTurnError("review_commit_verification_failed")
    if event.get("first_result") in {"independent_correct", "fragile_correct"}:
        capture_transaction = {
            "schema": "ordinary_capture_hot_adapter_v1",
            "status": "NOT_ELIGIBLE",
            "created": False,
            "capture_id": None,
            "quality_status": "not_eligible",
            "receipt_sha256": None,
            "review_event_id": event.get("event_id"),
            "review_event_sha256": review_event_sha256,
            "review_commit_receipt_sha256": commit_receipt_sha256,
            "formalization_authorized": False,
            "formal_write_authorized": False,
            "formal_write_count": 0,
        }
    else:
        authorization_receipt_ref: str | None = None
        authorization_receipt_sha256: str | None = None
        if formalization_authorized:
            assert managed_intent is not None
            assert authorization_intent_ref is not None
            assert authorization_intent_sha256 is not None
            (
                authorization_receipt_ref,
                authorization_receipt_sha256,
                _,
            ) = _publish_managed_authorization_receipt(
                root,
                intent_ref=authorization_intent_ref,
                intent_sha256=authorization_intent_sha256,
                intent=managed_intent,
                event=event,
                event_sha256=review_event_sha256,
                review_commit_receipt_sha256=commit_receipt_sha256,
            )
        try:
            capture_transaction = capture_managed_ordinary_outcome(
                root,
                event_id=str(event.get("event_id") or ""),
                event_sha256=review_event_sha256,
                review_commit_receipt_sha256=commit_receipt_sha256,
                review_state_dir=review_state_dir,
                hot_root=capture_hot_root,
                formalization_authorized=formalization_authorized,
                authorization_receipt_ref=authorization_receipt_ref,
                authorization_receipt_sha256=authorization_receipt_sha256,
                include_diagnostics=not compact,
            )
        except OrdinaryCaptureHotError as exc:
            return {
                "schema": SCHEMA,
                "status": "incomplete_recoverable",
                "reason_code": f"ordinary_capture_failed:{exc}",
                "review_event_id": event.get("event_id"),
                "review_event_sha256": review_event_sha256,
                "review_commit_receipt_sha256": commit_receipt_sha256,
                "review_commit_verification": "verified",
                "capture_status": "required_but_uncommitted",
                "feedback_authorized": False,
                "retry_with_same_idempotency_key": True,
                "formalization_authorized": False,
                "formal_write_count": 0,
            }
        if capture_transaction.get("status") == "AUTHORIZATION_RECOVERY_REQUIRED":
            return {
                "schema": SCHEMA,
                "status": "incomplete_recoverable",
                "reason_code": "ordinary_capture_authorization_incomplete",
                "review_event_id": event.get("event_id"),
                "review_event_sha256": review_event_sha256,
                "review_commit_receipt_sha256": commit_receipt_sha256,
                "review_commit_verification": "verified",
                "capture_status": "captured_but_authorization_unverified",
                "capture_id": capture_transaction.get("capture_id"),
                "fact_receipt_sha256": capture_transaction.get(
                    "fact_receipt_sha256"
                ),
                "recovery_ticket": capture_transaction.get("recovery_ticket"),
                "feedback_authorized": False,
                "retry_with_same_idempotency_key": bool(
                    (
                        capture_transaction.get("recovery_ticket") or {}
                    ).get("retry_with_same_idempotency_key")
                ),
                "formalization_authorized": False,
                "formal_write_count": 0,
            }
    capture_status = str(capture_transaction.get("status") or "")
    joint = capture_transaction.get("capture_joint_verification") or {}
    capture_receipt_sha256 = capture_transaction.get("receipt_sha256")
    noncapture_verified = (
        capture_status == "NOT_ELIGIBLE"
        and capture_transaction.get("created") is False
        and event.get("first_result") in {"independent_correct", "fragile_correct"}
        and capture_transaction.get("formalization_authorized") is False
    )
    legacy_capture_verified = (
        capture_status in {"CAPTURED", "ALREADY_COMMITTED"}
        and capture_transaction.get("quality_status") == "captured_unconfirmed"
        and capture_transaction.get("review_event_id") == event.get("event_id")
        and capture_transaction.get("review_event_sha256")
        == review_event_sha256
        and capture_transaction.get("review_commit_receipt_sha256")
        == commit_receipt_sha256
        and isinstance(capture_transaction.get("capture_id"), str)
        and bool(capture_transaction.get("capture_id"))
        and isinstance(capture_receipt_sha256, str)
        and bool(SHA256_RE.fullmatch(capture_receipt_sha256))
        and joint.get("status") == "PASS"
        and joint.get("capture_id") == capture_transaction.get("capture_id")
        and joint.get("receipt_sha256") == capture_receipt_sha256
        and joint.get("formal_write_count") == 0
    )
    explicit_capture_verified = (
        capture_status in {"CAPTURED_AND_AUTHORIZED", "ALREADY_AUTHORIZED"}
        and capture_transaction.get("quality_status")
        == "awaiting_daily_curation"
        and capture_transaction.get("review_event_id") == event.get("event_id")
        and capture_transaction.get("review_event_sha256")
        == review_event_sha256
        and capture_transaction.get("review_commit_receipt_sha256")
        == commit_receipt_sha256
        and isinstance(capture_transaction.get("capture_id"), str)
        and bool(capture_transaction.get("capture_id"))
        and isinstance(capture_receipt_sha256, str)
        and bool(SHA256_RE.fullmatch(capture_receipt_sha256))
        and isinstance(capture_transaction.get("authorization_event_id"), str)
        and bool(capture_transaction.get("authorization_event_id"))
        and capture_transaction.get("authorization_receipt_sha256")
        == capture_receipt_sha256
        and capture_transaction.get("formalization_authorized") is True
    )
    capture_verified = noncapture_verified or (
        explicit_capture_verified
        if formalization_authorized
        else legacy_capture_verified
    )
    expected_authorization = bool(
        formalization_authorized and not noncapture_verified
    )
    if (
        not capture_verified
        or int(capture_transaction.get("formal_write_count", -1)) != 0
        or bool(capture_transaction.get("formalization_authorized"))
        != expected_authorization
    ):
        return {
            "schema": SCHEMA,
            "status": "incomplete_recoverable",
            "reason_code": "ordinary_capture_verification_failed",
            "review_event_id": event.get("event_id"),
            "review_event_sha256": review_event_sha256,
            "review_commit_receipt_sha256": commit_receipt_sha256,
            "review_commit_verification": "verified",
            "capture_status": "required_but_unverified",
            "feedback_authorized": False,
            "retry_with_same_idempotency_key": True,
            "formalization_authorized": False,
            "formal_write_count": 0,
        }
    if intake_only:
        personalization = _safe_intake_overlay(
            root,
            event,
            state_dir=state_dir,
        )
        display = {
            "status": "not_scheduled",
            "reason_code": "intake_confirmation_defers_ranking",
        }
    else:
        personalization = safe_context_slice(
            root,
            current_event=event,
            state_dir=state_dir,
            max_bytes=max_bytes,
            mode=personalization_mode,
        )
        display = _display_candidate(
            root,
            personalization,
            state_dir=state_dir,
        )
    result = {
        "schema": SCHEMA,
        "status": "recorded",
        "created": bool(recorded.get("created")),
        "review_event_id": event.get("event_id"),
        "source": event.get("source"),
        "first_result": event.get("first_result"),
        "choice_result": event.get("choice_result", "not_applicable"),
        "reasoning_result": event.get("reasoning_result", "not_observed"),
        "confidence": event.get("confidence"),
        "review_commit_verification": verification.get("status"),
        "review_event_sha256": review_event_sha256,
        "review_commit_receipt_sha256": recorded.get(
            "commit_receipt_sha256"
        ),
        "review_audit": "BOUNDED_RECEIPT_VERIFIED",
        "personalization_status": personalization.get("status"),
        "personalization_slice_id": personalization.get("slice_id"),
        "personalization_trigger_event_id": personalization.get(
            "trigger_event_id"
        ),
        "personalization_update_mode": personalization.get("update_mode"),
        "intake_only": intake_only,
        "capture_status": capture_status,
        "capture_created": bool(
            capture_transaction.get("capture_created")
            if "capture_created" in capture_transaction
            else capture_transaction.get("created")
        ),
        "capture_id": capture_transaction.get("capture_id"),
        "capture_quality_status": capture_transaction.get("quality_status"),
        "capture_receipt_sha256": capture_transaction.get("receipt_sha256"),
        "authorization_event_id": capture_transaction.get(
            "authorization_event_id"
        ),
        "authorization_receipt_sha256": capture_transaction.get(
            "authorization_receipt_sha256"
        ),
        "formalization_authorized": expected_authorization,
        "feedback_authorized": True,
        "formal_write_count": 0,
    }
    if compact:
        result["compact"] = True
        return result
    return {
        **result,
        "display_candidate": display,
        "personalization_overlay": personalization if intake_only else None,
        "capture_transaction": capture_transaction,
    }


def safe_record_study_answer(
    repo: str | Path,
    package: dict[str, Any],
    *,
    state_dir: str | Path | None = None,
    review_state_dir: str | Path | None = None,
    formal_projection_dir: str | Path | None = None,
    capture_hot_root: str | Path | None = None,
    max_bytes: int = DEFAULT_MAX_BYTES,
    personalization_mode: str = "verified",
    formalization_authorized: bool = False,
    authorization_intent_ref: str | None = None,
    authorization_intent_sha256: str | None = None,
    intake_only: bool = False,
    compact: bool = False,
) -> dict[str, Any]:
    try:
        return record_study_answer(
            repo,
            package,
            state_dir=state_dir,
            review_state_dir=review_state_dir,
            formal_projection_dir=formal_projection_dir,
            capture_hot_root=capture_hot_root,
            max_bytes=max_bytes,
            personalization_mode=personalization_mode,
            formalization_authorized=formalization_authorized,
            authorization_intent_ref=authorization_intent_ref,
            authorization_intent_sha256=authorization_intent_sha256,
            intake_only=intake_only,
            compact=compact,
        )
    except PersonalizedStudyTurnError as exc:
        return {
            "schema": SCHEMA,
            "status": "unavailable",
            "reason_code": str(exc),
            "formal_write_count": 0,
        }
    except Exception:
        return {
            "schema": SCHEMA,
            "status": "unavailable",
            "reason_code": "internal_study_turn_error",
            "formal_write_count": 0,
        }


def record_explicit_fast_intake(
    repo: str | Path,
    package: dict[str, Any],
    **kwargs: Any,
) -> dict[str, Any]:
    """Default compact entrypoint for a current-message fast-intake request."""

    raise PersonalizedStudyTurnError(
        "legacy_explicit_fast_intake_retired_use_managed_current_question_turn"
    )

    options = dict(kwargs)
    if any(
        key in options
        for key in ("formalization_authorized", "intake_only", "compact")
    ):
        raise PersonalizedStudyTurnError(
            "explicit_fast_intake_mode_cannot_be_overridden"
        )
    if not options.get("authorization_intent_ref") or not options.get(
        "authorization_intent_sha256"
    ):
        raise PersonalizedStudyTurnError(
            "managed_authorization_intent_required"
        )
    options.update(
        {
            "formalization_authorized": True,
            "intake_only": True,
            "compact": True,
        }
    )
    return safe_record_study_answer(repo, package, **options)


def _read_package_argument(source: str) -> dict[str, Any]:
    """Read one answer-safe package from a file, stdin, or inline JSON."""

    try:
        if source == "-":
            raw = sys.stdin.buffer.read()
        elif source.lstrip().startswith("{"):
            raw = source.encode("utf-8")
        else:
            raw = Path(source).read_bytes()
        package = json.loads(raw)
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise PersonalizedStudyTurnError("invalid_package_input") from exc
    if not isinstance(package, dict):
        raise PersonalizedStudyTurnError("invalid_package_input")
    return package


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo", type=Path, default=Path.cwd())
    parser.add_argument(
        "--package",
        required=False,
        help="JSON file path, '-' for stdin, or an inline JSON object",
    )
    parser.add_argument("--state-dir", type=Path)
    parser.add_argument("--review-state-dir", type=Path)
    parser.add_argument("--formal-projection-dir", type=Path)
    parser.add_argument("--capture-hot-root", type=Path)
    parser.add_argument("--max-bytes", type=int, default=DEFAULT_MAX_BYTES)
    parser.add_argument(
        "--personalization-mode",
        choices=["off", "shadow", "verified"],
        default="verified",
    )
    parser.add_argument(
        "--full",
        action="store_true",
        help="include bounded diagnostic subreceipts",
    )
    parser.add_argument(
        "--legacy-unconfirmed",
        action="store_true",
        help=argparse.SUPPRESS,
    )
    parser.add_argument(
        "--legacy-recover-existing-event",
        action="store_true",
        help="named recovery for one already committed canonical outcome",
    )
    parser.add_argument("--existing-event-id")
    parser.add_argument("--existing-event-sha256")
    parser.add_argument("--existing-review-commit-receipt-sha256")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    if args.legacy_unconfirmed:
        value = {
            "schema": SCHEMA,
            "status": "unavailable",
            "reason_code": "legacy_recovery_requires_named_existing_event",
            "formal_write_count": 0,
        }
    elif args.legacy_recover_existing_event:
        if not all(
            (
                args.existing_event_id,
                args.existing_event_sha256,
                args.existing_review_commit_receipt_sha256,
            )
        ):
            value = {
                "schema": SCHEMA,
                "status": "unavailable",
                "reason_code": "legacy_recovery_existing_event_binding_required",
                "formal_write_count": 0,
            }
        else:
            try:
                recovered = capture_managed_ordinary_outcome(
                    args.repo,
                    event_id=args.existing_event_id,
                    event_sha256=args.existing_event_sha256,
                    review_commit_receipt_sha256=(
                        args.existing_review_commit_receipt_sha256
                    ),
                    review_state_dir=args.review_state_dir,
                    hot_root=args.capture_hot_root,
                    formalization_authorized=False,
                    include_diagnostics=args.full,
                )
            except OrdinaryCaptureHotError as exc:
                value = {
                    "schema": SCHEMA,
                    "status": "unavailable",
                    "reason_code": f"legacy_recovery_failed:{exc}",
                    "formal_write_count": 0,
                }
            else:
                value = {
                    "schema": SCHEMA,
                    "status": "legacy_recovery_complete",
                    "outcome_write_count": 0,
                    "capture_write_count": int(
                        bool(recovered.get("capture_created"))
                    ),
                    "authorization_write_count": 0,
                    "formal_write_count": 0,
                    "capture_transaction": recovered,
                }
    else:
        # The generic CLI is deliberately read-only.  Live recording must
        # enter through the rich managed harness, which owns the current raw
        # message and can publish a content-addressed authorization intent.
        if args.package is not None:
            try:
                _validate_package(_read_package_argument(args.package))
            except PersonalizedStudyTurnError:
                value = {
                    "schema": SCHEMA,
                    "status": "unavailable",
                    "reason_code": "invalid_package_input",
                    "outcome_write_count": 0,
                    "capture_write_count": 0,
                    "authorization_write_count": 0,
                    "formal_write_count": 0,
                }
            else:
                value = {
                    "schema": SCHEMA,
                    "status": "no_write_default",
                    "reason_code": "managed_entrypoint_required",
                    "outcome_write_count": 0,
                    "capture_write_count": 0,
                    "authorization_write_count": 0,
                    "formal_write_count": 0,
                }
        else:
            value = {
                "schema": SCHEMA,
                "status": "no_write_default",
                "reason_code": "managed_entrypoint_required",
                "outcome_write_count": 0,
                "capture_write_count": 0,
                "authorization_write_count": 0,
                "formal_write_count": 0,
            }
    print(json.dumps(value, ensure_ascii=False, sort_keys=True))
    return 0 if value.get("status") in {
        "no_write_default",
        "legacy_recovery_complete",
    } else 1


if __name__ == "__main__":
    raise SystemExit(main())
