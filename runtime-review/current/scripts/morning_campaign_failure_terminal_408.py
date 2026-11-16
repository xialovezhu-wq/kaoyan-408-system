#!/usr/bin/env python3
"""Deterministic owner for one successor-campaign failure terminal.

The owner keeps exact current-item turns in a private draft, seals one complete
conversation package, commits and reopens exactly one Capture, derives a
package-bound campaign receipt, and only then calls the existing answer bridge.
It never writes formal knowledge, T9, Obsidian, or background work.
"""

from __future__ import annotations

from study_commit_followup_408 import study_commit_boundary

import argparse
import contextlib
import fcntl
import hashlib
import json
import mimetypes
import os
import re
import stat
import tempfile
import time
from pathlib import Path
from typing import Any, Callable, Iterator, Mapping

import capture_hot_writer_408 as capture_hot
import conversation_package_408 as conversation_package
import intake_fact_capture_408 as fact_capture
import morning_campaign_answer_408 as campaign_answer
import morning_campaign_runtime_408 as campaign_runtime

import runtime_paths_408 as _paths


TURN_INPUT_SCHEMA = "morning-campaign-failure-turn-input-v1"
TERMINAL_INPUT_SCHEMA = "morning-campaign-failure-terminal-input-v1"
APPEND_INPUT_SCHEMA = "morning-campaign-failure-append-input-v1"
TERMINAL_APPEND_INPUT_SCHEMA = "morning-campaign-failure-terminal-append-input-v1"
APPEND_REQUEST_SCHEMA = "morning-campaign-failure-append-request-v1"
DRAFT_STATE_SCHEMA = "morning-campaign-failure-draft-v1"
DRAFT_TURN_SCHEMA = "morning-campaign-failure-turn-v1"
PACKAGE_CAPTURE_REQUEST_SCHEMA = "cs408-campaign-package-capture-request-v1"
PACKAGE_CAPTURE_PROOF_SCHEMA = "cs408-campaign-package-capture-proof-v1"
OPERATION_RECEIPT_SCHEMA = "morning-campaign-failure-terminal-receipt-v1"
CAMPAIGN_RECEIPT_SCHEMA_V2 = "rapid-review-durable-capture-receipt-v2"
PUBLIC_SCHEMA = "morning-campaign-failure-terminal-response-v1"

TIMEZONE = "Asia/Shanghai"
CAPTURE_HOT_DIRNAME = ".capture-hot-writer-v1"
DEFAULT_PACKAGE_ROOT = _paths.package_root()
DEFAULT_DRAFT_ROOT = DEFAULT_PACKAGE_ROOT / ".campaign-failure-drafts-v1"

SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
SAFE_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:@/+\-]{0,191}$")
FORMAL_ID_RE = re.compile(r"^(?:DS|CO|OS|CN)_(?:\d{4}|UNK)_\d{3}$")
ALLOWED_ROLES = {"user", "assistant"}
ALLOWED_TURN_KINDS = {
    "utterance",
    "first_action",
    "reasoning",
    "hint",
    "correction",
    "restatement",
    "answer",
}
ALLOWED_ATTACHMENT_ROLES = {
    "question_image",
    "solution_image",
    "explanation_image",
    "user_work_image",
    "source_article_image",
    "other_attachment",
}
ALLOWED_REASONING_RESULTS = {"sound"}
ALLOWED_PROMPT_DEPENDENCIES = {"none", "minimal", "progressive", "full"}
ALLOWED_CONFIDENCE = {"low", "medium", "high"}

MAX_JSON_BYTES = 4 * 1024 * 1024
MAX_TURN_BYTES = 256 * 1024
MAX_TURNS = 4096
MAX_ATTACHMENT_BYTES = 64 * 1024 * 1024


class CampaignFailureTerminalError(RuntimeError):
    """The same item remains recoverable and must not navigate."""


def _json_bytes(value: object, *, pretty: bool = False) -> bytes:
    if pretty:
        return (
            json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
        ).encode("utf-8")
    return json.dumps(
        value, ensure_ascii=False, separators=(",", ":"), sort_keys=True
    ).encode("utf-8")


def _digest(value: object) -> str:
    return hashlib.sha256(_json_bytes(value)).hexdigest()


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _safe_id(value: object, label: str) -> str:
    text = str(value or "")
    if not SAFE_ID_RE.fullmatch(text):
        raise CampaignFailureTerminalError(f"{label}_invalid")
    return text


def _safe_sha(value: object, label: str) -> str:
    text = str(value or "")
    if not SHA256_RE.fullmatch(text):
        raise CampaignFailureTerminalError(f"{label}_invalid")
    return text


def _private_root(value: str | Path, label: str) -> Path:
    root = Path(value).expanduser().resolve()
    if root == Path("/") or len(root.parts) < 3:
        raise CampaignFailureTerminalError(f"{label}_unsafe")
    return root


def _ensure_private_dir(path: Path) -> None:
    missing: list[Path] = []
    cursor = path
    while not cursor.exists():
        missing.append(cursor)
        if cursor.parent == cursor:
            break
        cursor = cursor.parent
    path.mkdir(parents=True, exist_ok=True)
    for created in reversed(missing):
        try:
            os.chmod(created, 0o700)
        except PermissionError:
            pass
    try:
        os.chmod(path, 0o700)
    except PermissionError:
        pass


def _atomic_write(path: Path, raw: bytes) -> None:
    _ensure_private_dir(path.parent)
    descriptor, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    tmp = Path(temporary)
    try:
        os.fchmod(descriptor, 0o600)
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(raw)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp, path)
        os.chmod(path, 0o600)
    finally:
        if tmp.exists():
            tmp.unlink()


def _read_json(path: Path, label: str, maximum: int = MAX_JSON_BYTES) -> dict[str, Any]:
    try:
        mode = path.lstat().st_mode
    except OSError as exc:
        raise CampaignFailureTerminalError(f"{label}_missing") from exc
    if stat.S_ISLNK(mode) or not stat.S_ISREG(mode):
        raise CampaignFailureTerminalError(f"{label}_unsafe")
    if path.stat().st_size > maximum:
        raise CampaignFailureTerminalError(f"{label}_too_large")
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise CampaignFailureTerminalError(f"{label}_unreadable") from exc
    if not isinstance(value, dict):
        raise CampaignFailureTerminalError(f"{label}_not_object")
    return value


@contextlib.contextmanager
def _lock(root: Path, identity: str) -> Iterator[None]:
    lock_dir = root / ".locks"
    _ensure_private_dir(lock_dir)
    path = lock_dir / f"{hashlib.sha256(identity.encode()).hexdigest()}.lock"
    descriptor = os.open(path, os.O_CREAT | os.O_RDWR, 0o600)
    try:
        os.fchmod(descriptor, 0o600)
        fcntl.flock(descriptor, fcntl.LOCK_EX)
        yield
    finally:
        fcntl.flock(descriptor, fcntl.LOCK_UN)
        os.close(descriptor)


def _load_display(path_value: object, expected_sha: object) -> tuple[Path, dict[str, Any], str]:
    try:
        return campaign_answer.reopen_campaign_display_receipt(
            str(path_value or ""), str(expected_sha or "")
        )
    except campaign_answer.CampaignAnswerError as exc:
        raise CampaignFailureTerminalError(
            f"display_receipt_invalid:{exc}"
        ) from exc


def _run_dir(runtime_root: Path, run_id: str) -> Path:
    path = runtime_root / "runs" / _safe_id(run_id, "run_id")
    if not path.is_dir() or path.is_symlink():
        raise CampaignFailureTerminalError("runtime_run_missing")
    return path


def _load_runtime_state(runtime_root: Path, run_id: str) -> tuple[Path, dict[str, Any]]:
    try:
        run_dir, _run, state, _item = campaign_runtime.reopen_verified_frontier(
            runtime_root, run_id
        )
    except campaign_runtime.CampaignRuntimeError as exc:
        raise CampaignFailureTerminalError(
            f"runtime_state_contract_invalid:{exc}"
        ) from exc
    return run_dir, state


def _current_item(state: Mapping[str, Any]) -> dict[str, Any]:
    segment = state.get("active_segment")
    if not isinstance(segment, dict):
        raise CampaignFailureTerminalError("runtime_active_segment_missing")
    items = segment.get("items")
    offset = state.get("current_item_offset")
    if not isinstance(items, list) or not isinstance(offset, int) or not 0 <= offset < len(items):
        raise CampaignFailureTerminalError("runtime_current_item_missing")
    item = items[offset]
    if not isinstance(item, dict):
        raise CampaignFailureTerminalError("runtime_current_item_invalid")
    return item


def _pending_binding(runtime_root: Path, display: Mapping[str, Any]) -> tuple[Path, dict[str, Any], dict[str, Any]]:
    run_id = str(display["run_id"])
    run_dir, state = _load_runtime_state(runtime_root, run_id)
    if state.get("status") == "episode_pending_capture":
        raise CampaignFailureTerminalError(
            "saving_contract_v2_requires_finish_item_or_close_unresolved"
        )
    if state.get("status") != "failure_pending_capture":
        raise CampaignFailureTerminalError("failure_capture_not_pending")
    pending = state.get("pending_failure")
    if not isinstance(pending, dict):
        raise CampaignFailureTerminalError("pending_failure_missing")
    item = _current_item(state)
    segment = state.get("active_segment") or {}
    checks = {
        "run_id": state.get("run_id"),
        "campaign_date": state.get("campaign_date"),
        "segment_id": segment.get("segment_id"),
        "segment_ordinal": state.get("current_segment_ordinal"),
        "item_id": item.get("item_id"),
        "formal_node_id": item.get("formal_node_id"),
        "true_item_id": item.get("true_item_id"),
        "variant_id": item.get("variant_id"),
        "surface_kind": item.get("surface_kind"),
        "surface_sha256": item.get("surface_sha256"),
        "grading_sha256": item.get("grading_sha256"),
        "campaign_item_commitment_sha256": item.get(
            "campaign_item_commitment_sha256"
        ),
    }
    for field, expected in checks.items():
        if display.get(field) != expected:
            raise CampaignFailureTerminalError(f"runtime_frontier_drift:{field}")
    payload = pending.get("payload")
    if not isinstance(payload, dict) or payload.get("capture_required") is not True:
        raise CampaignFailureTerminalError("pending_failure_payload_invalid")
    capture_request = payload.get("capture_authorization_request")
    if not isinstance(capture_request, dict):
        raise CampaignFailureTerminalError("capture_request_missing")
    if (
        pending.get("item_id") != display.get("item_id")
        or pending.get("segment_id") != display.get("segment_id")
        or pending.get("capture_request_id") != capture_request.get("request_id")
        or pending.get("pending_outcome_sha256") != payload.get("outcome_sha256")
    ):
        raise CampaignFailureTerminalError("pending_failure_binding_drift")
    binding = {
        "run_id": run_id,
        "campaign_date": display["campaign_date"],
        "segment_id": display["segment_id"],
        "segment_ordinal": display["segment_ordinal"],
        "item_id": display["item_id"],
        "formal_node_id": display["formal_node_id"],
        "true_item_id": display["true_item_id"],
        "variant_id": display.get("variant_id"),
        "surface_kind": display["surface_kind"],
        "surface_sha256": display["surface_sha256"],
        "grading_sha256": display["grading_sha256"],
        "campaign_item_commitment_sha256": display[
            "campaign_item_commitment_sha256"
        ],
        "display_receipt_sha256": None,
        "capture_request_id": capture_request["request_id"],
        "pending_outcome_sha256": payload["outcome_sha256"],
        "capture_required": True,
    }
    return run_dir, state, binding


def _normalize_turns(value: object) -> list[dict[str, Any]]:
    if not isinstance(value, list) or not value or len(value) > MAX_TURNS:
        raise CampaignFailureTerminalError("turns_invalid")
    result: list[dict[str, Any]] = []
    for expected_ordinal, row in enumerate(value, start=1):
        if not isinstance(row, dict):
            raise CampaignFailureTerminalError("turn_not_object")
        if set(row) - {"ordinal", "role", "kind", "text", "observed_at"}:
            raise CampaignFailureTerminalError("turn_fields_invalid")
        if row.get("ordinal") != expected_ordinal:
            raise CampaignFailureTerminalError("turn_ordinal_invalid")
        role = str(row.get("role") or "")
        if role not in ALLOWED_ROLES:
            raise CampaignFailureTerminalError("turn_role_invalid")
        kind = str(row.get("kind") or "")
        if kind not in ALLOWED_TURN_KINDS:
            raise CampaignFailureTerminalError("turn_kind_invalid")
        text = row.get("text")
        if not isinstance(text, str) or not text or len(text.encode("utf-8")) > MAX_TURN_BYTES:
            raise CampaignFailureTerminalError("turn_text_invalid")
        observed_at = row.get("observed_at")
        if observed_at is not None and not isinstance(observed_at, str):
            raise CampaignFailureTerminalError("turn_observed_at_invalid")
        result.append(
            {
                "ordinal": expected_ordinal,
                "role": role,
                "kind": kind,
                "text": text,
                "observed_at": observed_at,
            }
        )
    return result


def _normalize_turn_input(raw: Mapping[str, Any]) -> dict[str, Any]:
    allowed = {
        "schema",
        "display_receipt_path",
        "display_receipt_sha256",
        "turns",
        "idempotency_key",
    }
    if set(raw) != allowed or raw.get("schema") != TURN_INPUT_SCHEMA:
        raise CampaignFailureTerminalError("turn_input_schema_invalid")
    return {
        "schema": TURN_INPUT_SCHEMA,
        "display_receipt_path": str(raw["display_receipt_path"]),
        "display_receipt_sha256": _safe_sha(
            raw["display_receipt_sha256"], "display_receipt_sha256"
        ),
        "turns": _normalize_turns(raw["turns"]),
        "idempotency_key": _safe_id(raw["idempotency_key"], "idempotency_key"),
    }


def _draft_identity(display: Mapping[str, Any], display_sha: str) -> tuple[str, dict[str, str]]:
    binding = {
        "run_id": str(display["run_id"]),
        "segment_id": str(display["segment_id"]),
        "item_id": str(display["item_id"]),
        "display_receipt_sha256": display_sha,
    }
    return _digest(binding), binding


def _draft_paths(root: Path, identity_sha: str) -> tuple[Path, Path]:
    return root / "states" / f"{identity_sha}.json", root / "objects" / "turns"


def _verify_draft_objects(root: Path, state: Mapping[str, Any]) -> None:
    turns = state.get("turns")
    refs = state.get("turn_refs")
    if not isinstance(turns, list) or not isinstance(refs, list) or len(turns) != len(refs):
        raise CampaignFailureTerminalError("draft_turn_refs_invalid")
    object_root = root / "objects" / "turns"
    for ordinal, (turn, ref) in enumerate(zip(turns, refs), start=1):
        if not isinstance(turn, dict) or not isinstance(ref, dict):
            raise CampaignFailureTerminalError("draft_turn_refs_invalid")
        digest = _safe_sha(ref.get("sha256"), "draft_turn_sha256")
        path = object_root / f"{digest}.json"
        raw = path.read_bytes() if path.is_file() and not path.is_symlink() else b""
        if (
            ref.get("ordinal") != ordinal
            or ref.get("byte_count") != len(raw)
            or hashlib.sha256(raw).hexdigest() != digest
        ):
            raise CampaignFailureTerminalError("draft_turn_object_drift")
        try:
            material = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise CampaignFailureTerminalError("draft_turn_object_unreadable") from exc
        if material != {"schema": DRAFT_TURN_SCHEMA, **turn}:
            raise CampaignFailureTerminalError("draft_turn_object_drift")


def _store_turn_objects(root: Path, turns: list[dict[str, Any]]) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    _ensure_private_dir(root)
    for turn in turns:
        material = {"schema": DRAFT_TURN_SCHEMA, **turn}
        raw = _json_bytes(material, pretty=True)
        digest = hashlib.sha256(raw).hexdigest()
        path = root / f"{digest}.json"
        if path.exists():
            if path.read_bytes() != raw:
                raise CampaignFailureTerminalError("draft_turn_object_collision")
        else:
            _atomic_write(path, raw)
        rows.append(
            {
                "ordinal": turn["ordinal"],
                "sha256": digest,
                "byte_count": len(raw),
            }
        )
    return rows


def _buffer_turns_internal(
    normalized: dict[str, Any],
    *,
    runtime_root: Path,
    draft_root: Path,
    draft_lock_held: bool = False,
) -> dict[str, Any]:
    _display_path, display, display_sha = _load_display(
        normalized["display_receipt_path"], normalized["display_receipt_sha256"]
    )
    _run_path, _state, campaign_binding = _pending_binding(runtime_root, display)
    campaign_binding["display_receipt_sha256"] = display_sha
    identity_sha, identity = _draft_identity(display, display_sha)
    state_path, turn_root = _draft_paths(draft_root, identity_sha)
    trace_sha = _digest(normalized["turns"])
    request_material = {
        "schema": TURN_INPUT_SCHEMA,
        "identity": identity,
        "trace_sha256": trace_sha,
    }
    request_sha = _digest(request_material)
    pointer = draft_root / "idempotency" / (
        hashlib.sha256(normalized["idempotency_key"].encode()).hexdigest() + ".json"
    )
    with contextlib.nullcontext() if draft_lock_held else _lock(draft_root, identity_sha):
        if pointer.exists():
            prior = _read_json(pointer, "draft_idempotency_pointer")
            if prior.get("request_sha256") != request_sha:
                raise CampaignFailureTerminalError("idempotency_key_reused_differently")
        existing: dict[str, Any] | None = None
        if state_path.exists():
            existing = _read_json(state_path, "draft_state")
            old_turns = existing.get("turns")
            if not isinstance(old_turns, list):
                raise CampaignFailureTerminalError("draft_state_invalid")
            if (
                existing.get("identity") != identity
                or existing.get("campaign_binding") != campaign_binding
                or len(normalized["turns"]) < len(old_turns)
                or normalized["turns"][: len(old_turns)] != old_turns
            ):
                raise CampaignFailureTerminalError("draft_prefix_drift")
        refs = _store_turn_objects(turn_root, normalized["turns"])
        state = {
            "schema": DRAFT_STATE_SCHEMA,
            "identity": identity,
            "campaign_binding": campaign_binding,
            "turns": normalized["turns"],
            "turn_refs": refs,
            "turn_count": len(normalized["turns"]),
            "trace_sha256": trace_sha,
            "capture_write_count": 0,
            "formal_write_count": 0,
            "background_processing": "none",
        }
        raw = _json_bytes(state, pretty=True)
        if state_path.exists() and state_path.read_bytes() == raw:
            replayed = True
        else:
            _atomic_write(state_path, raw)
            replayed = False
        pointer_value = {
            "schema": "morning-campaign-failure-draft-idempotency-v1",
            "idempotency_key": normalized["idempotency_key"],
            "request_sha256": request_sha,
            "identity_sha256": identity_sha,
            "trace_sha256": trace_sha,
            "formal_write_count": 0,
        }
        pointer_raw = _json_bytes(pointer_value, pretty=True)
        if pointer.exists() and pointer.read_bytes() != pointer_raw:
            raise CampaignFailureTerminalError("draft_idempotency_pointer_drift")
        if not pointer.exists():
            _atomic_write(pointer, pointer_raw)
    return {
        "schema": PUBLIC_SCHEMA,
        "status": "buffered",
        "run_id": display["run_id"],
        "item_id": display["item_id"],
        "turn_count": len(normalized["turns"]),
        "trace_sha256": trace_sha,
        "runtime_status": "failure_pending_capture",
        "replayed": replayed,
        "capture_write_count": 0,
        "navigation_write_count": 0,
        "t9_write_count": 0,
        "obsidian_write_count": 0,
        "duplicate_capture_count": 0,
        "duplicate_display_count": 0,
        "formal_write_count": 0,
        "background_processing": "none",
    }


def buffer_turns(
    raw: Mapping[str, Any],
    *,
    repo_root: str | Path,
    runtime_root: str | Path,
    draft_root: str | Path = DEFAULT_DRAFT_ROOT,
) -> dict[str, Any]:
    repo = Path(repo_root).expanduser().resolve()
    if not repo.is_dir():
        raise CampaignFailureTerminalError("repo_root_missing")
    runtime = _private_root(runtime_root, "runtime_root")
    drafts = _private_root(draft_root, "draft_root")
    return _buffer_turns_internal(
        _normalize_turn_input(raw), runtime_root=runtime, draft_root=drafts
    )


def _normalize_terminal_input(
    raw: Mapping[str, Any], *, allow_empty_turns: bool = False,
) -> dict[str, Any]:
    allowed = {
        "schema",
        "display_receipt_path",
        "display_receipt_sha256",
        "turns",
        "corrected_response",
        "reasoning_result",
        "prompt_dependency",
        "confidence",
        "frozen_feedback",
        "user_error_entry",
        "user_error_provenance",
        "first_action",
        "first_action_provenance",
        "missing_fields",
        "attachments",
        "idempotency_key",
    }
    if set(raw) - allowed or raw.get("schema") != TERMINAL_INPUT_SCHEMA:
        raise CampaignFailureTerminalError("terminal_input_schema_invalid")
    required = allowed - {"attachments"}
    if any(field not in raw for field in required):
        raise CampaignFailureTerminalError("terminal_input_fields_missing")
    response = str(raw["corrected_response"] or "").upper()
    if response not in {"A", "B", "C", "D"}:
        raise CampaignFailureTerminalError("corrected_response_invalid")
    reasoning = str(raw["reasoning_result"] or "")
    prompt = str(raw["prompt_dependency"] or "")
    confidence = str(raw["confidence"] or "")
    if reasoning not in ALLOWED_REASONING_RESULTS:
        raise CampaignFailureTerminalError("terminal_reasoning_not_sound")
    if prompt not in ALLOWED_PROMPT_DEPENDENCIES:
        raise CampaignFailureTerminalError("prompt_dependency_invalid")
    if confidence not in ALLOWED_CONFIDENCE:
        raise CampaignFailureTerminalError("confidence_invalid")
    frozen = raw["frozen_feedback"]
    if not isinstance(frozen, str) or not frozen:
        raise CampaignFailureTerminalError("frozen_feedback_invalid")
    provenance = str(raw["user_error_provenance"] or "")
    first_provenance = str(raw["first_action_provenance"] or "")
    if provenance not in fact_capture.PROVENANCE or first_provenance not in {
        "user_report",
        "visible_evidence",
    }:
        raise CampaignFailureTerminalError("user_fact_provenance_invalid")
    if provenance == "not_observed" and str(raw["user_error_entry"]) != "未观察到":
        raise CampaignFailureTerminalError("user_error_provenance_conflict")
    missing = raw["missing_fields"]
    if not isinstance(missing, list) or not all(isinstance(item, str) for item in missing):
        raise CampaignFailureTerminalError("missing_fields_invalid")
    attachments = raw.get("attachments") or []
    if not isinstance(attachments, list):
        raise CampaignFailureTerminalError("attachments_invalid")
    try:
        turns = [] if allow_empty_turns and raw["turns"] == [] else _normalize_turns(raw["turns"])
    except CampaignFailureTerminalError as exc:
        if str(exc).startswith("turn_") or str(exc) == "turns_invalid":
            raise CampaignFailureTerminalError(
                "CAMPAIGN_FINAL_TRACE_INCOMPLETE"
            ) from exc
        raise
    return {
        "schema": TERMINAL_INPUT_SCHEMA,
        "display_receipt_path": str(raw["display_receipt_path"]),
        "display_receipt_sha256": _safe_sha(
            raw["display_receipt_sha256"], "display_receipt_sha256"
        ),
        "turns": turns,
        "corrected_response": response,
        "reasoning_result": reasoning,
        "prompt_dependency": prompt,
        "confidence": confidence,
        "frozen_feedback": frozen,
        "user_error_entry": str(raw["user_error_entry"]),
        "user_error_provenance": provenance,
        "first_action": str(raw["first_action"]),
        "first_action_provenance": first_provenance,
        "missing_fields": sorted({item for item in missing if item}),
        "attachments": attachments,
        "idempotency_key": _safe_id(raw["idempotency_key"], "idempotency_key"),
    }


def _correct_letter(display: Mapping[str, Any]) -> str:
    try:
        return campaign_answer.correct_letter_for_display(dict(display))
    except campaign_answer.CampaignAnswerError as exc:
        raise CampaignFailureTerminalError(f"grading_capsule_invalid:{exc}") from exc


def _attachment_from_path(
    path: Path,
    *,
    expected_sha: str,
    role: str,
    mime_type: str,
    label: str,
) -> dict[str, Any]:
    if role not in ALLOWED_ATTACHMENT_ROLES:
        raise CampaignFailureTerminalError("attachment_role_invalid")
    if not path.is_file() or path.is_symlink() or path.stat().st_size > MAX_ATTACHMENT_BYTES:
        raise CampaignFailureTerminalError("attachment_path_invalid")
    data = path.read_bytes()
    if hashlib.sha256(data).hexdigest() != _safe_sha(expected_sha, "attachment_sha256"):
        raise CampaignFailureTerminalError("attachment_sha256_drift")
    return {"data": data, "role": role, "mime_type": mime_type, "label": label}


def _attachments(display: Mapping[str, Any], terminal: Mapping[str, Any]) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    if "question_assets" in display:
        question_assets = display.get("question_assets")
        if not isinstance(question_assets, list):
            raise CampaignFailureTerminalError("display_question_assets_invalid")
        for ordinal, asset in enumerate(question_assets, start=1):
            if not isinstance(asset, dict):
                raise CampaignFailureTerminalError("display_question_assets_invalid")
            path = Path(str(asset.get("path") or "")).expanduser().resolve()
            if (
                not isinstance(asset.get("size_bytes"), int)
                or not path.is_file()
                or path.stat().st_size != asset["size_bytes"]
            ):
                raise CampaignFailureTerminalError("question_asset_size_drift")
            mime = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
            page = asset.get("page_locator")
            rows.append(
                _attachment_from_path(
                    path,
                    expected_sha=str(asset.get("sha256") or ""),
                    role="question_image",
                    mime_type=mime,
                    label=(
                        f"{display['true_item_id']} question image page {ordinal}"
                        + (f" ({page})" if page is not None else "")
                    ),
                )
            )
    else:
        # Historical display receipts only bound the first question asset.
        # Preserve that read-only compatibility while all new receipts carry
        # the complete validated page list above.
        surface_path = display.get("surface_object_path")
        surface_sha = display.get("question_asset_sha256") or display.get(
            "surface_sha256"
        )
        if isinstance(surface_path, str) and surface_path:
            path = Path(surface_path).expanduser().resolve()
            mime = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
            rows.append(
                _attachment_from_path(
                    path,
                    expected_sha=str(surface_sha),
                    role="question_image",
                    mime_type=mime,
                    label=f"{display['true_item_id']} question image",
                )
            )
    for raw in terminal.get("attachments") or []:
        if not isinstance(raw, dict):
            raise CampaignFailureTerminalError("attachment_descriptor_invalid")
        required = {"source_path", "sha256", "role", "mime_type", "label"}
        if set(raw) != required:
            raise CampaignFailureTerminalError("attachment_descriptor_fields_invalid")
        rows.append(
            _attachment_from_path(
                Path(str(raw["source_path"])).expanduser().resolve(),
                expected_sha=str(raw["sha256"]),
                role=str(raw["role"]),
                mime_type=str(raw["mime_type"]),
                label=str(raw["label"]),
            )
        )
    return rows


def _expected_package_turns(turns: list[dict[str, Any]], frozen_feedback: str) -> list[dict[str, Any]]:
    expected = [dict(row) for row in turns]
    expected.append(
        {
            "ordinal": len(expected) + 1,
            "role": "assistant",
            "kind": "frozen_feedback",
            "text": frozen_feedback,
            "observed_at": None,
        }
    )
    return expected


def _verify_package(
    opened: Mapping[str, Any], request: Mapping[str, Any], package_id: str
) -> dict[str, Any]:
    manifest = opened.get("manifest")
    receipt = opened.get("receipt")
    source = opened.get("source")
    conversation = opened.get("conversation")
    if not all(isinstance(value, dict) for value in (manifest, receipt, source, conversation)):
        raise CampaignFailureTerminalError("conversation_package_surfaces_incomplete")
    package = request["package"]
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
    recomputed_canonical_sha = hashlib.sha256(
        _json_bytes(canonical_material) + b"\n"
    ).hexdigest()
    if (
        manifest.get("package_id") != package_id
        or manifest.get("study_date") != package["study_date"]
        or manifest.get("session_id") != package["session_id"]
        or manifest.get("item_id") != package["item_id"]
        or manifest.get("source_id") != package["source_id"]
        or manifest.get("sealed") is not True
        or manifest.get("formal_write_count") != 0
        or manifest.get("background_processing") != "none"
        or receipt.get("sealed") is not True
        or receipt.get("status") != fact_capture.PENDING_FORMAL_STATUS
        or receipt.get("locator")
        != conversation_package.LOCATOR_PREFIX + package_id
        or receipt.get("canonical_sha256") != manifest.get("canonical_sha256")
        or recomputed_canonical_sha != manifest.get("canonical_sha256")
        or source.get("source") != package["source_envelope"]
        or source.get("session_id") != package["session_id"]
        or source.get("item_id") != package["item_id"]
        or source.get("source_id") != package["source_id"]
        or conversation.get("turns")
        != _expected_package_turns(package["turns"], package["frozen_feedback"])
        or conversation.get("complete_order_preserved") is not True
        or conversation.get("truncated") is not False
    ):
        raise CampaignFailureTerminalError("conversation_package_binding_drift")
    canonical_sha = _safe_sha(receipt.get("canonical_sha256"), "package_sha256")
    return {
        "locator": receipt["locator"],
        "canonical_sha256": canonical_sha,
        "revision": receipt.get("revision"),
        "sealed": True,
        "status": fact_capture.PENDING_FORMAL_STATUS,
        "study_date": package["study_date"],
        "session_id": package["session_id"],
        "item_id": package["item_id"],
        "source_id": package["source_id"],
        "turn_count": conversation.get("turn_count"),
        "complete_order_preserved": True,
        "truncated": False,
        "files_verified": True,
        "formal_write_count": 0,
        "background_processing": "none",
    }


def _package_capture_request(
    display: Mapping[str, Any],
    draft_state: Mapping[str, Any],
    terminal: Mapping[str, Any],
) -> dict[str, Any]:
    binding = dict(draft_state["campaign_binding"])
    binding["terminal_trace_sha256"] = draft_state["trace_sha256"]
    source_envelope = {
        "schema": "cs408-campaign-package-source-v1",
        "origin": "morning_coverage_campaign_v1",
        "campaign_binding": binding,
        "question_surface": display["learner_surface"],
        "question_asset_sha256": display.get("question_asset_sha256"),
        "grading_capsule_sha256": display.get("grading_capsule_sha256"),
        "terminal_assessment": {
            "corrected_response": terminal["corrected_response"],
            "reasoning_result": terminal["reasoning_result"],
            "prompt_dependency": terminal["prompt_dependency"],
            "confidence": terminal["confidence"],
        },
    }
    observed_at = draft_state["turns"][-1].get("observed_at")
    subject = str(display["formal_node_id"]).split("_", 1)[0]
    return {
        "schema": PACKAGE_CAPTURE_REQUEST_SCHEMA,
        "campaign_binding": binding,
        "package": {
            "study_date": display["campaign_date"],
            "session_id": display["run_id"],
            "item_id": display["item_id"],
            "source_id": display["true_item_id"],
            "source_envelope": source_envelope,
            "turns": draft_state["turns"],
            "frozen_feedback": terminal["frozen_feedback"],
            "attachments": _attachments(display, terminal),
            "missing_fields": terminal["missing_fields"],
        },
        "capture_facts": {
            "source_facts": {
                "subject": subject if subject in {"DS", "CO", "OS", "CN"} else "未确认",
                "question_type": "晨间 campaign 失败题终局",
                "source_id": display["true_item_id"],
                "details_id": "DISPLAY-" + binding["display_receipt_sha256"][:20],
            },
            "user_facts": {
                "user_error_entry": terminal["user_error_entry"],
                "user_error_provenance": terminal["user_error_provenance"],
                "first_action": terminal["first_action"],
                "first_action_provenance": terminal["first_action_provenance"],
                "observed_at": observed_at or f"{display['campaign_date']}T00:00:00+08:00",
            },
            "identity_hint": {
                "status": "existing",
                "formal_id": display["formal_node_id"],
                "mode": "redo",
                "basis": "campaign-display-receipt",
            },
            "answer_safe_context_anchor": f"campaign terminal {display['formal_node_id']}",
        },
    }


def _capture_index_projection(
    raw_payload: Mapping[str, Any], *, observed_at_known: bool = True,
) -> dict[str, Any]:
    """Keep protected narrative in its package, never in answer-safe index fields.

    This does not rewrite the terminal request, package, or evidence provenance.
    A deferred field is a locator notice, not a guessed error or an assertion that
    observed evidence was absent. The normal Capture validator still owns admission.
    """
    payload = dict(raw_payload)
    user = dict(payload["user_facts"])
    missing = set(payload["missing_fields"])
    placeholders = {
        "user_error_entry": "原始错误入口含受保护题证，详见已封存会话包",
        "first_action": "原始第一动作含受保护题证，详见已封存会话包",
    }
    deferred = False
    for field, placeholder in placeholders.items():
        if field not in user:
            continue
        try:
            user[field] = fact_capture._assert_answer_safe(f"user_facts.{field}", user[field])
        except fact_capture.CaptureError:
            deferred = True
            user[field] = fact_capture._assert_answer_safe(field, placeholder)
            missing.add(f"capture_{field}_summary_deferred")
    # These protected rows could never have committed under the old validator.
    # Do not synthesize a clock time when recovering them; keep already admissible
    # historical payloads byte-compatible for half-commit/idempotent recovery.
    if deferred and not observed_at_known:
        user.pop("observed_at", None)
    payload["user_facts"] = user
    payload["missing_fields"] = sorted(missing)
    return payload


def commit_campaign_package_capture(
    request: Mapping[str, Any],
    *,
    repo_root: str | Path,
    package_root: str | Path = DEFAULT_PACKAGE_ROOT,
) -> dict[str, Any]:
    if request.get("schema") != PACKAGE_CAPTURE_REQUEST_SCHEMA:
        raise CampaignFailureTerminalError("package_capture_request_schema_invalid")
    repo = Path(repo_root).expanduser().resolve()
    packages = _private_root(package_root, "package_root")
    binding = request.get("campaign_binding")
    package = request.get("package")
    facts = request.get("capture_facts")
    if not isinstance(binding, dict) or binding.get("capture_required") is not True:
        raise CampaignFailureTerminalError("campaign_capture_not_required")
    if not isinstance(package, dict) or not isinstance(facts, dict):
        raise CampaignFailureTerminalError("package_capture_request_invalid")
    package_started = time.perf_counter()
    package_id = conversation_package.package_id_for(
        study_date=str(package["study_date"]),
        session_id=str(package["session_id"]),
        item_id=str(package["item_id"]),
        source_id=str(package["source_id"]),
    )
    locator = conversation_package.LOCATOR_PREFIX + package_id
    package_dir = packages / str(package["study_date"]) / package_id
    opened: dict[str, Any]
    if package_dir.exists():
        if not (package_dir / "receipt.json").is_file():
            conversation_package.record_package(
                study_date=str(package["study_date"]),
                session_id=str(package["session_id"]),
                item_id=str(package["item_id"]),
                source_id=str(package["source_id"]),
                source=dict(package["source_envelope"]),
                conversation=list(package["turns"]),
                frozen_feedback=str(package["frozen_feedback"]),
                attachments=list(package["attachments"]),
                missing_fields=list(package["missing_fields"]),
                seal=True,
                private_root=packages,
            )
            opened = conversation_package.verify_package_binding(
                locator, require_sealed=True, private_root=packages
            )
        else:
            opened = conversation_package.verify_package_binding(
                locator, private_root=packages
            )
            if (opened.get("receipt") or {}).get("sealed") is not True:
                conversation_package.record_package(
                    study_date=str(package["study_date"]),
                    session_id=str(package["session_id"]),
                    item_id=str(package["item_id"]),
                    source_id=str(package["source_id"]),
                    source=dict(package["source_envelope"]),
                    conversation=list(package["turns"]),
                    frozen_feedback=str(package["frozen_feedback"]),
                    attachments=list(package["attachments"]),
                    missing_fields=list(package["missing_fields"]),
                    seal=True,
                    private_root=packages,
                )
                opened = conversation_package.verify_package_binding(
                    locator, require_sealed=True, private_root=packages
                )
    else:
        conversation_package.record_package(
            study_date=str(package["study_date"]),
            session_id=str(package["session_id"]),
            item_id=str(package["item_id"]),
            source_id=str(package["source_id"]),
            source=dict(package["source_envelope"]),
            conversation=list(package["turns"]),
            frozen_feedback=str(package["frozen_feedback"]),
            attachments=list(package["attachments"]),
            missing_fields=list(package["missing_fields"]),
            seal=True,
            private_root=packages,
        )
        opened = conversation_package.verify_package_binding(
            locator, require_sealed=True, private_root=packages
        )
    package_proof = _verify_package(opened, request, package_id)
    package_seal_reopen_ms = (time.perf_counter() - package_started) * 1000
    capture_key = (
        f"campaign-terminal:{package['study_date']}:{package['session_id']}:"
        f"{package['item_id']}:final-package:v2"
    )
    raw_payload = {
        "schema": fact_capture.SCHEMA,
        "study_date": package["study_date"],
        "timezone": TIMEZONE,
        "idempotency_key": capture_key,
        "stable_evidence_refs": [
            {
                "kind": fact_capture.CONVERSATION_PACKAGE_REF_KIND,
                "locator": package_proof["locator"],
                "sha256": package_proof["canonical_sha256"],
            }
        ],
        "source_facts": facts["source_facts"],
        "user_facts": facts["user_facts"],
        "identity_hint": facts["identity_hint"],
        "answer_safe_context_anchor": facts["answer_safe_context_anchor"],
        "missing_fields": sorted(set(package["missing_fields"])),
        "formalization_authorized": True,
        "authorization_policy": fact_capture.CONVERSATION_PACKAGE_PENDING_POLICY,
    }
    raw_payload = _capture_index_projection(
        raw_payload, observed_at_known=bool(package["turns"][-1].get("observed_at"))
    )
    try:
        validated_payload = fact_capture._validate_capture_payload(raw_payload)
        canonical = capture_hot.canonical_fact_event_from_payload(validated_payload)
    except (fact_capture.CaptureError, capture_hot.CaptureHotWriterError) as exc:
        raise CampaignFailureTerminalError(f"capture_payload_invalid:{exc}") from exc
    payload = canonical["capture"]
    capture_root = fact_capture.capture_root(repo)
    ledger = capture_root / "events.jsonl"
    state = capture_root / "state.json"
    hot_root = capture_root / CAPTURE_HOT_DIRNAME
    if not ledger.is_file() or not state.is_file() or not hot_root.is_dir():
        raise CampaignFailureTerminalError("capture_truth_not_prepared")
    pending_path = hot_root / capture_hot.PENDING_FILENAME
    if pending_path.is_file():
        try:
            pending_identity = capture_hot.read_pending_identity(hot_root)
            expected_pending = {
                "event_id": canonical["event_id"],
                "capture_id": canonical["capture_id"],
                "idempotency_key_sha256": hashlib.sha256(
                    capture_key.encode("utf-8")
                ).hexdigest(),
                "payload_sha256": canonical["payload_sha256"],
            }
            if pending_identity is None or any(
                pending_identity.get(key) != value
                for key, value in expected_pending.items()
            ):
                raise CampaignFailureTerminalError(
                    "foreign_capture_pending_reconcile_required"
                )
            fact_capture.reconcile(repo_root=repo)
            with capture_hot.capture_ledger_lock(ledger) as token:
                capture_hot.cold_bootstrap(
                    ledger,
                    state,
                    hot_root,
                    lock_token=token,
                    reconcile=True,
                )
        except CampaignFailureTerminalError:
            raise
        except (fact_capture.CaptureError, capture_hot.CaptureHotWriterError, OSError) as exc:
            raise CampaignFailureTerminalError(
                f"capture_pending_reconcile_failed:{exc}"
            ) from exc
    capture_commit_started = time.perf_counter()
    try:
        with capture_hot.capture_ledger_lock(ledger) as token:
            result = capture_hot.capture_fact(
                ledger,
                state,
                hot_root,
                lock_token=token,
                validated_payload=payload,
            )
            capture_hot.verify_joint_commit(
                ledger,
                state,
                hot_root,
                event_id=str(result["event_id"]),
            )
        capture_commit_ms = (time.perf_counter() - capture_commit_started) * 1000
        canonical_reopen_started = time.perf_counter()
        binding_proof = fact_capture.reopen_capture_binding(
            str(canonical["capture_id"]),
            expected_payload_sha256=str(canonical["payload_sha256"]),
            expected_idempotency_key=capture_key,
            repo_root=repo,
        )
        capture_canonical_reopen_ms = (
            time.perf_counter() - canonical_reopen_started
        ) * 1000
        joint_reopen_started = time.perf_counter()
        joint = capture_hot.verify_joint_commit(
            ledger,
            state,
            hot_root,
            idempotency_key=capture_key,
        )
        capture_joint_verify_ms = (
            time.perf_counter() - joint_reopen_started
        ) * 1000
    except (capture_hot.CaptureHotWriterError, fact_capture.CaptureError, OSError) as exc:
        raise CampaignFailureTerminalError(f"capture_commit_or_reopen_failed:{exc}") from exc
    expected_ref = {
        "kind": fact_capture.CONVERSATION_PACKAGE_REF_KIND,
        "locator": package_proof["locator"],
        "sha256": package_proof["canonical_sha256"],
    }
    if (
        result.get("status") not in {"CAPTURED", "ALREADY_COMMITTED"}
        or result.get("quality_status") != fact_capture.PENDING_FORMAL_STATUS
        or result.get("formal_write_count") != 0
        or binding_proof.get("stable_evidence_refs") != [expected_ref]
        or binding_proof.get("quality_status") != fact_capture.PENDING_FORMAL_STATUS
        or binding_proof.get("formalization_authorized") is not True
        or joint.get("status") != "PASS"
        or joint.get("capture_id") != canonical["capture_id"]
        or joint.get("receipt_sha256") != result.get("receipt_sha256")
    ):
        raise CampaignFailureTerminalError("capture_proof_not_verified")
    return {
        "schema": PACKAGE_CAPTURE_PROOF_SCHEMA,
        "status": "VERIFIED",
        "replayed": result.get("status") == "ALREADY_COMMITTED",
        "campaign_binding": dict(binding),
        "package": package_proof,
        "capture": {
            "capture_id": canonical["capture_id"],
            "capture_committed_at_utc": binding_proof["capture_committed_at_utc"],
            "idempotency_key": capture_key,
            "payload_sha256": canonical["payload_sha256"],
            "capture_event_id": canonical["event_id"],
            "capture_event_sha256": joint["event_sha256"],
            "capture_ledger_event_sha256": binding_proof["event_sha256"],
            "capture_receipt_sha256": joint["receipt_sha256"],
            "capture_joint_manifest_sha256": joint["joint_manifest_sha256"],
            "capture_bounded_manifest_sha256": joint["bounded_manifest_sha256"],
            "capture_status": fact_capture.PENDING_FORMAL_STATUS,
            "formalization_authorized": True,
            "stable_evidence_refs": [expected_ref],
            "joint_status": "PASS",
            "formal_write_count": 0,
        },
        "package_reopened": True,
        "capture_binding_reopened": True,
        "hot_joint_reopened": True,
        "navigation_write_count": 0,
        "runtime_write_count": 0,
        "t9_write_count": 0,
        "obsidian_write_count": 0,
        "formal_write_count": 0,
        "timing_ms": {
            "package_seal_reopen_ms": round(package_seal_reopen_ms, 3),
            "capture_commit_ms": round(capture_commit_ms, 3),
            "capture_canonical_reopen_ms": round(
                capture_canonical_reopen_ms, 3
            ),
            "capture_joint_verify_ms": round(capture_joint_verify_ms, 3),
        },
    }


def reopen_campaign_package_capture(
    proof: Mapping[str, Any],
    *,
    repo_root: str | Path,
    package_root: str | Path = DEFAULT_PACKAGE_ROOT,
) -> dict[str, Any]:
    if proof.get("schema") != PACKAGE_CAPTURE_PROOF_SCHEMA:
        raise CampaignFailureTerminalError("package_capture_proof_schema_invalid")
    packages = _private_root(package_root, "package_root")
    package = proof.get("package")
    capture = proof.get("capture")
    proof_binding = proof.get("campaign_binding")
    if (
        not isinstance(package, dict)
        or not isinstance(capture, dict)
        or not isinstance(proof_binding, dict)
    ):
        raise CampaignFailureTerminalError("package_capture_proof_invalid")
    try:
        opened = conversation_package.verify_package_binding(
            str(package["locator"]),
            expected_canonical_sha256=str(package["canonical_sha256"]),
            require_sealed=True,
            private_root=packages,
        )
    except (conversation_package.ConversationPackageError, KeyError) as exc:
        raise CampaignFailureTerminalError(
            f"package_capture_reopen_drift:{exc}"
        ) from exc
    source = opened.get("source") or {}
    source_envelope = source.get("source") if isinstance(source, dict) else None
    expected_ref = {
        "kind": fact_capture.CONVERSATION_PACKAGE_REF_KIND,
        "locator": package.get("locator"),
        "sha256": package.get("canonical_sha256"),
    }
    if (
        not isinstance(source_envelope, dict)
        or source_envelope.get("schema") != "cs408-campaign-package-source-v1"
        or source_envelope.get("origin") != "morning_coverage_campaign_v1"
        or source_envelope.get("campaign_binding") != proof_binding
        or source.get("study_date") != package.get("study_date")
        or source.get("session_id") != package.get("session_id")
        or source.get("item_id") != package.get("item_id")
        or source.get("source_id") != package.get("source_id")
        or opened.get("canonical_sha256") != package.get("canonical_sha256")
        or opened.get("package_id")
        != str(package.get("locator") or "").removeprefix(
            conversation_package.LOCATOR_PREFIX
        )
        or capture.get("stable_evidence_refs") != [expected_ref]
    ):
        raise CampaignFailureTerminalError("package_capture_proof_binding_drift")
    binding = fact_capture.reopen_capture_binding(
        str(capture["capture_id"]),
        expected_payload_sha256=str(capture["payload_sha256"]),
        expected_idempotency_key=str(capture["idempotency_key"]),
        repo_root=repo_root,
    )
    capture_root = fact_capture.capture_root(repo_root)
    joint = capture_hot.verify_joint_commit(
        capture_root / "events.jsonl",
        capture_root / "state.json",
        capture_root / CAPTURE_HOT_DIRNAME,
        idempotency_key=str(capture["idempotency_key"]),
    )
    if (
        binding.get("capture_id") != capture.get("capture_id")
        or binding.get("event_id") != capture.get("capture_event_id")
        or binding.get("event_sha256") != capture.get("capture_ledger_event_sha256")
        or binding.get("stable_evidence_refs") != [expected_ref]
        or binding.get("quality_status") != fact_capture.PENDING_FORMAL_STATUS
        or binding.get("formalization_authorized") is not True
        or binding.get("formal_write_count") != 0
        or joint.get("status") != "PASS"
        or joint.get("capture_id") != capture.get("capture_id")
        or joint.get("event_id") != capture.get("capture_event_id")
        or joint.get("event_sha256") != capture.get("capture_event_sha256")
        or joint.get("receipt_sha256") != capture.get("capture_receipt_sha256")
        or joint.get("formal_write_count") != 0
    ):
        raise CampaignFailureTerminalError("package_capture_reopen_not_verified")
    refreshed_capture = {
        **dict(capture),
        "capture_committed_at_utc": binding["capture_committed_at_utc"],
        "capture_event_sha256": joint["event_sha256"],
        "capture_ledger_event_sha256": binding["event_sha256"],
        "capture_receipt_sha256": joint["receipt_sha256"],
        "capture_joint_manifest_sha256": joint["joint_manifest_sha256"],
        "capture_bounded_manifest_sha256": joint[
            "bounded_manifest_sha256"
        ],
    }
    return {
        **dict(proof),
        "capture": refreshed_capture,
        "replayed": True,
    }


def _campaign_receipt(
    proof: Mapping[str, Any],
    *,
    run_dir: Path,
    package_root: Path,
    create_if_missing: bool = True,
) -> tuple[Path, str]:
    binding = proof["campaign_binding"]
    package = proof["package"]
    capture = proof["capture"]
    core = {
        "schema": CAMPAIGN_RECEIPT_SCHEMA_V2,
        "status": "durable",
        "run_id": binding["run_id"],
        "segment_id": binding["segment_id"],
        "item_id": binding["item_id"],
        "capture_request_id": binding["capture_request_id"],
        "pending_outcome_sha256": binding["pending_outcome_sha256"],
        "conversation_package_locator": package["locator"],
        "conversation_package_sha256": package["canonical_sha256"],
        "conversation_package_root": str(package_root),
        "capture_id": capture["capture_id"],
        "capture_payload_sha256": capture["payload_sha256"],
        "capture_idempotency_key": capture["idempotency_key"],
        "capture_event_id": capture["capture_event_id"],
        "capture_event_sha256": capture["capture_event_sha256"],
        "capture_ledger_event_sha256": capture["capture_ledger_event_sha256"],
        "capture_joint_receipt_sha256": capture["capture_receipt_sha256"],
        "capture_joint_manifest_sha256": capture[
            "capture_joint_manifest_sha256"
        ],
        "capture_bounded_manifest_sha256": capture[
            "capture_bounded_manifest_sha256"
        ],
        "capture_status": fact_capture.PENDING_FORMAL_STATUS,
        "formalization_authorized": True,
        "producer_schema": PACKAGE_CAPTURE_PROOF_SCHEMA,
        "formal_write_count": 0,
    }
    receipt = {**core, "receipt_content_sha256": _digest(core)}
    raw = _json_bytes(receipt, pretty=True)
    actual = hashlib.sha256(raw).hexdigest()
    path = run_dir / "campaign-capture-receipts-v2" / f"{actual}.json"
    if path.exists():
        if path.read_bytes() != raw:
            raise CampaignFailureTerminalError("campaign_receipt_collision")
    elif not create_if_missing:
        raise CampaignFailureTerminalError(
            "post_finalize_campaign_receipt_missing"
        )
    else:
        _atomic_write(path, raw)
    return path, actual


def _operation_paths(run_dir: Path, idempotency_key: str) -> tuple[Path, Path]:
    root = run_dir / "failure-terminal-v1"
    pointer = root / "idempotency" / (
        hashlib.sha256(idempotency_key.encode()).hexdigest() + ".json"
    )
    return root, pointer


def _operation_lookup(
    run_dir: Path, *, idempotency_key: str, request_sha256: str
) -> dict[str, Any] | None:
    root, pointer = _operation_paths(run_dir, idempotency_key)
    if not pointer.exists():
        receipts = root / "receipts"
        if not receipts.exists():
            return None
        matches: list[tuple[Path, dict[str, Any], str]] = []
        for candidate in sorted(receipts.glob("*.json")):
            receipt = _read_json(candidate, "orphan_terminal_receipt")
            if (
                receipt.get("schema") == OPERATION_RECEIPT_SCHEMA
                and receipt.get("idempotency_key") == idempotency_key
                and receipt.get("request_sha256") == request_sha256
            ):
                digest = _sha256_file(candidate)
                if candidate.stem != digest:
                    raise CampaignFailureTerminalError(
                        "orphan_terminal_receipt_hash_drift"
                    )
                matches.append((candidate, receipt, digest))
        if not matches:
            return None
        if len(matches) != 1:
            raise CampaignFailureTerminalError(
                "orphan_terminal_receipt_ambiguous"
            )
        candidate, receipt, digest = matches[0]
        pointer_value = {
            "schema": "morning-campaign-failure-terminal-pointer-v1",
            "idempotency_key": idempotency_key,
            "request_sha256": request_sha256,
            "receipt_ref": str(
                candidate.resolve().relative_to(root.resolve())
            ),
            "receipt_sha256": digest,
            "formal_write_count": 0,
        }
        _atomic_write(pointer, _json_bytes(pointer_value, pretty=True))
        response = receipt.get("public_response")
        if not isinstance(response, dict):
            raise CampaignFailureTerminalError(
                "terminal_operation_receipt_invalid"
            )
        return {**response, "replayed": True}
    value = _read_json(pointer, "terminal_operation_pointer")
    if value.get("request_sha256") != request_sha256:
        raise CampaignFailureTerminalError("idempotency_key_reused_differently")
    receipt_ref = value.get("receipt_ref")
    if not isinstance(receipt_ref, str):
        raise CampaignFailureTerminalError("terminal_operation_pointer_invalid")
    try:
        receipt_path = (root / receipt_ref).resolve(strict=True)
    except OSError:
        # The lower package/Capture/runtime/bridge layers are independently
        # idempotent. Re-run them to recreate the terminal operation receipt.
        return None
    try:
        receipt_path.relative_to(root.resolve())
    except ValueError as exc:
        raise CampaignFailureTerminalError("terminal_operation_pointer_escape") from exc
    if _sha256_file(receipt_path) != value.get("receipt_sha256"):
        raise CampaignFailureTerminalError("terminal_operation_receipt_drift")
    receipt = _read_json(receipt_path, "terminal_operation_receipt")
    response = receipt.get("public_response")
    if receipt.get("schema") != OPERATION_RECEIPT_SCHEMA or not isinstance(response, dict):
        raise CampaignFailureTerminalError("terminal_operation_receipt_invalid")
    return {**response, "replayed": True}


def _commit_operation(
    run_dir: Path,
    *,
    idempotency_key: str,
    request_sha256: str,
    public_response: Mapping[str, Any],
) -> None:
    root, pointer = _operation_paths(run_dir, idempotency_key)
    receipt = {
        "schema": OPERATION_RECEIPT_SCHEMA,
        "idempotency_key": idempotency_key,
        "request_sha256": request_sha256,
        "public_response": dict(public_response),
        "formal_write_count": 0,
    }
    raw = _json_bytes(receipt, pretty=True)
    receipt_sha = hashlib.sha256(raw).hexdigest()
    receipt_ref = Path("receipts") / f"{receipt_sha}.json"
    receipt_path = root / receipt_ref
    if receipt_path.exists() and receipt_path.read_bytes() != raw:
        raise CampaignFailureTerminalError("terminal_operation_receipt_collision")
    if not receipt_path.exists():
        _atomic_write(receipt_path, raw)
    pointer_value = {
        "schema": "morning-campaign-failure-terminal-pointer-v1",
        "idempotency_key": idempotency_key,
        "request_sha256": request_sha256,
        "receipt_ref": str(receipt_ref),
        "receipt_sha256": receipt_sha,
        "formal_write_count": 0,
    }
    pointer_raw = _json_bytes(pointer_value, pretty=True)
    if pointer.exists() and pointer.read_bytes() != pointer_raw:
        raise CampaignFailureTerminalError("terminal_operation_pointer_conflict")
    if not pointer.exists():
        _atomic_write(pointer, pointer_raw)


def _finalize_and_next_unlocked(
    raw: Mapping[str, Any],
    *,
    repo_root: str | Path,
    runtime_root: str | Path,
    package_root: str | Path = DEFAULT_PACKAGE_ROOT,
    draft_root: str | Path = DEFAULT_DRAFT_ROOT,
    display_publisher: Callable[..., dict[str, Any]] | None = None,
    draft_lock_held: bool = False,
) -> dict[str, Any]:
    started = time.perf_counter()
    terminal = _normalize_terminal_input(raw)
    repo = Path(repo_root).expanduser().resolve()
    runtime = _private_root(runtime_root, "runtime_root")
    packages = _private_root(package_root, "package_root")
    drafts = _private_root(draft_root, "draft_root")
    display_path, display, display_sha = _load_display(
        terminal["display_receipt_path"], terminal["display_receipt_sha256"]
    )
    run_dir = _run_dir(runtime, str(display["run_id"]))
    request_material = {
        "terminal": terminal,
        "repo_root": str(repo),
        "runtime_root": str(runtime),
        "package_root": str(packages),
        "draft_root": str(drafts),
    }
    request_sha = _digest(request_material)
    replay = _operation_lookup(
        run_dir,
        idempotency_key=terminal["idempotency_key"],
        request_sha256=request_sha,
    )
    if replay is not None:
        return replay
    draft_started = time.perf_counter()
    identity_sha, _identity = _draft_identity(display, display_sha)
    state_path, _turn_root = _draft_paths(drafts, identity_sha)
    _run_dir_path, runtime_state = _load_runtime_state(runtime, str(display["run_id"]))
    runtime_status = runtime_state.get("status")
    post_advance_recovery = runtime_status in {"active", "completed"}
    if runtime_status == "failure_pending_capture":
        sync_input = {
            "schema": TURN_INPUT_SCHEMA,
            "display_receipt_path": str(display_path),
            "display_receipt_sha256": display_sha,
            "turns": terminal["turns"],
            "idempotency_key": (
                "terminal-sync-" + hashlib.sha256(
                    (terminal["idempotency_key"] + _digest(terminal["turns"])).encode()
                ).hexdigest()[:24]
            ),
        }
        _buffer_turns_internal(
            _normalize_turn_input(sync_input),
            runtime_root=runtime,
            draft_root=drafts,
            draft_lock_held=draft_lock_held,
        )
    elif not post_advance_recovery:
        raise CampaignFailureTerminalError("runtime_not_recoverable")
    draft_state = _read_json(state_path, "draft_state")
    _verify_draft_objects(drafts, draft_state)
    if runtime_status == "failure_pending_capture":
        _pending_run_dir, _pending_state, live_binding = _pending_binding(
            runtime, display
        )
        live_binding["display_receipt_sha256"] = display_sha
        if draft_state.get("campaign_binding") != live_binding:
            raise CampaignFailureTerminalError("draft_pending_binding_drift")
    if (
        draft_state.get("turns") != terminal["turns"]
        or draft_state.get("trace_sha256") != _digest(terminal["turns"])
    ):
        raise CampaignFailureTerminalError("CAMPAIGN_FINAL_TRACE_INCOMPLETE")
    # This guards explicit structured evidence only. Arbitrary natural-language
    # soundness and the actual dependency level remain the caller's responsibility.
    if terminal["prompt_dependency"] == "none" and any(
        turn.get("role") == "assistant" and turn.get("kind") == "hint"
        for turn in draft_state["turns"]
    ):
        raise CampaignFailureTerminalError("prompt_dependency_conflicts_with_hint_trace")
    if _correct_letter(display) != terminal["corrected_response"]:
        raise CampaignFailureTerminalError("corrected_response_incorrect")
    draft_verify_ms = (time.perf_counter() - draft_started) * 1000
    package_started = time.perf_counter()
    proof_path = run_dir / "failure-terminal-v1" / "proofs" / f"{identity_sha}.json"
    if post_advance_recovery and not proof_path.exists():
        raise CampaignFailureTerminalError("post_finalize_proof_missing")
    receipt_proof: Mapping[str, Any]
    if proof_path.exists():
        stored_proof = _read_json(proof_path, "package_capture_proof")
        proof = reopen_campaign_package_capture(
            stored_proof,
            repo_root=repo,
            package_root=packages,
        )
        receipt_proof = stored_proof if post_advance_recovery else proof
    else:
        try:
            proof = commit_campaign_package_capture(
                _package_capture_request(display, draft_state, terminal),
                repo_root=repo,
                package_root=packages,
            )
        except conversation_package.ConversationPackageError as exc:
            raise CampaignFailureTerminalError(
                f"conversation_package_commit_failed:{exc}"
            ) from exc
        _atomic_write(proof_path, _json_bytes(proof, pretty=True))
        receipt_proof = proof
    expected_proof_binding = dict(draft_state["campaign_binding"])
    expected_proof_binding["terminal_trace_sha256"] = draft_state["trace_sha256"]
    if proof.get("campaign_binding") != expected_proof_binding:
        raise CampaignFailureTerminalError("package_capture_proof_drift")
    package_capture_ms = (time.perf_counter() - package_started) * 1000
    receipt_started = time.perf_counter()
    receipt_path, receipt_sha = _campaign_receipt(
        receipt_proof,
        run_dir=run_dir,
        package_root=packages,
        create_if_missing=not post_advance_recovery,
    )
    campaign_receipt_ms = (time.perf_counter() - receipt_started) * 1000
    repair_path = run_dir / "failure-terminal-v1" / "repairs" / f"{identity_sha}.json"
    correction = None
    # Historical already-advanced operations without this successor proof stay
    # read-only. A new operation persists its repair before it can navigate.
    if not post_advance_recovery or repair_path.is_file():
        import review_outcome_hot_408 as review_writer
        try:
            correction = review_writer.record_campaign_repair_hot(
                repo, run_id=display["run_id"], item_id=display["item_id"],
                formal_node_id=display["formal_node_id"], source_id=display["true_item_id"],
                confidence=terminal["confidence"], prompt_dependency=terminal["prompt_dependency"],
                event_time=proof["capture"]["capture_committed_at_utc"],
                session_review_date=display["campaign_date"], capture_id=proof["capture"]["capture_id"],
                package_locator=proof["package"]["locator"], package_sha256=proof["package"]["canonical_sha256"],
            )
        except (review_writer.HotOutcomeError, OSError, ValueError, RuntimeError) as exc:
            raise CampaignFailureTerminalError("terminal_repair_pending:" + str(exc)) from exc
        repair_proof = {"event_id": correction["event"]["event_id"],
                        "receipt_sha256": correction["commit_receipt_sha256"]}
        if repair_path.is_file() and _read_json(repair_path, "terminal_repair_proof") != repair_proof:
            raise CampaignFailureTerminalError("terminal_repair_proof_drift")
        if not repair_path.is_file():
            _atomic_write(repair_path, _json_bytes(repair_proof, pretty=True))
    bridge_started = time.perf_counter()
    try:
        result = campaign_answer.finalize_failure_and_next(
            runtime,
            repo_root=repo,
            display_receipt_path=display_path,
            display_receipt_sha256=display_sha,
            capture_receipt_path=receipt_path,
            capture_receipt_sha256=receipt_sha,
            idempotency_key=terminal["idempotency_key"] + ":campaign-finalize",
            display_publisher=display_publisher,
        )
    except (campaign_answer.CampaignAnswerError, OSError) as exc:
        raise CampaignFailureTerminalError(str(exc)) from exc
    bridge_ms = (time.perf_counter() - bridge_started) * 1000
    proof_timing = proof.get("timing_ms") or {}
    answer_timing = result.get("timing_ms") or {}
    runtime_finalize_ms = float(answer_timing.get("runtime_finalize_ms", 0.0))
    next_display_publish_ms = float(
        answer_timing.get("next_display_publish_ms", 0.0)
    )
    bridge_receipt_ms = max(
        0.0,
        bridge_ms - runtime_finalize_ms - next_display_publish_ms,
    )
    public = {
        **result,
        "schema": PUBLIC_SCHEMA,
        "capture_verified": True,
        "terminal_trace_verified": True,
        "package_reopened": True,
        "capture_binding_reopened": True,
        "hot_joint_reopened": True,
        "timing_ms": {
            "draft_verify_ms": round(draft_verify_ms, 3),
            "package_seal_reopen_ms": proof_timing.get(
                "package_seal_reopen_ms", round(package_capture_ms, 3)
            ),
            "capture_commit_ms": proof_timing.get("capture_commit_ms", 0.0),
            "capture_canonical_reopen_ms": proof_timing.get(
                "capture_canonical_reopen_ms", 0.0
            ),
            "capture_joint_verify_ms": proof_timing.get(
                "capture_joint_verify_ms", 0.0
            ),
            "campaign_receipt_ms": round(campaign_receipt_ms, 3),
            "runtime_finalize_ms": round(runtime_finalize_ms, 3),
            "next_display_publish_ms": round(next_display_publish_ms, 3),
            "bridge_receipt_ms": round(bridge_receipt_ms, 3),
            "entry_total_ms": round(
                (time.perf_counter() - started) * 1000, 3
            ),
        },
        "t9_write_count": 0,
        "obsidian_write_count": 0,
        "navigation_write_count": 0 if result.get("campaign_complete") else 1,
        "duplicate_capture_count": 0,
        "duplicate_display_count": 0,
        "formal_write_count": 0,
        "background_processing": "none",
        "correction_review_commit_verified": correction is not None,
        "correction_review_event_id": correction["event"]["event_id"] if correction else None,
    }
    _commit_operation(
        run_dir,
        idempotency_key=terminal["idempotency_key"],
        request_sha256=request_sha,
        public_response=public,
    )
    return public


@study_commit_boundary
def finalize_and_next(
    raw: Mapping[str, Any],
    *,
    repo_root: str | Path,
    runtime_root: str | Path,
    package_root: str | Path = DEFAULT_PACKAGE_ROOT,
    draft_root: str | Path = DEFAULT_DRAFT_ROOT,
    display_publisher: Callable[..., dict[str, Any]] | None = None,
) -> dict[str, Any]:
    terminal = _normalize_terminal_input(raw)
    runtime = _private_root(runtime_root, "runtime_root")
    _display_path, display, display_sha = _load_display(
        terminal["display_receipt_path"],
        terminal["display_receipt_sha256"],
    )
    run_dir = _run_dir(runtime, str(display["run_id"]))
    operation_root = run_dir / "failure-terminal-v1"
    drafts = _private_root(draft_root, "draft_root")
    identity_sha, _identity = _draft_identity(display, display_sha)
    with _lock(operation_root, "terminal:" + terminal["idempotency_key"]), _lock(drafts, identity_sha):
        return _finalize_and_next_unlocked(
            raw,
            repo_root=repo_root,
            runtime_root=runtime_root,
            package_root=package_root,
            draft_root=draft_root,
            display_publisher=display_publisher,
            draft_lock_held=True,
        )


def _normalize_append_input(raw: Mapping[str, Any], *, terminal: bool) -> dict[str, Any]:
    schema = TERMINAL_APPEND_INPUT_SCHEMA if terminal else APPEND_INPUT_SCHEMA
    compact_fields = {
        "run_id", "item_id", "expected_prior_trace_sha256",
        "expected_prior_turn_count", "new_turns",
    }
    if raw.get("schema") != schema or not compact_fields.issubset(raw) or "turns" in raw:
        raise CampaignFailureTerminalError("append_input_schema_invalid")
    count = raw["expected_prior_turn_count"]
    if type(count) is not int or not 0 <= count <= MAX_TURNS:
        raise CampaignFailureTerminalError("expected_prior_turn_count_invalid")
    new_turns = raw["new_turns"]
    if not isinstance(new_turns, list) or (not new_turns and not terminal):
        raise CampaignFailureTerminalError("new_turns_invalid")
    rows = []
    for ordinal, row in enumerate(new_turns, start=1):
        if not isinstance(row, dict) or "ordinal" in row:
            raise CampaignFailureTerminalError("new_turn_fields_invalid")
        rows.append({**row, "ordinal": ordinal})
    rows = _normalize_turns(rows) if rows else []
    if count + len(rows) > MAX_TURNS:
        raise CampaignFailureTerminalError("turns_invalid")
    # Validate all semantics before any private state or request journal write.
    owner_raw = {key: value for key, value in raw.items() if key not in compact_fields}
    owner_raw["schema"] = TERMINAL_INPUT_SCHEMA if terminal else TURN_INPUT_SCHEMA
    owner_raw["turns"] = rows
    owner = (
        _normalize_terminal_input(owner_raw, allow_empty_turns=True)
        if terminal else _normalize_turn_input(owner_raw)
    )
    owner.pop("turns")
    return {
        **owner,
        "schema": schema,
        "run_id": _safe_id(raw["run_id"], "run_id"),
        "item_id": _safe_id(raw["item_id"], "item_id"),
        "expected_prior_turn_count": count,
        "expected_prior_trace_sha256": _safe_sha(
            raw["expected_prior_trace_sha256"], "expected_prior_trace_sha256"
        ),
        "new_turns": [{key: value for key, value in row.items() if key != "ordinal"} for row in rows],
    }


def _read_append_draft(
    drafts: Path, display: Mapping[str, Any], display_sha: str,
) -> dict[str, Any] | None:
    identity_sha, identity = _draft_identity(display, display_sha)
    state_path, _objects = _draft_paths(drafts, identity_sha)
    if not state_path.exists():
        return None
    state = _read_json(state_path, "draft_state")
    turns = state.get("turns")
    if (
        state.get("schema") != DRAFT_STATE_SCHEMA
        or state.get("identity") != identity
        or not isinstance(turns, list)
        or state.get("turn_count") != len(turns)
        or state.get("trace_sha256") != _digest(turns)
        or _normalize_turns(turns) != turns
    ):
        raise CampaignFailureTerminalError("draft_state_invalid")
    _verify_draft_objects(drafts, state)
    return state


def draft_cursor(
    *, runtime_root: str | Path, draft_root: str | Path,
    display_receipt_path: str | Path, display_receipt_sha256: str,
    run_id: str, item_id: str,
) -> dict[str, Any]:
    """Read only the answer-safe CAS cursor; never create files or locks."""
    _path, display, display_sha = _load_display(display_receipt_path, display_receipt_sha256)
    if display.get("run_id") != run_id or display.get("item_id") != item_id:
        raise CampaignFailureTerminalError("append_display_identity_drift")
    _run, _runtime, binding = _pending_binding(_private_root(runtime_root, "runtime_root"), display)
    binding["display_receipt_sha256"] = display_sha
    state = _read_append_draft(_private_root(draft_root, "draft_root"), display, display_sha)
    if state is not None and state.get("campaign_binding") != binding:
        raise CampaignFailureTerminalError("draft_pending_binding_drift")
    return {
        "run_id": run_id, "item_id": item_id,
        "turn_count": state["turn_count"] if state else 0,
        "trace_sha256": state["trace_sha256"] if state else _digest([]),
        "formal_write_count": 0,
    }


def _assemble_append_input(compact: Mapping[str, Any], prior: list[dict[str, Any]], *, terminal: bool) -> dict[str, Any]:
    full = {key: value for key, value in compact.items() if key not in {
        "run_id", "item_id", "expected_prior_turn_count",
        "expected_prior_trace_sha256", "new_turns",
    }}
    full.update(
        schema=TERMINAL_INPUT_SCHEMA if terminal else TURN_INPUT_SCHEMA,
        idempotency_key="append-owner:" + hashlib.sha256(compact["idempotency_key"].encode()).hexdigest(),
        turns=prior + [
            {**row, "ordinal": len(prior) + index}
            for index, row in enumerate(compact["new_turns"], start=1)
        ],
    )
    return (_normalize_terminal_input if terminal else _normalize_turn_input)(full)


def _append_request(
    raw: Mapping[str, Any], *, terminal: bool,
    repo_root: str | Path, runtime_root: str | Path,
    draft_root: str | Path, package_root: str | Path = DEFAULT_PACKAGE_ROOT,
    display_publisher: Callable[..., dict[str, Any]] | None = None,
) -> dict[str, Any]:
    compact = _normalize_append_input(raw, terminal=terminal)
    repo = Path(repo_root).expanduser().resolve()
    if not repo.is_dir():
        raise CampaignFailureTerminalError("repo_root_missing")
    runtime = _private_root(runtime_root, "runtime_root")
    drafts = _private_root(draft_root, "draft_root")
    packages = _private_root(package_root, "package_root") if terminal else None
    _path, display, display_sha = _load_display(
        compact["display_receipt_path"], compact["display_receipt_sha256"]
    )
    if display.get("run_id") != compact["run_id"] or display.get("item_id") != compact["item_id"]:
        raise CampaignFailureTerminalError("append_display_identity_drift")
    run_dir = _run_dir(runtime, compact["run_id"])
    identity_sha, identity = _draft_identity(display, display_sha)
    key_sha = hashlib.sha256(compact["idempotency_key"].encode()).hexdigest()
    owner_key = "append-owner:" + key_sha
    request_sha = _digest({
        "input": compact, "repo_root": str(repo), "runtime_root": str(runtime),
        "draft_root": str(drafts), "package_root": str(packages) if packages else None,
    })
    request_path = drafts / "append-v1" / "requests" / f"{key_sha}.json"
    # Lock order matches the existing terminal owner. The draft lock spans CAS,
    # durable input sealing, and the owner call, including Capture/navigation.
    owner_lock = _lock(run_dir / "failure-terminal-v1", "terminal:" + owner_key) if terminal else contextlib.nullcontext()
    with _lock(drafts, "append:" + key_sha), owner_lock, _lock(drafts, identity_sha):
        replayed = request_path.exists()
        if replayed:
            saved = _read_json(request_path, "append_request")
            if saved.get("request_sha256") != request_sha:
                raise CampaignFailureTerminalError("idempotency_key_reused_differently")
            full = saved.get("full_input")
            core = {key: value for key, value in saved.items() if key != "record_sha256"}
            if (
                saved.get("schema") != APPEND_REQUEST_SCHEMA
                or saved.get("identity") != identity
                or saved.get("record_sha256") != _digest(core)
                or not isinstance(full, dict)
                or saved.get("full_input_sha256") != _digest(full)
            ):
                raise CampaignFailureTerminalError("append_request_drift")
            normalized = (_normalize_terminal_input if terminal else _normalize_turn_input)(full)
            prior = full["turns"][:compact["expected_prior_turn_count"]]
            if (
                normalized != full
                or len(prior) != compact["expected_prior_turn_count"]
                or _digest(prior) != compact["expected_prior_trace_sha256"]
                or full != _assemble_append_input(compact, prior, terminal=terminal)
                or not isinstance(saved.get("campaign_binding"), dict)
            ):
                raise CampaignFailureTerminalError("append_request_drift")
        else:
            _run, _runtime, binding = _pending_binding(runtime, display)
            binding["display_receipt_sha256"] = display_sha
            state = _read_append_draft(drafts, display, display_sha)
            old_turns = state["turns"] if state else []
            if state is not None and state.get("campaign_binding") != binding:
                raise CampaignFailureTerminalError("draft_pending_binding_drift")
            if (
                len(old_turns) != compact["expected_prior_turn_count"]
                or _digest(old_turns) != compact["expected_prior_trace_sha256"]
            ):
                raise CampaignFailureTerminalError("append_prior_trace_conflict")
            if not old_turns and (
                not compact["new_turns"]
                or compact["new_turns"][0]["role"] != "assistant"
                or compact["new_turns"][0]["kind"] != "utterance"
            ):
                raise CampaignFailureTerminalError("initial_display_turn_required")
            full = _assemble_append_input(compact, old_turns, terminal=terminal)
            core = {
                "schema": APPEND_REQUEST_SCHEMA, "identity": identity,
                "campaign_binding": binding,
                "request_sha256": request_sha, "full_input": full,
                "full_input_sha256": _digest(full), "formal_write_count": 0,
            }
            saved_bytes = _json_bytes({**core, "record_sha256": _digest(core)}, pretty=True)
            if len(saved_bytes) > MAX_JSON_BYTES:
                raise CampaignFailureTerminalError("append_request_too_large")
            # Once sealed, retries always use this exact full input, even when
            # the draft grew or the runtime already advanced before a crash.
            _atomic_write(request_path, saved_bytes)
        if terminal:
            result = _finalize_and_next_unlocked(
                full, repo_root=repo, runtime_root=runtime, package_root=packages,
                draft_root=drafts, display_publisher=display_publisher, draft_lock_held=True,
            )
        else:
            state = _read_append_draft(drafts, display, display_sha) if replayed else None
            if state and state.get("campaign_binding") != saved["campaign_binding"]:
                raise CampaignFailureTerminalError("draft_pending_binding_drift")
            if state and state["turns"][:len(full["turns"])] == full["turns"]:
                # The owner committed this exact prefix before a process stop.
                # Returning it is safe even after another append or navigation.
                result = {
                    "schema": PUBLIC_SCHEMA, "status": "buffered",
                    "run_id": display["run_id"], "item_id": display["item_id"],
                    "runtime_status": "failure_pending_capture", "replayed": True,
                    "capture_write_count": 0, "navigation_write_count": 0,
                    "t9_write_count": 0, "obsidian_write_count": 0,
                    "duplicate_capture_count": 0, "duplicate_display_count": 0,
                    "formal_write_count": 0, "background_processing": "none",
                }
            else:
                result = _buffer_turns_internal(
                    full, runtime_root=runtime, draft_root=drafts, draft_lock_held=True,
                )
        return {**result, "turn_count": len(full["turns"]), "trace_sha256": _digest(full["turns"])}


def append_turns(
    raw: Mapping[str, Any], *, repo_root: str | Path, runtime_root: str | Path,
    draft_root: str | Path = DEFAULT_DRAFT_ROOT,
) -> dict[str, Any]:
    return _append_request(
        raw, terminal=False, repo_root=repo_root, runtime_root=runtime_root, draft_root=draft_root,
    )


@study_commit_boundary
def finalize_appended_and_next(
    raw: Mapping[str, Any], *, repo_root: str | Path, runtime_root: str | Path,
    package_root: str | Path = DEFAULT_PACKAGE_ROOT, draft_root: str | Path = DEFAULT_DRAFT_ROOT,
    display_publisher: Callable[..., dict[str, Any]] | None = None,
) -> dict[str, Any]:
    return _append_request(
        raw, terminal=True, repo_root=repo_root, runtime_root=runtime_root,
        draft_root=draft_root, package_root=package_root, display_publisher=display_publisher,
    )


def _input_json(path: str | Path) -> dict[str, Any]:
    return _read_json(Path(path).expanduser().resolve(), "input_json")


def _paths_from_input(raw: dict[str, Any], *, terminal: bool) -> tuple[dict[str, Any], dict[str, str]]:
    required = {"repo_root", "runtime_root", "draft_root"}
    if terminal:
        required.add("package_root")
    if not required.issubset(raw):
        raise CampaignFailureTerminalError("input_roots_missing")
    paths = {key: str(raw[key]) for key in required}
    payload = {key: value for key, value in raw.items() if key not in required}
    return payload, paths


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    for name in ("buffer-turn", "finalize-and-next", "append-turn", "finalize-appended-and-next", "draft-cursor"):
        child = commands.add_parser(name)
        child.add_argument("--input-json", required=True)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        raw = _input_json(args.input_json)
        payload, paths = _paths_from_input(
            raw, terminal=args.command in {"finalize-and-next", "finalize-appended-and-next"}
        )
        if args.command == "draft-cursor":
            if set(payload) != {"display_receipt_path", "display_receipt_sha256", "run_id", "item_id"}:
                raise CampaignFailureTerminalError("cursor_input_fields_invalid")
            result = draft_cursor(runtime_root=paths["runtime_root"], draft_root=paths["draft_root"], **payload)
        elif args.command in {"buffer-turn", "append-turn"}:
            result = (append_turns if args.command == "append-turn" else buffer_turns)(
                payload,
                repo_root=paths["repo_root"],
                runtime_root=paths["runtime_root"],
                draft_root=paths["draft_root"],
            )
        else:
            result = (finalize_appended_and_next if args.command == "finalize-appended-and-next" else finalize_and_next)(
                payload,
                repo_root=paths["repo_root"],
                runtime_root=paths["runtime_root"],
                package_root=paths["package_root"],
                draft_root=paths["draft_root"],
            )
    except CampaignFailureTerminalError as exc:
        print(
            json.dumps(
                {
                    "schema": PUBLIC_SCHEMA,
                    "status": "blocked",
                    "reason": str(exc),
                    "capture_write_count": 0,
                    "navigation_write_count": 0,
                    "t9_write_count": 0,
                    "obsidian_write_count": 0,
                    "duplicate_capture_count": 0,
                    "duplicate_display_count": 0,
                    "formal_write_count": 0,
                    "background_processing": "none",
                },
                ensure_ascii=False,
                sort_keys=True,
            )
        )
        return 2
    print(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
