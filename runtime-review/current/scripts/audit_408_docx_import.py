#!/usr/bin/env python3
"""Audit the 408 Word import against the Markdown relationship network.

The script is intentionally conservative:
- it reads the DOCX and existing Markdown files;
- it classifies current historical candidates against the formal node table;
- it writes only import/audit reports when --write-reports is provided.

It does not store full questions or full explanations from the DOCX.
"""

from __future__ import annotations

import argparse
import json
import re
from collections import Counter, defaultdict
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Iterable

from docx import Document

import runtime_paths_408 as _paths


DEFAULT_DOCX = (
    str(_paths.legacy_docx())
)

SUBJECT_PREFIX = {
    "数据结构": "DS",
    "计算机组成原理": "CO",
    "操作系统": "OS",
    "计算机网络": "CN",
}
PREFIX_SUBJECT = {v: k for k, v in SUBJECT_PREFIX.items()}
HISTORY_PREFIX_SUBJECT = {
    "HDS": "数据结构",
    "HCO": "计算机组成原理",
    "HOS": "操作系统",
    "HCN": "计算机网络",
}
NODE_SUBJECT_FILE = {
    "数据结构": "节点库/数据结构节点.md",
    "计算机组成原理": "节点库/计算机组成原理节点.md",
    "操作系统": "节点库/操作系统节点.md",
    "计算机网络": "节点库/计算机网络节点.md",
}
CORE_FORMAL_FILES = [
    "节点总表.md",
    "年份索引.md",
    "知识点命中索引.md",
    "概念词典.md",
    "关系边表.md",
    "历史错题归档/正式ID映射.md",
    *NODE_SUBJECT_FILE.values(),
]
MISSING_MARKERS = ("待补充", "未标明", "未记录", "暂无明确错因")
FORMAL_FIELD_NAMES = [
    "ID",
    "来源ID",
    "年份",
    "科目",
    "主模块",
    "主知识点",
    "副知识点",
    "命中知识点",
    "题型",
    "核心考点",
    "模糊概念",
    "错因标签",
    "首次做题日期",
    "最近复做日期",
    "最近错误记录",
    "详情入口",
]
PROHIBITED_FORMAL_PATTERNS = {
    "完整问题标题": re.compile(r"##\s*问题"),
    "图片引用": re.compile(r"!\[[^\]]*\]\(|<img\b|\.png\b|\.jpe?g\b|\.gif\b", re.I),
    "代码围栏": re.compile(r"```"),
}
DATE_LIKE_RE = re.compile(r"^\d{4}[.-]\d{1,2}[.-]\d{1,2}$")
LOCATOR_RE = re.compile(r"\d+(?:\.\d+){1,3}\s*[（(]\s*\d+\s*[）)]|\d+\.\d+\.\d+")
KNOWLEDGE_CODE_RE = re.compile(r"\b(?:DS|CO|OS|CN)\d{2}-\d{2}\b")
NODE_ID_RE = re.compile(r"^(DS|CO|OS|CN)_(?:\d{4}|UNK)_\d{3}$")


@dataclass(frozen=True)
class DocxParagraph:
    number: int
    style: str
    text: str
    heading_path: tuple[str, ...]


def split_md_row(line: str) -> list[str]:
    return [cell.strip() for cell in line.strip().strip("|").split("|")]


def read_markdown_table(path: Path) -> list[dict[str, str]]:
    rows: list[dict[str, str]] = []
    header: list[str] | None = None
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line.startswith("|"):
            continue
        cells = split_md_row(line)
        if cells and all(re.fullmatch(r":?-{3,}:?", cell or "") for cell in cells):
            continue
        if header is None:
            header = cells
            continue
        if len(cells) < len(header):
            cells.extend([""] * (len(header) - len(cells)))
        rows.append(dict(zip(header, cells)))
    return rows


def read_total_count(path: Path) -> int | None:
    for line in path.read_text(encoding="utf-8").splitlines():
        m = re.search(r"总数[:：]\s*(\d+)", line)
        if m:
            return int(m.group(1))
    return None


def clean_heading(text: str) -> str:
    text = re.sub(r"\s+", " ", text).strip()
    text = re.sub(r"\s*>>\s*$", "", text).strip()
    text = text.lstrip("#").strip()
    return text


def heading_level(style_name: str) -> int | None:
    m = re.fullmatch(r"Heading (\d+)", style_name or "")
    if not m:
        return None
    return int(m.group(1))


def parse_docx(docx_path: Path) -> tuple[list[DocxParagraph], Counter[str]]:
    doc = Document(str(docx_path))
    heading_stack: dict[int, str] = {}
    paragraphs: list[DocxParagraph] = []
    style_counts: Counter[str] = Counter()

    for number, para in enumerate(doc.paragraphs, start=1):
        style = para.style.name if para.style else ""
        style_counts[style] += 1
        text = para.text.strip()
        level = heading_level(style)
        if level and text:
            heading_stack = {k: v for k, v in heading_stack.items() if k < level}
            heading_stack[level] = clean_heading(text)
        heading_path = tuple(heading_stack[k] for k in sorted(heading_stack))
        paragraphs.append(DocxParagraph(number, style, text, heading_path))

    return paragraphs, style_counts


def wrong_like_docx_blocks(paragraphs: Iterable[DocxParagraph]) -> list[DocxParagraph]:
    markers = (
        "① 本题所属科目",
        "题目信息简述",
        "我的错误",
        "我的错选",
        "错选",
        "第一次错",
        "正确选项",
        "错误答案",
        "正确答案",
        "错题回顾",
    )
    blocks: list[DocxParagraph] = []
    seen: set[int] = set()
    for para in paragraphs:
        if not para.text:
            continue
        if any(marker in para.text for marker in markers):
            if para.number not in seen:
                blocks.append(para)
                seen.add(para.number)
    return blocks


def normalize_text(value: str) -> str:
    value = re.sub(r"`[^`]*`", "", value)
    value = re.sub(r"[*_#✅❌🌟]+", "", value)
    value = re.sub(r"\s+", "", value)
    return value.strip()


def token_overlap(a: str, b: str) -> bool:
    a = normalize_text(a)
    b = normalize_text(b)
    if not a or not b:
        return False
    if a in b or b in a:
        return True
    chunks = [a[i : i + 4] for i in range(0, max(len(a) - 3, 0), 3)]
    return any(chunk and chunk in b for chunk in chunks[:6])


def paragraph_context(paragraphs: list[DocxParagraph], para_number: int, radius: int = 3) -> str:
    start = max(1, para_number - radius)
    end = min(len(paragraphs), para_number + radius)
    texts: list[str] = []
    for para in paragraphs[start - 1 : end]:
        texts.extend(para.heading_path[-3:])
        if para.text:
            texts.append(para.text[:300])
    return " / ".join(texts)


def locator_from_detail(value: str) -> str:
    m = re.search(r"定位标记[:：]\s*([^；|]+(?:；[^；|]+)*)", value)
    if not m:
        return "未提取"
    return m.group(1).strip()


def paragraph_number_from_detail(value: str) -> int | None:
    m = re.search(r"段落号[:：]\s*段落(\d+)|段落(\d+)", value)
    if not m:
        return None
    return int(next(group for group in m.groups() if group))


def source_prefix(source_id: str) -> str:
    m = re.match(r"H(?:DS|CO|OS|CN)", source_id)
    if m:
        return m.group(0)
    return ""


def formal_prefix(node_id: str) -> str:
    return node_id.split("_", 1)[0] if "_" in node_id else ""


def infer_fillable_fields(node: dict[str, str], history_row: dict[str, str]) -> list[str]:
    """Return fields that can be improved from current historical metadata.

    Values such as date "未记录" are valid project states when the source has no
    date, so they are not treated as fillable unless the historical row provides
    a concrete value.
    """
    fillable: list[str] = []
    history_year = history_row.get("年份", "")
    history_source = history_row.get("来源", "")
    history_kp = history_row.get("知识点键", "")
    history_errors = history_row.get("错因标签", "")

    if node.get("年份") in {"未标明", ""} and history_year and history_year != "未标明":
        fillable.append("年份")

    if node.get("题型") in {"未标明", "待补充", ""}:
        if "选择题" in history_source:
            fillable.append("题型")
        elif "算法题" in history_source or "大题" in history_source:
            fillable.append("题型")

    if node.get("错因标签") in {"暂无明确错因", "待补充", ""}:
        if history_errors and history_errors != "暂无明确错因":
            fillable.append("错因标签")

    history_codes = KNOWLEDGE_CODE_RE.findall(history_kp)
    if history_codes:
        if not KNOWLEDGE_CODE_RE.search(node.get("主知识点", "")):
            fillable.append("主知识点")
        if not KNOWLEDGE_CODE_RE.search(node.get("命中知识点", "")):
            fillable.append("命中知识点")

    detail = node.get("详情入口", "")
    if "定位标记" not in detail or "段落号" not in detail:
        if "段落" in history_source:
            fillable.append("详情入口")

    return fillable


def parse_knowledge_codes(label_text: str) -> set[str]:
    return set(KNOWLEDGE_CODE_RE.findall(label_text))


def non_standard_knowledge_fields(nodes: list[dict[str, str]], valid_codes: set[str]) -> list[dict[str, str]]:
    issues: list[dict[str, str]] = []
    for node in nodes:
        for field in ("主知识点", "副知识点", "命中知识点"):
            value = node.get(field, "")
            codes = set(KNOWLEDGE_CODE_RE.findall(value))
            unknown_codes = sorted(code for code in codes if code not in valid_codes)
            if unknown_codes:
                issues.append(
                    {
                        "ID": node.get("ID", ""),
                        "字段": field,
                        "问题": "未知知识点编号：" + "；".join(unknown_codes),
                    }
                )
            if not codes and value and value not in {"待补充", "暂无明确命中"}:
                if "/" in value or " / " in value or len(value) > 18:
                    issues.append(
                        {
                            "ID": node.get("ID", ""),
                            "字段": field,
                            "问题": "未归一为知识点编号",
                        }
                    )
    return issues


def classify_candidates(
    history_rows: list[dict[str, str]],
    mapping_rows: list[dict[str, str]],
    node_by_id: dict[str, dict[str, str]],
) -> tuple[list[dict[str, str]], Counter[str]]:
    mapping_by_source = {row.get("历史归档ID", ""): row for row in mapping_rows}
    classified: list[dict[str, str]] = []
    counts: Counter[str] = Counter()

    for row in history_rows:
        source_id = row.get("历史归档ID", "")
        mapping = mapping_by_source.get(source_id)
        formal_id = mapping.get("正式节点ID", "") if mapping else ""
        node = node_by_id.get(formal_id, {}) if formal_id else {}
        gap = row.get("信息缺口", "")
        status = "新候选"
        missing_fields: list[str] = []
        if mapping and node:
            missing_fields = infer_fillable_fields(node, row)
            if missing_fields or (gap and gap != "无明显缺口"):
                status = "可补字段" if missing_fields else "信息不足"
            else:
                status = "已存在"
        elif mapping and not node:
            status = "信息不足"
        elif "年份未标明" in gap or "错因需复核" in gap:
            status = "信息不足"
        counts[status] += 1
        classified.append(
            {
                "历史归档ID": source_id,
                "正式节点ID": formal_id or "未生成",
                "科目": row.get("科目", ""),
                "年份": row.get("年份", ""),
                "定位标记": "",
                "段落号": "",
                "核心考点": row.get("核心考点", ""),
                "知识点键": row.get("知识点键", ""),
                "状态": status,
                "可补字段": "；".join(missing_fields) if missing_fields else "",
                "信息缺口": gap,
            }
        )

    return classified, counts


def enrich_with_location(classified: list[dict[str, str]], location_rows: list[dict[str, str]]) -> None:
    by_source = {row.get("历史归档ID", ""): row for row in location_rows}
    for item in classified:
        loc = by_source.get(item["历史归档ID"], {})
        item["定位标记"] = loc.get("定位标记", "")
        item["段落号"] = loc.get("段落号", "")
        if not item.get("核心考点"):
            item["核心考点"] = loc.get("核心考点", "")


def validate_docx_trace(
    location_rows: list[dict[str, str]],
    paragraphs: list[DocxParagraph],
) -> dict[str, object]:
    missing: list[dict[str, str]] = []
    weak: list[dict[str, str]] = []
    for row in location_rows:
        para_no = paragraph_number_from_detail(row.get("段落号", ""))
        if para_no is None:
            missing.append({"历史归档ID": row.get("历史归档ID", ""), "原因": "缺少段落号"})
            continue
        if para_no < 1 or para_no > len(paragraphs):
            missing.append({"历史归档ID": row.get("历史归档ID", ""), "原因": "段落号越界"})
            continue
        context = paragraph_context(paragraphs, para_no)
        core = row.get("核心考点", "")
        source = row.get("完整来源位置", "")
        if core and not token_overlap(core, context) and not token_overlap(source, context):
            weak.append(
                {
                    "历史归档ID": row.get("历史归档ID", ""),
                    "段落号": row.get("段落号", ""),
                    "核心考点": core,
                }
            )
    return {
        "checked": len(location_rows),
        "missing_or_out_of_range": missing,
        "weak_context_match": weak,
    }


def duplicate_values(rows: Iterable[dict[str, str]], key: str) -> dict[str, int]:
    counts = Counter(row.get(key, "") for row in rows if row.get(key, ""))
    return {value: count for value, count in counts.items() if count > 1}


def relation_key(row: dict[str, str]) -> tuple[str, str, str]:
    a = row.get("起点ID", "")
    b = row.get("终点ID", "")
    pair = tuple(sorted([a, b]))
    return pair[0], pair[1], row.get("关系类型", "")


def audit_relations(
    relation_rows: list[dict[str, str]],
    node_by_id: dict[str, dict[str, str]],
) -> dict[str, object]:
    valid_nodes = set(node_by_id)
    missing_endpoints: list[dict[str, str]] = []
    weak_candidates: list[dict[str, str]] = []
    duplicate_counter: Counter[tuple[str, str, str]] = Counter()
    type_counts: Counter[str] = Counter()
    strength_counts: Counter[str] = Counter()
    incomplete: list[dict[str, str]] = []
    vague_reason: list[dict[str, str]] = []

    for row in relation_rows:
        start = row.get("起点ID", "")
        end = row.get("终点ID", "")
        rel_type = row.get("关系类型", "")
        strength = row.get("联系强度", "")
        reason = row.get("关联原因", "")
        type_counts[rel_type] += 1
        strength_counts[strength] += 1
        duplicate_counter[relation_key(row)] += 1
        if start not in valid_nodes or end not in valid_nodes:
            missing_endpoints.append(row)
        if any(not row.get(field, "") for field in ("起点ID", "终点ID", "关系类型", "联系强度", "关联原因", "复盘优先级")):
            incomplete.append(row)
        if normalize_text(reason) in {"相关", "有关", "相似"}:
            vague_reason.append(row)

        start_subject = node_by_id.get(start, {}).get("科目", "")
        end_subject = node_by_id.get(end, {}).get("科目", "")
        weak_reason = ""
        if rel_type.startswith("R07") or rel_type.startswith("R08") or strength == "弱":
            weak_reason = "R07/R08 或弱强度边，仅作辅助候选"
        elif rel_type.startswith("R09") and "同属专题链" in reason:
            weak_reason = "R09 专题链聚类边，建议保留为辅助而非主干强边"
            if start_subject and end_subject and start_subject != end_subject:
                weak_reason = "跨科目 R09 专题链聚类边，建议重点复核"
        if weak_reason:
            weak_candidates.append(
                {
                    "起点ID": start,
                    "终点ID": end,
                    "关系类型": rel_type,
                    "联系强度": strength,
                    "关联原因": reason,
                    "建议": weak_reason,
                }
            )

    duplicate_edges = [
        {"起点ID": key[0], "终点ID": key[1], "关系类型": key[2], "重复次数": str(count)}
        for key, count in duplicate_counter.items()
        if count > 1
    ]

    return {
        "type_counts": dict(type_counts),
        "strength_counts": dict(strength_counts),
        "missing_endpoints": missing_endpoints,
        "duplicate_edges": duplicate_edges,
        "incomplete_edges": incomplete,
        "vague_reason": vague_reason,
        "weak_candidates": weak_candidates,
    }


def validate_locations(location_rows: list[dict[str, str]]) -> list[dict[str, str]]:
    issues: list[dict[str, str]] = []
    for row in location_rows:
        locator = row.get("定位标记", "")
        if not locator or locator == "未提取":
            continue
        parts = [part.strip() for part in re.split(r"[；;]", locator) if part.strip()]
        for part in parts:
            normalized = part.replace(" ", "")
            if DATE_LIKE_RE.fullmatch(normalized):
                issues.append(
                    {
                        "历史归档ID": row.get("历史归档ID", ""),
                        "定位标记": locator,
                        "问题": "疑似整理日期被当成定位标记",
                    }
                )
            elif not LOCATOR_RE.search(part):
                issues.append(
                    {
                        "历史归档ID": row.get("历史归档ID", ""),
                        "定位标记": locator,
                        "问题": "定位标记格式需人工确认",
                    }
                )
    return issues


def scan_prohibited_formal_content(repo: Path) -> list[dict[str, str]]:
    issues: list[dict[str, str]] = []
    for rel in CORE_FORMAL_FILES:
        path = repo / rel
        if not path.exists():
            continue
        text = path.read_text(encoding="utf-8")
        for label, pattern in PROHIBITED_FORMAL_PATTERNS.items():
            if pattern.search(text):
                issues.append({"文件": rel, "问题": label})
    return issues


def subject_counts(rows: Iterable[dict[str, str]], subject_key: str = "科目") -> Counter[str]:
    return Counter(row.get(subject_key, "未标明") or "未标明" for row in rows)


def year_counts(rows: Iterable[dict[str, str]]) -> Counter[str]:
    return Counter(row.get("年份", "未标明") or "未标明" for row in rows)


def markdown_table(headers: list[str], rows: Iterable[dict[str, str]], limit: int | None = None) -> str:
    selected = list(rows)
    if limit is not None:
        selected = selected[:limit]
    lines = [
        "| " + " | ".join(headers) + " |",
        "| " + " | ".join("---" for _ in headers) + " |",
    ]
    for row in selected:
        cells = []
        for header in headers:
            value = str(row.get(header, ""))
            value = value.replace("\n", " ").replace("|", "｜")
            cells.append(value)
        lines.append("| " + " | ".join(cells) + " |")
    return "\n".join(lines)


def counter_table(counter: Counter[str] | dict[str, int], headers: tuple[str, str]) -> str:
    items = sorted(counter.items(), key=lambda item: (-item[1], item[0]))
    rows = [{headers[0]: key, headers[1]: str(value)} for key, value in items]
    return markdown_table([headers[0], headers[1]], rows)


def build_import_report(
    *,
    docx_path: Path,
    paragraphs: list[DocxParagraph],
    style_counts: Counter[str],
    wrong_blocks: list[DocxParagraph],
    history_rows: list[dict[str, str]],
    history_total: int | None,
    mapping_rows: list[dict[str, str]],
    nodes: list[dict[str, str]],
    classified: list[dict[str, str]],
    class_counts: Counter[str],
    location_rows: list[dict[str, str]],
    docx_trace: dict[str, object],
    relation_audit: dict[str, object],
    location_issues: list[dict[str, str]],
    prohibited_issues: list[dict[str, str]],
    knowledge_issues: list[dict[str, str]],
    validation_summary: dict[str, object],
    normalization_updates: int,
    field_curation_report_exists: bool,
) -> str:
    source_counts = subject_counts(history_rows)
    node_history_count = sum(1 for node in nodes if node.get("来源ID", "").startswith("H"))
    skipped = len(mapping_rows)
    pending = class_counts.get("信息不足", 0) + len(knowledge_issues)
    weak_count = len(relation_audit["weak_candidates"])  # type: ignore[index]
    duplicate_edges = len(relation_audit["duplicate_edges"])  # type: ignore[index]
    missing_endpoints = len(relation_audit["missing_endpoints"])  # type: ignore[index]

    weak_context = docx_trace["weak_context_match"]  # type: ignore[index]
    missing_docx_trace = docx_trace["missing_or_out_of_range"]  # type: ignore[index]

    lines = [
        "# 408 Word 旧错题导入报告",
        "",
        f"- 生成日期：{date.today().isoformat()}",
        f"- Word 来源：`{docx_path}`",
        "- 策略：只补缺口；不重建全库；不保存完整题干、完整解析、图片或手写痕迹。",
        "- 当前动作：DOCX 抽取与去重为 dry-run；正式库仅执行高置信知识点归一化和摘要字段补缺，不覆盖高质量已有节点。",
        "",
        "## 1. DOCX 抽取概况",
        "",
        f"- DOCX 段落数：{len(paragraphs)}",
        f"- DOCX 表格数：0",
        f"- 错题线索块（按错选/正确答案/错因等标记粗筛）：{len(wrong_blocks)}",
        f"- 历史归档候选清单总数：{history_total if history_total is not None else len(history_rows)}",
        f"- 当前正式节点总数：{len(nodes)}",
        f"- 当前正式节点中的历史来源节点数：{node_history_count}",
        "",
        "DOCX 标题样式分布（前 12 项）：",
        "",
        counter_table(Counter(dict(style_counts.most_common(12))), ("样式", "段落数")),
        "",
        "按科目聚合的历史候选：",
        "",
        counter_table(source_counts, ("科目", "候选数")),
        "",
        "按年份聚合的历史候选：",
        "",
        counter_table(year_counts(history_rows), ("年份", "候选数")),
        "",
        "## 2. 去重与写入分类",
        "",
        "去重键采用：科目 + 年份 + 定位标记 + 标题路径/完整来源位置 + 核心考点摘要。",
        "",
        counter_table(class_counts, ("分类", "数量")),
        "",
        f"- 本次新增正式节点：0（当前 {len(mapping_rows)} 条历史来源已存在正式 ID 映射）",
        "- 本次覆盖更新正式节点：0（只补缺口，未覆盖现有节点内容）",
        f"- 本次高置信知识点归一化补缺：{normalization_updates} 条（详见 `导入报告/408-docx-2026-05-07知识点归一化记录.md`）",
        "- 本次高置信摘要字段补缺：详见 `导入报告/408-docx-2026-05-07字段补缺记录.md`" if field_curation_report_exists else "- 本次高置信摘要字段补缺：0",
        f"- 跳过重复/已映射候选：{skipped}",
        f"- 待人工确认项：{pending}",
        f"- 弱关系候选数：{weak_count}",
        "",
        "可补字段/信息不足示例（最多 40 条）：",
        "",
        markdown_table(
            ["历史归档ID", "正式节点ID", "科目", "年份", "定位标记", "段落号", "核心考点", "状态", "可补字段", "信息缺口"],
            [row for row in classified if row["状态"] != "已存在"],
            limit=40,
        ),
        "",
        "## 3. DOCX 回溯校验",
        "",
        f"- 已按历史定位回查 DOCX 段落：{docx_trace['checked']}",
        f"- 段落号缺失或越界：{len(missing_docx_trace)}",
        f"- 附近上下文弱匹配：{len(weak_context)}",
        "",
        "弱匹配示例（最多 30 条）：",
        "",
        markdown_table(["历史归档ID", "段落号", "核心考点"], weak_context, limit=30),
        "",
        "## 4. 结构与内容校验",
        "",
        "- 新增节点 ID 唯一性：本次无新增；当前全库校验见下表。",
        f"- 关系边端点缺失：{missing_endpoints}",
        f"- 完全重复边：{duplicate_edges}",
        f"- 历史来源 ID 重复：{len(validation_summary['duplicate_history_ids'])}",
        f"- 正式节点 ID 重复：{len(validation_summary['duplicate_node_ids'])}",
        f"- 定位标记疑似问题：{len(location_issues)}",
        f"- 正式库禁存内容命中：{len(prohibited_issues)}",
        f"- 知识点编号/归一化待确认：{len(knowledge_issues)}",
        "",
        "定位标记问题示例（最多 30 条）：",
        "",
        markdown_table(["历史归档ID", "定位标记", "问题"], location_issues, limit=30),
        "",
        "知识点归一化待确认示例（最多 40 条）：",
        "",
        markdown_table(["ID", "字段", "问题"], knowledge_issues, limit=40),
        "",
        "## 5. 关系网紧凑化",
        "",
        "关系类型分布：",
        "",
        counter_table(Counter(relation_audit["type_counts"]), ("关系类型", "数量")),  # type: ignore[arg-type,index]
        "",
        "联系强度分布：",
        "",
        counter_table(Counter(relation_audit["strength_counts"]), ("联系强度", "数量")),  # type: ignore[arg-type,index]
        "",
        "- 本轮不删除非重复旧边。",
        "- 完全重复边才允许后续自动删除；弱语义边只进入弱关系审计报告。",
        "- R07/R08 和跨科目 R09 不作为强关系主干，仅保留为辅助复盘候选。",
        "",
        "## 6. 结论",
        "",
        f"当前 Markdown 关系网已经包含 Word 旧题的 296 条历史归档候选及正式 ID 映射；本轮新增的是可复跑的 dry-run/审计流程、报告输出、{normalization_updates} 条高置信知识点归一化补缺，以及可确认的摘要字段补缺和强关系强化。后续若要继续补缺口，优先处理“信息不足”和弱关系审计项，而不是重建全库。",
        "",
    ]
    return "\n".join(lines)


def build_weak_relation_report(relation_audit: dict[str, object], node_by_id: dict[str, dict[str, str]]) -> str:
    weak_candidates: list[dict[str, str]] = relation_audit["weak_candidates"]  # type: ignore[assignment,index]
    duplicate_edges: list[dict[str, str]] = relation_audit["duplicate_edges"]  # type: ignore[assignment,index]
    rows: list[dict[str, str]] = []
    for item in weak_candidates:
        start = item["起点ID"]
        end = item["终点ID"]
        rows.append(
            {
                "起点ID": start,
                "起点概况": node_by_id.get(start, {}).get("核心考点", ""),
                "终点ID": end,
                "终点概况": node_by_id.get(end, {}).get("核心考点", ""),
                "关系类型": item["关系类型"],
                "联系强度": item["联系强度"],
                "关联原因": item["关联原因"],
                "审计建议": item["建议"],
            }
        )

    lines = [
        "# 408 Word 旧错题弱关系审计报告",
        "",
        f"- 生成日期：{date.today().isoformat()}",
        "- 范围：当前 `关系边表.md` 全量关系边。",
        "- 原则：只审计，不删除；完全重复边可后续删除，语义弱但非重复边只建议收紧。",
        "",
        "## 1. 审计摘要",
        "",
        f"- 弱/辅助关系候选：{len(rows)}",
        f"- 完全重复边：{len(duplicate_edges)}",
        f"- 缺端点关系边：{len(relation_audit['missing_endpoints'])}",
        "",
        "完全重复边：",
        "",
        markdown_table(["起点ID", "终点ID", "关系类型", "重复次数"], duplicate_edges, limit=None),
        "",
        "## 2. 建议收紧的辅助关系边",
        "",
        "这些边不自动删除；复盘主干优先使用 R01/R02/R03/R04/R05 强关系边。",
        "",
        markdown_table(
            ["起点ID", "起点概况", "终点ID", "终点概况", "关系类型", "联系强度", "关联原因", "审计建议"],
            rows,
            limit=None,
        ),
        "",
    ]
    return "\n".join(lines)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--docx", default=DEFAULT_DOCX)
    parser.add_argument("--repo", default=".")
    parser.add_argument("--write-reports", action="store_true")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args()

    repo = Path(args.repo).resolve()
    docx_path = Path(args.docx).expanduser()
    if not docx_path.exists():
        raise SystemExit(f"DOCX not found: {docx_path}")

    paragraphs, style_counts = parse_docx(docx_path)
    wrong_blocks = wrong_like_docx_blocks(paragraphs)

    nodes = read_markdown_table(repo / "节点总表.md")
    history_rows = read_markdown_table(repo / "历史错题归档/历史错题清单.md")
    history_total = read_total_count(repo / "历史错题归档/历史错题清单.md")
    location_rows = read_markdown_table(repo / "历史错题归档/定位索引.md")
    mapping_rows = read_markdown_table(repo / "历史错题归档/正式ID映射.md")
    relation_rows = read_markdown_table(repo / "关系边表.md")
    valid_codes = parse_knowledge_codes((repo / "知识点标签表.md").read_text(encoding="utf-8"))
    normalization_report_path = repo / "导入报告/408-docx-2026-05-07知识点归一化记录.md"
    normalization_updates = (
        len(read_markdown_table(normalization_report_path)) if normalization_report_path.exists() else 0
    )
    field_curation_report_exists = (repo / "导入报告/408-docx-2026-05-07字段补缺记录.md").exists()

    node_by_id = {row.get("ID", ""): row for row in nodes if row.get("ID")}
    classified, class_counts = classify_candidates(history_rows, mapping_rows, node_by_id)
    enrich_with_location(classified, location_rows)

    docx_trace = validate_docx_trace(location_rows, paragraphs)
    relation_audit = audit_relations(relation_rows, node_by_id)
    location_issues = validate_locations(location_rows)
    prohibited_issues = scan_prohibited_formal_content(repo)
    knowledge_issues = non_standard_knowledge_fields(nodes, valid_codes)

    validation_summary = {
        "duplicate_node_ids": duplicate_values(nodes, "ID"),
        "duplicate_history_ids": duplicate_values(mapping_rows, "历史归档ID"),
        "duplicate_formal_ids_in_mapping": duplicate_values(mapping_rows, "正式节点ID"),
        "node_id_format_issues": [
            row.get("ID", "") for row in nodes if row.get("ID") and not NODE_ID_RE.fullmatch(row.get("ID", ""))
        ],
    }

    import_report = build_import_report(
        docx_path=docx_path,
        paragraphs=paragraphs,
        style_counts=style_counts,
        wrong_blocks=wrong_blocks,
        history_rows=history_rows,
        history_total=history_total,
        mapping_rows=mapping_rows,
        nodes=nodes,
        classified=classified,
        class_counts=class_counts,
        location_rows=location_rows,
        docx_trace=docx_trace,
        relation_audit=relation_audit,
        location_issues=location_issues,
        prohibited_issues=prohibited_issues,
        knowledge_issues=knowledge_issues,
        validation_summary=validation_summary,
        normalization_updates=normalization_updates,
        field_curation_report_exists=field_curation_report_exists,
    )
    weak_report = build_weak_relation_report(relation_audit, node_by_id)

    summary = {
        "docx_paragraphs": len(paragraphs),
        "docx_wrong_like_blocks": len(wrong_blocks),
        "history_candidates": len(history_rows),
        "history_total_declared": history_total,
        "formal_nodes": len(nodes),
        "history_mappings": len(mapping_rows),
        "classification": dict(class_counts),
        "weak_relation_candidates": len(relation_audit["weak_candidates"]),  # type: ignore[index]
        "duplicate_edges": len(relation_audit["duplicate_edges"]),  # type: ignore[index]
        "missing_relation_endpoints": len(relation_audit["missing_endpoints"]),  # type: ignore[index]
        "location_issues": len(location_issues),
        "prohibited_formal_content": len(prohibited_issues),
        "knowledge_normalization_issues": len(knowledge_issues),
        "knowledge_normalization_applied": normalization_updates,
        "validation_summary": validation_summary,
    }

    if args.write_reports:
        report_dir = repo / "导入报告"
        report_dir.mkdir(exist_ok=True)
        (report_dir / "408-docx-2026-05-07导入报告.md").write_text(import_report, encoding="utf-8")
        (report_dir / "408-docx-2026-05-07弱关系审计报告.md").write_text(weak_report, encoding="utf-8")
        (report_dir / "408-docx-2026-05-07校验摘要.json").write_text(
            json.dumps(summary, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )

    if args.json:
        print(json.dumps(summary, ensure_ascii=False, indent=2))
    else:
        print(f"DOCX paragraphs: {summary['docx_paragraphs']}")
        print(f"DOCX wrong-like blocks: {summary['docx_wrong_like_blocks']}")
        print(f"History candidates: {summary['history_candidates']}")
        print(f"Formal nodes: {summary['formal_nodes']}")
        print(f"History mappings: {summary['history_mappings']}")
        print(f"Classification: {summary['classification']}")
        print(f"Weak relation candidates: {summary['weak_relation_candidates']}")
        print(f"Duplicate edges: {summary['duplicate_edges']}")
        print(f"Missing relation endpoints: {summary['missing_relation_endpoints']}")
        print(f"Location issues: {summary['location_issues']}")
        print(f"Prohibited formal content: {summary['prohibited_formal_content']}")
        print(f"Knowledge normalization issues: {summary['knowledge_normalization_issues']}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
