#!/usr/bin/env python3
"""Manage independently assigned evaluation and activation of cross routes.

Only current ``verified_existing`` and ``activation_eligible`` edges may receive
managed evaluator assignments.  Evaluators submit answer-safe evidence against a
signed, same-day assignment; the caller cannot self-assert an evaluator identity,
an execution identity, an evaluation date, or an evaluation ID.  A cumulative
multi-execution, multi-date policy is the only writer of a live cross allowlist.
"""

from __future__ import annotations

import argparse
import fcntl
import hashlib
import hmac
import json
import os
import re
import secrets
import sys
import tempfile
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from typing import Any, Iterator
from zoneinfo import ZoneInfo

from cross_subject_candidates_408 import (
    DEFAULT_PROJECTION_RELATIVE,
    ShadowCatalogError,
    _build_shadow_catalog_with_live_allowlist,
    _write_activated_projection,
    build_shadow_catalog,
    validate_projection,
)


EVALUATION_SCHEMA = "cross_shadow_evaluation_v2"
EVALUATION_SUBMISSION_SCHEMA = "cross_shadow_evaluation_submission_v1"
EVALUATOR_EVIDENCE_SCHEMA = "cross_shadow_evaluator_evidence_v1"
ASSIGNMENT_REQUEST_SCHEMA = "cross_shadow_assignment_request_v1"
ASSIGNMENT_SCHEMA = "cross_shadow_evaluator_assignment_v1"
STATUS_SCHEMA = "cross_shadow_evaluation_status_v1"
LEGACY_ACTIVATION_RECEIPT_SCHEMA = "cross_shadow_activation_receipt_v2"
ACTIVATION_RECEIPT_SCHEMA = "cross_shadow_activation_receipt_v3"
ROLLBACK_RECEIPT_SCHEMA = "cross_shadow_rollback_receipt_v1"
DEFAULT_DURABLE_RELATIVE = Path(
    "wiki/study_vaults/408-full/state/linked-practice/cross-activation"
)
ORCHESTRATOR_ACTOR_ID = "nightly-cross-evaluation-orchestrator"
MIN_PASS_EVALUATIONS = 3
MIN_DISTINCT_EVALUATORS = 2
MIN_DISTINCT_EXECUTIONS = 3
MIN_DISTINCT_DATES = 2
MIN_INITIAL_ASSIGNMENTS = 2
MAX_EVALUATORS_PER_REQUEST = 4
MAX_EDGES_PER_REQUEST = 4
DEFAULT_STATUS_LIMIT = 8
MAX_STATUS_LIMIT = 20
MAX_EVIDENCE_BYTES = 16_384
MAX_ACTIVATION_OBJECT_BYTES = 4 * 1024 * 1024
ACTIVATABLE_RELATIONS = {"R01", "R02", "R04", "R05", "R06"}
EDGE_RE = re.compile(r"^CSR408-[0-9A-F]{16}$")
ASSIGNMENT_ID_RE = re.compile(r"^CSA-[0-9a-f]{64}$")
EVALUATION_ID_RE = re.compile(r"^CSE-[0-9a-f]{64}$")
ACTOR_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{2,80}$")
EXECUTION_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{7,120}$")
DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
SHA_RE = re.compile(r"^[0-9a-f]{64}$")
ANSWER_LEAK_RE = re.compile(
    r"(?:正确答案|标准答案|错误答案|我的答案|我的选择)\s*[：:]\s*[^；。,.，]{0,80}"
    r"|(?:错选|误选|应选|故选)\s*[A-DＡ-Ｄ]"
    r"|答案(?:是|为)\s*[A-DＡ-Ｄ]",
    re.IGNORECASE,
)
PROTECTED_KEYS = {
    "question",
    "question_text",
    "stem",
    "options",
    "answer",
    "correct_answer",
    "wrong_answer",
    "solution",
    "explanation",
    "full_response",
    "user_response",
    "first_break",
    "image",
    "attachment",
}
PERSONALIZATION_MANIFEST_RELATIVE = Path(
    "wiki/study_vaults/408-full/state/personalization/manifest.json"
)
ACTIVATION_RECEIPT_POINTER = "activation-receipt.json"
ROLLBACK_RECEIPT_POINTER = "rollback-receipt.json"
ACTIVATION_RECEIPT_DIR = "activation-receipts"
ROLLBACK_RECEIPT_DIR = "rollback-receipts"
SNAPSHOT_PROJECTION_DIR = Path("activation-snapshots/projections")
SNAPSHOT_MANIFEST_DIR = Path("activation-snapshots/manifests")


class CrossActivationError(RuntimeError):
    """A stable assignment, evidence, or activation-policy failure."""


def _retired_write_surface() -> None:
    raise CrossActivationError(
        "cross_shadow_activation_writes_retired_read_only_compatibility"
    )


def _json_bytes(value: Any, *, pretty: bool = False) -> bytes:
    kwargs: dict[str, Any] = {"ensure_ascii": False, "sort_keys": True}
    if pretty:
        kwargs["indent"] = 2
    else:
        kwargs["separators"] = (",", ":")
    return (json.dumps(value, **kwargs) + "\n").encode("utf-8")


def _sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _sha256_file(path: Path) -> str:
    return _sha256_bytes(path.read_bytes())


def _current_shanghai_date() -> str:
    return datetime.now(ZoneInfo("Asia/Shanghai")).date().isoformat()


@contextmanager
def _lock(path: Path) -> Iterator[None]:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a+", encoding="utf-8") as handle:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def _atomic_write_json(path: Path, value: Any) -> str:
    data = _json_bytes(value, pretty=True)
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)
    return _sha256_bytes(data)


def _atomic_write_bytes(path: Path, data: bytes) -> str:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)
    return _sha256_bytes(data)


def _bounded_regular_bytes(path: Path, label: str) -> bytes:
    try:
        metadata = path.lstat()
    except OSError as exc:
        raise CrossActivationError(f"{label} is missing") from exc
    if (
        path.is_symlink()
        or not path.is_file()
        or metadata.st_size <= 0
        or metadata.st_size > MAX_ACTIVATION_OBJECT_BYTES
    ):
        raise CrossActivationError(f"{label} must be a bounded regular file")
    try:
        data = path.read_bytes()
    except OSError as exc:
        raise CrossActivationError(f"{label} is unreadable") from exc
    if len(data) != metadata.st_size:
        raise CrossActivationError(f"{label} changed while being read")
    return data


def _write_content_addressed_bytes(path: Path, data: bytes, label: str) -> str:
    digest = _sha256_bytes(data)
    expected = path.parent / f"{digest}.json"
    if expected != path:
        raise CrossActivationError(f"{label} path is not content addressed")
    if path.exists():
        if _bounded_regular_bytes(path, label) != data:
            raise CrossActivationError(f"{label} content-address conflict")
        return digest
    _atomic_write_bytes(path, data)
    return digest


def _repo_ref(repo: Path, path: Path, label: str) -> str:
    try:
        return path.resolve().relative_to(repo).as_posix()
    except ValueError as exc:
        raise CrossActivationError(f"{label} path escaped the repository") from exc


def _activation_dependency_hashes() -> dict[str, str]:
    paths = {
        "scripts/cross_shadow_activation_408.py": Path(__file__).resolve(),
        "scripts/cross_subject_candidates_408.py": Path(__file__).resolve().with_name(
            "cross_subject_candidates_408.py"
        ),
    }
    return {name: _sha256_file(path) for name, path in sorted(paths.items())}


def _activation_policy() -> dict[str, Any]:
    return {
        "minimum_pass_evaluations": MIN_PASS_EVALUATIONS,
        "minimum_distinct_evaluators": MIN_DISTINCT_EVALUATORS,
        "minimum_distinct_execution_identities": MIN_DISTINCT_EXECUTIONS,
        "minimum_distinct_dates": MIN_DISTINCT_DATES,
        "requires_zero_failures": True,
        "requires_signed_same_day_assignment": True,
        "requires_verified_existing": True,
        "activatable_relation_codes": sorted(ACTIVATABLE_RELATIONS),
    }


def _load_object(path: Path, label: str) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise CrossActivationError(f"{label} is unreadable") from exc
    if not isinstance(value, dict):
        raise CrossActivationError(f"{label} must be a JSON object")
    return value


def _repo_file(repo: Path, locator: object, label: str) -> tuple[Path, str]:
    raw = str(locator or "").strip()
    candidate = Path(raw).expanduser()
    if not raw or "#" in raw or ".." in candidate.parts:
        raise CrossActivationError(f"{label} locator is invalid")
    path = candidate.resolve() if candidate.is_absolute() else (repo / candidate).resolve()
    if not path.is_file() or path.is_symlink() or path.stat().st_size <= 0:
        raise CrossActivationError(f"{label} must be a durable regular file")
    try:
        normalized = path.relative_to(repo).as_posix()
    except ValueError:
        normalized = str(path)
    return path, normalized


def _durable_root(repo: Path) -> Path:
    return (repo / DEFAULT_DURABLE_RELATIVE).resolve()


def _fingerprint_root(repo: Path, source_fingerprint: str) -> Path:
    if not SHA_RE.fullmatch(source_fingerprint):
        raise CrossActivationError("cross source fingerprint is invalid")
    return _durable_root(repo) / source_fingerprint


def _identity(value: object, label: str) -> dict[str, str]:
    if not isinstance(value, dict) or set(value) != {"actor_id", "execution_id"}:
        raise CrossActivationError(f"{label} identity must be structured")
    actor_id = str(value.get("actor_id") or "")
    execution_id = str(value.get("execution_id") or "")
    if not ACTOR_RE.fullmatch(actor_id) or not EXECUTION_RE.fullmatch(execution_id):
        raise CrossActivationError(f"{label} identity is invalid")
    return {"actor_id": actor_id, "execution_id": execution_id}


def _execution_identity(identity: dict[str, str]) -> str:
    return f"{identity['actor_id']}@{identity['execution_id']}"


def _assignment_key(repo: Path, *, create: bool) -> bytes:
    durable = _durable_root(repo)
    path = durable / ".assignment-signing.key"
    if path.exists():
        if path.is_symlink() or not path.is_file():
            raise CrossActivationError("assignment signing key is unsafe")
        key = path.read_bytes()
        if len(key) != 32:
            raise CrossActivationError("assignment signing key is invalid")
        return key
    if not create:
        raise CrossActivationError("assignment signing key is missing")
    durable.mkdir(parents=True, exist_ok=True)
    key = secrets.token_bytes(32)
    try:
        descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError:
        return _assignment_key(repo, create=False)
    with os.fdopen(descriptor, "wb") as handle:
        handle.write(key)
        handle.flush()
        os.fsync(handle.fileno())
    return key


def _candidate_view(record: dict[str, Any]) -> dict[str, Any]:
    return {
        "edge_id": record["edge_id"],
        "lifecycle": record["lifecycle"],
        "activation_eligible": bool(record["activation_eligible"]),
        "left_point_id": record["left"]["id"],
        "right_point_id": record["right"]["id"],
        "relation_code": record["relation_code"],
        "direction": record["direction"],
        "confidence": record["confidence"],
        "formal_edge_count": int(record.get("formal_edge_count", 0)),
        "evidence_summary": str(record.get("evidence_summary") or "")[:800],
    }


def _assignment_id(payload_without_id: dict[str, Any]) -> str:
    return "CSA-" + _sha256_bytes(_json_bytes(payload_without_id))


def _assignment_signature(key: bytes, payload_without_signature: dict[str, Any]) -> str:
    return hmac.new(key, _json_bytes(payload_without_signature), hashlib.sha256).hexdigest()


def _build_assignment(
    *,
    key: bytes,
    source_fingerprint: str,
    record: dict[str, Any],
    assigned_date: str,
    orchestrator_identity: dict[str, str],
    evaluator_identity: dict[str, str],
    request_sha256: str,
) -> dict[str, Any]:
    view = _candidate_view(record)
    base = {
        "schema": ASSIGNMENT_SCHEMA,
        "cross_source_fingerprint": source_fingerprint,
        "edge_id": record["edge_id"],
        "assigned_date": assigned_date,
        "orchestrator_identity": orchestrator_identity,
        "evaluator_identity": evaluator_identity,
        "candidate_view": view,
        "candidate_view_sha256": _sha256_bytes(_json_bytes(view)),
        "request_sha256": request_sha256,
        "answer_safe": True,
        "formal_write_count": 0,
    }
    assignment_id = _assignment_id(base)
    signed = {**base, "assignment_id": assignment_id}
    return {**signed, "assignment_signature": _assignment_signature(key, signed)}


def _validate_assignment(
    repo: Path,
    path: Path,
    payload: dict[str, Any],
    *,
    source_fingerprint: str,
    current_edges: dict[str, dict[str, Any]],
) -> dict[str, Any]:
    allowed = {
        "schema",
        "assignment_id",
        "cross_source_fingerprint",
        "edge_id",
        "assigned_date",
        "orchestrator_identity",
        "evaluator_identity",
        "candidate_view",
        "candidate_view_sha256",
        "request_sha256",
        "answer_safe",
        "formal_write_count",
        "assignment_signature",
    }
    edge_id = str(payload.get("edge_id") or "")
    assignment_id = str(payload.get("assignment_id") or "")
    if (
        set(payload) != allowed
        or payload.get("schema") != ASSIGNMENT_SCHEMA
        or not ASSIGNMENT_ID_RE.fullmatch(assignment_id)
        or path.name != f"{assignment_id}.json"
        or payload.get("cross_source_fingerprint") != source_fingerprint
        or not EDGE_RE.fullmatch(edge_id)
        or edge_id not in current_edges
        or not DATE_RE.fullmatch(str(payload.get("assigned_date") or ""))
        or payload.get("answer_safe") is not True
        or int(payload.get("formal_write_count", -1)) != 0
        or not SHA_RE.fullmatch(str(payload.get("request_sha256") or ""))
    ):
        raise CrossActivationError("managed evaluator assignment is invalid")
    orchestrator = _identity(payload.get("orchestrator_identity"), "orchestrator")
    evaluator = _identity(payload.get("evaluator_identity"), "evaluator")
    if orchestrator["actor_id"] != ORCHESTRATOR_ACTOR_ID:
        raise CrossActivationError("assignment was not issued by the nightly orchestrator")
    record = current_edges[edge_id]
    if not (
        record.get("lifecycle") == "verified_existing"
        and record.get("activation_eligible") is True
        and record.get("relation_code") in ACTIVATABLE_RELATIONS
    ):
        raise CrossActivationError("assignment edge is not activation eligible")
    expected_view = _candidate_view(record)
    if (
        payload.get("candidate_view") != expected_view
        or payload.get("candidate_view_sha256") != _sha256_bytes(_json_bytes(expected_view))
    ):
        raise CrossActivationError("assignment candidate view has drifted")
    without_signature = dict(payload)
    signature = str(without_signature.pop("assignment_signature", ""))
    without_id = dict(without_signature)
    without_id.pop("assignment_id", None)
    if assignment_id != _assignment_id(without_id):
        raise CrossActivationError("assignment content address mismatch")
    key = _assignment_key(repo, create=False)
    expected_signature = _assignment_signature(key, without_signature)
    if not SHA_RE.fullmatch(signature) or not hmac.compare_digest(signature, expected_signature):
        raise CrossActivationError("assignment signature mismatch")
    normalized = dict(payload)
    normalized["orchestrator_identity"] = orchestrator
    normalized["evaluator_identity"] = evaluator
    return normalized


def _read_assignments(
    repo: Path,
    partition: Path,
    *,
    current_edges: dict[str, dict[str, Any]],
    source_fingerprint: str,
) -> list[dict[str, Any]]:
    directory = partition / "assignments"
    if not directory.is_dir():
        return []
    assignments: list[dict[str, Any]] = []
    for path in sorted(directory.glob("CSA-*.json")):
        assignments.append(
            _validate_assignment(
                repo,
                path,
                _load_object(path, "managed evaluator assignment"),
                source_fingerprint=source_fingerprint,
                current_edges=current_edges,
            )
        )
    return assignments


def _assert_answer_safe(value: Any, *, key: str | None = None) -> None:
    if key in PROTECTED_KEYS:
        raise CrossActivationError("evaluator evidence contains a protected field")
    if isinstance(value, dict):
        for child_key, child in value.items():
            _assert_answer_safe(child, key=str(child_key))
    elif isinstance(value, list):
        for child in value:
            _assert_answer_safe(child)
    elif isinstance(value, str) and ANSWER_LEAK_RE.search(value):
        raise CrossActivationError("evaluator evidence contains answer-bearing text")


def _validate_evaluator_evidence(
    path: Path,
    payload: dict[str, Any],
    *,
    assignment: dict[str, Any],
) -> dict[str, Any]:
    if path.stat().st_size > MAX_EVIDENCE_BYTES:
        raise CrossActivationError("evaluator evidence exceeds the answer-safe bound")
    allowed = {
        "schema",
        "assignment_id",
        "edge_id",
        "evaluator_identity",
        "verdict",
        "route_semantics_verified",
        "direction_verified",
        "answer_safe_candidate_verified",
        "no_answer_leak_verified",
        "reasoning_summary",
        "counterexample_summary",
        "answer_safe",
        "formal_write_count",
    }
    verdict = str(payload.get("verdict") or "")
    if (
        set(payload) != allowed
        or payload.get("schema") != EVALUATOR_EVIDENCE_SCHEMA
        or payload.get("assignment_id") != assignment["assignment_id"]
        or payload.get("edge_id") != assignment["edge_id"]
        or _identity(payload.get("evaluator_identity"), "evaluator")
        != assignment["evaluator_identity"]
        or verdict not in {"pass", "fail"}
        or payload.get("answer_safe") is not True
        or int(payload.get("formal_write_count", -1)) != 0
    ):
        raise CrossActivationError("evaluator evidence identity is invalid")
    for field in ("reasoning_summary", "counterexample_summary"):
        text = str(payload.get(field) or "").strip()
        if not text or len(text) > 1200:
            raise CrossActivationError("evaluator evidence summary is invalid")
    checks = (
        "route_semantics_verified",
        "direction_verified",
        "answer_safe_candidate_verified",
        "no_answer_leak_verified",
    )
    if any(not isinstance(payload.get(field), bool) for field in checks):
        raise CrossActivationError("evaluator evidence checks must be boolean")
    if verdict == "pass" and not all(payload[field] for field in checks):
        raise CrossActivationError("passing evaluation lacks required checks")
    _assert_answer_safe(payload)
    return dict(payload)


def _evaluation_id(payload_without_id: dict[str, Any]) -> str:
    return "CSE-" + _sha256_bytes(_json_bytes(payload_without_id))


def _validate_evaluation(
    repo: Path,
    event: dict[str, Any],
    *,
    current_edges: dict[str, dict[str, Any]],
    source_fingerprint: str,
) -> dict[str, Any]:
    allowed = {
        "schema",
        "evaluation_id",
        "assignment_id",
        "assignment_ref",
        "assignment_sha256",
        "edge_id",
        "cross_source_fingerprint",
        "evaluation_mode",
        "verdict",
        "orchestrator_identity",
        "evaluator_identity",
        "evaluated_date",
        "evidence_ref",
        "evidence_sha256",
        "route_semantics_verified",
        "direction_verified",
        "answer_safe_candidate_verified",
        "no_answer_leak_verified",
        "answer_safe",
        "formal_write_count",
    }
    edge_id = str(event.get("edge_id") or "")
    evaluation_id = str(event.get("evaluation_id") or "")
    if (
        set(event) != allowed
        or event.get("schema") != EVALUATION_SCHEMA
        or not EVALUATION_ID_RE.fullmatch(evaluation_id)
        or not EDGE_RE.fullmatch(edge_id)
        or edge_id not in current_edges
        or event.get("cross_source_fingerprint") != source_fingerprint
        or event.get("evaluation_mode") != "shadow"
        or event.get("verdict") not in {"pass", "fail"}
        or not DATE_RE.fullmatch(str(event.get("evaluated_date") or ""))
        or event.get("answer_safe") is not True
        or int(event.get("formal_write_count", -1)) != 0
    ):
        raise CrossActivationError("cross evaluation event is invalid")
    partition = _fingerprint_root(repo, source_fingerprint)
    assignment_path, assignment_ref = _repo_file(
        repo, event.get("assignment_ref"), "managed evaluator assignment"
    )
    expected_assignment_dir = (partition / "assignments").resolve()
    if assignment_path.parent != expected_assignment_dir:
        raise CrossActivationError("evaluation did not consume a managed assignment")
    assignment_hash = str(event.get("assignment_sha256") or "")
    if not SHA_RE.fullmatch(assignment_hash) or _sha256_file(assignment_path) != assignment_hash:
        raise CrossActivationError("managed assignment hash mismatch")
    assignment = _validate_assignment(
        repo,
        assignment_path,
        _load_object(assignment_path, "managed evaluator assignment"),
        source_fingerprint=source_fingerprint,
        current_edges=current_edges,
    )
    evidence_path, evidence_ref = _repo_file(
        repo, event.get("evidence_ref"), "evaluator evidence"
    )
    evidence_hash = str(event.get("evidence_sha256") or "")
    if not SHA_RE.fullmatch(evidence_hash) or _sha256_file(evidence_path) != evidence_hash:
        raise CrossActivationError("evaluator evidence hash mismatch")
    evidence = _validate_evaluator_evidence(
        evidence_path,
        _load_object(evidence_path, "evaluator evidence"),
        assignment=assignment,
    )
    if not all(
        (
            event.get("assignment_id") == assignment["assignment_id"],
            edge_id == assignment["edge_id"],
            event.get("orchestrator_identity") == assignment["orchestrator_identity"],
            event.get("evaluator_identity") == assignment["evaluator_identity"],
            event.get("evaluated_date") == assignment["assigned_date"],
            event.get("verdict") == evidence["verdict"],
            event.get("route_semantics_verified") == evidence["route_semantics_verified"],
            event.get("direction_verified") == evidence["direction_verified"],
            event.get("answer_safe_candidate_verified")
            == evidence["answer_safe_candidate_verified"],
            event.get("no_answer_leak_verified") == evidence["no_answer_leak_verified"],
        )
    ):
        raise CrossActivationError("evaluation does not match assignment and evidence")
    normalized = dict(event)
    normalized["assignment_ref"] = assignment_ref
    normalized["evidence_ref"] = evidence_ref
    without_id = dict(normalized)
    without_id.pop("evaluation_id", None)
    if evaluation_id != _evaluation_id(without_id):
        raise CrossActivationError("evaluation content address mismatch")
    return normalized


def _read_ledger(
    repo: Path,
    ledger: Path,
    *,
    current_edges: dict[str, dict[str, Any]],
    source_fingerprint: str,
) -> list[dict[str, Any]]:
    if not ledger.is_file():
        return []
    events: list[dict[str, Any]] = []
    seen_ids: set[str] = set()
    seen_assignments: set[str] = set()
    for line_number, raw in enumerate(ledger.read_text(encoding="utf-8").splitlines(), 1):
        if not raw.strip():
            continue
        try:
            value = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise CrossActivationError(
                f"cross evaluation ledger line {line_number} is invalid"
            ) from exc
        if not isinstance(value, dict):
            raise CrossActivationError("cross evaluation ledger event must be an object")
        event = _validate_evaluation(
            repo,
            value,
            current_edges=current_edges,
            source_fingerprint=source_fingerprint,
        )
        evaluation_id = str(event["evaluation_id"])
        assignment_id = str(event["assignment_id"])
        if evaluation_id in seen_ids or assignment_id in seen_assignments:
            raise CrossActivationError("duplicate evaluation or consumed assignment")
        seen_ids.add(evaluation_id)
        seen_assignments.add(assignment_id)
        events.append(event)
    return events


def _policy_decisions(
    base: dict[str, Any],
    events: list[dict[str, Any]],
) -> tuple[list[str], list[dict[str, Any]]]:
    by_edge: dict[str, list[dict[str, Any]]] = {}
    for event in events:
        by_edge.setdefault(str(event["edge_id"]), []).append(event)
    enabled: list[str] = []
    decisions: list[dict[str, Any]] = []
    for record in base["records"]:
        edge_id = str(record["edge_id"])
        edge_events = by_edge.get(edge_id, [])
        passes = [event for event in edge_events if event["verdict"] == "pass"]
        failures = [event for event in edge_events if event["verdict"] == "fail"]
        actors = {str(event["evaluator_identity"]["actor_id"]) for event in passes}
        executions = {
            _execution_identity(event["evaluator_identity"]) for event in passes
        }
        dates = {str(event["evaluated_date"]) for event in passes}
        source_hashes = {str(event["evidence_sha256"]) for event in passes}
        eligible = bool(
            record.get("lifecycle") == "verified_existing"
            and record.get("activation_eligible") is True
            and record.get("relation_code") in ACTIVATABLE_RELATIONS
            and record.get("confidence") in {"high", "medium"}
            and int(record.get("formal_edge_count", 0)) >= 1
            and len(passes) >= MIN_PASS_EVALUATIONS
            and len(actors) >= MIN_DISTINCT_EVALUATORS
            and len(executions) >= MIN_DISTINCT_EXECUTIONS
            and len(dates) >= MIN_DISTINCT_DATES
            and len(source_hashes) >= MIN_PASS_EVALUATIONS
            and not failures
        )
        if eligible:
            enabled.append(edge_id)
        decisions.append(
            {
                "edge_id": edge_id,
                "lifecycle": record.get("lifecycle"),
                "activation_eligible": bool(record.get("activation_eligible")),
                "relation_code": record.get("relation_code"),
                "pass_count": len(passes),
                "fail_count": len(failures),
                "distinct_evaluators": len(actors),
                "distinct_execution_identities": len(executions),
                "distinct_dates": len(dates),
                "distinct_evidence": len(source_hashes),
                "evaluated_dates": sorted(dates),
                "policy_enabled": eligible,
                "evaluation_ids": sorted(str(item["evaluation_id"]) for item in edge_events),
                "consumed_assignment_ids": sorted(
                    str(item["assignment_id"]) for item in edge_events
                ),
            }
        )
    return sorted(enabled), decisions


def _status_rows(
    base: dict[str, Any],
    events: list[dict[str, Any]],
    assignments: list[dict[str, Any]],
    *,
    today: str,
) -> list[dict[str, Any]]:
    _, decisions = _policy_decisions(base, events)
    decisions_by_edge = {str(item["edge_id"]): item for item in decisions}
    consumed = {str(event["assignment_id"]) for event in events}
    rows: list[dict[str, Any]] = []
    for record in base["records"]:
        if not (
            record.get("lifecycle") == "verified_existing"
            and record.get("activation_eligible") is True
            and record.get("relation_code") in ACTIVATABLE_RELATIONS
        ):
            continue
        edge_id = str(record["edge_id"])
        decision = decisions_by_edge[edge_id]
        current_assignments = [
            item
            for item in assignments
            if item["edge_id"] == edge_id and item["assigned_date"] == today
        ]
        outstanding = [
            item for item in current_assignments if item["assignment_id"] not in consumed
        ]
        evaluated_dates = set(decision["evaluated_dates"])
        if decision["fail_count"]:
            action = "blocked_by_failure"
            assignments_needed = 0
        elif decision["policy_enabled"]:
            action = "activate_ready"
            assignments_needed = 0
        elif outstanding:
            action = "evaluate_assigned"
            assignments_needed = 0
        elif evaluated_dates and today in evaluated_dates and len(evaluated_dates) < 2:
            action = "wait_next_local_date"
            assignments_needed = 0
        else:
            action = "issue_assignments"
            assignments_needed = MIN_INITIAL_ASSIGNMENTS
        rows.append(
            {
                **decision,
                "left_point_id": record["left"]["id"],
                "right_point_id": record["right"]["id"],
                "direction": record["direction"],
                "confidence": record["confidence"],
                "current_date_assignment_count": len(current_assignments),
                "outstanding_assignment_ids": sorted(
                    str(item["assignment_id"]) for item in outstanding
                ),
                "action": action,
                "assignments_needed": assignments_needed,
            }
        )
    return sorted(rows, key=lambda item: (item["action"], item["edge_id"]))


def evaluation_status(repo: str | Path, *, limit: int = DEFAULT_STATUS_LIMIT) -> dict[str, Any]:
    if not 1 <= int(limit) <= MAX_STATUS_LIMIT:
        raise CrossActivationError("status limit is out of range")
    root = Path(repo).expanduser().resolve()
    base = build_shadow_catalog(root)
    edges = {str(row["edge_id"]): row for row in base["records"]}
    partition = _fingerprint_root(root, base["source_fingerprint"])
    ledger = partition / "evaluations.jsonl"
    events = _read_ledger(
        root,
        ledger,
        current_edges=edges,
        source_fingerprint=base["source_fingerprint"],
    )
    assignments = _read_assignments(
        root,
        partition,
        current_edges=edges,
        source_fingerprint=base["source_fingerprint"],
    )
    today = _current_shanghai_date()
    rows = _status_rows(base, events, assignments, today=today)
    pending = [
        row
        for row in rows
        if row["action"] not in {"activate_ready", "blocked_by_failure"}
    ]
    return {
        "schema": STATUS_SCHEMA,
        "status": "ok",
        "source_fingerprint": base["source_fingerprint"],
        "evaluated_date": today,
        "eligible_edge_count": len(rows),
        "pending_edge_count": len(pending),
        "activate_ready_count": sum(row["action"] == "activate_ready" for row in rows),
        "blocked_edge_count": sum(row["action"] == "blocked_by_failure" for row in rows),
        "returned_count": min(len(rows), int(limit)),
        "truncated": len(rows) > int(limit),
        "records": rows[: int(limit)],
        "formal_write_count": 0,
    }


def issue_assignments(repo: str | Path, request_path: str | Path) -> dict[str, Any]:
    _retired_write_surface()
    root = Path(repo).expanduser().resolve()
    base = build_shadow_catalog(root)
    edges = {str(row["edge_id"]): row for row in base["records"]}
    input_path, _ = _repo_file(root, request_path, "assignment request")
    request = _load_object(input_path, "assignment request")
    if set(request) != {
        "schema",
        "cross_source_fingerprint",
        "orchestrator_identity",
        "edge_ids",
        "evaluator_identities",
        "formal_write_count",
    }:
        raise CrossActivationError("assignment request fields are invalid")
    orchestrator = _identity(request.get("orchestrator_identity"), "orchestrator")
    if (
        request.get("schema") != ASSIGNMENT_REQUEST_SCHEMA
        or request.get("cross_source_fingerprint") != base["source_fingerprint"]
        or orchestrator["actor_id"] != ORCHESTRATOR_ACTOR_ID
        or int(request.get("formal_write_count", -1)) != 0
    ):
        raise CrossActivationError("assignment request identity is invalid")
    raw_edge_ids = request.get("edge_ids")
    raw_evaluators = request.get("evaluator_identities")
    if (
        not isinstance(raw_edge_ids, list)
        or not 1 <= len(raw_edge_ids) <= MAX_EDGES_PER_REQUEST
        or len(raw_edge_ids) != len(set(map(str, raw_edge_ids)))
        or not isinstance(raw_evaluators, list)
        or not MIN_INITIAL_ASSIGNMENTS
        <= len(raw_evaluators)
        <= MAX_EVALUATORS_PER_REQUEST
    ):
        raise CrossActivationError("assignment request is not bounded")
    edge_ids = [str(value) for value in raw_edge_ids]
    evaluators = [_identity(value, "evaluator") for value in raw_evaluators]
    actor_ids = {item["actor_id"] for item in evaluators}
    execution_ids = {item["execution_id"] for item in evaluators}
    if len(actor_ids) != len(evaluators) or len(execution_ids) != len(evaluators):
        raise CrossActivationError("evaluator assignments must be independently executed")
    for edge_id in edge_ids:
        record = edges.get(edge_id)
        if not record or not (
            record.get("lifecycle") == "verified_existing"
            and record.get("activation_eligible") is True
            and record.get("relation_code") in ACTIVATABLE_RELATIONS
        ):
            raise CrossActivationError("assignment request includes an ineligible edge")
    request_sha = _sha256_file(input_path)
    today = _current_shanghai_date()
    durable = _durable_root(root)
    partition = _fingerprint_root(root, base["source_fingerprint"])
    ledger = partition / "evaluations.jsonl"
    with _lock(durable / ".cross-activation.lock"):
        key = _assignment_key(root, create=True)
        events = _read_ledger(
            root,
            ledger,
            current_edges=edges,
            source_fingerprint=base["source_fingerprint"],
        )
        existing = _read_assignments(
            root,
            partition,
            current_edges=edges,
            source_fingerprint=base["source_fingerprint"],
        )
        rows = {
            row["edge_id"]: row
            for row in _status_rows(base, events, existing, today=today)
        }
        built: list[dict[str, Any]] = []
        for edge_id in edge_ids:
            for evaluator in evaluators[:MIN_INITIAL_ASSIGNMENTS]:
                built.append(
                    _build_assignment(
                        key=key,
                        source_fingerprint=base["source_fingerprint"],
                        record=edges[edge_id],
                        assigned_date=today,
                        orchestrator_identity=orchestrator,
                        evaluator_identity=evaluator,
                        request_sha256=request_sha,
                    )
                )
        assignment_dir = partition / "assignments"
        existing_by_id = {str(item["assignment_id"]): item for item in existing}
        built_ids_by_edge = {
            edge_id: {
                str(item["assignment_id"])
                for item in built
                if item["edge_id"] == edge_id
            }
            for edge_id in edge_ids
        }
        for edge_id in edge_ids:
            current_ids = {
                str(item["assignment_id"])
                for item in existing
                if item["edge_id"] == edge_id and item["assigned_date"] == today
            }
            if current_ids and not current_ids.issubset(built_ids_by_edge[edge_id]):
                raise CrossActivationError(
                    f"edge {edge_id} already has a different current-day assignment set"
                )
            if not current_ids and rows[edge_id]["action"] != "issue_assignments":
                raise CrossActivationError(
                    f"edge {edge_id} is not awaiting new evaluator assignments"
                )
        created = 0
        for assignment in built:
            path = assignment_dir / f"{assignment['assignment_id']}.json"
            previous = existing_by_id.get(str(assignment["assignment_id"]))
            if previous is not None and previous != assignment:
                raise CrossActivationError("assignment content-address conflict")
            if previous is None:
                _atomic_write_json(path, assignment)
                created += 1
    return {
        "status": "already_issued" if created == 0 else "issued",
        "source_fingerprint": base["source_fingerprint"],
        "assigned_date": today,
        "assignment_count": len(built),
        "assignments": [
            {
                "assignment_id": item["assignment_id"],
                "edge_id": item["edge_id"],
                "evaluator_identity": item["evaluator_identity"],
                "assignment_ref": (
                    partition
                    / "assignments"
                    / f"{item['assignment_id']}.json"
                ).relative_to(root).as_posix(),
                "assignment_sha256": _sha256_file(
                    partition
                    / "assignments"
                    / f"{item['assignment_id']}.json"
                ),
            }
            for item in built
        ],
        "formal_write_count": 0,
    }


def record_evaluation(repo: str | Path, submission_path: str | Path) -> dict[str, Any]:
    _retired_write_surface()
    root = Path(repo).expanduser().resolve()
    base = build_shadow_catalog(root)
    edges = {str(row["edge_id"]): row for row in base["records"]}
    input_path, _ = _repo_file(root, submission_path, "evaluation submission")
    submission = _load_object(input_path, "evaluation submission")
    if set(submission) != {
        "schema",
        "assignment_ref",
        "assignment_sha256",
        "evidence_ref",
        "evidence_sha256",
        "formal_write_count",
    } or (
        submission.get("schema") != EVALUATION_SUBMISSION_SCHEMA
        or int(submission.get("formal_write_count", -1)) != 0
    ):
        raise CrossActivationError("evaluation submission fields are invalid")
    partition = _fingerprint_root(root, base["source_fingerprint"])
    assignment_path, assignment_ref = _repo_file(
        root, submission.get("assignment_ref"), "managed evaluator assignment"
    )
    if assignment_path.parent != (partition / "assignments").resolve():
        raise CrossActivationError("evaluation did not consume a managed assignment")
    assignment_sha = str(submission.get("assignment_sha256") or "")
    if not SHA_RE.fullmatch(assignment_sha) or _sha256_file(assignment_path) != assignment_sha:
        raise CrossActivationError("managed assignment hash mismatch")
    assignment = _validate_assignment(
        root,
        assignment_path,
        _load_object(assignment_path, "managed evaluator assignment"),
        source_fingerprint=base["source_fingerprint"],
        current_edges=edges,
    )
    today = _current_shanghai_date()
    if assignment["assigned_date"] != today:
        raise CrossActivationError("evaluation assignment is not for the current Asia/Shanghai date")
    evidence_path, evidence_ref = _repo_file(
        root, submission.get("evidence_ref"), "evaluator evidence"
    )
    evidence_sha = str(submission.get("evidence_sha256") or "")
    if not SHA_RE.fullmatch(evidence_sha) or _sha256_file(evidence_path) != evidence_sha:
        raise CrossActivationError("evaluator evidence hash mismatch")
    evidence = _validate_evaluator_evidence(
        evidence_path,
        _load_object(evidence_path, "evaluator evidence"),
        assignment=assignment,
    )
    event_without_id = {
        "schema": EVALUATION_SCHEMA,
        "assignment_id": assignment["assignment_id"],
        "assignment_ref": assignment_ref,
        "assignment_sha256": assignment_sha,
        "edge_id": assignment["edge_id"],
        "cross_source_fingerprint": base["source_fingerprint"],
        "evaluation_mode": "shadow",
        "verdict": evidence["verdict"],
        "orchestrator_identity": assignment["orchestrator_identity"],
        "evaluator_identity": assignment["evaluator_identity"],
        "evaluated_date": today,
        "evidence_ref": evidence_ref,
        "evidence_sha256": evidence_sha,
        "route_semantics_verified": evidence["route_semantics_verified"],
        "direction_verified": evidence["direction_verified"],
        "answer_safe_candidate_verified": evidence["answer_safe_candidate_verified"],
        "no_answer_leak_verified": evidence["no_answer_leak_verified"],
        "answer_safe": True,
        "formal_write_count": 0,
    }
    event = {
        **event_without_id,
        "evaluation_id": _evaluation_id(event_without_id),
    }
    event = _validate_evaluation(
        root,
        event,
        current_edges=edges,
        source_fingerprint=base["source_fingerprint"],
    )
    durable = _durable_root(root)
    ledger = partition / "evaluations.jsonl"
    with _lock(durable / ".cross-activation.lock"):
        existing = _read_ledger(
            root,
            ledger,
            current_edges=edges,
            source_fingerprint=base["source_fingerprint"],
        )
        assignment_events = [
            item for item in existing if item["assignment_id"] == event["assignment_id"]
        ]
        if assignment_events:
            if assignment_events[0] != event:
                raise CrossActivationError("managed assignment was already consumed")
            status = "already_recorded"
        else:
            ledger.parent.mkdir(parents=True, exist_ok=True)
            with ledger.open("ab") as handle:
                handle.write(_json_bytes(event))
                handle.flush()
                os.fsync(handle.fileno())
            status = "recorded"
    return {
        "status": status,
        "evaluation_id": event["evaluation_id"],
        "assignment_id": event["assignment_id"],
        "edge_id": event["edge_id"],
        "evaluated_date": today,
        "ledger_sha256": _sha256_file(ledger),
        "formal_write_count": 0,
    }


def _json_from_bytes(data: bytes, label: str) -> dict[str, Any]:
    try:
        value = json.loads(data.decode("utf-8"))
    except (UnicodeError, json.JSONDecodeError) as exc:
        raise CrossActivationError(f"{label} is invalid JSON") from exc
    if not isinstance(value, dict):
        raise CrossActivationError(f"{label} must be a JSON object")
    return value


def _validate_root_manifest_binding(
    manifest: dict[str, Any], *, projection_sha256: str
) -> None:
    if (
        manifest.get("schema") != "personalization_index_v1"
        or manifest.get("kind") != "personalization_manifest"
        or manifest.get("answer_safe") is not True
        or int(manifest.get("formal_write_count", -1)) != 0
        or manifest.get("cross_subject_shadow_sha256") != projection_sha256
    ):
        raise CrossActivationError(
            "personalization root manifest does not bind the current cross projection"
        )


def _validate_post_rebuild_manifest(
    manifest: dict[str, Any], *, activated_projection_sha256: str
) -> None:
    cross_binding = (manifest.get("independent_projections") or {}).get(
        "cross_subject_shadow"
    )
    if (
        manifest.get("schema") != "personalization_index_v1"
        or manifest.get("kind") != "personalization_manifest"
        or manifest.get("answer_safe") is not True
        or int(manifest.get("formal_write_count", -1)) != 0
        or not DATE_RE.fullmatch(str(manifest.get("as_of") or ""))
        or not isinstance(manifest.get("artifact_hashes"), dict)
        or manifest.get("cross_subject_shadow_sha256")
        != activated_projection_sha256
        or not isinstance(cross_binding, dict)
        or cross_binding.get("file") != DEFAULT_PROJECTION_RELATIVE.name
        or cross_binding.get("included_in_this_build") is not True
    ):
        raise CrossActivationError(
            "post-rebuild root manifest is not a legal activated binding"
        )


def _rollback_manifest_state(
    manifest_raw: bytes,
    *,
    previous_manifest_sha256: str,
    activated_projection_sha256: str,
) -> tuple[str, str]:
    manifest_sha = _sha256_bytes(manifest_raw)
    manifest = _json_from_bytes(manifest_raw, "rollback-before root manifest")
    if manifest_sha == previous_manifest_sha256:
        return "pre_rebuild", manifest_sha
    _validate_post_rebuild_manifest(
        manifest,
        activated_projection_sha256=activated_projection_sha256,
    )
    return "post_rebuild", manifest_sha


def _snapshot_activation_prestate(
    root: Path,
    partition: Path,
    *,
    source_fingerprint: str,
) -> dict[str, Any]:
    projection_path = (root / DEFAULT_PROJECTION_RELATIVE).resolve()
    manifest_path = (root / PERSONALIZATION_MANIFEST_RELATIVE).resolve()
    projection_raw = _bounded_regular_bytes(
        projection_path, "previous cross projection"
    )
    manifest_raw = _bounded_regular_bytes(
        manifest_path, "previous personalization root manifest"
    )
    projection_sha = _sha256_bytes(projection_raw)
    manifest_sha = _sha256_bytes(manifest_raw)
    projection = _json_from_bytes(projection_raw, "previous cross projection")
    manifest = _json_from_bytes(
        manifest_raw, "previous personalization root manifest"
    )
    try:
        validate_projection(root, projection)
    except ShadowCatalogError as exc:
        raise CrossActivationError(
            "previous cross projection is not a current complete rebuild"
        ) from exc
    if projection.get("source_fingerprint") != source_fingerprint:
        raise CrossActivationError("previous cross projection source fingerprint drifted")
    _validate_root_manifest_binding(manifest, projection_sha256=projection_sha)

    projection_snapshot = (
        partition / SNAPSHOT_PROJECTION_DIR / f"{projection_sha}.json"
    )
    manifest_snapshot = partition / SNAPSHOT_MANIFEST_DIR / f"{manifest_sha}.json"
    _write_content_addressed_bytes(
        projection_snapshot, projection_raw, "previous cross projection snapshot"
    )
    _write_content_addressed_bytes(
        manifest_snapshot, manifest_raw, "previous root manifest snapshot"
    )
    return {
        "projection": {
            "ref": _repo_ref(root, projection_snapshot, "projection snapshot"),
            "sha256": projection_sha,
        },
        "root_manifest": {
            "ref": _repo_ref(root, manifest_snapshot, "manifest snapshot"),
            "sha256": manifest_sha,
        },
        "projection_raw": projection_raw,
        "manifest_raw": manifest_raw,
    }


def _write_receipt_entity(
    partition: Path,
    *,
    directory: str,
    pointer_name: str,
    receipt: dict[str, Any],
    label: str,
) -> str:
    raw = _json_bytes(receipt, pretty=True)
    digest = _sha256_bytes(raw)
    history_path = partition / directory / f"{digest}.json"
    _write_content_addressed_bytes(history_path, raw, label)
    _atomic_write_bytes(partition / pointer_name, raw)
    return digest


def _load_receipt_entity(
    partition: Path,
    *,
    directory: str,
    pointer_name: str,
    expected_sha256: str,
    label: str,
) -> tuple[dict[str, Any], bytes]:
    if not SHA_RE.fullmatch(expected_sha256):
        raise CrossActivationError(f"{label} SHA-256 is invalid")
    pointer = partition / pointer_name
    raw = _bounded_regular_bytes(pointer, f"latest {label}")
    if _sha256_bytes(raw) != expected_sha256:
        raise CrossActivationError(f"{label} is not the most recent receipt")
    history = partition / directory / f"{expected_sha256}.json"
    if _bounded_regular_bytes(history, f"content-addressed {label}") != raw:
        raise CrossActivationError(f"{label} pointer/history mismatch")
    return _json_from_bytes(raw, label), raw


def _receipt_snapshot(
    root: Path,
    partition: Path,
    value: object,
    *,
    directory: Path,
    label: str,
) -> tuple[Path, bytes, str]:
    if not isinstance(value, dict) or set(value) != {"ref", "sha256"}:
        raise CrossActivationError(f"{label} commitment is invalid")
    digest = str(value.get("sha256") or "")
    if not SHA_RE.fullmatch(digest):
        raise CrossActivationError(f"{label} SHA-256 is invalid")
    path, normalized = _repo_file(root, value.get("ref"), label)
    expected = (partition / directory / f"{digest}.json").resolve()
    if path != expected or normalized != _repo_ref(root, expected, label):
        raise CrossActivationError(f"{label} escaped its managed snapshot directory")
    raw = _bounded_regular_bytes(path, label)
    if _sha256_bytes(raw) != digest:
        raise CrossActivationError(f"{label} hash mismatch")
    return path, raw, digest


def validate_activation_receipt(
    repo: str | Path,
    receipt_sha256: str,
    *,
    require_activated_projection: bool,
) -> dict[str, Any]:
    root = Path(repo).expanduser().resolve()
    base = build_shadow_catalog(root)
    source_fingerprint = str(base["source_fingerprint"])
    partition = _fingerprint_root(root, source_fingerprint)
    receipt, _ = _load_receipt_entity(
        partition,
        directory=ACTIVATION_RECEIPT_DIR,
        pointer_name=ACTIVATION_RECEIPT_POINTER,
        expected_sha256=receipt_sha256,
        label="activation receipt",
    )
    allowed = {
        "schema",
        "source_fingerprint",
        "source_manifest",
        "dependency_hashes",
        "evaluation_ledger_sha256",
        "policy",
        "enabled_edge_ids",
        "decisions",
        "previous_projection",
        "previous_root_manifest",
        "activated_projection",
        "rollback_boundary",
        "personalization_rebuild_required",
        "formal_write_count",
    }
    if (
        set(receipt) != allowed
        or receipt.get("schema") != ACTIVATION_RECEIPT_SCHEMA
        or receipt.get("source_fingerprint") != source_fingerprint
        or receipt.get("source_manifest") != base.get("source_manifest")
        or receipt.get("dependency_hashes") != _activation_dependency_hashes()
        or receipt.get("policy") != _activation_policy()
        or receipt.get("rollback_boundary")
        != "pre_or_post_personalization_rebuild"
        or receipt.get("personalization_rebuild_required") is not True
        or int(receipt.get("formal_write_count", -1)) != 0
    ):
        raise CrossActivationError("activation receipt dependencies drifted")

    edges = {str(row["edge_id"]): row for row in base["records"]}
    ledger = partition / "evaluations.jsonl"
    events = _read_ledger(
        root,
        ledger,
        current_edges=edges,
        source_fingerprint=source_fingerprint,
    )
    ledger_sha = _sha256_file(ledger) if ledger.is_file() else None
    enabled, decisions = _policy_decisions(base, events)
    if (
        receipt.get("evaluation_ledger_sha256") != ledger_sha
        or receipt.get("enabled_edge_ids") != enabled
        or receipt.get("decisions") != decisions
    ):
        raise CrossActivationError("activation receipt evaluation state drifted")

    _, previous_projection_raw, previous_projection_sha = _receipt_snapshot(
        root,
        partition,
        receipt.get("previous_projection"),
        directory=SNAPSHOT_PROJECTION_DIR,
        label="previous cross projection snapshot",
    )
    _, previous_manifest_raw, previous_manifest_sha = _receipt_snapshot(
        root,
        partition,
        receipt.get("previous_root_manifest"),
        directory=SNAPSHOT_MANIFEST_DIR,
        label="previous root manifest snapshot",
    )
    previous_projection = _json_from_bytes(
        previous_projection_raw, "previous cross projection snapshot"
    )
    previous_manifest = _json_from_bytes(
        previous_manifest_raw, "previous root manifest snapshot"
    )
    try:
        validate_projection(root, previous_projection)
    except ShadowCatalogError as exc:
        raise CrossActivationError("previous projection snapshot dependencies drifted") from exc
    if previous_projection.get("source_fingerprint") != source_fingerprint:
        raise CrossActivationError("previous projection snapshot fingerprint drifted")
    _validate_root_manifest_binding(
        previous_manifest, projection_sha256=previous_projection_sha
    )

    activated = receipt.get("activated_projection")
    if not isinstance(activated, dict) or set(activated) != {"ref", "sha256"}:
        raise CrossActivationError("activated projection commitment is invalid")
    activated_ref = str(activated.get("ref") or "")
    activated_sha = str(activated.get("sha256") or "")
    expected_ref = DEFAULT_PROJECTION_RELATIVE.as_posix()
    if activated_ref != expected_ref or not SHA_RE.fullmatch(activated_sha):
        raise CrossActivationError("activated projection commitment is invalid")
    if require_activated_projection:
        current_projection = root / DEFAULT_PROJECTION_RELATIVE
        if _sha256_bytes(
            _bounded_regular_bytes(current_projection, "activated cross projection")
        ) != activated_sha:
            raise CrossActivationError("activated cross projection drifted")
    return {
        "receipt": receipt,
        "receipt_sha256": receipt_sha256,
        "partition": partition,
        "source_fingerprint": source_fingerprint,
        "evaluation_ledger_sha256": ledger_sha,
        "previous_projection_raw": previous_projection_raw,
        "previous_projection_sha256": previous_projection_sha,
        "previous_root_manifest_raw": previous_manifest_raw,
        "previous_root_manifest_sha256": previous_manifest_sha,
        "activated_projection_sha256": activated_sha,
    }


def validate_rollback_receipt(
    repo: str | Path,
    rollback_receipt_sha256: str,
) -> dict[str, Any]:
    root = Path(repo).expanduser().resolve()
    base = build_shadow_catalog(root)
    partition = _fingerprint_root(root, str(base["source_fingerprint"]))
    receipt, _ = _load_receipt_entity(
        partition,
        directory=ROLLBACK_RECEIPT_DIR,
        pointer_name=ROLLBACK_RECEIPT_POINTER,
        expected_sha256=rollback_receipt_sha256,
        label="rollback receipt",
    )
    allowed = {
        "schema",
        "activation_receipt_sha256",
        "source_fingerprint",
        "dependency_hashes",
        "evaluation_ledger_sha256",
        "replaced_projection_sha256",
        "restored_projection_sha256",
        "previous_root_manifest_sha256",
        "rollback_before_manifest",
        "rollback_manifest_state",
        "rollback_boundary",
        "personalization_rebuild_required",
        "formal_write_count",
    }
    activation_sha = str(receipt.get("activation_receipt_sha256") or "")
    context = validate_activation_receipt(
        root, activation_sha, require_activated_projection=False
    )
    _, manifest_raw, manifest_sha = _receipt_snapshot(
        root,
        context["partition"],
        receipt.get("rollback_before_manifest"),
        directory=SNAPSHOT_MANIFEST_DIR,
        label="rollback-before root manifest snapshot",
    )
    manifest_state, manifest_sha = _rollback_manifest_state(
        manifest_raw,
        previous_manifest_sha256=context["previous_root_manifest_sha256"],
        activated_projection_sha256=context["activated_projection_sha256"],
    )
    expected = {
        "schema": ROLLBACK_RECEIPT_SCHEMA,
        "activation_receipt_sha256": activation_sha,
        "source_fingerprint": context["source_fingerprint"],
        "dependency_hashes": _activation_dependency_hashes(),
        "evaluation_ledger_sha256": context["evaluation_ledger_sha256"],
        "replaced_projection_sha256": context["activated_projection_sha256"],
        "restored_projection_sha256": context["previous_projection_sha256"],
        "previous_root_manifest_sha256": context[
            "previous_root_manifest_sha256"
        ],
        "rollback_before_manifest": receipt.get("rollback_before_manifest"),
        "rollback_manifest_state": manifest_state,
        "rollback_boundary": "pre_or_post_personalization_rebuild",
        "personalization_rebuild_required": True,
        "formal_write_count": 0,
    }
    if set(receipt) != allowed or receipt != expected:
        raise CrossActivationError("rollback receipt is invalid or drifted")
    projection_path = root / DEFAULT_PROJECTION_RELATIVE
    if (
        _sha256_bytes(
            _bounded_regular_bytes(projection_path, "rolled-back cross projection")
        )
        != context["previous_projection_sha256"]
    ):
        raise CrossActivationError("rolled-back cross projection drifted")
    return {
        "receipt": receipt,
        "receipt_sha256": rollback_receipt_sha256,
        "activation_receipt_sha256": activation_sha,
        "source_fingerprint": context["source_fingerprint"],
        "rollback_manifest_state": manifest_state,
        "personalization_rebuild_required": True,
        "rollback_before_manifest_sha256": manifest_sha,
    }


def activate_eligible_routes(repo: str | Path) -> dict[str, Any]:
    _retired_write_surface()
    root = Path(repo).expanduser().resolve()
    base = build_shadow_catalog(root)
    edges = {str(row["edge_id"]): row for row in base["records"]}
    durable = _durable_root(root)
    partition = _fingerprint_root(root, base["source_fingerprint"])
    ledger = partition / "evaluations.jsonl"
    with _lock(durable / ".cross-activation.lock"):
        prestate = _snapshot_activation_prestate(
            root,
            partition,
            source_fingerprint=str(base["source_fingerprint"]),
        )
        events = _read_ledger(
            root,
            ledger,
            current_edges=edges,
            source_fingerprint=base["source_fingerprint"],
        )
        enabled, decisions = _policy_decisions(base, events)
        projection = _build_shadow_catalog_with_live_allowlist(
            root,
            adjacency_limit=int(base["adjacency_limit"]),
            enabled_live_edge_ids=enabled,
        )
        if projection.get("source_fingerprint") != base["source_fingerprint"]:
            raise CrossActivationError("cross sources drifted while activation was prepared")
        projection_path = root / DEFAULT_PROJECTION_RELATIVE
        manifest_path = root / PERSONALIZATION_MANIFEST_RELATIVE
        try:
            projection_receipt = _write_activated_projection(root, projection)
            post_base = build_shadow_catalog(root)
            if (
                post_base.get("source_fingerprint") != base["source_fingerprint"]
                or post_base.get("source_manifest") != base.get("source_manifest")
                or _sha256_bytes(
                    _bounded_regular_bytes(
                        manifest_path, "personalization root manifest"
                    )
                )
                != prestate["root_manifest"]["sha256"]
            ):
                raise CrossActivationError("activation dependencies drifted during write")
            receipt = {
                "schema": ACTIVATION_RECEIPT_SCHEMA,
                "source_fingerprint": base["source_fingerprint"],
                "source_manifest": base["source_manifest"],
                "dependency_hashes": _activation_dependency_hashes(),
                "evaluation_ledger_sha256": (
                    _sha256_file(ledger) if ledger.is_file() else None
                ),
                "policy": _activation_policy(),
                "enabled_edge_ids": enabled,
                "decisions": decisions,
                "previous_projection": prestate["projection"],
                "previous_root_manifest": prestate["root_manifest"],
                "activated_projection": {
                    "ref": DEFAULT_PROJECTION_RELATIVE.as_posix(),
                    "sha256": projection_receipt["sha256"],
                },
                "rollback_boundary": "pre_or_post_personalization_rebuild",
                "personalization_rebuild_required": True,
                "formal_write_count": 0,
            }
            receipt_sha = _write_receipt_entity(
                partition,
                directory=ACTIVATION_RECEIPT_DIR,
                pointer_name=ACTIVATION_RECEIPT_POINTER,
                receipt=receipt,
                label="activation receipt",
            )
        except Exception:
            _atomic_write_bytes(projection_path, prestate["projection_raw"])
            raise
    return {
        "status": "activated",
        "enabled_edge_ids": enabled,
        "enabled_count": len(enabled),
        "activation_receipt_sha256": receipt_sha,
        "projection_sha256": projection_receipt["sha256"],
        "personalization_rebuild_required": True,
        "formal_write_count": 0,
    }


def rollback_activation(
    repo: str | Path,
    activation_receipt_sha256: str,
) -> dict[str, Any]:
    _retired_write_surface()
    root = Path(repo).expanduser().resolve()
    expected_sha = str(activation_receipt_sha256 or "")
    if not SHA_RE.fullmatch(expected_sha):
        raise CrossActivationError("activation receipt SHA-256 is invalid")
    durable = _durable_root(root)
    with _lock(durable / ".cross-activation.lock"):
        base = build_shadow_catalog(root)
        partition = _fingerprint_root(root, str(base["source_fingerprint"]))
        rollback_pointer = partition / ROLLBACK_RECEIPT_POINTER
        if rollback_pointer.is_file():
            pointer_raw = _bounded_regular_bytes(
                rollback_pointer, "latest rollback receipt"
            )
            pointer_value = _json_from_bytes(pointer_raw, "latest rollback receipt")
            if pointer_value.get("activation_receipt_sha256") == expected_sha:
                validated = validate_rollback_receipt(
                    root, _sha256_bytes(pointer_raw)
                )
                return {
                    "status": "already_rolled_back",
                    "activation_receipt_sha256": expected_sha,
                    "rollback_receipt_sha256": validated["receipt_sha256"],
                    "source_fingerprint": validated["source_fingerprint"],
                    "rollback_manifest_state": validated[
                        "rollback_manifest_state"
                    ],
                    "personalization_rebuild_required": True,
                    "formal_write_count": 0,
                }

        context = validate_activation_receipt(
            root, expected_sha, require_activated_projection=True
        )
        manifest_path = root / PERSONALIZATION_MANIFEST_RELATIVE
        projection_path = root / DEFAULT_PROJECTION_RELATIVE
        current_manifest_raw = _bounded_regular_bytes(
            manifest_path, "current personalization root manifest"
        )
        manifest_state, current_manifest_sha = _rollback_manifest_state(
            current_manifest_raw,
            previous_manifest_sha256=context["previous_root_manifest_sha256"],
            activated_projection_sha256=context[
                "activated_projection_sha256"
            ],
        )
        rollback_manifest_snapshot = (
            partition
            / SNAPSHOT_MANIFEST_DIR
            / f"{current_manifest_sha}.json"
        )
        _write_content_addressed_bytes(
            rollback_manifest_snapshot,
            current_manifest_raw,
            "rollback-before root manifest snapshot",
        )
        rollback_manifest_commitment = {
            "ref": _repo_ref(
                root,
                rollback_manifest_snapshot,
                "rollback-before root manifest snapshot",
            ),
            "sha256": current_manifest_sha,
        }
        current_projection_raw = _bounded_regular_bytes(
            projection_path, "current activated cross projection"
        )
        receipt = {
            "schema": ROLLBACK_RECEIPT_SCHEMA,
            "activation_receipt_sha256": expected_sha,
            "source_fingerprint": context["source_fingerprint"],
            "dependency_hashes": _activation_dependency_hashes(),
            "evaluation_ledger_sha256": context["evaluation_ledger_sha256"],
            "replaced_projection_sha256": context[
                "activated_projection_sha256"
            ],
            "restored_projection_sha256": context[
                "previous_projection_sha256"
            ],
            "previous_root_manifest_sha256": context[
                "previous_root_manifest_sha256"
            ],
            "rollback_before_manifest": rollback_manifest_commitment,
            "rollback_manifest_state": manifest_state,
            "rollback_boundary": "pre_or_post_personalization_rebuild",
            "personalization_rebuild_required": True,
            "formal_write_count": 0,
        }
        try:
            _atomic_write_bytes(
                projection_path, context["previous_projection_raw"]
            )
            rollback_sha = _write_receipt_entity(
                partition,
                directory=ROLLBACK_RECEIPT_DIR,
                pointer_name=ROLLBACK_RECEIPT_POINTER,
                receipt=receipt,
                label="rollback receipt",
            )
            validate_rollback_receipt(root, rollback_sha)
        except Exception:
            _atomic_write_bytes(projection_path, current_projection_raw)
            raise
    return {
        "status": "rolled_back",
        "activation_receipt_sha256": expected_sha,
        "rollback_receipt_sha256": rollback_sha,
        "source_fingerprint": context["source_fingerprint"],
        "restored_projection_sha256": context["previous_projection_sha256"],
        "previous_root_manifest_sha256": context[
            "previous_root_manifest_sha256"
        ],
        "rollback_before_manifest_sha256": current_manifest_sha,
        "rollback_manifest_state": manifest_state,
        "personalization_rebuild_required": True,
        "formal_write_count": 0,
    }


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo", type=Path, default=Path.cwd())
    sub = parser.add_subparsers(dest="command", required=True)
    status = sub.add_parser("status")
    status.add_argument("--limit", type=int, default=DEFAULT_STATUS_LIMIT)
    issue = sub.add_parser("issue-assignments")
    issue.add_argument("--request", type=Path, required=True)
    record = sub.add_parser("record-evaluation")
    record.add_argument("--submission", type=Path, required=True)
    sub.add_parser("activate")
    rollback = sub.add_parser("rollback")
    rollback.add_argument("--activation-receipt-sha256", required=True)
    validate = sub.add_parser("validate-receipt")
    validate.add_argument("--kind", choices=("activation", "rollback"), required=True)
    validate.add_argument("--sha256", required=True)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        if args.command == "status":
            result = evaluation_status(args.repo, limit=args.limit)
        elif args.command == "issue-assignments":
            result = issue_assignments(args.repo, args.request)
        elif args.command == "record-evaluation":
            result = record_evaluation(args.repo, args.submission)
        elif args.command == "activate":
            result = activate_eligible_routes(args.repo)
        elif args.command == "rollback":
            result = rollback_activation(
                args.repo, args.activation_receipt_sha256
            )
        elif args.kind == "activation":
            validated = validate_activation_receipt(
                args.repo, args.sha256, require_activated_projection=True
            )
            result = {
                "status": "valid",
                "kind": "activation",
                "receipt_sha256": validated["receipt_sha256"],
                "source_fingerprint": validated["source_fingerprint"],
                "formal_write_count": 0,
            }
        else:
            validated = validate_rollback_receipt(args.repo, args.sha256)
            result = {
                "status": "valid",
                "kind": "rollback",
                "receipt_sha256": validated["receipt_sha256"],
                "source_fingerprint": validated["source_fingerprint"],
                "formal_write_count": 0,
            }
    except (CrossActivationError, ShadowCatalogError) as exc:
        print(json.dumps({"status": "error", "reason": str(exc)}, ensure_ascii=False))
        return 2
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    sys.exit(main())
