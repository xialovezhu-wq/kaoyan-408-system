#!/usr/bin/env python3
"""Strict completeness and boundary audit for the 408 true-exam corpus."""

from __future__ import annotations

import csv
import json
import re
from collections import Counter, defaultdict
from pathlib import Path

import build_true_exam_corpus as build
import finalize_true_exam_corpus as finalize


ROOT = build.ROOT
DETAILS = build.DETAILS
CORPUS = build.CORPUS
INDEX = CORPUS / "indexes" / "question-index.csv"


def taxonomy() -> set[str]:
    pattern = re.compile(r"^-\s+((?:DS|CO|OS|CN)\d{2}-\d{2})\s+")
    result = set()
    for line in (ROOT / "知识点标签表.md").read_text(encoding="utf-8").splitlines():
        match = pattern.match(line)
        if match:
            result.add(match.group(1))
    return result


def code(value: str) -> str | None:
    match = re.match(r"^((?:DS|CO|OS|CN)\d{2}-\d{2})(?:\s|$)", value.strip())
    return match.group(1) if match else None


def split_labels(value: str) -> list[str]:
    return [part.strip() for part in re.split(r"[；;]", value) if part.strip()]


def load_rows(path: Path) -> list[dict[str, str]]:
    with path.open(encoding="utf-8-sig", newline="") as handle:
        return list(csv.DictReader(handle))


def main() -> int:
    failures: list[str] = []
    warnings: list[str] = []
    labels = taxonomy()
    rows = load_rows(INDEX)
    expected_ids = {
        f"EXAM408-{year}-Q{number:02d}"
        for year in range(2009, 2026)
        for number in range(1, 48)
    }
    ids = [row["exam_question_id"] for row in rows]
    id_set = set(ids)
    if len(rows) != 799:
        failures.append(f"question-index rows={len(rows)}, expected 799")
    if len(id_set) != len(ids):
        failures.append(f"duplicate canonical IDs={len(ids) - len(id_set)}")
    if id_set != expected_ids:
        failures.append(
            f"canonical ID set mismatch: missing={sorted(expected_ids-id_set)[:10]}, extra={sorted(id_set-expected_ids)[:10]}"
        )

    year_counts = Counter(int(row["year"]) for row in rows)
    for year in range(2009, 2026):
        if year_counts[year] != 47:
            failures.append(f"{year} rows={year_counts[year]}, expected 47")

    missing_assets = []
    missing_question_ocr = []
    missing_solution_ocr = []
    bad_knowledge = []
    pending_knowledge = []
    mismatched_solution_headers = []
    for row in rows:
        exam_id = row["exam_question_id"]
        question_assets = json.loads(row["question_assets"])
        solution_assets = json.loads(row["solution_assets"])
        if not question_assets or not solution_assets:
            missing_assets.append(exam_id)
        for relative in question_assets + solution_assets:
            if not (DETAILS / relative).exists():
                missing_assets.append(f"{exam_id}:{relative}")
        if len(row.get("question_ocr", "").strip()) < 10:
            missing_question_ocr.append(exam_id)
        if len(row.get("solution_ocr", "").strip()) < 10:
            missing_solution_ocr.append(exam_id)
        main_code = code(row.get("main_knowledge", ""))
        hits = [code(value) for value in split_labels(row.get("hit_knowledge", ""))]
        if not main_code or main_code not in labels or any(hit not in labels for hit in hits) or main_code not in hits:
            bad_knowledge.append(exam_id)
        if row.get("knowledge_review_status") != "reviewed":
            pending_knowledge.append(exam_id)
        if int(row["year"]) != 2025:
            solution_text = row.get("solution_ocr", "").lstrip(" \t\r\n•·．.。*-")
            match = re.match(r"(\d{1,2})\s*[.．、:：]", solution_text)
            if match and int(match.group(1)) != int(row["official_question_no"]):
                mismatched_solution_headers.append(
                    f"{exam_id}:solution_starts_Q{int(match.group(1)):02d}"
                )
    if missing_assets:
        failures.append(f"missing question/solution assets: {missing_assets[:20]} (count={len(missing_assets)})")
    if missing_question_ocr:
        failures.append(f"missing question OCR: {missing_question_ocr[:20]} (count={len(missing_question_ocr)})")
    if missing_solution_ocr:
        failures.append(f"missing solution OCR: {missing_solution_ocr[:20]} (count={len(missing_solution_ocr)})")
    if bad_knowledge:
        failures.append(f"invalid knowledge labels: {bad_knowledge[:20]} (count={len(bad_knowledge)})")
    if pending_knowledge:
        failures.append(f"knowledge not fully reviewed: {pending_knowledge[:20]} (count={len(pending_knowledge)})")
    if mismatched_solution_headers:
        failures.append(
            f"solution/question number mismatches: {mismatched_solution_headers[:20]} (count={len(mismatched_solution_headers)})"
        )

    cards = list((CORPUS / "cards").glob("EXAM408-*.md"))
    if len(cards) != 799:
        failures.append(f"detail cards={len(cards)}, expected 799")
    source_rows = load_rows(CORPUS / "sources" / "source-manifest.csv")
    if len(source_rows) != 34:
        failures.append(f"source manifest rows={len(source_rows)}, expected 34 (17 question + 17 solution)")
    if len({(row["year"], row["role"]) for row in source_rows}) != 34:
        failures.append("source manifest has duplicate/missing year-role pairs")
    for row in source_rows:
        source = Path(row["source_path"])
        if not source.exists():
            failures.append(f"source missing: {source}")
            continue
        if build.sha256_file(source) != row["sha256"]:
            failures.append(f"source SHA-256 drift: {source}")
        if build.pdf_pages(source) != int(row["pages"]):
            failures.append(f"source page-count drift: {source}")

    alias_rows = load_rows(CORPUS / "indexes" / "duplicate-audit.csv")
    if len(alias_rows) != 44:
        failures.append(f"CO03 true-question alias audit rows={len(alias_rows)}, expected 44")
    non_exact_aliases = [row for row in alias_rows if row.get("match_level") != "exact"]
    if non_exact_aliases:
        failures.append(
            f"CO03 aliases not exact: {[row.get('alias_id') for row in non_exact_aliases[:20]]}"
        )

    crosswalk = load_rows(CORPUS / "indexes" / "formal-node-crosswalk.csv")
    formal_nodes = finalize.read_markdown_table(finalize.NODE_TABLE)
    try:
        exact_reviews = finalize.load_formal_exact_reviews(
            finalize.FORMAL_EXACT_REVIEW,
            id_set,
            {row["ID"] for row in formal_nodes},
        )
        finalize.validate_exact_crosswalk(crosswalk)
    except RuntimeError as exc:
        failures.append(str(exc))
        exact_reviews = {}
    exact = [row for row in crosswalk if row["match_level"] == "exact"]
    exact_rows_by_pair = defaultdict(list)
    for row in exact:
        exact_rows_by_pair[(row["exam_question_id"], row["formal_node_id"])].append(row)
    for canonical_id, review in exact_reviews.items():
        pair = (canonical_id, review["formal_node_id"])
        published = exact_rows_by_pair.get(pair, [])
        if len(published) != 1:
            failures.append(
                f"reviewed formal exact mapping not published exactly once: {canonical_id}=>{review['formal_node_id']}"
            )
            continue
        row = published[0]
        if row.get("review_status") != "manual_exact" or review["evidence"] not in row.get("match_basis", ""):
            failures.append(
                f"reviewed formal exact provenance mismatch: {canonical_id}=>{review['formal_node_id']}"
            )
    reviewed_pairs = {
        (canonical_id, review["formal_node_id"])
        for canonical_id, review in exact_reviews.items()
    }
    unexpected_manual = [
        (row.get("exam_question_id", ""), row.get("formal_node_id", ""))
        for row in exact
        if row.get("review_status") == "manual_exact"
        and (row.get("exam_question_id", ""), row.get("formal_node_id", "")) not in reviewed_pairs
    ]
    if unexpected_manual:
        failures.append(f"manual exact crosswalk rows lack durable review source: {unexpected_manual[:10]}")
    link_rows = load_rows(CORPUS / "indexes" / "wrong-question-knowledge-links.csv")
    if any(
        row.get("error_dimension_updated") != "no" or row.get("error_edges_updated") != "no"
        for row in link_rows
    ):
        failures.append("wrong-question knowledge link file claims error-dimension mutation")
    exact_pairs = {(row["exam_question_id"], row["formal_node_id"]) for row in exact if row.get("formal_node_id")}
    link_pairs = {(row["exam_question_id"], row["formal_node_id"]) for row in link_rows}
    if exact_pairs != link_pairs:
        failures.append(
            f"knowledge link/crosswalk mismatch: crosswalk_only={list(exact_pairs-link_pairs)[:10]}, link_only={list(link_pairs-exact_pairs)[:10]}"
        )

    # A formal node mapping to multiple canonical official questions is always a conflict.
    by_node: dict[str, set[str]] = defaultdict(set)
    for exam_id, node_id in exact_pairs:
        by_node[node_id].add(exam_id)
    conflicts = {node: sorted(exams) for node, exams in by_node.items() if len(exams) > 1}
    if conflicts:
        failures.append(f"formal node maps to multiple canonical questions: {dict(list(conflicts.items())[:10])}")
    cluster_rows = load_rows(CORPUS / "indexes" / "formal-duplicate-clusters.csv")
    expected_clusters = {
        exam_id: sorted(nodes)
        for exam_id, nodes in (
            (exam_id, [node_id for candidate, node_id in exact_pairs if candidate == exam_id])
            for exam_id in {candidate for candidate, _node_id in exact_pairs}
        )
        if len(nodes) > 1
    }
    actual_clusters = {
        row["exam_question_id"]: sorted(
            part for part in re.split(r"[；;]", row["formal_node_ids"]) if part
        )
        for row in cluster_rows
    }
    if actual_clusters != expected_clusters:
        failures.append(
            f"formal duplicate cluster report mismatch: expected={expected_clusters}, actual={actual_clusters}"
        )
    if cluster_rows:
        warnings.append(
            "existing formal duplicate clusters retained without auto-merge: "
            + "; ".join(
                f"{row['exam_question_id']}=>{row['formal_node_ids']}" for row in cluster_rows
            )
        )

    report_lines = [
        "# 408 统考真题语料完整性审计",
        "",
        f"- 结果：{'PASS' if not failures else 'FAIL'}",
        f"- canonical 题目：{len(rows)} / 799",
        f"- 年份覆盖：{len(year_counts)} / 17",
        f"- 详情卡：{len(cards)} / 799",
        f"- 题面/解析资产完整：{len(rows) - len(set(item.split(':',1)[0] for item in missing_assets))} / 799",
        f"- 来源角色：{len(source_rows)} / 34",
        f"- 旧 CO03 真题 alias exact：{sum(row.get('match_level') == 'exact' for row in alias_rows)} / 44",
        f"- 正式错题 exact 链接：{len(exact_pairs)}",
        f"- 既有正式重复冲突簇：{len(cluster_rows)}（只记录，不自动合并）",
        f"- 知识点待视觉复核：{len(pending_knowledge)}",
        f"- 错因维度写入：0",
        f"- 错因关系边写入：0",
        "",
        "## 失败项",
        "",
    ]
    report_lines.extend(f"- {item}" for item in failures)
    if not failures:
        report_lines.append("- 无")
    report_lines.extend(["", "## 警告", ""])
    report_lines.extend(f"- {item}" for item in warnings)
    if not warnings:
        report_lines.append("- 无")
    report_lines.extend(
        [
            "",
            "## 边界确认",
            "",
            "- 完整题干、答案、解析和图片只在 `kaoyan-408-details/corpus/408统考真题/`。",
            "- 主仓库未批量创建正式错题节点。",
            "- 本批次只发布真题知识点与既有错题的知识点 crosswalk；未写错因、错因标签或错因边。",
            "",
        ]
    )
    report = CORPUS / "indexes" / "audit-report.md"
    report.write_text("\n".join(report_lines), encoding="utf-8")
    print("PASS" if not failures else "FAIL")
    for failure in failures:
        print(f"FAIL: {failure}")
    for warning in warnings:
        print(f"WARN: {warning}")
    return 0 if not failures else 1


if __name__ == "__main__":
    raise SystemExit(main())
