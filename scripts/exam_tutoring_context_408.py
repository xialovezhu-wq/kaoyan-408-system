#!/usr/bin/env python3
"""Build and query the bounded, answer-safe 408 exam tutoring context.

The cold ``refresh-nightly`` path reads the formal repository and the complete
append-only review ledger.  It materializes small content-addressed profiles,
exact lookup indexes, and a refresh receipt before atomically replacing the
fixed manifest.  The hot ``query`` path never scans the formal repository or
the review ledger: it reads only the manifest, key-selected hash buckets,
point summaries, at most three profiles, and bounded source drift guards.

This module is deliberately local, deterministic, model-free, and independent
of the retired personalization pipeline.
"""

from __future__ import annotations

import argparse
import copy
import datetime as dt
import fcntl
import hashlib
import json
import os
import re
import stat
import sys
import tempfile
import time
import unicodedata
from collections import Counter, defaultdict
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Dict, Iterable, Iterator, List, Mapping, Optional, Sequence, Set, Tuple, Union

from knowledge_learning_records_408 import build_knowledge_memory, effective_review_records
from knowledge_history_sources_408 import collect_history_sources
from knowledge_tutoring_query_408 import (
    BUILD_VERSION, MAX_POINT_INDEX_BYTES, MAX_POINT_SUMMARY_BYTES,
    build_artifacts, compact_summaries, project_points, validate_index,
)
from knowledge_hot_routes_408 import (
    MAX_BUCKET_BYTES as MAX_HOT_ROUTE_BUCKET_BYTES,
    build_routes,
    resolve as resolve_hot_routes,
)

from intake_lib_408 import (
    FORMAL_ID_RE,
    MASTER_FILE,
    MASTER_COLS,
    REVIEW_UNIT_MAPPING_COLS,
    REVIEW_UNIT_MAPPING_FILE,
    Repo,
    iter_table_rows,
    read_text,
    scan_leak,
    split_multi,
)


PROFILE_SCHEMA = "exam_tutoring_profile_v1"
PRE_ANSWER_PROFILE_SCHEMA = "exam_tutoring_profile_pre_answer_route_v1"
INDEX_SCHEMA = "exam_tutoring_context_index_v1"
KNOWLEDGE_INDEX_SCHEMA = "exam_tutoring_context_knowledge_index_v1"
MANIFEST_SCHEMA = "exam_tutoring_context_manifest_v1"
RECEIPT_SCHEMA = "exam_tutoring_context_refresh_receipt_v1"
HISTORY_SOURCES_SCHEMA = "exam_tutoring_context_history_sources_v1"
HISTORY_SOURCES_DIR = "objects/history-sources"
QUERY_RESULT_SCHEMA = "exam_tutoring_context_slice_v1"
AUDIT_RESULT_SCHEMA = "exam_tutoring_context_audit_result_v1"
QUERY_INPUT_SCHEMA = "exam_tutoring_context_query_v1"

DEFAULT_STATE_REL = Path(
    "wiki/study_vaults/408-full/state/exam-tutoring-context-v1"
)
REVIEW_LEDGER_REL = Path(
    "wiki/study_vaults/408-full/state/review-loop/events.jsonl"
)

MANIFEST_FILENAME = "manifest.json"
FRESHNESS_CONTRACT = "committed-source-v5-knowledge-hot-routes"
INVALIDATION_FILENAME = "invalidation.json"
RELATED_SOURCE_FILES = ("知识点标签表.md", "知识点命中索引.md", "概念词典.md", "关系边表.md",
                        "support/concept-briefs/DS.json", "support/concept-briefs/CO.json",
                        "support/concept-briefs/OS.json", "support/concept-briefs/CN.json",
                        ".private/knowledge-profiles-v1/current.json")
LOCK_FILENAME = ".refresh.lock"
SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
CARD_FIELD_RE = re.compile(r"^\s*-\s*([^：:\n]+)[：:]\s*(.*?)\s*$")
TERM_SPLIT_RE = re.compile(r"[；;,，、/|｜:：()（）\[\]【】]+")
KNOWLEDGE_CODE_RE = re.compile(r"\b[A-Z]{2}\d{2}(?:-\d{2})?\b")

MAX_QUERY_PROFILES = 3
DEFAULT_QUERY_BYTES = 8192
MIN_QUERY_BYTES = 1024
MAX_QUERY_BYTES = 8192
MAX_INPUT_BYTES = 64 * 1024
MAX_PROFILE_BYTES = 24 * 1024
MAX_INDEX_BYTES = 4 * 1024 * 1024
MAX_KNOWLEDGE_INDEX_BYTES = 4 * 1024 * 1024
MAX_MANIFEST_BYTES = 2 * 1024 * 1024
MAX_RECEIPT_BYTES = 2 * 1024 * 1024  # Cold full-source receipts; never read on the learner path.
MAX_HISTORY_SOURCES_BYTES = 16 * 1024 * 1024  # Cold full registry; never read on the learner path.
MAX_EVENT_REFS = 24
SOURCE_ANCHOR_BYTES = 4096

RESULT_ENUM = {"independent_correct", "partial", "wrong", "uncertain"}
CONFIDENCE_ENUM = {"low", "medium", "high"}
DIRECT_FIRST_BREAK_PROVENANCE = {"user_report", "visible_evidence"}
PERFORMANCE_KINDS = {"outcome", "review_schedule_ensure_v1"}

FORBIDDEN_QUERY_KEYS = {
    "answer",
    "answers",
    "correct_answer",
    "correct_option",
    "full_question",
    "full_response",
    "options",
    "question_stem",
    "question_text",
    "solution",
    "standard_answer",
    "完整作答",
    "完整解析",
    "答案",
    "解析",
    "选项",
    "题干",
}

ALLOWED_QUERY_KEYS = {
    "schema",
    "stage",
    "formal_id",
    "source_id",
    "subject",
    "knowledge_points",
    "weak_knowledge_points",
    "mechanism_terms",
    "ask_type_terms",
    "max_profiles",
    "max_bytes",
    "read_stats",
}


class ExamTutoringContextError(RuntimeError):
    """Stable failure for cold refresh/audit or malformed hot queries."""


def _canonical_bytes(value: Any) -> bytes:
    return (
        json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        + "\n"
    ).encode("utf-8")


def _sha256_bytes(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _validate_date(value: str) -> str:
    if not DATE_RE.fullmatch(value or ""):
        raise ExamTutoringContextError("refresh date must use YYYY-MM-DD")
    try:
        parsed = dt.date.fromisoformat(value)
    except ValueError as exc:
        raise ExamTutoringContextError("refresh date is not a valid date") from exc
    if parsed.isoformat() != value:
        raise ExamTutoringContextError("refresh date is not canonical")
    return value


def _resolve_repo(repo: Union[str, Path]) -> Path:
    root = Path(repo).resolve()
    if not (root / MASTER_FILE).is_file():
        raise ExamTutoringContextError(
            "%s does not contain %s" % (root, MASTER_FILE)
        )
    return root


def _resolve_state_root(
    repo: Path, output_root: Optional[Union[str, Path]] = None
) -> Path:
    if output_root is None:
        return repo / DEFAULT_STATE_REL
    value = Path(output_root)
    if not value.is_absolute():
        value = repo / value
    return value.resolve()


def _regular_stat(path: Path, label: str) -> os.stat_result:
    try:
        info = path.lstat()
    except FileNotFoundError as exc:
        raise ExamTutoringContextError("%s is missing: %s" % (label, path)) from exc
    if stat.S_ISLNK(info.st_mode) or not stat.S_ISREG(info.st_mode):
        raise ExamTutoringContextError("%s must be a regular non-symlink file" % label)
    return info


def _source_guard(path: Path, label: str, *, with_anchor: bool = False) -> Dict[str, Any]:
    info = _regular_stat(path, label)
    guard: Dict[str, Any] = {
        "path": str(path),
        "size": info.st_size,
        "mtime_ns": info.st_mtime_ns,
        "ctime_ns": info.st_ctime_ns,
    }
    if with_anchor:
        head_bytes = min(info.st_size, SOURCE_ANCHOR_BYTES)
        tail_bytes = min(info.st_size, SOURCE_ANCHOR_BYTES)
        tail_start = max(0, info.st_size - tail_bytes)
        with path.open("rb") as handle:
            head = handle.read(head_bytes)
            handle.seek(tail_start)
            tail = handle.read(tail_bytes)
        guard.update(
            {
                "head_bytes": head_bytes,
                "head_sha256": _sha256_bytes(head),
                "tail_start": tail_start,
                "tail_bytes": tail_bytes,
                "tail_sha256": _sha256_bytes(tail),
                "ends_with_newline": info.st_size == 0 or tail.endswith(b"\n"),
            }
        )
    return guard


def _source_commitment(path: Path, label: str) -> Dict[str, Any]:
    info = _regular_stat(path, label)
    return {
        "path": str(path),
        "size": info.st_size,
        "sha256": _sha256_file(path),
    }


def _atomic_write(path: Path, payload: bytes, *, mode: int = 0o644) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(prefix=".%s." % path.name, dir=path.parent)
    try:
        os.fchmod(descriptor, mode)
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
        directory_fd = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def _content_write(directory: Path, value: Mapping[str, Any]) -> Tuple[Path, str]:
    payload = _canonical_bytes(value)
    digest = _sha256_bytes(payload)
    path = directory / (digest + ".json")
    if path.exists():
        info = _regular_stat(path, "content-addressed object")
        if info.st_size != len(payload) or path.read_bytes() != payload:
            raise ExamTutoringContextError("content-addressed object collision")
        return path, digest
    _atomic_write(path, payload)
    return path, digest


def _read_object(path: Path, label: str, *, max_bytes: int) -> Dict[str, Any]:
    info = _regular_stat(path, label)
    if info.st_size > max_bytes:
        raise ExamTutoringContextError("%s exceeds its bounded size" % label)
    try:
        value = json.loads(path.read_bytes().decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ExamTutoringContextError("%s is not valid UTF-8 JSON" % label) from exc
    if not isinstance(value, dict):
        raise ExamTutoringContextError("%s must be a JSON object" % label)
    return value


def _read_content_object(
    state_root: Path,
    ref: object,
    expected_sha256: object,
    label: str,
    *,
    max_bytes: int,
) -> Dict[str, Any]:
    if not isinstance(ref, str) or not isinstance(expected_sha256, str):
        raise ExamTutoringContextError("%s reference is malformed" % label)
    if not SHA256_RE.fullmatch(expected_sha256):
        raise ExamTutoringContextError("%s sha256 is malformed" % label)
    path = (state_root / ref).resolve()
    try:
        path.relative_to(state_root.resolve())
    except ValueError as exc:
        raise ExamTutoringContextError("%s reference escapes state root" % label) from exc
    if path.name != expected_sha256 + ".json":
        raise ExamTutoringContextError("%s is not at its content address" % label)
    value = _read_object(path, label, max_bytes=max_bytes)
    if _sha256_file(path) != expected_sha256:
        raise ExamTutoringContextError("%s content hash mismatch" % label)
    return value


@contextmanager
def _refresh_lock(state_root: Path) -> Iterator[None]:
    state_root.mkdir(parents=True, exist_ok=True)
    lock_path = state_root / LOCK_FILENAME
    with lock_path.open("a+b") as handle:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def _parse_card_fields(text: str) -> Dict[str, str]:
    fields: Dict[str, str] = {}
    for line in text.splitlines():
        match = CARD_FIELD_RE.match(line)
        if match:
            label = match.group(1).strip()
            value = match.group(2).strip()
            if label not in fields and value:
                fields[label] = value
    return fields


def _parse_review_mapping(repo_root: Path) -> Tuple[Dict[str, Dict[str, Any]], Dict[str, List[str]]]:
    path = repo_root / REVIEW_UNIT_MAPPING_FILE
    lines = read_text(path).splitlines()
    formal: Dict[str, Dict[str, Any]] = {}
    reverse: Dict[str, List[str]] = defaultdict(list)
    for _, cells in iter_table_rows(lines, "正式节点ID"):
        if len(cells) != len(REVIEW_UNIT_MAPPING_COLS):
            raise ExamTutoringContextError(
                "%s contains a malformed row" % REVIEW_UNIT_MAPPING_FILE
            )
        row = dict(zip(REVIEW_UNIT_MAPPING_COLS, cells))
        formal_id = row["正式节点ID"].strip()
        if not FORMAL_ID_RE.fullmatch(formal_id):
            raise ExamTutoringContextError(
                "%s contains an invalid formal ID: %s"
                % (REVIEW_UNIT_MAPPING_FILE, formal_id)
            )
        if formal_id in formal:
            raise ExamTutoringContextError(
                "%s contains duplicate formal ID %s"
                % (REVIEW_UNIT_MAPPING_FILE, formal_id)
            )
        review_units: List[str] = []
        for raw in (row["主复习单元ID"], row["次复习单元ID"]):
            for value in split_multi(raw):
                value = value.strip()
                if value and value != "无" and value not in review_units:
                    review_units.append(value)
                    reverse[value].append(formal_id)
        formal[formal_id] = {
            "actual_point": row["实际考点"].strip(),
            "review_unit_ids": review_units,
        }
    return formal, {key: sorted(set(value)) for key, value in reverse.items()}


def _bounded_text(value: object, limit: int, *, keep_tail: bool = False) -> str:
    text = re.sub(r"\s+", " ", str(value or "")).strip()
    if len(text) <= limit:
        return text
    if keep_tail:
        return "…" + text[-(limit - 1) :]
    return text[: limit - 1] + "…"


def _bounded_list(values: Iterable[str], *, item_limit: int = 220, count_limit: int = 24) -> List[str]:
    out: List[str] = []
    for raw in values:
        value = _bounded_text(raw, item_limit)
        if value and value not in out:
            out.append(value)
        if len(out) >= count_limit:
            break
    return out


def _answer_safe_field(label: str, value: str) -> None:
    fails, warns = scan_leak(value)
    if fails or warns:
        details = "; ".join(fails + warns)
        raise ExamTutoringContextError(
            "answer-safety scan rejected %s: %s" % (label, details)
        )


def _answer_safe_profile(profile: Mapping[str, Any]) -> None:
    text_fields: List[Tuple[str, str]] = []
    for key in (
        "question_type",
        "ask_type",
        "core_mechanism",
        "fuzzy_concepts",
        "user_error_entry",
        "redo_first_action",
    ):
        text_fields.append((key, str(profile.get(key) or "")))
    knowledge = profile.get("knowledge") or {}
    if isinstance(knowledge, dict):
        for key, values in knowledge.items():
            if isinstance(values, list):
                for value in values:
                    text_fields.append(("knowledge.%s" % key, str(value)))
    history = profile.get("history") or {}
    if isinstance(history, dict):
        for item in history.get("top_direct_first_breaks") or []:
            if isinstance(item, dict):
                text_fields.append(("top_direct_first_breaks", str(item.get("text") or "")))
    for label, value in text_fields:
        _answer_safe_field(label, value)


def _normalize_term(value: object) -> str:
    text = unicodedata.normalize("NFKC", str(value or ""))
    return re.sub(r"\s+", " ", text).strip().lower()


def _term_variants(value: str) -> Set[str]:
    normalized = _normalize_term(value)
    if not normalized:
        return set()
    variants = {normalized}
    for part in TERM_SPLIT_RE.split(value):
        item = _normalize_term(part)
        if 1 < len(item) <= 240:
            variants.add(item)
    code_match = re.match(r"^([A-Z]{2}\d{2}(?:-\d{2})?)\s+(.+)$", value.strip())
    if code_match:
        variants.add(_normalize_term(code_match.group(1)))
        variants.add(_normalize_term(code_match.group(2)))
    for code in KNOWLEDGE_CODE_RE.findall(value.upper()):
        variants.add(_normalize_term(code))
    return {item for item in variants if len(item) <= 300}


def _empty_signal_bucket() -> Dict[str, Any]:
    return {
        "event_count": 0,
        "result_counts": {},
        "confidence_counts": {},
        "prompt_dependency_counts": {},
        "event_refs": [],
        "event_refs_truncated": False,
    }


def _event_formal_ids(event: Mapping[str, Any], master_ids: Set[str]) -> Set[str]:
    result: Set[str] = set()
    direct = event.get("formal_node_id")
    if isinstance(direct, str) and direct in master_ids:
        result.add(direct)
    for key in ("formal_node_ids", "mapped_formal_node_ids"):
        values = event.get(key)
        if isinstance(values, list):
            for value in values:
                if isinstance(value, str) and value in master_ids:
                    result.add(value)
    return result


def _read_review_events(
    ledger_path: Path,
) -> Tuple[List[Dict[str, Any]], Dict[str, Dict[str, Any]], int]:
    records: List[Dict[str, Any]] = []
    by_id: Dict[str, Dict[str, Any]] = {}
    line_count = 0
    _regular_stat(ledger_path, "review ledger")
    with ledger_path.open("rb") as handle:
        for ordinal, raw in enumerate(handle, 1):
            if not raw.strip():
                continue
            line_count += 1
            try:
                event = json.loads(raw.decode("utf-8"))
            except (UnicodeDecodeError, json.JSONDecodeError) as exc:
                raise ExamTutoringContextError(
                    "review ledger line %d is invalid JSON" % ordinal
                ) from exc
            if not isinstance(event, dict):
                raise ExamTutoringContextError(
                    "review ledger line %d is not an object" % ordinal
                )
            if event.get("schema") != "review_event_v1":
                raise ExamTutoringContextError(
                    "review ledger line %d has an unsupported schema" % ordinal
                )
            event_id = event.get("event_id")
            if not isinstance(event_id, str) or not event_id:
                raise ExamTutoringContextError(
                    "review ledger line %d has no event_id" % ordinal
                )
            if event_id in by_id:
                raise ExamTutoringContextError(
                    "review ledger contains duplicate event_id %s" % event_id
                )
            record = {
                "event": event,
                "event_id": event_id,
                "event_sha256": _sha256_bytes(raw),
                "ordinal": ordinal,
            }
            records.append(record)
            by_id[event_id] = record
    return records, by_id, line_count


def _association_maps(
    records: Sequence[Mapping[str, Any]],
    by_id: Mapping[str, Mapping[str, Any]],
    master: Mapping[str, Mapping[str, Any]],
    source_to_formal: Mapping[str, Sequence[str]],
    review_unit_to_formal: Mapping[str, Sequence[str]],
) -> Tuple[Dict[str, Set[str]], Dict[str, Set[str]]]:
    master_ids = set(master)
    direct: Dict[str, Set[str]] = defaultdict(set)
    associated: Dict[str, Set[str]] = defaultdict(set)

    for record in records:
        event = record["event"]
        event_id = str(record["event_id"])
        explicit = _event_formal_ids(event, master_ids)
        direct[event_id].update(explicit)
        source_id = event.get("source_id")
        if not explicit and isinstance(source_id, str):
            direct[event_id].update(source_to_formal.get(source_id, ()))
        for key in ("review_unit_id", "mechanism_key", "source_id"):
            value = event.get(key)
            if isinstance(value, str):
                associated[event_id].update(review_unit_to_formal.get(value, ()))

    # Formal backflow events provide an explicit immutable link to the source
    # learner event.  Propagate that binding until stable; do not infer links
    # from text similarity.
    changed = True
    passes = 0
    while changed and passes < 4:
        changed = False
        passes += 1
        for record in records:
            event = record["event"]
            event_id = str(record["event_id"])
            bound = set(direct.get(event_id, set()))
            if not bound:
                continue
            for ref_key in (
                "source_review_event_id",
                "ensure_event_id",
                "source_outcome_event_id",
            ):
                ref = event.get(ref_key)
                if isinstance(ref, str) and ref in by_id:
                    before = len(direct[ref])
                    direct[ref].update(bound)
                    if len(direct[ref]) != before:
                        changed = True

    outcomes: Dict[Tuple[str, str], List[str]] = defaultdict(list)
    for record in records:
        event = record["event"]
        if event.get("event_kind") == "outcome" and event.get("session_id") and event.get("item_id"):
            outcomes[(event["session_id"], event["item_id"])].append(str(record["event_id"]))
    for record in records:
        event = record["event"]
        if event.get("event_kind") != "repair" or _event_formal_ids(event, master_ids):
            continue
        parents = outcomes.get((event.get("session_id"), event.get("item_id")), [])
        if len(parents) == 1:
            event_id = str(record["event_id"])
            direct[event_id] = set(direct.get(parents[0], set()))
            associated[event_id] = set(associated.get(parents[0], set()))
    for event_id, values in direct.items():
        associated[event_id].difference_update(values)
    return direct, associated


def _event_is_performance(event: Mapping[str, Any], by_id: Mapping[str, Any]) -> bool:
    kind = event.get("event_kind")
    if kind not in PERFORMANCE_KINDS:
        return False
    if event.get("first_result") not in RESULT_ENUM:
        return False
    if kind == "review_schedule_ensure_v1":
        source_event_id = event.get("source_review_event_id")
        if isinstance(source_event_id, str) and source_event_id in by_id:
            return False
    return True


def _prompt_dependency(event: Mapping[str, Any]) -> str:
    if event.get("hint_used") is True:
        return "hint_or_prompted"
    prompt_level = event.get("prompt_level")
    if isinstance(prompt_level, str) and prompt_level != "none":
        return "hint_or_prompted"
    if prompt_level == "none" and event.get("hint_used") is not True:
        return "independent"
    return "unknown"


def _aggregate_signals(
    records: Sequence[Mapping[str, Any]],
    by_id: Mapping[str, Mapping[str, Any]],
    direct: Mapping[str, Set[str]],
    associated: Mapping[str, Set[str]],
    formal_ids: Iterable[str],
) -> Dict[str, Dict[str, Any]]:
    raw: Dict[str, Dict[str, Any]] = {}
    for formal_id in formal_ids:
        raw[formal_id] = {
            "direct_events": [],
            "associated_events": [],
            "first_breaks": defaultdict(list),
            "direct_repairs": [],
            "associated_repairs": [],
        }

    for record in records:
        event = record["event"]
        repair = event.get("event_kind") == "repair" and event.get("correction_result") in (RESULT_ENUM | {"relearn_required"})
        if not repair and not _event_is_performance(event, by_id):
            continue
        event_id = str(record["event_id"])
        direct_ids = set(direct.get(event_id, set()))
        associated_ids = set(associated.get(event_id, set())) - direct_ids
        ref = {
            "event_id": event_id,
            "event_sha256": record["event_sha256"],
            "ordinal": record["ordinal"],
            "event": event,
            "correction_refs": record.get("correction_refs", []),
        }
        for formal_id in direct_ids:
            if formal_id not in raw:
                continue
            if repair:
                raw[formal_id]["direct_repairs"].append(ref)
                continue
            raw[formal_id]["direct_events"].append(ref)
            first_break = _bounded_text(event.get("first_break"), 360)
            provenance = event.get("first_break_provenance")
            if first_break and provenance in DIRECT_FIRST_BREAK_PROVENANCE:
                fails, warns = scan_leak(first_break)
                if not fails and not warns:
                    raw[formal_id]["first_breaks"][first_break].append(ref)
        for formal_id in associated_ids:
            if formal_id in raw:
                raw[formal_id]["associated_repairs" if repair else "associated_events"].append(ref)

    def event_reference(item: Mapping[str, Any]) -> Dict[str, Any]:
        reference = {"event_id": item["event_id"], "event_sha256": item["event_sha256"],
                     "observed_date": item["event"].get("observed_date")}
        if item.get("correction_refs"):
            reference["correction_refs"] = item["correction_refs"]
            if "stored_observed_date" in item["event"]:
                reference["stored_observed_date"] = item["event"]["stored_observed_date"]
        return reference

    def bucket(values: Sequence[Mapping[str, Any]]) -> Dict[str, Any]:
        results: Counter[str] = Counter()
        confidence: Counter[str] = Counter()
        dependency: Counter[str] = Counter()
        for value in values:
            event = value["event"]
            result = event.get("first_result")
            if result in RESULT_ENUM:
                results[str(result)] += 1
            conf = event.get("confidence")
            if conf in CONFIDENCE_ENUM:
                confidence[str(conf)] += 1
            else:
                confidence["unknown"] += 1
            dependency[_prompt_dependency(event)] += 1
        ordered = sorted(values, key=lambda item: int(item["ordinal"]), reverse=True)
        event_refs = [event_reference(item) for item in ordered[:MAX_EVENT_REFS]]
        return {
            "event_count": len(values),
            "result_counts": dict(sorted(results.items())),
            "confidence_counts": dict(sorted(confidence.items())),
            "prompt_dependency_counts": dict(sorted(dependency.items())),
            "event_refs": event_refs,
            "event_refs_truncated": len(values) > MAX_EVENT_REFS,
        }

    def repairs(values: Sequence[Mapping[str, Any]]) -> Dict[str, Any]:
        ordered = sorted(values, key=lambda item: int(item["ordinal"]), reverse=True)
        latest = None
        if ordered:
            item = ordered[0]
            event = item["event"]
            latest = {"event_id": item["event_id"], "event_sha256": item["event_sha256"],
                      "observed_date": event.get("observed_date"),
                      "correction_result": event["correction_result"],
                      "conclusion_correct": (True if event.get("correction_conclusion") == "correct"
                                              or event["correction_result"] == "independent_correct"
                                              else False if event["correction_result"] == "wrong" else None),
                      "prompt_dependency": _prompt_dependency(event),
                      "confidence": event.get("confidence"), "establishes_independent_mastery": False}
        return {"event_count": len(values), "latest": latest,
                "event_refs": [event_reference(item) for item in ordered[:8]],
                "event_refs_truncated": len(values) > 8}

    result: Dict[str, Dict[str, Any]] = {}
    for formal_id, value in raw.items():
        top_breaks: List[Dict[str, Any]] = []
        for text, refs in sorted(
            value["first_breaks"].items(),
            key=lambda item: (-len(item[1]), -max(int(ref["ordinal"]) for ref in item[1]), item[0]),
        )[:3]:
            ordered_refs = sorted(refs, key=lambda item: int(item["ordinal"]), reverse=True)
            top_breaks.append(
                {
                    "text": text,
                    "count": len(refs),
                    "event_refs": [event_reference(item) for item in ordered_refs[:4]],
                }
            )
        result[formal_id] = {
            "direct": bucket(value["direct_events"]),
            "review_unit_associated": bucket(value["associated_events"]),
            "top_direct_first_breaks": top_breaks,
            "direct_repairs": repairs(value["direct_repairs"]),
            "associated_repairs": repairs(value["associated_repairs"]),
        }
    return result


def _build_profile(
    formal_id: str,
    row: Mapping[str, Any],
    card_fields: Mapping[str, str],
    mapping: Mapping[str, Any],
    history: Mapping[str, Any],
) -> Dict[str, Any]:
    main = _bounded_list(split_multi(str(row.get("主知识点") or "")))
    sub = _bounded_list(split_multi(str(row.get("副知识点") or "")))
    hit = _bounded_list(split_multi(str(row.get("命中知识点") or "")))
    core_mechanism = (
        card_fields.get("实际考查机制")
        or str(mapping.get("actual_point") or "")
        or card_fields.get("核心考点")
        or str(row.get("核心考点") or "")
    )
    profile = {
        "schema": PROFILE_SCHEMA,
        "formal_id": formal_id,
        "source_id": _bounded_text(row.get("来源ID"), 240),
        "knowledge": {"main": main, "sub": sub, "hit": hit},
        "question_type": _bounded_text(row.get("题型"), 320),
        "ask_type": _bounded_text(
            card_fields.get("问法类型") or row.get("题型"), 420
        ),
        "core_mechanism": _bounded_text(core_mechanism, 1000),
        "fuzzy_concepts": _bounded_text(
            card_fields.get("模糊概念") or row.get("模糊概念"), 850
        ),
        "user_error_entry": _bounded_text(
            card_fields.get("用户错误入口") or row.get("最近错误记录"),
            1400,
            keep_tail=True,
        ),
        "redo_first_action": _bounded_text(
            card_fields.get("复做第一动作"), 850
        ),
        "history": history,
    }
    _answer_safe_profile(profile)
    if len(_canonical_bytes(profile)) > MAX_PROFILE_BYTES:
        raise ExamTutoringContextError("profile %s exceeds its bounded size" % formal_id)
    return profile


def _build_indexes(
    refresh_date: str,
    profiles: Mapping[str, Mapping[str, Any]],
    profile_sha256s: Mapping[str, str],
    cards: Mapping[str, Mapping[str, Any]],
    formal_mapping: Mapping[str, Mapping[str, Any]],
    event_mechanisms: Mapping[str, Set[str]],
) -> Tuple[Dict[str, Any], Dict[str, Any]]:
    by_source: Dict[str, List[str]] = defaultdict(list)
    index_profiles: Dict[str, Any] = {}
    by_knowledge: Dict[str, Set[str]] = defaultdict(set)
    by_mechanism: Dict[str, Set[str]] = defaultdict(set)
    by_ask_type: Dict[str, Set[str]] = defaultdict(set)

    for formal_id in sorted(profiles):
        profile = profiles[formal_id]
        source_id = str(profile.get("source_id") or "")
        if source_id:
            by_source[source_id].append(formal_id)
        mapping = formal_mapping.get(formal_id) or {}
        review_units = list(mapping.get("review_unit_ids") or [])
        card = cards[formal_id]
        index_profiles[formal_id] = {
            "profile_ref": "objects/profiles/%s.json" % profile_sha256s[formal_id],
            "profile_sha256": profile_sha256s[formal_id],
            "source_id": source_id,
            "card_relpath": card["relpath"],
            "card_sha256": card["sha256"],
            "card_guard": card["guard"],
            "review_unit_ids": sorted(review_units),
        }
        knowledge = profile.get("knowledge") or {}
        if isinstance(knowledge, dict):
            for values in knowledge.values():
                if isinstance(values, list):
                    for value in values:
                        for term in _term_variants(str(value)):
                            by_knowledge[term].add(formal_id)
        for value in [str(profile.get("core_mechanism") or "")] + review_units:
            for term in _term_variants(value):
                by_mechanism[term].add(formal_id)
        for term in event_mechanisms.get(formal_id, set()):
            normalized = _normalize_term(term)
            if normalized:
                by_mechanism[normalized].add(formal_id)
        for value in (
            str(profile.get("ask_type") or ""),
            str(profile.get("question_type") or ""),
        ):
            for term in _term_variants(value):
                by_ask_type[term].add(formal_id)

    index = {
        "schema": INDEX_SCHEMA,
        "refresh_date": refresh_date,
        "profiles": index_profiles,
        "by_source_id": {key: sorted(set(value)) for key, value in sorted(by_source.items())},
    }
    knowledge_index = {
        "schema": KNOWLEDGE_INDEX_SCHEMA,
        "refresh_date": refresh_date,
        "by_knowledge_term": {
            key: sorted(value) for key, value in sorted(by_knowledge.items())
        },
        "by_mechanism_term": {
            key: sorted(value) for key, value in sorted(by_mechanism.items())
        },
        "by_ask_type_term": {
            key: sorted(value) for key, value in sorted(by_ask_type.items())
        },
    }
    if len(_canonical_bytes(index)) > MAX_INDEX_BYTES:
        raise ExamTutoringContextError("exact index exceeds its bounded size")
    if len(_canonical_bytes(knowledge_index)) > MAX_KNOWLEDGE_INDEX_BYTES:
        raise ExamTutoringContextError("knowledge index exceeds its bounded size")
    return index, knowledge_index


def _history_source_projection(history_sources: Mapping[str, Any]) -> Dict[str, Any]:
    """Small hot-path view of the history source registry.

    The registry itself is ~650 KB, but the pre-answer identity check only asks
    which semantic annotations and concept-review sessions exist. Keep that
    projection inline in the manifest and let the full table live in its own
    content-addressed object so a hot query never parses it.
    """
    from knowledge_history_sources_408 import SEMANTIC_ROOT
    annotations_dir = SEMANTIC_ROOT / "annotations"
    semantic: List[str] = []
    sessions: List[str] = []
    for path in history_sources:
        candidate = Path(path)
        if candidate.parent == annotations_dir:
            semantic.append(path)
        elif (candidate.name == "session.json"
              and candidate.parent.parent.name == "sessions"
              and "concept-review" in candidate.parts):
            sessions.append(path)
    return {
        "count": len(history_sources),
        "semantic_annotation_paths": sorted(semantic),
        "concept_review_session_paths": sorted(sessions),
    }


def _compute_snapshot(repo_root: Path, refresh_date: str, *, verify_originals: bool = False) -> Dict[str, Any]:
    watched = [MASTER_FILE, REVIEW_UNIT_MAPPING_FILE, str(REVIEW_LEDGER_REL)]
    watched += [name for name in RELATED_SOURCE_FILES if (repo_root / name).is_file()]
    before = {name: _source_guard(repo_root / name, name) for name in watched}
    invalidation_path = repo_root / DEFAULT_STATE_REL / INVALIDATION_FILENAME
    invalidation_sha = _sha256_file(invalidation_path) if invalidation_path.is_file() else None
    repo = Repo(repo_root)
    master = repo.master()
    if not master:
        raise ExamTutoringContextError("formal master table is empty")
    for formal_id, row in master.items():
        if not FORMAL_ID_RE.fullmatch(formal_id):
            raise ExamTutoringContextError("formal master contains invalid ID %s" % formal_id)
        if row.get("_bad_colcount") is not None:
            raise ExamTutoringContextError("formal master row %s is malformed" % formal_id)

    formal_mapping, review_unit_to_formal = _parse_review_mapping(repo_root)
    missing_mapping = sorted(set(master) - set(formal_mapping))
    if missing_mapping:
        raise ExamTutoringContextError(
            "review-unit mapping is missing formal IDs: %s"
            % ", ".join(missing_mapping[:5])
        )

    cards: Dict[str, Dict[str, Any]] = {}
    card_fields: Dict[str, Dict[str, str]] = {}
    for formal_id in sorted(master):
        path = repo_root / (formal_id + ".md")
        info = _regular_stat(path, "safe card %s" % formal_id)
        text = path.read_text(encoding="utf-8")
        fields = _parse_card_fields(text)
        if fields.get("正式节点 ID") not in (None, formal_id):
            raise ExamTutoringContextError(
                "safe card %s declares a different formal ID" % formal_id
            )
        required = ("问法类型", "用户错误入口", "复做第一动作")
        missing = [key for key in required if not fields.get(key)]
        if missing:
            raise ExamTutoringContextError(
                "safe card %s is missing fields: %s"
                % (formal_id, ", ".join(missing))
            )
        cards[formal_id] = {
            "relpath": formal_id + ".md",
            "sha256": _sha256_bytes(text.encode("utf-8")),
            "guard": {
                "size": info.st_size,
                "mtime_ns": info.st_mtime_ns,
                "ctime_ns": info.st_ctime_ns,
            },
        }
        card_fields[formal_id] = fields

    ledger_path = repo_root / REVIEW_LEDGER_REL
    records, by_id, line_count = _read_review_events(ledger_path)
    source_to_formal: Dict[str, List[str]] = defaultdict(list)
    for formal_id, row in master.items():
        source_id = str(row.get("来源ID") or "").strip()
        if source_id:
            source_to_formal[source_id].append(formal_id)
    direct, associated = _association_maps(
        records,
        by_id,
        master,
        source_to_formal,
        review_unit_to_formal,
    )
    effective_records = effective_review_records(records)
    effective_by_id = {item["event_id"]: item for item in effective_records}
    history = _aggregate_signals(effective_records, effective_by_id, direct, associated, master)

    event_mechanisms: Dict[str, Set[str]] = defaultdict(set)
    for record in records:
        event_id = str(record["event_id"])
        mechanism = record["event"].get("mechanism_key")
        if not isinstance(mechanism, str) or not mechanism:
            continue
        for formal_id in set(direct.get(event_id, set())) | set(
            associated.get(event_id, set())
        ):
            event_mechanisms[formal_id].add(mechanism)

    profiles: Dict[str, Dict[str, Any]] = {}
    profile_sha256s: Dict[str, str] = {}
    for formal_id in sorted(master):
        profile = _build_profile(
            formal_id,
            master[formal_id],
            card_fields[formal_id],
            formal_mapping[formal_id],
            history.get(formal_id)
            or {
                "direct": _empty_signal_bucket(),
                "review_unit_associated": _empty_signal_bucket(),
                "top_direct_first_breaks": [],
            },
        )
        profiles[formal_id] = profile
        profile_sha256s[formal_id] = _sha256_bytes(_canonical_bytes(profile))

    index, knowledge_index = _build_indexes(
        refresh_date,
        profiles,
        profile_sha256s,
        cards,
        formal_mapping,
        event_mechanisms,
    )
    supplemental = (collect_history_sources(repo_root, master, force_source_read=True)
                    if verify_originals else collect_history_sources(repo_root, master))
    memory = build_knowledge_memory(profiles, master, cards, records, direct, associated,
                                    supplemental_records=supplemental["records"])
    source_coverage = {key: value for key, value in supplemental.get("coverage", {}).items() if key != "cache_reused"}
    taxonomy_path = repo_root / "知识点标签表.md"
    from knowledge_profile_408 import load_profiles
    from question_visual_links_408 import visual_link
    from urllib.parse import urlencode
    reviewed = load_profiles(repo_root)
    has_vault = (repo_root / ".obsidian").is_dir()
    question_references = {
        fid: {"formal_id": fid, "card_path": str(repo_root / card["relpath"]),
              "card_relpath": card["relpath"], "card_sha256": card["sha256"],
              **visual_link(repo_root, fid),
              "relationship": "associated_formal_card_not_proof_of_user_error"}
        for fid, card in cards.items()
    }
    point_artifacts = build_artifacts(memory, source_coverage, reviewed_profiles=reviewed["profiles"],
        question_references=question_references,
        taxonomy_text=taxonomy_path.read_text(encoding="utf-8") if taxonomy_path.is_file() else "",
        repo_root=repo_root)
    try:
        hot_routes = build_routes(index, knowledge_index, point_artifacts["index"])
    except (ValueError, KeyError, TypeError) as exc:
        raise ExamTutoringContextError(
            "knowledge hot-route build failed: %s" % exc) from exc
    point_artifacts["objects"].update(hot_routes["objects"])
    index_sha = _sha256_bytes(_canonical_bytes(index))
    knowledge_sha = _sha256_bytes(_canonical_bytes(knowledge_index))
    profile_set = [
        [formal_id, profile_sha256s[formal_id], cards[formal_id]["sha256"]]
        for formal_id in sorted(profiles)
    ]
    profile_set_sha = _sha256_bytes(_canonical_bytes(profile_set))
    cards_set_sha = _sha256_bytes(
        _canonical_bytes(
            [
                [formal_id, cards[formal_id]["sha256"]]
                for formal_id in sorted(cards)
            ]
        )
    )

    master_path = repo_root / MASTER_FILE
    mapping_path = repo_root / REVIEW_UNIT_MAPPING_FILE
    source_commitments = {
        "formal_master": _source_commitment(master_path, "formal master"),
        "review_unit_mapping": _source_commitment(
            mapping_path, "review-unit mapping"
        ),
        "safe_cards": {
            "count": len(cards),
            "set_sha256": cards_set_sha,
        },
        "review_ledger": {
            **_source_commitment(ledger_path, "review ledger"),
            "line_count": line_count,
        },
    }
    source_commitments["related_sources"] = {
        name: _source_commitment(repo_root / name, name)
        for name in RELATED_SOURCE_FILES if (repo_root / name).is_file()
    }
    source_commitments["point_memory_builder"] = {
        "version": BUILD_VERSION,
        "modules": {name: _sha256_file(Path(__file__).with_name(name)) for name in (
            "exam_tutoring_context_408.py", "knowledge_learning_records_408.py",
            "knowledge_history_sources_408.py", "knowledge_tutoring_query_408.py",
            "knowledge_hot_routes_408.py", "seal_knowledge_observations_408.py",
            "knowledge_profile_408.py", "knowledge_learning_paths_408.py",
            "question_visual_links_408.py")},
    }
    source_commitments["history_sources"] = {
        entry["path"]: entry for entry in supplemental.get("source_commitments", [])
    }
    # The full registry is ~650 KB and would dominate every hot manifest read.
    # Freeze it in its own content-addressed object and keep only the small
    # projection the hot identity check needs.
    history_sources_object = {
        "schema": HISTORY_SOURCES_SCHEMA,
        "history_sources": source_commitments["history_sources"],
    }
    history_sources_sha = _sha256_bytes(_canonical_bytes(history_sources_object))
    history_sources_ref = "%s/%s.json" % (HISTORY_SOURCES_DIR, history_sources_sha)
    history_source_projection = _history_source_projection(source_commitments["history_sources"])
    receipt_commitments = {k: v for k, v in source_commitments.items() if k != "history_sources"}
    receipt_commitments["history_sources_ref"] = history_sources_ref
    receipt_commitments["history_sources_sha256"] = history_sources_sha
    receipt_commitments["history_source_count"] = len(source_commitments["history_sources"])
    for guard in supplemental.get("source_guards", []):
        if not _guard_unchanged(Path(guard["path"]), guard, "history source"):
            raise ExamTutoringContextError("history_source_changed_during_refresh")
    for name, guard in before.items():
        if not _guard_unchanged(repo_root / name, guard, name):
            raise ExamTutoringContextError("source_changed_during_refresh:%s" % name)
    for formal_id, card in cards.items():
        if not _guard_unchanged(repo_root / card["relpath"], card["guard"], formal_id):
            raise ExamTutoringContextError("card_changed_during_refresh:%s" % formal_id)
    if invalidation_sha != (_sha256_file(invalidation_path) if invalidation_path.is_file() else None):
        raise ExamTutoringContextError("commit_changed_during_refresh")
    receipt = {
        "schema": RECEIPT_SCHEMA,
        "refresh_date": refresh_date,
        "timezone": "Asia/Shanghai",
        "source_commitments": receipt_commitments,
        "artifacts": {
            "profile_count": len(profiles),
            "profile_set_sha256": profile_set_sha,
            "index_sha256": index_sha,
            "knowledge_index_sha256": knowledge_sha,
            "point_index_sha256": point_artifacts["index_ref"]["sha256"],
            "knowledge_hot_route_set_sha256": hot_routes["descriptor"]["set_sha256"],
            "knowledge_hot_route_bucket_count": hot_routes["descriptor"]["bucket_count"],
            "learning_record_count": memory["coverage"]["record_count"],
            "knowledge_point_count": memory["coverage"]["point_count"],
        },
        "answer_safe": True,
        "model_call_count": 0,
        "formal_write_count": 0,
    }
    receipt_sha = _sha256_bytes(_canonical_bytes(receipt))
    source_guards = {
        "formal_master": _source_guard(master_path, "formal master"),
        "review_unit_mapping": _source_guard(
            mapping_path, "review-unit mapping"
        ),
        "review_ledger": _source_guard(
            ledger_path, "review ledger", with_anchor=True
        ),
    }
    source_guards["related_sources"] = {
        name: (_source_guard(repo_root / name, name) if (repo_root / name).is_file() else None)
        for name in RELATED_SOURCE_FILES
    }
    manifest = {
        "schema": MANIFEST_SCHEMA,
        "refresh_date": refresh_date,
        "timezone": "Asia/Shanghai",
        "receipt_ref": "receipts/%s.json" % receipt_sha,
        "receipt_sha256": receipt_sha,
        "index_ref": "objects/indexes/%s.json" % index_sha,
        "index_sha256": index_sha,
        "point_index_ref": point_artifacts["index_ref"]["ref"],
        "point_index_sha256": point_artifacts["index_ref"]["sha256"],
        "knowledge_hot_routes": hot_routes["descriptor"],
        "point_memory_builder": source_commitments["point_memory_builder"],
        "history_sources_ref": history_sources_ref,
        "history_sources_sha256": history_sources_sha,
        "history_source_projection": history_source_projection,
        "knowledge_index_ref": "objects/knowledge/%s.json" % knowledge_sha,
        "knowledge_index_sha256": knowledge_sha,
        "profile_count": len(profiles),
        "source_guards": source_guards,
        "publication_order": "manifest_last",
        "freshness_contract": FRESHNESS_CONTRACT,
        "invalidation_sha256": invalidation_sha,
        "source_version": _sha256_bytes(_canonical_bytes({"sources": source_commitments, "builder": FRESHNESS_CONTRACT})),
    }
    return {
        "point_artifacts": point_artifacts,
        "hot_routes": hot_routes,
        "profiles": profiles,
        "profile_sha256s": profile_sha256s,
        "cards": cards,
        "index": index,
        "knowledge_index": knowledge_index,
        "receipt": receipt,
        "receipt_sha256": receipt_sha,
        "history_sources_object": history_sources_object,
        "history_sources_ref": history_sources_ref,
        "history_sources_sha256": history_sources_sha,
        "manifest": manifest,
        "manifest_sha256": _sha256_bytes(_canonical_bytes(manifest)),
    }


def _prune_unreachable_artifacts(state_root: Path, manifest: Mapping[str, Any],
                                 *, grace_seconds: float = 3600.0) -> Dict[str, int]:
    """Drop derived objects that the fixed manifest no longer reaches.

    Every generation is content-addressed and the hot path only follows the
    current manifest, so a superseded generation is dead weight. Objects newer
    than the grace window are kept so a reader that is still walking the
    previous generation cannot lose files mid-query.
    """
    reachable: set = set()
    pending: List[str] = [MANIFEST_FILENAME]
    # The fixed manifest is also stored content-addressed next to itself, and
    # ``_load_manifest`` requires that copy to exist. It is named after the
    # manifest's own bytes, so it is never reached by following refs and must be
    # pinned explicitly or a later sweep would delete the live entrypoint.
    manifest_bytes = (state_root / MANIFEST_FILENAME).read_bytes()
    pinned = "manifests/" + hashlib.sha256(manifest_bytes).hexdigest() + ".json"
    while pending:
        ref = pending.pop()
        if ref in reachable:
            continue
        path = state_root / ref
        if not path.is_file():
            continue
        reachable.add(ref)
        try:
            value = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        stack: List[Any] = [value]
        while stack:
            item = stack.pop()
            if isinstance(item, dict):
                for key, child in item.items():
                    if key == "ref" or (isinstance(key, str) and key.endswith("_ref")):
                        if isinstance(child, str):
                            pending.append(child)
                    else:
                        stack.append(child)
            elif isinstance(item, list):
                stack.extend(item)
    if (state_root / pinned).is_file():
        reachable.add(pinned)
    cutoff = time.time() - grace_seconds
    removed = 0
    removed_bytes = 0
    for label in ("objects", "manifests", "receipts"):
        directory = state_root / label
        if not directory.is_dir():
            continue
        for current, _dirs, names in os.walk(directory, topdown=False):
            for name in names:
                path = Path(current) / name
                relative = path.relative_to(state_root).as_posix()
                if relative in reachable:
                    continue
                try:
                    info = path.stat()
                except OSError:
                    continue
                if info.st_mtime >= cutoff:
                    continue
                try:
                    path.unlink()
                except OSError:
                    continue
                removed += 1
                removed_bytes += info.st_size
            try:
                os.rmdir(current)
            except OSError:
                pass
    return {"removed": removed, "bytes": removed_bytes, "reachable": len(reachable)}


def refresh_nightly(
    repo: Union[str, Path],
    refresh_date: str,
    *,
    output_root: Optional[Union[str, Path]] = None,
) -> Dict[str, Any]:
    """Rebuild and atomically publish the nightly tutoring context."""

    repo_root = _resolve_repo(repo)
    refresh_date = _validate_date(refresh_date)
    state_root = _resolve_state_root(repo_root, output_root)
    with _refresh_lock(state_root):
        snapshot = _compute_snapshot(repo_root, refresh_date)
        for ref, value in snapshot["point_artifacts"]["objects"].items():
            path = state_root / ref
            if path.exists():
                _regular_stat(path, "point artifact")
                if _sha256_file(path) != Path(ref).stem:
                    # This is a deterministic derived object under its proven
                    # content address; repair corruption from the fresh build.
                    _atomic_write(path, _canonical_bytes(value))
            _, digest = _content_write((state_root / ref).parent, value)
            if Path(ref).stem != digest:
                raise ExamTutoringContextError("point artifact changed while publishing")
        profile_dir = state_root / "objects/profiles"
        for formal_id in sorted(snapshot["profiles"]):
            _, digest = _content_write(
                profile_dir, snapshot["profiles"][formal_id]
            )
            if digest != snapshot["profile_sha256s"][formal_id]:
                raise ExamTutoringContextError(
                    "profile digest changed while publishing %s" % formal_id
                )
        _, index_sha = _content_write(
            state_root / "objects/indexes", snapshot["index"]
        )
        _, knowledge_sha = _content_write(
            state_root / "objects/knowledge", snapshot["knowledge_index"]
        )
        _, receipt_sha = _content_write(
            state_root / "receipts", snapshot["receipt"]
        )
        _, history_sources_sha = _content_write(
            state_root / HISTORY_SOURCES_DIR, snapshot["history_sources_object"]
        )
        _, manifest_sha = _content_write(
            state_root / "manifests", snapshot["manifest"]
        )
        if (
            index_sha != snapshot["manifest"]["index_sha256"]
            or knowledge_sha != snapshot["manifest"]["knowledge_index_sha256"]
            or receipt_sha != snapshot["receipt_sha256"]
            or history_sources_sha != snapshot["history_sources_sha256"]
            or manifest_sha != snapshot["manifest_sha256"]
        ):
            raise ExamTutoringContextError("artifact digest changed while publishing")
        for relative, markdown in snapshot["point_artifacts"]["pages"].items():
            page = repo_root / relative
            raw = markdown.encode("utf-8")
            if not page.is_file() or page.read_bytes() != raw:
                _atomic_write(page, raw)
        # Refresh the rebuildable concept navigation with the current formal membership.
        if (repo_root / "知识点标签表.md").is_file():
            from concept_review_408 import materialize
            materialize(repo_root)
        # This is deliberately the final write.  All refs already exist and are
        # fsynced, so interruption cannot expose a partial new generation.
        _atomic_write(state_root / MANIFEST_FILENAME, _canonical_bytes(snapshot["manifest"]))
        pruned = _prune_unreachable_artifacts(state_root, snapshot["manifest"])

    return {
        "schema": RECEIPT_SCHEMA,
        "status": "refreshed",
        "refresh_date": refresh_date,
        "profile_count": len(snapshot["profiles"]),
        "knowledge_point_count": snapshot["point_artifacts"]["index"]["coverage"]["point_count"],
        "learning_path_count": len(snapshot["point_artifacts"]["index"]["points"]),
        "teaching_view_count": sum(len(entry["teaching_views"]) for entry in snapshot["point_artifacts"]["index"]["points"].values()),
        "learning_record_count": snapshot["point_artifacts"]["index"]["coverage"]["record_count"],
        "receipt_sha256": snapshot["receipt_sha256"],
        "receipt_ref": snapshot["manifest"]["receipt_ref"],
        "manifest_sha256": snapshot["manifest_sha256"],
        "manifest_ref": "manifests/%s.json" % snapshot["manifest_sha256"],
        "manifest_path": str(state_root / MANIFEST_FILENAME),
        "model_call_count": 0,
        "formal_write_count": 0,
        "pruned_artifacts": pruned,
    }


def _load_manifest(state_root: Path) -> Tuple[Dict[str, Any], str]:
    path = state_root / MANIFEST_FILENAME
    manifest = _read_object(path, "exam tutoring manifest", max_bytes=MAX_MANIFEST_BYTES)
    if manifest.get("schema") != MANIFEST_SCHEMA:
        raise ExamTutoringContextError("exam tutoring manifest schema mismatch")
    digest = _sha256_file(path)
    content_path = state_root / "manifests" / (digest + ".json")
    stored = _read_object(
        content_path, "content-addressed manifest", max_bytes=MAX_MANIFEST_BYTES
    )
    if stored != manifest or _sha256_file(content_path) != digest:
        raise ExamTutoringContextError("fixed manifest is not content-addressed")
    return manifest, digest


def _guard_unchanged(path: Path, guard: Mapping[str, Any], label: str) -> bool:
    try:
        info = _regular_stat(path, label)
    except ExamTutoringContextError:
        return False
    return (
        info.st_size == guard.get("size")
        and info.st_mtime_ns == guard.get("mtime_ns")
        and info.st_ctime_ns == guard.get("ctime_ns")
    )


def _review_ledger_guard(
    path: Path, guard: Mapping[str, Any]
) -> Tuple[str, int]:
    try:
        info = _regular_stat(path, "review ledger")
    except ExamTutoringContextError:
        return "drift", 0
    old_size = guard.get("size")
    if not isinstance(old_size, int) or old_size < 0:
        return "drift", 0
    if (info.st_size == old_size and info.st_mtime_ns == guard.get("mtime_ns")
            and info.st_ctime_ns == guard.get("ctime_ns")):
        return "current", 0
    if info.st_size <= old_size:
        return "drift", 0
    if guard.get("ends_with_newline") is not True:
        return "drift", 0
    required = {
        "head_bytes",
        "head_sha256",
        "tail_start",
        "tail_bytes",
        "tail_sha256",
    }
    if not required.issubset(guard):
        return "drift", 0
    try:
        with path.open("rb") as handle:
            head = handle.read(int(guard["head_bytes"]))
            handle.seek(int(guard["tail_start"]))
            tail = handle.read(int(guard["tail_bytes"]))
    except (OSError, TypeError, ValueError):
        return "drift", 0
    if (
        _sha256_bytes(head) != guard["head_sha256"]
        or _sha256_bytes(tail) != guard["tail_sha256"]
    ):
        return "drift", 0
    return "append_suffix", info.st_size - old_size


def invalidate_after_commit(repo: Union[str, Path], event_id: str) -> Dict[str, Any]:
    """Durably revoke the old cache without altering any learning evidence."""
    root = _resolve_repo(repo)
    marker = root / DEFAULT_STATE_REL / INVALIDATION_FILENAME
    value = {"schema": FRESHNESS_CONTRACT, "event_id": str(event_id)}
    ledger = root / REVIEW_LEDGER_REL
    if ledger.is_file():
        info = ledger.stat()
        value["ledger_size"] = info.st_size
        value["ledger_mtime_ns"] = info.st_mtime_ns
    raw = _canonical_bytes(value)
    if not marker.is_file() or marker.read_bytes() != raw:
        _atomic_write(marker, raw, mode=0o600)
    return {"status": "invalidated", "event_id": event_id}


def _freshness_reasons(repo: Path, manifest: Mapping[str, Any]) -> List[str]:
    if manifest.get("freshness_contract") != FRESHNESS_CONTRACT:
        return ["freshness_contract_upgrade_required"]
    builder = manifest.get("point_memory_builder") or {}
    if builder.get("version") != BUILD_VERSION:
        return ["point_memory_upgrade_required"]
    modules = builder.get("modules") or {}
    for name in ("exam_tutoring_context_408.py", "knowledge_learning_records_408.py",
                 "knowledge_history_sources_408.py", "knowledge_tutoring_query_408.py",
                 "knowledge_hot_routes_408.py", "seal_knowledge_observations_408.py",
                 "knowledge_profile_408.py", "knowledge_learning_paths_408.py",
                 "question_visual_links_408.py"):
        if modules.get(name) != _sha256_file(Path(__file__).with_name(name)):
            return ["point_memory_builder_changed"]
    guards = manifest.get("source_guards")
    if not isinstance(guards, dict):
        return ["manifest_source_guards_invalid"]
    reasons: List[str] = []
    for label, relative in (("formal_master", MASTER_FILE),
                            ("review_unit_mapping", REVIEW_UNIT_MAPPING_FILE)):
        if not _guard_unchanged(repo / relative, guards.get(label) or {}, label):
            reasons.append(label + "_drift")
    ledger_status, _ = _review_ledger_guard(repo / REVIEW_LEDGER_REL, guards.get("review_ledger") or {})
    if ledger_status != "current":
        reasons.append("review_ledger_" + ledger_status + "_requires_refresh")
    related = guards.get("related_sources")
    if not isinstance(related, dict):
        reasons.append("related_source_guards_missing")
    else:
        for relative in RELATED_SOURCE_FILES:
            guard = related.get(relative)
            path = repo / relative
            if (guard is None and path.exists()) or (guard is not None and not _guard_unchanged(path, guard, relative)):
                reasons.append("related_source_drift:" + relative)
    marker = repo / DEFAULT_STATE_REL / INVALIDATION_FILENAME
    current = _sha256_file(marker) if marker.is_file() else None
    if current != manifest.get("invalidation_sha256"):
        reasons.append("committed_source_invalidated")
    return reasons


def _cold_card_freshness_reasons(repo: Path, state: Path,
                                 manifest: Mapping[str, Any]) -> List[str]:
    """Validate every indexed card only at the cold commit/recovery boundary."""
    index = _read_content_object(
        state, manifest.get("index_ref"), manifest.get("index_sha256"),
        "exact tutoring index", max_bytes=MAX_INDEX_BYTES,
    )
    entries = index.get("profiles")
    if index.get("schema") != INDEX_SCHEMA or not isinstance(entries, dict):
        return ["exact_index_malformed"]
    for formal_id, entry in entries.items():
        if (not FORMAL_ID_RE.fullmatch(formal_id) or not isinstance(entry, dict)
                or entry.get("card_relpath") != formal_id + ".md"):
            return ["indexed_card_reference_invalid"]
        card = repo / entry["card_relpath"]
        if (not _guard_unchanged(card, entry.get("card_guard") or {}, formal_id)
                or _sha256_file(card) != entry.get("card_sha256")):
            return ["indexed_card_drift:" + formal_id]
    return []


def _cold_point_freshness_reasons(repo: Path, state: Path, manifest: Mapping[str, Any]) -> List[str]:
    index = _read_content_object(state, manifest.get("point_index_ref"),
        manifest.get("point_index_sha256"), "point memory index", max_bytes=MAX_POINT_INDEX_BYTES)
    validate_index(index)
    supplemental = collect_history_sources(repo, Repo(repo).master())
    sources = {entry["path"]: entry for entry in supplemental.get("source_commitments", [])}
    recorded = _read_content_object(
        state, manifest.get("history_sources_ref"), manifest.get("history_sources_sha256"),
        "history source registry", max_bytes=MAX_HISTORY_SOURCES_BYTES)
    if (recorded.get("schema") != HISTORY_SOURCES_SCHEMA
            or sources != recorded.get("history_sources")):
        return ["history_sources_changed"]
    references = [index["source_coverage_object"], *index["point_records"].values()]
    page_index = index.get("learning_paths_index") or {}
    if (page_index.get("relpath") != "知识点/学习路径/索引.md"
            or not (repo / page_index["relpath"]).is_file()
            or _sha256_file(repo / page_index["relpath"]) != page_index.get("sha256")):
        return ["learning_paths_index_drift"]
    for code, entry in index["points"].items():
        summary = _read_content_object(state, entry.get("ref"), entry.get("sha256"),
            "point memory summary", max_bytes=MAX_POINT_SUMMARY_BYTES)
        references.append(summary["history_object"])
        views = entry.get("teaching_views") or {}
        if set(views) != {"protected", "after_attempt", "direct"}:
            return ["prepared_teaching_views_missing"]
        references.extend(views.values())
        page_ref = entry.get("learning_path") or {}
        if page_ref.get("relpath") != f"知识点/学习路径/{code}.md":
            return ["learning_path_reference_invalid"]
        page = repo / page_ref["relpath"]
        _regular_stat(page, "knowledge learning path")
        if not page.resolve().is_relative_to(repo.resolve()) or _sha256_file(page) != page_ref.get("sha256"):
            return ["learning_path_drift"]
    for entry in references:
        path = (state / entry["ref"]).resolve()
        if not path.is_relative_to(state.resolve()) or path.name != entry["sha256"] + ".json":
            return ["point_history_reference_invalid"]
        _regular_stat(path, "point history artifact")
        if _sha256_file(path) != entry["sha256"]:
            return ["point_history_artifact_drift"]
    return []


def refresh_after_commit(repo: Union[str, Path], refresh_date: str,
                         *, output_root: Optional[Union[str, Path]] = None) -> Dict[str, Any]:
    """Refresh the existing projection at a committed change, including recovery."""
    root = _resolve_repo(repo)
    state = _resolve_state_root(root, output_root)
    try:
        manifest, _ = _load_manifest(state)
        if (not _freshness_reasons(root, manifest)
                and not _cold_card_freshness_reasons(root, state, manifest)
                and not _cold_point_freshness_reasons(root, state, manifest)):
            return {"status": "already_current", "source_version": manifest["source_version"],
                    "receipt_sha256": manifest["receipt_sha256"], "model_call_count": 0,
                    "formal_write_count": 0}
    except (ExamTutoringContextError, OSError, ValueError, KeyError):
        pass
    result = refresh_nightly(root, refresh_date, output_root=state)
    manifest, _ = _load_manifest(state)
    if (_freshness_reasons(root, manifest)
            or _cold_card_freshness_reasons(root, state, manifest)
            or _cold_point_freshness_reasons(root, state, manifest)):
        raise ExamTutoringContextError("source_changed_before_refresh_readback")
    return {**result, "source_version": manifest["source_version"]}


def _listify(value: object, label: str) -> List[str]:
    if value is None:
        return []
    values = value if isinstance(value, list) else [value]
    limit = 128 if label in {"knowledge_points", "weak_knowledge_points"} else 16
    if len(values) > limit:
        raise ExamTutoringContextError("%s accepts at most %d values" % (label, limit))
    out: List[str] = []
    for item in values:
        if not isinstance(item, str):
            raise ExamTutoringContextError("%s values must be strings" % label)
        normalized = _normalize_term(item)
        if not normalized or len(normalized) > 300:
            raise ExamTutoringContextError("%s contains an invalid term" % label)
        if normalized not in out:
            out.append(normalized)
    return out


def _validate_query(query: Mapping[str, Any]) -> Dict[str, Any]:
    unknown = set(query) - ALLOWED_QUERY_KEYS
    forbidden = set(query) & FORBIDDEN_QUERY_KEYS
    if forbidden:
        raise ExamTutoringContextError(
            "query must not contain question or answer payload fields"
        )
    if unknown:
        raise ExamTutoringContextError(
            "unsupported query keys: %s" % ", ".join(sorted(unknown))
        )
    if query.get("schema") != QUERY_INPUT_SCHEMA:
        raise ExamTutoringContextError(
            "query schema must be %s" % QUERY_INPUT_SCHEMA
        )
    stage = query.get("stage")
    if stage not in {"pre_answer", "post_answer"}:
        raise ExamTutoringContextError(
            "query stage must be pre_answer or post_answer"
        )
    subject = query.get("subject")
    if subject not in {None, "DS", "CO", "OS", "CN"}:
        raise ExamTutoringContextError(
            "query subject must be DS, CO, OS, CN, or null"
        )

    def exact_id(key: str, label: str) -> List[str]:
        value = query.get(key)
        if value is None:
            return []
        if not isinstance(value, str):
            raise ExamTutoringContextError("%s must be a string or null" % label)
        return _listify(value, label)

    def term_array(key: str, label: str) -> List[str]:
        value = query.get(key, [])
        if value is None:
            return []
        if not isinstance(value, list):
            raise ExamTutoringContextError("%s must be an array or null" % label)
        return _listify(value, label)

    formal = exact_id("formal_id", "formal_id")
    source = exact_id("source_id", "source_id")
    knowledge = term_array("knowledge_points", "knowledge_points")
    weak_knowledge = term_array("weak_knowledge_points", "weak_knowledge_points")
    mechanism = term_array("mechanism_terms", "mechanism_terms")
    ask_type = term_array("ask_type_terms", "ask_type_terms")
    max_profiles = query.get("max_profiles", MAX_QUERY_PROFILES)
    if not isinstance(max_profiles, int) or isinstance(max_profiles, bool):
        raise ExamTutoringContextError("max_profiles must be an integer")
    if max_profiles < 0:
        raise ExamTutoringContextError("max_profiles must be non-negative")
    query_max_bytes = query.get("max_bytes", DEFAULT_QUERY_BYTES)
    if not isinstance(query_max_bytes, int) or isinstance(query_max_bytes, bool):
        raise ExamTutoringContextError("max_bytes must be an integer")
    if query_max_bytes < MIN_QUERY_BYTES or query_max_bytes > MAX_QUERY_BYTES:
        raise ExamTutoringContextError(
            "max_bytes must be between %d and %d"
            % (MIN_QUERY_BYTES, MAX_QUERY_BYTES)
        )
    read_stats = query.get("read_stats", False)
    if not isinstance(read_stats, bool):
        raise ExamTutoringContextError("read_stats must be a boolean")
    return {
        "stage": stage,
        "subject": subject,
        "formal": formal,
        "source": source,
        "knowledge": knowledge,
        "weak_knowledge": weak_knowledge,
        "mechanism": mechanism,
        "ask_type": ask_type,
        "max_profiles": min(max_profiles, MAX_QUERY_PROFILES),
        "max_bytes": query_max_bytes,
        "read_stats": read_stats,
    }


def _unavailable_result(
    manifest: Optional[Mapping[str, Any]],
    reasons: Sequence[str],
    *,
    max_bytes: int,
) -> Dict[str, Any]:
    result: Dict[str, Any] = {
        "schema": QUERY_RESULT_SCHEMA,
        "status": "unavailable",
        "refresh_date": manifest.get("refresh_date") if manifest else None,
        "reasons": list(reasons),
        "profiles": [],
        "match_status": "NO_MATCH",
        "history_state": "empty",
        "snapshot_lag": {"status": "unknown", "bytes": 0},
        "correctness_policy": {
            "scope": "current_question_only",
            "current_evidence_first": True,
            "history_role": "bounded_teaching_route_only",
            "historical_signals_must_not_grade_current_answer": True,
        },
        "current_correctness_source": "current_question_only",
        "truncated": False,
        "serialized_bytes": 0,
    }
    _set_serialized_bytes(result)
    if len(_canonical_bytes(result)) > max_bytes:
        result["reasons"] = ["unavailable"]
        _set_serialized_bytes(result)
    return result


def _set_serialized_bytes(result: Dict[str, Any]) -> None:
    for _ in range(8):
        length = len(_canonical_bytes(result))
        if result.get("serialized_bytes") == length:
            return
        result["serialized_bytes"] = length


def _profile_query_projection(profile: Mapping[str, Any]) -> Dict[str, Any]:
    value = copy.deepcopy(dict(profile))
    history = value.get("history")
    if isinstance(history, dict):
        for bucket_name in ("direct", "review_unit_associated", "direct_repairs", "associated_repairs"):
            bucket = history.get(bucket_name)
            if isinstance(bucket, dict) and isinstance(bucket.get("event_refs"), list):
                refs = bucket["event_refs"]
                if len(refs) > 8:
                    bucket["event_refs"] = refs[:8]
                    bucket["event_refs_truncated"] = True
        top = history.get("top_direct_first_breaks")
        if isinstance(top, list):
            history["top_direct_first_breaks"] = top[:2]
    return value


def _teaching_actions(
    profile: Mapping[str, Any],
    *,
    exact_card_binding: Optional[Mapping[str, Any]] = None,
) -> List[Dict[str, Any]]:
    """Return only history signals that can change this one tutoring turn.

    These are deterministic routing hints, not a learner portrait or mastery
    estimate.  Every action remains subordinate to the learner's current first
    action and current-question grader evidence.
    """

    actions: List[Dict[str, Any]] = []
    if exact_card_binding is not None:
        error_entry = str(profile.get("user_error_entry") or "").strip()
        first_action = str(profile.get("redo_first_action") or "").strip()
        card_sha256 = exact_card_binding.get("card_sha256")
        if error_entry and first_action and isinstance(card_sha256, str):
            actions.append(
                {
                    "dimension": "diagnostic_question",
                    "action": "after_current_first_action_check_formal_card_boundary",
                    "formal_error_boundary_field": "user_error_entry",
                    "minimal_hint_action_field": "redo_first_action",
                    "formal_field_binding": {
                        "card_sha256": card_sha256,
                        "fields": ["用户错误入口", "复做第一动作"],
                    },
                    "does_not_establish_current_recurrence": True,
                }
            )

    history = profile.get("history")
    if not isinstance(history, dict):
        return actions
    latest_repair = (history.get("direct_repairs") or {}).get("latest")
    if isinstance(latest_repair, dict):
        actions.insert(0, {"dimension": "support_level",
            "action": "acknowledge_latest_correction_before_reusing_older_error_labels",
            "latest_correction": latest_repair,
            "does_not_establish_independent_mastery": True})
    direct = history.get("direct")
    if not isinstance(direct, dict) or int(direct.get("event_count") or 0) <= 0:
        return actions
    evidence_refs = [
        ref
        for ref in (direct.get("event_refs") or [])[:3]
        if isinstance(ref, dict)
        and isinstance(ref.get("event_id"), str)
        and isinstance(ref.get("event_sha256"), str)
    ]
    top_breaks = history.get("top_direct_first_breaks")
    if isinstance(top_breaks, list) and top_breaks:
        first = top_breaks[0]
        if isinstance(first, dict) and first.get("text"):
            actions.append(
                {
                    "dimension": "diagnostic_question",
                    "action": "after_current_first_action_check_prior_break",
                    "prior_break": first["text"],
                    "evidence_refs": (first.get("event_refs") or [])[:3],
                }
            )
    dependency = direct.get("prompt_dependency_counts")
    confidence = direct.get("confidence_counts")
    prior_prompt_dependency = (
        isinstance(dependency, dict)
        and int(dependency.get("hint_or_prompted") or 0) > 0
    )
    prior_low_confidence = (
        isinstance(confidence, dict) and int(confidence.get("low") or 0) > 0
    )
    if prior_prompt_dependency or prior_low_confidence:
        actions.append(
            {
                "dimension": "hint_start",
                "action": "current_unaided_attempt_then_minimal_cue_if_break_recurs",
                "basis": (
                    "prior_prompt_dependency"
                    if prior_prompt_dependency
                    else "prior_low_confidence"
                ),
                "evidence_refs": evidence_refs,
            }
        )
    results = direct.get("result_counts")
    if isinstance(results, dict):
        failures = sum(int(results.get(key) or 0) for key in ("wrong", "partial", "uncertain"))
        if failures >= 2:
            actions.append(
                {
                    "dimension": "representation_choice",
                    "action": "if_current_break_recurs_jump_to_minimal_worked_step_then_fade",
                    "condition": "only_after_current_break_recurs",
                    "does_not_establish_current_recurrence": True,
                    "evidence_refs": evidence_refs,
                }
            )
            actions.append(
                {
                    "dimension": "short_transfer_check",
                    "action": "one_short_same_mechanism_transfer_after_explanation",
                    "evidence_refs": evidence_refs,
                }
            )
    return actions


def _pre_answer_route_projection(
    profile: Mapping[str, Any], actions: Sequence[Mapping[str, Any]]
) -> Dict[str, Any]:
    """Remove every content-bearing historical field before answer reveal.

    Pre-answer consumers may know that a bounded route is justified, but they
    must not receive the historical boundary text, formal mechanism, redo
    action, first-break text, or any intermediate value embedded in them.
    """

    safe_actions: List[Dict[str, Any]] = []
    for action in actions:
        safe: Dict[str, Any] = {}
        for key in (
            "dimension",
            "action",
            "condition",
            "basis",
            "does_not_establish_current_recurrence",
        ):
            value = action.get(key)
            if value is not None:
                if (
                    key == "action"
                    and value == "after_current_first_action_check_prior_break"
                ):
                    value = "after_current_first_action_run_bounded_boundary_check"
                safe[key] = value
        refs = action.get("evidence_refs")
        if isinstance(refs, list):
            safe["evidence_refs"] = [
                {
                    key: ref[key]
                    for key in ("event_id", "event_sha256", "observed_date")
                    if key in ref
                }
                for ref in refs[:3]
                if isinstance(ref, dict)
            ]
        binding = action.get("formal_field_binding")
        if isinstance(binding, dict) and isinstance(
            binding.get("card_sha256"), str
        ):
            safe["formal_card_evidence_ref"] = {
                "card_sha256": binding["card_sha256"]
            }
        safe_actions.append(safe)
    return {
        "schema": PRE_ANSWER_PROFILE_SCHEMA,
        "projection_stage": "pre_answer",
        "formal_id": profile.get("formal_id"),
        "source_id": profile.get("source_id"),
        "teaching_actions": safe_actions,
        "content_fields_redacted": True,
    }


def _fit_query_result(result: Dict[str, Any], max_bytes: int) -> Dict[str, Any]:
    _set_serialized_bytes(result)
    if len(_canonical_bytes(result)) <= max_bytes:
        return result
    result["truncated"] = True
    # Wide point queries prioritize point evidence over question-profile fallback.
    if len(result.get("knowledge_summaries", [])) > 3:
        result["omitted_profile_count"] = len(result.get("profiles", []))
        result["profiles"] = []
        result["profile_count"] = 0
        result["match_evidence"] = {}
        result["why_history_changes_tutoring"] = {}
        _set_serialized_bytes(result)
        if len(_canonical_bytes(result)) <= max_bytes:
            return result
    # Compact every requested point equally; never silently take a top three.
    for level in (1, 2):
        compact_summaries(result, level)
        _set_serialized_bytes(result)
        if len(_canonical_bytes(result)) <= max_bytes:
            return result
    while len(result.get("profiles") or []) > 1:
        removed = result["profiles"].pop()
        result.get("match_evidence", {}).pop(removed.get("formal_id"), None)
        result.get("why_history_changes_tutoring", {}).pop(
            removed.get("formal_id"), None
        )
        result["omitted_profile_count"] = result.get("omitted_profile_count", 0) + 1
        result["profile_count"] = len(result["profiles"])
        _set_serialized_bytes(result)
        if len(_canonical_bytes(result)) <= max_bytes:
            return result
    if result.get("profiles"):
        profile = result["profiles"][0]
        history = profile.get("history") or {}
        for bucket_name in ("review_unit_associated", "direct", "direct_repairs", "associated_repairs"):
            bucket = history.get(bucket_name)
            if isinstance(bucket, dict):
                bucket["event_refs"] = []
                bucket["event_refs_truncated"] = bool(bucket.get("event_count"))
        if isinstance(history, dict):
            history["top_direct_first_breaks"] = (
                history.get("top_direct_first_breaks") or []
            )[:1]
        _set_serialized_bytes(result)
    if len(_canonical_bytes(result)) <= max_bytes:
        return result
    if result.get("profiles"):
        profile = result["profiles"][0]
        for key in (
            "core_mechanism",
            "fuzzy_concepts",
            "user_error_entry",
            "redo_first_action",
        ):
            profile[key] = _bounded_text(profile.get(key), 320)
        _set_serialized_bytes(result)
    if len(_canonical_bytes(result)) <= max_bytes:
        return result
    if result.get("profiles") and len(result.get("knowledge_summaries", [])) <= 3:
        compact_summaries(result, 3, preserve_evidence_fields=True)
        _set_serialized_bytes(result)
        if len(_canonical_bytes(result)) <= max_bytes:
            return result
    if result.get("profiles"):
        removed = result["profiles"].pop()
        result.get("match_evidence", {}).pop(removed.get("formal_id"), None)
        result.get("why_history_changes_tutoring", {}).pop(
            removed.get("formal_id"), None
        )
        result["omitted_profile_count"] = result.get("omitted_profile_count", 0) + 1
        result["profile_count"] = len(result["profiles"])
        result["match_status"] = "MATCH" if result.get("knowledge_summaries") else "NO_MATCH"
        result["history_state"] = "present" if result.get("knowledge_summaries") else "empty"
    _set_serialized_bytes(result)
    if len(_canonical_bytes(result)) > max_bytes and result.get("knowledge_summaries"):
        compact_summaries(result, 3)
        _set_serialized_bytes(result)
        if len(_canonical_bytes(result)) > max_bytes:
            compact_summaries(result, 4)
            _set_serialized_bytes(result)
        while len(_canonical_bytes(result)) > max_bytes and result["knowledge_summaries"]:
            omitted = result["knowledge_summaries"].pop()
            result["knowledge_coverage"]["omitted_points"].append(omitted["knowledge_point"])
            _set_serialized_bytes(result)
    if len(_canonical_bytes(result)) > max_bytes:
        return _unavailable_result(
            None, ["result_envelope_exceeds_budget"], max_bytes=max_bytes
        )
    return result


def _subpoint_teaching_result(manifest, routed, normalized, read_artifact,
                             artifact_reads, view, known_version, max_bytes):
    entries = routed["subpoint_entries"]
    version = _sha256_bytes(_canonical_bytes({"source_version": manifest["source_version"],
        "subpoints": entries, "view": view, "max_bytes": max_bytes}))
    result = {"schema": "knowledge_teaching_context_408_v1", "status": "available",
        "version": version, "source_version": manifest["source_version"], "view": view,
        "scope": "matched_subpoints_only", "requested_subpoints": list(entries),
        "requested_points": list(dict.fromkeys(v["knowledge_point"] for v in entries.values())),
        "ambiguous_terms": routed["ambiguous_terms"], "missing_points": routed["missing_points"],
        "records": [], "omitted_subpoints": [], "write_count": 0,
        "independent_mastery_inferred": False}
    if known_version == version:
        result["status"] = "unchanged"
    else:
        for key, entry in entries.items():
            ref = entry["teaching_views"][view]
            value = read_artifact(ref["ref"], ref["sha256"], "subpoint teaching summary",
                                  limit=MAX_POINT_SUMMARY_BYTES)
            if (value.get("subpoint_id") != key or value.get("view") != view
                    or value.get("knowledge_point") != entry["knowledge_point"]):
                raise ExamTutoringContextError("subpoint_teaching_identity_mismatch")
            result["records"].append(value)
    result["returned"] = len(result["records"])
    result["truncated"] = False
    if normalized["read_stats"]:
        result["read_stats"] = {"artifact_count": len(artifact_reads),
            "artifact_bytes": sum(artifact_reads.values()), "artifacts": dict(sorted(artifact_reads.items()))}
    while result["records"] and len(_canonical_bytes(result)) > max_bytes:
        omitted = result["records"].pop()
        result["omitted_subpoints"].insert(0, omitted["subpoint_id"])
        result["returned"] = len(result["records"])
        result["truncated"] = True
    if not result["records"] and result["omitted_subpoints"]:
        result["status"] = "budget_too_small"
    return result


def _prepared_teaching_result(repo_root, manifest, routed, normalized, read_artifact,
                              artifact_reads, view, known_version, max_bytes):
    requested = list(dict.fromkeys(routed["requested_points"]))
    weak = set(routed["weak_points"])
    requested.sort(key=lambda code: code not in weak)
    version = _sha256_bytes(_canonical_bytes({
        "source_version": manifest["source_version"], "query": normalized,
        "view": view, "max_bytes": max_bytes,
        "points": {code: routed["point_entries"].get(code) for code in requested},
    }))
    result = {
        "schema": "knowledge_teaching_context_408_v1", "status": "available",
        "version": version, "source_version": manifest["source_version"],
        "view": view, "requested_points": requested,
        "missing_points": list(routed["missing_points"]),
        "ambiguous_terms": list(routed["ambiguous_terms"]), "omitted_points": [],
        "records": [], "write_count": 0, "independent_mastery_inferred": False,
    }
    if known_version == version and requested:
        result["status"] = "unchanged"
    else:
        for code in requested:
            entry = routed["point_entries"].get(code) or {}
            reference = (entry.get("teaching_views") or {}).get(view)
            if not reference:
                raise ExamTutoringContextError("prepared_teaching_view_missing:" + code)
            record = read_artifact(reference.get("ref"), reference.get("sha256"),
                "prepared teaching summary", limit=MAX_POINT_SUMMARY_BYTES)
            if record.get("schema") != "knowledge_teaching_view_408_v1" or record.get("knowledge_point") != code:
                raise ExamTutoringContextError("prepared_teaching_identity_mismatch:" + code)
            record = copy.deepcopy(record)
            for question in record.get("questions", []):
                formal_id = question.get("formal_id", "")
                if not FORMAL_ID_RE.fullmatch(formal_id) or question.get("card_relpath") != formal_id + ".md":
                    raise ExamTutoringContextError("prepared_question_reference_invalid")
                card = repo_root / question["card_relpath"]
                _regular_stat(card, "prepared question card")
                if _sha256_file(card) != question.get("card_sha256"):
                    raise ExamTutoringContextError("prepared_question_card_changed:" + formal_id)
                visual = question.get("visual_note_path")
                if question.get("open_url") and (not visual or not Path(visual).is_file()
                        or Path(visual).name != formal_id + ".md"):
                    raise ExamTutoringContextError("prepared_visual_card_unavailable:" + formal_id)
                question.pop("card_relpath", None)
                question.pop("card_sha256", None)
                question.pop("visual_note_path", None)
                question.pop("visual_note_sha256", None)
            result["records"].append(record)
        if not result["records"]:
            result["status"] = "unknown_concept"
    if normalized["read_stats"]:
        result["read_stats"] = {"artifact_count": len(artifact_reads),
            "artifact_bytes": sum(artifact_reads.values()), "artifacts": dict(sorted(artifact_reads.items()))}
    result["returned"] = len(result["records"])
    result["truncated"] = False
    if len(result["records"]) > 1 and len(_canonical_bytes(result)) > max_bytes:
        # Keep every point's meaning, personal evidence and page link before
        # sacrificing point coverage to repeated per-question metadata.
        from knowledge_learning_paths_408 import _excerpt
        for record in result["records"]:
            if "short_summary" not in record:
                continue
            record["short_summary"] = _excerpt(record["short_summary"], 1100 if record["knowledge_point"] in weak else 800)
            record["questions"] = [{"formal_id": question["formal_id"], "obsidian_uri": question.get("obsidian_uri")}
                                   for question in record.get("questions", [])]
            record["compact_for_multi_point"] = True
    while result["records"] and len(_canonical_bytes(result)) > max_bytes:
        omitted = result["records"].pop()
        result["omitted_points"].insert(0, omitted["knowledge_point"])
        result["returned"] = len(result["records"])
        result["truncated"] = True
    if not result["records"] and result["omitted_points"]:
        result["status"] = "budget_too_small"
    if len(_canonical_bytes(result)) > max_bytes:
        return _unavailable_result(manifest, ["teaching_envelope_exceeds_budget"], max_bytes=max_bytes)
    return result


def query_context(
    repo: Union[str, Path],
    query: Mapping[str, Any],
    *,
    output_root: Optional[Union[str, Path]] = None,
    max_bytes: int = DEFAULT_QUERY_BYTES,
    teaching_view: Optional[str] = None,
    known_version: Optional[str] = None,
) -> Dict[str, Any]:
    """Read only bounded materialized artifacts and return up to three profiles."""

    if not isinstance(query, Mapping):
        raise ExamTutoringContextError("query input must be a JSON object")
    if not isinstance(max_bytes, int) or isinstance(max_bytes, bool):
        raise ExamTutoringContextError("max_bytes must be an integer")
    if max_bytes < MIN_QUERY_BYTES or max_bytes > MAX_QUERY_BYTES:
        raise ExamTutoringContextError(
            "max_bytes must be between %d and %d"
            % (MIN_QUERY_BYTES, MAX_QUERY_BYTES)
        )
    normalized = _validate_query(query)
    if teaching_view is not None:
        if teaching_view not in {"protected", "after_attempt", "direct"}:
            raise ExamTutoringContextError("unknown teaching view")
        if (teaching_view == "protected") != (normalized["stage"] == "pre_answer"):
            raise ExamTutoringContextError("teaching view conflicts with answer protection")
        if known_version is not None and (not isinstance(known_version, str) or not SHA256_RE.fullmatch(known_version)):
            raise ExamTutoringContextError("known-version must be a SHA-256 version")
    max_bytes = min(max_bytes, int(normalized["max_bytes"]))
    repo_root = _resolve_repo(repo)
    state_root = _resolve_state_root(repo_root, output_root)
    try:
        manifest, manifest_sha256 = _load_manifest(state_root)
    except ExamTutoringContextError as exc:
        return _unavailable_result(None, ["manifest_unavailable:%s" % exc], max_bytes=max_bytes)

    reasons = _freshness_reasons(repo_root, manifest)
    if reasons:
        return _unavailable_result(manifest, reasons, max_bytes=max_bytes)

    artifact_reads: Dict[str, int] = {}
    if normalized["read_stats"]:
        for relative in (MANIFEST_FILENAME,
                         "manifests/%s.json" % manifest_sha256):
            path = state_root / relative
            artifact_reads[relative] = _regular_stat(
                path, "query artifact"
            ).st_size

    def read_artifact(ref: object, sha256: object, label: str,
                      *, limit: int) -> Dict[str, Any]:
        value = _read_content_object(
            state_root, ref, sha256, label, max_bytes=limit)
        if normalized["read_stats"] and isinstance(ref, str):
            artifact_reads[ref] = _regular_stat(
                state_root / ref, label
            ).st_size
        return value

    def read_hot_bucket(entry: Mapping[str, str]) -> Mapping[str, Any]:
        return read_artifact(
            entry.get("ref"), entry.get("sha256"), "knowledge hot-route bucket",
            limit=MAX_HOT_ROUTE_BUCKET_BYTES)

    try:
        routed = resolve_hot_routes(
            manifest.get("knowledge_hot_routes") or {}, normalized,
            read_hot_bucket,
            include_profiles=teaching_view is None and normalized["max_profiles"] > 0)
    except (ExamTutoringContextError, ValueError, KeyError, TypeError) as exc:
        return _unavailable_result(
            manifest, ["knowledge_hot_routes_unavailable:%s" % exc],
            max_bytes=max_bytes)

    entries = {
        formal_id: route["profile"]
        for formal_id, route in routed["formal_routes"].items()
    }
    source_lookup = routed["source_lookup"]
    if normalized["formal"] and normalized["source"]:
        formal_id = normalized["formal"][0].upper()
        source_matches = source_lookup.get(normalized["source"][0], [])
        if formal_id not in entries or not source_matches:
            return _unavailable_result(
                manifest, ["exact_identity_unresolved"], max_bytes=max_bytes
            )
        if not isinstance(source_matches, list) or set(source_matches) != {formal_id}:
            return _unavailable_result(
                manifest, ["exact_identity_ambiguous"], max_bytes=max_bytes
            )
    try:
        exact_formals = routed["exact_formals"]
        for formal_id in exact_formals:
            entry = entries[formal_id]
            if (entry.get("card_relpath") != formal_id + ".md" or
                    not _guard_unchanged(repo_root / (formal_id + ".md"), entry.get("card_guard") or {}, formal_id)):
                return _unavailable_result(manifest, ["selected_card_drift:" + formal_id], max_bytes=max_bytes)
        if routed.get("subpoint_entries"):
            return _subpoint_teaching_result(manifest, routed, normalized, read_artifact,
                artifact_reads, teaching_view or ("protected" if normalized["stage"] == "pre_answer" else "direct"),
                known_version, max_bytes)
        if teaching_view is not None:
            return _prepared_teaching_result(repo_root, manifest, routed, normalized,
                read_artifact, artifact_reads, teaching_view, known_version, max_bytes)
        point_query = dict(normalized)
        point_query["resolved_formal_ids"] = exact_formals
        summaries, coverage = project_points(
            routed["requested_points"], routed["missing_points"],
            routed["weak_points"], routed["source_coverage"], point_query,
            routed["point_entries"],
            lambda entry: read_artifact(
                entry.get("ref"), entry.get("sha256"),
                "point memory summary", limit=MAX_POINT_SUMMARY_BYTES),
            ambiguous_terms=routed["ambiguous_terms"])
    except (ExamTutoringContextError, ValueError, KeyError, TypeError) as exc:
        return _unavailable_result(manifest, ["point_memory_unavailable:%s" % exc], max_bytes=max_bytes)
    score = routed["score"]
    match_keys = routed["match_keys"]

    selected = [
        formal_id
        for formal_id, _ in sorted(
            score.items(), key=lambda item: (-item[1], item[0])
        )[: normalized["max_profiles"]]
    ]
    profiles: List[Dict[str, Any]] = []
    selected_drift: List[str] = []
    for formal_id in selected:
        entry = entries.get(formal_id)
        if not isinstance(entry, dict):
            return _unavailable_result(
                manifest, ["selected_profile_index_malformed"], max_bytes=max_bytes
            )
        card_relpath = entry.get("card_relpath")
        if not isinstance(card_relpath, str) or card_relpath != formal_id + ".md":
            return _unavailable_result(
                manifest, ["selected_card_reference_invalid"], max_bytes=max_bytes
            )
        if not _guard_unchanged(
            repo_root / card_relpath,
            entry.get("card_guard") or {},
            "selected safe card",
        ):
            selected_drift.append(formal_id)
            continue
        if _sha256_file(repo_root / card_relpath) != entry.get("card_sha256"):
            selected_drift.append(formal_id)
            continue
        try:
            profile = read_artifact(
                entry.get("profile_ref"), entry.get("profile_sha256"),
                "selected tutoring profile", limit=MAX_PROFILE_BYTES)
        except ExamTutoringContextError as exc:
            return _unavailable_result(
                manifest, ["selected_profile_unavailable:%s" % exc], max_bytes=max_bytes
            )
        if profile.get("schema") != PROFILE_SCHEMA or profile.get("formal_id") != formal_id:
            return _unavailable_result(
                manifest, ["selected_profile_identity_mismatch"], max_bytes=max_bytes
            )
        projected = _profile_query_projection(profile)
        exact_match = bool(
            match_keys.get(formal_id, set()) & {"formal_id", "source_id"}
        )
        actions = _teaching_actions(
            projected,
            exact_card_binding=entry if exact_match else None,
        )
        if actions:
            if normalized["stage"] == "pre_answer":
                profiles.append(_pre_answer_route_projection(projected, actions))
            else:
                projected["teaching_actions"] = actions
                profiles.append(projected)
    if selected_drift:
        return _unavailable_result(
            manifest,
            ["selected_card_drift:%s" % ",".join(selected_drift)],
            max_bytes=max_bytes,
        )

    returned_ids = [str(profile.get("formal_id")) for profile in profiles]
    why_history_changes_tutoring = {
        str(profile["formal_id"]): {
            "action_dimensions": [action.get("dimension") for action in profile.get("teaching_actions", [])],
            "actions_location": "profiles.teaching_actions",
        }
        for profile in profiles
    }
    result: Dict[str, Any] = {
        "schema": QUERY_RESULT_SCHEMA,
        "status": "ready",
        "query_stage": normalized["stage"],
        "subject": normalized["subject"],
        "refresh_date": manifest.get("refresh_date"),
        "receipt_sha256": manifest.get("receipt_sha256"),
        "projection_build_sha256": manifest_sha256,
        "projection_build_schema": MANIFEST_SCHEMA,
        "source_version": manifest.get("source_version"),
        "profiles": profiles,
        "knowledge_summaries": summaries,
        "knowledge_coverage": coverage,
        "profile_count": len(profiles),
        "match_status": "MATCH" if profiles or summaries else "NO_MATCH",
        "history_state": "present" if profiles or summaries else "empty",
        "snapshot_lag": {
            "status": "current",
            "bytes": 0,
            "refresh_required_at_next_nightly_closeout": False,
        },
        "match_evidence": {
            formal_id: {
                "matched_key_types": sorted(match_keys[formal_id]),
            }
            for formal_id in returned_ids
        },
        "why_history_changes_tutoring": why_history_changes_tutoring,
        "correctness_policy": {
            "scope": "current_question_only",
            "current_evidence_first": True,
            "history_role": "bounded_teaching_route_only",
            "historical_signals_must_not_grade_current_answer": True,
        },
        "current_correctness_source": "current_question_only",
        "budgets": {
            "max_profiles": normalized["max_profiles"],
            "max_bytes": max_bytes,
        },
        "truncated": False,
        "serialized_bytes": 0,
    }
    if normalized["read_stats"]:
        result["read_stats"] = {
            "scope": "materialized_json_artifacts",
            "path_count": len(artifact_reads),
            "bytes": sum(artifact_reads.values()),
            "paths": [
                {"path": path, "bytes": artifact_reads[path]}
                for path in sorted(artifact_reads)
            ],
        }
    return _fit_query_result(result, max_bytes)


def audit_receipt(
    repo: Union[str, Path],
    receipt_sha256: str,
    *,
    output_root: Optional[Union[str, Path]] = None,
) -> Dict[str, Any]:
    """Cold full recomputation of one exact nightly refresh receipt."""

    if not SHA256_RE.fullmatch(receipt_sha256 or ""):
        raise ExamTutoringContextError("receipt sha256 is malformed")
    repo_root = _resolve_repo(repo)
    state_root = _resolve_state_root(repo_root, output_root)
    receipt_path = state_root / "receipts" / (receipt_sha256 + ".json")
    receipt = _read_object(
        receipt_path, "exam tutoring refresh receipt", max_bytes=MAX_RECEIPT_BYTES
    )
    if _sha256_file(receipt_path) != receipt_sha256:
        raise ExamTutoringContextError("refresh receipt content hash mismatch")
    if receipt.get("schema") != RECEIPT_SCHEMA:
        raise ExamTutoringContextError("refresh receipt schema mismatch")
    refresh_date = _validate_date(str(receipt.get("refresh_date") or ""))
    recomputed = _compute_snapshot(repo_root, refresh_date, verify_originals=True)
    if recomputed["receipt_sha256"] != receipt_sha256 or recomputed["receipt"] != receipt:
        raise ExamTutoringContextError("nightly receipt drift detected")

    # Verify the published artifacts rather than trusting the receipt alone.
    index_sha = recomputed["manifest"]["index_sha256"]
    knowledge_sha = recomputed["manifest"]["knowledge_index_sha256"]
    stored_index = _read_object(
        state_root / "objects/indexes" / (index_sha + ".json"),
        "published exact tutoring index",
        max_bytes=MAX_INDEX_BYTES,
    )
    stored_knowledge = _read_object(
        state_root / "objects/knowledge" / (knowledge_sha + ".json"),
        "published knowledge tutoring index",
        max_bytes=MAX_KNOWLEDGE_INDEX_BYTES,
    )
    if stored_index != recomputed["index"] or _sha256_file(
        state_root / "objects/indexes" / (index_sha + ".json")
    ) != index_sha:
        raise ExamTutoringContextError("published exact index drift detected")
    if stored_knowledge != recomputed["knowledge_index"] or _sha256_file(
        state_root / "objects/knowledge" / (knowledge_sha + ".json")
    ) != knowledge_sha:
        raise ExamTutoringContextError("published knowledge index drift detected")
    for ref, expected in recomputed["point_artifacts"]["objects"].items():
        path = state_root / ref
        _regular_stat(path, "published point memory artifact")
        if _sha256_file(path) != Path(ref).stem or path.read_bytes() != _canonical_bytes(expected):
            raise ExamTutoringContextError("published point memory artifact drift detected: " + ref)
    for relative, markdown in recomputed["point_artifacts"]["pages"].items():
        page = repo_root / relative
        _regular_stat(page, "published knowledge learning path")
        if page.read_bytes() != markdown.encode("utf-8"):
            raise ExamTutoringContextError("published learning path drift detected: " + relative)
    for formal_id, profile in recomputed["profiles"].items():
        digest = recomputed["profile_sha256s"][formal_id]
        path = state_root / "objects/profiles" / (digest + ".json")
        stored = _read_object(
            path, "published tutoring profile", max_bytes=MAX_PROFILE_BYTES
        )
        if stored != profile or _sha256_file(path) != digest:
            raise ExamTutoringContextError(
                "published profile drift detected: %s" % formal_id
            )

    manifest, manifest_sha = _load_manifest(state_root)
    if manifest.get("receipt_sha256") != receipt_sha256:
        raise ExamTutoringContextError("current manifest does not bind the audited receipt")
    if manifest != recomputed["manifest"] or manifest_sha != recomputed["manifest_sha256"]:
        raise ExamTutoringContextError("published manifest drift detected")
    return {
        "schema": AUDIT_RESULT_SCHEMA,
        "status": "pass",
        "refresh_date": refresh_date,
        "receipt_sha256": receipt_sha256,
        "manifest_sha256": manifest_sha,
        "profile_count": len(recomputed["profiles"]),
        "full_recompute": True,
        "model_call_count": 0,
        "formal_write_count": 0,
    }


def _load_input_json(value: str) -> Dict[str, Any]:
    if value == "-":
        payload = sys.stdin.buffer.read(MAX_INPUT_BYTES + 1)
    else:
        candidate = Path(value)
        try:
            candidate_exists = candidate.exists()
        except OSError:
            candidate_exists = False
        if candidate_exists:
            info = _regular_stat(candidate, "query input")
            if info.st_size > MAX_INPUT_BYTES:
                raise ExamTutoringContextError("query input exceeds its bounded size")
            payload = candidate.read_bytes()
        else:
            payload = value.encode("utf-8")
    if len(payload) > MAX_INPUT_BYTES:
        raise ExamTutoringContextError("query input exceeds its bounded size")
    try:
        parsed = json.loads(payload.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ExamTutoringContextError("query input is not valid UTF-8 JSON") from exc
    if not isinstance(parsed, dict):
        raise ExamTutoringContextError("query input must be a JSON object")
    return parsed


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Build or query the answer-safe 408 exam tutoring context"
    )
    sub = parser.add_subparsers(dest="command", required=True)

    for name in ("refresh-nightly", "refresh-current"):
        refresh = sub.add_parser(name)
        refresh.add_argument("--repo", default=".")
        refresh.add_argument("--refresh-date", required=True)
        refresh.add_argument("--output-root")

    query = sub.add_parser("query")
    query.add_argument("--repo", default=".")
    query.add_argument("--input-json", required=True)
    query.add_argument("--output-root")
    query.add_argument("--max-bytes", type=int, default=DEFAULT_QUERY_BYTES)

    resolve = sub.add_parser("resolve", help="Read precomputed knowledge-point teaching summaries")
    resolve.add_argument("--repo", default=".")
    resolve.add_argument("--concept", action="append", default=[])
    resolve.add_argument("--weak-concept", action="append", default=[])
    resolve.add_argument("--formal-id")
    resolve.add_argument("--source-id")
    resolve.add_argument("--view", choices=("protected", "after_attempt", "direct"), default="protected")
    resolve.add_argument("--known-version")
    resolve.add_argument("--max-bytes", type=int, default=4500)
    resolve.add_argument("--read-stats", action="store_true")
    resolve.add_argument("--output-root")

    audit = sub.add_parser("audit")
    audit.add_argument("--repo", default=".")
    audit.add_argument("--receipt-sha256", required=True)
    audit.add_argument("--output-root")
    return parser


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = _parser().parse_args(argv)
    try:
        if args.command in {"refresh-nightly", "refresh-current"}:
            refresh = refresh_nightly if args.command == "refresh-nightly" else refresh_after_commit
            result = refresh(
                args.repo,
                args.refresh_date,
                output_root=args.output_root,
            )
        elif args.command == "resolve":
            payload = {"schema": QUERY_INPUT_SCHEMA,
                "stage": "pre_answer" if args.view == "protected" else "post_answer",
                "knowledge_points": args.concept, "weak_knowledge_points": args.weak_concept,
                "max_profiles": 0, "max_bytes": args.max_bytes, "read_stats": args.read_stats}
            if args.formal_id:
                payload["formal_id"] = args.formal_id
            if args.source_id:
                payload["source_id"] = args.source_id
            result = query_context(args.repo, payload, output_root=args.output_root,
                max_bytes=args.max_bytes, teaching_view=args.view, known_version=args.known_version)
        elif args.command == "query":
            result = query_context(
                args.repo,
                _load_input_json(args.input_json),
                output_root=args.output_root,
                max_bytes=args.max_bytes,
            )
        else:
            result = audit_receipt(
                args.repo,
                args.receipt_sha256,
                output_root=args.output_root,
            )
    except ExamTutoringContextError as exc:
        print(
            json.dumps(
                {"status": "error", "error": str(exc)},
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            ),
            file=sys.stderr,
        )
        return 2
    sys.stdout.buffer.write(_canonical_bytes(result))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
