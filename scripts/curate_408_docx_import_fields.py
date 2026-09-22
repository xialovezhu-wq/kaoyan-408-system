#!/usr/bin/env python3
"""Apply curated field补缺 for high-confidence DOCX import nodes.

This keeps the formal library compact: only short summaries, official
knowledge labels, source定位, and strong same-knowledge edges are updated.
"""

from __future__ import annotations

import argparse
from dataclasses import dataclass
from datetime import date
from pathlib import Path

from normalize_408_knowledge_points import (
    load_valid_labels,
    read_table_with_preamble,
    regenerate_knowledge_index,
    write_table,
)


@dataclass(frozen=True)
class NodePatch:
    node_id: str
    fields: dict[str, str]
    reason: str


PATCHES: list[NodePatch] = []  # Public export: private migration input intentionally omitted.


def patch_rows(rows: list[dict[str, str]], patches: dict[str, NodePatch]) -> list[tuple[str, str, str, str]]:
    changes: list[tuple[str, str, str, str]] = []
    for row in rows:
        patch = patches.get(row.get("ID", ""))
        if not patch:
            continue
        for field, new_value in patch.fields.items():
            old_value = row.get(field, "")
            if old_value != new_value:
                row[field] = new_value
                changes.append((patch.node_id, field, old_value, new_value))
    return changes


def update_subject_view(path: Path, node_by_id: dict[str, dict[str, str]], patch_ids: set[str]) -> int:
    preamble, headers, rows = read_table_with_preamble(path)
    if not headers:
        return 0
    changed = 0
    for row in rows:
        node_id = row.get("ID", "")
        node = node_by_id.get(node_id)
        if node_id not in patch_ids or not node:
            continue
        updates = {
            "主知识点": node.get("主知识点", ""),
            "核心考点": node.get("核心考点", ""),
            "错因标签": node.get("错因标签", ""),
            "最近复做日期": node.get("最近复做日期", ""),
        }
        for field, new_value in updates.items():
            if field in headers and row.get(field, "") != new_value:
                row[field] = new_value
                changed += 1
    if changed:
        write_table(path, preamble, headers, rows)
    return changed


def update_year_index(path: Path, node_by_id: dict[str, dict[str, str]], patch_ids: set[str]) -> int:
    lines = path.read_text(encoding="utf-8").splitlines()
    headers: list[str] = []
    out: list[str] = []
    changed = 0
    for line in lines:
        stripped = line.strip()
        if not stripped.startswith("|"):
            out.append(line)
            continue
        cells = [cell.strip() for cell in stripped.strip("|").split("|")]
        if cells and all(set(cell) <= {"-", ":"} and "-" in cell for cell in cells):
            out.append(line)
            continue
        if "ID" in cells and "主知识点" in cells:
            headers = cells
            out.append(line)
            continue
        if headers and len(cells) >= len(headers):
            row = dict(zip(headers, cells))
            node = node_by_id.get(row.get("ID", ""))
            if node and row.get("ID", "") in patch_ids:
                updates = {
                    "科目": node.get("科目", ""),
                    "模块": node.get("主模块", ""),
                    "主知识点": node.get("主知识点", ""),
                    "首次做题日期": node.get("首次做题日期", ""),
                    "最近复做日期": node.get("最近复做日期", ""),
                    "详情入口": node.get("详情入口", ""),
                }
                row_changed = False
                for field, new_value in updates.items():
                    if field in headers:
                        idx = headers.index(field)
                        if cells[idx] != new_value:
                            cells[idx] = new_value
                            row_changed = True
                if row_changed:
                    changed += 1
                    out.append("|" + "|".join(cells) + "|")
                    continue
        out.append(line)
    if changed:
        path.write_text("\n".join(out) + "\n", encoding="utf-8")
    return changed


def update_mapping(path: Path, node_by_id: dict[str, dict[str, str]], patch_ids: set[str]) -> int:
    preamble, headers, rows = read_table_with_preamble(path)
    if not headers:
        return 0
    changed = 0
    for row in rows:
        node = node_by_id.get(row.get("正式节点ID", ""))
        if not node or node.get("ID", "") not in patch_ids:
            continue
        updates = {
            "科目": node.get("科目", ""),
            "年份": node.get("年份", ""),
            "核心考点": node.get("核心考点", ""),
            "首次做题日期": node.get("首次做题日期", ""),
            "最近复做日期": node.get("最近复做日期", ""),
            "详情入口": node.get("详情入口", ""),
        }
        for field, new_value in updates.items():
            if field in headers and row.get(field, "") != new_value:
                row[field] = new_value
                changed += 1
    if changed:
        write_table(path, preamble, headers, rows)
    return changed


def pair_key(a: str, b: str, rel_type: str) -> tuple[str, str, str]:
    start, end = sorted([a, b])
    return start, end, rel_type


def update_relations(path: Path, node_by_id: dict[str, dict[str, str]], patch_ids: set[str]) -> tuple[int, int]:
    preamble, headers, rows = read_table_with_preamble(path)
    if not headers:
        return 0, 0
    by_pair_type = {pair_key(row.get("起点ID", ""), row.get("终点ID", ""), row.get("关系类型", "")): row for row in rows}
    by_pair_any: dict[tuple[str, str], dict[str, str]] = {}
    for row in rows:
        a, b = sorted([row.get("起点ID", ""), row.get("终点ID", "")])
        by_pair_any[(a, b)] = row

    upgraded = 0
    added = 0
    for node_id in sorted(patch_ids):
        node = node_by_id[node_id]
        main = node.get("主知识点", "")
        if not main or main == "待补充":
            continue
        peers = sorted(
            peer_id
            for peer_id, peer in node_by_id.items()
            if peer_id != node_id and peer.get("主知识点", "") == main
        )
        for peer_id in peers:
            start, end = sorted([node_id, peer_id])
            reason = f"同一主知识点：{main}。"
            existing_r01 = by_pair_type.get((start, end, "R01 同一核心考点"))
            if existing_r01:
                for field, value in {
                    "联系强度": "强",
                    "关联原因": reason,
                    "复盘优先级": "高",
                }.items():
                    if existing_r01.get(field, "") != value:
                        existing_r01[field] = value
                        upgraded += 1
                continue
            existing_any = by_pair_any.get((start, end))
            if existing_any and existing_any.get("关系类型", "").startswith("R09"):
                existing_any.update(
                    {
                        "起点ID": start,
                        "终点ID": end,
                        "关系类型": "R01 同一核心考点",
                        "联系强度": "强",
                        "关联原因": reason,
                        "复盘优先级": "高",
                    }
                )
                by_pair_type[(start, end, "R01 同一核心考点")] = existing_any
                upgraded += 1
                continue
            new_row = {
                "起点ID": start,
                "终点ID": end,
                "关系类型": "R01 同一核心考点",
                "联系强度": "强",
                "关联原因": reason,
                "复盘优先级": "高",
            }
            rows.append(new_row)
            by_pair_any[(start, end)] = new_row
            by_pair_type[(start, end, "R01 同一核心考点")] = new_row
            added += 1

    if upgraded or added:
        write_table(path, preamble, headers, rows)
    return upgraded, added


def write_report(repo: Path, changes: list[tuple[str, str, str, str]], relation_upgrades: int, relation_additions: int) -> None:
    lines = [
        "# 408 Word 旧错题字段补缺记录",
        "",
        f"- 生成日期：{date.today().isoformat()}",
        "- 范围：仅处理 DOCX 回溯后可确认的低质量摘要字段。",
        "- 边界：不保存完整题干、完整解析、图片或手写痕迹。",
        f"- 强关系升级：{relation_upgrades} 处。",
        f"- 强关系新增：{relation_additions} 条。",
        "",
        "| 正式节点ID | 字段 | 原值 | 新值 |",
        "|---|---|---|---|",
    ]
    for node_id, field, old_value, new_value in changes:
        lines.append(f"|{node_id}|{field}|{old_value}|{new_value}|")
    lines.extend(["", "## 依据", ""])
    for patch in PATCHES:
        lines.append(f"- {patch.node_id}：{patch.reason}")
    report_path = repo / "导入报告/408-docx-2026-05-07字段补缺记录.md"
    report_path.parent.mkdir(exist_ok=True)
    report_path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--repo", default=".")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    repo = Path(args.repo).resolve()
    valid_labels = load_valid_labels(repo)
    for patch in PATCHES:
        for field in ("主知识点", "副知识点", "命中知识点"):
            for label in [part.strip() for part in patch.fields.get(field, "").split("；") if part.strip()]:
                if label != "待补充" and label not in valid_labels:
                    raise SystemExit(f"知识点标签表中缺少目标标签：{label}")

    patch_by_id = {patch.node_id: patch for patch in PATCHES}
    patch_ids = set(patch_by_id)
    preamble, headers, node_rows = read_table_with_preamble(repo / "节点总表.md")
    node_by_id = {row.get("ID", ""): row for row in node_rows}
    missing = sorted(patch_ids - set(node_by_id))
    if missing:
        raise SystemExit("节点总表缺少目标节点：" + "；".join(missing))

    changes = patch_rows(node_rows, patch_by_id)
    if args.dry_run:
        for node_id, field, old_value, new_value in changes:
            print(f"{node_id}: {field}: {old_value} -> {new_value}")
        print(f"planned_field_changes={len(changes)}")
        return 0

    write_table(repo / "节点总表.md", preamble, headers, node_rows)
    node_by_id = {row.get("ID", ""): row for row in node_rows}
    subject_changes = update_subject_view(repo / "节点库/计算机组成原理节点.md", node_by_id, patch_ids)
    year_changes = update_year_index(repo / "年份索引.md", node_by_id, patch_ids)
    mapping_changes = update_mapping(repo / "历史错题归档/正式ID映射.md", node_by_id, patch_ids)
    relation_upgrades, relation_additions = update_relations(repo / "关系边表.md", node_by_id, patch_ids)
    regenerate_knowledge_index(repo / "知识点命中索引.md", node_rows)
    write_report(repo, changes, relation_upgrades, relation_additions)

    print(f"field_changes={len(changes)}")
    print(f"subject_view_changes={subject_changes}")
    print(f"year_index_rows_changed={year_changes}")
    print(f"mapping_changes={mapping_changes}")
    print(f"relation_upgrades={relation_upgrades}")
    print(f"relation_additions={relation_additions}")
    print("regenerated=知识点命中索引.md")
    print("wrote=导入报告/408-docx-2026-05-07字段补缺记录.md")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
