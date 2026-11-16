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
from question_visual_templates_408 import (MANIFEST_REL as QUESTION_VISUAL_MANIFEST,
    read_template as read_question_visual, source_state as question_visual_source_state, valid_identity,
    observed_guards_current as question_visual_guards_current)
from visual_teaching_templates_408 import CATALOG_FILES as VISUAL_CATALOG_FILES
from source_visual_408 import MANIFEST_REL as SOURCE_VISUAL_MANIFEST
from visual_teaching_templates_408 import source_guards_current as visual_sources_current
from visual_teaching_templates_408 import redact_changed_source as redact_visual_source
import teaching_memory_408 as teaching_memory

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
                        "support/syllabus-2027/context.json", "support/syllabus-2027/catalog.json", "support/syllabus-2027/source.txt",
                        "support/syllabus-2027/bindings-DS.json", "support/syllabus-2027/bindings-CO.json",
                        "support/syllabus-2027/bindings-OS.json", "support/syllabus-2027/bindings-CN.json",
                        "support/syllabus-2027/exam-frequency.json",
                        "support/syllabus-2027/exam-subquestions.json",
                        "support/syllabus-2027/frequency-primary-anchor-review.json",
                        "support/syllabus-2027/exam-corpus-acceptance.json",
                        "support/syllabus-2027/question-frequency.json",
                        "support/syllabus-2027/exam-review-DS.json", "support/syllabus-2027/exam-review-CO.json",
                        "support/syllabus-2027/exam-review-OS.json", "support/syllabus-2027/exam-review-CN.json",
                        "support/syllabus-2027/point-question-links.json",
                        "support/syllabus-2027/question-syllabus-map.json",
                        "support/syllabus-2027/reviewed-teaching-relations.json",
                        "support/syllabus-2027/reviewed-subpoint-scopes.json",
                        ".private/knowledge-profiles-v1/current.json", "support/interactive-408/scene.html",
                        "support/semantic-408/lesson.html") + VISUAL_CATALOG_FILES + (QUESTION_VISUAL_MANIFEST.as_posix(), SOURCE_VISUAL_MANIFEST.as_posix())
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
# Complete evidence reads retain their established layout and cache contract.
TEACHING_DELIVERY_POLICY = "complete-personal-scope-v2"
# Display-only rendering is not a dependency of personal evidence. Keep the
# projection contract and actual materializers in the snapshot guard.
POINT_MEMORY_MODULES = (
    "exam_tutoring_context_408.py", "teaching_memory_408.py", "memory_access_408.py", "memory_retrieval_408.py",
    "memory_evidence_search_408.py", "memory_episode_source_408.py", "memory_discovery_408.py",
    "memory_semantic_precision_408.py", "memory_semantic_precision_408.mjs",
    "conversation_package_408.py", "knowledge_learning_records_408.py",
    "knowledge_history_sources_408.py", "runtime_paths_408.py", "knowledge_tutoring_query_408.py",
    "knowledge_hot_routes_408.py", "seal_knowledge_observations_408.py", "knowledge_profile_408.py",
    "knowledge_learning_paths_408.py", "question_visual_links_408.py", "syllabus_context_408.py",
    "exam_point_links_408.py", "visual_teaching_templates_408.py", "question_visual_templates_408.py",
    "interactive_teaching_408.py", "public_scene_408.py", "source_visual_408.py",
    "semantic_teaching_408.py", "teaching_research_408.py", "lesson_alignment_408.py",
    "scoped_question_sources_408.py", "exam_figure_binding_408.py",
)
PERSONAL_CONTEXT_MAX_BYTES = 1024 * 1024
PERSONAL_EVIDENCE_MAX_BYTES = 16 * 1024 * 1024
MAX_INPUT_BYTES = 64 * 1024
MAX_PROFILE_BYTES = 24 * 1024
MAX_INDEX_BYTES = 4 * 1024 * 1024
MAX_KNOWLEDGE_INDEX_BYTES = 4 * 1024 * 1024
MAX_MANIFEST_BYTES = 2 * 1024 * 1024
MAX_RECEIPT_BYTES = 8 * 1024 * 1024  # Cold receipts include the full exact-question source closure; never read on the learner path.
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
    "exam_question_id",
    "current_question_id",
    "current_question_part",
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


_EVIDENCE_FACT_CACHE: Dict[str, Any] = {}


def _facts_inventory_signature(repo_root: Path) -> str:
    from knowledge_history_sources_408 import _inventory, CANONICAL_REPO, implementation_sha256
    paths = _inventory(repo_root, repo_root == CANONICAL_REPO.resolve())
    return _sha256_bytes(_canonical_bytes({
        "inventory": [_source_guard(path, "fact inventory") for path in paths],
        "collector_implementation": implementation_sha256(),
        "record_builder": _sha256_file(Path(__file__).with_name("knowledge_learning_records_408.py")),
        "snapshot_builder": _sha256_file(Path(__file__)),
    }))


def _compute_snapshot_facts(repo_root: Path, refresh_date: str, *, verify_originals: bool = False):
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
    return {"master": master, "cards": cards, "index": index,
            "knowledge_index": knowledge_index, "profiles": profiles,
            "profile_sha256s": profile_sha256s, "supplemental": supplemental,
            "memory": memory, "ledger_path": ledger_path, "line_count": line_count}


def _reuse_evidence_facts(repo_root, refresh_date, before, invalidation_sha):
    cached = _EVIDENCE_FACT_CACHE.get(str(repo_root))
    if not cached or cached["refresh_date"] != refresh_date:
        return None
    fact_before = {key: value for key, value in before.items()
                   if key != ".private/knowledge-profiles-v1/current.json"}
    if cached["before"] != fact_before or cached["invalidation_sha256"] != invalidation_sha:
        return None
    facts = cached["facts"]
    try:
        if cached["inventory_sha256"] != _facts_inventory_signature(repo_root):
            return None
        if any(not _guard_unchanged(repo_root / card["relpath"], card["guard"], fid)
               for fid, card in facts["cards"].items()):
            return None
        if any(not _guard_unchanged(Path(guard["path"]), guard, "cached fact source")
               for guard in facts["supplemental"].get("source_guards", [])):
            return None
    except (OSError, ValueError, KeyError, ExamTutoringContextError):
        return None
    return copy.deepcopy(facts)


def _compute_snapshot(repo_root: Path, refresh_date: str, *, verify_originals: bool = False,
                      evidence_only: bool = False) -> Dict[str, Any]:
    repo_root = _resolve_repo(repo_root)
    watched = [MASTER_FILE, REVIEW_UNIT_MAPPING_FILE, str(REVIEW_LEDGER_REL)]
    watched += [name for name in RELATED_SOURCE_FILES if (repo_root / name).is_file()]
    before = {name: _source_guard(repo_root / name, name) for name in watched}
    inventory_before = _facts_inventory_signature(repo_root) if evidence_only else None
    invalidation_path = repo_root / DEFAULT_STATE_REL / INVALIDATION_FILENAME
    invalidation_sha = _sha256_file(invalidation_path) if invalidation_path.is_file() else None
    facts = None if verify_originals else _reuse_evidence_facts(repo_root, refresh_date, before, invalidation_sha)
    facts_reused = facts is not None
    if facts is None:
        facts = _compute_snapshot_facts(repo_root, refresh_date, verify_originals=verify_originals)
    repo = Repo(repo_root)
    master, cards = facts["master"], facts["cards"]
    index, knowledge_index = facts["index"], facts["knowledge_index"]
    profiles, profile_sha256s = facts["profiles"], facts["profile_sha256s"]
    supplemental, memory = facts["supplemental"], facts["memory"]
    ledger_path, line_count = facts["ledger_path"], facts["line_count"]
    if evidence_only:
        # The profile writer needs source-bound facts, not a second complete
        # visual/teaching build using the profile it is about to replace.
        for guard in supplemental.get("source_guards", []):
            if not _guard_unchanged(Path(guard["path"]), guard, "history source"):
                raise ExamTutoringContextError("history_source_changed_during_evidence_read")
        for name, guard in before.items():
            if not _guard_unchanged(repo_root / name, guard, name):
                raise ExamTutoringContextError("source_changed_during_evidence_read:" + name)
        for formal_id, card in cards.items():
            if not _guard_unchanged(repo_root / card["relpath"], card["guard"], formal_id):
                raise ExamTutoringContextError("card_changed_during_evidence_read:" + formal_id)
        if invalidation_sha != (_sha256_file(invalidation_path) if invalidation_path.is_file() else None):
            raise ExamTutoringContextError("commit_changed_during_evidence_read")
        if inventory_before != _facts_inventory_signature(repo_root):
            raise ExamTutoringContextError("source_inventory_changed_during_evidence_read")
        points = {}
        for code, raw in memory["points"].items():
            keys = list(raw["record_keys"]) + list(raw["unknown_date_record_keys"])
            value = {"schema": memory["schema"], "knowledge_point": code,
                     "records": {key: memory["records"][key] for key in keys}}
            points[code] = {"records": value, "sha256": _sha256_bytes(_canonical_bytes(value)),
                            "record_count": len(keys)}
        # Keep only one operation's immutable fact projection in this process.
        # Reuse is allowed across the profile-pointer update, never across a
        # learning, correction, source-inventory, or source-content change.
        _EVIDENCE_FACT_CACHE.clear()
        _EVIDENCE_FACT_CACHE[str(repo_root)] = {
            "refresh_date": refresh_date,
            "before": {key: value for key, value in before.items()
                       if key != ".private/knowledge-profiles-v1/current.json"},
            "invalidation_sha256": invalidation_sha,
            "inventory_sha256": inventory_before,
            "facts": copy.deepcopy(facts),
        }
        return {"schema": "cs408-evidence-snapshot-v1", "points": points,
                "source_guards": before, "history_source_guards": supplemental.get("source_guards", []),
                "invalidation_sha256": invalidation_sha, "model_call_count": 0,
                "formal_write_count": 0, "teaching_artifact_build_count": 0}
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
    from memory_access_408 import build_catalog
    exam_links_path = repo_root / "support/syllabus-2027/point-question-links.json"
    memory_catalog = build_catalog(point_artifacts, index,
        exam_links=json.loads(exam_links_path.read_bytes()) if exam_links_path.is_file() else None,
        formal_relations=repo.edges() if (repo_root / "关系边表.md").is_file() else None, repo=repo_root)
    point_artifacts["objects"].update(memory_catalog["objects"])
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
    source_commitments["visual_template_sources"] = {
        guard["path"]: guard
        for entry in [*point_artifacts["index"]["points"].values(),
                      *point_artifacts["index"].get("syllabus_entries", {}).values()]
        for guard in entry.get("visual_template_sources", [])
    }
    source_commitments["question_visual_sources"] = {
        guard["path"]: guard
        for entry in point_artifacts["index"].get("question_visuals", {}).values()
        for guard in entry.get("source_guards", [])}
    if not question_visual_guards_current(list(source_commitments["question_visual_sources"].values())):
        raise ExamTutoringContextError("question_visual_source_changed_during_refresh")
    if not visual_sources_current(list(source_commitments["visual_template_sources"].values())):
        raise ExamTutoringContextError("visual_template_source_changed_during_refresh")
    source_commitments["point_memory_builder"] = {
        "version": BUILD_VERSION,
        "modules": {name: _sha256_file(Path(__file__).with_name(name)) for name in POINT_MEMORY_MODULES},
    }
    from teaching_research_408 import INDEX_REL as RESEARCH_INDEX_REL
    research_index = repo_root / RESEARCH_INDEX_REL
    source_commitments['teaching_research_index'] = ({**_source_guard(research_index, 'teaching research index'),
        'sha256': _sha256_file(research_index)} if research_index.is_file() else None)
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
        "memory_catalog": memory_catalog["descriptor"],
        "point_memory_builder": source_commitments["point_memory_builder"],
        "teaching_research_index": source_commitments["teaching_research_index"],
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
        "facts_reused": facts_reused,
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


def compute_evidence_snapshot(repo: Union[str, Path], refresh_date: str) -> Dict[str, Any]:
    """Read source-verified point facts without materializing teaching artifacts."""
    return _compute_snapshot(_resolve_repo(repo), _validate_date(refresh_date), evidence_only=True)


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
    preserve_history: bool = True,
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
        pruned = ({"removed": 0, "bytes": 0, "history_preserved": True}
                  if preserve_history else _prune_unreachable_artifacts(state_root, snapshot["manifest"]))

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
        "source_facts_reused": snapshot.get("facts_reused", False),
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
    for name in POINT_MEMORY_MODULES:
        if modules.get(name) != _sha256_file(Path(__file__).with_name(name)):
            return ["point_memory_builder_changed"]
    from teaching_research_408 import INDEX_REL as RESEARCH_INDEX_REL
    research_index = repo / RESEARCH_INDEX_REL
    research_guard = manifest.get('teaching_research_index')
    if ((research_guard is None) != (not research_index.is_file()) or
            (research_guard and not _guard_unchanged(research_index, research_guard, 'teaching research index'))):
        return ['teaching_research_routes_changed']
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
    # The same cold boundary is used before portable snapshot publication.
    # Validate source bindings here so a new export cannot certify stale visual
    # proposals merely because the formal cards themselves are unchanged.
    point_index = _read_content_object(state, manifest.get("point_index_ref"),
        manifest.get("point_index_sha256"), "point memory index", max_bytes=MAX_POINT_INDEX_BYTES)
    guards = {guard["path"]: guard for entry in [*point_index.get("points", {}).values(),
                                               *point_index.get("syllabus_entries", {}).values()]
              for guard in entry.get("visual_template_sources", [])}
    if not visual_sources_current(list(guards.values())):
        return ["visual_template_reference_source_changed"]
    question_guards = {g["path"]: g for e in point_index.get("question_visuals", {}).values()
                       for g in e.get("source_guards", [])}
    if not question_visual_guards_current(list(question_guards.values())):
        return ["question_visual_reference_source_changed"]
    return []


def _cold_point_freshness_reasons(repo: Path, state: Path, manifest: Mapping[str, Any]) -> List[str]:
    index = _read_content_object(state, manifest.get("point_index_ref"),
        manifest.get("point_index_sha256"), "point memory index", max_bytes=MAX_POINT_INDEX_BYTES)
    validate_index(index)
    from knowledge_profile_408 import teaching_plan_guards_current
    for entry in [*index["points"].values(), *(index.get("subpoints") or {}).values(),
                  *(index.get("syllabus_entries") or {}).values()]:
        if not teaching_plan_guards_current(entry.get("teaching_plan_sources") or []):
            return ["teaching_plan_original_source_changed"]
        if not visual_sources_current(entry.get("visual_template_sources") or []):
            return ["visual_template_reference_source_changed"]
    for entry in index.get("question_visuals", {}).values():
        if not question_visual_guards_current(entry.get("source_guards") or []):
            return ["question_visual_reference_source_changed"]
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
                         *, output_root: Optional[Union[str, Path]] = None,
                         preserve_history: bool = True) -> Dict[str, Any]:
    """Refresh the existing projection at a committed change, including recovery."""
    root = _resolve_repo(repo)
    state = _resolve_state_root(root, output_root)
    result = None
    try:
        manifest, _ = _load_manifest(state)
        if (not _freshness_reasons(root, manifest)
                and not _cold_card_freshness_reasons(root, state, manifest)
                and not _cold_point_freshness_reasons(root, state, manifest)):
            result = {"status": "already_current", "source_version": manifest["source_version"],
                      "receipt_sha256": manifest["receipt_sha256"], "model_call_count": 0,
                      "formal_write_count": 0}
    except (ExamTutoringContextError, OSError, ValueError, KeyError):
        pass
    if result is None:
        result = refresh_nightly(root, refresh_date, output_root=state, preserve_history=preserve_history)
        manifest, _ = _load_manifest(state)
        if (_freshness_reasons(root, manifest)
                or _cold_card_freshness_reasons(root, state, manifest)
                or _cold_point_freshness_reasons(root, state, manifest)):
            raise ExamTutoringContextError("source_changed_before_refresh_readback")
        result = {**result, "source_version": manifest["source_version"]}
    # The course consumes the refreshed taxonomy and formal map. Its pointer has
    # its own guard; making it an input to the native refresh would create a cycle.
    if state == root / DEFAULT_STATE_REL:
        from local_quality_408 import refresh_registered_course
        course = refresh_registered_course(root)
        if course is not None:
            result = {**result, "local_syllabus_lessons": course}
    return result


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
    exam_question = exact_id("exam_question_id", "exam_question_id")
    if any(not re.fullmatch(r"EXAM408-\d{4}-Q\d{2}(?:-P\d{2})?", value.upper()) for value in exam_question):
        raise ExamTutoringContextError("exam_question_id must be a verified canonical EXAM408 identity")
    current_question = exact_id("current_question_id", "current_question_id")
    if any(not valid_identity(value) for value in current_question):
        raise ExamTutoringContextError("current_question_id must be an exact registered question identity")
    if current_question and exam_question and current_question != exam_question:
        raise ExamTutoringContextError("conflicting_current_question_identities")
    question_part = query.get("current_question_part")
    if question_part is not None and (not isinstance(question_part, str) or not re.fullmatch(r"P[0-9]{2,3}", question_part)):
        raise ExamTutoringContextError("current_question_part must be a registered Pxx teaching part")
    if question_part and not (current_question or exam_question or formal or source):
        raise ExamTutoringContextError("question_part_requires_exact_parent_identity")
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
        "exam_question": exam_question,
        "current_question": current_question,
        "question_part": question_part,
        "formal_visual_allowed": "current_question_id" not in query and "exam_question_id" not in query,
        # An explicit null from a verified variant means its actual exam
        # identity is unknown; a prototype formal ID must not fill that gap.
        "formal_introduction_allowed": "exam_question_id" not in query and "current_question_id" not in query,
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
        if result.get("question_exam_introduction"):
            return _question_introduction_budget_failure(result, max_bytes)
        return _unavailable_result(
            None, ["result_envelope_exceeds_budget"], max_bytes=max_bytes
        )
    return result


def _question_exam_introduction(routed, view, read_artifact, *, unchanged=False):
    """Read one prepared current-question introduction, independently of KSP selection."""
    identity = routed.get("question_introduction_identity")
    if view == "protected" or not identity:
        return None
    entry = routed.get("question_introduction_entry")
    if not entry:
        return {"status": "unavailable", "question_identity": identity,
                "reason": "question_frequency_not_preprocessed"}
    if unchanged:
        return {"status": "unchanged", "question_identity": identity}
    value = read_artifact(entry["ref"], entry["sha256"], "question frequency introduction",
                          limit=MAX_POINT_SUMMARY_BYTES)
    counts = [value.get(key) for key in ("total_count", "main_count", "secondary_only_count")]
    if (value.get("schema") != "cs408-question-frequency-view-v1"
            or value.get("question_identity") != identity
            or value.get("count_basis") != "unique_exam_questions"
            or value.get("frequency_scope") != "canonical_point"
            or any(type(count) is not int or count < 0 for count in counts)
            or counts[0] != counts[1] + counts[2]
            or not isinstance(value.get("primary_intro"), str) or not value["primary_intro"].strip()
            or "points" in value):
        raise ExamTutoringContextError("question_frequency_introduction_binding_invalid")
    return {**copy.deepcopy(value), "status": "available"}


def _question_introduction_budget_failure(result, max_bytes):
    """Keep the count introduction or an explicit gap; never silently drop it."""
    intro = result["question_exam_introduction"]
    compact = {"schema": result.get("schema"), "status": "budget_too_small",
        "source_version": result.get("source_version"), "truncated": True,
        "context_omitted_for_budget": True, "question_exam_introduction": intro,
        "records": [], "write_count": 0}
    if result.get("schema") == QUERY_RESULT_SCHEMA:
        # Retain the legacy unavailable envelope and its measured byte contract.
        compact = _unavailable_result(None, ["result_envelope_exceeds_budget"], max_bytes=max_bytes)
        compact["source_version"] = result.get("source_version")
        compact["question_exam_introduction"] = intro
    if result.get("version"):
        compact["version"] = result["version"]
    if "knowledge_summaries" in result:
        compact["knowledge_summaries"] = []
    if "serialized_bytes" in compact:
        _set_serialized_bytes(compact)
    if len(_canonical_bytes(compact)) > max_bytes:
        compact["question_exam_introduction"] = {
            "status": "omitted_for_budget", "question_identity": intro.get("question_identity"),
            "reason": "question_introduction_exceeds_query_budget"}
    if "serialized_bytes" in compact:
        _set_serialized_bytes(compact)
    return compact


def _deduplicate_question_frequency_intro(result):
    """Reference an identical current-question line without altering cold wording."""
    intro = result.get("question_exam_introduction") or {}
    if intro.get("status") != "available":
        return
    for record in [*result.get("records", []), *result.get("knowledge_summaries", [])]:
        scope = record.get("syllabus_context") or {}
        if (record.get("knowledge_point") == intro.get("primary_point")
                and scope.get("frequency_intro") == intro.get("primary_intro")):
            compact_scope = dict(scope)
            compact_scope.pop("frequency_intro")
            compact_scope["frequency_intro_ref"] = "question_exam_introduction"
            record["syllabus_context"] = compact_scope


def _read_syllabus_teaching_entries(entries, view, read_artifact, *, visual_states=None):
    """Read frozen views; native callers supply selected live source checks.

    Snapshot callers deliberately omit visual_states: their immutable version
    remains readable without the publisher's local reference files.
    """
    records = []
    for key, entry in entries.items():
        ref = entry["teaching_views"][view]
        value = read_artifact(ref["ref"], ref["sha256"], "syllabus teaching reference",
                              limit=MAX_POINT_SUMMARY_BYTES)
        if (value.get("schema") != "knowledge_teaching_view_408_v1"
                or value.get("syllabus_id") != key or value.get("view") != view
                or value.get("status") != "syllabus_only"
                or value.get("source_kind") != "syllabus_reference"
                or value.get("has_personal_history") is not False
                or value.get("scheduling_eligible") is not False):
            raise ExamTutoringContextError("syllabus_teaching_identity_mismatch")
        records.append(redact_visual_source(value) if (visual_states or {}).get(key) is False
                       else copy.deepcopy(value))
    return records


def _selected_teaching_plan_states(entries, view):
    if view == "protected":
        return {}
    from knowledge_profile_408 import teaching_plan_guards_current
    return {key: teaching_plan_guards_current(entry["teaching_plan_sources"])
            for key, entry in entries.items() if entry.get("teaching_plan_sources")}


def _selected_visual_template_states(entries, view):
    if view == "protected":
        return {}
    return {key: visual_sources_current(entry["visual_template_sources"])
            for key, entry in entries.items() if entry.get("visual_template_sources")}


def _redact_changed_teaching_source(record):
    value = copy.deepcopy(record)
    for field in ("teaching_plan", "teaching_plan_evidence_object", "teaching_plan_omitted_for_budget",
                  "teaching_focus", "teaching_focus_evidence_object", "teaching_focus_status",
                  "short_summary", "next_check", "progress", "evidence_object", "repair_evidence"):
        value.pop(field, None)
    if isinstance(value.get('linear_teaching_route'), dict):
        route = value['linear_teaching_route']
        route.pop('ordered_blocks', None)
        route.pop('visual_insertions', None)
        if 'current_teaching_brief' in route:
            from teaching_research_408 import current_teaching_brief
            route['current_teaching_brief'] = current_teaching_brief(
                route.get('identity'), value.get('syllabus_context'), None, 'semantic_stale')
        route.update(status='current_question_only', current_personal_status='semantic_stale',
                     personal_advice_withheld=True)
    value.update(status="semantic_stale", teaching_plan_status="source_changed_requires_rebind",
                 reason="teaching_plan_original_source_changed_or_moved")
    if isinstance(value.get('lesson_bridge'), dict):
        from lesson_alignment_408 import strip_personal
        value['lesson_bridge'] = strip_personal(value['lesson_bridge'])
    if isinstance(value.get("learning_profile"), dict):
        value["learning_profile"] = {key: item for key, item in value["learning_profile"].items()
                                     if key in {"version", "origin"}}
        value["learning_profile"].update(status="stale", reason="teaching_plan_original_source_changed_or_moved")
    return value


def _bind_question_lesson_bridge(result):
    """Use the same selected, version-checked records as the current answer turn."""
    question = result.get('question_visual_template') or {}
    if not isinstance(question.get('lesson_bridge'), dict):
        return
    from lesson_alignment_408 import bind_current_personal
    question['lesson_bridge'] = bind_current_personal(
        question['lesson_bridge'], result.get('records', result.get('knowledge_summaries', [])))


_PERSONAL_FIELDS = (
    "overview", "short_summary", "timeline", "progress", "next_check", "evidence",
    "source_refs", "cross_point_sources", "teaching_focus", "claims", "conditions",
    "supporting_evidence", "counterevidence", "unverified_boundaries", "teaching_plan",
)
_VISUAL_FIELDS = (
    "visual_template", "visual_template_status", "visual_template_source", "source_visuals",
    "visual_dispatch", "cached_visuals", "automatic_visual_teaching",
)
_OBJECTIVE_LONG_FIELDS = (
    "teaching_plan", "teaching_plan_evidence_object", "linear_teaching_route",
    "lesson_bridge", "teaching_research", "research_route", "narrative_plan",
)


def _complete_personal_record(record, entry, read_artifact, *, view, personal_scope, materials,
                             query_scope=None):
    """Complete the selected point/facet, never neighboring facets or a parent lesson.

    This is teacher evidence, not a student solution-unlock operation. Protected
    requests do not even open the personal objects. Original conversation bodies
    remain lazy, explicitly identified evidence reads.
    """
    value = copy.deepcopy(record)
    identity = value.get("subpoint_id") or value.get("knowledge_point") or value.get("syllabus_id")
    receipt = {"identity": identity, "scope": personal_scope, "objects": [],
               "complete": False, "raw_evidence_loaded": False}
    if view == "protected":
        receipt.update(status="withheld", reason="answer_protection")
        return value, receipt
    query_scope = query_scope or {}
    current_ids = (query_scope.get("current_question") or query_scope.get("exam_question")
                   or query_scope.get("formal") or query_scope.get("source") or [])
    exact_part = query_scope.get("question_part") or any(
        re.search(r"-P[0-9]{2,3}$", str(identity), re.I) for identity in current_ids)
    if exact_part and not value.get("subpoint_id") and value.get("has_personal_history") is not False:
        # Historical point prose has no executable Pxx scope contract. A tag
        # saying "teacher only" cannot make sibling answers safe. Until a
        # narrow facet is selected, keep objective boundaries and references
        # but withhold all parent prose, advice and point-level visual recipes.
        receipt.update(status="withheld_scope", reason="unscoped_parent_personal_history")
        allowed = {"schema", "knowledge_point", "concept_name", "view", "syllabus_context",
                   "concept_brief", "brief_status", "personal_source", "page_path", "page_relpath",
                   "obsidian_uri", "open_url", "markdown_link", "independent_mastery_inferred"}
        value = {key: item for key, item in value.items() if key in allowed}
        source = value.get("personal_source") or entry.get("personal_source") or {}
        value.update(status="current_question_only", reason=receipt["reason"],
            personal_context={**receipt, "evidence_object": source.get("records_object"),
                "profile_object": source.get("profile_object"),
                "independent_mastery_inferred": False})
        return value, receipt
    if materials == "visual":
        receipt.update(status="not_requested", reason="visual_material_only")
        return value, receipt
    if value.get("has_personal_history") is False:
        receipt.update(status="not_applicable", complete=True)
        return value, receipt
    if personal_scope == "summary":
        receipt.update(status="summary_only", reason="full_personal_context_not_read")
        return value, receipt
    if value.get("status") == "semantic_stale":
        receipt.update(status="stale", reason=value.get("reason") or
                       (value.get("personal_source") or {}).get("reason") or "personal_semantics_require_review")
        value["personal_context"] = {**receipt, "independent_mastery_inferred": False}
        if not value.get("subpoint_id"):
            source = value.get("personal_source") or {}
            for field, ref_field in (("evidence_object", "records_object"), ("history_object", "history_object")):
                if source.get(ref_field):
                    value["personal_context"][field] = copy.deepcopy(source[ref_field])
        return value, receipt
    facet = value.get("subpoint_id")
    source = value.get("personal_source") or {}
    reference = (value.get("evidence_object") or entry.get("evidence_object")) if facet else source.get("profile_object")
    if not facet and not source:
        # Compatibility with a published v1 summary: follow its own checked
        # point entry, not the global current profile or a filesystem search.
        summary = read_artifact(entry.get("ref"), entry.get("sha256"),
                                "personal point source", limit=MAX_POINT_SUMMARY_BYTES)
        profile = summary.get("learning_profile") or {}
        source = {"status": profile.get("status"), "profile_object": profile.get("profile_object"),
                  "profile_version": profile.get("version"), "history_object": summary.get("history_object")}
        reference = source.get("profile_object")
    if not facet and source.get("status") != "current":
        receipt.update(status="evidence_only" if source.get("status") == "not_authored" else "stale",
                       reason="no_current_reviewed_personal_semantics")
        value["personal_context"] = {**receipt, "evidence_object": source.get("records_object"),
                                     "history_object": source.get("history_object"),
                                     "independent_mastery_inferred": False}
        if (personal_scope == teaching_memory.SCOPE and receipt["status"] == "evidence_only"
                and value.get("repair_evidence") and source.get("records_object")):
            # The prebuilt repair is already bound to the same point/key and
            # checked source version. Keep a stable reference even if it won't fit.
            value["personal_context"] = {"identity": identity, "status": "evidence_only",
                "complete": False, "teacher_evidence_only": True,
                "evidence_object": source["records_object"], "repair_evidence": value["repair_evidence"]}
        return value, receipt
    if not isinstance(reference, dict) or not reference.get("ref"):
        receipt.update(status="unavailable", reason="complete_personal_object_missing")
        value = _redact_changed_teaching_source(value)
        value["reason"] = receipt["reason"]
        value["personal_context"] = {**receipt, "independent_mastery_inferred": False}
        return value, receipt
    try:
        full = read_artifact(reference["ref"], reference.get("sha256"),
                             "complete selected personal context", limit=PERSONAL_CONTEXT_MAX_BYTES)
        code = value.get("knowledge_point")
        if facet:
            if (full.get("knowledge_point") != code or full.get("subpoint_id") != facet
                    or full.get("parent_profile_version") != value.get("parent_profile_version")):
                raise ExamTutoringContextError("complete_subpoint_identity_or_version_mismatch")
        elif (full.get("concept_key") != code
              or full.get("version") != source.get("profile_version")):
            raise ExamTutoringContextError("complete_point_identity_or_version_mismatch")
    except (ExamTutoringContextError, OSError, ValueError, KeyError, TypeError) as exc:
        receipt.update(status="unavailable", reason="complete_personal_read_failed:" + str(exc))
        # A failed full read must not silently leave the truncated fields as if
        # they were adequate for a personal judgment.
        value = _redact_changed_teaching_source(value)
        value["reason"] = receipt["reason"]
        for field in _PERSONAL_FIELDS:
            value.pop(field, None)
        value["personal_context"] = {**receipt, "independent_mastery_inferred": False}
        return value, receipt
    profile_version = full.get("version") or full.get("parent_profile_version")
    if personal_scope == teaching_memory.SCOPE:
        value["personal_context"] = teaching_memory.selected_context(full, identity, profile_version)
        receipt.update(status="bounded", objects=[copy.deepcopy(reference)], profile_version=profile_version)
        return value, receipt
    personal = {field: copy.deepcopy(full[field]) for field in _PERSONAL_FIELDS
                if field in full and field != "teaching_plan"}
    plan = full.get("teaching_plan") or {}
    if plan:
        personal["teaching_plan"] = {field: copy.deepcopy(plan[field]) for field in (
            "learner_entry", "first_breakpoint", "first_action", "explanation",
            "visual", "avoid", "check", "visual_feedback") if field in plan}
    feedback = plan.get("visual_feedback")
    if feedback:
        personal["preferences"] = copy.deepcopy(feedback)
    from knowledge_profile_408 import claims_view
    personal["claim_view"] = claims_view(full, view=view)
    personal.update(schema="cs408-selected-personal-context-v1", identity=identity,
                    knowledge_point=code, status="current", complete=True,
                    profile_version=profile_version,
                    object_ref=copy.deepcopy(reference), teacher_evidence_only=True,
                    raw_evidence_loaded=False, independent_mastery_inferred=False)
    if source.get("records_object"):
        personal["evidence_object"] = copy.deepcopy(source["records_object"])
    # Deliberately exclude full.subpoints/question_refs and objective lesson
    # recipes. A narrow facet must never expand to its siblings or parent plan.
    value["personal_context"] = personal
    if materials in {"core", "personal"}:
        # New compact callers consume one complete personal object. Repeating
        # its entire timeline/progress at the record root can double a real
        # parent-point response without adding any evidence.
        for field in _PERSONAL_FIELDS:
            if field != "teaching_focus":
                value.pop(field, None)
        value["personal_fields_ref"] = "personal_context"
    else:
        # Keep the original record-level field layout for legacy all-material
        # callers; no semantic field is truncated in either representation.
        for field in ("overview", "short_summary", "timeline", "progress", "next_check"):
            if field in personal:
                value[field] = copy.deepcopy(personal[field])
    value["personal_summary_complete"] = True
    receipt.update(status="complete", complete=True, objects=[copy.deepcopy(reference)],
                   profile_version=personal["profile_version"])
    return value, receipt


def _finish_teaching_materials(result, entries, normalized, read_artifact, *, view):
    scope = normalized.get("personal_scope", "summary")
    materials = normalized.get("materials", "all")
    reads = []
    records = []
    is_exact = bool(normalized.get("current_question") or normalized.get("exam_question") or normalized.get("formal"))
    after_attempt_exact = view == "after_attempt" and is_exact
    for record in result.get("records", []):
        identity = record.get("subpoint_id") or record.get("knowledge_point") or record.get("syllabus_id")
        value, receipt = _complete_personal_record(record, entries.get(identity, {}), read_artifact,
            view=view, personal_scope=scope, materials=materials, query_scope=normalized)
        reads.append(receipt)
        deferred = []
        if materials in {"core", "personal"}:
            for field in _VISUAL_FIELDS + _OBJECTIVE_LONG_FIELDS:
                if field in value:
                    value.pop(field)
                    deferred.append(field)
        if after_attempt_exact:
            # Exact-turn advice comes from the question bridge. A point
            # bridge can carry other exam cases even when the parent label
            # matches, and duplicates the selected full personal evidence.
            value.pop("lesson_bridge", None)
            if materials == "all" and value.get("subpoint_id") and receipt["complete"]:
                # The compatibility fields keep short_summary/next_check at
                # the root. Full provenance is already in personal_context
                # and its receipt.
                value.pop("evidence_object", None)
        if materials == "personal":
            for field in ("syllabus_context", "concept_brief", "brief_status"):
                if field in value:
                    value.pop(field)
                    deferred.append(field)
        if materials == "visual":
            for field in _PERSONAL_FIELDS + ("personal_context", "personal_source", "personal_projection", "repair_evidence"):
                value.pop(field, None)
        if deferred:
            value["deferred_materials"] = {"fields": deferred, "identity": identity,
                "source_version": result.get("source_version"), "read_with_materials": "all"}
        records.append(teaching_memory.selected_record(value) if scope == teaching_memory.SCOPE else value)
    result["records"] = records
    unchanged = result.get("status") == "unchanged"
    result["personal_read_receipt"] = {
        "schema": "cs408-personal-context-read-v1", "source_version": result.get("source_version"),
        "version": result.get("version"), "view": view, "personal_scope": scope, "materials": materials,
        "query_scope": {key: normalized.get(key) for key in
                        ("formal", "source", "exam_question", "current_question", "question_part", "knowledge", "mechanism")},
        "status": "reuse_same_scope" if unchanged else "withheld" if view == "protected" else "read",
        "complete": (None if unchanged or view == "protected" else bool(reads) and all(r["complete"] for r in reads)),
        "reuse_requires_previously_read_version": result.get("version") if unchanged else None,
        "records": reads, "raw_evidence_loaded": False,
    }
    if any(row["status"] == "unavailable" for row in reads):
        result["personal_context_status"] = "incomplete_requires_evidence_read"
    elif any(row["status"] == "withheld_scope" for row in reads):
        result["personal_context_status"] = "incomplete_requires_scoped_personal_evidence"
    elif any(row["status"] == "stale" for row in reads):
        result["personal_context_status"] = "stale_personal_semantics_in_selected_scope"
    return result


def _subpoint_teaching_result(manifest, routed, normalized, read_artifact,
                             artifact_reads, view, known_version, max_bytes):
    entries = routed["subpoint_entries"]
    syllabus_entries = routed.get("syllabus_entries") or {}
    plan_states = _selected_teaching_plan_states(entries, view)
    visual_states = _selected_visual_template_states(entries, view)
    syllabus_visual_states = _selected_visual_template_states(syllabus_entries, view)
    question_visual_current = question_visual_source_state(routed, view)
    version = _sha256_bytes(_canonical_bytes({"source_version": manifest["source_version"], "query": normalized,
        "subpoints": entries, "syllabus_entries": syllabus_entries,
        "question_visual_entry": routed.get("question_visual_entry"),
        "question_visual_identity": routed.get("question_visual_identity"),
        "question_visual_reason": routed.get("question_visual_reason"),
        "question_visual_sources_current": question_visual_current,
        "question_introduction_identity": routed.get("question_introduction_identity"),
        "question_introduction_entry": routed.get("question_introduction_entry"),
        "view": view, "delivery_policy": (teaching_memory.DELIVERY_POLICY if
            normalized.get("personal_scope") == teaching_memory.SCOPE else TEACHING_DELIVERY_POLICY),
        "teaching_plan_sources_current": plan_states,
        "visual_template_sources_current": visual_states,
        "syllabus_visual_template_sources_current": syllabus_visual_states}))
    result = {"schema": "knowledge_teaching_context_408_v1", "status": "available",
        "version": version, "source_version": manifest["source_version"], "view": view,
        "scope": "matched_subpoints_only", "requested_subpoints": list(entries),
        "requested_points": list(dict.fromkeys(v["knowledge_point"] for v in entries.values())),
        "ambiguous_terms": routed["ambiguous_terms"], "missing_points": routed["missing_points"],
        "records": [], "omitted_subpoints": [], "write_count": 0,
        "independent_mastery_inferred": False}
    if routed.get("question_scope") is not None:
        result["question_scope"] = routed["question_scope"]
    if syllabus_entries:
        result.update(scope="matched_subpoints_and_syllabus", requested_syllabus_entries=list(syllabus_entries),
                      omitted_syllabus_entries=[])
    bounded = normalized.get("personal_scope") == teaching_memory.SCOPE
    question_visual = (read_question_visual(routed, view, read_artifact,
        unchanged=known_version == version, sources_current=question_visual_current, binding_only=bounded)
        if bounded or normalized.get("materials", "all") in {"all", "visual"} else None)
    if question_visual is not None:
        result["question_visual_template"] = question_visual
    introduction = _question_exam_introduction(routed, view, read_artifact, unchanged=known_version == version)
    if introduction is not None:
        result["question_exam_introduction"] = introduction
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
            value = _redact_changed_teaching_source(value) if plan_states.get(key) is False else value
            result["records"].append(redact_visual_source(value) if visual_states.get(key) is False else value)
        result["records"].extend(_read_syllabus_teaching_entries(syllabus_entries, view, read_artifact,
                                                                visual_states=syllabus_visual_states))
    result["returned"] = len(result["records"])
    result["truncated"] = False
    _finish_teaching_materials(result, {**entries, **syllabus_entries}, normalized, read_artifact, view=view)
    _bind_question_lesson_bridge(result)
    _deduplicate_question_frequency_intro(result)
    if normalized["read_stats"]:
        result["read_stats"] = {"artifact_count": len(artifact_reads),
            "artifact_bytes": sum(artifact_reads.values()), "artifacts": dict(sorted(artifact_reads.items()))}
    return result


def _prepared_teaching_result(repo_root, manifest, routed, normalized, read_artifact,
                              artifact_reads, view, known_version, max_bytes):
    requested = list(dict.fromkeys(routed["requested_points"]))
    syllabus_entries = routed.get("syllabus_entries") or {}
    weak = set(routed["weak_points"])
    requested.sort(key=lambda code: code not in weak)
    plan_states = _selected_teaching_plan_states(routed["point_entries"], view)
    visual_states = _selected_visual_template_states(routed["point_entries"], view)
    syllabus_visual_states = _selected_visual_template_states(syllabus_entries, view)
    question_visual_current = question_visual_source_state(routed, view)
    version = _sha256_bytes(_canonical_bytes({
        "source_version": manifest["source_version"], "query": normalized,
        "view": view, "delivery_policy": (teaching_memory.DELIVERY_POLICY if
            normalized.get("personal_scope") == teaching_memory.SCOPE else TEACHING_DELIVERY_POLICY),
        "points": {code: routed["point_entries"].get(code) for code in requested},
        "syllabus_entries": syllabus_entries,
        "question_visual_entry": routed.get("question_visual_entry"),
        "question_visual_identity": routed.get("question_visual_identity"),
        "question_visual_reason": routed.get("question_visual_reason"),
        "question_visual_sources_current": question_visual_current,
        "question_introduction_identity": routed.get("question_introduction_identity"),
        "question_introduction_entry": routed.get("question_introduction_entry"),
        "teaching_plan_sources_current": plan_states,
        "visual_template_sources_current": visual_states,
        "syllabus_visual_template_sources_current": syllabus_visual_states,
    }))
    result = {
        "schema": "knowledge_teaching_context_408_v1", "status": "available",
        "version": version, "source_version": manifest["source_version"],
        "view": view, "requested_points": requested,
        "missing_points": list(routed["missing_points"]),
        "ambiguous_terms": list(routed["ambiguous_terms"]), "omitted_points": [],
        "records": [], "write_count": 0, "independent_mastery_inferred": False,
    }
    if routed.get("question_scope") is not None:
        result["question_scope"] = routed["question_scope"]
    if syllabus_entries:
        result.update(scope="syllabus_only" if not requested else "knowledge_and_syllabus",
                      requested_syllabus_entries=list(syllabus_entries), omitted_syllabus_entries=[])
    bounded = normalized.get("personal_scope") == teaching_memory.SCOPE
    question_visual = (read_question_visual(routed, view, read_artifact,
        unchanged=known_version == version, sources_current=question_visual_current, binding_only=bounded)
        if bounded or normalized.get("materials", "all") in {"all", "visual"} else None)
    if question_visual is not None:
        result["question_visual_template"] = question_visual
    introduction = _question_exam_introduction(routed, view, read_artifact, unchanged=known_version == version)
    if introduction is not None:
        result["question_exam_introduction"] = introduction
    if known_version == version and (requested or syllabus_entries or introduction or question_visual):
        result["status"] = "unchanged"
    else:
        for code in requested:
            entry = routed["point_entries"].get(code)
            if entry is None and code in result["missing_points"]:
                continue
            entry = entry or {}
            reference = (entry.get("teaching_views") or {}).get(view)
            if not reference:
                raise ExamTutoringContextError("prepared_teaching_view_missing:" + code)
            record = read_artifact(reference.get("ref"), reference.get("sha256"),
                "prepared teaching summary", limit=MAX_POINT_SUMMARY_BYTES)
            if record.get("schema") != "knowledge_teaching_view_408_v1" or record.get("knowledge_point") != code:
                raise ExamTutoringContextError("prepared_teaching_identity_mismatch:" + code)
            record = copy.deepcopy(record)
            for question in ([] if bounded else record.get("questions", [])):
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
            record = _redact_changed_teaching_source(record) if plan_states.get(code) is False else record
            result["records"].append(redact_visual_source(record) if visual_states.get(code) is False else record)
        result["records"].extend(_read_syllabus_teaching_entries(syllabus_entries, view, read_artifact,
                                                                visual_states=syllabus_visual_states))
        if not result["records"]:
            result["status"] = "unknown_concept"
            if not requested and introduction and introduction.get("status") == "available":
                result.update(status="available", scope="question_introduction_only")
            elif view == "protected" and routed.get("question_visual_entry"):
                result.update(status="available", scope="protected_exact_question")
    result["returned"] = len(result["records"])
    result["truncated"] = False
    _finish_teaching_materials(result, {**routed["point_entries"], **syllabus_entries}, normalized, read_artifact, view=view)
    _bind_question_lesson_bridge(result)
    _deduplicate_question_frequency_intro(result)
    if normalized["read_stats"]:
        result["read_stats"] = {"artifact_count": len(artifact_reads),
            "artifact_bytes": sum(artifact_reads.values()), "artifacts": dict(sorted(artifact_reads.items()))}
    return result


def _query_context_native(
    repo: Union[str, Path],
    query: Mapping[str, Any],
    *,
    output_root: Optional[Union[str, Path]] = None,
    max_bytes: int = DEFAULT_QUERY_BYTES,
    teaching_view: Optional[str] = None,
    known_version: Optional[str] = None,
    personal_scope: str = "full",
    materials: str = "all",
) -> Dict[str, Any]:
    """Read selected artifacts for the requested delivery scope."""

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
    if personal_scope not in {"full", "summary", teaching_memory.SCOPE}:
        raise ExamTutoringContextError("personal_scope must be full, summary, or teaching")
    if materials not in {"all", "core", "personal", "visual"}:
        raise ExamTutoringContextError("materials must be all, core, personal, or visual")
    if personal_scope == teaching_memory.SCOPE:
        if teaching_view is None or materials not in {"core", "personal"}:
            raise ExamTutoringContextError("teaching scope requires a view and core or personal materials")
        normalized["max_bytes"] = min(max_bytes, normalized["max_bytes"])
    if teaching_view is not None:
        normalized.update(personal_scope=personal_scope, materials=materials)
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
        if teaching_view is not None or routed.get("syllabus_entries"):
            return _prepared_teaching_result(repo_root, manifest, routed, normalized,
                read_artifact, artifact_reads,
                teaching_view or ("protected" if normalized["stage"] == "pre_answer" else "direct"),
                known_version, max_bytes)
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
    if normalized["stage"] != "pre_answer":
        visual_states = _selected_visual_template_states(routed["point_entries"], "direct")
        plan_states = _selected_teaching_plan_states(routed["point_entries"], "direct")
        summaries = [_redact_changed_teaching_source(value) if plan_states.get(value["knowledge_point"]) is False
                     else value for value in summaries]
        summaries = [redact_visual_source(value) if visual_states.get(value["knowledge_point"]) is False
                     else value for value in summaries]
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
    try:
        view = "protected" if normalized["stage"] == "pre_answer" else "direct"
        question_visual = read_question_visual(routed, view, read_artifact,
            sources_current=question_visual_source_state(routed, view))
        if question_visual is not None:
            result["question_visual_template"] = question_visual
        introduction = _question_exam_introduction(routed, view, read_artifact)
    except (ExamTutoringContextError, ValueError, KeyError, TypeError) as exc:
        return _unavailable_result(manifest, ["question_introduction_unavailable:%s" % exc], max_bytes=max_bytes)
    if introduction is not None:
        result["question_exam_introduction"] = introduction
    _bind_question_lesson_bridge(result)
    _deduplicate_question_frequency_intro(result)
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


def query_context(
    repo: Union[str, Path],
    query: Mapping[str, Any],
    *,
    output_root: Optional[Union[str, Path]] = None,
    max_bytes: int = DEFAULT_QUERY_BYTES,
    teaching_view: Optional[str] = None,
    known_version: Optional[str] = None,
    personal_scope: str = "full",
    materials: str = "all",
) -> Dict[str, Any]:
    """Read native context plus the exact current question's prebuilt safe view.

    The visual pointer has its own version and is evaluated even when the
    personal view is unchanged. No query resolves a sibling, starts generation,
    or treats after_attempt/direct teacher access as a student solution unlock.
    """
    result = _query_context_native(repo, query, output_root=output_root,
                                  max_bytes=max_bytes, teaching_view=teaching_view,
                                  known_version=known_version, personal_scope=personal_scope,
                                  materials=materials)
    from local_quality_408 import attach_quality_context
    from study_visual_http_408 import attach_context
    attached = attach_quality_context(Path(repo), query, result, teaching_view)
    attached = attach_context(Path(repo), query, attached)
    if teaching_view is not None and materials == "personal":
        attached = {key: item for key, item in attached.items() if key not in {
            "cached_visuals", "automatic_visual_teaching", "visual_dispatch", "local_syllabus_lessons"}}
    if personal_scope == teaching_memory.SCOPE and "personal_read_receipt" in attached:
        return teaching_memory.fit(attached, min(max_bytes, query.get("max_bytes", DEFAULT_QUERY_BYTES)))
    if attached is result:
        return result
    if "serialized_bytes" in attached:
        _set_serialized_bytes(attached)
        budget = (result.get("budgets") or {}).get("max_bytes", max_bytes)
        if len(_canonical_bytes(attached)) > budget:
            # Preserve the native raw-query budget and its evidence rather
            # than dropping existing material for optional UI metadata.
            return result
    return attached


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
        refresh.add_argument("--preserve-history", action=argparse.BooleanOptionalAction, default=True,
                             help="Keep prior derived versions (default); explicit opt-out permits old derived-object GC")

    query = sub.add_parser("query")
    query.add_argument("--repo", default=".")
    query.add_argument("--input-json", required=True)
    query.add_argument("--output-root")
    query.add_argument("--max-bytes", type=int, default=DEFAULT_QUERY_BYTES)

    resolve = sub.add_parser("resolve", help="Read precomputed knowledge-point teaching summaries")
    resolve.add_argument("--repo", default=".")
    resolve.add_argument("--concept", action="append", default=[])
    resolve.add_argument("--weak-concept", action="append", default=[])
    resolve.add_argument("--mechanism", action="append", default=[],
                         help="Exact subpoint keyword; repeat for multiple current mechanisms")
    resolve.add_argument("--formal-id")
    resolve.add_argument("--source-id")
    resolve.add_argument("--exam-question-id", help="Exact currently displayed EXAM408 identity, never a variant prototype")
    resolve.add_argument("--current-question-id", help="Exact registered current question, including formal/chapter identity")
    resolve.add_argument("--question-part", help="Private teaching part under the exact current parent, e.g. P01")
    resolve.add_argument("--view", choices=("protected", "after_attempt", "direct"), default="protected")
    resolve.add_argument("--known-version")
    resolve.add_argument("--personal-scope", choices=("full", "summary", teaching_memory.SCOPE), default="full")
    resolve.add_argument("--materials", choices=("all", "core", "personal", "visual"), default="all")
    resolve.add_argument("--max-bytes", type=int, default=DEFAULT_QUERY_BYTES,
                         help="Byte budget for teaching scope; full and summary retain their existing layouts")
    resolve.add_argument("--read-stats", action="store_true")
    resolve.add_argument("--output-root")

    audit = sub.add_parser("audit")
    audit.add_argument("--repo", default=".")
    audit.add_argument("--receipt-sha256", required=True)
    audit.add_argument("--output-root")
    return parser


def main(argv: Optional[Sequence[str]] = None) -> int:
    arguments = list(sys.argv[1:] if argv is None else argv)
    if arguments and arguments[0] == "memory":
        from memory_access_408 import main as memory_main
        return memory_main(arguments[1:])
    args = _parser().parse_args(arguments)
    try:
        if args.command in {"refresh-nightly", "refresh-current"}:
            refresh = refresh_nightly if args.command == "refresh-nightly" else refresh_after_commit
            result = refresh(
                args.repo,
                args.refresh_date,
                output_root=args.output_root,
                preserve_history=args.preserve_history,
            )
        elif args.command == "resolve":
            payload = {"schema": QUERY_INPUT_SCHEMA,
                "stage": "pre_answer" if args.view == "protected" else "post_answer",
                "knowledge_points": args.concept, "weak_knowledge_points": args.weak_concept,
                "mechanism_terms": args.mechanism,
                "max_profiles": 0, "max_bytes": args.max_bytes, "read_stats": args.read_stats}
            if args.formal_id:
                payload["formal_id"] = args.formal_id
            if args.source_id:
                payload["source_id"] = args.source_id
            if args.exam_question_id:
                payload["exam_question_id"] = args.exam_question_id
            if args.current_question_id:
                payload["current_question_id"] = args.current_question_id
            if args.question_part:
                payload["current_question_part"] = args.question_part
            result = query_context(args.repo, payload, output_root=args.output_root,
                max_bytes=args.max_bytes, teaching_view=args.view, known_version=args.known_version,
                personal_scope=args.personal_scope, materials=args.materials)
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
