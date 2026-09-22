#!/usr/bin/env python3
"""逐题修复脏字段 + 重挂主知识点（2026-06-11 关系网重构 阶段2）。

修复对象（诊断详见 导入报告/2026-06-11关系网重构报告.md）：
- 核心考点为 DOCX 解析残渣（"错题回顾"/"我的答案"/"正确答案：…"/章节标题）的节点；
- 挂在"垃圾桶"主知识点（CN01-18 网络体系结构、OS02-36 锁、OS03-15 页）但
  历史归档定位面包屑指向更细粒度知识点的节点。

依据来源（不编造信息）：
- 历史错题归档/定位索引.md 的完整来源位置面包屑；
- 导入报告/408-docx-2026-05-07知识点归一化记录.md 中记录的 DOCX 上下文；
- 知识点标签表.md（所有目标主知识点必须在标签表中存在）。

联动更新：节点总表、年份索引（主知识点列）、节点库分科视图（整体重建）、
知识点命中索引（外科手术式迁移：保留手工维护的"命中知识点"交叉小节）。
关系边的关联原因/失效满团边在阶段3 curate_relation_edges.py 统一处理。

证据冲突的节点只改核心考点为"待人工核对：…"并列入报告待确认清单，不改主知识点。
"""
from __future__ import annotations

import argparse
import re
import sys
from dataclasses import dataclass
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from normalize_408_knowledge_points import (
    Mapping,
    load_valid_labels,
    read_table_with_preamble,
    update_year_index,
    write_table,
)


@dataclass(frozen=True)
class Curation:
    node_id: str
    new_kp: str | None      # None = 主知识点不变
    new_core: str | None    # None = 核心考点不变
    reason: str
    pending: bool = False   # True = 证据冲突，列入待人工确认清单


CURATIONS: list[Curation] = []  # Public export: private migration input intentionally omitted.

SUBJECT_VIEWS = {
    "数据结构": "节点库/数据结构节点.md",
    "计算机组成原理": "节点库/计算机组成原理节点.md",
    "操作系统": "节点库/操作系统节点.md",
    "计算机网络": "节点库/计算机网络节点.md",
}


def rebuild_subject_views(repo: Path, node_rows: list[dict[str, str]]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for subject, rel in SUBJECT_VIEWS.items():
        path = repo / rel
        preamble, headers, _ = read_table_with_preamble(path)
        rows = [r for r in node_rows if r.get("科目", "").strip() == subject]
        rows.sort(key=lambda r: r.get("ID", ""))
        view_rows = [{h: r.get(h, "") for h in headers} for r in rows]
        write_table(path, preamble, headers, view_rows)
        counts[rel] = len(view_rows)
    return counts


# ---------- 知识点命中索引：外科手术式迁移 ----------

@dataclass
class Section:
    kp: str
    lines: list[str]

    def code(self) -> str:
        m = re.match(r"([A-Z]{2}\d{2}-\d{2})", self.kp)
        return m.group(0) if m else "ZZ" + self.kp


def parse_index(path: Path) -> tuple[list[str], list[Section]]:
    lines = path.read_text(encoding="utf-8").splitlines()
    head: list[str] = []
    sections: list[Section] = []
    cur: Section | None = None
    for line in lines:
        m = re.match(r"^###\s+(.+)$", line)
        if m:
            cur = Section(m.group(1).strip(), [line])
            sections.append(cur)
            continue
        if cur is None:
            head.append(line)
        else:
            cur.lines.append(line)
    return head, sections


def block_bounds(sec: Section, block_title: str) -> tuple[int, int] | None:
    """返回 (块内首条目行号, 块结束行号) —— 即 #### 标题后的条目区间。"""
    start = None
    for i, line in enumerate(sec.lines):
        if line.strip() == f"#### {block_title}":
            start = i + 1
            break
    if start is None:
        return None
    end = start
    for j in range(start, len(sec.lines)):
        if sec.lines[j].startswith("####") or sec.lines[j].strip() == "---":
            break
        end = j + 1
    return (start, end)


def block_entries(sec: Section, block_title: str) -> list[str]:
    b = block_bounds(sec, block_title)
    if not b:
        return []
    return [l for l in sec.lines[b[0]:b[1]] if l.strip().startswith("- ")]


def remove_entry(sec: Section, block_title: str, node_id: str) -> str | None:
    b = block_bounds(sec, block_title)
    if not b:
        return None
    for i in range(b[0], b[1]):
        if re.match(rf"^-\s*{re.escape(node_id)}：", sec.lines[i].strip()):
            return sec.lines.pop(i)
    return None


def insert_entry(sec: Section, block_title: str, entry: str, node_id: str) -> None:
    b = block_bounds(sec, block_title)
    if not b:
        # 在 --- 前补一个块
        tail = len(sec.lines)
        for i, l in enumerate(sec.lines):
            if l.strip() == "---":
                tail = i
                break
        sec.lines[tail:tail] = [f"#### {block_title}", "", entry, ""]
        return
    start, end = b
    pos = end
    for i in range(start, end):
        m = re.match(r"^-\s*([A-Z]{2}_(?:\d{4}|UNK)_\d{3})：", sec.lines[i].strip())
        if m and m.group(1) > node_id:
            pos = i
            break
    sec.lines.insert(pos, entry)


def recount(sec: Section) -> None:
    ids: set[str] = set()
    for title in ("主知识点命中", "命中知识点"):
        for l in block_entries(sec, title):
            m = re.match(r"^-\s*([A-Z]{2}_(?:\d{4}|UNK)_\d{3})：", l.strip())
            if m:
                ids.add(m.group(1))
    for i, l in enumerate(sec.lines):
        if re.match(r"^-\s*节点数量：", l.strip()):
            sec.lines[i] = f"- 节点数量：{len(ids)}"
            return


def section_is_empty(sec: Section) -> bool:
    return not (block_entries(sec, "主知识点命中") or block_entries(sec, "命中知识点") or block_entries(sec, "错因触发记录"))


def new_section(kp: str) -> Section:
    return Section(kp, [f"### {kp}", "", "- 节点数量：0", "", "#### 主知识点命中", "", "#### 错因触发记录", "", "---", ""])


def normalize_section_blanks(sec: Section) -> None:
    out: list[str] = []
    prev_blank = False
    for l in sec.lines:
        blank = not l.strip()
        if blank and prev_blank:
            continue
        out.append(l)
        prev_blank = blank
    # 确保条目块与标题之间有空行
    fixed: list[str] = []
    for i, l in enumerate(out):
        fixed.append(l)
        if l.startswith("####") and i + 1 < len(out) and out[i + 1].strip().startswith("- "):
            fixed.append("")
    sec.lines = fixed


def apply_index_surgery(path: Path, moves: list[tuple[str, str, str]], core_edits: dict[str, tuple[str, str]],
                        nodes: dict[str, dict[str, str]]) -> tuple[int, int, list[str]]:
    """moves: (node_id, old_kp_full, new_kp_full)；core_edits: node_id -> (kp_full, new_core)。"""
    head, sections = parse_index(path)
    by_kp = {s.kp: s for s in sections}
    moved = 0
    edited = 0
    dropped: list[str] = []

    for node_id, old_kp, new_kp in moves:
        old_sec = by_kp.get(old_kp)
        main_line = remove_entry(old_sec, "主知识点命中", node_id) if old_sec else None
        err_line = remove_entry(old_sec, "错因触发记录", node_id) if old_sec else None
        if new_kp not in by_kp:
            sec = new_section(new_kp)
            sections.append(sec)
            by_kp[new_kp] = sec
        new_sec = by_kp[new_kp]
        row = nodes[node_id]
        if main_line is None:
            main_line = f"- {node_id}：{row.get('核心考点','')}；最近复做日期：{row.get('最近复做日期','')}。"
        if node_id in core_edits:
            kept = re.search(r"；(最近复做日期：.*)$", main_line.strip())
            tail = kept.group(1) if kept else f"最近复做日期：{row.get('最近复做日期','')}。"
            main_line = f"- {node_id}：{core_edits[node_id][1]}；{tail}"
        if err_line is None:
            err_line = f"- {node_id}：{row.get('错因标签','')}。"
        insert_entry(new_sec, "主知识点命中", main_line, node_id)
        insert_entry(new_sec, "错因触发记录", err_line, node_id)
        moved += 1
        for sec in (old_sec, new_sec):
            if sec:
                recount(sec)

    for node_id, (kp_full, new_core) in core_edits.items():
        if any(node_id == m[0] for m in moves):
            continue  # 已在迁移时改写
        sec = by_kp.get(kp_full)
        if not sec:
            continue
        b = block_bounds(sec, "主知识点命中")
        if not b:
            continue
        for i in range(*b):
            stripped = sec.lines[i].strip()
            if re.match(rf"^-\s*{re.escape(node_id)}：", stripped):
                kept = re.search(r"；(最近复做日期：.*)$", stripped)
                tail = kept.group(1) if kept else "最近复做日期：未记录。"
                sec.lines[i] = f"- {node_id}：{new_core}；{tail}"
                edited += 1
                break

    kept_sections = []
    for sec in sections:
        if section_is_empty(sec):
            dropped.append(sec.kp)
            continue
        normalize_section_blanks(sec)
        kept_sections.append(sec)
    kept_sections.sort(key=lambda s: s.code())

    out = list(head)
    while out and not out[-1].strip():
        out.pop()
    out.append("")
    for sec in kept_sections:
        if sec.lines and sec.lines[-1].strip() == "":
            sec_lines = sec.lines
        else:
            sec_lines = sec.lines + [""]
        out.extend(sec_lines)
    path.write_text("\n".join(out).rstrip("\n") + "\n", encoding="utf-8")
    return moved, edited, dropped


def write_report(repo: Path, applied: list[tuple[Curation, str, str]], dropped_sections: list[str]) -> None:
    lines = [
        "# 2026-06-11 节点字段修复对照表（阶段2）",
        "",
        f"- 生成日期：{date.today().isoformat()}",
        "- 依据：历史错题归档/定位索引.md 面包屑、DOCX 导入期知识点归一化记录；不编造题目内容。",
        "- 同步范围：节点总表、年份索引、节点库分科视图（整体重建）、知识点命中索引（条目迁移）。",
        "- 关系边的满团重建与关联原因更新在阶段3处理。",
        "",
        "## 一、字段修复对照",
        "",
        "| ID | 旧主知识点 | 新主知识点 | 旧核心考点 | 新核心考点 | 依据 |",
        "|---|---|---|---|---|---|",
    ]
    for cur, old_kp, old_core in applied:
        new_kp = cur.new_kp or "（不变）"
        new_core = cur.new_core or "（不变）"
        lines.append(f"|{cur.node_id}|{old_kp}|{new_kp}|{old_core}|{new_core}|{cur.reason}|")
    lines += [
        "",
        "## 二、待人工确认清单（证据冲突，未改主知识点）",
        "",
    ]
    for cur, old_kp, old_core in applied:
        if cur.pending:
            lines.append(f"- {cur.node_id}：{cur.reason}")
    lines += [
        "",
        "## 三、保留判定",
        "",
        "- 公开版不携带个人节点的历史保留判定。",
        "",
        "## 四、命中索引清理",
        "",
        f"- 迁移后空分节已删除：{('、'.join(dropped_sections)) if dropped_sections else '无'}",
    ]
    (repo / "导入报告/2026-06-11节点字段修复对照表.md").write_text("\n".join(lines) + "\n", encoding="utf-8")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--repo", default=".")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()
    repo = Path(args.repo).resolve()

    valid = load_valid_labels(repo)
    bad_targets = sorted({c.new_kp for c in CURATIONS if c.new_kp and c.new_kp not in valid})
    if bad_targets:
        raise SystemExit("目标主知识点不在知识点标签表中：\n" + "\n".join(bad_targets))

    preamble, headers, rows = read_table_with_preamble(repo / "节点总表.md")
    nodes = {r["ID"]: r for r in rows}
    missing = [c.node_id for c in CURATIONS if c.node_id not in nodes]
    if missing:
        raise SystemExit(f"节点不存在：{missing}")

    applied: list[tuple[Curation, str, str]] = []
    moves: list[tuple[str, str, str]] = []
    core_edits: dict[str, tuple[str, str]] = {}
    kp_mappings: dict[str, Mapping] = {}

    for c in CURATIONS:
        row = nodes[c.node_id]
        old_kp = row.get("主知识点", "").strip()
        old_core = row.get("核心考点", "").strip()
        applied.append((c, old_kp, old_core))
        if c.new_kp and c.new_kp != old_kp:
            moves.append((c.node_id, old_kp, c.new_kp))
            kp_mappings[c.node_id] = Mapping(c.node_id, c.new_kp, c.reason)
        if c.new_core and c.new_core != old_core:
            core_edits[c.node_id] = (c.new_kp or old_kp, c.new_core)

    if args.dry_run:
        for c, old_kp, old_core in applied:
            kp_part = f" KP[{old_kp}]->[{c.new_kp}]" if c.new_kp and c.new_kp != old_kp else ""
            core_part = f" 核心[{old_core[:24]}]->[{(c.new_core or '')[:36]}]" if c.node_id in core_edits else ""
            flag = " [待人工确认]" if c.pending else ""
            print(f"{c.node_id}:{kp_part}{core_part}{flag}")
        print(f"planned: kp_moves={len(moves)} core_edits={len(core_edits)} pending={sum(1 for c in CURATIONS if c.pending)}")
        return 0

    # 1. 节点总表
    for c in CURATIONS:
        row = nodes[c.node_id]
        old_kp = row.get("主知识点", "").strip()
        if c.new_kp and c.new_kp != old_kp:
            hits = row.get("命中知识点", "")
            row["命中知识点"] = hits.replace(old_kp, c.new_kp) if old_kp and old_kp in hits else (c.new_kp if not hits.strip() else hits)
            row["主知识点"] = c.new_kp
        if c.new_core:
            row["核心考点"] = c.new_core
    write_table(repo / "节点总表.md", preamble, headers, rows)

    # 2. 年份索引（主知识点列）
    year_changes = update_year_index(repo / "年份索引.md", kp_mappings)

    # 3. 节点库分科视图整体重建
    view_counts = rebuild_subject_views(repo, rows)

    # 4. 知识点命中索引外科迁移
    moved, edited, dropped = apply_index_surgery(repo / "知识点命中索引.md", moves, core_edits, nodes)

    # 5. 报告
    write_report(repo, applied, dropped)

    print(f"node_field_updates={len(applied)} (kp_moves={len(moves)}, core_edits={len(core_edits)})")
    print(f"year_index_updates={year_changes}")
    print(f"subject_views_rebuilt={view_counts}")
    print(f"hit_index: moved={moved} core_edited={edited} dropped_sections={dropped}")
    print("wrote=导入报告/2026-06-11节点字段修复对照表.md")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
