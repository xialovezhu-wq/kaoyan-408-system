#!/usr/bin/env python3
"""Build the 2009-2025 408 true-exam corpus in the external details vault.

The formal wrong-question repository intentionally does not store full stems,
answers, analyses, or source images.  This importer writes those materials only
to ``kaoyan-408-details/corpus/408统考真题`` and produces a canonical
``EXAM408-YYYY-QNN`` record for every official question.

The script is deliberately idempotent.  Source identity is recorded by SHA-256;
OCR is cached as bounding-box TSV; cards and CSV indexes are regenerated from
the cached, source-bound extraction records.
"""

from __future__ import annotations

import argparse
import concurrent.futures
import csv
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import unicodedata
from dataclasses import asdict, dataclass
from datetime import date
from pathlib import Path
from typing import Iterable, Sequence

from PIL import Image

import runtime_paths_408 as _paths


ROOT = Path(__file__).resolve().parents[1]
DETAILS = _paths.details_root()
CORPUS = DETAILS / "corpus" / "408统考真题"
ACCEPTANCE_REL = Path("support/syllabus-2027/exam-corpus-acceptance.json")
LEGACY_ACCEPTED_YEARS = tuple(range(2009, 2026))
SUPPORTED_YEARS = set(range(2009, 2027))
SOURCE_ROOT = _paths.source_root() / '408考研资料/09-26计算机408真题及解析版本2'
SOURCE_V1 = _paths.source_root() / '408考研资料/09-26年408真题及解析版本1'
OCR_SWIFT = ROOT / "scripts" / "vision_ocr_408.swift"
TMP_ROOT = ROOT / "tmp" / "pdfs" / "true_exam_ingest"

SUBJECT_NAMES = {
    "DS": "数据结构",
    "CO": "计算机组成原理",
    "OS": "操作系统",
    "CN": "计算机网络",
}


def accepted_coverage(repo: Path = ROOT) -> dict:
    """An added year becomes production coverage only after explicit acceptance.

    Keep the original 2009--2025 contract when the additive acceptance file is
    absent. Merely installing importer code must not promote provisional data.
    """
    path = Path(repo) / ACCEPTANCE_REL
    data = {}
    if not path.is_file():
        years = list(LEGACY_ACCEPTED_YEARS)
    else:
        data = json.loads(path.read_text(encoding="utf-8"))
        years = data.get("accepted_years")
        if (data.get("schema") != "cs408-exam-corpus-acceptance-v1"
                or data.get("status") != "accepted"
                or not isinstance(years, list) or not years
                or any(type(year) is not int for year in years)
                or years != sorted(set(years))
                or not set(LEGACY_ACCEPTED_YEARS) <= set(years)
                or not set(years) <= SUPPORTED_YEARS
                or data.get("questions_per_year") != 47
                or data.get("question_count") != len(years) * 47):
            raise ValueError("invalid_exam_corpus_acceptance_contract")
    return {"accepted_years": years, "questions_per_year": 47,
            "question_count": len(years) * 47,
            "source_authority": data.get("source_authority", "existing_reviewed_corpus"),
            "coverage_note": data.get("coverage_note", ""),
            "expected_ids": {f"EXAM408-{year}-Q{number:02d}"
                             for year in years for number in range(1, 48)}}


def source_pairs() -> dict[int, dict[str, Path]]:
    pairs: dict[int, dict[str, Path]] = {}
    for year in range(2009, 2026):
        subdir = "2009-2016" if year <= 2016 else "2017-2025"
        question = SOURCE_ROOT / subdir / f"{year}年408计算机专业基础真题.pdf"
        solution = SOURCE_ROOT / subdir / f"{year}年408计算机专业基础真题解析.pdf"
        if year == 2021:
            question = (
                SOURCE_V1
                / "408真题（2009-2023）"
                / "2021年计算机408统考真题.pdf"
            )
        if year == 2025:
            solution = SOURCE_ROOT / subdir / "2025年408统考真题及答案-皮皮灰.pdf"
        pairs[year] = {"question": question, "solution": solution}
    # Supplemental accepted sources are registered by the reviewed importer.
    # Their layout need not match the older, separate question/solution PDFs.
    manifest = CORPUS / "sources" / "source-manifest.csv"
    if manifest.is_file():
        with manifest.open(encoding="utf-8-sig", newline="") as handle:
            for row in csv.DictReader(handle):
                year = int(row["year"])
                if year in SUPPORTED_YEARS and row["role"] in {"question", "solution"}:
                    pairs.setdefault(year, {})[row["role"]] = Path(row["source_path"])
    return pairs


@dataclass(frozen=True)
class OCRLine:
    page: int
    x: float
    y: float
    width: float
    height: float
    text: str

    @property
    def top(self) -> float:
        return 1.0 - (self.y + self.height)

    @property
    def bottom(self) -> float:
        return 1.0 - self.y


@dataclass(frozen=True)
class Boundary:
    number: int
    page: int
    line_index: int
    top: float
    text: str


@dataclass
class ExtractedQuestion:
    exam_question_id: str
    year: int
    official_question_no: int
    question_type: str
    subject: str
    subject_name: str
    question_pages: list[int]
    solution_pages: list[int]
    question_assets: list[str]
    solution_assets: list[str]
    question_ocr: str
    solution_ocr: str
    question_ocr_normalized_sha256: str
    question_source_sha256: str
    solution_source_sha256: str
    question_source_path: str
    solution_source_path: str
    extraction_status: str
    main_knowledge: str = "待校准"
    hit_knowledge: str = "待校准"
    knowledge_review_status: str = "pending"
    legacy_corpus_ids: str = ""
    formal_node_ids: str = ""
    canonical_status: str = "canonical_new"
    duplicate_review_status: str = "pending"


def run(cmd: Sequence[str], *, cwd: Path | None = None) -> str:
    proc = subprocess.run(
        list(map(str, cmd)),
        cwd=cwd,
        check=True,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    return proc.stdout


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def pdf_pages(path: Path) -> int:
    text = run(["pdfinfo", path])
    match = re.search(r"^Pages:\s+(\d+)\s*$", text, re.MULTILINE)
    if not match:
        raise RuntimeError(f"cannot determine PDF pages: {path}")
    return int(match.group(1))


def compile_ocr() -> Path:
    TMP_ROOT.mkdir(parents=True, exist_ok=True)
    binary = TMP_ROOT / "vision_ocr_408"
    if binary.exists() and binary.stat().st_mtime >= OCR_SWIFT.stat().st_mtime:
        return binary
    run(["swiftc", OCR_SWIFT, "-o", binary])
    return binary


def render_pdf(path: Path, output_dir: Path, dpi: int) -> list[Path]:
    output_dir.mkdir(parents=True, exist_ok=True)
    for old in output_dir.glob("page-*.png"):
        old.unlink()
    prefix = output_dir / "page"
    run(["pdftoppm", "-r", str(dpi), "-png", path, prefix])
    pages = sorted(output_dir.glob("page-*.png"))
    expected = pdf_pages(path)
    if len(pages) != expected:
        raise RuntimeError(f"rendered {len(pages)} pages, expected {expected}: {path}")
    return pages


def ocr_one(binary: Path, image: Path, tsv_path: Path, force: bool) -> None:
    if tsv_path.exists() and tsv_path.stat().st_size > 0 and not force:
        return
    text = run([binary, image, "zh-Hans"])
    tsv_path.parent.mkdir(parents=True, exist_ok=True)
    tmp = tsv_path.with_suffix(".tmp")
    tmp.write_text(text, encoding="utf-8")
    tmp.replace(tsv_path)


def ocr_pages(
    binary: Path,
    images: Sequence[Path],
    cache_dir: Path,
    jobs: int,
    force: bool,
) -> list[Path]:
    cache_dir.mkdir(parents=True, exist_ok=True)
    outputs = [cache_dir / f"page-{index:03d}.tsv" for index in range(1, len(images) + 1)]
    with concurrent.futures.ThreadPoolExecutor(max_workers=max(1, jobs)) as pool:
        futures = [
            pool.submit(ocr_one, binary, image, output, force)
            for image, output in zip(images, outputs)
        ]
        for future in concurrent.futures.as_completed(futures):
            future.result()
    return outputs


def load_ocr(tsv_paths: Sequence[Path]) -> dict[int, list[OCRLine]]:
    pages: dict[int, list[OCRLine]] = {}
    for page, path in enumerate(tsv_paths, 1):
        lines: list[OCRLine] = []
        for raw in path.read_text(encoding="utf-8").splitlines():
            parts = raw.split("\t", 4)
            if len(parts) != 5:
                continue
            try:
                x, y, width, height = map(float, parts[:4])
            except ValueError:
                continue
            lines.append(OCRLine(page, x, y, width, height, parts[4].strip()))
        pages[page] = lines
    return pages


LEADING_NUMBER_RE = re.compile(
    r"^[\s•·．.。*-]*(?:第\s*)?([0Oo]?[0-9Il一二三四五六七八九十]{1,2})\s*"
    r"[\.．、:：]"
)


def parse_number(token: str) -> int | None:
    token = token.strip().replace("O", "0").replace("o", "0").replace("I", "1").replace("l", "1")
    if token.isdigit():
        value = int(token)
        return value if 1 <= value <= 47 else None
    chinese = {
        "一": 1,
        "二": 2,
        "三": 3,
        "四": 4,
        "五": 5,
        "六": 6,
        "七": 7,
        "八": 8,
        "九": 9,
        "十": 10,
    }
    if token in chinese:
        return chinese[token]
    return None


def header_number(text: str) -> int | None:
    match = LEADING_NUMBER_RE.match(unicodedata.normalize("NFKC", text))
    return parse_number(match.group(1)) if match else None


def flatten_pages(pages: dict[int, list[OCRLine]]) -> list[OCRLine]:
    return [line for page in sorted(pages) for line in pages[page]]


def find_question_boundaries(pages: dict[int, list[OCRLine]]) -> list[Boundary]:
    lines = flatten_pages(pages)
    start_page = 1
    for page, page_lines in pages.items():
        joined = "".join(line.text for line in page_lines)
        if "单项选择题" in joined or "单项选择" in joined:
            start_page = page
            break
    boundaries: list[Boundary] = []
    expected = 1
    for index, line in enumerate(lines):
        if line.page < start_page or line.x > 0.24:
            continue
        number = header_number(line.text)
        if number != expected:
            continue
        boundaries.append(Boundary(number, line.page, index, max(0.0, line.top - 0.006), line.text))
        expected += 1
        if expected == 48:
            break
    return boundaries


def is_solution_header(text: str, number: int) -> bool:
    normalized = unicodedata.normalize("NFKC", text)
    parsed = header_number(normalized)
    if parsed != number:
        return False
    return bool(re.search(r"解析|解答|详解|参考答案|【\s*(?:解析|解答|答案)", normalized))


def find_solution_boundaries(pages: dict[int, list[OCRLine]]) -> list[Boundary]:
    lines = flatten_pages(pages)
    boundaries: list[Boundary] = []
    expected = 1
    for index, line in enumerate(lines):
        if line.x > 0.28:
            continue
        if not boundaries:
            if not is_solution_header(line.text, expected):
                continue
        else:
            parsed = header_number(line.text)
            normalized = unicodedata.normalize("NFKC", line.text).strip()
            compact = re.sub(r"\s+", "", normalized)
            inferred_answer_header = bool(
                re.match(r"^[A-D][.。．]?【?解析】?", compact, re.IGNORECASE)
            )
            previous_compact = ""
            if index > 0 and lines[index - 1].page == line.page:
                previous_compact = re.sub(
                    r"\s+", "", unicodedata.normalize("NFKC", lines[index - 1].text)
                )
            if inferred_answer_header and header_number(previous_compact) == expected - 1:
                # The answer/analysis marker belongs to a split header on the
                # preceding line (e.g. ``08.`` then ``B。【解析】``), not to the
                # next question.
                continue
            next_compact = ""
            if index + 1 < len(lines) and lines[index + 1].page == line.page:
                next_compact = re.sub(
                    r"\s+", "", unicodedata.normalize("NFKC", lines[index + 1].text)
                )
            split_answer_header = bool(
                re.match(r"^[A-D][.。．]?【?解析】?", next_compact, re.IGNORECASE)
            )
            split_big_header = bool(
                expected >= 41
                and parse_number(compact) == expected
                and ("王道书" in next_compact or "标准答案" in next_compact)
            )
            if parsed == expected:
                if (
                    not is_solution_header(line.text, expected)
                    and expected < 41
                    and not (len(compact) <= 6 and split_answer_header)
                ):
                    continue
            elif not inferred_answer_header and not split_big_header:
                continue
        boundaries.append(Boundary(expected, line.page, index, max(0.0, line.top - 0.006), line.text))
        expected += 1
        if expected == 48:
            break
    return boundaries


def boundary_slices(
    pages: dict[int, list[OCRLine]], boundaries: Sequence[Boundary]
) -> dict[int, tuple[str, list[int], Boundary, Boundary | None]]:
    lines = flatten_pages(pages)
    result: dict[int, tuple[str, list[int], Boundary, Boundary | None]] = {}
    for pos, start in enumerate(boundaries):
        end = boundaries[pos + 1] if pos + 1 < len(boundaries) else None
        end_index = end.line_index if end else len(lines)
        text = "\n".join(line.text for line in lines[start.line_index:end_index]).strip()
        end_page = end.page if end else max(pages)
        used_pages = list(range(start.page, end_page + 1))
        if end and end.top < 0.08 and end.page > start.page:
            used_pages = list(range(start.page, end.page))
        result[start.number] = (text, used_pages, start, end)
    return result


def find_2025_solution_slices(
    pages: dict[int, list[OCRLine]],
) -> dict[int, tuple[str, list[int], Boundary, Boundary | None]]:
    """Map the 2025 subject-local answer numbering back to global Q1-Q47."""

    lines = flatten_pages(pages)
    line_positions = {id(line): index for index, line in enumerate(lines)}
    result: dict[int, tuple[str, list[int], Boundary, Boundary | None]] = {}

    mc_sections = [
        # subject, summary first page, summary last page, global question numbers
        ("DS", 29, 31, list(range(1, 12))),
        ("CO", 52, 53, list(range(12, 23))),
        ("OS", 76, 78, list(range(23, 33))),
        ("CN", 96, 97, list(range(33, 41))),
    ]
    token_re = re.compile(r"^(\d{1,2})\s*[.．]\s*[A-D0OoCc]", re.IGNORECASE)
    for _subject, first_page, last_page, global_numbers in mc_sections:
        starts: list[Boundary] = []
        expected_local = 1
        for page in range(first_page, last_page + 1):
            for line in pages.get(page, []):
                if not 0.12 <= line.x <= 0.24:
                    continue
                match = token_re.match(unicodedata.normalize("NFKC", line.text).strip())
                if not match or int(match.group(1)) != expected_local:
                    continue
                global_number = global_numbers[expected_local - 1]
                starts.append(
                    Boundary(
                        global_number,
                        page,
                        line_positions[id(line)],
                        max(0.0, line.top - 0.01),
                        line.text,
                    )
                )
                expected_local += 1
                if expected_local > len(global_numbers):
                    break
        if len(starts) != len(global_numbers):
            raise RuntimeError(
                f"2025 {_subject} summary boundaries {len(starts)}/{len(global_numbers)}"
            )
        for position, start in enumerate(starts):
            if position + 1 < len(starts):
                end = starts[position + 1]
                end_index = end.line_index
                used_pages = list(range(start.page, end.page + 1))
            else:
                page_lines = pages[last_page]
                end_index = line_positions[id(page_lines[-1])] + 1
                end = Boundary(0, last_page, end_index, 0.965, "section-end")
                used_pages = list(range(start.page, last_page + 1))
            text = "\n".join(
                line.text for line in lines[start.line_index:end_index]
            ).strip()
            result[start.number] = (text, used_pages, start, end)

    big_sections = [
        # first page, last page, local->global map
        (32, 51, {12: 41, 13: 42}),
        (54, 75, {12: 43, 13: 44}),
        (79, 95, {11: 45, 12: 46}),
        (98, 108, {9: 47}),
    ]
    for first_page, last_page, mapping in big_sections:
        starts: list[Boundary] = []
        for local_number, global_number in mapping.items():
            found: Boundary | None = None
            for page in range(first_page, last_page + 1):
                for line in pages.get(page, []):
                    if line.x > 0.30 or header_number(line.text) != local_number:
                        continue
                    normalized = unicodedata.normalize("NFKC", line.text)
                    if "分" not in normalized and "【" not in normalized:
                        continue
                    found = Boundary(
                        global_number,
                        page,
                        line_positions[id(line)],
                        max(0.0, line.top - 0.008),
                        line.text,
                    )
                    break
                if found:
                    break
            if not found:
                raise RuntimeError(
                    f"2025 big-question solution boundary missing: local {local_number}, global {global_number}"
                )
            starts.append(found)
        starts.sort(key=lambda item: item.line_index)
        for position, start in enumerate(starts):
            if position + 1 < len(starts):
                end = starts[position + 1]
                end_index = end.line_index
                used_pages = list(range(start.page, end.page + 1))
            else:
                page_lines = pages[last_page]
                end_index = line_positions[id(page_lines[-1])] + 1
                end = Boundary(0, last_page, end_index, 0.965, "section-end")
                used_pages = list(range(start.page, last_page + 1))
            text = "\n".join(
                line.text for line in lines[start.line_index:end_index]
            ).strip()
            result[start.number] = (text, used_pages, start, end)

    if sorted(result) != list(range(1, 48)):
        raise RuntimeError(f"2025 solution mapping incomplete: {sorted(result)}")
    return result


def crop_block(
    rendered_pages: Sequence[Path],
    start: Boundary,
    end: Boundary | None,
    destination: Path,
    prefix: str,
) -> list[str]:
    destination.mkdir(parents=True, exist_ok=True)
    assets: list[str] = []
    last_page = end.page if end else len(rendered_pages)
    for page_no in range(start.page, last_page + 1):
        image_path = rendered_pages[page_no - 1]
        with Image.open(image_path) as image:
            width, height = image.size
            left = int(width * 0.045)
            right = int(width * 0.955)
            if page_no == start.page:
                top = max(0, int(height * start.top) - 8)
            else:
                top = int(height * 0.035)
            if end and page_no == end.page:
                bottom = min(height, int(height * end.top) - 8)
            else:
                bottom = int(height * 0.965)
            if bottom - top < 12:
                continue
            crop = image.crop((left, top, right, bottom)).convert("RGB")
            output = destination / f"{prefix}-{len(assets) + 1:02d}.png"
            crop.save(output, format="PNG", optimize=True)
        assets.append(output.relative_to(DETAILS).as_posix())
    return assets


def normalize_ocr(text: str) -> str:
    text = unicodedata.normalize("NFKC", text).lower()
    text = re.sub(r"^\s*(?:第\s*)?[0-4]?\d\s*[.．、:：]", "", text)
    text = re.sub(r"灰灰考研|皮皮灰|第\s*\d+\s*页|共\s*\d+\s*页", "", text)
    return "".join(ch for ch in text if ch.isalnum() or "\u4e00" <= ch <= "\u9fff")


def sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def expected_subject(question_no: int, year: int) -> str:
    if question_no >= 41:
        if question_no <= 42:
            return "DS"
        if question_no <= 44:
            return "CO"
        if question_no <= 46:
            return "OS"
        return "CN"
    if year == 2025:
        if question_no <= 11:
            return "DS"
        if question_no <= 22:
            return "CO"
    else:
        if question_no <= 10:
            return "DS"
        if question_no <= 22:
            return "CO"
    if question_no <= 32:
        return "OS"
    return "CN"


def save_jsonl(path: Path, records: Iterable[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    with tmp.open("w", encoding="utf-8") as handle:
        for record in records:
            handle.write(json.dumps(record, ensure_ascii=False, sort_keys=True) + "\n")
    tmp.replace(path)


def write_source_manifest(rows: list[dict[str, str]]) -> None:
    path = CORPUS / "sources" / "source-manifest.csv"
    path.parent.mkdir(parents=True, exist_ok=True)
    fields = [
        "year",
        "role",
        "source_path",
        "source_variant",
        "sha256",
        "pages",
        "text_layer_status",
        "ingested_at",
    ]
    with path.open("w", newline="", encoding="utf-8-sig") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields)
        writer.writeheader()
        writer.writerows(rows)


def card_text(record: ExtractedQuestion) -> str:
    lines = [
        "---",
        "type: true_exam_question",
        "system: 408",
        f"exam_question_id: {record.exam_question_id}",
        f"year: {record.year}",
        f"official_question_no: {record.official_question_no}",
        f"subject: {record.subject}",
        f"knowledge_review_status: {record.knowledge_review_status}",
        "tags:",
        "  - question-corpus",
        "  - true-exam",
        "  - kaoyan-408",
        "---",
        "",
        f"# {record.exam_question_id}",
        "",
        f"- 年份：{record.year}",
        f"- 全卷题号：{record.official_question_no}",
        f"- 科目：{record.subject_name}",
        f"- 题型：{record.question_type}",
        f"- 主知识点：{record.main_knowledge}",
        f"- 命中知识点：{record.hit_knowledge}",
        f"- 知识点复核：{record.knowledge_review_status}",
        f"- 正式错题节点：{record.formal_node_ids or '未匹配'}",
        f"- 旧语料别名：{record.legacy_corpus_ids or '无'}",
        f"- 去重状态：{record.canonical_status} / {record.duplicate_review_status}",
        f"- 题卷来源：`{record.question_source_path}`（页 {','.join(map(str, record.question_pages))}）",
        f"- 解析来源：`{record.solution_source_path}`（页 {','.join(map(str, record.solution_pages)) or '待补'}）",
        "",
        "> [!question] 原题",
    ]
    for asset in record.question_assets:
        lines.append(f"> ![[{asset}]]")
    lines.extend(["", "> [!answer]- 答案与解析"])
    if record.solution_assets:
        for asset in record.solution_assets:
            lines.append(f"> ![[{asset}]]")
    else:
        lines.append("> 当前解析切片待补；可按上方解析来源页回到原 PDF。")
    lines.extend(
        [
            "",
            "## OCR 检索辅助",
            "",
            "> [!note]- 题干 OCR（未校准，以题图为准）",
        ]
    )
    for line in record.question_ocr.splitlines():
        lines.append(f"> {line}")
    lines.extend(["", "> [!note]- 解析 OCR（未校准，以解析图为准）"])
    for line in record.solution_ocr.splitlines():
        lines.append(f"> {line}")
    lines.extend(
        [
            "",
            "## 使用说明",
            "",
            "先看原题独立作答，再展开答案与解析。OCR 仅用于检索和知识点分类，不能替代源页校对。",
            "",
        ]
    )
    return "\n".join(lines)


def write_cards_and_index(records: list[ExtractedQuestion]) -> None:
    cards_dir = CORPUS / "cards"
    cards_dir.mkdir(parents=True, exist_ok=True)
    for record in records:
        (cards_dir / f"{record.exam_question_id}.md").write_text(
            card_text(record), encoding="utf-8"
        )

    index_path = CORPUS / "indexes" / "question-index.csv"
    index_path.parent.mkdir(parents=True, exist_ok=True)
    fields = list(asdict(records[0]).keys()) if records else []
    with index_path.open("w", newline="", encoding="utf-8-sig") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields)
        writer.writeheader()
        for record in records:
            row = asdict(record)
            for key in ("question_pages", "solution_pages", "question_assets", "solution_assets"):
                row[key] = json.dumps(row[key], ensure_ascii=False)
            writer.writerow(row)


def current_records() -> list[ExtractedQuestion]:
    path = CORPUS / "indexes" / "question-index.csv"
    if not path.is_file():
        return []
    records = []
    with path.open(encoding="utf-8-sig", newline="") as handle:
        for row in csv.DictReader(handle):
            for key in ("question_pages", "solution_pages", "question_assets", "solution_assets"):
                row[key] = json.loads(row[key])
            for key in ("year", "official_question_no"):
                row[key] = int(row[key])
            records.append(ExtractedQuestion(**row))
    return records


def merge_cached_records(current: list[ExtractedQuestion], incoming: list[ExtractedQuestion],
                         *, replace_existing: bool = False) -> list[ExtractedQuestion]:
    """Never regress accepted image adoption or labels to an old OCR cache.

    Current index rows are authoritative after cropping/label review. A partial
    year import also retains every other year already present in that index.
    """
    merged = {record.exam_question_id: record for record in current}
    if len(merged) != len(current):
        raise ValueError("duplicate_current_exam_identity")
    seen = set()
    for record in incoming:
        key = record.exam_question_id
        if key in seen:
            raise ValueError("duplicate_incoming_exam_identity:" + key)
        seen.add(key)
        previous = merged.get(key)
        if previous and not replace_existing:
            if (previous.question_source_sha256 != record.question_source_sha256
                    or previous.solution_source_sha256 != record.solution_source_sha256):
                raise ValueError("cached_source_changed_requires_explicit_reimport:" + key)
            continue
        merged[key] = record
    return sorted(merged.values(), key=lambda item: (item.year, item.official_question_no))


def merge_source_manifest(incoming: list[dict[str, str]]) -> list[dict[str, str]]:
    path = CORPUS / "sources" / "source-manifest.csv"
    current = []
    if path.is_file():
        with path.open(encoding="utf-8-sig", newline="") as handle:
            current = list(csv.DictReader(handle))
    merged = {(int(row["year"]), row["role"]): row for row in current}
    for row in incoming:
        key = (int(row["year"]), row["role"])
        if key not in merged or merged[key]["sha256"] != row["sha256"]:
            merged[key] = row
    return [merged[key] for key in sorted(merged)]


def build_year(
    year: int,
    pair: dict[str, Path],
    binary: Path,
    jobs: int,
    dpi: int,
    force_ocr: bool,
    allow_incomplete: bool,
) -> tuple[list[ExtractedQuestion], list[dict[str, str]]]:
    for role, path in pair.items():
        if not path.exists():
            raise FileNotFoundError(path)

    year_tmp = TMP_ROOT / str(year)
    year_derived = CORPUS / "_derived" / "ocr" / str(year)
    rendered: dict[str, list[Path]] = {}
    ocr_pages_by_role: dict[str, dict[int, list[OCRLine]]] = {}
    manifest_rows: list[dict[str, str]] = []
    hashes: dict[str, str] = {}

    for role, path in pair.items():
        hashes[role] = sha256_file(path)
        rendered[role] = render_pdf(path, year_tmp / role, dpi)
        tsv_paths = ocr_pages(
            binary,
            rendered[role],
            year_derived / role,
            jobs,
            force_ocr,
        )
        ocr_pages_by_role[role] = load_ocr(tsv_paths)
        manifest_rows.append(
            {
                "year": str(year),
                "role": role,
                "source_path": str(path),
                "source_variant": "version1-fallback" if year == 2021 and role == "question" else ("piphui-combined" if year == 2025 and role == "solution" else "version2"),
                "sha256": hashes[role],
                "pages": str(len(rendered[role])),
                "text_layer_status": "not_relied_on_vision_ocr",
                "ingested_at": date.today().isoformat(),
            }
        )

    question_boundaries = find_question_boundaries(ocr_pages_by_role["question"])
    solution_boundaries = find_solution_boundaries(ocr_pages_by_role["solution"])
    if len(question_boundaries) != 47:
        message = f"{year}: question boundaries {len(question_boundaries)}/47; found {[b.number for b in question_boundaries]}"
        if not allow_incomplete:
            raise RuntimeError(message)
        print(f"WARN {message}", file=sys.stderr)
    if year != 2025 and len(solution_boundaries) != 47:
        message = f"{year}: solution boundaries {len(solution_boundaries)}/47; found {[b.number for b in solution_boundaries]}"
        if not allow_incomplete:
            raise RuntimeError(message)
        print(f"WARN {message}", file=sys.stderr)

    question_slices = boundary_slices(ocr_pages_by_role["question"], question_boundaries)
    solution_slices = (
        find_2025_solution_slices(ocr_pages_by_role["solution"])
        if year == 2025
        else boundary_slices(ocr_pages_by_role["solution"], solution_boundaries)
    )
    extracted: list[ExtractedQuestion] = []
    extraction_json: list[dict] = []

    for number in range(1, 48):
        if number not in question_slices:
            continue
        question_text, question_pages, q_start, q_end = question_slices[number]
        solution_text = ""
        solution_pages: list[int] = []
        s_start: Boundary | None = None
        s_end: Boundary | None = None
        if number in solution_slices:
            solution_text, solution_pages, s_start, s_end = solution_slices[number]

        exam_id = f"EXAM408-{year}-Q{number:02d}"
        asset_dir = CORPUS / "assets" / str(year) / exam_id
        if asset_dir.exists():
            for old in asset_dir.glob("question-*.png"):
                old.unlink()
            for old in asset_dir.glob("solution-*.png"):
                old.unlink()
        question_assets = crop_block(
            rendered["question"], q_start, q_end, asset_dir, "question"
        )
        solution_assets: list[str] = []
        if s_start:
            solution_assets = crop_block(
                rendered["solution"], s_start, s_end, asset_dir, "solution"
            )

        subject = expected_subject(number, year)
        record = ExtractedQuestion(
            exam_question_id=exam_id,
            year=year,
            official_question_no=number,
            question_type="单项选择题" if number <= 40 else "综合应用题",
            subject=subject,
            subject_name=SUBJECT_NAMES[subject],
            question_pages=question_pages,
            solution_pages=solution_pages,
            question_assets=question_assets,
            solution_assets=solution_assets,
            question_ocr=question_text,
            solution_ocr=solution_text,
            question_ocr_normalized_sha256=sha256_text(normalize_ocr(question_text)),
            question_source_sha256=hashes["question"],
            solution_source_sha256=hashes["solution"],
            question_source_path=str(pair["question"]),
            solution_source_path=str(pair["solution"]),
            extraction_status=(
                "complete"
                if question_assets and (solution_assets or year == 2025)
                else "needs_review"
            ),
        )
        extracted.append(record)
        extraction_json.append(asdict(record))

    save_jsonl(CORPUS / "_derived" / "extracted" / f"{year}.jsonl", extraction_json)
    shutil.rmtree(year_tmp, ignore_errors=True)
    return extracted, manifest_rows


def write_readme(records: list[ExtractedQuestion]) -> None:
    years = sorted({record.year for record in records})
    complete = sum(record.extraction_status == "complete" for record in records)
    expected = accepted_coverage()["question_count"]
    text = f"""# 408 统考真题语料库

本目录保存已登记年份的 408 统考真题完整题面、答案解析和 OCR 检索层；它位于独立详情库，不属于正式错题节点库。可用于出题的年份以已接受覆盖合同及完整性审计为准。

## 当前覆盖

- 年份：{years[0] if years else '无'}-{years[-1] if years else '无'}
- canonical 题目：{len(records)} / {expected}
- 题面与解析切片完整：{complete} / {len(records)}
- 稳定 ID：`EXAM408-YYYY-QNN`
- 来源清单：`sources/source-manifest.csv`
- 机器索引：`indexes/question-index.csv`

## 边界

- 每个 canonical ID 只出现一次；旧 `CO03-Qxxx` 作为 alias 保留，不改名、不删除。
- 完整题干、答案、解析和图片只留在本详情库。
- 主仓库只登记安全入口、知识点和与既有正式错题的 crosswalk。
- 真题进入语料库不等于成为正式错题；只有用户真实做错后才走单题同步正式入库。
- 本批次不写错因、不建错因边。

## 使用

打开 `indexes/index.md` 或 `cards/EXAM408-YYYY-QNN.md`。先独立作答，再展开折叠的答案与解析。
"""
    (CORPUS / "README.md").write_text(text, encoding="utf-8")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--years", default=None, help="e.g. 2009-2012,2015; default: accepted coverage")
    parser.add_argument("--jobs", type=int, default=4)
    parser.add_argument("--dpi", type=int, default=180)
    parser.add_argument("--force-ocr", action="store_true")
    parser.add_argument("--allow-incomplete", action="store_true")
    parser.add_argument("--replace-existing-years", action="store_true",
                        help="explicitly re-extract selected existing years; may replace reviewed crops")
    parser.add_argument(
        "--aggregate-only",
        action="store_true",
        help="rebuild cards/indexes from cached per-year JSONL without rendering or OCR",
    )
    return parser.parse_args()


def parse_years(spec: str) -> list[int]:
    values: set[int] = set()
    for part in spec.split(","):
        part = part.strip()
        if not part:
            continue
        if "-" in part:
            start, end = map(int, part.split("-", 1))
            values.update(range(start, end + 1))
        else:
            values.add(int(part))
    invalid = values - SUPPORTED_YEARS
    if invalid:
        raise ValueError(f"unsupported years: {sorted(invalid)}")
    return sorted(values)


def main() -> int:
    args = parse_args()
    years = parse_years(args.years) if args.years else accepted_coverage()["accepted_years"]
    pairs = source_pairs()
    existing = current_records()
    if not args.aggregate_only and not args.replace_existing_years:
        overlap = set(years) & {record.year for record in existing}
        if overlap:
            raise ValueError("existing_years_require_aggregate_only_or_explicit_replace:" + str(sorted(overlap)))
    CORPUS.mkdir(parents=True, exist_ok=True)
    binary = compile_ocr()
    records: list[ExtractedQuestion] = []
    manifest_rows: list[dict[str, str]] = []
    if args.aggregate_only:
        for year in years:
            cache = CORPUS / "_derived" / "extracted" / f"{year}.jsonl"
            if not cache.exists():
                raise FileNotFoundError(cache)
            for raw in cache.read_text(encoding="utf-8").splitlines():
                if raw.strip():
                    records.append(ExtractedQuestion(**json.loads(raw)))
            for role, path in pairs[year].items():
                manifest_rows.append(
                    {
                        "year": str(year),
                        "role": role,
                        "source_path": str(path),
                        "source_variant": "version1-fallback" if year == 2021 and role == "question" else ("piphui-combined" if year == 2025 and role == "solution" else "version2"),
                        "sha256": sha256_file(path),
                        "pages": str(pdf_pages(path)),
                        "text_layer_status": "not_relied_on_vision_ocr",
                        "ingested_at": date.today().isoformat(),
                    }
                )
        records = merge_cached_records(existing, records)
        write_source_manifest(merge_source_manifest(manifest_rows))
        write_cards_and_index(records)
        write_readme(records)
        print(f"aggregated {len(records)} cached canonical records to {CORPUS}")
        return 0
    for year in years:
        print(f"[{year}] render / OCR / split", flush=True)
        year_records, year_manifest = build_year(
            year,
            pairs[year],
            binary,
            args.jobs,
            args.dpi,
            args.force_ocr,
            args.allow_incomplete,
        )
        records.extend(year_records)
        manifest_rows.extend(year_manifest)
        print(f"[{year}] {len(year_records)}/47 question records", flush=True)
    records = merge_cached_records(existing, records, replace_existing=args.replace_existing_years)
    write_source_manifest(merge_source_manifest(manifest_rows))
    write_cards_and_index(records)
    write_readme(records)
    print(f"wrote {len(records)} canonical records to {CORPUS}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
