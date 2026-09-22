#!/usr/bin/env python3
"""Prepare and register independently verified 408 variant question surfaces.

This module never derives an answer, option key, or distractor from a formal
node's thin safe summary.  Authoring starts only from a verified original
question manifest plus a separately hashed authoritative solution source.  A
different verifier must then attest the authored question against that sealed
evidence before a question-only manifest can enter the personalization index.
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
import stat
import sys
import tempfile
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator

from linked_practice_source_408 import (
    LinkedPracticeSourceError,
    PROTECTED_VARIANT_TEXT_RE,
    SHA256_RE,
    validate_source_for_practice,
)


GENERATION_REQUEST_SCHEMA = "variant_generation_request_v1"
AUTHORING_EVIDENCE_SCHEMA = "variant_authoring_evidence_v1"
ACTOR_IDENTITY_SCHEMA = "variant_actor_execution_identity_v1"
ATTESTATION_SCHEMA = "variant_independent_attestation_v1"
VARIANT_MANIFEST_SCHEMA = "variant_question_source_manifest_v1"
VARIANT_RECEIPT_SCHEMA = "variant_answer_verification_receipt_v1"
VARIANT_CATALOG_SCHEMA = "variant_source_catalog_v1"
FORMAL_NODE_RE = re.compile(r"^(?:DS|CO|OS|CN)_(?:\d{4}|UNK)_\d{3}$")
POINT_RE = re.compile(r"^(?:DS|CO|OS|CN)\d{2}-\d{2}$")
SAFE_ACTOR_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{2,80}$")
SAFE_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}(?:T[^\s]+)?$")
SOURCE_ID_RE = re.compile(r"^VAR-[A-Za-z0-9][A-Za-z0-9-]{8,40}$")
REQUEST_ID_RE = re.compile(r"^VRQ-[0-9A-F]{20}$")
ALLOWED_SURFACE_SUFFIXES = {".md", ".png", ".jpg", ".jpeg", ".webp"}
DEFAULT_STATE_RELATIVE = Path("wiki/study_vaults/408-full/state/personalization")
DEFAULT_DURABLE_RELATIVE = Path(
    "wiki/study_vaults/408-full/state/linked-practice"
)
CATALOG_FILENAME = "variant-catalog.json"
PREPARE_KEY_FILENAME = ".variant-pipeline-prepare.key"
PREPARE_KEY_BYTES = 32


class VariantPipelineError(RuntimeError):
    """A stable failure in the answer-safe variant authoring pipeline."""


def _retired_write_surface() -> None:
    raise VariantPipelineError(
        "variant_source_writes_retired_read_only_compatibility"
    )


def _json_bytes(value: Any, *, pretty: bool = True) -> bytes:
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


def _atomic_write_bytes(path: Path, data: bytes) -> str:
    _retired_write_surface()

    # Historical implementation retained below for read-only source compatibility.
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


def _atomic_write_json(path: Path, value: Any) -> str:
    return _atomic_write_bytes(path, _json_bytes(value))


def _canonical_digest(value: Any) -> str:
    return _sha256_bytes(_json_bytes(value, pretty=False))


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
        raise VariantPipelineError(f"{label} is unreadable") from exc
    if not isinstance(value, dict):
        raise VariantPipelineError(f"{label} must be a JSON object")
    return value


def _inside(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
        return True
    except ValueError:
        return False


def _repo_file(repo: Path, locator: object, label: str) -> tuple[Path, str]:
    raw = str(locator or "").strip()
    candidate = Path(raw).expanduser()
    if not raw or "#" in raw or ".." in candidate.parts:
        raise VariantPipelineError(f"{label} locator is invalid")
    unresolved = candidate if candidate.is_absolute() else repo / candidate
    if unresolved.is_symlink():
        raise VariantPipelineError(f"{label} must not be a symlink")
    path = unresolved.resolve()
    if not path.is_file() or path.is_symlink() or path.stat().st_size <= 0:
        raise VariantPipelineError(f"{label} must be one durable regular file")
    try:
        normalized = path.relative_to(repo).as_posix()
    except ValueError:
        normalized = str(path)
    return path, normalized


def _require_digest(path: Path, expected: object, label: str) -> str:
    digest = str(expected or "")
    if not SHA256_RE.fullmatch(digest) or _sha256_file(path) != digest:
        raise VariantPipelineError(f"{label} sha256 mismatch")
    return digest


def _state_roots(repo: Path) -> tuple[Path, Path]:
    return (
        (repo / DEFAULT_STATE_RELATIVE).resolve(),
        (repo / DEFAULT_DURABLE_RELATIVE).resolve(),
    )


def _managed_regular_file(path: Path, managed_root: Path, label: str) -> None:
    """Reject aliases and symlinked components inside a managed provenance tree."""

    try:
        relative = path.relative_to(managed_root)
    except ValueError as exc:
        raise VariantPipelineError(f"{label} is outside its managed directory") from exc
    cursor = managed_root
    if cursor.is_symlink():
        raise VariantPipelineError(f"{label} managed directory must not be a symlink")
    for part in relative.parts:
        cursor = cursor / part
        if cursor.is_symlink():
            raise VariantPipelineError(f"{label} must not use symlinked components")
    if not path.is_file() or not stat.S_ISREG(path.stat().st_mode):
        raise VariantPipelineError(f"{label} must be a managed regular file")


def _prepare_key(durable: Path, *, create: bool) -> bytes:
    """Load the local issuance key; only prepare is allowed to create it."""

    path = durable / PREPARE_KEY_FILENAME
    if path.is_symlink():
        raise VariantPipelineError("prepared-request issuance key is unsafe")
    if create and not path.exists():
        descriptor: int | None = None
        try:
            descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            payload = secrets.token_bytes(PREPARE_KEY_BYTES)
            os.write(descriptor, payload)
            os.fsync(descriptor)
        except FileExistsError:
            pass
        finally:
            if descriptor is not None:
                os.close(descriptor)
    try:
        info = path.lstat()
        payload = path.read_bytes()
    except OSError as exc:
        raise VariantPipelineError("prepared-request issuance key is unavailable") from exc
    if (
        stat.S_ISLNK(info.st_mode)
        or not stat.S_ISREG(info.st_mode)
        or info.st_mode & 0o077
        or len(payload) != PREPARE_KEY_BYTES
    ):
        raise VariantPipelineError("prepared-request issuance key is unsafe")
    return payload


def _actor_identity(raw: object, *, role: str, label: str) -> dict[str, str]:
    if not isinstance(raw, dict) or set(raw) != {
        "schema",
        "actor_id",
        "execution_id",
        "role",
    }:
        raise VariantPipelineError(f"{label} identity is incomplete")
    identity = {
        "schema": str(raw.get("schema") or ""),
        "actor_id": str(raw.get("actor_id") or ""),
        "execution_id": str(raw.get("execution_id") or ""),
        "role": str(raw.get("role") or ""),
    }
    if (
        identity["schema"] != ACTOR_IDENTITY_SCHEMA
        or identity["role"] != role
        or not SAFE_ACTOR_RE.fullmatch(identity["actor_id"])
        or not SAFE_ACTOR_RE.fullmatch(identity["execution_id"])
    ):
        raise VariantPipelineError(f"{label} identity is invalid")
    return identity


def _identity_pair(evidence: dict[str, Any]) -> tuple[dict[str, str], dict[str, str]]:
    author = _actor_identity(
        evidence.get("author_identity"), role="variant_author", label="author"
    )
    verifier = _actor_identity(
        evidence.get("verifier_identity"), role="variant_verifier", label="verifier"
    )
    if (
        author["actor_id"] == verifier["actor_id"]
        or author["execution_id"] == verifier["execution_id"]
    ):
        raise VariantPipelineError(
            "author and verifier must be distinct actors and executions"
        )
    return author, verifier


def _unsigned_request_from_evidence(
    root: Path,
    evidence_file: Path,
    evidence_ref: str,
) -> dict[str, Any]:
    """Rebuild the complete request body from currently verified source evidence."""

    evidence = _load_object(evidence_file, "authoring evidence")
    anchor = str(evidence.get("anchor_formal_node_id") or "")
    point = str(evidence.get("knowledge_point") or "")
    if (
        evidence.get("schema") != AUTHORING_EVIDENCE_SCHEMA
        or evidence.get("evidence_scope")
        != "full_question_and_authoritative_solution"
        or evidence.get("authoring_authority_verified") is not True
        or not FORMAL_NODE_RE.fullmatch(anchor)
        or not POINT_RE.fullmatch(point)
        or point[:2] != anchor[:2]
    ):
        raise VariantPipelineError("authoring evidence is incomplete or thin")
    author_identity, verifier_identity = _identity_pair(evidence)
    try:
        original = validate_source_for_practice(
            root,
            evidence.get("anchor_source_ref"),
            evidence.get("anchor_source_sha256"),
            practice_mode="original",
            formal_node_id=anchor,
        )
    except LinkedPracticeSourceError as exc:
        raise VariantPipelineError("anchor question source is not verified") from exc
    solution_path, solution_ref = _repo_file(
        root, evidence.get("sealed_solution_ref"), "sealed solution evidence"
    )
    solution_sha = _require_digest(
        solution_path,
        evidence.get("sealed_solution_sha256"),
        "sealed solution evidence",
    )
    if solution_path == original["path"]:
        raise VariantPipelineError("question and solution evidence must be distinct")
    evidence_sha = _sha256_file(evidence_file)
    author_identity_sha = _canonical_digest(author_identity)
    verifier_identity_sha = _canonical_digest(verifier_identity)
    request_material = {
        "anchor_formal_node_id": anchor,
        "anchor_source_sha256": original["source_sha256"],
        "sealed_solution_sha256": solution_sha,
        "knowledge_point": point,
        "mechanism_key": str(evidence.get("mechanism_key") or ""),
        "review_unit_id": evidence.get("review_unit_id"),
        "author_identity_sha256": author_identity_sha,
        "verifier_identity_sha256": verifier_identity_sha,
        "evidence_sha256": evidence_sha,
    }
    request_id = "VRQ-" + _sha256_bytes(_json_bytes(request_material, pretty=False))[:20].upper()
    return {
        "schema": GENERATION_REQUEST_SCHEMA,
        "request_id": request_id,
        "status": "awaiting_independent_authoring_and_verification",
        "anchor_formal_node_id": anchor,
        "anchor_source_ref": original["source_ref"],
        "anchor_source_sha256": original["source_sha256"],
        "sealed_solution_ref": solution_ref,
        "sealed_solution_sha256": solution_sha,
        "authoring_evidence_ref": evidence_ref,
        "authoring_evidence_sha256": evidence_sha,
        "knowledge_point": point,
        "mechanism_key": str(evidence.get("mechanism_key") or ""),
        "review_unit_id": evidence.get("review_unit_id"),
        "author_id": author_identity["actor_id"],
        "author_execution_id": author_identity["execution_id"],
        "author_identity": author_identity,
        "author_identity_sha256": author_identity_sha,
        "verifier_id": verifier_identity["actor_id"],
        "verifier_execution_id": verifier_identity["execution_id"],
        "verifier_identity": verifier_identity,
        "verifier_identity_sha256": verifier_identity_sha,
        "generation_constraints": {
            "same_target": True,
            "changed_context_or_parameters": True,
            "answer_format": "A/B/C/D",
            "question_surface_only": True,
            "no_answer_or_solution_in_surface": True,
            "must_not_infer_from_safe_summary": True,
        },
        "formal_write_count": 0,
    }


def _signed_request(unsigned_request: dict[str, Any], key: bytes) -> dict[str, Any]:
    payload = dict(unsigned_request)
    payload["prepare_key_id"] = _sha256_bytes(key)
    payload["prepare_signature"] = hmac.new(
        key,
        _json_bytes(payload, pretty=False),
        hashlib.sha256,
    ).hexdigest()
    return payload


def prepare_generation(
    repo: str | Path,
    evidence_path: str | Path,
) -> dict[str, Any]:
    """Create a durable, tool-issued authoring package without solution text."""

    _retired_write_surface()

    # Historical implementation retained below for read-only source compatibility.
    root = Path(repo).expanduser().resolve()
    evidence_file, evidence_ref = _repo_file(root, evidence_path, "authoring evidence")
    unsigned_request = _unsigned_request_from_evidence(
        root, evidence_file, evidence_ref
    )
    _, durable = _state_roots(root)
    with _lock(durable / ".variant-pipeline.lock"):
        key = _prepare_key(durable, create=True)
        request = _signed_request(unsigned_request, key)
        request_sha = _sha256_bytes(_json_bytes(request))
        request_id = str(request["request_id"])
        request_path = (
            durable
            / "variant-work"
            / request_id
            / "requests"
            / f"{request_sha}.json"
        )
        if request_path.is_file():
            existing = _load_object(request_path, "generation request")
            if existing != request:
                raise VariantPipelineError("generation request identity conflict")
        else:
            _atomic_write_json(request_path, request)
        if _sha256_file(request_path) != request_sha:
            raise VariantPipelineError("prepared generation request is not content-addressed")
    return {
        "status": "prepared",
        "request_id": request_id,
        "generation_request_ref": request_path.relative_to(root).as_posix(),
        "generation_request_sha256": request_sha,
        "author_identity_sha256": request["author_identity_sha256"],
        "verifier_identity_sha256": request["verifier_identity_sha256"],
        "formal_write_count": 0,
    }


def validate_authoring_evidence(
    repo: str | Path,
    evidence_path: str | Path,
) -> dict[str, Any]:
    """Deep-check protected authoring evidence and return only safe metadata.

    The demand planner uses this read-only adapter to distinguish a genuinely
    preparable request from a missing-source gap.  It deliberately omits the
    sealed solution locator while retaining its hash commitment.
    """

    root = Path(repo).expanduser().resolve()
    evidence_file, evidence_ref = _repo_file(
        root, evidence_path, "authoring evidence"
    )
    request = _unsigned_request_from_evidence(root, evidence_file, evidence_ref)
    return {
        "status": "verified_authoring_evidence",
        "evidence_ref": evidence_ref,
        "evidence_sha256": request["authoring_evidence_sha256"],
        "anchor_formal_node_id": request["anchor_formal_node_id"],
        "anchor_source_ref": request["anchor_source_ref"],
        "anchor_source_sha256": request["anchor_source_sha256"],
        "sealed_solution_sha256": request["sealed_solution_sha256"],
        "knowledge_point": request["knowledge_point"],
        "mechanism_key": request["mechanism_key"],
        "review_unit_id": request.get("review_unit_id"),
        "author_identity_sha256": request["author_identity_sha256"],
        "verifier_identity_sha256": request["verifier_identity_sha256"],
        "formal_write_count": 0,
    }


def _validate_prepared_request(
    root: Path,
    request_path: Path,
) -> tuple[dict[str, Any], str, str]:
    """Require an exact, live, prepare-issued and content-addressed request."""

    _, durable = _state_roots(root)
    work_root = durable / "variant-work"
    _managed_regular_file(request_path, work_root, "generation request")
    request = _load_object(request_path, "generation request")
    request_id = str(request.get("request_id") or "")
    request_sha = _sha256_file(request_path)
    canonical_path = (
        work_root / request_id / "requests" / f"{request_sha}.json"
    ).resolve()
    if (
        not REQUEST_ID_RE.fullmatch(request_id)
        or request_path != canonical_path
        or request.get("schema") != GENERATION_REQUEST_SCHEMA
    ):
        raise VariantPipelineError(
            "generation request was not persisted by prepare-generation"
        )
    evidence_file, evidence_ref = _repo_file(
        root, request.get("authoring_evidence_ref"), "authoring evidence"
    )
    _require_digest(
        evidence_file,
        request.get("authoring_evidence_sha256"),
        "authoring evidence",
    )
    unsigned_request = _unsigned_request_from_evidence(
        root, evidence_file, evidence_ref
    )
    key = _prepare_key(durable, create=False)
    expected = _signed_request(unsigned_request, key)
    if (
        request != expected
        or request.get("prepare_key_id") != _sha256_bytes(key)
        or not hmac.compare_digest(
            str(request.get("prepare_signature") or ""),
            str(expected.get("prepare_signature") or ""),
        )
        or _sha256_bytes(_json_bytes(expected)) != request_sha
    ):
        raise VariantPipelineError(
            "generation request is not an authentic current prepare artifact"
        )
    return request, request_sha, request_path.relative_to(root).as_posix()


def _authoring_commitment(
    request: dict[str, Any], *, request_sha: str, surface_sha: str
) -> str:
    return _canonical_digest(
        {
            "schema": "variant_authoring_chain_commitment_v1",
            "generation_request_id": request.get("request_id"),
            "generation_request_sha256": request_sha,
            "anchor_formal_node_id": request.get("anchor_formal_node_id"),
            "anchor_source_sha256": request.get("anchor_source_sha256"),
            "authoring_evidence_sha256": request.get("authoring_evidence_sha256"),
            "sealed_solution_sha256": request.get("sealed_solution_sha256"),
            "author_identity_sha256": request.get("author_identity_sha256"),
            "verifier_identity_sha256": request.get("verifier_identity_sha256"),
            "question_surface_sha256": surface_sha,
        }
    )


def _validated_attestation(
    attestation: dict[str, Any],
    *,
    request: dict[str, Any],
    request_sha: str,
    surface_sha: str,
) -> None:
    author = str(request.get("author_id") or "")
    verifier = str(request.get("verifier_id") or "")
    author_execution = str(request.get("author_execution_id") or "")
    verifier_execution = str(request.get("verifier_execution_id") or "")
    commitment = _authoring_commitment(
        request, request_sha=request_sha, surface_sha=surface_sha
    )
    if (
        attestation.get("schema") != ATTESTATION_SCHEMA
        or attestation.get("generation_request_id") != request.get("request_id")
        or attestation.get("generation_request_sha256") != request_sha
        or attestation.get("author_id") != author
        or attestation.get("author_execution_id") != author_execution
        or attestation.get("author_identity_sha256")
        != request.get("author_identity_sha256")
        or attestation.get("verifier_id") != verifier
        or attestation.get("verifier_execution_id") != verifier_execution
        or attestation.get("verifier_identity_sha256")
        != request.get("verifier_identity_sha256")
        or not SAFE_ACTOR_RE.fullmatch(verifier)
        or verifier == author
        or verifier_execution == author_execution
        or attestation.get("anchor_source_sha256")
        != request.get("anchor_source_sha256")
        or attestation.get("authoring_evidence_sha256")
        != request.get("authoring_evidence_sha256")
        or attestation.get("question_surface_sha256") != surface_sha
        or attestation.get("sealed_solution_sha256")
        != request.get("sealed_solution_sha256")
        or attestation.get("authoring_chain_commitment_sha256") != commitment
        or attestation.get("answer_isolated") is not True
        or attestation.get("unique_answer_verified") is not True
        or attestation.get("distractors_verified") is not True
        or attestation.get("same_target_verified") is not True
        or attestation.get("changed_surface_verified") is not True
        or not SAFE_DATE_RE.fullmatch(str(attestation.get("verified_at") or ""))
    ):
        raise VariantPipelineError("independent verifier attestation is invalid")


def _variant_ids(
    request: dict[str, Any],
    *,
    request_sha: str,
    surface_sha: str,
    attestation_sha: str,
) -> tuple[str, str]:
    identity = {
        "request_sha256": request_sha,
        "surface_sha256": surface_sha,
        "attestation_sha256": attestation_sha,
        "author_identity_sha256": request.get("author_identity_sha256"),
        "verifier_identity_sha256": request.get("verifier_identity_sha256"),
    }
    suffix = _sha256_bytes(_json_bytes(identity, pretty=False))[:16].upper()
    subject = str(request.get("anchor_formal_node_id") or "")[:2]
    return f"VAR-{subject}-{suffix}", f"VVR-{subject}-{suffix}"


def _receipt_payload(
    *,
    request: dict[str, Any],
    request_ref: str,
    request_sha: str,
    surface_sha: str,
    attestation: dict[str, Any],
    attestation_ref: str,
    attestation_sha: str,
) -> dict[str, Any]:
    source_id, receipt_id = _variant_ids(
        request,
        request_sha=request_sha,
        surface_sha=surface_sha,
        attestation_sha=attestation_sha,
    )
    return {
        "schema": VARIANT_RECEIPT_SCHEMA,
        "receipt_id": receipt_id,
        "source_id": source_id,
        "anchor_formal_node_id": request["anchor_formal_node_id"],
        "answer_isolated": True,
        "unique_answer_verified": True,
        "distractors_verified": True,
        "same_target_verified": True,
        "changed_surface_verified": True,
        "question_asset_sha256": [surface_sha],
        "generation_request_ref": request_ref,
        "generation_request_sha256": request_sha,
        "attestation_ref": attestation_ref,
        "attestation_sha256": attestation_sha,
        "prepare_key_id": request["prepare_key_id"],
        "author_id": request["author_id"],
        "author_execution_id": request["author_execution_id"],
        "author_identity_sha256": request["author_identity_sha256"],
        "verifier_id": request["verifier_id"],
        "verifier_execution_id": request["verifier_execution_id"],
        "verifier_identity_sha256": request["verifier_identity_sha256"],
        "anchor_source_ref": request["anchor_source_ref"],
        "anchor_source_sha256": request["anchor_source_sha256"],
        "authoring_evidence_ref": request["authoring_evidence_ref"],
        "authoring_evidence_sha256": request["authoring_evidence_sha256"],
        "sealed_solution_ref": request["sealed_solution_ref"],
        "sealed_solution_sha256": request["sealed_solution_sha256"],
        "authoring_chain_commitment_sha256": attestation[
            "authoring_chain_commitment_sha256"
        ],
        "verified_at": attestation["verified_at"],
        "formal_write_count": 0,
    }


def _validate_published_chain(
    root: Path,
    manifest: dict[str, Any],
    receipt: dict[str, Any],
) -> None:
    assets = manifest.get("question_assets")
    if not isinstance(assets, list) or len(assets) != 1 or not isinstance(assets[0], dict):
        raise VariantPipelineError("published variant must contain one question surface")
    surface_sha = str(assets[0].get("sha256") or "")
    if not SHA256_RE.fullmatch(surface_sha):
        raise VariantPipelineError("published variant surface hash is invalid")
    request_path, _ = _repo_file(
        root, receipt.get("generation_request_ref"), "generation request"
    )
    request, request_sha, request_ref = _validate_prepared_request(root, request_path)
    attestation_path, attestation_ref = _repo_file(
        root, receipt.get("attestation_ref"), "verifier attestation"
    )
    attestation_sha = _require_digest(
        attestation_path, receipt.get("attestation_sha256"), "verifier attestation"
    )
    attestation = _load_object(attestation_path, "verifier attestation")
    _validated_attestation(
        attestation,
        request=request,
        request_sha=request_sha,
        surface_sha=surface_sha,
    )
    expected = _receipt_payload(
        request=request,
        request_ref=request_ref,
        request_sha=request_sha,
        surface_sha=surface_sha,
        attestation=attestation,
        attestation_ref=attestation_ref,
        attestation_sha=attestation_sha,
    )
    if receipt != expected:
        raise VariantPipelineError("variant receipt does not match its prepared chain")
    if (
        manifest.get("source_id") != expected["source_id"]
        or manifest.get("anchor_formal_node_id")
        != request.get("anchor_formal_node_id")
        or manifest.get("knowledge_point") != request.get("knowledge_point")
        or manifest.get("mechanism_key") != request.get("mechanism_key")
        or manifest.get("review_unit_id") != request.get("review_unit_id")
        or manifest.get("verified_at") != attestation.get("verified_at")
    ):
        raise VariantPipelineError("variant manifest does not match its prepared chain")


def _manifest_entry(
    root: Path,
    manifest_path: Path,
    payload: dict[str, Any],
) -> dict[str, Any]:
    anchor = str(payload.get("anchor_formal_node_id") or "")
    source_id = str(payload.get("source_id") or "")
    if (
        payload.get("schema") != VARIANT_MANIFEST_SCHEMA
        or not FORMAL_NODE_RE.fullmatch(anchor)
        or not SOURCE_ID_RE.fullmatch(source_id)
    ):
        raise VariantPipelineError("variant manifest identity is invalid")
    declared_paths = {manifest_path.resolve()}
    for asset in payload.get("question_assets") or []:
        if isinstance(asset, dict):
            declared_paths.add(
                (manifest_path.parent / str(asset.get("path") or "")).resolve()
            )
    receipt_spec = payload.get("verification_receipt") or {}
    if isinstance(receipt_spec, dict):
        declared_paths.add(
            (manifest_path.parent / str(receipt_spec.get("path") or "")).resolve()
        )
    actual_paths = {
        path.resolve()
        for path in manifest_path.parent.rglob("*")
        if path.is_file() and not path.is_symlink()
    }
    if actual_paths != declared_paths:
        raise VariantPipelineError(
            "durable variant source directory contains undeclared material"
        )
    try:
        source_ref = manifest_path.relative_to(root).as_posix()
    except ValueError as exc:
        raise VariantPipelineError("variant manifest must stay inside the repository") from exc
    source_sha = _sha256_file(manifest_path)
    try:
        validated = validate_source_for_practice(
            root,
            source_ref,
            source_sha,
            practice_mode="variant",
            variant_of_formal_node_id=anchor,
        )
    except LinkedPracticeSourceError as exc:
        raise VariantPipelineError("variant source manifest failed deep validation") from exc
    receipt = _load_object(
        manifest_path.parent / str((payload.get("verification_receipt") or {}).get("path") or ""),
        "variant verification receipt",
    )
    _validate_published_chain(root, payload, receipt)
    if (
        receipt.get("author_id") == receipt.get("verifier_id")
        or receipt.get("same_target_verified") is not True
        or receipt.get("changed_surface_verified") is not True
        or not SHA256_RE.fullmatch(str(receipt.get("generation_request_sha256") or ""))
        or not SHA256_RE.fullmatch(str(receipt.get("attestation_sha256") or ""))
    ):
        raise VariantPipelineError("variant receipt lacks independent provenance")
    return {
        "source_id": source_id,
        "anchor_formal_node_id": anchor,
        "source_ref": validated["source_ref"],
        "source_sha256": source_sha,
        "knowledge_point": payload.get("knowledge_point"),
        "mechanism_key": payload.get("mechanism_key"),
        "review_unit_id": payload.get("review_unit_id"),
        "verified_at": payload.get("verified_at"),
        "verification_receipt_id": validated.get("verification_receipt_id"),
        "verification_receipt_sha256": str(
            (payload.get("verification_receipt") or {}).get("sha256") or ""
        ),
        "status": "verified_ready",
    }


def rebuild_variant_catalog(
    repo: str | Path,
) -> dict[str, Any]:
    """Rebuild the complete catalog from durable public variant manifests."""

    root = Path(repo).expanduser().resolve()
    _, durable = _state_roots(root)
    source_root = durable / "variant-sources"
    entries: list[dict[str, Any]] = []
    if source_root.is_dir():
        for manifest_path in sorted(source_root.glob("VAR-*/manifest.json")):
            if manifest_path.is_symlink():
                raise VariantPipelineError("variant manifest must not be a symlink")
            entries.append(
                _manifest_entry(
                    root,
                    manifest_path,
                    _load_object(manifest_path, "variant manifest"),
                )
            )
    entries.sort(key=lambda item: (item["anchor_formal_node_id"], item["source_id"]))
    source_ids = [entry["source_id"] for entry in entries]
    if len(source_ids) != len(set(source_ids)):
        raise VariantPipelineError("variant source_id collision")
    by_anchor: dict[str, list[str]] = {}
    for entry in entries:
        by_anchor.setdefault(entry["anchor_formal_node_id"], []).append(entry["source_id"])
    return {
        "schema": VARIANT_CATALOG_SCHEMA,
        "kind": "verified_variant_source_catalog",
        "formal_write_count": 0,
        "entry_count": len(entries),
        "entries": entries,
        "by_anchor": dict(sorted(by_anchor.items())),
    }


def validate_variant_catalog(
    repo: str | Path,
    payload: dict[str, Any],
) -> dict[str, Any]:
    try:
        formal_write_count = int(payload.get("formal_write_count", -1))
    except (AttributeError, TypeError, ValueError) as exc:
        raise VariantPipelineError(
            "variant catalog violates its derived boundary"
        ) from exc
    if (
        not isinstance(payload, dict)
        or payload.get("schema") != VARIANT_CATALOG_SCHEMA
        or formal_write_count != 0
    ):
        raise VariantPipelineError("variant catalog violates its derived boundary")
    rebuilt = rebuild_variant_catalog(repo)
    if _json_bytes(payload, pretty=False) != _json_bytes(rebuilt, pretty=False):
        raise VariantPipelineError("variant catalog is not a complete current rebuild")
    return rebuilt


def _publish_catalog(root: Path) -> tuple[dict[str, Any], str]:
    _retired_write_surface()

    # Historical implementation retained below for read-only source compatibility.
    state, _ = _state_roots(root)
    manifest_path = state / "manifest.json"
    if not manifest_path.is_file():
        raise VariantPipelineError("personalization manifest must be built first")
    catalog = rebuild_variant_catalog(root)
    catalog_path = state / CATALOG_FILENAME
    catalog_sha = _atomic_write_json(catalog_path, catalog)
    manifest = _load_object(manifest_path, "personalization manifest")
    try:
        formal_write_count = int(manifest.get("formal_write_count", -1))
    except (TypeError, ValueError) as exc:
        raise VariantPipelineError(
            "personalization manifest violates its derived boundary"
        ) from exc
    if manifest.get("answer_safe") is not True or formal_write_count != 0:
        raise VariantPipelineError("personalization manifest violates its derived boundary")
    manifest.setdefault("artifact_hashes", {})[CATALOG_FILENAME] = catalog_sha
    manifest.setdefault("counts", {})["verified_variant_sources"] = catalog["entry_count"]
    manifest.setdefault("independent_projections", {})["variant_source_catalog"] = {
        "file": CATALOG_FILENAME,
        "included_in_this_build": True,
    }
    manifest["variant_catalog_sha256"] = catalog_sha
    manifest.pop("last_personalization_selection", None)
    runtime_material = {
        "build_id": manifest.get("build_id"),
        "learner": (manifest.get("artifact_hashes") or {}).get("learner_state.json"),
        "questions": (manifest.get("artifact_hashes") or {}).get("question_index.json"),
        "variants": catalog_sha,
    }
    manifest["runtime_projection_id"] = _sha256_bytes(
        _json_bytes(runtime_material, pretty=False)
    )
    _atomic_write_json(manifest_path, manifest)
    return catalog, catalog_sha


def finalize_verified_variant(
    repo: str | Path,
    generation_request_path: str | Path,
    question_surface_path: str | Path,
    attestation_path: str | Path,
) -> dict[str, Any]:
    """Publish only after a second actor attests the answer and distractors."""

    _retired_write_surface()

    # Historical implementation retained below for read-only source compatibility.
    root = Path(repo).expanduser().resolve()
    request_path, _ = _repo_file(
        root, generation_request_path, "generation request"
    )
    surface_path, _ = _repo_file(root, question_surface_path, "question surface")
    attestation_file, attestation_ref = _repo_file(
        root, attestation_path, "verifier attestation"
    )
    request, request_sha, request_ref = _validate_prepared_request(
        root, request_path
    )
    attestation = _load_object(attestation_file, "verifier attestation")
    if (
        not FORMAL_NODE_RE.fullmatch(str(request.get("anchor_formal_node_id") or ""))
        or surface_path.suffix.lower() not in ALLOWED_SURFACE_SUFFIXES
    ):
        raise VariantPipelineError("generation request or question surface is invalid")
    surface_sha = _sha256_file(surface_path)
    _validated_attestation(
        attestation,
        request=request,
        request_sha=request_sha,
        surface_sha=surface_sha,
    )
    if surface_path.suffix.lower() == ".md":
        try:
            surface_text = surface_path.read_text(encoding="utf-8")
        except UnicodeError as exc:
            raise VariantPipelineError("question surface is not UTF-8") from exc
        if PROTECTED_VARIANT_TEXT_RE.search(surface_text):
            raise VariantPipelineError("question surface contains answer or solution material")
    anchor = str(request["anchor_formal_node_id"])
    point = str(request.get("knowledge_point") or "")
    if not POINT_RE.fullmatch(point) or point[:2] != anchor[:2]:
        raise VariantPipelineError("variant target is inconsistent with its anchor subject")
    attestation_sha = _sha256_file(attestation_file)
    receipt = _receipt_payload(
        request=request,
        request_ref=request_ref,
        request_sha=request_sha,
        surface_sha=surface_sha,
        attestation=attestation,
        attestation_ref=attestation_ref,
        attestation_sha=attestation_sha,
    )
    source_id = str(receipt["source_id"])
    state, durable = _state_roots(root)
    source_dir = durable / "variant-sources" / source_id
    asset_name = "variant-question-01" + surface_path.suffix.lower()
    asset_path = source_dir / asset_name
    receipt_path = source_dir / "verification.json"
    manifest_path = source_dir / "manifest.json"
    with _lock(durable / ".variant-pipeline.lock"):
        copied_sha = _atomic_write_bytes(asset_path, surface_path.read_bytes())
        if copied_sha != surface_sha:
            raise VariantPipelineError("question surface copy hash mismatch")
        receipt_sha = _atomic_write_json(receipt_path, receipt)
        manifest = {
            "schema": VARIANT_MANIFEST_SCHEMA,
            "source_id": source_id,
            "anchor_formal_node_id": anchor,
            "answer_isolated": True,
            "unique_answer_verified": True,
            "question_asset_count": 1,
            "question_assets": [
                {
                    "path": asset_name,
                    "sha256": surface_sha,
                    "size_bytes": asset_path.stat().st_size,
                }
            ],
            "verification_receipt": {
                "path": receipt_path.name,
                "sha256": receipt_sha,
            },
            "knowledge_point": point,
            "mechanism_key": request.get("mechanism_key"),
            "review_unit_id": request.get("review_unit_id"),
            "verified_at": attestation["verified_at"],
            "formal_write_count": 0,
        }
        manifest_sha = _atomic_write_json(manifest_path, manifest)
        # Deep validation is performed before the manifest is exposed through
        # the catalog or personalization root manifest.
        _manifest_entry(root, manifest_path, manifest)
        with _lock(state / ".hot-path.lock"):
            catalog, catalog_sha = _publish_catalog(root)
    return {
        "status": "verified_ready",
        "source_id": source_id,
        "anchor_formal_node_id": anchor,
        "source_ref": manifest_path.relative_to(root).as_posix(),
        "source_sha256": manifest_sha,
        "variant_catalog_sha256": catalog_sha,
        "verified_variant_sources": catalog["entry_count"],
        "formal_write_count": 0,
    }


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo", type=Path, default=Path.cwd())
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("inspect-catalog")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        result = rebuild_variant_catalog(args.repo.resolve())
    except VariantPipelineError as exc:
        print(json.dumps({"status": "error", "reason": str(exc)}, ensure_ascii=False))
        return 2
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    sys.exit(main())
