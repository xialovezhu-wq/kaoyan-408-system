#!/usr/bin/env python3
"""Content-addressed commit receipts for the 408 fast-capture ledger.

This module is deliberately sidecar-only.  It never appends capture events, never
changes the capture state, and never writes the formal wrong-question database.
The current capture writer can integrate the pure ``build_append_receipt`` entry
point later while holding its own writer lock.  Until then, ``cold_rebuild`` and
``cold_audit`` provide a deterministic migration and verification path.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sys
import tempfile
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence


INDEX_SCHEMA = "capture_commit_index_v1"
INDEX_KIND = "capture_fact_commit_index"
RECEIPT_SCHEMA = "capture_commit_receipt_v1"
RECEIPT_KIND = "capture_fact_event_commit"
EVENT_SCHEMA = "intake_fact_capture_event_v1"
STATE_SCHEMA = "intake_fact_capture_state_v2"
CHAIN_SCHEMA = "capture_commit_event_chain_v1"
PENDING_FORMAL_STATUS = "awaiting_sol_formalization"
STATE_SERIALIZATION = "json-sort-keys-indent-2-utf8-lf-v1"

INDEX_FILE = "index.json"
RECEIPT_DIR = "receipts"
GENESIS_CHAIN_SHA256 = hashlib.sha256(
    b"capture_commit_event_chain_v1:genesis"
).hexdigest()

SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
LOOKUP_KINDS = {"capture_id", "event_id", "idempotency_key"}

DEFAULT_MAX_LEDGER_BYTES = 64 * 1024 * 1024
DEFAULT_MAX_EVENT_BYTES = 256 * 1024
DEFAULT_MAX_STATE_BYTES = 16 * 1024 * 1024
DEFAULT_MAX_INDEX_BYTES = 32 * 1024 * 1024
DEFAULT_MAX_RECEIPT_BYTES = 64 * 1024


class CommitIndexError(RuntimeError):
    """The commit-index contract or one of its bindings failed closed."""


StateReplayer = Callable[[list[dict[str, Any]]], dict[str, Any]]


def _canonical(value: Any) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


def _sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _sha256_value(value: Any) -> str:
    return _sha256_bytes(_canonical(value))


def _serialize_state(state: Mapping[str, Any]) -> bytes:
    return (
        json.dumps(state, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    ).encode("utf-8")


def _require_sha(value: object, label: str) -> str:
    text = str(value or "")
    if not SHA256_RE.fullmatch(text):
        raise CommitIndexError(f"{label} must be a lowercase SHA-256")
    return text


def _require_nonempty(value: object, label: str) -> str:
    text = str(value or "").strip()
    if not text:
        raise CommitIndexError(f"{label} must be non-empty")
    return text


def _read_bounded(path: Path, *, limit: int, label: str) -> bytes:
    try:
        size = path.stat().st_size
    except OSError as exc:
        raise CommitIndexError(f"{label} is unavailable: {path}") from exc
    if size > limit:
        raise CommitIndexError(
            f"{label} exceeds the bounded read limit: {size} > {limit}"
        )
    try:
        value = path.read_bytes()
    except OSError as exc:
        raise CommitIndexError(f"failed to read {label}: {path}") from exc
    if len(value) != size:
        raise CommitIndexError(f"{label} changed while being read")
    return value


def _parse_json_bytes(raw: bytes, label: str) -> Any:
    try:
        return json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise CommitIndexError(f"{label} is not valid UTF-8 JSON") from exc


def _resolve_replayer(replayer: StateReplayer | None) -> StateReplayer:
    if replayer is not None:
        return replayer
    try:
        from intake_fact_capture_408 import replay as capture_replay
    except (ImportError, AttributeError) as exc:
        raise CommitIndexError(
            "capture replay dependency is unavailable; pass state_replayer explicitly"
        ) from exc
    return capture_replay


def _validate_fact_event(event: Mapping[str, Any]) -> dict[str, str]:
    if event.get("schema") != EVENT_SCHEMA:
        raise CommitIndexError("capture event schema mismatch")
    if event.get("event_type") != "fact_captured":
        raise CommitIndexError("commit receipts are only issued for fact_captured")
    capture_id = _require_nonempty(event.get("capture_id"), "capture_id")
    event_id = _require_nonempty(event.get("event_id"), "event_id")
    idempotency_key = _require_nonempty(
        event.get("idempotency_key"), "idempotency_key"
    )
    payload_sha256 = _require_sha(
        event.get("payload_sha256"), "payload_sha256"
    )
    capture = event.get("capture")
    if not isinstance(capture, dict):
        raise CommitIndexError("fact_captured.capture must be an object")
    if _sha256_value(capture) != payload_sha256:
        raise CommitIndexError("fact_captured payload hash mismatch")
    if str(capture.get("idempotency_key") or "") != idempotency_key:
        raise CommitIndexError(
            "event and capture payload use different idempotency keys"
        )
    return {
        "capture_id": capture_id,
        "event_id": event_id,
        "idempotency_key": idempotency_key,
        "payload_sha256": payload_sha256,
    }


def _capture_state_projection(
    state: Mapping[str, Any], capture_id: str
) -> dict[str, Any]:
    captures = state.get("captures")
    if not isinstance(captures, dict):
        raise CommitIndexError("post-event state has no captures map")
    value = captures.get(capture_id)
    if not isinstance(value, dict):
        raise CommitIndexError("post-event state does not contain the captured fact")
    return value


def _event_chain(
    *,
    previous_sha256: str,
    event_sha256: str,
    byte_offset: int,
    byte_length: int,
    event_ordinal: int,
) -> str:
    _require_sha(previous_sha256, "previous event-chain commitment")
    _require_sha(event_sha256, "event_sha256")
    if byte_offset < 0 or byte_length <= 0 or event_ordinal <= 0:
        raise CommitIndexError("event-chain position fields are invalid")
    return _sha256_value(
        {
            "schema": CHAIN_SCHEMA,
            "previous_sha256": previous_sha256,
            "event_sha256": event_sha256,
            "byte_offset": byte_offset,
            "byte_length": byte_length,
            "event_ordinal": event_ordinal,
        }
    )


def _attach_receipt_digest(material: dict[str, Any]) -> dict[str, Any]:
    digest = _sha256_value(material)
    return {
        **material,
        "receipt_id": f"CCR-{digest[:24]}",
        "receipt_sha256": digest,
    }


def _validate_receipt(receipt: Any) -> dict[str, Any]:
    if not isinstance(receipt, dict):
        raise CommitIndexError("commit receipt must be an object")
    if receipt.get("schema") != RECEIPT_SCHEMA or receipt.get("kind") != RECEIPT_KIND:
        raise CommitIndexError("commit receipt schema or kind mismatch")
    digest = _require_sha(receipt.get("receipt_sha256"), "receipt_sha256")
    if receipt.get("receipt_id") != f"CCR-{digest[:24]}":
        raise CommitIndexError("commit receipt ID does not match its digest")
    material = {
        key: value
        for key, value in receipt.items()
        if key not in {"receipt_id", "receipt_sha256"}
    }
    if _sha256_value(material) != digest:
        raise CommitIndexError("commit receipt content address mismatch")
    if receipt.get("formal_write_count") != 0:
        raise CommitIndexError("commit receipt attempted to claim a formal write")
    _validate_receipt_shape(receipt)
    return receipt


def _validate_receipt_shape(receipt: Mapping[str, Any]) -> None:
    for key in (
        "capture_id",
        "event_id",
        "idempotency_key",
        "payload_sha256",
        "event_sha256",
    ):
        if key.endswith("sha256"):
            _require_sha(receipt.get(key), key)
        else:
            _require_nonempty(receipt.get(key), key)
    for key in ("byte_offset", "byte_length", "event_ordinal"):
        value = receipt.get(key)
        if not isinstance(value, int) or isinstance(value, bool):
            raise CommitIndexError(f"receipt {key} must be an integer")
    if receipt["byte_offset"] < 0 or receipt["byte_length"] <= 0:
        raise CommitIndexError("receipt byte range is invalid")
    if receipt["event_ordinal"] <= 0:
        raise CommitIndexError("receipt event ordinal is invalid")
    ledger = receipt.get("ledger_commitment")
    state = receipt.get("state_commitment")
    if not isinstance(ledger, dict) or not isinstance(state, dict):
        raise CommitIndexError("receipt commitments are missing")
    if ledger.get("algorithm") != CHAIN_SCHEMA:
        raise CommitIndexError("receipt ledger commitment algorithm mismatch")
    for key in ("previous_sha256", "sha256"):
        _require_sha(ledger.get(key), f"ledger_commitment.{key}")
    if ledger.get("event_end_offset") != receipt["byte_offset"] + receipt["byte_length"]:
        raise CommitIndexError("receipt event end offset mismatch")
    if state.get("serialization") != STATE_SERIALIZATION:
        raise CommitIndexError("receipt state serialization mismatch")
    for key in ("post_event_state_sha256", "capture_state_sha256"):
        _require_sha(state.get(key), f"state_commitment.{key}")
    if state.get("post_event_event_count") != receipt["event_ordinal"]:
        raise CommitIndexError("receipt post-event state count mismatch")


def build_append_receipt(
    *,
    event_bytes: bytes,
    byte_offset: int,
    event_ordinal: int,
    previous_chain_sha256: str,
    post_event_state: Mapping[str, Any],
) -> dict[str, Any]:
    """Build a receipt immediately after one fact event and state are durable.

    This is a pure integration surface.  The caller owns the writer lock and
    supplies the exact JSONL bytes, their committed offset, the previous chain
    commitment, and the replayed post-event state.  No file is changed here.
    """

    if not isinstance(event_bytes, bytes) or not event_bytes.endswith(b"\n"):
        raise CommitIndexError("event_bytes must be one LF-terminated JSONL record")
    if b"\n" in event_bytes[:-1] or event_bytes.endswith(b"\r\n"):
        raise CommitIndexError("event_bytes must contain exactly one canonical LF line")
    event = _parse_json_bytes(event_bytes, "capture event")
    if not isinstance(event, dict):
        raise CommitIndexError("capture event must be an object")
    identity = _validate_fact_event(event)
    if not isinstance(post_event_state, Mapping):
        raise CommitIndexError("post_event_state must be an object")
    if post_event_state.get("schema") != STATE_SCHEMA:
        raise CommitIndexError("post-event state schema mismatch")
    if post_event_state.get("event_count") != event_ordinal:
        raise CommitIndexError("post-event state count does not match event ordinal")

    capture_state = _capture_state_projection(
        post_event_state, identity["capture_id"]
    )
    if capture_state.get("capture_id") != identity["capture_id"]:
        raise CommitIndexError("capture state projection has a different identity")
    if capture_state.get("payload_sha256") != identity["payload_sha256"]:
        raise CommitIndexError("capture state projection has a different payload hash")
    if capture_state.get("capture") != event.get("capture"):
        raise CommitIndexError("capture state projection differs from the event payload")

    event_sha256 = _sha256_bytes(event_bytes)
    chain_sha256 = _event_chain(
        previous_sha256=previous_chain_sha256,
        event_sha256=event_sha256,
        byte_offset=byte_offset,
        byte_length=len(event_bytes),
        event_ordinal=event_ordinal,
    )
    post_state_bytes = _serialize_state(post_event_state)
    material = {
        "schema": RECEIPT_SCHEMA,
        "kind": RECEIPT_KIND,
        **identity,
        "event_sha256": event_sha256,
        "byte_offset": byte_offset,
        "byte_length": len(event_bytes),
        "event_ordinal": event_ordinal,
        "ledger_commitment": {
            "algorithm": CHAIN_SCHEMA,
            "previous_sha256": previous_chain_sha256,
            "sha256": chain_sha256,
            "event_end_offset": byte_offset + len(event_bytes),
        },
        "state_commitment": {
            "serialization": STATE_SERIALIZATION,
            "post_event_state_sha256": _sha256_bytes(post_state_bytes),
            "post_event_event_count": event_ordinal,
            "capture_state_sha256": _sha256_value(capture_state),
        },
        "verification_boundary": {
            "answer_safe_metadata_only": True,
            "full_question_stored": False,
            "formal_write_authorized": False,
            "formal_database_touched": False,
        },
        "formal_write_count": 0,
    }
    return _attach_receipt_digest(material)


def _parse_ledger(
    raw: bytes, *, max_event_bytes: int
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], str]:
    if raw and not raw.endswith(b"\n"):
        raise CommitIndexError("capture ledger must end with LF")
    events: list[dict[str, Any]] = []
    metadata: list[dict[str, Any]] = []
    event_ids: set[str] = set()
    idempotency_keys: set[str] = set()
    capture_ids: set[str] = set()
    offset = 0
    previous_chain = GENESIS_CHAIN_SHA256
    for ordinal, line in enumerate(raw.splitlines(keepends=True), 1):
        if not line.strip():
            raise CommitIndexError(f"capture ledger line {ordinal} is blank")
        if len(line) > max_event_bytes:
            raise CommitIndexError(
                f"capture ledger line {ordinal} exceeds the event bound"
            )
        if not line.endswith(b"\n") or line.endswith(b"\r\n"):
            raise CommitIndexError(
                f"capture ledger line {ordinal} is not canonical LF JSONL"
            )
        event = _parse_json_bytes(line, f"capture ledger line {ordinal}")
        if not isinstance(event, dict) or event.get("schema") != EVENT_SCHEMA:
            raise CommitIndexError(f"capture ledger line {ordinal} schema mismatch")
        event_id = _require_nonempty(event.get("event_id"), "event_id")
        idempotency_key = _require_nonempty(
            event.get("idempotency_key"), "idempotency_key"
        )
        if event_id in event_ids:
            raise CommitIndexError(f"duplicate event_id: {event_id}")
        if idempotency_key in idempotency_keys:
            raise CommitIndexError(
                f"duplicate idempotency_key: {idempotency_key}"
            )
        event_ids.add(event_id)
        idempotency_keys.add(idempotency_key)

        event_sha256 = _sha256_bytes(line)
        chain_sha256 = _event_chain(
            previous_sha256=previous_chain,
            event_sha256=event_sha256,
            byte_offset=offset,
            byte_length=len(line),
            event_ordinal=ordinal,
        )
        if event.get("event_type") == "fact_captured":
            identity = _validate_fact_event(event)
            if identity["capture_id"] in capture_ids:
                raise CommitIndexError(
                    f"duplicate capture_id: {identity['capture_id']}"
                )
            capture_ids.add(identity["capture_id"])
        events.append(event)
        metadata.append(
            {
                "event_bytes": line,
                "event_sha256": event_sha256,
                "byte_offset": offset,
                "byte_length": len(line),
                "event_ordinal": ordinal,
                "previous_chain_sha256": previous_chain,
                "chain_sha256": chain_sha256,
            }
        )
        previous_chain = chain_sha256
        offset += len(line)
    return events, metadata, previous_chain


def _attach_index_digest(material: dict[str, Any]) -> dict[str, Any]:
    return {**material, "index_sha256": _sha256_value(material)}


def _index_entry(receipt: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "capture_id": receipt["capture_id"],
        "event_id": receipt["event_id"],
        "idempotency_key": receipt["idempotency_key"],
        "payload_sha256": receipt["payload_sha256"],
        "event_sha256": receipt["event_sha256"],
        "byte_offset": receipt["byte_offset"],
        "byte_length": receipt["byte_length"],
        "event_ordinal": receipt["event_ordinal"],
        "ledger_commitment_sha256": receipt["ledger_commitment"]["sha256"],
        "state_sha256": receipt["state_commitment"][
            "post_event_state_sha256"
        ],
        "receipt_sha256": receipt["receipt_sha256"],
    }


def _build_index(
    *,
    ledger_bytes: bytes,
    state_bytes: bytes,
    state: Mapping[str, Any],
    event_count: int,
    event_chain_sha256: str,
    receipts: Sequence[Mapping[str, Any]],
) -> dict[str, Any]:
    entries = [_index_entry(receipt) for receipt in receipts]
    lookups: dict[str, dict[str, str]] = {
        "capture_id": {},
        "event_id": {},
        "idempotency_key": {},
    }
    for entry in entries:
        for kind in LOOKUP_KINDS:
            value = str(entry[kind])
            if value in lookups[kind]:
                raise CommitIndexError(f"duplicate {kind} in commit index: {value}")
            lookups[kind][value] = str(entry["receipt_sha256"])
    material = {
        "schema": INDEX_SCHEMA,
        "kind": INDEX_KIND,
        "ledger_head": {
            "size_bytes": len(ledger_bytes),
            "event_count": event_count,
            "sha256": _sha256_bytes(ledger_bytes),
            "event_chain_algorithm": CHAIN_SCHEMA,
            "event_chain_sha256": event_chain_sha256,
            "canonical_lf_jsonl": True,
        },
        "state_head": {
            "size_bytes": len(state_bytes),
            "event_count": state.get("event_count"),
            "schema": state.get("schema"),
            "sha256": _sha256_bytes(state_bytes),
            "serialization_observed": "opaque-current-file-bytes",
        },
        "counts": {
            "ledger_event_count": event_count,
            "fact_capture_receipt_count": len(entries),
        },
        "entries": entries,
        "lookups": lookups,
        "receipt_sha256s": [str(row["receipt_sha256"]) for row in entries],
        "verification_boundary": {
            "cold_rebuild_replays_full_ledger": True,
            "bounded_verify_rescans_full_ledger": False,
            "bounded_verify_checks_exact_event_range": True,
            "bounded_verify_checks_current_state_file": True,
            "historical_post_event_state_requires_cold_audit": True,
            "formal_database_touched": False,
        },
        "formal_write_count": 0,
    }
    return _attach_index_digest(material)


def _validate_index(index: Any) -> dict[str, Any]:
    if not isinstance(index, dict):
        raise CommitIndexError("commit index must be an object")
    if index.get("schema") != INDEX_SCHEMA or index.get("kind") != INDEX_KIND:
        raise CommitIndexError("commit index schema or kind mismatch")
    digest = _require_sha(index.get("index_sha256"), "index_sha256")
    material = {key: value for key, value in index.items() if key != "index_sha256"}
    if _sha256_value(material) != digest:
        raise CommitIndexError("commit index content address mismatch")
    if index.get("formal_write_count") != 0:
        raise CommitIndexError("commit index attempted to claim a formal write")
    ledger = index.get("ledger_head")
    state = index.get("state_head")
    counts = index.get("counts")
    entries = index.get("entries")
    lookups = index.get("lookups")
    if not all(isinstance(value, dict) for value in (ledger, state, counts, lookups)):
        raise CommitIndexError("commit index head or lookup objects are missing")
    if not isinstance(entries, list):
        raise CommitIndexError("commit index entries must be an array")
    if ledger.get("event_chain_algorithm") != CHAIN_SCHEMA:
        raise CommitIndexError("commit index event-chain algorithm mismatch")
    for value, label in (
        (ledger.get("sha256"), "ledger_head.sha256"),
        (ledger.get("event_chain_sha256"), "ledger_head.event_chain_sha256"),
        (state.get("sha256"), "state_head.sha256"),
    ):
        _require_sha(value, label)
    if state.get("schema") != STATE_SCHEMA:
        raise CommitIndexError("commit index state schema mismatch")
    if counts.get("ledger_event_count") != ledger.get("event_count"):
        raise CommitIndexError("commit index ledger counts disagree")
    if counts.get("fact_capture_receipt_count") != len(entries):
        raise CommitIndexError("commit index receipt count disagrees")
    if index.get("receipt_sha256s") != [
        row.get("receipt_sha256") for row in entries if isinstance(row, dict)
    ]:
        raise CommitIndexError("commit index receipt order disagrees")

    expected_lookups = {kind: {} for kind in LOOKUP_KINDS}
    seen_receipts: set[str] = set()
    for entry in entries:
        if not isinstance(entry, dict):
            raise CommitIndexError("commit index entry must be an object")
        receipt_sha = _require_sha(
            entry.get("receipt_sha256"), "entry.receipt_sha256"
        )
        if receipt_sha in seen_receipts:
            raise CommitIndexError("commit index repeats a receipt")
        seen_receipts.add(receipt_sha)
        for kind in LOOKUP_KINDS:
            value = _require_nonempty(entry.get(kind), f"entry.{kind}")
            if value in expected_lookups[kind]:
                raise CommitIndexError(f"commit index repeats {kind}: {value}")
            expected_lookups[kind][value] = receipt_sha
        _require_sha(entry.get("payload_sha256"), "entry.payload_sha256")
        _require_sha(entry.get("event_sha256"), "entry.event_sha256")
        _require_sha(
            entry.get("ledger_commitment_sha256"),
            "entry.ledger_commitment_sha256",
        )
        _require_sha(entry.get("state_sha256"), "entry.state_sha256")
    if lookups != expected_lookups:
        raise CommitIndexError("commit index lookup maps disagree with entries")
    return index


def cold_rebuild(
    ledger_path: str | Path,
    state_path: str | Path,
    *,
    state_replayer: StateReplayer | None = None,
    max_ledger_bytes: int = DEFAULT_MAX_LEDGER_BYTES,
    max_event_bytes: int = DEFAULT_MAX_EVENT_BYTES,
    max_state_bytes: int = DEFAULT_MAX_STATE_BYTES,
) -> dict[str, Any]:
    """Read and replay the complete capture ledger into a deterministic bundle."""

    ledger = Path(ledger_path).expanduser().resolve()
    state_file = Path(state_path).expanduser().resolve()
    ledger_bytes = _read_bounded(
        ledger, limit=max_ledger_bytes, label="capture ledger"
    )
    state_bytes = _read_bounded(
        state_file, limit=max_state_bytes, label="capture state"
    )
    actual_state = _parse_json_bytes(state_bytes, "capture state")
    if not isinstance(actual_state, dict) or actual_state.get("schema") != STATE_SCHEMA:
        raise CommitIndexError("capture state schema mismatch")

    events, metadata, event_chain = _parse_ledger(
        ledger_bytes, max_event_bytes=max_event_bytes
    )
    replayer = _resolve_replayer(state_replayer)
    try:
        expected_state = replayer(events)
    except Exception as exc:
        raise CommitIndexError(f"capture ledger replay failed: {exc}") from exc
    if actual_state != expected_state:
        raise CommitIndexError("capture state differs from full ledger replay")
    if actual_state.get("event_count") != len(events):
        raise CommitIndexError("capture state event count differs from the ledger")

    receipts: list[dict[str, Any]] = []
    for index, (event, meta) in enumerate(zip(events, metadata), 1):
        if event.get("event_type") != "fact_captured":
            continue
        try:
            prefix_state = replayer(events[:index])
        except Exception as exc:
            raise CommitIndexError(
                f"capture prefix replay failed at event {index}: {exc}"
            ) from exc
        receipt = build_append_receipt(
            event_bytes=meta["event_bytes"],
            byte_offset=meta["byte_offset"],
            event_ordinal=index,
            previous_chain_sha256=meta["previous_chain_sha256"],
            post_event_state=prefix_state,
        )
        if receipt["ledger_commitment"]["sha256"] != meta["chain_sha256"]:
            raise CommitIndexError("rebuilt event-chain commitment drifted")
        receipts.append(receipt)

    index = _build_index(
        ledger_bytes=ledger_bytes,
        state_bytes=state_bytes,
        state=actual_state,
        event_count=len(events),
        event_chain_sha256=event_chain,
        receipts=receipts,
    )
    return {
        "schema": "capture_commit_bundle_v1",
        "index": index,
        "receipts": {
            receipt["receipt_sha256"]: receipt for receipt in receipts
        },
        "formal_write_count": 0,
    }


def _atomic_write(path: Path, raw: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(
        prefix=f".{path.name}.", dir=str(path.parent)
    )
    tmp = Path(temporary)
    try:
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


def _pretty_json(value: Any) -> bytes:
    return (
        json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    ).encode("utf-8")


def write_bundle(bundle: Mapping[str, Any], output_dir: str | Path) -> dict[str, Any]:
    """Publish receipts first and the deterministic index last.

    ``output_dir`` is always explicit.  This module has no production default and
    therefore cannot silently place artifacts beside the capture ledger.
    """

    if bundle.get("schema") != "capture_commit_bundle_v1":
        raise CommitIndexError("capture commit bundle schema mismatch")
    if bundle.get("formal_write_count") != 0:
        raise CommitIndexError("capture commit bundle attempted a formal write")
    index = _validate_index(bundle.get("index"))
    receipts = bundle.get("receipts")
    if not isinstance(receipts, Mapping):
        raise CommitIndexError("capture commit bundle receipts are missing")
    expected = set(index["receipt_sha256s"])
    if set(receipts) != expected:
        raise CommitIndexError("capture commit bundle receipt set disagrees")
    validated: dict[str, dict[str, Any]] = {}
    for digest, receipt in receipts.items():
        _require_sha(digest, "bundle receipt key")
        value = _validate_receipt(receipt)
        if value["receipt_sha256"] != digest:
            raise CommitIndexError("bundle receipt key differs from content address")
        validated[digest] = value

    root = Path(output_dir).expanduser().resolve()
    receipt_root = root / RECEIPT_DIR
    receipt_root.mkdir(parents=True, exist_ok=True)
    for digest in index["receipt_sha256s"]:
        target = receipt_root / f"{digest}.json"
        raw = _pretty_json(validated[digest])
        if target.exists():
            existing = _read_bounded(
                target,
                limit=DEFAULT_MAX_RECEIPT_BYTES,
                label="existing commit receipt",
            )
            if existing != raw:
                raise CommitIndexError(
                    "existing content-addressed receipt has different bytes"
                )
        else:
            _atomic_write(target, raw)
    _atomic_write(root / INDEX_FILE, _pretty_json(index))
    return {
        "status": "WRITTEN",
        "index_path": str(root / INDEX_FILE),
        "index_sha256": index["index_sha256"],
        "receipt_count": len(expected),
        "formal_write_count": 0,
    }


def _load_index(
    index_dir: str | Path, *, max_index_bytes: int = DEFAULT_MAX_INDEX_BYTES
) -> dict[str, Any]:
    path = Path(index_dir).expanduser().resolve() / INDEX_FILE
    raw = _read_bounded(path, limit=max_index_bytes, label="capture commit index")
    return _validate_index(_parse_json_bytes(raw, "capture commit index"))


def _load_receipt(
    index_dir: str | Path,
    receipt_sha256: str,
    *,
    max_receipt_bytes: int = DEFAULT_MAX_RECEIPT_BYTES,
) -> dict[str, Any]:
    digest = _require_sha(receipt_sha256, "receipt_sha256")
    path = (
        Path(index_dir).expanduser().resolve()
        / RECEIPT_DIR
        / f"{digest}.json"
    )
    raw = _read_bounded(path, limit=max_receipt_bytes, label="capture commit receipt")
    receipt = _validate_receipt(_parse_json_bytes(raw, "capture commit receipt"))
    if receipt["receipt_sha256"] != digest:
        raise CommitIndexError("receipt filename differs from its content address")
    return receipt


def lookup(
    index_dir: str | Path,
    *,
    key_kind: str,
    value: str,
    max_index_bytes: int = DEFAULT_MAX_INDEX_BYTES,
    max_receipt_bytes: int = DEFAULT_MAX_RECEIPT_BYTES,
) -> dict[str, Any]:
    """Resolve exactly one capture/event/idempotency identity to its receipt."""

    if key_kind not in LOOKUP_KINDS:
        raise CommitIndexError(f"unsupported lookup kind: {key_kind}")
    key_value = _require_nonempty(value, key_kind)
    index = _load_index(index_dir, max_index_bytes=max_index_bytes)
    receipt_sha = index["lookups"][key_kind].get(key_value)
    if not receipt_sha:
        raise CommitIndexError(f"commit receipt not found for {key_kind}={key_value}")
    matching = [
        row
        for row in index["entries"]
        if row.get(key_kind) == key_value
        and row.get("receipt_sha256") == receipt_sha
    ]
    if len(matching) != 1:
        raise CommitIndexError("lookup did not resolve to exactly one index entry")
    receipt = _load_receipt(
        index_dir,
        receipt_sha,
        max_receipt_bytes=max_receipt_bytes,
    )
    if receipt.get(key_kind) != key_value:
        raise CommitIndexError("lookup receipt identity mismatch")
    return {
        "status": "FOUND",
        "key_kind": key_kind,
        "key_value": key_value,
        "index_sha256": index["index_sha256"],
        "entry": matching[0],
        "receipt": receipt,
        "formal_write_count": 0,
    }


def _resolve_receipt_for_verify(
    index_dir: str | Path,
    *,
    receipt_sha256: str | None,
    key_kind: str | None,
    value: str | None,
    max_index_bytes: int,
    max_receipt_bytes: int,
) -> tuple[dict[str, Any], dict[str, Any], dict[str, Any]]:
    index = _load_index(index_dir, max_index_bytes=max_index_bytes)
    if receipt_sha256:
        digest = _require_sha(receipt_sha256, "receipt_sha256")
    else:
        if key_kind not in LOOKUP_KINDS or not value:
            raise CommitIndexError(
                "verify requires receipt_sha256 or one exact lookup identity"
            )
        digest = index["lookups"][key_kind].get(value)
        if not digest:
            raise CommitIndexError("verify lookup identity is absent")
    entries = [
        row for row in index["entries"] if row.get("receipt_sha256") == digest
    ]
    if len(entries) != 1:
        raise CommitIndexError("verify receipt is not uniquely indexed")
    receipt = _load_receipt(
        index_dir, digest, max_receipt_bytes=max_receipt_bytes
    )
    return index, entries[0], receipt


def bounded_verify(
    ledger_path: str | Path,
    state_path: str | Path,
    index_dir: str | Path,
    *,
    receipt_sha256: str | None = None,
    key_kind: str | None = None,
    value: str | None = None,
    max_event_bytes: int = DEFAULT_MAX_EVENT_BYTES,
    max_state_bytes: int = DEFAULT_MAX_STATE_BYTES,
    max_index_bytes: int = DEFAULT_MAX_INDEX_BYTES,
    max_receipt_bytes: int = DEFAULT_MAX_RECEIPT_BYTES,
) -> dict[str, Any]:
    """Verify one indexed event range and the current state without a ledger scan."""

    index, entry, receipt = _resolve_receipt_for_verify(
        index_dir,
        receipt_sha256=receipt_sha256,
        key_kind=key_kind,
        value=value,
        max_index_bytes=max_index_bytes,
        max_receipt_bytes=max_receipt_bytes,
    )
    ledger = Path(ledger_path).expanduser().resolve()
    state_file = Path(state_path).expanduser().resolve()
    try:
        ledger_size = ledger.stat().st_size
    except OSError as exc:
        raise CommitIndexError("capture ledger is unavailable") from exc
    expected_ledger_size = index["ledger_head"]["size_bytes"]
    if ledger_size != expected_ledger_size:
        raise CommitIndexError("capture ledger size drifted from the commit index")

    byte_offset = receipt["byte_offset"]
    byte_length = receipt["byte_length"]
    if byte_length > max_event_bytes:
        raise CommitIndexError("indexed event exceeds bounded verification limit")
    if byte_offset < 0 or byte_offset + byte_length > ledger_size:
        raise CommitIndexError("indexed event range lies outside the capture ledger")
    try:
        with ledger.open("rb") as handle:
            handle.seek(byte_offset)
            event_bytes = handle.read(byte_length)
    except OSError as exc:
        raise CommitIndexError("failed to read the indexed event range") from exc
    if len(event_bytes) != byte_length:
        raise CommitIndexError("indexed event range was truncated")
    if _sha256_bytes(event_bytes) != receipt["event_sha256"]:
        raise CommitIndexError("indexed event bytes drifted")
    event = _parse_json_bytes(event_bytes, "indexed capture event")
    if not isinstance(event, dict):
        raise CommitIndexError("indexed capture event must be an object")
    identity = _validate_fact_event(event)
    for key, expected in identity.items():
        if receipt.get(key) != expected or entry.get(key) != expected:
            raise CommitIndexError(f"indexed event {key} binding drifted")
    for key in (
        "event_sha256",
        "byte_offset",
        "byte_length",
        "event_ordinal",
        "receipt_sha256",
    ):
        if entry.get(key) != receipt.get(key):
            raise CommitIndexError(f"index and receipt disagree on {key}")
    expected_chain = _event_chain(
        previous_sha256=receipt["ledger_commitment"]["previous_sha256"],
        event_sha256=receipt["event_sha256"],
        byte_offset=byte_offset,
        byte_length=byte_length,
        event_ordinal=receipt["event_ordinal"],
    )
    if expected_chain != receipt["ledger_commitment"]["sha256"]:
        raise CommitIndexError("receipt event-chain commitment drifted")
    if entry["ledger_commitment_sha256"] != expected_chain:
        raise CommitIndexError("index event-chain commitment drifted")

    state_bytes = _read_bounded(
        state_file, limit=max_state_bytes, label="capture state"
    )
    if len(state_bytes) != index["state_head"]["size_bytes"]:
        raise CommitIndexError("capture state size drifted from the commit index")
    if _sha256_bytes(state_bytes) != index["state_head"]["sha256"]:
        raise CommitIndexError("capture state bytes drifted from the commit index")
    state = _parse_json_bytes(state_bytes, "capture state")
    if not isinstance(state, dict) or state.get("schema") != STATE_SCHEMA:
        raise CommitIndexError("capture state schema drifted")
    if state.get("event_count") != index["state_head"]["event_count"]:
        raise CommitIndexError("capture state event count drifted")

    capture = event["capture"]
    # Imported at the verification boundary to avoid a module-load cycle:
    # capture_hot_writer imports this sidecar before its capture model.
    from intake_fact_capture_408 import initial_capture_quality_status

    committed_capture_state = {
        "capture_id": identity["capture_id"],
        "study_date": capture["study_date"],
        "recorded_at": event["created_at"],
        "payload_sha256": identity["payload_sha256"],
        "quality_status": initial_capture_quality_status(capture),
        "current_batch_id": None,
        "formal_id": capture["identity_hint"].get("formal_id") or None,
        "last_error": None,
        "formalization_authorized": capture["formalization_authorized"],
        "capture": capture,
    }
    committed_capture_state_sha256s = {
        _sha256_value(committed_capture_state)
    }
    legacy_committed_capture_state = dict(committed_capture_state)
    legacy_committed_capture_state.pop("recorded_at")
    committed_capture_state_sha256s.add(
        _sha256_value(legacy_committed_capture_state)
    )
    if receipt["state_commitment"][
        "capture_state_sha256"
    ] not in committed_capture_state_sha256s:
        raise CommitIndexError("capture-specific committed state drifted")

    is_ledger_head_event = (
        receipt["event_ordinal"] == index["ledger_head"]["event_count"]
    )
    if is_ledger_head_event and (
        receipt["state_commitment"]["post_event_state_sha256"]
        != index["state_head"]["sha256"]
    ):
        raise CommitIndexError("head receipt and current state hash disagree")
    return {
        "status": "PASS",
        "capture_id": receipt["capture_id"],
        "event_id": receipt["event_id"],
        "receipt_sha256": receipt["receipt_sha256"],
        "index_sha256": index["index_sha256"],
        "event_bytes_read": len(event_bytes),
        "state_bytes_read": len(state_bytes),
        "whole_ledger_rescanned": False,
        "exact_event_range_verified": True,
        "current_state_file_verified": True,
        "capture_commit_state_verified": True,
        "historical_post_event_state_replayed": is_ledger_head_event,
        "cold_audit_required_for_whole_ledger": True,
        "formal_write_count": 0,
    }


def cold_audit(
    ledger_path: str | Path,
    state_path: str | Path,
    index_dir: str | Path,
    *,
    state_replayer: StateReplayer | None = None,
    max_ledger_bytes: int = DEFAULT_MAX_LEDGER_BYTES,
    max_event_bytes: int = DEFAULT_MAX_EVENT_BYTES,
    max_state_bytes: int = DEFAULT_MAX_STATE_BYTES,
    max_index_bytes: int = DEFAULT_MAX_INDEX_BYTES,
    max_receipt_bytes: int = DEFAULT_MAX_RECEIPT_BYTES,
) -> dict[str, Any]:
    """Fully replay the ledger and compare every referenced derived artifact."""

    expected = cold_rebuild(
        ledger_path,
        state_path,
        state_replayer=state_replayer,
        max_ledger_bytes=max_ledger_bytes,
        max_event_bytes=max_event_bytes,
        max_state_bytes=max_state_bytes,
    )
    actual_index = _load_index(index_dir, max_index_bytes=max_index_bytes)
    if actual_index != expected["index"]:
        raise CommitIndexError("stored commit index differs from cold rebuild")
    for digest, expected_receipt in expected["receipts"].items():
        actual_receipt = _load_receipt(
            index_dir, digest, max_receipt_bytes=max_receipt_bytes
        )
        if actual_receipt != expected_receipt:
            raise CommitIndexError(
                f"stored receipt differs from cold rebuild: {digest}"
            )
    receipt_root = Path(index_dir).expanduser().resolve() / RECEIPT_DIR
    stored_files = set(receipt_root.glob("*.json")) if receipt_root.is_dir() else set()
    return {
        "status": "PASS",
        "index_sha256": actual_index["index_sha256"],
        "ledger_event_count": actual_index["counts"]["ledger_event_count"],
        "receipt_count": len(expected["receipts"]),
        "unreferenced_receipt_file_count": max(
            0, len(stored_files) - len(expected["receipts"])
        ),
        "whole_ledger_replayed": True,
        "current_state_replay_verified": True,
        "historical_post_event_states_replayed": True,
        "formal_write_count": 0,
    }


def _summary(bundle: Mapping[str, Any]) -> dict[str, Any]:
    index = bundle["index"]
    return {
        "status": "REBUILT",
        "index_sha256": index["index_sha256"],
        "ledger_event_count": index["counts"]["ledger_event_count"],
        "receipt_count": index["counts"]["fact_capture_receipt_count"],
        "formal_write_count": 0,
    }


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Build and verify sidecar commit receipts for 408 captures"
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    rebuild_parser = subparsers.add_parser("rebuild")
    rebuild_parser.add_argument("--ledger", required=True)
    rebuild_parser.add_argument("--state", required=True)
    rebuild_parser.add_argument("--output-dir")

    audit_parser = subparsers.add_parser("audit")
    audit_parser.add_argument("--ledger", required=True)
    audit_parser.add_argument("--state", required=True)
    audit_parser.add_argument("--index-dir", required=True)

    lookup_parser = subparsers.add_parser("lookup")
    lookup_parser.add_argument("--index-dir", required=True)
    lookup_parser.add_argument("--key-kind", choices=sorted(LOOKUP_KINDS), required=True)
    lookup_parser.add_argument("--value", required=True)

    verify_parser = subparsers.add_parser("verify")
    verify_parser.add_argument("--ledger", required=True)
    verify_parser.add_argument("--state", required=True)
    verify_parser.add_argument("--index-dir", required=True)
    verify_parser.add_argument("--receipt-sha256")
    verify_parser.add_argument("--key-kind", choices=sorted(LOOKUP_KINDS))
    verify_parser.add_argument("--value")
    verify_parser.add_argument(
        "--max-event-bytes", type=int, default=DEFAULT_MAX_EVENT_BYTES
    )
    verify_parser.add_argument(
        "--max-state-bytes", type=int, default=DEFAULT_MAX_STATE_BYTES
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        if args.command == "rebuild":
            bundle = cold_rebuild(args.ledger, args.state)
            result = _summary(bundle)
            if args.output_dir:
                result["write"] = write_bundle(bundle, args.output_dir)
        elif args.command == "audit":
            result = cold_audit(args.ledger, args.state, args.index_dir)
        elif args.command == "lookup":
            result = lookup(
                args.index_dir, key_kind=args.key_kind, value=args.value
            )
        else:
            if bool(args.receipt_sha256) == bool(args.key_kind or args.value):
                raise CommitIndexError(
                    "verify requires exactly one of receipt_sha256 or key lookup"
                )
            if bool(args.key_kind) != bool(args.value):
                raise CommitIndexError("verify key-kind and value must be used together")
            result = bounded_verify(
                args.ledger,
                args.state,
                args.index_dir,
                receipt_sha256=args.receipt_sha256,
                key_kind=args.key_kind,
                value=args.value,
                max_event_bytes=args.max_event_bytes,
                max_state_bytes=args.max_state_bytes,
            )
    except CommitIndexError as exc:
        print(
            json.dumps(
                {
                    "status": "FAIL_CLOSED",
                    "error": str(exc),
                    "formal_write_count": 0,
                },
                ensure_ascii=False,
                sort_keys=True,
            ),
            file=sys.stderr,
        )
        return 2
    print(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
