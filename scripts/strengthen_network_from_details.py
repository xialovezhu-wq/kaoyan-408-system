#!/usr/bin/env python3
"""Strengthen 408 wrong-network quality from existing formal rows and detail cards.

This script keeps protected content out of the formal repo. It uses:
- `节点总表.md` as the formal source of node fields;
- `/Users/your-user/Documents/kaoyan-408-details/cards` and `assets` only as
  durable attachment metadata;
- `知识点命中索引.md` for surgical main-hit block repair;
- `关系边表.md` for duplicate-pair cleanup.

It does not copy full stems, options, answers, full solutions, images, or
handwriting into safe node cards.
"""

from __future__ import annotations

import argparse
import re
from collections import Counter, defaultdict
from dataclasses import dataclass
from datetime import date
from pathlib import Path


NODE_ID_RE = re.compile(r"^(DS|CO|OS|CN)_(?:\d{4}|UNK)_\d{3}$")
NODE_FILE_RE = re.compile(r"^(DS|CO|OS|CN)_(?:\d{4}|UNK)_\d{3}\.md$")
DETAILS_ROOT = Path("/Users/your-user/Documents/kaoyan-408-details")
ANSWER_MARKERS = [
    "标准答案",
    "正确答案",
    "错误答案",
    "我的答案",
    "我的选择",
    "错选",
    "误选",
    "正确选项",
    "选项字母",
]


@dataclass
class DetailMeta:
    card_exists: bool = False
    question_images: int = 0
    solution_images: int = 0
    has_solution_text: bool = False
    source_locator: str = "待补充"
    match_reason: str = "待补充"


def split_md_row(line: str) -> list[str]:
    return [cell.strip() for cell in line.strip().strip("|").split("|")]


def is_separator(line: str) -> bool:
    cells = split_md_row(line)
    return bool(cells) and all(re.fullmatch(r":?-{3,}:?", cell or "") for cell in cells)


def read_table(path: Path) -> tuple[list[str], list[str], list[dict[str, str]]]:
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


def write_table(path: Path, preamble: list[str], headers: list[str], rows: list[dict[str, str]]) -> None:
    out = list(preamble)
    if out and out[-1] != "":
        out.append("")
    out.append("|" + "|".join(headers) + "|")
    out.append("|" + "|".join("---" for _ in headers) + "|")
    for row in rows:
        out.append("|" + "|".join(row.get(header, "") for header in headers) + "|")
    path.write_text("\n".join(out) + "\n", encoding="utf-8")


def safe_text(value: str, default: str = "待补充") -> str:
    value = (value or "").strip()
    if not value:
        return default
    value = re.sub(r"##\s*(正确答案|标准答案|答案)\b", "## 答案信息已隐藏（复做保护）", value)
    value = value.replace("{{选项分析}}", "{{选项分析已隐藏（复做保护）}}")
    value = value.replace("{{错因总结}}", "{{错因总结（答案信息已隐藏）}}")
    value = re.sub(r"错选\s*[A-Z](?:（[^）]*）)?", "错选信息已隐藏（复做保护）", value)
    value = re.sub(r"误选\s*[A-Z](?:（[^）]*）)?", "错选信息已隐藏（复做保护）", value)
    value = re.sub(r"我的答案[：:][^；。,.，]*", "我的答案信息已隐藏（复做保护）", value)
    value = re.sub(r"我的选择[：:][^；。,.，]*", "我的选择信息已隐藏（复做保护）", value)
    value = re.sub(r"正确答案[：:][^；。,.，]*", "答案信息已隐藏（复做保护）", value)
    value = re.sub(r"正确\s*[A-Z](?:（[^）]*）)?(?=[；。,.，]|$)", "答案信息已隐藏（复做保护）", value)
    return value


def extract_field(text: str, label: str) -> str:
    match = re.search(rf"^\s*{re.escape(label)}[：:]\s*(.*?)\s*$", text, re.M)
    return match.group(1).strip() if match else ""


def load_detail_meta(details_root: Path, node_id: str) -> DetailMeta:
    card = details_root / "cards" / f"{node_id}.md"
    assets = details_root / "assets" / node_id
    meta = DetailMeta()
    if card.exists():
        meta.card_exists = True
        text = card.read_text(encoding="utf-8", errors="ignore")
        meta.source_locator = safe_text(extract_field(text, "来源定位"), "待补充")
        meta.match_reason = safe_text(extract_field(text, "匹配方式"), "待补充")
        answer_block = text.split("> [!answer]- 解析", 1)[1] if "> [!answer]- 解析" in text else ""
        stripped_answer = re.sub(r"!\[\[.*?\]\]", "", answer_block).strip()
        meta.has_solution_text = bool(stripped_answer and "解析文字待补充" not in stripped_answer)
    if assets.exists():
        meta.question_images = len([p for p in assets.iterdir() if p.is_file() and p.name.startswith("question-")])
        meta.solution_images = len([p for p in assets.iterdir() if p.is_file() and p.name.startswith("solution-")])
    return meta


def question_asset_status(meta: DetailMeta) -> str:
    if meta.question_images:
        return f"已登记（{meta.question_images} 张，位于 details vault）"
    if meta.card_exists:
        return "详情卡存在但题图待补充"
    return "待补充"


def solution_asset_status(meta: DetailMeta) -> str:
    bits: list[str] = []
    if meta.solution_images:
        bits.append(f"解析图 {meta.solution_images} 张")
    if meta.has_solution_text:
        bits.append("解析文字已在 details vault")
    if bits:
        return "已登记（" + "；".join(bits) + "）"
    if meta.card_exists:
        return "详情卡存在但解析图/解析文字待补充"
    return "待补充"


def first_action(row: dict[str, str]) -> str:
    record = safe_text(row.get("最近错误记录", ""))
    core = row.get("核心考点", "").strip()
    fuzzy = row.get("模糊概念", "").strip()
    for marker in ("复做时", "需", "先", "下次"):
        idx = record.find(marker)
        if idx >= 0:
            candidate = record[idx:]
            candidate = re.split(r"[。；]", candidate, maxsplit=1)[0].strip()
            if len(candidate) >= 8:
                return candidate
    if any(key in core + fuzzy for key in ("地址", "Cache", "页表", "字段", "位", "容量", "字节")):
        return "先列出题设中的地址、容量、字段、单位和边界条件，再按主知识点流程逐步拆分或计算。"
    if any(key in core + fuzzy for key in ("状态", "调度", "置换", "排序", "遍历", "算法", "队列", "栈", "窗口")):
        return "先写出初始状态和更新规则，再逐步维护状态表，最后检查边界条件。"
    return "先用主知识点复述判定规则，再回到详情卡题图独立重做，最后再展开解析核对。"


def key_parameters(row: dict[str, str], meta: DetailMeta) -> str:
    core = row.get("核心考点", "").strip()
    fuzzy = row.get("模糊概念", "").strip()
    detail_hint = "详情卡已登记题图/解析" if meta.card_exists else "详情卡待补"
    text = f"{core}；易混点：{fuzzy}" if fuzzy and fuzzy != "待补充" else core
    if not text:
        text = "待从详情卡结构化"
    return safe_text(f"{text}。具体容量、地址、序列、初态、表格字段和边界条件以 details vault 题图为准；{detail_hint}。")


def card_content(row: dict[str, str], meta: DetailMeta, details_root: Path) -> str:
    node_id = row["ID"]
    detail_card = f"cards/{node_id}.md" if meta.card_exists else "待补充"
    summary = safe_text(
        f"{row.get('题型', '待补充')}；核心围绕“{row.get('核心考点', '待补充')}”。完整题图、选项和解析只在 details vault 查看。",
    )
    content = f"""# {node_id}

## 安全节点卡片

- 正式节点 ID：{node_id}
- 来源 ID：{safe_text(row.get('来源ID', '待补充'))}
- 年份：{safe_text(row.get('年份', '未标明'))}
- 科目：{safe_text(row.get('科目', '待补充'))}
- 主模块：{safe_text(row.get('主模块', '待补充'))}
- 主知识点：{safe_text(row.get('主知识点', '待补充'))}
- 副知识点：{safe_text(row.get('副知识点', '待补充'))}
- 命中知识点：{safe_text(row.get('命中知识点', '待补充'))}
- 详情入口：{safe_text(row.get('详情入口', '待补充'))}
- MarginNote 定位：{meta.source_locator}
- MarginNote 定位状态：{meta.match_reason}

## 复做安全信息

- 安全题目摘要：{summary}
- 关键题设参数：{key_parameters(row, meta)}
- 问法类型：{safe_text(row.get('题型', '待补充'))}
- 用户错误入口：{safe_text(row.get('最近错误记录', '暂无明确错因'))}
- 复做第一动作：{first_action(row)}
- 核心考点：{safe_text(row.get('核心考点', '待补充'))}
- 模糊概念：{safe_text(row.get('模糊概念', '待补充'))}
- 错因标签：{safe_text(row.get('错因标签', '暂无明确错因'))}

## 附件登记

- 题图：{question_asset_status(meta)}
- 解析图或解析正文：{solution_asset_status(meta)}
- 作答痕迹：未记录
- 可视化详情入口：{detail_card}
- 当前附件来源：{details_root}/cards/{node_id}.md
- OCR/解析处理：本卡只登记安全摘要、关键参数、问法类型、错误入口和复做第一动作；不复制完整题干、选项、答案或完整解析。

## 复做保护

- 复做保护说明：本卡只保留题目骨架、必要参数、错误入口和复做动作；不保存完整题干、完整解析、标准答案、正确选项、用户错选项、选项字母组合、图片或手写痕迹。复做时先打开 details vault 题图独立作答，再展开解析核对。
"""
    for marker in ANSWER_MARKERS:
        if marker in content and marker not in ("错选",):
            pass
    return content


def create_missing_safe_cards(repo: Path, rows: list[dict[str, str]], details_root: Path) -> list[str]:
    created: list[str] = []
    for row in rows:
        node_id = row.get("ID", "")
        if not NODE_ID_RE.match(node_id):
            continue
        card = repo / f"{node_id}.md"
        if card.exists():
            continue
        meta = load_detail_meta(details_root, node_id)
        card.write_text(card_content(row, meta, details_root), encoding="utf-8")
        created.append(node_id)
    return created


def sanitize_existing_safe_cards(repo: Path) -> list[str]:
    changed: list[str] = []
    for path in sorted(repo.iterdir()):
        if not path.is_file() or not NODE_FILE_RE.match(path.name):
            continue
        old = path.read_text(encoding="utf-8")
        new = safe_text(old, "")
        if new != old:
            path.write_text(new + ("\n" if not new.endswith("\n") else ""), encoding="utf-8")
            changed.append(path.stem)
    return changed


def relation_rank(row: dict[str, str]) -> tuple[int, int]:
    strength_score = {"强": 3, "中": 2, "弱": 1}.get(row.get("联系强度", ""), 0)
    rel_code = row.get("关系类型", "").split()[0]
    rel_score = {
        "R01": 8,
        "R02": 9,
        "R03": 7,
        "R04": 6,
        "R05": 7,
        "R06": 4,
        "R09": 3,
        "R07": 2,
        "R08": 1,
    }.get(rel_code, 0)
    return strength_score, rel_score


def repair_duplicate_edges(repo: Path) -> list[tuple[str, str, str]]:
    preamble, headers, rows = read_table(repo / "关系边表.md")
    by_pair: dict[tuple[str, str], list[dict[str, str]]] = defaultdict(list)
    for row in rows:
        a = row.get("起点ID", "")
        b = row.get("终点ID", "")
        if a and b:
            by_pair[tuple(sorted((a, b)))].append(row)
    removed: list[tuple[str, str, str]] = []
    keep_ids: set[int] = set()
    for pair, group in by_pair.items():
        if len(group) == 1:
            keep_ids.add(id(group[0]))
            continue
        chosen = max(group, key=relation_rank)
        keep_ids.add(id(chosen))
        for row in group:
            if row is chosen:
                continue
            removed.append((row.get("起点ID", ""), row.get("终点ID", ""), row.get("关系类型", "")))
    if removed:
        write_table(repo / "关系边表.md", preamble, headers, [row for row in rows if id(row) in keep_ids])
    return removed


@dataclass
class Section:
    title: str
    lines: list[str]


def parse_sections(path: Path) -> tuple[list[str], list[Section]]:
    lines = path.read_text(encoding="utf-8").splitlines()
    head: list[str] = []
    sections: list[Section] = []
    cur: Section | None = None
    for line in lines:
        match = re.match(r"^###\s+(.+)$", line)
        if match:
            cur = Section(match.group(1).strip(), [line])
            sections.append(cur)
        elif cur is None:
            head.append(line)
        else:
            cur.lines.append(line)
    return head, sections


def block_bounds(sec: Section, title: str) -> tuple[int, int] | None:
    start = None
    for i, line in enumerate(sec.lines):
        if line.strip() == f"#### {title}":
            start = i + 1
            break
    if start is None:
        return None
    end = start
    while end < len(sec.lines):
        if sec.lines[end].startswith("####") or sec.lines[end].strip() == "---":
            break
        end += 1
    return start, end


def remove_main_entry(sec: Section, node_id: str) -> str | None:
    bounds = block_bounds(sec, "主知识点命中")
    if not bounds:
        return None
    start, end = bounds
    for i in range(start, end):
        if re.match(rf"^-\s*{re.escape(node_id)}：", sec.lines[i].strip()):
            return sec.lines.pop(i)
    return None


def insert_main_entry(sec: Section, entry: str, node_id: str) -> None:
    bounds = block_bounds(sec, "主知识点命中")
    if not bounds:
        insert_at = 1
        sec.lines[insert_at:insert_at] = ["", "#### 主知识点命中", "", entry]
        return
    start, end = bounds
    for i in range(start, end):
        match = re.match(r"^-\s*((?:DS|CO|OS|CN)_(?:\d{4}|UNK)_\d{3})：", sec.lines[i].strip())
        if match and match.group(1) > node_id:
            sec.lines.insert(i, entry)
            return
    sec.lines.insert(end, entry)


def recount_section(sec: Section) -> None:
    ids = set(re.findall(r"\b(?:DS|CO|OS|CN)_(?:\d{4}|UNK)_\d{3}\b", "\n".join(sec.lines)))
    for i, line in enumerate(sec.lines):
        if line.startswith("- 节点数量："):
            sec.lines[i] = f"- 节点数量：{len(ids)}"
            return


def repair_main_hit_blocks(repo: Path, rows: list[dict[str, str]]) -> list[str]:
    path = repo / "知识点命中索引.md"
    head, sections = parse_sections(path)
    by_title = {section.title: section for section in sections}
    row_by_id = {row["ID"]: row for row in rows if row.get("ID")}
    moved: list[str] = []

    # Remove nodes from incorrect main-hit blocks.
    for sec in sections:
        bounds = block_bounds(sec, "主知识点命中")
        if not bounds:
            continue
        start, end = bounds
        node_ids = [
            match.group(1)
            for line in sec.lines[start:end]
            for match in [re.match(r"^-\s*((?:DS|CO|OS|CN)_(?:\d{4}|UNK)_\d{3})：", line.strip())]
            if match
        ]
        for node_id in node_ids:
            row = row_by_id.get(node_id)
            if row and row.get("主知识点") != sec.title:
                remove_main_entry(sec, node_id)
                moved.append(node_id)

    # Ensure each node appears in exactly its own main-knowledge section.
    for node_id, row in row_by_id.items():
        main = row.get("主知识点", "")
        if not main:
            continue
        sec = by_title.get(main)
        if not sec:
            continue
        bounds = block_bounds(sec, "主知识点命中")
        block_text = "\n".join(sec.lines[bounds[0] : bounds[1]]) if bounds else ""
        if re.search(rf"^-\s*{re.escape(node_id)}：", block_text, re.M):
            continue
        entry = f"- {node_id}：{row.get('核心考点', '')}；最近复做日期：{row.get('最近复做日期', '')}。"
        insert_main_entry(sec, entry, node_id)
        moved.append(node_id)

    if moved:
        for sec in sections:
            recount_section(sec)
        out: list[str] = list(head)
        for sec in sections:
            out.extend(sec.lines)
        path.write_text("\n".join(out) + "\n", encoding="utf-8")
    return sorted(set(moved))


def write_report(
    repo: Path,
    created_cards: list[str],
    sanitized_cards: list[str],
    removed_edges: list[tuple[str, str, str]],
    repaired_hits: list[str],
    rows: list[dict[str, str]],
    details_root: Path,
) -> Path:
    report = repo / "导入报告" / f"{date.today().isoformat()}-关系网质量加固报告.md"
    report.parent.mkdir(exist_ok=True)
    subject_counts = Counter(node_id.split("_")[0] for node_id in created_cards)
    detail_with_cards = sum(1 for row in rows if (details_root / "cards" / f"{row.get('ID')}.md").exists())
    lines = [
        "# 408 关系网质量加固报告",
        "",
        f"- 生成日期：{date.today().isoformat()}",
        "- 范围：安全节点卡片补齐、重复关系边去重、知识点命中索引主块修复。",
        "- 边界：未复制完整题干、完整解析、标准答案、错选项、选项字母、图片或手写痕迹；未读取或修改 `复盘输出/`。",
        f"- 正式节点数：{len(rows)}",
        f"- details vault 已有详情卡：{detail_with_cards}",
        "",
        "## 一、安全节点卡片",
        "",
        f"- 本次新增：{len(created_cards)} 张",
        f"- 本次复做保护清洗：{len(sanitized_cards)} 张",
        f"- 分科统计：{dict(subject_counts)}",
        "",
        "## 二、关系边去重",
        "",
        f"- 删除重复低优先级边：{len(removed_edges)} 条",
    ]
    for start, end, rel in removed_edges:
        lines.append(f"- {start} -- {end}：删除 `{rel}`，保留同对中更具体/更高强度关系。")
    lines += [
        "",
        "## 三、知识点命中索引",
        "",
        f"- 修复主知识点命中块节点：{len(repaired_hits)} 个",
    ]
    if repaired_hits:
        lines.append("- 节点：" + "；".join(repaired_hits))
    lines += [
        "",
        "## 四、后续人工加固建议",
        "",
        "- 对新生成安全卡中的 `关键题设参数` 逐步从 details 解析中人工压缩为容量、地址、访问序列、算法初态、表格字段或边界条件。",
        "- 对 details 卡中仍只有题图、没有解析图/解析文字的节点，优先补齐外部详情层，不要把图片搬入正式 408 仓库。",
        "- Tutor 检测线可优先覆盖本次新增安全卡最多的科目和主知识点，但测验结果仍需经过 rollback-review 才能反写正式系统。",
        "",
    ]
    report.write_text("\n".join(lines), encoding="utf-8")
    return report


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--repo", default=".")
    parser.add_argument("--details-root", default=str(DETAILS_ROOT))
    args = parser.parse_args()
    repo = Path(args.repo).resolve()
    details_root = Path(args.details_root).resolve()

    _, _, node_rows = read_table(repo / "节点总表.md")
    node_rows = [row for row in node_rows if NODE_ID_RE.match(row.get("ID", ""))]

    created_cards = create_missing_safe_cards(repo, node_rows, details_root)
    sanitized_cards = sanitize_existing_safe_cards(repo)
    removed_edges = repair_duplicate_edges(repo)
    repaired_hits = repair_main_hit_blocks(repo, node_rows)
    report = write_report(repo, created_cards, sanitized_cards, removed_edges, repaired_hits, node_rows, details_root)

    print(f"created_safe_cards={len(created_cards)}")
    print(f"sanitized_safe_cards={len(sanitized_cards)}")
    print(f"removed_duplicate_edges={len(removed_edges)}")
    print(f"repaired_main_hit_nodes={len(repaired_hits)}")
    print(f"wrote_report={report.relative_to(repo)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
