#!/usr/bin/env python3
"""Build a manifest-bound nightly curation corpus for 408 personalization.

The capture ledger and formal repository remain the truth stores.  This module
joins only receipt/verification-bound successful results from closed, globally
passing daily-curation batches to the current answer-safe formal node surface.
Every emitted item is content-addressed and the entire corpus is disposable.
"""

from __future__ import annotations

import argparse
import fcntl
import hashlib
import hmac
import json
import os
import re
import stat
import tempfile
from collections import defaultdict
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator

import intake_fact_capture_408 as capture_ledger
from intake_lib_408 import (
    FORMAL_ID_RE,
    MASTER_COLS,
    iter_table_rows,
    scan_leak,
)
from personalization_commonality_408 import extract_facets

import runtime_paths_408 as _paths


SCHEMA = "personalization_corpus_v1"
MANIFEST_KIND = "nightly_curation_corpus_manifest"
INDEX_KIND = "nightly_curation_corpus_index"
BATCH_KIND = "nightly_curation_corpus_batch"
ITEM_KIND = "nightly_curation_corpus_item"
DEFAULT_OUTPUT_RELATIVE = Path(
    "wiki/study_vaults/408-full/state/personalization/corpus"
)
REVIEW_LEDGER_RELATIVE = Path(
    "wiki/study_vaults/408-full/state/review-loop/events.jsonl"
)
SUCCESS_OUTCOMES = {"curated", "already_current"}
CHOICE_RESULTS = {"correct", "incorrect", "partial", "uncertain", "not_applicable"}
REASONING_RESULTS = {"sound", "partial", "diverged", "not_observed"}
DEFAULT_RUNTIME_ROOT = _paths.intake_runtime_root().expanduser().resolve()
ALREADY_CURRENT_SCHEMA = "already_current_verification_v1"
ALREADY_CURRENT_KIND = "already_current_verification"
LEGACY_COMPATIBILITY_SCHEMA = "legacy_complete_compatibility_receipt_v1"
LEGACY_COMPATIBILITY_KIND = "daily_curation_legacy_complete_compatibility"
LEGACY_COMPATIBILITY_VERSION = "daily_curation_legacy_complete_compatibility_v1"
LEGACY_COMPATIBILITY_RECEIPT_DIR = "legacy-complete-compatibility-receipts"
POINT_RE = re.compile(r"\b(?:DS|CO|OS|CN)\d{2}-\d{2}\b")
SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
FORMAL_ID_TEXT_RE = re.compile(r"^(?:DS|CO|OS|CN)_(?:\d{4}|UNK)_\d{3}$")
DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
SEMANTIC_ID_RE = re.compile(r"^[A-Za-z0-9_.:-]{1,120}$")
SAFE_CARD_REQUIRED_FIELDS = (
    "正式节点 ID",
    "安全题目摘要",
    "关键题设参数",
    "问法类型",
    "用户错误入口",
    "复做第一动作",
)
FORBIDDEN_CORPUS_KEYS = {
    "full_question",
    "question_stem",
    "complete_stem",
    "correct_answer",
    "standard_answer",
    "wrong_answer",
    "user_answer",
    "options",
    "full_response",
    "full_explanation",
    "analysis_text",
    "attachment_path",
    "temporary_path",
    "image_path",
    "完整题干",
    "正确答案",
    "标准答案",
    "错误答案",
    "用户答案",
    "选项",
    "完整解析",
}


class PersonalizationCorpusError(RuntimeError):
    """A nightly corpus contract or integrity check failed."""


def _retired_write_surface() -> None:
    raise PersonalizationCorpusError(
        "personalization_corpus_writes_retired_read_only_compatibility"
    )


def canonical_json_bytes(value: Any) -> bytes:
    return json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")


def sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def sha256_value(value: Any) -> str:
    return sha256_bytes(canonical_json_bytes(value))


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _repo_key(repo: Path) -> str:
    return hashlib.sha256(str(repo.resolve()).encode("utf-8")).hexdigest()[:20]


def _load_json_object(path: Path, label: str) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise PersonalizationCorpusError(f"{label} is unreadable: {path}") from exc
    if not isinstance(value, dict):
        raise PersonalizationCorpusError(f"{label} is not a JSON object: {path}")
    return value


def _bounded_regular_file(path: Path, parent: Path, label: str) -> Path:
    if path.is_symlink() or not path.is_file():
        raise PersonalizationCorpusError(f"{label} is missing or unsafe: {path}")
    try:
        resolved = path.resolve(strict=True)
        resolved.relative_to(parent.resolve(strict=True))
    except (OSError, ValueError) as exc:
        raise PersonalizationCorpusError(f"{label} escapes its evidence root: {path}") from exc
    return resolved


def _normal_text(value: Any) -> str:
    return " ".join(str(value or "").strip().split())


def _assert_safe_text(label: str, value: Any) -> str:
    text = _normal_text(value)
    fails, warns = scan_leak(text)
    findings = [*fails, *warns]
    if findings:
        raise PersonalizationCorpusError(
            f"{label} failed answer-safety scan: {'; '.join(findings)}"
        )
    return text


def _assert_no_forbidden_keys(value: Any, path: str = "corpus") -> None:
    if isinstance(value, dict):
        for key, nested in value.items():
            if str(key) in FORBIDDEN_CORPUS_KEYS:
                raise PersonalizationCorpusError(
                    f"{path}.{key} is forbidden in the personalization corpus"
                )
            _assert_no_forbidden_keys(nested, f"{path}.{key}")
    elif isinstance(value, list):
        for index, nested in enumerate(value):
            _assert_no_forbidden_keys(nested, f"{path}[{index}]")


def _assert_safe_payload(value: Any, path: str = "corpus") -> None:
    _assert_no_forbidden_keys(value, path)
    if isinstance(value, dict):
        for key, nested in value.items():
            _assert_safe_payload(nested, f"{path}.{key}")
    elif isinstance(value, list):
        for index, nested in enumerate(value):
            _assert_safe_payload(nested, f"{path}[{index}]")
    elif isinstance(value, str):
        _assert_safe_text(path, value)


def _load_capture_events(repo: Path) -> list[dict[str, Any]]:
    path = capture_ledger.capture_root(repo) / "events.jsonl"
    if not path.is_file():
        raise PersonalizationCorpusError(f"capture ledger missing: {path}")
    events: list[dict[str, Any]] = []
    for line_number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        if not line.strip():
            continue
        try:
            event = json.loads(line)
        except json.JSONDecodeError as exc:
            raise PersonalizationCorpusError(
                f"invalid capture ledger JSON at line {line_number}"
            ) from exc
        if not isinstance(event, dict):
            raise PersonalizationCorpusError(
                f"capture ledger line {line_number} is not an object"
            )
        events.append(event)
    try:
        capture_ledger.replay(events)
    except capture_ledger.CaptureError as exc:
        raise PersonalizationCorpusError(str(exc)) from exc
    return events


def _load_review_events(repo: Path) -> dict[str, dict[str, Any]]:
    path = repo / REVIEW_LEDGER_RELATIVE
    if not path.is_file():
        return {}
    events: dict[str, dict[str, Any]] = {}
    for line_number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        if not line.strip():
            continue
        try:
            event = json.loads(line)
        except json.JSONDecodeError as exc:
            raise PersonalizationCorpusError(
                f"invalid review ledger JSON at line {line_number}"
            ) from exc
        event_id = str(event.get("event_id") or "") if isinstance(event, dict) else ""
        if not event_id or event_id in events:
            raise PersonalizationCorpusError("review ledger has duplicate or empty event_id")
        if event.get("schema") != "review_event_v1":
            raise PersonalizationCorpusError(
                f"review event has invalid schema: {event_id}"
            )
        events[event_id] = event
    return events


def _load_master_rows(repo: Path) -> dict[str, dict[str, str]]:
    path = repo / "节点总表.md"
    if not path.is_file():
        raise PersonalizationCorpusError(f"formal master missing: {path}")
    rows: dict[str, dict[str, str]] = {}
    lines = path.read_text(encoding="utf-8").splitlines()
    for line_number, cells in iter_table_rows(lines, "ID"):
        if len(cells) != len(MASTER_COLS):
            raise PersonalizationCorpusError(
                f"formal master row has {len(cells)} columns at line {line_number + 1}"
            )
        row = dict(zip(MASTER_COLS, cells))
        formal_id = row["ID"]
        if not FORMAL_ID_RE.fullmatch(formal_id):
            continue
        if formal_id in rows:
            raise PersonalizationCorpusError(f"duplicate formal node: {formal_id}")
        rows[formal_id] = row
    if not rows:
        raise PersonalizationCorpusError("formal master contains no valid nodes")
    return rows


def _load_safe_card(repo: Path, formal_id: str) -> tuple[dict[str, str], str]:
    path = repo / f"{formal_id}.md"
    if not path.is_file() or path.is_symlink():
        raise PersonalizationCorpusError(f"safe card missing or unsafe: {formal_id}")
    fields: dict[str, str] = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        match = re.match(r"^\s*-\s*([^：]+)：\s*(.*)\s*$", line)
        if not match:
            continue
        label = match.group(1).strip()
        value = match.group(2).strip()
        if label in fields and fields[label] != value:
            if label in SAFE_CARD_REQUIRED_FIELDS:
                raise PersonalizationCorpusError(
                    f"safe card has conflicting {label}: {formal_id}"
                )
            continue
        fields[label] = value
    missing = [field for field in SAFE_CARD_REQUIRED_FIELDS if not fields.get(field)]
    if missing:
        raise PersonalizationCorpusError(
            f"safe card missing required fields for {formal_id}: {missing}"
        )
    if fields["正式节点 ID"] != formal_id:
        raise PersonalizationCorpusError(f"safe card identity mismatch: {formal_id}")
    selected = {
        field: _assert_safe_text(f"{formal_id}.{field}", fields.get(field, ""))
        for field in (
            "安全题目摘要",
            "关键题设参数",
            "问法类型",
            "实际考查机制",
            "用户错误入口",
            "复做第一动作",
        )
    }
    return selected, sha256_file(path)


def _receipt_index(repo: Path, runtime_root: Path) -> dict[str, Path]:
    """Index only normal receipts for this exact repository, by content hash."""
    receipt_root = runtime_root / "receipts" / _repo_key(repo)
    if not receipt_root.is_dir() or receipt_root.is_symlink():
        raise PersonalizationCorpusError(
            f"normal receipt root missing or unsafe: {receipt_root}"
        )
    indexed: dict[str, Path] = {}
    for raw_path in sorted(receipt_root.glob("*.json")):
        if raw_path.is_symlink() or not raw_path.is_file():
            continue
        path = _bounded_regular_file(raw_path, receipt_root, "normal receipt")
        digest = sha256_file(path)
        if digest in indexed:
            raise PersonalizationCorpusError(
                f"duplicate normal receipt content for repository: {digest}"
            )
        indexed[digest] = path
    return indexed


def _validate_normal_receipt(
    *,
    repo: Path,
    runtime_root: Path,
    receipt_index: dict[str, Path],
    expected_sha256: str,
    formal_id: str,
    capture_row: dict[str, Any],
) -> dict[str, Any]:
    path = receipt_index.get(expected_sha256)
    if path is None:
        raise PersonalizationCorpusError(
            f"curated result receipt entity is missing: {expected_sha256}"
        )
    if sha256_file(path) != expected_sha256:
        raise PersonalizationCorpusError(
            f"curated result receipt content hash mismatch: {expected_sha256}"
        )
    receipt = _load_json_object(path, "normal receipt")
    if receipt.get("schema") != "intake_batch_receipt_v1" or receipt.get(
        "status"
    ) != "COMMITTED":
        raise PersonalizationCorpusError("normal receipt schema/status is invalid")
    try:
        receipt_repo = Path(str(receipt.get("repo_root") or "")).expanduser().resolve(
            strict=True
        )
    except OSError as exc:
        raise PersonalizationCorpusError("normal receipt repo_root is invalid") from exc
    if receipt_repo != repo:
        raise PersonalizationCorpusError("normal receipt is bound to another repository")
    items = receipt.get("items")
    if not isinstance(items, list) or len(items) != 1 or not isinstance(items[0], dict):
        raise PersonalizationCorpusError("normal receipt must bind one formal item")
    receipt_item = items[0]
    mode = str(receipt_item.get("mode") or "")
    if receipt_item.get("formal_id") != formal_id or mode not in {"new", "redo"}:
        raise PersonalizationCorpusError(
            f"normal receipt formal identity mismatch: {formal_id}"
        )
    identity_mode = str(
        capture_row.get("capture", {}).get("identity_hint", {}).get("mode") or ""
    )
    if identity_mode in {"new", "redo"} and identity_mode != mode:
        raise PersonalizationCorpusError(
            f"normal receipt mode conflicts with capture identity: {formal_id}"
        )
    idempotency_key = str(receipt.get("idempotency_key") or "")
    expected_name = f"{hashlib.sha256(idempotency_key.encode('utf-8')).hexdigest()}.json"
    if not idempotency_key or path.name != expected_name:
        raise PersonalizationCorpusError("normal receipt path/idempotency binding is invalid")
    if not SHA256_RE.fullmatch(str(receipt.get("payload_sha256") or "")):
        raise PersonalizationCorpusError("normal receipt payload_sha256 is invalid")
    transaction_id = str(receipt.get("transaction_id") or "")
    if not transaction_id:
        raise PersonalizationCorpusError("normal receipt transaction_id is missing")
    changed_files = receipt.get("changed_files")
    if not isinstance(changed_files, list):
        raise PersonalizationCorpusError("normal receipt changed_files is invalid")
    required_targets = {str(repo / "节点总表.md"), str(repo / f"{formal_id}.md")}
    if not required_targets.issubset({str(value) for value in changed_files}):
        raise PersonalizationCorpusError(
            f"normal receipt does not publish required formal surfaces: {formal_id}"
        )

    transaction_root = runtime_root / "transactions" / _repo_key(repo)
    journal_raw = Path(str(receipt.get("journal") or "")).expanduser()
    if not journal_raw.is_absolute():
        raise PersonalizationCorpusError("normal receipt journal path is not absolute")
    journal_path = _bounded_regular_file(journal_raw, transaction_root, "normal WAL")
    journal = _load_json_object(journal_path, "normal WAL")
    if journal.get("schema") != "intake_batch_wal_v1" or journal.get(
        "state"
    ) != "committed":
        raise PersonalizationCorpusError("normal WAL schema/state is invalid")
    for key in ("transaction_id", "idempotency_key", "payload_sha256", "repo_root"):
        if journal.get(key) != receipt.get(key):
            raise PersonalizationCorpusError(f"normal receipt/WAL {key} mismatch")
    if journal.get("result_items") != items:
        raise PersonalizationCorpusError("normal receipt/WAL formal items mismatch")
    journal_sha = sha256_file(journal_path)
    return {
        "entity_kind": "normal_receipt",
        "entity_sha256": expected_sha256,
        "journal_sha256": journal_sha,
        "transaction_id": transaction_id,
        "idempotency_key_sha256": sha256_bytes(idempotency_key.encode("utf-8")),
        "formal_id": formal_id,
        "mode": mode,
    }


def _already_current_material(
    *,
    repo: Path,
    batch_id: str,
    capture_id: str,
    study_date: str,
    formal_id: str,
    capture_payload_sha256: str,
    formal_row_sha256: str,
    safe_card_sha256: str,
) -> dict[str, Any]:
    return {
        "schema": ALREADY_CURRENT_SCHEMA,
        "kind": ALREADY_CURRENT_KIND,
        "status": "VERIFIED_CURRENT",
        "answer_safe": True,
        "formal_write_count": 0,
        "repo_root": str(repo),
        "batch_id": batch_id,
        "capture_id": capture_id,
        "study_date": study_date,
        "formal_id": formal_id,
        "capture_payload_sha256": capture_payload_sha256,
        "formal_row_sha256": formal_row_sha256,
        "safe_card_sha256": safe_card_sha256,
    }


def _verification_root(repo: Path, runtime_root: Path) -> Path:
    return runtime_root / "verification-receipts" / _repo_key(repo)


def seal_already_current_verification(
    repo_root: str | Path,
    *,
    batch_id: str,
    capture_id: str,
    formal_id: str,
    runtime_root: str | Path = DEFAULT_RUNTIME_ROOT,
) -> dict[str, Any]:
    """Seal current identity hashes before recording an already_current result."""
    _retired_write_surface()
    repo = Path(repo_root).expanduser().resolve(strict=True)
    runtime = Path(runtime_root).expanduser().resolve()
    try:
        capture_ledger.audit(repo_root=repo)
    except capture_ledger.CaptureError as exc:
        raise PersonalizationCorpusError(str(exc)) from exc
    events = _load_capture_events(repo)
    state = capture_ledger.replay(events)
    batch = state["batches"].get(batch_id)
    capture_row = state["captures"].get(capture_id)
    if not batch or batch.get("status") != "active":
        raise PersonalizationCorpusError("verification requires an active curation batch")
    if capture_id not in batch.get("capture_ids", []) or not capture_row:
        raise PersonalizationCorpusError("verification is not bound to the batch capture")
    if capture_id in batch.get("results", {}):
        raise PersonalizationCorpusError("verification must precede the item result")
    if not capture_row.get("formalization_authorized"):
        raise PersonalizationCorpusError("verification capture is not authorized")
    if str(capture_row.get("study_date") or "") != str(batch.get("study_date") or ""):
        raise PersonalizationCorpusError("verification capture/batch date mismatch")
    if not FORMAL_ID_RE.fullmatch(formal_id):
        raise PersonalizationCorpusError("verification formal_id is invalid")
    identity_formal = str(
        capture_row.get("capture", {}).get("identity_hint", {}).get("formal_id") or ""
    )
    if identity_formal and identity_formal != formal_id:
        raise PersonalizationCorpusError("verification conflicts with capture formal identity")
    rows = _load_master_rows(repo)
    row = rows.get(formal_id)
    if row is None:
        raise PersonalizationCorpusError("verification formal node does not exist")
    _, safe_card_sha = _load_safe_card(repo, formal_id)
    material = _already_current_material(
        repo=repo,
        batch_id=batch_id,
        capture_id=capture_id,
        study_date=str(batch["study_date"]),
        formal_id=formal_id,
        capture_payload_sha256=str(capture_row["payload_sha256"]),
        formal_row_sha256=sha256_value(row),
        safe_card_sha256=safe_card_sha,
    )
    _assert_safe_payload(material)
    content = canonical_json_bytes(material)
    digest = sha256_bytes(content)
    root = _verification_root(repo, runtime)
    root.mkdir(parents=True, exist_ok=True)
    if root.is_symlink():
        raise PersonalizationCorpusError("verification receipt root is unsafe")
    path = root / f"{digest}.json"
    if path.exists() and (
        path.is_symlink() or not path.is_file() or path.read_bytes() != content
    ):
        raise PersonalizationCorpusError("verification receipt path collision")
    if not path.exists():
        _atomic_write(path, content)
    return {
        "status": "SEALED",
        "schema": ALREADY_CURRENT_SCHEMA,
        "verification_sha256": digest,
        "verification_path": str(path),
        "batch_id": batch_id,
        "capture_id": capture_id,
        "formal_id": formal_id,
        "formal_write_count": 0,
    }


def _validate_already_current_verification(
    *,
    repo: Path,
    runtime_root: Path,
    expected_sha256: str,
    expected_material: dict[str, Any],
) -> dict[str, Any]:
    root = _verification_root(repo, runtime_root)
    path = _bounded_regular_file(
        root / f"{expected_sha256}.json", root, "already_current verification"
    )
    if sha256_file(path) != expected_sha256:
        raise PersonalizationCorpusError(
            "already_current verification content hash mismatch"
        )
    value = _load_json_object(path, "already_current verification")
    stable_fields = {
        "schema",
        "kind",
        "status",
        "answer_safe",
        "formal_write_count",
        "batch_id",
        "capture_id",
        "study_date",
        "formal_id",
        "capture_payload_sha256",
    }
    if any(value.get(field) != expected_material.get(field) for field in stable_fields):
        raise PersonalizationCorpusError(
            "already_current verification identity or source hash mismatch"
        )
    sealed_repo_root = str(value.get("repo_root") or "")
    if not sealed_repo_root or not Path(sealed_repo_root).is_absolute():
        raise PersonalizationCorpusError(
            "already_current verification repository binding is invalid"
        )
    for field in ("formal_row_sha256", "safe_card_sha256"):
        if not SHA256_RE.fullmatch(str(value.get(field) or "")):
            raise PersonalizationCorpusError(
                "already_current verification formal snapshot hash is invalid"
            )
    _assert_safe_payload(value)
    return {
        "entity_kind": ALREADY_CURRENT_KIND,
        "entity_sha256": expected_sha256,
        "formal_id": str(value["formal_id"]),
        "formal_row_sha256": str(value["formal_row_sha256"]),
        "safe_card_sha256": str(value["safe_card_sha256"]),
        "capture_payload_sha256": str(value["capture_payload_sha256"]),
        "sealed_repo_root": sealed_repo_root,
    }


def _event_formal_ids(event: dict[str, Any]) -> set[str]:
    values: set[str] = set()
    direct = str(event.get("formal_node_id") or "")
    if FORMAL_ID_TEXT_RE.fullmatch(direct):
        values.add(direct)
    mapped = event.get("mapped_formal_node_ids") or []
    if isinstance(mapped, str):
        mapped = [mapped]
    if isinstance(mapped, list):
        values.update(
            str(value)
            for value in mapped
            if FORMAL_ID_TEXT_RE.fullmatch(str(value))
        )
    actual = str(event.get("actual_question_formal_node_id") or "")
    if FORMAL_ID_TEXT_RE.fullmatch(actual):
        values.add(actual)
    return values


def _review_ref_event_id(ref: dict[str, Any]) -> str | None:
    kind = str(ref.get("kind") or "")
    locator = str(ref.get("locator") or "")
    prefix = f"{REVIEW_LEDGER_RELATIVE}#"
    if kind not in {
        "review_loop_event",
        "ordinary_review_loop_event",
        "review_outcome",
    } or not locator.startswith(prefix):
        return None
    suffix = locator[len(prefix) :]
    if suffix.startswith("event_id="):
        suffix = suffix[len("event_id=") :]
    return suffix or None


def _safe_outcome_projection(event: dict[str, Any]) -> dict[str, Any]:
    choice_present = "choice_result" in event
    reasoning_present = "reasoning_result" in event
    choice_result = event.get("choice_result") if choice_present else "not_applicable"
    reasoning_result = (
        event.get("reasoning_result") if reasoning_present else "not_observed"
    )
    if not isinstance(choice_result, str) or choice_result not in CHOICE_RESULTS:
        raise PersonalizationCorpusError(
            f"review event has invalid choice_result: {event.get('event_id')}"
        )
    if not isinstance(reasoning_result, str) or reasoning_result not in REASONING_RESULTS:
        raise PersonalizationCorpusError(
            f"review event has invalid reasoning_result: {event.get('event_id')}"
        )
    return {
        "event_id": _assert_safe_text("review.event_id", event.get("event_id")),
        "source": _assert_safe_text("review.source", event.get("source")),
        "observed_date": _assert_safe_text(
            "review.observed_date", event.get("observed_date")
        ),
        "source_id": _assert_safe_text("review.source_id", event.get("source_id")),
        "formal_node_ids": sorted(_event_formal_ids(event)),
        "knowledge_point": _assert_safe_text(
            "review.knowledge_point", event.get("knowledge_point")
        ),
        "mechanism_key": _assert_safe_text(
            "review.mechanism_key", event.get("mechanism_key")
        ),
        "review_unit_id": _assert_safe_text(
            "review.review_unit_id", event.get("review_unit_id")
        ),
        "first_result": _assert_safe_text(
            "review.first_result", event.get("first_result")
        ),
        "choice_result": choice_result,
        "reasoning_result": reasoning_result,
        "choice_result_evidence_field_present": choice_present,
        "reasoning_result_evidence_field_present": reasoning_present,
        "confidence": _assert_safe_text("review.confidence", event.get("confidence")),
        "evidence_kind": _assert_safe_text(
            "review.evidence_kind", event.get("evidence_kind")
        ),
        "fragile_mastery": bool(event.get("fragile_mastery")),
        "stable_retention": bool(event.get("stable_retention")),
    }


def _bind_canonical_outcome(
    capture_row: dict[str, Any],
    formal_id: str,
    review_events: dict[str, dict[str, Any]],
) -> dict[str, Any]:
    capture_payload = capture_row["capture"]
    exact_refs: list[tuple[str, str]] = []
    for ref in capture_payload.get("stable_evidence_refs") or []:
        event_id = _review_ref_event_id(ref)
        if event_id:
            exact_refs.append((event_id, str(ref.get("sha256") or "")))
    exact_ids = sorted({event_id for event_id, _ in exact_refs})
    if len(exact_ids) > 1:
        raise PersonalizationCorpusError(
            f"capture binds multiple review outcomes: {capture_row['capture_id']}"
        )
    if exact_ids:
        event_id = exact_ids[0]
        event = review_events.get(event_id)
        if not event or event.get("event_kind") != "outcome":
            raise PersonalizationCorpusError(
                f"capture exact review outcome missing: {capture_row['capture_id']}"
            )
        actual_sha = sha256_value(event)
        if any(expected != actual_sha for current, expected in exact_refs if current == event_id):
            raise PersonalizationCorpusError(
                f"capture review outcome hash drift: {capture_row['capture_id']}"
            )
        study_date = str(capture_row.get("study_date") or "")
        capture_source_id = str(
            capture_payload.get("source_facts", {}).get("source_id") or ""
        )
        if str(event.get("observed_date") or "") != study_date:
            raise PersonalizationCorpusError(
                f"capture review outcome date mismatch: {capture_row['capture_id']}"
            )
        if capture_source_id and str(event.get("source_id") or "") != capture_source_id:
            raise PersonalizationCorpusError(
                f"capture review outcome source mismatch: {capture_row['capture_id']}"
            )
        event_formal_ids = _event_formal_ids(event)
        if event_formal_ids and formal_id not in event_formal_ids:
            raise PersonalizationCorpusError(
                f"capture review outcome formal identity mismatch: {capture_row['capture_id']}"
            )
        return {
            "status": "exact_ref",
            "event_sha256": actual_sha,
            "outcome": _safe_outcome_projection(event),
            "gap": None,
        }

    study_date = str(capture_row.get("study_date") or "")
    source_id = str(capture_payload.get("source_facts", {}).get("source_id") or "")
    candidates: list[dict[str, Any]] = []
    identity_conflicts: list[dict[str, Any]] = []
    for event in review_events.values():
        if event.get("event_kind") != "outcome" or event.get("observed_date") != study_date:
            continue
        event_source_id = str(event.get("source_id") or "")
        event_formal_ids = _event_formal_ids(event)
        source_match = bool(source_id and event_source_id == source_id)
        formal_match = formal_id in event_formal_ids
        matched_identity = source_match or formal_match
        source_conflict = bool(source_id and event_source_id != source_id)
        formal_conflict = bool(event_formal_ids and not formal_match)
        if matched_identity and (source_conflict or formal_conflict):
            identity_conflicts.append(event)
            continue
        if matched_identity:
            candidates.append(event)
    candidates.sort(key=lambda event: str(event.get("event_id") or ""))
    identity_conflicts.sort(key=lambda event: str(event.get("event_id") or ""))
    if len(candidates) == 1:
        event = candidates[0]
        return {
            "status": "unique_identity_match",
            "event_sha256": sha256_value(event),
            "outcome": _safe_outcome_projection(event),
            "gap": None,
        }
    if candidates:
        status = "ambiguous"
        reason = "multiple same-day canonical outcomes share every provided identity"
        candidate_ids = [str(event.get("event_id") or "") for event in candidates[:8]]
    elif identity_conflicts:
        status = "identity_conflict"
        reason = "same-day outcomes partially match but conflict with another identity dimension"
        candidate_ids = [
            str(event.get("event_id") or "") for event in identity_conflicts[:8]
        ]
    else:
        status = "no_match"
        reason = "capture has no canonical outcome matching every provided identity"
        candidate_ids = []
    return {
        "status": status,
        "event_sha256": None,
        "outcome": None,
        "gap": {
            "reason": reason,
            "candidate_event_ids": candidate_ids,
        },
    }


def _point_ids(row: dict[str, str]) -> list[str]:
    text = " ".join(
        row.get(field, "") for field in ("主知识点", "副知识点", "命中知识点")
    )
    return sorted(set(POINT_RE.findall(text)))


def _semantic_capture_facts(capture_row: dict[str, Any]) -> dict[str, Any]:
    payload = capture_row["capture"]
    user = payload.get("user_facts") or {}
    source = payload.get("source_facts") or {}
    return {
        "answer_safe_context_anchor": _assert_safe_text(
            "capture.answer_safe_context_anchor",
            payload.get("answer_safe_context_anchor"),
        ),
        "user_error_entry": _assert_safe_text(
            "capture.user_error_entry", user.get("user_error_entry")
        ),
        "user_error_provenance": _assert_safe_text(
            "capture.user_error_provenance", user.get("user_error_provenance")
        ),
        "first_action": _assert_safe_text(
            "capture.first_action", user.get("first_action")
        ),
        "first_action_provenance": _assert_safe_text(
            "capture.first_action_provenance", user.get("first_action_provenance")
        ),
        "observed_at": _assert_safe_text(
            "capture.observed_at", user.get("observed_at")
        ),
        "source_id": _assert_safe_text("capture.source_id", source.get("source_id")),
        "source_subject": _assert_safe_text(
            "capture.source_subject", source.get("subject")
        ),
        "source_question_type": _assert_safe_text(
            "capture.source_question_type", source.get("question_type")
        ),
    }


def _formal_snapshot(
    formal_id: str, row: dict[str, str], safe_card: dict[str, str]
) -> dict[str, Any]:
    values = {
        "formal_id": formal_id,
        "source_id": row.get("来源ID", ""),
        "year": row.get("年份", ""),
        "subject": row.get("科目", ""),
        "module": row.get("主模块", ""),
        "primary_knowledge": row.get("主知识点", ""),
        "secondary_knowledge": row.get("副知识点", ""),
        "hit_knowledge": row.get("命中知识点", ""),
        "question_type": row.get("题型", ""),
        "core_point": row.get("核心考点", ""),
        "fuzzy_concepts": row.get("模糊概念", ""),
        "error_tags": row.get("错因标签", ""),
        "first_study_date": row.get("首次做题日期", ""),
        "last_review_date": row.get("最近复做日期", ""),
        "recent_error_record": row.get("最近错误记录", ""),
        "safe_summary": safe_card.get("安全题目摘要", ""),
        "key_parameters": safe_card.get("关键题设参数", ""),
        "prompt_type": safe_card.get("问法类型", ""),
        "actual_mechanism": safe_card.get("实际考查机制", ""),
        "formal_user_error_entry": safe_card.get("用户错误入口", ""),
        "redo_first_action": safe_card.get("复做第一动作", ""),
    }
    return {
        key: _assert_safe_text(f"{formal_id}.{key}", value)
        for key, value in values.items()
    }


def _semantic_index_keys(item: dict[str, Any]) -> dict[str, list[str]]:
    """Derive controlled semantic keys from one answer-safe corpus item.

    These keys are a cold projection only.  They make long-horizon retrieval
    possible without scanning every corpus object during an answer turn.
    """

    observed = item.get("observed_facts") or {}
    snapshot = item.get("formal_snapshot") or {}
    facets = extract_facets(
        [
            observed.get("user_error_entry"),
            observed.get("learner_error_entry"),
            observed.get("error_entry"),
            observed.get("first_action"),
            snapshot.get("actual_mechanism"),
            snapshot.get("core_point"),
            snapshot.get("redo_first_action"),
            snapshot.get("key_parameters"),
            snapshot.get("fuzzy_concepts"),
            snapshot.get("error_tags"),
        ]
    )
    point_mechanisms = [
        f"KP:{point_id}"
        for point_id in item.get("point_ids", [])
        if isinstance(point_id, str) and POINT_RE.fullmatch(point_id)
    ]
    return {
        "mechanism_ids": sorted(
            set(point_mechanisms) | set(facets.get("mechanism_primitives", []))
        ),
        "error_pattern_ids": sorted(set(facets.get("error_patterns", []))),
        "method_ids": sorted(set(facets.get("method_templates", []))),
    }


def _result_event_map(
    events: list[dict[str, Any]],
) -> tuple[dict[tuple[str, str], dict[str, Any]], dict[str, dict[str, Any]]]:
    results: dict[tuple[str, str], dict[str, Any]] = {}
    closes: dict[str, dict[str, Any]] = {}
    for event in events:
        if event.get("event_type") == "curation_item_result":
            key = (str(event.get("batch_id") or ""), str(event.get("capture_id") or ""))
            if key in results:
                raise PersonalizationCorpusError(f"duplicate curation result event: {key}")
            results[key] = event
        elif event.get("event_type") == "curation_batch_closed":
            batch_id = str(event.get("batch_id") or "")
            if batch_id in closes:
                raise PersonalizationCorpusError(f"duplicate batch close event: {batch_id}")
            closes[batch_id] = event
    return results, closes


def _validate_batch_close_chain(
    *,
    repo: Path,
    batch: dict[str, Any],
    close_event: dict[str, Any],
    require_current_objects: bool,
) -> None:
    batch_id = str(batch.get("batch_id") or "")
    failed = sorted(
        current_id
        for current_id, value in batch.get("results", {}).items()
        if value.get("outcome") == "failed"
    )
    needs_user = sorted(
        current_id
        for current_id, value in batch.get("results", {}).items()
        if value.get("outcome") == "needs_user"
    )
    close_payload = {
        "batch_id": batch_id,
        "status": str(batch.get("status") or ""),
        "global_closeout": str(batch.get("global_closeout") or ""),
        "needs_user_capture_ids": needs_user,
        "failed_capture_ids": failed,
    }
    close_contract = close_event.get("close_contract")
    if close_contract is None:
        if close_event.get("global_audit_receipt_sha256") is not None:
            raise PersonalizationCorpusError(
                f"legacy batch close mixes aggregate receipt: {batch_id}"
            )
    elif close_contract == capture_ledger.GLOBAL_AUDIT_CLOSE_CONTRACT:
        close_payload.update(
            {
                "close_contract": close_contract,
                "global_audit_receipt_sha256": close_event.get(
                    "global_audit_receipt_sha256"
                ),
            }
        )
        if close_payload["global_closeout"] == "pass":
            try:
                capture_ledger._validate_global_audit_receipt(
                    repo,
                    capture_ledger.capture_root(repo),
                    batch,
                    close_event.get("global_audit_receipt_sha256"),
                    require_current_objects=require_current_objects,
                )
            except capture_ledger.CaptureError as exc:
                raise PersonalizationCorpusError(str(exc)) from exc
    else:
        raise PersonalizationCorpusError(
            f"unknown batch close contract: {batch_id}"
        )
    if any(close_event.get(key) != value for key, value in close_payload.items()):
        raise PersonalizationCorpusError(f"batch close event mismatch: {batch_id}")
    if close_event.get("close_sha256") != sha256_value(close_payload):
        raise PersonalizationCorpusError(f"batch close content hash mismatch: {batch_id}")


def _validate_success_chain(
    *,
    batch: dict[str, Any],
    capture_id: str,
    capture_row: dict[str, Any],
    result: dict[str, Any],
    result_event: dict[str, Any],
) -> None:
    batch_id = str(batch.get("batch_id") or "")
    if capture_id not in batch.get("capture_ids", []):
        raise PersonalizationCorpusError(
            f"result capture is absent from batch inventory: {batch_id}/{capture_id}"
        )
    if str(capture_row.get("study_date") or "") != str(batch.get("study_date") or ""):
        raise PersonalizationCorpusError(
            f"batch/capture study date mismatch: {batch_id}/{capture_id}"
        )
    if not capture_row.get("formalization_authorized"):
        raise PersonalizationCorpusError(
            f"successful result references unauthorized capture: {capture_id}"
        )
    capture_payload = capture_row.get("capture")
    if not isinstance(capture_payload, dict) or capture_row.get(
        "payload_sha256"
    ) != sha256_value(capture_payload):
        raise PersonalizationCorpusError(f"capture payload hash mismatch: {capture_id}")
    formal_id = str(result.get("formal_id") or "")
    identity_formal = str(
        capture_payload.get("identity_hint", {}).get("formal_id") or ""
    )
    if identity_formal and identity_formal != formal_id:
        raise PersonalizationCorpusError(
            f"result conflicts with explicit capture formal identity: {capture_id}"
        )
    result_payload = {
        "batch_id": batch_id,
        "capture_id": capture_id,
        "outcome": result.get("outcome"),
        "formal_id": result.get("formal_id"),
        "receipt_sha256": result.get("receipt_sha256"),
        "verification_sha256": result.get("verification_sha256"),
        "reason": result.get("reason"),
    }
    if any(result_event.get(key) != value for key, value in result_payload.items()):
        raise PersonalizationCorpusError(
            f"replayed result/event mismatch: {batch_id}/{capture_id}"
        )
    if result_event.get("result_sha256") != sha256_value(result_payload):
        raise PersonalizationCorpusError(
            f"curation result content hash mismatch: {batch_id}/{capture_id}"
        )


def _legacy_close_binding(close_event: dict[str, Any]) -> dict[str, Any]:
    return {
        "event_id": str(close_event.get("event_id") or ""),
        "idempotency_key": str(close_event.get("idempotency_key") or ""),
        "close_sha256": str(close_event.get("close_sha256") or ""),
        "event_sha256": sha256_value(close_event),
    }


def _legacy_completion_bindings(
    *,
    repo: Path,
    runtime_root: Path,
    batch: dict[str, Any],
    state: dict[str, Any],
    result_events: dict[tuple[str, str], dict[str, Any]],
    formal_rows: dict[str, dict[str, str]],
) -> list[dict[str, Any]]:
    batch_id = str(batch.get("batch_id") or "")
    capture_ids = list(batch.get("capture_ids") or [])
    results = batch.get("results") or {}
    if set(results) != set(capture_ids):
        raise PersonalizationCorpusError(
            f"legacy compatibility batch result set is incomplete: {batch_id}"
        )
    normal_receipts: dict[str, Path] | None = None
    bindings: list[dict[str, Any]] = []
    for capture_id in capture_ids:
        result = results[capture_id]
        outcome = str(result.get("outcome") or "")
        if outcome not in SUCCESS_OUTCOMES:
            raise PersonalizationCorpusError(
                f"legacy compatibility accepts COMPLETE success results only: "
                f"{batch_id}/{capture_id}"
            )
        capture_row = state.get("captures", {}).get(capture_id)
        result_event = result_events.get((batch_id, capture_id))
        if not isinstance(capture_row, dict) or not isinstance(result_event, dict):
            raise PersonalizationCorpusError(
                f"legacy compatibility result evidence is missing: "
                f"{batch_id}/{capture_id}"
            )
        _validate_success_chain(
            batch=batch,
            capture_id=capture_id,
            capture_row=capture_row,
            result=result,
            result_event=result_event,
        )
        formal_id = str(result.get("formal_id") or "")
        row = formal_rows.get(formal_id)
        if not FORMAL_ID_RE.fullmatch(formal_id) or row is None:
            raise PersonalizationCorpusError(
                f"legacy compatibility formal identity is invalid: "
                f"{batch_id}/{capture_id}"
            )
        _, safe_card_sha = _load_safe_card(repo, formal_id)
        formal_row_sha = sha256_value(row)
        completion_field = (
            "receipt_sha256" if outcome == "curated" else "verification_sha256"
        )
        completion_sha = result.get(completion_field)
        if not isinstance(completion_sha, str) or not SHA256_RE.fullmatch(
            completion_sha
        ):
            raise PersonalizationCorpusError(
                f"legacy compatibility completion evidence is invalid: "
                f"{batch_id}/{capture_id}"
            )
        if outcome == "curated":
            if normal_receipts is None:
                normal_receipts = _receipt_index(repo, runtime_root)
            completion_entity = _validate_normal_receipt(
                repo=repo,
                runtime_root=runtime_root,
                receipt_index=normal_receipts,
                expected_sha256=completion_sha,
                formal_id=formal_id,
                capture_row=capture_row,
            )
        else:
            verification_material = _already_current_material(
                repo=repo,
                batch_id=batch_id,
                capture_id=capture_id,
                study_date=str(batch.get("study_date") or ""),
                formal_id=formal_id,
                capture_payload_sha256=str(capture_row.get("payload_sha256") or ""),
                formal_row_sha256=formal_row_sha,
                safe_card_sha256=safe_card_sha,
            )
            completion_entity = _validate_already_current_verification(
                repo=repo,
                runtime_root=runtime_root,
                expected_sha256=completion_sha,
                expected_material=verification_material,
            )
        bindings.append(
            {
                "capture_id": capture_id,
                "capture_payload_sha256": str(
                    capture_row.get("payload_sha256") or ""
                ),
                "outcome": outcome,
                "formal_id": formal_id,
                "result_sha256": str(result_event.get("result_sha256") or ""),
                "result_event_sha256": sha256_value(result_event),
                "completion_evidence_kind": completion_field,
                "completion_evidence_sha256": completion_sha,
                "completion_entity": completion_entity,
                "formal_row_sha256": formal_row_sha,
                "safe_card_sha256": safe_card_sha,
            }
        )
    return bindings


def _legacy_compatibility_receipt_root(repo: Path, *, create: bool) -> Path:
    root = capture_ledger.capture_root(repo) / LEGACY_COMPATIBILITY_RECEIPT_DIR
    if create:
        root.mkdir(mode=0o700, parents=True, exist_ok=True)
    try:
        resolved = root.resolve(strict=True)
        resolved.relative_to(repo)
    except (OSError, ValueError) as exc:
        raise PersonalizationCorpusError(
            "legacy compatibility receipt root is missing or escapes repository"
        ) from exc
    if resolved != root.absolute() or root.is_symlink() or not root.is_dir():
        raise PersonalizationCorpusError(
            "legacy compatibility receipt root is unsafe"
        )
    return root


def _atomic_write_private(path: Path, content: bytes) -> None:
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(
        prefix=f".{path.name}.", dir=str(path.parent)
    )
    temp_path = Path(temporary)
    try:
        os.fchmod(descriptor, 0o600)
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp_path, path)
        directory_fd = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
    finally:
        if temp_path.exists():
            temp_path.unlink()


def _legacy_compatibility_unsigned(
    *,
    repo: Path,
    batch: dict[str, Any],
    close_event: dict[str, Any],
    completion_bindings: list[dict[str, Any]],
    audit_tools: list[dict[str, Any]],
    audited_objects: list[dict[str, Any]],
    audit_report: dict[str, Any],
) -> dict[str, Any]:
    batch_snapshot = capture_ledger._global_audit_batch_snapshot(batch)
    close_binding = _legacy_close_binding(close_event)
    command = capture_ledger._global_audit_command(repo)
    return {
        "schema": LEGACY_COMPATIBILITY_SCHEMA,
        "kind": LEGACY_COMPATIBILITY_KIND,
        "compatibility_version": LEGACY_COMPATIBILITY_VERSION,
        "audit_status": "PASS",
        "answer_safe": True,
        "formal_write_count": 0,
        "repo_root": str(repo),
        "batch_snapshot": batch_snapshot,
        "batch_snapshot_sha256": sha256_value(batch_snapshot),
        "legacy_close_binding": close_binding,
        "legacy_close_binding_sha256": sha256_value(close_binding),
        "completion_bindings": completion_bindings,
        "completion_bindings_sha256": sha256_value(completion_bindings),
        "audit_command": command,
        "audit_command_sha256": sha256_value(command),
        "audit_tool_commitments": audit_tools,
        "audit_tool_commitments_sha256": sha256_value(audit_tools),
        "audited_object_commitments": audited_objects,
        "audited_object_commitments_sha256": sha256_value(audited_objects),
        "audit_report": audit_report,
        "audit_report_sha256": sha256_value(audit_report),
    }


def _legacy_compatibility_key(repo: Path, *, create: bool) -> bytes:
    try:
        return capture_ledger._global_audit_key(
            capture_ledger.capture_root(repo), create=create
        )
    except capture_ledger.CaptureError as exc:
        raise PersonalizationCorpusError(str(exc)) from exc


def _validate_legacy_compatibility_receipt(
    *,
    repo: Path,
    runtime_root: Path,
    expected_sha256: str,
    batch: dict[str, Any],
    close_event: dict[str, Any],
    state: dict[str, Any],
    result_events: dict[tuple[str, str], dict[str, Any]],
    formal_rows: dict[str, dict[str, str]],
) -> dict[str, Any]:
    if not SHA256_RE.fullmatch(expected_sha256):
        raise PersonalizationCorpusError(
            "legacy compatibility receipt SHA-256 is invalid"
        )
    _validate_batch_close_chain(
        repo=repo,
        batch=batch,
        close_event=close_event,
        require_current_objects=False,
    )
    if (
        batch.get("status") != "COMPLETE"
        or batch.get("global_closeout") != "pass"
        or close_event.get("close_contract") is not None
        or close_event.get("global_audit_receipt_sha256") is not None
    ):
        raise PersonalizationCorpusError(
            "legacy compatibility receipt requires a legacy_unsealed COMPLETE batch"
        )
    root = _legacy_compatibility_receipt_root(repo, create=False)
    path = _bounded_regular_file(
        root / f"{expected_sha256}.json",
        root,
        "legacy compatibility receipt",
    )
    mode = path.stat().st_mode
    if not stat.S_ISREG(mode) or stat.S_IMODE(mode) != 0o600:
        raise PersonalizationCorpusError(
            "legacy compatibility receipt must be a 0600 regular file"
        )
    raw = path.read_bytes()
    if sha256_bytes(raw) != expected_sha256:
        raise PersonalizationCorpusError(
            "legacy compatibility receipt content hash mismatch"
        )
    try:
        receipt = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise PersonalizationCorpusError(
            "legacy compatibility receipt is invalid JSON"
        ) from exc
    if not isinstance(receipt, dict) or raw != canonical_json_bytes(receipt):
        raise PersonalizationCorpusError(
            "legacy compatibility receipt is not canonical content-addressed JSON"
        )
    attestation = receipt.get("attestation")
    if not isinstance(attestation, dict):
        raise PersonalizationCorpusError(
            "legacy compatibility receipt lacks attestation"
        )
    unsigned = {key: value for key, value in receipt.items() if key != "attestation"}
    key = _legacy_compatibility_key(repo, create=False)
    signature = hmac.new(key, canonical_json_bytes(unsigned), hashlib.sha256).hexdigest()
    if (
        attestation.get("algorithm") != "hmac-sha256"
        or attestation.get("key_id") != hashlib.sha256(key).hexdigest()
        or not hmac.compare_digest(
            str(attestation.get("signature") or ""), signature
        )
    ):
        raise PersonalizationCorpusError(
            "legacy compatibility receipt attestation is invalid"
        )
    if (
        receipt.get("schema") != LEGACY_COMPATIBILITY_SCHEMA
        or receipt.get("kind") != LEGACY_COMPATIBILITY_KIND
        or receipt.get("compatibility_version") != LEGACY_COMPATIBILITY_VERSION
        or receipt.get("audit_status") != "PASS"
        or receipt.get("answer_safe") is not True
        or receipt.get("formal_write_count") != 0
        or receipt.get("repo_root") != str(repo)
    ):
        raise PersonalizationCorpusError(
            "legacy compatibility receipt identity or PASS contract is invalid"
        )
    snapshot = capture_ledger._global_audit_batch_snapshot(batch)
    close_binding = _legacy_close_binding(close_event)
    completion_bindings = _legacy_completion_bindings(
        repo=repo,
        runtime_root=runtime_root,
        batch=batch,
        state=state,
        result_events=result_events,
        formal_rows=formal_rows,
    )
    command = capture_ledger._global_audit_command(repo)
    try:
        report = capture_ledger._normalize_global_audit_report(
            receipt.get("audit_report")
        )
        expected_tools = capture_ledger._audit_file_commitments(
            repo,
            list(capture_ledger.GLOBAL_AUDIT_TOOL_RELS),
            "legacy compatibility audit tool",
        )
        expected_objects = capture_ledger._audit_file_commitments(
            repo,
            capture_ledger._global_audit_object_paths(batch),
            "legacy compatibility audited object",
        )
    except capture_ledger.CaptureError as exc:
        raise PersonalizationCorpusError(str(exc)) from exc
    tools = receipt.get("audit_tool_commitments")
    objects = receipt.get("audited_object_commitments")
    commitments = (
        ("batch_snapshot", snapshot),
        ("legacy_close_binding", close_binding),
        ("completion_bindings", completion_bindings),
        ("audit_command", command),
        ("audit_tool_commitments", expected_tools),
        ("audited_object_commitments", expected_objects),
        ("audit_report", report),
    )
    for field, expected in commitments:
        if (
            receipt.get(field) != expected
            or receipt.get(f"{field}_sha256") != sha256_value(expected)
        ):
            raise PersonalizationCorpusError(
                f"legacy compatibility receipt {field} binding is invalid"
            )
    if tools != expected_tools or objects != expected_objects:
        raise PersonalizationCorpusError(
            "legacy compatibility audited tools or formal objects have drifted"
        )
    return receipt


def _legacy_compatibility_receipt_index(
    *,
    repo: Path,
    runtime_root: Path,
    state: dict[str, Any],
    close_events: dict[str, dict[str, Any]],
    result_events: dict[tuple[str, str], dict[str, Any]],
    formal_rows: dict[str, dict[str, str]],
) -> dict[str, dict[str, Any]]:
    raw_root = capture_ledger.capture_root(repo) / LEGACY_COMPATIBILITY_RECEIPT_DIR
    if not raw_root.exists():
        return {}
    root = _legacy_compatibility_receipt_root(repo, create=False)
    indexed: dict[str, dict[str, Any]] = {}
    for path in sorted(root.glob("*.json")):
        if not re.fullmatch(r"[0-9a-f]{64}\.json", path.name):
            raise PersonalizationCorpusError(
                "legacy compatibility receipt filename is not content-addressed"
            )
        path = _bounded_regular_file(
            path, root, "legacy compatibility receipt"
        )
        if stat.S_IMODE(path.stat().st_mode) != 0o600:
            raise PersonalizationCorpusError(
                "legacy compatibility receipt must be a 0600 regular file"
            )
        digest = path.stem
        preview = _load_json_object(path, "legacy compatibility receipt")
        batch_id = str(
            (preview.get("batch_snapshot") or {}).get("batch_id")
            if isinstance(preview.get("batch_snapshot"), dict)
            else ""
        )
        batch = state.get("batches", {}).get(batch_id)
        close_event = close_events.get(batch_id)
        if not isinstance(batch, dict) or not isinstance(close_event, dict):
            raise PersonalizationCorpusError(
                "legacy compatibility receipt references an unknown batch"
            )
        receipt = _validate_legacy_compatibility_receipt(
            repo=repo,
            runtime_root=runtime_root,
            expected_sha256=digest,
            batch=batch,
            close_event=close_event,
            state=state,
            result_events=result_events,
            formal_rows=formal_rows,
        )
        if batch_id in indexed:
            raise PersonalizationCorpusError(
                f"duplicate legacy compatibility receipts for batch: {batch_id}"
            )
        indexed[batch_id] = {
            "receipt_sha256": digest,
            "receipt": receipt,
        }
    return indexed


def seal_legacy_complete(
    repo_root: str | Path,
    *,
    batch_id: str,
    runtime_root: str | Path = DEFAULT_RUNTIME_ROOT,
) -> dict[str, Any]:
    _retired_write_surface()
    repo = Path(repo_root).expanduser().resolve(strict=True)
    runtime = Path(runtime_root).expanduser().resolve()
    try:
        capture_ledger.audit(repo_root=repo)
    except capture_ledger.CaptureError as exc:
        raise PersonalizationCorpusError(str(exc)) from exc
    root = capture_ledger.capture_root(repo)
    with capture_ledger._ledger_lock(root):
        events = _load_capture_events(repo)
        state = capture_ledger.replay(events)
        result_events, close_events = _result_event_map(events)
        formal_rows = _load_master_rows(repo)
        batch = state.get("batches", {}).get(batch_id)
        close_event = close_events.get(batch_id)
        if not isinstance(batch, dict) or not isinstance(close_event, dict):
            raise PersonalizationCorpusError(
                f"unknown closed legacy batch: {batch_id}"
            )
        _validate_batch_close_chain(
            repo=repo,
            batch=batch,
            close_event=close_event,
            require_current_objects=False,
        )
        if (
            batch.get("status") != "COMPLETE"
            or batch.get("global_closeout") != "pass"
            or close_event.get("close_contract") is not None
            or close_event.get("global_audit_receipt_sha256") is not None
        ):
            raise PersonalizationCorpusError(
                "seal-legacy-complete accepts legacy_unsealed COMPLETE batches only"
            )
        existing = _legacy_compatibility_receipt_index(
            repo=repo,
            runtime_root=runtime,
            state=state,
            close_events=close_events,
            result_events=result_events,
            formal_rows=formal_rows,
        ).get(batch_id)
        if existing:
            return {
                "status": "ALREADY_SEALED",
                "schema": LEGACY_COMPATIBILITY_SCHEMA,
                "batch_id": batch_id,
                "compatibility_receipt_sha256": existing["receipt_sha256"],
                "receipt_path": str(
                    _legacy_compatibility_receipt_root(repo, create=False)
                    / f"{existing['receipt_sha256']}.json"
                ),
                "formal_write_count": 0,
            }
        completion_before = _legacy_completion_bindings(
            repo=repo,
            runtime_root=runtime,
            batch=batch,
            state=state,
            result_events=result_events,
            formal_rows=formal_rows,
        )
        try:
            tool_before = capture_ledger._audit_file_commitments(
                repo,
                list(capture_ledger.GLOBAL_AUDIT_TOOL_RELS),
                "legacy compatibility audit tool",
            )
            object_before = capture_ledger._audit_file_commitments(
                repo,
                capture_ledger._global_audit_object_paths(batch),
                "legacy compatibility audited object",
            )
            report = capture_ledger._normalize_global_audit_report(
                capture_ledger._execute_global_audit(repo)
            )
        except capture_ledger.CaptureError as exc:
            raise PersonalizationCorpusError(str(exc)) from exc
        formal_rows_after = _load_master_rows(repo)
        completion_after = _legacy_completion_bindings(
            repo=repo,
            runtime_root=runtime,
            batch=batch,
            state=state,
            result_events=result_events,
            formal_rows=formal_rows_after,
        )
        try:
            tool_after = capture_ledger._audit_file_commitments(
                repo,
                list(capture_ledger.GLOBAL_AUDIT_TOOL_RELS),
                "legacy compatibility audit tool",
            )
            object_after = capture_ledger._audit_file_commitments(
                repo,
                capture_ledger._global_audit_object_paths(batch),
                "legacy compatibility audited object",
            )
        except capture_ledger.CaptureError as exc:
            raise PersonalizationCorpusError(str(exc)) from exc
        if (
            completion_before != completion_after
            or tool_before != tool_after
            or object_before != object_after
        ):
            raise PersonalizationCorpusError(
                "legacy compatibility evidence changed during independent audit"
            )
        unsigned = _legacy_compatibility_unsigned(
            repo=repo,
            batch=batch,
            close_event=close_event,
            completion_bindings=completion_after,
            audit_tools=tool_after,
            audited_objects=object_after,
            audit_report=report,
        )
        key = _legacy_compatibility_key(repo, create=True)
        receipt = {
            **unsigned,
            "attestation": {
                "algorithm": "hmac-sha256",
                "key_id": hashlib.sha256(key).hexdigest(),
                "signature": hmac.new(
                    key, canonical_json_bytes(unsigned), hashlib.sha256
                ).hexdigest(),
            },
        }
        receipt_bytes = canonical_json_bytes(receipt)
        digest = sha256_bytes(receipt_bytes)
        receipt_root = _legacy_compatibility_receipt_root(repo, create=True)
        path = receipt_root / f"{digest}.json"
        if path.exists():
            if (
                path.is_symlink()
                or not path.is_file()
                or stat.S_IMODE(path.stat().st_mode) != 0o600
                or path.read_bytes() != receipt_bytes
            ):
                raise PersonalizationCorpusError(
                    "legacy compatibility receipt content-address collision"
                )
        else:
            _atomic_write_private(path, receipt_bytes)
        _validate_legacy_compatibility_receipt(
            repo=repo,
            runtime_root=runtime,
            expected_sha256=digest,
            batch=batch,
            close_event=close_event,
            state=state,
            result_events=result_events,
            formal_rows=formal_rows_after,
        )
    return {
        "status": "SEALED",
        "schema": LEGACY_COMPATIBILITY_SCHEMA,
        "batch_id": batch_id,
        "compatibility_receipt_sha256": digest,
        "receipt_path": str(path),
        "formal_write_count": 0,
    }


def _eligible_partial_batch(batch: dict[str, Any]) -> bool:
    """Allow audited needs-user gaps without admitting failed formal work."""
    if (
        batch.get("status") != "PARTIAL"
        or batch.get("global_closeout") != "pass"
    ):
        return False
    results = batch.get("results")
    if not isinstance(results, dict) or not results:
        return False
    capture_ids = batch.get("capture_ids")
    if (
        not isinstance(capture_ids, list)
        or not capture_ids
        or len(capture_ids) != len(set(capture_ids))
        or set(results) != set(capture_ids)
    ):
        return False
    if not all(isinstance(result, dict) for result in results.values()):
        return False
    outcomes = {
        str(result.get("outcome") or "")
        for result in results.values()
    }
    return (
        outcomes <= (SUCCESS_OUTCOMES | {"needs_user"})
        and bool(outcomes & SUCCESS_OUTCOMES)
        and "needs_user" in outcomes
    )


def _latest_publishable_batches(
    batches: list[dict[str, Any]],
) -> tuple[list[dict[str, Any]], list[str]]:
    """Keep only the newest safe attempt for each immutable capture set."""
    winners: dict[tuple[str, str], dict[str, Any]] = {}
    superseded: list[str] = []
    for batch in batches:
        lineage = (
            str(batch.get("study_date") or ""),
            str(batch.get("capture_set_sha256") or ""),
        )
        previous = winners.get(lineage)
        if previous is None:
            winners[lineage] = batch
            continue
        previous_rank = (
            int(previous.get("attempt", 1)),
            str(previous.get("batch_id") or ""),
        )
        current_rank = (
            int(batch.get("attempt", 1)),
            str(batch.get("batch_id") or ""),
        )
        if current_rank > previous_rank:
            superseded.append(str(previous.get("batch_id") or ""))
            winners[lineage] = batch
        else:
            superseded.append(str(batch.get("batch_id") or ""))
    selected = sorted(
        winners.values(),
        key=lambda batch: (
            str(batch.get("study_date") or ""),
            int(batch.get("attempt", 1)),
            str(batch.get("batch_id") or ""),
        ),
    )
    return selected, sorted(batch_id for batch_id in superseded if batch_id)


def compile_corpus(
    repo_root: str | Path,
    *,
    require_batch_id: str | None = None,
    runtime_root: str | Path = DEFAULT_RUNTIME_ROOT,
) -> tuple[dict[str, bytes], dict[str, Any]]:
    repo = Path(repo_root).expanduser().resolve()
    runtime = Path(runtime_root).expanduser().resolve()
    if not repo.is_dir():
        raise PersonalizationCorpusError(f"repo does not exist: {repo}")
    try:
        capture_ledger.audit(repo_root=repo)
    except capture_ledger.CaptureError as exc:
        raise PersonalizationCorpusError(str(exc)) from exc
    capture_events = _load_capture_events(repo)
    state = capture_ledger.replay(capture_events)
    result_events, close_events = _result_event_map(capture_events)
    review_events = _load_review_events(repo)
    formal_rows = _load_master_rows(repo)
    compatibility_receipts = _legacy_compatibility_receipt_index(
        repo=repo,
        runtime_root=runtime,
        state=state,
        close_events=close_events,
        result_events=result_events,
        formal_rows=formal_rows,
    )

    def completion_gate(batch_id: str) -> dict[str, str] | None:
        close_event = close_events.get(batch_id)
        if (
            isinstance(close_event, dict)
            and close_event.get("close_contract")
            == capture_ledger.GLOBAL_AUDIT_CLOSE_CONTRACT
            and SHA256_RE.fullmatch(
                str(close_event.get("global_audit_receipt_sha256") or "")
            )
        ):
            return {
                "kind": "aggregate_global_audit_receipt_v3",
                "entity_sha256": str(
                    close_event["global_audit_receipt_sha256"]
                ),
            }
        sealed = compatibility_receipts.get(batch_id)
        if sealed:
            return {
                "kind": LEGACY_COMPATIBILITY_KIND,
                "entity_sha256": str(sealed["receipt_sha256"]),
            }
        return None

    publishable_candidates = [
        batch
        for batch_id, batch in state["batches"].items()
        if (
            batch.get("status") == "COMPLETE"
            or (
                _eligible_partial_batch(batch)
                and (completion_gate(batch_id) or {}).get("kind")
                == "aggregate_global_audit_receipt_v3"
            )
        )
        and batch.get("global_closeout") == "pass"
        and completion_gate(batch_id) is not None
    ]
    selected_batches, superseded_batch_ids = _latest_publishable_batches(
        publishable_candidates
    )
    selected_batch_ids = {
        str(batch.get("batch_id") or "") for batch in selected_batches
    }

    if require_batch_id:
        required = state["batches"].get(require_batch_id)
        if not required:
            raise PersonalizationCorpusError(f"unknown required batch: {require_batch_id}")
        if required.get("status") == "active":
            raise PersonalizationCorpusError(
                f"required batch is still active: {require_batch_id}"
            )
        if (
            required.get("status") != "COMPLETE"
            and not _eligible_partial_batch(required)
        ):
            raise PersonalizationCorpusError(
                f"required batch is not complete: {require_batch_id}"
            )
        if required.get("global_closeout") != "pass":
            raise PersonalizationCorpusError(
                f"required batch did not pass global closeout: {require_batch_id}"
            )
        required_gate = completion_gate(require_batch_id)
        if required_gate is None:
            raise PersonalizationCorpusError(
                f"required batch lacks a sealed aggregate audit receipt or valid "
                f"legacy compatibility seal: {require_batch_id}"
            )
        if (
            _eligible_partial_batch(required)
            and required_gate.get("kind") != "aggregate_global_audit_receipt_v3"
        ):
            raise PersonalizationCorpusError(
                f"required partial batch lacks an aggregate v3 audit receipt: "
                f"{require_batch_id}"
            )
        if require_batch_id not in selected_batch_ids:
            if require_batch_id in superseded_batch_ids:
                raise PersonalizationCorpusError(
                    f"required batch was superseded by a newer safe attempt: "
                    f"{require_batch_id}"
                )
            raise PersonalizationCorpusError(
                f"required batch is not publishable: {require_batch_id}"
            )
    excluded = {
        "active_batch_ids": sorted(
            batch_id
            for batch_id, batch in state["batches"].items()
            if batch.get("status") == "active"
        ),
        "failed_closeout_batch_ids": sorted(
            batch_id
            for batch_id, batch in state["batches"].items()
            if batch.get("status") != "active"
            and batch.get("global_closeout") != "pass"
        ),
        "partial_batch_ids": sorted(
            batch_id
            for batch_id, batch in state["batches"].items()
            if batch.get("status") == "PARTIAL"
            and not _eligible_partial_batch(batch)
        ),
        "partial_batches": sorted(
            (
                {
                    "batch_id": batch_id,
                    "needs_user_capture_ids": sorted(
                        capture_id
                        for capture_id, result in batch.get("results", {}).items()
                        if result.get("outcome") == "needs_user"
                    ),
                    "failed_capture_ids": sorted(
                        capture_id
                        for capture_id, result in batch.get("results", {}).items()
                        if result.get("outcome") == "failed"
                    ),
                }
                for batch_id, batch in state["batches"].items()
                if batch.get("status") == "PARTIAL"
                and not _eligible_partial_batch(batch)
            ),
            key=lambda row: row["batch_id"],
        ),
        "unsealed_complete_batch_ids": sorted(
            batch_id
            for batch_id, batch in state["batches"].items()
            if batch.get("status") == "COMPLETE"
            and batch.get("global_closeout") == "pass"
            and completion_gate(batch_id) is None
        ),
        "superseded_batch_ids": superseded_batch_ids,
    }

    artifacts: dict[str, bytes] = {}
    index_entries: list[dict[str, Any]] = []
    batch_entries: list[dict[str, Any]] = []
    formal_row_hashes: dict[str, str] = {}
    safe_card_hashes: dict[str, str] = {}
    completion_projection: list[dict[str, Any]] = []
    outcome_binding_counts: dict[str, int] = defaultdict(int)
    normal_receipts: dict[str, Path] | None = None

    for batch in selected_batches:
        batch_id = str(batch["batch_id"])
        close_event = close_events.get(batch_id)
        if not close_event:
            raise PersonalizationCorpusError(f"closed batch lacks close event: {batch_id}")
        gate = completion_gate(batch_id)
        if gate is None:
            raise PersonalizationCorpusError(
                f"batch lacks a valid corpus completion gate: {batch_id}"
            )
        _validate_batch_close_chain(
            repo=repo,
            batch=batch,
            close_event=close_event,
            require_current_objects=(
                require_batch_id is not None and batch_id == require_batch_id
            ),
        )
        item_entries: list[dict[str, Any]] = []
        success_projection: list[dict[str, Any]] = []
        gap_projection: list[dict[str, Any]] = []
        outcome_counts: dict[str, int] = defaultdict(int)

        def append_missing_completion_gap(
            *,
            capture_id: str,
            source_outcome: str,
            formal_id: str,
            result_event: dict[str, Any],
            completion_kind: str,
            completion_sha: str,
        ) -> None:
            result_sha = str(result_event.get("result_sha256") or "")
            if not SHA256_RE.fullmatch(result_sha):
                raise PersonalizationCorpusError(
                    f"historical completion gap lacks valid result evidence: "
                    f"{batch_id}/{capture_id}"
                )
            gap_projection.append(
                {
                    "capture_id": capture_id,
                    "outcome": "projection_gap",
                    "source_outcome": source_outcome,
                    "formal_id": formal_id,
                    "reason": "formal completion entity unavailable after runtime migration",
                    "result_sha256": result_sha,
                    "completion_evidence_kind": completion_kind,
                    "completion_evidence_sha256": completion_sha,
                }
            )

        for capture_id in batch["capture_ids"]:
            result = batch["results"][capture_id]
            outcome = str(result["outcome"])
            outcome_counts[outcome] += 1
            result_event = result_events.get((batch_id, capture_id))
            if not result_event:
                raise PersonalizationCorpusError(
                    f"batch result lacks result event: {batch_id}/{capture_id}"
                )
            if outcome not in SUCCESS_OUTCOMES:
                if outcome == "needs_user":
                    result_sha = str(result_event.get("result_sha256") or "")
                    if not SHA256_RE.fullmatch(result_sha):
                        raise PersonalizationCorpusError(
                            f"needs-user result lacks valid evidence hash: "
                            f"{batch_id}/{capture_id}"
                        )
                    gap_projection.append(
                        {
                            "capture_id": capture_id,
                            "outcome": outcome,
                            "reason": str(result.get("reason") or "needs user evidence"),
                            "result_sha256": result_sha,
                        }
                    )
                continue
            capture_row = state["captures"].get(capture_id)
            if not capture_row:
                raise PersonalizationCorpusError(
                    f"successful result references missing capture: {capture_id}"
                )
            _validate_success_chain(
                batch=batch,
                capture_id=capture_id,
                capture_row=capture_row,
                result=result,
                result_event=result_event,
            )
            formal_id = str(result.get("formal_id") or "")
            if not FORMAL_ID_RE.fullmatch(formal_id):
                raise PersonalizationCorpusError(
                    f"successful result has invalid formal id: {batch_id}/{capture_id}"
                )
            row = formal_rows.get(formal_id)
            if row is None:
                raise PersonalizationCorpusError(
                    f"successful result references missing formal node: {formal_id}"
                )
            safe_card, safe_card_hash = _load_safe_card(repo, formal_id)
            row_hash = sha256_value(row)
            completion_kind = (
                "receipt_sha256" if outcome == "curated" else "verification_sha256"
            )
            completion_sha = result.get(completion_kind)
            if not isinstance(completion_sha, str) or not SHA256_RE.fullmatch(completion_sha):
                raise PersonalizationCorpusError(
                    f"successful result lacks completion evidence: {batch_id}/{capture_id}"
                )
            if outcome == "curated":
                if normal_receipts is None:
                    normal_receipts = _receipt_index(repo, runtime)
                if completion_sha not in normal_receipts:
                    if batch_id == require_batch_id:
                        raise PersonalizationCorpusError(
                            f"curated result receipt entity is missing: {completion_sha}"
                        )
                    append_missing_completion_gap(
                        capture_id=capture_id,
                        source_outcome=outcome,
                        formal_id=formal_id,
                        result_event=result_event,
                        completion_kind=completion_kind,
                        completion_sha=completion_sha,
                    )
                    continue
                completion_entity = _validate_normal_receipt(
                    repo=repo,
                    runtime_root=runtime,
                    receipt_index=normal_receipts,
                    expected_sha256=completion_sha,
                    formal_id=formal_id,
                    capture_row=capture_row,
                )
            else:
                verification_path = (
                    _verification_root(repo, runtime) / f"{completion_sha}.json"
                )
                if not verification_path.exists():
                    if batch_id == require_batch_id:
                        raise PersonalizationCorpusError(
                            f"already_current verification is missing or unsafe: "
                            f"{verification_path}"
                        )
                    append_missing_completion_gap(
                        capture_id=capture_id,
                        source_outcome=outcome,
                        formal_id=formal_id,
                        result_event=result_event,
                        completion_kind=completion_kind,
                        completion_sha=completion_sha,
                    )
                    continue
                verification_material = _already_current_material(
                    repo=repo,
                    batch_id=batch_id,
                    capture_id=capture_id,
                    study_date=str(batch["study_date"]),
                    formal_id=formal_id,
                    capture_payload_sha256=str(capture_row["payload_sha256"]),
                    formal_row_sha256=row_hash,
                    safe_card_sha256=safe_card_hash,
                )
                completion_entity = _validate_already_current_verification(
                    repo=repo,
                    runtime_root=runtime,
                    expected_sha256=completion_sha,
                    expected_material=verification_material,
                )
            formal_row_hashes[formal_id] = row_hash
            safe_card_hashes[formal_id] = safe_card_hash
            canonical_binding = _bind_canonical_outcome(
                capture_row, formal_id, review_events
            )
            outcome_binding_counts[str(canonical_binding["status"])] += 1
            item = {
                "schema": SCHEMA,
                "kind": ITEM_KIND,
                "answer_safe": True,
                "formal_write_count": 0,
                "batch_id": batch_id,
                "capture_id": capture_id,
                "study_date": str(batch["study_date"]),
                "outcome": outcome,
                "formal_id": formal_id,
                "point_ids": _point_ids(row),
                "observed_facts": _semantic_capture_facts(capture_row),
                "canonical_outcome_binding": canonical_binding,
                "formal_snapshot": _formal_snapshot(formal_id, row, safe_card),
                "source_binding": {
                    "capture_payload_sha256": capture_row["payload_sha256"],
                    "curation_result_sha256": result_event["result_sha256"],
                    "completion_evidence_kind": completion_kind,
                    "completion_evidence_sha256": completion_sha,
                    "completion_entity": completion_entity,
                    "batch_completion_gate": gate,
                    "batch_close_sha256": close_event["close_sha256"],
                    "formal_row_sha256": row_hash,
                    "safe_card_sha256": safe_card_hash,
                },
            }
            _assert_safe_payload(item)
            item_bytes = canonical_json_bytes(item)
            item_sha = sha256_bytes(item_bytes)
            object_ref = f"objects/{item_sha}.json"
            artifacts[object_ref] = item_bytes
            semantic_keys = _semantic_index_keys(item)
            entry = {
                "object_ref": object_ref,
                "object_sha256": item_sha,
                "batch_id": batch_id,
                "capture_id": capture_id,
                "study_date": str(batch["study_date"]),
                "outcome": outcome,
                "formal_id": formal_id,
                "point_ids": item["point_ids"],
                "semantic_keys": semantic_keys,
            }
            item_entries.append(entry)
            index_entries.append(entry)
            success_projection.append(
                {
                    "capture_id": capture_id,
                    "outcome": outcome,
                    "formal_id": formal_id,
                    "result_sha256": result_event["result_sha256"],
                    "completion_evidence_kind": completion_kind,
                    "completion_evidence_sha256": completion_sha,
                }
            )
        batch_object = {
            "schema": SCHEMA,
            "kind": BATCH_KIND,
            "answer_safe": True,
            "formal_write_count": 0,
            "batch_id": batch_id,
            "study_date": str(batch["study_date"]),
            "attempt": int(batch.get("attempt", 1)),
            "status": str(batch["status"]),
            "global_closeout": str(batch["global_closeout"]),
            "capture_set_sha256": str(batch["capture_set_sha256"]),
            "batch_close_sha256": str(close_event["close_sha256"]),
            "batch_completion_gate": gate,
            "outcome_counts": dict(sorted(outcome_counts.items())),
            "gaps": sorted(gap_projection, key=lambda row: row["capture_id"]),
            "items": sorted(
                item_entries,
                key=lambda entry: (entry["capture_id"], entry["object_sha256"]),
            ),
        }
        _assert_safe_payload(batch_object)
        batch_bytes = canonical_json_bytes(batch_object)
        batch_sha = sha256_bytes(batch_bytes)
        batch_ref = f"batches/{batch_sha}.json"
        artifacts[batch_ref] = batch_bytes
        batch_entries.append(
            {
                "batch_id": batch_id,
                "study_date": str(batch["study_date"]),
                "status": str(batch["status"]),
                "batch_ref": batch_ref,
                "batch_sha256": batch_sha,
                "item_count": len(item_entries),
                "gap_count": len(gap_projection),
                "batch_completion_gate": gate,
            }
        )
        completion_projection.append(
            {
                "batch_id": batch_id,
                "study_date": str(batch["study_date"]),
                "capture_set_sha256": str(batch["capture_set_sha256"]),
                "batch_close_sha256": str(close_event["close_sha256"]),
                "batch_completion_gate": gate,
                "successful_results": sorted(
                    success_projection, key=lambda row: row["capture_id"]
                ),
                "gaps": sorted(gap_projection, key=lambda row: row["capture_id"]),
            }
        )

    def sort_entries(values: list[dict[str, Any]]) -> list[dict[str, Any]]:
        return sorted(
            values,
            key=lambda entry: (
                entry["study_date"],
                entry["batch_id"],
                entry["capture_id"],
                entry["object_sha256"],
            ),
            reverse=True,
        )

    by_formal: dict[str, list[dict[str, Any]]] = defaultdict(list)
    by_point: dict[str, list[dict[str, Any]]] = defaultdict(list)
    by_date: dict[str, list[dict[str, Any]]] = defaultdict(list)
    by_mechanism: dict[str, list[dict[str, Any]]] = defaultdict(list)
    by_error_pattern: dict[str, list[dict[str, Any]]] = defaultdict(list)
    by_method: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for entry in index_entries:
        by_formal[entry["formal_id"]].append(entry)
        by_date[entry["study_date"]].append(entry)
        for point_id in entry["point_ids"]:
            by_point[point_id].append(entry)
        semantic_keys = entry["semantic_keys"]
        for mechanism_id in semantic_keys["mechanism_ids"]:
            by_mechanism[mechanism_id].append(entry)
        for error_pattern_id in semantic_keys["error_pattern_ids"]:
            by_error_pattern[error_pattern_id].append(entry)
        for method_id in semantic_keys["method_ids"]:
            by_method[method_id].append(entry)
    index = {
        "schema": SCHEMA,
        "kind": INDEX_KIND,
        "answer_safe": True,
        "formal_write_count": 0,
        "semantic_index_version": 1,
        "entry_count": len(index_entries),
        "by_formal_id": {
            key: sort_entries(values) for key, values in sorted(by_formal.items())
        },
        "by_point_id": {
            key: sort_entries(values) for key, values in sorted(by_point.items())
        },
        "by_study_date": {
            key: sort_entries(values) for key, values in sorted(by_date.items())
        },
        "by_mechanism_id": {
            key: sort_entries(values)
            for key, values in sorted(by_mechanism.items())
        },
        "by_error_pattern": {
            key: sort_entries(values)
            for key, values in sorted(by_error_pattern.items())
        },
        "by_method_id": {
            key: sort_entries(values) for key, values in sorted(by_method.items())
        },
    }
    _assert_safe_payload(index)
    index_bytes = canonical_json_bytes(index)
    index_sha256 = sha256_bytes(index_bytes)
    index_ref = f"indexes/{index_sha256}.json"
    artifacts[index_ref] = index_bytes
    artifact_hashes = {
        ref: sha256_bytes(content) for ref, content in sorted(artifacts.items())
    }
    source_hashes = {
        "capture_completion_projection": sha256_value(completion_projection),
        "formal_rows": sha256_value(dict(sorted(formal_row_hashes.items()))),
        "safe_cards": sha256_value(dict(sorted(safe_card_hashes.items()))),
        "bound_review_outcomes": sha256_value(
            sorted(
                {
                    entry["object_sha256"]: json.loads(
                        artifacts[entry["object_ref"]]
                    )["canonical_outcome_binding"].get("event_sha256")
                    for entry in index_entries
                }.items()
            )
        ),
    }
    counts = {
        "included_batches": len(selected_batches),
        "included_items": len(index_entries),
        "included_gaps": sum(
            int(batch.get("gap_count", 0)) for batch in batch_entries
        ),
        "formal_ids": len(by_formal),
        "point_ids": len(by_point),
        "study_dates": len(by_date),
        "mechanism_ids": len(by_mechanism),
        "error_pattern_ids": len(by_error_pattern),
        "method_ids": len(by_method),
        "canonical_outcome_bindings": dict(sorted(outcome_binding_counts.items())),
    }
    build_material = {
        "schema": SCHEMA,
        "source_hashes": source_hashes,
        "artifact_hashes": artifact_hashes,
        "counts": counts,
        "batches": batch_entries,
        "excluded": excluded,
    }
    manifest = {
        "schema": SCHEMA,
        "kind": MANIFEST_KIND,
        "build_id": sha256_value(build_material),
        "answer_safe": True,
        "formal_write_count": 0,
        "index_ref": index_ref,
        "source_hashes": source_hashes,
        "artifact_hashes": artifact_hashes,
        "counts": counts,
        "batches": batch_entries,
        "excluded": excluded,
    }
    return artifacts, manifest


@contextmanager
def _corpus_lock(output_dir: Path) -> Iterator[None]:
    output_dir.parent.mkdir(parents=True, exist_ok=True)
    lock_path = output_dir.parent / ".personalization-corpus.lock"
    with lock_path.open("a+", encoding="utf-8") as handle:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def _atomic_write(path: Path, content: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, raw_temp = tempfile.mkstemp(prefix=f".{path.name}.", dir=str(path.parent))
    temp_path = Path(raw_temp)
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp_path, path)
    finally:
        if temp_path.exists():
            temp_path.unlink()


def rebuild_corpus(
    repo_root: str | Path,
    *,
    output_dir: str | Path | None = None,
    require_batch_id: str | None = None,
    runtime_root: str | Path = DEFAULT_RUNTIME_ROOT,
) -> dict[str, Any]:
    _retired_write_surface()
    repo = Path(repo_root).expanduser().resolve()
    target = Path(output_dir).expanduser().resolve() if output_dir else repo / DEFAULT_OUTPUT_RELATIVE
    artifacts, manifest = compile_corpus(
        repo,
        require_batch_id=require_batch_id,
        runtime_root=runtime_root,
    )
    with _corpus_lock(target):
        for ref, content in artifacts.items():
            _atomic_write(target / ref, content)
        _atomic_write(target / "manifest.json", canonical_json_bytes(manifest))
    return {
        "status": "BUILT",
        "schema": SCHEMA,
        "build_id": manifest["build_id"],
        "output_dir": str(target),
        "counts": manifest["counts"],
        "required_batch_id": require_batch_id,
        "formal_write_count": 0,
    }


def _manifest_build_id(manifest: dict[str, Any]) -> str:
    material = {
        "schema": manifest.get("schema"),
        "source_hashes": manifest.get("source_hashes"),
        "artifact_hashes": manifest.get("artifact_hashes"),
        "counts": manifest.get("counts"),
        "batches": manifest.get("batches"),
        "excluded": manifest.get("excluded"),
    }
    return sha256_value(material)


def _manifest_index_ref(manifest: dict[str, Any]) -> str:
    """Resolve a content-addressed index, with legacy fixed-name support."""

    ref = manifest.get("index_ref", "index.json")
    if ref == "index.json":
        return ref
    if not isinstance(ref, str) or not re.fullmatch(
        r"indexes/[0-9a-f]{64}\.json", ref
    ):
        raise PersonalizationCorpusError("corpus index_ref is invalid")
    return ref


def _safe_artifact_path(output_dir: Path, ref: str) -> Path:
    relative = Path(ref)
    if relative.is_absolute() or ".." in relative.parts:
        raise PersonalizationCorpusError(f"artifact ref escapes corpus: {ref}")
    path = output_dir / relative
    if path.is_symlink() or not path.is_file():
        raise PersonalizationCorpusError(f"artifact missing or unsafe: {ref}")
    try:
        path.resolve().relative_to(output_dir.resolve())
    except ValueError as exc:
        raise PersonalizationCorpusError(f"artifact ref escapes corpus: {ref}") from exc
    return path


def _load_manifest(output_dir: str | Path) -> tuple[Path, dict[str, Any]]:
    root = Path(output_dir).expanduser().resolve()
    manifest_path = root / "manifest.json"
    if manifest_path.is_symlink() or not manifest_path.is_file():
        raise PersonalizationCorpusError("corpus manifest is missing or unsafe")
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise PersonalizationCorpusError("corpus manifest is invalid JSON") from exc
    if (
        manifest.get("schema") != SCHEMA
        or manifest.get("kind") != MANIFEST_KIND
        or manifest.get("answer_safe") is not True
        or manifest.get("formal_write_count") != 0
    ):
        raise PersonalizationCorpusError("corpus manifest contract is invalid")
    if manifest.get("build_id") != _manifest_build_id(manifest):
        raise PersonalizationCorpusError("corpus manifest build_id mismatch")
    artifact_hashes = manifest.get("artifact_hashes")
    index_ref = _manifest_index_ref(manifest)
    if not isinstance(artifact_hashes, dict) or index_ref not in artifact_hashes:
        raise PersonalizationCorpusError("corpus artifact hash map is invalid")
    if index_ref != "index.json" and Path(index_ref).stem != artifact_hashes[index_ref]:
        raise PersonalizationCorpusError("corpus content-addressed index mismatch")
    for ref, expected_sha in artifact_hashes.items():
        if not isinstance(ref, str) or not isinstance(expected_sha, str) or not SHA256_RE.fullmatch(expected_sha):
            raise PersonalizationCorpusError("corpus artifact hash entry is invalid")
        relative = Path(ref)
        if relative.is_absolute() or ".." in relative.parts:
            raise PersonalizationCorpusError(f"artifact ref escapes corpus: {ref}")
    return root, manifest


def _load_verified_json_artifact(
    root: Path, manifest: dict[str, Any], ref: str
) -> dict[str, Any]:
    expected_sha = manifest["artifact_hashes"].get(ref)
    if not isinstance(expected_sha, str) or not SHA256_RE.fullmatch(expected_sha):
        raise PersonalizationCorpusError(f"artifact is not manifest-bound: {ref}")
    path = _safe_artifact_path(root, ref)
    if sha256_file(path) != expected_sha:
        raise PersonalizationCorpusError(f"corpus artifact hash mismatch: {ref}")
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise PersonalizationCorpusError(f"corpus artifact is invalid JSON: {ref}") from exc
    if not isinstance(value, dict):
        raise PersonalizationCorpusError(f"corpus artifact is not an object: {ref}")
    return value


def _validate_index_contract(
    index: dict[str, Any], artifact_hashes: dict[str, str]
) -> None:
    if (
        index.get("schema") != SCHEMA
        or index.get("kind") != INDEX_KIND
        or index.get("answer_safe") is not True
        or index.get("formal_write_count") != 0
    ):
        raise PersonalizationCorpusError("corpus index contract is invalid")
    seen_entries = 0
    mapping_names = ["by_formal_id", "by_point_id", "by_study_date"]
    semantic_version = index.get("semantic_index_version")
    if semantic_version is not None:
        if semantic_version != 1:
            raise PersonalizationCorpusError("unsupported corpus semantic index")
        mapping_names.extend(
            ["by_mechanism_id", "by_error_pattern", "by_method_id"]
        )
    semantic_field = {
        "by_mechanism_id": "mechanism_ids",
        "by_error_pattern": "error_pattern_ids",
        "by_method_id": "method_ids",
    }
    for mapping_name in mapping_names:
        mapping = index.get(mapping_name)
        if not isinstance(mapping, dict):
            raise PersonalizationCorpusError(f"corpus index missing {mapping_name}")
        for key, entries in mapping.items():
            if not isinstance(entries, list):
                raise PersonalizationCorpusError("corpus index entries must be lists")
            for entry in entries:
                ref = str(entry.get("object_ref") or "") if isinstance(entry, dict) else ""
                digest = str(entry.get("object_sha256") or "") if isinstance(entry, dict) else ""
                if artifact_hashes.get(ref) != digest or not ref.startswith("objects/"):
                    raise PersonalizationCorpusError("corpus index object binding is invalid")
                semantic_key = semantic_field.get(mapping_name)
                if semantic_key is not None:
                    declared = (
                        (entry.get("semantic_keys") or {}).get(semantic_key, [])
                        if isinstance(entry, dict)
                        else []
                    )
                    if key not in declared or not SEMANTIC_ID_RE.fullmatch(str(key)):
                        raise PersonalizationCorpusError(
                            "corpus semantic index entry is inconsistent"
                        )
                if mapping_name == "by_formal_id":
                    seen_entries += 1
    if seen_entries != index.get("entry_count"):
        raise PersonalizationCorpusError("corpus index entry_count mismatch")


def _valid_batch_completion_gate(value: Any) -> bool:
    return (
        isinstance(value, dict)
        and value.get("kind")
        in {
            "aggregate_global_audit_receipt_v3",
            LEGACY_COMPATIBILITY_KIND,
        }
        and SHA256_RE.fullmatch(str(value.get("entity_sha256") or ""))
        is not None
    )


def _validate_item_contract(item: dict[str, Any], ref: str, digest: str) -> None:
    if (
        item.get("schema") != SCHEMA
        or item.get("kind") != ITEM_KIND
        or item.get("answer_safe") is not True
        or item.get("formal_write_count") != 0
        or Path(ref).stem != digest
    ):
        raise PersonalizationCorpusError(f"corpus item contract is invalid: {ref}")
    source_binding = item.get("source_binding")
    entity = (
        source_binding.get("completion_entity")
        if isinstance(source_binding, dict)
        and isinstance(source_binding.get("completion_entity"), dict)
        else None
    )
    if entity is None:
        raise PersonalizationCorpusError(
            f"corpus item lacks verified completion entity: {ref}"
        )
    entity_sha = entity.get("entity_sha256")
    completion_gate = (
        source_binding.get("batch_completion_gate")
        if isinstance(source_binding, dict)
        and isinstance(source_binding.get("batch_completion_gate"), dict)
        else None
    )
    if (
        entity.get("entity_kind")
        not in {"normal_receipt", ALREADY_CURRENT_KIND}
        or not isinstance(entity_sha, str)
        or not SHA256_RE.fullmatch(entity_sha)
        or entity_sha != source_binding.get("completion_evidence_sha256")
        or entity.get("formal_id") != item.get("formal_id")
    ):
        raise PersonalizationCorpusError(
            f"corpus item completion entity binding is invalid: {ref}"
        )
    if not _valid_batch_completion_gate(completion_gate):
        raise PersonalizationCorpusError(
            f"corpus item batch completion gate is invalid: {ref}"
        )
    _assert_safe_payload(item)


def _validate_batch_contract(batch: dict[str, Any], ref: str, digest: str) -> None:
    if (
        batch.get("schema") != SCHEMA
        or batch.get("kind") != BATCH_KIND
        or batch.get("answer_safe") is not True
        or batch.get("formal_write_count") != 0
        or Path(ref).stem != digest
        or not _valid_batch_completion_gate(batch.get("batch_completion_gate"))
    ):
        raise PersonalizationCorpusError(
            f"corpus batch contract is invalid: {ref}"
        )
    _assert_safe_payload(batch)


def validate_persisted_corpus(output_dir: str | Path) -> dict[str, Any]:
    root, manifest = _load_manifest(output_dir)
    artifact_hashes = manifest["artifact_hashes"]
    for ref in artifact_hashes:
        _load_verified_json_artifact(root, manifest, ref)
    index = _load_verified_json_artifact(
        root, manifest, _manifest_index_ref(manifest)
    )
    _validate_index_contract(index, artifact_hashes)
    for ref in artifact_hashes:
        if not ref.startswith("objects/"):
            continue
        item = _load_verified_json_artifact(root, manifest, ref)
        _validate_item_contract(item, ref, artifact_hashes[ref])
    for ref in artifact_hashes:
        if not ref.startswith("batches/"):
            continue
        batch = _load_verified_json_artifact(root, manifest, ref)
        _validate_batch_contract(batch, ref, artifact_hashes[ref])
    return {"manifest": manifest, "index": index}


def audit_corpus(
    repo_root: str | Path,
    *,
    output_dir: str | Path | None = None,
    runtime_root: str | Path = DEFAULT_RUNTIME_ROOT,
) -> dict[str, Any]:
    repo = Path(repo_root).expanduser().resolve()
    target = Path(output_dir).expanduser().resolve() if output_dir else repo / DEFAULT_OUTPUT_RELATIVE
    persisted = validate_persisted_corpus(target)
    expected_artifacts, expected_manifest = compile_corpus(
        repo, runtime_root=runtime_root
    )
    if persisted["manifest"] != expected_manifest:
        raise PersonalizationCorpusError("corpus is stale relative to current truth sources")
    for ref, content in expected_artifacts.items():
        if sha256_file(_safe_artifact_path(target, ref)) != sha256_bytes(content):
            raise PersonalizationCorpusError(f"corpus artifact differs from rebuild: {ref}")
    return {
        "status": "PASS",
        "schema": SCHEMA,
        "build_id": expected_manifest["build_id"],
        "counts": expected_manifest["counts"],
        "formal_write_count": 0,
    }


def query_corpus(
    output_dir: str | Path,
    *,
    formal_id: str | None = None,
    point_id: str | None = None,
    study_date: str | None = None,
    limit: int = 3,
) -> dict[str, Any]:
    selectors = [formal_id is not None, point_id is not None, study_date is not None]
    if sum(selectors) != 1:
        raise PersonalizationCorpusError("query requires exactly one selector")
    if not isinstance(limit, int) or isinstance(limit, bool) or not 1 <= limit <= 8:
        raise PersonalizationCorpusError("query limit must be between 1 and 8")
    if formal_id is not None and not FORMAL_ID_TEXT_RE.fullmatch(formal_id):
        raise PersonalizationCorpusError("invalid formal_id")
    if point_id is not None and not POINT_RE.fullmatch(point_id):
        raise PersonalizationCorpusError("invalid point_id")
    if study_date is not None and not DATE_RE.fullmatch(study_date):
        raise PersonalizationCorpusError("invalid study_date")
    root, manifest = _load_manifest(output_dir)
    index = _load_verified_json_artifact(
        root, manifest, _manifest_index_ref(manifest)
    )
    _validate_index_contract(index, manifest["artifact_hashes"])
    if formal_id is not None:
        mapping_name, key = "by_formal_id", formal_id
    elif point_id is not None:
        mapping_name, key = "by_point_id", point_id
    else:
        mapping_name, key = "by_study_date", str(study_date)
    entries = list(index[mapping_name].get(key, []))[:limit]
    records: list[dict[str, Any]] = []
    for entry in entries:
        ref = entry["object_ref"]
        item = _load_verified_json_artifact(root, manifest, ref)
        _validate_item_contract(item, ref, entry["object_sha256"])
        records.append(item)
    return {
        "status": "READY" if records else "NO_MATCH",
        "schema": SCHEMA,
        "build_id": manifest["build_id"],
        "selector": {"kind": mapping_name, "value": key},
        "record_count": len(records),
        "records": records,
        "formal_write_count": 0,
    }


def _print(value: dict[str, Any]) -> None:
    print(json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="408 日终正式结果个性化语料投影")
    parser.add_argument("--repo", default=".")
    parser.add_argument("--output-dir")
    parser.add_argument("--runtime-root", default=str(DEFAULT_RUNTIME_ROOT))
    subparsers = parser.add_subparsers(dest="command", required=True)
    rebuild = subparsers.add_parser("rebuild")
    rebuild.add_argument("--require-batch-id")
    subparsers.add_parser("audit")
    seal = subparsers.add_parser("seal-already-current")
    seal.add_argument("--batch-id", required=True)
    seal.add_argument("--capture-id", required=True)
    seal.add_argument("--formal-id", required=True)
    legacy_seal = subparsers.add_parser("seal-legacy-complete")
    legacy_seal.add_argument("--batch-id", required=True)
    query = subparsers.add_parser("query")
    selectors = query.add_mutually_exclusive_group(required=True)
    selectors.add_argument("--formal-id")
    selectors.add_argument("--point-id")
    selectors.add_argument("--study-date")
    query.add_argument("--limit", type=int, default=3)
    args = parser.parse_args(argv)
    repo = Path(args.repo)
    output = Path(args.output_dir) if args.output_dir else repo / DEFAULT_OUTPUT_RELATIVE
    try:
        if args.command == "rebuild":
            result = rebuild_corpus(
                repo,
                output_dir=output,
                require_batch_id=args.require_batch_id,
                runtime_root=args.runtime_root,
            )
        elif args.command == "audit":
            result = audit_corpus(
                repo, output_dir=output, runtime_root=args.runtime_root
            )
        elif args.command == "seal-already-current":
            result = seal_already_current_verification(
                repo,
                batch_id=args.batch_id,
                capture_id=args.capture_id,
                formal_id=args.formal_id,
                runtime_root=args.runtime_root,
            )
        elif args.command == "seal-legacy-complete":
            result = seal_legacy_complete(
                repo,
                batch_id=args.batch_id,
                runtime_root=args.runtime_root,
            )
        else:
            result = query_corpus(
                output,
                formal_id=args.formal_id,
                point_id=args.point_id,
                study_date=args.study_date,
                limit=args.limit,
            )
    except (PersonalizationCorpusError, capture_ledger.CaptureError, OSError) as exc:
        _print({"status": "FAILED", "schema": SCHEMA, "error": str(exc)})
        return 1
    _print(result)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
