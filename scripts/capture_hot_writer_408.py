#!/usr/bin/env python3
"""Bounded hot writer for one already-bound 408 ``fact_captured`` event.

The caller must hold the capture ledger lock.  This adapter never scans review
or morning-session ledgers and never writes any formal database surface.  Cold
bootstrap adopts the current capture ledger/state; the hot path then performs a
joint ledger, state, exact-index, and content-addressed receipt commit.
"""

from __future__ import annotations

import copy
import datetime as dt
import fcntl
import hashlib
import hmac
import json
import os
import re
import stat
import tempfile
import threading
import time
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Iterator, Mapping

import bounded_jsonl_index_408 as bounded_jsonl
import capture_commit_index_408 as capture_commit
import intake_fact_capture_408 as capture_model


JOINT_MANIFEST_SCHEMA = "capture_hot_joint_manifest_v1"
PENDING_SCHEMA = "capture_hot_pending_commit_v1"
LINK_SCHEMA = "capture_hot_receipt_link_v1"
HOT_RESULT_SCHEMA = "capture_hot_result_v1"
AUTH_RECEIPT_SCHEMA = "capture_authorization_commit_receipt_v1"
AUTH_RECEIPT_KIND = "capture_authorization_event_commit"
NEUTRAL_RECEIPT_SCHEMA = "capture_neutral_save_commit_receipt_v1"
NEUTRAL_RECEIPT_KIND = "capture_neutral_save_event_commit"
AUTHORIZATION_PROOF_SCHEMA = "managed_408_capture_authorization_proof_v1"
KEY_FILENAME = ".capture-hot-writer.key"
JOINT_MANIFEST_FILENAME = "joint-manifest.json"
PENDING_FILENAME = "pending-joint-commit.json"
JSONL_INDEX_DIRNAME = "bounded-jsonl-index"
BOOTSTRAP_COMMIT_DIRNAME = "capture-commit-bootstrap"
HOT_RECEIPT_DIRNAME = "capture-receipts"
HOT_LINK_DIRNAME = "capture-receipt-links"
PROBE_EVENT_ID = "CAPTURE-HOT-INDEX-PROBE-NOT-FOUND"

DEFAULT_MAX_STATE_BYTES = 16 * 1024 * 1024
HOT_LEDGER_CHUNK_SIZE = 4 * 1024
MAX_JOINT_MANIFEST_BYTES = 1024 * 1024
MAX_PENDING_BYTES = 256 * 1024
MAX_LINK_BYTES = 128 * 1024
MAX_RECEIPT_BYTES = 128 * 1024
SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
FACT_EVENT_KEYS = {
    "schema",
    "event_type",
    "event_id",
    "idempotency_key",
    "created_at",
    "capture_id",
    "payload_sha256",
    "capture",
}
AUTH_EVENT_KEYS = {
    "schema",
    "event_type",
    "event_id",
    "idempotency_key",
    "created_at",
    "capture_id",
    "authorization_proof",
}
LEGACY_AUTH_EVENT_KEYS = AUTH_EVENT_KEYS - {"authorization_proof"}
NEUTRAL_EVENT_KEYS = {
    "schema",
    "event_type",
    "event_id",
    "idempotency_key",
    "created_at",
    "payload_sha256",
    "neutral_save_id",
    "study_date",
    "timezone",
    "review_event_id",
    "review_event_sha256",
    "source_transaction_kind",
    "source_transaction_attestation_sha256",
    "authorization_message_sha256",
    "authorization_binding_sha256",
    "save_request_id",
    "display_receipt_sha256",
    "save_intent_receipt_sha256",
    "save_intent_receipt_ref",
    "authorization_message_size_bytes",
    "formalization_authorized",
    "curation_eligible",
    "status",
}
MANAGED_AUTHORIZATION_PROOF_KEYS = {
    "schema",
    "proof_kind",
    "study_authorization_receipt_ref",
    "study_authorization_receipt_sha256",
    "intent_receipt_ref",
    "intent_receipt_sha256",
    "request_id",
    "display_receipt_sha256",
    "authorization_message_sha256",
    "authorization_message_size_bytes",
    "authorization_binding_sha256",
    "authorization_body_stored",
    "formal_write_count",
}
EXISTING_CAPTURE_AUTHORIZATION_PROOF_KEYS = {
    "schema",
    "proof_kind",
    "existing_capture_intent_ref",
    "existing_capture_intent_sha256",
    "capture_id",
    "fact_event_id",
    "fact_event_sha256",
    "fact_receipt_sha256",
    "request_id",
    "authorization_message_sha256",
    "authorization_message_size_bytes",
    "authorization_binding_sha256",
    "authorization_body_stored",
    "formal_write_count",
}
EXISTING_CAPTURE_AUTHORIZATION_BINDING_SCHEMA = (
    "managed_408_existing_capture_authorization_binding_v1"
)


class CaptureHotWriterError(RuntimeError):
    """The hot capture or its joint evidence failed closed."""


FaultInjector = Callable[[str], None]
_TOKEN_SECRET = object()


@dataclass
class CaptureLedgerLockToken:
    """Proof object that remains valid only inside ``capture_ledger_lock``."""

    ledger_path: Path
    owner_pid: int
    owner_thread_id: int
    _secret: object
    active: bool = True


def _canonical(value: Any) -> bytes:
    return json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")


def _pretty(value: Any) -> bytes:
    return (
        json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    ).encode("utf-8")


def _ledger_line(event: Mapping[str, Any]) -> bytes:
    # Match the current capture ledger writer's stable line serialization.
    return (
        json.dumps(event, ensure_ascii=False, sort_keys=True) + "\n"
    ).encode("utf-8")


def _state_bytes(state: Mapping[str, Any]) -> bytes:
    return _pretty(state)


def _sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _sha256_value(value: Any) -> str:
    return _sha256_bytes(_canonical(value))


def _path_sha256(path: Path) -> str:
    return _sha256_bytes(str(path.resolve()).encode("utf-8"))


def _require_sha(value: object, label: str) -> str:
    text = str(value or "")
    if not SHA256_RE.fullmatch(text):
        raise CaptureHotWriterError(f"{label} must be a lowercase SHA-256")
    return text


def _regular_file(path: Path, label: str) -> os.stat_result:
    try:
        info = path.lstat()
    except OSError as exc:
        raise CaptureHotWriterError(f"{label} is unavailable") from exc
    if stat.S_ISLNK(info.st_mode) or not stat.S_ISREG(info.st_mode):
        raise CaptureHotWriterError(f"{label} must be a regular non-symlink file")
    return info


def _read_bounded(path: Path, *, maximum: int, label: str) -> bytes:
    info = _regular_file(path, label)
    if info.st_size > maximum:
        raise CaptureHotWriterError(
            f"{label} exceeds bounded size: {info.st_size} > {maximum}"
        )
    try:
        value = path.read_bytes()
    except OSError as exc:
        raise CaptureHotWriterError(f"failed to read {label}") from exc
    if len(value) != info.st_size:
        raise CaptureHotWriterError(f"{label} changed while being read")
    return value


def _read_json_bounded(path: Path, *, maximum: int, label: str) -> dict[str, Any]:
    raw = _read_bounded(path, maximum=maximum, label=label)
    try:
        value = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise CaptureHotWriterError(f"{label} is not valid UTF-8 JSON") from exc
    if not isinstance(value, dict):
        raise CaptureHotWriterError(f"{label} must be a JSON object")
    return value


def _atomic_write(path: Path, payload: bytes, *, mode: int = 0o600) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    descriptor, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    tmp = Path(temporary)
    try:
        os.fchmod(descriptor, mode)
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp, path)
        _fsync_directory(path.parent)
    finally:
        if tmp.exists():
            tmp.unlink()


def _fsync_directory(path: Path) -> None:
    descriptor = os.open(path, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _load_or_create_key(root: Path, *, create: bool) -> bytes:
    path = root / KEY_FILENAME
    if not path.exists():
        if not create:
            raise CaptureHotWriterError("capture hot-writer key is missing")
        root.mkdir(parents=True, exist_ok=True, mode=0o700)
        descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        try:
            os.write(descriptor, os.urandom(32))
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
        _fsync_directory(root)
    info = _regular_file(path, "capture hot-writer key")
    if info.st_mode & 0o077:
        raise CaptureHotWriterError("capture hot-writer key permissions are unsafe")
    key = path.read_bytes()
    if len(key) != 32:
        raise CaptureHotWriterError("capture hot-writer key is invalid")
    return key


def _sign(value: Mapping[str, Any], key: bytes) -> dict[str, Any]:
    core = dict(value)
    core.pop("hmac_sha256", None)
    return {
        **core,
        "hmac_sha256": hmac.new(key, _canonical(core), hashlib.sha256).hexdigest(),
    }


def _verify_signed(value: Mapping[str, Any], key: bytes, label: str) -> dict[str, Any]:
    signature = str(value.get("hmac_sha256") or "")
    core = dict(value)
    core.pop("hmac_sha256", None)
    expected = hmac.new(key, _canonical(core), hashlib.sha256).hexdigest()
    if not SHA256_RE.fullmatch(signature) or not hmac.compare_digest(
        signature, expected
    ):
        raise CaptureHotWriterError(f"{label} signature drifted")
    return core


def _joint_path(root: Path) -> Path:
    return root / JOINT_MANIFEST_FILENAME


def _pending_path(root: Path) -> Path:
    return root / PENDING_FILENAME


def _jsonl_index_dir(root: Path) -> Path:
    return root / JSONL_INDEX_DIRNAME


def _bootstrap_commit_dir(root: Path) -> Path:
    return root / BOOTSTRAP_COMMIT_DIRNAME


def _hot_receipt_dir(root: Path) -> Path:
    return root / HOT_RECEIPT_DIRNAME


def _hot_link_path(root: Path, event_id: str) -> Path:
    digest = _sha256_bytes(event_id.encode("utf-8"))
    return root / HOT_LINK_DIRNAME / f"{digest}.json"


@contextmanager
def capture_ledger_lock(
    ledger_path: str | Path,
) -> Iterator[CaptureLedgerLockToken]:
    """Acquire the existing capture-root ``.lock`` and yield a live proof token."""

    ledger = Path(ledger_path).expanduser().resolve()
    _regular_file(ledger, "capture ledger")
    lock_path = ledger.parent / ".lock"
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    with lock_path.open("a+", encoding="utf-8") as handle:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        token = CaptureLedgerLockToken(
            ledger_path=ledger,
            owner_pid=os.getpid(),
            owner_thread_id=threading.get_ident(),
            _secret=_TOKEN_SECRET,
        )
        try:
            yield token
        finally:
            token.active = False
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def _require_lock(token: CaptureLedgerLockToken, ledger: Path) -> None:
    if (
        not isinstance(token, CaptureLedgerLockToken)
        or token._secret is not _TOKEN_SECRET
        or not token.active
        or token.owner_pid != os.getpid()
        or token.owner_thread_id != threading.get_ident()
        or token.ledger_path != ledger.resolve()
    ):
        raise CaptureHotWriterError(
            "capture_fact requires a live caller-held capture ledger lock token"
        )


def _normalize_payload(payload: Mapping[str, Any]) -> dict[str, Any]:
    try:
        normalized = capture_model._validate_capture_payload(dict(payload))
    except Exception as exc:
        raise CaptureHotWriterError(f"capture payload validation failed: {exc}") from exc
    if normalized != payload:
        raise CaptureHotWriterError(
            "capture payload is not the upstream canonical normalized value"
        )
    return normalized


def canonical_fact_event_from_payload(
    validated_payload: Mapping[str, Any],
    *,
    created_at: str | None = None,
) -> dict[str, Any]:
    """Create the same deterministic identities used by the current capture writer."""

    payload = _normalize_payload(validated_payload)
    key = str(payload["idempotency_key"])
    timestamp = created_at or dt.datetime.now(dt.timezone.utc).isoformat()
    event = {
        "schema": capture_model.EVENT_SCHEMA,
        "event_type": "fact_captured",
        "event_id": (
            "CE-" + hashlib.sha256(("capture:" + key).encode()).hexdigest()[:20]
        ),
        "idempotency_key": key,
        "created_at": timestamp,
        "capture_id": (
            f"CAP-{payload['study_date'].replace('-', '')}-"
            f"{hashlib.sha256(key.encode()).hexdigest()[:12]}"
        ),
        "payload_sha256": _sha256_value(payload),
        "capture": payload,
    }
    return _validate_event(event)


def _validate_created_at(value: object, label: str) -> str:
    created_at = str(value or "")
    try:
        parsed = dt.datetime.fromisoformat(created_at)
    except ValueError as exc:
        raise CaptureHotWriterError(f"{label} created_at is invalid") from exc
    if parsed.tzinfo is None:
        raise CaptureHotWriterError(f"{label} created_at must be timezone-aware")
    return created_at


def _validate_fact_event(raw: Mapping[str, Any]) -> dict[str, Any]:
    event = copy.deepcopy(dict(raw))
    if set(event) != FACT_EVENT_KEYS:
        raise CaptureHotWriterError("fact_captured event fields are incomplete or unknown")
    if (
        event.get("schema") != capture_model.EVENT_SCHEMA
        or event.get("event_type") != "fact_captured"
    ):
        raise CaptureHotWriterError("hot writer only accepts fact_captured events")
    payload_raw = event.get("capture")
    if not isinstance(payload_raw, dict):
        raise CaptureHotWriterError("fact_captured event has no capture payload")
    payload = _normalize_payload(payload_raw)
    key = str(payload["idempotency_key"])
    if event.get("idempotency_key") != key:
        raise CaptureHotWriterError("event and payload idempotency keys differ")
    expected_payload_sha = _sha256_value(payload)
    if event.get("payload_sha256") != expected_payload_sha:
        raise CaptureHotWriterError("fact_captured payload SHA-256 drifted")
    expected_capture_id = (
        f"CAP-{payload['study_date'].replace('-', '')}-"
        f"{hashlib.sha256(key.encode()).hexdigest()[:12]}"
    )
    expected_event_id = (
        "CE-" + hashlib.sha256(("capture:" + key).encode()).hexdigest()[:20]
    )
    if event.get("capture_id") != expected_capture_id:
        raise CaptureHotWriterError("fact_captured capture_id is not canonical")
    if event.get("event_id") != expected_event_id:
        raise CaptureHotWriterError("fact_captured event_id is not canonical")
    _validate_created_at(event.get("created_at"), "fact_captured")
    return event


def _validate_authorization_proof(raw: Mapping[str, Any]) -> dict[str, Any]:
    proof = copy.deepcopy(dict(raw))
    if (
        proof.get("schema") != AUTHORIZATION_PROOF_SCHEMA
        or proof.get("authorization_body_stored") is not False
        or proof.get("formal_write_count") != 0
    ):
        raise CaptureHotWriterError("capture authorization proof is invalid")
    proof_kind = proof.get("proof_kind")
    if proof_kind == "managed_current_message_intent":
        if set(proof) != MANAGED_AUTHORIZATION_PROOF_KEYS:
            raise CaptureHotWriterError(
                "managed authorization proof fields are incomplete or unknown"
            )
        for field in (
            "study_authorization_receipt_sha256",
            "intent_receipt_sha256",
            "display_receipt_sha256",
            "authorization_message_sha256",
            "authorization_binding_sha256",
        ):
            _require_sha(proof.get(field), f"authorization proof {field}")
        for ref_field, sha_field in (
            (
                "study_authorization_receipt_ref",
                "study_authorization_receipt_sha256",
            ),
            ("intent_receipt_ref", "intent_receipt_sha256"),
        ):
            ref = str(proof.get(ref_field) or "")
            if (
                not ref
                or ref.startswith("/")
                or ".." in Path(ref).parts
                or not ref.endswith(f"/{proof[sha_field]}.json")
            ):
                raise CaptureHotWriterError(
                    f"authorization proof {ref_field} is invalid"
                )
        if (
            not re.fullmatch(
                r"[A-Za-z0-9_.:@-]{1,160}",
                str(proof.get("request_id") or ""),
            )
            or not isinstance(
                proof.get("authorization_message_size_bytes"), int
            )
            or isinstance(
                proof.get("authorization_message_size_bytes"), bool
            )
            or not 0 < proof["authorization_message_size_bytes"] <= 4096
        ):
            raise CaptureHotWriterError(
                "managed authorization proof message binding is invalid"
            )
        return proof
    if proof_kind == "managed_existing_capture_current_message":
        if set(proof) != EXISTING_CAPTURE_AUTHORIZATION_PROOF_KEYS:
            raise CaptureHotWriterError(
                "existing-capture authorization proof fields are incomplete or unknown"
            )
        for field in (
            "existing_capture_intent_sha256",
            "fact_event_sha256",
            "fact_receipt_sha256",
            "authorization_message_sha256",
            "authorization_binding_sha256",
        ):
            _require_sha(proof.get(field), f"authorization proof {field}")
        intent_ref = str(proof.get("existing_capture_intent_ref") or "")
        if (
            not intent_ref
            or intent_ref.startswith("/")
            or ".." in Path(intent_ref).parts
            or not intent_ref.endswith(
                f"/{proof['existing_capture_intent_sha256']}.json"
            )
        ):
            raise CaptureHotWriterError(
                "existing-capture authorization intent ref is invalid"
            )
        try:
            capture_id = capture_model._validate_id(
                proof.get("capture_id"), "authorization_proof.capture_id"
            )
            fact_event_id = capture_model._validate_id(
                proof.get("fact_event_id"),
                "authorization_proof.fact_event_id",
            )
        except Exception as exc:
            raise CaptureHotWriterError(
                f"existing-capture authorization identity is invalid: {exc}"
            ) from exc
        request_id = str(proof.get("request_id") or "")
        message_size = proof.get("authorization_message_size_bytes")
        if (
            not re.fullmatch(r"[A-Za-z0-9_.:@-]{1,160}", request_id)
            or not isinstance(message_size, int)
            or isinstance(message_size, bool)
            or not 0 < message_size <= 4096
        ):
            raise CaptureHotWriterError(
                "existing-capture authorization message binding is invalid"
            )
        binding = {
            "schema": EXISTING_CAPTURE_AUTHORIZATION_BINDING_SCHEMA,
            "request_id": request_id,
            "capture_id": capture_id,
            "fact_event_id": fact_event_id,
            "fact_event_sha256": proof["fact_event_sha256"],
            "fact_receipt_sha256": proof["fact_receipt_sha256"],
            "authorization_message_sha256": proof[
                "authorization_message_sha256"
            ],
            "authorization_message_size_bytes": message_size,
        }
        if proof["authorization_binding_sha256"] != _sha256_value(binding):
            raise CaptureHotWriterError(
                "existing-capture authorization binding drifted"
            )
        return proof
    raise CaptureHotWriterError("capture authorization proof kind is invalid")


def canonical_authorization_event(
    *,
    capture_id: str,
    idempotency_key: str,
    authorization_proof: Mapping[str, Any] | None = None,
    created_at: str | None = None,
) -> dict[str, Any]:
    """Build one deterministic, separately evidenced curation authorization."""

    try:
        normalized_capture_id = capture_model._validate_id(capture_id, "capture_id")
        key = capture_model._validate_id(idempotency_key, "idempotency_key")
    except Exception as exc:
        raise CaptureHotWriterError(
            f"capture authorization identity validation failed: {exc}"
        ) from exc
    if authorization_proof is None:
        raise CaptureHotWriterError(
            "managed authorization proof is required for a new authorization event"
        )
    event = {
        "schema": capture_model.EVENT_SCHEMA,
        "event_type": "capture_authorized",
        "event_id": (
            "CE-" + hashlib.sha256(("authorize:" + key).encode()).hexdigest()[:20]
        ),
        "idempotency_key": key,
        "created_at": created_at or dt.datetime.now(dt.timezone.utc).isoformat(),
        "capture_id": normalized_capture_id,
        "authorization_proof": _validate_authorization_proof(
            authorization_proof
        ),
    }
    return _validate_authorization_event(event, allow_legacy_read=False)


def _validate_authorization_event(
    raw: Mapping[str, Any], *, allow_legacy_read: bool = True
) -> dict[str, Any]:
    event = copy.deepcopy(dict(raw))
    event_keys = set(event)
    if event_keys != AUTH_EVENT_KEYS and not (
        allow_legacy_read and event_keys == LEGACY_AUTH_EVENT_KEYS
    ):
        raise CaptureHotWriterError(
            "capture_authorized event fields are incomplete or unknown"
        )
    if (
        event.get("schema") != capture_model.EVENT_SCHEMA
        or event.get("event_type") != "capture_authorized"
    ):
        raise CaptureHotWriterError(
            "authorization hot writer only accepts capture_authorized events"
        )
    try:
        capture_id = capture_model._validate_id(event.get("capture_id"), "capture_id")
        key = capture_model._validate_id(
            event.get("idempotency_key"), "idempotency_key"
        )
    except Exception as exc:
        raise CaptureHotWriterError(
            f"capture authorization identity validation failed: {exc}"
        ) from exc
    expected_event_id = (
        "CE-" + hashlib.sha256(("authorize:" + key).encode()).hexdigest()[:20]
    )
    if event.get("event_id") != expected_event_id:
        raise CaptureHotWriterError(
            "capture_authorized event_id is not canonical"
        )
    if event.get("capture_id") != capture_id:
        raise CaptureHotWriterError("capture_authorized capture_id is not canonical")
    if "authorization_proof" in event:
        event["authorization_proof"] = _validate_authorization_proof(
            event["authorization_proof"]
        )
    _validate_created_at(event.get("created_at"), "capture_authorized")
    return event


def _validate_neutral_event(raw: Mapping[str, Any]) -> dict[str, Any]:
    event = copy.deepcopy(dict(raw))
    if set(event) != NEUTRAL_EVENT_KEYS:
        raise CaptureHotWriterError(
            "neutral_saved event fields are incomplete or unknown"
        )
    if (
        event.get("schema") != capture_model.EVENT_SCHEMA
        or event.get("event_type") != "neutral_saved"
    ):
        raise CaptureHotWriterError(
            "neutral hot writer only accepts neutral_saved events"
        )
    try:
        material = capture_model._neutral_save_material(event)
    except Exception as exc:
        raise CaptureHotWriterError(
            f"neutral save material validation failed: {exc}"
        ) from exc
    review_event_id = str(material["review_event_id"])
    expected_event_id = (
        "NSE-"
        + hashlib.sha256(("neutral:" + review_event_id).encode()).hexdigest()[:20]
    )
    expected_key = f"neutral-save:{review_event_id}:v1"
    if event.get("event_id") != expected_event_id:
        raise CaptureHotWriterError("neutral_saved event_id is not canonical")
    if event.get("idempotency_key") != expected_key:
        raise CaptureHotWriterError(
            "neutral_saved idempotency_key is not canonical"
        )
    if event.get("payload_sha256") != _sha256_value(material):
        raise CaptureHotWriterError("neutral_saved payload SHA-256 drifted")
    _validate_created_at(event.get("created_at"), "neutral_saved")
    return event


def _validate_event(raw: Mapping[str, Any]) -> dict[str, Any]:
    event_type = raw.get("event_type")
    if event_type == "fact_captured":
        return _validate_fact_event(raw)
    if event_type == "capture_authorized":
        return _validate_authorization_event(raw)
    if event_type == "neutral_saved":
        return _validate_neutral_event(raw)
    raise CaptureHotWriterError(
        "hot writer only accepts fact_captured, capture_authorized, or neutral_saved events"
    )


def _load_state(
    state_path: Path, *, maximum: int
) -> tuple[bytes, dict[str, Any]]:
    raw = _read_bounded(state_path, maximum=maximum, label="capture state")
    try:
        value = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise CaptureHotWriterError("capture state is invalid JSON") from exc
    if (
        not isinstance(value, dict)
        or value.get("schema") != capture_model.STATE_SCHEMA
        or not isinstance(value.get("event_count"), int)
        or not isinstance(value.get("captures"), dict)
        or not isinstance(value.get("batches"), dict)
        or not isinstance(value.get("pending_by_date"), dict)
        or (
            "neutral_saves" in value
            and not isinstance(value.get("neutral_saves"), dict)
        )
    ):
        raise CaptureHotWriterError("capture state schema is invalid")
    return raw, value


def _initial_capture_state(event: Mapping[str, Any]) -> dict[str, Any]:
    payload = event["capture"]
    return {
        "capture_id": event["capture_id"],
        "study_date": payload["study_date"],
        "recorded_at": _validate_created_at(
            event.get("created_at"), "fact capture"
        ),
        "payload_sha256": event["payload_sha256"],
        "quality_status": capture_model.initial_capture_quality_status(payload),
        "current_batch_id": None,
        "formal_id": payload["identity_hint"].get("formal_id") or None,
        "last_error": None,
        "formalization_authorized": payload["formalization_authorized"],
        "capture": payload,
    }


def _increment_state(
    current: Mapping[str, Any], event: Mapping[str, Any]
) -> dict[str, Any]:
    state = copy.deepcopy(dict(current))
    capture_id = str(event["capture_id"])
    captures = state["captures"]
    if capture_id in captures:
        raise CaptureHotWriterError("capture_id already exists outside exact idempotency")
    captures[capture_id] = _initial_capture_state(event)
    if event["capture"]["formalization_authorized"]:
        study_date = str(event["capture"]["study_date"])
        pending = state["pending_by_date"].setdefault(study_date, [])
        if capture_id in pending:
            raise CaptureHotWriterError("capture pending projection already contains ID")
        pending.append(capture_id)
        pending.sort()
    state["event_count"] = int(state["event_count"]) + 1
    return state


def _authorized_capture_projection(
    capture_id: str,
    capture_state: Mapping[str, Any],
    *,
    include_recorded_at: bool = True,
) -> dict[str, Any]:
    payload = capture_state.get("capture")
    if not isinstance(payload, dict):
        raise CaptureHotWriterError(
            "capture authorization target has no canonical capture payload"
        )
    if payload.get("formalization_authorized") is not False:
        raise CaptureHotWriterError(
            "capture authorization target was not an unconfirmed ordinary capture"
        )
    payload_sha256 = _sha256_value(payload)
    if capture_state.get("payload_sha256") != payload_sha256:
        raise CaptureHotWriterError(
            "capture authorization target payload commitment drifted"
        )
    projection = {
        "capture_id": capture_id,
        "study_date": payload["study_date"],
        "payload_sha256": payload_sha256,
        "quality_status": capture_model.PENDING_FORMAL_STATUS,
        "current_batch_id": None,
        "formal_id": payload["identity_hint"].get("formal_id") or None,
        "last_error": None,
        "formalization_authorized": True,
        "capture": payload,
    }
    if include_recorded_at:
        projection["recorded_at"] = _validate_created_at(
            capture_state.get("recorded_at"), "capture state recorded_at"
        )
    return projection


def _increment_authorization_state(
    current: Mapping[str, Any], event: Mapping[str, Any]
) -> dict[str, Any]:
    state = copy.deepcopy(dict(current))
    capture_id = str(event["capture_id"])
    capture_state = state["captures"].get(capture_id)
    if not isinstance(capture_state, dict):
        raise CaptureHotWriterError(
            "capture_authorized event references an unknown capture"
        )
    if capture_state.get("formalization_authorized") is not False:
        raise CaptureHotWriterError(
            "capture is already authorized outside exact idempotency"
        )
    if capture_state.get("quality_status") != "captured_unconfirmed":
        raise CaptureHotWriterError(
            "only captured_unconfirmed can receive hot authorization"
        )
    authorized = _authorized_capture_projection(capture_id, capture_state)
    state["captures"][capture_id] = authorized
    pending = state["pending_by_date"].setdefault(
        str(authorized["study_date"]), []
    )
    if capture_id in pending:
        raise CaptureHotWriterError(
            "capture authorization target is already in pending_by_date"
        )
    pending.append(capture_id)
    pending.sort()
    state["event_count"] = int(state["event_count"]) + 1
    return state


def _increment_neutral_state(
    current: Mapping[str, Any], event: Mapping[str, Any]
) -> dict[str, Any]:
    state = copy.deepcopy(dict(current))
    material = capture_model._neutral_save_material(dict(event))
    neutral_saves = state.setdefault("neutral_saves", {})
    if not isinstance(neutral_saves, dict):
        raise CaptureHotWriterError("neutral_saves state projection is invalid")
    save_id = str(material["neutral_save_id"])
    review_event_id = str(material["review_event_id"])
    if save_id in neutral_saves or any(
        isinstance(value, Mapping)
        and value.get("review_event_id") == review_event_id
        for value in neutral_saves.values()
    ):
        raise CaptureHotWriterError(
            "neutral save already exists outside exact idempotency"
        )
    neutral_saves[save_id] = material
    state["event_count"] = int(state["event_count"]) + 1
    return state


def _bounded_manifest_snapshot(
    ledger: Path, index_dir: Path
) -> tuple[dict[str, Any], str]:
    # Public exact lookup validates the HMAC manifest and committed ledger state.
    bounded_jsonl.lookup_event(ledger, index_dir, PROBE_EVENT_ID)
    path = index_dir / bounded_jsonl.MANIFEST_FILENAME
    raw = _read_bounded(
        path,
        maximum=bounded_jsonl.MAX_MANIFEST_BYTES,
        label="bounded JSONL manifest",
    )
    try:
        manifest = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise CaptureHotWriterError("bounded JSONL manifest is invalid") from exc
    if not isinstance(manifest, dict):
        raise CaptureHotWriterError("bounded JSONL manifest must be an object")
    return manifest, _sha256_bytes(raw)


def _bounded_binding(manifest: Mapping[str, Any], manifest_sha: str) -> dict[str, Any]:
    ledger = manifest.get("ledger")
    if not isinstance(ledger, dict):
        raise CaptureHotWriterError("bounded JSONL manifest has no ledger binding")
    return {
        "manifest_sha256": manifest_sha,
        "generation_id": manifest.get("generation_id"),
        "ledger_size_bytes": ledger.get("size_bytes"),
        "ledger_root_sha256": ledger.get("root_sha256"),
        "event_count": manifest.get("event_count"),
        "last_commit_receipt_sha256": manifest.get(
            "last_commit_receipt_sha256"
        ),
    }


def _load_joint(root: Path) -> tuple[dict[str, Any], str]:
    path = _joint_path(root)
    raw = _read_bounded(
        path, maximum=MAX_JOINT_MANIFEST_BYTES, label="capture joint manifest"
    )
    try:
        signed = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise CaptureHotWriterError("capture joint manifest is invalid") from exc
    if not isinstance(signed, dict):
        raise CaptureHotWriterError("capture joint manifest must be an object")
    key = _load_or_create_key(root, create=False)
    core = _verify_signed(signed, key, "capture joint manifest")
    if (
        core.get("schema") != JOINT_MANIFEST_SCHEMA
        or core.get("formal_write_count") != 0
        or core.get("answer_safe") is not True
        or not isinstance(core.get("bounded_jsonl"), dict)
        or not isinstance(core.get("state"), dict)
        or not isinstance(core.get("capture_commit"), dict)
    ):
        raise CaptureHotWriterError("capture joint manifest contract drifted")
    return core, _sha256_bytes(raw)


def _publish_joint(root: Path, material: Mapping[str, Any]) -> tuple[dict[str, Any], str]:
    core = dict(material)
    core.pop("generation_id", None)
    core["generation_id"] = _sha256_value(
        {
            "schema": "capture_hot_joint_generation_v1",
            "material": core,
        }
    )
    key = _load_or_create_key(root, create=True)
    signed = _sign(core, key)
    raw = _pretty(signed)
    _atomic_write(_joint_path(root), raw)
    return core, _sha256_bytes(raw)


def _write_pending(root: Path, value: Mapping[str, Any]) -> None:
    key = _load_or_create_key(root, create=False)
    signed = _sign(dict(value), key)
    _atomic_write(_pending_path(root), _pretty(signed))


def _load_pending(root: Path) -> dict[str, Any] | None:
    path = _pending_path(root)
    if not path.exists():
        return None
    value = _read_json_bounded(path, maximum=MAX_PENDING_BYTES, label="pending commit")
    key = _load_or_create_key(root, create=False)
    core = _verify_signed(value, key, "pending commit")
    if core.get("schema") != PENDING_SCHEMA or core.get("formal_write_count") != 0:
        raise CaptureHotWriterError("pending commit contract drifted")
    return core


def _require_no_pending(root: Path) -> None:
    if _load_pending(root) is not None:
        raise CaptureHotWriterError(
            "an incomplete joint capture commit exists; run cold capture reconcile "
            "and cold_bootstrap(reconcile=True) before another hot write"
        )


def read_pending_identity(hot_root: str | Path) -> dict[str, Any] | None:
    """Return the signed answer-safe identity of one incomplete hot commit."""

    root = Path(hot_root).expanduser().resolve()
    pending = _load_pending(root)
    if pending is None:
        return None
    return {
        "schema": pending["schema"],
        "event_id": pending.get("event_id"),
        "capture_id": pending.get("capture_id"),
        "idempotency_key_sha256": pending.get("idempotency_key_sha256"),
        "payload_sha256": pending.get("payload_sha256"),
        "formal_write_count": 0,
    }


def _clear_pending(root: Path) -> None:
    path = _pending_path(root)
    if path.exists():
        path.unlink()
        _fsync_directory(path.parent)


def _assert_paths(joint: Mapping[str, Any], ledger: Path, state: Path) -> None:
    if (
        joint.get("ledger_path_sha256") != _path_sha256(ledger)
        or joint.get("state_path_sha256") != _path_sha256(state)
    ):
        raise CaptureHotWriterError("joint manifest path binding drifted")


def _assert_bounded_binding(
    joint: Mapping[str, Any], manifest: Mapping[str, Any], manifest_sha: str
) -> None:
    actual = _bounded_binding(manifest, manifest_sha)
    if joint.get("bounded_jsonl") != actual:
        raise CaptureHotWriterError(
            "bounded JSONL generation drifted; cold bootstrap is required"
        )


def _assert_state_binding(
    joint: Mapping[str, Any], state_raw: bytes, state: Mapping[str, Any]
) -> None:
    expected = joint.get("state")
    actual = {
        "sha256": _sha256_bytes(state_raw),
        "size_bytes": len(state_raw),
        "event_count": state.get("event_count"),
        "schema": state.get("schema"),
    }
    if expected != actual:
        raise CaptureHotWriterError(
            "capture state drifted from the joint manifest; cold reconcile is required"
        )


def _joint_material(
    *,
    previous_joint_sha256: str | None,
    generation_number: int,
    mode: str,
    ledger: Path,
    state_path: Path,
    state_raw: bytes,
    state: Mapping[str, Any],
    bounded_manifest: Mapping[str, Any],
    bounded_manifest_sha: str,
    bootstrap_index_sha256: str,
    event_chain_sha256: str,
    latest_receipt_sha256: str | None,
    latest_event_id: str | None,
    hot_receipt_count: int,
) -> dict[str, Any]:
    return {
        "schema": JOINT_MANIFEST_SCHEMA,
        "generation_number": generation_number,
        "previous_joint_manifest_sha256": previous_joint_sha256,
        "mode": mode,
        "ledger_path_sha256": _path_sha256(ledger),
        "state_path_sha256": _path_sha256(state_path),
        "bounded_jsonl": _bounded_binding(
            bounded_manifest, bounded_manifest_sha
        ),
        "state": {
            "sha256": _sha256_bytes(state_raw),
            "size_bytes": len(state_raw),
            "event_count": state.get("event_count"),
            "schema": state.get("schema"),
        },
        "capture_commit": {
            "bootstrap_index_sha256": bootstrap_index_sha256,
            "event_chain_sha256": event_chain_sha256,
            "latest_receipt_sha256": latest_receipt_sha256,
            "latest_event_id": latest_event_id,
            "hot_receipt_count": hot_receipt_count,
        },
        "performance_contract": {
            "hot_ledger_full_read_count": 0,
            "state_read_is_bounded": True,
            "hot_state_read_count": 2,
            "review_or_session_ledger_reads": 0,
            "formal_database_reads": 0,
            "formal_database_writes": 0,
        },
        "answer_safe": True,
        "formal_write_count": 0,
    }


def cold_bootstrap(
    ledger_path: str | Path,
    state_path: str | Path,
    hot_root: str | Path,
    *,
    lock_token: CaptureLedgerLockToken,
    reconcile: bool = False,
    max_state_bytes: int = DEFAULT_MAX_STATE_BYTES,
) -> dict[str, Any]:
    """Cold-adopt the exact ledger/state into both derived index families."""

    ledger = Path(ledger_path).expanduser().resolve()
    state_file = Path(state_path).expanduser().resolve()
    root = Path(hot_root).expanduser().resolve()
    _require_lock(lock_token, ledger)
    pending = _load_pending(root) if root.exists() else None
    if pending is not None and not reconcile:
        raise CaptureHotWriterError(
            "pending joint commit exists; cold_bootstrap requires reconcile=True"
        )

    # Validate the authoritative ledger/state before publishing any new derived head.
    try:
        capture_bundle = capture_commit.cold_rebuild(
            ledger,
            state_file,
            max_state_bytes=max_state_bytes,
        )
    except Exception as exc:
        raise CaptureHotWriterError(
            "capture ledger/state cold replay failed; run the authoritative capture "
            "reconcile before bootstrapping"
        ) from exc
    state_raw, state = _load_state(state_file, maximum=max_state_bytes)
    try:
        bounded_jsonl.rebuild_index(
            ledger,
            _jsonl_index_dir(root),
            event_id_field="event_id",
            idempotency_field="idempotency_key",
            chunk_size=HOT_LEDGER_CHUNK_SIZE,
        )
        capture_commit.write_bundle(
            capture_bundle, _bootstrap_commit_dir(root)
        )
        bounded_manifest, bounded_sha = _bounded_manifest_snapshot(
            ledger, _jsonl_index_dir(root)
        )
    except Exception as exc:
        raise CaptureHotWriterError(f"cold index bootstrap failed: {exc}") from exc

    prior_joint: dict[str, Any] | None = None
    prior_sha: str | None = None
    if _joint_path(root).is_file():
        prior_joint, prior_sha = _load_joint(root)
    index = capture_bundle["index"]
    entries = index["entries"]
    latest_entry = entries[-1] if entries else None
    material = _joint_material(
        previous_joint_sha256=prior_sha,
        generation_number=(
            int(prior_joint.get("generation_number", 0)) + 1
            if prior_joint
            else 0
        ),
        mode="cold_reconcile" if reconcile else "cold_bootstrap",
        ledger=ledger,
        state_path=state_file,
        state_raw=state_raw,
        state=state,
        bounded_manifest=bounded_manifest,
        bounded_manifest_sha=bounded_sha,
        bootstrap_index_sha256=index["index_sha256"],
        event_chain_sha256=index["ledger_head"]["event_chain_sha256"],
        latest_receipt_sha256=(
            latest_entry["receipt_sha256"] if latest_entry else None
        ),
        latest_event_id=latest_entry["event_id"] if latest_entry else None,
        hot_receipt_count=0,
    )
    joint, joint_sha = _publish_joint(root, material)
    if pending is not None:
        _clear_pending(root)
    return {
        "schema": HOT_RESULT_SCHEMA,
        "status": "BOOTSTRAPPED",
        "joint_manifest_sha256": joint_sha,
        "generation_id": joint["generation_id"],
        "ledger_event_count": bounded_manifest["event_count"],
        "capture_receipt_count": len(entries),
        "state_bytes_read": len(state_raw),
        "ledger_full_read_count": 1,
        "hot_path": False,
        "formal_write_count": 0,
    }


def _build_authorization_receipt(
    *,
    event_bytes: bytes,
    byte_offset: int,
    event_ordinal: int,
    previous_chain_sha256: str,
    post_event_state: Mapping[str, Any],
) -> dict[str, Any]:
    if not event_bytes.endswith(b"\n") or b"\n" in event_bytes[:-1]:
        raise CaptureHotWriterError(
            "authorization event bytes must be one canonical LF record"
        )
    try:
        raw_event = json.loads(event_bytes.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise CaptureHotWriterError(
            "authorization event bytes are invalid JSON"
        ) from exc
    if not isinstance(raw_event, dict):
        raise CaptureHotWriterError("authorization event must be an object")
    event = _validate_authorization_event(
        raw_event, allow_legacy_read=False
    )
    if (
        post_event_state.get("schema") != capture_model.STATE_SCHEMA
        or post_event_state.get("event_count") != event_ordinal
    ):
        raise CaptureHotWriterError(
            "authorization post-event state identity drifted"
        )
    captures = post_event_state.get("captures")
    current = (
        captures.get(event["capture_id"])
        if isinstance(captures, Mapping)
        else None
    )
    if not isinstance(current, Mapping):
        raise CaptureHotWriterError(
            "authorization post-event state lacks its capture"
        )
    expected_projection = _authorized_capture_projection(
        str(event["capture_id"]), current
    )
    if dict(current) != expected_projection:
        raise CaptureHotWriterError(
            "authorization post-event capture projection drifted"
        )
    event_sha256 = _sha256_bytes(event_bytes)
    chain_sha256 = _event_chain(
        previous_chain_sha256,
        event_sha256,
        byte_offset,
        len(event_bytes),
        event_ordinal,
    )
    material = {
        "schema": AUTH_RECEIPT_SCHEMA,
        "kind": AUTH_RECEIPT_KIND,
        "capture_id": event["capture_id"],
        "event_id": event["event_id"],
        "idempotency_key": event["idempotency_key"],
        "event_sha256": event_sha256,
        "byte_offset": byte_offset,
        "byte_length": len(event_bytes),
        "event_ordinal": event_ordinal,
        "ledger_commitment": {
            "algorithm": capture_commit.CHAIN_SCHEMA,
            "previous_sha256": previous_chain_sha256,
            "sha256": chain_sha256,
            "event_end_offset": byte_offset + len(event_bytes),
        },
        "state_commitment": {
            "serialization": capture_commit.STATE_SERIALIZATION,
            "post_event_state_sha256": _sha256_bytes(
                _state_bytes(post_event_state)
            ),
            "post_event_event_count": event_ordinal,
            "capture_state_sha256": _sha256_value(expected_projection),
        },
        "verification_boundary": {
            "answer_safe_metadata_only": True,
            "daily_curation_authorized": True,
            "formal_write_authorized": False,
            "formal_database_touched": False,
        },
        "formal_write_count": 0,
    }
    if "authorization_proof" in event:
        proof = _validate_authorization_proof(event["authorization_proof"])
        material["authorization_proof"] = proof
        material["authorization_proof_sha256"] = _sha256_value(proof)
    digest = _sha256_value(material)
    return {
        **material,
        "receipt_id": f"CAR-{digest[:24]}",
        "receipt_sha256": digest,
    }


def _build_neutral_receipt(
    *,
    event_bytes: bytes,
    byte_offset: int,
    event_ordinal: int,
    previous_chain_sha256: str,
    post_event_state: Mapping[str, Any],
) -> dict[str, Any]:
    if not event_bytes.endswith(b"\n") or b"\n" in event_bytes[:-1]:
        raise CaptureHotWriterError(
            "neutral save event bytes must be one canonical LF record"
        )
    try:
        raw_event = json.loads(event_bytes.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise CaptureHotWriterError(
            "neutral save event bytes are invalid JSON"
        ) from exc
    if not isinstance(raw_event, dict):
        raise CaptureHotWriterError("neutral save event must be an object")
    event = _validate_neutral_event(raw_event)
    if (
        post_event_state.get("schema") != capture_model.STATE_SCHEMA
        or post_event_state.get("event_count") != event_ordinal
    ):
        raise CaptureHotWriterError("neutral post-event state identity drifted")
    material_projection = capture_model._neutral_save_material(event)
    neutral_saves = post_event_state.get("neutral_saves")
    current = (
        neutral_saves.get(event["neutral_save_id"])
        if isinstance(neutral_saves, Mapping)
        else None
    )
    if current != material_projection:
        raise CaptureHotWriterError(
            "neutral post-event state lacks the canonical save projection"
        )
    event_sha256 = _sha256_bytes(event_bytes)
    chain_sha256 = _event_chain(
        previous_chain_sha256,
        event_sha256,
        byte_offset,
        len(event_bytes),
        event_ordinal,
    )
    material = {
        "schema": NEUTRAL_RECEIPT_SCHEMA,
        "kind": NEUTRAL_RECEIPT_KIND,
        "neutral_save_id": event["neutral_save_id"],
        "event_id": event["event_id"],
        "idempotency_key": event["idempotency_key"],
        "event_sha256": event_sha256,
        "payload_sha256": event["payload_sha256"],
        "byte_offset": byte_offset,
        "byte_length": len(event_bytes),
        "event_ordinal": event_ordinal,
        "ledger_commitment": {
            "algorithm": capture_commit.CHAIN_SCHEMA,
            "previous_sha256": previous_chain_sha256,
            "sha256": chain_sha256,
            "event_end_offset": byte_offset + len(event_bytes),
        },
        "state_commitment": {
            "serialization": capture_commit.STATE_SERIALIZATION,
            "post_event_state_sha256": _sha256_bytes(
                _state_bytes(post_event_state)
            ),
            "post_event_event_count": event_ordinal,
            "neutral_save_state_sha256": _sha256_value(material_projection),
        },
        "verification_boundary": {
            "answer_safe_metadata_only": True,
            "daily_curation_authorized": False,
            "curation_eligible": False,
            "formal_write_authorized": False,
            "formal_database_touched": False,
        },
        "formal_write_count": 0,
    }
    digest = _sha256_value(material)
    return {
        **material,
        "receipt_id": f"CNR-{digest[:24]}",
        "receipt_sha256": digest,
    }


def _write_capture_receipt(root: Path, receipt: Mapping[str, Any]) -> Path:
    digest = _require_sha(receipt.get("receipt_sha256"), "receipt_sha256")
    path = _hot_receipt_dir(root) / f"{digest}.json"
    raw = _pretty(receipt)
    if path.exists():
        existing = _read_bounded(
            path, maximum=MAX_RECEIPT_BYTES, label="capture receipt"
        )
        if existing != raw:
            raise CaptureHotWriterError("content-addressed capture receipt collision")
    else:
        _atomic_write(path, raw)
    return path


def _write_link(
    root: Path,
    *,
    event_id: str,
    capture_id: str | None,
    receipt_sha256: str,
) -> None:
    value = {
        "schema": LINK_SCHEMA,
        "event_id": event_id,
        "capture_id": capture_id,
        "receipt_sha256": receipt_sha256,
        "formal_write_count": 0,
    }
    key = _load_or_create_key(root, create=False)
    target = _hot_link_path(root, event_id)
    raw = _pretty(_sign(value, key))
    if target.exists():
        existing = _read_bounded(
            target, maximum=MAX_LINK_BYTES, label="existing receipt link"
        )
        if existing != raw:
            raise CaptureHotWriterError("existing receipt link drifted")
    else:
        _atomic_write(target, raw)


def _load_hot_receipt(root: Path, event_id: str) -> dict[str, Any]:
    link_path = _hot_link_path(root, event_id)
    link = _read_json_bounded(link_path, maximum=MAX_LINK_BYTES, label="receipt link")
    key = _load_or_create_key(root, create=False)
    core = _verify_signed(link, key, "receipt link")
    if (
        core.get("schema") != LINK_SCHEMA
        or core.get("event_id") != event_id
        or core.get("formal_write_count") != 0
    ):
        raise CaptureHotWriterError("receipt link contract drifted")
    digest = _require_sha(core.get("receipt_sha256"), "linked receipt SHA-256")
    receipt_path = _hot_receipt_dir(root) / f"{digest}.json"
    receipt_raw = _read_bounded(
        receipt_path, maximum=MAX_RECEIPT_BYTES, label="capture receipt"
    )
    try:
        receipt = json.loads(receipt_raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise CaptureHotWriterError("capture receipt is invalid") from exc
    if not isinstance(receipt, dict) or receipt_raw != _pretty(receipt):
        raise CaptureHotWriterError("capture receipt content address drifted")
    _validate_receipt(receipt)
    if receipt.get("receipt_sha256") != digest:
        raise CaptureHotWriterError("receipt link points to different content")
    return receipt


def _validate_authorization_receipt(receipt: Mapping[str, Any]) -> None:
    digest = _require_sha(receipt.get("receipt_sha256"), "receipt SHA-256")
    if receipt.get("receipt_id") != f"CAR-{digest[:24]}":
        raise CaptureHotWriterError("authorization receipt ID drifted")
    material = {
        key: value
        for key, value in receipt.items()
        if key not in {"receipt_id", "receipt_sha256"}
    }
    if _sha256_value(material) != digest:
        raise CaptureHotWriterError(
            "authorization receipt content address drifted"
        )
    if (
        receipt.get("schema") != AUTH_RECEIPT_SCHEMA
        or receipt.get("kind") != AUTH_RECEIPT_KIND
        or receipt.get("formal_write_count") != 0
    ):
        raise CaptureHotWriterError("authorization receipt contract drifted")
    proof = receipt.get("authorization_proof")
    proof_sha = receipt.get("authorization_proof_sha256")
    if proof is None and proof_sha is not None:
        raise CaptureHotWriterError(
            "authorization receipt proof binding is incomplete"
        )
    if proof is not None:
        if not isinstance(proof, Mapping):
            raise CaptureHotWriterError(
                "authorization receipt proof is invalid"
            )
        normalized_proof = _validate_authorization_proof(proof)
        if proof_sha != _sha256_value(normalized_proof):
            raise CaptureHotWriterError(
                "authorization receipt proof SHA-256 drifted"
            )
    for key in ("capture_id", "event_id", "idempotency_key"):
        try:
            capture_model._validate_id(receipt.get(key), f"receipt.{key}")
        except Exception as exc:
            raise CaptureHotWriterError(
                f"authorization receipt {key} drifted"
            ) from exc
    for key in ("event_sha256",):
        _require_sha(receipt.get(key), f"authorization receipt {key}")
    for key in ("byte_offset", "byte_length", "event_ordinal"):
        value = receipt.get(key)
        if (
            not isinstance(value, int)
            or isinstance(value, bool)
            or value < (0 if key == "byte_offset" else 1)
        ):
            raise CaptureHotWriterError(
                f"authorization receipt {key} drifted"
            )
    ledger = receipt.get("ledger_commitment")
    state = receipt.get("state_commitment")
    boundary = receipt.get("verification_boundary")
    if not all(isinstance(value, Mapping) for value in (ledger, state, boundary)):
        raise CaptureHotWriterError(
            "authorization receipt commitments are missing"
        )
    if (
        ledger.get("algorithm") != capture_commit.CHAIN_SCHEMA
        or ledger.get("event_end_offset")
        != receipt["byte_offset"] + receipt["byte_length"]
        or state.get("serialization") != capture_commit.STATE_SERIALIZATION
        or state.get("post_event_event_count") != receipt["event_ordinal"]
        or boundary.get("daily_curation_authorized") is not True
        or boundary.get("formal_write_authorized") is not False
        or boundary.get("formal_database_touched") is not False
    ):
        raise CaptureHotWriterError(
            "authorization receipt commitment contract drifted"
        )
    for value, label in (
        (ledger.get("previous_sha256"), "ledger previous commitment"),
        (ledger.get("sha256"), "ledger commitment"),
        (state.get("post_event_state_sha256"), "post-event state"),
        (state.get("capture_state_sha256"), "authorized capture state"),
    ):
        _require_sha(value, label)


def _validate_neutral_receipt(receipt: Mapping[str, Any]) -> None:
    digest = _require_sha(receipt.get("receipt_sha256"), "receipt SHA-256")
    if receipt.get("receipt_id") != f"CNR-{digest[:24]}":
        raise CaptureHotWriterError("neutral receipt ID drifted")
    material = {
        key: value
        for key, value in receipt.items()
        if key not in {"receipt_id", "receipt_sha256"}
    }
    if _sha256_value(material) != digest:
        raise CaptureHotWriterError("neutral receipt content address drifted")
    if (
        receipt.get("schema") != NEUTRAL_RECEIPT_SCHEMA
        or receipt.get("kind") != NEUTRAL_RECEIPT_KIND
        or receipt.get("formal_write_count") != 0
    ):
        raise CaptureHotWriterError("neutral receipt contract drifted")
    for key in ("neutral_save_id", "event_id", "idempotency_key"):
        try:
            capture_model._validate_id(receipt.get(key), f"receipt.{key}")
        except Exception as exc:
            raise CaptureHotWriterError(
                f"neutral receipt {key} drifted"
            ) from exc
    for key in ("event_sha256", "payload_sha256"):
        _require_sha(receipt.get(key), f"neutral receipt {key}")
    for key in ("byte_offset", "byte_length", "event_ordinal"):
        value = receipt.get(key)
        if (
            not isinstance(value, int)
            or isinstance(value, bool)
            or value < (0 if key == "byte_offset" else 1)
        ):
            raise CaptureHotWriterError(f"neutral receipt {key} drifted")
    ledger = receipt.get("ledger_commitment")
    state = receipt.get("state_commitment")
    boundary = receipt.get("verification_boundary")
    if not all(isinstance(value, Mapping) for value in (ledger, state, boundary)):
        raise CaptureHotWriterError("neutral receipt commitments are missing")
    if (
        ledger.get("algorithm") != capture_commit.CHAIN_SCHEMA
        or ledger.get("event_end_offset")
        != receipt["byte_offset"] + receipt["byte_length"]
        or state.get("serialization") != capture_commit.STATE_SERIALIZATION
        or state.get("post_event_event_count") != receipt["event_ordinal"]
        or boundary.get("daily_curation_authorized") is not False
        or boundary.get("curation_eligible") is not False
        or boundary.get("formal_write_authorized") is not False
        or boundary.get("formal_database_touched") is not False
    ):
        raise CaptureHotWriterError("neutral receipt commitment contract drifted")
    for value, label in (
        (ledger.get("previous_sha256"), "ledger previous commitment"),
        (ledger.get("sha256"), "ledger commitment"),
        (state.get("post_event_state_sha256"), "post-event state"),
        (state.get("neutral_save_state_sha256"), "neutral save state"),
    ):
        _require_sha(value, label)


def _validate_receipt(receipt: Mapping[str, Any]) -> None:
    if receipt.get("schema") == AUTH_RECEIPT_SCHEMA:
        _validate_authorization_receipt(receipt)
        return
    if receipt.get("schema") == NEUTRAL_RECEIPT_SCHEMA:
        _validate_neutral_receipt(receipt)
        return
    digest = _require_sha(receipt.get("receipt_sha256"), "receipt SHA-256")
    if receipt.get("receipt_id") != f"CCR-{digest[:24]}":
        raise CaptureHotWriterError("capture receipt ID drifted")
    material = {
        key: value
        for key, value in receipt.items()
        if key not in {"receipt_id", "receipt_sha256"}
    }
    if _sha256_value(material) != digest:
        raise CaptureHotWriterError("capture receipt content address drifted")
    if (
        receipt.get("schema") != capture_commit.RECEIPT_SCHEMA
        or receipt.get("kind") != capture_commit.RECEIPT_KIND
        or receipt.get("formal_write_count") != 0
    ):
        raise CaptureHotWriterError("capture receipt contract drifted")


def _event_chain(
    previous_sha256: str,
    event_sha256: str,
    byte_offset: int,
    byte_length: int,
    event_ordinal: int,
) -> str:
    return _sha256_value(
        {
            "schema": capture_commit.CHAIN_SCHEMA,
            "previous_sha256": previous_sha256,
            "event_sha256": event_sha256,
            "byte_offset": byte_offset,
            "byte_length": byte_length,
            "event_ordinal": event_ordinal,
        }
    )


def _resolve_receipt(
    root: Path, event: Mapping[str, Any]
) -> tuple[dict[str, Any], str]:
    event_id = str(event["event_id"])
    if event.get("event_type") == "capture_authorized":
        return _load_hot_receipt(root, event_id), "hot_authorization_receipt_link"
    if event.get("event_type") == "neutral_saved":
        return _load_hot_receipt(root, event_id), "hot_neutral_receipt_link"
    try:
        found = capture_commit.lookup(
            _bootstrap_commit_dir(root),
            key_kind="event_id",
            value=event_id,
        )
        receipt_path = (
            _bootstrap_commit_dir(root)
            / capture_commit.RECEIPT_DIR
            / f"{found['receipt']['receipt_sha256']}.json"
        )
        receipt_raw = _read_bounded(
            receipt_path,
            maximum=MAX_RECEIPT_BYTES,
            label="bootstrap capture receipt",
        )
        if receipt_raw != _pretty(found["receipt"]):
            raise CaptureHotWriterError(
                "bootstrap capture receipt content address drifted"
            )
        return found["receipt"], "cold_bootstrap_bundle"
    except capture_commit.CommitIndexError as exc:
        if "not found for event_id=" not in str(exc):
            raise CaptureHotWriterError(
                f"bootstrap capture receipt index drifted: {exc}"
            ) from exc
        return _load_hot_receipt(root, event_id), "hot_receipt_link"


def _verify_joint_commit(
    ledger: Path,
    state_file: Path,
    root: Path,
    *,
    event_id: str | None,
    idempotency_key: str | None,
    max_state_bytes: int,
    allow_pending: bool,
) -> dict[str, Any]:
    if not allow_pending:
        _require_no_pending(root)
    joint, joint_sha = _load_joint(root)
    _assert_paths(joint, ledger, state_file)
    try:
        bounded_manifest, bounded_sha = _bounded_manifest_snapshot(
            ledger, _jsonl_index_dir(root)
        )
        _assert_bounded_binding(joint, bounded_manifest, bounded_sha)
        if event_id is not None:
            found = bounded_jsonl.lookup_event(
                ledger, _jsonl_index_dir(root), event_id
            )
        else:
            assert idempotency_key is not None
            found = bounded_jsonl.lookup_idempotency(
                ledger, _jsonl_index_dir(root), idempotency_key
            )
    except bounded_jsonl.BoundedJsonlIndexError as exc:
        raise CaptureHotWriterError(f"bounded JSONL verification failed: {exc}") from exc
    if found is None:
        raise CaptureHotWriterError("joint commit target is absent from exact index")
    event = _validate_event(found["event"])
    if event_id is not None and event["event_id"] != event_id:
        raise CaptureHotWriterError("joint commit event identity drifted")
    if idempotency_key is not None and event["idempotency_key"] != idempotency_key:
        raise CaptureHotWriterError("joint commit idempotency identity drifted")

    state_raw, state = _load_state(state_file, maximum=max_state_bytes)
    _assert_state_binding(joint, state_raw, state)
    if state.get("event_count") != bounded_manifest.get("event_count"):
        raise CaptureHotWriterError("joint state and JSONL event counts drifted")
    receipt, receipt_source = _resolve_receipt(root, event)
    _validate_receipt(receipt)
    record = found["record"]
    expected_bindings = {
        "event_id": event["event_id"],
        "idempotency_key": event["idempotency_key"],
        "event_sha256": record["line_sha256"],
        "byte_offset": record["byte_offset"],
        "byte_length": record["byte_length"],
    }
    if event["event_type"] == "fact_captured":
        expected_bindings["capture_id"] = event["capture_id"]
        expected_bindings["payload_sha256"] = event["payload_sha256"]
        if receipt.get("schema") != capture_commit.RECEIPT_SCHEMA:
            raise CaptureHotWriterError(
                "fact_captured event is bound to a non-fact receipt"
            )
    elif event["event_type"] == "capture_authorized":
        expected_bindings["capture_id"] = event["capture_id"]
        if receipt.get("schema") != AUTH_RECEIPT_SCHEMA:
            raise CaptureHotWriterError(
                "capture_authorized event is bound to a non-authorization receipt"
            )
        if "authorization_proof" in event:
            expected_proof = _validate_authorization_proof(
                event["authorization_proof"]
            )
            if (
                receipt.get("authorization_proof") != expected_proof
                or receipt.get("authorization_proof_sha256")
                != _sha256_value(expected_proof)
            ):
                raise CaptureHotWriterError(
                    "capture authorization proof receipt binding drifted"
                )
    else:
        expected_bindings["neutral_save_id"] = event["neutral_save_id"]
        expected_bindings["payload_sha256"] = event["payload_sha256"]
        if receipt.get("schema") != NEUTRAL_RECEIPT_SCHEMA:
            raise CaptureHotWriterError(
                "neutral_saved event is bound to a non-neutral receipt"
            )
    for key, value in expected_bindings.items():
        if receipt.get(key) != value:
            raise CaptureHotWriterError(f"capture receipt {key} binding drifted")
    chain = receipt.get("ledger_commitment")
    if not isinstance(chain, dict):
        raise CaptureHotWriterError("capture receipt ledger commitment is missing")
    expected_chain = _event_chain(
        str(chain.get("previous_sha256") or ""),
        str(receipt["event_sha256"]),
        int(receipt["byte_offset"]),
        int(receipt["byte_length"]),
        int(receipt["event_ordinal"]),
    )
    if chain.get("sha256") != expected_chain:
        raise CaptureHotWriterError("capture receipt event chain drifted")
    if event["event_type"] == "fact_captured":
        expected_state_projection = _initial_capture_state(event)
        state_projection_key = "capture_state_sha256"
        current_capture = state["captures"].get(str(event["capture_id"]))
        if (
            not isinstance(current_capture, Mapping)
            or current_capture.get("capture_id") != event["capture_id"]
            or current_capture.get("payload_sha256") != event["payload_sha256"]
        ):
            raise CaptureHotWriterError(
                "fact receipt capture is absent from current state"
            )
    elif event["event_type"] == "capture_authorized":
        current_capture = state["captures"].get(str(event["capture_id"]))
        if not isinstance(current_capture, Mapping):
            raise CaptureHotWriterError(
                "authorization receipt capture is absent from current state"
            )
        expected_state_projection = _authorized_capture_projection(
            str(event["capture_id"]),
            current_capture,
            include_recorded_at="recorded_at" in current_capture,
        )
        state_projection_key = "capture_state_sha256"
    else:
        neutral_saves = state.get("neutral_saves")
        current_neutral = (
            neutral_saves.get(str(event["neutral_save_id"]))
            if isinstance(neutral_saves, Mapping)
            else None
        )
        expected_state_projection = capture_model._neutral_save_material(event)
        if current_neutral != expected_state_projection:
            raise CaptureHotWriterError(
                "neutral receipt save is absent from current state"
            )
        state_projection_key = "neutral_save_state_sha256"
    state_commitment = receipt.get("state_commitment")
    valid_state_projection_sha256s = {
        _sha256_value(expected_state_projection)
    }
    # Historical hot receipts predate the recorded_at status projection.  The
    # immutable receipt stays valid for its exact legacy projection; all new
    # writes bind recorded_at, and the canonical ledger event remains the only
    # source used to derive that value.
    if event["event_type"] == "fact_captured":
        legacy_projection = dict(expected_state_projection)
        legacy_projection.pop("recorded_at", None)
        valid_state_projection_sha256s.add(_sha256_value(legacy_projection))
    elif event["event_type"] == "capture_authorized":
        valid_state_projection_sha256s.add(
            _sha256_value(
                _authorized_capture_projection(
                    str(event["capture_id"]),
                    current_capture,
                    include_recorded_at=False,
                )
            )
        )
    if (
        not isinstance(state_commitment, dict)
        or state_commitment.get(state_projection_key)
        not in valid_state_projection_sha256s
    ):
        raise CaptureHotWriterError("capture receipt state projection drifted")
    latest = (
        joint["capture_commit"].get("latest_event_id") == event["event_id"]
        and receipt.get("event_ordinal")
        == joint["bounded_jsonl"].get("event_count")
    )
    if latest and (
        state_commitment.get("post_event_state_sha256")
        != joint["state"]["sha256"]
    ):
        raise CaptureHotWriterError("latest receipt does not bind current state")
    if latest and (
        joint["capture_commit"].get("event_chain_sha256") != expected_chain
        or joint["capture_commit"].get("latest_receipt_sha256")
        != receipt.get("receipt_sha256")
    ):
        raise CaptureHotWriterError("latest receipt does not bind the joint head")
    result = {
        "schema": HOT_RESULT_SCHEMA,
        "status": "PASS",
        "event_type": event["event_type"],
        "event_id": event["event_id"],
        "capture_id": event.get("capture_id"),
        "neutral_save_id": event.get("neutral_save_id"),
        "receipt_sha256": receipt["receipt_sha256"],
        "event_sha256": receipt["event_sha256"],
        # The receipt binds serialized JSONL bytes; capture bindings hash the
        # canonical JSON object. Expose both without changing either contract.
        "canonical_event_sha256": _sha256_value(event),
        "receipt_source": receipt_source,
        "joint_manifest_sha256": joint_sha,
        "bounded_manifest_sha256": bounded_sha,
        "event_bytes_read": int(record["byte_length"]),
        "state_bytes_read": len(state_raw),
        "state_read_count": 1,
        "ledger_full_read_count": 0,
        "review_or_session_ledger_read_count": 0,
        "formal_write_count": 0,
        "formalization_authorized": (
            event["event_type"] == "capture_authorized"
            or bool(expected_state_projection.get("formalization_authorized"))
        ),
    }
    if event["event_type"] in {"fact_captured", "capture_authorized"}:
        result["quality_status"] = current_capture.get("quality_status")
    if event["event_type"] == "capture_authorized":
        result["authorization_proof_sha256"] = receipt.get(
            "authorization_proof_sha256"
        )
    return result


def verify_joint_commit(
    ledger_path: str | Path,
    state_path: str | Path,
    hot_root: str | Path,
    *,
    event_id: str | None = None,
    idempotency_key: str | None = None,
    max_state_bytes: int = DEFAULT_MAX_STATE_BYTES,
) -> dict[str, Any]:
    """Boundedly verify one exact capture against the current joint head."""

    if bool(event_id) == bool(idempotency_key):
        raise CaptureHotWriterError(
            "verify_joint_commit requires exactly one event_id or idempotency_key"
        )
    return _verify_joint_commit(
        Path(ledger_path).expanduser().resolve(),
        Path(state_path).expanduser().resolve(),
        Path(hot_root).expanduser().resolve(),
        event_id=event_id,
        idempotency_key=idempotency_key,
        max_state_bytes=max_state_bytes,
        allow_pending=False,
    )


def verify_joint_readiness(
    ledger_path: str | Path,
    state_path: str | Path,
    hot_root: str | Path,
    *,
    max_state_bytes: int = DEFAULT_MAX_STATE_BYTES,
) -> dict[str, Any]:
    """Boundedly prove the current capture head is safe for a later hot write.

    The readiness probe is intentionally separate from ``capture_fact`` so a
    stale joint head is detected before a learner submits an answer.  It never
    rebuilds an index, replays the capture ledger, or writes learner evidence.
    """

    ledger = Path(ledger_path).expanduser().resolve()
    state_file = Path(state_path).expanduser().resolve()
    root = Path(hot_root).expanduser().resolve()
    with capture_ledger_lock(ledger):
        _require_no_pending(root)
        joint, joint_sha = _load_joint(root)
        _assert_paths(joint, ledger, state_file)
        try:
            bounded_manifest, bounded_sha = _bounded_manifest_snapshot(
                ledger, _jsonl_index_dir(root)
            )
            _assert_bounded_binding(joint, bounded_manifest, bounded_sha)
        except bounded_jsonl.BoundedJsonlIndexError as exc:
            raise CaptureHotWriterError(
                f"bounded JSONL readiness failed: {exc}"
            ) from exc
        state_raw, state = _load_state(state_file, maximum=max_state_bytes)
        _assert_state_binding(joint, state_raw, state)
        event_count = int(bounded_manifest.get("event_count", -1))
        if event_count < 0 or state.get("event_count") != event_count:
            raise CaptureHotWriterError(
                "joint state and JSONL event counts drifted"
            )
        commit = joint.get("capture_commit")
        if not isinstance(commit, dict):
            raise CaptureHotWriterError("capture joint head is missing")
        latest_event_id = commit.get("latest_event_id")
        exact: dict[str, Any] | None = None
        if event_count == 0:
            if (
                latest_event_id is not None
                or commit.get("latest_receipt_sha256") is not None
                or int(commit.get("hot_receipt_count", -1)) != 0
                or commit.get("event_chain_sha256")
                != capture_commit.GENESIS_CHAIN_SHA256
            ):
                raise CaptureHotWriterError(
                    "empty capture joint head commitment drifted"
                )
        else:
            if not isinstance(latest_event_id, str) or not latest_event_id:
                raise CaptureHotWriterError(
                    "nonempty capture joint head lacks its latest event"
                )
            exact = _verify_joint_commit(
                ledger,
                state_file,
                root,
                event_id=latest_event_id,
                idempotency_key=None,
                max_state_bytes=max_state_bytes,
                allow_pending=False,
            )
            if exact.get("status") != "PASS":
                raise CaptureHotWriterError(
                    "latest capture joint commit did not verify"
                )
    return {
        "schema": HOT_RESULT_SCHEMA,
        "status": "READY",
        "joint_manifest_sha256": joint_sha,
        "bounded_manifest_sha256": bounded_sha,
        "event_count": event_count,
        "latest_event_id": latest_event_id,
        "latest_receipt_sha256": commit.get("latest_receipt_sha256"),
        "state_bytes_read": len(state_raw)
        + (int(exact["state_bytes_read"]) if exact is not None else 0),
        "state_read_count": 2 if exact is not None else 1,
        "ledger_full_read_count": 0,
        "learner_evidence_write_count": 0,
        "formal_write_count": 0,
        "read_only": True,
    }


def _fault(injector: FaultInjector | None, phase: str) -> None:
    if injector is not None:
        injector(phase)


def capture_fact(
    ledger_path: str | Path,
    state_path: str | Path,
    hot_root: str | Path,
    *,
    lock_token: CaptureLedgerLockToken,
    event: Mapping[str, Any] | None = None,
    validated_payload: Mapping[str, Any] | None = None,
    created_at: str | None = None,
    max_state_bytes: int = DEFAULT_MAX_STATE_BYTES,
    fault_injector: FaultInjector | None = None,
) -> dict[str, Any]:
    """Jointly commit one daytime ``fact_captured`` event without a ledger replay."""

    started = time.perf_counter()
    ledger = Path(ledger_path).expanduser().resolve()
    state_file = Path(state_path).expanduser().resolve()
    root = Path(hot_root).expanduser().resolve()
    _require_lock(lock_token, ledger)
    if bool(event is not None) == bool(validated_payload is not None):
        raise CaptureHotWriterError(
            "capture_fact requires exactly one canonical event or validated payload"
        )
    payload_mode = validated_payload is not None
    canonical_event = (
        _validate_event(event)
        if event is not None
        else canonical_fact_event_from_payload(
            validated_payload or {}, created_at=created_at
        )
    )
    line = _ledger_line(canonical_event)
    if len(line) > bounded_jsonl.DEFAULT_MAX_EVENT_BYTES:
        raise CaptureHotWriterError("fact_captured event exceeds hot JSONL bound")
    _require_no_pending(root)
    joint, joint_sha = _load_joint(root)
    _assert_paths(joint, ledger, state_file)
    if not (
        _jsonl_index_dir(root) / bounded_jsonl.MANIFEST_FILENAME
    ).is_file():
        raise CaptureHotWriterError(
            "bounded JSONL manifest is missing; cold bootstrap is required"
        )

    pending_written = False
    state_bytes_read = 0
    try:
        with bounded_jsonl.locked_writer(
            ledger,
            _jsonl_index_dir(root),
            max_tail_bytes=bounded_jsonl.DEFAULT_MAX_TAIL_BYTES,
            max_tail_events=1,
        ) as index_writer:
            bounded_manifest, bounded_sha = _bounded_manifest_snapshot(
                ledger, _jsonl_index_dir(root)
            )
            _assert_bounded_binding(joint, bounded_manifest, bounded_sha)
            state_raw, state = _load_state(state_file, maximum=max_state_bytes)
            state_bytes_read = len(state_raw)
            _assert_state_binding(joint, state_raw, state)
            if state["event_count"] != bounded_manifest["event_count"]:
                raise CaptureHotWriterError(
                    "state and exact index event counts differ; cold reconcile required"
                )

            prior = bounded_jsonl.lookup_idempotency(
                ledger,
                _jsonl_index_dir(root),
                str(canonical_event["idempotency_key"]),
            )
            if prior is not None:
                prior_event_value = _validate_event(prior["event"])
                same_commit_identity = all(
                    prior_event_value.get(key) == canonical_event.get(key)
                    for key in (
                        "event_id",
                        "idempotency_key",
                        "capture_id",
                        "payload_sha256",
                        "capture",
                    )
                )
                if (
                    (payload_mode and not same_commit_identity)
                    or (not payload_mode and prior_event_value != canonical_event)
                ):
                    raise CaptureHotWriterError(
                        "idempotency key is bound to different capture event content"
                    )
                verified = _verify_joint_commit(
                    ledger,
                    state_file,
                    root,
                    event_id=str(prior_event_value["event_id"]),
                    idempotency_key=None,
                    max_state_bytes=max_state_bytes,
                    allow_pending=False,
                )
                joint_verification = dict(verified)
                verified.update(
                    {
                        "status": "ALREADY_COMMITTED",
                        "created": False,
                        "quality_status": state["captures"][
                            str(prior_event_value["capture_id"])
                        ]["quality_status"],
                        "joint_verification": joint_verification,
                        "state_bytes_read": state_bytes_read
                        + int(verified["state_bytes_read"]),
                        "state_read_count": 2,
                        "elapsed_ms": (time.perf_counter() - started) * 1000,
                    }
                )
                return verified
            prior_event = bounded_jsonl.lookup_event(
                ledger,
                _jsonl_index_dir(root),
                str(canonical_event["event_id"]),
            )
            if prior_event is not None:
                raise CaptureHotWriterError(
                    "event_id exists without the matching idempotency binding"
                )
            if canonical_event["capture_id"] in state["captures"]:
                raise CaptureHotWriterError(
                    "capture state contains an unindexed capture_id; cold reconcile required"
                )

            offset = int(bounded_manifest["ledger"]["size_bytes"])
            ordinal = int(bounded_manifest["event_count"]) + 1
            new_state = _increment_state(state, canonical_event)
            new_state_raw = _state_bytes(new_state)
            if len(new_state_raw) > max_state_bytes:
                raise CaptureHotWriterError("incremented capture state exceeds bounded size")
            receipt = capture_commit.build_append_receipt(
                event_bytes=line,
                byte_offset=offset,
                event_ordinal=ordinal,
                previous_chain_sha256=joint["capture_commit"][
                    "event_chain_sha256"
                ],
                post_event_state=new_state,
            )
            pending = {
                "schema": PENDING_SCHEMA,
                "previous_joint_manifest_sha256": joint_sha,
                "event_id": canonical_event["event_id"],
                "capture_id": canonical_event["capture_id"],
                "idempotency_key_sha256": _sha256_bytes(
                    str(canonical_event["idempotency_key"]).encode("utf-8")
                ),
                "event_sha256": receipt["event_sha256"],
                "payload_sha256": canonical_event["payload_sha256"],
                "expected_byte_offset": offset,
                "expected_event_ordinal": ordinal,
                "state_before_sha256": _sha256_bytes(state_raw),
                "state_after_sha256": _sha256_bytes(new_state_raw),
                "formal_write_count": 0,
            }
            _write_pending(root, pending)
            pending_written = True
            _fault(fault_injector, "after_pending_publish")

            info = _regular_file(ledger, "capture ledger")
            if (
                info.st_size != offset
                or info.st_dev != bounded_manifest["ledger"]["device"]
                or info.st_ino != bounded_manifest["ledger"]["inode"]
            ):
                raise CaptureHotWriterError("capture ledger drifted before append")
            with ledger.open("ab") as handle:
                handle.write(line)
                handle.flush()
                os.fsync(handle.fileno())
            _fault(fault_injector, "after_ledger_fsync")

            _atomic_write(state_file, new_state_raw)
            _fault(fault_injector, "after_state_publish")

            index_result = index_writer.recover_tail()
            if index_result.get("recovered_event_count") != 1:
                raise CaptureHotWriterError(
                    "bounded JSONL index did not publish exactly one event"
                )
            _fault(fault_injector, "after_jsonl_index_publish")

            _write_capture_receipt(root, receipt)
            _write_link(
                root,
                event_id=str(canonical_event["event_id"]),
                capture_id=str(canonical_event["capture_id"]),
                receipt_sha256=str(receipt["receipt_sha256"]),
            )
            _fault(fault_injector, "after_capture_receipt_publish")

            new_bounded_manifest, new_bounded_sha = _bounded_manifest_snapshot(
                ledger, _jsonl_index_dir(root)
            )
            if (
                new_bounded_manifest["event_count"] != ordinal
                or new_bounded_manifest["ledger"]["size_bytes"] != offset + len(line)
            ):
                raise CaptureHotWriterError("published JSONL index head drifted")
            material = _joint_material(
                previous_joint_sha256=joint_sha,
                generation_number=int(joint["generation_number"]) + 1,
                mode="hot_fact_capture",
                ledger=ledger,
                state_path=state_file,
                state_raw=new_state_raw,
                state=new_state,
                bounded_manifest=new_bounded_manifest,
                bounded_manifest_sha=new_bounded_sha,
                bootstrap_index_sha256=joint["capture_commit"][
                    "bootstrap_index_sha256"
                ],
                event_chain_sha256=receipt["ledger_commitment"]["sha256"],
                latest_receipt_sha256=receipt["receipt_sha256"],
                latest_event_id=canonical_event["event_id"],
                hot_receipt_count=int(
                    joint["capture_commit"]["hot_receipt_count"]
                )
                + 1,
            )
            _, new_joint_sha = _publish_joint(root, material)
            _fault(fault_injector, "after_joint_manifest_publish")

            verified = _verify_joint_commit(
                ledger,
                state_file,
                root,
                event_id=str(canonical_event["event_id"]),
                idempotency_key=None,
                max_state_bytes=max_state_bytes,
                allow_pending=True,
            )
            joint_verification = dict(verified)
            _clear_pending(root)
            pending_written = False
            verified.update(
                {
                    "status": "CAPTURED",
                    "created": True,
                    "joint_verification": joint_verification,
                    "quality_status": new_state["captures"][
                        canonical_event["capture_id"]
                    ]["quality_status"],
                    "joint_manifest_sha256": new_joint_sha,
                    "jsonl_index_receipt_sha256": index_result[
                        "receipt_sha256"
                    ],
                    "elapsed_ms": (time.perf_counter() - started) * 1000,
                    "state_bytes_read": state_bytes_read
                    + int(verified["state_bytes_read"]),
                    "state_read_count": 2,
                    "ledger_tail_bytes_read": len(line),
                    "ledger_full_read_count": 0,
                    "review_or_session_ledger_read_count": 0,
                    "formal_write_count": 0,
                }
            )
            return verified
    except CaptureHotWriterError:
        raise
    except Exception as exc:
        if pending_written or _pending_path(root).exists():
            raise CaptureHotWriterError(
                "joint capture commit stopped after intent publication; no success was "
                "declared. Run authoritative capture reconcile and then "
                "cold_bootstrap(reconcile=True)"
            ) from exc
        raise CaptureHotWriterError(f"hot capture preflight failed: {exc}") from exc


def save_neutral_event(
    ledger_path: str | Path,
    state_path: str | Path,
    hot_root: str | Path,
    *,
    lock_token: CaptureLedgerLockToken,
    event: Mapping[str, Any],
    max_state_bytes: int = DEFAULT_MAX_STATE_BYTES,
    fault_injector: FaultInjector | None = None,
) -> dict[str, Any]:
    """Jointly commit one canonical ``neutral_saved`` event without replay."""

    started = time.perf_counter()
    ledger = Path(ledger_path).expanduser().resolve()
    state_file = Path(state_path).expanduser().resolve()
    root = Path(hot_root).expanduser().resolve()
    _require_lock(lock_token, ledger)
    canonical_event = _validate_neutral_event(event)
    line = _ledger_line(canonical_event)
    if len(line) > bounded_jsonl.DEFAULT_MAX_EVENT_BYTES:
        raise CaptureHotWriterError("neutral_saved event exceeds hot JSONL bound")
    _require_no_pending(root)
    joint, joint_sha = _load_joint(root)
    _assert_paths(joint, ledger, state_file)
    if not (
        _jsonl_index_dir(root) / bounded_jsonl.MANIFEST_FILENAME
    ).is_file():
        raise CaptureHotWriterError(
            "bounded JSONL manifest is missing; cold bootstrap is required"
        )

    pending_written = False
    state_bytes_read = 0
    try:
        with bounded_jsonl.locked_writer(
            ledger,
            _jsonl_index_dir(root),
            max_tail_bytes=bounded_jsonl.DEFAULT_MAX_TAIL_BYTES,
            max_tail_events=1,
        ) as index_writer:
            bounded_manifest, bounded_sha = _bounded_manifest_snapshot(
                ledger, _jsonl_index_dir(root)
            )
            _assert_bounded_binding(joint, bounded_manifest, bounded_sha)
            state_raw, state = _load_state(state_file, maximum=max_state_bytes)
            state_bytes_read = len(state_raw)
            _assert_state_binding(joint, state_raw, state)
            if state["event_count"] != bounded_manifest["event_count"]:
                raise CaptureHotWriterError(
                    "state and exact index event counts differ; cold reconcile required"
                )

            prior = bounded_jsonl.lookup_idempotency(
                ledger,
                _jsonl_index_dir(root),
                str(canonical_event["idempotency_key"]),
            )
            if prior is not None:
                prior_event = _validate_neutral_event(prior["event"])
                immutable_fields = (
                    "event_id",
                    "idempotency_key",
                    "neutral_save_id",
                    "study_date",
                    "timezone",
                    "review_event_id",
                    "review_event_sha256",
                    "source_transaction_kind",
                    "source_transaction_attestation_sha256",
                    "formalization_authorized",
                    "curation_eligible",
                    "status",
                )
                if any(
                    prior_event.get(key) != canonical_event.get(key)
                    for key in immutable_fields
                ):
                    raise CaptureHotWriterError(
                        "neutral idempotency key is bound to different review evidence"
                    )
                verified = _verify_joint_commit(
                    ledger,
                    state_file,
                    root,
                    event_id=str(prior_event["event_id"]),
                    idempotency_key=None,
                    max_state_bytes=max_state_bytes,
                    allow_pending=False,
                )
                joint_verification = dict(verified)
                stored_material = capture_model._neutral_save_material(prior_event)
                verified.update(
                    {
                        "status": "ALREADY_SAVED_NEUTRAL",
                        "created": False,
                        "quality_status": capture_model.NEUTRAL_SAVE_STATUS,
                        "neutral_material": stored_material,
                        "joint_verification": joint_verification,
                        "state_bytes_read": state_bytes_read
                        + int(verified["state_bytes_read"]),
                        "state_read_count": 2,
                        "elapsed_ms": (time.perf_counter() - started) * 1000,
                    }
                )
                return verified
            prior_event = bounded_jsonl.lookup_event(
                ledger,
                _jsonl_index_dir(root),
                str(canonical_event["event_id"]),
            )
            if prior_event is not None:
                raise CaptureHotWriterError(
                    "neutral event_id exists without matching idempotency"
                )

            offset = int(bounded_manifest["ledger"]["size_bytes"])
            ordinal = int(bounded_manifest["event_count"]) + 1
            new_state = _increment_neutral_state(state, canonical_event)
            new_state_raw = _state_bytes(new_state)
            if len(new_state_raw) > max_state_bytes:
                raise CaptureHotWriterError(
                    "incremented neutral save state exceeds bounded size"
                )
            receipt = _build_neutral_receipt(
                event_bytes=line,
                byte_offset=offset,
                event_ordinal=ordinal,
                previous_chain_sha256=joint["capture_commit"][
                    "event_chain_sha256"
                ],
                post_event_state=new_state,
            )
            pending = {
                "schema": PENDING_SCHEMA,
                "previous_joint_manifest_sha256": joint_sha,
                "event_type": "neutral_saved",
                "event_id": canonical_event["event_id"],
                "neutral_save_id": canonical_event["neutral_save_id"],
                "idempotency_key_sha256": _sha256_bytes(
                    str(canonical_event["idempotency_key"]).encode("utf-8")
                ),
                "event_sha256": receipt["event_sha256"],
                "payload_sha256": canonical_event["payload_sha256"],
                "expected_byte_offset": offset,
                "expected_event_ordinal": ordinal,
                "state_before_sha256": _sha256_bytes(state_raw),
                "state_after_sha256": _sha256_bytes(new_state_raw),
                "formal_write_count": 0,
            }
            _write_pending(root, pending)
            pending_written = True
            _fault(fault_injector, "after_neutral_pending_publish")

            info = _regular_file(ledger, "capture ledger")
            if (
                info.st_size != offset
                or info.st_dev != bounded_manifest["ledger"]["device"]
                or info.st_ino != bounded_manifest["ledger"]["inode"]
            ):
                raise CaptureHotWriterError(
                    "capture ledger drifted before neutral append"
                )
            with ledger.open("ab") as handle:
                handle.write(line)
                handle.flush()
                os.fsync(handle.fileno())
            _fault(fault_injector, "after_neutral_ledger_fsync")

            _atomic_write(state_file, new_state_raw)
            _fault(fault_injector, "after_neutral_state_publish")

            index_result = index_writer.recover_tail()
            if index_result.get("recovered_event_count") != 1:
                raise CaptureHotWriterError(
                    "bounded JSONL index did not publish neutral event"
                )
            _fault(fault_injector, "after_neutral_index_publish")

            _write_capture_receipt(root, receipt)
            _write_link(
                root,
                event_id=str(canonical_event["event_id"]),
                capture_id=None,
                receipt_sha256=str(receipt["receipt_sha256"]),
            )
            _fault(fault_injector, "after_neutral_receipt_publish")

            new_bounded_manifest, new_bounded_sha = _bounded_manifest_snapshot(
                ledger, _jsonl_index_dir(root)
            )
            if (
                new_bounded_manifest["event_count"] != ordinal
                or new_bounded_manifest["ledger"]["size_bytes"]
                != offset + len(line)
            ):
                raise CaptureHotWriterError(
                    "published neutral JSONL head drifted"
                )
            material = _joint_material(
                previous_joint_sha256=joint_sha,
                generation_number=int(joint["generation_number"]) + 1,
                mode="hot_neutral_save",
                ledger=ledger,
                state_path=state_file,
                state_raw=new_state_raw,
                state=new_state,
                bounded_manifest=new_bounded_manifest,
                bounded_manifest_sha=new_bounded_sha,
                bootstrap_index_sha256=joint["capture_commit"][
                    "bootstrap_index_sha256"
                ],
                event_chain_sha256=receipt["ledger_commitment"]["sha256"],
                latest_receipt_sha256=receipt["receipt_sha256"],
                latest_event_id=canonical_event["event_id"],
                hot_receipt_count=int(
                    joint["capture_commit"]["hot_receipt_count"]
                )
                + 1,
            )
            _, new_joint_sha = _publish_joint(root, material)
            _fault(fault_injector, "after_neutral_joint_publish")

            verified = _verify_joint_commit(
                ledger,
                state_file,
                root,
                event_id=str(canonical_event["event_id"]),
                idempotency_key=None,
                max_state_bytes=max_state_bytes,
                allow_pending=True,
            )
            joint_verification = dict(verified)
            _clear_pending(root)
            pending_written = False
            verified.update(
                {
                    "status": "SAVED_NEUTRAL",
                    "created": True,
                    "quality_status": capture_model.NEUTRAL_SAVE_STATUS,
                    "neutral_material": capture_model._neutral_save_material(
                        canonical_event
                    ),
                    "joint_verification": joint_verification,
                    "joint_manifest_sha256": new_joint_sha,
                    "jsonl_index_receipt_sha256": index_result[
                        "receipt_sha256"
                    ],
                    "elapsed_ms": (time.perf_counter() - started) * 1000,
                    "state_bytes_read": state_bytes_read
                    + int(verified["state_bytes_read"]),
                    "state_read_count": 2,
                    "ledger_tail_bytes_read": len(line),
                    "ledger_full_read_count": 0,
                    "review_or_session_ledger_read_count": 0,
                    "formalization_authorized": False,
                    "formal_write_count": 0,
                }
            )
            return verified
    except CaptureHotWriterError:
        raise
    except Exception as exc:
        if pending_written or _pending_path(root).exists():
            raise CaptureHotWriterError(
                "joint neutral save stopped after intent publication; no success "
                "was declared. Run authoritative capture reconcile and then "
                "cold_bootstrap(reconcile=True)"
            ) from exc
        raise CaptureHotWriterError(
            f"hot neutral save preflight failed: {exc}"
        ) from exc


def authorize_capture(
    ledger_path: str | Path,
    state_path: str | Path,
    hot_root: str | Path,
    *,
    lock_token: CaptureLedgerLockToken,
    capture_id: str | None = None,
    idempotency_key: str | None = None,
    authorization_proof: Mapping[str, Any] | None = None,
    event: Mapping[str, Any] | None = None,
    created_at: str | None = None,
    max_state_bytes: int = DEFAULT_MAX_STATE_BYTES,
    fault_injector: FaultInjector | None = None,
) -> dict[str, Any]:
    """Append one independent ``capture_authorized`` event without replay.

    The caller keeps the same capture ledger lock across fact capture and this
    authorization.  A fault after a preceding fact commit therefore leaves an
    exact unconfirmed capture that can be resumed with the same authorization
    idempotency key; a fault after this function publishes its pending marker
    remains fenced for cold reconcile.
    """

    started = time.perf_counter()
    ledger = Path(ledger_path).expanduser().resolve()
    state_file = Path(state_path).expanduser().resolve()
    root = Path(hot_root).expanduser().resolve()
    _require_lock(lock_token, ledger)
    if event is not None:
        if (
            capture_id is not None
            or idempotency_key is not None
            or authorization_proof is not None
        ):
            raise CaptureHotWriterError(
                "authorize_capture accepts either event or capture/key/proof identities"
            )
        canonical_event = _validate_authorization_event(
            event, allow_legacy_read=False
        )
        generated_mode = False
    else:
        if capture_id is None or idempotency_key is None:
            raise CaptureHotWriterError(
                "authorize_capture requires capture_id and idempotency_key"
            )
        canonical_event = canonical_authorization_event(
            capture_id=capture_id,
            idempotency_key=idempotency_key,
            authorization_proof=authorization_proof,
            created_at=created_at,
        )
        generated_mode = True
    line = _ledger_line(canonical_event)
    if len(line) > bounded_jsonl.DEFAULT_MAX_EVENT_BYTES:
        raise CaptureHotWriterError(
            "capture_authorized event exceeds hot JSONL bound"
        )
    _require_no_pending(root)
    joint, joint_sha = _load_joint(root)
    _assert_paths(joint, ledger, state_file)
    if not (
        _jsonl_index_dir(root) / bounded_jsonl.MANIFEST_FILENAME
    ).is_file():
        raise CaptureHotWriterError(
            "bounded JSONL manifest is missing; cold bootstrap is required"
        )

    pending_written = False
    state_bytes_read = 0
    try:
        with bounded_jsonl.locked_writer(
            ledger,
            _jsonl_index_dir(root),
            max_tail_bytes=bounded_jsonl.DEFAULT_MAX_TAIL_BYTES,
            max_tail_events=1,
        ) as index_writer:
            bounded_manifest, bounded_sha = _bounded_manifest_snapshot(
                ledger, _jsonl_index_dir(root)
            )
            _assert_bounded_binding(joint, bounded_manifest, bounded_sha)
            state_raw, state = _load_state(state_file, maximum=max_state_bytes)
            state_bytes_read = len(state_raw)
            _assert_state_binding(joint, state_raw, state)
            if state["event_count"] != bounded_manifest["event_count"]:
                raise CaptureHotWriterError(
                    "state and exact index event counts differ; cold reconcile required"
                )

            prior = bounded_jsonl.lookup_idempotency(
                ledger,
                _jsonl_index_dir(root),
                str(canonical_event["idempotency_key"]),
            )
            if prior is not None:
                prior_event = _validate_authorization_event(prior["event"])
                same_identity = all(
                    prior_event.get(key) == canonical_event.get(key)
                    for key in (
                        "event_id",
                        "idempotency_key",
                        "capture_id",
                    )
                )
                prior_proof = prior_event.get("authorization_proof")
                current_proof = canonical_event.get("authorization_proof")
                same_proof = prior_proof == current_proof or (
                    prior_proof is None
                )
                if not same_identity or not same_proof or (
                    not generated_mode and prior_event != canonical_event
                ):
                    raise CaptureHotWriterError(
                        "idempotency key is bound to different authorization content"
                    )
                verified = _verify_joint_commit(
                    ledger,
                    state_file,
                    root,
                    event_id=str(prior_event["event_id"]),
                    idempotency_key=None,
                    max_state_bytes=max_state_bytes,
                    allow_pending=False,
                )
                joint_verification = dict(verified)
                current_capture = state["captures"].get(
                    str(prior_event["capture_id"])
                )
                verified.update(
                    {
                        "status": "ALREADY_AUTHORIZED",
                        "created": False,
                        "authorization_created": False,
                        "quality_status": (
                            current_capture.get("quality_status")
                            if isinstance(current_capture, Mapping)
                            else None
                        ),
                        "joint_verification": joint_verification,
                        "state_bytes_read": state_bytes_read
                        + int(verified["state_bytes_read"]),
                        "state_read_count": 2,
                        "elapsed_ms": (time.perf_counter() - started) * 1000,
                    }
                )
                return verified
            prior_event = bounded_jsonl.lookup_event(
                ledger,
                _jsonl_index_dir(root),
                str(canonical_event["event_id"]),
            )
            if prior_event is not None:
                raise CaptureHotWriterError(
                    "authorization event_id exists without matching idempotency"
                )

            offset = int(bounded_manifest["ledger"]["size_bytes"])
            ordinal = int(bounded_manifest["event_count"]) + 1
            new_state = _increment_authorization_state(state, canonical_event)
            new_state_raw = _state_bytes(new_state)
            if len(new_state_raw) > max_state_bytes:
                raise CaptureHotWriterError(
                    "authorized capture state exceeds bounded size"
                )
            receipt = _build_authorization_receipt(
                event_bytes=line,
                byte_offset=offset,
                event_ordinal=ordinal,
                previous_chain_sha256=joint["capture_commit"][
                    "event_chain_sha256"
                ],
                post_event_state=new_state,
            )
            pending = {
                "schema": PENDING_SCHEMA,
                "previous_joint_manifest_sha256": joint_sha,
                "event_type": "capture_authorized",
                "event_id": canonical_event["event_id"],
                "capture_id": canonical_event["capture_id"],
                "idempotency_key_sha256": _sha256_bytes(
                    str(canonical_event["idempotency_key"]).encode("utf-8")
                ),
                "event_sha256": receipt["event_sha256"],
                "expected_byte_offset": offset,
                "expected_event_ordinal": ordinal,
                "state_before_sha256": _sha256_bytes(state_raw),
                "state_after_sha256": _sha256_bytes(new_state_raw),
                "formal_write_count": 0,
            }
            _write_pending(root, pending)
            pending_written = True
            _fault(fault_injector, "after_authorization_pending_publish")

            info = _regular_file(ledger, "capture ledger")
            if (
                info.st_size != offset
                or info.st_dev != bounded_manifest["ledger"]["device"]
                or info.st_ino != bounded_manifest["ledger"]["inode"]
            ):
                raise CaptureHotWriterError(
                    "capture ledger drifted before authorization append"
                )
            with ledger.open("ab") as handle:
                handle.write(line)
                handle.flush()
                os.fsync(handle.fileno())
            _fault(fault_injector, "after_authorization_ledger_fsync")

            _atomic_write(state_file, new_state_raw)
            _fault(fault_injector, "after_authorization_state_publish")

            index_result = index_writer.recover_tail()
            if index_result.get("recovered_event_count") != 1:
                raise CaptureHotWriterError(
                    "bounded JSONL index did not publish authorization event"
                )
            _fault(fault_injector, "after_authorization_index_publish")

            _write_capture_receipt(root, receipt)
            _write_link(
                root,
                event_id=str(canonical_event["event_id"]),
                capture_id=str(canonical_event["capture_id"]),
                receipt_sha256=str(receipt["receipt_sha256"]),
            )
            _fault(fault_injector, "after_authorization_receipt_publish")

            new_bounded_manifest, new_bounded_sha = _bounded_manifest_snapshot(
                ledger, _jsonl_index_dir(root)
            )
            if (
                new_bounded_manifest["event_count"] != ordinal
                or new_bounded_manifest["ledger"]["size_bytes"]
                != offset + len(line)
            ):
                raise CaptureHotWriterError(
                    "published authorization JSONL head drifted"
                )
            material = _joint_material(
                previous_joint_sha256=joint_sha,
                generation_number=int(joint["generation_number"]) + 1,
                mode="hot_capture_authorization",
                ledger=ledger,
                state_path=state_file,
                state_raw=new_state_raw,
                state=new_state,
                bounded_manifest=new_bounded_manifest,
                bounded_manifest_sha=new_bounded_sha,
                bootstrap_index_sha256=joint["capture_commit"][
                    "bootstrap_index_sha256"
                ],
                event_chain_sha256=receipt["ledger_commitment"]["sha256"],
                latest_receipt_sha256=receipt["receipt_sha256"],
                latest_event_id=canonical_event["event_id"],
                hot_receipt_count=int(
                    joint["capture_commit"]["hot_receipt_count"]
                )
                + 1,
            )
            _, new_joint_sha = _publish_joint(root, material)
            _fault(fault_injector, "after_authorization_joint_publish")

            verified = _verify_joint_commit(
                ledger,
                state_file,
                root,
                event_id=str(canonical_event["event_id"]),
                idempotency_key=None,
                max_state_bytes=max_state_bytes,
                allow_pending=True,
            )
            joint_verification = dict(verified)
            _clear_pending(root)
            pending_written = False
            verified.update(
                {
                    "status": "AUTHORIZED",
                    "created": True,
                    "authorization_created": True,
                    "joint_verification": joint_verification,
                    "quality_status": capture_model.PENDING_FORMAL_STATUS,
                    "joint_manifest_sha256": new_joint_sha,
                    "jsonl_index_receipt_sha256": index_result[
                        "receipt_sha256"
                    ],
                    "elapsed_ms": (time.perf_counter() - started) * 1000,
                    "state_bytes_read": state_bytes_read
                    + int(verified["state_bytes_read"]),
                    "state_read_count": 2,
                    "ledger_tail_bytes_read": len(line),
                    "ledger_full_read_count": 0,
                    "review_or_session_ledger_read_count": 0,
                    "formalization_authorized": True,
                    "formal_write_count": 0,
                }
            )
            return verified
    except CaptureHotWriterError:
        raise
    except Exception as exc:
        if pending_written or _pending_path(root).exists():
            raise CaptureHotWriterError(
                "joint authorization commit stopped after intent publication; no "
                "success was declared. Run authoritative capture reconcile and "
                "then cold_bootstrap(reconcile=True)"
            ) from exc
        raise CaptureHotWriterError(
            f"hot capture authorization preflight failed: {exc}"
        ) from exc


__all__ = [
    "CaptureHotWriterError",
    "CaptureLedgerLockToken",
    "capture_ledger_lock",
    "canonical_authorization_event",
    "canonical_fact_event_from_payload",
    "cold_bootstrap",
    "authorize_capture",
    "capture_fact",
    "read_pending_identity",
    "save_neutral_event",
    "verify_joint_commit",
    "verify_joint_readiness",
]
