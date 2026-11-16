#!/usr/bin/env python3
"""SQLite hot projection for canonical 408 review outcomes.

Cold rebuild fully validates and replays the canonical review JSONL.  Hot
reads use only a content-addressed manifest, SQLite exact indexes, ledger stat,
and a bounded ledger tail commitment.  The module deliberately does not read
``state.json`` or any formal Markdown table.

``append_commit`` is an adoption API for a future core integration.  Its
caller must already hold the review runtime lock.  It appends and fsyncs the
canonical ledger before transactionally updating SQLite.  A crash in between
therefore leaves an unindexed ledger tail; every subsequent hot call fails
closed with ``rebuild_required`` rather than silently recovering it.
"""

from __future__ import annotations

import argparse
import contextlib
import hashlib
import json
import os
import re
import sqlite3
import stat
import sys
import tempfile
import time
from datetime import date
from pathlib import Path
from typing import Any, Iterator

import review_feedback_loop as canonical
from intake_lib_408 import scan_leak
from study_commit_followup_408 import note_ledger_commit


SCHEMA = "review_hot_state_v1"
POINTER_SCHEMA = "review_hot_state_pointer_v1"
PRECONDITION_SCHEMA = "review_hot_state_precondition_v1"
RECEIPT_SCHEMA = "review_hot_state_commit_receipt_v1"
READINESS_SCHEMA = "review_hot_state_readiness_v1"
BEFORE_LEARNER_ANSWER_PHASE = "before_learner_answer"
DEFAULT_STATE_RELATIVE = Path(
    "wiki/study_vaults/408-full/state/review-loop/hot-state"
)
LEDGER_RELATIVE = Path("wiki/study_vaults/408-full/state/review-loop/events.jsonl")
DB_SCHEMA_VERSION = 3
TAIL_BYTES = 65_536
MAX_EVENT_BYTES = 65_536
DEFAULT_MAX_VIEW_BYTES = 32_768
MIN_VIEW_BYTES = 2_048
MAX_VIEW_BYTES = 131_072
MAX_DERIVED_INTEGRITY_ROWS = 50_000
MAX_DERIVED_INTEGRITY_BYTES = 32 * 1024 * 1024
SHA_RE = re.compile(r"^[0-9a-f]{64}$")
SAFE_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:#/-]{0,255}$")
ROW_HASH_SCHEMA = "review_hot_state_row_hash_v1"
COLLECTION_HASH_SCHEMA = "review_hot_state_collection_hash_v1"
DERIVED_INTEGRITY_SCHEMA = "review_hot_state_derived_integrity_v1"
EVENT_REF_SCHEMA = "review_hot_state_event_ref_v1"
EVENT_ALIAS_SCHEMA = "review_hot_state_event_alias_v1"


class HotStateError(RuntimeError):
    """Stable fail-closed hot-state error."""


class SimulatedHotStateCrash(RuntimeError):
    """Test-only crash point after the durable ledger append."""


def _json_bytes(value: Any, *, pretty: bool = False) -> bytes:
    options: dict[str, Any] = {"ensure_ascii": False, "sort_keys": True}
    if pretty:
        options["indent"] = 2
    else:
        options["separators"] = (",", ":")
    return (json.dumps(value, **options) + "\n").encode("utf-8")


def _sha256_bytes(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _value_sha(value: Any) -> str:
    return _sha256_bytes(_json_bytes(value))


def _fsync_dir(path: Path) -> None:
    descriptor = os.open(str(path), os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _atomic_write(path: Path, raw: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    descriptor, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        os.fchmod(descriptor, 0o600)
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(raw)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
        _fsync_dir(path.parent)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def _content_write(directory: Path, value: dict[str, Any]) -> tuple[Path, str]:
    raw = _json_bytes(value, pretty=True)
    digest = _sha256_bytes(raw)
    path = directory / f"{digest}.json"
    if path.exists():
        if path.is_symlink() or path.read_bytes() != raw:
            raise HotStateError("content_address_conflict")
        return path, digest
    _atomic_write(path, raw)
    return path, digest


def _answer_safe(value: Any) -> None:
    payload = json.dumps(value, ensure_ascii=False, sort_keys=True)
    fails, warns = scan_leak(payload)
    if canonical.PROTECTED_RE.search(payload) or fails or warns:
        raise HotStateError("protected_content_in_hot_state")


def _repo(raw: str | Path) -> Path:
    value = Path(raw).expanduser().resolve()
    if not value.is_dir():
        raise HotStateError("repo_missing")
    return value


def _ledger(repo: Path) -> Path:
    path = (repo / LEDGER_RELATIVE).resolve()
    try:
        path.relative_to(repo)
    except ValueError as exc:
        raise HotStateError("ledger_path_escaped_repo") from exc
    return path


def _state_dir(repo: Path, state_dir: str | Path | None) -> Path:
    path = Path(state_dir) if state_dir is not None else repo / DEFAULT_STATE_RELATIVE
    if not path.is_absolute():
        path = repo / path
    return path.resolve()


def _regular_stat(path: Path) -> os.stat_result:
    try:
        info = path.lstat()
    except OSError as exc:
        raise HotStateError("ledger_missing") from exc
    if stat.S_ISLNK(info.st_mode) or not stat.S_ISREG(info.st_mode):
        raise HotStateError("ledger_not_regular")
    return info


def _chain_step(previous: str, line_sha: str, offset: int, end: int) -> str:
    return _value_sha(
        {
            "schema": "review_hot_state_event_chain_v1",
            "previous": previous,
            "line_sha256": line_sha,
            "byte_offset": offset,
            "byte_end": end,
        }
    )


def _tail_commitment(path: Path, size: int) -> tuple[int, str]:
    start = max(0, size - TAIL_BYTES)
    with path.open("rb") as handle:
        handle.seek(start)
        raw = handle.read(size - start)
    if len(raw) != size - start:
        raise HotStateError("ledger_tail_short_read")
    return start, _sha256_bytes(raw)


def _ledger_commitment(
    path: Path,
    *,
    event_count: int,
    event_chain_sha256: str,
    full_sha256: str | None,
    full_hash_scope: str,
    last_event_id: str | None,
    last_line_sha256: str | None,
) -> dict[str, Any]:
    info = _regular_stat(path)
    tail_start, tail_sha = _tail_commitment(path, info.st_size)
    value = {
        "path_sha256": _sha256_bytes(str(path.resolve()).encode("utf-8")),
        "device": info.st_dev,
        "inode": info.st_ino,
        "size_bytes": info.st_size,
        "mtime_ns": info.st_mtime_ns,
        "indexed_offset": info.st_size,
        "event_count": event_count,
        "event_chain_sha256": event_chain_sha256,
        "full_sha256": full_sha256,
        "full_hash_scope": full_hash_scope,
        "tail_start_offset": tail_start,
        "tail_sha256": tail_sha,
        "last_event_id": last_event_id,
        "last_line_sha256": last_line_sha256,
    }
    value["commitment_sha256"] = _value_sha(value)
    return value


def _parse_ledger(repo: Path) -> tuple[list[dict[str, Any]], list[dict[str, Any]], dict[str, Any]]:
    path = _ledger(repo)
    _regular_stat(path)
    records: list[dict[str, Any]] = []
    events: list[dict[str, Any]] = []
    seen_events: set[str] = set()
    seen_keys: set[str] = set()
    offset = 0
    chain = "0" * 64
    with path.open("rb") as handle:
        for line_number, line in enumerate(handle, start=1):
            if not line.endswith(b"\n") or len(line) > MAX_EVENT_BYTES:
                raise HotStateError(f"invalid_ledger_line:{line_number}")
            try:
                event = json.loads(line)
            except (UnicodeError, json.JSONDecodeError) as exc:
                raise HotStateError(f"invalid_ledger_json:{line_number}") from exc
            if not isinstance(event, dict) or event.get("schema") != canonical.LEDGER_SCHEMA:
                raise HotStateError(f"invalid_event_schema:{line_number}")
            event_id = str(event.get("event_id") or "")
            key = str(event.get("idempotency_key") or "")
            if (
                not SAFE_ID_RE.fullmatch(event_id)
                or not SAFE_ID_RE.fullmatch(key)
                or event_id in seen_events
                or key in seen_keys
            ):
                raise HotStateError(f"duplicate_or_invalid_event_identity:{line_number}")
            _answer_safe(event)
            line_sha = _sha256_bytes(line)
            event_sha = _value_sha(event)
            end = offset + len(line)
            chain = _chain_step(chain, line_sha, offset, end)
            records.append(
                {
                    "event_id": event_id,
                    "idempotency_key": key,
                    "event_kind": str(event.get("event_kind") or ""),
                    "source": event.get("source"),
                    "session_id": event.get("session_id"),
                    "item_id": event.get("item_id"),
                    "identity": canonical._identity(event),
                    "observed_date": event.get("observed_date"),
                    "evidence_kind": event.get("evidence_kind"),
                    "event_json": canonical.canonical_json(event),
                    "event_sha256": event_sha,
                    "line_sha256": line_sha,
                    "byte_offset": offset,
                    "byte_length": len(line),
                    "line_number": line_number,
                }
            )
            events.append(event)
            seen_events.add(event_id)
            seen_keys.add(key)
            offset = end
    core_events = canonical._read_events(repo)
    if core_events != events:
        raise HotStateError("canonical_reader_parity_failed")
    reduced = canonical._reduce(events)
    _answer_safe(reduced)
    last_record = records[-1] if records else None
    commitment = _ledger_commitment(
        path,
        event_count=len(events),
        event_chain_sha256=chain,
        full_sha256=_sha256_file(path),
        full_hash_scope="cold_rebuild_complete_ledger",
        last_event_id=last_record["event_id"] if last_record else None,
        last_line_sha256=last_record["line_sha256"] if last_record else None,
    )
    if commitment["size_bytes"] != offset:
        raise HotStateError("ledger_changed_during_rebuild")
    return records, events, {"reduced": reduced, "commitment": commitment}


SQL_SCHEMA = """
PRAGMA journal_mode=DELETE;
PRAGMA synchronous=FULL;
PRAGMA foreign_keys=ON;
CREATE TABLE meta (
  key TEXT PRIMARY KEY,
  value TEXT NOT NULL,
  row_sha256 TEXT NOT NULL
);
CREATE TABLE events (
  event_id TEXT PRIMARY KEY,
  idempotency_key TEXT NOT NULL UNIQUE,
  event_kind TEXT NOT NULL,
  source TEXT,
  session_id TEXT,
  item_id TEXT,
  identity TEXT NOT NULL,
  observed_date TEXT,
  evidence_kind TEXT,
  event_json TEXT NOT NULL,
  event_sha256 TEXT NOT NULL,
  line_sha256 TEXT NOT NULL,
  byte_offset INTEGER NOT NULL,
  byte_length INTEGER NOT NULL,
  line_number INTEGER NOT NULL
);
CREATE INDEX events_morning_item_idx
  ON events(source, session_id, item_id, event_kind);
CREATE INDEX events_identity_date_idx
  ON events(identity, event_kind, observed_date);
CREATE TABLE actions (
  action_id TEXT PRIMARY KEY,
  identity TEXT NOT NULL,
  action_type TEXT NOT NULL,
  due_date TEXT NOT NULL,
  status TEXT NOT NULL,
  dedupe_key TEXT,
  action_json TEXT NOT NULL,
  row_sha256 TEXT NOT NULL
);
CREATE INDEX actions_eligible_idx
  ON actions(identity, status, action_type, due_date, action_id);
CREATE TABLE action_formal_bindings (
  action_id TEXT PRIMARY KEY,
  review_unit_id TEXT NOT NULL,
  binding_json TEXT NOT NULL,
  row_sha256 TEXT NOT NULL,
  FOREIGN KEY(action_id) REFERENCES actions(action_id)
);
CREATE TABLE baselines (
  identity TEXT PRIMARY KEY,
  event_id TEXT NOT NULL,
  observed_date TEXT,
  baseline_json TEXT NOT NULL,
  row_sha256 TEXT NOT NULL
);
CREATE TABLE retention (
  identity TEXT PRIMARY KEY,
  retention_count INTEGER NOT NULL,
  last_observed_date TEXT,
  event_ids_json TEXT NOT NULL,
  row_sha256 TEXT NOT NULL
);
CREATE TABLE commit_receipts (
  receipt_sha256 TEXT PRIMARY KEY,
  event_id TEXT NOT NULL UNIQUE,
  idempotency_key TEXT NOT NULL UNIQUE,
  receipt_json TEXT NOT NULL
);
"""


def _canonical_value(value: Any) -> str:
    return json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    )


def _row_sha(collection: str, key: str, material: dict[str, Any]) -> str:
    return _value_sha(
        {
            "schema": ROW_HASH_SCHEMA,
            "collection": collection,
            "key": key,
            "material": material,
        }
    )


def _meta_material(key: str, value_json: str) -> dict[str, Any]:
    return {"key": key, "value_json": value_json}


def _meta_set(connection: sqlite3.Connection, key: str, value: Any) -> None:
    value_json = _canonical_value(value)
    connection.execute(
        "INSERT OR REPLACE INTO meta(key,value,row_sha256) VALUES(?,?,?)",
        (key, value_json, _row_sha("meta", key, _meta_material(key, value_json))),
    )


def _verify_meta_row(row: sqlite3.Row) -> tuple[str, str, int]:
    key = str(row["key"])
    value_json = str(row["value"])
    expected = _row_sha("meta", key, _meta_material(key, value_json))
    if row["row_sha256"] != expected:
        raise HotStateError("sqlite_meta_row_hash_drift_rebuild_required")
    try:
        parsed = json.loads(value_json)
    except json.JSONDecodeError as exc:
        raise HotStateError(f"sqlite_meta_invalid:{key}") from exc
    if _canonical_value(parsed) != value_json:
        raise HotStateError(f"sqlite_meta_noncanonical:{key}")
    return key, expected, len(value_json.encode("utf-8"))


def _meta_get(connection: sqlite3.Connection, key: str) -> Any:
    row = connection.execute(
        "SELECT key,value,row_sha256 FROM meta WHERE key=?", (key,)
    ).fetchone()
    if row is None:
        raise HotStateError(f"sqlite_meta_missing_rebuild_required:{key}")
    _verify_meta_row(row)
    return json.loads(row["value"])


def _action_values(action: dict[str, Any]) -> tuple[Any, ...]:
    action_json = canonical.canonical_json(action)
    material = {
        "action_id": action["action_id"],
        "identity": action["identity"],
        "action_type": action["action_type"],
        "due_date": action["due_date"],
        "status": action["status"],
        "dedupe_key": action.get("dedupe_key"),
        "action_json": action_json,
    }
    return (
        action["action_id"],
        action["identity"],
        action["action_type"],
        action["due_date"],
        action["status"],
        action.get("dedupe_key"),
        action_json,
        _row_sha("actions", str(action["action_id"]), material),
    )


def _insert_action(connection: sqlite3.Connection, action: dict[str, Any]) -> None:
    connection.execute(
        "INSERT OR REPLACE INTO actions VALUES(?,?,?,?,?,?,?,?)",
        _action_values(action),
    )


def _verify_action_row(
    row: sqlite3.Row, *, semantic: bool = True
) -> tuple[str, str, int]:
    key = str(row["action_id"])
    action_json = str(row["action_json"])
    material = {
        "action_id": key,
        "identity": row["identity"],
        "action_type": row["action_type"],
        "due_date": row["due_date"],
        "status": row["status"],
        "dedupe_key": row["dedupe_key"],
        "action_json": action_json,
    }
    expected = _row_sha("actions", key, material)
    if row["row_sha256"] != expected:
        raise HotStateError("sqlite_action_row_hash_drift_rebuild_required")
    if not semantic:
        return key, expected, len(action_json.encode("utf-8"))
    try:
        action = json.loads(action_json)
    except json.JSONDecodeError as exc:
        raise HotStateError("sqlite_action_json_invalid") from exc
    if canonical.canonical_json(action) != action_json or any(
        action.get(field) != material[field]
        for field in ("action_id", "identity", "action_type", "due_date", "status", "dedupe_key")
    ):
        raise HotStateError("sqlite_action_row_semantic_drift_rebuild_required")
    return key, expected, len(action_json.encode("utf-8"))


def _action_formal_binding_values(binding: dict[str, Any]) -> tuple[Any, ...]:
    binding_json = canonical.canonical_json(binding)
    material = {
        "action_id": binding["action_id"],
        "review_unit_id": binding["review_unit_id"],
        "binding_json": binding_json,
    }
    return (
        binding["action_id"],
        binding["review_unit_id"],
        binding_json,
        _row_sha(
            "action_formal_bindings", str(binding["action_id"]), material
        ),
    )


def _verify_action_formal_binding_row(
    row: sqlite3.Row, *, semantic: bool = True
) -> tuple[str, str, int]:
    key = str(row["action_id"])
    binding_json = str(row["binding_json"])
    material = {
        "action_id": key,
        "review_unit_id": row["review_unit_id"],
        "binding_json": binding_json,
    }
    expected = _row_sha("action_formal_bindings", key, material)
    if row["row_sha256"] != expected:
        raise HotStateError(
            "sqlite_action_formal_binding_row_hash_drift_rebuild_required"
        )
    if not semantic:
        return key, expected, len(binding_json.encode("utf-8"))
    try:
        binding = json.loads(binding_json)
    except json.JSONDecodeError as exc:
        raise HotStateError("sqlite_action_formal_binding_json_invalid") from exc
    if (
        canonical.canonical_json(binding) != binding_json
        or binding.get("action_id") != key
        or binding.get("review_unit_id") != row["review_unit_id"]
    ):
        raise HotStateError(
            "sqlite_action_formal_binding_semantic_drift_rebuild_required"
        )
    return key, expected, len(binding_json.encode("utf-8"))


def _baseline_values(baseline: dict[str, Any]) -> tuple[Any, ...]:
    baseline_json = canonical.canonical_json(baseline)
    material = {
        "identity": baseline["identity"],
        "event_id": baseline["event_id"],
        "observed_date": baseline.get("observed_date"),
        "baseline_json": baseline_json,
    }
    return (
        baseline["identity"],
        baseline["event_id"],
        baseline.get("observed_date"),
        baseline_json,
        _row_sha("baselines", str(baseline["identity"]), material),
    )


def _verify_baseline_row(
    row: sqlite3.Row, *, semantic: bool = True
) -> tuple[str, str, int]:
    key = str(row["identity"])
    baseline_json = str(row["baseline_json"])
    material = {
        "identity": key,
        "event_id": row["event_id"],
        "observed_date": row["observed_date"],
        "baseline_json": baseline_json,
    }
    expected = _row_sha("baselines", key, material)
    if row["row_sha256"] != expected:
        raise HotStateError("sqlite_baseline_row_hash_drift_rebuild_required")
    if not semantic:
        return key, expected, len(baseline_json.encode("utf-8"))
    try:
        baseline = json.loads(baseline_json)
    except json.JSONDecodeError as exc:
        raise HotStateError("sqlite_baseline_json_invalid") from exc
    if canonical.canonical_json(baseline) != baseline_json or any(
        baseline.get(field) != material[field]
        for field in ("identity", "event_id", "observed_date")
    ):
        raise HotStateError("sqlite_baseline_row_semantic_drift_rebuild_required")
    return key, expected, len(baseline_json.encode("utf-8"))


def _retention_values(
    identity: str,
    retention_count: int,
    last_observed_date: str | None,
    event_ids: list[str],
) -> tuple[Any, ...]:
    event_ids_json = canonical.canonical_json(event_ids)
    material = {
        "identity": identity,
        "retention_count": retention_count,
        "last_observed_date": last_observed_date,
        "event_ids_json": event_ids_json,
    }
    return (
        identity,
        retention_count,
        last_observed_date,
        event_ids_json,
        _row_sha("retention", identity, material),
    )


def _verify_retention_row(
    row: sqlite3.Row, *, semantic: bool = True
) -> tuple[str, str, int]:
    key = str(row["identity"])
    event_ids_json = str(row["event_ids_json"])
    material = {
        "identity": key,
        "retention_count": int(row["retention_count"]),
        "last_observed_date": row["last_observed_date"],
        "event_ids_json": event_ids_json,
    }
    expected = _row_sha("retention", key, material)
    if row["row_sha256"] != expected:
        raise HotStateError("sqlite_retention_row_hash_drift_rebuild_required")
    if not semantic:
        return key, expected, len(event_ids_json.encode("utf-8"))
    try:
        event_ids = json.loads(event_ids_json)
    except json.JSONDecodeError as exc:
        raise HotStateError("sqlite_retention_json_invalid") from exc
    if not isinstance(event_ids, list) or canonical.canonical_json(event_ids) != event_ids_json:
        raise HotStateError("sqlite_retention_row_semantic_drift_rebuild_required")
    return key, expected, len(event_ids_json.encode("utf-8"))


def _collection_commitment(
    connection: sqlite3.Connection,
    collection: str,
    *,
    stats: dict[str, Any] | None = None,
) -> dict[str, Any]:
    queries = {
        "actions": ("SELECT * FROM actions ORDER BY action_id", _verify_action_row),
        "action_formal_bindings": (
            "SELECT * FROM action_formal_bindings ORDER BY action_id",
            _verify_action_formal_binding_row,
        ),
        "baselines": ("SELECT * FROM baselines ORDER BY identity", _verify_baseline_row),
        "retention": ("SELECT * FROM retention ORDER BY identity", _verify_retention_row),
        "meta": (
            "SELECT key,value,row_sha256 FROM meta WHERE key<>'derived_integrity' ORDER BY key",
            _verify_meta_row,
        ),
    }
    if collection not in queries:
        raise HotStateError("unknown_integrity_collection")
    sql, verifier = queries[collection]
    rows = connection.execute(sql)
    if stats is not None:
        stats["sqlite_query_count"] += 1
    chain_digest = hashlib.sha256()
    chain_digest.update(COLLECTION_HASH_SCHEMA.encode("ascii"))
    chain_digest.update(b"\0")
    chain_digest.update(collection.encode("ascii"))
    logical_bytes = 0
    row_count = 0
    for row in rows:
        row_count += 1
        if row_count > MAX_DERIVED_INTEGRITY_ROWS:
            raise HotStateError("derived_integrity_row_budget_exceeded_rebuild_required")
        if collection == "meta":
            key, row_sha, payload_bytes = verifier(row)
        else:
            key, row_sha, payload_bytes = verifier(row, semantic=False)
        logical_bytes += payload_bytes
        if logical_bytes > MAX_DERIVED_INTEGRITY_BYTES:
            raise HotStateError("derived_integrity_byte_budget_exceeded_rebuild_required")
        key_raw = key.encode("utf-8")
        chain_digest.update(len(key_raw).to_bytes(4, "big"))
        chain_digest.update(key_raw)
        chain_digest.update(bytes.fromhex(row_sha))
    core = {
        "schema": COLLECTION_HASH_SCHEMA,
        "collection": collection,
        "row_count": row_count,
        "logical_bytes": logical_bytes,
        "chain_sha256": chain_digest.hexdigest(),
    }
    value = {**core, "commitment_sha256": _value_sha(core)}
    if stats is not None:
        stats["derived_integrity_rows"] += row_count
        stats["derived_integrity_bytes"] += logical_bytes
    return value


def _compute_derived_integrity(
    connection: sqlite3.Connection,
    *,
    stats: dict[str, Any] | None = None,
) -> dict[str, Any]:
    collections = {
        name: _collection_commitment(connection, name, stats=stats)
        for name in (
            "actions",
            "action_formal_bindings",
            "baselines",
            "retention",
            "meta",
        )
    }
    core = {
        "schema": DERIVED_INTEGRITY_SCHEMA,
        "collections": collections,
        "row_count": sum(item["row_count"] for item in collections.values()),
        "logical_bytes": sum(item["logical_bytes"] for item in collections.values()),
    }
    if core["row_count"] > MAX_DERIVED_INTEGRITY_ROWS:
        raise HotStateError("derived_integrity_row_budget_exceeded_rebuild_required")
    if core["logical_bytes"] > MAX_DERIVED_INTEGRITY_BYTES:
        raise HotStateError("derived_integrity_byte_budget_exceeded_rebuild_required")
    return {**core, "commitment_sha256": _value_sha(core)}


def _verify_derived_integrity(
    connection: sqlite3.Connection,
    *,
    stats: dict[str, Any],
) -> dict[str, Any]:
    stored = _meta_get(connection, "derived_integrity")
    stats["sqlite_query_count"] += 1
    computed = _compute_derived_integrity(connection, stats=stats)
    if stored != computed:
        raise HotStateError("sqlite_derived_collection_hash_drift_rebuild_required")
    return computed


def _insert_record(connection: sqlite3.Connection, row: dict[str, Any]) -> None:
    connection.execute(
        """
        INSERT INTO events(
          event_id,idempotency_key,event_kind,source,session_id,item_id,identity,
          observed_date,evidence_kind,event_json,event_sha256,line_sha256,
          byte_offset,byte_length,line_number
        ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
        """,
        tuple(
            row[key]
            for key in (
                "event_id",
                "idempotency_key",
                "event_kind",
                "source",
                "session_id",
                "item_id",
                "identity",
                "observed_date",
                "evidence_kind",
                "event_json",
                "event_sha256",
                "line_sha256",
                "byte_offset",
                "byte_length",
                "line_number",
            )
        ),
    )


def _populate_derived(connection: sqlite3.Connection, reduced: dict[str, Any]) -> None:
    for action in reduced["actions"]:
        _insert_action(connection, action)
    for binding in reduced.get("action_formal_bindings", []):
        connection.execute(
            "INSERT INTO action_formal_bindings VALUES(?,?,?,?)",
            _action_formal_binding_values(binding),
        )
    for baseline in reduced["organic_baselines"]:
        connection.execute(
            "INSERT INTO baselines VALUES(?,?,?,?,?)",
            _baseline_values(baseline),
        )
    for identity, value in reduced["retention"].items():
        connection.execute(
            "INSERT INTO retention VALUES(?,?,?,?,?)",
            _retention_values(
                identity,
                int(value["count"]),
                value.get("last_observed_date"),
                value.get("events", []),
            ),
        )


def _create_database(
    path: Path,
    *,
    build_id: str,
    records: list[dict[str, Any]],
    reduced: dict[str, Any],
    commitment: dict[str, Any],
) -> dict[str, Any]:
    connection = sqlite3.connect(path)
    connection.row_factory = sqlite3.Row
    try:
        connection.executescript(SQL_SCHEMA)
        connection.execute(f"PRAGMA user_version={DB_SCHEMA_VERSION}")
        connection.execute("BEGIN IMMEDIATE")
        for record in records:
            _insert_record(connection, record)
        _populate_derived(connection, reduced)
        _meta_set(connection, "schema", SCHEMA)
        _meta_set(connection, "db_schema_version", DB_SCHEMA_VERSION)
        _meta_set(connection, "projection_build_id", build_id)
        _meta_set(connection, "generation", 0)
        _meta_set(connection, "ledger_commitment", commitment)
        _meta_set(connection, "cold_reducer_sha256", _value_sha(reduced))
        _meta_set(connection, "cold_event_count", len(records))
        derived_integrity = _compute_derived_integrity(connection)
        _meta_set(connection, "derived_integrity", derived_integrity)
        connection.commit()
        result = connection.execute("PRAGMA integrity_check").fetchone()
        if not result or result[0] != "ok":
            raise HotStateError("sqlite_integrity_check_failed")
    finally:
        connection.close()
    descriptor = os.open(path, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)
    return derived_integrity


def cold_rebuild(
    repo: str | Path,
    *,
    state_dir: str | Path | None = None,
) -> dict[str, Any]:
    """Cold full validation, reducer parity, SQLite build, atomic activation."""

    started = time.perf_counter_ns()
    root = _repo(repo)
    output = _state_dir(root, state_dir)
    records, events, replay = _parse_ledger(root)
    reduced = replay["reduced"]
    commitment = replay["commitment"]
    build_material = {
        "schema": SCHEMA,
        "db_schema_version": DB_SCHEMA_VERSION,
        "ledger_commitment_sha256": commitment["commitment_sha256"],
        "reducer_sha256": _value_sha(reduced),
    }
    build_id = "RHS-" + _value_sha(build_material)[:24].upper()
    databases = output / "databases"
    databases.mkdir(parents=True, exist_ok=True, mode=0o700)
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{build_id}.", suffix=".sqlite3", dir=databases
    )
    os.close(descriptor)
    temporary = Path(temporary_name)
    target = databases / f"{build_id}.sqlite3"
    try:
        derived_integrity = _create_database(
            temporary,
            build_id=build_id,
            records=records,
            reduced=reduced,
            commitment=commitment,
        )
        os.replace(temporary, target)
        _fsync_dir(databases)
    finally:
        with contextlib.suppress(FileNotFoundError):
            temporary.unlink()
    manifest = {
        "schema": SCHEMA,
        "kind": "review_hot_state_manifest",
        "projection_build_id": build_id,
        "db_schema_version": DB_SCHEMA_VERSION,
        "database_ref": f"databases/{build_id}.sqlite3",
        "ledger_path_sha256": commitment["path_sha256"],
        "cold_ledger_commitment_sha256": commitment["commitment_sha256"],
        "cold_reducer_sha256": _value_sha(reduced),
        "cold_event_count": len(events),
        "cold_action_count": len(reduced["actions"]),
        "cold_action_formal_binding_count": len(
            reduced.get("action_formal_bindings", [])
        ),
        "cold_review_schedule_ensure_count": int(
            reduced.get("review_schedule_ensure_count", 0)
        ),
        "cold_formal_review_binding_count": int(
            reduced.get("formal_review_binding_count", 0)
        ),
        "cold_baseline_count": len(reduced["organic_baselines"]),
        "cold_retention_identity_count": len(reduced["retention"]),
        "generation": 0,
        "current_ledger_commitment_sha256": commitment["commitment_sha256"],
        "derived_integrity": derived_integrity,
        "answer_safe": True,
        "formal_write_count": 0,
    }
    _answer_safe(manifest)
    manifest_path, manifest_sha = _content_write(output / "manifests", manifest)
    pointer = {
        "schema": POINTER_SCHEMA,
        "kind": "review_hot_state_active_pointer",
        "manifest_ref": f"manifests/{manifest_sha}.json",
        "manifest_sha256": manifest_sha,
        "projection_build_id": build_id,
        "answer_safe": True,
        "formal_write_count": 0,
    }
    _atomic_write(output / "manifest.json", _json_bytes(pointer, pretty=True))
    return {
        "schema": SCHEMA,
        "status": "rebuilt",
        "projection_build_id": build_id,
        "active_manifest_ref": manifest_path.relative_to(output).as_posix(),
        "active_manifest_sha256": manifest_sha,
        "event_count": len(events),
        "action_count": len(reduced["actions"]),
        "action_formal_binding_count": len(
            reduced.get("action_formal_bindings", [])
        ),
        "review_schedule_ensure_count": int(
            reduced.get("review_schedule_ensure_count", 0)
        ),
        "formal_review_binding_count": int(
            reduced.get("formal_review_binding_count", 0)
        ),
        "baseline_count": len(reduced["organic_baselines"]),
        "retention_identity_count": len(reduced["retention"]),
        "cold_reducer_sha256": _value_sha(reduced),
        "ledger_commitment": commitment,
        "derived_integrity": derived_integrity,
        "elapsed_ms": round((time.perf_counter_ns() - started) / 1_000_000, 3),
        "ledger_full_scan_count": 1,
        "state_json_read_count": 0,
        "formal_table_read_count": 0,
        "formal_write_count": 0,
        "output_scope": "caller_selected_derived_projection",
    }


def _read_json_file(
    path: Path,
    *,
    expected_sha256: str | None,
    stats: dict[str, Any],
) -> dict[str, Any]:
    stats["file_read_attempt_count"] += 1
    try:
        raw = path.read_bytes()
    except OSError as exc:
        raise HotStateError("hot_state_artifact_missing") from exc
    stats["files_read"] += 1
    stats["bytes_read"] += len(raw)
    stats["paths_read"].append(path.name if path.name == "manifest.json" else f"{path.parent.name}/{path.name}")
    if expected_sha256 and _sha256_bytes(raw) != expected_sha256:
        raise HotStateError("hot_state_artifact_hash_drift")
    try:
        value = json.loads(raw)
    except (UnicodeError, json.JSONDecodeError) as exc:
        raise HotStateError("hot_state_artifact_invalid") from exc
    if not isinstance(value, dict):
        raise HotStateError("hot_state_artifact_invalid")
    return value


def _read_stats() -> dict[str, Any]:
    return {
        "file_read_attempt_count": 0,
        "files_read": 0,
        "bytes_read": 0,
        "paths_read": [],
        "sqlite_open_count": 0,
        "sqlite_query_count": 0,
        "ledger_stat_count": 0,
        "ledger_tail_bytes": 0,
        "ledger_full_scan_count": 0,
        "state_json_reads": 0,
        "formal_table_reads": 0,
        "derived_integrity_rows": 0,
        "derived_integrity_bytes": 0,
    }


def _validate_ledger_current(
    path: Path, commitment: dict[str, Any], stats: dict[str, Any]
) -> None:
    stats["ledger_stat_count"] += 1
    info = _regular_stat(path)
    for key, actual in (
        ("device", info.st_dev),
        ("inode", info.st_ino),
        ("size_bytes", info.st_size),
        ("mtime_ns", info.st_mtime_ns),
    ):
        if commitment.get(key) != actual:
            raise HotStateError("ledger_commitment_drift_rebuild_required")
    if commitment.get("indexed_offset") != info.st_size:
        raise HotStateError("ledger_commitment_drift_rebuild_required")
    start = commitment.get("tail_start_offset")
    if not isinstance(start, int) or not 0 <= start <= info.st_size:
        raise HotStateError("ledger_commitment_invalid")
    with path.open("rb") as handle:
        handle.seek(start)
        tail = handle.read(info.st_size - start)
    stats["ledger_tail_bytes"] += len(tail)
    if _sha256_bytes(tail) != commitment.get("tail_sha256"):
        raise HotStateError("ledger_tail_hash_drift_rebuild_required")
    core = dict(commitment)
    stored_sha = core.pop("commitment_sha256", None)
    if not SHA_RE.fullmatch(str(stored_sha or "")) or _value_sha(core) != stored_sha:
        raise HotStateError("ledger_commitment_invalid")


def _connect_readonly(path: Path, stats: dict[str, Any]) -> sqlite3.Connection:
    if not path.is_file() or path.is_symlink():
        raise HotStateError("hot_state_database_missing")
    try:
        connection = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    except sqlite3.Error as exc:
        raise HotStateError("hot_state_database_unreadable") from exc
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA query_only=ON")
    connection.execute("PRAGMA busy_timeout=1000")
    stats["sqlite_open_count"] += 1
    return connection


def _query_one(
    connection: sqlite3.Connection,
    sql: str,
    parameters: tuple[Any, ...],
    stats: dict[str, Any],
) -> sqlite3.Row | None:
    stats["sqlite_query_count"] += 1
    return connection.execute(sql, parameters).fetchone()


def _active(
    repo: Path,
    state_dir: Path,
    stats: dict[str, Any],
) -> tuple[dict[str, Any], dict[str, Any], sqlite3.Connection, dict[str, Any], Path]:
    pointer = _read_json_file(
        state_dir / "manifest.json", expected_sha256=None, stats=stats
    )
    if (
        pointer.get("schema") != POINTER_SCHEMA
        or pointer.get("kind") != "review_hot_state_active_pointer"
        or pointer.get("answer_safe") is not True
        or pointer.get("formal_write_count") != 0
    ):
        raise HotStateError("hot_state_pointer_invalid")
    manifest_sha = pointer.get("manifest_sha256")
    expected_ref = f"manifests/{manifest_sha}.json"
    if not SHA_RE.fullmatch(str(manifest_sha or "")) or pointer.get("manifest_ref") != expected_ref:
        raise HotStateError("hot_state_manifest_reference_invalid")
    manifest = _read_json_file(
        state_dir / expected_ref,
        expected_sha256=manifest_sha,
        stats=stats,
    )
    build_id = manifest.get("projection_build_id")
    db_ref = manifest.get("database_ref")
    if (
        manifest.get("schema") != SCHEMA
        or manifest.get("kind") != "review_hot_state_manifest"
        or manifest.get("answer_safe") is not True
        or manifest.get("formal_write_count") != 0
        or pointer.get("projection_build_id") != build_id
        or manifest.get("db_schema_version") != DB_SCHEMA_VERSION
        or db_ref != f"databases/{build_id}.sqlite3"
    ):
        raise HotStateError("hot_state_manifest_invalid")
    db_path = state_dir / str(db_ref)
    connection = _connect_readonly(db_path, stats)
    try:
        schema = _meta_get(connection, "schema")
        version = _meta_get(connection, "db_schema_version")
        actual_build = _meta_get(connection, "projection_build_id")
        commitment = _meta_get(connection, "ledger_commitment")
        generation = _meta_get(connection, "generation")
        derived_integrity = _verify_derived_integrity(connection, stats=stats)
    except Exception:
        connection.close()
        raise
    stats["sqlite_query_count"] += 5
    if schema != SCHEMA or version != DB_SCHEMA_VERSION or actual_build != build_id:
        connection.close()
        raise HotStateError("hot_state_database_identity_drift")
    if (
        manifest.get("generation") != generation
        or manifest.get("current_ledger_commitment_sha256")
        != commitment.get("commitment_sha256")
        or manifest.get("derived_integrity") != derived_integrity
    ):
        connection.close()
        raise HotStateError(
            "hot_state_manifest_database_commitment_drift_rebuild_required"
        )
    ledger_path = _ledger(repo)
    if manifest.get("ledger_path_sha256") != _sha256_bytes(
        str(ledger_path).encode("utf-8")
    ):
        connection.close()
        raise HotStateError("hot_state_ledger_identity_mismatch")
    try:
        _validate_ledger_current(ledger_path, commitment, stats)
    except Exception:
        connection.close()
        raise
    return pointer, manifest, connection, commitment, db_path


def _event_from_row(row: sqlite3.Row | None) -> dict[str, Any] | None:
    if row is None:
        return None
    try:
        event = json.loads(row["event_json"])
    except json.JSONDecodeError as exc:
        raise HotStateError("indexed_event_json_invalid") from exc
    if _value_sha(event) != row["event_sha256"]:
        raise HotStateError("indexed_event_hash_drift")
    return event


def _event_ref_from_row(row: sqlite3.Row | None) -> dict[str, Any] | None:
    if row is None:
        return None
    core = {
        "schema": EVENT_REF_SCHEMA,
        "event_id": row["event_id"],
        "idempotency_key": row["idempotency_key"],
        "event_kind": row["event_kind"],
        "source": row["source"],
        "session_id": row["session_id"],
        "item_id": row["item_id"],
        "identity": row["identity"],
        "observed_date": row["observed_date"],
        "event_sha256": row["event_sha256"],
        "line_sha256": row["line_sha256"],
        "byte_offset": int(row["byte_offset"]),
        "byte_length": int(row["byte_length"]),
        "line_number": int(row["line_number"]),
    }
    if (
        not SAFE_ID_RE.fullmatch(str(core["event_id"] or ""))
        or not SAFE_ID_RE.fullmatch(str(core["idempotency_key"] or ""))
        or not SHA_RE.fullmatch(str(core["event_sha256"] or ""))
        or not SHA_RE.fullmatch(str(core["line_sha256"] or ""))
        or core["byte_offset"] < 0
        or not 0 < core["byte_length"] <= MAX_EVENT_BYTES
        or core["line_number"] <= 0
    ):
        raise HotStateError("indexed_event_ref_invalid_rebuild_required")
    value = {**core, "ref_sha256": _value_sha(core)}
    _answer_safe(value)
    return value


def _morning_ref_without_duplicate(
    morning: dict[str, Any] | None,
    existing: dict[str, Any] | None,
) -> dict[str, Any] | None:
    if morning is None:
        return None
    if existing is None or morning["event_id"] != existing["event_id"]:
        return morning
    return {
        "schema": EVENT_ALIAS_SCHEMA,
        "event_id": morning["event_id"],
        "event_sha256": morning["event_sha256"],
        "alias_of": "existing_event_ref",
    }


def _action_from_row(row: sqlite3.Row | None) -> dict[str, Any] | None:
    if row is None:
        return None
    _verify_action_row(row)
    return json.loads(row["action_json"])


def _event_identity_candidates(event: dict[str, Any]) -> list[str]:
    """Return the canonical identities explicitly owned by one outcome.

    A queued original-question or node-specific review action is keyed by its
    formal node while the resulting outcome can also carry a review-unit id.
    The cold reducer already treats those fields as legitimate aliases for the
    same item.  Keep the hot path equally strict by accepting only identities
    carried by the exact event, rather than falling back to another action.
    """

    return sorted(
        {
            str(value)
            for value in (
                event.get("review_unit_id"),
                event.get("formal_node_id"),
                event.get("source_id"),
            )
            if isinstance(value, str) and value
        }
    )


def _identity_candidate_values(
    primary: str, candidates: list[str] | tuple[str, ...] | None
) -> list[str]:
    if candidates is not None and not isinstance(candidates, (list, tuple)):
        raise HotStateError("invalid_identity_candidates")
    raw = [primary] if candidates is None else list(candidates)
    if not raw or len(raw) > 4:
        raise HotStateError("invalid_identity_candidates")
    values = sorted(
        {
            _safe_identity(value, "identity_candidate")
            for value in raw
        }
    )
    if primary not in values:
        raise HotStateError("primary_identity_missing_from_candidates")
    return values


def _baseline_from_row(row: sqlite3.Row | None) -> dict[str, Any] | None:
    if row is None:
        return None
    _verify_baseline_row(row)
    return json.loads(row["baseline_json"])


def _retention_from_row(row: sqlite3.Row | None) -> dict[str, Any] | None:
    if row is None:
        return None
    _verify_retention_row(row)
    return {
        "identity": row["identity"],
        "retention_count": int(row["retention_count"]),
        "last_observed_date": row["last_observed_date"],
        "event_ids": json.loads(row["event_ids_json"]),
    }


def _safe_identity(value: str, label: str, *, safe_id: bool = False) -> str:
    compact = " ".join(str(value or "").split())
    if not compact or len(compact) > 512:
        raise HotStateError(f"invalid_{label}")
    if safe_id and not SAFE_ID_RE.fullmatch(compact):
        raise HotStateError(f"invalid_{label}")
    _answer_safe({label: compact})
    return compact


def _safe_date(value: str) -> str:
    try:
        return date.fromisoformat(value).isoformat()
    except ValueError as exc:
        raise HotStateError("invalid_observed_date") from exc


def _precondition(
    *,
    manifest: dict[str, Any],
    commitment: dict[str, Any],
    generation: int,
    request: dict[str, Any],
) -> dict[str, Any]:
    core = {
        "schema": PRECONDITION_SCHEMA,
        "projection_build_id": manifest["projection_build_id"],
        "generation": generation,
        "ledger_commitment": commitment,
        "request": request,
    }
    return {**core, "precondition_sha256": _value_sha(core)}


def begin_view(
    repo: str | Path,
    *,
    idempotency_key: str,
    session_id: str,
    item_id: str,
    identity: str,
    identity_candidates: list[str] | tuple[str, ...] | None = None,
    observed_date: str,
    event_kind: str = "outcome",
    repair_attempt: int | None = None,
    trigger_action_id: str | None = None,
    state_dir: str | Path | None = None,
    max_bytes: int = DEFAULT_MAX_VIEW_BYTES,
) -> dict[str, Any]:
    """Return the bounded exact state needed to construct one canonical event."""

    if not MIN_VIEW_BYTES <= max_bytes <= MAX_VIEW_BYTES:
        raise HotStateError("invalid_view_budget")
    started = time.perf_counter_ns()
    root = _repo(repo)
    output = _state_dir(root, state_dir)
    key = _safe_identity(idempotency_key, "idempotency_key", safe_id=True)
    session = _safe_identity(session_id, "session_id", safe_id=True)
    item = _safe_identity(item_id, "item_id", safe_id=True)
    identity_value = _safe_identity(identity, "identity")
    candidate_values = _identity_candidate_values(
        identity_value, identity_candidates
    )
    day = _safe_date(observed_date)
    if event_kind not in {"outcome", "repair"}:
        raise HotStateError("invalid_event_kind")
    if event_kind == "repair":
        if not isinstance(repair_attempt, int) or repair_attempt < 1:
            raise HotStateError("invalid_repair_attempt")
    elif repair_attempt is not None:
        raise HotStateError("repair_attempt_requires_repair_event")
    explicit_trigger = (
        _safe_identity(trigger_action_id, "trigger_action_id", safe_id=True)
        if trigger_action_id is not None
        else None
    )
    stats = _read_stats()
    _, manifest, connection, commitment, _ = _active(root, output, stats)
    try:
        existing_row = _query_one(
            connection,
            """
            SELECT event_id,idempotency_key,event_kind,source,session_id,item_id,
                   identity,observed_date,event_sha256,line_sha256,byte_offset,
                   byte_length,line_number
            FROM events WHERE idempotency_key=?
            """,
            (key,),
            stats,
        )
        morning_row = _query_one(
            connection,
            """
            SELECT event_id,idempotency_key,event_kind,source,session_id,item_id,
                   identity,observed_date,event_sha256,line_sha256,byte_offset,
                   byte_length,line_number
            FROM events
            WHERE source='morning_review' AND event_kind='outcome'
              AND session_id=? AND item_id=?
            ORDER BY line_number LIMIT 1
            """,
            (session, item),
            stats,
        )
        repair_count_row = _query_one(
            connection,
            """
            SELECT COUNT(*) AS repair_count FROM events
            WHERE source='morning_review' AND event_kind='repair'
              AND session_id=? AND item_id=?
            """,
            (session, item),
            stats,
        )
        if explicit_trigger is not None:
            trigger_row = _query_one(
                connection,
                "SELECT * FROM actions WHERE action_id=?",
                (explicit_trigger,),
                stats,
            )
        else:
            trigger_row = _query_one(
                connection,
                """
                SELECT * FROM actions
                WHERE identity=? AND status='pending'
                  AND action_type IN ('review','original_question') AND due_date<=?
                ORDER BY due_date, action_id LIMIT 1
                """,
                (identity_value, day),
                stats,
            )
        trigger = _action_from_row(trigger_row)
        scheduler_identity = identity_value
        if explicit_trigger is not None:
            if trigger is None:
                raise HotStateError("explicit_trigger_action_not_found")
            if trigger.get("identity") not in candidate_values:
                raise HotStateError("explicit_trigger_identity_mismatch")
            if trigger.get("action_type") not in {
                "review",
                "original_question",
            }:
                raise HotStateError("explicit_trigger_not_eligible")
            existing_event_id = (
                str(existing_row["event_id"]) if existing_row is not None else None
            )
            if existing_event_id is None:
                if trigger.get("status") != "pending":
                    raise HotStateError("explicit_trigger_not_eligible")
                try:
                    trigger_due = date.fromisoformat(str(trigger.get("due_date")))
                except ValueError as exc:
                    raise HotStateError("explicit_trigger_due_date_invalid") from exc
                if trigger_due > date.fromisoformat(day):
                    raise HotStateError("explicit_trigger_not_due")
            elif (
                trigger.get("status") != "completed"
                or trigger.get("completed_by_event_id") != existing_event_id
            ):
                raise HotStateError("explicit_trigger_not_eligible")
            scheduler_identity = str(trigger["identity"])
        prior_row = _query_one(
            connection,
            """
            SELECT 1 AS found FROM events
            WHERE identity=? AND event_kind='outcome' AND observed_date<? LIMIT 1
            """,
            (scheduler_identity, day),
            stats,
        )
        baseline_row = _query_one(
            connection,
            "SELECT * FROM baselines WHERE identity=?",
            (scheduler_identity,),
            stats,
        )
        retention_row = _query_one(
            connection,
            "SELECT * FROM retention WHERE identity=?",
            (scheduler_identity,),
            stats,
        )
        generation = int(_meta_get(connection, "generation"))
        stats["sqlite_query_count"] += 1
    finally:
        connection.close()
    baseline = _baseline_from_row(baseline_row)
    retention = _retention_from_row(retention_row)
    existing_ref = _event_ref_from_row(existing_row)
    morning_ref = _morning_ref_without_duplicate(
        _event_ref_from_row(morning_row), existing_ref
    )
    repair_count = int(repair_count_row["repair_count"])
    if event_kind == "repair":
        if morning_row is None:
            raise HotStateError("repair_requires_morning_outcome")
        expected_attempt = repair_count if existing_row is not None else repair_count + 1
        if repair_attempt != expected_attempt:
            raise HotStateError("repair_attempt_sequence_conflict")
    request = {
        "idempotency_key": key,
        "session_id": session,
        "item_id": item,
        "identity": identity_value,
        "identity_candidates": candidate_values,
        "scheduler_identity": scheduler_identity,
        "observed_date": day,
        "event_kind": event_kind,
    }
    if repair_attempt is not None:
        request["repair_attempt"] = repair_attempt
    if explicit_trigger is not None:
        request["trigger_action_id"] = explicit_trigger
    result = {
        "schema": SCHEMA,
        "status": "ready",
        "projection_build_id": manifest["projection_build_id"],
        "existing_event_ref": existing_ref,
        "same_morning_item_ref": morning_ref,
        "repair_count": repair_count,
        "eligible_trigger": trigger,
        "prior_cross_day": True if prior_row is not None else None,
        "has_baseline": baseline is not None,
        "retention_count": retention["retention_count"] if retention else 0,
        "precondition": _precondition(
            manifest=manifest,
            commitment=commitment,
            generation=generation,
            request=request,
        ),
        "read_only": True,
        "formal_write_count": 0,
        "scan_stats": stats,
        "elapsed_ms": round((time.perf_counter_ns() - started) / 1_000_000, 3),
    }
    _answer_safe(result)
    result["byte_budget"] = max_bytes
    result["byte_count"] = 0
    # The count includes its own decimal representation, so settle the small
    # self-reference before enforcing the hard envelope.
    for _ in range(8):
        actual_size = len(_json_bytes(result))
        if result["byte_count"] == actual_size:
            break
        result["byte_count"] = actual_size
    if result["byte_count"] > max_bytes:
        raise HotStateError("begin_view_exceeds_budget")
    return result


def safe_begin_view(*args: Any, **kwargs: Any) -> dict[str, Any]:
    try:
        return begin_view(*args, **kwargs)
    except HotStateError as exc:
        reason = str(exc)
    except (sqlite3.Error, OSError, UnicodeError):
        reason = "hot_state_storage_error_rebuild_required"
    return {
        "schema": SCHEMA,
        "status": "unavailable",
        "reason_code": reason,
        "rebuild_required": _readiness_rebuild_required(reason),
        "read_only": True,
        "formal_write_count": 0,
    }


def _readiness_rebuild_required(reason: str) -> bool:
    """Classify only projection failures that a cold rebuild can replace.

    The canonical ledger is evidence truth.  A missing or invalid ledger is not
    repairable by rebuilding its derived projection, while a missing active
    pointer, stale commitment, or damaged projection is.
    """

    if "rebuild_required" in reason:
        return True
    if reason in {
        "hot_state_active_pointer_disabled",
        "ledger_commitment_invalid",
        "ledger_tail_short_read",
    }:
        return True
    return reason.startswith(
        (
            "hot_state_artifact_",
            "hot_state_database_",
            "hot_state_ledger_identity_",
            "hot_state_manifest_",
            "hot_state_pointer_",
            "indexed_event_",
            "sqlite_action_",
            "sqlite_baseline_",
            "sqlite_derived_",
            "sqlite_meta_",
            "sqlite_retention_",
        )
    )


def readiness(
    repo: str | Path,
    *,
    state_dir: str | Path | None = None,
) -> dict[str, Any]:
    """Boundedly verify that the review projection can accept a later answer.

    This check never scans the full ledger and never repairs state.  In
    particular, a tracked ``manifest.json.disabled`` is diagnostic evidence,
    not an activation candidate: callers must cold-rebuild from the canonical
    ledger instead of renaming or trusting that pointer.
    """

    started = time.perf_counter_ns()
    stats = _read_stats()
    try:
        root = _repo(repo)
        output = _state_dir(root, state_dir)
        pointer_path = output / "manifest.json"
        if not os.path.lexists(pointer_path):
            reason = (
                "hot_state_active_pointer_disabled"
                if os.path.lexists(output / "manifest.json.disabled")
                else "hot_state_artifact_missing"
            )
            return {
                "schema": READINESS_SCHEMA,
                "status": "unavailable",
                "phase": BEFORE_LEARNER_ANSWER_PHASE,
                "reason_code": reason,
                "rebuild_required": True,
                "read_only": True,
                "answer_safe": True,
                "learner_evidence_write_count": 0,
                "formal_write_count": 0,
                "scan_stats": stats,
                "elapsed_ms": round(
                    (time.perf_counter_ns() - started) / 1_000_000, 3
                ),
            }
        pointer, manifest, connection, commitment, _ = _active(
            root, output, stats
        )
        connection.close()
    except HotStateError as exc:
        reason = str(exc)
        return {
            "schema": READINESS_SCHEMA,
            "status": "unavailable",
            "phase": BEFORE_LEARNER_ANSWER_PHASE,
            "reason_code": reason,
            "rebuild_required": _readiness_rebuild_required(reason),
            "read_only": True,
            "answer_safe": True,
            "learner_evidence_write_count": 0,
            "formal_write_count": 0,
            "scan_stats": stats,
            "elapsed_ms": round(
                (time.perf_counter_ns() - started) / 1_000_000, 3
            ),
        }
    except (sqlite3.Error, OSError, UnicodeError):
        reason = "hot_state_storage_error_rebuild_required"
        return {
            "schema": READINESS_SCHEMA,
            "status": "unavailable",
            "phase": BEFORE_LEARNER_ANSWER_PHASE,
            "reason_code": reason,
            "rebuild_required": True,
            "read_only": True,
            "answer_safe": True,
            "learner_evidence_write_count": 0,
            "formal_write_count": 0,
            "scan_stats": stats,
            "elapsed_ms": round(
                (time.perf_counter_ns() - started) / 1_000_000, 3
            ),
        }
    return {
        "schema": READINESS_SCHEMA,
        "status": "ready",
        "phase": BEFORE_LEARNER_ANSWER_PHASE,
        "rebuild_required": False,
        "projection_build_id": manifest["projection_build_id"],
        "active_manifest_sha256": pointer["manifest_sha256"],
        "current_ledger_commitment_sha256": commitment[
            "commitment_sha256"
        ],
        "read_only": True,
        "answer_safe": True,
        "learner_evidence_write_count": 0,
        "formal_write_count": 0,
        "scan_stats": stats,
        "elapsed_ms": round((time.perf_counter_ns() - started) / 1_000_000, 3),
    }


def prepare_study(
    repo: str | Path,
    *,
    state_dir: str | Path | None = None,
) -> dict[str, Any]:
    """Make review hot state ready before any learner answer is submitted.

    This is the only self-healing lifecycle entry point.  Answer writers and
    post-answer feedback paths must never call it: their failures stay
    fail-closed and return an exact recovery handle instead of replaying the
    canonical ledger while the learner waits.
    """

    started = time.perf_counter_ns()
    root = _repo(repo)
    with canonical.runtime_lock(root):
        initial = readiness(root, state_dir=state_dir)
        if initial.get("status") == "ready":
            return {
                "schema": READINESS_SCHEMA,
                "status": "ready",
                "phase": BEFORE_LEARNER_ANSWER_PHASE,
                "preparation_action": "already_ready",
                "rebuild_attempted": False,
                "rebuild_performed": False,
                "rebuild_required": False,
                "projection_build_id": initial["projection_build_id"],
                "active_manifest_sha256": initial[
                    "active_manifest_sha256"
                ],
                "current_ledger_commitment_sha256": initial[
                    "current_ledger_commitment_sha256"
                ],
                "ledger_full_scan_count": 0,
                "derived_projection_write_count": 0,
                "learner_evidence_write_count": 0,
                "formal_write_count": 0,
                "answer_safe": True,
                "elapsed_ms": round(
                    (time.perf_counter_ns() - started) / 1_000_000, 3
                ),
            }
        initial_reason = str(initial.get("reason_code") or "readiness_unavailable")
        if initial.get("rebuild_required") is not True:
            return {
                "schema": READINESS_SCHEMA,
                "status": "blocked",
                "phase": BEFORE_LEARNER_ANSWER_PHASE,
                "preparation_action": "not_repairable",
                "reason_code": initial_reason,
                "initial_reason_code": initial_reason,
                "rebuild_attempted": False,
                "rebuild_performed": False,
                "rebuild_required": False,
                "ledger_full_scan_count": 0,
                "derived_projection_write_count": 0,
                "learner_evidence_write_count": 0,
                "formal_write_count": 0,
                "answer_safe": True,
                "elapsed_ms": round(
                    (time.perf_counter_ns() - started) / 1_000_000, 3
                ),
            }
        try:
            rebuilt = cold_rebuild(root, state_dir=state_dir)
        except (HotStateError, sqlite3.Error, OSError, UnicodeError) as exc:
            failure_reason = str(exc)
            return {
                "schema": READINESS_SCHEMA,
                "status": "blocked",
                "phase": BEFORE_LEARNER_ANSWER_PHASE,
                "preparation_action": "cold_rebuild_failed",
                "reason_code": failure_reason,
                "initial_reason_code": initial_reason,
                "rebuild_attempted": True,
                "rebuild_performed": False,
                "rebuild_required": _readiness_rebuild_required(failure_reason),
                "ledger_full_scan_count": 0,
                "cold_rebuild_attempt_count": 1,
                "derived_projection_write_count": 0,
                "learner_evidence_write_count": 0,
                "formal_write_count": 0,
                "answer_safe": True,
                "elapsed_ms": round(
                    (time.perf_counter_ns() - started) / 1_000_000, 3
                ),
            }
        verified = readiness(root, state_dir=state_dir)
        if verified.get("status") != "ready":
            return {
                "schema": READINESS_SCHEMA,
                "status": "blocked",
                "phase": BEFORE_LEARNER_ANSWER_PHASE,
                "preparation_action": "cold_rebuild_verification_failed",
                "reason_code": str(
                    verified.get("reason_code") or "post_rebuild_unavailable"
                ),
                "initial_reason_code": initial_reason,
                "rebuild_attempted": True,
                "rebuild_performed": True,
                "rebuild_required": True,
                "ledger_full_scan_count": int(
                    rebuilt.get("ledger_full_scan_count", 1)
                ),
                "derived_projection_write_count": 1,
                "learner_evidence_write_count": 0,
                "formal_write_count": 0,
                "answer_safe": True,
                "elapsed_ms": round(
                    (time.perf_counter_ns() - started) / 1_000_000, 3
                ),
            }
    return {
        "schema": READINESS_SCHEMA,
        "status": "ready",
        "phase": BEFORE_LEARNER_ANSWER_PHASE,
        "preparation_action": "cold_rebuilt",
        "initial_reason_code": initial_reason,
        "rebuild_attempted": True,
        "rebuild_performed": True,
        "rebuild_required": False,
        "projection_build_id": verified["projection_build_id"],
        "active_manifest_sha256": verified["active_manifest_sha256"],
        "current_ledger_commitment_sha256": verified[
            "current_ledger_commitment_sha256"
        ],
        "ledger_full_scan_count": int(rebuilt.get("ledger_full_scan_count", 1)),
        "derived_projection_write_count": 1,
        "learner_evidence_write_count": 0,
        "formal_write_count": 0,
        "answer_safe": True,
        "elapsed_ms": round((time.perf_counter_ns() - started) / 1_000_000, 3),
    }


def lookup_event(
    repo: str | Path,
    *,
    event_id: str | None = None,
    idempotency_key: str | None = None,
    state_dir: str | Path | None = None,
) -> dict[str, Any]:
    if (event_id is None) == (idempotency_key is None):
        raise HotStateError("exactly_one_event_identity_required")
    root = _repo(repo)
    output = _state_dir(root, state_dir)
    stats = _read_stats()
    _, _, connection, _, _ = _active(root, output, stats)
    try:
        if event_id is not None:
            value = _safe_identity(event_id, "event_id", safe_id=True)
            row = _query_one(
                connection,
                "SELECT * FROM events WHERE event_id=?",
                (value,),
                stats,
            )
            kind = "event_id"
        else:
            value = _safe_identity(idempotency_key or "", "idempotency_key", safe_id=True)
            row = _query_one(
                connection,
                "SELECT * FROM events WHERE idempotency_key=?",
                (value,),
                stats,
            )
            kind = "idempotency_key"
        if row is None:
            return {
                "schema": SCHEMA,
                "status": "not_found",
                "query_kind": kind,
                "query_value": value,
                "read_only": True,
                "formal_write_count": 0,
                "scan_stats": stats,
            }
        ledger_path = _ledger(root)
        with ledger_path.open("rb") as handle:
            handle.seek(int(row["byte_offset"]))
            line = handle.read(int(row["byte_length"]))
        stats["bytes_read"] += len(line)
        stats["ledger_exact_range_reads"] = 1
        if _sha256_bytes(line) != row["line_sha256"]:
            raise HotStateError("indexed_event_line_hash_drift")
        event = _event_from_row(row)
        if event is None or _json_bytes(event) != line:
            raise HotStateError("indexed_event_line_content_drift")
        return {
            "schema": SCHEMA,
            "status": "found",
            "query_kind": kind,
            "query_value": value,
            "event": event,
            "event_sha256": row["event_sha256"],
            "line_sha256": row["line_sha256"],
            "byte_offset": row["byte_offset"],
            "byte_length": row["byte_length"],
            "read_only": True,
            "formal_write_count": 0,
            "scan_stats": stats,
        }
    finally:
        connection.close()


def lookup_commit_receipt(
    repo: str | Path,
    *,
    event_id: str | None = None,
    idempotency_key: str | None = None,
    state_dir: str | Path | None = None,
) -> dict[str, Any]:
    """Return one verified hot-append receipt without inventing cold history."""

    if (event_id is None) == (idempotency_key is None):
        raise HotStateError("exactly_one_event_identity_required")
    root = _repo(repo)
    output = _state_dir(root, state_dir)
    stats = _read_stats()
    _, manifest, connection, _, _ = _active(root, output, stats)
    try:
        if event_id is not None:
            value = _safe_identity(event_id, "event_id", safe_id=True)
            predicate = "e.event_id=?"
            query_kind = "event_id"
        else:
            value = _safe_identity(
                idempotency_key or "", "idempotency_key", safe_id=True
            )
            predicate = "e.idempotency_key=?"
            query_kind = "idempotency_key"
        row = _query_one(
            connection,
            f"""
            SELECT e.event_id,e.idempotency_key,e.event_sha256,
                   c.receipt_sha256,c.receipt_json,
                   c.idempotency_key AS receipt_idempotency_key
            FROM events e
            LEFT JOIN commit_receipts c ON c.event_id=e.event_id
            WHERE {predicate}
            """,
            (value,),
            stats,
        )
        if row is None:
            return {
                "schema": SCHEMA,
                "status": "not_found",
                "query_kind": query_kind,
                "query_value": value,
                "read_only": True,
                "formal_write_count": 0,
                "scan_stats": stats,
            }
        if row["receipt_sha256"] is None:
            return {
                "schema": SCHEMA,
                "status": "no_hot_commit_receipt",
                "query_kind": query_kind,
                "query_value": value,
                "event_id": row["event_id"],
                "event_sha256": row["event_sha256"],
                "reason": "event_entered_projection_through_cold_bootstrap",
                "receipt_ref": None,
                "receipt_sha256": None,
                "read_only": True,
                "formal_write_count": 0,
                "scan_stats": stats,
            }
        receipt_sha = str(row["receipt_sha256"])
        if not SHA_RE.fullmatch(receipt_sha):
            raise HotStateError("commit_receipt_row_invalid_rebuild_required")
        try:
            sqlite_receipt = json.loads(row["receipt_json"])
        except (TypeError, json.JSONDecodeError) as exc:
            raise HotStateError(
                "commit_receipt_row_json_invalid_rebuild_required"
            ) from exc
        sqlite_raw = _json_bytes(sqlite_receipt, pretty=True)
        if (
            _sha256_bytes(sqlite_raw) != receipt_sha
            or sqlite_raw.decode("utf-8").strip() != row["receipt_json"]
            or row["receipt_idempotency_key"] != row["idempotency_key"]
        ):
            raise HotStateError("commit_receipt_row_hash_drift_rebuild_required")
        receipt_ref = f"receipts/{receipt_sha}.json"
        file_receipt = _read_json_file(
            output / receipt_ref,
            expected_sha256=receipt_sha,
            stats=stats,
        )
        if file_receipt != sqlite_receipt:
            raise HotStateError("commit_receipt_file_row_drift_rebuild_required")
        if (
            file_receipt.get("schema") != RECEIPT_SCHEMA
            or file_receipt.get("kind") != "review_hot_state_commit_receipt"
            or file_receipt.get("projection_build_id")
            != manifest["projection_build_id"]
            or file_receipt.get("event_id") != row["event_id"]
            or file_receipt.get("event_sha256") != row["event_sha256"]
            or file_receipt.get("idempotency_key_sha256")
            != _sha256_bytes(str(row["idempotency_key"]).encode("utf-8"))
            or file_receipt.get("answer_safe") is not True
            or file_receipt.get("formal_write_count") != 0
        ):
            raise HotStateError("commit_receipt_identity_drift_rebuild_required")
        _answer_safe(file_receipt)
        return {
            "schema": SCHEMA,
            "status": "found",
            "query_kind": query_kind,
            "query_value": value,
            "event_id": row["event_id"],
            "event_sha256": row["event_sha256"],
            "receipt_ref": receipt_ref,
            "receipt_sha256": receipt_sha,
            "receipt": file_receipt,
            "read_only": True,
            "formal_write_count": 0,
            "scan_stats": stats,
        }
    finally:
        connection.close()


def _validate_precondition(value: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(value, dict) or value.get("schema") != PRECONDITION_SCHEMA:
        raise HotStateError("invalid_expected_precondition")
    core = dict(value)
    digest = core.pop("precondition_sha256", None)
    if not SHA_RE.fullmatch(str(digest or "")) or _value_sha(core) != digest:
        raise HotStateError("invalid_expected_precondition")
    return value


def _validate_append_event(event: dict[str, Any]) -> tuple[str, str, str]:
    if not isinstance(event, dict) or event.get("schema") != canonical.LEDGER_SCHEMA:
        raise HotStateError("invalid_append_event")
    event_kind = event.get("event_kind")
    if event_kind not in {"outcome", "repair"}:
        raise HotStateError("append_commit_accepts_outcome_or_repair_only")
    event_id = str(event.get("event_id") or "")
    key = str(event.get("idempotency_key") or "")
    if not SAFE_ID_RE.fullmatch(event_id) or not SAFE_ID_RE.fullmatch(key):
        raise HotStateError("invalid_append_event_identity")
    if event.get("formal_write_authorized") is not False:
        raise HotStateError("append_event_formal_write_boundary_invalid")
    if event_kind == "repair" and (
        event.get("correction_result")
        not in canonical.CORRECTION_RESULTS - {"not_attempted"}
        or event.get("evidence_kind") != "repair"
        or event.get("organic_baseline") is not False
        or event.get("stable_retention") is not False
        or event.get("generated_actions") != []
        or event.get("route")
        != {"kind": "repair_only", "formal_write_authorized": False}
        or not isinstance(event.get("attempt"), int)
        or int(event["attempt"]) < 1
    ):
        raise HotStateError("append_repair_event_shape_invalid")
    _answer_safe(event)
    # Reducer execution validates generated-action structure and collisions
    # inside the standalone event without scanning prior events.
    try:
        canonical._reduce([event])
    except Exception as exc:
        raise HotStateError("append_event_reducer_shape_invalid") from exc
    line = _json_bytes(event)
    if len(line) > MAX_EVENT_BYTES:
        raise HotStateError("append_event_exceeds_byte_limit")
    return event_id, key, _value_sha(event)


def _open_rw(db_path: Path) -> sqlite3.Connection:
    connection = sqlite3.connect(db_path)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA busy_timeout=1000")
    connection.execute("PRAGMA foreign_keys=ON")
    return connection


def _apply_incremental_event(connection: sqlite3.Connection, event: dict[str, Any]) -> None:
    trigger = event.get("trigger_action_id")
    if trigger:
        cursor = connection.execute(
            "SELECT action_json FROM actions WHERE action_id=?", (trigger,)
        )
        row = cursor.fetchone()
        if row is None:
            raise HotStateError("append_event_trigger_unknown")
        action = json.loads(row["action_json"])
        action["status"] = "completed"
        action["completed_by_event_id"] = event["event_id"]
        _insert_action(connection, action)
    identity = canonical._identity(event)
    if event.get("organic_baseline"):
        baseline = {
            "identity": identity,
            "event_id": event["event_id"],
            "observed_date": event["observed_date"],
            "review_unit_id": event.get("review_unit_id"),
            "formal_node_id": event.get("formal_node_id"),
            "kind": "organic_practice_baseline",
            "formal_mastery_modified": False,
        }
        connection.execute(
            "INSERT OR IGNORE INTO baselines VALUES(?,?,?,?,?)",
            _baseline_values(baseline),
        )
    if event.get("evidence_kind") == "retention":
        row = connection.execute(
            "SELECT retention_count,event_ids_json FROM retention WHERE identity=?",
            (identity,),
        ).fetchone()
        if row is None:
            event_ids = [event["event_id"]]
            connection.execute(
                "INSERT INTO retention VALUES(?,?,?,?,?)",
                _retention_values(
                    identity,
                    1,
                    event.get("observed_date"),
                    event_ids,
                ),
            )
        else:
            event_ids = json.loads(row["event_ids_json"])
            event_ids.append(event["event_id"])
            connection.execute(
                "INSERT OR REPLACE INTO retention VALUES(?,?,?,?,?)",
                _retention_values(
                    identity,
                    int(row["retention_count"]) + 1,
                    event.get("observed_date"),
                    event_ids,
                ),
            )
    for action in event.get("generated_actions", []):
        row = connection.execute(
            "SELECT dedupe_key,action_json FROM actions WHERE action_id=?",
            (action["action_id"],),
        ).fetchone()
        if row is None:
            _insert_action(connection, action)
        elif row["dedupe_key"] != action.get("dedupe_key"):
            raise HotStateError("append_event_action_identity_collision")
        else:
            existing = json.loads(row["action_json"])
            trigger_ids = existing.setdefault(
                "trigger_event_ids", [existing["trigger_event_id"]]
            )
            for trigger_id in action.get(
                "trigger_event_ids", [action["trigger_event_id"]]
            ):
                if trigger_id not in trigger_ids:
                    trigger_ids.append(trigger_id)
            _insert_action(connection, existing)


def _receipt_file(output: Path, payload: dict[str, Any]) -> tuple[str, str]:
    path, digest = _content_write(output / "receipts", payload)
    return path.relative_to(output).as_posix(), digest


def append_commit(
    repo: str | Path,
    *,
    event: dict[str, Any],
    expected_precondition: dict[str, Any],
    state_dir: str | Path | None = None,
    runtime_lock_held: bool = False,
    _crash_after_append: bool = False,
) -> dict[str, Any]:
    """Append+fsync then transactionally update SQLite under caller runtime lock."""

    if runtime_lock_held is not True:
        raise HotStateError("runtime_lock_must_be_held_by_caller")
    expected = _validate_precondition(expected_precondition)
    root = _repo(repo)
    output = _state_dir(root, state_dir)
    event_id, key, event_sha = _validate_append_event(event)
    stats = _read_stats()
    _, manifest, read_connection, commitment, db_path = _active(root, output, stats)
    try:
        existing = _query_one(
            read_connection,
            "SELECT event_id,event_sha256 FROM events WHERE idempotency_key=?",
            (key,),
            stats,
        )
        if existing is not None:
            if existing["event_sha256"] != event_sha:
                raise HotStateError("idempotency_key_reused_with_different_event")
            receipt_row = _query_one(
                read_connection,
                "SELECT receipt_sha256,receipt_json FROM commit_receipts WHERE event_id=?",
                (existing["event_id"],),
                stats,
            )
            if receipt_row is None:
                raise HotStateError("committed_event_receipt_missing_rebuild_required")
            payload = json.loads(receipt_row["receipt_json"])
            ref, digest = _receipt_file(output, payload)
            if digest != receipt_row["receipt_sha256"]:
                raise HotStateError("committed_event_receipt_hash_drift")
            return {
                "schema": SCHEMA,
                "status": "already_committed",
                "event_id": existing["event_id"],
                "event_sha256": event_sha,
                "receipt_ref": ref,
                "receipt_sha256": digest,
                "formal_write_count": 0,
            }
        morning = _query_one(
            read_connection,
            """
            SELECT event_id FROM events WHERE source='morning_review'
              AND event_kind='outcome' AND session_id=? AND item_id=? LIMIT 1
            """,
            (event.get("session_id"), event.get("item_id")),
            stats,
        )
        repair_count_row = _query_one(
            read_connection,
            """
            SELECT COUNT(*) AS repair_count FROM events
            WHERE source='morning_review' AND event_kind='repair'
              AND session_id=? AND item_id=?
            """,
            (event.get("session_id"), event.get("item_id")),
            stats,
        )
        if (
            event.get("event_kind") == "outcome"
            and event.get("source") == "morning_review"
            and morning is not None
        ):
            raise HotStateError("morning_item_already_has_canonical_outcome")
        if event.get("event_kind") == "repair":
            if event.get("source") != "morning_review" or morning is None:
                raise HotStateError("repair_requires_morning_outcome")
            if int(event.get("attempt") or 0) != int(
                repair_count_row["repair_count"]
            ) + 1:
                raise HotStateError("repair_attempt_sequence_conflict")
        generation = int(_meta_get(read_connection, "generation"))
        stats["sqlite_query_count"] += 1
    finally:
        read_connection.close()
    request = expected.get("request") or {}
    actual_precondition = _precondition(
        manifest=manifest,
        commitment=commitment,
        generation=generation,
        request=request,
    )
    if actual_precondition != expected:
        raise HotStateError("append_precondition_conflict")
    if (
        event.get("event_kind") != request.get("event_kind")
        or (
            event.get("event_kind") == "repair"
            and event.get("attempt") != request.get("repair_attempt")
        )
        or event.get("idempotency_key") != request.get("idempotency_key")
        or event.get("session_id") != request.get("session_id")
        or event.get("item_id") != request.get("item_id")
        or canonical._identity(event) != request.get("identity")
        or _event_identity_candidates(event)
        != request.get("identity_candidates")
        or event.get("observed_date") != request.get("observed_date")
        or (
            request.get("trigger_action_id") is not None
            and event.get("trigger_action_id") != request.get("trigger_action_id")
        )
    ):
        raise HotStateError("append_event_does_not_match_begin_view")

    ledger_path = _ledger(root)
    line = _json_bytes(event)
    old_size = int(commitment["size_bytes"])
    with ledger_path.open("ab") as handle:
        handle.write(line)
        handle.flush()
        os.fsync(handle.fileno())
    note_ledger_commit(ledger_path, event)
    if _crash_after_append:
        raise SimulatedHotStateCrash("simulated_crash_after_append_before_sqlite")

    line_sha = _sha256_bytes(line)
    info = _regular_stat(ledger_path)
    new_chain = _chain_step(
        commitment["event_chain_sha256"], line_sha, old_size, info.st_size
    )
    post_commitment = _ledger_commitment(
        ledger_path,
        event_count=int(commitment["event_count"]) + 1,
        event_chain_sha256=new_chain,
        full_sha256=None,
        full_hash_scope="incremental_event_chain_requires_cold_full_hash_for_audit",
        last_event_id=event_id,
        last_line_sha256=line_sha,
    )
    record = {
        "event_id": event_id,
        "idempotency_key": key,
        "event_kind": event.get("event_kind"),
        "source": event.get("source"),
        "session_id": event.get("session_id"),
        "item_id": event.get("item_id"),
        "identity": canonical._identity(event),
        "observed_date": event.get("observed_date"),
        "evidence_kind": event.get("evidence_kind"),
        "event_json": canonical.canonical_json(event),
        "event_sha256": event_sha,
        "line_sha256": line_sha,
        "byte_offset": old_size,
        "byte_length": len(line),
        "line_number": int(commitment["event_count"]) + 1,
    }
    connection = _open_rw(db_path)
    try:
        connection.execute("BEGIN IMMEDIATE")
        current_commitment = _meta_get(connection, "ledger_commitment")
        current_generation = int(_meta_get(connection, "generation"))
        if current_commitment != commitment or current_generation != generation:
            raise HotStateError("sqlite_precondition_changed_during_append")
        _insert_record(connection, record)
        _apply_incremental_event(connection, event)
        _meta_set(connection, "ledger_commitment", post_commitment)
        _meta_set(connection, "generation", generation + 1)
        post_integrity = _compute_derived_integrity(connection)
        _meta_set(connection, "derived_integrity", post_integrity)
        receipt_core = {
            "schema": RECEIPT_SCHEMA,
            "kind": "review_hot_state_commit_receipt",
            "projection_build_id": manifest["projection_build_id"],
            "generation": generation + 1,
            "event_id": event_id,
            "event_sha256": event_sha,
            "idempotency_key_sha256": _sha256_bytes(key.encode("utf-8")),
            "byte_offset": old_size,
            "byte_length": len(line),
            "line_sha256": line_sha,
            "precondition_sha256": expected["precondition_sha256"],
            "post_ledger_commitment_sha256": post_commitment["commitment_sha256"],
            "post_derived_integrity_sha256": post_integrity["commitment_sha256"],
            "answer_safe": True,
            "formal_write_count": 0,
        }
        _answer_safe(receipt_core)
        receipt_raw = _json_bytes(receipt_core, pretty=True)
        receipt_sha = _sha256_bytes(receipt_raw)
        connection.execute(
            "INSERT INTO commit_receipts VALUES(?,?,?,?)",
            (
                receipt_sha,
                event_id,
                key,
                receipt_raw.decode("utf-8").strip(),
            ),
        )
        connection.commit()
    except Exception:
        connection.rollback()
        raise
    finally:
        connection.close()
    next_manifest = dict(manifest)
    next_manifest["generation"] = generation + 1
    next_manifest["current_ledger_commitment_sha256"] = post_commitment[
        "commitment_sha256"
    ]
    next_manifest["derived_integrity"] = post_integrity
    _answer_safe(next_manifest)
    next_manifest_path, next_manifest_sha = _content_write(
        output / "manifests", next_manifest
    )
    receipt_path = output / "receipts" / f"{receipt_sha}.json"
    if receipt_path.exists():
        if receipt_path.read_bytes() != receipt_raw:
            raise HotStateError("commit_receipt_content_conflict")
    else:
        _atomic_write(receipt_path, receipt_raw)
    next_pointer = {
        "schema": POINTER_SCHEMA,
        "kind": "review_hot_state_active_pointer",
        "manifest_ref": next_manifest_path.relative_to(output).as_posix(),
        "manifest_sha256": next_manifest_sha,
        "projection_build_id": manifest["projection_build_id"],
        "answer_safe": True,
        "formal_write_count": 0,
    }
    # The mutable pointer is the final publication marker.  A crash before
    # this write leaves the prior generation visibly stale/fail-closed, never
    # an active generation whose receipt file is still missing.
    _atomic_write(output / "manifest.json", _json_bytes(next_pointer, pretty=True))
    return {
        "schema": SCHEMA,
        "status": "committed",
        "event_id": event_id,
        "event_sha256": event_sha,
        "receipt_ref": receipt_path.relative_to(output).as_posix(),
        "receipt_sha256": receipt_sha,
        "generation": generation + 1,
        "active_manifest_sha256": next_manifest_sha,
        "derived_integrity_sha256": post_integrity["commitment_sha256"],
        "ledger_size_bytes": post_commitment["size_bytes"],
        "formal_write_count": 0,
    }


def verify_commit(
    repo: str | Path,
    *,
    receipt_sha256: str,
    state_dir: str | Path | None = None,
    allow_historical_projection: bool = False,
) -> dict[str, Any]:
    """Bounded verification of receipt, SQLite row, and exact ledger bytes."""

    if not SHA_RE.fullmatch(receipt_sha256):
        raise HotStateError("invalid_commit_receipt_sha256")
    root = _repo(repo)
    output = _state_dir(root, state_dir)
    stats = _read_stats()
    _, manifest, connection, _, _ = _active(root, output, stats)
    try:
        receipt_path = output / "receipts" / f"{receipt_sha256}.json"
        receipt = _read_json_file(
            receipt_path, expected_sha256=receipt_sha256, stats=stats
        )
        historical_projection = receipt.get("projection_build_id") != manifest["projection_build_id"]
        if (
            receipt.get("schema") != RECEIPT_SCHEMA
            or receipt.get("kind") != "review_hot_state_commit_receipt"
            or (historical_projection and not allow_historical_projection)
            or not receipt.get("projection_build_id")
            or receipt.get("answer_safe") is not True
            or receipt.get("formal_write_count") != 0
        ):
            raise HotStateError("commit_receipt_invalid")
        if historical_projection:
            # Cold history verification uses the current canonical event index,
            # while retaining the old immutable receipt and its exact byte range.
            # A rebuild may discard derived commit_receipts rows, never the event.
            row = _query_one(
                connection,
                "SELECT event_id,event_sha256,line_sha256 FROM events WHERE event_id=?",
                (receipt.get("event_id"),), stats,
            )
        else:
            row = _query_one(
                connection,
                """
            SELECT e.event_id,e.event_sha256,e.line_sha256,
                   c.receipt_json,c.receipt_sha256
            FROM commit_receipts c JOIN events e ON e.event_id=c.event_id
            WHERE c.receipt_sha256=?
            """,
                (receipt_sha256,),
                stats,
            )
        if row is None:
            raise HotStateError("commit_receipt_not_indexed")
        if not historical_projection and _sha256_bytes((row["receipt_json"] + "\n").encode("utf-8")) != receipt_sha256:
            raise HotStateError("commit_receipt_sqlite_hash_drift")
        with _ledger(root).open("rb") as handle:
            handle.seek(int(receipt["byte_offset"]))
            line = handle.read(int(receipt["byte_length"]))
        stats["ledger_exact_range_reads"] = 1
        stats["bytes_read"] += len(line)
        if (
            _sha256_bytes(line) != receipt.get("line_sha256")
            or _sha256_bytes(line) != row["line_sha256"]
        ):
            raise HotStateError("commit_ledger_range_hash_drift")
        event = json.loads(line)
        if (
            event.get("event_id") != receipt.get("event_id")
            or _value_sha(event) != receipt.get("event_sha256")
            or _value_sha(event) != row["event_sha256"]
            or _sha256_bytes(str(event.get("idempotency_key") or "").encode("utf-8")) != receipt.get("idempotency_key_sha256")
        ):
            raise HotStateError("commit_event_identity_drift")
    finally:
        connection.close()
    return {
        "schema": SCHEMA,
        "status": "verified",
        "historical_projection": historical_projection,
        "receipt_sha256": receipt_sha256,
        "event_id": receipt["event_id"],
        "event_sha256": receipt["event_sha256"],
        "byte_offset": receipt["byte_offset"],
        "byte_length": receipt["byte_length"],
        "read_only": True,
        "formal_write_count": 0,
        "scan_stats": stats,
    }


def _read_json_argument(path: str) -> dict[str, Any]:
    try:
        raw = sys.stdin.buffer.read() if path == "-" else Path(path).read_bytes()
        value = json.loads(raw)
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise HotStateError("input_json_invalid") from exc
    if not isinstance(value, dict):
        raise HotStateError("input_json_invalid")
    return value


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo", type=Path, default=Path.cwd())
    parser.add_argument("--state-dir", type=Path)
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("rebuild")
    sub.add_parser(
        "readiness",
        help="Boundedly diagnose pre-study readiness without rebuilding",
    )
    sub.add_parser(
        "prepare-study",
        help="Cold-rebuild a missing/stale projection before a learner answer",
    )
    begin = sub.add_parser("begin-view")
    begin.add_argument("--idempotency-key", required=True)
    begin.add_argument("--session-id", required=True)
    begin.add_argument("--item-id", required=True)
    begin.add_argument("--identity", required=True)
    begin.add_argument("--observed-date", required=True)
    begin.add_argument("--event-kind", choices=("outcome", "repair"), default="outcome")
    begin.add_argument("--repair-attempt", type=int)
    begin.add_argument("--trigger-action-id")
    begin.add_argument("--max-bytes", type=int, default=DEFAULT_MAX_VIEW_BYTES)
    lookup = sub.add_parser("lookup")
    lookup_identity = lookup.add_mutually_exclusive_group(required=True)
    lookup_identity.add_argument("--event-id")
    lookup_identity.add_argument("--idempotency-key")
    lookup_receipt = sub.add_parser("lookup-receipt")
    receipt_identity = lookup_receipt.add_mutually_exclusive_group(required=True)
    receipt_identity.add_argument("--event-id")
    receipt_identity.add_argument("--idempotency-key")
    append = sub.add_parser("append")
    append.add_argument("--event-json", required=True)
    append.add_argument("--precondition-json", required=True)
    verify = sub.add_parser("verify")
    verify.add_argument("--receipt-sha256", required=True)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        if args.command == "rebuild":
            result = cold_rebuild(args.repo, state_dir=args.state_dir)
        elif args.command == "readiness":
            result = readiness(args.repo, state_dir=args.state_dir)
        elif args.command == "prepare-study":
            result = prepare_study(args.repo, state_dir=args.state_dir)
        elif args.command == "begin-view":
            result = safe_begin_view(
                args.repo,
                idempotency_key=args.idempotency_key,
                session_id=args.session_id,
                item_id=args.item_id,
                identity=args.identity,
                observed_date=args.observed_date,
                event_kind=args.event_kind,
                repair_attempt=args.repair_attempt,
                trigger_action_id=args.trigger_action_id,
                state_dir=args.state_dir,
                max_bytes=args.max_bytes,
            )
        elif args.command == "lookup":
            result = lookup_event(
                args.repo,
                event_id=args.event_id,
                idempotency_key=args.idempotency_key,
                state_dir=args.state_dir,
            )
        elif args.command == "lookup-receipt":
            result = lookup_commit_receipt(
                args.repo,
                event_id=args.event_id,
                idempotency_key=args.idempotency_key,
                state_dir=args.state_dir,
            )
        elif args.command == "append":
            with canonical.runtime_lock(_repo(args.repo)):
                result = append_commit(
                    args.repo,
                    event=_read_json_argument(args.event_json),
                    expected_precondition=_read_json_argument(args.precondition_json),
                    state_dir=args.state_dir,
                    runtime_lock_held=True,
                )
        else:
            result = verify_commit(
                args.repo,
                receipt_sha256=args.receipt_sha256,
                state_dir=args.state_dir,
            )
    except (HotStateError, sqlite3.Error, OSError) as exc:
        result = {
            "schema": SCHEMA,
            "status": "error",
            "reason_code": str(exc),
            "formal_write_count": 0,
        }
    print(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True))
    return 0 if result.get("status") in {
        "rebuilt",
        "ready",
        "found",
        "not_found",
        "no_hot_commit_receipt",
        "committed",
        "already_committed",
        "verified",
    } else 2


if __name__ == "__main__":
    raise SystemExit(main())
