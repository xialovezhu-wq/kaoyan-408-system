#!/usr/bin/env python3
"""Plan and track bounded nightly 408 variant-generation demand.

This is a cold-path planner.  It derives answer-safe demand only from current
canonical review outcomes, their verified formal original-question mappings,
the seven-day cooldown, and the independently verified variant catalog.  It
never authors a question, reads in the single-question hot path, or writes the
formal repository.
"""

from __future__ import annotations

import argparse
import fcntl
import hashlib
import json
import os
import re
import sys
import tempfile
from contextlib import contextmanager
from datetime import date, datetime
from pathlib import Path
from typing import Any, Iterator
from zoneinfo import ZoneInfo

from linked_practice_source_408 import (
    LinkedPracticeSourceError,
    SHA256_RE,
    validate_source_for_practice,
)
from variant_source_pipeline_408 import (
    VariantPipelineError,
    prepare_generation,
    validate_authoring_evidence,
    validate_variant_catalog,
)


PLAN_SCHEMA = "variant_demand_plan_v1"
PLAN_RECEIPT_SCHEMA = "variant_demand_plan_receipt_v1"
WORKFLOW_EVENT_SCHEMA = "variant_demand_workflow_event_v1"
FORMAL_NODE_RE = re.compile(r"^(?:DS|CO|OS|CN)_(?:\d{4}|UNK)_\d{3}$")
POINT_RE = re.compile(r"^(?:DS|CO|OS|CN)\d{2}-\d{2}$")
ISO_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
SAFE_KEY_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{2,160}$")
FAILURE_RESULTS = {"partial", "wrong", "uncertain"}
DEFAULT_LIMIT = 3
MAX_LIMIT = 8
MAX_EVIDENCE_FILES = 256
MAX_EVIDENCE_BYTES = 128 * 1024
DEFAULT_STATE_RELATIVE = Path("wiki/study_vaults/408-full/state/personalization")
DEFAULT_DURABLE_RELATIVE = Path("wiki/study_vaults/408-full/state/linked-practice")
DEFAULT_EVENTS_RELATIVE = Path("wiki/study_vaults/408-full/state/review-loop/events.jsonl")
EVIDENCE_DIRECTORY = "variant-authoring-evidence"
DEMAND_DIRECTORY = "variant-demand"
ALLOWED_MARK_OUTCOMES = {"finalized", "source_gap", "failed"}
ALLOWED_MARK_REASONS = {
    "verified_variant_published",
    "original_question_source_missing",
    "original_question_source_invalid",
    "authoring_evidence_missing",
    "authoring_evidence_ambiguous",
    "sealed_solution_or_evidence_invalid",
    "knowledge_point_ambiguous",
    "knowledge_point_conflict",
    "mechanism_ambiguous",
    "mechanism_conflict",
    "author_failed",
    "verifier_failed",
    "attestation_failed",
    "source_drift",
    "retryable_tool_failure",
}
FORBIDDEN_ARTIFACT_KEYS = {
    "answer",
    "correct_answer",
    "options",
    "question_stem",
    "question_text",
    "solution",
    "full_response",
    "first_break",
    "feedback",
}


class VariantDemandError(RuntimeError):
    """A stable failure in the nightly variant-demand planner."""


def _retired_write_surface() -> None:
    raise VariantDemandError(
        "variant_demand_writes_retired_read_only_compatibility"
    )


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
    return _sha256_bytes(path.read_bytes())


def _digest(value: Any) -> str:
    return _sha256_bytes(_canonical_bytes(value))


def _atomic_write(path: Path, payload: bytes) -> str:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)
    return _sha256_bytes(payload)


def _atomic_write_json(path: Path, value: Any) -> str:
    return _atomic_write(path, _pretty_bytes(value))


@contextmanager
def _lock(path: Path) -> Iterator[None]:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a+", encoding="utf-8") as handle:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def _load_object(path: Path, label: str) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise VariantDemandError(f"{label} is unreadable") from exc
    if not isinstance(value, dict):
        raise VariantDemandError(f"{label} must be a JSON object")
    return value


def _valid_date(raw: object, label: str) -> str:
    value = str(raw or "")
    if not ISO_DATE_RE.fullmatch(value):
        raise VariantDemandError(f"{label} must be YYYY-MM-DD")
    try:
        date.fromisoformat(value)
    except ValueError as exc:
        raise VariantDemandError(f"{label} is invalid") from exc
    return value


def _days_between(older: str, newer: str) -> int:
    return (date.fromisoformat(newer) - date.fromisoformat(older)).days


def _roots(repo: Path) -> tuple[Path, Path, Path]:
    state = (repo / DEFAULT_STATE_RELATIVE).resolve()
    linked = (repo / DEFAULT_DURABLE_RELATIVE).resolve()
    return state, linked, linked / DEMAND_DIRECTORY


def _assert_answer_safe(value: Any, trail: tuple[str, ...] = ()) -> None:
    if isinstance(value, dict):
        for key, child in value.items():
            if str(key).lower() in FORBIDDEN_ARTIFACT_KEYS:
                raise VariantDemandError(
                    f"demand artifact contains protected field at {'.'.join(trail + (str(key),))}"
                )
            _assert_answer_safe(child, trail + (str(key),))
    elif isinstance(value, list):
        for index, child in enumerate(value):
            _assert_answer_safe(child, trail + (str(index),))


def _load_events(path: Path) -> dict[str, dict[str, Any]]:
    events: dict[str, dict[str, Any]] = {}
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except (OSError, UnicodeError) as exc:
        raise VariantDemandError("canonical review ledger is unreadable") from exc
    for line_number, line in enumerate(lines, start=1):
        if not line.strip():
            continue
        try:
            value = json.loads(line)
        except json.JSONDecodeError as exc:
            raise VariantDemandError(
                f"canonical review ledger has invalid JSON at line {line_number}"
            ) from exc
        if not isinstance(value, dict) or not str(value.get("event_id") or ""):
            raise VariantDemandError(
                f"canonical review ledger has invalid event at line {line_number}"
            )
        event_id = str(value["event_id"])
        if event_id in events:
            raise VariantDemandError(f"duplicate canonical review event: {event_id}")
        events[event_id] = value
    return events


def _verified_inputs(
    repo: Path, *, as_of: str
) -> dict[str, Any]:
    state, _, _ = _roots(repo)
    manifest_path = state / "manifest.json"
    manifest = _load_object(manifest_path, "personalization manifest")
    if (
        manifest.get("schema") != "personalization_index_v1"
        or manifest.get("answer_safe") is not True
        or manifest.get("formal_write_count") != 0
        or manifest.get("as_of") != as_of
    ):
        raise VariantDemandError("personalization manifest is stale or unsafe")

    required = ("event_mappings.json", "question_index.json", "variant-catalog.json")
    artifacts: dict[str, dict[str, Any]] = {}
    artifact_hashes = manifest.get("artifact_hashes") or {}
    for name in required:
        path = state / name
        expected = str(artifact_hashes.get(name) or "")
        if not SHA256_RE.fullmatch(expected) or not path.is_file() or _sha256_file(path) != expected:
            raise VariantDemandError(f"personalization artifact drifted: {name}")
        artifacts[name] = _load_object(path, name)

    catalog = artifacts["variant-catalog.json"]
    try:
        validate_variant_catalog(repo, catalog)
    except VariantPipelineError as exc:
        raise VariantDemandError("verified variant catalog failed deep validation") from exc

    events_path = repo / DEFAULT_EVENTS_RELATIVE
    expected_events = str((manifest.get("source_hashes") or {}).get("events") or "")
    if (
        not SHA256_RE.fullmatch(expected_events)
        or not events_path.is_file()
        or _sha256_file(events_path) != expected_events
    ):
        raise VariantDemandError("canonical review ledger drifted from personalization build")
    events = _load_events(events_path)
    projection_commitment = _digest(
        {
            "manifest_sha256": _sha256_file(manifest_path),
            "events_sha256": expected_events,
            "event_mappings_sha256": artifact_hashes["event_mappings.json"],
            "question_index_sha256": artifact_hashes["question_index.json"],
            "variant_catalog_sha256": artifact_hashes["variant-catalog.json"],
            "as_of": as_of,
        }
    )
    return {
        "state": state,
        "manifest": manifest,
        "mappings": artifacts["event_mappings.json"],
        "questions": artifacts["question_index.json"],
        "catalog": catalog,
        "events": events,
        "projection_commitment": projection_commitment,
        "source_hashes": {
            "canonical_review_events": expected_events,
            "event_mappings": artifact_hashes["event_mappings.json"],
            "question_index": artifact_hashes["question_index.json"],
            "verified_variant_catalog": artifact_hashes["variant-catalog.json"],
        },
    }


def _target_value(
    mapping_values: object,
    question_values: object,
    *,
    point: bool,
) -> tuple[str | None, str | None]:
    mapped = {
        str(value)
        for value in (mapping_values if isinstance(mapping_values, list) else [])
        if str(value)
    }
    question = {
        str(value)
        for value in (question_values if isinstance(question_values, list) else [])
        if str(value)
    }
    if mapped and question:
        values = mapped & question
        if not values:
            return None, "knowledge_point_conflict" if point else "mechanism_conflict"
    else:
        values = mapped or question
    if len(values) == 1:
        value = next(iter(values))
        if not point or POINT_RE.fullmatch(value):
            return value, None
    return None, "knowledge_point_ambiguous" if point else "mechanism_ambiguous"


def _variant_covers(
    catalog: dict[str, Any], *, anchor: str, point: str, mechanism: str
) -> bool:
    return any(
        isinstance(row, dict)
        and row.get("status") == "verified_ready"
        and row.get("anchor_formal_node_id") == anchor
        and row.get("knowledge_point") == point
        and row.get("mechanism_key") == mechanism
        for row in catalog.get("entries", [])
    )


def _evidence_inventory(repo: Path) -> tuple[list[dict[str, Any]], str]:
    _, linked, _ = _roots(repo)
    evidence_root = linked / EVIDENCE_DIRECTORY
    paths = sorted(evidence_root.glob("*.json")) if evidence_root.is_dir() else []
    if len(paths) > MAX_EVIDENCE_FILES:
        raise VariantDemandError("variant authoring evidence inventory exceeds bounded limit")
    safe: list[dict[str, Any]] = []
    fingerprints: list[dict[str, Any]] = []
    for path in paths:
        if path.is_symlink() or not path.is_file() or path.stat().st_size > MAX_EVIDENCE_BYTES:
            fingerprints.append({"name": path.name, "status": "invalid"})
            continue
        try:
            raw = _load_object(path, "variant authoring evidence")
        except VariantDemandError:
            fingerprints.append({"name": path.name, "status": "invalid"})
            continue
        hint = {
            "anchor_formal_node_id": str(raw.get("anchor_formal_node_id") or ""),
            "knowledge_point": str(raw.get("knowledge_point") or ""),
            "mechanism_key": str(raw.get("mechanism_key") or ""),
        }
        try:
            checked = validate_authoring_evidence(repo, path)
        except VariantPipelineError:
            invalid = {
                **hint,
                "anchor_source_ref": str(raw.get("anchor_source_ref") or ""),
                "anchor_source_sha256": str(raw.get("anchor_source_sha256") or ""),
                "validation_status": "invalid",
            }
            safe.append(invalid)
            fingerprints.append({**invalid, "sha256": _sha256_file(path)})
            continue
        item = {
            "anchor_formal_node_id": checked["anchor_formal_node_id"],
            "knowledge_point": checked["knowledge_point"],
            "mechanism_key": checked["mechanism_key"],
            "review_unit_id": checked.get("review_unit_id"),
            "authoring_evidence_ref": checked["evidence_ref"],
            "authoring_evidence_sha256": checked["evidence_sha256"],
            "anchor_source_ref": checked["anchor_source_ref"],
            "anchor_source_sha256": checked["anchor_source_sha256"],
            "author_identity_sha256": checked["author_identity_sha256"],
            "verifier_identity_sha256": checked["verifier_identity_sha256"],
            "validation_status": "verified",
        }
        safe.append(item)
        fingerprints.append({**item, "status": "verified"})
    return safe, _digest(fingerprints)


def _matching_evidence(
    inventory: list[dict[str, Any]],
    *,
    anchor: str,
    point: str,
    mechanism: str,
    source_ref: str | None,
    source_sha256: str | None,
) -> tuple[dict[str, Any] | None, str | None]:
    target_matches = [
        row
        for row in inventory
        if row["anchor_formal_node_id"] == anchor
        and row["knowledge_point"] == point
        and row["mechanism_key"] == mechanism
    ]
    matches = [
        row
        for row in target_matches
        if row.get("validation_status") == "verified"
        and row["anchor_source_ref"] == source_ref
        and row["anchor_source_sha256"] == source_sha256
    ]
    if len(matches) == 1:
        return matches[0], None
    if len(matches) > 1:
        return None, "authoring_evidence_ambiguous"
    if target_matches:
        return None, "sealed_solution_or_evidence_invalid"
    return None, "authoring_evidence_missing"


def _candidate_rows(
    repo: Path,
    inputs: dict[str, Any],
    *,
    study_date: str,
    as_of: str,
    evidence_inventory: list[dict[str, Any]],
) -> tuple[list[dict[str, Any]], dict[str, int]]:
    mappings = (inputs["mappings"].get("events") or {})
    questions = (inputs["questions"].get("questions") or {})
    catalog = inputs["catalog"]
    grouped: dict[tuple[str, str, str], dict[str, Any]] = {}
    counts = {
        "eligible_failure_events": 0,
        "covered_by_verified_variant": 0,
        "not_recent_original": 0,
        "unresolved_original_identity": 0,
    }
    severity = {"wrong": 3, "partial": 2, "uncertain": 1}

    for event_id, event in inputs["events"].items():
        mapping = mappings.get(event_id)
        if not isinstance(mapping, dict):
            continue
        if (
            event.get("event_kind") != "outcome"
            or event.get("first_result") not in FAILURE_RESULTS
            or event.get("observed_date") != study_date
            or mapping.get("event_kind") != "outcome"
            or mapping.get("practice_mode") == "variant"
        ):
            continue
        question_ids = [
            str(value)
            for value in mapping.get("question_node_ids", [])
            if FORMAL_NODE_RE.fullmatch(str(value))
        ]
        if len(set(question_ids)) != 1:
            counts["unresolved_original_identity"] += 1
            continue
        anchor = next(iter(set(question_ids)))
        counts["eligible_failure_events"] += 1
        question = questions.get(anchor)
        gaps: list[str] = []
        if not isinstance(question, dict):
            question = {}
            gaps.append("original_question_source_missing")

        point, point_gap = _target_value(
            mapping.get("point_ids"), question.get("primary_point_ids"), point=True
        )
        if point_gap:
            gaps.append(point_gap)
        mechanism, mechanism_gap = _target_value(
            mapping.get("mechanism_ids"), question.get("mechanism_ids"), point=False
        )
        if mechanism_gap == "mechanism_ambiguous" and point:
            mechanism = f"KP:{point}"
            mechanism_gap = None
        if mechanism_gap:
            gaps.append(mechanism_gap)
        if not point or not mechanism:
            point = point or f"{anchor[:2]}00-00"
            mechanism = mechanism or f"UNRESOLVED:{anchor}"

        last_attempt = str(question.get("effective_last_attempt_date") or "")
        if not ISO_DATE_RE.fullmatch(last_attempt):
            gaps.append("original_question_source_missing")
            last_attempt = study_date
        age = _days_between(last_attempt, as_of)
        if age < 0 or age >= 7:
            counts["not_recent_original"] += 1
            continue

        source = question.get("reliable_source") or {}
        source_ref = str(source.get("source_ref") or "") or None
        source_sha = str(source.get("source_sha256") or "") or None
        if (
            source.get("original_question_eligible") is not True
            or not source_ref
            or not source_sha
        ):
            gaps.append("original_question_source_missing")
        else:
            try:
                validate_source_for_practice(
                    repo,
                    source_ref,
                    source_sha,
                    practice_mode="original",
                    formal_node_id=anchor,
                )
            except LinkedPracticeSourceError:
                gaps.append("original_question_source_invalid")

        if POINT_RE.fullmatch(point) and _variant_covers(
            catalog, anchor=anchor, point=point, mechanism=mechanism
        ):
            counts["covered_by_verified_variant"] += 1
            continue

        evidence: dict[str, Any] | None = None
        if not gaps:
            evidence, evidence_gap = _matching_evidence(
                evidence_inventory,
                anchor=anchor,
                point=point,
                mechanism=mechanism,
                source_ref=source_ref,
                source_sha256=source_sha,
            )
            if evidence_gap:
                gaps.append(evidence_gap)

        target = {
            "anchor_formal_node_id": anchor,
            "knowledge_point": point,
            "mechanism_key": mechanism,
        }
        demand_id = "VDM-" + _digest(target)[:24].upper()
        key = (anchor, point, mechanism)
        row = grouped.setdefault(
            key,
            {
                "demand_id": demand_id,
                **target,
                "review_unit_id": (
                    evidence.get("review_unit_id") if evidence else None
                ),
                "study_date": study_date,
                "as_of": as_of,
                "cooldown_days": 7,
                "effective_last_attempt_date": last_attempt,
                "days_since_original_attempt": age,
                "first_result_classes": [],
                "trigger_event_ids": [],
                "trigger_event_commitments": [],
                "original_source_ref": source_ref,
                "original_source_sha256": source_sha,
                "authoring_evidence_ref": (
                    evidence.get("authoring_evidence_ref") if evidence else None
                ),
                "authoring_evidence_sha256": (
                    evidence.get("authoring_evidence_sha256") if evidence else None
                ),
                "author_identity_sha256": (
                    evidence.get("author_identity_sha256") if evidence else None
                ),
                "verifier_identity_sha256": (
                    evidence.get("verifier_identity_sha256") if evidence else None
                ),
                "source_gaps": [],
                "priority": 0,
                "generation_authority": "nightly_only",
                "formal_write_count": 0,
            },
        )
        if event_id not in row["trigger_event_ids"]:
            row["trigger_event_ids"].append(event_id)
            row["trigger_event_commitments"].append(
                {
                    "event_id": event_id,
                    "event_sha256": _digest(event),
                }
            )
        result = str(event["first_result"])
        if result not in row["first_result_classes"]:
            row["first_result_classes"].append(result)
        row["source_gaps"] = sorted(set(row["source_gaps"]) | set(gaps))
        row["priority"] = max(int(row["priority"]), severity[result])

    rows = list(grouped.values())
    for row in rows:
        row["trigger_event_ids"].sort()
        row["trigger_event_commitments"].sort(key=lambda item: item["event_id"])
        row["first_result_classes"].sort()
        row["trigger_event_count"] = len(row["trigger_event_ids"])
        row["trigger_event_set_sha256"] = _digest(row["trigger_event_commitments"])
        row["trigger_event_ids"] = row["trigger_event_ids"][-8:]
        row["trigger_event_commitments"] = row["trigger_event_commitments"][-8:]
        row["planner_status"] = (
            "source_gap" if row["source_gaps"] else "ready_for_prepare"
        )
    rows.sort(
        key=lambda row: (
            -int(row["priority"]),
            -int(row["trigger_event_count"]),
            row["demand_id"],
        )
    )
    return rows, counts


def build_plan(
    repo: str | Path,
    *,
    study_date: str,
    as_of: str,
    limit: int = DEFAULT_LIMIT,
    write: bool = True,
) -> dict[str, Any]:
    if write:
        _retired_write_surface()
    root = Path(repo).expanduser().resolve()
    study_date = _valid_date(study_date, "study date")
    as_of = _valid_date(as_of, "as-of date")
    if as_of < study_date:
        raise VariantDemandError("as-of date must not precede study date")
    if not isinstance(limit, int) or not 1 <= limit <= MAX_LIMIT:
        raise VariantDemandError(f"limit must be between 1 and {MAX_LIMIT}")
    inputs = _verified_inputs(root, as_of=as_of)
    inventory, inventory_sha = _evidence_inventory(root)
    candidates, source_counts = _candidate_rows(
        root,
        inputs,
        study_date=study_date,
        as_of=as_of,
        evidence_inventory=inventory,
    )
    selected = candidates[:limit]
    counts = {
        **source_counts,
        "candidate_demands": len(candidates),
        "selected_demands": len(selected),
        "ready_for_prepare": sum(
            row["planner_status"] == "ready_for_prepare" for row in selected
        ),
        "source_gap": sum(row["planner_status"] == "source_gap" for row in selected),
        "truncated_demands": max(0, len(candidates) - len(selected)),
    }
    plan_material = {
        "study_date": study_date,
        "as_of": as_of,
        "limit": limit,
        "projection_commitment": inputs["projection_commitment"],
        "evidence_inventory_sha256": inventory_sha,
        "demands": selected,
        "counts": counts,
    }
    plan_id = "VDP-" + _digest(plan_material)[:24].upper()
    plan = {
        "schema": PLAN_SCHEMA,
        "kind": "bounded_nightly_variant_demand",
        "plan_id": plan_id,
        "answer_safe": True,
        "study_date": study_date,
        "as_of": as_of,
        "limit": limit,
        "cooldown_days": 7,
        "projection_commitment": inputs["projection_commitment"],
        "source_hashes": inputs["source_hashes"],
        "evidence_inventory_sha256": inventory_sha,
        "counts": counts,
        "demands": selected,
        "formal_write_count": 0,
    }
    _assert_answer_safe(plan)
    plan_sha = _sha256_bytes(_pretty_bytes(plan))
    receipt = {
        "schema": PLAN_RECEIPT_SCHEMA,
        "receipt_id": "VDR-" + _digest({"plan_id": plan_id, "plan_sha256": plan_sha})[:24].upper(),
        "plan_id": plan_id,
        "plan_sha256": plan_sha,
        "projection_commitment": inputs["projection_commitment"],
        "study_date": study_date,
        "as_of": as_of,
        "limit": limit,
        "counts": counts,
        "answer_safe": True,
        "formal_write_count": 0,
    }
    _assert_answer_safe(receipt)
    result = {
        "status": "planned",
        "plan_id": plan_id,
        "plan_sha256": plan_sha,
        "counts": counts,
        "demands": selected,
        "formal_write_count": 0,
    }
    if not write:
        return result | {"plan": plan, "receipt": receipt}

    _, _, demand_root = _roots(root)
    with _lock(demand_root / ".variant-demand.lock"):
        plan_path = demand_root / "plans" / f"{plan_id}.json"
        receipt_path = demand_root / "receipts" / f"{plan_id}.json"
        if plan_path.is_file() and _load_object(plan_path, "variant demand plan") != plan:
            raise VariantDemandError("content-addressed plan identity conflict")
        if receipt_path.is_file() and _load_object(receipt_path, "variant demand receipt") != receipt:
            raise VariantDemandError("content-addressed receipt identity conflict")
        _atomic_write_json(plan_path, plan)
        _atomic_write_json(receipt_path, receipt)
        _atomic_write_json(demand_root / "current.json", plan)
    return result | {
        "plan_ref": plan_path.relative_to(root).as_posix(),
        "receipt_ref": receipt_path.relative_to(root).as_posix(),
        "receipt_sha256": _sha256_file(receipt_path),
    }


def _load_current_plan(repo: Path) -> dict[str, Any]:
    _, _, demand_root = _roots(repo)
    current = _load_object(demand_root / "current.json", "current variant demand plan")
    plan_id = str(current.get("plan_id") or "")
    plan_path = demand_root / "plans" / f"{plan_id}.json"
    receipt_path = demand_root / "receipts" / f"{plan_id}.json"
    plan = _load_object(plan_path, "content-addressed variant demand plan")
    receipt = _load_object(receipt_path, "variant demand plan receipt")
    if (
        current != plan
        or plan.get("schema") != PLAN_SCHEMA
        or plan.get("answer_safe") is not True
        or plan.get("formal_write_count") != 0
        or receipt.get("schema") != PLAN_RECEIPT_SCHEMA
        or receipt.get("plan_id") != plan_id
        or receipt.get("plan_sha256") != _sha256_file(plan_path)
    ):
        raise VariantDemandError("current variant demand plan or receipt drifted")
    _assert_answer_safe(plan)
    return plan


def _workflow_events(path: Path) -> list[dict[str, Any]]:
    if not path.is_file():
        return []
    events: list[dict[str, Any]] = []
    keys: dict[str, dict[str, Any]] = {}
    previous_sha: str | None = None
    for line_number, raw in enumerate(path.read_bytes().splitlines(keepends=True), start=1):
        if not raw.strip():
            continue
        try:
            event = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise VariantDemandError(
                f"variant demand workflow ledger is corrupt at line {line_number}"
            ) from exc
        if (
            not isinstance(event, dict)
            or event.get("schema") != WORKFLOW_EVENT_SCHEMA
            or event.get("formal_write_count") != 0
            or event.get("previous_event_sha256") != previous_sha
        ):
            raise VariantDemandError("variant demand workflow ledger chain is invalid")
        key = str(event.get("idempotency_key") or "")
        if not SAFE_KEY_RE.fullmatch(key):
            raise VariantDemandError("variant demand workflow idempotency key is invalid")
        if key in keys and keys[key] != event:
            raise VariantDemandError("variant demand workflow idempotency conflict")
        keys[key] = event
        events.append(event)
        previous_sha = _sha256_bytes(raw)
    return events


def _recorded_at(value: str | None) -> str:
    if value:
        try:
            parsed = datetime.fromisoformat(value)
        except ValueError as exc:
            raise VariantDemandError("recorded-at must be an ISO timestamp") from exc
        if parsed.tzinfo is None:
            raise VariantDemandError("recorded-at must include an offset")
        return value
    return datetime.now(ZoneInfo("Asia/Shanghai")).isoformat(timespec="seconds")


def _append_workflow_event(
    repo: Path,
    *,
    plan: dict[str, Any],
    demand: dict[str, Any],
    event_type: str,
    idempotency_key: str,
    payload: dict[str, Any],
    recorded_at: str | None = None,
) -> dict[str, Any]:
    if not SAFE_KEY_RE.fullmatch(idempotency_key):
        raise VariantDemandError("idempotency key is invalid")
    _, _, demand_root = _roots(repo)
    ledger = demand_root / "events.jsonl"
    with _lock(demand_root / ".variant-demand.lock"):
        events = _workflow_events(ledger)
        existing = next(
            (event for event in events if event["idempotency_key"] == idempotency_key),
            None,
        )
        requested = {
            "plan_id": plan["plan_id"],
            "demand_id": demand["demand_id"],
            "event_type": event_type,
            **payload,
        }
        if existing:
            comparable = {key: existing.get(key) for key in requested}
            if comparable != requested:
                raise VariantDemandError("idempotency key was already used for another event")
            return existing
        previous_sha = None
        if ledger.is_file() and ledger.read_bytes():
            previous_sha = _sha256_bytes(ledger.read_bytes().splitlines(keepends=True)[-1])
        base = {
            "schema": WORKFLOW_EVENT_SCHEMA,
            **requested,
            "idempotency_key": idempotency_key,
            "recorded_at": _recorded_at(recorded_at),
            "previous_event_sha256": previous_sha,
            "formal_write_count": 0,
        }
        base["event_id"] = "VDE-" + _digest(base)[:24].upper()
        line = _canonical_bytes(base)
        ledger.parent.mkdir(parents=True, exist_ok=True)
        with ledger.open("ab") as handle:
            handle.write(line)
            handle.flush()
            os.fsync(handle.fileno())
        return base


def _demand_by_id(plan: dict[str, Any], demand_id: str) -> dict[str, Any]:
    matches = [row for row in plan.get("demands", []) if row.get("demand_id") == demand_id]
    if len(matches) != 1:
        raise VariantDemandError("demand ID is not uniquely present in the current plan")
    return matches[0]


def prepare_demand(
    repo: str | Path,
    *,
    demand_id: str,
    evidence_path: str | Path,
) -> dict[str, Any]:
    _retired_write_surface()
    root = Path(repo).expanduser().resolve()
    plan = _load_current_plan(root)
    demand = _demand_by_id(plan, demand_id)
    blocking = set(demand.get("source_gaps") or []) - {
        "authoring_evidence_missing",
        "authoring_evidence_ambiguous",
        "sealed_solution_or_evidence_invalid",
    }
    if blocking:
        raise VariantDemandError("demand has an unresolved original or target source gap")
    current = _verified_inputs(root, as_of=str(plan["as_of"]))
    if current["projection_commitment"] != plan.get("projection_commitment"):
        raise VariantDemandError("personalization inputs changed; rerun variant demand plan")
    try:
        evidence = validate_authoring_evidence(root, evidence_path)
    except VariantPipelineError as exc:
        raise VariantDemandError("authoring evidence or sealed solution is invalid") from exc
    expected = {
        "anchor_formal_node_id": demand["anchor_formal_node_id"],
        "knowledge_point": demand["knowledge_point"],
        "mechanism_key": demand["mechanism_key"],
        "anchor_source_ref": demand["original_source_ref"],
        "anchor_source_sha256": demand["original_source_sha256"],
    }
    if any(evidence.get(key) != value for key, value in expected.items()):
        raise VariantDemandError("authoring evidence does not match the planned demand")
    prepared = prepare_generation(root, evidence_path)
    key = f"prepare:{demand_id}:{evidence['evidence_sha256']}"
    event = _append_workflow_event(
        root,
        plan=plan,
        demand=demand,
        event_type="prepared",
        idempotency_key=key,
        payload={
            "generation_request_ref": prepared["generation_request_ref"],
            "generation_request_sha256": prepared["generation_request_sha256"],
            "author_identity_sha256": prepared["author_identity_sha256"],
            "verifier_identity_sha256": prepared["verifier_identity_sha256"],
        },
    )
    return {
        "status": "prepared",
        "plan_id": plan["plan_id"],
        "demand_id": demand_id,
        "workflow_event_id": event["event_id"],
        **prepared,
        "formal_write_count": 0,
    }


def mark_demand(
    repo: str | Path,
    *,
    demand_id: str,
    outcome: str,
    reason: str,
    idempotency_key: str,
    variant_source_id: str | None = None,
    recorded_at: str | None = None,
) -> dict[str, Any]:
    _retired_write_surface()
    root = Path(repo).expanduser().resolve()
    plan = _load_current_plan(root)
    demand = _demand_by_id(plan, demand_id)
    if outcome not in ALLOWED_MARK_OUTCOMES or reason not in ALLOWED_MARK_REASONS:
        raise VariantDemandError("mark outcome or reason is not allowed")
    payload: dict[str, Any] = {"reason_code": reason}
    if outcome == "finalized":
        if reason != "verified_variant_published":
            raise VariantDemandError("finalized demand requires verified publication reason")
        state, _, _ = _roots(root)
        catalog_path = state / "variant-catalog.json"
        catalog = _load_object(catalog_path, "verified variant catalog")
        try:
            validate_variant_catalog(root, catalog)
        except VariantPipelineError as exc:
            raise VariantDemandError("verified variant catalog failed deep validation") from exc
        manifest = _load_object(state / "manifest.json", "personalization manifest")
        if manifest.get("variant_catalog_sha256") != _sha256_file(catalog_path):
            raise VariantDemandError("verified variant catalog is not manifest-bound")
        matches = [
            row
            for row in catalog.get("entries", [])
            if isinstance(row, dict)
            and row.get("status") == "verified_ready"
            and row.get("anchor_formal_node_id") == demand["anchor_formal_node_id"]
            and row.get("knowledge_point") == demand["knowledge_point"]
            and row.get("mechanism_key") == demand["mechanism_key"]
            and (not variant_source_id or row.get("source_id") == variant_source_id)
        ]
        if len(matches) != 1:
            raise VariantDemandError("no unique verified variant satisfies this demand")
        payload.update(
            {
                "variant_source_id": matches[0]["source_id"],
                "variant_source_ref": matches[0]["source_ref"],
                "variant_source_sha256": matches[0]["source_sha256"],
                "verification_receipt_sha256": matches[0][
                    "verification_receipt_sha256"
                ],
            }
        )
    elif variant_source_id:
        raise VariantDemandError("variant source ID is only valid for finalized demand")
    event = _append_workflow_event(
        root,
        plan=plan,
        demand=demand,
        event_type=outcome,
        idempotency_key=idempotency_key,
        payload=payload,
        recorded_at=recorded_at,
    )
    return {
        "status": outcome,
        "plan_id": plan["plan_id"],
        "demand_id": demand_id,
        "workflow_event_id": event["event_id"],
        "formal_write_count": 0,
    }


def demand_status(repo: str | Path) -> dict[str, Any]:
    root = Path(repo).expanduser().resolve()
    plan = _load_current_plan(root)
    _, _, demand_root = _roots(root)
    events = _workflow_events(demand_root / "events.jsonl")
    by_demand: dict[str, list[dict[str, Any]]] = {}
    for event in events:
        by_demand.setdefault(str(event.get("demand_id") or ""), []).append(event)
    rows: list[dict[str, Any]] = []
    counts: dict[str, int] = {}
    for demand in plan.get("demands", []):
        history = by_demand.get(demand["demand_id"], [])
        workflow = str(history[-1]["event_type"]) if history else demand["planner_status"]
        counts[workflow] = counts.get(workflow, 0) + 1
        rows.append(
            {
                "demand_id": demand["demand_id"],
                "anchor_formal_node_id": demand["anchor_formal_node_id"],
                "knowledge_point": demand["knowledge_point"],
                "mechanism_key": demand["mechanism_key"],
                "planner_status": demand["planner_status"],
                "workflow_status": workflow,
                "source_gaps": demand["source_gaps"],
                "last_workflow_event_id": history[-1]["event_id"] if history else None,
                "formal_write_count": 0,
            }
        )
    return {
        "status": "ok",
        "plan_id": plan["plan_id"],
        "study_date": plan["study_date"],
        "as_of": plan["as_of"],
        "counts": dict(sorted(counts.items())),
        "demands": rows,
        "formal_write_count": 0,
    }


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo", type=Path, default=Path.cwd())
    sub = parser.add_subparsers(dest="command", required=True)
    plan = sub.add_parser("plan")
    plan.add_argument("--date", required=True)
    plan.add_argument("--as-of", required=True)
    plan.add_argument("--limit", type=int, default=DEFAULT_LIMIT)
    sub.add_parser("status")
    prepare = sub.add_parser("prepare-demand")
    prepare.add_argument("--demand-id", required=True)
    prepare.add_argument("--evidence", type=Path, required=True)
    mark = sub.add_parser("mark")
    mark.add_argument("--demand-id", required=True)
    mark.add_argument("--outcome", choices=sorted(ALLOWED_MARK_OUTCOMES), required=True)
    mark.add_argument("--reason", choices=sorted(ALLOWED_MARK_REASONS), required=True)
    mark.add_argument("--idempotency-key", required=True)
    mark.add_argument("--variant-source-id")
    mark.add_argument("--recorded-at")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        if args.command == "plan":
            result = build_plan(
                args.repo,
                study_date=args.date,
                as_of=args.as_of,
                limit=args.limit,
            )
        elif args.command == "status":
            result = demand_status(args.repo)
        elif args.command == "prepare-demand":
            result = prepare_demand(
                args.repo,
                demand_id=args.demand_id,
                evidence_path=args.evidence,
            )
        else:
            result = mark_demand(
                args.repo,
                demand_id=args.demand_id,
                outcome=args.outcome,
                reason=args.reason,
                idempotency_key=args.idempotency_key,
                variant_source_id=args.variant_source_id,
                recorded_at=args.recorded_at,
            )
    except VariantDemandError as exc:
        print(json.dumps({"status": "error", "reason": str(exc)}, ensure_ascii=False))
        return 2
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    sys.exit(main())
