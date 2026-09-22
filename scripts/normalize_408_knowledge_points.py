#!/usr/bin/env python3
"""Normalize high-confidence legacy knowledge-point strings to official codes.

This script applies only curated mappings whose target labels already exist in
`知识点标签表.md`. It updates the formal node table and derived index views.
It does not add full questions, explanations, images, or handwritten traces.
"""

from __future__ import annotations

import argparse
import re
from collections import defaultdict
from dataclasses import dataclass
from datetime import date
from pathlib import Path

from audit_408_docx_import import KNOWLEDGE_CODE_RE, read_markdown_table


@dataclass(frozen=True)
class Mapping:
    node_id: str
    target: str
    reason: str


MAPPINGS: list[Mapping] = []  # Public export: private migration input intentionally omitted.


def split_md_row(line: str) -> list[str]:
    return [cell.strip() for cell in line.strip().strip("|").split("|")]


def is_separator(line: str) -> bool:
    cells = split_md_row(line)
    return bool(cells) and all(re.fullmatch(r":?-{3,}:?", cell or "") for cell in cells)


def write_table(path: Path, title_lines: list[str], headers: list[str], rows: list[dict[str, str]]) -> None:
    lines = list(title_lines)
    if lines and lines[-1] != "":
        lines.append("")
    lines.append("|" + "|".join(headers) + "|")
    lines.append("|" + "|".join("---" for _ in headers) + "|")
    for row in rows:
        lines.append("|" + "|".join(row.get(header, "") for header in headers) + "|")
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def read_table_with_preamble(path: Path) -> tuple[list[str], list[str], list[dict[str, str]]]:
    lines = path.read_text(encoding="utf-8").splitlines()
    preamble: list[str] = []
    headers: list[str] = []
    rows: list[dict[str, str]] = []
    in_table = False
    for line in lines:
        if line.strip().startswith("|"):
            cells = split_md_row(line)
            if not in_table:
                headers = cells
                in_table = True
                continue
            if is_separator(line):
                continue
            if len(cells) < len(headers):
                cells.extend([""] * (len(headers) - len(cells)))
            rows.append(dict(zip(headers, cells)))
        elif not in_table:
            preamble.append(line)
    return preamble, headers, rows


def load_valid_labels(repo: Path) -> set[str]:
    labels: set[str] = set()
    for line in (repo / "知识点标签表.md").read_text(encoding="utf-8").splitlines():
        m = re.match(r"-\s*((?:DS|CO|OS|CN)\d{2}-\d{2}\s+.+)$", line.strip())
        if m:
            labels.add(m.group(1).strip())
    return labels


def update_node_rows(rows: list[dict[str, str]], mapping_by_id: dict[str, Mapping]) -> dict[str, str]:
    old_main_by_id: dict[str, str] = {}
    for row in rows:
        mapping = mapping_by_id.get(row.get("ID", ""))
        if not mapping:
            continue
        old_main = row.get("主知识点", "")
        old_hits = row.get("命中知识点", "")
        old_main_by_id[row["ID"]] = old_main
        row["主知识点"] = mapping.target
        if not KNOWLEDGE_CODE_RE.search(old_hits) or old_hits == old_main:
            row["命中知识点"] = mapping.target
    return old_main_by_id


def update_simple_table(path: Path, mapping_by_id: dict[str, Mapping], target_column: str = "主知识点") -> int:
    preamble, headers, rows = read_table_with_preamble(path)
    if not headers or target_column not in headers:
        return 0
    changed = 0
    for row in rows:
        mapping = mapping_by_id.get(row.get("ID", ""))
        if mapping and row.get(target_column) != mapping.target:
            row[target_column] = mapping.target
            changed += 1
    if changed:
        write_table(path, preamble, headers, rows)
    return changed


def update_year_index(path: Path, mapping_by_id: dict[str, Mapping]) -> int:
    lines = path.read_text(encoding="utf-8").splitlines()
    headers: list[str] = []
    changed = 0
    out: list[str] = []
    for line in lines:
        stripped = line.strip()
        if not stripped.startswith("|"):
            out.append(line)
            continue
        cells = split_md_row(line)
        if is_separator(line):
            out.append(line)
            continue
        if "ID" in cells and "主知识点" in cells:
            headers = cells
            out.append(line)
            continue
        if headers and len(cells) >= len(headers):
            row = dict(zip(headers, cells))
            mapping = mapping_by_id.get(row.get("ID", ""))
            if mapping and "主知识点" in headers:
                idx = headers.index("主知识点")
                if cells[idx] != mapping.target:
                    cells[idx] = mapping.target
                    changed += 1
                    out.append("|" + "|".join(cells) + "|")
                    continue
        out.append(line)
    if changed:
        path.write_text("\n".join(out) + "\n", encoding="utf-8")
    return changed


def update_relation_reasons(path: Path, old_main_by_id: dict[str, str], mapping_by_id: dict[str, Mapping]) -> int:
    preamble, headers, rows = read_table_with_preamble(path)
    if not headers or "关联原因" not in headers:
        return 0
    changed = 0
    for row in rows:
        endpoints = [row.get("起点ID", ""), row.get("终点ID", "")]
        reason = row.get("关联原因", "")
        new_reason = reason
        for node_id in endpoints:
            old = old_main_by_id.get(node_id)
            mapping = mapping_by_id.get(node_id)
            if old and mapping and old in new_reason:
                new_reason = new_reason.replace(old, mapping.target)
        if new_reason != reason:
            row["关联原因"] = new_reason
            changed += 1
    if changed:
        write_table(path, preamble, headers, rows)
    return changed


def regenerate_knowledge_index(path: Path, node_rows: list[dict[str, str]]) -> None:
    grouped: dict[str, list[dict[str, str]]] = defaultdict(list)
    for row in node_rows:
        hits = [hit.strip() for hit in row.get("命中知识点", "").split("；") if hit.strip()]
        for hit in hits:
            grouped[hit].append(row)

    def sort_key(item: tuple[str, list[dict[str, str]]]) -> tuple[str, str]:
        key = item[0]
        m = KNOWLEDGE_CODE_RE.search(key)
        return (m.group(0) if m else "ZZ" + key, key)

    lines = [
        "# 知识点命中索引",
        "",
        "本文件按 408 知识点聚合统一正式错题节点。历史归档题已映射为正式节点 ID；来源定位见 `历史错题归档/正式ID映射.md` 和 `历史错题归档/定位索引.md`。",
        "",
        "知识点命中不是错因。即使当前没有明确错因，只要节点明确考查、使用或提到某个知识点，也归入这里。",
        "",
    ]
    for knowledge, rows in sorted(grouped.items(), key=sort_key):
        rows = sorted(rows, key=lambda r: r.get("ID", ""))
        lines.extend(
            [
                f"### {knowledge}",
                "",
                f"- 节点数量：{len(rows)}",
                "",
                "#### 主知识点命中",
                "",
            ]
        )
        for row in rows:
            if row.get("主知识点") == knowledge:
                lines.append(
                    f"- {row.get('ID', '')}：{row.get('核心考点', '')}；最近复做日期：{row.get('最近复做日期', '')}。"
                )
        lines.extend(["", "#### 错因触发记录", ""])
        for row in rows:
            lines.append(f"- {row.get('ID', '')}：{row.get('错因标签', '')}。")
        lines.extend(["", "---", ""])
    path.write_text("\n".join(lines), encoding="utf-8")


def existing_report_rows(repo: Path) -> dict[str, tuple[str, str, str]]:
    report_path = repo / "导入报告/408-docx-2026-05-07知识点归一化记录.md"
    if not report_path.exists():
        return {}
    rows: dict[str, tuple[str, str, str]] = {}
    for row in read_markdown_table(report_path):
        node_id = row.get("正式节点ID", "")
        if node_id:
            rows[node_id] = (
                row.get("原主知识点", ""),
                row.get("新主知识点", ""),
                row.get("依据", ""),
            )
    return rows


def write_normalization_report(repo: Path, applied: list[tuple[Mapping, str]]) -> None:
    report_dir = repo / "导入报告"
    report_dir.mkdir(exist_ok=True)
    merged_rows = existing_report_rows(repo)
    for mapping, old_main in applied:
        merged_rows[mapping.node_id] = (old_main, mapping.target, mapping.reason)

    lines = [
        "# 408 Word 旧错题知识点归一化记录",
        "",
        f"- 生成日期：{date.today().isoformat()}",
        "- 范围：只处理本地规则可确认的高置信度历史节点。",
        "- 原则：只替换主知识点/命中知识点中的长路径或摘要为正式知识点编号；不补写题干、解析或图片。",
        "",
        "| 正式节点ID | 原主知识点 | 新主知识点 | 依据 |",
        "|---|---|---|---|",
    ]
    for node_id in sorted(merged_rows):
        old_main, target, reason = merged_rows[node_id]
        lines.append(f"|{node_id}|{old_main}|{target}|{reason}|")
    (report_dir / "408-docx-2026-05-07知识点归一化记录.md").write_text(
        "\n".join(lines) + "\n", encoding="utf-8"
    )


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--repo", default=".")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    repo = Path(args.repo).resolve()
    valid_labels = load_valid_labels(repo)
    mapping_by_id = {mapping.node_id: mapping for mapping in MAPPINGS}
    missing_labels = sorted({mapping.target for mapping in MAPPINGS if mapping.target not in valid_labels})
    if missing_labels:
        raise SystemExit("知识点标签表中缺少目标标签：\n" + "\n".join(missing_labels))

    preamble, headers, node_rows = read_table_with_preamble(repo / "节点总表.md")
    old_rows_by_id = {row["ID"]: dict(row) for row in node_rows}
    old_main_by_id = {}
    applied: list[tuple[Mapping, str]] = []
    for row in node_rows:
        mapping = mapping_by_id.get(row.get("ID", ""))
        if mapping:
            old_main = row.get("主知识点", "")
            if old_main != mapping.target:
                applied.append((mapping, old_main))
    if args.dry_run:
        for mapping, old_main in applied:
            print(f"{mapping.node_id}: {old_main} -> {mapping.target}")
        print(f"planned_updates={len(applied)}")
        return 0

    old_main_by_id = update_node_rows(node_rows, mapping_by_id)
    write_table(repo / "节点总表.md", preamble, headers, node_rows)

    subject_files = [
        "节点库/数据结构节点.md",
        "节点库/计算机组成原理节点.md",
        "节点库/操作系统节点.md",
        "节点库/计算机网络节点.md",
    ]
    subject_changes = sum(update_simple_table(repo / rel, mapping_by_id) for rel in subject_files)
    year_changes = update_year_index(repo / "年份索引.md", mapping_by_id)
    relation_changes = update_relation_reasons(repo / "关系边表.md", old_main_by_id, mapping_by_id)
    regenerate_knowledge_index(repo / "知识点命中索引.md", node_rows)
    write_normalization_report(repo, applied)

    print(f"applied_node_updates={len(applied)}")
    print(f"subject_view_updates={subject_changes}")
    print(f"year_index_updates={year_changes}")
    print(f"relation_reason_updates={relation_changes}")
    print("regenerated=知识点命中索引.md")
    print("wrote=导入报告/408-docx-2026-05-07知识点归一化记录.md")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
