#!/usr/bin/env python3
"""408 历史异步入库队列恢复工具（不写正式错题库）。

2026-07-12 起，408 日常入库恢复为单题同步流程。本脚本不再接收普通新题；
``new-id`` 与 ``submit`` 默认拒绝执行。完整的队列状态机和持久数据结构继续保留，
仅用于既有 JOB/WAL/receipt 的只读核对、恢复、收尾，以及显式隔离测试。

该脚本只负责 question_submission_v1 的持久化、领取、状态迁移、恢复和
coordinator 登记。题图、答案和完整解析仍由调用方预先放到 details inbox；
队列只校验文件指针与 SHA-256，绝不复制或移动证据文件。

历史恢复常用命令：

  python3 scripts/intake_queue_408.py revise JOB-... package-v2.json --ready \
    --expected-state NEEDS_USER --expected-state-version 3 --expected-generation 1 --json
  python3 scripts/intake_queue_408.py resolve-user JOB-... --metadata-json '{"user_resolution":"..."}' \
    --expected-state-version 9 --expected-generation 1 --json
  python3 scripts/intake_queue_408.py claim --worker coordinator-1 --limit 3 --expected-generation 1 --json
  python3 scripts/intake_queue_408.py transition JOB-... REVIEWED --actor coordinator-1 --claim-token ... --expected-generation 1
  python3 scripts/intake_queue_408.py recover --stale-seconds 900 --expected-generation 1 --json
  python3 scripts/intake_queue_408.py coordinator register --thread-id ... --json

隔离测试若确实需要覆盖退役提交契约，必须显式传
``--allow-retired-async-submit``；日常 408 入库禁止使用该开关。

默认队列根：~/.codex/kaoyan-408-intake
默认证据根：/Users/your-user/Documents/kaoyan-408-details
"""

from __future__ import annotations

import argparse
import contextlib
import datetime as dt
import errno
import fcntl
import hashlib
import json
import os
import re
import socket
import sys
import tempfile
import time
import uuid
from collections import Counter
from pathlib import Path
from typing import Any, Iterable, Iterator

from intake_lock_408 import RepoLock


SCHEMA_VERSION = "question_submission_v1"
COORDINATOR_SCHEMA_VERSION = "coordinator_v1"
DEFAULT_QUEUE_ROOT = Path("~/.codex/kaoyan-408-intake").expanduser()
DEFAULT_DETAILS_ROOT = Path("/Users/your-user/Documents/kaoyan-408-details")
DEFAULT_FORMAL_REPO_ROOT = Path(__file__).resolve().parents[1]

LINEAR_STATES = (
    "DRAFT",
    "READY",
    "REVIEWING",
    "REVIEWED",
    "COMMITTING",
    "COMMITTED",
    "VALIDATING",
    "DONE",
)
SIDE_STATES = ("NEEDS_USER", "RETRYABLE", "FAILED", "CANCELLED")
STATES = frozenset(LINEAR_STATES + SIDE_STATES)
TERMINAL_STATES = frozenset({"DONE", "FAILED", "CANCELLED"})

ALLOWED_TRANSITIONS: dict[str, frozenset[str]] = {
    "DRAFT": frozenset({"READY", "CANCELLED"}),
    "READY": frozenset({"REVIEWING", "NEEDS_USER", "FAILED", "CANCELLED"}),
    "REVIEWING": frozenset({"REVIEWED", "NEEDS_USER", "RETRYABLE", "FAILED", "CANCELLED"}),
    "REVIEWED": frozenset({"COMMITTING", "NEEDS_USER", "RETRYABLE", "FAILED", "CANCELLED"}),
    "COMMITTING": frozenset({"COMMITTED", "RETRYABLE", "FAILED"}),
    "COMMITTED": frozenset({"VALIDATING", "RETRYABLE", "FAILED"}),
    "VALIDATING": frozenset({"DONE", "NEEDS_USER", "RETRYABLE", "FAILED"}),
    "NEEDS_USER": frozenset({"READY", "FAILED", "CANCELLED"}),
    # RETRYABLE 的精确恢复目标还会受 retry_from_state 约束。
    "RETRYABLE": frozenset({
        "READY", "REVIEWING", "REVIEWED", "COMMITTING", "COMMITTED",
        "VALIDATING", "FAILED", "CANCELLED",
    }),
    "FAILED": frozenset({"CANCELLED"}),
    "DONE": frozenset(),
    "CANCELLED": frozenset(),
}

MODE_HINTS = frozenset({"new", "redo", "unknown"})
SUBJECT_HINTS = frozenset({"DS", "CO", "OS", "CN", "unknown", "未标明"})
JOB_ID_RE = re.compile(r"^JOB-[A-Za-z0-9][A-Za-z0-9._-]{2,95}$")
SHA256_RE = re.compile(r"^[0-9a-fA-F]{64}$")
IDEMPOTENCY_KEY_RE = re.compile(r"^sha256:[0-9a-f]{64}$")
FORMAL_ID_RE = re.compile(r"^(DS|CO|OS|CN)_(?:\d{4}|UNK)_\d{3}$")

# 只扫描 submission 的 safe_context。这里刻意不把“答案”二字本身当作泄露，
# 以免“不要背答案，要看机制”这类安全提醒被误杀；但答案标签带值、选项字母
# 结论和用户旧选择都必须留在 details inbox。
SAFE_CONTEXT_ANSWER_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    (
        "answer_label_with_value",
        re.compile(
            r"(?:正确答案|标准答案|参考答案|错误答案|错选答案|我的答案|用户答案|"
            r"用户旧答案|正确选项|错误选项)\s*(?:[:：为是]|\s)\s*"
            r"(?!已隐藏|未记录|未知|待补充|不提供)[^，。；;\n]{1,80}",
            re.IGNORECASE,
        ),
    ),
    (
        "answer_is",
        re.compile(
            r"(?:答案|应选)\s*(?:是|为|[:：])\s*"
            r"(?!已隐藏|未记录|未知|待补充|不提供)[A-H0-9][^，。；;\n]{0,32}",
            re.IGNORECASE,
        ),
    ),
    (
        "old_choice_letter",
        re.compile(
            r"(?:(?:我|用户)\s*(?:之前|此前|上次)?|此前|上次)?\s*"
            r"(?:选了?|选择了?|错选|误选)\s*"
            r"[A-H](?:\s*[,，、/+]\s*[A-H])*\b",
            re.IGNORECASE,
        ),
    ),
    (
        "option_letter_judgement",
        re.compile(r"\b[A-H]\s*(?:项|选项)?\s*(?:正确|错误|是答案|为答案)", re.IGNORECASE),
    ),
    (
        "direct_answer_or_conclusion",
        re.compile(
            r"(?:答案\s*(?:就是|应为)|结论\s*为)\s*"
            r"(?!如何|怎样|怎么|何以|为什么|是否|能否|可否)"
            r"[^，。；;？?\n]{1,80}",
            re.IGNORECASE,
        ),
    ),
    (
        "direct_assertion_phrase",
        re.compile(
            r"(?:就是|应为|结论为)\s*"
            r"(?!如何|怎样|怎么|何以|为什么|是否|能否|可否|待确认|待补充|未记录|已隐藏)"
            r"[^，。；;？?\n]{1,80}",
            re.IGNORECASE,
        ),
    ),
)

QUEUE_SAFE_TEXT_SECTIONS = (
    "safe_context",
    "unreviewed_candidates",
    "relation_review",
    "missing_fields",
    "redaction",
    "worker_self_check",
)

REQUIRED_REDACTION_TRUE_FIELDS = (
    "protected_content_removed_from_queue",
    "full_stem_in_inbox_only",
    "answers_in_inbox_only",
)
REQUIRED_WORKER_FALSE_FIELDS = (
    "formal_id_assigned",
    "formal_write_attempted",
    "apply_invoked",
)

SAFE_CONTEXT_FIELDS = (
    "question_skeleton",
    "key_parameters",
    "ask_type",
    "user_error_entry",
    "correct_mechanism",
    "redo_first_action",
)
CANDIDATE_GROUPS = ("knowledge_points", "error_tags", "relations")
REQUIRED_SUBMISSION_FIELDS = (
    "idempotency_key",
    "mode_hint",
    "subject_hint",
    "year_hint",
    "evidence",
    "safe_context",
    "unreviewed_candidates",
    "relation_review",
    "missing_fields",
    "redaction",
    "worker_self_check",
)

SUBMISSION_TOP_LEVEL_KEYS = frozenset({
    "schema_version",
    "contract",
    "job_id",
    "idempotency_key",
    "mode_hint",
    "subject_hint",
    "year_hint",
    "existing_formal_id",
    "evidence",
    "safe_context",
    "unreviewed_candidates",
    "relation_review",
    "missing_fields",
    "redaction",
    "worker_self_check",
})

# 这些字段一旦出现在队列 payload，通常意味着调用方把 raw evidence 直接塞进
# 了队列。大小写和中英文都检查；安全字段 correct_mechanism 不在此列。
FORBIDDEN_RAW_KEYS = frozenset({
    "full_question",
    "question_text",
    "raw_question",
    "correct_answer",
    "wrong_answer",
    "standard_answer",
    "full_explanation",
    "raw_explanation",
    "answer",
    "explanation",
    "options",
    "user_response",
    "handwriting",
    "image_base64",
    "题干",
    "答案",
    "解析",
    "选项",
    "完整题干",
    "完整解析",
    "正确答案",
    "标准答案",
    "错选答案",
    "用户答案",
})

PROTECTED_JOB_FIELDS = frozenset({
    "state",
    "state_version",
    "created_at",
    "updated_at",
    "state_updated_at",
    "submitted_at",
    "submission_sha256",
    "request_sha256",
    "claim",
    "retry_from_state",
    "queue_metadata",
    "commit_lease",
    "review_fence",
})

ALLOWED_METADATA_KEYS = frozenset({
    "wake_status",
    "wake_error",
    "approved_package_path",
    "approved_package_sha256",
    # 保留 skill 文档早期使用过的短别名；两者都只可由 coordinator 写入。
    "approved_path",
    "approved_sha256",
    "commit_batch_id",
    "batch_receipt_path",
    "batch_receipt_sha256",
    "batch_recovery_receipt_path",
    "batch_recovery_receipt_sha256",
    "formal_id",
    "formal_ids",
    "validation_summary",
    "notes",
    "user_resolution",
    "user_resolution_at",
    "wake_target_thread_id",
    "wake_target_generation",
    "wake_sent_at",
})

POSTCOMMIT_STATES = frozenset({"COMMITTING", "COMMITTED", "VALIDATING", "DONE"})
USER_RESOLUTION_METADATA_KEYS = frozenset({"validation_summary", "notes", "user_resolution"})

# These values form the immutable identity of a reviewed/committed item.  A
# COMMITTING job may bind its recovery receipt once (the documented WAL
# recovery flow uses a same-state annotation), but an annotation must never
# replace an identity value that is already present.
POSTCOMMIT_IDENTITY_METADATA_KEYS = frozenset({
    "approved_package_path",
    "approved_package_sha256",
    "approved_path",
    "approved_sha256",
    "commit_batch_id",
    "batch_receipt_path",
    "batch_receipt_sha256",
    "batch_recovery_receipt_path",
    "batch_recovery_receipt_sha256",
    "formal_id",
    "formal_ids",
})
POSTCOMMIT_LATE_BINDING_METADATA_KEYS = frozenset({
    "batch_recovery_receipt_path",
    "batch_recovery_receipt_sha256",
})
POSTCOMMIT_NORMAL_BINDING_METADATA_KEYS = frozenset({
    "batch_receipt_path",
    "batch_receipt_sha256",
    "formal_id",
})

MAX_REVIEWING_HARD_LIMIT = 3
RECEIPTS_DIRNAME = "receipts"


class QueueError(RuntimeError):
    """可向 CLI 用户直接展示的队列错误。"""


class ValidationError(QueueError):
    pass


class QueueBusyError(QueueError):
    pass


class JobNotFoundError(QueueError):
    pass


class InvalidTransitionError(QueueError):
    pass


class IdempotencyConflictError(QueueError):
    pass


def utc_now() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat(timespec="microseconds").replace("+00:00", "Z")


def parse_utc(value: str) -> dt.datetime:
    try:
        parsed = dt.datetime.fromisoformat(value.replace("Z", "+00:00"))
    except (TypeError, ValueError) as exc:
        raise ValidationError(f"无效 UTC 时间：{value!r}") from exc
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=dt.timezone.utc)
    return parsed.astimezone(dt.timezone.utc)


def new_job_id() -> str:
    stamp = dt.datetime.now(dt.timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
    return f"JOB-{stamp}-{uuid.uuid4().hex[:12]}"


def canonical_json_bytes(value: Any) -> bytes:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _fsync_dir(path: Path) -> None:
    flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
    descriptor = os.open(str(path), flags)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def atomic_write_json(path: Path, value: dict[str, Any]) -> None:
    """同目录临时文件 + fsync + replace + 目录 fsync。"""
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    descriptor, temp_name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    temp_path = Path(temp_name)
    try:
        os.fchmod(descriptor, 0o600)
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            json.dump(value, handle, ensure_ascii=False, indent=2, sort_keys=True)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp_path, path)
        _fsync_dir(path.parent)
    except Exception:
        with contextlib.suppress(FileNotFoundError):
            temp_path.unlink()
        raise


def read_json(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise JobNotFoundError(f"文件不存在：{path}") from exc
    except json.JSONDecodeError as exc:
        raise QueueError(f"JSON 损坏：{path}: {exc}") from exc
    if not isinstance(value, dict):
        raise QueueError(f"JSON 顶层必须为 object：{path}")
    return value


def _iter_keys(value: Any) -> Iterator[str]:
    if isinstance(value, dict):
        for key, nested in value.items():
            yield str(key)
            yield from _iter_keys(nested)
    elif isinstance(value, list):
        for nested in value:
            yield from _iter_keys(nested)


def _iter_string_values(value: Any, path: str = "safe_context") -> Iterator[tuple[str, str]]:
    if isinstance(value, dict):
        for key, nested in value.items():
            yield from _iter_string_values(nested, f"{path}.{key}")
    elif isinstance(value, list):
        for index, nested in enumerate(value):
            yield from _iter_string_values(nested, f"{path}[{index}]")
    elif isinstance(value, str):
        yield path, value


def _validate_text_tree_no_answer_leak(value: Any, *, root: str) -> None:
    for path, text in _iter_string_values(value, root):
        for rule_name, pattern in SAFE_CONTEXT_ANSWER_PATTERNS:
            match = pattern.search(text)
            if match:
                excerpt = match.group(0).strip()
                if len(excerpt) > 48:
                    excerpt = excerpt[:45] + "..."
                raise ValidationError(
                    f"队列安全文本检出答案泄露模式 {rule_name}：{path}={excerpt!r}；"
                    "答案、旧选择和选项结论只能留在 details inbox"
                )


def _validate_safe_context_no_answer_leak(safe_context: Any) -> None:
    if not isinstance(safe_context, dict):
        raise ValidationError("safe_context 必须为 object")
    _validate_text_tree_no_answer_leak(safe_context, root="safe_context")


def _validate_submission_safe_text_no_answer_leak(submission: dict[str, Any]) -> None:
    """扫描所有会落进队列的安全文本，而不触碰 evidence 文件内容/路径。"""
    for section in QUEUE_SAFE_TEXT_SECTIONS:
        if section in submission:
            _validate_text_tree_no_answer_leak(submission[section], root=section)


def _nonempty_string(value: Any, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValidationError(f"{field} 必须是非空字符串")
    return value.strip()


def _normalize_candidate(candidate: Any, field: str, index: int) -> dict[str, Any]:
    if not isinstance(candidate, dict):
        raise ValidationError(f"{field}[{index}] 必须是 object，并包含 value 与 basis")
    normalized = dict(candidate)
    value = normalized.get("value", normalized.get("candidate"))
    normalized["value"] = _nonempty_string(value, f"{field}[{index}].value")
    normalized["basis"] = _nonempty_string(normalized.get("basis"), f"{field}[{index}].basis")
    review_status = normalized.get("review_status", "unreviewed")
    if review_status not in ("unreviewed", "未审核"):
        raise ValidationError(f"{field}[{index}].review_status 必须为 unreviewed/未审核")
    normalized["review_status"] = "unreviewed"
    return normalized


def submission_receipt(job: dict[str, Any], *, duplicate: bool) -> str:
    """返回不会把历史幂等命中误报成新入队的用户回执。"""
    state = str(job.get("state") or "UNKNOWN")
    if not duplicate:
        return "QUEUED" if state == "READY" else "DRAFT_SAVED"
    if state == "READY":
        return "ALREADY_QUEUED"
    if state == "DRAFT":
        return "DRAFT_ALREADY_SAVED"
    return f"ALREADY_{state}"


class QueueStore:
    """单锁、文件持久化的 question_submission_v1 队列。"""

    def __init__(
        self,
        root: str | Path | None = None,
        details_root: str | Path | None = None,
        *,
        formal_repo_root: str | Path | None = None,
        lock_timeout: float = 30.0,
        max_reviewing: int = 3,
    ) -> None:
        self.root = Path(root or DEFAULT_QUEUE_ROOT).expanduser().resolve()
        self.details_root_raw = Path(details_root or DEFAULT_DETAILS_ROOT).expanduser().absolute()
        self.details_root = self.details_root_raw.resolve()
        self.formal_repo_root = (
            Path(formal_repo_root).expanduser().resolve()
            if formal_repo_root is not None
            else None
        )
        self.details_inbox_raw = self.details_root_raw / "inbox"
        self.details_inbox = self.details_root / "inbox"
        self.jobs_dir = self.root / "jobs"
        self.idempotency_dir = self.root / "idempotency"
        self.receipts_dir = self.root / RECEIPTS_DIRNAME
        self.events_path = self.root / "events.jsonl"
        self.coordinator_path = self.root / "coordinator.json"
        self.lock_path = self.root / ".queue.lock"
        self.lock_timeout = lock_timeout
        self.max_reviewing = max_reviewing
        if isinstance(max_reviewing, bool) or not 1 <= max_reviewing <= MAX_REVIEWING_HARD_LIMIT:
            raise ValidationError(
                f"max_reviewing 必须在 1..{MAX_REVIEWING_HARD_LIMIT}；"
                "全局并行复核硬上限不可由 CLI 放大"
            )
        self._ensure_layout()

    def _ensure_layout(self) -> None:
        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.jobs_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.idempotency_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.receipts_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        for directory in (self.root, self.jobs_dir, self.idempotency_dir, self.receipts_dir):
            with contextlib.suppress(PermissionError):
                directory.chmod(0o700)

    @contextlib.contextmanager
    def _locked(self) -> Iterator[None]:
        self.lock_path.touch(mode=0o600, exist_ok=True)
        descriptor = os.open(str(self.lock_path), os.O_RDWR)
        deadline = time.monotonic() + self.lock_timeout
        try:
            while True:
                try:
                    fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    break
                except OSError as exc:
                    if exc.errno not in (errno.EACCES, errno.EAGAIN):
                        raise
                    if time.monotonic() >= deadline:
                        raise QueueBusyError(f"队列锁等待超过 {self.lock_timeout:g} 秒：{self.lock_path}") from exc
                    time.sleep(0.05)
            yield
        finally:
            with contextlib.suppress(OSError):
                fcntl.flock(descriptor, fcntl.LOCK_UN)
            os.close(descriptor)

    @contextlib.contextmanager
    def _authority_locked(self) -> Iterator[None]:
        """固定按 formal repo lock → queue lock 的顺序修改 coordinator authority。"""
        if self.formal_repo_root is None:
            raise ValidationError("coordinator authority 操作必须显式绑定 formal_repo_root")
        with RepoLock(
            self.formal_repo_root,
            timeout=self.lock_timeout,
            timeout_error=QueueBusyError,
        ):
            with self._locked():
                yield

    def _job_path(self, job_id: str) -> Path:
        self._validate_job_id(job_id)
        return self.jobs_dir / f"{job_id}.json"

    @staticmethod
    def _validate_job_id(job_id: str) -> None:
        if not isinstance(job_id, str) or not JOB_ID_RE.fullmatch(job_id):
            raise ValidationError(f"非法 job_id：{job_id!r}")

    def _idempotency_path(self, key: str) -> Path:
        return self.idempotency_dir / f"{hashlib.sha256(key.encode('utf-8')).hexdigest()}.json"

    def _canonical_job_dir(self, job_id: str, *, must_exist: bool = True) -> tuple[Path, Path]:
        """返回 canonical inbox/job 目录，并拒绝 job 目录本身是符号链接。"""
        self._validate_job_id(job_id)
        try:
            inbox = self.details_inbox.resolve(strict=must_exist)
        except FileNotFoundError as exc:
            raise ValidationError(f"details inbox 不存在：{self.details_inbox}") from exc
        raw_job_dir = self.details_inbox / job_id
        if raw_job_dir.is_symlink():
            raise ValidationError(f"details/inbox/{job_id} 不能是符号链接")
        try:
            resolved_job_dir = raw_job_dir.resolve(strict=must_exist)
        except FileNotFoundError as exc:
            raise ValidationError(f"details/inbox/{job_id} 不存在：{raw_job_dir}") from exc
        try:
            relative = resolved_job_dir.relative_to(inbox)
        except ValueError as exc:
            raise ValidationError(
                f"details/inbox/{job_id} 解析后逃逸 canonical inbox：{resolved_job_dir}"
            ) from exc
        if relative.parts != (job_id,):
            raise ValidationError(
                f"details job 目录必须是 canonical inbox 的直接子目录：{resolved_job_dir}"
            )
        return inbox, resolved_job_dir

    def _append_event_locked(
        self,
        event_type: str,
        *,
        event_id: str | None = None,
        job_id: str | None = None,
        actor: str = "system",
        from_state: str | None = None,
        to_state: str | None = None,
        reason: str | None = None,
        details: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        event: dict[str, Any] = {
            "event_id": event_id or uuid.uuid4().hex,
            "timestamp": utc_now(),
            "event_type": event_type,
            "actor": actor,
        }
        if job_id is not None:
            event["job_id"] = job_id
        if from_state is not None:
            event["from_state"] = from_state
        if to_state is not None:
            event["to_state"] = to_state
        if reason:
            event["reason"] = reason
        if details:
            event["details"] = details
        payload = canonical_json_bytes(event) + b"\n"
        descriptor = os.open(str(self.events_path), os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
        try:
            view = memoryview(payload)
            while view:
                written = os.write(descriptor, view)
                view = view[written:]
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
        return event

    def _load_job_locked(self, job_id: str) -> dict[str, Any]:
        path = self._job_path(job_id)
        if not path.exists():
            raise JobNotFoundError(f"找不到任务：{job_id}")
        job = read_json(path)
        if job.get("job_id") != job_id:
            raise QueueError(f"任务文件 ID 不一致：{path}")
        return job

    def _all_jobs_locked(self) -> list[dict[str, Any]]:
        jobs = []
        for path in sorted(self.jobs_dir.glob("JOB-*.json")):
            jobs.append(read_json(path))
        return jobs

    def _assert_coordinator_generation_locked(
        self,
        expected_generation: int | None,
        *,
        require_active: bool = True,
    ) -> dict[str, Any]:
        if expected_generation is None:
            raise ValidationError("必须提供 expected_generation 作为 coordinator CAS token")
        if isinstance(expected_generation, bool) or not isinstance(expected_generation, int):
            raise ValidationError("expected_generation 必须是整数")
        if not self.coordinator_path.exists():
            raise InvalidTransitionError("尚未登记 coordinator，不能执行受保护队列操作")
        coordinator = read_json(self.coordinator_path)
        actual = coordinator.get("generation")
        if actual != expected_generation:
            raise InvalidTransitionError(
                f"stale coordinator generation：expected={expected_generation} actual={actual}"
            )
        if require_active and not coordinator.get("active"):
            raise InvalidTransitionError(f"coordinator generation {actual} 已停用")
        return coordinator

    def _validate_ready_gate_locked(self, job: dict[str, Any]) -> None:
        """所有进入 READY 的路径共同使用的证据与 worker 门禁。"""
        job_id = _nonempty_string(job.get("job_id"), "job_id")
        self._validate_job_id(job_id)
        idempotency_key = _nonempty_string(job.get("idempotency_key"), "idempotency_key")
        if not IDEMPOTENCY_KEY_RE.fullmatch(idempotency_key):
            raise ValidationError("进入 READY 的 idempotency_key 必须为 sha256:<64位小写hex>")
        evidence = job.get("evidence")
        if not isinstance(evidence, list) or not evidence:
            raise ValidationError("进入 READY 至少需要一项 details inbox 证据")
        if job.get("missing_fields"):
            raise ValidationError("missing_fields 非空，只能保持 DRAFT/NEEDS_USER，不能进入 READY")
        _validate_safe_context_no_answer_leak(job.get("safe_context"))
        worker_check = job.get("worker_self_check")
        if not isinstance(worker_check, dict) or worker_check.get("passed") is not True:
            raise ValidationError("worker_self_check.passed 必须为 true 才能进入 READY")
        for field in REQUIRED_WORKER_FALSE_FIELDS:
            if worker_check.get(field) is not False:
                raise ValidationError(f"worker_self_check.{field} 必须显式为 false 才能进入 READY")
        # question_submission_v1 参考契约使用 evidence_hashes_verified；同时接受
        # 更短的 hashes_verified 名称，二者任一必须显式为 true。
        if not (
            worker_check.get("hashes_verified") is True
            or worker_check.get("evidence_hashes_verified") is True
        ):
            raise ValidationError(
                "worker_self_check.hashes_verified/evidence_hashes_verified 必须显式为 true"
            )
        redaction = job.get("redaction")
        if not isinstance(redaction, dict) or redaction.get("status") not in {"pass", "safe_fields_only"}:
            raise ValidationError("redaction.status 必须为 pass/safe_fields_only 才能进入 READY")
        for field in REQUIRED_REDACTION_TRUE_FIELDS:
            if redaction.get(field) is not True:
                raise ValidationError(f"redaction.{field} 必须显式为 true 才能进入 READY")

        _inbox, expected_job_dir = self._canonical_job_dir(job_id)
        required_kinds = {
            "detail_card_source": "detail-source.md",
            "evidence_manifest": "evidence-manifest.json",
        }
        seen_required: dict[str, Path] = {}
        submission_manifest_files: list[dict[str, Any]] = []
        for index, item in enumerate(evidence):
            if not isinstance(item, dict):
                raise ValidationError(f"evidence[{index}] 必须为 object")
            kind = _nonempty_string(item.get("kind"), f"evidence[{index}].kind")
            path_value = _nonempty_string(item.get("path"), f"evidence[{index}].path")
            expected_hash = _nonempty_string(item.get("sha256"), f"evidence[{index}].sha256").lower()
            if not SHA256_RE.fullmatch(expected_hash):
                raise ValidationError(f"evidence[{index}].sha256 必须为 64 位十六进制")
            path = Path(path_value).expanduser()
            try:
                resolved = path.resolve(strict=True)
            except FileNotFoundError as exc:
                raise ValidationError(f"evidence[{index}] 文件不存在：{path}") from exc
            if not resolved.is_file():
                raise ValidationError(f"evidence[{index}] 不是普通文件：{resolved}")
            try:
                resolved.relative_to(expected_job_dir)
            except ValueError as exc:
                raise ValidationError(
                    f"evidence[{index}] 必须位于 details/inbox/{job_id}/：{resolved}"
                ) from exc
            if kind in required_kinds:
                expected_basename = required_kinds[kind]
                if resolved.name != expected_basename:
                    raise ValidationError(
                        f"evidence kind={kind} 的 basename 必须为 {expected_basename}：{resolved.name}"
                    )
                if item.get("protected") is not True:
                    raise ValidationError(f"evidence kind={kind} 必须显式 protected=true")
                if kind in seen_required:
                    raise ValidationError(f"evidence kind={kind} 只能出现一次")
                seen_required[kind] = resolved
            if kind != "evidence_manifest":
                submission_manifest_files.append({
                    "kind": kind,
                    "path": str(resolved),
                    "sha256": expected_hash,
                    "protected": item.get("protected"),
                })
            actual_hash = sha256_file(resolved)
            if actual_hash != expected_hash:
                raise ValidationError(
                    f"evidence[{index}] SHA-256 已变化：期望 {expected_hash}，实际 {actual_hash}"
                )
        missing_kinds = sorted(set(required_kinds) - set(seen_required))
        if missing_kinds:
            raise ValidationError(f"进入 READY 缺少必需 evidence kind：{missing_kinds}")
        try:
            manifest = read_json(seen_required["evidence_manifest"])
        except QueueError as exc:
            raise ValidationError(f"evidence-manifest.json 必须是有效 JSON object：{exc}") from exc
        if manifest.get("schema_version") != "evidence_manifest_v1":
            raise ValidationError("evidence-manifest.json schema_version 必须为 evidence_manifest_v1")
        manifest_job_id = manifest.get("job_id")
        if manifest_job_id != job_id:
            raise ValidationError(
                f"evidence-manifest.json job_id 不匹配：{manifest_job_id!r} != {job_id!r}"
            )
        manifest_files = manifest.get("files")
        if not isinstance(manifest_files, list):
            raise ValidationError("evidence-manifest.json files 必须为数组")
        normalized_manifest_files: list[dict[str, Any]] = []
        for index, item in enumerate(manifest_files):
            if not isinstance(item, dict):
                raise ValidationError(f"evidence-manifest.json files[{index}] 必须为 object")
            kind = _nonempty_string(item.get("kind"), f"manifest.files[{index}].kind")
            if kind == "evidence_manifest":
                raise ValidationError("manifest.files 不得包含 evidence-manifest.json 自身")
            path_text = _nonempty_string(item.get("path"), f"manifest.files[{index}].path")
            manifest_path = Path(path_text).expanduser()
            if not manifest_path.is_absolute():
                raise ValidationError(f"manifest.files[{index}].path 必须为绝对路径")
            try:
                resolved_manifest_path = manifest_path.resolve(strict=True)
            except FileNotFoundError as exc:
                raise ValidationError(f"manifest.files[{index}] 文件不存在：{manifest_path}") from exc
            try:
                resolved_manifest_path.relative_to(expected_job_dir)
            except ValueError as exc:
                raise ValidationError(
                    f"manifest.files[{index}] 必须位于 details/inbox/{job_id}/"
                ) from exc
            sha = _nonempty_string(item.get("sha256"), f"manifest.files[{index}].sha256").lower()
            if not SHA256_RE.fullmatch(sha):
                raise ValidationError(f"manifest.files[{index}].sha256 必须为 64 位十六进制")
            if item.get("protected") is not True:
                raise ValidationError(f"manifest.files[{index}].protected 必须显式为 true")
            if sha256_file(resolved_manifest_path) != sha:
                raise ValidationError(f"manifest.files[{index}] SHA-256 与文件不一致")
            normalized_manifest_files.append({
                "kind": kind,
                "path": str(resolved_manifest_path),
                "sha256": sha,
                "protected": True,
            })
        manifest_set = {
            (item["kind"], item["path"], item["sha256"], item["protected"])
            for item in normalized_manifest_files
        }
        submission_set = {
            (item["kind"], item["path"], item["sha256"], item["protected"])
            for item in submission_manifest_files
        }
        if len(manifest_set) != len(normalized_manifest_files):
            raise ValidationError("evidence-manifest.json files 含重复项")
        if len(submission_set) != len(submission_manifest_files):
            raise ValidationError("submission evidence（除 manifest）含重复项")
        if manifest_set != submission_set:
            missing = sorted(submission_set - manifest_set)
            extra = sorted(manifest_set - submission_set)
            raise ValidationError(
                "evidence-manifest.json files 与 submission evidence 不一致："
                f"missing={missing} extra={extra}"
            )

    def _validate_batch_receipt_locked(
        self,
        job: dict[str, Any],
        *,
        recovery_required: bool,
    ) -> dict[str, Any]:
        if recovery_required:
            path_value = job.get("batch_recovery_receipt_path")
            expected_hash = job.get("batch_recovery_receipt_sha256")
        else:
            path_value = job.get("batch_receipt_path")
            expected_hash = job.get("batch_receipt_sha256")
        path_text = _nonempty_string(path_value, "batch receipt path")
        hash_text = _nonempty_string(expected_hash, "batch receipt sha256").lower()
        if not SHA256_RE.fullmatch(hash_text):
            raise ValidationError("batch receipt sha256 必须为 64 位十六进制")
        path = Path(path_text).expanduser()
        if not path.is_absolute():
            raise ValidationError("batch receipt path 必须为绝对路径")
        if path.is_symlink():
            raise ValidationError("batch receipt 不得是符号链接")
        try:
            resolved = path.resolve(strict=True)
        except FileNotFoundError as exc:
            raise ValidationError(f"batch receipt 不存在：{path}") from exc
        try:
            resolved.relative_to(self.receipts_dir.resolve())
        except ValueError as exc:
            raise ValidationError(f"batch receipt 必须位于 {self.receipts_dir.resolve()} 下") from exc
        if sha256_file(resolved) != hash_text:
            raise ValidationError("batch receipt SHA-256 不匹配")
        receipt = read_json(resolved)
        if receipt.get("schema") != "intake_batch_receipt_v1" or receipt.get("status") != "COMMITTED":
            raise ValidationError("batch receipt schema/status 非法")
        transaction_id = _nonempty_string(receipt.get("transaction_id"), "receipt.transaction_id")
        if not re.fullmatch(r"TXN-[A-Za-z0-9._-]{8,160}", transaction_id):
            raise ValidationError("receipt.transaction_id 非法")
        journal_value = _nonempty_string(receipt.get("journal"), "receipt.journal")
        journal_raw = Path(journal_value).expanduser()
        if not journal_raw.is_absolute() or journal_raw.is_symlink():
            raise ValidationError("receipt.journal 必须是非符号链接绝对路径")
        try:
            journal_path = journal_raw.resolve(strict=True)
        except FileNotFoundError as exc:
            raise ValidationError(f"receipt journal 不存在：{journal_raw}") from exc
        transactions_root = (self.root / "transactions").resolve()
        try:
            journal_path.relative_to(transactions_root)
        except ValueError as exc:
            raise ValidationError(f"receipt journal 必须位于 {transactions_root} 下") from exc
        if journal_path.name != "journal.json" or journal_path.parent.name != transaction_id:
            raise ValidationError("receipt journal 路径与 transaction_id 不一致")
        journal = read_json(journal_path)
        if journal.get("schema") != "intake_batch_wal_v1":
            raise ValidationError("WAL journal schema 非法")
        if journal.get("transaction_id") != transaction_id:
            raise ValidationError("receipt 与 WAL transaction_id 不一致")

        lease = job.get("commit_lease")
        if not isinstance(lease, dict):
            raise ValidationError("COMMITTING 任务缺 commit_lease")
        batch_id = _nonempty_string(job.get("commit_batch_id"), "job.commit_batch_id")
        if lease.get("batch_id") != batch_id:
            raise ValidationError("job commit_batch_id 与 commit lease 不一致")
        if receipt.get("commit_batch_id") != batch_id or journal.get("commit_batch_id") != batch_id:
            raise ValidationError("receipt/WAL commit_batch_id 与 queue job lease 不一致")
        if receipt.get("actor") != lease.get("actor") or journal.get("actor") != lease.get("actor"):
            raise ValidationError("batch receipt/WAL actor 与 commit lease 不一致")
        if (
            receipt.get("coordinator_generation") != lease.get("coordinator_generation")
            or journal.get("coordinator_generation") != lease.get("coordinator_generation")
        ):
            raise ValidationError("batch receipt/WAL coordinator_generation 与 commit lease 不一致")

        repo_text = _nonempty_string(receipt.get("repo_root"), "receipt.repo_root")
        repo_raw = Path(repo_text).expanduser()
        if not repo_raw.is_absolute():
            raise ValidationError("receipt.repo_root 必须为绝对路径")
        try:
            repo_root = repo_raw.resolve(strict=True)
        except FileNotFoundError as exc:
            raise ValidationError(f"receipt.repo_root 不存在：{repo_raw}") from exc
        if not repo_root.is_dir() or journal.get("repo_root") != str(repo_root):
            raise ValidationError("receipt.repo_root 与 canonical WAL repo_root 不一致")
        if self.formal_repo_root is None or repo_root != self.formal_repo_root:
            raise ValidationError("receipt.repo_root 未绑定当前正式 408 仓库")
        expected_repo_key = hashlib.sha256(str(repo_root).encode("utf-8")).hexdigest()[:20]
        if journal_path.parent.parent.name != expected_repo_key:
            raise ValidationError("WAL journal repo namespace 与 repo_root 不一致")
        details_text = _nonempty_string(journal.get("details_root"), "WAL.details_root")
        try:
            journal_details = Path(details_text).expanduser().resolve(strict=True)
        except FileNotFoundError as exc:
            raise ValidationError(f"WAL.details_root 不存在：{details_text}") from exc
        if journal_details != self.details_root.resolve():
            raise ValidationError("WAL.details_root 与当前 queue details root 不一致")
        allowed_roots = journal.get("allowed_roots")
        if not isinstance(allowed_roots, list):
            raise ValidationError("WAL.allowed_roots 必须为数组")
        canonical_allowed = {str(Path(value).expanduser().resolve()) for value in allowed_roots}
        if canonical_allowed != {str(repo_root), str(journal_details)}:
            raise ValidationError("WAL.allowed_roots 必须恰好绑定 formal repo 与 details root")

        payload_sha = _nonempty_string(receipt.get("payload_sha256"), "receipt.payload_sha256").lower()
        if not SHA256_RE.fullmatch(payload_sha) or journal.get("payload_sha256") != payload_sha:
            raise ValidationError("receipt/WAL payload_sha256 非法或不一致")
        payload_identity = journal.get("payload_identity")
        if not isinstance(payload_identity, dict):
            raise ValidationError("WAL 缺 payload_identity")
        actual_payload_sha = hashlib.sha256(canonical_json_bytes(payload_identity)).hexdigest()
        if actual_payload_sha != payload_sha:
            raise ValidationError("WAL payload_identity 无法重现 payload_sha256")

        manifest_identity = receipt.get("manifest")
        if not isinstance(manifest_identity, dict) or manifest_identity != journal.get("manifest"):
            raise ValidationError("receipt/WAL manifest identity 缺失或不一致")
        if manifest_identity.get("contract") != "coordinator_batch_manifest_v1":
            raise ValidationError("receipt manifest contract 非法")
        manifest_path_text = _nonempty_string(manifest_identity.get("path"), "manifest.path")
        manifest_path_raw = Path(manifest_path_text).expanduser()
        if not manifest_path_raw.is_absolute() or manifest_path_raw.is_symlink():
            raise ValidationError("manifest.path 必须是非符号链接绝对路径")
        try:
            manifest_path = manifest_path_raw.resolve(strict=True)
            manifest_path.relative_to((self.root / "manifests").resolve())
        except FileNotFoundError as exc:
            raise ValidationError(f"manifest.path 不存在：{manifest_path_raw}") from exc
        except ValueError as exc:
            raise ValidationError("manifest.path 必须位于当前 runtime manifests 下") from exc
        manifest_sha = _nonempty_string(manifest_identity.get("sha256"), "manifest.sha256").lower()
        if not SHA256_RE.fullmatch(manifest_sha) or sha256_file(manifest_path) != manifest_sha:
            raise ValidationError("manifest SHA-256 不匹配")
        manifest = read_json(manifest_path)
        manifest_schema = manifest.get("schema_version")
        if (
            manifest.get("contract") != "coordinator_batch_manifest_v1"
            # Early immutable manifests used only ``contract``.  The batch
            # writer explicitly accepts a missing schema_version, so receipt
            # reconciliation must apply the same compatibility rule.  A
            # non-empty conflicting schema remains a hard failure; the bound
            # manifest path/SHA, receipt, WAL, lease and payload identity are
            # still verified above and below.
            or manifest_schema not in (None, "", "coordinator_batch_manifest_v1")
            or manifest.get("batch_id") != batch_id
            or manifest.get("actor") != lease.get("actor")
            or manifest.get("coordinator_generation") != lease.get("coordinator_generation")
            or manifest.get("idempotency_key") != journal.get("idempotency_key")
        ):
            raise ValidationError("manifest identity 与 queue lease/WAL 不一致")

        queue_claims = journal.get("queue_claims")
        if (
            not isinstance(queue_claims, list)
            or receipt.get("queue_claims") != queue_claims
            or manifest.get("queue_claims") != queue_claims
            or payload_identity.get("queue_claims") != queue_claims
        ):
            raise ValidationError("receipt/WAL/manifest payload 的 queue_claims 不一致")
        matching_claims = [
            claim for claim in queue_claims
            if isinstance(claim, dict) and claim.get("job_id") == job.get("job_id")
        ]
        if len(matching_claims) != 1:
            raise ValidationError("WAL queue_claims 未唯一绑定当前 job")
        claim = matching_claims[0]
        fence = job.get("review_fence")
        if not isinstance(fence, dict):
            raise ValidationError("queue job 缺 review_fence")
        token = _nonempty_string(claim.get("claim_token"), "queue_claim.claim_token")
        if (
            claim.get("worker_id") != fence.get("worker_id")
            or hashlib.sha256(token.encode("utf-8")).hexdigest() != fence.get("claim_token_sha256")
            or claim.get("coordinator_generation") != fence.get("coordinator_generation")
            or claim.get("claim_event_id") != fence.get("claim_event_id")
            or claim.get("claimed_at") != fence.get("claimed_at")
        ):
            raise ValidationError("WAL queue claim 与持久 review_fence 不一致")

        manifest_packages = manifest.get("packages")
        if not isinstance(manifest_packages, list) or not manifest_packages:
            raise ValidationError("manifest.packages 必须为非空路径数组")
        loaded_packages: list[dict[str, Any]] = []
        loaded_package_paths: list[Path] = []
        approved_root = (self.root / "approved").resolve()
        for index, entry in enumerate(manifest_packages):
            value = entry.get("path") if isinstance(entry, dict) and set(entry) == {"path"} else entry
            if not isinstance(value, str):
                raise ValidationError(f"manifest.packages[{index}] 必须是 package 路径")
            package_raw = Path(value).expanduser()
            if not package_raw.is_absolute() or package_raw.is_symlink():
                raise ValidationError(f"manifest.packages[{index}] 必须是非符号链接绝对路径")
            try:
                package_path = package_raw.resolve(strict=True)
                package_path.relative_to(approved_root)
            except (FileNotFoundError, ValueError) as exc:
                raise ValidationError(f"manifest.packages[{index}] 越过 approved 或不存在") from exc
            loaded_package_paths.append(package_path)
            loaded_packages.append(read_json(package_path))
        expected_payload_identity = {
            "packages": loaded_packages,
            "details_root": str(journal_details),
            "manifest_contract": manifest.get("contract"),
            "batch_id": batch_id,
            "actor": lease.get("actor"),
            "coordinator_generation": lease.get("coordinator_generation"),
            "queue_claims": queue_claims,
        }
        if payload_identity != expected_payload_identity:
            raise ValidationError("WAL payload_identity 与 canonical manifest packages 不一致")

        # The immutable queue metadata must point to the exact approved package
        # that the hash-bound manifest/WAL consumed.  Otherwise a later repair
        # could be redirected to a different approved package even though the
        # original receipt itself still validates.
        approved_path_text = _nonempty_string(
            job.get("approved_package_path"), "job.approved_package_path"
        )
        approved_sha = _nonempty_string(
            job.get("approved_package_sha256"), "job.approved_package_sha256"
        ).lower()
        if not SHA256_RE.fullmatch(approved_sha):
            raise ValidationError("job.approved_package_sha256 必须为 64 位十六进制")
        approved_raw = Path(approved_path_text).expanduser()
        if not approved_raw.is_absolute() or approved_raw.is_symlink():
            raise ValidationError("job.approved_package_path 必须是非符号链接绝对路径")
        try:
            approved_path = approved_raw.resolve(strict=True)
            approved_path.relative_to(approved_root)
        except (FileNotFoundError, ValueError) as exc:
            raise ValidationError("job.approved_package_path 越过 approved 或不存在") from exc
        if sha256_file(approved_path) != approved_sha:
            raise ValidationError("job.approved_package_sha256 与文件不一致")
        legacy_path = job.get("approved_path")
        if legacy_path not in (None, "", approved_path_text):
            raise ValidationError("job approved_path 与 approved_package_path 冲突")
        legacy_sha = job.get("approved_sha256")
        if legacy_sha not in (None, "", approved_sha):
            raise ValidationError("job approved_sha256 与 approved_package_sha256 冲突")
        approved_matches = [
            path
            for path, package in zip(loaded_package_paths, loaded_packages, strict=True)
            if isinstance(package, dict) and package.get("job_id") == job.get("job_id")
        ]
        if approved_matches != [approved_path]:
            raise ValidationError("queue approved metadata 与 manifest package 身份不一致")

        items = receipt.get("items")
        if not isinstance(items, list) or items != journal.get("result_items"):
            raise ValidationError("receipt items 与 WAL result_items 不一致")
        item_job_ids = [
            item.get("job_id")
            for item in items
            if isinstance(item, dict) and isinstance(item.get("job_id"), str)
        ]
        if len(item_job_ids) != len(items):
            raise ValidationError("receipt items 必须逐项包含合法 job_id")
        if len(item_job_ids) != len(set(item_job_ids)):
            raise ValidationError("receipt items 含重复 job_id")
        matching_items = [item for item in items if isinstance(item, dict) and item.get("job_id") == job["job_id"]]
        if len(matching_items) != 1:
            raise ValidationError(f"batch receipt 未包含任务 {job['job_id']}")
        formal_id = _nonempty_string(matching_items[0].get("formal_id"), "receipt item formal_id")
        if not FORMAL_ID_RE.fullmatch(formal_id):
            raise ValidationError("receipt item formal_id 非法")
        if job.get("formal_id") not in (None, "", formal_id):
            raise ValidationError("receipt item formal_id 与 queue job 不一致")

        recovery = receipt.get("recovery")
        action = recovery.get("action") if isinstance(recovery, dict) else None
        if recovery_required:
            if not isinstance(recovery, dict):
                raise ValidationError("COMMITTING 恢复必须提供带 recovery 的 batch receipt")
            if action not in {"roll_forward", "roll_back"}:
                raise ValidationError("batch recovery receipt action 必须为 roll_forward/roll_back")
            _nonempty_string(recovery.get("recovered_at"), "recovery.recovered_at")
        elif action == "roll_back":
            raise ValidationError("normal COMMITTED 不能使用 roll_back receipt")

        expected_state = "rolled_back" if action == "roll_back" else "committed"
        if journal.get("state") != expected_state:
            raise ValidationError(f"WAL state 必须为 {expected_state}")
        files = journal.get("files")
        if not isinstance(files, list):
            raise ValidationError("WAL.files 必须为数组")
        targets: list[str] = []
        for index, item in enumerate(files):
            if not isinstance(item, dict):
                raise ValidationError(f"WAL.files[{index}] 必须为 object")
            target_text = _nonempty_string(item.get("target"), f"WAL.files[{index}].target")
            target = Path(target_text).expanduser()
            if not target.is_absolute():
                raise ValidationError(f"WAL.files[{index}].target 必须为绝对路径")
            canonical_target = target.resolve(strict=False)
            if str(canonical_target) != target_text or not any(
                canonical_target == Path(root) or canonical_target.is_relative_to(Path(root))
                for root in canonical_allowed
            ):
                raise ValidationError(f"WAL.files[{index}].target 非 canonical 或越界")
            before_sha = item.get("before_sha256")
            after_sha = str(item.get("after_sha256") or "").lower()
            if before_sha is not None and not SHA256_RE.fullmatch(str(before_sha)):
                raise ValidationError(f"WAL.files[{index}].before_sha256 非法")
            if not SHA256_RE.fullmatch(after_sha):
                raise ValidationError(f"WAL.files[{index}].after_sha256 非法")
            targets.append(target_text)
            if action == "roll_back":
                if item.get("published") is not False:
                    raise ValidationError("roll_back WAL 文件必须标记 published=false")
                if before_sha is None:
                    if target.exists():
                        raise ValidationError("roll_back 后原本不存在的目标仍存在")
                elif not target.is_file() or sha256_file(target) != before_sha:
                    raise ValidationError("roll_back 后目标 SHA 不等于 before_sha256")
            else:
                if item.get("published") is not True:
                    raise ValidationError("committed WAL 文件必须标记 published=true")
                if not target.is_file() or sha256_file(target) != after_sha:
                    raise ValidationError("normal/roll_forward 后目标 SHA 不等于 after_sha256")
        changed_files = receipt.get("changed_files")
        if not isinstance(changed_files, list):
            raise ValidationError("receipt.changed_files 必须为数组")
        if action == "roll_back":
            if changed_files:
                raise ValidationError("roll_back receipt.changed_files 必须为空")
        elif changed_files != targets:
            raise ValidationError("receipt.changed_files 与 WAL targets 不一致")
        return receipt

    def _validate_and_normalize_submission(self, raw: dict[str, Any]) -> dict[str, Any]:
        if not isinstance(raw, dict):
            raise ValidationError("question_submission_v1 顶层必须为 object")
        unexpected = set(raw) - SUBMISSION_TOP_LEVEL_KEYS
        if unexpected:
            raise ValidationError(f"question_submission_v1 顶层含未声明字段：{sorted(unexpected)}")
        for protected in PROTECTED_JOB_FIELDS:
            if protected in raw:
                raise ValidationError(f"{protected} 由队列维护，提交包不得指定")
        forbidden = sorted({key for key in _iter_keys(raw) if key.lower() in FORBIDDEN_RAW_KEYS or key in FORBIDDEN_RAW_KEYS})
        if forbidden:
            raise ValidationError(f"提交包含 raw/答案字段，必须改成 details 指针：{forbidden}")

        normalized = json.loads(json.dumps(raw, ensure_ascii=False))
        version = normalized.get("schema_version", normalized.get("contract"))
        if version != SCHEMA_VERSION:
            raise ValidationError(f"schema_version 必须为 {SCHEMA_VERSION}")
        if "contract" in normalized and normalized["contract"] != SCHEMA_VERSION:
            raise ValidationError(f"contract 必须为 {SCHEMA_VERSION}")
        normalized["schema_version"] = SCHEMA_VERSION
        normalized["contract"] = SCHEMA_VERSION

        missing = [field for field in REQUIRED_SUBMISSION_FIELDS if field not in normalized]
        if missing:
            raise ValidationError(f"question_submission_v1 缺字段：{missing}")

        normalized["idempotency_key"] = _nonempty_string(normalized["idempotency_key"], "idempotency_key")
        if len(normalized["idempotency_key"]) > 512:
            raise ValidationError("idempotency_key 不能超过 512 字符")
        if normalized["mode_hint"] not in MODE_HINTS:
            raise ValidationError(f"mode_hint 应为 {sorted(MODE_HINTS)}")
        if normalized["subject_hint"] not in SUBJECT_HINTS:
            raise ValidationError(f"subject_hint 应为 {sorted(SUBJECT_HINTS)}")
        year_hint = str(normalized["year_hint"]).strip()
        if not (re.fullmatch(r"\d{4}", year_hint) or year_hint in {"UNK", "unknown", "未标明", "待确认"}):
            raise ValidationError("year_hint 应为四位年份或 UNK/unknown/未标明/待确认")
        normalized["year_hint"] = year_hint
        existing = normalized.get("existing_formal_id")
        if existing not in (None, ""):
            existing = _nonempty_string(existing, "existing_formal_id")
            if not FORMAL_ID_RE.fullmatch(existing):
                raise ValidationError(f"existing_formal_id 格式非法：{existing}")
            normalized["existing_formal_id"] = existing

        supplied_job_id = normalized.get("job_id")
        if supplied_job_id is not None:
            self._validate_job_id(supplied_job_id)

        safe_context = normalized["safe_context"]
        if not isinstance(safe_context, dict):
            raise ValidationError("safe_context 必须为 object")
        missing_safe = [field for field in SAFE_CONTEXT_FIELDS if field not in safe_context]
        if missing_safe:
            raise ValidationError(f"safe_context 缺字段：{missing_safe}")
        for field in SAFE_CONTEXT_FIELDS:
            value = safe_context[field]
            if field == "key_parameters":
                if value is None:
                    raise ValidationError("safe_context.key_parameters 不能为 null")
            else:
                safe_context[field] = _nonempty_string(value, f"safe_context.{field}")

        candidates = normalized["unreviewed_candidates"]
        if not isinstance(candidates, dict):
            raise ValidationError("unreviewed_candidates 必须为 object")
        missing_groups = [group for group in CANDIDATE_GROUPS if group not in candidates]
        if missing_groups:
            raise ValidationError(f"unreviewed_candidates 缺分组：{missing_groups}")
        for group in CANDIDATE_GROUPS:
            if not isinstance(candidates[group], list):
                raise ValidationError(f"unreviewed_candidates.{group} 必须为数组")
            candidates[group] = [
                _normalize_candidate(item, f"unreviewed_candidates.{group}", index)
                for index, item in enumerate(candidates[group])
            ]

        relation_review = normalized["relation_review"]
        if not isinstance(relation_review, dict):
            raise ValidationError("relation_review 必须为 object")
        if relation_review.get("status") not in {"unreviewed", "未审核"}:
            raise ValidationError(
                "question_submission_v1 的 relation_review.status 必须为 unreviewed；"
                "connected/reviewed_no_reliable_edge/needs_user 只能由 coordinator 决定"
            )
        relation_review["status"] = "unreviewed"
        relation_review["note"] = _nonempty_string(relation_review.get("note"), "relation_review.note")

        if not isinstance(normalized["missing_fields"], list) or not all(
            isinstance(item, str) and item.strip() for item in normalized["missing_fields"]
        ):
            raise ValidationError("missing_fields 必须为字符串数组（可为空）")

        redaction = normalized["redaction"]
        if isinstance(redaction, str):
            redaction = {"status": redaction}
            normalized["redaction"] = redaction
        if not isinstance(redaction, dict) or not isinstance(redaction.get("status"), str):
            raise ValidationError("redaction 必须包含字符串 status")

        worker_check = normalized["worker_self_check"]
        if not isinstance(worker_check, dict) or not isinstance(worker_check.get("passed"), bool):
            raise ValidationError("worker_self_check 必须为 object 且包含布尔 passed")

        evidence = normalized["evidence"]
        if not isinstance(evidence, list):
            raise ValidationError("evidence 必须为数组")
        normalized_evidence: list[dict[str, Any]] = []
        inferred_job_ids: set[str] = set()
        raw_inboxes = (
            Path(os.path.abspath(str(self.details_inbox_raw))),
            Path(os.path.abspath(str(self.details_inbox))),
        )
        for index, item in enumerate(evidence):
            if not isinstance(item, dict):
                raise ValidationError(f"evidence[{index}] 必须为 object")
            path_value = _nonempty_string(item.get("path"), f"evidence[{index}].path")
            expected_hash = _nonempty_string(item.get("sha256"), f"evidence[{index}].sha256").lower()
            if not SHA256_RE.fullmatch(expected_hash):
                raise ValidationError(f"evidence[{index}].sha256 必须为 64 位十六进制")
            evidence_path = Path(path_value).expanduser()
            if not evidence_path.is_absolute():
                raise ValidationError(f"evidence[{index}].path 必须为绝对路径")
            raw_evidence_path = Path(os.path.abspath(str(evidence_path)))
            raw_relative = None
            for raw_inbox in raw_inboxes:
                with contextlib.suppress(ValueError):
                    raw_relative = raw_evidence_path.relative_to(raw_inbox)
                    break
            if raw_relative is None:
                raise ValidationError(f"evidence[{index}] 必须位于 {self.details_inbox} 下")
            if len(raw_relative.parts) < 2 or not JOB_ID_RE.fullmatch(raw_relative.parts[0]):
                raise ValidationError(
                    f"evidence[{index}] 必须位于 details/inbox/JOB-.../ 文件夹内：{evidence_path}"
                )
            lexical_job_id = raw_relative.parts[0]
            _inbox_resolved, canonical_job_dir = self._canonical_job_dir(lexical_job_id)
            try:
                resolved = evidence_path.resolve(strict=True)
            except FileNotFoundError as exc:
                raise ValidationError(f"evidence[{index}] 文件不存在：{evidence_path}") from exc
            if not resolved.is_file():
                raise ValidationError(f"evidence[{index}] 不是普通文件：{resolved}")
            try:
                resolved.relative_to(canonical_job_dir)
            except ValueError as exc:
                raise ValidationError(
                    f"evidence[{index}] 解析后逃逸 details/inbox/{lexical_job_id}/：{resolved}"
                ) from exc
            inferred_job_ids.add(lexical_job_id)
            actual_hash = sha256_file(resolved)
            if actual_hash != expected_hash:
                raise ValidationError(
                    f"evidence[{index}] SHA-256 不一致：期望 {expected_hash}，实际 {actual_hash}"
                )
            normalized_item = dict(item)
            normalized_item["path"] = str(resolved)
            normalized_item["sha256"] = actual_hash
            normalized_item["size_bytes"] = resolved.stat().st_size
            normalized_evidence.append(normalized_item)
        normalized["evidence"] = normalized_evidence

        if supplied_job_id is None and len(inferred_job_ids) == 1:
            normalized["job_id"] = next(iter(inferred_job_ids))
        elif supplied_job_id is not None and inferred_job_ids and inferred_job_ids != {supplied_job_id}:
            raise ValidationError(
                f"evidence 必须位于 details/inbox/{supplied_job_id}/；实际任务目录 {sorted(inferred_job_ids)}"
            )
        elif len(inferred_job_ids) > 1:
            raise ValidationError(f"同一任务的 evidence 不能跨多个 JOB 目录：{sorted(inferred_job_ids)}")
        _validate_submission_safe_text_no_answer_leak(normalized)
        return normalized

    def submit(self, raw: dict[str, Any], *, draft: bool = False, actor: str = "question-worker") -> tuple[dict[str, Any], bool]:
        if not isinstance(raw, dict):
            raise ValidationError("question_submission_v1 顶层必须为 object")
        raw_key = raw.get("idempotency_key")
        request_payload = json.loads(json.dumps(raw, ensure_ascii=False))
        request_payload.pop("job_id", None)
        request_digest = hashlib.sha256(canonical_json_bytes(request_payload)).hexdigest()
        # 历史 alias 必须在证据重校验前命中：revise 可能已经原子更新同一 job
        # inbox 的文件，完全相同的旧请求仍应返回当前 job，而不是因旧 SHA 失败。
        if isinstance(raw_key, str) and raw_key.strip():
            early_index_path = self._idempotency_path(raw_key.strip())
            with self._locked():
                if early_index_path.exists():
                    early_index = read_json(early_index_path)
                    indexed_request = early_index.get("request_sha256")
                    if indexed_request is not None:
                        existing_job = self._load_job_locked(early_index.get("job_id", ""))
                        if indexed_request != request_digest:
                            raise IdempotencyConflictError(
                                f"idempotency_key 已用于不同提交：{raw_key.strip()} -> "
                                f"{existing_job['job_id']}"
                            )
                        return existing_job, True

        normalized = self._validate_and_normalize_submission(raw)
        requested_job_id = normalized.get("job_id")
        payload_for_digest = dict(normalized)
        payload_for_digest.pop("job_id", None)
        submission_digest = hashlib.sha256(canonical_json_bytes(payload_for_digest)).hexdigest()
        key = normalized["idempotency_key"]
        index_path = self._idempotency_path(key)

        with self._locked():
            if index_path.exists():
                index = read_json(index_path)
                existing_job = self._load_job_locked(index.get("job_id", ""))
                indexed_request = index.get("request_sha256")
                if indexed_request is not None and indexed_request != request_digest:
                    raise IdempotencyConflictError(
                        f"idempotency_key 已用于不同提交：{key} -> {existing_job['job_id']}"
                    )
                if indexed_request is None and index.get("submission_sha256") != submission_digest:
                    raise IdempotencyConflictError(
                        f"idempotency_key 已用于不同提交：{key} -> {existing_job['job_id']}"
                    )
                # active 索引与历史 alias/tombstone 都必须返回同一个现存 job。
                # 历史 payload 的 digest 不再等于 job 当前 digest，这是修订后的正常状态。
                return existing_job, True

            # 如果上次进程在 job 原子发布后、idempotency 索引发布前崩溃，
            # 仍以 job 文件为事实恢复索引，禁止产生第二份正式任务。
            orphan_matches = [job for job in self._all_jobs_locked() if job.get("idempotency_key") == key]
            if orphan_matches:
                if len(orphan_matches) > 1:
                    raise QueueError(f"idempotency_key 对应多个孤儿任务，需人工修复：{key}")
                existing_job = orphan_matches[0]
                if existing_job.get("submission_sha256") != submission_digest:
                    raise IdempotencyConflictError(
                        f"idempotency_key 已用于不同提交：{key} -> {existing_job['job_id']}"
                    )
                atomic_write_json(index_path, {
                    "schema_version": "idempotency_index_v1",
                    "status": "active",
                    "idempotency_key_sha256": index_path.stem,
                    "job_id": existing_job["job_id"],
                    "submission_sha256": submission_digest,
                    "request_sha256": request_digest,
                    "created_at": existing_job.get("created_at", utc_now()),
                    "recovered_at": utc_now(),
                })
                self._append_event_locked(
                    "idempotency_index_recovered",
                    job_id=existing_job["job_id"],
                    actor="queue-recovery",
                )
                return existing_job, True

            job_id = requested_job_id or new_job_id()
            self._validate_job_id(job_id)
            job_path = self._job_path(job_id)
            if job_path.exists():
                raise IdempotencyConflictError(f"job_id 已存在但 idempotency_key 不同：{job_id}")
            now = utc_now()
            state = "DRAFT" if draft else "READY"
            job = dict(normalized)
            job.update({
                "job_id": job_id,
                "submitted_at": now,
                "created_at": now,
                "updated_at": now,
                "state_updated_at": now,
                "state": state,
                "state_version": 1,
                "submission_sha256": submission_digest,
                "request_sha256": request_digest,
                "wake_status": "QUEUED_WAKE_PENDING",
            })
            if state == "READY":
                self._validate_ready_gate_locked(job)
            # 先发布 job，再发布索引；锁内任一异常不会产生“索引指向不存在 job”。
            atomic_write_json(job_path, job)
            atomic_write_json(index_path, {
                "schema_version": "idempotency_index_v1",
                "status": "active",
                "idempotency_key_sha256": index_path.stem,
                "job_id": job_id,
                "submission_sha256": submission_digest,
                "request_sha256": request_digest,
                "created_at": now,
            })
            self._append_event_locked(
                "submitted",
                job_id=job_id,
                actor=actor,
                to_state=state,
                details={"idempotency_key_sha256": index_path.stem, "wake_status": "QUEUED_WAKE_PENDING"},
            )
            return job, False

    def revise(
        self,
        job_id: str,
        raw: dict[str, Any],
        *,
        ready: bool = False,
        actor: str = "question-worker",
        expected_state: str,
        expected_state_version: int,
        expected_generation: int | None,
        claim_token: str | None = None,
    ) -> dict[str, Any]:
        """原子修订 DRAFT/NEEDS_USER 的完整 submission 用户字段。

        修订不是 patch：调用方必须重新提供完整 question_submission_v1。队列用
        state/version/generation 三重 CAS 防止覆盖并发更新；若异常历史记录仍带
        claim，还会额外校验 actor/token/generation fencing。
        """
        self._validate_job_id(job_id)
        if raw.get("job_id") is None:
            raise ValidationError("revise 的完整 question_submission_v1 必须显式包含 job_id")
        if raw.get("job_id") != job_id:
            raise ValidationError(f"revise 禁止修改 job_id：{raw.get('job_id')!r} != {job_id!r}")
        request_payload = json.loads(json.dumps(raw, ensure_ascii=False))
        request_payload.pop("job_id", None)
        request_digest = hashlib.sha256(canonical_json_bytes(request_payload)).hexdigest()
        normalized = self._validate_and_normalize_submission(raw)
        if normalized.get("job_id") != job_id:
            raise ValidationError("revise 规范化后 job_id 不一致")
        if expected_state not in {"DRAFT", "NEEDS_USER"}:
            raise ValidationError("revise expected_state 只能为 DRAFT 或 NEEDS_USER")
        if isinstance(expected_state_version, bool) or not isinstance(expected_state_version, int):
            raise ValidationError("expected_state_version 必须为整数")
        actor = _nonempty_string(actor, "actor")

        payload_for_digest = dict(normalized)
        payload_for_digest.pop("job_id", None)
        submission_digest = hashlib.sha256(canonical_json_bytes(payload_for_digest)).hexdigest()
        new_key = normalized["idempotency_key"]
        new_index_path = self._idempotency_path(new_key)

        with self._locked():
            self._assert_coordinator_generation_locked(expected_generation)
            job = self._load_job_locked(job_id)
            current = job.get("state")
            if current not in {"DRAFT", "NEEDS_USER"}:
                raise InvalidTransitionError(f"任务 {job_id} 当前为 {current}，只允许修订 DRAFT/NEEDS_USER")
            if current == "NEEDS_USER" and job.get("needs_user_from_state") in POSTCOMMIT_STATES:
                raise InvalidTransitionError(
                    f"任务 {job_id} 已正式发布后从 {job.get('needs_user_from_state')} 进入 NEEDS_USER；"
                    "禁止 revise，必须使用 resolve-user 仅补充校验元数据并回到 VALIDATING"
                )
            if current != expected_state:
                raise InvalidTransitionError(
                    f"任务 {job_id} 当前为 {current}，不符合 expected_state={expected_state}"
                )
            actual_version = job.get("state_version")
            if actual_version != expected_state_version:
                raise InvalidTransitionError(
                    f"stale state_version：expected={expected_state_version} actual={actual_version}"
                )

            claim = job.get("claim")
            if isinstance(claim, dict):
                if (
                    actor != claim.get("worker_id")
                    or not claim_token
                    or claim_token != claim.get("token")
                    or expected_generation != claim.get("coordinator_generation")
                ):
                    raise InvalidTransitionError("revise claim actor/token/generation fencing 不匹配")
            elif claim_token is not None:
                raise InvalidTransitionError("任务没有 claim，revise 不接受 claim_token")

            old_evidence = job.get("evidence")
            if not isinstance(old_evidence, list):
                raise QueueError(f"任务 {job_id} 的 persisted evidence 损坏")
            old_evidence_by_path: dict[str, str] = {}
            for index, item in enumerate(old_evidence):
                if not isinstance(item, dict):
                    raise QueueError(f"任务 {job_id} 的 persisted evidence[{index}] 损坏")
                old_path = Path(_nonempty_string(item.get("path"), f"persisted evidence[{index}].path"))
                old_sha = _nonempty_string(item.get("sha256"), f"persisted evidence[{index}].sha256").lower()
                try:
                    old_resolved = old_path.resolve(strict=True)
                except FileNotFoundError as exc:
                    raise ValidationError(
                        f"revise 禁止删除历史 evidence：{old_path}"
                    ) from exc
                if sha256_file(old_resolved) != old_sha:
                    raise ValidationError(
                        f"revise 检出历史 evidence 被同路径覆盖：{old_resolved}；"
                        "必须保留旧文件并写入 revision-N/ 新路径"
                    )
                old_evidence_by_path[str(old_resolved)] = old_sha

            new_evidence = normalized.get("evidence") or []
            new_evidence_by_path = {
                str(Path(str(item["path"])).resolve()): str(item["sha256"]).lower()
                for item in new_evidence
                if isinstance(item, dict)
            }
            for path, old_sha in old_evidence_by_path.items():
                new_sha = new_evidence_by_path.get(path)
                if new_sha is not None and new_sha != old_sha:
                    raise ValidationError(
                        f"revise 禁止同路径 evidence 改 SHA：{path}；"
                        "必须保留旧文件并写入 revision-N/ 新路径"
                    )
            evidence_changed = old_evidence_by_path != new_evidence_by_path
            if old_evidence_by_path and evidence_changed:
                _inbox, canonical_job_dir = self._canonical_job_dir(job_id)
                for path in new_evidence_by_path:
                    relative = Path(path).relative_to(canonical_job_dir)
                    if len(relative.parts) < 2 or not re.fullmatch(r"revision-\d+", relative.parts[0]):
                        raise ValidationError(
                            "revise 变更 evidence 时所有新证据必须写入 "
                            f"details/inbox/{job_id}/revision-N/：{path}"
                        )

            old_key = _nonempty_string(job.get("idempotency_key"), "persisted idempotency_key")
            old_digest = _nonempty_string(job.get("submission_sha256"), "persisted submission_sha256")
            old_request_digest = str(job.get("request_sha256") or old_digest)
            if old_key == new_key and old_digest != submission_digest:
                raise IdempotencyConflictError(
                    "submission 内容发生变化时 revise 必须使用新的 idempotency_key"
                )

            if new_index_path.exists():
                new_index = read_json(new_index_path)
                if new_index.get("job_id") != job_id:
                    raise IdempotencyConflictError(
                        f"新 idempotency_key 已属于其他任务：{new_key} -> {new_index.get('job_id')}"
                    )
                indexed_digest = new_index.get("submission_sha256")
                if new_key != old_key and indexed_digest != submission_digest:
                    raise IdempotencyConflictError(
                        f"新 idempotency_key 已绑定不同 submission：{new_key}"
                    )
            duplicate_jobs = [
                other for other in self._all_jobs_locked()
                if other.get("job_id") != job_id and other.get("idempotency_key") == new_key
            ]
            if duplicate_jobs:
                raise IdempotencyConflictError(
                    f"新 idempotency_key 已被任务占用：{new_key} -> "
                    f"{sorted(str(item.get('job_id')) for item in duplicate_jobs)}"
                )

            old_index_path = self._idempotency_path(old_key)
            if old_index_path.exists():
                old_index = read_json(old_index_path)
                if old_index.get("job_id") != job_id:
                    raise QueueError(
                        f"旧 idempotency 索引不安全，未执行 revise：{old_key} -> {old_index.get('job_id')}"
                    )

            now = utc_now()
            target_state = "READY" if ready else current
            updated = dict(normalized)
            updated.update({
                "job_id": job_id,
                "submitted_at": job.get("submitted_at"),
                "created_at": job.get("created_at"),
                "updated_at": now,
                "state_updated_at": now if target_state != current else job.get("state_updated_at"),
                "state": target_state,
                "state_version": expected_state_version + 1,
                "submission_sha256": submission_digest,
                "request_sha256": request_digest,
                "wake_status": "QUEUED_WAKE_PENDING",
            })
            if target_state == "READY":
                self._validate_ready_gate_locked(updated)

            # 崩溃安全顺序：先发布 job。若随后中断，submit 的孤儿索引恢复逻辑可
            # 依据 job 重建新索引；旧索引只会因 digest 不符而拒绝，绝不会生成副本。
            atomic_write_json(self._job_path(job_id), updated)
            atomic_write_json(new_index_path, {
                "schema_version": "idempotency_index_v1",
                "status": "active",
                "idempotency_key_sha256": new_index_path.stem,
                "job_id": job_id,
                "submission_sha256": submission_digest,
                "request_sha256": request_digest,
                "created_at": now,
                "revised_from_key_sha256": old_index_path.stem,
            })
            if old_index_path != new_index_path:
                atomic_write_json(old_index_path, {
                    "schema_version": "idempotency_index_v1",
                    "status": "alias",
                    "idempotency_key_sha256": old_index_path.stem,
                    "job_id": job_id,
                    "submission_sha256": old_digest,
                    "request_sha256": old_request_digest,
                    "created_at": job.get("created_at", now),
                    "aliased_to_key_sha256": new_index_path.stem,
                    "revised_at": now,
                })
            self._append_event_locked(
                "revised",
                job_id=job_id,
                actor=actor,
                from_state=current,
                to_state=target_state,
                details={
                    "old_idempotency_key_sha256": old_index_path.stem,
                    "new_idempotency_key_sha256": new_index_path.stem,
                    "old_submission_sha256": old_digest,
                    "new_submission_sha256": submission_digest,
                    "old_state_version": expected_state_version,
                    "new_state_version": expected_state_version + 1,
                    "ready": ready,
                },
            )
            return updated

    def status(self, job_id: str) -> dict[str, Any]:
        with self._locked():
            return self._load_job_locked(job_id)

    def verify_validating_batch_receipts(
        self,
        job_ids: Iterable[str],
        *,
        expected_generation: int | None,
    ) -> dict[str, Any]:
        """为 post-commit 质量修复只读复核同一批 normal receipt。

        调用方必须先用原 normal receipt 把任务推进到 ``VALIDATING``。本方法
        不改变队列，只在同一 queue lock 内重新验证 coordinator CAS、旧 commit
        lease、manifest/approved/WAL/receipt 身份以及全部正式目标 after hash。
        """
        requested = list(job_ids)
        if not requested:
            raise ValidationError("post-commit receipt 复核至少需要一个 job_id")
        if len(set(requested)) != len(requested):
            raise ValidationError("post-commit receipt 复核 job_id 不得重复")
        with self._locked():
            self._assert_coordinator_generation_locked(expected_generation)
            jobs: list[dict[str, Any]] = []
            receipt: dict[str, Any] | None = None
            identity: tuple[Any, ...] | None = None
            for job_id in requested:
                job = self._load_job_locked(job_id)
                if job.get("state") != "VALIDATING":
                    raise InvalidTransitionError(
                        f"post-commit 质量修复只允许 VALIDATING；{job_id} 当前为 {job.get('state')}"
                    )
                current_receipt = self._validate_batch_receipt_locked(
                    job, recovery_required=False
                )
                formal_id = _nonempty_string(job.get("formal_id"), "job.formal_id")
                current_identity = (
                    job.get("commit_batch_id"),
                    job.get("batch_receipt_path"),
                    job.get("batch_receipt_sha256"),
                    current_receipt.get("transaction_id"),
                )
                if identity is None:
                    identity = current_identity
                    receipt = current_receipt
                elif current_identity != identity:
                    raise ValidationError("post-commit 质量修复的 jobs 必须绑定同一 batch receipt")
                matching = [
                    item for item in current_receipt.get("items", [])
                    if isinstance(item, dict) and item.get("job_id") == job_id
                ]
                if len(matching) != 1 or matching[0].get("formal_id") != formal_id:
                    raise ValidationError(f"{job_id} formal_id 与 batch receipt 不一致")
                jobs.append(job)
            assert receipt is not None
            receipt_items = receipt.get("items")
            if not isinstance(receipt_items, list):
                raise ValidationError("post-commit batch receipt.items 必须为数组")
            receipt_job_ids: list[str] = []
            for index, item in enumerate(receipt_items):
                if not isinstance(item, dict):
                    raise ValidationError(
                        f"post-commit batch receipt.items[{index}] 必须为 object"
                    )
                receipt_job_ids.append(
                    _nonempty_string(
                        item.get("job_id"),
                        f"post-commit batch receipt.items[{index}].job_id",
                    )
                )
            if len(receipt_job_ids) != len(set(receipt_job_ids)):
                raise ValidationError("post-commit batch receipt.items 含重复 job_id")
            if set(requested) != set(receipt_job_ids):
                raise ValidationError(
                    "post-commit 质量修复必须覆盖原 receipt 的完整 job 集合："
                    f"requested={sorted(requested)} receipt={sorted(receipt_job_ids)}"
                )
            return {"jobs": jobs, "receipt": receipt}

    def list_jobs(self, states: Iterable[str] | None = None) -> list[dict[str, Any]]:
        requested = set(states or [])
        unknown = requested - STATES
        if unknown:
            raise ValidationError(f"未知状态：{sorted(unknown)}")
        with self._locked():
            jobs = self._all_jobs_locked()
        if requested:
            jobs = [job for job in jobs if job.get("state") in requested]
        return sorted(jobs, key=lambda item: (item.get("created_at", ""), item.get("job_id", "")))

    def _transition_locked(
        self,
        job: dict[str, Any],
        to_state: str,
        *,
        actor: str,
        reason: str | None,
        expected_state: str | None = None,
        metadata: dict[str, Any] | None = None,
        event_type: str = "transitioned",
        allow_recovery: bool = False,
        allow_user_resolution: bool = False,
        coordinator_generation: int | None = None,
        claim_token: str | None = None,
    ) -> dict[str, Any]:
        if to_state not in STATES:
            raise ValidationError(f"未知目标状态：{to_state}")
        current = job.get("state")
        if current not in STATES:
            raise QueueError(f"任务 {job.get('job_id')} 当前状态损坏：{current!r}")
        if expected_state is not None and current != expected_state:
            raise InvalidTransitionError(
                f"任务 {job['job_id']} 当前为 {current}，不符合 expected_state={expected_state}"
            )
        if metadata is not None:
            if not isinstance(metadata, dict):
                raise ValidationError("metadata 必须为 object")
            bad_metadata = set(metadata) - ALLOWED_METADATA_KEYS
            if bad_metadata:
                raise ValidationError(f"metadata 含不可修改字段：{sorted(bad_metadata)}")

        same_state_annotation = current == to_state and bool(metadata)
        postcommit_needs_user = (
            current == "NEEDS_USER" and job.get("needs_user_from_state") in POSTCOMMIT_STATES
        )
        if postcommit_needs_user and not allow_user_resolution:
            raise InvalidTransitionError(
                f"任务 {job['job_id']} 是正式发布后的 NEEDS_USER；"
                "只能使用 resolve-user 补充校验元数据并回到 VALIDATING"
            )
        if not same_state_annotation and to_state not in ALLOWED_TRANSITIONS[current] and not allow_recovery:
            raise InvalidTransitionError(f"非法状态迁移：{current} -> {to_state}")
        if allow_user_resolution and not (
            postcommit_needs_user and to_state == "VALIDATING"
        ):
            raise InvalidTransitionError("resolve-user 只允许 postcommit NEEDS_USER -> VALIDATING")

        if current in POSTCOMMIT_STATES or isinstance(job.get("commit_lease"), dict):
            identity_updates = POSTCOMMIT_IDENTITY_METADATA_KEYS.intersection(metadata or {})
            changed_identity: list[str] = []
            for key in sorted(identity_updates):
                old_value = job.get(key)
                new_value = metadata[key]
                if old_value == new_value:
                    continue
                # WAL recovery is the sole legitimate late binding: both
                # recovery receipt fields are initially absent in COMMITTING.
                if (
                    current == "COMMITTING"
                    and to_state == "COMMITTING"
                    and key in POSTCOMMIT_LATE_BINDING_METADATA_KEYS
                    and old_value in (None, "")
                ):
                    continue
                # The normal receipt and the receipt-assigned formal ID are
                # first bound atomically while leaving COMMITTING.  No later
                # transition or annotation may supply a replacement.
                if (
                    current == "COMMITTING"
                    and to_state == "COMMITTED"
                    and key in POSTCOMMIT_NORMAL_BINDING_METADATA_KEYS
                    and old_value in (None, "")
                ):
                    continue
                changed_identity.append(key)
            if changed_identity:
                raise InvalidTransitionError(
                    "postcommit 迁移或同状态注记不可改写身份字段："
                    f"{changed_identity}"
                )

        if to_state == "REVIEWING" and not allow_recovery:
            raise InvalidTransitionError("进入 REVIEWING 只能使用 claim，不能用 transition 绕过并发上限")

        if current == "COMMITTING" and to_state == "RETRYABLE" and not allow_recovery:
            raise InvalidTransitionError(
                "COMMITTING 不能直接转 RETRYABLE；必须先运行 WAL recovery 并校验 recovery receipt"
            )

        if (
            current == "COMMITTING"
            and metadata
            and "commit_batch_id" in metadata
            and metadata["commit_batch_id"] != job.get("commit_batch_id")
        ):
            raise InvalidTransitionError("COMMITTING lease 的 commit_batch_id 不可改写")

        if current == "REVIEWING" and not allow_recovery:
            claim = job.get("claim")
            if not isinstance(claim, dict):
                raise InvalidTransitionError(f"REVIEWING 任务 {job['job_id']} 缺 claim")
            if actor != claim.get("worker_id"):
                raise InvalidTransitionError("只有持有 claim 的 worker 才能转出 REVIEWING")
            if not claim_token or claim_token != claim.get("token"):
                raise InvalidTransitionError("claim token 不匹配")
            if coordinator_generation != claim.get("coordinator_generation"):
                raise InvalidTransitionError("claim coordinator generation 不匹配")

        if current == "RETRYABLE" and to_state not in {"FAILED", "CANCELLED"}:
            retry_from = job.get("retry_from_state")
            if retry_from not in STATES or to_state != retry_from:
                raise InvalidTransitionError(
                    f"{job['job_id']} 从 {retry_from} 中断，只能先恢复到 {retry_from}，不能转 {to_state}"
                )

        now = utc_now()
        updated = dict(job)
        updated["updated_at"] = now
        updated["state_version"] = int(job.get("state_version", 0)) + 1
        if not same_state_annotation:
            updated["state"] = to_state
            updated["state_updated_at"] = now
        if metadata:
            updated.update(metadata)
        if to_state == "NEEDS_USER" and current != "NEEDS_USER":
            updated["needs_user_from_state"] = current
            updated["needs_user_at"] = now
        elif current == "NEEDS_USER" and to_state != "NEEDS_USER":
            updated.pop("needs_user_from_state", None)
            updated.pop("needs_user_at", None)
        if to_state == "READY" and current != "READY":
            self._validate_ready_gate_locked(updated)
        if to_state == "COMMITTING" and current != "COMMITTING":
            batch_id = updated.get("commit_batch_id")
            batch_id = _nonempty_string(batch_id, "metadata.commit_batch_id")
            active = [
                other for other in self._all_jobs_locked()
                if other.get("state") == "COMMITTING" and other.get("job_id") != job.get("job_id")
            ]
            conflicts = [other for other in active if other.get("commit_batch_id") != batch_id]
            if conflicts:
                raise InvalidTransitionError(
                    "已有其他 COMMITTING batch lease："
                    f"{sorted({str(item.get('commit_batch_id')) for item in conflicts})}"
                )
            for other in active:
                lease = other.get("commit_lease") or {}
                if (
                    lease.get("actor") != actor
                    or lease.get("coordinator_generation") != coordinator_generation
                ):
                    raise InvalidTransitionError("同一 commit batch 的所有 job 必须共享 writer/generation")
            updated["commit_batch_id"] = batch_id
            updated["commit_lease"] = {
                "batch_id": batch_id,
                "actor": actor,
                "coordinator_generation": coordinator_generation,
                "acquired_at": now,
            }
        if current == "COMMITTING" and to_state == "COMMITTED" and not allow_recovery:
            # normal commit receipt 与 recovery receipt 分开，避免把无 recovery 字段的
            # 正常 receipt 冒充崩溃恢复证明。
            self._validate_batch_receipt_locked(updated, recovery_required=False)
        if to_state == "RETRYABLE" and current != "RETRYABLE":
            updated["retry_from_state"] = current
        elif current == "RETRYABLE" and to_state != "RETRYABLE":
            updated.pop("retry_from_state", None)
        if current == "REVIEWING" and to_state != "REVIEWING":
            updated.pop("claim", None)
            if to_state != "REVIEWED":
                updated.pop("review_fence", None)

        atomic_write_json(self._job_path(job["job_id"]), updated)
        emitted_event_type = (
            event_type if event_type != "transitioned" else ("annotated" if same_state_annotation else "transitioned")
        )
        self._append_event_locked(
            emitted_event_type,
            job_id=job["job_id"],
            actor=actor,
            from_state=current,
            to_state=to_state,
            reason=reason,
            details={"metadata": metadata} if metadata else None,
        )
        return updated

    def transition(
        self,
        job_id: str,
        to_state: str,
        *,
        actor: str = "coordinator",
        reason: str | None = None,
        expected_state: str | None = None,
        metadata: dict[str, Any] | None = None,
        expected_generation: int | None = None,
        claim_token: str | None = None,
    ) -> dict[str, Any]:
        with self._locked():
            self._assert_coordinator_generation_locked(expected_generation)
            job = self._load_job_locked(job_id)
            return self._transition_locked(
                job,
                to_state,
                actor=actor,
                reason=reason,
                expected_state=expected_state,
                metadata=metadata,
                coordinator_generation=expected_generation,
                claim_token=claim_token,
            )

    def resolve_user(
        self,
        job_id: str,
        metadata: dict[str, Any],
        *,
        actor: str = "coordinator",
        reason: str | None = None,
        expected_state_version: int,
        expected_generation: int | None,
    ) -> dict[str, Any]:
        """仅补充 postcommit NEEDS_USER 的校验元数据并回到 VALIDATING。"""
        if not isinstance(metadata, dict) or not metadata:
            raise ValidationError("resolve-user metadata 必须为非空 object")
        bad = set(metadata) - USER_RESOLUTION_METADATA_KEYS
        if bad:
            raise ValidationError(f"resolve-user metadata 只允许 {sorted(USER_RESOLUTION_METADATA_KEYS)}：{sorted(bad)}")
        if isinstance(expected_state_version, bool) or not isinstance(expected_state_version, int):
            raise ValidationError("expected_state_version 必须为整数")
        actor = _nonempty_string(actor, "actor")
        with self._locked():
            self._assert_coordinator_generation_locked(expected_generation)
            job = self._load_job_locked(job_id)
            if job.get("state") != "NEEDS_USER" or job.get("needs_user_from_state") not in POSTCOMMIT_STATES:
                raise InvalidTransitionError(
                    "resolve-user 只适用于从正式提交/校验阶段进入的 NEEDS_USER"
                )
            if job.get("state_version") != expected_state_version:
                raise InvalidTransitionError(
                    f"stale state_version：expected={expected_state_version} actual={job.get('state_version')}"
                )
            resolution_metadata = dict(metadata)
            resolution_metadata["user_resolution_at"] = utc_now()
            return self._transition_locked(
                job,
                "VALIDATING",
                actor=actor,
                reason=reason or "用户补充已记录；保留正式/receipt 身份并继续只读校验",
                expected_state="NEEDS_USER",
                metadata=resolution_metadata,
                event_type="user_resolved_postcommit",
                allow_recovery=True,
                allow_user_resolution=True,
                coordinator_generation=expected_generation,
            )

    def claim(
        self,
        job_id: str | None = None,
        *,
        worker: str,
        limit: int = 1,
        expected_generation: int | None = None,
    ) -> list[dict[str, Any]]:
        worker = _nonempty_string(worker, "worker")
        if limit < 1:
            raise ValidationError("claim limit 必须至少为 1")
        with self._locked():
            self._assert_coordinator_generation_locked(expected_generation)
            all_jobs = self._all_jobs_locked()
            reviewing = sum(1 for job in all_jobs if job.get("state") == "REVIEWING")
            capacity = max(0, self.max_reviewing - reviewing)
            take = min(limit, capacity)
            if take == 0:
                return []
            if job_id:
                selected = [self._load_job_locked(job_id)]
                if selected[0].get("state") != "READY":
                    raise InvalidTransitionError(f"任务 {job_id} 当前为 {selected[0].get('state')}，不能 claim")
            else:
                selected = [job for job in all_jobs if job.get("state") == "READY"]
                selected.sort(key=lambda item: (item.get("created_at", ""), item.get("job_id", "")))
            claimed: list[dict[str, Any]] = []
            for job in selected:
                if len(claimed) >= take:
                    break
                try:
                    self._validate_ready_gate_locked(job)
                except ValidationError as exc:
                    self._transition_locked(
                        job,
                        "FAILED",
                        actor=worker,
                        reason=f"READY evidence gate failed: {exc}",
                        expected_state="READY",
                        event_type="ready_gate_failed",
                        coordinator_generation=expected_generation,
                    )
                    if job_id:
                        raise
                    continue
                now = utc_now()
                token = uuid.uuid4().hex
                claim_event_id = uuid.uuid4().hex
                updated = dict(job)
                updated.update({
                    "state": "REVIEWING",
                    "state_version": int(job.get("state_version", 0)) + 1,
                    "updated_at": now,
                    "state_updated_at": now,
                    "claim": {
                        "token": token,
                        "worker_id": worker,
                        "coordinator_generation": expected_generation,
                        "claimed_at": now,
                        "heartbeat_at": now,
                    },
                    "review_fence": {
                        "worker_id": worker,
                        "claim_token_sha256": hashlib.sha256(token.encode("utf-8")).hexdigest(),
                        "coordinator_generation": expected_generation,
                        "claim_event_id": claim_event_id,
                        "claimed_at": now,
                    },
                })
                atomic_write_json(self._job_path(job["job_id"]), updated)
                self._append_event_locked(
                    "claimed",
                    event_id=claim_event_id,
                    job_id=job["job_id"],
                    actor=worker,
                    from_state="READY",
                    to_state="REVIEWING",
                )
                claimed.append(updated)
            return claimed

    def heartbeat(
        self,
        job_id: str,
        *,
        worker: str,
        claim_token: str,
        expected_generation: int | None,
    ) -> dict[str, Any]:
        worker = _nonempty_string(worker, "worker")
        claim_token = _nonempty_string(claim_token, "claim_token")
        with self._locked():
            self._assert_coordinator_generation_locked(expected_generation)
            job = self._load_job_locked(job_id)
            if job.get("state") != "REVIEWING" or not isinstance(job.get("claim"), dict):
                raise InvalidTransitionError(f"任务 {job_id} 当前不可 heartbeat")
            claim = dict(job["claim"])
            if (
                claim.get("worker_id") != worker
                or claim.get("token") != claim_token
                or claim.get("coordinator_generation") != expected_generation
            ):
                raise InvalidTransitionError("heartbeat claim fencing 不匹配")
            now = utc_now()
            claim["heartbeat_at"] = now
            updated = dict(job)
            updated["claim"] = claim
            updated["updated_at"] = now
            updated["state_version"] = int(job.get("state_version", 0)) + 1
            atomic_write_json(self._job_path(job_id), updated)
            self._append_event_locked(
                "claim_heartbeat",
                job_id=job_id,
                actor=worker,
                from_state="REVIEWING",
                to_state="REVIEWING",
                details={"coordinator_generation": expected_generation},
            )
            return updated

    def cancel(self, job_id: str, *, actor: str = "user", reason: str | None = None) -> dict[str, Any]:
        with self._locked():
            job = self._load_job_locked(job_id)
            if job.get("state") in {"COMMITTING", "COMMITTED", "VALIDATING", "DONE"}:
                raise InvalidTransitionError(
                    f"任务 {job_id} 已进入提交/校验阶段（{job.get('state')}），不能直接取消"
                )
            return self._transition_locked(
                job,
                "CANCELLED",
                actor=actor,
                reason=reason or "用户取消（保留任务与证据）",
                event_type="cancelled",
            )

    def mark_wake(
        self,
        job_id: str,
        *,
        ok: bool,
        actor: str = "question-worker",
        error: str | None = None,
        target_thread_id: str | None = None,
        target_generation: int | None = None,
    ) -> dict[str, Any]:
        """记录跨任务唤醒结果，不改变正式状态机。"""
        with self._locked():
            job = self._load_job_locked(job_id)
            metadata: dict[str, Any] = {
                "wake_status": "WAKE_SENT" if ok else "QUEUED_WAKE_PENDING",
                "wake_error": None if ok else (error or "主协调器未确认唤醒；请手动扫描磁盘队列"),
            }
            if ok:
                coordinator = self._assert_coordinator_generation_locked(target_generation)
                target_thread_id = _nonempty_string(target_thread_id, "target_thread_id")
                if coordinator.get("threadId") != target_thread_id:
                    raise InvalidTransitionError("wake target thread 与当前 coordinator 不一致")
                metadata.update({
                    "wake_target_thread_id": target_thread_id,
                    "wake_target_generation": target_generation,
                    "wake_sent_at": utc_now(),
                })
            updated = self._transition_locked(
                job,
                job["state"],
                actor=actor,
                reason="协调器唤醒成功" if ok else "协调器唤醒待重试；主状态保持不变",
                expected_state=job["state"],
                metadata=metadata,
                event_type="wake_marked",
            )
            return updated

    def recover(
        self,
        *,
        stale_seconds: int = 900,
        actor: str = "coordinator-recovery",
        expected_generation: int | None = None,
    ) -> list[dict[str, Any]]:
        if stale_seconds < 0:
            raise ValidationError("stale_seconds 不能为负数")
        cutoff = dt.datetime.now(dt.timezone.utc) - dt.timedelta(seconds=stale_seconds)
        recovered: list[dict[str, Any]] = []
        with self._locked():
            self._assert_coordinator_generation_locked(expected_generation)
            for job in self._all_jobs_locked():
                state = job.get("state")
                if state not in {"REVIEWING", "COMMITTING", "COMMITTED", "VALIDATING"}:
                    continue
                timestamp_value = job.get("state_updated_at") or job.get("updated_at")
                if state == "REVIEWING" and isinstance(job.get("claim"), dict):
                    timestamp_value = job["claim"].get("heartbeat_at") or timestamp_value
                if not timestamp_value or parse_utc(timestamp_value) > cutoff:
                    continue
                if state == "REVIEWING":
                    target = "READY"
                    reason = "REVIEWING 租约过期，释放后可重新领取"
                elif state == "COMMITTING":
                    try:
                        receipt = self._validate_batch_receipt_locked(job, recovery_required=True)
                    except ValidationError as exc:
                        # 没有可验证的事务恢复回执时保持 COMMITTING；绝不能靠超时
                        # 猜测“已回滚”并重新 apply。
                        self._append_event_locked(
                            "recovery_deferred",
                            job_id=job["job_id"],
                            actor=actor,
                            from_state="COMMITTING",
                            to_state="COMMITTING",
                            reason=str(exc),
                        )
                        continue
                    action = receipt["recovery"]["action"]
                    if action == "roll_forward":
                        target = "COMMITTED"
                        reason = "WAL 已 roll-forward，依据带哈希 recovery receipt 补记 COMMITTED"
                    else:
                        target = "RETRYABLE"
                        reason = "WAL 已确认 roll-back，可从 COMMITTING 精确重试"
                elif state == "COMMITTED":
                    target = "VALIDATING"
                    reason = "COMMITTED 回执中断：不重复 apply，转入正式校验"
                else:
                    target = "RETRYABLE"
                    reason = "VALIDATING 中断：恢复后重跑只读校验"
                try:
                    updated = self._transition_locked(
                        job,
                        target,
                        actor=actor,
                        reason=reason,
                        expected_state=state,
                        event_type="recovered",
                        allow_recovery=True,
                        coordinator_generation=expected_generation,
                    )
                except ValidationError as exc:
                    if state != "REVIEWING":
                        raise
                    updated = self._transition_locked(
                        job,
                        "FAILED",
                        actor=actor,
                        reason=f"stale REVIEWING evidence gate failed: {exc}",
                        expected_state=state,
                        event_type="recovery_failed_evidence",
                        allow_recovery=True,
                        coordinator_generation=expected_generation,
                    )
                recovered.append(updated)
        return recovered

    def drain(
        self,
        *,
        deactivate_if_idle: bool = False,
        actor: str = "coordinator",
        expected_generation: int | None = None,
    ) -> dict[str, Any]:
        """登记 graceful drain；不替 coordinator 执行正式入库。"""
        with self._authority_locked():
            self._assert_coordinator_generation_locked(expected_generation)
            jobs = self._all_jobs_locked()
            counts = Counter(job.get("state", "UNKNOWN") for job in jobs)
            active_states = {"READY", "REVIEWING", "REVIEWED", "COMMITTING", "COMMITTED", "VALIDATING", "RETRYABLE"}
            active_count = sum(counts[state] for state in active_states)
            coordinator = read_json(self.coordinator_path) if self.coordinator_path.exists() else None
            now = utc_now()
            if coordinator:
                coordinator["draining"] = True
                coordinator["drain_requested_at"] = now
                coordinator["updated_at"] = now
                if deactivate_if_idle and active_count == 0:
                    coordinator["active"] = False
                    coordinator["deactivated_at"] = now
                atomic_write_json(self.coordinator_path, coordinator)
            self._append_event_locked(
                "drain_requested",
                actor=actor,
                details={"state_counts": dict(counts), "active_count": active_count, "deactivated": bool(coordinator and not coordinator.get("active"))},
            )
            return {
                "drain_complete": active_count == 0,
                "active_count": active_count,
                "state_counts": dict(sorted(counts.items())),
                "coordinator": coordinator,
            }

    def register_coordinator(self, thread_id: str, host_id: str | None = None) -> dict[str, Any]:
        thread_id = _nonempty_string(thread_id, "thread_id")
        host_id = _nonempty_string(host_id or socket.gethostname(), "host_id")
        with self._authority_locked():
            previous = read_json(self.coordinator_path) if self.coordinator_path.exists() else {}
            generation = int(previous.get("generation", 0)) + 1
            now = utc_now()
            coordinator = {
                "schema_version": COORDINATOR_SCHEMA_VERSION,
                "threadId": thread_id,
                "hostId": host_id,
                "generation": generation,
                "active": True,
                "draining": False,
                "registered_at": now,
                "updated_at": now,
                "deactivated_at": None,
            }
            atomic_write_json(self.coordinator_path, coordinator)
            self._append_event_locked(
                "coordinator_registered",
                actor=thread_id,
                details={"threadId": thread_id, "hostId": host_id, "generation": generation},
            )
            return coordinator

    def show_coordinator(self) -> dict[str, Any] | None:
        with self._locked():
            return read_json(self.coordinator_path) if self.coordinator_path.exists() else None

    def deactivate_coordinator(
        self,
        *,
        actor: str = "coordinator",
        expected_generation: int | None = None,
    ) -> dict[str, Any]:
        with self._authority_locked():
            coordinator = self._assert_coordinator_generation_locked(expected_generation)
            now = utc_now()
            coordinator["active"] = False
            coordinator["draining"] = False
            coordinator["deactivated_at"] = now
            coordinator["updated_at"] = now
            atomic_write_json(self.coordinator_path, coordinator)
            self._append_event_locked(
                "coordinator_deactivated",
                actor=actor,
                details={"generation": coordinator.get("generation")},
            )
            return coordinator


def _load_payload(source: str) -> dict[str, Any]:
    if source == "-":
        text = sys.stdin.read()
    else:
        text = Path(source).read_text(encoding="utf-8")
    try:
        value = json.loads(text)
    except json.JSONDecodeError as exc:
        raise ValidationError(f"提交包不是有效 JSON：{exc}") from exc
    if not isinstance(value, dict):
        raise ValidationError("提交包顶层必须为 object")
    return value


def _json_object(value: str | None) -> dict[str, Any] | None:
    if value is None:
        return None
    try:
        parsed = json.loads(value)
    except json.JSONDecodeError as exc:
        raise ValidationError(f"--metadata-json 不是有效 JSON：{exc}") from exc
    if not isinstance(parsed, dict):
        raise ValidationError("--metadata-json 顶层必须为 object")
    return parsed


def _print(value: Any, *, as_json: bool) -> None:
    if as_json:
        print(json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True))
        return
    if isinstance(value, dict):
        if "job_id" in value:
            print(f"job_id={value['job_id']}")
            print(f"state={value.get('state', '')}")
            print(f"state_version={value.get('state_version', '')}")
            print(f"wake_status={value.get('wake_status', '')}")
            if "duplicate" in value:
                print(f"duplicate={'true' if value.get('duplicate') else 'false'}")
            if "receipt" in value:
                print(f"receipt={value.get('receipt', '')}")
        else:
            for key, item in value.items():
                print(f"{key}={item}")
    elif isinstance(value, list):
        for item in value:
            if isinstance(item, dict) and "job_id" in item:
                print(f"{item['job_id']}\t{item.get('state', '')}\t{item.get('updated_at', '')}")
            else:
                print(item)
    else:
        print(value)


def _add_store_args(parser: argparse.ArgumentParser, *, details: bool = False) -> None:
    parser.add_argument("--queue-root", "--root", default=str(DEFAULT_QUEUE_ROOT), help="持久队列根目录")
    if details:
        parser.add_argument("--details-root", default=str(DEFAULT_DETAILS_ROOT), help="kaoyan-408-details 根目录")
    parser.add_argument(
        "--repo",
        default=str(DEFAULT_FORMAL_REPO_ROOT),
        help="正式 kaoyan-408 仓库；coordinator authority 与 receipt 必须绑定此仓库",
    )
    parser.add_argument("--lock-timeout", type=float, default=30.0, help="单队列锁等待秒数")
    parser.add_argument("--json", action="store_true", help="输出机器可读 JSON")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="408 历史异步队列恢复工具（2026-07-12 起停止接收新任务）"
    )
    sub = parser.add_subparsers(dest="command", required=True)

    p_new = sub.add_parser("new-id", help="已退役：仅测试/历史演练可生成 job_id")
    p_new.add_argument("--json", action="store_true")
    p_new.add_argument(
        "--allow-retired-async-submit",
        action="store_true",
        help="显式解锁已退役 job_id 生成，仅供隔离测试或历史恢复演练",
    )

    p_submit = sub.add_parser(
        "submit",
        help="已退役：仅测试/历史演练可显式解锁 question_submission_v1 提交",
    )
    p_submit.add_argument("package", help="JSON 文件，- 表示 stdin")
    p_submit.add_argument("--draft", action="store_true", help="以 DRAFT 而非 READY 提交")
    p_submit.add_argument("--actor", default="question-worker")
    p_submit.add_argument(
        "--allow-retired-async-submit",
        action="store_true",
        help="显式解锁已退役提交，仅供隔离测试或历史恢复演练；日常 408 入库禁止使用",
    )
    _add_store_args(p_submit, details=True)

    p_revise = sub.add_parser(
        "revise",
        help="原子替换 DRAFT/NEEDS_USER 的完整 submission，可选择通过 READY 门禁",
    )
    p_revise.add_argument("job_id")
    p_revise.add_argument("package", help="完整 question_submission_v1 JSON；job_id 必须不变")
    p_revise.add_argument("--ready", action="store_true", help="修订成功后进入 READY；否则保持原状态")
    p_revise.add_argument("--actor", default="question-worker")
    p_revise.add_argument("--expected-state", choices=("DRAFT", "NEEDS_USER"), required=True)
    p_revise.add_argument("--expected-state-version", type=int, required=True)
    p_revise.add_argument("--expected-generation", type=int, required=True)
    p_revise.add_argument("--claim-token")
    _add_store_args(p_revise, details=True)

    p_resolve = sub.add_parser(
        "resolve-user",
        help="为 postcommit NEEDS_USER 仅补校验元数据并回到 VALIDATING",
    )
    p_resolve.add_argument("job_id")
    p_resolve.add_argument("--metadata-json", required=True)
    p_resolve.add_argument("--actor", default="coordinator")
    p_resolve.add_argument("--reason")
    p_resolve.add_argument("--expected-state-version", type=int, required=True)
    p_resolve.add_argument("--expected-generation", type=int, required=True)
    _add_store_args(p_resolve)

    p_list = sub.add_parser("list", help="列出任务")
    p_list.add_argument("--state", action="append", choices=sorted(STATES), help="可重复指定状态")
    _add_store_args(p_list)

    p_status = sub.add_parser("status", help="读取单个任务")
    p_status.add_argument("job_id")
    _add_store_args(p_status)

    p_claim = sub.add_parser("claim", help="原子领取 READY 任务（全队列最多 3 个 REVIEWING）")
    p_claim.add_argument("job_id", nargs="?")
    p_claim.add_argument("--worker", default=f"{socket.gethostname()}:{os.getpid()}")
    p_claim.add_argument("--limit", type=int, default=1)
    p_claim.add_argument("--max-reviewing", type=int, default=3)
    p_claim.add_argument("--expected-generation", type=int, required=True)
    _add_store_args(p_claim)

    p_heartbeat = sub.add_parser("heartbeat", help="刷新 REVIEWING claim 租约")
    p_heartbeat.add_argument("job_id")
    p_heartbeat.add_argument("--worker", required=True)
    p_heartbeat.add_argument("--claim-token", required=True)
    p_heartbeat.add_argument("--expected-generation", type=int, required=True)
    _add_store_args(p_heartbeat)

    p_transition = sub.add_parser("transition", help="执行受约束状态迁移或同状态元数据注记")
    p_transition.add_argument("job_id")
    p_transition.add_argument("to_state", choices=sorted(STATES))
    p_transition.add_argument("--actor", default="coordinator")
    p_transition.add_argument("--reason")
    p_transition.add_argument("--expected-state", choices=sorted(STATES))
    p_transition.add_argument("--expected-generation", type=int, required=True)
    p_transition.add_argument("--claim-token")
    p_transition.add_argument("--metadata-json")
    _add_store_args(p_transition)

    p_recover = sub.add_parser("recover", help="恢复过期 REVIEWING/COMMITTING/COMMITTED/VALIDATING")
    p_recover.add_argument("--stale-seconds", type=int, default=900)
    p_recover.add_argument("--actor", default="coordinator-recovery")
    p_recover.add_argument("--expected-generation", type=int, required=True)
    _add_store_args(p_recover)

    p_cancel = sub.add_parser("cancel", help="标记取消；不删除任务或证据")
    p_cancel.add_argument("job_id")
    p_cancel.add_argument("--actor", default="user")
    p_cancel.add_argument("--reason")
    _add_store_args(p_cancel)

    p_wake = sub.add_parser("mark-wake", help="记录主协调器唤醒结果；不改变任务主状态")
    p_wake.add_argument("job_id")
    wake_group = p_wake.add_mutually_exclusive_group(required=True)
    wake_group.add_argument("--ok", action="store_true", help="已成功发送 QUEUE_READY")
    wake_group.add_argument("--pending", action="store_true", help="唤醒失败/待重试，保持可扫描")
    p_wake.add_argument("--error")
    p_wake.add_argument("--actor", default="question-worker")
    p_wake.add_argument("--target-thread-id")
    p_wake.add_argument("--target-generation", type=int)
    _add_store_args(p_wake)

    p_drain = sub.add_parser("drain", help="登记 graceful drain 并汇总未完成任务")
    p_drain.add_argument("--deactivate-if-idle", action="store_true")
    p_drain.add_argument("--actor", default="coordinator")
    p_drain.add_argument("--expected-generation", type=int, required=True)
    _add_store_args(p_drain)

    p_coord = sub.add_parser("coordinator", help="管理 coordinator.json")
    coord_sub = p_coord.add_subparsers(dest="coordinator_command", required=True)
    p_register = coord_sub.add_parser("register", help="登记/接管主协调器，generation 自动递增")
    p_register.add_argument("--thread-id", required=True)
    p_register.add_argument("--host-id")
    _add_store_args(p_register)
    p_show = coord_sub.add_parser("show", help="读取 coordinator.json")
    _add_store_args(p_show)
    p_deactivate = coord_sub.add_parser("deactivate", help="停用 coordinator，不删除任务")
    p_deactivate.add_argument("--actor", default="coordinator")
    p_deactivate.add_argument("--expected-generation", type=int, required=True)
    _add_store_args(p_deactivate)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        if args.command == "new-id":
            if not args.allow_retired_async_submit:
                raise ValidationError(
                    "408 新异步任务已于 2026-07-12 退役；日常单题入库不再生成 JOB-*。"
                )
            job_id = new_job_id()
            _print({"job_id": job_id}, as_json=args.json)
            return 0

        if args.command == "submit" and not args.allow_retired_async_submit:
            raise ValidationError(
                "408 新异步提交已于 2026-07-12 退役；请使用 "
                "kaoyan-408-wrong-intake + scripts/intake_apply_408.py "
                "一次同步正式入库一道题。若这是隔离测试或历史恢复演练，必须显式传 "
                "--allow-retired-async-submit。"
            )

        store = QueueStore(
            args.queue_root,
            getattr(args, "details_root", DEFAULT_DETAILS_ROOT),
            formal_repo_root=args.repo,
            lock_timeout=args.lock_timeout,
            max_reviewing=getattr(args, "max_reviewing", 3),
        )
        if args.command == "submit":
            job, duplicate = store.submit(_load_payload(args.package), draft=args.draft, actor=args.actor)
            result = dict(job)
            result["duplicate"] = duplicate
            result["receipt"] = submission_receipt(job, duplicate=duplicate)
            _print(result, as_json=args.json)
        elif args.command == "revise":
            _print(
                store.revise(
                    args.job_id,
                    _load_payload(args.package),
                    ready=args.ready,
                    actor=args.actor,
                    expected_state=args.expected_state,
                    expected_state_version=args.expected_state_version,
                    expected_generation=args.expected_generation,
                    claim_token=args.claim_token,
                ),
                as_json=args.json,
            )
        elif args.command == "resolve-user":
            _print(
                store.resolve_user(
                    args.job_id,
                    _json_object(args.metadata_json) or {},
                    actor=args.actor,
                    reason=args.reason,
                    expected_state_version=args.expected_state_version,
                    expected_generation=args.expected_generation,
                ),
                as_json=args.json,
            )
        elif args.command == "list":
            jobs = store.list_jobs(args.state)
            result: Any = {"count": len(jobs), "jobs": jobs} if args.json else jobs
            _print(result, as_json=args.json)
        elif args.command == "status":
            _print(store.status(args.job_id), as_json=args.json)
        elif args.command == "claim":
            jobs = store.claim(
                args.job_id,
                worker=args.worker,
                limit=args.limit,
                expected_generation=args.expected_generation,
            )
            result = {"count": len(jobs), "jobs": jobs} if args.json else jobs
            _print(result, as_json=args.json)
        elif args.command == "heartbeat":
            _print(
                store.heartbeat(
                    args.job_id,
                    worker=args.worker,
                    claim_token=args.claim_token,
                    expected_generation=args.expected_generation,
                ),
                as_json=args.json,
            )
        elif args.command == "transition":
            result = store.transition(
                args.job_id,
                args.to_state,
                actor=args.actor,
                reason=args.reason,
                expected_state=args.expected_state,
                metadata=_json_object(args.metadata_json),
                expected_generation=args.expected_generation,
                claim_token=args.claim_token,
            )
            _print(result, as_json=args.json)
        elif args.command == "recover":
            jobs = store.recover(
                stale_seconds=args.stale_seconds,
                actor=args.actor,
                expected_generation=args.expected_generation,
            )
            result = {"count": len(jobs), "jobs": jobs} if args.json else jobs
            _print(result, as_json=args.json)
        elif args.command == "cancel":
            _print(store.cancel(args.job_id, actor=args.actor, reason=args.reason), as_json=args.json)
        elif args.command == "mark-wake":
            _print(
                store.mark_wake(
                    args.job_id,
                    ok=args.ok,
                    actor=args.actor,
                    error=args.error,
                    target_thread_id=args.target_thread_id,
                    target_generation=args.target_generation,
                ),
                as_json=args.json,
            )
        elif args.command == "drain":
            _print(
                store.drain(
                    deactivate_if_idle=args.deactivate_if_idle,
                    actor=args.actor,
                    expected_generation=args.expected_generation,
                ),
                as_json=args.json,
            )
        elif args.command == "coordinator":
            if args.coordinator_command == "register":
                result = store.register_coordinator(args.thread_id, args.host_id)
            elif args.coordinator_command == "show":
                result = store.show_coordinator() or {
                    "schema_version": COORDINATOR_SCHEMA_VERSION,
                    "active": False,
                    "registered": False,
                }
            else:
                result = store.deactivate_coordinator(
                    actor=args.actor,
                    expected_generation=args.expected_generation,
                )
            _print(result, as_json=args.json)
        return 0
    except (QueueError, OSError, ValueError) as exc:
        error = {"error": type(exc).__name__, "message": str(exc)}
        if getattr(args, "json", False):
            print(json.dumps(error, ensure_ascii=False, sort_keys=True), file=sys.stderr)
        else:
            print(f"[错误] {error['error']}: {error['message']}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
