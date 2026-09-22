#!/usr/bin/env python3
"""408 单题并行入库编排器。

本脚本不实现第二个正式写者。它只负责：

1. 冻结并校验恰好一个 package；
2. 调用一次公开单题写入口 ``intake_apply_408.py``；
3. 在正式 RepoLock 释放后，按依赖图并行刷新派生面、运行审计并生成旧题回顾；
4. 任一必需分支失败时 fail closed，并允许凭 normal receipt 只重跑 closeout。

流水线锁覆盖 apply + closeout，防止下一道题在当前题派生面尚未收敛时越过。
正式 RepoLock 仍只覆盖短事务；HTTP、审计和派生刷新从不在正式锁内等待。
"""

from __future__ import annotations

import argparse
import fcntl
import hashlib
import json
import os
import subprocess
import sys
import time
from concurrent.futures import FIRST_COMPLETED, Future, ThreadPoolExecutor, wait
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Iterable

sys.path.insert(0, str(Path(__file__).resolve().parent))

from intake_batch_apply_408 import BatchError, load_batch_input  # noqa: E402
from intake_lib_408 import FORMAL_ID_RE, Repo  # noqa: E402
from intake_preflight_408 import DETAILS_ROOT_DEFAULT  # noqa: E402
from intake_validate_408 import SNAPSHOT_PATH  # noqa: E402


TASK_ORDER = (
    "coverage_refresh",
    "bridge_refresh",
    "tables_validate",
    "network_audit",
    "safe_card_audit",
    "review_schedule_audit",
    "related_candidates",
    "extended_audit",
    "detail_validate",
)


class PipelineError(RuntimeError):
    """单题编排合同、栅栏或 receipt 校验失败。"""


class PipelineLockTimeout(PipelineError):
    pass


def pipeline_lock_path(repo_root: str | Path) -> Path:
    root = Path(repo_root).expanduser().resolve()
    git_dir = root / ".git"
    return (git_dir if git_dir.is_dir() else root) / "codex-intake-408-pipeline.lock"


class PipelineLock:
    """跨 apply 与 closeout 的单题流水线栅栏。"""

    def __init__(self, repo_root: str | Path, timeout: float = 600.0) -> None:
        self.path = pipeline_lock_path(repo_root)
        self.timeout = timeout
        self._fh = None

    def __enter__(self) -> "PipelineLock":
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._fh = self.path.open("a+")
        deadline = time.monotonic() + self.timeout
        try:
            while True:
                try:
                    fcntl.flock(self._fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                    self._fh.seek(0)
                    self._fh.truncate()
                    self._fh.write(f"pid={os.getpid()} acquired={time.time():.6f}\n")
                    self._fh.flush()
                    return self
                except BlockingIOError as exc:
                    if time.monotonic() >= deadline:
                        raise PipelineLockTimeout(
                            f"单题流水线栅栏等待超过 {self.timeout:g}s：{self.path}"
                        ) from exc
                    time.sleep(0.05)
        except BaseException:
            self._fh.close()
            self._fh = None
            raise

    def __exit__(self, exc_type, exc, tb) -> None:
        if self._fh is not None:
            fcntl.flock(self._fh.fileno(), fcntl.LOCK_UN)
            self._fh.close()
            self._fh = None


@dataclass(frozen=True)
class TaskSpec:
    key: str
    argv: tuple[str, ...]
    dependencies: tuple[str, ...] = ()
    timeout_seconds: float = 180.0
    required: bool = True


@dataclass
class TaskResult:
    key: str
    status: str
    returncode: int | None
    stdout: str = ""
    stderr: str = ""
    duration_ms: int = 0
    blocked_by: list[str] = field(default_factory=list)

    @property
    def passed(self) -> bool:
        return self.status == "PASS"

    def as_dict(self) -> dict[str, Any]:
        return {
            "key": self.key,
            "status": self.status,
            "returncode": self.returncode,
            "duration_ms": self.duration_ms,
            "blocked_by": self.blocked_by,
            "stdout_tail": _tail(self.stdout),
            "stderr_tail": _tail(self.stderr),
        }


@dataclass
class PipelineResult:
    status: str
    formal_id: str | None
    mode: str | None
    apply_status: str | None
    receipt_path: str | None
    preflight: TaskResult | None = None
    apply: TaskResult | None = None
    tasks: list[TaskResult] = field(default_factory=list)
    error: str | None = None
    formal_apply_count: int = 0
    backflow_status: str | None = None
    review_schedule_ensure_receipt_sha256: str | None = None
    formal_review_binding_receipt_sha256: str | None = None
    formal_review_action_id: str | None = None
    formal_review_due_date: str | None = None
    formal_review_unit_id: str | None = None

    @property
    def complete(self) -> bool:
        return self.status == "COMPLETE"

    def as_dict(self) -> dict[str, Any]:
        related = next((item.stdout for item in self.tasks if item.key == "related_candidates"), "")
        return {
            "status": self.status,
            "formal_id": self.formal_id,
            "mode": self.mode,
            "apply_status": self.apply_status,
            "receipt_path": self.receipt_path,
            "error": self.error,
            "formal_apply_count": self.formal_apply_count,
            "backflow_status": self.backflow_status,
            "review_schedule_ensure_receipt_sha256": (
                self.review_schedule_ensure_receipt_sha256
            ),
            "formal_review_binding_receipt_sha256": (
                self.formal_review_binding_receipt_sha256
            ),
            "formal_review_action_id": self.formal_review_action_id,
            "formal_review_due_date": self.formal_review_due_date,
            "formal_review_unit_id": self.formal_review_unit_id,
            "preflight": self.preflight.as_dict() if self.preflight else None,
            "apply": self.apply.as_dict() if self.apply else None,
            "tasks": [item.as_dict() for item in self.tasks],
            "related_candidates_output": related,
        }


@dataclass(frozen=True)
class CommittedReceiptProof:
    formal_id: str
    mode: str
    receipt_path: str
    receipt_sha256: str
    package: dict[str, Any]


TaskRunner = Callable[[TaskSpec], TaskResult]


def _tail(text: str, lines: int = 8) -> str:
    parts = text.strip().splitlines()
    return "\n".join(parts[-lines:])


def run_command(spec: TaskSpec) -> TaskResult:
    started = time.perf_counter()
    try:
        completed = subprocess.run(
            list(spec.argv),
            capture_output=True,
            text=True,
            timeout=spec.timeout_seconds,
            check=False,
        )
    except subprocess.TimeoutExpired as exc:
        return TaskResult(
            key=spec.key,
            status="TIMEOUT",
            returncode=None,
            stdout=str(exc.stdout or ""),
            stderr=str(exc.stderr or ""),
            duration_ms=round((time.perf_counter() - started) * 1000),
        )
    except OSError as exc:
        return TaskResult(
            key=spec.key,
            status="ERROR",
            returncode=None,
            stderr=str(exc),
            duration_ms=round((time.perf_counter() - started) * 1000),
        )
    return TaskResult(
        key=spec.key,
        status="PASS" if completed.returncode == 0 else "FAIL",
        returncode=completed.returncode,
        stdout=completed.stdout,
        stderr=completed.stderr,
        duration_ms=round((time.perf_counter() - started) * 1000),
    )


def _safe_run(runner: TaskRunner, spec: TaskSpec) -> TaskResult:
    started = time.perf_counter()
    try:
        result = runner(spec)
    except BaseException as exc:  # 子任务异常必须转成硬失败，不能逃逸成误报完成。
        return TaskResult(
            key=spec.key,
            status="ERROR",
            returncode=None,
            stderr=f"{type(exc).__name__}: {exc}",
            duration_ms=round((time.perf_counter() - started) * 1000),
        )
    if result.key != spec.key:
        return TaskResult(
            key=spec.key,
            status="ERROR",
            returncode=None,
            stderr=f"runner 返回了错误 task key：{result.key}",
            duration_ms=result.duration_ms,
        )
    return result


def _validate_task_graph(specs: Iterable[TaskSpec]) -> list[TaskSpec]:
    ordered = list(specs)
    keys = [spec.key for spec in ordered]
    if len(keys) != len(set(keys)):
        raise PipelineError("postcommit task key 不得重复")
    known = set(keys)
    for spec in ordered:
        unknown = set(spec.dependencies) - known
        if unknown:
            raise PipelineError(f"{spec.key} 引用了未知依赖：{sorted(unknown)}")
        if spec.key in spec.dependencies:
            raise PipelineError(f"{spec.key} 不得依赖自身")
    return ordered


def run_task_graph(
    specs: Iterable[TaskSpec],
    *,
    max_workers: int = 7,
    runner: TaskRunner = run_command,
) -> list[TaskResult]:
    """并行执行依赖图，并按声明顺序确定性返回结果。"""
    ordered = _validate_task_graph(specs)
    if max_workers < 1:
        raise PipelineError("max_workers 必须至少为 1")
    order_index = {spec.key: index for index, spec in enumerate(ordered)}
    pending = {spec.key: spec for spec in ordered}
    results: dict[str, TaskResult] = {}
    running: dict[Future[TaskResult], TaskSpec] = {}

    with ThreadPoolExecutor(max_workers=max_workers, thread_name_prefix="intake408") as pool:
        while pending or running:
            made_progress = False
            for spec in ordered:
                if spec.key not in pending or len(running) >= max_workers:
                    continue
                if not all(dep in results for dep in spec.dependencies):
                    continue
                failed_deps = [dep for dep in spec.dependencies if not results[dep].passed]
                if failed_deps:
                    results[spec.key] = TaskResult(
                        key=spec.key,
                        status="BLOCKED",
                        returncode=None,
                        blocked_by=failed_deps,
                        stderr="依赖未通过，任务未启动",
                    )
                    del pending[spec.key]
                    made_progress = True
                    continue
                future = pool.submit(_safe_run, runner, spec)
                running[future] = spec
                del pending[spec.key]
                made_progress = True

            if running:
                done, _ = wait(running, return_when=FIRST_COMPLETED)
                for future in sorted(done, key=lambda item: order_index[running[item].key]):
                    spec = running.pop(future)
                    results[spec.key] = future.result()
                continue
            if pending and not made_progress:
                raise PipelineError(f"postcommit task graph 存在环或无法解析：{sorted(pending)}")

    return [results[spec.key] for spec in ordered]


def _package_snapshot(package_path: Path) -> tuple[str, dict[str, Any]]:
    packages, _ = load_batch_input(package_path)
    if len(packages) != 1:
        raise PipelineError(
            f"并行编排器一次必须恰好 1 个 package，当前为 {len(packages)}；"
            "不得把多题 fan-out 结果直接送入正式写入。"
        )
    package = packages[0]
    canonical = json.dumps(
        package, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")
    return hashlib.sha256(canonical).hexdigest(), package


def _package_identity(package: dict[str, Any]) -> tuple[str | None, str]:
    formal_id = str(package.get("formal_id") or "").strip()
    mode = str(package.get("mode") or "new").strip()
    if mode not in {"new", "redo"}:
        raise PipelineError(f"不支持的 mode：{mode}")
    if mode == "new":
        if (
            formal_id
            or package.get("auto_id") is not True
            or package.get("contract") != "coordinator_approved_intake_v1"
            or package.get("schema_version") != "coordinator_approved_intake_v1"
        ):
            raise PipelineError(
                "new 单题必须以 approved auto_id=true、formal_id=null 进入 apply，"
                "由 RepoLock 内分配正式 ID"
            )
        return None, mode
    if not FORMAL_ID_RE.fullmatch(formal_id):
        raise PipelineError(
            "redo 单题 package 必须提供合法既有 formal_id"
        )
    return formal_id, mode


def _formal_backflow_identity(package: dict[str, Any]) -> tuple[str, str]:
    binding = package.get("curation_evidence_binding")
    if not isinstance(binding, dict):
        raise PipelineError(
            "正式 package 缺 curation_evidence_binding，不能在 preflight 前冻结回流证据"
        )
    batch_id = str(binding.get("batch_id") or "").strip()
    capture_id = str(binding.get("capture_id") or "").strip()
    if not batch_id or not capture_id:
        raise PipelineError("正式 package 缺 batch_id/capture_id 回流身份")
    return batch_id, capture_id


def _ensure_formal_backflow(
    repo_root: Path, package: dict[str, Any]
) -> dict[str, Any]:
    batch_id, capture_id = _formal_backflow_identity(package)
    try:
        from formal_intake_review_backflow_408 import (  # type: ignore
            BackflowError,
            ensure_review_backflow,
        )
        from review_feedback_loop import SCHEDULING_POLICY_VERSION

        return ensure_review_backflow(
            repo_root=repo_root,
            batch_id=batch_id,
            capture_id=capture_id,
            policy_version=SCHEDULING_POLICY_VERSION,
        )
    except ImportError as exc:
        raise PipelineError("正式回流 writer 缺失") from exc
    except BackflowError as exc:
        raise PipelineError(f"正式回流 ensure 失败：{exc}") from exc


def _bind_formal_backflow(
    repo_root: Path,
    package: dict[str, Any],
    ensure_receipt: dict[str, Any],
    *,
    formal_id: str,
    formal_result_status: str,
    formal_result_receipt_sha256: str,
    formal_proof_path: str | Path,
) -> dict[str, Any]:
    batch_id, capture_id = _formal_backflow_identity(package)
    ensure_sha = str(ensure_receipt.get("receipt_sha256") or "")
    try:
        from formal_intake_review_backflow_408 import (  # type: ignore
            BackflowError,
            bind_formal_intake,
        )

        return bind_formal_intake(
            repo_root=repo_root,
            batch_id=batch_id,
            capture_id=capture_id,
            ensure_receipt_sha256=ensure_sha,
            formal_result_status=formal_result_status,
            formal_result_receipt_sha256=formal_result_receipt_sha256,
            formal_id=formal_id,
            formal_proof_path=formal_proof_path,
        )
    except ImportError as exc:
        raise PipelineError("正式回流 writer 缺失") from exc
    except BackflowError as exc:
        raise PipelineError(f"formal review binding 失败：{exc}") from exc


def _validate_complete_formal_backflow(
    repo_root: Path,
    ensure_receipt: dict[str, Any],
    final_receipt: dict[str, Any],
) -> dict[str, Any]:
    try:
        from formal_intake_review_backflow_408 import (  # type: ignore
            BackflowError,
            FINAL_RECEIPT_SCHEMA,
            validate_review_backflow_receipt,
        )

        checked = validate_review_backflow_receipt(
            repo_root,
            str(final_receipt.get("receipt_sha256") or ""),
            expected_schema=FINAL_RECEIPT_SCHEMA,
        )
    except ImportError as exc:
        raise PipelineError("正式回流 validator 缺失") from exc
    except BackflowError as exc:
        raise PipelineError(f"formal review binding receipt 非法：{exc}") from exc
    if checked.get("status") not in {"satisfied", "maintenance_noop"}:
        raise PipelineError("正式回流未达到 satisfied/maintenance_noop")
    if checked.get("ensure_receipt_sha256") != ensure_receipt.get(
        "receipt_sha256"
    ):
        raise PipelineError("formal binding 与 ensure receipt 不一致")
    return checked


def _record_pending_formal_backflow(
    repo_root: Path,
    package: dict[str, Any],
    ensure_receipt: dict[str, Any],
    *,
    formal_id: str,
    formal_result_status: str,
    formal_result_receipt_sha256: str,
    formal_proof_path: str | Path,
    error_code: str,
) -> dict[str, Any] | None:
    batch_id, capture_id = _formal_backflow_identity(package)
    try:
        from formal_intake_review_backflow_408 import (  # type: ignore
            BackflowError,
            record_formal_committed_backflow_pending,
        )

        return record_formal_committed_backflow_pending(
            repo_root,
            batch_id=batch_id,
            capture_id=capture_id,
            ensure_receipt_sha256=str(
                ensure_receipt.get("receipt_sha256") or ""
            ),
            formal_result_status=formal_result_status,
            formal_result_receipt_sha256=formal_result_receipt_sha256,
            formal_id=formal_id,
            error_code=error_code,
            formal_proof_ref=str(formal_proof_path),
        )
    except (ImportError, BackflowError, OSError):
        return None


def _formal_backflow_fields(
    ensure_receipt: dict[str, Any] | None,
    final_receipt: dict[str, Any] | None = None,
    *,
    status: str | None = None,
) -> dict[str, Any]:
    ensure = ensure_receipt or {}
    final = final_receipt or {}
    return {
        "backflow_status": status or final.get("status") or ensure.get("status"),
        "review_schedule_ensure_receipt_sha256": ensure.get("receipt_sha256"),
        "formal_review_binding_receipt_sha256": final.get("receipt_sha256"),
        "formal_review_action_id": final.get("action_id", ensure.get("action_id")),
        "formal_review_due_date": final.get("due_date", ensure.get("due_date")),
        "formal_review_unit_id": final.get(
            "review_unit_id", ensure.get("review_unit_id")
        ),
    }


def build_postcommit_tasks(
    *,
    repo_root: Path,
    details_root: Path,
    snapshot_path: Path,
    formal_id: str,
    mode: str,
    do_http: bool,
    timeout_seconds: float,
) -> list[TaskSpec]:
    scripts = repo_root / "scripts"
    py = sys.executable
    validate_common = (
        "--id", formal_id,
        "--repo", str(repo_root),
        "--details-root", str(details_root),
        "--snapshot", str(snapshot_path),
    )
    new_flag = ("--new",) if mode == "new" else ()
    http_flag = ("--http",) if do_http else ()
    return [
        TaskSpec(
            "coverage_refresh",
            (py, str(scripts / "refresh_coverage_index.py"), "--repo", str(repo_root)),
            timeout_seconds=timeout_seconds,
        ),
        TaskSpec(
            "bridge_refresh",
            (
                py, str(scripts / "build_obsidian_bridge_snapshot.py"),
                "--repo", str(repo_root), "--details-root", str(details_root),
                "--out", str(snapshot_path),
            ),
            timeout_seconds=timeout_seconds,
        ),
        TaskSpec(
            "tables_validate",
            (
                py, str(scripts / "intake_validate_408.py"),
                *validate_common, "--scope", "tables", *new_flag,
            ),
            timeout_seconds=timeout_seconds,
        ),
        TaskSpec(
            "network_audit",
            (py, str(scripts / "audit_network_health.py"), "--repo", str(repo_root)),
            timeout_seconds=timeout_seconds,
        ),
        TaskSpec(
            "safe_card_audit",
            (
                py, str(scripts / "check_safe_node_cards.py"),
                "--repo", str(repo_root), "--list-missing",
            ),
            timeout_seconds=timeout_seconds,
        ),
        TaskSpec(
            "review_schedule_audit",
            (py, str(scripts / "audit_review_schedule.py"), "--repo", str(repo_root)),
            timeout_seconds=timeout_seconds,
        ),
        TaskSpec(
            "related_candidates",
            (
                py, str(scripts / "related_candidates_408.py"),
                "--id", formal_id, "--repo", str(repo_root),
            ),
            timeout_seconds=timeout_seconds,
        ),
        TaskSpec(
            "extended_audit",
            (
                py, str(scripts / "audit_extended.py"),
                "--repo", str(repo_root), "--details-root", str(details_root),
            ),
            dependencies=("coverage_refresh",),
            timeout_seconds=timeout_seconds,
        ),
        TaskSpec(
            "detail_validate",
            (
                py, str(scripts / "intake_validate_408.py"),
                *validate_common, "--scope", "detail", *http_flag,
            ),
            dependencies=("bridge_refresh",),
            timeout_seconds=timeout_seconds,
        ),
    ]


def _parse_apply_payload(
    result: TaskResult, expected_id: str | None
) -> tuple[dict[str, Any], str]:
    if not result.passed:
        raise PipelineError("正式 apply 未通过")
    try:
        payload = json.loads(result.stdout)
    except json.JSONDecodeError as exc:
        raise PipelineError(f"intake_apply_408.py --json 输出无法解析：{exc}") from exc
    if not isinstance(payload, dict):
        raise PipelineError("intake_apply_408.py --json 输出必须是 object")
    if payload.get("status") not in {"COMMITTED", "ALREADY_COMMITTED"}:
        raise PipelineError(f"正式 apply 状态非法：{payload.get('status')}")
    items = payload.get("items")
    if not isinstance(items, list) or len(items) != 1:
        raise PipelineError("正式 apply 必须返回恰好一个 formal item")
    actual_id = str(items[0].get("formal_id") or "")
    if not FORMAL_ID_RE.fullmatch(actual_id):
        raise PipelineError("正式 apply 返回了非法 formal_id")
    if expected_id is not None and actual_id != expected_id:
        raise PipelineError("redo apply 返回的 formal_id 与 package 不一致")
    receipt = str(payload.get("receipt_path") or "").strip()
    if not receipt:
        raise PipelineError("正式 apply 成功但未返回 normal receipt")
    return payload, actual_id


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _load_json_object(path: Path, label: str) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise PipelineError(f"{label} 读取失败：{exc}") from exc
    if not isinstance(value, dict):
        raise PipelineError(f"{label} 必须是 JSON object")
    return value


def verify_committed_receipt(
    receipt_path: str | Path, repo_root: Path
) -> CommittedReceiptProof:
    """验证 normal receipt/WAL 及当前正式目标 after hash，供只收尾恢复。"""
    raw = Path(receipt_path).expanduser()
    if not raw.is_absolute() or raw.is_symlink():
        raise PipelineError("--resume-receipt 必须是非符号链接绝对路径")
    path = raw.resolve(strict=True)
    receipt = _load_json_object(path, "normal receipt")
    if receipt.get("schema") != "intake_batch_receipt_v1" or receipt.get("status") != "COMMITTED":
        raise PipelineError("normal receipt schema/status 非法")
    if Path(str(receipt.get("repo_root") or "")).expanduser().resolve() != repo_root:
        raise PipelineError("normal receipt 未绑定当前正式仓库")
    items = receipt.get("items")
    if not isinstance(items, list) or len(items) != 1:
        raise PipelineError("resume 只允许绑定 batch size=1 的 normal receipt")
    formal_id = str(items[0].get("formal_id") or "")
    mode = str(items[0].get("mode") or "new")
    if not FORMAL_ID_RE.fullmatch(formal_id) or mode not in {"new", "redo"}:
        raise PipelineError("normal receipt 的 formal_id/mode 非法")

    journal_raw = Path(str(receipt.get("journal") or "")).expanduser()
    if not journal_raw.is_absolute() or journal_raw.is_symlink():
        raise PipelineError("normal receipt.journal 必须是非符号链接绝对路径")
    journal = _load_json_object(journal_raw.resolve(strict=True), "normal WAL")
    if journal.get("schema") != "intake_batch_wal_v1" or journal.get("state") != "committed":
        raise PipelineError("normal WAL schema/state 非法")
    for key in ("transaction_id", "idempotency_key", "payload_sha256", "repo_root"):
        if receipt.get(key) != journal.get(key):
            raise PipelineError(f"normal receipt/WAL {key} 不一致")
    if journal.get("result_items") != items:
        raise PipelineError("normal receipt/WAL items 不一致")
    payload_identity = journal.get("payload_identity")
    if not isinstance(payload_identity, dict):
        raise PipelineError("normal WAL 缺 payload_identity")
    payload_bytes = json.dumps(
        payload_identity,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    if hashlib.sha256(payload_bytes).hexdigest() != receipt.get("payload_sha256"):
        raise PipelineError("normal receipt/WAL payload identity 哈希不一致")
    packages = payload_identity.get("packages")
    if (
        not isinstance(packages, list)
        or len(packages) != 1
        or not isinstance(packages[0], dict)
    ):
        raise PipelineError("normal WAL 未冻结恰好一个正式 package")
    package = dict(packages[0])
    package_mode = str(package.get("mode") or "new")
    if package_mode != mode:
        raise PipelineError("normal WAL package mode 与正式结果不一致")
    if mode == "redo" and package.get("formal_id") != formal_id:
        raise PipelineError("normal WAL redo package formal_id 不一致")
    files = journal.get("files")
    if not isinstance(files, list) or not files:
        raise PipelineError("normal WAL 缺 files")
    for item in files:
        if not isinstance(item, dict) or item.get("published") is not True:
            raise PipelineError("normal WAL 含未发布目标")
        target = Path(str(item.get("target") or ""))
        expected = str(item.get("after_sha256") or "")
        if not target.is_absolute() or not target.is_file() or _sha256_file(target) != expected:
            raise PipelineError(f"normal receipt 目标 after hash 已变化：{target}")
    return CommittedReceiptProof(
        formal_id=formal_id,
        mode=mode,
        receipt_path=str(path),
        receipt_sha256=_sha256_file(path),
        package=package,
    )


def _postcommit_complete(specs: list[TaskSpec], results: list[TaskResult]) -> bool:
    required = {spec.key for spec in specs if spec.required}
    return all(result.passed for result in results if result.key in required) and {
        result.key for result in results if result.passed
    } >= required


def run_single_pipeline(
    package_path: str | Path,
    *,
    repo_root: str | Path = ".",
    details_root: str | Path = DETAILS_ROOT_DEFAULT,
    snapshot_path: str | Path = SNAPSHOT_PATH,
    idempotency_key: str | None = None,
    formal_lock_timeout: float = 30.0,
    pipeline_lock_timeout: float = 600.0,
    task_timeout: float = 180.0,
    max_workers: int = 7,
    do_http: bool = True,
    runner: TaskRunner = run_command,
) -> PipelineResult:
    repo_root = Path(repo_root).expanduser().resolve()
    details_root = Path(details_root).expanduser().resolve()
    snapshot_path = Path(snapshot_path).expanduser().resolve()
    package_path = Path(package_path).expanduser().resolve(strict=True)
    Repo(repo_root)
    frozen_hash, frozen_package = _package_snapshot(package_path)
    formal_id, mode = _package_identity(frozen_package)

    with PipelineLock(repo_root, pipeline_lock_timeout):
        current_hash, current_package = _package_snapshot(package_path)
        if current_hash != frozen_hash or current_package != frozen_package:
            raise PipelineError("package 在等待流水线栅栏期间发生变化，拒绝提交陈旧分析结果")

        try:
            ensure_receipt = _ensure_formal_backflow(
                repo_root, frozen_package
            )
        except (PipelineError, OSError) as exc:
            return PipelineResult(
                "FAILED",
                formal_id,
                mode,
                None,
                None,
                error=f"{exc}；正式 preflight/apply 均未启动",
            )

        if (
            mode == "new"
            and frozen_package.get("sol_decision_locator")
            and frozen_package.get("contract") == "coordinator_approved_intake_v1"
        ):
            # A new Sol-direct package deliberately keeps formal_id=null until
            # RepoLock allocation.  The legacy standalone preflight cannot
            # validate that state, so run the same writer in locked dry-run
            # mode: it allocates only in memory, verifies the sealed decision,
            # projects details, and performs the full preflight with zero writes.
            preflight_argv = (
                sys.executable,
                str(repo_root / "scripts" / "intake_apply_408.py"),
                str(package_path),
                "--repo",
                str(repo_root),
                "--details-root",
                str(details_root),
                "--lock-timeout",
                str(formal_lock_timeout),
                "--dry-run",
                "--json",
            )
        else:
            preflight_argv = (
                sys.executable,
                str(repo_root / "scripts" / "intake_preflight_408.py"),
                str(package_path),
                "--repo",
                str(repo_root),
                "--details-root",
                str(details_root),
            )
        preflight_spec = TaskSpec(
            "preflight", preflight_argv, timeout_seconds=task_timeout
        )
        preflight = _safe_run(runner, preflight_spec)
        if not preflight.passed:
            return PipelineResult(
                "FAILED", formal_id, mode, None, None,
                preflight=preflight, error="preflight 未通过，正式 apply 未启动",
                **_formal_backflow_fields(
                    ensure_receipt, status="scheduled_unbound"
                ),
            )

        apply_argv = [
            sys.executable, str(repo_root / "scripts" / "intake_apply_408.py"),
            str(package_path), "--repo", str(repo_root),
            "--details-root", str(details_root),
            "--lock-timeout", str(formal_lock_timeout), "--json",
        ]
        if idempotency_key:
            apply_argv.extend(["--idempotency-key", idempotency_key])
        apply_result = _safe_run(
            runner,
            TaskSpec("apply", tuple(apply_argv), timeout_seconds=task_timeout),
        )
        try:
            apply_payload, applied_formal_id = _parse_apply_payload(
                apply_result, formal_id
            )
        except PipelineError as exc:
            return PipelineResult(
                "FAILED", formal_id, mode, None, None,
                preflight=preflight, apply=apply_result, error=str(exc),
                formal_apply_count=1,
                **_formal_backflow_fields(
                    ensure_receipt, status="scheduled_unbound"
                ),
            )
        formal_id = applied_formal_id

        try:
            proof = verify_committed_receipt(
                str(apply_payload["receipt_path"]), repo_root
            )
        except (PipelineError, OSError) as exc:
            return PipelineResult(
                "FORMAL_COMMITTED_BACKFLOW_PENDING",
                formal_id,
                mode,
                str(apply_payload["status"]),
                str(apply_payload["receipt_path"]),
                preflight=preflight,
                apply=apply_result,
                error=(
                    f"正式 apply 已报告 COMMITTED，但 receipt/WAL 重开失败：{exc}；"
                    "禁止重放 apply"
                ),
                formal_apply_count=1,
                **_formal_backflow_fields(
                    ensure_receipt,
                    status="formal_committed_backflow_pending",
                ),
            )
        if proof.formal_id != formal_id or proof.mode != mode:
            return PipelineResult(
                "FORMAL_COMMITTED_BACKFLOW_PENDING",
                formal_id,
                mode,
                str(apply_payload["status"]),
                proof.receipt_path,
                preflight=preflight,
                apply=apply_result,
                error="正式 apply 输出与 receipt-bound formal identity 不一致；禁止重放 apply",
                formal_apply_count=1,
                **_formal_backflow_fields(
                    ensure_receipt,
                    status="formal_committed_backflow_pending",
                ),
            )

        try:
            final_receipt = _bind_formal_backflow(
                repo_root,
                frozen_package,
                ensure_receipt,
                formal_id=formal_id,
                formal_result_status="curated",
                formal_result_receipt_sha256=proof.receipt_sha256,
                formal_proof_path=proof.receipt_path,
            )
            if final_receipt.get("status") == "formal_committed_backflow_pending":
                return PipelineResult(
                    "FORMAL_COMMITTED_BACKFLOW_PENDING",
                    formal_id,
                    mode,
                    str(apply_payload["status"]),
                    proof.receipt_path,
                    preflight=preflight,
                    apply=apply_result,
                    error=(
                        "正式提交已完成，但 backflow writer 返回可恢复 pending；"
                        "禁止重放 apply"
                    ),
                    formal_apply_count=1,
                    **_formal_backflow_fields(
                        ensure_receipt,
                        final_receipt,
                        status="formal_committed_backflow_pending",
                    ),
                )
            final_receipt = _validate_complete_formal_backflow(
                repo_root, ensure_receipt, final_receipt
            )
        except PipelineError as exc:
            pending_receipt = _record_pending_formal_backflow(
                repo_root,
                frozen_package,
                ensure_receipt,
                formal_id=formal_id,
                formal_result_status="curated",
                formal_result_receipt_sha256=proof.receipt_sha256,
                formal_proof_path=proof.receipt_path,
                error_code="formal_binding_failed",
            )
            return PipelineResult(
                "FORMAL_COMMITTED_BACKFLOW_PENDING",
                formal_id,
                mode,
                str(apply_payload["status"]),
                proof.receipt_path,
                preflight=preflight,
                apply=apply_result,
                error=f"正式提交已完成但 formal binding 未完成：{exc}；禁止重放 apply",
                formal_apply_count=1,
                **_formal_backflow_fields(
                    ensure_receipt,
                    pending_receipt,
                    status="formal_committed_backflow_pending",
                ),
            )

        specs = build_postcommit_tasks(
            repo_root=repo_root,
            details_root=details_root,
            snapshot_path=snapshot_path,
            formal_id=formal_id,
            mode=mode,
            do_http=do_http,
            timeout_seconds=task_timeout,
        )
        tasks = run_task_graph(specs, max_workers=max_workers, runner=runner)
        complete = _postcommit_complete(specs, tasks)
        if not complete:
            return PipelineResult(
                "FORMAL_COMMITTED_BACKFLOW_PENDING",
                formal_id,
                mode,
                str(apply_payload["status"]),
                proof.receipt_path,
                preflight=preflight,
                apply=apply_result,
                tasks=tasks,
                error=(
                    "formal binding 已满足，但一个或多个 postcommit 必需分支未通过；"
                    "只能凭 receipt 重跑 closeout"
                ),
                formal_apply_count=1,
                **_formal_backflow_fields(ensure_receipt, final_receipt),
            )
        return PipelineResult(
            "COMPLETE",
            formal_id,
            mode,
            str(apply_payload["status"]),
            proof.receipt_path,
            preflight=preflight,
            apply=apply_result,
            tasks=tasks,
            formal_apply_count=1,
            **_formal_backflow_fields(ensure_receipt, final_receipt),
        )


def resume_closeout(
    receipt_path: str | Path,
    *,
    repo_root: str | Path = ".",
    details_root: str | Path = DETAILS_ROOT_DEFAULT,
    snapshot_path: str | Path = SNAPSHOT_PATH,
    pipeline_lock_timeout: float = 600.0,
    task_timeout: float = 180.0,
    max_workers: int = 7,
    do_http: bool = True,
    runner: TaskRunner = run_command,
) -> PipelineResult:
    repo_root = Path(repo_root).expanduser().resolve()
    details_root = Path(details_root).expanduser().resolve()
    snapshot_path = Path(snapshot_path).expanduser().resolve()
    Repo(repo_root)
    with PipelineLock(repo_root, pipeline_lock_timeout):
        proof = verify_committed_receipt(receipt_path, repo_root)
        formal_id, mode = proof.formal_id, proof.mode
        ensure_receipt = _ensure_formal_backflow(repo_root, proof.package)
        try:
            final_receipt = _bind_formal_backflow(
                repo_root,
                proof.package,
                ensure_receipt,
                formal_id=formal_id,
                formal_result_status="curated",
                formal_result_receipt_sha256=proof.receipt_sha256,
                formal_proof_path=proof.receipt_path,
            )
            if (
                final_receipt.get("status")
                == "formal_committed_backflow_pending"
            ):
                raise PipelineError(
                    "backflow writer 仍处于 formal_committed_backflow_pending"
                )
            final_receipt = _validate_complete_formal_backflow(
                repo_root, ensure_receipt, final_receipt
            )
        except PipelineError as exc:
            final_receipt = _record_pending_formal_backflow(
                repo_root,
                proof.package,
                ensure_receipt,
                formal_id=formal_id,
                formal_result_status="curated",
                formal_result_receipt_sha256=proof.receipt_sha256,
                formal_proof_path=proof.receipt_path,
                error_code="resume_binding_failed",
            )
            return PipelineResult(
                "FORMAL_COMMITTED_BACKFLOW_PENDING",
                formal_id,
                mode,
                "RECEIPT_BOUND_RESUME",
                proof.receipt_path,
                error=f"receipt-bound formal binding 仍未完成：{exc}",
                formal_apply_count=0,
                **_formal_backflow_fields(
                    ensure_receipt,
                    final_receipt,
                    status="formal_committed_backflow_pending",
                ),
            )
        specs = build_postcommit_tasks(
            repo_root=repo_root,
            details_root=details_root,
            snapshot_path=snapshot_path,
            formal_id=formal_id,
            mode=mode,
            do_http=do_http,
            timeout_seconds=task_timeout,
        )
        tasks = run_task_graph(specs, max_workers=max_workers, runner=runner)
        complete = _postcommit_complete(specs, tasks)
        error = (
            None
            if complete
            else "formal binding 已满足，但 receipt-bound closeout 仍有必需分支未通过；未重新 apply"
        )
        return PipelineResult(
            "COMPLETE" if complete else "FORMAL_COMMITTED_BACKFLOW_PENDING",
            formal_id,
            mode,
            "RECEIPT_BOUND_RESUME",
            proof.receipt_path,
            tasks=tasks,
            error=error,
            formal_apply_count=0,
            **_formal_backflow_fields(
                ensure_receipt,
                final_receipt,
            ),
        )


def _print_text(result: PipelineResult) -> None:
    print(f"单题并行流水线：{result.status}")
    if result.formal_id:
        print(f"正式节点：{result.formal_id}（mode={result.mode}）")
    if result.apply_status:
        print(f"正式事务：{result.apply_status}")
    if result.receipt_path:
        print(f"normal receipt：{result.receipt_path}")
    if result.review_schedule_ensure_receipt_sha256:
        print(
            "晨间回流 ensure receipt："
            f"{result.review_schedule_ensure_receipt_sha256}"
        )
        print(f"晨间回流状态：{result.backflow_status}")
    if result.formal_review_binding_receipt_sha256:
        print(
            "晨间回流 final binding receipt："
            f"{result.formal_review_binding_receipt_sha256}"
        )
    if result.preflight:
        print(f"preflight：{result.preflight.status}")
    if result.apply:
        print(f"apply：{result.apply.status}")
    for item in result.tasks:
        suffix = f"，blocked_by={','.join(item.blocked_by)}" if item.blocked_by else ""
        print(f"  - {item.key}：{item.status}（{item.duration_ms} ms{suffix}）")
    related = next((item.stdout.strip() for item in result.tasks if item.key == "related_candidates"), "")
    if related:
        print("\n7 天外强关联旧题回顾：")
        print(related)
    if result.error:
        print(f"[错误] {result.error}", file=sys.stderr)


def main() -> int:
    parser = argparse.ArgumentParser(description="408 单题并行预处理后的单写事务与并行 closeout 编排器")
    parser.add_argument("package", nargs="?", help="恰好一个 intake package JSON")
    parser.add_argument("--resume-receipt", help="已提交事务只重跑 closeout；不会重新 apply")
    parser.add_argument("--repo", default=".")
    parser.add_argument("--details-root", default=DETAILS_ROOT_DEFAULT)
    parser.add_argument("--snapshot", default=str(SNAPSHOT_PATH))
    parser.add_argument("--idempotency-key")
    parser.add_argument("--lock-timeout", type=float, default=30.0, help="正式 RepoLock 超时秒数")
    parser.add_argument("--pipeline-lock-timeout", type=float, default=600.0)
    parser.add_argument("--task-timeout", type=float, default=180.0)
    parser.add_argument("--workers", type=int, default=7)
    parser.add_argument("--no-http", action="store_true")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args()
    if bool(args.package) == bool(args.resume_receipt):
        parser.error("必须二选一：package 或 --resume-receipt")

    try:
        if args.resume_receipt:
            result = resume_closeout(
                args.resume_receipt,
                repo_root=args.repo,
                details_root=args.details_root,
                snapshot_path=args.snapshot,
                pipeline_lock_timeout=args.pipeline_lock_timeout,
                task_timeout=args.task_timeout,
                max_workers=args.workers,
                do_http=not args.no_http,
            )
        else:
            result = run_single_pipeline(
                args.package,
                repo_root=args.repo,
                details_root=args.details_root,
                snapshot_path=args.snapshot,
                idempotency_key=args.idempotency_key,
                formal_lock_timeout=args.lock_timeout,
                pipeline_lock_timeout=args.pipeline_lock_timeout,
                task_timeout=args.task_timeout,
                max_workers=args.workers,
                do_http=not args.no_http,
            )
    except (PipelineError, BatchError, OSError) as exc:
        if args.json:
            print(json.dumps({"status": "FAILED", "error": str(exc)}, ensure_ascii=False, indent=2))
        else:
            print(f"[错误] {exc}", file=sys.stderr)
        return 1

    if args.json:
        print(json.dumps(result.as_dict(), ensure_ascii=False, indent=2))
    else:
        _print_text(result)
    return 0 if result.complete else 1


if __name__ == "__main__":
    raise SystemExit(main())
