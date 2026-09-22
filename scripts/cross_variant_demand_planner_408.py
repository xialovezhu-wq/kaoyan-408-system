#!/usr/bin/env python3
"""Build one answer-safe, managed demand for a cross-subject variant.

This is a dated nightly cold-path bridge.  It binds one canonical failed
original-question outcome to the current personalization build, the exact
answer-isolated original source, the seven-day cooldown, and one current live
cross-subject edge.  It never authors a question and never changes formal data.

The optional ``prepare-demand`` command revalidates the complete demand before
delegating a matching private authoring-evidence package to
``cross_variant_pipeline_408.prepare_cross_variant``.
"""

from __future__ import annotations

import argparse
import contextlib
import datetime as dt
import fcntl
import hashlib
import json
import os
import re
import stat
import sys
import tempfile
from pathlib import Path
from typing import Any, Iterator
from zoneinfo import ZoneInfo

from cross_variant_pipeline_408 import (
    CrossVariantPipelineError,
    _prepare_cross_variant_hot_locked as prepare_cross_variant,
    validate_current_live_cross_edge,
)
from linked_practice_source_408 import (
    LinkedPracticeSourceError,
    validate_source_for_practice,
)
from review_hot_state_408 import HotStateError, lookup_event


REQUEST_SCHEMA = "cross_variant_demand_request_v1"
DEMAND_SCHEMA = "cross_variant_demand_v1"
IDEMPOTENCY_SCHEMA = "cross_variant_demand_idempotency_v1"
PREPARE_RECEIPT_SCHEMA = "cross_variant_demand_prepare_receipt_v1"

DEFAULT_STATE_RELATIVE = Path("wiki/study_vaults/408-full/state/personalization")
DEFAULT_REVIEW_RELATIVE = Path("wiki/study_vaults/408-full/state/review-loop")
DEFAULT_DEMAND_RELATIVE = Path(
    "wiki/study_vaults/408-full/state/linked-practice/cross-variant-demand"
)
DEFAULT_LINKED_PRACTICE_RELATIVE = DEFAULT_DEMAND_RELATIVE.parent
CROSS_PROJECTION_FILENAME = "cross-subject-shadow.json"

MAX_REQUEST_BYTES = 32 * 1024
MAX_MANIFEST_BYTES = 1024 * 1024
MAX_ARTIFACT_BYTES = 8 * 1024 * 1024
MAX_REVIEW_LEDGER_BYTES = 64 * 1024 * 1024
MAX_ARTIFACT_RECORDS = 4096
MAX_EVIDENCE_BYTES = 128 * 1024
COOLDOWN_DAYS = 7

FAILURE_RESULTS = {"partial", "wrong", "uncertain"}
SUBJECTS = {"DS", "CO", "OS", "CN"}
LIVE_RELATIONS = {"R01", "R02", "R04", "R05", "R06"}
FORMAL_NODE_RE = re.compile(r"^(?:DS|CO|OS|CN)_(?:\d{4}|UNK)_\d{3}$")
POINT_RE = re.compile(r"^(?:DS|CO|OS|CN)\d{2}-\d{2}$")
EDGE_RE = re.compile(r"^CSR408-[0-9A-F]{16}$")
DEMAND_RE = re.compile(r"^CVD-[0-9A-F]{24}$")
SHA_RE = re.compile(r"^[0-9a-f]{64}$")
SAFE_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{2,180}$")
MECHANISM_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:/-]{2,120}$")
DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
ANSWER_LEAK_RE = re.compile(
    r"(?:正确答案|标准答案|参考答案|答案是|answer\s*key|correct\s*answer|"
    r"完整解析|详细解析|solution\s*:)",
    re.IGNORECASE,
)
FORBIDDEN_KEYS = {
    "question",
    "question_text",
    "question_stem",
    "stem",
    "options",
    "answer",
    "correct_answer",
    "solution",
    "analysis",
    "explanation",
    "response",
    "full_response",
    "feedback",
    "first_break",
    "image",
    "images",
    "attachment",
    "attachments",
}


class CrossVariantDemandError(RuntimeError):
    """Fail-closed error for a managed cross-variant demand."""


def _retired_write_surface() -> None:
    raise CrossVariantDemandError(
        "cross_variant_demand_writes_retired_read_only_compatibility"
    )


def _canonical_bytes(value: Any) -> bytes:
    return (
        json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
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
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _digest(value: Any) -> str:
    return _sha256_bytes(_canonical_bytes(value))


def _inside(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
    except ValueError:
        return False
    return True


def _current_shanghai_date() -> str:
    return dt.datetime.now(ZoneInfo("Asia/Shanghai")).date().isoformat()


def _valid_date(raw: object, label: str) -> str:
    value = str(raw or "")
    if not DATE_RE.fullmatch(value):
        raise CrossVariantDemandError(f"{label} is invalid")
    try:
        return dt.date.fromisoformat(value).isoformat()
    except ValueError as exc:
        raise CrossVariantDemandError(f"{label} is invalid") from exc


def _days_between(older: str, newer: str) -> int:
    return (dt.date.fromisoformat(newer) - dt.date.fromisoformat(older)).days


def _assert_answer_safe(value: Any, trail: tuple[str, ...] = ()) -> None:
    if isinstance(value, dict):
        for key, item in value.items():
            normalized = str(key).strip().lower()
            if normalized in FORBIDDEN_KEYS:
                raise CrossVariantDemandError(
                    f"answer-bearing field is forbidden: {'.'.join(trail + (normalized,))}"
                )
            _assert_answer_safe(item, trail + (normalized,))
    elif isinstance(value, list):
        for index, item in enumerate(value):
            _assert_answer_safe(item, trail + (str(index),))
    elif isinstance(value, str) and ANSWER_LEAK_RE.search(value):
        raise CrossVariantDemandError(
            f"answer-bearing text is forbidden: {'.'.join(trail)}"
        )


def _repo_file(
    repo: Path,
    locator: str | Path,
    label: str,
    *,
    max_bytes: int,
) -> tuple[Path, str]:
    raw = str(locator or "").strip()
    if not raw or "\x00" in raw:
        raise CrossVariantDemandError(f"{label} path is invalid")
    candidate = Path(raw).expanduser()
    if ".." in candidate.parts:
        raise CrossVariantDemandError(f"{label} path is invalid")
    unresolved = candidate if candidate.is_absolute() else repo / candidate
    try:
        lexical_relative = unresolved.relative_to(repo)
    except ValueError:
        lexical_relative = None
    current = repo
    if lexical_relative is not None:
        for part in lexical_relative.parts:
            current = current / part
            if current.is_symlink():
                raise CrossVariantDemandError(f"{label} must not use symlinks")
    if unresolved.is_symlink():
        raise CrossVariantDemandError(f"{label} must not be a symlink")
    path = unresolved.resolve()
    if not _inside(path, repo) or not path.is_file() or path.is_symlink():
        raise CrossVariantDemandError(f"{label} escapes the repository or is missing")
    try:
        size = path.stat().st_size
    except OSError as exc:
        raise CrossVariantDemandError(f"{label} cannot be inspected") from exc
    if size > max_bytes:
        raise CrossVariantDemandError(f"{label} exceeds its bounded size")
    return path, path.relative_to(repo).as_posix()


def _read_snapshot(
    path: Path, label: str, *, max_bytes: int
) -> tuple[bytes, str]:
    """Read and hash one regular file through one no-follow descriptor."""

    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(path, flags)
    except OSError as exc:
        raise CrossVariantDemandError(f"{label} is missing or linked") from exc
    try:
        before = os.fstat(descriptor)
        if not stat.S_ISREG(before.st_mode) or before.st_size > max_bytes:
            raise CrossVariantDemandError(f"{label} is missing, linked, or oversized")
        chunks: list[bytes] = []
        total = 0
        while True:
            block = os.read(descriptor, min(1024 * 1024, max_bytes + 1 - total))
            if not block:
                break
            chunks.append(block)
            total += len(block)
            if total > max_bytes:
                raise CrossVariantDemandError(f"{label} exceeds its bounded size")
        after = os.fstat(descriptor)
    finally:
        os.close(descriptor)
    if (
        before.st_dev,
        before.st_ino,
        before.st_size,
        before.st_mtime_ns,
        before.st_ctime_ns,
    ) != (
        after.st_dev,
        after.st_ino,
        after.st_size,
        after.st_mtime_ns,
        after.st_ctime_ns,
    ):
        raise CrossVariantDemandError(f"{label} changed while it was being read")
    payload = b"".join(chunks)
    if len(payload) != before.st_size:
        raise CrossVariantDemandError(f"{label} changed while it was being read")
    return payload, _sha256_bytes(payload)


def _load_object_snapshot(
    path: Path, label: str, *, max_bytes: int
) -> tuple[dict[str, Any], str, bytes]:
    payload, digest = _read_snapshot(path, label, max_bytes=max_bytes)
    try:
        value = json.loads(payload)
    except (UnicodeError, json.JSONDecodeError) as exc:
        raise CrossVariantDemandError(f"{label} is not valid JSON") from exc
    if not isinstance(value, dict):
        raise CrossVariantDemandError(f"{label} must be a JSON object")
    return value, digest, payload


def _load_object(path: Path, label: str, *, max_bytes: int) -> dict[str, Any]:
    value, _, _ = _load_object_snapshot(path, label, max_bytes=max_bytes)
    return value


def _reject_symlink_components(repo: Path, path: Path, label: str) -> None:
    try:
        relative = path.relative_to(repo)
    except ValueError as exc:
        raise CrossVariantDemandError(f"{label} escapes the repository") from exc
    current = repo
    for part in relative.parts:
        current = current / part
        try:
            metadata = os.lstat(current)
        except FileNotFoundError:
            continue
        except OSError as exc:
            raise CrossVariantDemandError(f"{label} cannot be inspected") from exc
        if stat.S_ISLNK(metadata.st_mode):
            raise CrossVariantDemandError(f"{label} must not use symlink components")


def _managed_demand_root(repo: Path, *, create: bool) -> Path:
    """Return the managed output root only after lexical and resolved containment checks."""

    linked_root = repo / DEFAULT_LINKED_PRACTICE_RELATIVE
    demand_root = repo / DEFAULT_DEMAND_RELATIVE
    _reject_symlink_components(repo, linked_root, "linked-practice state root")
    _reject_symlink_components(repo, demand_root, "cross-variant demand output root")
    if create:
        demand_root.mkdir(parents=True, exist_ok=True, mode=0o700)
        _reject_symlink_components(repo, demand_root, "cross-variant demand output root")
    resolved_linked = linked_root.resolve()
    resolved_demand = demand_root.resolve()
    if not _inside(resolved_demand, resolved_linked):
        raise CrossVariantDemandError(
            "cross-variant demand output root escapes linked-practice state"
        )
    return demand_root


def _managed_repo_state_root(repo: Path, relative: Path, label: str) -> Path:
    path = repo / relative
    _reject_symlink_components(repo, path, label)
    if not _inside(path.resolve(), repo):
        raise CrossVariantDemandError(f"{label} escapes the repository")
    return path


def _managed_demand_path(
    repo: Path, *parts: str, create_root: bool = False
) -> Path:
    demand_root = _managed_demand_root(repo, create=create_root)
    path = demand_root.joinpath(*parts)
    _reject_symlink_components(repo, path, "cross-variant demand managed path")
    resolved_linked = (repo / DEFAULT_LINKED_PRACTICE_RELATIVE).resolve()
    if not _inside(path.resolve(), resolved_linked):
        raise CrossVariantDemandError(
            "cross-variant demand managed path escapes linked-practice state"
        )
    return path


def _atomic_write(path: Path, payload: bytes) -> str:
    _retired_write_surface()

    # Historical implementation retained below for read-only source compatibility.
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    descriptor, temporary_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
        directory = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    finally:
        with contextlib.suppress(FileNotFoundError):
            temporary.unlink()
    return _sha256_file(path)


def _atomic_write_json(path: Path, value: Any) -> str:
    return _atomic_write(path, _pretty_bytes(value))


@contextlib.contextmanager
def _lock(path: Path) -> Iterator[None]:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    flags = os.O_RDWR | os.O_CREAT | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    descriptor = os.open(path, flags, 0o600)
    with os.fdopen(descriptor, "a+b") as handle:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def _canonical_targets(raw: object) -> list[dict[str, str]]:
    if not isinstance(raw, list) or len(raw) != 2:
        raise CrossVariantDemandError("exactly two cross-subject targets are required")
    targets: list[dict[str, str]] = []
    for index, item in enumerate(raw, start=1):
        if not isinstance(item, dict) or set(item) != {
            "subject",
            "point_id",
            "mechanism_key",
        }:
            raise CrossVariantDemandError(f"target {index} is incomplete")
        target = {
            "subject": str(item.get("subject") or ""),
            "point_id": str(item.get("point_id") or ""),
            "mechanism_key": str(item.get("mechanism_key") or ""),
        }
        if (
            target["subject"] not in SUBJECTS
            or not POINT_RE.fullmatch(target["point_id"])
            or target["point_id"][:2] != target["subject"]
            or not MECHANISM_RE.fullmatch(target["mechanism_key"])
        ):
            raise CrossVariantDemandError(f"target {index} is invalid")
        targets.append(target)
    if len({item["subject"] for item in targets}) != 2:
        raise CrossVariantDemandError("cross demand must use two distinct subjects")
    if len({item["point_id"] for item in targets}) != 2:
        raise CrossVariantDemandError("cross demand must use two distinct points")
    return sorted(targets, key=lambda item: (item["subject"], item["point_id"]))


def _optional_sha(raw: object, label: str) -> str | None:
    if raw is None:
        return None
    value = str(raw)
    if not SHA_RE.fullmatch(value):
        raise CrossVariantDemandError(f"{label} must be null or lowercase SHA-256")
    return value


def _optional_source_ref(raw: object) -> str | None:
    if raw is None:
        return None
    value = str(raw).strip()
    if not value or len(value.encode("utf-8")) > 1024 or "\n" in value or "\r" in value:
        raise CrossVariantDemandError("original source reference is invalid")
    return value


def _normalize_request(raw: dict[str, Any]) -> dict[str, Any]:
    required = {
        "schema",
        "execution_scope",
        "canonical_failure_event_id",
        "canonical_failure_event_sha256",
        "anchor_formal_node_id",
        "original_source_ref",
        "original_source_sha256",
        "original_answer_isolation_attestation_sha256",
        "expected_effective_last_attempt_date",
        "verified_cross_edge_id",
        "targets",
        "expected_cross_activation",
        "personalization_manifest_sha256",
        "personalization_build_id",
        "cross_source_fingerprint",
        "as_of",
        "cooldown_days",
        "idempotency_key",
        "formal_write_count",
    }
    if set(raw) != required:
        raise CrossVariantDemandError("cross-variant demand request fields are incomplete")
    event_id = str(raw.get("canonical_failure_event_id") or "")
    formal_node_id = str(raw.get("anchor_formal_node_id") or "")
    edge_id = str(raw.get("verified_cross_edge_id") or "")
    idempotency_key = str(raw.get("idempotency_key") or "")
    build_id = str(raw.get("personalization_build_id") or "")
    if (
        raw.get("schema") != REQUEST_SCHEMA
        or raw.get("execution_scope") != "dated_nightly_cold_path"
        or raw.get("expected_cross_activation") != "verified_live"
        or raw.get("cooldown_days") != COOLDOWN_DAYS
        or raw.get("formal_write_count") != 0
        or not SAFE_ID_RE.fullmatch(event_id)
        or not FORMAL_NODE_RE.fullmatch(formal_node_id)
        or not EDGE_RE.fullmatch(edge_id)
        or not SAFE_ID_RE.fullmatch(idempotency_key)
        or not SHA_RE.fullmatch(build_id)
    ):
        raise CrossVariantDemandError("cross-variant demand request is invalid")
    expected_date = raw.get("expected_effective_last_attempt_date")
    if expected_date is not None:
        expected_date = _valid_date(expected_date, "expected last attempt date")
    request = {
        "schema": REQUEST_SCHEMA,
        "execution_scope": "dated_nightly_cold_path",
        "canonical_failure_event_id": event_id,
        "canonical_failure_event_sha256": _optional_sha(
            raw.get("canonical_failure_event_sha256"), "canonical event SHA-256"
        ),
        "anchor_formal_node_id": formal_node_id,
        "original_source_ref": _optional_source_ref(raw.get("original_source_ref")),
        "original_source_sha256": _optional_sha(
            raw.get("original_source_sha256"), "original source SHA-256"
        ),
        "original_answer_isolation_attestation_sha256": _optional_sha(
            raw.get("original_answer_isolation_attestation_sha256"),
            "answer-isolation attestation SHA-256",
        ),
        "expected_effective_last_attempt_date": expected_date,
        "verified_cross_edge_id": edge_id,
        "targets": _canonical_targets(raw.get("targets")),
        "expected_cross_activation": "verified_live",
        "personalization_manifest_sha256": _optional_sha(
            raw.get("personalization_manifest_sha256"),
            "personalization manifest SHA-256",
        ),
        "personalization_build_id": build_id,
        "cross_source_fingerprint": _optional_sha(
            raw.get("cross_source_fingerprint"), "cross source fingerprint"
        ),
        "as_of": _valid_date(raw.get("as_of"), "as-of date"),
        "cooldown_days": COOLDOWN_DAYS,
        "idempotency_key": idempotency_key,
        "formal_write_count": 0,
    }
    required_sha_fields = (
        "canonical_failure_event_sha256",
        "personalization_manifest_sha256",
        "cross_source_fingerprint",
    )
    if any(request[field] is None for field in required_sha_fields):
        raise CrossVariantDemandError("required request commitments cannot be null")
    if (request["original_source_ref"] is None) != (
        request["original_source_sha256"] is None
    ):
        raise CrossVariantDemandError("original source ref and hash must be both set or both null")
    _assert_answer_safe(request)
    return request


def _load_artifact(
    state: Path,
    manifest: dict[str, Any],
    name: str,
) -> tuple[dict[str, Any], str]:
    expected = str((manifest.get("artifact_hashes") or {}).get(name) or "")
    path = state / name
    if not SHA_RE.fullmatch(expected):
        raise CrossVariantDemandError(f"personalization manifest does not bind {name}")
    payload, actual, _ = _load_object_snapshot(
        path, name, max_bytes=MAX_ARTIFACT_BYTES
    )
    if actual != expected:
        raise CrossVariantDemandError(f"personalization artifact drifted: {name}")
    return payload, expected


def _list_of_strings(raw: object) -> list[str]:
    if not isinstance(raw, list):
        return []
    return [str(item) for item in raw if str(item)]


def _evaluate_request(repo: Path, request: dict[str, Any]) -> dict[str, Any]:
    state = _managed_repo_state_root(
        repo, DEFAULT_STATE_RELATIVE, "personalization state root"
    )
    manifest_path = state / "manifest.json"
    manifest, manifest_sha, _ = _load_object_snapshot(
        manifest_path, "personalization manifest", max_bytes=MAX_MANIFEST_BYTES
    )
    if (
        manifest_sha != request["personalization_manifest_sha256"]
        or manifest.get("schema") != "personalization_index_v1"
        or manifest.get("answer_safe") is not True
        or manifest.get("formal_write_count") != 0
        or manifest.get("build_id") != request["personalization_build_id"]
        or manifest.get("as_of") != request["as_of"]
        or request["as_of"] != _current_shanghai_date()
    ):
        raise CrossVariantDemandError("personalization manifest is stale, unsafe, or drifted")

    event_mappings, event_mappings_sha = _load_artifact(
        state, manifest, "event_mappings.json"
    )
    question_index, question_index_sha = _load_artifact(
        state, manifest, "question_index.json"
    )
    mappings = event_mappings.get("events")
    questions = question_index.get("questions")
    raw_mechanisms = question_index.get("mechanisms")
    mechanisms = raw_mechanisms if isinstance(raw_mechanisms, dict) else {}
    mechanism_projection_missing = not isinstance(raw_mechanisms, dict)
    if (
        event_mappings.get("schema") != "personalization_index_v1"
        or question_index.get("schema") != "personalization_index_v1"
        or not isinstance(mappings, dict)
        or not isinstance(questions, dict)
        or len(mappings) > MAX_ARTIFACT_RECORDS * 8
        or len(questions) > MAX_ARTIFACT_RECORDS
        or len(mechanisms) > MAX_ARTIFACT_RECORDS * 8
    ):
        raise CrossVariantDemandError("personalization mapping or question index is invalid")

    review_root = _managed_repo_state_root(
        repo, DEFAULT_REVIEW_RELATIVE, "review-loop state root"
    )
    events_path = review_root / "events.jsonl"
    expected_events_sha = str((manifest.get("source_hashes") or {}).get("events") or "")
    if (
        not SHA_RE.fullmatch(expected_events_sha)
        or not events_path.is_file()
        or events_path.is_symlink()
        or _read_snapshot(
            events_path,
            "canonical review ledger",
            max_bytes=MAX_REVIEW_LEDGER_BYTES,
        )[1]
        != expected_events_sha
    ):
        raise CrossVariantDemandError("canonical review ledger drifted from personalization build")

    try:
        found = lookup_event(
            repo, event_id=request["canonical_failure_event_id"]
        )
    except (HotStateError, OSError) as exc:
        raise CrossVariantDemandError("canonical failure outcome exact lookup failed") from exc
    event = found.get("event")
    if (
        found.get("status") != "found"
        or not isinstance(event, dict)
        or found.get("event_sha256") != request["canonical_failure_event_sha256"]
    ):
        raise CrossVariantDemandError("canonical failure outcome commitment drifted")

    anchor = request["anchor_formal_node_id"]
    mapped_formals = set(_list_of_strings(event.get("mapped_formal_node_ids")))
    event_identity_ok = (
        event.get("schema") == "review_event_v1"
        and event.get("event_kind") == "outcome"
        and event.get("first_result") in FAILURE_RESULTS
        and event.get("presentation_kind") == "original_question"
        and event.get("practice_mode") != "variant"
        and not event.get("variant_of_formal_node_id")
        and event.get("formal_node_id") == anchor
        and (not mapped_formals or mapped_formals == {anchor})
    )
    if not event_identity_ok:
        raise CrossVariantDemandError(
            "canonical event is not one exact failed original-question outcome"
        )
    observed_date = _valid_date(event.get("observed_date"), "event observed date")
    if observed_date > request["as_of"]:
        raise CrossVariantDemandError("canonical event date is in the future")

    mapping = mappings.get(request["canonical_failure_event_id"])
    if not isinstance(mapping, dict):
        raise CrossVariantDemandError("canonical failure outcome mapping is missing")
    question_nodes = set(_list_of_strings(mapping.get("question_node_ids")))
    formal_nodes = set(_list_of_strings(mapping.get("formal_node_ids")))
    if (
        mapping.get("event_id") != request["canonical_failure_event_id"]
        or mapping.get("event_kind") != "outcome"
        or mapping.get("mapping_status") != "resolved"
        or mapping.get("first_result") != event.get("first_result")
        or mapping.get("observed_date") != observed_date
        or mapping.get("presentation_kind") != "original_question"
        or mapping.get("practice_mode") == "variant"
        or question_nodes != {anchor}
        or anchor not in formal_nodes
    ):
        raise CrossVariantDemandError("canonical failure outcome identity is not exactly resolved")

    question = questions.get(anchor)
    if not isinstance(question, dict) or question.get("node_id") != anchor:
        raise CrossVariantDemandError("anchor original is missing from the current question index")
    anchor_subject = anchor[:2]
    targets = request["targets"]
    anchor_targets = [item for item in targets if item["subject"] == anchor_subject]
    if len(anchor_targets) != 1:
        raise CrossVariantDemandError("anchor subject is not exactly one cross target")
    anchor_target = anchor_targets[0]

    integrity_blockers: list[str] = []
    source_gaps: list[str] = (
        ["target_mechanism_projection_missing"]
        if mechanism_projection_missing
        else []
    )
    mapping_points = set(_list_of_strings(mapping.get("point_ids")))
    mapping_mechanisms = set(_list_of_strings(mapping.get("mechanism_ids")))
    question_points = set(_list_of_strings(question.get("point_ids")))
    question_primary_points = set(_list_of_strings(question.get("primary_point_ids")))
    question_mechanisms = set(_list_of_strings(question.get("mechanism_ids")))
    if (
        event.get("knowledge_point") != anchor_target["point_id"]
        or anchor_target["point_id"] not in mapping_points
        or anchor_target["point_id"] not in question_points
        or anchor_target["point_id"] not in question_primary_points
    ):
        integrity_blockers.append("anchor_point_mismatch")
    if (
        event.get("mechanism_key") != anchor_target["mechanism_key"]
        or (
            mapping_mechanisms
            and anchor_target["mechanism_key"] not in mapping_mechanisms
        )
        or (
            question_mechanisms
            and anchor_target["mechanism_key"] not in question_mechanisms
        )
    ):
        integrity_blockers.append("anchor_mechanism_mismatch")

    # A syntactically valid mechanism key is not identity evidence.  Both ends
    # must bind the exact target point and subject through this manifest-bound
    # question-index projection.
    for target in targets:
        mechanism = mechanisms.get(target["mechanism_key"])
        if not isinstance(mechanism, dict):
            source_gaps.append("target_mechanism_binding_missing")
            continue
        mechanism_points = set(_list_of_strings(mechanism.get("point_ids")))
        mechanism_nodes = set(_list_of_strings(mechanism.get("formal_node_ids")))
        bound_nodes = {
            node_id
            for node_id in mechanism_nodes
            if FORMAL_NODE_RE.fullmatch(node_id)
            and isinstance(questions.get(node_id), dict)
            and questions[node_id].get("node_id") == node_id
            and questions[node_id].get("subject") == target["subject"]
            and target["point_id"]
            in set(_list_of_strings(questions[node_id].get("point_ids")))
            and target["mechanism_key"]
            in set(_list_of_strings(questions[node_id].get("mechanism_ids")))
        }
        if (
            mechanism.get("mechanism_id") != target["mechanism_key"]
            or target["point_id"] not in mechanism_points
            or not bound_nodes
        ):
            integrity_blockers.append("target_mechanism_binding_mismatch")

    projection_path = state / CROSS_PROJECTION_FILENAME
    projection_binding = (manifest.get("independent_projections") or {}).get(
        "cross_subject_shadow"
    )
    expected_projection_sha = str(manifest.get("cross_subject_shadow_sha256") or "")
    if (
        not isinstance(projection_binding, dict)
        or projection_binding.get("included_in_this_build") is not True
        or projection_binding.get("file") != CROSS_PROJECTION_FILENAME
        or not SHA_RE.fullmatch(expected_projection_sha)
    ):
        raise CrossVariantDemandError("current cross projection is not manifest-bound")
    projection, projection_sha, _ = _load_object_snapshot(
        projection_path, "current cross projection", max_bytes=MAX_ARTIFACT_BYTES
    )
    records = projection.get("records")
    if (
        projection_sha != expected_projection_sha
        or projection.get("schema_version") != "cross_subject_shadow_v1"
        or projection.get("formal_write_count") != 0
        or projection.get("source_fingerprint")
        != request["cross_source_fingerprint"]
        or not isinstance(records, list)
        or len(records) > MAX_ARTIFACT_RECORDS
    ):
        raise CrossVariantDemandError("current cross projection is stale, unsafe, or drifted")
    matching_edges = [
        record
        for record in records
        if isinstance(record, dict)
        and record.get("edge_id") == request["verified_cross_edge_id"]
    ]
    if len(matching_edges) != 1:
        integrity_blockers.append("cross_edge_missing_or_ambiguous")
        edge: dict[str, Any] = {}
    else:
        edge = matching_edges[0]
    edge_endpoints = {
        str((edge.get("left") or {}).get("id") or ""),
        str((edge.get("right") or {}).get("id") or ""),
    }
    target_points = {item["point_id"] for item in targets}
    if edge and edge_endpoints != target_points:
        integrity_blockers.append("cross_target_mismatch")
    if edge and edge.get("relation_code") not in LIVE_RELATIONS:
        integrity_blockers.append("cross_relation_not_activatable")

    edge_claims_live = bool(
        edge
        and edge.get("lifecycle") == "verified_existing"
        and edge.get("activation_eligible") is True
        and edge.get("enabled_for_live_routing") is True
        and edge.get("activation") == "verified_live"
    )
    live_verified = False
    if edge_claims_live and not integrity_blockers:
        try:
            validate_current_live_cross_edge(
                repo,
                edge_id=request["verified_cross_edge_id"],
                targets=targets,
            )
        except CrossVariantPipelineError as exc:
            raise CrossVariantDemandError("current live cross edge failed revalidation") from exc
        live_verified = True

    gaps: list[str] = list(source_gaps)
    source = question.get("reliable_source")
    source = source if isinstance(source, dict) else {}
    current_source_ref = str(source.get("source_ref") or "") or None
    current_source_sha = str(source.get("source_sha256") or "") or None
    expected_source_ref = request["original_source_ref"]
    expected_source_sha = request["original_source_sha256"]
    if expected_source_ref != current_source_ref or expected_source_sha != current_source_sha:
        raise CrossVariantDemandError("anchor original source commitment drifted")
    attestation_sha: str | None = None
    if (
        source.get("original_question_eligible") is not True
        or not current_source_ref
        or not current_source_sha
    ):
        gaps.append("original_question_source_missing")
    else:
        try:
            checked_source = validate_source_for_practice(
                repo,
                current_source_ref,
                current_source_sha,
                practice_mode="original",
                formal_node_id=anchor,
            )
        except LinkedPracticeSourceError:
            gaps.append("original_answer_isolation_attestation_missing_or_invalid")
        else:
            attestation_sha = str(
                checked_source.get("answer_isolation_attestation_sha256") or ""
            ) or None
            if (
                request["original_answer_isolation_attestation_sha256"]
                != attestation_sha
            ):
                raise CrossVariantDemandError(
                    "anchor answer-isolation attestation commitment drifted"
                )

    last_attempt = question.get("effective_last_attempt_date")
    if last_attempt is None:
        normalized_last_attempt = None
        gaps.append("original_attempt_date_missing")
        age: int | None = None
    else:
        normalized_last_attempt = _valid_date(last_attempt, "effective last attempt date")
        age = _days_between(normalized_last_attempt, request["as_of"])
        if age < 0:
            integrity_blockers.append("original_attempt_date_in_future")
        if normalized_last_attempt < observed_date:
            gaps.append("cooldown_projection_stale_for_trigger_event")
    if request["expected_effective_last_attempt_date"] != normalized_last_attempt:
        raise CrossVariantDemandError("anchor cooldown date commitment drifted")

    if integrity_blockers:
        status = "blocked"
        route_action = "repair_cross_or_anchor_binding"
        reason_codes = sorted(set(integrity_blockers))
    elif gaps:
        status = "source_gap"
        route_action = "repair_original_source_or_projection"
        reason_codes = sorted(set(gaps))
    elif age is not None and age >= COOLDOWN_DAYS:
        status = "original_due"
        route_action = "use_verified_original_question"
        reason_codes = ["original_cooldown_elapsed"]
    elif not live_verified:
        status = "blocked"
        route_action = "wait_for_verified_live_cross_edge"
        reason_codes = ["cross_edge_not_live"]
    else:
        status = "ready_for_cross_variant"
        route_action = "prepare_cross_variant_authoring"
        reason_codes = []

    request_sha = _sha256_bytes(_pretty_bytes(request))
    request_ref = f"{DEFAULT_DEMAND_RELATIVE.as_posix()}/requests/{request_sha}.json"
    fixed_prepare_fields = None
    if status == "ready_for_cross_variant":
        fixed_prepare_fields = {
            "schema": "cross_variant_authoring_evidence_v1",
            "evidence_scope": "verified_original_private_answer_and_verified_cross_edge",
            "original_formal_node_id": anchor,
            "original_source_ref": current_source_ref,
            "original_source_sha256": current_source_sha,
            "cross_projection_ref": (
                DEFAULT_STATE_RELATIVE / CROSS_PROJECTION_FILENAME
            ).as_posix(),
            "cross_projection_sha256": expected_projection_sha,
            "verified_cross_edge_id": request["verified_cross_edge_id"],
            "targets": targets,
            "formal_write_count": 0,
        }

    material = {
        "request_sha256": request_sha,
        "status": status,
        "reason_codes": reason_codes,
        "canonical_failure_event_sha256": request["canonical_failure_event_sha256"],
        "anchor_formal_node_id": anchor,
        "original_source_sha256": current_source_sha,
        "answer_isolation_attestation_sha256": attestation_sha,
        "effective_last_attempt_date": normalized_last_attempt,
        "days_since_original_attempt": age,
        "verified_cross_edge_id": request["verified_cross_edge_id"],
        "cross_projection_sha256": expected_projection_sha,
        "cross_source_fingerprint": request["cross_source_fingerprint"],
        "personalization_manifest_sha256": manifest_sha,
    }
    demand_id = "CVD-" + _digest(material)[:24].upper()
    demand = {
        "schema": DEMAND_SCHEMA,
        "kind": "managed_nightly_cross_variant_demand",
        "demand_id": demand_id,
        "status": status,
        "route_action": route_action,
        "reason_codes": reason_codes,
        "answer_safe": True,
        "execution_scope": "dated_nightly_cold_path",
        "hot_path_eligible": False,
        "question_authored": False,
        "request_ref": request_ref,
        "request_sha256": request_sha,
        "idempotency_key_sha256": _sha256_bytes(
            request["idempotency_key"].encode("utf-8")
        ),
        "canonical_failure": {
            "event_id": request["canonical_failure_event_id"],
            "event_sha256": request["canonical_failure_event_sha256"],
            "first_result": event["first_result"],
            "observed_date": observed_date,
        },
        "anchor_original": {
            "formal_node_id": anchor,
            "subject": anchor_subject,
            "source_ref": current_source_ref,
            "source_sha256": current_source_sha,
            "answer_isolation_attestation_sha256": attestation_sha,
            "effective_last_attempt_date": normalized_last_attempt,
            "days_since_original_attempt": age,
            "cooldown_days": COOLDOWN_DAYS,
        },
        "cross_binding": {
            "edge_id": request["verified_cross_edge_id"],
            "relation_code": edge.get("relation_code") if edge else None,
            "activation": edge.get("activation") if edge else None,
            "enabled_for_live_routing": bool(
                edge.get("enabled_for_live_routing") if edge else False
            ),
            "live_revalidation_passed": live_verified,
            "targets": targets,
            "projection_ref": (
                DEFAULT_STATE_RELATIVE / CROSS_PROJECTION_FILENAME
            ).as_posix(),
            "projection_sha256": expected_projection_sha,
            "source_fingerprint": request["cross_source_fingerprint"],
        },
        "personalization_binding": {
            "manifest_ref": (DEFAULT_STATE_RELATIVE / "manifest.json").as_posix(),
            "manifest_sha256": manifest_sha,
            "build_id": request["personalization_build_id"],
            "as_of": request["as_of"],
            "event_mappings_sha256": event_mappings_sha,
            "question_index_sha256": question_index_sha,
            "canonical_review_events_sha256": expected_events_sha,
        },
        "prepare_contract": fixed_prepare_fields,
        "generation_authority": "manual_dated_nightly_only",
        "formal_write_count": 0,
    }
    _assert_answer_safe(demand)
    return demand


def build_cross_variant_demand(
    repo: str | Path,
    request_path: str | Path,
    *,
    write: bool = True,
) -> dict[str, Any]:
    if write:
        _retired_write_surface()

    root = Path(repo).expanduser().resolve()
    path, _ = _repo_file(
        root, request_path, "cross-variant demand request", max_bytes=MAX_REQUEST_BYTES
    )
    request_raw, _, _ = _load_object_snapshot(
        path, "cross-variant demand request", max_bytes=MAX_REQUEST_BYTES
    )
    request = _normalize_request(request_raw)
    demand = _evaluate_request(root, request)
    demand_sha = _sha256_bytes(_pretty_bytes(demand))
    result = {
        "status": demand["status"],
        "demand_id": demand["demand_id"],
        "demand_sha256": demand_sha,
        "route_action": demand["route_action"],
        "reason_codes": demand["reason_codes"],
        "formal_write_count": 0,
    }
    if not write:
        return {**result, "demand": demand, "idempotent_replay": False}

    demand_root = _managed_demand_root(root, create=True)
    request_copy = _managed_demand_path(
        root, "requests", f"{demand['request_sha256']}.json"
    )
    demand_path = _managed_demand_path(
        root, "demands", f"{demand['demand_id']}.json"
    )
    key_path = _managed_demand_path(
        root, "idempotency", f"{demand['idempotency_key_sha256']}.json"
    )
    idempotency = {
        "schema": IDEMPOTENCY_SCHEMA,
        "idempotency_key_sha256": demand["idempotency_key_sha256"],
        "request_sha256": demand["request_sha256"],
        "demand_id": demand["demand_id"],
        "demand_sha256": demand_sha,
        "formal_write_count": 0,
    }
    _assert_answer_safe(idempotency)
    replay = False
    lock_path = _managed_demand_path(root, ".cross-variant-demand.lock")
    with _lock(lock_path):
        _managed_demand_root(root, create=False)
        _reject_symlink_components(root, request_copy, "cross demand request copy")
        _reject_symlink_components(root, demand_path, "cross demand artifact")
        _reject_symlink_components(root, key_path, "cross demand idempotency record")
        if key_path.is_file():
            existing = _load_object(
                key_path, "cross-variant demand idempotency record", max_bytes=MAX_REQUEST_BYTES
            )
            if existing != idempotency:
                raise CrossVariantDemandError(
                    "idempotency key was already used for a different cross demand"
                )
            replay = True
        if request_copy.is_file():
            if _read_snapshot(
                request_copy,
                "content-addressed demand request",
                max_bytes=MAX_REQUEST_BYTES,
            )[0] != _pretty_bytes(request):
                raise CrossVariantDemandError("content-addressed request identity conflict")
        else:
            _atomic_write(request_copy, _pretty_bytes(request))
        if demand_path.is_file():
            if _read_snapshot(
                demand_path,
                "content-addressed cross demand",
                max_bytes=MAX_ARTIFACT_BYTES,
            )[0] != _pretty_bytes(demand):
                raise CrossVariantDemandError("content-addressed demand identity conflict")
        else:
            _atomic_write_json(demand_path, demand)
        if not key_path.exists():
            _atomic_write_json(key_path, idempotency)
    return {
        **result,
        "demand_ref": demand_path.relative_to(root).as_posix(),
        "request_ref": request_copy.relative_to(root).as_posix(),
        "idempotent_replay": replay,
    }


def _load_managed_demand(repo: Path, demand_id: str) -> tuple[dict[str, Any], Path, str]:
    if not DEMAND_RE.fullmatch(str(demand_id or "")):
        raise CrossVariantDemandError("cross demand ID is invalid")
    demand_path = _managed_demand_path(
        repo, "demands", f"{demand_id}.json", create_root=False
    )
    demand, demand_sha, _ = _load_object_snapshot(
        demand_path, "managed cross demand", max_bytes=MAX_ARTIFACT_BYTES
    )
    if (
        demand.get("schema") != DEMAND_SCHEMA
        or demand.get("demand_id") != demand_id
        or demand.get("answer_safe") is not True
        or demand.get("formal_write_count") != 0
    ):
        raise CrossVariantDemandError("managed cross demand is invalid")
    _assert_answer_safe(demand)
    request_path, normalized = _repo_file(
        repo, demand.get("request_ref"), "managed cross demand request", max_bytes=MAX_REQUEST_BYTES
    )
    expected_request_ref = (
        DEFAULT_DEMAND_RELATIVE
        / "requests"
        / f"{demand.get('request_sha256')}.json"
    ).as_posix()
    expected_request_path = _managed_demand_path(
        repo, "requests", f"{demand.get('request_sha256')}.json"
    )
    if (
        normalized != expected_request_ref
        or demand.get("request_ref") != expected_request_ref
        or request_path != expected_request_path.resolve()
    ):
        raise CrossVariantDemandError("managed cross demand request drifted")
    request_raw, request_sha, _ = _load_object_snapshot(
        request_path, "managed cross demand request", max_bytes=MAX_REQUEST_BYTES
    )
    if request_sha != demand.get("request_sha256"):
        raise CrossVariantDemandError("managed cross demand request drifted")
    request = _normalize_request(request_raw)
    current = _evaluate_request(repo, request)
    if current != demand:
        raise CrossVariantDemandError("cross demand inputs changed; rebuild the demand")
    return demand, demand_path, demand_sha


def _prepare_demand_locked(
    root: Path,
    *,
    demand_id: str,
    evidence_path: str | Path,
    answer_key_private_root: str | Path,
) -> dict[str, Any]:
    _retired_write_surface()

    # Historical implementation retained below for read-only source compatibility.
    demand, demand_path, demand_sha = _load_managed_demand(root, demand_id)
    if demand.get("status") != "ready_for_cross_variant":
        raise CrossVariantDemandError("only a ready cross demand can enter prepare")
    evidence_file, evidence_ref = _repo_file(
        root, evidence_path, "cross-variant authoring evidence", max_bytes=MAX_EVIDENCE_BYTES
    )
    evidence, evidence_sha, _ = _load_object_snapshot(
        evidence_file, "cross-variant authoring evidence", max_bytes=MAX_EVIDENCE_BYTES
    )
    contract = demand.get("prepare_contract")
    if not isinstance(contract, dict):
        raise CrossVariantDemandError("ready demand lacks its prepare contract")
    fixed_fields = {
        "schema": evidence.get("schema"),
        "evidence_scope": evidence.get("evidence_scope"),
        "original_formal_node_id": evidence.get("original_formal_node_id"),
        "original_source_ref": evidence.get("original_source_ref"),
        "original_source_sha256": evidence.get("original_source_sha256"),
        "cross_projection_ref": evidence.get("cross_projection_ref"),
        "cross_projection_sha256": evidence.get("cross_projection_sha256"),
        "verified_cross_edge_id": evidence.get("verified_cross_edge_id"),
        "targets": _canonical_targets(evidence.get("targets")),
        "formal_write_count": evidence.get("formal_write_count"),
    }
    if fixed_fields != contract:
        raise CrossVariantDemandError("authoring evidence does not match the cross demand")
    try:
        prepared = prepare_cross_variant(
            root,
            evidence_file,
            answer_key_private_root=answer_key_private_root,
        )
    except CrossVariantPipelineError as exc:
        raise CrossVariantDemandError("cross-variant prepare pipeline rejected evidence") from exc
    receipt = {
        "schema": PREPARE_RECEIPT_SCHEMA,
        "demand_id": demand_id,
        "demand_ref": demand_path.relative_to(root).as_posix(),
        "demand_sha256": demand_sha,
        "authoring_evidence_ref": evidence_ref,
        "authoring_evidence_sha256": evidence_sha,
        "generation_request_ref": prepared["generation_request_ref"],
        "generation_request_sha256": prepared["generation_request_sha256"],
        "formal_write_count": 0,
    }
    _assert_answer_safe(receipt)
    receipt_path = _managed_demand_path(
        root,
        "prepared",
        demand_id,
        f"{prepared['generation_request_sha256']}.json",
    )
    demand_lock = _managed_demand_path(root, ".cross-variant-demand.lock")
    with _lock(demand_lock):
        _managed_demand_root(root, create=False)
        _reject_symlink_components(root, receipt_path, "cross demand prepare receipt")
        if receipt_path.is_file():
            if _load_object(
                receipt_path, "cross demand prepare receipt", max_bytes=MAX_REQUEST_BYTES
            ) != receipt:
                raise CrossVariantDemandError("cross demand prepare receipt conflict")
        else:
            _atomic_write_json(receipt_path, receipt)
    return {
        "status": "prepared",
        "demand_id": demand_id,
        "prepare_receipt_ref": receipt_path.relative_to(root).as_posix(),
        "prepare_receipt_sha256": _sha256_file(receipt_path),
        **prepared,
        "formal_write_count": 0,
    }


def prepare_demand(
    repo: str | Path,
    *,
    demand_id: str,
    evidence_path: str | Path,
    answer_key_private_root: str | Path,
) -> dict[str, Any]:
    _retired_write_surface()

    # Historical implementation retained below for read-only source compatibility.
    root = Path(repo).expanduser().resolve()
    # Global order for this bridge is personalization hot lock first, then the
    # downstream prepare lock, and only afterwards the demand receipt lock.  No
    # demand lock is held while acquiring the personalization lock.
    state = _managed_repo_state_root(
        root, DEFAULT_STATE_RELATIVE, "personalization state root"
    )
    hot_lock = state / ".hot-path.lock"
    _reject_symlink_components(root, hot_lock, "personalization hot-path lock")
    with _lock(hot_lock):
        return _prepare_demand_locked(
            root,
            demand_id=demand_id,
            evidence_path=evidence_path,
            answer_key_private_root=answer_key_private_root,
        )


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo", type=Path, default=Path.cwd())
    commands = parser.add_subparsers(dest="command", required=True)
    plan = commands.add_parser("plan")
    plan.add_argument("--request", required=True, type=Path)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        result = build_cross_variant_demand(args.repo, args.request, write=False)
    except CrossVariantDemandError as exc:
        print(
            json.dumps(
                {
                    "status": "error",
                    "error": str(exc),
                    "formal_write_count": 0,
                },
                ensure_ascii=False,
                sort_keys=True,
            ),
            file=sys.stderr,
        )
        return 1
    print(json.dumps(result, ensure_ascii=False, sort_keys=True, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
