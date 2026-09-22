#!/usr/bin/env python3
"""Append-only formal-review-to-campaign admission successor for 408.

The writer proves an existing formal result and formal review binding, then
admits or refreshes that formal node for the next unfrozen campaign run.  It
never calls formal apply and never edits campaign scheduler/runtime objects.
"""

from __future__ import annotations

import argparse
import datetime as dt
import fcntl
import hashlib
import json
import os
import re
import sys
import tempfile
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator, Sequence

import formal_intake_review_backflow_408 as review_backflow
import intake_fact_capture_408 as capture_ledger
from formal_intake_formal_proof_408 import (
    DEFAULT_RUNTIME_ROOT,
    FormalProof,
    FormalProofError,
    verify_formal_proof,
)


CONTRACT_VERSION = "campaign-node-admission-v1"
EVENT_SCHEMA = "campaign_node_admission_event_v1"
ENSURE_RECEIPT_SCHEMA = "campaign_node_admission_ensure_receipt_v1"
ADMISSION_RECEIPT_SCHEMA = "campaign_node_admission_receipt_v1"
REFRESH_REQUEST_SCHEMA = "campaign_forecast_refresh_request_v1"
HANDOFF_RECEIPT_SCHEMA = "campaign_forecast_handoff_receipt_v1"
AUDIT_SCHEMA = "campaign_node_admission_history_audit_v1"
COHORT_SCHEMA = "campaign_formal_node_cohort_snapshot_v1"

STATUS_REVIEW_PENDING = "FORMAL_COMMITTED_REVIEW_BACKFLOW_PENDING"
STATUS_ADMISSION_PENDING = "FORMAL_BOUND_CAMPAIGN_ADMISSION_PENDING"
STATUS_FORECAST_PENDING = "CAMPAIGN_ADMITTED_FORECAST_PENDING"
STATUS_COMPLETE = "CAMPAIGN_ADMISSION_COMPLETE"
STATUS_NO_FORMAL_RESULT = "NO_FORMAL_SUCCESS_RESULT"

ROOT_REL = Path(
    "wiki/study_vaults/408-full/state/review-campaign/formal-admission"
)
LEDGER_REL = ROOT_REL / "events.jsonl"
INTAKE_LEDGER_REL = Path(
    "wiki/study_vaults/408-full/state/intake-curation/events.jsonl"
)
MASTER_REL = Path("节点总表.md")
MAPPING_REL = Path("复习单元节点映射.md")
EFFECTIVE_DATE = dt.date(2026, 8, 31)
DEFAULT_CAMPAIGN_ID = "RC-408-2026-110D"
DEFAULT_CAMPAIGN_POLICY = "morning-node-coverage-campaign-v1"

FORMAL_NODE_RE = re.compile(r"^(?:DS|CO|OS|CN)_(?:\d{4}|UNK)_\d{3}$")
REVIEW_UNIT_RE = re.compile(r"^RU_[A-Za-z0-9._-]+$")
SAFE_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:/+-]{0,255}$")
SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
ADMISSION_TYPES = {"campaign_node_admitted", "campaign_node_refreshed"}
CAMPAIGN_DISPOSITIONS = {
    "admitted",
    "refreshed",
    "authority_excluded",
    "blocked",
}
DISPOSITION_EVENT_TYPES = {
    "campaign_node_admitted": "admitted",
    "campaign_node_refreshed": "refreshed",
    "campaign_node_authority_excluded": "authority_excluded",
    "campaign_node_blocked": "blocked",
}
SUCCESS_HANDOFF_STATUSES = {"accepted", "refreshed"}
ZERO_COUNTS = {
    "formal_apply_count": 0,
    "formal_write_count": 0,
    "review_backflow_write_count": 0,
    "learner_outcome_write_count": 0,
    "luna_call_count": 0,
    "provider_call_count": 0,
    "mcp_call_count": 0,
}


class AdmissionError(RuntimeError):
    """Admission evidence is absent, inconsistent, or unsafe to mutate."""


def _canonical(value: Any) -> bytes:
    return json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")


def _sha_value(value: Any) -> str:
    return hashlib.sha256(_canonical(value)).hexdigest()


def _sha_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _now() -> str:
    return dt.datetime.now(dt.timezone.utc).replace(microsecond=0).isoformat()


def _repo(repo_root: str | Path) -> Path:
    path = Path(repo_root).expanduser().resolve()
    if not path.is_dir():
        raise AdmissionError(f"repo does not exist: {path}")
    return path


def _safe_id(value: object, label: str) -> str:
    text = str(value or "").strip()
    if not SAFE_ID_RE.fullmatch(text):
        raise AdmissionError(f"{label} is invalid")
    return text


def _sha(value: object, label: str) -> str:
    text = str(value or "")
    if not SHA256_RE.fullmatch(text):
        raise AdmissionError(f"{label} must be lowercase SHA-256")
    return text


def _date(value: object, label: str) -> str:
    try:
        return dt.date.fromisoformat(str(value or "")).isoformat()
    except ValueError as exc:
        raise AdmissionError(f"{label} must be YYYY-MM-DD") from exc


def _read_jsonl(path: Path, label: str) -> list[dict[str, Any]]:
    if not path.is_file():
        return []
    rows: list[dict[str, Any]] = []
    for line_number, raw in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        if not raw.strip():
            continue
        try:
            row = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise AdmissionError(f"{label} is corrupt at line {line_number}") from exc
        if not isinstance(row, dict):
            raise AdmissionError(f"{label} line {line_number} is not an object")
        rows.append(row)
    return rows


def _atomic_json(path: Path, value: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    payload = json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    descriptor, temporary = tempfile.mkstemp(
        prefix=f".{path.name}.", suffix=".tmp", dir=path.parent
    )
    try:
        os.fchmod(descriptor, 0o600)
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


@contextmanager
def _locked(repo: Path) -> Iterator[None]:
    root = repo / ROOT_REL
    root.mkdir(parents=True, exist_ok=True, mode=0o700)
    descriptor = os.open(root / ".lock", os.O_RDWR | os.O_CREAT, 0o600)
    try:
        fcntl.flock(descriptor, fcntl.LOCK_EX)
        yield
    finally:
        fcntl.flock(descriptor, fcntl.LOCK_UN)
        os.close(descriptor)


def _event_material(event: dict[str, Any]) -> dict[str, Any]:
    material = dict(event)
    material.pop("event_sha256", None)
    return material


def _read_events(repo: Path) -> list[dict[str, Any]]:
    rows = _read_jsonl(repo / LEDGER_REL, "campaign admission ledger")
    event_ids: set[str] = set()
    idempotency: dict[str, str] = {}
    for row in rows:
        if row.get("schema") != EVENT_SCHEMA:
            raise AdmissionError("campaign admission ledger schema drifted")
        event_sha = str(row.get("event_sha256") or "")
        if not SHA256_RE.fullmatch(event_sha) or _sha_value(_event_material(row)) != event_sha:
            raise AdmissionError("campaign admission event hash drifted")
        event_id = _safe_id(row.get("event_id"), "event_id")
        key = _safe_id(row.get("idempotency_key"), "idempotency_key")
        if event_id in event_ids:
            raise AdmissionError("duplicate campaign admission event_id")
        if key in idempotency and idempotency[key] != event_sha:
            raise AdmissionError("campaign admission idempotency conflict")
        event_ids.add(event_id)
        idempotency[key] = event_sha
    return rows


def _append_event(repo: Path, event: dict[str, Any]) -> dict[str, Any]:
    events = _read_events(repo)
    key = _safe_id(event.get("idempotency_key"), "idempotency_key")
    material = _event_material(event)
    expected_sha = _sha_value(material)
    prior = [row for row in events if row.get("idempotency_key") == key]
    if prior:
        if len(prior) != 1 or prior[0].get("event_sha256") != expected_sha:
            raise AdmissionError("idempotency key was reused with different material")
        return prior[0]
    row = dict(material)
    row["event_sha256"] = expected_sha
    path = repo / LEDGER_REL
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n")
        handle.flush()
        os.fsync(handle.fileno())
    return row


def _object_dir(repo: Path, kind: str) -> Path:
    return repo / ROOT_REL / kind


def _write_object(repo: Path, kind: str, value: dict[str, Any]) -> dict[str, Any]:
    digest = _sha_value(value)
    path = _object_dir(repo, kind) / f"{digest}.json"
    if path.exists():
        existing = _read_json_file(path, kind)
        if _sha_value(existing) != digest:
            raise AdmissionError(f"{kind} object hash conflict")
    else:
        _atomic_json(path, value)
    returned = dict(value)
    returned["object_sha256"] = digest
    if "receipt" in kind:
        returned["receipt_sha256"] = digest
    returned["object_path"] = path.relative_to(repo).as_posix()
    return returned


def _read_json_file(path: Path, label: str) -> dict[str, Any]:
    if path.is_symlink() or not path.is_file():
        raise AdmissionError(f"{label} must be a non-symlink regular JSON file")
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise AdmissionError(f"{label} is invalid JSON") from exc
    if not isinstance(value, dict):
        raise AdmissionError(f"{label} must contain an object")
    return value


def _read_object(repo: Path, kind: str, digest: str) -> dict[str, Any]:
    sha = _sha(digest, f"{kind}_sha256")
    path = _object_dir(repo, kind) / f"{sha}.json"
    value = _read_json_file(path, kind)
    if _sha_value(value) != sha:
        raise AdmissionError(f"{kind} content hash drifted")
    returned = dict(value)
    returned["object_sha256"] = sha
    if "receipt" in kind:
        returned["receipt_sha256"] = sha
    returned["object_path"] = path.relative_to(repo).as_posix()
    return returned


def _markdown_table(path: Path, required: set[str]) -> list[dict[str, str]]:
    if path.is_symlink() or not path.is_file():
        raise AdmissionError(f"canonical table is missing: {path.name}")
    header: list[str] | None = None
    rows: list[dict[str, str]] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.startswith("|"):
            continue
        cells = [cell.strip() for cell in line.strip().strip("|").split("|")]
        if header is None and required <= set(cells):
            header = cells
            continue
        if header is None or all(cell and set(cell) <= {"-", ":"} for cell in cells):
            continue
        if len(cells) == len(header):
            rows.append(dict(zip(header, cells)))
    if header is None:
        raise AdmissionError(f"canonical table headers are missing: {path.name}")
    return rows


def _cohort_snapshot(
    repo: Path, campaign_id: str, campaign_policy_version: str
) -> dict[str, Any]:
    master = repo / MASTER_REL
    rows = _markdown_table(master, {"ID"})
    node_ids = [str(row.get("ID") or "") for row in rows]
    if not node_ids or any(not FORMAL_NODE_RE.fullmatch(node) for node in node_ids):
        raise AdmissionError("master table contains an invalid formal node identity")
    if len(set(node_ids)) != len(node_ids):
        raise AdmissionError("master table contains duplicate formal node identities")
    snapshot = {
        "schema": COHORT_SCHEMA,
        "campaign_id": _safe_id(campaign_id, "campaign_id"),
        "campaign_policy_version": _safe_id(
            campaign_policy_version, "campaign_policy_version"
        ),
        "formal_node_count": len(node_ids),
        "formal_node_ids": sorted(node_ids),
        "master_ref": MASTER_REL.as_posix(),
        "master_sha256": _sha_file(master),
    }
    snapshot["cohort_sha256"] = _sha_value(snapshot)
    return snapshot


def _real_review_unit(repo: Path, formal_node_id: str) -> str:
    rows = _markdown_table(repo / MAPPING_REL, {"正式节点ID", "主复习单元ID"})
    matches = [row for row in rows if row.get("正式节点ID") == formal_node_id]
    units = {str(row.get("主复习单元ID") or "") for row in matches}
    if len(matches) != 1 or len(units) != 1:
        raise AdmissionError("formal node lacks one canonical primary review unit")
    unit = next(iter(units))
    if not REVIEW_UNIT_RE.fullmatch(unit):
        raise AdmissionError("canonical primary review unit is invalid")
    return unit


def _formal_results(repo: Path) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    intake_events = _read_jsonl(repo / INTAKE_LEDGER_REL, "intake ledger")
    try:
        state = capture_ledger.replay(intake_events)
    except capture_ledger.CaptureError as exc:
        raise AdmissionError(str(exc)) from exc
    successes = [
        row
        for row in intake_events
        if row.get("event_type") == "curation_item_result"
        and row.get("outcome") in {"curated", "already_current"}
    ]
    return successes, state


def _result_for(
    repo: Path, batch_id: str, capture_id: str
) -> tuple[dict[str, Any], dict[str, Any]]:
    successes, state = _formal_results(repo)
    matches = [
        row
        for row in successes
        if row.get("batch_id") == batch_id and row.get("capture_id") == capture_id
    ]
    if len(matches) != 1:
        raise AdmissionError("batch/capture does not have one canonical formal-success result")
    captured = state.get("captures", {}).get(capture_id)
    batch = state.get("batches", {}).get(batch_id)
    if not isinstance(captured, dict) or not isinstance(batch, dict):
        raise AdmissionError("canonical batch/capture replay is missing")
    if capture_id not in list(batch.get("capture_ids") or []):
        raise AdmissionError("capture is outside the canonical batch")
    payload = captured.get("capture")
    if not isinstance(payload, dict):
        raise AdmissionError("canonical capture payload is missing")
    study_date = _date(payload.get("study_date"), "study_date")
    if batch.get("study_date") != study_date:
        raise AdmissionError("batch and capture study_date conflict")
    return matches[0], payload


def _result_proof_digest(result: dict[str, Any]) -> str:
    field = "receipt_sha256" if result.get("outcome") == "curated" else "verification_sha256"
    return _sha(result.get(field), f"formal result {field}")


def _validate_review_binding(
    repo: Path, result: dict[str, Any]
) -> dict[str, Any]:
    binding_sha = _sha(
        result.get("formal_review_binding_receipt_sha256"),
        "formal_review_binding_receipt_sha256",
    )
    try:
        final = review_backflow.validate_review_backflow_receipt(
            repo,
            binding_sha,
            expected_schema=review_backflow.FINAL_RECEIPT_SCHEMA,
        )
    except (review_backflow.BackflowError, OSError) as exc:
        raise AdmissionError(f"formal review binding is not verified: {exc}") from exc
    if final.get("status") not in review_backflow.SUCCESS_FINAL_STATUSES:
        raise AdmissionError("formal review binding is still pending")
    proof_sha = _result_proof_digest(result)
    expected = {
        "batch_id": result.get("batch_id"),
        "capture_id": result.get("capture_id"),
        "formal_result_status": result.get("outcome"),
        "formal_id": result.get("formal_id"),
        "formal_node_id": result.get("formal_id"),
        "formal_proof_sha256": proof_sha,
        "review_unit_id": result.get("formal_review_unit_id"),
    }
    if any(final.get(key) != value for key, value in expected.items()):
        raise AdmissionError("formal review binding receipt conflicts with intake result")
    return final


def _formal_proof(
    repo: Path,
    result: dict[str, Any],
    *,
    runtime_root: str | Path,
) -> FormalProof:
    try:
        return verify_formal_proof(
            repo,
            batch_id=str(result["batch_id"]),
            capture_id=str(result["capture_id"]),
            formal_id=str(result["formal_id"]),
            formal_result_status=str(result["outcome"]),
            formal_result_receipt_sha256=_result_proof_digest(result),
            runtime_root=runtime_root,
            historical_result_event=result,
        )
    except (FormalProofError, OSError) as exc:
        raise AdmissionError(f"formal result proof is not verified: {exc}") from exc


def _formal_mode(result: dict[str, Any], proof: FormalProof) -> str:
    if result.get("outcome") == "already_current":
        return "already_current"
    items = proof.material.get("items") if isinstance(proof.material, dict) else None
    if not isinstance(items, list) or len(items) != 1 or not isinstance(items[0], dict):
        raise AdmissionError("normal formal receipt lacks one result item")
    mode = str(items[0].get("mode") or "")
    if mode not in {"new", "redo"}:
        raise AdmissionError("normal formal receipt mode is not new/redo")
    return mode


def _authority(
    repo: Path,
    *,
    batch_id: str,
    capture_id: str,
    runtime_root: str | Path,
    campaign_id: str,
    campaign_policy_version: str,
) -> dict[str, Any]:
    batch = _safe_id(batch_id, "batch_id")
    capture = _safe_id(capture_id, "capture_id")
    result, capture_payload = _result_for(repo, batch, capture)
    formal_node_id = str(result.get("formal_id") or "")
    if not FORMAL_NODE_RE.fullmatch(formal_node_id):
        raise AdmissionError("formal result has an invalid formal node identity")
    final = _validate_review_binding(repo, result)
    proof = _formal_proof(repo, result, runtime_root=runtime_root)
    review_unit_id = _real_review_unit(repo, formal_node_id)
    if proof.review_unit_id != review_unit_id or final.get("review_unit_id") != review_unit_id:
        raise AdmissionError("formal proof, review binding, and real RU disagree")
    cohort = _cohort_snapshot(repo, campaign_id, campaign_policy_version)
    if formal_node_id not in cohort["formal_node_ids"]:
        raise AdmissionError("formal node is absent from the canonical campaign cohort")
    mode = _formal_mode(result, proof)
    event_type = "campaign_node_admitted" if mode == "new" else "campaign_node_refreshed"
    authority = {
        "schema": "campaign_node_admission_authority_v1",
        "contract_version": CONTRACT_VERSION,
        "batch_id": batch,
        "capture_id": capture,
        "study_date": _date(capture_payload.get("study_date"), "study_date"),
        "formal_result_event_id": _safe_id(result.get("event_id"), "formal result event_id"),
        "formal_result_event_sha256": _sha_value(result),
        "formal_result_status": result.get("outcome"),
        "formal_result_receipt_sha256": _result_proof_digest(result),
        "formal_review_binding_receipt_sha256": final["receipt_sha256"],
        "formal_review_binding_event_id": final.get("formal_binding_event_id"),
        "formal_node_id": formal_node_id,
        "review_unit_id": review_unit_id,
        "formal_mode": mode,
        "admission_event_type": event_type,
        "campaign_id": cohort["campaign_id"],
        "campaign_policy_version": cohort["campaign_policy_version"],
        "campaign_cohort_sha256": cohort["cohort_sha256"],
        "campaign_cohort_count": cohort["formal_node_count"],
        "target_run_policy": "next_unfrozen_run_only",
        "frozen_run_mutation_allowed": False,
        "active_run_mutation_allowed": False,
    }
    authority["authority_sha256"] = _sha_value(authority)
    authority["_cohort"] = cohort
    return authority


def _public_authority(authority: dict[str, Any]) -> dict[str, Any]:
    return {key: value for key, value in authority.items() if not key.startswith("_")}


def ensure_campaign_admission(
    repo_root: str | Path,
    *,
    batch_id: str,
    capture_id: str,
    runtime_root: str | Path = DEFAULT_RUNTIME_ROOT,
    campaign_id: str = DEFAULT_CAMPAIGN_ID,
    campaign_policy_version: str = DEFAULT_CAMPAIGN_POLICY,
) -> dict[str, Any]:
    """Freeze the exact formal/review/cohort authority; append no admission."""

    repo = _repo(repo_root)
    authority = _authority(
        repo,
        batch_id=batch_id,
        capture_id=capture_id,
        runtime_root=runtime_root,
        campaign_id=campaign_id,
        campaign_policy_version=campaign_policy_version,
    )
    with _locked(repo):
        cohort_object = _write_object(repo, "cohorts", authority["_cohort"])
        payload = {
            "schema": ENSURE_RECEIPT_SCHEMA,
            "status": STATUS_ADMISSION_PENDING,
            "authority": _public_authority(authority),
            "campaign_cohort_object_sha256": cohort_object["object_sha256"],
            "campaign_cohort_ref": cohort_object["object_path"],
            **ZERO_COUNTS,
        }
        return _write_object(repo, "ensure-receipts", payload)


def _validate_ensure(repo: Path, digest: str) -> dict[str, Any]:
    receipt = _read_object(repo, "ensure-receipts", digest)
    if receipt.get("schema") != ENSURE_RECEIPT_SCHEMA:
        raise AdmissionError("ensure receipt schema mismatch")
    if receipt.get("status") != STATUS_ADMISSION_PENDING:
        raise AdmissionError("ensure receipt status mismatch")
    if any(receipt.get(key) != value for key, value in ZERO_COUNTS.items()):
        raise AdmissionError("ensure receipt claims a forbidden write/call")
    authority = receipt.get("authority")
    if not isinstance(authority, dict):
        raise AdmissionError("ensure receipt authority is missing")
    supplied_sha = str(authority.get("authority_sha256") or "")
    material = dict(authority)
    material.pop("authority_sha256", None)
    if not SHA256_RE.fullmatch(supplied_sha) or _sha_value(material) != supplied_sha:
        raise AdmissionError("ensure authority hash drifted")
    return receipt


def _find_admission_event(
    events: Sequence[dict[str, Any]], formal_result_event_id: str
) -> dict[str, Any] | None:
    matches = [
        row
        for row in events
        if row.get("event_type") in ADMISSION_TYPES
        and row.get("formal_result_event_id") == formal_result_event_id
    ]
    if not matches:
        return None
    identities = {
        (
            row.get("authority_sha256"),
            row.get("formal_node_id"),
            row.get("review_unit_id"),
            row.get("campaign_cohort_sha256"),
        )
        for row in matches
    }
    if len(matches) != 1 or len(identities) != 1:
        raise AdmissionError("formal result has conflicting campaign admissions")
    return matches[0]


def bind_campaign_admission(
    repo_root: str | Path,
    *,
    ensure_receipt_sha256: str,
    runtime_root: str | Path = DEFAULT_RUNTIME_ROOT,
    idempotency_key: str | None = None,
) -> dict[str, Any]:
    """Append exactly one admitted/refreshed event after reopening authority."""

    repo = _repo(repo_root)
    ensure = _validate_ensure(repo, ensure_receipt_sha256)
    frozen = ensure["authority"]
    current = _authority(
        repo,
        batch_id=str(frozen["batch_id"]),
        capture_id=str(frozen["capture_id"]),
        runtime_root=runtime_root,
        campaign_id=str(frozen["campaign_id"]),
        campaign_policy_version=str(frozen["campaign_policy_version"]),
    )
    current_public = _public_authority(current)
    if current_public != frozen:
        raise AdmissionError("formal/review/cohort authority changed after ensure")
    request = {
        "schema": "campaign_node_admission_request_v1",
        "ensure_receipt_sha256": _sha(ensure_receipt_sha256, "ensure_receipt_sha256"),
        "authority_sha256": frozen["authority_sha256"],
        "event_type": frozen["admission_event_type"],
    }
    request_sha = _sha_value(request)
    key = idempotency_key or (
        f"campaign-admission:{frozen['formal_result_event_id']}:{request_sha[:24]}"
    )
    _safe_id(key, "idempotency_key")
    with _locked(repo):
        events = _read_events(repo)
        prior = _find_admission_event(events, str(frozen["formal_result_event_id"]))
        event_material = {
            "schema": EVENT_SCHEMA,
            "event_type": frozen["admission_event_type"],
            "event_id": f"CAE-{request_sha[:24]}",
            "idempotency_key": key,
            "created_at": str(frozen.get("study_date")) + "T23:59:59+08:00",
            "ensure_receipt_sha256": ensure_receipt_sha256,
            "admission_request_sha256": request_sha,
            **{
                field: frozen[field]
                for field in (
                    "authority_sha256",
                    "batch_id",
                    "capture_id",
                    "study_date",
                    "formal_result_event_id",
                    "formal_result_event_sha256",
                    "formal_result_status",
                    "formal_result_receipt_sha256",
                    "formal_review_binding_receipt_sha256",
                    "formal_review_binding_event_id",
                    "formal_node_id",
                    "review_unit_id",
                    "formal_mode",
                    "campaign_id",
                    "campaign_policy_version",
                    "campaign_cohort_sha256",
                    "campaign_cohort_count",
                    "target_run_policy",
                    "frozen_run_mutation_allowed",
                    "active_run_mutation_allowed",
                )
            },
            **ZERO_COUNTS,
        }
        expected_sha = _sha_value(event_material)
        if prior is not None:
            if prior.get("event_sha256") != expected_sha:
                raise AdmissionError("existing campaign admission conflicts with exact request")
            event = prior
        else:
            event = _append_event(repo, event_material)
        receipt = {
            "schema": ADMISSION_RECEIPT_SCHEMA,
            "status": event["event_type"],
            "ensure_receipt_sha256": ensure_receipt_sha256,
            "admission_event_id": event["event_id"],
            "admission_event_sha256": event["event_sha256"],
            "authority_sha256": event["authority_sha256"],
            "batch_id": event["batch_id"],
            "capture_id": event["capture_id"],
            "study_date": event["study_date"],
            "formal_result_event_id": event["formal_result_event_id"],
            "formal_node_id": event["formal_node_id"],
            "review_unit_id": event["review_unit_id"],
            "campaign_id": event["campaign_id"],
            "campaign_cohort_sha256": event["campaign_cohort_sha256"],
            "target_run_policy": "next_unfrozen_run_only",
            "frozen_run_mutation_allowed": False,
            "active_run_mutation_allowed": False,
            **ZERO_COUNTS,
        }
        return _write_object(repo, "admission-receipts", receipt)


def validate_admission_receipt(
    repo_root: str | Path, receipt_sha256: str
) -> dict[str, Any]:
    repo = _repo(repo_root)
    receipt = _read_object(repo, "admission-receipts", receipt_sha256)
    if receipt.get("schema") != ADMISSION_RECEIPT_SCHEMA:
        raise AdmissionError("admission receipt schema mismatch")
    if receipt.get("status") not in ADMISSION_TYPES:
        raise AdmissionError("admission receipt status is invalid")
    if any(receipt.get(key) != value for key, value in ZERO_COUNTS.items()):
        raise AdmissionError("admission receipt claims a forbidden write/call")
    events = _read_events(repo)
    matches = [row for row in events if row.get("event_id") == receipt.get("admission_event_id")]
    if len(matches) != 1:
        raise AdmissionError("admission receipt does not resolve one event")
    event = matches[0]
    expected = {
        "event_sha256": receipt.get("admission_event_sha256"),
        "event_type": receipt.get("status"),
        "authority_sha256": receipt.get("authority_sha256"),
        "batch_id": receipt.get("batch_id"),
        "capture_id": receipt.get("capture_id"),
        "formal_result_event_id": receipt.get("formal_result_event_id"),
        "formal_node_id": receipt.get("formal_node_id"),
        "review_unit_id": receipt.get("review_unit_id"),
        "campaign_id": receipt.get("campaign_id"),
        "campaign_cohort_sha256": receipt.get("campaign_cohort_sha256"),
    }
    if any(event.get(key) != value for key, value in expected.items()):
        raise AdmissionError("admission receipt/event binding drifted")
    return receipt


def _refresh_request(admission: dict[str, Any]) -> dict[str, Any]:
    request = {
        "schema": REFRESH_REQUEST_SCHEMA,
        "contract_version": CONTRACT_VERSION,
        "admission_receipt_sha256": admission["receipt_sha256"],
        "admission_event_id": admission["admission_event_id"],
        "formal_result_event_id": admission["formal_result_event_id"],
        "formal_node_id": admission["formal_node_id"],
        "review_unit_id": admission["review_unit_id"],
        "study_date": admission["study_date"],
        "campaign_id": admission["campaign_id"],
        "campaign_cohort_sha256": admission["campaign_cohort_sha256"],
        "target_run_policy": "next_unfrozen_run_only",
        "frozen_run_mutation_allowed": False,
        "active_run_mutation_allowed": False,
        "requested_products": ["next_unfrozen_due_run", "coverage_forecast"],
        **ZERO_COUNTS,
    }
    request["request_sha256"] = _sha_value(request)
    return request


def _validate_handoff(
    path: Path,
    digest: str | None,
    request: dict[str, Any],
    admission: dict[str, Any],
) -> tuple[dict[str, Any], str]:
    value = _read_json_file(path.expanduser(), "handoff receipt")
    actual = _sha_value(value)
    if digest is not None and _sha(digest, "handoff_receipt_sha256") != actual:
        raise AdmissionError("handoff receipt path/hash mismatch")
    if value.get("schema") != HANDOFF_RECEIPT_SCHEMA:
        raise AdmissionError("handoff receipt schema mismatch")
    if value.get("status") not in SUCCESS_HANDOFF_STATUSES:
        raise AdmissionError("handoff receipt is not successful")
    expected = {
        "request_sha256": request["request_sha256"],
        "admission_receipt_sha256": admission["receipt_sha256"],
        "formal_node_id": admission["formal_node_id"],
        "campaign_id": admission["campaign_id"],
        "campaign_cohort_sha256": admission["campaign_cohort_sha256"],
        "target_run_policy": "next_unfrozen_run_only",
        "frozen_run_mutation_count": 0,
        "active_run_mutation_count": 0,
        "formal_write_count": 0,
    }
    if any(value.get(key) != expected_value for key, expected_value in expected.items()):
        raise AdmissionError("handoff receipt does not bind the exact refresh request")
    return value, actual


def refresh_campaign_projection(
    repo_root: str | Path,
    *,
    admission_receipt_sha256: str,
    handoff_receipt: str | Path | None = None,
    handoff_receipt_sha256: str | None = None,
) -> dict[str, Any]:
    """Emit a content-addressed handoff; optionally bind its external receipt."""

    repo = _repo(repo_root)
    admission = validate_admission_receipt(repo, admission_receipt_sha256)
    request = _refresh_request(admission)
    with _locked(repo):
        stored_request = _write_object(repo, "refresh-requests", request)
        request_sha = request["request_sha256"]
        request_event_material = {
            "schema": EVENT_SCHEMA,
            "event_type": "campaign_forecast_refresh_requested",
            "event_id": f"CAE-REFRESH-{request_sha[:16]}",
            "idempotency_key": f"campaign-refresh-request:{request_sha[:32]}",
            "created_at": _now(),
            "admission_receipt_sha256": admission_receipt_sha256,
            "refresh_request_sha256": request_sha,
            "refresh_request_object_sha256": stored_request["object_sha256"],
            "formal_result_event_id": admission["formal_result_event_id"],
            "formal_node_id": admission["formal_node_id"],
            "campaign_id": admission["campaign_id"],
            "campaign_cohort_sha256": admission["campaign_cohort_sha256"],
            "target_run_policy": "next_unfrozen_run_only",
            "frozen_run_mutation_allowed": False,
            "active_run_mutation_allowed": False,
            **ZERO_COUNTS,
        }
        # created_at is frozen from an existing event on idempotent retry.
        existing = [
            row
            for row in _read_events(repo)
            if row.get("idempotency_key") == request_event_material["idempotency_key"]
        ]
        if existing:
            request_event_material["created_at"] = existing[0]["created_at"]
        request_event = _append_event(repo, request_event_material)
        if handoff_receipt is None:
            return {
                "status": STATUS_FORECAST_PENDING,
                "admission_receipt_sha256": admission_receipt_sha256,
                "refresh_request_sha256": request_sha,
                "refresh_request_ref": stored_request["object_path"],
                "refresh_request_event_id": request_event["event_id"],
                "next_action": "scheduler/forecast consumer must return an exact handoff receipt",
                **ZERO_COUNTS,
            }
        handoff, handoff_sha = _validate_handoff(
            Path(handoff_receipt), handoff_receipt_sha256, request, admission
        )
        stored_handoff = _write_object(repo, "handoff-receipts", handoff)
        bind_material = {
            "schema": EVENT_SCHEMA,
            "event_type": "campaign_forecast_refresh_bound",
            "event_id": f"CAE-BOUND-{handoff_sha[:16]}",
            "idempotency_key": f"campaign-refresh-bound:{request_sha[:20]}:{handoff_sha[:20]}",
            "created_at": str(handoff.get("created_at") or _now()),
            "admission_receipt_sha256": admission_receipt_sha256,
            "refresh_request_sha256": request_sha,
            "handoff_receipt_sha256": handoff_sha,
            "handoff_receipt_object_sha256": stored_handoff["object_sha256"],
            "formal_result_event_id": admission["formal_result_event_id"],
            "formal_node_id": admission["formal_node_id"],
            "campaign_id": admission["campaign_id"],
            "campaign_cohort_sha256": admission["campaign_cohort_sha256"],
            "target_run_policy": "next_unfrozen_run_only",
            "frozen_run_mutation_allowed": False,
            "active_run_mutation_allowed": False,
            **ZERO_COUNTS,
        }
        bound_event = _append_event(repo, bind_material)
        return {
            "status": STATUS_COMPLETE,
            "admission_receipt_sha256": admission_receipt_sha256,
            "refresh_request_sha256": request_sha,
            "handoff_receipt_sha256": handoff_sha,
            "refresh_bound_event_id": bound_event["event_id"],
            **ZERO_COUNTS,
        }


def _admission_receipt_for_event(repo: Path, event: dict[str, Any]) -> dict[str, Any]:
    directory = _object_dir(repo, "admission-receipts")
    matches: list[dict[str, Any]] = []
    if directory.is_dir() and not directory.is_symlink():
        for path in directory.glob("*.json"):
            if path.is_symlink() or not path.is_file():
                continue
            value = _read_json_file(path, "admission receipt")
            if value.get("admission_event_id") == event.get("event_id"):
                matches.append(value)
    if len(matches) != 1:
        raise AdmissionError("admission event does not have one content-addressed receipt")
    digest = _sha_value(matches[0])
    return validate_admission_receipt(repo, digest)


def admission_status(
    repo_root: str | Path,
    *,
    batch_id: str,
    capture_id: str,
    runtime_root: str | Path = DEFAULT_RUNTIME_ROOT,
    campaign_id: str = DEFAULT_CAMPAIGN_ID,
    campaign_policy_version: str = DEFAULT_CAMPAIGN_POLICY,
) -> dict[str, Any]:
    repo = _repo(repo_root)
    try:
        result, _ = _result_for(repo, batch_id, capture_id)
    except AdmissionError as exc:
        return {"status": STATUS_NO_FORMAL_RESULT, "reason": str(exc), **ZERO_COUNTS}
    try:
        authority = _authority(
            repo,
            batch_id=batch_id,
            capture_id=capture_id,
            runtime_root=runtime_root,
            campaign_id=campaign_id,
            campaign_policy_version=campaign_policy_version,
        )
    except AdmissionError as exc:
        return {
            "status": STATUS_REVIEW_PENDING,
            "batch_id": batch_id,
            "capture_id": capture_id,
            "formal_result_event_id": result.get("event_id"),
            "formal_node_id": result.get("formal_id"),
            "reason": str(exc),
            **ZERO_COUNTS,
        }
    events = _read_events(repo)
    admission_event = _find_admission_event(events, authority["formal_result_event_id"])
    if admission_event is None:
        return {
            "status": STATUS_ADMISSION_PENDING,
            **{
                key: authority[key]
                for key in (
                    "batch_id",
                    "capture_id",
                    "study_date",
                    "formal_result_event_id",
                    "formal_node_id",
                    "review_unit_id",
                    "campaign_id",
                    "campaign_cohort_sha256",
                )
            },
            **ZERO_COUNTS,
        }
    # The admission intentionally freezes the cohort that existed at bind
    # time. Later append-only formal admissions may grow the live cohort; that
    # must not invalidate an older receipt or rewrite its target run. Recheck
    # the immutable formal/review identity here, while retaining the event's
    # receipt-bound cohort for its handoff.
    stable_fields = (
        "batch_id",
        "capture_id",
        "study_date",
        "formal_result_event_id",
        "formal_result_event_sha256",
        "formal_result_status",
        "formal_result_receipt_sha256",
        "formal_review_binding_receipt_sha256",
        "formal_review_binding_event_id",
        "formal_node_id",
        "review_unit_id",
        "formal_mode",
        "campaign_id",
        "campaign_policy_version",
    )
    if any(admission_event.get(field) != authority.get(field) for field in stable_fields):
        raise AdmissionError("existing admission conflicts with current formal/review authority")
    admission = _admission_receipt_for_event(repo, admission_event)
    request = _refresh_request(admission)
    bound = [
        row
        for row in events
        if row.get("event_type") == "campaign_forecast_refresh_bound"
        and row.get("admission_receipt_sha256") == admission["receipt_sha256"]
        and row.get("refresh_request_sha256") == request["request_sha256"]
    ]
    if len(bound) > 1:
        raise AdmissionError("admission has conflicting forecast handoff bindings")
    status = STATUS_COMPLETE if bound else STATUS_FORECAST_PENDING
    return {
        "status": status,
        "batch_id": batch_id,
        "capture_id": capture_id,
        "formal_result_event_id": authority["formal_result_event_id"],
        "formal_node_id": authority["formal_node_id"],
        "review_unit_id": authority["review_unit_id"],
        "campaign_id": authority["campaign_id"],
        "campaign_cohort_sha256": admission_event["campaign_cohort_sha256"],
        "admission_receipt_sha256": admission["receipt_sha256"],
        "refresh_request_sha256": request["request_sha256"],
        "refresh_bound_event_id": bound[0]["event_id"] if bound else None,
        **ZERO_COUNTS,
    }


def recover_campaign_admission(
    repo_root: str | Path,
    *,
    batch_id: str,
    capture_id: str,
    runtime_root: str | Path = DEFAULT_RUNTIME_ROOT,
    campaign_id: str = DEFAULT_CAMPAIGN_ID,
    campaign_policy_version: str = DEFAULT_CAMPAIGN_POLICY,
    handoff_receipt: str | Path | None = None,
    handoff_receipt_sha256: str | None = None,
) -> dict[str, Any]:
    """Fill successor layers only; never replay formal or review backflow."""

    status = admission_status(
        repo_root,
        batch_id=batch_id,
        capture_id=capture_id,
        runtime_root=runtime_root,
        campaign_id=campaign_id,
        campaign_policy_version=campaign_policy_version,
    )
    if status["status"] in {STATUS_NO_FORMAL_RESULT, STATUS_REVIEW_PENDING, STATUS_COMPLETE}:
        return {**status, "recovery_action": "none", "formal_apply_count": 0}
    if status["status"] == STATUS_ADMISSION_PENDING:
        ensured = ensure_campaign_admission(
            repo_root,
            batch_id=batch_id,
            capture_id=capture_id,
            runtime_root=runtime_root,
            campaign_id=campaign_id,
            campaign_policy_version=campaign_policy_version,
        )
        admission = bind_campaign_admission(
            repo_root,
            ensure_receipt_sha256=ensured["receipt_sha256"],
            runtime_root=runtime_root,
        )
        refreshed = refresh_campaign_projection(
            repo_root,
            admission_receipt_sha256=admission["receipt_sha256"],
            handoff_receipt=handoff_receipt,
            handoff_receipt_sha256=handoff_receipt_sha256,
        )
        return {**refreshed, "recovery_action": "admission_then_forecast_handoff"}
    refreshed = refresh_campaign_projection(
        repo_root,
        admission_receipt_sha256=status["admission_receipt_sha256"],
        handoff_receipt=handoff_receipt,
        handoff_receipt_sha256=handoff_receipt_sha256,
    )
    return {**refreshed, "recovery_action": "forecast_handoff_only"}


def _created_date(row: dict[str, Any]) -> dt.date:
    try:
        parsed = dt.datetime.fromisoformat(
            str(row.get("created_at") or "").replace("Z", "+00:00")
        )
    except ValueError:
        return EFFECTIVE_DATE
    return parsed.date()


def _canonical_formal_success_rows(
    attempts: list[dict[str, Any]], state: dict[str, Any]
) -> list[dict[str, Any]]:
    """Choose one current success result per Capture, not every retry attempt."""

    rows: list[dict[str, Any]] = []
    captures = state.get("captures", {})
    if not isinstance(captures, dict):
        raise AdmissionError("intake replay lacks canonical captures")
    for capture_id, capture in sorted(captures.items()):
        if not isinstance(capture, dict) or capture.get("quality_status") not in {
            "curated",
            "already_current",
        }:
            continue
        current_batch = str(capture.get("current_batch_id") or "")
        matches = [
            row
            for row in attempts
            if row.get("capture_id") == capture_id
            and row.get("batch_id") == current_batch
            and row.get("outcome") in {"curated", "already_current"}
        ]
        if len(matches) != 1:
            raise AdmissionError(
                f"canonical success Capture {capture_id} does not resolve one current result"
            )
        rows.append(matches[0])
    return rows


def _verified_binding_rows(
    repo: Path,
    canonical_results: list[dict[str, Any]],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Verify canonical formal bindings against their result and ensure event.

    Some pre-contract results were immutable before review binding was
    backfilled, so their intake result event cannot contain the later final
    receipt SHA. The append-only review event is still verifiable: it binds the
    current batch/Capture, exact formal proof digest, real RU, and one canonical
    ensure event. Newer results additionally re-open their final receipt.
    """

    review_events = _read_jsonl(
        repo
        / "wiki/study_vaults/408-full/state/review-loop/events.jsonl",
        "review ledger",
    )
    verified: list[dict[str, Any]] = []
    invalid: list[dict[str, Any]] = []
    for result in canonical_results:
        capture_id = str(result.get("capture_id") or "")
        batch_id = str(result.get("batch_id") or "")
        try:
            proof_digest = _result_proof_digest(result)
            candidates = [
                row
                for row in review_events
                if row.get("event_kind") == "formal_review_binding_v1"
                and row.get("batch_id") == batch_id
                and row.get("capture_id") == capture_id
            ]
            valid: list[dict[str, Any]] = []
            for event in candidates:
                unit = str(event.get("review_unit_id") or "")
                ensure_id = str(event.get("ensure_event_id") or "")
                ensure_matches = [
                    row
                    for row in review_events
                    if row.get("event_kind") == "review_schedule_ensure_v1"
                    and row.get("event_id") == ensure_id
                    and row.get("batch_id") == batch_id
                    and row.get("capture_id") == capture_id
                    and row.get("review_action_id") == event.get("review_action_id")
                ]
                route = event.get("route")
                if (
                    event.get("formal_node_id") != result.get("formal_id")
                    or event.get("formal_result_status") != result.get("outcome")
                    or event.get("formal_result_receipt_sha256") != proof_digest
                    or unit != _real_review_unit(repo, str(result.get("formal_id") or ""))
                    or event.get("review_unit_identity_basis")
                    != "formal_result_receipt"
                    or event.get("generated_actions") != []
                    or event.get("formal_write_authorized") is not False
                    or not isinstance(route, dict)
                    or route.get("formal_write_authorized") is not False
                    or len(ensure_matches) != 1
                ):
                    continue
                valid.append(event)
            final_sha = result.get("formal_review_binding_receipt_sha256")
            verification_basis = "canonical_review_binding_event"
            if final_sha is not None:
                final = _validate_review_binding(repo, result)
                bound_event_id = str(final.get("formal_binding_event_id") or "")
                valid = [
                    event for event in valid if event.get("event_id") == bound_event_id
                ]
                final_sha = final["receipt_sha256"]
                verification_basis = "final_receipt_plus_canonical_review_binding_event"
            if len(valid) != 1:
                raise AdmissionError(
                    "canonical result does not have one exact formal_review_binding_v1"
                )
            event = valid[0]
            event_id = str(event.get("event_id") or "")
            verified.append(
                {
                    "capture_id": capture_id,
                    "batch_id": batch_id,
                    "formal_result_event_id": result.get("event_id"),
                    "formal_review_binding_event_id": event_id,
                    "formal_review_binding_receipt_sha256": final_sha,
                    "formal_node_id": result.get("formal_id"),
                    "review_unit_id": event.get("review_unit_id"),
                    "study_date": event.get("observed_date"),
                    "result_created_at": result.get("created_at"),
                    "verification_basis": verification_basis,
                }
            )
        except (AdmissionError, KeyError) as exc:
            invalid.append(
                {
                    "capture_id": capture_id,
                    "batch_id": batch_id,
                    "formal_result_event_id": result.get("event_id"),
                    "formal_node_id": result.get("formal_id"),
                    "reason": str(exc),
                }
            )
    return verified, invalid


def _campaign_disposition_rows(events: Sequence[dict[str, Any]]) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for event in events:
        event_type = str(event.get("event_type") or "")
        disposition = str(
            event.get("campaign_disposition")
            or DISPOSITION_EVENT_TYPES.get(event_type)
            or ""
        )
        if disposition not in CAMPAIGN_DISPOSITIONS:
            continue
        node = str(event.get("formal_node_id") or "")
        if not FORMAL_NODE_RE.fullmatch(node):
            continue
        rows.append(
            {
                "formal_node_id": node,
                "disposition": disposition,
                "event_id": event.get("event_id"),
                "event_sha256": event.get("event_sha256"),
                "campaign_cohort_sha256": event.get("campaign_cohort_sha256"),
            }
        )
    return rows


def audit_history(repo_root: str | Path) -> dict[str, Any]:
    """Audit both successor boundaries with attempts de-duplicated by Capture."""

    repo = _repo(repo_root)
    success_attempts, state = _formal_results(repo)
    canonical_results = _canonical_formal_success_rows(success_attempts, state)
    verified_bindings, invalid_binding_proofs = _verified_binding_rows(
        repo, canonical_results
    )
    result_by_capture = {
        str(row["capture_id"]): row for row in canonical_results
    }
    success_capture_set = set(result_by_capture)
    binding_capture_set = {str(row["capture_id"]) for row in verified_bindings}
    missing_binding_rows: list[dict[str, Any]] = []
    for capture_id in sorted(success_capture_set - binding_capture_set):
        result = result_by_capture[capture_id]
        missing_binding_rows.append(
            {
                "capture_id": capture_id,
                "batch_id": result.get("batch_id"),
                "formal_result_event_id": result.get("event_id"),
                "formal_node_id": result.get("formal_id"),
                "outcome": result.get("outcome"),
                "created_at": result.get("created_at"),
            }
        )
    current_missing_binding = [
        row for row in missing_binding_rows if _created_date(row) >= EFFECTIVE_DATE
    ]
    historical_missing_binding = [
        row for row in missing_binding_rows if _created_date(row) < EFFECTIVE_DATE
    ]

    campaign_events = _read_events(repo)
    dispositions = _campaign_disposition_rows(campaign_events)
    binding_node_set = {str(row["formal_node_id"]) for row in verified_bindings}
    disposition_node_set = {str(row["formal_node_id"]) for row in dispositions}
    binding_dates_by_node: dict[str, list[dt.date]] = {}
    for binding in verified_bindings:
        binding_dates_by_node.setdefault(str(binding["formal_node_id"]), []).append(
            _created_date({"created_at": binding.get("result_created_at")})
        )
    missing_disposition_nodes = sorted(binding_node_set - disposition_node_set)
    current_missing_disposition = [
        node
        for node in missing_disposition_nodes
        if max(binding_dates_by_node.get(node, [EFFECTIVE_DATE])) >= EFFECTIVE_DATE
    ]
    historical_missing_disposition = [
        node for node in missing_disposition_nodes if node not in current_missing_disposition
    ]
    orphan_disposition_nodes = sorted(disposition_node_set - binding_node_set)

    payload = {
        "schema": AUDIT_SCHEMA,
        "contract_effective_date": EFFECTIVE_DATE.isoformat(),
        "formal_success_attempt_count": len(success_attempts),
        "canonical_formal_success_capture_count": len(success_capture_set),
        "canonical_formal_success_capture_ids": sorted(success_capture_set),
        "canonical_formal_success_capture_ids_sha256": _sha_value(
            sorted(success_capture_set)
        ),
        "verified_formal_review_binding_capture_count": len(binding_capture_set),
        "verified_formal_review_binding_capture_ids": sorted(binding_capture_set),
        "verified_formal_review_binding_capture_ids_sha256": _sha_value(
            sorted(binding_capture_set)
        ),
        "missing_binding_count": len(missing_binding_rows),
        "missing_binding_node_count": len(
            {str(row["formal_node_id"]) for row in missing_binding_rows}
        ),
        "current_missing_binding": current_missing_binding,
        "historical_missing_binding_residual": historical_missing_binding,
        "invalid_binding_proofs": invalid_binding_proofs,
        "verified_binding_formal_node_count": len(binding_node_set),
        "verified_binding_formal_node_ids": sorted(binding_node_set),
        "verified_binding_formal_node_ids_sha256": _sha_value(
            sorted(binding_node_set)
        ),
        "campaign_disposition_formal_node_count": len(disposition_node_set),
        "campaign_disposition_formal_node_ids": sorted(disposition_node_set),
        "campaign_disposition_formal_node_ids_sha256": _sha_value(
            sorted(disposition_node_set)
        ),
        "campaign_dispositions": dispositions,
        "current_binding_nodes_without_campaign_disposition": current_missing_disposition,
        "historical_binding_nodes_without_campaign_disposition_residual": (
            historical_missing_disposition
        ),
        "campaign_disposition_nodes_without_verified_binding": orphan_disposition_nodes,
        "current_missing_disposition_count": len(current_missing_disposition),
        "historical_missing_disposition_count": len(historical_missing_disposition),
        "orphan_disposition_count": len(orphan_disposition_nodes),
        "read_only": True,
        **ZERO_COUNTS,
    }
    payload["audit_sha256"] = _sha_value(payload)
    return payload


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo", default=".")
    commands = parser.add_subparsers(dest="command", required=True)

    def common_item(child: argparse.ArgumentParser) -> None:
        child.add_argument("--batch-id", required=True)
        child.add_argument("--capture-id", required=True)
        child.add_argument("--runtime-root", default=str(DEFAULT_RUNTIME_ROOT))
        child.add_argument("--campaign-id", default=DEFAULT_CAMPAIGN_ID)
        child.add_argument("--campaign-policy-version", default=DEFAULT_CAMPAIGN_POLICY)

    common_item(commands.add_parser("ensure"))
    bind = commands.add_parser("bind")
    bind.add_argument("--ensure-receipt-sha256", required=True)
    bind.add_argument("--runtime-root", default=str(DEFAULT_RUNTIME_ROOT))
    bind.add_argument("--idempotency-key")
    refresh = commands.add_parser("refresh")
    refresh.add_argument("--admission-receipt-sha256", required=True)
    refresh.add_argument("--handoff-receipt")
    refresh.add_argument("--handoff-receipt-sha256")
    common_item(commands.add_parser("status"))
    recover = commands.add_parser("recover")
    common_item(recover)
    recover.add_argument("--handoff-receipt")
    recover.add_argument("--handoff-receipt-sha256")
    commands.add_parser("audit-history")
    return parser


def _print(value: dict[str, Any]) -> None:
    print(json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True))


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        if args.command == "ensure":
            result = ensure_campaign_admission(
                args.repo,
                batch_id=args.batch_id,
                capture_id=args.capture_id,
                runtime_root=args.runtime_root,
                campaign_id=args.campaign_id,
                campaign_policy_version=args.campaign_policy_version,
            )
        elif args.command == "bind":
            result = bind_campaign_admission(
                args.repo,
                ensure_receipt_sha256=args.ensure_receipt_sha256,
                runtime_root=args.runtime_root,
                idempotency_key=args.idempotency_key,
            )
        elif args.command == "refresh":
            result = refresh_campaign_projection(
                args.repo,
                admission_receipt_sha256=args.admission_receipt_sha256,
                handoff_receipt=args.handoff_receipt,
                handoff_receipt_sha256=args.handoff_receipt_sha256,
            )
        elif args.command == "status":
            result = admission_status(
                args.repo,
                batch_id=args.batch_id,
                capture_id=args.capture_id,
                runtime_root=args.runtime_root,
                campaign_id=args.campaign_id,
                campaign_policy_version=args.campaign_policy_version,
            )
        elif args.command == "recover":
            result = recover_campaign_admission(
                args.repo,
                batch_id=args.batch_id,
                capture_id=args.capture_id,
                runtime_root=args.runtime_root,
                campaign_id=args.campaign_id,
                campaign_policy_version=args.campaign_policy_version,
                handoff_receipt=args.handoff_receipt,
                handoff_receipt_sha256=args.handoff_receipt_sha256,
            )
        else:
            result = audit_history(args.repo)
        _print(result)
        return 0
    except AdmissionError as exc:
        _print({"status": "BLOCKED", "error": str(exc), **ZERO_COUNTS})
        return 2


if __name__ == "__main__":
    sys.exit(main())
