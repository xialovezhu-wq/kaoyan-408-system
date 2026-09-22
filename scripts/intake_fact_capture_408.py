#!/usr/bin/env python3
"""408 学习中事实捕获账本。

该账本只保存日终仍无法可靠重建的答案安全事实和稳定证据定位。它不是正式
错题库，不分配正式节点，不写关系、索引、日期记录或复习调度。日终编纂以显式
study_date 冻结待处理集合，再逐题交给正式单写入口。
"""

from __future__ import annotations

import argparse
import datetime as dt
import fcntl
import hashlib
import hmac
import json
import os
import re
import secrets
import stat
import subprocess
import sys
import tempfile
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator
from zoneinfo import ZoneInfo

from intake_lib_408 import FORMAL_ID_RE, scan_leak
from bounded_jsonl_index_408 import (
    BoundedJsonlIndexError,
    lookup_event as lookup_indexed_event,
)
from linked_practice_source_408 import (
    LinkedPracticeSourceError,
    resolve_selection_receipt,
    resolve_source as resolve_linked_practice_source,
    validate_selection_receipt,
    validate_source_for_practice,
)
from review_feedback_loop import (
    ReviewLoopError,
    morning_session_review_date,
    normalize_morning_first_classification,
)


SCHEMA = "intake_fact_capture_v1"
EVENT_SCHEMA = "intake_fact_capture_event_v1"
STATE_SCHEMA = "intake_fact_capture_state_v2"
CANONICAL_COMMIT_PROOF_SCHEMA = "capture_canonical_commit_proof_v1"
LEGACY_BATCH_SCHEMA = "intake_daily_curation_batch_v1"
BATCH_SCHEMA_V2 = "intake_daily_curation_batch_v2"
BATCH_SCHEMA = "intake_daily_curation_batch_v3"
TIMEZONE = "Asia/Shanghai"
REVIEW_EVENT_SCHEMA = "review_event_v1"
BACKFLOW_POLICY_SCHEMA = "morning_review_backflow_policy_v1"
BACKFLOW_POLICY_REL = Path("schema/morning-review-backflow-policy-v1.json")
REVIEW_LOOP_LEDGER_REL = Path(
    "wiki/study_vaults/408-full/state/review-loop/events.jsonl"
)
REVIEW_LOOP_EVENT_INDEX_REL = Path(
    "wiki/study_vaults/408-full/state/review-loop/event-index"
)
MORNING_SESSION_REL = Path("wiki/study_vaults/408-full/state/morning-review")
PERSONALIZATION_STATE_REL = Path(
    "wiki/study_vaults/408-full/state/personalization"
)
MORNING_FAILURE_RESULTS = {"partial", "uncertain", "wrong"}
MORNING_CORRECT_RESULTS = {"independent_correct", "fragile_correct"}
NEUTRAL_SAVE_STATUS = "saved_neutral"
MANAGED_RICH_INTENT_OBJECT_REL = Path(
    "wiki/study_vaults/408-full/state/managed-408-rich-intake-turn/objects/intent"
)
SAVE_REQUEST_RE = re.compile(r"^[A-Za-z0-9_.:@-]{1,160}$")
MAX_SAVE_AUTHORIZATION_MESSAGE_BYTES = 4096
MAX_HOT_MORNING_QUEUE_BYTES = 1024 * 1024
ORDINARY_REVIEW_SOURCES = {"daily_practice", "evening_d0"}
ORDINARY_REVIEW_REF_KIND = "ordinary_review_loop_event"
CURRENT_QUESTION_EVIDENCE_REF_KIND = "current_question_evidence_bundle_v1"
CONVERSATION_PACKAGE_REF_KIND = "cs408_conversation_package_v1"
CURRENT_QUESTION_FAILURE_STANDING_POLICY = (
    "current_question_failure_standing_policy_v1"
)
CONVERSATION_PACKAGE_PENDING_POLICY = "cs408_complete_conversation_pending_policy_v1"
WEB_ENHANCEMENT_PENDING_POLICY = "cs408_awaiting_web_enhancement_policy_v1"
PENDING_FORMAL_STATUS = "awaiting_sol_formalization"
AWAITING_WEB_ENHANCEMENT_STATUS = "awaiting_web_enhancement"
REVIEW_FIRST_BREAK_PROVENANCES = {
    "user_report",
    "visible_evidence",
    "not_observed",
    "derived_classification",
}
REVIEW_NONE_BREAKS = {
    "",
    "none",
    "无",
    "未观察到",
    "未记录",
    "未可靠观察到",
    "not_observed",
}
DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
SAFE_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:/+-]{2,255}$")
SESSION_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
TEMP_LOCATOR_MARKERS = (
    "/tmp/",
    "/private/tmp/",
    "/var/folders/",
    "/private/var/folders/",
    "file://",
)
FORBIDDEN_CAPTURE_KEYS = {
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
SOURCE_FACT_KEYS = {
    "year",
    "subject",
    "question_type",
    "source_id",
    "details_id",
    "episode_id",
    "execution_kind",
    "requires_web_enhancement",
    "awaiting_web_enhancement",
}
USER_FACT_KEYS = {
    "user_error_entry",
    "user_error_provenance",
    "first_action",
    "first_action_provenance",
    "observed_at",
}
IDENTITY_KEYS = {"status", "formal_id", "mode", "basis"}
PROVENANCE = {"user_report", "visible_evidence", "not_observed"}
IDENTITY_STATUSES = {"existing", "new_candidate", "unknown"}
IDENTITY_MODES = {"new", "redo", "unknown"}
FORMAL_SUCCESS_OUTCOMES = {"curated", "already_current"}
CONCEPT_SUCCESS_OUTCOMES = {"concept_only"}
RESULT_OUTCOMES = FORMAL_SUCCESS_OUTCOMES | CONCEPT_SUCCESS_OUTCOMES | {
    "needs_user",
    "failed",
}
SUCCESS_OUTCOMES = FORMAL_SUCCESS_OUTCOMES | CONCEPT_SUCCESS_OUTCOMES
NEEDS_USER_RETIREMENT_SCHEMA = "needs_user_retirement_v1"
NEEDS_USER_RETIREMENT_REASON = "user_removed"
FORMAL_BACKFLOW_CONTRACT_REL = Path(
    "schema/formal-intake-review-backflow-v1.md"
)
FORMAL_BACKFLOW_SUCCESS_STATUSES = {"satisfied", "maintenance_noop"}
FORMAL_BACKFLOW_RESULT_FIELDS = (
    "review_schedule_ensure_receipt_sha256",
    "formal_review_binding_status",
    "formal_review_binding_receipt_sha256",
    "formal_review_action_id",
    "formal_review_due_date",
    "formal_review_unit_id",
)
DEFAULT_RUNTIME_ROOT = Path("~/.codex/kaoyan-408-intake").expanduser().resolve()
NORMAL_RECEIPT_SCHEMA = "intake_batch_receipt_v1"
NORMAL_WAL_SCHEMA = "intake_batch_wal_v1"
MAX_NORMAL_RECEIPT_BYTES = 2 * 1024 * 1024
MAX_NORMAL_WAL_BYTES = 8 * 1024 * 1024
GLOBAL_AUDIT_RECEIPT_SCHEMA = "intake_daily_global_audit_receipt_v1"
GLOBAL_AUDIT_RECEIPT_KIND = "daily_curation_global_audit"
GLOBAL_AUDIT_CLOSE_CONTRACT = "daily_curation_close_v3"
GLOBAL_AUDIT_VERSION = "daily_curation_global_audit_v1"
GLOBAL_AUDIT_RECEIPT_DIR = "global-audit-receipts"
GLOBAL_AUDIT_KEY_FILE = ".global-audit.key"
GLOBAL_AUDIT_TIMEOUT_SECONDS = 600
GLOBAL_AUDIT_MAX_REPORT_BYTES = 512 * 1024
GLOBAL_AUDIT_TOOL_RELS = (
    Path("scripts/intake_validate_408.py"),
    Path("scripts/audit_network_health.py"),
    Path("scripts/check_safe_node_cards.py"),
    Path("scripts/audit_extended.py"),
    Path("scripts/audit_review_schedule.py"),
)
GLOBAL_AUDIT_OBJECT_RELS = (
    Path("节点总表.md"),
    Path("关系边表.md"),
    Path("年份索引.md"),
    Path("知识点命中索引.md"),
    Path("错题日期索引.md"),
    Path("概念词典.md"),
    Path("复习单元总表.md"),
    Path("复习单元节点映射.md"),
    Path("原题复做轨总表.md"),
)
BACKLOG_PLAN_SCHEMA = "intake_curation_backlog_plan_v1"
BACKLOG_FINAL_GATE_SCHEMA = "intake_curation_backlog_final_gate_v1"
MAX_BACKLOG_RECEIPTS = 4096


class CaptureError(RuntimeError):
    """事实捕获合同或账本一致性错误。"""


def _batch_attempt(event: dict[str, Any]) -> int:
    raw = event.get("attempt", 1)
    if not isinstance(raw, int) or isinstance(raw, bool) or raw < 1:
        raise CaptureError("日终批次 attempt 必须是正整数")
    return raw


def _batch_id(study_date: str, capture_set_sha: str, attempt: int) -> str:
    return (
        f"CUR-{study_date.replace('-', '')}-{capture_set_sha[:12]}-A{attempt:02d}"
    )


def _next_batch_attempt(
    state: dict[str, Any], study_date: str, capture_set_sha: str
) -> int:
    prior = [
        int(batch.get("attempt", 1))
        for batch in state.get("batches", {}).values()
        if batch.get("study_date") == study_date
        and batch.get("capture_set_sha256") == capture_set_sha
    ]
    return max(prior, default=0) + 1


def _repo_key(repo: Path) -> str:
    return hashlib.sha256(str(repo.resolve()).encode("utf-8")).hexdigest()[:20]


def _parse_timestamp(value: object, label: str) -> dt.datetime:
    text = str(value or "").strip()
    if not text:
        raise CaptureError(f"{label} 缺失")
    try:
        parsed = dt.datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError as exc:
        raise CaptureError(f"{label} 不是合法 ISO 时间") from exc
    if parsed.tzinfo is None:
        raise CaptureError(f"{label} 必须带时区")
    return parsed.astimezone(dt.timezone.utc)


def _morning_actual_observed_date(event: dict[str, Any]) -> str:
    actual = _parse_timestamp(
        event.get("event_time"), "review event.event_time"
    ).astimezone(ZoneInfo(TIMEZONE)).date().isoformat()
    if "session_review_date" in event and event.get("observed_date") != actual:
        raise CaptureError(
            "新 review event 的 observed_date 与上海实际作答日期不一致"
        )
    return actual


def capture_root(repo_root: str | Path) -> Path:
    repo = Path(repo_root).expanduser().resolve()
    return repo / "wiki" / "study_vaults" / "408-full" / "state" / "intake-curation"


def _resolve_repo(repo_root: str | Path) -> Path:
    repo = Path(repo_root).expanduser().resolve()
    if not repo.is_dir():
        raise CaptureError(f"repo 不存在：{repo}")
    return repo


def _canonical(value: Any) -> bytes:
    return json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")


def _sha256_value(value: Any) -> str:
    return hashlib.sha256(_canonical(value)).hexdigest()


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _sha256_file_bounded(path: Path, *, maximum: int, label: str) -> str:
    try:
        before = path.lstat()
    except OSError as exc:
        raise CaptureError(f"{label} 不可读") from exc
    if stat.S_ISLNK(before.st_mode) or not stat.S_ISREG(before.st_mode):
        raise CaptureError(f"{label} 必须是非链接普通文件")
    if before.st_size > maximum:
        raise CaptureError(f"{label} 超过热路径大小上限")
    digest = hashlib.sha256()
    try:
        with path.open("rb") as handle:
            opened = os.fstat(handle.fileno())
            if (
                opened.st_dev != before.st_dev
                or opened.st_ino != before.st_ino
                or opened.st_size != before.st_size
            ):
                raise CaptureError(f"{label} 在打开前发生漂移")
            for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                digest.update(chunk)
            after = os.fstat(handle.fileno())
    except OSError as exc:
        raise CaptureError(f"{label} 读取失败") from exc
    if (
        after.st_dev != before.st_dev
        or after.st_ino != before.st_ino
        or after.st_size != before.st_size
        or after.st_mtime_ns != before.st_mtime_ns
    ):
        raise CaptureError(f"{label} 在哈希期间发生漂移")
    return digest.hexdigest()


def _now_utc() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat()


def _validate_date(value: object, label: str = "study_date") -> str:
    text = str(value or "").strip()
    if not DATE_RE.fullmatch(text):
        raise CaptureError(f"{label} 必须是 YYYY-MM-DD")
    try:
        dt.date.fromisoformat(text)
    except ValueError as exc:
        raise CaptureError(f"{label} 不是有效日期：{text}") from exc
    return text


def _validate_id(value: object, label: str) -> str:
    text = str(value or "").strip()
    if not SAFE_ID_RE.fullmatch(text):
        raise CaptureError(f"{label} 格式非法")
    return text


def _validate_sha(value: object, label: str) -> str:
    text = str(value or "").strip()
    if not SHA256_RE.fullmatch(text):
        raise CaptureError(f"{label} 必须是小写 SHA-256")
    return text


def _validate_formal_backflow_receipts(
    *,
    repo: Path,
    batch_id: str,
    capture_id: str,
    outcome: str,
    formal_id: str | None,
    formal_result_receipt_sha256: str | None,
    ensure_receipt_sha256: str,
    binding_receipt_sha256: str | None,
) -> dict[str, Any]:
    """Reopen the exact review-backflow receipts bound to one item result.

    The ensure event deliberately precedes formal preflight.  A final binding
    exists only after a committed normal receipt or an exact already-current
    verification has been reopened.  This function validates those artifacts;
    it never creates an action, a formal binding, or a learner outcome.
    """

    # An item can terminate before formal preflight when the canonical capture
    # itself lacks a trustworthy identity or learning breakpoint.  Such a
    # needs_user/failed residual has no formal success to bind and, by design,
    # may be unable to create a scheduling ensure receipt.  Persist an explicit
    # not-applicable terminal instead of forcing callers to fabricate a receipt.
    # Formal-success outcomes still require the complete ensure + final binding
    # chain below.
    if outcome not in FORMAL_SUCCESS_OUTCOMES and not str(ensure_receipt_sha256 or ""):
        if binding_receipt_sha256 is not None:
            raise CaptureError("非正式成功结果不得伪造 final binding receipt")
        return {
            "review_schedule_ensure_receipt_sha256": None,
            "formal_review_binding_status": "not_applicable_preflight_residual",
            "formal_review_binding_receipt_sha256": None,
            "formal_review_action_id": None,
            "formal_review_due_date": None,
            "formal_review_unit_id": None,
        }

    try:
        from formal_intake_review_backflow_408 import (  # type: ignore
            BackflowError,
            ENSURE_RECEIPT_SCHEMA,
            FINAL_RECEIPT_SCHEMA,
            validate_review_backflow_receipt,
        )
    except ImportError as exc:
        raise CaptureError("正式回流合同已启用，但 backflow writer 缺失") from exc

    ensure_sha = _validate_sha(
        ensure_receipt_sha256,
        "review_schedule_ensure_receipt_sha256",
    )
    try:
        ensure = validate_review_backflow_receipt(
            repo,
            ensure_sha,
            expected_schema=ENSURE_RECEIPT_SCHEMA,
        )
    except BackflowError as exc:
        raise CaptureError(f"review ensure receipt 非法：{exc}") from exc
    if (
        ensure.get("batch_id") != batch_id
        or ensure.get("capture_id") != capture_id
    ):
        raise CaptureError("review ensure receipt 未绑定当前 batch/capture")

    action_id = ensure.get("action_id")
    due_date = ensure.get("due_date")
    review_unit_id = ensure.get("review_unit_id")
    if action_id is not None:
        action_id = _validate_id(action_id, "formal_review_action_id")
        due_date = _validate_date(due_date, "formal_review_due_date")
    elif due_date is not None:
        raise CaptureError("无 action 的 ensure receipt 不得声明 due_date")
    if review_unit_id is not None:
        review_unit_id = _validate_id(
            review_unit_id, "formal_review_unit_id"
        )

    if outcome not in FORMAL_SUCCESS_OUTCOMES:
        if binding_receipt_sha256 is not None:
            raise CaptureError("非正式成功结果不得伪造 final binding receipt")
        return {
            "review_schedule_ensure_receipt_sha256": ensure_sha,
            "formal_review_binding_status": (
                "scheduled_unbound" if action_id is not None else str(
                    ensure.get("status") or "needs_user"
                )
            ),
            "formal_review_binding_receipt_sha256": None,
            "formal_review_action_id": action_id,
            "formal_review_due_date": due_date,
            "formal_review_unit_id": review_unit_id,
        }

    final_sha = _validate_sha(
        binding_receipt_sha256,
        "formal_review_binding_receipt_sha256",
    )
    try:
        final = validate_review_backflow_receipt(
            repo,
            final_sha,
            expected_schema=FINAL_RECEIPT_SCHEMA,
        )
    except BackflowError as exc:
        raise CaptureError(f"formal review binding receipt 非法：{exc}") from exc
    status = str(final.get("status") or "")
    if status not in FORMAL_BACKFLOW_SUCCESS_STATUSES:
        raise CaptureError(
            "正式成功结果的 backflow 必须达到 satisfied 或 maintenance_noop"
        )
    expected_proof_kind = (
        "normal_receipt"
        if outcome == "curated"
        else "already_current_verification"
    )
    expected = {
        "batch_id": batch_id,
        "capture_id": capture_id,
        "ensure_receipt_sha256": ensure_sha,
        "formal_id": formal_id,
        "formal_node_id": formal_id,
        "formal_proof_kind": expected_proof_kind,
        "formal_proof_sha256": formal_result_receipt_sha256,
    }
    if any(final.get(field) != value for field, value in expected.items()):
        raise CaptureError("final binding receipt 与逐题正式结果不一致")
    final_action_id = final.get("action_id")
    if final_action_id is not None:
        final_action_id = _validate_id(
            final_action_id, "formal_review_action_id"
        )
    if final.get("due_date") != due_date:
        raise CaptureError("final binding 与 ensure 的 due_date 不一致")
    final_unit = final.get("review_unit_id")
    if final_unit is not None:
        final_unit = _validate_id(final_unit, "formal_review_unit_id")
    if review_unit_id is not None and final_unit != review_unit_id:
        raise CaptureError("final binding 与 ensure 的 review_unit_id 不一致")
    if status == "satisfied":
        if not final.get("formal_binding_event_id") or final_action_id is None:
            raise CaptureError("satisfied final binding 缺 event 或 canonical action")
        if final_unit is None:
            raise CaptureError("satisfied final binding 缺 receipt-bound review unit")
    elif (
        final_action_id is not None
        or due_date is not None
        or not final.get("formal_binding_event_id")
    ):
        raise CaptureError("maintenance_noop 只能绑定 actionless formal event")
    return {
        "review_schedule_ensure_receipt_sha256": ensure_sha,
        "formal_review_binding_status": status,
        "formal_review_binding_receipt_sha256": final_sha,
        "formal_review_action_id": final_action_id,
        "formal_review_due_date": due_date,
        "formal_review_unit_id": final_unit,
    }


def _normalize_review_break_provenance(value: object, first_break: object) -> str:
    provenance = str(value or "")
    if provenance in REVIEW_FIRST_BREAK_PROVENANCES:
        return provenance
    if provenance:
        raise CaptureError("review event 的 first_break_provenance 非法")
    return (
        "not_observed"
        if str(first_break or "").strip().lower() in REVIEW_NONE_BREAKS
        else "derived_classification"
    )


def _canonical_morning_session_result(first: dict[str, Any]) -> str:
    try:
        result, _ = normalize_morning_first_classification(
            str(first.get("result") or ""),
            choice_result=str(first.get("choice_result") or "not_applicable"),
            reasoning_result=str(
                first.get("reasoning_result") or "not_observed"
            ),
            prompt_level=str(first.get("prompt_level") or ""),
        )
    except ReviewLoopError as exc:
        raise CaptureError(str(exc)) from exc
    return result


def _assert_no_forbidden_keys(value: Any, path: str = "capture") -> None:
    if isinstance(value, dict):
        for key, nested in value.items():
            if str(key) in FORBIDDEN_CAPTURE_KEYS:
                raise CaptureError(f"{path}.{key} 是受保护字段，禁止进入事实账本")
            _assert_no_forbidden_keys(nested, f"{path}.{key}")
    elif isinstance(value, list):
        for index, nested in enumerate(value):
            _assert_no_forbidden_keys(nested, f"{path}[{index}]")


def _assert_answer_safe(label: str, value: object) -> str:
    text = str(value or "").strip()
    fails, warns = scan_leak(text)
    if fails or warns:
        reasons = "；".join(fails + warns)
        raise CaptureError(f"{label} 未通过答案安全扫描：{reasons}")
    return text


def _stable_locator(value: object, label: str) -> str:
    text = str(value or "").strip()
    if not text:
        raise CaptureError(f"{label} 不能为空")
    lowered = text.lower()
    if any(marker in lowered for marker in TEMP_LOCATOR_MARKERS):
        raise CaptureError(f"{label} 不能使用临时路径或 file URI")
    return _assert_answer_safe(label, text)


def _validate_capture_payload(raw: Any) -> dict[str, Any]:
    if not isinstance(raw, dict):
        raise CaptureError("capture 输入必须是 JSON object")
    _assert_no_forbidden_keys(raw)
    allowed = {
        "schema",
        "study_date",
        "timezone",
        "idempotency_key",
        "stable_evidence_refs",
        "source_facts",
        "user_facts",
        "identity_hint",
        "answer_safe_context_anchor",
        "missing_fields",
        "formalization_authorized",
        "authorization_policy",
    }
    unknown = sorted(set(raw) - allowed)
    if unknown:
        raise CaptureError(f"capture 含未授权字段：{unknown}")
    if raw.get("schema") != SCHEMA:
        raise CaptureError(f"schema 必须是 {SCHEMA}")
    study_date = _validate_date(raw.get("study_date"))
    timezone = str(raw.get("timezone") or TIMEZONE).strip()
    if timezone != TIMEZONE:
        raise CaptureError(f"timezone 当前必须显式使用 {TIMEZONE}")
    idempotency_key = _validate_id(raw.get("idempotency_key"), "idempotency_key")

    refs = raw.get("stable_evidence_refs")
    if not isinstance(refs, list) or not refs:
        raise CaptureError("stable_evidence_refs 至少需要一项耐久证据定位")
    normalized_refs: list[dict[str, str]] = []
    for index, item in enumerate(refs, 1):
        if not isinstance(item, dict) or set(item) != {"kind", "locator", "sha256"}:
            raise CaptureError(
                f"stable_evidence_refs#{index} 必须恰含 kind/locator/sha256"
            )
        normalized_refs.append(
            {
                "kind": _assert_answer_safe(f"stable_evidence_refs#{index}.kind", item["kind"]),
                "locator": _stable_locator(
                    item["locator"], f"stable_evidence_refs#{index}.locator"
                ),
                "sha256": _validate_sha(
                    item["sha256"], f"stable_evidence_refs#{index}.sha256"
                ),
            }
        )

    source_facts = raw.get("source_facts") or {}
    if not isinstance(source_facts, dict) or set(source_facts) - SOURCE_FACT_KEYS:
        raise CaptureError("source_facts 含未授权字段")
    normalized_source = {
        key: _assert_answer_safe(f"source_facts.{key}", value)
        for key, value in source_facts.items()
        if str(value or "").strip()
    }
    subject = normalized_source.get("subject")
    if subject and subject not in {"DS", "CO", "OS", "CN", "未确认"}:
        raise CaptureError("source_facts.subject 必须是 DS/CO/OS/CN/未确认")

    user_facts = raw.get("user_facts") or {}
    if not isinstance(user_facts, dict) or set(user_facts) - USER_FACT_KEYS:
        raise CaptureError("user_facts 含未授权字段")
    normalized_user = {
        key: _assert_answer_safe(f"user_facts.{key}", value)
        for key, value in user_facts.items()
        if str(value or "").strip()
    }
    error_provenance = normalized_user.get("user_error_provenance", "not_observed")
    if error_provenance not in PROVENANCE:
        raise CaptureError("user_error_provenance 非法")
    error_entry = normalized_user.get("user_error_entry", "未观察到")
    if error_provenance == "not_observed" and error_entry != "未观察到":
        raise CaptureError("未观察到用户错因时不得写入推断错因")
    normalized_user["user_error_entry"] = error_entry
    normalized_user["user_error_provenance"] = error_provenance
    if "first_action" in normalized_user:
        first_provenance = normalized_user.get("first_action_provenance")
        if first_provenance not in PROVENANCE - {"not_observed"}:
            raise CaptureError("记录 first_action 时必须给出直接 provenance")

    identity = raw.get("identity_hint") or {"status": "unknown", "mode": "unknown"}
    if not isinstance(identity, dict) or set(identity) - IDENTITY_KEYS:
        raise CaptureError("identity_hint 含未授权字段")
    status = str(identity.get("status") or "unknown").strip()
    mode = str(identity.get("mode") or "unknown").strip()
    if status not in IDENTITY_STATUSES or mode not in IDENTITY_MODES:
        raise CaptureError("identity_hint.status/mode 非法")
    formal_id = str(identity.get("formal_id") or "").strip()
    if formal_id and not FORMAL_ID_RE.fullmatch(formal_id):
        raise CaptureError("identity_hint.formal_id 非法")
    if status == "existing" and (not formal_id or mode != "redo"):
        raise CaptureError("existing 身份必须绑定合法 formal_id 且 mode=redo")
    if status != "existing" and formal_id:
        raise CaptureError("只有 existing 身份可以在捕获阶段绑定正式 ID")
    normalized_identity = {
        "status": status,
        "mode": mode,
        "formal_id": formal_id,
        "basis": _assert_answer_safe("identity_hint.basis", identity.get("basis", "")),
    }

    anchor = _assert_answer_safe(
        "answer_safe_context_anchor", raw.get("answer_safe_context_anchor")
    )
    if not anchor:
        raise CaptureError("answer_safe_context_anchor 不能为空")
    missing = raw.get("missing_fields") or []
    if not isinstance(missing, list) or not all(isinstance(item, str) for item in missing):
        raise CaptureError("missing_fields 必须是字符串数组")
    normalized_missing = sorted(
        {_assert_answer_safe("missing_fields", item) for item in missing if item.strip()}
    )
    authorized = raw.get("formalization_authorized")
    if not isinstance(authorized, bool):
        raise CaptureError("formalization_authorized 必须显式为 boolean")
    if (
        any(ref["kind"] == ORDINARY_REVIEW_REF_KIND for ref in normalized_refs)
        and authorized
    ):
        raise CaptureError("ordinary review event 不能自动授权 formalization")

    current_question_refs = [
        ref
        for ref in normalized_refs
        if ref["kind"]
        in {CURRENT_QUESTION_EVIDENCE_REF_KIND, CONVERSATION_PACKAGE_REF_KIND}
    ]
    authorization_policy = str(raw.get("authorization_policy") or "").strip()
    if current_question_refs:
        if len(current_question_refs) != 1 or len(normalized_refs) != 1:
            raise CaptureError("current-question capture 必须唯一绑定私有证据包")
        allowed_policies = (
            {
                CONVERSATION_PACKAGE_PENDING_POLICY,
                WEB_ENHANCEMENT_PENDING_POLICY,
            }
            if current_question_refs[0]["kind"] == CONVERSATION_PACKAGE_REF_KIND
            else {CURRENT_QUESTION_FAILURE_STANDING_POLICY}
        )
        if authorization_policy not in allowed_policies:
            raise CaptureError("current-question capture 缺少固定本地待正式处理 policy")
        if authorized is not True:
            raise CaptureError("current-question failure policy 必须进入日终待编纂清单")
    elif authorization_policy:
        raise CaptureError("非 current-question capture 不得声明该 standing policy")

    normalized = {
        "schema": SCHEMA,
        "study_date": study_date,
        "timezone": timezone,
        "idempotency_key": idempotency_key,
        "stable_evidence_refs": normalized_refs,
        "source_facts": normalized_source,
        "user_facts": normalized_user,
        "identity_hint": normalized_identity,
        "answer_safe_context_anchor": anchor,
        "missing_fields": normalized_missing,
        "formalization_authorized": authorized,
    }
    if authorization_policy:
        normalized["authorization_policy"] = authorization_policy
    return normalized


def initial_capture_quality_status(payload: dict[str, Any]) -> str:
    """Return the honest initial replay state for one validated Capture."""

    if payload.get("authorization_policy") == WEB_ENHANCEMENT_PENDING_POLICY:
        return AWAITING_WEB_ENHANCEMENT_STATUS
    return (
        PENDING_FORMAL_STATUS
        if payload.get("formalization_authorized") is True
        else "captured_unconfirmed"
    )


def _read_json(path: str | Path) -> Any:
    try:
        return json.loads(Path(path).expanduser().read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise CaptureError(f"读取 JSON 失败：{exc}") from exc


def _load_backflow_policy(repo: Path) -> dict[str, Any]:
    path = repo / BACKFLOW_POLICY_REL
    raw = _read_json(path)
    required = {
        "schema",
        "enabled",
        "effective_date",
        "timezone",
        "eligible_source",
        "eligible_first_results",
        "fast_capture_formalization_authorized",
        "formal_write_authorized",
        "daily_curation_requires_explicit_date",
        "authorization_basis",
    }
    if not isinstance(raw, dict) or set(raw) != required:
        raise CaptureError("晨间回流 policy 字段不完整或含未知字段")
    if raw.get("schema") != BACKFLOW_POLICY_SCHEMA or raw.get("enabled") is not True:
        raise CaptureError("晨间回流 policy 未启用或 schema 非法")
    _validate_date(raw.get("effective_date"), "policy.effective_date")
    if raw.get("timezone") != TIMEZONE or raw.get("eligible_source") != "morning_review":
        raise CaptureError("晨间回流 policy 的时区或来源非法")
    if set(raw.get("eligible_first_results") or []) != MORNING_FAILURE_RESULTS:
        raise CaptureError("晨间回流 policy 的首答结果集合非法")
    if raw.get("fast_capture_formalization_authorized") is not True:
        raise CaptureError("晨间回流 policy 未授权进入日终清单")
    if raw.get("formal_write_authorized") is not False:
        raise CaptureError("晨间回流 policy 不能授权晨间正式写入")
    if raw.get("daily_curation_requires_explicit_date") is not True:
        raise CaptureError("晨间回流 policy 必须要求显式日期日终编纂")
    _assert_answer_safe("policy.authorization_basis", raw.get("authorization_basis"))
    return raw


def _review_event_by_id(repo: Path, event_id: str) -> dict[str, Any]:
    event_value = _validate_id(event_id, "event_id")
    path = repo / REVIEW_LOOP_LEDGER_REL
    if not path.is_file():
        raise CaptureError("统一复盘事件账本不存在")
    hot_state_dir = repo / REVIEW_LOOP_LEDGER_REL.parent / "hot-state"
    index_dir = repo / REVIEW_LOOP_EVENT_INDEX_REL
    if (hot_state_dir / "manifest.json").is_file():
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
            raise CaptureError("统一复盘 hot state 精确校验失败") from exc
        matches = [hot["event"]] if hot.get("status") == "found" else []
    elif (index_dir / "manifest.json").is_file():
        try:
            indexed = lookup_indexed_event(path, index_dir, event_value)
        except BoundedJsonlIndexError as exc:
            raise CaptureError("统一复盘 event 精确索引校验失败") from exc
        matches = [indexed["event"]] if indexed is not None else []
    else:
        # Recovery compatibility only. The managed answer hot path bootstraps
        # this exact index before it can capture a canonical review event.
        matches = []
        seen: set[str] = set()
        for line_no, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
            if not line.strip():
                continue
            try:
                event = json.loads(line)
            except json.JSONDecodeError as exc:
                raise CaptureError(f"统一复盘事件账本第 {line_no} 行损坏：{exc}") from exc
            current_id = str(event.get("event_id") or "") if isinstance(event, dict) else ""
            if current_id in seen or not current_id:
                raise CaptureError("统一复盘事件账本含重复或空 event_id")
            seen.add(current_id)
            if current_id == event_value:
                matches.append(event)
    if len(matches) != 1:
        raise CaptureError(f"未找到唯一 review event：{event_value}")
    event = matches[0]
    if event.get("schema") != REVIEW_EVENT_SCHEMA:
        raise CaptureError("review event schema 非法")
    return event


def _validate_linked_personalization_manifest(
    path: Path, *, expected_formal_node_id: str | None = None
) -> str:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise CaptureError("linked-practice personalization manifest 不可读") from exc
    assets = payload.get("question_assets") if isinstance(payload, dict) else None
    formal_id = str(payload.get("formal_node_id") or "")
    if (
        payload.get("schema") != "question_source_manifest_v1"
        or payload.get("answer_isolated") is not True
        or not FORMAL_ID_RE.fullmatch(formal_id)
        or (expected_formal_node_id and formal_id != expected_formal_node_id)
        or not isinstance(assets, list)
        or not assets
        or payload.get("question_asset_count") != len(assets)
    ):
        raise CaptureError("linked-practice personalization manifest 未通过题面隔离门禁")
    for asset in assets:
        if not isinstance(asset, dict) or set(asset) != {
            "path",
            "sha256",
            "size_bytes",
        }:
            raise CaptureError("linked-practice question asset 记录非法")
        asset_path = Path(str(asset.get("path") or "")).expanduser()
        digest = str(asset.get("sha256") or "")
        size = asset.get("size_bytes")
        if (
            not asset_path.is_absolute()
            or not asset_path.is_file()
            or not asset_path.name.startswith("question-")
            or "solution" in asset_path.name.lower()
            or not SHA256_RE.fullmatch(digest)
            or not isinstance(size, int)
            or size < 0
            or asset_path.stat().st_size != size
            or _sha256_file(asset_path) != digest
        ):
            raise CaptureError("linked-practice question asset 校验失败")
    return formal_id


def _linked_source_path(repo: Path, locator: object) -> tuple[Path, str]:
    raw = _stable_locator(locator, "linked_practice.source_ref")
    try:
        return resolve_linked_practice_source(repo, raw)
    except LinkedPracticeSourceError as exc:
        raise CaptureError(str(exc)) from exc


def _session_evidence_binding(
    repo: Path, event: dict[str, Any]
) -> tuple[
    dict[str, Any],
    dict[str, Any],
    Path,
    str,
    dict[str, Any] | None,
]:
    session_id = str(event.get("session_id") or "")
    item_id = str(event.get("item_id") or "")
    if not SESSION_ID_RE.fullmatch(session_id):
        raise CaptureError("review event session_id 非法")
    session_dir = (repo / MORNING_SESSION_REL / session_id).resolve()
    try:
        session_dir.relative_to((repo / MORNING_SESSION_REL).resolve())
    except ValueError as exc:
        raise CaptureError("morning session 路径越界") from exc
    session_ledger = session_dir / "events.jsonl"
    if not session_ledger.is_file():
        raise CaptureError("morning session 事件账本不存在")
    try:
        session_events = [
            json.loads(line)
            for line in session_ledger.read_text(encoding="utf-8").splitlines()
            if line.strip()
        ]
        started = session_events[0]
    except (IndexError, json.JSONDecodeError) as exc:
        raise CaptureError("morning session 缺少有效 session_started") from exc
    for sequence, session_event in enumerate(session_events, 1):
        if (
            session_event.get("schema") != "morning_review_session_event_v1"
            or session_event.get("session_id") != session_id
            or session_event.get("sequence") != sequence
        ):
            raise CaptureError("morning session 事件序列非法")
    if (
        started.get("schema") != "morning_review_session_event_v1"
        or started.get("event_type") != "session_started"
        or started.get("session_id") != session_id
    ):
        raise CaptureError("morning session 首事件非法")
    payload = started.get("payload") or {}
    if payload.get("review_date") != morning_session_review_date(event):
        raise CaptureError("review event 日期与 morning session 不一致")
    queue_item = next(
        (row for row in payload.get("items", []) if row.get("item_id") == item_id),
        None,
    )
    followup_events = [
        row
        for row in session_events[1:]
        if row.get("event_type") == "followup_scheduled"
        and (row.get("payload") or {}).get("item_id") == item_id
    ]
    if queue_item is not None and followup_events:
        raise CaptureError("morning session item 同时出现在不可变队列与 followup 事件")
    linked: dict[str, Any] | None = None
    if queue_item is None:
        if len(followup_events) != 1:
            raise CaptureError("动态 morning item 缺少唯一 followup_scheduled 证据")
        followup_event = followup_events[0]
        item = followup_event.get("payload") or {}
        practice_mode = str(item.get("practice_mode") or "")
        expected_kind = (
            "related_original" if practice_mode == "original" else "grounded_variant"
        )
        for label in (
            "scheduling_key",
            "trigger_event_id",
            "parent_item_id",
            "context_slice_id",
            "candidate_id",
            "source_id",
            "source_ref",
            "source_sha256",
            "selection_receipt_ref",
            "selection_receipt_sha256",
            "mechanism_key",
            "knowledge_point",
        ):
            if not str(item.get(label) or ""):
                raise CaptureError(f"followup_scheduled 缺少 {label}")
        if practice_mode not in {"original", "variant"} or item.get(
            "item_kind"
        ) != expected_kind:
            raise CaptureError("followup_scheduled 的 practice_mode/item_kind 非法")
        source_path, source_ref = _linked_source_path(repo, item.get("source_ref"))
        source_sha = _validate_sha(
            item.get("source_sha256"), "followup_scheduled.source_sha256"
        )
        if _sha256_file(source_path) != source_sha:
            raise CaptureError("linked-practice source 已偏离调度快照")
        trigger = _review_event_by_id(repo, str(item.get("trigger_event_id")))
        if (
            trigger.get("event_kind") != "outcome"
            or trigger.get("source") != "morning_review"
            or trigger.get("session_id") != session_id
            or trigger.get("item_id") != item.get("parent_item_id")
            or trigger.get("practice_mode")
        ):
            raise CaptureError("followup_scheduled 的 parent trigger 非法")
        for label in (
            "practice_mode",
            "anchor_formal_node_id",
            "variant_of_formal_node_id",
            "trigger_event_id",
            "context_slice_id",
            "candidate_id",
            "source_ref",
            "source_sha256",
            "selection_receipt_ref",
            "selection_receipt_sha256",
        ):
            event_label = (
                "parent_trigger_event_id" if label == "trigger_event_id" else label
            )
            projected = source_ref if label == "source_ref" else item.get(label)
            if projected != event.get(event_label):
                raise CaptureError(
                    f"review event 与 followup_scheduled 的 {label} 不一致"
                )
        for label in ("mechanism_key", "knowledge_point"):
            if item.get(label) != event.get(label):
                raise CaptureError(
                    f"review event 未保真保存 followup_scheduled 的 {label}"
                )
        formal_id = item.get("formal_node_id") or None
        variant_of = item.get("variant_of_formal_node_id") or None
        anchor_formal = item.get("anchor_formal_node_id") or None
        if practice_mode == "original":
            if (
                not formal_id
                or variant_of
                or event.get("formal_node_id") != formal_id
                or event.get("actual_question_formal_node_id") != formal_id
            ):
                raise CaptureError("original followup 的真实原题身份绑定非法")
        elif (
            formal_id
            or not variant_of
            or anchor_formal != variant_of
            or event.get("formal_node_id") is not None
            or event.get("actual_question_formal_node_id") is not None
        ):
            raise CaptureError("variant followup 污染了原题真实作答身份")
        try:
            verified_source = validate_source_for_practice(
                repo,
                source_ref,
                source_sha,
                practice_mode=practice_mode,
                formal_node_id=(str(formal_id) if formal_id else None),
                variant_of_formal_node_id=(str(variant_of) if variant_of else None),
            )
        except LinkedPracticeSourceError as exc:
            raise CaptureError(str(exc)) from exc
        if (
            verified_source["path"] != source_path
            or verified_source["source_ref"] != source_ref
        ):
            raise CaptureError("linked-practice source identity 不一致")
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
                    "source_ref": source_ref,
                    "source_sha256": source_sha,
                    "mechanism_key": item.get("mechanism_key"),
                    "knowledge_point": item.get("knowledge_point"),
                    "review_unit_id": item.get("review_unit_id"),
                },
                source_path=source_path,
            )
        except LinkedPracticeSourceError as exc:
            raise CaptureError(str(exc)) from exc
        selection_path = verified_selection["path"]
        selection_ref = verified_selection["selection_receipt_ref"]
        if selection_ref != item.get("selection_receipt_ref"):
            raise CaptureError("linked-practice selection receipt identity 不一致")
        if event.get("followup_event_id") != followup_event.get("event_id"):
            raise CaptureError("review event 未绑定唯一 followup_scheduled 事件")
        linked = {
            "followup_event": followup_event,
            "parent_trigger": trigger,
            "source_path": source_path,
            "source_ref": source_ref,
            "selection_path": selection_path,
            "selection_ref": selection_ref,
        }
    else:
        if not isinstance(queue_item, dict):
            raise CaptureError("morning session item 非法")
        item = queue_item
    if item.get("source_id") != event.get("source_id"):
        raise CaptureError("review event 与 morning session item/source 不一致")
    first_events = [
        row
        for row in session_events[1:]
        if row.get("event_type") in {"first_recorded", "first_reconciled_from_loop"}
        and (row.get("payload") or {}).get("item_id") == item_id
    ]
    if len(first_events) != 1:
        raise CaptureError("review event 尚未唯一投影为 morning session 首答")
    first = first_events[0].get("payload") or {}
    binding_events = [
        row
        for row in session_events[1:]
        if row.get("event_type") == "first_bound_to_loop"
        and (row.get("payload") or {}).get("item_id") == item_id
        and (row.get("payload") or {}).get("canonical_event_id")
        == event.get("event_id")
    ]
    reconciled_binding = first.get("canonical_event_id") == event.get("event_id")
    if not reconciled_binding and len(binding_events) != 1:
        raise CaptureError("review event 尚未由 morning session 首答唯一绑定")
    session_result = _canonical_morning_session_result(first)
    for field, projected, canonical in (
        ("result", session_result, event.get("first_result")),
        ("confidence", first.get("confidence"), event.get("confidence")),
        ("prompt_level", first.get("prompt_level"), event.get("prompt_level")),
        ("first_break", first.get("first_break"), event.get("first_break")),
        (
            "first_break_provenance",
            _normalize_review_break_provenance(
                first.get("first_break_provenance"), first.get("first_break")
            ),
            _normalize_review_break_provenance(
                event.get("first_break_provenance"), event.get("first_break")
            ),
        ),
        (
            "event_time",
            first.get("recorded_at") or first_events[0].get("timestamp"),
            event.get("event_time"),
        ),
    ):
        if projected != canonical:
            raise CaptureError(f"review event 与 morning session 首答 {field} 不一致")
    queue_raw = str(payload.get("queue_path") or "")
    queue_path = (repo / queue_raw).resolve()
    try:
        queue_rel = str(queue_path.relative_to(repo))
    except ValueError as exc:
        raise CaptureError("morning queue 路径越界") from exc
    expected_hash = _validate_sha(payload.get("queue_sha256"), "session.queue_sha256")
    if not queue_path.is_file() or _sha256_file(queue_path) != expected_hash:
        raise CaptureError("morning queue 缺失或已偏离 session 启动快照")
    return started, first_events[0], queue_path, queue_rel, linked


def _review_capture_payload_from_verified_evidence(
    repo: Path,
    event: dict[str, Any],
    policy: dict[str, Any],
    *,
    started: dict[str, Any],
    first_event: dict[str, Any],
    queue_path: Path,
    queue_rel: str,
    linked: dict[str, Any] | None,
    queue_sha256: str | None = None,
    extra_stable_refs: list[dict[str, str]] | None = None,
) -> dict[str, Any]:
    if (
        event.get("schema") != REVIEW_EVENT_SCHEMA
        or event.get("event_kind") != "outcome"
        or event.get("source") != "morning_review"
        or event.get("question_valid") is not True
        or event.get("formal_write_authorized") is not False
    ):
        raise CaptureError("只允许捕获有效的晨间首答 outcome")
    first_result = str(event.get("first_result") or "")
    if first_result not in set(policy["eligible_first_results"]):
        raise CaptureError("该晨间首答结果不属于快速捕获范围")
    observed_date = _validate_date(
        _morning_actual_observed_date(event), "event.observed_date"
    )
    if observed_date < str(policy["effective_date"]):
        raise CaptureError("review event 早于晨间回流 policy 生效日期")
    event_id = str(event["event_id"])
    session_id = str(event["session_id"])
    session_ledger_rel = str(MORNING_SESSION_REL / session_id / "events.jsonl")
    event_locator = f"{REVIEW_LOOP_LEDGER_REL}#event_id={event_id}"
    session_locator = f"{session_ledger_rel}#sequence=1"
    first_locator = f"{session_ledger_rel}#sequence={first_event['sequence']}"

    route = event.get("route") or {}
    candidate = route.get("candidate") or {}
    formal_id = str(event.get("formal_node_id") or "")
    if not formal_id and route.get("kind") == "update_existing":
        formal_id = str(candidate.get("formal_node_id") or "")
    if formal_id and not FORMAL_ID_RE.fullmatch(formal_id):
        raise CaptureError("review event 的正式节点身份非法")
    if formal_id:
        identity = {
            "status": "existing",
            "formal_id": formal_id,
            "mode": "redo",
            "basis": f"canonical review event 明确绑定 {formal_id}",
        }
    else:
        identity = {
            "status": "unknown",
            "mode": "unknown",
            "basis": "正式身份留待日终依据稳定证据核验",
        }

    first_break = str(event.get("first_break") or "").strip()
    provenance = _normalize_review_break_provenance(
        event.get("first_break_provenance"), first_break
    )
    if provenance in {"user_report", "visible_evidence"} and first_break:
        user_facts = {
            "user_error_entry": first_break,
            "user_error_provenance": provenance,
            "observed_at": str(event.get("event_time") or ""),
        }
    else:
        user_facts = {
            "user_error_entry": "未观察到",
            "user_error_provenance": "not_observed",
            "observed_at": str(event.get("event_time") or ""),
        }

    source_id = str(event.get("source_id") or "")
    context = str(
        event.get("knowledge_point")
        or event.get("mechanism_key")
        or source_id
    )
    source_facts: dict[str, str] = {
        "question_type": (
            "关联原题复做"
            if event.get("practice_mode") == "original"
            else "有据变式练习"
            if event.get("practice_mode") == "variant"
            else "晨间主动回忆"
        ),
        "source_id": source_id,
        "details_id": event_id,
    }
    if formal_id:
        subject, year, _ = formal_id.split("_", 2)
        source_facts["subject"] = subject
        if year.isdigit():
            source_facts["year"] = year
    else:
        variant_origin = str(event.get("variant_of_formal_node_id") or "")
        if FORMAL_ID_RE.fullmatch(variant_origin):
            source_facts["subject"] = variant_origin.split("_", 1)[0]
        else:
            source_facts["subject"] = "未确认"
    missing = []
    if not formal_id:
        missing.append("formal_identity")
    if not event.get("knowledge_point"):
        missing.append("main_knowledge")
    if user_facts["user_error_provenance"] == "not_observed":
        missing.append("user_error_entry")

    stable_refs = [
        {
            "kind": "review_loop_event",
            "locator": event_locator,
            "sha256": _sha256_value(event),
        },
        {
            "kind": "morning_session_start",
            "locator": session_locator,
            "sha256": _sha256_value(started),
        },
        {
            "kind": "morning_session_first",
            "locator": first_locator,
            "sha256": _sha256_value(first_event),
        },
        {
            "kind": "morning_session_queue",
            "locator": queue_rel,
            "sha256": queue_sha256 or _sha256_file(queue_path),
        },
    ]
    stable_refs.extend(extra_stable_refs or [])
    if linked is not None:
        followup_event = linked["followup_event"]
        trigger = linked["parent_trigger"]
        stable_refs.extend(
            [
                {
                    "kind": "linked_practice_followup",
                    "locator": (
                        f"{session_ledger_rel}#sequence={followup_event['sequence']}"
                    ),
                    "sha256": _sha256_value(followup_event),
                },
                {
                    "kind": "linked_practice_parent_trigger",
                    "locator": (
                        f"{REVIEW_LOOP_LEDGER_REL}#event_id={trigger['event_id']}"
                    ),
                    "sha256": _sha256_value(trigger),
                },
                {
                    "kind": "linked_practice_source",
                    "locator": linked["source_ref"],
                    "sha256": _sha256_file(linked["source_path"]),
                },
                {
                    "kind": "personalization_selection_receipt",
                    "locator": linked["selection_ref"],
                    "sha256": _sha256_file(linked["selection_path"]),
                },
            ]
        )

    return {
        "schema": SCHEMA,
        "study_date": observed_date,
        "timezone": TIMEZONE,
        "idempotency_key": f"review-event:{event_id}:fast-capture:v1",
        "stable_evidence_refs": stable_refs,
        "source_facts": source_facts,
        "user_facts": user_facts,
        "identity_hint": identity,
        "answer_safe_context_anchor": f"晨间复盘 {source_id} 的 {context} 非正确首答证据",
        "missing_fields": missing,
        "formalization_authorized": bool(
            policy["fast_capture_formalization_authorized"]
        ),
    }


def _review_capture_payload(
    repo: Path, event: dict[str, Any], policy: dict[str, Any]
) -> dict[str, Any]:
    """Cold compatibility bridge for historical and explicit recovery callers.

    The managed morning answer path uses bounded receipts and must call
    ``build_verified_morning_capture_payload`` instead.  This bridge preserves
    the existing recovery behavior and may scan the one session ledger.
    """

    started, first_event, queue_path, queue_rel, linked = _session_evidence_binding(
        repo, event
    )
    return _review_capture_payload_from_verified_evidence(
        repo,
        event,
        policy,
        started=started,
        first_event=first_event,
        queue_path=queue_path,
        queue_rel=queue_rel,
        queue_sha256=None,
        linked=linked,
    )


def build_verified_morning_capture_payload(
    repo_root: str | Path,
    event: dict[str, Any],
    *,
    session_started_event: dict[str, Any],
    session_first_event: dict[str, Any],
    session_binding_event: dict[str, Any],
    linked_followup_event: dict[str, Any] | None = None,
    parent_trigger_event: dict[str, Any] | None = None,
    receipt_refs: list[dict[str, str]] | None = None,
) -> dict[str, Any]:
    """Build one morning capture from already exact-read, receipt-bound evidence.

    This function performs no review, session, or capture ledger lookup.  It
    revalidates the supplied event identities, the immutable queue snapshot,
    and any linked-practice source/selection receipts before returning a fully
    normalized answer-safe capture payload.
    """

    repo = _resolve_repo(repo_root)
    policy = _load_backflow_policy(repo)
    if not isinstance(event, dict):
        raise CaptureError("managed morning capture 缺少 canonical event")
    event_id = _validate_id(event.get("event_id"), "event_id")
    session_id = str(event.get("session_id") or "")
    item_id = _validate_id(event.get("item_id"), "item_id")
    if not SESSION_ID_RE.fullmatch(session_id):
        raise CaptureError("review event session_id 非法")

    def session_event(
        value: dict[str, Any], event_type: str, label: str
    ) -> tuple[dict[str, Any], dict[str, Any]]:
        if (
            not isinstance(value, dict)
            or value.get("schema") != "morning_review_session_event_v1"
            or value.get("session_id") != session_id
            or value.get("event_type") != event_type
            or not isinstance(value.get("sequence"), int)
            or int(value["sequence"]) <= 0
            or not isinstance(value.get("payload"), dict)
        ):
            raise CaptureError(f"{label} 的 session event 身份非法")
        return value, value["payload"]

    started, started_payload = session_event(
        session_started_event, "session_started", "morning session start"
    )
    first_event, first = session_event(
        session_first_event, "first_recorded", "morning session first"
    )
    binding_event, binding = session_event(
        session_binding_event,
        "first_bound_to_loop",
        "morning session binding",
    )
    if started.get("sequence") != 1:
        raise CaptureError("morning session start 必须是 sequence=1")
    if started_payload.get("review_date") != morning_session_review_date(event):
        raise CaptureError("review event 日期与 morning session 不一致")
    if (
        first.get("item_id") != item_id
        or binding.get("item_id") != item_id
        or binding.get("canonical_event_id") != event_id
    ):
        raise CaptureError("morning 首答或 canonical binding 身份不一致")

    session_result = _canonical_morning_session_result(first)
    comparisons = (
        ("result", session_result, event.get("first_result")),
        ("choice_result", first.get("choice_result"), event.get("choice_result")),
        (
            "reasoning_result",
            first.get("reasoning_result"),
            event.get("reasoning_result"),
        ),
        ("confidence", first.get("confidence"), event.get("confidence")),
        ("prompt_level", first.get("prompt_level"), event.get("prompt_level")),
        ("first_break", first.get("first_break"), event.get("first_break")),
        (
            "first_break_provenance",
            _normalize_review_break_provenance(
                first.get("first_break_provenance"), first.get("first_break")
            ),
            _normalize_review_break_provenance(
                event.get("first_break_provenance"), event.get("first_break")
            ),
        ),
        (
            "event_time",
            first.get("recorded_at") or first_event.get("timestamp"),
            event.get("event_time"),
        ),
    )
    for field, projected, canonical in comparisons:
        if projected != canonical:
            raise CaptureError(
                f"review event 与 receipt-bound morning 首答 {field} 不一致"
            )

    queue_raw = str(started_payload.get("queue_path") or "")
    queue_path = (repo / queue_raw).resolve()
    try:
        queue_rel = str(queue_path.relative_to(repo))
    except ValueError as exc:
        raise CaptureError("morning queue 路径越界") from exc
    queue_sha = _validate_sha(
        started_payload.get("queue_sha256"), "session.queue_sha256"
    )
    if (
        _sha256_file_bounded(
            queue_path,
            maximum=MAX_HOT_MORNING_QUEUE_BYTES,
            label="morning queue",
        )
        != queue_sha
    ):
        raise CaptureError("morning queue 缺失或已偏离 session 启动快照")
    queue_items = started_payload.get("items")
    if not isinstance(queue_items, list):
        raise CaptureError("morning session start 缺少不可变 items")
    queue_matches = [
        row
        for row in queue_items
        if isinstance(row, dict) and row.get("item_id") == item_id
    ]
    if len(queue_matches) > 1:
        raise CaptureError("morning queue 含重复 item_id")

    linked: dict[str, Any] | None = None
    if queue_matches:
        if linked_followup_event is not None or parent_trigger_event is not None:
            raise CaptureError("queue item 不得伪装成 linked followup")
        item = queue_matches[0]
    else:
        if linked_followup_event is None or parent_trigger_event is None:
            raise CaptureError("dynamic morning item 缺少有界 followup/trigger 证据")
        followup_event, item = session_event(
            linked_followup_event,
            "followup_scheduled",
            "linked-practice followup",
        )
        if item.get("item_id") != item_id:
            raise CaptureError("linked-practice followup item 身份不一致")
        practice_mode = str(item.get("practice_mode") or "")
        expected_kind = (
            "related_original" if practice_mode == "original" else "grounded_variant"
        )
        for label in (
            "scheduling_key",
            "trigger_event_id",
            "parent_item_id",
            "context_slice_id",
            "candidate_id",
            "source_id",
            "source_ref",
            "source_sha256",
            "selection_receipt_ref",
            "selection_receipt_sha256",
            "mechanism_key",
            "knowledge_point",
        ):
            if not str(item.get(label) or ""):
                raise CaptureError(f"followup_scheduled 缺少 {label}")
        if (
            practice_mode not in {"original", "variant"}
            or item.get("item_kind") != expected_kind
        ):
            raise CaptureError("followup_scheduled 的 practice_mode/item_kind 非法")

        trigger = parent_trigger_event
        if (
            trigger.get("schema") != REVIEW_EVENT_SCHEMA
            or trigger.get("event_kind") != "outcome"
            or trigger.get("source") != "morning_review"
            or trigger.get("event_id") != item.get("trigger_event_id")
            or trigger.get("session_id") != session_id
            or trigger.get("item_id") != item.get("parent_item_id")
            or trigger.get("practice_mode")
        ):
            raise CaptureError("followup_scheduled 的 parent trigger 非法")

        source_path, source_ref = _linked_source_path(repo, item.get("source_ref"))
        source_sha = _validate_sha(
            item.get("source_sha256"), "followup_scheduled.source_sha256"
        )
        if _sha256_file(source_path) != source_sha:
            raise CaptureError("linked-practice source 已偏离调度快照")
        for label in (
            "practice_mode",
            "anchor_formal_node_id",
            "variant_of_formal_node_id",
            "trigger_event_id",
            "context_slice_id",
            "candidate_id",
            "source_ref",
            "source_sha256",
            "selection_receipt_ref",
            "selection_receipt_sha256",
        ):
            event_label = (
                "parent_trigger_event_id" if label == "trigger_event_id" else label
            )
            projected = source_ref if label == "source_ref" else item.get(label)
            if projected != event.get(event_label):
                raise CaptureError(
                    f"review event 与 followup_scheduled 的 {label} 不一致"
                )
        for label in ("mechanism_key", "knowledge_point"):
            if item.get(label) != event.get(label):
                raise CaptureError(
                    f"review event 未保真保存 followup_scheduled 的 {label}"
                )
        formal_id = item.get("formal_node_id") or None
        variant_of = item.get("variant_of_formal_node_id") or None
        anchor_formal = item.get("anchor_formal_node_id") or None
        if practice_mode == "original":
            if (
                not formal_id
                or variant_of
                or event.get("formal_node_id") != formal_id
                or event.get("actual_question_formal_node_id") != formal_id
            ):
                raise CaptureError("original followup 的真实原题身份绑定非法")
        elif (
            formal_id
            or not variant_of
            or anchor_formal != variant_of
            or event.get("formal_node_id") is not None
            or event.get("actual_question_formal_node_id") is not None
        ):
            raise CaptureError("variant followup 污染了原题真实作答身份")
        try:
            verified_source = validate_source_for_practice(
                repo,
                source_ref,
                source_sha,
                practice_mode=practice_mode,
                formal_node_id=(str(formal_id) if formal_id else None),
                variant_of_formal_node_id=(str(variant_of) if variant_of else None),
            )
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
                    "anchor_formal_node_id": item.get("anchor_formal_node_id"),
                    "source_id": item.get("source_id"),
                    "source_ref": source_ref,
                    "source_sha256": source_sha,
                    "mechanism_key": item.get("mechanism_key"),
                    "knowledge_point": item.get("knowledge_point"),
                    "review_unit_id": item.get("review_unit_id"),
                },
                source_path=source_path,
            )
        except LinkedPracticeSourceError as exc:
            raise CaptureError(str(exc)) from exc
        if (
            verified_source["path"] != source_path
            or verified_source["source_ref"] != source_ref
        ):
            raise CaptureError("linked-practice source identity 不一致")
        if event.get("followup_event_id") != followup_event.get("event_id"):
            raise CaptureError("review event 未绑定唯一 followup_scheduled 事件")
        linked = {
            "followup_event": followup_event,
            "parent_trigger": trigger,
            "source_path": source_path,
            "source_ref": source_ref,
            "selection_path": verified_selection["path"],
            "selection_ref": verified_selection["selection_receipt_ref"],
        }

    if item.get("source_id") != event.get("source_id"):
        raise CaptureError("review event 与 receipt-bound morning item/source 不一致")
    session_ledger_rel = str(MORNING_SESSION_REL / session_id / "events.jsonl")
    extra_refs = [
        {
            "kind": "morning_session_binding",
            "locator": (
                f"{session_ledger_rel}#sequence={binding_event['sequence']}"
            ),
            "sha256": _sha256_value(binding_event),
        }
    ]
    extra_refs.extend(receipt_refs or [])
    raw = _review_capture_payload_from_verified_evidence(
        repo,
        event,
        policy,
        started=started,
        first_event=first_event,
        queue_path=queue_path,
        queue_rel=queue_rel,
        linked=linked,
        queue_sha256=queue_sha,
        extra_stable_refs=extra_refs,
    )
    return _validate_capture_payload(raw)


def _ordinary_formal_identity(event: dict[str, Any]) -> dict[str, str]:
    """Resolve only a conflict-free formal identity already present in the event."""

    candidates: set[str] = set()
    direct = str(event.get("formal_node_id") or "").strip()
    if direct:
        if not FORMAL_ID_RE.fullmatch(direct):
            raise CaptureError("ordinary review event 的 formal_node_id 非法")
        candidates.add(direct)
    mapped = event.get("mapped_formal_node_ids") or []
    if not isinstance(mapped, list):
        raise CaptureError("ordinary review event 的 mapped_formal_node_ids 非法")
    for value in mapped:
        node_id = str(value or "").strip()
        if not FORMAL_ID_RE.fullmatch(node_id):
            raise CaptureError("ordinary review event 含非法 mapped formal identity")
    route = event.get("route") or {}
    if not isinstance(route, dict):
        raise CaptureError("ordinary review event 的 route 非法")
    if route.get("formal_write_authorized") is not False:
        raise CaptureError("ordinary review event 的 route 不能授权正式写入")
    candidate = route.get("candidate") or {}
    if candidate and not isinstance(candidate, dict):
        raise CaptureError("ordinary review event 的 candidate 非法")
    route_formal = str(candidate.get("formal_node_id") or "").strip()
    if route_formal:
        if not FORMAL_ID_RE.fullmatch(route_formal):
            raise CaptureError("ordinary review event 的 route formal identity 非法")
        if route.get("kind") == "update_existing":
            candidates.add(route_formal)
    if len(candidates) > 1:
        raise CaptureError("ordinary review event 的 formal identity 冲突")
    if candidates:
        formal_id = next(iter(candidates))
        if mapped and formal_id not in mapped:
            raise CaptureError("ordinary review event 的 formal identity 与映射冲突")
        return {
            "status": "existing",
            "formal_id": formal_id,
            "mode": "redo",
            "basis": f"canonical ordinary review event 明确绑定 {formal_id}",
        }
    return {
        "status": "unknown",
        "formal_id": "",
        "mode": "unknown",
        "basis": "正式身份留待日终依据稳定证据核验",
    }


def _ordinary_review_capture_payload(
    event: dict[str, Any],
) -> dict[str, Any]:
    """Build an unconfirmed capture from one ordinary canonical outcome."""

    if (
        event.get("schema") != REVIEW_EVENT_SCHEMA
        or event.get("event_kind") != "outcome"
        or event.get("source") not in ORDINARY_REVIEW_SOURCES
        or event.get("question_valid") is not True
        or event.get("formal_write_authorized") is not False
    ):
        raise CaptureError("只允许绑定有效且未授权正式写入的普通学习 outcome")
    first_result = str(event.get("first_result") or "")
    if first_result not in MORNING_FAILURE_RESULTS:
        raise CaptureError("该普通学习首答结果不属于快速捕获范围")
    event_id = _validate_id(event.get("event_id"), "event_id")
    study_date = _validate_date(
        event.get("observed_date"), "ordinary event.observed_date"
    )
    source_id = _assert_answer_safe(
        "ordinary event.source_id", event.get("source_id")
    )
    if not source_id:
        raise CaptureError("ordinary review event 缺少 source_id")
    identity = _ordinary_formal_identity(event)

    first_break = str(event.get("first_break") or "").strip()
    provenance = _normalize_review_break_provenance(
        event.get("first_break_provenance"), first_break
    )
    if (
        provenance in {"user_report", "visible_evidence"}
        and first_break
        and first_break.lower() not in REVIEW_NONE_BREAKS
    ):
        user_facts = {
            "user_error_entry": first_break,
            "user_error_provenance": provenance,
            "observed_at": str(event.get("event_time") or ""),
        }
    else:
        user_facts = {
            "user_error_entry": "未观察到",
            "user_error_provenance": "not_observed",
            "observed_at": str(event.get("event_time") or ""),
        }

    source = str(event["source"])
    source_facts: dict[str, str] = {
        "question_type": (
            "日常练习首答" if source == "daily_practice" else "晚间 D0 首答"
        ),
        "source_id": source_id,
        "details_id": event_id,
    }
    formal_id = identity["formal_id"]
    if formal_id:
        subject, year, _ = formal_id.split("_", 2)
        source_facts["subject"] = subject
        if year.isdigit():
            source_facts["year"] = year
    else:
        source_facts["subject"] = "未确认"

    missing = []
    if not formal_id:
        missing.append("formal_identity")
    if not event.get("knowledge_point"):
        missing.append("main_knowledge")
    if user_facts["user_error_provenance"] == "not_observed":
        missing.append("user_error_entry")
    context = str(
        event.get("knowledge_point")
        or event.get("mechanism_key")
        or source_id
    )
    event_locator = f"{REVIEW_LOOP_LEDGER_REL}#event_id={event_id}"
    return {
        "schema": SCHEMA,
        "study_date": study_date,
        "timezone": TIMEZONE,
        "idempotency_key": f"ordinary-review-event:{event_id}:fast-capture:v1",
        "stable_evidence_refs": [
            {
                "kind": ORDINARY_REVIEW_REF_KIND,
                "locator": event_locator,
                "sha256": _sha256_value(event),
            }
        ],
        "source_facts": source_facts,
        "user_facts": user_facts,
        "identity_hint": identity,
        "answer_safe_context_anchor": (
            f"{source} {source_id} 的 {context} 非正确首答证据"
        ),
        "missing_fields": missing,
        # A canonical outcome is evidence, not formalization authorization.
        "formalization_authorized": False,
    }


def build_ordinary_review_capture_payload(
    event: dict[str, Any],
) -> dict[str, Any]:
    """Return the authoritative normalized payload for one ordinary outcome.

    The caller must independently prove that ``event`` is the exact committed
    canonical outcome.  This function performs the same answer-safety,
    eligibility, identity, and authorization checks as the legacy cold bridge,
    but does not read either ledger and does not write any state.
    """

    return _validate_capture_payload(_ordinary_review_capture_payload(event))


def _atomic_write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    tmp = Path(temporary)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp, path)
        directory_fd = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
    finally:
        if tmp.exists():
            tmp.unlink()


@contextmanager
def _ledger_lock(root: Path) -> Iterator[None]:
    root.mkdir(parents=True, exist_ok=True)
    ledger = root / "events.jsonl"
    hot_root = root / ".capture-hot-writer-v1"
    if ledger.is_file():
        import capture_hot_writer_408 as hot
        with hot.capture_ledger_lock(ledger) as token:
            before = ledger.stat()
            yield
            after = ledger.stat()
            if hot_root.is_dir() and (
                before.st_ino, before.st_size, before.st_mtime_ns
            ) != (after.st_ino, after.st_size, after.st_mtime_ns):
                # Cold curation and hot capture share truth and must publish one head.
                hot.cold_bootstrap(
                    ledger, root / "state.json", hot_root,
                    lock_token=token, reconcile=True,
                )
        return
    lock_path = root / ".lock"
    with lock_path.open("a+") as handle:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def _load_events(root: Path) -> list[dict[str, Any]]:
    path = root / "events.jsonl"
    if not path.exists():
        return []
    events: list[dict[str, Any]] = []
    for line_no, raw in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        if not raw.strip():
            continue
        try:
            event = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise CaptureError(f"events.jsonl 第 {line_no} 行损坏：{exc}") from exc
        if not isinstance(event, dict) or event.get("schema") != EVENT_SCHEMA:
            raise CaptureError(f"events.jsonl 第 {line_no} 行 schema 非法")
        events.append(event)
    return events


def _event_index(events: list[dict[str, Any]]) -> tuple[set[str], dict[str, dict[str, Any]]]:
    event_ids: set[str] = set()
    by_key: dict[str, dict[str, Any]] = {}
    for event in events:
        event_id = str(event.get("event_id") or "")
        key = str(event.get("idempotency_key") or "")
        if not event_id or event_id in event_ids:
            raise CaptureError(f"账本含重复或空 event_id：{event_id}")
        if not key or key in by_key:
            raise CaptureError(f"账本含重复或空 idempotency_key：{key}")
        event_ids.add(event_id)
        by_key[key] = event
    return event_ids, by_key


def _canonical_fact_identity(event: dict[str, Any]) -> dict[str, str]:
    capture = event.get("capture")
    if not isinstance(capture, dict):
        raise CaptureError("fact_captured 事件缺少 Capture 载荷")
    normalized = _validate_capture_payload(capture)
    if normalized != capture:
        raise CaptureError("fact_captured 载荷不是规范化答案安全形式")
    key = str(normalized["idempotency_key"])
    payload_sha256 = _sha256_value(normalized)
    capture_id = (
        f"CAP-{normalized['study_date'].replace('-', '')}-"
        f"{hashlib.sha256(key.encode()).hexdigest()[:12]}"
    )
    event_id = (
        "CE-"
        + hashlib.sha256(("capture:" + key).encode()).hexdigest()[:20]
    )
    if event.get("schema") != EVENT_SCHEMA or event.get("event_type") != "fact_captured":
        raise CaptureError("fact_captured 事件 schema 非法")
    if event.get("idempotency_key") != key:
        raise CaptureError("fact_captured 事件与载荷幂等身份不一致")
    if event.get("payload_sha256") != payload_sha256:
        raise CaptureError("fact_captured payload_sha256 不一致")
    if event.get("capture_id") != capture_id:
        raise CaptureError("fact_captured Capture ID 不是确定性身份")
    if event.get("event_id") != event_id:
        raise CaptureError("fact_captured event ID 不是确定性身份")
    _parse_timestamp(event.get("created_at"), "fact_captured.created_at")
    return {
        "capture_id": capture_id,
        "event_id": event_id,
        "idempotency_key": key,
        "payload_sha256": payload_sha256,
    }


def _canonical_commit_proofs(
    root: Path, events: list[dict[str, Any]]
) -> dict[str, dict[str, Any]]:
    identities = [
        _canonical_fact_identity(event)
        for event in events
        if event.get("event_type") == "fact_captured"
    ]
    if not identities:
        return {}
    ledger = root / "events.jsonl"
    state_file = root / "state.json"
    hot_root = root / ".capture-hot-writer-v1"
    try:
        import capture_hot_writer_408 as capture_hot
    except ImportError:
        capture_hot = None
    proofs: dict[str, dict[str, Any]] = {}
    if capture_hot is None:
        verification_error = "capture_joint_verifier_unavailable"
        for identity in identities:
            proofs[identity["capture_id"]] = {
                "schema": CANONICAL_COMMIT_PROOF_SCHEMA,
                "status": "unverified",
                **identity,
                "reason_code": verification_error,
                "formal_write_count": 0,
            }
        return proofs
    # The exact verifier is read-only and checks one immutable indexed range,
    # the receipt, state projection, and current joint head.  Do not acquire a
    # second flock here: status/audit may be called by the writer that already
    # owns the ledger lock.  A concurrent generation change simply returns an
    # unverified proof and the local replay refuses that snapshot.
    for identity in identities:
        try:
            verified = capture_hot.verify_joint_commit(
                ledger,
                state_file,
                hot_root,
                event_id=identity["event_id"],
            )
            if (
                verified.get("status") != "PASS"
                or verified.get("event_type") != "fact_captured"
                or verified.get("event_id") != identity["event_id"]
                or verified.get("capture_id") != identity["capture_id"]
                or verified.get("formal_write_count") != 0
            ):
                raise capture_hot.CaptureHotWriterError(
                    "canonical fact proof binding mismatch"
                )
            proofs[identity["capture_id"]] = {
                "schema": CANONICAL_COMMIT_PROOF_SCHEMA,
                "status": "verified",
                **identity,
                "receipt_sha256": verified["receipt_sha256"],
                "event_sha256": verified["event_sha256"],
                "joint_manifest_sha256": verified[
                    "joint_manifest_sha256"
                ],
                "bounded_manifest_sha256": verified[
                    "bounded_manifest_sha256"
                ],
                "formal_write_count": 0,
            }
        except (capture_hot.CaptureHotWriterError, OSError, KeyError):
            proofs[identity["capture_id"]] = {
                "schema": CANONICAL_COMMIT_PROOF_SCHEMA,
                "status": "unverified",
                **identity,
                "reason_code": "capture_joint_receipt_or_index_unverified",
                "formal_write_count": 0,
            }
    return proofs


def _needs_user_resolution_material(event: dict[str, Any]) -> dict[str, Any]:
    material = {
        "resolution_kind": event.get("resolution_kind"),
        "study_date": event.get("study_date"),
        "prior_batch_id": event.get("prior_batch_id"),
        "capture_id": event.get("capture_id"),
        "formal_id": event.get("formal_id"),
        "source_review_event_id": event.get("source_review_event_id"),
        "source_review_event_sha256": event.get("source_review_event_sha256"),
        "confirmation_event_id": event.get("confirmation_event_id"),
        "confirmation_event_sha256": event.get("confirmation_event_sha256"),
        "normal_receipt_sha256": event.get("normal_receipt_sha256"),
        "normal_receipt_transaction_id": event.get(
            "normal_receipt_transaction_id"
        ),
        "normal_receipt_journal_sha256": event.get(
            "normal_receipt_journal_sha256"
        ),
    }
    if material["resolution_kind"] not in {
        "confirmed_identity",
        "confirmed_identity_with_existing_receipt",
    }:
        raise CaptureError("needs_user resolution kind 非法")
    _validate_date(material["study_date"], "resolution.study_date")
    for label in (
        "prior_batch_id",
        "capture_id",
        "source_review_event_id",
        "confirmation_event_id",
    ):
        _validate_id(material[label], f"resolution.{label}")
    if not FORMAL_ID_RE.fullmatch(str(material["formal_id"] or "")):
        raise CaptureError("needs_user resolution formal_id 非法")
    for label in ("source_review_event_sha256", "confirmation_event_sha256"):
        _validate_sha(material[label], f"resolution.{label}")
    receipt_labels = (
        "normal_receipt_sha256",
        "normal_receipt_transaction_id",
        "normal_receipt_journal_sha256",
    )
    if material["resolution_kind"] == "confirmed_identity":
        if any(material[label] is not None for label in receipt_labels):
            raise CaptureError("仅身份确认的 resolution 不得混入 normal receipt")
    else:
        _validate_sha(
            material["normal_receipt_sha256"],
            "resolution.normal_receipt_sha256",
        )
        _validate_id(
            material["normal_receipt_transaction_id"],
            "resolution.normal_receipt_transaction_id",
        )
        _validate_sha(
            material["normal_receipt_journal_sha256"],
            "resolution.normal_receipt_journal_sha256",
        )
    return material


def _needs_user_retirement_material(event: dict[str, Any]) -> dict[str, Any]:
    material = {
        "retirement_schema": event.get("retirement_schema"),
        "study_date": event.get("study_date"),
        "prior_batch_id": event.get("prior_batch_id"),
        "capture_ids": event.get("capture_ids"),
        "capture_set_sha256": event.get("capture_set_sha256"),
        "capture_evidence": event.get("capture_evidence"),
        "close_event_id": event.get("close_event_id"),
        "close_event_sha256": event.get("close_event_sha256"),
        "close_sha256": event.get("close_sha256"),
        "reason": event.get("reason"),
        "explicit_user_confirmation": event.get(
            "explicit_user_confirmation"
        ),
        "formal_write_count": event.get("formal_write_count"),
    }
    if material["retirement_schema"] != NEEDS_USER_RETIREMENT_SCHEMA:
        raise CaptureError("needs_user retirement schema 非法")
    _validate_date(material["study_date"], "retirement.study_date")
    _validate_id(material["prior_batch_id"], "retirement.prior_batch_id")
    capture_ids = material["capture_ids"]
    if not isinstance(capture_ids, list) or not capture_ids:
        raise CaptureError("needs_user retirement capture_ids 必须是非空数组")
    normalized_capture_ids = [
        _validate_id(value, "retirement.capture_ids[]")
        for value in capture_ids
    ]
    if normalized_capture_ids != sorted(set(normalized_capture_ids)):
        raise CaptureError(
            "needs_user retirement capture_ids 必须排序且唯一"
        )
    _validate_sha(
        material["capture_set_sha256"],
        "retirement.capture_set_sha256",
    )
    evidence = material["capture_evidence"]
    if not isinstance(evidence, list) or len(evidence) != len(capture_ids):
        raise CaptureError("needs_user retirement capture_evidence 非法")
    evidence_ids: list[str] = []
    for index, row in enumerate(evidence):
        if not isinstance(row, dict):
            raise CaptureError("needs_user retirement capture evidence 必须是 object")
        capture_id = _validate_id(
            row.get("capture_id"),
            f"retirement.capture_evidence[{index}].capture_id",
        )
        evidence_ids.append(capture_id)
        _validate_sha(
            row.get("payload_sha256"),
            f"retirement.capture_evidence[{index}].payload_sha256",
        )
        _validate_id(
            row.get("result_event_id"),
            f"retirement.capture_evidence[{index}].result_event_id",
        )
        _validate_sha(
            row.get("result_event_sha256"),
            f"retirement.capture_evidence[{index}].result_event_sha256",
        )
        _validate_sha(
            row.get("result_sha256"),
            f"retirement.capture_evidence[{index}].result_sha256",
        )
        if row.get("outcome") != "needs_user":
            raise CaptureError(
                "needs_user retirement 只能绑定原始 needs_user result"
            )
    if evidence_ids != normalized_capture_ids:
        raise CaptureError(
            "needs_user retirement capture_evidence 必须按 capture_ids 对齐"
        )
    _validate_id(material["close_event_id"], "retirement.close_event_id")
    _validate_sha(
        material["close_event_sha256"],
        "retirement.close_event_sha256",
    )
    _validate_sha(material["close_sha256"], "retirement.close_sha256")
    if material["reason"] != NEEDS_USER_RETIREMENT_REASON:
        raise CaptureError("needs_user retirement reason 必须是 user_removed")
    if material["explicit_user_confirmation"] is not True:
        raise CaptureError("needs_user retirement 缺少显式用户确认")
    if material["formal_write_count"] != 0:
        raise CaptureError("needs_user retirement 不得执行正式写入")
    return material


def _neutral_save_material(event: dict[str, Any]) -> dict[str, Any]:
    material = {
        "neutral_save_id": event.get("neutral_save_id"),
        "study_date": event.get("study_date"),
        "timezone": event.get("timezone"),
        "review_event_id": event.get("review_event_id"),
        "review_event_sha256": event.get("review_event_sha256"),
        "source_transaction_kind": event.get("source_transaction_kind"),
        "source_transaction_attestation_sha256": event.get(
            "source_transaction_attestation_sha256"
        ),
        "authorization_message_sha256": event.get(
            "authorization_message_sha256"
        ),
        "authorization_binding_sha256": event.get(
            "authorization_binding_sha256"
        ),
        "save_request_id": event.get("save_request_id"),
        "display_receipt_sha256": event.get("display_receipt_sha256"),
        "save_intent_receipt_sha256": event.get(
            "save_intent_receipt_sha256"
        ),
        "save_intent_receipt_ref": event.get("save_intent_receipt_ref"),
        "authorization_message_size_bytes": event.get(
            "authorization_message_size_bytes"
        ),
        "formalization_authorized": event.get("formalization_authorized"),
        "curation_eligible": event.get("curation_eligible"),
        "status": event.get("status"),
    }
    _validate_id(material["neutral_save_id"], "neutral_save.neutral_save_id")
    _validate_date(material["study_date"], "neutral_save.study_date")
    if material["timezone"] != TIMEZONE:
        raise CaptureError("neutral save timezone 非法")
    _validate_id(material["review_event_id"], "neutral_save.review_event_id")
    for label in (
        "review_event_sha256",
        "source_transaction_attestation_sha256",
        "authorization_message_sha256",
        "authorization_binding_sha256",
        "display_receipt_sha256",
        "save_intent_receipt_sha256",
    ):
        _validate_sha(material[label], f"neutral_save.{label}")
    request_id = str(material["save_request_id"] or "")
    if not SAVE_REQUEST_RE.fullmatch(request_id):
        raise CaptureError("neutral save request_id 非法")
    expected_intent_ref = str(
        MANAGED_RICH_INTENT_OBJECT_REL
        / f"{material['save_intent_receipt_sha256']}.json"
    )
    if material["save_intent_receipt_ref"] != expected_intent_ref:
        raise CaptureError("neutral save intent receipt 引用非法")
    message_size = material["authorization_message_size_bytes"]
    if (
        not isinstance(message_size, int)
        or isinstance(message_size, bool)
        or not 0 < message_size <= MAX_SAVE_AUTHORIZATION_MESSAGE_BYTES
    ):
        raise CaptureError("neutral save authorization message size 非法")
    if material["source_transaction_kind"] not in {
        "prepared_option_turn",
        "rich_morning_turn",
        "explicit_fast_intake_turn",
    }:
        raise CaptureError("neutral save source transaction kind 非法")
    if (
        material["formalization_authorized"] is not False
        or material["curation_eligible"] is not False
        or material["status"] != NEUTRAL_SAVE_STATUS
    ):
        raise CaptureError("neutral save 不得进入错题编纂或正式写入")
    return material


def replay(events: list[dict[str, Any]]) -> dict[str, Any]:
    _event_index(events)
    captures: dict[str, dict[str, Any]] = {}
    neutral_saves: dict[str, dict[str, Any]] = {}
    neutral_review_events: set[str] = set()
    batches: dict[str, dict[str, Any]] = {}
    result_event_ids: dict[tuple[str, str], str] = {}
    result_events: dict[tuple[str, str], dict[str, Any]] = {}
    close_events: dict[str, dict[str, Any]] = {}
    for event in events:
        event_type = event.get("event_type")
        if event_type == "fact_captured":
            canonical_identity = _canonical_fact_identity(event)
            capture_id = canonical_identity["capture_id"]
            if capture_id in captures:
                raise CaptureError(f"重复 capture_id：{capture_id}")
            captures[capture_id] = {
                "capture_id": capture_id,
                "study_date": event["capture"]["study_date"],
                "recorded_at": event["created_at"],
                "payload_sha256": event["payload_sha256"],
                "quality_status": initial_capture_quality_status(
                    event["capture"]
                ),
                "current_batch_id": None,
                "formal_id": event["capture"]["identity_hint"].get("formal_id") or None,
                "last_error": None,
                "formalization_authorized": event["capture"][
                    "formalization_authorized"
                ],
                "capture": event["capture"],
            }
        elif event_type == "capture_supplemented":
            capture_id = _validate_id(event.get("capture_id"), "supplement.capture_id")
            capture = captures.get(capture_id)
            evidence = event.get("evidence")
            if not capture or event.get("study_date") != capture["study_date"]:
                raise CaptureError("supplement 原 episode 日期绑定不一致")
            if (not isinstance(evidence, dict) or set(evidence) != {"locator", "sha256"}
                or not str(evidence.get("locator", "")).startswith("cs408-conversation-package://")
                or not SHA256_RE.fullmatch(str(evidence.get("sha256", "")))):
                raise CaptureError("supplement evidence 非法")
            material = {"capture_id": capture_id, "study_date": capture["study_date"],
                        "evidence": evidence, "parent_evidence": event.get("parent_evidence")}
            if event.get("supplement_sha256") != _sha256_value(material):
                raise CaptureError("supplement 内容哈希不一致")
            if any(batch.get("status") == "active" for batch in batches.values()):
                raise CaptureError("active 批次期间不能追加 supplement")
            history = capture.setdefault("supplement_history", [])
            root_refs = capture["capture"]["stable_evidence_refs"]
            parent = history[-1] if history else {k: root_refs[0][k] for k in ("locator", "sha256")}
            if event.get("parent_evidence") != parent or evidence in history:
                raise CaptureError("supplement parent 漂移或重复")
            if capture.get("formal_id"):
                capture["supplement_prior_formal_id"] = capture["formal_id"]
            history.append(evidence)
            capture.setdefault("supplement_evidence", []).append(evidence)
            capture["quality_status"] = initial_capture_quality_status(capture["capture"])
            capture["current_batch_id"] = None
            capture["last_error"] = None
        elif event_type == "neutral_saved":
            material = _neutral_save_material(event)
            if event.get("payload_sha256") != _sha256_value(material):
                raise CaptureError("neutral save payload_sha256 非法")
            save_id = str(material["neutral_save_id"])
            review_event_id = str(material["review_event_id"])
            if save_id in neutral_saves or review_event_id in neutral_review_events:
                raise CaptureError("同一复盘事件只能有一份中性保存记录")
            neutral_review_events.add(review_event_id)
            neutral_saves[save_id] = dict(material)
        elif event_type == "capture_authorized":
            capture_id = str(event["capture_id"])
            capture = captures.get(capture_id)
            if not capture:
                raise CaptureError(f"授权事件引用未知 capture：{capture_id}")
            if capture["quality_status"] != "captured_unconfirmed":
                raise CaptureError(f"capture 当前状态不能授权：{capture_id}")
            capture["formalization_authorized"] = True
            capture["quality_status"] = PENDING_FORMAL_STATUS
        elif event_type == "needs_user_resolved":
            material = _needs_user_resolution_material(event)
            if event.get("resolution_sha256") != _sha256_value(material):
                raise CaptureError("needs_user resolution_sha256 非法")
            capture_id = str(material["capture_id"])
            capture = captures.get(capture_id)
            if not capture:
                raise CaptureError(f"needs_user resolution 引用未知 capture：{capture_id}")
            if capture["study_date"] != material["study_date"]:
                raise CaptureError("needs_user resolution 与 capture 学习日期不一致")
            if capture["quality_status"] != "needs_user":
                raise CaptureError(
                    f"只有 needs_user capture 可以追加 resolution：{capture_id}"
                )
            prior_batch = batches.get(str(material["prior_batch_id"]))
            if (
                not prior_batch
                or prior_batch.get("status") != "PARTIAL"
                or capture_id not in prior_batch.get("capture_ids", [])
                or (prior_batch.get("results", {}).get(capture_id) or {}).get(
                    "outcome"
                )
                != "needs_user"
                or capture.get("current_batch_id") != prior_batch.get("batch_id")
            ):
                raise CaptureError(
                    "needs_user resolution 未绑定已关闭 PARTIAL 批次的原始缺口"
                )
            capture["quality_status"] = "needs_user_resolved"
            capture["formal_id"] = material["formal_id"]
            capture["last_error"] = None
            capture["resolution"] = material
        elif event_type == "curation_batch_started":
            batch_id = str(event["batch_id"])
            capture_ids = list(event.get("capture_ids") or [])
            if batch_id in batches or not capture_ids:
                raise CaptureError(f"日终批次非法或重复：{batch_id}")
            if any(batch.get("status") == "active" for batch in batches.values()):
                raise CaptureError("同一时刻只允许一个 active 日终批次")
            if capture_ids != sorted(set(capture_ids)):
                raise CaptureError("日终批次 capture_ids 必须排序且唯一")
            study_date = _validate_date(event.get("study_date"), "batch.study_date")
            if event.get("timezone") != TIMEZONE:
                raise CaptureError("日终批次 timezone 非法")
            capture_set_sha = _validate_sha(
                event.get("capture_set_sha256"), "batch.capture_set_sha256"
            )
            if capture_set_sha != _sha256_value(capture_ids):
                raise CaptureError("日终批次 capture_set_sha256 与完整集合不一致")
            batch_schema = event.get("batch_schema")
            if batch_schema not in {
                LEGACY_BATCH_SCHEMA,
                BATCH_SCHEMA_V2,
                BATCH_SCHEMA,
            }:
                raise CaptureError("日终批次 schema 非法")
            attempt = _batch_attempt(event)
            prior_attempts = [
                int(batch.get("attempt", 1))
                for batch in batches.values()
                if batch.get("study_date") == study_date
                and batch.get("capture_set_sha256") == capture_set_sha
            ]
            if attempt != max(prior_attempts, default=0) + 1:
                raise CaptureError("同一冻结集合的日终批次 attempt 不连续")
            if batch_schema in {BATCH_SCHEMA_V2, BATCH_SCHEMA} and batch_id != _batch_id(
                study_date, capture_set_sha, attempt
            ):
                raise CaptureError("日终批次 ID 与日期、集合、attempt 不一致")
            if batch_schema == LEGACY_BATCH_SCHEMA and (
                attempt != 1
                or batch_id
                != f"CUR-{study_date.replace('-', '')}-{capture_set_sha[:12]}"
            ):
                raise CaptureError("legacy 日终批次身份非法")
            supplement_evidence = {
                cid: captures[cid]["supplement_evidence"]
                for cid in capture_ids if cid in captures and captures[cid].get("supplement_evidence")
            }
            supplement_prior_ids = {
                cid: captures[cid].get("supplement_prior_formal_id") for cid in supplement_evidence
            }
            if (event.get("supplement_evidence", {}) != supplement_evidence
                or event.get("supplement_prior_formal_ids", {}) != supplement_prior_ids):
                raise CaptureError("批次 supplement 冻结证据不一致")
            supplement_restart = bool(supplement_evidence) and event.get("retry_of_batch_id") is None
            retry_of_batch_id = event.get("retry_of_batch_id")
            retry_capture_ids = list(event.get("retry_capture_ids") or [])
            carried_capture_ids = list(event.get("carried_capture_ids") or [])
            if attempt == 1 or supplement_restart:
                if retry_of_batch_id is not None or retry_capture_ids or carried_capture_ids:
                    raise CaptureError("首个日终 attempt 不得声明 retry/carry")
                retry_capture_ids = list(capture_ids)
            else:
                if batch_schema != BATCH_SCHEMA:
                    raise CaptureError("只有 v3 日终批次可以表达同集合重试")
                retry_of_batch_id = _validate_id(
                    retry_of_batch_id, "batch.retry_of_batch_id"
                )
                if (
                    retry_capture_ids != sorted(set(retry_capture_ids))
                    or carried_capture_ids != sorted(set(carried_capture_ids))
                    or sorted(retry_capture_ids + carried_capture_ids) != capture_ids
                    or set(retry_capture_ids) & set(carried_capture_ids)
                ):
                    raise CaptureError("日终重试的 retry/carry 分区非法")
                prior_batch = batches.get(retry_of_batch_id)
                if (
                    not prior_batch
                    or prior_batch.get("status") != "PARTIAL"
                    or prior_batch.get("study_date") != study_date
                    or prior_batch.get("capture_set_sha256") != capture_set_sha
                    or prior_batch.get("capture_ids") != capture_ids
                    or int(prior_batch.get("attempt", 1)) != attempt - 1
                    or set(prior_batch.get("results", {})) != set(capture_ids)
                ):
                    raise CaptureError("日终重试未绑定同集合的前一关闭 attempt")
                prior_success = sorted(
                    capture_id
                    for capture_id, value in prior_batch["results"].items()
                    if value.get("outcome") in SUCCESS_OUTCOMES
                )
                prior_retry = sorted(set(capture_ids) - set(prior_success))
                if carried_capture_ids != prior_success or retry_capture_ids != prior_retry:
                    raise CaptureError("日终重试分区与前一 attempt 结果不一致")
            retry_capture_id_set = set(retry_capture_ids)
            carried_capture_id_set = set(carried_capture_ids)
            for capture_id in capture_ids:
                capture = captures.get(capture_id)
                if not capture:
                    raise CaptureError(f"日终批次引用未知 capture：{capture_id}")
                if capture["study_date"] != study_date:
                    raise CaptureError("日终批次不能混入其他学习日期的 capture")
                if not capture["formalization_authorized"]:
                    raise CaptureError("日终批次不能包含未授权 capture")
                if capture_id in retry_capture_id_set:
                    allowed = {
                        PENDING_FORMAL_STATUS,
                        AWAITING_WEB_ENHANCEMENT_STATUS,
                        "curation_failed",
                        "needs_user_resolved",
                    }
                    if capture["quality_status"] not in allowed:
                        raise CaptureError(
                            f"capture 当前状态不能进入日终批次：{capture_id}"
                        )
                    if attempt > 1 and not supplement_restart:
                        prior_outcome = prior_batch["results"][capture_id]["outcome"]
                        if (
                            prior_outcome == "needs_user"
                            and capture["quality_status"] != "needs_user_resolved"
                        ) or (
                            prior_outcome == "failed"
                            and capture["quality_status"] != "curation_failed"
                        ):
                            raise CaptureError("日终重试项未达到对应的可重试状态")
                    capture["quality_status"] = "curating"
                    capture["current_batch_id"] = batch_id
                elif capture_id in carried_capture_id_set:
                    prior_result = prior_batch["results"][capture_id]
                    expected_status = prior_result["outcome"]
                    if (
                        capture["quality_status"] != expected_status
                        or capture.get("formal_id") != prior_result.get("formal_id")
                    ):
                        raise CaptureError("日终重试 carry 项与前一成功结果不一致")
            batch_row = {
                "batch_id": batch_id,
                "study_date": study_date,
                "capture_ids": capture_ids,
                "capture_set_sha256": capture_set_sha,
                "attempt": attempt,
                "started_at": event.get("created_at"),
                "status": "active",
                "results": {},
            }
            observation_contract = event.get("knowledge_observation_contract")
            if observation_contract not in (None, "formal-point-observations-408-v1"):
                raise CaptureError("unknown formal point observation contract")
            if observation_contract:
                batch_row["knowledge_observation_contract"] = observation_contract
            if supplement_evidence:
                batch_row["supplement_evidence"] = {cid: list(refs) for cid, refs in supplement_evidence.items()}
                batch_row["supplement_prior_formal_ids"] = supplement_prior_ids
            if retry_of_batch_id is not None:
                batch_row.update(
                    {
                        "retry_of_batch_id": retry_of_batch_id,
                        "retry_capture_ids": retry_capture_ids,
                        "carried_capture_ids": carried_capture_ids,
                    }
                )
            batches[batch_id] = batch_row
        elif event_type == "curation_item_result":
            batch_id = str(event["batch_id"])
            capture_id = str(event["capture_id"])
            batch = batches.get(batch_id)
            capture = captures.get(capture_id)
            if not batch or not capture or capture_id not in batch["capture_ids"]:
                raise CaptureError("日终结果未绑定有效 batch/capture")
            if batch["status"] != "active":
                raise CaptureError("已关闭的日终批次不能再记录结果")
            if capture_id in batch["results"]:
                raise CaptureError(f"同一批次重复记录 capture 结果：{capture_id}")
            outcome = str(event["outcome"])
            if outcome not in RESULT_OUTCOMES:
                raise CaptureError(f"非法日终结果：{outcome}")
            carried_from_batch_id = event.get("carried_from_batch_id")
            carried_from_result_event_id = event.get("carried_from_result_event_id")
            if carried_from_batch_id is not None:
                if outcome not in SUCCESS_OUTCOMES:
                    raise CaptureError("carry 结果只能复用既有成功终态")
                source_batch = batches.get(str(carried_from_batch_id))
                source = (
                    source_batch.get("results", {}).get(capture_id)
                    if source_batch
                    else None
                )
                if (
                    batch.get("retry_of_batch_id") != carried_from_batch_id
                    or capture_id not in batch.get("carried_capture_ids", [])
                    or not source
                    or source.get("outcome") not in SUCCESS_OUTCOMES
                    or result_event_ids.get(
                        (str(carried_from_batch_id), capture_id)
                    )
                    != carried_from_result_event_id
                    or any(
                        source.get(key) != event.get(key)
                        for key in (
                            "outcome",
                            "formal_id",
                            "receipt_sha256",
                            "verification_sha256",
                            "reason",
                            "concept_source_id",
                            "concept_receipt_sha256",
                            *FORMAL_BACKFLOW_RESULT_FIELDS,
                        )
                    )
                ):
                    raise CaptureError("carry 结果未精确绑定前一 attempt 的成功事件")
            elif capture_id in batch.get("carried_capture_ids", []):
                raise CaptureError("carry capture 缺少来源结果绑定")
            result_row = {
                "outcome": outcome,
                "formal_id": event.get("formal_id"),
                "receipt_sha256": event.get("receipt_sha256"),
                "verification_sha256": event.get("verification_sha256"),
                "reason": event.get("reason"),
            }
            if outcome == "concept_only":
                concept_source_id = str(event.get("concept_source_id") or "")
                concept_receipt_sha256 = str(
                    event.get("concept_receipt_sha256") or ""
                )
                if (
                    not re.fullmatch(r"CR408-[0-9a-f]{64}", concept_source_id)
                    or not SHA256_RE.fullmatch(concept_receipt_sha256)
                ):
                    raise CaptureError("concept_only 结果缺原生 concept-review 绑定")
                result_row.update(
                    {
                        "concept_source_id": concept_source_id,
                        "concept_receipt_sha256": concept_receipt_sha256,
                    }
                )
            if any(
                field in event for field in FORMAL_BACKFLOW_RESULT_FIELDS
            ):
                result_row.update(
                    {
                        field: event.get(field)
                        for field in FORMAL_BACKFLOW_RESULT_FIELDS
                    }
                )
            if carried_from_batch_id is not None:
                result_row.update(
                    {
                        "carried_from_batch_id": carried_from_batch_id,
                        "carried_from_result_event_id": carried_from_result_event_id,
                    }
                )
            if outcome in SUCCESS_OUTCOMES and capture_id in batch.get("supplement_evidence", {}):
                prior_id = batch.get("supplement_prior_formal_ids", {}).get(capture_id)
                if prior_id and event.get("formal_id") != prior_id:
                    raise CaptureError("supplement 成功结果不得改变既有 formal_id")
                consumed = batch["supplement_evidence"][capture_id]
                result_row["consumed_supplement_evidence"] = list(consumed)
                capture["supplement_evidence"] = [
                    ref for ref in capture.get("supplement_evidence", []) if ref not in consumed
                ]
            batch["results"][capture_id] = result_row
            result_event_ids[(batch_id, capture_id)] = str(event.get("event_id") or "")
            result_events[(batch_id, capture_id)] = event
            capture["current_batch_id"] = batch_id
            if outcome == "curated":
                capture["quality_status"] = "curated"
                capture["formal_id"] = event["formal_id"]
                capture["last_error"] = None
            elif outcome == "already_current":
                capture["quality_status"] = "already_current"
                capture["formal_id"] = event["formal_id"]
                capture["last_error"] = None
            elif outcome == "concept_only":
                capture["quality_status"] = "concept_only"
                capture["formal_id"] = None
                capture["last_error"] = None
            elif outcome == "needs_user":
                capture["quality_status"] = "needs_user"
                capture["last_error"] = event.get("reason")
            else:
                capture["quality_status"] = "curation_failed"
                capture["last_error"] = event.get("reason")
        elif event_type == "curation_batch_closed":
            batch_id = str(event["batch_id"])
            batch = batches.get(batch_id)
            if not batch:
                raise CaptureError(f"关闭未知日终批次：{batch_id}")
            if batch["status"] != "active":
                raise CaptureError(f"日终批次已关闭：{batch_id}")
            missing = sorted(set(batch["capture_ids"]) - set(batch["results"]))
            if missing:
                raise CaptureError(f"日终批次收尾前仍缺结果：{missing}")
            global_closeout = str(event.get("global_closeout"))
            if global_closeout not in {"pass", "failed"}:
                raise CaptureError("日终批次 global_closeout 非法")
            failed = sorted(
                capture_id
                for capture_id, value in batch["results"].items()
                if value["outcome"] == "failed"
            )
            needs_user = sorted(
                capture_id
                for capture_id, value in batch["results"].items()
                if value["outcome"] == "needs_user"
            )
            expected_status = (
                "COMPLETE"
                if global_closeout == "pass" and not failed and not needs_user
                else "PARTIAL"
            )
            close_contract = event.get("close_contract")
            audit_receipt_sha = event.get("global_audit_receipt_sha256")
            if close_contract is None:
                # Historical v1/v2 close events predate aggregate audit receipts.
                # They remain replayable but are never emitted by new code.
                if audit_receipt_sha is not None:
                    raise CaptureError("legacy 日终收尾不得混入 aggregate receipt")
            elif close_contract == GLOBAL_AUDIT_CLOSE_CONTRACT:
                if global_closeout == "pass":
                    if failed:
                        raise CaptureError(
                            "failed 逐题结果不得声明 global_closeout=pass"
                        )
                    _validate_sha(
                        audit_receipt_sha,
                        "close.global_audit_receipt_sha256",
                    )
                elif audit_receipt_sha is not None:
                    raise CaptureError("failed 日终收尾不得声明 PASS audit receipt")
            else:
                raise CaptureError("日终批次 close_contract 非法")
            close_payload = {
                "batch_id": batch_id,
                "status": expected_status,
                "global_closeout": global_closeout,
                "needs_user_capture_ids": needs_user,
                "failed_capture_ids": failed,
            }
            if close_contract is not None:
                close_payload.update(
                    {
                        "close_contract": close_contract,
                        "global_audit_receipt_sha256": audit_receipt_sha,
                    }
                )
            if any(event.get(key) != value for key, value in close_payload.items()):
                raise CaptureError("日终批次收尾字段与逐题结果不一致")
            if event.get("close_sha256") != _sha256_value(close_payload):
                raise CaptureError("日终批次 close_sha256 非法")
            batch["status"] = expected_status
            batch["global_closeout"] = global_closeout
            batch["close_contract"] = close_contract or "legacy_unsealed"
            batch["global_audit_receipt_sha256"] = audit_receipt_sha
            batch["closed_at"] = event["created_at"]
            close_events[batch_id] = event
        elif event_type == "needs_user_retired":
            material = _needs_user_retirement_material(event)
            if event.get("retirement_sha256") != _sha256_value(material):
                raise CaptureError("needs_user retirement_sha256 非法")
            study_date = str(material["study_date"])
            prior_batch_id = str(material["prior_batch_id"])
            capture_ids = list(material["capture_ids"])
            if any(
                batch.get("status") == "active" for batch in batches.values()
            ):
                raise CaptureError(
                    "needs_user retirement 前不得存在 active 日终批次"
                )
            prior_batch = batches.get(prior_batch_id)
            if (
                not isinstance(prior_batch, dict)
                or prior_batch.get("study_date") != study_date
            ):
                raise CaptureError(
                    "needs_user retirement 未绑定显式日期的原始批次"
                )
            if prior_batch.get("status") != "PARTIAL":
                raise CaptureError(
                    "needs_user retirement 只能绑定已关闭 PARTIAL 批次"
                )
            if (
                material["capture_set_sha256"]
                != prior_batch.get("capture_set_sha256")
            ):
                raise CaptureError(
                    "needs_user retirement 与原批次 capture_set_sha256 不一致"
                )
            same_set_batches = [
                batch
                for batch in batches.values()
                if batch.get("study_date") == study_date
                and batch.get("capture_set_sha256")
                == prior_batch.get("capture_set_sha256")
            ]
            if (
                not same_set_batches
                or same_set_batches[-1].get("batch_id") != prior_batch_id
            ):
                raise CaptureError(
                    "needs_user retirement 必须绑定同一冻结集合的最新 attempt"
                )
            non_success_ids = sorted(
                capture_id
                for capture_id, row in prior_batch.get("results", {}).items()
                if row.get("outcome") not in SUCCESS_OUTCOMES
            )
            if capture_ids != non_success_ids:
                raise CaptureError(
                    "needs_user retirement 必须等于最新 PARTIAL 批次完整非成功集合"
                )
            if any(
                prior_batch["results"][capture_id].get("outcome")
                != "needs_user"
                for capture_id in non_success_ids
            ):
                raise CaptureError(
                    "needs_user retirement 的全部非成功结果必须是 needs_user"
                )
            expected_evidence: list[dict[str, Any]] = []
            for capture_id in capture_ids:
                capture = captures.get(capture_id)
                if (
                    not capture
                    or capture.get("study_date") != study_date
                    or capture.get("current_batch_id") != prior_batch_id
                    or capture.get("quality_status") != "needs_user"
                ):
                    raise CaptureError(
                        "needs_user retirement 项必须是同批次同日期的当前 needs_user"
                    )
                result_event = result_events.get((prior_batch_id, capture_id))
                if not isinstance(result_event, dict):
                    raise CaptureError(
                        "needs_user retirement 缺少原始 result event"
                    )
                expected_evidence.append(
                    {
                        "capture_id": capture_id,
                        "payload_sha256": capture["payload_sha256"],
                        "result_event_id": result_event.get("event_id"),
                        "result_event_sha256": _sha256_value(result_event),
                        "result_sha256": result_event.get("result_sha256"),
                        "outcome": result_event.get("outcome"),
                    }
                )
            if material["capture_evidence"] != expected_evidence:
                raise CaptureError(
                    "needs_user retirement 与原始 capture/result evidence 不一致"
                )
            close_event = close_events.get(prior_batch_id)
            if not isinstance(close_event, dict):
                raise CaptureError(
                    "needs_user retirement 缺少原始 batch close event"
                )
            if (
                material["close_event_id"] != close_event.get("event_id")
                or material["close_event_sha256"]
                != _sha256_value(close_event)
                or material["close_sha256"]
                != close_event.get("close_sha256")
            ):
                raise CaptureError(
                    "needs_user retirement 与原始 close event evidence 不一致"
                )
            retirement_projection = {
                **material,
                "retirement_sha256": event["retirement_sha256"],
                "retirement_event_id": event.get("event_id"),
            }
            prior_batch["needs_user_retirement"] = retirement_projection
            for capture_id in capture_ids:
                captures[capture_id]["quality_status"] = "user_removed"
                captures[capture_id]["retirement"] = retirement_projection
        else:
            raise CaptureError(f"未知 event_type：{event_type}")
    pending_by_date: dict[str, list[str]] = {}
    for capture_id, capture in captures.items():
        if capture["quality_status"] in {
            PENDING_FORMAL_STATUS,
            AWAITING_WEB_ENHANCEMENT_STATUS,
            "curation_failed",
            "needs_user_resolved",
        }:
            current_batch = batches.get(str(capture.get("current_batch_id") or ""))
            if current_batch and current_batch.get("status") == "active":
                continue
            pending_by_date.setdefault(capture["study_date"], []).append(capture_id)
    for values in pending_by_date.values():
        values.sort()
    state = {
        "schema": STATE_SCHEMA,
        "event_count": len(events),
        "captures": captures,
        "batches": batches,
        "pending_by_date": pending_by_date,
    }
    if neutral_saves:
        state["neutral_saves"] = neutral_saves
    return state


def _append_events(
    root: Path,
    events: list[dict[str, Any]],
    additions: list[dict[str, Any]],
) -> None:
    if not additions:
        raise CaptureError("追加事件集合不能为空")
    updated = [*events, *additions]
    state = replay(updated)
    ledger = root / "events.jsonl"
    if ledger.exists():
        with ledger.open("rb") as handle:
            handle.seek(0, os.SEEK_END)
            if handle.tell():
                handle.seek(-1, os.SEEK_END)
                if handle.read(1) != b"\n":
                    raise CaptureError("capture ledger 尾行不完整")
    ledger_text = "".join(
        json.dumps(item, ensure_ascii=False, sort_keys=True) + "\n" for item in additions
    )
    with ledger.open("ab") as handle:
        handle.write(ledger_text.encode("utf-8"))
        handle.flush()
        os.fsync(handle.fileno())
    _atomic_write(
        root / "state.json",
        json.dumps(state, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
    )


def _append_event(root: Path, events: list[dict[str, Any]], event: dict[str, Any]) -> None:
    _append_events(root, events, [event])


def append_standalone_supplement(
    capture_id: str, *, evidence: dict[str, str], parent_evidence: dict[str, str],
    repo_root: str | Path, package_root: str | Path,
    fault_injector: Any = None,
) -> dict[str, Any]:
    """Append an immutable evidence revision to one existing standalone episode."""
    return _append_episode_supplement(
        capture_id, evidence=evidence, parent_evidence=parent_evidence,
        repo_root=repo_root, package_root=package_root, fault_injector=fault_injector,
        campaign=False,
    )


def append_campaign_supplement(
    capture_id: str, *, evidence: dict[str, str], parent_evidence: dict[str, str],
    repo_root: str | Path, package_root: str | Path,
    fault_injector: Any = None,
) -> dict[str, Any]:
    """Attach evidence to a captured v2 campaign item without changing its outcome."""
    return _append_episode_supplement(
        capture_id, evidence=evidence, parent_evidence=parent_evidence,
        repo_root=repo_root, package_root=package_root, fault_injector=fault_injector,
        campaign=True,
    )


def _append_episode_supplement(
    capture_id: str, *, evidence: dict[str, str], parent_evidence: dict[str, str],
    repo_root: str | Path, package_root: str | Path, campaign: bool,
    fault_injector: Any = None,
) -> dict[str, Any]:
    import conversation_package_408 as packages
    import capture_hot_writer_408 as hot
    capture_id = _validate_id(capture_id, "capture_id")
    opened = packages.verify_package_binding(evidence["locator"],
        expected_canonical_sha256=evidence["sha256"], require_sealed=True,
        private_root=package_root)
    source = opened["source"]["source"]
    root = capture_root(repo_root)
    ledger, state_file = root / "events.jsonl", root / "state.json"
    with hot.capture_ledger_lock(ledger) as token:
        events = _load_events(root)
        state = replay(events)
        capture = state["captures"].get(capture_id)
        if not capture:
            raise CaptureError("supplement capture 不存在")
        refs = capture["capture"]["stable_evidence_refs"]
        if len(refs) != 1 or refs[0].get("kind") != CONVERSATION_PACKAGE_REF_KIND:
            raise CaptureError("supplement 原 capture 必须绑定唯一完整包")
        origin = packages.verify_package_binding(refs[0]["locator"],
            expected_canonical_sha256=refs[0]["sha256"], require_sealed=True,
            private_root=package_root)
        origin_kind = "study_episode_capture" if campaign else "standalone_question_intake"
        supplement_kind = "study_episode_capture" if campaign else "standalone_question_supplement"
        if (origin["source"]["source"].get("entry_kind") != origin_kind
            or source.get("entry_kind") != supplement_kind
            or source.get("episode_capture_id") != capture_id
            or source.get("root_package_locator") != refs[0]["locator"]
            or source.get("root_package_sha256") != refs[0]["sha256"]
            or {"locator": source.get("parent_package_locator"), "sha256": source.get("parent_package_sha256")} != parent_evidence
            or opened["manifest"]["study_date"] != capture["study_date"]
            or opened["manifest"]["item_id"] != origin["manifest"]["item_id"]):
            raise CaptureError("supplement 原 episode 绑定不一致")
        parent = packages.verify_package_binding(parent_evidence["locator"],
            expected_canonical_sha256=parent_evidence["sha256"], require_sealed=True,
            private_root=package_root)
        root_source = origin["source"]["source"]
        if campaign:
            binding = root_source.get("display_binding") or {}
            authorization = root_source.get("authorization") or {}
            original_source = root_source.get("source") or {}
            if (original_source.get("origin_lane") not in {"campaign_original", "campaign_variant"}
                    or source.get('evidence_revision_kind') != 'campaign_episode_supplement'
                    or not binding.get("display_receipt_sha256")
                    or authorization.get("policy") != "morning-campaign-saving-contract-v2"
                    or authorization.get("per_item_capture_authorized") is not True):
                raise CaptureError("supplement requires a captured campaign v2 episode")
        if any(source.get(k) != v for k, v in root_source.items() if k != "entry_kind"):
            raise CaptureError("supplement 不得改写原题来源或身份")
        old_turns = parent["conversation"]["turns"]
        if (opened["conversation"]["turns"][:len(old_turns)] != old_turns
            or len(opened["conversation"]["turns"]) <= len(old_turns)):
            raise CaptureError("supplement 必须完整保留旧对话并追加新轮次")
        new_attachments = {(row["sha256"], row["role"], row["mime_type"], row["label"])
                           for row in opened["manifest"]["attachments"]}
        if any((row["sha256"], row["role"], row["mime_type"], row["label"]) not in new_attachments
               for row in parent["manifest"]["attachments"]):
            raise CaptureError("supplement 不得遗漏旧附件")
        material = {"capture_id": capture_id, "study_date": capture["study_date"],
                    "evidence": evidence, "parent_evidence": parent_evidence}
        digest = _sha256_value(material)
        key = ("campaign-supplement:" if campaign else "standalone-supplement:") + digest
        existing = next((e for e in events if e.get("idempotency_key") == key), None)
        if existing is None:
            event = {"schema": EVENT_SCHEMA, "event_type": "capture_supplemented",
                     "event_id": "CE-" + digest[:20], "idempotency_key": key,
                     "created_at": _now_utc(), "supplement_sha256": digest, **material}
            state = replay([*events, event])  # validates parent/frontier before append
            previous = ledger.read_bytes()
            if previous and not previous.endswith(b"\n"):
                raise CaptureError("capture ledger 尾行不完整")
            # Preserve every historical byte; only the new event is appended.
            with ledger.open("ab") as handle:
                handle.write((json.dumps(event, ensure_ascii=False, sort_keys=True) + "\n").encode())
                handle.flush()
                os.fsync(handle.fileno())
            if fault_injector:
                fault_injector("after_supplement_event")
        elif existing.get("supplement_sha256") != digest:
            raise CaptureError("supplement 幂等键冲突")
        _atomic_write(state_file, json.dumps(state, ensure_ascii=False, sort_keys=True, indent=2) + "\n")
        hot.cold_bootstrap(ledger, state_file, root / ".capture-hot-writer-v1",
                           lock_token=token, reconcile=True)
        reopened = replay(_load_events(root))
        if evidence not in reopened["captures"][capture_id].get("supplement_history", []):
            raise CaptureError("supplement durable readback 失败")
    return {"capture_id": capture_id, "supplement_sha256": digest,
            "study_date": capture["study_date"], "event_id": "CE-" + digest[:20],
            "formal_write_count": 0}


def _capture_value(raw: Any, *, repo_root: str | Path = ".") -> dict[str, Any]:
    payload = _validate_capture_payload(raw)
    root = capture_root(repo_root)
    payload_hash = _sha256_value(payload)
    key = payload["idempotency_key"]
    capture_id = f"CAP-{payload['study_date'].replace('-', '')}-{hashlib.sha256(key.encode()).hexdigest()[:12]}"
    with _ledger_lock(root):
        events = _load_events(root)
        _, by_key = _event_index(events)
        existing = by_key.get(key)
        if existing:
            if existing.get("event_type") != "fact_captured" or existing.get("payload_sha256") != payload_hash:
                raise CaptureError("idempotency_key 已被不同事实载荷使用")
            current = replay(events)["captures"][existing["capture_id"]]
            return {
                "status": "ALREADY_CAPTURED",
                "capture_id": existing["capture_id"],
                "quality_status": current["quality_status"],
                "payload_sha256": payload_hash,
                "created": False,
                "formal_write_count": 0,
            }
        event = {
            "schema": EVENT_SCHEMA,
            "event_type": "fact_captured",
            "event_id": f"CE-{hashlib.sha256(('capture:' + key).encode()).hexdigest()[:20]}",
            "idempotency_key": key,
            "created_at": _now_utc(),
            "capture_id": capture_id,
            "payload_sha256": payload_hash,
            "capture": payload,
        }
        _append_event(root, events, event)
    return {
        "status": (
            "CAPTURED_PENDING_FORMALIZATION"
            if payload["formalization_authorized"]
            else "CAPTURED_UNCONFIRMED"
        ),
        "capture_id": capture_id,
        "quality_status": initial_capture_quality_status(payload),
        "payload_sha256": payload_hash,
        "created": True,
        "formal_write_count": 0,
    }


def capture(payload_path: str | Path, *, repo_root: str | Path = ".") -> dict[str, Any]:
    return _capture_value(_read_json(payload_path), repo_root=repo_root)


def capture_value(
    payload: dict[str, Any], *, repo_root: str | Path = "."
) -> dict[str, Any]:
    """Commit one already in-memory answer-safe capture payload.

    This is the narrow public entry for deterministic local producers that have
    already sealed their private evidence.  It deliberately exposes no option
    to bypass the canonical payload validator or the idempotency ledger.
    """

    return _capture_value(payload, repo_root=repo_root)


def reopen_capture_binding(
    capture_id: str,
    *,
    expected_payload_sha256: str,
    expected_idempotency_key: str,
    repo_root: str | Path = ".",
    include_payload_proof: bool = False,
) -> dict[str, Any]:
    """Reopen one exact fact event and return only its safe binding proof."""

    capture_value_id = _validate_id(capture_id, "capture_id")
    payload_sha = _validate_sha(
        expected_payload_sha256, "expected_payload_sha256"
    )
    idempotency_key = _validate_id(
        expected_idempotency_key, "expected_idempotency_key"
    )
    root = capture_root(repo_root)
    with _ledger_lock(root):
        events = _load_events(root)
        matches = [
            event
            for event in events
            if event.get("event_type") == "fact_captured"
            and event.get("capture_id") == capture_value_id
        ]
        if len(matches) != 1:
            raise CaptureError("未找到唯一 fact capture 绑定")
        event = matches[0]
        identity = _canonical_fact_identity(event)
        if (
            identity["payload_sha256"] != payload_sha
            or identity["idempotency_key"] != idempotency_key
        ):
            raise CaptureError("fact capture 与预期 payload/request 绑定不一致")
        state = replay(events)
        current = state["captures"].get(capture_value_id)
        if not isinstance(current, dict):
            raise CaptureError("fact capture 未形成可重放状态")
        capture_payload = event.get("capture")
        if not isinstance(capture_payload, dict):
            raise CaptureError("fact capture 载荷缺失")
        refs = capture_payload.get("stable_evidence_refs")
        if not isinstance(refs, list):
            raise CaptureError("fact capture 稳定证据绑定缺失")
        return {
            "schema": "intake_fact_capture_binding_v1",
            "capture_id": capture_value_id,
            "event_id": identity["event_id"],
            "event_sha256": _sha256_value(event),
            "capture_committed_at_utc": _parse_timestamp(
                event.get("created_at"), "fact_captured.created_at"
            ).isoformat(),
            "idempotency_key": idempotency_key,
            "payload_sha256": payload_sha,
            "stable_evidence_refs": refs,
            **(
                {
                    "source_facts": capture_payload.get("source_facts") or {},
                    "authorization_policy": capture_payload.get(
                        "authorization_policy"
                    ),
                }
                if include_payload_proof
                else {}
            ),
            "quality_status": current["quality_status"],
            "formalization_authorized": current[
                "formalization_authorized"
            ],
            "formal_write_count": 0,
        }


def capture_review_event(
    event_id: str, *, repo_root: str | Path = "."
) -> dict[str, Any]:
    repo = _resolve_repo(repo_root)
    policy = _load_backflow_policy(repo)
    event = _review_event_by_id(repo, event_id)
    payload = _review_capture_payload(repo, event, policy)
    result = _capture_value(payload, repo_root=repo)
    return {
        **result,
        "review_event_id": event["event_id"],
        "study_date": payload["study_date"],
        "policy_schema": policy["schema"],
        "formal_write_authorized": False,
    }


def build_neutral_review_event(
    event_id: str,
    *,
    source_transaction_kind: str,
    source_transaction_attestation_sha256: str,
    authorization_message_sha256: str,
    authorization_binding_sha256: str,
    save_request_id: str,
    display_receipt_sha256: str,
    save_intent_receipt_sha256: str,
    save_intent_receipt_ref: str,
    authorization_message_size_bytes: int,
    repo_root: str | Path = ".",
    created_at: str | None = None,
) -> dict[str, Any]:
    """Build one canonical neutral-save event without changing capture state."""

    repo = _resolve_repo(repo_root)
    event = _review_event_by_id(repo, event_id)
    if (
        event.get("event_kind") != "outcome"
        or event.get("source") not in {"morning_review", "daily_practice"}
        or event.get("question_valid") is not True
        or event.get("formal_write_authorized") is not False
        or event.get("first_result") not in MORNING_CORRECT_RESULTS
    ):
        raise CaptureError("只有有效的正确首答可以中性保存")
    study_date = _validate_date(
        event.get("observed_date"), "neutral_save.observed_date"
    )
    event_id_value = _validate_id(event.get("event_id"), "review_event_id")
    transaction_kind = str(source_transaction_kind or "")
    allowed_kind = {
        "morning_review": {"prepared_option_turn", "rich_morning_turn"},
        "daily_practice": {"explicit_fast_intake_turn"},
    }
    if transaction_kind not in allowed_kind[str(event["source"])]:
        raise CaptureError("neutral save source transaction kind 非法")
    transaction_sha = _validate_sha(
        source_transaction_attestation_sha256,
        "source_transaction_attestation_sha256",
    )
    message_sha = _validate_sha(
        authorization_message_sha256, "authorization_message_sha256"
    )
    binding_sha = _validate_sha(
        authorization_binding_sha256, "authorization_binding_sha256"
    )
    request_id = str(save_request_id or "")
    if not SAVE_REQUEST_RE.fullmatch(request_id):
        raise CaptureError("neutral save request_id 非法")
    display_sha = _validate_sha(
        display_receipt_sha256, "display_receipt_sha256"
    )
    intent_sha = _validate_sha(
        save_intent_receipt_sha256, "save_intent_receipt_sha256"
    )
    intent_ref = str(save_intent_receipt_ref or "")
    expected_intent_ref = str(
        MANAGED_RICH_INTENT_OBJECT_REL / f"{intent_sha}.json"
    )
    if intent_ref != expected_intent_ref:
        raise CaptureError("neutral save intent receipt 引用非法")
    if (
        not isinstance(authorization_message_size_bytes, int)
        or isinstance(authorization_message_size_bytes, bool)
        or not 0
        < authorization_message_size_bytes
        <= MAX_SAVE_AUTHORIZATION_MESSAGE_BYTES
    ):
        raise CaptureError("neutral save authorization message size 非法")
    idempotency_key = f"neutral-save:{event_id_value}:v1"
    neutral_save_id = (
        f"NS-{study_date.replace('-', '')}-"
        f"{hashlib.sha256(idempotency_key.encode()).hexdigest()[:12]}"
    )
    material = {
        "neutral_save_id": neutral_save_id,
        "study_date": study_date,
        "timezone": TIMEZONE,
        "review_event_id": event_id_value,
        "review_event_sha256": _sha256_value(event),
        "source_transaction_kind": transaction_kind,
        "source_transaction_attestation_sha256": transaction_sha,
        "authorization_message_sha256": message_sha,
        "authorization_binding_sha256": binding_sha,
        "save_request_id": request_id,
        "display_receipt_sha256": display_sha,
        "save_intent_receipt_sha256": intent_sha,
        "save_intent_receipt_ref": intent_ref,
        "authorization_message_size_bytes": authorization_message_size_bytes,
        "formalization_authorized": False,
        "curation_eligible": False,
        "status": NEUTRAL_SAVE_STATUS,
    }
    payload_sha = _sha256_value(material)
    event_row = {
        "schema": EVENT_SCHEMA,
        "event_type": "neutral_saved",
        "event_id": (
            "NSE-"
            + hashlib.sha256(("neutral:" + event_id_value).encode()).hexdigest()[:20]
        ),
        "idempotency_key": idempotency_key,
        "created_at": created_at or _now_utc(),
        "payload_sha256": payload_sha,
        **material,
    }
    if _neutral_save_material(event_row) != material:
        raise CaptureError("neutral save canonical event 非法")
    return event_row


def save_neutral_review_event(
    event_id: str,
    *,
    source_transaction_kind: str,
    source_transaction_attestation_sha256: str,
    authorization_message_sha256: str,
    authorization_binding_sha256: str,
    save_request_id: str,
    display_receipt_sha256: str,
    save_intent_receipt_sha256: str,
    save_intent_receipt_ref: str,
    authorization_message_size_bytes: int,
    repo_root: str | Path = ".",
) -> dict[str, Any]:
    """Durably preserve one correct morning result without creating a wrong item.

    The managed caller must first validate the current authorization message and
    the completed answer transaction.  This ledger stores only their hashes and
    the canonical review event identity; it never stores the message body.
    """

    repo = _resolve_repo(repo_root)
    event_row = build_neutral_review_event(
        event_id,
        source_transaction_kind=source_transaction_kind,
        source_transaction_attestation_sha256=(
            source_transaction_attestation_sha256
        ),
        authorization_message_sha256=authorization_message_sha256,
        authorization_binding_sha256=authorization_binding_sha256,
        save_request_id=save_request_id,
        display_receipt_sha256=display_receipt_sha256,
        save_intent_receipt_sha256=save_intent_receipt_sha256,
        save_intent_receipt_ref=save_intent_receipt_ref,
        authorization_message_size_bytes=authorization_message_size_bytes,
        repo_root=repo,
    )
    root = capture_root(repo)
    root.mkdir(parents=True, exist_ok=True)
    ledger = root / "events.jsonl"
    state_path = root / "state.json"
    if not ledger.exists() and not state_path.exists():
        _atomic_write(ledger, "")
        _atomic_write(
            state_path,
            json.dumps(replay([]), ensure_ascii=False, indent=2, sort_keys=True)
            + "\n",
        )
    elif not ledger.is_file() or not state_path.is_file():
        raise CaptureError("中性保存事实账本未完整初始化")
    hot_root = root / ".capture-hot-writer-v1"
    try:
        import capture_hot_writer_408 as capture_hot

        with capture_hot.capture_ledger_lock(ledger) as lock_token:
            if not (hot_root / capture_hot.JOINT_MANIFEST_FILENAME).is_file():
                capture_hot.cold_bootstrap(
                    ledger,
                    state_path,
                    hot_root,
                    lock_token=lock_token,
                )
            committed = capture_hot.save_neutral_event(
                ledger,
                state_path,
                hot_root,
                lock_token=lock_token,
                event=event_row,
            )
    except (ImportError, OSError, RuntimeError) as exc:
        raise CaptureError(f"中性保存热事务失败：{exc}") from exc
    if committed.get("status") not in {
        "SAVED_NEUTRAL",
        "ALREADY_SAVED_NEUTRAL",
    } or not isinstance(committed.get("neutral_material"), dict):
        raise CaptureError("中性保存热事务终态非法")
    return {
        "event_status": committed["status"],
        **dict(committed["neutral_material"]),
        "created": bool(committed.get("created")),
        "receipt_sha256": committed.get("receipt_sha256"),
        "formal_write_authorized": False,
    }


def capture_ordinary_review_event(
    event_id: str, *, repo_root: str | Path = "."
) -> dict[str, Any]:
    """Capture ordinary review evidence without inferring curation authority."""

    repo = _resolve_repo(repo_root)
    event = _review_event_by_id(repo, event_id)
    payload = _ordinary_review_capture_payload(event)
    result = _capture_value(payload, repo_root=repo)
    return {
        **result,
        "ordinary_review_event_id": event["event_id"],
        "review_source": event["source"],
        "study_date": payload["study_date"],
        "formalization_authorized": False,
        "formal_write_authorized": False,
    }


def authorize_capture(
    *,
    capture_id: str,
    idempotency_key: str,
    repo_root: str | Path = ".",
) -> dict[str, Any]:
    """Rejected legacy surface retained only for a precise migration error."""

    raise CaptureError(
        "直接 capture_id/idempotency_key 授权已禁用；"
        "新授权必须经 managed current-message intent 入口"
    )


def authorize_capture_hot(
    *,
    capture_id: str,
    idempotency_key: str,
    repo_root: str | Path = ".",
    hot_root: str | Path | None = None,
    full: bool = False,
) -> dict[str, Any]:
    """Rejected legacy surface; it can no longer append an authorization."""

    raise CaptureError(
        "直接 capture hot 授权已禁用；恢复只能核验已存在的 canonical authorization event"
    )


def recover_existing_legacy_authorization(
    *,
    capture_id: str,
    authorization_event_id: str,
    authorization_event_sha256: str,
    fact_receipt_sha256: str,
    repo_root: str | Path = ".",
    hot_root: str | Path | None = None,
) -> dict[str, Any]:
    """Recover projections for one already-present proofless legacy event.

    This named cold-path recovery never appends an authorization.  It requires
    the exact existing legacy event line and the deterministic fact-capture
    commit receipt, then rebuilds only replayable capture state and hot indexes.
    """

    capture_id = _validate_id(capture_id, "capture_id")
    authorization_event_id = _validate_id(
        authorization_event_id, "authorization_event_id"
    )
    _validate_sha(
        authorization_event_sha256, "authorization_event_sha256"
    )
    _validate_sha(fact_receipt_sha256, "fact_receipt_sha256")
    repo = _resolve_repo(repo_root)
    root = capture_root(repo)
    ledger = root / "events.jsonl"
    state = root / "state.json"
    if not ledger.is_file() or not state.is_file():
        raise CaptureError("capture truth files 尚未准备")
    try:
        import capture_commit_index_408 as capture_commit
        import capture_hot_writer_408 as capture_hot
    except ImportError as exc:
        raise CaptureError("capture recovery runtime 不可用") from exc
    derived = (
        Path(hot_root).expanduser()
        if hot_root is not None
        else root / ".capture-hot-writer-v1"
    )
    if not derived.is_absolute():
        derived = repo / derived
    derived = derived.resolve()
    try:
        derived.relative_to(repo)
    except ValueError as exc:
        raise CaptureError("capture hot root 越出仓库") from exc
    with capture_hot.capture_ledger_lock(ledger) as token:
        raw_ledger = ledger.read_bytes()
        events: list[dict[str, Any]] = []
        matched_line: bytes | None = None
        for raw_line in raw_ledger.splitlines(keepends=True):
            if not raw_line.strip():
                continue
            if not raw_line.endswith(b"\n"):
                raise CaptureError("capture ledger 尾行不完整")
            try:
                current = json.loads(raw_line.decode("utf-8"))
            except (UnicodeError, json.JSONDecodeError) as exc:
                raise CaptureError("capture ledger 含非法事件") from exc
            if not isinstance(current, dict):
                raise CaptureError("capture ledger 含非对象事件")
            events.append(current)
            if current.get("event_id") == authorization_event_id:
                if matched_line is not None:
                    raise CaptureError("legacy authorization event_id 重复")
                matched_line = raw_line
                authorization_event = current
        if matched_line is None:
            raise CaptureError("legacy authorization event 不存在")
        if hashlib.sha256(matched_line).hexdigest() != authorization_event_sha256:
            raise CaptureError("legacy authorization event SHA-256 不匹配")
        legacy_keys = {
            "schema",
            "event_type",
            "event_id",
            "idempotency_key",
            "created_at",
            "capture_id",
        }
        if (
            set(authorization_event) != legacy_keys
            or authorization_event.get("schema") != EVENT_SCHEMA
            or authorization_event.get("event_type") != "capture_authorized"
            or authorization_event.get("capture_id") != capture_id
        ):
            raise CaptureError("目标不是 proofless historical authorization")
        expected_state = replay(events)
        current_capture = expected_state["captures"].get(capture_id)
        if (
            not isinstance(current_capture, dict)
            or current_capture.get("formalization_authorized") is not True
            or current_capture.get("quality_status")
            != PENDING_FORMAL_STATUS
        ):
            raise CaptureError("legacy authorization replay 未形成授权终态")
        with tempfile.TemporaryDirectory() as temporary:
            expected_state_path = Path(temporary) / "state.json"
            expected_state_path.write_text(
                json.dumps(
                    expected_state,
                    ensure_ascii=False,
                    indent=2,
                    sort_keys=True,
                )
                + "\n",
                encoding="utf-8",
            )
            try:
                bundle = capture_commit.cold_rebuild(
                    ledger,
                    expected_state_path,
                    state_replayer=replay,
                )
            except capture_commit.CommitIndexError as exc:
                raise CaptureError(
                    f"legacy recovery receipt rebuild 失败：{exc}"
                ) from exc
        matching_fact_receipts = [
            receipt
            for receipt in bundle["receipts"].values()
            if receipt.get("capture_id") == capture_id
            and receipt.get("receipt_sha256") == fact_receipt_sha256
        ]
        if len(matching_fact_receipts) != 1:
            raise CaptureError("fact receipt 未精确绑定 legacy capture")
        before_event_count = len(events)
        _atomic_write(
            state,
            json.dumps(
                expected_state,
                ensure_ascii=False,
                indent=2,
                sort_keys=True,
            )
            + "\n",
        )
        try:
            rebuilt = capture_hot.cold_bootstrap(
                ledger,
                state,
                derived,
                lock_token=token,
                reconcile=True,
            )
        except capture_hot.CaptureHotWriterError as exc:
            raise CaptureError(
                f"legacy authorization hot rebuild 失败：{exc}"
            ) from exc
        if int(rebuilt.get("ledger_event_count", -1)) != before_event_count:
            raise CaptureError("legacy recovery 意外改变 event count")
    return {
        "schema": "legacy_capture_authorization_recovery_v1",
        "status": "RECOVERED_EXISTING_LEGACY_AUTHORIZATION",
        "capture_id": capture_id,
        "authorization_event_id": authorization_event_id,
        "authorization_event_sha256": authorization_event_sha256,
        "fact_receipt_sha256": fact_receipt_sha256,
        "quality_status": PENDING_FORMAL_STATUS,
        "event_append_count": 0,
        "formal_write_count": 0,
        "hot_rebuild": rebuilt,
    }


def _bounded_external_file(
    path: Path,
    parent: Path,
    *,
    maximum: int,
    label: str,
) -> Path:
    try:
        parent_resolved = parent.resolve(strict=True)
        path_resolved = path.resolve(strict=True)
        path_resolved.relative_to(parent_resolved)
        metadata = path.lstat()
    except (OSError, ValueError) as exc:
        raise CaptureError(f"{label} 缺失或越界") from exc
    if (
        path.is_symlink()
        or not stat.S_ISREG(metadata.st_mode)
        or metadata.st_size > maximum
    ):
        raise CaptureError(f"{label} 必须是有界的非链接普通文件")
    return path_resolved


def _normal_receipt_resolution_evidence(
    repo: Path,
    *,
    capture_row: dict[str, Any],
    prior_batch_id: str,
    formal_id: str,
    source_event: dict[str, Any],
    confirmation_event: dict[str, Any],
    expected_receipt_sha256: str,
    runtime_root: Path,
) -> dict[str, str]:
    receipt_sha = _validate_sha(
        expected_receipt_sha256, "normal_receipt_sha256"
    )
    runtime = runtime_root.expanduser().resolve()
    receipt_root = runtime / "receipts" / _repo_key(repo)
    if not receipt_root.is_dir() or receipt_root.is_symlink():
        raise CaptureError("normal receipt 根目录缺失或不安全")
    matches: list[Path] = []
    for candidate in sorted(receipt_root.glob("*.json")):
        if candidate.is_symlink() or not candidate.is_file():
            continue
        path = _bounded_external_file(
            candidate,
            receipt_root,
            maximum=MAX_NORMAL_RECEIPT_BYTES,
            label="normal receipt",
        )
        if _sha256_file(path) == receipt_sha:
            matches.append(path)
    if len(matches) != 1:
        raise CaptureError("未找到唯一的 normal receipt 内容实体")
    receipt_path = matches[0]
    receipt = _read_json(receipt_path)
    if (
        not isinstance(receipt, dict)
        or receipt.get("schema") != NORMAL_RECEIPT_SCHEMA
        or receipt.get("status") != "COMMITTED"
    ):
        raise CaptureError("normal receipt schema/status 非法")
    try:
        receipt_repo = Path(str(receipt.get("repo_root") or "")).resolve(
            strict=True
        )
    except OSError as exc:
        raise CaptureError("normal receipt repo_root 非法") from exc
    if receipt_repo != repo:
        raise CaptureError("normal receipt 绑定了其他仓库")
    items = receipt.get("items")
    if (
        not isinstance(items, list)
        or len(items) != 1
        or not isinstance(items[0], dict)
        or items[0].get("formal_id") != formal_id
        or items[0].get("mode") != "redo"
    ):
        raise CaptureError("normal receipt 未唯一绑定确认后的 redo 身份")
    changed_files = receipt.get("changed_files")
    required_targets = {str(repo / "节点总表.md"), str(repo / f"{formal_id}.md")}
    if (
        not isinstance(changed_files, list)
        or not required_targets.issubset({str(value) for value in changed_files})
    ):
        raise CaptureError("normal receipt 未发布确认身份的必要正式表面")
    idempotency_key = str(receipt.get("idempotency_key") or "")
    expected_name = (
        hashlib.sha256(idempotency_key.encode("utf-8")).hexdigest() + ".json"
    )
    if not idempotency_key or receipt_path.name != expected_name:
        raise CaptureError("normal receipt 路径与幂等键不一致")
    transaction_id = _validate_id(
        receipt.get("transaction_id"), "normal receipt.transaction_id"
    )
    transaction_root = runtime / "transactions" / _repo_key(repo)
    journal_raw = Path(str(receipt.get("journal") or "")).expanduser()
    if not journal_raw.is_absolute():
        raise CaptureError("normal receipt journal 必须是绝对路径")
    journal_path = _bounded_external_file(
        journal_raw,
        transaction_root,
        maximum=MAX_NORMAL_WAL_BYTES,
        label="normal receipt WAL",
    )
    journal = _read_json(journal_path)
    if (
        not isinstance(journal, dict)
        or journal.get("schema") != NORMAL_WAL_SCHEMA
        or journal.get("state") != "committed"
    ):
        raise CaptureError("normal receipt WAL schema/state 非法")
    for label in (
        "transaction_id",
        "idempotency_key",
        "payload_sha256",
        "repo_root",
    ):
        if journal.get(label) != receipt.get(label):
            raise CaptureError(f"normal receipt/WAL {label} 不一致")
    if journal.get("result_items") != items:
        raise CaptureError("normal receipt/WAL 正式结果不一致")
    payload_identity = journal.get("payload_identity")
    packages = (
        payload_identity.get("packages")
        if isinstance(payload_identity, dict)
        else None
    )
    if not isinstance(packages, list):
        raise CaptureError("normal receipt WAL 缺少 capture-bound package")
    source_event_sha = _sha256_value(source_event)
    matching_packages = []
    for package in packages:
        if not isinstance(package, dict):
            continue
        binding = package.get("capture_binding")
        if not isinstance(binding, dict):
            continue
        if (
            binding.get("batch_id") == prior_batch_id
            and binding.get("capture_id") == capture_row.get("capture_id")
            and binding.get("capture_payload_sha256")
            == capture_row.get("payload_sha256")
            and binding.get("stable_evidence_sha256") == source_event_sha
            and package.get("formal_id") == formal_id
            and package.get("mode") == "redo"
        ):
            matching_packages.append(package)
    if len(matching_packages) != 1:
        raise CaptureError("normal receipt WAL 未唯一绑定原 capture 与首答证据")
    package = matching_packages[0]
    if (
        str(source_event.get("event_id") or "")
        not in str(package.get("date_source") or "")
        or formal_id not in str(
            (package.get("capture_binding") or {}).get("identity_resolution") or ""
        )
    ):
        raise CaptureError("normal receipt WAL 缺少答案安全的身份确认链")
    if _parse_timestamp(
        receipt.get("committed_at"), "normal receipt.committed_at"
    ) < _parse_timestamp(
        confirmation_event.get("event_time"), "confirmation.event_time"
    ):
        raise CaptureError("normal receipt 早于用户身份确认事件")
    return {
        "normal_receipt_sha256": receipt_sha,
        "normal_receipt_transaction_id": transaction_id,
        "normal_receipt_journal_sha256": _sha256_file(journal_path),
    }


def _validate_needs_user_resolution_evidence(
    repo: Path,
    *,
    capture_row: dict[str, Any],
    prior_batch_id: str,
    formal_id: str,
    confirmation_event_id: str,
    normal_receipt_sha256: str | None,
    runtime_root: Path,
) -> dict[str, Any]:
    refs = [
        ref
        for ref in (capture_row.get("capture", {}).get("stable_evidence_refs") or [])
        if ref.get("kind") in {"review_loop_event", ORDINARY_REVIEW_REF_KIND}
    ]
    if len(refs) != 1:
        raise CaptureError("needs_user resolution 需要唯一 canonical review event")
    locator = str(refs[0].get("locator") or "")
    marker = "#event_id="
    if marker not in locator:
        raise CaptureError("needs_user resolution 的 review event 定位非法")
    source_event_id = _validate_id(
        locator.rsplit(marker, 1)[1], "resolution.source_review_event_id"
    )
    source_event = _review_event_by_id(repo, source_event_id)
    source_event_sha = _sha256_value(source_event)
    if refs[0].get("sha256") != source_event_sha:
        raise CaptureError("needs_user resolution 的源 review event 已漂移")
    route = source_event.get("route") or {}
    candidate = route.get("candidate") or {}
    candidate_id = str(candidate.get("candidate_id") or "")
    if route.get("kind") != "needs_user" or not candidate_id:
        raise CaptureError("源 review event 不是可由用户解决的身份候选")
    confirmation_id = _validate_id(
        confirmation_event_id, "confirmation_event_id"
    )
    confirmation = _review_event_by_id(repo, confirmation_id)
    if (
        confirmation.get("event_kind") != "candidate_confirmation"
        or confirmation.get("explicit_user_confirmation") is not True
        or confirmation.get("formal_write_authorized") is not False
        or confirmation.get("candidate_id") != candidate_id
        or confirmation.get("resolved_formal_node_id") != formal_id
    ):
        raise CaptureError("用户确认事件未唯一解决该 capture 的正式身份")
    receipt_evidence: dict[str, str | None]
    if normal_receipt_sha256 is None:
        resolution_kind = "confirmed_identity"
        receipt_evidence = {
            "normal_receipt_sha256": None,
            "normal_receipt_transaction_id": None,
            "normal_receipt_journal_sha256": None,
        }
    else:
        resolution_kind = "confirmed_identity_with_existing_receipt"
        receipt_evidence = _normal_receipt_resolution_evidence(
            repo,
            capture_row=capture_row,
            prior_batch_id=prior_batch_id,
            formal_id=formal_id,
            source_event=source_event,
            confirmation_event=confirmation,
            expected_receipt_sha256=normal_receipt_sha256,
            runtime_root=runtime_root,
        )
    return {
        "resolution_kind": resolution_kind,
        "study_date": capture_row["study_date"],
        "prior_batch_id": prior_batch_id,
        "capture_id": capture_row["capture_id"],
        "formal_id": formal_id,
        "source_review_event_id": source_event_id,
        "source_review_event_sha256": source_event_sha,
        "confirmation_event_id": confirmation_id,
        "confirmation_event_sha256": _sha256_value(confirmation),
        **receipt_evidence,
    }


def resolve_needs_user(
    *,
    study_date: str,
    capture_id: str,
    formal_id: str,
    confirmation_event_id: str,
    normal_receipt_sha256: str | None = None,
    idempotency_key: str,
    repo_root: str | Path = ".",
    runtime_root: str | Path = DEFAULT_RUNTIME_ROOT,
) -> dict[str, Any]:
    date = _validate_date(study_date)
    capture_id = _validate_id(capture_id, "capture_id")
    if not FORMAL_ID_RE.fullmatch(formal_id):
        raise CaptureError("formal_id 非法")
    confirmation_event_id = _validate_id(
        confirmation_event_id, "confirmation_event_id"
    )
    receipt_sha = (
        _validate_sha(normal_receipt_sha256, "normal_receipt_sha256")
        if normal_receipt_sha256 is not None
        else None
    )
    key = _validate_id(idempotency_key, "idempotency_key")
    repo = _resolve_repo(repo_root)
    root = capture_root(repo)
    runtime = Path(runtime_root).expanduser().resolve()
    with _ledger_lock(root):
        events = _load_events(root)
        _, by_key = _event_index(events)
        state = replay(events)
        existing = by_key.get(key)
        if existing:
            if existing.get("event_type") != "needs_user_resolved":
                raise CaptureError("idempotency_key 已被其他事件使用")
            material = _needs_user_resolution_material(existing)
            if (
                material["study_date"] != date
                or material["capture_id"] != capture_id
                or material["formal_id"] != formal_id
                or material["confirmation_event_id"] != confirmation_event_id
                or material["normal_receipt_sha256"] != receipt_sha
            ):
                raise CaptureError("idempotency_key 已被不同 needs_user resolution 使用")
            return {
                "status": "ALREADY_RESOLVED",
                **material,
                "resolution_sha256": existing["resolution_sha256"],
                "created": False,
            }
        current = state["captures"].get(capture_id)
        if not current or current.get("study_date") != date:
            raise CaptureError("显式日期未唯一绑定目标 capture")
        if current.get("quality_status") != "needs_user":
            resolution = current.get("resolution")
            if isinstance(resolution, dict):
                if (
                    resolution.get("study_date") == date
                    and resolution.get("formal_id") == formal_id
                    and resolution.get("confirmation_event_id")
                    == confirmation_event_id
                    and resolution.get("normal_receipt_sha256") == receipt_sha
                ):
                    return {
                        "status": "ALREADY_RESOLVED",
                        **resolution,
                        "resolution_sha256": _sha256_value(resolution),
                        "created": False,
                    }
            raise CaptureError("只有已关闭批次中的 needs_user capture 可以补录确认")
        prior_batch_id = str(current.get("current_batch_id") or "")
        prior_batch = state["batches"].get(prior_batch_id)
        if not prior_batch or prior_batch.get("status") != "PARTIAL":
            raise CaptureError("needs_user capture 未绑定已关闭 PARTIAL 批次")
        material = _validate_needs_user_resolution_evidence(
            repo,
            capture_row=current,
            prior_batch_id=prior_batch_id,
            formal_id=formal_id,
            confirmation_event_id=confirmation_event_id,
            normal_receipt_sha256=receipt_sha,
            runtime_root=runtime,
        )
        event = {
            "schema": EVENT_SCHEMA,
            "event_type": "needs_user_resolved",
            "event_id": f"CE-{hashlib.sha256(('resolve-needs-user:' + key).encode()).hexdigest()[:20]}",
            "idempotency_key": key,
            "created_at": _now_utc(),
            "resolution_sha256": _sha256_value(material),
            **material,
        }
        _append_event(root, events, event)
    return {
        "status": "NEEDS_USER_RESOLVED",
        **material,
        "resolution_sha256": event["resolution_sha256"],
        "created": True,
    }


def retire_needs_user(
    *,
    study_date: str,
    capture_ids: list[str],
    reason: str,
    user_confirmed: bool,
    idempotency_key: str,
    repo_root: str | Path = ".",
) -> dict[str, Any]:
    """Retire one closed PARTIAL batch's complete current needs_user set."""

    date = _validate_date(study_date)
    if not isinstance(capture_ids, list) or not capture_ids:
        raise CaptureError("needs_user retirement capture_ids 必须是非空数组")
    requested_capture_ids = [
        _validate_id(value, "capture_ids[]") for value in capture_ids
    ]
    if requested_capture_ids != sorted(set(requested_capture_ids)):
        raise CaptureError("needs_user retirement capture_ids 必须排序且唯一")
    if reason != NEEDS_USER_RETIREMENT_REASON:
        raise CaptureError("needs_user retirement reason 必须是 user_removed")
    if user_confirmed is not True:
        raise CaptureError("needs_user retirement 需要显式 --user-confirmed")
    key = _validate_id(idempotency_key, "idempotency_key")
    root = capture_root(repo_root)
    with _ledger_lock(root):
        events = _load_events(root)
        _, by_key = _event_index(events)
        state = replay(events)
        existing = by_key.get(key)
        if existing:
            if existing.get("event_type") != "needs_user_retired":
                raise CaptureError("idempotency_key 已被其他事件使用")
            material = _needs_user_retirement_material(existing)
            if (
                material["study_date"] != date
                or material["capture_ids"] != requested_capture_ids
                or material["reason"] != reason
                or material["explicit_user_confirmation"] is not True
            ):
                raise CaptureError(
                    "idempotency_key 已被不同 needs_user retirement 使用"
                )
            if existing.get("retirement_sha256") != _sha256_value(material):
                raise CaptureError("已记录 needs_user retirement 哈希非法")
            return {
                "status": "ALREADY_RETIRED",
                **material,
                "retirement_sha256": existing["retirement_sha256"],
                "created": False,
            }
        for prior_event in events:
            if prior_event.get("event_type") != "needs_user_retired":
                continue
            prior_material = _needs_user_retirement_material(prior_event)
            if (
                prior_material["study_date"] == date
                and prior_material["capture_ids"] == requested_capture_ids
                and prior_material["reason"] == reason
            ):
                if prior_event.get("retirement_sha256") != _sha256_value(
                    prior_material
                ):
                    raise CaptureError(
                        "既有 needs_user retirement 哈希非法"
                    )
                return {
                    "status": "ALREADY_RETIRED",
                    **prior_material,
                    "retirement_sha256": prior_event["retirement_sha256"],
                    "created": False,
                }
        active_batches = [
            batch
            for batch in state["batches"].values()
            if batch.get("status") == "active"
        ]
        if active_batches:
            raise CaptureError(
                "needs_user retirement 前不得存在 active 日终批次"
            )
        requested_rows: list[dict[str, Any]] = []
        for capture_id in requested_capture_ids:
            current = state["captures"].get(capture_id)
            if (
                not current
                or current.get("study_date") != date
                or current.get("quality_status") != "needs_user"
            ):
                raise CaptureError(
                    "needs_user retirement 项必须是显式日期的当前 needs_user"
                )
            requested_rows.append(current)
        prior_batch_ids = {
            str(row.get("current_batch_id") or "") for row in requested_rows
        }
        if len(prior_batch_ids) != 1 or "" in prior_batch_ids:
            raise CaptureError(
                "needs_user retirement 项必须绑定同一个原始批次"
            )
        prior_batch_id = next(iter(prior_batch_ids))
        prior_batch = state["batches"].get(prior_batch_id)
        if (
            not isinstance(prior_batch, dict)
            or prior_batch.get("study_date") != date
        ):
            raise CaptureError("显式日期没有可退休的已关闭批次")
        if prior_batch.get("status") != "PARTIAL":
            raise CaptureError(
                "capture 当前绑定批次不是已关闭 PARTIAL 批次"
            )
        same_set_batches = [
            batch
            for batch in state["batches"].values()
            if batch.get("study_date") == date
            and batch.get("capture_set_sha256")
            == prior_batch.get("capture_set_sha256")
        ]
        if (
            not same_set_batches
            or same_set_batches[-1].get("batch_id") != prior_batch_id
        ):
            raise CaptureError(
                "capture 当前绑定批次不是同一冻结集合的最新 attempt"
            )
        non_success_ids = sorted(
            capture_id
            for capture_id, row in prior_batch.get("results", {}).items()
            if row.get("outcome") not in SUCCESS_OUTCOMES
        )
        if requested_capture_ids != non_success_ids:
            raise CaptureError(
                "needs_user retirement 请求必须等于最新 PARTIAL 批次完整非成功集合"
            )
        if any(
            prior_batch["results"][capture_id].get("outcome")
            != "needs_user"
            for capture_id in non_success_ids
        ):
            raise CaptureError(
                "needs_user retirement 的全部非成功结果必须是 needs_user"
            )
        for current in requested_rows:
            if current.get("current_batch_id") != prior_batch_id:
                raise CaptureError(
                    "needs_user retirement 项必须是同批次同日期的当前 needs_user"
                )
        capture_evidence: list[dict[str, Any]] = []
        for capture_id in requested_capture_ids:
            result_matches = [
                event
                for event in events
                if event.get("event_type") == "curation_item_result"
                and event.get("batch_id") == prior_batch_id
                and event.get("capture_id") == capture_id
            ]
            if len(result_matches) != 1:
                raise CaptureError(
                    "needs_user retirement 缺少唯一原始 result event"
                )
            result_event = result_matches[0]
            capture_evidence.append(
                {
                    "capture_id": capture_id,
                    "payload_sha256": state["captures"][capture_id][
                        "payload_sha256"
                    ],
                    "result_event_id": result_event["event_id"],
                    "result_event_sha256": _sha256_value(result_event),
                    "result_sha256": result_event["result_sha256"],
                    "outcome": result_event["outcome"],
                }
            )
        close_matches = [
            event
            for event in events
            if event.get("event_type") == "curation_batch_closed"
            and event.get("batch_id") == prior_batch_id
        ]
        if len(close_matches) != 1:
            raise CaptureError(
                "needs_user retirement 缺少唯一原始 close event"
            )
        close_event = close_matches[0]
        material = {
            "retirement_schema": NEEDS_USER_RETIREMENT_SCHEMA,
            "study_date": date,
            "prior_batch_id": prior_batch_id,
            "capture_ids": requested_capture_ids,
            "capture_set_sha256": prior_batch["capture_set_sha256"],
            "capture_evidence": capture_evidence,
            "close_event_id": close_event["event_id"],
            "close_event_sha256": _sha256_value(close_event),
            "close_sha256": close_event["close_sha256"],
            "reason": reason,
            "explicit_user_confirmation": True,
            "formal_write_count": 0,
        }
        _needs_user_retirement_material(material)
        event = {
            "schema": EVENT_SCHEMA,
            "event_type": "needs_user_retired",
            "event_id": f"CE-{hashlib.sha256(('retire-needs-user:' + key).encode()).hexdigest()[:20]}",
            "idempotency_key": key,
            "created_at": _now_utc(),
            "retirement_sha256": _sha256_value(material),
            **material,
        }
        _append_event(root, events, event)
    return {
        "status": "NEEDS_USER_RETIRED",
        **material,
        "retirement_sha256": event["retirement_sha256"],
        "created": True,
    }


def start_batch(
    study_date: str,
    idempotency_key: str,
    *,
    capture_ids: list[str] | None = None,
    repo_root: str | Path = ".",
) -> dict[str, Any]:
    date = _validate_date(study_date)
    key = _validate_id(idempotency_key, "idempotency_key")
    requested_capture_ids: list[str] | None = None
    if capture_ids is not None:
        if not isinstance(capture_ids, list) or not capture_ids:
            raise CaptureError("精确 capture subset 必须是非空数组")
        requested_capture_ids = [
            _validate_id(value, "capture_ids[]") for value in capture_ids
        ]
        if requested_capture_ids != sorted(set(requested_capture_ids)):
            raise CaptureError("精确 capture subset 必须排序且唯一")
    root = capture_root(repo_root)
    with _ledger_lock(root):
        events = _load_events(root)
        _, by_key = _event_index(events)
        state = replay(events)
        existing = by_key.get(key)
        if existing:
            if (
                existing.get("event_type") != "curation_batch_started"
                or existing.get("study_date") != date
                or (
                    requested_capture_ids is not None
                    and existing.get("capture_ids") != requested_capture_ids
                )
            ):
                raise CaptureError("idempotency_key 已被不同日终清单使用")
            return {
                "status": "ALREADY_STARTED",
                "batch_id": existing["batch_id"],
                "study_date": date,
                "capture_ids": existing["capture_ids"],
                "capture_set_sha256": existing["capture_set_sha256"],
                "attempt": _batch_attempt(existing),
                "retry_of_batch_id": existing.get("retry_of_batch_id"),
                "retry_capture_ids": list(
                    existing.get("retry_capture_ids") or existing["capture_ids"]
                ),
                "carried_capture_ids": list(
                    existing.get("carried_capture_ids") or []
                ),
                "created": False,
            }
        active = [
            batch
            for batch in state["batches"].values()
            if batch.get("status") == "active"
        ]
        if active:
            current = active[0]
            raise CaptureError(
                "已有 active 日终批次，必须先恢复或关闭："
                f"{current['batch_id']} ({current['study_date']})"
            )
        all_pending_capture_ids = list(state["pending_by_date"].get(date, []))
        pending_capture_ids = list(all_pending_capture_ids)
        if requested_capture_ids is not None:
            pending_capture_ids = sorted(
                set(requested_capture_ids) & set(all_pending_capture_ids)
            )
        if not pending_capture_ids:
            return {
                "status": "NO_PENDING_CAPTURE",
                "study_date": date,
                "capture_ids": [],
                "created": False,
            }
        pending_set = set(pending_capture_ids)
        latest_by_set: dict[str, dict[str, Any]] = {}
        for prior in state["batches"].values():
            if prior.get("study_date") != date or prior.get("status") != "PARTIAL":
                continue
            set_sha = str(prior.get("capture_set_sha256") or "")
            current = latest_by_set.get(set_sha)
            if current is None or int(prior.get("attempt", 1)) > int(
                current.get("attempt", 1)
            ):
                latest_by_set[set_sha] = prior
        retry_candidates: list[dict[str, Any]] = []
        blocked_retry_capture_ids: set[str] = set()
        for prior in latest_by_set.values():
            if any(state["captures"][cid].get("supplement_evidence") for cid in prior["capture_ids"]):
                continue
            results = prior.get("results") or {}
            retry_ids = sorted(
                capture_id
                for capture_id, value in results.items()
                if value.get("outcome") not in SUCCESS_OUTCOMES
            )
            if not retry_ids or not (set(retry_ids) & pending_set):
                continue
            ready = all(
                (
                    results[capture_id].get("outcome") == "failed"
                    and state["captures"][capture_id].get("quality_status")
                    == "curation_failed"
                )
                or (
                    results[capture_id].get("outcome") == "needs_user"
                    and state["captures"][capture_id].get("quality_status")
                    == "needs_user_resolved"
                )
                for capture_id in retry_ids
            )
            if ready:
                retry_candidates.append(prior)
            else:
                blocked_retry_capture_ids.update(retry_ids)
        retry_of: dict[str, Any] | None = None
        if retry_candidates:
            retry_of = min(
                retry_candidates,
                key=lambda batch: (
                    str(batch.get("started_at") or ""),
                    str(batch.get("batch_id") or ""),
                ),
            )
            capture_ids = list(retry_of["capture_ids"])
            if (
                requested_capture_ids is not None
                and requested_capture_ids != capture_ids
            ):
                raise CaptureError(
                    "PARTIAL 重试必须保留原冻结集合与 carry，"
                    "精确 subset 必须等于原 capture_ids"
                )
            capture_set_sha = str(retry_of["capture_set_sha256"])
            attempt = int(retry_of.get("attempt", 1)) + 1
            retry_capture_ids = sorted(
                capture_id
                for capture_id, value in retry_of["results"].items()
                if value.get("outcome") not in SUCCESS_OUTCOMES
            )
            carried_capture_ids = sorted(
                set(capture_ids) - set(retry_capture_ids)
            )
        else:
            if requested_capture_ids is not None:
                missing_requested = sorted(
                    set(requested_capture_ids) - set(all_pending_capture_ids)
                )
                if missing_requested:
                    raise CaptureError(
                        "精确 capture subset 含非当前日期可启动项："
                        f"{missing_requested}"
                    )
            capture_ids = sorted(pending_set - blocked_retry_capture_ids)
            if not capture_ids:
                return {
                    "status": "RETRY_BLOCKED",
                    "study_date": date,
                    "capture_ids": [],
                    "blocked_capture_ids": sorted(blocked_retry_capture_ids),
                    "created": False,
                }
            capture_set_sha = _sha256_value(capture_ids)
            attempt = _next_batch_attempt(state, date, capture_set_sha)
            retry_capture_ids = list(capture_ids)
            carried_capture_ids = []
        batch_id = _batch_id(date, capture_set_sha, attempt)
        event = {
            "schema": EVENT_SCHEMA,
            "event_type": "curation_batch_started",
            "knowledge_observation_contract": "formal-point-observations-408-v1",
            "event_id": f"CE-{hashlib.sha256(('start:' + key).encode()).hexdigest()[:20]}",
            "idempotency_key": key,
            "created_at": _now_utc(),
            "batch_id": batch_id,
            "batch_schema": BATCH_SCHEMA,
            "study_date": date,
            "timezone": TIMEZONE,
            "capture_ids": capture_ids,
            "capture_set_sha256": capture_set_sha,
            "attempt": attempt,
        }
        supplements = {cid: list(state["captures"][cid]["supplement_evidence"])
                       for cid in capture_ids if state["captures"][cid].get("supplement_evidence")}
        if supplements:
            event["supplement_evidence"] = supplements
            event["supplement_prior_formal_ids"] = {
                cid: state["captures"][cid].get("supplement_prior_formal_id") for cid in supplements
            }
        if retry_of is not None:
            event.update(
                {
                    "retry_of_batch_id": retry_of["batch_id"],
                    "retry_capture_ids": retry_capture_ids,
                    "carried_capture_ids": carried_capture_ids,
                }
            )
        additions = [event]
        if retry_of is not None:
            source_result_event_ids = {
                (str(row.get("batch_id") or ""), str(row.get("capture_id") or "")):
                str(row.get("event_id") or "")
                for row in events
                if row.get("event_type") == "curation_item_result"
            }
            for carried_capture_id in carried_capture_ids:
                source = retry_of["results"][carried_capture_id]
                source_result_event_id = source_result_event_ids.get(
                    (str(retry_of["batch_id"]), carried_capture_id)
                )
                if not source_result_event_id:
                    raise CaptureError("前一 attempt 的成功结果事件缺失")
                result_payload = {
                    "batch_id": batch_id,
                    "capture_id": carried_capture_id,
                    "outcome": source["outcome"],
                    "formal_id": source.get("formal_id"),
                    "receipt_sha256": source.get("receipt_sha256"),
                    "verification_sha256": source.get("verification_sha256"),
                    "reason": source.get("reason"),
                }
                if source.get("outcome") == "concept_only":
                    result_payload.update(
                        {
                            "concept_source_id": source.get("concept_source_id"),
                            "concept_receipt_sha256": source.get(
                                "concept_receipt_sha256"
                            ),
                        }
                    )
                if any(
                    field in source for field in FORMAL_BACKFLOW_RESULT_FIELDS
                ):
                    result_payload.update(
                        {
                            field: source.get(field)
                            for field in FORMAL_BACKFLOW_RESULT_FIELDS
                        }
                    )
                carry_key = f"curation-carry:{batch_id}:{carried_capture_id}"
                additions.append(
                    {
                        "schema": EVENT_SCHEMA,
                        "event_type": "curation_item_result",
                        "event_id": f"CE-{hashlib.sha256(('carry:' + carry_key).encode()).hexdigest()[:20]}",
                        "idempotency_key": carry_key,
                        "created_at": _now_utc(),
                        "result_sha256": _sha256_value(result_payload),
                        "carried_from_batch_id": retry_of["batch_id"],
                        "carried_from_result_event_id": source_result_event_id,
                        **result_payload,
                    }
                )
        _append_events(root, events, additions)
    return {
        "status": "CURATION_BATCH_STARTED",
        "batch_id": batch_id,
        "study_date": date,
        "capture_ids": capture_ids,
        "capture_set_sha256": capture_set_sha,
        "attempt": attempt,
        "retry_of_batch_id": retry_of["batch_id"] if retry_of else None,
        "retry_capture_ids": retry_capture_ids,
        "carried_capture_ids": carried_capture_ids,
        "created": True,
    }


def _validate_concept_only_receipt(
    repo: Path,
    *,
    capture_id: str,
    concept_source_id: str,
    concept_receipt_sha256: str,
) -> dict[str, Any]:
    """Reopen one native concept-review session bound to this Capture.

    A concept-only terminal is a successful point-level learning record.  It is
    deliberately outside the formal-node/backflow path and therefore cannot
    carry a formal ID, question receipt, review action, or question-level FSRS.
    """

    if not re.fullmatch(r"CR408-[0-9a-f]{64}", str(concept_source_id or "")):
        raise CaptureError("concept_only 缺合法 concept_source_id")
    session_sha = _validate_sha(
        concept_receipt_sha256, "concept_receipt_sha256"
    )
    try:
        import concept_review_408 as concept_review

        path = (
            concept_review.private_root(repo)
            / "sessions"
            / concept_source_id.removeprefix("CR408-")
            / "session.json"
        )
        sealed = concept_review.reopen(path, repo)
    except (ImportError, OSError, ValueError, KeyError) as exc:
        raise CaptureError(f"concept_only 原生会话无法重开：{exc}") from exc
    session = sealed.get("session") or {}
    observations = session.get("observations") or []
    expected_source_id = "CR408-" + concept_review.digest(
        str(session.get("session_id") or "")
    )
    if (
        sealed.get("session_sha256") != session_sha
        or expected_source_id != concept_source_id
        or not observations
        or any(row.get("question_ref") != capture_id for row in observations)
    ):
        raise CaptureError("concept_only receipt/session/Capture 绑定不一致")
    return {
        "concept_source_id": concept_source_id,
        "concept_receipt_sha256": session_sha,
    }


def mark_result(
    *,
    batch_id: str,
    capture_id: str,
    outcome: str,
    idempotency_key: str,
    formal_id: str | None = None,
    receipt_sha256: str | None = None,
    verification_sha256: str | None = None,
    reason: str | None = None,
    review_schedule_ensure_receipt_sha256: str | None = None,
    formal_review_binding_receipt_sha256: str | None = None,
    concept_source_id: str | None = None,
    concept_receipt_sha256: str | None = None,
    repo_root: str | Path = ".",
) -> dict[str, Any]:
    batch_id = _validate_id(batch_id, "batch_id")
    capture_id = _validate_id(capture_id, "capture_id")
    key = _validate_id(idempotency_key, "idempotency_key")
    if outcome not in RESULT_OUTCOMES:
        raise CaptureError(f"outcome 必须是 {sorted(RESULT_OUTCOMES)}")
    if outcome == "curated":
        if not formal_id or not FORMAL_ID_RE.fullmatch(formal_id):
            raise CaptureError("curated 结果必须绑定合法 formal_id")
        if verification_sha256 is not None:
            raise CaptureError("curated 只能绑定 receipt_sha256")
        receipt_sha256 = _validate_sha(receipt_sha256, "receipt_sha256")
        verification_sha256 = None
        reason = None
    elif outcome == "already_current":
        if not formal_id or not FORMAL_ID_RE.fullmatch(formal_id):
            raise CaptureError("already_current 结果必须绑定合法 formal_id")
        if receipt_sha256 is not None:
            raise CaptureError("already_current 不能伪造 receipt_sha256")
        verification_sha256 = _validate_sha(
            verification_sha256, "verification_sha256"
        )
        receipt_sha256 = None
        reason = None
    elif outcome == "concept_only":
        if any(
            value is not None
            for value in (formal_id, receipt_sha256, verification_sha256)
        ):
            raise CaptureError("concept_only 禁止绑定题级正式成功字段")
        formal_id = None
        receipt_sha256 = None
        verification_sha256 = None
        reason = None
    else:
        if formal_id is not None or receipt_sha256 is not None or verification_sha256 is not None:
            raise CaptureError("needs_user/failed 不能绑定正式成功字段")
        formal_id = None
        receipt_sha256 = None
        verification_sha256 = None
        reason = _assert_answer_safe("reason", reason)
        if not reason:
            raise CaptureError("needs_user/failed 必须提供答案安全 reason")
    repo = _resolve_repo(repo_root)
    concept_fields: dict[str, str] = {}
    if outcome == "concept_only":
        concept_fields = _validate_concept_only_receipt(
            repo,
            capture_id=capture_id,
            concept_source_id=str(concept_source_id or ""),
            concept_receipt_sha256=str(concept_receipt_sha256 or ""),
        )
    elif concept_source_id is not None or concept_receipt_sha256 is not None:
        raise CaptureError("只有 concept_only 可绑定 concept-review receipt")
    backflow_enabled = (repo / FORMAL_BACKFLOW_CONTRACT_REL).is_file()
    backflow_fields: dict[str, Any] = {}
    if backflow_enabled:
        backflow_fields = _validate_formal_backflow_receipts(
            repo=repo,
            batch_id=batch_id,
            capture_id=capture_id,
            outcome=outcome,
            formal_id=formal_id,
            formal_result_receipt_sha256=(
                receipt_sha256
                if outcome == "curated"
                else verification_sha256
                if outcome == "already_current"
                else None
            ),
            ensure_receipt_sha256=str(
                review_schedule_ensure_receipt_sha256 or ""
            ),
            binding_receipt_sha256=(
                formal_review_binding_receipt_sha256
            ),
        )
    elif any(
        value is not None
        for value in (
            review_schedule_ensure_receipt_sha256,
            formal_review_binding_receipt_sha256,
        )
    ):
        raise CaptureError("仓库尚未启用正式回流合同，不能记录回流 receipt")
    root = capture_root(repo)
    result_payload = {
        "batch_id": batch_id,
        "capture_id": capture_id,
        "outcome": outcome,
        "formal_id": formal_id,
        "receipt_sha256": receipt_sha256,
        "verification_sha256": verification_sha256,
        "reason": reason,
        **concept_fields,
    }
    if backflow_enabled:
        result_payload.update(backflow_fields)
    payload_hash = _sha256_value(result_payload)
    with _ledger_lock(root):
        events = _load_events(root)
        _, by_key = _event_index(events)
        state = replay(events)
        existing = by_key.get(key)
        if existing:
            if (
                existing.get("event_type") != "curation_item_result"
                or existing.get("result_sha256") != payload_hash
            ):
                raise CaptureError("idempotency_key 已被不同日终结果使用")
            return {"status": "ALREADY_RECORDED", **result_payload, "created": False}
        batch = state["batches"].get(batch_id)
        if not batch or capture_id not in batch["capture_ids"]:
            raise CaptureError("结果未绑定当前日终批次的 capture")
        if batch["status"] != "active":
            raise CaptureError("已关闭的日终批次不能再记录结果")
        capture_row = state["captures"].get(capture_id) or {}
        resolution = capture_row.get("resolution")
        if isinstance(resolution, dict) and outcome in FORMAL_SUCCESS_OUTCOMES:
            if formal_id != resolution.get("formal_id"):
                raise CaptureError(
                    "已补录 needs_user 的正式成功结果必须使用确认后的 formal_id"
                )
            if (
                resolution.get("resolution_kind")
                == "confirmed_identity_with_existing_receipt"
                and outcome != "already_current"
            ):
                raise CaptureError(
                    "已有精确 normal receipt 的补录项不得重放 apply；"
                    "必须 seal 后记录 already_current"
                )
        prior = batch["results"].get(capture_id)
        if prior:
            comparable_fields = (
                "outcome",
                "formal_id",
                "receipt_sha256",
                "verification_sha256",
                "reason",
                "concept_source_id",
                "concept_receipt_sha256",
            )
            comparable = {
                "batch_id": batch_id,
                "capture_id": capture_id,
                **{
                    label: prior.get(label)
                    for label in comparable_fields
                },
            }
            if any(
                field in prior for field in FORMAL_BACKFLOW_RESULT_FIELDS
            ):
                comparable.update(
                    {
                        field: prior.get(field)
                        for field in FORMAL_BACKFLOW_RESULT_FIELDS
                    }
                )
            if _sha256_value(comparable) != payload_hash:
                raise CaptureError("同一日终批次已记录不同的 capture 结果")
            return {"status": "ALREADY_RECORDED", **result_payload, "created": False}
        event = {
            "schema": EVENT_SCHEMA,
            "event_type": "curation_item_result",
            "event_id": f"CE-{hashlib.sha256(('result:' + key).encode()).hexdigest()[:20]}",
            "idempotency_key": key,
            "created_at": _now_utc(),
            "result_sha256": payload_hash,
            **result_payload,
        }
        _append_event(root, events, event)
    return {"status": "CURATION_RESULT_RECORDED", **result_payload, "created": True}


def _global_audit_batch_snapshot(batch: dict[str, Any]) -> dict[str, Any]:
    capture_ids = list(batch.get("capture_ids") or [])
    results = batch.get("results") or {}
    result_rows = []
    for capture_id in capture_ids:
        if capture_id not in results:
            continue
        row = {
            "capture_id": capture_id,
            "outcome": results[capture_id].get("outcome"),
            "formal_id": results[capture_id].get("formal_id"),
            "receipt_sha256": results[capture_id].get("receipt_sha256"),
            "verification_sha256": results[capture_id].get("verification_sha256"),
            "reason": results[capture_id].get("reason"),
        }
        if results[capture_id].get("outcome") == "concept_only":
            row.update(
                {
                    "concept_source_id": results[capture_id].get(
                        "concept_source_id"
                    ),
                    "concept_receipt_sha256": results[capture_id].get(
                        "concept_receipt_sha256"
                    ),
                }
            )
        if any(
            field in results[capture_id]
            for field in FORMAL_BACKFLOW_RESULT_FIELDS
        ):
            row.update(
                {
                    field: results[capture_id].get(field)
                    for field in FORMAL_BACKFLOW_RESULT_FIELDS
                }
            )
        if results[capture_id].get("carried_from_batch_id") is not None:
            row.update(
                {
                    "carried_from_batch_id": results[capture_id][
                        "carried_from_batch_id"
                    ],
                    "carried_from_result_event_id": results[capture_id][
                        "carried_from_result_event_id"
                    ],
                }
            )
        result_rows.append(row)
    return {
        "batch_id": batch.get("batch_id"),
        "study_date": batch.get("study_date"),
        "attempt": int(batch.get("attempt", 1)),
        "capture_ids": capture_ids,
        "capture_set_sha256": batch.get("capture_set_sha256"),
        "result_count": len(result_rows),
        "results": result_rows,
        "results_sha256": _sha256_value(result_rows),
    }


def _bounded_audit_file(repo: Path, relative: Path, label: str) -> Path:
    if relative.is_absolute() or ".." in relative.parts:
        raise CaptureError(f"{label} 路径非法")
    candidate = repo / relative
    try:
        resolved = candidate.resolve(strict=True)
        resolved.relative_to(repo)
    except (OSError, ValueError) as exc:
        raise CaptureError(f"{label} 缺失或越界：{relative}") from exc
    if resolved != candidate.absolute() or candidate.is_symlink():
        raise CaptureError(f"{label} 不得经过符号链接：{relative}")
    mode = candidate.stat().st_mode
    if not stat.S_ISREG(mode):
        raise CaptureError(f"{label} 不是普通文件：{relative}")
    return candidate


def _audit_file_commitments(
    repo: Path, relative_paths: tuple[Path, ...] | list[Path], label: str
) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for relative in sorted(set(relative_paths), key=lambda value: value.as_posix()):
        path = _bounded_audit_file(repo, relative, label)
        rows.append(
            {
                "path": relative.as_posix(),
                "sha256": _sha256_file(path),
                "size_bytes": path.stat().st_size,
            }
        )
    return rows


def _validate_signed_audit_origin(receipt: dict[str, Any]) -> None:
    """Validate the signed origin hint without binding it to today's mount."""
    raw = receipt.get("repo_root")
    if not isinstance(raw, str) or not raw or "\x00" in raw:
        raise CaptureError("aggregate audit receipt repo_root 来源提示非法")
    origin = Path(raw)
    if not origin.is_absolute() or str(origin) != raw:
        raise CaptureError("aggregate audit receipt repo_root 来源提示非法")


def _validate_audit_commitment_identity(
    commitments: object,
    expected_paths: tuple[Path, ...] | list[Path],
    label: str,
) -> None:
    """Bind a signed receipt to the exact repository-relative audit surface."""
    if not isinstance(commitments, list):
        raise CaptureError(f"{label} 相对路径身份非法")
    expected = sorted(
        {path.as_posix() for path in expected_paths}
    )
    actual: list[str] = []
    for row in commitments:
        if not isinstance(row, dict) or set(row) != {
            "path",
            "sha256",
            "size_bytes",
        }:
            raise CaptureError(f"{label} 相对路径身份非法")
        relative = row.get("path")
        digest = row.get("sha256")
        size = row.get("size_bytes")
        if (
            not isinstance(relative, str)
            or not isinstance(digest, str)
            or not SHA256_RE.fullmatch(digest)
            or not isinstance(size, int)
            or isinstance(size, bool)
            or size < 0
        ):
            raise CaptureError(f"{label} 相对路径身份非法")
        path = Path(relative)
        if path.is_absolute() or ".." in path.parts or path.as_posix() != relative:
            raise CaptureError(f"{label} 相对路径身份非法")
        actual.append(relative)
    if actual != expected:
        raise CaptureError(f"{label} 相对路径身份非法")


def _global_audit_object_paths(batch: dict[str, Any]) -> list[Path]:
    paths = list(GLOBAL_AUDIT_OBJECT_RELS)
    for result in (batch.get("results") or {}).values():
        formal_id = result.get("formal_id")
        if formal_id:
            if not FORMAL_ID_RE.fullmatch(str(formal_id)):
                raise CaptureError("aggregate audit 遇到非法 formal_id")
            paths.append(Path(f"{formal_id}.md"))
    return paths


def _global_audit_command(repo: Path) -> list[str]:
    del repo
    return [
        "python3",
        "scripts/intake_validate_408.py",
        "--repo",
        ".",
        "--full",
        "--json",
    ]


def _normalize_global_audit_report(raw: Any) -> dict[str, Any]:
    if not isinstance(raw, dict) or raw.get("result") != "PASS":
        raise CaptureError("aggregate global audit 未返回 PASS")
    items = raw.get("items")
    if not isinstance(items, list) or len(items) > 2048:
        raise CaptureError("aggregate global audit items 非法或过大")
    normalized_items: list[dict[str, str]] = []
    for item in items:
        if not isinstance(item, dict):
            raise CaptureError("aggregate global audit item 必须是 object")
        level = str(item.get("level") or "")
        if level not in {"PASS", "WARN"}:
            raise CaptureError("aggregate global audit 含非 PASS item")
        normalized_items.append(
            {
                "level": level,
                "check": str(item.get("check") or ""),
                "message": str(item.get("message") or ""),
            }
        )
    normalized = {"result": "PASS", "items": normalized_items}
    if len(_canonical(normalized)) > GLOBAL_AUDIT_MAX_REPORT_BYTES:
        raise CaptureError("aggregate global audit report 超过有界大小")
    return normalized


def _execute_global_audit(repo: Path) -> dict[str, Any]:
    command = [
        sys.executable,
        str(repo / "scripts" / "intake_validate_408.py"),
        "--repo",
        str(repo),
        "--full",
        "--json",
    ]
    try:
        completed = subprocess.run(
            command,
            cwd=repo,
            capture_output=True,
            text=True,
            timeout=GLOBAL_AUDIT_TIMEOUT_SECONDS,
            check=False,
        )
    except subprocess.TimeoutExpired as exc:
        raise CaptureError("aggregate global audit 运行超时") from exc
    if completed.returncode != 0:
        tail = (completed.stdout or completed.stderr or "").strip()[-1000:]
        raise CaptureError(f"aggregate global audit 失败：{tail}")
    try:
        raw = json.loads(completed.stdout)
    except json.JSONDecodeError as exc:
        raise CaptureError("aggregate global audit 输出不是 JSON") from exc
    return _normalize_global_audit_report(raw)


def _global_audit_key(root: Path, *, create: bool) -> bytes:
    key_path = root / GLOBAL_AUDIT_KEY_FILE
    if create and not key_path.exists():
        root.mkdir(parents=True, exist_ok=True)
        try:
            descriptor = os.open(
                key_path,
                os.O_WRONLY | os.O_CREAT | os.O_EXCL,
                0o600,
            )
        except FileExistsError:
            descriptor = -1
        if descriptor >= 0:
            try:
                key = secrets.token_bytes(32)
                os.write(descriptor, key)
                os.fsync(descriptor)
            finally:
                os.close(descriptor)
    try:
        resolved = key_path.resolve(strict=True)
    except OSError as exc:
        raise CaptureError("aggregate audit 签名密钥缺失") from exc
    if resolved != key_path.absolute() or key_path.is_symlink():
        raise CaptureError("aggregate audit 签名密钥不得经过符号链接")
    mode = key_path.stat().st_mode
    if not stat.S_ISREG(mode) or stat.S_IMODE(mode) != 0o600:
        raise CaptureError("aggregate audit 签名密钥必须是 0600 普通文件")
    key = key_path.read_bytes()
    if len(key) != 32:
        raise CaptureError("aggregate audit 签名密钥长度非法")
    return key


def _global_audit_receipt_root(root: Path, *, create: bool) -> Path:
    receipt_root = root / GLOBAL_AUDIT_RECEIPT_DIR
    if create:
        receipt_root.mkdir(parents=True, exist_ok=True)
    try:
        resolved = receipt_root.resolve(strict=True)
    except OSError as exc:
        raise CaptureError("aggregate audit receipt 目录缺失") from exc
    if resolved != receipt_root.absolute() or receipt_root.is_symlink():
        raise CaptureError("aggregate audit receipt 目录不得经过符号链接")
    if not receipt_root.is_dir():
        raise CaptureError("aggregate audit receipt 根不是目录")
    return receipt_root


def seal_global_audit(
    *, batch_id: str, repo_root: str | Path = "."
) -> dict[str, Any]:
    repo = _resolve_repo(repo_root)
    batch_id = _validate_id(batch_id, "batch_id")
    root = capture_root(repo)
    with _ledger_lock(root):
        events = _load_events(root)
        state = replay(events)
        batch = state["batches"].get(batch_id)
        if not batch or batch.get("status") != "active":
            raise CaptureError("aggregate audit 只接受 active 日终批次")
        missing = sorted(set(batch["capture_ids"]) - set(batch["results"]))
        if missing:
            raise CaptureError(f"aggregate audit 前仍缺逐题结果：{missing}")
        snapshot = _global_audit_batch_snapshot(batch)
        tool_before = _audit_file_commitments(
            repo, list(GLOBAL_AUDIT_TOOL_RELS), "aggregate audit tool"
        )
        object_before = _audit_file_commitments(
            repo,
            _global_audit_object_paths(batch),
            "aggregate audited object",
        )
        report = _execute_global_audit(repo)
        tool_after = _audit_file_commitments(
            repo, list(GLOBAL_AUDIT_TOOL_RELS), "aggregate audit tool"
        )
        object_after = _audit_file_commitments(
            repo,
            _global_audit_object_paths(batch),
            "aggregate audited object",
        )
        if tool_before != tool_after or object_before != object_after:
            raise CaptureError("aggregate audit 运行期间审计工具或对象发生漂移")
        command = _global_audit_command(repo)
        unsigned = {
            "schema": GLOBAL_AUDIT_RECEIPT_SCHEMA,
            "kind": GLOBAL_AUDIT_RECEIPT_KIND,
            "audit_version": GLOBAL_AUDIT_VERSION,
            "audit_status": "PASS",
            "repo_root": str(repo),
            "batch_snapshot": snapshot,
            "batch_snapshot_sha256": _sha256_value(snapshot),
            "audit_command": command,
            "audit_command_sha256": _sha256_value(command),
            "audit_tool_commitments": tool_after,
            "audit_tool_commitments_sha256": _sha256_value(tool_after),
            "audited_object_commitments": object_after,
            "audited_object_commitments_sha256": _sha256_value(object_after),
            "audit_report": report,
            "audit_report_sha256": _sha256_value(report),
        }
        key = _global_audit_key(root, create=True)
        signature = hmac.new(key, _canonical(unsigned), hashlib.sha256).hexdigest()
        receipt = {
            **unsigned,
            "attestation": {
                "algorithm": "hmac-sha256",
                "key_id": hashlib.sha256(key).hexdigest(),
                "signature": signature,
            },
        }
        receipt_bytes = _canonical(receipt)
        digest = hashlib.sha256(receipt_bytes).hexdigest()
        receipt_root = _global_audit_receipt_root(root, create=True)
        path = receipt_root / f"{digest}.json"
        if path.exists():
            existing = _bounded_audit_file(
                receipt_root, Path(path.name), "aggregate audit receipt"
            ).read_bytes()
            if existing != receipt_bytes:
                raise CaptureError("aggregate audit receipt 内容寻址冲突")
        else:
            _atomic_write(path, receipt_bytes.decode("utf-8"))
    return {
        "status": "GLOBAL_AUDIT_RECEIPT_SEALED",
        "batch_id": batch_id,
        "global_audit_receipt_sha256": digest,
        "receipt_path": str(path),
    }


def _validate_global_audit_receipt(
    repo: Path,
    root: Path,
    batch: dict[str, Any],
    expected_sha256: object,
    *,
    require_current_objects: bool,
) -> dict[str, Any]:
    expected_sha = _validate_sha(
        expected_sha256, "global_audit_receipt_sha256"
    )
    receipt_root = _global_audit_receipt_root(root, create=False)
    path = _bounded_audit_file(
        receipt_root,
        Path(f"{expected_sha}.json"),
        "aggregate audit receipt",
    )
    raw = path.read_bytes()
    if hashlib.sha256(raw).hexdigest() != expected_sha:
        raise CaptureError("aggregate audit receipt 内容哈希不一致")
    try:
        receipt = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise CaptureError("aggregate audit receipt 不是 JSON") from exc
    if not isinstance(receipt, dict) or raw != _canonical(receipt):
        raise CaptureError("aggregate audit receipt 不是规范化内容寻址实体")
    attestation = receipt.get("attestation")
    if not isinstance(attestation, dict):
        raise CaptureError("aggregate audit receipt 缺 attestation")
    unsigned = {key: value for key, value in receipt.items() if key != "attestation"}
    key = _global_audit_key(root, create=False)
    expected_signature = hmac.new(
        key, _canonical(unsigned), hashlib.sha256
    ).hexdigest()
    if (
        attestation.get("algorithm") != "hmac-sha256"
        or attestation.get("key_id") != hashlib.sha256(key).hexdigest()
        or not hmac.compare_digest(
            str(attestation.get("signature") or ""), expected_signature
        )
    ):
        raise CaptureError("aggregate audit receipt attestation 非法")
    if (
        receipt.get("schema") != GLOBAL_AUDIT_RECEIPT_SCHEMA
        or receipt.get("kind") != GLOBAL_AUDIT_RECEIPT_KIND
        or receipt.get("audit_version") != GLOBAL_AUDIT_VERSION
        or receipt.get("audit_status") != "PASS"
    ):
        raise CaptureError("aggregate audit receipt 身份或 PASS 状态非法")
    # repo_root is an HMAC-covered origin hint, not a stable repository
    # identity: an intact repository may move between mounts.  Stable identity
    # is enforced below by the signed batch and exact repository-relative audit
    # surfaces, while an unsigned/tampered origin still fails attestation.
    _validate_signed_audit_origin(receipt)
    snapshot = _global_audit_batch_snapshot(batch)
    if (
        receipt.get("batch_snapshot") != snapshot
        or receipt.get("batch_snapshot_sha256") != _sha256_value(snapshot)
    ):
        raise CaptureError("aggregate audit receipt 与批次或逐题结果不一致")
    command = _global_audit_command(repo)
    if (
        receipt.get("audit_command") != command
        or receipt.get("audit_command_sha256") != _sha256_value(command)
    ):
        raise CaptureError("aggregate audit command/version 绑定非法")
    report = _normalize_global_audit_report(receipt.get("audit_report"))
    if receipt.get("audit_report_sha256") != _sha256_value(report):
        raise CaptureError("aggregate audit report 承诺非法")
    tools = receipt.get("audit_tool_commitments")
    objects = receipt.get("audited_object_commitments")
    if (
        receipt.get("audit_tool_commitments_sha256") != _sha256_value(tools)
        or receipt.get("audited_object_commitments_sha256")
        != _sha256_value(objects)
    ):
        raise CaptureError("aggregate audit 对象承诺哈希非法")
    _validate_audit_commitment_identity(
        tools,
        list(GLOBAL_AUDIT_TOOL_RELS),
        "aggregate audit tool commitments",
    )
    _validate_audit_commitment_identity(
        objects,
        _global_audit_object_paths(batch),
        "aggregate audited object commitments",
    )
    if require_current_objects:
        expected_tools = _audit_file_commitments(
            repo, list(GLOBAL_AUDIT_TOOL_RELS), "aggregate audit tool"
        )
        expected_objects = _audit_file_commitments(
            repo,
            _global_audit_object_paths(batch),
            "aggregate audited object",
        )
        if tools != expected_tools or objects != expected_objects:
            raise CaptureError("aggregate audit 后工具或审计对象已漂移")
    return receipt


def _persisted_close_payload(event: dict[str, Any]) -> dict[str, Any]:
    payload = {
        "batch_id": event["batch_id"],
        "status": event["status"],
        "global_closeout": event["global_closeout"],
        "needs_user_capture_ids": list(event.get("needs_user_capture_ids") or []),
        "failed_capture_ids": list(event.get("failed_capture_ids") or []),
    }
    if event.get("close_contract") is not None:
        payload.update(
            {
                "close_contract": event.get("close_contract"),
                "global_audit_receipt_sha256": event.get(
                    "global_audit_receipt_sha256"
                ),
            }
        )
    return payload


def close_batch(
    *,
    batch_id: str,
    global_closeout: str,
    idempotency_key: str,
    global_audit_receipt_sha256: str | None = None,
    repo_root: str | Path = ".",
) -> dict[str, Any]:
    batch_id = _validate_id(batch_id, "batch_id")
    key = _validate_id(idempotency_key, "idempotency_key")
    if global_closeout not in {"pass", "failed"}:
        raise CaptureError("global_closeout 必须是 pass 或 failed")
    repo = _resolve_repo(repo_root)
    root = capture_root(repo)
    with _ledger_lock(root):
        events = _load_events(root)
        _, by_key = _event_index(events)
        state = replay(events)
        existing = by_key.get(key)
        if existing:
            if (
                existing.get("event_type") != "curation_batch_closed"
                or existing.get("batch_id") != batch_id
                or existing.get("global_closeout") != global_closeout
            ):
                raise CaptureError("idempotency_key 已被不同批次收尾使用")
            persisted = _persisted_close_payload(existing)
            persisted_receipt = persisted.get("global_audit_receipt_sha256")
            if (
                global_audit_receipt_sha256 is not None
                and global_audit_receipt_sha256 != persisted_receipt
            ):
                raise CaptureError("日终批次已经按不同 aggregate receipt 关闭")
            if existing.get("close_sha256") != _sha256_value(persisted):
                raise CaptureError("已记录批次收尾的 close_sha256 非法")
            return {"event_status": "ALREADY_CLOSED", **persisted, "created": False}
        batch = state["batches"].get(batch_id)
        if not batch:
            raise CaptureError("未知 batch_id")
        if batch["status"] != "active":
            close_events = [
                event
                for event in events
                if event.get("event_type") == "curation_batch_closed"
                and event.get("batch_id") == batch_id
            ]
            if len(close_events) != 1:
                raise CaptureError("已关闭批次缺少唯一收尾事件")
            closed = close_events[0]
            if closed.get("global_closeout") != global_closeout:
                raise CaptureError("日终批次已经按不同语义关闭")
            persisted = _persisted_close_payload(closed)
            persisted_receipt = persisted.get("global_audit_receipt_sha256")
            if (
                global_audit_receipt_sha256 is not None
                and global_audit_receipt_sha256 != persisted_receipt
            ):
                raise CaptureError("日终批次已经按不同 aggregate receipt 关闭")
            if closed.get("close_sha256") != _sha256_value(persisted):
                raise CaptureError("已关闭批次的 close_sha256 非法")
            return {"event_status": "ALREADY_CLOSED", **persisted, "created": False}
        missing = sorted(set(batch["capture_ids"]) - set(batch["results"]))
        if missing:
            raise CaptureError(f"日终批次仍有未记录结果：{missing}")
        failed = sorted(
            capture_id
            for capture_id, value in batch["results"].items()
            if value["outcome"] == "failed"
        )
        needs_user = sorted(
            capture_id
            for capture_id, value in batch["results"].items()
            if value["outcome"] == "needs_user"
        )
        status = (
            "COMPLETE"
            if global_closeout == "pass" and not failed and not needs_user
            else "PARTIAL"
        )
        if global_closeout == "pass":
            if failed:
                raise CaptureError(
                    "failed 逐题结果不得声明 global_closeout=pass"
                )
            if global_audit_receipt_sha256 is None:
                raise CaptureError(
                    "global_closeout=pass 必须提供 aggregate audit receipt"
                )
            _validate_global_audit_receipt(
                repo,
                root,
                batch,
                global_audit_receipt_sha256,
                require_current_objects=True,
            )
        elif global_audit_receipt_sha256 is not None:
            raise CaptureError("global_closeout=failed 不得提供 PASS audit receipt")
        result_payload = {
            "batch_id": batch_id,
            "status": status,
            "global_closeout": global_closeout,
            "needs_user_capture_ids": needs_user,
            "failed_capture_ids": failed,
            "close_contract": GLOBAL_AUDIT_CLOSE_CONTRACT,
            "global_audit_receipt_sha256": global_audit_receipt_sha256,
        }
        result_hash = _sha256_value(result_payload)
        event = {
            "schema": EVENT_SCHEMA,
            "event_type": "curation_batch_closed",
            "event_id": f"CE-{hashlib.sha256(('close:' + key).encode()).hexdigest()[:20]}",
            "idempotency_key": key,
            "created_at": _now_utc(),
            "close_sha256": result_hash,
            **result_payload,
        }
        _append_event(root, events, event)
    return {"event_status": "CURATION_BATCH_CLOSED", **result_payload, "created": True}


def _shanghai_today() -> str:
    return dt.datetime.now(ZoneInfo(TIMEZONE)).date().isoformat()


def _capture_package_locators(capture_row: dict[str, Any]) -> list[str]:
    refs = (capture_row.get("capture") or {}).get("stable_evidence_refs") or []
    return sorted(
        {
            str(ref.get("locator") or "")
            for ref in refs
            if isinstance(ref, dict)
            and str(ref.get("locator") or "").startswith(
                "cs408-conversation-package://"
            )
        }
    )


def _capture_evidence_locators(capture_row: dict[str, Any]) -> list[str]:
    refs = (capture_row.get("capture") or {}).get("stable_evidence_refs") or []
    return sorted(
        {
            str(ref.get("locator") or "")
            for ref in refs
            if isinstance(ref, dict) and str(ref.get("locator") or "")
        }
    )


def _local_archive_recovery_status(
    package_locators: list[str], package_root: Path
) -> tuple[str | None, str | None]:
    """Return only durable local archive recovery evidence.

    Absence of an archive intent does not downgrade an otherwise canonical
    historical formal terminal.  Once an intent or pointer exists, however, the
    local package itself proves whether archive-only recovery remains.
    """

    for locator in package_locators:
        package_id = locator.removeprefix("cs408-conversation-package://")
        if not SAFE_ID_RE.fullmatch(package_id):
            return "damaged", "package_locator_invalid"
        matches = list(package_root.glob(f"????-??-??/{package_id}"))
        if len(matches) > 1:
            return "damaged", "package_locator_ambiguous"
        if not matches:
            continue
        package_dir = matches[0]
        pointer_path = package_dir / "archive-pointer.json"
        intent_path = package_dir / "archive-intent.json"
        if pointer_path.is_file():
            try:
                pointer = json.loads(pointer_path.read_text(encoding="utf-8"))
            except (OSError, UnicodeError, json.JSONDecodeError):
                return "damaged", "archive_pointer_unreadable"
            if pointer.get("schema_version") != "cs408-local-archive-pointer-v1":
                return "damaged", "archive_pointer_schema_invalid"
            pointer_status = str(pointer.get("status") or "")
            if pointer_status == "local_heavy_content_cleaned":
                return "archived", None
            if pointer_status == "cleanup_pending":
                return "archive_pending", "archive_cleanup_pending"
            return "damaged", "archive_pointer_status_invalid"
        if intent_path.exists():
            if intent_path.is_symlink() or not intent_path.is_file():
                return "damaged", "archive_intent_unsafe"
            return "archive_pending", "formal_committed_archive_pending"
    if package_locators:
        return "archive_pending", "verified_normal_archive_pointer_missing"
    return None, None


def _legacy_archive_recovery_status(
    *,
    repo: Path,
    capture_id: str,
    capture_payload_sha256: str,
    evidence_locators: list[str],
    legacy_root: Path,
    archive_root: Path,
    decision_root: Path | None,
) -> tuple[str, str | None]:
    legacy = [
        locator
        for locator in evidence_locators
        if locator.startswith("current-question-evidence://sha256/")
    ]
    if len(legacy) > 1:
        return "legacy_archive_pending", "legacy_evidence_locator_ambiguous"
    if legacy:
        archive_locator = legacy[0]
        archive_identity_sha = archive_locator.removeprefix(
            "current-question-evidence://sha256/"
        )
    else:
        if not SHA256_RE.fullmatch(capture_payload_sha256):
            return "legacy_archive_pending", "legacy_capture_payload_sha_invalid"
        archive_identity_sha = capture_payload_sha256
        archive_locator = (
            "cs408-ledger-only://sha256/" + archive_identity_sha
        )
    roots = [legacy_root]
    migrated = legacy_root / "legacy-verified"
    if migrated.is_dir() and not migrated.is_symlink():
        roots.append(migrated.resolve())
    pointer_roots = [
        root
        for root in roots
        if (
            root
            / "legacy-archive-pointers"
            / f"{archive_identity_sha}.json"
        ).is_file()
    ]
    if not pointer_roots:
        return "legacy_archive_pending", "legacy_archive_pointer_missing"
    if len(pointer_roots) != 1:
        return "legacy_archive_pending", "legacy_archive_pointer_ambiguous"
    try:
        import archive_408_conversation_package as archive_module

        archive_module.verify_legacy_archive_for_current_capture(
            repo=repo,
            capture_id=capture_id,
            evidence_locator=archive_locator,
            evidence_root=pointer_roots[0],
            decision_root=decision_root,
            archive_root=archive_root,
        )
    except Exception as exc:
        return "legacy_archive_pending", f"legacy_archive_proof_invalid:{exc}"
    return "legacy_archived", None


def _committed_receipt_index(
    repo: Path, runtime_root: Path
) -> dict[tuple[str, str], dict[str, Any]]:
    """Index exact COMMITTED receipt/WAL capture bindings without mutation."""

    receipt_root = runtime_root / "receipts" / _repo_key(repo)
    transaction_root = runtime_root / "transactions" / _repo_key(repo)
    if not receipt_root.is_dir() or receipt_root.is_symlink():
        return {}
    paths = sorted(receipt_root.glob("*.json"))
    if len(paths) > MAX_BACKLOG_RECEIPTS:
        raise CaptureError("formal receipt backlog 超过只读 planner 上限")
    index: dict[tuple[str, str], dict[str, Any]] = {}
    for path in paths:
        try:
            if path.is_symlink() or not path.is_file():
                continue
            info = path.stat()
            if info.st_size > MAX_NORMAL_RECEIPT_BYTES:
                continue
            raw = path.read_bytes()
            receipt = json.loads(raw.decode("utf-8"))
            if (
                not isinstance(receipt, dict)
                or receipt.get("schema") != NORMAL_RECEIPT_SCHEMA
                or receipt.get("status") != "COMMITTED"
                or Path(str(receipt.get("repo_root") or "")).resolve() != repo
            ):
                continue
            idempotency_key = str(receipt.get("idempotency_key") or "")
            if (
                not idempotency_key
                or path.name
                != hashlib.sha256(idempotency_key.encode("utf-8")).hexdigest()
                + ".json"
            ):
                continue
            transaction_id = str(receipt.get("transaction_id") or "")
            if not transaction_id:
                continue
            journal_ref = Path(str(receipt.get("journal") or "")).expanduser()
            expected_journal = transaction_root / transaction_id / "journal.json"
            if (
                not journal_ref.is_absolute()
                or journal_ref.resolve() != expected_journal.resolve()
                or not journal_ref.is_file()
                or journal_ref.is_symlink()
                or journal_ref.stat().st_size > MAX_NORMAL_WAL_BYTES
            ):
                continue
            journal = json.loads(journal_ref.read_text(encoding="utf-8"))
            if (
                not isinstance(journal, dict)
                or journal.get("schema") != NORMAL_WAL_SCHEMA
                or journal.get("state") != "committed"
                or journal.get("result_items") != receipt.get("items")
                or any(
                    journal.get(field) != receipt.get(field)
                    for field in (
                        "transaction_id",
                        "idempotency_key",
                        "payload_sha256",
                        "repo_root",
                    )
                )
            ):
                continue
            payload_identity = journal.get("payload_identity")
            if (
                not isinstance(payload_identity, dict)
                or _sha256_value(payload_identity)
                != journal.get("payload_sha256")
            ):
                continue
            packages = payload_identity.get("packages")
            items = receipt.get("items")
            if (
                not isinstance(packages, list)
                or len(packages) != 1
                or not isinstance(packages[0], dict)
                or not isinstance(items, list)
                or len(items) != 1
                or not isinstance(items[0], dict)
            ):
                continue
            binding = packages[0].get("capture_binding")
            if not isinstance(binding, dict):
                continue
            batch_id = str(binding.get("batch_id") or "")
            capture_id = str(binding.get("capture_id") or "")
            if not batch_id or not capture_id:
                continue
            key = (batch_id, capture_id)
            value = {
                "receipt_sha256": hashlib.sha256(raw).hexdigest(),
                "transaction_id": transaction_id,
                "formal_id": items[0].get("formal_id"),
                "mode": items[0].get("mode"),
                "status": "COMMITTED",
            }
            if key in index and index[key] != value:
                raise CaptureError(
                    f"{batch_id}/{capture_id} 存在多个不同 COMMITTED receipts"
                )
            index[key] = value
        except (OSError, UnicodeError, json.JSONDecodeError, ValueError):
            continue
    return index


def backlog_plan(
    *,
    cutoff_date: str | None = None,
    only_today: bool = False,
    capture_ids: list[str] | None = None,
    package_locators: list[str] | None = None,
    repo_root: str | Path = ".",
    runtime_root: str | Path = DEFAULT_RUNTIME_ROOT,
    package_root: str | Path = (
        "/Users/your-user/.codex/kaoyan-408-conversation-packages"
    ),
    legacy_root: str | Path | None = None,
    archive_root: str | Path = "/Volumes/T9-Data",
    decision_root: str | Path | None = None,
) -> dict[str, Any]:
    """Build a canonical, read-only, cross-date curation backlog plan."""

    repo = _resolve_repo(repo_root)
    cutoff = _validate_date(cutoff_date or _shanghai_today(), "cutoff_date")
    root = capture_root(repo)
    ledger = root / "events.jsonl"
    ledger_raw = ledger.read_bytes() if ledger.is_file() else b""
    events = _load_events(root)
    state = replay(events)
    proofs = _canonical_commit_proofs(root, events)
    runtime = Path(runtime_root).expanduser().resolve()
    private_packages = Path(package_root).expanduser().resolve()
    private_legacy = (
        Path(legacy_root).expanduser().resolve()
        if legacy_root is not None
        else private_packages
    )
    t9_archive = Path(archive_root).expanduser().resolve()
    private_decisions = (
        Path(decision_root).expanduser().resolve()
        if decision_root is not None
        else None
    )

    exact_capture_ids = sorted(
        {
            _validate_id(value, "capture_ids[]")
            for value in (capture_ids or [])
        }
    )
    exact_package_locators = sorted(
        {str(value or "").strip() for value in (package_locators or [])}
    )
    if any(not value for value in exact_package_locators):
        raise CaptureError("package_locators[] 不能为空")
    all_capture_ids = set(state["captures"])
    unknown_capture_ids = sorted(set(exact_capture_ids) - all_capture_ids)
    if unknown_capture_ids:
        raise CaptureError(f"精确 capture subset 含未知 ID：{unknown_capture_ids}")

    package_matches: dict[str, list[str]] = {}
    for locator in exact_package_locators:
        matches = sorted(
            capture_id
            for capture_id, row in state["captures"].items()
            if locator in _capture_evidence_locators(row)
        )
        if len(matches) != 1:
            raise CaptureError(
                f"package subset 必须唯一映射一个 capture：{locator} -> {matches}"
            )
        package_matches[locator] = matches
    package_capture_ids = {
        capture_id for matches in package_matches.values() for capture_id in matches
    }
    exact_subset = set(exact_capture_ids) | package_capture_ids
    subset_enabled = bool(exact_capture_ids or exact_package_locators)

    committed_receipts = _committed_receipt_index(repo, runtime)
    active_batches = [
        batch
        for batch in state["batches"].values()
        if batch.get("status") == "active"
    ]
    active_batch = active_batches[0] if active_batches else None
    rows_by_date: dict[str, dict[str, Any]] = {}
    future_excluded: list[dict[str, str]] = []
    trusted_terminal_excluded: list[dict[str, str]] = []
    administrative_terminal_excluded: list[dict[str, str]] = []

    def day_row(date: str) -> dict[str, Any]:
        return rows_by_date.setdefault(
            date,
            {
                "study_date": date,
                "processable_items": [],
                "residual_items": [],
                "terminal_items": [],
            },
        )

    for capture_id, capture_row in sorted(
        state["captures"].items(),
        key=lambda item: (str(item[1].get("study_date") or ""), item[0]),
    ):
        date = str(capture_row.get("study_date") or "")
        if date > cutoff:
            future_excluded.append(
                {
                    "study_date": date,
                    "capture_id": capture_id,
                    "status": str(capture_row.get("quality_status") or ""),
                }
            )
            continue
        if only_today and date != cutoff:
            continue
        if subset_enabled and capture_id not in exact_subset:
            continue
        capture_policy = str(
            ((capture_row.get("capture") or {}).get("authorization_policy") or "")
        )
        if (
            capture_policy == WEB_ENHANCEMENT_PENDING_POLICY
            and not subset_enabled
        ):
            # New study episodes are intentionally invisible to the ordinary
            # cutoff backlog.  The web-enhancement owner must name the exact
            # Capture/package subset after reviewing the exported day bundle.
            continue
        if capture_row.get("formalization_authorized") is not True:
            continue

        packages = _capture_package_locators(capture_row)
        common = {
            "study_date": date,
            "capture_id": capture_id,
            "package_locators": packages,
            "evidence_locators": _capture_evidence_locators(capture_row),
            "quality_status": capture_row.get("quality_status"),
            "current_batch_id": capture_row.get("current_batch_id"),
        }
        day = day_row(date)
        proof = proofs.get(capture_id)
        common.update(
            {
                "canonical_commit_proof_status": (
                    str(proof.get("status") or "")
                    if isinstance(proof, dict)
                    else "missing"
                ),
                "canonical_commit_proof_reason": (
                    str(proof.get("reason_code") or "")
                    if isinstance(proof, dict)
                    else "canonical_commit_proof_missing"
                ),
            }
        )

        quality = str(capture_row.get("quality_status") or "")
        if quality == "user_removed":
            retirement = capture_row.get("retirement") or {}
            administrative_terminal_excluded.append(
                {
                    "study_date": date,
                    "capture_id": capture_id,
                    "status": "user_removed",
                    "reason": str(
                        retirement.get("reason")
                        or NEEDS_USER_RETIREMENT_REASON
                    ),
                    "prior_batch_id": str(
                        retirement.get("prior_batch_id") or ""
                    ),
                }
            )
            continue
        batch_id = str(capture_row.get("current_batch_id") or "")
        batch = state["batches"].get(batch_id)
        active = isinstance(batch, dict) and batch.get("status") == "active"
        result = (batch.get("results") or {}).get(capture_id) if active else None
        if quality in SUCCESS_OUTCOMES:
            if active and isinstance(result, dict):
                day["processable_items"].append(
                    {
                        **common,
                        "status": (
                            f"active_result_{quality}_closeout_pending"
                        ),
                        "reason": "active batch already has this canonical result",
                        "next_action": "close_active_batch_then_replay_plan",
                        "apply_allowed": False,
                    }
                )
            evidence_locators = _capture_evidence_locators(capture_row)
            if not packages:
                archive_status, archive_reason = _legacy_archive_recovery_status(
                    repo=repo,
                    capture_id=capture_id,
                    capture_payload_sha256=str(
                        capture_row.get("payload_sha256") or ""
                    ),
                    evidence_locators=evidence_locators,
                    legacy_root=private_legacy,
                    archive_root=t9_archive,
                    decision_root=private_decisions,
                )
            else:
                archive_status, archive_reason = _local_archive_recovery_status(
                    packages, private_packages
                )
            if archive_status == "legacy_archive_pending":
                day["residual_items"].append(
                    {
                        **common,
                        "status": "legacy_archive_pending",
                        "reason": "FORMAL_COMMITTED_ARCHIVE_PENDING",
                        "archive_reason_detail": archive_reason,
                        "next_action": (
                            "preview_then_apply_legacy_archive_only_do_not_reapply"
                        ),
                        "apply_allowed": False,
                    }
                )
                continue
            if archive_status in {"archive_pending", "damaged"}:
                day["residual_items"].append(
                    {
                        **common,
                        "status": archive_status,
                        "reason": archive_reason,
                        "next_action": (
                            "resume_archive_only_do_not_reapply"
                            if archive_status == "archive_pending"
                            else "repair_archive_evidence_only"
                        ),
                        "apply_allowed": False,
                    }
                )
            else:
                terminal = {
                    **common,
                    "status": "trusted_formal_terminal",
                    "reason": quality,
                    "archive_status": archive_status or "not_locally_pending",
                    "apply_allowed": False,
                }
                day["terminal_items"].append(terminal)
                trusted_terminal_excluded.append(
                    {
                        "study_date": date,
                        "capture_id": capture_id,
                        "status": quality,
                    }
                )
            continue
        if quality == "needs_user":
            if active and isinstance(result, dict):
                day["processable_items"].append(
                    {
                        **common,
                        "status": "active_result_needs_user_closeout_pending",
                        "reason": "active batch already has this canonical result",
                        "next_action": "close_active_batch_then_replay_plan",
                        "apply_allowed": False,
                    }
                )
            day["residual_items"].append(
                {
                    **common,
                    "status": "needs_user",
                    "reason": str(capture_row.get("last_error") or "needs_user"),
                    "next_action": "wait_for_exact_user_resolution",
                    "apply_allowed": False,
                }
            )
            continue

        if active and isinstance(result, dict):
            outcome = str(result.get("outcome") or "")
            day["processable_items"].append(
                {
                    **common,
                    "status": f"active_result_{outcome}_closeout_pending",
                    "reason": "active batch already has this canonical result",
                    "next_action": "close_active_batch_then_replay_plan",
                    "apply_allowed": False,
                }
            )
            continue
        if active:
            committed = committed_receipts.get((batch_id, capture_id))
            if committed is not None:
                day["processable_items"].append(
                    {
                        **common,
                        "status": "formal_committed_closeout_pending",
                        "reason": "canonical COMMITTED receipt/WAL already binds item",
                        "next_action": "resume_backflow_mark_result_closeout_only",
                        "normal_receipt_sha256": committed["receipt_sha256"],
                        "formal_id": committed["formal_id"],
                        "apply_allowed": False,
                    }
                )
            else:
                day["processable_items"].append(
                    {
                        **common,
                        "status": "active_item_unfinished",
                        "reason": "active batch item lacks canonical result",
                        "next_action": "resume_active_item",
                        "apply_allowed": True,
                    }
                )
            continue
        if quality == "curating":
            day["residual_items"].append(
                {
                    **common,
                    "status": "damaged",
                    "reason": "curating capture is not bound to an active batch",
                    "next_action": "repair_batch_projection_before_any_apply",
                    "apply_allowed": False,
                }
            )
            continue
        if quality == "curation_failed":
            next_status = "retry_failed"
            next_action = "start_exact_partial_retry"
            apply_allowed = True
        elif quality == "needs_user_resolved":
            resolution = capture_row.get("resolution") or {}
            if resolution.get("normal_receipt_sha256"):
                next_status = "formal_committed_closeout_pending"
                next_action = "start_retry_then_record_already_current_no_apply"
                apply_allowed = False
            else:
                next_status = "retry_resolved"
                next_action = "start_exact_partial_retry"
                apply_allowed = True
        elif quality in {
            PENDING_FORMAL_STATUS,
            AWAITING_WEB_ENHANCEMENT_STATUS,
        }:
            next_status = "pending_start"
            next_action = "start_date_batch"
            apply_allowed = True
        else:
            day["residual_items"].append(
                {
                    **common,
                    "status": "damaged",
                    "reason": f"unsupported replay quality status: {quality}",
                    "next_action": "repair_capture_state_before_any_apply",
                    "apply_allowed": False,
                }
            )
            continue
        day["processable_items"].append(
            {
                **common,
                "status": next_status,
                "reason": quality,
                "next_action": next_action,
                "apply_allowed": apply_allowed,
            }
        )

    days: list[dict[str, Any]] = []
    for date in sorted(rows_by_date):
        day = rows_by_date[date]
        if not day["processable_items"] and not day["residual_items"]:
            continue
        day_active = (
            active_batch
            if isinstance(active_batch, dict)
            and active_batch.get("study_date") == date
            else None
        )
        statuses = {
            str(item["status"]) for item in day["processable_items"]
        }
        if day_active is not None:
            mode = "resume_active_batch"
            start_capture_ids: list[str] | None = None
        elif statuses & {"retry_failed", "retry_resolved", "formal_committed_closeout_pending"}:
            mode = "start_partial_retry"
            partial_batches = [
                batch
                for batch in state["batches"].values()
                if batch.get("study_date") == date
                and batch.get("status") == "PARTIAL"
            ]
            latest_partial = max(
                partial_batches,
                key=lambda value: (
                    int(value.get("attempt", 1)),
                    str(value.get("batch_id") or ""),
                ),
                default=None,
            )
            start_capture_ids = (
                list(latest_partial.get("capture_ids") or [])
                if latest_partial is not None
                else sorted(
                    item["capture_id"] for item in day["processable_items"]
                )
            )
        elif day["processable_items"]:
            mode = "start_new_batch"
            start_capture_ids = (
                sorted(item["capture_id"] for item in day["processable_items"])
                if subset_enabled
                else None
            )
        else:
            mode = "residual_only"
            start_capture_ids = None
        day.update(
            {
                "mode": mode,
                "active_batch_id": (
                    day_active.get("batch_id") if day_active else None
                ),
                "start_batch_capture_ids": start_capture_ids,
                "no_cross_date_batch": True,
            }
        )
        days.append(day)

    processable = [
        item for day in days for item in day["processable_items"]
    ]
    residual = [item for day in days for item in day["residual_items"]]
    active_after_cutoff = (
        isinstance(active_batch, dict)
        and str(active_batch.get("study_date") or "") > cutoff
    )
    plan_material = {
        "schema": BACKLOG_PLAN_SCHEMA,
        "cutoff_date": cutoff,
        "timezone": TIMEZONE,
        "only_today": bool(only_today),
        "exact_capture_ids": exact_capture_ids,
        "exact_package_locators": exact_package_locators,
        "package_capture_map": package_matches,
        "ledger_sha256": hashlib.sha256(ledger_raw).hexdigest(),
        "ledger_event_count": len(events),
        "execution_order": [day["study_date"] for day in days],
        "days": days,
        "processable_pending_count": len(processable),
        "residual_count": len(residual),
        "future_excluded": future_excluded,
        "trusted_terminal_excluded": trusted_terminal_excluded,
        "administrative_terminal_excluded": (
            administrative_terminal_excluded
        ),
        "global_active_batch": (
            {
                "batch_id": active_batch.get("batch_id"),
                "study_date": active_batch.get("study_date"),
                "status": active_batch.get("status"),
            }
            if active_batch
            else None
        ),
        "global_blocker": (
            "future_active_batch_must_close_before_cutoff_backlog"
            if active_after_cutoff
            else None
        ),
        "read_only": True,
        "formal_write_count": 0,
    }
    final_gate = {
        "schema": BACKLOG_FINAL_GATE_SCHEMA,
        "status": (
            "PASS" if not processable and not active_after_cutoff else "PENDING"
        ),
        "cutoff_date": cutoff,
        "processable_pending_count": len(processable),
        "processable_pending": [
            {
                key: item.get(key)
                for key in (
                    "study_date",
                    "capture_id",
                    "status",
                    "reason",
                    "next_action",
                )
            }
            for item in processable
        ],
        "residual_count": len(residual),
        "residual_items": [
            {
                key: item.get(key)
                for key in (
                    "study_date",
                    "capture_id",
                    "status",
                    "reason",
                    "next_action",
                )
            }
            for item in residual
        ],
        "global_blocker": plan_material["global_blocker"],
    }
    result = {**plan_material, "final_gate": final_gate}
    result["canonical_plan_sha256"] = _sha256_value(result)
    return result


def status(study_date: str | None, *, repo_root: str | Path = ".") -> dict[str, Any]:
    root = capture_root(repo_root)
    events = _load_events(root)
    state = replay(events)
    commit_proofs = _canonical_commit_proofs(root, events)
    for capture_id, row in state["captures"].items():
        proof = commit_proofs.get(capture_id)
        if not isinstance(proof, dict):
            raise CaptureError(f"{capture_id} 缺少 canonical commit proof")
        row["canonical_commit_proof"] = proof
        row["formal_write_count"] = 0
    if not study_date:
        return state
    date = _validate_date(study_date)
    captures = {
        capture_id: value
        for capture_id, value in state["captures"].items()
        if value["study_date"] == date
    }
    neutral_saves = {
        save_id: value
        for save_id, value in state.get("neutral_saves", {}).items()
        if value["study_date"] == date
    }
    def batch_summary(batch: dict[str, Any] | None) -> dict[str, Any] | None:
        if batch is None:
            return None
        return {
            "batch_id": batch["batch_id"],
            "study_date": batch["study_date"],
            "attempt": int(batch.get("attempt", 1)),
            "status": batch["status"],
            "capture_set_sha256": batch["capture_set_sha256"],
            "completed_count": len(batch.get("results") or {}),
            "total_count": len(batch.get("capture_ids") or []),
            "retry_of_batch_id": batch.get("retry_of_batch_id"),
            "retry_capture_ids": list(batch.get("retry_capture_ids") or []),
            "carried_capture_ids": list(batch.get("carried_capture_ids") or []),
            "close_contract": batch.get("close_contract"),
            "global_audit_receipt_sha256": batch.get(
                "global_audit_receipt_sha256"
            ),
        }

    active = next(
        (
            batch
            for batch in state["batches"].values()
            if batch.get("status") == "active"
        ),
        None,
    )
    dated_batches = [
        batch
        for batch in state["batches"].values()
        if batch.get("study_date") == date
    ]
    latest = max(
        dated_batches,
        key=lambda batch: (int(batch.get("attempt", 1)), str(batch["batch_id"])),
        default=None,
    )
    return {
        "schema": STATE_SCHEMA,
        "study_date": date,
        "capture_count": len(captures),
        "neutral_save_count": len(neutral_saves),
        "pending_capture_ids": state["pending_by_date"].get(date, []),
        "captures": captures,
        "neutral_saves": neutral_saves,
        "active_batch": batch_summary(active),
        "latest_date_batch": batch_summary(latest),
    }


def reconcile(*, repo_root: str | Path = ".") -> dict[str, Any]:
    root = capture_root(repo_root)
    with _ledger_lock(root):
        events = _load_events(root)
        state = replay(events)
        _atomic_write(
            root / "state.json",
            json.dumps(state, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        )
    return {"status": "RECONCILED", "event_count": len(events)}


def _audit_ordinary_review_binding(
    repo: Path,
    capture_id: str,
    stored_capture: dict[str, Any],
    refs: list[dict[str, Any]],
) -> int:
    ordinary_refs = [
        ref for ref in refs if ref.get("kind") == ORDINARY_REVIEW_REF_KIND
    ]
    if len(ordinary_refs) != 1:
        raise CaptureError(f"{capture_id} 缺少唯一普通学习 event 证据")
    forbidden_chain = {
        "review_loop_event",
        "morning_session_start",
        "morning_session_first",
        "morning_session_queue",
        "linked_practice_followup",
        "linked_practice_parent_trigger",
        "linked_practice_source",
        "personalization_selection_receipt",
    }
    kinds = {str(ref.get("kind")) for ref in refs}
    if kinds & forbidden_chain:
        raise CaptureError(f"{capture_id} 的普通学习证据不得混入晨间证据链")
    ref = ordinary_refs[0]
    locator = str(ref.get("locator") or "")
    prefix = f"{REVIEW_LOOP_LEDGER_REL}#event_id="
    if not locator.startswith(prefix):
        raise CaptureError(f"{capture_id} 的普通学习 event locator 非法")
    event = _review_event_by_id(repo, locator[len(prefix) :])
    expected_sha = _validate_sha(
        ref.get("sha256"), f"{capture_id}.ordinary_review_loop_event.sha256"
    )
    if _sha256_value(event) != expected_sha:
        raise CaptureError(f"{capture_id} 的 ordinary_review_loop_event SHA-256 不一致")

    expected = _validate_capture_payload(_ordinary_review_capture_payload(event))
    if stored_capture.get("study_date") != expected["study_date"]:
        raise CaptureError(f"{capture_id} 的 study_date 与 ordinary review event 不一致")
    if (
        stored_capture.get("source_facts", {}).get("source_id")
        != expected["source_facts"]["source_id"]
    ):
        raise CaptureError(f"{capture_id} 的 source_id 与 ordinary review event 不一致")
    if stored_capture.get("identity_hint") != expected["identity_hint"]:
        raise CaptureError(f"{capture_id} 的 formal identity 与 ordinary review event 不一致")
    if stored_capture.get("user_facts") != expected["user_facts"]:
        raise CaptureError(f"{capture_id} 的直接 provenance 与 ordinary review event 不一致")
    if stored_capture.get("formalization_authorized") is not False:
        raise CaptureError(f"{capture_id} 不得由 ordinary review event 自动授权正式编纂")
    if stored_capture != expected:
        raise CaptureError(f"{capture_id} 的 ordinary review capture 载荷与事件绑定不一致")
    return 1


def _audit_review_evidence_refs(repo: Path, state: dict[str, Any]) -> int:
    checked = 0
    for capture_id, row in state["captures"].items():
        refs = row["capture"].get("stable_evidence_refs") or []
        kinds = {str(ref.get("kind")) for ref in refs}
        if ORDINARY_REVIEW_REF_KIND in kinds:
            checked += _audit_ordinary_review_binding(
                repo, capture_id, row["capture"], refs
            )
            continue
        if "review_loop_event" not in kinds:
            continue
        required = {
            "review_loop_event",
            "morning_session_start",
            "morning_session_first",
            "morning_session_queue",
        }
        if not required.issubset(kinds):
            raise CaptureError(f"{capture_id} 缺少晨间回流证据链")
        linked_required = {
            "linked_practice_followup",
            "linked_practice_parent_trigger",
            "linked_practice_source",
            "personalization_selection_receipt",
        }
        review_ref = next(
            (ref for ref in refs if ref.get("kind") == "review_loop_event"), None
        )
        review_prefix = f"{REVIEW_LOOP_LEDGER_REL}#event_id="
        if review_ref and str(review_ref.get("locator", "")).startswith(review_prefix):
            bound_review = _review_event_by_id(
                repo, str(review_ref["locator"])[len(review_prefix) :]
            )
            if bound_review.get("practice_mode") in {"original", "variant"} and not (
                linked_required.issubset(kinds)
            ):
                raise CaptureError(f"{capture_id} 的动态关联练习缺少专用证据链")
            if bound_review.get("practice_mode") in {"original", "variant"}:
                source_ref_row = next(
                    ref
                    for ref in refs
                    if ref.get("kind") == "linked_practice_source"
                )
                selection_ref_row = next(
                    ref
                    for ref in refs
                    if ref.get("kind") == "personalization_selection_receipt"
                )
                source_path, source_ref = _linked_source_path(
                    repo, source_ref_row.get("locator")
                )
                try:
                    verified_source = validate_source_for_practice(
                        repo,
                        source_ref,
                        source_ref_row.get("sha256"),
                        practice_mode=str(bound_review.get("practice_mode")),
                        formal_node_id=bound_review.get("formal_node_id"),
                        variant_of_formal_node_id=bound_review.get(
                            "variant_of_formal_node_id"
                        ),
                    )
                    validate_selection_receipt(
                        repo,
                        selection_ref_row.get("locator"),
                        selection_ref_row.get("sha256"),
                        expected={
                            "slice_id": bound_review.get("context_slice_id"),
                            "trigger_event_id": bound_review.get(
                                "parent_trigger_event_id"
                            ),
                            "candidate_id": bound_review.get("candidate_id"),
                            "practice_mode": bound_review.get("practice_mode"),
                            "formal_node_id": bound_review.get("formal_node_id"),
                            "anchor_formal_node_id": bound_review.get(
                                "anchor_formal_node_id"
                            ),
                            "source_id": bound_review.get("source_id"),
                            "source_ref": source_ref,
                            "source_sha256": bound_review.get("source_sha256"),
                            "mechanism_key": bound_review.get("mechanism_key"),
                            "knowledge_point": bound_review.get("knowledge_point"),
                            "review_unit_id": bound_review.get("review_unit_id"),
                        },
                        source_path=verified_source["path"],
                    )
                except LinkedPracticeSourceError as exc:
                    raise CaptureError(str(exc)) from exc
        if kinds & linked_required and not linked_required.issubset(kinds):
            raise CaptureError(f"{capture_id} 缺少关联练习证据链")
        for ref in refs:
            kind = str(ref.get("kind"))
            locator = str(ref.get("locator"))
            expected_sha = str(ref.get("sha256"))
            if kind in {"review_loop_event", "linked_practice_parent_trigger"}:
                prefix = f"{REVIEW_LOOP_LEDGER_REL}#event_id="
                if not locator.startswith(prefix):
                    raise CaptureError(f"{capture_id} 的 review event locator 非法")
                event = _review_event_by_id(repo, locator[len(prefix) :])
                if kind == "linked_practice_parent_trigger" and (
                    event.get("event_kind") != "outcome"
                    or event.get("practice_mode")
                ):
                    raise CaptureError(f"{capture_id} 的 linked parent trigger 非法")
                actual_sha = _sha256_value(event)
            elif kind in {
                "morning_session_start",
                "morning_session_first",
                "linked_practice_followup",
            }:
                marker = "#sequence="
                if marker not in locator:
                    raise CaptureError(f"{capture_id} 的 session locator 非法")
                relative, raw_sequence = locator.rsplit(marker, 1)
                try:
                    sequence = int(raw_sequence)
                except ValueError as exc:
                    raise CaptureError(f"{capture_id} 的 session sequence 非法") from exc
                if kind == "morning_session_start" and sequence != 1:
                    raise CaptureError(f"{capture_id} 的 session start sequence 非法")
                path = (repo / relative).resolve()
                try:
                    path.relative_to(repo)
                except ValueError as exc:
                    raise CaptureError(f"{capture_id} 的 session locator 越界") from exc
                try:
                    session_events = [
                        json.loads(line)
                        for line in path.read_text(encoding="utf-8").splitlines()
                        if line.strip()
                    ]
                    selected = session_events[sequence - 1]
                except (OSError, IndexError, json.JSONDecodeError) as exc:
                    raise CaptureError(f"{capture_id} 的 session 事件不可读") from exc
                if selected.get("sequence") != sequence:
                    raise CaptureError(f"{capture_id} 的 session sequence 漂移")
                if kind == "linked_practice_followup" and selected.get(
                    "event_type"
                ) != "followup_scheduled":
                    raise CaptureError(f"{capture_id} 的 followup 事件类型非法")
                actual_sha = _sha256_value(selected)
            elif kind in {
                "morning_session_queue",
                "linked_practice_source",
                "personalization_selection_receipt",
            }:
                if kind == "linked_practice_source":
                    path, _ = _linked_source_path(repo, locator)
                elif kind == "personalization_selection_receipt":
                    try:
                        path, _, _ = resolve_selection_receipt(repo, locator)
                    except LinkedPracticeSourceError as exc:
                        raise CaptureError(str(exc)) from exc
                else:
                    path = (repo / locator).resolve()
                    try:
                        path.relative_to(repo)
                    except ValueError as exc:
                        raise CaptureError(f"{capture_id} 的 queue locator 越界") from exc
                if not path.is_file():
                    raise CaptureError(f"{capture_id} 的文件证据缺失")
                actual_sha = _sha256_file(path)
            else:
                continue
            if actual_sha != expected_sha:
                raise CaptureError(f"{capture_id} 的 {kind} SHA-256 不一致")
            checked += 1
    return checked


def _verify_neutral_source_transaction(
    repo: Path, material: dict[str, Any]
) -> None:
    source_kind = str(material["source_transaction_kind"])
    source_sha = str(material["source_transaction_attestation_sha256"])

    def read_json(path: Path, *, expected_sha: str | None = None) -> tuple[dict[str, Any], bytes]:
        try:
            resolved = path.resolve()
            resolved.relative_to(repo)
            info = path.lstat()
        except (OSError, ValueError) as exc:
            raise CaptureError("中性保存 source transaction 回执缺失") from exc
        if stat.S_ISLNK(info.st_mode) or not stat.S_ISREG(info.st_mode):
            raise CaptureError("中性保存 source transaction 回执类型非法")
        try:
            raw = path.read_bytes()
            value = json.loads(raw)
        except (OSError, UnicodeError, json.JSONDecodeError) as exc:
            raise CaptureError("中性保存 source transaction 回执损坏") from exc
        if not isinstance(value, dict):
            raise CaptureError("中性保存 source transaction 回执内容非法")
        if expected_sha is not None and hashlib.sha256(raw).hexdigest() != expected_sha:
            raise CaptureError("中性保存 source transaction 回执哈希漂移")
        return value, raw

    def canonical(value: Any, *, trailing_lf: bool) -> bytes:
        raw = json.dumps(
            value, ensure_ascii=False, sort_keys=True, separators=(",", ":")
        ).encode("utf-8")
        return raw + (b"\n" if trailing_lf else b"")

    try:
        if source_kind == "prepared_option_turn":
            root = repo / (
                "wiki/study_vaults/408-full/state/managed-408-turn-broker"
            )
            attestation, _ = read_json(
                root / "objects" / "transactions" / f"{source_sha}.json",
                expected_sha=source_sha,
            )
            request_id = str(attestation.get("request_id") or "")
            payload = dict(attestation)
            transaction_id = payload.pop("transaction_id", None)
            expected_id = (
                "MTA-"
                + hashlib.sha256(canonical(payload, trailing_lf=True))
                .hexdigest()[:24]
                .upper()
            )
            if (
                attestation.get("schema")
                != "managed_408_transaction_attestation_v1"
                or attestation.get("kind") != "frozen_prepared_option_turn"
                or transaction_id != expected_id
                or attestation.get("display_receipt_sha256")
                != material["display_receipt_sha256"]
                or attestation.get("review_event_id")
                != material["review_event_id"]
                or attestation.get("session_result")
                not in MORNING_CORRECT_RESULTS
                or attestation.get("reply_body_stored") is not False
                or attestation.get("formal_write_count") != 0
                or not SHA256_RE.fullmatch(
                    str(attestation.get("broker_build_sha256") or "")
                )
            ):
                raise CaptureError("中性保存 option source transaction 绑定非法")
            key_sha = hashlib.sha256(
                canonical(
                    {"namespace": "transaction", "logical_key": request_id},
                    trailing_lf=True,
                )
            ).hexdigest()
            pointer, _ = read_json(
                root / "pointers" / "transaction" / f"{key_sha}.json"
            )
            if pointer != {
                "schema": "managed_408_turn_broker_pointer_v1",
                "namespace": "transaction",
                "key_sha256": key_sha,
                "object_sha256": source_sha,
            }:
                raise CaptureError("中性保存 option source pointer 绑定非法")
        elif source_kind == "rich_morning_turn":
            root = repo / (
                "wiki/study_vaults/408-full/state/managed-408-rich-intake-turn"
            )
            attestation, _ = read_json(
                root / "objects" / "attestation" / f"{source_sha}.json",
                expected_sha=source_sha,
            )
            request_id = str(attestation.get("request_id") or "")
            payload = dict(attestation)
            attestation_id = payload.pop("attestation_id", None)
            expected_id = (
                "MRI-"
                + hashlib.sha256(canonical(payload, trailing_lf=True))
                .hexdigest()[:24]
                .upper()
            )
            if (
                attestation.get("schema")
                != "managed_408_rich_intake_attestation_v1"
                or attestation.get("kind") != "rich_morning_finalized"
                or attestation_id != expected_id
                or attestation.get("display_receipt_sha256")
                != material["display_receipt_sha256"]
                or attestation.get("review_event_id")
                != material["review_event_id"]
                or attestation.get("reply_body_stored") is not False
                or attestation.get("formal_write_count") != 0
                or not SHA256_RE.fullmatch(
                    str(attestation.get("broker_build_sha256") or "")
                )
                or not SHA256_RE.fullmatch(
                    str(attestation.get("rich_build_sha256") or "")
                )
            ):
                raise CaptureError("中性保存 rich source transaction 绑定非法")
            key_sha = hashlib.sha256(
                canonical(
                    {
                        "namespace": "attestation_request",
                        "logical_key": request_id,
                    },
                    trailing_lf=True,
                )
            ).hexdigest()
            pointer, _ = read_json(
                root
                / "pointers"
                / "attestation_request"
                / f"{key_sha}.json"
            )
            if pointer != {
                "schema": "managed_408_rich_intake_pointer_v1",
                "namespace": "attestation_request",
                "key_sha256": key_sha,
                "object_sha256": source_sha,
            }:
                raise CaptureError("中性保存 rich source pointer 绑定非法")
        elif source_kind == "explicit_fast_intake_turn":
            if source_sha != material["save_intent_receipt_sha256"]:
                raise CaptureError("中性保存 fast source intent 绑定非法")
        else:
            raise CaptureError("中性保存 source transaction kind 非法")
    except CaptureError as exc:
        raise CaptureError(
            "中性保存 source transaction 回执验证失败"
        ) from exc
    except (OSError, RuntimeError, ValueError) as exc:
        raise CaptureError("中性保存 source transaction 回执验证失败") from exc


def _audit_neutral_saves(repo: Path, state: dict[str, Any]) -> int:
    checked = 0
    for save_id, material in state.get("neutral_saves", {}).items():
        event = _review_event_by_id(repo, str(material["review_event_id"]))
        intent_ref = str(material["save_intent_receipt_ref"])
        intent_path = (repo / intent_ref).resolve()
        try:
            intent_path.relative_to(repo)
            info = intent_path.lstat()
        except (ValueError, OSError) as exc:
            raise CaptureError(f"{save_id} 的保存意图回执缺失") from exc
        if stat.S_ISLNK(info.st_mode) or not stat.S_ISREG(info.st_mode):
            raise CaptureError(f"{save_id} 的保存意图回执类型非法")
        try:
            intent_raw = intent_path.read_bytes()
            intent = json.loads(intent_raw)
        except (OSError, UnicodeError, json.JSONDecodeError) as exc:
            raise CaptureError(f"{save_id} 的保存意图回执损坏") from exc
        source_kind = material.get("source_transaction_kind")
        is_morning = source_kind in {
            "prepared_option_turn",
            "rich_morning_turn",
        }
        expected_intent_schema = (
            "managed_408_save_current_result_intent_v1"
            if is_morning
            else "managed_408_fast_intake_intent_v1"
        )
        expected_intent_kind = (
            "save_current_result_intent"
            if is_morning
            else "explicit_fast_intake_intent"
        )
        if (
            not isinstance(intent, dict)
            or hashlib.sha256(intent_raw).hexdigest()
            != material.get("save_intent_receipt_sha256")
            or intent.get("schema") != expected_intent_schema
            or intent.get("kind") != expected_intent_kind
            or intent.get("request_id") != material.get("save_request_id")
            or intent.get("display_receipt_sha256")
            != material.get("display_receipt_sha256")
            or intent.get("authorization_message_sha256")
            != material.get("authorization_message_sha256")
            or intent.get("authorization_message_size_bytes")
            != material.get("authorization_message_size_bytes")
            or intent.get("authorization_binding_sha256")
            != material.get("authorization_binding_sha256")
            or intent.get("authorization_body_stored") is not False
            or intent.get("formal_write_count") != 0
        ):
            raise CaptureError(f"{save_id} 的保存意图证据绑定非法")
        if is_morning and (
            intent.get("source_transaction_kind") != source_kind
            or intent.get("source_transaction_attestation_sha256")
            != material.get("source_transaction_attestation_sha256")
            or intent.get("review_event_id") != material.get("review_event_id")
        ):
            raise CaptureError(f"{save_id} 的晨间保存意图来源绑定非法")
        if (not is_morning) and (
            source_kind != "explicit_fast_intake_turn"
            or material.get("source_transaction_attestation_sha256")
            != material.get("save_intent_receipt_sha256")
            or intent.get("observed_date") != material.get("study_date")
            or event.get("session_id") != intent.get("session_id")
            or event.get("item_id") != intent.get("item_id")
            or event.get("first_result")
            != intent.get("canonical_first_result")
        ):
            raise CaptureError(f"{save_id} 的快速入库保存意图来源绑定非法")
        _verify_neutral_source_transaction(repo, material)
        authorization_material = {
            "schema": "managed_408_explicit_authorization_binding_v1",
            "request_id": material["save_request_id"],
            "display_receipt_sha256": material["display_receipt_sha256"],
            "authorization_message_sha256": material[
                "authorization_message_sha256"
            ],
            "authorization_message_size_bytes": material[
                "authorization_message_size_bytes"
            ],
        }
        if (
            event.get("event_kind") != "outcome"
            or event.get("source")
            != ("morning_review" if is_morning else "daily_practice")
            or event.get("question_valid") is not True
            or event.get("formal_write_authorized") is not False
            or event.get("first_result") not in MORNING_CORRECT_RESULTS
            or event.get("observed_date") != material.get("study_date")
            or _sha256_value(event) != material.get("review_event_sha256")
            or material.get("formalization_authorized") is not False
            or material.get("curation_eligible") is not False
            or material.get("status") != NEUTRAL_SAVE_STATUS
            or hashlib.sha256(
                _canonical(authorization_material) + b"\n"
            ).hexdigest()
            != material.get("authorization_binding_sha256")
        ):
            raise CaptureError(f"{save_id} 的中性保存证据链非法")
        checked += 1
    return checked


def audit(*, repo_root: str | Path = ".") -> dict[str, Any]:
    repo = _resolve_repo(repo_root)
    root = capture_root(repo)
    events = _load_events(root)
    for event in events:
        if event.get("event_type") != "fact_captured":
            continue
        normalized = _validate_capture_payload(event.get("capture"))
        if normalized != event.get("capture"):
            raise CaptureError(
                f"{event.get('capture_id')} 的事实载荷不是规范化答案安全形式"
            )
        if event.get("payload_sha256") != _sha256_value(normalized):
            raise CaptureError(f"{event.get('capture_id')} 的 payload_sha256 不一致")
    expected = replay(events)
    commit_proofs = _canonical_commit_proofs(root, events)
    needs_user_resolution_count = 0
    user_removed_capture_ids: set[str] = set()
    carried_result_count = 0
    formal_backflow_result_count = 0
    for event in events:
        if event.get("event_type") == "needs_user_resolved":
            material = _needs_user_resolution_material(event)
            capture_row = expected["captures"].get(str(material["capture_id"]))
            if not isinstance(capture_row, dict):
                raise CaptureError("needs_user resolution 引用未知 capture")
            verified = _validate_needs_user_resolution_evidence(
                repo,
                capture_row=capture_row,
                prior_batch_id=str(material["prior_batch_id"]),
                formal_id=str(material["formal_id"]),
                confirmation_event_id=str(material["confirmation_event_id"]),
                normal_receipt_sha256=(
                    str(material["normal_receipt_sha256"])
                    if material["normal_receipt_sha256"] is not None
                    else None
                ),
                runtime_root=DEFAULT_RUNTIME_ROOT,
            )
            if verified != material:
                raise CaptureError("needs_user resolution 与外部证据链不一致")
            needs_user_resolution_count += 1
        elif event.get("event_type") == "needs_user_retired":
            material = _needs_user_retirement_material(event)
            if event.get("retirement_sha256") != _sha256_value(material):
                raise CaptureError("needs_user retirement_sha256 非法")
            projection = {
                **material,
                "retirement_sha256": event["retirement_sha256"],
                "retirement_event_id": event.get("event_id"),
            }
            batch = expected["batches"].get(
                str(material["prior_batch_id"])
            )
            if (
                not isinstance(batch, dict)
                or batch.get("needs_user_retirement") != projection
            ):
                raise CaptureError(
                    "needs_user retirement 的批次投影发生漂移"
                )
            for capture_id in material["capture_ids"]:
                if capture_id in user_removed_capture_ids:
                    raise CaptureError(
                        "同一 capture 存在重复 needs_user retirement"
                    )
                capture_row = expected["captures"].get(capture_id)
                if (
                    not isinstance(capture_row, dict)
                    or capture_row.get("quality_status") != "user_removed"
                    or capture_row.get("retirement") != projection
                ):
                    raise CaptureError(
                        "needs_user retirement 的 capture 投影发生漂移"
                    )
                user_removed_capture_ids.add(capture_id)
        elif (
            event.get("event_type") == "curation_item_result"
            and event.get("carried_from_batch_id") is not None
        ):
            carried_result_count += 1
            if event.get("outcome") == "concept_only":
                _validate_concept_only_receipt(
                    repo,
                    capture_id=str(event.get("capture_id") or ""),
                    concept_source_id=str(event.get("concept_source_id") or ""),
                    concept_receipt_sha256=str(
                        event.get("concept_receipt_sha256") or ""
                    ),
                )
        elif (
            event.get("event_type") == "curation_item_result"
            and event.get("outcome") == "concept_only"
        ):
            _validate_concept_only_receipt(
                repo,
                capture_id=str(event.get("capture_id") or ""),
                concept_source_id=str(event.get("concept_source_id") or ""),
                concept_receipt_sha256=str(
                    event.get("concept_receipt_sha256") or ""
                ),
            )
        elif (
            event.get("event_type") == "curation_item_result"
            and any(
                field in event for field in FORMAL_BACKFLOW_RESULT_FIELDS
            )
        ):
            outcome = str(event.get("outcome") or "")
            verified_fields = _validate_formal_backflow_receipts(
                repo=repo,
                batch_id=str(event.get("batch_id") or ""),
                capture_id=str(event.get("capture_id") or ""),
                outcome=outcome,
                formal_id=event.get("formal_id"),
                formal_result_receipt_sha256=(
                    event.get("receipt_sha256")
                    if outcome == "curated"
                    else event.get("verification_sha256")
                    if outcome == "already_current"
                    else None
                ),
                ensure_receipt_sha256=str(
                    event.get("review_schedule_ensure_receipt_sha256")
                    or ""
                ),
                binding_receipt_sha256=event.get(
                    "formal_review_binding_receipt_sha256"
                ),
            )
            if any(
                event.get(field) != verified_fields.get(field)
                for field in FORMAL_BACKFLOW_RESULT_FIELDS
            ):
                raise CaptureError("curation result 的正式回流字段发生漂移")
            formal_backflow_result_count += 1
    aggregate_receipt_count = 0
    legacy_unsealed_close_count = 0
    for event in events:
        if event.get("event_type") != "curation_batch_closed":
            continue
        if event.get("close_contract") is None:
            legacy_unsealed_close_count += 1
            continue
        if event.get("global_closeout") != "pass":
            continue
        batch = expected["batches"].get(str(event.get("batch_id") or ""))
        if batch is None:
            raise CaptureError("aggregate audit receipt 引用未知已关闭批次")
        _validate_global_audit_receipt(
            repo,
            root,
            batch,
            event.get("global_audit_receipt_sha256"),
            require_current_objects=False,
        )
        aggregate_receipt_count += 1
    state_path = root / "state.json"
    if not state_path.is_file():
        raise CaptureError("state.json 缺失；运行 reconcile")
    try:
        actual = json.loads(state_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise CaptureError(f"state.json 损坏：{exc}") from exc
    if actual != expected:
        raise CaptureError("state.json 与事件账本重放结果不一致；运行 reconcile")
    review_ref_count = _audit_review_evidence_refs(repo, expected)
    neutral_save_count = _audit_neutral_saves(repo, expected)
    ordinary_ref_count = sum(
        1
        for row in expected["captures"].values()
        if any(
            ref.get("kind") == ORDINARY_REVIEW_REF_KIND
            for ref in row["capture"].get("stable_evidence_refs") or []
        )
    )
    projected_user_removed = {
        capture_id
        for capture_id, row in expected["captures"].items()
        if row.get("quality_status") == "user_removed"
    }
    if projected_user_removed != user_removed_capture_ids:
        raise CaptureError("user_removed capture 缺少唯一 retirement 事件")
    return {
        "status": "PASS",
        "event_count": len(events),
        "capture_count": len(expected["captures"]),
        "neutral_save_count": neutral_save_count,
        "batch_count": len(expected["batches"]),
        "review_evidence_ref_count": review_ref_count,
        "ordinary_review_evidence_ref_count": ordinary_ref_count,
        "needs_user_resolution_count": needs_user_resolution_count,
        "user_removed_capture_count": len(user_removed_capture_ids),
        "carried_result_count": carried_result_count,
        "formal_backflow_result_count": formal_backflow_result_count,
        "aggregate_global_audit_receipt_count": aggregate_receipt_count,
        "legacy_unsealed_close_count": legacy_unsealed_close_count,
        "canonical_commit_verified_count": sum(
            proof.get("status") == "verified"
            for proof in commit_proofs.values()
        ),
        "canonical_commit_unverified_capture_ids": sorted(
            capture_id
            for capture_id, proof in commit_proofs.items()
            if proof.get("status") != "verified"
        ),
        "formal_write_count": 0,
    }


def _print(value: dict[str, Any]) -> None:
    print(json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True))


def main() -> int:
    parser = argparse.ArgumentParser(description="408 学习中事实捕获与日终编纂清单账本")
    parser.add_argument("--repo", default=".")
    sub = parser.add_subparsers(dest="command", required=True)

    recover_legacy_parser = sub.add_parser(
        "recover-existing-legacy-authorization"
    )
    recover_legacy_parser.add_argument("--capture-id", required=True)
    recover_legacy_parser.add_argument(
        "--authorization-event-id", required=True
    )
    recover_legacy_parser.add_argument(
        "--authorization-event-sha256", required=True
    )
    recover_legacy_parser.add_argument(
        "--fact-receipt-sha256", required=True
    )
    recover_legacy_parser.add_argument("--capture-hot-root")

    resolve_parser = sub.add_parser("resolve-needs-user")
    resolve_parser.add_argument("--date", required=True)
    resolve_parser.add_argument("--capture-id", required=True)
    resolve_parser.add_argument("--formal-id", required=True)
    resolve_parser.add_argument("--confirmation-event-id", required=True)
    resolve_parser.add_argument("--normal-receipt-sha256")
    resolve_parser.add_argument("--idempotency-key", required=True)

    retire_parser = sub.add_parser("retire-needs-user")
    retire_parser.add_argument("--date", required=True)
    retire_parser.add_argument(
        "--capture-id", action="append", required=True
    )
    retire_parser.add_argument(
        "--reason",
        choices=(NEEDS_USER_RETIREMENT_REASON,),
        required=True,
    )
    retire_parser.add_argument(
        "--user-confirmed", action="store_true", required=True
    )
    retire_parser.add_argument("--idempotency-key", required=True)

    start_parser = sub.add_parser("start-batch")
    start_parser.add_argument("--date", required=True)
    start_parser.add_argument("--idempotency-key", required=True)
    start_parser.add_argument("--capture-id", action="append")

    result_parser = sub.add_parser("mark-result")
    result_parser.add_argument("--batch-id", required=True)
    result_parser.add_argument("--capture-id", required=True)
    result_parser.add_argument("--outcome", choices=sorted(RESULT_OUTCOMES), required=True)
    result_parser.add_argument("--idempotency-key", required=True)
    result_parser.add_argument("--formal-id")
    result_parser.add_argument("--receipt-sha256")
    result_parser.add_argument("--verification-sha256")
    result_parser.add_argument("--concept-source-id")
    result_parser.add_argument("--concept-receipt-sha256")
    result_parser.add_argument("--reason")
    result_parser.add_argument("--review-schedule-ensure-receipt-sha256")
    result_parser.add_argument("--formal-review-binding-receipt-sha256")

    close_parser = sub.add_parser("close-batch")
    close_parser.add_argument("--batch-id", required=True)
    close_parser.add_argument("--global-closeout", choices=("pass", "failed"), required=True)
    close_parser.add_argument("--idempotency-key", required=True)
    close_parser.add_argument("--global-audit-receipt-sha256")

    seal_audit_parser = sub.add_parser("seal-global-audit")
    seal_audit_parser.add_argument("--batch-id", required=True)

    status_parser = sub.add_parser("status")
    status_parser.add_argument("--date")
    backlog_parser = sub.add_parser("backlog-plan")
    backlog_parser.add_argument("--cutoff-date")
    backlog_parser.add_argument("--only-today", action="store_true")
    backlog_parser.add_argument("--capture-id", action="append")
    backlog_parser.add_argument("--package-locator", action="append")
    backlog_parser.add_argument("--runtime-root")
    backlog_parser.add_argument("--package-root")
    backlog_parser.add_argument("--legacy-root")
    backlog_parser.add_argument("--decision-root")
    sub.add_parser("reconcile")
    sub.add_parser("audit")
    args = parser.parse_args()

    try:
        if args.command == "recover-existing-legacy-authorization":
            value = recover_existing_legacy_authorization(
                capture_id=args.capture_id,
                authorization_event_id=args.authorization_event_id,
                authorization_event_sha256=(
                    args.authorization_event_sha256
                ),
                fact_receipt_sha256=args.fact_receipt_sha256,
                repo_root=args.repo,
                hot_root=args.capture_hot_root,
            )
        elif args.command == "resolve-needs-user":
            value = resolve_needs_user(
                study_date=args.date,
                capture_id=args.capture_id,
                formal_id=args.formal_id,
                confirmation_event_id=args.confirmation_event_id,
                normal_receipt_sha256=args.normal_receipt_sha256,
                idempotency_key=args.idempotency_key,
                repo_root=args.repo,
            )
        elif args.command == "retire-needs-user":
            value = retire_needs_user(
                study_date=args.date,
                capture_ids=args.capture_id,
                reason=args.reason,
                user_confirmed=args.user_confirmed,
                idempotency_key=args.idempotency_key,
                repo_root=args.repo,
            )
        elif args.command == "start-batch":
            value = start_batch(
                args.date,
                args.idempotency_key,
                capture_ids=(
                    sorted(args.capture_id) if args.capture_id else None
                ),
                repo_root=args.repo,
            )
        elif args.command == "mark-result":
            value = mark_result(
                batch_id=args.batch_id,
                capture_id=args.capture_id,
                outcome=args.outcome,
                idempotency_key=args.idempotency_key,
                formal_id=args.formal_id,
                receipt_sha256=args.receipt_sha256,
                verification_sha256=args.verification_sha256,
                concept_source_id=args.concept_source_id,
                concept_receipt_sha256=args.concept_receipt_sha256,
                reason=args.reason,
                review_schedule_ensure_receipt_sha256=(
                    args.review_schedule_ensure_receipt_sha256
                ),
                formal_review_binding_receipt_sha256=(
                    args.formal_review_binding_receipt_sha256
                ),
                repo_root=args.repo,
            )
        elif args.command == "close-batch":
            value = close_batch(
                batch_id=args.batch_id,
                global_closeout=args.global_closeout,
                idempotency_key=args.idempotency_key,
                global_audit_receipt_sha256=args.global_audit_receipt_sha256,
                repo_root=args.repo,
            )
        elif args.command == "seal-global-audit":
            value = seal_global_audit(
                batch_id=args.batch_id,
                repo_root=args.repo,
            )
        elif args.command == "status":
            value = status(args.date, repo_root=args.repo)
        elif args.command == "backlog-plan":
            options: dict[str, Any] = {
                "cutoff_date": args.cutoff_date,
                "only_today": args.only_today,
                "capture_ids": args.capture_id,
                "package_locators": args.package_locator,
                "repo_root": args.repo,
            }
            if args.runtime_root is not None:
                options["runtime_root"] = args.runtime_root
            if args.package_root is not None:
                options["package_root"] = args.package_root
            if args.legacy_root is not None:
                options["legacy_root"] = args.legacy_root
            if args.decision_root is not None:
                options["decision_root"] = args.decision_root
            value = backlog_plan(**options)
        elif args.command == "reconcile":
            value = reconcile(repo_root=args.repo)
        else:
            value = audit(repo_root=args.repo)
    except (CaptureError, OSError) as exc:
        _print({"status": "FAILED", "error": str(exc)})
        return 1
    _print(value)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
