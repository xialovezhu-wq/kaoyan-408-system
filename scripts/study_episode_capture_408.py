#!/usr/bin/env python3
"""Seal one complete 408 study episode and commit one verified Capture.

This is a small transaction facade over the existing private conversation
package and Capture hot writer.  It records learner evidence only.  It never
runs review, formal intake, FSRS, personalization refresh, publication, or
background processing.
"""
from __future__ import annotations

import argparse
import contextlib
import copy
import datetime as dt
import fcntl
import hashlib
import json
import mimetypes
import os
import re
import stat
import sys
import tempfile
from pathlib import Path
from typing import Any, Callable, Iterator, Mapping

import capture_hot_writer_408 as hot_writer
import conversation_package_408 as packages
import intake_fact_capture_408 as captures


SCHEMA = "cs408-study-episode-capture-v1"
RECEIPT_SCHEMA = "cs408-study-episode-capture-receipt-v1"
STATUS = "awaiting_web_enhancement"
TIMEZONE = "Asia/Shanghai"
EXECUTION_KINDS = {
    "original_review",
    "concept_supplement",
    "standalone_question",
}
FIRST_RESULTS = {"correct", "incorrect", "blank", "uncertain", "not_observed"}
TERMINAL_STATES = {
    "correct",
    "screened_correct",
    "prompted_correct",
    "wrong",
    "unresolved",
}
PROMPT_DEPENDENCIES = {"none", "minimal", "progressive", "full", "not_observed"}
REASONING_RESULTS = {"sound", "partial", "diverged", "not_observed"}
EPISODE_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,159}$")
MAX_JSON_BYTES = 64 * 1024 * 1024
CANONICAL_REPO = Path("/Users/your-user/Documents/kaoyan-408")
TOP_LEVEL_KEYS = {
    "schema",
    "episode_id",
    "execution_kind",
    "study_date",
    "timezone",
    "source",
    "turns",
    "attachments",
    "observed_outcome",
    "authorization",
    "display_binding",
    "missing_fields",
    "identity_hint",
    "frozen_feedback",
}
OUTCOME_KEYS = {
    "first_response",
    "first_result",
    "final_response",
    "terminal_state",
    "prompt_dependency",
    "reasoning_result",
}


class StudyEpisodeCaptureError(RuntimeError):
    """One episode payload or its durable binding is invalid."""


def _json_bytes(value: Any, *, pretty: bool = True) -> bytes:
    options: dict[str, Any] = {"ensure_ascii": False, "sort_keys": True}
    if pretty:
        options["indent"] = 2
    else:
        options["separators"] = (",", ":")
    try:
        raw = (json.dumps(value, **options) + "\n").encode("utf-8")
    except (TypeError, ValueError) as exc:
        raise StudyEpisodeCaptureError("episode payload is not JSON serializable") from exc
    if len(raw) > MAX_JSON_BYTES:
        raise StudyEpisodeCaptureError("episode payload exceeds the private size ceiling")
    return raw


def _sha256(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


def _sha256_value(value: Any) -> str:
    return _sha256(_json_bytes(value, pretty=False))


def _text(value: object, label: str, *, required: bool = False) -> str:
    if not isinstance(value, str):
        raise StudyEpisodeCaptureError(f"{label} must be a string")
    if required and not value.strip():
        raise StudyEpisodeCaptureError(f"{label} is required")
    if len(value.encode("utf-8")) > MAX_JSON_BYTES:
        raise StudyEpisodeCaptureError(f"{label} exceeds the private size ceiling")
    return value


def _json_object(value: object, label: str, *, required: bool = False) -> dict[str, Any]:
    if not isinstance(value, dict) or (required and not value):
        raise StudyEpisodeCaptureError(f"{label} must be a non-empty object")
    # Round-trip now so mutable caller objects and non-JSON values cannot enter
    # the package after its idempotency material has been computed.
    try:
        copied = json.loads(_json_bytes(value, pretty=False))
    except json.JSONDecodeError as exc:  # pragma: no cover - encoding is local
        raise StudyEpisodeCaptureError(f"{label} could not be frozen") from exc
    if not isinstance(copied, dict):
        raise StudyEpisodeCaptureError(f"{label} must be an object")
    return copied


def _normalize_turns(value: object) -> list[dict[str, Any]]:
    if not isinstance(value, list) or not value:
        raise StudyEpisodeCaptureError("turns must be a non-empty array")
    result: list[dict[str, Any]] = []
    for index, row in enumerate(value):
        if not isinstance(row, dict) or set(row) - {"role", "kind", "text", "observed_at"}:
            raise StudyEpisodeCaptureError(f"turns[{index}] fields are invalid")
        role = str(row.get("role") or "")
        if role == "learner":
            role = "user"
        if role not in {"user", "assistant"}:
            raise StudyEpisodeCaptureError(f"turns[{index}].role is invalid")
        observed_at = row.get("observed_at")
        if observed_at is not None:
            observed_at = _text(observed_at, f"turns[{index}].observed_at", required=True)
        result.append(
            {
                "role": role,
                "kind": _text(row.get("kind", "utterance"), f"turns[{index}].kind", required=True),
                "text": _text(row.get("text"), f"turns[{index}].text", required=True),
                "observed_at": observed_at,
            }
        )
    return result


def _read_attachment_path(value: object, label: str) -> tuple[Path, bytes]:
    path = Path(_text(value, label, required=True)).expanduser()
    try:
        before = path.lstat()
    except OSError as exc:
        raise StudyEpisodeCaptureError(f"{label} is unreadable") from exc
    if stat.S_ISLNK(before.st_mode) or not stat.S_ISREG(before.st_mode):
        raise StudyEpisodeCaptureError(f"{label} must be a non-symlink regular file")
    if not 0 < before.st_size <= packages.MAX_ATTACHMENT_BYTES:
        raise StudyEpisodeCaptureError(f"{label} has an invalid byte count")
    try:
        with path.open("rb") as handle:
            opened = os.fstat(handle.fileno())
            data = handle.read(packages.MAX_ATTACHMENT_BYTES + 1)
            after = os.fstat(handle.fileno())
    except OSError as exc:
        raise StudyEpisodeCaptureError(f"{label} could not be read") from exc
    if (
        opened.st_dev != before.st_dev
        or opened.st_ino != before.st_ino
        or opened.st_size != before.st_size
        or after.st_dev != before.st_dev
        or after.st_ino != before.st_ino
        or after.st_size != before.st_size
        or after.st_mtime_ns != before.st_mtime_ns
        or len(data) != before.st_size
    ):
        raise StudyEpisodeCaptureError(f"{label} drifted during read")
    return path, data


def _normalize_attachments(value: object) -> list[dict[str, Any]]:
    if not isinstance(value, list) or len(value) > packages.MAX_ATTACHMENTS_PER_REVISION:
        raise StudyEpisodeCaptureError("attachments must be a bounded array")
    result: list[dict[str, Any]] = []
    seen: set[tuple[str, str, str]] = set()
    for index, row in enumerate(value):
        if not isinstance(row, dict) or set(row) - {"path", "data", "role", "mime_type", "label", "sha256", "size_bytes"}:
            raise StudyEpisodeCaptureError(f"attachments[{index}] fields are invalid")
        if ("path" in row) == ("data" in row):
            raise StudyEpisodeCaptureError(f"attachments[{index}] requires exactly one of path or data")
        path: Path | None = None
        if "path" in row:
            path, data = _read_attachment_path(row["path"], f"attachments[{index}].path")
        else:
            data = row.get("data")
            if not isinstance(data, bytes) or not 0 < len(data) <= packages.MAX_ATTACHMENT_BYTES:
                raise StudyEpisodeCaptureError(f"attachments[{index}].data is invalid")
        role = _text(row.get("role", "other_attachment"), f"attachments[{index}].role", required=True)
        if row.get("sha256") is not None and row["sha256"] != _sha256(data):
            raise StudyEpisodeCaptureError(f"attachments[{index}].sha256 drifted")
        if row.get("size_bytes") is not None and row["size_bytes"] != len(data):
            raise StudyEpisodeCaptureError(f"attachments[{index}].size_bytes drifted")
        if role not in packages.ATTACHMENT_ROLES:
            raise StudyEpisodeCaptureError(f"attachments[{index}].role is invalid")
        mime_type = _text(
            row.get("mime_type")
            or (mimetypes.guess_type(path.name)[0] if path is not None else None)
            or "application/octet-stream",
            f"attachments[{index}].mime_type",
            required=True,
        ).lower()
        label = _text(row.get("label") or role, f"attachments[{index}].label", required=True)
        digest = _sha256(data)
        identity = (digest, role, label)
        if identity in seen:
            continue
        seen.add(identity)
        result.append(
            {
                "data": data,
                "role": role,
                "mime_type": mime_type,
                "label": label,
                "sha256": digest,
                "byte_count": len(data),
            }
        )
    return result


def _normalize_outcome(value: object) -> dict[str, Any]:
    if not isinstance(value, dict) or set(value) != OUTCOME_KEYS:
        raise StudyEpisodeCaptureError("observed_outcome fields are incomplete or unknown")
    first_response = value.get("first_response")
    final_response = value.get("final_response")
    if first_response is not None:
        first_response = _text(first_response, "observed_outcome.first_response", required=True)
    if final_response is not None:
        final_response = _text(final_response, "observed_outcome.final_response", required=True)
    first_result = str(value.get("first_result") or "")
    terminal_state = str(value.get("terminal_state") or "")
    prompt = str(value.get("prompt_dependency") or "")
    reasoning = str(value.get("reasoning_result") or "")
    if first_result not in FIRST_RESULTS:
        raise StudyEpisodeCaptureError("observed_outcome.first_result is invalid")
    if terminal_state not in TERMINAL_STATES:
        raise StudyEpisodeCaptureError("observed_outcome.terminal_state is invalid")
    if prompt not in PROMPT_DEPENDENCIES:
        raise StudyEpisodeCaptureError("observed_outcome.prompt_dependency is invalid")
    if reasoning not in REASONING_RESULTS:
        raise StudyEpisodeCaptureError("observed_outcome.reasoning_result is invalid")
    if first_result == "not_observed" and first_response is not None:
        raise StudyEpisodeCaptureError("unobserved first response cannot contain invented text")
    if first_result != "not_observed" and first_response is None:
        raise StudyEpisodeCaptureError("observed first result requires the private first response")
    if terminal_state in {"correct", "screened_correct"} and prompt != "none":
        raise StudyEpisodeCaptureError("independent terminal state requires prompt_dependency=none")
    if terminal_state == "prompted_correct" and prompt not in {"minimal", "progressive", "full"}:
        raise StudyEpisodeCaptureError("prompted_correct requires an observed prompt dependency")
    if terminal_state != "unresolved" and final_response is None:
        raise StudyEpisodeCaptureError("terminal outcome requires the private final response")
    return {
        "first_response": first_response,
        "first_result": first_result,
        "final_response": final_response,
        "terminal_state": terminal_state,
        "prompt_dependency": prompt,
        "reasoning_result": reasoning,
    }


def _normalize_identity(value: object) -> dict[str, str]:
    if value is None:
        return {"status": "unknown", "mode": "unknown", "basis": "episode-package-only"}
    if not isinstance(value, dict) or set(value) - captures.IDENTITY_KEYS:
        raise StudyEpisodeCaptureError("identity_hint fields are invalid")
    return {str(key): str(child) for key, child in value.items()}


def _normalize_payload(raw: Mapping[str, Any]) -> dict[str, Any]:
    if not isinstance(raw, Mapping):
        raise StudyEpisodeCaptureError("payload must be an object")
    value = dict(raw)
    if set(value) - TOP_LEVEL_KEYS:
        raise StudyEpisodeCaptureError(f"payload contains unknown fields: {sorted(set(value) - TOP_LEVEL_KEYS)}")
    required = {
        "schema", "episode_id", "execution_kind", "study_date", "source", "turns",
        "attachments", "observed_outcome", "authorization", "display_binding",
    }
    if not required <= set(value):
        raise StudyEpisodeCaptureError(f"payload is missing fields: {sorted(required - set(value))}")
    if value.get("schema") != SCHEMA:
        raise StudyEpisodeCaptureError(f"schema must be {SCHEMA}")
    episode_id = str(value.get("episode_id") or "")
    if not EPISODE_ID_RE.fullmatch(episode_id):
        raise StudyEpisodeCaptureError("episode_id is invalid")
    execution_kind = str(value.get("execution_kind") or "")
    if execution_kind not in EXECUTION_KINDS:
        raise StudyEpisodeCaptureError("execution_kind is invalid")
    study_date = str(value.get("study_date") or "")
    try:
        dt.date.fromisoformat(study_date)
    except ValueError as exc:
        raise StudyEpisodeCaptureError("study_date must be a real YYYY-MM-DD date") from exc
    if value.get("timezone", TIMEZONE) != TIMEZONE:
        raise StudyEpisodeCaptureError(f"timezone must be {TIMEZONE}")
    source = _json_object(value.get("source"), "source", required=True)
    missing_external_source = not source.get("source_id") and execution_kind == "standalone_question"
    if missing_external_source:
        source["source_id"] = "local-episode:" + episode_id
        source["source_id_origin"] = "local_episode_key_not_external_question_identity"
        source["external_source_id"] = None
    source_id = _text(source.get("source_id"), "source.source_id", required=True)
    source["source_id"] = source_id
    for field in ("session_id", "item_id"):
        if source.get(field) is not None:
            source[field] = _text(source[field], f"source.{field}", required=True)
    missing = value.get("missing_fields", [])
    if not isinstance(missing, list) or not all(isinstance(item, str) for item in missing):
        raise StudyEpisodeCaptureError("missing_fields must be a string array")
    normalized_missing = sorted({item for item in missing if item})
    if missing_external_source and "external_source_id" not in normalized_missing:
        normalized_missing.append("external_source_id")
    attachments = _normalize_attachments(value.get("attachments"))
    normalized = {
        "schema": SCHEMA,
        "episode_id": episode_id,
        "execution_kind": execution_kind,
        "study_date": study_date,
        "timezone": TIMEZONE,
        "source": source,
        "turns": _normalize_turns(value.get("turns")),
        "attachments": attachments,
        "observed_outcome": _normalize_outcome(value.get("observed_outcome")),
        "authorization": _json_object(value.get("authorization"), "authorization", required=True),
        "display_binding": _json_object(value.get("display_binding"), "display_binding", required=True),
        "missing_fields": normalized_missing,
        "identity_hint": _normalize_identity(value.get("identity_hint")),
        "frozen_feedback": _text(value.get("frozen_feedback", ""), "frozen_feedback"),
    }
    return normalized


def _operation_root(repo: Path) -> Path:
    return captures.capture_root(repo) / "study-episode-capture-v1"


def _default_package_root(repo: Path) -> Path:
    return (
        packages.DEFAULT_ROOT
        if repo == CANONICAL_REPO.resolve()
        else repo / ".private" / "conversation-packages"
    )


def _atomic_write(path: Path, raw: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
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


@contextlib.contextmanager
def _operation_lock(root: Path, episode_hash: str) -> Iterator[None]:
    lock_dir = root / ".locks"
    lock_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    path = lock_dir / f"{episode_hash}.lock"
    descriptor = os.open(path, os.O_CREAT | os.O_RDWR, 0o600)
    try:
        os.fchmod(descriptor, 0o600)
        fcntl.flock(descriptor, fcntl.LOCK_EX)
        yield
    finally:
        fcntl.flock(descriptor, fcntl.LOCK_UN)
        os.close(descriptor)


def _attachment_material(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [
        {key: row[key] for key in ("role", "mime_type", "label", "sha256", "byte_count")}
        for row in rows
    ]


def _request_material(normalized: dict[str, Any]) -> dict[str, Any]:
    return {
        **{key: copy.deepcopy(value) for key, value in normalized.items() if key != "attachments"},
        "attachments": _attachment_material(normalized["attachments"]),
    }


def _source_value(normalized: dict[str, Any]) -> dict[str, Any]:
    return {
        "entry_kind": "study_episode_capture",
        "episode_id": normalized["episode_id"],
        "execution_kind": normalized["execution_kind"],
        "source": normalized["source"],
        "observed_outcome": normalized["observed_outcome"],
        "authorization": normalized["authorization"],
        "display_binding": normalized["display_binding"],
        "requires_web_enhancement": True,
        "awaiting_web_enhancement": True,
    }


def _expected_turns(normalized: dict[str, Any]) -> list[dict[str, Any]]:
    rows = [
        {"ordinal": index, **copy.deepcopy(row)}
        for index, row in enumerate(normalized["turns"], start=1)
    ]
    if normalized["frozen_feedback"]:
        rows.append(
            {
                "ordinal": len(rows) + 1,
                "role": "assistant",
                "kind": "frozen_feedback",
                "text": normalized["frozen_feedback"],
                "observed_at": None,
            }
        )
    return rows


def _package_locator(normalized: dict[str, Any]) -> tuple[str, str, str, str | None]:
    episode_id = normalized["episode_id"]
    source_id = f"EPISODE-{_sha256(episode_id.encode('utf-8'))[:24]}"
    item_id = str(normalized["source"].get("item_id") or episode_id)
    session_id = normalized["source"].get("session_id")
    package_id = packages.package_id_for(
        study_date=normalized["study_date"],
        session_id=session_id,
        item_id=item_id,
        source_id=source_id,
    )
    return packages.LOCATOR_PREFIX + package_id, source_id, item_id, session_id


def _seal_or_reopen_package(normalized: dict[str, Any], private_root: Path) -> dict[str, Any]:
    locator, source_id, item_id, session_id = _package_locator(normalized)
    package_id = locator.removeprefix(packages.LOCATOR_PREFIX)
    package_dir = private_root / normalized["study_date"] / package_id
    if not package_dir.exists():
        packages.record_package(
            study_date=normalized["study_date"],
            session_id=session_id,
            item_id=item_id,
            source_id=source_id,
            source=_source_value(normalized),
            conversation=normalized["turns"],
            frozen_feedback=normalized["frozen_feedback"],
            attachments=[
                {key: row[key] for key in ("data", "role", "mime_type", "label")}
                for row in normalized["attachments"]
            ],
            missing_fields=normalized["missing_fields"],
            seal=True,
            private_root=private_root,
        )
    opened = packages.verify_package_binding(locator, require_sealed=True, private_root=private_root)
    if (
        opened["source"].get("source") != _source_value(normalized)
        or opened["conversation"].get("turns") != _expected_turns(normalized)
        or opened["manifest"].get("missing_fields") != normalized["missing_fields"]
        or opened["manifest"].get("formal_write_count") != 0
        or opened["receipt"].get("formal_write_count") != 0
    ):
        raise StudyEpisodeCaptureError("conversation package binding drifted")
    actual_attachments = [
        {key: row.get(key) for key in ("role", "mime_type", "label", "sha256", "byte_count")}
        for row in opened["manifest"].get("attachments") or []
    ]
    if actual_attachments != _attachment_material(normalized["attachments"]):
        raise StudyEpisodeCaptureError("conversation package attachment binding drifted")
    return opened


def _capture_payload(normalized: dict[str, Any], opened: dict[str, Any]) -> dict[str, Any]:
    source = normalized["source"]
    source_facts: dict[str, str] = {
        "source_id": str(source["source_id"]),
        "details_id": "episode-" + _sha256(normalized["episode_id"].encode("utf-8"))[:20],
        "episode_id": normalized["episode_id"],
        "execution_kind": normalized["execution_kind"],
        "requires_web_enhancement": "true",
        "awaiting_web_enhancement": "true",
    }
    for field in ("year", "question_type"):
        if isinstance(source.get(field), str) and source[field].strip():
            source_facts[field] = source[field]
    if source.get("subject") in {"DS", "CO", "OS", "CN", "未确认"}:
        source_facts["subject"] = str(source["subject"])
    raw = {
        "schema": captures.SCHEMA,
        "study_date": normalized["study_date"],
        "timezone": TIMEZONE,
        "idempotency_key": "study-episode:" + normalized["episode_id"],
        "stable_evidence_refs": [
            {
                "kind": captures.CONVERSATION_PACKAGE_REF_KIND,
                "locator": opened["receipt"]["locator"],
                "sha256": opened["canonical_sha256"],
            }
        ],
        "source_facts": source_facts,
        "user_facts": {
            "user_error_entry": "未观察到",
            "user_error_provenance": "not_observed",
        },
        "identity_hint": normalized["identity_hint"],
        "answer_safe_context_anchor": "cs408-study-episode-private-package",
        "missing_fields": normalized["missing_fields"],
        "formalization_authorized": True,
        "authorization_policy": captures.WEB_ENHANCEMENT_PENDING_POLICY,
    }
    try:
        return captures._validate_capture_payload(raw)
    except captures.CaptureError as exc:
        raise StudyEpisodeCaptureError(f"Capture payload is invalid: {exc}") from exc


def _capture_paths(repo: Path) -> tuple[Path, Path, Path]:
    root = captures.capture_root(repo)
    return root / "events.jsonl", root / "state.json", root / ".capture-hot-writer-v1"


def _commit_capture(repo: Path, payload: dict[str, Any], fault: Callable[[str], None] | None) -> dict[str, Any]:
    ledger, state, hot_root = _capture_paths(repo)
    if not ledger.is_file() or not state.is_file():
        raise StudyEpisodeCaptureError("verified Capture hot writer is not prepared")
    try:
        with hot_writer.capture_ledger_lock(ledger) as lock_token:
            result = hot_writer.capture_fact(
                ledger,
                state,
                hot_root,
                lock_token=lock_token,
                validated_payload=payload,
                fault_injector=fault,
            )
        canonical = hot_writer.canonical_fact_event_from_payload(payload)
        verified = hot_writer.verify_joint_commit(
            ledger, state, hot_root, event_id=canonical["event_id"]
        )
        binding = captures.reopen_capture_binding(
            canonical["capture_id"],
            expected_payload_sha256=canonical["payload_sha256"],
            expected_idempotency_key=payload["idempotency_key"],
            repo_root=repo,
            include_payload_proof=True,
        )
    except (hot_writer.CaptureHotWriterError, captures.CaptureError, OSError) as exc:
        raise StudyEpisodeCaptureError(f"verified Capture commit failed: {exc}") from exc
    if (
        result.get("status") not in {"CAPTURED", "ALREADY_COMMITTED"}
        or verified.get("status") != "PASS"
        or verified.get("event_type") != "fact_captured"
        or verified.get("capture_id") != canonical["capture_id"]
        or binding.get("capture_id") != canonical["capture_id"]
        or binding.get("quality_status") != captures.AWAITING_WEB_ENHANCEMENT_STATUS
        or binding.get("formalization_authorized") is not True
        or any(row.get("formal_write_count") != 0 for row in (result, verified, binding))
    ):
        raise StudyEpisodeCaptureError("Capture did not produce a durable joint proof")
    return {"result": result, "verified": verified, "binding": binding, "canonical": canonical}


def _receipt(
    normalized: dict[str, Any],
    opened: dict[str, Any],
    committed: dict[str, Any],
    request_binding_sha256: str,
) -> dict[str, Any]:
    canonical = committed["canonical"]
    verified = committed["verified"]
    return {
        "schema": RECEIPT_SCHEMA,
        "status": STATUS,
        "episode_id": normalized["episode_id"],
        "execution_kind": normalized["execution_kind"],
        "study_date": normalized["study_date"],
        "request_payload_sha256": _sha256_value(_request_material(normalized)),
        "episode_request_binding_sha256": request_binding_sha256,
        "conversation_package_locator": opened["receipt"]["locator"],
        "conversation_package_sha256": opened["canonical_sha256"],
        "capture_id": canonical["capture_id"],
        "capture_payload_sha256": canonical["payload_sha256"],
        "capture_event_id": canonical["event_id"],
        "capture_event_sha256": verified["event_sha256"],
        "capture_joint_receipt_sha256": verified["receipt_sha256"],
        "capture_joint_manifest_sha256": verified["joint_manifest_sha256"],
        "capture_bounded_manifest_sha256": verified["bounded_manifest_sha256"],
        "bounded_manifest_sha256": verified["bounded_manifest_sha256"],
        "package_verified": True,
        "capture_verified": True,
        "requires_web_enhancement": True,
        "awaiting_web_enhancement": True,
        "authorization_policy": captures.WEB_ENHANCEMENT_PENDING_POLICY,
        "formal_write_count": 0,
        "fsrs_write_count": 0,
        "personal_summary_write_count": 0,
        "background_processing": "none",
    }


def record_episode(
    repo: str | Path,
    payload: Mapping[str, Any],
    *,
    package_root: str | Path | None = None,
    fault_injector: Callable[[str], None] | None = None,
) -> dict[str, Any]:
    """Seal and Capture one terminal episode; exact retries are idempotent."""

    repo_path = Path(repo).expanduser().resolve()
    if not repo_path.is_dir():
        raise StudyEpisodeCaptureError("repo does not exist")
    private_root = Path(
        package_root if package_root is not None else _default_package_root(repo_path)
    ).expanduser().resolve()
    normalized = _normalize_payload(payload)
    request_material = _request_material(normalized)
    request_sha = _sha256_value(request_material)
    episode_hash = _sha256(normalized["episode_id"].encode("utf-8"))
    root = _operation_root(repo_path)
    binding_path = root / "episode-bindings" / f"{episode_hash}.json"
    receipt_path = root / "receipts" / f"{episode_hash}.json"
    binding = {
        "schema": "cs408-study-episode-request-binding-v1",
        "episode_id": normalized["episode_id"],
        "execution_kind": normalized["execution_kind"],
        "study_date": normalized["study_date"],
        "request_payload_sha256": request_sha,
        "formal_write_count": 0,
    }
    binding_raw = _json_bytes(binding)
    with _operation_lock(root, episode_hash):
        if binding_path.exists():
            if binding_path.is_symlink() or not binding_path.is_file() or binding_path.read_bytes() != binding_raw:
                raise StudyEpisodeCaptureError("episode_id is already bound to different episode bytes")
        else:
            _atomic_write(binding_path, binding_raw)
        if receipt_path.exists():
            verified = verify_episode_capture(repo_path, receipt_path, package_root=private_root)
            return {**verified, "created": False}
        opened = _seal_or_reopen_package(normalized, private_root)
        if fault_injector:
            fault_injector("after_package")
        committed = _commit_capture(repo_path, _capture_payload(normalized, opened), fault_injector)
        receipt = _receipt(
            normalized,
            opened,
            committed,
            _sha256(binding_raw),
        )
        receipt_raw = _json_bytes(receipt)
        if receipt_path.exists():
            if receipt_path.is_symlink() or not receipt_path.is_file() or receipt_path.read_bytes() != receipt_raw:
                raise StudyEpisodeCaptureError("episode receipt conflicts with durable state")
        else:
            if fault_injector:
                fault_injector("before_receipt")
            _atomic_write(receipt_path, receipt_raw)
    verified = verify_episode_capture(repo_path, receipt_path, package_root=private_root)
    return {**verified, "created": committed["result"].get("status") == "CAPTURED"}


def _read_receipt(path: str | Path) -> tuple[Path, dict[str, Any], str]:
    receipt_path = Path(path).expanduser().resolve()
    if receipt_path.is_symlink() or not receipt_path.is_file():
        raise StudyEpisodeCaptureError("episode receipt is missing or unsafe")
    raw = receipt_path.read_bytes()
    try:
        receipt = json.loads(raw)
    except (UnicodeError, json.JSONDecodeError) as exc:
        raise StudyEpisodeCaptureError("episode receipt is unreadable") from exc
    if not isinstance(receipt, dict) or receipt.get("schema") != RECEIPT_SCHEMA:
        raise StudyEpisodeCaptureError("episode receipt schema is invalid")
    return receipt_path, receipt, _sha256(raw)


def verify_episode_capture(
    repo: str | Path,
    receipt_ref: str | Path,
    *,
    package_root: str | Path | None = None,
) -> dict[str, Any]:
    """Reopen one receipt and verify its package, Capture, and joint commit."""

    repo_path = Path(repo).expanduser().resolve()
    private_root = Path(
        package_root if package_root is not None else _default_package_root(repo_path)
    ).expanduser().resolve()
    receipt_path, receipt, receipt_sha = _read_receipt(receipt_ref)
    episode_hash = _sha256(str(receipt.get("episode_id") or "").encode("utf-8"))
    request_binding_path = (
        _operation_root(repo_path) / "episode-bindings" / f"{episode_hash}.json"
    )
    if request_binding_path.is_symlink() or not request_binding_path.is_file():
        raise StudyEpisodeCaptureError("episode request binding is missing or unsafe")
    binding_raw = request_binding_path.read_bytes()
    try:
        request_binding = json.loads(binding_raw)
    except (UnicodeError, json.JSONDecodeError) as exc:
        raise StudyEpisodeCaptureError("episode request binding is unreadable") from exc
    expected_request_binding = {
        "schema": "cs408-study-episode-request-binding-v1",
        "episode_id": receipt.get("episode_id"),
        "execution_kind": receipt.get("execution_kind"),
        "study_date": receipt.get("study_date"),
        "request_payload_sha256": receipt.get("request_payload_sha256"),
        "formal_write_count": 0,
    }
    if (
        request_binding != expected_request_binding
        or _sha256(binding_raw)
        != receipt.get("episode_request_binding_sha256")
    ):
        raise StudyEpisodeCaptureError("episode request binding drifted")
    opened = packages.verify_package_binding(
        receipt["conversation_package_locator"],
        expected_canonical_sha256=receipt["conversation_package_sha256"],
        require_sealed=True,
        private_root=private_root,
    )
    source = opened["source"].get("source") or {}
    if (
        source.get("episode_id") != receipt.get("episode_id")
        or source.get("execution_kind") != receipt.get("execution_kind")
        or source.get("requires_web_enhancement") is not True
        or source.get("awaiting_web_enhancement") is not True
    ):
        raise StudyEpisodeCaptureError("episode receipt/package identity drifted")
    binding = captures.reopen_capture_binding(
        receipt["capture_id"],
        expected_payload_sha256=receipt["capture_payload_sha256"],
        expected_idempotency_key="study-episode:" + receipt["episode_id"],
        repo_root=repo_path,
        include_payload_proof=True,
    )
    ledger, state, hot_root = _capture_paths(repo_path)
    verified = hot_writer.verify_joint_commit(
        ledger, state, hot_root, event_id=receipt["capture_event_id"]
    )
    expected_ref = {
        "kind": captures.CONVERSATION_PACKAGE_REF_KIND,
        "locator": receipt["conversation_package_locator"],
        "sha256": receipt["conversation_package_sha256"],
    }
    if (
        binding.get("stable_evidence_refs") != [expected_ref]
        or binding.get("source_facts", {}).get("episode_id")
        != receipt.get("episode_id")
        or binding.get("source_facts", {}).get("execution_kind")
        != receipt.get("execution_kind")
        or binding.get("source_facts", {}).get("requires_web_enhancement")
        != "true"
        or binding.get("source_facts", {}).get("awaiting_web_enhancement")
        != "true"
        or binding.get("authorization_policy")
        != captures.WEB_ENHANCEMENT_PENDING_POLICY
        or binding.get("quality_status") not in {
            captures.AWAITING_WEB_ENHANCEMENT_STATUS, captures.PENDING_FORMAL_STATUS,
            "curating", "curated", "already_current", "concept_only", "needs_user",
            "needs_user_resolved", "curation_failed", "user_removed",
        }
        or binding.get("event_id") != receipt.get("capture_event_id")
        or binding.get("event_sha256") != verified.get("canonical_event_sha256")
        or verified.get("status") != "PASS"
        or verified.get("event_sha256") != receipt.get("capture_event_sha256")
        or verified.get("receipt_sha256") != receipt.get("capture_joint_receipt_sha256")
        # The current joint/index heads advance on later Captures and formal
        # results. The exact event and its immutable commit receipt above must
        # still match; the historical head is not the current head.
        or any(not re.fullmatch(r"[0-9a-f]{64}", str(receipt.get(field) or "")) for field in (
            "capture_joint_manifest_sha256", "capture_bounded_manifest_sha256"
        ))
        or receipt.get("status") != STATUS
        or receipt.get("authorization_policy") != captures.WEB_ENHANCEMENT_PENDING_POLICY
        or any(receipt.get(field) != 0 for field in (
            "formal_write_count", "fsrs_write_count", "personal_summary_write_count"
        ))
        or receipt.get("background_processing") != "none"
    ):
        raise StudyEpisodeCaptureError("episode durable proof drifted")
    return {
        **receipt,
        "receipt_ref": str(receipt_path),
        "receipt_sha256": receipt_sha,
        "package_verified": True,
        "capture_verified": True,
        "current_quality_status": binding.get("quality_status"),
        "currently_awaiting_web_enhancement": binding.get("quality_status") == captures.AWAITING_WEB_ENHANCEMENT_STATUS,
    }


def _load_json_file(path: str | Path) -> dict[str, Any]:
    source = Path(path).expanduser()
    if source.is_symlink() or not source.is_file() or source.stat().st_size > MAX_JSON_BYTES:
        raise StudyEpisodeCaptureError("input JSON is missing, unsafe, or too large")
    try:
        value = json.loads(source.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise StudyEpisodeCaptureError("input JSON is unreadable") from exc
    if not isinstance(value, dict):
        raise StudyEpisodeCaptureError("input JSON must contain an object")
    return value


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo", default=".")
    parser.add_argument("--package-root", default=str(packages.DEFAULT_ROOT))
    subparsers = parser.add_subparsers(dest="operation", required=True)
    record = subparsers.add_parser("record")
    record.add_argument("--input-json", required=True)
    verify = subparsers.add_parser("verify")
    verify.add_argument("--receipt", required=True)
    args = parser.parse_args()
    try:
        result = (
            record_episode(
                args.repo,
                _load_json_file(args.input_json),
                package_root=args.package_root,
            )
            if args.operation == "record"
            else verify_episode_capture(
                args.repo, args.receipt, package_root=args.package_root
            )
        )
    except (
        StudyEpisodeCaptureError,
        packages.ConversationPackageError,
        captures.CaptureError,
        hot_writer.CaptureHotWriterError,
        OSError,
    ) as exc:
        print(f"BLOCKED: {exc}", file=sys.stderr)
        return 2
    print(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())


__all__ = [
    "EXECUTION_KINDS",
    "RECEIPT_SCHEMA",
    "SCHEMA",
    "STATUS",
    "StudyEpisodeCaptureError",
    "record_episode",
    "verify_episode_capture",
]
