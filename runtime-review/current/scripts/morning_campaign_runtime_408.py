#!/usr/bin/env python3
"""Bounded successor runtime for high-throughput 408 morning campaigns.

This module is intentionally separate from the managed one-question session.
It consumes an immutable daily campaign manifest, keeps only one at-most-16
item segment in active state, and writes its own append-only execution ledger.
Historical manifests retain their canonical-review/failure-Capture behavior.
New manifests bind saving-contract-v2: every terminal item must present a
verified shared episode Capture before advance, while this runtime writes no
review event, FSRS state, formal knowledge, or personal-summary refresh.
"""

from __future__ import annotations

from study_commit_followup_408 import study_commit_boundary

import argparse
import fcntl
import hashlib
import json
import math
import os
import re
import stat
import sys
import tempfile
from contextlib import contextmanager
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any, Iterator
from zoneinfo import ZoneInfo

import review_hot_state_408 as canonical_review_state
import review_outcome_hot_408 as canonical_review_writer
import capture_hot_writer_408 as capture_hot
import conversation_package_408 as conversation_package
import intake_fact_capture_408 as fact_capture


MANIFEST_SCHEMA = "morning-coverage-campaign-v1"
SEGMENT_SCHEMA = "morning-coverage-segment-commitment-v1"
RUN_SCHEMA = "morning-campaign-runtime-v1"
STATE_SCHEMA = "morning-campaign-active-segment-state-v1"
EVENT_SCHEMA = "morning-campaign-event-v1"
RECEIPT_SCHEMA = "morning-campaign-operation-receipt-v1"
OUTCOME_SCHEMA = "rapid-review-outcome-v1"
CAPTURE_REQUEST_SCHEMA = "rapid-review-capture-authorization-request-v1"
CAPTURE_RECEIPT_SCHEMA_V1 = "rapid-review-durable-capture-receipt-v1"
CAPTURE_RECEIPT_SCHEMA_V2 = "rapid-review-durable-capture-receipt-v2"
CAPTURE_RECEIPT_SCHEMA = CAPTURE_RECEIPT_SCHEMA_V1
CARRYOVER_SCHEMA = "morning-coverage-campaign-carryover-v1"
REVIEW_OUTCOME_CONTRACT = "canonical-review-event-v1"
NO_REVIEW_OUTCOME_CONTRACT = "none-saving-contract-v2"
SAVING_CONTRACT_V2_SCHEMA = "morning-campaign-saving-contract-v2"
EPISODE_CAPTURE_SCHEMA = "cs408-study-episode-capture-v1"
LEGACY_SAVING_CONTRACT = "canonical-review-with-failure-capture-v1"
TRANSACTION_INTENT_SCHEMA_V1 = "morning-campaign-transaction-intent-v1"
TRANSACTION_INTENT_SCHEMA_V2 = "morning-campaign-transaction-intent-v2"
TRANSACTION_INTENT_POLICY_SCHEMA = "morning-campaign-transaction-intent-policy-v1"

SEGMENT_SIZE = 16
HASH_AUDIT_RATE = 0.20
MAX_MANIFEST_BYTES = 8 * 1024 * 1024
MAX_SEGMENT_BYTES = 256 * 1024
MAX_STATE_BYTES = 64 * 1024
MAX_RECEIPT_BYTES = 256 * 1024

SHA_RE = re.compile(r"^[0-9a-f]{64}$")
RUN_ID_RE = re.compile(r"^MCR-[0-9]{8}-[0-9A-F]{16}$")
ITEM_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}$")
SAFE_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:@/+\-]{0,191}$")
FORMAL_NODE_RE = re.compile(r"^(?:DS|CO|OS|CN)_(?:\d{4}|UNK)_\d{3}$")
CANONICAL_ITEM_RE = re.compile(r"^EXAM408-\d{4}-Q\d{2}$")
VARIANT_ITEM_RE = re.compile(r"^EVB-[0-9A-F]{24}$")
FORMAL_ORIGINAL_ITEM_RE = re.compile(r"^FOS-[0-9A-F]{24}$")

CHOICE_RESULTS = {"correct", "incorrect", "partial", "uncertain"}
REASONING_RESULTS = {"sound", "partial", "diverged", "not_observed"}
CONFIDENCE_LEVELS = {"low", "medium", "high"}
PROMPT_DEPENDENCIES = {"none", "minimal", "progressive", "full"}
PAUSE_REASONS = {
    "user_pause", "time_boundary", "fatigue", "external_interruption",
    "capture_hold",
}
SURFACE_KINDS = {"canonical_original", "formal_original", "verified_variant"}


class CampaignRuntimeError(RuntimeError):
    """A stable fail-closed campaign runtime error."""


def _canonical_bytes(value: Any) -> bytes:
    return json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")


def _pretty_bytes(value: Any) -> bytes:
    return (
        json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2) + "\n"
    ).encode("utf-8")


def _digest(value: Any) -> str:
    return hashlib.sha256(_canonical_bytes(value)).hexdigest()


def _sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def _safe_timestamp(value: str | None) -> str:
    if value is None:
        return _now()
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError as exc:
        raise CampaignRuntimeError("event_time must be ISO-8601") from exc
    if parsed.tzinfo is None:
        raise CampaignRuntimeError("event_time must include a timezone offset")
    return parsed.replace(microsecond=0).isoformat()


def _safe_date(value: object, label: str) -> str:
    if not isinstance(value, str):
        raise CampaignRuntimeError(f"{label} must be YYYY-MM-DD")
    try:
        return date.fromisoformat(value).isoformat()
    except ValueError as exc:
        raise CampaignRuntimeError(f"{label} must be YYYY-MM-DD") from exc


def _safe_id(value: object, label: str, pattern: re.Pattern[str] = SAFE_ID_RE) -> str:
    if not isinstance(value, str) or not pattern.fullmatch(value):
        raise CampaignRuntimeError(f"{label} is unsafe")
    return value


def _safe_sha(value: object, label: str) -> str:
    if not isinstance(value, str) or not SHA_RE.fullmatch(value):
        raise CampaignRuntimeError(f"{label} must be a lowercase SHA-256")
    return value


def _regular_file(path: Path, label: str, maximum: int) -> None:
    try:
        info = path.lstat()
    except OSError as exc:
        raise CampaignRuntimeError(f"{label} is unavailable") from exc
    if stat.S_ISLNK(info.st_mode) or not stat.S_ISREG(info.st_mode):
        raise CampaignRuntimeError(f"{label} must be a regular non-symlink file")
    if info.st_size > maximum:
        raise CampaignRuntimeError(f"{label} exceeds its bounded size")


def _read_json(path: Path, label: str, maximum: int) -> dict[str, Any]:
    _regular_file(path, label, maximum)
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise CampaignRuntimeError(f"{label} is invalid JSON") from exc
    if not isinstance(value, dict):
        raise CampaignRuntimeError(f"{label} must be a JSON object")
    return value


def _ensure_dir(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True, mode=0o700)
    info = path.lstat()
    if stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode):
        raise CampaignRuntimeError(f"managed directory is unsafe: {path}")
    os.chmod(path, 0o700)


def _atomic_write(path: Path, value: Any) -> str:
    payload = _pretty_bytes(value)
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    os.chmod(path.parent, 0o700)
    descriptor, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        os.fchmod(descriptor, 0o600)
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


@contextmanager
def _runtime_lock(runtime_root: Path) -> Iterator[None]:
    _ensure_dir(runtime_root)
    path = runtime_root / ".morning-campaign-runtime.lock"
    descriptor = os.open(path, os.O_RDWR | os.O_CREAT, 0o600)
    try:
        fcntl.flock(descriptor, fcntl.LOCK_EX)
        yield
    finally:
        fcntl.flock(descriptor, fcntl.LOCK_UN)
        os.close(descriptor)


def _run_dir(runtime_root: str | Path, run_id: str) -> Path:
    root = Path(runtime_root).expanduser().resolve()
    _safe_id(run_id, "run_id", RUN_ID_RE)
    return root / "runs" / run_id


def runtime_run_directory(runtime_root: str | Path, run_id: str) -> Path:
    """Return the validated run directory without reading mutable state."""

    return _run_dir(runtime_root, run_id)


def saving_contract_for_run(
    runtime_root: str | Path, run_id: str
) -> dict[str, Any]:
    """Return the immutable saving policy without exposing item content."""

    run_dir = _run_dir(runtime_root, run_id)
    run = _load_run(run_dir)
    kind, digest = _saving_contract(run)
    return {
        "kind": kind,
        "saving_contract_sha256": digest,
        "episode_capture_every_item": kind == SAVING_CONTRACT_V2_SCHEMA,
    }


def _normalize_item(raw: object, label: str) -> dict[str, Any]:
    if not isinstance(raw, dict):
        raise CampaignRuntimeError(f"{label} must be an object")
    allowed = {
        "item_id",
        "source_id",
        "formal_node_id",
        "action_id",
        "surface_sha256",
        "grading_sha256",
        "pack_commitment_sha256",
        "mandatory_audit",
        "true_item_id",
        "variant_id",
        "surface_kind",
        "campaign_item_commitment_sha256",
        "first_variant",
        "recent_recurrence",
    }
    if set(raw) - allowed:
        raise CampaignRuntimeError(f"{label} contains unsupported fields")
    formal_node_id = raw.get("formal_node_id")
    if formal_node_id is not None:
        formal_node_id = _safe_id(
            formal_node_id, f"{label}.formal_node_id", FORMAL_NODE_RE
        )
    action_id = raw.get("action_id")
    if action_id is not None:
        action_id = _safe_id(action_id, f"{label}.action_id")
    mandatory = raw.get("mandatory_audit", False)
    if not isinstance(mandatory, bool):
        raise CampaignRuntimeError(f"{label}.mandatory_audit must be boolean")
    true_item_id = _safe_id(raw.get("true_item_id"), f"{label}.true_item_id")
    surface_kind = raw.get("surface_kind")
    variant_id = raw.get("variant_id")
    if surface_kind not in SURFACE_KINDS:
        raise CampaignRuntimeError(f"{label}.surface_kind is invalid")
    if surface_kind == "canonical_original":
        if not CANONICAL_ITEM_RE.fullmatch(true_item_id) or variant_id is not None:
            raise CampaignRuntimeError(f"{label} canonical identity is invalid")
        normalized_variant_id = None
    elif surface_kind == "formal_original":
        if not FORMAL_ORIGINAL_ITEM_RE.fullmatch(true_item_id) or variant_id is not None:
            raise CampaignRuntimeError(f"{label} formal-original identity is invalid")
        normalized_variant_id = None
    else:
        normalized_variant_id = _safe_id(
            variant_id, f"{label}.variant_id", VARIANT_ITEM_RE
        )
        if true_item_id != normalized_variant_id:
            raise CampaignRuntimeError(f"{label} variant identity is inconsistent")
    first_variant = raw.get("first_variant", False)
    recent_recurrence = raw.get("recent_recurrence", False)
    if not isinstance(first_variant, bool) or not isinstance(recent_recurrence, bool):
        raise CampaignRuntimeError(f"{label} audit flags must be boolean")
    if first_variant and surface_kind != "verified_variant":
        raise CampaignRuntimeError(f"{label}.first_variant requires a verified variant")
    return {
        "item_id": _safe_id(raw.get("item_id"), f"{label}.item_id", ITEM_ID_RE),
        "source_id": _safe_id(raw.get("source_id"), f"{label}.source_id"),
        "formal_node_id": formal_node_id,
        "action_id": action_id,
        "surface_sha256": _safe_sha(
            raw.get("surface_sha256"), f"{label}.surface_sha256"
        ),
        "grading_sha256": _safe_sha(
            raw.get("grading_sha256"), f"{label}.grading_sha256"
        ),
        "pack_commitment_sha256": _safe_sha(
            raw.get("pack_commitment_sha256"),
            f"{label}.pack_commitment_sha256",
        ),
        "mandatory_audit": mandatory,
        "true_item_id": true_item_id,
        "variant_id": normalized_variant_id,
        "surface_kind": surface_kind,
        "campaign_item_commitment_sha256": _safe_sha(
            raw.get("campaign_item_commitment_sha256"),
            f"{label}.campaign_item_commitment_sha256",
        ),
        "first_variant": first_variant,
        "recent_recurrence": recent_recurrence,
    }


def saving_contract_v2() -> dict[str, Any]:
    """Return the immutable no-review contract for newly prepared campaigns."""

    return {
        "schema": SAVING_CONTRACT_V2_SCHEMA,
        "mode": "episode_capture_every_item",
        "episode_capture_schema": EPISODE_CAPTURE_SCHEMA,
        "capture_before_advance": True,
        "canonical_review_write": False,
        "fsrs_write": False,
        "personal_summary_write": False,
        "formal_write": False,
        "background_processing": "none",
    }


def _saving_contract(manifest_or_run: dict[str, Any]) -> tuple[str, str | None]:
    contract = manifest_or_run.get("saving_contract")
    if contract is None:
        if (
            "saving_contract" in manifest_or_run
            or "saving_contract_sha256" in manifest_or_run
        ):
            raise CampaignRuntimeError("campaign saving contract is incomplete")
        return LEGACY_SAVING_CONTRACT, None
    expected = saving_contract_v2()
    digest = _digest(expected)
    if (
        contract != expected
        or manifest_or_run.get("saving_contract_sha256") != digest
    ):
        raise CampaignRuntimeError("campaign saving contract drifted")
    return SAVING_CONTRACT_V2_SCHEMA, digest


def _run_id(
    campaign_date: str,
    items: list[dict[str, Any]],
    saving_contract: dict[str, Any] | None = None,
) -> str:
    basis = {
        "schema": MANIFEST_SCHEMA,
        "campaign_date": campaign_date,
        "timezone": "Asia/Shanghai",
        "segment_size": SEGMENT_SIZE,
        "items": items,
    }
    if saving_contract is not None:
        basis["saving_contract"] = saving_contract
    return f"MCR-{campaign_date.replace('-', '')}-{_digest(basis)[:16].upper()}"


def _segment_commitment(
    run_id: str, ordinal: int, items: list[dict[str, Any]]
) -> tuple[str, str]:
    segment_id = f"{run_id}-S{ordinal + 1:03d}"
    core = {
        "schema": SEGMENT_SCHEMA,
        "run_id": run_id,
        "segment_id": segment_id,
        "ordinal": ordinal,
        "item_count": len(items),
        "items": items,
    }
    return segment_id, _digest(core)


def _stable_audit_ids(run_id: str, item_ids: list[str]) -> set[str]:
    count = math.ceil(len(item_ids) * HASH_AUDIT_RATE)
    ranked = sorted(
        item_ids,
        key=lambda item_id: (
            hashlib.sha256(f"{run_id}\0{item_id}".encode()).hexdigest(),
            item_id,
        ),
    )
    return set(ranked[:count])


def build_campaign_manifest(
    campaign_date: str,
    items: list[dict[str, Any]],
    *,
    segment_verification_sha256s: list[str] | None = None,
    all_ready: bool = False,
    saving_contract: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Build the canonical cold manifest from prepared-pack item commitments."""

    normalized_date = _safe_date(campaign_date, "campaign_date")
    if not items:
        raise CampaignRuntimeError("campaign items must be non-empty")
    normalized_items = [
        _normalize_item(item, f"items[{index}]") for index, item in enumerate(items)
    ]
    item_ids = [item["item_id"] for item in normalized_items]
    if len(item_ids) != len(set(item_ids)):
        raise CampaignRuntimeError("campaign item_id values must be unique")
    contract = saving_contract_v2() if saving_contract is None else saving_contract
    if contract != saving_contract_v2():
        raise CampaignRuntimeError("only saving-contract-v2 may be used for a new manifest")
    contract_sha = _digest(contract)
    run_id = _run_id(normalized_date, normalized_items, contract)
    audit_ids = _stable_audit_ids(run_id, item_ids)
    normalized_items = [
        {**item, "stable_hash_audit": item["item_id"] in audit_ids}
        for item in normalized_items
    ]
    segment_count = math.ceil(len(normalized_items) / SEGMENT_SIZE)
    if segment_verification_sha256s is None:
        if all_ready:
            raise CampaignRuntimeError(
                "ready segments require explicit upstream verification SHA-256 values"
            )
        segment_verification_sha256s = [None] * segment_count  # type: ignore[list-item]
    if len(segment_verification_sha256s) != segment_count:
        raise CampaignRuntimeError("segment verification count is inconsistent")
    segments: list[dict[str, Any]] = []
    for ordinal in range(segment_count):
        chunk = normalized_items[ordinal * SEGMENT_SIZE : (ordinal + 1) * SEGMENT_SIZE]
        segment_id, commitment = _segment_commitment(run_id, ordinal, chunk)
        segments.append(
            {
                "segment_id": segment_id,
                "ordinal": ordinal,
                "item_count": len(chunk),
                "items": chunk,
                "commitment_sha256": commitment,
                "ready": all_ready,
                "verification_sha256": (
                    _safe_sha(
                        segment_verification_sha256s[ordinal],
                        f"segment_verification_sha256s[{ordinal}]",
                    )
                    if segment_verification_sha256s[ordinal] is not None
                    else None
                ),
            }
        )
    return {
        "schema": MANIFEST_SCHEMA,
        "run_id": run_id,
        "campaign_date": normalized_date,
        "timezone": "Asia/Shanghai",
        "segment_size": SEGMENT_SIZE,
        "item_count": len(normalized_items),
        "segment_count": len(segments),
        "stable_hash_audit_rate": HASH_AUDIT_RATE,
        "stable_hash_audit_count": len(audit_ids),
        "all_segments_ready": all_ready,
        "review_outcome_contract_version": NO_REVIEW_OUTCOME_CONTRACT,
        "saving_contract": contract,
        "saving_contract_sha256": contract_sha,
        "segments": segments,
        "answer_safe": True,
        "capture_write_count": 0,
        "formal_write_count": 0,
    }


def normalize_manifest(raw: dict[str, Any]) -> dict[str, Any]:
    legacy_allowed = {
        "schema",
        "run_id",
        "campaign_date",
        "timezone",
        "segment_size",
        "item_count",
        "segment_count",
        "stable_hash_audit_rate",
        "stable_hash_audit_count",
        "all_segments_ready",
        "review_outcome_contract_version",
        "segments",
        "answer_safe",
        "capture_write_count",
        "formal_write_count",
    }
    v2_allowed = legacy_allowed | {"saving_contract", "saving_contract_sha256"}
    if frozenset(raw) not in {frozenset(legacy_allowed), frozenset(v2_allowed)} or raw.get("schema") != MANIFEST_SCHEMA:
        raise CampaignRuntimeError("campaign manifest schema or fields are invalid")
    contract_kind, _contract_sha = _saving_contract(raw)
    campaign_date = _safe_date(raw.get("campaign_date"), "campaign_date")
    if raw.get("timezone") != "Asia/Shanghai":
        raise CampaignRuntimeError("campaign timezone must be Asia/Shanghai")
    if raw.get("segment_size") != SEGMENT_SIZE:
        raise CampaignRuntimeError("campaign segment_size must be 16")
    rows = raw.get("segments")
    if not isinstance(rows, list) or not rows:
        raise CampaignRuntimeError("campaign segments must be non-empty")
    normalized_segments: list[dict[str, Any]] = []
    all_items: list[dict[str, Any]] = []
    for ordinal, row in enumerate(rows):
        if not isinstance(row, dict) or set(row) != {
            "segment_id",
            "ordinal",
            "item_count",
            "items",
            "commitment_sha256",
            "ready",
            "verification_sha256",
        }:
            raise CampaignRuntimeError(f"segments[{ordinal}] fields are invalid")
        if row.get("ordinal") != ordinal:
            raise CampaignRuntimeError("segment ordinals must be contiguous")
        segment_items_raw = row.get("items")
        if not isinstance(segment_items_raw, list) or not 1 <= len(segment_items_raw) <= SEGMENT_SIZE:
            raise CampaignRuntimeError("each segment must contain 1 to 16 items")
        normalized_items: list[dict[str, Any]] = []
        for offset, item_raw in enumerate(segment_items_raw):
            if not isinstance(item_raw, dict):
                raise CampaignRuntimeError("segment item must be an object")
            stable = item_raw.get("stable_hash_audit")
            base = dict(item_raw)
            base.pop("stable_hash_audit", None)
            item = _normalize_item(base, f"segments[{ordinal}].items[{offset}]")
            if not isinstance(stable, bool):
                raise CampaignRuntimeError("stable_hash_audit must be boolean")
            normalized_items.append({**item, "stable_hash_audit": stable})
        if row.get("item_count") != len(normalized_items):
            raise CampaignRuntimeError("segment item_count drifted")
        ready = row.get("ready")
        if not isinstance(ready, bool):
            raise CampaignRuntimeError("segment ready must be boolean")
        segment_id = _safe_id(row.get("segment_id"), "segment_id")
        verification = row.get("verification_sha256")
        if ready:
            verification = _safe_sha(verification, "segment verification")
        elif verification is not None:
            verification = _safe_sha(verification, "segment verification")
        normalized_segments.append(
            {
                "segment_id": segment_id,
                "ordinal": ordinal,
                "item_count": len(normalized_items),
                "items": normalized_items,
                "commitment_sha256": _safe_sha(
                    row.get("commitment_sha256"), "segment commitment"
                ),
                "ready": ready,
                "verification_sha256": verification,
            }
        )
        all_items.extend(normalized_items)
    item_ids = [item["item_id"] for item in all_items]
    if len(item_ids) != len(set(item_ids)):
        raise CampaignRuntimeError("campaign item_id values must be unique")
    expected_run_id = _run_id(
        campaign_date,
        [{key: value for key, value in item.items() if key != "stable_hash_audit"} for item in all_items],
        raw.get("saving_contract") if contract_kind == SAVING_CONTRACT_V2_SCHEMA else None,
    )
    if raw.get("run_id") != expected_run_id:
        raise CampaignRuntimeError("run_id does not bind the campaign items")
    expected_audit = _stable_audit_ids(expected_run_id, item_ids)
    for item in all_items:
        if item["stable_hash_audit"] != (item["item_id"] in expected_audit):
            raise CampaignRuntimeError("stable 20 percent hash audit assignment drifted")
    for segment in normalized_segments:
        expected_id, expected_sha = _segment_commitment(
            expected_run_id, segment["ordinal"], segment["items"]
        )
        if segment["segment_id"] != expected_id or segment["commitment_sha256"] != expected_sha:
            raise CampaignRuntimeError("segment commitment drifted")
    all_ready = all(segment["ready"] for segment in normalized_segments)
    if (
        raw.get("item_count") != len(all_items)
        or raw.get("segment_count") != len(normalized_segments)
        or raw.get("stable_hash_audit_rate") != HASH_AUDIT_RATE
        or raw.get("stable_hash_audit_count") != len(expected_audit)
        or raw.get("all_segments_ready") is not all_ready
        or raw.get("review_outcome_contract_version")
        != (
            NO_REVIEW_OUTCOME_CONTRACT
            if contract_kind == SAVING_CONTRACT_V2_SCHEMA
            else REVIEW_OUTCOME_CONTRACT
        )
        or raw.get("answer_safe") is not True
        or raw.get("capture_write_count") != 0
        or raw.get("formal_write_count") != 0
    ):
        raise CampaignRuntimeError("campaign manifest aggregate fields drifted")
    return {
        **raw,
        "campaign_date": campaign_date,
        "segments": normalized_segments,
    }


def _segment_path(run_dir: Path, ordinal: int) -> Path:
    return run_dir / "segments" / f"{ordinal:04d}.json"


def _load_segment(run_dir: Path, ordinal: int) -> dict[str, Any]:
    segment = _read_json(
        _segment_path(run_dir, ordinal), "campaign segment", MAX_SEGMENT_BYTES
    )
    if segment.get("schema") != SEGMENT_SCHEMA or segment.get("ordinal") != ordinal:
        raise CampaignRuntimeError("campaign segment identity drifted")
    expected_id, expected_sha = _segment_commitment(
        str(segment.get("run_id")), ordinal, list(segment.get("items") or [])
    )
    if segment.get("segment_id") != expected_id or segment.get("commitment_sha256") != expected_sha:
        raise CampaignRuntimeError("campaign segment commitment drifted")
    return segment


def _active_segment(segment: dict[str, Any], completed: int = 0) -> dict[str, Any]:
    return {
        "segment_id": segment["segment_id"],
        "ordinal": segment["ordinal"],
        "commitment_sha256": segment["commitment_sha256"],
        "item_count": segment["item_count"],
        "completed_count": completed,
        "items": segment["items"],
    }


def _initial_state(manifest: dict[str, Any]) -> dict[str, Any]:
    first = manifest["segments"][0]
    state = {
        "schema": STATE_SCHEMA,
        "run_id": manifest["run_id"],
        "campaign_date": manifest["campaign_date"],
        "status": "ready" if manifest["all_segments_ready"] else "preparing",
        "segment_count": manifest["segment_count"],
        "item_count": manifest["item_count"],
        "completed_items": 0,
        "ratings": {"Again": 0, "Hard": 0, "Good": 0},
        "current_segment_ordinal": 0,
        "current_item_offset": 0,
        "active_segment": _active_segment(first),
        "last_sequence": 0,
        "last_event_sha256": None,
        "capture_write_count": 0,
        "formal_write_count": 0,
    }
    if manifest.get("saving_contract") is not None:
        state.update(
            saving_contract_sha256=manifest["saving_contract_sha256"],
            episode_outcomes={
                "correct": 0,
                "screened_correct": 0,
                "prompted_correct": 0,
                "wrong": 0,
                "unresolved": 0,
            },
        )
    return state


def _load_state(run_dir: Path) -> dict[str, Any]:
    state = _read_json(run_dir / "active-state.json", "active state", MAX_STATE_BYTES)
    if state.get("schema") != STATE_SCHEMA or state.get("run_id") != run_dir.name:
        raise CampaignRuntimeError("active state identity drifted")
    size = (run_dir / "active-state.json").stat().st_size
    if size > MAX_STATE_BYTES:
        raise CampaignRuntimeError("active state exceeded its constant segment budget")
    active = state.get("active_segment")
    if active is not None:
        if not isinstance(active, dict) or len(active.get("items") or []) > SEGMENT_SIZE:
            raise CampaignRuntimeError("active state contains more than one segment")
    return state


def _load_run(run_dir: Path) -> dict[str, Any]:
    run = _read_json(run_dir / "run.json", "campaign run", MAX_SEGMENT_BYTES)
    if run.get("schema") != RUN_SCHEMA or run.get("run_id") != run_dir.name:
        raise CampaignRuntimeError("campaign run identity drifted")
    return run


def _verify_state_binding(run_dir: Path, state: dict[str, Any]) -> None:
    run = _load_run(run_dir)
    manifest_path = run_dir / "manifest.json"
    if _sha256_file(manifest_path) != run.get("manifest_sha256"):
        raise CampaignRuntimeError("runtime manifest binding drifted")
    manifest = normalize_manifest(
        _read_json(manifest_path, "campaign manifest", MAX_MANIFEST_BYTES)
    )
    if (
        run.get("campaign_date") != manifest.get("campaign_date")
        or run.get("segment_count") != manifest.get("segment_count")
        or run.get("item_count") != manifest.get("item_count")
        or run.get("all_segments_ready") != manifest.get("all_segments_ready")
        or run.get("review_outcome_contract_version")
        != manifest.get("review_outcome_contract_version")
        or run.get("stable_hash_audit_count")
        != manifest.get("stable_hash_audit_count")
    ):
        raise CampaignRuntimeError("runtime run manifest aggregate drifted")
    run_contract, run_contract_sha = _saving_contract(run)
    manifest_contract, manifest_contract_sha = _saving_contract(manifest)
    if (run_contract, run_contract_sha) != (manifest_contract, manifest_contract_sha):
        raise CampaignRuntimeError("runtime run saving contract drifted")
    if (
        state.get("campaign_date") != run.get("campaign_date")
        or state.get("segment_count") != run.get("segment_count")
        or state.get("item_count") != run.get("item_count")
        or state.get("capture_write_count") != 0
        or state.get("formal_write_count") != 0
    ):
        raise CampaignRuntimeError("active state aggregate binding drifted")
    completed = state.get("completed_items")
    ratings = state.get("ratings")
    v2 = run_contract == SAVING_CONTRACT_V2_SCHEMA
    episode_outcomes = state.get("episode_outcomes")
    if (
        not isinstance(completed, int)
        or completed < 0
        or not isinstance(ratings, dict)
        or set(ratings) != {"Again", "Hard", "Good"}
        or any(not isinstance(ratings[key], int) or ratings[key] < 0 for key in ratings)
        or (not v2 and sum(ratings.values()) != completed)
        or (v2 and sum(ratings.values()) != 0)
        or (
            v2
            and (
                state.get("saving_contract_sha256") != run_contract_sha
                or not isinstance(episode_outcomes, dict)
                or set(episode_outcomes)
                != {"correct", "screened_correct", "prompted_correct", "wrong", "unresolved"}
                or any(
                    not isinstance(value, int) or value < 0
                    for value in episode_outcomes.values()
                )
                or sum(episode_outcomes.values()) != completed
            )
        )
    ):
        raise CampaignRuntimeError("active state completion binding drifted")
    events = _read_events(run_dir)
    if len(events) != state.get("last_sequence"):
        raise CampaignRuntimeError("active state event sequence drifted")
    if events:
        previous_sha: str | None = None
        for expected_sequence, event in enumerate(events, start=1):
            core = {
                key: value
                for key, value in event.items()
                if key not in {"event_id", "event_sha256"}
            }
            expected_sha = _digest(core)
            if (
                event.get("sequence") != expected_sequence
                or event.get("previous_event_sha256") != previous_sha
                or event.get("event_sha256") != expected_sha
                or event.get("event_id")
                != f"MCE-{expected_sha[:24].upper()}"
            ):
                raise CampaignRuntimeError("runtime event chain drifted")
            previous_sha = expected_sha
        policy_start = _intent_policy_start(run_dir)
        required_last_intent: dict[str, Any] | None = None
        if policy_start is not None:
            for event in events:
                sequence = int(event["sequence"])
                if sequence < policy_start:
                    continue
                intent_path = _intent_path(
                    run_dir, str(event.get("idempotency_key") or "")
                )
                if not intent_path.is_file():
                    raise CampaignRuntimeError(
                        "required runtime transaction intent is missing"
                    )
                intent = _read_json(
                    intent_path, "runtime transaction intent", MAX_MANIFEST_BYTES
                )
                if intent.get("schema") != TRANSACTION_INTENT_SCHEMA_V2:
                    raise CampaignRuntimeError(
                        "required runtime transaction intent schema drifted"
                    )
                _verify_transaction_intent_v2(run_dir, intent)
                if intent.get("event") != event:
                    raise CampaignRuntimeError(
                        "runtime transaction intent event drifted"
                    )
                required_last_intent = intent
        last = events[-1]
        if last.get("event_sha256") != state.get("last_event_sha256"):
            raise CampaignRuntimeError("active state event hash drifted")
        latest_intent = required_last_intent
        if latest_intent is None:
            intent_path = _intent_path(
                run_dir, str(last.get("idempotency_key") or "")
            )
            if intent_path.is_file():
                latest_intent = _read_json(
                    intent_path, "runtime transaction intent", MAX_MANIFEST_BYTES
                )
                schema = latest_intent.get("schema")
                if schema == TRANSACTION_INTENT_SCHEMA_V2:
                    _verify_transaction_intent_v2(run_dir, latest_intent)
                elif schema != TRANSACTION_INTENT_SCHEMA_V1:
                    raise CampaignRuntimeError(
                        "runtime transaction intent schema drifted"
                    )
        if latest_intent is not None:
            if latest_intent.get("committed_state") != state:
                raise CampaignRuntimeError("active state transaction binding drifted")
    elif state.get("last_event_sha256") is not None:
        raise CampaignRuntimeError("empty runtime state event hash drifted")
    status = state.get("status")
    if status == "completed":
        if state.get("active_segment") is not None or completed != run.get("item_count"):
            raise CampaignRuntimeError("completed state frontier drifted")
        return
    if status not in {
        "preparing",
        "ready",
        "active",
        "paused",
        "failure_pending_capture",
        "episode_pending_capture",
        "carried_over",
    }:
        raise CampaignRuntimeError("active state status invalid")
    ordinal = state.get("current_segment_ordinal")
    offset = state.get("current_item_offset")
    if not isinstance(ordinal, int) or not isinstance(offset, int):
        raise CampaignRuntimeError("active state coordinate invalid")
    segment = _load_segment(run_dir, ordinal)
    manifest_segments = manifest.get("segments")
    if not isinstance(manifest_segments, list) or not 0 <= ordinal < len(
        manifest_segments
    ):
        raise CampaignRuntimeError("runtime manifest segment missing")
    expected_segment = {
        "schema": SEGMENT_SCHEMA,
        "run_id": run_dir.name,
        **manifest_segments[ordinal],
    }
    if segment != expected_segment:
        raise CampaignRuntimeError("runtime segment manifest binding drifted")
    expected_active = _active_segment(segment, completed=offset)
    if state.get("active_segment") != expected_active:
        raise CampaignRuntimeError("active state immutable segment drifted")
    expected_completed = ordinal * SEGMENT_SIZE + offset
    if completed != expected_completed:
        raise CampaignRuntimeError("active state completed frontier drifted")


def _load_verified_state(run_dir: Path) -> dict[str, Any]:
    state = _load_state(run_dir)
    _verify_state_binding(run_dir, state)
    return state


def _content_receipt(run_dir: Path, receipt: dict[str, Any]) -> tuple[Path, str]:
    digest = _digest(receipt)
    path = run_dir / "receipts" / f"{digest}.json"
    if path.exists():
        if _read_json(path, "operation receipt", MAX_RECEIPT_BYTES) != receipt:
            raise CampaignRuntimeError("content-addressed receipt collision")
    else:
        _atomic_write(path, receipt)
    return path, digest


def _idempotency_path(run_dir: Path, key: str) -> Path:
    return run_dir / "idempotency" / f"{hashlib.sha256(key.encode()).hexdigest()}.json"


def _intent_path(run_dir: Path, key: str) -> Path:
    return run_dir / "transaction-intents" / (
        hashlib.sha256(key.encode()).hexdigest() + ".json"
    )


def _intent_policy_path(run_dir: Path) -> Path:
    return run_dir / "transaction-intent-policy.json"


def _intent_policy_start(run_dir: Path) -> int | None:
    path = _intent_policy_path(run_dir)
    if not path.exists():
        return None
    policy = _read_json(path, "runtime transaction intent policy", MAX_SEGMENT_BYTES)
    start = policy.get("required_from_sequence")
    if (
        policy.get("schema") != TRANSACTION_INTENT_POLICY_SCHEMA
        or policy.get("run_id") != run_dir.name
        or not isinstance(start, int)
        or start < 1
        or policy.get("required_schema") != TRANSACTION_INTENT_SCHEMA_V2
        or policy.get("formal_write_count") != 0
    ):
        raise CampaignRuntimeError("runtime transaction intent policy drifted")
    return start


def _ensure_intent_policy(run_dir: Path, sequence: int) -> None:
    start = _intent_policy_start(run_dir)
    if start is not None:
        if start > sequence:
            raise CampaignRuntimeError("runtime transaction intent policy sequence drifted")
        return
    _atomic_write(
        _intent_policy_path(run_dir),
        {
            "schema": TRANSACTION_INTENT_POLICY_SCHEMA,
            "run_id": run_dir.name,
            "required_from_sequence": sequence,
            "required_schema": TRANSACTION_INTENT_SCHEMA_V2,
            "formal_write_count": 0,
        },
    )


def _expected_transaction_state(
    run_dir: Path,
    previous_state: dict[str, Any],
    event: dict[str, Any],
) -> dict[str, Any]:
    event_type = event.get("event_type")
    payload = event.get("payload")
    if not isinstance(payload, dict):
        raise CampaignRuntimeError("runtime transaction payload drifted")
    prior_status = previous_state.get("status")

    if event_type == "run_started":
        if prior_status != "ready":
            raise CampaignRuntimeError("runtime transaction start transition drifted")
        next_state = {**previous_state, "status": "active"}
        run = _load_run(run_dir)
        _kind, contract_sha = _saving_contract(run)
        if payload.get("saving_contract_sha256") != contract_sha:
            raise CampaignRuntimeError("runtime transaction start contract drifted")
    elif event_type == "run_paused":
        if prior_status != "active":
            raise CampaignRuntimeError("runtime transaction pause transition drifted")
        next_state = {**previous_state, "status": "paused"}
    elif event_type == "run_resumed":
        if prior_status != "paused":
            raise CampaignRuntimeError("runtime transaction resume transition drifted")
        next_state = {**previous_state, "status": "active"}
    elif event_type == "run_carried_over":
        if prior_status != "paused":
            raise CampaignRuntimeError("runtime transaction carryover transition drifted")
        next_state = {**previous_state, "status": "carried_over"}
    elif event_type == "rapid_review_failure_pending":
        capture_request = payload.get("capture_authorization_request")
        if (
            prior_status != "active"
            or payload.get("capture_required") is not True
            or payload.get("rating") != "Again"
            or not isinstance(capture_request, dict)
            or capture_request.get("capture_required") is not True
            or capture_request.get("capture_authorized") is not False
            or capture_request.get("direct_capture_write") is not False
        ):
            raise CampaignRuntimeError("runtime transaction failure transition drifted")
        next_state = json.loads(json.dumps(previous_state))
        next_state["status"] = "failure_pending_capture"
        next_state["pending_failure"] = {
            "item_id": payload.get("item_id"),
            "segment_id": payload.get("segment_id"),
            "capture_request_id": capture_request.get("request_id"),
            "pending_outcome_sha256": payload.get("outcome_sha256"),
            "payload": payload,
        }
    elif event_type == "rapid_review_failure_finalized":
        if (
            prior_status != "failure_pending_capture"
            or payload.get("capture_verified") is not True
            or payload.get("capture_status") != fact_capture.PENDING_FORMAL_STATUS
            or payload.get("rating") != "Again"
        ):
            raise CampaignRuntimeError("runtime transaction finalize transition drifted")
        next_state = _advance_state_after_item(run_dir, previous_state, "Again")
    elif event_type == "rapid_review_outcome_recorded":
        rating = payload.get("rating")
        if (
            prior_status != "active"
            or rating not in {"Hard", "Good"}
            or payload.get("capture_required") is not False
        ):
            raise CampaignRuntimeError("runtime transaction outcome transition drifted")
        next_state = _advance_state_after_item(run_dir, previous_state, str(rating))
    elif event_type == "campaign_response_observed":
        if (
            prior_status != "active"
            or payload.get("capture_required") is not True
            or payload.get("episode_id")
            != f"{previous_state.get('run_id')}:{payload.get('item_id')}"
        ):
            raise CampaignRuntimeError("runtime episode observation transition drifted")
        next_state = json.loads(json.dumps(previous_state))
        next_state["status"] = "episode_pending_capture"
        next_state["pending_episode"] = {
            "episode_id": payload.get("episode_id"),
            "item_id": payload.get("item_id"),
            "segment_id": payload.get("segment_id"),
            "observation_sha256": payload.get("observation_sha256"),
            "payload": payload,
        }
    elif event_type == "campaign_episode_finalized":
        terminal_state = payload.get("terminal_state")
        if (
            prior_status != "episode_pending_capture"
            or terminal_state
            not in {"correct", "screened_correct", "prompted_correct", "wrong", "unresolved"}
            or payload.get("capture_verified") is not True
            or payload.get("package_verified") is not True
            or payload.get("formal_write_count") != 0
            or payload.get("fsrs_write_count") != 0
            or payload.get("personal_summary_write_count") != 0
        ):
            raise CampaignRuntimeError("runtime episode finalization transition drifted")
        next_state = _advance_state_after_episode(
            run_dir, previous_state, str(terminal_state)
        )
    else:
        raise CampaignRuntimeError("runtime transaction event type drifted")

    return {
        **next_state,
        "last_sequence": event.get("sequence"),
        "last_event_sha256": event.get("event_sha256"),
    }


def _verify_transaction_intent_v2(
    run_dir: Path, intent: dict[str, Any]
) -> None:
    previous_state = intent.get("previous_state")
    request = intent.get("request")
    event = intent.get("event")
    committed_state = intent.get("committed_state")
    receipt = intent.get("receipt")
    pointer = intent.get("pointer")
    if not all(
        isinstance(value, dict)
        for value in (
            previous_state,
            request,
            event,
            committed_state,
            receipt,
            pointer,
        )
    ):
        raise CampaignRuntimeError("runtime transaction v2 is incomplete")
    event_type = event.get("event_type")
    request_operation = {
        "run_started": "start",
        "rapid_review_outcome_recorded": "record-outcome",
        "rapid_review_failure_pending": "record-outcome",
        "rapid_review_failure_finalized": "finalize-capture",
        "run_paused": "pause",
        "run_resumed": "resume",
        "run_carried_over": "carryover",
        "campaign_response_observed": "record-outcome",
        "campaign_episode_finalized": "finalize-episode",
    }.get(event_type)
    event_sequence = event.get("sequence")
    event_core = {
        key: value for key, value in event.items() if key not in {"event_id", "event_sha256"}
    }
    expected_event_sha = _digest(event_core)
    expected_receipt_sha = _digest(receipt)
    if (
        intent.get("schema") != TRANSACTION_INTENT_SCHEMA_V2
        or intent.get("run_id") != run_dir.name
        or intent.get("formal_write_count") != 0
        or not isinstance(intent.get("idempotency_key"), str)
        or request_operation is None
        or request.get("operation") != request_operation
        or request.get("run_id") != run_dir.name
        or _digest(request) != intent.get("request_sha256")
        or not isinstance(event_sequence, int)
        or event_sequence < 1
        or previous_state.get("schema") != STATE_SCHEMA
        or previous_state.get("run_id") != run_dir.name
        or previous_state.get("last_sequence") != event_sequence - 1
        or previous_state.get("last_event_sha256")
        != event.get("previous_event_sha256")
        or event.get("schema") != EVENT_SCHEMA
        or event.get("run_id") != run_dir.name
        or event.get("idempotency_key") != intent.get("idempotency_key")
        or event.get("request_sha256") != intent.get("request_sha256")
        or event.get("event_sha256") != expected_event_sha
        or event.get("event_id") != f"MCE-{expected_event_sha[:24].upper()}"
        or receipt.get("schema") != RECEIPT_SCHEMA
        or receipt.get("status") != "committed"
        or receipt.get("run_id") != run_dir.name
        or receipt.get("operation") != event_type
        or receipt.get("event_id") != event.get("event_id")
        or receipt.get("event_sha256") != event.get("event_sha256")
        or receipt.get("sequence") != event.get("sequence")
        or receipt.get("idempotency_key") != intent.get("idempotency_key")
        or receipt.get("request_sha256") != intent.get("request_sha256")
        or receipt.get("state_status") != committed_state.get("status")
        or receipt.get("payload") != event.get("payload")
        or receipt.get("capture_write_count") != 0
        or receipt.get("formal_write_count") != 0
        or pointer.get("schema") != "morning-campaign-idempotency-pointer-v1"
        or pointer.get("run_id") != run_dir.name
        or pointer.get("idempotency_key") != intent.get("idempotency_key")
        or pointer.get("request_sha256") != intent.get("request_sha256")
        or pointer.get("receipt_ref") != f"receipts/{expected_receipt_sha}.json"
        or pointer.get("receipt_sha256") != expected_receipt_sha
    ):
        raise CampaignRuntimeError("runtime transaction v2 semantic binding drifted")
    expected_state = _expected_transaction_state(run_dir, previous_state, event)
    if committed_state != expected_state:
        raise CampaignRuntimeError("runtime transaction v2 state transition drifted")


def _recover_intent(
    run_dir: Path, key: str, request_sha256: str
) -> dict[str, Any] | None:
    path = _intent_path(run_dir, key)
    if not path.exists():
        return None
    intent = _read_json(path, "runtime transaction intent", MAX_MANIFEST_BYTES)
    intent_schema = intent.get("schema")
    if (
        intent_schema
        not in {TRANSACTION_INTENT_SCHEMA_V1, TRANSACTION_INTENT_SCHEMA_V2}
        or intent.get("idempotency_key") != key
        or intent.get("request_sha256") != request_sha256
    ):
        raise CampaignRuntimeError("runtime transaction intent drifted")
    event = intent.get("event")
    committed_state = intent.get("committed_state")
    receipt = intent.get("receipt")
    pointer = intent.get("pointer")
    if not all(isinstance(value, dict) for value in (event, committed_state, receipt, pointer)):
        raise CampaignRuntimeError("runtime transaction intent incomplete")
    if intent_schema == TRANSACTION_INTENT_SCHEMA_V2:
        _verify_transaction_intent_v2(run_dir, intent)
    if (
        event.get("run_id") != run_dir.name
        or event.get("idempotency_key") != key
        or event.get("request_sha256") != request_sha256
        or event.get("event_type")
        not in {
            "run_started",
            "rapid_review_outcome_recorded",
            "rapid_review_failure_pending",
            "rapid_review_failure_finalized",
            "run_paused",
            "run_resumed",
            "run_carried_over",
            "campaign_response_observed",
            "campaign_episode_finalized",
        }
        or committed_state.get("run_id") != run_dir.name
        or receipt.get("run_id") != run_dir.name
        or receipt.get("event_id") != event.get("event_id")
        or receipt.get("event_sha256") != event.get("event_sha256")
        or receipt.get("idempotency_key") != key
        or receipt.get("request_sha256") != request_sha256
        or pointer.get("schema") != "morning-campaign-idempotency-pointer-v1"
        or pointer.get("run_id") != run_dir.name
        or pointer.get("idempotency_key") != key
        or pointer.get("request_sha256") != request_sha256
    ):
        raise CampaignRuntimeError("runtime transaction semantic binding drifted")
    ledger = run_dir / "events.jsonl"
    existing_events = _read_events(run_dir)
    matches = [row for row in existing_events if row.get("event_id") == event.get("event_id")]
    if len(matches) > 1 or (matches and matches[0] != event):
        raise CampaignRuntimeError("runtime transaction event drifted")
    if not matches:
        with ledger.open("ab") as handle:
            handle.write(_canonical_bytes(event) + b"\n")
            handle.flush()
            os.fsync(handle.fileno())
    current = _load_state(run_dir)
    current_sequence = int(current.get("last_sequence", -1))
    event_sequence = int(event.get("sequence", -1))
    if current_sequence == event_sequence - 1:
        if current.get("last_event_sha256") != event.get("previous_event_sha256"):
            raise CampaignRuntimeError("runtime transaction predecessor drifted")
        _atomic_write(run_dir / "active-state.json", committed_state)
        current = committed_state
    elif current_sequence == event_sequence:
        if current != committed_state:
            raise CampaignRuntimeError("runtime transaction state drifted")
    elif current_sequence < event_sequence:
        raise CampaignRuntimeError("runtime transaction state sequence drifted")
    else:
        # A later valid operation may already have advanced state while this older
        # operation's pointer is being repaired. The full event chain and current
        # state are verified below; never overwrite the newer state.
        pass
    _verify_state_binding(run_dir, current)
    receipt_path, receipt_sha = _content_receipt(run_dir, receipt)
    if (
        receipt_sha != pointer.get("receipt_sha256")
        or receipt_path.relative_to(run_dir).as_posix()
        != pointer.get("receipt_ref")
    ):
        raise CampaignRuntimeError("runtime transaction receipt drifted")
    _atomic_write(_idempotency_path(run_dir, key), pointer)
    return {
        **receipt,
        "status": "already_committed",
        "receipt_ref": pointer["receipt_ref"],
        "receipt_sha256": pointer["receipt_sha256"],
    }


def _idempotent_lookup(
    run_dir: Path, key: str, request_sha256: str
) -> dict[str, Any] | None:
    pointer_path = _idempotency_path(run_dir, key)
    if not pointer_path.exists():
        recovered = _recover_intent(run_dir, key, request_sha256)
        if recovered is not None:
            return recovered
        return None
    pointer = _read_json(pointer_path, "idempotency pointer", MAX_SEGMENT_BYTES)
    if pointer.get("idempotency_key") != key or pointer.get("request_sha256") != request_sha256:
        raise CampaignRuntimeError("idempotency key was already used differently")
    receipt_path = run_dir / str(pointer.get("receipt_ref") or "")
    try:
        receipt = _read_json(
            receipt_path, "idempotent receipt", MAX_RECEIPT_BYTES
        )
    except CampaignRuntimeError:
        recovered = _recover_intent(run_dir, key, request_sha256)
        if recovered is not None:
            return recovered
        raise
    if _digest(receipt) != pointer.get("receipt_sha256"):
        recovered = _recover_intent(run_dir, key, request_sha256)
        if recovered is not None:
            return recovered
        raise CampaignRuntimeError("idempotent receipt binding drifted")
    return {
        **receipt,
        "status": "already_committed",
        "receipt_ref": str(pointer.get("receipt_ref")),
        "receipt_sha256": str(pointer.get("receipt_sha256")),
    }


def _append_operation(
    run_dir: Path,
    state: dict[str, Any],
    *,
    event_type: str,
    payload: dict[str, Any],
    request: dict[str, Any],
    idempotency_key: str,
    event_time: str | None,
    new_state: dict[str, Any],
) -> dict[str, Any]:
    key = _safe_id(idempotency_key, "idempotency_key")
    request_sha = _digest(request)
    repeated = _idempotent_lookup(run_dir, key, request_sha)
    if repeated is not None:
        return repeated
    timestamp = _safe_timestamp(event_time)
    sequence = int(state["last_sequence"]) + 1
    _ensure_intent_policy(run_dir, sequence)
    event_core = {
        "schema": EVENT_SCHEMA,
        "run_id": state["run_id"],
        "sequence": sequence,
        "event_type": event_type,
        "event_time": timestamp,
        "idempotency_key": key,
        "request_sha256": request_sha,
        "previous_event_sha256": state.get("last_event_sha256"),
        "payload": payload,
    }
    event_sha = _digest(event_core)
    event = {**event_core, "event_id": f"MCE-{event_sha[:24].upper()}", "event_sha256": event_sha}
    committed_state = {
        **new_state,
        "last_sequence": sequence,
        "last_event_sha256": event_sha,
    }
    receipt = {
        "schema": RECEIPT_SCHEMA,
        "status": "committed",
        "run_id": state["run_id"],
        "operation": event_type,
        "event_id": event["event_id"],
        "event_sha256": event_sha,
        "sequence": sequence,
        "idempotency_key": key,
        "request_sha256": request_sha,
        "state_status": committed_state["status"],
        "payload": payload,
        "capture_write_count": 0,
        "formal_write_count": 0,
    }
    receipt_sha = _digest(receipt)
    receipt_ref = f"receipts/{receipt_sha}.json"
    pointer = {
        "schema": "morning-campaign-idempotency-pointer-v1",
        "run_id": state["run_id"],
        "idempotency_key": key,
        "request_sha256": request_sha,
        "receipt_ref": receipt_ref,
        "receipt_sha256": receipt_sha,
    }
    intent = {
        "schema": TRANSACTION_INTENT_SCHEMA_V2,
        "run_id": state["run_id"],
        "idempotency_key": key,
        "request_sha256": request_sha,
        "request": json.loads(json.dumps(request)),
        "previous_state": json.loads(json.dumps(state)),
        "event": event,
        "committed_state": committed_state,
        "receipt": receipt,
        "pointer": pointer,
        "formal_write_count": 0,
    }
    intent_path = _intent_path(run_dir, key)
    if intent_path.exists():
        if _read_json(intent_path, "runtime transaction intent", MAX_MANIFEST_BYTES) != intent:
            raise CampaignRuntimeError("runtime transaction intent conflict")
    else:
        _atomic_write(intent_path, intent)
    recovered = _recover_intent(run_dir, key, request_sha)
    if recovered is None:
        raise CampaignRuntimeError("runtime transaction recovery missing")
    return {**receipt, "receipt_ref": pointer["receipt_ref"], "receipt_sha256": receipt_sha}


def create_run(runtime_root: str | Path, manifest_path: str | Path) -> dict[str, Any]:
    source = Path(manifest_path).expanduser().resolve()
    raw = _read_json(source, "campaign manifest", MAX_MANIFEST_BYTES)
    manifest = normalize_manifest(raw)
    root = Path(runtime_root).expanduser().resolve()
    run_dir = _run_dir(root, manifest["run_id"])
    with _runtime_lock(root):
        if run_dir.exists():
            prior = _read_json(run_dir / "manifest.json", "existing manifest", MAX_MANIFEST_BYTES)
            if prior != manifest:
                raise CampaignRuntimeError("run_id is already bound to another manifest")
            state = _load_verified_state(run_dir)
            return {
                "status": "already_created",
                "run_id": manifest["run_id"],
                "state_status": state["status"],
                "manifest_sha256": _sha256_file(run_dir / "manifest.json"),
                "active_state_size_bytes": (run_dir / "active-state.json").stat().st_size,
                "capture_write_count": 0,
                "formal_write_count": 0,
                **(
                    {
                        "saving_contract_sha256": manifest[
                            "saving_contract_sha256"
                        ]
                    }
                    if manifest.get("saving_contract_sha256")
                    else {}
                ),
            }
        for directory in (run_dir, run_dir / "segments", run_dir / "receipts", run_dir / "idempotency", run_dir / "transaction-intents", run_dir / "carryovers"):
            _ensure_dir(directory)
        manifest_sha = _atomic_write(run_dir / "manifest.json", manifest)
        for segment in manifest["segments"]:
            object_value = {
                "schema": SEGMENT_SCHEMA,
                "run_id": manifest["run_id"],
                **segment,
            }
            _atomic_write(_segment_path(run_dir, segment["ordinal"]), object_value)
        run = {
            "schema": RUN_SCHEMA,
            "run_id": manifest["run_id"],
            "campaign_date": manifest["campaign_date"],
            "timezone": "Asia/Shanghai",
            "manifest_sha256": manifest_sha,
            "segment_count": manifest["segment_count"],
            "item_count": manifest["item_count"],
            "all_segments_ready": manifest["all_segments_ready"],
            "review_outcome_contract_version": manifest[
                "review_outcome_contract_version"
            ],
            "stable_hash_audit_count": manifest["stable_hash_audit_count"],
            "capture_write_count": 0,
            "formal_write_count": 0,
        }
        if manifest.get("saving_contract") is not None:
            run.update(
                saving_contract=manifest["saving_contract"],
                saving_contract_sha256=manifest["saving_contract_sha256"],
            )
        _atomic_write(run_dir / "run.json", run)
        _atomic_write(run_dir / "active-state.json", _initial_state(manifest))
        (run_dir / "events.jsonl").touch(mode=0o600)
        _ensure_intent_policy(run_dir, 1)
        return {
            "status": "created",
            "run_id": manifest["run_id"],
            "state_status": "ready" if manifest["all_segments_ready"] else "preparing",
            "manifest_sha256": manifest_sha,
            "segment_count": manifest["segment_count"],
            "item_count": manifest["item_count"],
            "stable_hash_audit_count": manifest["stable_hash_audit_count"],
            "active_state_size_bytes": (run_dir / "active-state.json").stat().st_size,
            "capture_write_count": 0,
            "formal_write_count": 0,
            **(
                {"saving_contract_sha256": manifest["saving_contract_sha256"]}
                if manifest.get("saving_contract_sha256")
                else {}
            ),
        }


def start_run(
    runtime_root: str | Path,
    run_id: str,
    *,
    idempotency_key: str,
    event_time: str | None = None,
) -> dict[str, Any]:
    root = Path(runtime_root).expanduser().resolve()
    run_dir = _run_dir(root, run_id)
    request = {"operation": "start", "run_id": run_id}
    with _runtime_lock(root):
        state = _load_verified_state(run_dir)
        request_sha = _digest(request)
        repeated = _idempotent_lookup(run_dir, idempotency_key, request_sha)
        if repeated is not None:
            return repeated
        run = _load_run(run_dir)
        if not run["all_segments_ready"] or state["status"] == "preparing":
            raise CampaignRuntimeError("all segments must be ready before start")
        if state["status"] != "ready":
            raise CampaignRuntimeError(f"run cannot start from {state['status']}")
        new_state = {**state, "status": "active"}
        return _append_operation(
            run_dir,
            state,
            event_type="run_started",
            payload={
                "segment_count": run["segment_count"],
                "item_count": run["item_count"],
                "saving_contract_sha256": run.get("saving_contract_sha256"),
            },
            request=request,
            idempotency_key=idempotency_key,
            event_time=event_time,
            new_state=new_state,
        )


def _current_item(state: dict[str, Any]) -> dict[str, Any]:
    segment = state.get("active_segment")
    if not isinstance(segment, dict):
        raise CampaignRuntimeError("run has no active segment")
    offset = state.get("current_item_offset")
    items = segment.get("items")
    if not isinstance(offset, int) or not isinstance(items, list) or not 0 <= offset < len(items):
        raise CampaignRuntimeError("active item frontier drifted")
    item = items[offset]
    if not isinstance(item, dict):
        raise CampaignRuntimeError("active item is invalid")
    return item


def _classification(
    *,
    choice_result: str,
    reasoning_result: str,
    confidence: str,
    prompt_dependency: str,
    audit_performed: bool,
) -> tuple[str, str, bool]:
    failure = (
        choice_result != "correct"
        or reasoning_result in {"partial", "diverged"}
        or prompt_dependency != "none"
    )
    if failure:
        return "failure", "Again", True
    if audit_performed:
        if reasoning_result != "sound":
            raise CampaignRuntimeError("audited correct requires reasoning_result=sound")
        if confidence == "low":
            return "audited_conservative", "Hard", False
        return "audited_sound", "Good", False
    return "screened_correct", "Hard", False


def _advance_state_after_item(
    run_dir: Path, state: dict[str, Any], rating: str
) -> dict[str, Any]:
    new_state = json.loads(json.dumps(state))
    new_state.pop("pending_failure", None)
    new_state["status"] = "active"
    new_state["completed_items"] += 1
    new_state["ratings"][rating] += 1
    new_state["active_segment"]["completed_count"] += 1
    next_offset = state["current_item_offset"] + 1
    if next_offset < state["active_segment"]["item_count"]:
        new_state["current_item_offset"] = next_offset
    elif state["current_segment_ordinal"] + 1 < state["segment_count"]:
        next_ordinal = state["current_segment_ordinal"] + 1
        next_segment = _load_segment(run_dir, next_ordinal)
        new_state["current_segment_ordinal"] = next_ordinal
        new_state["current_item_offset"] = 0
        new_state["active_segment"] = _active_segment(next_segment)
    else:
        new_state["status"] = "completed"
        new_state["current_item_offset"] = 0
        new_state["active_segment"] = None
    return new_state


def _advance_state_after_episode(
    run_dir: Path, state: dict[str, Any], terminal_state: str
) -> dict[str, Any]:
    new_state = json.loads(json.dumps(state))
    new_state.pop("pending_episode", None)
    new_state["status"] = "active"
    new_state["completed_items"] += 1
    new_state["episode_outcomes"][terminal_state] += 1
    new_state["active_segment"]["completed_count"] += 1
    next_offset = state["current_item_offset"] + 1
    if next_offset < state["active_segment"]["item_count"]:
        new_state["current_item_offset"] = next_offset
    elif state["current_segment_ordinal"] + 1 < state["segment_count"]:
        next_ordinal = state["current_segment_ordinal"] + 1
        next_segment = _load_segment(run_dir, next_ordinal)
        new_state["current_segment_ordinal"] = next_ordinal
        new_state["current_item_offset"] = 0
        new_state["active_segment"] = _active_segment(next_segment)
    else:
        new_state["status"] = "completed"
        new_state["current_item_offset"] = 0
        new_state["active_segment"] = None
    return new_state


@study_commit_boundary
def record_outcome(
    runtime_root: str | Path,
    run_id: str,
    *,
    repo_root: str | Path,
    item_id: str,
    observed_date: str,
    choice_result: str,
    reasoning_result: str,
    confidence: str,
    prompt_dependency: str,
    audit_performed: bool,
    recognized_original: bool = False,
    response_seconds: float | None = None,
    response_token: str | None = None,
    idempotency_key: str,
    event_time: str | None = None,
) -> dict[str, Any]:
    root = Path(runtime_root).expanduser().resolve()
    run_dir = _run_dir(root, run_id)
    normalized = {
        "item_id": _safe_id(item_id, "item_id", ITEM_ID_RE),
        "observed_date": _safe_date(observed_date, "observed_date"),
        "choice_result": choice_result,
        "reasoning_result": reasoning_result,
        "confidence": confidence,
        "prompt_dependency": prompt_dependency,
        "audit_performed": audit_performed,
        "recognized_original": recognized_original,
        "response_seconds": response_seconds,
        "response_token": response_token,
    }
    if choice_result not in CHOICE_RESULTS:
        raise CampaignRuntimeError("choice_result is invalid")
    if reasoning_result not in REASONING_RESULTS:
        raise CampaignRuntimeError("reasoning_result is invalid")
    if confidence not in CONFIDENCE_LEVELS:
        raise CampaignRuntimeError("confidence is invalid")
    if prompt_dependency not in PROMPT_DEPENDENCIES:
        raise CampaignRuntimeError("prompt_dependency is invalid")
    if not isinstance(audit_performed, bool):
        raise CampaignRuntimeError("audit_performed must be boolean")
    if not isinstance(recognized_original, bool):
        raise CampaignRuntimeError("recognized_original must be boolean")
    if response_seconds is not None and not 0 < response_seconds <= 7200:
        raise CampaignRuntimeError(
            "response_seconds must be > 0 and <= 7200; use null when unmeasured"
        )
    if response_token is not None and response_token not in {
        "A", "B", "C", "D", "BLANK", "UNCERTAIN"
    }:
        raise CampaignRuntimeError("response_token is invalid")
    canonical_repo = Path(repo_root).expanduser().resolve()
    timestamp = _safe_timestamp(event_time)
    request = {"operation": "record-outcome", "run_id": run_id, **normalized}
    with _runtime_lock(root):
        request_sha = _digest(request)
        repeated = _idempotent_lookup(run_dir, idempotency_key, request_sha)
        if repeated is not None:
            return repeated
        state = _load_verified_state(run_dir)
        if state["status"] != "active":
            raise CampaignRuntimeError(f"outcome cannot be recorded from {state['status']}")
        item = _current_item(state)
        saving_kind, saving_contract_sha = _saving_contract(_load_run(run_dir))
        v2 = saving_kind == SAVING_CONTRACT_V2_SCHEMA
        if item["item_id"] != item_id:
            raise CampaignRuntimeError("item_id is not the current campaign frontier")
        if recognized_original and item["surface_kind"] not in {
            "canonical_original",
            "formal_original",
        }:
            raise CampaignRuntimeError(
                "recognized_original is only valid for an original"
            )
        audit_required = bool(
            item["stable_hash_audit"]
            or item["mandatory_audit"]
            or confidence == "low"
            or recognized_original
            or item["first_variant"]
            or item["recent_recurrence"]
        )
        known_failure = reasoning_result in {"partial", "diverged"} or prompt_dependency != "none"
        if (
            not v2
            and choice_result == "correct"
            and audit_required
            and not audit_performed
            and not known_failure
        ):
            raise CampaignRuntimeError("current item requires a forced semantic audit")
        if choice_result == "correct" and audit_performed and reasoning_result == "not_observed":
            raise CampaignRuntimeError("semantic audit must observe reasoning")
        outcome_class, rating, capture_required = _classification(
            choice_result=choice_result,
            reasoning_result=reasoning_result,
            confidence=confidence,
            prompt_dependency=prompt_dependency,
            audit_performed=audit_performed,
        )
        response_mode = "choice_plus_reason" if audit_performed else "choice_only"
        screened_correct = outcome_class == "screened_correct"
        evidence = {
            "formal_node_id": item.get("formal_node_id"),
            "observed_date": normalized["observed_date"],
            "choice_result": choice_result,
            "reasoning_result": reasoning_result,
            "confidence": confidence,
            "prompt_dependency": prompt_dependency,
            "response_mode": response_mode,
            "screened_correct": screened_correct,
            "reason_audited": audit_performed,
            "recognized_original": recognized_original,
            "true_item_id": item["true_item_id"],
            "variant_id": item["variant_id"],
            "response_seconds": response_seconds,
        }
        scheduler_handoff = {
            "schema": "rapid-review-scheduler-handoff-v1",
            "admission_independent": True,
            "campaign_evidence_committed": True,
            "campaign_target_complete": not capture_required,
            "normalized_evidence": evidence,
            "formalization_scope": "identity_binding_or_refresh_only",
            "must_not_repeat_campaign_target": True,
        }
        outcome_core = {
            "schema": OUTCOME_SCHEMA,
            "run_id": run_id,
            "campaign_date": state["campaign_date"],
            "segment_id": state["active_segment"]["segment_id"],
            "segment_ordinal": state["current_segment_ordinal"],
            "item_id": item_id,
            "source_id": item["source_id"],
            "action_id": item.get("action_id"),
            "formal_node_id": item.get("formal_node_id"),
            "true_item_id": item["true_item_id"],
            "variant_id": item["variant_id"],
            "surface_kind": item["surface_kind"],
            "campaign_item_commitment_sha256": item[
                "campaign_item_commitment_sha256"
            ],
            "item_commitment": {
                "surface_sha256": item["surface_sha256"],
                "grading_sha256": item["grading_sha256"],
                "pack_commitment_sha256": item["pack_commitment_sha256"],
            },
            "audit_required": audit_required,
            "audit_performed": audit_performed,
            "outcome_class": outcome_class,
            "rating": rating,
            "normalized_evidence": evidence,
            "capture_required": capture_required,
            "capture_write_count": 0,
            "formal_write_count": 0,
        }
        provisional_outcome_sha = _digest(outcome_core)
        if v2:
            if response_token is None:
                raise CampaignRuntimeError("saving-contract-v2 requires response_token")
            observation_core = {
                "schema": "morning-campaign-response-observation-v2",
                "saving_contract_sha256": saving_contract_sha,
                "episode_id": f"{run_id}:{item_id}",
                "run_id": run_id,
                "campaign_date": state["campaign_date"],
                "observed_date": normalized["observed_date"],
                "segment_id": state["active_segment"]["segment_id"],
                "segment_ordinal": state["current_segment_ordinal"],
                "item_id": item_id,
                "source_id": item["source_id"],
                "formal_node_id": item.get("formal_node_id"),
                "true_item_id": item["true_item_id"],
                "variant_id": item["variant_id"],
                "surface_kind": item["surface_kind"],
                "campaign_item_commitment_sha256": item[
                    "campaign_item_commitment_sha256"
                ],
                "first_response": response_token,
                "first_result": choice_result,
                "initial_reasoning_result": reasoning_result,
                "initial_prompt_dependency": prompt_dependency,
                "confidence": confidence,
                "recognized_original": recognized_original,
                "response_seconds": response_seconds,
                "audit_required": audit_required,
                "audit_performed": audit_performed,
                "capture_required": True,
                "formal_write_count": 0,
                "fsrs_write_count": 0,
                "personal_summary_write_count": 0,
            }
            observation_sha = _digest(observation_core)
            payload = {
                **observation_core,
                "observation_sha256": observation_sha,
            }
            new_state = json.loads(json.dumps(state))
            new_state["status"] = "episode_pending_capture"
            new_state["pending_episode"] = {
                "episode_id": payload["episode_id"],
                "item_id": item_id,
                "segment_id": payload["segment_id"],
                "observation_sha256": observation_sha,
                "payload": payload,
            }
            return _append_operation(
                run_dir,
                state,
                event_type="campaign_response_observed",
                payload=payload,
                request=request,
                idempotency_key=idempotency_key,
                event_time=timestamp,
                new_state=new_state,
            )
        capture_request = None
        if capture_required:
            capture_request = {
                "schema": CAPTURE_REQUEST_SCHEMA,
                "request_id": f"RCR-{provisional_outcome_sha[:24].upper()}",
                "requested": True,
                "capture_required": True,
                "capture_authorized": False,
                "direct_capture_write": False,
                "run_id": run_id,
                "segment_id": outcome_core["segment_id"],
                "item_id": item_id,
                "outcome_sha256": provisional_outcome_sha,
                "formal_write_authorized": False,
            }
        canonical_key = (
            f"campaign-review:{run_id}:{item_id}:"
            f"{item['campaign_item_commitment_sha256']}"
        )
        try:
            canonical_commit = canonical_review_writer.record_campaign_outcome_hot(
                canonical_repo,
                run_id=run_id,
                segment_id=outcome_core["segment_id"],
                item_id=item_id,
                source_id=item["source_id"],
                formal_node_id=item.get("formal_node_id"),
                true_item_id=item["true_item_id"],
                variant_id=item["variant_id"],
                surface_kind=item["surface_kind"],
                campaign_item_commitment_sha256=item[
                    "campaign_item_commitment_sha256"
                ],
                choice_result=choice_result,
                reasoning_result=reasoning_result,
                confidence=confidence,
                prompt_dependency=prompt_dependency,
                response_mode=response_mode,
                screened_correct=screened_correct,
                reason_audited=audit_performed,
                recognized_original=recognized_original,
                response_seconds=response_seconds,
                campaign_rating=rating,
                capture_required=capture_required,
                event_time=timestamp,
                observed_date=normalized["observed_date"],
                idempotency_key=canonical_key,
            )
        except Exception as exc:
            raise CampaignRuntimeError(
                f"canonical_review_outcome_commit_failed:{exc}"
            ) from exc
        verification = canonical_commit.get("verification") or {}
        if verification.get("status") != "verified":
            raise CampaignRuntimeError("canonical_review_outcome_commit_unverified")
        canonical_binding = {
            "event_id": str((canonical_commit.get("event") or {}).get("event_id") or ""),
            "event_sha256": str(verification.get("event_sha256") or ""),
            "commit_receipt_ref": str(
                canonical_commit.get("commit_receipt_ref") or ""
            ),
            "commit_receipt_sha256": str(
                canonical_commit.get("commit_receipt_sha256") or ""
            ),
            "verification_status": "verified",
        }
        if (
            not canonical_binding["event_id"]
            or not _safe_sha(
                canonical_binding["event_sha256"], "canonical event SHA-256"
            )
            or not canonical_binding["commit_receipt_ref"]
            or not _safe_sha(
                canonical_binding["commit_receipt_sha256"],
                "canonical commit receipt SHA-256",
            )
        ):
            raise CampaignRuntimeError("canonical_review_outcome_binding_invalid")
        scheduler_handoff["campaign_evidence_committed"] = True
        scheduler_handoff["canonical_review_event_id"] = canonical_binding[
            "event_id"
        ]
        scheduler_handoff["canonical_review_event_sha256"] = canonical_binding[
            "event_sha256"
        ]
        payload = {
            **outcome_core,
            "outcome_sha256": provisional_outcome_sha,
            "capture_authorization_request": capture_request,
            "scheduler_replay": evidence,
            "scheduler_handoff": scheduler_handoff,
            "canonical_review_commit": canonical_binding,
        }
        if capture_required:
            new_state = json.loads(json.dumps(state))
            new_state["status"] = "failure_pending_capture"
            new_state["pending_failure"] = {
                "item_id": item_id,
                "segment_id": outcome_core["segment_id"],
                "capture_request_id": capture_request["request_id"],
                "pending_outcome_sha256": provisional_outcome_sha,
                "payload": payload,
            }
            event_type = "rapid_review_failure_pending"
        else:
            new_state = _advance_state_after_item(run_dir, state, rating)
            event_type = "rapid_review_outcome_recorded"
        return _append_operation(
            run_dir,
            state,
            event_type=event_type,
            payload=payload,
            request=request,
            idempotency_key=idempotency_key,
            event_time=timestamp,
            new_state=new_state,
        )


def finalize_episode_capture(
    runtime_root: str | Path,
    run_id: str,
    *,
    repo_root: str | Path,
    item_id: str,
    terminal_state: str,
    final_response: str | None,
    reasoning_result: str,
    prompt_dependency: str,
    capture_proof: dict[str, Any],
    package_root: str | Path | None = None,
    idempotency_key: str,
    event_time: str | None = None,
) -> dict[str, Any]:
    """Advance a v2 item only after reopening its unified episode Capture."""

    if terminal_state not in {
        "correct", "screened_correct", "prompted_correct", "wrong", "unresolved"
    }:
        raise CampaignRuntimeError("terminal_state is invalid")
    if reasoning_result not in REASONING_RESULTS:
        raise CampaignRuntimeError("reasoning_result is invalid")
    if prompt_dependency not in PROMPT_DEPENDENCIES | {"not_observed"}:
        raise CampaignRuntimeError("prompt_dependency is invalid")
    if final_response is not None and final_response not in {
        "A", "B", "C", "D", "BLANK", "UNCERTAIN"
    }:
        raise CampaignRuntimeError("final_response is invalid")
    if terminal_state == "unresolved" and final_response is not None:
        raise CampaignRuntimeError("unresolved terminal must not fabricate final_response")
    if terminal_state != "unresolved" and final_response is None:
        raise CampaignRuntimeError("resolved terminal requires final_response")
    if not isinstance(capture_proof, dict):
        raise CampaignRuntimeError("episode capture proof is invalid")

    root = Path(runtime_root).expanduser().resolve()
    run_dir = _run_dir(root, run_id)
    request = {
        "operation": "finalize-episode",
        "run_id": run_id,
        "item_id": _safe_id(item_id, "item_id", ITEM_ID_RE),
        "terminal_state": terminal_state,
        "final_response": final_response,
        "reasoning_result": reasoning_result,
        "prompt_dependency": prompt_dependency,
        "capture_receipt_ref": capture_proof.get("receipt_ref"),
        "capture_receipt_sha256": capture_proof.get("receipt_sha256"),
    }
    with _runtime_lock(root):
        request_sha = _digest(request)
        repeated = _idempotent_lookup(run_dir, idempotency_key, request_sha)
        if repeated is not None:
            return repeated
        state = _load_verified_state(run_dir)
        run = _load_run(run_dir)
        saving_kind, saving_contract_sha = _saving_contract(run)
        if saving_kind != SAVING_CONTRACT_V2_SCHEMA:
            raise CampaignRuntimeError("finalize-episode requires saving-contract-v2")
        if (terminal_state in {"correct", "screened_correct", "prompted_correct"}
                and reasoning_result != "sound"):
            raise CampaignRuntimeError("correct_terminal_requires_sound_reasoning")
        if state.get("status") != "episode_pending_capture":
            raise CampaignRuntimeError(
                f"episode cannot be finalized from {state.get('status')}"
            )
        item = _current_item(state)
        pending = state.get("pending_episode")
        if (
            item.get("item_id") != item_id
            or not isinstance(pending, dict)
            or pending.get("episode_id") != f"{run_id}:{item_id}"
            or pending.get("item_id") != item_id
            or pending.get("segment_id") != state["active_segment"]["segment_id"]
        ):
            raise CampaignRuntimeError("pending episode binding drifted")
        try:
            import study_episode_capture_408 as episode_capture

            verified = episode_capture.verify_episode_capture(
                Path(repo_root).expanduser().resolve(),
                capture_proof.get("receipt_ref"),
                package_root=package_root,
            )
        except Exception as exc:
            raise CampaignRuntimeError(
                f"episode_capture_verification_failed:{exc}"
            ) from exc
        required_equal = (
            "receipt_ref",
            "receipt_sha256",
            "episode_id",
            "execution_kind",
            "study_date",
            "capture_id",
            "capture_payload_sha256",
            "capture_event_id",
            "capture_event_sha256",
            "conversation_package_locator",
            "conversation_package_sha256",
            "capture_joint_receipt_sha256",
            "capture_joint_manifest_sha256",
            "capture_bounded_manifest_sha256",
        )
        if any(verified.get(key) != capture_proof.get(key) for key in required_equal):
            raise CampaignRuntimeError("episode capture proof binding drifted")
        try:
            opened_episode = conversation_package.verify_package_binding(
                verified["conversation_package_locator"],
                expected_canonical_sha256=verified["conversation_package_sha256"],
                require_sealed=True,
                private_root=package_root,
            )
        except Exception as exc:
            raise CampaignRuntimeError(
                f"episode_package_semantic_verification_failed:{exc}"
            ) from exc
        source_wrapper = opened_episode.get("source")
        episode_source = (
            source_wrapper.get("source")
            if isinstance(source_wrapper, dict)
            else None
        )
        observed_outcome = (
            episode_source.get("observed_outcome")
            if isinstance(episode_source, dict)
            else None
        )
        display_binding = (
            episode_source.get("display_binding")
            if isinstance(episode_source, dict)
            else None
        )
        source_facts = (
            episode_source.get("source")
            if isinstance(episode_source, dict)
            else None
        )
        pending_payload = pending.get("payload") or {}
        expected_first_result = {
            "correct": "correct",
            "incorrect": "incorrect",
            "partial": "blank",
            "uncertain": "uncertain",
            "not_observed": "not_observed",
        }.get(pending_payload.get("first_result"))
        if (
            not isinstance(observed_outcome, dict)
            or observed_outcome.get("first_response")
            != pending_payload.get("first_response")
            or observed_outcome.get("first_result") != expected_first_result
            or observed_outcome.get("final_response") != final_response
            or observed_outcome.get("terminal_state") != terminal_state
            or observed_outcome.get("reasoning_result") != reasoning_result
            or observed_outcome.get("prompt_dependency") != prompt_dependency
            or not isinstance(display_binding, dict)
            or display_binding.get("run_id") != run_id
            or display_binding.get("item_id") != item_id
            or display_binding.get("saving_contract_sha256")
            != saving_contract_sha
            or not isinstance(source_facts, dict)
            or source_facts.get("surface_kind") != item.get("surface_kind")
            or source_facts.get("variant_id") != item.get("variant_id")
            or source_facts.get("origin_lane")
            != (
                "campaign_variant"
                if item.get("surface_kind") == "verified_variant"
                or item.get("variant_id") is not None
                else "campaign_original"
            )
        ):
            raise CampaignRuntimeError("episode package semantic binding drifted")
        if (
            verified.get("episode_id") != pending["episode_id"]
            or verified.get("execution_kind")
            != (
                "concept_supplement"
                if item.get("surface_kind") == "verified_variant"
                or item.get("variant_id") is not None
                else "original_review"
            )
            or verified.get("study_date")
            != (pending.get("payload") or {}).get("observed_date")
            or verified.get("capture_verified") is not True
            or verified.get("package_verified") is not True
            or verified.get("formal_write_count") != 0
            or verified.get("fsrs_write_count") != 0
            or verified.get("personal_summary_write_count") != 0
            or verified.get("background_processing") != "none"
        ):
            raise CampaignRuntimeError("episode capture proof policy drifted")
        proof = {key: verified.get(key) for key in required_equal}
        proof["package_root"] = (
            str(Path(package_root).expanduser().resolve())
            if package_root is not None
            else None
        )
        payload = {
            "schema": "morning-campaign-episode-finalization-v2",
            "saving_contract_sha256": saving_contract_sha,
            "run_id": run_id,
            "campaign_date": state["campaign_date"],
            "study_date": verified.get("study_date"),
            "segment_id": state["active_segment"]["segment_id"],
            "item_id": item_id,
            "episode_id": pending["episode_id"],
            "observation_sha256": pending["observation_sha256"],
            "terminal_state": terminal_state,
            "final_response": final_response,
            "reasoning_result": reasoning_result,
            "prompt_dependency": prompt_dependency,
            "capture_proof": proof,
            "capture_verified": True,
            "package_verified": True,
            "formal_write_count": 0,
            "fsrs_write_count": 0,
            "personal_summary_write_count": 0,
            "background_processing": "none",
        }
        new_state = _advance_state_after_episode(run_dir, state, terminal_state)
        return _append_operation(
            run_dir,
            state,
            event_type="campaign_episode_finalized",
            payload=payload,
            request=request,
            idempotency_key=idempotency_key,
            event_time=event_time,
            new_state=new_state,
        )


def begin_unresolved_episode(
    runtime_root: str | Path,
    run_id: str,
    *,
    item_id: str,
    observed_date: str,
    idempotency_key: str,
    event_time: str | None = None,
) -> dict[str, Any]:
    """Bind an explicit skip/end without inventing a learner response."""

    root = Path(runtime_root).expanduser().resolve()
    run_dir = _run_dir(root, run_id)
    request = {
        "operation": "record-outcome",
        "mode": "explicit_unresolved_without_response",
        "run_id": run_id,
        "item_id": _safe_id(item_id, "item_id", ITEM_ID_RE),
        "observed_date": _safe_date(observed_date, "observed_date"),
    }
    with _runtime_lock(root):
        request_sha = _digest(request)
        repeated = _idempotent_lookup(run_dir, idempotency_key, request_sha)
        if repeated is not None:
            return repeated
        state = _load_verified_state(run_dir)
        run = _load_run(run_dir)
        saving_kind, saving_contract_sha = _saving_contract(run)
        if saving_kind != SAVING_CONTRACT_V2_SCHEMA or state.get("status") != "active":
            raise CampaignRuntimeError("unresolved episode cannot start here")
        item = _current_item(state)
        if item.get("item_id") != item_id:
            raise CampaignRuntimeError("item_id is not the current campaign frontier")
        core = {
            "schema": "morning-campaign-response-observation-v2",
            "saving_contract_sha256": saving_contract_sha,
            "episode_id": f"{run_id}:{item_id}",
            "run_id": run_id,
            "campaign_date": state["campaign_date"],
            "observed_date": request["observed_date"],
            "segment_id": state["active_segment"]["segment_id"],
            "segment_ordinal": state["current_segment_ordinal"],
            "item_id": item_id,
            "source_id": item["source_id"],
            "formal_node_id": item.get("formal_node_id"),
            "true_item_id": item["true_item_id"],
            "variant_id": item["variant_id"],
            "surface_kind": item["surface_kind"],
            "campaign_item_commitment_sha256": item[
                "campaign_item_commitment_sha256"
            ],
            "first_response": None,
            "first_result": "not_observed",
            "initial_reasoning_result": "not_observed",
            "initial_prompt_dependency": "not_observed",
            "confidence": None,
            "recognized_original": False,
            "response_seconds": None,
            "audit_required": False,
            "audit_performed": False,
            "capture_required": True,
            "formal_write_count": 0,
            "fsrs_write_count": 0,
            "personal_summary_write_count": 0,
        }
        observation_sha = _digest(core)
        payload = {**core, "observation_sha256": observation_sha}
        new_state = json.loads(json.dumps(state))
        new_state["status"] = "episode_pending_capture"
        new_state["pending_episode"] = {
            "episode_id": payload["episode_id"],
            "item_id": item_id,
            "segment_id": payload["segment_id"],
            "observation_sha256": observation_sha,
            "payload": payload,
        }
        return _append_operation(
            run_dir,
            state,
            event_type="campaign_response_observed",
            payload=payload,
            request=request,
            idempotency_key=idempotency_key,
            event_time=event_time,
            new_state=new_state,
        )


def _validate_durable_capture_receipt(
    path: Path,
    expected_sha256: str,
    pending: dict[str, Any],
    run_id: str,
    repo_root: Path,
) -> tuple[dict[str, Any], str]:
    expected = _safe_sha(expected_sha256, "capture_receipt_sha256")
    _regular_file(path, "durable Capture receipt", MAX_RECEIPT_BYTES)
    actual = _sha256_file(path)
    if actual != expected:
        raise CampaignRuntimeError("durable Capture receipt SHA-256 drifted")
    receipt = _read_json(path, "durable Capture receipt", MAX_RECEIPT_BYTES)
    allowed_v1 = {
        "schema",
        "status",
        "run_id",
        "segment_id",
        "item_id",
        "capture_request_id",
        "pending_outcome_sha256",
        "capture_id",
        "capture_status",
        "formal_write_count",
    }
    allowed_v2 = {
        "schema",
        "status",
        "run_id",
        "segment_id",
        "item_id",
        "capture_request_id",
        "pending_outcome_sha256",
        "conversation_package_locator",
        "conversation_package_sha256",
        "conversation_package_root",
        "capture_id",
        "capture_payload_sha256",
        "capture_idempotency_key",
        "capture_event_id",
        "capture_event_sha256",
        "capture_ledger_event_sha256",
        "capture_joint_receipt_sha256",
        "capture_joint_manifest_sha256",
        "capture_bounded_manifest_sha256",
        "capture_status",
        "formalization_authorized",
        "producer_schema",
        "formal_write_count",
        "receipt_content_sha256",
    }
    schema = receipt.get("schema")
    if schema == CAPTURE_RECEIPT_SCHEMA_V1:
        if set(receipt) != allowed_v1:
            raise CampaignRuntimeError(
                "durable Capture receipt schema or fields are invalid"
            )
        raise CampaignRuntimeError(
            "durable Capture v1 is historical replay only"
        )
    elif schema == CAPTURE_RECEIPT_SCHEMA_V2:
        if set(receipt) != allowed_v2:
            raise CampaignRuntimeError(
                "durable Capture v2 receipt schema or fields are invalid"
            )
        content_core = {
            key: value
            for key, value in receipt.items()
            if key != "receipt_content_sha256"
        }
        if receipt.get("receipt_content_sha256") != _digest(content_core):
            raise CampaignRuntimeError("durable Capture v2 content hash drifted")
        for field in (
            "conversation_package_sha256",
            "capture_payload_sha256",
            "capture_event_sha256",
            "capture_ledger_event_sha256",
            "capture_joint_receipt_sha256",
            "capture_joint_manifest_sha256",
            "capture_bounded_manifest_sha256",
            "receipt_content_sha256",
        ):
            _safe_sha(receipt.get(field), field)
        for field in (
            "conversation_package_locator",
            "capture_id",
            "capture_idempotency_key",
            "capture_event_id",
            "producer_schema",
        ):
            _safe_id(receipt.get(field), field)
        package_root = Path(
            str(receipt.get("conversation_package_root") or "")
        ).expanduser().resolve()
        if package_root == Path("/") or len(package_root.parts) < 3:
            raise CampaignRuntimeError("conversation package root is unsafe")
        if receipt.get("producer_schema") != "cs408-campaign-package-capture-proof-v1":
            raise CampaignRuntimeError("durable Capture v2 producer drifted")
        expected_ref = {
            "kind": fact_capture.CONVERSATION_PACKAGE_REF_KIND,
            "locator": receipt["conversation_package_locator"],
            "sha256": receipt["conversation_package_sha256"],
        }
        try:
            opened_package = conversation_package.verify_package_binding(
                str(receipt["conversation_package_locator"]),
                expected_canonical_sha256=str(
                    receipt["conversation_package_sha256"]
                ),
                require_sealed=True,
                private_root=package_root,
            )
            canonical = fact_capture.reopen_capture_binding(
                str(receipt["capture_id"]),
                expected_payload_sha256=str(receipt["capture_payload_sha256"]),
                expected_idempotency_key=str(receipt["capture_idempotency_key"]),
                repo_root=repo_root,
            )
            capture_root = fact_capture.capture_root(repo_root)
            joint = capture_hot.verify_joint_commit(
                capture_root / "events.jsonl",
                capture_root / "state.json",
                capture_root / ".capture-hot-writer-v1",
                idempotency_key=str(receipt["capture_idempotency_key"]),
            )
        except (
            conversation_package.ConversationPackageError,
            fact_capture.CaptureError,
            capture_hot.CaptureHotWriterError,
            OSError,
        ) as exc:
            raise CampaignRuntimeError(
                f"durable Capture v2 verification failed:{exc}"
            ) from exc
        if (
            opened_package.get("sealed") is not True
            or opened_package.get("canonical_recomputed") is not True
            or opened_package.get("canonical_sha256")
            != receipt.get("conversation_package_sha256")
            or canonical.get("capture_id") != receipt.get("capture_id")
            or canonical.get("event_id") != receipt.get("capture_event_id")
            or canonical.get("event_sha256")
            != receipt.get("capture_ledger_event_sha256")
            or canonical.get("stable_evidence_refs") != [expected_ref]
            or canonical.get("quality_status")
            != fact_capture.PENDING_FORMAL_STATUS
            or canonical.get("formalization_authorized") is not True
            or receipt.get("formalization_authorized") is not True
            or canonical.get("formal_write_count") != 0
            or joint.get("status") != "PASS"
            or joint.get("capture_id") != receipt.get("capture_id")
            or joint.get("event_id") != receipt.get("capture_event_id")
            or joint.get("event_sha256") != receipt.get("capture_event_sha256")
            or joint.get("receipt_sha256")
            != receipt.get("capture_joint_receipt_sha256")
            or joint.get("joint_manifest_sha256")
            != receipt.get("capture_joint_manifest_sha256")
            or joint.get("bounded_manifest_sha256")
            != receipt.get("capture_bounded_manifest_sha256")
            or joint.get("formal_write_count") != 0
        ):
            raise CampaignRuntimeError("durable Capture v2 proof binding drifted")
    else:
        raise CampaignRuntimeError("durable Capture receipt schema or fields are invalid")
    if (
        receipt.get("status") != "durable"
        or receipt.get("run_id") != run_id
        or receipt.get("segment_id") != pending.get("segment_id")
        or receipt.get("item_id") != pending.get("item_id")
        or receipt.get("capture_request_id") != pending.get("capture_request_id")
        or receipt.get("pending_outcome_sha256")
        != pending.get("pending_outcome_sha256")
        or receipt.get("capture_status") != "awaiting_sol_formalization"
        or receipt.get("formal_write_count") != 0
    ):
        raise CampaignRuntimeError("durable Capture receipt binding drifted")
    _safe_id(receipt.get("capture_id"), "capture_id")
    return receipt, actual


def finalize_capture(
    runtime_root: str | Path,
    run_id: str,
    *,
    repo_root: str | Path,
    capture_receipt_path: str | Path,
    capture_receipt_sha256: str,
    idempotency_key: str,
    event_time: str | None = None,
) -> dict[str, Any]:
    root = Path(runtime_root).expanduser().resolve()
    canonical_repo = Path(repo_root).expanduser().resolve()
    run_dir = _run_dir(root, run_id)
    receipt_path = Path(capture_receipt_path).expanduser().resolve()
    request = {
        "operation": "finalize-capture",
        "run_id": run_id,
        "capture_receipt_path": str(receipt_path),
        "capture_receipt_sha256": _safe_sha(
            capture_receipt_sha256, "capture_receipt_sha256"
        ),
    }
    with _runtime_lock(root):
        repeated = _idempotent_lookup(run_dir, idempotency_key, _digest(request))
        if repeated is not None:
            return repeated
        state = _load_verified_state(run_dir)
        if state["status"] != "failure_pending_capture":
            raise CampaignRuntimeError(
                f"Capture cannot be finalized from {state['status']}"
            )
        pending = state.get("pending_failure")
        if not isinstance(pending, dict):
            raise CampaignRuntimeError("pending failure evidence is missing")
        if _current_item(state)["item_id"] != pending.get("item_id"):
            raise CampaignRuntimeError("pending failure and active frontier differ")
        capture_receipt, receipt_sha = _validate_durable_capture_receipt(
            receipt_path,
            capture_receipt_sha256,
            pending,
            run_id,
            canonical_repo,
        )
        pending_payload = pending.get("payload")
        if not isinstance(pending_payload, dict):
            raise CampaignRuntimeError("pending failure payload is missing")
        canonical_binding = pending_payload.get("canonical_review_commit")
        if not isinstance(canonical_binding, dict):
            raise CampaignRuntimeError("pending canonical review proof is missing")
        try:
            canonical_verified = canonical_review_state.verify_commit(
                canonical_repo,
                receipt_sha256=_safe_sha(
                    canonical_binding.get("commit_receipt_sha256"),
                    "canonical commit receipt SHA-256",
                ),
            )
        except Exception as exc:
            raise CampaignRuntimeError(
                f"pending_canonical_review_proof_invalid:{exc}"
            ) from exc
        if (
            canonical_verified.get("status") != "verified"
            or canonical_verified.get("event_id") != canonical_binding.get("event_id")
            or canonical_verified.get("event_sha256")
            != canonical_binding.get("event_sha256")
        ):
            raise CampaignRuntimeError("pending canonical review proof drifted")
        final_core = {
            **pending_payload,
            "capture_verified": True,
            "capture_id": capture_receipt["capture_id"],
            "capture_status": capture_receipt["capture_status"],
            "capture_receipt_sha256": receipt_sha,
            "capture_receipt_schema": capture_receipt["schema"],
        }
        handoff = dict(final_core["scheduler_handoff"])
        handoff["campaign_target_complete"] = True
        final_core["scheduler_handoff"] = handoff
        final_core.pop("outcome_sha256", None)
        final_payload = {**final_core, "outcome_sha256": _digest(final_core)}
        new_state = _advance_state_after_item(run_dir, state, "Again")
        return _append_operation(
            run_dir,
            state,
            event_type="rapid_review_failure_finalized",
            payload=final_payload,
            request=request,
            idempotency_key=idempotency_key,
            event_time=_safe_timestamp(event_time),
            new_state=new_state,
        )


def pause_run(
    runtime_root: str | Path,
    run_id: str,
    *,
    reason: str,
    idempotency_key: str,
    event_time: str | None = None,
) -> dict[str, Any]:
    if reason not in PAUSE_REASONS:
        raise CampaignRuntimeError("pause reason is invalid")
    root = Path(runtime_root).expanduser().resolve()
    run_dir = _run_dir(root, run_id)
    request = {"operation": "pause", "run_id": run_id, "reason": reason}
    with _runtime_lock(root):
        state = _load_verified_state(run_dir)
        repeated = _idempotent_lookup(run_dir, idempotency_key, _digest(request))
        if repeated is not None:
            return repeated
        if state["status"] != "active":
            raise CampaignRuntimeError(f"run cannot pause from {state['status']}")
        return _append_operation(
            run_dir,
            state,
            event_type="run_paused",
            payload={"reason": reason},
            request=request,
            idempotency_key=idempotency_key,
            event_time=event_time,
            new_state={**state, "status": "paused"},
        )


def resume_run(
    runtime_root: str | Path,
    run_id: str,
    *,
    idempotency_key: str,
    event_time: str | None = None,
) -> dict[str, Any]:
    root = Path(runtime_root).expanduser().resolve()
    run_dir = _run_dir(root, run_id)
    request = {"operation": "resume", "run_id": run_id}
    with _runtime_lock(root):
        state = _load_verified_state(run_dir)
        repeated = _idempotent_lookup(run_dir, idempotency_key, _digest(request))
        if repeated is not None:
            return repeated
        if state["status"] != "paused":
            raise CampaignRuntimeError(f"run cannot resume from {state['status']}")
        return _append_operation(
            run_dir,
            state,
            event_type="run_resumed",
            payload={"current_item_id": _current_item(state)["item_id"]},
            request=request,
            idempotency_key=idempotency_key,
            event_time=event_time,
            new_state={**state, "status": "active"},
        )


def _remaining_items(run_dir: Path, state: dict[str, Any]) -> list[dict[str, Any]]:
    remaining: list[dict[str, Any]] = []
    current_ordinal = state["current_segment_ordinal"]
    for ordinal in range(current_ordinal, state["segment_count"]):
        segment = _load_segment(run_dir, ordinal)
        start = state["current_item_offset"] if ordinal == current_ordinal else 0
        remaining.extend(segment["items"][start:])
    return [
        {key: value for key, value in item.items() if key != "stable_hash_audit"}
        for item in remaining
    ]


def carryover_run(
    runtime_root: str | Path,
    run_id: str,
    *,
    target_date: str,
    idempotency_key: str,
    event_time: str | None = None,
) -> dict[str, Any]:
    target = _safe_date(target_date, "target_date")
    root = Path(runtime_root).expanduser().resolve()
    run_dir = _run_dir(root, run_id)
    request = {"operation": "carryover", "run_id": run_id, "target_date": target}
    with _runtime_lock(root):
        state = _load_verified_state(run_dir)
        repeated = _idempotent_lookup(run_dir, idempotency_key, _digest(request))
        if repeated is not None:
            return repeated
        if state["status"] != "paused":
            raise CampaignRuntimeError("carryover requires a paused run")
        if _saving_contract(_load_run(run_dir))[0] == SAVING_CONTRACT_V2_SCHEMA:
            events = _read_events(run_dir)
            last = events[-1] if events else {}
            if (
                last.get("event_type") != "run_paused"
                or (last.get("payload") or {}).get("reason") != "capture_hold"
            ):
                raise CampaignRuntimeError(
                    "saving-contract-v2 requires close-unresolved before carryover"
                )
        if date.fromisoformat(target) <= date.fromisoformat(state["campaign_date"]):
            raise CampaignRuntimeError("carryover target_date must be later")
        remaining = _remaining_items(run_dir, state)
        if not remaining:
            raise CampaignRuntimeError("run has no remaining items to carry over")
        successor = build_campaign_manifest(target, remaining, all_ready=False)
        successor_path = run_dir / "carryovers" / f"{successor['run_id']}.json"
        successor_sha = _atomic_write(successor_path, successor)
        payload = {
            "schema": CARRYOVER_SCHEMA,
            "target_date": target,
            "remaining_item_count": len(remaining),
            "successor_run_id": successor["run_id"],
            "successor_manifest_ref": successor_path.relative_to(run_dir).as_posix(),
            "successor_manifest_sha256": successor_sha,
            "source_run_immutable": True,
        }
        return _append_operation(
            run_dir,
            state,
            event_type="run_carried_over",
            payload=payload,
            request=request,
            idempotency_key=idempotency_key,
            event_time=event_time,
            new_state={**state, "status": "carried_over"},
        )


def status_run(runtime_root: str | Path, run_id: str) -> dict[str, Any]:
    run_dir = _run_dir(runtime_root, run_id)
    state = _load_verified_state(run_dir)
    current = None
    if state["status"] in {
        "active",
        "paused",
        "ready",
        "preparing",
        "failure_pending_capture",
        "episode_pending_capture",
    }:
        current = _current_item(state)["item_id"]
    result = {
        "schema": STATE_SCHEMA,
        "status": state["status"],
        "run_id": run_id,
        "campaign_date": state["campaign_date"],
        "completed_items": state["completed_items"],
        "item_count": state["item_count"],
        "ratings": state["ratings"],
        "current_segment_ordinal": state["current_segment_ordinal"],
        "current_item_id": current,
        "active_segment_item_count": len((state.get("active_segment") or {}).get("items", [])),
        "active_state_size_bytes": (run_dir / "active-state.json").stat().st_size,
        "last_sequence": state["last_sequence"],
        "capture_write_count": 0,
        "formal_write_count": 0,
    }
    if state.get("saving_contract_sha256") is not None:
        result.update(
            episode_outcomes=state["episode_outcomes"],
            saving_contract_sha256=state["saving_contract_sha256"],
        )
        if state.get("status") == "episode_pending_capture":
            pending = state.get("pending_episode") or {}
            payload = pending.get("payload") or {}
            result["open_episode"] = {
                "episode_id": pending.get("episode_id"),
                "study_date": payload.get("observed_date"),
                "run_id": run_id,
                "item_id": pending.get("item_id"),
                "status": "capture_pending",
                "capture_verified": False,
            }
    return result


@contextmanager
def verified_frontier_guard(
    runtime_root: str | Path, run_id: str
) -> Iterator[tuple[Path, dict[str, Any], dict[str, Any], dict[str, Any] | None]]:
    """Hold the runtime lock while exposing one fully verified frontier."""

    root = Path(runtime_root).expanduser().resolve()
    run_dir = _run_dir(root, run_id)
    with _runtime_lock(root):
        run = _load_run(run_dir)
        state = _load_verified_state(run_dir)
        item = None
        if state.get("status") in {
            "preparing",
            "ready",
            "active",
            "paused",
            "failure_pending_capture",
            "episode_pending_capture",
            "carried_over",
        }:
            item = _current_item(state)
        yield (
            run_dir,
            json.loads(json.dumps(run)),
            json.loads(json.dumps(state)),
            json.loads(json.dumps(item)) if item is not None else None,
        )


def reopen_verified_frontier(
    runtime_root: str | Path, run_id: str
) -> tuple[Path, dict[str, Any], dict[str, Any], dict[str, Any] | None]:
    """Public read-only API for hash-bound campaign owners and bridges."""

    with verified_frontier_guard(runtime_root, run_id) as frontier:
        return frontier


def _read_events(run_dir: Path) -> list[dict[str, Any]]:
    events: list[dict[str, Any]] = []
    path = run_dir / "events.jsonl"
    _regular_file(path, "campaign ledger", MAX_MANIFEST_BYTES * 8)
    for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        if not line.strip():
            continue
        try:
            value = json.loads(line)
        except json.JSONDecodeError as exc:
            raise CampaignRuntimeError(f"campaign ledger line {number} is invalid") from exc
        if not isinstance(value, dict):
            raise CampaignRuntimeError(f"campaign ledger line {number} is not an object")
        events.append(value)
    return events


def audit_run(
    runtime_root: str | Path,
    run_id: str,
    *,
    repo_root: str | Path | None = None,
) -> dict[str, Any]:
    run_dir = _run_dir(runtime_root, run_id)
    errors: list[str] = []
    canonical_repo = (
        Path(repo_root).expanduser().resolve() if repo_root is not None else None
    )
    canonical_receipts: dict[str, dict[str, str]] = {}
    try:
        run = _load_run(run_dir)
        manifest = normalize_manifest(
            _read_json(run_dir / "manifest.json", "campaign manifest", MAX_MANIFEST_BYTES)
        )
        if _sha256_file(run_dir / "manifest.json") != run["manifest_sha256"]:
            errors.append("manifest SHA-256 drifted")
        for ordinal in range(run["segment_count"]):
            _load_segment(run_dir, ordinal)
        state = _load_verified_state(run_dir)
        events = _read_events(run_dir)
        previous = None
        for index, event in enumerate(events, 1):
            core = dict(event)
            event_id = core.pop("event_id", None)
            event_sha = core.pop("event_sha256", None)
            expected = _digest(core)
            if event.get("sequence") != index:
                errors.append(f"event sequence drifted at {index}")
            if event.get("previous_event_sha256") != previous:
                errors.append(f"event chain drifted at {index}")
            if event_sha != expected or event_id != f"MCE-{expected[:24].upper()}":
                errors.append(f"event digest drifted at {index}")
            previous = event_sha
            if event.get("event_type") in {
                "rapid_review_outcome_recorded",
                "rapid_review_failure_pending",
                "rapid_review_failure_finalized",
            }:
                payload = event.get("payload") or {}
                if payload.get("capture_write_count") != 0 or payload.get("formal_write_count") != 0:
                    errors.append(f"outcome write boundary drifted at {index}")
                if payload.get("capture_required") is False and payload.get("capture_authorization_request") is not None:
                    errors.append(f"correct path contains capture request at {index}")
                if payload.get("capture_required") is True:
                    request = payload.get("capture_authorization_request") or {}
                    if request.get("capture_authorized") is not False or request.get("direct_capture_write") is not False:
                        errors.append(f"failure capture request over-authorized at {index}")
                binding = payload.get("canonical_review_commit")
                if not isinstance(binding, dict):
                    errors.append(f"canonical review binding missing at {index}")
                else:
                    receipt_sha = str(binding.get("commit_receipt_sha256") or "")
                    event_id_value = str(binding.get("event_id") or "")
                    event_sha_value = str(binding.get("event_sha256") or "")
                    if (
                        not SHA_RE.fullmatch(receipt_sha)
                        or not event_id_value
                        or not SHA_RE.fullmatch(event_sha_value)
                    ):
                        errors.append(f"canonical review binding invalid at {index}")
                    else:
                        prior = canonical_receipts.get(receipt_sha)
                        current = {
                            "event_id": event_id_value,
                            "event_sha256": event_sha_value,
                        }
                        if prior is not None and prior != current:
                            errors.append(
                                f"canonical review receipt identity drifted at {index}"
                            )
                        canonical_receipts[receipt_sha] = current
            elif event.get("event_type") == "campaign_response_observed":
                payload = event.get("payload") or {}
                if (
                    payload.get("capture_required") is not True
                    or payload.get("formal_write_count") != 0
                    or payload.get("fsrs_write_count") != 0
                    or payload.get("personal_summary_write_count") != 0
                    or payload.get("episode_id")
                    != f"{run_id}:{payload.get('item_id')}"
                    or "canonical_review_commit" in payload
                    or "rating" in payload
                ):
                    errors.append(f"episode observation policy drifted at {index}")
            elif event.get("event_type") == "campaign_episode_finalized":
                payload = event.get("payload") or {}
                if (
                    payload.get("capture_verified") is not True
                    or payload.get("package_verified") is not True
                    or payload.get("formal_write_count") != 0
                    or payload.get("fsrs_write_count") != 0
                    or payload.get("personal_summary_write_count") != 0
                    or "canonical_review_commit" in payload
                    or "rating" in payload
                ):
                    errors.append(f"episode finalization policy drifted at {index}")
                proof = payload.get("capture_proof") or {}
                if canonical_repo is None:
                    errors.append(f"episode capture audit requires repo_root at {index}")
                else:
                    try:
                        import study_episode_capture_408 as episode_capture

                        verified_episode = episode_capture.verify_episode_capture(
                            canonical_repo,
                            proof.get("receipt_ref"),
                            package_root=proof.get("package_root"),
                        )
                    except Exception as exc:
                        errors.append(
                            f"episode capture receipt failed verification at {index}: {exc}"
                        )
                    else:
                        if any(
                            verified_episode.get(key) != proof.get(key)
                            for key in proof
                            if key != "package_root"
                        ):
                            errors.append(
                                f"episode capture receipt identity drifted at {index}"
                            )
        if state["last_sequence"] != len(events) or state.get("last_event_sha256") != previous:
            errors.append("active state and ledger frontier differ")
        if state["completed_items"] > manifest["item_count"]:
            errors.append("completed item count exceeds manifest")
        saving_kind, _saving_sha = _saving_contract(run)
        if saving_kind == SAVING_CONTRACT_V2_SCHEMA:
            if (
                sum(state["ratings"].values()) != 0
                or sum((state.get("episode_outcomes") or {}).values())
                != state["completed_items"]
            ):
                errors.append("episode counters differ from completed item count")
        elif sum(state["ratings"].values()) != state["completed_items"]:
            errors.append("rating counters differ from completed item count")
        if state.get("capture_write_count") != 0 or state.get("formal_write_count") != 0:
            errors.append("active state write boundary drifted")
        if state.get("status") == "failure_pending_capture":
            pending = state.get("pending_failure")
            if (
                not isinstance(pending, dict)
                or pending.get("item_id") != _current_item(state).get("item_id")
            ):
                errors.append("pending failure and active frontier differ")
        if state.get("status") == "episode_pending_capture":
            pending = state.get("pending_episode")
            if (
                not isinstance(pending, dict)
                or pending.get("item_id") != _current_item(state).get("item_id")
            ):
                errors.append("pending episode and active frontier differ")
        if canonical_receipts and canonical_repo is None:
            errors.append("canonical review audit requires repo_root")
        elif canonical_repo is not None:
            for receipt_sha, binding in canonical_receipts.items():
                try:
                    verified = canonical_review_state.verify_commit(
                        canonical_repo, receipt_sha256=receipt_sha,
                        allow_historical_projection=True,
                    )
                except Exception as exc:
                    errors.append(f"canonical review receipt failed verification: {exc}")
                    continue
                if (
                    verified.get("status") != "verified"
                    or verified.get("event_id") != binding["event_id"]
                    or verified.get("event_sha256") != binding["event_sha256"]
                ):
                    errors.append("canonical review receipt proof drifted")
    except (CampaignRuntimeError, OSError, KeyError, TypeError, ValueError) as exc:
        errors.append(str(exc))
        state = None
        manifest = None
        events = []
    return {
        "status": "PASS" if not errors else "FAIL",
        "run_id": run_id,
        "errors": errors,
        "event_count": len(events),
        "item_count": manifest.get("item_count") if isinstance(manifest, dict) else None,
        "completed_items": state.get("completed_items") if isinstance(state, dict) else None,
        "active_state_size_bytes": (run_dir / "active-state.json").stat().st_size if (run_dir / "active-state.json").exists() else None,
        "full_ledger_replay_count": 1,
        "canonical_review_receipt_count": len(canonical_receipts),
        "capture_write_count": 0,
        "formal_write_count": 0,
    }


def _load_json_arg(path: str) -> dict[str, Any]:
    return _read_json(Path(path).expanduser().resolve(), "input JSON", MAX_MANIFEST_BYTES)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--runtime-root", required=True)
    commands = parser.add_subparsers(dest="command", required=True)
    create = commands.add_parser("create-run")
    create.add_argument("--manifest", required=True)
    for name in (
        "start",
        "pause",
        "resume",
        "carryover",
        "record-outcome",
        "finalize-capture",
        "status",
        "audit",
    ):
        command = commands.add_parser(name)
        command.add_argument("--run-id", required=True)
        if name not in {"status", "audit"}:
            command.add_argument("--idempotency-key", required=True)
            command.add_argument("--event-time")
        if name == "pause":
            command.add_argument("--reason", required=True, choices=sorted(PAUSE_REASONS))
        elif name == "carryover":
            command.add_argument("--target-date", required=True)
        elif name == "record-outcome":
            command.add_argument("--repo", required=True)
            command.add_argument("--item-id", required=True)
            command.add_argument("--observed-date", required=True)
            command.add_argument("--choice-result", required=True, choices=sorted(CHOICE_RESULTS))
            command.add_argument("--reasoning-result", required=True, choices=sorted(REASONING_RESULTS))
            command.add_argument("--confidence", required=True, choices=sorted(CONFIDENCE_LEVELS))
            command.add_argument("--prompt-dependency", required=True, choices=sorted(PROMPT_DEPENDENCIES))
            command.add_argument("--audit-performed", "--force-audit", action="store_true")
            command.add_argument("--recognized-original", action="store_true")
            command.add_argument("--response-seconds", type=float)
        elif name == "finalize-capture":
            command.add_argument("--repo", required=True)
            command.add_argument("--capture-receipt", required=True)
            command.add_argument("--capture-receipt-sha256", required=True)
        elif name == "audit":
            command.add_argument("--repo", required=True)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        if args.command == "create-run":
            result = create_run(args.runtime_root, args.manifest)
        elif args.command == "start":
            result = start_run(args.runtime_root, args.run_id, idempotency_key=args.idempotency_key, event_time=args.event_time)
        elif args.command == "status":
            result = status_run(args.runtime_root, args.run_id)
        elif args.command == "pause":
            result = pause_run(args.runtime_root, args.run_id, reason=args.reason, idempotency_key=args.idempotency_key, event_time=args.event_time)
        elif args.command == "resume":
            result = resume_run(args.runtime_root, args.run_id, idempotency_key=args.idempotency_key, event_time=args.event_time)
        elif args.command == "carryover":
            result = carryover_run(args.runtime_root, args.run_id, target_date=args.target_date, idempotency_key=args.idempotency_key, event_time=args.event_time)
        elif args.command == "record-outcome":
            result = record_outcome(
                args.runtime_root,
                args.run_id,
                repo_root=args.repo,
                item_id=args.item_id,
                observed_date=args.observed_date,
                choice_result=args.choice_result,
                reasoning_result=args.reasoning_result,
                confidence=args.confidence,
                prompt_dependency=args.prompt_dependency,
                audit_performed=args.audit_performed,
                recognized_original=args.recognized_original,
                response_seconds=args.response_seconds,
                idempotency_key=args.idempotency_key,
                event_time=args.event_time,
            )
        elif args.command == "finalize-capture":
            result = finalize_capture(
                args.runtime_root,
                args.run_id,
                repo_root=args.repo,
                capture_receipt_path=args.capture_receipt,
                capture_receipt_sha256=args.capture_receipt_sha256,
                idempotency_key=args.idempotency_key,
                event_time=args.event_time,
            )
        elif args.command == "audit":
            result = audit_run(
                args.runtime_root, args.run_id, repo_root=args.repo
            )
        else:
            raise CampaignRuntimeError("unsupported command")
    except (CampaignRuntimeError, OSError) as exc:
        print(json.dumps({"status": "blocked", "error": str(exc)}, ensure_ascii=False))
        return 2
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    sys.exit(main())
