#!/usr/bin/env python3
"""Post-commit archive for one sealed local 408 conversation package."""

from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import os
import re
import shutil
import stat
import tempfile
from pathlib import Path
from typing import Any

import conversation_package_408 as package
import current_question_evidence_408 as legacy_evidence
import display_assets_408 as display_assets
import formal_problem_document_408 as formal_document
import intake_fact_capture_408 as capture_model
import sol_curation_decision_408 as sol_decision


ARCHIVE_ROOT = Path("/Volumes/T9-Data")
SUBJECT_RELATIVE_ROOT = Path("01_408/资料库/原始会话资料")
SENTINEL_RELATIVE_PATH = Path("00_迁移管理/状态/volume-sentinel.json")
SENTINEL_SHA256 = "f086b32b29b2b38f1a28dffb8fde4fa850078332d8a23ae269cc8e857a6bd2ca"
VOLUME_NAME = "T9-Data"
VOLUME_UUID = "00000000-0000-0000-0000-000000000000"
LOCATOR_NOTE_ROOT = Path("wiki/sources/raw_archives")
ARCHIVE_RECEIPT_SCHEMA = "cs408-package-archive-receipt-v1"
POINTER_SCHEMA = "cs408-local-archive-pointer-v1"
INTENT_SCHEMA = "cs408-package-archive-intent-v1"
LEGACY_PACKAGE_SCHEMA = "cs408-legacy-evidence-archive-package-v1"
LEGACY_ARCHIVE_RECEIPT_SCHEMA = "cs408-legacy-evidence-archive-receipt-v1"
LEGACY_POINTER_SCHEMA = "cs408-legacy-evidence-archive-pointer-v1"
LEGACY_INTENT_SCHEMA = "cs408-legacy-evidence-archive-intent-v1"
LEGACY_POINTER_ROOT = Path("legacy-archive-pointers")
LEGACY_INTENT_ROOT = Path("legacy-archive-intents")
LEGACY_ARCHIVE_TYPE = "legacy_current_question_evidence"
LEGACY_PREVIEW_STATUS = "LEGACY_ARCHIVE_PREVIEW"
LEGACY_ARCHIVE_PENDING_STATUS = "legacy_archive_pending"
LEGACY_ARCHIVE_AUTH_PREFIX = "CS408-LEGACY-ARCHIVE-"
LEDGER_ONLY_LOCATOR_PREFIX = "cs408-ledger-only://sha256/"
DEFAULT_DETAILS_ROOT = Path("/Users/your-user/Documents/kaoyan-408-details")
MAX_REFERENCE_FILE_BYTES = 64 * 1024 * 1024
MAX_PRIVATE_PROOF_BYTES = 4 * 1024 * 1024
MAX_POINTERS = 4096
PENDING_STATUS = "FORMAL_COMMITTED_ARCHIVE_PENDING"
INCOMPLETE_STATUS = "FORMAL_PROCESSING_INCOMPLETE"
ARCHIVABLE_OUTCOMES = {
    "curated", "created", "updated", "already_current", "skip", "skipped",
    "noop", "duplicate", "concept_only",
}
SHA256_RE = package.SHA256_RE
FORMAL_ID_RE = display_assets.FORMAL_ID_RE
CAPTURE_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{2,255}$")
FaultInjector = Any


class ArchiveError(RuntimeError):
    pass


class TerminalProofMissing(ArchiveError):
    pass


def _json_bytes(value: Any) -> bytes:
    return (json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n").encode("utf-8")


def _sha256(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


def _relative(value: str) -> Path:
    path = Path(value)
    if (
        path.is_absolute()
        or not path.parts
        or any(part in {"", ".", ".."} for part in path.parts)
    ):
        raise ArchiveError("relative archive path invalid")
    return path


def _atomic_write(path: Path, raw: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    tmp = Path(temporary)
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(raw)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp, path)
    finally:
        if tmp.exists():
            tmp.unlink()


def _authorized_root(root: Path) -> Path:
    try:
        info = root.lstat()
    except OSError as exc:
        raise ArchiveError(f"authorized root is missing: {root}") from exc
    if stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode):
        raise ArchiveError(f"authorized root is unsafe: {root}")
    resolved = root.resolve(strict=True)
    if resolved == Path(resolved.anchor):
        raise ArchiveError("authorized root is too broad")
    return resolved


def _assert_safe_components(
    root: Path, target: Path, *, allow_missing: bool
) -> tuple[Path, Path]:
    lexical_root = root.expanduser().absolute()
    authorized = _authorized_root(root)
    supplied = target.expanduser().absolute() if target.is_absolute() else lexical_root / target
    try:
        relative = supplied.relative_to(authorized)
    except ValueError:
        try:
            relative = supplied.relative_to(lexical_root)
        except ValueError as exc:
            raise ArchiveError("path escaped its exact authorized root") from exc
    candidate = authorized / relative
    cursor = authorized
    parts = relative.parts
    for index, part in enumerate(parts):
        cursor = cursor / part
        exists = cursor.exists() or cursor.is_symlink()
        if not exists:
            if not allow_missing:
                raise ArchiveError(f"required path component is missing: {cursor}")
            continue
        info = cursor.lstat()
        if stat.S_ISLNK(info.st_mode):
            raise ArchiveError(f"symlink path component is forbidden: {cursor}")
        if index < len(parts) - 1 and not stat.S_ISDIR(info.st_mode):
            raise ArchiveError(f"non-directory parent component is forbidden: {cursor}")
    resolved_candidate = candidate.resolve(strict=False)
    try:
        resolved_candidate.relative_to(authorized)
    except ValueError as exc:
        raise ArchiveError("resolved path escaped its exact authorized root") from exc
    return authorized, candidate


def _safe_mkdir_chain(root: Path, directory: Path, *, private: bool = False) -> Path:
    authorized, target = _assert_safe_components(
        root, directory, allow_missing=True
    )
    cursor = authorized
    for part in target.relative_to(authorized).parts:
        cursor = cursor / part
        if cursor.exists() or cursor.is_symlink():
            info = cursor.lstat()
            if stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode):
                raise ArchiveError(f"unsafe directory component: {cursor}")
        else:
            cursor.mkdir(mode=0o700 if private else 0o755)
            info = cursor.lstat()
            if stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode):
                raise ArchiveError(f"created directory is unsafe: {cursor}")
        if private:
            os.chmod(cursor, 0o700)
    _assert_safe_components(authorized, target, allow_missing=False)
    return target


def _safe_atomic_write(
    root: Path, path: Path, raw: bytes, *, private: bool = False
) -> None:
    authorized, target = _assert_safe_components(root, path, allow_missing=True)
    parent = _safe_mkdir_chain(authorized, target.parent, private=private)
    _assert_safe_components(authorized, parent, allow_missing=False)
    if target.exists() or target.is_symlink():
        info = target.lstat()
        if stat.S_ISLNK(info.st_mode) or not stat.S_ISREG(info.st_mode):
            raise ArchiveError(f"atomic-write target is unsafe: {target}")
    descriptor, temporary = tempfile.mkstemp(prefix=f".{target.name}.", dir=parent)
    tmp = Path(temporary)
    try:
        if private:
            os.fchmod(descriptor, 0o600)
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(raw)
            handle.flush()
            os.fsync(handle.fileno())
        _assert_safe_components(authorized, target, allow_missing=True)
        os.replace(tmp, target)
        _assert_safe_components(authorized, target, allow_missing=False)
        if private:
            os.chmod(target, 0o600)
    finally:
        if tmp.exists() or tmp.is_symlink():
            tmp.unlink()


def _assert_symlink_free_tree(root: Path, tree: Path) -> None:
    authorized, target = _assert_safe_components(root, tree, allow_missing=False)
    if not target.is_dir():
        raise ArchiveError("archive tree root is not a directory")
    for path in target.rglob("*"):
        _assert_safe_components(authorized, path, allow_missing=False)
        if path.is_symlink():
            raise ArchiveError(f"archive tree contains symlink: {path}")


def _safe_replace_directory(root: Path, source: Path, target: Path) -> None:
    authorized, source_path = _assert_safe_components(
        root, source, allow_missing=False
    )
    _assert_symlink_free_tree(authorized, source_path)
    _authorized, target_path = _assert_safe_components(
        authorized, target, allow_missing=True
    )
    _safe_mkdir_chain(authorized, target_path.parent)
    if target_path.exists() or target_path.is_symlink():
        raise ArchiveError("atomic archive destination already exists")
    os.replace(source_path, target_path)
    _assert_safe_components(authorized, target_path, allow_missing=False)
    _assert_symlink_free_tree(authorized, target_path)


def verify_volume(archive_root: str | Path = ARCHIVE_ROOT) -> dict[str, Any]:
    root = Path(archive_root).expanduser()
    if str(root) != str(ARCHIVE_ROOT) or root.name != VOLUME_NAME:
        raise ArchiveError("archive root must be the canonical /Volumes/T9-Data mount")
    if root.is_symlink() or not root.is_dir():
        raise ArchiveError("canonical T9-Data volume is not mounted safely")
    sentinel = root / SENTINEL_RELATIVE_PATH
    raw = _read_bounded_regular(sentinel, boundary=root)
    if _sha256(raw) != SENTINEL_SHA256:
        raise ArchiveError("T9-Data sentinel SHA-256 mismatch")
    value = json.loads(raw.decode("utf-8"))
    if not isinstance(value, dict):
        raise ArchiveError("T9-Data sentinel is not an object")
    volume_name = value.get("volume_name", value.get("name"))
    volume_uuid = value.get("volume_uuid", value.get("uuid", value.get("UUID")))
    if volume_name != VOLUME_NAME or volume_uuid != VOLUME_UUID:
        raise ArchiveError("T9-Data sentinel identity mismatch")
    return {
        "sentinel_relative_path": str(SENTINEL_RELATIVE_PATH),
        "sentinel_sha256": SENTINEL_SHA256,
        "volume_name": VOLUME_NAME,
        "volume_uuid": VOLUME_UUID,
    }


def _tree_rows(root: Path) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for path in sorted(item for item in root.rglob("*") if item.is_file()):
        if path.name == "archive-receipt.json":
            continue
        if path.is_symlink():
            raise ArchiveError("package tree contains a symlink")
        raw = path.read_bytes()
        rows.append(
            {
                "relative_path": str(path.relative_to(root)),
                "byte_count": len(raw),
                "sha256": _sha256(raw),
            }
        )
    return rows


def _verify_copy(local: Path, copied: Path) -> tuple[list[dict[str, Any]], str]:
    local_rows = _tree_rows(local)
    copied_rows = _tree_rows(copied)
    if copied_rows != local_rows:
        raise ArchiveError("archive tree bytes or paths drifted")
    manifest = copied / "manifest.json"
    if not manifest.is_file():
        raise ArchiveError("archived manifest missing")
    manifest_sha = _sha256(manifest.read_bytes())
    return copied_rows, manifest_sha


def _frontmatter(note: dict[str, Any]) -> str:
    formal_ids = note["formal_ids"]
    lines = [
        "---",
        "type: raw_archive_locator",
        "subject: cs408",
        f"package_id: {note['package_id']}",
        "formal_ids:",
    ]
    lines.extend(f"  - {formal_id}" for formal_id in formal_ids)
    lines.append("retrieval_keys:")
    lines.extend(f"  - {key}" for key in note.get("retrieval_keys") or [])
    lines.extend(
        [
            "archive_volume: T9-Data",
            f"raw_archive_relpath: {note['raw_archive_relpath']}",
            f"raw_archive_manifest_sha256: {note['raw_archive_manifest_sha256']}",
            f"raw_archive_package_sha256: {note['raw_archive_package_sha256']}",
            f"archive_verified_at: {note['archive_verified_at']}",
            "archive_status: verified",
            "---",
            "",
            "408 原始会话包已归档。此页只保存经验证的数据盘相对定位。",
            "",
        ]
    )
    return "\n".join(lines)


def _verify_locator_note(path: Path, expected: dict[str, Any]) -> str:
    text = path.read_text(encoding="utf-8")
    required_lines = {
        "type: raw_archive_locator",
        "subject: cs408",
        f"package_id: {expected['package_id']}",
        "archive_volume: T9-Data",
        f"raw_archive_relpath: {expected['raw_archive_relpath']}",
        f"raw_archive_manifest_sha256: {expected['raw_archive_manifest_sha256']}",
        f"raw_archive_package_sha256: {expected['raw_archive_package_sha256']}",
        f"archive_verified_at: {expected['archive_verified_at']}",
        "archive_status: verified",
    }
    if not required_lines <= set(text.splitlines()):
        raise ArchiveError("locator note reread validation failed")
    for formal_id in expected["formal_ids"]:
        if f"  - {formal_id}" not in text.splitlines():
            raise ArchiveError("locator note formal_ids drifted")
    for key in expected.get("retrieval_keys") or []:
        if f"  - {key}" not in text.splitlines():
            raise ArchiveError("locator note retrieval_keys drifted")
    return _sha256(text.encode("utf-8"))


def _read_locator_note(path: Path) -> dict[str, Any]:
    text = path.read_text(encoding="utf-8")
    lines = text.splitlines()
    if len(lines) < 3 or lines[0] != "---" or "---" not in lines[1:]:
        raise ArchiveError("raw archive locator frontmatter invalid")
    end = lines[1:].index("---") + 1
    result: dict[str, Any] = {}
    formal_ids: list[str] = []
    in_formal_ids = False
    in_retrieval_keys = False
    retrieval_keys: list[str] = []
    for line in lines[1:end]:
        if line == "formal_ids:":
            in_formal_ids = True
            in_retrieval_keys = False
            continue
        if in_formal_ids and line.startswith("  - "):
            formal_ids.append(line[4:])
            continue
        in_formal_ids = False
        if line == "retrieval_keys:":
            in_retrieval_keys = True
            continue
        if in_retrieval_keys and line.startswith("  - "):
            retrieval_keys.append(line[4:])
            continue
        in_retrieval_keys = False
        key, separator, value = line.partition(": ")
        if not separator or not key:
            raise ArchiveError("raw archive locator field invalid")
        result[key] = value
    result["formal_ids"] = formal_ids
    if retrieval_keys:
        result["retrieval_keys"] = retrieval_keys
    required = {
        "type", "subject", "package_id", "formal_ids", "archive_volume",
        "raw_archive_relpath", "raw_archive_manifest_sha256",
        "raw_archive_package_sha256", "archive_verified_at", "archive_status",
    }
    if frozenset(result) not in {
        frozenset(required),
        frozenset(required | {"retrieval_keys"}),
    }:
        raise ArchiveError("raw archive locator fields drifted")
    if (
        result["type"] != "raw_archive_locator"
        or result["subject"] != "cs408"
        or result["archive_volume"] != VOLUME_NAME
        or result["archive_status"] != "verified"
    ):
        raise ArchiveError("raw archive locator identity invalid")
    return result


def _read_bounded_regular(
    path: Path,
    *,
    maximum: int = MAX_PRIVATE_PROOF_BYTES,
    boundary: Path | None = None,
    private: bool = False,
) -> bytes:
    try:
        if boundary is not None:
            authorized, checked = _assert_safe_components(
                boundary, path, allow_missing=False
            )
            checked.resolve(strict=True).relative_to(authorized)
        info = path.lstat()
    except (OSError, ValueError) as exc:
        raise ArchiveError(f"required proof file is missing or escaped: {path}") from exc
    if (
        stat.S_ISLNK(info.st_mode)
        or not stat.S_ISREG(info.st_mode)
        or info.st_size <= 0
        or info.st_size > maximum
        or (private and stat.S_IMODE(info.st_mode) & 0o077)
    ):
        raise ArchiveError(f"required proof file is unsafe: {path}")
    raw = path.read_bytes()
    if len(raw) != info.st_size:
        raise ArchiveError(f"required proof file changed while reading: {path}")
    return raw


def _existing_private_root(value: str | Path | None) -> Path:
    root = (
        Path(value).expanduser()
        if value is not None
        else legacy_evidence.DEFAULT_PRIVATE_ROOT
    )
    if not root.exists():
        raise ArchiveError("legacy evidence root is missing")
    try:
        info = root.lstat()
    except OSError as exc:
        raise ArchiveError("legacy evidence root is unreadable") from exc
    if stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode):
        raise ArchiveError("legacy evidence root is unsafe")
    resolved = root.resolve(strict=True)
    if resolved == Path(resolved.anchor):
        raise ArchiveError("legacy evidence root is too broad")
    return resolved


def _existing_decision_root(value: str | Path | None) -> Path:
    root = (
        Path(value).expanduser()
        if value is not None
        else sol_decision.DEFAULT_DECISION_ROOT
    )
    if not root.exists():
        raise ArchiveError("Sol decision root is missing")
    try:
        info = root.lstat()
    except OSError as exc:
        raise ArchiveError("Sol decision root is unreadable") from exc
    if stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode):
        raise ArchiveError("Sol decision root is unsafe")
    resolved = root.resolve(strict=True)
    if resolved == Path(resolved.anchor):
        raise ArchiveError("Sol decision root is too broad")
    return resolved


def _ledger_rows(repo: Path) -> tuple[list[dict[str, Any]], list[tuple[dict[str, Any], bytes]]]:
    ledger = capture_model.capture_root(repo) / "events.jsonl"
    raw = _read_bounded_regular(
        ledger,
        maximum=max(MAX_PRIVATE_PROOF_BYTES, 64 * 1024 * 1024),
        boundary=repo,
    )
    rows: list[tuple[dict[str, Any], bytes]] = []
    for line_number, line in enumerate(raw.splitlines(keepends=True), start=1):
        if not line.strip():
            continue
        try:
            event = json.loads(line.decode("utf-8"))
        except (UnicodeError, json.JSONDecodeError) as exc:
            raise ArchiveError(
                f"canonical ledger line {line_number} is invalid"
            ) from exc
        if not isinstance(event, dict) or event.get("schema") != capture_model.EVENT_SCHEMA:
            raise ArchiveError(f"canonical ledger line {line_number} schema is invalid")
        rows.append((event, line))
    try:
        state = capture_model.replay([event for event, _line in rows])
    except capture_model.CaptureError as exc:
        raise ArchiveError(f"canonical ledger replay failed: {exc}") from exc
    return [event for event, _line in rows], rows


def _one_ledger_row(
    rows: list[tuple[dict[str, Any], bytes]],
    *,
    event_type: str,
    capture_id: str | None = None,
    batch_id: str | None = None,
    event_id: str | None = None,
) -> tuple[dict[str, Any], bytes]:
    matches = []
    for event, raw in rows:
        if event.get("event_type") != event_type:
            continue
        if capture_id is not None and event.get("capture_id") != capture_id:
            continue
        if batch_id is not None and event.get("batch_id") != batch_id:
            continue
        if event_id is not None and event.get("event_id") != event_id:
            continue
        matches.append((event, raw))
    if len(matches) != 1:
        raise ArchiveError(
            f"canonical ledger requires exactly one {event_type} row; found {len(matches)}"
        )
    return matches[0]


def _read_content_addressed_json(
    path: Path, digest: str, *, boundary: Path
) -> tuple[dict[str, Any], bytes]:
    if not SHA256_RE.fullmatch(digest) or path.name != f"{digest}.json":
        raise ArchiveError("content-addressed proof path is invalid")
    raw = _read_bounded_regular(
        path,
        maximum=MAX_PRIVATE_PROOF_BYTES,
        boundary=boundary,
        private=True,
    )
    if _sha256(raw) != digest:
        raise ArchiveError("content-addressed proof hash drifted")
    try:
        value = json.loads(raw.decode("utf-8"))
    except (UnicodeError, json.JSONDecodeError) as exc:
        raise ArchiveError("content-addressed proof JSON is invalid") from exc
    if not isinstance(value, dict):
        raise ArchiveError("content-addressed proof must be an object")
    return value, raw


def _terminal_and_decision_proof(
    *,
    decision_root: Path,
    capture_id: str,
    batch_id: str,
    result_event: dict[str, Any],
    result_raw: bytes,
    evidence_locator: str,
    evidence_sha256: str,
    capture_payload_sha256: str,
) -> dict[str, Any]:
    terminal_dir = decision_root / "terminals"
    if terminal_dir.is_symlink() or not terminal_dir.is_dir():
        raise ArchiveError("Sol terminal directory is missing or unsafe")
    paths = sorted(terminal_dir.glob("*.json"))
    if len(paths) > MAX_POINTERS:
        raise ArchiveError("Sol terminal inventory exceeds bounded scan limit")
    candidates: list[tuple[dict[str, Any], bytes, str]] = []
    for path in paths:
        digest = path.stem
        if not SHA256_RE.fullmatch(digest):
            continue
        try:
            value, raw = _read_content_addressed_json(
                path, digest, boundary=decision_root
            )
        except ArchiveError:
            continue
        if (
            value.get("schema_version") == sol_decision.TERMINAL_SCHEMA
            and value.get("capture_id") == capture_id
            and value.get("batch_id") == batch_id
            and value.get("item_result_event_id") == result_event.get("event_id")
        ):
            candidates.append((value, raw, digest))
    if not candidates:
        raise TerminalProofMissing(
            "content-addressed Sol terminal object is absent"
        )
    if len(candidates) != 1:
        raise ArchiveError(
            "canonical successful result requires exactly one content-addressed terminal"
        )
    terminal, terminal_raw, terminal_sha256 = candidates[0]
    outcome = str(result_event.get("outcome") or "")
    if outcome not in capture_model.SUCCESS_OUTCOMES:
        raise ArchiveError("canonical result is not a successful terminal")
    if (
        terminal.get("item_result_event_sha256") != _sha256(result_raw)
        or terminal.get("outcome") != outcome
        or terminal.get("formal_id") != result_event.get("formal_id")
        or terminal.get("normal_receipt_sha256")
        != result_event.get("receipt_sha256")
        or terminal.get("verification_sha256")
        != result_event.get("verification_sha256")
    ):
        raise ArchiveError("terminal proof drifted from canonical result bytes")
    decision_locator = str(terminal.get("decision_locator") or "")
    decision_sha256 = str(terminal.get("decision_sha256") or "")
    if (
        not decision_locator.startswith(sol_decision.DECISION_LOCATOR_PREFIX)
        or decision_locator.removeprefix(sol_decision.DECISION_LOCATOR_PREFIX)
        != decision_sha256
    ):
        raise ArchiveError("terminal decision locator/hash binding is invalid")
    decision, decision_raw = _read_content_addressed_json(
        decision_root / "decisions" / f"{decision_sha256}.json",
        decision_sha256,
        boundary=decision_root,
    )
    if (
        decision.get("schema_version") != sol_decision.DECISION_SCHEMA
        or decision.get("capture_id") != capture_id
        or decision.get("batch_id") != batch_id
        or decision.get("capture_payload_sha256") != capture_payload_sha256
        or decision.get("evidence_locator") != evidence_locator
        or decision.get("capture_original_evidence_sha256") != evidence_sha256
        or decision.get("reopened_evidence_sha256") != evidence_sha256
    ):
        raise ArchiveError("Sol decision drifted from capture/evidence truth")
    formal_id = terminal.get("formal_id")
    formal_ids = [formal_id] if formal_id else []
    if outcome in capture_model.SUCCESS_OUTCOMES and not formal_ids:
        raise ArchiveError("successful terminal proof is missing formal ID")
    return {
        "outcome": outcome,
        "formal_ids": formal_ids,
        "decision_locator": decision_locator,
        "decision_sha256": decision_sha256,
        "decision": decision,
        "decision_raw": decision_raw,
        "terminal_locator": sol_decision.TERMINAL_LOCATOR_PREFIX + terminal_sha256,
        "terminal_sha256": terminal_sha256,
        "terminal": terminal,
        "terminal_raw": terminal_raw,
    }


def _legacy_canonical_proof(
    *, repo: str | Path, capture_id: str, decision_root: str | Path | None
) -> dict[str, Any]:
    repo_path = Path(repo).expanduser().resolve(strict=True)
    if not CAPTURE_ID_RE.fullmatch(capture_id):
        raise ArchiveError("capture_id is invalid")
    _events, rows = _ledger_rows(repo_path)
    try:
        state = capture_model.replay([event for event, _raw in rows])
    except capture_model.CaptureError as exc:
        raise ArchiveError(str(exc)) from exc
    capture_row = (state.get("captures") or {}).get(capture_id)
    if not isinstance(capture_row, dict):
        raise ArchiveError("capture_id is absent from canonical ledger")
    if capture_row.get("quality_status") not in capture_model.SUCCESS_OUTCOMES:
        raise ArchiveError("capture does not have a canonical successful terminal")
    batch_id = str(capture_row.get("current_batch_id") or "")
    batch = (state.get("batches") or {}).get(batch_id)
    if not isinstance(batch, dict) or batch.get("status") == "active":
        raise ArchiveError("successful capture lacks a closed canonical batch")
    result = (batch.get("results") or {}).get(capture_id)
    if not isinstance(result, dict) or result.get("outcome") not in capture_model.SUCCESS_OUTCOMES:
        raise ArchiveError("closed batch lacks the canonical successful result")

    refs = (capture_row.get("capture") or {}).get("stable_evidence_refs") or []
    package_refs = [
        ref
        for ref in refs
        if isinstance(ref, dict)
        and str(ref.get("locator") or "").startswith(package.LOCATOR_PREFIX)
    ]
    if package_refs:
        raise ArchiveError("capture has a canonical conversation package; legacy route forbidden")
    evidence_refs = [
        ref
        for ref in refs
        if isinstance(ref, dict)
        and str(ref.get("locator") or "").startswith(legacy_evidence.LOCATOR_PREFIX)
    ]
    if len(evidence_refs) > 1:
        raise ArchiveError("legacy capture has ambiguous current-question evidence")
    archive_mode = (
        "current_question_evidence"
        if evidence_refs
        else "ledger_only_reference_files"
    )
    if evidence_refs:
        evidence_locator = str(evidence_refs[0].get("locator") or "")
        evidence_sha256 = str(evidence_refs[0].get("sha256") or "")
        if (
            not SHA256_RE.fullmatch(evidence_sha256)
            or evidence_locator.removeprefix(legacy_evidence.LOCATOR_PREFIX)
            != evidence_sha256
        ):
            raise ArchiveError("capture evidence locator/SHA binding is invalid")
    else:
        evidence_sha256 = str(capture_row.get("payload_sha256") or "")
        if not SHA256_RE.fullmatch(evidence_sha256):
            raise ArchiveError("ledger-only archive identity SHA is invalid")
        evidence_locator = LEDGER_ONLY_LOCATOR_PREFIX + evidence_sha256

    capture_event, capture_raw = _one_ledger_row(
        rows, event_type="fact_captured", capture_id=capture_id
    )
    result_event, result_raw = _one_ledger_row(
        rows,
        event_type="curation_item_result",
        capture_id=capture_id,
        batch_id=batch_id,
    )
    close_event, close_raw = _one_ledger_row(
        rows, event_type="curation_batch_closed", batch_id=batch_id
    )
    if (
        capture_event.get("payload_sha256") != capture_row.get("payload_sha256")
        or result_event.get("outcome") != result.get("outcome")
        or result_event.get("formal_id") != result.get("formal_id")
        or close_event.get("status") != batch.get("status")
    ):
        raise ArchiveError("canonical capture/result/closeout replay binding drifted")
    ledger_relative = str(
        (capture_model.capture_root(repo_path) / "events.jsonl").relative_to(
            repo_path
        )
    )
    ledger_terminal_event_locator = (
        f"{ledger_relative}#event_id={result_event['event_id']}"
    )
    ledger_terminal_event_sha256 = _sha256(result_raw)
    proof_rows = [
        {
            "role": "canonical_capture_event",
            "relative_path": "proof/capture-event.jsonl",
            "event_id": capture_event.get("event_id"),
            "sha256": _sha256(capture_raw),
            "byte_count": len(capture_raw),
            "raw": capture_raw,
        },
        {
            "role": "canonical_result_event",
            "relative_path": "proof/result-event.jsonl",
            "event_id": result_event.get("event_id"),
            "sha256": _sha256(result_raw),
            "byte_count": len(result_raw),
            "raw": result_raw,
        },
        {
            "role": "canonical_closeout_event",
            "relative_path": "proof/closeout-event.jsonl",
            "event_id": close_event.get("event_id"),
            "sha256": _sha256(close_raw),
            "byte_count": len(close_raw),
            "raw": close_raw,
        },
    ]
    if archive_mode == "current_question_evidence":
        try:
            terminal = _terminal_and_decision_proof(
                decision_root=_existing_decision_root(decision_root),
                capture_id=capture_id,
                batch_id=batch_id,
                result_event=result_event,
                result_raw=result_raw,
                evidence_locator=evidence_locator,
                evidence_sha256=evidence_sha256,
                capture_payload_sha256=str(capture_row.get("payload_sha256") or ""),
            )
        except (TerminalProofMissing, ArchiveError) as exc:
            if isinstance(exc, TerminalProofMissing) or "decision root is missing" in str(exc):
                formal_id = result_event.get("formal_id")
                if not formal_id:
                    raise ArchiveError(
                        "legacy ledger-bound successful result is missing formal ID"
                    ) from exc
                terminal = {
                    "outcome": str(result_event["outcome"]),
                    "formal_ids": [formal_id],
                    "decision_locator": None,
                    "decision_sha256": None,
                    "terminal_locator": None,
                    "terminal_sha256": None,
                }
                archive_mode = "current_question_evidence_ledger_terminal"
                terminal_proof_kind = (
                    "legacy_ledger_bound_no_terminal_object"
                )
            else:
                raise
        else:
            proof_rows.extend(
                [
                    {
                        "role": "sol_decision",
                        "relative_path": "proof/sol-decision.json",
                        "event_id": None,
                        "sha256": terminal["decision_sha256"],
                        "byte_count": len(terminal["decision_raw"]),
                        "raw": terminal["decision_raw"],
                    },
                    {
                        "role": "sol_terminal",
                        "relative_path": "proof/sol-terminal.json",
                        "event_id": result_event.get("event_id"),
                        "sha256": terminal["terminal_sha256"],
                        "byte_count": len(terminal["terminal_raw"]),
                        "raw": terminal["terminal_raw"],
                    },
                ]
            )
            terminal_proof_kind = "sol_content_addressed_terminal"
        capture_reference_count = sum(
            1
            for row in (state.get("captures") or {}).values()
            if evidence_locator
            in {
                str(ref.get("locator") or "")
                for ref in ((row.get("capture") or {}).get("stable_evidence_refs") or [])
                if isinstance(ref, dict)
            }
        )
    else:
        formal_id = result_event.get("formal_id")
        if not formal_id:
            raise ArchiveError("ledger-only successful terminal is missing formal ID")
        terminal = {
            "outcome": str(result_event["outcome"]),
            "formal_ids": [formal_id],
            "decision_locator": None,
            "decision_sha256": None,
            "terminal_locator": (
                ledger_terminal_event_locator
            ),
            "terminal_sha256": ledger_terminal_event_sha256,
        }
        capture_reference_count = 1
        terminal_proof_kind = "canonical_ledger_terminal_facts"
    return {
        "repo": repo_path,
        "capture_id": capture_id,
        "study_date": str(capture_row.get("study_date") or ""),
        "batch_id": batch_id,
        "capture_payload_sha256": str(capture_row.get("payload_sha256") or ""),
        "evidence_locator": evidence_locator,
        "evidence_sha256": evidence_sha256,
        "archive_mode": archive_mode,
        "stable_evidence_refs": [dict(ref) for ref in refs],
        "terminal_proof_kind": terminal_proof_kind,
        "ledger_terminal_event_locator": ledger_terminal_event_locator,
        "ledger_terminal_event_sha256": ledger_terminal_event_sha256,
        "capture_reference_count": capture_reference_count,
        "proof_rows": proof_rows,
        **terminal,
    }


def _legacy_conversation_truth(
    manifest: dict[str, Any], bundle: dict[str, Any], evidence_kind: str
) -> tuple[bool, str]:
    schema = str(manifest.get("bundle_schema_version") or "")
    trace = bundle.get("interaction_trace")
    if evidence_kind == "formal_problem_document_v4":
        complete = isinstance(trace, dict) and trace.get("complete") is True
        return complete, (
            "verified_formal_problem_document_dialogue_complete"
            if complete
            else "formal_problem_document_dialogue_not_complete"
        )
    if schema == legacy_evidence.BUNDLE_SCHEMA_V3 and isinstance(trace, dict):
        complete = (
            trace.get("schema") == "current-question-interaction-trace-v2"
            and trace.get("omitted_event_count") == 0
            and trace.get("original_event_count") == trace.get("included_event_count")
            and trace.get("omitted_ranges") == []
            and trace.get("truncation_reason") is None
        )
        return complete, (
            "verified_v3_trace_complete"
            if complete
            else "verified_v3_trace_incomplete_or_truncated"
        )
    if schema == legacy_evidence.BUNDLE_SCHEMA_V2 and isinstance(trace, dict):
        complete = trace.get("truncated") is False
        return complete, (
            "verified_v2_trace_complete"
            if complete
            else "verified_v2_trace_incomplete_or_truncated"
        )
    return False, "source_evidence_does_not_prove_complete_conversation"


def _legacy_evidence_files(
    *, evidence_locator: str, evidence_root: Path
) -> tuple[dict[str, Any], dict[str, Any], str, list[dict[str, Any]]]:
    try:
        manifest, bundle = legacy_evidence.read_bundle(
            evidence_locator, private_root=evidence_root
        )
        kind = "legacy_current_question_evidence"
    except legacy_evidence.CurrentQuestionEvidenceError:
        try:
            manifest, bundle = formal_document.read_formal_problem_document_bundle(
                evidence_locator, private_root=evidence_root
            )
            kind = "formal_problem_document_v4"
        except formal_document.FormalProblemDocumentError as exc:
            raise ArchiveError(
                "current-question evidence could not be reopened without fabrication"
            ) from exc
    manifest_sha = evidence_locator.removeprefix(legacy_evidence.LOCATOR_PREFIX)
    object_sha = str(manifest.get("object_sha256") or "")
    source_specs: list[tuple[str, str, str | None, int]] = [
        (
            f"manifests/{manifest_sha}.json",
            "evidence_manifest",
            None,
            legacy_evidence.MAX_METADATA_BYTES,
        ),
        (
            f"objects/{object_sha}.json",
            "evidence_bundle",
            None,
            legacy_evidence.MAX_BUNDLE_BYTES,
        ),
    ]
    for row in manifest.get("attachment_objects") or []:
        if not isinstance(row, dict):
            raise ArchiveError("legacy evidence attachment row is invalid")
        digest = str(row.get("sha256") or "")
        source_specs.append(
            (
                f"attachments/{digest}.bin",
                "evidence_attachment",
                str(row.get("role") or "attachment"),
                legacy_evidence.MAX_ATTACHMENT_BYTES,
            )
        )
    files: list[dict[str, Any]] = []
    for source_relative_path, role, source_role, maximum in source_specs:
        source_path = evidence_root / source_relative_path
        raw = _read_bounded_regular(
            source_path,
            maximum=maximum,
            boundary=evidence_root,
            private=True,
        )
        digest = source_path.stem
        if _sha256(raw) != digest:
            raise ArchiveError("legacy evidence source object hash drifted")
        files.append(
            {
                "source_relative_path": source_relative_path,
                "relative_path": f"evidence/{source_relative_path}",
                "role": role,
                "source_role": source_role,
                "sha256": digest,
                "byte_count": len(raw),
                "raw": raw,
            }
        )
    return manifest, bundle, kind, files


def _ledger_reference_files(
    *,
    proof: dict[str, Any],
    details_root: str | Path | None,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    repo = Path(proof["repo"])
    allowed_details = (
        Path(details_root).expanduser()
        if details_root is not None
        else DEFAULT_DETAILS_ROOT
    )
    files: list[dict[str, Any]] = []
    unresolved: list[dict[str, Any]] = []
    for index, ref in enumerate(proof.get("stable_evidence_refs") or [], start=1):
        kind = str(ref.get("kind") or "")
        locator = str(ref.get("locator") or "")
        declared_sha = str(ref.get("sha256") or "")
        base_locator = locator.split("#", 1)[0]
        binding = {
            "ref_index": index,
            "kind": kind,
            "locator": locator,
            "declared_ref_sha256": declared_sha,
        }
        try:
            if not base_locator:
                raise ArchiveError("locator_has_no_file_component")
            supplied = Path(base_locator).expanduser()
            if supplied.is_absolute():
                if not allowed_details.exists():
                    raise ArchiveError("details_allowlist_root_missing")
                details, source_path = _assert_safe_components(
                    allowed_details, supplied, allow_missing=False
                )
                source_scope = "allowlisted_external_details"
                source_path_value = str(source_path.relative_to(details))
                archive_relative = (
                    Path("reference-files")
                    / f"{index:03d}"
                    / "details"
                    / source_path_value
                )
                boundary = details
            else:
                if "://" in base_locator:
                    raise ArchiveError("non_file_locator_scheme")
                relative = _relative(base_locator)
                _authorized, source_path = _assert_safe_components(
                    repo, repo / relative, allow_missing=False
                )
                source_scope = "repo_relative"
                source_path_value = str(relative)
                archive_relative = (
                    Path("reference-files")
                    / f"{index:03d}"
                    / "repo"
                    / relative
                )
                boundary = repo
            raw = _read_bounded_regular(
                source_path,
                maximum=MAX_REFERENCE_FILE_BYTES,
                boundary=boundary,
            )
            files.append(
                {
                    "source_relative_path": (
                        f"{source_scope}:{source_path_value}"
                    ),
                    "source_path_scope": source_scope,
                    "source_path": source_path_value,
                    "source_locator": locator,
                    "declared_ref_sha256": declared_sha,
                    "stable_ref_kind": kind,
                    "relative_path": str(archive_relative),
                    "role": "stable_evidence_reference_file",
                    "source_role": kind,
                    "sha256": _sha256(raw),
                    "byte_count": len(raw),
                    "raw": raw,
                }
            )
        except (ArchiveError, OSError, ValueError) as exc:
            unresolved.append({**binding, "reason": str(exc)})
    return files, unresolved


def _legacy_package_id(capture_id: str, evidence_sha256: str) -> str:
    safe_capture = re.sub(r"[^A-Za-z0-9._-]+", "-", capture_id).strip("-.")
    safe_capture = safe_capture[:96] or "capture"
    value = f"CS408-LEGACY-{safe_capture}-{evidence_sha256[:16]}"
    if not package.SAFE_ID_RE.fullmatch(value):
        raise ArchiveError("derived legacy package ID is invalid")
    return value


def _without_raw(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [{key: value for key, value in row.items() if key != "raw"} for row in rows]


def _authorization_material(
    proof: dict[str, Any],
    *,
    evidence_kind: str,
    evidence_schema_version: str,
    conversation_complete: bool,
    conversation_complete_basis: str,
    source_rows: list[dict[str, Any]],
    unresolved_refs: list[dict[str, Any]],
) -> dict[str, Any]:
    legacy_package_id = _legacy_package_id(
        proof["capture_id"], proof["evidence_sha256"]
    )
    return {
        "schema_version": "cs408-legacy-evidence-archive-authorization-v1",
        "operation": "archive_legacy_evidence_after_formal_commit",
        "capture_id": proof["capture_id"],
        "study_date": proof["study_date"],
        "batch_id": proof["batch_id"],
        "capture_payload_sha256": proof["capture_payload_sha256"],
        "capture_reference_count": proof["capture_reference_count"],
        "archive_mode": proof["archive_mode"],
        "terminal_proof_kind": proof["terminal_proof_kind"],
        "stable_evidence_refs": proof["stable_evidence_refs"],
        "evidence_locator": proof["evidence_locator"],
        "evidence_sha256": proof["evidence_sha256"],
        "evidence_kind": evidence_kind,
        "evidence_schema_version": evidence_schema_version,
        "legacy_package_id": legacy_package_id,
        "canonical_package": False,
        "conversation_complete": conversation_complete,
        "conversation_complete_basis": conversation_complete_basis,
        "outcome": proof["outcome"],
        "formal_ids": proof["formal_ids"],
        "decision_locator": proof["decision_locator"],
        "decision_sha256": proof["decision_sha256"],
        "terminal_locator": proof["terminal_locator"],
        "terminal_sha256": proof["terminal_sha256"],
        "ledger_terminal_event_locator": proof[
            "ledger_terminal_event_locator"
        ],
        "ledger_terminal_event_sha256": proof[
            "ledger_terminal_event_sha256"
        ],
        "source_files": _without_raw(source_rows),
        "unresolved_refs": unresolved_refs,
        "proof_files": _without_raw(proof["proof_rows"]),
        "formal_apply_allowed": False,
        "formal_write_count": 0,
        "mark_result_count": 0,
        "terminal_binding_count": 0,
    }


def _current_authorization_from_pointer(
    proof: dict[str, Any], pointer: dict[str, Any]
) -> tuple[dict[str, Any], str]:
    core = _authorization_material(
        proof,
        evidence_kind=str(pointer["evidence_kind"]),
        evidence_schema_version=str(pointer["evidence_schema_version"]),
        conversation_complete=bool(pointer["conversation_complete"]),
        conversation_complete_basis=str(pointer["conversation_complete_basis"]),
        source_rows=list(pointer["source_files"]),
        unresolved_refs=list(pointer["unresolved_refs"]),
    )
    authorization = LEGACY_ARCHIVE_AUTH_PREFIX + _sha256(_json_bytes(core))
    return core, authorization


def _rebind_pointer_to_current_proof(
    pointer: dict[str, Any], proof: dict[str, Any]
) -> None:
    current_material, current_authorization = _current_authorization_from_pointer(
        proof, pointer
    )
    if (
        current_material != pointer.get("authorization_material")
        or current_authorization != pointer.get("authorization")
        or current_material.get("proof_files")
        != _without_raw(proof["proof_rows"])
        or current_material.get("capture_reference_count")
        != proof.get("capture_reference_count")
    ):
        raise ArchiveError(
            "legacy archive pointer no longer binds current canonical proof/ownership"
        )


def _legacy_manifest_document(
    material: dict[str, Any], archive_verified_at: str
) -> dict[str, Any]:
    files = [
        {
            key: value
            for key, value in row.items()
            if key
            in {
                "source_relative_path",
                "source_path_scope",
                "source_path",
                "source_locator",
                "declared_ref_sha256",
                "stable_ref_kind",
                "relative_path",
                "role",
                "source_role",
                "event_id",
                "sha256",
                "byte_count",
            }
        }
        for row in [*material["source_files"], *material["proof_files"]]
    ]
    content = {
        "archive_package_type": LEGACY_ARCHIVE_TYPE,
        "package_id": material["legacy_package_id"],
        "subject": "cs408",
        "study_date": material["study_date"],
        "capture_id": material["capture_id"],
        "batch_id": material["batch_id"],
        "archive_mode": material["archive_mode"],
        "terminal_proof_kind": material["terminal_proof_kind"],
        "stable_evidence_refs": material["stable_evidence_refs"],
        "evidence_locator": material["evidence_locator"],
        "evidence_sha256": material["evidence_sha256"],
        "evidence_kind": material["evidence_kind"],
        "evidence_schema_version": material["evidence_schema_version"],
        "canonical_package": False,
        "conversation_complete": material["conversation_complete"],
        "conversation_complete_basis": material["conversation_complete_basis"],
        "outcome": material["outcome"],
        "formal_ids": material["formal_ids"],
        "decision_locator": material["decision_locator"],
        "decision_sha256": material["decision_sha256"],
        "terminal_locator": material["terminal_locator"],
        "terminal_sha256": material["terminal_sha256"],
        "ledger_terminal_event_locator": material[
            "ledger_terminal_event_locator"
        ],
        "ledger_terminal_event_sha256": material[
            "ledger_terminal_event_sha256"
        ],
        "authorization": material["authorization"],
        "files": files,
        "unresolved_refs": material["unresolved_refs"],
        "formal_apply_allowed": False,
        "formal_write_count": 0,
        "mark_result_count": 0,
        "terminal_binding_count": 0,
    }
    return {
        "schema_version": LEGACY_PACKAGE_SCHEMA,
        **content,
        "package_content_sha256": _sha256(_json_bytes(content)),
        "archive_verified_at": archive_verified_at,
        "sealed": True,
    }


def _legacy_locator_text(note: dict[str, Any]) -> str:
    lines = [
        "---",
        "type: raw_archive_locator",
        "subject: cs408",
        f"archive_package_type: {LEGACY_ARCHIVE_TYPE}",
        f"package_id: {note['package_id']}",
        f"capture_id: {note['capture_id']}",
        f"archive_mode: {note['archive_mode']}",
        f"terminal_proof_kind: {note['terminal_proof_kind']}",
        f"terminal_object_locator: {note['terminal_object_locator'] or 'null'}",
        f"terminal_object_sha256: {note['terminal_object_sha256'] or 'null'}",
        f"ledger_terminal_event_locator: {note['ledger_terminal_event_locator']}",
        f"ledger_terminal_event_sha256: {note['ledger_terminal_event_sha256']}",
        f"legacy_evidence_locator: {note['legacy_evidence_locator']}",
        "canonical_package: false",
        f"conversation_complete: {'true' if note['conversation_complete'] else 'false'}",
        f"conversation_complete_basis: {note['conversation_complete_basis']}",
        f"resolved_reference_file_count: {note['resolved_reference_file_count']}",
        f"unresolved_reference_count: {note['unresolved_reference_count']}",
        f"terminal_outcome: {note['terminal_outcome']}",
        "formal_ids:",
    ]
    lines.extend(f"  - {formal_id}" for formal_id in note["formal_ids"])
    lines.append("retrieval_keys:")
    lines.extend(f"  - {key}" for key in note.get("retrieval_keys") or [])
    lines.extend(
        [
            "archive_volume: T9-Data",
            f"raw_archive_relpath: {note['raw_archive_relpath']}",
            f"raw_archive_manifest_sha256: {note['raw_archive_manifest_sha256']}",
            f"raw_archive_package_sha256: {note['raw_archive_package_sha256']}",
            f"archive_verified_at: {note['archive_verified_at']}",
            "archive_status: verified",
            "---",
            "",
            "408 历史原始证据已按原字节归档；此页不宣称它是完整会话包。",
            "",
        ]
    )
    return "\n".join(lines)


def _read_legacy_locator_note(path: Path, *, repo_root: Path) -> dict[str, Any]:
    raw = _read_bounded_regular(path, boundary=repo_root)
    try:
        text = raw.decode("utf-8")
    except UnicodeError as exc:
        raise ArchiveError("legacy locator note is not UTF-8") from exc
    lines = text.splitlines()
    if len(lines) < 3 or lines[0] != "---" or "---" not in lines[1:]:
        raise ArchiveError("legacy locator frontmatter is invalid")
    end = lines[1:].index("---") + 1
    values: dict[str, Any] = {}
    formal_ids: list[str] = []
    in_formal_ids = False
    in_retrieval_keys = False
    retrieval_keys: list[str] = []
    for line in lines[1:end]:
        if line == "formal_ids:":
            in_formal_ids = True
            in_retrieval_keys = False
            continue
        if in_formal_ids and line.startswith("  - "):
            formal_ids.append(line[4:])
            continue
        in_formal_ids = False
        if line == "retrieval_keys:":
            in_retrieval_keys = True
            continue
        if in_retrieval_keys and line.startswith("  - "):
            retrieval_keys.append(line[4:])
            continue
        in_retrieval_keys = False
        key, separator, value = line.partition(": ")
        if not separator or not key:
            raise ArchiveError("legacy locator field is invalid")
        values[key] = value
    values["formal_ids"] = formal_ids
    if retrieval_keys:
        values["retrieval_keys"] = retrieval_keys
    required = {
        "type",
        "subject",
        "archive_package_type",
        "package_id",
        "capture_id",
        "archive_mode",
        "terminal_proof_kind",
        "terminal_object_locator",
        "terminal_object_sha256",
        "ledger_terminal_event_locator",
        "ledger_terminal_event_sha256",
        "legacy_evidence_locator",
        "canonical_package",
        "conversation_complete",
        "conversation_complete_basis",
        "resolved_reference_file_count",
        "unresolved_reference_count",
        "terminal_outcome",
        "formal_ids",
        "archive_volume",
        "raw_archive_relpath",
        "raw_archive_manifest_sha256",
        "raw_archive_package_sha256",
        "archive_verified_at",
        "archive_status",
    }
    if frozenset(values) not in {
        frozenset(required),
        frozenset(required | {"retrieval_keys"}),
    }:
        raise ArchiveError("legacy locator fields drifted")
    if (
        values["type"] != "raw_archive_locator"
        or values["subject"] != "cs408"
        or values["archive_package_type"] != LEGACY_ARCHIVE_TYPE
        or values["archive_mode"]
        not in {
            "current_question_evidence",
            "current_question_evidence_ledger_terminal",
            "ledger_only_reference_files",
        }
        or values["canonical_package"] != "false"
        or values["conversation_complete"] not in {"true", "false"}
        or values["archive_volume"] != VOLUME_NAME
        or values["archive_status"] != "verified"
    ):
        raise ArchiveError("legacy locator truth flags are invalid")
    values["canonical_package"] = False
    values["conversation_complete"] = values["conversation_complete"] == "true"
    for field in ("terminal_object_locator", "terminal_object_sha256"):
        if values[field] == "null":
            values[field] = None
    if not SHA256_RE.fullmatch(str(values["ledger_terminal_event_sha256"] or "")):
        raise ArchiveError("legacy locator ledger terminal SHA is invalid")
    for field in ("resolved_reference_file_count", "unresolved_reference_count"):
        try:
            values[field] = int(values[field])
        except ValueError as exc:
            raise ArchiveError("legacy locator reference count is invalid") from exc
        if values[field] < 0:
            raise ArchiveError("legacy locator reference count is invalid")
    values["locator_note_sha256"] = _sha256(raw)
    return values


def _write_legacy_stage(
    stage: Path,
    *,
    archive_root: Path,
    manifest_raw: bytes,
    source_rows: list[dict[str, Any]],
    proof_rows: list[dict[str, Any]],
) -> None:
    authorized, stage_path = _assert_safe_components(
        archive_root, stage, allow_missing=True
    )
    if stage_path.exists() or stage_path.is_symlink():
        if stage_path.is_symlink() or not stage_path.is_dir():
            raise ArchiveError("legacy archive stage is unsafe")
        _assert_symlink_free_tree(authorized, stage_path)
        shutil.rmtree(stage_path)
    _safe_mkdir_chain(authorized, stage_path)
    _safe_atomic_write(authorized, stage_path / "manifest.json", manifest_raw)
    for row in [*source_rows, *proof_rows]:
        _safe_atomic_write(
            authorized, stage_path / str(row["relative_path"]), row["raw"]
        )


def _verify_legacy_package(
    package_dir: Path,
    *,
    archive_root: Path,
    expected_manifest_raw: bytes | None = None,
    receipt_mode: str = "optional",
) -> tuple[dict[str, Any], bytes]:
    if receipt_mode not in {"forbidden", "optional", "required"}:
        raise ArchiveError("legacy archive receipt verification mode is invalid")
    authorized, checked_package = _assert_safe_components(
        archive_root, package_dir, allow_missing=False
    )
    _assert_symlink_free_tree(authorized, checked_package)
    if checked_package.is_symlink() or not checked_package.is_dir():
        raise ArchiveError("legacy archive package directory is unsafe")
    manifest_raw = _read_bounded_regular(
        checked_package / "manifest.json", boundary=checked_package
    )
    if expected_manifest_raw is not None and manifest_raw != expected_manifest_raw:
        raise ArchiveError("legacy archive manifest bytes conflict")
    try:
        manifest = json.loads(manifest_raw.decode("utf-8"))
    except (UnicodeError, json.JSONDecodeError) as exc:
        raise ArchiveError("legacy archive manifest is invalid") from exc
    if (
        not isinstance(manifest, dict)
        or manifest.get("schema_version") != LEGACY_PACKAGE_SCHEMA
        or manifest.get("archive_package_type") != LEGACY_ARCHIVE_TYPE
        or manifest.get("canonical_package") is not False
        or not isinstance(manifest.get("conversation_complete"), bool)
        or manifest.get("sealed") is not True
        or manifest.get("formal_apply_allowed") is not False
        or any(
            manifest.get(field) != 0
            for field in (
                "formal_write_count",
                "mark_result_count",
                "terminal_binding_count",
            )
        )
    ):
        raise ArchiveError("legacy archive manifest identity is invalid")
    content = {
        key: value
        for key, value in manifest.items()
        if key
        not in {
            "schema_version",
            "package_content_sha256",
            "archive_verified_at",
            "sealed",
        }
    }
    if _sha256(_json_bytes(content)) != manifest.get("package_content_sha256"):
        raise ArchiveError("legacy archive package content SHA drifted")
    files = manifest.get("files")
    if not isinstance(files, list) or not files:
        raise ArchiveError("legacy archive file inventory is empty")
    seen: set[str] = set()
    for row in files:
        if not isinstance(row, dict):
            raise ArchiveError("legacy archive file row is invalid")
        relative = _relative(str(row.get("relative_path") or ""))
        relative_text = str(relative)
        if relative_text in seen:
            raise ArchiveError("legacy archive file inventory has duplicates")
        seen.add(relative_text)
        target = checked_package / relative
        raw = _read_bounded_regular(
            target,
            maximum=max(MAX_PRIVATE_PROOF_BYTES, legacy_evidence.MAX_ATTACHMENT_BYTES),
            boundary=package_dir,
        )
        if len(raw) != row.get("byte_count") or _sha256(raw) != row.get("sha256"):
            raise ArchiveError("legacy archive source/proof bytes drifted")
    expected_files = {"manifest.json", *seen}
    receipt_path = checked_package / "archive-receipt.json"
    receipt_exists = receipt_path.exists() or receipt_path.is_symlink()
    if receipt_mode == "required" and not receipt_exists:
        raise ArchiveError("legacy archive receipt is missing from exact tree")
    if receipt_mode == "forbidden" and receipt_exists:
        raise ArchiveError("legacy archive stage unexpectedly contains a receipt")
    if receipt_exists:
        _read_bounded_regular(receipt_path, boundary=checked_package)
        expected_files.add("archive-receipt.json")
    expected_dirs: set[str] = set()
    for relative_text in expected_files:
        parent = Path(relative_text).parent
        while str(parent) != ".":
            expected_dirs.add(str(parent))
            parent = parent.parent
    actual_files: set[str] = set()
    actual_dirs: set[str] = set()
    for path in checked_package.rglob("*"):
        _assert_safe_components(checked_package, path, allow_missing=False)
        info = path.lstat()
        relative_text = str(path.relative_to(checked_package))
        if stat.S_ISLNK(info.st_mode):
            raise ArchiveError("legacy archive exact tree contains a symlink")
        if stat.S_ISDIR(info.st_mode):
            actual_dirs.add(relative_text)
        elif stat.S_ISREG(info.st_mode):
            actual_files.add(relative_text)
        else:
            raise ArchiveError("legacy archive exact tree contains a special file")
    if actual_files != expected_files or actual_dirs != expected_dirs:
        raise ArchiveError("legacy archive exact tree contains extra or missing paths")
    return manifest, manifest_raw


def _pointer_path(evidence_root: Path, evidence_sha256: str) -> Path:
    if not SHA256_RE.fullmatch(evidence_sha256):
        raise ArchiveError("legacy evidence SHA is invalid")
    return evidence_root / LEGACY_POINTER_ROOT / f"{evidence_sha256}.json"


def _normalized_retained_files(value: Any) -> list[dict[str, str]]:
    if not isinstance(value, list):
        raise ArchiveError("legacy retained-file inventory is invalid")
    grouped: dict[str, list[dict[str, str]]] = {}
    for row in value:
        if (
            not isinstance(row, dict)
            or set(row) != {"source_relative_path", "reason"}
            or not isinstance(row.get("source_relative_path"), str)
            or not row["source_relative_path"]
            or not isinstance(row.get("reason"), str)
            or not row["reason"]
        ):
            raise ArchiveError("legacy retained-file row is invalid")
        grouped.setdefault(row["source_relative_path"], []).append(
            {
                "source_relative_path": row["source_relative_path"],
                "reason": row["reason"],
            }
        )
    return [
        min(rows, key=lambda row: _json_bytes(row))
        for _path, rows in sorted(grouped.items())
    ]


def _read_pointer(evidence_root: Path, evidence_sha256: str) -> tuple[dict[str, Any], bytes]:
    path = _pointer_path(evidence_root, evidence_sha256)
    raw = _read_bounded_regular(
        path,
        maximum=MAX_PRIVATE_PROOF_BYTES,
        boundary=evidence_root,
        private=True,
    )
    try:
        pointer = json.loads(raw.decode("utf-8"))
    except (UnicodeError, json.JSONDecodeError) as exc:
        raise ArchiveError("legacy archive pointer JSON is invalid") from exc
    required = {
        "schema_version",
        "status",
        "archive_package_type",
        "canonical_package",
        "conversation_complete",
        "conversation_complete_basis",
        "archive_mode",
        "terminal_proof_kind",
        "stable_evidence_refs",
        "unresolved_refs",
        "repo_root",
        "capture_id",
        "study_date",
        "evidence_locator",
        "evidence_sha256",
        "evidence_kind",
        "evidence_schema_version",
        "legacy_package_id",
        "outcome",
        "formal_ids",
        "terminal_locator",
        "terminal_sha256",
        "ledger_terminal_event_locator",
        "ledger_terminal_event_sha256",
        "authorization",
        "authorization_material",
        "archive_volume",
        "archive_relative_path",
        "archive_manifest_sha256",
        "archive_package_sha256",
        "archive_receipt_sha256",
        "locator_note_relative_path",
        "locator_note_sha256",
        "source_files",
        "cleanup_candidates",
        "retained_files",
        "cleanup_count",
        "formal_apply_allowed",
        "formal_write_count",
        "mark_result_count",
        "terminal_binding_count",
    }
    if set(pointer) != required:
        raise ArchiveError("legacy archive pointer fields drifted")
    if (
        pointer.get("schema_version") != LEGACY_POINTER_SCHEMA
        or pointer.get("status") != "archive_verified"
        or pointer.get("archive_package_type") != LEGACY_ARCHIVE_TYPE
        or pointer.get("archive_mode")
        not in {
            "current_question_evidence",
            "current_question_evidence_ledger_terminal",
            "ledger_only_reference_files",
        }
        or pointer.get("canonical_package") is not False
        or not isinstance(pointer.get("conversation_complete"), bool)
        or pointer.get("evidence_sha256") != evidence_sha256
        or pointer.get("evidence_locator")
        not in {
            legacy_evidence.LOCATOR_PREFIX + evidence_sha256,
            LEDGER_ONLY_LOCATOR_PREFIX + evidence_sha256,
        }
        or pointer.get("archive_volume") != VOLUME_NAME
        or pointer.get("formal_apply_allowed") is not False
        or pointer.get("cleanup_count")
        != (
            0
            if pointer.get("archive_mode") == "ledger_only_reference_files"
            else len(pointer.get("cleanup_candidates") or [])
        )
        or any(
            pointer.get(field) != 0
            for field in (
                "formal_write_count",
                "mark_result_count",
                "terminal_binding_count",
            )
        )
    ):
        raise ArchiveError("legacy archive pointer identity is invalid")
    if (
        not isinstance(pointer.get("ledger_terminal_event_locator"), str)
        or not pointer["ledger_terminal_event_locator"]
        or not SHA256_RE.fullmatch(
            str(pointer.get("ledger_terminal_event_sha256") or "")
        )
    ):
        raise ArchiveError("legacy ledger terminal event binding is invalid")
    if pointer.get("archive_mode") == "current_question_evidence_ledger_terminal":
        if (
            pointer.get("terminal_proof_kind")
            != "legacy_ledger_bound_no_terminal_object"
            or pointer.get("terminal_locator") is not None
            or pointer.get("terminal_sha256") is not None
        ):
            raise ArchiveError("legacy fallback must not synthesize a terminal object")
    material = pointer.get("authorization_material")
    if (
        not isinstance(material, dict)
        or LEGACY_ARCHIVE_AUTH_PREFIX + _sha256(_json_bytes(material))
        != pointer.get("authorization")
    ):
        raise ArchiveError("legacy archive pointer authorization drifted")
    source_files = pointer.get("source_files")
    candidates = pointer.get("cleanup_candidates")
    retained = _normalized_retained_files(pointer.get("retained_files"))
    if (
        source_files != material.get("source_files")
        or pointer.get("unresolved_refs") != material.get("unresolved_refs")
        or pointer.get("stable_evidence_refs")
        != material.get("stable_evidence_refs")
        or not isinstance(source_files, list)
        or not isinstance(candidates, list)
        or candidates != sorted(set(candidates))
    ):
        raise ArchiveError("legacy archive pointer cleanup inventory is invalid")
    if any(
        not isinstance(row, dict)
        or not isinstance(row.get("source_relative_path"), str)
        or not row["source_relative_path"]
        or not isinstance(row.get("relative_path"), str)
        or not row["relative_path"]
        for row in source_files
    ):
        raise ArchiveError("legacy archive source descriptor is invalid")
    archive_descriptor_paths = [str(row["relative_path"]) for row in source_files]
    if len(archive_descriptor_paths) != len(set(archive_descriptor_paths)):
        raise ArchiveError("legacy archive per-reference descriptor paths are not unique")
    source_paths = {str(row["source_relative_path"]) for row in source_files}
    retained_paths = {
        str(row.get("source_relative_path") or "")
        for row in retained
        if isinstance(row, dict) and isinstance(row.get("reason"), str)
    }
    if (
        set(candidates) & retained_paths
        or set(candidates) | retained_paths != source_paths
    ):
        raise ArchiveError("legacy archive pointer cleanup partition drifted")
    return {**pointer, "retained_files": retained}, raw


def verify_legacy_archive_pointer(
    *,
    repo: str | Path,
    evidence_locator: str,
    evidence_root: str | Path,
    archive_root: str | Path = ARCHIVE_ROOT,
) -> dict[str, Any]:
    if not evidence_locator.startswith(
        (legacy_evidence.LOCATOR_PREFIX, LEDGER_ONLY_LOCATOR_PREFIX)
    ):
        raise ArchiveError("legacy evidence locator is invalid")
    evidence_sha256 = (
        evidence_locator.removeprefix(legacy_evidence.LOCATOR_PREFIX)
        if evidence_locator.startswith(legacy_evidence.LOCATOR_PREFIX)
        else evidence_locator.removeprefix(LEDGER_ONLY_LOCATOR_PREFIX)
    )
    root = _existing_private_root(evidence_root)
    pointer, pointer_raw = _read_pointer(root, evidence_sha256)
    repo_path = Path(repo).expanduser().resolve(strict=True)
    if Path(str(pointer["repo_root"])).resolve(strict=True) != repo_path:
        raise ArchiveError("legacy archive pointer repo binding drifted")
    sentinel = verify_volume(archive_root)
    relative = _relative(str(pointer["archive_relative_path"]))
    expected_prefix = SUBJECT_RELATIVE_ROOT / str(pointer["study_date"])
    if (
        relative.parts[: len(expected_prefix.parts)] != expected_prefix.parts
        or relative.name != pointer["legacy_package_id"]
    ):
        raise ArchiveError("legacy archive pointer escaped the dated 408 root")
    archive_base = _authorized_root(Path(archive_root))
    _authorized, archived_dir = _assert_safe_components(
        archive_base, archive_base / relative, allow_missing=False
    )
    try:
        archived_dir.relative_to(archive_base / SUBJECT_RELATIVE_ROOT)
    except ValueError as exc:
        raise ArchiveError("legacy archive pointer escaped T9 408 root") from exc
    manifest, manifest_raw = _verify_legacy_package(
        archived_dir, archive_root=archive_base, receipt_mode="required"
    )
    expected_manifest = _legacy_manifest_document(
        {
            **pointer["authorization_material"],
            "authorization": pointer["authorization"],
        },
        str(manifest.get("archive_verified_at") or ""),
    )
    if (
        manifest != expected_manifest
        or
        _sha256(manifest_raw) != pointer["archive_manifest_sha256"]
        or manifest.get("package_content_sha256")
        != pointer["archive_package_sha256"]
        or manifest.get("package_id") != pointer["legacy_package_id"]
        or manifest.get("capture_id") != pointer["capture_id"]
        or manifest.get("archive_mode") != pointer["archive_mode"]
        or manifest.get("evidence_locator") != evidence_locator
        or manifest.get("terminal_sha256") != pointer["terminal_sha256"]
        or manifest.get("canonical_package") is not False
        or manifest.get("conversation_complete")
        is not pointer["conversation_complete"]
    ):
        raise ArchiveError("legacy archive pointer/manifest binding drifted")
    note_path = repo_path / str(pointer["locator_note_relative_path"])
    note = _read_legacy_locator_note(note_path, repo_root=repo_path)
    if (
        note["locator_note_sha256"] != pointer["locator_note_sha256"]
        or note["package_id"] != pointer["legacy_package_id"]
        or note["capture_id"] != pointer["capture_id"]
        or note["archive_mode"] != pointer["archive_mode"]
        or note["terminal_proof_kind"] != pointer["terminal_proof_kind"]
        or note["terminal_object_locator"] != pointer["terminal_locator"]
        or note["terminal_object_sha256"] != pointer["terminal_sha256"]
        or note["ledger_terminal_event_locator"]
        != pointer["ledger_terminal_event_locator"]
        or note["ledger_terminal_event_sha256"]
        != pointer["ledger_terminal_event_sha256"]
        or note["legacy_evidence_locator"] != evidence_locator
        or note["raw_archive_relpath"] != str(relative)
        or note["raw_archive_manifest_sha256"]
        != pointer["archive_manifest_sha256"]
        or note["raw_archive_package_sha256"]
        != pointer["archive_package_sha256"]
        or note["canonical_package"] is not False
        or note["conversation_complete"] is not pointer["conversation_complete"]
        or note["resolved_reference_file_count"]
        != len(pointer["source_files"])
        or note["unresolved_reference_count"]
        != len(pointer["unresolved_refs"])
    ):
        raise ArchiveError("legacy archive locator note drifted from pointer")
    receipt_raw = _read_bounded_regular(
        archived_dir / "archive-receipt.json", boundary=archived_dir
    )
    if _sha256(receipt_raw) != pointer["archive_receipt_sha256"]:
        raise ArchiveError("legacy archive receipt hash drifted")
    try:
        receipt = json.loads(receipt_raw.decode("utf-8"))
    except (UnicodeError, json.JSONDecodeError) as exc:
        raise ArchiveError("legacy archive receipt JSON is invalid") from exc
    if (
        not isinstance(receipt, dict)
        or receipt.get("schema_version") != LEGACY_ARCHIVE_RECEIPT_SCHEMA
        or receipt.get("status") != "ARCHIVED_AND_LOCATOR_BOUND"
        or receipt.get("legacy_package_id") != pointer["legacy_package_id"]
        or receipt.get("capture_id") != pointer["capture_id"]
        or receipt.get("archive_mode") != pointer["archive_mode"]
        or receipt.get("terminal_proof_kind")
        != pointer["terminal_proof_kind"]
        or receipt.get("stable_evidence_refs")
        != pointer["stable_evidence_refs"]
        or receipt.get("unresolved_refs") != pointer["unresolved_refs"]
        or receipt.get("ledger_terminal_event_locator")
        != pointer["ledger_terminal_event_locator"]
        or receipt.get("ledger_terminal_event_sha256")
        != pointer["ledger_terminal_event_sha256"]
        or receipt.get("evidence_locator") != evidence_locator
        or receipt.get("archive_manifest_sha256")
        != pointer["archive_manifest_sha256"]
        or receipt.get("archive_package_sha256")
        != pointer["archive_package_sha256"]
        or receipt.get("locator_note_sha256") != pointer["locator_note_sha256"]
        or receipt.get("authorization") != pointer["authorization"]
        or receipt.get("cleanup_count")
        != (
            0
            if pointer["archive_mode"] == "ledger_only_reference_files"
            else None
        )
        or receipt.get("canonical_package") is not False
        or receipt.get("conversation_complete")
        is not pointer["conversation_complete"]
    ):
        raise ArchiveError("legacy archive receipt binding is invalid")
    return {
        "status": "verified",
        "pointer": pointer,
        "pointer_sha256": _sha256(pointer_raw),
        "manifest": manifest,
        "receipt": receipt,
        "locator": note,
        "archive_dir": str(archived_dir),
        "sentinel": sentinel,
    }


def verify_legacy_archive_for_current_capture(
    *,
    repo: str | Path,
    capture_id: str,
    evidence_locator: str,
    evidence_root: str | Path,
    decision_root: str | Path | None = None,
    archive_root: str | Path = ARCHIVE_ROOT,
) -> dict[str, Any]:
    verified = verify_legacy_archive_pointer(
        repo=repo,
        evidence_locator=evidence_locator,
        evidence_root=evidence_root,
        archive_root=archive_root,
    )
    proof = _legacy_canonical_proof(
        repo=repo, capture_id=capture_id, decision_root=decision_root
    )
    if proof.get("evidence_locator") != evidence_locator:
        raise ArchiveError("planner legacy evidence locator drifted from capture")
    _rebind_pointer_to_current_proof(verified["pointer"], proof)
    return {**verified, "current_canonical_proof": _without_raw(proof["proof_rows"])}


def reopen_legacy_evidence_source_file(
    *,
    evidence_root: str | Path,
    source_relative_path: str,
    expected_sha256: str,
    maximum: int,
    archive_root: str | Path | None = None,
) -> bytes:
    root = _existing_private_root(evidence_root)
    relative = _relative(source_relative_path)
    if relative.parts[0] not in {"manifests", "objects", "attachments"}:
        raise ArchiveError("legacy archived source role is not reopenable")
    if not SHA256_RE.fullmatch(expected_sha256) or relative.stem != expected_sha256:
        raise ArchiveError("legacy archived source path/hash mismatch")
    pointer_dir = root / LEGACY_POINTER_ROOT
    if pointer_dir.is_symlink() or not pointer_dir.is_dir():
        raise ArchiveError("legacy archive pointer registry is missing")
    paths = sorted(pointer_dir.glob("*.json"))
    if len(paths) > MAX_POINTERS:
        raise ArchiveError("legacy archive pointer registry exceeds bounded scan limit")
    candidates: list[dict[str, Any]] = []
    for path in paths:
        try:
            pointer, _raw = _read_pointer(root, path.stem)
        except ArchiveError:
            continue
        for row in pointer.get("source_files") or []:
            if (
                isinstance(row, dict)
                and row.get("source_relative_path") == str(relative)
                and row.get("sha256") == expected_sha256
            ):
                candidates.append(pointer)
    if len(candidates) != 1:
        raise ArchiveError("legacy archived source pointer is missing or ambiguous")
    pointer = candidates[0]
    verified = verify_legacy_archive_pointer(
        repo=str(pointer["repo_root"]),
        evidence_locator=str(pointer["evidence_locator"]),
        evidence_root=root,
        archive_root=(ARCHIVE_ROOT if archive_root is None else archive_root),
    )
    rows = {
        str(row.get("source_relative_path")): row
        for row in verified["pointer"].get("source_files") or []
        if isinstance(row, dict)
    }
    row = rows.get(str(relative))
    if row is None or row.get("sha256") != expected_sha256:
        raise ArchiveError("legacy archived source is not bound by verified pointer")
    target = Path(verified["archive_dir"]) / str(row["relative_path"])
    raw = _read_bounded_regular(
        target,
        maximum=maximum,
        boundary=Path(verified["archive_dir"]),
    )
    if len(raw) != row.get("byte_count") or _sha256(raw) != expected_sha256:
        raise ArchiveError("legacy archived source bytes drifted")
    return raw


def _legacy_cleanup_plan(
    *,
    evidence_root: Path,
    source_rows: list[dict[str, Any]],
    capture_reference_count: int,
) -> tuple[list[str], list[dict[str, str]]]:
    reference_counts: dict[str, int] = {}
    inventory_safe = True
    manifest_dir = evidence_root / "manifests"
    if manifest_dir.is_symlink() or not manifest_dir.is_dir():
        inventory_safe = False
    else:
        manifests = sorted(manifest_dir.glob("*.json"))
        if len(manifests) > MAX_POINTERS:
            inventory_safe = False
        else:
            for path in manifests:
                try:
                    raw = _read_bounded_regular(
                        path,
                        maximum=legacy_evidence.MAX_METADATA_BYTES,
                        boundary=evidence_root,
                        private=True,
                    )
                    if _sha256(raw) != path.stem:
                        raise ArchiveError("manifest content address drifted")
                    value = json.loads(raw.decode("utf-8"))
                    if not isinstance(value, dict):
                        raise ArchiveError("manifest inventory row invalid")
                    object_sha = str(value.get("object_sha256") or "")
                    if not SHA256_RE.fullmatch(object_sha):
                        raise ArchiveError("manifest object SHA invalid")
                    object_relative = f"objects/{object_sha}.json"
                    reference_counts[object_relative] = (
                        reference_counts.get(object_relative, 0) + 1
                    )
                    for row in value.get("attachment_objects") or []:
                        digest = str(row.get("sha256") or "") if isinstance(row, dict) else ""
                        if not SHA256_RE.fullmatch(digest):
                            raise ArchiveError("manifest attachment SHA invalid")
                        relative = f"attachments/{digest}.bin"
                        reference_counts[relative] = reference_counts.get(relative, 0) + 1
                except (
                    ArchiveError,
                    OSError,
                    UnicodeError,
                    json.JSONDecodeError,
                ):
                    inventory_safe = False
                    break
    pointer_dir = evidence_root / LEGACY_POINTER_ROOT
    if pointer_dir.exists():
        if pointer_dir.is_symlink() or not pointer_dir.is_dir():
            inventory_safe = False
        else:
            pointer_paths = sorted(pointer_dir.glob("*.json"))
            if len(pointer_paths) > MAX_POINTERS:
                inventory_safe = False
            else:
                for path in pointer_paths:
                    try:
                        pointer, _raw = _read_pointer(evidence_root, path.stem)
                        live_manifest = (
                            evidence_root
                            / "manifests"
                            / f"{pointer['evidence_sha256']}.json"
                        )
                        if live_manifest.is_file() and not live_manifest.is_symlink():
                            continue
                        for row in pointer.get("source_files") or []:
                            relative = (
                                str(row.get("source_relative_path") or "")
                                if isinstance(row, dict)
                                else ""
                            )
                            if relative.startswith(("objects/", "attachments/")):
                                reference_counts[relative] = (
                                    reference_counts.get(relative, 0) + 1
                                )
                    except ArchiveError:
                        inventory_safe = False
                        break
    candidates: list[str] = []
    retained: list[dict[str, str]] = []
    for row in source_rows:
        relative = str(row["source_relative_path"])
        reason: str | None = None
        if capture_reference_count != 1:
            reason = "evidence_locator_shared_by_multiple_captures"
        elif not inventory_safe:
            reason = "exclusive_ownership_not_provable"
        elif row.get("source_role") == "source_document":
            reason = "source_document_preserved"
        elif relative.startswith("manifests/"):
            candidates.append(relative)
        elif reference_counts.get(relative) == 1:
            candidates.append(relative)
        else:
            reason = "shared_content_addressed_object"
        if reason is not None:
            retained.append({"source_relative_path": relative, "reason": reason})
    return sorted(candidates), sorted(
        retained, key=lambda row: row["source_relative_path"]
    )


def _cleanup_legacy_sources(
    evidence_root: Path,
    pointer: dict[str, Any],
    *,
    repo: str | Path,
    capture_id: str,
    decision_root: str | Path | None,
    fault_injector: FaultInjector | None = None,
) -> dict[str, Any]:
    current_proof = _legacy_canonical_proof(
        repo=repo, capture_id=capture_id, decision_root=decision_root
    )
    _rebind_pointer_to_current_proof(pointer, current_proof)
    if pointer.get("archive_mode") == "ledger_only_reference_files":
        retained = list(pointer.get("retained_files") or [])
        return {
            "removed_file_count": 0,
            "already_missing_file_count": 0,
            "retained_file_count": len(retained),
            "retained_files": retained,
            "ownership_recomputed": True,
            "cleanup_count": 0,
        }
    current_candidates, current_retained = _legacy_cleanup_plan(
        evidence_root=evidence_root,
        source_rows=list(pointer["source_files"]),
        capture_reference_count=int(current_proof["capture_reference_count"]),
    )
    originally_authorized = set(pointer.get("cleanup_candidates") or [])
    currently_exclusive = set(current_candidates)
    effective_candidates = sorted(originally_authorized & currently_exclusive)
    ownership_changed = sorted(originally_authorized - currently_exclusive)
    dynamic_retained = list(current_retained)
    dynamic_retained.extend(
        {
            "source_relative_path": relative,
            "reason": "exclusive_ownership_changed_before_cleanup",
        }
        for relative in ownership_changed
        if relative
        not in {
            str(row.get("source_relative_path") or "")
            for row in dynamic_retained
            if isinstance(row, dict)
        }
    )
    rows = {
        str(row.get("source_relative_path")): row
        for row in pointer.get("source_files") or []
        if isinstance(row, dict)
    }
    removed = 0
    already_missing = 0
    for index, relative_text in enumerate(effective_candidates):
        row = rows.get(str(relative_text))
        if row is None:
            raise ArchiveError("cleanup candidate is absent from pointer inventory")
        relative = _relative(str(relative_text))
        target = evidence_root / relative
        if not target.exists():
            already_missing += 1
            continue
        raw = _read_bounded_regular(
            target,
            maximum=max(MAX_PRIVATE_PROOF_BYTES, legacy_evidence.MAX_ATTACHMENT_BYTES),
            boundary=evidence_root,
            private=True,
        )
        if len(raw) != row.get("byte_count") or _sha256(raw) != row.get("sha256"):
            raise ArchiveError("cleanup candidate drifted; no deletion performed")
        target.unlink()
        removed += 1
        _fault(fault_injector, f"legacy_after_cleanup_{index + 1}")
    return {
        "removed_file_count": removed,
        "already_missing_file_count": already_missing,
        "retained_file_count": len(dynamic_retained),
        "retained_files": sorted(
            dynamic_retained, key=lambda row: row["source_relative_path"]
        ),
        "ownership_recomputed": True,
    }


def _legacy_preview_context(
    *,
    repo: str | Path,
    capture_id: str,
    decision_root: str | Path | None,
    evidence_root: str | Path | None,
    archive_root: str | Path,
    details_root: str | Path | None,
) -> dict[str, Any]:
    proof = _legacy_canonical_proof(
        repo=repo, capture_id=capture_id, decision_root=decision_root
    )
    private = _existing_private_root(evidence_root)
    pointer_path = _pointer_path(private, proof["evidence_sha256"])
    if pointer_path.is_file():
        verified = verify_legacy_archive_pointer(
            repo=repo,
            evidence_locator=proof["evidence_locator"],
            evidence_root=private,
            archive_root=archive_root,
        )
        pointer = verified["pointer"]
        material = pointer["authorization_material"]
        _rebind_pointer_to_current_proof(pointer, proof)
        return {
            "proof": proof,
            "private": private,
            "material": {**material, "authorization": pointer["authorization"]},
            "source_rows": [],
            "existing": verified,
        }
    if proof["archive_mode"] in {
        "current_question_evidence",
        "current_question_evidence_ledger_terminal",
    }:
        manifest, bundle, evidence_kind, source_rows = _legacy_evidence_files(
            evidence_locator=proof["evidence_locator"], evidence_root=private
        )
        conversation_complete, basis = _legacy_conversation_truth(
            manifest, bundle, evidence_kind
        )
        evidence_schema_version = str(
            manifest.get("bundle_schema_version") or ""
        )
        unresolved_refs: list[dict[str, Any]] = []
    else:
        source_rows, unresolved_refs = _ledger_reference_files(
            proof=proof, details_root=details_root
        )
        evidence_kind = "ledger_only_reference_files"
        evidence_schema_version = "ledger-only-reference-files-v1"
        conversation_complete = False
        basis = "no_complete_conversation_evidence_available"
    core = _authorization_material(
        proof,
        evidence_kind=evidence_kind,
        evidence_schema_version=evidence_schema_version,
        conversation_complete=conversation_complete,
        conversation_complete_basis=basis,
        source_rows=source_rows,
        unresolved_refs=unresolved_refs,
    )
    authorization = LEGACY_ARCHIVE_AUTH_PREFIX + _sha256(_json_bytes(core))
    return {
        "proof": proof,
        "private": private,
        "material": {**core, "authorization": authorization},
        "source_rows": source_rows,
        "existing": None,
    }


def archive_legacy_evidence_after_formal_commit(
    *,
    repo: str | Path,
    capture_id: str,
    apply: bool = False,
    authorization: str | None = None,
    decision_root: str | Path | None = None,
    evidence_root: str | Path | None = None,
    archive_root: str | Path = ARCHIVE_ROOT,
    details_root: str | Path | None = None,
    fault_injector: FaultInjector | None = None,
) -> dict[str, Any]:
    """Preview or archive one legacy evidence tree without replaying formal work."""

    try:
        context = _legacy_preview_context(
            repo=repo,
            capture_id=capture_id,
            decision_root=decision_root,
            evidence_root=evidence_root,
            archive_root=archive_root,
            details_root=details_root,
        )
        material = context["material"]
        expected_authorization = str(material["authorization"])
        preview = {
            "schema_version": "cs408-legacy-evidence-archive-preview-v1",
            "status": LEGACY_PREVIEW_STATUS,
            "capture_id": material["capture_id"],
            "study_date": material["study_date"],
            "legacy_package_id": material["legacy_package_id"],
            "evidence_locator": material["evidence_locator"],
            "evidence_sha256": material["evidence_sha256"],
            "evidence_kind": material["evidence_kind"],
            "evidence_schema_version": material["evidence_schema_version"],
            "archive_mode": material["archive_mode"],
            "terminal_proof_kind": material["terminal_proof_kind"],
            "canonical_package": False,
            "conversation_complete": material["conversation_complete"],
            "conversation_complete_basis": material[
                "conversation_complete_basis"
            ],
            "terminal_locator": material["terminal_locator"],
            "terminal_sha256": material["terminal_sha256"],
            "ledger_terminal_event_locator": material[
                "ledger_terminal_event_locator"
            ],
            "ledger_terminal_event_sha256": material[
                "ledger_terminal_event_sha256"
            ],
            "terminal_outcome": material["outcome"],
            "formal_ids": material["formal_ids"],
            "source_file_count": len(material["source_files"]),
            "unresolved_reference_count": len(material["unresolved_refs"]),
            "proof_file_count": len(material["proof_files"]),
            "authorization": expected_authorization,
            "apply_required": True,
            "zero_write_preview": True,
            "archive_only": True,
            "cleanup_count": (
                0
                if material["archive_mode"] == "ledger_only_reference_files"
                else None
            ),
            "formal_apply_allowed": False,
            "formal_write_count": 0,
            "mark_result_count": 0,
            "terminal_binding_count": 0,
        }
        if not apply:
            return preview
        if authorization != expected_authorization:
            raise ArchiveError(
                "--apply requires exact content-derived --authorization from preview"
            )
        if context["existing"] is not None:
            cleanup = _cleanup_legacy_sources(
                context["private"],
                context["existing"]["pointer"],
                repo=repo,
                capture_id=capture_id,
                decision_root=decision_root,
                fault_injector=fault_injector,
            )
            return {
                **context["existing"]["receipt"],
                "archive_receipt_sha256": context["existing"]["pointer"][
                    "archive_receipt_sha256"
                ],
                "pointer_sha256": context["existing"]["pointer_sha256"],
                "local_cleanup": cleanup,
                "idempotent_resume": True,
            }

        sentinel = verify_volume(archive_root)
        private = context["private"]
        proof = context["proof"]
        source_rows = context["source_rows"]
        repo_path = Path(repo).expanduser().resolve(strict=True)
        intent_path = private / LEGACY_INTENT_ROOT / f"{material['evidence_sha256']}.json"
        if intent_path.exists() or intent_path.is_symlink():
            intent_raw = _read_bounded_regular(
                intent_path,
                maximum=MAX_PRIVATE_PROOF_BYTES,
                boundary=private,
                private=True,
            )
            intent = json.loads(intent_raw.decode("utf-8"))
        else:
            intent = {
                "schema_version": LEGACY_INTENT_SCHEMA,
                "authorization": expected_authorization,
                "capture_id": material["capture_id"],
                "evidence_locator": material["evidence_locator"],
                "evidence_sha256": material["evidence_sha256"],
                "terminal_sha256": material["terminal_sha256"],
                "legacy_package_id": material["legacy_package_id"],
                "archive_verified_at": dt.datetime.now(dt.timezone.utc).isoformat(
                    timespec="seconds"
                ),
                "formal_apply_allowed": False,
                "formal_write_count": 0,
                "mark_result_count": 0,
                "terminal_binding_count": 0,
            }
            _safe_atomic_write(
                private, intent_path, _json_bytes(intent), private=True
            )
        if (
            intent.get("schema_version") != LEGACY_INTENT_SCHEMA
            or intent.get("authorization") != expected_authorization
            or intent.get("capture_id") != material["capture_id"]
            or intent.get("evidence_sha256") != material["evidence_sha256"]
            or intent.get("terminal_sha256") != material["terminal_sha256"]
            or intent.get("legacy_package_id") != material["legacy_package_id"]
        ):
            raise ArchiveError("legacy archive intent drifted")
        _fault(fault_injector, "legacy_after_intent")

        archive_base = _authorized_root(Path(archive_root))
        relative = (
            SUBJECT_RELATIVE_ROOT
            / material["study_date"]
            / material["legacy_package_id"]
        )
        final = archive_base / relative
        stage = final.parent / (
            f".{material['legacy_package_id']}.stage-"
            f"{expected_authorization[-12:]}"
        )
        manifest = _legacy_manifest_document(
            material, str(intent["archive_verified_at"])
        )
        manifest_raw = _json_bytes(manifest)
        if final.exists() or final.is_symlink():
            _verify_legacy_package(
                final,
                archive_root=archive_base,
                expected_manifest_raw=manifest_raw,
                receipt_mode="optional",
            )
        else:
            _write_legacy_stage(
                stage,
                archive_root=archive_base,
                manifest_raw=manifest_raw,
                source_rows=source_rows,
                proof_rows=proof["proof_rows"],
            )
            _verify_legacy_package(
                stage,
                archive_root=archive_base,
                expected_manifest_raw=manifest_raw,
                receipt_mode="forbidden",
            )
            _safe_replace_directory(archive_base, stage, final)
            _verify_legacy_package(
                final,
                archive_root=archive_base,
                expected_manifest_raw=manifest_raw,
                receipt_mode="forbidden",
            )
        _fault(fault_injector, "legacy_after_atomic_final")

        manifest_sha256 = _sha256(manifest_raw)
        note_data = {
            "package_id": material["legacy_package_id"],
            "capture_id": material["capture_id"],
            "archive_mode": material["archive_mode"],
            "terminal_proof_kind": material["terminal_proof_kind"],
            "terminal_object_locator": material["terminal_locator"],
            "terminal_object_sha256": material["terminal_sha256"],
            "ledger_terminal_event_locator": material[
                "ledger_terminal_event_locator"
            ],
            "ledger_terminal_event_sha256": material[
                "ledger_terminal_event_sha256"
            ],
            "legacy_evidence_locator": material["evidence_locator"],
            "conversation_complete": material["conversation_complete"],
            "conversation_complete_basis": material[
                "conversation_complete_basis"
            ],
            "terminal_outcome": material["outcome"],
            "resolved_reference_file_count": len(material["source_files"]),
            "unresolved_reference_count": len(material["unresolved_refs"]),
            "formal_ids": material["formal_ids"],
            "retrieval_keys": [
                f"package:{material['legacy_package_id']}",
                f"capture:{material['capture_id']}",
                *(f"formal:{formal_id}" for formal_id in material["formal_ids"]),
            ],
            "raw_archive_relpath": str(relative),
            "raw_archive_manifest_sha256": manifest_sha256,
            "raw_archive_package_sha256": manifest["package_content_sha256"],
            "archive_verified_at": intent["archive_verified_at"],
        }
        note_path = (
            repo_path / LOCATOR_NOTE_ROOT / f"{material['legacy_package_id']}.md"
        )
        note_text = _legacy_locator_text(note_data)
        if note_path.exists() or note_path.is_symlink():
            existing_note = _read_bounded_regular(note_path, boundary=repo_path)
            if existing_note != note_text.encode("utf-8"):
                raise ArchiveError("legacy locator note conflicts with existing bytes")
        _safe_atomic_write(repo_path, note_path, note_text.encode("utf-8"))
        note = _read_legacy_locator_note(note_path, repo_root=repo_path)
        note_sha256 = note["locator_note_sha256"]
        _fault(fault_injector, "legacy_after_locator_note")

        receipt = {
            "schema_version": LEGACY_ARCHIVE_RECEIPT_SCHEMA,
            "status": "ARCHIVED_AND_LOCATOR_BOUND",
            "archive_package_type": LEGACY_ARCHIVE_TYPE,
            "legacy_package_id": material["legacy_package_id"],
            "capture_id": material["capture_id"],
            "study_date": material["study_date"],
            "archive_mode": material["archive_mode"],
            "terminal_proof_kind": material["terminal_proof_kind"],
            "stable_evidence_refs": material["stable_evidence_refs"],
            "unresolved_refs": material["unresolved_refs"],
            "evidence_locator": material["evidence_locator"],
            "evidence_sha256": material["evidence_sha256"],
            "evidence_kind": material["evidence_kind"],
            "evidence_schema_version": material["evidence_schema_version"],
            "canonical_package": False,
            "conversation_complete": material["conversation_complete"],
            "conversation_complete_basis": material[
                "conversation_complete_basis"
            ],
            "terminal_locator": material["terminal_locator"],
            "terminal_sha256": material["terminal_sha256"],
            "ledger_terminal_event_locator": material[
                "ledger_terminal_event_locator"
            ],
            "ledger_terminal_event_sha256": material[
                "ledger_terminal_event_sha256"
            ],
            "terminal_outcome": material["outcome"],
            "formal_ids": material["formal_ids"],
            "authorization": expected_authorization,
            "archive_volume": VOLUME_NAME,
            "archive_relative_path": str(relative),
            "archive_manifest_sha256": manifest_sha256,
            "archive_package_sha256": manifest["package_content_sha256"],
            "locator_note_relative_path": str(
                LOCATOR_NOTE_ROOT / f"{material['legacy_package_id']}.md"
            ),
            "locator_note_sha256": note_sha256,
            "source_file_inventory_sha256": _sha256(
                _json_bytes(material["source_files"])
            ),
            "proof_file_inventory_sha256": _sha256(
                _json_bytes(material["proof_files"])
            ),
            "sentinel": sentinel,
            "archive_verified_at": intent["archive_verified_at"],
            "archive_only": True,
            "cleanup_count": (
                0
                if material["archive_mode"] == "ledger_only_reference_files"
                else None
            ),
            "formal_apply_allowed": False,
            "formal_write_count": 0,
            "mark_result_count": 0,
            "terminal_binding_count": 0,
        }
        receipt_raw = _json_bytes(receipt)
        receipt_path = final / "archive-receipt.json"
        if receipt_path.exists() or receipt_path.is_symlink():
            existing_receipt = _read_bounded_regular(
                receipt_path, boundary=final
            )
            if existing_receipt != receipt_raw:
                raise ArchiveError("legacy archive receipt conflict")
        _safe_atomic_write(archive_base, receipt_path, receipt_raw)
        if _read_bounded_regular(receipt_path, boundary=final) != receipt_raw:
            raise ArchiveError("legacy archive receipt reread failed")
        _verify_legacy_package(
            final,
            archive_root=archive_base,
            expected_manifest_raw=manifest_raw,
            receipt_mode="required",
        )
        receipt_sha256 = _sha256(receipt_raw)
        _fault(fault_injector, "legacy_after_archive_receipt")

        if material["archive_mode"] == "ledger_only_reference_files":
            cleanup_candidates = []
            retained_files = _normalized_retained_files(
                [
                    {
                        "source_relative_path": str(row["source_relative_path"]),
                        "reason": "ledger_reference_source_always_retained",
                    }
                    for row in source_rows
                ]
            )
        else:
            cleanup_candidates, retained_files = _legacy_cleanup_plan(
                evidence_root=private,
                source_rows=source_rows,
                capture_reference_count=int(material["capture_reference_count"]),
            )
        pointer = {
            "schema_version": LEGACY_POINTER_SCHEMA,
            "status": "archive_verified",
            "archive_package_type": LEGACY_ARCHIVE_TYPE,
            "canonical_package": False,
            "conversation_complete": material["conversation_complete"],
            "conversation_complete_basis": material[
                "conversation_complete_basis"
            ],
            "repo_root": str(repo_path),
            "capture_id": material["capture_id"],
            "study_date": material["study_date"],
            "archive_mode": material["archive_mode"],
            "terminal_proof_kind": material["terminal_proof_kind"],
            "stable_evidence_refs": material["stable_evidence_refs"],
            "unresolved_refs": material["unresolved_refs"],
            "evidence_locator": material["evidence_locator"],
            "evidence_sha256": material["evidence_sha256"],
            "evidence_kind": material["evidence_kind"],
            "evidence_schema_version": material["evidence_schema_version"],
            "legacy_package_id": material["legacy_package_id"],
            "outcome": material["outcome"],
            "formal_ids": material["formal_ids"],
            "terminal_locator": material["terminal_locator"],
            "terminal_sha256": material["terminal_sha256"],
            "ledger_terminal_event_locator": material[
                "ledger_terminal_event_locator"
            ],
            "ledger_terminal_event_sha256": material[
                "ledger_terminal_event_sha256"
            ],
            "authorization": expected_authorization,
            "authorization_material": {
                key: value for key, value in material.items() if key != "authorization"
            },
            "archive_volume": VOLUME_NAME,
            "archive_relative_path": str(relative),
            "archive_manifest_sha256": manifest_sha256,
            "archive_package_sha256": manifest["package_content_sha256"],
            "archive_receipt_sha256": receipt_sha256,
            "locator_note_relative_path": receipt["locator_note_relative_path"],
            "locator_note_sha256": note_sha256,
            "source_files": material["source_files"],
            "cleanup_candidates": cleanup_candidates,
            "retained_files": retained_files,
            "cleanup_count": (
                0
                if material["archive_mode"] == "ledger_only_reference_files"
                else len(cleanup_candidates)
            ),
            "formal_apply_allowed": False,
            "formal_write_count": 0,
            "mark_result_count": 0,
            "terminal_binding_count": 0,
        }
        pointer_path = _pointer_path(private, material["evidence_sha256"])
        pointer_raw = _json_bytes(pointer)
        if pointer_path.exists() or pointer_path.is_symlink():
            existing_pointer = _read_bounded_regular(
                pointer_path,
                maximum=MAX_PRIVATE_PROOF_BYTES,
                boundary=private,
                private=True,
            )
            if existing_pointer != pointer_raw:
                raise ArchiveError("legacy archive pointer conflict")
        _safe_atomic_write(private, pointer_path, pointer_raw, private=True)
        reread_pointer, reread_pointer_raw = _read_pointer(
            private, material["evidence_sha256"]
        )
        if reread_pointer != pointer or reread_pointer_raw != pointer_raw:
            raise ArchiveError("legacy archive pointer reread failed")
        _fault(fault_injector, "legacy_after_pointer")
        cleanup = _cleanup_legacy_sources(
            private,
            pointer,
            repo=repo,
            capture_id=capture_id,
            decision_root=decision_root,
            fault_injector=fault_injector,
        )
        return {
            **receipt,
            "archive_receipt_sha256": receipt_sha256,
            "pointer_sha256": _sha256(pointer_raw),
            "local_cleanup": cleanup,
            "idempotent_resume": False,
        }
    except (
        ArchiveError,
        capture_model.CaptureError,
        legacy_evidence.CurrentQuestionEvidenceError,
        formal_document.FormalProblemDocumentError,
        OSError,
        UnicodeError,
        json.JSONDecodeError,
        ValueError,
    ) as exc:
        return {
            "schema_version": LEGACY_ARCHIVE_RECEIPT_SCHEMA,
            "status": PENDING_STATUS,
            "planner_status": LEGACY_ARCHIVE_PENDING_STATUS,
            "reason_code": str(exc),
            "resume_action": "preview_then_resume_legacy_archive_only_do_not_reapply",
            "archive_only": True,
            "formal_apply_allowed": False,
            "formal_write_count": 0,
            "mark_result_count": 0,
            "terminal_binding_count": 0,
        }


def reopen_archived_package(
    *, repo: str | Path, package_id: str, requested_relative_paths: list[str],
    archive_root: str | Path = ARCHIVE_ROOT,
) -> dict[str, Any]:
    """Verify one locator and read only explicitly requested manifest-bound objects."""

    sentinel = verify_volume(archive_root)
    if not package.SAFE_ID_RE.fullmatch(package_id):
        raise ArchiveError("package_id invalid")
    note_path = (
        Path(repo).expanduser().resolve() / LOCATOR_NOTE_ROOT / f"{package_id}.md"
    )
    note = _read_locator_note(note_path)
    if note["package_id"] != package_id:
        raise ArchiveError("locator note package identity drifted")
    relative = _relative(note["raw_archive_relpath"])
    if relative.parts[: len(SUBJECT_RELATIVE_ROOT.parts)] != SUBJECT_RELATIVE_ROOT.parts:
        raise ArchiveError("raw archive path is outside the fixed 408 root")
    archive_base = Path(archive_root).resolve()
    package_dir = (archive_base / relative).resolve()
    try:
        package_dir.relative_to(archive_base / SUBJECT_RELATIVE_ROOT)
    except ValueError as exc:
        raise ArchiveError("raw archive path escaped the fixed 408 root") from exc
    manifest_path = package_dir / "manifest.json"
    manifest_raw = manifest_path.read_bytes()
    if _sha256(manifest_raw) != note["raw_archive_manifest_sha256"]:
        raise ArchiveError("raw archive manifest file SHA drifted")
    manifest = json.loads(manifest_raw.decode("utf-8"))
    if (
        not isinstance(manifest, dict)
        or manifest.get("package_id") != package_id
        or manifest.get("canonical_sha256") != note["raw_archive_package_sha256"]
        or manifest.get("sealed") is not True
    ):
        raise ArchiveError("raw archive manifest package binding invalid")
    manifest_rows: dict[str, dict[str, Any]] = {}
    for row in manifest.get("files") or []:
        if not isinstance(row, dict):
            raise ArchiveError("raw archive manifest row invalid")
        rel = _relative(str(row.get("relative_path") or ""))
        target = (package_dir / rel).resolve()
        try:
            target.relative_to(package_dir)
        except ValueError as exc:
            raise ArchiveError("raw archive manifest row escaped package") from exc
        if target.is_symlink() or not target.is_file():
            raise ArchiveError("raw archive manifest object missing")
        raw = target.read_bytes()
        if len(raw) != row.get("byte_count") or _sha256(raw) != row.get("sha256"):
            raise ArchiveError("raw archive manifest object SHA drifted")
        manifest_rows[str(rel)] = row
    requested: list[dict[str, Any]] = []
    for raw_relative in requested_relative_paths:
        rel = _relative(raw_relative)
        relative_text = str(rel)
        if relative_text not in manifest_rows:
            raise ArchiveError("requested object is not bound by the package manifest")
        if relative_text not in {"conversation.json", "source.json"} and not relative_text.startswith("attachments/"):
            raise ArchiveError("requested object role is not allowed for precise reopen")
        target = package_dir / rel
        row = manifest_rows[relative_text]
        item: dict[str, Any] = {
            "relative_path": relative_text,
            "role": row.get("role"),
            "mime_type": row.get("mime_type"),
            "byte_count": row.get("byte_count"),
            "sha256": row.get("sha256"),
        }
        if relative_text in {"conversation.json", "source.json"}:
            value = json.loads(target.read_text(encoding="utf-8"))
            item["json"] = value
        else:
            item["verified_absolute_path"] = str(target)
        requested.append(item)
    return {
        "schema_version": "cs408-raw-archive-reopen-receipt-v1",
        "status": "verified",
        "package_id": package_id,
        "package_sha256": note["raw_archive_package_sha256"],
        "manifest_sha256": note["raw_archive_manifest_sha256"],
        "locator_note_relative_path": str(LOCATOR_NOTE_ROOT / f"{package_id}.md"),
        "requested_objects": requested,
        "sentinel": sentinel,
        "formal_write_count": 0,
    }


def resolve_locator_note(*, repo: str | Path, retrieval_key: str) -> dict[str, Any]:
    """Resolve one exact Obsidian locator note without touching or scanning T9."""

    repo_path = Path(repo).expanduser().resolve()
    key = str(retrieval_key or "").strip()
    if not key or len(key) > 512 or "\n" in key:
        raise ArchiveError("retrieval key is invalid")
    if FORMAL_ID_RE.fullmatch(key):
        key = f"formal:{key}"
    root = repo_path / LOCATOR_NOTE_ROOT
    matches: list[dict[str, Any]] = []
    paths = sorted(root.glob("*.md"))
    if len(paths) > MAX_POINTERS:
        raise ArchiveError("locator note inventory exceeds bounded limit")
    for path in paths:
        try:
            note = _read_locator_note(path)
            keys = set(note.get("retrieval_keys") or [])
            keys.add(f"package:{note['package_id']}")
            keys.update(f"formal:{item}" for item in note.get("formal_ids") or [])
            canonical = True
        except (ArchiveError, OSError, UnicodeError):
            try:
                note = _read_legacy_locator_note(path, repo_root=repo_path)
            except (ArchiveError, OSError, UnicodeError):
                continue
            keys = {
                f"package:{note['package_id']}",
                f"capture:{note['capture_id']}",
                *(f"formal:{item}" for item in note.get("formal_ids") or []),
            }
            canonical = False
        if key in keys:
            matches.append(
                {
                    "package_id": note["package_id"],
                    "locator_note_relative_path": path.relative_to(repo_path).as_posix(),
                    "locator_note_sha256": _sha256(path.read_bytes()),
                    "canonical_package": canonical,
                    "formal_ids": note.get("formal_ids") or [],
                    "raw_archive_relpath": note["raw_archive_relpath"],
                }
            )
    if len(matches) != 1:
        raise ArchiveError(
            "retrieval key must resolve to exactly one subject locator note"
        )
    return {
        "schema_version": "cs408-raw-archive-locator-resolution-v1",
        "status": "resolved",
        "retrieval_key": key,
        **matches[0],
        "t9_scan_count": 0,
        "formal_write_count": 0,
    }


def _cleanup_local_heavy(
    package_dir: Path, fault_injector: FaultInjector | None = None
) -> None:
    for name in ("attachments", ".revisions"):
        target = package_dir / name
        if target.is_dir():
            shutil.rmtree(target)
        _fault(fault_injector, f"after_cleanup_{name.lstrip('.').replace('-', '_')}")
    for name in ("conversation.json", "source.json"):
        target = package_dir / name
        if target.is_file():
            target.unlink()
        _fault(fault_injector, f"after_cleanup_{name.removesuffix('.json')}")


def _synthetic_no_display_binding(
    *, package_dir: Path, manifest: dict[str, Any], reason: str
) -> dict[str, Any]:
    value = {
        "schema_version": display_assets.CLOSURE_SCHEMA,
        "subject": "cs408",
        "package_id": manifest.get("package_id"),
        "package_sha256": manifest.get("canonical_sha256"),
        "formal_id": None,
        "stable_asset_count": 0,
        "no_display": True,
        "no_display_proof": reason,
        "embed_resolution": "not_applicable",
        "obsidian_reread": "not_applicable",
    }
    value["receipt_id"] = "CS408-DISPLAY-" + _sha256(
        json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    )[:20]
    raw = _json_bytes(value)
    path = package_dir / "display-asset-closure-receipt.json"
    if path.exists() and path.read_bytes() != raw:
        raise ArchiveError("synthetic no-display closure receipt drifted")
    if not path.exists():
        _atomic_write(path, raw)
    return {
        "receipt_id": value["receipt_id"],
        "receipt_relative_path": "display-asset-closure-receipt.json",
        "receipt_sha256": _sha256(raw),
        "stable_asset_count": 0,
        "no_display": True,
    }


def _already_current_existing_display_binding(
    *, package_dir: Path, manifest: dict[str, Any], details_root: Path,
    formal_id: str,
) -> dict[str, Any]:
    """Bind an already-current package to identical, already-embedded assets."""

    card = details_root / "cards" / f"{formal_id}.md"
    if not card.is_file() or card.is_symlink():
        raise ArchiveError("already-current display card is missing")
    card_raw = card.read_bytes()
    card_text = card_raw.decode("utf-8")
    asset_root = details_root / "assets" / formal_id
    candidates = [path for path in sorted(asset_root.rglob("*")) if path.is_file()]
    if len(candidates) > 1024:
        raise ArchiveError("already-current display asset inventory is unbounded")
    items: list[dict[str, Any]] = []
    image_rows = [
        row for row in manifest.get("attachments") or []
        if isinstance(row, dict)
        and str(row.get("role") or "") in display_assets.IMAGE_ROLES
    ]
    for row in image_rows:
        digest = str(row.get("sha256") or "")
        byte_count = int(row.get("byte_count") or 0)
        embedded_matches: list[Path] = []
        for path in candidates:
            if path.stat().st_size != byte_count or _sha256(path.read_bytes()) != digest:
                continue
            relative = path.relative_to(details_root).as_posix()
            if f"![[{relative}]]" in card_text:
                embedded_matches.append(path)
        if not embedded_matches:
            # A cross-source redo can legitimately carry an image that is not
            # the formal node's primary display asset. Preserve it in T9 as
            # archive-only evidence instead of promoting it onto the card.
            continue
        chosen = embedded_matches[0]
        items.append(
            {
                "source_sha256": digest,
                "stable_vault_relative_path": chosen.relative_to(details_root).as_posix(),
                "stable_sha256": digest,
            }
        )
    value = {
        "schema_version": display_assets.CLOSURE_SCHEMA,
        "subject": "cs408",
        "package_id": manifest.get("package_id"),
        "package_sha256": manifest.get("canonical_sha256"),
        "formal_id": formal_id,
        "stable_asset_count": len(items),
        "no_display": not items,
        "existing_display_proof": (
            "already_current_hash_identical_embedded_assets"
            if items
            else "already_current_no_promoted_package_assets"
        ),
        "markdown_vault_relative_path": card.relative_to(details_root).as_posix(),
        "markdown_sha256": _sha256(card_raw),
        "items": items,
        "embed_resolution": "verified_existing",
        "obsidian_reread": "verified_existing",
    }
    value["receipt_id"] = "CS408-DISPLAY-" + _sha256(
        json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    )[:20]
    raw = _json_bytes(value)
    path = package_dir / "display-asset-closure-receipt.json"
    if path.exists() and path.read_bytes() != raw:
        raise ArchiveError("already-current display closure receipt drifted")
    if not path.exists():
        _atomic_write(path, raw)
    return {
        "receipt_id": value["receipt_id"],
        "receipt_relative_path": "display-asset-closure-receipt.json",
        "receipt_sha256": _sha256(raw),
        "stable_asset_count": len(items),
        "no_display": not items,
        "items": items,
        "existing_display_proof": value["existing_display_proof"],
    }


def _frozen_companion_display_binding(
    *, repo: Path, details_root: Path, package_dir: Path,
    manifest: dict[str, Any], formal_id: str, decision: dict[str, Any],
    plan: dict[str, Any], plan_sha: str,
) -> dict[str, Any]:
    """An earlier frozen package is raw evidence; the chosen package owns display."""
    events, _ = _ledger_rows(repo)
    state = capture_model.replay(events)
    cid = str(decision.get("capture_id") or "")
    capture = state.get("captures", {}).get(cid) or {}
    batch = state.get("batches", {}).get(decision.get("batch_id")) or {}
    refs = list((capture.get("capture") or {}).get("stable_evidence_refs") or [])
    refs.extend((batch.get("supplement_evidence") or {}).get(cid, []))
    bound = {(row.get("locator"), row.get("sha256")) for row in refs}
    locator = "cs408-conversation-package://" + str(manifest.get("package_id") or "")
    primary = (plan.get("package_locator"), plan.get("package_sha256"))
    if (cid not in (batch.get("capture_ids") or [])
            or (locator, manifest.get("canonical_sha256")) not in bound
            or primary not in bound
            or primary != (decision.get("evidence_locator"),
                           decision.get("reopened_evidence_sha256"))
            or plan.get("package_locator") != "cs408-conversation-package://" + str(plan.get("package_id") or "")
            or plan.get("formal_id") != formal_id):
        raise ArchiveError("non-primary archive package is not bound to the same frozen capture")
    closure = display_assets.verify_closure_receipt(
        details_root=details_root, package_id=plan["package_id"],
        package_sha256=plan["package_sha256"], formal_id=formal_id,
        display_asset_plan_sha256=plan_sha,
    )
    return _synthetic_no_display_binding(
        package_dir=package_dir, manifest=manifest,
        reason=("frozen_capture_companion:" + cid + ":" + plan["package_id"]
                + ":" + closure["receipt_sha256"]),
    )


def _display_cleanup_binding(
    *,
    repo: Path,
    details_root: Path,
    package_dir: Path,
    manifest: dict[str, Any],
    formal_ids: list[str],
    decision: dict[str, Any],
) -> dict[str, Any]:
    scan = display_assets.formal_reference_scan(
        repo=repo, details_root=details_root
    )
    if scan["status"] != "pass":
        raise ArchiveError(
            "formal/details Markdown still references temporary package or T9 image paths"
        )
    normalized = decision.get("decision") or {}
    canonical = normalized.get("canonical_package") or {}
    plan = canonical.get("display_asset_plan")
    plan_sha = decision.get("display_asset_plan_sha256")
    image_count = sum(
        1
        for row in manifest.get("attachments") or []
        if isinstance(row, dict) and str(row.get("role") or "") in display_assets.IMAGE_ROLES
    )
    if formal_ids and isinstance(plan, dict):
        if len(formal_ids) != 1 or not isinstance(plan_sha, str):
            raise ArchiveError("display closure formal identity is ambiguous")
        try:
            if plan.get("package_id") != manifest.get("package_id"):
                binding = _frozen_companion_display_binding(
                    repo=repo, details_root=details_root, package_dir=package_dir,
                    manifest=manifest, formal_id=formal_ids[0], decision=decision,
                    plan=plan, plan_sha=plan_sha,
                )
                return {
                    **binding,
                    "formal_reference_scan_sha256": scan["scan_sha256"],
                    "formal_reference_scan_finding_count": scan["finding_count"],
                }
            closure = display_assets.verify_closure_receipt(
                details_root=details_root,
                package_id=str(manifest.get("package_id") or ""),
                package_sha256=str(manifest.get("canonical_sha256") or ""),
                formal_id=formal_ids[0],
                display_asset_plan_sha256=plan_sha,
            )
        except (display_assets.DisplayAssetError, OSError, UnicodeError) as exc:
            raise ArchiveError(f"display asset closure verification failed: {exc}") from exc
        binding = closure
        binding["receipt_relative_path"] = "details:" + str(
            closure["receipt_relative_path"]
        )
    elif formal_ids and image_count:
        if normalized.get("decision") in {"already_current", "correct_review"} and len(formal_ids) == 1:
            binding = _already_current_existing_display_binding(
                package_dir=package_dir,
                manifest=manifest,
                details_root=details_root,
                formal_id=formal_ids[0],
            )
        else:
            raise ArchiveError(
                "sealed package has image attachments but no verified display asset closure"
            )
    else:
        reason = (
            "no_formal_surface"
            if not formal_ids
            else "sealed_package_has_no_image_attachments"
        )
        binding = _synthetic_no_display_binding(
            package_dir=package_dir, manifest=manifest, reason=reason
        )
    return {
        **binding,
        "formal_reference_scan_sha256": scan["scan_sha256"],
        "formal_reference_scan_finding_count": scan["finding_count"],
    }


def _fault(injector: FaultInjector | None, phase: str) -> None:
    if injector is not None:
        injector(phase)


def _local_package_dir(locator: str, local_root: str | Path | None) -> Path:
    if not locator.startswith(package.LOCATOR_PREFIX):
        raise ArchiveError("package locator invalid")
    package_id = locator.removeprefix(package.LOCATOR_PREFIX)
    root = (
        Path(local_root).expanduser().resolve()
        if local_root is not None
        else Path(os.environ.get("KAOYAN_408_CONVERSATION_PACKAGE_ROOT", package.DEFAULT_ROOT)).expanduser().resolve()
    )
    matches = list(root.glob(f"????-??-??/{package_id}"))
    if len(matches) != 1:
        raise ArchiveError("local package is missing or ambiguous")
    return matches[0]


def archive_after_formal_commit(
    *, repo: str | Path, package_locator: str, package_sha256: str,
    formal_ids: list[str], formal_terminal_locator: str,
    formal_terminal_sha256: str, local_root: str | Path | None = None,
    archive_root: str | Path = ARCHIVE_ROOT,
    decision_root: str | Path | None = None,
    details_root: str | Path = DEFAULT_DETAILS_ROOT,
    fault_injector: FaultInjector | None = None,
) -> dict[str, Any]:
    """Resume-safe archive owned by a verified Sol terminal object."""

    pending_component = "archive"
    try:
        sentinel = verify_volume(archive_root)
        terminal = sol_decision.read_terminal_binding(
            formal_terminal_locator,
            formal_terminal_sha256,
            decision_root=decision_root,
            repo=repo,
        )
        outcome = str(terminal.get("outcome") or "")
        if outcome in {"needs_user", "failed"}:
            return {
                "schema_version": ARCHIVE_RECEIPT_SCHEMA,
                "status": INCOMPLETE_STATUS,
                "terminal_outcome": outcome,
                "local_package_retained": True,
                "formal_apply_allowed": False,
                "formal_write_count": 0,
            }
        if outcome not in ARCHIVABLE_OUTCOMES:
            raise ArchiveError("verified terminal outcome is not archive-eligible")
        decision_locator = str(terminal.get("decision_locator") or "")
        decision_sha256 = str(terminal.get("decision_sha256") or "")
        if decision_locator or decision_sha256:
            decision = sol_decision.read_sealed_decision(
                decision_locator,
                decision_sha256,
                decision_root=decision_root,
            )
        else:
            decision = {"decision": {}, "display_asset_plan_sha256": None}
        if not isinstance(formal_ids, list) or not all(
            isinstance(item, str) and item for item in formal_ids
        ):
            raise ArchiveError("formal_ids must be an explicit list")
        terminal_formal_id = terminal.get("formal_id")
        expected_formal_ids = [terminal_formal_id] if terminal_formal_id else []
        if formal_ids != expected_formal_ids:
            raise ArchiveError("formal_ids do not exactly match verified terminal proof")

        local = _local_package_dir(package_locator, local_root)
        manifest = json.loads((local / "manifest.json").read_text(encoding="utf-8"))
        if (
            not isinstance(manifest, dict)
            or manifest.get("canonical_sha256") != package_sha256
            or manifest.get("sealed") is not True
        ):
            raise ArchiveError("only the exact sealed package may be archived")
        pending_component = "display_assets"
        display_binding = _display_cleanup_binding(
            repo=Path(repo).expanduser().resolve(),
            details_root=Path(details_root).expanduser().resolve(),
            package_dir=local,
            manifest=manifest,
            formal_ids=formal_ids,
            decision=decision,
        )
        pointer_path = local / "archive-pointer.json"
        if pointer_path.is_file():
            pointer = json.loads(pointer_path.read_text(encoding="utf-8"))
            if (
                pointer.get("schema_version") != POINTER_SCHEMA
                or pointer.get("package_sha256") != package_sha256
                or pointer.get("formal_terminal_sha256") != formal_terminal_sha256
                or pointer.get("display_asset_closure_receipt_sha256")
                != display_binding["receipt_sha256"]
                or pointer.get("formal_reference_scan_sha256")
                != display_binding["formal_reference_scan_sha256"]
                or pointer.get("stable_asset_count")
                != display_binding["stable_asset_count"]
                or pointer.get("no_display") is not display_binding["no_display"]
            ):
                raise ArchiveError("local archive pointer drifted")
            archive_base = Path(archive_root).resolve()
            final = archive_base / str(pointer["archive_relative_path"])
            receipt_path = final / "archive-receipt.json"
            receipt_raw = receipt_path.read_bytes()
            if _sha256(receipt_raw) != pointer.get("archive_receipt_sha256"):
                raise ArchiveError("archive receipt drifted during cleanup resume")
            note_path = Path(repo).expanduser().resolve() / str(
                pointer["locator_note_relative_path"]
            )
            if _sha256(note_path.read_bytes()) != pointer.get("locator_note_sha256"):
                raise ArchiveError("locator note drifted during cleanup resume")
            if pointer.get("status") == "cleanup_pending":
                pending_component = "cleanup"
                _cleanup_local_heavy(local)
                pointer = {**pointer, "status": "local_heavy_content_cleaned"}
                _atomic_write(pointer_path, _json_bytes(pointer))
            elif pointer.get("status") != "local_heavy_content_cleaned":
                raise ArchiveError("local archive pointer status invalid")
            receipt = json.loads(receipt_raw.decode("utf-8"))
            return {
                **receipt,
                "archive_receipt_sha256": pointer["archive_receipt_sha256"],
                "local_cleanup": "complete",
                "idempotent_resume": True,
            }

        opened = package.read_package(package_locator, private_root=local_root)
        manifest = opened["manifest"]
        package_id = str(manifest["package_id"])
        archive_base = Path(archive_root).resolve()
        relative = SUBJECT_RELATIVE_ROOT / manifest["study_date"] / package_id
        intent_path = local / "archive-intent.json"
        if intent_path.is_file():
            intent = json.loads(intent_path.read_text(encoding="utf-8"))
        else:
            intent = {
                "schema_version": INTENT_SCHEMA,
                "package_id": package_id,
                "package_sha256": package_sha256,
                "formal_ids": formal_ids,
                "formal_terminal_locator": formal_terminal_locator,
                "formal_terminal_sha256": formal_terminal_sha256,
                "formal_terminal_outcome": outcome,
                "archive_relative_path": str(relative),
                "archive_verified_at": dt.datetime.now(dt.timezone.utc).isoformat(
                    timespec="seconds"
                ),
                "display_asset_closure_receipt_id": display_binding["receipt_id"],
                "display_asset_closure_receipt_relative_path": display_binding[
                    "receipt_relative_path"
                ],
                "display_asset_closure_receipt_sha256": display_binding[
                    "receipt_sha256"
                ],
                "formal_reference_scan_sha256": display_binding[
                    "formal_reference_scan_sha256"
                ],
                "stable_asset_count": display_binding["stable_asset_count"],
                "no_display": display_binding["no_display"],
                "pending_component": None,
                "formal_write_count": 0,
            }
            _atomic_write(intent_path, _json_bytes(intent))
        if (
            intent.get("schema_version") != INTENT_SCHEMA
            or intent.get("package_sha256") != package_sha256
            or intent.get("formal_ids") != formal_ids
            or intent.get("formal_terminal_sha256") != formal_terminal_sha256
            or intent.get("formal_terminal_outcome") != outcome
            or intent.get("archive_relative_path") != str(relative)
            or intent.get("display_asset_closure_receipt_sha256")
            != display_binding["receipt_sha256"]
            or intent.get("formal_reference_scan_sha256")
            != display_binding["formal_reference_scan_sha256"]
        ):
            raise ArchiveError("archive intent drifted")
        _fault(fault_injector, "after_intent")

        pending_component = "archive"
        final = archive_base / relative
        stage = final.parent / f".{package_id}.stage-{package_sha256[:12]}"
        if final.exists():
            rows, manifest_file_sha = _verify_copy(local, final)
        else:
            if stage.exists():
                shutil.rmtree(stage)
            stage.parent.mkdir(parents=True, exist_ok=True)
            shutil.copytree(local, stage, symlinks=False)
            rows, manifest_file_sha = _verify_copy(local, stage)
            os.replace(stage, final)
            rows, manifest_file_sha = _verify_copy(local, final)
        _fault(fault_injector, "after_atomic_final")

        pending_component = "locator"
        note_data = {
            "package_id": package_id,
            "formal_ids": formal_ids,
            "raw_archive_relpath": str(relative),
            "raw_archive_manifest_sha256": manifest_file_sha,
            "raw_archive_package_sha256": package_sha256,
            "archive_verified_at": intent["archive_verified_at"],
            "retrieval_keys": [
                f"package:{package_id}",
                *(f"formal:{formal_id}" for formal_id in formal_ids),
                *(
                    [f"source:{manifest.get('source_id')}"]
                    if manifest.get("source_id")
                    else []
                ),
                *(
                    [f"item:{manifest.get('item_id')}"]
                    if manifest.get("item_id")
                    else []
                ),
                f"study_date:{manifest.get('study_date')}",
            ],
        }
        note_path = (
            Path(repo).expanduser().resolve()
            / LOCATOR_NOTE_ROOT
            / f"{package_id}.md"
        )
        note_text = _frontmatter(note_data)
        if note_path.exists() and note_path.read_text(encoding="utf-8") != note_text:
            raise ArchiveError("locator note already exists with different bytes")
        _atomic_write(note_path, note_text.encode("utf-8"))
        note_sha = _verify_locator_note(note_path, note_data)
        _fault(fault_injector, "after_locator_note")

        pending_component = "archive"
        receipt = {
            "schema_version": ARCHIVE_RECEIPT_SCHEMA,
            "status": "ARCHIVED_AND_LOCATOR_BOUND",
            "package_id": package_id,
            "package_sha256": package_sha256,
            "formal_ids": formal_ids,
            "formal_terminal_locator": formal_terminal_locator,
            "formal_terminal_sha256": formal_terminal_sha256,
            "formal_terminal_outcome": outcome,
            "archive_volume": VOLUME_NAME,
            "archive_relative_path": str(relative),
            "archive_manifest_sha256": manifest_file_sha,
            "archive_file_inventory_sha256": _sha256(_json_bytes(rows)),
            "locator_note_relative_path": str(
                LOCATOR_NOTE_ROOT / f"{package_id}.md"
            ),
            "locator_note_sha256": note_sha,
            "sentinel": sentinel,
            "archive_verified_at": intent["archive_verified_at"],
            "display_asset_closure_receipt_id": display_binding["receipt_id"],
            "display_asset_closure_receipt_relative_path": display_binding[
                "receipt_relative_path"
            ],
            "display_asset_closure_receipt_sha256": display_binding[
                "receipt_sha256"
            ],
            "formal_reference_scan_sha256": display_binding[
                "formal_reference_scan_sha256"
            ],
            "stable_asset_count": display_binding["stable_asset_count"],
            "no_display": display_binding["no_display"],
            "pending_component": None,
            "formal_write_count": 0,
        }
        receipt_raw = _json_bytes(receipt)
        receipt_sha = _sha256(receipt_raw)
        receipt_path = final / "archive-receipt.json"
        if receipt_path.exists() and receipt_path.read_bytes() != receipt_raw:
            raise ArchiveError("archive receipt conflict")
        _atomic_write(receipt_path, receipt_raw)
        _fault(fault_injector, "after_archive_receipt")

        pointer = {
            "schema_version": POINTER_SCHEMA,
            "package_id": package_id,
            "package_sha256": package_sha256,
            "formal_terminal_sha256": formal_terminal_sha256,
            "archive_volume": VOLUME_NAME,
            "archive_relative_path": str(relative),
            "archive_receipt_sha256": receipt_sha,
            "locator_note_relative_path": str(
                LOCATOR_NOTE_ROOT / f"{package_id}.md"
            ),
            "locator_note_sha256": note_sha,
            "display_asset_closure_receipt_id": display_binding["receipt_id"],
            "display_asset_closure_receipt_relative_path": display_binding[
                "receipt_relative_path"
            ],
            "display_asset_closure_receipt_sha256": display_binding[
                "receipt_sha256"
            ],
            "formal_reference_scan_sha256": display_binding[
                "formal_reference_scan_sha256"
            ],
            "stable_asset_count": display_binding["stable_asset_count"],
            "no_display": display_binding["no_display"],
            "pending_component": "cleanup",
            "status": "cleanup_pending",
            "formal_write_count": 0,
        }
        _atomic_write(pointer_path, _json_bytes(pointer))
        _fault(fault_injector, "after_cleanup_intent")
        pending_component = "cleanup"
        _cleanup_local_heavy(local, fault_injector)
        pointer = {
            **pointer,
            "status": "local_heavy_content_cleaned",
            "pending_component": None,
        }
        _atomic_write(pointer_path, _json_bytes(pointer))
        return {
            **receipt,
            "archive_receipt_sha256": receipt_sha,
            "local_cleanup": "complete",
            "idempotent_resume": False,
        }
    except (
        ArchiveError,
        sol_decision.SolCurationDecisionError,
        OSError,
        UnicodeError,
        json.JSONDecodeError,
    ) as exc:
        return {
            "schema_version": ARCHIVE_RECEIPT_SCHEMA,
            "status": PENDING_STATUS,
            "reason_code": str(exc),
            "pending_component": pending_component,
            "resume_action": "resume_archive_only_do_not_reapply",
            "formal_apply_allowed": False,
            "formal_write_count": 0,
        }


def main() -> int:
    parser = argparse.ArgumentParser()
    subparsers = parser.add_subparsers(dest="command", required=True)
    archive = subparsers.add_parser("archive-after-formal-commit")
    archive.add_argument("--repo", required=True)
    archive.add_argument("--package-locator", required=True)
    archive.add_argument("--package-sha256", required=True)
    archive.add_argument("--formal-ids-json", required=True)
    archive.add_argument("--formal-terminal-locator", required=True)
    archive.add_argument("--formal-terminal-sha256", required=True)
    archive.add_argument("--decision-root")
    archive.add_argument("--local-root")
    archive.add_argument("--details-root", default=str(DEFAULT_DETAILS_ROOT))
    legacy_archive = subparsers.add_parser(
        "archive-legacy-evidence-after-formal-commit"
    )
    legacy_archive.add_argument("--repo", required=True)
    legacy_archive.add_argument("--capture-id", required=True)
    legacy_archive.add_argument("--decision-root")
    legacy_archive.add_argument("--evidence-root")
    legacy_archive.add_argument("--apply", action="store_true")
    legacy_archive.add_argument("--authorization")
    reopen = subparsers.add_parser("reopen-verify")
    reopen.add_argument("--repo", required=True)
    reopen.add_argument("--package-id", required=True)
    reopen.add_argument("--read-relative-path", action="append", default=[])
    resolve = subparsers.add_parser("resolve-locator")
    resolve.add_argument("--repo", required=True)
    resolve.add_argument("--retrieval-key", required=True)
    args = parser.parse_args()
    if args.command == "resolve-locator":
        result = resolve_locator_note(
            repo=args.repo,
            retrieval_key=args.retrieval_key,
        )
    elif args.command == "reopen-verify":
        result = reopen_archived_package(
            repo=args.repo,
            package_id=args.package_id,
            requested_relative_paths=args.read_relative_path,
        )
    elif args.command == "archive-legacy-evidence-after-formal-commit":
        result = archive_legacy_evidence_after_formal_commit(
            repo=args.repo,
            capture_id=args.capture_id,
            apply=args.apply,
            authorization=args.authorization,
            decision_root=args.decision_root,
            evidence_root=args.evidence_root,
        )
    else:
        formal_ids = json.loads(args.formal_ids_json)
        result = archive_after_formal_commit(
            repo=args.repo,
            package_locator=args.package_locator,
            package_sha256=args.package_sha256,
            formal_ids=formal_ids,
            formal_terminal_locator=args.formal_terminal_locator,
            formal_terminal_sha256=args.formal_terminal_sha256,
            local_root=args.local_root,
            decision_root=args.decision_root,
            details_root=args.details_root,
        )
    print(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True))
    return 0 if result["status"] != PENDING_STATUS else 2


if __name__ == "__main__":
    raise SystemExit(main())
