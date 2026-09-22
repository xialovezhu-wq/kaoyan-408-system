#!/usr/bin/env python3
"""Read-only verification of formal-intake success evidence for review backflow.

This module is deliberately narrower than the formal writer.  It only reopens
an already committed normal receipt/WAL, or an ``already_current`` verification,
and resolves the real review-unit mapping without guessing ``RU_<formal_id>``.
"""

from __future__ import annotations

import hashlib
import json
import re
import stat
from dataclasses import dataclass
from pathlib import Path
from typing import Any


DEFAULT_RUNTIME_ROOT = Path("~/.codex/kaoyan-408-intake").expanduser()
FORMAL_ID_RE = re.compile(r"^(?:DS|CO|OS|CN)_(?:\d{4}|UNK)_\d{3}$")
REVIEW_UNIT_RE = re.compile(r"^RU_[A-Za-z0-9._-]+$")
SHA256_RE = re.compile(r"^[0-9a-f]{64}$")


class FormalProofError(RuntimeError):
    """The claimed formal result is absent, stale, or bound to another item."""


@dataclass(frozen=True)
class FormalProof:
    kind: str
    sha256: str
    ref: str
    formal_id: str
    review_unit_id: str
    mapping_basis: str
    mapping_sha256: str
    transaction_id: str | None
    capture_binding_basis: str
    material: dict[str, Any]


def _canonical(value: Any) -> bytes:
    return json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")


def _sha_bytes(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


def _sha_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _repo_key(repo: Path) -> str:
    return hashlib.sha256(str(repo).encode("utf-8")).hexdigest()[:20]


def _regular_file(path: Path, label: str) -> Path:
    try:
        info = path.lstat()
    except OSError as exc:
        raise FormalProofError(f"{label} is missing") from exc
    if stat.S_ISLNK(info.st_mode) or not stat.S_ISREG(info.st_mode):
        raise FormalProofError(f"{label} must be a non-symlink regular file")
    try:
        return path.resolve(strict=True)
    except OSError as exc:
        raise FormalProofError(f"{label} is unreadable") from exc


def _json_file(path: Path, label: str) -> tuple[dict[str, Any], bytes]:
    path = _regular_file(path, label)
    try:
        raw = path.read_bytes()
        value = json.loads(raw.decode("utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise FormalProofError(f"{label} is invalid JSON") from exc
    if not isinstance(value, dict):
        raise FormalProofError(f"{label} must be a JSON object")
    return value, raw


def _is_within(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
    except ValueError:
        return False
    return True


def _split_table_row(line: str) -> list[str]:
    return [cell.strip() for cell in line.strip().strip("|").split("|")]


def _review_unit_from_mapping(raw: bytes, formal_id: str) -> str:
    try:
        lines = raw.decode("utf-8").splitlines()
    except UnicodeDecodeError as exc:
        raise FormalProofError("review-unit mapping is not UTF-8") from exc
    header_index = next(
        (
            index
            for index, line in enumerate(lines)
            if line.startswith("|")
            and "正式节点ID" in line
            and "主复习单元ID" in line
        ),
        None,
    )
    if header_index is None:
        raise FormalProofError("review-unit mapping lacks canonical headers")
    headers = _split_table_row(lines[header_index])
    rows: list[dict[str, str]] = []
    for line in lines[header_index + 2 :]:
        if not line.startswith("|"):
            continue
        cells = _split_table_row(line)
        if len(cells) != len(headers):
            continue
        row = dict(zip(headers, cells))
        if row.get("正式节点ID") == formal_id:
            rows.append(row)
    units = {
        str(row.get("主复习单元ID") or "").strip()
        for row in rows
        if REVIEW_UNIT_RE.fullmatch(str(row.get("主复习单元ID") or "").strip())
    }
    if len(rows) != 1 or len(units) != 1:
        raise FormalProofError(
            "formal node does not have one receipt-bound primary review unit"
        )
    return next(iter(units))


def _locate_by_digest(directory: Path, digest: str, label: str) -> Path:
    if not directory.is_dir() or directory.is_symlink():
        raise FormalProofError(f"canonical {label} directory is missing")
    matches: list[Path] = []
    for path in sorted(directory.glob("*.json")):
        if path.is_symlink() or not path.is_file():
            continue
        if _sha_file(path) == digest:
            matches.append(path.resolve(strict=True))
    if len(matches) != 1:
        raise FormalProofError(
            f"expected exactly one canonical {label} for digest, got {len(matches)}"
        )
    return matches[0]


def _resolve_supplied(
    supplied: str | Path | None,
    *,
    directory: Path,
    digest: str,
    label: str,
) -> Path:
    if supplied is None:
        return _locate_by_digest(directory, digest, label)
    path = _regular_file(Path(supplied).expanduser(), label)
    canonical_root = directory.resolve(strict=True)
    if not _is_within(path, canonical_root):
        raise FormalProofError(f"{label} is outside its canonical runtime root")
    if _sha_file(path) != digest:
        raise FormalProofError(f"{label} path/hash mismatch")
    return path


def _validate_identity(
    *, batch_id: str, capture_id: str, formal_id: str, digest: str
) -> None:
    if not batch_id or not capture_id:
        raise FormalProofError("batch_id and capture_id are required")
    if not FORMAL_ID_RE.fullmatch(formal_id):
        raise FormalProofError("formal_id is invalid")
    if not SHA256_RE.fullmatch(digest):
        raise FormalProofError("formal result receipt SHA-256 is invalid")


def _verify_normal_receipt(
    repo: Path,
    runtime: Path,
    *,
    batch_id: str,
    capture_id: str,
    formal_id: str,
    digest: str,
    supplied: str | Path | None,
    historical_result_event: dict[str, Any] | None,
) -> FormalProof:
    repo_key = _repo_key(repo)
    receipt_root = runtime / "receipts" / repo_key
    path = _resolve_supplied(
        supplied,
        directory=receipt_root,
        digest=digest,
        label="normal receipt",
    )
    receipt, raw = _json_file(path, "normal receipt")
    if _sha_bytes(raw) != digest:
        raise FormalProofError("normal receipt bytes changed during verification")
    if (
        receipt.get("schema") != "intake_batch_receipt_v1"
        or receipt.get("status") != "COMMITTED"
    ):
        raise FormalProofError("normal receipt schema/status is invalid")
    if Path(str(receipt.get("repo_root") or "")).expanduser().resolve() != repo:
        raise FormalProofError("normal receipt belongs to another repository")
    recovery = receipt.get("recovery")
    if isinstance(recovery, dict) and recovery.get("action") == "roll_back":
        raise FormalProofError("rolled-back receipt is not commit proof")
    items = receipt.get("items")
    if not isinstance(items, list) or len(items) != 1 or not isinstance(items[0], dict):
        raise FormalProofError("normal receipt must contain exactly one formal item")
    item = items[0]
    if item.get("formal_id") != formal_id or item.get("mode") not in {"new", "redo"}:
        raise FormalProofError("normal receipt formal identity mismatch")

    transaction_id = str(receipt.get("transaction_id") or "")
    transaction_root = runtime / "transactions" / repo_key / transaction_id
    expected_journal = transaction_root / "journal.json"
    journal_ref = Path(str(receipt.get("journal") or "")).expanduser()
    journal_path = _regular_file(journal_ref, "normal WAL")
    if journal_path != expected_journal.resolve(strict=True):
        raise FormalProofError("normal receipt does not bind the canonical WAL")
    journal, _ = _json_file(journal_path, "normal WAL")
    if journal.get("schema") != "intake_batch_wal_v1" or journal.get("state") != "committed":
        raise FormalProofError("normal WAL schema/state is invalid")
    for field in ("transaction_id", "idempotency_key", "payload_sha256", "repo_root"):
        if journal.get(field) != receipt.get(field):
            raise FormalProofError(f"normal receipt/WAL {field} mismatch")
    if journal.get("result_items") != items:
        raise FormalProofError("normal receipt/WAL result_items mismatch")
    payload_identity = journal.get("payload_identity")
    if not isinstance(payload_identity, dict):
        raise FormalProofError("normal WAL lacks payload_identity")
    if _sha_bytes(_canonical(payload_identity)) != journal.get("payload_sha256"):
        raise FormalProofError("normal WAL payload identity hash mismatch")
    packages = payload_identity.get("packages")
    if not isinstance(packages, list) or len(packages) != 1 or not isinstance(packages[0], dict):
        raise FormalProofError("normal WAL must freeze exactly one package")
    package = packages[0]
    package_mode = package.get("mode", "new")
    package_formal_id = package.get("formal_id")
    locked_auto_id = (
        package_mode == "new"
        and item.get("mode") == "new"
        and package.get("contract") == "coordinator_approved_intake_v1"
        and package.get("schema_version") == "coordinator_approved_intake_v1"
        and package.get("auto_id") is True
        and package_formal_id in (None, "")
        and formal_id.startswith(
            f"{str(package.get('subject') or '')}_{str(package.get('year') or '')}_"
        )
    )
    if package_mode != item.get("mode") or (
        package_formal_id != formal_id and not locked_auto_id
    ):
        raise FormalProofError("normal WAL package identity mismatch")
    capture_binding = package.get("capture_binding")
    package_bound = isinstance(capture_binding, dict) and (
        capture_binding.get("batch_id") == batch_id
        and capture_binding.get("capture_id") == capture_id
    )
    if package_bound:
        capture_binding_basis = "normal_wal_package"
    else:
        historical = historical_result_event
        if not isinstance(historical, dict) or (
            historical.get("schema") != "intake_fact_capture_event_v1"
            or historical.get("event_type") != "curation_item_result"
            or historical.get("batch_id") != batch_id
            or historical.get("capture_id") != capture_id
            or historical.get("outcome") != "curated"
            or historical.get("formal_id") != formal_id
            or historical.get("receipt_sha256") != digest
        ):
            if not isinstance(capture_binding, dict):
                raise FormalProofError("normal WAL package lacks capture binding")
            raise FormalProofError("normal WAL package capture binding mismatch")
        capture_binding_basis = "historical_curation_result_event"

    mapping_entries = [
        entry
        for entry in journal.get("files") or []
        if isinstance(entry, dict)
        and Path(str(entry.get("target") or "")).name == "复习单元节点映射.md"
        and entry.get("published") is True
    ]
    if len(mapping_entries) != 1:
        raise FormalProofError("normal WAL lacks one published review-unit mapping")
    mapping_entry = mapping_entries[0]
    stage_ref = Path(str(mapping_entry.get("stage") or ""))
    if stage_ref.is_absolute() or ".." in stage_ref.parts:
        raise FormalProofError("normal WAL mapping stage locator is unsafe")
    stage_path = _regular_file(transaction_root / stage_ref, "mapping stage")
    if not _is_within(stage_path, transaction_root.resolve(strict=True)):
        raise FormalProofError("normal WAL mapping stage escaped its transaction")
    stage_raw = stage_path.read_bytes()
    stage_sha = _sha_bytes(stage_raw)
    if stage_sha != mapping_entry.get("after_sha256"):
        raise FormalProofError("normal WAL mapping stage hash mismatch")
    review_unit_id = _review_unit_from_mapping(stage_raw, formal_id)

    current_mapping = _regular_file(repo / "复习单元节点映射.md", "current mapping")
    current_raw = current_mapping.read_bytes()
    current_unit = _review_unit_from_mapping(current_raw, formal_id)
    if current_unit != review_unit_id:
        raise FormalProofError("receipt-bound and current review-unit mappings conflict")
    return FormalProof(
        kind="normal_receipt",
        sha256=digest,
        ref=path.as_posix(),
        formal_id=formal_id,
        review_unit_id=review_unit_id,
        mapping_basis="committed_wal_stage",
        mapping_sha256=stage_sha,
        transaction_id=transaction_id,
        capture_binding_basis=capture_binding_basis,
        material=receipt,
    )


def _verify_already_current(
    repo: Path,
    runtime: Path,
    *,
    batch_id: str,
    capture_id: str,
    formal_id: str,
    digest: str,
    supplied: str | Path | None,
) -> FormalProof:
    verification_root = runtime / "verification-receipts" / _repo_key(repo)
    path = _resolve_supplied(
        supplied,
        directory=verification_root,
        digest=digest,
        label="already_current verification",
    )
    value, raw = _json_file(path, "already_current verification")
    if _sha_bytes(raw) != digest:
        raise FormalProofError("already_current verification changed during read")
    if (
        value.get("schema") != "already_current_verification_v1"
        or value.get("status") != "VERIFIED_CURRENT"
        or value.get("kind") != "already_current_verification"
        or value.get("formal_write_count") != 0
    ):
        raise FormalProofError("already_current verification contract is invalid")
    if (
        value.get("batch_id") != batch_id
        or value.get("capture_id") != capture_id
        or value.get("formal_id") != formal_id
    ):
        raise FormalProofError("already_current verification identity mismatch")
    mapping = _regular_file(repo / "复习单元节点映射.md", "current mapping")
    mapping_raw = mapping.read_bytes()
    review_unit_id = _review_unit_from_mapping(mapping_raw, formal_id)
    return FormalProof(
        kind="already_current_verification",
        sha256=digest,
        ref=path.as_posix(),
        formal_id=formal_id,
        review_unit_id=review_unit_id,
        mapping_basis="verification_plus_canonical_mapping_snapshot",
        mapping_sha256=_sha_bytes(mapping_raw),
        transaction_id=None,
        capture_binding_basis="already_current_verification",
        material=value,
    )


def verify_formal_proof(
    repo_root: str | Path,
    *,
    batch_id: str,
    capture_id: str,
    formal_id: str,
    formal_result_status: str,
    formal_result_receipt_sha256: str,
    formal_proof_path: str | Path | None = None,
    runtime_root: str | Path = DEFAULT_RUNTIME_ROOT,
    historical_result_event: dict[str, Any] | None = None,
) -> FormalProof:
    """Verify one already completed formal result without invoking the writer."""

    repo = Path(repo_root).expanduser().resolve(strict=True)
    runtime = Path(runtime_root).expanduser().resolve(strict=True)
    digest = str(formal_result_receipt_sha256 or "")
    _validate_identity(
        batch_id=batch_id,
        capture_id=capture_id,
        formal_id=formal_id,
        digest=digest,
    )
    if formal_result_status == "curated":
        return _verify_normal_receipt(
            repo,
            runtime,
            batch_id=batch_id,
            capture_id=capture_id,
            formal_id=formal_id,
            digest=digest,
            supplied=formal_proof_path,
            historical_result_event=historical_result_event,
        )
    if formal_result_status == "already_current":
        return _verify_already_current(
            repo,
            runtime,
            batch_id=batch_id,
            capture_id=capture_id,
            formal_id=formal_id,
            digest=digest,
            supplied=formal_proof_path,
        )
    raise FormalProofError("formal_result_status must be curated or already_current")
