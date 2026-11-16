#!/usr/bin/env python3
"""Bounded hot-state adapter for one existing morning-review session.

The immutable ``events.jsonl`` ledger remains the truth.  A cold bootstrap is
allowed to replay that one ledger once.  Afterwards, state loads verify only a
64 KiB state projection, signed hot manifest, signed bounded-index manifest,
and ledger stat/tail commitments.  Exact event verification reads only the
indexed event and its bounded chunk(s).

This module intentionally does not replace ``morning_review_session.py``.  A
caller that commits an event must already hold that module's per-session lock.
"""

from __future__ import annotations

import copy
import hashlib
import hmac
import json
import os
import re
import stat
import tempfile
from pathlib import Path
from typing import Any

import bounded_jsonl_index_408 as bounded
import morning_review_session as morning


HOT_SCHEMA = "morning_session_hot_state_v1"
SEQUENCE_SCHEMA = "morning_session_sequence_index_v1"
RECEIPT_SCHEMA = "morning_session_hot_commit_receipt_v1"
BOOTSTRAP_RECEIPT_SCHEMA = "morning_session_hot_bootstrap_receipt_v1"
POINTER_SCHEMA = "morning_session_hot_idempotency_pointer_v1"
PENDING_SCHEMA = "morning_session_hot_pending_v1"
STATE_SCHEMA = "morning_review_session_state_v1"
EVENT_SCHEMA = "morning_review_session_event_v1"

DEFAULT_HOT_DIRNAME = ".morning-session-hot-v1"
DEFAULT_STATE_BUDGET_BYTES = 64 * 1024
MAX_STATE_BUDGET_BYTES = 64 * 1024
MAX_HOT_MANIFEST_BYTES = 128 * 1024
MAX_RECEIPT_BYTES = 128 * 1024
MAX_SEQUENCE_INDEX_BYTES = 512 * 1024
MAX_SESSION_EVENTS = 4096
MAX_POINTER_BYTES = 16 * 1024
MAX_PENDING_BYTES = 16 * 1024
HOT_KEY_FILENAME = ".morning-session-hot-state.key"
HOT_MANIFEST_FILENAME = "manifest.json"
PENDING_FILENAME = "pending.json"

SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
EVENT_TYPE_RE = re.compile(r"^[a-z][a-z0-9_]{1,80}$")


class MorningSessionHotStateError(RuntimeError):
    """Stable fail-closed error raised by a mutating or exact-read operation."""


def _canonical_bytes(value: Any) -> bytes:
    return json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")


def _pretty_bytes(value: Any) -> bytes:
    return (
        json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2) + "\n"
    ).encode("utf-8")


def _event_digest(value: dict[str, Any]) -> str:
    # bounded_jsonl_index_408 hashes the parsed event in this exact form.
    return hashlib.sha256(_canonical_bytes(value) + b"\n").hexdigest()


def _logical_digest(value: Any) -> str:
    return hashlib.sha256(_canonical_bytes(value)).hexdigest()


def _sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _path_digest(path: Path) -> str:
    return _sha256_bytes(str(path.resolve()).encode("utf-8"))


def _fsync_dir(path: Path) -> None:
    descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _atomic_write(path: Path, payload: bytes, *, mode: int = 0o600) -> str:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    descriptor, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        os.fchmod(descriptor, mode)
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
        _fsync_dir(path.parent)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)
    return _sha256_bytes(payload)


def _read_bytes(path: Path, label: str, *, max_bytes: int) -> bytes:
    try:
        info = path.lstat()
        if stat.S_ISLNK(info.st_mode) or not stat.S_ISREG(info.st_mode):
            raise MorningSessionHotStateError(
                f"{label} must be a regular non-symlink file"
            )
        if info.st_size > max_bytes:
            raise MorningSessionHotStateError(f"{label} exceeds its bounded size")
        with path.open("rb") as handle:
            payload = handle.read(max_bytes + 1)
    except MorningSessionHotStateError:
        raise
    except OSError as exc:
        raise MorningSessionHotStateError(f"{label} is unavailable") from exc
    if len(payload) > max_bytes:
        raise MorningSessionHotStateError(f"{label} exceeds its bounded size")
    return payload


def _read_json(path: Path, label: str, *, max_bytes: int) -> tuple[dict[str, Any], bytes]:
    payload = _read_bytes(path, label, max_bytes=max_bytes)
    try:
        value = json.loads(payload)
    except (UnicodeError, json.JSONDecodeError) as exc:
        raise MorningSessionHotStateError(f"{label} is invalid JSON") from exc
    if not isinstance(value, dict):
        raise MorningSessionHotStateError(f"{label} must be a JSON object")
    return value, payload


def _ensure_directory(path: Path, *, create: bool) -> None:
    if create:
        path.mkdir(parents=True, exist_ok=True, mode=0o700)
    try:
        info = path.lstat()
    except OSError as exc:
        raise MorningSessionHotStateError(f"directory is unavailable: {path}") from exc
    if stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode):
        raise MorningSessionHotStateError(f"directory must not be a symlink: {path}")


def _paths(
    session_dir: str | Path, hot_dir: str | Path | None
) -> tuple[Path, Path, Path, Path]:
    session = Path(session_dir).expanduser().resolve()
    _ensure_directory(session, create=False)
    ledger = session / "events.jsonl"
    state = session / "state.json"
    hot = (
        (session / DEFAULT_HOT_DIRNAME).resolve()
        if hot_dir is None
        else Path(hot_dir).expanduser().resolve()
    )
    return session, ledger, state, hot


def _validate_budget(value: int) -> int:
    if not isinstance(value, int) or value <= 0 or value > MAX_STATE_BUDGET_BYTES:
        raise MorningSessionHotStateError(
            f"state budget must be between 1 and {MAX_STATE_BUDGET_BYTES} bytes"
        )
    return value


def _load_or_create_key(hot_dir: Path, *, create: bool) -> bytes:
    path = hot_dir / HOT_KEY_FILENAME
    if not path.exists():
        if not create:
            raise MorningSessionHotStateError("hot-state signing key is missing")
        _ensure_directory(hot_dir, create=True)
        descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        try:
            os.write(descriptor, os.urandom(32))
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
        _fsync_dir(hot_dir)
    try:
        info = path.lstat()
    except OSError as exc:
        raise MorningSessionHotStateError("hot-state signing key is unavailable") from exc
    if stat.S_ISLNK(info.st_mode) or not stat.S_ISREG(info.st_mode) or info.st_mode & 0o077:
        raise MorningSessionHotStateError("hot-state signing key permissions are unsafe")
    key = path.read_bytes()
    if len(key) != 32:
        raise MorningSessionHotStateError("hot-state signing key is invalid")
    return key


def _sign(value: dict[str, Any], key: bytes) -> dict[str, Any]:
    core = dict(value)
    core.pop("hmac_sha256", None)
    signature = hmac.new(key, _canonical_bytes(core), hashlib.sha256).hexdigest()
    return {**core, "hmac_sha256": signature}


def _verify_signature(value: dict[str, Any], key: bytes, label: str) -> dict[str, Any]:
    signature = str(value.get("hmac_sha256") or "")
    core = dict(value)
    core.pop("hmac_sha256", None)
    expected = hmac.new(key, _canonical_bytes(core), hashlib.sha256).hexdigest()
    if not SHA256_RE.fullmatch(signature) or not hmac.compare_digest(signature, expected):
        raise MorningSessionHotStateError(f"{label} signature drifted")
    return core


def _content_write(directory: Path, value: dict[str, Any]) -> tuple[Path, str, int]:
    payload = _pretty_bytes(value)
    digest = _sha256_bytes(payload)
    path = directory / f"{digest}.json"
    if path.exists():
        prior = _read_bytes(path, "content-addressed object", max_bytes=max(len(payload), 1))
        if prior != payload:
            raise MorningSessionHotStateError("content-addressed object collision")
    else:
        _atomic_write(path, payload)
    return path, digest, len(payload)


def _failure(
    status: str, reason_code: str, detail: str, *, ledger_read_bytes: int = 0
) -> dict[str, Any]:
    return {
        "status": status,
        "reason_code": reason_code,
        "detail": detail,
        "ledger_read_bytes": ledger_read_bytes,
        "answer_safe": True,
        "formal_write_count": 0,
    }


def _state_payload(
    state_path: Path, *, state_budget_bytes: int
) -> tuple[dict[str, Any], bytes]:
    value, payload = _read_json(
        state_path, "morning session state", max_bytes=state_budget_bytes
    )
    return value, payload


def _validate_state(state: dict[str, Any], session_id: str) -> int:
    if state.get("schema") != STATE_SCHEMA:
        raise MorningSessionHotStateError("state has an invalid schema")
    if state.get("session_id") != session_id:
        raise MorningSessionHotStateError("state/session identity drifted")
    sequence = state.get("last_sequence")
    if not isinstance(sequence, int) or sequence <= 0 or sequence > MAX_SESSION_EVENTS:
        raise MorningSessionHotStateError("state sequence is outside the hot-session bound")
    if not isinstance(state.get("items"), dict) or not isinstance(
        state.get("item_order"), list
    ):
        raise MorningSessionHotStateError("state item projection is invalid")
    try:
        bounded._assert_answer_safe(state)
    except bounded.BoundedJsonlIndexError as exc:
        raise MorningSessionHotStateError(str(exc)) from exc
    return sequence


def _legacy_projection_equivalent(
    actual: Any, replayed: Any, trail: tuple[str, ...] = ()
) -> bool:
    """Accept only known neutral fields added by later state-schema code.

    Existing active sessions can have a current sequence but predate optional
    follow-up fields or split choice/reasoning judgments.  Omitting those
    neutral defaults is compatible; any observed value drift remains fatal.
    """

    if isinstance(actual, dict) and isinstance(replayed, dict):
        if any(key not in replayed for key in actual):
            return False
        for key, replayed_value in replayed.items():
            next_trail = trail + (str(key),)
            if key in actual:
                if not _legacy_projection_equivalent(
                    actual[key], replayed_value, next_trail
                ):
                    return False
                continue
            if replayed_value is None or replayed_value == {}:
                continue
            if key == "choice_result" and replayed_value == "not_applicable":
                continue
            if key == "reasoning_result" and replayed_value == "not_observed":
                continue
            return False
        return True
    if isinstance(actual, list) and isinstance(replayed, list):
        return len(actual) == len(replayed) and all(
            _legacy_projection_equivalent(left, right, trail + (str(index),))
            for index, (left, right) in enumerate(zip(actual, replayed))
        )
    return type(actual) is type(replayed) and actual == replayed


def _sequence_value(
    session_id: str, events: list[dict[str, Any]]
) -> dict[str, Any]:
    if len(events) > MAX_SESSION_EVENTS:
        raise MorningSessionHotStateError("session event count exceeds the hot bound")
    entries: dict[str, dict[str, Any]] = {}
    for expected, event in enumerate(events, start=1):
        if event.get("session_id") != session_id or event.get("sequence") != expected:
            raise MorningSessionHotStateError("session event sequence drifted")
        entries[str(expected)] = {
            "sequence": expected,
            "event_id": str(event.get("event_id") or ""),
            "event_sha256": _event_digest(event),
        }
    value = {
        "schema": SEQUENCE_SCHEMA,
        "session_id": session_id,
        "event_count": len(events),
        "entries": entries,
        "answer_safe": True,
        "formal_write_count": 0,
    }
    if len(_pretty_bytes(value)) > MAX_SEQUENCE_INDEX_BYTES:
        raise MorningSessionHotStateError("sequence index exceeds the hot bound")
    return value


def _load_sequence(hot_dir: Path, digest: str, session_id: str) -> dict[str, Any]:
    if not SHA256_RE.fullmatch(str(digest)):
        raise MorningSessionHotStateError("sequence index hash is invalid")
    path = hot_dir / "sequence-objects" / f"{digest}.json"
    value, payload = _read_json(
        path, "sequence index", max_bytes=MAX_SEQUENCE_INDEX_BYTES
    )
    if _sha256_bytes(payload) != digest:
        raise MorningSessionHotStateError("sequence index content drifted")
    if (
        value.get("schema") != SEQUENCE_SCHEMA
        or value.get("session_id") != session_id
        or value.get("answer_safe") is not True
        or value.get("formal_write_count") != 0
        or not isinstance(value.get("entries"), dict)
        or value.get("event_count") != len(value["entries"])
        or len(value["entries"]) > MAX_SESSION_EVENTS
    ):
        raise MorningSessionHotStateError("sequence index is invalid")
    for raw_sequence, row in value["entries"].items():
        if (
            not raw_sequence.isdigit()
            or not isinstance(row, dict)
            or row.get("sequence") != int(raw_sequence)
            or not str(row.get("event_id") or "")
            or not SHA256_RE.fullmatch(str(row.get("event_sha256") or ""))
        ):
            raise MorningSessionHotStateError("sequence index entry is invalid")
    return value


def _write_state_snapshot(hot_dir: Path, state: dict[str, Any]) -> tuple[str, int]:
    _, digest, size = _content_write(hot_dir / "state-objects", state)
    if size > MAX_STATE_BUDGET_BYTES:
        raise MorningSessionHotStateError("state snapshot exceeds 64 KiB")
    return digest, size


def _load_state_snapshot(hot_dir: Path, digest: str) -> dict[str, Any]:
    if not SHA256_RE.fullmatch(str(digest)):
        raise MorningSessionHotStateError("state snapshot hash is invalid")
    path = hot_dir / "state-objects" / f"{digest}.json"
    value, payload = _read_json(
        path, "state snapshot", max_bytes=MAX_STATE_BUDGET_BYTES
    )
    if _sha256_bytes(payload) != digest:
        raise MorningSessionHotStateError("state snapshot content drifted")
    return value


def _index_binding(
    ledger: Path, index_dir: Path, *, allow_tail: bool
) -> tuple[dict[str, Any], str]:
    try:
        return bounded._load_manifest(ledger, index_dir, allow_tail=allow_tail)
    except bounded.BoundedJsonlIndexError as exc:
        raise MorningSessionHotStateError(str(exc)) from exc


def _ledger_binding(
    ledger: Path, index_manifest: dict[str, Any], *, last_sequence: int,
    last_event_id: str, last_event_sha256: str
) -> dict[str, Any]:
    try:
        info = ledger.lstat()
    except OSError as exc:
        raise MorningSessionHotStateError("session ledger is unavailable") from exc
    if stat.S_ISLNK(info.st_mode) or not stat.S_ISREG(info.st_mode):
        raise MorningSessionHotStateError("session ledger must be a regular file")
    indexed = index_manifest["ledger"]
    return {
        "path_sha256": _path_digest(ledger),
        "device": info.st_dev,
        "inode": info.st_ino,
        "size_bytes": info.st_size,
        "mtime_ns": info.st_mtime_ns,
        "root_sha256": indexed["root_sha256"],
        "last_sequence": last_sequence,
        "last_event_id": last_event_id,
        "last_event_sha256": last_event_sha256,
    }


def _hot_manifest_core(
    *,
    session_id: str,
    state_path: Path,
    state: dict[str, Any],
    state_payload: bytes,
    state_snapshot_sha256: str,
    ledger_binding: dict[str, Any],
    index_manifest: dict[str, Any],
    index_manifest_sha256: str,
    sequence_sha256: str,
    sequence_size_bytes: int,
    receipt_sha256: str,
    previous_manifest_sha256: str | None,
    commit_kind: str,
) -> dict[str, Any]:
    core = {
        "schema": HOT_SCHEMA,
        "session_id": session_id,
        "generation_id": "",
        "previous_manifest_sha256": previous_manifest_sha256,
        "commit_kind": commit_kind,
        "state": {
            "path_sha256": _path_digest(state_path),
            "file_sha256": _sha256_bytes(state_payload),
            "logical_sha256": _logical_digest(state),
            "size_bytes": len(state_payload),
            "snapshot_sha256": state_snapshot_sha256,
            "last_sequence": state["last_sequence"],
        },
        "ledger": ledger_binding,
        "index": {
            "manifest_sha256": index_manifest_sha256,
            "generation_id": index_manifest["generation_id"],
            "event_count": index_manifest["event_count"],
            "ledger_size_bytes": index_manifest["ledger"]["size_bytes"],
            "ledger_root_sha256": index_manifest["ledger"]["root_sha256"],
        },
        "sequence_index": {
            "sha256": sequence_sha256,
            "size_bytes": sequence_size_bytes,
            "event_count": index_manifest["event_count"],
        },
        "last_commit_receipt_sha256": receipt_sha256,
        "answer_safe": True,
        "formal_write_count": 0,
    }
    core["generation_id"] = _logical_digest(
        {key: value for key, value in core.items() if key != "generation_id"}
    )
    return core


def _load_hot_manifest(
    hot_dir: Path, session_id: str
) -> tuple[dict[str, Any], str]:
    key = _load_or_create_key(hot_dir, create=False)
    value, payload = _read_json(
        hot_dir / HOT_MANIFEST_FILENAME,
        "hot-state manifest",
        max_bytes=MAX_HOT_MANIFEST_BYTES,
    )
    core = _verify_signature(value, key, "hot-state manifest")
    state = core.get("state")
    ledger = core.get("ledger")
    index = core.get("index")
    sequence = core.get("sequence_index")
    expected_generation = _logical_digest(
        {key: item for key, item in core.items() if key != "generation_id"}
    )
    if (
        core.get("schema") != HOT_SCHEMA
        or core.get("session_id") != session_id
        or core.get("generation_id") != expected_generation
        or core.get("answer_safe") is not True
        or core.get("formal_write_count") != 0
        or not isinstance(state, dict)
        or not isinstance(ledger, dict)
        or not isinstance(index, dict)
        or not isinstance(sequence, dict)
        or not SHA256_RE.fullmatch(str(state.get("file_sha256") or ""))
        or not SHA256_RE.fullmatch(str(state.get("logical_sha256") or ""))
        or not SHA256_RE.fullmatch(str(state.get("snapshot_sha256") or ""))
        or not SHA256_RE.fullmatch(str(ledger.get("root_sha256") or ""))
        or not SHA256_RE.fullmatch(str(ledger.get("last_event_sha256") or ""))
        or not SHA256_RE.fullmatch(str(index.get("manifest_sha256") or ""))
        or not SHA256_RE.fullmatch(str(index.get("ledger_root_sha256") or ""))
        or not SHA256_RE.fullmatch(str(sequence.get("sha256") or ""))
        or not SHA256_RE.fullmatch(str(core.get("last_commit_receipt_sha256") or ""))
    ):
        raise MorningSessionHotStateError("hot-state manifest is invalid")
    return core, _sha256_bytes(payload)


def _pending_exists(hot_dir: Path) -> bool:
    path = hot_dir / PENDING_FILENAME
    try:
        info = path.lstat()
    except FileNotFoundError:
        return False
    except OSError:
        return True
    return True


def _remove_pending(hot_dir: Path) -> None:
    path = hot_dir / PENDING_FILENAME
    try:
        path.unlink()
    except FileNotFoundError:
        return
    _fsync_dir(hot_dir)


def _write_signed_content(
    directory: Path, core: dict[str, Any], key: bytes
) -> tuple[Path, str, int]:
    return _content_write(directory, _sign(core, key))


def _publish_hot_manifest(hot_dir: Path, core: dict[str, Any], key: bytes) -> str:
    return _atomic_write(
        hot_dir / HOT_MANIFEST_FILENAME, _pretty_bytes(_sign(core, key))
    )


def _cold_bootstrap_locked(
    session: Path,
    ledger: Path,
    state_path: Path,
    hot: Path,
    *,
    state_budget_bytes: int,
) -> dict[str, Any]:
    session_id = session.name
    try:
        state, state_payload = _state_payload(
            state_path, state_budget_bytes=state_budget_bytes
        )
    except MorningSessionHotStateError as exc:
        if "bounded size" in str(exc):
            return _failure("exit_hot_path", "state_budget_exceeded", str(exc))
        return _failure("reconcile_required", "state_unavailable", str(exc))
    try:
        sequence = _validate_state(state, session_id)
        events = morning._read_events(ledger, session_id)
        replayed = morning.replay_events(copy.deepcopy(events))
        if not _legacy_projection_equivalent(state, replayed) or len(events) != sequence:
            return _failure(
                "reconcile_required",
                "state_ledger_projection_drift",
                "state.json does not equal a full replay of this session ledger",
                ledger_read_bytes=ledger.stat().st_size,
            )
        bounded._assert_answer_safe(events)
    except (morning.SessionError, bounded.BoundedJsonlIndexError, MorningSessionHotStateError) as exc:
        return _failure(
            "reconcile_required",
            "cold_replay_failed",
            str(exc),
            ledger_read_bytes=ledger.stat().st_size if ledger.exists() else 0,
        )
    _ensure_directory(hot, create=True)
    key = _load_or_create_key(hot, create=True)
    index_dir = hot / "index"
    try:
        bounded.rebuild_index(ledger, index_dir)
        index_manifest, index_sha = _index_binding(
            ledger, index_dir, allow_tail=False
        )
        if int(index_manifest["event_count"]) != sequence:
            raise MorningSessionHotStateError("index event count does not match state")
        sequence_value = _sequence_value(session_id, events)
        _, sequence_sha, sequence_size = _content_write(
            hot / "sequence-objects", sequence_value
        )
        state_snapshot_sha, _ = _write_state_snapshot(hot, state)
        last_event = events[-1]
        ledger_commitment = _ledger_binding(
            ledger,
            index_manifest,
            last_sequence=sequence,
            last_event_id=str(last_event["event_id"]),
            last_event_sha256=_event_digest(last_event),
        )
        bootstrap_core = {
            "schema": BOOTSTRAP_RECEIPT_SCHEMA,
            "kind": "cold_bootstrap_binding",
            "session_id": session_id,
            "state_logical_sha256": _logical_digest(state),
            "state_file_sha256": _sha256_bytes(state_payload),
            "state_snapshot_sha256": state_snapshot_sha,
            "last_sequence": sequence,
            "last_event_id": last_event["event_id"],
            "last_event_sha256": _event_digest(last_event),
            "index_manifest_sha256": index_sha,
            "ledger_root_sha256": index_manifest["ledger"]["root_sha256"],
            "sequence_index_sha256": sequence_sha,
            "answer_safe": True,
            "formal_write_count": 0,
        }
        receipt_path, receipt_sha, _ = _write_signed_content(
            hot / "receipts", bootstrap_core, key
        )
        prior_sha = None
        manifest_path = hot / HOT_MANIFEST_FILENAME
        if manifest_path.is_file() and not manifest_path.is_symlink():
            prior_sha = _sha256_bytes(manifest_path.read_bytes())
        manifest_core = _hot_manifest_core(
            session_id=session_id,
            state_path=state_path,
            state=state,
            state_payload=state_payload,
            state_snapshot_sha256=state_snapshot_sha,
            ledger_binding=ledger_commitment,
            index_manifest=index_manifest,
            index_manifest_sha256=index_sha,
            sequence_sha256=sequence_sha,
            sequence_size_bytes=sequence_size,
            receipt_sha256=receipt_sha,
            previous_manifest_sha256=prior_sha,
            commit_kind="cold_bootstrap",
        )
        manifest_sha = _publish_hot_manifest(hot, manifest_core, key)
        _remove_pending(hot)
    except (OSError, bounded.BoundedJsonlIndexError, MorningSessionHotStateError) as exc:
        return _failure(
            "reconcile_required",
            "cold_index_binding_failed",
            str(exc),
            ledger_read_bytes=ledger.stat().st_size if ledger.exists() else 0,
        )
    return {
        "status": "ready",
        "action": "cold_bootstrap",
        "session_id": session_id,
        "last_sequence": sequence,
        "state_size_bytes": len(state_payload),
        "state_budget_bytes": state_budget_bytes,
        "manifest_sha256": manifest_sha,
        "index_manifest_sha256": index_sha,
        "receipt_path": str(receipt_path),
        "receipt_sha256": receipt_sha,
        "ledger_read_bytes": ledger.stat().st_size,
        "answer_safe": True,
        "formal_write_count": 0,
    }


def cold_bootstrap(
    session_dir: str | Path,
    hot_dir: str | Path | None = None,
    *,
    state_budget_bytes: int = DEFAULT_STATE_BUDGET_BYTES,
    session_lock_held: bool = False,
) -> dict[str, Any]:
    """Cold-bind one existing ledger and state projection.

    This is the only API in this module allowed to scan the complete session
    ledger.  By default it obtains the existing morning session lock itself.
    """

    budget = _validate_budget(state_budget_bytes)
    session, ledger, state_path, hot = _paths(session_dir, hot_dir)
    if session_lock_held:
        return _cold_bootstrap_locked(
            session, ledger, state_path, hot, state_budget_bytes=budget
        )
    with morning.session_lock(session):
        return _cold_bootstrap_locked(
            session, ledger, state_path, hot, state_budget_bytes=budget
        )


def load_state_bounded(
    session_dir: str | Path,
    hot_dir: str | Path | None = None,
    *,
    state_budget_bytes: int = DEFAULT_STATE_BUDGET_BYTES,
) -> dict[str, Any]:
    """Load a verified session state without opening or reading events.jsonl."""

    budget = _validate_budget(state_budget_bytes)
    try:
        session, ledger, state_path, hot = _paths(session_dir, hot_dir)
        session_id = session.name
        if _pending_exists(hot):
            return _failure(
                "reconcile_required",
                "incomplete_hot_transaction",
                "a prior hot transaction did not reach clean completion",
            )
        state, state_payload = _state_payload(state_path, state_budget_bytes=budget)
        sequence = _validate_state(state, session_id)
        manifest, manifest_sha = _load_hot_manifest(hot, session_id)
        index_manifest, index_sha = _index_binding(
            ledger, hot / "index", allow_tail=True
        )
        ledger_info = ledger.lstat()
        state_commitment = manifest["state"]
        ledger_commitment = manifest["ledger"]
        index_commitment = manifest["index"]
        sequence_commitment = manifest["sequence_index"]
        checks = (
            manifest["session_id"] == session_id,
            state_commitment.get("path_sha256") == _path_digest(state_path),
            state_commitment.get("file_sha256") == _sha256_bytes(state_payload),
            state_commitment.get("logical_sha256") == _logical_digest(state),
            state_commitment.get("size_bytes") == len(state_payload),
            state_commitment.get("last_sequence") == sequence,
            ledger_commitment.get("path_sha256") == _path_digest(ledger),
            ledger_commitment.get("device") == ledger_info.st_dev,
            ledger_commitment.get("inode") == ledger_info.st_ino,
            ledger_commitment.get("size_bytes") == ledger_info.st_size,
            ledger_commitment.get("mtime_ns") == ledger_info.st_mtime_ns,
            ledger_commitment.get("last_sequence") == sequence,
            index_commitment.get("manifest_sha256") == index_sha,
            index_commitment.get("generation_id") == index_manifest.get("generation_id"),
            index_commitment.get("event_count") == sequence,
            index_manifest.get("event_count") == sequence,
            index_commitment.get("ledger_size_bytes") == ledger_info.st_size,
            index_manifest["ledger"].get("size_bytes") == ledger_info.st_size,
            index_commitment.get("ledger_root_sha256")
            == index_manifest["ledger"].get("root_sha256"),
            ledger_commitment.get("root_sha256")
            == index_manifest["ledger"].get("root_sha256"),
            sequence_commitment.get("event_count") == sequence,
        )
        if not all(checks):
            return _failure(
                "reconcile_required",
                "hot_commitment_drift",
                "state, index, or ledger tail commitment no longer agrees",
            )
    except MorningSessionHotStateError as exc:
        if "bounded size" in str(exc) and "state" in str(exc):
            return _failure("exit_hot_path", "state_budget_exceeded", str(exc))
        return _failure("reconcile_required", "hot_load_failed", str(exc))
    except OSError as exc:
        return _failure("reconcile_required", "hot_load_failed", str(exc))
    return {
        "status": "ready",
        "session_id": session_id,
        "state": state,
        "last_sequence": sequence,
        "state_size_bytes": len(state_payload),
        "state_budget_bytes": budget,
        "manifest_sha256": manifest_sha,
        "manifest": manifest,
        "index_manifest_sha256": index_sha,
        "index_generation_id": index_manifest["generation_id"],
        "ledger_read_bytes": 0,
        "answer_safe": True,
        "formal_write_count": 0,
    }


def _load_receipt(hot_dir: Path, digest: str, key: bytes) -> dict[str, Any]:
    if not SHA256_RE.fullmatch(str(digest)):
        raise MorningSessionHotStateError("receipt hash is invalid")
    path = hot_dir / "receipts" / f"{digest}.json"
    value, payload = _read_json(path, "hot commit receipt", max_bytes=MAX_RECEIPT_BYTES)
    if _sha256_bytes(payload) != digest:
        raise MorningSessionHotStateError("hot commit receipt content drifted")
    return _verify_signature(value, key, "hot commit receipt")


def _pointer_path(hot_dir: Path, idempotency_key: str) -> Path:
    digest = _sha256_bytes(idempotency_key.encode("utf-8"))
    return hot_dir / "idempotency" / f"{digest}.json"


def _load_pointer(
    hot_dir: Path, idempotency_key: str, key: bytes, *, session_id: str
) -> dict[str, Any] | None:
    path = _pointer_path(hot_dir, idempotency_key)
    if not path.exists():
        return None
    value, _ = _read_json(path, "idempotency pointer", max_bytes=MAX_POINTER_BYTES)
    core = _verify_signature(value, key, "idempotency pointer")
    if (
        core.get("schema") != POINTER_SCHEMA
        or core.get("session_id") != session_id
        or core.get("idempotency_key_sha256")
        != _sha256_bytes(idempotency_key.encode("utf-8"))
        or not str(core.get("event_id") or "")
        or not isinstance(core.get("sequence"), int)
        or not 1 <= int(core["sequence"]) <= MAX_SESSION_EVENTS
        or not SHA256_RE.fullmatch(str(core.get("request_sha256") or ""))
        or not SHA256_RE.fullmatch(str(core.get("receipt_sha256") or ""))
        or core.get("answer_safe") is not True
        or core.get("formal_write_count") != 0
    ):
        raise MorningSessionHotStateError("idempotency pointer is invalid")
    return core


def _assert_pointer_commit_binding(
    pointer: dict[str, Any], verified: dict[str, Any], *, session_id: str
) -> None:
    if (
        verified.get("status") != "pass"
        or pointer.get("session_id") != session_id
        or verified.get("session_id") != session_id
        or pointer.get("event_id") != verified.get("event_id")
        or pointer.get("sequence") != verified.get("sequence")
        or pointer.get("receipt_sha256") != verified.get("receipt_sha256")
        or pointer.get("request_sha256") != verified.get("request_sha256")
    ):
        raise MorningSessionHotStateError(
            "idempotency pointer does not bind the verified session commit"
        )


def _request_digest(
    session_id: str, event_type: str, payload: dict[str, Any], idempotency_key: str
) -> str:
    return _logical_digest(
        {
            "schema": "morning_session_hot_request_v1",
            "session_id": session_id,
            "event_type": event_type,
            "payload": payload,
            "idempotency_key": idempotency_key,
        }
    )


def _enforce_feedback_binding(
    state: dict[str, Any], event_type: str, payload: dict[str, Any]
) -> None:
    if event_type != "feedback_revealed":
        return
    item_id = payload.get("item_id")
    item = state.get("items", {}).get(item_id)
    first = item.get("first") if isinstance(item, dict) else None
    if not isinstance(first, dict) or not first.get("canonical_event_id"):
        raise MorningSessionHotStateError(
            "feedback_revealed requires a first answer already bound to the canonical review ledger"
        )


def _append_fsync(ledger: Path, event: dict[str, Any]) -> tuple[int, int, str]:
    payload = _canonical_bytes(event) + b"\n"
    if len(payload) > bounded.DEFAULT_MAX_EVENT_BYTES:
        raise MorningSessionHotStateError("session event exceeds the 64 KiB event bound")
    offset = ledger.stat().st_size
    descriptor = os.open(ledger, os.O_WRONLY | os.O_APPEND)
    try:
        written = 0
        while written < len(payload):
            written += os.write(descriptor, payload[written:])
        os.fsync(descriptor)
    finally:
        os.close(descriptor)
    return offset, len(payload), _sha256_bytes(payload)


def commit_event(
    session_dir: str | Path,
    state: dict[str, Any],
    event_type: str,
    payload: dict[str, Any],
    *,
    idempotency_key: str,
    session_lock_held: bool = False,
    hot_dir: str | Path | None = None,
    timestamp: str | None = None,
) -> dict[str, Any]:
    """Commit one event while the caller holds the existing session lock.

    The caller-held lock is a trusted local contract because the existing lock
    context manager yields no verifiable token.  A signed pending marker is
    published before the append; any incomplete post-append step therefore
    makes all later hot loads fail closed until an explicit reconciliation.
    """

    if session_lock_held is not True:
        raise MorningSessionHotStateError(
            "commit_event requires the caller to hold morning_review_session.session_lock"
        )
    if not isinstance(state, dict) or not isinstance(payload, dict):
        raise MorningSessionHotStateError("state and payload must be JSON objects")
    if not EVENT_TYPE_RE.fullmatch(str(event_type)):
        raise MorningSessionHotStateError("event_type is invalid")
    if not bounded.SAFE_IDEMPOTENCY_RE.fullmatch(str(idempotency_key)):
        raise MorningSessionHotStateError("idempotency_key is missing or unsafe")
    try:
        bounded._assert_answer_safe(payload)
    except bounded.BoundedJsonlIndexError as exc:
        raise MorningSessionHotStateError(str(exc)) from exc

    session, ledger, state_path, hot = _paths(session_dir, hot_dir)
    loaded = load_state_bounded(session, hot)
    if loaded["status"] != "ready":
        raise MorningSessionHotStateError(
            f"{loaded['status']}:{loaded.get('reason_code', 'hot_state_unavailable')}"
        )
    current = loaded["state"]
    session_id = session.name
    request_sha = _request_digest(session_id, event_type, payload, idempotency_key)
    key = _load_or_create_key(hot, create=False)

    pointer = _load_pointer(
        hot, idempotency_key, key, session_id=session_id
    )
    if pointer is not None:
        if pointer["request_sha256"] != request_sha:
            raise MorningSessionHotStateError(
                "idempotency key was reused for a different hot-state request"
            )
        verified = verify_commit(session, pointer["receipt_sha256"], hot_dir=hot)
        if verified["status"] != "pass":
            raise MorningSessionHotStateError("idempotent receipt did not verify")
        _assert_pointer_commit_binding(
            pointer, verified, session_id=session_id
        )
        return {
            "status": "already_committed",
            "session_id": session_id,
            "event_id": verified["event_id"],
            "sequence": verified["sequence"],
            "receipt_sha256": pointer["receipt_sha256"],
            "state": current,
            "answer_safe": True,
            "formal_write_count": 0,
        }

    if _logical_digest(state) != _logical_digest(current):
        raise MorningSessionHotStateError("caller state is stale or drifted")
    _enforce_feedback_binding(current, event_type, payload)

    try:
        prior_by_idem = bounded.lookup_idempotency(
            ledger, hot / "index", idempotency_key
        )
    except bounded.BoundedJsonlIndexError as exc:
        raise MorningSessionHotStateError(str(exc)) from exc
    if prior_by_idem is not None:
        raise MorningSessionHotStateError(
            "reconcile_required:idempotency event exists without a complete hot receipt"
        )

    sequence = int(current["last_sequence"]) + 1
    if sequence > MAX_SESSION_EVENTS:
        raise MorningSessionHotStateError("session event count exceeds the hot bound")
    event_id = "HSE-" + _logical_digest(
        {
            "session_id": session_id,
            "idempotency_key": idempotency_key,
            "request_sha256": request_sha,
        }
    )[:32]
    event = {
        "schema": EVENT_SCHEMA,
        "event_id": event_id,
        "session_id": session_id,
        "sequence": sequence,
        "timestamp": str(timestamp or morning.utc_now()),
        "event_type": event_type,
        "payload": copy.deepcopy(payload),
        "idempotency_key": idempotency_key,
        "hot_request_sha256": request_sha,
    }
    try:
        bounded._assert_answer_safe(event)
        next_state = morning._apply_event(copy.deepcopy(current), event)
    except (
        bounded.BoundedJsonlIndexError,
        morning.SessionError,
        morning.ReviewLoopError,
        KeyError,
        TypeError,
    ) as exc:
        raise MorningSessionHotStateError(str(exc)) from exc
    next_state_payload = _pretty_bytes(next_state)
    if len(next_state_payload) > MAX_STATE_BUDGET_BYTES:
        raise MorningSessionHotStateError("next state exceeds 64 KiB; exit hot path")

    manifest = loaded["manifest"]
    sequence_value = _load_sequence(
        hot, manifest["sequence_index"]["sha256"], session_id
    )
    if sequence_value["event_count"] != current["last_sequence"]:
        raise MorningSessionHotStateError("sequence index is stale before commit")
    pending_core = {
        "schema": PENDING_SCHEMA,
        "session_id": session_id,
        "request_sha256": request_sha,
        "idempotency_key_sha256": _sha256_bytes(idempotency_key.encode("utf-8")),
        "expected_sequence": sequence,
        "prior_manifest_sha256": loaded["manifest_sha256"],
        "answer_safe": True,
        "formal_write_count": 0,
    }
    _atomic_write(hot / PENDING_FILENAME, _pretty_bytes(_sign(pending_core, key)))

    try:
        with bounded.locked_writer(ledger, hot / "index") as writer:
            before_index, before_index_sha = _index_binding(
                ledger, hot / "index", allow_tail=False
            )
            if before_index_sha != loaded["index_manifest_sha256"]:
                raise MorningSessionHotStateError("index changed before append")
            if before_index["event_count"] != current["last_sequence"]:
                raise MorningSessionHotStateError("index count changed before append")
            offset, line_length, line_sha = _append_fsync(ledger, event)
            _atomic_write(state_path, next_state_payload)
            index_result = writer.recover_tail()
            after_index, after_index_sha = _index_binding(
                ledger, hot / "index", allow_tail=False
            )
            found = bounded.lookup_event(ledger, hot / "index", event_id)
            if found is None:
                raise MorningSessionHotStateError("new event is absent from exact index")
            record = found["record"]
            if (
                record.get("byte_offset") != offset
                or record.get("byte_length") != line_length
                or record.get("line_sha256") != line_sha
                or found["event"] != event
                or after_index.get("event_count") != sequence
            ):
                raise MorningSessionHotStateError("new event index commitment drifted")

            next_sequence = copy.deepcopy(sequence_value)
            next_sequence["entries"][str(sequence)] = {
                "sequence": sequence,
                "event_id": event_id,
                "event_sha256": record["event_sha256"],
            }
            next_sequence["event_count"] = sequence
            if len(_pretty_bytes(next_sequence)) > MAX_SEQUENCE_INDEX_BYTES:
                raise MorningSessionHotStateError("sequence index exceeds the hot bound")
            _, sequence_sha, sequence_size = _content_write(
                hot / "sequence-objects", next_sequence
            )
            next_snapshot_sha, _ = _write_state_snapshot(hot, next_state)
            prior_state_commitment = manifest["state"]
            ledger_commitment = _ledger_binding(
                ledger,
                after_index,
                last_sequence=sequence,
                last_event_id=event_id,
                last_event_sha256=record["event_sha256"],
            )
            receipt_core = {
                "schema": RECEIPT_SCHEMA,
                "kind": "session_event_commit",
                "session_id": session_id,
                "request_sha256": request_sha,
                "idempotency_key_sha256": _sha256_bytes(
                    idempotency_key.encode("utf-8")
                ),
                "event": {
                    "event_id": event_id,
                    "event_type": event_type,
                    "sequence": sequence,
                    "event_sha256": record["event_sha256"],
                    "line_sha256": line_sha,
                    "byte_offset": offset,
                    "byte_length": line_length,
                },
                "prior_state": {
                    "logical_sha256": prior_state_commitment["logical_sha256"],
                    "snapshot_sha256": prior_state_commitment["snapshot_sha256"],
                    "last_sequence": current["last_sequence"],
                },
                "next_state": {
                    "logical_sha256": _logical_digest(next_state),
                    "file_sha256": _sha256_bytes(next_state_payload),
                    "snapshot_sha256": next_snapshot_sha,
                    "last_sequence": sequence,
                },
                "index_manifest_sha256": after_index_sha,
                "bounded_index_receipt_sha256": index_result.get("receipt_sha256"),
                "ledger_root_sha256": after_index["ledger"]["root_sha256"],
                "sequence_index_sha256": sequence_sha,
                "answer_safe": True,
                "formal_write_count": 0,
            }
            receipt_path, receipt_sha, _ = _write_signed_content(
                hot / "receipts", receipt_core, key
            )
            pointer_core = {
                "schema": POINTER_SCHEMA,
                "session_id": session_id,
                "idempotency_key_sha256": _sha256_bytes(
                    idempotency_key.encode("utf-8")
                ),
                "request_sha256": request_sha,
                "event_id": event_id,
                "sequence": sequence,
                "receipt_sha256": receipt_sha,
                "answer_safe": True,
                "formal_write_count": 0,
            }
            _atomic_write(
                _pointer_path(hot, idempotency_key),
                _pretty_bytes(_sign(pointer_core, key)),
            )
            hot_core = _hot_manifest_core(
                session_id=session_id,
                state_path=state_path,
                state=next_state,
                state_payload=next_state_payload,
                state_snapshot_sha256=next_snapshot_sha,
                ledger_binding=ledger_commitment,
                index_manifest=after_index,
                index_manifest_sha256=after_index_sha,
                sequence_sha256=sequence_sha,
                sequence_size_bytes=sequence_size,
                receipt_sha256=receipt_sha,
                previous_manifest_sha256=loaded["manifest_sha256"],
                commit_kind="session_event_commit",
            )
            hot_manifest_sha = _publish_hot_manifest(hot, hot_core, key)
        _remove_pending(hot)
    except BaseException:
        # The durable pending marker is deliberately retained.  The next read
        # must fail closed even when the ledger append itself completed.
        raise

    return {
        "status": "committed",
        "session_id": session_id,
        "event_id": event_id,
        "sequence": sequence,
        "event_sha256": record["event_sha256"],
        "state": next_state,
        "receipt_path": str(receipt_path),
        "receipt_sha256": receipt_sha,
        "manifest_sha256": hot_manifest_sha,
        "index_manifest_sha256": after_index_sha,
        "answer_safe": True,
        "formal_write_count": 0,
    }


def lookup_session_event(
    session_dir: str | Path,
    event_id: str,
    *,
    hot_dir: str | Path | None = None,
) -> dict[str, Any] | None:
    """Perform one exact, bounded event lookup after verifying hot state."""

    session, ledger, _, hot = _paths(session_dir, hot_dir)
    loaded = load_state_bounded(session, hot)
    if loaded["status"] != "ready":
        raise MorningSessionHotStateError(
            f"{loaded['status']}:{loaded.get('reason_code', 'hot_state_unavailable')}"
        )
    try:
        found = bounded.lookup_event(ledger, hot / "index", str(event_id))
    except bounded.BoundedJsonlIndexError as exc:
        raise MorningSessionHotStateError(str(exc)) from exc
    if found is None:
        return None
    event = found["event"]
    if (
        event.get("schema") != EVENT_SCHEMA
        or event.get("session_id") != session.name
        or not isinstance(event.get("sequence"), int)
        or event["sequence"] > loaded["last_sequence"]
    ):
        raise MorningSessionHotStateError("exact event does not belong to this session")
    return found


def lookup_sequence(
    session_dir: str | Path,
    sequence: int,
    *,
    hot_dir: str | Path | None = None,
) -> dict[str, Any] | None:
    """Resolve one session sequence through the bounded sequence projection."""

    if not isinstance(sequence, int) or sequence <= 0 or sequence > MAX_SESSION_EVENTS:
        raise MorningSessionHotStateError("sequence is outside the hot-session bound")
    session, _, _, hot = _paths(session_dir, hot_dir)
    loaded = load_state_bounded(session, hot)
    if loaded["status"] != "ready":
        raise MorningSessionHotStateError(
            f"{loaded['status']}:{loaded.get('reason_code', 'hot_state_unavailable')}"
        )
    projection = _load_sequence(
        hot, loaded["manifest"]["sequence_index"]["sha256"], session.name
    )
    row = projection["entries"].get(str(sequence))
    if row is None:
        return None
    found = lookup_session_event(session, str(row["event_id"]), hot_dir=hot)
    if (
        found is None
        or found["event"].get("sequence") != sequence
        or found["record"].get("event_sha256") != row["event_sha256"]
    ):
        raise MorningSessionHotStateError("sequence and exact event index drifted")
    return found


def verify_commit(
    session_dir: str | Path,
    receipt_sha256: str,
    *,
    hot_dir: str | Path | None = None,
) -> dict[str, Any]:
    """Boundedly verify one event/state commit receipt without ledger replay."""

    session, ledger, _, hot = _paths(session_dir, hot_dir)
    loaded = load_state_bounded(session, hot)
    if loaded["status"] != "ready":
        return _failure(
            "reconcile_required",
            loaded.get("reason_code", "hot_state_unavailable"),
            loaded.get("detail", "hot state is unavailable"),
        )
    try:
        key = _load_or_create_key(hot, create=False)
        receipt = _load_receipt(hot, receipt_sha256, key)
        event_commitment = receipt.get("event")
        prior_commitment = receipt.get("prior_state")
        next_commitment = receipt.get("next_state")
        if (
            receipt.get("schema") != RECEIPT_SCHEMA
            or receipt.get("kind") != "session_event_commit"
            or receipt.get("session_id") != session.name
            or receipt.get("answer_safe") is not True
            or receipt.get("formal_write_count") != 0
            or not isinstance(event_commitment, dict)
            or not isinstance(prior_commitment, dict)
            or not isinstance(next_commitment, dict)
        ):
            raise MorningSessionHotStateError("hot commit receipt is invalid")
        found = bounded.lookup_event(
            ledger, hot / "index", str(event_commitment.get("event_id") or "")
        )
        if found is None:
            raise MorningSessionHotStateError("receipt event is missing")
        event = found["event"]
        record = found["record"]
        if (
            event.get("session_id") != session.name
            or event.get("sequence") != event_commitment.get("sequence")
            or event.get("event_type") != event_commitment.get("event_type")
            or record.get("event_sha256") != event_commitment.get("event_sha256")
            or record.get("line_sha256") != event_commitment.get("line_sha256")
            or record.get("byte_offset") != event_commitment.get("byte_offset")
            or record.get("byte_length") != event_commitment.get("byte_length")
            or _request_digest(
                session.name,
                str(event.get("event_type") or ""),
                event.get("payload") if isinstance(event.get("payload"), dict) else {},
                str(event.get("idempotency_key") or ""),
            )
            != receipt.get("request_sha256")
        ):
            raise MorningSessionHotStateError("receipt event commitment drifted")
        prior = _load_state_snapshot(
            hot, str(prior_commitment.get("snapshot_sha256") or "")
        )
        expected_next = morning._apply_event(copy.deepcopy(prior), event)
        recorded_next = _load_state_snapshot(
            hot, str(next_commitment.get("snapshot_sha256") or "")
        )
        if (
            _logical_digest(prior) != prior_commitment.get("logical_sha256")
            or prior.get("last_sequence") != prior_commitment.get("last_sequence")
            or expected_next != recorded_next
            or _logical_digest(recorded_next) != next_commitment.get("logical_sha256")
            or recorded_next.get("last_sequence") != next_commitment.get("last_sequence")
            or next_commitment.get("last_sequence") > loaded["last_sequence"]
        ):
            raise MorningSessionHotStateError("receipt state transition drifted")
        by_sequence = lookup_sequence(
            session, int(event_commitment["sequence"]), hot_dir=hot
        )
        if by_sequence is None or by_sequence["event"] != event:
            raise MorningSessionHotStateError("receipt sequence binding drifted")
    except (
        MorningSessionHotStateError,
        bounded.BoundedJsonlIndexError,
        morning.SessionError,
        morning.ReviewLoopError,
    ) as exc:
        return _failure("reconcile_required", "commit_verification_failed", str(exc))
    return {
        "status": "pass",
        "session_id": session.name,
        "event_id": event_commitment["event_id"],
        "sequence": event_commitment["sequence"],
        "request_sha256": receipt["request_sha256"],
        "receipt_sha256": receipt_sha256,
        "verified_ledger_bytes_upper_bound": (
            int(record["byte_length"]) + 2 * bounded.DEFAULT_CHUNK_SIZE
        ),
        "full_ledger_replay_count": 0,
        "answer_safe": True,
        "formal_write_count": 0,
    }


def verify_idempotency(
    session_dir: str | Path,
    idempotency_key: str,
    *,
    hot_dir: str | Path | None = None,
) -> dict[str, Any]:
    """Resolve and verify one committed hot transaction by idempotency key."""

    if not bounded.SAFE_IDEMPOTENCY_RE.fullmatch(str(idempotency_key)):
        raise MorningSessionHotStateError("idempotency_key is missing or unsafe")
    session, _, _, hot = _paths(session_dir, hot_dir)
    loaded = load_state_bounded(session, hot)
    if loaded["status"] != "ready":
        return _failure(
            "reconcile_required",
            loaded.get("reason_code", "hot_state_unavailable"),
            loaded.get("detail", "hot state is unavailable"),
        )
    try:
        key = _load_or_create_key(hot, create=False)
        pointer = _load_pointer(
            hot, idempotency_key, key, session_id=session.name
        )
        if pointer is None:
            return {
                "status": "not_found",
                "session_id": session.name,
                "idempotency_key_sha256": _sha256_bytes(
                    idempotency_key.encode("utf-8")
                ),
                "ledger_read_bytes": 0,
                "answer_safe": True,
                "formal_write_count": 0,
            }
        verified = verify_commit(
            session, str(pointer["receipt_sha256"]), hot_dir=hot
        )
        if verified.get("status") != "pass":
            return verified
        _assert_pointer_commit_binding(
            pointer, verified, session_id=session.name
        )
    except MorningSessionHotStateError as exc:
        return _failure(
            "reconcile_required", "idempotency_verification_failed", str(exc)
        )
    return {
        "status": "pass",
        "session_id": session.name,
        "event_id": pointer["event_id"],
        "sequence": pointer["sequence"],
        "request_sha256": pointer["request_sha256"],
        "receipt_sha256": pointer["receipt_sha256"],
        "verification": verified,
        "answer_safe": True,
        "formal_write_count": 0,
    }
