#!/usr/bin/env python3
"""Persist and route answer-safe 408 review evidence.

This is the single derived-state writer for daily practice, evening D0, and
morning review outcomes.  It never writes formal wrong-question facts,
relations, mastery scores, or review dates.  The append-only ledger is the
source of truth; actions, routing candidates, organic baselines, and dashboard
status are deterministic projections.
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
import sys
import tempfile
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterator
from zoneinfo import ZoneInfo

from intake_lib_408 import scan_leak
from study_commit_followup_408 import note_ledger_commit, study_commit_boundary
from bounded_jsonl_index_408 import (
    BoundedJsonlIndexError,
    lookup_event as lookup_indexed_event,
)
from linked_practice_source_408 import (
    LinkedPracticeSourceError,
    validate_selection_receipt,
    validate_source_for_practice,
)


ROOT = Path(__file__).resolve().parents[1]
LOOP_REL = Path("wiki/study_vaults/408-full/state/review-loop")
MORNING_REL = Path("wiki/study_vaults/408-full/state/morning-review")
DASHBOARD_REL = Path("wiki/study_vaults/408-full/StudyVault/00-Dashboard")
LEDGER_SCHEMA = "review_event_v1"
STATE_SCHEMA = "review_feedback_loop_state_v1"
RUNTIME_SCHEMA = "morning_review_runtime_v1"
POINTER_SCHEMA = "morning_review_pointer_v2"
SOURCES = {"daily_practice", "evening_d0", "morning_review"}
FIRST_RESULTS = {"independent_correct", "partial", "wrong", "uncertain"}
CHOICE_RESULTS = {"correct", "incorrect", "partial", "uncertain", "not_applicable"}
REASONING_RESULTS = {"sound", "partial", "diverged", "not_observed"}
CORRECTION_RESULTS = FIRST_RESULTS | {"relearn_required", "not_attempted"}
CONFIDENCE_LEVELS = {"low", "medium", "high"}
PROMPT_LEVELS = {"none", "minimal", "progressive", "full"}
FIRST_BREAK_PROVENANCES = {
    "user_report",
    "visible_evidence",
    "not_observed",
    "derived_classification",
}
LATENCY_STATES = {"normal", "abnormal", "unmeasured"}
CROSS_DAY_STATES = {True, False, None}
FOLLOWUP_PRACTICE_MODES = {"original", "variant"}
DEFERRED_MORNING_BINDING_SCHEMA = "cs408-deferred-morning-outcome-binding-v1"
ACTIVE_SESSION_STATES = {"in_progress", "paused"}
FORMAL_NODE_RE = re.compile(r"^(?:DS|CO|OS|CN)_(?:\d{4}|UNK)_\d{3}$")
REVIEW_UNIT_RE = re.compile(r"^RU_[A-Za-z0-9._-]+$")
SAFE_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:#/-]{0,255}$")
PROTECTED_RE = re.compile(
    r"完整题干|完整答案|完整解析|题目原文|标准答案|正确答案|参考答案|"
    r"错误答案|错选答案|我的答案|我的错选|用户答案|用户旧答案|"
    r"正确选项|错误选项|选项\s*[A-HＡ-Ｈ](?![A-Za-z])|答案字母",
    re.IGNORECASE,
)
NONE_BREAKS = {
    "",
    "none",
    "无",
    "未观察到",
    "未记录",
    "未可靠观察到",
    "not_observed",
}
STAGE_DAYS = {"D1": 1, "D3": 3, "D7": 7, "D14": 14, "D30": 30, "D60": 60, "D90": 90}
STAGE_SEQUENCE = ("D1", "D3", "D7", "D14", "D30", "D60", "D90")
D1_FAMILY_STAGES = {"D1", "D1_rescue"}
REVIEW_SCHEDULE_ENSURE_KIND = "review_schedule_ensure_v1"
FORMAL_REVIEW_BINDING_KIND = "formal_review_binding_v1"
GRADER_OUTCOME_CORRECTION_KIND = "grader_outcome_correction_v1"
CLOSED_EQUIVALENT_ITEM_RETIREMENT_KIND = (
    "closed_equivalent_item_retirement_v1"
)
CLOSED_EQUIVALENT_ITEM_RETIREMENT_CONTRACT = (
    "closed-equivalent-item-retirement-v1"
)
REVIEW_CONTROL_EVENT_KINDS = {
    REVIEW_SCHEDULE_ENSURE_KIND,
    FORMAL_REVIEW_BINDING_KIND,
    GRADER_OUTCOME_CORRECTION_KIND,
}
FORMAL_RESULT_STATUSES = {"curated", "already_current"}
SCHEDULING_POLICY_VERSION = "current_question_failure_standing_policy_v1"
SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
SCHEDULING_RECONCILIATION_CONTRACT = "review-scheduling-reconciliation-v1"
SCHEDULING_RECEIPT_REL = LOOP_REL / "scheduling-reconciliation-receipts"
LOCAL_TZ = ZoneInfo("Asia/Shanghai")
EVENT_INDEX_DIRNAME = "event-index"


class ReviewLoopError(RuntimeError):
    """Fail-closed review-loop error."""


def normalize_morning_first_classification(
    result: str,
    *,
    choice_result: str,
    reasoning_result: str,
    prompt_level: str,
) -> tuple[str, bool]:
    """Project one morning-session classification into canonical evidence.

    The morning surface keeps ``fragile_correct`` and ``blank`` as teaching
    states, while the canonical review ledger stores only the four evidence
    outcomes in ``FIRST_RESULTS``.  Separate choice/reasoning evidence is a
    hard constraint: a declared correct state may not outrank a known wrong,
    uncertain, or incomplete component.

    Returns ``(canonical_result, fragile_override)``.
    """

    morning_results = FIRST_RESULTS | {"fragile_correct", "blank"}
    if result not in morning_results:
        raise ReviewLoopError(f"invalid morning first result: {result}")
    if choice_result not in CHOICE_RESULTS:
        raise ReviewLoopError(f"invalid morning choice result: {choice_result}")
    if reasoning_result not in REASONING_RESULTS:
        raise ReviewLoopError(
            f"invalid morning reasoning result: {reasoning_result}"
        )
    if prompt_level not in PROMPT_LEVELS:
        raise ReviewLoopError(f"invalid morning prompt level: {prompt_level}")

    if result not in {"independent_correct", "fragile_correct"}:
        return ({"blank": "uncertain"}.get(result, result), False)
    if choice_result == "incorrect":
        return "wrong", False
    if choice_result == "uncertain":
        return "uncertain", False
    if (
        choice_result == "partial"
        or reasoning_result in {"partial", "diverged"}
        or prompt_level != "none"
    ):
        return "partial", False
    return "independent_correct", result == "fragile_correct"


@dataclass(frozen=True)
class LoopAction:
    key: str
    title: str
    prompt: str
    feedback: str
    source: str
    item_kind: str
    source_id: str
    action_id: str
    trigger_event_id: str
    stage: str
    due_date: str
    formal_node_id: str | None
    review_unit_id: str | None
    origin_kind: str | None = None


def utc_now() -> str:
    return dt.datetime.now(dt.timezone.utc).replace(microsecond=0).isoformat()


def canonical_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def content_id(prefix: str, value: Any, length: int = 20) -> str:
    digest = hashlib.sha256(canonical_json(value).encode("utf-8")).hexdigest()
    return f"{prefix}-{digest[:length]}"


def _resolve_repo(raw: str | Path) -> Path:
    repo = Path(raw).expanduser().resolve()
    if not repo.is_dir():
        raise ReviewLoopError(f"repo does not exist: {repo}")
    return repo


def _loop_root(repo: Path) -> Path:
    repo = repo.resolve()
    root = (repo / LOOP_REL).resolve()
    try:
        root.relative_to(repo)
    except ValueError as exc:
        raise ReviewLoopError("review-loop state escaped repo") from exc
    return root


def _fsync_dir(path: Path) -> None:
    flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
    descriptor = os.open(str(path), flags)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def atomic_write(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    descriptor, temp_name = tempfile.mkstemp(
        prefix=f".{path.name}.", suffix=".tmp", dir=path.parent
    )
    temp_path = Path(temp_name)
    try:
        os.fchmod(descriptor, 0o600)
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp_path, path)
        _fsync_dir(path.parent)
    except BaseException:
        with contextlib.suppress(FileNotFoundError):
            temp_path.unlink()
        raise


def atomic_write_json(path: Path, value: dict[str, Any]) -> None:
    atomic_write(path, json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n")


def append_jsonl(path: Path, value: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    payload = (canonical_json(value) + "\n").encode("utf-8")
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
    try:
        offset = 0
        while offset < len(payload):
            offset += os.write(descriptor, payload[offset:])
        os.fsync(descriptor)
    finally:
        os.close(descriptor)
    note_ledger_commit(path, value)


@contextmanager
def runtime_lock(repo: Path) -> Iterator[None]:
    root = _loop_root(repo)
    root.mkdir(parents=True, exist_ok=True, mode=0o700)
    descriptor = os.open(root / ".lock", os.O_RDWR | os.O_CREAT, 0o600)
    try:
        fcntl.flock(descriptor, fcntl.LOCK_EX)
        yield
    finally:
        fcntl.flock(descriptor, fcntl.LOCK_UN)
        os.close(descriptor)


def _safe_text(value: str | None, label: str, *, required: bool = False, limit: int = 240) -> str:
    compact = re.sub(r"\s+", " ", value or "").strip()
    if required and not compact:
        raise ReviewLoopError(f"{label} is required")
    if len(compact) > limit:
        raise ReviewLoopError(f"{label} must be <= {limit} characters")
    fails, warns = scan_leak(compact)
    if PROTECTED_RE.search(compact) or fails or warns:
        findings = sorted(set(fails + warns))
        detail = ", ".join(findings) if findings else "protected review content"
        raise ReviewLoopError(f"{label} contains protected content: {detail}")
    return compact


def validate_first_break_text(value: str) -> str:
    """Apply the canonical answer-safety gate without writing review state."""

    return _safe_text(value, "first_break", required=True, limit=200)


def _safe_id(value: str | None, label: str, *, required: bool = False) -> str | None:
    compact = _safe_text(value, label, required=required, limit=256)
    if not compact:
        return None
    if not SAFE_ID_RE.fullmatch(compact):
        raise ReviewLoopError(f"{label} contains unsupported characters")
    return compact


def _parse_datetime(value: str) -> dt.datetime:
    try:
        parsed = dt.datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ReviewLoopError(f"invalid event_time: {value}") from exc
    if parsed.tzinfo is None:
        raise ReviewLoopError("event_time must include a timezone")
    return parsed


def normalize_first_break_provenance(value: object, first_break: object) -> str:
    """Normalize legacy rows without silently upgrading derived evidence."""
    provenance = str(value or "")
    if provenance in FIRST_BREAK_PROVENANCES:
        return provenance
    if provenance:
        raise ReviewLoopError(f"invalid first_break provenance: {provenance}")
    return (
        "not_observed"
        if str(first_break or "").strip().lower() in NONE_BREAKS
        else "derived_classification"
    )


def _parse_date(value: str, label: str) -> dt.date:
    try:
        return dt.date.fromisoformat(value)
    except ValueError as exc:
        raise ReviewLoopError(f"invalid {label}: {value}") from exc


def _event_local_date(event_time: str) -> dt.date:
    """Return the actual Asia/Shanghai date carried by one event timestamp."""

    return _parse_datetime(event_time).astimezone(LOCAL_TZ).date()


def morning_session_review_date(event: dict[str, Any]) -> str | None:
    """Return the immutable queue date for a morning event.

    New events carry ``session_review_date`` explicitly.  Historical rows used
    ``observed_date`` for the queue date, so the fallback is intentionally
    limited to morning events and never mutates the ledger row.
    """

    if event.get("source") != "morning_review":
        return None
    value = str(event.get("session_review_date") or event.get("observed_date") or "")
    return _parse_date(value, "session_review_date").isoformat()


def source_study_date_matches(event: dict[str, Any], study_date: str) -> bool:
    """A verified deferred recorder may append on a later calendar date."""
    if event.get("observed_date") == study_date:
        return True
    binding = event.get("deferred_morning_binding") or {}
    if (event.get("source") != "morning_review"
            or binding.get("time_quality") != "runtime_observation_recorded_at"
            or event.get("session_review_date") != study_date
            or not SHA256_RE.fullmatch(str(binding.get("time_source_event_sha256") or ""))):
        return False
    actual = _event_local_date(str(event.get("event_time") or "")).isoformat()
    return event.get("observed_date") == actual and actual > study_date


def _split_md_row(line: str) -> list[str]:
    return [cell.strip() for cell in line.strip().strip("|").split("|")]


def _read_markdown_table(path: Path, id_header: str) -> list[dict[str, str]]:
    if not path.is_file():
        return []
    lines = path.read_text(encoding="utf-8").splitlines()
    header_index = next(
        (index for index, line in enumerate(lines) if line.startswith("|") and id_header in line),
        None,
    )
    if header_index is None:
        return []
    headers = _split_md_row(lines[header_index])
    rows: list[dict[str, str]] = []
    for line in lines[header_index + 2 :]:
        if not line.startswith("|"):
            continue
        cells = _split_md_row(line)
        if len(cells) == len(headers):
            rows.append(dict(zip(headers, cells)))
    return rows


def mapped_nodes(repo: Path, review_unit_id: str | None, source_id: str) -> list[str]:
    if FORMAL_NODE_RE.fullmatch(source_id):
        return [source_id]
    if not review_unit_id:
        return []
    nodes: set[str] = set()
    for row in _read_markdown_table(repo / "复习单元节点映射.md", "正式节点ID"):
        if row.get("主复习单元ID") == review_unit_id or review_unit_id in row.get("次复习单元ID", "").split("；"):
            node = row.get("正式节点ID", "")
            if FORMAL_NODE_RE.fullmatch(node):
                nodes.add(node)
    if nodes:
        return sorted(nodes)
    for row in _read_markdown_table(repo / "复习单元总表.md", "复习单元ID"):
        if row.get("复习单元ID") != review_unit_id:
            continue
        nodes.update(FORMAL_NODE_RE.findall(row.get("覆盖节点", "")))
        for match in re.findall(r"(?:DS|CO|OS|CN)_(?:\d{4}|UNK)_\d{3}", row.get("覆盖节点", "")):
            nodes.add(match)
    return sorted(node for node in nodes if FORMAL_NODE_RE.fullmatch(node))


def _read_events(repo: Path) -> list[dict[str, Any]]:
    path = _loop_root(repo) / "events.jsonl"
    if not path.exists():
        return []
    events: list[dict[str, Any]] = []
    seen_ids: set[str] = set()
    seen_keys: set[str] = set()
    for line_no, raw in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
        try:
            event = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise ReviewLoopError(f"review ledger is corrupt at line {line_no}") from exc
        if not isinstance(event, dict) or event.get("schema") != LEDGER_SCHEMA:
            raise ReviewLoopError(f"review ledger line {line_no} has invalid schema")
        event_id = str(event.get("event_id", ""))
        key = str(event.get("idempotency_key", ""))
        if not event_id or event_id in seen_ids:
            raise ReviewLoopError(f"duplicate or missing event_id at line {line_no}")
        if not key or key in seen_keys:
            raise ReviewLoopError(f"duplicate or missing idempotency_key at line {line_no}")
        seen_ids.add(event_id)
        seen_keys.add(key)
        events.append(event)
    return events


def _verified_morning_effective_date(
    repo: Path,
    event: dict[str, Any],
    *,
    session_cache: dict[str, list[dict[str, Any]]] | None = None,
) -> tuple[str, str, str]:
    """Project one morning event's actual date without rewriting legacy truth.

    Historical rows stored the immutable queue date in ``observed_date``.  They
    are projected to the timestamp date only when the session start, one unique
    first-answer event, and the canonical binding all agree.
    """

    session_id = str(event.get("session_id") or "")
    item_id = str(event.get("item_id") or "")
    if not session_id or not item_id:
        raise ReviewLoopError("morning event lacks session/item identity")
    session_date = morning_session_review_date(event)
    assert session_date is not None
    actual_date = _event_local_date(str(event.get("event_time") or "")).isoformat()
    if "session_review_date" in event and event.get("observed_date") != actual_date:
        raise ReviewLoopError(
            f"morning event observed_date is not its Shanghai event date: {event.get('event_id')}"
        )
    if "session_review_date" not in event and event.get("observed_date") != session_date:
        raise ReviewLoopError(
            f"legacy morning event lacks a stable queue date: {event.get('event_id')}"
        )

    cache = session_cache if session_cache is not None else {}
    session_events = cache.get(session_id)
    if session_events is None:
        ledger = repo / MORNING_REL / session_id / "events.jsonl"
        if not ledger.is_file():
            raise ReviewLoopError(f"morning session ledger is missing: {session_id}")
        session_events = []
        for line_no, raw in enumerate(
            ledger.read_text(encoding="utf-8").splitlines(), start=1
        ):
            try:
                row = json.loads(raw)
            except json.JSONDecodeError as exc:
                raise ReviewLoopError(
                    f"morning session is corrupt at line {line_no}: {session_id}"
                ) from exc
            if (
                not isinstance(row, dict)
                or row.get("schema") != "morning_review_session_event_v1"
                or row.get("session_id") != session_id
                or row.get("sequence") != line_no
            ):
                raise ReviewLoopError(
                    f"morning session sequence is invalid: {session_id}"
                )
            session_events.append(row)
        cache[session_id] = session_events
    if not session_events or session_events[0].get("event_type") != "session_started":
        raise ReviewLoopError(f"morning session lacks session_started: {session_id}")
    started_payload = session_events[0].get("payload") or {}
    if started_payload.get("review_date") != session_date:
        raise ReviewLoopError(
            f"morning event/session queue date mismatch: {event.get('event_id')}"
        )
    first_events = [
        row
        for row in session_events[1:]
        if row.get("event_type") in {"first_recorded", "first_reconciled_from_loop"}
        and (row.get("payload") or {}).get("item_id") == item_id
    ]
    if len(first_events) != 1:
        raise ReviewLoopError(
            f"morning event lacks one unique session first answer: {event.get('event_id')}"
        )
    first_event = first_events[0]
    first_payload = first_event.get("payload") or {}
    projected_time = first_payload.get("recorded_at") or first_event.get("timestamp")
    if projected_time != event.get("event_time"):
        raise ReviewLoopError(
            f"morning event/session answer time mismatch: {event.get('event_id')}"
        )
    reconciled_id = first_payload.get("canonical_event_id")
    bindings = [
        row
        for row in session_events[1:]
        if row.get("event_type") == "first_bound_to_loop"
        and (row.get("payload") or {}).get("item_id") == item_id
    ]
    if reconciled_id is not None and reconciled_id != event.get("event_id"):
        raise ReviewLoopError(
            f"morning event conflicts with its reconciled session answer: {event.get('event_id')}"
        )
    if bindings and (
        len(bindings) != 1
        or (bindings[0].get("payload") or {}).get("canonical_event_id")
        != event.get("event_id")
    ):
        raise ReviewLoopError(
            f"morning event conflicts with its session binding: {event.get('event_id')}"
        )
    basis = (
        "event_observed_date"
        if "session_review_date" in event
        else "legacy_verified_event_time"
    )
    return actual_date, basis, session_date


def effective_observed_date(
    repo: Path,
    event: dict[str, Any],
    *,
    session_cache: dict[str, list[dict[str, Any]]] | None = None,
) -> tuple[str, str, str | None]:
    """Return actual study date, projection basis, and optional queue date."""

    if event.get("source") == "morning_review":
        return _verified_morning_effective_date(
            repo, event, session_cache=session_cache
        )
    observed = _parse_date(
        str(event.get("observed_date") or ""), "observed_date"
    ).isoformat()
    if (
        event.get("source") in {"daily_practice", "evening_d0"}
        and not event.get("legacy_import")
        and _event_local_date(str(event.get("event_time") or "")).isoformat()
        != observed
    ):
        raise ReviewLoopError(
            f"review event observed_date mismatch: {event.get('event_id')}"
        )
    return observed, "event_observed_date", None


def _identity(event: dict[str, Any]) -> str:
    return str(
        event.get("review_unit_id")
        or event.get("formal_node_id")
        or event.get("source_id")
        or event.get("mechanism_key")
    )


def review_action_family(stage: str | None) -> str:
    """Return the scheduling-equivalence family for one review stage."""

    return "D1_family" if stage in D1_FAMILY_STAGES else str(stage or "")


def ordinary_independent_correct_needs_no_schedule(
    *,
    source: str,
    first_result: str,
    confidence: str,
    prompt_level: str,
    reasoning_break: bool,
    correction_result: str = "not_attempted",
    fragile: bool = False,
) -> bool:
    """Return whether an ordinary result is complete without a morning action."""

    return bool(
        source in {"daily_practice", "evening_d0"}
        and first_result == "independent_correct"
        and confidence == "high"
        and prompt_level == "none"
        and reasoning_break is False
        and correction_result == "not_attempted"
        and fragile is False
    )


def _merge_projected_action(
    actions: dict[str, dict[str, Any]], action: dict[str, Any]
) -> None:
    action_id = str(action.get("action_id") or "")
    if not action_id:
        raise ReviewLoopError("generated action requires action_id")
    existing = actions.get(action_id)
    if existing is None:
        projected = dict(action)
        trigger_ids = list(
            dict.fromkeys(
                str(value)
                for value in action.get(
                    "trigger_event_ids", [action.get("trigger_event_id")]
                )
                if value
            )
        )
        if not trigger_ids:
            raise ReviewLoopError("generated action requires a trigger event")
        projected["trigger_event_ids"] = trigger_ids
        origin_kinds = list(
            dict.fromkeys(
                str(value)
                for value in action.get(
                    "origin_kinds", [action.get("origin_kind")]
                )
                if value
            )
        )
        if origin_kinds:
            projected["origin_kinds"] = origin_kinds
        actions[action_id] = projected
        return
    if existing.get("dedupe_key") != action.get("dedupe_key"):
        raise ReviewLoopError(f"action identity collision: {action_id}")
    trigger_ids = existing.setdefault(
        "trigger_event_ids", [existing.get("trigger_event_id")]
    )
    for trigger_id in action.get(
        "trigger_event_ids", [action.get("trigger_event_id")]
    ):
        if trigger_id and trigger_id not in trigger_ids:
            trigger_ids.append(trigger_id)
    origin_kinds = existing.setdefault(
        "origin_kinds",
        [existing.get("origin_kind")] if existing.get("origin_kind") else [],
    )
    for origin_kind in action.get(
        "origin_kinds", [action.get("origin_kind")]
    ):
        if origin_kind and origin_kind not in origin_kinds:
            origin_kinds.append(origin_kind)
    if not origin_kinds:
        existing.pop("origin_kinds", None)


def _validate_review_control_event(event: dict[str, Any]) -> None:
    kind = str(event.get("event_kind") or "")
    if kind not in REVIEW_CONTROL_EVENT_KINDS:
        return
    if event.get("schema") != LEDGER_SCHEMA:
        raise ReviewLoopError(f"{kind} has invalid ledger schema")
    _safe_id(str(event.get("event_id") or ""), "event_id", required=True)
    _safe_id(
        str(event.get("idempotency_key") or ""),
        "idempotency_key",
        required=True,
    )
    if not SHA256_RE.fullmatch(str(event.get("request_hash") or "")):
        raise ReviewLoopError(f"{kind} request_hash must be SHA-256")
    _parse_datetime(str(event.get("event_time") or ""))
    _parse_date(str(event.get("observed_date") or ""), "observed_date")
    if event.get("source") != "formal_intake_backflow":
        raise ReviewLoopError(f"{kind} source must be formal_intake_backflow")
    if event.get("formal_write_authorized") is not False:
        raise ReviewLoopError(f"{kind} must keep formal_write_authorized=false")
    if event.get("organic_baseline") is not False:
        raise ReviewLoopError(f"{kind} cannot create an organic baseline")
    if event.get("stable_retention") is not False:
        raise ReviewLoopError(f"{kind} cannot create retention evidence")
    if event.get("trigger_action_id") is not None:
        raise ReviewLoopError(f"{kind} cannot consume a learner action")
    if event.get("route") != {
        "kind": kind,
        "formal_write_authorized": False,
    }:
        raise ReviewLoopError(f"{kind} has invalid route boundary")
    if not isinstance(event.get("generated_actions"), list):
        raise ReviewLoopError(f"{kind} generated_actions must be a list")

    capture_id = _safe_id(
        str(event.get("capture_id") or ""), "capture_id", required=True
    )
    batch_id = _safe_id(
        str(event.get("batch_id") or ""), "batch_id", required=True
    )
    assert capture_id and batch_id
    if event.get("timezone") != "Asia/Shanghai":
        raise ReviewLoopError(f"{kind} timezone must be Asia/Shanghai")

    if kind == REVIEW_SCHEDULE_ENSURE_KIND:
        if event.get("policy_version") != SCHEDULING_POLICY_VERSION:
            raise ReviewLoopError("review schedule ensure policy version is invalid")
        disposition = event.get("schedule_disposition")
        if disposition not in {
            "created_action",
            "reused_existing_action",
            "maintenance_noop",
        }:
            raise ReviewLoopError("review schedule ensure disposition is invalid")
        action_id = event.get("review_action_id")
        generated = event["generated_actions"]
        if disposition == "maintenance_noop":
            if action_id is not None or generated or event.get("due_date") is not None:
                raise ReviewLoopError("maintenance ensure cannot carry an action")
            if event.get("action_family") is not None:
                raise ReviewLoopError("maintenance ensure cannot carry an action family")
        else:
            _safe_id(str(action_id or ""), "review_action_id", required=True)
            _parse_date(str(event.get("due_date") or ""), "ensure due_date")
            if event.get("action_family") not in {
                "D1_family",
                *STAGE_SEQUENCE,
            }:
                raise ReviewLoopError("review schedule ensure action family is invalid")
            if disposition == "created_action" and len(generated) != 1:
                raise ReviewLoopError("created ensure must generate exactly one action")
            if disposition == "reused_existing_action" and generated:
                raise ReviewLoopError("reused ensure cannot generate another action")
            if generated:
                action = generated[0]
                if (
                    action.get("schema") != "review_action_v1"
                    or action.get("action_id") != action_id
                    or action.get("action_type") != "review"
                    or action.get("stage") not in D1_FAMILY_STAGES
                    or action.get("origin_kind") != "formal_intake_backflow"
                    or action.get("status") != "pending"
                    or action.get("trigger_event_id") != event.get("event_id")
                    or action.get("due_date") != event.get("due_date")
                ):
                    raise ReviewLoopError("created ensure action shape is invalid")
        if not isinstance(event.get("equivalent_action_dispositions"), list):
            raise ReviewLoopError("ensure equivalence dispositions must be a list")
        return

    if kind == GRADER_OUTCOME_CORRECTION_KIND:
        if event.get("generated_actions"):
            raise ReviewLoopError("grader correction cannot generate actions")
        for field in (
            "source_outcome_event_sha256",
            "corrected_ensure_event_sha256",
        ):
            if not SHA256_RE.fullmatch(str(event.get(field) or "")):
                raise ReviewLoopError(f"{field} must be SHA-256")
        _safe_id(
            str(event.get("source_outcome_event_id") or ""),
            "source_outcome_event_id",
            required=True,
        )
        _safe_id(
            str(event.get("corrected_ensure_event_id") or ""),
            "corrected_ensure_event_id",
            required=True,
        )
        if event.get("reason_code") not in {
            "prepared_grader_key_conflict",
            "choice_only_grader_reasoning_conflict",
        }:
            raise ReviewLoopError("grader correction reason is invalid")
        if not isinstance(event.get("closed_action_ids"), list):
            raise ReviewLoopError("grader correction closed actions must be a list")
        return

    if event["generated_actions"]:
        raise ReviewLoopError("formal review binding cannot generate actions")
    if event.get("formal_result_status") not in FORMAL_RESULT_STATUSES:
        raise ReviewLoopError("formal review binding result status is invalid")
    for field in (
        "formal_result_receipt_sha256",
        "ensure_receipt_sha256",
    ):
        if not SHA256_RE.fullmatch(str(event.get(field) or "")):
            raise ReviewLoopError(f"{field} must be SHA-256")
    formal_node_id = str(event.get("formal_node_id") or "")
    review_unit_id = str(event.get("review_unit_id") or "")
    if not FORMAL_NODE_RE.fullmatch(formal_node_id):
        raise ReviewLoopError("formal review binding requires formal_node_id")
    if not REVIEW_UNIT_RE.fullmatch(review_unit_id):
        raise ReviewLoopError("formal review binding requires receipt-bound review_unit_id")
    if event.get("review_unit_identity_basis") != "formal_result_receipt":
        raise ReviewLoopError("formal review binding cannot guess review_unit_id")
    action_id = event.get("review_action_id")
    canonical_action_id = event.get("canonical_review_action_id")
    if action_id is None:
        if canonical_action_id is not None:
            raise ReviewLoopError("maintenance binding cannot carry canonical action")
    else:
        _safe_id(str(action_id), "review_action_id", required=True)
        _safe_id(
            str(canonical_action_id or ""),
            "canonical_review_action_id",
            required=True,
        )
    if not isinstance(event.get("equivalent_action_dispositions"), list):
        raise ReviewLoopError("binding equivalence dispositions must be a list")


def _apply_equivalent_action_dispositions(
    actions: dict[str, dict[str, Any]],
    event: dict[str, Any],
) -> None:
    for row in event.get("equivalent_action_dispositions") or []:
        if not isinstance(row, dict) or set(row) != {
            "action_id",
            "authoritative_action_id",
            "disposition",
        }:
            raise ReviewLoopError("invalid equivalent action disposition")
        action_id = str(row.get("action_id") or "")
        authoritative_id = str(row.get("authoritative_action_id") or "")
        if (
            row.get("disposition") != "closed_equivalent_d1_family"
            or action_id == authoritative_id
        ):
            raise ReviewLoopError("invalid D1-family equivalence disposition")
        action = actions.get(action_id)
        authoritative = actions.get(authoritative_id)
        if action is None or authoritative is None:
            raise ReviewLoopError("equivalence disposition references unknown action")
        if (
            review_action_family(str(action.get("stage") or "")) != "D1_family"
            or review_action_family(str(authoritative.get("stage") or ""))
            != "D1_family"
            or action.get("due_date") != authoritative.get("due_date")
        ):
            raise ReviewLoopError("equivalence disposition crosses scheduling slots")
        action_identity = action.get("bound_identity") or action.get("identity")
        authoritative_identity = (
            authoritative.get("bound_identity") or authoritative.get("identity")
        )
        if action_identity != authoritative_identity:
            binding_identity = (
                str(event.get("review_unit_id") or "")
                if event.get("event_kind") == FORMAL_REVIEW_BINDING_KIND
                else ""
            )
            binding_action_ids = {
                str(value)
                for value in (
                    event.get("review_action_id"),
                    event.get("canonical_review_action_id"),
                )
                if value
            }
            action_will_share_binding = (
                action_id in binding_action_ids
                or binding_identity in _action_identities(action)
            )
            authoritative_will_share_binding = (
                authoritative_id in binding_action_ids
                or binding_identity in _action_identities(authoritative)
            )
            if not (
                binding_identity
                and action_will_share_binding
                and authoritative_will_share_binding
            ):
                raise ReviewLoopError("equivalence disposition crosses identities")
        if action.get("status") == "pending":
            action["status"] = "closed_equivalent"
            action["closed_by_event_id"] = event["event_id"]
        action["equivalent_to_action_id"] = authoritative_id
        action.setdefault("equivalence_disposition_event_ids", []).append(
            event["event_id"]
        )


def _closed_equivalent_retirement_request(
    *,
    session_id: str,
    item_id: str,
    source_id: str,
    trigger_action_id: str,
    formal_binding_event_id: str,
    reason: str,
    equivalent_to_action_id: str,
) -> dict[str, Any]:
    return {
        "schema": "closed_equivalent_item_retirement_request_v1",
        "session_id": session_id,
        "item_id": item_id,
        "source_id": source_id,
        "trigger_action_id": trigger_action_id,
        "formal_binding_event_id": formal_binding_event_id,
        "reason": reason,
        "equivalent_to_action_id": equivalent_to_action_id,
        "explicit_user_confirmation": True,
    }


def _validate_closed_equivalent_item_retirement_event(
    event: dict[str, Any],
    *,
    actions: dict[str, dict[str, Any]],
    formal_review_bindings: list[dict[str, Any]],
) -> None:
    required = {
        "schema",
        "event_kind",
        "event_id",
        "idempotency_key",
        "request_hash",
        "event_time",
        "observed_date",
        "timezone",
        "source",
        "session_id",
        "item_id",
        "source_id",
        "trigger_action_id",
        "disposition",
        "resolution_reason",
        "explicit_user_confirmation",
        "answer_free_terminal",
        "formal_binding_event_id",
        "formal_binding_event_sha256",
        "equivalent_to_action_id",
        "closed_equivalent_action_sha256",
        "processing_contract",
        "learner_evidence_write_count",
        "formal_write_authorized",
        "formal_write_count",
        "generated_actions",
    }
    if set(event) != required:
        raise ReviewLoopError(
            "closed-equivalent item retirement fields are incomplete or unknown"
        )
    if (
        event.get("schema") != LEDGER_SCHEMA
        or event.get("event_kind")
        != CLOSED_EQUIVALENT_ITEM_RETIREMENT_KIND
        or event.get("source") != "morning_review_administration"
        or event.get("timezone") != "Asia/Shanghai"
        or event.get("disposition") != "administratively_retired"
        or event.get("resolution_reason") != "user_removed"
        or event.get("explicit_user_confirmation") is not True
        or event.get("answer_free_terminal") is not True
        or event.get("processing_contract")
        != CLOSED_EQUIVALENT_ITEM_RETIREMENT_CONTRACT
        or event.get("learner_evidence_write_count") != 0
        or event.get("formal_write_authorized") is not False
        or event.get("formal_write_count") != 0
        or event.get("generated_actions") != []
    ):
        raise ReviewLoopError(
            "closed-equivalent item retirement is not an answer-free administrative terminal"
        )
    for field in (
        "event_id",
        "idempotency_key",
        "session_id",
        "item_id",
        "trigger_action_id",
        "formal_binding_event_id",
        "equivalent_to_action_id",
    ):
        _safe_id(str(event.get(field) or ""), field, required=True)
    _safe_text(str(event.get("source_id") or ""), "source_id", required=True)
    for field in (
        "request_hash",
        "formal_binding_event_sha256",
        "closed_equivalent_action_sha256",
    ):
        if not SHA256_RE.fullmatch(str(event.get(field) or "")):
            raise ReviewLoopError(f"closed-equivalent item retirement {field} is invalid")
    event_time = str(event.get("event_time") or "")
    _parse_datetime(event_time)
    observed_date = _parse_date(
        str(event.get("observed_date") or ""), "observed_date"
    ).isoformat()
    if _event_local_date(event_time).isoformat() != observed_date:
        raise ReviewLoopError(
            "closed-equivalent item retirement observed_date mismatches event_time"
        )
    key = str(event["idempotency_key"])
    expected_event_id = content_id(
        "RE",
        {"kind": CLOSED_EQUIVALENT_ITEM_RETIREMENT_KIND, "key": key},
    )
    if event.get("event_id") != expected_event_id:
        raise ReviewLoopError(
            "closed-equivalent item retirement event identity drifted"
        )
    request = _closed_equivalent_retirement_request(
        session_id=str(event["session_id"]),
        item_id=str(event["item_id"]),
        source_id=str(event["source_id"]),
        trigger_action_id=str(event["trigger_action_id"]),
        formal_binding_event_id=str(event["formal_binding_event_id"]),
        reason=str(event["resolution_reason"]),
        equivalent_to_action_id=str(event["equivalent_to_action_id"]),
    )
    expected_request_hash = hashlib.sha256(
        canonical_json(request).encode("utf-8")
    ).hexdigest()
    if event.get("request_hash") != expected_request_hash:
        raise ReviewLoopError(
            "closed-equivalent item retirement request hash drifted"
        )

    action_id = str(event["trigger_action_id"])
    action = actions.get(action_id)
    if action is None:
        raise ReviewLoopError(
            "closed-equivalent item retirement references an unknown action"
        )
    equivalent_to = str(event["equivalent_to_action_id"])
    if (
        action.get("status") != "closed_equivalent"
        or action.get("equivalent_to_action_id") != equivalent_to
        or equivalent_to not in actions
        or action.get("source_id") != event.get("source_id")
        or str(event["formal_binding_event_id"])
        not in (action.get("equivalence_disposition_event_ids") or [])
    ):
        raise ReviewLoopError(
            "closed-equivalent item retirement action binding is invalid"
        )
    if event.get("closed_equivalent_action_sha256") != _sha256_value(action):
        raise ReviewLoopError(
            "closed-equivalent item retirement action hash drifted"
        )

    binding_id = str(event["formal_binding_event_id"])
    matches = [
        row
        for row in formal_review_bindings
        if row.get("event_id") == binding_id
    ]
    if len(matches) != 1:
        raise ReviewLoopError(
            "closed-equivalent item retirement requires one formal binding event"
        )
    binding = matches[0]
    expected_equivalence = {
        "action_id": action_id,
        "authoritative_action_id": equivalent_to,
        "disposition": "closed_equivalent_d1_family",
    }
    if (
        binding.get("event_kind") != FORMAL_REVIEW_BINDING_KIND
        or binding.get("formal_write_authorized") is not False
        or expected_equivalence
        not in (binding.get("equivalent_action_dispositions") or [])
        or event.get("formal_binding_event_sha256") != _sha256_value(binding)
    ):
        raise ReviewLoopError(
            "closed-equivalent item retirement formal binding proof is invalid"
        )


def _reduce(events: list[dict[str, Any]]) -> dict[str, Any]:
    actions: dict[str, dict[str, Any]] = {}
    action_formal_bindings: dict[str, dict[str, Any]] = {}
    candidates: dict[str, dict[str, Any]] = {}
    baselines: dict[str, dict[str, Any]] = {}
    retention: dict[str, dict[str, Any]] = {}
    records: list[dict[str, Any]] = []
    confirmations: list[dict[str, Any]] = []
    dispositions: list[dict[str, Any]] = []
    date_corrections: list[dict[str, Any]] = []
    scheduling_reconciliations: list[dict[str, Any]] = []
    review_schedule_ensures: list[dict[str, Any]] = []
    review_schedule_ensure_by_id: dict[str, dict[str, Any]] = {}
    formal_review_bindings: list[dict[str, Any]] = []
    grader_outcome_corrections: list[dict[str, Any]] = []
    closed_equivalent_item_retirements: list[dict[str, Any]] = []
    retired_session_items: set[tuple[str, str]] = set()

    for event in events:
        kind = event.get("event_kind")
        if kind in REVIEW_CONTROL_EVENT_KINDS:
            _validate_review_control_event(event)
            records.append(event)
            if kind == GRADER_OUTCOME_CORRECTION_KIND:
                source_id = str(event["source_outcome_event_id"])
                source_matches = [
                    row for row in records[:-1]
                    if row.get("event_id") == source_id
                    and row.get("event_kind") == "outcome"
                ]
                ensure = review_schedule_ensure_by_id.get(
                    str(event["corrected_ensure_event_id"])
                )
                if (
                    len(source_matches) != 1
                    or _sha256_value(source_matches[0])
                    != event["source_outcome_event_sha256"]
                    or ensure is None
                    or _sha256_value(ensure)
                    != event["corrected_ensure_event_sha256"]
                ):
                    raise ReviewLoopError("grader correction canonical proof drifted")
                for action_id in event["closed_action_ids"]:
                    action = actions.get(str(action_id))
                    if (
                        action is None
                        or action.get("status") != "pending"
                        or source_id not in _action_trigger_ids(action)
                        or len(_action_trigger_ids(action)) != 1
                    ):
                        raise ReviewLoopError(
                            "grader correction cannot close a nonexclusive action"
                        )
                    action["status"] = "closed_grader_correction"
                    action["closed_by_event_id"] = event["event_id"]
                source_event = source_matches[0]
                if source_event.get("evidence_kind") == "retention":
                    identity = _identity(source_event)
                    entry = retention.get(identity)
                    if entry is None or source_id not in entry.get("events", []):
                        raise ReviewLoopError("grader correction retention proof is missing")
                    entry["events"] = [
                        event_id for event_id in entry["events"] if event_id != source_id
                    ]
                    entry["count"] = len(entry["events"])
                    if not entry["events"]:
                        retention.pop(identity, None)
                grader_outcome_corrections.append(event)
                continue
            if kind == REVIEW_SCHEDULE_ENSURE_KIND:
                for generated in event.get("generated_actions") or []:
                    _merge_projected_action(actions, generated)
                action_id = event.get("review_action_id")
                if action_id is not None and str(action_id) not in actions:
                    raise ReviewLoopError(
                        "review schedule ensure references an unknown action"
                    )
                _apply_equivalent_action_dispositions(actions, event)
                review_schedule_ensures.append(event)
                review_schedule_ensure_by_id[str(event["event_id"])] = event
                continue

            ensure_event_id = str(event.get("ensure_event_id") or "")
            ensure_event = review_schedule_ensure_by_id.get(ensure_event_id)
            if ensure_event is None:
                raise ReviewLoopError(
                    "formal review binding references an unknown ensure event"
                )
            if (
                ensure_event.get("capture_id") != event.get("capture_id")
                or ensure_event.get("batch_id") != event.get("batch_id")
                or ensure_event.get("review_action_id")
                != event.get("review_action_id")
                or ensure_event.get("source_review_event_id")
                != event.get("source_review_event_id")
            ):
                raise ReviewLoopError("formal review binding conflicts with ensure")
            ensured_action_id = event.get("review_action_id")
            canonical_action_id = event.get("canonical_review_action_id")
            review_unit_id = str(event["review_unit_id"])
            formal_node_id = str(event["formal_node_id"])
            if ensured_action_id is None:
                if ensure_event.get("schedule_disposition") != "maintenance_noop":
                    raise ReviewLoopError(
                        "actionless formal binding requires maintenance ensure"
                    )
            else:
                ensured = actions.get(str(ensured_action_id))
                canonical_action = actions.get(str(canonical_action_id))
                if ensured is None or canonical_action is None:
                    raise ReviewLoopError(
                        "formal review binding references an unknown canonical action"
                    )
                _apply_equivalent_action_dispositions(actions, event)
                for action in {
                    str(ensured_action_id): ensured,
                    str(canonical_action_id): canonical_action,
                }.values():
                    previous_identity = action.get("bound_identity")
                    if previous_identity not in {None, review_unit_id}:
                        raise ReviewLoopError(
                            "one review action cannot bind conflicting review units"
                        )
                    action.setdefault("source_identity", action.get("identity"))
                    action["bound_identity"] = review_unit_id
                    action["identity"] = review_unit_id
                    action["review_unit_id"] = review_unit_id
                binding = action_formal_bindings.setdefault(
                    str(canonical_action_id),
                    {
                        "schema": "action_formal_binding_projection_v1",
                        "action_id": str(canonical_action_id),
                        "review_unit_id": review_unit_id,
                        "formal_node_ids": [],
                        "capture_ids": [],
                        "formal_result_receipt_sha256s": [],
                        "binding_event_ids": [],
                    },
                )
                if binding["review_unit_id"] != review_unit_id:
                    raise ReviewLoopError(
                        "one canonical action cannot bind conflicting review units"
                    )
                for field, value in (
                    ("formal_node_ids", formal_node_id),
                    ("capture_ids", str(event["capture_id"])),
                    (
                        "formal_result_receipt_sha256s",
                        str(event["formal_result_receipt_sha256"]),
                    ),
                    ("binding_event_ids", str(event["event_id"])),
                ):
                    if value not in binding[field]:
                        binding[field].append(value)
                    binding[field].sort()
                canonical_action = actions[str(canonical_action_id)]
                canonical_action["bound_identity"] = review_unit_id
                canonical_action["identity"] = review_unit_id
                canonical_action["review_unit_id"] = review_unit_id
                canonical_action["formal_node_ids"] = list(
                    binding["formal_node_ids"]
                )
                canonical_action["formal_node_id"] = (
                    binding["formal_node_ids"][0]
                    if len(binding["formal_node_ids"]) == 1
                    else None
                )
                canonical_action["formal_binding_event_ids"] = list(
                    binding["binding_event_ids"]
                )
            formal_review_bindings.append(event)
            continue
        if kind == CLOSED_EQUIVALENT_ITEM_RETIREMENT_KIND:
            _validate_closed_equivalent_item_retirement_event(
                event,
                actions=actions,
                formal_review_bindings=formal_review_bindings,
            )
            coordinate = (
                str(event.get("session_id") or ""),
                str(event.get("item_id") or ""),
            )
            if coordinate in retired_session_items:
                raise ReviewLoopError(
                    "one morning item cannot have multiple closed-equivalent retirements"
                )
            retired_session_items.add(coordinate)
            records.append(event)
            dispositions.append(event)
            closed_equivalent_item_retirements.append(event)
            continue
        if kind == "scheduling_reconciliation":
            action_id = str(event.get("trigger_action_id") or "")
            action = actions.get(action_id)
            if action is None or action.get("status") != "pending":
                raise ReviewLoopError(
                    f"scheduling reconciliation cannot consume pending action: {action_id}"
                )
            authoritative = str(
                event.get("authoritative_outcome_event_id") or ""
            )
            if not authoritative:
                raise ReviewLoopError(
                    "scheduling reconciliation lacks authoritative outcome"
                )
            action["status"] = "completed"
            action["completed_by_event_id"] = authoritative
            action["scheduling_reconciled_by_event_id"] = event["event_id"]
            if event.get("stable_retention") is True:
                identity = str(event.get("identity") or _identity(event))
                entry = retention.setdefault(identity, {"count": 0, "events": []})
                if authoritative not in entry["events"]:
                    entry["count"] += 1
                    entry["events"].append(authoritative)
                entry["last_observed_date"] = event["observed_date"]
            for generated in event.get("generated_actions") or []:
                _merge_projected_action(actions, generated)
            scheduling_reconciliations.append(event)
            continue
        if kind == "date_correction":
            source_event_id = str(event.get("source_event_id") or "")
            for action_id in event.get("closed_action_ids") or []:
                action = actions.get(str(action_id))
                if action is None or action.get("status") != "pending":
                    raise ReviewLoopError(
                        f"date correction cannot close pending action: {action_id}"
                    )
                trigger_ids = action.get("trigger_event_ids") or [
                    action.get("trigger_event_id")
                ]
                if source_event_id not in trigger_ids or len(trigger_ids) != 1:
                    raise ReviewLoopError(
                        f"date correction close is not exclusive: {action_id}"
                    )
                action["status"] = "closed_date_correction"
                action["closed_by_event_id"] = event["event_id"]
            for action_id in event.get("detached_action_ids") or []:
                action = actions.get(str(action_id))
                if action is None or action.get("status") != "pending":
                    raise ReviewLoopError(
                        f"date correction cannot detach pending action: {action_id}"
                    )
                trigger_ids = list(
                    action.get("trigger_event_ids")
                    or [action.get("trigger_event_id")]
                )
                if source_event_id not in trigger_ids or len(trigger_ids) <= 1:
                    raise ReviewLoopError(
                        f"date correction detach is not shared: {action_id}"
                    )
                action["trigger_event_ids"] = [
                    trigger_id
                    for trigger_id in trigger_ids
                    if trigger_id != source_event_id
                ]
                if action.get("trigger_event_id") == source_event_id:
                    action["trigger_event_id"] = action["trigger_event_ids"][0]
                action.setdefault("date_correction_event_ids", []).append(
                    event["event_id"]
                )
            for replacement in event.get("replacement_actions") or []:
                action_id = replacement["action_id"]
                existing = actions.get(action_id)
                if existing is None:
                    _merge_projected_action(actions, replacement)
                elif existing.get("dedupe_key") != replacement.get("dedupe_key"):
                    raise ReviewLoopError(
                        f"date correction action identity collision: {action_id}"
                    )
                else:
                    if existing.get("status") != "pending":
                        raise ReviewLoopError(
                            f"date correction replacement is not pending: {action_id}"
                        )
                    trigger_ids = existing.setdefault(
                        "trigger_event_ids", [existing["trigger_event_id"]]
                    )
                    for trigger_id in replacement.get(
                        "trigger_event_ids", [replacement["trigger_event_id"]]
                    ):
                        if trigger_id not in trigger_ids:
                            trigger_ids.append(trigger_id)
            date_corrections.append(event)
            continue
        if kind == "candidate_disposition":
            candidate = candidates.get(str(event.get("candidate_id")))
            if candidate is not None:
                candidate["resolved_from_kind"] = candidate.get("kind")
                candidate["kind"] = "evidence_only"
                candidate["status"] = "closed_evidence_only"
                candidate["closed_by_event_id"] = event["event_id"]
                candidate["formal_node_id"] = None
                candidate["required_handoff"] = None
                candidate["resolution_reason"] = event.get("resolution_reason")
            for action_id in event.get("dismissed_action_ids", []):
                action = actions.get(str(action_id))
                if action is not None and action.get("status") == "pending":
                    action["status"] = "closed_evidence_only"
                    action["closed_by_event_id"] = event["event_id"]
            dispositions.append(event)
            continue
        if kind == "candidate_confirmation":
            candidate = candidates.get(str(event.get("candidate_id")))
            if candidate is not None:
                resolved_node = event.get("resolved_formal_node_id")
                if resolved_node:
                    candidate["resolved_from_kind"] = candidate.get("kind")
                    candidate["kind"] = "update_existing"
                    candidate["formal_node_id"] = resolved_node
                    candidate["resolved_formal_node_id"] = resolved_node
                    candidate["resolution_reason"] = "explicit user selection from mapped formal nodes"
                candidate["status"] = "confirmed_for_intake"
                candidate["confirmed_by_event_id"] = event["event_id"]
                candidate["handoff_skill"] = "kaoyan-408-wrong-intake"
            confirmations.append(event)
            continue
        records.append(event)
        consumed = event.get("trigger_action_id")
        if consumed and consumed in actions:
            actions[consumed]["status"] = "completed"
            actions[consumed]["completed_by_event_id"] = event["event_id"]
        if event.get("organic_baseline"):
            identity = _identity(event)
            baselines.setdefault(
                identity,
                {
                    "identity": identity,
                    "event_id": event["event_id"],
                    "observed_date": event["observed_date"],
                    "review_unit_id": event.get("review_unit_id"),
                    "formal_node_id": event.get("formal_node_id"),
                    "kind": "organic_practice_baseline",
                    "formal_mastery_modified": False,
                },
            )
        if event.get("evidence_kind") == "retention":
            identity = _identity(event)
            entry = retention.setdefault(identity, {"count": 0, "events": []})
            entry["count"] += 1
            entry["events"].append(event["event_id"])
            entry["last_observed_date"] = event["observed_date"]
        for action in event.get("generated_actions", []):
            _merge_projected_action(actions, action)
        route = event.get("route") or {}
        candidate = route.get("candidate")
        if candidate:
            candidate_id = candidate["candidate_id"]
            existing = candidates.get(candidate_id)
            if existing is None:
                candidates[candidate_id] = dict(candidate)
                candidates[candidate_id]["trigger_event_ids"] = [event["event_id"]]
                candidates[candidate_id]["occurrence_count"] = 1
            else:
                if event["event_id"] not in existing["trigger_event_ids"]:
                    existing["trigger_event_ids"].append(event["event_id"])
                    existing["occurrence_count"] += 1

    action_rows = sorted(
        actions.values(),
        key=lambda item: (item["due_date"], item["action_type"], item["action_id"]),
    )
    pending = [item for item in action_rows if item.get("status") == "pending"]
    return {
        "schema": STATE_SCHEMA,
        "event_count": len(events),
        "events": records,
        "actions": action_rows,
        "pending_actions": pending,
        "action_formal_bindings": sorted(
            action_formal_bindings.values(), key=lambda item: item["action_id"]
        ),
        "candidates": sorted(candidates.values(), key=lambda item: item["candidate_id"]),
        "organic_baselines": sorted(baselines.values(), key=lambda item: item["identity"]),
        "retention": retention,
        "confirmation_count": len(confirmations),
        "disposition_count": len(dispositions),
        "date_correction_count": len(date_corrections),
        "date_corrections": date_corrections,
        "scheduling_reconciliation_count": len(scheduling_reconciliations),
        "scheduling_reconciliations": scheduling_reconciliations,
        "review_schedule_ensure_count": len(review_schedule_ensures),
        "review_schedule_ensures": review_schedule_ensures,
        "formal_review_binding_count": len(formal_review_bindings),
        "formal_review_bindings": formal_review_bindings,
        "grader_outcome_correction_count": len(grader_outcome_corrections),
        "grader_outcome_corrections": grader_outcome_corrections,
        "closed_equivalent_item_retirement_count": len(
            closed_equivalent_item_retirements
        ),
        "closed_equivalent_item_retirements": (
            closed_equivalent_item_retirements
        ),
        "formal_write_count": 0,
    }


def load_state(repo: Path) -> dict[str, Any]:
    events = _read_events(repo)
    expected = _reduce(events)
    path = _loop_root(repo) / "state.json"
    try:
        stored = json.loads(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError):
        stored = None
    if stored != expected:
        atomic_write_json(path, expected)
    return expected


def _event_by_key(events: list[dict[str, Any]], key: str) -> dict[str, Any] | None:
    return next((event for event in events if event.get("idempotency_key") == key), None)


def get_review_event(repo: Path, event_id: str) -> dict[str, Any]:
    """Return one canonical event by ID without rebuilding another projection."""
    repo = _resolve_repo(repo)
    event_value = _safe_id(event_id, "event_id", required=True)
    assert event_value
    with runtime_lock(repo):
        root = _loop_root(repo)
        hot_state_dir = root / "hot-state"
        index_dir = root / EVENT_INDEX_DIRNAME
        if (hot_state_dir / "manifest.json").is_file():
            # Dynamic import avoids a module cycle: review_hot_state_408 uses
            # this module's canonical reducer and lock implementation.
            try:
                from review_hot_state_408 import (
                    HotStateError,
                    lookup_event as lookup_hot_review_event,
                )

                hot = lookup_hot_review_event(
                    repo,
                    event_id=event_value,
                    state_dir=hot_state_dir,
                )
            except (ImportError, HotStateError) as exc:
                raise ReviewLoopError(
                    "review event hot state failed closed"
                ) from exc
            matches = [hot["event"]] if hot.get("status") == "found" else []
        elif (index_dir / "manifest.json").is_file():
            try:
                indexed = lookup_indexed_event(
                    root / "events.jsonl", index_dir, event_value
                )
            except BoundedJsonlIndexError as exc:
                raise ReviewLoopError(
                    "review event exact index failed closed"
                ) from exc
            matches = [indexed["event"]] if indexed is not None else []
        else:
            # Recovery compatibility only. Managed hot paths require the cold
            # event-index bootstrap and therefore never enter this branch.
            matches = [
                event
                for event in _read_events(repo)
                if event.get("event_id") == event_value
            ]
    if len(matches) != 1:
        raise ReviewLoopError(
            f"expected exactly one canonical review event for {event_value}, got {len(matches)}"
        )
    return matches[0]


def _action_by_id(state: dict[str, Any], action_id: str | None) -> dict[str, Any] | None:
    if not action_id:
        return None
    return next((item for item in state["actions"] if item.get("action_id") == action_id), None)


def _prior_cross_day(state: dict[str, Any], identity: str, observed_date: dt.date) -> bool | None:
    dates = []
    for event in state["events"]:
        if event.get("event_kind") != "outcome" or _identity(event) != identity:
            continue
        try:
            dates.append(dt.date.fromisoformat(event["observed_date"]))
        except (KeyError, ValueError):
            continue
    if any(day < observed_date for day in dates):
        return True
    return None


def _next_stage(current_stage: str | None, retention_count: int) -> str:
    if current_stage in STAGE_SEQUENCE:
        index = STAGE_SEQUENCE.index(current_stage)
        return STAGE_SEQUENCE[min(index + 1, len(STAGE_SEQUENCE) - 1)]
    return STAGE_SEQUENCE[min(retention_count + 1, len(STAGE_SEQUENCE) - 1)]


def _make_action(
    *,
    action_type: str,
    identity: str,
    source_id: str,
    due_date: dt.date,
    stage: str,
    trigger_event_id: str,
    origin_date: dt.date,
    reason: str,
    first_break: str,
    formal_node_id: str | None,
    review_unit_id: str | None,
) -> dict[str, Any]:
    dedupe = {
        "action_type": action_type,
        "identity": identity,
        "due_date": due_date.isoformat(),
        "stage": stage,
    }
    action_id = content_id("RA", dedupe)
    return {
        "schema": "review_action_v1",
        "action_id": action_id,
        "dedupe_key": canonical_json(dedupe),
        "action_type": action_type,
        "identity": identity,
        "source_id": source_id,
        "formal_node_id": formal_node_id,
        "review_unit_id": review_unit_id,
        "due_date": due_date.isoformat(),
        "stage": stage,
        "origin_date": origin_date.isoformat(),
        "trigger_event_id": trigger_event_id,
        "trigger_event_ids": [trigger_event_id],
        "reason": reason,
        "first_break": first_break,
        "status": "pending",
    }


def _action_trigger_ids(action: dict[str, Any]) -> set[str]:
    return {
        str(value)
        for value in action.get(
            "trigger_event_ids", [action.get("trigger_event_id")]
        )
        if value
    }


def _action_identities(action: dict[str, Any]) -> set[str]:
    return {
        str(value)
        for value in (
            action.get("identity"),
            action.get("source_identity"),
            action.get("bound_identity"),
            action.get("review_unit_id"),
            action.get("source_id"),
        )
        if value
    }


def _prefer_d1_family_action(action: dict[str, Any]) -> tuple[int, int, str]:
    return (
        0 if action.get("stage") == "D1_rescue" else 1,
        0 if action.get("status") == "pending" else 1,
        str(action.get("action_id") or ""),
    )


def _d1_family_dispositions(
    actions: list[dict[str, Any]], authoritative_action_id: str
) -> list[dict[str, str]]:
    return [
        {
            "action_id": str(action["action_id"]),
            "authoritative_action_id": authoritative_action_id,
            "disposition": "closed_equivalent_d1_family",
        }
        for action in sorted(actions, key=lambda item: str(item["action_id"]))
        if action.get("action_id") != authoritative_action_id
        and action.get("status") == "pending"
    ]


def build_review_schedule_ensure_event(
    events: list[dict[str, Any]],
    *,
    batch_id: str,
    capture_id: str,
    source: str,
    study_date: str,
    source_id: str,
    source_review_event_id: str | None,
    source_review_event_identity: str | None,
    review_unit_id: str | None,
    formal_node_id: str | None,
    first_result: str,
    confidence: str,
    prompt_level: str,
    reasoning_break: bool,
    first_break: str,
    first_break_provenance: str,
    policy_version: str,
    evidence_receipt_sha256: str,
    event_time: str,
    idempotency_key: str,
) -> dict[str, Any]:
    """Build one pure scheduling ensure event from frozen learning evidence.

    The caller owns evidence/receipt verification.  This helper owns only the
    scheduling policy and canonical action reuse.  It never creates an outcome,
    retention fact, baseline, mastery fact, or formal write authority.
    """

    if source not in SOURCES | {"current_question"}:
        raise ReviewLoopError(f"invalid ensure learning source: {source}")
    batch = _safe_id(batch_id, "batch_id", required=True)
    capture = _safe_id(capture_id, "capture_id", required=True)
    source_value = _safe_text(source_id, "source_id", required=True, limit=512)
    source_event_id = _safe_id(source_review_event_id, "source_review_event_id")
    source_event_identity = _safe_id(
        source_review_event_identity, "source_review_event_identity"
    )
    review_unit = _safe_id(review_unit_id, "review_unit_id")
    formal_node = _safe_id(formal_node_id, "formal_node_id")
    key = _safe_id(idempotency_key, "idempotency_key", required=True)
    assert batch and capture and source_value and key
    if review_unit and not REVIEW_UNIT_RE.fullmatch(review_unit):
        raise ReviewLoopError("review_unit_id is invalid")
    if formal_node and not FORMAL_NODE_RE.fullmatch(formal_node):
        raise ReviewLoopError("formal_node_id is invalid")
    if first_result not in FIRST_RESULTS:
        raise ReviewLoopError("invalid ensure first_result")
    if confidence not in CONFIDENCE_LEVELS or prompt_level not in PROMPT_LEVELS:
        raise ReviewLoopError("invalid ensure confidence or prompt level")
    if not isinstance(reasoning_break, bool):
        raise ReviewLoopError("reasoning_break must be boolean")
    break_value = _safe_text(first_break, "first_break", required=True, limit=200)
    if first_break_provenance not in FIRST_BREAK_PROVENANCES:
        raise ReviewLoopError("invalid ensure first_break provenance")
    if policy_version != SCHEDULING_POLICY_VERSION:
        raise ReviewLoopError("unsupported scheduling policy version")
    if not SHA256_RE.fullmatch(evidence_receipt_sha256):
        raise ReviewLoopError("evidence_receipt_sha256 must be SHA-256")
    study_day = _parse_date(study_date, "study_date")
    _parse_datetime(event_time)

    request_material = {
        "schema": "review_schedule_ensure_request_v1",
        "batch_id": batch,
        "capture_id": capture,
        "source": source,
        "study_date": study_day.isoformat(),
        "source_id": source_value,
        "source_review_event_id": source_event_id,
        "source_review_event_identity": source_event_identity,
        "review_unit_id": review_unit,
        "formal_node_id": formal_node,
        "first_result": first_result,
        "confidence": confidence,
        "prompt_level": prompt_level,
        "reasoning_break": reasoning_break,
        "first_break": break_value,
        "first_break_provenance": first_break_provenance,
        "policy_version": policy_version,
        "evidence_receipt_sha256": evidence_receipt_sha256,
    }
    request_hash = hashlib.sha256(
        canonical_json(request_material).encode("utf-8")
    ).hexdigest()
    existing = _event_by_key(events, key)
    if existing is not None:
        if (
            existing.get("event_kind") != REVIEW_SCHEDULE_ENSURE_KIND
            or existing.get("request_hash") != request_hash
        ):
            raise ReviewLoopError("ensure idempotency key was reused with drifted evidence")
        return existing

    source_event: dict[str, Any] | None = None
    if source_event_id is not None:
        matches = [
            event
            for event in events
            if event.get("event_id") == source_event_id
        ]
        if len(matches) != 1 or matches[0].get("event_kind") != "outcome":
            raise ReviewLoopError("source_review_event_id is not one canonical outcome")
        source_event = matches[0]
        actual_identity = str(
            source_event.get("review_unit_id")
            or source_event.get("formal_node_id")
            or source_event.get("source_id")
            or ""
        )
        if source_event_identity is not None and actual_identity != source_event_identity:
            raise ReviewLoopError("source review event identity conflicts with ensure")
        if not source_study_date_matches(source_event, study_day.isoformat()):
            raise ReviewLoopError("source review event date conflicts with ensure")

    state = _reduce(events)
    actions = list(state.get("pending_actions") or [])
    pending_actions = list(state.get("pending_actions") or [])
    # source_event_identity binds the historical evidence. Explicit current
    # targets can differ after a verified source-binding correction.
    identity = str(review_unit or formal_node or source_value or capture)
    aliases = {identity, capture, source_value}
    if review_unit:
        aliases.add(review_unit)
    due = study_day + dt.timedelta(days=1)
    generated: list[dict[str, Any]] = []
    family: str | None = None
    review_action_id: str | None = None
    scheduled_due_date: str | None = None
    equivalence: list[dict[str, str]] = []

    no_schedule = ordinary_independent_correct_needs_no_schedule(
        source="daily_practice" if source == "current_question" else source,
        first_result=first_result,
        confidence=confidence,
        prompt_level=prompt_level,
        reasoning_break=reasoning_break,
    )
    morning_source_actions = [
        action
        for action in pending_actions
        if action.get("action_type") == "review"
        and action.get("status") == "pending"
        and source_event_id is not None
        and source_event_id in _action_trigger_ids(action)
        and (
            source_event_identity == identity
            or (
                (action.get("formal_node_id") or None) == formal_node
                and (action.get("review_unit_id") or None) == review_unit
                and identity in _action_identities(action)
            )
        )
    ]
    corrected_morning_failure = bool(
        source == "morning_review"
        and source_event is not None
        and (
            (
                source_event.get("first_result") == "independent_correct"
                and (
                    first_result in {"wrong", "partial", "blank", "uncertain"}
                    or reasoning_break
                )
            )
            or (
                source_event.get("first_result")
                in {"wrong", "partial", "uncertain"}
                and not morning_source_actions
            )
        )
    )
    if no_schedule:
        disposition = "maintenance_noop"
    elif source == "morning_review" and not corrected_morning_failure:
        if source_event is None:
            raise ReviewLoopError("morning ensure requires source_review_event_id")
        matching = morning_source_actions
        if len(matching) != 1:
            raise ReviewLoopError(
                "morning source outcome lacks one canonical successor; reconcile scheduling first"
            )
        selected = matching[0]
        review_action_id = str(selected["action_id"])
        scheduled_due_date = str(selected["due_date"])
        family = review_action_family(str(selected.get("stage") or ""))
        disposition = "reused_existing_action"
    else:
        slot_actions = [
            action
            for action in pending_actions
            if action.get("action_type") == "review"
            and review_action_family(str(action.get("stage") or ""))
            == "D1_family"
            and action.get("due_date") == due.isoformat()
            and bool(_action_identities(action) & aliases)
        ]
        if slot_actions:
            causal_matches = [
                action
                for action in slot_actions
                if source_event_id is not None
                and source_event_id in _action_trigger_ids(action)
            ]
            selected = sorted(
                causal_matches or slot_actions,
                key=_prefer_d1_family_action,
            )[0]
            rescue = [
                action for action in slot_actions if action.get("stage") == "D1_rescue"
            ]
            if rescue:
                selected = sorted(rescue, key=_prefer_d1_family_action)[0]
            review_action_id = str(selected["action_id"])
            scheduled_due_date = str(selected["due_date"])
            family = "D1_family"
            disposition = "reused_existing_action"
            equivalence = _d1_family_dispositions(slot_actions, review_action_id)
        else:
            event_id = content_id(
                "RE", {"kind": REVIEW_SCHEDULE_ENSURE_KIND, "key": key}
            )
            action = _make_action(
                action_type="review",
                identity=(
                    formal_node
                    if corrected_morning_failure and formal_node
                    else identity
                ),
                source_id=source_value,
                due_date=due,
                stage=("D1_rescue" if corrected_morning_failure else "D1"),
                trigger_event_id=event_id,
                origin_date=study_day,
                reason="formal intake failure-standing mechanism backflow",
                first_break=break_value,
                formal_node_id=formal_node,
                review_unit_id=review_unit,
            )
            action["origin_kind"] = "formal_intake_backflow"
            generated = [action]
            review_action_id = str(action["action_id"])
            scheduled_due_date = str(action["due_date"])
            family = "D1_family"
            disposition = "created_action"

    event_id = content_id(
        "RE", {"kind": REVIEW_SCHEDULE_ENSURE_KIND, "key": key}
    )
    event = {
        "schema": LEDGER_SCHEMA,
        "event_kind": REVIEW_SCHEDULE_ENSURE_KIND,
        "event_id": event_id,
        "idempotency_key": key,
        "request_hash": request_hash,
        "event_time": event_time,
        "observed_date": study_day.isoformat(),
        "timezone": "Asia/Shanghai",
        "source": "formal_intake_backflow",
        "learning_source": source,
        "batch_id": batch,
        "capture_id": capture,
        "source_id": source_value,
        "source_review_event_id": source_event_id,
        "source_review_event_identity": source_event_identity,
        "review_unit_id": review_unit,
        "formal_node_id": formal_node,
        "first_result": first_result,
        "confidence": confidence,
        "prompt_level": prompt_level,
        "reasoning_break": reasoning_break,
        "first_break": break_value,
        "first_break_provenance": first_break_provenance,
        "policy_version": policy_version,
        "evidence_receipt_sha256": evidence_receipt_sha256,
        "schedule_disposition": disposition,
        "review_action_id": review_action_id,
        "due_date": scheduled_due_date,
        "action_family": family,
        "equivalent_action_dispositions": equivalence,
        "organic_baseline": False,
        "stable_retention": False,
        "evidence_kind": "formal_schedule_ensure",
        "trigger_action_id": None,
        "generated_actions": generated,
        "route": {
            "kind": REVIEW_SCHEDULE_ENSURE_KIND,
            "formal_write_authorized": False,
        },
        "formal_write_authorized": False,
    }
    _validate_review_control_event(event)
    _reduce([*events, event])
    return event


def build_grader_outcome_correction_event(
    events: list[dict[str, Any]],
    *,
    batch_id: str,
    capture_id: str,
    source_outcome_event_id: str,
    source_outcome_event_sha256: str,
    corrected_ensure_event_id: str,
    corrected_ensure_event_sha256: str,
    event_time: str,
    idempotency_key: str,
    reason_code: str = "prepared_grader_key_conflict",
) -> dict[str, Any]:
    """Supersede only scheduling/retention derived from one proven bad grader key."""

    source_matches = [
        row for row in events
        if row.get("event_id") == source_outcome_event_id
        and row.get("event_kind") == "outcome"
    ]
    ensure_matches = [
        row for row in events
        if row.get("event_id") == corrected_ensure_event_id
        and row.get("event_kind") == REVIEW_SCHEDULE_ENSURE_KIND
    ]
    if len(source_matches) != 1 or _sha256_value(source_matches[0]) != source_outcome_event_sha256:
        raise ReviewLoopError("grader correction source outcome proof is invalid")
    if len(ensure_matches) != 1 or _sha256_value(ensure_matches[0]) != corrected_ensure_event_sha256:
        raise ReviewLoopError("grader correction ensure proof is invalid")
    source_event = source_matches[0]
    ensure_event = ensure_matches[0]
    if (
        ensure_event.get("batch_id") != batch_id
        or ensure_event.get("capture_id") != capture_id
        or ensure_event.get("source_review_event_id") != source_outcome_event_id
        or ensure_event.get("first_result") == "independent_correct"
    ):
        raise ReviewLoopError("grader correction evidence does not establish a corrected failure")
    state = _reduce(events)
    closed_action_ids = sorted(
        str(row["action_id"])
        for row in state.get("pending_actions") or []
        if source_outcome_event_id in _action_trigger_ids(row)
        and len(_action_trigger_ids(row)) == 1
    )
    if not closed_action_ids:
        raise ReviewLoopError("grader correction found no exclusive derived action")
    key = _safe_id(idempotency_key, "idempotency_key", required=True)
    assert key
    if reason_code not in {
        "prepared_grader_key_conflict",
        "choice_only_grader_reasoning_conflict",
    }:
        raise ReviewLoopError("grader correction reason is invalid")
    request = {
        "schema": "grader_outcome_correction_request_v1",
        "batch_id": batch_id,
        "capture_id": capture_id,
        "source_outcome_event_id": source_outcome_event_id,
        "source_outcome_event_sha256": source_outcome_event_sha256,
        "corrected_ensure_event_id": corrected_ensure_event_id,
        "corrected_ensure_event_sha256": corrected_ensure_event_sha256,
        "closed_action_ids": closed_action_ids,
        "reason_code": reason_code,
    }
    request_hash = hashlib.sha256(canonical_json(request).encode("utf-8")).hexdigest()
    existing = _event_by_key(events, key)
    if existing is not None:
        if (
            existing.get("event_kind") != GRADER_OUTCOME_CORRECTION_KIND
            or existing.get("request_hash") != request_hash
        ):
            raise ReviewLoopError("grader correction idempotency key drifted")
        return existing
    event = {
        "schema": LEDGER_SCHEMA,
        "event_kind": GRADER_OUTCOME_CORRECTION_KIND,
        "event_id": content_id("RE", {"kind": GRADER_OUTCOME_CORRECTION_KIND, "key": key}),
        "idempotency_key": key,
        "request_hash": request_hash,
        "event_time": event_time,
        "observed_date": str(ensure_event["observed_date"]),
        "timezone": "Asia/Shanghai",
        "source": "formal_intake_backflow",
        "batch_id": batch_id,
        "capture_id": capture_id,
        "source_outcome_event_id": source_outcome_event_id,
        "source_outcome_event_sha256": source_outcome_event_sha256,
        "corrected_ensure_event_id": corrected_ensure_event_id,
        "corrected_ensure_event_sha256": corrected_ensure_event_sha256,
        "closed_action_ids": closed_action_ids,
        "reason_code": reason_code,
        "organic_baseline": False,
        "stable_retention": False,
        "trigger_action_id": None,
        "generated_actions": [],
        "route": {
            "kind": GRADER_OUTCOME_CORRECTION_KIND,
            "formal_write_authorized": False,
        },
        "formal_write_authorized": False,
    }
    _validate_review_control_event(event)
    _reduce([*events, event])
    return event


def build_formal_review_binding_event(
    events: list[dict[str, Any]],
    *,
    batch_id: str,
    capture_id: str,
    review_action_id: str | None,
    ensure_event_id: str,
    source_id: str,
    source_review_event_id: str | None,
    formal_result_status: str,
    formal_result_receipt_sha256: str,
    ensure_receipt_sha256: str,
    formal_node_id: str,
    review_unit_id: str,
    observed_date: str,
    event_time: str,
    idempotency_key: str,
) -> dict[str, Any]:
    """Build a receipt-bound formal mapping without guessing an RU identity."""

    batch = _safe_id(batch_id, "batch_id", required=True)
    capture = _safe_id(capture_id, "capture_id", required=True)
    action_id = _safe_id(review_action_id, "review_action_id")
    ensure_id = _safe_id(ensure_event_id, "ensure_event_id", required=True)
    source_value = _safe_text(source_id, "source_id", required=True, limit=512)
    source_event_id = _safe_id(source_review_event_id, "source_review_event_id")
    formal_node = _safe_id(formal_node_id, "formal_node_id", required=True)
    review_unit = _safe_id(review_unit_id, "review_unit_id", required=True)
    key = _safe_id(idempotency_key, "idempotency_key", required=True)
    assert batch and capture and ensure_id and source_value and formal_node and review_unit and key
    if not FORMAL_NODE_RE.fullmatch(formal_node):
        raise ReviewLoopError("formal_node_id is invalid")
    if not REVIEW_UNIT_RE.fullmatch(review_unit):
        raise ReviewLoopError("receipt-bound review_unit_id is invalid")
    if formal_result_status not in FORMAL_RESULT_STATUSES:
        raise ReviewLoopError("formal_result_status is invalid")
    if not SHA256_RE.fullmatch(formal_result_receipt_sha256):
        raise ReviewLoopError("formal_result_receipt_sha256 must be SHA-256")
    if not SHA256_RE.fullmatch(ensure_receipt_sha256):
        raise ReviewLoopError("ensure_receipt_sha256 must be SHA-256")
    day = _parse_date(observed_date, "observed_date")
    _parse_datetime(event_time)
    ensure_matches = [
        event
        for event in events
        if event.get("event_id") == ensure_id
        and event.get("event_kind") == REVIEW_SCHEDULE_ENSURE_KIND
    ]
    if len(ensure_matches) != 1:
        raise ReviewLoopError("formal binding requires one canonical ensure event")
    ensure = ensure_matches[0]
    if (
        ensure.get("batch_id") != batch
        or ensure.get("capture_id") != capture
        or ensure.get("review_action_id") != action_id
        or ensure.get("source_review_event_id") != source_event_id
    ):
        raise ReviewLoopError("formal binding conflicts with ensure identity")

    request_material = {
        "schema": "formal_review_binding_request_v1",
        "batch_id": batch,
        "capture_id": capture,
        "review_action_id": action_id,
        "ensure_event_id": ensure_id,
        "source_id": source_value,
        "source_review_event_id": source_event_id,
        "formal_result_status": formal_result_status,
        "formal_result_receipt_sha256": formal_result_receipt_sha256,
        "ensure_receipt_sha256": ensure_receipt_sha256,
        "formal_node_id": formal_node,
        "review_unit_id": review_unit,
        "observed_date": day.isoformat(),
    }
    request_hash = hashlib.sha256(
        canonical_json(request_material).encode("utf-8")
    ).hexdigest()
    existing = _event_by_key(events, key)
    if existing is not None:
        if (
            existing.get("event_kind") != FORMAL_REVIEW_BINDING_KIND
            or existing.get("request_hash") != request_hash
        ):
            raise ReviewLoopError("binding idempotency key was reused with drifted proof")
        return existing

    state = _reduce(events)
    actions = list(state.get("pending_actions") or [])
    equivalence: list[dict[str, str]] = []
    canonical_action_id: str | None = None
    if action_id is not None:
        ensured = next(
            (action for action in actions if action.get("action_id") == action_id),
            None,
        )
        if ensured is None:
            raise ReviewLoopError("formal binding review action is unknown")
        slot_aliases = _action_identities(ensured) | {review_unit}
        slot = [
            action
            for action in actions
            if action.get("action_type") == "review"
            and action.get("due_date") == ensured.get("due_date")
            and review_action_family(str(action.get("stage") or ""))
            == review_action_family(str(ensured.get("stage") or ""))
            and bool(_action_identities(action) & slot_aliases)
        ]
        rescue = [action for action in slot if action.get("stage") == "D1_rescue"]
        authoritative = sorted(
            rescue or slot,
            key=lambda action: (
                0 if review_unit in _action_identities(action) else 1,
                *_prefer_d1_family_action(action),
            ),
        )[0]
        canonical_action_id = str(authoritative["action_id"])
        if review_action_family(str(ensured.get("stage") or "")) == "D1_family":
            equivalence = _d1_family_dispositions(slot, canonical_action_id)
    elif ensure.get("schedule_disposition") != "maintenance_noop":
        raise ReviewLoopError("actionless binding requires maintenance ensure")

    event_id = content_id(
        "RE", {"kind": FORMAL_REVIEW_BINDING_KIND, "key": key}
    )
    event = {
        "schema": LEDGER_SCHEMA,
        "event_kind": FORMAL_REVIEW_BINDING_KIND,
        "event_id": event_id,
        "idempotency_key": key,
        "request_hash": request_hash,
        "event_time": event_time,
        "observed_date": day.isoformat(),
        "timezone": "Asia/Shanghai",
        "source": "formal_intake_backflow",
        "batch_id": batch,
        "capture_id": capture,
        "source_id": source_value,
        "source_review_event_id": source_event_id,
        "ensure_event_id": ensure_id,
        "review_action_id": action_id,
        "canonical_review_action_id": canonical_action_id,
        "formal_result_status": formal_result_status,
        "formal_result_receipt_sha256": formal_result_receipt_sha256,
        "ensure_receipt_sha256": ensure_receipt_sha256,
        "formal_node_id": formal_node,
        "review_unit_id": review_unit,
        "review_unit_identity_basis": "formal_result_receipt",
        "equivalent_action_dispositions": equivalence,
        "organic_baseline": False,
        "stable_retention": False,
        "evidence_kind": "formal_review_binding",
        "trigger_action_id": None,
        "generated_actions": [],
        "route": {
            "kind": FORMAL_REVIEW_BINDING_KIND,
            "formal_write_authorized": False,
        },
        "formal_write_authorized": False,
    }
    _validate_review_control_event(event)
    _reduce([*events, event])
    return event


def append_review_control_event(
    repo: Path,
    event: dict[str, Any],
    *,
    converge_hot_state: bool = True,
) -> dict[str, Any]:
    """Idempotently append one control event and converge an active hot state."""

    repo = _resolve_repo(repo)
    _validate_review_control_event(event)
    if event.get("event_kind") not in REVIEW_CONTROL_EVENT_KINDS:
        raise ReviewLoopError("append_review_control_event accepts control events only")
    with runtime_lock(repo):
        events = _read_events(repo)
        existing = _event_by_key(events, str(event["idempotency_key"]))
        if existing is not None:
            if existing != event:
                raise ReviewLoopError(
                    "control event idempotency key was reused with drifted evidence"
                )
            created = False
            committed_event = existing
            state = _reduce(events)
        else:
            created = True
            committed_event = event
            state = _reduce([*events, event])
            append_jsonl(_loop_root(repo) / "events.jsonl", event)
        atomic_write_json(_loop_root(repo) / "state.json", state)
        hot_result: dict[str, Any] = {
            "status": "not_present",
            "rebuild_performed": False,
            "formal_write_count": 0,
        }
        hot_pointer = _loop_root(repo) / "hot-state" / "manifest.json"
        if converge_hot_state and hot_pointer.is_file():
            try:
                import review_hot_state_408 as hot_state

                rebuilt = hot_state.cold_rebuild(repo)
            except Exception as exc:
                raise ReviewLoopError(
                    "control event committed but review hot-state convergence failed"
                ) from exc
            hot_result = {
                "status": rebuilt.get("status"),
                "rebuild_performed": True,
                "projection_build_id": rebuilt.get("projection_build_id"),
                "review_schedule_ensure_count": rebuilt.get(
                    "review_schedule_ensure_count"
                ),
                "formal_review_binding_count": rebuilt.get(
                    "formal_review_binding_count"
                ),
                "formal_write_count": rebuilt.get("formal_write_count", 0),
            }
        runtime = _write_runtime_views_locked(repo)
    return {
        "created": created,
        "event": committed_event,
        "state": state,
        "hot_state": hot_result,
        "runtime": runtime,
    }


def _parse_optional_date(value: str) -> dt.date | None:
    if not value or value in {"未记录", "待确认", "待补充", "无"}:
        return None
    try:
        return dt.date.fromisoformat(value)
    except ValueError as exc:
        raise ReviewLoopError(f"invalid date in original track: {value}") from exc


def _original_actions(
    repo: Path,
    *,
    event_id: str,
    observed_date: dt.date,
    review_unit_id: str | None,
    source_id: str,
    first_result: str,
    hint_used: bool,
    question_valid: bool,
    fragile: bool = False,
    current_formal_node_id: str | None = None,
) -> list[dict[str, Any]]:
    if (
        not review_unit_id
        or first_result != "independent_correct"
        or hint_used
        or not question_valid
        or fragile
    ):
        return []
    current_node = (
        current_formal_node_id
        if current_formal_node_id and FORMAL_NODE_RE.fullmatch(current_formal_node_id)
        else source_id if FORMAL_NODE_RE.fullmatch(source_id) else None
    )
    result: list[dict[str, Any]] = []
    for row in _read_markdown_table(repo / "原题复做轨总表.md", "正式节点ID"):
        if row.get("主复习单元ID") != review_unit_id:
            continue
        if row.get("原题轨状态", "").startswith("blocked"):
            continue
        node_id = row.get("正式节点ID", "")
        if not FORMAL_NODE_RE.fullmatch(node_id):
            continue
        last = _parse_optional_date(row.get("最近复做日期", ""))
        earliest = _parse_optional_date(row.get("最早允许日期", ""))
        if node_id == current_node:
            due = observed_date + dt.timedelta(days=7)
            action_reason = "current original attempt resets the 7-day gate"
        elif last is not None:
            expected = last + dt.timedelta(days=7)
            if earliest is not None and earliest != expected:
                raise ReviewLoopError(f"{node_id} has inconsistent 7-day original-track gate")
            due = max(observed_date, expected)
            action_reason = "organic or retained unhinted mechanism evidence; 7-day gate satisfied"
        else:
            due = observed_date
            action_reason = "organic or retained unhinted mechanism evidence; 7-day gate satisfied"
        result.append(
            _make_action(
                action_type="original_question",
                identity=node_id,
                source_id=node_id,
                due_date=due,
                stage="original_track",
                trigger_event_id=event_id,
                origin_date=observed_date,
                reason=action_reason,
                first_break="none",
                formal_node_id=node_id,
                review_unit_id=review_unit_id,
            )
        )
    return result


def _route_candidate(
    *,
    event_id: str,
    first_result: str,
    first_break: str,
    mechanism_key: str,
    mapped: list[str],
    formal_node_id: str | None,
    review_unit_id: str | None,
) -> dict[str, Any]:
    if first_result == "independent_correct":
        return {"kind": "evidence_only", "formal_write_authorized": False}
    useful_break = first_break.lower() not in NONE_BREAKS
    explicit_node = formal_node_id if formal_node_id and FORMAL_NODE_RE.fullmatch(formal_node_id) else None
    unique_node = explicit_node or (mapped[0] if len(mapped) == 1 else None)
    if not useful_break:
        kind = "needs_user"
        status = "needs_user"
        target = unique_node
        reason = "first breakpoint is insufficient; do not infer the user's error mechanism"
    elif unique_node:
        kind = "update_existing"
        status = "pending_user_confirmation"
        target = unique_node
        reason = "an existing mapped error mechanism recurred"
    elif mapped:
        kind = "needs_user"
        status = "needs_user"
        target = None
        reason = "the review unit maps to multiple formal nodes"
    else:
        kind = "new_wrong_candidate"
        status = "pending_user_confirmation"
        target = None
        reason = "a new answer-safe error mechanism was observed without a formal-node mapping"
    candidate_identity = {
        "kind": kind,
        "target": target,
        "review_unit_id": review_unit_id,
        "mechanism_key": mechanism_key,
        "first_break": first_break if useful_break else "needs_user",
    }
    candidate = {
        "schema": "review_route_candidate_v1",
        "candidate_id": content_id("RC", candidate_identity),
        "kind": kind,
        "status": status,
        "formal_node_id": target,
        "review_unit_id": review_unit_id,
        "mechanism_key": mechanism_key,
        "first_break": first_break,
        "reason": reason,
        "formal_write_authorized": False,
        "created_by_event_id": event_id,
        "required_handoff": "kaoyan-408-wrong-intake" if status == "pending_user_confirmation" else None,
    }
    return {"kind": kind, "formal_write_authorized": False, "candidate": candidate}


@study_commit_boundary
def record_outcome(
    repo: Path,
    *,
    source: str,
    session_id: str,
    item_id: str,
    source_id: str,
    mechanism_key: str,
    knowledge_point: str,
    first_result: str,
    choice_result: str = "not_applicable",
    reasoning_result: str = "not_observed",
    confidence: str,
    prompt_level: str,
    first_break: str,
    event_time: str,
    observed_date: str,
    idempotency_key: str,
    session_review_date: str | None = None,
    correction_result: str = "not_attempted",
    response_seconds: float | None = None,
    latency_state: str = "unmeasured",
    cross_day: bool | None = None,
    question_valid: bool = True,
    formal_node_id: str | None = None,
    review_unit_id: str | None = None,
    trigger_action_id: str | None = None,
    legacy_import: bool = False,
    fragile_override: bool = False,
    first_break_provenance: str = "derived_classification",
    practice_mode: str | None = None,
    anchor_formal_node_id: str | None = None,
    variant_of_formal_node_id: str | None = None,
    followup_event_id: str | None = None,
    parent_trigger_event_id: str | None = None,
    context_slice_id: str | None = None,
    candidate_id: str | None = None,
    source_ref: str | None = None,
    source_sha256: str | None = None,
    selection_receipt_ref: str | None = None,
    selection_receipt_sha256: str | None = None,
    presentation_kind: str | None = None,
    deferred_morning_binding: dict[str, Any] | None = None,
    _from_session_sync: bool = False,
) -> dict[str, Any]:
    repo = _resolve_repo(repo)
    if source not in SOURCES:
        raise ReviewLoopError(f"invalid source: {source}")
    if first_result not in FIRST_RESULTS or correction_result not in CORRECTION_RESULTS:
        raise ReviewLoopError("invalid first/correction result")
    if choice_result not in CHOICE_RESULTS or reasoning_result not in REASONING_RESULTS:
        raise ReviewLoopError("invalid choice or reasoning result")
    if first_result == "independent_correct" and (
        choice_result in {"incorrect", "partial", "uncertain"}
        or reasoning_result in {"partial", "diverged"}
    ):
        raise ReviewLoopError(
            "independent_correct conflicts with choice or reasoning result"
        )
    if confidence not in CONFIDENCE_LEVELS or prompt_level not in PROMPT_LEVELS:
        raise ReviewLoopError("invalid confidence or prompt level")
    if first_break_provenance not in FIRST_BREAK_PROVENANCES:
        raise ReviewLoopError("invalid first_break provenance")
    if latency_state not in LATENCY_STATES or cross_day not in CROSS_DAY_STATES:
        raise ReviewLoopError("invalid latency or cross-day state")
    if first_result == "independent_correct" and prompt_level != "none":
        raise ReviewLoopError("independent_correct requires prompt_level=none")
    if response_seconds is not None and not 0 < response_seconds <= 7200:
        raise ReviewLoopError("response_seconds must be > 0 and <= 7200; use null when unmeasured")
    parsed_event_time = _parse_datetime(event_time)
    requested_day = _parse_date(observed_date, "observed_date")
    local_day = parsed_event_time.astimezone(LOCAL_TZ).date()
    session_day: dt.date | None = None
    if source == "morning_review":
        session_day = _parse_date(
            session_review_date or requested_day.isoformat(),
            "session_review_date",
        )
        # Morning sessions may be resumed days later.  The immutable queue date
        # remains separate while all scheduling and study-day semantics use the
        # timestamp-derived actual answer date.
        day = local_day
    else:
        if session_review_date is not None:
            raise ReviewLoopError(
                "session_review_date is only valid for morning_review"
            )
        day = requested_day
    if (
        source in {"daily_practice", "evening_d0"}
        and not legacy_import
        and local_day != day
    ):
        raise ReviewLoopError(
            "event_time Asia/Shanghai date must equal observed_date"
        )
    session_id_value = _safe_id(session_id, "session_id", required=True)
    item_id_value = _safe_id(item_id, "item_id", required=True)
    source_id_value = _safe_text(source_id, "source_id", required=True, limit=512)
    mechanism_value = _safe_text(mechanism_key, "mechanism_key", required=True)
    knowledge_value = _safe_text(knowledge_point, "knowledge_point")
    break_value = _safe_text(first_break, "first_break", required=True, limit=200)
    formal_node = _safe_id(formal_node_id, "formal_node_id")
    review_unit = _safe_id(review_unit_id, "review_unit_id")
    trigger_action = _safe_id(trigger_action_id, "trigger_action_id")
    presentation = _safe_id(presentation_kind, "presentation_kind")
    key = _safe_id(idempotency_key, "idempotency_key", required=True)
    assert session_id_value and item_id_value and source_id_value and key
    deferred_binding: dict[str, Any] | None = None
    if deferred_morning_binding is not None:
        required_deferred = {
            "schema", "capture_id", "capture_payload_sha256", "episode_id",
            "execution_kind", "evidence_locator", "evidence_sha256",
            "display_binding_sha256", "observed_outcome_sha256",
            "decision_sha256", "time_quality", "time_source_event_id",
            "time_source_event_sha256",
        }
        if (
            not isinstance(deferred_morning_binding, dict)
            or set(deferred_morning_binding) != required_deferred
            or deferred_morning_binding.get("schema")
            != DEFERRED_MORNING_BINDING_SCHEMA
            or deferred_morning_binding.get("execution_kind")
            != "original_review"
        ):
            raise ReviewLoopError("deferred morning binding fields are invalid")
        for label in (
            "capture_payload_sha256", "evidence_sha256",
            "display_binding_sha256", "observed_outcome_sha256",
            "decision_sha256",
        ):
            if not SHA256_RE.fullmatch(
                str(deferred_morning_binding.get(label) or "")
            ):
                raise ReviewLoopError(
                    "deferred morning binding contains an invalid SHA-256"
                )
        time_quality = deferred_morning_binding.get("time_quality")
        time_event_id = deferred_morning_binding.get("time_source_event_id")
        time_event_sha = deferred_morning_binding.get(
            "time_source_event_sha256"
        )
        if time_quality == "user_turn_observed_at":
            if time_event_id is not None or time_event_sha is not None:
                raise ReviewLoopError(
                    "direct learner timestamp cannot claim a runtime source event"
                )
        elif time_quality == "runtime_observation_recorded_at":
            if (
                not _safe_id(
                    time_event_id,
                    "deferred_morning_binding.time_source_event_id",
                    required=True,
                )
                or not SHA256_RE.fullmatch(str(time_event_sha or ""))
            ):
                raise ReviewLoopError(
                    "runtime observation timestamp proof is invalid"
                )
        else:
            raise ReviewLoopError("deferred morning time quality is invalid")
        deferred_capture = _safe_id(
            deferred_morning_binding.get("capture_id"),
            "deferred_morning_binding.capture_id", required=True,
        )
        deferred_episode = _safe_id(
            deferred_morning_binding.get("episode_id"),
            "deferred_morning_binding.episode_id", required=True,
        )
        deferred_locator = _safe_text(
            deferred_morning_binding.get("evidence_locator"),
            "deferred_morning_binding.evidence_locator", required=True,
            limit=1024,
        )
        deferred_key = "formal-enhancement-morning:" + hashlib.sha256(
            canonical_json([deferred_episode, deferred_capture]).encode("utf-8")
        ).hexdigest()
        if (
            source != "morning_review"
            or not _from_session_sync
            or practice_mode is not None
            or key != deferred_key
        ):
            raise ReviewLoopError(
                "deferred morning binding requires the canonical enhancement identity"
            )
        deferred_binding = {
            **deferred_morning_binding,
            "capture_id": deferred_capture,
            "episode_id": deferred_episode,
            "evidence_locator": deferred_locator,
        }
    if formal_node and not FORMAL_NODE_RE.fullmatch(formal_node):
        raise ReviewLoopError("formal_node_id is invalid")
    if review_unit and not REVIEW_UNIT_RE.fullmatch(review_unit):
        raise ReviewLoopError("review_unit_id is invalid")
    if review_unit is None and REVIEW_UNIT_RE.fullmatch(source_id_value):
        review_unit = source_id_value
    if formal_node is None and FORMAL_NODE_RE.fullmatch(source_id_value):
        formal_node = source_id_value
    followup_payload: dict[str, Any] | None = None
    if practice_mode is not None:
        if source != "morning_review" or not _from_session_sync:
            raise ReviewLoopError("linked practice must be synchronized from a morning session")
        if practice_mode not in FOLLOWUP_PRACTICE_MODES:
            raise ReviewLoopError("invalid linked-practice mode")
        variant_origin = _safe_id(
            variant_of_formal_node_id, "variant_of_formal_node_id"
        )
        anchor_formal = _safe_id(
            anchor_formal_node_id, "anchor_formal_node_id"
        )
        if anchor_formal and not FORMAL_NODE_RE.fullmatch(anchor_formal):
            raise ReviewLoopError("anchor_formal_node_id is invalid")
        followup_event = _safe_id(
            followup_event_id, "followup_event_id", required=True
        )
        parent_trigger = _safe_id(
            parent_trigger_event_id, "parent_trigger_event_id", required=True
        )
        slice_id = _safe_id(context_slice_id, "context_slice_id", required=True)
        candidate = _safe_id(candidate_id, "candidate_id", required=True)
        source_reference = _safe_text(
            source_ref, "source_ref", required=True, limit=1024
        )
        source_digest = _safe_text(
            source_sha256, "source_sha256", required=True, limit=64
        )
        if not re.fullmatch(r"[0-9a-f]{64}", source_digest):
            raise ReviewLoopError("source_sha256 must be lowercase SHA-256")
        selection_reference = _safe_text(
            selection_receipt_ref,
            "selection_receipt_ref",
            required=True,
            limit=1024,
        )
        selection_digest = _safe_text(
            selection_receipt_sha256,
            "selection_receipt_sha256",
            required=True,
            limit=64,
        )
        if not re.fullmatch(r"[0-9a-f]{64}", selection_digest):
            raise ReviewLoopError(
                "selection_receipt_sha256 must be lowercase SHA-256"
            )
        if practice_mode == "original":
            if not formal_node or variant_origin:
                raise ReviewLoopError(
                    "original linked practice must bind one formal_node_id only"
                )
        elif formal_node or not variant_origin or not FORMAL_NODE_RE.fullmatch(
            variant_origin
        ):
            raise ReviewLoopError(
                "variant linked practice must not bind the original as formal_node_id"
            )
        if practice_mode == "variant" and anchor_formal != variant_origin:
            raise ReviewLoopError("variant linked-practice anchor is inconsistent")
        followup_payload = {
            "practice_mode": practice_mode,
            "anchor_formal_node_id": anchor_formal,
            "variant_of_formal_node_id": variant_origin,
            "followup_event_id": followup_event,
            "parent_trigger_event_id": parent_trigger,
            "context_slice_id": slice_id,
            "candidate_id": candidate,
            "source_ref": source_reference,
            "source_sha256": source_digest,
            "selection_receipt_ref": selection_reference,
            "selection_receipt_sha256": selection_digest,
        }

    request_payload = {
        "source": source,
        "session_id": session_id_value,
        "item_id": item_id_value,
        "source_id": source_id_value,
        "mechanism_key": mechanism_value,
        "knowledge_point": knowledge_value,
        "first_result": first_result,
        "choice_result": choice_result,
        "reasoning_result": reasoning_result,
        "confidence": confidence,
        "prompt_level": prompt_level,
        "first_break": break_value,
        "event_time": event_time,
        "observed_date": day.isoformat(),
        "correction_result": correction_result,
        "response_seconds": response_seconds,
        "latency_state": latency_state,
        "cross_day": cross_day,
        "question_valid": bool(question_valid),
        "formal_node_id": formal_node,
        "review_unit_id": review_unit,
        "trigger_action_id": trigger_action,
        "presentation_kind": presentation,
        "legacy_import": bool(legacy_import),
        "fragile_override": bool(fragile_override),
    }
    if deferred_binding is not None:
        request_payload["deferred_morning_binding"] = deferred_binding
    if session_day is not None:
        request_payload["session_review_date"] = session_day.isoformat()
    if first_break_provenance != "derived_classification":
        request_payload["first_break_provenance"] = first_break_provenance
    if followup_payload is not None:
        request_payload.update(followup_payload)
    request_hash = hashlib.sha256(canonical_json(request_payload).encode("utf-8")).hexdigest()

    with runtime_lock(repo):
        events = _read_events(repo)
        existing = _event_by_key(events, key)
        if existing:
            inferred_legacy_provenance = normalize_first_break_provenance(
                None, existing.get("first_break")
            )
            if (
                "first_break_provenance" not in existing
                and first_break_provenance != inferred_legacy_provenance
            ):
                raise ReviewLoopError(
                    "idempotency key was reused with different review evidence"
                )
            if existing.get("request_hash") != request_hash:
                legacy_payload = dict(request_payload)
                if (
                    "first_break_provenance" not in existing
                    and first_break_provenance == inferred_legacy_provenance
                ):
                    legacy_payload.pop("first_break_provenance", None)
                if "choice_result" not in existing and choice_result == "not_applicable":
                    legacy_payload.pop("choice_result", None)
                if "reasoning_result" not in existing and reasoning_result == "not_observed":
                    legacy_payload.pop("reasoning_result", None)
                compatible_payloads = [legacy_payload]
                if (
                    source == "morning_review"
                    and session_day is not None
                    and "session_review_date" not in existing
                    and existing.get("observed_date") == session_day.isoformat()
                    and _event_local_date(str(existing.get("event_time") or ""))
                    == day
                ):
                    historical = dict(legacy_payload)
                    historical.pop("session_review_date", None)
                    historical["observed_date"] = session_day.isoformat()
                    compatible_payloads.append(historical)
                legacy_hashes: set[str] = set()
                for compatible in compatible_payloads:
                    legacy_hashes.add(
                        hashlib.sha256(
                            canonical_json(compatible).encode("utf-8")
                        ).hexdigest()
                    )
                    if existing.get("presentation_kind") == presentation:
                        no_presentation = dict(compatible)
                        no_presentation.pop("presentation_kind", None)
                        legacy_hashes.add(
                            hashlib.sha256(
                                canonical_json(no_presentation).encode("utf-8")
                            ).hexdigest()
                        )
                legacy_replay = (
                    existing.get("request_hash") in legacy_hashes
                )
                if not legacy_replay:
                    raise ReviewLoopError(
                        "idempotency key was reused with different review evidence"
                    )
            return {"created": False, "event": existing, "state": _reduce(events)}
        if source == "morning_review":
            same_item = [
                item
                for item in events
                if item.get("event_kind") == "outcome"
                and item.get("source") == "morning_review"
                and item.get("session_id") == session_id_value
                and item.get("item_id") == item_id_value
            ]
            if same_item:
                raise ReviewLoopError(
                    "morning item already has a canonical outcome: "
                    f"{same_item[0].get('event_id')}"
                )
            if not _from_session_sync:
                _assert_direct_morning_write_allowed(
                    repo, session_id_value, item_id_value
                )
        prior = _reduce(events)
        identity_probe = review_unit or formal_node or source_id_value
        # An original-question item carries both its review unit and its formal
        # node id; the action it consumes is keyed on the formal node. Accept any
        # identity the item legitimately owns instead of a single ordered probe.
        identity_candidates = {
            value for value in (review_unit, formal_node, source_id_value) if value
        }
        trigger = _action_by_id(prior, trigger_action)
        if trigger_action and trigger is None:
            raise ReviewLoopError(f"unknown trigger_action_id: {trigger_action}")
        if trigger_action and trigger is not None:
            if trigger.get("identity") not in identity_candidates:
                raise ReviewLoopError("explicit trigger action identity mismatch")
            identity_probe = trigger.get("identity")
            if trigger.get("action_type") not in {"review", "original_question"}:
                raise ReviewLoopError("explicit trigger action is not eligible")
            if trigger.get("status") != "pending":
                if not _from_session_sync:
                    raise ReviewLoopError("explicit trigger action is not eligible")
                # An immutable morning queue duplicated one action across two
                # slots; a sibling item already consumed it. Keep the evidence,
                # drop the explicit binding, and fall back to normal selection.
                trigger = None
            elif _parse_date(trigger["due_date"], "action due_date") > day:
                raise ReviewLoopError("explicit trigger action is not due")
        if trigger is None:
            eligible_triggers = [
                item
                for item in prior["pending_actions"]
                if item.get("identity") == identity_probe
                and item.get("action_type") in {"review", "original_question"}
                and _parse_date(item["due_date"], "action due_date") <= day
            ]
            if eligible_triggers:
                trigger = sorted(
                    eligible_triggers,
                    key=lambda item: (item["due_date"], item["action_id"]),
                )[0]
        effective_trigger_id = str(trigger["action_id"]) if trigger else None
        derived_cross_day = cross_day
        if derived_cross_day is None and trigger:
            derived_cross_day = _parse_date(trigger["origin_date"], "origin_date") < day
        if derived_cross_day is None:
            derived_cross_day = _prior_cross_day(prior, identity_probe, day)
        hint_used = prompt_level != "none"
        fragile = (
            fragile_override
            or confidence == "low"
            or latency_state == "abnormal"
            or (source == "morning_review" and derived_cross_day is not True)
        )
        reasoning_break = bool(
            reasoning_result in {"partial", "diverged"}
            or break_value.lower() not in NONE_BREAKS
        )
        ordinary_no_schedule = ordinary_independent_correct_needs_no_schedule(
            source=source,
            first_result=first_result,
            confidence=confidence,
            prompt_level=prompt_level,
            reasoning_break=reasoning_break,
            correction_result=correction_result,
            fragile=bool(
                fragile_override
                or confidence == "low"
                or latency_state == "abnormal"
            ),
        )
        stable_retention = (
            source == "morning_review"
            and derived_cross_day is True
            and first_result == "independent_correct"
            and not hint_used
            and confidence in {"medium", "high"}
            and latency_state != "abnormal"
            and not fragile_override
        )
        if stable_retention:
            evidence_kind = "retention"
        elif first_result == "independent_correct" and (hint_used or correction_result != "not_attempted"):
            evidence_kind = "repair"
        elif first_result == "independent_correct" and fragile:
            evidence_kind = "fragile"
        elif first_result == "independent_correct":
            evidence_kind = "practice"
        else:
            evidence_kind = "failure"

        organic_baseline = bool(question_valid and not hint_used and identity_probe not in {
            item["identity"] for item in prior["organic_baselines"]
        })
        event_id = content_id("RE", key)
        mapped = (
            []
            if practice_mode == "variant"
            else mapped_nodes(repo, review_unit, source_id_value)
        )
        if formal_node and formal_node not in mapped:
            mapped = sorted({*mapped, formal_node})
        route = _route_candidate(
            event_id=event_id,
            first_result=first_result,
            first_break=break_value,
            mechanism_key=mechanism_value,
            mapped=mapped,
            formal_node_id=formal_node,
            review_unit_id=review_unit,
        )
        if deferred_binding is not None:
            # This exact Capture is already inside the authorized formal batch.
            # Keep the real outcome and schedule, but do not emit a second
            # wrong-intake candidate for the same episode.
            route = {
                "kind": "deferred_formal_enhancement",
                "formal_write_authorized": False,
            }

        generated: list[dict[str, Any]] = []
        if ordinary_no_schedule:
            next_stage = None
            due = None
            origin = None
            reason = None
        elif stable_retention:
            current_stage = trigger.get("stage") if trigger else None
            count = int(prior["retention"].get(identity_probe, {}).get("count", 0))
            next_stage = _next_stage(current_stage, count)
            origin = _parse_date(trigger["origin_date"], "origin_date") if trigger else day
            due = origin + dt.timedelta(days=STAGE_DAYS[next_stage])
            if due <= day:
                due = day + dt.timedelta(days=max(1, STAGE_DAYS[next_stage]))
            reason = "cross-day unhinted independent retrieval retained"
        else:
            next_stage = "D1" if source in {"daily_practice", "evening_d0"} else "D1_rescue"
            due = day + dt.timedelta(days=1)
            origin = day if trigger is None else _parse_date(trigger["origin_date"], "origin_date")
            if first_result == "independent_correct" and fragile:
                reason = "fragile retrieval needs a short unhinted retest"
            elif first_result == "independent_correct":
                reason = "same-day practice requires cross-day verification"
            else:
                reason = "unstable first response requires a short retry"
        if not ordinary_no_schedule:
            assert next_stage is not None and due is not None and origin is not None
            assert reason is not None
            generated.append(
                _make_action(
                    action_type="review",
                    identity=identity_probe,
                    source_id=source_id_value,
                    due_date=due,
                    stage=next_stage,
                    trigger_event_id=event_id,
                    origin_date=origin,
                    reason=reason,
                    first_break=break_value,
                    formal_node_id=formal_node,
                    review_unit_id=review_unit,
                )
            )
        if practice_mode != "variant" and not ordinary_no_schedule:
            generated.extend(
                _original_actions(
                    repo,
                    event_id=event_id,
                    observed_date=day,
                    review_unit_id=review_unit,
                    source_id=source_id_value,
                    first_result=first_result,
                    hint_used=hint_used,
                    question_valid=bool(question_valid),
                    fragile=(
                        fragile_override
                        or confidence == "low"
                        or latency_state == "abnormal"
                    ),
                    current_formal_node_id=formal_node,
                )
            )
        event = {
            "schema": LEDGER_SCHEMA,
            "event_kind": "outcome",
            "event_id": event_id,
            "idempotency_key": key,
            "request_hash": request_hash,
            "event_time": event_time,
            "observed_date": day.isoformat(),
            "source": source,
            "session_id": session_id_value,
            "item_id": item_id_value,
            "source_id": source_id_value,
            "presentation_kind": presentation,
            "mechanism_key": mechanism_value,
            "knowledge_point": knowledge_value or None,
            "formal_node_id": formal_node,
            "mapped_formal_node_ids": mapped,
            "review_unit_id": review_unit,
            "first_result": first_result,
            "choice_result": choice_result,
            "reasoning_result": reasoning_result,
            "confidence": confidence,
            "hint_used": hint_used,
            "prompt_level": prompt_level,
            "first_break": break_value,
            "first_break_provenance": first_break_provenance,
            "correction_result": correction_result,
            "response_seconds": response_seconds,
            "latency_state": latency_state,
            "cross_day": derived_cross_day,
            "question_valid": bool(question_valid),
            "organic_baseline": organic_baseline,
            "evidence_kind": evidence_kind,
            "stable_retention": stable_retention,
            "fragile_mastery": fragile,
            "trigger_action_id": effective_trigger_id,
            "generated_actions": generated,
            "route": route,
            "legacy_import": bool(legacy_import),
            "formal_write_authorized": False,
        }
        if deferred_binding is not None:
            event["deferred_morning_binding"] = deferred_binding
        if ordinary_no_schedule:
            event["scheduling_mode"] = "ordinary_no_schedule"
        if session_day is not None:
            event["session_review_date"] = session_day.isoformat()
        if followup_payload is not None:
            event.update(followup_payload)
            event["actual_question_formal_node_id"] = (
                formal_node if practice_mode == "original" else None
            )
        append_jsonl(_loop_root(repo) / "events.jsonl", event)
        events.append(event)
        state = _reduce(events)
        atomic_write_json(_loop_root(repo) / "state.json", state)
    runtime = reconcile_runtime_views(repo)
    return {"created": True, "event": event, "state": state, "runtime": runtime}


def build_repair_event(
    *,
    source: str = "morning_review",
    session_id: str,
    item_id: str,
    source_id: str,
    result: str,
    confidence: str | None,
    prompt_level: str | None,
    event_time: str,
    observed_date: str,
    idempotency_key: str,
    attempt: int,
    session_review_date: str | None = None,
    response_seconds: float | None = None,
    legacy_import: bool = False,
) -> dict[str, Any]:
    """Validate repair evidence and construct its canonical ledger event."""

    if source not in SOURCES:
        raise ReviewLoopError(f"invalid source: {source}")
    if result not in CORRECTION_RESULTS - {"not_attempted"}:
        raise ReviewLoopError("invalid repair result")
    if result != "relearn_required":
        if confidence not in CONFIDENCE_LEVELS or prompt_level not in PROMPT_LEVELS:
            raise ReviewLoopError("repair requires confidence and prompt level")
    session = _safe_id(session_id, "session_id", required=True)
    item = _safe_id(item_id, "item_id", required=True)
    source_value = _safe_text(source_id, "source_id", required=True, limit=512)
    key = _safe_id(idempotency_key, "idempotency_key", required=True)
    parsed_event_time = _parse_datetime(event_time)
    requested_day = _parse_date(observed_date, "observed_date")
    local_day = parsed_event_time.astimezone(LOCAL_TZ).date()
    session_day: dt.date | None = None
    if source == "morning_review":
        session_day = _parse_date(
            session_review_date or requested_day.isoformat(),
            "session_review_date",
        )
        day = local_day
    else:
        if session_review_date is not None:
            raise ReviewLoopError(
                "session_review_date is only valid for morning_review"
            )
        day = requested_day
    if (
        source in {"daily_practice", "evening_d0"}
        and not legacy_import
        and local_day != day
    ):
        raise ReviewLoopError(
            "event_time Asia/Shanghai date must equal observed_date"
        )
    if attempt < 1:
        raise ReviewLoopError("repair attempt must be positive")
    if response_seconds is not None and not 0 < response_seconds <= 7200:
        raise ReviewLoopError("response_seconds must be > 0 and <= 7200")
    assert session and item and source_value and key
    request = {
        "session_id": session,
        "item_id": item,
        "source_id": source_value,
        "result": result,
        "confidence": confidence,
        "prompt_level": prompt_level,
        "event_time": event_time,
        "observed_date": day.isoformat(),
        "attempt": attempt,
        "response_seconds": response_seconds,
        "legacy_import": bool(legacy_import),
        "source": source,
    }
    if session_day is not None:
        request["session_review_date"] = session_day.isoformat()
    request_hash = hashlib.sha256(canonical_json(request).encode("utf-8")).hexdigest()
    event = {
        "schema": LEDGER_SCHEMA,
        "event_kind": "repair",
        "event_id": content_id("RE", key),
        "idempotency_key": key,
        "request_hash": request_hash,
        "event_time": event_time,
        "observed_date": day.isoformat(),
        "source": source,
        "session_id": session,
        "item_id": item,
        "source_id": source_value,
        "correction_result": result,
        "confidence": confidence,
        "hint_used": (prompt_level or "none") != "none",
        "prompt_level": prompt_level,
        "response_seconds": response_seconds,
        "attempt": attempt,
        "organic_baseline": False,
        "evidence_kind": "repair",
        "stable_retention": False,
        "generated_actions": [],
        "route": {"kind": "repair_only", "formal_write_authorized": False},
        "legacy_import": bool(legacy_import),
        "formal_write_authorized": False,
    }
    if session_day is not None:
        event["session_review_date"] = session_day.isoformat()
    return event


@study_commit_boundary
def record_repair(
    repo: Path,
    *,
    source: str = "morning_review",
    session_id: str,
    item_id: str,
    source_id: str,
    result: str,
    confidence: str | None,
    prompt_level: str | None,
    event_time: str,
    observed_date: str,
    idempotency_key: str,
    attempt: int,
    session_review_date: str | None = None,
    response_seconds: float | None = None,
    legacy_import: bool = False,
) -> dict[str, Any]:
    repo = _resolve_repo(repo)
    event = build_repair_event(
        source=source,
        session_id=session_id,
        item_id=item_id,
        source_id=source_id,
        result=result,
        confidence=confidence,
        prompt_level=prompt_level,
        event_time=event_time,
        observed_date=observed_date,
        idempotency_key=idempotency_key,
        attempt=attempt,
        session_review_date=session_review_date,
        response_seconds=response_seconds,
        legacy_import=legacy_import,
    )
    key = str(event["idempotency_key"])
    request_hash = str(event["request_hash"])
    with runtime_lock(repo):
        events = _read_events(repo)
        existing = _event_by_key(events, key)
        if existing:
            compatible = existing.get("request_hash") == request_hash
            if (
                not compatible
                and source == "morning_review"
                and "session_review_date" not in existing
                and session_review_date is not None
                and existing.get("observed_date") == session_review_date
                and _event_local_date(str(existing.get("event_time") or ""))
                == _parse_date(str(event["observed_date"]), "observed_date")
            ):
                historical = {
                    "session_id": event["session_id"],
                    "item_id": event["item_id"],
                    "source_id": event["source_id"],
                    "result": event["correction_result"],
                    "confidence": event["confidence"],
                    "prompt_level": event["prompt_level"],
                    "event_time": event["event_time"],
                    "observed_date": session_review_date,
                    "attempt": event["attempt"],
                    "response_seconds": event["response_seconds"],
                    "legacy_import": event["legacy_import"],
                    "source": event["source"],
                }
                historical_hash = hashlib.sha256(
                    canonical_json(historical).encode("utf-8")
                ).hexdigest()
                compatible = existing.get("request_hash") == historical_hash
            if not compatible:
                raise ReviewLoopError(
                    "idempotency key was reused with different repair evidence"
                )
            return {"created": False, "event": existing, "state": _reduce(events)}
        append_jsonl(_loop_root(repo) / "events.jsonl", event)
        events.append(event)
        state = _reduce(events)
        atomic_write_json(_loop_root(repo) / "state.json", state)
    runtime = reconcile_runtime_views(repo)
    return {"created": True, "event": event, "state": state, "runtime": runtime}


def organic_baseline_units(repo: Path) -> set[str]:
    if not (_loop_root(repo) / "events.jsonl").exists():
        return set()
    return {
        str(item["review_unit_id"])
        for item in load_state(repo)["organic_baselines"]
        if item.get("review_unit_id")
    }


def load_due_actions(repo: Path, today: dt.date) -> list[LoopAction]:
    if not (_loop_root(repo) / "events.jsonl").exists():
        return []
    scheduling_report = scheduling_drift_audit(repo)
    answered_pending_action_ids = {
        str(row["action_id"])
        for row in scheduling_report.get("gaps") or []
        if row.get("action_status") == "pending"
        and row.get("status") != "reconciled"
    }
    result: list[LoopAction] = []
    for action in load_state(repo)["pending_actions"]:
        if str(action.get("action_id")) in answered_pending_action_ids:
            # Cold queue generation may inspect session projections.  An action
            # that already has a learner outcome must be reconciled or blocked,
            # never silently selected as if it were unanswered.
            continue
        due = _parse_date(action["due_date"], "action due_date")
        if due > today:
            continue
        source_id = str(action["source_id"])
        stage = str(action["stage"])
        item_kind = "original_question" if action["action_type"] == "original_question" else "derived_d0"
        break_label = str(action.get("first_break") or "").strip()
        review_label = (
            break_label
            if break_label.lower() not in NONE_BREAKS
            else source_id
        )
        title = (
            f"原题轨 {source_id}"
            if item_kind == "original_question"
            else f"{stage} 安全复测 · {review_label}"
        )
        prompt = (
            "打开原题但不要展开答案或解析，独立完成判型、第一动作与关键步骤；"
            "只记录首个断点和结构化结果。"
            if item_kind == "original_question"
            else "不看资料，先说第一动作，再解释关键机制与边界；若仍卡住，只记录首个断点。"
        )
        result.append(
            LoopAction(
                key=str(action["action_id"]),
                title=title,
                prompt=prompt,
                feedback="反馈由当前安全知识源在首答落账后提供，不写入统一账本。",
                source=str(_loop_root(repo) / "state.json"),
                item_kind=item_kind,
                source_id=source_id,
                action_id=str(action["action_id"]),
                trigger_event_id=str(action["trigger_event_id"]),
                stage=stage,
                due_date=str(action["due_date"]),
                formal_node_id=action.get("formal_node_id"),
                review_unit_id=action.get("review_unit_id"),
                origin_kind=(
                    "formal_intake_backflow"
                    if action.get("origin_kind") == "formal_intake_backflow"
                    or "formal_intake_backflow" in action.get("origin_kinds", [])
                    else None
                ),
            )
        )
    return sorted(
        result,
        key=lambda item: (item.item_kind == "original_question", item.due_date, item.key),
    )


def _session_states(repo: Path) -> tuple[list[dict[str, Any]], list[str]]:
    root = repo / MORNING_REL
    if not root.is_dir():
        return [], []
    states: list[dict[str, Any]] = []
    errors: list[str] = []
    for directory in sorted(path for path in root.iterdir() if path.is_dir()):
        path = directory / "state.json"
        try:
            state = json.loads(path.read_text(encoding="utf-8"))
        except (FileNotFoundError, json.JSONDecodeError) as exc:
            errors.append(f"{directory.name}: unreadable state ({exc})")
            continue
        if state.get("schema") != "morning_review_session_state_v1":
            errors.append(f"{directory.name}: invalid state schema")
            continue
        states.append(state)
    return states, errors


def _assert_direct_morning_write_allowed(
    repo: Path, session_id: str, item_id: str
) -> None:
    states, errors = _session_states(repo)
    if errors:
        raise ReviewLoopError(
            "direct morning outcome refused because session state is invalid: "
            + "; ".join(errors)
        )
    active = [state for state in states if state.get("status") in ACTIVE_SESSION_STATES]
    if not active:
        return
    active_ids = ", ".join(str(state.get("session_id")) for state in active)
    matching = next(
        (state for state in active if state.get("session_id") == session_id), None
    )
    if matching is not None and item_id not in (matching.get("items") or {}):
        raise ReviewLoopError(
            f"active morning session {session_id} does not contain item {item_id}"
        )
    raise ReviewLoopError(
        "an active morning session must persist first answers through "
        "scripts/morning_review_session.py record-first; "
        f"active session(s): {active_ids}"
    )


def assert_can_start_session(repo: Path, session_id: str) -> None:
    states, errors = _session_states(repo)
    if errors:
        raise ReviewLoopError("cannot prove single-active state: " + "; ".join(errors))
    active = [state for state in states if state.get("status") in ACTIVE_SESSION_STATES]
    if active:
        ids = ", ".join(str(state.get("session_id")) for state in active)
        raise ReviewLoopError(f"another morning session is active: {ids}")
    if any(state.get("session_id") == session_id for state in states):
        raise ReviewLoopError(f"session already exists: {session_id}")


def _parse_frontmatter(text: str) -> dict[str, str]:
    if not text.startswith("---\n"):
        return {}
    end = text.find("\n---\n", 4)
    if end < 0:
        return {}
    result: dict[str, str] = {}
    for line in text[4:end].splitlines():
        if ":" in line:
            key, value = line.split(":", 1)
            result[key.strip()] = value.strip()
    return result


def _runtime_projection(repo: Path, queue_hint: Path | None = None) -> dict[str, Any]:
    states, errors = _session_states(repo)
    active = [state for state in states if state.get("status") in ACTIVE_SESSION_STATES]
    if len(active) > 1:
        errors.append("multiple active sessions: " + ", ".join(str(item.get("session_id")) for item in active))
    existing_path = _loop_root(repo) / "runtime.json"
    try:
        existing = json.loads(existing_path.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError):
        existing = {}
    selected: dict[str, Any] | None = None
    if active:
        selected = sorted(active, key=lambda item: str(item.get("updated_at", "")))[-1]
        status = str(selected["status"])
        queue_rel = str(selected.get("queue_path", ""))
        review_date = str(selected.get("review_date", "undated"))
        session_id = str(selected.get("session_id"))
    elif queue_hint is not None:
        queue_rel = str(queue_hint.resolve().relative_to(repo))
        frontmatter = _parse_frontmatter(queue_hint.read_text(encoding="utf-8"))
        review_date = frontmatter.get("review_date", "undated")
        status = "ready"
        session_id = None
    elif states:
        selected = sorted(states, key=lambda item: str(item.get("updated_at", "")))[-1]
        status = str(selected.get("status", "closed"))
        queue_rel = str(selected.get("queue_path", ""))
        review_date = str(selected.get("review_date", "undated"))
        session_id = str(selected.get("session_id")) if status in ACTIVE_SESSION_STATES else None
    else:
        existing_queue = existing.get("queue_path")
        queue_rel = str(existing_queue) if existing_queue else ""
        review_date = str(existing.get("review_date") or "undated")
        status = "ready" if queue_rel else "idle"
        session_id = None
    current_item_id = None
    current_action_id = None
    current_stage = None
    if selected is not None and status in ACTIVE_SESSION_STATES:
        current_item_id = next(
            (
                item_id
                for item_id in selected.get("item_order") or []
                if isinstance((selected.get("items") or {}).get(item_id), dict)
                and (selected["items"][item_id]).get("first") is None
                and (selected["items"][item_id]).get("resolution")
                not in {
                    "dismissed",
                    "scheduling_superseded",
                    "administratively_retired",
                }
            ),
            None,
        )
        current_item = (
            (selected.get("items") or {}).get(current_item_id)
            if current_item_id is not None
            else None
        )
        if isinstance(current_item, dict):
            current_action_id = current_item.get("action_id")
            current_stage = current_item.get("stage")
    queue_path = repo / queue_rel if queue_rel else None
    queue_schema = "missing"
    queue_sha256 = None
    if queue_path and queue_path.is_file():
        frontmatter = _parse_frontmatter(queue_path.read_text(encoding="utf-8"))
        queue_schema = frontmatter.get("schema", "missing")
        queue_sha256 = hashlib.sha256(queue_path.read_bytes()).hexdigest()
    loop_state = load_state(repo)
    scheduling_report = scheduling_drift_audit(repo)
    scheduling_gaps = [
        {
            "action_id": row.get("action_id"),
            "stage": row.get("stage"),
            "due_date": row.get("due_date"),
            "status": row.get("status"),
            "authoritative_outcome_event_id": row.get(
                "authoritative_outcome_event_id"
            ),
        }
        for row in scheduling_report.get("gaps") or []
        if row.get("action_status") == "pending"
        and row.get("status") != "reconciled"
    ]
    recent_scheduling = []
    for event in (loop_state.get("scheduling_reconciliations") or [])[-50:]:
        generated = list(event.get("generated_actions") or [])
        next_action = generated[0] if generated else {}
        recent_scheduling.append(
            {
                "action_id": event.get("trigger_action_id"),
                "status": "completed",
                "authoritative_outcome_event_id": event.get(
                    "authoritative_outcome_event_id"
                ),
                "superseded_outcome_event_ids": event.get(
                    "superseded_outcome_event_ids"
                )
                or [],
                "next_action_id": next_action.get("action_id"),
                "next_stage": next_action.get("stage"),
                "next_due_date": next_action.get("due_date"),
                "reconciliation_event_id": event.get("event_id"),
            }
        )
    session_scheduling_superseded = [
        {
            "session_id": session.get("session_id"),
            "item_id": item_id,
            "action_id": item.get("action_id"),
            "status": "superseded",
            "authoritative_outcome_event_id": (
                item.get("scheduling_disposition") or {}
            ).get("authoritative_outcome_event_id"),
            "reconciliation_event_id": (
                item.get("scheduling_disposition") or {}
            ).get("reconciliation_event_id"),
        }
        for session in states
        for item_id, item in (session.get("items") or {}).items()
        if isinstance(item, dict)
        and item.get("resolution") == "scheduling_superseded"
    ]
    return {
        "schema": RUNTIME_SCHEMA,
        "status": status,
        "review_date": review_date,
        "queue_path": queue_rel or None,
        "queue_schema": queue_schema,
        "queue_sha256": queue_sha256,
        "active_session_id": session_id,
        "current_item_id": current_item_id,
        "current_action_id": current_action_id,
        "current_stage": current_stage,
        "active_session_count": len(active),
        "session_count": len(states),
        "pending_action_count": len(loop_state["pending_actions"]),
        "pending_candidate_count": sum(
            item.get("status") in {"pending_user_confirmation", "needs_user"}
            for item in loop_state["candidates"]
        ),
        "event_count": loop_state["event_count"],
        "scheduling_gap_count": len(scheduling_gaps),
        "scheduling_gaps": scheduling_gaps,
        "scheduling_reconciliation_count": loop_state.get(
            "scheduling_reconciliation_count", 0
        ),
        "recent_scheduling": recent_scheduling,
        "session_scheduling_superseded_count": len(
            session_scheduling_superseded
        ),
        "session_scheduling_superseded": session_scheduling_superseded,
        "errors": errors,
    }


def _pointer_text(runtime: dict[str, Any]) -> str:
    queue_path = runtime.get("queue_path")
    queue_link = Path(str(queue_path)).stem if queue_path else ""
    lines = [
        "---",
        f"schema: {POINTER_SCHEMA}",
        f"review_date: {runtime['review_date']}",
        f"status: {runtime['status']}",
        f"queue_schema: {runtime['queue_schema']}",
        f"active_session_id: {runtime['active_session_id'] or 'none'}",
        "runtime_source: wiki/study_vaults/408-full/state/review-loop/runtime.json",
        "---",
        "",
        "# 当前晨间复盘",
        "",
    ]
    if queue_link:
        lines.append(f"- 当前行动队列：[[{queue_link}|{runtime['review_date']} 408 晨间行动队列]]")
    else:
        lines.append("- 当前行动队列：无")
    lines += [
        f"- 运行状态：{runtime['status']}",
        f"- 活跃 session：{runtime['active_session_id'] or '无'}",
        f"- 当前有效 item：{runtime['current_item_id'] or '无'}",
        f"- 当前 action：{runtime['current_action_id'] or '无'}",
        f"- 待执行后续行动：{runtime['pending_action_count']}",
        f"- 待确认候选：{runtime['pending_candidate_count']}",
        f"- 已作答但调度待恢复：{runtime['scheduling_gap_count']}",
        f"- 调度恢复事件：{runtime['scheduling_reconciliation_count']}",
        f"- Session 中已跳过的重复 action：{runtime['session_scheduling_superseded_count']}",
        "- 稳定入口：[[晨间复盘中心]]",
        "",
        "> 本指针是统一复盘状态的投影；正式错题事实、关系边、掌握度和正式日期均未自动修改。",
        "",
    ]
    return "\n".join(lines)


def _generated_pointer(repo: Path) -> Path:
    pointer = repo / DASHBOARD_REL / "当前晨间复盘.md"
    if pointer.exists():
        text = pointer.read_text(encoding="utf-8")
        if (
            "schema: morning_review_pointer_v1" not in text
            and f"schema: {POINTER_SCHEMA}" not in text
        ):
            raise ReviewLoopError(f"refusing to overwrite non-generated pointer: {pointer}")
    return pointer


def _restore_text_snapshots(snapshots: dict[Path, str | None]) -> list[str]:
    errors: list[str] = []
    for path, original in reversed(tuple(snapshots.items())):
        try:
            if original is None:
                if path.exists():
                    path.unlink()
                    _fsync_dir(path.parent)
            else:
                atomic_write(path, original)
        except OSError as exc:
            errors.append(f"{path}: {exc}")
    return errors


def _write_runtime_views_locked(repo: Path, queue_hint: Path | None = None) -> dict[str, Any]:
    runtime = _runtime_projection(repo, queue_hint)
    dashboard = dict(runtime)
    dashboard["schema"] = "review_loop_dashboard_v1"
    runtime_path = _loop_root(repo) / "runtime.json"
    dashboard_path = _loop_root(repo) / "dashboard.json"
    pointer = _generated_pointer(repo)
    snapshots = {
        path: path.read_text(encoding="utf-8") if path.exists() else None
        for path in (runtime_path, dashboard_path, pointer)
    }
    try:
        atomic_write_json(runtime_path, runtime)
        atomic_write_json(dashboard_path, dashboard)
        atomic_write(pointer, _pointer_text(runtime))
    except BaseException as exc:
        rollback_errors = _restore_text_snapshots(snapshots)
        if rollback_errors:
            raise ReviewLoopError(
                "runtime projection failed and rollback was incomplete: "
                + "; ".join(rollback_errors)
            ) from exc
        raise
    return runtime


def reconcile_runtime_views(repo: Path, queue_hint: Path | None = None) -> dict[str, Any]:
    repo = _resolve_repo(repo)
    with runtime_lock(repo):
        return _write_runtime_views_locked(repo, queue_hint)


def _assert_queue_publication_allowed(repo: Path) -> None:
    states, errors = _session_states(repo)
    if errors:
        raise ReviewLoopError("cannot publish queue while session state is invalid")
    active = [state for state in states if state.get("status") in ACTIVE_SESSION_STATES]
    if active:
        raise ReviewLoopError(
            "cannot replace the current queue while a session is active: "
            + ", ".join(str(item.get("session_id")) for item in active)
        )


def register_ready_queue(repo: Path, queue_path: Path) -> dict[str, Any]:
    repo = _resolve_repo(repo)
    queue = queue_path.resolve()
    queue.relative_to(repo)
    with runtime_lock(repo):
        _assert_queue_publication_allowed(repo)
        return _write_runtime_views_locked(repo, queue)


def publish_ready_queue(repo: Path, queue_path: Path, content: str) -> dict[str, Any]:
    """Publish a queue and its projections under one runtime lock.

    Existing identical queues are preserved. A failed publication removes only a
    queue created by this call, while runtime/dashboard/pointer rollback happens in
    ``_write_runtime_views_locked``.
    """

    repo = _resolve_repo(repo)
    queue = queue_path.resolve()
    queue.relative_to(repo)
    with runtime_lock(repo):
        _assert_queue_publication_allowed(repo)
        _generated_pointer(repo)
        created = False
        if queue.exists():
            if queue.read_text(encoding="utf-8") != content:
                raise ReviewLoopError(f"refusing to overwrite an existing action queue: {queue}")
        else:
            atomic_write(queue, content)
            created = True
        try:
            return _write_runtime_views_locked(repo, queue)
        except BaseException as exc:
            if created:
                try:
                    queue.unlink()
                    _fsync_dir(queue.parent)
                except OSError as cleanup_exc:
                    raise ReviewLoopError(
                        f"queue publication failed and cleanup was incomplete: {cleanup_exc}"
                    ) from exc
            raise


def sync_session(
    repo: Path, session_id: str, *, item_id: str | None = None
) -> dict[str, Any]:
    repo = _resolve_repo(repo)
    session = _safe_id(session_id, "session_id", required=True)
    target_item = _safe_id(item_id, "item_id")
    assert session
    directory = repo / MORNING_REL / session
    ledger = directory / "events.jsonl"
    if not ledger.is_file():
        raise ReviewLoopError(f"missing morning session ledger: {session}")
    raw_events: list[dict[str, Any]] = []
    for line_no, raw in enumerate(ledger.read_text(encoding="utf-8").splitlines(), start=1):
        try:
            raw_events.append(json.loads(raw))
        except json.JSONDecodeError as exc:
            raise ReviewLoopError(f"morning session is corrupt at line {line_no}") from exc
    if not raw_events or raw_events[0].get("event_type") != "session_started":
        raise ReviewLoopError("morning session lacks session_started")
    started = raw_events[0]
    payload = started.get("payload", {})
    review_date = str(payload.get("review_date", ""))
    _parse_date(review_date, "session review_date")
    items = {item["item_id"]: item for item in payload.get("items", [])}
    for raw in raw_events[1:]:
        if raw.get("event_type") != "followup_scheduled":
            continue
        row = raw.get("payload") or {}
        dynamic_item_id = str(row.get("item_id") or "")
        practice_mode = str(row.get("practice_mode") or "")
        expected_kind = (
            "related_original" if practice_mode == "original" else "grounded_variant"
        )
        required_refs = (
            "scheduling_key",
            "trigger_event_id",
            "context_slice_id",
            "candidate_id",
            "source_id",
            "source_ref",
            "source_sha256",
            "selection_receipt_ref",
            "selection_receipt_sha256",
            "mechanism_key",
            "knowledge_point",
        )
        if (
            not re.fullmatch(r"XF-[0-9a-f]{20}", dynamic_item_id)
            or practice_mode not in FOLLOWUP_PRACTICE_MODES
            or row.get("item_kind") != expected_kind
            or any(not str(row.get(field) or "") for field in required_refs)
            or dynamic_item_id in items
        ):
            raise ReviewLoopError("morning session contains an invalid linked-practice item")
        dynamic = dict(row)
        dynamic["followup_event_id"] = str(raw.get("event_id") or "")
        if not dynamic["followup_event_id"]:
            raise ReviewLoopError("linked-practice event lacks event_id")
        items[dynamic_item_id] = dynamic
    if target_item and target_item not in items:
        raise ReviewLoopError(f"morning session does not contain item: {target_item}")
    first_break_repairs: dict[str, dict[str, Any]] = {}
    for raw in raw_events[1:]:
        if raw.get("event_type") != "unbound_first_repaired":
            continue
        row = raw.get("payload", {})
        item_id = str(row.get("item_id", ""))
        if item_id in items:
            first_break_repairs[item_id] = row
    repair_counts: dict[str, int] = {}
    outcome_event_ids: dict[str, str] = {}
    created = 0
    replayed = 0
    for raw in raw_events[1:]:
        kind = raw.get("event_type")
        row = raw.get("payload", {})
        item_id = str(row.get("item_id", ""))
        item = items.get(item_id)
        if not item:
            continue
        if target_item and item_id != target_item:
            continue
        source_id = str(item.get("source_id", ""))
        if kind == "first_recorded":
            effective_row = dict(row)
            repair = first_break_repairs.get(item_id)
            if repair:
                effective_row["first_break"] = repair.get("first_break")
                effective_row["first_break_provenance"] = repair.get(
                    "first_break_provenance"
                )
                if repair.get("normalized_result"):
                    effective_row["result"] = repair["normalized_result"]
            row = effective_row
            old_result = str(row.get("result", ""))
            result, fragile = normalize_morning_first_classification(
                old_result,
                choice_result=str(row.get("choice_result") or "not_applicable"),
                reasoning_result=str(
                    row.get("reasoning_result") or "not_observed"
                ),
                prompt_level=str(row.get("prompt_level") or ""),
            )
            response = row.get("response_seconds")
            response_value = float(response) if isinstance(response, (int, float)) and response > 0 else None
            latency = "normal" if response_value is not None else "unmeasured"
            first_break = str(row.get("first_break"))
            provenance = normalize_first_break_provenance(
                row.get("first_break_provenance"), first_break
            )
            practice_mode = item.get("practice_mode")
            linked_kwargs: dict[str, Any] = {}
            if practice_mode:
                try:
                    verified_source = validate_source_for_practice(
                        repo,
                        item.get("source_ref"),
                        item.get("source_sha256"),
                        practice_mode=str(practice_mode),
                        formal_node_id=item.get("formal_node_id"),
                        variant_of_formal_node_id=item.get(
                            "variant_of_formal_node_id"
                        ),
                    )
                except LinkedPracticeSourceError as exc:
                    raise ReviewLoopError(str(exc)) from exc
                if verified_source["source_ref"] != item.get("source_ref"):
                    raise ReviewLoopError("linked source locator is not canonical")
                try:
                    verified_selection = validate_selection_receipt(
                        repo,
                        item.get("selection_receipt_ref"),
                        item.get("selection_receipt_sha256"),
                        expected={
                            "slice_id": item.get("context_slice_id"),
                            "trigger_event_id": item.get("trigger_event_id"),
                            "candidate_id": item.get("candidate_id"),
                            "practice_mode": item.get("practice_mode"),
                            "formal_node_id": item.get("formal_node_id"),
                            "anchor_formal_node_id": item.get(
                                "anchor_formal_node_id"
                            ),
                            "source_id": item.get("source_id"),
                            "source_ref": item.get("source_ref"),
                            "source_sha256": item.get("source_sha256"),
                            "mechanism_key": item.get("mechanism_key"),
                            "knowledge_point": item.get("knowledge_point"),
                            "review_unit_id": item.get("review_unit_id"),
                        },
                        source_path=verified_source["path"],
                    )
                except LinkedPracticeSourceError as exc:
                    raise ReviewLoopError(str(exc)) from exc
                if (
                    verified_selection["selection_receipt_ref"]
                    != item.get("selection_receipt_ref")
                ):
                    raise ReviewLoopError(
                        "selection receipt locator is not canonical"
                    )
                linked_kwargs = {
                    "practice_mode": str(practice_mode),
                    "anchor_formal_node_id": item.get(
                        "anchor_formal_node_id"
                    ),
                    "variant_of_formal_node_id": item.get(
                        "variant_of_formal_node_id"
                    ),
                    "followup_event_id": item.get("followup_event_id"),
                    "parent_trigger_event_id": item.get("trigger_event_id"),
                    "context_slice_id": item.get("context_slice_id"),
                    "candidate_id": item.get("candidate_id"),
                    "source_ref": item.get("source_ref"),
                    "source_sha256": item.get("source_sha256"),
                    "selection_receipt_ref": item.get(
                        "selection_receipt_ref"
                    ),
                    "selection_receipt_sha256": item.get(
                        "selection_receipt_sha256"
                    ),
                }
            outcome = record_outcome(
                repo,
                source="morning_review",
                session_id=session,
                item_id=item_id,
                source_id=source_id,
                mechanism_key=str(item.get("mechanism_key") or source_id),
                knowledge_point=str(item.get("knowledge_point") or ""),
                first_result=result,
                choice_result=str(row.get("choice_result") or "not_applicable"),
                reasoning_result=str(
                    row.get("reasoning_result") or "not_observed"
                ),
                confidence=str(row.get("confidence")),
                prompt_level=str(row.get("prompt_level")),
                first_break=first_break,
                first_break_provenance=provenance,
                event_time=str(raw.get("timestamp")),
                observed_date=_event_local_date(
                    str(raw.get("timestamp"))
                ).isoformat(),
                session_review_date=review_date,
                idempotency_key=f"morning:{session}:{item_id}:first",
                response_seconds=response_value,
                latency_state=latency,
                cross_day=False if practice_mode else None,
                question_valid=True,
                formal_node_id=item.get("formal_node_id"),
                review_unit_id=item.get("review_unit_id") or (source_id if REVIEW_UNIT_RE.fullmatch(source_id) else None),
                trigger_action_id=item.get("action_id"),
                legacy_import=payload.get("queue_schema") not in {"morning_review_action_queue_v3"},
                fragile_override=fragile,
                presentation_kind=item.get("item_kind"),
                _from_session_sync=True,
                **linked_kwargs,
            )
            outcome_event_ids[item_id] = str(outcome["event"]["event_id"])
            created += int(outcome["created"])
            replayed += int(not outcome["created"])
        elif kind == "repair_recorded":
            repair_counts[item_id] = repair_counts.get(item_id, 0) + 1
            response = row.get("response_seconds")
            response_value = float(response) if isinstance(response, (int, float)) and response > 0 else None
            repair = record_repair(
                repo,
                session_id=session,
                item_id=item_id,
                source_id=source_id,
                result={"fragile_correct": "independent_correct", "blank": "uncertain"}.get(
                    str(row.get("result")), str(row.get("result"))
                ),
                confidence=row.get("confidence"),
                prompt_level=row.get("prompt_level"),
                event_time=str(raw.get("timestamp")),
                observed_date=_event_local_date(
                    str(raw.get("timestamp"))
                ).isoformat(),
                session_review_date=review_date,
                idempotency_key=f"morning:{session}:{item_id}:repair:{repair_counts[item_id]}",
                attempt=repair_counts[item_id],
                response_seconds=response_value,
                legacy_import=payload.get("queue_schema") not in {"morning_review_action_queue_v3"},
            )
            created += int(repair["created"])
            replayed += int(not repair["created"])
    runtime = reconcile_runtime_views(repo)
    return {
        "session_id": session,
        "item_id": target_item,
        "created_event_count": created,
        "replayed_event_count": replayed,
        "outcome_event_ids": outcome_event_ids,
        "runtime": runtime,
    }


def migrate_sessions(repo: Path) -> dict[str, Any]:
    repo = _resolve_repo(repo)
    root = repo / MORNING_REL
    results = []
    if root.is_dir():
        for directory in sorted(path for path in root.iterdir() if path.is_dir()):
            if (directory / "events.jsonl").is_file():
                results.append(sync_session(repo, directory.name))
    runtime = reconcile_runtime_views(repo)
    return {
        "schema": "review_feedback_loop_migration_v1",
        "session_count": len(results),
        "created_event_count": sum(item["created_event_count"] for item in results),
        "replayed_event_count": sum(item["replayed_event_count"] for item in results),
        "runtime": runtime,
    }


def confirm_candidate(
    repo: Path,
    candidate_id: str,
    idempotency_key: str,
    formal_node_id: str | None = None,
) -> dict[str, Any]:
    repo = _resolve_repo(repo)
    candidate_value = _safe_id(candidate_id, "candidate_id", required=True)
    key = _safe_id(idempotency_key, "idempotency_key", required=True)
    formal_node_value = _safe_id(formal_node_id, "formal_node_id")
    assert candidate_value and key
    if formal_node_value and not FORMAL_NODE_RE.fullmatch(formal_node_value):
        raise ReviewLoopError("formal_node_id is invalid")
    request_payload = {
        "candidate_id": candidate_value,
        "explicit_user_confirmation": True,
    }
    if formal_node_value:
        request_payload["resolved_formal_node_id"] = formal_node_value
    request_hash = hashlib.sha256(canonical_json(request_payload).encode("utf-8")).hexdigest()
    with runtime_lock(repo):
        events = _read_events(repo)
        existing = _event_by_key(events, key)
        if existing:
            if existing.get("request_hash") != request_hash:
                raise ReviewLoopError("idempotency key was reused with a different candidate confirmation")
            return {"created": False, "event": existing, "state": _reduce(events)}
        state = _reduce(events)
        candidate = next(
            (item for item in state["candidates"] if item["candidate_id"] == candidate_value),
            None,
        )
        if candidate is None:
            raise ReviewLoopError(f"unknown candidate: {candidate_value}")
        candidate_status = candidate.get("status")
        if candidate_status == "needs_user":
            if not formal_node_value:
                raise ReviewLoopError("needs_user candidate requires an explicit formal_node_id resolution")
            source_event = next(
                (
                    item
                    for item in state["events"]
                    if item.get("event_id") == candidate.get("created_by_event_id")
                ),
                None,
            )
            mapped_nodes_for_event = set((source_event or {}).get("mapped_formal_node_ids") or [])
            if formal_node_value not in mapped_nodes_for_event:
                raise ReviewLoopError("resolved formal_node_id is not one of the candidate's mapped nodes")
        elif candidate_status == "pending_user_confirmation":
            existing_target = candidate.get("formal_node_id")
            if formal_node_value and existing_target and formal_node_value != existing_target:
                raise ReviewLoopError("formal_node_id conflicts with the candidate's existing target")
        else:
            raise ReviewLoopError("candidate is not eligible for explicit confirmation")
        event = {
            "schema": LEDGER_SCHEMA,
            "event_kind": "candidate_confirmation",
            "event_id": content_id("RE", key),
            "idempotency_key": key,
            "request_hash": request_hash,
            "event_time": utc_now(),
            "candidate_id": candidate_value,
            "resolved_formal_node_id": formal_node_value,
            "explicit_user_confirmation": True,
            "handoff_skill": "kaoyan-408-wrong-intake",
            "formal_write_authorized": False,
        }
        append_jsonl(_loop_root(repo) / "events.jsonl", event)
        events.append(event)
        state = _reduce(events)
        atomic_write_json(_loop_root(repo) / "state.json", state)
    runtime = reconcile_runtime_views(repo)
    return {"created": True, "event": event, "state": state, "runtime": runtime}


def close_candidate_as_evidence_only(
    repo: Path,
    candidate_id: str,
    idempotency_key: str,
    *,
    cancel_generated_actions: bool = False,
) -> dict[str, Any]:
    repo = _resolve_repo(repo)
    candidate_value = _safe_id(candidate_id, "candidate_id", required=True)
    key = _safe_id(idempotency_key, "idempotency_key", required=True)
    assert candidate_value and key
    request_payload = {
        "candidate_id": candidate_value,
        "disposition": "evidence_only",
        "explicit_user_confirmation": True,
        "cancel_generated_actions": cancel_generated_actions,
    }
    request_hash = hashlib.sha256(canonical_json(request_payload).encode("utf-8")).hexdigest()
    with runtime_lock(repo):
        events = _read_events(repo)
        existing = _event_by_key(events, key)
        if existing:
            if existing.get("request_hash") != request_hash:
                raise ReviewLoopError("idempotency key was reused with a different candidate disposition")
            return {"created": False, "event": existing, "state": _reduce(events)}
        state = _reduce(events)
        candidate = next(
            (item for item in state["candidates"] if item["candidate_id"] == candidate_value),
            None,
        )
        if candidate is None:
            raise ReviewLoopError(f"unknown candidate: {candidate_value}")
        candidate_status = candidate.get("status")
        if candidate_status not in {"needs_user", "pending_user_confirmation"}:
            raise ReviewLoopError("only an unconfirmed candidate can be closed as evidence_only")
        dismissed_action_ids: list[str] = []
        if cancel_generated_actions:
            source_event_id = str(candidate.get("created_by_event_id", ""))
            for action in state["actions"]:
                trigger_ids = set(
                    action.get("trigger_event_ids")
                    or ([action.get("trigger_event_id")] if action.get("trigger_event_id") else [])
                )
                if (
                    action.get("status") == "pending"
                    and source_event_id
                    and trigger_ids == {source_event_id}
                ):
                    dismissed_action_ids.append(str(action["action_id"]))
        if candidate_status == "needs_user":
            resolution_reason = "explicit user confirmation that no mapped formal node fits"
        else:
            resolution_reason = "explicit user decision that this mechanism should remain evidence-only"
        event = {
            "schema": LEDGER_SCHEMA,
            "event_kind": "candidate_disposition",
            "event_id": content_id("RE", key),
            "idempotency_key": key,
            "request_hash": request_hash,
            "event_time": utc_now(),
            "candidate_id": candidate_value,
            "disposition": "evidence_only",
            "resolution_reason": resolution_reason,
            "dismissed_action_ids": dismissed_action_ids,
            "explicit_user_confirmation": True,
            "formal_write_authorized": False,
        }
        append_jsonl(_loop_root(repo) / "events.jsonl", event)
        events.append(event)
        state = _reduce(events)
        atomic_write_json(_loop_root(repo) / "state.json", state)
    runtime = reconcile_runtime_views(repo)
    return {"created": True, "event": event, "state": state, "runtime": runtime}


def dismiss_review_item(
    repo: Path,
    session_id: str,
    item_id: str,
    source_id: str,
    trigger_action_id: str,
    idempotency_key: str,
    reason: str,
) -> dict[str, Any]:
    repo = _resolve_repo(repo)
    session_value = _safe_id(session_id, "session_id", required=True)
    item_value = _safe_id(item_id, "item_id", required=True)
    source_value = _safe_text(source_id, "source_id", required=True)
    action_value = _safe_id(trigger_action_id, "trigger_action_id", required=True)
    key = _safe_id(idempotency_key, "idempotency_key", required=True)
    if reason not in {"duplicate", "redundant", "user_removed"}:
        raise ReviewLoopError("dismiss reason must be duplicate, redundant, or user_removed")
    assert session_value and item_value and source_value and action_value and key
    request_payload = {
        "session_id": session_value,
        "item_id": item_value,
        "source_id": source_value,
        "trigger_action_id": action_value,
        "reason": reason,
    }
    request_hash = hashlib.sha256(canonical_json(request_payload).encode("utf-8")).hexdigest()
    with runtime_lock(repo):
        events = _read_events(repo)
        existing = _event_by_key(events, key)
        if existing:
            if existing.get("request_hash") != request_hash:
                raise ReviewLoopError("idempotency key was reused with a different item disposition")
            return {"created": False, "event": existing, "state": _reduce(events)}
        state = _reduce(events)
        action = _action_by_id(state, action_value)
        if action is None:
            raise ReviewLoopError(f"unknown trigger action: {action_value}")
        if action.get("status") != "pending":
            raise ReviewLoopError("only a pending review action can be dismissed")
        if action.get("source_id") != source_value:
            raise ReviewLoopError("dismissed item source does not match trigger action")
        event = {
            "schema": LEDGER_SCHEMA,
            "event_kind": "item_disposition",
            "event_id": content_id("RE", key),
            "idempotency_key": key,
            "request_hash": request_hash,
            "event_time": utc_now(),
            "session_id": session_value,
            "item_id": item_value,
            "source_id": source_value,
            "trigger_action_id": action_value,
            "disposition": "dismissed",
            "resolution_reason": reason,
            "explicit_user_confirmation": True,
            "formal_write_authorized": False,
        }
        append_jsonl(_loop_root(repo) / "events.jsonl", event)
        events.append(event)
        state = _reduce(events)
        atomic_write_json(_loop_root(repo) / "state.json", state)
    runtime = reconcile_runtime_views(repo)
    return {"created": True, "event": event, "state": state, "runtime": runtime}


def retire_closed_equivalent_item(
    repo: Path,
    *,
    session_id: str,
    item_id: str,
    source_id: str,
    trigger_action_id: str,
    formal_binding_event_id: str,
    idempotency_key: str,
    reason: str,
    user_confirmed: bool,
) -> dict[str, Any]:
    """Append one answer-free administrative terminal for a closed equivalent.

    This is deliberately separate from ``dismiss_review_item``.  The latter
    consumes a pending action; this bridge only retires a stale session item
    whose exact action was already closed by a receipt-bound formal equivalence.
    """

    repo = _resolve_repo(repo)
    session_value = _safe_id(session_id, "session_id", required=True)
    item_value = _safe_id(item_id, "item_id", required=True)
    source_value = _safe_text(source_id, "source_id", required=True)
    action_value = _safe_id(
        trigger_action_id, "trigger_action_id", required=True
    )
    binding_value = _safe_id(
        formal_binding_event_id, "formal_binding_event_id", required=True
    )
    key = _safe_id(idempotency_key, "idempotency_key", required=True)
    if reason != "user_removed":
        raise ReviewLoopError(
            "closed-equivalent item retirement reason must be user_removed"
        )
    if user_confirmed is not True:
        raise ReviewLoopError(
            "closed-equivalent item retirement requires explicit user confirmation"
        )
    assert (
        session_value
        and item_value
        and source_value
        and action_value
        and binding_value
        and key
    )

    try:
        import morning_review_session as morning_session
    except ImportError as exc:
        raise ReviewLoopError(
            "closed-equivalent item retirement session writer is unavailable"
        ) from exc
    session_dir = morning_session._session_dir(repo, session_value)
    with morning_session.session_lock(session_dir):
        try:
            session_state = morning_session._load_state(
                session_dir, session_value
            )
            session_item = morning_session._get_item(
                session_state, item_value
            )
        except morning_session.SessionError as exc:
            raise ReviewLoopError(str(exc)) from exc
        if (
            session_item.get("source_id") != source_value
            or session_item.get("action_id") != action_value
            or session_item.get("first") is not None
            or session_item.get("resolution")
            not in {"pending", "administratively_retired"}
            or (
                session_item.get("resolution") == "pending"
                and session_state.get("status") not in ACTIVE_SESSION_STATES
            )
        ):
            raise ReviewLoopError(
                "closed-equivalent item retirement requires one unanswered pending session item"
            )

        with runtime_lock(repo):
            events = _read_events(repo)
            existing = _event_by_key(events, key)
            state = _reduce(events)
            if existing is not None:
                if (
                    existing.get("event_kind")
                    != CLOSED_EQUIVALENT_ITEM_RETIREMENT_KIND
                    or existing.get("session_id") != session_value
                    or existing.get("item_id") != item_value
                    or existing.get("source_id") != source_value
                    or existing.get("trigger_action_id") != action_value
                    or existing.get("formal_binding_event_id") != binding_value
                    or existing.get("resolution_reason") != reason
                ):
                    raise ReviewLoopError(
                        "idempotency key was reused with a different closed-equivalent retirement"
                    )
                created = False
                event = existing
            else:
                if any(
                    event.get("event_kind")
                    == CLOSED_EQUIVALENT_ITEM_RETIREMENT_KIND
                    and event.get("session_id") == session_value
                    and event.get("item_id") == item_value
                    for event in events
                ):
                    raise ReviewLoopError(
                        "morning item already has a closed-equivalent retirement"
                    )
                action = _action_by_id(state, action_value)
                if (
                    action is None
                    or action.get("status") != "closed_equivalent"
                ):
                    raise ReviewLoopError(
                        "closed-equivalent item retirement requires a closed_equivalent action"
                    )
                equivalent_to = _safe_id(
                    str(action.get("equivalent_to_action_id") or ""),
                    "equivalent_to_action_id",
                    required=True,
                )
                assert equivalent_to
                binding_matches = [
                    binding
                    for binding in state.get("formal_review_bindings") or []
                    if binding.get("event_id") == binding_value
                ]
                if len(binding_matches) != 1:
                    raise ReviewLoopError(
                        "closed-equivalent item retirement requires the exact formal binding event"
                    )
                binding = binding_matches[0]
                request = _closed_equivalent_retirement_request(
                    session_id=session_value,
                    item_id=item_value,
                    source_id=source_value,
                    trigger_action_id=action_value,
                    formal_binding_event_id=binding_value,
                    reason=reason,
                    equivalent_to_action_id=equivalent_to,
                )
                request_hash = hashlib.sha256(
                    canonical_json(request).encode("utf-8")
                ).hexdigest()
                if (
                    session_item.get("resolution")
                    == "administratively_retired"
                ):
                    try:
                        stored_disposition = (
                            morning_session._validate_administrative_retirement_disposition(
                                session_item,
                                session_item.get("administrative_disposition"),
                            )
                        )
                    except morning_session.SessionError as exc:
                        raise ReviewLoopError(str(exc)) from exc
                    event_time = str(
                        stored_disposition[
                            "canonical_retirement_event_time"
                        ]
                    )
                else:
                    event_time = utc_now()
                event = {
                    "schema": LEDGER_SCHEMA,
                    "event_kind": CLOSED_EQUIVALENT_ITEM_RETIREMENT_KIND,
                    "event_id": content_id(
                        "RE",
                        {
                            "kind": CLOSED_EQUIVALENT_ITEM_RETIREMENT_KIND,
                            "key": key,
                        },
                    ),
                    "idempotency_key": key,
                    "request_hash": request_hash,
                    "event_time": event_time,
                    "observed_date": _event_local_date(event_time).isoformat(),
                    "timezone": "Asia/Shanghai",
                    "source": "morning_review_administration",
                    "session_id": session_value,
                    "item_id": item_value,
                    "source_id": source_value,
                    "trigger_action_id": action_value,
                    "disposition": "administratively_retired",
                    "resolution_reason": reason,
                    "explicit_user_confirmation": True,
                    "answer_free_terminal": True,
                    "formal_binding_event_id": binding_value,
                    "formal_binding_event_sha256": _sha256_value(binding),
                    "equivalent_to_action_id": equivalent_to,
                    "closed_equivalent_action_sha256": _sha256_value(action),
                    "processing_contract": (
                        CLOSED_EQUIVALENT_ITEM_RETIREMENT_CONTRACT
                    ),
                    "learner_evidence_write_count": 0,
                    "formal_write_authorized": False,
                    "formal_write_count": 0,
                    "generated_actions": [],
                }
                state = _reduce([*events, event])
                created = True

            administrative_disposition = {
                "status": "administratively_retired",
                "canonical_retirement_event_id": str(event["event_id"]),
                "canonical_retirement_event_sha256": _sha256_value(event),
                "canonical_retirement_event_time": str(event["event_time"]),
                "formal_binding_event_id": str(
                    event["formal_binding_event_id"]
                ),
                "formal_binding_event_sha256": str(
                    event["formal_binding_event_sha256"]
                ),
                "action_id": action_value,
                "closed_equivalent_action_sha256": str(
                    event["closed_equivalent_action_sha256"]
                ),
                "equivalent_to_action_id": str(
                    event["equivalent_to_action_id"]
                ),
                "resolution_reason": "user_removed",
                "processing_contract": (
                    CLOSED_EQUIVALENT_ITEM_RETIREMENT_CONTRACT
                ),
                "learner_evidence_write_count": 0,
                "formal_write_count": 0,
            }
            try:
                session_projection = (
                    morning_session._commit_closed_equivalent_retirement_locked(
                        session_dir,
                        session_state,
                        session_id=session_value,
                        item_id=item_value,
                        administrative_disposition=administrative_disposition,
                    )
                )
            except morning_session.SessionError as exc:
                raise ReviewLoopError(
                    "closed-equivalent retirement session projection failed before canonical append"
                ) from exc

            if created:
                append_jsonl(_loop_root(repo) / "events.jsonl", event)
                atomic_write_json(_loop_root(repo) / "state.json", state)

            hot_state: dict[str, Any] = {
                "status": "not_present",
                "rebuild_performed": False,
                "formal_write_count": 0,
            }
            hot_pointer = _loop_root(repo) / "hot-state" / "manifest.json"
            if hot_pointer.is_file():
                try:
                    import review_hot_state_408 as review_hot

                    rebuilt = review_hot.cold_rebuild(repo)
                except Exception as exc:
                    raise ReviewLoopError(
                        "closed-equivalent retirement committed but review hot-state convergence failed"
                    ) from exc
                hot_state = {
                    "status": rebuilt.get("status"),
                    "rebuild_performed": True,
                    "projection_build_id": rebuilt.get(
                        "projection_build_id"
                    ),
                    "formal_write_count": rebuilt.get(
                        "formal_write_count", 0
                    ),
                }
            runtime = _write_runtime_views_locked(repo)
    return {
        "created": created,
        "event": event,
        "state": state,
        "hot_state": hot_state,
        "runtime": runtime,
        "session_projection": {
            key: value
            for key, value in session_projection.items()
            if key != "state"
        },
        "learner_evidence_write_count": 0,
        "formal_write_count": 0,
    }


def _session_first_result(first: dict[str, Any]) -> str:
    result, _ = normalize_morning_first_classification(
        str(first.get("result")),
        choice_result=str(first.get("choice_result") or "not_applicable"),
        reasoning_result=str(first.get("reasoning_result") or "not_observed"),
        prompt_level=str(first.get("prompt_level") or ""),
    )
    return result


def _normalized_response_seconds(value: object) -> float | None:
    if not isinstance(value, (int, float)) or float(value) <= 0:
        return None
    return float(value)


def _active_session_alignment_errors(
    events: list[dict[str, Any]], active_states: list[dict[str, Any]]
) -> list[str]:
    errors: list[str] = []
    outcomes: dict[tuple[str, str], list[dict[str, Any]]] = {}
    active_ids = {str(state.get("session_id")) for state in active_states}
    for event in events:
        if (
            event.get("event_kind") == "outcome"
            and event.get("source") == "morning_review"
            and str(event.get("session_id")) in active_ids
        ):
            key = (str(event.get("session_id")), str(event.get("item_id")))
            outcomes.setdefault(key, []).append(event)

    for state in active_states:
        session_id = str(state.get("session_id"))
        review_date = str(state.get("review_date"))
        items = state.get("items")
        if not isinstance(items, dict):
            errors.append(f"active session {session_id} lacks item projection")
            continue
        for item_id, item in items.items():
            if not isinstance(item, dict) or item.get("resolution") == "dismissed":
                continue
            key = (session_id, str(item_id))
            matches = outcomes.get(key, [])
            first = item.get("first")
            label = f"{session_id}/{item_id}"
            if len(matches) > 1:
                errors.append(f"active session item has multiple canonical outcomes: {label}")
                continue
            if first is None and matches:
                errors.append(f"canonical outcome is not projected into active session: {label}")
                continue
            if first is not None and not matches:
                errors.append(f"active session first answer lacks canonical outcome: {label}")
                continue
            if first is None:
                continue
            event = matches[0]
            try:
                projected_provenance = normalize_first_break_provenance(
                    first.get("first_break_provenance"), first.get("first_break")
                )
                canonical_provenance = normalize_first_break_provenance(
                    event.get("first_break_provenance"), event.get("first_break")
                )
            except ReviewLoopError as exc:
                errors.append(f"active session invalid first_break provenance: {label} ({exc})")
                continue
            comparisons = {
                "source_id": (item.get("source_id"), event.get("source_id")),
                "session_review_date": (
                    review_date,
                    morning_session_review_date(event),
                ),
                "result": (_session_first_result(first), event.get("first_result")),
                "choice_result": (
                    str(first.get("choice_result") or "not_applicable"),
                    str(event.get("choice_result") or "not_applicable"),
                ),
                "reasoning_result": (
                    str(first.get("reasoning_result") or "not_observed"),
                    str(event.get("reasoning_result") or "not_observed"),
                ),
                "confidence": (first.get("confidence"), event.get("confidence")),
                "prompt_level": (first.get("prompt_level"), event.get("prompt_level")),
                "first_break": (first.get("first_break"), event.get("first_break")),
                "first_break_provenance": (
                    projected_provenance,
                    canonical_provenance,
                ),
                "recorded_at": (first.get("recorded_at"), event.get("event_time")),
                "response_seconds": (
                    _normalized_response_seconds(first.get("response_seconds")),
                    _normalized_response_seconds(event.get("response_seconds")),
                ),
            }
            if item.get("practice_mode"):
                comparisons.update(
                    {
                        "practice_mode": (
                            item.get("practice_mode"),
                            event.get("practice_mode"),
                        ),
                        "anchor_formal_node_id": (
                            item.get("anchor_formal_node_id"),
                            event.get("anchor_formal_node_id"),
                        ),
                        "mechanism_key": (
                            item.get("mechanism_key"),
                            event.get("mechanism_key"),
                        ),
                        "knowledge_point": (
                            item.get("knowledge_point"),
                            event.get("knowledge_point"),
                        ),
                        "followup_event_id": (
                            item.get("followup_event_id"),
                            event.get("followup_event_id"),
                        ),
                        "parent_trigger_event_id": (
                            item.get("trigger_event_id"),
                            event.get("parent_trigger_event_id"),
                        ),
                        "context_slice_id": (
                            item.get("context_slice_id"),
                            event.get("context_slice_id"),
                        ),
                        "candidate_id": (
                            item.get("candidate_id"),
                            event.get("candidate_id"),
                        ),
                        "source_ref": (
                            item.get("source_ref"),
                            event.get("source_ref"),
                        ),
                        "source_sha256": (
                            item.get("source_sha256"),
                            event.get("source_sha256"),
                        ),
                        "selection_receipt_ref": (
                            item.get("selection_receipt_ref"),
                            event.get("selection_receipt_ref"),
                        ),
                        "selection_receipt_sha256": (
                            item.get("selection_receipt_sha256"),
                            event.get("selection_receipt_sha256"),
                        ),
                    }
                )
                expected_actual = (
                    item.get("formal_node_id")
                    if item.get("practice_mode") == "original"
                    else None
                )
                comparisons["actual_question_formal_node_id"] = (
                    expected_actual,
                    event.get("actual_question_formal_node_id"),
                )
            for field, (projected, canonical) in comparisons.items():
                if projected != canonical:
                    errors.append(
                        f"active session/canonical {field} mismatch: {label}"
                    )
            canonical_event_id = first.get("canonical_event_id")
            if canonical_event_id and canonical_event_id != event.get("event_id"):
                errors.append(f"active session canonical event mismatch: {label}")
    return errors


def audit(repo: Path) -> dict[str, Any]:
    repo = _resolve_repo(repo)
    errors: list[str] = []
    protected: list[str] = []
    try:
        events = _read_events(repo)
        expected = _reduce(events)
    except ReviewLoopError as exc:
        return {
            "schema": "review_feedback_loop_audit_v1",
            "status": "FAIL",
            "errors": [str(exc)],
            "protected_findings": [],
        }
    payload = canonical_json(events)
    fails, warns = scan_leak(payload)
    if PROTECTED_RE.search(payload) or fails or warns:
        protected = sorted(set(fails + warns + (["protected keyword"] if PROTECTED_RE.search(payload) else [])))
        errors.append("protected content found in review ledger")
    stored_path = _loop_root(repo) / "state.json"
    try:
        stored = json.loads(stored_path.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError):
        stored = None
    if stored != expected:
        errors.append("state projection differs from ledger replay")
    actions = expected["actions"]
    if len({item["action_id"] for item in actions}) != len(actions):
        errors.append("duplicate action IDs")
    pending_keys = [item["dedupe_key"] for item in expected["pending_actions"]]
    if len(set(pending_keys)) != len(pending_keys):
        errors.append("duplicate pending action dedupe keys")
    states, state_errors = _session_states(repo)
    errors.extend(state_errors)
    active = [state for state in states if state.get("status") in ACTIVE_SESSION_STATES]
    if len(active) > 1:
        errors.append("more than one active morning session")
    errors.extend(_active_session_alignment_errors(events, active))
    scheduling_report = scheduling_drift_audit(repo)
    unresolved_scheduling = [
        row
        for row in scheduling_report.get("gaps") or []
        if row.get("action_status") == "pending"
        and row.get("status") != "reconciled"
    ]
    if unresolved_scheduling:
        errors.append(
            "answered morning actions still require scheduling reconciliation: "
            + ", ".join(
                str(row.get("action_id")) for row in unresolved_scheduling[:20]
            )
        )
    if scheduling_report.get("errors"):
        errors.append("scheduling drift audit is incomplete")
    runtime_path = _loop_root(repo) / "runtime.json"
    try:
        runtime = json.loads(runtime_path.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError):
        runtime = None
    expected_runtime = _runtime_projection(repo)
    if runtime != expected_runtime:
        errors.append("runtime projection differs from session truth")
    dashboard_path = _loop_root(repo) / "dashboard.json"
    try:
        dashboard = json.loads(dashboard_path.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError):
        dashboard = None
    expected_dashboard = dict(expected_runtime)
    expected_dashboard["schema"] = "review_loop_dashboard_v1"
    if dashboard != expected_dashboard:
        errors.append("dashboard projection differs from runtime truth")
    pointer = repo / DASHBOARD_REL / "当前晨间复盘.md"
    pointer_meta = _parse_frontmatter(pointer.read_text(encoding="utf-8")) if pointer.is_file() else {}
    if pointer_meta.get("schema") != POINTER_SCHEMA:
        errors.append("pointer is missing or not v2")
    else:
        for field, expected_value in {
            "status": expected_runtime["status"],
            "review_date": expected_runtime["review_date"],
            "active_session_id": expected_runtime["active_session_id"] or "none",
        }.items():
            if pointer_meta.get(field) != str(expected_value):
                errors.append(f"pointer {field} drift")
    return {
        "schema": "review_feedback_loop_audit_v1",
        "status": "PASS" if not errors else "FAIL",
        "event_count": len(events),
        "pending_action_count": len(expected["pending_actions"]),
        "candidate_count": len(expected["candidates"]),
        "retention_count": sum(item["count"] for item in expected["retention"].values()),
        "repair_count": sum(item.get("event_kind") == "repair" for item in events),
        "duplicate_pending_action_count": len(pending_keys) - len(set(pending_keys)),
        "active_session_count": len(active),
        "scheduling_gap_count": len(unresolved_scheduling),
        "scheduling_reconciliation_count": expected.get(
            "scheduling_reconciliation_count", 0
        ),
        "protected_findings": protected,
        "formal_write_count": expected["formal_write_count"],
        "capture_write_count": 0,
        "luna_call_count": 0,
        "errors": errors,
    }


def _capture_review_event_id(capture: dict[str, Any]) -> str | None:
    payload = capture.get("capture") or {}
    source_facts = payload.get("source_facts") or {}
    details_id = str(source_facts.get("details_id") or "")
    if details_id.startswith("RE-"):
        return details_id
    matches: set[str] = set()
    for ref in payload.get("stable_evidence_refs") or []:
        if not isinstance(ref, dict) or ref.get("kind") not in {
            "review_loop_event",
            "ordinary_review_loop_event",
        }:
            continue
        found = re.search(r"#event_id=(RE-[A-Za-z0-9._-]+)$", str(ref.get("locator") or ""))
        if found:
            matches.add(found.group(1))
    return next(iter(matches)) if len(matches) == 1 else None


def daily_summary(repo: Path, study_date: str) -> dict[str, Any]:
    """Return one read-only, actual-date learning list with capture joins."""

    repo = _resolve_repo(repo)
    day = _parse_date(study_date, "study_date").isoformat()
    events = _read_events(repo)
    session_cache: dict[str, list[dict[str, Any]]] = {}
    projection_errors: list[dict[str, str]] = []
    selected: list[tuple[dict[str, Any], str, str | None]] = []
    for event in events:
        if event.get("event_kind") != "outcome":
            continue
        try:
            effective_date, basis, session_date = effective_observed_date(
                repo, event, session_cache=session_cache
            )
        except ReviewLoopError as exc:
            candidate_dates = {str(event.get("observed_date") or "")}
            try:
                candidate_dates.add(
                    _event_local_date(
                        str(event.get("event_time") or "")
                    ).isoformat()
                )
            except ReviewLoopError:
                pass
            if day in candidate_dates:
                projection_errors.append(
                    {
                        "event_id": str(event.get("event_id") or ""),
                        "reason": str(exc),
                    }
                )
            continue
        if effective_date == day:
            selected.append((event, basis, session_date))

    try:
        import intake_fact_capture_408 as capture_model

        capture_state = capture_model.status(None, repo_root=repo)
    except (ImportError, OSError, ValueError, RuntimeError) as exc:
        raise ReviewLoopError(f"capture ledger cannot be replayed: {exc}") from exc
    captures = capture_state.get("captures") or {}
    neutral_saves = capture_state.get("neutral_saves") or {}
    capture_by_event: dict[str, dict[str, Any]] = {}
    for capture_id, capture in captures.items():
        if not isinstance(capture, dict):
            continue
        event_id = _capture_review_event_id(capture)
        if not event_id:
            continue
        if event_id in capture_by_event:
            raise ReviewLoopError(
                f"multiple captures reference one review event: {event_id}"
            )
        capture_by_event[event_id] = {
            "capture_id": str(capture_id),
            "study_date": capture.get("study_date"),
            "quality_status": capture.get("quality_status"),
            "formalization_authorized": bool(
                capture.get("formalization_authorized")
            ),
            "current_batch_id": capture.get("current_batch_id"),
        }
    neutral_by_event: dict[str, dict[str, Any]] = {}
    for save_id, save in neutral_saves.items():
        if not isinstance(save, dict):
            continue
        event_id = str(save.get("review_event_id") or "")
        if not event_id:
            continue
        if event_id in neutral_by_event:
            raise ReviewLoopError(
                f"multiple neutral saves reference one review event: {event_id}"
            )
        neutral_by_event[event_id] = {
            "neutral_save_id": str(save_id),
            "study_date": save.get("study_date"),
            "status": save.get("status"),
            "formalization_authorized": False,
            "curation_eligible": False,
        }

    rows: list[dict[str, Any]] = []
    result_counts = {result: 0 for result in sorted(FIRST_RESULTS)}
    correct_count = 0
    fragile_correct_count = 0
    capture_eligible_noncorrect_count = 0
    for event, basis, session_date in sorted(
        selected,
        key=lambda item: (
            str(item[0].get("event_time") or ""),
            str(item[0].get("event_id") or ""),
        ),
    ):
        event_id = str(event["event_id"])
        result = str(event.get("first_result") or "")
        if result in result_counts:
            result_counts[result] += 1
        fragile_correct = bool(
            result == "independent_correct"
            and (
                event.get("fragile_mastery") is True
                or event.get("evidence_kind") == "fragile"
            )
        )
        if fragile_correct:
            fragile_correct_count += 1
        elif result == "independent_correct":
            correct_count += 1
        elif result in {"partial", "wrong", "uncertain"}:
            capture_eligible_noncorrect_count += 1
        capture = capture_by_event.get(event_id)
        neutral = neutral_by_event.get(event_id)
        rows.append(
            {
                "event_id": event_id,
                "event_time": event.get("event_time"),
                "observed_date": day,
                "date_basis": basis,
                "session_review_date": session_date,
                "source": event.get("source"),
                "session_id": event.get("session_id"),
                "item_id": event.get("item_id"),
                "source_id": event.get("source_id"),
                "mechanism_key": event.get("mechanism_key"),
                "knowledge_point": event.get("knowledge_point"),
                "first_result": result,
                "result_group": (
                    "fragile_correct"
                    if fragile_correct
                    else "correct"
                    if result == "independent_correct"
                    else "non_correct"
                ),
                "confidence": event.get("confidence"),
                "evidence_kind": event.get("evidence_kind"),
                "wrong_capture": capture,
                "neutral_save": neutral,
                "capture_date_mismatch": bool(
                    capture is not None and capture.get("study_date") != day
                ),
            }
        )

    pending_wrong = sorted(
        str(capture_id)
        for capture_id, capture in captures.items()
        if isinstance(capture, dict)
        and capture.get("study_date") == day
        and capture.get("quality_status") == capture_model.PENDING_FORMAL_STATUS
    )
    blocked_wrong = sorted(
        str(capture_id)
        for capture_id, capture in captures.items()
        if isinstance(capture, dict)
        and capture.get("study_date") == day
        and capture.get("quality_status") in {"needs_user", "curation_failed"}
    )
    dated_neutral = sorted(
        str(save_id)
        for save_id, save in neutral_saves.items()
        if isinstance(save, dict) and save.get("study_date") == day
    )
    return {
        "schema": "review_daily_summary_v1",
        "study_date": day,
        "timezone": "Asia/Shanghai",
        "complete": not projection_errors,
        "outcome_count": len(rows),
        "correct_count": correct_count,
        "fragile_correct_count": fragile_correct_count,
        "capture_eligible_noncorrect_count": capture_eligible_noncorrect_count,
        "non_correct_count": capture_eligible_noncorrect_count,
        "result_counts": result_counts,
        "outcomes": rows,
        "pending_curation_capture_ids": pending_wrong,
        "blocked_capture_ids": blocked_wrong,
        "neutral_save_ids": dated_neutral,
        "date_projection_errors": projection_errors,
        "read_only": True,
        "formal_write_count": 0,
    }


def _sha256_value(value: Any) -> str:
    return hashlib.sha256(canonical_json(value).encode("utf-8")).hexdigest()


def _verified_bound_file(
    repo: Path, reference: object, expected_sha256: object, label: str
) -> str:
    reference_value = str(reference or "")
    expected = str(expected_sha256 or "")
    if not reference_value or not re.fullmatch(r"[0-9a-f]{64}", expected):
        raise ReviewLoopError(f"{label} binding is incomplete")
    path = (repo / reference_value).resolve()
    try:
        path.relative_to(repo.resolve())
    except ValueError as exc:
        raise ReviewLoopError(f"{label} escaped repo") from exc
    if not path.is_file() or path.is_symlink():
        raise ReviewLoopError(f"{label} is unavailable")
    actual = hashlib.sha256(path.read_bytes()).hexdigest()
    if actual != expected:
        raise ReviewLoopError(f"{label} hash drifted")
    return actual


def _scheduling_evidence_for_item(
    repo: Path,
    *,
    session: dict[str, Any],
    item_id: str,
    item: dict[str, Any],
    outcome: dict[str, Any],
    action: dict[str, Any],
    session_cache: dict[str, list[dict[str, Any]]],
) -> dict[str, Any]:
    errors: list[str] = []
    session_id = str(session.get("session_id") or "")
    action_id = str(action.get("action_id") or "")
    if (
        outcome.get("event_kind") != "outcome"
        or outcome.get("source") != "morning_review"
        or outcome.get("session_id") != session_id
        or outcome.get("item_id") != item_id
    ):
        errors.append("morning_schedule_outcome_session_item_mismatch")
    if outcome.get("source_id") != item.get("source_id"):
        errors.append("morning_schedule_identity_mismatch")
    identity_candidates = {
        str(value)
        for value in (
            item.get("source_id"),
            item.get("review_unit_id"),
            item.get("formal_node_id"),
        )
        if value
    }
    if str(action.get("identity") or "") not in identity_candidates:
        errors.append("morning_schedule_action_identity_mismatch")
    if item.get("action_id") != action_id:
        errors.append("morning_schedule_session_item_action_mismatch")
    if item.get("stage") != action.get("stage"):
        errors.append("morning_schedule_stage_mismatch")
    if item.get("trigger_event_id") != action.get("trigger_event_id"):
        errors.append("morning_schedule_trigger_event_mismatch")
    first = item.get("first")
    if not isinstance(first, dict):
        errors.append("scheduling_evidence_incomplete:first_answer_missing")
        first = {}
    if first.get("canonical_event_id") != outcome.get("event_id"):
        errors.append("morning_schedule_canonical_binding_mismatch")
    try:
        normalized_result, fragile_override = normalize_morning_first_classification(
            str(first.get("result") or ""),
            choice_result=str(first.get("choice_result") or "not_applicable"),
            reasoning_result=str(first.get("reasoning_result") or "not_observed"),
            prompt_level=str(first.get("prompt_level") or ""),
        )
    except ReviewLoopError:
        normalized_result, fragile_override = "", False
        errors.append("scheduling_evidence_incomplete:first_result_invalid")
    comparisons = {
        "first_result": (normalized_result, outcome.get("first_result")),
        "choice_result": (first.get("choice_result"), outcome.get("choice_result")),
        "reasoning_result": (
            first.get("reasoning_result"),
            outcome.get("reasoning_result"),
        ),
        "confidence": (first.get("confidence"), outcome.get("confidence")),
        "prompt_level": (first.get("prompt_level"), outcome.get("prompt_level")),
        "first_break": (first.get("first_break"), outcome.get("first_break")),
        "first_break_provenance": (
            first.get("first_break_provenance"),
            outcome.get("first_break_provenance"),
        ),
        "recorded_at": (first.get("recorded_at"), outcome.get("event_time")),
    }
    for field, (projected, canonical_value) in comparisons.items():
        if projected != canonical_value:
            errors.append(f"morning_schedule_{field}_mismatch")
    try:
        effective_date, date_basis, session_review_date = effective_observed_date(
            repo, outcome, session_cache=session_cache
        )
    except ReviewLoopError as exc:
        effective_date = str(outcome.get("observed_date") or "")
        date_basis = "unverified"
        session_review_date = str(session.get("review_date") or "")
        errors.append(f"scheduling_evidence_incomplete:{exc}")
    try:
        observed_day = _parse_date(effective_date, "effective observed date")
        origin_day = _parse_date(str(action.get("origin_date") or ""), "origin_date")
        due_day = _parse_date(str(action.get("due_date") or ""), "action due_date")
        if due_day > observed_day:
            errors.append("morning_schedule_action_not_due")
        cross_day = origin_day < observed_day
    except ReviewLoopError as exc:
        observed_day = dt.date.min
        origin_day = dt.date.min
        cross_day = False
        errors.append(f"scheduling_evidence_incomplete:{exc}")
    try:
        queue_sha = _verified_bound_file(
            repo,
            session.get("queue_path"),
            session.get("queue_sha256"),
            "morning queue",
        )
        pack_sha = _verified_bound_file(
            repo,
            session.get("delivery_pack_ref"),
            session.get("delivery_pack_sha256"),
            "morning prepared pack",
        )
    except ReviewLoopError as exc:
        queue_sha = str(session.get("queue_sha256") or "")
        pack_sha = str(session.get("delivery_pack_sha256") or "")
        errors.append(f"scheduling_evidence_incomplete:{exc}")
    hint_used = outcome.get("prompt_level") != "none"
    stable_retention = bool(
        not errors
        and cross_day
        and normalized_result == "independent_correct"
        and not hint_used
        and outcome.get("confidence") in {"medium", "high"}
        and outcome.get("latency_state") != "abnormal"
        and not fragile_override
    )
    session_path = repo / MORNING_REL / session_id / "state.json"
    session_state_sha = (
        hashlib.sha256(session_path.read_bytes()).hexdigest()
        if session_path.is_file() and not session_path.is_symlink()
        else ""
    )
    return {
        "session_id": session_id,
        "item_id": item_id,
        "action_id": action_id,
        "outcome_event_id": outcome.get("event_id"),
        "event_time": outcome.get("event_time"),
        "effective_observed_date": effective_date,
        "date_basis": date_basis,
        "session_review_date": session_review_date,
        "cross_day": cross_day,
        "normalized_first_result": normalized_result,
        "stable_retention": stable_retention,
        "fragile_override": fragile_override,
        "session_state_sha256": session_state_sha,
        "queue_sha256": queue_sha,
        "prepared_pack_sha256": pack_sha,
        "outcome_sha256": _sha256_value(outcome),
        "action_sha256": _sha256_value(action),
        "errors": errors,
        "verification_status": "VERIFIED" if not errors else "INCOMPLETE",
    }


def scheduling_drift_audit(repo: Path) -> dict[str, Any]:
    """Find answered session actions that current-only outcomes did not consume."""

    repo = _resolve_repo(repo)
    events = _read_events(repo)
    state = _reduce(events)
    events_by_id = {
        str(event.get("event_id")): event
        for event in events
        if event.get("event_kind") == "outcome"
    }
    actions_by_id = {
        str(action.get("action_id")): action for action in state.get("actions") or []
    }
    existing_by_action = {
        str(event.get("trigger_action_id")): event
        for event in state.get("scheduling_reconciliations") or []
    }
    sessions, session_errors = _session_states(repo)
    session_cache: dict[str, list[dict[str, Any]]] = {}
    evidence_by_action: dict[str, list[dict[str, Any]]] = {}
    orphan_errors: list[str] = list(session_errors)
    for session in sessions:
        items = session.get("items") or {}
        if not isinstance(items, dict):
            orphan_errors.append(
                f"{session.get('session_id')}: session items are invalid"
            )
            continue
        for item_id, item in items.items():
            if not isinstance(item, dict) or not item.get("action_id"):
                continue
            first = item.get("first")
            if not isinstance(first, dict) or not first.get("canonical_event_id"):
                continue
            action_id = str(item["action_id"])
            outcome_id = str(first["canonical_event_id"])
            outcome = events_by_id.get(outcome_id)
            action = actions_by_id.get(action_id)
            if outcome is None:
                orphan_errors.append(
                    f"{session.get('session_id')}:{item_id}: canonical outcome missing"
                )
                continue
            if outcome.get("trigger_action_id") == action_id:
                continue
            if action is None:
                orphan_errors.append(
                    f"{session.get('session_id')}:{item_id}: action missing"
                )
                continue
            evidence = _scheduling_evidence_for_item(
                repo,
                session=session,
                item_id=str(item_id),
                item=item,
                outcome=outcome,
                action=action,
                session_cache=session_cache,
            )
            evidence_by_action.setdefault(action_id, []).append(evidence)
    gaps: list[dict[str, Any]] = []
    for action_id, evidence_rows in sorted(evidence_by_action.items()):
        action = actions_by_id[action_id]
        ordered = sorted(
            evidence_rows,
            key=lambda row: (str(row.get("event_time") or ""), str(row["outcome_event_id"])),
        )
        existing = existing_by_action.get(action_id)
        if existing is not None:
            status = "reconciled"
            authoritative = existing.get("authoritative_outcome_event_id")
        elif action.get("status") != "pending":
            status = "nonpending_audit_only"
            authoritative = None
        else:
            verified = [
                row for row in ordered if row["verification_status"] == "VERIFIED"
            ]
            authoritative = (
                verified[0]["outcome_event_id"] if verified else None
            )
            status = "eligible" if authoritative else "scheduling_evidence_incomplete"
        gaps.append(
            {
                "action_id": action_id,
                "action_status": action.get("status"),
                "stage": action.get("stage"),
                "due_date": action.get("due_date"),
                "origin_date": action.get("origin_date"),
                "identity": action.get("identity"),
                "status": status,
                "authoritative_outcome_event_id": authoritative,
                "superseded_outcome_event_ids": [
                    row["outcome_event_id"]
                    for row in ordered
                    if row["outcome_event_id"] != authoritative
                ],
                "evidence": ordered,
                "existing_reconciliation_event_id": (
                    existing.get("event_id") if existing else None
                ),
            }
        )
    unresolved = [
        row
        for row in gaps
        if row.get("action_status") == "pending"
        and row["status"] != "reconciled"
    ]
    return {
        "schema": "review_scheduling_drift_audit_v1",
        "status": (
            "INCOMPLETE"
            if orphan_errors
            or any(row["status"] != "eligible" for row in unresolved)
            else "DRIFT_FOUND"
            if unresolved
            else "PASS"
        ),
        "answered_action_count": sum(len(rows) for rows in evidence_by_action.values()),
        "affected_action_count": len(unresolved),
        "eligible_action_count": sum(row["status"] == "eligible" for row in unresolved),
        "gaps": gaps,
        "errors": orphan_errors,
        "read_only": True,
        "formal_write_count": 0,
        "capture_write_count": 0,
        "luna_call_count": 0,
    }


def _build_scheduling_reconciliation_event(
    repo: Path, action_id: str
) -> dict[str, Any]:
    audit_report = scheduling_drift_audit(repo)
    gap = next(
        (row for row in audit_report["gaps"] if row.get("action_id") == action_id),
        None,
    )
    if gap is None:
        raise ReviewLoopError(f"unknown answered pending action: {action_id}")
    if gap.get("status") == "reconciled":
        events = _read_events(repo)
        existing_id = gap.get("existing_reconciliation_event_id")
        return next(event for event in events if event.get("event_id") == existing_id)
    if gap.get("status") != "eligible":
        raise ReviewLoopError(
            f"scheduling_evidence_incomplete:{action_id}:{gap.get('status')}"
        )
    authoritative_id = str(gap["authoritative_outcome_event_id"])
    evidence = next(
        row
        for row in gap["evidence"]
        if row.get("outcome_event_id") == authoritative_id
    )
    events = _read_events(repo)
    outcome = next(
        event for event in events if event.get("event_id") == authoritative_id
    )
    state = _reduce(events)
    action = _action_by_id(state, action_id)
    if action is None or action.get("status") != "pending":
        raise ReviewLoopError("morning_schedule_action_not_pending")
    observed_day = _parse_date(
        str(evidence["effective_observed_date"]), "effective observed date"
    )
    origin_day = _parse_date(str(action["origin_date"]), "origin_date")
    if evidence.get("stable_retention") is True:
        next_stage = _next_stage(str(action.get("stage") or ""), 0)
        due = origin_day + dt.timedelta(days=STAGE_DAYS[next_stage])
        if due <= observed_day:
            due = observed_day + dt.timedelta(days=max(1, STAGE_DAYS[next_stage]))
        reason = "append-only recovery of cross-day unhinted independent retrieval"
        reconciliation_kind = "retention"
    else:
        next_stage = "D1_rescue"
        due = observed_day + dt.timedelta(days=1)
        reason = "append-only recovery of an unstable first response"
        reconciliation_kind = "short_retest"
    idempotency_key = (
        f"scheduling-reconciliation:{action_id}:{authoritative_id}:v1"
    )
    reconciliation_event_id = content_id("RSR", idempotency_key)
    generated = _make_action(
        action_type="review",
        identity=str(action["identity"]),
        source_id=str(action["source_id"]),
        due_date=due,
        stage=next_stage,
        trigger_event_id=authoritative_id,
        origin_date=origin_day,
        reason=reason,
        first_break=str(outcome.get("first_break") or "not_observed"),
        formal_node_id=action.get("formal_node_id"),
        review_unit_id=action.get("review_unit_id"),
    )
    material = {
        "processing_contract": SCHEDULING_RECONCILIATION_CONTRACT,
        "trigger_action_id": action_id,
        "action_sha256": _sha256_value(action),
        "authoritative_outcome_event_id": authoritative_id,
        "authoritative_outcome_sha256": _sha256_value(outcome),
        "session_state_sha256": evidence["session_state_sha256"],
        "queue_sha256": evidence["queue_sha256"],
        "prepared_pack_sha256": evidence["prepared_pack_sha256"],
        "pre_state_sha256": _sha256_value(state),
        "superseded_outcome_event_ids": gap["superseded_outcome_event_ids"],
        "generated_action": generated,
    }
    return {
        "schema": LEDGER_SCHEMA,
        "event_kind": "scheduling_reconciliation",
        "event_id": reconciliation_event_id,
        "idempotency_key": idempotency_key,
        "request_hash": _sha256_value(material),
        "event_time": utc_now(),
        "observed_date": evidence["effective_observed_date"],
        "source": "morning_review",
        "session_id": outcome.get("session_id"),
        "item_id": outcome.get("item_id"),
        "source_id": action.get("source_id"),
        "mechanism_key": outcome.get("mechanism_key"),
        "formal_node_id": action.get("formal_node_id"),
        "review_unit_id": action.get("review_unit_id"),
        "identity": action.get("identity"),
        "processing_contract": SCHEDULING_RECONCILIATION_CONTRACT,
        "action_sha256": material["action_sha256"],
        "authoritative_outcome_event_id": authoritative_id,
        "authoritative_outcome_sha256": material[
            "authoritative_outcome_sha256"
        ],
        "session_state_sha256": material["session_state_sha256"],
        "queue_sha256": material["queue_sha256"],
        "prepared_pack_sha256": material["prepared_pack_sha256"],
        "pre_state_sha256": material["pre_state_sha256"],
        "superseded_outcome_event_ids": gap["superseded_outcome_event_ids"],
        "reconciliation_kind": reconciliation_kind,
        "cross_day": evidence["cross_day"],
        "stable_retention": evidence["stable_retention"],
        "evidence_kind": reconciliation_kind,
        "trigger_action_id": action_id,
        "generated_actions": [generated],
        "formal_write_authorized": False,
        "formal_write_count": 0,
        "capture_write_count": 0,
        "luna_call_count": 0,
    }


def _ensure_review_hot_state_after_scheduling_reconciliation_locked(
    repo: Path, *, force_rebuild: bool
) -> dict[str, Any]:
    try:
        import review_hot_state_408 as review_hot
    except ImportError as exc:
        raise ReviewLoopError(
            "scheduling reconciliation committed but review hot-state is unavailable"
        ) from exc
    initial = review_hot.readiness(repo)
    rebuild = None
    try:
        if force_rebuild or initial.get("status") != "ready":
            rebuild = review_hot.cold_rebuild(repo)
        final = review_hot.readiness(repo)
    except (review_hot.HotStateError, OSError, KeyError) as exc:
        raise ReviewLoopError(
            "scheduling reconciliation committed but hot-state rebuild failed; "
            "rerun the same apply command"
        ) from exc
    if final.get("status") != "ready":
        raise ReviewLoopError(
            "scheduling reconciliation committed but hot-state is not ready"
        )
    return {
        "initial_status": initial.get("status"),
        "rebuild_performed": rebuild is not None,
        "rebuild": rebuild,
        "readiness": final,
    }


def _write_scheduling_reconciliation_receipt(
    repo: Path,
    *,
    event: dict[str, Any],
    state: dict[str, Any],
    hot_state: dict[str, Any],
    session_dispositions: list[dict[str, Any]],
) -> dict[str, str]:
    completed = _action_by_id(state, str(event["trigger_action_id"]))
    if (
        completed is None
        or completed.get("status") != "completed"
        or completed.get("completed_by_event_id")
        != event.get("authoritative_outcome_event_id")
    ):
        raise ReviewLoopError(
            "scheduling reconciliation receipt lacks completed action binding"
        )
    state_actions = {
        str(action.get("action_id")): action for action in state.get("actions") or []
    }
    for generated in event.get("generated_actions") or []:
        committed = state_actions.get(str(generated.get("action_id") or ""))
        if committed is None or committed.get("dedupe_key") != generated.get(
            "dedupe_key"
        ):
            raise ReviewLoopError(
                "scheduling reconciliation receipt lacks generated action binding"
            )
    payload = {
        "schema": "review_scheduling_reconciliation_receipt_v1",
        "event_id": event["event_id"],
        "event_sha256": _sha256_value(event),
        "trigger_action_id": event["trigger_action_id"],
        "authoritative_outcome_event_id": event[
            "authoritative_outcome_event_id"
        ],
        "pre_state_sha256": event["pre_state_sha256"],
        "completed_action_sha256": _sha256_value(completed),
        "generated_action_sha256s": [
            _sha256_value(action) for action in event.get("generated_actions") or []
        ],
        "session_dispositions": [
            {
                "session_id": row["session_id"],
                "item_id": row["item_id"],
                "action_id": row["action_id"],
                "disposition_sha256": row["disposition_sha256"],
                "session_commit_receipt_sha256": row.get(
                    "session_commit_receipt_sha256"
                ),
            }
            for row in session_dispositions
        ],
        "runtime_projection_published": True,
        "review_hot_state_status": hot_state["readiness"]["status"],
        "processing_contract": SCHEDULING_RECONCILIATION_CONTRACT,
        "formal_write_count": 0,
        "capture_write_count": 0,
        "luna_call_count": 0,
    }
    raw = json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    digest = hashlib.sha256(raw.encode("utf-8")).hexdigest()
    path = repo / SCHEDULING_RECEIPT_REL / f"{digest}.json"
    if path.exists():
        if path.read_text(encoding="utf-8") != raw:
            raise ReviewLoopError("scheduling reconciliation receipt hash conflict")
    else:
        atomic_write(path, raw)
    return {
        "receipt_ref": path.relative_to(repo).as_posix(),
        "receipt_sha256": digest,
    }


def _scheduling_session_disposition_targets(
    repo: Path, action_id: str
) -> list[dict[str, str]]:
    sessions, errors = _session_states(repo)
    if errors:
        raise ReviewLoopError(
            "scheduling_session_state_incomplete:" + ";".join(errors[:20])
        )
    targets: list[dict[str, str]] = []
    for session in sessions:
        for item_id, item in (session.get("items") or {}).items():
            if not isinstance(item, dict) or item.get("action_id") != action_id:
                continue
            if item.get("first") is not None:
                continue
            resolution = str(item.get("resolution") or "")
            if resolution not in {"pending", "scheduling_superseded"}:
                raise ReviewLoopError(
                    "scheduling_session_item_conflict:"
                    f"{session.get('session_id')}:{item_id}:{resolution}"
                )
            if session.get("status") not in {"in_progress", "paused"}:
                raise ReviewLoopError(
                    "scheduling_session_item_not_active:"
                    f"{session.get('session_id')}:{item_id}:{session.get('status')}"
                )
            targets.append(
                {
                    "session_id": str(session["session_id"]),
                    "item_id": str(item_id),
                }
            )
    return sorted(targets, key=lambda row: (row["session_id"], row["item_id"]))


def _apply_scheduling_session_dispositions(
    repo: Path, event: dict[str, Any]
) -> list[dict[str, Any]]:
    try:
        import morning_review_session as morning_session
    except ImportError as exc:
        raise ReviewLoopError(
            "scheduling reconciliation committed but session disposition is unavailable"
        ) from exc
    results: list[dict[str, Any]] = []
    for target in _scheduling_session_disposition_targets(
        repo, str(event["trigger_action_id"])
    ):
        try:
            result = morning_session.mark_scheduling_superseded(
                repo,
                session_id=target["session_id"],
                item_id=target["item_id"],
                action_id=str(event["trigger_action_id"]),
                action_sha256=str(event["action_sha256"]),
                authoritative_outcome_event_id=str(
                    event["authoritative_outcome_event_id"]
                ),
                reconciliation_event_id=str(event["event_id"]),
                processing_contract=SCHEDULING_RECONCILIATION_CONTRACT,
            )
        except (morning_session.SessionError, OSError, KeyError) as exc:
            raise ReviewLoopError(
                "scheduling reconciliation committed but session disposition failed; "
                "rerun the same apply command"
            ) from exc
        results.append(result)
    return results


def _apply_one_scheduling_reconciliation(
    repo: Path, action_id: str
) -> dict[str, Any]:
    repo = _resolve_repo(repo)
    with runtime_lock(repo):
        events = _read_events(repo)
        existing = next(
            (
                event
                for event in events
                if event.get("event_kind") == "scheduling_reconciliation"
                and event.get("trigger_action_id") == action_id
            ),
            None,
        )
        created = existing is None
        if created:
            event = _build_scheduling_reconciliation_event(repo, action_id)
            append_jsonl(_loop_root(repo) / "events.jsonl", event)
            events.append(event)
            state = _reduce(events)
            atomic_write_json(_loop_root(repo) / "state.json", state)
        else:
            event = existing
            state = _reduce(events)
            atomic_write_json(_loop_root(repo) / "state.json", state)
        hot_state = _ensure_review_hot_state_after_scheduling_reconciliation_locked(
            repo, force_rebuild=created
        )
        runtime = _write_runtime_views_locked(repo)
    session_dispositions = _apply_scheduling_session_dispositions(repo, event)
    if session_dispositions:
        with runtime_lock(repo):
            runtime = _write_runtime_views_locked(repo)
    receipt = _write_scheduling_reconciliation_receipt(
        repo,
        event=event,
        state=state,
        hot_state=hot_state,
        session_dispositions=session_dispositions,
    )
    return {
        "status": "RECONCILED" if created else "ALREADY_RECONCILED",
        "created": created,
        "event": event,
        "state": state,
        "runtime": runtime,
        "review_hot_state": hot_state,
        "session_dispositions": session_dispositions,
        **receipt,
        "formal_write_count": 0,
        "capture_write_count": 0,
        "luna_call_count": 0,
    }


def reconcile_scheduling_drift(
    repo: Path,
    *,
    action_ids: list[str] | None = None,
    all_eligible: bool = False,
    apply: bool = False,
) -> dict[str, Any]:
    repo = _resolve_repo(repo)
    requested = [
        str(_safe_id(action_id, "action_id", required=True))
        for action_id in (action_ids or [])
    ]
    if bool(requested) == bool(all_eligible):
        raise ReviewLoopError(
            "choose explicit action_id values or --all-eligible"
        )
    audit_report = scheduling_drift_audit(repo)
    gaps = {str(row["action_id"]): row for row in audit_report["gaps"]}
    selected = (
        sorted(
            action_id
            for action_id, row in gaps.items()
            if row.get("status") == "eligible"
        )
        if all_eligible
        else requested
    )
    results: list[dict[str, Any]] = []
    for action_id in selected:
        gap = gaps.get(action_id)
        if gap is None:
            raise ReviewLoopError(f"unknown answered pending action: {action_id}")
        if gap.get("status") == "reconciled":
            if apply:
                applied = _apply_one_scheduling_reconciliation(repo, action_id)
                results.append(
                    {
                        "action_id": action_id,
                        "status": applied["status"],
                        "event_id": applied["event"]["event_id"],
                        "receipt_sha256": applied["receipt_sha256"],
                        "session_dispositions": applied[
                            "session_dispositions"
                        ],
                    }
                )
            else:
                results.append(
                    {
                        "action_id": action_id,
                        "status": "ALREADY_RECONCILED",
                        "event_id": gap.get("existing_reconciliation_event_id"),
                    }
                )
            continue
        if gap.get("status") != "eligible":
            results.append(
                {
                    "action_id": action_id,
                    "status": "SCHEDULING_EVIDENCE_INCOMPLETE",
                    "error_code": "scheduling_evidence_incomplete",
                    "evidence": gap.get("evidence"),
                }
            )
            continue
        if apply:
            applied = _apply_one_scheduling_reconciliation(repo, action_id)
            event = applied["event"]
            results.append(
                {
                    "action_id": action_id,
                    "status": applied["status"],
                    "event_id": event["event_id"],
                    "authoritative_outcome_event_id": event[
                        "authoritative_outcome_event_id"
                    ],
                    "superseded_outcome_event_ids": event[
                        "superseded_outcome_event_ids"
                    ],
                    "generated_actions": event["generated_actions"],
                    "receipt_ref": applied["receipt_ref"],
                    "receipt_sha256": applied["receipt_sha256"],
                    "review_hot_state_status": applied["review_hot_state"][
                        "readiness"
                    ]["status"],
                    "session_dispositions": applied["session_dispositions"],
                }
            )
        else:
            preview = _build_scheduling_reconciliation_event(repo, action_id)
            results.append(
                {
                    "action_id": action_id,
                    "status": "PREVIEW",
                    "event_id": preview["event_id"],
                    "authoritative_outcome_event_id": preview[
                        "authoritative_outcome_event_id"
                    ],
                    "superseded_outcome_event_ids": preview[
                        "superseded_outcome_event_ids"
                    ],
                    "stable_retention": preview["stable_retention"],
                    "generated_actions": preview["generated_actions"],
                }
            )
    return {
        "schema": "review_scheduling_reconciliation_command_v1",
        "mode": "apply" if apply else "preview",
        "selected_count": len(selected),
        "created_count": sum(row.get("status") == "RECONCILED" for row in results),
        "results": results,
        "read_only": not apply,
        "formal_write_count": 0,
        "capture_write_count": 0,
        "luna_call_count": 0,
    }


def date_drift_audit(repo: Path) -> dict[str, Any]:
    """Report legacy cross-day rows whose embedded actions were not re-dated."""

    repo = _resolve_repo(repo)
    events = _read_events(repo)
    state = _reduce(events)
    capture_by_event: dict[str, dict[str, Any]] = {}
    capture_projection_error: str | None = None
    try:
        import intake_fact_capture_408 as capture_model

        capture_state = capture_model.status(None, repo_root=repo)
        for capture_id, capture in (capture_state.get("captures") or {}).items():
            if not isinstance(capture, dict):
                continue
            review_event_id = _capture_review_event_id(capture)
            if review_event_id:
                capture_by_event[review_event_id] = {
                    "capture_id": str(capture_id),
                    "quality_status": capture.get("quality_status"),
                }
    except (ImportError, OSError, ValueError, RuntimeError) as exc:
        capture_projection_error = str(exc)
    pending_by_trigger: dict[str, list[dict[str, Any]]] = {}
    for action in state.get("pending_actions") or []:
        trigger_ids = action.get("trigger_event_ids") or [
            action.get("trigger_event_id")
        ]
        for trigger_id in trigger_ids:
            if trigger_id:
                pending_by_trigger.setdefault(str(trigger_id), []).append(action)
    session_cache: dict[str, list[dict[str, Any]]] = {}
    drifts: list[dict[str, Any]] = []
    errors: list[dict[str, str]] = []
    for event in events:
        if (
            event.get("event_kind") != "outcome"
            or event.get("source") != "morning_review"
        ):
            continue
        try:
            effective, basis, session_date = effective_observed_date(
                repo, event, session_cache=session_cache
            )
        except ReviewLoopError as exc:
            errors.append(
                {
                    "event_id": str(event.get("event_id") or ""),
                    "reason": str(exc),
                }
            )
            continue
        stored = str(event.get("observed_date") or "")
        if stored == effective:
            continue
        pending = sorted(
            (
                {
                    "action_id": action.get("action_id"),
                    "action_type": action.get("action_type"),
                    "origin_date": action.get("origin_date"),
                    "due_date": action.get("due_date"),
                }
                for action in pending_by_trigger.get(str(event["event_id"]), [])
            ),
            key=lambda action: (str(action["due_date"]), str(action["action_id"])),
        )
        capture = capture_by_event.get(str(event["event_id"]))
        curation_locked = bool(
            capture is not None
            and capture.get("quality_status") in {"curated", "already_current"}
        )
        drifts.append(
            {
                "event_id": event["event_id"],
                "session_id": event.get("session_id"),
                "item_id": event.get("item_id"),
                "stored_observed_date": stored,
                "effective_observed_date": effective,
                "session_review_date": session_date,
                "date_basis": basis,
                "verification_status": "VERIFIED",
                "pending_actions": pending,
                "affects_future_schedule": bool(pending),
                "capture": capture,
                "formal_curation_locked": curation_locked,
                "correction_status": (
                    "audited_formally_curated_no_change"
                    if pending and curation_locked
                    else "needs_append_only_correction"
                    if pending and capture_projection_error is None
                    else "blocked_capture_projection"
                    if pending
                    else "no_pending_action"
                ),
            }
        )
    affecting = [row for row in drifts if row["affects_future_schedule"]]
    eligible = [
        row
        for row in drifts
        if row["correction_status"] == "needs_append_only_correction"
    ]
    if capture_projection_error is not None:
        errors.append(
            {
                "event_id": "capture-ledger",
                "reason": f"capture projection unavailable: {capture_projection_error}",
            }
        )
    return {
        "schema": "review_date_drift_audit_v1",
        "status": (
            "INCOMPLETE"
            if errors
            else "DRIFT_FOUND"
            if eligible
            else "PASS"
        ),
        "legacy_cross_day_event_count": len(drifts),
        "future_schedule_drift_count": len(affecting),
        "correction_eligible_count": len(eligible),
        "drifts": drifts,
        "projection_errors": errors,
        "read_only": True,
        "formal_write_count": 0,
    }


def _build_date_correction_event(
    repo: Path,
    source_event_id: str,
) -> dict[str, Any]:
    audit_report = date_drift_audit(repo)
    drift = next(
        (
            row
            for row in audit_report["drifts"]
            if row.get("event_id") == source_event_id
        ),
        None,
    )
    if drift is None:
        raise ReviewLoopError(
            f"event is not a verified legacy date drift: {source_event_id}"
        )
    if drift.get("verification_status") != "VERIFIED":
        raise ReviewLoopError(f"date drift is not verified: {source_event_id}")
    if drift.get("formal_curation_locked") is True:
        raise ReviewLoopError(
            f"date drift belongs to a formally curated capture: {source_event_id}"
        )
    if drift.get("correction_status") != "needs_append_only_correction":
        raise ReviewLoopError(
            f"date drift has no eligible pending action: {source_event_id}"
        )
    events = _read_events(repo)
    existing = next(
        (
            event
            for event in events
            if event.get("event_kind") == "date_correction"
            and event.get("source_event_id") == source_event_id
        ),
        None,
    )
    if existing is not None:
        return existing
    source_event = next(
        (
            event
            for event in events
            if event.get("event_id") == source_event_id
            and event.get("event_kind") == "outcome"
        ),
        None,
    )
    if source_event is None:
        raise ReviewLoopError(f"source outcome is missing: {source_event_id}")
    state = _reduce(events)
    action_by_id = {
        str(action["action_id"]): action for action in state.get("actions") or []
    }
    stored_day = _parse_date(
        str(drift["stored_observed_date"]), "stored_observed_date"
    )
    effective_day = _parse_date(
        str(drift["effective_observed_date"]), "effective_observed_date"
    )
    shift = effective_day - stored_day
    if shift.days <= 0:
        raise ReviewLoopError("date correction only supports later actual dates")
    idempotency_key = (
        f"date-correction:{source_event_id}:{effective_day.isoformat()}:v1"
    )
    correction_event_id = content_id("RDC", idempotency_key)
    closed_action_ids: list[str] = []
    detached_action_ids: list[str] = []
    replacements: list[dict[str, Any]] = []
    replacement_map: list[dict[str, str]] = []
    for summary in drift.get("pending_actions") or []:
        old_action_id = str(summary.get("action_id") or "")
        action = action_by_id.get(old_action_id)
        if action is None or action.get("status") != "pending":
            raise ReviewLoopError(
                f"date correction action is no longer pending: {old_action_id}"
            )
        trigger_ids = list(
            action.get("trigger_event_ids") or [action.get("trigger_event_id")]
        )
        if source_event_id not in trigger_ids:
            raise ReviewLoopError(
                f"date correction action lost its source trigger: {old_action_id}"
            )
        if len(trigger_ids) == 1:
            closed_action_ids.append(old_action_id)
        else:
            detached_action_ids.append(old_action_id)
        old_due = _parse_date(str(action.get("due_date") or ""), "action due_date")
        old_origin = _parse_date(
            str(action.get("origin_date") or ""), "action origin_date"
        )
        new_due = old_due + shift
        new_origin = effective_day if old_origin == stored_day else old_origin
        replacement = _make_action(
            action_type=str(action.get("action_type") or ""),
            identity=str(action.get("identity") or ""),
            source_id=str(action.get("source_id") or ""),
            due_date=new_due,
            stage=str(action.get("stage") or ""),
            trigger_event_id=correction_event_id,
            origin_date=new_origin,
            reason=(
                f"append-only actual-date correction for {source_event_id}; "
                + str(action.get("reason") or "")
            ),
            first_break=str(action.get("first_break") or "none"),
            formal_node_id=action.get("formal_node_id"),
            review_unit_id=action.get("review_unit_id"),
        )
        replacements.append(replacement)
        replacement_map.append(
            {
                "old_action_id": old_action_id,
                "replacement_action_id": str(replacement["action_id"]),
                "old_due_date": old_due.isoformat(),
                "new_due_date": new_due.isoformat(),
            }
        )
    material = {
        "source_event_id": source_event_id,
        "stored_observed_date": stored_day.isoformat(),
        "effective_observed_date": effective_day.isoformat(),
        "session_review_date": drift.get("session_review_date"),
        "verification_basis": drift.get("date_basis"),
        "closed_action_ids": sorted(closed_action_ids),
        "detached_action_ids": sorted(detached_action_ids),
        "replacement_actions": replacements,
        "replacement_map": replacement_map,
        "formal_curation_locked": False,
    }
    return {
        "schema": LEDGER_SCHEMA,
        "event_kind": "date_correction",
        "event_id": correction_event_id,
        "idempotency_key": idempotency_key,
        "request_hash": hashlib.sha256(
            canonical_json(material).encode("utf-8")
        ).hexdigest(),
        "event_time": utc_now(),
        "observed_date": effective_day.isoformat(),
        "source": "morning_review",
        "session_id": source_event.get("session_id"),
        "item_id": source_event.get("item_id"),
        **material,
        "formal_write_authorized": False,
    }


def _ensure_review_hot_state_after_date_correction_locked(
    repo: Path, *, force_rebuild: bool
) -> dict[str, Any]:
    """Make the canonical-ledger hot projection usable before releasing the lock.

    A date correction changes both the ledger commitment and projected actions.
    Leaving the old hot manifest active would make the next answer fail closed.
    The initial correction therefore always cold-rebuilds; an idempotent retry
    rebuilds only when readiness shows that an earlier attempt stopped midway.
    """

    try:
        import review_hot_state_408 as review_hot
    except ImportError as exc:
        raise ReviewLoopError(
            "date correction is committed but review hot-state is unavailable; "
            "rerun the same correction apply command after restoring the module"
        ) from exc
    initial = review_hot.readiness(repo)
    rebuild: dict[str, Any] | None = None
    try:
        if force_rebuild or initial.get("status") != "ready":
            rebuild = review_hot.cold_rebuild(repo)
        final = review_hot.readiness(repo)
    except (review_hot.HotStateError, OSError, KeyError) as exc:
        raise ReviewLoopError(
            "date correction is committed but review hot-state rebuild failed; "
            "rerun the same correction apply command"
        ) from exc
    if final.get("status") != "ready":
        raise ReviewLoopError(
            "date correction is committed but review hot-state verification "
            "failed: "
            + str(final.get("reason_code") or "unknown_reason")
            + "; rerun the same correction apply command"
        )
    return {
        "initial_status": initial.get("status"),
        "rebuild_performed": rebuild is not None,
        "rebuild": rebuild,
        "readiness": final,
    }


def _apply_one_date_correction(repo: Path, source_event_id: str) -> dict[str, Any]:
    repo = _resolve_repo(repo)
    with runtime_lock(repo):
        events = _read_events(repo)
        existing = next(
            (
                event
                for event in events
                if event.get("event_kind") == "date_correction"
                and event.get("source_event_id") == source_event_id
            ),
            None,
        )
        created = existing is None
        if created:
            event = _build_date_correction_event(repo, source_event_id)
            # Re-read under the same lock above; the builder is read-only and
            # the lock prevents review-ledger/action drift before append.
            append_jsonl(_loop_root(repo) / "events.jsonl", event)
            events.append(event)
            state = _reduce(events)
            atomic_write_json(_loop_root(repo) / "state.json", state)
        else:
            event = existing
        hot_state = _ensure_review_hot_state_after_date_correction_locked(
            repo, force_rebuild=created
        )
        runtime = _write_runtime_views_locked(repo)
    return {
        "status": "DATE_CORRECTED" if created else "ALREADY_CORRECTED",
        "created": created,
        "event": event,
        "runtime": runtime,
        "review_hot_state": hot_state,
        "formal_write_count": 0,
    }


def correct_date_drift(
    repo: Path,
    *,
    event_ids: list[str] | None = None,
    all_verified_pending: bool = False,
    apply: bool = False,
) -> dict[str, Any]:
    """Preview by default; append corrections only after explicit selection."""

    repo = _resolve_repo(repo)
    requested = [
        str(_safe_id(event_id, "event_id", required=True))
        for event_id in (event_ids or [])
    ]
    if bool(requested) == bool(all_verified_pending):
        raise ReviewLoopError(
            "choose explicit event_id values or --all-verified-pending"
        )
    events = _read_events(repo)
    existing_by_source = {
        str(event.get("source_event_id")): event
        for event in events
        if event.get("event_kind") == "date_correction"
    }
    audit_report = date_drift_audit(repo)
    drifts = {
        str(row["event_id"]): row for row in audit_report.get("drifts") or []
    }
    selected_ids = (
        sorted(
            event_id
            for event_id, row in drifts.items()
            if row.get("correction_status") == "needs_append_only_correction"
        )
        if all_verified_pending
        else requested
    )
    results: list[dict[str, Any]] = []
    for event_id in selected_ids:
        if event_id in existing_by_source:
            if apply:
                applied = _apply_one_date_correction(repo, event_id)
                results.append(
                    {
                        "event_id": event_id,
                        "status": applied["status"],
                        "created": applied["created"],
                        "correction_event_id": applied["event"]["event_id"],
                        "review_hot_rebuild_performed": applied[
                            "review_hot_state"
                        ]["rebuild_performed"],
                        "review_hot_state_status": applied["review_hot_state"][
                            "readiness"
                        ]["status"],
                    }
                )
            else:
                results.append(
                    {
                        "event_id": event_id,
                        "status": "ALREADY_CORRECTED",
                        "correction_event_id": existing_by_source[event_id].get(
                            "event_id"
                        ),
                    }
                )
            continue
        drift = drifts.get(event_id)
        if drift is None:
            raise ReviewLoopError(f"unknown verified date drift: {event_id}")
        if drift.get("formal_curation_locked") is True:
            results.append(
                {
                    "event_id": event_id,
                    "status": "AUDIT_ONLY_FORMALLY_CURATED",
                    "capture": drift.get("capture"),
                }
            )
            continue
        if drift.get("correction_status") != "needs_append_only_correction":
            results.append(
                {
                    "event_id": event_id,
                    "status": "NO_ELIGIBLE_PENDING_ACTION",
                }
            )
            continue
        if apply:
            applied = _apply_one_date_correction(repo, event_id)
            results.append(
                {
                    "event_id": event_id,
                    "status": applied["status"],
                    "created": applied["created"],
                    "correction_event_id": applied["event"]["event_id"],
                    "closed_action_ids": applied["event"]["closed_action_ids"],
                    "detached_action_ids": applied["event"][
                        "detached_action_ids"
                    ],
                    "replacement_map": applied["event"]["replacement_map"],
                    "review_hot_rebuild_performed": applied[
                        "review_hot_state"
                    ]["rebuild_performed"],
                    "review_hot_state_status": applied["review_hot_state"][
                        "readiness"
                    ]["status"],
                }
            )
        else:
            preview = _build_date_correction_event(repo, event_id)
            results.append(
                {
                    "event_id": event_id,
                    "status": "PREVIEW",
                    "correction_event_id": preview["event_id"],
                    "closed_action_ids": preview["closed_action_ids"],
                    "detached_action_ids": preview["detached_action_ids"],
                    "replacement_map": preview["replacement_map"],
                }
            )
    return {
        "schema": "review_date_correction_command_v1",
        "mode": "apply" if apply else "preview",
        "selected_count": len(selected_ids),
        "created_count": sum(row.get("created") is True for row in results),
        "results": results,
        "read_only": not apply,
        "formal_write_count": 0,
    }


def _compact_outcome_receipt(value: dict[str, Any]) -> dict[str, Any]:
    event = value["event"]
    route = event.get("route") or {}
    candidate = route.get("candidate") or {}
    runtime = value.get("runtime") or {}
    return {
        "action": "record-outcome",
        "created": bool(value.get("created")),
        "event_id": event.get("event_id"),
        "session_id": event.get("session_id"),
        "item_id": event.get("item_id"),
        "first_result": event.get("first_result"),
        "choice_result": event.get("choice_result", "not_applicable"),
        "reasoning_result": event.get("reasoning_result", "not_observed"),
        "confidence": event.get("confidence"),
        "evidence_kind": event.get("evidence_kind"),
        "route_kind": route.get("kind"),
        "candidate_id": candidate.get("candidate_id"),
        "generated_action_ids": [
            item.get("action_id") for item in event.get("generated_actions", [])
        ],
        "formal_write_authorized": bool(event.get("formal_write_authorized")),
        "runtime": {
            "status": runtime.get("status"),
            "active_session_id": runtime.get("active_session_id"),
            "event_count": runtime.get("event_count"),
        },
    }


def _add_repo(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--repo", default=str(ROOT))


def _cross_day_arg(value: str) -> bool | None:
    normalized = value.lower()
    if normalized in {"yes", "true", "1"}:
        return True
    if normalized in {"no", "false", "0"}:
        return False
    if normalized == "unknown":
        return None
    raise argparse.ArgumentTypeError("cross-day must be yes, no, or unknown")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)

    outcome = sub.add_parser("record-outcome", help="Persist one first answer and route it")
    _add_repo(outcome)
    outcome.add_argument("--source", choices=sorted(SOURCES), required=True)
    outcome.add_argument("--session-id", required=True)
    outcome.add_argument("--item-id", required=True)
    outcome.add_argument("--source-id", required=True)
    outcome.add_argument("--mechanism", required=True)
    outcome.add_argument("--knowledge-point", default="")
    outcome.add_argument("--first-result", choices=sorted(FIRST_RESULTS), required=True)
    outcome.add_argument(
        "--choice-result", choices=sorted(CHOICE_RESULTS), default="not_applicable"
    )
    outcome.add_argument(
        "--reasoning-result", choices=sorted(REASONING_RESULTS), default="not_observed"
    )
    outcome.add_argument("--confidence", choices=sorted(CONFIDENCE_LEVELS), required=True)
    outcome.add_argument("--prompt-level", choices=sorted(PROMPT_LEVELS), required=True)
    outcome.add_argument("--first-break", required=True)
    outcome.add_argument(
        "--first-break-provenance",
        choices=sorted(FIRST_BREAK_PROVENANCES),
        default="derived_classification",
    )
    outcome.add_argument("--correction-result", choices=sorted(CORRECTION_RESULTS), default="not_attempted")
    outcome.add_argument("--event-time", required=True)
    outcome.add_argument("--observed-date", required=True)
    outcome.add_argument("--session-review-date")
    outcome.add_argument("--idempotency-key", required=True)
    outcome.add_argument("--response-seconds", type=float)
    outcome.add_argument("--latency-state", choices=sorted(LATENCY_STATES), default="unmeasured")
    outcome.add_argument("--cross-day", type=_cross_day_arg, default=None)
    outcome.add_argument("--question-valid", action="store_true")
    outcome.add_argument("--formal-node-id")
    outcome.add_argument("--review-unit-id")
    outcome.add_argument("--trigger-action-id")
    outcome.add_argument("--full", action="store_true", help="Print the full event/state projection")

    repair = sub.add_parser("record-repair", help="Persist repair evidence without retention")
    _add_repo(repair)
    repair.add_argument("--source", choices=sorted(SOURCES), default="morning_review")
    repair.add_argument("--session-id", required=True)
    repair.add_argument("--item-id", required=True)
    repair.add_argument("--source-id", required=True)
    repair.add_argument("--result", choices=sorted(CORRECTION_RESULTS - {"not_attempted"}), required=True)
    repair.add_argument("--confidence", choices=sorted(CONFIDENCE_LEVELS))
    repair.add_argument("--prompt-level", choices=sorted(PROMPT_LEVELS))
    repair.add_argument("--event-time", required=True)
    repair.add_argument("--observed-date", required=True)
    repair.add_argument("--session-review-date")
    repair.add_argument("--idempotency-key", required=True)
    repair.add_argument("--attempt", type=int, required=True)
    repair.add_argument("--response-seconds", type=float)

    sync = sub.add_parser("sync-session", help="Idempotently publish one v1/v3 morning session")
    _add_repo(sync)
    sync.add_argument("--session", required=True)

    migrate = sub.add_parser("migrate-sessions", help="Import existing sessions without rewriting them")
    _add_repo(migrate)

    status = sub.add_parser("status", help="Read the canonical review-loop state")
    _add_repo(status)
    status.add_argument("--full", action="store_true", help="Include the complete derived projection")

    daily = sub.add_parser(
        "daily-summary",
        help="Read all review outcomes for one actual Asia/Shanghai study date",
    )
    _add_repo(daily)
    daily.add_argument("--date", required=True)

    drift = sub.add_parser(
        "date-drift-audit",
        help="Read legacy morning date drift and pending affected actions",
    )
    _add_repo(drift)

    correct_drift = sub.add_parser(
        "correct-date-drift",
        help="Preview or append verified legacy morning date corrections",
    )
    _add_repo(correct_drift)
    selection = correct_drift.add_mutually_exclusive_group(required=True)
    selection.add_argument("--event-id", action="append", dest="event_ids")
    selection.add_argument("--all-verified-pending", action="store_true")
    correct_drift.add_argument(
        "--apply",
        action="store_true",
        help="Append corrections; without this flag the command is read-only",
    )

    scheduling = sub.add_parser(
        "reconcile-scheduling",
        help="Preview or append recovery for answered morning actions left pending",
    )
    _add_repo(scheduling)
    scheduling_selection = scheduling.add_mutually_exclusive_group(required=True)
    scheduling_selection.add_argument(
        "--action-id", action="append", dest="action_ids"
    )
    scheduling_selection.add_argument("--all-eligible", action="store_true")
    scheduling.add_argument(
        "--apply",
        action="store_true",
        help="Append reconciliation events; without this flag the command is read-only",
    )

    reconcile = sub.add_parser("reconcile", help="Refresh pointer and dashboard from session truth")
    _add_repo(reconcile)

    check = sub.add_parser("audit", help="Audit ledger, actions, sessions, pointer, and dashboard")
    _add_repo(check)

    confirm = sub.add_parser("confirm-candidate", help="Record explicit user confirmation for intake handoff")
    _add_repo(confirm)
    confirm.add_argument("--candidate-id", required=True)
    confirm.add_argument("--formal-node-id")
    confirm.add_argument("--idempotency-key", required=True)
    confirm.add_argument("--user-confirmed", action="store_true", required=True)

    close_candidate = sub.add_parser(
        "close-candidate",
        help="Close an unconfirmed candidate as derived evidence only",
    )
    _add_repo(close_candidate)
    close_candidate.add_argument("--candidate-id", required=True)
    close_candidate.add_argument("--idempotency-key", required=True)
    close_candidate.add_argument("--evidence-only", action="store_true", required=True)
    close_candidate.add_argument("--user-confirmed", action="store_true", required=True)
    close_candidate.add_argument("--cancel-generated-actions", action="store_true")

    dismiss_item = sub.add_parser(
        "dismiss-item",
        help="Dismiss a duplicate or user-removed review item without grading it",
    )
    _add_repo(dismiss_item)
    dismiss_item.add_argument("--session-id", required=True)
    dismiss_item.add_argument("--item-id", required=True)
    dismiss_item.add_argument("--source-id", required=True)
    dismiss_item.add_argument("--trigger-action-id", required=True)
    dismiss_item.add_argument("--reason", choices=["duplicate", "redundant", "user_removed"], required=True)
    dismiss_item.add_argument("--idempotency-key", required=True)
    dismiss_item.add_argument("--user-confirmed", action="store_true", required=True)

    retire_equivalent = sub.add_parser(
        "retire-closed-equivalent-item",
        help=(
            "Retire an unanswered session item whose action was already closed "
            "by a formal equivalence binding"
        ),
    )
    _add_repo(retire_equivalent)
    retire_equivalent.add_argument("--session-id", required=True)
    retire_equivalent.add_argument("--item-id", required=True)
    retire_equivalent.add_argument("--source-id", required=True)
    retire_equivalent.add_argument("--trigger-action-id", required=True)
    retire_equivalent.add_argument("--formal-binding-event-id", required=True)
    retire_equivalent.add_argument(
        "--reason", choices=["user_removed"], required=True
    )
    retire_equivalent.add_argument("--idempotency-key", required=True)
    retire_equivalent.add_argument(
        "--user-confirmed", action="store_true", required=True
    )
    return parser


def main(argv: list[str]) -> int:
    args = build_parser().parse_args(argv)
    try:
        repo = _resolve_repo(args.repo)
        if args.command == "record-outcome":
            value = record_outcome(
                repo,
                source=args.source,
                session_id=args.session_id,
                item_id=args.item_id,
                source_id=args.source_id,
                mechanism_key=args.mechanism,
                knowledge_point=args.knowledge_point,
                first_result=args.first_result,
                choice_result=args.choice_result,
                reasoning_result=args.reasoning_result,
                confidence=args.confidence,
                prompt_level=args.prompt_level,
                first_break=args.first_break,
                first_break_provenance=args.first_break_provenance,
                correction_result=args.correction_result,
                event_time=args.event_time,
                observed_date=args.observed_date,
                idempotency_key=args.idempotency_key,
                session_review_date=args.session_review_date,
                response_seconds=args.response_seconds,
                latency_state=args.latency_state,
                cross_day=args.cross_day,
                question_valid=args.question_valid,
                formal_node_id=args.formal_node_id,
                review_unit_id=args.review_unit_id,
                trigger_action_id=args.trigger_action_id,
            )
            if not args.full:
                value = _compact_outcome_receipt(value)
        elif args.command == "record-repair":
            value = record_repair(
                repo,
                source=args.source,
                session_id=args.session_id,
                item_id=args.item_id,
                source_id=args.source_id,
                result=args.result,
                confidence=args.confidence,
                prompt_level=args.prompt_level,
                event_time=args.event_time,
                observed_date=args.observed_date,
                idempotency_key=args.idempotency_key,
                attempt=args.attempt,
                session_review_date=args.session_review_date,
                response_seconds=args.response_seconds,
            )
        elif args.command == "sync-session":
            value = sync_session(repo, args.session)
        elif args.command == "migrate-sessions":
            value = migrate_sessions(repo)
        elif args.command == "status":
            state = load_state(repo)
            value = {
                "runtime": _runtime_projection(repo),
                "summary": {
                    "event_count": state["event_count"],
                    "pending_action_count": len(state["pending_actions"]),
                    "candidate_count": len(state["candidates"]),
                    "organic_baseline_count": len(state["organic_baselines"]),
                    "retention_count": sum(item["count"] for item in state["retention"].values()),
                    "formal_write_count": state["formal_write_count"],
                },
            }
            if args.full:
                value["state"] = state
        elif args.command == "daily-summary":
            value = daily_summary(repo, args.date)
        elif args.command == "date-drift-audit":
            value = date_drift_audit(repo)
        elif args.command == "correct-date-drift":
            value = correct_date_drift(
                repo,
                event_ids=args.event_ids,
                all_verified_pending=args.all_verified_pending,
                apply=args.apply,
            )
        elif args.command == "reconcile-scheduling":
            value = reconcile_scheduling_drift(
                repo,
                action_ids=args.action_ids,
                all_eligible=args.all_eligible,
                apply=args.apply,
            )
        elif args.command == "reconcile":
            value = reconcile_runtime_views(repo)
        elif args.command == "audit":
            value = audit(repo)
        elif args.command == "confirm-candidate":
            value = confirm_candidate(
                repo,
                args.candidate_id,
                args.idempotency_key,
                formal_node_id=args.formal_node_id,
            )
        elif args.command == "close-candidate":
            value = close_candidate_as_evidence_only(
                repo,
                args.candidate_id,
                args.idempotency_key,
                cancel_generated_actions=args.cancel_generated_actions,
            )
        elif args.command == "dismiss-item":
            value = dismiss_review_item(
                repo,
                args.session_id,
                args.item_id,
                args.source_id,
                args.trigger_action_id,
                args.idempotency_key,
                args.reason,
            )
        elif args.command == "retire-closed-equivalent-item":
            value = retire_closed_equivalent_item(
                repo,
                session_id=args.session_id,
                item_id=args.item_id,
                source_id=args.source_id,
                trigger_action_id=args.trigger_action_id,
                formal_binding_event_id=args.formal_binding_event_id,
                idempotency_key=args.idempotency_key,
                reason=args.reason,
                user_confirmed=args.user_confirmed,
            )
        else:
            raise ReviewLoopError(f"unsupported command: {args.command}")
    except (OSError, ValueError, ReviewLoopError) as exc:
        print(json.dumps({"status": "ERROR", "error": str(exc)}, ensure_ascii=False), file=sys.stderr)
        return 2
    print(json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True))
    if args.command == "audit" and value.get("status") != "PASS":
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
