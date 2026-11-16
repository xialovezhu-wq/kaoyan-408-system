#!/usr/bin/env python3
"""Run an answer-safe 408 morning-review session in a derived state layer.

The queue is immutable input.  This tool writes only beneath
``wiki/study_vaults/408-full/state/morning-review/`` and never stores prompts,
feedback text, full stems, answers, options, or formal mastery scores.
"""

from __future__ import annotations

import argparse
import contextlib
import copy
import datetime as dt
import fcntl
import hashlib
import json
import os
import re
import stat
import sys
import tempfile
import uuid
from collections import Counter
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator

import review_feedback_loop as review_loop
import generate_morning_action_queue as queue_generation

from intake_lib_408 import scan_leak
from intake_fact_capture_408 import (
    CaptureError,
    audit as audit_fact_capture,
    capture_root,
    capture_review_event,
)
from review_feedback_loop import (
    CHOICE_RESULTS,
    FIRST_BREAK_PROVENANCES,
    REASONING_RESULTS,
    ReviewLoopError,
    audit as audit_review_loop,
    assert_can_start_session,
    get_review_event,
    load_state as load_review_loop_state,
    normalize_first_break_provenance,
    normalize_morning_first_classification,
    reconcile_runtime_views,
    runtime_lock,
    sync_session,
    validate_first_break_text,
)


ROOT = Path(__file__).resolve().parents[1]
STATE_REL = Path("wiki/study_vaults/408-full/state/morning-review")
V3_QUEUE_SCHEMAS = {
    "morning_review_queue_v3",
    "morning_review_action_queue_v3",
}
QUEUE_SCHEMAS = {"morning_review_queue_v2", *V3_QUEUE_SCHEMAS}
FIRST_RESULTS = {
    "independent_correct",
    "fragile_correct",
    "partial",
    "wrong",
    "blank",
}
REPAIR_RESULTS = FIRST_RESULTS | {"relearn_required"}
CONFIDENCE_LEVELS = {"low", "medium", "high"}
PROMPT_LEVELS = {"none", "minimal", "progressive", "full"}
FEEDBACK_TYPES = {
    "brief_verification",
    "minimal_hint",
    "progressive_hint",
    "misconception_contrast",
    "corrective_feedback",
    "relearn_required",
}
QUEUE_ITEM_KINDS = {
    "formal_due",
    "baseline_calibration",
    "derived_d0",
    "original_question",
}
FOLLOWUP_ITEM_KINDS = {
    "related_original",
    "grounded_variant",
}
ITEM_KINDS = QUEUE_ITEM_KINDS | FOLLOWUP_ITEM_KINDS
FOLLOWUP_PRACTICE_MODES = {"original", "variant"}
FOLLOWUP_ACTIVATIONS = {"shadow", "verified_existing"}
ACTIVE_FOLLOWUP_ACTIVATIONS = FOLLOWUP_ACTIVATIONS - {"shadow"}
PREPARED_LEARNER_CHOICES = {"A", "B", "C", "D", "BLANK"}
PAUSE_REASONS = {
    "user_pause",
    "time_boundary",
    "fatigue",
    "external_interruption",
}
CANCEL_REASONS = {
    "user_no_time",
    "date_rollover",
}
COMPLETE_RESOLUTIONS = {
    "independent_correct",
    "repaired_independent",
    "relearn_required",
    "dismissed",
    "scheduling_superseded",
    "administratively_retired",
}
NAVIGATION_EXCLUDED_RESOLUTIONS = {
    "dismissed",
    "scheduling_superseded",
    "administratively_retired",
}
SESSION_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
ITEM_ID_RE = re.compile(r"^(?:MQ|DQ|OQ|AOQ|XF)-[A-Za-z0-9._-]+$")
FOLLOWUP_ITEM_ID_RE = re.compile(r"^XF-[0-9a-f]{20}$")
SAFE_REFERENCE_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:#/+\-]{0,255}$")
FORMAL_NODE_RE = re.compile(r"^(?:DS|CO|OS|CN)_(?:\d{4}|UNK)_\d{3}$")
REVIEW_UNIT_RE = re.compile(r"^RU_[A-Za-z0-9._-]+$")
SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
ITEM_HEADING_RE = re.compile(
    r"(?m)^#{3,6}\s+(?P<item>(?:MQ|DQ|OQ|AOQ)-[A-Za-z0-9._-]+)"
    r"(?:\s+(?P<title>[^\n]*))?$"
)
PROTECTED_META_RE = re.compile(
    r"完整题干|完整解析|标准答案|正确答案|错选答案|我的答案|我的错选|选项字母"
)
FIXED_FEEDBACK_BOUNDARY = (
    "请在安全详情或教师核验层检查关键机制与首个断点；只回传结果与断点，"
    "不要在晨间会话状态中保存完整题干、答案、选项或既往错选。"
)
SESSION_HOT_DIRNAME = ".morning-session-hot-v1"
MAX_QUEUE_FEEDBACK_BYTES = 262_144
MAX_REVEALED_FEEDBACK_BYTES = 12_288
FORMAL_HOT_POINTER_REL = Path(
    "wiki/study_vaults/408-full/state/review-hot-projection/manifest.json"
)
LEGACY_PERSONALIZATION_STATE_RELATIVE = Path(
    "wiki/study_vaults/408-full/state/personalization"
)


class SessionError(RuntimeError):
    """A fail-closed morning-review session error."""


def _linked_practice_module() -> Any:
    """Load the retired compatibility reader only on an explicit legacy path."""

    import linked_practice_source_408 as linked_practice

    return linked_practice


def _normalized_session_first_result(
    result: str,
    *,
    choice_result: str,
    reasoning_result: str,
    prompt_level: str,
) -> tuple[str, str, bool]:
    """Return session result, canonical result, and fragile evidence flag."""

    try:
        canonical_result, fragile = normalize_morning_first_classification(
            result,
            choice_result=choice_result,
            reasoning_result=reasoning_result,
            prompt_level=prompt_level,
        )
    except ReviewLoopError as exc:
        raise SessionError(str(exc)) from exc
    if canonical_result == "uncertain":
        session_result = "blank"
    elif canonical_result == "independent_correct" and fragile:
        session_result = "fragile_correct"
    else:
        session_result = canonical_result
    if session_result not in FIRST_RESULTS:
        raise SessionError(
            f"normalized morning result is unsupported: {session_result}"
        )
    return session_result, canonical_result, fragile


class SessionState(dict[str, Any]):
    """State-compatible mapping carrying non-persisted hot commit evidence."""

    def __init__(
        self,
        value: dict[str, Any],
        *,
        hot_commit: dict[str, Any] | None = None,
        hot_load: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(value)
        self.hot_commit = hot_commit
        self.hot_load = hot_load


def utc_now() -> str:
    return dt.datetime.now(dt.timezone.utc).replace(microsecond=0).isoformat()


def _session_hot_manifest_path(session_dir: Path) -> Path:
    return session_dir / SESSION_HOT_DIRNAME / "manifest.json"


def _session_hot_managed(session_dir: Path) -> bool:
    # lexists deliberately treats a broken symlink as present-but-invalid, so
    # managed state cannot silently fall back to a full legacy replay.
    return os.path.lexists(_session_hot_manifest_path(session_dir))


def _session_hot_module() -> Any:
    try:
        import morning_session_hot_state_408 as session_hot
    except ImportError as exc:
        raise SessionError("managed morning session hot-state module is unavailable") from exc
    return session_hot


def _morning_capture_hot_module() -> Any:
    try:
        import morning_capture_hot_adapter_408 as capture_hot_adapter
    except ImportError as exc:
        raise SessionError(
            "managed morning capture hot adapter is unavailable"
        ) from exc
    return capture_hot_adapter


def _review_hot_module() -> Any:
    try:
        import review_hot_state_408 as review_hot
    except ImportError as exc:
        raise SessionError("managed review hot-state module is unavailable") from exc
    return review_hot


def _prepare_review_hot_for_study(repo: Path) -> dict[str, Any]:
    """Prepare review hot state before a question can receive an answer.

    Clean legacy/test repositories without a canonical ledger or any hot-state
    artifacts remain on their existing path.  Once either surface exists, a
    missing or stale active pointer is a managed-state failure and must be
    repaired from the canonical ledger before the session is published.
    """

    review_hot = _review_hot_module()
    ledger = repo / review_hot.LEDGER_RELATIVE
    state_root = repo / review_hot.DEFAULT_STATE_RELATIVE
    managed_surface_exists = (
        os.path.lexists(ledger)
        or os.path.lexists(state_root / "manifest.json")
        or os.path.lexists(state_root / "manifest.json.disabled")
        or state_root.is_dir()
    )
    if not managed_surface_exists:
        return {
            "schema": review_hot.READINESS_SCHEMA,
            "status": "not_configured",
            "phase": review_hot.BEFORE_LEARNER_ANSWER_PHASE,
            "preparation_action": "legacy_uninitialized",
            "rebuild_attempted": False,
            "rebuild_performed": False,
            "learner_evidence_write_count": 0,
            "formal_write_count": 0,
        }
    prepared = review_hot.prepare_study(repo)
    if prepared.get("status") != "ready":
        raise SessionError(
            "review hot-state pre-study readiness failed closed: "
            + str(prepared.get("reason_code") or prepared.get("status"))
        )
    return prepared


def _prepare_capture_hot_for_study(repo: Path) -> dict[str, Any]:
    """Prepare and verify capture hot state before publishing a session."""

    capture_hot_adapter = _morning_capture_hot_module()
    truth_root = capture_root(repo)
    hot_root = capture_hot_adapter.capture_hot_root(repo)
    managed_surface_exists = (
        os.path.lexists(truth_root / "events.jsonl")
        or os.path.lexists(truth_root / "state.json")
        or os.path.lexists(hot_root / "joint-manifest.json")
        or hot_root.is_dir()
    )
    if not managed_surface_exists:
        return {
            "schema": capture_hot_adapter.CAPTURE_READINESS_SCHEMA,
            "status": "not_configured",
            "phase": capture_hot_adapter.BEFORE_LEARNER_ANSWER_PHASE,
            "preparation_action": "legacy_uninitialized",
            "rebuild_attempted": False,
            "rebuild_performed": False,
            "learner_evidence_write_count": 0,
            "formal_write_count": 0,
        }
    prepared = capture_hot_adapter.prepare_capture_hot_state(repo)
    if prepared.get("status") != "ready":
        raise SessionError(
            "capture hot-state pre-study readiness failed closed: "
            + str(prepared.get("reason_code") or prepared.get("status"))
        )
    return prepared


def _session_hot_event_key(
    session_id: str,
    event_type: str,
    payload: dict[str, Any],
    *,
    purpose: str | None = None,
) -> str:
    digest = hashlib.sha256(
        _canonical_json(
            {
                "schema": "morning_session_managed_event_key_v1",
                "session_id": session_id,
                "event_type": event_type,
                "payload": payload,
                "purpose": purpose,
            }
        ).encode("utf-8")
    ).hexdigest()
    return f"morning-session:{event_type}:{digest}"


def _state_hot_commit(state: dict[str, Any]) -> dict[str, Any] | None:
    value = getattr(state, "hot_commit", None)
    return value if isinstance(value, dict) else None


def _verify_session_hot_key(
    session_dir: Path, idempotency_key: str
) -> dict[str, Any]:
    session_hot = _session_hot_module()
    try:
        proof = session_hot.verify_idempotency(
            session_dir, idempotency_key
        )
    except session_hot.MorningSessionHotStateError as exc:
        raise SessionError(f"managed session receipt verification failed: {exc}") from exc
    if proof.get("status") == "pass":
        return proof
    if proof.get("status") == "not_found":
        # A session may have been cold-adopted after this transition existed.
        # The signed bootstrap/current manifest still gives a bounded proof of
        # the complete state, but is explicitly not an event-specific receipt.
        loaded = session_hot.load_state_bounded(session_dir)
        if loaded.get("status") != "ready":
            raise SessionError(
                "managed session state could not prove an adopted transition"
            )
        manifest = loaded["manifest"]
        return {
            "status": "state_bound_without_event_receipt",
            "session_id": session_dir.name,
            "idempotency_key_sha256": hashlib.sha256(
                idempotency_key.encode("utf-8")
            ).hexdigest(),
            "manifest_sha256": loaded["manifest_sha256"],
            "state_logical_sha256": manifest["state"]["logical_sha256"],
            "state_last_sequence": loaded["last_sequence"],
            "binding_receipt_sha256": manifest["last_commit_receipt_sha256"],
            "ledger_read_bytes": 0,
            "answer_safe": True,
            "formal_write_count": 0,
        }
    raise SessionError(
        "managed session receipt verification failed closed: "
        + str(proof.get("reason_code") or proof.get("status") or "unknown")
    )


def _verify_session_hot_event_receipt(
    session_dir: Path,
    receipt_sha256: str,
    *,
    event_type: str,
    item_id: str,
) -> dict[str, Any]:
    """Verify one exact managed-session receipt and its target event."""

    if not SHA256_RE.fullmatch(str(receipt_sha256 or "")):
        raise SessionError("managed session event receipt sha256 is invalid")
    session_hot = _session_hot_module()
    try:
        proof = session_hot.verify_commit(session_dir, receipt_sha256)
        found = (
            session_hot.lookup_session_event(session_dir, str(proof.get("event_id") or ""))
            if proof.get("status") == "pass"
            else None
        )
    except session_hot.MorningSessionHotStateError as exc:
        raise SessionError(f"managed session event receipt failed: {exc}") from exc
    event = found.get("event") if isinstance(found, dict) else None
    payload = event.get("payload") if isinstance(event, dict) else None
    if (
        proof.get("status") != "pass"
        or proof.get("receipt_sha256") != receipt_sha256
        or not isinstance(event, dict)
        or event.get("event_type") != event_type
        or not isinstance(payload, dict)
        or payload.get("item_id") != item_id
    ):
        raise SessionError("managed session event receipt binds the wrong transition")
    return proof


def _managed_first_commit_for_recovery(
    session_dir: Path, item_id: str
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Resolve the exact original first-answer commit on an explicit recovery path."""

    matches = [
        event
        for event in _read_events(session_dir / "events.jsonl", session_dir.name)
        if event.get("event_type") == "first_recorded"
        and (event.get("payload") or {}).get("item_id") == item_id
    ]
    if len(matches) != 1:
        raise SessionError(
            "managed recovery requires exactly one original first_recorded event"
        )
    event = matches[0]
    idempotency_key = str(event.get("idempotency_key") or "")
    if not idempotency_key:
        raise SessionError(
            "managed recovery first answer lacks an event-specific idempotency key"
        )
    proof = _verify_session_hot_key(session_dir, idempotency_key)
    if (
        proof.get("status") != "pass"
        or proof.get("event_id") != event.get("event_id")
        or not proof.get("receipt_sha256")
    ):
        raise SessionError(
            "managed recovery first answer lacks an exact commit receipt"
        )
    return event, proof


def _recovered_first_commit_proof(
    session_dir: Path, item: dict[str, Any]
) -> dict[str, Any] | None:
    receipt_sha256 = str(item.get("recovery_first_commit_receipt_sha256") or "")
    if not receipt_sha256:
        return None
    proof = _verify_session_hot_event_receipt(
        session_dir,
        receipt_sha256,
        event_type="first_recorded",
        item_id=str(item.get("item_id") or ""),
    )
    source_event_id = str(item.get("recovery_first_event_id") or "")
    if not source_event_id or proof.get("event_id") != source_event_id:
        raise SessionError(
            "recovered first-answer receipt does not bind its source event"
        )
    return proof


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _canonical_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _validate_followup_text(
    value: object, label: str, *, required: bool = True, limit: int = 512
) -> str:
    compact = re.sub(r"\s+", " ", str(value or "")).strip()
    if required and not compact:
        raise SessionError(f"{label} is required")
    if len(compact) > limit:
        raise SessionError(f"{label} must be <= {limit} characters")
    fails, warns = scan_leak(compact)
    if PROTECTED_META_RE.search(compact) or fails or warns:
        detail = ", ".join(sorted(set(fails + warns))) or "protected content"
        raise SessionError(f"{label} contains protected content: {detail}")
    return compact


def _validate_reference_id(value: object, label: str) -> str:
    compact = _validate_followup_text(value, label, limit=256)
    if not SAFE_REFERENCE_ID_RE.fullmatch(compact):
        raise SessionError(f"{label} contains unsupported characters")
    return compact


def _followup_item_id(payload_without_item_id: dict[str, Any]) -> str:
    digest = hashlib.sha256(
        _canonical_json(payload_without_item_id).encode("utf-8")
    ).hexdigest()
    return f"XF-{digest[:20]}"


def _followup_source_path(
    repo: Path,
    source_ref: str,
    *,
    personalization_state_dir: str | Path | None = None,
) -> tuple[Path, str]:
    locator = _validate_followup_text(source_ref, "source_ref", limit=1024)
    linked_practice = _linked_practice_module()
    try:
        return linked_practice.resolve_source(
            repo,
            locator,
            personalization_state_dir=personalization_state_dir,
        )
    except linked_practice.LinkedPracticeSourceError as exc:
        raise SessionError(str(exc)) from exc


def _managed_personalization_state(
    repo: Path, personalization_state_dir: str | Path | None
) -> Path:
    default_state = (repo / LEGACY_PERSONALIZATION_STATE_RELATIVE).resolve()
    if personalization_state_dir is None:
        return default_state
    candidate = Path(personalization_state_dir).expanduser()
    if not candidate.is_absolute():
        candidate = repo / candidate
    candidate = candidate.resolve()
    if candidate != default_state:
        raise SessionError(
            "verified followup requires the managed default personalization projection"
        )
    return default_state


def _followup_selection_receipt_path(
    repo: Path,
    selection_receipt_ref: object,
    *,
    personalization_state_dir: str | Path | None = None,
) -> tuple[Path, str]:
    linked_practice = _linked_practice_module()
    try:
        path, normalized, _ = linked_practice.resolve_selection_receipt(
            repo,
            selection_receipt_ref,
            personalization_state_dir=personalization_state_dir,
        )
    except linked_practice.LinkedPracticeSourceError as exc:
        raise SessionError(str(exc)) from exc
    return path, normalized


def _validate_personalization_question_manifest(
    path: Path, *, expected_formal_node_id: str
) -> None:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise SessionError("personalization question manifest is unreadable") from exc
    assets = payload.get("question_assets") if isinstance(payload, dict) else None
    if (
        payload.get("schema") != "question_source_manifest_v1"
        or payload.get("answer_isolated") is not True
        or payload.get("formal_node_id") != expected_formal_node_id
        or not isinstance(assets, list)
        or not assets
        or payload.get("question_asset_count") != len(assets)
    ):
        raise SessionError("personalization question manifest is not answer-isolated")
    for asset in assets:
        if not isinstance(asset, dict) or set(asset) != {
            "path",
            "sha256",
            "size_bytes",
        }:
            raise SessionError("personalization question asset entry is invalid")
        asset_path = Path(str(asset.get("path") or "")).expanduser()
        digest = str(asset.get("sha256") or "")
        size = asset.get("size_bytes")
        if (
            not asset_path.is_absolute()
            or not asset_path.is_file()
            or not asset_path.name.startswith("question-")
            or "solution" in asset_path.name.lower()
            or not SHA256_RE.fullmatch(digest)
            or not isinstance(size, int)
            or size < 0
            or asset_path.stat().st_size != size
            or sha256_file(asset_path) != digest
        ):
            raise SessionError("personalization question asset verification failed")


def _normalize_followup_payload(
    raw: dict[str, Any], *, repo: Path | None = None
) -> dict[str, Any]:
    required = {
        "item_id",
        "scheduling_key",
        "activation",
        "trigger_event_id",
        "parent_item_id",
        "context_slice_id",
        "candidate_id",
        "source_id",
        "source_ref",
        "source_sha256",
        "mechanism_key",
        "knowledge_point",
        "practice_mode",
        "item_kind",
        "formal_node_id",
        "anchor_formal_node_id",
        "variant_of_formal_node_id",
        "review_unit_id",
        "selection_receipt_ref",
        "selection_receipt_sha256",
    }
    if not isinstance(raw, dict) or set(raw) != required:
        raise SessionError("followup_scheduled payload fields are incomplete or unknown")
    practice_mode = str(raw.get("practice_mode") or "")
    if practice_mode not in FOLLOWUP_PRACTICE_MODES:
        raise SessionError("practice_mode must be original or variant")
    activation = str(raw.get("activation") or "")
    if activation not in ACTIVE_FOLLOWUP_ACTIVATIONS:
        raise SessionError("persisted followup activation is not enabled")
    item_kind = str(raw.get("item_kind") or "")
    expected_kind = "related_original" if practice_mode == "original" else "grounded_variant"
    if item_kind != expected_kind:
        raise SessionError("item_kind does not match practice_mode")
    formal_node_id = str(raw.get("formal_node_id") or "") or None
    anchor_formal_node_id = str(raw.get("anchor_formal_node_id") or "") or None
    variant_of = str(raw.get("variant_of_formal_node_id") or "") or None
    review_unit_id = str(raw.get("review_unit_id") or "") or None
    if formal_node_id and not FORMAL_NODE_RE.fullmatch(formal_node_id):
        raise SessionError("formal_node_id is invalid")
    if anchor_formal_node_id and not FORMAL_NODE_RE.fullmatch(
        anchor_formal_node_id
    ):
        raise SessionError("anchor_formal_node_id is invalid")
    if variant_of and not FORMAL_NODE_RE.fullmatch(variant_of):
        raise SessionError("variant_of_formal_node_id is invalid")
    if review_unit_id and not REVIEW_UNIT_RE.fullmatch(review_unit_id):
        raise SessionError("review_unit_id is invalid")
    if practice_mode == "original":
        if not formal_node_id or variant_of is not None:
            raise SessionError("original followup must bind exactly one formal_node_id")
    else:
        if formal_node_id is not None or not variant_of:
            raise SessionError(
                "variant followup must keep formal_node_id empty and bind variant origin separately"
            )
        if anchor_formal_node_id != variant_of:
            raise SessionError("variant followup anchor must match its formal origin")
    source_id = _validate_reference_id(raw.get("source_id"), "source_id")
    if practice_mode == "variant" and FORMAL_NODE_RE.fullmatch(source_id):
        raise SessionError("variant source_id must not impersonate the original formal node")
    source_sha = str(raw.get("source_sha256") or "")
    if not SHA256_RE.fullmatch(source_sha):
        raise SessionError("source_sha256 must be lowercase SHA-256")
    selection_receipt_sha = str(raw.get("selection_receipt_sha256") or "")
    if not SHA256_RE.fullmatch(selection_receipt_sha):
        raise SessionError("selection_receipt_sha256 must be lowercase SHA-256")
    source_ref = _validate_followup_text(raw.get("source_ref"), "source_ref", limit=1024)
    selection_receipt_ref = _validate_followup_text(
        raw.get("selection_receipt_ref"), "selection_receipt_ref", limit=1024
    )
    if repo is not None:
        source_path, source_ref = _followup_source_path(repo, source_ref)
        if sha256_file(source_path) != source_sha:
            raise SessionError("source_ref does not match source_sha256")
    normalized_without_item = {
        "scheduling_key": _validate_reference_id(raw.get("scheduling_key"), "scheduling_key"),
        "activation": activation,
        "trigger_event_id": _validate_reference_id(
            raw.get("trigger_event_id"), "trigger_event_id"
        ),
        "parent_item_id": _validate_reference_id(raw.get("parent_item_id"), "parent_item_id"),
        "context_slice_id": _validate_reference_id(
            raw.get("context_slice_id"), "context_slice_id"
        ),
        "candidate_id": _validate_reference_id(raw.get("candidate_id"), "candidate_id"),
        "source_id": source_id,
        "source_ref": source_ref,
        "source_sha256": source_sha,
        "mechanism_key": _validate_followup_text(
            raw.get("mechanism_key"), "mechanism_key", limit=240
        ),
        "knowledge_point": _validate_followup_text(
            raw.get("knowledge_point"), "knowledge_point", limit=240
        ),
        "practice_mode": practice_mode,
        "item_kind": item_kind,
        "formal_node_id": formal_node_id,
        "anchor_formal_node_id": anchor_formal_node_id,
        "variant_of_formal_node_id": variant_of,
        "review_unit_id": review_unit_id,
        "selection_receipt_ref": selection_receipt_ref,
        "selection_receipt_sha256": selection_receipt_sha,
    }
    expected_item_id = _followup_item_id(normalized_without_item)
    if raw.get("item_id") != expected_item_id or not FOLLOWUP_ITEM_ID_RE.fullmatch(
        expected_item_id
    ):
        raise SessionError("followup item_id is not the content-addressed payload ID")
    return {"item_id": expected_item_id, **normalized_without_item}


def _fsync_dir(path: Path) -> None:
    flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
    descriptor = os.open(str(path), flags)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def atomic_write_json(path: Path, value: dict[str, Any]) -> None:
    """Write a JSON projection atomically in the derived state directory."""
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    descriptor, temp_name = tempfile.mkstemp(
        prefix=f".{path.name}.", suffix=".tmp", dir=path.parent
    )
    temp_path = Path(temp_name)
    try:
        os.fchmod(descriptor, 0o600)
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            json.dump(value, handle, ensure_ascii=False, indent=2, sort_keys=True)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp_path, path)
        _fsync_dir(path.parent)
    except BaseException:
        with contextlib.suppress(FileNotFoundError):
            temp_path.unlink()
        raise


def append_event(path: Path, event: dict[str, Any]) -> None:
    """Append one compact JSON event while the per-session lock is held."""
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    payload = (json.dumps(event, ensure_ascii=False, separators=(",", ":")) + "\n").encode(
        "utf-8"
    )
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
    try:
        offset = 0
        while offset < len(payload):
            offset += os.write(descriptor, payload[offset:])
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _resolve_repo(raw: str | Path) -> Path:
    repo = Path(raw).expanduser().resolve()
    if not repo.is_dir():
        raise SessionError(f"repo does not exist: {repo}")
    return repo


def _inside_repo(repo: Path, raw: str | Path, label: str) -> Path:
    candidate = Path(raw).expanduser()
    path = candidate.resolve() if candidate.is_absolute() else (repo / candidate).resolve()
    try:
        relative = path.relative_to(repo)
    except ValueError as exc:
        raise SessionError(f"{label} must stay inside repo: {path}") from exc
    if relative.parts and relative.parts[0] == "复盘输出":
        raise SessionError(f"{label} must not use 复盘输出/: {path}")
    return path


def _validate_session_id(value: str) -> str:
    if not SESSION_ID_RE.fullmatch(value):
        raise SessionError("session id must match [A-Za-z0-9][A-Za-z0-9._-]{0,127}")
    return value


def _state_root(repo: Path) -> Path:
    root = (repo / STATE_REL).resolve()
    try:
        root.relative_to(repo)
    except ValueError as exc:
        raise SessionError("derived state root escaped repo") from exc
    return root


def _session_dir(repo: Path, session_id: str) -> Path:
    return _state_root(repo) / _validate_session_id(session_id)


@contextmanager
def session_lock(session_dir: Path, *, create: bool = False) -> Iterator[None]:
    if create:
        session_dir.mkdir(parents=True, exist_ok=False, mode=0o700)
    elif not session_dir.is_dir():
        raise SessionError(f"session does not exist: {session_dir.name}")
    lock_path = session_dir / ".lock"
    descriptor = os.open(lock_path, os.O_RDWR | os.O_CREAT, 0o600)
    try:
        fcntl.flock(descriptor, fcntl.LOCK_EX)
        yield
    finally:
        fcntl.flock(descriptor, fcntl.LOCK_UN)
        os.close(descriptor)


def _split_md_row(line: str) -> list[str]:
    return [cell.strip() for cell in line.strip().strip("|").split("|")]


def _clean_field_value(raw: str) -> str:
    value = raw.strip()
    if len(value) >= 2 and value.startswith("`") and value.endswith("`"):
        value = value[1:-1].strip()
    return value


def _section_field(section: str, aliases: list[str]) -> str | None:
    alternatives = "|".join(re.escape(alias) for alias in aliases)
    match = re.search(
        rf"(?mi)^-\s*(?:{alternatives})\s*[:：]\s*(?P<value>.+?)\s*$", section
    )
    return _clean_field_value(match.group("value")) if match else None


def _infer_v2_kind(item_id: str) -> str:
    if item_id.startswith("DQ-"):
        return "derived_d0"
    if item_id.startswith(("OQ-", "AOQ-")):
        return "original_question"
    return "formal_due"


def _queue_item_sections(text: str, schema: str) -> list[dict[str, str]]:
    matches = list(ITEM_HEADING_RE.finditer(text))
    items: list[dict[str, str]] = []
    seen: set[str] = set()
    for index, match in enumerate(matches):
        item_id = match.group("item")
        if item_id in seen:
            raise SessionError(f"duplicate queue item heading: {item_id}")
        seen.add(item_id)
        end = matches[index + 1].start() if index + 1 < len(matches) else len(text)
        section = text[match.start() : end]
        source_id = _section_field(
            section,
            [
                "source_id",
                "Source ID",
                "来源复习单元",
                "来源派生项",
                "主机制单元",
            ],
        )
        item_kind = _section_field(section, ["item_kind", "Item Kind"])
        prompt = _section_field(section, ["prompt", "Prompt", "闭卷问题", "闭卷任务"])
        action_id = _section_field(section, ["action_id", "Action ID"])
        trigger_event_id = _section_field(section, ["trigger_event_id", "Trigger Event ID"])
        stage = _section_field(section, ["review_stage", "Review Stage", "stage"])
        formal_node_id = _section_field(section, ["formal_node_id", "Formal Node ID"])
        review_unit_id = _section_field(section, ["review_unit_id", "Review Unit ID"])
        title = (match.group("title") or "").strip()
        if schema in V3_QUEUE_SCHEMAS:
            if not source_id:
                raise SessionError(f"v3 queue item missing source_id: {item_id}")
            if item_kind not in QUEUE_ITEM_KINDS:
                raise SessionError(f"v3 queue item has invalid item_kind: {item_id}={item_kind}")
            if not prompt:
                raise SessionError(f"v3 queue item missing prompt: {item_id}")
        else:
            item_kind = (
                item_kind
                if item_kind in QUEUE_ITEM_KINDS
                else _infer_v2_kind(item_id)
            )
            if item_kind == "original_question" and title:
                # In v2, 主机制单元 is an RU_* dependency, not the stable OQ source.
                # Prefer the formal node ID carried by the OQ/AOQ heading.
                source_id = title.split()[0]
            if not source_id:
                raise SessionError(f"v2 queue item missing stable source: {item_id}")
            if not prompt:
                raise SessionError(f"v2 queue item missing closed-book prompt/task: {item_id}")
        items.append(
            {
                "item_id": item_id,
                "source_id": source_id,
                "item_kind": item_kind,
                "action_id": action_id,
                "trigger_event_id": trigger_event_id,
                "stage": stage,
                "formal_node_id": formal_node_id,
                "review_unit_id": review_unit_id,
            }
        )
    if not items:
        raise SessionError("queue contains no actionable item headings")
    return items


def parse_queue(path: Path) -> dict[str, Any]:
    text = path.read_text(encoding="utf-8")
    schema_match = re.search(r"(?m)^schema:\s*(\S+)\s*$", text)
    schema = schema_match.group(1) if schema_match else ""
    if schema not in QUEUE_SCHEMAS:
        raise SessionError(f"unsupported queue schema: {schema or 'missing'}")
    date_match = re.search(r"(?m)^review_date:\s*(\d{4}-\d{2}-\d{2})\s*$", text)
    generation = queue_generation.queue_generation_metadata(path)
    return {
        "schema": schema,
        "review_date": date_match.group(1) if date_match else "undated",
        "items": _queue_item_sections(text, schema),
        "generation_id": generation.get("generation_id"),
        "generation_input_sha256": generation.get("generation_input_sha256"),
        "eligible_due_action_set_sha256": generation.get(
            "eligible_due_action_set_sha256"
        ),
        "formal_due_source_sha256": generation.get("formal_due_source_sha256"),
    }


def read_review_unit_feedback(path: Path) -> dict[str, str]:
    if not path.is_file():
        raise SessionError(f"missing review-unit table: {path}")
    lines = path.read_text(encoding="utf-8").splitlines()
    header_index = next(
        (index for index, line in enumerate(lines) if line.startswith("|") and "复习单元ID" in line),
        None,
    )
    if header_index is None:
        raise SessionError("review-unit table has no header")
    headers = _split_md_row(lines[header_index])
    if "复习单元ID" not in headers or "派生自查口径" not in headers:
        raise SessionError("review-unit table lacks 复习单元ID/派生自查口径")
    result: dict[str, str] = {}
    for line in lines[header_index + 2 :]:
        if not line.startswith("|"):
            continue
        cells = _split_md_row(line)
        if len(cells) != len(headers):
            raise SessionError("review-unit table contains malformed row")
        row = dict(zip(headers, cells))
        unit_id = row["复习单元ID"]
        if not unit_id:
            continue
        if unit_id in result:
            raise SessionError(f"duplicate review unit: {unit_id}")
        result[unit_id] = row["派生自查口径"].strip()
    return result


def _validate_v3_feedback_sources(repo: Path, items: list[dict[str, str]]) -> str | None:
    formal_items = [
        item
        for item in items
        if item["item_kind"] in {"formal_due", "baseline_calibration"}
    ]
    if not formal_items:
        return None
    table = repo / "复习单元总表.md"
    feedback = read_review_unit_feedback(table)
    for item in formal_items:
        source_id = item["source_id"]
        if not source_id.startswith("RU_"):
            raise SessionError(f"formal v3 item source_id must be RU_*: {item['item_id']}")
        if not feedback.get(source_id):
            raise SessionError(f"missing safe derived feedback for {source_id}")
    return sha256_file(table)


def _read_events(path: Path, session_id: str) -> list[dict[str, Any]]:
    if not path.is_file():
        raise SessionError(f"missing event ledger for session: {session_id}")
    events: list[dict[str, Any]] = []
    for line_no, raw in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
        try:
            event = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise SessionError(f"event ledger is corrupt at line {line_no}") from exc
        if not isinstance(event, dict):
            raise SessionError(f"event ledger line {line_no} is not an object")
        expected = len(events) + 1
        if event.get("schema") != "morning_review_session_event_v1":
            raise SessionError(f"event ledger line {line_no} has invalid schema")
        if event.get("session_id") != session_id or event.get("sequence") != expected:
            raise SessionError(f"event ledger sequence/session mismatch at line {line_no}")
        events.append(event)
    if not events:
        raise SessionError(f"event ledger is empty for session: {session_id}")
    return events


def _initial_item(
    item: dict[str, Any], *, followup_event_id: str | None = None
) -> dict[str, Any]:
    return {
        "item_id": item["item_id"],
        "source_id": item["source_id"],
        "item_kind": item["item_kind"],
        "action_id": item.get("action_id"),
        "trigger_event_id": item.get("trigger_event_id"),
        "stage": item.get("stage"),
        "formal_node_id": item.get("formal_node_id"),
        "review_unit_id": item.get("review_unit_id"),
        "practice_mode": item.get("practice_mode"),
        "anchor_formal_node_id": item.get("anchor_formal_node_id"),
        "variant_of_formal_node_id": item.get("variant_of_formal_node_id"),
        "mechanism_key": item.get("mechanism_key"),
        "knowledge_point": item.get("knowledge_point"),
        "scheduling_key": item.get("scheduling_key"),
        "context_slice_id": item.get("context_slice_id"),
        "candidate_id": item.get("candidate_id"),
        "parent_item_id": item.get("parent_item_id"),
        "source_ref": item.get("source_ref"),
        "source_sha256": item.get("source_sha256"),
        "queue_sha256": item.get("queue_sha256"),
        "pack_commitment_sha256": item.get("pack_commitment_sha256"),
        "selection_receipt_ref": item.get("selection_receipt_ref"),
        "selection_receipt_sha256": item.get("selection_receipt_sha256"),
        "activation": item.get("activation"),
        "surface_sha256": item.get("surface_sha256"),
        "grading_sha256": item.get("grading_sha256"),
        "guided_surface_sha256": item.get("guided_surface_sha256"),
        "guided_grading_sha256": item.get("guided_grading_sha256"),
        "repair_surface_sha256": item.get("repair_surface_sha256"),
        "repair_grading_sha256": item.get("repair_grading_sha256"),
        "followup_event_id": followup_event_id,
        "first": None,
        "feedback_revealed": False,
        "feedback_source": None,
        "feedback_type": None,
        "repair_attempts": [],
        "repair_eligible_at": None,
        "resolution": "pending",
    }


def _apply_first_payload(
    state: dict[str, Any],
    item: dict[str, Any],
    payload: dict[str, Any],
    *,
    recorded_at: str,
) -> None:
    state["progress_clock"] += 1
    first = {
        "result": payload["result"],
        "choice_result": payload.get("choice_result", "not_applicable"),
        "reasoning_result": payload.get("reasoning_result", "not_observed"),
        "confidence": payload["confidence"],
        "response_seconds": payload["response_seconds"],
        "prompt_level": payload["prompt_level"],
        "first_break": payload["first_break"],
        "first_break_provenance": normalize_first_break_provenance(
            payload.get("first_break_provenance"), payload.get("first_break")
        ),
        "recorded_at": recorded_at,
    }
    managed_reply_binding = payload.get("managed_user_reply_binding_sha256")
    if managed_reply_binding is not None:
        if not isinstance(managed_reply_binding, str) or not SHA256_RE.fullmatch(
            managed_reply_binding
        ):
            raise SessionError("managed user reply binding is invalid")
        first["managed_user_reply_binding_sha256"] = managed_reply_binding
    if payload.get("canonical_event_id"):
        first["canonical_event_id"] = payload["canonical_event_id"]
    item["first"] = first
    if payload["result"] == "independent_correct":
        item["resolution"] = "independent_correct"
    else:
        item["resolution"] = "repair_pending"
        item["repair_eligible_at"] = state["progress_clock"] + state["repair_gap"]


ADMINISTRATIVE_RETIREMENT_FIELDS = {
    "status",
    "canonical_retirement_event_id",
    "canonical_retirement_event_sha256",
    "canonical_retirement_event_time",
    "formal_binding_event_id",
    "formal_binding_event_sha256",
    "action_id",
    "closed_equivalent_action_sha256",
    "equivalent_to_action_id",
    "resolution_reason",
    "processing_contract",
    "learner_evidence_write_count",
    "formal_write_count",
}


def _validate_administrative_retirement_disposition(
    item: dict[str, Any], disposition: object
) -> dict[str, Any]:
    if (
        not isinstance(disposition, dict)
        or set(disposition) != ADMINISTRATIVE_RETIREMENT_FIELDS
        or disposition.get("status") != "administratively_retired"
        or disposition.get("resolution_reason") != "user_removed"
        or disposition.get("processing_contract")
        != review_loop.CLOSED_EQUIVALENT_ITEM_RETIREMENT_CONTRACT
        or disposition.get("learner_evidence_write_count") != 0
        or disposition.get("formal_write_count") != 0
        or disposition.get("action_id") != item.get("action_id")
    ):
        raise SessionError("administrative retirement disposition is invalid")
    for field in (
        "canonical_retirement_event_id",
        "formal_binding_event_id",
        "action_id",
        "equivalent_to_action_id",
    ):
        if not SAFE_REFERENCE_ID_RE.fullmatch(
            str(disposition.get(field) or "")
        ):
            raise SessionError("administrative retirement ID is invalid")
    for field in (
        "canonical_retirement_event_sha256",
        "formal_binding_event_sha256",
        "closed_equivalent_action_sha256",
    ):
        if not SHA256_RE.fullmatch(str(disposition.get(field) or "")):
            raise SessionError("administrative retirement hash is invalid")
    try:
        review_loop._parse_datetime(
            str(disposition.get("canonical_retirement_event_time") or "")
        )
    except ReviewLoopError as exc:
        raise SessionError("administrative retirement event time is invalid") from exc
    return disposition


def _apply_administrative_retirement(
    item: dict[str, Any], disposition: object
) -> None:
    verified = _validate_administrative_retirement_disposition(
        item, disposition
    )
    if item.get("resolution") == "administratively_retired":
        if (
            item.get("first") is not None
            or item.get("feedback_revealed") is not False
            or item.get("feedback_source") is not None
            or item.get("feedback_type") is not None
            or item.get("administrative_disposition") != verified
        ):
            raise SessionError("administrative retirement replay drifted")
        return
    if item.get("first") is not None or item.get("resolution") != "pending":
        raise SessionError(
            "administrative retirement requires an unanswered pending item"
        )
    item["first"] = None
    item["feedback_revealed"] = False
    item["feedback_source"] = None
    item["feedback_type"] = None
    item["repair_attempts"] = []
    item["repair_eligible_at"] = None
    item["resolution"] = "administratively_retired"
    item["administrative_disposition"] = copy.deepcopy(verified)


def _apply_event(state: dict[str, Any] | None, event: dict[str, Any]) -> dict[str, Any]:
    event_type = event["event_type"]
    payload = event["payload"]
    if event_type == "session_started":
        if state is not None:
            raise SessionError("duplicate session_started event")
        item_rows = payload["items"]
        state = {
            "schema": "morning_review_session_state_v1",
            "session_id": event["session_id"],
            "status": "in_progress",
            "queue_schema": payload["queue_schema"],
            "queue_path": payload["queue_path"],
            "queue_sha256": payload["queue_sha256"],
            "generation_id": payload.get("generation_id"),
            "generation_input_sha256": payload.get("generation_input_sha256"),
            "eligible_due_action_set_sha256": payload.get(
                "eligible_due_action_set_sha256"
            ),
            "formal_due_source_sha256": payload.get("formal_due_source_sha256"),
            "review_table_sha256": payload.get("review_table_sha256"),
            "interaction_mode": payload.get("interaction_mode"),
            "delivery_pack_ref": payload.get("delivery_pack_ref"),
            "delivery_pack_sha256": payload.get("delivery_pack_sha256"),
            "delivery_pack_id": payload.get("delivery_pack_id"),
            "delivery_pack_commitment_sha256": payload.get(
                "delivery_pack_commitment_sha256"
            ),
            "binding_status": "session_bound",
            "review_date": payload["review_date"],
            "repair_gap": payload["repair_gap"],
            "max_failed_repairs": payload["max_failed_repairs"],
            "progress_clock": 0,
            "created_at": event["timestamp"],
            "updated_at": event["timestamp"],
            "last_sequence": event["sequence"],
            "pause_reason": None,
            "cancel_reason": None,
            "closed_summary": None,
            "cancel_summary": None,
            "item_order": [item["item_id"] for item in item_rows],
            "items": {item["item_id"]: _initial_item(item) for item in item_rows},
            "followup_by_key": {},
        }
        return state
    if state is None:
        raise SessionError("first event must be session_started")
    if event_type == "followup_scheduled":
        if state["status"] != "in_progress":
            raise SessionError("followup can only be scheduled in an active session")
        row = _normalize_followup_payload(payload)
        item_id = row["item_id"]
        by_key = state.setdefault("followup_by_key", {})
        existing_id = by_key.get(row["scheduling_key"])
        if existing_id:
            raise SessionError("duplicate followup scheduling_key event")
        if item_id in state["items"]:
            raise SessionError(f"duplicate followup item: {item_id}")
        for existing in state["items"].values():
            if (
                existing.get("trigger_event_id") == row["trigger_event_id"]
                and existing.get("followup_event_id")
                and existing.get("resolution") not in COMPLETE_RESOLUTIONS
            ):
                raise SessionError("trigger already has an active followup")
        state["item_order"].append(item_id)
        state["items"][item_id] = _initial_item(
            row, followup_event_id=str(event["event_id"])
        )
        by_key[row["scheduling_key"]] = item_id
        state["updated_at"] = event["timestamp"]
        state["last_sequence"] = event["sequence"]
        return state
    item: dict[str, Any] | None = None
    item_id = payload.get("item_id")
    if item_id is not None:
        item = state["items"].get(item_id)
        if item is None:
            raise SessionError(f"event references unknown item: {item_id}")
    if event_type in {"first_recorded", "first_reconciled_from_loop"}:
        assert item is not None
        _apply_first_payload(
            state,
            item,
            payload,
            recorded_at=str(payload.get("recorded_at") or event["timestamp"]),
        )
    elif event_type == "unbound_first_repaired":
        assert item is not None
        first = item.get("first")
        if first is None:
            raise SessionError("cannot repair an item without a recorded first answer")
        if first.get("canonical_event_id"):
            raise SessionError("cannot repair a first answer already bound to the review ledger")
        first["first_break"] = payload["first_break"]
        first["first_break_provenance"] = normalize_first_break_provenance(
            payload.get("first_break_provenance"), payload.get("first_break")
        )
        normalized_result = payload.get("normalized_result")
        if normalized_result is not None:
            previous_result = payload.get("previous_result")
            if previous_result and first.get("result") != previous_result:
                raise SessionError(
                    "unbound first classification repair does not match prior state"
                )
            if normalized_result not in FIRST_RESULTS:
                raise SessionError("unbound first normalized result is invalid")
            first["result"] = normalized_result
            if normalized_result == "independent_correct":
                item["resolution"] = "independent_correct"
                item["repair_eligible_at"] = None
            else:
                item["resolution"] = "repair_pending"
                if item.get("repair_eligible_at") is None:
                    item["repair_eligible_at"] = (
                        state["progress_clock"] + state["repair_gap"]
                    )
        source_first_event_id = payload.get("source_first_event_id")
        source_first_receipt = payload.get("source_first_commit_receipt_sha256")
        if source_first_event_id is not None or source_first_receipt is not None:
            if (
                not isinstance(source_first_event_id, str)
                or not source_first_event_id
                or len(source_first_event_id) > 128
                or not SHA256_RE.fullmatch(str(source_first_receipt or ""))
            ):
                raise SessionError("unbound first recovery evidence is invalid")
            item["recovery_first_event_id"] = source_first_event_id
            item["recovery_first_commit_receipt_sha256"] = source_first_receipt
    elif event_type == "first_bound_to_loop":
        assert item is not None
        first = item.get("first")
        if first is None:
            raise SessionError("cannot bind an item without a recorded first answer")
        existing_event_id = first.get("canonical_event_id")
        if existing_event_id and existing_event_id != payload["canonical_event_id"]:
            raise SessionError("first answer is already bound to another canonical event")
        first["canonical_event_id"] = payload["canonical_event_id"]
    elif event_type == "feedback_revealed":
        assert item is not None
        item["feedback_revealed"] = True
        item["feedback_source"] = payload["feedback_source"]
        item["feedback_type"] = payload["feedback_type"]
    elif event_type == "teaching_resolved":
        assert item is not None
        if item.get("first") is None or not item.get("feedback_revealed"):
            raise SessionError(
                "teaching resolution requires first answer and revealed feedback"
            )
        if item.get("resolution") != "repair_pending":
            raise SessionError(
                f"teaching resolution requires repair_pending, got {item.get('resolution')}"
            )
        for field in (
            "resolution_attestation_sha256",
            "trace_supplement_sha256",
            "interaction_trace_sha256",
            "capture_receipt_sha256",
        ):
            if not SHA256_RE.fullmatch(str(payload.get(field) or "")):
                raise SessionError(f"teaching resolution {field} is invalid")
        if not isinstance(payload.get("capture_id"), str) or not payload["capture_id"]:
            raise SessionError("teaching resolution capture_id is invalid")
        item["teaching_resolution"] = {
            "status": "teaching_resolved",
            "mastery_effect": "none",
            "retention_effect": "none",
            "independent_repair": False,
            "capture_id": payload["capture_id"],
            "capture_receipt_sha256": payload["capture_receipt_sha256"],
            "resolution_attestation_sha256": payload[
                "resolution_attestation_sha256"
            ],
            "trace_supplement_sha256": payload["trace_supplement_sha256"],
            "interaction_trace_sha256": payload["interaction_trace_sha256"],
            "recorded_at": event["timestamp"],
        }
        item["resolution"] = "relearn_required"
        item["repair_eligible_at"] = None
    elif event_type == "clock_advanced":
        state["progress_clock"] += payload["count"]
    elif event_type == "repair_recorded":
        assert item is not None
        attempt_number = payload.get("attempt", len(item["repair_attempts"]) + 1)
        if (
            not isinstance(attempt_number, int)
            or attempt_number != len(item["repair_attempts"]) + 1
        ):
            raise SessionError("repair attempt sequence is invalid")
        expected_review_key = (
            f"morning:{state['session_id']}:{payload['item_id']}:"
            f"repair:{attempt_number}"
        )
        review_key = payload.get("review_idempotency_key", expected_review_key)
        if review_key != expected_review_key:
            raise SessionError("repair review idempotency key is invalid")
        attempt = {
            "attempt": attempt_number,
            "result": payload["result"],
            "confidence": payload.get("confidence"),
            "response_seconds": payload.get("response_seconds"),
            "prompt_level": payload.get("prompt_level"),
            "recorded_at": event["timestamp"],
            "explicit": payload["result"] == "relearn_required",
            "review_idempotency_key": review_key,
            "session_idempotency_key": event.get("idempotency_key"),
            "canonical_event_id": None,
        }
        item["repair_attempts"].append(attempt)
        if payload["result"] == "independent_correct":
            item["resolution"] = "repaired_independent"
            item["repair_eligible_at"] = None
        elif payload["result"] == "relearn_required":
            item["resolution"] = "relearn_required"
            item["repair_eligible_at"] = None
        else:
            failed = sum(
                attempt_row["result"] not in {"independent_correct", "relearn_required"}
                for attempt_row in item["repair_attempts"]
            )
            if failed >= state["max_failed_repairs"]:
                item["resolution"] = "relearn_required"
                item["repair_eligible_at"] = None
            else:
                item["resolution"] = "repair_pending"
                item["repair_eligible_at"] = state["progress_clock"] + state["repair_gap"]
    elif event_type == "repair_bound_to_loop":
        assert item is not None
        attempt_number = payload.get("attempt")
        canonical_event_id = payload.get("canonical_event_id")
        if (
            not isinstance(attempt_number, int)
            or not 1 <= attempt_number <= len(item["repair_attempts"])
            or not isinstance(canonical_event_id, str)
            or not canonical_event_id
        ):
            raise SessionError("repair canonical binding is invalid")
        attempt = item["repair_attempts"][attempt_number - 1]
        existing_event_id = attempt.get("canonical_event_id")
        if existing_event_id and existing_event_id != canonical_event_id:
            raise SessionError("repair is already bound to another canonical event")
        attempt["canonical_event_id"] = canonical_event_id
    elif event_type == "session_reconciled_from_loop":
        for row in payload["items"]:
            reconciled = state["items"].get(row["item_id"])
            if reconciled is None:
                raise SessionError(
                    f"canonical reconciliation references unknown item: {row['item_id']}"
                )
            if row.get("scheduling_superseded"):
                if (
                    reconciled.get("resolution") != "scheduling_superseded"
                    or reconciled.get("first") is not None
                    or reconciled.get("scheduling_disposition")
                    != row.get("scheduling_disposition")
                ):
                    raise SessionError(
                        "canonical reconciliation scheduling supersession drifted"
                    )
                continue
            if row.get("administratively_retired"):
                disposition = row.get("administrative_disposition")
                _apply_administrative_retirement(reconciled, disposition)
                continue
            if row.get("dismissed"):
                reconciled["first"] = None
                reconciled["feedback_revealed"] = True
                reconciled["feedback_source"] = "explicit_user_disposition"
                reconciled["feedback_type"] = None
                reconciled["repair_attempts"] = []
                reconciled["repair_eligible_at"] = None
                reconciled["resolution"] = "dismissed"
                continue
            reconciled["first"] = row["first"]
            reconciled["feedback_revealed"] = True
            reconciled["feedback_source"] = "review_loop_canonical_ledger"
            reconciled["feedback_type"] = row["feedback_type"]
            reconciled["repair_attempts"] = row["repair_attempts"]
            reconciled["repair_eligible_at"] = None
            reconciled["resolution"] = row["resolution"]
        state["progress_clock"] = max(state["progress_clock"], len(state["item_order"]))
    elif event_type == "item_administratively_retired":
        assert item is not None
        if set(payload) != {"item_id", "administrative_disposition"}:
            raise SessionError(
                "item administrative retirement fields are incomplete or unknown"
            )
        if state.get("status") not in {"in_progress", "paused"}:
            raise SessionError(
                "item administrative retirement requires an active or paused session"
            )
        _apply_administrative_retirement(
            item, payload.get("administrative_disposition")
        )
    elif event_type == "item_scheduling_superseded":
        assert item is not None
        required = {
            "item_id",
            "action_id",
            "action_sha256",
            "authoritative_outcome_event_id",
            "reconciliation_event_id",
            "processing_contract",
            "formal_write_count",
        }
        if set(payload) != required:
            raise SessionError(
                "item scheduling supersession fields are incomplete or unknown"
            )
        if state.get("status") not in {"in_progress", "paused"}:
            raise SessionError(
                "item scheduling supersession requires an active or paused session"
            )
        if item.get("first") is not None or item.get("resolution") != "pending":
            raise SessionError(
                "item scheduling supersession requires an unanswered pending item"
            )
        if item.get("action_id") != payload.get("action_id"):
            raise SessionError("item scheduling supersession action binding drifted")
        for field in (
            "action_id",
            "authoritative_outcome_event_id",
            "reconciliation_event_id",
            "processing_contract",
        ):
            if not SAFE_REFERENCE_ID_RE.fullmatch(str(payload.get(field) or "")):
                raise SessionError(
                    f"item scheduling supersession {field} is invalid"
                )
        if not SHA256_RE.fullmatch(str(payload.get("action_sha256") or "")):
            raise SessionError("item scheduling supersession action SHA is invalid")
        if payload.get("formal_write_count") != 0:
            raise SessionError("item scheduling supersession cannot write formal state")
        item["resolution"] = "scheduling_superseded"
        item["repair_eligible_at"] = None
        item["scheduling_disposition"] = {
            "status": "superseded",
            "action_id": payload["action_id"],
            "action_sha256": payload["action_sha256"],
            "authoritative_outcome_event_id": payload[
                "authoritative_outcome_event_id"
            ],
            "reconciliation_event_id": payload["reconciliation_event_id"],
            "processing_contract": payload["processing_contract"],
            "recorded_at": event["timestamp"],
            "formal_write_count": 0,
        }
    elif event_type == "session_paused":
        state["status"] = "paused"
        state["pause_reason"] = payload["reason"]
    elif event_type == "session_resumed":
        state["status"] = "in_progress"
        state["pause_reason"] = None
    elif event_type == "session_cancelled":
        if state["status"] not in {"in_progress", "paused"}:
            raise SessionError(
                f"session must be active to cancel, got {state['status']}"
            )
        touched = [
            item_id
            for item_id in state["item_order"]
            if state["items"][item_id]["first"] is not None
            or state["items"][item_id]["feedback_revealed"]
            or state["items"][item_id]["repair_attempts"]
            or state["items"][item_id]["resolution"] != "pending"
        ]
        if touched:
            raise SessionError(
                "cannot cancel a session after learning evidence exists: "
                + ", ".join(touched[:20])
            )
        state["status"] = "cancelled"
        state["pause_reason"] = None
        state["cancel_reason"] = payload["reason"]
        state["cancel_summary"] = payload["summary"]
    elif event_type == "session_closed":
        state["status"] = "closed"
        state["closed_summary"] = payload["summary"]
    else:
        raise SessionError(f"unknown event type: {event_type}")
    state["updated_at"] = event["timestamp"]
    state["last_sequence"] = event["sequence"]
    return state


def replay_events(events: list[dict[str, Any]]) -> dict[str, Any]:
    state: dict[str, Any] | None = None
    for event in events:
        state = _apply_event(state, event)
    if state is None:
        raise SessionError("cannot replay empty event ledger")
    return state


def _load_state(
    session_dir: Path, session_id: str, *, repair_projection: bool = True
) -> dict[str, Any]:
    if _session_hot_managed(session_dir):
        session_hot = _session_hot_module()
        try:
            loaded = session_hot.load_state_bounded(session_dir)
        except session_hot.MorningSessionHotStateError as exc:
            raise SessionError(f"managed session hot load failed closed: {exc}") from exc
        if loaded.get("status") != "ready":
            raise SessionError(
                "managed session hot load failed closed: "
                + str(loaded.get("reason_code") or loaded.get("status") or "unknown")
            )
        state = loaded.get("state")
        if not isinstance(state, dict):
            raise SessionError("managed session hot load returned no state")
        return SessionState(state, hot_load=loaded)
    events_path = session_dir / "events.jsonl"
    state_path = session_dir / "state.json"
    events = _read_events(events_path, session_id)
    state: dict[str, Any] | None = None
    try:
        loaded = json.loads(state_path.read_text(encoding="utf-8"))
        if isinstance(loaded, dict) and loaded.get("last_sequence") == len(events):
            state = loaded
    except (FileNotFoundError, json.JSONDecodeError):
        state = None
    if state is None:
        state = replay_events(events)
        if repair_projection:
            atomic_write_json(state_path, state)
    if state.get("schema") != "morning_review_session_state_v1":
        raise SessionError("state has invalid schema")
    if state.get("session_id") != session_id:
        raise SessionError("state/session mismatch")
    return SessionState(state)


def _commit_event(
    session_dir: Path,
    state: dict[str, Any] | None,
    session_id: str,
    event_type: str,
    payload: dict[str, Any],
    *,
    idempotency_key: str | None = None,
) -> dict[str, Any]:
    if _session_hot_managed(session_dir):
        if state is None:
            raise SessionError("managed session cannot commit without a bound state")
        key = idempotency_key or _session_hot_event_key(
            session_id,
            event_type,
            payload,
            purpose=f"sequence:{int(state['last_sequence']) + 1}",
        )
        session_hot = _session_hot_module()
        try:
            committed = session_hot.commit_event(
                session_dir,
                state,
                event_type,
                payload,
                idempotency_key=key,
                session_lock_held=True,
            )
            verified = session_hot.verify_commit(
                session_dir, committed["receipt_sha256"]
            )
        except (KeyError, session_hot.MorningSessionHotStateError) as exc:
            raise SessionError(f"managed session hot commit failed closed: {exc}") from exc
        if committed.get("status") not in {"committed", "already_committed"}:
            raise SessionError("managed session hot commit did not reach a terminal state")
        if verified.get("status") != "pass":
            raise SessionError(
                "managed session hot commit receipt failed verification: "
                + str(verified.get("reason_code") or verified.get("status"))
            )
        next_state = committed.get("state")
        if not isinstance(next_state, dict):
            raise SessionError("managed session hot commit returned no state")
        return SessionState(
            next_state,
            hot_commit={
                "status": committed["status"],
                "event_id": committed["event_id"],
                "sequence": committed["sequence"],
                "receipt_ref": committed.get("receipt_path"),
                "receipt_sha256": committed["receipt_sha256"],
                "verification": verified,
                "idempotency_key": key,
                "answer_safe": True,
                "formal_write_count": 0,
            },
        )
    sequence = 1 if state is None else int(state["last_sequence"]) + 1
    event = {
        "schema": "morning_review_session_event_v1",
        "event_id": str(uuid.uuid4()),
        "session_id": session_id,
        "sequence": sequence,
        "timestamp": utc_now(),
        "event_type": event_type,
        "payload": payload,
    }
    next_state = _apply_event(copy.deepcopy(state), event)
    append_event(session_dir / "events.jsonl", event)
    atomic_write_json(session_dir / "state.json", next_state)
    return SessionState(next_state)


def _ensure_active(state: dict[str, Any]) -> None:
    if state["status"] != "in_progress":
        raise SessionError(f"session must be in_progress, got {state['status']}")


def _commit_closed_equivalent_retirement_locked(
    session_dir: Path,
    state: dict[str, Any],
    *,
    session_id: str,
    item_id: str,
    administrative_disposition: dict[str, Any],
) -> dict[str, Any]:
    """Project one canonical retirement while the caller holds session_lock."""

    payload = {
        "item_id": item_id,
        "administrative_disposition": administrative_disposition,
    }
    key = _session_hot_event_key(
        session_id,
        "item_administratively_retired",
        payload,
        purpose=f"item:{item_id}:closed-equivalent-retirement",
    )
    item = _get_item(state, item_id)
    if item.get("resolution") == "administratively_retired":
        _validate_administrative_retirement_disposition(
            item, administrative_disposition
        )
        if item.get("administrative_disposition") != administrative_disposition:
            raise SessionError(
                "existing administrative retirement binding drifted"
            )
        verification = (
            _verify_session_hot_key(session_dir, key)
            if _session_hot_managed(session_dir)
            else None
        )
        status = "already_retired"
    else:
        if state.get("status") not in {"in_progress", "paused"}:
            raise SessionError(
                "administrative retirement requires an active or paused session"
            )
        if item.get("first") is not None or item.get("resolution") != "pending":
            raise SessionError(
                "administrative retirement requires an unanswered pending item"
            )
        committed = _commit_event(
            session_dir,
            state,
            session_id,
            "item_administratively_retired",
            payload,
            idempotency_key=(
                key if _session_hot_managed(session_dir) else None
            ),
        )
        state = committed
        verification = _state_hot_commit(committed)
        status = "retired"
    projected = _get_item(state, item_id).get("administrative_disposition")
    if projected != administrative_disposition:
        raise SessionError("administrative retirement was not projected")
    receipt_sha256 = None
    if isinstance(verification, dict):
        receipt_sha256 = verification.get("receipt_sha256")
        if receipt_sha256 is None:
            receipt_sha256 = verification.get("verification", {}).get(
                "receipt_sha256"
            )
    return {
        "status": status,
        "state": state,
        "session_id": session_id,
        "item_id": item_id,
        "session_commit_receipt_sha256": receipt_sha256,
        "learner_evidence_write_count": 0,
        "formal_write_count": 0,
    }


def mark_scheduling_superseded(
    repo: str | Path,
    *,
    session_id: str,
    item_id: str,
    action_id: str,
    action_sha256: str,
    authoritative_outcome_event_id: str,
    reconciliation_event_id: str,
    processing_contract: str,
) -> dict[str, Any]:
    """Append one answer-free disposition for an already consumed action."""

    root = _resolve_repo(repo)
    session_dir = _session_dir(root, session_id)
    payload = {
        "item_id": item_id,
        "action_id": action_id,
        "action_sha256": action_sha256,
        "authoritative_outcome_event_id": authoritative_outcome_event_id,
        "reconciliation_event_id": reconciliation_event_id,
        "processing_contract": processing_contract,
        "formal_write_count": 0,
    }
    key = _session_hot_event_key(
        session_id,
        "item_scheduling_superseded",
        payload,
        purpose=f"item:{item_id}:action:{action_id}",
    )
    with session_lock(session_dir):
        state = _load_state(session_dir, session_id)
        item = _get_item(state, item_id)
        existing = item.get("scheduling_disposition")
        if item.get("resolution") == "scheduling_superseded":
            expected = {
                "status": "superseded",
                "action_id": action_id,
                "action_sha256": action_sha256,
                "authoritative_outcome_event_id": authoritative_outcome_event_id,
                "reconciliation_event_id": reconciliation_event_id,
                "processing_contract": processing_contract,
                "formal_write_count": 0,
            }
            if not isinstance(existing, dict) or any(
                existing.get(field) != value for field, value in expected.items()
            ):
                raise SessionError(
                    "existing item scheduling supersession binding drifted"
                )
            verification = (
                _verify_session_hot_key(session_dir, key)
                if _session_hot_managed(session_dir)
                else None
            )
            status = "already_superseded"
        else:
            committed = _commit_event(
                session_dir,
                state,
                session_id,
                "item_scheduling_superseded",
                payload,
                idempotency_key=key if _session_hot_managed(session_dir) else None,
            )
            state = committed
            item = _get_item(state, item_id)
            existing = item.get("scheduling_disposition")
            verification = _state_hot_commit(committed)
            status = "superseded"
        if not isinstance(existing, dict):
            raise SessionError("item scheduling supersession was not projected")
        disposition_sha256 = hashlib.sha256(
            _canonical_json(existing).encode("utf-8")
        ).hexdigest()
        receipt_sha256 = None
        if isinstance(verification, dict):
            receipt_sha256 = verification.get("receipt_sha256")
            if receipt_sha256 is None:
                receipt_sha256 = verification.get("verification", {}).get(
                    "receipt_sha256"
                )
        return {
            "schema": "morning_item_scheduling_supersession_v1",
            "status": status,
            "session_id": session_id,
            "item_id": item_id,
            "action_id": action_id,
            "disposition_sha256": disposition_sha256,
            "session_commit_receipt_sha256": receipt_sha256,
            "first_answer_write_count": 0,
            "formal_write_count": 0,
        }


def _get_item(state: dict[str, Any], item_id: str) -> dict[str, Any]:
    item = state["items"].get(item_id)
    if item is None:
        raise SessionError(f"item is not selected in this session: {item_id}")
    return item


def _validate_first_break(value: str) -> str:
    compact = re.sub(r"\s+", " ", value).strip()
    if not compact:
        raise SessionError("first_break is required; use 'none' when no break was observed")
    if len(compact) > 200:
        raise SessionError("first_break must be a short answer-safe label (<= 200 characters)")
    if PROTECTED_META_RE.search(compact):
        raise SessionError("first_break must not contain protected stem/answer material")
    try:
        return validate_first_break_text(compact)
    except ReviewLoopError as exc:
        raise SessionError(str(exc)) from exc


def _validate_reaction_time(value: float | None) -> float | None:
    if value is None:
        return None
    if value <= 0 or value > 7200:
        raise SessionError(
            "reaction time must be > 0 and <= 7200 seconds; use null when unmeasured"
        )
    return float(value)


def session_summary(state: dict[str, Any]) -> dict[str, Any]:
    items = list(state["items"].values())
    resolutions = Counter(item["resolution"] for item in items)
    first_answered = sum(item["first"] is not None for item in items)
    dismissed = resolutions.get("dismissed", 0)
    scheduling_superseded = resolutions.get("scheduling_superseded", 0)
    administratively_retired = resolutions.get("administratively_retired", 0)
    independently_complete = sum(
        item["resolution"] in {"independent_correct", "repaired_independent"} for item in items
    )
    total = len(items)
    return {
        "session_id": state["session_id"],
        "status": state["status"],
        "queue_schema": state["queue_schema"],
        "generation_id": state.get("generation_id"),
        "generation_input_sha256": state.get("generation_input_sha256"),
        "eligible_due_action_set_sha256": state.get(
            "eligible_due_action_set_sha256"
        ),
        "formal_due_source_sha256": state.get("formal_due_source_sha256"),
        "interaction_mode": state.get("interaction_mode"),
        "delivery_pack_ref": state.get("delivery_pack_ref"),
        "delivery_pack_sha256": state.get("delivery_pack_sha256"),
        "delivery_pack_id": state.get("delivery_pack_id"),
        "delivery_pack_commitment_sha256": state.get(
            "delivery_pack_commitment_sha256"
        ),
        "queue_binding_status": "queue_bound",
        "queue_generation_mode": (
            "generation" if state.get("generation_id") else "legacy"
        ),
        "pack_binding_status": (
            "pack_bound" if state.get("delivery_pack_sha256") else "not_bound"
        ),
        "session_binding_status": state.get("binding_status", "session_bound"),
        "review_date": state["review_date"],
        "selected_count": total,
        "first_answered_count": first_answered,
        "unanswered_count": total
        - first_answered
        - dismissed
        - scheduling_superseded
        - administratively_retired,
        "independently_complete_count": independently_complete,
        "relearn_required_count": resolutions.get("relearn_required", 0),
        "repair_pending_count": resolutions.get("repair_pending", 0),
        "progress_clock": state["progress_clock"],
        "queue_coverage": (
            (
                first_answered
                + dismissed
                + scheduling_superseded
                + administratively_retired
            )
            / total
            if total
            else 1.0
        ),
        "criterion_coverage": independently_complete / total if total else 1.0,
        "dismissed_count": dismissed,
        "scheduling_superseded_count": scheduling_superseded,
        "administratively_retired_count": administratively_retired,
        "cancel_reason": state.get("cancel_reason"),
        "closure_ready": (
            first_answered
            + dismissed
            + scheduling_superseded
            + administratively_retired
            == total
        )
        and all(item["resolution"] in COMPLETE_RESOLUTIONS for item in items),
        "last_sequence": state["last_sequence"],
    }


def _item_public_view(item: dict[str, Any]) -> dict[str, Any]:
    return {
        "item_id": item["item_id"],
        "source_id": item["source_id"],
        "item_kind": item["item_kind"],
        "action_id": item.get("action_id"),
        "trigger_event_id": item.get("trigger_event_id"),
        "stage": item.get("stage"),
        "formal_node_id": item.get("formal_node_id"),
        "review_unit_id": item.get("review_unit_id"),
        "practice_mode": item.get("practice_mode"),
        "anchor_formal_node_id": item.get("anchor_formal_node_id"),
        "variant_of_formal_node_id": item.get("variant_of_formal_node_id"),
        "mechanism_key": item.get("mechanism_key"),
        "knowledge_point": item.get("knowledge_point"),
        "scheduling_key": item.get("scheduling_key"),
        "context_slice_id": item.get("context_slice_id"),
        "candidate_id": item.get("candidate_id"),
        "parent_item_id": item.get("parent_item_id"),
        "source_ref": item.get("source_ref"),
        "source_sha256": item.get("source_sha256"),
        "queue_sha256": item.get("queue_sha256"),
        "pack_commitment_sha256": item.get("pack_commitment_sha256"),
        "selection_receipt_ref": item.get("selection_receipt_ref"),
        "selection_receipt_sha256": item.get("selection_receipt_sha256"),
        "activation": item.get("activation"),
        "surface_sha256": item.get("surface_sha256"),
        "grading_sha256": item.get("grading_sha256"),
        "guided_surface_sha256": item.get("guided_surface_sha256"),
        "guided_grading_sha256": item.get("guided_grading_sha256"),
        "repair_surface_sha256": item.get("repair_surface_sha256"),
        "repair_grading_sha256": item.get("repair_grading_sha256"),
        "followup_event_id": item.get("followup_event_id"),
        "first": item["first"],
        "feedback_revealed": item["feedback_revealed"],
        "feedback_type": item["feedback_type"],
        "repair_attempts": item["repair_attempts"],
        "repair_eligible_at": item["repair_eligible_at"],
        "resolution": item["resolution"],
        "teaching_resolution": item.get("teaching_resolution"),
        "administrative_disposition": item.get("administrative_disposition"),
    }


def _json_output(value: dict[str, Any]) -> None:
    print(json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True))


def command_start(args: argparse.Namespace) -> dict[str, Any]:
    repo = _resolve_repo(args.repo)
    queue_path = _inside_repo(repo, args.queue, "queue")
    if not queue_path.is_file():
        raise SessionError(f"queue does not exist: {queue_path}")
    parsed = parse_queue(queue_path)
    item_by_id = {item["item_id"]: item for item in parsed["items"]}
    requested = args.item or []
    if requested:
        if len(requested) != len(set(requested)):
            raise SessionError("selected item IDs must be unique")
        missing = [item_id for item_id in requested if item_id not in item_by_id]
        if missing:
            raise SessionError(f"selected items are missing from queue: {missing}")
        selected = [item_by_id[item_id] for item_id in requested]
    else:
        selected = parsed["items"]
    if not selected:
        raise SessionError("session must select at least one item")
    queue_hash = sha256_file(queue_path)
    prepared_binding: dict[str, Any] | None = None
    if getattr(args, "prepared_manifest", None):
        try:
            import morning_review_prepared_pack_408 as prepared_pack

            prepared_binding = prepared_pack.validate_prepared_pack_for_start(
                repo,
                manifest_ref=args.prepared_manifest,
                queue_path=queue_path,
                queue_sha256=queue_hash,
                selected_items=selected,
            )
        except (ImportError, OSError, RuntimeError) as exc:
            raise SessionError(f"prepared pack failed closed before session start: {exc}") from exc
        selected = [
            {
                **item,
                **prepared_binding["item_bindings"][item["item_id"]],
            }
            for item in selected
        ]
    review_table_hash = None
    if parsed["schema"] in V3_QUEUE_SCHEMAS:
        review_table_hash = _validate_v3_feedback_sources(repo, selected)
    default_id = f"MR-{parsed['review_date']}-{queue_hash[:8]}"
    session_id = _validate_session_id(args.session_id or default_id)
    session_dir = _session_dir(repo, session_id)
    review_hot_readiness = _prepare_review_hot_for_study(repo)
    capture_hot_readiness = _prepare_capture_hot_for_study(repo)
    payload = {
        "queue_schema": parsed["schema"],
        "queue_path": str(queue_path.relative_to(repo)),
        "queue_sha256": queue_hash,
        "generation_id": parsed.get("generation_id"),
        "generation_input_sha256": parsed.get("generation_input_sha256"),
        "eligible_due_action_set_sha256": parsed.get(
            "eligible_due_action_set_sha256"
        ),
        "formal_due_source_sha256": parsed.get("formal_due_source_sha256"),
        "review_table_sha256": review_table_hash,
        "interaction_mode": (
            prepared_binding["interaction_mode"] if prepared_binding else None
        ),
        "delivery_pack_ref": (
            prepared_binding["delivery_pack_ref"] if prepared_binding else None
        ),
        "delivery_pack_sha256": (
            prepared_binding["delivery_pack_sha256"] if prepared_binding else None
        ),
        "delivery_pack_id": (
            prepared_binding["delivery_pack_id"] if prepared_binding else None
        ),
        "delivery_pack_commitment_sha256": (
            prepared_binding.get("pack_commitment_sha256")
            if prepared_binding
            else None
        ),
        "review_date": parsed["review_date"],
        "repair_gap": 3,
        "max_failed_repairs": 2,
        "items": selected,
    }
    with runtime_lock(repo):
        freshness = queue_generation.validate_generation_freshness(
            repo, queue_path
        )
        if not freshness.get("fresh"):
            reasons = ",".join(
                str(item) for item in freshness.get("reason_codes") or []
            )
            raise SessionError(
                "queue_generation_stale_pre_session:" + (reasons or "unknown")
            )
        assert_can_start_session(repo, session_id)
        with session_lock(session_dir, create=True):
            state = _commit_event(session_dir, None, session_id, "session_started", payload)
    reconcile_runtime_views(repo)
    managed_hot_path = (
        review_hot_readiness.get("status") == "ready"
        and capture_hot_readiness.get("status") == "ready"
        and os.path.lexists(repo / FORMAL_HOT_POINTER_REL)
    )
    hot_bootstrap: dict[str, Any] | None = None
    if managed_hot_path:
        session_hot = _session_hot_module()
        try:
            hot_bootstrap = session_hot.cold_bootstrap(session_dir)
        except session_hot.MorningSessionHotStateError as exc:
            raise SessionError(
                f"new session hot-state bootstrap failed closed: {exc}"
            ) from exc
        if hot_bootstrap.get("status") != "ready":
            raise SessionError("new session hot-state bootstrap did not become ready")
    return {
        "action": "start",
        **session_summary(state),
        "review_hot_readiness": review_hot_readiness,
        "capture_hot_readiness": capture_hot_readiness,
        "managed_hot_path": managed_hot_path,
        "session_hot_bootstrap": hot_bootstrap,
    }


def _with_state(args: argparse.Namespace) -> tuple[Path, str]:
    repo = _resolve_repo(args.repo)
    session_id = _validate_session_id(args.session)
    return repo, session_id


def command_status(args: argparse.Namespace) -> dict[str, Any]:
    repo, session_id = _with_state(args)
    session_dir = _session_dir(repo, session_id)
    with session_lock(session_dir):
        state = _load_state(session_dir, session_id, repair_projection=False)
    output = {"action": "status", **session_summary(state)}
    if args.item:
        output["item"] = _item_public_view(_get_item(state, args.item))
    return output


def schedule_followup(
    repo_root: str | Path,
    session_id: str,
    *,
    scheduling_key: str,
    trigger_event_id: str,
    context_slice_id: str,
    candidate_id: str,
    source_id: str,
    source_ref: str,
    source_sha256: str,
    mechanism_key: str,
    knowledge_point: str,
    practice_mode: str,
    formal_node_id: str | None = None,
    anchor_formal_node_id: str | None = None,
    variant_of_formal_node_id: str | None = None,
    review_unit_id: str | None = None,
    activation: str = "shadow",
    trigger_event: dict[str, Any] | None = None,
    personalization_state_dir: str | Path | None = None,
    selection_receipt_ref: str | None = None,
    selection_receipt_sha256: str | None = None,
) -> dict[str, Any]:
    """Retired write surface retained only to reject legacy callers.

    Existing followup events remain readable through session replay, but no new
    linked-practice or variant item may be scheduled.
    """

    raise SessionError(
        "legacy_schedule_followup_retired_use_existing_queue_next_item"
    )

    if activation not in FOLLOWUP_ACTIVATIONS:
        raise SessionError("followup activation is invalid")
    if activation == "shadow":
        return {
            "action": "schedule-followup",
            "status": "shadow_only",
            "activation": activation,
            "registered": False,
        }
    repo = _resolve_repo(repo_root)
    managed_state = _managed_personalization_state(
        repo, personalization_state_dir
    )
    session = _validate_session_id(session_id)
    trigger_id = _validate_reference_id(trigger_event_id, "trigger_event_id")
    trigger = (
        trigger_event
        if trigger_event is not None
        else get_review_event(repo, trigger_id)
    )
    if (
        not isinstance(trigger, dict)
        or trigger.get("event_id") != trigger_id
        or trigger.get("event_kind") != "outcome"
        or trigger.get("source") != "morning_review"
        or trigger.get("session_id") != session
    ):
        raise SessionError("followup trigger must be one canonical outcome in this session")
    if trigger.get("practice_mode"):
        raise SessionError("linked-practice outcomes cannot recursively schedule followups")
    mode = str(practice_mode or "")
    source_path, normalized_source_ref = _followup_source_path(
        repo,
        source_ref,
        personalization_state_dir=managed_state,
    )
    _, normalized_selection_receipt_ref = _followup_selection_receipt_path(
        repo,
        selection_receipt_ref,
        personalization_state_dir=managed_state,
    )
    item_kind = "related_original" if mode == "original" else "grounded_variant"
    without_item: dict[str, Any] = {
        "scheduling_key": _validate_reference_id(scheduling_key, "scheduling_key"),
        "activation": activation,
        "trigger_event_id": trigger_id,
        "parent_item_id": _validate_reference_id(
            trigger.get("item_id"), "parent_item_id"
        ),
        "context_slice_id": _validate_reference_id(
            context_slice_id, "context_slice_id"
        ),
        "candidate_id": _validate_reference_id(candidate_id, "candidate_id"),
        "source_id": _validate_reference_id(source_id, "source_id"),
        "source_ref": normalized_source_ref,
        "source_sha256": source_sha256,
        "selection_receipt_ref": normalized_selection_receipt_ref,
        "selection_receipt_sha256": selection_receipt_sha256,
        "mechanism_key": _validate_followup_text(
            mechanism_key, "mechanism_key", limit=240
        ),
        "knowledge_point": _validate_followup_text(
            knowledge_point, "knowledge_point", limit=240
        ),
        "practice_mode": mode,
        "item_kind": item_kind,
        "formal_node_id": formal_node_id,
        "anchor_formal_node_id": anchor_formal_node_id,
        "variant_of_formal_node_id": variant_of_formal_node_id,
        "review_unit_id": review_unit_id,
    }
    raw = {"item_id": _followup_item_id(without_item), **without_item}
    payload = _normalize_followup_payload(raw)
    session_dir = _session_dir(repo, session)
    managed_hot = _session_hot_managed(session_dir)
    followup_commit_key = _session_hot_event_key(
        session,
        "followup_scheduled",
        payload,
        purpose=f"scheduling-key:{payload['scheduling_key']}",
    )
    with session_lock(session_dir):
        try:
            verified_source = validate_source_for_practice(
                repo,
                normalized_source_ref,
                payload["source_sha256"],
                practice_mode=mode,
                formal_node_id=payload.get("formal_node_id"),
                variant_of_formal_node_id=payload.get(
                    "variant_of_formal_node_id"
                ),
                personalization_state_dir=managed_state,
            )
        except LinkedPracticeSourceError as exc:
            raise SessionError(str(exc)) from exc
        if verified_source["path"] != source_path or (
            verified_source["source_ref"] != payload["source_ref"]
        ):
            raise SessionError("linked source identity changed during scheduling")
        try:
            verified_selection = validate_selection_receipt(
                repo,
                payload["selection_receipt_ref"],
                payload["selection_receipt_sha256"],
                expected={
                    "slice_id": payload["context_slice_id"],
                    "trigger_event_id": payload["trigger_event_id"],
                    "candidate_id": payload["candidate_id"],
                    "practice_mode": payload["practice_mode"],
                    "formal_node_id": payload.get("formal_node_id"),
                    "anchor_formal_node_id": payload.get(
                        "anchor_formal_node_id"
                    ),
                    "source_id": payload["source_id"],
                    "source_ref": payload["source_ref"],
                    "source_sha256": payload["source_sha256"],
                    "mechanism_key": payload["mechanism_key"],
                    "knowledge_point": payload["knowledge_point"],
                    "review_unit_id": payload.get("review_unit_id"),
                },
                source_path=source_path,
                personalization_state_dir=managed_state,
            )
        except LinkedPracticeSourceError as exc:
            raise SessionError(str(exc)) from exc
        if (
            verified_selection["selection_receipt_ref"]
            != payload["selection_receipt_ref"]
        ):
            raise SessionError("selection receipt locator changed during scheduling")
        state = _load_state(session_dir, session)
        _ensure_active(state)
        parent = _get_item(state, str(trigger["item_id"]))
        parent_first = parent.get("first") or {}
        if parent_first.get("canonical_event_id") != trigger_id:
            raise SessionError("followup trigger is not bound to the parent session item")
        existing_item_id = (state.get("followup_by_key") or {}).get(
            payload["scheduling_key"]
        )
        if existing_item_id:
            existing_item = _get_item(state, str(existing_item_id))
            if any(
                existing_item.get(field) != value
                for field, value in payload.items()
            ):
                raise SessionError(
                    "followup scheduling_key was reused with a different payload"
                )
            session_commit = (
                _verify_session_hot_key(session_dir, followup_commit_key)
                if managed_hot
                else None
            )
            if managed_hot and (
                not isinstance(session_commit, dict)
                or session_commit.get("status") != "pass"
                or not SHA256_RE.fullmatch(
                    str(session_commit.get("receipt_sha256") or "")
                )
                or not isinstance(session_commit.get("verification"), dict)
                or session_commit["verification"].get("status") != "pass"
                or session_commit["verification"].get("receipt_sha256")
                != session_commit.get("receipt_sha256")
            ):
                raise SessionError(
                    "managed followup replay lacks an event-specific commit receipt"
                )
            return {
                "action": "schedule-followup",
                **session_summary(state),
                "followup_status": "active",
                "activation": activation,
                "registered": True,
                "replayed": True,
                "followup_event_id": existing_item.get("followup_event_id"),
                "item": _item_public_view(existing_item),
                "session_commit": session_commit,
            }
        for existing in state["items"].values():
            if (
                existing.get("followup_event_id")
                and existing.get("trigger_event_id") == trigger_id
                and existing.get("resolution") not in COMPLETE_RESOLUTIONS
            ):
                raise SessionError("trigger already has an active followup")
        state = _commit_event(
            session_dir,
            state,
            session,
            "followup_scheduled",
            payload,
            idempotency_key=(followup_commit_key if managed_hot else None),
        )
        session_commit = _state_hot_commit(state)
    if not managed_hot:
        reconcile_runtime_views(repo)
    return {
        "action": "schedule-followup",
        **session_summary(state),
        "followup_status": "active",
        "activation": activation,
        "registered": True,
        "replayed": False,
        "followup_event_id": state["items"][payload["item_id"]]["followup_event_id"],
        "item": _item_public_view(state["items"][payload["item_id"]]),
        "session_commit": session_commit,
    }


def command_schedule_followup(args: argparse.Namespace) -> dict[str, Any]:
    del args
    raise SessionError(
        "legacy_schedule_followup_retired_use_existing_queue_next_item"
    )


def _validate_dynamic_item_source(repo: Path, item: dict[str, Any]) -> None:
    del repo
    practice_mode = str(item.get("practice_mode") or "")
    if not practice_mode:
        return
    raise SessionError("legacy_dynamic_followup_is_read_only")


def _validate_prepared_choice_before_write(
    repo: Path,
    state: dict[str, Any],
    item: dict[str, Any],
    args: argparse.Namespace,
) -> None:
    """Validate the ephemeral learner choice against the frozen private key.

    The concrete A/B/C/D value is deliberately not returned or persisted.  Only
    its deterministic correctness projection may enter the canonical evidence
    payload.  This gate runs while the session lock is held and before the first
    session or review-ledger write.
    """

    if state.get("interaction_mode") != "frozen_prepared_pack":
        return
    displayed_surface = getattr(args, "display_surface_sha256", None)
    if (
        not isinstance(displayed_surface, str)
        or not SHA256_RE.fullmatch(displayed_surface)
        or displayed_surface != item.get("surface_sha256")
    ):
        raise SessionError(
            "prepared answer is not bound to the displayed public surface"
        )
    raw_choice = getattr(args, "learner_choice", None)
    if not isinstance(raw_choice, str) or raw_choice not in PREPARED_LEARNER_CHOICES:
        raise SessionError(
            "prepared answer requires one ephemeral learner choice before recording"
        )
    if raw_choice == "BLANK":
        if args.result != "blank" or args.choice_result != "uncertain":
            raise SessionError(
                "a prepared blank response must remain blank and uncertain"
            )
        return

    if args.result == "blank":
        raise SessionError("a prepared A/B/C/D response cannot be recorded as blank")
    if bool(getattr(args, "_current_question_capsule_verified", False)):
        derived = str(
            getattr(args, "_current_question_controlled_choice_result", "")
        )
    else:
        try:
            import morning_review_prepared_pack_408 as prepared_pack

            grading = prepared_pack.open_grading_context(
                repo,
                str(state["session_id"]),
                str(item["item_id"]),
                raw_choice,
            )
        except (ImportError, OSError, RuntimeError) as exc:
            raise SessionError(
                f"prepared choice verification failed closed: {exc}"
            ) from exc
        derived = str(grading.get("deterministic_choice_result") or "")
    if derived not in {"correct", "incorrect"} or args.choice_result != derived:
        raise SessionError(
            "declared choice result differs from the current grader capsule"
        )
    if derived == "incorrect":
        if args.result in {"independent_correct", "fragile_correct"}:
            raise SessionError(
                "an incorrect prepared choice cannot be recorded as a correct state"
            )
        if args.reasoning_result == "sound":
            raise SessionError(
                "an incorrect prepared choice conflicts with sound complete reasoning"
            )
        return

    if args.result in {"partial", "wrong"} and args.reasoning_result not in {
        "partial",
        "diverged",
    }:
        raise SessionError(
            "a correct prepared choice needs observed reasoning evidence before "
            "it can be downgraded"
        )


def _validate_prepared_repair_choice_before_write(
    repo: Path,
    state: dict[str, Any],
    item: dict[str, Any],
    args: argparse.Namespace,
) -> None:
    if state.get("interaction_mode") != "frozen_prepared_pack":
        return
    displayed_surface = getattr(args, "display_surface_sha256", None)
    if (
        not isinstance(displayed_surface, str)
        or not SHA256_RE.fullmatch(displayed_surface)
        or displayed_surface != item.get("repair_surface_sha256")
    ):
        raise SessionError(
            "prepared repair is not bound to the displayed repair surface"
        )
    raw_choice = getattr(args, "learner_choice", None)
    if not isinstance(raw_choice, str) or raw_choice not in PREPARED_LEARNER_CHOICES:
        raise SessionError(
            "prepared repair requires one ephemeral learner choice before recording"
        )
    try:
        import morning_review_prepared_pack_408 as prepared_pack

        grading = prepared_pack.open_grading_context(
            repo,
            str(state["session_id"]),
            str(item["item_id"]),
            None if raw_choice == "BLANK" else raw_choice,
            surface_role="repair",
        )
    except (ImportError, OSError, RuntimeError) as exc:
        raise SessionError(
            f"prepared repair verification failed closed: {exc}"
        ) from exc
    if raw_choice == "BLANK":
        if args.result not in {"blank", "relearn_required"}:
            raise SessionError(
                "a blank prepared repair cannot be recorded as a completed state"
            )
        return
    derived = str(grading.get("deterministic_choice_result") or "")
    if derived == "incorrect" and args.result in {
        "independent_correct",
        "fragile_correct",
    }:
        raise SessionError(
            "an incorrect prepared repair choice cannot be recorded as correct"
        )
    if derived == "correct" and args.result in {"blank", "relearn_required"}:
        raise SessionError(
            "a correct prepared repair choice conflicts with a blank repair state"
        )
    if derived == "correct" and args.result in {"partial", "wrong"}:
        raise SessionError(
            "a correct prepared repair choice cannot be downgraded without "
            "explicit repair reasoning evidence"
        )


def command_record_first(args: argparse.Namespace) -> dict[str, Any]:
    if not bool(getattr(args, "_current_question_managed_v1", False)):
        raise SessionError(
            "legacy_record_first_retired_use_managed_408_current_turn"
        )
    repo, session_id = _with_state(args)
    session_dir = _session_dir(repo, session_id)
    if args.result == "independent_correct" and args.prompt_level != "none":
        raise SessionError("independent_correct requires prompt_level=none")
    if args.result == "independent_correct" and (
        args.choice_result in {"incorrect", "partial", "uncertain"}
        or args.reasoning_result in {"partial", "diverged"}
    ):
        raise SessionError(
            "independent_correct conflicts with choice or reasoning result"
        )
    normalized_result, _, _ = _normalized_session_first_result(
        args.result,
        choice_result=args.choice_result,
        reasoning_result=args.reasoning_result,
        prompt_level=args.prompt_level,
    )
    payload = {
        "item_id": args.item,
        "result": normalized_result,
        "choice_result": args.choice_result,
        "reasoning_result": args.reasoning_result,
        "confidence": args.confidence,
        "response_seconds": _validate_reaction_time(args.response_seconds),
        "prompt_level": args.prompt_level,
        "first_break": _validate_first_break(args.first_break),
        "first_break_provenance": args.first_break_provenance,
    }
    managed_reply_binding = getattr(
        args, "managed_user_reply_binding_sha256", None
    )
    if managed_reply_binding is not None:
        if not isinstance(managed_reply_binding, str) or not SHA256_RE.fullmatch(
            managed_reply_binding
        ):
            raise SessionError("managed user reply binding is invalid")
        payload["managed_user_reply_binding_sha256"] = managed_reply_binding
    created_session_event = False
    resumed_unbound = False
    managed_hot = _session_hot_managed(session_dir)
    first_commit_key = _session_hot_event_key(
        session_id,
        "first_recorded",
        payload,
        purpose=f"item:{args.item}:first",
    )
    first_session_commit: dict[str, Any] | None = None
    with session_lock(session_dir):
        state = _load_state(session_dir, session_id)
        _ensure_active(state)
        item = _get_item(state, args.item)
        if item.get("first") is None and item.get("resolution") != "pending":
            raise SessionError(
                "first response requires an unanswered pending session item"
            )
        if not bool(getattr(args, "current_only", False)):
            _validate_dynamic_item_source(repo, item)
        _validate_prepared_choice_before_write(repo, state, item, args)
        existing = item.get("first")
        if existing is None:
            state = _commit_event(
                session_dir,
                state,
                session_id,
                "first_recorded",
                payload,
                idempotency_key=first_commit_key if managed_hot else None,
            )
            first_session_commit = _state_hot_commit(state)
            created_session_event = True
            existing_event_id = None
        else:
            if not _same_first_input(existing, payload):
                raise SessionError(
                    "first response already differs; use repair-unbound-first only "
                    "when canonical synchronization never completed"
                )
            existing_event_id = existing.get("canonical_event_id")
            resumed_unbound = existing_event_id is None
        staged_state = copy.deepcopy(dict(state))
        staged_item = copy.deepcopy(_get_item(state, args.item))

    review_outcome: dict[str, Any] | None = None
    if managed_hot:
        if first_session_commit is None:
            first_session_commit = _recovered_first_commit_proof(
                session_dir, staged_item
            ) or _verify_session_hot_key(session_dir, first_commit_key)
        review_outcome = _record_hot_morning_outcome(
            repo,
            staged_state,
            staged_item,
            args.item,
            current_question_minimal=bool(
                getattr(args, "current_only", False)
            ),
        )
        canonical_event = review_outcome["event"]
        review_event_id = str(canonical_event["event_id"])
        if existing_event_id and str(existing_event_id) != review_event_id:
            raise SessionError(
                "managed canonical outcome conflicts with the existing session binding"
            )
        state, binding_created = _bind_first_to_loop(
            repo,
            session_id,
            args.item,
            review_event_id,
            event=canonical_event,
        )
        binding_payload = {
            "item_id": args.item,
            "canonical_event_id": review_event_id,
        }
        binding_key = _session_hot_event_key(
            session_id,
            "first_bound_to_loop",
            binding_payload,
            purpose=f"item:{args.item}:canonical-binding",
        )
        binding_session_commit = _state_hot_commit(state)
        if binding_session_commit is None:
            binding_session_commit = _verify_session_hot_key(
                session_dir, binding_key
            )
    else:
        first_session_commit = None
        binding_session_commit = None
        if existing_event_id:
            review_event_id = str(existing_event_id)
            binding_created = False
        else:
            synced = sync_session(repo, session_id, item_id=args.item)
            review_event_id = str(synced["outcome_event_ids"].get(args.item) or "")
            if not review_event_id:
                raise SessionError("canonical synchronization did not return an outcome event")
            state, binding_created = _bind_first_to_loop(
                repo, session_id, args.item, review_event_id
            )
        canonical_event = get_review_event(repo, review_event_id)
    return {
        "action": "record-first",
        **session_summary(state),
        "item": _item_public_view(state["items"][args.item]),
        "review_event_id": review_event_id,
        "created_session_event": created_session_event,
        "resumed_unbound": resumed_unbound,
        "binding_created": binding_created,
        "classification_normalized": args.result != normalized_result,
        "normalized_result": normalized_result,
        "managed_hot_path": managed_hot,
        "canonical_event": canonical_event,
        "review_commit_receipt_ref": (
            review_outcome.get("commit_receipt_ref")
            if review_outcome is not None
            else None
        ),
        "review_commit_receipt_sha256": (
            review_outcome.get("commit_receipt_sha256")
            if review_outcome is not None
            else None
        ),
        "review_verification": (
            review_outcome.get("verification")
            if review_outcome is not None
            else None
        ),
        "review_receipt_verification": (
            review_outcome.get("receipt_verification")
            if review_outcome is not None
            else None
        ),
        "review_scan_stats": (
            review_outcome.get("review_scan_stats")
            if review_outcome is not None
            else None
        ),
        "session_first_commit": first_session_commit,
        "session_binding_commit": binding_session_commit,
        "session_binding_receipt_sha256": (
            binding_session_commit.get("receipt_sha256")
            if isinstance(binding_session_commit, dict)
            else None
        ),
    }


def _legacy_first_from_outcome(event: dict[str, Any]) -> dict[str, Any]:
    canonical_result = str(event.get("first_result"))
    if canonical_result == "uncertain":
        result = "blank"
    elif canonical_result == "independent_correct" and event.get("fragile_mastery"):
        result = "fragile_correct"
    else:
        result = canonical_result
    if result not in FIRST_RESULTS:
        raise SessionError(f"canonical event has invalid first result: {canonical_result}")
    first_break = _validate_first_break(str(event.get("first_break") or ""))
    provenance = normalize_first_break_provenance(
        event.get("first_break_provenance"), first_break
    )
    response = event.get("response_seconds")
    response_seconds = (
        float(response) if isinstance(response, (int, float)) and response > 0 else 0.0
    )
    return {
        "result": result,
        "choice_result": str(event.get("choice_result") or "not_applicable"),
        "reasoning_result": str(event.get("reasoning_result") or "not_observed"),
        "confidence": str(event.get("confidence")),
        "response_seconds": response_seconds,
        "prompt_level": str(event.get("prompt_level")),
        "first_break": first_break,
        "first_break_provenance": provenance,
        "recorded_at": str(event.get("event_time")),
        "canonical_event_id": str(event.get("event_id")),
        **(
            {
                "managed_user_reply_binding_sha256": event[
                    "managed_user_reply_binding_sha256"
                ]
            }
            if event.get("managed_user_reply_binding_sha256") is not None
            else {}
        ),
    }


def _same_first_projection(existing: dict[str, Any], expected: dict[str, Any]) -> bool:
    fields = (
        "result",
        "confidence",
        "response_seconds",
        "prompt_level",
        "first_break",
        "recorded_at",
    )
    return all(existing.get(field) == expected.get(field) for field in fields) and (
        existing.get("managed_user_reply_binding_sha256")
        == expected.get("managed_user_reply_binding_sha256")
    ) and (
        str(existing.get("choice_result") or "not_applicable")
        == str(expected.get("choice_result") or "not_applicable")
    ) and (
        str(existing.get("reasoning_result") or "not_observed")
        == str(expected.get("reasoning_result") or "not_observed")
    ) and (
        normalize_first_break_provenance(
            existing.get("first_break_provenance"), existing.get("first_break")
        )
        == normalize_first_break_provenance(
            expected.get("first_break_provenance"), expected.get("first_break")
        )
    )


def _first_matches_outcome(existing: dict[str, Any], event: dict[str, Any]) -> bool:
    try:
        _, session_result, _ = _normalized_session_first_result(
            str(existing.get("result")),
            choice_result=str(
                existing.get("choice_result") or "not_applicable"
            ),
            reasoning_result=str(
                existing.get("reasoning_result") or "not_observed"
            ),
            prompt_level=str(existing.get("prompt_level") or ""),
        )
    except SessionError:
        return False
    response = existing.get("response_seconds")
    projected_response = (
        float(response) if isinstance(response, (int, float)) and response > 0 else None
    )
    canonical_response = event.get("response_seconds")
    canonical_response = (
        float(canonical_response)
        if isinstance(canonical_response, (int, float)) and canonical_response > 0
        else None
    )
    try:
        projected_provenance = normalize_first_break_provenance(
            existing.get("first_break_provenance"), existing.get("first_break")
        )
        canonical_provenance = normalize_first_break_provenance(
            event.get("first_break_provenance"), event.get("first_break")
        )
    except ReviewLoopError:
        return False
    return all(
        (
            session_result == event.get("first_result"),
            str(existing.get("choice_result") or "not_applicable")
            == str(event.get("choice_result") or "not_applicable"),
            str(existing.get("reasoning_result") or "not_observed")
            == str(event.get("reasoning_result") or "not_observed"),
            existing.get("confidence") == event.get("confidence"),
            existing.get("prompt_level") == event.get("prompt_level"),
            existing.get("first_break") == event.get("first_break"),
            projected_provenance == canonical_provenance,
            existing.get("recorded_at") == event.get("event_time"),
            projected_response == canonical_response,
            existing.get("managed_user_reply_binding_sha256")
            == event.get("managed_user_reply_binding_sha256"),
        )
    )


def _same_first_input(existing: dict[str, Any], payload: dict[str, Any]) -> bool:
    fields = (
        "result",
        "confidence",
        "response_seconds",
        "prompt_level",
        "first_break",
    )
    return all(existing.get(field) == payload.get(field) for field in fields) and (
        existing.get("managed_user_reply_binding_sha256")
        == payload.get("managed_user_reply_binding_sha256")
    ) and (
        str(existing.get("choice_result") or "not_applicable")
        == str(payload.get("choice_result") or "not_applicable")
    ) and (
        str(existing.get("reasoning_result") or "not_observed")
        == str(payload.get("reasoning_result") or "not_observed")
    ) and (
        normalize_first_break_provenance(
            existing.get("first_break_provenance"), existing.get("first_break")
        )
        == normalize_first_break_provenance(
            payload.get("first_break_provenance"), payload.get("first_break")
        )
    )


def _record_hot_morning_outcome(
    repo: Path,
    state: dict[str, Any],
    item: dict[str, Any],
    item_id: str,
    *,
    current_question_minimal: bool = False,
) -> dict[str, Any]:
    """Create or exactly replay one canonical outcome without session replay."""

    first = item.get("first")
    if not isinstance(first, dict):
        raise SessionError("hot canonical outcome requires one staged first answer")
    old_result = str(first.get("result") or "")
    _, first_result, fragile = _normalized_session_first_result(
        old_result,
        choice_result=str(first.get("choice_result") or "not_applicable"),
        reasoning_result=str(first.get("reasoning_result") or "not_observed"),
        prompt_level=str(first.get("prompt_level") or ""),
    )
    response = first.get("response_seconds")
    response_value = (
        float(response)
        if isinstance(response, (int, float)) and float(response) > 0
        else None
    )
    first_break = str(first.get("first_break") or "")
    try:
        provenance = normalize_first_break_provenance(
            first.get("first_break_provenance"), first_break
        )
    except ReviewLoopError as exc:
        raise SessionError(str(exc)) from exc
    source_id = str(item.get("source_id") or "")
    practice_mode = item.get("practice_mode")
    linked_kwargs: dict[str, Any] = {}
    if practice_mode and not current_question_minimal:
        linked_kwargs = {
            "practice_mode": str(practice_mode),
            "anchor_formal_node_id": item.get("anchor_formal_node_id"),
            "variant_of_formal_node_id": item.get(
                "variant_of_formal_node_id"
            ),
            "followup_event_id": item.get("followup_event_id"),
            "parent_trigger_event_id": item.get("trigger_event_id"),
            "context_slice_id": item.get("context_slice_id"),
            "candidate_id": item.get("candidate_id"),
            "source_ref": item.get("source_ref"),
            "source_sha256": item.get("source_sha256"),
            "selection_receipt_ref": item.get("selection_receipt_ref"),
            "selection_receipt_sha256": item.get(
                "selection_receipt_sha256"
            ),
        }
    idempotency_key = f"morning:{state['session_id']}:{item_id}:first"
    event_time = str(first.get("recorded_at") or "")
    observed_date = review_loop._event_local_date(event_time).isoformat()
    session_review_date = str(state.get("review_date") or "")
    try:
        import review_hot_state_408 as review_hot
        import review_outcome_hot_408 as review_outcome_hot
    except ImportError as exc:
        raise SessionError("managed canonical outcome modules are unavailable") from exc
    try:
        outcome = review_outcome_hot.record_outcome_hot(
            repo,
            source="morning_review",
            session_id=str(state["session_id"]),
            item_id=item_id,
            source_id=source_id,
            mechanism_key=str(item.get("mechanism_key") or source_id),
            knowledge_point=str(item.get("knowledge_point") or ""),
            first_result=first_result,
            choice_result=str(first.get("choice_result") or "not_applicable"),
            reasoning_result=str(
                first.get("reasoning_result") or "not_observed"
            ),
            confidence=str(first.get("confidence") or ""),
            prompt_level=str(first.get("prompt_level") or ""),
            first_break=first_break,
            first_break_provenance=provenance,
            event_time=event_time,
            observed_date=observed_date,
            session_review_date=session_review_date,
            idempotency_key=idempotency_key,
            response_seconds=response_value,
            latency_state="normal" if response_value is not None else "unmeasured",
            # For an exact session-bound action, the hot-state adapter derives
            # cross-day from the canonical action origin_date and this observed
            # date.  Do not manufacture a value from the session review date.
            cross_day=(False if practice_mode and not current_question_minimal else None),
            question_valid=True,
            formal_node_id=item.get("formal_node_id"),
            review_unit_id=(
                item.get("review_unit_id")
                or (source_id if REVIEW_UNIT_RE.fullmatch(source_id) else None)
            ),
            # current_question_minimal protects the read scope.  It must not
            # discard the one action coordinate already frozen into this item.
            trigger_action_id=item.get("action_id"),
            legacy_import=(
                state.get("queue_schema") != "morning_review_action_queue_v3"
            ),
            fragile_override=fragile,
            presentation_kind=item.get("item_kind"),
            managed_user_reply_binding_sha256=first.get(
                "managed_user_reply_binding_sha256"
            ),
            _from_session_sync=True,
            current_question_minimal=current_question_minimal,
            **linked_kwargs,
        )
        if not outcome.get("commit_receipt_sha256"):
            receipt = review_hot.lookup_commit_receipt(
                repo, idempotency_key=idempotency_key
            )
            if receipt.get("status") == "found":
                receipt_verification = review_hot.verify_commit(
                    repo, receipt_sha256=str(receipt["receipt_sha256"])
                )
                if receipt_verification.get("status") != "verified":
                    raise SessionError(
                        "replayed canonical review receipt failed verification"
                    )
                outcome = {
                    **outcome,
                    "commit_receipt_ref": receipt["receipt_ref"],
                    "commit_receipt_sha256": receipt["receipt_sha256"],
                    "receipt_verification": receipt_verification,
                    "receipt_lookup": receipt,
                }
            elif receipt.get("status") != "no_hot_commit_receipt":
                raise SessionError(
                    "replayed canonical review receipt lookup failed closed: "
                    + str(receipt.get("status") or "unknown")
                )
            else:
                outcome = {**outcome, "receipt_lookup": receipt}
        else:
            outcome = {
                **outcome,
                "receipt_verification": outcome.get("verification"),
            }
    except SessionError:
        raise
    except (
        review_outcome_hot.HotOutcomeError,
        review_hot.HotStateError,
        OSError,
    ) as exc:
        raise SessionError(f"managed canonical outcome failed closed: {exc}") from exc
    event = outcome.get("event")
    if (
        not isinstance(event, dict)
        or event.get("event_kind") != "outcome"
        or event.get("source") != "morning_review"
        or event.get("session_id") != state.get("session_id")
        or event.get("item_id") != item_id
    ):
        raise SessionError("managed canonical outcome identity is invalid")
    verification = outcome.get("verification")
    if not isinstance(verification, dict) or verification.get("status") != "verified":
        raise SessionError("managed canonical outcome lacks bounded verification")
    return outcome


def _same_repair_input(existing: dict[str, Any], payload: dict[str, Any]) -> bool:
    if existing.get("result") != payload.get("result"):
        return False
    if payload.get("result") == "relearn_required":
        return True
    return all(
        existing.get(field) == payload.get(field)
        for field in ("confidence", "response_seconds", "prompt_level")
    )


def _repair_event_replay_matches(
    existing: dict[str, Any], expected: dict[str, Any]
) -> bool:
    """Accept an exact new event or one verified pre-date-split repair row."""

    if review_loop.canonical_json(existing) == review_loop.canonical_json(expected):
        return True
    session_date = expected.get("session_review_date")
    if (
        not session_date
        or "session_review_date" in existing
        or existing.get("source") != "morning_review"
        or existing.get("observed_date") != session_date
        or review_loop._event_local_date(str(existing.get("event_time") or ""))
        != review_loop._parse_date(
            str(expected.get("observed_date") or ""), "observed_date"
        )
    ):
        return False
    historical = dict(expected)
    historical.pop("session_review_date", None)
    historical["observed_date"] = session_date
    request = {
        "session_id": historical["session_id"],
        "item_id": historical["item_id"],
        "source_id": historical["source_id"],
        "result": historical["correction_result"],
        "confidence": historical["confidence"],
        "prompt_level": historical["prompt_level"],
        "event_time": historical["event_time"],
        "observed_date": session_date,
        "attempt": historical["attempt"],
        "response_seconds": historical["response_seconds"],
        "legacy_import": historical["legacy_import"],
        "source": historical["source"],
    }
    historical["request_hash"] = hashlib.sha256(
        review_loop.canonical_json(request).encode("utf-8")
    ).hexdigest()
    return review_loop.canonical_json(existing) == review_loop.canonical_json(
        historical
    )


def _record_hot_morning_repair(
    repo: Path,
    state: dict[str, Any],
    item: dict[str, Any],
    item_id: str,
    *,
    attempt: int,
) -> dict[str, Any]:
    """Commit or exactly replay one canonical repair without a ledger scan."""

    repairs = item.get("repair_attempts") or []
    if not isinstance(repairs, list) or not 1 <= attempt <= len(repairs):
        raise SessionError("hot canonical repair requires one staged repair attempt")
    row = repairs[attempt - 1]
    if not isinstance(row, dict):
        raise SessionError("staged repair attempt is invalid")
    result = {
        "fragile_correct": "independent_correct",
        "blank": "uncertain",
    }.get(str(row.get("result") or ""), str(row.get("result") or ""))
    if result not in review_loop.CORRECTION_RESULTS - {"not_attempted"}:
        raise SessionError("staged repair result is invalid")
    response = row.get("response_seconds")
    response_value = (
        float(response)
        if isinstance(response, (int, float)) and float(response) > 0
        else None
    )
    source_id = str(item.get("source_id") or "")
    session_id = str(state.get("session_id") or "")
    event_time = str(row.get("recorded_at") or "")
    observed_date = review_loop._event_local_date(event_time).isoformat()
    session_review_date = str(state.get("review_date") or "")
    idempotency_key = f"morning:{session_id}:{item_id}:repair:{attempt}"
    legacy_import = state.get("queue_schema") not in {
        "morning_review_action_queue_v3"
    }
    try:
        event = review_loop.build_repair_event(
            source="morning_review",
            session_id=session_id,
            item_id=item_id,
            source_id=source_id,
            result=result,
            confidence=row.get("confidence"),
            prompt_level=row.get("prompt_level"),
            event_time=event_time,
            observed_date=observed_date,
            session_review_date=session_review_date,
            idempotency_key=idempotency_key,
            attempt=attempt,
            response_seconds=response_value,
            legacy_import=legacy_import,
        )
    except ReviewLoopError as exc:
        raise SessionError(str(exc)) from exc
    try:
        import review_hot_state_408 as review_hot
    except ImportError as exc:
        raise SessionError("managed canonical repair module is unavailable") from exc
    try:
        view = review_hot.begin_view(
            repo,
            idempotency_key=idempotency_key,
            session_id=session_id,
            item_id=item_id,
            identity=source_id,
            observed_date=observed_date,
            event_kind="repair",
            repair_attempt=attempt,
        )
        existing_ref = view.get("existing_event_ref")
        if existing_ref is not None:
            found = review_hot.lookup_event(
                repo, event_id=str(existing_ref.get("event_id") or "")
            )
            existing = found.get("event")
            if (
                found.get("status") != "found"
                or not isinstance(existing, dict)
                or not _repair_event_replay_matches(existing, event)
            ):
                raise SessionError(
                    "replayed canonical repair conflicts with staged evidence"
                )
            receipt = review_hot.lookup_commit_receipt(
                repo, event_id=str(existing["event_id"])
            )
            if receipt.get("status") != "found":
                raise SessionError(
                    "replayed canonical repair lacks an event-specific commit receipt"
                )
            verified = review_hot.verify_commit(
                repo, receipt_sha256=str(receipt["receipt_sha256"])
            )
            committed = {
                "status": "already_committed",
                "receipt_ref": receipt["receipt_ref"],
                "receipt_sha256": receipt["receipt_sha256"],
            }
        else:
            with runtime_lock(repo):
                committed = review_hot.append_commit(
                    repo,
                    event=event,
                    expected_precondition=view["precondition"],
                    runtime_lock_held=True,
                )
            verified = review_hot.verify_commit(
                repo, receipt_sha256=str(committed["receipt_sha256"])
            )
    except SessionError:
        raise
    except (review_hot.HotStateError, OSError, KeyError) as exc:
        raise SessionError(f"managed canonical repair failed closed: {exc}") from exc
    if verified.get("status") != "verified":
        raise SessionError("managed canonical repair lacks bounded verification")
    return {
        "created": committed.get("status") == "committed",
        "event": event,
        "commit_status": committed.get("status"),
        "commit_receipt_ref": committed.get("receipt_ref"),
        "commit_receipt_sha256": committed.get("receipt_sha256"),
        "verification": verified,
        "review_scan_stats": view.get("scan_stats"),
        "formal_write_count": 0,
    }


def _repair_matches_event(
    attempt_row: dict[str, Any], event: dict[str, Any]
) -> bool:
    result = {
        "fragile_correct": "independent_correct",
        "blank": "uncertain",
    }.get(
        str(attempt_row.get("result") or ""),
        str(attempt_row.get("result") or ""),
    )
    response = attempt_row.get("response_seconds")
    projected_response = (
        float(response)
        if isinstance(response, (int, float)) and float(response) > 0
        else None
    )
    canonical_response = event.get("response_seconds")
    canonical_response = (
        float(canonical_response)
        if isinstance(canonical_response, (int, float))
        and float(canonical_response) > 0
        else None
    )
    return all(
        (
            event.get("event_kind") == "repair",
            event.get("correction_result") == result,
            event.get("confidence") == attempt_row.get("confidence"),
            event.get("prompt_level") == attempt_row.get("prompt_level"),
            event.get("event_time") == attempt_row.get("recorded_at"),
            event.get("attempt") == attempt_row.get("attempt"),
            event.get("idempotency_key")
            == attempt_row.get("review_idempotency_key"),
            canonical_response == projected_response,
        )
    )


def _bind_repair_to_loop(
    repo: Path,
    session_id: str,
    item_id: str,
    attempt_number: int,
    event: dict[str, Any],
) -> tuple[dict[str, Any], bool]:
    if (
        event.get("event_kind") != "repair"
        or event.get("source") != "morning_review"
        or event.get("session_id") != session_id
        or event.get("item_id") != item_id
        or event.get("attempt") != attempt_number
    ):
        raise SessionError("canonical repair does not match the session target")
    session_dir = _session_dir(repo, session_id)
    with session_lock(session_dir):
        state = _load_state(session_dir, session_id)
        _ensure_active(state)
        item = _get_item(state, item_id)
        attempts = item.get("repair_attempts") or []
        if not 1 <= attempt_number <= len(attempts):
            raise SessionError("session repair attempt is unavailable for binding")
        attempt = attempts[attempt_number - 1]
        if event.get("source_id") != item.get("source_id") or not _repair_matches_event(
            attempt, event
        ):
            raise SessionError("canonical repair conflicts with the staged attempt")
        bound = attempt.get("canonical_event_id")
        if bound and bound != event.get("event_id"):
            raise SessionError("repair is already bound to another canonical event")
        binding_created = not bool(bound)
        if binding_created:
            payload = {
                "item_id": item_id,
                "attempt": attempt_number,
                "canonical_event_id": str(event["event_id"]),
            }
            state = _commit_event(
                session_dir,
                state,
                session_id,
                "repair_bound_to_loop",
                payload,
                idempotency_key=(
                    _session_hot_event_key(
                        session_id,
                        "repair_bound_to_loop",
                        payload,
                        purpose=f"item:{item_id}:repair:{attempt_number}:binding",
                    )
                    if _session_hot_managed(session_dir)
                    else None
                ),
            )
    return state, binding_created


def _bind_first_to_loop(
    repo: Path,
    session_id: str,
    item_id: str,
    event_id: str,
    *,
    event: dict[str, Any] | None = None,
) -> tuple[dict[str, Any], bool]:
    event = event if event is not None else get_review_event(repo, event_id)
    if (
        event.get("event_kind") != "outcome"
        or event.get("source") != "morning_review"
        or event.get("session_id") != session_id
        or event.get("item_id") != item_id
    ):
        raise SessionError("canonical event does not match the session binding target")
    session_dir = _session_dir(repo, session_id)
    with session_lock(session_dir):
        state = _load_state(session_dir, session_id)
        _ensure_active(state)
        item = _get_item(state, item_id)
        existing = item.get("first")
        if existing is None or not _first_matches_outcome(existing, event):
            raise SessionError("canonical outcome conflicts with the staged first answer")
        bound = existing.get("canonical_event_id")
        if bound and bound != event_id:
            raise SessionError("first answer is already bound to another canonical event")
        binding_created = not bool(bound)
        if binding_created:
            binding_payload = {
                "item_id": item_id,
                "canonical_event_id": event_id,
            }
            state = _commit_event(
                session_dir,
                state,
                session_id,
                "first_bound_to_loop",
                binding_payload,
                idempotency_key=(
                    _session_hot_event_key(
                        session_id,
                        "first_bound_to_loop",
                        binding_payload,
                        purpose=f"item:{item_id}:canonical-binding",
                    )
                    if _session_hot_managed(session_dir)
                    else None
                ),
            )
    return state, binding_created


def _canonical_outcomes_for_item(
    repo: Path, session_id: str, item_id: str
) -> list[dict[str, Any]]:
    return [
        event
        for event in load_review_loop_state(repo).get("events", [])
        if event.get("event_kind") == "outcome"
        and event.get("source") == "morning_review"
        and event.get("session_id") == session_id
        and event.get("item_id") == item_id
    ]


def command_repair_unbound_first(args: argparse.Namespace) -> dict[str, Any]:
    repo, session_id = _with_state(args)
    safe_break = _validate_first_break(args.first_break)
    provenance = normalize_first_break_provenance(
        args.first_break_provenance, safe_break
    )
    session_dir = _session_dir(repo, session_id)
    outcomes = _canonical_outcomes_for_item(repo, session_id, args.item)
    if len(outcomes) > 1:
        raise SessionError("multiple canonical outcomes exist for the recovery target")
    managed_hot = _session_hot_managed(session_dir)
    repaired_event_created = False
    classification_normalized = False
    first_session_commit: dict[str, Any] | None = None
    repair_session_commit: dict[str, Any] | None = None
    already_bound_event_id = ""
    with session_lock(session_dir):
        state = _load_state(session_dir, session_id)
        _ensure_active(state)
        item = _get_item(state, args.item)
        first = item.get("first")
        if first is None:
            raise SessionError("no staged first answer is available for recovery")
        current_provenance = normalize_first_break_provenance(
            first.get("first_break_provenance"), first.get("first_break")
        )
        normalized_result, _, _ = _normalized_session_first_result(
            str(first.get("result") or ""),
            choice_result=str(first.get("choice_result") or "not_applicable"),
            reasoning_result=str(
                first.get("reasoning_result") or "not_observed"
            ),
            prompt_level=str(first.get("prompt_level") or ""),
        )
        classification_normalized = first.get("result") != normalized_result
        bound_event_id = str(first.get("canonical_event_id") or "")
        if bound_event_id:
            if (
                len(outcomes) != 1
                or str(outcomes[0].get("event_id") or "") != bound_event_id
                or first.get("first_break") != safe_break
                or current_provenance != provenance
                or classification_normalized
            ):
                raise SessionError(
                    "recovery retry conflicts with the bound canonical outcome"
                )
            already_bound_event_id = bound_event_id
            staged_state = copy.deepcopy(dict(state))
            staged_item = copy.deepcopy(item)
        elif item.get("feedback_revealed") or item.get("repair_attempts"):
            raise SessionError("unbound first recovery is forbidden after feedback or repair")
        else:
            if outcomes and (
                first.get("first_break") != safe_break
                or current_provenance != provenance
            ):
                raise SessionError("recovery retry conflicts with the canonical outcome")
            recovery_event: dict[str, Any] | None = None
            needs_recovery_evidence = managed_hot and not item.get(
                "recovery_first_commit_receipt_sha256"
            )
            if needs_recovery_evidence:
                recovery_event, first_session_commit = (
                    _managed_first_commit_for_recovery(session_dir, args.item)
                )
            elif managed_hot:
                first_session_commit = _recovered_first_commit_proof(
                    session_dir, item
                )
            if (
                first.get("first_break") != safe_break
                or current_provenance != provenance
                or classification_normalized
                or needs_recovery_evidence
            ):
                repair_payload: dict[str, Any] = {
                    "item_id": args.item,
                    "first_break": safe_break,
                    "first_break_provenance": provenance,
                }
                if classification_normalized:
                    repair_payload.update(
                        {
                            "previous_result": first.get("result"),
                            "normalized_result": normalized_result,
                        }
                    )
                if recovery_event is not None and first_session_commit is not None:
                    repair_payload.update(
                        {
                            "source_first_event_id": recovery_event["event_id"],
                            "source_first_commit_receipt_sha256": (
                                first_session_commit["receipt_sha256"]
                            ),
                        }
                    )
                state = _commit_event(
                    session_dir,
                    state,
                    session_id,
                    "unbound_first_repaired",
                    repair_payload,
                )
                repair_session_commit = _state_hot_commit(state)
                repaired_event_created = True
            staged_state = copy.deepcopy(dict(state))
            staged_item = copy.deepcopy(_get_item(state, args.item))

    if already_bound_event_id:
        return {
            "action": "repair-unbound-first",
            **session_summary(staged_state),
            "item": _item_public_view(staged_item),
            "review_event_id": already_bound_event_id,
            "repaired_event_created": False,
            "classification_normalized": False,
            "binding_created": False,
            "managed_hot_path": managed_hot,
            "replayed": True,
        }

    review_outcome: dict[str, Any] | None = None
    if managed_hot:
        if first_session_commit is None:
            first_session_commit = _recovered_first_commit_proof(
                session_dir, staged_item
            )
        if first_session_commit is None:
            raise SessionError(
                "managed recovery lacks the original first-answer commit receipt"
            )
        review_outcome = _record_hot_morning_outcome(
            repo, staged_state, staged_item, args.item
        )
        canonical_event = review_outcome["event"]
        review_event_id = str(canonical_event["event_id"])
        if outcomes and str(outcomes[0].get("event_id") or "") != review_event_id:
            raise SessionError("managed recovery conflicts with the canonical outcome")
    else:
        if outcomes:
            review_event_id = str(outcomes[0]["event_id"])
            canonical_event = outcomes[0]
        else:
            synced = sync_session(repo, session_id, item_id=args.item)
            review_event_id = str(synced["outcome_event_ids"].get(args.item) or "")
            if not review_event_id:
                raise SessionError(
                    "canonical synchronization did not return an outcome event"
                )
            canonical_event = get_review_event(repo, review_event_id)
    state, binding_created = _bind_first_to_loop(
        repo,
        session_id,
        args.item,
        review_event_id,
        event=canonical_event,
    )
    binding_session_commit = _state_hot_commit(state)
    if managed_hot and binding_session_commit is None:
        binding_payload = {
            "item_id": args.item,
            "canonical_event_id": review_event_id,
        }
        binding_key = _session_hot_event_key(
            session_id,
            "first_bound_to_loop",
            binding_payload,
            purpose=f"item:{args.item}:canonical-binding",
        )
        binding_session_commit = _verify_session_hot_key(
            session_dir, binding_key
        )
    return {
        "action": "repair-unbound-first",
        **session_summary(state),
        "item": _item_public_view(state["items"][args.item]),
        "review_event_id": review_event_id,
        "repaired_event_created": repaired_event_created,
        "classification_normalized": classification_normalized,
        "binding_created": binding_created,
        "managed_hot_path": managed_hot,
        "canonical_event": canonical_event,
        "review_commit_receipt_sha256": (
            review_outcome.get("commit_receipt_sha256")
            if review_outcome is not None
            else None
        ),
        "review_verification": (
            review_outcome.get("verification")
            if review_outcome is not None
            else None
        ),
        "review_receipt_verification": (
            review_outcome.get("receipt_verification")
            if review_outcome is not None
            else None
        ),
        "session_first_commit": first_session_commit,
        "session_repair_commit": repair_session_commit,
        "session_binding_commit": binding_session_commit,
        "replayed": False,
    }


def _followup_from_personalization(
    repo: Path,
    *,
    session_id: str,
    event: dict[str, Any],
    personalization: dict[str, Any],
    mode: str,
    personalization_state_dir: str | Path | None = None,
) -> dict[str, Any]:
    """Register at most one verified linked item; all failures stay fail-soft."""

    if personalization.get("status") != "ready":
        return {
            "status": "not_registered",
            "reason_code": "personalization_unavailable",
            "registered": False,
        }
    context = personalization.get("context") or {}
    selected = context.get("selected_practice") or {}
    if selected.get("status") != "selected":
        return {
            "status": "not_registered",
            "reason_code": selected.get("reason_code") or "no_selected_practice",
            "registered": False,
        }
    if event.get("practice_mode"):
        return {
            "status": "not_registered",
            "reason_code": "recursive_followup_suppressed",
            "registered": False,
        }
    if mode != "verified" or not selected.get("schedule_allowed"):
        return {
            "status": "shadow_only",
            "reason_code": "activation_not_enabled",
            "registered": False,
            "candidate_id": selected.get("candidate_id"),
        }
    required = (
        "candidate_id",
        "source_id",
        "source_ref",
        "source_sha256",
        "mechanism_key",
        "knowledge_point",
    )
    if any(not str(selected.get(field) or "") for field in required):
        return {
            "status": "unavailable",
            "reason_code": "selected_practice_incomplete",
            "registered": False,
        }
    selection_receipt_ref = str(
        personalization.get("selection_receipt_ref") or ""
    )
    selection_receipt_sha256 = str(
        personalization.get("selection_receipt_sha256") or ""
    )
    if not selection_receipt_ref or not selection_receipt_sha256:
        return {
            "status": "unavailable",
            "reason_code": "selection_receipt_missing",
            "registered": False,
        }
    practice_mode = str(selected.get("practice_mode") or "")
    formal_node_id = selected.get("formal_node_id")
    variant_of = (
        selected.get("anchor_formal_node_id") if practice_mode == "variant" else None
    )
    try:
        return schedule_followup(
            repo,
            session_id,
            scheduling_key=(
                f"personalization:{event['event_id']}:{selected['candidate_id']}:v1"
            ),
            trigger_event_id=str(event["event_id"]),
            context_slice_id=str(personalization["slice_id"]),
            candidate_id=str(selected["candidate_id"]),
            source_id=str(selected["source_id"]),
            source_ref=str(selected["source_ref"]),
            source_sha256=str(selected["source_sha256"]),
            selection_receipt_ref=selection_receipt_ref,
            selection_receipt_sha256=selection_receipt_sha256,
            mechanism_key=str(selected["mechanism_key"]),
            knowledge_point=str(selected["knowledge_point"]),
            practice_mode=practice_mode,
            formal_node_id=(str(formal_node_id) if formal_node_id else None),
            anchor_formal_node_id=(
                str(selected["anchor_formal_node_id"])
                if selected.get("anchor_formal_node_id")
                else None
            ),
            variant_of_formal_node_id=(str(variant_of) if variant_of else None),
            review_unit_id=(
                str(selected["review_unit_id"])
                if selected.get("review_unit_id")
                else None
            ),
            activation="verified_existing",
            trigger_event=event,
            personalization_state_dir=personalization_state_dir,
        )
    except (SessionError, OSError):
        return {
            "status": "unavailable",
            "reason_code": "followup_registration_failed",
            "registered": False,
            "candidate_id": selected.get("candidate_id"),
        }


def command_answer_turn(args: argparse.Namespace) -> dict[str, Any]:
    """Reject the retired combined answer, personalization and continuation path."""

    del args
    raise SessionError(
        "legacy_answer_turn_retired_use_managed_408_current_turn"
    )

    repo, _ = _with_state(args)
    recorded = command_record_first(args)
    event_id = str(recorded["review_event_id"])
    event = recorded.get("canonical_event")
    if not isinstance(event, dict):
        if recorded.get("managed_hot_path"):
            raise SessionError(
                "managed answer turn did not return its canonical event"
            )
        event = get_review_event(repo, event_id)
    managed_hot_path = bool(recorded.get("managed_hot_path"))
    capture_receipt: dict[str, Any] | None = None
    if managed_hot_path:
        verification = recorded.get("review_verification")
        receipt_verification = recorded.get("review_receipt_verification")
        if (
            not isinstance(verification, dict)
            or verification.get("status") != "verified"
            or not recorded.get("review_commit_receipt_sha256")
            or not isinstance(receipt_verification, dict)
            or receipt_verification.get("status") != "verified"
        ):
            raise SessionError(
                "managed review outcome lacks a verified bounded commit receipt"
            )
        for label in ("session_first_commit", "session_binding_commit"):
            proof = recorded.get(label)
            if not isinstance(proof, dict) or proof.get("status") not in {
                "committed",
                "already_committed",
                "pass",
            } or not proof.get("receipt_sha256"):
                raise SessionError(
                    f"managed answer turn lacks bounded {label} proof"
                )
        review_report = {
            "status": "HOT_VERIFIED",
            "formal_write_count": 0,
            "full_ledger_audit_count": 0,
        }
        if event.get("first_result") in {"partial", "wrong", "uncertain"}:
            capture_hot_adapter = _morning_capture_hot_module()
            try:
                capture_receipt = capture_hot_adapter.capture_managed_morning_outcome(
                    repo,
                    event,
                    review_commit_receipt_sha256=str(
                        recorded["review_commit_receipt_sha256"]
                    ),
                    session_first_commit=recorded["session_first_commit"],
                    session_binding_commit=recorded["session_binding_commit"],
                )
            except capture_hot_adapter.MorningCaptureHotError as exc:
                raise SessionError(
                    f"managed morning failure capture failed closed: {exc}"
                ) from exc
            if (
                capture_receipt.get("quality_status")
                != "awaiting_sol_formalization"
                or (capture_receipt.get("capture_joint_verification") or {}).get(
                    "status"
                )
                != "PASS"
            ):
                raise SessionError(
                    "managed morning failure capture lacks a verified joint receipt"
                )
    else:
        if event.get("first_result") in {"partial", "wrong", "uncertain"}:
            capture_receipt = capture_review_event(event_id, repo_root=repo)
            if capture_receipt.get("quality_status") != "awaiting_sol_formalization":
                raise SessionError(
                    "morning failure capture did not remain awaiting_sol_formalization"
                )
        review_report = audit_review_loop(repo)
        if (
            review_report.get("status") != "PASS"
            or int(review_report.get("formal_write_count", -1)) != 0
        ):
            raise SessionError(
                "review-loop audit failed before feedback reveal: "
                + "; ".join(review_report.get("errors") or [])
            )
    capture_report: dict[str, Any] | None = None
    if managed_hot_path and capture_receipt is not None:
        capture_report = {
            "status": "HOT_VERIFIED",
            "receipt_sha256": capture_receipt.get("receipt_sha256"),
            "formal_write_count": 0,
            "full_ledger_audit_count": 0,
        }
    elif not managed_hot_path and (
        capture_receipt is not None or (capture_root(repo) / "state.json").is_file()
    ):
        capture_report = audit_fact_capture(repo_root=repo)
        if capture_report.get("status") != "PASS":
            raise SessionError("fact-capture audit failed before feedback reveal")

    revealed = command_reveal_feedback(
        args,
        review_receipt_sha256=recorded.get("review_commit_receipt_sha256"),
        session_binding_receipt_sha256=recorded.get(
            "session_binding_receipt_sha256"
        ),
    )
    personalization_mode = str(getattr(args, "personalization_mode", "verified"))
    if recorded.get("interaction_mode") == "frozen_prepared_pack":
        personalization = {
            "status": "deferred_to_prepared_pack",
            "reason_code": "frozen_session_uses_prepared_target_and_feedback",
            "read_only": True,
            "formal_write_count": 0,
        }
        linked_followup = {
            "status": "not_registered",
            "reason_code": "dynamic_followup_disabled_for_frozen_prepared_pack",
            "registered": False,
        }
    else:
        personalization = safe_context_slice(
            repo,
            current_event=event,
            state_dir=getattr(args, "personalization_state_dir", None),
            max_bytes=int(
                getattr(
                    args,
                    "personalization_max_bytes",
                    DEFAULT_PERSONALIZATION_MAX_BYTES,
                )
            ),
            mode=personalization_mode,
        )
        linked_followup = _followup_from_personalization(
            repo,
            session_id=str(recorded["session_id"]),
            event=event,
            personalization=personalization,
            mode=personalization_mode,
            personalization_state_dir=getattr(
                args, "personalization_state_dir", None
            ),
        )
    return {
        "action": "answer-turn",
        "session_id": recorded["session_id"],
        "item_id": args.item,
        "review_event_id": event_id,
        "first_result": event.get("first_result"),
        "confidence": event.get("confidence"),
        "evidence_kind": event.get("evidence_kind"),
        "capture_id": (
            capture_receipt.get("capture_id") if capture_receipt is not None else None
        ),
        "capture_status": (
            capture_receipt.get("quality_status")
            if capture_receipt is not None
            else "not_eligible"
        ),
        "formal_write_count": review_report.get("formal_write_count"),
        "review_audit": review_report.get("status"),
        "capture_audit": (
            capture_report.get("status") if capture_report is not None else "not_applicable"
        ),
        "capture_commit_status": (
            capture_receipt.get("status") if capture_receipt is not None else None
        ),
        "capture_commit_receipt_sha256": (
            capture_receipt.get("receipt_sha256")
            if capture_receipt is not None
            else None
        ),
        "capture_joint_verification": (
            capture_receipt.get("capture_joint_verification")
            if capture_receipt is not None
            else None
        ),
        "feedback_source": revealed["feedback_source"],
        "feedback_type": revealed["feedback_type"],
        "feedback": revealed["feedback"],
        "already_revealed": revealed["already_revealed"],
        "record_replayed": not recorded["created_session_event"],
        "managed_hot_path": recorded.get("managed_hot_path", False),
        "canonical_event": event,
        "review_commit_receipt_ref": recorded.get(
            "review_commit_receipt_ref"
        ),
        "review_commit_receipt_sha256": recorded.get(
            "review_commit_receipt_sha256"
        ),
        "review_verification": recorded.get("review_verification"),
        "review_receipt_verification": recorded.get(
            "review_receipt_verification"
        ),
        "session_first_commit": recorded.get("session_first_commit"),
        "session_binding_commit": recorded.get("session_binding_commit"),
        "session_binding_receipt_sha256": recorded.get(
            "session_binding_receipt_sha256"
        ),
        "session_feedback_commit": revealed.get("session_commit"),
        "personalization": personalization,
        "linked_followup": linked_followup,
        "continuation": revealed.get("continuation"),
    }


def command_reconcile_item_from_loop(args: argparse.Namespace) -> dict[str, Any]:
    repo, session_id = _with_state(args)
    event = get_review_event(repo, args.event_id)
    if event.get("event_kind") != "outcome" or event.get("source") != "morning_review":
        raise SessionError("canonical event must be one morning_review outcome")
    if event.get("session_id") != session_id or event.get("item_id") != args.item:
        raise SessionError("canonical event session/item does not match recovery target")
    session_dir = _session_dir(repo, session_id)
    with session_lock(session_dir):
        state = _load_state(session_dir, session_id)
        _ensure_active(state)
        item = _get_item(state, args.item)
        if event.get("source_id") != item.get("source_id"):
            raise SessionError("canonical event source does not match session item")
        if review_loop.morning_session_review_date(event) != state.get(
            "review_date"
        ):
            raise SessionError("canonical event date does not match session review date")
        if item.get("action_id") and event.get("trigger_action_id") != item.get("action_id"):
            raise SessionError("canonical event trigger does not match session item")
        expected = _legacy_first_from_outcome(event)
        existing = item.get("first")
        already_reconciled = False
        already_projected = False
        binding_created = False
        if existing is not None:
            if existing.get("canonical_event_id") == event.get("event_id"):
                if not _first_matches_outcome(existing, event):
                    raise SessionError(
                        "session canonical binding conflicts with stored first answer"
                    )
                already_reconciled = True
            elif _first_matches_outcome(existing, event):
                already_projected = True
                state = _commit_event(
                    session_dir,
                    state,
                    session_id,
                    "first_bound_to_loop",
                    {
                        "item_id": args.item,
                        "canonical_event_id": str(event.get("event_id")),
                    },
                )
                binding_created = True
            else:
                raise SessionError("session already contains a conflicting first answer")
        else:
            state = _commit_event(
                session_dir,
                state,
                session_id,
                "first_reconciled_from_loop",
                {"item_id": args.item, **expected},
            )
    return {
        "action": "reconcile-item-from-loop",
        **session_summary(state),
        "item": _item_public_view(state["items"][args.item]),
        "canonical_event_id": event.get("event_id"),
        "already_reconciled": already_reconciled,
        "already_projected": already_projected,
        "binding_created": binding_created,
    }


def _current_queue(repo: Path, state: dict[str, Any]) -> tuple[Path, str]:
    queue_path = _inside_repo(repo, state["queue_path"], "queue")
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
    descriptor: int | None = None
    try:
        descriptor = os.open(queue_path, flags)
        before = os.fstat(descriptor)
        if (
            not stat.S_ISREG(before.st_mode)
            or before.st_size > MAX_QUEUE_FEEDBACK_BYTES
        ):
            raise SessionError(
                "queue is not a bounded regular file; feedback reveal refused"
            )
        raw_buffer = bytearray()
        while len(raw_buffer) <= MAX_QUEUE_FEEDBACK_BYTES:
            chunk = os.read(
                descriptor,
                min(64 * 1024, MAX_QUEUE_FEEDBACK_BYTES + 1 - len(raw_buffer)),
            )
            if not chunk:
                break
            raw_buffer.extend(chunk)
        after = os.fstat(descriptor)
    except OSError as exc:
        raise SessionError("queue is unavailable; feedback reveal refused") from exc
    finally:
        if descriptor is not None:
            os.close(descriptor)
    raw = bytes(raw_buffer)
    if len(raw) > MAX_QUEUE_FEEDBACK_BYTES:
        raise SessionError("queue exceeds the feedback byte budget")
    stable_fields = ("st_dev", "st_ino", "st_size", "st_mtime_ns")
    if any(getattr(before, field) != getattr(after, field) for field in stable_fields):
        raise SessionError("queue changed while being read; feedback reveal refused")
    try:
        queue_text = raw.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise SessionError("queue cannot be read safely; feedback reveal refused") from exc
    if len(raw) != before.st_size or hashlib.sha256(raw).hexdigest() != state[
        "queue_sha256"
    ]:
        raise SessionError("queue changed after session start; feedback reveal refused")
    return queue_path, queue_text


def _bounded_feedback(value: str, source: str) -> str:
    if len(value.encode("utf-8")) > MAX_REVEALED_FEEDBACK_BYTES:
        raise SessionError(f"feedback exceeds the byte budget: {source}")
    return value


def _v2_feedback(text: str, item_id: str) -> str | None:
    match = re.search(
        rf"(?m)^#{{3,6}}\s+{re.escape(item_id)}(?:\s+[^\n]*)?$", text
    )
    if not match:
        return None
    next_match = ITEM_HEADING_RE.search(text, match.end())
    section = text[match.start() : next_match.start() if next_match else len(text)]
    lines = section.splitlines()
    start = next(
        (index for index, line in enumerate(lines) if re.match(r"^>\s*\[!answer\]", line)),
        None,
    )
    if start is None:
        return None
    feedback: list[str] = []
    for line in lines[start + 1 :]:
        if not line.startswith(">"):
            break
        feedback.append(re.sub(r"^>\s?", "", line))
    rendered = "\n".join(feedback).strip()
    return rendered or None


def _feedback_for_item(repo: Path, state: dict[str, Any], item: dict[str, Any]) -> tuple[str, str]:
    if item["item_kind"] in {"related_original", "grounded_variant"}:
        return FIXED_FEEDBACK_BOUNDARY, f"fixed_boundary:{item['item_kind']}"
    if state["queue_schema"] == "morning_review_queue_v2":
        _, queue_text = _current_queue(repo, state)
        feedback = _v2_feedback(queue_text, item["item_id"])
        if feedback:
            return feedback, f"queue_folded_callout:{item['item_id']}"
        return FIXED_FEEDBACK_BOUNDARY, f"fixed_boundary:{item['item_kind']}"
    if item["item_kind"] in {"formal_due", "baseline_calibration"}:
        table = repo / "复习单元总表.md"
        expected_hash = state.get("review_table_sha256")
        if not table.is_file() or not expected_hash or sha256_file(table) != expected_hash:
            raise SessionError("review-unit table changed after session start; feedback reveal refused")
        feedback = read_review_unit_feedback(table).get(item["source_id"], "").strip()
        if not feedback:
            raise SessionError(f"safe derived feedback is missing for {item['source_id']}")
        return feedback, f"review_unit_table:{item['source_id']}"
    return FIXED_FEEDBACK_BOUNDARY, f"fixed_boundary:{item['item_kind']}"


def _managed_feedback_receipts(
    repo: Path,
    session_dir: Path,
    item: dict[str, Any],
    args: argparse.Namespace,
    *,
    review_receipt_sha256: str | None,
    session_binding_receipt_sha256: str | None,
) -> dict[str, str]:
    """Reverify exact review and session-binding receipts before feedback."""

    try:
        import review_hot_state_408 as review_hot
    except ImportError as exc:
        raise SessionError("managed feedback receipt gate is unavailable") from exc
    first = item.get("first") or {}
    event_id = str(first.get("canonical_event_id") or "")
    if not event_id:
        raise SessionError("managed feedback identity is incomplete")
    review_state_dir = getattr(args, "review_state_dir", None)
    try:
        receipt = review_hot.lookup_commit_receipt(
            repo, event_id=event_id, state_dir=review_state_dir
        )
        if receipt.get("status") != "found" or not receipt.get("receipt_sha256"):
            raise SessionError(
                "managed review event lacks an event-specific receipt"
            )
        review_receipt = str(receipt["receipt_sha256"])
        if review_receipt_sha256 and review_receipt_sha256 != review_receipt:
            raise SessionError(
                "supplied review receipt differs from the current event receipt"
            )
        review_verification = review_hot.verify_commit(
            repo,
            receipt_sha256=review_receipt,
            state_dir=review_state_dir,
        )
    except review_hot.HotStateError as exc:
        raise SessionError("managed review receipt lookup failed closed") from exc
    if (
        review_verification.get("status") != "verified"
        or review_verification.get("event_id") != event_id
        or review_verification.get("receipt_sha256") != review_receipt
    ):
        raise SessionError("managed review receipt binds the wrong event")

    binding_payload = {
        "item_id": item["item_id"],
        "canonical_event_id": event_id,
    }
    binding_key = _session_hot_event_key(
        session_dir.name,
        "first_bound_to_loop",
        binding_payload,
        purpose=f"item:{item['item_id']}:canonical-binding",
    )
    proof = _verify_session_hot_key(session_dir, binding_key)
    if proof.get("status") != "pass" or not proof.get("receipt_sha256"):
        raise SessionError(
            "managed session binding lacks an event-specific commit receipt"
        )
    binding_receipt = str(proof["receipt_sha256"])
    if (
        session_binding_receipt_sha256
        and session_binding_receipt_sha256 != binding_receipt
    ):
        raise SessionError(
            "supplied session binding receipt differs from the current binding"
        )
    binding_verification = _verify_session_hot_event_receipt(
        session_dir,
        binding_receipt,
        event_type="first_bound_to_loop",
        item_id=str(item["item_id"]),
    )
    if (
        binding_verification.get("status") != "pass"
        or binding_verification.get("receipt_sha256") != binding_receipt
    ):
        raise SessionError("managed session binding receipt is not current")
    return {
        "canonical_event_id": event_id,
        "review_receipt_sha256": review_receipt,
        "session_binding_receipt_sha256": binding_receipt,
    }


def _managed_postanswer_feedback(
    repo: Path,
    session_dir: Path,
    item: dict[str, Any],
    args: argparse.Namespace,
    *,
    review_receipt_sha256: str | None,
    session_binding_receipt_sha256: str | None,
) -> tuple[str, str, dict[str, Any]]:
    """Resolve formal RU feedback only after bounded review/session proofs."""

    try:
        import postanswer_feedback_gate_408 as feedback_gate
    except ImportError as exc:
        raise SessionError("managed post-answer feedback gate is unavailable") from exc
    receipts = _managed_feedback_receipts(
        repo,
        session_dir,
        item,
        args,
        review_receipt_sha256=review_receipt_sha256,
        session_binding_receipt_sha256=session_binding_receipt_sha256,
    )
    event_id = receipts["canonical_event_id"]
    review_receipt = receipts["review_receipt_sha256"]
    binding_receipt = receipts["session_binding_receipt_sha256"]
    review_unit_id = str(
        item.get("review_unit_id")
        or (
            item.get("source_id")
            if REVIEW_UNIT_RE.fullmatch(str(item.get("source_id") or ""))
            else ""
        )
    )
    if not REVIEW_UNIT_RE.fullmatch(review_unit_id):
        raise SessionError("managed post-answer feedback identity is incomplete")
    review_state_dir = getattr(args, "review_state_dir", None)
    try:
        gated = feedback_gate.reveal_feedback(
            repo,
            review_unit_id=review_unit_id,
            canonical_event_id=event_id,
            review_receipt_sha256=review_receipt,
            review_state_dir=review_state_dir,
            formal_projection_dir=getattr(args, "formal_projection_dir", None),
            session_dir=session_dir,
            session_binding_receipt_sha256=binding_receipt,
            session_hot_dir=getattr(args, "session_hot_dir", None),
        )
    except feedback_gate.PostanswerFeedbackError as exc:
        raise SessionError(f"managed post-answer feedback failed closed: {exc}") from exc
    if gated.get("status") != "ready":
        raise SessionError("managed post-answer feedback did not become ready")
    return (
        str(gated["feedback"]),
        str(gated["feedback_source"]),
        gated,
    )


def _default_feedback_type(item: dict[str, Any]) -> str:
    first = item.get("first") or {}
    result, _, _ = _normalized_session_first_result(
        str(first.get("result") or ""),
        choice_result=str(first.get("choice_result") or "not_applicable"),
        reasoning_result=str(first.get("reasoning_result") or "not_observed"),
        prompt_level=str(first.get("prompt_level") or ""),
    )
    if result == "independent_correct":
        return "brief_verification"
    if result == "fragile_correct":
        return "minimal_hint"
    if result == "partial":
        return "progressive_hint"
    if result == "wrong" and first.get("confidence") == "high":
        return "misconception_contrast"
    return "corrective_feedback"


def command_reveal_feedback(
    args: argparse.Namespace,
    *,
    review_receipt_sha256: str | None = None,
    session_binding_receipt_sha256: str | None = None,
) -> dict[str, Any]:
    if not bool(getattr(args, "_current_question_managed_v1", False)):
        raise SessionError(
            "legacy_reveal_feedback_retired_use_managed_408_current_turn"
        )
    current_only = True
    repo, session_id = _with_state(args)
    session_dir = _session_dir(repo, session_id)
    with session_lock(session_dir):
        state = _load_state(session_dir, session_id)
        _ensure_active(state)
        item = _get_item(state, args.item)
        if item["first"] is None:
            raise SessionError("feedback reveal is forbidden before record-first")
        if not item["first"].get("canonical_event_id"):
            raise SessionError(
                "feedback reveal is forbidden until the first answer is bound "
                "to the canonical review ledger"
            )
        feedback_gate: dict[str, Any] | None = None
        receipt_only = bool(
            getattr(args, "_current_question_receipt_only", False)
        )
        if receipt_only:
            if not _session_hot_managed(session_dir):
                raise SessionError(
                    "current-question feedback receipt requires managed session hot state"
                )
            receipts = _managed_feedback_receipts(
                repo,
                session_dir,
                item,
                args,
                review_receipt_sha256=review_receipt_sha256,
                session_binding_receipt_sha256=(
                    session_binding_receipt_sha256
                ),
            )
            frozen_feedback_sha256 = str(
                getattr(args, "_current_question_feedback_sha256", "") or ""
            )
            if not SHA256_RE.fullmatch(frozen_feedback_sha256):
                raise SessionError(
                    "current-question frozen feedback binding is invalid"
                )
            feedback = ""
            source = "current_question_frozen_feedback"
            feedback_gate = {
                "status": "receipt_only",
                "frozen_feedback_sha256": frozen_feedback_sha256,
                "receipt_gate": receipts,
            }
        elif state.get("interaction_mode") == "frozen_prepared_pack":
            receipts: dict[str, str] | None = None
            if _session_hot_managed(session_dir):
                receipts = _managed_feedback_receipts(
                    repo,
                    session_dir,
                    item,
                    args,
                    review_receipt_sha256=review_receipt_sha256,
                    session_binding_receipt_sha256=(
                        session_binding_receipt_sha256
                    ),
                )
            try:
                import morning_review_prepared_pack_408 as prepared_pack

                feedback, source, feedback_gate = (
                    prepared_pack.prepared_feedback_for_item(repo, state, item)
                )
            except (ImportError, OSError, RuntimeError) as exc:
                raise SessionError(
                    f"prepared feedback failed closed: {exc}"
                ) from exc
            if receipts is not None:
                feedback_gate = {**feedback_gate, "receipt_gate": receipts}
        elif _session_hot_managed(session_dir) and item["item_kind"] in {
            "formal_due",
            "baseline_calibration",
        }:
            feedback, source, feedback_gate = _managed_postanswer_feedback(
                repo,
                session_dir,
                item,
                args,
                review_receipt_sha256=review_receipt_sha256,
                session_binding_receipt_sha256=(
                    session_binding_receipt_sha256
                ),
            )
        else:
            feedback, source = _feedback_for_item(repo, state, item)
        feedback = _bounded_feedback(feedback, source)
        feedback_type = args.feedback_type or _default_feedback_type(item)
        already_revealed = bool(item["feedback_revealed"])
        if already_revealed:
            feedback_type = str(item["feedback_type"])
        feedback_payload = {
            "item_id": args.item,
            "feedback_source": source,
            "feedback_type": feedback_type,
        }
        feedback_key = _session_hot_event_key(
            session_id,
            "feedback_revealed",
            feedback_payload,
            purpose=f"item:{args.item}:feedback",
        )
        if not already_revealed:
            state = _commit_event(
                session_dir,
                state,
                session_id,
                "feedback_revealed",
                feedback_payload,
                idempotency_key=(
                    feedback_key if _session_hot_managed(session_dir) else None
                ),
            )
            session_commit = _state_hot_commit(state)
        else:
            session_commit = (
                _verify_session_hot_key(session_dir, feedback_key)
                if _session_hot_managed(session_dir)
                else None
            )
            if _session_hot_managed(session_dir) and (
                not isinstance(session_commit, dict)
                or session_commit.get("status") != "pass"
                or not SHA256_RE.fullmatch(
                    str(session_commit.get("receipt_sha256") or "")
                )
                or not isinstance(session_commit.get("verification"), dict)
                or session_commit["verification"].get("status") != "pass"
                or session_commit["verification"].get("receipt_sha256")
                != session_commit.get("receipt_sha256")
            ):
                raise SessionError(
                    "managed feedback replay lacks an event-specific commit receipt"
                )
        if current_only:
            return {
                "action": "reveal-feedback",
                "session_id": session_id,
                "item_id": args.item,
                "feedback_source": source,
                "feedback_type": feedback_type,
                "already_revealed": already_revealed,
                "feedback": feedback,
                "session_commit": session_commit,
                "feedback_gate": feedback_gate,
                "continuation": None,
                "next_item_lookup_count": 0,
                "next_item_published": False,
            }
        next_item_id = next(
            (
                candidate_id
                for candidate_id in state["item_order"]
                if state["items"][candidate_id].get("first") is None
                and state["items"][candidate_id].get("resolution")
                not in NAVIGATION_EXCLUDED_RESOLUTIONS
            ),
            None,
        )
        eligible_repair_id = next(
            (
                candidate_id
                for candidate_id in state["item_order"]
                if state["items"][candidate_id].get("resolution")
                == "repair_pending"
                and isinstance(
                    state["items"][candidate_id].get("repair_eligible_at"), int
                )
                and state["items"][candidate_id]["repair_eligible_at"]
                <= state["progress_clock"]
            ),
            None,
        )
        first_result = str((item.get("first") or {}).get("result") or "")
    continuation: dict[str, Any] | None = None
    if state.get("interaction_mode") == "frozen_prepared_pack":
        if first_result == "independent_correct" and eligible_repair_id:
            try:
                repair_surface = prepared_pack.show_item(
                    repo,
                    session_id,
                    eligible_repair_id,
                    surface_role="repair",
                )
            except (NameError, OSError, RuntimeError) as exc:
                raise SessionError(
                    f"prepared repair question failed closed: {exc}"
                ) from exc
            continuation = {
                "kind": "eligible_spaced_repair",
                "item_id": eligible_repair_id,
                "surface_sha256": repair_surface["surface_sha256"],
                "surface": repair_surface["surface"],
                "next_main_item_id": next_item_id,
            }
        elif first_result == "independent_correct" and next_item_id:
            try:
                next_surface = prepared_pack.show_item(repo, session_id, next_item_id)
            except (NameError, OSError, RuntimeError) as exc:
                raise SessionError(
                    f"next prepared question failed closed: {exc}"
                ) from exc
            continuation = {
                "kind": "next_main_item",
                "item_id": next_item_id,
                "surface_sha256": next_surface["surface_sha256"],
                "surface": next_surface["surface"],
            }
        elif first_result != "independent_correct" and isinstance(
            feedback_gate, dict
        ):
            continuation = {
                "kind": "same_item_guided_check",
                "item_id": args.item,
                "surface_sha256": feedback_gate.get(
                    "guided_surface_sha256"
                ),
                "surface": feedback_gate.get("guided_surface"),
                "later_repair_surface_sha256": feedback_gate.get(
                    "repair_surface_sha256"
                ),
                "next_main_item_id": next_item_id,
            }
    return {
        "action": "reveal-feedback",
        "session_id": session_id,
        "item_id": args.item,
        "feedback_source": source,
        "feedback_type": feedback_type,
        "already_revealed": already_revealed,
        "feedback": feedback,
        "session_commit": session_commit,
        "feedback_gate": feedback_gate,
        "continuation": continuation,
    }


def command_advance(args: argparse.Namespace) -> dict[str, Any]:
    if args.count < 1 or args.count > 1000:
        raise SessionError("advance count must be between 1 and 1000")
    repo, session_id = _with_state(args)
    session_dir = _session_dir(repo, session_id)
    with session_lock(session_dir):
        state = _load_state(session_dir, session_id)
        _ensure_active(state)
        state = _commit_event(
            session_dir, state, session_id, "clock_advanced", {"count": args.count}
        )
    return {"action": "advance", **session_summary(state), "advanced_by": args.count}


def mark_teaching_resolved(
    repo_root: str | Path,
    session_id: str,
    item_id: str,
    *,
    capture_id: str,
    capture_receipt_sha256: str,
    resolution_attestation_sha256: str,
    trace_supplement_sha256: str,
    interaction_trace_sha256: str,
) -> dict[str, Any]:
    """Close same-item teaching without creating mastery or a second outcome."""

    repo = _resolve_repo(repo_root)
    session = _validate_session_id(session_id)
    item_key = str(item_id or "")
    if not ITEM_ID_RE.fullmatch(item_key):
        raise SessionError("teaching resolution item_id is invalid")
    session_dir = _session_dir(repo, session)
    if not _session_hot_managed(session_dir):
        raise SessionError("teaching resolution requires managed session hot-state")
    payload = {
        "item_id": item_key,
        "capture_id": str(capture_id or ""),
        "capture_receipt_sha256": str(capture_receipt_sha256 or ""),
        "resolution_attestation_sha256": str(
            resolution_attestation_sha256 or ""
        ),
        "trace_supplement_sha256": str(trace_supplement_sha256 or ""),
        "interaction_trace_sha256": str(interaction_trace_sha256 or ""),
        "mastery_effect": "none",
        "retention_effect": "none",
        "independent_repair": False,
    }
    key = _session_hot_event_key(
        session,
        "teaching_resolved",
        payload,
        purpose=f"item:{item_key}:teaching-resolution",
    )
    with session_lock(session_dir):
        state = _load_state(session_dir, session)
        _ensure_active(state)
        item = _get_item(state, item_key)
        existing = item.get("teaching_resolution")
        if isinstance(existing, dict):
            if any(
                existing.get(field) != payload.get(field)
                for field in (
                    "capture_id",
                    "capture_receipt_sha256",
                    "resolution_attestation_sha256",
                    "trace_supplement_sha256",
                    "interaction_trace_sha256",
                )
            ):
                raise SessionError("teaching resolution binding drifted")
            session_commit = _verify_session_hot_key(session_dir, key)
        else:
            if item.get("resolution") != "repair_pending":
                raise SessionError(
                    f"item is not awaiting teaching resolution: {item.get('resolution')}"
                )
            state = _commit_event(
                session_dir,
                state,
                session,
                "teaching_resolved",
                payload,
                idempotency_key=key,
            )
            session_commit = _state_hot_commit(state)
        if (
            not isinstance(session_commit, dict)
            or session_commit.get("status") not in {
                "committed",
                "already_committed",
                "pass",
            }
            or not SHA256_RE.fullmatch(
                str(session_commit.get("receipt_sha256") or "")
            )
            or not isinstance(session_commit.get("verification"), dict)
            or session_commit["verification"].get("status") != "pass"
            or session_commit["verification"].get("receipt_sha256")
            != session_commit.get("receipt_sha256")
        ):
            raise SessionError(
                "teaching resolution lacks an event-specific commit receipt"
            )
    return {
        "action": "teaching-resolved",
        "session_id": session,
        "item_id": item_key,
        "resolution": "relearn_required",
        "mastery_effect": "none",
        "retention_effect": "none",
        "independent_repair": False,
        "session_commit": session_commit,
        "formal_write_count": 0,
    }


def command_record_repair(args: argparse.Namespace) -> dict[str, Any]:
    repo, session_id = _with_state(args)
    session_dir = _session_dir(repo, session_id)
    payload: dict[str, Any] = {"item_id": args.item, "result": args.result}
    if args.result != "relearn_required":
        if args.confidence is None or args.prompt_level is None:
            raise SessionError("repair attempt requires confidence and prompt_level")
        if args.result == "independent_correct" and args.prompt_level != "none":
            raise SessionError("independent repair requires prompt_level=none")
        payload.update(
            {
                "confidence": args.confidence,
                "response_seconds": _validate_reaction_time(args.response_seconds),
                "prompt_level": args.prompt_level,
            }
        )
    managed_hot = _session_hot_managed(session_dir)
    created_session_event = False
    session_repair_commit: dict[str, Any] | None = None
    with session_lock(session_dir):
        state = _load_state(session_dir, session_id)
        _ensure_active(state)
        item = _get_item(state, args.item)
        if item["first"] is None:
            raise SessionError("record-first is required before record-repair")
        if not item["feedback_revealed"]:
            raise SessionError("feedback must be revealed before record-repair")
        _validate_prepared_repair_choice_before_write(repo, state, item, args)
        attempts = item.get("repair_attempts") or []
        latest = attempts[-1] if attempts else None
        resume_existing = bool(
            isinstance(latest, dict)
            and _same_repair_input(latest, payload)
            and (
                not latest.get("canonical_event_id")
                or item.get("resolution") != "repair_pending"
            )
        )
        if resume_existing:
            attempt_number = int(latest.get("attempt") or len(attempts))
            if managed_hot:
                session_key = str(latest.get("session_idempotency_key") or "")
                if not session_key:
                    raise SessionError(
                        "managed repair recovery lacks its session idempotency key"
                    )
                session_repair_commit = _verify_session_hot_key(
                    session_dir, session_key
                )
        else:
            if item["resolution"] != "repair_pending":
                raise SessionError(
                    f"item is not awaiting repair: {item['resolution']}"
                )
            eligible_at = int(item["repair_eligible_at"])
            if state["progress_clock"] < eligible_at:
                raise SessionError(
                    "repair spacing not met: "
                    f"clock={state['progress_clock']} eligible_at={eligible_at}"
                )
            attempt_number = len(attempts) + 1
            review_key = f"morning:{session_id}:{args.item}:repair:{attempt_number}"
            commit_payload = {
                **payload,
                "attempt": attempt_number,
                "review_idempotency_key": review_key,
            }
            session_key = _session_hot_event_key(
                session_id,
                "repair_recorded",
                commit_payload,
                purpose=f"item:{args.item}:repair:{attempt_number}",
            )
            state = _commit_event(
                session_dir,
                state,
                session_id,
                "repair_recorded",
                commit_payload,
                idempotency_key=session_key if managed_hot else None,
            )
            session_repair_commit = _state_hot_commit(state)
            created_session_event = True
        staged_state = copy.deepcopy(dict(state))
        staged_item = copy.deepcopy(_get_item(state, args.item))

    review_repair: dict[str, Any] | None = None
    binding_created = False
    session_binding_commit: dict[str, Any] | None = None
    if managed_hot:
        if (
            not isinstance(session_repair_commit, dict)
            or session_repair_commit.get("status")
            not in {"committed", "already_committed", "pass"}
            or not session_repair_commit.get("receipt_sha256")
        ):
            raise SessionError("managed repair lacks its session commit receipt")
        review_repair = _record_hot_morning_repair(
            repo,
            staged_state,
            staged_item,
            args.item,
            attempt=attempt_number,
        )
        canonical_event = review_repair["event"]
        state, binding_created = _bind_repair_to_loop(
            repo,
            session_id,
            args.item,
            attempt_number,
            canonical_event,
        )
        binding_payload = {
            "item_id": args.item,
            "attempt": attempt_number,
            "canonical_event_id": str(canonical_event["event_id"]),
        }
        binding_key = _session_hot_event_key(
            session_id,
            "repair_bound_to_loop",
            binding_payload,
            purpose=f"item:{args.item}:repair:{attempt_number}:binding",
        )
        session_binding_commit = _state_hot_commit(state)
        if session_binding_commit is None:
            session_binding_commit = _verify_session_hot_key(
                session_dir, binding_key
            )
        if (
            not isinstance(session_binding_commit, dict)
            or session_binding_commit.get("status")
            not in {"committed", "already_committed", "pass"}
            or not session_binding_commit.get("receipt_sha256")
        ):
            raise SessionError("managed repair lacks its binding commit receipt")
    else:
        sync_session(repo, session_id, item_id=args.item)
    return {
        "action": "record-repair",
        **session_summary(state),
        "item": _item_public_view(state["items"][args.item]),
        "attempt": attempt_number,
        "created_session_event": created_session_event,
        "record_replayed": not created_session_event,
        "managed_hot_path": managed_hot,
        "binding_created": binding_created,
        "review_event_id": (
            review_repair["event"]["event_id"]
            if review_repair is not None
            else None
        ),
        "review_commit_receipt_ref": (
            review_repair.get("commit_receipt_ref")
            if review_repair is not None
            else None
        ),
        "review_commit_receipt_sha256": (
            review_repair.get("commit_receipt_sha256")
            if review_repair is not None
            else None
        ),
        "review_verification": (
            review_repair.get("verification")
            if review_repair is not None
            else None
        ),
        "session_repair_commit": session_repair_commit,
        "session_binding_commit": session_binding_commit,
    }


def command_pause(args: argparse.Namespace) -> dict[str, Any]:
    repo, session_id = _with_state(args)
    session_dir = _session_dir(repo, session_id)
    with session_lock(session_dir):
        state = _load_state(session_dir, session_id)
        _ensure_active(state)
        state = _commit_event(
            session_dir,
            state,
            session_id,
            "session_paused",
            {"reason": args.reason},
        )
    reconcile_runtime_views(repo)
    return {"action": "pause", **session_summary(state), "pause_reason": args.reason}


def command_resume(args: argparse.Namespace) -> dict[str, Any]:
    repo, session_id = _with_state(args)
    session_dir = _session_dir(repo, session_id)
    with session_lock(session_dir):
        state = _load_state(session_dir, session_id)
        if state["status"] != "paused":
            raise SessionError(f"session must be paused, got {state['status']}")
        state = _commit_event(session_dir, state, session_id, "session_resumed", {})
    reconcile_runtime_views(repo)
    return {"action": "resume", **session_summary(state)}


def command_cancel(args: argparse.Namespace) -> dict[str, Any]:
    """Cancel an untouched session without consuming its canonical due actions."""

    repo, session_id = _with_state(args)
    session_dir = _session_dir(repo, session_id)
    with session_lock(session_dir):
        state = _load_state(session_dir, session_id)
        if state["status"] == "cancelled":
            if state.get("cancel_reason") != args.reason:
                raise SessionError(
                    "cancel replay reason conflicts with the recorded cancellation"
                )
            cancel_summary = state.get("cancel_summary") or {}
            replayed = True
        else:
            replayed = False
        if state["status"] not in {"in_progress", "paused"}:
            if not replayed:
                raise SessionError(
                    f"session must be active to cancel, got {state['status']}"
                )
        if not replayed:
            touched = [
                item_id
                for item_id in state["item_order"]
                if state["items"][item_id]["first"] is not None
                or state["items"][item_id]["feedback_revealed"]
                or state["items"][item_id]["repair_attempts"]
                or state["items"][item_id]["resolution"] != "pending"
            ]
            if touched:
                raise SessionError(
                    "cancel refused because learning evidence already exists for "
                    + ", ".join(touched[:20])
                )
            loop_state = load_review_loop_state(repo)
            canonical_events = [
                event
                for event in loop_state["events"]
                if event.get("session_id") == session_id
            ]
            if canonical_events:
                raise SessionError(
                    "cancel refused because the canonical review ledger already contains "
                    f"{len(canonical_events)} event(s) for this session"
                )
            selected_action_ids = sorted(
                {
                    str(item["action_id"])
                    for item in state["items"].values()
                    if item.get("action_id")
                }
            )
            pending_by_id = {
                str(action["action_id"]): action
                for action in loop_state["pending_actions"]
            }
            unavailable = [
                action_id
                for action_id in selected_action_ids
                if action_id not in pending_by_id
            ]
            if unavailable:
                raise SessionError(
                    "cancel refused because selected canonical actions are no longer pending: "
                    + ", ".join(unavailable[:20])
                )
            cancel_summary = {
                "selected_count": len(state["item_order"]),
                "first_answered_count": 0,
                "preserved_action_ids": selected_action_ids,
                "pending_actions_preserved": True,
                "learning_evidence_written": False,
            }
            state = _commit_event(
                session_dir,
                state,
                session_id,
                "session_cancelled",
                {"reason": args.reason, "summary": cancel_summary},
            )
    reconcile_runtime_views(repo)
    preserved_action_ids = list(cancel_summary.get("preserved_action_ids") or [])
    current_pending = {
        str(action["action_id"])
        for action in load_review_loop_state(repo)["pending_actions"]
    }
    lost = [action_id for action_id in preserved_action_ids if action_id not in current_pending]
    if lost:
        raise SessionError(
            "cancelled session no longer has all preserved actions pending: "
            + ", ".join(lost[:20])
        )
    return {
        "action": "cancel",
        **session_summary(state),
        "replayed": replayed,
        "preserved_action_ids": preserved_action_ids,
        "pending_actions_preserved": True,
        "learning_evidence_written": False,
    }


def _verified_closed_equivalent_retirement_for_close(
    loop_state: dict[str, Any],
    *,
    session_id: str,
    item_id: str,
    item: dict[str, Any],
    event: dict[str, Any],
) -> dict[str, Any]:
    if (
        event.get("event_kind")
        != review_loop.CLOSED_EQUIVALENT_ITEM_RETIREMENT_KIND
        or event.get("session_id") != session_id
        or event.get("item_id") != item_id
        or event.get("source_id") != item.get("source_id")
        or event.get("trigger_action_id") != item.get("action_id")
        or event.get("disposition") != "administratively_retired"
        or event.get("resolution_reason") != "user_removed"
        or event.get("explicit_user_confirmation") is not True
        or event.get("answer_free_terminal") is not True
        or event.get("processing_contract")
        != review_loop.CLOSED_EQUIVALENT_ITEM_RETIREMENT_CONTRACT
        or event.get("learner_evidence_write_count") != 0
        or event.get("formal_write_authorized") is not False
        or event.get("formal_write_count") != 0
        or event.get("generated_actions") != []
        or item.get("first") is not None
        or item.get("resolution")
        not in {"pending", "administratively_retired"}
    ):
        raise SessionError(
            f"canonical close refused: administrative retirement shape conflict for {item_id}"
        )
    action_id = str(item.get("action_id") or "")
    action = next(
        (
            row
            for row in loop_state.get("actions") or []
            if row.get("action_id") == action_id
        ),
        None,
    )
    equivalent_to = str(event.get("equivalent_to_action_id") or "")
    if (
        not isinstance(action, dict)
        or action.get("status") != "closed_equivalent"
        or action.get("source_id") != item.get("source_id")
        or action.get("equivalent_to_action_id") != equivalent_to
        or not any(
            row.get("action_id") == equivalent_to
            for row in loop_state.get("actions") or []
        )
        or event.get("formal_binding_event_id")
        not in (action.get("equivalence_disposition_event_ids") or [])
        or event.get("closed_equivalent_action_sha256")
        != hashlib.sha256(
            _canonical_json(action).encode("utf-8")
        ).hexdigest()
    ):
        raise SessionError(
            f"canonical close refused: closed-equivalent action conflict for {item_id}"
        )
    binding_id = str(event.get("formal_binding_event_id") or "")
    bindings = [
        row
        for row in loop_state.get("formal_review_bindings") or []
        if row.get("event_id") == binding_id
    ]
    expected_equivalence = {
        "action_id": action_id,
        "authoritative_action_id": equivalent_to,
        "disposition": "closed_equivalent_d1_family",
    }
    if len(bindings) != 1:
        raise SessionError(
            f"canonical close refused: formal binding missing for {item_id}"
        )
    binding = bindings[0]
    if (
        binding.get("event_kind") != review_loop.FORMAL_REVIEW_BINDING_KIND
        or binding.get("formal_write_authorized") is not False
        or expected_equivalence
        not in (binding.get("equivalent_action_dispositions") or [])
        or event.get("formal_binding_event_sha256")
        != hashlib.sha256(
            _canonical_json(binding).encode("utf-8")
        ).hexdigest()
    ):
        raise SessionError(
            f"canonical close refused: formal equivalence proof conflict for {item_id}"
        )
    disposition = {
        "status": "administratively_retired",
        "canonical_retirement_event_id": str(event["event_id"]),
        "canonical_retirement_event_sha256": hashlib.sha256(
            _canonical_json(event).encode("utf-8")
        ).hexdigest(),
        "canonical_retirement_event_time": str(event["event_time"]),
        "formal_binding_event_id": binding_id,
        "formal_binding_event_sha256": str(
            event["formal_binding_event_sha256"]
        ),
        "action_id": action_id,
        "closed_equivalent_action_sha256": str(
            event["closed_equivalent_action_sha256"]
        ),
        "equivalent_to_action_id": equivalent_to,
        "resolution_reason": "user_removed",
        "processing_contract": (
            review_loop.CLOSED_EQUIVALENT_ITEM_RETIREMENT_CONTRACT
        ),
        "learner_evidence_write_count": 0,
        "formal_write_count": 0,
    }
    if (
        item.get("resolution") == "administratively_retired"
        and item.get("administrative_disposition") != disposition
    ):
        raise SessionError(
            f"canonical close refused: projected administrative retirement drifted for {item_id}"
        )
    return disposition


def command_close(args: argparse.Namespace) -> dict[str, Any]:
    repo, session_id = _with_state(args)
    session_dir = _session_dir(repo, session_id)
    with session_lock(session_dir):
        state = _load_state(session_dir, session_id)
        if state["status"] not in {"in_progress", "paused"}:
            raise SessionError(
                f"session must be active to close, got {state['status']}"
            )
        if args.reconcile_from_loop:
            loop_state = load_review_loop_state(repo)
            loop_events = list(loop_state["events"])
            session_events = [
                event
                for event in loop_events
                if event.get("session_id") == session_id
            ]
            outcomes = {
                str(event.get("item_id")): event
                for event in session_events
                if event.get("event_kind") == "outcome"
            }
            dispositions = {
                str(event.get("item_id")): event
                for event in session_events
                if event.get("event_kind") == "item_disposition"
                and event.get("disposition") == "dismissed"
            }
            retirement_rows = [
                event
                for event in session_events
                if event.get("event_kind")
                == review_loop.CLOSED_EQUIVALENT_ITEM_RETIREMENT_KIND
            ]
            retirement_groups: dict[str, list[dict[str, Any]]] = {}
            for event in retirement_rows:
                retirement_groups.setdefault(
                    str(event.get("item_id") or ""), []
                ).append(event)
            duplicate_retirements = [
                item_id
                for item_id, rows in retirement_groups.items()
                if len(rows) != 1
            ]
            if duplicate_retirements:
                raise SessionError(
                    "canonical close refused: duplicate administrative retirements for "
                    + ", ".join(duplicate_retirements[:20])
                )
            administrative_retirements: dict[str, dict[str, Any]] = {}
            for item_id, rows in retirement_groups.items():
                item = state["items"].get(item_id)
                if not isinstance(item, dict):
                    raise SessionError(
                        "canonical close refused: administrative retirement references "
                        f"unknown item {item_id}"
                    )
                administrative_retirements[item_id] = (
                    _verified_closed_equivalent_retirement_for_close(
                        loop_state,
                        session_id=session_id,
                        item_id=item_id,
                        item=item,
                        event=rows[0],
                    )
                )
            canonical_close_events = [
                *loop_events,
                *list(loop_state.get("scheduling_reconciliations") or []),
            ]
            events_by_id = {
                str(event.get("event_id")): event
                for event in canonical_close_events
                if event.get("event_id")
            }
            scheduling_superseded: dict[str, dict[str, Any]] = {}
            for item_id in state["item_order"]:
                item = state["items"][item_id]
                if item.get("resolution") != "scheduling_superseded":
                    continue
                disposition = item.get("scheduling_disposition")
                if not isinstance(disposition, dict):
                    raise SessionError(
                        f"canonical close refused: scheduling disposition missing for {item_id}"
                    )
                reconciliation = events_by_id.get(
                    str(disposition.get("reconciliation_event_id") or "")
                )
                authoritative = events_by_id.get(
                    str(disposition.get("authoritative_outcome_event_id") or "")
                )
                if (
                    disposition.get("status") != "superseded"
                    or disposition.get("formal_write_count") != 0
                    or disposition.get("action_id") != item.get("action_id")
                    or not isinstance(reconciliation, dict)
                    or reconciliation.get("event_kind") != "scheduling_reconciliation"
                    or reconciliation.get("trigger_action_id")
                    != disposition.get("action_id")
                    or reconciliation.get("action_sha256")
                    != disposition.get("action_sha256")
                    or reconciliation.get("authoritative_outcome_event_id")
                    != disposition.get("authoritative_outcome_event_id")
                    or reconciliation.get("processing_contract")
                    != disposition.get("processing_contract")
                    or reconciliation.get("formal_write_count") != 0
                    or reconciliation.get("source_id") != item.get("source_id")
                    or not isinstance(authoritative, dict)
                    or authoritative.get("event_kind") != "outcome"
                    or authoritative.get("source_id") != item.get("source_id")
                ):
                    raise SessionError(
                        f"canonical close refused: scheduling supersession binding conflict for {item_id}"
                    )
                scheduling_superseded[item_id] = copy.deepcopy(disposition)
            missing = [
                item_id
                for item_id in state["item_order"]
                if item_id not in outcomes
                and item_id not in dispositions
                and item_id not in scheduling_superseded
                and item_id not in administrative_retirements
            ]
            if missing:
                raise SessionError(
                    "canonical close refused: unified ledger lacks outcomes for "
                    + ", ".join(missing[:20])
                )
            reconciled_items = []
            for item_id in state["item_order"]:
                item = state["items"][item_id]
                if item_id in scheduling_superseded:
                    reconciled_items.append(
                        {
                            "item_id": item_id,
                            "scheduling_superseded": True,
                            "scheduling_disposition": scheduling_superseded[item_id],
                        }
                    )
                    continue
                if item_id in administrative_retirements:
                    reconciled_items.append(
                        {
                            "item_id": item_id,
                            "administratively_retired": True,
                            "administrative_disposition": (
                                administrative_retirements[item_id]
                            ),
                        }
                    )
                    continue
                if item_id in dispositions:
                    disposition = dispositions[item_id]
                    if disposition.get("source_id") != item.get("source_id"):
                        raise SessionError(
                            f"canonical close refused: disposition source mismatch for {item_id}"
                        )
                    reconciled_items.append({"item_id": item_id, "dismissed": True})
                    continue
                outcome = outcomes[item_id]
                if outcome.get("source_id") != item.get("source_id"):
                    raise SessionError(
                        f"canonical close refused: source mismatch for {item_id}"
                    )
                repairs = sorted(
                    (
                        event
                        for event in session_events
                        if event.get("event_kind") == "repair"
                        and event.get("item_id") == item_id
                    ),
                    key=lambda event: int(event.get("attempt", 0)),
                )
                first_result = str(outcome.get("first_result"))
                legacy_first_result = "blank" if first_result == "uncertain" else first_result
                if legacy_first_result not in FIRST_RESULTS:
                    raise SessionError(
                        f"canonical close refused: invalid first result for {item_id}"
                    )
                repair_rows = []
                for repair in repairs:
                    correction = str(repair.get("correction_result"))
                    prompt_level = str(repair.get("prompt_level"))
                    if correction == "independent_correct" and prompt_level != "none":
                        legacy_correction = "fragile_correct"
                    elif correction == "uncertain":
                        legacy_correction = "blank"
                    else:
                        legacy_correction = correction
                    if legacy_correction not in REPAIR_RESULTS:
                        raise SessionError(
                            f"canonical close refused: invalid repair result for {item_id}"
                        )
                    repair_rows.append(
                        {
                            "result": legacy_correction,
                            "confidence": repair.get("confidence"),
                            "response_seconds": repair.get("response_seconds"),
                            "prompt_level": repair.get("prompt_level"),
                            "recorded_at": repair.get("event_time"),
                            "explicit": legacy_correction == "relearn_required",
                        }
                    )
                latest = repairs[-1] if repairs else None
                if first_result == "independent_correct" and not repairs:
                    resolution = "independent_correct"
                elif (
                    latest is not None
                    and latest.get("correction_result") == "independent_correct"
                    and latest.get("prompt_level") == "none"
                ):
                    resolution = "repaired_independent"
                else:
                    resolution = "relearn_required"
                reconciled_first = {
                    "result": legacy_first_result,
                    "choice_result": outcome.get("choice_result")
                    or "not_applicable",
                    "reasoning_result": outcome.get("reasoning_result")
                    or "not_observed",
                    "confidence": outcome.get("confidence"),
                    "response_seconds": outcome.get("response_seconds") or 0.0,
                    "prompt_level": outcome.get("prompt_level"),
                    "first_break": outcome.get("first_break"),
                    "recorded_at": outcome.get("event_time"),
                }
                reconciled_items.append(
                    {
                        "item_id": item_id,
                        "first": reconciled_first,
                        "feedback_type": item.get("feedback_type")
                        or _default_feedback_type({"first": reconciled_first}),
                        "repair_attempts": repair_rows,
                        "resolution": resolution,
                    }
                )
            state = _commit_event(
                session_dir,
                state,
                session_id,
                "session_reconciled_from_loop",
                {"items": reconciled_items},
            )
        summary = session_summary(state)
        if summary["unanswered_count"]:
            raise SessionError(
                f"close refused: {summary['unanswered_count']} selected items lack record-first"
            )
        unresolved = [
            item_id
            for item_id in state["item_order"]
            if state["items"][item_id]["resolution"] not in COMPLETE_RESOLUTIONS
        ]
        if unresolved:
            raise SessionError(
                "close refused: repair is unresolved for " + ", ".join(unresolved[:20])
            )
        feedback_missing = [
            item_id
            for item_id in state["item_order"]
            if state["items"][item_id]["resolution"]
            not in NAVIGATION_EXCLUDED_RESOLUTIONS
            and not state["items"][item_id]["feedback_revealed"]
        ]
        if feedback_missing:
            raise SessionError(
                "close refused: feedback was not revealed after record-first for "
                + ", ".join(feedback_missing[:20])
            )
        close_summary = {
            "selected_count": summary["selected_count"],
            "first_answered_count": summary["first_answered_count"],
            "independently_complete_count": summary["independently_complete_count"],
            "relearn_required_count": summary["relearn_required_count"],
            "queue_coverage": summary["queue_coverage"],
            "criterion_coverage": summary["criterion_coverage"],
            "skipped_count": (
                summary.get("dismissed_count", 0)
                + summary.get("administratively_retired_count", 0)
            ),
            "administratively_retired_count": summary.get(
                "administratively_retired_count", 0
            ),
            "answer_free_terminal_count": (
                summary.get("dismissed_count", 0)
                + summary.get("scheduling_superseded_count", 0)
                + summary.get("administratively_retired_count", 0)
            ),
            "scheduling_superseded_count": summary.get(
                "scheduling_superseded_count", 0
            ),
            "closure_rule": "all_items_answered_or_answer_free_terminal",
        }
        state = _commit_event(
            session_dir,
            state,
            session_id,
            "session_closed",
            {"summary": close_summary},
        )
    if not args.reconcile_from_loop:
        sync_session(repo, session_id)
    reconcile_runtime_views(repo)
    return {"action": "close", **session_summary(state), "closed_summary": close_summary}


def auto_close_completed_session(repo: str | Path, session_id: str) -> dict[str, Any]:
    """Close one completed session from canonical evidence without replaying outcomes.

    The canonical review ledger is authoritative for managed sessions. Rebuilding the
    session projection from that ledger before closing avoids reusing fixed outcome
    idempotency keys after later evidence enrichment. Repeated calls never append a
    second close event.
    """

    root = _resolve_repo(repo)
    normalized_session = _validate_session_id(session_id)
    session_dir = _session_dir(root, normalized_session)
    with session_lock(session_dir):
        state = _load_state(session_dir, normalized_session)
        if state.get("status") == "closed":
            existing = {
                "action": "auto-close",
                "status": "already_closed",
                "session_id": normalized_session,
                "closed_summary": state.get("closed_summary"),
                "formal_write_count": 0,
            }
        else:
            if state.get("status") not in {"in_progress", "paused"}:
                raise SessionError(
                    "morning_session_closeout_state_conflict:"
                    f"expected active session, got {state.get('status')}"
                )
            summary = session_summary(state)
            local_complete = (
                summary["unanswered_count"] == 0
                and all(
                    state["items"][item_id]["resolution"] in COMPLETE_RESOLUTIONS
                    for item_id in state["item_order"]
                )
            )
            existing = {
                "action": "auto-close",
                "status": "needs_canonical_closeout",
                "session_id": normalized_session,
                "local_complete": local_complete,
                "formal_write_count": 0,
            }
    if existing["status"] == "already_closed":
        reconcile_runtime_views(root)
        return existing
    try:
        closed = command_close(
            argparse.Namespace(
                repo=str(root),
                session=normalized_session,
                reconcile_from_loop=True,
            )
        )
    except SessionError as exc:
        message = str(exc)
        if "unified ledger lacks outcomes" in message and not existing["local_complete"]:
            return {
                **existing,
                "status": "items_still_incomplete",
                "error_code": "morning_session_items_incomplete",
            }
        raise SessionError(
            "morning_session_closeout_evidence_conflict:" + message
        ) from exc
    return {
        "action": "auto-close",
        "status": "closed",
        "session_id": normalized_session,
        "closed_summary": closed.get("closed_summary"),
        "formal_write_count": 0,
    }


def _add_repo(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--repo", default=str(ROOT))


def _add_session(parser: argparse.ArgumentParser) -> None:
    _add_repo(parser)
    parser.add_argument("--session", required=True)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)

    start = subparsers.add_parser("start", help="Start a derived session from a v2/v3 queue")
    _add_repo(start)
    start.add_argument("--queue", required=True)
    start.add_argument("--session-id")
    start.add_argument("--item", action="append", help="Select one queue Item; repeat as needed")
    start.add_argument(
        "--prepared-manifest",
        help=(
            "Bind a fully verified frozen delivery pack before the first learner answer"
        ),
    )
    start.set_defaults(handler=command_start)

    status = subparsers.add_parser("status", help="Read the safe session projection")
    _add_session(status)
    status.add_argument("--item")
    status.set_defaults(handler=command_status)

    repair_unbound = subparsers.add_parser(
        "repair-unbound-first",
        help="Safely repair and resume one session-first/canonical-missing half commit",
    )
    _add_session(repair_unbound)
    repair_unbound.add_argument("--item", required=True)
    repair_unbound.add_argument("--first-break", required=True)
    repair_unbound.add_argument(
        "--first-break-provenance",
        choices=sorted(FIRST_BREAK_PROVENANCES),
        default="derived_classification",
    )
    repair_unbound.set_defaults(handler=command_repair_unbound_first)

    recover = subparsers.add_parser(
        "reconcile-item-from-loop",
        help="Project one existing canonical outcome into a stale active session item",
    )
    _add_session(recover)
    recover.add_argument("--item", required=True)
    recover.add_argument("--event-id", required=True)
    recover.set_defaults(handler=command_reconcile_item_from_loop)

    repair = subparsers.add_parser("record-repair", help="Record a spaced repair attempt")
    _add_session(repair)
    repair.add_argument("--item", required=True)
    repair.add_argument("--result", choices=sorted(REPAIR_RESULTS), required=True)
    repair.add_argument(
        "--learner-choice",
        choices=sorted(PREPARED_LEARNER_CHOICES),
        help=(
            "Ephemeral A/B/C/D repair choice for a frozen prepared session, "
            "or BLANK; never persisted"
        ),
    )
    repair.add_argument(
        "--display-surface-sha256",
        help="Ephemeral commitment returned by the displayed repair surface",
    )
    repair.add_argument("--confidence", choices=sorted(CONFIDENCE_LEVELS))
    repair.add_argument(
        "--reaction-time",
        "--response-seconds",
        dest="response_seconds",
        type=float,
        metavar="SECONDS",
    )
    repair.add_argument("--prompt-level", choices=sorted(PROMPT_LEVELS))
    repair.set_defaults(handler=command_record_repair)

    advance = subparsers.add_parser(
        "advance", help="Advance the separation clock (normally first answers do this)"
    )
    _add_session(advance)
    advance.add_argument("--count", type=int, default=1)
    advance.set_defaults(handler=command_advance)

    pause = subparsers.add_parser("pause", help="Pause without closing the session")
    _add_session(pause)
    pause.add_argument("--reason", choices=sorted(PAUSE_REASONS), default="user_pause")
    pause.set_defaults(handler=command_pause)

    resume = subparsers.add_parser("resume", help="Resume a paused session")
    _add_session(resume)
    resume.set_defaults(handler=command_resume)

    cancel = subparsers.add_parser(
        "cancel",
        help="Cancel an untouched session while preserving its pending due actions",
    )
    _add_session(cancel)
    cancel.add_argument("--reason", choices=sorted(CANCEL_REASONS), required=True)
    cancel.set_defaults(handler=command_cancel)

    close = subparsers.add_parser("close", help="Close only when every selected item is resolved")
    _add_session(close)
    close.add_argument(
        "--reconcile-from-loop",
        action="store_true",
        help="Repair a stale session projection from the canonical review ledger before closing",
    )
    close.set_defaults(handler=command_close)
    return parser


def main(argv: list[str]) -> int:
    args = build_parser().parse_args(argv)
    try:
        output = args.handler(args)
    except (CaptureError, OSError, ReviewLoopError, SessionError, ValueError) as exc:
        print(json.dumps({"status": "ERROR", "error": str(exc)}, ensure_ascii=False), file=sys.stderr)
        return 2
    _json_output(output)
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
