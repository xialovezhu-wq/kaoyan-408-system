#!/usr/bin/env python3
"""Build independently blind-verified cross-subject 408 variants.

The public artifact is deliberately question-only.  An author receives a
signed request containing an authoritative-answer hash, but not the private
answer-key locator.  A different verifier commits a blind answer before the
answer key is opened.  Only a machine equality check may create the reveal
receipt used by ``finalize``.
"""

from __future__ import annotations

import argparse
import fcntl
import hashlib
import hmac
import itertools
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

from cross_subject_candidates_408 import (
    DEFAULT_PROJECTION_RELATIVE,
    ShadowCatalogError,
    validate_projection,
)
from linked_practice_source_408 import (
    LinkedPracticeSourceError,
    SHA256_RE,
    validate_source_for_practice,
)


EVIDENCE_SCHEMA = "cross_variant_authoring_evidence_v1"
ANSWER_KEY_SCHEMA = "cross_variant_authoritative_answer_key_v1"
REQUEST_SCHEMA = "cross_variant_generation_request_v1"
PRIVATE_PREPARE_SCHEMA = "cross_variant_private_prepare_v1"
AUTHOR_SUBMISSION_SCHEMA = "cross_variant_author_surface_submission_v1"
AUTHOR_COMMIT_SCHEMA = "cross_variant_author_surface_commit_v1"
BLIND_SUBMISSION_SCHEMA = "cross_variant_blind_answer_submission_v1"
BLIND_COMMIT_SCHEMA = "cross_variant_blind_answer_commit_v1"
SEMANTIC_UNIQUENESS_SCHEMA = "cross_variant_semantic_uniqueness_evidence_v1"
REVEAL_SCHEMA = "cross_variant_machine_reveal_receipt_v1"
PUBLIC_RECEIPT_SCHEMA = "cross_variant_public_verification_receipt_v1"
PUBLIC_MANIFEST_SCHEMA = "cross_variant_question_source_manifest_v1"
CATALOG_SCHEMA = "cross_variant_catalog_v1"
DEPENDENCY_SNAPSHOT_SCHEMA = "cross_variant_target_dependency_snapshot_v1"
TARGET_PROOF_SCHEMA = "cross_variant_target_dependency_proof_v1"
EDGE_PROOF_SCHEMA = "cross_variant_edge_semantic_binding_v1"

DEFAULT_STATE_RELATIVE = Path("wiki/study_vaults/408-full/state/personalization")
DEFAULT_DURABLE_RELATIVE = Path("wiki/study_vaults/408-full/state/linked-practice")
CATALOG_FILENAME = "cross-variant-catalog.json"
PREPARE_KEY_FILENAME = ".cross-variant-prepare.key"
PREPARE_KEY_BYTES = 32
MAX_SURFACE_BYTES = 64 * 1024
MAX_ANSWER_KEY_BYTES = 64 * 1024
MAX_BLIND_SUBMISSION_BYTES = 256 * 1024
MAX_LIVE_CROSS_MANIFEST_BYTES = 1024 * 1024
MAX_LIVE_CROSS_PROJECTION_BYTES = 8 * 1024 * 1024
MAX_QUESTION_INDEX_BYTES = 8 * 1024 * 1024
MAX_LIVE_CROSS_RECORDS = 4096
MAX_DEPENDENCY_SNAPSHOT_BYTES = 256 * 1024

FORMAL_NODE_RE = re.compile(r"^(?:DS|CO|OS|CN)_(?:\d{4}|UNK)_\d{3}$")
POINT_RE = re.compile(r"^(?:DS|CO|OS|CN)\d{2}-\d{2}$")
EDGE_RE = re.compile(r"^CSR408-[0-9A-F]{16}$")
REQUEST_ID_RE = re.compile(r"^CVRQ-[0-9A-F]{24}$")
SOURCE_ID_RE = re.compile(
    r"^CVAR-(?:DS|CO|OS|CN)-(?:DS|CO|OS|CN)-[0-9A-F]{16}$"
)
ACTOR_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{2,80}$")
EXECUTION_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{7,120}$")
MECHANISM_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:/-]{2,120}$")
ANSWER_TOKEN_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:+/=-]{0,39}$")
ANSWER_LEAK_RE = re.compile(
    r"(?:标准答案|正确答案|参考答案|答案\s*(?:[:：]|是|为)|解析\s*[:：]|"
    r"正确选项|本题\s*(?:应选|选)|[A-HＡ-Ｈ]\s*项\s*(?:正确|为真)|"
    r"(?:故选|应选|正确的是)\s*[A-HＡ-Ｈ]|answer[_ -]?set|correct[_ -]?answer|"
    r"solution\s*(?::|is\b)|answer\s*(?::|is\b)|answer\s+key)",
    re.IGNORECASE,
)
ALLOWED_STRUCTURAL_CHANGES = {
    "boundary_condition_change",
    "context_and_constraint_recomposition",
    "cross_pipeline_synthesis",
    "representation_change",
    "state_transition_recomposition",
}


class CrossVariantPipelineError(RuntimeError):
    """A stable cross-variant provenance or verification failure."""


def _retired_write_surface() -> None:
    raise CrossVariantPipelineError(
        "cross_variant_writes_retired_read_only_compatibility"
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


def _canonical_digest(value: Any) -> str:
    return _sha256_bytes(_json_bytes(value, pretty=False))


def _formal_boundary_is_zero(payload: dict[str, Any]) -> bool:
    try:
        return int(payload.get("formal_write_count", -1)) == 0
    except (TypeError, ValueError):
        return False


def _inside(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
    except ValueError:
        return False
    return True


def _reject_symlink_components(path: Path, label: str) -> None:
    """Reject every existing component in one absolute lexical path."""

    if not path.is_absolute():
        raise CrossVariantPipelineError(f"{label} must be absolute")
    current = Path(path.anchor)
    for part in path.parts[1:]:
        current = current / part
        try:
            info = os.lstat(current)
        except FileNotFoundError:
            continue
        except OSError as exc:
            raise CrossVariantPipelineError(f"{label} cannot be inspected") from exc
        if stat.S_ISLNK(info.st_mode):
            raise CrossVariantPipelineError(f"{label} must not contain symlink components")


def _guard_managed_linked_path(repo: Path, path: Path, label: str) -> Path:
    durable = repo / DEFAULT_DURABLE_RELATIVE
    _reject_symlink_components(durable, "linked-practice managed root")
    _reject_symlink_components(path, label)
    resolved_durable = durable.resolve()
    resolved = path.resolve()
    if not _inside(resolved_durable, repo) or not _inside(resolved, resolved_durable):
        raise CrossVariantPipelineError(f"{label} escapes the managed repository root")
    return path


def _atomic_write_bytes(path: Path, data: bytes, *, private: bool = False) -> str:
    _retired_write_surface()

    # Historical implementation retained below for read-only source compatibility.
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        if private:
            os.chmod(temporary, 0o600)
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)
    return _sha256_bytes(data)


def _atomic_write_json(path: Path, value: Any, *, private: bool = False) -> str:
    return _atomic_write_bytes(path, _json_bytes(value), private=private)


def _atomic_write_managed_json(
    repo: Path, path: Path, value: Any, *, private: bool = False
) -> str:
    _guard_managed_linked_path(repo, path, "cross-variant managed write path")
    path.parent.mkdir(parents=True, exist_ok=True)
    _guard_managed_linked_path(repo, path, "cross-variant managed write path")
    return _atomic_write_json(path, value, private=private)


def _atomic_write_managed_bytes(repo: Path, path: Path, data: bytes) -> str:
    _guard_managed_linked_path(repo, path, "cross-variant managed write path")
    path.parent.mkdir(parents=True, exist_ok=True)
    _guard_managed_linked_path(repo, path, "cross-variant managed write path")
    return _atomic_write_bytes(path, data)


@contextmanager
def _lock(path: Path) -> Iterator[None]:
    path.parent.mkdir(parents=True, exist_ok=True)
    flags = os.O_RDWR | os.O_CREAT | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    descriptor = os.open(path, flags, 0o600)
    with os.fdopen(descriptor, "a+", encoding="utf-8") as handle:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


@contextmanager
def _pipeline_lock(repo: Path, durable: Path) -> Iterator[None]:
    path = _guard_managed_linked_path(
        repo, durable / ".cross-variant-pipeline.lock", "cross-variant pipeline lock"
    )
    with _lock(path):
        yield


def _load_object(path: Path, label: str) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise CrossVariantPipelineError(f"{label} is unreadable") from exc
    if not isinstance(value, dict):
        raise CrossVariantPipelineError(f"{label} must be a JSON object")
    return value


def _load_bounded_object(
    path: Path, label: str, *, max_bytes: int
) -> tuple[dict[str, Any], str]:
    try:
        info = path.lstat()
        if (
            stat.S_ISLNK(info.st_mode)
            or not stat.S_ISREG(info.st_mode)
            or info.st_size <= 0
            or info.st_size > max_bytes
        ):
            raise CrossVariantPipelineError(f"{label} exceeds its safe input boundary")
        raw = path.read_bytes()
        if len(raw) != info.st_size or len(raw) > max_bytes:
            raise CrossVariantPipelineError(f"{label} changed while being read")
        value = json.loads(raw.decode("utf-8"))
    except CrossVariantPipelineError:
        raise
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise CrossVariantPipelineError(f"{label} is unreadable") from exc
    if not isinstance(value, dict):
        raise CrossVariantPipelineError(f"{label} must be a JSON object")
    return value, _sha256_bytes(raw)


def _repo_file(repo: Path, locator: object, label: str) -> tuple[Path, str]:
    raw = str(locator or "").strip()
    candidate = Path(raw).expanduser()
    if not raw or "#" in raw or ".." in candidate.parts:
        raise CrossVariantPipelineError(f"{label} locator is invalid")
    unresolved = candidate if candidate.is_absolute() else repo / candidate
    if unresolved.is_symlink():
        raise CrossVariantPipelineError(f"{label} must not be a symlink")
    path = unresolved.resolve()
    if not path.is_file() or path.is_symlink() or path.stat().st_size <= 0:
        raise CrossVariantPipelineError(f"{label} must be one durable regular file")
    try:
        normalized = path.relative_to(repo).as_posix()
    except ValueError:
        normalized = str(path)
    return path, normalized


def _managed_private_root(
    repo: Path, locator: str | Path | None
) -> tuple[Path, str, str]:
    """Resolve and bind the explicitly selected answer-key private root.

    The root may live inside or outside the repository, but it must be an
    owner-only, non-symlink directory.  The returned digest binds both its
    canonical location and filesystem identity without exposing that location
    in the author-visible generation request.
    """

    raw = str(locator or "").strip()
    candidate = Path(raw).expanduser()
    if not raw:
        raise CrossVariantPipelineError(
            "authoritative answer key private root is required"
        )
    if "#" in raw or ".." in candidate.parts:
        raise CrossVariantPipelineError(
            "authoritative answer key private root locator is invalid"
        )
    unresolved = candidate if candidate.is_absolute() else repo / candidate
    _reject_symlink_components(
        unresolved.absolute(), "authoritative answer key private root path"
    )
    try:
        info = unresolved.lstat()
    except OSError as exc:
        raise CrossVariantPipelineError(
            "authoritative answer key private root is unavailable"
        ) from exc
    if (
        stat.S_ISLNK(info.st_mode)
        or not stat.S_ISDIR(info.st_mode)
        or info.st_uid != os.getuid()
        or stat.S_IMODE(info.st_mode) != 0o700
    ):
        raise CrossVariantPipelineError(
            "authoritative answer key private root must be an owner-only 0700 directory"
        )
    path = unresolved.resolve()
    try:
        normalized = path.relative_to(repo).as_posix()
    except ValueError:
        normalized = str(path)
    binding = _canonical_digest(
        {
            "schema": "cross_variant_private_root_binding_v1",
            "resolved_path": str(path),
            "device": info.st_dev,
            "inode": info.st_ino,
            "owner_uid": info.st_uid,
            "mode": stat.S_IMODE(info.st_mode),
        }
    )
    return path, normalized, binding


def _managed_private_answer_key(
    private_root: Path, locator: object
) -> tuple[Path, str]:
    """Resolve one root-relative, non-symlink, owner-only answer key."""

    raw = str(locator or "").strip()
    candidate = Path(raw)
    if (
        not raw
        or candidate.is_absolute()
        or raw.startswith("~")
        or "#" in raw
        or ".." in candidate.parts
    ):
        raise CrossVariantPipelineError(
            "authoritative answer key locator must be relative to the managed private root"
        )
    current = private_root
    parts = candidate.parts
    if not parts:
        raise CrossVariantPipelineError("authoritative answer key locator is invalid")
    try:
        for index, part in enumerate(parts):
            current = current / part
            info = current.lstat()
            if stat.S_ISLNK(info.st_mode):
                raise CrossVariantPipelineError(
                    "authoritative answer key path must not contain symlinks"
                )
            if index < len(parts) - 1 and not stat.S_ISDIR(info.st_mode):
                raise CrossVariantPipelineError(
                    "authoritative answer key parent must be a regular directory"
                )
    except CrossVariantPipelineError:
        raise
    except OSError as exc:
        raise CrossVariantPipelineError(
            "authoritative answer key is unavailable"
        ) from exc
    path = current.resolve()
    try:
        normalized = path.relative_to(private_root).as_posix()
    except ValueError as exc:
        raise CrossVariantPipelineError(
            "authoritative answer key escapes the managed private root"
        ) from exc
    info = path.lstat()
    if not stat.S_ISREG(info.st_mode) or info.st_size <= 0:
        raise CrossVariantPipelineError(
            "authoritative answer key must be one non-empty regular file"
        )
    if info.st_uid != os.getuid():
        raise CrossVariantPipelineError(
            "authoritative answer key must be owned by the current user"
        )
    if stat.S_IMODE(info.st_mode) != 0o600:
        raise CrossVariantPipelineError(
            "authoritative answer key must have exact mode 0600"
        )
    if info.st_size > MAX_ANSWER_KEY_BYTES:
        raise CrossVariantPipelineError(
            "authoritative answer key exceeds its safe input boundary"
        )
    return path, normalized


def _read_managed_private_file(path: Path, label: str) -> bytes:
    """Read a checked private file without following a final symlink."""

    flags = os.O_RDONLY
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    try:
        descriptor = os.open(path, flags)
    except OSError as exc:
        raise CrossVariantPipelineError(f"{label} is unreadable") from exc
    try:
        info = os.fstat(descriptor)
        if (
            not stat.S_ISREG(info.st_mode)
            or info.st_uid != os.getuid()
            or stat.S_IMODE(info.st_mode) != 0o600
            or info.st_size <= 0
            or info.st_size > MAX_ANSWER_KEY_BYTES
        ):
            raise CrossVariantPipelineError(f"{label} private file boundary failed")
        with os.fdopen(descriptor, "rb", closefd=False) as handle:
            raw = handle.read(MAX_ANSWER_KEY_BYTES + 1)
        if len(raw) != info.st_size or len(raw) > MAX_ANSWER_KEY_BYTES:
            raise CrossVariantPipelineError(f"{label} changed while being read")
        try:
            post = path.lstat()
        except OSError as exc:
            raise CrossVariantPipelineError(f"{label} changed while being read") from exc
        if (
            stat.S_ISLNK(post.st_mode)
            or (post.st_dev, post.st_ino, post.st_size)
            != (info.st_dev, info.st_ino, info.st_size)
        ):
            raise CrossVariantPipelineError(f"{label} changed while being read")
        return raw
    finally:
        os.close(descriptor)


def _require_digest(path: Path, expected: object, label: str) -> str:
    digest = str(expected or "")
    if not SHA256_RE.fullmatch(digest) or _sha256_file(path) != digest:
        raise CrossVariantPipelineError(f"{label} sha256 mismatch")
    return digest


def _state_roots(repo: Path) -> tuple[Path, Path, Path]:
    state = repo / DEFAULT_STATE_RELATIVE
    durable = repo / DEFAULT_DURABLE_RELATIVE
    work = durable / "cross-variant-work"
    _reject_symlink_components(state, "personalization state root")
    _guard_managed_linked_path(repo, durable, "linked-practice managed root")
    _guard_managed_linked_path(repo, work, "cross-variant private work root")
    _guard_managed_linked_path(
        repo, durable / "cross-variant-sources", "cross-variant published root"
    )
    if not _inside(state.resolve(), repo):
        raise CrossVariantPipelineError("personalization state root escapes repository")
    return state, durable, work


def _prepare_key(repo: Path, durable: Path, *, create: bool) -> bytes:
    path = durable / PREPARE_KEY_FILENAME
    _guard_managed_linked_path(repo, path, "cross-variant issuance key")
    if path.is_symlink():
        raise CrossVariantPipelineError("cross-variant issuance key is unsafe")
    if create and not path.exists():
        path.parent.mkdir(parents=True, exist_ok=True)
        descriptor: int | None = None
        try:
            descriptor = os.open(
                path,
                os.O_WRONLY
                | os.O_CREAT
                | os.O_EXCL
                | getattr(os, "O_NOFOLLOW", 0),
                0o600,
            )
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
        raise CrossVariantPipelineError("cross-variant issuance key is unavailable") from exc
    if (
        stat.S_ISLNK(info.st_mode)
        or not stat.S_ISREG(info.st_mode)
        or info.st_mode & 0o077
        or len(payload) != PREPARE_KEY_BYTES
    ):
        raise CrossVariantPipelineError("cross-variant issuance key is unsafe")
    return payload


def _identity(raw: object, *, role: str, label: str) -> dict[str, str]:
    if not isinstance(raw, dict) or set(raw) != {
        "actor_id",
        "execution_id",
        "role",
    }:
        raise CrossVariantPipelineError(f"{label} identity is incomplete")
    identity = {
        "actor_id": str(raw.get("actor_id") or ""),
        "execution_id": str(raw.get("execution_id") or ""),
        "role": str(raw.get("role") or ""),
    }
    if (
        identity["role"] != role
        or not ACTOR_RE.fullmatch(identity["actor_id"])
        or not EXECUTION_RE.fullmatch(identity["execution_id"])
    ):
        raise CrossVariantPipelineError(f"{label} identity is invalid")
    return identity


def _identity_pair(evidence: dict[str, Any]) -> tuple[dict[str, str], dict[str, str]]:
    author = _identity(
        evidence.get("author_identity"), role="cross_variant_author", label="author"
    )
    verifier = _identity(
        evidence.get("verifier_identity"),
        role="cross_variant_verifier",
        label="verifier",
    )
    if (
        author["actor_id"] == verifier["actor_id"]
        or author["execution_id"] == verifier["execution_id"]
    ):
        raise CrossVariantPipelineError(
            "author and verifier must be distinct actors and executions"
        )
    return author, verifier


def _target(raw: object, label: str) -> dict[str, str]:
    if not isinstance(raw, dict) or set(raw) != {
        "subject",
        "point_id",
        "mechanism_key",
    }:
        raise CrossVariantPipelineError(f"{label} target is incomplete")
    target = {
        "subject": str(raw.get("subject") or ""),
        "point_id": str(raw.get("point_id") or ""),
        "mechanism_key": str(raw.get("mechanism_key") or ""),
    }
    if (
        target["subject"] not in {"DS", "CO", "OS", "CN"}
        or not POINT_RE.fullmatch(target["point_id"])
        or target["point_id"][:2] != target["subject"]
        or not MECHANISM_RE.fullmatch(target["mechanism_key"])
    ):
        raise CrossVariantPipelineError(f"{label} target is invalid")
    return target


def _canonical_targets(raw: object) -> list[dict[str, str]]:
    if not isinstance(raw, list) or len(raw) != 2:
        raise CrossVariantPipelineError("exactly two cross-subject targets are required")
    targets = [_target(item, f"target {index}") for index, item in enumerate(raw, 1)]
    if len({item["subject"] for item in targets}) != 2:
        raise CrossVariantPipelineError("cross variant must use two distinct subjects")
    if len({item["point_id"] for item in targets}) != 2:
        raise CrossVariantPipelineError("cross variant must use two distinct points")
    return sorted(targets, key=lambda item: (item["subject"], item["point_id"]))


def _target_dependency_proof(
    question_index: dict[str, Any],
    targets: list[dict[str, str]],
    *,
    preferred_formal_node_id: str | None = None,
) -> dict[str, Any]:
    """Select one durable, exact question-index witness for each target.

    The witness set is deliberately minimal.  A later unrelated question or a
    second question under the same mechanism must not change this proof, while
    removal or semantic drift of the selected witness must fail current routing.
    """

    questions = question_index.get("questions")
    mechanisms = question_index.get("mechanisms")
    if (
        question_index.get("schema") != "personalization_index_v1"
        or question_index.get("kind") != "question_inverted_index"
        or not isinstance(questions, dict)
        or not isinstance(mechanisms, dict)
    ):
        raise CrossVariantPipelineError(
            "current question index cannot prove cross target mechanisms"
        )
    bindings: list[dict[str, Any]] = []
    for target in targets:
        mechanism = mechanisms.get(target["mechanism_key"])
        if not isinstance(mechanism, dict):
            raise CrossVariantPipelineError(
                "cross target mechanism is not bound by the current question index"
            )
        point_ids = (
            {str(value) for value in mechanism.get("point_ids", [])}
            if isinstance(mechanism.get("point_ids"), list)
            else set()
        )
        formal_ids = (
            {str(value) for value in mechanism.get("formal_node_ids", [])}
            if isinstance(mechanism.get("formal_node_ids"), list)
            else set()
        )
        supporting_formal_node_ids: list[str] = []
        for node_id in sorted(formal_ids):
            question = questions.get(node_id)
            if not isinstance(question, dict):
                continue
            question_points = (
                {str(value) for value in question.get("point_ids", [])}
                if isinstance(question.get("point_ids"), list)
                else set()
            )
            question_mechanisms = (
                {str(value) for value in question.get("mechanism_ids", [])}
                if isinstance(question.get("mechanism_ids"), list)
                else set()
            )
            if (
                FORMAL_NODE_RE.fullmatch(node_id)
                and question.get("node_id") == node_id
                and question.get("subject") == target["subject"]
                and target["point_id"] in question_points
                and target["mechanism_key"] in question_mechanisms
            ):
                supporting_formal_node_ids.append(node_id)
        if (
            mechanism.get("mechanism_id") != target["mechanism_key"]
            or target["point_id"] not in point_ids
            or not supporting_formal_node_ids
        ):
            raise CrossVariantPipelineError(
                "cross target mechanism point and subject binding is invalid"
            )
        witness = (
            preferred_formal_node_id
            if preferred_formal_node_id in supporting_formal_node_ids
            else supporting_formal_node_ids[0]
        )
        bindings.append({"target": target, "supporting_formal_node_id": witness})
    return {"schema": TARGET_PROOF_SCHEMA, "bindings": bindings}


def _validate_target_dependency_proof(
    question_index: dict[str, Any],
    targets: list[dict[str, str]],
    proof: object,
) -> dict[str, Any]:
    """Revalidate only the named target witnesses in the current index."""

    questions = question_index.get("questions")
    mechanisms = question_index.get("mechanisms")
    if (
        question_index.get("schema") != "personalization_index_v1"
        or question_index.get("kind") != "question_inverted_index"
        or not isinstance(questions, dict)
        or not isinstance(mechanisms, dict)
        or not isinstance(proof, dict)
        or set(proof) != {"schema", "bindings"}
        or proof.get("schema") != TARGET_PROOF_SCHEMA
        or not isinstance(proof.get("bindings"), list)
        or len(proof["bindings"]) != len(targets)
    ):
        raise CrossVariantPipelineError(
            "current question index cannot prove cross target dependencies"
        )
    expected_targets = {target["point_id"]: target for target in targets}
    seen: set[str] = set()
    canonical_bindings: list[dict[str, Any]] = []
    for raw_binding in proof["bindings"]:
        if not isinstance(raw_binding, dict) or set(raw_binding) != {
            "target",
            "supporting_formal_node_id",
        }:
            raise CrossVariantPipelineError("target dependency snapshot is invalid")
        target = _target(raw_binding.get("target"), "snapshot")
        expected_target = expected_targets.get(target["point_id"])
        node_id = str(raw_binding.get("supporting_formal_node_id") or "")
        if expected_target != target or target["point_id"] in seen:
            raise CrossVariantPipelineError("target dependency snapshot is invalid")
        seen.add(target["point_id"])
        mechanism = mechanisms.get(target["mechanism_key"])
        question = questions.get(node_id)
        point_ids = (
            {str(value) for value in mechanism.get("point_ids", [])}
            if isinstance(mechanism, dict)
            and isinstance(mechanism.get("point_ids"), list)
            else set()
        )
        formal_ids = (
            {str(value) for value in mechanism.get("formal_node_ids", [])}
            if isinstance(mechanism, dict)
            and isinstance(mechanism.get("formal_node_ids"), list)
            else set()
        )
        question_points = (
            {str(value) for value in question.get("point_ids", [])}
            if isinstance(question, dict)
            and isinstance(question.get("point_ids"), list)
            else set()
        )
        question_mechanisms = (
            {str(value) for value in question.get("mechanism_ids", [])}
            if isinstance(question, dict)
            and isinstance(question.get("mechanism_ids"), list)
            else set()
        )
        if (
            not FORMAL_NODE_RE.fullmatch(node_id)
            or not isinstance(mechanism, dict)
            or mechanism.get("mechanism_id") != target["mechanism_key"]
            or target["point_id"] not in point_ids
            or node_id not in formal_ids
            or not isinstance(question, dict)
            or question.get("node_id") != node_id
            or question.get("subject") != target["subject"]
            or target["point_id"] not in question_points
            or target["mechanism_key"] not in question_mechanisms
        ):
            raise CrossVariantPipelineError(
                "target dependency witness has drifted from the current question index"
            )
        canonical_bindings.append(
            {"target": target, "supporting_formal_node_id": node_id}
        )
    canonical = {
        "schema": TARGET_PROOF_SCHEMA,
        "bindings": sorted(
            canonical_bindings,
            key=lambda item: (
                item["target"]["subject"],
                item["target"]["point_id"],
            ),
        ),
    }
    if canonical != proof or seen != set(expected_targets):
        raise CrossVariantPipelineError("target dependency snapshot is invalid")
    return canonical


def _target_semantic_binding(proof: dict[str, Any]) -> str:
    return _canonical_digest(proof)


def _edge_semantic_proof(
    record: dict[str, Any],
    *,
    edge_id: str,
    targets: list[dict[str, str]],
) -> dict[str, Any]:
    """Normalize routing-relevant semantics for one exact verified-live edge."""

    endpoints = {
        str((record.get("left") or {}).get("id") or ""),
        str((record.get("right") or {}).get("id") or ""),
    }
    endpoint_subjects = {
        str((record.get("left") or {}).get("id") or ""): str(
            (record.get("left") or {}).get("subject") or ""
        ),
        str((record.get("right") or {}).get("id") or ""): str(
            (record.get("right") or {}).get("subject") or ""
        ),
    }
    expected_endpoint_subjects = {
        target["point_id"]: target["subject"] for target in targets
    }
    if (
        record.get("edge_id") != edge_id
        or endpoints != set(expected_endpoint_subjects)
        or endpoint_subjects != expected_endpoint_subjects
    ):
        raise CrossVariantPipelineError("cross edge endpoints do not match both targets")
    direction = record.get("direction")
    direction_binding = None
    if isinstance(direction, dict):
        direction_binding = {
            "from": str(direction.get("from") or ""),
            "kind": str(direction.get("kind") or ""),
            "to": str(direction.get("to") or ""),
        }
    return {
        "schema": EDGE_PROOF_SCHEMA,
        "edge_id": edge_id,
        "left": {
            "id": str((record.get("left") or {}).get("id") or ""),
            "subject": str((record.get("left") or {}).get("subject") or ""),
        },
        "right": {
            "id": str((record.get("right") or {}).get("id") or ""),
            "subject": str((record.get("right") or {}).get("subject") or ""),
        },
        "direction": direction_binding,
        "relation_code": str(record.get("relation_code") or ""),
        "lifecycle": str(record.get("lifecycle") or ""),
        "activation_eligible": record.get("activation_eligible") is True,
        "enabled_for_live_routing": record.get("enabled_for_live_routing") is True,
        "activation": str(record.get("activation") or ""),
    }


def _edge_semantic_binding(proof: dict[str, Any]) -> str:
    return _canonical_digest(proof)


def _answer_set(raw: object, label: str) -> list[str]:
    if not isinstance(raw, list) or not 1 <= len(raw) <= 8:
        raise CrossVariantPipelineError(f"{label} must be one non-empty answer set")
    values = [str(value).strip() for value in raw]
    if (
        any(not ANSWER_TOKEN_RE.fullmatch(value) for value in values)
        or len(values) != len(set(values))
    ):
        raise CrossVariantPipelineError(f"{label} contains invalid answer tokens")
    return sorted(values)


def _strict_canonical_answer_set(raw: object, label: str) -> list[str]:
    """Validate one answer set without silently normalizing verifier evidence."""

    if not isinstance(raw, list) or any(not isinstance(value, str) for value in raw):
        raise CrossVariantPipelineError(f"{label} must be a canonical answer set")
    canonical = _answer_set(raw, label)
    if raw != canonical:
        raise CrossVariantPipelineError(
            f"{label} contains duplicate or non-canonical answer tokens"
        )
    return canonical


def _candidate_answer_sets(
    tokens: list[str], minimum: int, maximum: int
) -> list[list[str]]:
    return [
        list(candidate)
        for width in range(minimum, maximum + 1)
        for candidate in itertools.combinations(tokens, width)
    ]


def _canonical_candidate_subset(
    raw: object,
    *,
    label: str,
    candidate_order: dict[tuple[str, ...], int],
) -> list[list[str]]:
    if not isinstance(raw, list):
        raise CrossVariantPipelineError(f"{label} must be a list of answer sets")
    canonical: list[list[str]] = []
    seen: set[tuple[str, ...]] = set()
    for index, item in enumerate(raw, 1):
        answer_set = _strict_canonical_answer_set(item, f"{label} item {index}")
        key = tuple(answer_set)
        if key not in candidate_order:
            raise CrossVariantPipelineError(
                f"{label} must be a subset of the complete candidate universe"
            )
        if key in seen:
            raise CrossVariantPipelineError(
                f"{label} contains duplicate answer sets"
            )
        seen.add(key)
        canonical.append(answer_set)
    expected = sorted(canonical, key=lambda item: candidate_order[tuple(item)])
    if canonical != expected:
        raise CrossVariantPipelineError(f"{label} is not canonically sorted")
    return canonical


def _semantic_uniqueness_evidence(
    raw: object,
    *,
    targets: list[dict[str, str]],
    blind_answer_set: list[str],
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Recompute uniqueness and two-subject essentiality from private evidence.

    The verifier must enumerate the complete answer-set universe implied by the
    token universe and selection cardinality.  Each subject contributes the
    subset satisfying all of that subject's constraints.  The machine derives
    the joint intersection and both leave-one-subject-out intersections rather
    than trusting the verifier's booleans.
    """

    required = {
        "schema",
        "answer_token_universe",
        "selection_cardinality",
        "candidate_answer_sets",
        "subject_constraint_satisfaction",
        "joint_satisfying_answer_sets",
        "formal_write_count",
    }
    if (
        not isinstance(raw, dict)
        or set(raw) != required
        or raw.get("schema") != SEMANTIC_UNIQUENESS_SCHEMA
        or not _formal_boundary_is_zero(raw)
    ):
        raise CrossVariantPipelineError("semantic uniqueness evidence is invalid")

    token_raw = raw.get("answer_token_universe")
    if (
        not isinstance(token_raw, list)
        or not 2 <= len(token_raw) <= 8
        or any(not isinstance(value, str) for value in token_raw)
    ):
        raise CrossVariantPipelineError(
            "semantic uniqueness answer token universe is invalid"
        )
    tokens = [value.strip() for value in token_raw]
    if (
        tokens != token_raw
        or tokens != sorted(tokens)
        or len(tokens) != len(set(tokens))
        or any(not ANSWER_TOKEN_RE.fullmatch(value) for value in tokens)
    ):
        raise CrossVariantPipelineError(
            "semantic uniqueness answer token universe is not canonical"
        )

    cardinality = raw.get("selection_cardinality")
    if not isinstance(cardinality, dict) or set(cardinality) != {"minimum", "maximum"}:
        raise CrossVariantPipelineError(
            "semantic uniqueness selection cardinality is invalid"
        )
    minimum = cardinality.get("minimum")
    maximum = cardinality.get("maximum")
    if (
        isinstance(minimum, bool)
        or isinstance(maximum, bool)
        or not isinstance(minimum, int)
        or not isinstance(maximum, int)
        or not 1 <= minimum <= maximum <= len(tokens)
    ):
        raise CrossVariantPipelineError(
            "semantic uniqueness selection cardinality is invalid"
        )

    complete_candidates = _candidate_answer_sets(tokens, minimum, maximum)
    candidate_order = {
        tuple(candidate): index for index, candidate in enumerate(complete_candidates)
    }
    candidate_raw = raw.get("candidate_answer_sets")
    candidates = _canonical_candidate_subset(
        candidate_raw,
        label="semantic uniqueness candidate answer sets",
        candidate_order=candidate_order,
    )
    if candidates != complete_candidates:
        raise CrossVariantPipelineError(
            "semantic uniqueness candidate answer sets are not the complete canonical universe"
        )

    constraint_raw = raw.get("subject_constraint_satisfaction")
    if not isinstance(constraint_raw, list) or len(constraint_raw) != len(targets):
        raise CrossVariantPipelineError(
            "semantic uniqueness subject constraints are incomplete"
        )
    expected_by_point = {target["point_id"]: target for target in targets}
    seen_points: set[str] = set()
    constraints: list[dict[str, Any]] = []
    satisfaction_by_subject: dict[str, set[tuple[str, ...]]] = {}
    for index, item in enumerate(constraint_raw, 1):
        if not isinstance(item, dict) or set(item) != {
            "target",
            "satisfying_answer_sets",
        }:
            raise CrossVariantPipelineError(
                "semantic uniqueness subject constraint is invalid"
            )
        target = _target(item.get("target"), f"semantic constraint {index}")
        if (
            expected_by_point.get(target["point_id"]) != target
            or target["point_id"] in seen_points
            or target["subject"] in satisfaction_by_subject
        ):
            raise CrossVariantPipelineError(
                "semantic uniqueness subject constraints do not match bound targets"
            )
        seen_points.add(target["point_id"])
        satisfying = _canonical_candidate_subset(
            item.get("satisfying_answer_sets"),
            label=f"semantic uniqueness {target['subject']} satisfying answer sets",
            candidate_order=candidate_order,
        )
        satisfaction_by_subject[target["subject"]] = {
            tuple(answer_set) for answer_set in satisfying
        }
        constraints.append(
            {"target": target, "satisfying_answer_sets": satisfying}
        )
    constraints.sort(
        key=lambda item: (item["target"]["subject"], item["target"]["point_id"])
    )
    if constraints != constraint_raw or seen_points != set(expected_by_point):
        raise CrossVariantPipelineError(
            "semantic uniqueness subject constraints are not canonical"
        )

    all_candidate_keys = [tuple(candidate) for candidate in complete_candidates]
    joint_keys = set(all_candidate_keys)
    for satisfying in satisfaction_by_subject.values():
        joint_keys.intersection_update(satisfying)
    recomputed_joint = [
        candidate for candidate in complete_candidates if tuple(candidate) in joint_keys
    ]
    declared_joint = _canonical_candidate_subset(
        raw.get("joint_satisfying_answer_sets"),
        label="semantic uniqueness joint satisfying answer sets",
        candidate_order=candidate_order,
    )
    if declared_joint != recomputed_joint:
        raise CrossVariantPipelineError(
            "semantic uniqueness joint satisfying sets do not match machine recomputation"
        )
    if len(recomputed_joint) != 1:
        raise CrossVariantPipelineError(
            "semantic uniqueness requires exactly one jointly satisfying answer set"
        )
    if recomputed_joint[0] != blind_answer_set:
        raise CrossVariantPipelineError(
            "semantic uniqueness result does not equal the blind answer"
        )

    ablation_counts: dict[str, int] = {}
    subjects = sorted(satisfaction_by_subject)
    for removed_subject in subjects:
        remaining = set(all_candidate_keys)
        for subject, satisfying in satisfaction_by_subject.items():
            if subject != removed_subject:
                remaining.intersection_update(satisfying)
        count = len(remaining)
        ablation_counts[removed_subject] = count
        if count < 2:
            raise CrossVariantPipelineError(
                "semantic uniqueness proves one subject alone is already unique; "
                "each subject must be essential"
            )

    canonical = {
        "schema": SEMANTIC_UNIQUENESS_SCHEMA,
        "answer_token_universe": tokens,
        "selection_cardinality": {"minimum": minimum, "maximum": maximum},
        "candidate_answer_sets": complete_candidates,
        "subject_constraint_satisfaction": constraints,
        "joint_satisfying_answer_sets": recomputed_joint,
        "formal_write_count": 0,
    }
    if raw != canonical:
        raise CrossVariantPipelineError(
            "semantic uniqueness evidence is not canonical"
        )
    summary = {
        "schema": "cross_variant_semantic_uniqueness_recomputation_v1",
        "semantic_uniqueness_evidence_sha256": _canonical_digest(canonical),
        "candidate_answer_set_count": len(complete_candidates),
        "joint_satisfying_count": len(recomputed_joint),
        "leave_one_subject_out_satisfying_counts": dict(sorted(ablation_counts.items())),
    }
    return canonical, summary


def _authoritative_answer_key(
    path: Path,
    *,
    formal_node_id: str,
    edge_id: str,
    expected_sha256: object,
) -> tuple[dict[str, Any], list[str], str]:
    raw = _read_managed_private_file(path, "authoritative answer key")
    digest = _sha256_bytes(raw)
    expected = str(expected_sha256 or "")
    if (
        not SHA256_RE.fullmatch(expected)
        or digest != expected
    ):
        raise CrossVariantPipelineError("authoritative answer key sha256 mismatch")
    try:
        payload = json.loads(raw.decode("utf-8"))
    except (UnicodeError, json.JSONDecodeError) as exc:
        raise CrossVariantPipelineError(
            "authoritative answer key is unreadable"
        ) from exc
    if not isinstance(payload, dict):
        raise CrossVariantPipelineError(
            "authoritative answer key must be a JSON object"
        )
    if set(payload) != {
        "schema",
        "original_formal_node_id",
        "verified_cross_edge_id",
        "answer_sets",
        "formal_write_count",
    } or (
        payload.get("schema") != ANSWER_KEY_SCHEMA
        or payload.get("original_formal_node_id") != formal_node_id
        or payload.get("verified_cross_edge_id") != edge_id
        or not _formal_boundary_is_zero(payload)
    ):
        raise CrossVariantPipelineError("authoritative answer key is invalid")
    answer_sets = payload.get("answer_sets")
    if not isinstance(answer_sets, list) or len(answer_sets) != 1:
        raise CrossVariantPipelineError(
            "authoritative answer key must contain exactly one answer set"
        )
    return payload, _answer_set(answer_sets[0], "authoritative answer set"), digest


def _validated_cross_edge(
    repo: Path,
    projection_path: Path,
    projection: dict[str, Any],
    *,
    edge_id: str,
    targets: list[dict[str, str]],
) -> dict[str, Any]:
    expected_path = (repo / DEFAULT_PROJECTION_RELATIVE).resolve()
    if projection_path != expected_path:
        raise CrossVariantPipelineError("cross projection is not the managed projection")
    try:
        validated = validate_projection(repo, projection)
    except ShadowCatalogError as exc:
        raise CrossVariantPipelineError("cross projection is not a current verified build") from exc
    records = validated.get("records")
    matches = [
        record
        for record in records if isinstance(records, list) and isinstance(record, dict)
        and record.get("edge_id") == edge_id
    ] if isinstance(records, list) else []
    if len(matches) != 1:
        raise CrossVariantPipelineError("verified cross edge is missing or ambiguous")
    record = matches[0]
    if (
        record.get("lifecycle") != "verified_existing"
        or record.get("activation_eligible") is not True
        or record.get("enabled_for_live_routing") is not True
        or record.get("activation") != "verified_live"
    ):
        raise CrossVariantPipelineError("cross edge is not verified live")
    endpoints = {
        str((record.get("left") or {}).get("id") or ""),
        str((record.get("right") or {}).get("id") or ""),
    }
    target_points = {item["point_id"] for item in targets}
    if endpoints != target_points:
        raise CrossVariantPipelineError("cross edge endpoints do not match both targets")
    return record


def validate_current_live_cross_edge(
    repo: str | Path,
    *,
    edge_id: str,
    targets: list[dict[str, str]] | None = None,
    required_point_id: str | None = None,
) -> dict[str, Any]:
    """Revalidate one edge from the bounded root-manifest-bound live projection.

    This is the answer/register hot-path check.  It intentionally does not call
    ``validate_projection`` because that cold-path validator rebuilds the whole
    relationship projection from repository sources.
    """

    root = Path(repo).expanduser().resolve()
    if not EDGE_RE.fullmatch(str(edge_id or "")):
        raise CrossVariantPipelineError("cross edge identity is invalid")
    if required_point_id is not None and not POINT_RE.fullmatch(required_point_id):
        raise CrossVariantPipelineError("selected cross point identity is invalid")
    state, _, _ = _state_roots(root)
    manifest_path = state / "manifest.json"
    manifest, _ = _load_bounded_object(
        manifest_path,
        "current personalization manifest",
        max_bytes=MAX_LIVE_CROSS_MANIFEST_BYTES,
    )
    projection_path = (root / DEFAULT_PROJECTION_RELATIVE).resolve()
    binding = (manifest.get("independent_projections") or {}).get(
        "cross_subject_shadow"
    )
    expected_sha = str(manifest.get("cross_subject_shadow_sha256") or "")
    if (
        manifest.get("schema") != "personalization_index_v1"
        or manifest.get("answer_safe") is not True
        or not _formal_boundary_is_zero(manifest)
        or not isinstance(binding, dict)
        or binding.get("included_in_this_build") is not True
        or binding.get("file") != projection_path.name
        or not SHA256_RE.fullmatch(expected_sha)
    ):
        raise CrossVariantPipelineError(
            "current cross projection is not root-manifest bound"
        )
    projection, projection_sha = _load_bounded_object(
        projection_path,
        "current cross projection",
        max_bytes=MAX_LIVE_CROSS_PROJECTION_BYTES,
    )
    records = projection.get("records")
    if (
        projection_sha != expected_sha
        or projection.get("schema_version") != "cross_subject_shadow_v1"
        or not _formal_boundary_is_zero(projection)
        or not isinstance(records, list)
        or len(records) > MAX_LIVE_CROSS_RECORDS
    ):
        raise CrossVariantPipelineError(
            "current cross projection is stale, oversized, or invalid"
        )
    canonical_targets = _canonical_targets(targets) if targets is not None else None
    matches = [
        record
        for record in records
        if isinstance(record, dict) and record.get("edge_id") == edge_id
    ]
    if len(matches) != 1:
        raise CrossVariantPipelineError("verified cross edge is missing or ambiguous")
    record = matches[0]
    if (
        record.get("lifecycle") != "verified_existing"
        or record.get("activation_eligible") is not True
        or record.get("enabled_for_live_routing") is not True
        or record.get("activation") != "verified_live"
    ):
        raise CrossVariantPipelineError("cross edge is not verified live")
    endpoints = {
        str((record.get("left") or {}).get("id") or ""),
        str((record.get("right") or {}).get("id") or ""),
    }
    if canonical_targets is not None and endpoints != {
        target["point_id"] for target in canonical_targets
    }:
        raise CrossVariantPipelineError("cross edge endpoints do not match both targets")
    if required_point_id is not None and required_point_id not in endpoints:
        raise CrossVariantPipelineError("cross edge does not contain the selected point")
    return record


def _validated_evidence(
    evidence: dict[str, Any],
) -> tuple[
    str,
    str,
    list[dict[str, str]],
    dict[str, str],
    dict[str, str],
]:
    required = {
        "schema",
        "evidence_scope",
        "original_formal_node_id",
        "original_source_ref",
        "original_source_sha256",
        "authoritative_answer_key_ref",
        "authoritative_answer_key_sha256",
        "authoritative_answer_key_private",
        "cross_projection_ref",
        "cross_projection_sha256",
        "verified_cross_edge_id",
        "targets",
        "author_identity",
        "verifier_identity",
        "formal_write_count",
    }
    formal_node_id = str(evidence.get("original_formal_node_id") or "")
    edge_id = str(evidence.get("verified_cross_edge_id") or "")
    if set(evidence) != required or (
        evidence.get("schema") != EVIDENCE_SCHEMA
        or evidence.get("evidence_scope")
        != "verified_original_private_answer_and_verified_cross_edge"
        or not FORMAL_NODE_RE.fullmatch(formal_node_id)
        or not EDGE_RE.fullmatch(edge_id)
        or evidence.get("authoritative_answer_key_private") is not True
        or not str(evidence.get("original_source_ref") or "").strip()
        or not SHA256_RE.fullmatch(
            str(evidence.get("original_source_sha256") or "")
        )
        or not str(evidence.get("authoritative_answer_key_ref") or "").strip()
        or not SHA256_RE.fullmatch(
            str(evidence.get("authoritative_answer_key_sha256") or "")
        )
        or not str(evidence.get("cross_projection_ref") or "").strip()
        or not SHA256_RE.fullmatch(
            str(evidence.get("cross_projection_sha256") or "")
        )
        or not _formal_boundary_is_zero(evidence)
    ):
        raise CrossVariantPipelineError("cross-variant authoring evidence is incomplete")
    targets = _canonical_targets(evidence.get("targets"))
    if formal_node_id[:2] not in {item["subject"] for item in targets}:
        raise CrossVariantPipelineError("original question subject is outside cross targets")
    author, verifier = _identity_pair(evidence)
    return formal_node_id, edge_id, targets, author, verifier


def _validate_prepare_projection_bindings(
    repo: Path,
    *,
    targets: list[dict[str, str]],
    preferred_formal_node_id: str,
    projection_path: Path,
    projection_sha256: str,
) -> tuple[dict[str, Any], str]:
    """Read one root-bound issuance snapshot and select exact target witnesses."""

    state, _, _ = _state_roots(repo)
    manifest, _ = _load_bounded_object(
        state / "manifest.json",
        "current personalization manifest",
        max_bytes=MAX_LIVE_CROSS_MANIFEST_BYTES,
    )
    question_name = "question_index.json"
    question_expected = str(
        (manifest.get("artifact_hashes") or {}).get(question_name) or ""
    )
    projection_binding = (manifest.get("independent_projections") or {}).get(
        "cross_subject_shadow"
    )
    managed_projection = (repo / DEFAULT_PROJECTION_RELATIVE).resolve()
    if (
        manifest.get("schema") != "personalization_index_v1"
        or manifest.get("answer_safe") is not True
        or not _formal_boundary_is_zero(manifest)
        or not SHA256_RE.fullmatch(question_expected)
        or not isinstance(projection_binding, dict)
        or projection_binding.get("included_in_this_build") is not True
        or projection_binding.get("file") != managed_projection.name
        or manifest.get("cross_subject_shadow_sha256") != projection_sha256
        or projection_path != managed_projection
    ):
        raise CrossVariantPipelineError(
            "prepare targets and cross projection are not bound by one current manifest"
        )
    question_index, question_sha = _load_bounded_object(
        state / question_name,
        "current question index",
        max_bytes=MAX_QUESTION_INDEX_BYTES,
    )
    if question_sha != question_expected:
        raise CrossVariantPipelineError(
            "current question index cannot prove cross target mechanisms"
        )
    proof = _target_dependency_proof(
        question_index,
        targets,
        preferred_formal_node_id=preferred_formal_node_id,
    )
    return proof, question_sha


def _dependency_snapshot_path(repo: Path, digest: str) -> Path:
    if not SHA256_RE.fullmatch(digest):
        raise CrossVariantPipelineError("target dependency snapshot sha256 is invalid")
    _, durable, _ = _state_roots(repo)
    return _guard_managed_linked_path(
        repo,
        durable / "cross-variant-dependency-snapshots" / f"{digest}.json",
        "cross-variant target dependency snapshot",
    )


def _write_dependency_snapshot(
    repo: Path,
    *,
    formal_node_id: str,
    original_source_ref: str,
    original_source_sha256: str,
    targets: list[dict[str, str]],
    target_proof: dict[str, Any],
    edge_id: str,
    edge_proof: dict[str, Any],
) -> tuple[dict[str, Any], str, str]:
    _retired_write_surface()

    # Historical implementation retained below for read-only source compatibility.
    snapshot = {
        "schema": DEPENDENCY_SNAPSHOT_SCHEMA,
        "answer_safe": True,
        "original_source": {
            "formal_node_id": formal_node_id,
            "source_ref": original_source_ref,
            "source_sha256": original_source_sha256,
        },
        "targets": targets,
        "target_dependency_proof": target_proof,
        "target_semantic_binding_sha256": _target_semantic_binding(target_proof),
        "verified_cross_edge_id": edge_id,
        "cross_edge_semantic_proof": edge_proof,
        "cross_edge_semantic_binding_sha256": _edge_semantic_binding(edge_proof),
        "formal_write_count": 0,
    }
    digest = _sha256_bytes(_json_bytes(snapshot))
    path = _dependency_snapshot_path(repo, digest)
    if path.exists():
        existing, existing_sha = _load_bounded_object(
            path,
            "target dependency snapshot",
            max_bytes=MAX_DEPENDENCY_SNAPSHOT_BYTES,
        )
        if existing != snapshot or existing_sha != digest:
            raise CrossVariantPipelineError("target dependency snapshot identity conflict")
    else:
        _atomic_write_managed_json(repo, path, snapshot)
    return snapshot, path.relative_to(repo).as_posix(), digest


def _load_dependency_snapshot(
    repo: Path,
    locator: object,
    expected_sha256: object,
) -> dict[str, Any]:
    digest = str(expected_sha256 or "")
    raw = str(locator or "").strip()
    candidate = Path(raw)
    expected_path = _dependency_snapshot_path(repo, digest)
    if (
        not raw
        or candidate.is_absolute()
        or raw.startswith("~")
        or "#" in raw
        or ".." in candidate.parts
        or (repo / candidate).resolve() != expected_path.resolve()
    ):
        raise CrossVariantPipelineError(
            "target dependency snapshot is outside its content-addressed store"
        )
    snapshot, actual_sha = _load_bounded_object(
        expected_path,
        "target dependency snapshot",
        max_bytes=MAX_DEPENDENCY_SNAPSHOT_BYTES,
    )
    if actual_sha != digest or _sha256_bytes(_json_bytes(snapshot)) != digest:
        raise CrossVariantPipelineError("target dependency snapshot sha256 mismatch")
    required = {
        "schema",
        "answer_safe",
        "original_source",
        "targets",
        "target_dependency_proof",
        "target_semantic_binding_sha256",
        "verified_cross_edge_id",
        "cross_edge_semantic_proof",
        "cross_edge_semantic_binding_sha256",
        "formal_write_count",
    }
    original = snapshot.get("original_source")
    if (
        set(snapshot) != required
        or snapshot.get("schema") != DEPENDENCY_SNAPSHOT_SCHEMA
        or snapshot.get("answer_safe") is not True
        or not _formal_boundary_is_zero(snapshot)
        or not isinstance(original, dict)
        or set(original) != {"formal_node_id", "source_ref", "source_sha256"}
        or not FORMAL_NODE_RE.fullmatch(str(original.get("formal_node_id") or ""))
        or not str(original.get("source_ref") or "").strip()
        or not SHA256_RE.fullmatch(str(original.get("source_sha256") or ""))
    ):
        raise CrossVariantPipelineError("target dependency snapshot is invalid")
    targets = _canonical_targets(snapshot.get("targets"))
    if targets != snapshot.get("targets"):
        raise CrossVariantPipelineError("target dependency snapshot targets are not canonical")
    proof = snapshot.get("target_dependency_proof")
    if (
        not isinstance(proof, dict)
        or set(proof) != {"schema", "bindings"}
        or proof.get("schema") != TARGET_PROOF_SCHEMA
        or not isinstance(proof.get("bindings"), list)
        or len(proof["bindings"]) != len(targets)
    ):
        raise CrossVariantPipelineError("target dependency snapshot is invalid")
    frozen_bindings: list[dict[str, Any]] = []
    for binding in proof["bindings"]:
        if not isinstance(binding, dict) or set(binding) != {
            "target",
            "supporting_formal_node_id",
        }:
            raise CrossVariantPipelineError("target dependency snapshot is invalid")
        target = _target(binding.get("target"), "snapshot")
        node_id = str(binding.get("supporting_formal_node_id") or "")
        if not FORMAL_NODE_RE.fullmatch(node_id):
            raise CrossVariantPipelineError("target dependency snapshot is invalid")
        frozen_bindings.append(
            {"target": target, "supporting_formal_node_id": node_id}
        )
    frozen_proof = {
        "schema": TARGET_PROOF_SCHEMA,
        "bindings": sorted(
            frozen_bindings,
            key=lambda item: (
                item["target"]["subject"],
                item["target"]["point_id"],
            ),
        ),
    }
    if (
        frozen_proof != proof
        or [item["target"] for item in frozen_proof["bindings"]] != targets
        or snapshot.get("target_semantic_binding_sha256")
        != _target_semantic_binding(frozen_proof)
    ):
        raise CrossVariantPipelineError("target dependency snapshot is invalid")
    edge_id = str(snapshot.get("verified_cross_edge_id") or "")
    edge_proof = snapshot.get("cross_edge_semantic_proof")
    if not EDGE_RE.fullmatch(edge_id) or not isinstance(edge_proof, dict):
        raise CrossVariantPipelineError("target dependency snapshot is invalid")
    normalized_edge = _edge_semantic_proof(
        edge_proof,
        edge_id=edge_id,
        targets=targets,
    )
    if (
        edge_proof != normalized_edge
        or edge_proof.get("lifecycle") != "verified_existing"
        or edge_proof.get("activation_eligible") is not True
        or edge_proof.get("enabled_for_live_routing") is not True
        or edge_proof.get("activation") != "verified_live"
        or snapshot.get("cross_edge_semantic_binding_sha256")
        != _edge_semantic_binding(normalized_edge)
        or str(original["formal_node_id"])[:2]
        not in {target["subject"] for target in targets}
    ):
        raise CrossVariantPipelineError("target dependency snapshot is invalid")
    return snapshot


def _unsigned_request_from_dependencies(
    *,
    evidence_sha256: str,
    formal_node_id: str,
    original_source_ref: str,
    original_source_sha256: str,
    answer_key_sha256: str,
    private_root_binding_sha256: str,
    dependency_snapshot_ref: str,
    dependency_snapshot_sha256: str,
    target_binding_sha256: str,
    edge_binding_sha256: str,
    edge_id: str,
    relation_code: str,
    targets: list[dict[str, str]],
    author: dict[str, str],
    verifier: dict[str, str],
) -> dict[str, Any]:
    material = {
        "evidence_sha256": evidence_sha256,
        "original_formal_node_id": formal_node_id,
        "original_source_sha256": original_source_sha256,
        "authoritative_answer_key_sha256": answer_key_sha256,
        "authoritative_answer_key_private_root_binding_sha256": (
            private_root_binding_sha256
        ),
        "target_dependency_snapshot_sha256": dependency_snapshot_sha256,
        "verified_cross_edge_id": edge_id,
        "targets": targets,
        "author_identity_sha256": _canonical_digest(author),
        "verifier_identity_sha256": _canonical_digest(verifier),
    }
    request_id = "CVRQ-" + _canonical_digest(material)[:24].upper()
    return {
        "schema": REQUEST_SCHEMA,
        "request_id": request_id,
        "status": "awaiting_author_surface_commit",
        "original_formal_node_id": formal_node_id,
        "original_source_ref": original_source_ref,
        "original_source_sha256": original_source_sha256,
        "authoritative_answer_key_sha256": answer_key_sha256,
        "authoritative_answer_key_private_root_binding_sha256": (
            private_root_binding_sha256
        ),
        "target_dependency_snapshot_ref": dependency_snapshot_ref,
        "target_dependency_snapshot_sha256": dependency_snapshot_sha256,
        "target_semantic_binding_sha256": target_binding_sha256,
        "cross_edge_semantic_binding_sha256": edge_binding_sha256,
        "verified_cross_edge_id": edge_id,
        "relation_code": relation_code,
        "targets": targets,
        "subjects": sorted(item["subject"] for item in targets),
        "author_identity": author,
        "author_identity_sha256": _canonical_digest(author),
        "verifier_identity": verifier,
        "verifier_identity_sha256": _canonical_digest(verifier),
        "evidence_sha256": evidence_sha256,
        "generation_constraints": {
            "question_surface_only": True,
            "blind_verifier_before_reveal": True,
            "both_subjects_essential": True,
            "structural_change_required": True,
            "numbers_only_forbidden": True,
        },
        "formal_write_count": 0,
    }


def _build_prepare_material(
    repo: Path,
    evidence_path: Path,
    evidence_ref: str,
    answer_key_private_root: str | Path | None,
) -> tuple[dict[str, Any], dict[str, Any]]:
    evidence = _load_object(evidence_path, "cross-variant authoring evidence")
    formal_node_id, edge_id, targets, author, verifier = _validated_evidence(evidence)
    private_root, private_root_ref, private_root_binding = _managed_private_root(
        repo, answer_key_private_root
    )
    try:
        original = validate_source_for_practice(
            repo,
            evidence.get("original_source_ref"),
            evidence.get("original_source_sha256"),
            practice_mode="original",
            formal_node_id=formal_node_id,
        )
    except LinkedPracticeSourceError as exc:
        raise CrossVariantPipelineError("original question source is not verified") from exc
    answer_key_path, answer_key_ref = _managed_private_answer_key(
        private_root, evidence.get("authoritative_answer_key_ref")
    )
    _, _, answer_key_sha = _authoritative_answer_key(
        answer_key_path,
        formal_node_id=formal_node_id,
        edge_id=edge_id,
        expected_sha256=evidence.get("authoritative_answer_key_sha256"),
    )
    projection_path, _ = _repo_file(
        repo, evidence.get("cross_projection_ref"), "cross projection"
    )
    issuance_projection_sha = str(evidence["cross_projection_sha256"])
    current_projection_sha = _sha256_file(projection_path)
    if current_projection_sha != issuance_projection_sha:
        raise CrossVariantPipelineError("cross projection sha256 mismatch")
    projection = _load_object(projection_path, "cross projection")
    target_proof, _ = _validate_prepare_projection_bindings(
        repo,
        targets=targets,
        preferred_formal_node_id=formal_node_id,
        projection_path=projection_path,
        projection_sha256=current_projection_sha,
    )
    record = _validated_cross_edge(
        repo,
        projection_path,
        projection,
        edge_id=edge_id,
        targets=targets,
    )
    edge_proof = _edge_semantic_proof(record, edge_id=edge_id, targets=targets)
    snapshot, snapshot_ref, snapshot_sha = _write_dependency_snapshot(
        repo,
        formal_node_id=formal_node_id,
        original_source_ref=str(original["source_ref"]),
        original_source_sha256=str(original["source_sha256"]),
        targets=targets,
        target_proof=target_proof,
        edge_id=edge_id,
        edge_proof=edge_proof,
    )
    evidence_sha = _sha256_file(evidence_path)
    unsigned = _unsigned_request_from_dependencies(
        evidence_sha256=evidence_sha,
        formal_node_id=formal_node_id,
        original_source_ref=str(original["source_ref"]),
        original_source_sha256=str(original["source_sha256"]),
        answer_key_sha256=answer_key_sha,
        private_root_binding_sha256=private_root_binding,
        dependency_snapshot_ref=snapshot_ref,
        dependency_snapshot_sha256=snapshot_sha,
        target_binding_sha256=str(snapshot["target_semantic_binding_sha256"]),
        edge_binding_sha256=str(snapshot["cross_edge_semantic_binding_sha256"]),
        edge_id=edge_id,
        relation_code=str(record.get("relation_code") or ""),
        targets=targets,
        author=author,
        verifier=verifier,
    )
    request_id = str(unsigned["request_id"])
    private = {
        "schema": PRIVATE_PREPARE_SCHEMA,
        "request_id": request_id,
        "evidence_ref": evidence_ref,
        "evidence_sha256": evidence_sha,
        "authoritative_answer_key_private_root_ref": private_root_ref,
        "authoritative_answer_key_private_root_binding_sha256": private_root_binding,
        "authoritative_answer_key_ref": answer_key_ref,
        "authoritative_answer_key_sha256": answer_key_sha,
        "target_dependency_snapshot_ref": snapshot_ref,
        "target_dependency_snapshot_sha256": snapshot_sha,
        "formal_write_count": 0,
    }
    return unsigned, private


def _signed_request(unsigned: dict[str, Any], key: bytes) -> dict[str, Any]:
    payload = {**unsigned, "prepare_key_id": _sha256_bytes(key)}
    payload["prepare_signature"] = hmac.new(
        key, _json_bytes(payload, pretty=False), hashlib.sha256
    ).hexdigest()
    return payload


def _prepare_cross_variant_hot_locked(
    repo: str | Path,
    evidence_path: str | Path,
    *,
    answer_key_private_root: str | Path | None = None,
) -> dict[str, Any]:
    _retired_write_surface()

    # Historical implementation retained below for read-only source compatibility.
    root = Path(repo).expanduser().resolve()
    _, durable, work = _state_roots(root)
    with _pipeline_lock(root, durable):
        evidence_file, evidence_ref = _repo_file(
            root, evidence_path, "authoring evidence"
        )
        unsigned, private = _build_prepare_material(
            root, evidence_file, evidence_ref, answer_key_private_root
        )
        request_id = str(unsigned["request_id"])
        request_path = _guard_managed_linked_path(
            root,
            work / request_id / "requests" / "pending.json",
            "cross-variant generation request path",
        )
        private_path = _guard_managed_linked_path(
            root,
            work / request_id / "private" / "prepare.json",
            "cross-variant private work path",
        )
        key = _prepare_key(root, durable, create=True)
        request = _signed_request(unsigned, key)
        request_sha = _sha256_bytes(_json_bytes(request))
        request_path = work / request_id / "requests" / f"{request_sha}.json"
        _guard_managed_linked_path(
            root, request_path, "cross-variant generation request path"
        )
        if request_path.is_file() and _load_object(request_path, "generation request") != request:
            raise CrossVariantPipelineError("generation request identity conflict")
        if private_path.is_file() and _load_object(private_path, "private prepare") != private:
            raise CrossVariantPipelineError("private prepare identity conflict")
        if not request_path.exists():
            _atomic_write_managed_json(root, request_path, request)
        if not private_path.exists():
            _atomic_write_managed_json(root, private_path, private, private=True)
    return {
        "status": "prepared",
        "request_id": request_id,
        "generation_request_ref": request_path.relative_to(root).as_posix(),
        "generation_request_sha256": request_sha,
        "authoritative_answer_key_sha256": request["authoritative_answer_key_sha256"],
        "formal_write_count": 0,
    }


def prepare_cross_variant(
    repo: str | Path,
    evidence_path: str | Path,
    *,
    answer_key_private_root: str | Path | None = None,
) -> dict[str, Any]:
    """Public direct prepare; callers cannot bypass the personalization lock."""

    _retired_write_surface()

    # Historical implementation retained below for read-only source compatibility.
    root = Path(repo).expanduser().resolve()
    state, _, _ = _state_roots(root)
    with _lock(state / ".hot-path.lock"):
        return _prepare_cross_variant_hot_locked(
            root,
            evidence_path,
            answer_key_private_root=answer_key_private_root,
        )


def _replay_prepare_material(
    root: Path,
    *,
    evidence_path: Path,
    evidence_ref: str,
    private: dict[str, Any],
    request: dict[str, Any],
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Reconstruct issuance only from immutable, content-addressed evidence."""

    evidence = _load_object(evidence_path, "cross-variant authoring evidence")
    formal_node_id, edge_id, targets, author, verifier = _validated_evidence(evidence)
    evidence_sha = _require_digest(
        evidence_path, private.get("evidence_sha256"), "authoring evidence"
    )
    snapshot_ref = str(private.get("target_dependency_snapshot_ref") or "")
    snapshot_sha = str(private.get("target_dependency_snapshot_sha256") or "")
    snapshot = _load_dependency_snapshot(root, snapshot_ref, snapshot_sha)
    original = snapshot["original_source"]
    edge_proof = snapshot["cross_edge_semantic_proof"]
    if (
        snapshot.get("verified_cross_edge_id") != edge_id
        or snapshot.get("targets") != targets
        or original.get("formal_node_id") != formal_node_id
        or original.get("source_sha256") != evidence.get("original_source_sha256")
        or request.get("target_dependency_snapshot_ref") != snapshot_ref
        or request.get("target_dependency_snapshot_sha256") != snapshot_sha
    ):
        raise CrossVariantPipelineError(
            "generation request does not match its target dependency snapshot"
        )
    private_root, private_root_ref, private_root_binding = _managed_private_root(
        root, private.get("authoritative_answer_key_private_root_ref")
    )
    answer_key_path, answer_key_ref = _managed_private_answer_key(
        private_root, private.get("authoritative_answer_key_ref")
    )
    _, _, answer_key_sha = _authoritative_answer_key(
        answer_key_path,
        formal_node_id=formal_node_id,
        edge_id=edge_id,
        expected_sha256=private.get("authoritative_answer_key_sha256"),
    )
    if answer_key_sha != evidence.get("authoritative_answer_key_sha256"):
        raise CrossVariantPipelineError(
            "authoritative answer key no longer matches authoring evidence"
        )
    unsigned = _unsigned_request_from_dependencies(
        evidence_sha256=evidence_sha,
        formal_node_id=formal_node_id,
        original_source_ref=str(original["source_ref"]),
        original_source_sha256=str(original["source_sha256"]),
        answer_key_sha256=answer_key_sha,
        private_root_binding_sha256=private_root_binding,
        dependency_snapshot_ref=snapshot_ref,
        dependency_snapshot_sha256=snapshot_sha,
        target_binding_sha256=str(snapshot["target_semantic_binding_sha256"]),
        edge_binding_sha256=str(snapshot["cross_edge_semantic_binding_sha256"]),
        edge_id=edge_id,
        relation_code=str(edge_proof.get("relation_code") or ""),
        targets=targets,
        author=author,
        verifier=verifier,
    )
    expected_private = {
        "schema": PRIVATE_PREPARE_SCHEMA,
        "request_id": unsigned["request_id"],
        "evidence_ref": evidence_ref,
        "evidence_sha256": evidence_sha,
        "authoritative_answer_key_private_root_ref": private_root_ref,
        "authoritative_answer_key_private_root_binding_sha256": private_root_binding,
        "authoritative_answer_key_ref": answer_key_ref,
        "authoritative_answer_key_sha256": answer_key_sha,
        "target_dependency_snapshot_ref": snapshot_ref,
        "target_dependency_snapshot_sha256": snapshot_sha,
        "formal_write_count": 0,
    }
    return unsigned, expected_private


def _validate_request(
    root: Path, request_path: Path
) -> tuple[dict[str, Any], str, str, dict[str, Any]]:
    _, durable, work = _state_roots(root)
    request = _load_object(request_path, "generation request")
    request_sha = _sha256_file(request_path)
    request_id = str(request.get("request_id") or "")
    expected_path = (work / request_id / "requests" / f"{request_sha}.json").resolve()
    if (
        not REQUEST_ID_RE.fullmatch(request_id)
        or request_path.resolve() != expected_path
        or request_path.is_symlink()
        or request.get("schema") != REQUEST_SCHEMA
    ):
        raise CrossVariantPipelineError("generation request is not a managed prepare artifact")
    private_path = work / request_id / "private" / "prepare.json"
    private = _load_object(private_path, "private prepare")
    evidence_path, evidence_ref = _repo_file(
        root, private.get("evidence_ref"), "authoring evidence"
    )
    unsigned, current_private = _replay_prepare_material(
        root,
        evidence_path=evidence_path,
        evidence_ref=evidence_ref,
        private=private,
        request=request,
    )
    key = _prepare_key(root, durable, create=False)
    expected = _signed_request(unsigned, key)
    if (
        private != current_private
        or request != expected
        or not hmac.compare_digest(
            str(request.get("prepare_signature") or ""),
            str(expected.get("prepare_signature") or ""),
        )
        or _sha256_bytes(_json_bytes(expected)) != request_sha
    ):
        raise CrossVariantPipelineError("generation request or bound source has drifted")
    return request, request_sha, request_path.relative_to(root).as_posix(), private


def _current_question_index(root: Path) -> dict[str, Any]:
    state, _, _ = _state_roots(root)
    manifest, _ = _load_bounded_object(
        state / "manifest.json",
        "current personalization manifest",
        max_bytes=MAX_LIVE_CROSS_MANIFEST_BYTES,
    )
    expected = str(
        (manifest.get("artifact_hashes") or {}).get("question_index.json") or ""
    )
    if (
        manifest.get("schema") != "personalization_index_v1"
        or manifest.get("answer_safe") is not True
        or not _formal_boundary_is_zero(manifest)
        or not SHA256_RE.fullmatch(expected)
    ):
        raise CrossVariantPipelineError(
            "current personalization manifest cannot prove target dependencies"
        )
    question_index, actual = _load_bounded_object(
        state / "question_index.json",
        "current question index",
        max_bytes=MAX_QUESTION_INDEX_BYTES,
    )
    if actual != expected:
        raise CrossVariantPipelineError(
            "current question index is not root-manifest bound"
        )
    return question_index


def _validate_current_request_dependencies(
    root: Path, request: dict[str, Any]
) -> dict[str, Any]:
    """Fail closed on current source, selected witness, or selected edge drift."""

    snapshot = _load_dependency_snapshot(
        root,
        request.get("target_dependency_snapshot_ref"),
        request.get("target_dependency_snapshot_sha256"),
    )
    if (
        request.get("original_formal_node_id")
        != snapshot["original_source"]["formal_node_id"]
        or request.get("original_source_ref")
        != snapshot["original_source"]["source_ref"]
        or request.get("original_source_sha256")
        != snapshot["original_source"]["source_sha256"]
        or request.get("targets") != snapshot.get("targets")
        or request.get("verified_cross_edge_id")
        != snapshot.get("verified_cross_edge_id")
    ):
        raise CrossVariantPipelineError(
            "generation request does not match its target dependency snapshot"
        )
    try:
        validate_source_for_practice(
            root,
            snapshot["original_source"]["source_ref"],
            snapshot["original_source"]["source_sha256"],
            practice_mode="original",
            formal_node_id=str(snapshot["original_source"]["formal_node_id"]),
        )
    except LinkedPracticeSourceError as exc:
        raise CrossVariantPipelineError(
            "target original question source has drifted"
        ) from exc
    question_index = _current_question_index(root)
    current_target_proof = _validate_target_dependency_proof(
        question_index,
        list(snapshot["targets"]),
        snapshot["target_dependency_proof"],
    )
    if (
        _target_semantic_binding(current_target_proof)
        != snapshot["target_semantic_binding_sha256"]
    ):
        raise CrossVariantPipelineError(
            "target dependency witness has drifted from the current question index"
        )
    record = validate_current_live_cross_edge(
        root,
        edge_id=str(snapshot["verified_cross_edge_id"]),
        targets=list(snapshot["targets"]),
    )
    current_edge_proof = _edge_semantic_proof(
        record,
        edge_id=str(snapshot["verified_cross_edge_id"]),
        targets=list(snapshot["targets"]),
    )
    if current_edge_proof != snapshot["cross_edge_semantic_proof"]:
        raise CrossVariantPipelineError(
            "target cross edge semantics have drifted from the dependency snapshot"
        )
    return snapshot


def _validate_surface(path: Path) -> tuple[str, int]:
    if path.suffix.lower() != ".md" or path.stat().st_size > MAX_SURFACE_BYTES:
        raise CrossVariantPipelineError("question surface must be bounded UTF-8 Markdown")
    try:
        text = path.read_text(encoding="utf-8")
    except UnicodeError as exc:
        raise CrossVariantPipelineError("question surface must be UTF-8") from exc
    if not text.strip() or ANSWER_LEAK_RE.search(text):
        raise CrossVariantPipelineError("question surface contains answer or solution material")
    return _sha256_file(path), path.stat().st_size


def _single_existing(directory: Path, label: str) -> Path | None:
    if not directory.is_dir():
        return None
    files = [path for path in directory.iterdir() if path.is_file() and not path.is_symlink()]
    if len(files) > 1:
        raise CrossVariantPipelineError(f"multiple {label} artifacts exist")
    return files[0] if files else None


def commit_author_surface(
    repo: str | Path,
    generation_request_path: str | Path,
    question_surface_path: str | Path,
    author_submission_path: str | Path,
) -> dict[str, Any]:
    _retired_write_surface()

    # Historical implementation retained below for read-only source compatibility.
    root = Path(repo).expanduser().resolve()
    request_path, _ = _repo_file(root, generation_request_path, "generation request")
    surface_path, _ = _repo_file(root, question_surface_path, "question surface")
    submission_path, _ = _repo_file(root, author_submission_path, "author submission")
    request, request_sha, request_ref, _ = _validate_request(root, request_path)
    _validate_current_request_dependencies(root, request)
    surface_sha, surface_size = _validate_surface(surface_path)
    submission = _load_object(submission_path, "author submission")
    if set(submission) != {
        "schema",
        "generation_request_sha256",
        "author_identity",
        "question_surface_sha256",
        "formal_write_count",
    } or (
        submission.get("schema") != AUTHOR_SUBMISSION_SCHEMA
        or submission.get("generation_request_sha256") != request_sha
        or submission.get("author_identity") != request.get("author_identity")
        or submission.get("question_surface_sha256") != surface_sha
        or not _formal_boundary_is_zero(submission)
    ):
        raise CrossVariantPipelineError("author surface submission is invalid")
    _, durable, work = _state_roots(root)
    request_root = _guard_managed_linked_path(
        root,
        work / str(request["request_id"]),
        "cross-variant request work root",
    )
    surface_ref = (
        request_root / "surfaces" / f"{surface_sha}.md"
    ).relative_to(root).as_posix()
    submission_sha = _sha256_file(submission_path)
    submission_ref = (
        request_root / "author-submissions" / f"{submission_sha}.json"
    ).relative_to(root).as_posix()
    commit = {
        "schema": AUTHOR_COMMIT_SCHEMA,
        "request_id": request["request_id"],
        "generation_request_ref": request_ref,
        "generation_request_sha256": request_sha,
        "author_identity_sha256": request["author_identity_sha256"],
        "author_submission_ref": submission_ref,
        "author_submission_sha256": submission_sha,
        "question_surface_ref": surface_ref,
        "question_surface_sha256": surface_sha,
        "question_surface_size_bytes": surface_size,
        "answer_safe": True,
        "formal_write_count": 0,
    }
    commit_sha = _sha256_bytes(_json_bytes(commit))
    commit_path = _guard_managed_linked_path(
        root,
        request_root / "author-commits" / f"{commit_sha}.json",
        "cross-variant author commit path",
    )
    with _pipeline_lock(root, durable):
        existing = _single_existing(commit_path.parent, "author commit")
        if existing is not None and existing.resolve() != commit_path.resolve():
            raise CrossVariantPipelineError("author surface was already committed")
        _atomic_write_managed_bytes(
            root,
            request_root / "surfaces" / f"{surface_sha}.md",
            surface_path.read_bytes(),
        )
        _atomic_write_managed_bytes(
            root,
            request_root / "author-submissions" / f"{submission_sha}.json",
            submission_path.read_bytes(),
        )
        if not commit_path.exists():
            _atomic_write_managed_json(root, commit_path, commit)
    return {
        "status": "author_surface_committed",
        "request_id": request["request_id"],
        "author_commit_ref": commit_path.relative_to(root).as_posix(),
        "author_commit_sha256": _sha256_file(commit_path),
        "question_surface_sha256": surface_sha,
        "formal_write_count": 0,
    }


def _validate_author_commit(
    root: Path, commit_path: Path
) -> tuple[dict[str, Any], dict[str, Any], str, str]:
    commit = _load_object(commit_path, "author commit")
    commit_sha = _sha256_file(commit_path)
    request_path, _ = _repo_file(
        root, commit.get("generation_request_ref"), "generation request"
    )
    request, request_sha, request_ref, _ = _validate_request(root, request_path)
    _, _, work = _state_roots(root)
    expected_path = (
        work / str(request["request_id"]) / "author-commits" / f"{commit_sha}.json"
    ).resolve()
    surface_path, surface_ref = _repo_file(
        root, commit.get("question_surface_ref"), "committed question surface"
    )
    surface_sha, surface_size = _validate_surface(surface_path)
    submission_path, submission_ref = _repo_file(
        root, commit.get("author_submission_ref"), "managed author submission"
    )
    submission_sha = _sha256_file(submission_path)
    submission = _load_object(submission_path, "managed author submission")
    expected = {
        "schema": AUTHOR_COMMIT_SCHEMA,
        "request_id": request["request_id"],
        "generation_request_ref": request_ref,
        "generation_request_sha256": request_sha,
        "author_identity_sha256": request["author_identity_sha256"],
        "author_submission_ref": submission_ref,
        "author_submission_sha256": submission_sha,
        "question_surface_ref": surface_ref,
        "question_surface_sha256": surface_sha,
        "question_surface_size_bytes": surface_size,
        "answer_safe": True,
        "formal_write_count": 0,
    }
    if (
        commit_path.resolve() != expected_path
        or commit != expected
        or commit_sha != _sha256_bytes(_json_bytes(expected))
        or submission != {
            "schema": AUTHOR_SUBMISSION_SCHEMA,
            "generation_request_sha256": request_sha,
            "author_identity": request["author_identity"],
            "question_surface_sha256": surface_sha,
            "formal_write_count": 0,
        }
    ):
        raise CrossVariantPipelineError("author commit is invalid or drifted")
    return commit, request, commit_sha, commit_path.relative_to(root).as_posix()


def _canonical_mechanisms(raw: object) -> list[dict[str, str]]:
    return _canonical_targets(raw)


def commit_blind_answer(
    repo: str | Path,
    author_commit_path: str | Path,
    blind_answer_submission_path: str | Path,
) -> dict[str, Any]:
    _retired_write_surface()

    # Historical implementation retained below for read-only source compatibility.
    root = Path(repo).expanduser().resolve()
    commit_path, _ = _repo_file(root, author_commit_path, "author commit")
    submission_path, _ = _repo_file(
        root, blind_answer_submission_path, "blind answer submission"
    )
    author_commit, request, author_commit_sha, author_commit_ref = _validate_author_commit(
        root, commit_path
    )
    _validate_current_request_dependencies(root, request)
    submission, _ = _load_bounded_object(
        submission_path,
        "blind answer submission",
        max_bytes=MAX_BLIND_SUBMISSION_BYTES,
    )
    if set(submission) != {
        "schema",
        "author_commit_sha256",
        "verifier_identity",
        "unique_answer",
        "answer_set",
        "mechanisms_used",
        "semantic_uniqueness_evidence",
        "semantic_uniqueness_evidence_sha256",
        "structural_change_kind",
        "both_subjects_essential",
        "solved_without_authoritative_key",
        "formal_write_count",
    } or (
        submission.get("schema") != BLIND_SUBMISSION_SCHEMA
        or submission.get("author_commit_sha256") != author_commit_sha
        or submission.get("verifier_identity") != request.get("verifier_identity")
        or submission.get("unique_answer") is not True
        or submission.get("both_subjects_essential") is not True
        or submission.get("solved_without_authoritative_key") is not True
        or not _formal_boundary_is_zero(submission)
    ):
        raise CrossVariantPipelineError("blind answer submission is invalid")
    answer_set = _strict_canonical_answer_set(
        submission.get("answer_set"), "blind answer set"
    )
    mechanisms = _canonical_mechanisms(submission.get("mechanisms_used"))
    if mechanisms != request.get("targets"):
        raise CrossVariantPipelineError("blind verifier did not use both bound mechanisms")
    semantic_evidence, semantic_recomputation = _semantic_uniqueness_evidence(
        submission.get("semantic_uniqueness_evidence"),
        targets=mechanisms,
        blind_answer_set=answer_set,
    )
    semantic_sha = _canonical_digest(semantic_evidence)
    if submission.get("semantic_uniqueness_evidence_sha256") != semantic_sha:
        raise CrossVariantPipelineError(
            "semantic uniqueness evidence sha256 does not match canonical evidence"
        )
    if (
        semantic_recomputation.get("semantic_uniqueness_evidence_sha256")
        != semantic_sha
    ):
        raise CrossVariantPipelineError(
            "semantic uniqueness machine recomputation is inconsistent"
        )
    structural = str(submission.get("structural_change_kind") or "")
    if structural == "numbers_only" or structural not in ALLOWED_STRUCTURAL_CHANGES:
        raise CrossVariantPipelineError("numbers-only or unverified structural change is forbidden")
    _, durable, work = _state_roots(root)
    request_root = _guard_managed_linked_path(
        root,
        work / str(request["request_id"]),
        "cross-variant request work root",
    )
    reveals_root = _guard_managed_linked_path(
        root, request_root / "reveals", "cross-variant reveals root"
    )
    if _single_existing(reveals_root, "reveal") is not None:
        raise CrossVariantPipelineError("blind answer must be committed before reveal")
    blind = {
        "schema": BLIND_COMMIT_SCHEMA,
        "request_id": request["request_id"],
        "generation_request_ref": author_commit["generation_request_ref"],
        "generation_request_sha256": author_commit["generation_request_sha256"],
        "author_commit_ref": author_commit_ref,
        "author_commit_sha256": author_commit_sha,
        "verifier_identity_sha256": request["verifier_identity_sha256"],
        "unique_answer": True,
        "answer_set": answer_set,
        "mechanisms_used": mechanisms,
        "semantic_uniqueness_evidence": semantic_evidence,
        "semantic_uniqueness_evidence_sha256": semantic_sha,
        "structural_change_kind": structural,
        "both_subjects_essential": True,
        "solved_without_authoritative_key": True,
        "formal_write_count": 0,
    }
    blind_sha = _sha256_bytes(_json_bytes(blind))
    blind_path = _guard_managed_linked_path(
        root,
        request_root / "blind-commits" / f"{blind_sha}.json",
        "cross-variant blind commit path",
    )
    with _pipeline_lock(root, durable):
        _guard_managed_linked_path(root, reveals_root, "cross-variant reveals root")
        if _single_existing(reveals_root, "reveal") is not None:
            raise CrossVariantPipelineError("blind answer must be committed before reveal")
        existing = _single_existing(blind_path.parent, "blind answer commit")
        if existing is not None and existing.resolve() != blind_path.resolve():
            raise CrossVariantPipelineError("a different blind answer was already committed")
        if not blind_path.exists():
            _atomic_write_managed_json(root, blind_path, blind, private=True)
    return {
        "status": "blind_answer_committed",
        "request_id": request["request_id"],
        "blind_commit_ref": blind_path.relative_to(root).as_posix(),
        "blind_commit_sha256": _sha256_file(blind_path),
        "formal_write_count": 0,
    }


def _validate_blind_commit(
    root: Path, blind_path: Path
) -> tuple[dict[str, Any], dict[str, Any], dict[str, Any], str, str, dict[str, Any]]:
    blind = _load_object(blind_path, "blind answer commit")
    blind_sha = _sha256_file(blind_path)
    author_path, _ = _repo_file(root, blind.get("author_commit_ref"), "author commit")
    author, request, author_sha, author_ref = _validate_author_commit(root, author_path)
    _, _, work = _state_roots(root)
    expected_path = (
        work / str(request["request_id"]) / "blind-commits" / f"{blind_sha}.json"
    ).resolve()
    mechanisms = _canonical_mechanisms(blind.get("mechanisms_used"))
    answer_set = _strict_canonical_answer_set(
        blind.get("answer_set"), "blind answer set"
    )
    semantic_evidence, semantic_recomputation = _semantic_uniqueness_evidence(
        blind.get("semantic_uniqueness_evidence"),
        targets=mechanisms,
        blind_answer_set=answer_set,
    )
    semantic_sha = _canonical_digest(semantic_evidence)
    expected = {
        "schema": BLIND_COMMIT_SCHEMA,
        "request_id": request["request_id"],
        "generation_request_ref": author["generation_request_ref"],
        "generation_request_sha256": author["generation_request_sha256"],
        "author_commit_ref": author_ref,
        "author_commit_sha256": author_sha,
        "verifier_identity_sha256": request["verifier_identity_sha256"],
        "unique_answer": True,
        "answer_set": answer_set,
        "mechanisms_used": mechanisms,
        "semantic_uniqueness_evidence": semantic_evidence,
        "semantic_uniqueness_evidence_sha256": semantic_sha,
        "structural_change_kind": blind.get("structural_change_kind"),
        "both_subjects_essential": True,
        "solved_without_authoritative_key": True,
        "formal_write_count": 0,
    }
    structural = str(blind.get("structural_change_kind") or "")
    if (
        blind_path.resolve() != expected_path
        or blind != expected
        or blind_sha != _sha256_bytes(_json_bytes(expected))
        or mechanisms != request.get("targets")
        or blind.get("semantic_uniqueness_evidence_sha256") != semantic_sha
        or structural not in ALLOWED_STRUCTURAL_CHANGES
    ):
        raise CrossVariantPipelineError("blind answer commit is invalid or drifted")
    private_path = work / str(request["request_id"]) / "private" / "prepare.json"
    private = _load_object(private_path, "private prepare")
    private_root, _, private_root_binding = _managed_private_root(
        root, private.get("authoritative_answer_key_private_root_ref")
    )
    if (
        private_root_binding
        != request.get("authoritative_answer_key_private_root_binding_sha256")
        or private_root_binding
        != private.get("authoritative_answer_key_private_root_binding_sha256")
    ):
        raise CrossVariantPipelineError(
            "authoritative answer key private root binding mismatch"
        )
    key_path, key_ref = _managed_private_answer_key(
        private_root, private.get("authoritative_answer_key_ref")
    )
    if key_ref != private.get("authoritative_answer_key_ref"):
        raise CrossVariantPipelineError(
            "authoritative answer key private locator drifted"
        )
    answer_key, authoritative, _ = _authoritative_answer_key(
        key_path,
        formal_node_id=str(request["original_formal_node_id"]),
        edge_id=str(request["verified_cross_edge_id"]),
        expected_sha256=request.get("authoritative_answer_key_sha256"),
    )
    return blind, author, request, blind_sha, blind_path.relative_to(root).as_posix(), {
        "payload": answer_key,
        "answer_set": authoritative,
        "semantic_uniqueness_recomputation": semantic_recomputation,
    }


def reveal_and_compare(repo: str | Path, blind_commit_path: str | Path) -> dict[str, Any]:
    _retired_write_surface()

    # Historical implementation retained below for read-only source compatibility.
    root = Path(repo).expanduser().resolve()
    blind_path, _ = _repo_file(root, blind_commit_path, "blind answer commit")
    blind, author, request, blind_sha, blind_ref, key = _validate_blind_commit(
        root, blind_path
    )
    _validate_current_request_dependencies(root, request)
    if blind["answer_set"] != key["answer_set"]:
        raise CrossVariantPipelineError("blind answer does not match authoritative unique answer")
    semantic_recomputation = key["semantic_uniqueness_recomputation"]
    if (
        semantic_recomputation.get("joint_satisfying_count") != 1
        or any(
            count < 2
            for count in semantic_recomputation.get(
                "leave_one_subject_out_satisfying_counts", {}
            ).values()
        )
    ):
        raise CrossVariantPipelineError(
            "semantic uniqueness machine recomputation no longer proves both subjects"
        )
    comparison = _canonical_digest(
        {
            "schema": "cross_variant_answer_comparison_v1",
            "blind_answer_set": blind["answer_set"],
            "authoritative_answer_set": key["answer_set"],
        }
    )
    semantic_comparison = _canonical_digest(semantic_recomputation)
    reveal = {
        "schema": REVEAL_SCHEMA,
        "status": "verified_match",
        "request_id": request["request_id"],
        "generation_request_sha256": author["generation_request_sha256"],
        "author_commit_sha256": blind["author_commit_sha256"],
        "blind_commit_ref": blind_ref,
        "blind_commit_sha256": blind_sha,
        "authoritative_answer_key_sha256": request["authoritative_answer_key_sha256"],
        "machine_comparison_sha256": comparison,
        "semantic_uniqueness_evidence_sha256": blind[
            "semantic_uniqueness_evidence_sha256"
        ],
        "semantic_uniqueness_recomputation_sha256": semantic_comparison,
        "unique_answer_verified": True,
        "both_subjects_verified": True,
        "structural_change_kind": blind["structural_change_kind"],
        "formal_write_count": 0,
    }
    reveal_sha = _sha256_bytes(_json_bytes(reveal))
    _, durable, work = _state_roots(root)
    reveal_path = _guard_managed_linked_path(
        root,
        work / str(request["request_id"]) / "reveals" / f"{reveal_sha}.json",
        "cross-variant reveal path",
    )
    with _pipeline_lock(root, durable):
        existing = _single_existing(reveal_path.parent, "reveal")
        if existing is not None and existing.resolve() != reveal_path.resolve():
            raise CrossVariantPipelineError("a different reveal receipt already exists")
        if not reveal_path.exists():
            _atomic_write_managed_json(root, reveal_path, reveal, private=True)
    return {
        "status": "machine_verified",
        "request_id": request["request_id"],
        "reveal_receipt_ref": reveal_path.relative_to(root).as_posix(),
        "reveal_receipt_sha256": _sha256_file(reveal_path),
        "formal_write_count": 0,
    }


def _validate_reveal(
    root: Path, reveal_path: Path
) -> tuple[dict[str, Any], dict[str, Any], dict[str, Any], dict[str, Any], str]:
    reveal = _load_object(reveal_path, "reveal receipt")
    reveal_sha = _sha256_file(reveal_path)
    blind_path, _ = _repo_file(root, reveal.get("blind_commit_ref"), "blind answer commit")
    blind, author, request, blind_sha, blind_ref, key = _validate_blind_commit(root, blind_path)
    if blind["answer_set"] != key["answer_set"]:
        raise CrossVariantPipelineError("blind answer no longer matches authoritative answer")
    semantic_recomputation = key["semantic_uniqueness_recomputation"]
    if (
        semantic_recomputation.get("joint_satisfying_count") != 1
        or any(
            count < 2
            for count in semantic_recomputation.get(
                "leave_one_subject_out_satisfying_counts", {}
            ).values()
        )
    ):
        raise CrossVariantPipelineError(
            "semantic uniqueness machine recomputation no longer proves both subjects"
        )
    comparison = _canonical_digest(
        {
            "schema": "cross_variant_answer_comparison_v1",
            "blind_answer_set": blind["answer_set"],
            "authoritative_answer_set": key["answer_set"],
        }
    )
    semantic_comparison = _canonical_digest(semantic_recomputation)
    expected = {
        "schema": REVEAL_SCHEMA,
        "status": "verified_match",
        "request_id": request["request_id"],
        "generation_request_sha256": author["generation_request_sha256"],
        "author_commit_sha256": blind["author_commit_sha256"],
        "blind_commit_ref": blind_ref,
        "blind_commit_sha256": blind_sha,
        "authoritative_answer_key_sha256": request["authoritative_answer_key_sha256"],
        "machine_comparison_sha256": comparison,
        "semantic_uniqueness_evidence_sha256": blind[
            "semantic_uniqueness_evidence_sha256"
        ],
        "semantic_uniqueness_recomputation_sha256": semantic_comparison,
        "unique_answer_verified": True,
        "both_subjects_verified": True,
        "structural_change_kind": blind["structural_change_kind"],
        "formal_write_count": 0,
    }
    _, _, work = _state_roots(root)
    expected_path = (
        work / str(request["request_id"]) / "reveals" / f"{reveal_sha}.json"
    ).resolve()
    if (
        reveal_path.resolve() != expected_path
        or reveal != expected
        or reveal_sha != _sha256_bytes(_json_bytes(expected))
    ):
        raise CrossVariantPipelineError("reveal receipt is invalid or drifted")
    return reveal, blind, author, request, reveal_sha


def _source_ids(
    request: dict[str, Any],
    *,
    surface_sha: str,
    blind_sha: str,
    reveal_sha: str,
) -> tuple[str, str]:
    subjects = sorted(str(value) for value in request["subjects"])
    suffix = _canonical_digest(
        {
            "request_id": request["request_id"],
            "question_surface_sha256": surface_sha,
            "blind_commit_sha256": blind_sha,
            "reveal_receipt_sha256": reveal_sha,
        }
    )[:16].upper()
    return (
        f"CVAR-{subjects[0]}-{subjects[1]}-{suffix}",
        f"CVVR-{subjects[0]}-{subjects[1]}-{suffix}",
    )


def _public_receipt(
    request: dict[str, Any],
    author: dict[str, Any],
    blind: dict[str, Any],
    *,
    surface_sha: str,
    reveal_sha: str,
) -> dict[str, Any]:
    source_id, receipt_id = _source_ids(
        request,
        surface_sha=surface_sha,
        blind_sha=_sha256_bytes(_json_bytes(blind)),
        reveal_sha=reveal_sha,
    )
    return {
        "schema": PUBLIC_RECEIPT_SCHEMA,
        "receipt_id": receipt_id,
        "source_id": source_id,
        "request_id": request["request_id"],
        "generation_request_ref": author["generation_request_ref"],
        "generation_request_sha256": author["generation_request_sha256"],
        "original_formal_node_id": request["original_formal_node_id"],
        "original_source_sha256": request["original_source_sha256"],
        "target_dependency_snapshot_ref": request[
            "target_dependency_snapshot_ref"
        ],
        "target_dependency_snapshot_sha256": request[
            "target_dependency_snapshot_sha256"
        ],
        "target_semantic_binding_sha256": request[
            "target_semantic_binding_sha256"
        ],
        "cross_edge_semantic_binding_sha256": request[
            "cross_edge_semantic_binding_sha256"
        ],
        "verified_cross_edge_id": request["verified_cross_edge_id"],
        "targets": request["targets"],
        "subjects": request["subjects"],
        "author_identity_sha256": request["author_identity_sha256"],
        "verifier_identity_sha256": request["verifier_identity_sha256"],
        "author_commit_sha256": blind["author_commit_sha256"],
        "blind_answer_commit_sha256": _sha256_bytes(_json_bytes(blind)),
        "reveal_receipt_sha256": reveal_sha,
        "authoritative_answer_key_sha256": request["authoritative_answer_key_sha256"],
        "question_surface_sha256": surface_sha,
        "structural_change_kind": blind["structural_change_kind"],
        "answer_isolated": True,
        "unique_answer_verified": True,
        "both_subjects_verified": True,
        "blind_verification_verified": True,
        "formal_write_count": 0,
    }


def _check_personalization_manifest(root: Path) -> dict[str, Any]:
    state, _, _ = _state_roots(root)
    path = state / "manifest.json"
    manifest = _load_object(path, "personalization manifest")
    if manifest.get("answer_safe") is not True or not _formal_boundary_is_zero(manifest):
        raise CrossVariantPipelineError("personalization manifest violates derived boundary")
    return manifest


def _private_path_by_hash(root: Path, request_id: str, folder: str, digest: str) -> Path:
    if not SHA256_RE.fullmatch(digest):
        raise CrossVariantPipelineError(f"{folder} hash is invalid")
    _, _, work = _state_roots(root)
    return work / request_id / folder / f"{digest}.json"


def _validate_public_chain(
    root: Path,
    receipt: dict[str, Any],
    *,
    surface_sha: str,
) -> tuple[dict[str, Any], dict[str, Any]]:
    request_path, _ = _repo_file(
        root, receipt.get("generation_request_ref"), "generation request"
    )
    request, request_sha, _, _ = _validate_request(root, request_path)
    request_id = str(request["request_id"])
    author_path = _private_path_by_hash(
        root, request_id, "author-commits", str(receipt.get("author_commit_sha256") or "")
    )
    blind_path = _private_path_by_hash(
        root,
        request_id,
        "blind-commits",
        str(receipt.get("blind_answer_commit_sha256") or ""),
    )
    reveal_path = _private_path_by_hash(
        root, request_id, "reveals", str(receipt.get("reveal_receipt_sha256") or "")
    )
    author, checked_request, _, _ = _validate_author_commit(root, author_path)
    blind, _, blind_request, _, _, _ = _validate_blind_commit(root, blind_path)
    _, reveal_blind, _, reveal_request, reveal_sha = _validate_reveal(root, reveal_path)
    if not (request == checked_request == blind_request == reveal_request) or blind != reveal_blind:
        raise CrossVariantPipelineError("public verification chain is inconsistent")
    expected = _public_receipt(
        request, author, blind, surface_sha=surface_sha, reveal_sha=reveal_sha
    )
    if receipt != expected or request_sha != receipt.get("generation_request_sha256"):
        raise CrossVariantPipelineError("public verification receipt is invalid")
    return request, blind


def _manifest_entry(
    root: Path, manifest_path: Path
) -> tuple[dict[str, Any], dict[str, Any]]:
    manifest = _load_object(manifest_path, "cross-variant manifest")
    source_id = str(manifest.get("source_id") or "")
    if manifest.get("schema") != PUBLIC_MANIFEST_SCHEMA or not SOURCE_ID_RE.fullmatch(source_id):
        raise CrossVariantPipelineError("cross-variant manifest identity is invalid")
    declared = {manifest_path.resolve()}
    assets = manifest.get("question_assets")
    if not isinstance(assets, list) or len(assets) != 1 or not isinstance(assets[0], dict):
        raise CrossVariantPipelineError("cross variant must publish one question-only surface")
    asset = assets[0]
    surface = (manifest_path.parent / str(asset.get("path") or "")).resolve()
    receipt_spec = manifest.get("verification_receipt")
    if not isinstance(receipt_spec, dict):
        raise CrossVariantPipelineError("cross variant lacks a public receipt")
    receipt_path = (manifest_path.parent / str(receipt_spec.get("path") or "")).resolve()
    declared.update({surface, receipt_path})
    actual = {
        path.resolve()
        for path in manifest_path.parent.rglob("*")
        if path.is_file() and not path.is_symlink()
    }
    if actual != declared or manifest_path.parent.name != source_id:
        raise CrossVariantPipelineError("public source directory contains undeclared material")
    surface_sha, surface_size = _validate_surface(surface)
    if (
        asset != {
            "path": surface.name,
            "sha256": surface_sha,
            "size_bytes": surface_size,
        }
        or _require_digest(receipt_path, receipt_spec.get("sha256"), "public receipt")
        != receipt_spec.get("sha256")
    ):
        raise CrossVariantPipelineError("public question surface or receipt has drifted")
    receipt = _load_object(receipt_path, "public receipt")
    request, blind = _validate_public_chain(root, receipt, surface_sha=surface_sha)
    expected_manifest = {
        "schema": PUBLIC_MANIFEST_SCHEMA,
        "source_id": source_id,
        "original_formal_node_id": request["original_formal_node_id"],
        "verified_cross_edge_id": request["verified_cross_edge_id"],
        "subjects": request["subjects"],
        "targets": request["targets"],
        "target_dependency_snapshot_ref": request[
            "target_dependency_snapshot_ref"
        ],
        "target_dependency_snapshot_sha256": request[
            "target_dependency_snapshot_sha256"
        ],
        "structural_change_kind": blind["structural_change_kind"],
        "answer_isolated": True,
        "unique_answer_verified": True,
        "both_subjects_verified": True,
        "question_asset_count": 1,
        "question_assets": [asset],
        "verification_receipt": receipt_spec,
        "authoritative_answer_key_sha256": request["authoritative_answer_key_sha256"],
        "formal_write_count": 0,
    }
    if manifest != expected_manifest or receipt.get("source_id") != source_id:
        raise CrossVariantPipelineError("cross-variant manifest does not match verified chain")
    return {
        "source_id": source_id,
        "source_ref": manifest_path.relative_to(root).as_posix(),
        "source_sha256": _sha256_file(manifest_path),
        "original_formal_node_id": request["original_formal_node_id"],
        "verified_cross_edge_id": request["verified_cross_edge_id"],
        "subjects": request["subjects"],
        "targets": request["targets"],
        "target_dependency_snapshot_ref": request[
            "target_dependency_snapshot_ref"
        ],
        "target_dependency_snapshot_sha256": request[
            "target_dependency_snapshot_sha256"
        ],
        "structural_change_kind": blind["structural_change_kind"],
        "authoritative_answer_key_sha256": request["authoritative_answer_key_sha256"],
        "verification_receipt_sha256": receipt_spec["sha256"],
        "status": "verified_ready",
    }, request


def validate_published_cross_variant_source(
    repo: str | Path,
    manifest_path: str | Path,
    *,
    expected_original_formal_node_id: str | None = None,
) -> dict[str, Any]:
    """Deep-validate one published source for linked-practice replay.

    This public adapter exposes only answer-safe metadata.  It deliberately
    replays the private blind/reveal chain instead of trusting booleans in the
    public manifest, and it never returns an answer-key locator or answer set.
    """

    root = Path(repo).expanduser().resolve()
    path, _ = _repo_file(root, manifest_path, "cross-variant manifest")
    entry, request = _manifest_entry(root, path)
    anchor = str(entry["original_formal_node_id"])
    if (
        expected_original_formal_node_id is not None
        and anchor != expected_original_formal_node_id
    ):
        raise CrossVariantPipelineError(
            "cross variant does not match the expected original formal node"
        )
    _validate_current_request_dependencies(root, request)
    manifest = _load_object(path, "cross-variant manifest")
    receipt_path = path.parent / str(
        (manifest.get("verification_receipt") or {}).get("path") or ""
    )
    receipt = _load_object(receipt_path, "cross-variant public receipt")
    return {
        "kind": "cross_variant_manifest",
        "manifest_path": str(path),
        "source_id": entry["source_id"],
        "anchor_formal_node_id": anchor,
        "original_formal_node_id": anchor,
        "verified_cross_edge_id": entry["verified_cross_edge_id"],
        "subjects": entry["subjects"],
        "targets": entry["targets"],
        "asset_count": int(manifest["question_asset_count"]),
        "verification_receipt_id": receipt["receipt_id"],
        "answer_isolated": True,
        "unique_answer_verified": True,
    }


def rebuild_cross_variant_catalog(repo: str | Path) -> dict[str, Any]:
    root = Path(repo).expanduser().resolve()
    _, durable, _ = _state_roots(root)
    source_root = durable / "cross-variant-sources"
    entries: list[dict[str, Any]] = []
    if source_root.is_dir():
        for manifest_path in sorted(source_root.glob("CVAR-*/manifest.json")):
            entry, _ = _manifest_entry(root, manifest_path)
            entries.append(entry)
    entries.sort(key=lambda item: (item["verified_cross_edge_id"], item["source_id"]))
    source_ids = [item["source_id"] for item in entries]
    if len(source_ids) != len(set(source_ids)):
        raise CrossVariantPipelineError("cross-variant source ID collision")
    by_edge: dict[str, list[str]] = {}
    by_point: dict[str, list[str]] = {}
    for item in entries:
        by_edge.setdefault(item["verified_cross_edge_id"], []).append(item["source_id"])
        for target in item["targets"]:
            by_point.setdefault(target["point_id"], []).append(item["source_id"])
    return {
        "schema": CATALOG_SCHEMA,
        "kind": "verified_cross_variant_catalog",
        "formal_write_count": 0,
        "entry_count": len(entries),
        "entries": entries,
        "by_edge": dict(sorted(by_edge.items())),
        "by_point": dict(sorted(by_point.items())),
    }


def validate_cross_variant_catalog(repo: str | Path, payload: dict[str, Any]) -> dict[str, Any]:
    if (
        not isinstance(payload, dict)
        or payload.get("schema") != CATALOG_SCHEMA
        or not _formal_boundary_is_zero(payload)
    ):
        raise CrossVariantPipelineError("cross-variant catalog violates derived boundary")
    rebuilt = rebuild_cross_variant_catalog(repo)
    if _json_bytes(payload, pretty=False) != _json_bytes(rebuilt, pretty=False):
        raise CrossVariantPipelineError("cross-variant catalog is not a complete current rebuild")
    return rebuilt


def _publish_catalog(root: Path) -> tuple[dict[str, Any], str]:
    _retired_write_surface()

    # Historical implementation retained below for read-only source compatibility.
    state, _, _ = _state_roots(root)
    manifest_path = state / "manifest.json"
    manifest = _check_personalization_manifest(root)
    catalog = rebuild_cross_variant_catalog(root)
    catalog_sha = _atomic_write_json(state / CATALOG_FILENAME, catalog)
    manifest.setdefault("artifact_hashes", {})[CATALOG_FILENAME] = catalog_sha
    manifest.setdefault("counts", {})["verified_cross_variant_sources"] = catalog[
        "entry_count"
    ]
    manifest.setdefault("independent_projections", {})["cross_variant_catalog"] = {
        "file": CATALOG_FILENAME,
        "included_in_this_build": True,
    }
    manifest["cross_variant_catalog_sha256"] = catalog_sha
    runtime_material = {
        "build_id": manifest.get("build_id"),
        "learner": (manifest.get("artifact_hashes") or {}).get("learner_state.json"),
        "questions": (manifest.get("artifact_hashes") or {}).get("question_index.json"),
        "variants": (manifest.get("artifact_hashes") or {}).get("variant-catalog.json"),
        "cross_variants": catalog_sha,
    }
    manifest["runtime_projection_id"] = _canonical_digest(runtime_material)
    _atomic_write_json(manifest_path, manifest)
    return catalog, catalog_sha


def _finalize_cross_variant_locked(
    root: Path, reveal_receipt_path: str | Path
) -> dict[str, Any]:
    _retired_write_surface()

    # Historical implementation retained below for read-only source compatibility.
    reveal_path, _ = _repo_file(root, reveal_receipt_path, "reveal receipt")
    reveal, blind, author, request, reveal_sha = _validate_reveal(root, reveal_path)
    _validate_current_request_dependencies(root, request)
    _check_personalization_manifest(root)
    surface_path, _ = _repo_file(
        root, author.get("question_surface_ref"), "committed question surface"
    )
    surface_sha, surface_size = _validate_surface(surface_path)
    source_id, _ = _source_ids(
        request,
        surface_sha=surface_sha,
        blind_sha=_sha256_bytes(_json_bytes(blind)),
        reveal_sha=reveal_sha,
    )
    public_receipt = _public_receipt(
        request, author, blind, surface_sha=surface_sha, reveal_sha=reveal_sha
    )
    _, durable, _ = _state_roots(root)
    source_dir = _guard_managed_linked_path(
        root,
        durable / "cross-variant-sources" / source_id,
        "cross-variant published source root",
    )
    asset_path = source_dir / "cross-variant-question-01.md"
    receipt_path = source_dir / "verification.json"
    manifest_path = source_dir / "manifest.json"
    _atomic_write_managed_bytes(root, asset_path, surface_path.read_bytes())
    receipt_sha = _atomic_write_managed_json(root, receipt_path, public_receipt)
    manifest = {
        "schema": PUBLIC_MANIFEST_SCHEMA,
        "source_id": source_id,
        "original_formal_node_id": request["original_formal_node_id"],
        "verified_cross_edge_id": request["verified_cross_edge_id"],
        "subjects": request["subjects"],
        "targets": request["targets"],
        "target_dependency_snapshot_ref": request[
            "target_dependency_snapshot_ref"
        ],
        "target_dependency_snapshot_sha256": request[
            "target_dependency_snapshot_sha256"
        ],
        "structural_change_kind": blind["structural_change_kind"],
        "answer_isolated": True,
        "unique_answer_verified": True,
        "both_subjects_verified": True,
        "question_asset_count": 1,
        "question_assets": [
            {
                "path": asset_path.name,
                "sha256": surface_sha,
                "size_bytes": surface_size,
            }
        ],
        "verification_receipt": {
            "path": receipt_path.name,
            "sha256": receipt_sha,
        },
        "authoritative_answer_key_sha256": request["authoritative_answer_key_sha256"],
        "formal_write_count": 0,
    }
    manifest_sha = _atomic_write_managed_json(root, manifest_path, manifest)
    _manifest_entry(root, manifest_path)
    catalog, catalog_sha = _publish_catalog(root)
    return {
        "status": "verified_ready",
        "source_id": source_id,
        "source_ref": manifest_path.relative_to(root).as_posix(),
        "source_sha256": manifest_sha,
        "cross_variant_catalog_sha256": catalog_sha,
        "verified_cross_variant_sources": catalog["entry_count"],
        "formal_write_count": 0,
    }


def finalize_cross_variant(repo: str | Path, reveal_receipt_path: str | Path) -> dict[str, Any]:
    _retired_write_surface()

    # Historical implementation retained below for read-only source compatibility.
    root = Path(repo).expanduser().resolve()
    state, durable, _ = _state_roots(root)
    # Any operation requiring both locks uses one global order.  Validation,
    # source publication, and personalization catalog publication share this
    # same locked snapshot.
    with _lock(state / ".hot-path.lock"):
        with _pipeline_lock(root, durable):
            return _finalize_cross_variant_locked(root, reveal_receipt_path)


def publish_cross_variant_catalog(repo: str | Path) -> dict[str, Any]:
    """Rebuild and publish the managed catalog under the global lock order."""

    _retired_write_surface()

    # Historical implementation retained below for read-only source compatibility.
    root = Path(repo).expanduser().resolve()
    state, durable, _ = _state_roots(root)
    with _lock(state / ".hot-path.lock"):
        with _pipeline_lock(root, durable):
            catalog, digest = _publish_catalog(root)
    return {
        "status": "rebuilt",
        "cross_variant_catalog_sha256": digest,
        "verified_cross_variant_sources": catalog["entry_count"],
        "formal_write_count": 0,
    }


# Explicit aliases keep the public Python API readable at each protocol stage.
prepare_generation = prepare_cross_variant
commit_surface = commit_author_surface
commit_blind = commit_blind_answer
reveal_authoritative_answer = reveal_and_compare
finalize_verified_cross_variant = finalize_cross_variant


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo", type=Path, default=Path.cwd())
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("inspect-catalog")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        result = rebuild_cross_variant_catalog(args.repo)
    except CrossVariantPipelineError as exc:
        print(json.dumps({"status": "error", "reason": str(exc)}, ensure_ascii=False))
        return 2
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    sys.exit(main())
