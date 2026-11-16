#!/usr/bin/env python3
"""Transactionally maintain exactly one existing 408 formal node.

The command is intentionally narrower than the new/redo intake writers and the
full-library semantic publishers.  A package may update only model-derived,
answer-safe fields of one existing node, its directly affected relation rows,
the existing detail card metadata, and explicitly guarded review-unit
projections.  It never allocates, merges, deletes, or migrates a formal node.

``publish`` is a dry run unless ``--apply`` is present.  Formal writes reuse the
repository lock, WAL, idempotency receipt, and compare-and-swap transaction
engine used by the intake pipeline.  ``closeout`` can safely rerun derived
refreshes and audits for an already committed receipt without reapplying the
transaction.
"""

from __future__ import annotations

import argparse
import copy
import hashlib
import json
import re
import subprocess
import sys
from collections import Counter
from pathlib import Path
from typing import Any, Iterable

sys.path.insert(0, str(Path(__file__).resolve().parent))

from apply_relation_semantic_review import (  # noqa: E402
    RELATION_LABELS,
    SUBJECT_PREFIXES,
    SUBJECT_VIEWS,
    PublishError,
    _capture_before_hashes,
    _ensure_card_sections,
    _existing_bullet_section,
    _project_sectioned_table_rows,
    _render_table,
    _run_shadow_gates,
    _split_tags,
    _upsert_bullet,
    pair_key,
    sha256_file,
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
from intake_lib_408 import (  # noqa: E402
    DATE_INDEX_COLS,
    EDGE_COLS,
    FORMAL_ID_RE,
    MASTER_COLS,
    NO_ETAG,
    ORIGINAL_TRACK_COLS,
    REVIEW_UNIT_COLS,
    REVIEW_UNIT_MAPPING_COLS,
    Repo,
    check_etag_item,
    check_kp_item,
    scan_leak,
    split_multi,
)
from intake_lock_408 import RepoLock, RepoLockTimeout  # noqa: E402
from normalize_408_knowledge_points import read_table_with_preamble  # noqa: E402

import runtime_paths_408 as _paths


SCHEMA = "existing_node_maintenance_v1"
RECEIPT_MODE = SCHEMA
ACTOR = "408-existing-node-maintenance-writer"

ALLOWED_MASTER_FIELDS = {
    "来源ID",
    "主模块",
    "主知识点",
    "副知识点",
    "命中知识点",
    "题型",
    "核心考点",
    "模糊概念",
    "错因标签",
    "详情入口",
}
IMMUTABLE_MASTER_FIELDS = {
    "ID",
    "年份",
    "科目",
    "首次做题日期",
    "最近复做日期",
    "最近错误记录",
}

MASTER_TO_CARD = {
    "来源ID": "来源 ID",
    "主模块": "主模块",
    "主知识点": "主知识点",
    "副知识点": "副知识点",
    "命中知识点": "命中知识点",
    "核心考点": "核心考点",
    "模糊概念": "模糊概念",
    "错因标签": "错因标签",
    "详情入口": "详情入口",
}
SAFE_CARD_SECTIONS = {
    "来源 ID": "安全节点卡片",
    "主模块": "安全节点卡片",
    "主知识点": "安全节点卡片",
    "副知识点": "安全节点卡片",
    "命中知识点": "安全节点卡片",
    "详情入口": "安全节点卡片",
    "语义证据等级": "安全节点卡片",
    "关系复核": "安全节点卡片",
    "关系复核日期": "安全节点卡片",
    "安全题目摘要": "复做安全信息",
    "关键题设参数": "复做安全信息",
    "问法类型": "复做安全信息",
    "实际考查机制": "复做安全信息",
    "复做第一动作": "复做安全信息",
    "核心考点": "复做安全信息",
    "模糊概念": "复做安全信息",
    "错因标签": "复做安全信息",
    "题图": "附件登记",
    "解析图或解析正文": "附件登记",
    "复做保护说明": "复做保护",
}
FORBIDDEN_SAFE_LABELS = {
    "正式节点 ID",
    "年份",
    "科目",
    "用户错误入口",
    "首次做题日期",
    "最近复做日期",
    "最近错误记录",
    "作答痕迹",
    "当前附件来源",
}
ALLOWED_DETAILS_LABELS = {
    "来源定位",
    "来源 ID",
    "全卷题号",
    "全卷定位",
    "章节定位",
    "题卷来源",
    "解析来源",
    "解析来源角色",
    "主模块",
    "主知识点",
    "核心考点",
    "题型",
    "匹配方式",
}
FORBIDDEN_DETAILS_LABELS = {"正式节点", "年份", "科目", "用户错误入口", "用户作答"}
ALLOWED_MAPPING_FIELDS = {"证据等级", "实际考点", "覆盖方式", "复核状态", "待补事项"}
ALLOWED_TRACK_FIELDS = {"来源ID", "定位标记", "段落号", "完整来源位置", "详情入口"}
ALLOWED_UNIT_FIELDS = {"模块", "内容名称", "闭卷问题", "派生自查口径"}
PROTECTED_RU_TEXT = {"schedule_state", "review_unit_id", "首次学习日期", "上次复习日期", "下次复习日期", "掌握度", "错误次数"}

EVIDENCE_ROLES = {"safe_card", "detail_card", "question_image", "solution_image"}
REQUIRED_EVIDENCE_ROLES = EVIDENCE_ROLES


class MaintenanceError(RuntimeError):
    """The package or projected formal state violates maintenance policy."""


def _emit(value: dict[str, Any]) -> None:
    print(json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True))


def _load_json(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise MaintenanceError(f"维护包读取失败：{path}: {exc}") from exc
    if not isinstance(value, dict):
        raise MaintenanceError("维护包顶层必须是 object")
    return value


def _safe_value(value: Any, label: str, *, allow_empty: bool = False) -> str:
    if not isinstance(value, str):
        raise MaintenanceError(f"{label} 必须是字符串")
    value = value.strip()
    if not value and not allow_empty:
        raise MaintenanceError(f"{label} 不能为空")
    if any(mark in value for mark in ("\n", "\r", "|")):
        raise MaintenanceError(f"{label} 含换行或 Markdown 表格危险字符")
    fails, warns = scan_leak(value)
    if fails or warns:
        raise MaintenanceError(f"{label} 疑似泄露答案或既往选项：{fails + warns}")
    return value


def _under(child: Path, root: Path) -> bool:
    try:
        child.resolve().relative_to(root.resolve())
        return True
    except ValueError:
        return False


def _resolve_evidence(logical: str, repo: Path, details_root: Path) -> Path:
    if not isinstance(logical, str) or ":" not in logical:
        raise MaintenanceError(f"证据路径格式非法：{logical!r}")
    namespace, relative = logical.split(":", 1)
    if not relative or Path(relative).is_absolute() or ".." in Path(relative).parts:
        raise MaintenanceError(f"证据路径越界：{logical}")
    if namespace == "formal":
        result = (repo / relative).resolve()
        root = repo
    elif namespace == "details":
        result = (details_root / relative).resolve()
        root = details_root
    else:
        raise MaintenanceError(f"未知证据 namespace：{namespace}")
    if not _under(result, root) or not result.is_file() or result.is_symlink():
        raise MaintenanceError(f"证据必须是根目录内的普通文件：{logical}")
    return result


def _validate_evidence(
    package: dict[str, Any], repo: Path, details_root: Path, node_id: str
) -> dict[str, Path]:
    if package.get("answer_verified") is not True:
        raise MaintenanceError("answer_verified 必须为 true")
    records = package.get("evidence")
    if not isinstance(records, list) or not records:
        raise MaintenanceError("evidence 必须是非空数组")
    found: dict[str, Path] = {}
    for item in records:
        if not isinstance(item, dict):
            raise MaintenanceError("evidence 每项必须是 object")
        role = str(item.get("role") or "")
        if role not in EVIDENCE_ROLES:
            raise MaintenanceError(f"证据 role 非法：{role!r}")
        logical = item.get("path")
        expected = item.get("sha256")
        if not isinstance(expected, str) or not re.fullmatch(r"[0-9a-f]{64}", expected):
            raise MaintenanceError(f"证据 SHA-256 非法：{role}")
        resolved = _resolve_evidence(str(logical), repo, details_root)
        actual = sha256_file(resolved)
        if actual != expected:
            raise MaintenanceError(f"证据哈希漂移：{role} {logical}")
        if role in found and role != "solution_image":
            raise MaintenanceError(f"证据 role 重复：{role}")
        found[role] = resolved
    missing = REQUIRED_EVIDENCE_ROLES - set(found)
    if missing:
        raise MaintenanceError(f"证据角色不完整：{sorted(missing)}")
    if found["safe_card"] != (repo / f"{node_id}.md").resolve():
        raise MaintenanceError("safe_card 证据未绑定目标正式 ID")
    if found["detail_card"] != (details_root / "cards" / f"{node_id}.md").resolve():
        raise MaintenanceError("detail_card 证据未绑定目标正式 ID")
    return found


def _rows(path: Path, key: str) -> tuple[list[str], list[str], list[dict[str, str]], dict[str, dict[str, str]]]:
    preamble, headers, rows = read_table_with_preamble(path)
    if not headers or headers[0] != key:
        raise MaintenanceError(f"表格首列不是 {key}：{path}")
    by_key: dict[str, dict[str, str]] = {}
    for row in rows:
        value = row.get(key, "")
        if not value or value in by_key:
            raise MaintenanceError(f"表格主键为空或重复：{path} {value!r}")
        by_key[value] = row
    return preamble, headers, rows, by_key


def _validate_master_change(
    package: dict[str, Any], repo: Path, node_id: str
) -> tuple[list[str], list[str], list[dict[str, str]], dict[str, str], dict[str, str]]:
    preamble, headers, rows, by_id = _rows(repo / "节点总表.md", "ID")
    if node_id not in by_id:
        raise MaintenanceError(f"正式节点不存在：{node_id}")
    before = copy.deepcopy(by_id[node_id])
    expected_root = package.get("expected")
    changes_root = package.get("changes")
    if not isinstance(expected_root, dict) or not isinstance(changes_root, dict):
        raise MaintenanceError("expected 与 changes 必须是 object")
    expected = expected_root.get("master")
    changes = changes_root.get("master", {})
    if not isinstance(expected, dict) or not isinstance(changes, dict):
        raise MaintenanceError("expected.master 与 changes.master 必须是 object")
    required_guards = set(changes) | IMMUTABLE_MASTER_FIELDS
    if not required_guards <= set(expected):
        raise MaintenanceError(
            "expected.master 必须覆盖全部修改字段及 ID/年份/科目/日期/错误事实"
        )
    for field, value in expected.items():
        if field not in MASTER_COLS:
            raise MaintenanceError(f"expected.master 含未知字段：{field}")
        if before[field] != value:
            raise MaintenanceError(f"节点总表 expected 漂移：{node_id}.{field}")
    unknown = set(changes) - ALLOWED_MASTER_FIELDS
    if unknown:
        raise MaintenanceError(f"禁止维护这些正式字段：{sorted(unknown)}")
    after = copy.deepcopy(before)
    for field, raw in changes.items():
        after[field] = _safe_value(raw, f"changes.master.{field}", allow_empty=field == "副知识点")
    if before["ID"] != after["ID"] or before["年份"] != after["年份"] or before["科目"] != after["科目"]:
        raise MaintenanceError("既有节点维护禁止 ID、年份或科目迁移")
    if node_id.split("_", 1)[0] != SUBJECT_PREFIXES.get(after["科目"]):
        raise MaintenanceError("正式 ID 前缀与科目不一致")

    repo_view = Repo(repo)
    modules, leaves = repo_view.kp_tags()
    for field in ("主知识点", "副知识点", "命中知识点"):
        # 正式库的历史节点允许副知识点使用“待补充”等规范占位值。
        # 维护其他字段时不能把这种既有合法状态误判为知识点编码；主知识点
        # 与命中知识点仍须解析出至少一个真实标签。
        values = _split_tags(after[field])
        if field != "副知识点" and not values:
            raise MaintenanceError(f"{field} 不能为空")
        for item in values:
            error = check_kp_item(item, leaves)
            if error:
                raise MaintenanceError(error)
    main_code = after["主知识点"].split()[0]
    expected_module = f"{main_code[:4]} {modules.get(main_code[:4], '')}".strip()
    if after["主模块"] != expected_module:
        raise MaintenanceError(f"主模块必须与主知识点同章：期望 {expected_module}")
    if after["主知识点"] in _split_tags(after["副知识点"]):
        raise MaintenanceError("主知识点不得重复出现在副知识点")
    if after["主知识点"] not in _split_tags(after["命中知识点"]):
        raise MaintenanceError("命中知识点必须包含主知识点")

    etags = split_multi(after["错因标签"])
    if not etags:
        raise MaintenanceError("错因标签不能为空")
    if NO_ETAG in etags and len(etags) != 1:
        raise MaintenanceError("暂无明确错因不能与 E 标签混用")
    for item in etags:
        error = check_etag_item(item, repo_view.etags())
        if error:
            raise MaintenanceError(error)

    new_source = after["来源ID"]
    if new_source and new_source not in {"VISUAL_PENDING", "待补充", "未记录"}:
        conflicts = sorted(
            row["ID"] for row in rows
            if row["ID"] != node_id and row.get("来源ID") == new_source
        )
        if conflicts:
            raise MaintenanceError(f"来源 ID 已被其他节点占用：{new_source} -> {conflicts}")
    return preamble, headers, rows, before, after


def _project_master_and_indexes(
    repo: Path,
    node_id: str,
    preamble: list[str],
    headers: list[str],
    rows: list[dict[str, str]],
    after: dict[str, str],
    changed_fields: set[str],
) -> tuple[dict[Path, bytes], dict[str, dict[str, str]], dict[str, dict[str, str]]]:
    before_rows = {row["ID"]: copy.deepcopy(row) for row in rows}
    after_rows = {row["ID"]: row for row in rows}
    target = after_rows[node_id]
    target.clear()
    target.update(after)
    outputs: dict[Path, bytes] = {}
    master_path = (repo / "节点总表.md").resolve()
    master_data = _render_table(preamble, headers, rows)
    if master_data != master_path.read_bytes():
        outputs[master_path] = master_data

    index_changes: dict[str, dict[str, str]] = {}
    year_map = {"来源ID": "来源ID", "主模块": "模块", "主知识点": "主知识点", "详情入口": "详情入口"}
    date_map = {"来源ID": "来源ID", "主知识点": "主知识点"}
    for source_field, index_field in year_map.items():
        if source_field in changed_fields:
            index_changes.setdefault("年份索引.md", {})[index_field] = after[source_field]
    for source_field, index_field in date_map.items():
        if source_field in changed_fields:
            index_changes.setdefault("错题日期索引.md", {})[index_field] = after[source_field]
    for relative, changes in index_changes.items():
        index_path = (repo / relative).resolve()
        data = _project_sectioned_table_rows(index_path, {node_id: changes})
        if data != index_path.read_bytes():
            outputs[index_path] = data

    # Rebuild only the target subject mirror.  Re-rendering unrelated subject
    # views would turn harmless pre-existing formatting differences into
    # out-of-scope writes in a batch-size-one transaction.
    subject = after["科目"]
    relative = SUBJECT_VIEWS[subject]
    view_path = (repo / relative).resolve()
    view_preamble, view_headers, _ = read_table_with_preamble(view_path)
    prefix = SUBJECT_PREFIXES[subject]
    view_rows = [
        {header: row.get(header, "") for header in view_headers}
        for row in rows if row.get("ID", "").startswith(prefix + "_")
    ]
    view_rows.sort(key=lambda row: row.get("ID", ""))
    data = _render_table(view_preamble, view_headers, view_rows)
    if data != view_path.read_bytes():
        outputs[view_path] = data

    if changed_fields & {"主知识点", "命中知识点", "核心考点", "错因标签"}:
        hit_data = _project_hit_index_one(repo, node_id, after)
        hit_path = (repo / "知识点命中索引.md").resolve()
        if hit_data != hit_path.read_bytes():
            outputs[hit_path] = hit_data
    return outputs, before_rows, after_rows


def _project_hit_index_one(repo: Path, node_id: str, row: dict[str, str]) -> bytes:
    """Surgically reconcile one node in the knowledge-hit index.

    The full-library semantic publisher rebuilds the whole index, which is
    appropriate for a frozen global review but can normalize unrelated blank
    lines.  A batch-size-one writer instead removes and reinserts only the
    target node while preserving every other line.
    """
    index_path = repo / "知识点命中索引.md"
    original = index_path.read_text(encoding="utf-8")
    lines = original.splitlines()
    heading_re = re.compile(r"^###\s+((?:DS|CO|OS|CN)\d{2}-\d{2}\s+.+)$")
    node_line_re = re.compile(rf"^-\s*{re.escape(node_id)}：")

    # Remove only the target entries from every existing section.
    lines = [line for line in lines if not node_line_re.match(line.strip())]

    main = row["主知识点"]
    hits = _split_tags(row["命中知识点"])
    if main not in hits:
        hits.insert(0, main)
    desired: dict[str, dict[str, str]] = {}
    for knowledge in hits:
        desired[knowledge] = {
            "主知识点命中" if knowledge == main else "命中知识点": (
                f"- {node_id}：{row['核心考点']}；最近复做日期：{row['最近复做日期']}。"
                if knowledge == main
                else f"- {node_id}：{row['核心考点']}（命中，主知识点 {main.split()[0]}）；"
                f"最近复做日期：{row['最近复做日期']}。"
            ),
            "错因触发记录": f"- {node_id}：{row['错因标签']}。",
        }

    def section_ranges(current: list[str]) -> dict[str, tuple[int, int]]:
        starts: list[tuple[str, int]] = []
        for index, line in enumerate(current):
            match = heading_re.match(line)
            if match:
                starts.append((match.group(1), index))
        return {
            knowledge: (start, starts[pos + 1][1] if pos + 1 < len(starts) else len(current))
            for pos, (knowledge, start) in enumerate(starts)
        }

    # Create a missing knowledge section in code order.  This is rare but is
    # safer than silently omitting a newly verified hit.
    for knowledge in sorted(desired):
        ranges = section_ranges(lines)
        if knowledge in ranges:
            continue
        insert_at = len(lines)
        for existing, (start, _) in ranges.items():
            if existing.split()[0] > knowledge.split()[0]:
                insert_at = start
                break
        new_section = [
            f"### {knowledge}",
            "",
            "- 节点数量：0",
            "",
            "#### 主知识点命中",
            "",
            "#### 错因触发记录",
            "",
            "---",
            "",
        ]
        lines[insert_at:insert_at] = new_section

    def insert_block_entry(
        current: list[str], start: int, end: int, block: str, entry: str
    ) -> None:
        block_heading = f"#### {block}"
        block_start = next(
            (index for index in range(start + 1, end) if current[index].strip() == block_heading),
            None,
        )
        if block_start is None:
            # Keep the established block order: main, error, ordinary hit.
            if block == "命中知识点":
                insert_at = next(
                    (index for index in range(start + 1, end) if current[index].strip() == "---"),
                    end,
                )
            elif block == "错因触发记录":
                insert_at = next(
                    (index for index in range(start + 1, end) if current[index].strip() == "#### 命中知识点"),
                    next((index for index in range(start + 1, end) if current[index].strip() == "---"), end),
                )
            else:
                insert_at = next(
                    (index for index in range(start + 1, end) if current[index].startswith("#### ")),
                    next((index for index in range(start + 1, end) if current[index].strip() == "---"), end),
                )
            current[insert_at:insert_at] = [block_heading, ""]
            block_start = insert_at
            end += 2
        block_end = next(
            (
                index for index in range(block_start + 1, min(end, len(current)))
                if current[index].startswith("#### ") or current[index].strip() == "---"
            ),
            min(end, len(current)),
        )
        entries = [
            (index, line) for index, line in enumerate(current[block_start + 1:block_end], block_start + 1)
            if re.match(r"^-\s*(?:DS|CO|OS|CN)_(?:\d{4}|UNK)_\d{3}：", line.strip())
        ]
        target_key = node_id
        insert_at = block_end
        for index, existing_line in entries:
            match = re.match(r"^-\s*((?:DS|CO|OS|CN)_(?:\d{4}|UNK)_\d{3})：", existing_line.strip())
            if match and match.group(1) > target_key:
                insert_at = index
                break
        current.insert(insert_at, entry)

    for knowledge in sorted(desired):
        for block, entry in desired[knowledge].items():
            ranges = section_ranges(lines)
            start, end = ranges[knowledge]
            insert_block_entry(lines, start, end, block, entry)

    # Recount only affected sections from unique formal IDs in their section.
    ranges = section_ranges(lines)
    formal_re = re.compile(r"\b(?:DS|CO|OS|CN)_(?:\d{4}|UNK)_\d{3}\b")
    affected = set(desired)
    # Also recount sections from which a stale target entry was removed.
    for match in re.finditer(rf"(?ms)^###\s+([^\n]+).*?^-\s*{re.escape(node_id)}：", original):
        affected.add(match.group(1).strip())
    for knowledge in sorted(affected):
        if knowledge not in ranges:
            continue
        start, end = ranges[knowledge]
        count = len(set(formal_re.findall("\n".join(lines[start:end]))))
        count_line = next(
            (index for index in range(start + 1, end) if re.match(r"^-\s*节点数量：", lines[index].strip())),
            None,
        )
        if count_line is None:
            lines.insert(start + 1, f"- 节点数量：{count}")
            ranges = section_ranges(lines)
        else:
            lines[count_line] = f"- 节点数量：{count}"

    projected = "\n".join(lines) + ("\n" if original.endswith("\n") else "")
    return projected.encode("utf-8")


def _card_section(text: str, label: str) -> str:
    existing = _existing_bullet_section(text, label, set(SAFE_CARD_SECTIONS.values()))
    return existing or SAFE_CARD_SECTIONS[label]


def _project_safe_card(
    repo: Path,
    node_id: str,
    master_changes: dict[str, str],
    explicit_changes: dict[str, Any],
    relation_status: str | None,
    review_date: str,
) -> tuple[Path, bytes]:
    card_path = (repo / f"{node_id}.md").resolve()
    text = _ensure_card_sections(card_path.read_text(encoding="utf-8"), node_id)
    merged: dict[str, str] = {}
    for field, value in master_changes.items():
        if field in MASTER_TO_CARD:
            # The formal table represents an intentionally empty secondary
            # knowledge-point set with an empty cell.  Safe cards, however,
            # require every bullet to carry an explicit, non-empty value.
            # Preserve the formal empty-set semantics while rendering it as
            # the answer-safe human-readable marker "无" on the card.
            rendered = "无" if field == "副知识点" and not value else value
            merged[MASTER_TO_CARD[field]] = rendered
    if not isinstance(explicit_changes, dict):
        raise MaintenanceError("changes.safe_card 必须是 object")
    for label, raw in explicit_changes.items():
        if label in FORBIDDEN_SAFE_LABELS or label not in SAFE_CARD_SECTIONS:
            raise MaintenanceError(f"安全卡字段禁止维护：{label}")
        merged[label] = _safe_value(raw, f"changes.safe_card.{label}")
    if relation_status:
        merged["关系复核"] = relation_status
        merged["关系复核日期"] = review_date
    for label, value in merged.items():
        text = _upsert_bullet(
            text,
            label,
            value,
            section_heading=_card_section(text, label),
        )
    data = (text.rstrip("\n") + "\n").encode("utf-8")
    fails, warns = scan_leak(data.decode("utf-8"))
    if fails or warns:
        raise MaintenanceError(f"安全卡投影触发答案泄露门禁：{fails + warns}")
    return card_path, data


def _upsert_detail_metadata(text: str, label: str, value: str) -> str:
    lines = text.splitlines()
    boundary = next(
        (index for index, line in enumerate(lines) if line.startswith("> [!question]")),
        len(lines),
    )
    pattern = re.compile(rf"^{re.escape(label)}[：:]\s*.*$")
    matches = [index for index in range(boundary) if pattern.match(lines[index])]
    replacement = f"{label}：{value}"
    if len(matches) > 1:
        raise MaintenanceError(f"details 顶部字段重复：{label}")
    if matches:
        lines[matches[0]] = replacement
    else:
        insert_at = next(
            (index for index in range(boundary) if lines[index].startswith("匹配方式：")),
            next((index for index in range(boundary) if lines[index].startswith("## ")), boundary),
        )
        lines.insert(insert_at, replacement)
    return "\n".join(lines) + ("\n" if text.endswith("\n") else "")


def _project_details(
    details_root: Path, node_id: str, changes: dict[str, Any]
) -> tuple[Path, bytes] | None:
    if not changes:
        return None
    if not isinstance(changes, dict):
        raise MaintenanceError("changes.details_metadata 必须是 object")
    unknown = set(changes) - ALLOWED_DETAILS_LABELS
    if unknown or set(changes) & FORBIDDEN_DETAILS_LABELS:
        raise MaintenanceError(f"details 顶部字段禁止维护：{sorted(unknown)}")
    details_path = (details_root / "cards" / f"{node_id}.md").resolve()
    text = details_path.read_text(encoding="utf-8")
    boundary_match = re.search(r"^> \[!question\].*$", text, flags=re.MULTILINE)
    suffix = text[boundary_match.start():] if boundary_match else ""
    for label, raw in changes.items():
        value = _safe_value(raw, f"changes.details_metadata.{label}")
        text = _upsert_detail_metadata(text, label, value)
    if boundary_match:
        new_match = re.search(r"^> \[!question\].*$", text, flags=re.MULTILINE)
        if not new_match or text[new_match.start():] != suffix:
            raise MaintenanceError("details 题图、解析或 QA 正文发生非授权变化")
    return details_path, (text.rstrip("\n") + "\n").encode("utf-8")


def _edge_short(row: dict[str, str]) -> dict[str, str]:
    return {
        "type": row["关系类型"].split()[0],
        "strength": row["联系强度"],
        "reason": row["关联原因"],
        "priority": row["复盘优先级"],
    }


def _validate_relation_row(
    row: dict[str, str], nodes: dict[str, dict[str, str]]
) -> None:
    a, b = row["起点ID"], row["终点ID"]
    if a == b or a not in nodes or b not in nodes:
        raise MaintenanceError(f"关系端点非法：{a}::{b}")
    code = row["关系类型"].split()[0]
    if code not in RELATION_LABELS:
        raise MaintenanceError(f"关系类型非法：{code}")
    if row["联系强度"] not in {"强", "中", "弱"} or row["复盘优先级"] not in {"高", "中", "低"}:
        raise MaintenanceError("关系强度或复盘优先级非法")
    reason = _safe_value(row["关联原因"], "关系理由")
    if len(reason) < 10:
        raise MaintenanceError("关系理由必须是题目特异、至少十个字符")
    if code in {"R07", "R08"} and row["联系强度"] != "弱":
        raise MaintenanceError(f"{code} 只能为弱关系")
    if code == "R07" and a[:2] == b[:2]:
        raise MaintenanceError("R07 必须跨科目")
    if code == "R08":
        if nodes[a]["年份"] != nodes[b]["年份"] or nodes[a]["主模块"] != nodes[b]["主模块"]:
            raise MaintenanceError("R08 必须同年份、同模块")
    if code == "R09" and (a[:2] != b[:2] or nodes[a]["主知识点"][:4] != nodes[b]["主知识点"][:4]):
        raise MaintenanceError("R09 必须同科目、同主知识章节")


def _project_relations(
    repo: Path,
    node_id: str,
    changes: list[Any],
    nodes: dict[str, dict[str, str]],
) -> tuple[Path, bytes, dict[str, str], set[str]] | None:
    if not changes:
        return None
    if not isinstance(changes, list):
        raise MaintenanceError("changes.relations 必须是数组")
    edge_path = (repo / "关系边表.md").resolve()
    preamble, headers, rows = read_table_with_preamble(edge_path)
    projected = [copy.deepcopy(row) for row in rows]
    initial_keys = {
        pair_key(row["起点ID"], row["终点ID"])
        for row in projected
    }
    locations = {
        pair_key(row["起点ID"], row["终点ID"]): index
        for index, row in enumerate(projected)
    }
    if len(locations) != len(projected):
        raise MaintenanceError("关系边表已有重复无序题对")
    touched: set[str] = {node_id}
    seen: set[str] = set()
    for item in changes:
        if not isinstance(item, dict):
            raise MaintenanceError("relation patch 每项必须是 object")
        if item.get("confidence") != "high":
            raise MaintenanceError("只允许发布 high 置信关系修正")
        action = str(item.get("action") or "")
        a, b = str(item.get("a") or ""), str(item.get("b") or "")
        if node_id not in {a, b}:
            raise MaintenanceError("每条关系修正必须直接连接目标节点")
        key = pair_key(a, b)
        if key in seen:
            raise MaintenanceError(f"同一维护包重复关系题对：{key}")
        seen.add(key)
        touched.update({a, b})
        current_index = locations.get(key)
        current = projected[current_index] if current_index is not None else None
        expected = item.get("expected")
        if action in {"remove", "replace"}:
            if current is None or not isinstance(expected, dict) or _edge_short(current) != expected:
                raise MaintenanceError(f"关系 expected 漂移：{key}")
        elif action == "add":
            if current is not None or expected not in (None, {}):
                raise MaintenanceError(f"新增关系题对已存在或 expected 非空：{key}")
        else:
            raise MaintenanceError(f"关系 action 非法：{action}")
        if action == "remove":
            assert current_index is not None
            projected.pop(current_index)
            locations = {
                pair_key(row["起点ID"], row["终点ID"]): index
                for index, row in enumerate(projected)
            }
            continue
        after = item.get("after")
        if not isinstance(after, dict):
            raise MaintenanceError(f"关系 {action} 缺 after：{key}")
        code = str(after.get("type") or "")
        row = {
            "起点ID": min(a, b),
            "终点ID": max(a, b),
            "关系类型": f"{code} {RELATION_LABELS.get(code, '')}".strip(),
            "联系强度": str(after.get("strength") or ""),
            "关联原因": str(after.get("reason") or (current or {}).get("关联原因") or ""),
            "复盘优先级": str(after.get("priority") or (current or {}).get("复盘优先级") or ""),
        }
        _validate_relation_row(row, nodes)
        if current_index is None:
            projected.append(row)
        else:
            projected[current_index] = row
        locations = {
            pair_key(existing["起点ID"], existing["终点ID"]): index
            for index, existing in enumerate(projected)
        }
    degree = Counter()
    for row in projected:
        _validate_relation_row(row, nodes)
        degree[row["起点ID"]] += 1
        degree[row["终点ID"]] += 1
    statuses = {
        formal_id: "connected" if degree[formal_id] else "reviewed_no_reliable_edge"
        for formal_id in touched
    }
    final_by_pair = {
        pair_key(row["起点ID"], row["终点ID"]): row
        for row in projected
    }

    # 单节点维护只改直接命中的关系行。不要把整张关系表交给 renderer，
    # 否则即使语义未变也会重排或规范化无关边的空格与文本表面。
    original = edge_path.read_text(encoding="utf-8")
    original_lines = original.splitlines()
    row_key_by_line: dict[int, str] = {}
    for index, line in enumerate(original_lines):
        stripped = line.strip()
        if not (stripped.startswith("|") and stripped.endswith("|")):
            continue
        cells = [cell.strip() for cell in stripped[1:-1].split("|")]
        if len(cells) != len(headers) or not FORMAL_ID_RE.fullmatch(cells[0]) or not FORMAL_ID_RE.fullmatch(cells[1]):
            continue
        row_key_by_line[index] = pair_key(cells[0], cells[1])
    if set(row_key_by_line.values()) != initial_keys or len(row_key_by_line) != len(initial_keys):
        raise MaintenanceError("关系边表原始行定位与解析结果不一致")

    changed_keys = set(seen)
    added_keys = sorted(key for key in seen if key not in initial_keys and key in final_by_pair)
    if row_key_by_line:
        insertion_index = max(row_key_by_line)
    else:
        separator_indexes = [
            index
            for index, line in enumerate(original_lines)
            if line.strip().startswith("|")
            and line.strip().endswith("|")
            and all(
                cell.strip().replace("-", "") == ""
                for cell in line.strip()[1:-1].split("|")
            )
        ]
        if len(separator_indexes) != 1:
            raise MaintenanceError("空关系边表缺唯一表头分隔行")
        insertion_index = separator_indexes[0]
    out_lines: list[str] = []
    for index, line in enumerate(original_lines):
        key = row_key_by_line.get(index)
        if key in changed_keys:
            final = final_by_pair.get(key)
            if final is not None:
                out_lines.append("|" + "|".join(final.get(header, "") for header in headers) + "|")
        else:
            out_lines.append(line)
        if index == insertion_index:
            for added_key in added_keys:
                final = final_by_pair[added_key]
                out_lines.append("|" + "|".join(final.get(header, "") for header in headers) + "|")
    data = ("\n".join(out_lines) + ("\n" if original.endswith("\n") else "")).encode("utf-8")
    return edge_path, data, statuses, touched


def _apply_row_patch(
    path: Path,
    key_field: str,
    key_value: str,
    expected: dict[str, Any],
    after: dict[str, Any],
    allowed: set[str],
) -> bytes:
    _, headers, _, by_key = _rows(path, key_field)
    if key_value not in by_key:
        raise MaintenanceError(f"表格缺目标行：{path.name} {key_value}")
    row = by_key[key_value]
    if not isinstance(expected, dict) or not isinstance(after, dict):
        raise MaintenanceError(f"{path.name} expected/after 必须是 object")
    if set(after) - allowed:
        raise MaintenanceError(f"{path.name} 含禁止字段：{sorted(set(after) - allowed)}")
    if not set(after) <= set(expected):
        raise MaintenanceError(f"{path.name} expected 必须覆盖全部修改字段")
    for field, value in expected.items():
        if field not in headers or row[field] != value:
            raise MaintenanceError(f"{path.name} expected 漂移：{key_value}.{field}")
    for field, raw in after.items():
        row[field] = _safe_value(raw, f"{path.name}.{field}", allow_empty=False)
    # Preserve every non-target byte.  Some review-unit tables intentionally use
    # spaced Markdown cells, whereas the canonical renderer uses compact cells;
    # rebuilding the full table would therefore create hundreds of unrelated
    # formatting changes in a one-node transaction.
    original = path.read_text(encoding="utf-8")
    lines = original.splitlines()

    def split_cells(line: str) -> list[str]:
        stripped = line.strip()
        if not (stripped.startswith("|") and stripped.endswith("|")):
            return []
        return [cell.strip() for cell in stripped[1:-1].split("|")]

    active_headers: list[str] | None = None
    updated = 0
    rendered: list[str] = []
    for line in lines:
        cells = split_cells(line)
        if cells and cells[0] == key_field:
            active_headers = cells
            rendered.append(line)
            continue
        if line.startswith("## "):
            active_headers = None
            rendered.append(line)
            continue
        if active_headers and cells and cells[0] == key_value:
            if len(cells) != len(active_headers):
                raise MaintenanceError(f"{path.name} 目标行列数异常：{key_value}")
            current = dict(zip(active_headers, cells))
            for field, raw in after.items():
                current[field] = _safe_value(raw, f"{path.name}.{field}")
            rendered.append("|" + "|".join(current[field] for field in active_headers) + "|")
            updated += 1
        else:
            rendered.append(line)
    if updated != 1:
        raise MaintenanceError(f"{path.name} 目标行必须恰好更新一次：{key_value} -> {updated}")
    return ("\n".join(rendered) + ("\n" if original.endswith("\n") else "")).encode("utf-8")


def _project_review_unit(
    repo: Path,
    node_id: str,
    spec: dict[str, Any],
) -> dict[Path, bytes]:
    if not spec:
        return {}
    if not isinstance(spec, dict):
        raise MaintenanceError("changes.review_unit 必须是 object")
    allowed_groups = {"mapping", "original_track", "unit", "card_id", "card_replacements"}
    unknown_groups = set(spec) - allowed_groups
    if unknown_groups:
        raise MaintenanceError(f"changes.review_unit 含未知组：{sorted(unknown_groups)}")
    outputs: dict[Path, bytes] = {}
    if spec.get("mapping"):
        item = spec["mapping"]
        path = (repo / "复习单元节点映射.md").resolve()
        data = _apply_row_patch(
            path, "正式节点ID", node_id, item.get("expected"), item.get("after"), ALLOWED_MAPPING_FIELDS
        )
        if data != path.read_bytes():
            outputs[path] = data
    if spec.get("original_track"):
        item = spec["original_track"]
        path = (repo / "原题复做轨总表.md").resolve()
        data = _apply_row_patch(
            path, "正式节点ID", node_id, item.get("expected"), item.get("after"), ALLOWED_TRACK_FIELDS
        )
        if data != path.read_bytes():
            outputs[path] = data
    if spec.get("unit"):
        item = spec["unit"]
        unit_id = str(item.get("id") or "")
        mapping_path = repo / "复习单元节点映射.md"
        _, _, mapping_rows = read_table_with_preamble(mapping_path)
        members = sorted(
            row["正式节点ID"] for row in mapping_rows
            if row.get("主复习单元ID") == unit_id
        )
        if members != [node_id]:
            raise MaintenanceError(f"共享 RU 不允许改单元级字段：{unit_id} members={members}")
        path = (repo / "复习单元总表.md").resolve()
        data = _apply_row_patch(
            path, "复习单元ID", unit_id, item.get("expected"), item.get("after"), ALLOWED_UNIT_FIELDS
        )
        if data != path.read_bytes():
            outputs[path] = data
    replacements = spec.get("card_replacements") or []
    if replacements:
        if not isinstance(replacements, list):
            raise MaintenanceError("review_unit.card_replacements 必须是数组")
        unit_id = str(spec.get("card_id") or "")
        if not re.fullmatch(r"RU_[A-Z0-9_]+", unit_id):
            raise MaintenanceError("review_unit.card_id 非法")
        mapping_path = repo / "复习单元节点映射.md"
        _, _, _, mapping_by_id = _rows(mapping_path, "正式节点ID")
        if node_id not in mapping_by_id:
            raise MaintenanceError(f"复习单元映射缺目标节点：{node_id}")
        mapping_row = mapping_by_id[node_id]
        linked_units = {
            value
            for field in ("主复习单元ID", "次复习单元ID")
            for value in split_multi(mapping_row.get(field, ""))
            if value
        }
        if unit_id not in linked_units:
            raise MaintenanceError(f"RU 卡不属于目标节点：{node_id} -> {unit_id}")
        _, _, mapping_rows = read_table_with_preamble(mapping_path)
        unit_members = {
            row["正式节点ID"]
            for row in mapping_rows
            if unit_id in split_multi(
                "；".join((row.get("主复习单元ID", ""), row.get("次复习单元ID", "")))
            )
        }
        path = (repo / "复习单元卡" / f"{unit_id}.md").resolve()
        text = path.read_text(encoding="utf-8")
        for replacement in replacements:
            if not isinstance(replacement, dict):
                raise MaintenanceError("RU 卡替换项必须是 object")
            before = replacement.get("expected")
            after = replacement.get("after")
            if not isinstance(before, str) or not isinstance(after, str) or not before:
                raise MaintenanceError("RU 卡 expected/after 必须是非空字符串")
            if text.count(before) != 1:
                raise MaintenanceError(f"RU 卡 expected 不唯一或漂移：{unit_id}")
            if len(unit_members) > 1 and (node_id not in before or node_id not in after):
                raise MaintenanceError("共享 RU 卡只允许替换显式绑定目标节点的贡献行")
            if any(marker in before or marker in after for marker in PROTECTED_RU_TEXT):
                raise MaintenanceError("RU 卡替换触及受保护调度或历史字段")
            _safe_value(after, "RU 卡 after")
            text = text.replace(before, after, 1)
        data = (text.rstrip("\n") + "\n").encode("utf-8")
        fails, warns = scan_leak(data.decode("utf-8"))
        if fails or warns:
            raise MaintenanceError(f"RU 卡投影触发答案泄露门禁：{fails + warns}")
        if data != path.read_bytes():
            outputs[path] = data
    return outputs


def _validate_cross_surface_requirements(
    master_changes: dict[str, str], safe_changes: dict[str, Any], review_unit: dict[str, Any]
) -> None:
    if master_changes.keys() & {"来源ID", "详情入口"} and not review_unit.get("original_track"):
        raise MaintenanceError("来源或详情入口变化必须同步原题复做轨")
    if "主知识点" in master_changes and not review_unit.get("card_replacements"):
        raise MaintenanceError("主知识点变化必须同步 RU 卡中的目标节点知识点贡献")
    semantic_safe = {"实际考查机制", "复做第一动作"} & set(safe_changes)
    if ("核心考点" in master_changes or semantic_safe) and not (
        review_unit.get("mapping") or review_unit.get("unit") or review_unit.get("card_replacements")
    ):
        raise MaintenanceError("核心机制或第一动作变化必须显式同步复习单元")


def _payload_identity(
    package_path: Path,
    package: dict[str, Any],
    repo: Path,
    details_root: Path,
) -> dict[str, Any]:
    return {
        "mode": RECEIPT_MODE,
        "package": {"path": str(package_path), "sha256": sha256_file(package_path)},
        "node_id": package.get("node_id"),
        "review_date": package.get("review_date"),
        "repo_root": str(repo.resolve()),
        "details_root": str(details_root.resolve()),
    }


def _project_all(
    package: dict[str, Any], repo: Path, details_root: Path
) -> tuple[dict[Path, bytes], dict[str, Any]]:
    if package.get("schema_version") != SCHEMA:
        raise MaintenanceError(f"schema_version 必须为 {SCHEMA}")
    node_id = str(package.get("node_id") or "")
    if not FORMAL_ID_RE.fullmatch(node_id):
        raise MaintenanceError(f"正式节点 ID 非法：{node_id}")
    review_date = str(package.get("review_date") or "")
    if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", review_date):
        raise MaintenanceError("review_date 必须是 YYYY-MM-DD")
    _validate_evidence(package, repo, details_root, node_id)
    changes = package.get("changes")
    if not isinstance(changes, dict):
        raise MaintenanceError("changes 必须是 object")
    allowed_change_groups = {"master", "safe_card", "details_metadata", "relations", "review_unit"}
    if set(changes) - allowed_change_groups:
        raise MaintenanceError(f"changes 含未知组：{sorted(set(changes) - allowed_change_groups)}")
    master_changes = changes.get("master", {})
    safe_changes = changes.get("safe_card", {})
    details_changes = changes.get("details_metadata", {})
    relation_changes = changes.get("relations", [])
    review_unit = changes.get("review_unit", {})
    typed_groups = {
        "changes.master": (master_changes, dict),
        "changes.safe_card": (safe_changes, dict),
        "changes.details_metadata": (details_changes, dict),
        "changes.relations": (relation_changes, list),
        "changes.review_unit": (review_unit, dict),
    }
    for label, (value, expected_type) in typed_groups.items():
        if not isinstance(value, expected_type):
            raise MaintenanceError(f"{label} 必须是 {expected_type.__name__}")
    _validate_cross_surface_requirements(master_changes, safe_changes, review_unit)

    preamble, headers, rows, before, after = _validate_master_change(package, repo, node_id)
    outputs, _, after_rows = _project_master_and_indexes(
        repo, node_id, preamble, headers, rows, after, set(master_changes)
    )
    relation_projection = _project_relations(
        repo, node_id, relation_changes, after_rows
    )
    relation_statuses: dict[str, str] = {}
    touched_relation_nodes: set[str] = set()
    if relation_projection:
        relation_path, relation_data, relation_statuses, touched_relation_nodes = relation_projection
        if relation_data != relation_path.read_bytes():
            outputs[relation_path] = relation_data

    card_path, card_data = _project_safe_card(
        repo,
        node_id,
        master_changes,
        safe_changes,
        relation_statuses.get(node_id),
        review_date,
    )
    if card_data != card_path.read_bytes():
        outputs[card_path] = card_data

    for endpoint in sorted(touched_relation_nodes - {node_id}):
        endpoint_path, endpoint_data = _project_safe_card(
            repo, endpoint, {}, {}, relation_statuses[endpoint], review_date
        )
        if endpoint_data != endpoint_path.read_bytes():
            outputs[endpoint_path] = endpoint_data

    detail_projection = _project_details(
        details_root, node_id, details_changes
    )
    if detail_projection:
        detail_path, detail_data = detail_projection
        if detail_data != detail_path.read_bytes():
            outputs[detail_path] = detail_data

    for output_path, data in _project_review_unit(repo, node_id, review_unit).items():
        if output_path in outputs and outputs[output_path] != data:
            raise MaintenanceError(f"事务投影目标冲突：{output_path}")
        outputs[output_path] = data

    if not outputs:
        raise MaintenanceError("维护包没有产生任何变化")
    return outputs, {
        "node_id": node_id,
        "master_fields": sorted(master_changes),
        "safe_card_fields": sorted(safe_changes),
        "details_fields": sorted(details_changes),
        "relation_changes": len(relation_changes),
        "files_changed": len(outputs),
        "before_master": before,
        "after_master": after,
    }


def _run_closeout(
    repo: Path, details_root: Path, review_date: str, receipt_path: Path
) -> list[dict[str, Any]]:
    receipt = _read_json_object(receipt_path)
    if receipt.get("status") != "COMMITTED":
        raise MaintenanceError(f"receipt 不是 COMMITTED：{receipt_path}")
    if receipt.get("actor") != ACTOR:
        raise MaintenanceError(f"receipt 不属于既有节点维护 writer：{receipt_path}")
    journal_path = Path(str(receipt.get("journal") or "")).resolve()
    journal = _read_json_object(journal_path)
    identity = journal.get("payload_identity")
    if not isinstance(identity, dict) or identity.get("mode") != RECEIPT_MODE:
        raise MaintenanceError(f"receipt payload mode 不匹配：{receipt_path}")
    if Path(str(journal.get("repo_root") or "")).resolve() != repo.resolve():
        raise MaintenanceError("receipt 绑定的正式仓库不匹配")
    if Path(str(journal.get("details_root") or "")).resolve() != details_root.resolve():
        raise MaintenanceError("receipt 绑定的 details 根不匹配")
    if identity.get("repo_root") != str(repo.resolve()) or identity.get("details_root") != str(
        details_root.resolve()
    ):
        raise MaintenanceError("receipt payload 根目录绑定不匹配")
    if identity.get("review_date") != review_date:
        raise MaintenanceError("receipt 绑定的 review_date 不匹配")
    for item in journal.get("files") or []:
        target = Path(str(item.get("target") or "")).resolve()
        expected = item.get("after_sha256")
        if not target.is_file() or not isinstance(expected, str) or sha256_file(target) != expected:
            raise MaintenanceError(f"receipt after hash 验收失败：{target}")
    commands = [
        [sys.executable, str(repo / "scripts/refresh_coverage_index.py"), "--repo", str(repo)],
        [
            sys.executable,
            str(repo / "scripts/build_obsidian_bridge_snapshot.py"),
            "--repo",
            str(repo),
            "--details-root",
            str(details_root),
        ],
        [sys.executable, str(repo / "scripts/check_safe_node_cards.py"), "--repo", str(repo), "--list-missing"],
        [sys.executable, str(repo / "scripts/audit_network_health.py"), "--repo", str(repo)],
        [
            sys.executable,
            str(repo / "scripts/audit_extended.py"),
            "--repo",
            str(repo),
            "--details-root",
            str(details_root),
        ],
        [
            sys.executable,
            str(repo / "scripts/audit_review_schedule.py"),
            "--repo",
            str(repo),
            "--date",
            review_date,
            "--json",
        ],
        [sys.executable, str(repo / "scripts/intake_validate_408.py"), "--repo", str(repo), "--full", "--json"],
    ]
    results: list[dict[str, Any]] = []
    for command in commands:
        completed = subprocess.run(command, capture_output=True, text=True)
        result = {
            "command": " ".join(command),
            "returncode": completed.returncode,
            "stdout": completed.stdout[-4000:],
            "stderr": completed.stderr[-2000:],
        }
        results.append(result)
        if completed.returncode:
            raise MaintenanceError(
                "closeout 失败；事务已提交，禁止重新 apply，只能重跑 closeout：\n"
                + result["command"]
                + "\n"
                + (completed.stdout or completed.stderr)[-3000:]
            )
    return results


def _publish(args: argparse.Namespace) -> int:
    repo = Path(args.repo).expanduser().resolve()
    details_root = Path(args.details_root).expanduser().resolve()
    runtime_root = Path(args.runtime_root).expanduser().resolve()
    package_path = Path(args.package).expanduser().resolve()
    if not package_path.is_file() or package_path.is_symlink():
        raise MaintenanceError(f"维护包不是普通文件：{package_path}")
    if "复盘输出" in package_path.parts:
        raise MaintenanceError("维护包不得位于复盘输出目录")
    package = _load_json(package_path)
    if args.apply:
        from formal_quality_408 import require_idle_or_owner
        require_idle_or_owner(repo)
    node_id = str(package.get("node_id") or "")
    idempotency_key = args.idempotency_key or f"{SCHEMA}:{node_id}:{sha256_file(package_path)}"
    if (repo / ".git").exists() and runtime_root != DEFAULT_RUNTIME_ROOT:
        raise MaintenanceError(f"生产 Git 仓库 runtime_root 固定为 {DEFAULT_RUNTIME_ROOT}")
    try:
        with RepoLock(repo, runtime_root, args.lock_timeout, timeout_error=RepoLockTimeout):
            payload_identity = _payload_identity(package_path, package, repo, details_root)
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
                    raise MaintenanceError(f"幂等键已用于不同 payload：{idempotency_key}")
                if args.apply:
                    closeout = _run_closeout(
                        repo, details_root, str(package.get("review_date") or ""), receipt_path
                    )
                    result = _result_from_receipt(receipt_path, receipt).as_dict()
                    result["closeout"] = [
                        {"command": item["command"], "returncode": item["returncode"]}
                        for item in closeout
                    ]
                    _emit(result)
                    return 0
                _emit(_result_from_receipt(receipt_path, receipt).as_dict())
                return 0

            outputs, stats = _project_all(package, repo, details_root)
            base_hashes = _capture_before_hashes(outputs)
            repo_outputs = {path: data for path, data in outputs.items() if _under(path, repo)}
            gates = _run_shadow_gates(repo, details_root, repo_outputs)
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

            final_outputs, final_stats = _project_all(package, repo, details_root)
            if final_stats != stats or final_outputs != outputs:
                raise MaintenanceError("影子门禁期间正式状态或证据发生变化")
            expected_before = {path: base_hashes[path] for path in outputs}
            txid, receipt, changed = _commit_transaction(
                repo,
                runtime_root,
                outputs,
                idempotency_key,
                payload_sha,
                [{"job_id": "", "formal_id": node_id, "mode": RECEIPT_MODE}],
                [repo, details_root],
                ACTOR,
                None,
                [],
                f"EXISTING-NODE-{node_id}-{package.get('review_date')}",
                {"contract": SCHEMA, "package_sha256": sha256_file(package_path)},
                payload_identity,
                details_root,
                expected_before_hashes=expected_before,
            )
        closeout = _run_closeout(
            repo, details_root, str(package.get("review_date") or ""), Path(receipt)
        )
        _emit({
            "status": "COMMITTED_CLOSEOUT_PASS",
            "transaction_id": txid,
            "receipt": str(receipt),
            "changed_files": changed,
            "stats": stats,
            "wal_recovered_before_commit": recovered,
            "shadow_gates": [
                {"command": item["command"], "returncode": item["returncode"]}
                for item in gates
            ],
            "closeout": [
                {"command": item["command"], "returncode": item["returncode"]}
                for item in closeout
            ],
        })
        return 0
    except RepoLockTimeout as exc:
        raise MaintenanceError(str(exc)) from exc
    except TransactionRolledBack as exc:
        raise MaintenanceError(str(exc)) from exc
    except (BatchError, PublishError) as exc:
        raise MaintenanceError(str(exc)) from exc


def _closeout_command(args: argparse.Namespace) -> int:
    repo = Path(args.repo).expanduser().resolve()
    details_root = Path(args.details_root).expanduser().resolve()
    receipt = Path(args.receipt).expanduser().resolve()
    results = _run_closeout(repo, details_root, args.review_date, receipt)
    _emit({
        "status": "CLOSEOUT_PASS",
        "receipt": str(receipt),
        "closeout": [
            {"command": item["command"], "returncode": item["returncode"]}
            for item in results
        ],
    })
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    publish = sub.add_parser("publish", help="默认 dry-run；显式 --apply 才提交单节点事务")
    publish.add_argument("package")
    publish.add_argument("--repo", default=".")
    publish.add_argument("--details-root", default=str(_paths.details_root()))
    publish.add_argument("--runtime-root", default=str(DEFAULT_RUNTIME_ROOT))
    publish.add_argument("--idempotency-key")
    publish.add_argument("--lock-timeout", type=float, default=30.0)
    publish.add_argument("--apply", action="store_true")
    publish.set_defaults(func=_publish)

    closeout = sub.add_parser("closeout", help="绑定已提交 receipt 重跑派生刷新与验收")
    closeout.add_argument("--receipt", required=True)
    closeout.add_argument("--review-date", required=True)
    closeout.add_argument("--repo", default=".")
    closeout.add_argument("--details-root", default=str(_paths.details_root()))
    closeout.set_defaults(func=_closeout_command)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        return int(args.func(args))
    except MaintenanceError as exc:
        print(json.dumps({"status": "ERROR", "error": str(exc)}, ensure_ascii=False), file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
