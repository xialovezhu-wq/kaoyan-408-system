#!/usr/bin/env python3
"""408 receipt-bound postcommit 派生项修复。

本工具只处理已经由 normal batch receipt 证明正式提交成功、且 queue 正处于
``VALIDATING`` 的 new job。它不会重跑 apply，不会修改节点、关系、日期、redo
记录或 details；唯一允许的修复面是：

* ``知识点命中索引.md`` 中 approved 包明确列出的副命中；
* review-unit-v2 的总表、映射、原题轨和缺失的对应 RU 卡。

首次修复前必须由 ``QueueStore.verify_validating_batch_receipts`` 重新绑定原 normal
receipt、manifest、approved 包、WAL 和当前正式文件 after hashes。修复本身使用新的
WAL 事务和独立稳定幂等键；幂等身份不包含 actor / coordinator generation。
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import re
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable

sys.path.insert(0, str(Path(__file__).resolve().parent))

from intake_apply_408 import Txn, ensure_review_unit_v2_for_new, hit_index_add  # noqa: E402
from intake_batch_apply_408 import (  # noqa: E402
    DEFAULT_RUNTIME_ROOT,
    BatchError,
    BatchValidationError,
    LockTimeout,
    _canonical_bytes,
    _commit_transaction,
    _assert_dry_run_wal_clean,
    _read_json_object,
    _receipt_path,
    _repo_key,
    _sha256_bytes,
    _sha256_file,
    recover_transactions,
)
from intake_lib_408 import (  # noqa: E402
    FORMAL_ID_RE,
    HIT_INDEX_FILE,
    ORIGINAL_TRACK_FILE,
    REVIEW_UNIT_CARD_DIR,
    REVIEW_UNIT_FILE,
    REVIEW_UNIT_MAPPING_FILE,
    SUBJECT_FULL,
    Repo,
    normalize_tag,
    split_multi,
)
from intake_lock_408 import RepoLock  # noqa: E402
from intake_preflight_408 import DETAILS_ROOT_DEFAULT  # noqa: E402
from intake_queue_408 import QueueStore, ValidationError  # noqa: E402


REPAIR_CONTRACT = "receipt_bound_postcommit_repair_v1"
REPAIR_MODE = "postcommit_repair"
SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
STATIC_ALLOWED_RELS = {
    HIT_INDEX_FILE,
    REVIEW_UNIT_FILE,
    REVIEW_UNIT_MAPPING_FILE,
    ORIGINAL_TRACK_FILE,
}


@dataclass
class RepairResult:
    status: str
    idempotency_key: str
    transaction_id: str | None
    items: list[dict[str, str]]
    receipt_path: str | None = None
    changed_files: list[str] = field(default_factory=list)
    logs: list[str] = field(default_factory=list)
    recovered: list[dict[str, str]] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return {
            "status": self.status,
            "idempotency_key": self.idempotency_key,
            "transaction_id": self.transaction_id,
            "items": self.items,
            "receipt_path": self.receipt_path,
            "changed_files": self.changed_files,
            "logs": self.logs,
            "recovered": self.recovered,
        }


def _required_text(value: Any, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise BatchValidationError(f"{field} 必须是非空字符串")
    return value.strip()


def _required_sha(value: Any, field: str) -> str:
    text = _required_text(value, field).lower()
    if not SHA256_RE.fullmatch(text):
        raise BatchValidationError(f"{field} 必须是 64 位小写 SHA-256")
    return text


def _canonical_existing_file(
    value: Any,
    *,
    field: str,
    parent: Path,
    expected_sha: str | None = None,
) -> Path:
    raw = Path(_required_text(value, field)).expanduser()
    if not raw.is_absolute() or raw.is_symlink():
        raise BatchValidationError(f"{field} 必须是非符号链接绝对路径")
    try:
        path = raw.resolve(strict=True)
        path.relative_to(parent.resolve(strict=True))
    except FileNotFoundError as exc:
        raise BatchValidationError(f"{field} 文件不存在：{raw}") from exc
    except ValueError as exc:
        raise BatchValidationError(f"{field} 必须位于 {parent.resolve()} 下") from exc
    if not path.is_file():
        raise BatchValidationError(f"{field} 不是普通文件：{path}")
    if expected_sha is not None and _sha256_file(path) != expected_sha:
        raise BatchValidationError(f"{field} SHA-256 不匹配")
    return path


def _normalize_job_ids(job_ids: Iterable[str]) -> list[str]:
    requested = [str(value).strip() for value in job_ids]
    if not requested or any(not value for value in requested):
        raise BatchValidationError("至少需要一个非空 --job")
    if len(requested) != len(set(requested)):
        raise BatchValidationError("--job 不得重复")
    return sorted(requested)


def _job_binding(job: dict[str, Any], runtime_root: Path) -> dict[str, str]:
    job_id = _required_text(job.get("job_id"), "job.job_id")
    formal_id = _required_text(job.get("formal_id"), f"{job_id}.formal_id")
    if not FORMAL_ID_RE.fullmatch(formal_id):
        raise BatchValidationError(f"{job_id}.formal_id 非法：{formal_id}")
    approved_sha = _required_sha(
        job.get("approved_package_sha256"), f"{job_id}.approved_package_sha256"
    )
    approved_path = _canonical_existing_file(
        job.get("approved_package_path"),
        field=f"{job_id}.approved_package_path",
        parent=runtime_root / "approved",
        expected_sha=approved_sha,
    )
    receipt_sha = _required_sha(
        job.get("batch_receipt_sha256"), f"{job_id}.batch_receipt_sha256"
    )
    receipt_path = _canonical_existing_file(
        job.get("batch_receipt_path"),
        field=f"{job_id}.batch_receipt_path",
        parent=runtime_root / "receipts",
        expected_sha=receipt_sha,
    )
    return {
        "job_id": job_id,
        "formal_id": formal_id,
        "approved_package_path": str(approved_path),
        "approved_package_sha256": approved_sha,
        "batch_receipt_path": str(receipt_path),
        "batch_receipt_sha256": receipt_sha,
        "commit_batch_id": _required_text(
            job.get("commit_batch_id"), f"{job_id}.commit_batch_id"
        ),
    }


def _original_receipt_identity(bindings: list[dict[str, str]]) -> dict[str, str]:
    identities = {
        (item["batch_receipt_path"], item["batch_receipt_sha256"], item["commit_batch_id"])
        for item in bindings
    }
    if len(identities) != 1:
        raise BatchValidationError("postcommit repair 的 jobs 必须绑定同一 normal receipt")
    receipt_path_text, receipt_sha, batch_id = next(iter(identities))
    receipt = _read_json_object(Path(receipt_path_text))
    if receipt.get("schema") != "intake_batch_receipt_v1" or receipt.get("status") != "COMMITTED":
        raise BatchValidationError("原 batch receipt schema/status 非法")
    transaction_id = _required_text(receipt.get("transaction_id"), "original receipt.transaction_id")
    if receipt.get("commit_batch_id") != batch_id:
        raise BatchValidationError("原 batch receipt 与 job commit_batch_id 不一致")
    return {
        "path": receipt_path_text,
        "sha256": receipt_sha,
        "transaction_id": transaction_id,
        "commit_batch_id": batch_id,
    }


def _payload_identity(
    repo_root: Path,
    details_root: Path,
    bindings: list[dict[str, str]],
    original_receipt: dict[str, str],
) -> dict[str, Any]:
    """构造不含 actor/generation 的稳定修复身份。"""
    return {
        "contract": REPAIR_CONTRACT,
        "repo_root": str(repo_root),
        "details_root": str(details_root),
        "original_batch_receipt": original_receipt,
        "jobs": bindings,
        "operations": [
            "add_missing_approved_secondary_hits_without_double_count",
            "repair_review_unit_v2_surfaces_for_new",
        ],
    }


def _idempotency_key(payload_identity: dict[str, Any]) -> str:
    digest = _sha256_bytes(_canonical_bytes(payload_identity))
    return f"postcommit-repair-v1:sha256:{digest}"


def _repair_manifest_identity(
    payload_identity: dict[str, Any], payload_sha256: str
) -> dict[str, Any]:
    return {
        "contract": REPAIR_CONTRACT,
        "payload_sha256": payload_sha256,
        "original_batch_receipt": payload_identity["original_batch_receipt"],
        "jobs": payload_identity["jobs"],
    }


def _repair_batch_id(idempotency_key: str) -> str:
    return "POSTCOMMIT-REPAIR-" + idempotency_key.rsplit(":", 1)[-1][:24]


def _allowed_rel_for_target(target: Path, repo_root: Path, formal_ids: set[str]) -> str:
    try:
        rel = str(target.relative_to(repo_root))
    except ValueError as exc:
        raise BatchValidationError(f"postcommit repair 目标越过正式仓库：{target}") from exc
    if rel in STATIC_ALLOWED_RELS:
        return rel
    allowed_cards = {f"{REVIEW_UNIT_CARD_DIR}/RU_{formal_id}.md" for formal_id in formal_ids}
    if rel in allowed_cards:
        return rel
    raise BatchValidationError(f"postcommit repair 非法写目标：{rel}")


def _validate_repair_receipt(
    receipt_path: Path,
    *,
    repo_root: Path,
    details_root: Path,
    runtime_root: Path,
    idempotency_key: str,
    payload_identity: dict[str, Any],
    payload_sha256: str,
    manifest_identity: dict[str, Any],
    result_items: list[dict[str, str]],
) -> dict[str, Any]:
    """完整验证自身 receipt/WAL 身份和所有当前 after hashes。"""
    expected_path = _receipt_path(runtime_root, repo_root, idempotency_key)
    if receipt_path != expected_path or receipt_path.is_symlink():
        raise BatchValidationError("repair receipt 路径不在稳定幂等命名空间")
    receipt = _read_json_object(receipt_path)
    if receipt.get("schema") != "intake_batch_receipt_v1" or receipt.get("status") != "COMMITTED":
        raise BatchValidationError("repair receipt schema/status 非法")
    if receipt.get("idempotency_key") != idempotency_key:
        raise BatchValidationError("repair receipt idempotency_key 不一致")
    if receipt.get("payload_sha256") != payload_sha256:
        raise BatchValidationError("repair receipt payload_sha256 不一致")
    if receipt.get("manifest") != manifest_identity:
        raise BatchValidationError("repair receipt manifest identity 不一致")
    if receipt.get("items") != result_items:
        raise BatchValidationError("repair receipt items 与请求 jobs 不一致")
    txid = _required_text(receipt.get("transaction_id"), "repair receipt.transaction_id")
    journal_raw = Path(_required_text(receipt.get("journal"), "repair receipt.journal")).expanduser()
    if not journal_raw.is_absolute() or journal_raw.is_symlink():
        raise BatchValidationError("repair receipt.journal 必须是非符号链接绝对路径")
    journal_path = journal_raw.resolve(strict=True)
    expected_tx_root = runtime_root / "transactions" / _repo_key(repo_root) / txid
    if journal_path != expected_tx_root / "journal.json":
        raise BatchValidationError("repair receipt journal 路径与 repo namespace/transaction 不一致")
    journal = _read_json_object(journal_path)
    if journal.get("schema") != "intake_batch_wal_v1" or journal.get("state") != "committed":
        raise BatchValidationError("repair WAL schema/state 非法")
    for field in (
        "transaction_id", "idempotency_key", "payload_sha256", "repo_root",
        "actor", "coordinator_generation", "commit_batch_id", "manifest",
    ):
        if receipt.get(field) != journal.get(field):
            raise BatchValidationError(f"repair receipt/WAL {field} 不一致")
    if journal.get("transaction_id") != txid:
        raise BatchValidationError("repair WAL transaction_id 不一致")
    if journal.get("repo_root") != str(repo_root) or journal.get("details_root") != str(details_root):
        raise BatchValidationError("repair WAL formal/details root 不一致")
    _required_text(journal.get("actor"), "repair WAL.actor")
    repair_generation = journal.get("coordinator_generation")
    if (
        isinstance(repair_generation, bool)
        or not isinstance(repair_generation, int)
        or repair_generation < 0
    ):
        raise BatchValidationError("repair WAL.coordinator_generation 非法")
    if journal.get("commit_batch_id") != _repair_batch_id(idempotency_key):
        raise BatchValidationError("repair WAL deterministic commit_batch_id 不一致")
    if set(journal.get("allowed_roots") or []) != {str(repo_root)}:
        raise BatchValidationError("repair WAL allowed_roots 必须仅含正式 repo")
    if journal.get("payload_identity") != payload_identity:
        raise BatchValidationError("repair WAL payload_identity 不一致")
    if _sha256_bytes(_canonical_bytes(journal["payload_identity"])) != payload_sha256:
        raise BatchValidationError("repair WAL payload_identity 无法重现 payload_sha256")
    if journal.get("queue_claims") not in ([], None) or receipt.get("queue_claims") not in ([], None):
        raise BatchValidationError("repair WAL 不得伪造 queue claim")
    if journal.get("result_items") != result_items:
        raise BatchValidationError("repair WAL result_items 不一致")
    if receipt.get("recovery") != journal.get("recovery"):
        raise BatchValidationError("repair receipt/WAL recovery 身份不一致")
    recovery = journal.get("recovery")
    if recovery is not None and (
        not isinstance(recovery, dict)
        or recovery.get("action") != "roll_forward"
        or not str(recovery.get("recovered_at") or "").strip()
    ):
        raise BatchValidationError("repair committed WAL 只允许 normal 或 roll_forward recovery")

    original = payload_identity["original_batch_receipt"]
    original_path = _canonical_existing_file(
        original["path"],
        field="repair original_batch_receipt.path",
        parent=runtime_root / "receipts",
        expected_sha=original["sha256"],
    )
    original_value = _read_json_object(original_path)
    if original_value.get("transaction_id") != original["transaction_id"]:
        raise BatchValidationError("repair receipt 绑定的 original transaction 已改变")

    formal_ids = {item["formal_id"] for item in result_items}
    files = journal.get("files")
    if not isinstance(files, list):
        raise BatchValidationError("repair WAL files 必须是数组")
    changed_files = receipt.get("changed_files")
    expected_changed = [item.get("target") for item in files]
    if changed_files != expected_changed:
        raise BatchValidationError("repair receipt changed_files 与 WAL 不一致")
    if len(expected_changed) != len(set(expected_changed)):
        raise BatchValidationError("repair WAL targets 不得重复")
    for item in files:
        if not isinstance(item, dict):
            raise BatchValidationError("repair WAL file item 非 object")
        if item.get("published") is not True:
            raise BatchValidationError("repair committed WAL file 必须标记 published=true")
        before_sha = item.get("before_sha256")
        if before_sha is not None:
            _required_sha(before_sha, "repair WAL file.before_sha256")
        target_raw = Path(_required_text(item.get("target"), "repair WAL file.target"))
        if not target_raw.is_absolute() or target_raw.is_symlink():
            raise BatchValidationError("repair WAL target 必须为非符号链接绝对路径")
        # target 可能是本事务创建的 RU 卡；strict resolve 只在文件存在时执行。
        target = target_raw.resolve(strict=True)
        _allowed_rel_for_target(target, repo_root, formal_ids)
        after_sha = _required_sha(item.get("after_sha256"), "repair WAL file.after_sha256")
        if _sha256_file(target) != after_sha:
            raise BatchValidationError(f"repair WAL after_sha256 不匹配：{target}")
    return receipt


def _load_approved_package(
    binding: dict[str, str], repo: Repo
) -> tuple[dict[str, Any], dict[str, str]]:
    package = _read_json_object(Path(binding["approved_package_path"]))
    job_id = binding["job_id"]
    formal_id = binding["formal_id"]
    if package.get("contract") != "coordinator_approved_intake_v1":
        raise BatchValidationError(f"{job_id} approved contract 非法")
    if package.get("job_id") != job_id or package.get("decision") != "pass":
        raise BatchValidationError(f"{job_id} approved 身份/decision 不一致")
    if package.get("mode") != "new":
        raise BatchValidationError(f"{job_id} postcommit repair 只允许原 new 包")
    package_formal = str(package.get("formal_id") or "").strip()
    if package_formal and package_formal != formal_id:
        raise BatchValidationError(f"{job_id} approved formal_id 与 receipt 不一致")

    master = repo.master()
    if formal_id not in master:
        raise BatchValidationError(f"{job_id} formal_id 不在节点总表：{formal_id}")
    row = master[formal_id]
    subject = _required_text(package.get("subject"), f"{job_id}.approved.subject")
    if subject not in SUBJECT_FULL:
        raise BatchValidationError(f"{job_id} approved.subject 非法：{subject}")
    year = "未标明" if str(package.get("year")) == "UNK" else str(package.get("year"))
    expected = {
        "来源ID": str(package.get("source_id") or ""),
        "年份": year,
        "科目": SUBJECT_FULL[subject],
        "主模块": str(package.get("module") or ""),
        "主知识点": str(package.get("main_knowledge") or ""),
        "核心考点": str(package.get("core_point") or ""),
    }
    mismatches = [
        f"{column}: master={row.get(column)!r} approved={value!r}"
        for column, value in expected.items()
        if row.get(column) != value
    ]
    if mismatches:
        raise BatchValidationError(
            f"{job_id} approved 与节点总表不一致，拒绝派生修复：" + "; ".join(mismatches)
        )
    approved_hits = list(package.get("hit_knowledge") or [])
    main = _required_text(package.get("main_knowledge"), f"{job_id}.main_knowledge")
    if normalize_tag(main)[0] not in {normalize_tag(str(item))[0] for item in approved_hits}:
        approved_hits.insert(0, main)
    approved_codes = {normalize_tag(str(item))[0] for item in approved_hits}
    master_codes = {normalize_tag(item)[0] for item in split_multi(row.get("命中知识点", ""))}
    if approved_codes != master_codes:
        raise BatchValidationError(
            f"{job_id} approved hit_knowledge 与节点总表不一致："
            f"approved={sorted(approved_codes)} master={sorted(master_codes)}"
        )
    package = dict(package)
    package["formal_id"] = formal_id
    package["hit_knowledge"] = approved_hits
    return package, row


def _committed_date(receipt: dict[str, Any]) -> dt.date:
    raw = _required_text(receipt.get("committed_at"), "original receipt.committed_at")
    try:
        return dt.date.fromisoformat(raw[:10])
    except ValueError as exc:
        raise BatchValidationError("original receipt.committed_at 不是可解析日期") from exc


def repair_postcommit_batch(
    job_ids: Iterable[str],
    *,
    repo_root: str | Path = ".",
    details_root: str | Path = DETAILS_ROOT_DEFAULT,
    runtime_root: str | Path = DEFAULT_RUNTIME_ROOT,
    actor: str,
    coordinator_generation: int,
    dry_run: bool = False,
    lock_timeout: float = 30.0,
    _crash_after: int | None = None,
    _fail_after: int | None = None,
) -> RepairResult:
    requested = _normalize_job_ids(job_ids)
    repo_root = Path(repo_root).expanduser().resolve()
    details_root = Path(details_root).expanduser().resolve()
    runtime_root = Path(runtime_root).expanduser().resolve()
    actor = _required_text(actor, "actor")
    if isinstance(coordinator_generation, bool) or not isinstance(coordinator_generation, int):
        raise BatchValidationError("coordinator_generation 必须是整数")
    if coordinator_generation < 0:
        raise BatchValidationError("coordinator_generation 不能为负数")
    if (repo_root / ".git").exists() and runtime_root != DEFAULT_RUNTIME_ROOT:
        raise BatchValidationError(
            f"生产 Git 仓库的 runtime_root 固定为 {DEFAULT_RUNTIME_ROOT}，拒绝切换到 {runtime_root}"
        )
    repo = Repo(repo_root)
    store = QueueStore(
        runtime_root,
        details_root,
        formal_repo_root=repo_root,
        lock_timeout=lock_timeout,
    )

    with RepoLock(repo_root, runtime_root, lock_timeout, timeout_error=LockTimeout):
        # dry-run 必须纯只读：脏 WAL 只报告不恢复；正式运行才先完成 WAL recovery。
        if dry_run:
            _assert_dry_run_wal_clean(repo_root, runtime_root)
            recovered: list[dict[str, str]] = []
        else:
            recovered = recover_transactions(repo_root, runtime_root)
        snapshot_jobs = [store.status(job_id) for job_id in requested]
        snapshot_bindings = [_job_binding(job, runtime_root) for job in snapshot_jobs]
        original_identity = _original_receipt_identity(snapshot_bindings)
        payload_identity = _payload_identity(
            repo_root, details_root, snapshot_bindings, original_identity
        )
        payload_sha = _sha256_bytes(_canonical_bytes(payload_identity))
        idem = _idempotency_key(payload_identity)
        items = [
            {
                "job_id": binding["job_id"],
                "formal_id": binding["formal_id"],
                "mode": REPAIR_MODE,
            }
            for binding in snapshot_bindings
        ]
        manifest_identity = _repair_manifest_identity(payload_identity, payload_sha)
        repair_receipt_path = _receipt_path(runtime_root, repo_root, idem)
        if repair_receipt_path.exists():
            receipt = _validate_repair_receipt(
                repair_receipt_path,
                repo_root=repo_root,
                details_root=details_root,
                runtime_root=runtime_root,
                idempotency_key=idem,
                payload_identity=payload_identity,
                payload_sha256=payload_sha,
                manifest_identity=manifest_identity,
                result_items=items,
            )
            return RepairResult(
                status="ALREADY_REPAIRED",
                idempotency_key=idem,
                transaction_id=str(receipt["transaction_id"]),
                items=items,
                receipt_path=str(repair_receipt_path),
                changed_files=list(receipt.get("changed_files") or []),
                logs=["repair receipt/WAL/after hashes 完整校验通过；未重复修改"],
                recovered=recovered,
            )

        # 首次修复必须由 queue 在同一锁内重新证明原 receipt 和所有原 after hashes。
        verified = store.verify_validating_batch_receipts(
            requested, expected_generation=coordinator_generation
        )
        verified_jobs = sorted(verified["jobs"], key=lambda item: item["job_id"])
        bindings = [_job_binding(job, runtime_root) for job in verified_jobs]
        verified_original = _original_receipt_identity(bindings)
        verified_payload = _payload_identity(repo_root, details_root, bindings, verified_original)
        if verified_payload != payload_identity:
            raise BatchValidationError("queue job 身份在 repair 领取期间发生变化，请重新运行")
        original_receipt = verified["receipt"]

        txn = Txn(repo)
        _, leaves = repo.kp_tags()
        as_of = _committed_date(original_receipt)
        for binding in bindings:
            package, master_row = _load_approved_package(binding, repo)
            formal_id = binding["formal_id"]
            main_code = normalize_tag(package["main_knowledge"])[0]
            for hit in package["hit_knowledge"]:
                code, _ = normalize_tag(str(hit))
                if code == main_code:
                    continue
                if code not in leaves:
                    raise BatchValidationError(
                        f"{binding['job_id']} approved 副命中编码不在标签表：{code}"
                    )
                hit_index_add(
                    txn,
                    formal_id,
                    code,
                    leaves[code],
                    False,
                    master_row["核心考点"],
                    master_row["最近复做日期"],
                    master_row["错因标签"],
                    main_code,
                )
            ensure_review_unit_v2_for_new(
                txn,
                package,
                formal_id,
                detail_entry=master_row["详情入口"],
                repair_existing=True,
                as_of=as_of,
            )

        rendered = txn.rendered_files()
        formal_ids = {binding["formal_id"] for binding in bindings}
        target_files: dict[Path, bytes] = {}
        for rel, data in rendered.items():
            target = (repo_root / rel).resolve()
            _allowed_rel_for_target(target, repo_root, formal_ids)
            # 不把纯读取产生的同字节文件写入 repair WAL。
            if target.exists() and target.read_bytes() == data:
                continue
            target_files[target] = data
        expected_before = {
            target: (_sha256_file(target) if target.exists() else None)
            for target in target_files
        }
        logs = [*txn.log]
        if dry_run:
            return RepairResult(
                status="DRY_RUN",
                idempotency_key=idem,
                transaction_id=None,
                items=items,
                changed_files=[str(path) for path in sorted(target_files, key=str)],
                logs=logs,
                recovered=recovered,
            )

        repair_batch_id = _repair_batch_id(idem)
        txid, receipt_path, changed = _commit_transaction(
            repo_root,
            runtime_root,
            target_files,
            idem,
            payload_sha,
            items,
            [repo_root],
            actor,
            coordinator_generation,
            [],
            repair_batch_id,
            manifest_identity,
            payload_identity,
            details_root,
            crash_after=_crash_after,
            fail_after=_fail_after,
            expected_before_hashes=expected_before,
        )
        _validate_repair_receipt(
            receipt_path,
            repo_root=repo_root,
            details_root=details_root,
            runtime_root=runtime_root,
            idempotency_key=idem,
            payload_identity=payload_identity,
            payload_sha256=payload_sha,
            manifest_identity=manifest_identity,
            result_items=items,
        )
        return RepairResult(
            status="REPAIRED",
            idempotency_key=idem,
            transaction_id=txid,
            items=items,
            receipt_path=str(receipt_path),
            changed_files=changed,
            logs=logs,
            recovered=recovered,
        )


def _print_result(result: RepairResult, json_output: bool) -> None:
    if json_output:
        print(json.dumps(result.as_dict(), ensure_ascii=False, indent=2))
        return
    print(f"postcommit repair：{result.status}")
    print(f"idempotency_key：{result.idempotency_key}")
    if result.transaction_id:
        print(f"transaction_id：{result.transaction_id}")
    for item in result.items:
        print(f"  - {item['job_id']} → {item['formal_id']}")
    if result.receipt_path:
        print(f"receipt：{result.receipt_path}")


def main() -> int:
    parser = argparse.ArgumentParser(description="408 receipt-bound postcommit 派生项修复")
    parser.add_argument("--job", action="append", required=True, help="可重复指定 VALIDATING job")
    parser.add_argument("--repo", default=".")
    parser.add_argument("--details-root", default=DETAILS_ROOT_DEFAULT)
    parser.add_argument("--runtime-root", default=str(DEFAULT_RUNTIME_ROOT))
    parser.add_argument("--actor", required=True)
    parser.add_argument("--coordinator-generation", required=True, type=int)
    parser.add_argument("--lock-timeout", type=float, default=30.0)
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args()
    try:
        result = repair_postcommit_batch(
            args.job,
            repo_root=args.repo,
            details_root=args.details_root,
            runtime_root=args.runtime_root,
            actor=args.actor,
            coordinator_generation=args.coordinator_generation,
            dry_run=args.dry_run,
            lock_timeout=args.lock_timeout,
        )
        _print_result(result, args.json)
        return 0
    except (BatchError, ValidationError, OSError, ValueError) as exc:
        if args.json:
            print(json.dumps({"status": "ERROR", "error": str(exc)}, ensure_ascii=False, indent=2))
        else:
            print(f"[错误] {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
