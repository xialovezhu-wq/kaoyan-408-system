#!/usr/bin/env python3
"""Project and transactionally publish a full-library error-evidence review.

The command is dry-run by default.  ``--apply`` publishes only current node
metadata, subject mirrors, knowledge-hit error records, and root safe cards.
It never writes the details vault, review logs, or ``复盘输出/``.
"""

from __future__ import annotations

import argparse
import copy
import json
import re
import sys
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))

from apply_relation_semantic_review import (  # noqa: E402
    RELATION_LABELS,
    SUBJECT_PREFIXES,
    SUBJECT_VIEWS,
    PublishError,
    _capture_before_hashes,
    _ensure_card_sections,
    _existing_bullet_section,
    _project_hit_index,
    _render_table,
    _run_shadow_gates,
    _upsert_bullet,
)
from intake_batch_apply_408 import (  # noqa: E402
    DEFAULT_RUNTIME_ROOT,
    BatchError,
    TransactionRolledBack,
    _assert_dry_run_wal_clean,
    _canonical_bytes,
    _commit_transaction,
    _read_json_object,
    _receipt_path,
    _result_from_receipt,
    _sha256_bytes,
    recover_transactions,
)
from intake_lock_408 import RepoLock, RepoLockTimeout  # noqa: E402
from normalize_408_knowledge_points import read_table_with_preamble  # noqa: E402
from question_evidence_review import (  # noqa: E402
    GENERIC_ACTION_FRAGMENTS,
    GENERIC_ERROR_FRAGMENTS,
    NO_ERROR_TEXT,
    _load_records,
    _load_edge_records,
    _protected_hits,
    validate as validate_evidence,
)
from relation_semantic_review import (  # noqa: E402
    _check_snapshot_hashes,
    pair_key,
    sha256_file,
)


MODE = "question_evidence_review"
NEUTRAL_RECENT = (
    "历史归档仅有泛化错因标签，未找到题目特异用户错误；"
    "当前按暂无明确错因处理。"
)
NEEDS_ERROR_TEXT = "错因归属或题目身份待确认；当前不把候选错误记录绑定为本节点事实。"
NEEDS_RECENT = "错因归属或题目身份待确认；现有候选记录暂不作为本节点正式错误事实。"
GENERIC_RECENT_PATTERNS = (
    "历史归档导入：有错因描述",
    "历史归档导入：错因待复核",
    "历史归档错因待复核",
    "当前历史归档标记为错因待复核",
    "错因仍待复核",
    "错因缺少直接证据",
    "详情图复核：",
    "题图复核：",
    "MarginNote 复核：",
    "MarginNote 右侧搜索已复核：",
    "详情库看图校准：",
)
SPECIFIC_ERROR_RE = re.compile(
    r"(?:曾把|误把|把.{0,48}(?:当成|当作|误判)|混淆|遗漏|漏掉|不会|"
    r"没思路|不知|答错|算错|误选|蒙对)"
)


def _load_json(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise PublishError(f"JSON object required: {path}")
    return value


def _review_files(paths: list[Path]) -> list[Path]:
    files = sorted({path.expanduser().resolve() for path in paths})
    if not files or any(not path.is_file() or path.suffix != ".jsonl" for path in files):
        raise PublishError("--reviews must name existing JSONL files")
    return files


def _optional_review_files(paths: list[Path]) -> list[Path]:
    if not paths:
        return []
    return _review_files(paths)


def _snapshot_live(
    snapshot: dict[str, Any], repo: Path, details_root: Path
) -> None:
    if Path(str(snapshot.get("repo_root", ""))).resolve() != repo:
        raise PublishError("snapshot repo_root mismatch")
    if Path(str(snapshot.get("details_root", ""))).resolve() != details_root:
        raise PublishError("snapshot details_root mismatch")
    nodes = snapshot.get("nodes")
    edges = snapshot.get("edges")
    if not isinstance(nodes, list) or not nodes:
        raise PublishError("snapshot must contain at least one node")
    if not isinstance(edges, list):
        raise PublishError("snapshot edges missing")
    errors: list[str] = []
    _check_snapshot_hashes(snapshot, repo, details_root, errors)
    if errors:
        raise PublishError("snapshot evidence drift:\n- " + "\n- ".join(errors[:80]))

    _, _, rows = read_table_with_preamble(repo / "节点总表.md")
    live_ids = {row.get("ID", "") for row in rows}
    snap_ids = {str(node.get("id", "")) for node in nodes if isinstance(node, dict)}
    if live_ids != snap_ids or len(live_ids) != len(nodes):
        raise PublishError("snapshot node set differs from live formal table")
    _, _, edge_rows = read_table_with_preamble(repo / "关系边表.md")
    live_pairs = {pair_key(row["起点ID"], row["终点ID"]) for row in edge_rows}
    snap_pairs = {
        pair_key(str(edge.get("a", "")), str(edge.get("b", "")))
        for edge in edges if isinstance(edge, dict)
    }
    if live_pairs != snap_pairs or len(edge_rows) != len(edges):
        raise PublishError("snapshot edge set differs from live relation table")


def _payload_identity(
    snapshot: Path,
    reviews: list[Path],
    edge_reviews: list[Path],
    review_date: str,
) -> dict[str, Any]:
    return {
        "mode": MODE,
        "snapshot": {"path": str(snapshot), "sha256": sha256_file(snapshot)},
        "reviews": [
            {"path": str(path), "sha256": sha256_file(path)} for path in reviews
        ],
        "edge_reviews": [
            {"path": str(path), "sha256": sha256_file(path)} for path in edge_reviews
        ],
        "review_date": review_date,
    }


def _labels(record: dict[str, Any]) -> str:
    labels = [str(value).strip() for value in record.get("error_labels", []) if str(value).strip()]
    return "；".join(labels) if labels else "暂无明确错因"


def _needs_action(record: dict[str, Any]) -> str:
    combined = " ".join(
        str(record.get(field, "")) for field in ("user_error_entry", "notes")
    )
    if record.get("evidence_grade") == "D":
        return "先补齐并核对同题题图与解析；证据确认前不绑定错因、不安排机制连边。"
    if any(marker in combined for marker in ("重复", "完全相同", "同一题")):
        return "先确认重复节点的保留身份并合并来源与复做记录；去重完成前不重复归因或安排复做。"
    return "先核对题图年份、正式 ID 与来源链的一致性；身份确认前不绑定错因或发布关系。"


def _is_generic_recent(value: str) -> bool:
    value = value.strip()
    return not value or value in {"未记录", "暂无明确错因", "待补充", "待确认"} or any(
        pattern in value for pattern in GENERIC_RECENT_PATTERNS
    )


def _has_specific_recent_error(value: str) -> bool:
    if any(pattern in value for pattern in GENERIC_RECENT_PATTERNS):
        return False
    return bool(SPECIFIC_ERROR_RE.search(value))


def _project(
    repo: Path,
    records: dict[str, dict[str, Any]],
    edge_records: list[dict[str, Any]],
    review_date: str,
) -> tuple[dict[Path, bytes], dict[str, Any]]:
    preamble, headers, rows = read_table_with_preamble(repo / "节点总表.md")
    before_rows = {row["ID"]: copy.deepcopy(row) for row in rows}
    by_id = {row["ID"]: row for row in rows}
    changed_error_ids: set[str] = set()
    stats = {
        "labels_changed": 0,
        "generic_recent_neutralized": 0,
        "needs_user_recent_neutralized": 0,
        "cards_changed": 0,
        "edges_changed": 0,
        "edges_removed": 0,
        "edges_added": 0,
    }

    for node_id, record in sorted(records.items()):
        row = by_id[node_id]
        label_text = _labels(record)
        if row.get("错因标签", "") != label_text:
            row["错因标签"] = label_text
            stats["labels_changed"] += 1
            changed_error_ids.add(node_id)
        if record["user_error_status"] == "needs_user":
            if row.get("最近错误记录", "") != NEEDS_RECENT:
                row["最近错误记录"] = NEEDS_RECENT
                stats["needs_user_recent_neutralized"] += 1
            continue
        if record["user_error_status"] == "none_recorded":
            current = row.get("最近错误记录", "")
            if _has_specific_recent_error(current):
                raise PublishError(
                    f"{node_id}: ledger says none_recorded but formal table has a specific recent error"
                )
            if _is_generic_recent(current) and current != NEUTRAL_RECENT:
                row["最近错误记录"] = NEUTRAL_RECENT
                stats["generic_recent_neutralized"] += 1

    relation_preamble, relation_headers, edge_rows = read_table_with_preamble(
        repo / "关系边表.md"
    )
    edge_by_pair = {
        pair_key(row["起点ID"], row["终点ID"]): row for row in edge_rows
    }
    for record in edge_records:
        a, b = str(record["a"]), str(record["b"])
        key = pair_key(a, b)
        decision = str(record["decision"])
        if decision == "add":
            if key in edge_by_pair:
                raise PublishError(f"edge already exists: {key}")
            code = str(record["proposed_type"]).split()[0]
            strength = str(record.get("proposed_strength") or "中")
            priority = str(
                record.get("proposed_priority")
                or ("高" if strength == "强" else "中" if strength == "中" else "低")
            )
            row = {
                "起点ID": a,
                "终点ID": b,
                "关系类型": f"{code} {RELATION_LABELS[code]}",
                "联系强度": strength,
                "关联原因": str(record["reason"]).strip(),
                "复盘优先级": priority,
            }
            edge_rows.append(row)
            edge_by_pair[key] = row
            stats["edges_added"] += 1
            continue
        row = edge_by_pair.get(key)
        if row is None:
            raise PublishError(f"edge adjustment target missing: {key}")
        if decision == "remove":
            edge_rows.remove(row)
            del edge_by_pair[key]
            stats["edges_removed"] += 1
            continue
        code = str(record.get("proposed_type") or row["关系类型"]).split()[0]
        row["关系类型"] = f"{code} {RELATION_LABELS[code]}"
        if record.get("proposed_strength"):
            row["联系强度"] = str(record["proposed_strength"])
        if record.get("proposed_priority"):
            row["复盘优先级"] = str(record["proposed_priority"])
        row["关联原因"] = str(record["reason"]).strip()
        stats["edges_changed"] += 1

    # R03 is the only relation type whose validity directly depends on the
    # personal-error layer.  Retyping can repair a stale baseline R03; any
    # R03 that remains must have shared, evidence-backed labels.
    for edge in edge_rows:
        if not edge.get("关系类型", "").startswith("R03"):
            continue
        left = {
            item.split()[0]
            for item in by_id[edge["起点ID"]].get("错因标签", "").split("；")
            if item.startswith("E")
        }
        right = {
            item.split()[0]
            for item in by_id[edge["终点ID"]].get("错因标签", "").split("；")
            if item.startswith("E")
        }
        if not left.intersection(right):
            raise PublishError(
                f"R03 loses shared evidence after label cleanup: "
                f"{edge['起点ID']}::{edge['终点ID']}"
            )

    outputs: dict[Path, bytes] = {}
    relation_path = (repo / "关系边表.md").resolve()
    relation_data = _render_table(relation_preamble, relation_headers, edge_rows)
    if relation_data != relation_path.read_bytes():
        outputs[relation_path] = relation_data
    master = (repo / "节点总表.md").resolve()
    master_data = _render_table(preamble, headers, rows)
    if master_data != master.read_bytes():
        outputs[master] = master_data

    for subject, relative in SUBJECT_VIEWS.items():
        path = (repo / relative).resolve()
        view_preamble, view_headers, _ = read_table_with_preamble(path)
        prefix = SUBJECT_PREFIXES[subject]
        view_rows = [
            {header: row.get(header, "") for header in view_headers}
            for row in rows if row.get("ID", "").startswith(prefix + "_")
        ]
        view_rows.sort(key=lambda row: row.get("ID", ""))
        data = _render_table(view_preamble, view_headers, view_rows)
        if data != path.read_bytes():
            outputs[path] = data

    if changed_error_ids:
        dummy_reviews = {
            node_id: {"core_point_decision": "keep"} for node_id in by_id
        }
        data = _project_hit_index(
            repo,
            before_rows,
            by_id,
            {},
            dummy_reviews,
            force_error_ids=changed_error_ids,
        )
        path = (repo / "知识点命中索引.md").resolve()
        if data != path.read_bytes():
            outputs[path] = data

    for node_id, record in sorted(records.items()):
        path = (repo / f"{node_id}.md").resolve()
        text = _ensure_card_sections(path.read_text(encoding="utf-8"), node_id)
        status = str(record["user_error_status"])
        text = _upsert_bullet(
            text,
            "错因证据状态",
            status,
            section_heading="安全节点卡片",
            after_label="关系复核日期",
        )
        evidence_review_date = str(
            record.get("evidence_review_date") or review_date
        ).strip()
        text = _upsert_bullet(
            text,
            "错因证据复核日期",
            evidence_review_date,
            section_heading="安全节点卡片",
            after_label="错因证据状态",
        )
        text = _upsert_bullet(
            text,
            "用户错误入口",
            NEEDS_ERROR_TEXT if status == "needs_user" else str(record["user_error_entry"]).strip(),
            section_heading="复做安全信息",
            after_label="实际考查机制",
        )
        text = _upsert_bullet(
            text,
            "复做第一动作",
            _needs_action(record) if status == "needs_user" else str(record["redo_first_action"]).strip(),
            section_heading="复做安全信息",
            after_label="用户错误入口",
        )
        label_section = _existing_bullet_section(
            text, "错因标签", {"安全节点卡片", "复做安全信息"}
        ) or "安全节点卡片"
        text = _upsert_bullet(
            text,
            "错因标签",
            _labels(record),
            section_heading=label_section,
        )
        data = (text.rstrip("\n") + "\n").encode("utf-8")
        if data != path.read_bytes():
            outputs[path] = data
            stats["cards_changed"] += 1

    for fragment in (*GENERIC_ERROR_FRAGMENTS, *GENERIC_ACTION_FRAGMENTS):
        for node_id, record in records.items():
            if record["user_error_status"] == "needs_user":
                continue
            if fragment in str(record.get("user_error_entry", "")) or fragment in str(
                record.get("redo_first_action", "")
            ):
                raise PublishError(f"{node_id}: generic template survived projection")
    for node_id, record in records.items():
        if _protected_hits(record):
            raise PublishError(f"{node_id}: protected content in review record")
    stats["files_changed"] = len(outputs)
    stats["changed_error_ids"] = len(changed_error_ids)
    return outputs, stats


def _validated(
    repo: Path,
    details_root: Path,
    snapshot_path: Path,
    review_files: list[Path],
    edge_review_files: list[Path],
) -> tuple[dict[str, dict[str, Any]], list[dict[str, Any]], dict[str, Any]]:
    snapshot = _load_json(snapshot_path)
    _snapshot_live(snapshot, repo, details_root)
    report = validate_evidence(snapshot_path, review_files, edge_review_files)
    if report["status"] != "PASS":
        raise PublishError("question evidence validation failed:\n- " + "\n- ".join(report["errors"][:100]))
    records = _load_records(review_files)
    by_id = {
        str(record["id"]): {key: value for key, value in record.items() if key != "_source"}
        for record in records
    }
    edge_records = [
        {key: value for key, value in record.items() if key != "_source"}
        for record in _load_edge_records(edge_review_files)
    ]
    return by_id, edge_records, report


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo", default=".")
    parser.add_argument("--details-root", required=True)
    parser.add_argument("--runtime-root", default=str(DEFAULT_RUNTIME_ROOT))
    parser.add_argument("--snapshot", required=True)
    parser.add_argument("--reviews", required=True, nargs="+")
    parser.add_argument("--edge-reviews", nargs="*", default=[])
    parser.add_argument("--review-date", required=True)
    parser.add_argument("--idempotency-key")
    parser.add_argument("--lock-timeout", type=float, default=30.0)
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()

    repo = Path(args.repo).expanduser().resolve()
    details_root = Path(args.details_root).expanduser().resolve()
    runtime_root = Path(args.runtime_root).expanduser().resolve()
    snapshot_path = Path(args.snapshot).expanduser().resolve()
    review_files = _review_files([Path(value) for value in args.reviews])
    edge_review_files = _optional_review_files(
        [Path(value) for value in args.edge_reviews]
    )
    if (repo / ".git").exists() and runtime_root != DEFAULT_RUNTIME_ROOT:
        raise PublishError(f"production Git repo runtime_root fixed at {DEFAULT_RUNTIME_ROOT}")
    idempotency_key = args.idempotency_key or (
        f"question-evidence-review:{args.review_date}:{sha256_file(snapshot_path)[:20]}"
    )

    try:
        with RepoLock(repo, runtime_root, args.lock_timeout, timeout_error=RepoLockTimeout):
            payload = _payload_identity(
                snapshot_path, review_files, edge_review_files, args.review_date
            )
            payload_sha = _sha256_bytes(_canonical_bytes(payload))
            recovered = recover_transactions(repo, runtime_root) if args.apply else []
            if not args.apply:
                _assert_dry_run_wal_clean(repo, runtime_root)
            receipt_path = _receipt_path(runtime_root, repo, idempotency_key)
            if receipt_path.exists():
                receipt = _read_json_object(receipt_path)
                if receipt.get("payload_sha256") != payload_sha:
                    raise PublishError("idempotency key already used for another payload")
                print(json.dumps(_result_from_receipt(receipt_path, receipt).as_dict(), ensure_ascii=False, indent=2))
                return 0

            records, edge_records, validation = _validated(
                repo, details_root, snapshot_path, review_files, edge_review_files
            )
            if payload != _payload_identity(
                snapshot_path, review_files, edge_review_files, args.review_date
            ):
                raise PublishError("review inputs changed while locked")
            targets = {
                (repo / "节点总表.md").resolve(),
                (repo / "关系边表.md").resolve(),
                (repo / "知识点命中索引.md").resolve(),
                *((repo / relative).resolve() for relative in SUBJECT_VIEWS.values()),
                *((repo / f"{node_id}.md").resolve() for node_id in records),
            }
            before_hashes = _capture_before_hashes(targets)
            outputs, stats = _project(repo, records, edge_records, args.review_date)
            gates = _run_shadow_gates(repo, details_root, outputs)
            if not args.apply:
                print(json.dumps({
                    "status": "DRY_RUN",
                    "idempotency_key": idempotency_key,
                    "validation": validation["counts"],
                    "stats": stats,
                    "changed_files": [str(path) for path in sorted(outputs, key=str)],
                    "shadow_gates": [
                        {"command": item["command"], "returncode": item["returncode"]}
                        for item in gates
                    ],
                }, ensure_ascii=False, indent=2))
                return 0
            if not outputs:
                raise PublishError("projection has no changes and no prior receipt")

            _validated(
                repo, details_root, snapshot_path, review_files, edge_review_files
            )
            if payload != _payload_identity(
                snapshot_path, review_files, edge_review_files, args.review_date
            ):
                raise PublishError("review inputs changed during shadow gates")
            expected_before = {path: before_hashes[path] for path in outputs}
            txid, receipt, changed = _commit_transaction(
                repo,
                runtime_root,
                outputs,
                idempotency_key,
                payload_sha,
                [{"job_id": "", "formal_id": f"ALL-{len(records)}", "mode": MODE}],
                [repo],
                "408-question-evidence-review-writer",
                None,
                [],
                f"QUESTION-EVIDENCE-{args.review_date}",
                {"contract": MODE, "snapshot_sha256": sha256_file(snapshot_path)},
                payload,
                details_root,
                expected_before_hashes=expected_before,
            )
            print(json.dumps({
                "status": "COMMITTED",
                "transaction_id": txid,
                "receipt": str(receipt),
                "changed_files": changed,
                "stats": stats,
                "wal_recovered_before_commit": recovered,
                "shadow_gates": [
                    {"command": item["command"], "returncode": item["returncode"]}
                    for item in gates
                ],
            }, ensure_ascii=False, indent=2))
            return 0
    except (RepoLockTimeout, TransactionRolledBack, BatchError, PublishError) as exc:
        print(json.dumps({"status": "ERROR", "error": str(exc)}, ensure_ascii=False, indent=2))
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
