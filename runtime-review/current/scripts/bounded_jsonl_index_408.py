#!/usr/bin/env python3
"""Content-addressed exact index and commit receipts for append-only JSONL.

The JSONL ledger remains the truth.  This module stores only answer-safe event
identity, byte ranges, hashes, idempotency commitments, chunk commitments, and
content-addressed commit receipts.  It is intentionally independent of the 408
review, morning, and capture writers so they can adopt the lock-held API later.
"""

from __future__ import annotations

import argparse
import fcntl
import hashlib
import hmac
import json
import os
import re
import stat
import sys
import tempfile
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable, Iterator


MANIFEST_SCHEMA = "bounded_jsonl_index_v1"
SHARD_SCHEMA = "bounded_jsonl_exact_shard_v1"
COMMIT_RECEIPT_SCHEMA = "bounded_jsonl_commit_receipt_v1"
AUDIT_SCHEMA = "bounded_jsonl_index_audit_v1"
DEFAULT_CHUNK_SIZE = 1024 * 1024
DEFAULT_MAX_EVENT_BYTES = 64 * 1024
DEFAULT_MAX_TAIL_BYTES = 1024 * 1024
DEFAULT_MAX_TAIL_EVENTS = 256
MAX_INDEXED_EVENTS = 1_000_000
MAX_MANIFEST_BYTES = 8 * 1024 * 1024
MAX_SHARD_BYTES = 2 * 1024 * 1024
SHARD_PREFIX_LENGTH = 4
SIGNING_KEY_FILENAME = ".bounded-jsonl-index.key"
MANIFEST_FILENAME = "manifest.json"
LOCK_FILENAME = ".bounded-jsonl-index.lock"
SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
SAFE_EVENT_ID_RE = re.compile(r"^[^\x00-\x1f\x7f]{1,200}$")
SAFE_IDEMPOTENCY_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{2,200}$")
FORBIDDEN_KEYS = {
    "answer",
    "answers",
    "correct_answer",
    "correct_option",
    "full_question",
    "full_response",
    "options",
    "question_stem",
    "question_text",
    "solution",
    "standard_answer",
    "完整作答",
    "完整解析",
    "答案",
    "解析",
    "选项",
    "题干",
}


class BoundedJsonlIndexError(RuntimeError):
    """A stable fail-closed index or ledger error."""


def _canonical_bytes(value: Any) -> bytes:
    return (
        json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        + "\n"
    ).encode("utf-8")


def _pretty_bytes(value: Any) -> bytes:
    return (
        json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2) + "\n"
    ).encode("utf-8")


def _sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _digest(value: Any) -> str:
    return _sha256_bytes(_canonical_bytes(value))


def _inside(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
        return True
    except ValueError:
        return False


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
        directory_fd = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)
    return _sha256_bytes(payload)


def _content_write(directory: Path, value: dict[str, Any]) -> tuple[Path, str]:
    payload = _pretty_bytes(value)
    digest = _sha256_bytes(payload)
    path = directory / f"{digest}.json"
    if path.exists():
        if path.is_symlink() or path.read_bytes() != payload:
            raise BoundedJsonlIndexError("content-addressed object collision")
        return path, digest
    _atomic_write(path, payload)
    return path, digest


def _read_object(path: Path, label: str, *, max_bytes: int) -> dict[str, Any]:
    try:
        info = path.lstat()
        if stat.S_ISLNK(info.st_mode) or not stat.S_ISREG(info.st_mode):
            raise BoundedJsonlIndexError(f"{label} must be a regular non-symlink file")
        if info.st_size > max_bytes:
            raise BoundedJsonlIndexError(f"{label} exceeds its bounded size")
        value = json.loads(path.read_text(encoding="utf-8"))
    except BoundedJsonlIndexError:
        raise
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise BoundedJsonlIndexError(f"{label} is unreadable") from exc
    if not isinstance(value, dict):
        raise BoundedJsonlIndexError(f"{label} must be a JSON object")
    return value


def _load_or_create_key(index_dir: Path, *, create: bool) -> bytes | None:
    path = index_dir / SIGNING_KEY_FILENAME
    if not path.exists():
        if not create:
            return None
        index_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        try:
            os.write(descriptor, os.urandom(32))
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
    info = path.lstat()
    if stat.S_ISLNK(info.st_mode) or not stat.S_ISREG(info.st_mode) or info.st_mode & 0o077:
        raise BoundedJsonlIndexError("index signing key permissions are unsafe")
    key = path.read_bytes()
    if len(key) != 32:
        raise BoundedJsonlIndexError("index signing key is invalid")
    return key


def _sign(value: dict[str, Any], key: bytes) -> dict[str, Any]:
    core = dict(value)
    core.pop("hmac_sha256", None)
    return {
        **core,
        "hmac_sha256": hmac.new(key, _canonical_bytes(core), hashlib.sha256).hexdigest(),
    }


def _verify_signature(value: dict[str, Any], key: bytes, label: str) -> dict[str, Any]:
    signature = str(value.get("hmac_sha256") or "")
    core = dict(value)
    core.pop("hmac_sha256", None)
    expected = hmac.new(key, _canonical_bytes(core), hashlib.sha256).hexdigest()
    if not SHA256_RE.fullmatch(signature) or not hmac.compare_digest(signature, expected):
        raise BoundedJsonlIndexError(f"{label} signature drifted")
    return core


def _assert_answer_safe(value: Any, trail: tuple[str, ...] = ()) -> None:
    if isinstance(value, dict):
        for key, child in value.items():
            lowered = str(key).lower()
            if lowered in FORBIDDEN_KEYS:
                location = ".".join(trail + (str(key),))
                raise BoundedJsonlIndexError(
                    f"protected question or answer field is forbidden at {location}"
                )
            _assert_answer_safe(child, trail + (str(key),))
    elif isinstance(value, list):
        for index, child in enumerate(value):
            _assert_answer_safe(child, trail + (str(index),))


def _event_identity(
    event: dict[str, Any], *, event_id_field: str, idempotency_field: str
) -> tuple[str, str | None, str]:
    _assert_answer_safe(event)
    event_id = str(event.get(event_id_field) or "")
    if not SAFE_EVENT_ID_RE.fullmatch(event_id):
        raise BoundedJsonlIndexError("event_id is missing or unsafe")
    idempotency_raw = event.get(idempotency_field)
    idempotency_key: str | None = None
    if idempotency_raw is not None:
        idempotency_key = str(idempotency_raw)
        if not SAFE_IDEMPOTENCY_RE.fullmatch(idempotency_key):
            raise BoundedJsonlIndexError("idempotency key is unsafe")
    return event_id, idempotency_key, _digest(event)


def _parse_line(
    line: bytes,
    *,
    event_id_field: str,
    idempotency_field: str,
    max_event_bytes: int,
) -> tuple[dict[str, Any], str, str | None, str]:
    if not line.endswith(b"\n"):
        raise BoundedJsonlIndexError("JSONL event is not newline terminated")
    if len(line) > max_event_bytes:
        raise BoundedJsonlIndexError("JSONL event exceeds max_event_bytes")
    try:
        event = json.loads(line)
    except (UnicodeError, json.JSONDecodeError) as exc:
        raise BoundedJsonlIndexError("JSONL event is invalid") from exc
    if not isinstance(event, dict):
        raise BoundedJsonlIndexError("JSONL event must be an object")
    event_id, idempotency_key, event_sha = _event_identity(
        event,
        event_id_field=event_id_field,
        idempotency_field=idempotency_field,
    )
    return event, event_id, idempotency_key, event_sha


def _ledger_stat(path: Path) -> os.stat_result:
    try:
        info = path.lstat()
    except OSError as exc:
        raise BoundedJsonlIndexError("ledger is unavailable") from exc
    if stat.S_ISLNK(info.st_mode) or not stat.S_ISREG(info.st_mode):
        raise BoundedJsonlIndexError("ledger must be a regular non-symlink file")
    return info


def _chunk_hashes(path: Path, chunk_size: int) -> list[str]:
    hashes: list[str] = []
    with path.open("rb") as handle:
        while True:
            chunk = handle.read(chunk_size)
            if not chunk:
                break
            hashes.append(_sha256_bytes(chunk))
    return hashes


def _ledger_root(size: int, chunk_size: int, chunk_hashes: list[str]) -> str:
    return _digest(
        {
            "schema": "bounded_jsonl_chunk_commitment_v1",
            "size_bytes": size,
            "chunk_size": chunk_size,
            "chunk_hashes": chunk_hashes,
        }
    )


def _shard_prefix(identifier: str) -> str:
    return _sha256_bytes(identifier.encode("utf-8"))[:SHARD_PREFIX_LENGTH]


def _empty_shard(kind: str, prefix: str) -> dict[str, Any]:
    return {
        "schema": SHARD_SCHEMA,
        "kind": kind,
        "prefix": prefix,
        "entries": {},
        "answer_safe": True,
        "formal_write_count": 0,
    }


def _load_shard(
    index_dir: Path,
    mapping: dict[str, str],
    *,
    prefix: str,
    kind: str,
) -> dict[str, Any]:
    digest = mapping.get(prefix)
    if digest is None:
        return _empty_shard(kind, prefix)
    if not SHA256_RE.fullmatch(str(digest)):
        raise BoundedJsonlIndexError("index shard hash is invalid")
    path = index_dir / "objects" / f"{digest}.json"
    if not path.is_file() or path.is_symlink() or _sha256_file(path) != digest:
        raise BoundedJsonlIndexError("index shard drifted")
    shard = _read_object(path, "index shard", max_bytes=MAX_SHARD_BYTES)
    if (
        shard.get("schema") != SHARD_SCHEMA
        or shard.get("kind") != kind
        or shard.get("prefix") != prefix
        or not isinstance(shard.get("entries"), dict)
        or shard.get("answer_safe") is not True
        or shard.get("formal_write_count") != 0
    ):
        raise BoundedJsonlIndexError("index shard is invalid")
    return shard


def _store_shard(index_dir: Path, shard: dict[str, Any]) -> str:
    payload = _pretty_bytes(shard)
    if len(payload) > MAX_SHARD_BYTES:
        raise BoundedJsonlIndexError("index shard exceeds bounded size")
    _, digest = _content_write(index_dir / "objects", shard)
    return digest


def _manifest_path(index_dir: Path) -> Path:
    return index_dir / MANIFEST_FILENAME


def _load_manifest(
    ledger_path: Path,
    index_dir: Path,
    *,
    allow_tail: bool,
    _allow_device_change: bool = False,
) -> tuple[dict[str, Any], str]:
    key = _load_or_create_key(index_dir, create=False)
    path = _manifest_path(index_dir)
    if key is None or not path.is_file() or path.is_symlink():
        raise BoundedJsonlIndexError("index manifest is missing or incomplete")
    manifest = _read_object(path, "index manifest", max_bytes=MAX_MANIFEST_BYTES)
    core = _verify_signature(manifest, key, "index manifest")
    ledger = core.get("ledger")
    if (
        core.get("schema") != MANIFEST_SCHEMA
        or core.get("answer_safe") is not True
        or core.get("formal_write_count") != 0
        or not isinstance(ledger, dict)
        or not isinstance(core.get("event_shards"), dict)
        or not isinstance(core.get("idempotency_shards"), dict)
        or not isinstance(ledger.get("chunk_hashes"), list)
        or ledger.get("path_sha256")
        != _sha256_bytes(str(ledger_path.resolve()).encode("utf-8"))
    ):
        raise BoundedJsonlIndexError("index manifest is invalid")
    try:
        committed_size = int(ledger["size_bytes"])
        committed_mtime = int(ledger["mtime_ns"])
        committed_device = int(ledger["device"])
        committed_inode = int(ledger["inode"])
        chunk_size = int(ledger["chunk_size"])
        event_count = int(core["event_count"])
        idempotency_count = int(core["idempotency_count"])
    except (KeyError, TypeError, ValueError) as exc:
        raise BoundedJsonlIndexError("index manifest ledger commitment is invalid") from exc
    if (
        committed_size < 0
        or event_count < 0
        or idempotency_count < 0
        or idempotency_count > event_count
        or event_count > MAX_INDEXED_EVENTS
        or chunk_size <= 0
        or chunk_size > DEFAULT_CHUNK_SIZE
        or ledger.get("root_sha256")
        != _ledger_root(committed_size, chunk_size, ledger["chunk_hashes"])
        or any(not SHA256_RE.fullmatch(str(item)) for item in ledger["chunk_hashes"])
        or len(ledger["chunk_hashes"])
        != ((committed_size + chunk_size - 1) // chunk_size if committed_size else 0)
        or any(
            not isinstance(prefix, str)
            or len(prefix) != SHARD_PREFIX_LENGTH
            or not re.fullmatch(r"[0-9a-f]+", prefix)
            or not SHA256_RE.fullmatch(str(digest))
            for mapping in (core["event_shards"], core["idempotency_shards"])
            for prefix, digest in mapping.items()
        )
    ):
        raise BoundedJsonlIndexError("index manifest ledger commitment is invalid")
    info = _ledger_stat(ledger_path)
    if info.st_ino != committed_inode or (
        info.st_dev != committed_device and not _allow_device_change
    ):
        raise BoundedJsonlIndexError("ledger identity drifted")
    if info.st_size < committed_size:
        raise BoundedJsonlIndexError("ledger was truncated below committed size")
    if info.st_size == committed_size:
        if info.st_mtime_ns != committed_mtime:
            raise BoundedJsonlIndexError("ledger mtime drifted without an indexed append")
    elif not allow_tail:
        raise BoundedJsonlIndexError("ledger has an unindexed tail")
    return core, _sha256_file(path)


def _read_record_bytes(ledger_path: Path, record: dict[str, Any]) -> bytes:
    try:
        offset = int(record["byte_offset"])
        length = int(record["byte_length"])
    except (KeyError, TypeError, ValueError) as exc:
        raise BoundedJsonlIndexError("index record byte range is invalid") from exc
    if offset < 0 or length <= 0 or length > DEFAULT_MAX_EVENT_BYTES:
        raise BoundedJsonlIndexError("index record byte range is invalid")
    with ledger_path.open("rb") as handle:
        handle.seek(offset)
        value = handle.read(length)
    if len(value) != length or _sha256_bytes(value) != record.get("line_sha256"):
        raise BoundedJsonlIndexError("indexed ledger event drifted")
    return value


def _verify_record_chunks(
    ledger_path: Path, manifest: dict[str, Any], record: dict[str, Any]
) -> None:
    ledger = manifest["ledger"]
    chunk_size = int(ledger["chunk_size"])
    offset = int(record["byte_offset"])
    length = int(record["byte_length"])
    first = offset // chunk_size
    last = (offset + length - 1) // chunk_size
    with ledger_path.open("rb") as handle:
        for index in range(first, last + 1):
            handle.seek(index * chunk_size)
            chunk = handle.read(chunk_size)
            try:
                expected = ledger["chunk_hashes"][index]
            except IndexError as exc:
                raise BoundedJsonlIndexError("indexed chunk is missing") from exc
            if _sha256_bytes(chunk) != expected:
                raise BoundedJsonlIndexError("ledger chunk commitment drifted")


def _record_for_event(
    ledger_path: Path, index_dir: Path, manifest: dict[str, Any], event_id: str
) -> dict[str, Any] | None:
    prefix = _shard_prefix(event_id)
    shard = _load_shard(
        index_dir,
        manifest["event_shards"],
        prefix=prefix,
        kind="event_id",
    )
    record = shard["entries"].get(event_id)
    return record if isinstance(record, dict) else None


def _record_for_idempotency(
    index_dir: Path, manifest: dict[str, Any], key: str
) -> dict[str, Any] | None:
    if not SAFE_IDEMPOTENCY_RE.fullmatch(key):
        raise BoundedJsonlIndexError("idempotency key is unsafe")
    digest = _sha256_bytes(key.encode("utf-8"))
    prefix = digest[:SHARD_PREFIX_LENGTH]
    shard = _load_shard(
        index_dir,
        manifest["idempotency_shards"],
        prefix=prefix,
        kind="idempotency_sha256",
    )
    record = shard["entries"].get(digest)
    return record if isinstance(record, dict) else None


def lookup_event(
    ledger_path: str | Path, index_dir: str | Path, event_id: str
) -> dict[str, Any] | None:
    ledger = Path(ledger_path).expanduser().resolve()
    index = Path(index_dir).expanduser().resolve()
    manifest, manifest_sha = _load_manifest(ledger, index, allow_tail=False)
    record = _record_for_event(ledger, index, manifest, str(event_id))
    if record is None:
        return None
    _verify_record_chunks(ledger, manifest, record)
    line = _read_record_bytes(ledger, record)
    event, actual_id, _, event_sha = _parse_line(
        line,
        event_id_field=manifest["event_id_field"],
        idempotency_field=manifest["idempotency_field"],
        max_event_bytes=int(manifest["max_event_bytes"]),
    )
    if actual_id != event_id or event_sha != record.get("event_sha256"):
        raise BoundedJsonlIndexError("indexed event identity drifted")
    return {
        "status": "found",
        "event": event,
        "record": record,
        "manifest_sha256": manifest_sha,
    }


def lookup_idempotency(
    ledger_path: str | Path, index_dir: str | Path, idempotency_key: str
) -> dict[str, Any] | None:
    ledger = Path(ledger_path).expanduser().resolve()
    index = Path(index_dir).expanduser().resolve()
    manifest, _ = _load_manifest(ledger, index, allow_tail=False)
    record = _record_for_idempotency(index, manifest, idempotency_key)
    if record is None:
        return None
    found = lookup_event(ledger, index, str(record.get("event_id") or ""))
    if found is None or found["record"].get("event_sha256") != record.get(
        "event_sha256"
    ):
        raise BoundedJsonlIndexError("idempotency record commitment drifted")
    return found


def _build_records(
    ledger_path: Path,
    *,
    event_id_field: str,
    idempotency_field: str,
    max_event_bytes: int,
) -> tuple[dict[str, dict[str, Any]], dict[str, dict[str, Any]], int]:
    events: dict[str, dict[str, Any]] = {}
    idempotency: dict[str, dict[str, Any]] = {}
    offset = 0
    with ledger_path.open("rb") as handle:
        for line_number, line in enumerate(handle, start=1):
            event, event_id, idem_key, event_sha = _parse_line(
                line,
                event_id_field=event_id_field,
                idempotency_field=idempotency_field,
                max_event_bytes=max_event_bytes,
            )
            if event_id in events:
                raise BoundedJsonlIndexError(f"duplicate event_id at line {line_number}")
            record = {
                "event_id": event_id,
                "event_sha256": event_sha,
                "line_sha256": _sha256_bytes(line),
                "byte_offset": offset,
                "byte_length": len(line),
                "idempotency_key_sha256": (
                    _sha256_bytes(idem_key.encode("utf-8")) if idem_key else None
                ),
            }
            events[event_id] = record
            if idem_key:
                idem_digest = _sha256_bytes(idem_key.encode("utf-8"))
                existing = idempotency.get(idem_digest)
                if existing and existing["event_sha256"] != event_sha:
                    raise BoundedJsonlIndexError(
                        f"idempotency conflict at line {line_number}"
                    )
                idempotency[idem_digest] = {
                    "event_id": event_id,
                    "event_sha256": event_sha,
                }
            offset += len(line)
    return events, idempotency, offset


def _store_all_shards(
    index_dir: Path, records: dict[str, dict[str, Any]], *, kind: str
) -> dict[str, str]:
    buckets: dict[str, dict[str, Any]] = {}
    for identifier, record in records.items():
        prefix = (
            identifier[:SHARD_PREFIX_LENGTH]
            if kind == "idempotency_sha256"
            else _shard_prefix(identifier)
        )
        buckets.setdefault(prefix, {})[identifier] = record
    mapping: dict[str, str] = {}
    for prefix, entries in sorted(buckets.items()):
        shard = _empty_shard(kind, prefix)
        shard["entries"] = dict(sorted(entries.items()))
        mapping[prefix] = _store_shard(index_dir, shard)
    return mapping


def _generation_id(core: dict[str, Any], previous_manifest_sha256: str | None) -> str:
    return _digest(
        {
            "schema": "bounded_jsonl_generation_commitment_v1",
            "previous_manifest_sha256": previous_manifest_sha256,
            "state": core,
        }
    )


def _publish_generation(
    *,
    ledger_path: Path,
    index_dir: Path,
    event_shards: dict[str, str],
    idempotency_shards: dict[str, str],
    event_count: int,
    idempotency_count: int,
    chunk_hashes: list[str],
    chunk_size: int,
    event_id_field: str,
    idempotency_field: str,
    max_event_bytes: int,
    previous_manifest_sha256: str | None,
    receipt_details: dict[str, Any],
) -> dict[str, Any]:
    info = _ledger_stat(ledger_path)
    expected_chunks = (
        (info.st_size + chunk_size - 1) // chunk_size if info.st_size else 0
    )
    if len(chunk_hashes) != expected_chunks:
        raise BoundedJsonlIndexError("ledger changed before generation publication")
    if chunk_hashes:
        last_index = len(chunk_hashes) - 1
        with ledger_path.open("rb") as handle:
            handle.seek(last_index * chunk_size)
            last_chunk = handle.read(chunk_size)
        if _sha256_bytes(last_chunk) != chunk_hashes[last_index]:
            raise BoundedJsonlIndexError("ledger changed before generation publication")
    ledger_commitment = {
        "path_sha256": _sha256_bytes(str(ledger_path.resolve()).encode("utf-8")),
        "device": info.st_dev,
        "inode": info.st_ino,
        "size_bytes": info.st_size,
        "mtime_ns": info.st_mtime_ns,
        "chunk_size": chunk_size,
        "chunk_hashes": chunk_hashes,
        "root_sha256": _ledger_root(info.st_size, chunk_size, chunk_hashes),
    }
    state_core = {
        "ledger": ledger_commitment,
        "event_shards": dict(sorted(event_shards.items())),
        "idempotency_shards": dict(sorted(idempotency_shards.items())),
        "event_count": event_count,
        "idempotency_count": idempotency_count,
        "event_id_field": event_id_field,
        "idempotency_field": idempotency_field,
        "max_event_bytes": max_event_bytes,
    }
    generation_id = _generation_id(state_core, previous_manifest_sha256)
    receipt = {
        "schema": COMMIT_RECEIPT_SCHEMA,
        "generation_id": generation_id,
        "previous_manifest_sha256": previous_manifest_sha256,
        "ledger_size_bytes": info.st_size,
        "ledger_root_sha256": ledger_commitment["root_sha256"],
        "event_count": event_count,
        "idempotency_count": idempotency_count,
        "commit": receipt_details,
        "answer_safe": True,
        "formal_write_count": 0,
    }
    receipt_path, receipt_sha = _content_write(index_dir / "receipts", receipt)
    manifest_core = {
        "schema": MANIFEST_SCHEMA,
        "generation_id": generation_id,
        "previous_manifest_sha256": previous_manifest_sha256,
        "ledger": ledger_commitment,
        "event_shards": state_core["event_shards"],
        "idempotency_shards": state_core["idempotency_shards"],
        "event_count": event_count,
        "idempotency_count": idempotency_count,
        "event_id_field": event_id_field,
        "idempotency_field": idempotency_field,
        "max_event_bytes": max_event_bytes,
        "last_commit_receipt_sha256": receipt_sha,
        "answer_safe": True,
        "formal_write_count": 0,
    }
    key = _load_or_create_key(index_dir, create=True)
    assert key is not None
    signed = _sign(manifest_core, key)
    manifest_sha = _atomic_write(_manifest_path(index_dir), _pretty_bytes(signed))
    return {
        "status": "published",
        "generation_id": generation_id,
        "manifest_sha256": manifest_sha,
        "receipt_path": str(receipt_path),
        "receipt_sha256": receipt_sha,
        "event_count": event_count,
        "ledger_size_bytes": info.st_size,
    }


@contextmanager
def _index_lock(index_dir: Path) -> Iterator[None]:
    index_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    path = index_dir / LOCK_FILENAME
    with path.open("a+", encoding="utf-8") as handle:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def _ensure_ledger(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if not path.exists():
        descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        os.close(descriptor)
    _ledger_stat(path)


def rebuild_index(
    ledger_path: str | Path,
    index_dir: str | Path,
    *,
    event_id_field: str = "event_id",
    idempotency_field: str = "idempotency_key",
    max_event_bytes: int = DEFAULT_MAX_EVENT_BYTES,
    chunk_size: int = DEFAULT_CHUNK_SIZE,
) -> dict[str, Any]:
    """Cold-rebuild the complete exact index and audit commitments."""

    ledger = Path(ledger_path).expanduser().resolve()
    index = Path(index_dir).expanduser().resolve()
    if not SAFE_EVENT_ID_RE.fullmatch(event_id_field) or not SAFE_EVENT_ID_RE.fullmatch(
        idempotency_field
    ):
        raise BoundedJsonlIndexError("index field name is unsafe")
    if max_event_bytes <= 0 or max_event_bytes > DEFAULT_MAX_EVENT_BYTES:
        raise BoundedJsonlIndexError("max_event_bytes exceeds the supported bound")
    if chunk_size <= 0 or chunk_size > DEFAULT_CHUNK_SIZE:
        raise BoundedJsonlIndexError("chunk_size exceeds the supported bound")
    _ensure_ledger(ledger)
    with _index_lock(index):
        previous = (
            _sha256_file(_manifest_path(index)) if _manifest_path(index).is_file() else None
        )
        events, idempotency, observed_size = _build_records(
            ledger,
            event_id_field=event_id_field,
            idempotency_field=idempotency_field,
            max_event_bytes=max_event_bytes,
        )
        if len(events) > MAX_INDEXED_EVENTS or observed_size != _ledger_stat(ledger).st_size:
            raise BoundedJsonlIndexError("ledger changed during cold rebuild")
        event_shards = _store_all_shards(index, events, kind="event_id")
        idempotency_shards = _store_all_shards(
            index, idempotency, kind="idempotency_sha256"
        )
        return _publish_generation(
            ledger_path=ledger,
            index_dir=index,
            event_shards=event_shards,
            idempotency_shards=idempotency_shards,
            event_count=len(events),
            idempotency_count=len(idempotency),
            chunk_hashes=_chunk_hashes(ledger, chunk_size),
            chunk_size=chunk_size,
            event_id_field=event_id_field,
            idempotency_field=idempotency_field,
            max_event_bytes=max_event_bytes,
            previous_manifest_sha256=previous,
            receipt_details={
                "kind": "cold_rebuild",
                "rebuilt_event_count": len(events),
            },
        )


def _updated_chunk_hashes(
    ledger: Path, manifest: dict[str, Any], new_size: int
) -> list[str]:
    commitment = manifest["ledger"]
    old_size = int(commitment["size_bytes"])
    chunk_size = int(commitment["chunk_size"])
    old_hashes = list(commitment["chunk_hashes"])
    if old_size:
        verify_index = (old_size - 1) // chunk_size
        with ledger.open("rb") as handle:
            handle.seek(verify_index * chunk_size)
            existing = handle.read(min(chunk_size, old_size - verify_index * chunk_size))
        if _sha256_bytes(existing) != old_hashes[verify_index]:
            raise BoundedJsonlIndexError("committed ledger tail chunk drifted")
    replace_from = old_size // chunk_size
    if old_size % chunk_size == 0:
        prefix_hashes = old_hashes
    else:
        prefix_hashes = old_hashes[:replace_from]
    hashes = list(prefix_hashes)
    with ledger.open("rb") as handle:
        handle.seek(replace_from * chunk_size)
        remaining = new_size - replace_from * chunk_size
        while remaining > 0:
            chunk = handle.read(min(chunk_size, remaining))
            if not chunk:
                raise BoundedJsonlIndexError("ledger ended during chunk commitment")
            hashes.append(_sha256_bytes(chunk))
            remaining -= len(chunk)
    return hashes


def _apply_tail_records(
    ledger: Path,
    index: Path,
    manifest: dict[str, Any],
    tail: bytes,
) -> tuple[dict[str, str], dict[str, str], list[dict[str, Any]]]:
    if tail and not tail.endswith(b"\n"):
        raise BoundedJsonlIndexError("unindexed tail ends with an incomplete JSONL event")
    event_shards = dict(manifest["event_shards"])
    idem_shards = dict(manifest["idempotency_shards"])
    loaded_events: dict[str, dict[str, Any]] = {}
    loaded_idem: dict[str, dict[str, Any]] = {}
    recovered: list[dict[str, Any]] = []
    offset = int(manifest["ledger"]["size_bytes"])
    for line in tail.splitlines(keepends=True):
        event, event_id, idem_key, event_sha = _parse_line(
            line,
            event_id_field=manifest["event_id_field"],
            idempotency_field=manifest["idempotency_field"],
            max_event_bytes=int(manifest["max_event_bytes"]),
        )
        event_prefix = _shard_prefix(event_id)
        event_shard = loaded_events.get(event_prefix)
        if event_shard is None:
            event_shard = _load_shard(
                index,
                event_shards,
                prefix=event_prefix,
                kind="event_id",
            )
            event_shard = json.loads(json.dumps(event_shard))
            loaded_events[event_prefix] = event_shard
        if event_id in event_shard["entries"]:
            raise BoundedJsonlIndexError("tail repeats an indexed event_id")
        record = {
            "event_id": event_id,
            "event_sha256": event_sha,
            "line_sha256": _sha256_bytes(line),
            "byte_offset": offset,
            "byte_length": len(line),
            "idempotency_key_sha256": (
                _sha256_bytes(idem_key.encode("utf-8")) if idem_key else None
            ),
        }
        event_shard["entries"][event_id] = record
        if idem_key:
            idem_digest = _sha256_bytes(idem_key.encode("utf-8"))
            idem_prefix = idem_digest[:SHARD_PREFIX_LENGTH]
            idem_shard = loaded_idem.get(idem_prefix)
            if idem_shard is None:
                idem_shard = _load_shard(
                    index,
                    idem_shards,
                    prefix=idem_prefix,
                    kind="idempotency_sha256",
                )
                idem_shard = json.loads(json.dumps(idem_shard))
                loaded_idem[idem_prefix] = idem_shard
            previous = idem_shard["entries"].get(idem_digest)
            if previous and previous.get("event_sha256") != event_sha:
                raise BoundedJsonlIndexError("tail contains an idempotency conflict")
            idem_shard["entries"][idem_digest] = {
                "event_id": event_id,
                "event_sha256": event_sha,
            }
        recovered.append(
            {
                "event_id": event_id,
                "event_sha256": event_sha,
                "line_sha256": record["line_sha256"],
                "byte_offset": offset,
                "byte_length": len(line),
                "idempotency_key_sha256": record["idempotency_key_sha256"],
            }
        )
        offset += len(line)
    for prefix, shard in loaded_events.items():
        shard["entries"] = dict(sorted(shard["entries"].items()))
        event_shards[prefix] = _store_shard(index, shard)
    for prefix, shard in loaded_idem.items():
        shard["entries"] = dict(sorted(shard["entries"].items()))
        idem_shards[prefix] = _store_shard(index, shard)
    return event_shards, idem_shards, recovered


def _recover_tail_locked(
    ledger: Path,
    index: Path,
    *,
    max_tail_bytes: int,
    max_tail_events: int,
) -> dict[str, Any]:
    manifest, manifest_sha = _load_manifest(ledger, index, allow_tail=True)
    committed = int(manifest["ledger"]["size_bytes"])
    current = _ledger_stat(ledger).st_size
    if current == committed:
        return {
            "status": "already_current",
            "manifest_sha256": manifest_sha,
            "event_count": int(manifest["event_count"]),
        }
    tail_size = current - committed
    if tail_size > max_tail_bytes:
        raise BoundedJsonlIndexError("tail recovery byte budget exceeded")
    with ledger.open("rb") as handle:
        handle.seek(committed)
        tail = handle.read(tail_size)
    event_count = tail.count(b"\n")
    if event_count > max_tail_events:
        raise BoundedJsonlIndexError("tail recovery event budget exceeded")
    event_shards, idem_shards, recovered = _apply_tail_records(
        ledger, index, manifest, tail
    )
    if len(recovered) != event_count:
        raise BoundedJsonlIndexError("tail recovery event accounting drifted")
    chunks = _updated_chunk_hashes(ledger, manifest, current)
    result = _publish_generation(
        ledger_path=ledger,
        index_dir=index,
        event_shards=event_shards,
        idempotency_shards=idem_shards,
        event_count=int(manifest["event_count"]) + len(recovered),
        idempotency_count=int(manifest["idempotency_count"])
        + sum(1 for row in recovered if row["idempotency_key_sha256"]),
        chunk_hashes=chunks,
        chunk_size=int(manifest["ledger"]["chunk_size"]),
        event_id_field=manifest["event_id_field"],
        idempotency_field=manifest["idempotency_field"],
        max_event_bytes=int(manifest["max_event_bytes"]),
        previous_manifest_sha256=manifest_sha,
        receipt_details={
            "kind": "bounded_tail_recovery",
            "tail_bytes": tail_size,
            "recovered_events": recovered,
        },
    )
    result["recovered_event_count"] = len(recovered)
    return result


def recover_tail(
    ledger_path: str | Path,
    index_dir: str | Path,
    *,
    max_tail_bytes: int = DEFAULT_MAX_TAIL_BYTES,
    max_tail_events: int = DEFAULT_MAX_TAIL_EVENTS,
) -> dict[str, Any]:
    ledger = Path(ledger_path).expanduser().resolve()
    index = Path(index_dir).expanduser().resolve()
    if max_tail_bytes <= 0 or max_tail_bytes > DEFAULT_MAX_TAIL_BYTES:
        raise BoundedJsonlIndexError("max_tail_bytes exceeds the supported bound")
    if max_tail_events <= 0 or max_tail_events > DEFAULT_MAX_TAIL_EVENTS:
        raise BoundedJsonlIndexError("max_tail_events exceeds the supported bound")
    with _index_lock(index):
        return _recover_tail_locked(
            ledger,
            index,
            max_tail_bytes=max_tail_bytes,
            max_tail_events=max_tail_events,
        )


@dataclass
class LockedJsonlWriter:
    ledger_path: Path
    index_dir: Path
    max_tail_bytes: int
    max_tail_events: int

    def recover_tail(self) -> dict[str, Any]:
        return _recover_tail_locked(
            self.ledger_path,
            self.index_dir,
            max_tail_bytes=self.max_tail_bytes,
            max_tail_events=self.max_tail_events,
        )

    def append(
        self,
        event: dict[str, Any],
        *,
        idempotency_key: str | None = None,
    ) -> dict[str, Any]:
        manifest, _ = _load_manifest(
            self.ledger_path, self.index_dir, allow_tail=True
        )
        if _ledger_stat(self.ledger_path).st_size != int(manifest["ledger"]["size_bytes"]):
            self.recover_tail()
            manifest, _ = _load_manifest(
                self.ledger_path, self.index_dir, allow_tail=False
            )
        event_id, event_key, event_sha = _event_identity(
            event,
            event_id_field=manifest["event_id_field"],
            idempotency_field=manifest["idempotency_field"],
        )
        if idempotency_key is not None and event_key != idempotency_key:
            raise BoundedJsonlIndexError(
                "idempotency_key must equal the ledger event idempotency field"
            )
        effective_key = idempotency_key or event_key
        if effective_key:
            prior = _record_for_idempotency(self.index_dir, manifest, effective_key)
            if prior:
                if prior.get("event_sha256") != event_sha:
                    raise BoundedJsonlIndexError(
                        "idempotency key was reused with different event content"
                    )
                return {
                    "status": "already_committed",
                    "event_id": prior["event_id"],
                    "event_sha256": event_sha,
                    "idempotency_key_sha256": _sha256_bytes(
                        effective_key.encode("utf-8")
                    ),
                }
        prior_event = _record_for_event(
            self.ledger_path, self.index_dir, manifest, event_id
        )
        if prior_event is not None:
            if prior_event.get("event_sha256") == event_sha:
                return {
                    "status": "already_committed",
                    "event_id": event_id,
                    "event_sha256": event_sha,
                    "idempotency_key_sha256": (
                        _sha256_bytes(effective_key.encode("utf-8"))
                        if effective_key
                        else None
                    ),
                    "matched_by": "event_id",
                }
            raise BoundedJsonlIndexError("event_id already exists with different content")
        line = _canonical_bytes(event)
        if len(line) > int(manifest["max_event_bytes"]):
            raise BoundedJsonlIndexError("event exceeds max_event_bytes")
        with self.ledger_path.open("ab") as handle:
            handle.write(line)
            handle.flush()
            os.fsync(handle.fileno())
        result = self.recover_tail()
        result["event_id"] = event_id
        result["event_sha256"] = event_sha
        return result


@contextmanager
def locked_writer(
    ledger_path: str | Path,
    index_dir: str | Path,
    *,
    max_tail_bytes: int = DEFAULT_MAX_TAIL_BYTES,
    max_tail_events: int = DEFAULT_MAX_TAIL_EVENTS,
) -> Iterator[LockedJsonlWriter]:
    """Yield the only supported lock-held append/recovery interface."""

    ledger = Path(ledger_path).expanduser().resolve()
    index = Path(index_dir).expanduser().resolve()
    _ensure_ledger(ledger)
    if max_tail_bytes <= 0 or max_tail_bytes > DEFAULT_MAX_TAIL_BYTES:
        raise BoundedJsonlIndexError("max_tail_bytes exceeds the supported bound")
    if max_tail_events <= 0 or max_tail_events > DEFAULT_MAX_TAIL_EVENTS:
        raise BoundedJsonlIndexError("max_tail_events exceeds the supported bound")
    with _index_lock(index):
        if not _manifest_path(index).is_file():
            # Initial empty/non-empty adoption is a cold action under the same
            # lock.  Inline it to avoid attempting to reacquire the lock.
            events, idempotency, observed_size = _build_records(
                ledger,
                event_id_field="event_id",
                idempotency_field="idempotency_key",
                max_event_bytes=DEFAULT_MAX_EVENT_BYTES,
            )
            if observed_size != _ledger_stat(ledger).st_size:
                raise BoundedJsonlIndexError("ledger changed during initial adoption")
            _publish_generation(
                ledger_path=ledger,
                index_dir=index,
                event_shards=_store_all_shards(index, events, kind="event_id"),
                idempotency_shards=_store_all_shards(
                    index, idempotency, kind="idempotency_sha256"
                ),
                event_count=len(events),
                idempotency_count=len(idempotency),
                chunk_hashes=_chunk_hashes(ledger, DEFAULT_CHUNK_SIZE),
                chunk_size=DEFAULT_CHUNK_SIZE,
                event_id_field="event_id",
                idempotency_field="idempotency_key",
                max_event_bytes=DEFAULT_MAX_EVENT_BYTES,
                previous_manifest_sha256=None,
                receipt_details={
                    "kind": "initial_locked_adoption",
                    "rebuilt_event_count": len(events),
                },
            )
        yield LockedJsonlWriter(
            ledger_path=ledger,
            index_dir=index,
            max_tail_bytes=max_tail_bytes,
            max_tail_events=max_tail_events,
        )


def append_event(
    ledger_path: str | Path,
    index_dir: str | Path,
    event: dict[str, Any],
    *,
    idempotency_key: str | None = None,
) -> dict[str, Any]:
    """Convenience wrapper around the lock-held writer API."""

    with locked_writer(ledger_path, index_dir) as writer:
        return writer.append(event, idempotency_key=idempotency_key)


def audit_index(
    ledger_path: str | Path, index_dir: str | Path
) -> dict[str, Any]:
    """Cold full audit of ledger chunks, shards, byte ranges, and counts."""

    ledger = Path(ledger_path).expanduser().resolve()
    index = Path(index_dir).expanduser().resolve()
    manifest, manifest_sha = _load_manifest(ledger, index, allow_tail=False)
    actual_chunks = _chunk_hashes(ledger, int(manifest["ledger"]["chunk_size"]))
    if actual_chunks != manifest["ledger"]["chunk_hashes"]:
        raise BoundedJsonlIndexError("cold audit found ledger chunk drift")
    event_seen: dict[str, str] = {}
    idem_seen: set[str] = set()
    for prefix, digest in manifest["event_shards"].items():
        shard = _load_shard(
            index,
            manifest["event_shards"],
            prefix=prefix,
            kind="event_id",
        )
        if digest != manifest["event_shards"][prefix]:
            raise BoundedJsonlIndexError("event shard mapping drifted")
        for event_id, record in shard["entries"].items():
            if event_id in event_seen:
                raise BoundedJsonlIndexError("event index contains a duplicate")
            line = _read_record_bytes(ledger, record)
            _, actual_id, _, event_sha = _parse_line(
                line,
                event_id_field=manifest["event_id_field"],
                idempotency_field=manifest["idempotency_field"],
                max_event_bytes=int(manifest["max_event_bytes"]),
            )
            if actual_id != event_id or event_sha != record.get("event_sha256"):
                raise BoundedJsonlIndexError("event index record drifted")
            event_seen[event_id] = event_sha
    for prefix in manifest["idempotency_shards"]:
        shard = _load_shard(
            index,
            manifest["idempotency_shards"],
            prefix=prefix,
            kind="idempotency_sha256",
        )
        for digest, record in shard["entries"].items():
            if digest in idem_seen or not SHA256_RE.fullmatch(digest):
                raise BoundedJsonlIndexError("idempotency index contains a duplicate")
            idem_seen.add(digest)
            if record.get("event_id") not in event_seen:
                raise BoundedJsonlIndexError("idempotency index points outside event index")
            if record.get("event_sha256") != event_seen[record["event_id"]]:
                raise BoundedJsonlIndexError("idempotency index event hash drifted")
    if (
        len(event_seen) != int(manifest["event_count"])
        or len(idem_seen) != int(manifest["idempotency_count"])
    ):
        raise BoundedJsonlIndexError("index count commitment drifted")
    receipt_sha = str(manifest.get("last_commit_receipt_sha256") or "")
    receipt_path = index / "receipts" / f"{receipt_sha}.json"
    if (
        not SHA256_RE.fullmatch(receipt_sha)
        or not receipt_path.is_file()
        or receipt_path.is_symlink()
        or _sha256_file(receipt_path) != receipt_sha
    ):
        raise BoundedJsonlIndexError("last commit receipt drifted")
    receipt = _read_object(receipt_path, "last commit receipt", max_bytes=MAX_MANIFEST_BYTES)
    if (
        receipt.get("schema") != COMMIT_RECEIPT_SCHEMA
        or receipt.get("generation_id") != manifest.get("generation_id")
        or receipt.get("ledger_root_sha256") != manifest["ledger"]["root_sha256"]
        or receipt.get("answer_safe") is not True
        or receipt.get("formal_write_count") != 0
    ):
        raise BoundedJsonlIndexError("last commit receipt is invalid")
    return {
        "schema": AUDIT_SCHEMA,
        "status": "pass",
        "manifest_sha256": manifest_sha,
        "generation_id": manifest["generation_id"],
        "event_count": len(event_seen),
        "idempotency_count": len(idem_seen),
        "ledger_size_bytes": manifest["ledger"]["size_bytes"],
        "ledger_root_sha256": manifest["ledger"]["root_sha256"],
        "answer_safe": True,
        "formal_write_count": 0,
    }


def _load_event_arg(raw: str) -> dict[str, Any]:
    try:
        value = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise BoundedJsonlIndexError("event-json is invalid") from exc
    if not isinstance(value, dict):
        raise BoundedJsonlIndexError("event-json must decode to an object")
    return value


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--ledger", required=True)
    parser.add_argument("--index-dir", required=True)
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("rebuild")
    sub.add_parser("audit")
    recover = sub.add_parser("recover-tail")
    recover.add_argument("--max-tail-bytes", type=int, default=DEFAULT_MAX_TAIL_BYTES)
    recover.add_argument("--max-tail-events", type=int, default=DEFAULT_MAX_TAIL_EVENTS)
    lookup = sub.add_parser("lookup-event")
    lookup.add_argument("--event-id", required=True)
    idem = sub.add_parser("lookup-idempotency")
    idem.add_argument("--idempotency-key", required=True)
    append = sub.add_parser("append")
    append.add_argument("--event-json", required=True)
    append.add_argument("--idempotency-key")
    return parser


def main(argv: Iterable[str] | None = None) -> int:
    args = _parser().parse_args(list(argv) if argv is not None else None)
    try:
        if args.command == "rebuild":
            result = rebuild_index(args.ledger, args.index_dir)
        elif args.command == "audit":
            result = audit_index(args.ledger, args.index_dir)
        elif args.command == "recover-tail":
            result = recover_tail(
                args.ledger,
                args.index_dir,
                max_tail_bytes=args.max_tail_bytes,
                max_tail_events=args.max_tail_events,
            )
        elif args.command == "lookup-event":
            result = lookup_event(args.ledger, args.index_dir, args.event_id) or {
                "status": "not_found"
            }
        elif args.command == "lookup-idempotency":
            result = lookup_idempotency(
                args.ledger, args.index_dir, args.idempotency_key
            ) or {"status": "not_found"}
        else:
            result = append_event(
                args.ledger,
                args.index_dir,
                _load_event_arg(args.event_json),
                idempotency_key=args.idempotency_key,
            )
    except BoundedJsonlIndexError as exc:
        print(json.dumps({"status": "error", "reason": str(exc)}, ensure_ascii=False))
        return 2
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    sys.exit(main())
