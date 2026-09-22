#!/usr/bin/env python3
"""Managed begin/finalize receipts for every substantive 408 reply.

The begin phase calls the bounded pre-answer lookup exactly once and persists a
content-addressed receipt.  The finalize phase stores only the final reply hash,
the evidence IDs actually adopted, and controlled action labels; it never stores
the reply body.  These receipts prove the behavior of managed repository entry
points.  They cannot intercept an arbitrary platform response that bypasses the
managed wrapper.
"""

from __future__ import annotations

import argparse
import fcntl
import hashlib
import json
import os
import re
import stat
import sys
import tempfile
from contextlib import contextmanager
from datetime import date
from pathlib import Path
from typing import Any, Iterator

from personalization_preanswer_408 import (
    DEFAULT_MAX_BYTES,
    DEFAULT_STATE_RELATIVE,
    ENVELOPE_SCHEMA,
    SCHEMA as PREANSWER_SCHEMA,
    safe_preanswer_slice,
)


BEGIN_SCHEMA = "personalization_turn_begin_receipt_v1"
BEGIN_SCHEMA_V2 = "personalization_turn_begin_receipt_v2"
FINALIZE_SCHEMA = "personalization_reply_binding_v1"
ADOPTION_SCHEMA = "personalization_response_adoption_v1"
AUDIT_SCHEMA = "personalization_turn_receipt_audit_v1"
INSPECTION_SCHEMA = "personalization_turn_receipt_inspection_v2"
PUBLIC_BEGIN_SCHEMA = "personalization_turn_begin_result_v2"
LEGACY_SEAL_SCHEMA = "personalization_legacy_turn_seal_v1"
ABORTED_BEGIN_SEAL_SCHEMA = "personalization_invalid_begin_abort_seal_v1"
CURRENT_CONTRACT_ID = "personalization_turn_begin_receipt_v1_current"
CURRENT_V2_CONTRACT_ID = "personalization_turn_begin_receipt_v2_current"
LEGACY_CONTRACT_ID = "personalization_turn_begin_receipt_v1_pre_slice_snapshot"
PRE_EXACT_TARGET_LEGACY_CONTRACT_ID = (
    "personalization_turn_begin_receipt_v1_pre_exact_target_binding"
)
LEGACY_SEAL_NAMESPACE = "legacy-seal"
ABORTED_BEGIN_SEAL_NAMESPACE = "aborted-begin-seal"
SHA_RE = re.compile(r"^[0-9a-f]{64}$")
SAFE_ID_RE = re.compile(r"^[A-Za-z0-9_.:@/-]{1,180}$")
POINTER_KEY_RE = re.compile(r"^[A-Za-z0-9_.:@-]{1,180}$")
LOCAL_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")

LEGACY_FINALIZE_KEYS = {
    "schema",
    "kind",
    "turn_id",
    "turn_receipt_sha256",
    "request_sha256",
    "slice_sha256",
    "response_plan_sha256",
    "reply_sha256",
    "reply_body_stored",
    "adoption",
    "finalized",
    "display_authorized",
    "answer_safe_metadata_only",
    "formal_write_count",
    "binding_id",
}

ENTRY_TO_CONVERSATION = {
    "morning_selection": "morning_review",
    "morning_answer_feedback": "morning_review",
    "current_question": "current_question",
    "past_exam_review": "past_exam_review",
    "chapter_preview": "chapter_preview",
    "knowledge_explanation": "knowledge_explanation",
    "evening_d0_review": "evening_review",
    "wrong_intake": "wrong_intake",
    "nightly_curation": "wrong_intake",
    "general_408_planning": "general_408",
}

ALLOWED_ACTIONS = {
    "direct_answer",
    "minimum_framework",
    "boundary_check",
    "historical_connection",
    "cross_subject_connection",
    "active_recall",
    "original_question",
    "related_question",
    "verified_variant",
    "mechanism_check",
    "degraded_generic_answer",
    "fast_capture_status",
    "nightly_curation_status",
}

ROOT_MANIFEST_BINDING_FIELDS = (
    "root_manifest_sha256",
    "projection_build_id",
    "recent_incremental_sha256",
    "historical_manifest_sha256",
    "historical_index_sha256",
)

LEGACY_BEGIN_KEYS = {
    "schema",
    "kind",
    "entry_kind",
    "conversation_kind",
    "request_id",
    "request_sha256",
    "local_date",
    "targets",
    *ROOT_MANIFEST_BINDING_FIELDS,
    "slice_id",
    "slice_sha256",
    "retrieval_status",
    "degraded_reason",
    "response_plan",
    "response_plan_sha256",
    "preanswer_call_count",
    "answer_safe",
    "formal_write_count",
    "turn_id",
}

CURRENT_BEGIN_KEYS = LEGACY_BEGIN_KEYS | {"slice_snapshot"}

CURRENT_V2_BEGIN_KEYS = LEGACY_BEGIN_KEYS - {"response_plan"} | {
    "slice_object_ref",
    "slice_object_sha256",
}

RUNTIME_PLAN_FIELDS = (
    "current_target",
    "direct_recent_errors",
    "direct_historical_errors",
    "common_patterns",
    "verified_cross_connections",
    "explanation_strategy",
    "first_boundary_to_check",
    "optional_practice_action",
    "evidence_gaps",
)

PUBLIC_BEGIN_MAX_BYTES = 2_048

READY_SLICE_KEYS = {
    "schema",
    "status",
    "slice_id",
    "request_id",
    "request_sha256",
    "conversation_kind",
    "as_of",
    "projection_build_id",
    "layer_hashes",
    "mode",
    "read_only",
    "formal_write_count",
    "scheduling_authority",
    "truncated",
    "byte_count",
    "context",
}

DEGRADED_SLICE_KEYS = {
    "schema",
    "status",
    "request_id",
    "reason_code",
    "as_of",
    "read_only",
    "formal_write_count",
    "scheduling_authority",
}

INDEPENDENT_SELECTION_ACTIONS = {
    "original_question",
    "related_question",
    "verified_variant",
}

ACTIVE_RECALL_ENTRY_KINDS = {
    "morning_selection",
    "morning_answer_feedback",
    "current_question",
    "past_exam_review",
    "chapter_preview",
    "knowledge_explanation",
    "evening_d0_review",
    "general_408_planning",
}


class TurnReceiptError(RuntimeError):
    """Stable managed-turn contract failure."""


def _retired_write_surface() -> None:
    raise TurnReceiptError(
        "personalization_turn_receipt_writes_retired_read_only_compatibility"
    )


def _is_valid_local_date(value: Any) -> bool:
    if not isinstance(value, str) or not LOCAL_DATE_RE.fullmatch(value):
        return False
    try:
        parsed = date.fromisoformat(value)
    except ValueError:
        return False
    return parsed.isoformat() == value


def _json_bytes(value: Any, *, pretty: bool = False) -> bytes:
    kwargs: dict[str, Any] = {"ensure_ascii": False, "sort_keys": True}
    if pretty:
        kwargs["indent"] = 2
    else:
        kwargs["separators"] = (",", ":")
    return (json.dumps(value, **kwargs) + "\n").encode("utf-8")


def _sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _sha256_file(path: Path) -> str:
    return _sha256_bytes(path.read_bytes())


def _read_json(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise TurnReceiptError("receipt_object_unreadable") from exc
    if not isinstance(value, dict):
        raise TurnReceiptError("receipt_object_invalid")
    return value


def _is_relative_to(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
    except ValueError:
        return False
    return True


def _reject_symlink_components(path: Path, *, anchor: Path) -> None:
    """Reject every existing managed component below a trusted lexical anchor."""

    try:
        relative = path.relative_to(anchor)
    except ValueError as exc:
        raise TurnReceiptError("managed_state_path_escape") from exc
    current = anchor
    for component in relative.parts:
        current = current / component
        try:
            mode = current.lstat().st_mode
        except FileNotFoundError:
            continue
        if stat.S_ISLNK(mode):
            raise TurnReceiptError("managed_state_symlink_rejected")


def _repo_lexical_anchor(path: Path, *, repo: Path) -> Path | None:
    """Find the caller's lexical spelling of repo, tolerating system aliases."""

    for candidate in (path, *path.parents):
        try:
            if candidate.resolve() == repo:
                return candidate
        except OSError:
            continue
    return None


def _reject_external_state_symlinks(path: Path, *, repo: Path) -> None:
    """Reject external lexical symlinks except an OS alias shared by repo."""

    current = Path(path.anchor)
    for component in path.parts[1:]:
        current = current / component
        try:
            mode = current.lstat().st_mode
        except FileNotFoundError:
            continue
        if not stat.S_ISLNK(mode):
            continue
        resolved_component = current.resolve()
        if not _is_relative_to(repo, resolved_component):
            raise TurnReceiptError("managed_state_symlink_rejected")


def _assert_managed_path(path: Path, *, state: Path) -> None:
    """Fail closed before a managed lock, write, read traversal, or file open."""

    allowed = state.resolve()
    resolved = path.resolve(strict=False)
    if not _is_relative_to(resolved, allowed):
        raise TurnReceiptError("managed_state_path_escape")
    _reject_symlink_components(path, anchor=state)


def _atomic_write(path: Path, value: Any, *, state: Path) -> str:
    _retired_write_surface()

    # Historical implementation retained below for read-only source compatibility.
    data = _json_bytes(value, pretty=True)
    _assert_managed_path(path.parent, state=state)
    _assert_managed_path(path, state=state)
    path.parent.mkdir(parents=True, exist_ok=True)
    _assert_managed_path(path.parent, state=state)
    descriptor, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)
    return _sha256_bytes(data)


@contextmanager
def _lock(path: Path, *, state: Path | None = None) -> Iterator[None]:
    allowed = state or path.parent
    _assert_managed_path(path.parent, state=allowed)
    _assert_managed_path(path, state=allowed)
    path.parent.mkdir(parents=True, exist_ok=True)
    _assert_managed_path(path.parent, state=allowed)
    with path.open("a+", encoding="utf-8") as handle:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def _state_dir(repo: Path, state_dir: str | Path | None) -> Path:
    value = Path(state_dir) if state_dir is not None else repo / DEFAULT_STATE_RELATIVE
    if not value.is_absolute():
        value = repo / value
    lexical = Path(os.path.abspath(value))
    anchor = _repo_lexical_anchor(lexical, repo=repo)
    if anchor is not None:
        _reject_symlink_components(lexical, anchor=anchor)
    else:
        _reject_external_state_symlinks(lexical, repo=repo)
    resolved = lexical.resolve()
    if anchor is not None and not _is_relative_to(resolved, repo):
        raise TurnReceiptError("managed_state_path_escape")
    return resolved


def _receipt_root(state: Path) -> Path:
    root = state / "turn-receipts"
    _assert_managed_path(root, state=state)
    return root


def _validated_envelope(
    target_envelope: dict[str, Any], *, entry_kind: str
) -> dict[str, Any]:
    if entry_kind not in ENTRY_TO_CONVERSATION:
        raise TurnReceiptError("invalid_entry_kind")
    if not isinstance(target_envelope, dict):
        raise TurnReceiptError("invalid_target_envelope")
    if target_envelope.get("schema") != ENVELOPE_SCHEMA:
        raise TurnReceiptError("invalid_target_envelope")
    request_id = target_envelope.get("request_id")
    if not isinstance(request_id, str) or not POINTER_KEY_RE.fullmatch(request_id):
        raise TurnReceiptError("managed_turn_requires_unique_request_id")
    if target_envelope.get("conversation_kind") != ENTRY_TO_CONVERSATION[entry_kind]:
        raise TurnReceiptError("entry_conversation_kind_mismatch")
    # Date validation is deliberately performed before resolving the managed
    # state directory, taking a lock, calling preanswer, or writing any object.
    # A malformed date therefore cannot create another invalid durable begin.
    if not _is_valid_local_date(target_envelope.get("as_of")):
        raise TurnReceiptError("managed_turn_requires_valid_as_of")
    return target_envelope


def _root_manifest_binding(state: Path) -> dict[str, Any]:
    path = state / "manifest.json"
    _assert_managed_path(path, state=state)
    if not path.is_file():
        return {
            "root_manifest_sha256": None,
            "projection_build_id": None,
            "recent_incremental_sha256": None,
            "historical_manifest_sha256": None,
            "historical_index_sha256": None,
        }
    try:
        manifest = _read_json(path)
    except TurnReceiptError:
        return {
            "root_manifest_sha256": _sha256_file(path),
            "projection_build_id": None,
            "recent_incremental_sha256": None,
            "historical_manifest_sha256": None,
            "historical_index_sha256": None,
        }
    hashes = manifest.get("artifact_hashes") or {}
    return {
        "root_manifest_sha256": _sha256_file(path),
        "projection_build_id": manifest.get("build_id"),
        "recent_incremental_sha256": hashes.get("failure_signatures.json"),
        "historical_manifest_sha256": manifest.get("corpus_manifest_sha256"),
        "historical_index_sha256": manifest.get("corpus_index_sha256"),
    }


def _bound_root_manifest(begin: dict[str, Any]) -> dict[str, Any]:
    binding = {key: begin.get(key) for key in ROOT_MANIFEST_BINDING_FIELDS}
    for key in (
        "root_manifest_sha256",
        "recent_incremental_sha256",
        "historical_manifest_sha256",
        "historical_index_sha256",
    ):
        value = binding[key]
        if value is not None and (not isinstance(value, str) or not SHA_RE.fullmatch(value)):
            raise TurnReceiptError("turn_receipt_manifest_binding_invalid")
    build_id = binding["projection_build_id"]
    if build_id is not None and not isinstance(build_id, str):
        raise TurnReceiptError("turn_receipt_manifest_binding_invalid")
    return binding


def _target_view(envelope: dict[str, Any], result: dict[str, Any]) -> dict[str, Any]:
    context = result.get("context") if isinstance(result.get("context"), dict) else {}
    targets = context.get("current_targets") if isinstance(context.get("current_targets"), dict) else {}
    return {
        "subject": envelope.get("subject"),
        "point_ids": list(targets.get("point_ids") or envelope.get("point_ids") or []),
        "mechanism_ids": list(
            targets.get("mechanism_ids") or envelope.get("mechanism_ids") or []
        ),
        "mechanism_primitive_ids": list(
            targets.get("mechanism_primitive_ids")
            or envelope.get("mechanism_primitive_ids")
            or []
        ),
        "formal_node_ids": list(
            targets.get("formal_node_ids") or envelope.get("formal_node_ids") or []
        ),
        "module_ids": list(targets.get("module_ids") or envelope.get("module_ids") or []),
        "error_pattern_ids": list(envelope.get("error_pattern_ids") or []),
        "method_ids": list(envelope.get("method_ids") or []),
    }


def _degraded_response_plan(
    *, targets: dict[str, Any], reason_code: str
) -> dict[str, Any]:
    return {
        "current_target": targets,
        "direct_recent_errors": [],
        "direct_historical_errors": [],
        "common_patterns": [],
        "verified_cross_connections": [],
        "explanation_strategy": {
            "strategy": "degraded_generic_answer",
            "personalization_claim_allowed": False,
        },
        "first_boundary_to_check": {},
        "optional_practice_action": {"action": "none"},
        "evidence_gaps": [reason_code],
    }


def _response_plan_from_slice(
    slice_result: dict[str, Any], *, targets: dict[str, Any]
) -> dict[str, Any]:
    context = (
        slice_result.get("context")
        if isinstance(slice_result.get("context"), dict)
        else {}
    )
    plan = context.get("response_plan")
    if isinstance(plan, dict):
        return plan
    reason = str(slice_result.get("reason_code") or "lookup_unavailable")
    return _degraded_response_plan(targets=targets, reason_code=reason)


def _bounded_runtime_list(value: Any, limit: int) -> list[Any]:
    return list(value[:limit]) if isinstance(value, list) else []


def _runtime_target(value: Any) -> dict[str, Any]:
    target = value if isinstance(value, dict) else {}
    result: dict[str, Any] = {}
    for key, limit in (
        ("subjects", 2),
        ("point_ids", 4),
        ("mechanism_ids", 4),
        ("mechanism_primitive_ids", 3),
        ("formal_node_ids", 2),
        ("module_ids", 2),
    ):
        values = _bounded_runtime_list(target.get(key), limit)
        if values:
            result[key] = values
    subject = target.get("subject")
    if isinstance(subject, str) and subject:
        result["subject"] = subject
    return result


def _runtime_match(value: Any) -> dict[str, Any]:
    row = value if isinstance(value, dict) else {}
    result: dict[str, Any] = {}
    for key in (
        "signature_id",
        "evidence_event_id",
        "pattern_id",
        "edge_id",
        "match_basis",
        "relation_code",
        "strength",
        "other_point_id",
        "target_point_id",
        "formal_id",
        "study_date",
        "status",
    ):
        item = row.get(key)
        if isinstance(item, (str, bool, int)) and not isinstance(item, float):
            result[key] = item
    if isinstance(row.get("live_routable"), bool):
        result["live_routable"] = row["live_routable"]
    for key, limit in (
        ("evidence_event_ids", 2),
        ("shared_boundaries", 2),
        ("point_ids", 3),
    ):
        values = _bounded_runtime_list(row.get(key), limit)
        if values:
            result[key] = values
    signature = row.get("failure_signature")
    if isinstance(signature, dict):
        signature_id = signature.get("signature_id")
        if isinstance(signature_id, str):
            result.setdefault("signature_id", signature_id)
        evidence = signature.get("evidence")
        event_id = evidence.get("event_id") if isinstance(evidence, dict) else None
        if isinstance(event_id, str):
            result.setdefault("evidence_event_id", event_id)
    return result


def _runtime_mapping(value: Any, allowed: tuple[str, ...]) -> dict[str, Any]:
    source = value if isinstance(value, dict) else {}
    result: dict[str, Any] = {}
    for key in allowed:
        item = source.get(key)
        if isinstance(item, str):
            result[key] = item[:160]
        elif isinstance(item, (bool, int)) and not isinstance(item, float):
            result[key] = item
    return result


def _compact_runtime_plan(plan: dict[str, Any]) -> dict[str, Any]:
    """Expose only the nine actionable fields, without weak background rows."""

    return {
        "current_target": _runtime_target(plan.get("current_target")),
        "direct_recent_errors": [
            _runtime_match(row)
            for row in _bounded_runtime_list(plan.get("direct_recent_errors"), 2)
        ],
        "direct_historical_errors": [
            _runtime_match(row)
            for row in _bounded_runtime_list(plan.get("direct_historical_errors"), 1)
        ],
        "common_patterns": [
            _runtime_match(row)
            for row in _bounded_runtime_list(plan.get("common_patterns"), 1)
        ],
        "verified_cross_connections": [
            _runtime_match(row)
            for row in _bounded_runtime_list(plan.get("verified_cross_connections"), 1)
        ],
        "explanation_strategy": _runtime_mapping(
            plan.get("explanation_strategy"),
            ("strategy", "depth", "personalization_claim_allowed"),
        ),
        "first_boundary_to_check": _runtime_mapping(
            plan.get("first_boundary_to_check"),
            ("boundary_id", "boundary", "boundary_label", "status"),
        ),
        "optional_practice_action": _runtime_mapping(
            plan.get("optional_practice_action"),
            ("action", "candidate_id", "reason", "scheduling_authority"),
        ),
        "evidence_gaps": [
            str(value)[:120]
            for value in _bounded_runtime_list(plan.get("evidence_gaps"), 3)
            if isinstance(value, str)
        ],
    }


def _compact_slice_status(slice_result: dict[str, Any]) -> dict[str, Any]:
    result: dict[str, Any] = {
        "status": slice_result.get("status"),
        "as_of": slice_result.get("as_of"),
    }
    if isinstance(slice_result.get("slice_id"), str):
        result["slice_id"] = slice_result["slice_id"]
    if isinstance(slice_result.get("reason_code"), str):
        result["reason_code"] = slice_result["reason_code"]
    return result


def _public_begin_result(
    *,
    status: str,
    receipt: dict[str, Any],
    receipt_ref: str,
    receipt_sha256: str,
    slice_result: dict[str, Any],
    response_plan: dict[str, Any],
    preanswer_call_count: int,
    include_full: bool,
) -> dict[str, Any]:
    result: dict[str, Any] = {
        "schema": PUBLIC_BEGIN_SCHEMA,
        "status": status,
        "turn_receipt_ref": receipt_ref,
        "turn_receipt_sha256": receipt_sha256,
        "turn_id": receipt.get("turn_id"),
        "receipt_schema": receipt.get("schema"),
        "preanswer_call_count": preanswer_call_count,
        "retrieval_status": receipt.get("retrieval_status"),
        "degraded_reason": receipt.get("degraded_reason"),
        "slice": _compact_slice_status(slice_result),
        "runtime_plan": _compact_runtime_plan(response_plan),
        "full_slice_available": True,
        "formal_write_count": 0,
    }
    if include_full:
        result["begin_receipt"] = receipt
        result["slice"] = slice_result
        result["runtime_plan"] = response_plan
        return result
    if len(_json_bytes(result)) <= PUBLIC_BEGIN_MAX_BYTES:
        return result

    runtime = result["runtime_plan"]
    assert isinstance(runtime, dict)
    for key in (
        "direct_historical_errors",
        "verified_cross_connections",
        "common_patterns",
        "direct_recent_errors",
    ):
        rows = runtime.get(key)
        if isinstance(rows, list):
            rows.clear()
        if len(_json_bytes(result)) <= PUBLIC_BEGIN_MAX_BYTES:
            return result
    runtime["optional_practice_action"] = {
        "action": (runtime.get("optional_practice_action") or {}).get("action")
    }
    runtime["first_boundary_to_check"] = {
        "boundary_id": (runtime.get("first_boundary_to_check") or {}).get(
            "boundary_id"
        )
    }
    runtime["evidence_gaps"] = list(runtime.get("evidence_gaps") or [])[:1]
    target = runtime.get("current_target")
    if isinstance(target, dict):
        for key, value in list(target.items()):
            if isinstance(value, list):
                target[key] = [str(item)[:80] for item in value[:1]]
            elif isinstance(value, str):
                target[key] = value[:80]
    if len(_json_bytes(result)) > PUBLIC_BEGIN_MAX_BYTES:
        strategy = runtime.get("explanation_strategy")
        runtime = {
            "current_target": target if isinstance(target, dict) else {},
            "direct_recent_errors": [],
            "direct_historical_errors": [],
            "common_patterns": [],
            "verified_cross_connections": [],
            "explanation_strategy": {
                "strategy": (strategy or {}).get("strategy"),
                "personalization_claim_allowed": bool(
                    (strategy or {}).get("personalization_claim_allowed")
                ),
            },
            "first_boundary_to_check": {},
            "optional_practice_action": {
                "action": (
                    result["runtime_plan"].get("optional_practice_action") or {}
                ).get("action")
            },
            "evidence_gaps": list(runtime.get("evidence_gaps") or [])[:1],
        }
        result["runtime_plan"] = runtime
    if len(_json_bytes(result)) > PUBLIC_BEGIN_MAX_BYTES:
        raise TurnReceiptError("compact_begin_result_budget_exceeded")
    return result


def _write_content_object(
    *, root: Path, kind: str, payload: dict[str, Any]
) -> tuple[str, Path]:
    _retired_write_surface()

    # Historical implementation retained below for read-only source compatibility.
    state = root.parent
    raw = _json_bytes(payload, pretty=True)
    digest = _sha256_bytes(raw)
    path = root / "objects" / kind / f"{digest}.json"
    _assert_managed_path(path, state=state)
    if path.exists():
        if path.is_symlink() or _sha256_file(path) != digest:
            raise TurnReceiptError("content_addressed_receipt_conflict")
    else:
        _atomic_write(path, payload, state=state)
    return digest, path


def _bind_once(
    *, root: Path, namespace: str, key: str, object_sha256: str
) -> Path:
    _retired_write_surface()

    # Historical implementation retained below for read-only source compatibility.
    state = root.parent
    if not POINTER_KEY_RE.fullmatch(key) and not SHA_RE.fullmatch(key):
        raise TurnReceiptError("receipt_pointer_key_invalid")
    pointer = root / "pointers" / namespace / f"{key}.json"
    _assert_managed_path(pointer, state=state)
    value = {
        "schema": "personalization_receipt_pointer_v1",
        "namespace": namespace,
        "key": key,
        "object_sha256": object_sha256,
    }
    if pointer.exists():
        existing = _read_json(pointer)
        if existing != value:
            raise TurnReceiptError("managed_turn_already_bound_differently")
    else:
        _atomic_write(pointer, value, state=state)
    return pointer


def begin_personalized_turn(
    repo: str | Path,
    *,
    entry_kind: str,
    target_envelope: dict[str, Any],
    state_dir: str | Path | None = None,
    max_bytes: int = DEFAULT_MAX_BYTES,
    mode: str = "verified",
    receipt_version: int = 2,
    include_full: bool = False,
) -> dict[str, Any]:
    """Call preanswer exactly once and persist a durable begin receipt.

    New calls default to the v2 detached-slice contract.  ``receipt_version=1``
    remains available only for compatibility tests and controlled migration.
    """

    _retired_write_surface()

    # Historical implementation retained below for read-only source compatibility.
    root_repo = Path(repo).resolve()
    envelope = _validated_envelope(target_envelope, entry_kind=entry_kind)
    if receipt_version not in {1, 2}:
        raise TurnReceiptError("invalid_begin_receipt_version")
    state = _state_dir(root_repo, state_dir)
    receipt_root = _receipt_root(state)
    request_sha256 = _sha256_bytes(
        _json_bytes(
            {
                "entry_kind": entry_kind,
                "target_envelope": envelope,
                "mode": mode,
                "max_bytes": int(max_bytes),
            }
        )
    )
    request_id = str(envelope["request_id"])
    # The projection builder uses this same outer lock.  Keep the bounded lookup,
    # root-manifest binding, receipt object, and request pointer in one snapshot
    # transaction so an OLD slice can never be paired with a NEW root binding.
    # The lock order matches finalize: projection first, receipt second.
    with _lock(state / ".hot-path.lock", state=state):
        with _lock(receipt_root / ".turn-receipts.lock", state=state):
            existing_pointer = (
                receipt_root / "pointers" / "request" / f"{request_id}.json"
            )
            _assert_managed_path(existing_pointer, state=state)
            if existing_pointer.exists():
                pointer = _read_json(existing_pointer)
                digest = str(pointer.get("object_sha256") or "")
                _pointer_value(
                    existing_pointer,
                    namespace="request",
                    key=request_id,
                    object_sha256=digest,
                )
                object_path = receipt_root / "objects" / "begin" / f"{digest}.json"
                _assert_managed_path(object_path, state=state)
                if (
                    not object_path.is_file()
                    or object_path.is_symlink()
                    or _sha256_file(object_path) != digest
                ):
                    raise TurnReceiptError("turn_receipt_missing_or_drifted")
                existing = _read_json(object_path)
                snapshot = _validate_current_begin_contract(
                    existing, receipt_root=receipt_root
                )
                if existing.get("request_sha256") != request_sha256:
                    raise TurnReceiptError("request_id_reused_with_different_request")
                response_plan = _response_plan_from_slice(
                    snapshot, targets=dict(existing.get("targets") or {})
                )
                return _public_begin_result(
                    status="already_begun",
                    receipt=existing,
                    receipt_ref=object_path.relative_to(state).as_posix(),
                    receipt_sha256=digest,
                    slice_result=snapshot,
                    response_plan=response_plan,
                    preanswer_call_count=0,
                    include_full=include_full,
                )

            # This is the only preanswer call in the managed begin transaction.
            slice_result = safe_preanswer_slice(
                root_repo,
                target_envelope=envelope,
                state_dir=state,
                max_bytes=max_bytes,
                mode=mode,
            )
            if slice_result.get("status") != "ready" and "as_of" not in slice_result:
                slice_result = {**slice_result, "as_of": envelope.get("as_of")}
            slice_sha256 = _sha256_bytes(_json_bytes(slice_result))
            context = (
                slice_result.get("context")
                if isinstance(slice_result.get("context"), dict)
                else {}
            )
            bindings = _root_manifest_binding(state)
            # safe_preanswer_slice deliberately reports ``unavailable`` on a
            # fail-soft lookup.  The managed receipt has a two-state contract:
            # ready, or degraded-but-displayable through the generic action.
            retrieval_status = (
                "ready" if slice_result.get("status") == "ready" else "degraded"
            )
            targets = (
                dict(context["current_targets"])
                if isinstance(context.get("current_targets"), dict)
                else _target_view(envelope, slice_result)
            )
            response_plan = _response_plan_from_slice(
                slice_result, targets=targets
            )
            common_receipt = {
                "kind": "personalization_turn_begin",
                "entry_kind": entry_kind,
                "conversation_kind": envelope["conversation_kind"],
                "request_id": request_id,
                "request_sha256": request_sha256,
                "local_date": envelope.get("as_of"),
                "targets": targets,
                **bindings,
                "slice_id": slice_result.get("slice_id"),
                "slice_sha256": slice_sha256,
                "retrieval_status": retrieval_status,
                "degraded_reason": slice_result.get("reason_code"),
                "response_plan_sha256": _sha256_bytes(_json_bytes(response_plan)),
                "preanswer_call_count": 1,
                "answer_safe": True,
                "formal_write_count": 0,
            }
            if receipt_version == 1:
                receipt_without_id = {
                    "schema": BEGIN_SCHEMA,
                    **common_receipt,
                    "slice_snapshot": slice_result,
                    "response_plan": response_plan,
                }
            else:
                slice_object_sha256, slice_object_path = _write_content_object(
                    root=receipt_root, kind="slice", payload=slice_result
                )
                receipt_without_id = {
                    "schema": BEGIN_SCHEMA_V2,
                    **common_receipt,
                    "slice_object_ref": slice_object_path.relative_to(
                        state
                    ).as_posix(),
                    "slice_object_sha256": slice_object_sha256,
                }
            turn_id = (
                "PTR-"
                + _sha256_bytes(_json_bytes(receipt_without_id))[:24].upper()
            )
            receipt = {**receipt_without_id, "turn_id": turn_id}
            digest, object_path = _write_content_object(
                root=receipt_root, kind="begin", payload=receipt
            )
            _bind_once(
                root=receipt_root,
                namespace="request",
                key=request_id,
                object_sha256=digest,
            )
    return _public_begin_result(
        status="begun",
        receipt=receipt,
        receipt_ref=object_path.relative_to(state).as_posix(),
        receipt_sha256=digest,
        slice_result=slice_result,
        response_plan=response_plan,
        preanswer_call_count=1,
        include_full=include_full,
    )


def _collect_allowed_ids(plan: dict[str, Any]) -> tuple[set[str], set[str]]:
    evidence_ids: set[str] = set()
    match_ids: set[str] = set()
    for key in ("direct_recent_errors", "direct_historical_errors"):
        for row in plan.get(key, []) or []:
            if not isinstance(row, dict):
                continue
            for candidate in (
                row.get("evidence_event_id"),
                (row.get("failure_signature") or {}).get("evidence", {}).get("event_id")
                if isinstance(row.get("failure_signature"), dict)
                else None,
            ):
                if isinstance(candidate, str) and SAFE_ID_RE.fullmatch(candidate):
                    evidence_ids.add(candidate)
            if isinstance(row.get("signature_id"), str):
                match_ids.add(row["signature_id"])
    for row in plan.get("common_patterns", []) or []:
        if not isinstance(row, dict):
            continue
        if isinstance(row.get("pattern_id"), str):
            match_ids.add(row["pattern_id"])
        for value in row.get("evidence_event_ids", []) or []:
            if isinstance(value, str) and SAFE_ID_RE.fullmatch(value):
                evidence_ids.add(value)
    for row in plan.get("verified_cross_connections", []) or []:
        if not isinstance(row, dict):
            continue
        if isinstance(row.get("edge_id"), str):
            match_ids.add(row["edge_id"])
        for evidence in row.get("user_evidence", []) or []:
            if not isinstance(evidence, dict):
                continue
            event_id = evidence.get("evidence_event_id")
            signature_id = evidence.get("signature_id")
            if isinstance(event_id, str) and SAFE_ID_RE.fullmatch(event_id):
                evidence_ids.add(event_id)
            if isinstance(signature_id, str) and SAFE_ID_RE.fullmatch(signature_id):
                match_ids.add(signature_id)
    return evidence_ids, match_ids


def _collect_section_ids(rows: Any) -> tuple[set[str], set[str]]:
    evidence_ids: set[str] = set()
    match_ids: set[str] = set()
    if not isinstance(rows, list):
        return evidence_ids, match_ids
    for row in rows:
        if not isinstance(row, dict):
            continue
        for key in ("signature_id", "pattern_id", "edge_id"):
            value = row.get(key)
            if isinstance(value, str) and SAFE_ID_RE.fullmatch(value):
                match_ids.add(value)
        signature = row.get("failure_signature")
        if isinstance(signature, dict):
            signature_id = signature.get("signature_id")
            if isinstance(signature_id, str) and SAFE_ID_RE.fullmatch(signature_id):
                match_ids.add(signature_id)
            evidence = signature.get("evidence")
            if isinstance(evidence, dict):
                event_id = evidence.get("event_id")
                if isinstance(event_id, str) and SAFE_ID_RE.fullmatch(event_id):
                    evidence_ids.add(event_id)
        event_id = row.get("evidence_event_id")
        if isinstance(event_id, str) and SAFE_ID_RE.fullmatch(event_id):
            evidence_ids.add(event_id)
        for event_id in row.get("evidence_event_ids", []) or []:
            if isinstance(event_id, str) and SAFE_ID_RE.fullmatch(event_id):
                evidence_ids.add(event_id)
    return evidence_ids, match_ids


def _validate_adoption_against_begin(
    begin: dict[str, Any],
    adopted: dict[str, Any],
    *,
    response_plan: dict[str, Any] | None = None,
) -> None:
    plan = response_plan if response_plan is not None else begin.get("response_plan")
    if not isinstance(plan, dict):
        raise TurnReceiptError("response_plan_binding_invalid")
    allowed_evidence, allowed_matches = _collect_allowed_ids(plan)
    used_evidence = set(adopted["used_evidence_ids"])
    used_matches = set(adopted["used_match_ids"])
    if not used_evidence <= allowed_evidence:
        raise TurnReceiptError("adoption_uses_unretrieved_evidence")
    if not used_matches <= allowed_matches:
        raise TurnReceiptError("adoption_uses_unretrieved_match")

    ready = begin.get("retrieval_status") == "ready"
    strategy = plan.get("explanation_strategy")
    personalization_allowed = bool(
        isinstance(strategy, dict) and strategy.get("personalization_claim_allowed")
    )
    if adopted["personalization_claimed"] and not ready:
        raise TurnReceiptError("cannot_claim_personalization_when_degraded")
    if adopted["personalization_claimed"] and not personalization_allowed:
        raise TurnReceiptError("personalization_claim_not_authorized_by_plan")
    if adopted["personalization_claimed"] and not (used_evidence or used_matches):
        raise TurnReceiptError("personalization_claim_requires_adopted_evidence")
    if not ready and "degraded_generic_answer" not in adopted["actions"]:
        raise TurnReceiptError("degraded_reply_action_required")
    if ready and "degraded_generic_answer" in adopted["actions"]:
        raise TurnReceiptError("degraded_action_not_authorized_when_ready")

    recent_evidence, recent_matches = _collect_section_ids(
        plan.get("direct_recent_errors")
    )
    historical_evidence, historical_matches = _collect_section_ids(
        plan.get("direct_historical_errors")
    )
    pattern_evidence, pattern_matches = _collect_section_ids(
        plan.get("common_patterns")
    )
    boundary = plan.get("first_boundary_to_check")
    boundary_authorized = isinstance(boundary, dict) and bool(
        boundary.get("boundary_id")
        or boundary.get("boundary")
        or boundary.get("boundary_label")
    ) and boundary.get("status") != "not_identified"
    optional_practice = plan.get("optional_practice_action")
    optional_action = (
        optional_practice.get("action")
        if isinstance(optional_practice, dict)
        else None
    )
    entry_kind = begin.get("entry_kind")

    cross_rows = plan.get("verified_cross_connections")
    live_cross_ids = {
        str(row.get("edge_id"))
        for row in cross_rows or []
        if isinstance(row, dict)
        and row.get("live_routable") is True
        and isinstance(row.get("edge_id"), str)
        and SAFE_ID_RE.fullmatch(str(row.get("edge_id")))
    }

    for action in adopted["actions"]:
        if action in {"direct_answer", "minimum_framework"}:
            continue
        if action == "degraded_generic_answer":
            continue
        if action in INDEPENDENT_SELECTION_ACTIONS:
            # A pre-answer optional action has no scheduling authority.  The v1
            # adoption schema carries no independent selection/source receipt,
            # so these display actions must remain closed.
            raise TurnReceiptError(
                "practice_action_requires_independent_selection_receipt"
            )
        if action == "boundary_check":
            relevant_evidence = recent_evidence | historical_evidence | pattern_evidence
            relevant_matches = recent_matches | historical_matches | pattern_matches
            if not boundary_authorized or not (
                used_evidence & relevant_evidence or used_matches & relevant_matches
            ):
                raise TurnReceiptError("adoption_action_not_authorized_by_plan")
            continue
        if action == "historical_connection":
            if not adopted["personalization_claimed"] or not (
                used_evidence & historical_evidence
                or used_matches & historical_matches
            ):
                raise TurnReceiptError("adoption_action_not_authorized_by_evidence")
            continue
        if action == "cross_subject_connection":
            if not (used_matches & live_cross_ids):
                raise TurnReceiptError("cross_action_requires_live_adopted_edge")
            continue
        if action == "mechanism_check":
            relevant_evidence = recent_evidence | historical_evidence | pattern_evidence
            relevant_matches = recent_matches | historical_matches | pattern_matches
            if optional_action not in {"mechanism_check", "mechanism_check_only"} or not (
                used_evidence & relevant_evidence or used_matches & relevant_matches
            ):
                raise TurnReceiptError("adoption_action_not_authorized_by_plan")
            continue
        if action == "active_recall":
            if entry_kind not in ACTIVE_RECALL_ENTRY_KINDS:
                raise TurnReceiptError("adoption_action_not_authorized_for_entry")
            continue
        if action == "fast_capture_status":
            if entry_kind not in {"wrong_intake", "morning_answer_feedback"}:
                raise TurnReceiptError("adoption_action_not_authorized_for_entry")
            continue
        if action == "nightly_curation_status":
            if entry_kind != "nightly_curation":
                raise TurnReceiptError("adoption_action_not_authorized_for_entry")
            continue
        raise TurnReceiptError("invalid_response_action")


def _validate_begin_contract(begin: dict[str, Any]) -> None:
    if (
        set(begin) != CURRENT_BEGIN_KEYS
        or begin.get("schema") != BEGIN_SCHEMA
        or begin.get("kind") != "personalization_turn_begin"
        or type(begin.get("preanswer_call_count")) is not int
        or begin.get("preanswer_call_count") != 1
    ):
        raise TurnReceiptError("turn_receipt_contract_invalid")
    entry_kind = begin.get("entry_kind")
    if (
        entry_kind not in ENTRY_TO_CONVERSATION
        or begin.get("conversation_kind") != ENTRY_TO_CONVERSATION[entry_kind]
    ):
        raise TurnReceiptError("turn_receipt_contract_invalid")
    request_id = begin.get("request_id")
    local_date = begin.get("local_date")
    if (
        not isinstance(request_id, str)
        or not POINTER_KEY_RE.fullmatch(request_id)
        or not _is_valid_local_date(local_date)
    ):
        raise TurnReceiptError("turn_receipt_contract_invalid")
    for key in ("request_sha256", "slice_sha256", "response_plan_sha256"):
        value = begin.get(key)
        if not isinstance(value, str) or not SHA_RE.fullmatch(value):
            raise TurnReceiptError("turn_receipt_contract_invalid")
    plan = begin.get("response_plan")
    if not isinstance(plan, dict) or _sha256_bytes(_json_bytes(plan)) != begin.get(
        "response_plan_sha256"
    ):
        raise TurnReceiptError("response_plan_binding_invalid")
    if (
        begin.get("answer_safe") is not True
        or type(begin.get("formal_write_count")) is not int
        or begin.get("formal_write_count") != 0
    ):
        raise TurnReceiptError("turn_receipt_contract_invalid")
    if begin.get("retrieval_status") not in {"ready", "degraded"}:
        raise TurnReceiptError("turn_receipt_contract_invalid")
    snapshot = begin.get("slice_snapshot")
    if (
        not isinstance(snapshot, dict)
        or _sha256_bytes(_json_bytes(snapshot)) != begin.get("slice_sha256")
        or snapshot.get("schema") != PREANSWER_SCHEMA
        or snapshot.get("request_id") != request_id
        or snapshot.get("as_of") != local_date
        or snapshot.get("read_only") is not True
        or type(snapshot.get("formal_write_count")) is not int
        or snapshot.get("formal_write_count") != 0
        or snapshot.get("scheduling_authority") != "none"
    ):
        raise TurnReceiptError("slice_snapshot_binding_invalid")
    snapshot_status = snapshot.get("status")
    if begin.get("retrieval_status") == "ready":
        context = snapshot.get("context")
        if (
            set(snapshot) != READY_SLICE_KEYS
            or snapshot_status != "ready"
            or not isinstance(snapshot.get("slice_id"), str)
            or not SAFE_ID_RE.fullmatch(str(snapshot.get("slice_id")))
            or snapshot.get("slice_id") != begin.get("slice_id")
            or snapshot.get("conversation_kind") != begin.get("conversation_kind")
            or snapshot.get("mode") not in {"off", "shadow", "verified"}
            or not isinstance(snapshot.get("request_sha256"), str)
            or not SHA_RE.fullmatch(str(snapshot.get("request_sha256")))
            or not isinstance(snapshot.get("truncated"), bool)
            or type(snapshot.get("byte_count")) is not int
            or snapshot.get("byte_count", -1) < 0
            or not isinstance(context, dict)
            or not isinstance(context.get("current_targets"), dict)
            or not isinstance(context.get("response_plan"), dict)
            or _json_bytes(begin.get("targets"))
            != _json_bytes(context.get("current_targets"))
            or _json_bytes(plan) != _json_bytes(context.get("response_plan"))
            or begin.get("degraded_reason") is not None
        ):
            raise TurnReceiptError("slice_snapshot_binding_invalid")
        if snapshot.get("projection_build_id") != begin.get("projection_build_id"):
            raise TurnReceiptError("slice_manifest_binding_invalid")
        layer_hashes = snapshot.get("layer_hashes")
        if not isinstance(layer_hashes, dict) or any(
            layer_hashes.get(layer_key) != begin.get(begin_key)
            for layer_key, begin_key in (
                ("recent_incremental_sha256", "recent_incremental_sha256"),
                ("historical_manifest_sha256", "historical_manifest_sha256"),
                ("historical_index_sha256", "historical_index_sha256"),
            )
        ):
            raise TurnReceiptError("slice_manifest_binding_invalid")
    else:
        if (
            set(snapshot) != DEGRADED_SLICE_KEYS
            or snapshot_status != "unavailable"
            or begin.get("slice_id") is not None
            or not isinstance(snapshot.get("reason_code"), str)
            or not snapshot.get("reason_code")
            or len(str(snapshot.get("reason_code"))) > 120
            or begin.get("degraded_reason") != snapshot.get("reason_code")
        ):
            raise TurnReceiptError("slice_snapshot_binding_invalid")
        _validate_legacy_targets(begin.get("targets"))
        if (
            plan.get("current_target") != begin.get("targets")
            or plan.get("direct_recent_errors") != []
            or plan.get("direct_historical_errors") != []
            or plan.get("common_patterns") != []
            or plan.get("verified_cross_connections") != []
            or plan.get("explanation_strategy")
            != {
                "strategy": "degraded_generic_answer",
                "personalization_claim_allowed": False,
            }
            or plan.get("first_boundary_to_check") != {}
            or plan.get("optional_practice_action") != {"action": "none"}
            or plan.get("evidence_gaps") != [snapshot.get("reason_code")]
        ):
            raise TurnReceiptError("response_plan_binding_invalid")
    _bound_root_manifest(begin)
    payload = dict(begin)
    turn_id = payload.pop("turn_id", None)
    expected_turn_id = "PTR-" + _sha256_bytes(_json_bytes(payload))[:24].upper()
    if turn_id != expected_turn_id:
        raise TurnReceiptError("turn_receipt_identity_invalid")


def _load_v2_slice_object(
    begin: dict[str, Any], *, receipt_root: Path
) -> dict[str, Any]:
    state = receipt_root.parent
    digest = begin.get("slice_object_sha256")
    reference = begin.get("slice_object_ref")
    if not isinstance(digest, str) or not SHA_RE.fullmatch(digest):
        raise TurnReceiptError("slice_object_binding_invalid")
    path = receipt_root / "objects" / "slice" / f"{digest}.json"
    expected_ref = path.relative_to(state).as_posix()
    _assert_managed_path(path, state=state)
    if reference != expected_ref:
        raise TurnReceiptError("slice_object_binding_invalid")
    if (
        not path.is_file()
        or path.is_symlink()
        or _sha256_file(path) != digest
    ):
        raise TurnReceiptError("slice_object_missing_or_drifted")
    return _read_json(path)


def _validate_begin_contract_v2(
    begin: dict[str, Any], *, receipt_root: Path
) -> dict[str, Any]:
    if (
        set(begin) != CURRENT_V2_BEGIN_KEYS
        or begin.get("schema") != BEGIN_SCHEMA_V2
        or begin.get("kind") != "personalization_turn_begin"
        or type(begin.get("preanswer_call_count")) is not int
        or begin.get("preanswer_call_count") != 1
    ):
        raise TurnReceiptError("turn_receipt_v2_contract_invalid")
    snapshot = _load_v2_slice_object(begin, receipt_root=receipt_root)
    plan = _response_plan_from_slice(
        snapshot, targets=dict(begin.get("targets") or {})
    )
    if _sha256_bytes(_json_bytes(plan)) != begin.get("response_plan_sha256"):
        raise TurnReceiptError("response_plan_binding_invalid")

    # Reuse every v1 semantic and answer-safety check against an equivalent
    # in-memory view.  The detached slice is never copied back into the v2
    # durable receipt.
    compatible = {
        key: value
        for key, value in begin.items()
        if key not in {"slice_object_ref", "slice_object_sha256", "turn_id"}
    }
    compatible["schema"] = BEGIN_SCHEMA
    compatible["response_plan"] = plan
    compatible["slice_snapshot"] = snapshot
    compatible["turn_id"] = (
        "PTR-"
        + _sha256_bytes(_json_bytes(compatible))[:24].upper()
    )
    _validate_begin_contract(compatible)

    payload = dict(begin)
    turn_id = payload.pop("turn_id", None)
    expected_turn_id = "PTR-" + _sha256_bytes(_json_bytes(payload))[:24].upper()
    if turn_id != expected_turn_id:
        raise TurnReceiptError("turn_receipt_identity_invalid")
    return snapshot


def _validate_current_begin_contract(
    begin: dict[str, Any], *, receipt_root: Path
) -> dict[str, Any]:
    if begin.get("schema") == BEGIN_SCHEMA_V2:
        return _validate_begin_contract_v2(begin, receipt_root=receipt_root)
    _validate_begin_contract(begin)
    snapshot = begin.get("slice_snapshot")
    if not isinstance(snapshot, dict):
        raise TurnReceiptError("slice_snapshot_binding_invalid")
    return snapshot


def _validate_legacy_targets(targets: Any) -> None:
    expected_keys = {
        "subject",
        "point_ids",
        "mechanism_ids",
        "mechanism_primitive_ids",
        "formal_node_ids",
        "module_ids",
        "error_pattern_ids",
        "method_ids",
    }
    if not isinstance(targets, dict) or set(targets) != expected_keys:
        raise TurnReceiptError("legacy_begin_contract_invalid")
    subject = targets.get("subject")
    if subject is not None and subject not in {"DS", "CO", "OS", "CN"}:
        raise TurnReceiptError("legacy_begin_contract_invalid")
    for key in expected_keys - {"subject"}:
        values = targets.get(key)
        if (
            not isinstance(values, list)
            or len(values) > 24
            or any(
                not isinstance(item, str) or not SAFE_ID_RE.fullmatch(item)
                for item in values
            )
            or len(values) != len(set(values))
        ):
            raise TurnReceiptError("legacy_begin_contract_invalid")


def _validate_legacy_begin_contract(begin: dict[str, Any]) -> None:
    """Recognize only the narrow pre-slice-snapshot degraded contract.

    This validator is intentionally separate from ``_validate_begin_contract``.
    A recognized object remains invalid under the current contract and can only
    be archived by an independently content-addressed legacy seal.
    """

    if set(begin) != LEGACY_BEGIN_KEYS or "slice_snapshot" in begin:
        raise TurnReceiptError("legacy_begin_contract_invalid")
    if (
        begin.get("schema") != BEGIN_SCHEMA
        or begin.get("kind") != "personalization_turn_begin"
        or begin.get("preanswer_call_count") != 1
        or begin.get("answer_safe") is not True
        or begin.get("formal_write_count") != 0
        or begin.get("retrieval_status") != "unavailable"
        or begin.get("slice_id") is not None
    ):
        raise TurnReceiptError("legacy_begin_contract_invalid")
    entry_kind = begin.get("entry_kind")
    if (
        entry_kind not in ENTRY_TO_CONVERSATION
        or begin.get("conversation_kind") != ENTRY_TO_CONVERSATION[entry_kind]
    ):
        raise TurnReceiptError("legacy_begin_contract_invalid")
    request_id = begin.get("request_id")
    local_date = begin.get("local_date")
    degraded_reason = begin.get("degraded_reason")
    if (
        not isinstance(request_id, str)
        or not POINTER_KEY_RE.fullmatch(request_id)
        or not _is_valid_local_date(local_date)
        or not isinstance(degraded_reason, str)
        or not degraded_reason
        or len(degraded_reason) > 120
    ):
        raise TurnReceiptError("legacy_begin_contract_invalid")
    for key in ("request_sha256", "slice_sha256", "response_plan_sha256"):
        value = begin.get(key)
        if not isinstance(value, str) or not SHA_RE.fullmatch(value):
            raise TurnReceiptError("legacy_begin_contract_invalid")
    _validate_legacy_targets(begin.get("targets"))
    plan = begin.get("response_plan")
    expected_plan_keys = {
        "current_target",
        "direct_recent_errors",
        "direct_historical_errors",
        "common_patterns",
        "verified_cross_connections",
        "explanation_strategy",
        "first_boundary_to_check",
        "optional_practice_action",
        "evidence_gaps",
    }
    if (
        not isinstance(plan, dict)
        or set(plan) != expected_plan_keys
        or _sha256_bytes(_json_bytes(plan)) != begin.get("response_plan_sha256")
        or plan.get("current_target") != begin.get("targets")
        or plan.get("direct_recent_errors") != []
        or plan.get("direct_historical_errors") != []
        or plan.get("common_patterns") != []
        or plan.get("verified_cross_connections") != []
        or plan.get("explanation_strategy")
        != {
            "strategy": "degraded_generic_answer",
            "personalization_claim_allowed": False,
        }
        or plan.get("first_boundary_to_check") != {}
        or plan.get("optional_practice_action") != {"action": "none"}
        or not isinstance(plan.get("evidence_gaps"), list)
        or plan.get("evidence_gaps") != [degraded_reason]
    ):
        raise TurnReceiptError("legacy_begin_contract_invalid")
    _bound_root_manifest(begin)
    payload = dict(begin)
    turn_id = payload.pop("turn_id", None)
    expected_turn_id = "PTR-" + _sha256_bytes(_json_bytes(payload))[:24].upper()
    if turn_id != expected_turn_id:
        raise TurnReceiptError("legacy_begin_identity_invalid")


def _validate_pre_exact_target_legacy_begin_contract(
    begin: dict[str, Any],
) -> None:
    """Recognize only ready receipts made before exact target binding."""

    if (
        set(begin) != CURRENT_BEGIN_KEYS
        or begin.get("schema") != BEGIN_SCHEMA
        or begin.get("kind") != "personalization_turn_begin"
        or type(begin.get("preanswer_call_count")) is not int
        or begin.get("preanswer_call_count") != 1
        or begin.get("answer_safe") is not True
        or type(begin.get("formal_write_count")) is not int
        or begin.get("formal_write_count") != 0
        or begin.get("retrieval_status") != "ready"
        or begin.get("degraded_reason") is not None
    ):
        raise TurnReceiptError("legacy_begin_contract_invalid")
    entry_kind = begin.get("entry_kind")
    if (
        entry_kind not in ENTRY_TO_CONVERSATION
        or begin.get("conversation_kind") != ENTRY_TO_CONVERSATION[entry_kind]
    ):
        raise TurnReceiptError("legacy_begin_contract_invalid")
    request_id = begin.get("request_id")
    local_date = begin.get("local_date")
    if (
        not isinstance(request_id, str)
        or not POINTER_KEY_RE.fullmatch(request_id)
        or not _is_valid_local_date(local_date)
    ):
        raise TurnReceiptError("legacy_begin_contract_invalid")
    for key in ("request_sha256", "slice_sha256", "response_plan_sha256"):
        value = begin.get(key)
        if not isinstance(value, str) or not SHA_RE.fullmatch(value):
            raise TurnReceiptError("legacy_begin_contract_invalid")

    plan = begin.get("response_plan")
    snapshot = begin.get("slice_snapshot")
    if (
        not isinstance(plan, dict)
        or _sha256_bytes(_json_bytes(plan)) != begin.get("response_plan_sha256")
        or not isinstance(snapshot, dict)
        or set(snapshot) != READY_SLICE_KEYS
        or _sha256_bytes(_json_bytes(snapshot)) != begin.get("slice_sha256")
        or snapshot.get("schema") != PREANSWER_SCHEMA
        or snapshot.get("status") != "ready"
        or snapshot.get("request_id") != request_id
        or snapshot.get("as_of") != local_date
        or snapshot.get("conversation_kind") != begin.get("conversation_kind")
        or snapshot.get("read_only") is not True
        or type(snapshot.get("formal_write_count")) is not int
        or snapshot.get("formal_write_count") != 0
        or snapshot.get("scheduling_authority") != "none"
        or snapshot.get("mode") not in {"off", "shadow", "verified"}
        or not isinstance(snapshot.get("request_sha256"), str)
        or not SHA_RE.fullmatch(str(snapshot.get("request_sha256")))
        or not isinstance(snapshot.get("slice_id"), str)
        or not SAFE_ID_RE.fullmatch(str(snapshot.get("slice_id")))
        or snapshot.get("slice_id") != begin.get("slice_id")
        or not isinstance(snapshot.get("truncated"), bool)
        or type(snapshot.get("byte_count")) is not int
        or snapshot.get("byte_count", -1) < 0
    ):
        raise TurnReceiptError("legacy_begin_contract_invalid")
    context = snapshot.get("context")
    if (
        not isinstance(context, dict)
        or not isinstance(context.get("current_targets"), dict)
        or not isinstance(context.get("response_plan"), dict)
        or _json_bytes(plan) != _json_bytes(context.get("response_plan"))
    ):
        raise TurnReceiptError("legacy_begin_contract_invalid")

    targets = begin.get("targets")
    _validate_legacy_targets(targets)
    snapshot_targets = context["current_targets"]
    subjects = snapshot_targets.get("subjects")
    if (
        not isinstance(subjects, list)
        or any(
            not isinstance(subject, str) or subject not in {"DS", "CO", "OS", "CN"}
            for subject in subjects
        )
        or len(subjects) != len(set(subjects))
        or targets.get("subject") not in subjects
    ):
        raise TurnReceiptError("legacy_begin_contract_invalid")
    for key in (
        "point_ids",
        "mechanism_ids",
        "mechanism_primitive_ids",
        "formal_node_ids",
        "module_ids",
        "error_pattern_ids",
        "method_ids",
    ):
        if _json_bytes(targets.get(key)) != _json_bytes(snapshot_targets.get(key)):
            raise TurnReceiptError("legacy_begin_contract_invalid")
    if _json_bytes(targets) == _json_bytes(snapshot_targets):
        raise TurnReceiptError("legacy_begin_contract_invalid")

    if snapshot.get("projection_build_id") != begin.get("projection_build_id"):
        raise TurnReceiptError("legacy_begin_contract_invalid")
    layer_hashes = snapshot.get("layer_hashes")
    if not isinstance(layer_hashes, dict) or any(
        layer_hashes.get(layer_key) != begin.get(begin_key)
        for layer_key, begin_key in (
            ("recent_incremental_sha256", "recent_incremental_sha256"),
            ("historical_manifest_sha256", "historical_manifest_sha256"),
            ("historical_index_sha256", "historical_index_sha256"),
        )
    ):
        raise TurnReceiptError("legacy_begin_contract_invalid")
    _bound_root_manifest(begin)
    payload = dict(begin)
    turn_id = payload.pop("turn_id", None)
    expected_turn_id = "PTR-" + _sha256_bytes(_json_bytes(payload))[:24].upper()
    if turn_id != expected_turn_id:
        raise TurnReceiptError("legacy_begin_identity_invalid")


def _legacy_contract_id_for_begin(begin: dict[str, Any]) -> str:
    try:
        _validate_legacy_begin_contract(begin)
    except TurnReceiptError:
        _validate_pre_exact_target_legacy_begin_contract(begin)
        return PRE_EXACT_TARGET_LEGACY_CONTRACT_ID
    return LEGACY_CONTRACT_ID


def _failed_supported_begin_contracts(
    begin: dict[str, Any],
) -> list[dict[str, str]]:
    """Return deterministic failures for every supported begin contract.

    An aborted-begin seal is recovery evidence for an object that never met any
    supported begin contract.  If even one validator accepts the object, the
    recovery surface must fail closed rather than downgrade a usable receipt.
    """

    validators = (
        (CURRENT_CONTRACT_ID, _validate_begin_contract),
        (LEGACY_CONTRACT_ID, _validate_legacy_begin_contract),
        (
            PRE_EXACT_TARGET_LEGACY_CONTRACT_ID,
            _validate_pre_exact_target_legacy_begin_contract,
        ),
    )
    failures: list[dict[str, str]] = []
    for contract_id, validator in validators:
        try:
            validator(begin)
        except TurnReceiptError as exc:
            failures.append(
                {
                    "contract_id": contract_id,
                    "status": "fail",
                    "reason_code": str(exc),
                }
            )
        else:
            raise TurnReceiptError("invalid_begin_matches_supported_contract")
    return failures


def _pointer_value(
    path: Path, *, namespace: str, key: str, object_sha256: str
) -> dict[str, Any]:
    if not path.is_file() or path.is_symlink():
        raise TurnReceiptError("receipt_pointer_missing_or_invalid")
    value = _read_json(path)
    expected = {
        "schema": "personalization_receipt_pointer_v1",
        "namespace": namespace,
        "key": key,
        "object_sha256": object_sha256,
    }
    if value != expected:
        raise TurnReceiptError("receipt_pointer_missing_or_invalid")
    return value


def _validated_adoption(value: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(value, dict) or value.get("schema") != ADOPTION_SCHEMA:
        raise TurnReceiptError("invalid_response_adoption")
    keys = {
        "schema",
        "personalization_claimed",
        "used_evidence_ids",
        "used_match_ids",
        "actions",
        "degraded_reason",
    }
    if set(value) - keys:
        raise TurnReceiptError("invalid_response_adoption")
    claimed = value.get("personalization_claimed")
    if not isinstance(claimed, bool):
        raise TurnReceiptError("invalid_response_adoption")
    result: dict[str, Any] = {
        "schema": ADOPTION_SCHEMA,
        "personalization_claimed": claimed,
    }
    for key in ("used_evidence_ids", "used_match_ids", "actions"):
        values = value.get(key, [])
        if not isinstance(values, list) or len(values) > 12:
            raise TurnReceiptError("invalid_response_adoption")
        clean: list[str] = []
        for item in values:
            if not isinstance(item, str) or not SAFE_ID_RE.fullmatch(item):
                raise TurnReceiptError("invalid_response_adoption")
            if item not in clean:
                clean.append(item)
        result[key] = clean
    if any(action not in ALLOWED_ACTIONS for action in result["actions"]):
        raise TurnReceiptError("invalid_response_action")
    reason = value.get("degraded_reason")
    result["degraded_reason"] = (
        str(reason)[:120] if isinstance(reason, str) and reason else None
    )
    return result


def _default_adoption_for_begin(begin: dict[str, Any]) -> dict[str, Any]:
    """Return the least-privileged adoption that the begin receipt permits."""

    ready = begin.get("retrieval_status") == "ready"
    reason = begin.get("degraded_reason")
    return _validated_adoption(
        {
            "schema": ADOPTION_SCHEMA,
            "personalization_claimed": False,
            "used_evidence_ids": [],
            "used_match_ids": [],
            "actions": [] if ready else ["degraded_generic_answer"],
            "degraded_reason": (
                None
                if ready
                else (
                    reason
                    if isinstance(reason, str) and reason
                    else "personalization_unavailable"
                )
            ),
        }
    )


def finalize_personalized_reply(
    repo: str | Path,
    *,
    turn_receipt_sha256: str,
    reply_body: str | bytes,
    adoption: dict[str, Any] | None = None,
    state_dir: str | Path | None = None,
) -> dict[str, Any]:
    """Bind exact reply bytes without storing them.

    Omitting ``adoption`` records the least-privileged adoption permitted by
    the bound begin receipt and never claims that retrieved evidence was used.
    """

    _retired_write_surface()

    # Historical implementation retained below for read-only source compatibility.
    if not SHA_RE.fullmatch(turn_receipt_sha256):
        raise TurnReceiptError("invalid_turn_receipt_sha256")
    root_repo = Path(repo).resolve()
    state = _state_dir(root_repo, state_dir)
    receipt_root = _receipt_root(state)
    body = (
        reply_body.encode("utf-8")
        if isinstance(reply_body, str)
        else bytes(reply_body)
    )
    if not body:
        raise TurnReceiptError("empty_reply_body")
    reply_sha256 = _sha256_bytes(body)
    # The projection builder uses this same lock.  Holding it through the
    # manifest comparison and finalize pointer commit prevents a nightly root
    # manifest swap from authorizing a reply against a stale begin snapshot.
    with _lock(state / ".hot-path.lock", state=state):
        with _lock(receipt_root / ".turn-receipts.lock", state=state):
            begin_path = (
                receipt_root
                / "objects"
                / "begin"
                / f"{turn_receipt_sha256}.json"
            )
            _assert_managed_path(begin_path, state=state)
            if (
                not begin_path.is_file()
                or begin_path.is_symlink()
                or _sha256_file(begin_path) != turn_receipt_sha256
            ):
                raise TurnReceiptError("turn_receipt_missing_or_drifted")
            begin = _read_json(begin_path)
            snapshot = _validate_current_begin_contract(
                begin, receipt_root=receipt_root
            )
            response_plan = _response_plan_from_slice(
                snapshot, targets=dict(begin.get("targets") or {})
            )
            adopted = (
                _default_adoption_for_begin(begin)
                if adoption is None
                else _validated_adoption(adoption)
            )
            request_pointer_path = (
                receipt_root
                / "pointers"
                / "request"
                / f"{begin['request_id']}.json"
            )
            _assert_managed_path(request_pointer_path, state=state)
            _pointer_value(
                request_pointer_path,
                namespace="request",
                key=str(begin["request_id"]),
                object_sha256=turn_receipt_sha256,
            )
            if _root_manifest_binding(state) != _bound_root_manifest(begin):
                raise TurnReceiptError("root_manifest_changed_since_begin")
            _validate_adoption_against_begin(
                begin, adopted, response_plan=response_plan
            )

            payload_without_id = {
                "schema": FINALIZE_SCHEMA,
                "kind": "personalization_reply_binding",
                "turn_id": begin.get("turn_id"),
                "turn_receipt_sha256": turn_receipt_sha256,
                "request_sha256": begin.get("request_sha256"),
                "slice_sha256": begin.get("slice_sha256"),
                "response_plan_sha256": begin.get("response_plan_sha256"),
                "reply_sha256": reply_sha256,
                "reply_body_stored": False,
                "adoption": adopted,
                "finalized": True,
                "display_authorized": True,
                "answer_safe_metadata_only": True,
                "formal_write_count": 0,
            }
            binding_id = (
                "PRB-"
                + _sha256_bytes(_json_bytes(payload_without_id))[:24].upper()
            )
            payload = {**payload_without_id, "binding_id": binding_id}
            digest = _sha256_bytes(_json_bytes(payload, pretty=True))
            object_path = (
                receipt_root / "objects" / "finalize" / f"{digest}.json"
            )
            finalize_pointer = (
                receipt_root
                / "pointers"
                / "finalize"
                / f"{turn_receipt_sha256}.json"
            )
            _assert_managed_path(finalize_pointer, state=state)
            if finalize_pointer.exists():
                _pointer_value(
                    finalize_pointer,
                    namespace="finalize",
                    key=turn_receipt_sha256,
                    object_sha256=digest,
                )
                if (
                    not object_path.is_file()
                    or object_path.is_symlink()
                    or _sha256_file(object_path) != digest
                    or _read_json(object_path) != payload
                ):
                    raise TurnReceiptError("reply_binding_missing_or_drifted")
            else:
                written_digest, object_path = _write_content_object(
                    root=receipt_root, kind="finalize", payload=payload
                )
                if written_digest != digest:
                    raise TurnReceiptError("reply_binding_hash_mismatch")
                _bind_once(
                    root=receipt_root,
                    namespace="finalize",
                    key=turn_receipt_sha256,
                    object_sha256=digest,
                )
    return {
        "status": "finalized",
        "binding_id": binding_id,
        "reply_binding_ref": object_path.relative_to(state).as_posix(),
        "reply_binding_sha256": digest,
        "reply_sha256": reply_sha256,
        "display_authorized": True,
        "reply_body_stored": False,
        "formal_write_count": 0,
    }


def inspect_turn_receipt(
    repo: str | Path,
    *,
    turn_receipt_sha256: str,
    state_dir: str | Path | None = None,
) -> dict[str, Any]:
    """Read one exact receipt and its full slice without lookup or full audit."""

    if not SHA_RE.fullmatch(turn_receipt_sha256):
        raise TurnReceiptError("invalid_turn_receipt_sha256")
    repo_path = Path(repo).resolve()
    state = _state_dir(repo_path, state_dir)
    root = _receipt_root(state)
    begin_path = root / "objects" / "begin" / f"{turn_receipt_sha256}.json"
    _assert_managed_path(begin_path, state=state)
    if (
        not begin_path.is_file()
        or begin_path.is_symlink()
        or _sha256_file(begin_path) != turn_receipt_sha256
    ):
        raise TurnReceiptError("turn_receipt_missing_or_drifted")
    begin = _read_json(begin_path)
    try:
        snapshot = _validate_current_begin_contract(begin, receipt_root=root)
    except TurnReceiptError as current_exc:
        try:
            legacy_contract_id = _legacy_contract_id_for_begin(begin)
        except TurnReceiptError:
            raise current_exc
        response_plan = begin.get("response_plan")
        return {
            "schema": INSPECTION_SCHEMA,
            "status": "legacy_inspected",
            "legacy_contract_id": legacy_contract_id,
            "turn_receipt_ref": begin_path.relative_to(state).as_posix(),
            "turn_receipt_sha256": turn_receipt_sha256,
            "begin_receipt": begin,
            "slice": None,
            "runtime_plan": (
                response_plan if isinstance(response_plan, dict) else {}
            ),
            "preanswer_call_count": 0,
            "audit_call_count": 0,
            "formal_write_count": 0,
        }
    response_plan = _response_plan_from_slice(
        snapshot, targets=dict(begin.get("targets") or {})
    )
    return {
        "schema": INSPECTION_SCHEMA,
        "status": "inspected",
        "receipt_schema": begin.get("schema"),
        "turn_receipt_ref": begin_path.relative_to(state).as_posix(),
        "turn_receipt_sha256": turn_receipt_sha256,
        "begin_receipt": begin,
        "slice": snapshot,
        "runtime_plan": response_plan,
        "preanswer_call_count": 0,
        "audit_call_count": 0,
        "formal_write_count": 0,
    }


def _validate_finalize_contract(
    value: dict[str, Any],
    *,
    begin: dict[str, Any],
    begin_sha256: str,
    receipt_root: Path | None = None,
) -> None:
    if (
        value.get("schema") != FINALIZE_SCHEMA
        or value.get("kind") != "personalization_reply_binding"
        or value.get("reply_body_stored") is not False
        or value.get("finalized") is not True
        or value.get("display_authorized") is not True
        or value.get("answer_safe_metadata_only") is not True
        or value.get("formal_write_count") != 0
    ):
        raise TurnReceiptError("finalize_contract_invalid")
    reply_sha256 = value.get("reply_sha256")
    if not isinstance(reply_sha256, str) or not SHA_RE.fullmatch(reply_sha256):
        raise TurnReceiptError("finalize_contract_invalid")
    if value.get("turn_receipt_sha256") != begin_sha256:
        raise TurnReceiptError("finalize_begin_binding_invalid")
    for final_key, begin_key in (
        ("turn_id", "turn_id"),
        ("request_sha256", "request_sha256"),
        ("slice_sha256", "slice_sha256"),
        ("response_plan_sha256", "response_plan_sha256"),
    ):
        if value.get(final_key) != begin.get(begin_key):
            raise TurnReceiptError("finalize_begin_binding_invalid")
    adopted = _validated_adoption(value.get("adoption"))
    if adopted != value.get("adoption"):
        raise TurnReceiptError("finalize_adoption_not_canonical")
    if begin.get("schema") == BEGIN_SCHEMA_V2:
        if receipt_root is None:
            raise TurnReceiptError("slice_object_binding_invalid")
        snapshot = _validate_begin_contract_v2(
            begin, receipt_root=receipt_root
        )
        response_plan = _response_plan_from_slice(
            snapshot, targets=dict(begin.get("targets") or {})
        )
    else:
        response_plan = begin.get("response_plan")
    _validate_adoption_against_begin(
        begin,
        adopted,
        response_plan=(
            response_plan if isinstance(response_plan, dict) else None
        ),
    )
    payload = dict(value)
    binding_id = payload.pop("binding_id", None)
    expected_binding_id = "PRB-" + _sha256_bytes(_json_bytes(payload))[:24].upper()
    if binding_id != expected_binding_id:
        raise TurnReceiptError("finalize_identity_invalid")


def _validate_legacy_finalize_contract(
    value: dict[str, Any], *, begin: dict[str, Any], begin_sha256: str
) -> None:
    legacy_contract_id = _legacy_contract_id_for_begin(begin)
    if set(value) != LEGACY_FINALIZE_KEYS or "reply_body" in value:
        raise TurnReceiptError("legacy_finalize_contract_invalid")
    if (
        value.get("schema") != FINALIZE_SCHEMA
        or value.get("kind") != "personalization_reply_binding"
        or value.get("reply_body_stored") is not False
        or value.get("finalized") is not True
        or value.get("display_authorized") is not True
        or value.get("answer_safe_metadata_only") is not True
        or value.get("formal_write_count") != 0
    ):
        raise TurnReceiptError("legacy_finalize_contract_invalid")
    reply_sha256 = value.get("reply_sha256")
    if not isinstance(reply_sha256, str) or not SHA_RE.fullmatch(reply_sha256):
        raise TurnReceiptError("legacy_finalize_contract_invalid")
    if value.get("turn_receipt_sha256") != begin_sha256:
        raise TurnReceiptError("legacy_finalize_begin_binding_invalid")
    for final_key, begin_key in (
        ("turn_id", "turn_id"),
        ("request_sha256", "request_sha256"),
        ("slice_sha256", "slice_sha256"),
        ("response_plan_sha256", "response_plan_sha256"),
    ):
        if value.get(final_key) != begin.get(begin_key):
            raise TurnReceiptError("legacy_finalize_begin_binding_invalid")
    adopted = _validated_adoption(value.get("adoption"))
    if (
        adopted != value.get("adoption")
        or adopted.get("personalization_claimed") is not False
        or adopted.get("used_evidence_ids") != []
        or adopted.get("used_match_ids") != []
    ):
        raise TurnReceiptError("legacy_finalize_adoption_invalid")
    if legacy_contract_id == LEGACY_CONTRACT_ID and (
        adopted.get("actions") != ["degraded_generic_answer"]
        or not adopted.get("degraded_reason")
    ):
        raise TurnReceiptError("legacy_finalize_adoption_invalid")
    if legacy_contract_id == PRE_EXACT_TARGET_LEGACY_CONTRACT_ID and (
        adopted.get("actions") != ["direct_answer"]
        or adopted.get("degraded_reason") is not None
    ):
        raise TurnReceiptError("legacy_finalize_adoption_invalid")
    payload = dict(value)
    binding_id = payload.pop("binding_id", None)
    expected_binding_id = "PRB-" + _sha256_bytes(_json_bytes(payload))[:24].upper()
    if binding_id != expected_binding_id:
        raise TurnReceiptError("legacy_finalize_identity_invalid")


def _legacy_seal_payload(
    *,
    begin: dict[str, Any],
    begin_sha256: str,
    request_pointer_sha256: str,
    finalize: dict[str, Any],
    finalize_sha256: str,
    finalize_pointer_sha256: str,
) -> dict[str, Any]:
    legacy_contract_id = _legacy_contract_id_for_begin(begin)
    pre_exact_target = legacy_contract_id == PRE_EXACT_TARGET_LEGACY_CONTRACT_ID
    return {
        "schema": LEGACY_SEAL_SCHEMA,
        "kind": "personalization_legacy_turn_seal",
        "legacy_contract_id": legacy_contract_id,
        "begin_receipt_sha256": begin_sha256,
        "request_id": begin["request_id"],
        "request_pointer_sha256": request_pointer_sha256,
        "finalize_binding_sha256": finalize_sha256,
        "finalize_pointer_sha256": finalize_pointer_sha256,
        "turn_id": begin["turn_id"],
        "request_sha256": begin["request_sha256"],
        "slice_sha256": begin["slice_sha256"],
        "response_plan_sha256": begin["response_plan_sha256"],
        "reply_sha256": finalize["reply_sha256"],
        "legacy_retrieval_status": "ready" if pre_exact_target else "unavailable",
        "slice_snapshot_recorded": pre_exact_target,
        "personalization_claimed": False,
        "adopted_actions": ["direct_answer"] if pre_exact_target else ["degraded_generic_answer"],
        "reply_body_stored": False,
        "answer_safe_metadata_only": True,
        "current_contract_valid": False,
        "seal_scope": "audit_compatibility_only",
        "reauthorizes_display": False,
        "formal_write_count": 0,
    }


def _validate_legacy_seal_contract(
    value: dict[str, Any],
    *,
    begin: dict[str, Any],
    begin_sha256: str,
    request_pointer_sha256: str,
    finalize: dict[str, Any],
    finalize_sha256: str,
    finalize_pointer_sha256: str,
) -> None:
    expected_without_id = _legacy_seal_payload(
        begin=begin,
        begin_sha256=begin_sha256,
        request_pointer_sha256=request_pointer_sha256,
        finalize=finalize,
        finalize_sha256=finalize_sha256,
        finalize_pointer_sha256=finalize_pointer_sha256,
    )
    seal_id = value.get("seal_id")
    if set(value) != set(expected_without_id) | {"seal_id"}:
        raise TurnReceiptError("legacy_seal_contract_invalid")
    if value != {
        **expected_without_id,
        "seal_id": "PLS-"
        + _sha256_bytes(_json_bytes(expected_without_id))[:24].upper(),
    }:
        raise TurnReceiptError("legacy_seal_binding_invalid")
    if not isinstance(seal_id, str):
        raise TurnReceiptError("legacy_seal_identity_invalid")


def seal_legacy_turn(
    repo: str | Path,
    *,
    turn_receipt_sha256: str,
    state_dir: str | Path | None = None,
) -> dict[str, Any]:
    """Seal one exact pre-snapshot begin/finalize pair without rewriting it."""

    _retired_write_surface()

    # Historical implementation retained below for read-only source compatibility.
    if not SHA_RE.fullmatch(turn_receipt_sha256):
        raise TurnReceiptError("invalid_turn_receipt_sha256")
    repo_path = Path(repo).resolve()
    state = _state_dir(repo_path, state_dir)
    root = _receipt_root(state)
    with _lock(root / ".turn-receipts.lock", state=state):
        begin_path = root / "objects" / "begin" / f"{turn_receipt_sha256}.json"
        _assert_managed_path(begin_path, state=state)
        if (
            not begin_path.is_file()
            or begin_path.is_symlink()
            or _sha256_file(begin_path) != turn_receipt_sha256
        ):
            raise TurnReceiptError("legacy_begin_missing_or_drifted")
        begin = _read_json(begin_path)
        legacy_contract_id = _legacy_contract_id_for_begin(begin)

        request_pointer = (
            root / "pointers" / "request" / f"{begin['request_id']}.json"
        )
        _assert_managed_path(request_pointer, state=state)
        _pointer_value(
            request_pointer,
            namespace="request",
            key=str(begin["request_id"]),
            object_sha256=turn_receipt_sha256,
        )
        request_pointer_sha256 = _sha256_file(request_pointer)

        finalize_pointer = (
            root / "pointers" / "finalize" / f"{turn_receipt_sha256}.json"
        )
        _assert_managed_path(finalize_pointer, state=state)
        if not finalize_pointer.is_file() or finalize_pointer.is_symlink():
            raise TurnReceiptError("legacy_finalize_pointer_missing_or_invalid")
        finalize_pointer_value = _read_json(finalize_pointer)
        finalize_sha256 = finalize_pointer_value.get("object_sha256")
        if not isinstance(finalize_sha256, str) or not SHA_RE.fullmatch(finalize_sha256):
            raise TurnReceiptError("legacy_finalize_pointer_missing_or_invalid")
        _pointer_value(
            finalize_pointer,
            namespace="finalize",
            key=turn_receipt_sha256,
            object_sha256=finalize_sha256,
        )
        finalize_pointer_sha256 = _sha256_file(finalize_pointer)
        finalize_path = root / "objects" / "finalize" / f"{finalize_sha256}.json"
        _assert_managed_path(finalize_path, state=state)
        if (
            not finalize_path.is_file()
            or finalize_path.is_symlink()
            or _sha256_file(finalize_path) != finalize_sha256
        ):
            raise TurnReceiptError("legacy_finalize_missing_or_drifted")
        finalize = _read_json(finalize_path)
        _validate_legacy_finalize_contract(
            finalize, begin=begin, begin_sha256=turn_receipt_sha256
        )

        payload_without_id = _legacy_seal_payload(
            begin=begin,
            begin_sha256=turn_receipt_sha256,
            request_pointer_sha256=request_pointer_sha256,
            finalize=finalize,
            finalize_sha256=finalize_sha256,
            finalize_pointer_sha256=finalize_pointer_sha256,
        )
        seal_id = (
            "PLS-"
            + _sha256_bytes(_json_bytes(payload_without_id))[:24].upper()
        )
        payload = {**payload_without_id, "seal_id": seal_id}
        seal_sha256 = _sha256_bytes(_json_bytes(payload, pretty=True))
        object_path = root / "objects" / "legacy-seal" / f"{seal_sha256}.json"
        pointer_path = (
            root
            / "pointers"
            / LEGACY_SEAL_NAMESPACE
            / f"{turn_receipt_sha256}.json"
        )
        _assert_managed_path(object_path, state=state)
        _assert_managed_path(pointer_path, state=state)
        existed = pointer_path.exists()
        if existed:
            _pointer_value(
                pointer_path,
                namespace=LEGACY_SEAL_NAMESPACE,
                key=turn_receipt_sha256,
                object_sha256=seal_sha256,
            )
            if (
                not object_path.is_file()
                or object_path.is_symlink()
                or _sha256_file(object_path) != seal_sha256
                or _read_json(object_path) != payload
            ):
                raise TurnReceiptError("legacy_seal_missing_or_drifted")
        else:
            written_sha256, object_path = _write_content_object(
                root=root, kind="legacy-seal", payload=payload
            )
            if written_sha256 != seal_sha256:
                raise TurnReceiptError("legacy_seal_hash_mismatch")
            _bind_once(
                root=root,
                namespace=LEGACY_SEAL_NAMESPACE,
                key=turn_receipt_sha256,
                object_sha256=seal_sha256,
            )
    return {
        "status": "already_legacy_sealed" if existed else "legacy_sealed",
        "legacy_contract_id": legacy_contract_id,
        "turn_receipt_sha256": turn_receipt_sha256,
        "finalize_binding_sha256": finalize_sha256,
        "legacy_seal_ref": object_path.relative_to(state).as_posix(),
        "legacy_seal_sha256": seal_sha256,
        "current_contract_valid": False,
        "reauthorizes_display": False,
        "formal_write_count": 0,
    }


def _require_no_finalize_for_begin(
    *, root: Path, state: Path, begin_sha256: str
) -> None:
    """Prove that no finalize pointer or object exists for one begin digest."""

    pointer = root / "pointers" / "finalize" / f"{begin_sha256}.json"
    _assert_managed_path(pointer, state=state)
    if pointer.exists() or pointer.is_symlink():
        raise TurnReceiptError("invalid_begin_finalize_pointer_exists")

    finalize_dir = root / "objects" / "finalize"
    _assert_managed_path(finalize_dir, state=state)
    for path in sorted(finalize_dir.glob("*.json")):
        _assert_managed_path(path, state=state)
        if path.is_symlink() or not path.is_file():
            raise TurnReceiptError("invalid_begin_finalize_scan_untrusted")
        if not SHA_RE.fullmatch(path.stem) or _sha256_file(path) != path.stem:
            raise TurnReceiptError("invalid_begin_finalize_scan_untrusted")
        value = _read_json(path)
        if value.get("turn_receipt_sha256") == begin_sha256:
            raise TurnReceiptError("invalid_begin_finalize_object_exists")


def _aborted_begin_seal_payload(
    *,
    begin: dict[str, Any],
    begin_sha256: str,
    request_pointer_sha256: str,
    failed_contract_checks: list[dict[str, str]],
) -> dict[str, Any]:
    return {
        "schema": ABORTED_BEGIN_SEAL_SCHEMA,
        "kind": "personalization_invalid_begin_abort_seal",
        "begin_receipt_sha256": begin_sha256,
        "request_id": begin["request_id"],
        "request_pointer_sha256": request_pointer_sha256,
        "turn_id": begin.get("turn_id"),
        "failed_contract_checks": failed_contract_checks,
        "all_supported_begin_contracts_failed": True,
        "no_finalize_pointer": True,
        "no_finalize_object": True,
        "operator_confirmed_not_displayed": True,
        "reply_displayed": False,
        "display_nonoccurrence_basis": "operator_explicit_confirmation",
        "finalized": False,
        "reason": "invalid_begin_contract",
        "preservation_scope": "audit_aborted_attempt_only",
        "answer_safe_metadata_only": True,
        "current_contract_valid": False,
        "reauthorizes_display": False,
        "formal_write_count": 0,
    }


def _validate_aborted_begin_seal_contract(
    value: dict[str, Any],
    *,
    begin: dict[str, Any],
    begin_sha256: str,
    request_pointer_sha256: str,
) -> None:
    failed_contract_checks = _failed_supported_begin_contracts(begin)
    expected_without_id = _aborted_begin_seal_payload(
        begin=begin,
        begin_sha256=begin_sha256,
        request_pointer_sha256=request_pointer_sha256,
        failed_contract_checks=failed_contract_checks,
    )
    if set(value) != set(expected_without_id) | {"seal_id"}:
        raise TurnReceiptError("aborted_begin_seal_contract_invalid")
    expected = {
        **expected_without_id,
        "seal_id": "PABS-"
        + _sha256_bytes(_json_bytes(expected_without_id))[:24].upper(),
    }
    if value != expected:
        raise TurnReceiptError("aborted_begin_seal_binding_invalid")


def seal_invalid_begin_aborted(
    repo: str | Path,
    *,
    turn_receipt_sha256: str,
    reply_was_not_displayed: bool,
    state_dir: str | Path | None = None,
) -> dict[str, Any]:
    """Preserve one invalid, undisplayed begin as an aborted attempt.

    The operation adds only a content-addressed recovery seal and pointer.  It
    never rewrites the original begin or request pointer and never fabricates a
    finalize binding.
    """

    _retired_write_surface()

    # Historical implementation retained below for read-only source compatibility.
    if not SHA_RE.fullmatch(turn_receipt_sha256):
        raise TurnReceiptError("invalid_turn_receipt_sha256")
    if reply_was_not_displayed is not True:
        raise TurnReceiptError("invalid_begin_requires_not_displayed_confirmation")
    repo_path = Path(repo).resolve()
    state = _state_dir(repo_path, state_dir)
    root = _receipt_root(state)
    with _lock(root / ".turn-receipts.lock", state=state):
        begin_path = root / "objects" / "begin" / f"{turn_receipt_sha256}.json"
        _assert_managed_path(begin_path, state=state)
        if (
            not begin_path.is_file()
            or begin_path.is_symlink()
            or _sha256_file(begin_path) != turn_receipt_sha256
        ):
            raise TurnReceiptError("invalid_begin_missing_or_drifted")
        begin = _read_json(begin_path)
        if (
            begin.get("schema") != BEGIN_SCHEMA
            or begin.get("kind") != "personalization_turn_begin"
            or begin.get("answer_safe") is not True
            or type(begin.get("preanswer_call_count")) is not int
            or begin.get("preanswer_call_count") != 1
            or type(begin.get("formal_write_count")) is not int
            or begin.get("formal_write_count") != 0
        ):
            raise TurnReceiptError("invalid_begin_not_safe_recovery_candidate")
        request_id = begin.get("request_id")
        if not isinstance(request_id, str) or not POINTER_KEY_RE.fullmatch(request_id):
            raise TurnReceiptError("invalid_begin_not_safe_recovery_candidate")
        failed_contract_checks = _failed_supported_begin_contracts(begin)

        request_pointer = root / "pointers" / "request" / f"{request_id}.json"
        _assert_managed_path(request_pointer, state=state)
        _pointer_value(
            request_pointer,
            namespace="request",
            key=request_id,
            object_sha256=turn_receipt_sha256,
        )
        request_pointer_sha256 = _sha256_file(request_pointer)
        _require_no_finalize_for_begin(
            root=root, state=state, begin_sha256=turn_receipt_sha256
        )

        payload_without_id = _aborted_begin_seal_payload(
            begin=begin,
            begin_sha256=turn_receipt_sha256,
            request_pointer_sha256=request_pointer_sha256,
            failed_contract_checks=failed_contract_checks,
        )
        payload = {
            **payload_without_id,
            "seal_id": "PABS-"
            + _sha256_bytes(_json_bytes(payload_without_id))[:24].upper(),
        }
        seal_sha256 = _sha256_bytes(_json_bytes(payload, pretty=True))
        object_path = (
            root / "objects" / "aborted-begin-seal" / f"{seal_sha256}.json"
        )
        pointer_path = (
            root
            / "pointers"
            / ABORTED_BEGIN_SEAL_NAMESPACE
            / f"{turn_receipt_sha256}.json"
        )
        _assert_managed_path(object_path, state=state)
        _assert_managed_path(pointer_path, state=state)
        existed = pointer_path.exists() or pointer_path.is_symlink()
        if existed:
            _pointer_value(
                pointer_path,
                namespace=ABORTED_BEGIN_SEAL_NAMESPACE,
                key=turn_receipt_sha256,
                object_sha256=seal_sha256,
            )
            if (
                not object_path.is_file()
                or object_path.is_symlink()
                or _sha256_file(object_path) != seal_sha256
                or _read_json(object_path) != payload
            ):
                raise TurnReceiptError("aborted_begin_seal_missing_or_drifted")
        else:
            written_sha256, object_path = _write_content_object(
                root=root, kind="aborted-begin-seal", payload=payload
            )
            if written_sha256 != seal_sha256:
                raise TurnReceiptError("aborted_begin_seal_hash_mismatch")
            _bind_once(
                root=root,
                namespace=ABORTED_BEGIN_SEAL_NAMESPACE,
                key=turn_receipt_sha256,
                object_sha256=seal_sha256,
            )
    return {
        "status": (
            "already_aborted_begin_sealed" if existed else "aborted_begin_sealed"
        ),
        "turn_receipt_sha256": turn_receipt_sha256,
        "aborted_begin_seal_ref": object_path.relative_to(state).as_posix(),
        "aborted_begin_seal_sha256": seal_sha256,
        "finalized": False,
        "reauthorizes_display": False,
        "reason": "invalid_begin_contract",
        "formal_write_count": 0,
    }


def _audit_read_object(
    path: Path, *, label: str, errors: list[str]
) -> dict[str, Any] | None:
    if path.is_symlink():
        errors.append(f"{label}_symlink:{path.name}")
        return None
    if not SHA_RE.fullmatch(path.stem):
        errors.append(f"{label}_filename:{path.name}")
        return None
    try:
        actual = _sha256_file(path)
    except OSError:
        errors.append(f"{label}_unreadable:{path.name}")
        return None
    if actual != path.stem:
        errors.append(f"{label}_hash:{path.name}")
        return None
    try:
        return _read_json(path)
    except TurnReceiptError:
        errors.append(f"{label}_json:{path.name}")
        return None


def _audit_pointer_directory(
    root: Path, *, namespace: str, errors: list[str]
) -> dict[str, str]:
    pointers: dict[str, str] = {}
    directory = root / "pointers" / namespace
    for path in sorted(directory.glob("*.json")):
        if path.is_symlink():
            errors.append(f"{namespace}_pointer_symlink:{path.name}")
            continue
        try:
            value = _read_json(path)
        except TurnReceiptError:
            errors.append(f"{namespace}_pointer_json:{path.name}")
            continue
        key = value.get("key")
        digest = value.get("object_sha256")
        expected_keys = {"schema", "namespace", "key", "object_sha256"}
        key_valid = isinstance(key, str) and (
            POINTER_KEY_RE.fullmatch(key)
            if namespace == "request"
            else SHA_RE.fullmatch(key)
        )
        if (
            set(value) != expected_keys
            or value.get("schema") != "personalization_receipt_pointer_v1"
            or value.get("namespace") != namespace
            or not key_valid
            or path.stem != key
            or not isinstance(digest, str)
            or not SHA_RE.fullmatch(digest)
        ):
            errors.append(f"{namespace}_pointer_contract:{path.name}")
            continue
        pointers[str(key)] = str(digest)
    return pointers


def audit_receipts(
    repo: str | Path, *, state_dir: str | Path | None = None
) -> dict[str, Any]:
    repo_path = Path(repo).resolve()
    state = _state_dir(repo_path, state_dir)
    root = _receipt_root(state)
    for directory in (
        root / "objects" / "begin",
        root / "objects" / "slice",
        root / "objects" / "finalize",
        root / "objects" / "legacy-seal",
        root / "objects" / "aborted-begin-seal",
        root / "pointers" / "request",
        root / "pointers" / "finalize",
        root / "pointers" / LEGACY_SEAL_NAMESPACE,
        root / "pointers" / ABORTED_BEGIN_SEAL_NAMESPACE,
    ):
        _assert_managed_path(directory, state=state)
    errors: list[str] = []
    begin_objects: dict[str, dict[str, Any]] = {}
    slice_objects: dict[str, dict[str, Any]] = {}
    finalize_objects: dict[str, dict[str, Any]] = {}
    legacy_seal_objects: dict[str, dict[str, Any]] = {}
    aborted_begin_seal_objects: dict[str, dict[str, Any]] = {}
    current_contract_begins: set[str] = set()
    current_v1_begins: set[str] = set()
    current_v2_begins: set[str] = set()
    referenced_slices: set[str] = set()
    legacy_candidates: set[str] = set()
    unsupported_invalid_begins: dict[str, TurnReceiptError] = {}
    current_contract_finalize_objects: set[str] = set()
    legacy_finalize_objects: set[str] = set()
    degraded_count = 0
    observed_begin_objects = {
        path.stem
        for path in (root / "objects" / "begin").glob("*.json")
        if path.is_file() or path.is_symlink()
    }
    for path in sorted((root / "objects" / "begin").glob("*.json")):
        value = _audit_read_object(path, label="begin", errors=errors)
        if value is None:
            continue
        begin_objects[path.stem] = value
        try:
            _validate_current_begin_contract(value, receipt_root=root)
        except TurnReceiptError as current_exc:
            try:
                _legacy_contract_id_for_begin(value)
            except TurnReceiptError:
                unsupported_invalid_begins[path.stem] = current_exc
            else:
                legacy_candidates.add(path.stem)
        else:
            current_contract_begins.add(path.stem)
            if value.get("schema") == BEGIN_SCHEMA_V2:
                current_v2_begins.add(path.stem)
                slice_digest = value.get("slice_object_sha256")
                if isinstance(slice_digest, str):
                    referenced_slices.add(slice_digest)
            else:
                current_v1_begins.add(path.stem)
        if value.get("retrieval_status") != "ready":
            degraded_count += 1
    for path in sorted((root / "objects" / "slice").glob("*.json")):
        value = _audit_read_object(path, label="slice", errors=errors)
        if value is not None:
            slice_objects[path.stem] = value
    for digest in sorted(referenced_slices - set(slice_objects)):
        errors.append(f"slice_object_missing:{digest}")
    for digest in sorted(set(slice_objects) - referenced_slices):
        errors.append(f"slice_object_orphan:{digest}")
    for path in sorted((root / "objects" / "finalize").glob("*.json")):
        value = _audit_read_object(path, label="finalize", errors=errors)
        if value is None:
            continue
        finalize_objects[path.stem] = value
    for path in sorted((root / "objects" / "legacy-seal").glob("*.json")):
        value = _audit_read_object(path, label="legacy_seal", errors=errors)
        if value is None:
            continue
        legacy_seal_objects[path.stem] = value
    for path in sorted((root / "objects" / "aborted-begin-seal").glob("*.json")):
        value = _audit_read_object(path, label="aborted_begin_seal", errors=errors)
        if value is None:
            continue
        aborted_begin_seal_objects[path.stem] = value

    request_pointers = _audit_pointer_directory(
        root, namespace="request", errors=errors
    )
    finalize_pointers = _audit_pointer_directory(
        root, namespace="finalize", errors=errors
    )
    legacy_seal_pointers = _audit_pointer_directory(
        root, namespace=LEGACY_SEAL_NAMESPACE, errors=errors
    )
    aborted_begin_seal_pointers = _audit_pointer_directory(
        root, namespace=ABORTED_BEGIN_SEAL_NAMESPACE, errors=errors
    )

    referenced_begins: set[str] = set()
    for request_id, digest in request_pointers.items():
        begin = begin_objects.get(digest)
        if begin is None:
            errors.append(f"request_pointer_dangling:{request_id}")
            continue
        if begin.get("request_id") != request_id:
            errors.append(f"request_pointer_binding:{request_id}")
            continue
        referenced_begins.add(digest)
    for digest, begin in begin_objects.items():
        request_id = begin.get("request_id")
        if not isinstance(request_id, str) or request_pointers.get(request_id) != digest:
            errors.append(f"begin_orphan:{digest}")

    for digest, final in finalize_objects.items():
        begin_digest = final.get("turn_receipt_sha256")
        begin = begin_objects.get(begin_digest) if isinstance(begin_digest, str) else None
        if begin is None:
            errors.append(f"finalize_missing_begin:{digest}")
            continue
        try:
            if begin_digest in current_contract_begins:
                _validate_finalize_contract(
                    final,
                    begin=begin,
                    begin_sha256=begin_digest,
                    receipt_root=root,
                )
            elif begin_digest in legacy_candidates:
                _validate_legacy_finalize_contract(
                    final, begin=begin, begin_sha256=begin_digest
                )
            else:
                raise TurnReceiptError("bound_begin_invalid")
        except TurnReceiptError as exc:
            errors.append(f"finalize_contract:{digest}.json:{exc}")
        else:
            if begin_digest in current_contract_begins:
                current_contract_finalize_objects.add(digest)
            else:
                legacy_finalize_objects.add(digest)

    referenced_finalizes: set[str] = set()
    current_contract_finalized_begins: set[str] = set()
    legacy_finalized_begins: set[str] = set()
    for begin_digest, finalize_digest in finalize_pointers.items():
        begin = begin_objects.get(begin_digest)
        final = finalize_objects.get(finalize_digest)
        if begin is None:
            errors.append(f"finalize_pointer_missing_begin:{begin_digest}")
            continue
        if final is None:
            errors.append(f"finalize_pointer_dangling:{begin_digest}")
            continue
        if final.get("turn_receipt_sha256") != begin_digest:
            errors.append(f"finalize_pointer_binding:{begin_digest}")
            continue
        referenced_finalizes.add(finalize_digest)
        if finalize_digest in current_contract_finalize_objects:
            current_contract_finalized_begins.add(begin_digest)
        elif finalize_digest in legacy_finalize_objects:
            legacy_finalized_begins.add(begin_digest)

    for digest, final in finalize_objects.items():
        if digest not in referenced_finalizes:
            errors.append(f"finalize_orphan:{digest}")

    valid_legacy_seal_objects: set[str] = set()
    for digest, seal in legacy_seal_objects.items():
        begin_digest = seal.get("begin_receipt_sha256")
        finalize_digest = seal.get("finalize_binding_sha256")
        begin = begin_objects.get(begin_digest) if isinstance(begin_digest, str) else None
        final = (
            finalize_objects.get(finalize_digest)
            if isinstance(finalize_digest, str)
            else None
        )
        if begin is None:
            errors.append(f"legacy_seal_missing_begin:{digest}")
            continue
        if begin_digest not in legacy_candidates:
            errors.append(f"legacy_seal_begin_not_legacy:{digest}")
            continue
        if final is None:
            errors.append(f"legacy_seal_missing_finalize:{digest}")
            continue
        try:
            request_id = begin.get("request_id")
            if not isinstance(request_id, str) or request_pointers.get(request_id) != begin_digest:
                raise TurnReceiptError("legacy_seal_request_pointer_invalid")
            if finalize_pointers.get(begin_digest) != finalize_digest:
                raise TurnReceiptError("legacy_seal_finalize_pointer_invalid")
            request_pointer_path = (
                root / "pointers" / "request" / f"{request_id}.json"
            )
            finalize_pointer_path = (
                root / "pointers" / "finalize" / f"{begin_digest}.json"
            )
            _validate_legacy_finalize_contract(
                final, begin=begin, begin_sha256=begin_digest
            )
            _validate_legacy_seal_contract(
                seal,
                begin=begin,
                begin_sha256=begin_digest,
                request_pointer_sha256=_sha256_file(request_pointer_path),
                finalize=final,
                finalize_sha256=finalize_digest,
                finalize_pointer_sha256=_sha256_file(finalize_pointer_path),
            )
        except (TurnReceiptError, OSError) as exc:
            errors.append(f"legacy_seal_contract:{digest}.json:{exc}")
        else:
            valid_legacy_seal_objects.add(digest)

    referenced_legacy_seals: set[str] = set()
    legacy_sealed_begins: set[str] = set()
    legacy_sealed_finalizes: set[str] = set()
    for begin_digest, seal_digest in legacy_seal_pointers.items():
        begin = begin_objects.get(begin_digest)
        seal = legacy_seal_objects.get(seal_digest)
        if begin is None:
            errors.append(f"legacy_seal_pointer_missing_begin:{begin_digest}")
            continue
        if seal is None:
            errors.append(f"legacy_seal_pointer_dangling:{begin_digest}")
            continue
        if seal.get("begin_receipt_sha256") != begin_digest:
            errors.append(f"legacy_seal_pointer_binding:{begin_digest}")
            continue
        referenced_legacy_seals.add(seal_digest)
        if seal_digest in valid_legacy_seal_objects:
            legacy_sealed_begins.add(begin_digest)
            finalize_digest = seal.get("finalize_binding_sha256")
            if isinstance(finalize_digest, str):
                legacy_sealed_finalizes.add(finalize_digest)

    for digest in legacy_seal_objects:
        if digest not in referenced_legacy_seals:
            errors.append(f"legacy_seal_orphan:{digest}")

    valid_aborted_begin_seal_objects: set[str] = set()
    for digest, seal in aborted_begin_seal_objects.items():
        begin_digest = seal.get("begin_receipt_sha256")
        begin = (
            begin_objects.get(begin_digest)
            if isinstance(begin_digest, str)
            else None
        )
        if begin is None:
            errors.append(f"aborted_begin_seal_missing_begin:{digest}")
            continue
        if begin_digest not in unsupported_invalid_begins:
            errors.append(f"aborted_begin_seal_begin_not_invalid:{digest}")
            continue
        try:
            request_id = begin.get("request_id")
            if (
                not isinstance(request_id, str)
                or request_pointers.get(request_id) != begin_digest
            ):
                raise TurnReceiptError("aborted_begin_seal_request_pointer_invalid")
            request_pointer_path = (
                root / "pointers" / "request" / f"{request_id}.json"
            )
            _require_no_finalize_for_begin(
                root=root, state=state, begin_sha256=begin_digest
            )
            _validate_aborted_begin_seal_contract(
                seal,
                begin=begin,
                begin_sha256=begin_digest,
                request_pointer_sha256=_sha256_file(request_pointer_path),
            )
        except (TurnReceiptError, OSError) as exc:
            errors.append(f"aborted_begin_seal_contract:{digest}.json:{exc}")
        else:
            valid_aborted_begin_seal_objects.add(digest)

    referenced_aborted_begin_seals: set[str] = set()
    preserved_aborted_begins: set[str] = set()
    for begin_digest, seal_digest in aborted_begin_seal_pointers.items():
        begin = begin_objects.get(begin_digest)
        seal = aborted_begin_seal_objects.get(seal_digest)
        if begin is None:
            errors.append(
                f"aborted_begin_seal_pointer_missing_begin:{begin_digest}"
            )
            continue
        if seal is None:
            errors.append(f"aborted_begin_seal_pointer_dangling:{begin_digest}")
            continue
        if seal.get("begin_receipt_sha256") != begin_digest:
            errors.append(f"aborted_begin_seal_pointer_binding:{begin_digest}")
            continue
        referenced_aborted_begin_seals.add(seal_digest)
        if seal_digest in valid_aborted_begin_seal_objects:
            preserved_aborted_begins.add(begin_digest)

    for digest in aborted_begin_seal_objects:
        if digest not in referenced_aborted_begin_seals:
            errors.append(f"aborted_begin_seal_orphan:{digest}")

    for digest, current_exc in unsupported_invalid_begins.items():
        if digest not in preserved_aborted_begins:
            errors.append(f"begin_contract:{digest}.json:{current_exc}")

    legacy_unsealed = legacy_candidates - legacy_sealed_begins
    for digest in sorted(legacy_candidates - legacy_finalized_begins):
        errors.append(f"legacy_finalize_missing:{digest}")
    for digest in sorted(legacy_unsealed):
        errors.append(f"legacy_begin_unsealed:{digest}")

    # Completion and integrity are orthogonal.  A strict legacy pair closes
    # completion through its compatibility seal, while an exact invalid-begin
    # seal resolves the attempt as aborted without fabricating a finalize.
    closed_begins = current_contract_finalized_begins | legacy_sealed_begins
    resolved_begins = closed_begins | preserved_aborted_begins
    pending = sorted(observed_begin_objects - resolved_begins)
    current_contract_pending = sorted(
        current_contract_begins - current_contract_finalized_begins
    )
    unresolved_invalid = (
        observed_begin_objects
        - current_contract_begins
        - legacy_sealed_begins
        - preserved_aborted_begins
    )
    errors = sorted(set(errors))
    integrity_status = "pass" if not errors else "fail"
    completion_status = "complete" if not pending else "incomplete"
    if errors:
        status = "fail"
    elif pending:
        status = "incomplete"
    else:
        status = "pass"
    return {
        "schema": AUDIT_SCHEMA,
        "status": status,
        "integrity_status": integrity_status,
        "completion_status": completion_status,
        "all_begun_turns_finalized": not pending,
        "all_begun_attempts_resolved": not pending,
        "begin_receipt_count": len(begin_objects),
        "begin_object_count": len(observed_begin_objects),
        "slice_object_count": len(slice_objects),
        "referenced_slice_object_count": len(referenced_slices),
        "orphan_slice_object_count": len(set(slice_objects) - referenced_slices),
        "request_pointer_count": len(request_pointers),
        "finalize_object_count": len(finalize_objects),
        "finalize_pointer_count": len(finalize_pointers),
        "finalized_reply_count": len(closed_begins),
        "unfinalized_begin_count": len(pending),
        "unfinalized_begin_receipts": pending[:50],
        "invalid_begin_count": len(unresolved_invalid),
        "orphan_begin_count": len(observed_begin_objects - referenced_begins),
        "orphan_finalize_count": len(set(finalize_objects) - referenced_finalizes),
        "current_contract_valid_begin_count": len(current_contract_begins),
        "current_v1_valid_begin_count": len(current_v1_begins),
        "current_v2_valid_begin_count": len(current_v2_begins),
        "current_contract_finalized_count": len(current_contract_finalized_begins),
        "current_contract_unfinalized_count": len(current_contract_pending),
        "current_contract_unfinalized_begin_receipts": current_contract_pending[:50],
        "current_contract_invalid_begin_count": len(
            observed_begin_objects - current_contract_begins
        ),
        "current_contract_invalid_begin_receipts": sorted(
            observed_begin_objects - current_contract_begins
        )[:50],
        "legacy_candidate_count": len(legacy_candidates),
        "legacy_seal_object_count": len(legacy_seal_objects),
        "legacy_seal_pointer_count": len(legacy_seal_pointers),
        "legacy_sealed_begin_count": len(legacy_sealed_begins),
        "legacy_sealed_finalize_count": len(legacy_sealed_finalizes),
        "legacy_sealed_begin_receipts": sorted(legacy_sealed_begins)[:50],
        "legacy_sealed_finalize_receipts": sorted(legacy_sealed_finalizes)[:50],
        "legacy_unsealed_begin_count": len(legacy_unsealed),
        "legacy_unsealed_begin_receipts": sorted(legacy_unsealed)[:50],
        "orphan_legacy_seal_count": len(
            set(legacy_seal_objects) - referenced_legacy_seals
        ),
        "aborted_attempt_count": len(preserved_aborted_begins),
        "aborted_begin_receipts": sorted(preserved_aborted_begins)[:50],
        "aborted_begin_seal_object_count": len(aborted_begin_seal_objects),
        "aborted_begin_seal_pointer_count": len(aborted_begin_seal_pointers),
        "orphan_aborted_begin_seal_count": len(
            set(aborted_begin_seal_objects) - referenced_aborted_begin_seals
        ),
        "degraded_begin_count": degraded_count,
        "errors": errors,
        "formal_write_count": 0,
    }


def _read_json_argument(source: str) -> dict[str, Any]:
    try:
        if source == "-":
            raw = sys.stdin.buffer.read()
        elif source.lstrip().startswith("{"):
            raw = source.encode("utf-8")
        else:
            raw = Path(source).read_bytes()
        value = json.loads(raw)
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise TurnReceiptError("input_json_invalid") from exc
    if not isinstance(value, dict):
        raise TurnReceiptError("input_json_invalid")
    return value


def _read_reply_argument(
    *, reply_file: str | None, reply_text: str | None
) -> bytes:
    """Read the exact reply bytes without persisting the response body."""

    if (reply_file is None) == (reply_text is None):
        raise TurnReceiptError("reply_input_invalid")
    try:
        if reply_text is not None:
            return reply_text.encode("utf-8")
        if reply_file == "-":
            return sys.stdin.buffer.read()
        return Path(str(reply_file)).read_bytes()
    except (OSError, UnicodeError) as exc:
        raise TurnReceiptError("reply_input_invalid") from exc


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo", type=Path, default=Path.cwd())
    parser.add_argument("--state-dir", type=Path)
    parser.add_argument("--pretty", action="store_true")
    sub = parser.add_subparsers(dest="command", required=True)

    def output_option(command: argparse.ArgumentParser) -> None:
        command.add_argument(
            "--pretty", action="store_true", default=argparse.SUPPRESS
        )

    inspect = sub.add_parser("inspect-receipt")
    output_option(inspect)
    inspect.add_argument("--turn-receipt-sha256", required=True)

    audit = sub.add_parser("audit")
    output_option(audit)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        if args.command == "inspect-receipt":
            result = inspect_turn_receipt(
                args.repo,
                turn_receipt_sha256=args.turn_receipt_sha256,
                state_dir=args.state_dir,
            )
        else:
            result = audit_receipts(args.repo, state_dir=args.state_dir)
    except (TurnReceiptError, OSError) as exc:
        result = {
            "status": "error",
            "reason_code": str(exc),
            "formal_write_count": 0,
        }
    if getattr(args, "pretty", False):
        print(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True))
    else:
        print(
            json.dumps(
                result,
                ensure_ascii=False,
                separators=(",", ":"),
                sort_keys=True,
            )
        )
    return 0 if result.get("status") in {
        "begun",
        "already_begun",
        "finalized",
        "legacy_sealed",
        "already_legacy_sealed",
        "aborted_begin_sealed",
        "already_aborted_begin_sealed",
        "inspected",
        "legacy_inspected",
        "pass",
    } else 2


if __name__ == "__main__":
    raise SystemExit(main())
