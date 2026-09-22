#!/usr/bin/env python3
"""主模块全量归一：主模块 := 主知识点所在章对应的标准模块编码+名称。

背景（2026-06-11 关系网重构 阶段1）：
- DOCX 导入把 Word 笔记的章节路径写进了主模块（如“第七章 输入输出设备 / I/O接口”），
  284 个节点主模块非标准格式，44 个与主知识点章节直接矛盾；
- 失真主模块曾骗过 curate_relation_edges.py 的 same_topic 判定，导致跨章 R09 噪声漏删。

规则：
- 标准模块名取自 知识点标签表.md 的 `## XXnn 名称` 标题（与 知识体系/*.md 一致）。
- 原 Word 路径不丢失：详情入口与 历史错题归档/定位索引.md 已保留完整来源位置。
- 同步 年份索引.md 的“模块”列；节点库分科视图无主模块列，不涉及。
"""
from __future__ import annotations

import argparse
import re
import sys
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from normalize_408_knowledge_points import read_table_with_preamble, write_table, split_md_row, is_separator

CODE_RE = re.compile(r"^([A-Z]{2}\d{2})")


def load_module_names(repo: Path) -> dict[str, str]:
    names: dict[str, str] = {}
    for line in (repo / "知识点标签表.md").read_text(encoding="utf-8").splitlines():
        m = re.match(r"^##\s+((DS|CO|OS|CN)\d{2})\s+(.+)$", line.strip())
        if m:
            names[m.group(1)] = f"{m.group(1)} {m.group(3).strip()}"
    return names


def target_module(row: dict[str, str], modules: dict[str, str]) -> str | None:
    m = CODE_RE.match(row.get("主知识点", "").strip())
    if not m:
        return None
    return modules.get(m.group(1))


def update_year_index_modules(path: Path, module_by_id: dict[str, str]) -> int:
    lines = path.read_text(encoding="utf-8").splitlines()
    headers: list[str] = []
    changed = 0
    out: list[str] = []
    for line in lines:
        if not line.strip().startswith("|") or is_separator(line):
            out.append(line)
            continue
        cells = split_md_row(line)
        if "ID" in cells and "模块" in cells:
            headers = cells
            out.append(line)
            continue
        if headers and len(cells) >= len(headers):
            row = dict(zip(headers, cells))
            target = module_by_id.get(row.get("ID", ""))
            idx = headers.index("模块")
            if target and cells[idx] != target:
                cells[idx] = target
                changed += 1
                out.append("|" + "|".join(cells) + "|")
                continue
        out.append(line)
    if changed:
        path.write_text("\n".join(out) + "\n", encoding="utf-8")
    return changed


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--repo", default=".")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()
    repo = Path(args.repo).resolve()

    modules = load_module_names(repo)
    preamble, headers, rows = read_table_with_preamble(repo / "节点总表.md")

    changes: list[tuple[str, str, str]] = []
    unmapped: list[str] = []
    module_by_id: dict[str, str] = {}
    for row in rows:
        target = target_module(row, modules)
        if target is None:
            unmapped.append(row.get("ID", ""))
            continue
        module_by_id[row["ID"]] = target
        if row.get("主模块", "").strip() != target:
            changes.append((row["ID"], row.get("主模块", ""), target))

    if args.dry_run:
        for nid, old, new in changes[:20]:
            print(f"{nid}: [{old[:36]}] -> [{new}]")
        print(f"planned_updates={len(changes)} unmapped={len(unmapped)} {unmapped[:5]}")
        return 0

    for row in rows:
        target = module_by_id.get(row.get("ID", ""))
        if target:
            row["主模块"] = target
    write_table(repo / "节点总表.md", preamble, headers, rows)
    year_changes = update_year_index_modules(repo / "年份索引.md", module_by_id)

    report = [
        "# 2026-06-11 主模块归一记录",
        "",
        f"- 生成日期：{date.today().isoformat()}",
        "- 规则：主模块 := 主知识点所在章对应 `知识点标签表.md` 的标准模块标题。",
        "- 原 Word 章节路径不丢失：完整来源位置仍保存在 详情入口 与 历史错题归档/定位索引.md。",
        f"- 节点总表更新 {len(changes)} 行；年份索引模块列更新 {year_changes} 行。",
        "",
        "| ID | 旧主模块 | 新主模块 |",
        "|---|---|---|",
    ]
    for nid, old, new in changes:
        report.append(f"|{nid}|{old}|{new}|")
    (repo / "导入报告/2026-06-11主模块归一记录.md").write_text("\n".join(report) + "\n", encoding="utf-8")

    print(f"node_updates={len(changes)}")
    print(f"year_index_updates={year_changes}")
    print(f"unmapped={len(unmapped)} {unmapped[:5]}")
    print("wrote=导入报告/2026-06-11主模块归一记录.md")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
