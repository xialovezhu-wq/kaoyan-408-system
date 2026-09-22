#!/usr/bin/env python3
"""408 扩展质量审计。

本脚本只读检查正式关系网、详情库覆盖和题库候选隔离状态。
默认对 T8 明确负责补齐的详情页/覆盖索引缺口标记为 PENDING_T8，不阻断 T7。
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from normalize_408_knowledge_points import load_valid_labels, read_table_with_preamble
from refresh_coverage_index import (
    COVERAGE_HEADERS,
    SUBJECTS,
    derive_wiki_fields,
    edge_counts,
    read_coverage_rows,
)


ID_RE = re.compile(r"\b(?:DS|CO|OS|CN)_(?:\d{4}|UNK)_\d{3}\b")
TQ_RE = re.compile(r"\bTQ_[A-Z0-9]+_\d{3}\b")
CODE_RE = re.compile(r"^(?:DS|CO|OS|CN)\d{2}-\d{2}\b")
ERROR_CODE_RE = re.compile(r"^E(?:0[1-9]|10)\b")

SUBJECT_VIEWS = {
    "DS": "节点库/数据结构节点.md",
    "CO": "节点库/计算机组成原理节点.md",
    "OS": "节点库/操作系统节点.md",
    "CN": "节点库/计算机网络节点.md",
}

FORMAL_SCAN_FILES = [
    "节点总表.md",
    "关系边表.md",
    "年份索引.md",
    "知识点命中索引.md",
    "错题日期索引.md",
    "错题复做记录.md",
    "复习单元总表.md",
]

KNOWN_T8_DETAIL_PLACEHOLDERS: set[str] = set()  # Public export: no private exception identities.

ALLOWED_STRENGTH = {"强", "中", "弱"}
ALLOWED_PRIORITY = {"高", "中", "低"}
PLACEHOLDER_KP = {"", "待补充", "暂无明确副知识点", "暂无明确命中知识点", "未记录"}
SAFE_LINE_MARKERS = {
    "不保存",
    "不存",
    "不写",
    "不把",
    "不输出",
    "不复制",
    "不搬运",
    "不暴露",
    "不记录",
    "未保存",
    "禁止",
    "不得",
    "已隐藏",
    "复做保护",
    "待补充",
    "详情库",
    "可视化详情入口待补充",
}
NEGATED_PROTECTED_CONTENT_RE = re.compile(
    r"(?:未提供|未记录|未保存|无)(?:任何)?(?:题目图片|解析图片|手写痕迹)"
)
LEAK_RE = re.compile(
    r"正确答案|标准答案|错误答案|我的答案|错选答案|正确选项|错误选项|选项字母|"
    r"完整题干|完整解析|题目图片|解析图片|手写痕迹|答案是|答案为|应选\s*[A-D]|[A-D]\s*[✅✓]"
)


@dataclass
class Finding:
    status: str
    name: str
    detail: str
    samples: list[str] = field(default_factory=list)


def split_multi(value: str) -> list[str]:
    parts = re.split(r"[；;、]", value or "")
    return [part.strip() for part in parts if part.strip()]


def edge_code(value: str) -> str:
    return (value or "").split()[0]


def node_subject(node_id: str) -> str:
    return node_id.split("_", 1)[0]


def collect_root_cards(repo: Path, node_ids: set[str]) -> list[Path]:
    return sorted(path for path in repo.glob("*.md") if path.stem in node_ids)


def collect_markdown_files(path: Path) -> list[Path]:
    if not path.exists():
        return []
    if path.is_file():
        return [path]
    return sorted(p for p in path.rglob("*.md") if p.is_file())


def is_safe_leak_line(line: str) -> bool:
    if any(marker in line for marker in SAFE_LINE_MARKERS):
        return True
    remaining = NEGATED_PROTECTED_CONTENT_RE.sub("", line)
    return LEAK_RE.search(remaining) is None


def report_path(path: Path, repo: Path) -> str:
    try:
        return str(path.relative_to(repo))
    except ValueError:
        return str(path)


def add(findings: list[Finding], status: str, name: str, detail: str, samples: list[str] | None = None) -> None:
    findings.append(Finding(status=status, name=name, detail=detail, samples=samples or []))


def check_error_tags(rows: list[dict[str, str]], findings: list[Finding]) -> None:
    bad: list[str] = []
    for row in rows:
        value = row.get("错因标签", "")
        tags = split_multi(value)
        if value.strip() == "暂无明确错因":
            continue
        if not tags:
            bad.append(f"{row.get('ID')}: 空错因标签")
            continue
        for tag in tags:
            if tag == "暂无明确错因":
                continue
            if not ERROR_CODE_RE.match(tag):
                bad.append(f"{row.get('ID')}: {tag}")
    add(findings, "FAIL" if bad else "PASS", "错因标签合法性", f"非法项 {len(bad)}", bad[:10])


def check_knowledge_tags(rows: list[dict[str, str]], valid_labels: set[str], findings: list[Finding]) -> None:
    valid_codes = {label.split()[0] for label in valid_labels if CODE_RE.match(label)}
    bad: list[str] = []
    for row in rows:
        for column in ("副知识点", "命中知识点"):
            value = row.get(column, "")
            if value.strip() in PLACEHOLDER_KP:
                continue
            for code in re.findall(r"(?:DS|CO|OS|CN)\d{2}-\d{2}", value):
                if code not in valid_codes:
                    bad.append(f"{row.get('ID')} {column}: {code}")
    add(findings, "FAIL" if bad else "PASS", "副知识点/命中知识点合法性", f"非法项 {len(bad)}", bad[:10])


def check_date_and_main(repo: Path, nodes: dict[str, dict[str, str]], findings: list[Finding]) -> None:
    _, _, date_rows = read_table_with_preamble(repo / "错题日期索引.md")
    date_by_id = {row["ID"]: row for row in date_rows if row.get("ID")}
    missing = sorted(set(nodes) - set(date_by_id))
    extra = sorted(set(date_by_id) - set(nodes))
    date_bad: list[str] = []
    main_bad: list[str] = []
    for node_id, row in nodes.items():
        date_row = date_by_id.get(node_id)
        if not date_row:
            continue
        if row.get("最近复做日期", "") != date_row.get("最近复做日期", ""):
            date_bad.append(
                f"{node_id}: 节点总表={row.get('最近复做日期', '')} 日期索引={date_row.get('最近复做日期', '')}"
            )
        if row.get("主知识点", "") != date_row.get("主知识点", ""):
            main_bad.append(f"{node_id}: 节点总表={row.get('主知识点', '')} 日期索引={date_row.get('主知识点', '')}")
    coverage_bad = [f"missing={missing[:10]}", f"extra={extra[:10]}"] if missing or extra else []
    add(
        findings,
        "FAIL" if missing or extra or date_bad else "PASS",
        "日期索引最近复做日期一致",
        f"缺失 {len(missing)} | 多余 {len(extra)} | 日期不一致 {len(date_bad)}",
        coverage_bad + date_bad[:10],
    )
    add(findings, "FAIL" if main_bad else "PASS", "日期索引主知识点一致", f"不一致 {len(main_bad)}", main_bad[:10])

    view_bad: list[str] = []
    for prefix, rel in SUBJECT_VIEWS.items():
        path = repo / rel
        _, _, rows = read_table_with_preamble(path)
        view_by_id = {row["ID"]: row for row in rows if row.get("ID")}
        expected = {node_id for node_id in nodes if node_subject(node_id) == prefix}
        missing_view = sorted(expected - set(view_by_id))
        extra_view = sorted(set(view_by_id) - expected)
        if missing_view or extra_view:
            view_bad.append(f"{rel}: missing={missing_view[:5]} extra={extra_view[:5]}")
        for node_id in sorted(expected & set(view_by_id)):
            expected_date = nodes[node_id].get("最近复做日期", "")
            actual_date = view_by_id[node_id].get("最近复做日期", "")
            if expected_date != actual_date:
                view_bad.append(f"{rel} {node_id}: 节点总表={expected_date} 节点库={actual_date}")
    add(findings, "FAIL" if view_bad else "PASS", "节点库最近复做日期镜像一致", f"异常 {len(view_bad)}", view_bad[:10])


def check_leaks(repo: Path, node_ids: set[str], findings: list[Finding]) -> None:
    files: list[Path] = []
    files.extend(repo / rel for rel in FORMAL_SCAN_FILES if (repo / rel).exists())
    files.extend(collect_root_cards(repo, node_ids))
    files.extend(collect_markdown_files(repo / "题库候选"))
    hits: list[str] = []
    for path in sorted(set(files)):
        for lineno, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
            if not LEAK_RE.search(line):
                continue
            if is_safe_leak_line(line):
                continue
            hits.append(f"{report_path(path, repo)}:{lineno}: {line.strip()[:90]}")
    add(findings, "FAIL" if hits else "PASS", "正式层/候选层泄露词扫描", f"高风险行 {len(hits)}", hits[:15])


def check_edges(rows: list[dict[str, str]], findings: list[Finding]) -> None:
    bad_strength: list[str] = []
    bad_priority: list[str] = []
    strong_cross: list[str] = []
    template_reasons: list[str] = []
    for idx, row in enumerate(rows, start=1):
        etype = edge_code(row.get("关系类型", ""))
        strength = row.get("联系强度", "")
        priority = row.get("复盘优先级", "")
        reason = row.get("关联原因", "")
        edge_id = f"{row.get('起点ID')}->{row.get('终点ID')}#{idx}"
        if strength not in ALLOWED_STRENGTH:
            bad_strength.append(f"{edge_id}: {strength}")
        if priority not in ALLOWED_PRIORITY:
            bad_priority.append(f"{edge_id}: {priority}")
        if etype in {"R07", "R08"} and strength == "强":
            strong_cross.append(edge_id)
        if re.match(r"^(?:同一主知识点|同属专题链|同一模糊概念)[：:]", reason):
            template_reasons.append(edge_id)
    add(findings, "FAIL" if bad_strength else "PASS", "关系强度枚举", f"非法项 {len(bad_strength)}", bad_strength[:10])
    add(findings, "FAIL" if bad_priority else "PASS", "复盘优先级枚举", f"非法项 {len(bad_priority)}", bad_priority[:10])
    add(findings, "FAIL" if strong_cross else "PASS", "R07/R08 强关系为零", f"强关系 {len(strong_cross)}", strong_cross[:10])
    add(
        findings,
        "FAIL" if template_reasons else "PASS",
        "模板关系理由为零",
        f"模板边 {len(template_reasons)}",
        template_reasons[:10],
    )


def check_tq_absence(repo: Path, findings: list[Finding]) -> None:
    files: list[Path] = []
    files.extend(repo / rel for rel in FORMAL_SCAN_FILES if (repo / rel).exists())
    files.extend(collect_markdown_files(repo / "节点库"))
    files.extend(collect_markdown_files(repo / "专题链"))
    hits: list[str] = []
    for path in sorted(set(files)):
        for lineno, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
            if TQ_RE.search(line):
                hits.append(f"{report_path(path, repo)}:{lineno}: {line.strip()[:90]}")
    add(findings, "FAIL" if hits else "PASS", "TQ 候选未进入正式层", f"命中 {len(hits)}", hits[:10])


def _coverage_table_mismatches(
    repo: Path,
    nodes: dict[str, dict[str, str]],
    expected_ids: set[str],
    rows: list[dict[str, str]],
    expected_edge_counts: Counter[str],
) -> tuple[list[str], list[str], list[str], list[str], list[str]]:
    id_list = [row.get("ID", "") for row in rows if ID_RE.fullmatch(row.get("ID", ""))]
    actual_ids = set(id_list)
    duplicates = sorted(node_id for node_id, count in Counter(id_list).items() if count > 1)
    missing = sorted(expected_ids - actual_ids)
    extra = sorted(actual_ids - expected_ids)
    by_id = {row["ID"]: row for row in rows if row.get("ID") in actual_ids}
    source_bad: list[str] = []
    wiki_bad: list[str] = []
    source_fields = {
        "年份": "年份",
        "科目": "科目",
        "主知识点": "主知识点",
        "来源 ID": "来源ID",
    }
    for node_id in sorted(expected_ids & actual_ids):
        node = nodes[node_id]
        coverage = by_id[node_id]
        for coverage_field, node_field in source_fields.items():
            expected = node.get(node_field, "")
            actual = coverage.get(coverage_field, "")
            if actual != expected:
                source_bad.append(f"{node_id} {coverage_field}: 正式={expected} 覆盖={actual}")
        expected_count = str(expected_edge_counts[node_id])
        actual_count = coverage.get("关系边数", "")
        if actual_count != expected_count:
            source_bad.append(f"{node_id} 关系边数: 正式={expected_count} 覆盖={actual_count}")
        expected_status, expected_page = derive_wiki_fields(repo, node, coverage)
        actual_status = coverage.get("wiki 状态", "")
        actual_page = coverage.get("wiki 页面", "")
        if (actual_status, actual_page) != (expected_status, expected_page):
            wiki_bad.append(
                f"{node_id}: 期望=({expected_status}, {expected_page}) "
                f"覆盖=({actual_status}, {actual_page})"
            )
    return missing, extra, duplicates, source_bad, wiki_bad


def _summary_mismatches(path: Path, expected: dict[str, int]) -> list[str]:
    if not path.exists():
        return [f"{path.name}: 文件缺失"]
    text = path.read_text(encoding="utf-8")
    bad: list[str] = []
    for label, value in expected.items():
        matches = re.findall(rf"^- {re.escape(label)}：\s*(\d+)\s*$", text, re.M)
        if len(matches) != 1:
            bad.append(f"{path.name} {label}: 期望唯一摘要值 {value}，实际匹配 {matches}")
        elif int(matches[0]) != value:
            bad.append(f"{path.name} {label}: 期望={value} 实际={matches[0]}")
    return bad


def _expected_compiled_count(
    repo: Path,
    nodes: dict[str, dict[str, str]],
    expected_ids: set[str],
    rows: list[dict[str, str]],
) -> int:
    by_id = {row.get("ID", ""): row for row in rows}
    return sum(
        1
        for node_id in expected_ids
        if derive_wiki_fields(repo, nodes[node_id], by_id.get(node_id, {}))[0] == "compiled"
    )


def check_details_and_coverage(
    repo: Path,
    nodes: dict[str, dict[str, str]],
    edge_rows: list[dict[str, str]],
    findings: list[Finding],
    details_root: Path,
) -> None:
    node_ids = set(nodes)
    details_cards = details_root / "cards"
    detail_ids = {path.stem for path in details_cards.glob("*.md") if ID_RE.fullmatch(path.stem)} if details_cards.exists() else set()
    missing_details = sorted(node_ids - detail_ids)
    extra_details = sorted(detail_ids - node_ids)
    if not missing_details:
        status = "PASS"
    elif set(missing_details) <= KNOWN_T8_DETAIL_PLACEHOLDERS:
        status = "PENDING_T8"
    else:
        status = "FAIL"
    samples = [f"missing={missing_details[:20]}", f"extra={extra_details[:10]}"] if missing_details or extra_details else []
    add(findings, status, "详情库 cards 覆盖", f"正式 {len(node_ids)} | 详情 {len(detail_ids & node_ids)} | 缺 {len(missing_details)}", samples)

    coverage_path = repo / "wiki/coverage/408-COVERAGE-001_正式错题全量覆盖索引.md"
    coverage_rows = [row for row in read_coverage_rows(coverage_path) if ID_RE.fullmatch(row.get("ID", ""))]
    expected_edge_counts = edge_counts(edge_rows)
    missing_coverage, extra_coverage, duplicate_coverage, source_bad, wiki_bad = _coverage_table_mismatches(
        repo,
        nodes,
        node_ids,
        coverage_rows,
        expected_edge_counts,
    )
    coverage_ids = {row["ID"] for row in coverage_rows}
    if not missing_coverage and not extra_coverage and not duplicate_coverage:
        coverage_status = "PASS"
    else:
        coverage_status = "FAIL"
    coverage_samples = (
        [
            f"missing={missing_coverage[:20]}",
            f"extra={extra_coverage[:10]}",
            f"duplicates={duplicate_coverage[:10]}",
        ]
        if missing_coverage or extra_coverage or duplicate_coverage
        else []
    )
    add(
        findings,
        coverage_status,
        "覆盖索引正式节点数一致",
        f"正式 {len(node_ids)} | 覆盖 {len(coverage_ids & node_ids)} | 缺 {len(missing_coverage)} | "
        f"多 {len(extra_coverage)} | 重复 {len(duplicate_coverage)}",
        coverage_samples,
    )

    add(
        findings,
        "FAIL" if source_bad else "PASS",
        "覆盖索引来源字段一致",
        f"年份/科目/主知识点/来源 ID/关系边数不一致 {len(source_bad)}",
        source_bad[:15],
    )
    add(
        findings,
        "FAIL" if wiki_bad else "PASS",
        "覆盖索引 wiki 状态/页面现行有效",
        f"陈旧或无效 {len(wiki_bad)}",
        wiki_bad[:15],
    )

    main_coverage_by_id = {row["ID"]: row for row in coverage_rows}
    subject_bad: list[str] = []
    subject_summary_bad: list[str] = []
    for prefix, (_, filename) in SUBJECTS.items():
        path = repo / "wiki/coverage" / filename
        subject_rows = [row for row in read_coverage_rows(path) if ID_RE.fullmatch(row.get("ID", ""))]
        expected_subject_ids = {node_id for node_id in node_ids if node_id.startswith(prefix + "_")}
        missing, extra, duplicates, fields, pages = _coverage_table_mismatches(
            repo,
            nodes,
            expected_subject_ids,
            subject_rows,
            expected_edge_counts,
        )
        if missing or extra or duplicates:
            subject_bad.append(
                f"{prefix}: missing={missing[:10]} extra={extra[:10]} duplicates={duplicates[:10]}"
            )
        subject_bad.extend(f"{prefix}: {item}" for item in (fields + pages)[:15])
        subject_by_id = {row["ID"]: row for row in subject_rows}
        for node_id in sorted(expected_subject_ids & set(subject_by_id) & set(main_coverage_by_id)):
            differing = [
                field
                for field in COVERAGE_HEADERS
                if subject_by_id[node_id].get(field, "") != main_coverage_by_id[node_id].get(field, "")
            ]
            if differing:
                subject_bad.append(f"{prefix}: {node_id} 与主覆盖表字段不一致：{'、'.join(differing)}")
        expected_compiled = _expected_compiled_count(
            repo,
            nodes,
            expected_subject_ids,
            subject_rows,
        )
        subject_summary_bad.extend(
            _summary_mismatches(
                path,
                {
                    "正式节点": len(expected_subject_ids),
                    "已编译": expected_compiled,
                    "仅 formal source": len(expected_subject_ids) - expected_compiled,
                },
            )
        )
    add(
        findings,
        "FAIL" if subject_bad else "PASS",
        "分科覆盖表完整且字段一致",
        f"异常 {len(subject_bad)}",
        subject_bad[:20],
    )

    main_compiled = _expected_compiled_count(repo, nodes, node_ids, coverage_rows)
    main_summary_bad = _summary_mismatches(
        coverage_path,
        {
            "正式节点总数": len(node_ids),
            "已有 wiki 编译覆盖": main_compiled,
            "仅 formal source 登记": len(node_ids) - main_compiled,
        },
    )
    summary_bad = main_summary_bad + subject_summary_bad
    add(
        findings,
        "FAIL" if summary_bad else "PASS",
        "覆盖索引主表/分科表摘要计数一致",
        f"异常 {len(summary_bad)}",
        summary_bad[:20],
    )


def check_historical_chain(repo: Path, rows: list[dict[str, str]], findings: list[Finding]) -> None:
    _, _, hist_rows = read_table_with_preamble(repo / "历史错题归档/历史错题清单.md")
    _, _, map_rows = read_table_with_preamble(repo / "历史错题归档/正式ID映射.md")
    hist_ids = {row.get("历史归档ID", "") for row in hist_rows}
    mapped_ids = {row.get("历史归档ID", "") for row in map_rows}
    source_h = {
        row.get("来源ID", "")
        for row in rows
        if re.fullmatch(r"H(?:DS|CO|OS|CN)_\d{4}", row.get("来源ID", ""))
    }
    formal_ids = {row.get("ID", "") for row in rows}
    ledger = repo / "wiki/study_vaults/408-full/state/formal-node-deletions.jsonl"
    tombstones: dict[str, dict[str, object]] = {}
    malformed: list[str] = []
    if ledger.is_file():
        for line_no, raw in enumerate(ledger.read_text(encoding="utf-8").splitlines(), 1):
            if not raw.strip():
                continue
            try:
                event = json.loads(raw)
            except json.JSONDecodeError:
                malformed.append(f"ledger line {line_no}: invalid JSON")
                continue
            if not isinstance(event, dict) or event.get("operation") != "historical_source_tombstone":
                continue
            source_id = str(event.get("source_id") or "")
            deleted_id = str(event.get("deleted_node_id") or "")
            if not re.fullmatch(r"H(?:DS|CO|OS|CN)_\d{4}", source_id):
                malformed.append(f"ledger line {line_no}: invalid tombstone source {source_id!r}")
                continue
            if not ID_RE.fullmatch(deleted_id):
                malformed.append(f"ledger line {line_no}: invalid deleted node {deleted_id!r}")
                continue
            tombstones[source_id] = event
    tombstone_ids = set(tombstones)
    overlap = sorted(mapped_ids & tombstone_ids)
    active_deleted = sorted(
        str(event.get("deleted_node_id"))
        for event in tombstones.values()
        if str(event.get("deleted_node_id")) in formal_ids
    )
    missing_terminal = sorted(hist_ids - mapped_ids - tombstone_ids)
    extra_terminal = sorted((mapped_ids | tombstone_ids) - hist_ids)
    source_mismatch = sorted(mapped_ids ^ source_h)
    ok = not (malformed or overlap or active_deleted or missing_terminal or extra_terminal or source_mismatch)
    detail = (
        f"历史清单 {len(hist_ids)} | 活动映射 {len(mapped_ids)} | "
        f"删除终止记录 {len(tombstone_ids)} | 正式来源H {len(source_h)}"
    )
    samples = list(malformed)
    if overlap:
        samples.append(f"映射与 tombstone 重叠={overlap[:10]}")
    if active_deleted:
        samples.append(f"tombstone 节点仍活动={active_deleted[:10]}")
    if missing_terminal:
        samples.append(f"缺终态={missing_terminal[:10]}")
    if extra_terminal:
        samples.append(f"多余终态={extra_terminal[:10]}")
    if source_mismatch:
        samples.append(f"活动映射/正式来源不一致={source_mismatch[:10]}")
    add(findings, "PASS" if ok else "FAIL", "历史归档链条终态一致", detail, samples[:20])


def write_report(path: Path, findings: list[Finding], failed: bool) -> None:
    def esc(value: str) -> str:
        return value.replace("|", r"\|")

    lines = [
        "# 408 扩展审计报告",
        "",
        f"结论：{'FAIL' if failed else 'PASS'}",
        "",
        "| 状态 | 检查项 | 结果 |",
        "|---|---|---|",
    ]
    for finding in findings:
        lines.append(f"| {finding.status} | {esc(finding.name)} | {esc(finding.detail)} |")
        for sample in finding.samples:
            lines.append(f"|  | 样例 | `{esc(sample)}` |")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lines).rstrip() + "\n", encoding="utf-8")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--repo", default=".")
    parser.add_argument(
        "--details-root",
        default="/Users/your-user/Documents/kaoyan-408-details",
        help="408 详情库根目录",
    )
    parser.add_argument("--report", default=None)
    args = parser.parse_args()
    repo = Path(args.repo).resolve()

    _, _, node_rows = read_table_with_preamble(repo / "节点总表.md")
    _, _, edge_rows = read_table_with_preamble(repo / "关系边表.md")
    rows = [row for row in node_rows if ID_RE.fullmatch(row.get("ID", ""))]
    nodes = {row["ID"]: row for row in rows}
    valid_labels = load_valid_labels(repo)

    findings: list[Finding] = []
    check_error_tags(rows, findings)
    check_knowledge_tags(rows, valid_labels, findings)
    check_date_and_main(repo, nodes, findings)
    check_leaks(repo, set(nodes), findings)
    check_edges(edge_rows, findings)
    check_tq_absence(repo, findings)
    check_details_and_coverage(repo, nodes, edge_rows, findings, Path(args.details_root).resolve())
    check_historical_chain(repo, rows, findings)

    failed = any(finding.status == "FAIL" for finding in findings)
    for finding in findings:
        print(f"[{finding.status}] {finding.name}: {finding.detail}")
        for sample in finding.samples[:5]:
            print(f"  - {sample}")
    if args.report:
        write_report(repo / args.report, findings, failed)
        print(f"report={args.report}")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
