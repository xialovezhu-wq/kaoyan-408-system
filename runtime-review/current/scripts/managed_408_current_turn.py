#!/usr/bin/env python3
"""Bounded current-question buffering and terminal Capture finalization.

Non-terminal answers and teaching turns update only one private same-item
draft.  A terminal transaction seals the complete package, creates and reopens
exactly one Capture bound to that final package, commits the canonical learning
receipts, and only then publishes a successor, holds, or closes the session.
No path reads personalization, related questions, variants, Luna state, or a
formal table.
"""

from __future__ import annotations

from study_commit_followup_408 import study_commit_boundary

import argparse
import datetime as dt
import hashlib
import json
import os
import re
import stat
import sys
from pathlib import Path
from typing import Any, Callable
from zoneinfo import ZoneInfo

import capture_hot_writer_408 as capture_hot
import conversation_package_408 as conversation_package
import current_question_context_408 as context_model
import current_question_evidence_408 as private_evidence
import intake_fact_capture_408 as capture_model
import review_outcome_hot_408 as review_outcome

import runtime_paths_408 as _paths


TURN_SCHEMA = "managed-408-current-question-turn-v1"
RECOVERY_RESULT_SCHEMA = "managed-408-current-question-recovery-result-v1"
NEXT_ITEM_SCHEMA = "managed-408-next-item-v1"
CONTINUE_CURRENT_SCHEMA = "managed-408-continue-current-v1"
ANSWER_AND_NEXT_SCHEMA = "managed-408-answer-current-and-next-v1"
DEFAULT_NAVIGATION_POLICY = "publish_successor"
NAVIGATION_POLICIES = frozenset(
    {DEFAULT_NAVIGATION_POLICY, "hold_current"}
)
MANAGED_CHOICES = frozenset({"A", "B", "C", "D", "BLANK", "UNCERTAIN"})
MAX_FEEDBACK_BYTES = 16 * 1024
MAX_ATTACHMENTS_JSON_BYTES = 1024 * 1024
SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
CAPTURE_HOT_DIRNAME = ".capture-hot-writer-v1"
BUFFERING_STATUS = conversation_package.BUFFERING_STATUS
CANONICAL_PROMPT_LEVEL_MAP = {
    "none": "none",
    "L1": "minimal",
    "L2": "progressive",
    "L3": "progressive",
    "L4": "progressive",
    "L5": "full",
}


class ManagedCurrentTurnError(RuntimeError):
    """One bounded current-question transaction was invalid."""


FaultInjector = Callable[[str], None]


def _normalize_navigation_policy(value: object) -> str:
    policy = str(value or "").strip()
    if policy not in NAVIGATION_POLICIES:
        raise ManagedCurrentTurnError("navigation_policy_invalid")
    return policy


def _operation_navigation_policy(
    operation: dict[str, Any], *, expected: str | None = None
) -> str:
    prepared = operation.get("prepared")
    if not isinstance(prepared, dict):
        raise ManagedCurrentTurnError("answer_operation_prepared_missing")
    # Operations written before navigation-policy support keep their original
    # publish-on-success behavior. Every new operation freezes the explicit
    # value in prepared metadata as well as in the input hash.
    policy = _normalize_navigation_policy(
        prepared.get("navigation_policy", DEFAULT_NAVIGATION_POLICY)
    )
    if expected is not None and policy != expected:
        raise ManagedCurrentTurnError(
            "answer_operation_navigation_policy_drifted"
        )
    return policy


def _result_navigation_policy(result: dict[str, Any]) -> str:
    # Completed legacy operations did not expose the field and therefore mean
    # the historical default. New results always carry the frozen value.
    return _normalize_navigation_policy(
        result.get("navigation_policy", DEFAULT_NAVIGATION_POLICY)
    )


def _conversation_package_root_for_runtime(
    private_root: str | Path | None,
) -> Path | None:
    if os.environ.get("KAOYAN_408_CONVERSATION_PACKAGE_ROOT"):
        return None
    if private_root is None:
        return None
    candidate = Path(private_root).expanduser().resolve()
    production_boundary = (_paths.codex_home()).resolve()
    try:
        candidate.relative_to(production_boundary)
        return None
    except ValueError:
        return candidate.parent / "kaoyan-408-conversation-packages"


def _json_bytes(value: Any, *, pretty: bool = False) -> bytes:
    options: dict[str, Any] = {"ensure_ascii": False, "sort_keys": True}
    if pretty:
        options["indent"] = 2
    else:
        options["separators"] = (",", ":")
    return (json.dumps(value, **options) + "\n").encode("utf-8")


def _sha256(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


def _legacy_source_identity_fallback(
    repo: Path,
    *,
    display_receipt: dict[str, Any],
    prepared_pack: Any,
    allow_reconstructed_binding: bool = False,
) -> dict[str, str] | None:
    """Resolve one invalid legacy label from an exact live schedule binding.

    A content-addressed display receipt is necessary but not sufficient.  The
    embedded scheduling binding must equal the binding recomputed from the
    still-bound immutable session/pack, and that binding must expose one legal
    scalar formal-node ID.  Every ambiguous or drifted legacy case keeps the
    stable ``source_id_invalid`` fail-closed surface.
    """

    displayed_value = display_receipt.get("scheduling_binding")
    if displayed_value is not None and not isinstance(displayed_value, dict):
        raise ManagedCurrentTurnError("source_id_invalid")
    displayed = displayed_value if isinstance(displayed_value, dict) else None
    if displayed is None and not allow_reconstructed_binding:
        return None
    session_id = str(display_receipt.get("session_id") or "")
    item_id = str(display_receipt.get("item_id") or "")
    try:
        live = prepared_pack.scheduling_binding_for_item(
            repo, session_id, item_id
        )
    except Exception as exc:
        raise ManagedCurrentTurnError("source_id_invalid") from exc
    if not isinstance(live, dict):
        raise ManagedCurrentTurnError("source_id_invalid")
    binding = displayed if displayed is not None else live
    raw_source_label = str(binding.get("source_id") or "").strip()
    if context_model.is_stable_source_id(raw_source_label):
        return None
    formal_node_id = str(binding.get("formal_node_id") or "").strip()
    binding_sha = str(binding.get("binding_sha256") or "")
    if (
        (displayed is not None and live != displayed)
        or live.get("session_id") != session_id
        or live.get("item_id") != item_id
        or live.get("surface_sha256")
        != display_receipt.get("surface_sha256")
        or not raw_source_label
        or not context_model.FORMAL_ID_RE.fullmatch(formal_node_id)
        or not SHA256_RE.fullmatch(binding_sha)
    ):
        raise ManagedCurrentTurnError("source_id_invalid")
    return {
        "raw_source_label": raw_source_label,
        "machine_source_id": formal_node_id,
        "normalization_basis": context_model.SOURCE_ID_FALLBACK_BASIS,
        "scheduling_binding_sha256": binding_sha,
        "binding_verification": (
            "verified_embedded_scheduling_binding"
            if displayed is not None
            else "reconstructed_legacy_display_session_pack_binding"
        ),
    }


def _prepare_current_turn_with_source_compatibility(
    repo: Path,
    *,
    display_receipt: dict[str, Any],
    display_receipt_locator: str,
    choice: str,
    confidence: str,
    prompt_level: str,
    request_id: str,
    event_time: str,
    display_surface_sha256: str,
    interaction_trace: dict[str, Any],
    semantic_assessment: dict[str, Any],
    private_root: str | Path | None,
    prepared_pack: Any,
) -> tuple[dict[str, Any], dict[str, str] | None]:
    kwargs = {
        "session_id": str(display_receipt.get("session_id") or ""),
        "item_id": str(display_receipt.get("item_id") or ""),
        "choice": choice,
        "confidence": confidence,
        "prompt_level": prompt_level,
        "request_id": request_id,
        "event_time": event_time,
        "display_surface_sha256": display_surface_sha256,
        "display_receipt_locator": display_receipt_locator,
        "interaction_trace": interaction_trace,
        "semantic_assessment": semantic_assessment,
        "include_private_feedback": True,
        "private_root": private_root,
    }

    def prepare() -> dict[str, Any]:
        return prepared_pack.prepare_current_turn(repo, **kwargs)

    displayed_binding = display_receipt.get("scheduling_binding")
    if displayed_binding is None:
        try:
            return prepare(), None
        except Exception as exc:
            if str(exc) != "source_id_invalid":
                raise
        fallback = _legacy_source_identity_fallback(
            repo,
            display_receipt=display_receipt,
            prepared_pack=prepared_pack,
            allow_reconstructed_binding=True,
        )
        if fallback is None:
            raise ManagedCurrentTurnError("source_id_invalid")
    else:
        fallback = _legacy_source_identity_fallback(
            repo,
            display_receipt=display_receipt,
            prepared_pack=prepared_pack,
        )
    try:
        if fallback is None:
            return prepare(), None
        with context_model.verified_source_identity_fallback(
            raw_source_label=fallback["raw_source_label"],
            machine_source_id=fallback["machine_source_id"],
            scheduling_binding_sha256=fallback[
                "scheduling_binding_sha256"
            ],
            binding_verification=fallback["binding_verification"],
        ):
            return prepare(), fallback
    except Exception as exc:
        if str(exc) == "source_id_invalid":
            raise ManagedCurrentTurnError("source_id_invalid") from exc
        raise


def _stored_source_identity_fallback(
    stored: object,
) -> dict[str, str] | None:
    """Validate a fallback already frozen in a private prepared operation.

    Identity is resolved exactly once, before that operation is written.  A
    crash replay validates the frozen decision but does not reopen scheduling
    state or derive a different identity.
    """

    if stored is None:
        return None
    if not isinstance(stored, dict) or set(stored) != {
        "raw_source_label",
        "machine_source_id",
        "normalization_basis",
        "scheduling_binding_sha256",
        "binding_verification",
    }:
        raise ManagedCurrentTurnError("source_id_invalid")
    normalized = {key: str(value or "") for key, value in stored.items()}
    if (
        context_model.is_stable_source_id(normalized["raw_source_label"])
        or not context_model.FORMAL_ID_RE.fullmatch(
            normalized["machine_source_id"]
        )
        or normalized["normalization_basis"]
        != context_model.SOURCE_ID_FALLBACK_BASIS
        or not SHA256_RE.fullmatch(
            normalized["scheduling_binding_sha256"]
        )
        or normalized["binding_verification"]
        not in {
            "verified_embedded_scheduling_binding",
            "reconstructed_legacy_display_session_pack_binding",
        }
    ):
        raise ManagedCurrentTurnError("source_id_invalid")
    return normalized


def _validated_prepared_context(
    raw_context: dict[str, Any],
    source_identity_fallback: object,
) -> dict[str, Any]:
    fallback = _stored_source_identity_fallback(source_identity_fallback)
    if fallback is None:
        return context_model.validate_context(raw_context)
    with context_model.verified_source_identity_fallback(
        raw_source_label=fallback["raw_source_label"],
        machine_source_id=fallback["machine_source_id"],
        scheduling_binding_sha256=fallback[
            "scheduling_binding_sha256"
        ],
        binding_verification=fallback["binding_verification"],
    ):
        return context_model.validate_context(raw_context)


def _frozen_feedback(value: object) -> tuple[str, str]:
    if not isinstance(value, str) or not value.strip():
        raise ManagedCurrentTurnError("feedback_text_required")
    body = value.strip()
    raw = body.encode("utf-8")
    if len(raw) > MAX_FEEDBACK_BYTES:
        raise ManagedCurrentTurnError("feedback_text_too_large")
    return body, _sha256(raw)


def _fault(injector: FaultInjector | None, phase: str) -> None:
    if injector is not None:
        injector(phase)


def _validate_controlled_assessment(
    context: dict[str, Any], private_evaluation: dict[str, Any]
) -> None:
    """Bind the public controlled verdict to the private grader capsule."""

    if not isinstance(private_evaluation, dict) or not private_evaluation:
        raise ManagedCurrentTurnError("private_grader_evidence_required")
    learner_choice = str(
        context["learner_evidence"].get("choice") or ""
    ).strip().upper()
    declared = str(context["assessment"].get("choice_result") or "")
    correct_option = str(
        private_evaluation.get("correct_option") or ""
    ).strip().upper()
    if learner_choice in {"BLANK", "UNCERTAIN", "不确定", "空白", ""}:
        if declared not in {"blank", "uncertain", "not_applicable"}:
            raise ManagedCurrentTurnError(
                "controlled_assessment_blank_choice_drifted"
            )
    elif learner_choice in {"A", "B", "C", "D"} and correct_option in {
        "A",
        "B",
        "C",
        "D",
    }:
        derived = "correct" if learner_choice == correct_option else "incorrect"
        if declared != derived:
            raise ManagedCurrentTurnError(
                "controlled_assessment_private_grader_drifted"
            )

    grader_result = str(
        private_evaluation.get("grader_result") or ""
    ).strip().lower()
    if grader_result:
        expected = {
            # The private grader owns option correctness only.  Root Sol's
            # frozen reasoning judgment may correctly downgrade a matching
            # option to partial; a choice-only grader must never overwrite it.
            "correct": {
                "independent_correct",
                "fragile_correct",
                "partial",
            },
            "independent_correct": {"independent_correct"},
            "fragile_correct": {"fragile_correct"},
            "partial": {"partial"},
            "wrong": {"wrong"},
            "incorrect": {"wrong"},
            "uncertain": {"uncertain"},
            "blank": {"uncertain"},
        }.get(grader_result)
        if expected is not None and context["classification"]["teaching_result"] not in expected:
            raise ManagedCurrentTurnError(
                "controlled_assessment_grader_result_drifted"
            )


def _capture_paths(repo: Path) -> tuple[Path, Path, Path]:
    root = capture_model.capture_root(repo)
    return root / "events.jsonl", root / "state.json", root / CAPTURE_HOT_DIRNAME


def _canonical_prompt_level(prompt_level: str) -> str:
    mapped = CANONICAL_PROMPT_LEVEL_MAP.get(prompt_level)
    if mapped is None:
        raise ManagedCurrentTurnError("prompt_level_mapping_missing")
    return mapped


def _direct_user_facts(context: dict[str, Any]) -> dict[str, str]:
    assessment = context["assessment"]
    provenance = assessment["first_break_provenance"]
    first_break = assessment["first_break"]
    if provenance in {"user_report", "visible_evidence"}:
        result = {
            "user_error_entry": first_break,
            "user_error_provenance": provenance,
            "observed_at": context["event_time"],
        }
    else:
        result = {
            "user_error_entry": "未观察到",
            "user_error_provenance": "not_observed",
            "observed_at": context["event_time"],
        }
    if assessment.get("first_action") is not None:
        result["first_action"] = str(assessment["first_action"])
        result["first_action_provenance"] = str(
            assessment["first_action_provenance"]
        )
    return result


def _capture_payload(
    context: dict[str, Any], evidence_receipt: dict[str, Any]
) -> dict[str, Any]:
    locator = str(evidence_receipt.get("locator") or "")
    package_sha = str(evidence_receipt.get("canonical_sha256") or "")
    if (
        not locator.startswith(conversation_package.LOCATOR_PREFIX)
        or not SHA256_RE.fullmatch(package_sha)
    ):
        raise ManagedCurrentTurnError("conversation_package_receipt_invalid")
    identity = {
        "status": "unknown",
        "formal_id": "",
        "mode": "unknown",
        "basis": "正式身份留待日终 Sol 重新核验",
    }
    source_facts = {
        "subject": "未确认",
        "question_type": "当前单题首答",
        "source_id": context["source_id"],
        "details_id": context["context_id"],
    }
    missing: list[str] = ["formal_identity", "main_knowledge"]
    if _direct_user_facts(context)["user_error_provenance"] == "not_observed":
        missing.append("user_error_entry")
    anchor = context["source_id"]
    raw = {
        "schema": capture_model.SCHEMA,
        "study_date": context["study_date"],
        "timezone": context_model.TIMEZONE,
        "idempotency_key": (
            "managed-terminal:"
            f"{context['session_id']}:{context['item_id']}:"
            "final-conversation-package:v2"
        ),
        "stable_evidence_refs": [
            {
                "kind": capture_model.CONVERSATION_PACKAGE_REF_KIND,
                "locator": locator,
                "sha256": package_sha,
            }
        ],
        "source_facts": source_facts,
        "user_facts": _direct_user_facts(context),
        "identity_hint": identity,
        "answer_safe_context_anchor": (
            f"当前题 {context['source_id']} 的 {anchor} 完整会话证据"
        ),
        "missing_fields": sorted(set(missing)),
        "formalization_authorized": True,
        "authorization_policy": (
            capture_model.CONVERSATION_PACKAGE_PENDING_POLICY
        ),
    }
    return capture_model._validate_capture_payload(raw)


def _conversation_package_source(
    context: dict[str, Any],
    private_evaluation: dict[str, Any],
    *,
    question_mode: str,
) -> tuple[dict[str, Any], list[str]]:
    missing_fields = list(context.get("missing_fields") or [])
    if not context.get("source_stable"):
        missing_fields.append("stable_source_identity")
    source = {
        "source_kind": context.get("source"),
        "source_binding_sha256": context.get("source_binding_sha256"),
        "question": context.get("question_evidence"),
        "learner_evidence": context.get("learner_evidence"),
        "assessment": context.get("assessment"),
        "evaluation_evidence": private_evaluation,
        "question_mode": question_mode,
    }
    provenance = context.get("source_identity_provenance")
    if isinstance(provenance, dict):
        source["source_identity_provenance"] = dict(provenance)
    return source, sorted(set(missing_fields))


def _write_current_conversation_package(
    context: dict[str, Any],
    *,
    private_evaluation: dict[str, Any],
    interaction_trace: object | None,
    feedback_text: str,
    attachments: list[dict[str, Any]] | None,
    question_mode: str,
    seal: bool,
    private_root: str | Path | None,
) -> dict[str, Any]:
    source, missing_fields = _conversation_package_source(
        context, private_evaluation, question_mode=question_mode
    )
    return conversation_package.record_package(
        study_date=context["study_date"],
        session_id=context.get("session_id"),
        item_id=context.get("item_id"),
        source_id=context["source_id"],
        source=source,
        conversation=interaction_trace,
        frozen_feedback=feedback_text,
        attachments=attachments,
        missing_fields=missing_fields,
        seal=seal,
        private_root=_conversation_package_root_for_runtime(private_root),
    )


def _terminal_resolved(context: dict[str, Any]) -> bool:
    classification = context["classification"]
    return bool(
        classification.get(
            "terminal_resolved",
            classification.get("teaching_result")
            in {"independent_correct", "fragile_correct"},
        )
    )


def _verify_frozen_terminal_verdict(
    record: dict[str, Any],
    *,
    context: dict[str, Any],
    interaction_trace: dict[str, Any],
    feedback_text: str,
) -> None:
    verdict = record.get("terminal_verdict")
    if not isinstance(verdict, dict):
        raise ManagedCurrentTurnError("terminal_verdict_missing")
    canonical = json.dumps(
        verdict,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    verdict_sha = hashlib.sha256(canonical).hexdigest()
    trace_sha = str(interaction_trace.get("full_trace_sha256") or "")
    feedback_sha = hashlib.sha256(feedback_text.encode("utf-8")).hexdigest()
    classification = context["classification"]
    assessment = context["assessment"]
    if (
        record.get("terminal_verdict_sha256") != verdict_sha
        or record.get("interaction_trace_sha256") != trace_sha
        or record.get("feedback_sha256") != feedback_sha
        or verdict.get("semantic_assessment_sha256")
        != record.get("semantic_assessment_sha256")
        or verdict.get("interaction_trace_sha256") != trace_sha
        or verdict.get("feedback_sha256") != feedback_sha
        or verdict.get("choice_result") != assessment["choice_result"]
        or verdict.get("reasoning_result") != assessment["reasoning_result"]
        or verdict.get("prompt_level") != assessment["prompt_level"]
        or verdict.get("teaching_result")
        != classification["teaching_result"]
        or verdict.get("terminal_resolved")
        != classification.get("terminal_resolved")
        or verdict.get("capture_required")
        != classification.get("capture_required")
    ):
        raise ManagedCurrentTurnError("terminal_verdict_binding_drifted")


def _reopen_final_conversation_package(
    context: dict[str, Any],
    receipt: dict[str, Any],
    *,
    private_root: str | Path | None,
) -> dict[str, Any]:
    locator = str(receipt.get("locator") or "")
    expected_sha = str(receipt.get("canonical_sha256") or "")
    if (
        not locator.startswith(conversation_package.LOCATOR_PREFIX)
        or not SHA256_RE.fullmatch(expected_sha)
    ):
        raise ManagedCurrentTurnError("terminal_conversation_package_receipt_invalid")
    opened = conversation_package.read_package(
        locator,
        private_root=_conversation_package_root_for_runtime(private_root),
    )
    manifest = opened.get("manifest") or {}
    stored_receipt = opened.get("receipt") or {}
    source = opened.get("source") or {}
    canonical_material = {
        key: value
        for key, value in manifest.items()
        if key
        not in {
            "schema_version",
            "revision",
            "attachments",
            "canonical_sha256",
        }
    }
    canonical_material["schema_version"] = conversation_package.SCHEMA
    recomputed_sha = _sha256(_json_bytes(canonical_material))
    if (
        manifest.get("sealed") is not True
        or stored_receipt.get("sealed") is not True
        or stored_receipt.get("status")
        != conversation_package.PENDING_FORMAL_STATUS
        or manifest.get("canonical_sha256") != expected_sha
        or stored_receipt.get("canonical_sha256") != expected_sha
        or recomputed_sha != expected_sha
        or source.get("session_id") != context.get("session_id")
        or source.get("item_id") != context.get("item_id")
        or source.get("source_id") != context.get("source_id")
    ):
        raise ManagedCurrentTurnError("terminal_conversation_package_binding_drifted")
    return {
        "locator": locator,
        "canonical_sha256": expected_sha,
        "manifest_sha256": expected_sha,
        "evidence_status": "ready",
        "sealed": True,
    }


def _reopen_verified_capture_binding(
    repo: Path,
    *,
    payload: dict[str, Any],
    capture: dict[str, Any],
    final_package: dict[str, Any],
) -> dict[str, Any]:
    payload_sha = str(
        capture.get("payload_sha256")
        or capture.get("capture_content_sha256")
        or ""
    )
    capture_id = str(capture.get("capture_id") or "")
    if not capture_id or not SHA256_RE.fullmatch(payload_sha):
        raise ManagedCurrentTurnError("terminal_capture_receipt_invalid")
    try:
        binding = capture_model.reopen_capture_binding(
            capture_id,
            expected_payload_sha256=payload_sha,
            expected_idempotency_key=str(payload["idempotency_key"]),
            repo_root=repo,
        )
    except (capture_model.CaptureError, OSError) as exc:
        raise ManagedCurrentTurnError(
            f"terminal_capture_reopen_failed:{exc}"
        ) from exc
    ledger_path, state_path, hot_root = _capture_paths(repo)
    try:
        durable = capture_hot.verify_joint_commit(
            ledger_path,
            state_path,
            hot_root,
            idempotency_key=str(payload["idempotency_key"]),
        )
    except (capture_hot.CaptureHotWriterError, OSError) as exc:
        raise ManagedCurrentTurnError(
            f"terminal_capture_durable_receipt_reopen_failed:{exc}"
        ) from exc
    expected_ref = {
        "kind": capture_model.CONVERSATION_PACKAGE_REF_KIND,
        "locator": final_package["locator"],
        "sha256": final_package["canonical_sha256"],
    }
    if (
        binding.get("capture_id") != capture_id
        or binding.get("payload_sha256") != payload_sha
        or binding.get("idempotency_key") != payload["idempotency_key"]
        or binding.get("stable_evidence_refs") != [expected_ref]
        or binding.get("quality_status") != capture_model.PENDING_FORMAL_STATUS
        or binding.get("formal_write_count") != 0
        or not SHA256_RE.fullmatch(str(binding.get("event_sha256") or ""))
        or durable.get("status") != "PASS"
        or durable.get("capture_id") != capture_id
        or durable.get("quality_status")
        != capture_model.PENDING_FORMAL_STATUS
        or durable.get("receipt_sha256") != capture.get("receipt_sha256")
        or not SHA256_RE.fullmatch(
            str(durable.get("joint_manifest_sha256") or "")
        )
    ):
        raise ManagedCurrentTurnError("terminal_capture_binding_not_verified")
    return {
        **binding,
        "durable_receipt_sha256": durable["receipt_sha256"],
        "durable_event_sha256": durable["event_sha256"],
        "durable_joint_manifest_sha256": durable[
            "joint_manifest_sha256"
        ],
        "durable_bounded_manifest_sha256": durable[
            "bounded_manifest_sha256"
        ],
        "durable_status": "PASS",
    }


def _record_ordinary_first(
    repo: Path,
    context: dict[str, Any],
    *,
    evidence_manifest_sha256: str | None,
) -> dict[str, Any]:
    assessment = context["assessment"]
    classification = context["classification"]
    canonical_source = (
        context["source"]
        if context["source"] in {"daily_practice", "evening_d0"}
        else "daily_practice"
    )
    choice_result = assessment["choice_result"]
    if choice_result == "blank":
        choice_result = "uncertain"
    prompt_level = _canonical_prompt_level(assessment["prompt_level"])
    first_result = classification["canonical_first_result"]
    fragile_override = classification["fragile_override"]
    if (
        classification["teaching_result"] == "fragile_correct"
        and prompt_level != "none"
    ):
        first_result = "partial"
        fragile_override = False
    try:
        return review_outcome.record_outcome_hot(
            repo,
            source=canonical_source,
            session_id=context["session_id"],
            item_id=context["item_id"],
            source_id=context["source_id"],
            mechanism_key=context["source_id"],
            knowledge_point=context["source_id"],
            first_result=first_result,
            choice_result=choice_result,
            reasoning_result=assessment["reasoning_result"],
            confidence=assessment["confidence"],
            prompt_level=prompt_level,
            first_break=assessment["first_break"],
            first_break_provenance=assessment["first_break_provenance"],
            event_time=context["event_time"],
            observed_date=context["study_date"],
            idempotency_key=context["idempotency_key"],
            formal_node_id=None,
            review_unit_id=None,
            fragile_override=fragile_override,
            managed_user_reply_binding_sha256=evidence_manifest_sha256,
            presentation_kind="current_question",
            current_question_minimal=True,
        )
    except review_outcome.HotOutcomeError as exc:
        raise ManagedCurrentTurnError(f"first_answer_commit_failed:{exc}") from exc


def _morning_args(context: dict[str, Any]) -> argparse.Namespace:
    classification = context["classification"]
    assessment = context["assessment"]
    learner_choice = str(context["learner_evidence"].get("choice") or "BLANK").upper()
    if learner_choice in {"UNCERTAIN", "不确定", "空白"}:
        learner_choice = "BLANK"
    surface_sha = str(
        context["question_evidence"].get("public_surface_sha256") or ""
    )
    if not SHA256_RE.fullmatch(surface_sha):
        raise ManagedCurrentTurnError("morning_surface_sha256_required")
    teaching_result = classification["teaching_result"]
    session_result = "blank" if teaching_result == "uncertain" else teaching_result
    choice_result = assessment["choice_result"]
    if choice_result == "blank":
        choice_result = "uncertain"
    prompt_level = _canonical_prompt_level(assessment["prompt_level"])
    return argparse.Namespace(
        repo=str(context["_repo"]),
        session=context["session_id"],
        item=context["item_id"],
        display_surface_sha256=surface_sha,
        learner_choice=learner_choice,
        result=session_result,
        choice_result=choice_result,
        reasoning_result=assessment["reasoning_result"],
        confidence=assessment["confidence"],
        response_seconds=None,
        prompt_level=prompt_level,
        first_break=assessment["first_break"],
        first_break_provenance=assessment["first_break_provenance"],
        feedback_type="current_question_feedback",
        current_only=True,
        _current_question_managed_v1=True,
        _current_question_capsule_verified=True,
        _current_question_controlled_choice_result=choice_result,
        _current_question_receipt_only=True,
        _current_question_feedback_sha256=context.get("_feedback_sha256"),
        managed_user_reply_binding_sha256=context.get(
            "_evidence_manifest_sha256"
        ),
    )


def _record_morning_first(
    repo: Path,
    context: dict[str, Any],
    *,
    evidence_manifest_sha256: str | None,
) -> dict[str, Any]:
    try:
        import morning_review_session as morning_session
    except ImportError as exc:
        raise ManagedCurrentTurnError("morning_session_module_unavailable") from exc
    internal = {
        **context,
        "_repo": repo,
        "_evidence_manifest_sha256": evidence_manifest_sha256,
    }
    try:
        return morning_session.command_record_first(_morning_args(internal))
    except morning_session.SessionError as exc:
        raise ManagedCurrentTurnError(f"first_answer_commit_failed:{exc}") from exc


def _record_first_answer(
    repo: Path,
    context: dict[str, Any],
    *,
    evidence_manifest_sha256: str | None,
) -> dict[str, Any]:
    if context["source"] == "morning_review":
        return _record_morning_first(
            repo, context, evidence_manifest_sha256=evidence_manifest_sha256
        )
    return _record_ordinary_first(
        repo, context, evidence_manifest_sha256=evidence_manifest_sha256
    )


def _validate_first_answer_receipts(
    context: dict[str, Any], result: dict[str, Any]
) -> None:
    if not isinstance(result, dict):
        raise ManagedCurrentTurnError("first_answer_commit_receipt_missing")
    if context["source"] == "morning_review":
        required = (
            "review_commit_receipt_sha256",
            "session_binding_receipt_sha256",
        )
    else:
        required = ("commit_receipt_sha256",)
    for field in required:
        if not SHA256_RE.fullmatch(str(result.get(field) or "")):
            raise ManagedCurrentTurnError(
                f"first_answer_commit_receipt_invalid:{field}"
            )
    if context["source"] == "morning_review":
        _validate_event_specific_session_commit(
            result.get("session_first_commit"), "session_first_commit"
        )
        _validate_event_specific_session_commit(
            result.get("session_binding_commit"), "session_binding_commit"
        )
        if (
            result["session_binding_commit"].get("receipt_sha256")
            != result.get("session_binding_receipt_sha256")
        ):
            raise ManagedCurrentTurnError(
                "session_binding_receipt_hash_drifted"
            )
        receipt_verification = result.get("review_receipt_verification")
        if (
            not isinstance(receipt_verification, dict)
            or receipt_verification.get("status") != "verified"
            or receipt_verification.get("receipt_sha256")
            != result.get("review_commit_receipt_sha256")
        ):
            raise ManagedCurrentTurnError(
                "review_receipt_verification_binding_failed"
            )
    verification = (
        result.get("review_verification")
        if context["source"] == "morning_review"
        else result.get("verification")
    )
    if not isinstance(verification, dict) or verification.get("status") not in {
        "verified",
        "pass",
    }:
        raise ManagedCurrentTurnError("first_answer_commit_verification_failed")
    if (
        context["source"] == "morning_review"
        and verification.get("receipt_sha256")
        != result.get("review_commit_receipt_sha256")
    ):
        raise ManagedCurrentTurnError("review_commit_verification_binding_failed")


def _validate_event_specific_session_commit(
    commit: object, label: str
) -> None:
    if not isinstance(commit, dict):
        raise ManagedCurrentTurnError(f"{label}_missing")
    receipt_sha = str(commit.get("receipt_sha256") or "")
    verification = commit.get("verification")
    if (
        commit.get("status") not in {"pass", "committed", "already_committed"}
        or not SHA256_RE.fullmatch(receipt_sha)
        or not isinstance(verification, dict)
        or verification.get("status") != "pass"
        or verification.get("receipt_sha256") != receipt_sha
    ):
        raise ManagedCurrentTurnError(f"{label}_verification_failed")


def _validate_session_feedback_receipt(result: dict[str, Any] | None) -> None:
    if not isinstance(result, dict):
        raise ManagedCurrentTurnError("session_feedback_receipt_missing")
    commit = result.get("session_commit")
    _validate_event_specific_session_commit(commit, "session_feedback_commit")


def _mark_morning_feedback(
    repo: Path,
    context: dict[str, Any],
    first_answer: dict[str, Any],
    *,
    feedback_sha256: str,
) -> dict[str, Any] | None:
    if context["source"] != "morning_review":
        return None
    try:
        import morning_review_session as morning_session
    except ImportError as exc:
        raise ManagedCurrentTurnError(
            "session_feedback_receipt_failed:module_unavailable"
        ) from exc
    try:
        internal = {
            **context,
            "_repo": repo,
            "_evidence_manifest_sha256": None,
            "_feedback_sha256": feedback_sha256,
        }
        return morning_session.command_reveal_feedback(
            _morning_args(internal),
            review_receipt_sha256=first_answer.get(
                "review_commit_receipt_sha256"
            ),
            session_binding_receipt_sha256=first_answer.get(
                "session_binding_receipt_sha256"
            ),
        )
    except morning_session.SessionError as exc:
        raise ManagedCurrentTurnError(
            f"session_feedback_receipt_failed:{exc}"
        ) from exc


def _commit_capture(repo: Path, payload: dict[str, Any]) -> dict[str, Any]:
    ledger, state, hot_root = _capture_paths(repo)
    if not ledger.is_file() or not state.is_file():
        raise ManagedCurrentTurnError("capture_truth_not_prepared")
    try:
        with capture_hot.capture_ledger_lock(ledger) as lock_token:
            canonical = capture_hot.canonical_fact_event_from_payload(payload)
            expected_capture_id = str(canonical["capture_id"])
            final_refs = payload.get("stable_evidence_refs") or []
            final_locator = (
                str(final_refs[0].get("locator") or "")
                if len(final_refs) == 1 and isinstance(final_refs[0], dict)
                else ""
            )
            try:
                state_value = json.loads(state.read_text(encoding="utf-8"))
            except (OSError, UnicodeError, json.JSONDecodeError) as exc:
                raise ManagedCurrentTurnError(
                    "capture_state_unreadable_before_item_uniqueness_gate"
                ) from exc
            captures = state_value.get("captures")
            if not isinstance(captures, dict):
                raise ManagedCurrentTurnError(
                    "capture_state_invalid_before_item_uniqueness_gate"
                )
            for existing_id, row in captures.items():
                if not isinstance(row, dict):
                    continue
                existing_payload = row.get("capture")
                if not isinstance(existing_payload, dict):
                    continue
                existing_refs = existing_payload.get("stable_evidence_refs")
                if not isinstance(existing_refs, list):
                    continue
                if any(
                    isinstance(ref, dict)
                    and str(ref.get("locator") or "") == final_locator
                    for ref in existing_refs
                ) and str(existing_id) != expected_capture_id:
                    raise ManagedCurrentTurnError(
                        "session_item_already_has_an_immutable_capture"
                    )
            result = capture_hot.capture_fact(
                ledger,
                state,
                hot_root,
                lock_token=lock_token,
                validated_payload=payload,
            )
    except (capture_hot.CaptureHotWriterError, OSError) as exc:
        raise ManagedCurrentTurnError(f"capture_commit_failed:{exc}") from exc
    joint = result.get("joint_verification") or {}
    if (
        result.get("status") not in {"CAPTURED", "ALREADY_COMMITTED"}
        or result.get("quality_status") != capture_model.PENDING_FORMAL_STATUS
        or joint.get("status") != "PASS"
        or result.get("formal_write_count") != 0
    ):
        raise ManagedCurrentTurnError("capture_commit_not_verified")
    capture_id = str(canonical["capture_id"])
    payload_sha256 = str(canonical["payload_sha256"])
    if (
        result.get("capture_id") != capture_id
        or result.get("event_id") != canonical["event_id"]
    ):
        raise ManagedCurrentTurnError("capture_commit_identity_drifted")
    return {
        **result,
        "subject": "cs408",
        "payload_sha256": payload_sha256,
        "capture_content_sha256": payload_sha256,
    }


def _publish_recovery(
    *,
    context: dict[str, Any],
    feedback_sha256: str,
    evidence_receipt: dict[str, Any] | None,
    capture_payload: dict[str, Any] | None,
    failed_stage: str,
    reason_code: str,
    feedback_text: str,
    first_answer: dict[str, Any] | None,
    evidence_bundle: dict[str, Any] | None,
    staged_attachment_objects: list[dict[str, Any]] | None,
    private_root: str | Path | None,
) -> dict[str, str]:
    recovery_kind = {
        "first_answer": "first_answer_recovery",
        "session_feedback": "session_feedback_recovery",
        "capture": "capture_recovery",
        "private_evidence": "capture_evidence_recovery",
    }.get(failed_stage, "turn_recovery")
    recovery = {
        "schema": private_evidence.RECOVERY_SCHEMA,
        "recovery_kind": recovery_kind,
        "context": context,
        "feedback_text": feedback_text,
        "feedback_sha256": feedback_sha256,
        "evidence_locator": (
            evidence_receipt.get("locator") if evidence_receipt else None
        ),
        "evidence_manifest_sha256": (
            evidence_receipt.get("manifest_sha256") if evidence_receipt else None
        ),
        "capture_payload": capture_payload,
        "evidence_bundle": evidence_bundle,
        "staged_attachment_objects": staged_attachment_objects or [],
        "first_answer": first_answer,
        "failed_stage": failed_stage,
        "reason_code": reason_code[:320],
        "formal_write_count": 0,
    }
    return private_evidence.publish_metadata_object(
        recovery, kind="recoveries", private_root=private_root
    )


def _turn_advance_allowed(
    *,
    context: dict[str, Any],
    capture_status: str,
    feedback_authorized: bool,
) -> bool:
    if not feedback_authorized:
        return False
    if not context["classification"].get(
        "terminal_resolved",
        context["classification"]["teaching_result"]
        in {"independent_correct", "fragile_correct"},
    ):
        return False
    return capture_status == capture_model.PENDING_FORMAL_STATUS


def _publish_turn_receipt(
    *,
    context: dict[str, Any],
    feedback_sha256: str,
    first_answer: dict[str, Any] | None,
    capture: dict[str, Any] | None,
    capture_status: str,
    recovery: dict[str, str] | None,
    feedback_authorized: bool,
    recovery_kind: str | None,
    private_root: str | Path | None,
) -> dict[str, str]:
    receipt = {
        "schema": private_evidence.TURN_RECEIPT_SCHEMA,
        "subject": "cs408",
        "context_id": context["context_id"],
        "request_id": context["request_id"],
        "session_id": context["session_id"],
        "item_id": context["item_id"],
        "feedback_sha256": feedback_sha256,
        "first_result": context["classification"]["teaching_result"],
        "first_answer_receipt_sha256": (
            first_answer.get("commit_receipt_sha256")
            or first_answer.get("review_commit_receipt_sha256")
            if first_answer
            else None
        ),
        "capture_id": capture.get("capture_id") if capture else None,
        "capture_content_sha256": (
            capture.get("capture_content_sha256") if capture else None
        ),
        "capture_receipt_sha256": capture.get("receipt_sha256") if capture else None,
        "capture_status": capture_status,
        "recovery_locator": recovery.get("locator") if recovery else None,
        "recovery_kind": recovery_kind,
        "feedback_authorized": feedback_authorized,
        "advance_allowed": _turn_advance_allowed(
            context=context,
            capture_status=capture_status,
            feedback_authorized=feedback_authorized,
        ),
        "next_display_receipt_sha256": None,
        "formal_write_count": 0,
    }
    return private_evidence.publish_metadata_object(
        receipt, kind="turns", private_root=private_root
    )


@study_commit_boundary
def run_current_question_turn(
    repo: str | Path,
    raw_context: dict[str, Any],
    *,
    feedback_text: str,
    private_evaluation: dict[str, Any],
    interaction_trace: object | None = None,
    attachments: list[dict[str, Any]] | None = None,
    question_mode: str | None = None,
    private_root: str | Path | None = None,
    fault_injector: FaultInjector | None = None,
) -> dict[str, Any]:
    """Freeze feedback, then perform one bounded current-question transaction."""

    root = Path(repo).expanduser().resolve()
    if not root.is_dir():
        raise ManagedCurrentTurnError("repository_missing")
    context = context_model.validate_context(raw_context)
    _validate_controlled_assessment(context, private_evaluation)
    context_binding_sha = _sha256(
        _json_bytes(
            {
                key: value
                for key, value in context.items()
                if key not in {"classification", "context_id"}
            }
        )
    )
    feedback, feedback_sha = _frozen_feedback(feedback_text)
    capture_required = _terminal_resolved(context)
    effective_trace = interaction_trace
    learner_choice = str(context["learner_evidence"].get("choice") or "").strip().upper()
    if capture_required and learner_choice in {"A", "B", "C", "D"}:
        effective_trace = _trace_with_current_answer(
            context_model.normalize_interaction_trace(interaction_trace),
            learner_choice,
        )
    evidence_receipt: dict[str, Any] | None = None
    evidence_bundle: dict[str, Any] | None = None
    staged_attachment_objects: list[dict[str, Any]] = []
    capture_payload: dict[str, Any] | None = None
    first_answer: dict[str, Any] | None = None
    capture: dict[str, Any] | None = None
    recovery: dict[str, str] | None = None
    recovery_kind: str | None = None
    capture_status = "not_eligible"
    session_feedback: dict[str, Any] | None = None
    private_evidence_failure_reason: str | None = None

    try:
        evidence_receipt = _write_current_conversation_package(
            context,
            private_evaluation=private_evaluation,
            interaction_trace=effective_trace,
            feedback_text=feedback,
            attachments=attachments,
            question_mode=question_mode or "dialogue_only",
            seal=capture_required,
            private_root=private_root,
        )
        if capture_required:
            evidence_receipt = _reopen_final_conversation_package(
                context, evidence_receipt, private_root=private_root
            )
            capture_payload = _capture_payload(context, evidence_receipt)
    except (conversation_package.ConversationPackageError, OSError) as exc:
        private_evidence_failure_reason = str(exc)
        capture_status = "conversation_package_pending_recovery"
        recovery_kind = "conversation_package_recovery"

    if not capture_required:
        return {
            "schema": TURN_SCHEMA,
            "subject": "cs408",
            "status": "feedback_ready",
            "context_id": context["context_id"],
            "session_id": context["session_id"],
            "item_id": context["item_id"],
            "first_result": context["classification"]["teaching_result"],
            "feedback_text": feedback,
            "feedback_sha256": feedback_sha,
            "feedback_authorized": True,
            "first_answer_committed": False,
            "capture_status": BUFFERING_STATUS,
            "capture_id": None,
            "capture_content_sha256": None,
            "capture_receipt_sha256": None,
            "conversation_package_locator": (
                evidence_receipt.get("locator") if evidence_receipt else None
            ),
            "conversation_package_sha256": (
                evidence_receipt.get("canonical_sha256")
                if evidence_receipt
                else None
            ),
            "recovery_locator": None,
            "recovery_kind": None,
            "turn_receipt_locator": None,
            "turn_receipt_sha256": None,
            "advance_allowed": False,
            "retry_idempotency_key": None,
            "context_binding_sha256": context_binding_sha,
            "next_display_receipt_sha256": None,
            "next_item_published": False,
            "learner_evidence_write_count": 0,
            "luna_call_count": 0,
            "personalization_read_count": 0,
            "formal_write_count": 0,
        }

    try:
        _fault(fault_injector, "before_first_answer_commit")
        first_answer = _record_first_answer(
            root,
            context,
            evidence_manifest_sha256=(
                evidence_receipt.get("manifest_sha256")
                if evidence_receipt
                else None
            ),
        )
        _validate_first_answer_receipts(context, first_answer)
        _fault(fault_injector, "after_first_answer_commit")
    except Exception as exc:
        recovery = _publish_recovery(
            context=context,
            feedback_text=feedback,
            feedback_sha256=feedback_sha,
            evidence_receipt=evidence_receipt,
            capture_payload=capture_payload,
            failed_stage="first_answer",
            reason_code=str(exc),
            first_answer=first_answer,
            evidence_bundle=evidence_bundle,
            staged_attachment_objects=staged_attachment_objects,
            private_root=private_root,
        )
        capture_status = "first_answer_pending_recovery"
        recovery_kind = "first_answer_recovery"
        turn_receipt = _publish_turn_receipt(
            context=context,
            feedback_sha256=feedback_sha,
            first_answer=first_answer,
            capture=None,
            capture_status=capture_status,
            recovery=recovery,
            feedback_authorized=False,
            recovery_kind=recovery_kind,
            private_root=private_root,
        )
        return {
            "schema": TURN_SCHEMA,
            "status": "recovery_required",
            "reason_code": "first_answer_commit_or_receipt_failed",
            "context_id": context["context_id"],
            "session_id": context["session_id"],
            "item_id": context["item_id"],
            "feedback_authorized": False,
            "first_answer_committed": False,
            "capture_status": capture_status,
            "recovery_kind": recovery_kind,
            "recovery_locator": recovery["locator"],
            "turn_receipt_locator": turn_receipt["locator"],
            "turn_receipt_sha256": turn_receipt["sha256"],
            "next_item_published": False,
            "formal_write_count": 0,
        }

    if context["source"] == "morning_review":
        try:
            _fault(fault_injector, "before_session_feedback_receipt")
            session_feedback = _mark_morning_feedback(
                root,
                context,
                first_answer,
                feedback_sha256=feedback_sha,
            )
            _validate_session_feedback_receipt(session_feedback)
            _fault(fault_injector, "after_session_feedback_receipt")
        except Exception as exc:
            recovery = _publish_recovery(
                context=context,
                feedback_text=feedback,
                feedback_sha256=feedback_sha,
                evidence_receipt=evidence_receipt,
                capture_payload=capture_payload,
                failed_stage="session_feedback",
                reason_code=str(exc),
                first_answer=first_answer,
                evidence_bundle=evidence_bundle,
                staged_attachment_objects=staged_attachment_objects,
                private_root=private_root,
            )
            capture_status = "session_feedback_pending_recovery"
            recovery_kind = "session_feedback_recovery"
            turn_receipt = _publish_turn_receipt(
                context=context,
                feedback_sha256=feedback_sha,
                first_answer=first_answer,
                capture=None,
                capture_status=capture_status,
                recovery=recovery,
                feedback_authorized=False,
                recovery_kind=recovery_kind,
                private_root=private_root,
            )
            return {
                "schema": TURN_SCHEMA,
                "status": "recovery_required",
                "reason_code": "session_feedback_receipt_failed",
                "context_id": context["context_id"],
                "session_id": context["session_id"],
                "item_id": context["item_id"],
                "feedback_authorized": False,
                "first_answer_committed": True,
                "capture_status": capture_status,
                "recovery_kind": recovery_kind,
                "recovery_locator": recovery["locator"],
                "turn_receipt_locator": turn_receipt["locator"],
                "turn_receipt_sha256": turn_receipt["sha256"],
                "next_item_published": False,
                "formal_write_count": 0,
            }

    if recovery_kind in {
        "capture_evidence_recovery",
        "observation_evidence_recovery",
    }:
        try:
            recovery = _publish_recovery(
                context=context,
                feedback_text=feedback,
                feedback_sha256=feedback_sha,
                evidence_receipt=None,
                capture_payload=None,
                failed_stage="private_evidence",
                reason_code=(
                    private_evidence_failure_reason
                    or "private_evidence_publish_failed"
                ),
                first_answer=first_answer,
                evidence_bundle=evidence_bundle,
                staged_attachment_objects=staged_attachment_objects,
                private_root=private_root,
            )
        except (private_evidence.CurrentQuestionEvidenceError, OSError):
            recovery = None

    if (
        first_answer is not None
        and capture_required
        and capture_payload is not None
    ):
        try:
            if evidence_receipt is None or evidence_receipt.get("evidence_status") != "ready":
                raise ManagedCurrentTurnError("capture_evidence_receipt_not_ready")
            _fault(fault_injector, "before_capture_commit")
            capture = _commit_capture(root, capture_payload)
            _fault(fault_injector, "after_capture_commit")
            capture_status = capture_model.PENDING_FORMAL_STATUS
        except Exception as exc:
            try:
                recovery = _publish_recovery(
                    context=context,
                    feedback_text=feedback,
                    feedback_sha256=feedback_sha,
                    evidence_receipt=evidence_receipt,
                    capture_payload=capture_payload,
                    failed_stage="capture",
                    reason_code=str(exc),
                    first_answer=first_answer,
                    evidence_bundle=evidence_bundle,
                    staged_attachment_objects=staged_attachment_objects,
                    private_root=private_root,
                )
            except (private_evidence.CurrentQuestionEvidenceError, OSError):
                recovery = None
            capture_status = "capture_pending_recovery"
            recovery_kind = "capture_recovery"

    try:
        turn_receipt = _publish_turn_receipt(
            context=context,
            feedback_sha256=feedback_sha,
            first_answer=first_answer,
            capture=capture,
            capture_status=capture_status,
            recovery=recovery,
            feedback_authorized=True,
            recovery_kind=recovery_kind,
            private_root=private_root,
        )
    except (private_evidence.CurrentQuestionEvidenceError, OSError):
        if capture_status != "capture_pending_recovery":
            raise
        turn_receipt = None
    return {
        "schema": TURN_SCHEMA,
        "subject": "cs408",
        "status": "feedback_ready",
        "context_id": context["context_id"],
        "session_id": context["session_id"],
        "item_id": context["item_id"],
        "first_result": context["classification"]["teaching_result"],
        "feedback_text": feedback,
        "feedback_sha256": feedback_sha,
        "feedback_authorized": True,
        "first_answer_committed": first_answer is not None,
        "session_feedback_receipt_sha256": (
            ((session_feedback or {}).get("session_commit") or {}).get(
                "receipt_sha256"
            )
            if session_feedback is not None
            else None
        ),
        "capture_status": capture_status,
        "capture_id": capture.get("capture_id") if capture else None,
        "capture_content_sha256": (
            capture.get("capture_content_sha256") if capture else None
        ),
        "capture_receipt_sha256": capture.get("receipt_sha256") if capture else None,
        "evidence_locator": evidence_receipt.get("locator") if evidence_receipt else None,
        "evidence_manifest_sha256": (
            evidence_receipt.get("manifest_sha256") if evidence_receipt else None
        ),
        "evidence_status": (
            evidence_receipt.get("evidence_status") if evidence_receipt else None
        ),
        "conversation_package_locator": (
            evidence_receipt.get("locator") if evidence_receipt else None
        ),
        "conversation_package_sha256": (
            evidence_receipt.get("canonical_sha256") if evidence_receipt else None
        ),
        "recovery_locator": recovery.get("locator") if recovery else None,
        "recovery_kind": recovery_kind,
        "turn_receipt_locator": (
            turn_receipt.get("locator") if turn_receipt else None
        ),
        "turn_receipt_sha256": (
            turn_receipt.get("sha256") if turn_receipt else None
        ),
        "advance_allowed": _turn_advance_allowed(
            context=context,
            capture_status=capture_status,
            feedback_authorized=True,
        ),
        "retry_idempotency_key": (
            context["idempotency_key"]
            if capture_status == "capture_pending_recovery"
            else None
        ),
        "context_binding_sha256": context_binding_sha,
        "next_display_receipt_sha256": None,
        "next_item_published": False,
        "luna_call_count": 0,
        "personalization_read_count": 0,
        "formal_write_count": 0,
    }


@study_commit_boundary
def recover_current_receipts(
    repo: str | Path,
    recovery_locator: str,
    *,
    private_root: str | Path | None = None,
) -> dict[str, Any]:
    root = Path(repo).expanduser().resolve()
    recovery = private_evidence.read_metadata_object(
        recovery_locator, kind="recoveries", private_root=private_root
    )
    raw_context = dict(recovery["context"])
    raw_context.pop("classification", None)
    raw_context.pop("context_id", None)
    context = context_model.validate_context(raw_context)
    feedback, feedback_sha = _frozen_feedback(recovery.get("feedback_text"))
    if feedback_sha != recovery.get("feedback_sha256"):
        raise ManagedCurrentTurnError("recovery_feedback_binding_drifted")
    failed_stage = str(recovery.get("failed_stage") or "")
    if failed_stage not in {"first_answer", "session_feedback"}:
        raise ManagedCurrentTurnError("receipt_recovery_stage_invalid")
    first_answer = recovery.get("first_answer")
    if failed_stage == "first_answer":
        first_answer = _record_first_answer(
            root,
            context,
            evidence_manifest_sha256=recovery.get("evidence_manifest_sha256"),
        )
        _validate_first_answer_receipts(context, first_answer)
    else:
        _validate_first_answer_receipts(context, first_answer)

    session_feedback: dict[str, Any] | None = None
    if failed_stage in {"first_answer", "session_feedback"} and context["source"] == "morning_review":
        session_feedback = _mark_morning_feedback(
            root,
            context,
            first_answer,
            feedback_sha256=feedback_sha,
        )
        _validate_session_feedback_receipt(session_feedback)

    payload = recovery.get("capture_payload")
    capture_recovery: dict[str, str] | None = None
    if isinstance(payload, dict):
        capture_status = "capture_pending_recovery"
        capture_recovery = _publish_recovery(
            context=context,
            feedback_text=feedback,
            feedback_sha256=feedback_sha,
            evidence_receipt=(
                {
                    "locator": recovery.get("evidence_locator"),
                    "manifest_sha256": recovery.get("evidence_manifest_sha256"),
                }
                if recovery.get("evidence_locator")
                and recovery.get("evidence_manifest_sha256")
                else None
            ),
            capture_payload=payload,
            failed_stage="capture",
            reason_code="receipt_recovery_completed_capture_still_pending",
            first_answer=first_answer,
            evidence_bundle=recovery.get("evidence_bundle"),
            staged_attachment_objects=recovery.get("staged_attachment_objects"),
            private_root=private_root,
        )
    else:
        raise ManagedCurrentTurnError("receipt_recovery_lacks_capture_payload")
    turn_receipt = _publish_turn_receipt(
        context=context,
        feedback_sha256=feedback_sha,
        first_answer=first_answer,
        capture=None,
        capture_status=capture_status,
        recovery=capture_recovery,
        feedback_authorized=True,
        recovery_kind=("capture_recovery" if capture_recovery else None),
        private_root=private_root,
    )
    return {
        "schema": RECOVERY_RESULT_SCHEMA,
        "subject": "cs408",
        "status": "feedback_ready",
        "context_id": context["context_id"],
        "capture_status": capture_status,
        "capture_id": None,
        "capture_content_sha256": None,
        "capture_receipt_sha256": None,
        "feedback_text": feedback,
        "feedback_authorized": True,
        "session_feedback_receipt_sha256": (
            ((session_feedback or {}).get("session_commit") or {}).get(
                "receipt_sha256"
            )
            if session_feedback is not None
            else None
        ),
        "position_changed": False,
        "next_item": None,
        "answer_operation_result": None,
        "recovery_locator": (
            capture_recovery.get("locator") if capture_recovery else None
        ),
        "turn_receipt_locator": turn_receipt["locator"],
        "turn_receipt_sha256": turn_receipt["sha256"],
        "formal_write_count": 0,
    }


def _complete_recovered_answer_operation(
    repo: Path,
    *,
    context: dict[str, Any],
    recovery_locator: str,
    feedback_text: str,
    capture_status: str,
    capture_id: str | None,
    turn_receipt: dict[str, str],
    private_root: str | Path | None,
    capture_metadata: dict[str, Any] | None = None,
) -> dict[str, Any] | None:
    operation_id = str(context.get("request_id") or "")
    display_digest = str(context.get("display_receipt_sha256") or "")
    if not re.fullmatch(r"AON-[0-9A-F]{64}", operation_id):
        return None
    if not SHA256_RE.fullmatch(display_digest):
        return None
    with private_evidence.answer_display_lock(
        display_digest, private_root=private_root
    ), private_evidence.answer_operation_lock(
        operation_id, private_root=private_root
    ):
        operation = private_evidence.read_answer_operation(
            operation_id, private_root=private_root
        )
        if operation is None:
            return None
        navigation_policy = _operation_navigation_policy(operation)
        prepared_context = (operation.get("prepared") or {}).get("context")
        if not isinstance(prepared_context, dict):
            raise ManagedCurrentTurnError(
                "recovered_answer_operation_context_missing"
            )
        source_identity_fallback = _stored_source_identity_fallback(
            (operation.get("prepared") or {}).get(
                "source_identity_fallback"
            )
        )
        if source_identity_fallback is None:
            prepared_context_id = context_model.validate_context(
                prepared_context
            )["context_id"]
        else:
            with context_model.verified_source_identity_fallback(
                raw_source_label=source_identity_fallback[
                    "raw_source_label"
                ],
                machine_source_id=source_identity_fallback[
                    "machine_source_id"
                ],
                scheduling_binding_sha256=source_identity_fallback[
                    "scheduling_binding_sha256"
                ],
                binding_verification=source_identity_fallback[
                    "binding_verification"
                ],
            ):
                prepared_context_id = context_model.validate_context(
                    prepared_context
                )["context_id"]
        if (
            prepared_context_id != context["context_id"]
            or prepared_context.get("session_id") != context["session_id"]
            or prepared_context.get("item_id") != context["item_id"]
        ):
            raise ManagedCurrentTurnError(
                "recovered_answer_operation_context_drifted"
            )
        existing_result = operation.get("result")
        if (
            operation.get("status") == "complete"
            and isinstance(existing_result, dict)
            and existing_result.get("recovery_locator") is None
            and existing_result.get("capture_status") == capture_status
            and existing_result.get("capture_id") == capture_id
        ):
            replay = dict(existing_result)
            replay.setdefault("navigation_policy", navigation_policy)
            replay["idempotent_replay"] = True
            replay["learner_evidence_write_count"] = 0
            replay["formal_write_count"] = 0
            return replay
        if (
            isinstance(existing_result, dict)
            and existing_result.get("recovery_locator") not in {
                None,
                recovery_locator,
            }
            and existing_result.get("status")
            not in {"recovery_required", "feedback_ready_recovery_required"}
        ):
            raise ManagedCurrentTurnError(
                "recovered_answer_operation_state_drifted"
            )
        first_result = context["classification"]["teaching_result"]
        advance_allowed = _turn_advance_allowed(
            context=context,
            capture_status=capture_status,
            feedback_authorized=True,
        )
        following: dict[str, Any] | None = None
        if advance_allowed and navigation_policy == DEFAULT_NAVIGATION_POLICY:
            following = next_item(
                repo,
                prior_turn_receipt_locator=turn_receipt["locator"],
                private_root=private_root,
            )
        if first_result in {"wrong", "partial", "uncertain"}:
            status = "feedback_ready_continue_current"
        elif advance_allowed and navigation_policy == "hold_current":
            status = "feedback_ready_current_held"
        elif following is None:
            status = "feedback_ready_recovery_required"
        elif following.get("status") == "complete":
            status = "feedback_ready_session_complete"
        else:
            status = "feedback_and_next_ready"
        result = _answer_result(
            operation_id=operation_id,
            status=status,
            feedback_text=feedback_text,
            first_result=first_result,
            capture_status=capture_status,
            capture_id=capture_id,
            turn_receipt_locator=turn_receipt["locator"],
            following=following,
            recovery_locator=None,
            learner_evidence_write_count=0,
            navigation_policy=navigation_policy,
            capture_content_sha256=(
                capture_metadata.get("capture_content_sha256")
                if capture_metadata
                else None
            ),
        )
        return _complete_answer_operation(
            operation, result, private_root=private_root
        )


@study_commit_boundary
def recover_current_capture(
    repo: str | Path,
    recovery_locator: str,
    *,
    private_root: str | Path | None = None,
) -> dict[str, Any]:
    """Retry only private evidence plus the required capture or observation."""

    root = Path(repo).expanduser().resolve()
    recovery = private_evidence.read_metadata_object(
        recovery_locator, kind="recoveries", private_root=private_root
    )
    failed_stage = recovery.get("failed_stage")
    if failed_stage not in {"capture", "private_evidence"}:
        raise ManagedCurrentTurnError("capture_recovery_stage_invalid")
    raw_context = dict(recovery["context"])
    raw_context.pop("classification", None)
    raw_context.pop("context_id", None)
    context = context_model.validate_context(raw_context)
    payload = recovery.get("capture_payload")
    evidence_bundle = recovery.get("evidence_bundle")
    first_answer = recovery.get("first_answer")
    if not isinstance(first_answer, dict):
        raise ManagedCurrentTurnError("capture_recovery_first_answer_receipt_missing")
    _validate_first_answer_receipts(context, first_answer)
    evidence_manifest_sha = str(
        recovery.get("evidence_manifest_sha256") or ""
    )
    evidence_receipt: dict[str, Any] | None = None
    if failed_stage == "private_evidence":
        bundle = evidence_bundle
        staged = recovery.get("staged_attachment_objects")
        if not isinstance(bundle, dict) or not isinstance(staged, list):
            raise ManagedCurrentTurnError("capture_evidence_recovery_material_missing")
        evidence_receipt = private_evidence.publish_bundle(
            bundle,
            private_root=private_root,
            staged_attachment_objects=staged,
        )
        evidence_manifest_sha = str(evidence_receipt["manifest_sha256"])
        if context["classification"]["capture_required"]:
            payload = _capture_payload(context, evidence_receipt)
    elif recovery.get("evidence_locator") and evidence_manifest_sha:
        locator = str(recovery["evidence_locator"])
        if locator.startswith(conversation_package.LOCATOR_PREFIX):
            opened = conversation_package.read_package(
                locator,
                private_root=_conversation_package_root_for_runtime(private_root),
            )
            current_sha = str(opened["manifest"].get("canonical_sha256") or "")
            if current_sha != evidence_manifest_sha:
                raise ManagedCurrentTurnError("conversation_package_recovery_hash_drifted")
            evidence_receipt = {
                "locator": locator,
                "manifest_sha256": current_sha,
                "canonical_sha256": current_sha,
                "evidence_status": "ready",
            }
            if not isinstance(payload, dict):
                payload = _capture_payload(context, evidence_receipt)
        else:
            _, reopened_bundle = private_evidence.read_bundle(
                locator, private_root=private_root
            )
            evidence_receipt = {
                "locator": locator,
                "manifest_sha256": evidence_manifest_sha,
                "evidence_status": private_evidence.bundle_evidence_status(
                    reopened_bundle
                ),
            }
    if not isinstance(payload, dict):
        raise ManagedCurrentTurnError("capture_recovery_payload_missing")
    if not SHA256_RE.fullmatch(evidence_manifest_sha):
        raise ManagedCurrentTurnError("capture_recovery_evidence_binding_missing")
    expected_capture_id = str(
        capture_hot.canonical_fact_event_from_payload(payload)["capture_id"]
    )
    capture = _commit_capture(
        root, capture_model._validate_capture_payload(payload)
    )
    if capture.get("capture_id") != expected_capture_id:
        raise ManagedCurrentTurnError("capture_recovery_identity_drifted")
    turn_receipt = _publish_turn_receipt(
        context=context,
        feedback_sha256=str(recovery["feedback_sha256"]),
        first_answer=first_answer,
        capture=capture,
        capture_status=capture_model.PENDING_FORMAL_STATUS,
        recovery=None,
        feedback_authorized=True,
        recovery_kind=None,
        private_root=private_root,
    )
    display_lifecycle_repaired = False
    display_digest = str(context.get("display_receipt_sha256") or "")
    if SHA256_RE.fullmatch(display_digest):
        lifecycle = private_evidence.read_answer_display_lifecycle(
            display_digest, private_root=private_root
        )
        if lifecycle is not None and lifecycle.get("status") == "first_recorded":
            first_turn = lifecycle.get("first_turn")
            if (
                not isinstance(first_turn, dict)
                or first_turn.get("context_id") != context["context_id"]
                or first_turn.get("session_id") != context.get("session_id")
                or first_turn.get("item_id") != context.get("item_id")
                or first_turn.get("recovery_locator") != recovery_locator
            ):
                raise ManagedCurrentTurnError(
                    "capture_recovery_display_lifecycle_drifted"
                )
            repaired_first_turn = {
                **first_turn,
                "capture_status": capture_model.PENDING_FORMAL_STATUS,
                "capture_id": capture["capture_id"],
                "capture_content_sha256": capture["capture_content_sha256"],
                "capture_receipt_sha256": capture["receipt_sha256"],
                "recovery_locator": None,
                "recovery_kind": None,
                "turn_receipt_locator": turn_receipt["locator"],
                "turn_receipt_sha256": turn_receipt["sha256"],
                "advance_allowed": _turn_advance_allowed(
                    context=context,
                    capture_status=capture_model.PENDING_FORMAL_STATUS,
                    feedback_authorized=True,
                ),
                "next_item_published": False,
                "next_display_receipt_sha256": None,
            }
            private_evidence.write_answer_display_lifecycle(
                {**lifecycle, "first_turn": repaired_first_turn},
                private_root=private_root,
            )
            reopened = private_evidence.read_answer_display_lifecycle(
                display_digest, private_root=private_root
            )
            if (
                reopened is None
                or (reopened.get("first_turn") or {}).get("capture_id")
                != capture["capture_id"]
                or (reopened.get("first_turn") or {}).get("capture_status")
                != capture_model.PENDING_FORMAL_STATUS
            ):
                raise ManagedCurrentTurnError(
                    "capture_recovery_display_lifecycle_not_verified"
                )
            display_lifecycle_repaired = True
    operation_result = _complete_recovered_answer_operation(
        root,
        context=context,
        recovery_locator=recovery_locator,
        feedback_text=str(recovery["feedback_text"]),
        capture_status=capture_model.PENDING_FORMAL_STATUS,
        capture_id=str(capture["capture_id"]),
        turn_receipt=turn_receipt,
        private_root=private_root,
        capture_metadata=capture,
    )
    following = (
        operation_result.get("next_item")
        if isinstance(operation_result, dict)
        else None
    )
    return {
        "schema": RECOVERY_RESULT_SCHEMA,
        "subject": "cs408",
        "status": capture_model.PENDING_FORMAL_STATUS,
        "context_id": context["context_id"],
        "capture_id": capture["capture_id"],
        "capture_content_sha256": capture["capture_content_sha256"],
        "capture_receipt_sha256": capture["receipt_sha256"],
        "display_lifecycle_repaired": display_lifecycle_repaired,
        "feedback_authorized": True,
        "position_changed": (
            isinstance(following, dict) and following.get("status") == "published"
        ),
        "next_item": following,
        "answer_operation_result": operation_result,
        "outcome_write_count": 0,
        "session_write_count": 0,
        "turn_receipt_locator": turn_receipt["locator"],
        "turn_receipt_sha256": turn_receipt["sha256"],
        "formal_write_count": 0,
    }


def _publish_prepared_display(
    repo: Path,
    *,
    session_id: str,
    item_id: str,
    private_root: str | Path | None,
) -> dict[str, Any]:
    import morning_review_prepared_pack_408 as prepared_pack

    shown = prepared_pack.show_item(repo, session_id, item_id)
    surface = dict(shown["surface"])
    question_asset = prepared_pack.resolve_question_asset(surface)
    lines = [str(surface["stem"]).strip(), ""]
    lines.extend(
        f"{key}. {str(surface['options'][key]).strip()}"
        for key in ("A", "B", "C", "D")
    )
    lines.extend(["", str(surface["response_instruction"]).strip()])
    display_text = "\n".join(lines).strip() + "\n"
    scheduling_binding = prepared_pack.scheduling_binding_for_item(
        repo, session_id, item_id
    )
    display = {
        "schema": private_evidence.TURN_RECEIPT_SCHEMA,
        "status": "display_published",
        "context_id": None,
        "request_id": None,
        "session_id": session_id,
        "item_id": item_id,
        "feedback_sha256": None,
        "first_result": None,
        "first_answer_receipt_sha256": None,
        "capture_id": None,
        "capture_receipt_sha256": None,
        "capture_status": "not_started",
        "recovery_locator": None,
        "advance_allowed": False,
        "surface_sha256": shown["surface_sha256"],
        "display_sha256": _sha256(display_text.encode("utf-8")),
        "next_display_receipt_sha256": None,
        "question_asset_ref": (
            question_asset["relative_path"] if question_asset is not None else None
        ),
        "question_asset_sha256": (
            question_asset["sha256"] if question_asset is not None else None
        ),
        "scheduling_binding": scheduling_binding,
        "formal_write_count": 0,
    }
    receipt = private_evidence.publish_metadata_object(
        display, kind="turns", private_root=private_root
    )
    return {
        "schema": NEXT_ITEM_SCHEMA,
        "status": "published",
        "session_id": session_id,
        "item_id": item_id,
        "surface_sha256": shown["surface_sha256"],
        "display_text": display_text,
        "display_receipt_locator": receipt["locator"],
        "display_receipt_sha256": receipt["sha256"],
        "question_asset_path": (
            question_asset["absolute_path"] if question_asset is not None else None
        ),
        "question_asset_sha256": (
            question_asset["sha256"] if question_asset is not None else None
        ),
        "scheduling_binding": scheduling_binding,
        "formal_write_count": 0,
    }


def next_item(
    repo: str | Path,
    *,
    prior_turn_receipt_locator: str,
    private_root: str | Path | None = None,
) -> dict[str, Any]:
    """Publish the next unanswered prepared surface as a separate action."""

    prior = private_evidence.read_metadata_object(
        prior_turn_receipt_locator, kind="turns", private_root=private_root
    )
    if prior.get("navigation_policy") != DEFAULT_NAVIGATION_POLICY:
        raise ManagedCurrentTurnError(
            "current_navigation_policy_forbids_next_item"
        )
    if prior.get("advance_allowed") is not True:
        raise ManagedCurrentTurnError("current_capture_recovery_required_before_next_item")
    root = Path(repo).expanduser().resolve()
    capture_id = str(prior.get("capture_id") or "")
    payload_sha = str(prior.get("capture_payload_sha256") or "")
    idempotency_key = str(prior.get("capture_idempotency_key") or "")
    package_locator = str(prior.get("conversation_package_locator") or "")
    package_sha = str(prior.get("conversation_package_sha256") or "")
    if (
        prior.get("capture_binding_verified") is not True
        or prior.get("conversation_package_sealed") is not True
        or not capture_id
        or not SHA256_RE.fullmatch(payload_sha)
        or not idempotency_key
        or not package_locator.startswith(conversation_package.LOCATOR_PREFIX)
        or not SHA256_RE.fullmatch(package_sha)
    ):
        raise ManagedCurrentTurnError(
            "terminal_capture_receipt_required_before_navigation"
        )
    try:
        capture_binding = capture_model.reopen_capture_binding(
            capture_id,
            expected_payload_sha256=payload_sha,
            expected_idempotency_key=idempotency_key,
            repo_root=root,
        )
    except (capture_model.CaptureError, OSError) as exc:
        raise ManagedCurrentTurnError(
            f"terminal_capture_receipt_reopen_failed:{exc}"
        ) from exc
    ledger_path, state_path, hot_root = _capture_paths(root)
    try:
        durable_binding = capture_hot.verify_joint_commit(
            ledger_path,
            state_path,
            hot_root,
            idempotency_key=idempotency_key,
        )
    except (capture_hot.CaptureHotWriterError, OSError) as exc:
        raise ManagedCurrentTurnError(
            f"terminal_capture_durable_receipt_reopen_failed:{exc}"
        ) from exc
    expected_ref = {
        "kind": capture_model.CONVERSATION_PACKAGE_REF_KIND,
        "locator": package_locator,
        "sha256": package_sha,
    }
    if (
        capture_binding.get("stable_evidence_refs") != [expected_ref]
        or capture_binding.get("quality_status")
        != capture_model.PENDING_FORMAL_STATUS
        or durable_binding.get("status") != "PASS"
        or durable_binding.get("capture_id") != capture_id
        or durable_binding.get("receipt_sha256")
        != prior.get("capture_receipt_sha256")
        or durable_binding.get("event_sha256")
        != prior.get("capture_event_sha256")
        or durable_binding.get("joint_manifest_sha256")
        != prior.get("capture_joint_manifest_sha256")
    ):
        raise ManagedCurrentTurnError(
            "terminal_capture_receipt_binding_drifted"
        )
    try:
        import morning_review_prepared_pack_408 as prepared_pack
        import morning_review_session as morning_session
    except ImportError as exc:
        raise ManagedCurrentTurnError("morning_next_item_module_unavailable") from exc
    session_id = str(prior.get("session_id") or "")
    prior_item_id = str(prior.get("item_id") or "")
    session_dir = morning_session._session_dir(root, session_id)
    with morning_session.session_lock(session_dir):
        state = morning_session._load_state(session_dir, session_id)
        morning_session._ensure_active(state)
        active_order = [
            item_id
            for item_id in state["item_order"]
            if state["items"][item_id].get("resolution")
            not in morning_session.NAVIGATION_EXCLUDED_RESOLUTIONS
        ]
        if (
            prior_item_id not in active_order
            or state["items"][prior_item_id].get("first") is None
        ):
            raise ManagedCurrentTurnError(
                "prior_turn_receipt_is_not_navigation_frontier"
            )
        next_id = next(
            (
                item_id
                for item_id in active_order
                if state["items"][item_id].get("first") is None
            ),
            None,
        )
        prior_index = active_order.index(prior_item_id)
        if next_id is None:
            frontier_matches = prior_index == len(active_order) - 1
            if not frontier_matches and prior.get("status") == "teaching_resolved":
                item = state["items"][prior_item_id]
                resolution = item.get("teaching_resolution")
                supplement = prior.get("trace_supplement")
                receipt_sha = str(
                    prior.get("session_resolution_receipt_sha256") or ""
                )
                resolution_matches = bool(
                    item.get("resolution") == "relearn_required"
                    and isinstance(resolution, dict)
                    and isinstance(supplement, dict)
                    and resolution.get("capture_id") == prior.get("capture_id")
                    and resolution.get("capture_receipt_sha256")
                    == prior.get("capture_receipt_sha256")
                    and resolution.get("resolution_attestation_sha256")
                    == prior.get("resolution_attestation_sha256")
                    and resolution.get("trace_supplement_sha256")
                    == supplement.get("object_sha")
                    and resolution.get("interaction_trace_sha256")
                    == supplement.get("interaction_trace_sha256")
                    and resolution.get("mastery_effect") == "none"
                    and resolution.get("retention_effect") == "none"
                    and resolution.get("independent_repair") is False
                    and SHA256_RE.fullmatch(receipt_sha)
                )
                if resolution_matches:
                    try:
                        morning_session._verify_session_hot_event_receipt(
                            session_dir,
                            receipt_sha,
                            event_type="teaching_resolved",
                            item_id=prior_item_id,
                        )
                    except morning_session.SessionError:
                        resolution_matches = False
                frontier_matches = resolution_matches
        else:
            next_index = active_order.index(next_id)
            frontier_matches = next_index > 0 and prior_index == next_index - 1
        if not frontier_matches:
            raise ManagedCurrentTurnError(
                "prior_turn_receipt_is_not_navigation_frontier"
            )
    if next_id is None:
        try:
            close_result = morning_session.auto_close_completed_session(
                root, session_id
            )
        except morning_session.SessionError as exc:
            raise ManagedCurrentTurnError(
                f"session_complete_close_failed:{exc}"
            ) from exc
        if close_result.get("status") not in {"closed", "already_closed"}:
            raise ManagedCurrentTurnError(
                "session_complete_close_not_verified"
            )
        return {
            "schema": NEXT_ITEM_SCHEMA,
            "status": "complete",
            "session_id": session_id,
            "item_id": None,
            "session_closed": True,
            "session_close_receipt": close_result,
            "formal_write_count": 0,
        }
    return _publish_prepared_display(
        root,
        session_id=session_id,
        item_id=next_id,
        private_root=private_root,
    )


def resume_held_session(
    repo: str | Path,
    *,
    held_turn_receipt_locator: str,
    private_root: str | Path | None = None,
) -> dict[str, Any]:
    """Explicitly authorize a successor after a previously durable hold."""

    held = private_evidence.read_metadata_object(
        held_turn_receipt_locator, kind="turns", private_root=private_root
    )
    if (
        held.get("navigation_policy") != "hold_current"
        or held.get("advance_allowed") is not False
        or held.get("capture_binding_verified") is not True
        or held.get("conversation_package_sealed") is not True
    ):
        raise ManagedCurrentTurnError("held_turn_receipt_invalid")
    root = Path(repo).expanduser().resolve()
    session_id = str(held.get("session_id") or "")
    item_id = str(held.get("item_id") or "")
    try:
        import morning_review_session as morning_session
    except ImportError as exc:
        raise ManagedCurrentTurnError("morning_session_module_unavailable") from exc
    session_dir = morning_session._session_dir(root, session_id)
    with morning_session.session_lock(session_dir):
        state = morning_session._load_state(session_dir, session_id)
        status = str(state.get("status") or "")
        pause_reason = state.get("pause_reason")
    if status == "closed":
        return {
            "schema": "managed-408-resume-held-session-v1",
            "status": "session_already_closed",
            "session_id": session_id,
            "item_id": item_id,
            "next_item_published": False,
            "next_item": None,
            "formal_write_count": 0,
        }
    if status == "paused":
        if pause_reason != "user_pause":
            raise ManagedCurrentTurnError("held_session_pause_reason_drifted")
        morning_session.command_resume(
            argparse.Namespace(repo=str(root), session=session_id)
        )
    elif status != "in_progress":
        raise ManagedCurrentTurnError("held_session_state_invalid")

    held_sha = held_turn_receipt_locator.removeprefix(
        private_evidence.TURN_LOCATOR_PREFIX
    )
    if not SHA256_RE.fullmatch(held_sha):
        raise ManagedCurrentTurnError("held_turn_receipt_locator_invalid")
    resume_value = {
        **held,
        "status": "held_session_resume_authorized",
        "request_id": f"resume-held:{held_sha}",
        "navigation_policy": DEFAULT_NAVIGATION_POLICY,
        "advance_allowed": True,
        "resume_from_held_receipt_sha256": held_sha,
        "next_display_receipt_sha256": None,
        "formal_write_count": 0,
    }
    resume_receipt = private_evidence.publish_metadata_object(
        resume_value, kind="turns", private_root=private_root
    )
    following = next_item(
        root,
        prior_turn_receipt_locator=resume_receipt["locator"],
        private_root=private_root,
    )
    return {
        "schema": "managed-408-resume-held-session-v1",
        "status": (
            "session_complete"
            if following.get("status") == "complete"
            else "next_item_ready"
        ),
        "session_id": session_id,
        "item_id": item_id,
        "held_turn_receipt_locator": held_turn_receipt_locator,
        "resume_receipt_locator": resume_receipt["locator"],
        "resume_receipt_sha256": resume_receipt["sha256"],
        "next_item_published": following.get("status") == "published",
        "next_item": following,
        "formal_write_count": 0,
    }


def _complete_answer_operation(
    operation: dict[str, Any],
    result: dict[str, Any],
    *,
    private_root: str | Path | None,
) -> dict[str, Any]:
    private_evidence.write_answer_operation(
        {**operation, "status": "complete", "result": result},
        private_root=private_root,
    )
    return result


def _trace_with_current_answer(
    trace: dict[str, Any], choice: str
) -> dict[str, Any]:
    events = [
        {"role": row["role"], "kind": row["kind"], "text": row["text"]}
        for row in trace["events"]
    ]
    canonical_choice_present = False
    for row in events:
        if row["role"] != "learner" or row["kind"] != "answer":
            continue
        trace_choice = str(row["text"]).strip().upper()
        if trace_choice in MANAGED_CHOICES:
            if trace_choice != choice:
                raise ManagedCurrentTurnError("interaction_trace_choice_drifted")
            canonical_choice_present = True
    if not canonical_choice_present:
        events.append({"role": "learner", "kind": "answer", "text": choice})
    if trace.get("omitted_event_count", 0) and not canonical_choice_present:
        raise ManagedCurrentTurnError(
            "truncated_interaction_trace_must_include_canonical_choice"
        )
    return context_model.normalize_interaction_trace(events)


def _merge_interaction_traces(
    existing: dict[str, Any], current: dict[str, Any]
) -> dict[str, Any]:
    if existing.get("omitted_event_count", 0) or current.get("omitted_event_count", 0):
        raise ManagedCurrentTurnError(
            "interaction_trace_merge_requires_complete_source_traces"
        )
    merged = [
        {
            "role": str(row["role"]),
            "kind": str(row["kind"]),
            "text": str(row["text"]),
        }
        for trace in (existing, current)
        for row in trace["events"]
    ]
    return context_model.normalize_interaction_trace(merged)


def _append_followup_conversation_package(
    first_turn: dict[str, Any],
    *,
    cumulative_trace: dict[str, Any],
    feedback_text: str,
    attachments: list[dict[str, Any]],
    seal: bool,
    runtime_private_root: str | Path | None,
) -> dict[str, Any]:
    locator = str(first_turn.get("conversation_package_locator") or "")
    if not locator.startswith(conversation_package.LOCATOR_PREFIX):
        raise ManagedCurrentTurnError("conversation_package_locator_missing")
    package_root = _conversation_package_root_for_runtime(runtime_private_root)
    opened = conversation_package.read_package(locator, private_root=package_root)
    source_doc = opened["source"]
    source_payload = source_doc.get("source")
    if not isinstance(source_payload, dict):
        raise ManagedCurrentTurnError("conversation_package_source_invalid")
    return conversation_package.record_package(
        study_date=str(source_doc.get("study_date") or ""),
        session_id=source_doc.get("session_id"),
        item_id=source_doc.get("item_id"),
        source_id=str(source_doc.get("source_id") or ""),
        source=source_payload,
        conversation=cumulative_trace,
        frozen_feedback=feedback_text,
        attachments=attachments,
        missing_fields=list(opened["manifest"].get("missing_fields") or []),
        seal=seal,
        private_root=package_root,
    )


def _terminal_package_capture_and_navigation(
    repo: Path,
    *,
    operation_id: str,
    canonical_context: dict[str, Any],
    final_context: dict[str, Any],
    first_feedback_text: str,
    final_feedback_text: str,
    final_package_receipt: dict[str, Any],
    interaction_trace: dict[str, Any],
    navigation_policy: str,
    private_root: str | Path | None,
) -> dict[str, Any]:
    """Run the one legal terminal order for a managed session item."""

    if not _terminal_resolved(final_context):
        raise ManagedCurrentTurnError("terminal_finalization_requires_resolved_reasoning")
    for field in ("session_id", "item_id", "source_id", "study_date"):
        if canonical_context.get(field) != final_context.get(field):
            raise ManagedCurrentTurnError(
                f"terminal_finalization_{field}_binding_drifted"
            )
    first_feedback, first_feedback_sha = _frozen_feedback(first_feedback_text)
    final_feedback, final_feedback_sha = _frozen_feedback(final_feedback_text)

    # Fixed order begins here: final package -> one Capture -> durable reopen.
    final_package = _reopen_final_conversation_package(
        final_context,
        final_package_receipt,
        private_root=private_root,
    )
    payload = _capture_payload(canonical_context, final_package)
    capture = _commit_capture(repo, payload)
    binding = _reopen_verified_capture_binding(
        repo,
        payload=payload,
        capture=capture,
        final_package=final_package,
    )

    # Canonical review/session evidence is committed only after the Capture is
    # durably reopenable.  Retries are idempotent under the same operation and
    # item identities.
    first_answer = _record_first_answer(
        repo,
        canonical_context,
        evidence_manifest_sha256=final_package["canonical_sha256"],
    )
    _validate_first_answer_receipts(canonical_context, first_answer)
    session_feedback: dict[str, Any] | None = None
    if canonical_context["source"] == "morning_review":
        session_feedback = _mark_morning_feedback(
            repo,
            canonical_context,
            first_answer,
            feedback_sha256=first_feedback_sha,
        )
        _validate_session_feedback_receipt(session_feedback)

    # Any terminal that is not an unprompted independent first result still
    # needs the explicit teaching-resolution event.  `fragile_correct` is only
    # a session teaching label; its canonical first result is partial.
    corrected = (
        canonical_context["classification"]["teaching_result"]
        != "independent_correct"
    )
    attestation: dict[str, str] | None = None
    supplement: dict[str, Any] | None = None
    session_resolution_receipt: str | None = None
    if corrected:
        trace_sha = _sha256(_json_bytes(interaction_trace))
        attestation = private_evidence.publish_metadata_object(
            {
                "schema": private_evidence.TURN_RECEIPT_SCHEMA,
                "status": "teaching_resolution_attested",
                "attestation_schema": "teaching-resolution-attestation-v2",
                "context_id": final_context["context_id"],
                "first_context_id": canonical_context["context_id"],
                "session_id": final_context["session_id"],
                "item_id": final_context["item_id"],
                "capture_id": binding["capture_id"],
                "capture_event_sha256": binding["durable_event_sha256"],
                "capture_ledger_event_sha256": binding["event_sha256"],
                "capture_payload_sha256": binding["payload_sha256"],
                "evidence_manifest_sha256": final_package[
                    "canonical_sha256"
                ],
                "interaction_trace_sha256": trace_sha,
                "resolution_choice_result": "correct",
                "event_time": final_context["event_time"],
                "advance_allowed": False,
                "formal_write_count": 0,
            },
            kind="turns",
            private_root=private_root,
        )
        events = [
            {
                "role": row["role"],
                "kind": row["kind"],
                "text": row["text"],
                "observed_at": None,
            }
            for row in interaction_trace["events"]
        ]
        supplement = private_evidence.publish_trace_supplement(
            private_root=private_root,
            capture_id=binding["capture_id"],
            context_id=final_context["context_id"],
            item_id=final_context["item_id"],
            evidence_manifest_sha256=final_package["canonical_sha256"],
            created_at=final_context["event_time"],
            # Compatibility attestation only.  The Capture already binds the
            # complete final package directly; this supplement is never used
            # as a substitute for that evidence.
            supplement_kind="resolved_trace",
            resolution_receipt_sha256=attestation["sha256"],
            events=events,
        )
        supplement_sha = str(
            supplement.get("object_sha")
            or supplement.get("object_sha256")
            or supplement.get("sha256")
            or ""
        )
        supplement_trace_sha = str(
            supplement.get("interaction_trace_sha256") or ""
        )
        if (
            not SHA256_RE.fullmatch(supplement_sha)
            or not SHA256_RE.fullmatch(supplement_trace_sha)
        ):
            raise ManagedCurrentTurnError("terminal_trace_attestation_invalid")
        try:
            import morning_review_session as morning_session

            resolution = morning_session.mark_teaching_resolved(
                repo,
                final_context["session_id"],
                final_context["item_id"],
                capture_id=binding["capture_id"],
                capture_receipt_sha256=str(capture["receipt_sha256"]),
                resolution_attestation_sha256=attestation["sha256"],
                trace_supplement_sha256=supplement_sha,
                interaction_trace_sha256=supplement_trace_sha,
            )
        except (ImportError, RuntimeError) as exc:
            raise ManagedCurrentTurnError(
                f"teaching_resolution_session_commit_failed:{exc}"
            ) from exc
        session_resolution_receipt = str(
            (resolution.get("session_commit") or {}).get("receipt_sha256")
            or ""
        )
        if not SHA256_RE.fullmatch(session_resolution_receipt):
            raise ManagedCurrentTurnError(
                "teaching_resolution_session_receipt_invalid"
            )

    turn_status = "teaching_resolved" if corrected else "terminal_capture_verified"
    turn_value: dict[str, Any] = {
        "schema": private_evidence.TURN_RECEIPT_SCHEMA,
        "subject": "cs408",
        "status": turn_status,
        "context_id": final_context["context_id"],
        "first_context_id": canonical_context["context_id"],
        "request_id": final_context["request_id"],
        "session_id": final_context["session_id"],
        "item_id": final_context["item_id"],
        "feedback_sha256": final_feedback_sha,
        "first_result": canonical_context["classification"]["teaching_result"],
        "terminal_result": final_context["classification"]["teaching_result"],
        "first_answer_receipt_sha256": (
            first_answer.get("commit_receipt_sha256")
            or first_answer.get("review_commit_receipt_sha256")
        ),
        "session_feedback_receipt_sha256": (
            ((session_feedback or {}).get("session_commit") or {}).get(
                "receipt_sha256"
            )
            if session_feedback is not None
            else None
        ),
        "capture_id": binding["capture_id"],
        "capture_content_sha256": binding["payload_sha256"],
        "capture_payload_sha256": binding["payload_sha256"],
        "capture_idempotency_key": binding["idempotency_key"],
        "capture_event_sha256": binding["durable_event_sha256"],
        "capture_ledger_event_sha256": binding["event_sha256"],
        "capture_receipt_sha256": capture.get("receipt_sha256"),
        "capture_joint_manifest_sha256": binding[
            "durable_joint_manifest_sha256"
        ],
        "capture_bounded_manifest_sha256": binding[
            "durable_bounded_manifest_sha256"
        ],
        "capture_status": capture_model.PENDING_FORMAL_STATUS,
        "capture_binding_verified": True,
        "conversation_package_locator": final_package["locator"],
        "conversation_package_sha256": final_package["canonical_sha256"],
        "conversation_package_sealed": True,
        "resolution_attestation_locator": (
            attestation.get("locator") if attestation else None
        ),
        "resolution_attestation_sha256": (
            attestation.get("sha256") if attestation else None
        ),
        "trace_supplement": supplement,
        "session_resolution_receipt_sha256": session_resolution_receipt,
        "feedback_authorized": True,
        "navigation_policy": navigation_policy,
        "advance_allowed": navigation_policy == DEFAULT_NAVIGATION_POLICY,
        "next_display_receipt_sha256": None,
        "formal_write_count": 0,
    }
    turn_receipt = private_evidence.publish_metadata_object(
        turn_value, kind="turns", private_root=private_root
    )

    following: dict[str, Any] | None = None
    hold_receipt: dict[str, Any] | None = None
    if navigation_policy == DEFAULT_NAVIGATION_POLICY:
        following = next_item(
            repo,
            prior_turn_receipt_locator=turn_receipt["locator"],
            private_root=private_root,
        )
    else:
        try:
            import morning_review_session as morning_session

            session_dir = morning_session._session_dir(
                repo, final_context["session_id"]
            )
            with morning_session.session_lock(session_dir):
                state = morning_session._load_state(
                    session_dir, final_context["session_id"]
                )
                summary = morning_session.session_summary(state)
                already_paused = (
                    state.get("status") == "paused"
                    and state.get("pause_reason") == "user_pause"
                )
                session_complete = (
                    summary.get("unanswered_count") == 0
                    and all(
                        state["items"][item_id].get("resolution")
                        in morning_session.COMPLETE_RESOLUTIONS
                        for item_id in state["item_order"]
                    )
                )
            if session_complete:
                hold_receipt = morning_session.auto_close_completed_session(
                    repo, final_context["session_id"]
                )
            elif already_paused:
                hold_receipt = {
                    "action": "pause",
                    "status": "already_held",
                    "session_id": final_context["session_id"],
                    "pause_reason": "user_pause",
                    "formal_write_count": 0,
                }
            else:
                hold_receipt = morning_session.command_pause(
                    argparse.Namespace(
                        repo=str(repo),
                        session=final_context["session_id"],
                        reason="user_pause",
                    )
                )
        except (ImportError, RuntimeError) as exc:
            raise ManagedCurrentTurnError(
                f"terminal_hold_or_close_failed:{exc}"
            ) from exc
    status = (
        (
            "feedback_ready_session_complete"
            if isinstance(hold_receipt, dict)
            and hold_receipt.get("status") in {"closed", "already_closed"}
            else "feedback_ready_current_held"
        )
        if navigation_policy == "hold_current"
        else (
            "feedback_ready_session_complete"
            if following is not None and following.get("status") == "complete"
            else "feedback_and_next_ready"
        )
    )
    result = _answer_result(
        operation_id=operation_id,
        status=status,
        feedback_text=final_feedback,
        first_result=canonical_context["classification"]["teaching_result"],
        capture_status=capture_model.PENDING_FORMAL_STATUS,
        capture_id=binding["capture_id"],
        turn_receipt_locator=turn_receipt["locator"],
        following=following,
        recovery_locator=None,
        learner_evidence_write_count=1,
        navigation_policy=navigation_policy,
        capture_content_sha256=binding["payload_sha256"],
        trace_supplement=supplement,
    )
    result.update(
        {
            "terminal_result": final_context["classification"][
                "teaching_result"
            ],
            "capture_receipt_sha256": capture.get("receipt_sha256"),
            "capture_event_sha256": binding["durable_event_sha256"],
            "capture_ledger_event_sha256": binding["event_sha256"],
            "capture_binding_verified": True,
            "capture_joint_manifest_sha256": binding[
                "durable_joint_manifest_sha256"
            ],
            "capture_idempotency_key": binding["idempotency_key"],
            "conversation_package_locator": final_package["locator"],
            "conversation_package_sha256": final_package[
                "canonical_sha256"
            ],
            "conversation_package_sealed": True,
            "hold_or_close_receipt": hold_receipt,
        }
    )
    return result


def _normalize_attachments_json(
    raw: object | None,
) -> tuple[str, list[dict[str, Any]], list[dict[str, Any]]]:
    if raw is None:
        raw = '{"question_mode":"dialogue_only","attachments":[]}'
    if isinstance(raw, str):
        encoded = raw.encode("utf-8")
        if len(encoded) > MAX_ATTACHMENTS_JSON_BYTES:
            raise ManagedCurrentTurnError("attachments_json_too_large")
        try:
            value = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise ManagedCurrentTurnError("attachments_json_invalid") from exc
    else:
        encoded = json.dumps(
            raw, ensure_ascii=False, separators=(",", ":")
        ).encode("utf-8")
        if len(encoded) > MAX_ATTACHMENTS_JSON_BYTES:
            raise ManagedCurrentTurnError("attachments_json_too_large")
        value = raw
    if not isinstance(value, dict) or set(value) != {"question_mode", "attachments"}:
        raise ManagedCurrentTurnError("attachments_json_fields_invalid")
    question_mode = str(value.get("question_mode") or "").strip()
    descriptors = value.get("attachments")
    if question_mode not in {"dialogue_only", "image_question"}:
        raise ManagedCurrentTurnError("attachments_question_mode_invalid")
    if (
        not isinstance(descriptors, list)
        or len(descriptors) > conversation_package.MAX_ATTACHMENTS_PER_REVISION
    ):
        raise ManagedCurrentTurnError("attachments_count_invalid")
    writer_rows: list[dict[str, Any]] = []
    identity_rows: list[dict[str, Any]] = []
    for descriptor in descriptors:
        if not isinstance(descriptor, dict) or set(descriptor) != {
            "path",
            "sha256",
            "mime_type",
            "role",
            "label",
        }:
            raise ManagedCurrentTurnError("attachment_descriptor_fields_invalid")
        path_text = descriptor.get("path")
        if not isinstance(path_text, str) or not path_text or "\x00" in path_text:
            raise ManagedCurrentTurnError("attachment_path_invalid")
        path = Path(path_text).expanduser()
        try:
            before = path.lstat()
            if stat.S_ISLNK(before.st_mode) or not stat.S_ISREG(before.st_mode):
                raise ManagedCurrentTurnError("attachment_must_be_regular_non_symlink")
            if not 0 < before.st_size <= conversation_package.MAX_ATTACHMENT_BYTES:
                raise ManagedCurrentTurnError("attachment_size_invalid")
            with path.open("rb") as handle:
                info = os.fstat(handle.fileno())
                if not stat.S_ISREG(info.st_mode):
                    raise ManagedCurrentTurnError("attachment_must_be_regular_non_symlink")
                data = handle.read(conversation_package.MAX_ATTACHMENT_BYTES + 1)
            after = path.lstat()
        except OSError as exc:
            raise ManagedCurrentTurnError("attachment_read_failed") from exc
        if (
            stat.S_ISLNK(after.st_mode)
            or (before.st_dev, before.st_ino, before.st_size)
            != (after.st_dev, after.st_ino, after.st_size)
            or len(data) != before.st_size
        ):
            raise ManagedCurrentTurnError("attachment_changed_while_reading")
        digest = _sha256(data)
        declared_digest = str(descriptor.get("sha256") or "").strip().lower()
        if not SHA256_RE.fullmatch(declared_digest) or digest != declared_digest:
            raise ManagedCurrentTurnError("attachment_sha256_mismatch")
        mime_type = str(descriptor.get("mime_type") or "").strip().lower()
        role = str(descriptor.get("role") or "").strip()
        label = str(descriptor.get("label") or "").strip()
        if mime_type.startswith("image/"):
            try:
                _image_format, detected_mime = private_evidence._verified_image_format(data)
            except private_evidence.CurrentQuestionEvidenceError as exc:
                raise ManagedCurrentTurnError(str(exc)) from exc
            if mime_type != detected_mime:
                raise ManagedCurrentTurnError("attachment_mime_mismatch")
        if role not in conversation_package.ATTACHMENT_ROLES:
            raise ManagedCurrentTurnError("attachment_role_invalid")
        if not label or len(label.encode("utf-8")) > 160:
            raise ManagedCurrentTurnError("attachment_label_invalid")
        writer_rows.append(
            {"data": data, "mime_type": mime_type, "role": role, "label": label}
        )
        identity_rows.append(
            {
                "role": role,
                "label": label,
                "mime_type": mime_type,
                "sha256": digest,
                "byte_count": len(data),
            }
        )
    return question_mode, writer_rows, identity_rows


def _answer_result(
    *,
    operation_id: str,
    status: str,
    feedback_text: str | None,
    first_result: str | None,
    capture_status: str | None,
    capture_id: str | None,
    turn_receipt_locator: str | None,
    following: dict[str, Any] | None,
    recovery_locator: str | None,
    learner_evidence_write_count: int,
    navigation_policy: str = DEFAULT_NAVIGATION_POLICY,
    capture_content_sha256: str | None = None,
    trace_supplement: dict[str, Any] | None = None,
) -> dict[str, Any]:
    frozen_navigation_policy = _normalize_navigation_policy(
        navigation_policy
    )
    return {
        "schema": ANSWER_AND_NEXT_SCHEMA,
        "subject": "cs408",
        "status": status,
        "operation_id": operation_id,
        "navigation_policy": frozen_navigation_policy,
        "idempotent_replay": False,
        "feedback_authorized": feedback_text is not None,
        "feedback_text": feedback_text,
        "first_result": first_result,
        "capture_status": capture_status,
        "capture_id": capture_id,
        "capture_content_sha256": capture_content_sha256,
        "turn_receipt_locator": turn_receipt_locator,
        "next_item_published": (
            isinstance(following, dict) and following.get("status") == "published"
        ),
        "next_item": following,
        "recovery_locator": recovery_locator,
        "trace_supplement": trace_supplement,
        "learner_evidence_write_count": learner_evidence_write_count,
        "luna_call_count": 0,
        "formal_write_count": 0,
    }


def _answer_current_and_next_legacy(
    repo: str | Path,
    *,
    display_receipt_locator: str,
    choice: str,
    confidence: str,
    prompt_level: str,
    navigation_policy: str = DEFAULT_NAVIGATION_POLICY,
    interaction_trace: object | None = None,
    attachments_json: object | None = None,
    private_root: str | Path | None = None,
) -> dict[str, Any]:
    """Commit one first answer; advance only after the current item is resolved."""

    normalized_choice = str(choice or "").strip().upper()
    normalized_confidence = str(confidence or "").strip().lower()
    normalized_prompt = str(prompt_level or "").strip()
    normalized_navigation_policy = _normalize_navigation_policy(
        navigation_policy
    )
    if normalized_choice not in MANAGED_CHOICES:
        raise ManagedCurrentTurnError("choice_must_be_A_to_D_or_blank_uncertain")
    if normalized_confidence not in {"high", "medium", "low"}:
        raise ManagedCurrentTurnError("confidence_invalid")
    if normalized_prompt not in context_model.PROMPT_LEVELS:
        raise ManagedCurrentTurnError("prompt_level_invalid")
    normalized_trace = _trace_with_current_answer(
        context_model.normalize_interaction_trace(interaction_trace),
        normalized_choice,
    )
    question_mode, attachment_rows, attachment_identity = _normalize_attachments_json(
        attachments_json
    )
    if not isinstance(display_receipt_locator, str) or not display_receipt_locator.startswith(
        private_evidence.TURN_LOCATOR_PREFIX
    ):
        raise ManagedCurrentTurnError("display_receipt_locator_invalid")
    display_digest = display_receipt_locator.removeprefix(
        private_evidence.TURN_LOCATOR_PREFIX
    )
    if not SHA256_RE.fullmatch(display_digest):
        raise ManagedCurrentTurnError("display_receipt_locator_invalid")
    default_operation_material = {
        "display_receipt_locator": display_receipt_locator,
        "choice": normalized_choice,
        "confidence": normalized_confidence,
        "prompt_level": normalized_prompt,
        "interaction_trace": normalized_trace,
        "question_mode": question_mode,
        "attachments": attachment_identity,
    }
    operation_material = dict(default_operation_material)
    # Preserve the historical operation identity for the default route. The
    # absence of this field is the hash-bound spelling of publish_successor;
    # hold_current is explicit and therefore receives a distinct operation ID.
    if normalized_navigation_policy != DEFAULT_NAVIGATION_POLICY:
        operation_material["navigation_policy"] = normalized_navigation_policy
    input_sha = _sha256(_json_bytes(operation_material))
    operation_id = "AON-" + input_sha.upper()
    counterpart_material = dict(default_operation_material)
    if normalized_navigation_policy == DEFAULT_NAVIGATION_POLICY:
        counterpart_material["navigation_policy"] = "hold_current"
    counterpart_sha = _sha256(_json_bytes(counterpart_material))
    counterpart_operation_id = "AON-" + counterpart_sha.upper()
    root = Path(repo).expanduser().resolve()
    if not root.is_dir():
        raise ManagedCurrentTurnError("repository_missing")

    with private_evidence.answer_display_lock(
        display_digest, private_root=private_root
    ), private_evidence.answer_operation_lock(
        operation_id, private_root=private_root
    ):
        operation = private_evidence.read_answer_operation(
            operation_id, private_root=private_root
        )
        counterpart = private_evidence.read_answer_operation(
            counterpart_operation_id, private_root=private_root
        )
        if counterpart is not None:
            counterpart_policy = _operation_navigation_policy(counterpart)
            if counterpart_policy == normalized_navigation_policy:
                raise ManagedCurrentTurnError(
                    "answer_operation_navigation_policy_hash_drifted"
                )
            raise ManagedCurrentTurnError(
                "answer_display_navigation_policy_conflict"
            )
        if operation is not None and operation.get("input_sha256") != input_sha:
            raise ManagedCurrentTurnError("answer_operation_input_drifted")
        if operation is not None:
            _operation_navigation_policy(
                operation, expected=normalized_navigation_policy
            )
        if operation is not None and operation.get("status") == "complete":
            if (
                _result_navigation_policy(operation["result"])
                != normalized_navigation_policy
            ):
                raise ManagedCurrentTurnError(
                    "answer_operation_result_navigation_policy_drifted"
                )
            replay = dict(operation["result"])
            replay.setdefault(
                "navigation_policy", normalized_navigation_policy
            )
            replay["idempotent_replay"] = True
            replay["learner_evidence_write_count"] = 0
            replay["formal_write_count"] = 0
            return replay
        lifecycle = private_evidence.read_answer_display_lifecycle(
            display_digest, private_root=private_root
        )
        if lifecycle is not None and lifecycle.get("status") == "resolved":
            replay = dict(lifecycle["resolution"]["result"])
            if (
                _result_navigation_policy(replay)
                != normalized_navigation_policy
            ):
                raise ManagedCurrentTurnError(
                    "resolved_display_navigation_policy_conflict"
                )
            replay.setdefault(
                "navigation_policy", normalized_navigation_policy
            )
            replay["idempotent_replay"] = True
            replay["learner_evidence_write_count"] = 0
            if operation is None:
                now = dt.datetime.now(ZoneInfo(context_model.TIMEZONE)).isoformat(
                    timespec="microseconds"
                )
                operation = {
                    "schema": private_evidence.ANSWER_OPERATION_SCHEMA,
                    "operation_id": operation_id,
                    "input_sha256": input_sha,
                    "status": "prepared",
                    "event_time": now,
                    "prepared": {
                        "mode": "resolved_replay",
                        "navigation_policy": normalized_navigation_policy,
                    },
                    "result": None,
                    "formal_write_count": 0,
                }
            return _complete_answer_operation(
                operation, replay, private_root=private_root
            )

        try:
            import morning_review_prepared_pack_408 as prepared_pack
        except ImportError as exc:
            raise ManagedCurrentTurnError("prepared_pack_module_unavailable") from exc

        if lifecycle is not None:
            if operation is None:
                event_time = dt.datetime.now(
                    ZoneInfo(context_model.TIMEZONE)
                ).isoformat(timespec="microseconds")
                followup = prepared_pack.prepare_followup_attempt_from_display(
                    root,
                    display_receipt_locator=display_receipt_locator,
                    choice=normalized_choice,
                    confidence=normalized_confidence,
                    prompt_level=normalized_prompt,
                    private_root=private_root,
                )
                operation = {
                    "schema": private_evidence.ANSWER_OPERATION_SCHEMA,
                    "operation_id": operation_id,
                    "input_sha256": input_sha,
                    "status": "prepared",
                    "event_time": event_time,
                    "prepared": {
                        "mode": "teaching_followup",
                        "navigation_policy": normalized_navigation_policy,
                        "followup": followup,
                        "interaction_trace": normalized_trace,
                    },
                    "result": None,
                    "formal_write_count": 0,
                }
                private_evidence.write_answer_operation(
                    operation, private_root=private_root
                )
            prepared = operation["prepared"]
            frozen_navigation_policy = _operation_navigation_policy(
                operation, expected=normalized_navigation_policy
            )
            followup = prepared["followup"]
            first_turn = lifecycle["first_turn"]
            if (
                first_turn.get("capture_status") != capture_model.PENDING_FORMAL_STATUS
                or not first_turn.get("capture_id")
                or not SHA256_RE.fullmatch(
                    str(first_turn.get("capture_receipt_sha256") or "")
                )
            ):
                raise ManagedCurrentTurnError(
                    "current_capture_recovery_required_before_correction"
                )
            cumulative_trace = _merge_interaction_traces(
                lifecycle["interaction_trace"], prepared["interaction_trace"]
            )
            if cumulative_trace != lifecycle["interaction_trace"]:
                lifecycle = {**lifecycle, "interaction_trace": cumulative_trace}
                private_evidence.write_answer_display_lifecycle(
                    lifecycle, private_root=private_root
                )
            package_receipt = _append_followup_conversation_package(
                first_turn,
                cumulative_trace=cumulative_trace,
                feedback_text=followup["feedback_text"],
                attachments=attachment_rows,
                seal=followup.get("choice_result") == "correct",
                runtime_private_root=private_root,
            )
            if followup.get("choice_result") != "correct":
                result = _answer_result(
                    operation_id=operation_id,
                    status="feedback_ready_continue_current",
                    feedback_text=followup["feedback_text"],
                    first_result=lifecycle["first_result"],
                    capture_status=first_turn.get("capture_status"),
                    capture_id=first_turn.get("capture_id"),
                    turn_receipt_locator=first_turn.get("turn_receipt_locator"),
                    following=None,
                    recovery_locator=None,
                    learner_evidence_write_count=0,
                    navigation_policy=frozen_navigation_policy,
                )
                result["conversation_package_locator"] = package_receipt["locator"]
                result["conversation_package_sha256"] = package_receipt[
                    "canonical_sha256"
                ]
                return _complete_answer_operation(
                    operation, result, private_root=private_root
                )

            trace = cumulative_trace
            trace_sha = _sha256(_json_bytes(trace))
            evidence_manifest_sha = str(package_receipt["canonical_sha256"])
            capture_id = str(first_turn.get("capture_id") or "")
            capture_receipt_sha = str(
                first_turn.get("capture_receipt_sha256") or ""
            )
            if (
                not SHA256_RE.fullmatch(evidence_manifest_sha)
                or not capture_id
                or not SHA256_RE.fullmatch(capture_receipt_sha)
            ):
                raise ManagedCurrentTurnError(
                    "resolved_trace_original_capture_binding_missing"
                )
            attestation = private_evidence.publish_metadata_object(
                {
                    "schema": private_evidence.TURN_RECEIPT_SCHEMA,
                    "status": "teaching_resolution_attested",
                    "attestation_schema": "teaching-resolution-attestation-v1",
                    "context_id": first_turn["context_id"],
                    "session_id": followup["session_id"],
                    "item_id": followup["item_id"],
                    "capture_id": capture_id,
                    "capture_receipt_sha256": capture_receipt_sha,
                    "evidence_manifest_sha256": evidence_manifest_sha,
                    "interaction_trace_sha256": trace_sha,
                    "resolution_choice_result": "correct",
                    "event_time": operation["event_time"],
                    "advance_allowed": False,
                    "formal_write_count": 0,
                },
                kind="turns",
                private_root=private_root,
            )
            events = [
                {
                    "role": row["role"],
                    "kind": row["kind"],
                    "text": row["text"],
                    "observed_at": None,
                }
                for row in trace["events"]
            ]
            supplement = private_evidence.publish_trace_supplement(
                private_root=private_root,
                capture_id=capture_id,
                context_id=first_turn["context_id"],
                item_id=followup["item_id"],
                evidence_manifest_sha256=evidence_manifest_sha,
                created_at=operation["event_time"],
                supplement_kind="resolved_trace",
                resolution_receipt_sha256=attestation["sha256"],
                events=events,
            )
            if not isinstance(supplement, dict):
                raise ManagedCurrentTurnError("trace_supplement_receipt_invalid")
            supplement_sha = str(
                supplement.get("object_sha")
                or supplement.get("object_sha256")
                or supplement.get("sha256")
                or ""
            )
            if not SHA256_RE.fullmatch(supplement_sha):
                raise ManagedCurrentTurnError("trace_supplement_hash_invalid")
            supplement_trace_sha = str(
                supplement.get("interaction_trace_sha256") or ""
            )
            if not SHA256_RE.fullmatch(supplement_trace_sha):
                raise ManagedCurrentTurnError(
                    "trace_supplement_interaction_hash_invalid"
                )
            try:
                import morning_review_session as morning_session
            except ImportError as exc:
                raise ManagedCurrentTurnError(
                    "morning_session_module_unavailable"
                ) from exc
            try:
                teaching_resolution = morning_session.mark_teaching_resolved(
                    root,
                    followup["session_id"],
                    followup["item_id"],
                    capture_id=capture_id,
                    capture_receipt_sha256=capture_receipt_sha,
                    resolution_attestation_sha256=attestation["sha256"],
                    trace_supplement_sha256=supplement_sha,
                    interaction_trace_sha256=supplement_trace_sha,
                )
            except morning_session.SessionError as exc:
                raise ManagedCurrentTurnError(
                    f"teaching_resolution_session_commit_failed:{exc}"
                ) from exc
            session_resolution_receipt = str(
                (teaching_resolution.get("session_commit") or {}).get(
                    "receipt_sha256"
                )
                or ""
            )
            if not SHA256_RE.fullmatch(session_resolution_receipt):
                raise ManagedCurrentTurnError(
                    "teaching_resolution_session_receipt_invalid"
                )
            final_turn = private_evidence.publish_metadata_object(
                {
                    "schema": private_evidence.TURN_RECEIPT_SCHEMA,
                    "subject": "cs408",
                    "status": "teaching_resolved",
                    "context_id": first_turn["context_id"],
                    "session_id": followup["session_id"],
                    "item_id": followup["item_id"],
                    "capture_id": capture_id,
                    "capture_content_sha256": first_turn.get(
                        "capture_content_sha256"
                    ),
                    "capture_receipt_sha256": capture_receipt_sha,
                    "evidence_manifest_sha256": evidence_manifest_sha,
                    "resolution_attestation_locator": attestation["locator"],
                    "resolution_attestation_sha256": attestation["sha256"],
                    "trace_supplement": supplement,
                    "session_resolution_receipt_sha256": (
                        session_resolution_receipt
                    ),
                    "mastery_effect": "none",
                    "retention_effect": "none",
                    "independent_repair": False,
                    "feedback_sha256": _sha256(
                        followup["feedback_text"].encode("utf-8")
                    ),
                    "advance_allowed": True,
                    "formal_write_count": 0,
                },
                kind="turns",
                private_root=private_root,
            )
            following: dict[str, Any] | None = None
            if frozen_navigation_policy == DEFAULT_NAVIGATION_POLICY:
                following = next_item(
                    root,
                    prior_turn_receipt_locator=final_turn["locator"],
                    private_root=private_root,
                )
            result = _answer_result(
                operation_id=operation_id,
                status=(
                    "feedback_ready_current_held"
                    if frozen_navigation_policy == "hold_current"
                    else (
                        "feedback_ready_session_complete"
                        if following is not None
                        and following.get("status") == "complete"
                        else "feedback_and_next_ready"
                    )
                ),
                feedback_text=followup["feedback_text"],
                first_result=lifecycle["first_result"],
                capture_status=first_turn.get("capture_status"),
                capture_id=capture_id,
                turn_receipt_locator=final_turn["locator"],
                following=following,
                recovery_locator=None,
                learner_evidence_write_count=0,
                navigation_policy=frozen_navigation_policy,
                capture_content_sha256=first_turn.get(
                    "capture_content_sha256"
                ),
                trace_supplement=supplement,
            )
            result["conversation_package_locator"] = package_receipt["locator"]
            result["conversation_package_sha256"] = package_receipt[
                "canonical_sha256"
            ]
            private_evidence.write_answer_display_lifecycle(
                {
                    **lifecycle,
                    "status": "resolved",
                    "resolution": {
                        "operation_id": operation_id,
                        "attestation": attestation,
                        "supplement": supplement,
                        "turn_receipt": final_turn,
                        "result": result,
                    },
                },
                private_root=private_root,
            )
            return _complete_answer_operation(
                operation, result, private_root=private_root
            )

        if operation is None:
            display_receipt = private_evidence.read_metadata_object(
                display_receipt_locator,
                kind="turns",
                private_root=private_root,
            )
            if display_receipt.get("status") != "display_published":
                raise ManagedCurrentTurnError("display_receipt_is_not_current_surface")
            session_id = str(display_receipt.get("session_id") or "")
            item_id = str(display_receipt.get("item_id") or "")
            surface_sha = str(display_receipt.get("surface_sha256") or "")
            if not session_id or not item_id or not SHA256_RE.fullmatch(surface_sha):
                raise ManagedCurrentTurnError("display_receipt_binding_incomplete")
            event_time = dt.datetime.now(ZoneInfo(context_model.TIMEZONE)).isoformat(
                timespec="microseconds"
            )
            request_id = operation_id
            preparation, source_identity_fallback = (
                _prepare_current_turn_with_source_compatibility(
                    root,
                    display_receipt=display_receipt,
                    display_receipt_locator=display_receipt_locator,
                    choice=normalized_choice,
                    confidence=normalized_confidence,
                    prompt_level=normalized_prompt,
                    request_id=request_id,
                    event_time=event_time,
                    display_surface_sha256=surface_sha,
                    interaction_trace=normalized_trace,
                    semantic_assessment={},
                    private_root=private_root,
                    prepared_pack=prepared_pack,
                )
            )
            prepared = {
                "mode": "first_answer",
                "navigation_policy": normalized_navigation_policy,
                "context": preparation["context"],
                "grader_capsule_locator": preparation["grader_capsule_locator"],
                "grader_capsule_sha256": preparation["grader_capsule_sha256"],
                "feedback_text": preparation["_private_frozen_feedback_text"],
                "interaction_trace": preparation["_private_interaction_trace"],
                "scheduling_binding": preparation.get("scheduling_binding"),
                "scheduling_binding_status": preparation.get(
                    "scheduling_binding_status"
                ),
                "source_identity_fallback": source_identity_fallback,
            }
            operation = {
                "schema": private_evidence.ANSWER_OPERATION_SCHEMA,
                "operation_id": operation_id,
                "input_sha256": input_sha,
                "status": "prepared",
                "event_time": event_time,
                "prepared": prepared,
                "result": None,
                "formal_write_count": 0,
            }
            private_evidence.write_answer_operation(
                operation, private_root=private_root
            )
        else:
            prepared = operation["prepared"]
        frozen_navigation_policy = _operation_navigation_policy(
            operation, expected=normalized_navigation_policy
        )
        cached_current = prepared.get("current_result")
        if isinstance(cached_current, dict):
            current = dict(cached_current)
        else:
            capsule = private_evidence.read_evaluation_capsule(
                prepared["grader_capsule_locator"],
                expected_sha256=prepared["grader_capsule_sha256"],
                private_root=private_root,
            )
            source_identity_fallback = _stored_source_identity_fallback(
                prepared.get("source_identity_fallback")
            )
            if source_identity_fallback is None:
                current = run_current_question_turn(
                    root,
                    prepared["context"],
                    feedback_text=prepared["feedback_text"],
                    private_evaluation=capsule["evaluation_evidence"],
                    interaction_trace=prepared["interaction_trace"],
                    attachments=attachment_rows,
                    question_mode=question_mode,
                    private_root=private_root,
                )
            else:
                with context_model.verified_source_identity_fallback(
                    raw_source_label=source_identity_fallback[
                        "raw_source_label"
                    ],
                    machine_source_id=source_identity_fallback[
                        "machine_source_id"
                    ],
                    scheduling_binding_sha256=source_identity_fallback[
                        "scheduling_binding_sha256"
                    ],
                    binding_verification=source_identity_fallback[
                        "binding_verification"
                    ],
                ):
                    current = run_current_question_turn(
                        root,
                        prepared["context"],
                        feedback_text=prepared["feedback_text"],
                        private_evaluation=capsule["evaluation_evidence"],
                        interaction_trace=prepared["interaction_trace"],
                        attachments=attachment_rows,
                        question_mode=question_mode,
                        private_root=private_root,
                    )
            operation = {
                **operation,
                "prepared": {**prepared, "current_result": current},
            }
            private_evidence.write_answer_operation(
                operation, private_root=private_root
            )
        needs_teaching_lifecycle = (
            current.get("status") == "feedback_ready"
            and current.get("first_result") in {"wrong", "partial", "uncertain"}
        )
        hold_for_teaching = (
            needs_teaching_lifecycle
            and current.get("capture_status") == capture_model.PENDING_FORMAL_STATUS
        )
        following: dict[str, Any] | None = None
        if (
            current.get("advance_allowed") is True
            and not hold_for_teaching
            and frozen_navigation_policy == DEFAULT_NAVIGATION_POLICY
        ):
            following = next_item(
                root,
                prior_turn_receipt_locator=str(
                    current.get("turn_receipt_locator") or ""
                ),
                private_root=private_root,
            )
        if current.get("status") != "feedback_ready":
            status = "recovery_required"
        elif hold_for_teaching:
            status = "feedback_ready_continue_current"
        elif (
            current.get("advance_allowed") is True
            and frozen_navigation_policy == "hold_current"
        ):
            status = "feedback_ready_current_held"
        elif following is None:
            status = "feedback_ready_recovery_required"
        elif following.get("status") == "complete":
            status = "feedback_ready_session_complete"
        else:
            status = "feedback_and_next_ready"
        result = _answer_result(
            operation_id=operation_id,
            status=status,
            feedback_text=(
                current.get("feedback_text")
                if current.get("feedback_authorized") is True
                else None
            ),
            first_result=current.get("first_result"),
            capture_status=current.get("capture_status"),
            capture_id=current.get("capture_id"),
            turn_receipt_locator=current.get("turn_receipt_locator"),
            following=following,
            recovery_locator=current.get("recovery_locator"),
            learner_evidence_write_count=1,
            navigation_policy=frozen_navigation_policy,
            capture_content_sha256=current.get("capture_content_sha256"),
        )
        result["conversation_package_locator"] = current.get(
            "conversation_package_locator"
        )
        result["conversation_package_sha256"] = current.get(
            "conversation_package_sha256"
        )
        if needs_teaching_lifecycle:
            private_evidence.write_answer_display_lifecycle(
                {
                    "schema": private_evidence.ANSWER_DISPLAY_LIFECYCLE_SCHEMA,
                    "display_receipt_sha256": display_digest,
                    "status": "first_recorded",
                    "first_operation_id": operation_id,
                    "first_result": current["first_result"],
                    "first_turn": current,
                    "interaction_trace": prepared["interaction_trace"],
                    "resolution": None,
                    "formal_write_count": 0,
                },
                private_root=private_root,
            )
        return _complete_answer_operation(
            operation, result, private_root=private_root
        )


@study_commit_boundary
def answer_current_and_next(
    repo: str | Path,
    *,
    display_receipt_locator: str,
    choice: str,
    confidence: str,
    prompt_level: str,
    navigation_policy: str = DEFAULT_NAVIGATION_POLICY,
    interaction_trace: object | None = None,
    semantic_assessment: object | None = None,
    attachments_json: object | None = None,
    private_root: str | Path | None = None,
) -> dict[str, Any]:
    """Buffer one attempt or atomically finalize one resolved session item."""

    normalized_choice = str(choice or "").strip().upper()
    normalized_confidence = str(confidence or "").strip().lower()
    normalized_prompt = str(prompt_level or "").strip()
    frozen_navigation_policy = _normalize_navigation_policy(navigation_policy)
    if normalized_choice not in MANAGED_CHOICES:
        raise ManagedCurrentTurnError("choice_must_be_A_to_D_or_blank_uncertain")
    if normalized_confidence not in {"high", "medium", "low"}:
        raise ManagedCurrentTurnError("confidence_invalid")
    if normalized_prompt not in context_model.PROMPT_LEVELS:
        raise ManagedCurrentTurnError("prompt_level_invalid")
    normalized_trace = _trace_with_current_answer(
        context_model.normalize_interaction_trace(interaction_trace),
        normalized_choice,
    )
    if semantic_assessment is None:
        normalized_semantic: dict[str, Any] = {}
    elif isinstance(semantic_assessment, str):
        try:
            decoded_semantic = json.loads(semantic_assessment)
        except json.JSONDecodeError as exc:
            raise ManagedCurrentTurnError(
                "semantic_assessment_json_invalid"
            ) from exc
        if not isinstance(decoded_semantic, dict):
            raise ManagedCurrentTurnError("semantic_assessment_must_be_object")
        normalized_semantic = decoded_semantic
    elif isinstance(semantic_assessment, dict):
        normalized_semantic = json.loads(
            json.dumps(semantic_assessment, ensure_ascii=False)
        )
    else:
        raise ManagedCurrentTurnError("semantic_assessment_must_be_object")
    question_mode, attachment_rows, attachment_identity = (
        _normalize_attachments_json(attachments_json)
    )
    if (
        not isinstance(display_receipt_locator, str)
        or not display_receipt_locator.startswith(
            private_evidence.TURN_LOCATOR_PREFIX
        )
    ):
        raise ManagedCurrentTurnError("display_receipt_locator_invalid")
    display_digest = display_receipt_locator.removeprefix(
        private_evidence.TURN_LOCATOR_PREFIX
    )
    if not SHA256_RE.fullmatch(display_digest):
        raise ManagedCurrentTurnError("display_receipt_locator_invalid")
    root = Path(repo).expanduser().resolve()
    if not root.is_dir():
        raise ManagedCurrentTurnError("repository_missing")

    operation_material = {
        "schema": "managed-terminal-buffer-operation-v2",
        "display_receipt_locator": display_receipt_locator,
        "choice": normalized_choice,
        "confidence": normalized_confidence,
        "prompt_level": normalized_prompt,
        "semantic_assessment": normalized_semantic,
        "interaction_trace": normalized_trace,
        "question_mode": question_mode,
        "attachments": attachment_identity,
        "navigation_policy": frozen_navigation_policy,
    }
    input_sha = _sha256(_json_bytes(operation_material))
    operation_id = "AON-" + input_sha.upper()
    counterpart_material = {
        **operation_material,
        "navigation_policy": (
            "hold_current"
            if frozen_navigation_policy == DEFAULT_NAVIGATION_POLICY
            else DEFAULT_NAVIGATION_POLICY
        ),
    }
    counterpart_id = "AON-" + _sha256(
        _json_bytes(counterpart_material)
    ).upper()

    with private_evidence.answer_display_lock(
        display_digest, private_root=private_root
    ), private_evidence.answer_operation_lock(
        operation_id, private_root=private_root
    ):
        operation = private_evidence.read_answer_operation(
            operation_id, private_root=private_root
        )
        counterpart = private_evidence.read_answer_operation(
            counterpart_id, private_root=private_root
        )
        if counterpart is not None:
            raise ManagedCurrentTurnError(
                "answer_display_navigation_policy_conflict"
            )
        if operation is not None and operation.get("input_sha256") != input_sha:
            raise ManagedCurrentTurnError("answer_operation_input_drifted")
        if operation is not None and operation.get("status") == "complete":
            replay = dict(operation["result"])
            replay["idempotent_replay"] = True
            replay["learner_evidence_write_count"] = 0
            replay["formal_write_count"] = 0
            return replay

        lifecycle = private_evidence.read_answer_display_lifecycle(
            display_digest, private_root=private_root
        )
        if lifecycle is not None and lifecycle.get("status") == "resolved":
            replay = dict(lifecycle["resolution"]["result"])
            if _result_navigation_policy(replay) != frozen_navigation_policy:
                raise ManagedCurrentTurnError(
                    "resolved_display_navigation_policy_conflict"
                )
            replay["idempotent_replay"] = True
            replay["learner_evidence_write_count"] = 0
            if operation is None:
                operation = {
                    "schema": private_evidence.ANSWER_OPERATION_SCHEMA,
                    "operation_id": operation_id,
                    "input_sha256": input_sha,
                    "status": "prepared",
                    "event_time": dt.datetime.now(
                        ZoneInfo(context_model.TIMEZONE)
                    ).isoformat(timespec="microseconds"),
                    "prepared": {
                        "mode": "resolved_replay_v2",
                        "navigation_policy": frozen_navigation_policy,
                    },
                    "result": None,
                    "formal_write_count": 0,
                }
            return _complete_answer_operation(
                operation, replay, private_root=private_root
            )

        try:
            import morning_review_prepared_pack_408 as prepared_pack
        except ImportError as exc:
            raise ManagedCurrentTurnError(
                "prepared_pack_module_unavailable"
            ) from exc

        if operation is None:
            event_time = dt.datetime.now(
                ZoneInfo(context_model.TIMEZONE)
            ).isoformat(timespec="microseconds")
            if lifecycle is None:
                display_receipt = private_evidence.read_metadata_object(
                    display_receipt_locator,
                    kind="turns",
                    private_root=private_root,
                )
                if display_receipt.get("status") != "display_published":
                    raise ManagedCurrentTurnError(
                        "display_receipt_is_not_current_surface"
                    )
                surface_sha = str(
                    display_receipt.get("surface_sha256") or ""
                )
                preparation, source_fallback = (
                    _prepare_current_turn_with_source_compatibility(
                        root,
                        display_receipt=display_receipt,
                        display_receipt_locator=display_receipt_locator,
                        choice=normalized_choice,
                        confidence=normalized_confidence,
                        prompt_level=normalized_prompt,
                        request_id=operation_id,
                        event_time=event_time,
                        display_surface_sha256=surface_sha,
                        interaction_trace=normalized_trace,
                        semantic_assessment=normalized_semantic,
                        private_root=private_root,
                        prepared_pack=prepared_pack,
                    )
                )
                prepared = {
                    "mode": "first_attempt_v2",
                    "navigation_policy": frozen_navigation_policy,
                    "context": preparation["context"],
                    "grader_capsule_locator": preparation[
                        "grader_capsule_locator"
                    ],
                    "grader_capsule_sha256": preparation[
                        "grader_capsule_sha256"
                    ],
                    "feedback_text": preparation[
                        "_private_frozen_feedback_text"
                    ],
                    "interaction_trace": preparation[
                        "_private_interaction_trace"
                    ],
                    "semantic_assessment": normalized_semantic,
                    "semantic_assessment_sha256": preparation[
                        "semantic_assessment_sha256"
                    ],
                    "interaction_trace_sha256": preparation[
                        "interaction_trace_sha256"
                    ],
                    "feedback_sha256": preparation["feedback_sha256"],
                    "terminal_verdict": preparation["terminal_verdict"],
                    "terminal_verdict_sha256": preparation[
                        "terminal_verdict_sha256"
                    ],
                    "source_identity_fallback": source_fallback,
                }
            else:
                first_turn = lifecycle.get("first_turn") or {}
                if first_turn.get("capture_id"):
                    raise ManagedCurrentTurnError(
                        "legacy_first_answer_capture_requires_manual_closeout"
                    )
                followup = prepared_pack.prepare_followup_attempt_from_display(
                    root,
                    display_receipt_locator=display_receipt_locator,
                    choice=normalized_choice,
                    confidence=normalized_confidence,
                    prompt_level=normalized_prompt,
                    request_id=operation_id,
                    event_time=event_time,
                    interaction_trace=normalized_trace,
                    semantic_assessment=normalized_semantic,
                    private_root=private_root,
                )
                prepared = {
                    "mode": "followup_attempt_v2",
                    "navigation_policy": frozen_navigation_policy,
                    "followup": followup,
                    "semantic_assessment": normalized_semantic,
                    "semantic_assessment_sha256": followup[
                        "semantic_assessment_sha256"
                    ],
                    "interaction_trace_sha256": followup[
                        "interaction_trace_sha256"
                    ],
                    "feedback_sha256": followup["feedback_sha256"],
                    "terminal_verdict": followup["terminal_verdict"],
                    "terminal_verdict_sha256": followup[
                        "terminal_verdict_sha256"
                    ],
                    "interaction_trace": followup.get(
                        "interaction_trace", normalized_trace
                    ),
                }
            operation = {
                "schema": private_evidence.ANSWER_OPERATION_SCHEMA,
                "operation_id": operation_id,
                "input_sha256": input_sha,
                "status": "prepared",
                "event_time": event_time,
                "prepared": prepared,
                "result": None,
                "formal_write_count": 0,
            }
            private_evidence.write_answer_operation(
                operation, private_root=private_root
            )
        else:
            prepared = operation["prepared"]
        if _operation_navigation_policy(operation) != frozen_navigation_policy:
            raise ManagedCurrentTurnError(
                "answer_operation_navigation_policy_drifted"
            )

        mode = str(prepared.get("mode") or "")
        if mode == "first_attempt_v2":
            context = _validated_prepared_context(
                prepared["context"],
                prepared.get("source_identity_fallback"),
            )
            capsule = private_evidence.read_evaluation_capsule(
                prepared["grader_capsule_locator"],
                expected_sha256=prepared["grader_capsule_sha256"],
                private_root=private_root,
            )
            _validate_controlled_assessment(
                context, capsule["evaluation_evidence"]
            )
            _verify_frozen_terminal_verdict(
                prepared,
                context=context,
                interaction_trace=prepared["interaction_trace"],
                feedback_text=prepared["feedback_text"],
            )
            terminal = _terminal_resolved(context)
            package_receipt = _write_current_conversation_package(
                context,
                private_evaluation=capsule["evaluation_evidence"],
                interaction_trace=prepared["interaction_trace"],
                feedback_text=prepared["feedback_text"],
                attachments=attachment_rows,
                question_mode=question_mode,
                seal=terminal,
                private_root=private_root,
            )
            if not terminal:
                result = _answer_result(
                    operation_id=operation_id,
                    status="feedback_ready_continue_current",
                    feedback_text=prepared["feedback_text"],
                    first_result=context["classification"]["teaching_result"],
                    capture_status=BUFFERING_STATUS,
                    capture_id=None,
                    turn_receipt_locator=None,
                    following=None,
                    recovery_locator=None,
                    learner_evidence_write_count=1,
                    navigation_policy=frozen_navigation_policy,
                )
                result.update(
                    {
                        "conversation_package_locator": package_receipt[
                            "locator"
                        ],
                        "conversation_package_sha256": package_receipt[
                            "canonical_sha256"
                        ],
                        "conversation_package_sealed": False,
                        "pending_formal_write_count": 0,
                    }
                )
                private_evidence.write_answer_display_lifecycle(
                    {
                        "schema": private_evidence.ANSWER_DISPLAY_LIFECYCLE_SCHEMA,
                        "display_receipt_sha256": display_digest,
                        "status": "first_recorded",
                        "first_operation_id": operation_id,
                        "first_result": context["classification"][
                            "teaching_result"
                        ],
                        "first_turn": {
                            **result,
                            "context": prepared["context"],
                            "source_identity_fallback": prepared.get(
                                "source_identity_fallback"
                            ),
                            "feedback_text": prepared["feedback_text"],
                            "grader_capsule_locator": prepared[
                                "grader_capsule_locator"
                            ],
                            "grader_capsule_sha256": prepared[
                                "grader_capsule_sha256"
                            ],
                        },
                        "interaction_trace": prepared["interaction_trace"],
                        "resolution": None,
                        "formal_write_count": 0,
                    },
                    private_root=private_root,
                )
                return _complete_answer_operation(
                    operation, result, private_root=private_root
                )
            try:
                result = _terminal_package_capture_and_navigation(
                    root,
                    operation_id=operation_id,
                    canonical_context=context,
                    final_context=context,
                    first_feedback_text=prepared["feedback_text"],
                    final_feedback_text=prepared["feedback_text"],
                    final_package_receipt=package_receipt,
                    interaction_trace=prepared["interaction_trace"],
                    navigation_policy=frozen_navigation_policy,
                    private_root=private_root,
                )
            except (RuntimeError, OSError) as exc:
                result = _answer_result(
                    operation_id=operation_id,
                    status="recovery_required",
                    feedback_text=prepared["feedback_text"],
                    first_result=context["classification"]["teaching_result"],
                    capture_status="capture_pending_recovery",
                    capture_id=None,
                    turn_receipt_locator=None,
                    following=None,
                    recovery_locator=None,
                    learner_evidence_write_count=0,
                    navigation_policy=frozen_navigation_policy,
                )
                result.update(
                    {
                        "reason_code": str(exc)[:320],
                        "retry_same_operation": True,
                        "conversation_package_locator": package_receipt[
                            "locator"
                        ],
                        "conversation_package_sha256": package_receipt[
                            "canonical_sha256"
                        ],
                        "conversation_package_sealed": True,
                    }
                )
                return result
            return _complete_answer_operation(
                operation, result, private_root=private_root
            )

        if mode != "followup_attempt_v2" or lifecycle is None:
            raise ManagedCurrentTurnError("answer_operation_mode_invalid")
        first_turn = lifecycle["first_turn"]
        canonical_context = _validated_prepared_context(
            first_turn["context"],
            first_turn.get("source_identity_fallback"),
        )
        followup = prepared["followup"]
        final_context = _validated_prepared_context(
            followup["context"], followup.get("source_identity_fallback")
        )
        final_capsule = private_evidence.read_evaluation_capsule(
            followup["grader_capsule_locator"],
            expected_sha256=followup["grader_capsule_sha256"],
            private_root=private_root,
        )
        _validate_controlled_assessment(
            final_context, final_capsule["evaluation_evidence"]
        )
        _verify_frozen_terminal_verdict(
            prepared,
            context=final_context,
            interaction_trace=prepared["interaction_trace"],
            feedback_text=followup["feedback_text"],
        )
        cumulative_trace = _merge_interaction_traces(
            lifecycle["interaction_trace"], prepared["interaction_trace"]
        )
        terminal = _terminal_resolved(final_context)
        package_receipt = _append_followup_conversation_package(
            first_turn,
            cumulative_trace=cumulative_trace,
            feedback_text=followup["feedback_text"],
            attachments=attachment_rows,
            seal=terminal,
            runtime_private_root=private_root,
        )
        updated_lifecycle = {
            **lifecycle,
            "interaction_trace": cumulative_trace,
            "first_turn": {
                **first_turn,
                "conversation_package_locator": package_receipt["locator"],
                "conversation_package_sha256": package_receipt[
                    "canonical_sha256"
                ],
            },
        }
        private_evidence.write_answer_display_lifecycle(
            updated_lifecycle, private_root=private_root
        )
        if not terminal:
            result = _answer_result(
                operation_id=operation_id,
                status="feedback_ready_continue_current",
                feedback_text=followup["feedback_text"],
                first_result=lifecycle["first_result"],
                capture_status=BUFFERING_STATUS,
                capture_id=None,
                turn_receipt_locator=None,
                following=None,
                recovery_locator=None,
                learner_evidence_write_count=1,
                navigation_policy=frozen_navigation_policy,
            )
            result.update(
                {
                    "terminal_result": final_context["classification"][
                        "teaching_result"
                    ],
                    "conversation_package_locator": package_receipt[
                        "locator"
                    ],
                    "conversation_package_sha256": package_receipt[
                        "canonical_sha256"
                    ],
                    "conversation_package_sealed": False,
                    "pending_formal_write_count": 0,
                }
            )
            return _complete_answer_operation(
                operation, result, private_root=private_root
            )
        try:
            result = _terminal_package_capture_and_navigation(
                root,
                operation_id=operation_id,
                canonical_context=canonical_context,
                final_context=final_context,
                first_feedback_text=first_turn["feedback_text"],
                final_feedback_text=followup["feedback_text"],
                final_package_receipt=package_receipt,
                interaction_trace=cumulative_trace,
                navigation_policy=frozen_navigation_policy,
                private_root=private_root,
            )
        except (RuntimeError, OSError) as exc:
            result = _answer_result(
                operation_id=operation_id,
                status="recovery_required",
                feedback_text=followup["feedback_text"],
                first_result=lifecycle["first_result"],
                capture_status="capture_pending_recovery",
                capture_id=None,
                turn_receipt_locator=None,
                following=None,
                recovery_locator=None,
                learner_evidence_write_count=0,
                navigation_policy=frozen_navigation_policy,
            )
            result.update(
                {
                    "reason_code": str(exc)[:320],
                    "retry_same_operation": True,
                    "conversation_package_locator": package_receipt["locator"],
                    "conversation_package_sha256": package_receipt[
                        "canonical_sha256"
                    ],
                    "conversation_package_sealed": True,
                }
            )
            return result
        private_evidence.write_answer_display_lifecycle(
            {
                **updated_lifecycle,
                "status": "resolved",
                "resolution": {
                    "operation_id": operation_id,
                    "result": result,
                },
            },
            private_root=private_root,
        )
        return _complete_answer_operation(
            operation, result, private_root=private_root
        )


def continue_current(
    *,
    prior_turn_receipt_locator: str,
    context_id: str,
    session_id: str,
    item_id: str,
    private_root: str | Path | None = None,
) -> dict[str, Any]:
    """Verify and retain the current item without looking up a successor."""

    prior = private_evidence.read_metadata_object(
        prior_turn_receipt_locator, kind="turns", private_root=private_root
    )
    expected = {
        "context_id": context_id,
        "session_id": session_id,
        "item_id": item_id,
    }
    for field, value in expected.items():
        if prior.get(field) != value:
            raise ManagedCurrentTurnError(
                f"continue_current_{field}_binding_drifted"
            )
    return {
        "schema": CONTINUE_CURRENT_SCHEMA,
        "status": "current_item_retained",
        **expected,
        "capture_status": prior.get("capture_status"),
        "feedback_authorized": prior.get("feedback_authorized") is True,
        "next_item_lookup_count": 0,
        "next_item_published": False,
        "formal_write_count": 0,
    }


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo", type=Path, default=Path.cwd())
    parser.add_argument("--private-root", type=Path)
    sub = parser.add_subparsers(dest="command", required=True)
    recover = sub.add_parser("recover-current-capture")
    recover.add_argument("--recovery-locator", required=True)
    recover_receipts = sub.add_parser("recover-current-receipts")
    recover_receipts.add_argument("--recovery-locator", required=True)
    resume_held = sub.add_parser("resume-held-session")
    resume_held.add_argument("--held-turn-receipt-locator", required=True)
    answer_next = sub.add_parser("answer-current-and-next")
    answer_next.add_argument("--display-receipt-locator", required=True)
    answer_next.add_argument(
        "--choice", choices=sorted(MANAGED_CHOICES), required=True
    )
    answer_next.add_argument(
        "--confidence", choices=("high", "medium", "low"), required=True
    )
    answer_next.add_argument(
        "--prompt-level", choices=sorted(context_model.PROMPT_LEVELS), default="none"
    )
    answer_next.add_argument(
        "--navigation-policy",
        choices=sorted(NAVIGATION_POLICIES),
        default=DEFAULT_NAVIGATION_POLICY,
        help="Publish an authorized successor or finish the current item without publishing one",
    )
    answer_next.add_argument(
        "--trace-json",
        default="[]",
        help="Bounded private current-question trace; never enters the public capture",
    )
    answer_next.add_argument(
        "--semantic-assessment-json",
        default="{}",
        help=(
            "Root-Sol frozen reasoning verdict; deterministic choice grading "
            "cannot override it"
        ),
    )
    answer_next.add_argument(
        "--attachments-json",
        default='{"question_mode":"dialogue_only","attachments":[]}',
        help="Bounded private attachment descriptors; paths never enter stored evidence",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        if args.command == "recover-current-capture":
            result = recover_current_capture(
                args.repo,
                args.recovery_locator,
                private_root=args.private_root,
            )
        elif args.command == "recover-current-receipts":
            result = recover_current_receipts(
                args.repo,
                args.recovery_locator,
                private_root=args.private_root,
            )
        elif args.command == "resume-held-session":
            result = resume_held_session(
                args.repo,
                held_turn_receipt_locator=args.held_turn_receipt_locator,
                private_root=args.private_root,
            )
        elif args.command == "answer-current-and-next":
            result = answer_current_and_next(
                args.repo,
                display_receipt_locator=args.display_receipt_locator,
                choice=args.choice,
                confidence=args.confidence,
                prompt_level=args.prompt_level,
                navigation_policy=args.navigation_policy,
                interaction_trace=json.loads(args.trace_json),
                semantic_assessment=json.loads(
                    args.semantic_assessment_json
                ),
                attachments_json=args.attachments_json,
                private_root=args.private_root,
            )
        else:
            raise ManagedCurrentTurnError("unsupported_current_turn_command")
    except (KeyError, json.JSONDecodeError, ValueError, RuntimeError) as exc:
        result = {
            "schema": TURN_SCHEMA,
            "subject": "cs408",
            "status": "blocked",
            "reason_code": str(exc),
            "feedback_authorized": False,
            "capture_id": None,
            "capture_content_sha256": None,
            "formal_write_count": 0,
        }
    sys.stdout.buffer.write(_json_bytes(result))
    return 0 if result.get("status") not in {"blocked", "recovery_required"} else 2


if __name__ == "__main__":
    raise SystemExit(main())


__all__ = [
    "ManagedCurrentTurnError",
    "answer_current_and_next",
    "resume_held_session",
    "recover_current_capture",
    "recover_current_receipts",
]
