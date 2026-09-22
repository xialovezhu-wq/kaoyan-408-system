#!/usr/bin/env python3
"""Prepare a complete frozen 408 morning pack outside the learner turn.

This module owns only a rebuildable delivery sidecar.  It never starts, answers,
advances, or closes a morning session and it never writes formal study facts.
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
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import Any

import morning_review_prepared_pack_408 as prepared_pack
import generate_morning_action_queue as queue_generation
import review_feedback_loop as review_loop


JOB_SCHEMA = "morning_review_preparation_job_v1"
STATE_SCHEMA = "morning_review_preparation_state_v1"
POINTER_SCHEMA = "morning_review_preparation_pointer_v1"
CURRENT_POINTER_SCHEMA = "morning_review_preparation_current_pointer_v1"
RECEIPT_SCHEMA = "morning_review_preparation_receipt_v1"
MODEL_OUTPUT_SCHEMA = "morning_review_preparation_model_output_v1"
POLICY_VERSION = "prepare-next-morning-v2-current-sources"
JOB_ROOT_REL = Path(
    "wiki/study_vaults/408-full/state/morning-review-preparation-jobs"
)
QUEUE_ROOT_REL = Path(
    "wiki/study_vaults/408-full/StudyVault/00-Dashboard"
)
HEX64 = re.compile(r"[0-9a-f]{64}")
SAFE_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:@+-]{2,159}")
MAX_JSON_BYTES = 8 * 1024 * 1024
DEFAULT_CODEX = Path("/Applications/ChatGPT.app/Contents/Resources/codex")
DEFAULT_MODEL = "gpt-5.6-sol"
DEFAULT_REASONING = "max"
DEFAULT_TIMEOUT_SECONDS = 1800
CHUNK_SIZE = 4
RETRY_BASE_SECONDS = 300
RETRY_MAX_SECONDS = 3600
MCP_POINTER = Path(
    "/Users/your-user/.codex/local-study-read-mcp/morning-preparation-current.json"
)
MCP_PYTHON = Path(
    "/Users/your-user/Documents/Codex/local-study-read-mcp/.venv/bin/python"
)

AUTHOR_SOURCE_GROUNDING_CONTRACT = """Source-grounding contract:
- The learning target and knowledge mechanism must be directly supported by the evidence content.
- Create a new derived variant only when the evidence content explicitly authorizes migration,
  parameter changes, scenario changes, or question-form changes.
- For an explicitly authorized variant, synthetic exact parameter values do not need to appear
  verbatim in the evidence. They must obey every evidence-stated order of magnitude, unit,
  boundary, and mechanism. The public stem must state the constructed parameters, be
  self-contained, and determine one unique best answer from its own facts. Never describe
  synthetic values as facts from the original or source question.
- If a derived variant lacks explicit migration authorization or enough evidence about scale,
  units, or boundaries to construct it safely, return needs_source for that item. Do not guess
  a convenient scale or silently reconstruct the original question.
- Migration permission does not relax answer isolation, feedback safety, same-target, or any
  other publication check."""

VERIFIER_SOURCE_GROUNDING_CONTRACT = """For source_grounded, use this exact contract:
- The learning target and knowledge mechanism must be directly supported by the evidence content.
- A self-consistent parameterized variant may pass only when the evidence content explicitly
  authorizes migration, parameter changes, scenario changes, or question-form changes. Its
  synthetic exact parameter values do not need to appear verbatim in the evidence, but they must
  obey every evidence-stated order of magnitude, unit, boundary, and mechanism. The public stem
  must state the constructed parameters, be self-contained, determine one unique best answer,
  and not present synthetic values as facts from the original or source question.
- Set source_grounded=false when a derived variant lacks that explicit authorization, the
  evidence lacks enough scale/unit/boundary constraints, or the draft departs from any supported
  scale, unit, boundary, mechanism, or target."""


class PreparationError(RuntimeError):
    pass


class ModelInvocationError(PreparationError):
    def __init__(
        self,
        *,
        error_code: str,
        error_type: str,
        returncode: int | None,
        retryable: bool,
        safe_summary: str,
    ) -> None:
        super().__init__(safe_summary)
        self.error_code = error_code
        self.error_type = error_type
        self.returncode = returncode
        self.retryable = retryable
        self.safe_summary = safe_summary


class MCPInvocationError(PreparationError):
    def __init__(self, code: str, *, retryable: bool) -> None:
        super().__init__(code)
        self.code = code
        self.retryable = retryable


def _chunk_plan(item_order: list[str]) -> list[dict[str, Any]]:
    chunks: list[dict[str, Any]] = []
    for offset in range(0, len(item_order), CHUNK_SIZE):
        index = len(chunks) + 1
        chunks.append(
            {
                "chunk_id": f"C{index:02d}",
                "chunk_index": index,
                "item_ids": item_order[offset : offset + CHUNK_SIZE],
                "status": "pending_author",
                "mcp": None,
                "author": None,
                "verifier": None,
                "author_attempts": 0,
                "verifier_attempts": 0,
                "failure": None,
            }
        )
    return chunks


def _ensure_chunk_state(state: dict[str, Any]) -> dict[str, Any]:
    chunks = state.get("chunks")
    expected = _chunk_plan(list(state.get("item_order") or []))
    if not isinstance(chunks, list) or [row.get("item_ids") for row in chunks if isinstance(row, dict)] != [row["item_ids"] for row in expected]:
        return {
            **state,
            "execution_contract_version": "morning-preparation-chunked-v2",
            "chunk_size": CHUNK_SIZE,
            "chunks": expected,
            "mcp_tool_call_count": 0,
            "next_retry_at": None,
        }
    return state


def _canonical_bytes(value: Any) -> bytes:
    return json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")


def _digest(value: Any) -> str:
    return hashlib.sha256(_canonical_bytes(value)).hexdigest()


def _file_sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _optional_file_sha(path: Path) -> str | None:
    if not path.exists():
        return None
    if path.is_symlink() or not path.is_file():
        raise PreparationError(f"snapshot input is not a regular file: {path}")
    return _file_sha(path)


def _review_ledger_cutoff(repo: Path) -> dict[str, Any]:
    path = repo / "wiki/study_vaults/408-full/state/review-loop/events.jsonl"
    if not path.exists():
        return {
            "status": "absent",
            "sha256": None,
            "byte_size": 0,
            "event_line_count": 0,
        }
    if path.is_symlink() or not path.is_file():
        raise PreparationError("review ledger is not a safe regular file")
    raw = path.read_bytes()
    return {
        "status": "bound",
        "sha256": hashlib.sha256(raw).hexdigest(),
        "byte_size": len(raw),
        "event_line_count": len(raw.splitlines()),
    }


def _now() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat().replace("+00:00", "Z")


def _safe_id(value: object, label: str) -> str:
    text = str(value or "")
    if not SAFE_ID.fullmatch(text):
        raise PreparationError(f"{label} is invalid")
    return text


def _date(value: str) -> str:
    try:
        return dt.date.fromisoformat(value).isoformat()
    except ValueError as exc:
        raise PreparationError("date must be YYYY-MM-DD") from exc


def _job_identity(repo: Path, review_date: str, queue: Path) -> dict[str, Any]:
    queue_schema, queue_date, queue_items = prepared_pack._parse_queue(queue)
    generation_binding = prepared_pack._queue_generation_binding(queue)
    if queue_date != review_date:
        raise PreparationError("queue date does not match the preparation date")
    queue_sha256 = _file_sha(queue)
    review_ledger_cutoff = _review_ledger_cutoff(repo)
    current_question_sources = []
    for row in queue_items:
        source = {
            "item_id": row["item_id"],
            "source_id": row["source_id"],
            "item_kind": row["item_kind"],
        }
        if any(row.get(field) is not None for field in ("action_id", "stage", "trigger_event_id")):
            source.update(
                {
                    "action_id": row.get("action_id"),
                    "stage": row.get("stage"),
                    "trigger_event_id": row.get("trigger_event_id"),
                }
            )
        current_question_sources.append(source)
    current_question_sources_sha256 = _digest(current_question_sources)
    preparation_worker_sha256 = _file_sha(Path(__file__).resolve())
    builder_sha256 = _file_sha(Path(prepared_pack.__file__).resolve())
    schema_path = repo / "schema/morning-review-preparation-model-output-v1.json"
    if schema_path.is_symlink() or not schema_path.is_file():
        raise PreparationError("prepared-pack schema is missing or unsafe")
    schema_sha256 = _file_sha(schema_path)
    source_versions = {
        "queue_sha256": queue_sha256,
        "review_ledger_cutoff": review_ledger_cutoff,
        "current_question_sources_sha256": current_question_sources_sha256,
        "preparation_worker_sha256": preparation_worker_sha256,
        "prepared_pack_builder_sha256": builder_sha256,
        "prepared_pack_schema_sha256": schema_sha256,
    }
    identity = {
        "schema": JOB_SCHEMA,
        "policy_version": POLICY_VERSION,
        "review_date": review_date,
        "queue_ref": _relative_to_repo(repo, queue),
        "queue_sha256": queue_sha256,
        "queue_schema": queue_schema,
        "item_order": [row["item_id"] for row in queue_items],
        "current_question_sources": current_question_sources,
        "current_question_sources_sha256": current_question_sources_sha256,
        "review_ledger_cutoff": review_ledger_cutoff,
        "prepared_pack_builder_sha256": builder_sha256,
        "prepared_pack_schema_sha256": schema_sha256,
        "preparation_worker_sha256": preparation_worker_sha256,
        "canonical_input_snapshot_sha256": (
            _digest(
                {
                    "source_versions": source_versions,
                    "queue_generation_binding": generation_binding,
                }
            )
            if generation_binding.get("generation_id")
            else _digest(source_versions)
        ),
        "source_versions": source_versions,
    }
    if generation_binding.get("generation_id"):
        identity["queue_generation_binding"] = generation_binding
    return identity


def _resolved_repo(repo: Path) -> Path:
    try:
        resolved = repo.expanduser().resolve(strict=True)
    except OSError as exc:
        raise PreparationError("repository root does not exist") from exc
    if not resolved.is_dir():
        raise PreparationError("repository root is not a directory")
    return resolved


def _repo_file(repo: Path, value: str | Path, label: str) -> Path:
    raw = Path(value)
    path = raw if raw.is_absolute() else repo / raw
    try:
        resolved = path.resolve(strict=True)
        resolved.relative_to(repo)
    except (OSError, ValueError) as exc:
        raise PreparationError(f"{label} must be an existing repository file") from exc
    if resolved.is_symlink() or not resolved.is_file():
        raise PreparationError(f"{label} is not a safe regular file")
    return resolved


def _read_json(path: Path, label: str) -> dict[str, Any]:
    if path.stat().st_size > MAX_JSON_BYTES:
        raise PreparationError(f"{label} exceeds the JSON size limit")
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise PreparationError(f"{label} is not valid UTF-8 JSON") from exc
    if not isinstance(value, dict):
        raise PreparationError(f"{label} JSON root must be an object")
    return value


def _model_payload(value: dict[str, Any], label: str) -> dict[str, Any]:
    payload = value.get("payload")
    if not isinstance(payload, dict):
        raise PreparationError(f"{label} model output lacks payload")
    if label == "author":
        if payload.get("status") == "needs_source":
            if (
                set(payload) != {"status", "missing_item_ids"}
                or not isinstance(payload.get("missing_item_ids"), list)
                or not payload["missing_item_ids"]
                or any(not isinstance(item, str) for item in payload["missing_item_ids"])
            ):
                raise PreparationError("author model schema rejected")
            return payload
        if set(payload) != {
            "schema",
            "author_actor_id",
            "author_execution_id",
            "queue_sha256",
            "items",
        } or not isinstance(payload.get("items"), list):
            raise PreparationError("author model schema rejected")
        return payload
    if label == "verifier":
        if set(payload) != {
            "verifier_actor_id",
            "verifier_execution_id",
            "items",
        } or not isinstance(payload.get("items"), list):
            raise PreparationError("verifier model schema rejected")
        expected_checks = set(prepared_pack.CHECKS)
        for row in payload["items"]:
            if (
                not isinstance(row, dict)
                or set(row) != {"item_id", "checks"}
                or not isinstance(row.get("item_id"), str)
                or not isinstance(row.get("checks"), dict)
                or set(row["checks"]) != expected_checks
                or any(not isinstance(value, bool) for value in row["checks"].values())
            ):
                raise PreparationError("verifier model schema rejected")
        return payload
    raise PreparationError("model stage is invalid")


def _atomic_json(path: Path, value: dict[str, Any], mode: int = 0o600) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    os.chmod(path.parent, 0o700)
    payload = json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    fd, temp_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    temp = Path(temp_name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(temp, mode)
        os.replace(temp, path)
        directory_fd = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
    finally:
        with contextlib.suppress(FileNotFoundError):
            temp.unlink()


@contextlib.contextmanager
def _lock(root: Path):
    root.mkdir(parents=True, exist_ok=True, mode=0o700)
    os.chmod(root, 0o700)
    path = root / ".preparation.lock"
    with path.open("a+", encoding="utf-8") as handle:
        os.chmod(path, 0o600)
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def _root(repo: Path) -> Path:
    return repo / JOB_ROOT_REL


def _relative_to_repo(repo: Path, path: Path) -> str:
    return path.relative_to(repo).as_posix()


def _write_object(repo: Path, kind: str, value: dict[str, Any]) -> tuple[str, str]:
    root = _root(repo)
    sha = _digest(value)
    path = root / "objects" / kind / f"{sha}.json"
    if path.exists():
        if _read_json(path, f"existing {kind} object") != value:
            raise PreparationError(f"existing {kind} object hash collision")
    else:
        _atomic_json(path, value)
    return _relative_to_repo(repo, path), sha


def _pointer_path(repo: Path, job_id: str) -> Path:
    return _root(repo) / "pointers" / f"{job_id}.json"


def _current_pointer_path(repo: Path, review_date: str) -> Path:
    return _root(repo) / "current" / f"{_date(review_date)}.json"


def _load_current_pointer(repo: Path, review_date: str) -> dict[str, Any] | None:
    path = _current_pointer_path(repo, review_date)
    if not path.is_file():
        return None
    value = _read_json(path, "current preparation pointer")
    if (
        value.get("schema") != CURRENT_POINTER_SCHEMA
        or value.get("review_date") != _date(review_date)
        or not isinstance(value.get("job_id"), str)
    ):
        raise PreparationError("current preparation pointer is invalid")
    return value


def _publish_current_pointer(repo: Path, state: dict[str, Any]) -> None:
    _atomic_json(
        _current_pointer_path(repo, str(state["review_date"])),
        {
            "schema": CURRENT_POINTER_SCHEMA,
            "review_date": state["review_date"],
            "job_id": state["job_id"],
            "queue_sha256": state["queue_sha256"],
            "generation_id": (
                state.get("queue_generation_binding") or {}
            ).get("generation_id"),
            "canonical_input_snapshot_sha256": state.get(
                "canonical_input_snapshot_sha256"
            ),
        },
    )


def _load_state(repo: Path, job_id: str) -> tuple[dict[str, Any], dict[str, Any]]:
    pointer_path = _pointer_path(repo, _safe_id(job_id, "job_id"))
    if not pointer_path.is_file():
        raise PreparationError("unknown preparation job")
    pointer = _read_json(pointer_path, "job pointer")
    if (
        pointer.get("schema") != POINTER_SCHEMA
        or pointer.get("job_id") != job_id
        or not isinstance(pointer.get("state_ref"), str)
        or not HEX64.fullmatch(str(pointer.get("state_sha256") or ""))
    ):
        raise PreparationError("job pointer is invalid")
    state_path = _repo_file(repo, str(pointer["state_ref"]), "job state")
    if _digest(_read_json(state_path, "job state")) != pointer["state_sha256"]:
        raise PreparationError("job state pointer hash drifted")
    state = _read_json(state_path, "job state")
    if state.get("schema") != STATE_SCHEMA or state.get("job_id") != job_id:
        raise PreparationError("job state is invalid")
    return pointer, state


def _publish_state(repo: Path, state: dict[str, Any]) -> dict[str, Any]:
    state_ref, state_sha = _write_object(repo, "states", state)
    pointer = {
        "schema": POINTER_SCHEMA,
        "job_id": state["job_id"],
        "state_ref": state_ref,
        "state_sha256": state_sha,
    }
    _atomic_json(_pointer_path(repo, str(state["job_id"])), pointer)
    return pointer


def _ready_generation_allows_ledger_suffix(
    repo: Path, state: dict[str, Any]
) -> bool:
    """A ready pack survives unrelated append-only ledger suffixes."""

    if state.get("status") != "ready" or not state.get("pack"):
        return False
    schema_path = repo / "schema/morning-review-preparation-model-output-v1.json"
    if (
        state.get("policy_version") != POLICY_VERSION
        or state.get("preparation_worker_sha256")
        != _file_sha(Path(__file__).resolve())
        or state.get("prepared_pack_builder_sha256")
        != _file_sha(Path(prepared_pack.__file__).resolve())
        or not schema_path.is_file()
        or state.get("prepared_pack_schema_sha256") != _file_sha(schema_path)
    ):
        return False
    binding = state.get("queue_generation_binding") or {}
    if not binding.get("generation_id"):
        return False
    queue = _repo_file(repo, state["queue_ref"], "ready generation queue")
    freshness = queue_generation.validate_generation_freshness(repo, queue)
    if not freshness.get("fresh"):
        return False
    cutoff = state.get("review_ledger_cutoff") or {}
    path = repo / "wiki/study_vaults/408-full/state/review-loop/events.jsonl"
    if cutoff.get("status") == "absent":
        # The empty ledger is an append-only prefix; queue freshness separately
        # proves that the suffix did not alter the executable action set.
        return True
    try:
        size = int(cutoff.get("byte_size"))
    except (TypeError, ValueError):
        return False
    expected = str(cutoff.get("sha256") or "")
    if size < 0 or not HEX64.fullmatch(expected) or not path.is_file() or path.is_symlink():
        return False
    with path.open("rb") as handle:
        prefix = handle.read(size)
    return len(prefix) == size and hashlib.sha256(prefix).hexdigest() == expected


def adopt_current_job(
    repo: Path,
    job_id: str,
    *,
    supersede_duplicates: bool = False,
) -> dict[str, Any]:
    repo = _resolved_repo(repo)
    root = _root(repo)
    with _lock(root):
        _, state = _load_state(repo, job_id)
        queue = _repo_file(repo, state["queue_ref"], "job queue")
        current_identity = _job_identity(repo, str(state["review_date"]), queue)
        identity_matches = _job_identity_matches(repo, state)
        ready_suffix_compatible = (
            not identity_matches
            and _ready_generation_allows_ledger_suffix(repo, state)
        )
        if not identity_matches and not ready_suffix_compatible:
            if state.get("author") or state.get("verifier") or state.get("pack"):
                raise PreparationError(
                    "a preparation job with model output cannot be rebound"
                )
            receipt = {
                "schema": RECEIPT_SCHEMA,
                "kind": "pre_model_contract_rebound",
                "job_id": job_id,
                "old_canonical_input_snapshot_sha256": state.get(
                    "canonical_input_snapshot_sha256"
                ),
                "new_canonical_input_snapshot_sha256": current_identity[
                    "canonical_input_snapshot_sha256"
                ],
                "old_status": state.get("status"),
                "new_status": "pending_author",
                "learner_evidence_write_count": 0,
                "formal_write_count": 0,
            }
            receipt_ref, receipt_sha = _write_object(repo, "receipts", receipt)
            history = list(state.get("contract_rebindings") or [])
            history.append(
                {
                    "receipt_ref": receipt_ref,
                    "receipt_sha256": receipt_sha,
                }
            )
            state = {
                **state,
                **current_identity,
                "schema": STATE_SCHEMA,
                "job_id": job_id,
                "status": "pending_author",
                "failure": None,
                "contract_generation": int(state.get("contract_generation") or 1)
                + 1,
                "contract_rebindings": history,
                "execution_contract_version": "morning-preparation-chunked-v2",
                "chunk_size": CHUNK_SIZE,
                "chunks": _chunk_plan(list(current_identity["item_order"])),
                "mcp_tool_call_count": 0,
                "next_retry_at": None,
                "updated_at": _now(),
            }
            _publish_state(repo, state)
        elif (
            state.get("status") in {"failed_author", "failed_verifier"}
            and not state.get("pack")
            and (
                (state.get("failure") or {}).get("error_type")
                == "model_process_error"
                or (
                    (state.get("failure") or {}).get("code")
                    == "deterministic_author_rejected"
                    and int(state.get("deterministic_requeue_count") or 0) < 1
                )
                or (
                    (state.get("failure") or {}).get("code")
                    == "verifier_semantic_rejected"
                    and int(state.get("semantic_requeue_count") or 0) < 1
                )
            )
        ):
            # Explicit operator adoption is the recovery boundary after a
            # model-process launch defect has been remediated. One bounded
            # author re-attempt is also allowed after deterministic rejection;
            # the rejected candidate stays private and the full deterministic
            # gate still runs again. One bounded re-author is likewise allowed
            # after an independent verifier rejection. Source gaps and identity
            # drift do not enter this branch.
            old_failure = dict(state.get("failure") or {})
            deterministic_requeue = (
                old_failure.get("code") == "deterministic_author_rejected"
            )
            semantic_requeue = (
                old_failure.get("code") == "verifier_semantic_rejected"
            )
            old_failure_receipt = _read_json(
                _repo_file(
                    repo,
                    str(old_failure.get("receipt_ref") or ""),
                    "old failure receipt",
                ),
                "old failure receipt",
            )
            receipt = {
                "schema": RECEIPT_SCHEMA,
                "kind": (
                    "verifier_semantic_failure_requeued"
                    if semantic_requeue
                    else (
                        "deterministic_author_failure_requeued"
                        if deterministic_requeue
                        else "pre_model_process_failure_requeued"
                    )
                ),
                "job_id": job_id,
                "old_status": state.get("status"),
                "old_failure_code": old_failure.get("code"),
                "old_failure_receipt_sha256": old_failure.get("receipt_sha256"),
                "failed_items": old_failure_receipt.get("failed_items") or [],
                "new_status": "pending_author",
                "learner_evidence_write_count": 0,
                "formal_write_count": 0,
            }
            receipt_ref, receipt_sha = _write_object(repo, "receipts", receipt)
            recovery_history = list(state.get("recovery_history") or [])
            recovery_history.append(
                {"receipt_ref": receipt_ref, "receipt_sha256": receipt_sha}
            )
            chunks = [dict(row) for row in state["chunks"]]
            for row in chunks:
                failure = row.get("failure") or {}
                if failure.get("stage") in {"author", "verifier"}:
                    if semantic_requeue:
                        row["revision_guidance"] = {
                            "source": "independent_verifier",
                            "failed_items": old_failure_receipt.get("failed_items")
                            or [],
                        }
                        row["author"] = None
                        row["verifier"] = None
                        row["status"] = "pending_author"
                    else:
                        row["status"] = (
                            "pending_verifier"
                            if row.get("author")
                            else "pending_author"
                        )
                    row["failure"] = None
            aggregate = (
                "pending_verifier"
                if all(row.get("author") is not None for row in chunks)
                else "pending_author"
            )
            state = {
                **state,
                "status": aggregate,
                "chunks": chunks,
                "author": None if semantic_requeue else state.get("author"),
                "verifier": None if semantic_requeue else state.get("verifier"),
                "failure": None,
                "next_retry_at": None,
                "recovery_history": recovery_history,
                "deterministic_requeue_count": int(
                    state.get("deterministic_requeue_count") or 0
                )
                + (1 if deterministic_requeue else 0),
                "semantic_requeue_count": int(
                    state.get("semantic_requeue_count") or 0
                )
                + (1 if semantic_requeue else 0),
                "updated_at": _now(),
            }
            _publish_state(repo, state)
        if supersede_duplicates:
            for pointer_path in sorted((root / "pointers").glob("MRJ-*.json")):
                pointer = _read_json(pointer_path, "preparation pointer")
                other_id = str(pointer.get("job_id") or "")
                if not other_id or other_id == job_id:
                    continue
                _, other = _load_state(repo, other_id)
                if (
                    other.get("review_date") != state.get("review_date")
                    or other.get("queue_sha256") != state.get("queue_sha256")
                    or other.get("pack")
                    or other.get("author")
                    or other.get("verifier")
                ):
                    continue
                other = {
                    **other,
                    "status": "superseded",
                    "failure": {
                        "code": "superseded_by_current_job",
                        "current_job_id": job_id,
                    },
                    "updated_at": _now(),
                }
                _publish_state(repo, other)
        _publish_current_pointer(repo, state)
    return _public_state(state, created=False)


def _queue_for_date(repo: Path, review_date: str) -> Path:
    current = queue_generation.load_current_generation_queue(repo, review_date)
    if current is not None:
        return current
    return repo / QUEUE_ROOT_REL / f"{review_date}-408晨间行动队列.md"


def _runtime(repo: Path) -> dict[str, Any]:
    path = repo / "wiki/study_vaults/408-full/state/review-loop/runtime.json"
    if not path.is_file():
        return {}
    value = _read_json(path, "review runtime")
    return value


def _active_session_gate(repo: Path, target_date: str) -> dict[str, Any] | None:
    runtime = _runtime(repo)
    active = runtime.get("active_session_id")
    if not isinstance(active, str) or not active:
        return None
    current_date = str(runtime.get("review_date") or "")
    if current_date != target_date:
        return {
            "status": "blocked_active_session",
            "active_session_id": active,
            "active_review_date": current_date or None,
            "target_date": target_date,
            "learner_evidence_write_count": 0,
            "formal_write_count": 0,
        }
    deferred = record_late_actions_for_active_session(
        repo, active, review_date=target_date
    )
    try:
        _, manifest = prepared_pack._session_pack_binding(repo, active)
    except (OSError, prepared_pack.PreparedPackError, ValueError) as exc:
        return {
            "status": "active_session_pack_invalid",
            "active_session_id": active,
            "target_date": target_date,
            "reason_code": type(exc).__name__,
            "learner_evidence_write_count": 0,
            "formal_write_count": 0,
        }
    return {
        "status": "ready_existing_session",
        "active_session_id": active,
        "target_date": target_date,
        "pack_id": manifest.get("pack_id"),
        "manifest_sha256": _digest(manifest),
        "late_action_deferred_receipts": deferred["receipts"],
        "late_action_deferred_count": deferred["deferred_count"],
        "learner_evidence_write_count": 0,
        "formal_write_count": 0,
    }


def _closeout_binding(repo: Path, batch_id: str | None) -> dict[str, Any] | None:
    if not batch_id:
        return None
    import intake_fact_capture_408 as intake

    batch_value = intake.status(None, repo_root=repo).get("batches", {}).get(batch_id)
    if not isinstance(batch_value, dict):
        raise PreparationError("nightly closeout batch is unknown")
    if (
        batch_value.get("status") != "closed"
        or batch_value.get("global_closeout") != "pass"
        or batch_value.get("failed_capture_ids")
        or batch_value.get("needs_user_capture_ids")
        or not HEX64.fullmatch(str(batch_value.get("global_audit_receipt_sha256") or ""))
    ):
        raise PreparationError("nightly closeout is not a complete PASS batch")
    return {
        "batch_id": batch_id,
        "global_audit_receipt_sha256": batch_value["global_audit_receipt_sha256"],
    }


def _has_model_artifact(state: dict[str, Any]) -> bool:
    if state.get("author") or state.get("verifier"):
        return True
    for chunk in state.get("chunks") or []:
        if not isinstance(chunk, dict):
            continue
        if chunk.get("author") or chunk.get("verifier"):
            return True
        diagnostic = (chunk.get("failure") or {}).get("diagnostic")
        if isinstance(diagnostic, dict) and diagnostic.get("candidate_ref"):
            return True
    return False


def _allows_contract_successor(state: dict[str, Any]) -> bool:
    return (
        state.get("status") in {"failed_author", "failed_verifier"}
        and not state.get("pack")
        and (state.get("failure") or {}).get("retryable") is not True
        and _has_model_artifact(state)
    )


def enqueue_job(
    repo: Path,
    *,
    review_date: str,
    queue: Path,
    trigger: str,
    batch_id: str | None = None,
) -> dict[str, Any]:
    repo = _resolved_repo(repo)
    review_date = _date(review_date)
    if trigger not in {"nightly_closeout", "guardian_precheck", "manual"}:
        raise PreparationError("trigger is invalid")
    if trigger == "nightly_closeout" and not batch_id:
        raise PreparationError("nightly_closeout requires batch_id")
    closeout = _closeout_binding(repo, batch_id) if trigger == "nightly_closeout" else None
    queue = _repo_file(repo, queue, "morning queue")
    job_identity = _job_identity(repo, review_date, queue)
    job_id = "MRJ-" + _digest(job_identity)[:24].upper()
    root = _root(repo)
    predecessor_job_id: str | None = None
    with _lock(root):
        current_pointer = _load_current_pointer(repo, review_date)
        if current_pointer is not None:
            _, current_state = _load_state(repo, str(current_pointer["job_id"]))
            if current_state.get("queue_sha256") == job_identity["queue_sha256"]:
                if not _job_identity_matches(repo, current_state):
                    if not _allows_contract_successor(current_state):
                        raise PreparationError(
                            "current preparation contract requires explicit refresh"
                        )
                    predecessor_job_id = str(current_state["job_id"])
                else:
                    return _public_state(current_state, created=False)
        pointer_path = _pointer_path(repo, job_id)
        if pointer_path.exists():
            _, state = _load_state(repo, job_id)
            _publish_current_pointer(repo, state)
            return _public_state(state, created=False)
        state = {
            **job_identity,
            "schema": STATE_SCHEMA,
            "job_id": job_id,
            "status": "pending_author",
            "trigger": trigger,
            "closeout_binding": closeout,
            "created_at": _now(),
            "updated_at": _now(),
            "author": None,
            "verifier": None,
            "pack": None,
            "failure": None,
            "contract_generation": 1,
            "contract_rebindings": [],
            "execution_contract_version": "morning-preparation-chunked-v2",
            "chunk_size": CHUNK_SIZE,
            "chunks": _chunk_plan(list(job_identity["item_order"])),
            "mcp_tool_call_count": 0,
            "next_retry_at": None,
            "learner_evidence_write_count": 0,
            "formal_write_count": 0,
        }
        if predecessor_job_id is not None:
            state["predecessor_job_id"] = predecessor_job_id
            state["successor_reason"] = (
                "execution_contract_changed_after_terminal_failure"
            )
        _publish_state(repo, state)
        _publish_current_pointer(repo, state)
    return _public_state(state, created=True)


def _public_state(state: dict[str, Any], *, created: bool | None = None) -> dict[str, Any]:
    state = _ensure_chunk_state(state)
    chunks = [row for row in state.get("chunks") or [] if isinstance(row, dict)]
    author_completed = sum(row.get("author") is not None for row in chunks)
    verifier_completed = sum(row.get("verifier") is not None for row in chunks)
    if state.get("status") in {"pending_author", "needs_source", "retry_wait"}:
        current_chunk = next(
            (row.get("chunk_id") for row in chunks if row.get("author") is None),
            None,
        )
    elif state.get("status") in {"pending_verifier", "failed_verifier"}:
        current_chunk = next(
            (row.get("chunk_id") for row in chunks if row.get("verifier") is None),
            None,
        )
    elif state.get("status") == "failed_author":
        current_chunk = next(
            (
                row.get("chunk_id")
                for row in chunks
                if (row.get("failure") or {}).get("stage")
                in {"mcp", "author", "verifier"}
            ),
            None,
        )
    else:
        current_chunk = None
    value = {
        "schema": STATE_SCHEMA,
        "job_id": state["job_id"],
        "status": state["status"],
        "review_date": state["review_date"],
        "queue_ref": state["queue_ref"],
        "queue_sha256": state["queue_sha256"],
        "generation_id": (state.get("queue_generation_binding") or {}).get(
            "generation_id"
        ),
        "generation_input_sha256": (
            state.get("queue_generation_binding") or {}
        ).get("generation_input_sha256"),
        "eligible_due_action_set_sha256": (
            state.get("queue_generation_binding") or {}
        ).get("eligible_due_action_set_sha256"),
        "formal_due_source_sha256": (
            state.get("queue_generation_binding") or {}
        ).get("formal_due_source_sha256"),
        "canonical_input_snapshot_sha256": state.get(
            "canonical_input_snapshot_sha256"
        ),
        "current_question_sources_sha256": state.get(
            "current_question_sources_sha256"
        ),
        "review_ledger_cutoff": state.get("review_ledger_cutoff"),
        "prepared_pack_builder_sha256": state.get(
            "prepared_pack_builder_sha256"
        ),
        "prepared_pack_schema_sha256": state.get(
            "prepared_pack_schema_sha256"
        ),
        "preparation_worker_sha256": state.get(
            "preparation_worker_sha256"
        ),
        "predecessor_job_id": state.get("predecessor_job_id"),
        "successor_reason": state.get("successor_reason"),
        "item_count": len(state.get("item_order") or []),
        "execution_contract_version": state.get("execution_contract_version"),
        "chunk_size": state.get("chunk_size", CHUNK_SIZE),
        "chunk_count": len(chunks),
        "author_chunks_completed": author_completed,
        "verifier_chunks_completed": verifier_completed,
        "current_chunk_id": current_chunk,
        "next_retry_at": state.get("next_retry_at"),
        "mcp_tool_call_count": int(state.get("mcp_tool_call_count") or 0),
        "author_receipt_sha256": (state.get("author") or {}).get("receipt_sha256"),
        "verifier_receipt_sha256": (state.get("verifier") or {}).get("receipt_sha256"),
        "pack_id": (state.get("pack") or {}).get("pack_id"),
        "manifest_ref": (state.get("pack") or {}).get("manifest_ref"),
        "manifest_sha256": (state.get("pack") or {}).get("manifest_sha256"),
        "pack_commitment_sha256": (state.get("pack") or {}).get(
            "pack_commitment_sha256"
        ),
        "failure_code": (state.get("failure") or {}).get("code"),
        "failure_stage": (state.get("failure") or {}).get("stage"),
        "failure_retryable": (state.get("failure") or {}).get("retryable"),
        "failure_receipt_sha256": (state.get("failure") or {}).get(
            "receipt_sha256"
        ),
        "learner_evidence_write_count": 0,
        "formal_write_count": 0,
    }
    if created is not None:
        value["created"] = created
    return value


def _job_identity_matches(repo: Path, state: dict[str, Any]) -> bool:
    queue = _repo_file(repo, state["queue_ref"], "job queue")
    current = _job_identity(repo, str(state["review_date"]), queue)
    fields = (
        "policy_version",
        "queue_ref",
        "queue_sha256",
        "queue_schema",
        "item_order",
        "current_question_sources",
        "current_question_sources_sha256",
        "review_ledger_cutoff",
        "preparation_worker_sha256",
        "prepared_pack_builder_sha256",
        "prepared_pack_schema_sha256",
        "canonical_input_snapshot_sha256",
        "source_versions",
        "queue_generation_binding",
    )
    return all(current.get(key) == state.get(key) for key in fields)


def _update_state(
    repo: Path, job_id: str, expected: set[str], changes: dict[str, Any]
) -> dict[str, Any]:
    root = _root(repo)
    with _lock(root):
        _, state = _load_state(repo, job_id)
        if state.get("status") not in expected:
            raise PreparationError(
                f"job status {state.get('status')} is not valid for this operation"
            )
        next_state = {**state, **changes, "updated_at": _now()}
        _publish_state(repo, next_state)
    return next_state


def _mark_stale(
    repo: Path, job_id: str, *, expected: set[str]
) -> dict[str, Any]:
    return _update_state(
        repo,
        job_id,
        expected,
        {"status": "stale", "failure": {"code": "input_snapshot_drift"}},
    )


def _mark_current_job_stale_pre_session(
    repo: Path,
    review_date: str,
    queue: Path,
    freshness: dict[str, Any],
) -> dict[str, Any] | None:
    current = _load_current_pointer(repo, review_date)
    if current is None:
        return None
    job_id = str(current.get("job_id") or "")
    if not job_id:
        return None
    _, state = _load_state(repo, job_id)
    if state.get("queue_sha256") != _file_sha(queue):
        return None
    if state.get("status") == "stale_pre_session":
        return _public_state(state, created=False)
    receipt = {
        "schema": RECEIPT_SCHEMA,
        "kind": "generation_stale_pre_session",
        "job_id": job_id,
        "review_date": review_date,
        "generation_id": (state.get("queue_generation_binding") or {}).get(
            "generation_id"
        ),
        "queue_ref": state.get("queue_ref"),
        "queue_sha256": state.get("queue_sha256"),
        "pack_id": (state.get("pack") or {}).get("pack_id"),
        "manifest_ref": (state.get("pack") or {}).get("manifest_ref"),
        "manifest_sha256": (state.get("pack") or {}).get("manifest_sha256"),
        "pack_commitment_sha256": (state.get("pack") or {}).get(
            "pack_commitment_sha256"
        ),
        "reason_codes": list(freshness.get("reason_codes") or []),
        "replacement_generation_id": freshness.get("current_generation_id"),
        "learner_evidence_write_count": 0,
        "formal_write_count": 0,
    }
    receipt_ref, receipt_sha = _write_object(repo, "receipts", receipt)
    next_state = _update_state(
        repo,
        job_id,
        {str(state.get("status"))},
        {
            "status": "stale_pre_session",
            "failure": {
                "code": "eligible_generation_changed_before_session_start",
                "reason_codes": receipt["reason_codes"],
                "receipt_ref": receipt_ref,
                "receipt_sha256": receipt_sha,
            },
        },
    )
    return _public_state(next_state, created=False)


def record_late_actions_for_active_session(
    repo: Path,
    session_id: str,
    *,
    review_date: str,
) -> dict[str, Any]:
    with review_loop.runtime_lock(repo):
        return _record_late_actions_for_active_session_locked(
            repo, session_id, review_date=review_date
        )


def _frozen_generation_action_set(
    repo: Path,
    state: dict[str, Any],
    *,
    review_date: str,
) -> dict[str, Any] | None:
    generation_id = str(state.get("generation_id") or "")
    if not generation_id:
        return None
    if not SAFE_ID.fullmatch(generation_id):
        raise PreparationError("active session generation_id is invalid")
    manifest_path = (
        repo
        / queue_generation.QUEUE_GENERATION_ROOT_REL
        / "generations"
        / review_date
        / generation_id
        / "manifest.json"
    )
    manifest = _read_json(manifest_path, "active session queue-generation manifest")
    if (
        manifest.get("schema") != queue_generation.QUEUE_GENERATION_SCHEMA
        or manifest.get("status") != "queue_bound"
        or manifest.get("review_date") != review_date
        or manifest.get("generation_id") != generation_id
        or manifest.get("queue_sha256") != state.get("queue_sha256")
    ):
        raise PreparationError("active session queue-generation binding drifted")
    rows = manifest.get("eligible_due_actions")
    expected_sha = str(manifest.get("eligible_due_action_set_sha256") or "")
    if not isinstance(rows, list) or _digest(rows) != expected_sha:
        raise PreparationError("frozen eligible due-action set is invalid")
    action_ids: set[str] = set()
    for row in rows:
        action_id = str(row.get("action_id") or "") if isinstance(row, dict) else ""
        if not action_id or not SAFE_ID.fullmatch(action_id) or action_id in action_ids:
            raise PreparationError("frozen eligible due-action identity is invalid")
        action_ids.add(action_id)
    return {
        "generation_id": generation_id,
        "eligible_due_action_set_sha256": expected_sha,
        "action_ids": action_ids,
    }


def _record_late_actions_for_active_session_locked(
    repo: Path,
    session_id: str,
    *,
    review_date: str,
) -> dict[str, Any]:
    """Append idempotent receipts for due actions arriving after session freeze."""

    session_state_path = (
        repo
        / "wiki/study_vaults/408-full/state/morning-review"
        / session_id
        / "state.json"
    )
    if not session_state_path.is_file():
        raise PreparationError("active morning session state is missing")
    state = _read_json(session_state_path, "active morning session state")
    if state.get("status") not in {"in_progress", "paused"}:
        return {"status": "no_active_session", "deferred_count": 0, "receipts": []}
    day = dt.date.fromisoformat(_date(review_date))
    if state.get("review_date") != day.isoformat():
        raise PreparationError("active session review_date drifted")
    frozen_generation = _frozen_generation_action_set(
        repo, state, review_date=day.isoformat()
    )
    if frozen_generation is None:
        return {
            "status": "legacy_generation_late_action_unverifiable",
            "deferred_count": 0,
            "receipts": [],
            "formal_write_count": 0,
            "learner_evidence_write_count": 0,
        }
    frozen_action_ids = set(frozen_generation["action_ids"])
    current_actions = [
        {
            "action_id": item.action_id,
            "trigger_event_id": item.trigger_event_id,
            "stage": item.stage,
            "due_date": item.due_date,
        }
        for item in queue_generation.load_due_actions(repo, day)
    ]
    current_action_set_sha = _digest(
        sorted(current_actions, key=lambda row: str(row["action_id"]))
    )
    receipts: list[dict[str, str]] = []
    root = repo / queue_generation.QUEUE_GENERATION_ROOT_REL
    for action in current_actions:
        action_id = str(action.get("action_id") or "")
        if not action_id or action_id in frozen_action_ids:
            continue
        receipt = {
            "schema": "morning_late_action_deferred_v1",
            "status": "deferred_to_next_unfrozen_generation",
            "session_id": session_id,
            "review_date": review_date,
            "session_started_at": state.get("created_at"),
            "session_started_sequence": 1,
            "frozen_generation_id": state.get("generation_id"),
            "frozen_eligible_due_action_set_sha256": frozen_generation[
                "eligible_due_action_set_sha256"
            ],
            "queue_sha256": state.get("queue_sha256"),
            "detected_eligible_due_action_set_sha256": current_action_set_sha,
            "action_id": action_id,
            "trigger_event_id": action.get("trigger_event_id"),
            "stage": action.get("stage"),
            "original_due_date": action.get("due_date"),
            "due_date_preserved": True,
            "pending_state_preserved": True,
            "formal_write_count": 0,
            "learner_evidence_write_count": 0,
        }
        sha = _digest(receipt)
        path = root / "receipts" / "late-actions" / f"{sha}.json"
        if path.exists():
            if _read_json(path, "late action receipt") != receipt:
                raise PreparationError("late action receipt hash collision")
        else:
            _atomic_json(path, receipt)
        receipts.append(
            {"action_id": action_id, "receipt_ref": _relative_to_repo(repo, path), "receipt_sha256": sha}
        )
    return {
        "status": "late_actions_deferred" if receipts else "no_late_actions",
        "deferred_count": len(receipts),
        "receipts": receipts,
        "formal_write_count": 0,
        "learner_evidence_write_count": 0,
    }


def ensure_fresh_generation(
    repo: Path,
    review_date: str,
) -> dict[str, Any]:
    """Return the current fresh queue, replacing only pre-session generations."""

    repo = _resolved_repo(repo)
    review_date = _date(review_date)
    runtime = _runtime(repo)
    active = runtime.get("active_session_id")
    active_date = str(runtime.get("review_date") or "")
    if isinstance(active, str) and active and active_date == review_date:
        deferred = record_late_actions_for_active_session(
            repo, active, review_date=str(runtime.get("review_date") or review_date)
        )
        return {
            "status": "active_session_frozen",
            "queue": _queue_for_date(repo, review_date),
            "deferred": deferred,
        }
    current_generation_queue = queue_generation.load_current_generation_queue(
        repo, review_date
    )
    queue = current_generation_queue or _queue_for_date(repo, review_date)
    if queue.is_file():
        freshness = queue_generation.validate_generation_freshness(repo, queue)
        if (
            freshness.get("fresh")
            and freshness.get("generation_id")
            and current_generation_queue is not None
        ):
            return {"status": "fresh", "queue": queue, "freshness": freshness}
        if freshness.get("generation_id"):
            _mark_current_job_stale_pre_session(
                repo, review_date, queue, freshness
            )
    published = queue_generation.publish_generation_queue(
        repo,
        dt.date.fromisoformat(review_date),
        baseline_quota=0,
        activate_runtime=not (isinstance(active, str) and bool(active)),
    )
    replacement = Path(str(published["queue_path"]))
    return {
        "status": "replacement_generation_created",
        "queue": replacement,
        "generation_id": published["generation_id"],
        "queue_sha256": published["queue_sha256"],
    }


def _record_model_failure(
    repo: Path,
    state: dict[str, Any],
    *,
    stage: str,
    error: ModelInvocationError,
    model: str,
    reasoning: str,
) -> dict[str, Any]:
    if stage not in {"author", "verifier"}:
        raise PreparationError("model failure stage is invalid")
    receipt = {
        "schema": RECEIPT_SCHEMA,
        "kind": "model_invocation_failure",
        "job_id": state["job_id"],
        "stage": stage,
        "error_code": error.error_code,
        "error_type": error.error_type,
        "requested_model": model,
        "requested_reasoning_effort": reasoning,
        "runtime_identity_status": "requested_unverified",
        "confirmed_model": None,
        "confirmed_reasoning_effort": None,
        "returncode": error.returncode,
        "retryable": error.retryable,
        "safe_stderr_summary": error.safe_summary,
        "learner_evidence_write_count": 0,
        "formal_write_count": 0,
    }
    receipt_ref, receipt_sha = _write_object(repo, "receipts", receipt)
    expected = (
        {"pending_author", "needs_source", "failed_author"}
        if stage == "author"
        else {"pending_verifier", "failed_verifier"}
    )
    return _update_state(
        repo,
        str(state["job_id"]),
        expected,
        {
            "status": f"failed_{stage}",
            "failure": {
                "code": error.error_code,
                "stage": stage,
                "error_type": error.error_type,
                "retryable": error.retryable,
                "returncode": error.returncode,
                "safe_stderr_summary": error.safe_summary,
                "receipt_ref": receipt_ref,
                "receipt_sha256": receipt_sha,
            },
        },
    )


def _record_verifier_semantic_rejection(
    repo: Path,
    state: dict[str, Any],
    *,
    verification: dict[str, Any],
    invocation: dict[str, Any],
    model: str,
    reasoning: str,
) -> dict[str, Any]:
    failed_rows: list[dict[str, Any]] = []
    for row in verification.get("items") or []:
        if not isinstance(row, dict):
            continue
        checks = row.get("checks")
        failed_checks = sorted(
            key
            for key, value in (checks.items() if isinstance(checks, dict) else [])
            if value is not True
        )
        if failed_checks:
            failed_rows.append(
                {
                    "item_id": str(row.get("item_id") or "unknown")[:80],
                    "failed_checks": failed_checks,
                }
            )
    verification_ref, verification_sha = _write_object(
        repo, "verifications", verification
    )
    receipt = {
        "schema": RECEIPT_SCHEMA,
        "kind": "model_verifier_semantic_rejection",
        "job_id": state["job_id"],
        "stage": "verifier",
        "error_code": "verifier_semantic_rejected",
        "error_type": "semantic_gate_rejected",
        "requested_model": model,
        "requested_reasoning_effort": reasoning,
        "runtime_identity_status": invocation.get(
            "runtime_identity_status", "requested_unverified"
        ),
        "confirmed_model": invocation.get("confirmed_model"),
        "confirmed_reasoning_effort": invocation.get(
            "confirmed_reasoning_effort"
        ),
        "returncode": invocation.get("returncode"),
        "retryable": True,
        "failed_items": failed_rows,
        "verification_ref": verification_ref,
        "verification_sha256": verification_sha,
        "learner_evidence_write_count": 0,
        "formal_write_count": 0,
    }
    receipt_ref, receipt_sha = _write_object(repo, "receipts", receipt)
    return _update_state(
        repo,
        str(state["job_id"]),
        {"pending_verifier", "failed_verifier"},
        {
            # A semantic rejection invalidates the authored content, not the
            # verifier invocation.  The same immutable job therefore returns
            # to the author stage while preserving the rejected draft and
            # verification as content-addressed audit objects.
            "status": "failed_author",
            "failure": {
                "code": "verifier_semantic_rejected",
                "stage": "verifier",
                "error_type": "semantic_gate_rejected",
                "retryable": True,
                "failed_items": failed_rows,
                "receipt_ref": receipt_ref,
                "receipt_sha256": receipt_sha,
            },
        },
    )
def submit_author(repo: Path, job_id: str, draft_path: Path) -> dict[str, Any]:
    repo = _resolved_repo(repo)
    _, state = _load_state(repo, job_id)
    if state.get("status") in {"pending_verifier", "ready"}:
        return _public_state(state, created=False)
    if state.get("status") not in {"pending_author", "needs_source"}:
        raise PreparationError("job is not waiting for author output")
    if not _job_identity_matches(repo, state):
        stale = _mark_stale(
            repo, job_id, expected={"pending_author", "needs_source"}
        )
        return _public_state(stale, created=False)
    draft_path = _repo_file(repo, draft_path, "author draft")
    queue = _repo_file(repo, state["queue_ref"], "job queue")
    hashes = prepared_pack.draft_hashes(repo, queue, draft_path)
    draft = _read_json(draft_path, "author draft")
    draft_ref, draft_sha = _write_object(repo, "drafts", draft)
    receipt = {
        "schema": RECEIPT_SCHEMA,
        "kind": "author_submission",
        "job_id": job_id,
        "draft_ref": draft_ref,
        "draft_sha256": draft_sha,
        "normalized_draft_sha256": hashes["draft_sha256"],
        "author_actor_id": draft.get("author_actor_id"),
        "author_execution_id": draft.get("author_execution_id"),
        "item_count": len(hashes.get("item_hashes") or []),
        "learner_evidence_write_count": 0,
        "formal_write_count": 0,
    }
    receipt_ref, receipt_sha = _write_object(repo, "receipts", receipt)
    next_state = _update_state(
        repo,
        job_id,
        {"pending_author", "needs_source"},
        {
            "status": "pending_verifier",
            "author": {
                "draft_ref": draft_ref,
                "draft_sha256": draft_sha,
                "normalized_draft_sha256": hashes["draft_sha256"],
                "receipt_ref": receipt_ref,
                "receipt_sha256": receipt_sha,
            },
            "failure": None,
        },
    )
    return _public_state(next_state, created=True)


def submit_verifier(
    repo: Path, job_id: str, verification_path: Path
) -> dict[str, Any]:
    repo = _resolved_repo(repo)
    _, state = _load_state(repo, job_id)
    if state.get("status") == "ready":
        return _public_state(state, created=False)
    if state.get("status") != "pending_verifier":
        raise PreparationError("job is not waiting for verifier output")
    if not _job_identity_matches(repo, state):
        stale = _mark_stale(repo, job_id, expected={"pending_verifier"})
        return _public_state(stale, created=False)
    verification_path = _repo_file(repo, verification_path, "verifier submission")
    draft = _repo_file(repo, state["author"]["draft_ref"], "bound author draft")
    queue = _repo_file(repo, state["queue_ref"], "job queue")
    result = prepared_pack.publish_pack(repo, queue, draft, verification_path)
    if not _job_identity_matches(repo, state):
        stale = _mark_stale(repo, job_id, expected={"pending_verifier"})
        return _public_state(stale, created=False)
    verification = _read_json(verification_path, "verifier submission")
    verification_ref, verification_sha = _write_object(
        repo, "verifications", verification
    )
    receipt = {
        "schema": RECEIPT_SCHEMA,
        "kind": "verifier_publication",
        "job_id": job_id,
        "verification_ref": verification_ref,
        "verification_sha256": verification_sha,
        "verifier_actor_id": verification.get("verifier_actor_id"),
        "verifier_execution_id": verification.get("verifier_execution_id"),
        "pack_id": result["pack_id"],
        "manifest_ref": result["manifest_ref"],
        "manifest_sha256": result["manifest_sha256"],
        "pack_commitment_sha256": result.get("pack_commitment_sha256"),
        "learner_evidence_write_count": 0,
        "formal_write_count": 0,
    }
    receipt_ref, receipt_sha = _write_object(repo, "receipts", receipt)
    next_state = _update_state(
        repo,
        job_id,
        {"pending_verifier"},
        {
            "status": "ready",
            "verifier": {
                "verification_ref": verification_ref,
                "verification_sha256": verification_sha,
                "receipt_ref": receipt_ref,
                "receipt_sha256": receipt_sha,
            },
            "pack": {
                "pack_id": result["pack_id"],
                "manifest_ref": result["manifest_ref"],
                "manifest_sha256": result["manifest_sha256"],
                "pack_commitment_sha256": result.get(
                    "pack_commitment_sha256"
                ),
            },
            "failure": None,
        },
    )
    return _public_state(next_state, created=True)


def _model_schema_path(repo: Path) -> Path:
    path = _repo_file(
        repo,
        "schema/morning-review-preparation-model-output-v1.json",
        "model output schema",
    )
    schema = _read_json(path, "model output schema")
    _validate_strict_schema(schema)
    return path


def _validate_strict_schema(node: object, path: str = "$") -> None:
    if isinstance(node, dict):
        if node.get("type") == "object":
            properties = node.get("properties")
            required = node.get("required")
            if (
                node.get("additionalProperties") is not False
                or not isinstance(properties, dict)
                or not isinstance(required, list)
                or set(required) != set(properties)
            ):
                raise PreparationError(
                    f"model output schema object is not strict at {path}"
                )
        for key, value in node.items():
            _validate_strict_schema(value, f"{path}.{key}")
    elif isinstance(node, list):
        for index, value in enumerate(node):
            _validate_strict_schema(value, f"{path}[{index}]")


def _safe_model_error(stderr: str, *, returncode: int | None) -> ModelInvocationError:
    segment = (stderr or "").rsplit("ERROR:", 1)[-1]
    code_match = re.search(r'"code"\s*:\s*"([A-Za-z0-9_.-]{1,120})"', segment)
    type_match = re.search(r'"type"\s*:\s*"([A-Za-z0-9_.-]{1,120})"', segment)
    error_code = code_match.group(1) if code_match else "model_invocation_failed"
    error_type = type_match.group(1) if type_match else "model_process_error"
    retryable = error_code in {
        "rate_limit_exceeded",
        "server_error",
        "service_unavailable",
        "timeout",
    }
    return ModelInvocationError(
        error_code=error_code,
        error_type=error_type,
        returncode=returncode,
        retryable=retryable,
        safe_summary=f"{error_type}:{error_code}",
    )


def _model_command(
    *,
    codex: Path,
    repo: Path,
    model: str,
    reasoning: str,
    schema: Path,
    output: Path,
) -> list[str]:
    return [
        str(codex),
        "exec",
        "--ephemeral",
        "--ignore-user-config",
        "--ignore-rules",
        "--sandbox",
        "read-only",
        "--model",
        model,
        "-c",
        f'model_reasoning_effort="{reasoning}"',
        "-c",
        'approval_policy="never"',
        "-c",
        "project_doc_max_bytes=0",
        "-c",
        "project_doc_fallback_filenames=[]",
        "-c",
        "features.plugins=false",
        "-c",
        "agents.enabled=false",
        "-c",
        'web_search="disabled"',
        "--color",
        "never",
        "--json",
        "--skip-git-repo-check",
        "--cd",
        str(repo),
        "--output-schema",
        str(schema),
        "--output-last-message",
        str(output),
        "-",
    ]


def _invoke_model(
    *,
    codex: Path,
    repo: Path,
    model: str,
    reasoning: str,
    prompt: str,
    timeout_seconds: int,
) -> tuple[dict[str, Any], dict[str, Any]]:
    if codex.is_symlink() or not codex.is_file() or not os.access(codex, os.X_OK):
        raise PreparationError("configured Codex binary is not a safe executable")
    schema = _model_schema_path(repo)
    with tempfile.TemporaryDirectory(prefix="morning-pack-model-") as temp_name:
        workdir = Path(temp_name)
        output = workdir / "result.json"
        started = time.monotonic()
        try:
            result = subprocess.run(
                _model_command(
                    codex=codex,
                    repo=workdir,
                    model=model,
                    reasoning=reasoning,
                    schema=schema,
                    output=output,
                ),
                cwd=workdir,
                capture_output=True,
                text=True,
                input=prompt,
                timeout=timeout_seconds,
                check=False,
                env={
                    "PATH": "/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin",
                    "HOME": str(Path.home()),
                    "LANG": "zh_CN.UTF-8",
                    "PYTHONDONTWRITEBYTECODE": "1",
                    **{
                        name: os.environ[name]
                        for name in (
                            "HTTP_PROXY",
                            "HTTPS_PROXY",
                            "ALL_PROXY",
                            "NO_PROXY",
                            "http_proxy",
                            "https_proxy",
                            "all_proxy",
                            "no_proxy",
                        )
                        if os.environ.get(name)
                    },
                },
            )
        except subprocess.TimeoutExpired as exc:
            raise ModelInvocationError(
                error_code="model_timeout",
                error_type="timeout",
                returncode=None,
                retryable=True,
                safe_summary="timeout:model_timeout",
            ) from exc
        duration_ms = round((time.monotonic() - started) * 1000.0, 3)
        if result.returncode != 0 or not output.is_file():
            raise _safe_model_error(result.stderr, returncode=result.returncode)
        value = _read_json(output, "model output")
        confirmed_model = None
        confirmed_reasoning = None
        for line in result.stdout.splitlines():
            try:
                event = json.loads(line)
            except json.JSONDecodeError:
                continue
            if not isinstance(event, dict):
                continue
            confirmed_model = event.get("model") or confirmed_model
            confirmed_reasoning = (
                event.get("reasoning_effort")
                or event.get("model_reasoning_effort")
                or confirmed_reasoning
            )
        invocation = {
            "requested_model": model,
            "requested_reasoning_effort": reasoning,
            "runtime_identity_status": (
                "confirmed" if confirmed_model == model else "requested_unverified"
            ),
            "confirmed_model": confirmed_model,
            "confirmed_reasoning_effort": confirmed_reasoning,
            "sandbox": "read-only",
            "ephemeral": True,
            "plugins": False,
            "subagents": False,
            "web_search": "disabled",
            "returncode": result.returncode,
            "duration_ms": duration_ms,
            "output_sha256": _digest(value),
            "mcp_access": "host_supplied_frozen_bundle",
        }
        return value, invocation


def _load_mcp_release() -> dict[str, Any]:
    pointer_path = Path(os.environ.get("KAOYAN_408_MORNING_MCP_POINTER", str(MCP_POINTER)))
    if pointer_path.is_symlink() or not pointer_path.is_file():
        raise MCPInvocationError("mcp_release_pointer_missing", retryable=True)
    pointer = _read_json(pointer_path, "morning MCP release pointer")
    if (
        pointer.get("schema") != "study_read_mcp_morning_preparation_pointer_v1"
        or not isinstance(pointer.get("release_root"), str)
        or not HEX64.fullmatch(str(pointer.get("release_id") or ""))
        or not isinstance(pointer.get("release_manifest_sha256"), str)
        or not HEX64.fullmatch(pointer["release_manifest_sha256"])
        or not isinstance(pointer.get("server_release"), str)
    ):
        raise MCPInvocationError("mcp_release_pointer_invalid", retryable=False)
    root = Path(str(pointer["release_root"]))
    try:
        resolved = root.resolve(strict=True)
    except OSError as exc:
        raise MCPInvocationError("mcp_release_missing", retryable=True) from exc
    if root.is_symlink() or not resolved.is_dir() or resolved.name != pointer["release_id"]:
        raise MCPInvocationError("mcp_release_binding_invalid", retryable=False)
    manifest_path = resolved / "release.json"
    if manifest_path.is_symlink() or not manifest_path.is_file():
        raise MCPInvocationError("mcp_release_manifest_missing", retryable=False)
    try:
        manifest_raw = manifest_path.read_bytes()
    except OSError as exc:
        raise MCPInvocationError(
            "mcp_release_manifest_missing", retryable=False
        ) from exc
    if len(manifest_raw) > MAX_JSON_BYTES:
        raise PreparationError(
            "morning MCP release manifest exceeds the JSON size limit"
        )
    try:
        manifest = json.loads(manifest_raw.decode("utf-8"))
    except (UnicodeError, json.JSONDecodeError) as exc:
        raise PreparationError(
            "morning MCP release manifest is not valid UTF-8 JSON"
        ) from exc
    if not isinstance(manifest, dict):
        raise PreparationError(
            "morning MCP release manifest JSON root must be an object"
        )
    if (
        hashlib.sha256(manifest_raw).hexdigest()
        != pointer["release_manifest_sha256"]
        or manifest.get("release_id") != pointer["release_id"]
        or manifest.get("server_release") != pointer["server_release"]
        or manifest.get("formal_write_count") != 0
    ):
        raise MCPInvocationError("mcp_release_manifest_drift", retryable=False)
    python = Path(str(pointer.get("python") or MCP_PYTHON))
    try:
        python_resolved = python.resolve(strict=True)
    except OSError as exc:
        raise MCPInvocationError("mcp_python_unavailable", retryable=True) from exc
    if not python_resolved.is_file() or not os.access(python, os.X_OK):
        raise MCPInvocationError("mcp_python_unavailable", retryable=True)
    return {**pointer, "release_root": str(resolved), "python": str(python)}


def _scope_hash(review_date: str, queue_sha256: str, item_ids: list[str]) -> str:
    return _digest(
        {
            "review_date": review_date,
            "queue_sha256": queue_sha256,
            "item_ids": item_ids,
        }
    )


def _invoke_mcp(
    repo: Path,
    state: dict[str, Any],
    chunk: dict[str, Any],
) -> tuple[dict[str, Any], dict[str, Any]]:
    release = _load_mcp_release()
    item_ids = list(chunk["item_ids"])
    scope = _scope_hash(str(state["review_date"]), str(state["queue_sha256"]), item_ids)
    chunk_count = len(state.get("chunks") or _chunk_plan(list(state["item_order"])))
    request = {
        "tool": "cs408_morning_preparation_bundle",
        "arguments": {
            "request": {
                "review_date": state["review_date"],
                "queue_sha256": state["queue_sha256"],
                "item_ids": item_ids,
            },
            "expected_release": release["server_release"],
            "route": {
                "caller_skill_id": "kaoyan-408-morning-control",
                "caller_skill_version": "3.0.0",
                "plugin_version": str(release.get("plugin_version") or "0.3.0"),
                "route_request_id": f"{state['job_id']}-{chunk['chunk_id']}",
                "evidence_scope_hash": scope,
                "read_route": "mcp_chunked" if chunk_count > 1 else "mcp",
                "chunk_index": chunk["chunk_index"],
                "chunk_count": chunk_count,
                "consumed_duplicate_read_count": 0,
            },
        },
    }
    env = {
        "PATH": "/usr/bin:/bin",
        "PYTHONUTF8": "1",
        "PYTHONDONTWRITEBYTECODE": "1",
        "PYTHONPATH": str(Path(release["release_root"]) / "src"),
        "STUDY_READ_MCP_EXPECTED_PROJECT_ROOT": release["release_root"],
        "STUDY_READ_MCP_EXPECTED_RELEASE_ID": release["release_id"],
        "STUDY_READ_MCP_EXPECTED_RELEASE_MANIFEST_SHA256": release[
            "release_manifest_sha256"
        ],
    }
    try:
        result = subprocess.run(
            [
                release["python"],
                "-m",
                "study_read_mcp.client",
                "--profile",
                "morning_preparation",
                "--subject",
                "cs408",
                "--project-root",
                release["release_root"],
            ],
            cwd=release["release_root"],
            input=json.dumps(request, ensure_ascii=False),
            text=True,
            capture_output=True,
            timeout=15,
            check=False,
            env=env,
        )
    except subprocess.TimeoutExpired as exc:
        raise MCPInvocationError("mcp_timeout", retryable=True) from exc
    if result.returncode != 0:
        raise MCPInvocationError("mcp_transport_failed", retryable=True)
    try:
        bundle = json.loads(result.stdout)
    except json.JSONDecodeError as exc:
        raise MCPInvocationError("mcp_response_invalid", retryable=True) from exc
    if not isinstance(bundle, dict) or bundle.get("ok") is not True:
        error = bundle.get("error") if isinstance(bundle, dict) else None
        code = str(error.get("code") if isinstance(error, dict) else "mcp_read_failed")
        retryable = bool(error.get("retryable")) if isinstance(error, dict) else True
        raise MCPInvocationError(f"mcp_{code.lower()}", retryable=retryable)
    if (
        bundle.get("profile") != "morning_preparation"
        or bundle.get("queue_sha256") != state["queue_sha256"]
        or bundle.get("evidence_scope_hash") != scope
        or bundle.get("server_release") != release["server_release"]
        or bundle.get("formal_write_count") != 0
        or bundle.get("learner_evidence_write_count") != 0
        or bundle.get("model_call_count") != 0
        or bundle.get("mcp_tool_call_count") != 1
    ):
        raise MCPInvocationError("mcp_binding_invalid", retryable=False)
    if state.get("queue_ref"):
        try:
            import morning_semantic_selection_408 as semantic_selection

            bundle = semantic_selection.enrich_mcp_bundle(
                repo, str(state["queue_ref"]), bundle
            )
        except (ImportError, OSError, ValueError, KeyError, RuntimeError) as exc:
            raise MCPInvocationError("mcp_semantic_selection_binding_invalid", retryable=False) from exc
    rows = bundle.get("items")
    if not isinstance(rows, list) or [row.get("item_id") for row in rows if isinstance(row, dict)] != item_ids:
        raise MCPInvocationError("mcp_item_order_invalid", retryable=False)
    for row in rows:
        evidence = row.get("evidence")
        if not isinstance(evidence, list) or not evidence:
            raise MCPInvocationError("mcp_source_gap", retryable=False)
        for entry in evidence:
            if not isinstance(entry, dict):
                raise MCPInvocationError("mcp_evidence_invalid", retryable=False)
            path = _repo_file(repo, str(entry.get("source_ref") or ""), "MCP evidence")
            content = entry.get("content")
            if (
                not isinstance(content, str)
                or _file_sha(path) != entry.get("sha256")
                or hashlib.sha256(content.encode("utf-8")).hexdigest() != entry.get("sha256")
            ):
                raise MCPInvocationError("mcp_evidence_hash_drift", retryable=True)
    invocation = {
        "tool": "cs408_morning_preparation_bundle",
        "server_release": bundle["server_release"],
        "generation": bundle["generation"],
        "authority_fingerprint": bundle["authority_fingerprint"],
        "evidence_scope_hash": scope,
        "chunk_id": chunk["chunk_id"],
        "item_count": len(rows),
        "mcp_tool_call_count": 1,
        "model_call_count": 0,
        "learner_evidence_write_count": 0,
        "formal_write_count": 0,
    }
    return bundle, invocation


def _validate_author_chunk(
    repo: Path,
    queue: Path,
    payload: dict[str, Any],
    item_ids: list[str],
) -> None:
    _queue_schema, _review_date, queue_items = prepared_pack._parse_queue(queue)
    queue_by_id = {row["item_id"]: row for row in queue_items}
    items = payload.get("items")
    if not isinstance(items, list) or [row.get("item_id") for row in items if isinstance(row, dict)] != item_ids:
        raise prepared_pack.PreparedPackError("author chunk item order is invalid")
    for raw_item in items:
        item_id = str(raw_item.get("item_id") or "")
        expected = queue_by_id.get(item_id)
        if expected is None or raw_item.get("source_id") != expected["source_id"] or raw_item.get("item_kind") != expected["item_kind"]:
            raise prepared_pack.PreparedPackError("author chunk identity differs from queue")
        if set(raw_item) != {
            "item_id", "source_id", "item_kind", "target", "surface", "evaluator",
            "guided", "repair", "evidence",
        }:
            raise prepared_pack.PreparedPackError("author chunk item fields are invalid")
        prepared_pack._normalize_target(raw_item["target"], f"{item_id}.target")
        prepared_pack._normalize_surface(raw_item["surface"], item_id)
        prepared_pack._normalize_evaluator(raw_item["evaluator"], item_id)
        prepared_pack._normalize_branch(raw_item["guided"], item_id, "guided")
        prepared_pack._normalize_branch(raw_item["repair"], item_id, "repair")
        prepared_pack._normalize_evidence(repo, raw_item["evidence"], item_id)
        try:
            import morning_semantic_selection_408 as semantic_selection

            semantic_candidate = semantic_selection.decision_candidate_for_queue_item(
                repo, queue, item_id
            )
        except (ImportError, OSError, ValueError, KeyError, RuntimeError) as exc:
            raise prepared_pack.PreparedPackError(
                "semantic selection candidate binding is invalid"
            ) from exc
        if semantic_candidate is not None:
            mode = semantic_candidate.get("surface_mode")
            surfaces = [
                raw_item["surface"],
                raw_item["guided"]["surface"],
                raw_item["repair"]["surface"],
            ]
            evaluators = [
                raw_item["evaluator"],
                raw_item["guided"]["evaluator"],
                raw_item["repair"]["evaluator"],
            ]
            if mode == "original_question":
                private = semantic_selection.original_private_evidence(
                    semantic_candidate
                )
                expected_asset = private["question_asset"]
                if raw_item.get("item_kind") != "original_question":
                    raise prepared_pack.PreparedPackError(
                        "original semantic item is not marked original_question"
                    )
                if any(surface.get("question_asset") != expected_asset for surface in surfaces):
                    raise prepared_pack.PreparedPackError(
                        "original semantic item changed or omitted its question asset"
                    )
                if any(
                    evaluator.get("correct_option") != private["correct_option"]
                    for evaluator in evaluators
                ):
                    raise prepared_pack.PreparedPackError(
                        "original semantic item evaluator differs from canonical details"
                    )
            elif mode == "derived_variant":
                if raw_item.get("item_kind") == "original_question" or any(
                    surface.get("question_asset") is not None for surface in surfaces
                ):
                    raise prepared_pack.PreparedPackError(
                        "derived semantic item cannot masquerade as an original"
                    )
            else:
                raise prepared_pack.PreparedPackError(
                    "semantic item surface_mode is invalid"
                )


def _retry_at(attempt: int) -> str:
    delay = min(RETRY_MAX_SECONDS, RETRY_BASE_SECONDS * (2 ** max(0, attempt - 1)))
    value = dt.datetime.now(dt.timezone.utc) + dt.timedelta(seconds=delay)
    return value.isoformat().replace("+00:00", "Z")


def _retry_due(state: dict[str, Any]) -> bool:
    value = state.get("next_retry_at")
    if not isinstance(value, str) or not value:
        return True
    try:
        due = dt.datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return True
    return dt.datetime.now(dt.timezone.utc) >= due


def _author_prompt(state: dict[str, Any], queue_text: str, execution_id: str) -> str:
    return f"""You are the isolated author for one 408 morning-review prepared pack.
Return one JSON object with key payload. payload must be a complete
morning_review_prepared_pack_draft_v1 object. Set author_actor_id to
codex-pack-author and author_execution_id to {execution_id}. Bind queue_sha256 to
{state['queue_sha256']}. Cover every item exactly once and in this order:
{json.dumps(state['item_order'], ensure_ascii=False)}.

Create separate main, guided, and repair A/B/C/D surfaces and private evaluators.
Every surface has exactly four mutually exclusive options and one source-defensible
best answer. Use only reliable repository evidence that you inspect read-only. Put
only repository-relative evidence refs and exact SHA-256 values in evidence. Never
reconstruct an original from a thin safe summary. If evidence is insufficient,
return payload={{"status":"needs_source","missing_item_ids":[...]}} instead of
inventing a question. Use the response instruction: 回复选项即可；若不是高置信度，请注明中或低.
Do not write files or learning state.

Immutable queue:
{queue_text}
"""


def _verifier_prompt(
    state: dict[str, Any], draft: dict[str, Any], hashes: dict[str, Any], execution_id: str
) -> str:
    return f"""You are the independent verifier for one frozen 408 morning pack.
Inspect every repository-relative evidence reference read-only and independently
check each public/private pair. Return one JSON object with key payload. payload
must contain verifier_actor_id=codex-pack-verifier,
verifier_execution_id={execution_id}, and items in exact order. Each item row has
item_id and a checks object with these keys, all booleans:
{json.dumps(hashes['verification_checks_required'], ensure_ascii=False)}.
Set a check true only when the evidence directly supports it. If any check fails,
return it false; publication will fail closed. Do not write files or learning state.

Bound draft:
{json.dumps(draft, ensure_ascii=False, sort_keys=True)}

Deterministic hash rows:
{json.dumps(hashes['item_hashes'], ensure_ascii=False, sort_keys=True)}
"""


def _revision_note(revision_guidance: object) -> str:
    if not isinstance(revision_guidance, dict):
        return ""
    safe_failed_items: list[dict[str, Any]] = []
    failed_checks: set[str] = set()
    raw_failed_items = revision_guidance.get("failed_items")
    if isinstance(raw_failed_items, list):
        for raw_item in raw_failed_items:
            if not isinstance(raw_item, dict):
                continue
            item_id = raw_item.get("item_id")
            raw_checks = raw_item.get("failed_checks")
            if not isinstance(item_id, str) or not isinstance(raw_checks, list):
                continue
            checks = [
                check
                for check in raw_checks
                if isinstance(check, str) and check in prepared_pack.CHECKS
            ]
            if not checks:
                continue
            safe_failed_items.append({"item_id": item_id, "failed_checks": checks})
            failed_checks.update(checks)
    if not safe_failed_items:
        return ""
    details = [
        "\nIndependent verifier revision requirements from the prior immutable attempt:\n",
        json.dumps(
            {"failed_items": safe_failed_items},
            ensure_ascii=False,
            sort_keys=True,
        ),
        "\nRewrite the chunk so every listed failed check is satisfied.\n",
    ]
    details.append(
        "For standalone_surface, the stem must contain all facts needed to answer and "
        "must not refer to another question, a prior prompt, a diagram that is not "
        "included, or implicit context.\n"
    )
    if "source_grounded" in failed_checks:
        details.append(
            "For source_grounded, apply the same source-grounding contract above: keep the "
            "mechanism and target directly supported; use synthetic exact parameters only "
            "under explicit evidence authorization for migration and within the stated "
            "scale, units, boundaries, and mechanism; make the stem self-contained with one "
            "unique best answer; and never present synthetic values as original facts. If "
            "those conditions cannot be met, return needs_source for that item.\n"
        )
    return "".join(details)


def _author_chunk_prompt(
    state: dict[str, Any],
    chunk: dict[str, Any],
    bundle: dict[str, Any],
    execution_id: str,
) -> str:
    protected = {
        "server_release": bundle["server_release"],
        "generation": bundle["generation"],
        "authority_fingerprint": bundle["authority_fingerprint"],
        "evidence_scope_hash": bundle["evidence_scope_hash"],
        "items": bundle["items"],
    }
    revision_note = _revision_note(chunk.get("revision_guidance"))
    return f"""You are the isolated author for chunk {chunk['chunk_id']} of one 408 morning-review pack.
Return one JSON object with key payload. payload is a morning_review_prepared_pack_draft_v1
object containing exactly these items in order: {json.dumps(chunk['item_ids'])}.
Set queue_sha256 to {state['queue_sha256']}. The host will replace actor and execution IDs.

The protected MCP bundle below is the only authority. It was obtained by the host through
cs408_morning_preparation_bundle. Do not call tools, inspect the repository, use shell, or
invent missing facts. Every evidence citation must copy one exact source_ref and sha256 from
the corresponding MCP item. If the bundle is insufficient, return
payload={{"status":"needs_source","missing_item_ids":[...]}}.

For each item create separate main, guided, and repair A/B/C/D surfaces and private evaluators.
Each surface has exactly four mutually exclusive options and one source-defensible best answer.
Use response_instruction: 回复选项即可；若不是高置信度，请注明中或低.
Copy source_id and item_kind exactly from the MCP item. In target, use only exact ASCII IDs
that are explicitly present in the evidence; use an empty list instead of inventing an ID or
putting a Chinese phrase in an ID field. Public stems, option values, and instructions must not
name an option letter, state a correct/incorrect option, or contain answer-bearing wording.
Every evaluator must have at least one accepted_reasoning_points entry. Copy evidence entries
exactly as source_ref, sha256, and role, without the content field.
For item_kind=original_question, original_private_evidence is authoritative and mandatory.
Do not author or paraphrase a replacement question. Copy its question_asset object into main,
guided, and repair surfaces unchanged; use a short stem telling the learner to answer the bound
original image; use options A-D only as references to the image's A-D choices; and copy the exact
correct_option from original_private_evidence into every private evaluator. Guided and repair
must repeat the same original asset, never a variant. For non-original items, set question_asset
to null and create one grounded derived variant under the source-grounding contract.
{AUTHOR_SOURCE_GROUNDING_CONTRACT}
{revision_note}
Do not write any file or study state.

Protected MCP bundle:
{json.dumps(protected, ensure_ascii=False, sort_keys=True)}
"""


def _verifier_chunk_prompt(
    chunk: dict[str, Any],
    draft_items: list[dict[str, Any]],
    hash_rows: list[dict[str, Any]],
    bundle: dict[str, Any],
    execution_id: str,
) -> str:
    protected = {
        "server_release": bundle["server_release"],
        "generation": bundle["generation"],
        "authority_fingerprint": bundle["authority_fingerprint"],
        "evidence_scope_hash": bundle["evidence_scope_hash"],
        "items": bundle["items"],
    }
    return f"""You are the independent verifier for chunk {chunk['chunk_id']} of a frozen 408 morning pack.
Return one JSON object with key payload. payload contains verifier_actor_id,
verifier_execution_id={execution_id}, and exactly these item IDs in order:
{json.dumps(chunk['item_ids'])}.
Each item has item_id and checks with exactly these boolean keys:
{json.dumps(list(prepared_pack.CHECKS))}.

The protected MCP bundle is the only authority. Do not call tools, inspect the repository, use
shell, or write state. Independently compare every public/private pair to the evidence. Set a
check true only when directly supported; use false on any semantic, identity, answer-isolation,
rubric, or grounding defect.

{VERIFIER_SOURCE_GROUNDING_CONTRACT}
Parameter-migration permission changes only how source_grounded is judged. It does not weaken
standalone_surface, exactly_four_options, mutually_exclusive_options, unique_best_answer,
same_target, answer_isolated, rubric_sufficient, or feedback_safe. Evaluate every check
independently and set each failing check false.
For item_kind=original_question, set same_target/source_grounded true only if all three surfaces
carry the exact same bound question_asset, the evaluator option matches original_private_evidence,
and no generated or paraphrased replacement question appears. For other items, question_asset is
forbidden and the item must be a grounded derived variant.

Bound draft items:
{json.dumps(draft_items, ensure_ascii=False, sort_keys=True)}

Deterministic item hashes:
{json.dumps(hash_rows, ensure_ascii=False, sort_keys=True)}

Protected MCP bundle:
{json.dumps(protected, ensure_ascii=False, sort_keys=True)}
"""


def _record_chunk_failure(
    repo: Path,
    state: dict[str, Any],
    chunk: dict[str, Any],
    *,
    stage: str,
    code: str,
    error_type: str,
    retryable: bool,
    model: str | None = None,
    reasoning: str | None = None,
    returncode: int | None = None,
    failed_items: list[dict[str, Any]] | None = None,
    diagnostic: dict[str, Any] | None = None,
) -> dict[str, Any]:
    receipt = {
        "schema": RECEIPT_SCHEMA,
        "kind": "chunk_failure",
        "job_id": state["job_id"],
        "chunk_id": chunk["chunk_id"],
        "item_ids": list(chunk["item_ids"]),
        "stage": stage,
        "error_code": code,
        "error_type": error_type,
        "requested_model": model,
        "requested_reasoning_effort": reasoning,
        "runtime_identity_status": "requested_unverified" if model else None,
        "confirmed_model": None,
        "confirmed_reasoning_effort": None,
        "returncode": returncode,
        "retryable": retryable,
        "failed_items": failed_items or [],
        "diagnostic": diagnostic,
        "mcp_tool_call_count": int(state.get("mcp_tool_call_count") or 0),
        "learner_evidence_write_count": 0,
        "formal_write_count": 0,
    }
    receipt_ref, receipt_sha = _write_object(repo, "receipts", receipt)
    chunks = [dict(row) for row in state["chunks"]]
    target = next(row for row in chunks if row["chunk_id"] == chunk["chunk_id"])
    attempt = int(
        target.get("author_attempts" if stage in {"mcp", "author"} else "verifier_attempts")
        or 1
    )
    target["failure"] = {
        "code": code,
        "stage": stage,
        "retryable": retryable,
        "receipt_ref": receipt_ref,
        "receipt_sha256": receipt_sha,
        "diagnostic": diagnostic,
    }
    target["status"] = "retry_wait" if retryable else "failed"
    if code == "source_gap" or code == "mcp_source_gap":
        status = "needs_source"
    elif retryable:
        status = "retry_wait"
    elif code == "verifier_semantic_rejected":
        status = "failed_author"
    else:
        status = "failed_verifier" if stage == "verifier" else "failed_author"
    next_retry = _retry_at(attempt) if retryable else None
    return _update_state(
        repo,
        str(state["job_id"]),
        {str(state["status"])},
        {
            "status": status,
            "chunks": chunks,
            "next_retry_at": next_retry,
            "failure": {
                "code": code,
                "stage": stage,
                "error_type": error_type,
                "retryable": retryable,
                "receipt_ref": receipt_ref,
                "receipt_sha256": receipt_sha,
            },
        },
    )


def _author_validation_code(error: prepared_pack.PreparedPackError) -> str:
    text = str(error).casefold()
    if "answer-bearing" in text or "unsafe" in text:
        return "answer_safety"
    if "hash drifted" in text:
        return "evidence_hash"
    if "identity" in text:
        return "identity"
    if "item order" in text:
        return "item_order"
    if "evidence" in text:
        return "evidence"
    if "target" in text:
        return "target"
    if "surface" in text or "options" in text:
        return "surface"
    if "evaluator" in text or "feedback" in text or "first_break" in text:
        return "evaluator"
    if "fields" in text:
        return "fields"
    return "unknown"


def _chunk_bundle(repo: Path, chunk: dict[str, Any]) -> dict[str, Any]:
    binding = chunk.get("mcp")
    if not isinstance(binding, dict):
        raise PreparationError("chunk MCP binding is missing")
    path = _repo_file(repo, str(binding.get("bundle_ref") or ""), "chunk MCP bundle")
    bundle = _read_json(path, "chunk MCP bundle")
    if _digest(bundle) != binding.get("bundle_sha256"):
        raise PreparationError("chunk MCP bundle hash drifted")
    for row in bundle.get("items") or []:
        for evidence in row.get("evidence") or []:
            source = _repo_file(repo, str(evidence.get("source_ref") or ""), "chunk MCP evidence")
            if _file_sha(source) != evidence.get("sha256"):
                raise PreparationError("chunk MCP evidence drifted")
    return bundle


def _run_worker_monolithic_legacy(
    repo: Path,
    job_id: str,
    *,
    codex: Path = DEFAULT_CODEX,
    model: str = DEFAULT_MODEL,
    reasoning: str = DEFAULT_REASONING,
    timeout_seconds: int = 600,
) -> dict[str, Any]:
    repo = _resolved_repo(repo)
    _, state = _load_state(repo, job_id)
    if state.get("status") == "ready":
        return _public_state(state, created=False)
    queue = _repo_file(repo, state["queue_ref"], "job queue")
    if not _job_identity_matches(repo, state):
        next_state = _mark_stale(
            repo,
            job_id,
            expected={
                "pending_author",
                "pending_verifier",
                "needs_source",
                "failed_author",
                "failed_verifier",
            },
        )
        return _public_state(next_state, created=False)
    if state.get("status") in {"pending_author", "needs_source", "failed_author"}:
        execution_id = "author-" + job_id.lower()
        try:
            authored, invocation = _invoke_model(
                codex=codex,
                repo=repo,
                model=model,
                reasoning=reasoning,
                prompt=_author_prompt(
                    state, queue.read_text(encoding="utf-8"), execution_id
                ),
                timeout_seconds=timeout_seconds,
            )
        except ModelInvocationError as exc:
            failed = _record_model_failure(
                repo,
                state,
                stage="author",
                error=exc,
                model=model,
                reasoning=reasoning,
            )
            return _public_state(failed, created=False)
        if not _job_identity_matches(repo, state):
            stale = _mark_stale(
                repo,
                job_id,
                expected={"pending_author", "needs_source", "failed_author"},
            )
            return _public_state(stale, created=False)
        payload = _model_payload(authored, "author")
        if payload.get("status") == "needs_source":
            next_state = _update_state(
                repo,
                job_id,
                {"pending_author", "needs_source", "failed_author"},
                {
                    "status": "needs_source",
                    "failure": {
                        "code": "source_gap",
                        "missing_item_ids": payload.get("missing_item_ids") or [],
                        "invocation": invocation,
                    },
                },
            )
            return _public_state(next_state, created=False)
        payload["author_actor_id"] = "codex-pack-author"
        payload["author_execution_id"] = execution_id
        with tempfile.TemporaryDirectory(prefix="morning-pack-author-") as temp_name:
            draft_path = Path(temp_name) / "draft.json"
            draft_path.write_text(
                json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
                encoding="utf-8",
            )
            # The public submit command requires a repository file.  The worker uses
            # the same validator directly, then persists the normalized input.
            hashes = prepared_pack.draft_hashes(repo, queue, draft_path)
            draft_ref, draft_sha = _write_object(repo, "drafts", payload)
        receipt = {
            "schema": RECEIPT_SCHEMA,
            "kind": "model_author_submission",
            "job_id": job_id,
            "draft_ref": draft_ref,
            "draft_sha256": draft_sha,
            "normalized_draft_sha256": hashes["draft_sha256"],
            "invocation": invocation,
            "learner_evidence_write_count": 0,
            "formal_write_count": 0,
        }
        receipt_ref, receipt_sha = _write_object(repo, "receipts", receipt)
        state = _update_state(
            repo,
            job_id,
            {"pending_author", "needs_source", "failed_author"},
            {
                "status": "pending_verifier",
                "author": {
                    "draft_ref": draft_ref,
                    "draft_sha256": draft_sha,
                    "normalized_draft_sha256": hashes["draft_sha256"],
                    "receipt_ref": receipt_ref,
                    "receipt_sha256": receipt_sha,
                },
                "failure": None,
            },
        )
    if not _job_identity_matches(repo, state):
        stale = _mark_stale(
            repo, job_id, expected={"pending_verifier", "failed_verifier"}
        )
        return _public_state(stale, created=False)
    draft_path = _repo_file(repo, state["author"]["draft_ref"], "bound draft")
    draft = _read_json(draft_path, "bound draft")
    hashes = prepared_pack.draft_hashes(repo, queue, draft_path)
    execution_id = "verifier-" + job_id.lower()
    try:
        verified, invocation = _invoke_model(
            codex=codex,
            repo=repo,
            model=model,
            reasoning=reasoning,
            prompt=_verifier_prompt(state, draft, hashes, execution_id),
            timeout_seconds=timeout_seconds,
        )
    except ModelInvocationError as exc:
        failed = _record_model_failure(
            repo,
            state,
            stage="verifier",
            error=exc,
            model=model,
            reasoning=reasoning,
        )
        return _public_state(failed, created=False)
    if not _job_identity_matches(repo, state):
        stale = _mark_stale(
            repo, job_id, expected={"pending_verifier", "failed_verifier"}
        )
        return _public_state(stale, created=False)
    payload = _model_payload(verified, "verifier")
    if not isinstance(payload.get("items"), list):
        raise PreparationError("verifier model output lacks item checks")
    rows_by_id = {
        str(row.get("item_id")): row
        for row in payload["items"]
        if isinstance(row, dict)
    }
    verification = {
        "schema": prepared_pack.VERIFICATION_SCHEMA,
        "draft_sha256": hashes["draft_sha256"],
        "verifier_actor_id": "codex-pack-verifier",
        "verifier_execution_id": execution_id,
        "items": [],
    }
    for hash_row in hashes["item_hashes"]:
        item_id = str(hash_row["item_id"])
        semantic = rows_by_id.get(item_id)
        checks = semantic.get("checks") if isinstance(semantic, dict) else None
        verification["items"].append({**hash_row, "checks": checks})
    with tempfile.TemporaryDirectory(prefix="morning-pack-verifier-") as temp_name:
        verification_path = Path(temp_name) / "verification.json"
        verification_path.write_text(
            json.dumps(verification, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        try:
            result = prepared_pack.publish_pack(
                repo, queue, draft_path, verification_path
            )
        except prepared_pack.PreparedPackError:
            failed = _record_verifier_semantic_rejection(
                repo,
                state,
                verification=verification,
                invocation=invocation,
                model=model,
                reasoning=reasoning,
            )
            return _public_state(failed, created=False)
    if not _job_identity_matches(repo, state):
        stale = _mark_stale(repo, job_id, expected={"pending_verifier"})
        return _public_state(stale, created=False)
    verification_ref, verification_sha = _write_object(
        repo, "verifications", verification
    )
    receipt = {
        "schema": RECEIPT_SCHEMA,
        "kind": "model_verifier_publication",
        "job_id": job_id,
        "verification_ref": verification_ref,
        "verification_sha256": verification_sha,
        "pack_id": result["pack_id"],
        "manifest_ref": result["manifest_ref"],
        "manifest_sha256": result["manifest_sha256"],
        "pack_commitment_sha256": result.get("pack_commitment_sha256"),
        "invocation": invocation,
        "learner_evidence_write_count": 0,
        "formal_write_count": 0,
    }
    receipt_ref, receipt_sha = _write_object(repo, "receipts", receipt)
    state = _update_state(
        repo,
        job_id,
        {"pending_verifier"},
        {
            "status": "ready",
            "verifier": {
                "verification_ref": verification_ref,
                "verification_sha256": verification_sha,
                "receipt_ref": receipt_ref,
                "receipt_sha256": receipt_sha,
            },
            "pack": {
                "pack_id": result["pack_id"],
                "manifest_ref": result["manifest_ref"],
                "manifest_sha256": result["manifest_sha256"],
                "pack_commitment_sha256": result.get(
                    "pack_commitment_sha256"
                ),
            },
            "failure": None,
        },
    )
    return _public_state(state, created=True)


def run_worker(
    repo: Path,
    job_id: str,
    *,
    codex: Path = DEFAULT_CODEX,
    model: str = DEFAULT_MODEL,
    reasoning: str = DEFAULT_REASONING,
    timeout_seconds: int = DEFAULT_TIMEOUT_SECONDS,
) -> dict[str, Any]:
    repo = _resolved_repo(repo)
    _, loaded = _load_state(repo, job_id)
    state = _ensure_chunk_state(loaded)
    if state != loaded:
        state = _update_state(
            repo,
            job_id,
            {str(loaded["status"])},
            {
                "execution_contract_version": state["execution_contract_version"],
                "chunk_size": state["chunk_size"],
                "chunks": state["chunks"],
                "mcp_tool_call_count": 0,
                "next_retry_at": None,
            },
        )
    if state.get("status") == "ready":
        return _public_state(state, created=False)
    failure = state.get("failure") or {}
    if state.get("status") in {"failed_author", "failed_verifier", "needs_source"} and failure.get("retryable") is not True:
        return _public_state(state, created=False)
    if state.get("status") == "retry_wait" and not _retry_due(state):
        return _public_state(state, created=False)

    queue = _repo_file(repo, state["queue_ref"], "job queue")
    if not _job_identity_matches(repo, state):
        stale = _mark_stale(
            repo,
            job_id,
            expected={str(state["status"])},
        )
        return _public_state(stale, created=False)

    if state.get("status") == "retry_wait":
        chunks = [dict(row) for row in state["chunks"]]
        for row in chunks:
            if row.get("status") == "retry_wait":
                row["status"] = "pending_verifier" if row.get("author") else "pending_author"
                row["failure"] = None
        aggregate = "pending_verifier" if all(row.get("author") for row in chunks) else "pending_author"
        state = _update_state(
            repo,
            job_id,
            {"retry_wait"},
            {"status": aggregate, "chunks": chunks, "next_retry_at": None, "failure": None},
        )

    # Compatibility for an already accepted full author submission: split its
    # immutable draft into checkpoint objects without invoking the author again.
    if state.get("author") is not None and all(
        row.get("author") is None for row in state["chunks"]
    ):
        full_path = _repo_file(repo, state["author"]["draft_ref"], "bound author draft")
        full = _read_json(full_path, "bound author draft")
        by_id = {row["item_id"]: row for row in full.get("items") or []}
        chunks = [dict(row) for row in state["chunks"]]
        for row in chunks:
            payload = {
                "schema": prepared_pack.DRAFT_SCHEMA,
                "author_actor_id": "codex-pack-author",
                "author_execution_id": f"author-{job_id.lower()}-{row['chunk_id'].lower()}-adopted",
                "queue_sha256": state["queue_sha256"],
                "items": [by_id[item_id] for item_id in row["item_ids"]],
            }
            ref, sha = _write_object(repo, "author-chunks", payload)
            row["author"] = {
                "chunk_ref": ref,
                "chunk_sha256": sha,
                "receipt_ref": state["author"].get("receipt_ref"),
                "receipt_sha256": state["author"].get("receipt_sha256"),
            }
            row["status"] = "pending_verifier"
        state = _update_state(
            repo,
            job_id,
            {str(state["status"])},
            {"status": "pending_verifier", "chunks": chunks},
        )

    # Author chunks are checkpointed independently. Each chunk first consumes one
    # mandatory MCP bundle and the model sees only that frozen bundle.
    for chunk_index in range(len(state["chunks"])):
        chunk = dict(state["chunks"][chunk_index])
        if chunk.get("mcp") is None:
            try:
                bundle, mcp_invocation = _invoke_mcp(repo, state, chunk)
            except MCPInvocationError as exc:
                failed = _record_chunk_failure(
                    repo,
                    state,
                    chunk,
                    stage="mcp",
                    code=exc.code,
                    error_type="mcp_read_error",
                    retryable=exc.retryable,
                )
                return _public_state(failed, created=False)
            bundle_ref, bundle_sha = _write_object(repo, "mcp-evidence", bundle)
            chunks = [dict(row) for row in state["chunks"]]
            target = chunks[chunk_index]
            target["mcp"] = {
                "bundle_ref": bundle_ref,
                "bundle_sha256": bundle_sha,
                "invocation": mcp_invocation,
            }
            target["status"] = "pending_author"
            state = _update_state(
                repo,
                job_id,
                {str(state["status"])},
                {
                    "status": "pending_author",
                    "chunks": chunks,
                    "mcp_tool_call_count": int(state.get("mcp_tool_call_count") or 0) + 1,
                    "failure": None,
                    "next_retry_at": None,
                },
            )
            chunk = dict(state["chunks"][chunk_index])
        if chunk.get("author") is not None:
            continue
        bundle = _chunk_bundle(repo, chunk)
        attempts = int(chunk.get("author_attempts") or 0) + 1
        execution_id = f"author-{job_id.lower()}-{chunk['chunk_id'].lower()}-a{attempts}"
        try:
            authored, invocation = _invoke_model(
                codex=codex,
                repo=repo,
                model=model,
                reasoning=reasoning,
                prompt=_author_chunk_prompt(state, chunk, bundle, execution_id),
                timeout_seconds=timeout_seconds,
            )
        except ModelInvocationError as exc:
            chunks = [dict(row) for row in state["chunks"]]
            chunks[chunk_index]["author_attempts"] = attempts
            state = _update_state(
                repo, job_id, {str(state["status"])}, {"chunks": chunks}
            )
            failed = _record_chunk_failure(
                repo,
                state,
                chunks[chunk_index],
                stage="author",
                code=exc.error_code,
                error_type=exc.error_type,
                retryable=exc.retryable,
                model=model,
                reasoning=reasoning,
                returncode=exc.returncode,
            )
            return _public_state(failed, created=False)
        if not _job_identity_matches(repo, state):
            stale = _mark_stale(repo, job_id, expected={str(state["status"])})
            return _public_state(stale, created=False)
        payload = _model_payload(authored, "author")
        if payload.get("status") == "needs_source":
            failed = _record_chunk_failure(
                repo,
                state,
                chunk,
                stage="author",
                code="source_gap",
                error_type="source_gap",
                retryable=False,
                model=model,
                reasoning=reasoning,
                failed_items=[{"item_id": value} for value in payload.get("missing_item_ids") or []],
            )
            return _public_state(failed, created=False)
        payload["author_actor_id"] = "codex-pack-author"
        payload["author_execution_id"] = execution_id
        payload["queue_sha256"] = state["queue_sha256"]
        candidate_ref, candidate_sha = _write_object(
            repo, "author-chunk-candidates", payload
        )
        try:
            _validate_author_chunk(repo, queue, payload, list(chunk["item_ids"]))
        except prepared_pack.PreparedPackError as exc:
            failed = _record_chunk_failure(
                repo,
                state,
                chunk,
                stage="author",
                code="deterministic_author_rejected",
                error_type="deterministic_validation",
                retryable=False,
                model=model,
                reasoning=reasoning,
                diagnostic={
                    "validation_code": _author_validation_code(exc),
                    "candidate_ref": candidate_ref,
                    "candidate_sha256": candidate_sha,
                },
            )
            return _public_state(failed, created=False)
        author_ref, author_sha = _write_object(repo, "author-chunks", payload)
        receipt = {
            "schema": RECEIPT_SCHEMA,
            "kind": "model_author_chunk_submission",
            "job_id": job_id,
            "chunk_id": chunk["chunk_id"],
            "item_ids": list(chunk["item_ids"]),
            "author_chunk_ref": author_ref,
            "author_chunk_sha256": author_sha,
            "mcp_bundle_sha256": chunk["mcp"]["bundle_sha256"],
            "mcp_server_release": chunk["mcp"]["invocation"]["server_release"],
            "mcp_evidence_scope_hash": chunk["mcp"]["invocation"]["evidence_scope_hash"],
            "invocation": invocation,
            "mcp_tool_call_count": 1,
            "learner_evidence_write_count": 0,
            "formal_write_count": 0,
        }
        receipt_ref, receipt_sha = _write_object(repo, "receipts", receipt)
        chunks = [dict(row) for row in state["chunks"]]
        chunks[chunk_index].update(
            {
                "status": "pending_verifier",
                "author_attempts": attempts,
                "author": {
                    "chunk_ref": author_ref,
                    "chunk_sha256": author_sha,
                    "receipt_ref": receipt_ref,
                    "receipt_sha256": receipt_sha,
                },
                "failure": None,
            }
        )
        state = _update_state(
            repo,
            job_id,
            {str(state["status"])},
            {"status": "pending_author", "chunks": chunks, "failure": None},
        )

    if not _job_identity_matches(repo, state):
        stale = _mark_stale(repo, job_id, expected={str(state["status"])})
        return _public_state(stale, created=False)

    # Merge only after every author chunk passed deterministic validation. The
    # full draft is validated against the complete immutable queue exactly once.
    merged_items: list[dict[str, Any]] = []
    for chunk in state["chunks"]:
        author_path = _repo_file(repo, chunk["author"]["chunk_ref"], "author chunk")
        author_chunk = _read_json(author_path, "author chunk")
        merged_items.extend(author_chunk["items"])
    draft = {
        "schema": prepared_pack.DRAFT_SCHEMA,
        "author_actor_id": "codex-pack-author",
        "author_execution_id": f"author-{job_id.lower()}-merged",
        "queue_sha256": state["queue_sha256"],
        "items": merged_items,
    }
    with tempfile.TemporaryDirectory(prefix="morning-pack-merge-") as temp_name:
        draft_temp = Path(temp_name) / "draft.json"
        draft_temp.write_text(
            json.dumps(draft, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        hashes = prepared_pack.draft_hashes(repo, queue, draft_temp)
    draft_ref, draft_sha = _write_object(repo, "drafts", draft)
    if state.get("author") is None or state["author"].get("draft_sha256") != draft_sha:
        receipt = {
            "schema": RECEIPT_SCHEMA,
            "kind": "model_author_chunks_merged",
            "job_id": job_id,
            "draft_ref": draft_ref,
            "draft_sha256": draft_sha,
            "normalized_draft_sha256": hashes["draft_sha256"],
            "chunk_receipt_sha256s": [row["author"]["receipt_sha256"] for row in state["chunks"]],
            "mcp_tool_call_count": int(state.get("mcp_tool_call_count") or 0),
            "learner_evidence_write_count": 0,
            "formal_write_count": 0,
        }
        receipt_ref, receipt_sha = _write_object(repo, "receipts", receipt)
        state = _update_state(
            repo,
            job_id,
            {str(state["status"])},
            {
                "status": "pending_verifier",
                "author": {
                    "draft_ref": draft_ref,
                    "draft_sha256": draft_sha,
                    "normalized_draft_sha256": hashes["draft_sha256"],
                    "receipt_ref": receipt_ref,
                    "receipt_sha256": receipt_sha,
                },
                "failure": None,
            },
        )

    draft_path = _repo_file(repo, state["author"]["draft_ref"], "bound merged draft")
    draft = _read_json(draft_path, "bound merged draft")
    hashes = prepared_pack.draft_hashes(repo, queue, draft_path)
    draft_by_id = {row["item_id"]: row for row in draft["items"]}
    hash_by_id = {row["item_id"]: row for row in hashes["item_hashes"]}

    for chunk_index in range(len(state["chunks"])):
        chunk = dict(state["chunks"][chunk_index])
        if chunk.get("verifier") is not None:
            continue
        bundle = _chunk_bundle(repo, chunk)
        attempts = int(chunk.get("verifier_attempts") or 0) + 1
        execution_id = f"verifier-{job_id.lower()}-{chunk['chunk_id'].lower()}-a{attempts}"
        try:
            verified, invocation = _invoke_model(
                codex=codex,
                repo=repo,
                model=model,
                reasoning=reasoning,
                prompt=_verifier_chunk_prompt(
                    chunk,
                    [draft_by_id[item_id] for item_id in chunk["item_ids"]],
                    [hash_by_id[item_id] for item_id in chunk["item_ids"]],
                    bundle,
                    execution_id,
                ),
                timeout_seconds=timeout_seconds,
            )
        except ModelInvocationError as exc:
            chunks = [dict(row) for row in state["chunks"]]
            chunks[chunk_index]["verifier_attempts"] = attempts
            state = _update_state(
                repo, job_id, {str(state["status"])}, {"chunks": chunks}
            )
            failed = _record_chunk_failure(
                repo,
                state,
                chunks[chunk_index],
                stage="verifier",
                code=exc.error_code,
                error_type=exc.error_type,
                retryable=exc.retryable,
                model=model,
                reasoning=reasoning,
                returncode=exc.returncode,
            )
            return _public_state(failed, created=False)
        if not _job_identity_matches(repo, state):
            stale = _mark_stale(repo, job_id, expected={str(state["status"])})
            return _public_state(stale, created=False)
        payload = _model_payload(verified, "verifier")
        rows = payload.get("items") or []
        if [row.get("item_id") for row in rows if isinstance(row, dict)] != list(chunk["item_ids"]):
            failed = _record_chunk_failure(
                repo,
                state,
                chunk,
                stage="verifier",
                code="deterministic_verifier_rejected",
                error_type="deterministic_validation",
                retryable=False,
                model=model,
                reasoning=reasoning,
            )
            return _public_state(failed, created=False)
        failed_items = [
            {
                "item_id": row["item_id"],
                "failed_checks": sorted(key for key, value in row["checks"].items() if value is not True),
            }
            for row in rows
            if any(value is not True for value in row["checks"].values())
        ]
        if failed_items:
            failed = _record_chunk_failure(
                repo,
                state,
                chunk,
                stage="verifier",
                code="verifier_semantic_rejected",
                error_type="semantic_gate_rejected",
                retryable=False,
                model=model,
                reasoning=reasoning,
                failed_items=failed_items,
            )
            return _public_state(failed, created=False)
        semantic = {
            "verifier_actor_id": "codex-pack-verifier",
            "verifier_execution_id": execution_id,
            "items": rows,
        }
        verifier_ref, verifier_sha = _write_object(repo, "verifier-chunks", semantic)
        receipt = {
            "schema": RECEIPT_SCHEMA,
            "kind": "model_verifier_chunk_acceptance",
            "job_id": job_id,
            "chunk_id": chunk["chunk_id"],
            "item_ids": list(chunk["item_ids"]),
            "verifier_chunk_ref": verifier_ref,
            "verifier_chunk_sha256": verifier_sha,
            "bound_author_chunk_sha256": chunk["author"]["chunk_sha256"],
            "mcp_bundle_sha256": chunk["mcp"]["bundle_sha256"],
            "invocation": invocation,
            "mcp_tool_call_count": 1,
            "learner_evidence_write_count": 0,
            "formal_write_count": 0,
        }
        receipt_ref, receipt_sha = _write_object(repo, "receipts", receipt)
        chunks = [dict(row) for row in state["chunks"]]
        chunks[chunk_index].update(
            {
                "status": "verified",
                "verifier_attempts": attempts,
                "verifier": {
                    "chunk_ref": verifier_ref,
                    "chunk_sha256": verifier_sha,
                    "receipt_ref": receipt_ref,
                    "receipt_sha256": receipt_sha,
                },
                "failure": None,
            }
        )
        state = _update_state(
            repo,
            job_id,
            {str(state["status"])},
            {"status": "pending_verifier", "chunks": chunks, "failure": None},
        )

    verification_rows: list[dict[str, Any]] = []
    verifier_execution_ids: list[str] = []
    for chunk in state["chunks"]:
        semantic_path = _repo_file(repo, chunk["verifier"]["chunk_ref"], "verifier chunk")
        semantic = _read_json(semantic_path, "verifier chunk")
        verifier_execution_ids.append(semantic["verifier_execution_id"])
        by_id = {row["item_id"]: row["checks"] for row in semantic["items"]}
        for item_id in chunk["item_ids"]:
            verification_rows.append({**hash_by_id[item_id], "checks": by_id[item_id]})
    verification = {
        "schema": prepared_pack.VERIFICATION_SCHEMA,
        "draft_sha256": hashes["draft_sha256"],
        "verifier_actor_id": "codex-pack-verifier",
        "verifier_execution_id": f"verifier-{job_id.lower()}-merged",
        "items": verification_rows,
    }
    with tempfile.TemporaryDirectory(prefix="morning-pack-publish-") as temp_name:
        verification_path = Path(temp_name) / "verification.json"
        verification_path.write_text(
            json.dumps(verification, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        result = prepared_pack.publish_pack(repo, queue, draft_path, verification_path)
    verification_ref, verification_sha = _write_object(repo, "verifications", verification)
    receipt = {
        "schema": RECEIPT_SCHEMA,
        "kind": "model_verifier_chunks_atomic_publication",
        "job_id": job_id,
        "verification_ref": verification_ref,
        "verification_sha256": verification_sha,
        "verifier_execution_ids": verifier_execution_ids,
        "pack_id": result["pack_id"],
        "manifest_ref": result["manifest_ref"],
        "manifest_sha256": result["manifest_sha256"],
        "pack_commitment_sha256": result.get("pack_commitment_sha256"),
        "mcp_tool_call_count": int(state.get("mcp_tool_call_count") or 0),
        "learner_evidence_write_count": 0,
        "formal_write_count": 0,
    }
    receipt_ref, receipt_sha = _write_object(repo, "receipts", receipt)
    state = _update_state(
        repo,
        job_id,
        {"pending_verifier"},
        {
            "status": "ready",
            "verifier": {
                "verification_ref": verification_ref,
                "verification_sha256": verification_sha,
                "receipt_ref": receipt_ref,
                "receipt_sha256": receipt_sha,
            },
            "pack": {
                "pack_id": result["pack_id"],
                "manifest_ref": result["manifest_ref"],
                "manifest_sha256": result["manifest_sha256"],
                "pack_commitment_sha256": result.get(
                    "pack_commitment_sha256"
                ),
            },
            "next_retry_at": None,
            "failure": None,
        },
    )
    return _public_state(state, created=True)


def _ensure_queue(repo: Path, review_date: str) -> Path | None:
    current = queue_generation.load_current_generation_queue(repo, review_date)
    if current is not None:
        freshness = queue_generation.validate_generation_freshness(repo, current)
        if freshness.get("fresh"):
            return current
    result = subprocess.run(
        [
            os.environ.get("PYTHON", "/usr/local/bin/python3"),
            str(repo / "scripts/generate_morning_action_queue.py"),
            "--repo",
            str(repo),
            "--date",
            review_date,
            "--baseline-quota",
            "0",
            "--write-safe",
        ],
        cwd=repo,
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )
    queue = _queue_for_date(repo, review_date)
    return queue if result.returncode == 0 and queue.is_file() else None


def prepare_next_morning(
    repo: Path,
    review_date: str,
    *,
    trigger: str,
    batch_id: str | None,
    ensure_queue: bool,
    execute: bool,
    codex: Path = DEFAULT_CODEX,
    model: str = DEFAULT_MODEL,
    reasoning: str = DEFAULT_REASONING,
    timeout_seconds: int = DEFAULT_TIMEOUT_SECONDS,
) -> dict[str, Any]:
    repo = _resolved_repo(repo)
    review_date = _date(review_date)
    queue = _queue_for_date(repo, review_date)
    if ensure_queue:
        queue = Path(ensure_fresh_generation(repo, review_date)["queue"])
    if not queue.is_file():
        return {
            "status": "needs_queue",
            "review_date": review_date,
            "reason_code": "queue_missing",
            "learner_evidence_write_count": 0,
            "formal_write_count": 0,
        }
    result = enqueue_job(
        repo,
        review_date=review_date,
        queue=queue,
        trigger=trigger,
        batch_id=batch_id,
    )
    if execute and result.get("status") in {
        "pending_author",
        "pending_verifier",
        "needs_source",
        "failed_author",
        "failed_verifier",
        "retry_wait",
    }:
        return run_worker(
            repo,
            str(result["job_id"]),
            codex=codex,
            model=model,
            reasoning=reasoning,
            timeout_seconds=timeout_seconds,
        )
    return result


def guardian_precheck(
    repo: Path,
    review_date: str,
    *,
    ensure_queue: bool,
    enqueue: bool,
) -> dict[str, Any]:
    repo = _resolved_repo(repo)
    review_date = _date(review_date)
    active = _active_session_gate(repo, review_date)
    if active is not None:
        return active
    queue = _queue_for_date(repo, review_date)
    if ensure_queue:
        queue = Path(ensure_fresh_generation(repo, review_date)["queue"])
        if not queue.is_file():
            return {
                "status": "needs_queue",
                "review_date": review_date,
                "reason_code": "queue_generation_blocked",
                "learner_evidence_write_count": 0,
                "formal_write_count": 0,
            }
    if not queue.is_file():
        return {
            "status": "needs_queue",
            "review_date": review_date,
            "reason_code": "queue_missing",
            "learner_evidence_write_count": 0,
            "formal_write_count": 0,
        }
    identity = _job_identity(repo, review_date, queue)
    current = _load_current_pointer(repo, review_date)
    if current is not None:
        current_job_id = str(current.get("job_id") or "")
        if current_job_id:
            _, state = _load_state(repo, current_job_id)
            return _public_state(state, created=False)
    job_id = "MRJ-" + _digest(identity)[:24].upper()
    pointer = _pointer_path(repo, job_id)
    if pointer.is_file():
        _, state = _load_state(repo, job_id)
        return _public_state(state, created=False)
    if not enqueue:
        return {
            "status": "not_enqueued",
            "review_date": review_date,
            "job_id": job_id,
            "queue_ref": identity["queue_ref"],
            "learner_evidence_write_count": 0,
            "formal_write_count": 0,
        }
    return enqueue_job(
        repo,
        review_date=review_date,
        queue=queue,
        trigger="guardian_precheck",
    )


def inspect_receipt(repo: Path, sha: str) -> dict[str, Any]:
    repo = _resolved_repo(repo)
    if not HEX64.fullmatch(sha):
        raise PreparationError("receipt sha256 is invalid")
    path = _root(repo) / "objects" / "receipts" / f"{sha}.json"
    if not path.is_file() or _digest(_read_json(path, "preparation receipt")) != sha:
        raise PreparationError("receipt is missing or hash-invalid")
    return _read_json(path, "preparation receipt")


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo", type=Path, default=Path.cwd())
    sub = parser.add_subparsers(dest="command", required=True)

    enqueue = sub.add_parser("enqueue")
    enqueue.add_argument("--date", required=True)
    enqueue.add_argument("--queue", required=True)
    enqueue.add_argument(
        "--trigger",
        choices=("nightly_closeout", "guardian_precheck", "manual"),
        required=True,
    )
    enqueue.add_argument("--batch-id")

    author = sub.add_parser("submit-author")
    author.add_argument("--job", required=True)
    author.add_argument("--draft", required=True)

    verifier = sub.add_parser("submit-verifier")
    verifier.add_argument("--job", required=True)
    verifier.add_argument("--verification", required=True)

    adopt = sub.add_parser("adopt-current")
    adopt.add_argument("--job", required=True)
    adopt.add_argument("--supersede-duplicates", action="store_true")

    worker = sub.add_parser("run-worker")
    worker.add_argument("--job", required=True)
    worker.add_argument("--codex-path", type=Path, default=DEFAULT_CODEX)
    worker.add_argument("--model", default=DEFAULT_MODEL)
    worker.add_argument("--reasoning-effort", default=DEFAULT_REASONING)
    worker.add_argument("--timeout-seconds", type=int, default=DEFAULT_TIMEOUT_SECONDS)

    prepare = sub.add_parser("prepare-next-morning")
    prepare.add_argument("--date", required=True)
    prepare.add_argument(
        "--trigger",
        choices=("nightly_closeout", "guardian_precheck", "manual"),
        default="manual",
    )
    prepare.add_argument("--batch-id")
    prepare.add_argument("--ensure-queue", action="store_true")
    prepare.add_argument("--execute", action="store_true")
    prepare.add_argument("--codex-path", type=Path, default=DEFAULT_CODEX)
    prepare.add_argument("--model", default=DEFAULT_MODEL)
    prepare.add_argument("--reasoning-effort", default=DEFAULT_REASONING)
    prepare.add_argument("--timeout-seconds", type=int, default=DEFAULT_TIMEOUT_SECONDS)

    precheck = sub.add_parser("guardian-precheck")
    precheck.add_argument("--date", required=True)
    precheck.add_argument("--ensure-queue", action="store_true")
    precheck.add_argument("--enqueue", action="store_true")

    status = sub.add_parser("status")
    status.add_argument("--job", required=True)

    inspect = sub.add_parser("inspect-receipt")
    inspect.add_argument("--sha256", required=True)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    repo = args.repo.expanduser().resolve()
    try:
        if args.command == "enqueue":
            result = enqueue_job(
                repo,
                review_date=args.date,
                queue=Path(args.queue),
                trigger=args.trigger,
                batch_id=args.batch_id,
            )
        elif args.command == "submit-author":
            result = submit_author(repo, args.job, Path(args.draft))
        elif args.command == "submit-verifier":
            result = submit_verifier(repo, args.job, Path(args.verification))
        elif args.command == "adopt-current":
            result = adopt_current_job(
                repo,
                args.job,
                supersede_duplicates=args.supersede_duplicates,
            )
        elif args.command == "run-worker":
            result = run_worker(
                repo,
                args.job,
                codex=args.codex_path.expanduser().resolve(),
                model=args.model,
                reasoning=args.reasoning_effort,
                timeout_seconds=args.timeout_seconds,
            )
        elif args.command == "prepare-next-morning":
            result = prepare_next_morning(
                repo,
                args.date,
                trigger=args.trigger,
                batch_id=args.batch_id,
                ensure_queue=args.ensure_queue,
                execute=args.execute,
                codex=args.codex_path.expanduser().resolve(),
                model=args.model,
                reasoning=args.reasoning_effort,
                timeout_seconds=args.timeout_seconds,
            )
        elif args.command == "guardian-precheck":
            result = guardian_precheck(
                repo,
                args.date,
                ensure_queue=args.ensure_queue,
                enqueue=args.enqueue,
            )
        elif args.command == "status":
            _, state = _load_state(repo, args.job)
            result = _public_state(state, created=False)
        else:
            result = inspect_receipt(repo, args.sha256)
    except (
        OSError,
        UnicodeError,
        ValueError,
        subprocess.TimeoutExpired,
        PreparationError,
        prepared_pack.PreparedPackError,
    ) as exc:
        print(
            json.dumps(
                {
                    "status": "ERROR",
                    "error": str(exc),
                    "learner_evidence_write_count": 0,
                    "formal_write_count": 0,
                },
                ensure_ascii=False,
            ),
            file=os.sys.stderr,
        )
        return 2
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
