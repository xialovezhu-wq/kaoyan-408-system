#!/usr/bin/env python3
"""Build and audit the answer-safe 408 exam-style release.

The canonical 2009-2025 corpus is read-only input.  Generated artifacts contain
only identities, hashes, controlled classifications, counts, and structural
profiles.  They never copy stems, options, answers, analyses, or images into the
main repository.
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import os
import re
import subprocess
import sys
import tempfile
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any, Iterable

from PIL import Image


PROJECT_ROOT = Path(__file__).resolve().parents[1]
DETAILS_ROOT = Path("/Users/your-user/Documents/kaoyan-408-details")
CORPUS_ROOT = DETAILS_ROOT / "corpus" / "408统考真题"
T9_VERSION2_ROOT = Path(
    "/Volumes/T9-Data/01_408/资料库/408考研电子书资料/408考研资料/"
    "09-26计算机408真题及解析版本2"
)
T9_PROVISIONAL_2026_ROOT = T9_VERSION2_ROOT / "2026年计算机408真题及答案（最新）"
T9_PRIORITY_HINT = Path("/Volumes/T9-Data/01_408/资料库/408重点考纲.pdf")

QUESTION_COUNT = 799
DIRECT_POOL_COUNT = 680
STYLE_ONLY_COUNT = 119
SOURCE_SLOT_COUNT = 34
SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
CANONICAL_RE = re.compile(r"^EXAM408-(20(?:0[9]|1\d|2[0-5]))-Q(0[1-9]|[1-3]\d|4[0-7])$")
ANSWER_TOKEN_RE = re.compile(
    r"(?:标准答案|正确答案|参考答案|答案)\s*(?:是|为|选|[:：=])?\s*[（(\[]?\s*([A-D])"
    r"|(?:故|所以|因此|应|可|本题)?\s*选(?:择)?\s*[（(\[]?\s*([A-D])"
    r"|^\s*0?\d{1,2}\s*[.．、]\s*([A-D])(?:\b|[.．、，,\s])",
    re.IGNORECASE | re.MULTILINE,
)

CONFIDENCE_LEVELS = {"high", "medium", "low"}
CONDITION_DENSITIES = {"low", "medium", "high"}
CALCULATION_SCALES = {"none", "local_single_step", "short_multi_step", "extended_multi_stage"}
POLARITIES = {"positive", "negative", "neutral_or_implicit"}
OPTION_PARALLELISM = {"high", "medium", "low", "unobserved"}
REASONING_DEPTHS = {"recall_one_hop", "short_chain", "multi_constraint_chain", "extended_derivation"}

PARAMETER_PATTERNS: tuple[tuple[str, re.Pattern[str], str], ...] = (
    ("bit_width", re.compile(r"\d+(?:\.\d+)?\s*(?:位|bit|bits)\b", re.IGNORECASE), "positive_discrete"),
    ("capacity_or_size", re.compile(r"\d+(?:\.\d+)?\s*(?:B|KB|MB|GB|TB|字节|块|页|扇区)\b", re.IGNORECASE), "positive_discrete_or_scaled"),
    ("time_duration", re.compile(r"\d+(?:\.\d+)?\s*(?:ns|us|ms|s|秒|毫秒|微秒|纳秒)\b", re.IGNORECASE), "nonnegative_continuous"),
    ("rate_or_bandwidth", re.compile(r"\d+(?:\.\d+)?\s*(?:bps|Kbps|Mbps|Gbps|Hz|MHz|GHz)\b", re.IGNORECASE), "positive_continuous"),
    ("probability_or_ratio", re.compile(r"\d+(?:\.\d+)?\s*(?:%|％|比例|概率)"), "closed_unit_interval_or_percent"),
    ("address_literal", re.compile(r"(?:0x[0-9A-F]+|[0-9A-F]+H)\b", re.IGNORECASE), "nonnegative_address_domain"),
    ("bounded_interval", re.compile(r"\d+(?:\.\d+)?\s*(?:~|～|至|到|-)\s*\d+(?:\.\d+)?"), "ordered_bounded_domain"),
)

DISTRACTOR_RULES: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("boundary_condition_swap", ("边界", "上限", "下限", "至少", "至多", "不超过", "范围")),
    ("unit_or_scale_confusion", ("单位", "换算", "字节", "位", "KB", "MB", "ms", "ns", "Mbps")),
    ("direction_or_order_reversal", ("顺序", "逆序", "先", "后", "方向", "入栈", "出栈", "发送", "接收")),
    ("state_transition_omission", ("状态", "转换", "执行后", "缺页", "中断", "阻塞", "唤醒")),
    ("formula_component_omission", ("公式", "相加", "乘", "除", "开销", "首部", "延迟", "传输")),
    ("mechanism_or_term_substitution", ("混淆", "区别", "不同", "概念", "协议", "算法", "结构")),
    ("off_by_one_or_counting", ("一个", "少", "多", "计数", "编号", "下标", "长度")),
    ("causal_direction_inversion", ("导致", "原因", "影响", "作用", "目的")),
)

PROVISIONAL_SOURCE_NAMES = (
    "2026年计算机408统考真题及答案回忆版.pdf",
    "2026年408参考答案【皮皮灰】.pdf",
    "2026年考研真题及答案解析万学海文版.pdf",
)
PROVISIONAL_SOURCE_ROLES = (
    "memory_question_and_answer",
    "reference_answer",
    "third_party_question_answer_analysis",
)

COGNITIVE_RULES: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("calculate_or_derive", ("计算", "求", "数值", "大小", "长度", "次数", "时间", "地址", "位数", "容量", "带宽")),
    ("trace_state_transition", ("执行", "运行", "访问", "序列", "调度", "状态", "过程", "之后", "依次", "变化")),
    ("compare_or_distinguish", ("相比", "区别", "相同", "不同", "优于", "关系", "比较")),
    ("diagnose_cause_or_effect", ("原因", "导致", "影响", "故障", "异常", "目的", "作用")),
    ("choose_operation_or_structure", ("采用", "选择", "应使用", "适合", "操作", "结构", "算法", "协议")),
)


class ReleaseError(RuntimeError):
    """A deterministic release or audit failure."""


def canonical_json(value: Any) -> bytes:
    return (json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n").encode("utf-8")


def pretty_json(value: Any) -> bytes:
    return (json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2) + "\n").encode("utf-8")


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def pdf_pages(path: Path) -> int:
    process = subprocess.run(
        ["pdfinfo", str(path)],
        check=False,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    if process.returncode != 0:
        raise ReleaseError(f"pdfinfo failed for {path}: {process.stderr.strip()}")
    match = re.search(r"^Pages:\s+(\d+)\s*$", process.stdout, re.MULTILINE)
    if not match:
        raise ReleaseError(f"cannot determine PDF page count: {path}")
    return int(match.group(1))


def read_csv(path: Path) -> list[dict[str, str]]:
    if not path.is_file():
        raise ReleaseError(f"missing CSV: {path}")
    with path.open(encoding="utf-8-sig", newline="") as handle:
        return list(csv.DictReader(handle))


def parse_canonical_id(value: str) -> tuple[int, int]:
    match = CANONICAL_RE.fullmatch(value)
    if not match:
        raise ReleaseError(f"invalid canonical question ID: {value}")
    return int(match.group(1)), int(match.group(2))


def knowledge_code(value: str) -> str:
    match = re.match(r"^((?:DS|CO|OS|CN)\d{2}-\d{2})(?:\s|$)", value.strip())
    if not match:
        raise ReleaseError(f"invalid main knowledge label: {value!r}")
    return match.group(1)


def classify_cognitive_action(question_ocr: str) -> str:
    normalized = re.sub(r"\s+", "", question_ocr)
    for label, terms in COGNITIVE_RULES:
        if any(term in normalized for term in terms):
            return label
    return "recognize_or_judge"


def classify_question_form(question_no: int, question_ocr: str) -> str:
    if question_no >= 41:
        return "comprehensive_problem_structure"
    normalized = re.sub(r"\s+", "", question_ocr)
    if any(term in normalized for term in ("设", "若", "某", "给定", "假定", "现有")):
        return "single_choice_scenario"
    if any(term in normalized for term in ("计算", "数值", "大小", "长度", "次数", "时间", "地址", "容量")):
        return "single_choice_quantitative"
    if any(term in normalized for term in ("正确的是", "错误的是", "不正确的是", "说法")):
        return "single_choice_statement_judgment"
    return "single_choice_direct"


def count_bucket(value: int) -> str:
    if value == 0:
        return "0"
    if value == 1:
        return "1"
    if value <= 3:
        return "2-3"
    if value <= 7:
        return "4-7"
    return "8+"


def feature(category: Any, confidence: str, source: list[str], **extra: Any) -> dict[str, Any]:
    if confidence not in CONFIDENCE_LEVELS:
        raise ReleaseError(f"unsupported confidence: {confidence}")
    return {"category": category, "confidence": confidence, "source": source, **extra}


def parse_json_list(raw: str, label: str) -> list[Any]:
    try:
        value = json.loads(raw or "[]")
    except json.JSONDecodeError as exc:
        raise ReleaseError(f"invalid JSON list in {label}") from exc
    if not isinstance(value, list):
        raise ReleaseError(f"{label} is not a JSON list")
    return value


def condition_density_feature(question_ocr: str, question_asset_count: int) -> dict[str, Any]:
    compact = re.sub(r"\s+", "", question_ocr)
    condition_markers = sum(compact.count(term) for term in ("若", "已知", "设", "给定", "假定", "其中", "且", "当", "初始", "采用"))
    numeric_tokens = len(re.findall(r"(?<![A-Za-z])\d+(?:\.\d+)?", compact))
    score = condition_markers + min(numeric_tokens, 6) + max(0, question_asset_count - 1)
    category = "low" if score <= 2 else "medium" if score <= 6 else "high"
    confidence = "high" if question_asset_count and len(compact) >= 40 else "medium"
    return feature(
        category,
        confidence,
        ["question_ocr_lexical_proxy", "question_asset_metadata"],
        condition_marker_count_bucket=count_bucket(condition_markers),
        numeric_token_count_bucket=count_bucket(numeric_tokens),
        question_asset_count_bucket=count_bucket(question_asset_count),
    )


def data_representation_feature(question_ocr: str, question_asset_count: int) -> dict[str, Any]:
    compact = re.sub(r"\s+", "", question_ocr)
    categories: list[str] = []
    rules = (
        ("table_or_matrix", ("表", "矩阵", "行", "列")),
        ("code_or_pseudocode", ("程序", "代码", "int", "for", "while", "return", "->")),
        ("sequence_or_trace", ("序列", "访问串", "依次", "队列", "栈", "调度")),
        ("graph_tree_or_topology", ("图", "树", "结点", "边", "拓扑", "路径")),
        ("address_or_bit_layout", ("地址", "位", "二进制", "十六进制", "页号", "标记")),
        ("formula_or_symbolic", ("公式", "复杂度", "O(", "=")),
        ("timing_or_state_diagram", ("时序", "状态图", "状态转换", "波形", "流水线")),
    )
    for label, terms in rules:
        if any(term in compact for term in terms):
            categories.append(label)
    if question_asset_count > 1 and "table_or_matrix" not in categories:
        categories.append("multi_asset_visual")
    if not categories:
        categories.append("natural_language_scenario")
    return feature(
        sorted(set(categories)),
        "medium" if categories == ["natural_language_scenario"] else "high",
        ["question_ocr_representation_proxy", "question_asset_metadata"],
    )


def legal_parameter_ranges_feature(question_ocr: str) -> dict[str, Any]:
    observations: list[dict[str, str]] = []
    for parameter_type, pattern, inferred_domain in PARAMETER_PATTERNS:
        count = len(pattern.findall(question_ocr))
        if count:
            observations.append(
                {
                    "parameter_type": parameter_type,
                    "observation_count_bucket": count_bucket(count),
                    "inferred_domain": inferred_domain,
                    "confidence": "medium",
                    "source": "question_ocr_typed_range_proxy",
                }
            )
    generic_numeric_count = len(re.findall(r"(?<![A-Za-z])\d+(?:\.\d+)?", question_ocr))
    if generic_numeric_count and not observations:
        observations.append(
            {
                "parameter_type": "generic_numeric_parameter",
                "observation_count_bucket": count_bucket(generic_numeric_count),
                "inferred_domain": "domain_not_established",
                "confidence": "low",
                "source": "question_ocr_numeric_token_proxy",
            }
        )
    return {
        "categories": observations,
        "parameter_type_count_bucket": count_bucket(len(observations)),
        "raw_values_stored": False,
        "confidence": "medium" if observations else "low",
        "source": ["question_ocr_typed_range_proxy"],
    }


def calculation_scale_feature(question_ocr: str, solution_ocr: str) -> dict[str, Any]:
    question_action = classify_cognitive_action(question_ocr)
    operators = len(re.findall(r"[=+×*/÷]|(?<!\w)-(?!\w)", solution_ocr))
    step_markers = len(re.findall(r"(?:首先|然后|因此|所以|可得|进而|最后|步骤|\d+[）)])", solution_ocr))
    if question_action != "calculate_or_derive" and operators <= 1:
        category = "none"
    elif operators <= 2 and step_markers <= 1:
        category = "local_single_step"
    elif operators <= 7 and step_markers <= 4:
        category = "short_multi_step"
    else:
        category = "extended_multi_stage"
    return feature(
        category,
        "medium",
        ["question_ocr_cognitive_proxy", "solution_ocr_operator_and_step_proxy"],
        operator_count_bucket=count_bucket(operators),
        step_marker_count_bucket=count_bucket(step_markers),
    )


def polarity_feature(question_ocr: str) -> dict[str, Any]:
    compact = re.sub(r"\s+", "", question_ocr)
    negative = ("不正确", "错误的是", "不能", "不属于", "不包括", "不可能", "除", "不是")
    positive = ("正确的是", "可以", "能够", "应当", "符合", "最适合")
    if any(term in compact for term in negative):
        return feature("negative", "high", ["question_ocr_polarity_marker"])
    if any(term in compact for term in positive):
        return feature("positive", "high", ["question_ocr_polarity_marker"])
    return feature("neutral_or_implicit", "low", ["question_ocr_no_explicit_polarity_marker"])


def option_parallelism_feature(question_ocr: str) -> dict[str, Any]:
    option_lines = re.findall(r"(?m)^\s*[A-DＡ-Ｄ]\s*[.．、:：]?\s*(.+?)\s*$", question_ocr)
    if len(option_lines) < 3:
        return feature("unobserved", "low", ["question_ocr_option_line_proxy"], option_count_bucket=count_bucket(len(option_lines)))
    lengths = [max(1, len(re.sub(r"\d+(?:\.\d+)?", "N", line))) for line in option_lines]
    length_ratio = max(lengths) / min(lengths)
    prefix_shapes = {
        re.sub(r"[\u4e00-\u9fffA-Za-z0-9]+", "T", line.strip())[:12]
        for line in option_lines
    }
    if length_ratio <= 1.6 and len(prefix_shapes) <= 2:
        category = "high"
    elif length_ratio <= 2.8:
        category = "medium"
    else:
        category = "low"
    return feature(
        category,
        "medium",
        ["question_ocr_option_length_and_shape_proxy"],
        option_count_bucket=count_bucket(len(option_lines)),
        length_spread_bucket="narrow" if length_ratio <= 1.6 else "moderate" if length_ratio <= 2.8 else "wide",
    )


def reasoning_depth_feature(question_no: int, solution_ocr: str, calculation_scale: str) -> dict[str, Any]:
    compact_length = len(re.sub(r"\s+", "", solution_ocr))
    causal_markers = len(re.findall(r"(?:因为|因此|所以|可得|说明|从而|进而|否则|若|当)", solution_ocr))
    if question_no >= 41 or compact_length >= 600 or calculation_scale == "extended_multi_stage":
        category = "extended_derivation"
    elif compact_length >= 250 or causal_markers >= 4 or calculation_scale == "short_multi_step":
        category = "multi_constraint_chain"
    elif compact_length >= 90 or causal_markers >= 1:
        category = "short_chain"
    else:
        category = "recall_one_hop"
    return feature(
        category,
        "medium",
        ["solution_ocr_length_and_reasoning_marker_proxy", "official_question_number"],
        solution_length_bucket="short" if compact_length < 90 else "medium" if compact_length < 250 else "long" if compact_length < 600 else "extended",
        causal_marker_count_bucket=count_bucket(causal_markers),
    )


def distractor_mechanism_feature(question_ocr: str, solution_ocr: str) -> dict[str, Any]:
    combined = question_ocr + "\n" + solution_ocr
    categories = [label for label, terms in DISTRACTOR_RULES if any(term in combined for term in terms)]
    if not categories:
        categories = ["generic_plausible_alternative"]
        confidence = "low"
    else:
        confidence = "medium"
    return feature(
        sorted(set(categories)),
        confidence,
        ["question_and_solution_ocr_mechanism_proxy"],
    )


def deep_style_features(row: dict[str, str], question_no: int, question_asset_count: int) -> dict[str, Any]:
    question_ocr = row.get("question_ocr", "")
    solution_ocr = row.get("solution_ocr", "")
    calculation = calculation_scale_feature(question_ocr, solution_ocr)
    return {
        "condition_density": condition_density_feature(question_ocr, question_asset_count),
        "data_representation": data_representation_feature(question_ocr, question_asset_count),
        "legal_parameter_ranges": legal_parameter_ranges_feature(question_ocr),
        "calculation_scale": calculation,
        "polarity": polarity_feature(question_ocr),
        "option_parallelism_proxy": option_parallelism_feature(question_ocr),
        "reasoning_depth": reasoning_depth_feature(question_no, solution_ocr, calculation["category"]),
        "distractor_mechanism_categories": distractor_mechanism_feature(question_ocr, solution_ocr),
    }


def strict_answer_option(card_text: str, solution_ocr: str) -> tuple[str, str | None]:
    matches: set[str] = set()
    for source in (card_text, solution_ocr):
        for match in ANSWER_TOKEN_RE.finditer(source):
            matches.add(next(group for group in match.groups() if group).upper())
    if len(matches) == 1:
        return "resolved", next(iter(matches))
    if len(matches) > 1:
        return "ambiguous", None
    return "missing", None


def normalize_answer_token(text: str) -> str | None:
    upper = text.upper()
    candidates: set[str] = set()
    if "A" in upper:
        candidates.add("A")
    if "B" in upper:
        candidates.add("B")
    if "C" in upper or "€" in text or "¢" in text:
        candidates.add("C")
    if "D" in upper:
        candidates.add("D")
    return next(iter(candidates)) if len(candidates) == 1 else None


def cluster_axis(values: list[tuple[int, Any]], tolerance: int) -> list[list[tuple[int, Any]]]:
    groups: list[list[tuple[int, Any]]] = []
    for coordinate, value in sorted(values, key=lambda item: item[0]):
        if not groups or coordinate - groups[-1][-1][0] > tolerance:
            groups.append([])
        groups[-1].append((coordinate, value))
    return groups


def parse_answer_table_tsv(
    *,
    year: int,
    image_path: Path,
    tsv_path: Path,
    scratch_dir: Path,
) -> dict[str, Any]:
    rows = read_csv_tsv(tsv_path)
    with Image.open(image_path) as image:
        width, height = image.size
        row_count, column_count = (4, 10) if year == 2023 else (5, 8)
        candidates: list[tuple[int, dict[str, Any]]] = []
        for row in rows:
            answer = normalize_answer_token(row.get("text", ""))
            top = int(row.get("top") or 0)
            if not answer or not 0.15 * height < top < 0.41 * height:
                continue
            candidates.append(
                (
                    top,
                    {
                        "left": int(row.get("left") or 0),
                        "top": top,
                        "answer": answer,
                        "confidence": float(row.get("conf") or -1),
                        "raw_token": row.get("text", ""),
                    },
                )
            )
        y_groups = cluster_axis(candidates, max(20, int(height * 0.008)))
        y_groups = sorted(
            sorted(y_groups, key=lambda group: -len(group))[:row_count],
            key=lambda group: sum(item[0] for item in group) / len(group),
        )
        y_centers = [round(sum(item[0] for item in group) / len(group)) for group in y_groups]
        x_values = [(item[1]["left"], item[1]) for group in y_groups for item in group]
        x_groups = cluster_axis(x_values, max(30, int(width * 0.018)))
        x_groups = sorted(
            sorted(x_groups, key=lambda group: -len(group))[:column_count],
            key=lambda group: sum(item[0] for item in group) / len(group),
        )
        x_centers = [round(sum(item[0] for item in group) / len(group)) for group in x_groups]
        answer_map: dict[str, str] = {}
        cell_evidence: list[dict[str, Any]] = []
        crop_fallback_count = 0
        question_no = 0
        if len(y_centers) == row_count and len(x_centers) == column_count:
            for y_center in y_centers:
                for x_center in x_centers:
                    question_no += 1
                    matches = [
                        value
                        for _top, value in candidates
                        if abs(value["top"] - y_center) < 35 and abs(value["left"] - x_center) < 70
                    ]
                    matches.sort(key=lambda value: -value["confidence"])
                    method = "tsv_spatial_cell"
                    answer = matches[0]["answer"] if matches else None
                    confidence = matches[0]["confidence"] if matches else None
                    raw_token = matches[0]["raw_token"] if matches else None
                    if answer is None:
                        crop = image.crop(
                            (
                                max(0, x_center - 45),
                                max(0, y_center - 45),
                                min(width, x_center + 70),
                                min(height, y_center + 55),
                            )
                        )
                        crop_path = scratch_dir / f"{year}-q{question_no:02d}-answer-cell.png"
                        crop.save(crop_path)
                        process = subprocess.run(
                            [
                                "tesseract",
                                str(crop_path),
                                "stdout",
                                "-l",
                                "eng",
                                "--psm",
                                "10",
                                "-c",
                                "tessedit_char_whitelist=ABCD",
                            ],
                            check=False,
                            text=True,
                            stdout=subprocess.PIPE,
                            stderr=subprocess.PIPE,
                        )
                        answer = normalize_answer_token(process.stdout.strip()) if process.returncode == 0 else None
                        method = "cell_crop_whitelist_fallback"
                        confidence = None
                        raw_token = None
                        crop_fallback_count += int(answer is not None)
                    if answer:
                        answer_map[str(question_no)] = answer
                    cell_evidence.append(
                        {
                            "question_no": question_no,
                            "candidate": answer,
                            "method": method,
                            "ocr_confidence": confidence,
                            "raw_token": raw_token,
                            "cell_center": [x_center, y_center],
                        }
                    )
        bbox = (
            [
                max(0, min(x_centers) - 100),
                max(0, min(y_centers) - 70),
                min(width, max(x_centers) + 100),
                min(height, max(y_centers) + 70),
            ]
            if x_centers and y_centers
            else None
        )
        header_detected = any("答案" in row.get("text", "") for row in rows)
        complete = len(answer_map) == 40 and len(set(answer_map)) == 40 and header_detected
        return {
            "year": year,
            "source_page": 1,
            "layout_parser": "grid_4x10_2023" if year == 2023 else "grid_5x8_year_specific",
            "render_dpi": 350,
            "render_sha256": sha256_file(image_path),
            "render_size": [width, height],
            "source_page_bbox_pixels": bbox,
            "source_page_bbox_normalized": (
                [round(bbox[0] / width, 6), round(bbox[1] / height, 6), round(bbox[2] / width, 6), round(bbox[3] / height, 6)]
                if bbox
                else None
            ),
            "tsv_sha256": sha256_file(tsv_path),
            "header_detected": header_detected,
            "row_count": len(y_centers),
            "column_count": len(x_centers),
            "candidate_count": len(answer_map),
            "crop_fallback_count": crop_fallback_count,
            "structurally_complete": complete,
            "candidate_answers": answer_map,
            "cell_evidence": cell_evidence,
        }


def read_csv_tsv(path: Path) -> list[dict[str, str]]:
    with path.open(encoding="utf-8", newline="") as handle:
        return list(csv.DictReader(handle, delimiter="\t"))


def build_answer_table_evidence(source_rows: list[dict[str, str]]) -> dict[int, dict[str, Any]]:
    solution_rows = {int(row["year"]): row for row in source_rows if row["role"] == "solution"}
    result: dict[int, dict[str, Any]] = {}
    with tempfile.TemporaryDirectory(prefix="exam-answer-table-") as temp_name:
        scratch = Path(temp_name)
        for year in range(2009, 2025):
            source = Path(solution_rows[year]["source_path"])
            prefix = scratch / str(year)
            render = Path(str(prefix) + ".png")
            tsv = Path(str(prefix) + ".tsv")
            subprocess.run(
                ["pdftoppm", "-f", "1", "-l", "1", "-singlefile", "-r", "350", "-png", str(source), str(prefix)],
                check=True,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.PIPE,
            )
            subprocess.run(
                ["tesseract", str(render), str(prefix), "-l", "chi_sim+eng", "--psm", "6", "tsv"],
                check=True,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.PIPE,
            )
            evidence = parse_answer_table_tsv(
                year=year,
                image_path=render,
                tsv_path=tsv,
                scratch_dir=scratch,
            )
            evidence["source_pdf_path"] = str(source)
            evidence["source_pdf_sha256"] = solution_rows[year]["sha256"]
            evidence["visual_review_status"] = "representative_layout_reviewed" if year in {2009, 2012, 2019, 2021, 2024} else "same_layout_parser_family"
            result[year] = evidence
    result[2025] = {
        "year": 2025,
        "source_page": None,
        "layout_parser": "distributed_subject_answer_pages_not_adopted",
        "render_dpi": 350,
        "structurally_complete": False,
        "candidate_count": 0,
        "candidate_answers": {},
        "cell_evidence": [],
        "verification_status": "blocked_no_single_complete_q01_q40_table",
    }
    return result


def resolve_corpus_asset(corpus_root: Path, reference: str) -> Path:
    relative = Path(reference)
    if relative.is_absolute() or ".." in relative.parts:
        raise ReleaseError(f"unsafe question asset reference: {reference}")
    candidates = [corpus_root / relative]
    if len(corpus_root.parents) >= 2:
        candidates.append(corpus_root.parents[1] / relative)
    prefix = Path("corpus") / "408统考真题"
    try:
        stripped = relative.relative_to(prefix)
    except ValueError:
        stripped = None
    if stripped is not None:
        candidates.append(corpus_root / stripped)
    for candidate in candidates:
        if candidate.is_file() and not candidate.is_symlink():
            return candidate
    raise ReleaseError(f"missing question asset: {reference}")


def revision_id_from_asset_reference(reference: str) -> str | None:
    """Return the immutable crop revision encoded in an asset reference.

    Ordinary corpus assets deliberately remain outside this provenance path.  A
    source override is only meaningful for assets copied into an immutable
    ``assets-revisions/<revision_id>/...`` tree.
    """
    parts = Path(reference).parts
    try:
        position = parts.index("assets-revisions")
    except ValueError:
        return None
    if position + 1 >= len(parts) or not parts[position + 1]:
        raise ReleaseError(f"invalid revision asset reference: {reference}")
    return parts[position + 1]


def preprocessed_revision_id_from_asset_reference(reference: str) -> str | None:
    parts = Path(reference).parts
    try:
        position = parts.index("assets-preprocessed")
    except ValueError:
        return None
    if position + 1 >= len(parts) or not parts[position + 1]:
        raise ReleaseError(f"invalid preprocessed asset reference: {reference}")
    return parts[position + 1]


def _sha256_path(path: Path, label: str, canonical_id: str) -> str:
    if not path.is_file() or path.is_symlink():
        raise ReleaseError(f"{label} missing: {canonical_id}")
    return sha256_file(path)


def _receipt_source_path(package: dict[str, Any], raw_path: Any, canonical_id: str) -> Path:
    if not isinstance(raw_path, str) or not raw_path:
        raise ReleaseError(f"preprocessed input path missing: {canonical_id}")
    candidate = Path(raw_path)
    if candidate.is_absolute():
        return candidate
    root = package.get("root")
    if not isinstance(root, str) or not root:
        raise ReleaseError(f"preprocessed package root missing: {canonical_id}")
    resolved = Path(root) / candidate
    if ".." in candidate.parts:
        raise ReleaseError(f"unsafe preprocessed input path: {canonical_id}")
    return resolved


def _preprocessed_manifest_record(
    package: dict[str, Any],
    binding: dict[str, Any],
    canonical_id: str,
) -> tuple[dict[str, Any], dict[str, Any]]:
    root = package.get("root")
    manifest_ref = package.get("manifest_path")
    manifest_sha = package.get("manifest_sha256")
    if not isinstance(root, str) or not isinstance(manifest_ref, str) or not isinstance(manifest_sha, str) or not SHA256_RE.fullmatch(manifest_sha):
        raise ReleaseError(f"preprocessed package manifest locator invalid: {canonical_id}")
    relative = Path(manifest_ref)
    if ".." in relative.parts:
        raise ReleaseError(f"unsafe preprocessed manifest path: {canonical_id}")
    package_root = Path(root).resolve()
    manifest_path = relative.resolve() if relative.is_absolute() else (package_root / relative).resolve()
    try:
        manifest_path.relative_to(package_root)
    except ValueError as exc:
        raise ReleaseError(f"unsafe preprocessed manifest path: {canonical_id}") from exc
    if _sha256_path(manifest_path, "preprocessed package manifest", canonical_id) != manifest_sha:
        raise ReleaseError(f"preprocessed package manifest hash drift: {canonical_id}")
    line_number = binding.get("manifest_line")
    raw_sha = binding.get("raw_line_sha256")
    if not isinstance(line_number, int) or line_number <= 0 or not isinstance(raw_sha, str) or not SHA256_RE.fullmatch(raw_sha):
        raise ReleaseError(f"preprocessed manifest binding invalid: {canonical_id}")
    try:
        raw_line = manifest_path.read_text(encoding="utf-8").splitlines()[line_number - 1]
    except (OSError, IndexError) as exc:
        raise ReleaseError(f"preprocessed manifest line missing: {canonical_id}") from exc
    if sha256_bytes(raw_line.encode("utf-8")) != raw_sha:
        raise ReleaseError(f"preprocessed manifest raw-line hash drift: {canonical_id}")
    try:
        record = json.loads(raw_line)
    except json.JSONDecodeError as exc:
        raise ReleaseError(f"preprocessed manifest record invalid JSON: {canonical_id}") from exc
    if not isinstance(record, dict) or binding.get("record") != record:
        raise ReleaseError(f"preprocessed manifest record binding drift: {canonical_id}")
    return record, {
        "manifest_path": manifest_ref,
        "manifest_sha256": manifest_sha,
        "manifest_line": line_number,
        "raw_line_sha256": raw_sha,
    }


def _preprocessed_source_kind(provenance: Any, canonical_id: str) -> tuple[str, dict[str, Any]]:
    if not isinstance(provenance, dict):
        raise ReleaseError(f"preprocessed input provenance missing: {canonical_id}")
    declared_kind = provenance.get("kind")
    if not isinstance(declared_kind, str) or not declared_kind:
        raise ReleaseError(f"preprocessed input provenance kind invalid: {canonical_id}")
    if declared_kind == "preprocessed_package":
        return "preprocessed_manifest", provenance
    if declared_kind in {"existing_local", "existing_local_revision", "user_supplement", "assembled_preprocessed"}:
        return declared_kind, provenance
    raise ReleaseError(f"preprocessed input provenance kind unsupported: {canonical_id}")


def _validate_preprocessed_input(
    package: dict[str, Any],
    provenance: Any,
    expected_sha: str,
    expected_source_sha: Any,
    expected_pixel_sha: Any,
    canonical_id: str,
    manifest_record: dict[str, Any] | None,
    adopted_png: Path,
) -> tuple[str, dict[str, Any]]:
    kind, input_value = _preprocessed_source_kind(provenance, canonical_id)
    source_sha = expected_source_sha
    if not isinstance(source_sha, str) or not SHA256_RE.fullmatch(source_sha):
        raise ReleaseError(f"preprocessed source bytes SHA-256 invalid: {canonical_id}")
    if not isinstance(expected_pixel_sha, str) or not SHA256_RE.fullmatch(expected_pixel_sha):
        raise ReleaseError(f"preprocessed decoded pixel SHA-256 invalid: {canonical_id}")
    raw_path = input_value.get("source_path")
    path = _receipt_source_path(package, raw_path, canonical_id)
    if _sha256_path(path, "preprocessed input", canonical_id) != source_sha:
        raise ReleaseError(f"preprocessed input hash drift: {canonical_id}")
    try:
        with Image.open(path) as source_image:
            source_image.load()
            mode = "RGBA" if "A" in source_image.getbands() else "RGB"
            source_pixels = source_image.convert(mode)
            source_pixel_sha = sha256_bytes(mode.encode() + b"\0" + str(source_pixels.size).encode() + b"\0" + source_pixels.tobytes())
        with Image.open(adopted_png) as output_image:
            output_image.load()
            output_pixels = output_image.convert(mode)
            output_pixel_sha = sha256_bytes(mode.encode() + b"\0" + str(output_pixels.size).encode() + b"\0" + output_pixels.tobytes())
    except OSError as exc:
        raise ReleaseError(f"preprocessed input image decode failed: {canonical_id}") from exc
    if source_pixel_sha != expected_pixel_sha or output_pixel_sha != expected_pixel_sha:
        raise ReleaseError(f"preprocessed input decoded pixel drift: {canonical_id}")
    if kind == "assembled_preprocessed":
        components = input_value.get("components")
        if not isinstance(components, list) or not components:
            raise ReleaseError(f"assembled preprocessed input components missing: {canonical_id}")
        for component in components:
            if not isinstance(component, dict):
                raise ReleaseError(f"assembled preprocessed input component invalid: {canonical_id}")
            component_path = _receipt_source_path(package, component.get("path"), canonical_id)
            component_sha = component.get("sha256")
            if not isinstance(component_sha, str) or not SHA256_RE.fullmatch(component_sha) or _sha256_path(component_path, "assembled preprocessed component", canonical_id) != component_sha:
                raise ReleaseError(f"assembled preprocessed component hash drift: {canonical_id}")
    return source_sha, input_value


def _preprocessed_asset_binding(
    corpus_root: Path,
    row: dict[str, str],
    references: list[Any],
) -> dict[str, Any] | None:
    canonical_id = str(row.get("exam_question_id") or "")
    revision_ids = {preprocessed_revision_id_from_asset_reference(str(reference)) for reference in references}
    revision_ids.discard(None)
    if not revision_ids:
        return None
    if len(revision_ids) != 1 or any(preprocessed_revision_id_from_asset_reference(str(reference)) is None for reference in references):
        raise ReleaseError(f"preprocessed question assets are not one revision: {canonical_id}")
    revision_id = next(iter(revision_ids))
    receipt_ref = f"_derived/preprocessed-image-adoptions/{revision_id}/{canonical_id}.json"
    receipt_path = corpus_root / receipt_ref
    receipt_bytes = receipt_path.read_bytes() if receipt_path.is_file() and not receipt_path.is_symlink() else None
    if receipt_bytes is None:
        raise ReleaseError(f"preprocessed adoption receipt missing: {canonical_id}")
    try:
        receipt = json.loads(receipt_bytes)
    except json.JSONDecodeError as exc:
        raise ReleaseError(f"preprocessed adoption receipt invalid JSON: {canonical_id}") from exc
    if (
        not isinstance(receipt, dict)
        or receipt.get("schema") != "preprocessed-image-adoption-receipt-v1"
        or receipt.get("revision_id") != revision_id
        or receipt.get("canonical_question_id") != canonical_id
    ):
        raise ReleaseError(f"preprocessed adoption receipt identity invalid: {canonical_id}")
    root_plan_sha = receipt.get("root_plan_sha256")
    root_review = receipt.get("root_review")
    if not isinstance(root_plan_sha, str) or not SHA256_RE.fullmatch(root_plan_sha):
        raise ReleaseError(f"preprocessed root plan hash invalid: {canonical_id}")
    if not isinstance(root_review, dict) or root_review.get("actor") != "root_sol" or root_review.get("decision") != "accept":
        raise ReleaseError(f"preprocessed root acceptance invalid: {canonical_id}")
    package = receipt.get("package")
    roles = receipt.get("roles")
    if not isinstance(package, dict) or not isinstance(roles, dict) or not isinstance(roles.get("question"), dict):
        raise ReleaseError(f"preprocessed adoption receipt roles invalid: {canonical_id}")
    question_role = roles["question"]
    new_assets = question_role.get("new")
    old_assets = question_role.get("old")
    if not isinstance(new_assets, list) or not isinstance(old_assets, list) or len(new_assets) != len(references):
        raise ReleaseError(f"preprocessed question role assets invalid: {canonical_id}")
    bindings = receipt.get("manifest_bindings")
    if not isinstance(bindings, list):
        raise ReleaseError(f"preprocessed manifest bindings invalid: {canonical_id}")
    records: list[dict[str, Any]] = []
    manifest_locators: list[dict[str, Any]] = []
    manifest_by_identity: dict[tuple[int, str], dict[str, Any]] = {}
    manifest_by_component: dict[int, dict[str, Any]] = {}
    for binding in bindings:
        if not isinstance(binding, dict) or binding.get("role") != "question":
            continue
        record, locator = _preprocessed_manifest_record(package, binding, canonical_id)
        records.append(record)
        manifest_locators.append(locator)
        manifest_by_identity[(locator["manifest_line"], locator["raw_line_sha256"])] = record
        component_ordinal = binding.get("component_ordinal")
        if component_ordinal is not None:
            if not isinstance(component_ordinal, int) or component_ordinal <= 0 or component_ordinal in manifest_by_component:
                raise ReleaseError(f"preprocessed component manifest binding invalid: {canonical_id}")
            manifest_by_component[component_ordinal] = binding
    original_pages = receipt.get("original_source_pages")
    if not isinstance(original_pages, dict) or not isinstance(original_pages.get("question"), list) or any(
        not isinstance(page, int) or page <= 0 for page in original_pages["question"]
    ):
        raise ReleaseError(f"preprocessed original source pages invalid: {canonical_id}")
    expected_refs = [str(reference) for reference in references]
    inputs: list[dict[str, Any]] = []
    flattened_pages: list[int] = []
    for ordinal, (asset_info, reference) in enumerate(zip(new_assets, expected_refs), 1):
        if not isinstance(asset_info, dict) or asset_info.get("ref") != reference:
            raise ReleaseError(f"preprocessed receipt asset reference mismatch: {canonical_id}")
        asset = resolve_corpus_asset(corpus_root, reference)
        asset_sha = sha256_file(asset)
        if asset_info.get("sha256") != asset_sha:
            raise ReleaseError(f"preprocessed receipt PNG hash drift: {canonical_id}")
        provenance = asset_info.get("input")
        kind, provenance = _preprocessed_source_kind(provenance, canonical_id)
        manifest_record = None
        if kind == "preprocessed_manifest":
            identity = (provenance.get("manifest_line"), provenance.get("raw_line_sha256"))
            manifest_record = manifest_by_identity.get(identity)
            if manifest_record is None or provenance.get("manifest_record") != manifest_record:
                raise ReleaseError(f"preprocessed input does not bind manifest record: {canonical_id}")
            field = provenance.get("original_image_field")
            if field != "question_image":
                raise ReleaseError(f"preprocessed question image field invalid: {canonical_id}")
            raw_path = manifest_record.get(field) if isinstance(field, str) else None
            if isinstance(raw_path, list):
                raw_path = provenance.get("source_path")
            if not isinstance(raw_path, str) or _receipt_source_path(package, raw_path, canonical_id) != _receipt_source_path(package, provenance.get("source_path"), canonical_id):
                raise ReleaseError(f"preprocessed input source path does not bind manifest: {canonical_id}")
            if provenance.get("file_sha256") != asset_info.get("source_bytes_sha256"):
                raise ReleaseError(f"preprocessed manifest input hash binding invalid: {canonical_id}")
            if receipt.get("package_id") is not None and manifest_record.get("id") != receipt.get("package_id"):
                raise ReleaseError(f"preprocessed package ID does not bind manifest: {canonical_id}")
        source_sha, input_value = _validate_preprocessed_input(
            package,
            provenance,
            asset_sha,
            asset_info.get("source_bytes_sha256"),
            asset_info.get("decoded_pixel_sha256"),
            canonical_id,
            manifest_record,
            asset,
        )
        source_pages = provenance.get("source_pages")
        if not isinstance(source_pages, list) or any(not isinstance(page, int) or page <= 0 for page in source_pages):
            raise ReleaseError(f"preprocessed input source pages invalid: {canonical_id}")
        if kind == "assembled_preprocessed":
            component_bindings = provenance.get("component_manifest_bindings")
            components = provenance.get("components")
            if not isinstance(component_bindings, list) or not isinstance(components, list):
                raise ReleaseError(f"assembled preprocessed manifest components missing: {canonical_id}")
            input_component_bindings: dict[int, dict[str, Any]] = {}
            component_pages: list[int] = []
            for component in components:
                if not isinstance(component, dict):
                    raise ReleaseError(f"assembled preprocessed component invalid: {canonical_id}")
                pages = component.get("source_pages")
                if not isinstance(pages, list) or any(not isinstance(page, int) or page <= 0 for page in pages):
                    raise ReleaseError(f"assembled preprocessed component pages invalid: {canonical_id}")
                component_pages.extend(pages)
            for component_binding in component_bindings:
                if not isinstance(component_binding, dict):
                    raise ReleaseError(f"assembled preprocessed component binding invalid: {canonical_id}")
                ordinal = component_binding.get("ordinal")
                identity = (component_binding.get("manifest_line"), component_binding.get("raw_line_sha256"))
                record = manifest_by_identity.get(identity)
                if not isinstance(ordinal, int) or ordinal <= 0 or ordinal > len(components) or ordinal in input_component_bindings:
                    raise ReleaseError(f"assembled preprocessed component ordinal invalid: {canonical_id}")
                if record is None or component_binding.get("record") != record:
                    raise ReleaseError(f"assembled preprocessed component record binding drift: {canonical_id}")
                receipt_binding = manifest_by_component.get(ordinal)
                if receipt_binding is None or receipt_binding.get("record") != record:
                    raise ReleaseError(f"assembled preprocessed receipt component binding missing: {canonical_id}")
                component = components[ordinal - 1]
                field = component_binding.get("original_image_field")
                if field != component.get("original_image_field") or record.get(field) != component.get("path"):
                    raise ReleaseError(f"assembled preprocessed component field mismatch: {canonical_id}")
                if component.get("file_sha256") != component.get("sha256"):
                    raise ReleaseError(f"assembled preprocessed component hash binding invalid: {canonical_id}")
                input_component_bindings[ordinal] = component_binding
            stable_unique_pages = list(dict.fromkeys(component_pages))
            if tuple(source_pages) not in {tuple(component_pages), tuple(stable_unique_pages)}:
                raise ReleaseError(f"assembled preprocessed source pages do not match components: {canonical_id}")
        flattened_pages.extend(source_pages)
        inputs.append({"kind": kind, "source_image_sha256": source_sha, "provenance": provenance})
    if original_pages["question"] != flattened_pages:
        raise ReleaseError(f"preprocessed original source pages do not match image inputs: {canonical_id}")
    if canonical_id.startswith("EXAM408-2021-") and (
        receipt.get("package_id") is not None or any(item["kind"] not in {"existing_local", "existing_local_revision"} for item in inputs)
    ):
        raise ReleaseError(f"2021 preprocessed receipt is not local-only: {canonical_id}")
    for asset_info in old_assets:
        if not isinstance(asset_info, dict) or not isinstance(asset_info.get("ref"), str) or not isinstance(asset_info.get("sha256"), str):
            raise ReleaseError(f"preprocessed receipt old asset invalid: {canonical_id}")
        if sha256_file(resolve_corpus_asset(corpus_root, asset_info["ref"])) != asset_info["sha256"]:
            raise ReleaseError(f"preprocessed receipt old asset hash drift: {canonical_id}")
    metadata = [
        {
            "package_id": record.get("id"),
            "source_status": record.get("source_status"),
            "review_flags": record.get("review_flags"),
            "subject_code": record.get("subject_code"),
        }
        for record in records
    ]
    return {
        "receipt_ref": receipt_ref,
        "receipt_sha256": sha256_bytes(receipt_bytes),
        "root_plan_sha256": root_plan_sha,
        "source_pages": original_pages["question"],
        "manifest_bindings": manifest_locators,
        "metadata": metadata,
        "inputs": inputs,
    }


def source_object(value: Any, canonical_id: str, label: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ReleaseError(f"source override {label} invalid: {canonical_id}")
    path = value.get("pdf_path")
    digest = value.get("pdf_sha256")
    pages = value.get("pages")
    if not isinstance(path, str) or not path or not isinstance(digest, str) or not SHA256_RE.fullmatch(digest):
        raise ReleaseError(f"source override {label} locator invalid: {canonical_id}")
    if not isinstance(pages, list) or not pages or any(not isinstance(page, int) or page <= 0 for page in pages):
        raise ReleaseError(f"source override {label} pages invalid: {canonical_id}")
    return {"pdf_path": path, "pdf_sha256": digest, "pages": pages}


def source_override_root_accepts(value: Any, canonical_id: str, replacement: dict[str, Any]) -> None:
    if not isinstance(value, dict):
        raise ReleaseError(f"source override root acceptance missing: {canonical_id}")
    if value.get("actor") != "root_sol" or value.get("decision") != "accept_verified_replacement_source":
        raise ReleaseError(f"source override root acceptance invalid: {canonical_id}")
    if value.get("verified_question_identity") is not True or value.get("verified_required_figure_complete") is not True:
        raise ReleaseError(f"source override root verification incomplete: {canonical_id}")
    if value.get("replacement_pdf_sha256") != replacement["pdf_sha256"]:
        raise ReleaseError(f"source override root replacement hash mismatch: {canonical_id}")
    if value.get("replacement_pages") != replacement["pages"]:
        raise ReleaseError(f"source override root replacement pages mismatch: {canonical_id}")


def _source_override_binding(
    corpus_root: Path,
    row: dict[str, str],
    references: list[Any],
    pages: list[int],
) -> dict[str, Any] | None:
    """Resolve one exact, root-accepted replacement-source adoption.

    The adoption receipt is intentionally found by the revision embedded in the
    current asset references.  This avoids a broad receipt scan and prevents a
    different repair from donating provenance to this question.
    """
    canonical_id = str(row.get("exam_question_id") or "")
    source_path = row.get("question_source_path", "")
    source_sha = row.get("question_source_sha256", "")
    if bool(source_path) != bool(source_sha):
        raise ReleaseError(f"question source fields incomplete: {canonical_id}")
    revision_ids = {revision_id_from_asset_reference(str(reference)) for reference in references}
    revision_ids.discard(None)
    if not revision_ids:
        return None
    if len(revision_ids) != 1:
        raise ReleaseError(f"question assets span multiple revisions: {canonical_id}")
    if not source_path:
        # Crop-only revisions remain compatible with historical releases.  They
        # do not acquire replacement-source authority simply by being revised.
        return None
    if any(revision_id_from_asset_reference(str(reference)) is None for reference in references):
        raise ReleaseError(f"source override assets are not all revision assets: {canonical_id}")
    if not SHA256_RE.fullmatch(source_sha):
        raise ReleaseError(f"question source SHA-256 invalid: {canonical_id}")
    revision_id = next(iter(revision_ids))
    receipt_ref = f"_derived/crop-adoptions/{revision_id}.json"
    receipt_path = corpus_root / receipt_ref
    if not receipt_path.is_file() or receipt_path.is_symlink():
        raise ReleaseError(f"source override adoption receipt missing: {canonical_id}")
    receipt_bytes = receipt_path.read_bytes()
    try:
        receipt = json.loads(receipt_bytes)
    except json.JSONDecodeError as exc:
        raise ReleaseError(f"source override adoption receipt invalid JSON: {canonical_id}") from exc
    if not isinstance(receipt, dict) or receipt.get("schema") != "question-crop-adoption-v2" or receipt.get("revision_id") != revision_id:
        raise ReleaseError(f"source override adoption receipt identity invalid: {canonical_id}")
    repairs = receipt.get("repairs")
    if not isinstance(repairs, list):
        raise ReleaseError(f"source override adoption receipt repairs invalid: {canonical_id}")
    matches = [repair for repair in repairs if isinstance(repair, dict) and repair.get("id") == canonical_id]
    if len(matches) != 1:
        raise ReleaseError(f"source override adoption receipt item mismatch: {canonical_id}")
    repair = matches[0]
    override = repair.get("source_override")
    override_hash = repair.get("source_override_sha256")
    if not isinstance(override, dict) or override.get("schema") != "question-source-override-v1":
        raise ReleaseError(f"source override declaration missing: {canonical_id}")
    if not isinstance(override_hash, str) or not SHA256_RE.fullmatch(override_hash):
        raise ReleaseError(f"source override declaration hash invalid: {canonical_id}")
    if override.get("exam_question_id") != canonical_id:
        raise ReleaseError(f"source override declaration identity mismatch: {canonical_id}")
    original = source_object(repair.get("original_source"), canonical_id, "original source")
    replacement = source_object(repair.get("effective_source"), canonical_id, "effective source")
    if repair.get("source_pdf") != replacement["pdf_path"] or repair.get("source_pdf_sha256") != replacement["pdf_sha256"]:
        raise ReleaseError(f"source override receipt effective source mismatch: {canonical_id}")
    override_original = source_object(override.get("original_source"), canonical_id, "declaration original source")
    override_replacement = source_object(override.get("replacement_source"), canonical_id, "declaration replacement source")
    if override_original != original or override_replacement != replacement:
        raise ReleaseError(f"source override declaration does not bind receipt sources: {canonical_id}")
    source_override_root_accepts(override.get("root_accept"), canonical_id, replacement)
    if replacement["pdf_path"] != source_path or replacement["pdf_sha256"] != source_sha:
        raise ReleaseError(f"question source does not match effective replacement source: {canonical_id}")
    replacement_path = Path(source_path)
    if not replacement_path.is_file() or replacement_path.is_symlink() or sha256_file(replacement_path) != source_sha:
        raise ReleaseError(f"replacement source PDF hash drift: {canonical_id}")
    images = repair.get("images")
    if not isinstance(images, list) or len(images) != len(references) or len(pages) != len(references):
        raise ReleaseError(f"source override image/page binding invalid: {canonical_id}")
    expected_assets = [str(reference) for reference in references]
    for ordinal, (image, reference, source_page) in enumerate(zip(images, expected_assets, pages), 1):
        if not isinstance(image, dict):
            raise ReleaseError(f"source override image invalid: {canonical_id}")
        if image.get("ordinal") != ordinal or image.get("ref") != reference or image.get("page") != source_page:
            raise ReleaseError(f"source override image/page mismatch: {canonical_id}")
        if (
            image.get("source_pdf") != source_path
            or image.get("source_pdf_sha256") != source_sha
            or image.get("source_page") != source_page
            or image.get("region_indices") != image.get("regions")
        ):
            raise ReleaseError(f"source override truthful image binding invalid: {canonical_id}")
        asset = resolve_corpus_asset(corpus_root, reference)
        if image.get("sha256") != sha256_file(asset):
            raise ReleaseError(f"source override image hash drift: {canonical_id}")
        if source_page not in replacement["pages"]:
            raise ReleaseError(f"source override image page outside replacement source: {canonical_id}")
    return {
        "receipt_ref": receipt_ref,
        "receipt_sha256": sha256_bytes(receipt_bytes),
        "source_override_sha256": override_hash,
        "source_locator": {"pdf_path": source_path, "pdf_sha256": source_sha},
    }


def question_asset_commitments(corpus_root: Path, row: dict[str, str]) -> list[dict[str, Any]]:
    references = parse_json_list(row.get("question_assets", "[]"), f"{row.get('exam_question_id')} question_assets")
    pages = [int(value) for value in parse_json_list(row.get("question_pages", "[]"), f"{row.get('exam_question_id')} question_pages")]
    preprocessed_binding = _preprocessed_asset_binding(corpus_root, row, references)
    override_binding = None if preprocessed_binding is not None else _source_override_binding(corpus_root, row, references, pages)
    commitments: list[dict[str, Any]] = []
    for index, raw_reference in enumerate(references):
        reference = str(raw_reference)
        asset = resolve_corpus_asset(corpus_root, reference)
        source_page = pages[min(index, len(pages) - 1)] if pages else None
        commitment = {
            "ref": reference,
            "sha256": sha256_file(asset),
            "size_bytes": asset.stat().st_size,
            "page_locator": {"source_page": source_page, "asset_ordinal": index + 1},
        }
        if preprocessed_binding is not None:
            input_binding = preprocessed_binding["inputs"][index]
            provenance = input_binding["provenance"]
            source_pages = provenance.get("source_pages")
            if not isinstance(source_pages, list) or any(not isinstance(page, int) or page <= 0 for page in source_pages):
                raise ReleaseError(f"preprocessed input source pages invalid: {row.get('exam_question_id')}")
            commitment["source_locator"] = {
                "kind": input_binding["kind"],
                "source_pages": source_pages,
                "source_image_sha256": input_binding["source_image_sha256"],
                "manifest_bindings": preprocessed_binding["manifest_bindings"],
                "package_metadata": preprocessed_binding["metadata"],
            }
            commitment["preprocessed_adoption"] = {
                "receipt_ref": preprocessed_binding["receipt_ref"],
                "receipt_sha256": preprocessed_binding["receipt_sha256"],
                "root_plan_sha256": preprocessed_binding["root_plan_sha256"],
                "item_id": row.get("exam_question_id"),
            }
        elif override_binding is not None:
            commitment["source_locator"] = {**override_binding["source_locator"], "source_page": source_page}
            commitment["source_override"] = {
                "adoption_receipt_ref": override_binding["receipt_ref"],
                "adoption_receipt_sha256": override_binding["receipt_sha256"],
                "source_override_sha256": override_binding["source_override_sha256"],
                "item_id": row.get("exam_question_id"),
            }
        commitments.append(commitment)
    return commitments


def validate_source_override_commitments(
    corpus_root: Path,
    canonical_id: str,
    assets: list[dict[str, Any]],
) -> None:
    """Cold-audit an override commitment against its exact adoption receipt."""
    declared = [asset.get("source_override") for asset in assets]
    if not any(value is not None for value in declared):
        return
    if any(not isinstance(value, dict) for value in declared):
        raise ReleaseError(f"source override commitment incomplete: {canonical_id}")
    locators = [asset.get("source_locator") for asset in assets]
    if any(not isinstance(locator, dict) for locator in locators):
        raise ReleaseError(f"source override locator missing: {canonical_id}")
    paths = {locator.get("pdf_path") for locator in locators}
    digests = {locator.get("pdf_sha256") for locator in locators}
    if len(paths) != 1 or len(digests) != 1:
        raise ReleaseError(f"source override locator inconsistent: {canonical_id}")
    source_path = next(iter(paths))
    source_sha = next(iter(digests))
    if not isinstance(source_path, str) or not isinstance(source_sha, str):
        raise ReleaseError(f"source override locator invalid: {canonical_id}")
    references = [asset.get("ref") for asset in assets]
    pages = [locator.get("source_page") for locator in locators]
    if any(not isinstance(reference, str) for reference in references) or any(not isinstance(page, int) for page in pages):
        raise ReleaseError(f"source override asset/page locator invalid: {canonical_id}")
    binding = _source_override_binding(
        corpus_root,
        {
            "exam_question_id": canonical_id,
            "question_source_path": source_path,
            "question_source_sha256": source_sha,
        },
        references,
        pages,
    )
    if binding is None:
        raise ReleaseError(f"source override commitment has no verified binding: {canonical_id}")
    for ordinal, (asset, locator, override) in enumerate(zip(assets, locators, declared), 1):
        if locator != {**binding["source_locator"], "source_page": pages[ordinal - 1]}:
            raise ReleaseError(f"source override source locator drift: {canonical_id}")
        expected_override = {
            "adoption_receipt_ref": binding["receipt_ref"],
            "adoption_receipt_sha256": binding["receipt_sha256"],
            "source_override_sha256": binding["source_override_sha256"],
            "item_id": canonical_id,
        }
        if override != expected_override:
            raise ReleaseError(f"source override receipt commitment drift: {canonical_id}")


def validate_preprocessed_asset_commitments(
    corpus_root: Path,
    canonical_id: str,
    assets: list[dict[str, Any]],
) -> None:
    declared = [asset.get("preprocessed_adoption") for asset in assets]
    if not any(value is not None for value in declared):
        return
    if any(not isinstance(value, dict) for value in declared):
        raise ReleaseError(f"preprocessed adoption commitment incomplete: {canonical_id}")
    references = [asset.get("ref") for asset in assets]
    if any(not isinstance(reference, str) for reference in references):
        raise ReleaseError(f"preprocessed asset reference invalid: {canonical_id}")
    binding = _preprocessed_asset_binding(corpus_root, {"exam_question_id": canonical_id}, references)
    if binding is None:
        raise ReleaseError(f"preprocessed adoption commitment has no verified binding: {canonical_id}")
    expected_adoption = {
        "receipt_ref": binding["receipt_ref"],
        "receipt_sha256": binding["receipt_sha256"],
        "root_plan_sha256": binding["root_plan_sha256"],
        "item_id": canonical_id,
    }
    for index, (asset, adoption) in enumerate(zip(assets, declared)):
        if adoption != expected_adoption:
            raise ReleaseError(f"preprocessed adoption receipt commitment drift: {canonical_id}")
        input_binding = binding["inputs"][index]
        provenance = input_binding["provenance"]
        source_pages = provenance.get("source_pages")
        expected_locator = {
            "kind": input_binding["kind"],
            "source_pages": source_pages,
            "source_image_sha256": input_binding["source_image_sha256"],
            "manifest_bindings": binding["manifest_bindings"],
            "package_metadata": binding["metadata"],
        }
        if asset.get("source_locator") != expected_locator:
            raise ReleaseError(f"preprocessed source locator drift: {canonical_id}")


def answer_capsule(
    corpus_root: Path,
    row: dict[str, str],
    canonical_id: str,
    table_evidence: dict[str, Any],
    table_evidence_ref: str,
    table_evidence_sha256: str,
) -> tuple[bytes, str]:
    card = corpus_root / "cards" / f"{canonical_id}.md"
    if card.is_file() and not card.is_symlink():
        card_text = card.read_text(encoding="utf-8")
        card_sha = sha256_file(card)
        card_ref = f"cards/{canonical_id}.md"
    else:
        card_text = ""
        card_sha = None
        card_ref = None
    solution_ocr = row.get("solution_ocr", "")
    solution_status, solution_answer = strict_answer_option("", solution_ocr)
    _year, question_no = parse_canonical_id(canonical_id)
    table_answer = (table_evidence.get("candidate_answers") or {}).get(str(question_no))
    table_complete = table_evidence.get("structurally_complete") is True
    if table_answer and solution_status == "resolved" and table_answer != solution_answer:
        status, answer = "conflict", None
        verification_method = "answer_table_solution_conflict"
    elif table_complete and table_answer:
        status, answer = "resolved", table_answer
        verification_method = "complete_clear_answer_table" if solution_status != "resolved" else "complete_table_plus_solution_agree"
    elif table_answer and solution_status == "resolved" and table_answer == solution_answer:
        status, answer = "resolved", table_answer
        verification_method = "two_independent_evidence_agree"
    else:
        status, answer = "missing", None
        verification_method = "insufficient_independent_evidence"
    payload = {
        "schema_version": "canonical_answer_capsule_v1",
        "canonical_id": canonical_id,
        "parser_status": status,
        "parser": "answer_table_grid_plus_solution_token_verifier_v1",
        "source_card_ref": card_ref,
        "source_card_sha256": card_sha,
        "source_solution_ocr_sha256": sha256_bytes(solution_ocr.encode("utf-8")),
        "answer_table_evidence_ref": table_evidence_ref,
        "answer_table_evidence_sha256": table_evidence_sha256,
        "answer_table_source_page": table_evidence.get("source_page"),
        "answer_table_source_page_bbox_pixels": table_evidence.get("source_page_bbox_pixels"),
        "answer_table_render_sha256": table_evidence.get("render_sha256"),
        "answer_table_candidate": table_answer,
        "solution_ocr_candidate": solution_answer,
        "verification_method": verification_method,
        "correct_option": answer,
        "private": True,
    }
    return canonical_json(payload), status


def v2_axes(features: dict[str, Any]) -> dict[str, Any]:
    return {
        "invariant_axes": [
            "main_knowledge",
            "cognitive_action",
            "polarity",
            "reasoning_depth",
            "distractor_mechanism_categories",
        ],
        "mutable_surface_axes": [
            "narrative_entities",
            "surface_symbols",
            "parameter_instantiation_within_legal_parameter_ranges",
            "option_wording_and_order",
            "condition_density_adjacent_bucket",
            "data_representation_compatible_carrier",
            "calculation_scale_adjacent_bucket",
        ],
        "observed_axis_categories": {
            "condition_density": features["condition_density"]["category"],
            "data_representation": features["data_representation"]["category"],
            "calculation_scale": features["calculation_scale"]["category"],
            "polarity": features["polarity"]["category"],
            "reasoning_depth": features["reasoning_depth"]["category"],
        },
    }


def v3_allowed_edges(cognitive_action: str) -> list[dict[str, Any]]:
    edges = [
        {
            "edge": "condition_density_adjacent_bucket",
            "mutable_axis": "condition_density",
            "allowed_transition": "low<->medium_or_medium<->high",
            "max_changed_structural_axes": 1,
        },
        {
            "edge": "data_representation_compatible_carrier",
            "mutable_axis": "data_representation",
            "allowed_transition": "one_compatible_representation_category_only",
            "max_changed_structural_axes": 1,
        },
        {
            "edge": "option_parallelism_adjacent_bucket",
            "mutable_axis": "option_parallelism_proxy",
            "allowed_transition": "low->medium_or_medium->high",
            "max_changed_structural_axes": 1,
        },
    ]
    if cognitive_action == "calculate_or_derive":
        edges.append(
            {
                "edge": "calculation_scale_adjacent_bucket",
                "mutable_axis": "calculation_scale",
                "allowed_transition": "single_step<->short_multi_step",
                "max_changed_structural_axes": 1,
            }
        )
    return edges


def expected_t9_path(row: dict[str, str], t9_root: Path) -> Path:
    year = int(row["year"])
    subdir = "2009-2016" if year <= 2016 else "2017-2025"
    if year == 2021 and row["role"] == "question":
        filename = "2021年408计算机专业基础真题.pdf"
    else:
        filename = Path(row["source_path"]).name
    return t9_root / subdir / filename


def reconcile_sources(source_rows: list[dict[str, str]], t9_root: Path) -> list[dict[str, Any]]:
    if len(source_rows) != SOURCE_SLOT_COUNT:
        raise ReleaseError(f"source manifest has {len(source_rows)} rows, expected {SOURCE_SLOT_COUNT}")
    if len({(row.get("year"), row.get("role")) for row in source_rows}) != SOURCE_SLOT_COUNT:
        raise ReleaseError("source manifest does not contain 34 unique year-role slots")

    result: list[dict[str, Any]] = []
    for row in sorted(source_rows, key=lambda item: (int(item["year"]), item["role"])):
        year = int(row["year"])
        role = row["role"]
        manifest_sha = row["sha256"].lower()
        if not SHA256_RE.fullmatch(manifest_sha):
            raise ReleaseError(f"invalid source SHA-256 for {year}/{role}")
        candidate = expected_t9_path(row, t9_root)
        fallback = Path(row["source_path"])
        candidate_exists = candidate.is_file()
        candidate_sha = sha256_file(candidate) if candidate_exists else None
        fallback_exists = fallback.is_file()
        fallback_sha = sha256_file(fallback) if fallback_exists else None

        if candidate_exists and candidate_sha == manifest_sha:
            status = "t9_hash_match"
            authority_path = candidate
            authority_kind = "t9_version2_attachment"
        elif year == 2021 and role == "question" and not candidate_exists and fallback_sha == manifest_sha:
            status = "legacy_fallback_registered"
            authority_path = fallback
            authority_kind = "local_version1_fallback"
        elif candidate_exists:
            status = "t9_hash_conflict"
            authority_path = candidate
            authority_kind = "conflict"
        else:
            status = "source_missing"
            authority_path = fallback
            authority_kind = "unavailable"

        result.append(
            {
                "year": year,
                "role": role,
                "manifest_sha256": manifest_sha,
                "t9_expected_path": str(candidate),
                "t9_exists": candidate_exists,
                "t9_sha256": candidate_sha,
                "fallback_path": str(fallback) if year == 2021 and role == "question" else None,
                "fallback_exists": fallback_exists if year == 2021 and role == "question" else None,
                "fallback_sha256": fallback_sha if year == 2021 and role == "question" else None,
                "authority_path": str(authority_path),
                "authority_kind": authority_kind,
                "status": status,
                "production_usable": status in {"t9_hash_match", "legacy_fallback_registered"},
            }
        )
    return result


def build_exposure_and_profiles(
    index_rows: list[dict[str, str]],
    *,
    asset_commitments: dict[str, list[dict[str, Any]]] | None = None,
    capsule_commitments: dict[str, dict[str, Any]] | None = None,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    if len(index_rows) != QUESTION_COUNT:
        raise ReleaseError(f"question index has {len(index_rows)} rows, expected {QUESTION_COUNT}")
    ids = [row.get("exam_question_id", "") for row in index_rows]
    expected = {
        f"EXAM408-{year}-Q{question_no:02d}"
        for year in range(2009, 2026)
        for question_no in range(1, 48)
    }
    if set(ids) != expected or len(ids) != len(set(ids)):
        raise ReleaseError("question index canonical identity set is not exactly 2009-2025 Q01-Q47")

    asset_commitments = asset_commitments or {}
    capsule_commitments = capsule_commitments or {}
    exposure: list[dict[str, Any]] = []
    profile_groups: dict[tuple[str, str, str], dict[str, Any]] = {}
    for row in sorted(index_rows, key=lambda item: parse_canonical_id(item["exam_question_id"])):
        canonical_id = row["exam_question_id"]
        year, question_no = parse_canonical_id(canonical_id)
        knowledge = knowledge_code(row.get("main_knowledge", ""))
        cognitive_action = classify_cognitive_action(row.get("question_ocr", ""))
        question_form = classify_question_form(question_no, row.get("question_ocr", ""))
        exposure_mode = "direct_pool" if question_no <= 40 else "style_only"
        assets = asset_commitments.get(canonical_id, [])
        raw_asset_count = len(parse_json_list(row.get("question_assets", "[]"), f"{canonical_id} question_assets"))
        features = deep_style_features(row, question_no, raw_asset_count)
        capsule = capsule_commitments.get(canonical_id)
        direct_usable = bool(assets) and capsule is not None and capsule.get("parser_status") == "resolved"
        production_usable = direct_usable if exposure_mode == "direct_pool" else bool(assets)
        exposure.append(
            {
                "schema_version": "canonical_question_exposure_v1",
                "canonical_id": canonical_id,
                "year": year,
                "official_question_no": question_no,
                "main_knowledge": knowledge,
                "cognitive_action": cognitive_action,
                "question_form": question_form,
                "exposure_mode": exposure_mode,
                "direct_replay_allowed": exposure_mode == "direct_pool",
                "style_observation_allowed": True,
                "question_asset_refs": assets,
                "direct_surface_status": (
                    "ready" if direct_usable else (capsule or {}).get("parser_status", "not_applicable")
                ),
                "answer_capsule_commitment_ref": (capsule or {}).get("ref"),
                "answer_capsule_commitment_sha256": (capsule or {}).get("sha256"),
                **features,
                "v2_axes": v2_axes(features),
                "v3_allowed_single_structural_edges": v3_allowed_edges(cognitive_action),
                "answer_content_in_release": False,
                "production_usable": production_usable,
            }
        )

        key = (knowledge, cognitive_action, question_form)
        if key not in profile_groups:
            profile_groups[key] = {
                "schema_version": "exam_style_profile_v1",
                "main_knowledge": knowledge,
                "cognitive_action": cognitive_action,
                "question_form": question_form,
                "question_count": 0,
                "direct_pool_count": 0,
                "style_only_count": 0,
                "years": set(),
                "canonical_ids": [],
                "_feature_rows": [],
            }
        group = profile_groups[key]
        group["question_count"] += 1
        group["direct_pool_count"] += int(exposure_mode == "direct_pool")
        group["style_only_count"] += int(exposure_mode == "style_only")
        group["years"].add(year)
        group["canonical_ids"].append(canonical_id)
        group["_feature_rows"].append(features)

    profiles: list[dict[str, Any]] = []
    for key in sorted(profile_groups):
        group = profile_groups[key]
        group["years"] = sorted(group["years"])
        group["canonical_ids"] = sorted(group["canonical_ids"])
        group["profile_id"] = "ESP-" + sha256_bytes(canonical_json({name: group[name] for name in ("main_knowledge", "cognitive_action", "question_form")}))[:16]
        feature_rows = group.pop("_feature_rows")
        group["condition_density"] = dict(sorted(Counter(row["condition_density"]["category"] for row in feature_rows).items()))
        group["data_representation"] = dict(sorted(Counter(category for row in feature_rows for category in row["data_representation"]["category"]).items()))
        parameter_categories = [category for row in feature_rows for category in row["legal_parameter_ranges"]["categories"]]
        group["legal_parameter_ranges"] = {
            "parameter_type_counts": dict(sorted(Counter(row["parameter_type"] for row in parameter_categories).items())),
            "observation_count_bucket_counts": dict(sorted(Counter(row["observation_count_bucket"] for row in parameter_categories).items())),
            "raw_values_stored": False,
        }
        group["calculation_scale"] = dict(sorted(Counter(row["calculation_scale"]["category"] for row in feature_rows).items()))
        group["polarity"] = dict(sorted(Counter(row["polarity"]["category"] for row in feature_rows).items()))
        group["option_parallelism_proxy"] = dict(sorted(Counter(row["option_parallelism_proxy"]["category"] for row in feature_rows).items()))
        group["reasoning_depth"] = dict(sorted(Counter(row["reasoning_depth"]["category"] for row in feature_rows).items()))
        group["distractor_mechanism_categories"] = dict(sorted(Counter(category for row in feature_rows for category in row["distractor_mechanism_categories"]["category"]).items()))
        feature_objects = [value for row in feature_rows for value in row.values() if isinstance(value, dict) and "confidence" in value]
        group["inference_quality"] = {
            "confidence_counts": dict(sorted(Counter(value["confidence"] for value in feature_objects).items())),
            "sources": sorted({source for value in feature_objects for source in value.get("source", [])}),
        }
        group["v2_axes"] = {
            "invariant_axes": ["main_knowledge", "cognitive_action", "polarity", "reasoning_depth", "distractor_mechanism_categories"],
            "mutable_surface_axes": ["narrative_entities", "surface_symbols", "parameter_instantiation_within_legal_parameter_ranges", "option_wording_and_order", "condition_density_adjacent_bucket", "data_representation_compatible_carrier", "calculation_scale_adjacent_bucket"],
        }
        group["v3_allowed_single_structural_edges"] = v3_allowed_edges(group["cognitive_action"])
        profiles.append(group)
    return exposure, profiles


def prepare_surface_bindings(
    corpus_root: Path,
    index_rows: list[dict[str, str]],
    answer_table_evidence: dict[int, dict[str, Any]],
) -> tuple[dict[str, list[dict[str, Any]]], dict[str, bytes], dict[str, dict[str, Any]]]:
    assets: dict[str, list[dict[str, Any]]] = {}
    private_capsules: dict[str, bytes] = {}
    commitments: dict[str, dict[str, Any]] = {}
    evidence_bindings: dict[int, tuple[str, str]] = {}
    for year, evidence in sorted(answer_table_evidence.items()):
        evidence_name = f"answer-table-evidence/{year}.json"
        evidence_bytes = canonical_json(evidence)
        private_capsules[evidence_name] = evidence_bytes
        evidence_bindings[year] = (f"private-sibling://{evidence_name}", sha256_bytes(evidence_bytes))
    for row in index_rows:
        canonical_id = row["exam_question_id"]
        _year, question_no = parse_canonical_id(canonical_id)
        assets[canonical_id] = question_asset_commitments(corpus_root, row)
        if question_no > 40:
            continue
        evidence_ref, evidence_sha = evidence_bindings[_year]
        capsule_bytes, parser_status = answer_capsule(
            corpus_root,
            row,
            canonical_id,
            answer_table_evidence[_year],
            evidence_ref,
            evidence_sha,
        )
        capsule_name = f"answer-capsules/{canonical_id}.json"
        private_capsules[capsule_name] = capsule_bytes
        commitments[canonical_id] = {
            "ref": f"private-sibling://{capsule_name}",
            "sha256": sha256_bytes(capsule_bytes),
            "parser_status": parser_status,
        }
    return assets, private_capsules, commitments


def jsonl_bytes(rows: Iterable[dict[str, Any]]) -> bytes:
    return b"".join(canonical_json(row) for row in rows)


def style_guide_bytes(profiles: list[dict[str, Any]]) -> bytes:
    lines = [
        "# 408 真题风格结构规范",
        "",
        "本规范是 2009–2025 年 canonical 语料的答案安全结构画像。它不保存题干、选项、答案或解析，也不授权模型直接生成或发布题目。",
        "",
        "## 使用边界",
        "",
        "- Q01–Q40 共 680 题属于 direct pool；是否展示原题仍需通过运行时近 7 天、来源绑定和答案隔离门禁。",
        "- Q41–Q47 共 119 题只用于 style observation，不得作为晨间选择题直接池。",
        "- 2026 三源均为 provisional；未逐题交叉核验前，production usable question count 固定为 0。",
        "- 408 重点考纲只提供 priority hint，不是题目事实、答案依据、生成授权或验收依据。",
        "",
        "## 受控维度",
        "",
        "- knowledge：canonical index 中已复核的唯一主知识点代码。",
        "- cognitive action：由受控词法规则确定的认知动作类别。",
        "- question form：单选直接问法、判断、情境、定量或综合题结构。",
        "- deep style：条件密度、数据表示、参数类型与范围桶、计算规模、问法极性、选项平行度代理、推理深度和干扰项机制；所有推断均带 confidence/source。",
        "- direct surface：公开层只保存题面资产 locator/SHA/page 与私有答案 capsule commitment；答案字母仅存在于权限受限的私有 sibling。",
        "",
        "## 结构画像",
        "",
        "| profile_id | knowledge | cognitive_action | question_form | condition | representation | calculation | polarity | reasoning | distractor | total | direct | style_only | years |",
        "|---|---|---|---|---|---|---|---|---|---|---:|---:|---:|---|",
    ]
    for profile in profiles:
        years = f"{profile['years'][0]}–{profile['years'][-1]}" if profile["years"] else ""
        dominant = lambda field: max(profile[field], key=lambda key: (profile[field][key], key)) if profile[field] else "none"
        lines.append(
            f"| {profile['profile_id']} | {profile['main_knowledge']} | {profile['cognitive_action']} | "
            f"{profile['question_form']} | {dominant('condition_density')} | {dominant('data_representation')} | "
            f"{dominant('calculation_scale')} | {dominant('polarity')} | {dominant('reasoning_depth')} | "
            f"{dominant('distractor_mechanism_categories')} | {profile['question_count']} | "
            f"{profile['direct_pool_count']} | {profile['style_only_count']} | {years} |"
        )
    lines.extend(
        [
            "",
            "## V2/V3 读取规则",
            "",
            "V2 只能消费 canonical-exposure.jsonl 中 production_usable=true 的已验收 canonical 项；direct replay 还必须要求 exposure_mode=direct_pool。V3 若使用 style profile 规划候选，仍必须经过独立 author、verifier、来源承诺、答案隔离和运行时 freshness 门禁。本 release 本身不构成出题授权。",
            "",
        ]
    )
    return "\n".join(lines).encode("utf-8")


def source_descriptor(path: Path, role: str, *, priority_hint_only: bool = False) -> dict[str, Any]:
    if not path.is_file():
        raise ReleaseError(f"missing source: {path}")
    return {
        "role": role,
        "source_path": str(path),
        "sha256": sha256_file(path),
        "pages": pdf_pages(path),
        "review_status": "unreviewed" if not priority_hint_only else "hint_only",
        "production_usable": False,
        "authority": "priority_hint_only" if priority_hint_only else "provisional_source_only",
    }


def provisional_documents(source_paths: list[Path]) -> tuple[dict[str, Any], dict[str, Any], dict[str, Any]]:
    if len(source_paths) != 3:
        raise ReleaseError(f"2026 provisional source count={len(source_paths)}, expected 3")
    descriptors = [source_descriptor(path, role) for path, role in zip(source_paths, PROVISIONAL_SOURCE_ROLES)]
    source_hashes = [item["sha256"] for item in descriptors]
    source_set_sha = sha256_bytes(canonical_json(source_hashes))
    manifest = {
        "schema_version": "provisional_exam_source_manifest_v1",
        "year": 2026,
        "source_count": 3,
        "source_set_sha256": source_set_sha,
        "review_status": "unreviewed_multi_source",
        "production_usable_question_count": 0,
        "sources": descriptors,
    }
    conflicts = {
        "schema_version": "provisional_exam_conflict_state_v1",
        "year": 2026,
        "source_set_sha256": source_set_sha,
        "cross_source_question_identity_status": "not_reviewed",
        "cross_source_answer_agreement_status": "not_reviewed",
        "official_source_status": "not_established",
        "conflict_resolution_status": "open",
        "production_usable": False,
        "production_usable_question_count": 0,
        "required_successor": "question_level_three_source_review_and_acceptance",
    }
    structure = {
        "schema_version": "provisional_exam_structure_profile_v1",
        "year": 2026,
        "source_set_sha256": source_set_sha,
        "observed_source_file_count": 3,
        "canonical_question_count": 0,
        "style_profile_count": 0,
        "production_usable_question_count": 0,
        "status": "source_bound_not_extracted_not_reviewed",
        "content_copied_into_main_repository": False,
    }
    return manifest, conflicts, structure


def atomic_write(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temp_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp_name, path)
    finally:
        try:
            Path(temp_name).unlink()
        except FileNotFoundError:
            pass


def write_immutable(path: Path, data: bytes) -> None:
    if path.exists():
        if path.read_bytes() != data:
            raise ReleaseError(f"immutable release artifact conflict: {path}")
        return
    atomic_write(path, data)


def write_private_immutable(path: Path, data: bytes) -> None:
    write_immutable(path, data)
    path.chmod(0o600)


def build_release(
    *,
    corpus_root: Path = CORPUS_ROOT,
    t9_root: Path = T9_VERSION2_ROOT,
    provisional_sources: list[Path] | None = None,
    priority_hint_source: Path | None = T9_PRIORITY_HINT,
) -> Path:
    index_path = corpus_root / "indexes" / "question-index.csv"
    source_manifest_path = corpus_root / "sources" / "source-manifest.csv"
    index_rows = read_csv(index_path)
    source_rows = read_csv(source_manifest_path)
    reconciliation = reconcile_sources(source_rows, t9_root)
    status_counts = Counter(row["status"] for row in reconciliation)
    if status_counts != Counter({"t9_hash_match": 33, "legacy_fallback_registered": 1}):
        raise ReleaseError(f"source reconciliation is not 33 T9 matches + 1 fallback: {dict(status_counts)}")

    answer_table_evidence = build_answer_table_evidence(source_rows)
    asset_bindings, private_capsules, capsule_commitments = prepare_surface_bindings(
        corpus_root,
        index_rows,
        answer_table_evidence,
    )
    exposure, profiles = build_exposure_and_profiles(
        index_rows,
        asset_commitments=asset_bindings,
        capsule_commitments=capsule_commitments,
    )
    exposure_counts = Counter(row["exposure_mode"] for row in exposure)
    if exposure_counts != Counter({"direct_pool": DIRECT_POOL_COUNT, "style_only": STYLE_ONLY_COUNT}):
        raise ReleaseError(f"canonical exposure counts invalid: {dict(exposure_counts)}")
    capsule_status_counts = Counter(value["parser_status"] for value in capsule_commitments.values())
    direct_usable_count = sum(
        row["exposure_mode"] == "direct_pool" and row["production_usable"]
        for row in exposure
    )

    if provisional_sources is None:
        provisional_sources = [T9_PROVISIONAL_2026_ROOT / name for name in PROVISIONAL_SOURCE_NAMES]
    provisional_manifest, provisional_conflicts, provisional_structure = provisional_documents(provisional_sources)

    if priority_hint_source is None or not priority_hint_source.is_file():
        priority_hint = {
            "schema_version": "exam_priority_hint_v1",
            "status": "not_registered",
            "authority": "priority_hint_only",
            "production_usable": False,
            "generation_authority": False,
        }
    else:
        priority_hint = {
            "schema_version": "exam_priority_hint_v1",
            "status": "registered",
            **source_descriptor(priority_hint_source, "priority_hint", priority_hint_only=True),
            "generation_authority": False,
        }

    artifacts: dict[str, bytes] = {
        "source-reconciliation.jsonl": jsonl_bytes(reconciliation),
        "canonical-exposure.jsonl": jsonl_bytes(exposure),
        "style-profiles.jsonl": jsonl_bytes(profiles),
        "style-guide.md": style_guide_bytes(profiles),
        "provisional-2026-source-manifest.json": pretty_json(provisional_manifest),
        "provisional-2026-conflicts.json": pretty_json(provisional_conflicts),
        "provisional-2026-structure-profile.json": pretty_json(provisional_structure),
        "priority-hint.json": pretty_json(priority_hint),
    }
    artifact_records = {
        name: {"sha256": sha256_bytes(data), "bytes": len(data)}
        for name, data in sorted(artifacts.items())
    }
    release_content = {
        "schema_version": "exam_style_release_v1",
        "deep_style_spec_version": "exam_style_deep_spec_v1",
        "canonical_corpus": {
            "years": [2009, 2025],
            "question_count": QUESTION_COUNT,
            "question_index_sha256": sha256_file(index_path),
            "source_manifest_sha256": sha256_file(source_manifest_path),
            "corpus_mutated": False,
        },
        "source_reconciliation": {
            "slot_count": SOURCE_SLOT_COUNT,
            "t9_hash_match_count": 33,
            "legacy_fallback_count": 1,
            "conflict_count": 0,
            "production_usable_count": SOURCE_SLOT_COUNT,
        },
        "canonical_exposure": {
            "direct_pool_count": DIRECT_POOL_COUNT,
            "style_only_count": STYLE_ONLY_COUNT,
            "direct_production_usable_count": direct_usable_count,
            "direct_production_blocked_count": DIRECT_POOL_COUNT - direct_usable_count,
            "answer_capsule_status_counts": dict(sorted(capsule_status_counts.items())),
            "private_sibling_layout": "style-releases-private/{release_id}/answer-capsules",
            "answer_content_in_release": False,
        },
        "provisional_2026": {
            "source_count": 3,
            "review_status": "unreviewed_multi_source",
            "production_usable_question_count": 0,
            "source_set_sha256": provisional_manifest["source_set_sha256"],
        },
        "priority_hint": {
            "status": priority_hint["status"],
            "authority": "priority_hint_only",
            "generation_authority": False,
        },
        "gates": {
            "v2_canonical_exposure": {
                "schema_version": "canonical_question_exposure_gate_v2",
                "status": "pass",
                "requires_verified_release": True,
                "direct_pool_requires_exposure_mode": "direct_pool",
                "style_only_direct_replay_allowed": False,
                "provisional_questions_allowed": False,
            },
            "v3_style_generation": {
                "schema_version": "exam_style_generation_gate_v3",
                "status": "release_ready_provisional_2026_blocked",
                "requires_independent_author": True,
                "requires_independent_verifier": True,
                "requires_source_commitment": True,
                "requires_answer_isolation": True,
                "requires_runtime_freshness": True,
                "release_itself_grants_generation_authority": False,
                "provisional_2026_production_usable": False,
            },
        },
        "artifacts": artifact_records,
    }
    content_sha = sha256_bytes(canonical_json(release_content))
    release_id = f"ESR-{content_sha[:24]}"
    manifest = {
        **release_content,
        "release_id": release_id,
        "content_sha256": content_sha,
    }
    release_dir = corpus_root / "style-releases" / release_id
    release_dir.mkdir(parents=True, exist_ok=True)
    for name, data in artifacts.items():
        write_immutable(release_dir / name, data)
    write_immutable(release_dir / "manifest.json", pretty_json(manifest))
    private_release_dir = corpus_root / "style-releases-private" / release_id
    private_release_dir.mkdir(parents=True, exist_ok=True)
    private_release_dir.chmod(0o700)
    (private_release_dir / "answer-capsules").mkdir(parents=True, exist_ok=True)
    (private_release_dir / "answer-capsules").chmod(0o700)
    for name, data in private_capsules.items():
        write_private_immutable(private_release_dir / name, data)

    provisional_dir = corpus_root / "provisional" / "2026"
    atomic_write(provisional_dir / "source-manifest.json", pretty_json(provisional_manifest))
    atomic_write(provisional_dir / "conflicts.json", pretty_json(provisional_conflicts))
    atomic_write(provisional_dir / "structure-profile.json", pretty_json(provisional_structure))

    failures = audit_release(release_dir)
    if failures:
        raise ReleaseError("release self-audit failed: " + "; ".join(failures))
    return release_dir


def load_jsonl(path: Path) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    with path.open(encoding="utf-8") as handle:
        for line_no, line in enumerate(handle, 1):
            if not line.strip():
                continue
            try:
                value = json.loads(line)
            except json.JSONDecodeError as exc:
                raise ReleaseError(f"invalid JSONL {path}:{line_no}: {exc}") from exc
            if not isinstance(value, dict):
                raise ReleaseError(f"JSONL row is not object: {path}:{line_no}")
            rows.append(value)
    return rows


def audit_release(release_dir: Path) -> list[str]:
    failures: list[str] = []
    manifest_path = release_dir / "manifest.json"
    if not manifest_path.is_file():
        return [f"missing manifest: {manifest_path}"]
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        return [f"invalid manifest: {exc}"]

    if manifest.get("schema_version") != "exam_style_release_v1":
        failures.append("manifest schema_version mismatch")
    content = {key: value for key, value in manifest.items() if key not in {"release_id", "content_sha256"}}
    computed_content_sha = sha256_bytes(canonical_json(content))
    if manifest.get("content_sha256") != computed_content_sha:
        failures.append("manifest content SHA-256 mismatch")
    if manifest.get("release_id") != f"ESR-{computed_content_sha[:24]}":
        failures.append("release ID is not content-addressed")
    if release_dir.name != manifest.get("release_id"):
        failures.append("release directory name does not match release ID")

    artifacts = manifest.get("artifacts")
    if not isinstance(artifacts, dict):
        failures.append("manifest artifacts is not an object")
        artifacts = {}
    for name, record in artifacts.items():
        path = release_dir / name
        if not path.is_file():
            failures.append(f"missing artifact: {name}")
            continue
        data = path.read_bytes()
        if record.get("sha256") != sha256_bytes(data):
            failures.append(f"artifact SHA-256 mismatch: {name}")
        if record.get("bytes") != len(data):
            failures.append(f"artifact byte count mismatch: {name}")

    try:
        reconciliation = load_jsonl(release_dir / "source-reconciliation.jsonl")
        exposure = load_jsonl(release_dir / "canonical-exposure.jsonl")
        profiles = load_jsonl(release_dir / "style-profiles.jsonl")
        provisional_manifest = json.loads((release_dir / "provisional-2026-source-manifest.json").read_text(encoding="utf-8"))
        provisional_conflicts = json.loads((release_dir / "provisional-2026-conflicts.json").read_text(encoding="utf-8"))
        priority_hint = json.loads((release_dir / "priority-hint.json").read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError, ReleaseError) as exc:
        failures.append(str(exc))
        return failures

    if Counter(row.get("status") for row in reconciliation) != Counter({"t9_hash_match": 33, "legacy_fallback_registered": 1}):
        failures.append("source reconciliation is not exactly 33 matches + 1 fallback")
    if len(exposure) != QUESTION_COUNT:
        failures.append(f"canonical exposure rows={len(exposure)}, expected {QUESTION_COUNT}")
    exposure_counts = Counter(row.get("exposure_mode") for row in exposure)
    if exposure_counts != Counter({"direct_pool": DIRECT_POOL_COUNT, "style_only": STYLE_ONLY_COUNT}):
        failures.append(f"canonical exposure counts invalid: {dict(exposure_counts)}")
    if any(row.get("direct_replay_allowed") != (row.get("exposure_mode") == "direct_pool") for row in exposure):
        failures.append("canonical direct replay policy mismatch")
    if any(row.get("answer_content_in_release") is not False for row in exposure):
        failures.append("canonical exposure claims answer content")
    profile_total = sum(int(row.get("question_count", 0)) for row in profiles)
    profile_direct = sum(int(row.get("direct_pool_count", 0)) for row in profiles)
    profile_style = sum(int(row.get("style_only_count", 0)) for row in profiles)
    if (profile_total, profile_direct, profile_style) != (QUESTION_COUNT, DIRECT_POOL_COUNT, STYLE_ONLY_COUNT):
        failures.append("style profile aggregate counts invalid")
    if provisional_manifest.get("source_count") != 3 or provisional_manifest.get("production_usable_question_count") != 0:
        failures.append("2026 provisional manifest gate invalid")
    if provisional_conflicts.get("production_usable") is not False:
        failures.append("2026 provisional conflict state is not fail-closed")
    if priority_hint.get("authority") != "priority_hint_only" or priority_hint.get("generation_authority") is not False:
        failures.append("priority hint exceeds its allowed authority")
    gates = manifest.get("gates", {})
    if gates.get("v2_canonical_exposure", {}).get("status") != "pass":
        failures.append("V2 canonical exposure gate not pass")
    if gates.get("v3_style_generation", {}).get("release_itself_grants_generation_authority") is not False:
        failures.append("V3 gate incorrectly grants generation authority")
    if manifest.get("deep_style_spec_version") == "exam_style_deep_spec_v1":
        corpus_root = release_dir.parent.parent
        private_root = corpus_root / "style-releases-private" / release_dir.name
        required_features = {
            "condition_density",
            "data_representation",
            "legal_parameter_ranges",
            "calculation_scale",
            "polarity",
            "option_parallelism_proxy",
            "reasoning_depth",
            "distractor_mechanism_categories",
            "v2_axes",
            "v3_allowed_single_structural_edges",
        }
        direct_statuses: Counter[str] = Counter()
        direct_usable = 0
        audited_table_evidence: set[str] = set()
        for row in exposure:
            canonical_id = str(row.get("canonical_id") or "")
            if not required_features.issubset(row):
                failures.append(f"deep style features missing: {canonical_id}")
                continue
            if row["condition_density"].get("category") not in CONDITION_DENSITIES:
                failures.append(f"invalid condition density: {canonical_id}")
            if row["calculation_scale"].get("category") not in CALCULATION_SCALES:
                failures.append(f"invalid calculation scale: {canonical_id}")
            if row["polarity"].get("category") not in POLARITIES:
                failures.append(f"invalid polarity: {canonical_id}")
            if row["option_parallelism_proxy"].get("category") not in OPTION_PARALLELISM:
                failures.append(f"invalid option parallelism: {canonical_id}")
            if row["reasoning_depth"].get("category") not in REASONING_DEPTHS:
                failures.append(f"invalid reasoning depth: {canonical_id}")
            parameter_ranges = row["legal_parameter_ranges"]
            if parameter_ranges.get("raw_values_stored") is not False:
                failures.append(f"legal parameter ranges store raw values: {canonical_id}")
            for parameter in parameter_ranges.get("categories", []):
                if any(key in parameter for key in ("value", "min", "max", "example", "raw")):
                    failures.append(f"legal parameter entry contains raw-value field: {canonical_id}")
            for feature_name in required_features - {"v2_axes", "v3_allowed_single_structural_edges"}:
                value = row[feature_name]
                if isinstance(value, dict) and "confidence" in value:
                    if value.get("confidence") not in CONFIDENCE_LEVELS or not value.get("source"):
                        failures.append(f"feature confidence/source invalid: {canonical_id}:{feature_name}")
            if not row["v2_axes"].get("invariant_axes") or not row["v2_axes"].get("mutable_surface_axes"):
                failures.append(f"V2 axes missing: {canonical_id}")
            if any(edge.get("max_changed_structural_axes") != 1 for edge in row["v3_allowed_single_structural_edges"]):
                failures.append(f"V3 edge changes more than one structural axis: {canonical_id}")
            assets = row.get("question_asset_refs") or []
            if not assets:
                failures.append(f"question asset commitment missing: {canonical_id}")
            elif not isinstance(assets, list) or any(not isinstance(asset, dict) for asset in assets):
                failures.append(f"question asset commitment invalid: {canonical_id}")
                assets = []
            else:
                try:
                    validate_preprocessed_asset_commitments(corpus_root, canonical_id, assets)
                except ReleaseError as exc:
                    failures.append(str(exc))
                try:
                    validate_source_override_commitments(corpus_root, canonical_id, assets)
                except ReleaseError as exc:
                    failures.append(str(exc))
            for asset in assets:
                try:
                    asset_path = resolve_corpus_asset(corpus_root, asset["ref"])
                except (KeyError, ReleaseError) as exc:
                    failures.append(str(exc))
                    continue
                if sha256_file(asset_path) != asset.get("sha256") or asset_path.stat().st_size != asset.get("size_bytes"):
                    failures.append(f"question asset commitment drift: {canonical_id}")
                locator = asset.get("page_locator") or {}
                if not isinstance(locator.get("asset_ordinal"), int):
                    failures.append(f"question asset page locator invalid: {canonical_id}")
            if row.get("exposure_mode") == "direct_pool":
                ref = str(row.get("answer_capsule_commitment_ref") or "")
                expected_prefix = "private-sibling://answer-capsules/"
                if not ref.startswith(expected_prefix):
                    failures.append(f"private answer capsule ref invalid: {canonical_id}")
                    continue
                capsule_path = private_root / ref.removeprefix("private-sibling://")
                if not capsule_path.is_file() or capsule_path.is_symlink():
                    failures.append(f"private answer capsule missing: {canonical_id}")
                    continue
                capsule_bytes = capsule_path.read_bytes()
                if sha256_bytes(capsule_bytes) != row.get("answer_capsule_commitment_sha256"):
                    failures.append(f"private answer capsule commitment drift: {canonical_id}")
                    continue
                if capsule_path.stat().st_mode & 0o777 != 0o600:
                    failures.append(f"private answer capsule mode invalid: {canonical_id}")
                try:
                    capsule = json.loads(capsule_bytes)
                except json.JSONDecodeError:
                    failures.append(f"private answer capsule invalid JSON: {canonical_id}")
                    continue
                parser_status = capsule.get("parser_status")
                direct_statuses[parser_status] += 1
                table_ref = str(capsule.get("answer_table_evidence_ref") or "")
                if not table_ref.startswith("private-sibling://answer-table-evidence/"):
                    failures.append(f"private answer-table evidence ref invalid: {canonical_id}")
                else:
                    table_path = private_root / table_ref.removeprefix("private-sibling://")
                    if not table_path.is_file() or table_path.is_symlink():
                        failures.append(f"private answer-table evidence missing: {canonical_id}")
                    elif sha256_file(table_path) != capsule.get("answer_table_evidence_sha256"):
                        failures.append(f"private answer-table evidence commitment drift: {canonical_id}")
                    elif table_path.stat().st_mode & 0o777 != 0o600:
                        failures.append(f"private answer-table evidence mode invalid: {canonical_id}")
                    elif table_ref not in audited_table_evidence:
                        audited_table_evidence.add(table_ref)
                        try:
                            table_evidence = json.loads(table_path.read_text(encoding="utf-8"))
                        except json.JSONDecodeError:
                            failures.append(f"private answer-table evidence invalid JSON: {canonical_id}")
                        else:
                            candidates = table_evidence.get("candidate_answers") or {}
                            if table_evidence.get("candidate_count") != len(candidates):
                                failures.append(f"answer-table candidate count mismatch: {canonical_id}")
                            if any(answer not in {"A", "B", "C", "D"} for answer in candidates.values()):
                                failures.append(f"answer-table candidate token invalid: {canonical_id}")
                            if table_evidence.get("structurally_complete") is True:
                                if len(candidates) != 40 or table_evidence.get("render_dpi") != 350:
                                    failures.append(f"complete answer-table structure invalid: {canonical_id}")
                                bbox = table_evidence.get("source_page_bbox_pixels")
                                if not isinstance(bbox, list) or len(bbox) != 4:
                                    failures.append(f"complete answer-table bbox invalid: {canonical_id}")
                                if not SHA256_RE.fullmatch(str(table_evidence.get("render_sha256") or "")):
                                    failures.append(f"complete answer-table render hash invalid: {canonical_id}")
                                if not SHA256_RE.fullmatch(str(table_evidence.get("tsv_sha256") or "")):
                                    failures.append(f"complete answer-table TSV hash invalid: {canonical_id}")
                if parser_status == "resolved":
                    if capsule.get("correct_option") not in {"A", "B", "C", "D"}:
                        failures.append(f"resolved private answer capsule has invalid option: {canonical_id}")
                    direct_usable += int(bool(assets))
                elif parser_status not in {"missing", "ambiguous", "conflict"} or capsule.get("correct_option") is not None:
                    failures.append(f"unresolved private answer capsule invalid: {canonical_id}")
                if row.get("production_usable") != (parser_status == "resolved" and bool(assets)):
                    failures.append(f"direct production usable gate mismatch: {canonical_id}")
            elif row.get("answer_capsule_commitment_ref") is not None or row.get("answer_capsule_commitment_sha256") is not None:
                failures.append(f"style-only row exposes answer capsule commitment: {canonical_id}")
        exposure_manifest = manifest.get("canonical_exposure", {})
        if exposure_manifest.get("direct_production_usable_count") != direct_usable:
            failures.append("direct production usable manifest count mismatch")
        if exposure_manifest.get("answer_capsule_status_counts") != dict(sorted(direct_statuses.items())):
            failures.append("answer capsule status manifest count mismatch")
        profile_required = required_features | {"inference_quality"}
        for profile in profiles:
            if not profile_required.issubset(profile):
                failures.append(f"deep profile fields missing: {profile.get('profile_id')}")
    return failures


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)
    build_parser = subparsers.add_parser("build", help="build and self-audit a content-addressed release")
    build_parser.add_argument("--corpus-root", type=Path, default=CORPUS_ROOT)
    build_parser.add_argument("--t9-root", type=Path, default=T9_VERSION2_ROOT)
    build_parser.add_argument("--provisional-source", type=Path, action="append", default=None)
    build_parser.add_argument("--priority-hint-source", type=Path, default=T9_PRIORITY_HINT)
    audit_parser = subparsers.add_parser("audit", help="audit an existing release without rewriting it")
    audit_parser.add_argument("--release", type=Path, required=True)
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv or sys.argv[1:])
    try:
        if args.command == "build":
            release_dir = build_release(
                corpus_root=args.corpus_root,
                t9_root=args.t9_root,
                provisional_sources=args.provisional_source,
                priority_hint_source=args.priority_hint_source,
            )
            print(json.dumps({"status": "PASS", "release": str(release_dir)}, ensure_ascii=False))
            return 0
        failures = audit_release(args.release)
        print(json.dumps({"status": "FAIL" if failures else "PASS", "release": str(args.release), "failures": failures}, ensure_ascii=False))
        return 1 if failures else 0
    except ReleaseError as exc:
        print(json.dumps({"status": "FAIL", "error": str(exc)}, ensure_ascii=False), file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
