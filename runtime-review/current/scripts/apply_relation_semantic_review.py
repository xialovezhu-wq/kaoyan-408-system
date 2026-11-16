#!/usr/bin/env python3
"""以唯一写者事务发布全库逐题语义复核结论。

本工具只接受 ``relation_semantic_review.py validate`` 已能全量通过的审核集。
默认只做投影和影子门禁；只有显式 ``--apply`` 才会在仓库锁内通过既有 WAL
发布。它不会读取、生成或修改 ``复盘输出/``，也不会写 details vault。

发布内容：

* 每条基线边按 keep/remove/replace 裁决，模板理由不得残留；
* 每张安全卡登记实际考查机制、证据等级和关系复核状态；
* ``refine`` 的核心考点同步总表、分科镜像、命中索引和安全卡；
* ``replace`` 的主知识点必须另外提供人工核对后的 node plan，联动总表、
  年份/日期索引、分科镜像、命中索引和安全卡。

该脚本不是新题入库入口，不能分配 ID、写复做记录或改专题链。
"""

from __future__ import annotations

import argparse
import copy
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
from collections import Counter
from pathlib import Path
from typing import Any, Iterable

sys.path.insert(0, str(Path(__file__).resolve().parent))

from curate_node_fields import (  # noqa: E402
    Section,
    block_entries,
    insert_entry,
    new_section,
    normalize_section_blanks,
    parse_index,
    remove_entry,
)
from intake_batch_apply_408 import (  # noqa: E402
    DEFAULT_RUNTIME_ROOT,
    BatchError,
    TransactionRolledBack,
    _canonical_bytes,
    _commit_transaction,
    _assert_dry_run_wal_clean,
    _read_json_object,
    _receipt_path,
    _result_from_receipt,
    _sha256_bytes,
    recover_transactions,
)
from intake_lock_408 import RepoLock, RepoLockTimeout  # noqa: E402
from normalize_408_knowledge_points import (  # noqa: E402
    load_valid_labels,
    read_table_with_preamble,
)
from relation_semantic_review import (  # noqa: E402
    CORE_FILES,
    TEMPLATE_REASON_RE,
    load_review_records,
    pair_key,
    sha256_file,
    validate_reviews,
)

import runtime_paths_408 as _paths


NODE_PLAN_SCHEMA = "relation_semantic_node_plan_v1"
PUBLISH_RECEIPT_MODE = "relation_semantic_review"
FORMAL_ID_RE = re.compile(r"^(DS|CO|OS|CN)_(?:\d{4}|UNK)_\d{3}$")
KP_RE = re.compile(r"^(DS|CO|OS|CN)\d{2}-\d{2}\s+\S")
RELATION_LABELS = {
    "R01": "同一核心考点",
    "R02": "同一题型模板",
    "R03": "同一错因标签",
    "R04": "同一模糊概念",
    "R05": "易混概念对比",
    "R06": "上下游知识链",
    "R07": "跨科目类比",
    "R08": "同年份真题",
    "R09": "高频考点聚类",
}
SUBJECT_VIEWS = {
    "数据结构": "节点库/数据结构节点.md",
    "计算机组成原理": "节点库/计算机组成原理节点.md",
    "操作系统": "节点库/操作系统节点.md",
    "计算机网络": "节点库/计算机网络节点.md",
}
SUBJECT_PREFIXES = {
    "数据结构": "DS",
    "计算机组成原理": "CO",
    "操作系统": "OS",
    "计算机网络": "CN",
}
PLAN_FIELDS = (
    "module",
    "main_knowledge",
    "secondary_knowledge",
    "hit_knowledge",
)
PLAN_BASELINE_FILES = (
    "年份索引.md",
    "错题日期索引.md",
    "知识点标签表.md",
)
MASTER_TO_PLAN = {
    "module": "主模块",
    "main_knowledge": "主知识点",
    "secondary_knowledge": "副知识点",
    "hit_knowledge": "命中知识点",
}
CARD_TO_PLAN = {
    "module": "主模块",
    "main_knowledge": "主知识点",
    "secondary_knowledge": "副知识点",
    "hit_knowledge": "命中知识点",
}
PLACEHOLDER_TAGS = {"", "待补充", "暂无明确副知识点", "暂无明确命中知识点", "未记录"}
SAFE_COPY_SUFFIXES = {".md", ".py", ".json", ".jsonl", ".base"}
SHADOW_EXCLUDED_PARTS = {
    ".git",
    "书籍资料",
    "复盘输出",
    "node_modules",
    "__pycache__",
}


class PublishError(RuntimeError):
    """审核集、节点计划或正式发布不满足安全条件。"""


def _validate_snapshot_authority(
    snapshot: dict[str, Any], repo: Path, details_root: Path
) -> None:
    """拒绝自描述的小快照或遗漏正式证据的伪基线。"""
    if Path(str(snapshot.get("repo_root") or "")).expanduser().resolve() != repo:
        raise PublishError("snapshot.repo_root 与本次正式仓库不一致")
    if Path(str(snapshot.get("details_root") or "")).expanduser().resolve() != details_root:
        raise PublishError("snapshot.details_root 与本次 details 根不一致")
    counts = snapshot.get("counts") or {}
    expected_nodes = counts.get("nodes")
    expected_edges = counts.get("edges")
    if (
        not isinstance(expected_nodes, int)
        or expected_nodes <= 0
        or not isinstance(expected_edges, int)
        or expected_edges < 0
    ):
        raise PublishError("snapshot.counts 缺有效全库节点数或边数")

    expected_core = {f"formal:{relative}" for relative in CORE_FILES}
    core_records = snapshot.get("core_files")
    if not isinstance(core_records, list) or len(core_records) != len(expected_core):
        raise PublishError("snapshot.core_files 数量不完整")
    core_paths = {str(item.get("path") or "") for item in core_records if isinstance(item, dict)}
    if core_paths != expected_core:
        raise PublishError("snapshot.core_files 未精确覆盖正式核心文件")

    _, _, live_rows = read_table_with_preamble(repo / "节点总表.md")
    live_ids = {row.get("ID", "").strip() for row in live_rows if row.get("ID")}
    nodes = snapshot.get("nodes")
    if not isinstance(nodes, list) or len(nodes) != expected_nodes:
        raise PublishError("snapshot.nodes 不完整")
    snapshot_ids: set[str] = set()
    for node in nodes:
        if not isinstance(node, dict):
            raise PublishError("snapshot.nodes 每项必须是 object")
        node_id = str(node.get("id") or "")
        if not FORMAL_ID_RE.fullmatch(node_id) or node_id in snapshot_ids:
            raise PublishError(f"snapshot 节点非法或重复：{node_id!r}")
        snapshot_ids.add(node_id)
        evidence = node.get("evidence_files")
        if not isinstance(evidence, list):
            raise PublishError(f"snapshot 节点缺证据：{node_id}")
        safe = [
            item for item in evidence if isinstance(item, dict)
            and item.get("path") == f"formal:{node_id}.md"
            and item.get("role") == "safe_card"
            and isinstance(item.get("sha256"), str)
        ]
        if len(safe) != 1:
            raise PublishError(f"snapshot 节点未唯一冻结根安全卡：{node_id}")
    if snapshot_ids != live_ids or len(live_ids) != expected_nodes:
        raise PublishError("snapshot 节点集合与 live 节点总表不一致")

    _, _, live_edges = read_table_with_preamble(repo / "关系边表.md")
    live_pairs = {pair_key(row["起点ID"], row["终点ID"]) for row in live_edges}
    edges = snapshot.get("edges")
    if not isinstance(edges, list) or len(edges) != expected_edges:
        raise PublishError("snapshot.edges 不完整")
    snapshot_pairs = {
        pair_key(str(edge.get("a") or ""), str(edge.get("b") or ""))
        for edge in edges if isinstance(edge, dict)
    }
    if (
        len(live_edges) != expected_edges
        or len(live_pairs) != expected_edges
        or len(snapshot_pairs) != expected_edges
        or snapshot_pairs != live_pairs
    ):
        raise PublishError("snapshot 边集合与 live 关系边表不一致")


def _load_json(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise PublishError(f"JSON 读取失败 {path}：{exc}") from exc
    if not isinstance(value, dict):
        raise PublishError(f"JSON 顶层必须是 object：{path}")
    return value


def _render_table(preamble: list[str], headers: list[str], rows: list[dict[str, str]]) -> bytes:
    lines = list(preamble)
    if lines and lines[-1] != "":
        lines.append("")
    lines.append("|" + "|".join(headers) + "|")
    lines.append("|" + "|".join("---" for _ in headers) + "|")
    for row in rows:
        values = [str(row.get(header, "")) for header in headers]
        if any("|" in value or "\n" in value or "\r" in value for value in values):
            raise PublishError("Markdown 表格字段含竖线或换行，拒绝发布")
        lines.append("|" + "|".join(values) + "|")
    return ("\n".join(lines) + "\n").encode("utf-8")


def _project_sectioned_table_rows(
    path: Path,
    changes: dict[str, dict[str, str]],
) -> bytes:
    """外科更新单表或多分节 Markdown 表，非目标行保持字节级文本不变。"""
    original = path.read_text(encoding="utf-8")
    lines = original.splitlines()
    before_headings = [line for line in lines if line.startswith("## ")]
    before_headers = [line for line in lines if _split_table_cells(line)[:1] == ["ID"]]
    current_headers: list[str] | None = None
    updated = Counter()
    out: list[str] = []
    for line in lines:
        cells = _split_table_cells(line)
        if cells and cells[0] == "ID":
            current_headers = cells
            out.append(line)
            continue
        if line.startswith("## "):
            current_headers = None
            out.append(line)
            continue
        if current_headers and cells and cells[0] in changes:
            if len(cells) != len(current_headers):
                raise PublishError(f"{path.name} 目标行列数异常：{cells[0]}")
            row = dict(zip(current_headers, cells))
            for column, value in changes[cells[0]].items():
                if column not in row:
                    raise PublishError(f"{path.name} 缺目标列 {column}")
                if "|" in value or "\n" in value or "\r" in value:
                    raise PublishError(f"{path.name} {cells[0]}.{column} 含危险字符")
                row[column] = value
            out.append("|" + "|".join(row[header] for header in current_headers) + "|")
            updated[cells[0]] += 1
        else:
            out.append(line)
    missing_or_duplicate = {
        node_id: updated[node_id] for node_id in changes if updated[node_id] != 1
    }
    if missing_or_duplicate:
        raise PublishError(f"{path.name} 目标行必须恰好更新一次：{missing_or_duplicate}")
    if [line for line in out if line.startswith("## ")] != before_headings:
        raise PublishError(f"{path.name} 分节标题在投影中发生变化")
    if [line for line in out if _split_table_cells(line)[:1] == ["ID"]] != before_headers:
        raise PublishError(f"{path.name} 表头在投影中发生变化")
    return ("\n".join(out) + ("\n" if original.endswith("\n") else "")).encode("utf-8")


def _split_table_cells(line: str) -> list[str]:
    stripped = line.strip()
    if not (stripped.startswith("|") and stripped.endswith("|")):
        return []
    return [cell.strip() for cell in stripped[1:-1].split("|")]


def _split_tags(value: str) -> list[str]:
    stripped = (value or "").strip()
    if stripped in PLACEHOLDER_TAGS:
        return []
    # 知识点名称本身可含顿号，例如“二进制、八进制、十进制、十六进制转换”。
    return [part.strip() for part in re.split(r"[；;]", stripped) if part.strip()]


def _assert_input_paths_outside_review_output(paths: Iterable[Path]) -> None:
    """正式维护禁止把 ``复盘输出/`` 当作任何输入或输出证据层。"""
    bad = sorted(
        str(Path(path).expanduser().resolve())
        for path in paths
        if "复盘输出" in Path(path).expanduser().resolve().parts
    )
    if bad:
        raise PublishError("不得读取或写入复盘输出路径：" + ", ".join(bad))


def _join_tags(values: Iterable[str], placeholder: str) -> str:
    out: list[str] = []
    for value in values:
        value = value.strip()
        if value and value not in out:
            out.append(value)
    return "；".join(out) if out else placeholder


def _snapshot_sha(path: Path) -> str:
    return sha256_file(path)


def _review_maps(records: list[dict[str, Any]]) -> tuple[dict[str, dict[str, Any]], dict[str, dict[str, Any]]]:
    nodes: dict[str, dict[str, Any]] = {}
    edges: dict[str, dict[str, Any]] = {}
    for record in records:
        if record.get("record_type") == "node_review":
            nodes[str(record.get("id", ""))] = record
        elif record.get("record_type") == "edge_review":
            key = pair_key(str(record.get("a", "")), str(record.get("b", "")))
            edges[key] = record
    return nodes, edges


def _canonical_module_map(rows: list[dict[str, str]]) -> dict[str, str]:
    candidates: dict[str, Counter[str]] = {}
    for row in rows:
        main = row.get("主知识点", "").strip()
        module = row.get("主模块", "").strip()
        if len(main) < 4 or len(module) < 4:
            continue
        candidates.setdefault(main[:4], Counter())[module] += 1
    return {code: counts.most_common(1)[0][0] for code, counts in candidates.items()}


def generate_node_plan(
    snapshot_path: Path,
    records: list[dict[str, Any]],
    repo: Path,
    *,
    approve_conservative_defaults: bool = False,
) -> dict[str, Any]:
    """为主知识点 replace 生成必须人工核对的安全草案。"""
    _, _, rows = read_table_with_preamble(repo / "节点总表.md")
    by_id = {row["ID"]: row for row in rows}
    modules = _canonical_module_map(rows)
    node_reviews, _ = _review_maps(records)
    planned: list[dict[str, Any]] = []
    for node_id, review in sorted(node_reviews.items()):
        if review.get("main_knowledge_decision") != "replace":
            continue
        row = by_id[node_id]
        new_main = str(review["main_knowledge"]).strip()
        expected = {field: row[MASTER_TO_PLAN[field]].strip() for field in PLAN_FIELDS}
        old_main = expected["main_knowledge"]
        secondary = _split_tags(expected["secondary_knowledge"])
        hits = _split_tags(expected["hit_knowledge"])
        if new_main in secondary:
            secondary = [old_main if item == new_main else item for item in secondary]
        elif old_main not in secondary:
            secondary.append(old_main)
        if new_main not in hits:
            hits.insert(0, new_main)
        after = {
            "module": modules.get(new_main[:4], expected["module"]),
            "main_knowledge": new_main,
            "secondary_knowledge": _join_tags(secondary, "待补充"),
            "hit_knowledge": _join_tags(hits, new_main),
        }
        planned.append({
            "id": node_id,
            "expected": expected,
            "after": after,
            "requires_manual_decision": not approve_conservative_defaults,
            "reason": str(review.get("notes") or "主知识点逐题复核建议"),
        })
    return {
        "schema_version": NODE_PLAN_SCHEMA,
        "snapshot_sha256": _snapshot_sha(snapshot_path),
        "publish_file_hashes": {
            relative: sha256_file(repo / relative)
            for relative in PLAN_BASELINE_FILES
        },
        "policy": (
            "中央写者已核对保守迁移：新主知识点升为 main；旧主知识点保留为副/命中，避免静默丢失上下文"
            if approve_conservative_defaults
            else "每项核对副知识点与命中知识点后，把 requires_manual_decision 改为 false"
        ),
        "nodes": planned,
        "new_edges": [],
    }


def _validate_node_plan(
    plan: dict[str, Any],
    snapshot_path: Path,
    repo: Path,
    node_reviews: dict[str, dict[str, Any]],
    rows: list[dict[str, str]],
    valid_labels: set[str],
    baseline_edge_keys: set[str],
) -> tuple[dict[str, dict[str, Any]], list[dict[str, str]]]:
    if plan.get("schema_version") != NODE_PLAN_SCHEMA:
        raise PublishError(f"node plan schema 非法：{plan.get('schema_version')!r}")
    if plan.get("snapshot_sha256") != _snapshot_sha(snapshot_path):
        raise PublishError("node plan 绑定的 snapshot SHA-256 不一致")
    publish_hashes = plan.get("publish_file_hashes")
    if not isinstance(publish_hashes, dict):
        raise PublishError("node plan 缺 publish_file_hashes")
    for relative in PLAN_BASELINE_FILES:
        expected_hash = publish_hashes.get(relative)
        path = repo / relative
        if not isinstance(expected_hash, str) or expected_hash != sha256_file(path):
            raise PublishError(f"node plan 发布基线哈希漂移：{relative}")
    entries = plan.get("nodes")
    if not isinstance(entries, list):
        raise PublishError("node plan.nodes 必须是数组")
    expected_ids = {
        node_id for node_id, review in node_reviews.items()
        if review.get("main_knowledge_decision") == "replace"
    }
    by_id = {row["ID"]: row for row in rows}
    canonical_modules = _canonical_module_map(rows)
    actual: dict[str, dict[str, Any]] = {}
    for entry in entries:
        if not isinstance(entry, dict):
            raise PublishError("node plan 每项必须是 object")
        node_id = str(entry.get("id") or "")
        if node_id in actual:
            raise PublishError(f"node plan ID 重复：{node_id}")
        if node_id not in expected_ids:
            raise PublishError(f"node plan 含非 replace 节点：{node_id}")
        if entry.get("requires_manual_decision") is not False:
            raise PublishError(f"node plan 尚未完成人工核对：{node_id}")
        before, after = entry.get("expected"), entry.get("after")
        if not isinstance(before, dict) or not isinstance(after, dict):
            raise PublishError(f"node plan 缺 expected/after：{node_id}")
        for field in PLAN_FIELDS:
            if not isinstance(before.get(field), str) or not isinstance(after.get(field), str):
                raise PublishError(f"node plan {node_id} 字段 {field} 必须是字符串")
            if any(mark in before[field] or mark in after[field] for mark in ("\n", "\r")):
                raise PublishError(f"node plan {node_id}.{field} 含换行")
            live = by_id[node_id][MASTER_TO_PLAN[field]].strip()
            if before[field].strip() != live:
                raise PublishError(f"node plan {node_id}.{field} expected 与正式表不一致")
        review_main = str(node_reviews[node_id].get("main_knowledge") or "").strip()
        if after["main_knowledge"].strip() != review_main:
            raise PublishError(f"node plan {node_id} 新主知识点与 node_review 不一致")
        main = after["main_knowledge"].strip()
        if main not in valid_labels:
            raise PublishError(f"node plan {node_id} 主知识点不在标签表：{main}")
        module = after["module"].strip()
        subject = node_id.split("_", 1)[0]
        if main[:2] != subject or module[:2] != subject:
            raise PublishError(f"node plan {node_id} 主知识点/主模块科目与正式 ID 不一致")
        expected_module = canonical_modules.get(main[:4])
        if not expected_module or module != expected_module:
            raise PublishError(
                f"node plan {node_id} 主模块不是 {main[:4]} 的规范名称：{module!r}"
            )
        secondary = _split_tags(after["secondary_knowledge"])
        hits = _split_tags(after["hit_knowledge"])
        if main in secondary:
            raise PublishError(f"node plan {node_id} 主知识点不得重复出现在副知识点")
        if main not in hits:
            raise PublishError(f"node plan {node_id} 命中知识点必须包含新主知识点")
        for tag in secondary + hits:
            if tag not in valid_labels:
                raise PublishError(f"node plan {node_id} 含非法知识点：{tag}")
        actual[node_id] = copy.deepcopy(entry)
    if set(actual) != expected_ids:
        missing = sorted(expected_ids - set(actual))
        raise PublishError(f"node plan 未覆盖全部 replace 节点：{missing}")
    new_edges_raw = plan.get("new_edges") or []
    if not isinstance(new_edges_raw, list):
        raise PublishError("node plan.new_edges 必须是数组")
    new_edges: list[dict[str, str]] = []
    seen_new: set[str] = set()
    for item in new_edges_raw:
        if not isinstance(item, dict):
            raise PublishError("new_edges 每项必须是 object")
        a, b = str(item.get("a") or ""), str(item.get("b") or "")
        if a not in by_id or b not in by_id or a == b:
            raise PublishError(f"new_edge 端点非法：{a}::{b}")
        key = pair_key(a, b)
        if key in baseline_edge_keys:
            raise PublishError(f"new_edge 已是基线题对，应走 edge_review：{key}")
        if key in seen_new:
            raise PublishError(f"new_edge 无序题对重复：{key}")
        seen_new.add(key)
        if item.get("requires_manual_decision") is not False:
            raise PublishError(f"new_edge 尚未完成人工核对：{key}")
        code = str(item.get("type") or "").split()[0]
        strength = str(item.get("strength") or "")
        priority = str(item.get("priority") or "")
        confidence = str(item.get("confidence") or "")
        reason = str(item.get("reason") or "").strip()
        if code not in RELATION_LABELS:
            raise PublishError(f"new_edge 类型非法：{key} {code!r}")
        if strength not in {"强", "中", "弱"} or priority not in {"高", "中", "低"}:
            raise PublishError(f"new_edge 强度/优先级非法：{key}")
        if confidence != "high":
            raise PublishError(f"全库维护只发布 high 置信新边：{key}")
        if code in {"R07", "R08"} and strength != "弱":
            raise PublishError(f"new_edge {code} 只能是弱关系：{key}")
        if len(reason) < 10 or TEMPLATE_REASON_RE.match(reason):
            raise PublishError(f"new_edge 理由不具体或仍是模板：{key}")
        if "|" in reason or "\n" in reason or "\r" in reason:
            raise PublishError(f"new_edge 理由含表格危险字符：{key}")
        left, right = sorted((a, b))
        new_edges.append({
            "a": left,
            "b": right,
            "type": code,
            "strength": strength,
            "priority": priority,
            "reason": reason,
            "confidence": confidence,
        })
    return actual, sorted(new_edges, key=lambda item: pair_key(item["a"], item["b"]))


def _project_edges(
    repo: Path,
    edge_reviews: dict[str, dict[str, Any]],
    new_edges: list[dict[str, str]] | None = None,
) -> tuple[bytes, dict[str, int]]:
    preamble, headers, rows = read_table_with_preamble(repo / "关系边表.md")
    projected: list[dict[str, str]] = []
    stats = Counter()
    for row in rows:
        key = pair_key(row["起点ID"], row["终点ID"])
        review = edge_reviews.get(key)
        if not review:
            raise PublishError(f"关系边缺审核裁决：{key}")
        decision = review.get("decision")
        stats[str(decision)] += 1
        if decision == "remove":
            continue
        if decision == "keep":
            reason = str(review.get("reason") or "").strip()
            if TEMPLATE_REASON_RE.match(reason) or "|" in reason or "\n" in reason or "\r" in reason:
                raise PublishError(f"keep 关系理由不安全或仍是模板：{key}")
            kept = copy.deepcopy(row)
            # keep 表示保留类型/强度/优先级，不表示丢弃 reviewer 对机制理由的精炼。
            kept["关联原因"] = reason
            projected.append(kept)
            continue
        if decision != "replace":
            raise PublishError(f"关系边裁决非法：{key} {decision!r}")
        code = str(review.get("new_type") or "").split()[0]
        reason = str(review.get("reason") or "").strip()
        if code not in RELATION_LABELS:
            raise PublishError(f"关系边新类型非法：{key} {code!r}")
        if TEMPLATE_REASON_RE.match(reason):
            raise PublishError(f"关系边替换后仍是模板理由：{key}")
        if "|" in reason or "\n" in reason or "\r" in reason:
            raise PublishError(f"关系边理由含 Markdown 表格危险字符：{key}")
        new_row = copy.deepcopy(row)
        new_row["关系类型"] = f"{code} {RELATION_LABELS[code]}"
        new_row["联系强度"] = str(review.get("new_strength") or "")
        new_row["关联原因"] = reason
        new_row["复盘优先级"] = str(review.get("new_priority") or "")
        projected.append(new_row)
    if len(edge_reviews) != len(rows):
        raise PublishError(f"edge_review 数量与正式边不一致：{len(edge_reviews)} != {len(rows)}")
    existing_pairs = {pair_key(row["起点ID"], row["终点ID"]) for row in projected}
    for item in new_edges or []:
        key = pair_key(item["a"], item["b"])
        if key in existing_pairs:
            raise PublishError(f"新增边与投影后既有边重复：{key}")
        existing_pairs.add(key)
        code = item["type"]
        projected.append({
            "起点ID": item["a"],
            "终点ID": item["b"],
            "关系类型": f"{code} {RELATION_LABELS[code]}",
            "联系强度": item["strength"],
            "关联原因": item["reason"],
            "复盘优先级": item["priority"],
        })
        stats["add"] += 1
    if any(TEMPLATE_REASON_RE.match(row.get("关联原因", "")) for row in projected):
        raise PublishError("投影关系表仍含纯模板理由")
    stats["after"] = len(projected)
    return _render_table(preamble, headers, projected), dict(stats)


def _upsert_bullet(
    text: str,
    label: str,
    value: str,
    *,
    section_heading: str,
    after_label: str | None = None,
) -> str:
    if any(mark in value for mark in ("\n", "\r")):
        raise PublishError(f"安全卡字段 {label} 含换行")
    value = value.strip()
    if not value:
        raise PublishError(f"安全卡字段 {label} 不能为空")
    line = f"- {label}：{value}"
    pattern = re.compile(rf"^-\s*{re.escape(label)}[：:]\s*.*$")
    lines = text.splitlines()
    heading_line = f"## {section_heading}"
    try:
        section_start = next(
            index for index, current in enumerate(lines)
            if current.strip() == heading_line
        )
    except StopIteration as exc:
        raise PublishError(f"安全卡缺章节：{heading_line}") from exc
    section_end = next(
        (
            index for index in range(section_start + 1, len(lines))
            if lines[index].startswith("## ")
        ),
        len(lines),
    )
    matches = [index for index, current in enumerate(lines) if pattern.match(current)]
    if matches:
        if any(not (section_start < index < section_end) for index in matches):
            raise PublishError(f"安全卡字段 {label} 位于错误章节，拒绝静默搬移")
        lines[matches[0]] = line
        for index in reversed(matches[1:]):
            del lines[index]
        return "\n".join(lines) + ("\n" if text.endswith("\n") else "")
    if after_label:
        anchor = re.compile(rf"^-\s*{re.escape(after_label)}[：:].*$")
        anchors = [
            index for index in range(section_start + 1, section_end)
            if anchor.match(lines[index])
        ]
        if anchors:
            lines.insert(anchors[-1] + 1, line)
            return "\n".join(lines) + ("\n" if text.endswith("\n") else "")
    insert_at = section_end
    while insert_at > section_start + 1 and not lines[insert_at - 1].strip():
        insert_at -= 1
    lines.insert(insert_at, line)
    return "\n".join(lines) + ("\n" if text.endswith("\n") else "")


def _existing_bullet_section(
    text: str,
    label: str,
    allowed_sections: set[str],
) -> str | None:
    """返回既有字段所在章节；跨章节重复或意外章节一律拒绝。"""
    pattern = re.compile(rf"^-\s*{re.escape(label)}[：:]\s*.*$")
    current: str | None = None
    found: set[str] = set()
    for line in text.splitlines():
        if line.startswith("## "):
            current = line[3:].strip()
        elif pattern.match(line):
            found.add(current or "")
    if len(found) > 1:
        raise PublishError(f"安全卡字段 {label} 跨章节重复：{sorted(found)}")
    if not found:
        return None
    section = next(iter(found))
    if section not in allowed_sections:
        raise PublishError(f"安全卡字段 {label} 位于不允许章节：{section}")
    return section


def _ensure_card_sections(text: str, node_id: str) -> str:
    """把少量历史扁平安全卡无损升级为统一的两段结构。"""
    lines = text.splitlines()
    if "## 安全节点卡片" not in lines:
        first_bullet = next(
            (index for index, line in enumerate(lines) if line.startswith("- 正式节点 ID")),
            None,
        )
        if first_bullet is None:
            raise PublishError(f"{node_id} 扁平安全卡缺正式节点 ID 锚点")
        lines[first_bullet:first_bullet] = ["## 安全节点卡片", ""]
    if "## 复做安全信息" not in lines:
        redo_anchor = next(
            (index for index, line in enumerate(lines) if line.startswith("- 安全题目摘要")),
            None,
        )
        if redo_anchor is None:
            raise PublishError(f"{node_id} 安全卡缺安全题目摘要锚点")
        lines[redo_anchor:redo_anchor] = ["", "## 复做安全信息", ""]
    return "\n".join(lines) + ("\n" if text.endswith("\n") else "")


def _project_cards(
    repo: Path,
    node_reviews: dict[str, dict[str, Any]],
    plan: dict[str, dict[str, Any]],
    review_date: str,
    effective_status: dict[str, str] | None = None,
) -> dict[Path, bytes]:
    out: dict[Path, bytes] = {}
    for node_id, review in sorted(node_reviews.items()):
        path = repo / f"{node_id}.md"
        if not path.is_file():
            raise PublishError(f"缺安全节点卡：{node_id}.md")
        text = _ensure_card_sections(path.read_text(encoding="utf-8"), node_id)
        if node_id in plan:
            after = plan[node_id]["after"]
            anchors = {
                "module": "科目",
                "main_knowledge": "主模块",
                "secondary_knowledge": "主知识点",
                "hit_knowledge": "副知识点",
            }
            for field, label in CARD_TO_PLAN.items():
                text = _upsert_bullet(
                    text,
                    label,
                    after[field],
                    section_heading="安全节点卡片",
                    after_label=anchors[field],
                )
        if review.get("core_point_decision") == "refine":
            core_section = _existing_bullet_section(
                text,
                "核心考点",
                {"安全节点卡片", "复做安全信息"},
            ) or "复做安全信息"
            text = _upsert_bullet(
                text,
                "核心考点",
                str(review["core_point"]).strip(),
                section_heading=core_section,
            )
        mechanism = str(review["question_mechanism"]).strip()
        if review.get("relation_status") == "needs_user":
            mechanism = f"待确认候选：{mechanism}"
        text = _upsert_bullet(
            text,
            "实际考查机制",
            mechanism,
            section_heading="复做安全信息",
            after_label="问法类型",
        )
        status = (effective_status or {}).get(node_id, str(review["relation_status"]).strip())
        semantic_section = _existing_bullet_section(
            text,
            "关系复核",
            {"安全节点卡片", "复做安全信息"},
        ) or "安全节点卡片"
        text = _upsert_bullet(
            text,
            "语义证据等级",
            str(review["evidence_grade"]).strip(),
            section_heading=semantic_section,
            after_label="MarginNote 定位状态",
        )
        text = _upsert_bullet(
            text,
            "关系复核",
            status,
            section_heading=semantic_section,
            after_label="语义证据等级",
        )
        relation_review_date = str(
            review.get("relation_review_date") or review_date
        ).strip()
        text = _upsert_bullet(
            text,
            "关系复核日期",
            relation_review_date,
            section_heading=semantic_section,
            after_label="关系复核",
        )
        data = (text.rstrip("\n") + "\n").encode("utf-8")
        if data != path.read_bytes():
            out[path.resolve()] = data
    return out


def _render_index(head: list[str], sections: list[Section]) -> bytes:
    kept: list[Section] = []
    for section in sections:
        normalize_section_blanks(section)
        kept.append(section)
    kept.sort(key=lambda section: section.code())
    lines = list(head)
    while lines and not lines[-1].strip():
        lines.pop()
    lines.append("")
    for section in kept:
        lines.extend(section.lines)
        if not lines or lines[-1].strip():
            lines.append("")
    return ("\n".join(lines).rstrip("\n") + "\n").encode("utf-8")


def _recount_index_section(section: Section) -> None:
    """节点数量按该知识点分节内出现的唯一正式 ID 计数。

    旧索引有些命中只保存在“错因触发记录”，不能只数主命中/显式命中块。
    """
    ids = set(
        re.findall(
            r"\b(?:DS|CO|OS|CN)_(?:\d{4}|UNK)_\d{3}\b",
            "\n".join(section.lines),
        )
    )
    for index, line in enumerate(section.lines):
        if re.match(r"^-\s*节点数量：", line.strip()):
            section.lines[index] = f"- 节点数量：{len(ids)}"
            return
    section.lines.insert(2, f"- 节点数量：{len(ids)}")


def _index_remove_everywhere(sections: list[Section], node_id: str) -> None:
    for section in sections:
        for title in ("主知识点命中", "命中知识点", "错因触发记录"):
            while remove_entry(section, title, node_id) is not None:
                pass


def _index_find_main_section(sections: list[Section], node_id: str) -> Section | None:
    for section in sections:
        for line in block_entries(section, "主知识点命中"):
            if re.match(rf"^-\s*{re.escape(node_id)}：", line.strip()):
                return section
    return None


def _formal_entry_id(line: str) -> str | None:
    match = re.match(
        r"^-\s*((?:DS|CO|OS|CN)_(?:\d{4}|UNK)_\d{3})：",
        line.strip(),
    )
    return match.group(1) if match else None


def _expected_hit_pairs(
    after_rows: dict[str, dict[str, str]],
) -> tuple[set[tuple[str, str]], set[tuple[str, str]]]:
    main_pairs: set[tuple[str, str]] = set()
    all_pairs: set[tuple[str, str]] = set()
    for node_id, row in after_rows.items():
        main = row["主知识点"].strip()
        hits = _split_tags(row.get("命中知识点", ""))
        if main not in hits:
            hits.insert(0, main)
        main_pairs.add((main, node_id))
        all_pairs.update((kp, node_id) for kp in hits)
    return main_pairs, all_pairs


def _validate_hit_index_exact(
    sections: list[Section],
    after_rows: dict[str, dict[str, str]],
) -> None:
    """命中索引必须与节点总表命中集合完全相等，且每类条目恰好一条。"""
    expected_main, expected_all = _expected_hit_pairs(after_rows)
    actual_main: Counter[tuple[str, str]] = Counter()
    actual_hit: Counter[tuple[str, str]] = Counter()
    actual_error: Counter[tuple[str, str]] = Counter()
    for section in sections:
        for block, counter in (
            ("主知识点命中", actual_main),
            ("命中知识点", actual_hit),
            ("错因触发记录", actual_error),
        ):
            for line in block_entries(section, block):
                node_id = _formal_entry_id(line)
                if node_id:
                    counter[(section.kp, node_id)] += 1

    actual_all = set(actual_main) | set(actual_hit)
    duplicate = sorted(
        (kp, node_id, count)
        for counter in (actual_main, actual_hit, actual_error)
        for (kp, node_id), count in counter.items()
        if count != 1
    )
    overlap = sorted(set(actual_main) & set(actual_hit))
    errors: list[str] = []
    if set(actual_main) != expected_main:
        errors.append(
            f"主命中缺 {len(expected_main - set(actual_main))} / 多 {len(set(actual_main) - expected_main)}"
        )
    if actual_all != expected_all:
        errors.append(
            f"全部命中缺 {len(expected_all - actual_all)} / 多 {len(actual_all - expected_all)}"
        )
    if set(actual_error) != expected_all:
        errors.append(
            f"错因触发缺 {len(expected_all - set(actual_error))} / 多 {len(set(actual_error) - expected_all)}"
        )
    if duplicate:
        errors.append(f"重复条目 {duplicate[:10]}")
    if overlap:
        errors.append(f"同一题点同时出现在主/普通命中 {overlap[:10]}")
    if errors:
        raise PublishError("知识点命中索引未与节点总表 exact reconciliation：" + "；".join(errors))


def _project_hit_index(
    repo: Path,
    before_rows: dict[str, dict[str, str]],
    after_rows: dict[str, dict[str, str]],
    plan: dict[str, dict[str, Any]],
    node_reviews: dict[str, dict[str, Any]],
    force_error_ids: set[str] | None = None,
) -> bytes:
    path = repo / "知识点命中索引.md"
    head, sections = parse_index(path)
    by_kp = {section.kp: section for section in sections}

    # 冻结可复用的人工描述，再移除全部正式 ID 条目；随后严格按节点总表
    # 重建 pair 集。这样既保留正确的专用描述，又清掉历史漏项、错项和重复项。
    existing: dict[tuple[str, str, str], str] = {}
    all_existing_ids: set[str] = set()
    for section in sections:
        for block in ("主知识点命中", "命中知识点", "错因触发记录"):
            for line in block_entries(section, block):
                node_id = _formal_entry_id(line)
                if node_id:
                    existing.setdefault((section.kp, block, node_id), line)
                    all_existing_ids.add(node_id)
    for node_id in sorted(all_existing_ids):
        _index_remove_everywhere(sections, node_id)

    force_error_ids = force_error_ids or set()
    for node_id, row in sorted(after_rows.items()):
        main = row["主知识点"].strip()
        hits = _split_tags(row.get("命中知识点", ""))
        if main not in hits:
            hits.insert(0, main)
        for kp in hits:
            section = by_kp.get(kp)
            if section is None:
                section = new_section(kp)
                sections.append(section)
                by_kp[kp] = section
            if kp == main:
                reuse = existing.get((kp, "主知识点命中", node_id))
                if node_id in plan or node_reviews[node_id].get("core_point_decision") == "refine":
                    reuse = None
                entry = reuse or (
                    f"- {node_id}：{row['核心考点']}；"
                    f"最近复做日期：{row['最近复做日期']}。"
                )
                insert_entry(section, "主知识点命中", entry, node_id)
            else:
                entry = existing.get((kp, "命中知识点", node_id)) or (
                    f"- {node_id}：{row['核心考点']}（命中，主知识点 "
                    f"{main.split()[0]}）；最近复做日期：{row['最近复做日期']}。"
                )
                insert_entry(section, "命中知识点", entry, node_id)
            error_entry = (
                None
                if node_id in force_error_ids
                else existing.get((kp, "错因触发记录", node_id))
            ) or (
                f"- {node_id}：{row['错因标签']}。"
            )
            insert_entry(section, "错因触发记录", error_entry, node_id)

    for section in sections:
        _recount_index_section(section)
    _validate_hit_index_exact(sections, after_rows)
    return _render_index(head, sections)


def _project_node_tables(
    repo: Path,
    node_reviews: dict[str, dict[str, Any]],
    plan: dict[str, dict[str, Any]],
) -> tuple[dict[Path, bytes], dict[str, int]]:
    preamble, headers, rows = read_table_with_preamble(repo / "节点总表.md")
    preamble = [
        re.sub(r"共\s*\d+\s*条", f"共 {len(rows)} 条", line)
        for line in preamble
    ]
    before_rows = {row["ID"]: copy.deepcopy(row) for row in rows}
    after_rows = {row["ID"]: row for row in rows}
    stats = Counter()

    for node_id, review in node_reviews.items():
        row = after_rows[node_id]
        if review.get("core_point_decision") == "refine":
            row["核心考点"] = str(review["core_point"]).strip()
            stats["core_refine"] += 1
    for node_id, item in plan.items():
        row = after_rows[node_id]
        for field in PLAN_FIELDS:
            row[MASTER_TO_PLAN[field]] = str(item["after"][field]).strip()
        stats["main_replace"] += 1

    outputs: dict[Path, bytes] = {}
    master_path = (repo / "节点总表.md").resolve()
    master_data = _render_table(preamble, headers, rows)
    if master_data != master_path.read_bytes():
        outputs[master_path] = master_data

    # 年份索引含 17 张分年表，必须逐行外科更新，严禁单表解析后拍平分节。
    index_changes = {
        "年份索引.md": {
            node_id: {
                "模块": after_rows[node_id]["主模块"],
                "主知识点": after_rows[node_id]["主知识点"],
            }
            for node_id in plan
        },
        "错题日期索引.md": {
            node_id: {"主知识点": after_rows[node_id]["主知识点"]}
            for node_id in plan
        },
    }
    for rel, changes in index_changes.items():
        path = (repo / rel).resolve()
        data = _project_sectioned_table_rows(path, changes)
        if data != path.read_bytes():
            outputs[path] = data

    # 分科镜像按当前总表投影，保留各文件现有前言与列集合。
    for subject, rel in SUBJECT_VIEWS.items():
        path = (repo / rel).resolve()
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

    hit_path = (repo / "知识点命中索引.md").resolve()
    hit_data = _project_hit_index(repo, before_rows, after_rows, plan, node_reviews)
    if hit_data != hit_path.read_bytes():
        outputs[hit_path] = hit_data
    return outputs, dict(stats)


def _project_all(
    repo: Path,
    node_reviews: dict[str, dict[str, Any]],
    edge_reviews: dict[str, dict[str, Any]],
    plan: dict[str, dict[str, Any]],
    new_edges: list[dict[str, str]],
    review_date: str,
) -> tuple[dict[Path, bytes], dict[str, Any]]:
    relation_data, edge_stats = _project_edges(repo, edge_reviews, new_edges)
    outputs, node_stats = _project_node_tables(repo, node_reviews, plan)
    relation_path = (repo / "关系边表.md").resolve()
    if relation_data != relation_path.read_bytes():
        outputs[relation_path] = relation_data
    card_outputs = _project_cards(repo, node_reviews, plan, review_date)
    for path, data in card_outputs.items():
        if path in outputs and outputs[path] != data:
            raise PublishError(f"事务投影目标冲突：{path}")
        outputs[path] = data
    return outputs, {
        "node": node_stats,
        "edge": edge_stats,
        "cards_changed": len(card_outputs),
        "relation_status": dict(
            Counter(str(review["relation_status"]) for review in node_reviews.values())
        ),
        "files_changed": len(outputs),
    }


def _post_projection_semantic_checks(
    outputs: dict[Path, bytes],
    repo: Path,
    node_reviews: dict[str, dict[str, Any]],
) -> None:
    relation_text = outputs.get((repo / "关系边表.md").resolve(), (repo / "关系边表.md").read_bytes()).decode("utf-8")
    if re.search(r"\|(?:R01|R09)\s+[^|]+\|[^|]+\|(?:同一主知识点|同属专题链|同一模糊概念)[：:]", relation_text):
        raise PublishError("投影后仍存在模板关系边")
    node_data = outputs.get(
        (repo / "节点总表.md").resolve(),
        (repo / "节点总表.md").read_bytes(),
    )
    _, _, node_rows = _table_from_bytes(node_data, "ID")
    by_id = {row["ID"]: row for row in node_rows}
    degree = Counter()
    seen_pairs: set[str] = set()
    _, _, edge_rows = _table_from_bytes(relation_text.encode("utf-8"), "起点ID")
    for row in edge_rows:
        a, b = row["起点ID"], row["终点ID"]
        key = pair_key(a, b)
        if a == b or a not in by_id or b not in by_id or key in seen_pairs:
            raise PublishError(f"投影关系边端点非法、自环或重复：{key}")
        seen_pairs.add(key)
        code = row.get("关系类型", "").split()[0]
        strength = row.get("联系强度", "")
        left, right = by_id[a], by_id[b]
        if code == "R07":
            if a[:2] == b[:2] or strength != "弱":
                raise PublishError(f"R07 必须跨科目且为弱关系：{key}")
        elif code == "R08":
            year_a, year_b = left.get("年份", ""), right.get("年份", "")
            if (
                not re.fullmatch(r"\d{4}", year_a)
                or year_a != year_b
                or left.get("主模块") != right.get("主模块")
                or strength != "弱"
            ):
                raise PublishError(f"R08 必须同年份、同模块且为弱关系：{key}")
        elif code == "R09":
            if (
                a[:2] != b[:2]
                or left.get("主知识点", "")[:4]
                != right.get("主知识点", "")[:4]
            ):
                raise PublishError(f"R09 必须同科目且同主知识章节：{key}")
        degree[a] += 1
        degree[b] += 1

    status_errors: list[str] = []
    for node_id, review in node_reviews.items():
        status = str(review.get("relation_status") or "")
        field_needs = (
            review.get("main_knowledge_decision") == "needs_user"
            or review.get("core_point_decision") == "needs_user"
        )
        if field_needs and status != "needs_user":
            status_errors.append(f"{node_id}:字段待确认但关系状态={status}")
        if status == "connected" and degree[node_id] == 0:
            status_errors.append(f"{node_id}:connected 但无边")
        if status in {"reviewed_no_reliable_edge", "needs_user"} and degree[node_id] != 0:
            status_errors.append(f"{node_id}:{status} 但仍有 {degree[node_id]} 条边")
    if status_errors:
        raise PublishError("关系复核状态与最终图不闭合：" + "；".join(status_errors[:50]))

    hit_data = outputs.get(
        (repo / "知识点命中索引.md").resolve(),
        (repo / "知识点命中索引.md").read_bytes(),
    )
    with tempfile.NamedTemporaryFile(suffix=".md", delete=False) as handle:
        temp = Path(handle.name)
        handle.write(hit_data)
    try:
        _, hit_sections = parse_index(temp)
    finally:
        temp.unlink(missing_ok=True)
    _validate_hit_index_exact(hit_sections, by_id)


def _table_from_bytes(data: bytes, first_column: str) -> tuple[list[str], list[str], list[dict[str, str]]]:
    """使用临时文件复用项目 Markdown 表格解析器。"""
    with tempfile.NamedTemporaryFile(suffix=".md", delete=False) as handle:
        temp = Path(handle.name)
        handle.write(data)
    try:
        result = read_table_with_preamble(temp)
    finally:
        temp.unlink(missing_ok=True)
    if not result[1] or result[1][0] != first_column:
        raise PublishError(f"投影表首列不是 {first_column}")
    return result


def _copy_shadow_source(repo: Path, shadow: Path) -> None:
    """复制门禁所需的轻量文本树；明确跳过复盘输出与大体积参考资料。"""
    for current, dirnames, filenames in os.walk(repo, topdown=True, followlinks=False):
        dirnames[:] = [name for name in dirnames if name not in SHADOW_EXCLUDED_PARTS]
        current_path = Path(current)
        for filename in filenames:
            source = current_path / filename
            if source.suffix not in SAFE_COPY_SUFFIXES or not source.is_file():
                continue
            relative = source.relative_to(repo)
            target = shadow / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source, target)


def _run_shadow_gates(
    repo: Path,
    details_root: Path,
    outputs: dict[Path, bytes],
) -> list[dict[str, Any]]:
    results: list[dict[str, Any]] = []
    with tempfile.TemporaryDirectory(prefix="kaoyan-408-semantic-shadow-") as raw:
        shadow = Path(raw) / "repo"
        shadow.mkdir()
        _copy_shadow_source(repo, shadow)
        for target, data in outputs.items():
            _assert_input_paths_outside_review_output([target])
            try:
                relative = target.resolve().relative_to(repo.resolve())
            except ValueError as exc:
                raise PublishError(f"投影目标越过正式仓库：{target}") from exc
            shadow_target = shadow / relative
            shadow_target.parent.mkdir(parents=True, exist_ok=True)
            shadow_target.write_bytes(data)
        # coverage/bridge 是锁外派生层，不进入正式 WAL；但影子树先刷新 coverage，
        # 证明“正式投影 + 派生刷新”能够在提交前通过一致性硬门禁。
        commands = [
            [
                sys.executable,
                str(shadow / "scripts/refresh_coverage_index.py"),
                "--repo",
                str(shadow),
            ],
            [sys.executable, str(shadow / "scripts/check_safe_node_cards.py"), "--repo", str(shadow), "--list-missing"],
            [sys.executable, str(shadow / "scripts/audit_network_health.py"), "--repo", str(shadow)],
            [
                sys.executable,
                str(shadow / "scripts/audit_extended.py"),
                "--repo",
                str(shadow),
                "--details-root",
                str(details_root),
            ],
        ]
        for command in commands:
            completed = subprocess.run(command, capture_output=True, text=True)
            item = {
                "command": " ".join(command),
                "returncode": completed.returncode,
                "stdout": completed.stdout[-6000:],
                "stderr": completed.stderr[-3000:],
            }
            results.append(item)
            if completed.returncode:
                raise PublishError(
                    "影子门禁失败：\n"
                    + item["command"]
                    + "\n"
                    + (completed.stdout or completed.stderr)[-5000:]
                )
    return results


def _payload_identity(
    snapshot_path: Path,
    review_files: list[Path],
    node_plan_path: Path,
    review_date: str,
) -> dict[str, Any]:
    return {
        "mode": PUBLISH_RECEIPT_MODE,
        "snapshot": {"path": str(snapshot_path), "sha256": sha256_file(snapshot_path)},
        "reviews": [
            {"path": str(path), "sha256": sha256_file(path)} for path in sorted(review_files)
        ],
        "node_plan": {"path": str(node_plan_path), "sha256": sha256_file(node_plan_path)},
        "review_date": review_date,
    }


def _semantic_publish_targets(
    repo: Path,
    node_reviews: dict[str, dict[str, Any]],
) -> set[Path]:
    targets = {
        (repo / relative).resolve()
        for relative in (
            "节点总表.md",
            "关系边表.md",
            "年份索引.md",
            "错题日期索引.md",
            "知识点命中索引.md",
            *SUBJECT_VIEWS.values(),
        )
    }
    targets.update((repo / f"{node_id}.md").resolve() for node_id in node_reviews)
    return targets


def _capture_before_hashes(paths: Iterable[Path]) -> dict[Path, str | None]:
    out: dict[Path, str | None] = {}
    for path in paths:
        resolved = path.resolve()
        if resolved.exists() and not resolved.is_file():
            raise PublishError(f"事务目标不是普通文件：{resolved}")
        out[resolved] = sha256_file(resolved) if resolved.is_file() else None
    return out


def _review_jsonl_files(paths: Iterable[Path]) -> list[Path]:
    files: set[Path] = set()
    for raw in paths:
        expanded = raw.expanduser()
        if expanded.is_symlink():
            raise PublishError(f"review 路径不得是符号链接：{expanded}")
        path = expanded.resolve()
        _assert_input_paths_outside_review_output([path])
        if path.is_dir():
            for candidate in path.glob("*.jsonl"):
                if candidate.is_symlink():
                    raise PublishError(f"review JSONL 不得是符号链接：{candidate}")
                if candidate.is_file():
                    resolved = candidate.resolve()
                    _assert_input_paths_outside_review_output([resolved])
                    files.add(resolved)
        elif path.is_file():
            files.add(path)
        else:
            raise PublishError(f"review 路径不存在：{path}")
    if not files:
        raise PublishError("没有找到 review JSONL")
    return sorted(files)


def _validated_inputs(
    repo: Path,
    details_root: Path,
    snapshot_path: Path,
    review_paths: list[Path],
    node_plan_path: Path,
) -> tuple[
    dict[str, dict[str, Any]],
    dict[str, dict[str, Any]],
    dict[str, dict[str, Any]],
    list[dict[str, str]],
    list[Path],
]:
    _assert_input_paths_outside_review_output(
        [snapshot_path, node_plan_path, *review_paths]
    )
    snapshot = _load_json(snapshot_path)
    _validate_snapshot_authority(snapshot, repo, details_root)
    review_files = _review_jsonl_files(review_paths)
    records, load_errors = load_review_records(review_files)
    report = validate_reviews(
        snapshot,
        records,
        repo=repo,
        details_root=details_root,
        initial_errors=load_errors,
    )
    if report["status"] != "PASS":
        raise PublishError("全量 review 校验未通过：\n- " + "\n- ".join(report["errors"][:100]))
    node_reviews, edge_reviews = _review_maps(records)
    inconsistent_needs = sorted(
        node_id for node_id, review in node_reviews.items()
        if (
            review.get("main_knowledge_decision") == "needs_user"
            or review.get("core_point_decision") == "needs_user"
        )
        and review.get("relation_status") != "needs_user"
    )
    if inconsistent_needs:
        raise PublishError(
            "字段 decision=needs_user 时关系状态也必须 needs_user："
            + ", ".join(inconsistent_needs)
        )
    unsafe_needs_user_fields = sorted(
        node_id for node_id, review in node_reviews.items()
        if review.get("relation_status") == "needs_user"
        and (
            review.get("main_knowledge_decision") == "replace"
            or review.get("core_point_decision") == "refine"
        )
    )
    if unsafe_needs_user_fields:
        raise PublishError(
            "needs_user 节点不得同时发布 replace/refine 字段："
            + ", ".join(unsafe_needs_user_fields)
        )
    needs_user_ids = {
        node_id for node_id, review in node_reviews.items()
        if review.get("relation_status") == "needs_user"
    }
    unsafe_needs_user_edges = sorted(
        key for key, review in edge_reviews.items()
        if review.get("decision") != "remove"
        and any(node_id in needs_user_ids for node_id in key.split("::"))
    )
    if unsafe_needs_user_edges:
        raise PublishError(
            "needs_user 端点的基线边必须保守 remove："
            + ", ".join(unsafe_needs_user_edges)
        )
    _, _, rows = read_table_with_preamble(repo / "节点总表.md")
    plan_raw = _load_json(node_plan_path)
    plan, new_edges = _validate_node_plan(
        plan_raw,
        snapshot_path,
        repo,
        node_reviews,
        rows,
        load_valid_labels(repo),
        set(edge_reviews),
    )
    bad_new_needs = sorted(
        pair_key(item["a"], item["b"])
        for item in new_edges
        if item["a"] in needs_user_ids or item["b"] in needs_user_ids
    )
    if bad_new_needs:
        raise PublishError("new_edge 不得连接 needs_user 节点：" + ", ".join(bad_new_needs))
    return node_reviews, edge_reviews, plan, new_edges, review_files


def _emit(value: dict[str, Any]) -> None:
    print(json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True))


def _generate_plan_command(args: argparse.Namespace) -> int:
    repo = Path(args.repo).expanduser().resolve()
    snapshot_path = Path(args.snapshot).expanduser().resolve()
    _assert_input_paths_outside_review_output([snapshot_path])
    review_files = _review_jsonl_files([Path(value) for value in args.reviews])
    records, errors = load_review_records(review_files)
    if errors:
        raise PublishError("review JSONL 读取失败：\n- " + "\n- ".join(errors))
    plan = generate_node_plan(
        snapshot_path,
        records,
        repo,
        approve_conservative_defaults=args.approve_conservative_defaults,
    )
    payload = json.dumps(plan, ensure_ascii=False, indent=2, sort_keys=True).encode("utf-8") + b"\n"
    raw_out = Path(args.out).expanduser()
    if not raw_out.is_absolute():
        raw_out = Path.cwd() / raw_out
    out = raw_out.resolve()
    _assert_input_paths_outside_review_output([out])
    reports_root = (repo / "wiki/lint_reports").resolve()
    try:
        out.relative_to(reports_root)
    except ValueError as exc:
        raise PublishError("node plan 只能写入当前仓库 wiki/lint_reports/ 下") from exc
    if out.name != "formal-change-plan.json":
        raise PublishError("node plan 文件名固定为 formal-change-plan.json")
    if out.exists() and not args.overwrite:
        raise PublishError(f"输出已存在；确认重建请加 --overwrite：{out}")
    out.parent.mkdir(parents=True, exist_ok=True)
    temporary = out.parent / f".{out.name}.{os.getpid()}.tmp"
    try:
        temporary.write_bytes(payload)
        os.replace(temporary, out)
    finally:
        temporary.unlink(missing_ok=True)
    _emit({
        "status": "PLAN_GENERATED",
        "path": str(out),
        "replace_nodes": len(plan["nodes"]),
        "manual_decisions_required": sum(
            1 for item in plan["nodes"] if item["requires_manual_decision"]
        ),
    })
    return 0


def _publish_command(args: argparse.Namespace) -> int:
    repo = Path(args.repo).expanduser().resolve()
    details_root = Path(args.details_root).expanduser().resolve()
    runtime_root = Path(args.runtime_root).expanduser().resolve()
    snapshot_path = Path(args.snapshot).expanduser().resolve()
    node_plan_path = Path(args.node_plan).expanduser().resolve()
    review_paths = [Path(value) for value in args.reviews]
    _assert_input_paths_outside_review_output(
        [snapshot_path, node_plan_path, *review_paths]
    )
    review_files = _review_jsonl_files(review_paths)
    idempotency_key = args.idempotency_key or (
        f"relation-semantic-review:{args.review_date}:{sha256_file(snapshot_path)[:20]}"
    )

    if (repo / ".git").exists() and runtime_root != DEFAULT_RUNTIME_ROOT:
        raise PublishError(
            f"生产 Git 仓库 runtime_root 固定为 {DEFAULT_RUNTIME_ROOT}"
        )

    try:
        with RepoLock(repo, runtime_root, args.lock_timeout, timeout_error=RepoLockTimeout):
            payload_identity = _payload_identity(
                snapshot_path, review_files, node_plan_path, args.review_date
            )
            payload_sha = _sha256_bytes(_canonical_bytes(payload_identity))
            if args.apply:
                recovered = recover_transactions(repo, runtime_root)
            else:
                _assert_dry_run_wal_clean(repo, runtime_root)
                recovered = []
            receipt_path = _receipt_path(runtime_root, repo, idempotency_key)
            if receipt_path.exists():
                receipt = _read_json_object(receipt_path)
                if receipt.get("payload_sha256") != payload_sha:
                    raise PublishError(f"幂等键已用于不同 payload：{idempotency_key}")
                result = _result_from_receipt(receipt_path, receipt)
                _emit(result.as_dict())
                return 0

            node_reviews, edge_reviews, plan, new_edges, validated_review_files = _validated_inputs(
                repo, details_root, snapshot_path, review_paths, node_plan_path
            )
            if validated_review_files != review_files:
                raise PublishError("review 目录在锁内校验期间新增或移除了 JSONL")
            if payload_identity != _payload_identity(
                snapshot_path, review_files, node_plan_path, args.review_date
            ):
                raise PublishError("review 或 node plan 在锁内校验期间发生变化")
            # 在投影前冻结全部可能输出的 base hash。影子门禁期间若有外部
            # 编辑，WAL 准备阶段会 compare-and-abort，绝不以旧投影覆盖新内容。
            base_hashes = _capture_before_hashes(
                _semantic_publish_targets(repo, node_reviews)
            )
            outputs, stats = _project_all(
                repo, node_reviews, edge_reviews, plan, new_edges, args.review_date
            )
            _post_projection_semantic_checks(outputs, repo, node_reviews)
            gates = _run_shadow_gates(repo, details_root, outputs)

            if not args.apply:
                _emit({
                    "status": "DRY_RUN",
                    "idempotency_key": idempotency_key,
                    "stats": stats,
                    "changed_files": [str(path) for path in sorted(outputs, key=str)],
                    "shadow_gates": [
                        {"command": item["command"], "returncode": item["returncode"]}
                        for item in gates
                    ],
                })
                return 0

            if not outputs:
                raise PublishError("投影没有任何变更，且不存在幂等 receipt")
            # 门禁耗时期间，正式依赖与 details 证据也必须保持冻结状态。
            _, _, _, _, final_review_files = _validated_inputs(
                repo, details_root, snapshot_path, review_paths, node_plan_path
            )
            if final_review_files != review_files or payload_identity != _payload_identity(
                snapshot_path, review_files, node_plan_path, args.review_date
            ):
                raise PublishError("影子门禁期间 review/snapshot/node plan 发生变化")
            expected_before_hashes = {
                path: base_hashes[path]
                for path in outputs
                if path in base_hashes
            }
            if set(expected_before_hashes) != set(outputs):
                raise PublishError("投影包含未在门禁前冻结 base hash 的目标")
            items = [
                {
                    "job_id": "",
                    "formal_id": f"ALL-{len(node_reviews)}",
                    "mode": PUBLISH_RECEIPT_MODE,
                }
            ]
            txid, receipt, changed = _commit_transaction(
                repo,
                runtime_root,
                outputs,
                idempotency_key,
                payload_sha,
                items,
                [repo],
                "408-relation-semantic-review-writer",
                None,
                [],
                f"RELATION-SEMANTIC-{args.review_date}",
                {
                    "contract": PUBLISH_RECEIPT_MODE,
                    "snapshot_sha256": sha256_file(snapshot_path),
                    "node_plan_sha256": sha256_file(node_plan_path),
                },
                payload_identity,
                details_root,
                expected_before_hashes=expected_before_hashes,
            )
            _emit({
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
            })
            return 0
    except RepoLockTimeout as exc:
        raise PublishError(str(exc)) from exc
    except TransactionRolledBack as exc:
        raise PublishError(str(exc)) from exc
    except BatchError as exc:
        raise PublishError(str(exc)) from exc


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)

    plan = sub.add_parser("generate-plan", help="生成主知识点迁移草案；每项默认仍需人工核对")
    plan.add_argument("--repo", default=".")
    plan.add_argument("--snapshot", required=True)
    plan.add_argument("--reviews", nargs="+", required=True)
    plan.add_argument("--out", required=True)
    plan.add_argument("--overwrite", action="store_true")
    plan.add_argument(
        "--approve-conservative-defaults",
        action="store_true",
        help="中央写者已逐项核对后，批准保留旧主知识点为副/命中项的保守迁移",
    )
    plan.set_defaults(func=_generate_plan_command)

    publish = sub.add_parser("publish", help="默认 dry-run；显式 --apply 才通过 WAL 发布")
    publish.add_argument("--repo", default=".")
    publish.add_argument("--details-root", default=str(_paths.details_root()))
    publish.add_argument("--runtime-root", default=str(DEFAULT_RUNTIME_ROOT))
    publish.add_argument("--snapshot", required=True)
    publish.add_argument("--reviews", nargs="+", required=True)
    publish.add_argument("--node-plan", required=True)
    publish.add_argument("--review-date", required=True)
    publish.add_argument("--idempotency-key")
    publish.add_argument("--lock-timeout", type=float, default=30.0)
    publish.add_argument("--apply", action="store_true")
    publish.set_defaults(func=_publish_command)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        return int(args.func(args))
    except PublishError as exc:
        print(json.dumps({"status": "ERROR", "error": str(exc)}, ensure_ascii=False), file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
