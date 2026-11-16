#!/usr/bin/env python3
"""Refresh 408 wiki coverage tables from formal node and edge tables."""

from __future__ import annotations

import argparse
import re
from collections import Counter, defaultdict
from datetime import datetime
from pathlib import Path

from normalize_408_knowledge_points import read_table_with_preamble


SUBJECTS = {
    "DS": ("DS 数据结构", "408-COVERAGE-DS_数据结构覆盖表.md"),
    "CO": ("CO 计算机组成原理", "408-COVERAGE-CO_计算机组成原理覆盖表.md"),
    "OS": ("OS 操作系统", "408-COVERAGE-OS_操作系统覆盖表.md"),
    "CN": ("CN 计算机网络", "408-COVERAGE-CN_计算机网络覆盖表.md"),
}

MAIN_COVERAGE = "408-COVERAGE-001_正式错题全量覆盖索引.md"
ID_RE = re.compile(r"^(?:DS|CO|OS|CN)_(?:\d{4}|UNK)_\d{3}$")
COVERAGE_HEADERS = ["ID", "年份", "科目", "主知识点", "来源 ID", "关系边数", "wiki 状态", "wiki 页面"]
COMPATIBLE_WIKI_DIRS = {"topics", "error_patterns", "relation_summaries", "source_summaries"}


def _page_mentions_node(path: Path, node_id: str) -> bool:
    if not node_id or not path.is_file():
        return False
    text = path.read_text(encoding="utf-8", errors="ignore")
    pattern = rf"(?<![A-Z0-9_]){re.escape(node_id)}(?![A-Z0-9_])"
    return bool(re.search(pattern, text))


def concept_page(repo: Path, main_knowledge: str, node_id: str) -> str | None:
    if not main_knowledge.strip():
        return None
    parts = main_knowledge.split(maxsplit=1)
    if len(parts) != 2:
        return None
    code, name = parts
    safe_name = name.replace("/", "-").replace(" ", "")
    rel = f"wiki/concepts/{code}_{safe_name}边界.md"
    return rel if _page_mentions_node(repo / rel, node_id) else None


def _split_md_row(line: str) -> list[str]:
    return [cell.strip() for cell in line.strip().strip("|").split("|")]


def _is_separator(line: str) -> bool:
    cells = _split_md_row(line)
    return bool(cells) and all(re.fullmatch(r":?-{3,}:?", cell or "") for cell in cells)


def read_coverage_rows(path: Path) -> list[dict[str, str]]:
    """Read the node-status table without confusing earlier summary tables for it."""

    if not path.exists():
        return []
    lines = path.read_text(encoding="utf-8").splitlines()
    for index in range(len(lines) - 1):
        if not lines[index].lstrip().startswith("|") or not _is_separator(lines[index + 1]):
            continue
        headers = _split_md_row(lines[index])
        if headers != COVERAGE_HEADERS:
            continue
        rows: list[dict[str, str]] = []
        cursor = index + 2
        while cursor < len(lines) and lines[cursor].lstrip().startswith("|"):
            cells = _split_md_row(lines[cursor])
            if len(cells) < len(headers):
                cells.extend([""] * (len(headers) - len(cells)))
            rows.append(dict(zip(headers, cells)))
            cursor += 1
        return rows
    return []


def load_existing_rows(repo: Path) -> dict[str, dict[str, str]]:
    existing: dict[str, dict[str, str]] = {}
    paths = sorted(
        (repo / "wiki/coverage").glob("408-COVERAGE-*.md"),
        key=lambda path: (path.name == MAIN_COVERAGE, path.name),
    )
    for path in paths:
        for row in read_coverage_rows(path):
            node_id = row.get("ID", "")
            if ID_RE.fullmatch(node_id):
                existing[node_id] = row
    return existing


def _compatible_existing_page(repo: Path, raw_page: str, node_id: str) -> str | None:
    """Keep a non-concept wiki page only when it still exists and names this node.

    Concept pages are intentionally excluded here: their canonical path is derived
    from the *current* main knowledge point by :func:`concept_page`, so an old
    concept page can never survive a main-knowledge migration by accident.
    """

    page = (raw_page or "").strip().strip("`").strip()
    if page.startswith("[[") and page.endswith("]]"):
        page = page[2:-2].split("|", 1)[0].strip()
    if not page or page == "待编译":
        return None
    if not page.endswith(".md"):
        page += ".md"
    rel = Path(page)
    if rel.is_absolute() or not rel.parts or rel.parts[0] != "wiki":
        return None
    if len(rel.parts) < 2 or rel.parts[1] not in COMPATIBLE_WIKI_DIRS:
        return None
    candidate = (repo / rel).resolve()
    try:
        candidate.relative_to(repo.resolve())
    except ValueError:
        return None
    return rel.as_posix() if _page_mentions_node(candidate, node_id) else None


def derive_wiki_fields(repo: Path, node: dict[str, str], old_row: dict[str, str] | None = None) -> tuple[str, str]:
    """Derive coverage status/page from the current main knowledge point.

    A canonical current concept page always wins.  Compatibility is limited to
    an unchanged main knowledge point and a real non-concept wiki page that
    explicitly references the node.  This prevents a migrated node from keeping
    the previous concept page merely because the old coverage row said compiled.
    """

    current_main = node.get("主知识点", "").strip()
    rel = concept_page(repo, current_main, node.get("ID", ""))
    if rel:
        return "compiled", f"`{rel}`"

    old_row = old_row or {}
    if old_row.get("主知识点", "").strip() == current_main and old_row.get("wiki 状态", "").strip() == "compiled":
        compatible = _compatible_existing_page(repo, old_row.get("wiki 页面", ""), node.get("ID", ""))
        if compatible:
            return "compiled", f"`{compatible}`"
    return "formal_source_only", "待编译"


def edge_counts(rows: list[dict[str, str]]) -> Counter[str]:
    counts: Counter[str] = Counter()
    for row in rows:
        for column in ("起点ID", "终点ID"):
            node_id = row.get(column, "")
            if ID_RE.fullmatch(node_id):
                counts[node_id] += 1
    return counts


def build_rows(repo: Path) -> list[dict[str, str]]:
    _, _, node_rows = read_table_with_preamble(repo / "节点总表.md")
    _, _, edge_rows = read_table_with_preamble(repo / "关系边表.md")
    old = load_existing_rows(repo)
    counts = edge_counts(edge_rows)

    rows: list[dict[str, str]] = []
    for node in node_rows:
        node_id = node.get("ID", "")
        if not ID_RE.fullmatch(node_id):
            continue
        old_row = old.get(node_id, {})
        status, page = derive_wiki_fields(repo, node, old_row)
        rows.append(
            {
                "ID": node_id,
                "年份": node.get("年份", ""),
                "科目": node.get("科目", ""),
                "主知识点": node.get("主知识点", ""),
                "来源 ID": node.get("来源ID", ""),
                "关系边数": str(counts[node_id]),
                "wiki 状态": status,
                "wiki 页面": page or "待编译",
            }
        )
    return rows


def md_table(rows: list[dict[str, str]]) -> list[str]:
    headers = ["ID", "年份", "科目", "主知识点", "来源 ID", "关系边数", "wiki 状态", "wiki 页面"]
    lines = [
        "| " + " | ".join(headers) + " |",
        "|---|---|---|---|---|---:|---|---|",
    ]
    for row in rows:
        lines.append("| " + " | ".join(row.get(header, "") for header in headers) + " |")
    return lines


def write_subject_table(repo: Path, prefix: str, rows: list[dict[str, str]], stamp: str) -> None:
    title, filename = SUBJECTS[prefix]
    subject_rows = [row for row in rows if row["ID"].startswith(prefix + "_")]
    compiled = sum(1 for row in subject_rows if row["wiki 状态"] == "compiled")
    formal_only = len(subject_rows) - compiled
    lines = [
        f"# 408-COVERAGE-{prefix} {title.split(maxsplit=1)[1]}覆盖表",
        "",
        "用途：按科目跟踪正式错题节点的 LLM Wiki 编译状态。",
        "",
        "边界：本文件不保存完整题干、完整解析、标准答案、错选答案、图片或手写痕迹。",
        "",
        f"更新时间：{stamp}",
        "",
        f"- 正式节点：{len(subject_rows)}",
        f"- 已编译：{compiled}",
        f"- 仅 formal source：{formal_only}",
        "",
        *md_table(subject_rows),
    ]
    (repo / "wiki/coverage" / filename).write_text("\n".join(lines).rstrip() + "\n", encoding="utf-8")


def write_main_table(repo: Path, rows: list[dict[str, str]], stamp: str) -> None:
    compiled = sum(1 for row in rows if row["wiki 状态"] == "compiled")
    formal_only = len(rows) - compiled
    by_subject = defaultdict(list)
    for row in rows:
        by_subject[row["ID"].split("_", 1)[0]].append(row)

    uncompiled = [row for row in rows if row["wiki 状态"] != "compiled"]
    kp_counts = Counter(row["主知识点"] for row in uncompiled)
    kp_lines = [
        "| 主知识点 | 未编译节点数 | 节点样例 |",
        "|---|---:|---|",
    ]
    if kp_counts:
        for kp, count in kp_counts.most_common(10):
            sample = "；".join(row["ID"] for row in uncompiled if row["主知识点"] == kp)[:120]
            kp_lines.append(f"| {kp} | {count} | {sample} |")
    else:
        kp_lines.append("| 无 | 0 | 全部正式节点均已进入 wiki 显式引用范围 |")

    subject_lines = [
        "| 科目 | 总数 | 已编译 | formal_source_only | 覆盖表 |",
        "|---|---:|---:|---:|---|",
    ]
    for prefix, (label, filename) in SUBJECTS.items():
        subject_rows = by_subject[prefix]
        subject_compiled = sum(1 for row in subject_rows if row["wiki 状态"] == "compiled")
        subject_lines.append(
            f"| {label} | {len(subject_rows)} | {subject_compiled} | {len(subject_rows) - subject_compiled} | "
            f"[[wiki/coverage/{filename.removesuffix('.md')}]] |"
        )

    lines = [
        "# 408-COVERAGE-001 正式错题全量覆盖索引",
        "",
        "用途：让 Codex/Obsidian 明确知道 408 正式错题节点哪些已经被 LLM Wiki 编译，哪些仍只是正式数据来源。",
        "",
        "边界：本文件是派生覆盖表，不保存完整题干、完整解析、标准答案、错选答案、图片或手写痕迹。",
        "",
        f"更新时间：{stamp}",
        "",
        "## 总览",
        "",
        f"- 正式节点总数：{len(rows)}",
        f"- 已有 wiki 编译覆盖：{compiled}",
        f"- 仅 formal source 登记：{formal_only}",
        "- 正式数据来源：`节点总表.md`、`关系边表.md`",
        "- 语义 wiki 扫描目录：`wiki/concepts/`、`wiki/topics/`、`wiki/error_patterns/`、`wiki/relation_summaries/`、`wiki/source_summaries/`",
        "",
        "## 科目完成度",
        "",
        *subject_lines,
        "",
        "## 高频未编译主知识点候选",
        "",
        *kp_lines,
        "",
        "## 使用规则",
        "",
        "- `compiled` 表示该正式节点已至少出现在一个 wiki 概念页、专题页、错因模式页、关系综合页或来源摘要页中。",
        "- `formal_source_only` 表示该节点仍只在正式数据表中登记，查询时必须回到正式表，不得编造 wiki 总结。",
        "- 后续每批编译完成后，刷新本覆盖索引和 Dashboard 摘要。",
        "",
        "## 全量节点状态",
        "",
        *md_table(rows),
    ]
    (repo / "wiki/coverage" / MAIN_COVERAGE).write_text("\n".join(lines).rstrip() + "\n", encoding="utf-8")


def refresh(repo: Path) -> dict[str, int]:
    repo = Path(repo).resolve()
    stamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S +0800")
    rows = build_rows(repo)
    for prefix in SUBJECTS:
        write_subject_table(repo, prefix, rows, stamp)
    write_main_table(repo, rows, stamp)
    compiled = sum(1 for row in rows if row["wiki 状态"] == "compiled")
    return {"coverage_total": len(rows), "coverage_compiled": compiled,
            "coverage_formal_source_only": len(rows) - compiled}


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--repo", default=".")
    args = parser.parse_args()
    for key, value in refresh(Path(args.repo)).items():
        print(f"{key}={value}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
