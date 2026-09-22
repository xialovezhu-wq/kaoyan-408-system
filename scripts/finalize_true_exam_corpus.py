#!/usr/bin/env python3
"""Apply reviewed knowledge labels and rebuild true-exam crosswalk indexes.

This script never edits the formal wrong-question tables.  It links canonical
true-exam questions to existing formal nodes and publishes a knowledge-only
crosswalk; error causes and error-cause edges remain untouched.
"""

from __future__ import annotations

import argparse
import concurrent.futures
import csv
import hashlib
import json
import re
import subprocess
import unicodedata
from collections import defaultdict
from dataclasses import asdict
from datetime import date
from pathlib import Path

import build_true_exam_corpus as build


ROOT = build.ROOT
DETAILS = build.DETAILS
CORPUS = build.CORPUS
INDEX = CORPUS / "indexes" / "question-index.csv"
REVIEW_DIR = CORPUS / "_review"
FORMAL_EXACT_REVIEW = REVIEW_DIR / "formal-crosswalk-exact-overrides.csv"
OCR_CACHE = CORPUS / "_derived" / "existing-question-ocr"
NODE_TABLE = ROOT / "节点总表.md"
CO03_INDEX = DETAILS / "corpus" / "CO03-存储系统" / "indexes" / "question-index.csv"
FORMAL_EXACT_REVIEW_FIELDS = ("canonical_id", "formal_node_id", "status", "evidence")


def read_markdown_table(path: Path) -> list[dict[str, str]]:
    rows: list[dict[str, str]] = []
    header: list[str] | None = None
    for raw in path.read_text(encoding="utf-8").splitlines():
        if not raw.startswith("|"):
            continue
        cells = [cell.strip() for cell in raw.strip().strip("|").split("|")]
        if header is None:
            if "ID" in cells and "主知识点" in cells:
                header = cells
            continue
        if all(re.fullmatch(r":?-+:?", cell) for cell in cells):
            continue
        if len(cells) != len(header):
            continue
        rows.append(dict(zip(header, cells)))
    return rows


def read_index() -> list[build.ExtractedQuestion]:
    records: list[build.ExtractedQuestion] = []
    with INDEX.open(encoding="utf-8-sig", newline="") as handle:
        for row in csv.DictReader(handle):
            for key in ("question_pages", "solution_pages", "question_assets", "solution_assets"):
                row[key] = json.loads(row[key])
            row["year"] = int(row["year"])
            row["official_question_no"] = int(row["official_question_no"])
            records.append(build.ExtractedQuestion(**row))
    return records


def taxonomy() -> dict[str, str]:
    result: dict[str, str] = {}
    pattern = re.compile(r"^-\s+((?:DS|CO|OS|CN)\d{2}-\d{2})\s+(.+?)\s*$")
    for raw in (ROOT / "知识点标签表.md").read_text(encoding="utf-8").splitlines():
        match = pattern.match(raw)
        if match:
            result[match.group(1)] = match.group(2).strip()
    if len(result) < 100:
        raise RuntimeError(f"taxonomy unexpectedly small: {len(result)}")
    return result


def code_from_label(value: str) -> str | None:
    match = re.match(r"^((?:DS|CO|OS|CN)\d{2}-\d{2})(?:\s|$)", value.strip())
    return match.group(1) if match else None


def split_labels(value: str) -> list[str]:
    return [part.strip() for part in re.split(r"[；;]", value) if part.strip()]


def canonical_label(code: str, labels: dict[str, str]) -> str:
    return f"{code} {labels[code]}"


def load_reviews(labels: dict[str, str]) -> dict[str, dict[str, str]]:
    paths = sorted(REVIEW_DIR.glob("knowledge-*.csv"))
    if not paths:
        raise RuntimeError(f"no knowledge review CSVs under {REVIEW_DIR}")
    reviews: dict[str, dict[str, str]] = {}
    for path in paths:
        with path.open(encoding="utf-8-sig", newline="") as handle:
            for row in csv.DictReader(handle):
                exam_id = row["exam_question_id"].strip()
                if exam_id in reviews:
                    raise RuntimeError(f"duplicate reviewed ID: {exam_id}")
                main_code = code_from_label(row["main_knowledge"])
                if not main_code or main_code not in labels:
                    raise RuntimeError(f"invalid main knowledge in {path}: {exam_id} {row['main_knowledge']}")
                hits: list[str] = []
                for label in split_labels(row["hit_knowledge"]):
                    code = code_from_label(label)
                    if not code or code not in labels:
                        raise RuntimeError(f"invalid hit knowledge in {path}: {exam_id} {label}")
                    if code not in hits:
                        hits.append(code)
                if main_code not in hits:
                    hits.insert(0, main_code)
                row["main_knowledge"] = canonical_label(main_code, labels)
                row["hit_knowledge"] = "；".join(canonical_label(code, labels) for code in hits[:4])
                reviews[exam_id] = row
    return reviews


def load_formal_exact_reviews(
    path: Path,
    canonical_ids: set[str],
    formal_node_ids: set[str],
) -> dict[str, dict[str, str]]:
    """Load reviewed exact mappings as durable source truth.

    The generated crosswalk is only a projection.  This loader therefore fails
    closed on malformed or conflicting review rows before finalize publishes
    any cards or indexes.
    """
    if not path.exists():
        raise RuntimeError(f"missing formal exact review CSV: {path}")
    reviews: dict[str, dict[str, str]] = {}
    canonical_by_node: dict[str, str] = {}
    with path.open(encoding="utf-8-sig", newline="") as handle:
        reader = csv.DictReader(handle)
        missing_fields = [
            field for field in FORMAL_EXACT_REVIEW_FIELDS
            if field not in (reader.fieldnames or [])
        ]
        if missing_fields:
            raise RuntimeError(
                f"formal exact review CSV missing fields {missing_fields}: {path}"
            )
        for line_number, raw_row in enumerate(reader, 2):
            row = {field: (raw_row.get(field) or "").strip() for field in FORMAL_EXACT_REVIEW_FIELDS}
            canonical_id = row["canonical_id"]
            node_id = row["formal_node_id"]
            if not canonical_id:
                raise RuntimeError(f"empty canonical_id in {path}:{line_number}")
            if canonical_id in reviews:
                raise RuntimeError(f"duplicate reviewed canonical ID: {canonical_id}")
            if canonical_id not in canonical_ids:
                raise RuntimeError(f"reviewed canonical ID does not exist: {canonical_id}")
            if not node_id:
                raise RuntimeError(f"empty formal_node_id in {path}:{line_number}")
            if node_id not in formal_node_ids:
                raise RuntimeError(f"reviewed formal node does not exist: {node_id}")
            if row["status"] != "exact":
                raise RuntimeError(
                    f"unsupported formal review status for {canonical_id}: {row['status'] or '<empty>'}"
                )
            if not row["evidence"]:
                raise RuntimeError(f"missing formal exact evidence for {canonical_id}")
            previous_canonical = canonical_by_node.get(node_id)
            if previous_canonical and previous_canonical != canonical_id:
                raise RuntimeError(
                    "formal node maps to multiple reviewed canonical questions: "
                    f"{node_id}=>{previous_canonical},{canonical_id}"
                )
            canonical_by_node[node_id] = canonical_id
            reviews[canonical_id] = row
    return reviews


def validate_exact_crosswalk(rows: list[dict[str, str]]) -> None:
    exact_pairs: set[tuple[str, str]] = set()
    canonical_by_node: dict[str, str] = {}
    for row in rows:
        if row.get("match_level") != "exact" or not row.get("formal_node_id"):
            continue
        pair = (row["exam_question_id"], row["formal_node_id"])
        if pair in exact_pairs:
            raise RuntimeError(f"duplicate exact crosswalk pair: {pair[0]}=>{pair[1]}")
        exact_pairs.add(pair)
        previous_canonical = canonical_by_node.get(pair[1])
        if previous_canonical and previous_canonical != pair[0]:
            raise RuntimeError(
                "formal node maps to multiple canonical questions: "
                f"{pair[1]}=>{previous_canonical},{pair[0]}"
            )
        canonical_by_node[pair[1]] = pair[0]


def apply_formal_exact_reviews(
    exact_by_exam: dict[str, list[str]],
    rows: list[dict[str, str]],
    reviews: dict[str, dict[str, str]],
) -> tuple[dict[str, list[str]], list[dict[str, str]]]:
    """Project durable exact reviews onto the generated machine crosswalk."""
    validate_exact_crosswalk(rows)
    existing_exact = {
        exam_id: set(node_ids) for exam_id, node_ids in exact_by_exam.items()
    }
    exact_canonical_by_node = {
        row["formal_node_id"]: row["exam_question_id"]
        for row in rows
        if row.get("match_level") == "exact" and row.get("formal_node_id")
    }

    # Preflight the complete review set before mutating even the in-memory projection.
    for canonical_id, review in reviews.items():
        node_id = review["formal_node_id"]
        conflicting_nodes = existing_exact.get(canonical_id, set()) - {node_id}
        if conflicting_nodes:
            raise RuntimeError(
                "manual exact review conflicts with existing exact mapping: "
                f"{canonical_id}=>{node_id}, existing={sorted(conflicting_nodes)}"
            )
        conflicting_canonical = exact_canonical_by_node.get(node_id)
        if conflicting_canonical and conflicting_canonical != canonical_id:
            raise RuntimeError(
                "manual exact review would map one formal node to multiple canonical questions: "
                f"{node_id}=>{conflicting_canonical},{canonical_id}"
            )

    projected = [dict(row) for row in rows]
    projected_exact = {exam_id: sorted(nodes) for exam_id, nodes in existing_exact.items()}
    for canonical_id, review in sorted(reviews.items()):
        node_id = review["formal_node_id"]
        matching = [
            row for row in projected
            if row.get("exam_question_id") == canonical_id
            and row.get("formal_node_id") == node_id
        ]
        exact_matching = [row for row in matching if row.get("match_level") == "exact"]
        if len(exact_matching) > 1:
            raise RuntimeError(f"duplicate exact crosswalk pair: {canonical_id}=>{node_id}")
        target = exact_matching[0] if exact_matching else (matching[0] if matching else None)
        manual_basis = f"manual exact review:{review['evidence']}"
        if target is None:
            target = {
                "exam_question_id": canonical_id,
                "alias_id": "",
                "formal_node_id": node_id,
                "jaccard": "",
                "containment": "",
            }
            projected.append(target)
        target.update(
            {
                "match_level": "exact",
                "match_basis": manual_basis,
                "review_status": "manual_exact",
            }
        )
        projected_exact[canonical_id] = sorted(
            set(projected_exact.get(canonical_id, [])) | {node_id}
        )

    validate_exact_crosswalk(projected)
    return projected_exact, projected


def normalize(text: str) -> str:
    text = unicodedata.normalize("NFKC", text).lower()
    text = re.sub(r"灰灰考研|皮皮灰|王道书|第\s*\d+\s*页|共\s*\d+\s*页", "", text)
    text = re.sub(r"^(?:一、)?单项选择题.*?$", "", text, flags=re.MULTILINE)
    text = re.sub(r"^[\s•·．.。*-]*(?:第\s*)?[0-4]?\d\s*[.．、:：]", "", text)
    return "".join(ch for ch in text if ch.isalnum() or "\u4e00" <= ch <= "\u9fff")


def grams(text: str, size: int = 3) -> set[str]:
    text = normalize(text)
    if len(text) < size:
        return {text} if text else set()
    return {text[index:index + size] for index in range(len(text) - size + 1)}


def similarity(left: set[str], right: set[str]) -> tuple[float, float]:
    if not left or not right:
        return 0.0, 0.0
    overlap = len(left & right)
    union = len(left | right)
    return overlap / union, overlap / min(len(left), len(right))


def ocr_asset(binary: Path, image: Path, output: Path) -> str:
    if output.exists() and output.stat().st_size > 0:
        return output.read_text(encoding="utf-8")
    proc = subprocess.run(
        [str(binary), str(image), "zh-Hans"],
        check=True,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    lines = []
    for raw in proc.stdout.splitlines():
        parts = raw.split("\t", 4)
        if len(parts) == 5:
            lines.append(parts[4])
    text = "\n".join(lines)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(text, encoding="utf-8")
    return text


def existing_question_assets() -> list[tuple[str, Path, Path]]:
    work: list[tuple[str, Path, Path]] = []
    pattern = re.compile(r"^question-\d+\.png$", re.IGNORECASE)
    for node_dir in sorted((DETAILS / "assets").iterdir()):
        if not node_dir.is_dir() or node_dir.name.startswith("_"):
            continue
        for image in sorted(node_dir.iterdir()):
            if pattern.match(image.name):
                output = OCR_CACHE / node_dir.name / f"{image.stem}.txt"
                work.append((node_dir.name, image, output))
    return work


def load_existing_ocr(jobs: int) -> dict[str, list[tuple[str, set[str]]]]:
    binary = build.compile_ocr()
    work = existing_question_assets()
    by_node: dict[str, list[tuple[str, set[str]]]] = defaultdict(list)
    with concurrent.futures.ThreadPoolExecutor(max_workers=max(1, jobs)) as pool:
        future_map = {
            pool.submit(ocr_asset, binary, image, output): (node_id, image)
            for node_id, image, output in work
        }
        for future in concurrent.futures.as_completed(future_map):
            node_id, image = future_map[future]
            text = future.result()
            by_node[node_id].append((str(image), grams(text)))
    return by_node


def subject_code_from_node(row: dict[str, str]) -> str:
    node_id = row["ID"]
    return node_id.split("_", 1)[0] if "_" in node_id else ""


def build_alias_matches(records: list[build.ExtractedQuestion]) -> tuple[dict[str, list[str]], list[dict[str, str]]]:
    by_year = defaultdict(list)
    for record in records:
        if record.subject == "CO":
            by_year[record.year].append(record)
    aliases: dict[str, list[str]] = defaultdict(list)
    audit: list[dict[str, str]] = []
    binary = build.compile_ocr()
    with CO03_INDEX.open(encoding="utf-8-sig", newline="") as handle:
        for row in csv.DictReader(handle):
            if row.get("is_true_question", "").lower() != "yes":
                continue
            try:
                year = int(row["year"])
            except (TypeError, ValueError):
                continue
            source_text = row.get("question_ocr_summary_unchecked", "")
            image_value = row.get("question_image", "")
            image_path = DETAILS / image_value if image_value else None
            if image_path and image_path.exists():
                source_text = ocr_asset(
                    binary,
                    image_path,
                    CORPUS / "_derived" / "co03-alias-ocr" / f"{row['corpus_id']}.txt",
                )
            source_grams = grams(source_text)
            best: tuple[float, float, build.ExtractedQuestion] | None = None
            for record in by_year.get(year, []):
                jac, contain = similarity(source_grams, grams(record.question_ocr))
                if best is None or (jac, contain) > (best[0], best[1]):
                    best = (jac, contain, record)
            if not best:
                continue
            jac, contain, record = best
            level = "exact" if jac >= 0.45 or contain >= 0.72 else ("probable_needs_review" if jac >= 0.25 or contain >= 0.50 else "no_match")
            if level == "exact":
                aliases[record.exam_question_id].append(row["corpus_id"])
            audit.append(
                {
                    "exam_question_id": record.exam_question_id,
                    "alias_id": row["corpus_id"],
                    "match_level": level,
                    "match_basis": "CO03 OCR normalized trigram",
                    "jaccard": f"{jac:.4f}",
                    "containment": f"{contain:.4f}",
                    "review_status": "machine_exact" if level == "exact" else "needs_review",
                }
            )
    return aliases, audit


def build_formal_crosswalk(
    records: list[build.ExtractedQuestion],
    nodes: list[dict[str, str]],
    existing_ocr: dict[str, list[tuple[str, set[str]]]],
    aliases: dict[str, list[str]],
) -> tuple[dict[str, list[str]], list[dict[str, str]]]:
    record_grams = {record.exam_question_id: grams(record.question_ocr) for record in records}
    exact_by_exam: dict[str, list[str]] = defaultdict(list)
    rows: list[dict[str, str]] = []
    node_by_id = {row["ID"]: row for row in nodes}

    for record in records:
        left = record_grams[record.exam_question_id]
        scored: list[tuple[float, float, str, str]] = []
        for node_id, assets in existing_ocr.items():
            if node_id not in node_by_id:
                continue
            if subject_code_from_node(node_by_id[node_id]) != record.subject:
                continue
            for asset_path, right in assets:
                jac, contain = similarity(left, right)
                scored.append((jac, contain, node_id, asset_path))
        scored.sort(reverse=True)
        emitted: set[str] = set()
        for jac, contain, node_id, asset_path in scored[:8]:
            if node_id in emitted:
                continue
            if jac >= 0.50 or (jac >= 0.32 and contain >= 0.76):
                level = "exact"
            elif jac >= 0.28 or (jac >= 0.18 and contain >= 0.55):
                level = "probable_needs_review"
            else:
                continue
            emitted.add(node_id)
            if level == "exact":
                exact_by_exam[record.exam_question_id].append(node_id)
            rows.append(
                {
                    "exam_question_id": record.exam_question_id,
                    "alias_id": "",
                    "formal_node_id": node_id,
                    "match_level": level,
                    "match_basis": f"question image OCR:{asset_path}",
                    "jaccard": f"{jac:.4f}",
                    "containment": f"{contain:.4f}",
                    "review_status": "machine_exact" if level == "exact" else "needs_review",
                }
            )

    alias_to_exam = {
        alias: exam_id for exam_id, values in aliases.items() for alias in values
    }
    for node in nodes:
        alias = node.get("来源ID", "")
        exam_id = alias_to_exam.get(alias)
        if not exam_id:
            continue
        node_id = node["ID"]
        if node_id not in exact_by_exam[exam_id]:
            exact_by_exam[exam_id].append(node_id)
            rows.append(
                {
                    "exam_question_id": exam_id,
                    "alias_id": alias,
                    "formal_node_id": node_id,
                    "match_level": "exact",
                    "match_basis": "formal source_id equals exact CO03 corpus alias",
                    "jaccard": "",
                    "containment": "",
                    "review_status": "source_exact",
                }
            )
    for exam_id in exact_by_exam:
        exact_by_exam[exam_id] = sorted(set(exact_by_exam[exam_id]))
    return exact_by_exam, rows


def write_csv(path: Path, rows: list[dict[str, str]], fields: list[str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8-sig") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)


def write_navigation(records: list[build.ExtractedQuestion], crosswalk_rows: list[dict[str, str]]) -> None:
    by_year: dict[int, list[build.ExtractedQuestion]] = defaultdict(list)
    by_code: dict[str, list[build.ExtractedQuestion]] = defaultdict(list)
    for record in records:
        by_year[record.year].append(record)
        for label in split_labels(record.hit_knowledge):
            code = code_from_label(label)
            if code:
                by_code[code].append(record)

    year_lines = ["# 408 统考真题按年份索引", ""]
    for year in sorted(by_year):
        year_lines.extend([f"## {year}", ""])
        for record in by_year[year]:
            year_lines.append(
                f"- [[corpus/408统考真题/cards/{record.exam_question_id}|{record.exam_question_id}]] · {record.subject_name} · {record.main_knowledge}"
            )
        year_lines.append("")
    (CORPUS / "indexes" / "by-year.md").write_text("\n".join(year_lines), encoding="utf-8")

    knowledge_lines = ["# 408 统考真题按知识点索引", "", "本索引只组织真题语料，不创建错因或错因关系边。", ""]
    for code in sorted(by_code):
        items = by_code[code]
        label = next(
            part for part in split_labels(items[0].hit_knowledge) if code_from_label(part) == code
        )
        knowledge_lines.extend([f"## {label}（{len(items)}）", ""])
        for record in items:
            knowledge_lines.append(
                f"- [[corpus/408统考真题/cards/{record.exam_question_id}|{record.exam_question_id}]] · {record.year} · Q{record.official_question_no:02d}"
            )
        knowledge_lines.append("")
    (CORPUS / "indexes" / "by-knowledge.md").write_text("\n".join(knowledge_lines), encoding="utf-8")

    exact = [row for row in crosswalk_rows if row["match_level"] == "exact"]
    linked_questions = len({row["exam_question_id"] for row in exact})
    index_text = f"""# 408 统考真题语料索引

## 覆盖

- canonical 题目：{len(records)} / 799
- 年份：2009-2025（每年 47 题）
- 题面切片：{sum(bool(record.question_assets) for record in records)} / 799
- 答案解析切片：{sum(bool(record.solution_assets) for record in records)} / 799
- 知识点已复核：{sum(record.knowledge_review_status == 'reviewed' for record in records)}
- 知识点需视觉复核：{sum(record.knowledge_review_status != 'reviewed' for record in records)}
- 与正式错题 exact 链接：{linked_questions} 道 canonical 真题 / {len({row['formal_node_id'] for row in exact})} 个正式节点

## 入口

- [[corpus/408统考真题/indexes/by-year|按年份]]
- [[corpus/408统考真题/indexes/by-knowledge|按知识点]]
- `question-index.csv`：机器总表
- `formal-node-crosswalk.csv`：真题与既有正式错题映射
- `formal-duplicate-clusters.csv`：同一道 canonical 真题命中多个既有正式节点的显式冲突簇
- `wrong-question-knowledge-links.csv`：本轮知识点维度链接；明确不写错因
- `duplicate-audit.csv`：旧 CO03 语料别名与重复审计
- `audit-report.md`：完整性门禁

## 边界

完整题面、答案和解析只在独立 details 语料层。真题语料不等于正式错题；本批次没有修改错因、错因标签或关系边。
"""
    (CORPUS / "indexes" / "index.md").write_text(index_text, encoding="utf-8")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--jobs", type=int, default=4)
    args = parser.parse_args()

    labels = taxonomy()
    records = read_index()
    reviews = load_reviews(labels)
    expected_ids = {record.exam_question_id for record in records}
    if set(reviews) != expected_ids:
        missing = sorted(expected_ids - set(reviews))[:20]
        extra = sorted(set(reviews) - expected_ids)[:20]
        raise RuntimeError(f"review coverage mismatch: missing={missing}, extra={extra}")
    for record in records:
        review = reviews[record.exam_question_id]
        record.main_knowledge = review["main_knowledge"]
        record.hit_knowledge = review["hit_knowledge"]
        record.knowledge_review_status = review["knowledge_review_status"].strip() or "reviewed"
        record.subject = code_from_label(record.main_knowledge)[:2]
        record.subject_name = build.SUBJECT_NAMES[record.subject]

    nodes = read_markdown_table(NODE_TABLE)
    formal_exact_reviews = load_formal_exact_reviews(
        FORMAL_EXACT_REVIEW,
        expected_ids,
        {row["ID"] for row in nodes},
    )
    aliases, alias_audit = build_alias_matches(records)
    existing_ocr = load_existing_ocr(args.jobs)
    exact_by_exam, crosswalk_rows = build_formal_crosswalk(records, nodes, existing_ocr, aliases)
    exact_by_exam, crosswalk_rows = apply_formal_exact_reviews(
        exact_by_exam,
        crosswalk_rows,
        formal_exact_reviews,
    )

    for record in records:
        record.legacy_corpus_ids = "；".join(sorted(set(aliases.get(record.exam_question_id, []))))
        record.formal_node_ids = "；".join(exact_by_exam.get(record.exam_question_id, []))
        if record.formal_node_ids:
            record.canonical_status = "linked_existing_wrong_question"
            record.duplicate_review_status = "exact_crosswalk"
        elif record.legacy_corpus_ids:
            record.canonical_status = "canonical_with_legacy_alias"
            record.duplicate_review_status = "exact_alias"
        else:
            record.canonical_status = "canonical_new"
            record.duplicate_review_status = "no_exact_existing_match"

    build.write_cards_and_index(records)
    build.write_readme(records)

    crosswalk_fields = [
        "exam_question_id", "alias_id", "formal_node_id", "match_level",
        "match_basis", "jaccard", "containment", "review_status",
    ]
    write_csv(CORPUS / "indexes" / "formal-node-crosswalk.csv", crosswalk_rows, crosswalk_fields)
    duplicate_clusters = [
        {
            "exam_question_id": exam_id,
            "formal_node_ids": "；".join(node_ids),
            "node_count": str(len(node_ids)),
            "status": "existing_duplicate_cluster",
            "action": "record_only_no_auto_merge",
        }
        for exam_id, node_ids in sorted(exact_by_exam.items())
        if len(node_ids) > 1
    ]
    write_csv(
        CORPUS / "indexes" / "formal-duplicate-clusters.csv",
        duplicate_clusters,
        ["exam_question_id", "formal_node_ids", "node_count", "status", "action"],
    )
    write_csv(
        CORPUS / "indexes" / "duplicate-audit.csv",
        alias_audit,
        ["exam_question_id", "alias_id", "match_level", "match_basis", "jaccard", "containment", "review_status"],
    )

    node_by_id = {row["ID"]: row for row in nodes}
    record_by_id = {record.exam_question_id: record for record in records}
    knowledge_links: list[dict[str, str]] = []
    for exam_id, node_ids in sorted(exact_by_exam.items()):
        record = record_by_id[exam_id]
        for node_id in node_ids:
            node = node_by_id[node_id]
            knowledge_links.append(
                {
                    "exam_question_id": exam_id,
                    "formal_node_id": node_id,
                    "corpus_main_knowledge": record.main_knowledge,
                    "corpus_hit_knowledge": record.hit_knowledge,
                    "existing_main_knowledge": node.get("主知识点", ""),
                    "existing_hit_knowledge": node.get("命中知识点", ""),
                    "action": "knowledge_only_link",
                    "error_dimension_updated": "no",
                    "error_edges_updated": "no",
                }
            )
    write_csv(
        CORPUS / "indexes" / "wrong-question-knowledge-links.csv",
        knowledge_links,
        [
            "exam_question_id", "formal_node_id", "corpus_main_knowledge",
            "corpus_hit_knowledge", "existing_main_knowledge", "existing_hit_knowledge",
            "action", "error_dimension_updated", "error_edges_updated",
        ],
    )
    write_navigation(records, crosswalk_rows)
    print(
        f"finalized {len(records)} questions; exact formal links={len(knowledge_links)}; "
        f"legacy aliases={sum(bool(v) for v in aliases.values())}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
