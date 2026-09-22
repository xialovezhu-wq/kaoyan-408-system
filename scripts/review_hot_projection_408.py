#!/usr/bin/env python3
"""Build and query a hash-bound hot projection of three formal review tables.

The cold builder is the only path that reads the formal Markdown tables.  It
publishes small content-addressed review-unit and formal-node objects, a bounded
exact-identity index, a content-addressed manifest, and finally an atomic root
pointer.  The hot query reads exactly the pointer, manifest, index, and one
object.  It never reads a formal table or a review/capture ledger.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import stat
import sys
import tempfile
import time
from datetime import date, timedelta
from pathlib import Path
from typing import Any

from intake_lib_408 import scan_leak


SCHEMA = "review_hot_projection_v1"
POINTER_SCHEMA = "review_hot_projection_pointer_v1"
DEFAULT_OUTPUT_RELATIVE = Path(
    "wiki/study_vaults/408-full/state/review-hot-projection"
)
SOURCE_FILES = (
    "复习单元节点映射.md",
    "复习单元总表.md",
    "原题复做轨总表.md",
)
DEFAULT_MAX_BYTES = 12_288
MIN_MAX_BYTES = 1_024
MAX_MAX_BYTES = 65_536
MAX_OBJECT_BYTES = 9_216
MAX_POSTANSWER_FEEDBACK_BYTES = 4_096
MAX_POINTER_INPUT_BYTES = 16 * 1024
MAX_MANIFEST_INPUT_BYTES = 128 * 1024
MAX_INDEX_INPUT_BYTES = 8 * 1024 * 1024
PROJECTION_COUNT_KEYS = (
    "review_unit_count",
    "postanswer_feedback_count",
    "formal_node_count",
    "question_alias_count",
    "ambiguous_question_count",
    "object_count",
)
VALID_TRACK_ELIGIBILITY = {
    "eligible",
    "eligible_unrecorded_date",
    "protected_until",
}

FORMAL_ID_RE = re.compile(r"^(DS|CO|OS|CN)_(?:\d{4}|UNK)_\d{3}$")
RU_ID_RE = re.compile(r"^RU_[A-Za-z0-9_.:-]{1,156}$")
SAFE_ID_RE = re.compile(r"^[A-Za-z0-9_.:@/-]{1,180}$")
SHA_RE = re.compile(r"^[0-9a-f]{64}$")
BUILD_ID_RE = re.compile(r"^RHP-[0-9A-F]{24}$")
DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
PLACEHOLDERS = {
    "",
    "无",
    "未记录",
    "待补充",
    "待确认",
    "VISUAL_PENDING",
    "UNKNOWN",
    "N/A",
}
POSTANSWER_PROTECTED_RE = re.compile(
    r"完整题干|完整解析|标准答案|正确答案\s*[:：]|"
    r"选项\s*[A-HＡ-Ｈ](?![A-Za-z])|答案字母",
    re.IGNORECASE,
)

REVIEW_REQUIRED = {
    "复习单元ID",
    "科目",
    "模块",
    "内容名称",
    "类型",
    "调度状态",
    "覆盖节点",
    "首次学习日期",
    "上次复习日期",
    "下次复习日期",
    "当前间隔天数",
    "掌握度",
    "错误次数",
    "重要程度",
    "难度等级",
}
MAPPING_REQUIRED = {
    "正式节点ID",
    "实际考点",
    "主复习单元ID",
    "次复习单元ID",
    "覆盖方式",
    "复核状态",
}
TRACK_REQUIRED = {
    "正式节点ID",
    "主复习单元ID",
    "原题轨状态",
    "7天资格",
    "最近复做日期",
    "最早允许日期",
    "下次原题复做日期",
    "来源ID",
    "年份",
    "定位标记",
    "段落号",
    "完整来源位置",
    "详情入口",
    "激活条件",
}


class ProjectionError(RuntimeError):
    """Stable, answer-safe projection failure."""


def _json_bytes(value: Any, *, pretty: bool = False) -> bytes:
    options: dict[str, Any] = {"ensure_ascii": False, "sort_keys": True}
    if pretty:
        options["indent"] = 2
    else:
        options["separators"] = (",", ":")
    return (json.dumps(value, **options) + "\n").encode("utf-8")


def _sha256_bytes(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


def _sha256_file(path: Path) -> str:
    return _sha256_bytes(path.read_bytes())


def _atomic_write_bytes(path: Path, raw: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(raw)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def _write_content(path: Path, raw: bytes, digest: str) -> bool:
    """Write immutable content if absent; return True only for a new file."""

    if path.exists():
        if path.is_symlink() or _sha256_file(path) != digest:
            raise ProjectionError("content_address_conflict")
        return False
    _atomic_write_bytes(path, raw)
    if _sha256_file(path) != digest:
        raise ProjectionError("content_publish_hash_mismatch")
    return True


def _content_ref(kind: str, digest: str) -> str:
    if kind not in {"objects", "indexes", "manifests"} or not SHA_RE.fullmatch(digest):
        raise ProjectionError("invalid_content_reference")
    return f"{kind}/{digest}.json"


def _validate_ref(value: Any, kind: str, digest: Any) -> str:
    if not isinstance(digest, str) or not SHA_RE.fullmatch(digest):
        raise ProjectionError(f"invalid_{kind}_hash")
    expected = _content_ref(kind, digest)
    if value != expected:
        raise ProjectionError(f"invalid_{kind}_reference")
    return expected


def _split_row(line: str) -> list[str]:
    stripped = line.strip()
    if not (stripped.startswith("|") and stripped.endswith("|")):
        return []
    return [cell.strip() for cell in stripped[1:-1].split("|")]


def _separator(cells: list[str]) -> bool:
    return bool(cells) and all(re.fullmatch(r":?-{2,}:?", cell) for cell in cells)


def _read_table(path: Path, required: set[str]) -> tuple[list[str], list[dict[str, str]], str]:
    try:
        raw = path.read_bytes()
        text = raw.decode("utf-8")
    except FileNotFoundError as exc:
        raise ProjectionError(f"missing_source_table:{path.name}") from exc
    except UnicodeDecodeError as exc:
        raise ProjectionError(f"invalid_source_encoding:{path.name}") from exc
    lines = text.splitlines()
    header_index: int | None = None
    headers: list[str] = []
    for index, line in enumerate(lines):
        cells = _split_row(line)
        if required <= set(cells):
            header_index = index
            headers = cells
            break
    if header_index is None or header_index + 1 >= len(lines):
        raise ProjectionError(f"source_table_header_missing:{path.name}")
    if len(set(headers)) != len(headers) or not _separator(_split_row(lines[header_index + 1])):
        raise ProjectionError(f"source_table_header_invalid:{path.name}")
    rows: list[dict[str, str]] = []
    started = False
    for line in lines[header_index + 2 :]:
        cells = _split_row(line)
        if not cells:
            if started:
                break
            continue
        started = True
        if len(cells) != len(headers):
            raise ProjectionError(f"source_table_row_malformed:{path.name}")
        rows.append(dict(zip(headers, cells)))
    return headers, rows, _sha256_bytes(raw)


def _safe_text(value: Any, *, maximum: int = 240, essential: bool = False) -> str | None:
    text = " ".join(str(value or "").split())[:maximum]
    if not text or text in PLACEHOLDERS:
        if essential:
            raise ProjectionError("required_safe_text_missing")
        return None
    fails, warns = scan_leak(text)
    if fails or warns:
        if essential:
            raise ProjectionError("protected_content_in_required_field")
        return None
    return text


def _safe_date(value: Any) -> str | None:
    text = str(value or "").strip()
    if text in PLACEHOLDERS:
        return None
    if not DATE_RE.fullmatch(text):
        raise ProjectionError("invalid_formal_date")
    try:
        return date.fromisoformat(text).isoformat()
    except ValueError as exc:
        raise ProjectionError("invalid_formal_date") from exc


def _safe_nonnegative_int(value: Any, *, nullable: bool = True) -> int | None:
    text = str(value or "").strip()
    if text in PLACEHOLDERS:
        return None if nullable else 0
    if not re.fullmatch(r"\d+", text):
        raise ProjectionError("invalid_nonnegative_integer")
    return int(text)


def _secondary_units(value: Any) -> list[str]:
    text = str(value or "").strip()
    if text in PLACEHOLDERS:
        return []
    result: list[str] = []
    for item in re.split(r"[；;、,，\s]+", text):
        if not item:
            continue
        if not RU_ID_RE.fullmatch(item):
            raise ProjectionError("invalid_secondary_review_unit_id")
        if item not in result:
            result.append(item)
    if len(result) > 2:
        raise ProjectionError("too_many_secondary_review_units")
    return result


def _covered_nodes(value: Any) -> list[str]:
    return sorted(set(re.findall(r"(?:DS|CO|OS|CN)_(?:\d{4}|UNK)_\d{3}", str(value or ""))))


def _specific_question_id(value: Any) -> str | None:
    text = str(value or "").strip()
    if text in PLACEHOLDERS or FORMAL_ID_RE.fullmatch(text):
        return None
    return text if SAFE_ID_RE.fullmatch(text) else None


def _schedule_view(row: dict[str, str]) -> dict[str, Any]:
    return {
        "subject": _safe_text(row.get("科目"), maximum=40, essential=True),
        "module": _safe_text(row.get("模块"), maximum=80, essential=True),
        "content_name": _safe_text(row.get("内容名称"), maximum=160, essential=True),
        "unit_type": _safe_text(row.get("类型"), maximum=80, essential=True),
        "schedule_status": _safe_text(row.get("调度状态"), maximum=100, essential=True),
        "first_study_date": _safe_date(row.get("首次学习日期")),
        "last_review_date": _safe_date(row.get("上次复习日期")),
        "next_review_date": _safe_date(row.get("下次复习日期")),
        "interval_days": _safe_nonnegative_int(row.get("当前间隔天数"), nullable=False),
        "mastery_user_value": _safe_text(row.get("掌握度"), maximum=40),
        "error_count": _safe_nonnegative_int(row.get("错误次数")),
        "importance": _safe_text(row.get("重要程度"), maximum=40),
        "difficulty": _safe_text(row.get("难度等级"), maximum=40),
    }


def _postanswer_feedback(row: dict[str, str]) -> str:
    text = " ".join(str(row.get("派生自查口径") or "").split())
    if not text or text in PLACEHOLDERS:
        raise ProjectionError("postanswer_feedback_missing")
    raw = text.encode("utf-8")
    if len(raw) > MAX_POSTANSWER_FEEDBACK_BYTES:
        raise ProjectionError("postanswer_feedback_exceeds_bound")
    if POSTANSWER_PROTECTED_RE.search(text):
        raise ProjectionError("protected_question_content_in_postanswer_feedback")
    return text


def _answer_safe(value: dict[str, Any]) -> None:
    serialized = json.dumps(value, ensure_ascii=False, sort_keys=True)
    fails, warns = scan_leak(serialized)
    if fails or warns:
        raise ProjectionError("answer_safety_gate_failed")


def _build_payloads(repo: Path, *, as_of: str) -> tuple[dict[str, bytes], dict[str, Any], dict[str, Any]]:
    try:
        as_of_date = date.fromisoformat(as_of)
        as_of = as_of_date.isoformat()
    except ValueError as exc:
        raise ProjectionError("invalid_as_of") from exc
    _, mapping_rows, mapping_sha = _read_table(
        repo / SOURCE_FILES[0], MAPPING_REQUIRED
    )
    _, unit_rows, unit_sha = _read_table(repo / SOURCE_FILES[1], REVIEW_REQUIRED)
    _, track_rows, track_sha = _read_table(repo / SOURCE_FILES[2], TRACK_REQUIRED)

    units: dict[str, dict[str, Any]] = {}
    for row in unit_rows:
        unit_id = str(row.get("复习单元ID") or "").strip()
        if not RU_ID_RE.fullmatch(unit_id) or unit_id in units:
            raise ProjectionError("invalid_or_duplicate_review_unit")
        units[unit_id] = {
            "row": row,
            "schedule": _schedule_view(row),
            "postanswer_feedback": _postanswer_feedback(row),
            "covered_nodes": _covered_nodes(row.get("覆盖节点")),
            "primary_nodes": [],
            "secondary_nodes": [],
        }

    mappings: dict[str, dict[str, Any]] = {}
    for row in mapping_rows:
        formal_id = str(row.get("正式节点ID") or "").strip()
        primary = str(row.get("主复习单元ID") or "").strip()
        if not FORMAL_ID_RE.fullmatch(formal_id) or formal_id in mappings:
            raise ProjectionError("invalid_or_duplicate_formal_mapping")
        if not RU_ID_RE.fullmatch(primary) or primary not in units:
            raise ProjectionError("primary_review_unit_missing")
        secondaries = _secondary_units(row.get("次复习单元ID"))
        if primary in secondaries or any(item not in units for item in secondaries):
            raise ProjectionError("secondary_review_unit_missing_or_duplicate")
        mapping = {
            "formal_node_id": formal_id,
            "actual_point": _safe_text(row.get("实际考点"), maximum=180, essential=True),
            "primary_review_unit_id": primary,
            "secondary_review_unit_ids": secondaries,
            "coverage_mode": _safe_text(row.get("覆盖方式"), maximum=100, essential=True),
            "review_status": _safe_text(row.get("复核状态"), maximum=60, essential=True),
            "subject": _safe_text(row.get("科目"), maximum=40),
            "evidence_grade": _safe_text(row.get("证据等级"), maximum=60),
            "mapping_gaps": _safe_text(row.get("待补事项"), maximum=180),
        }
        mappings[formal_id] = mapping
        units[primary]["primary_nodes"].append(formal_id)
        for secondary in secondaries:
            units[secondary]["secondary_nodes"].append(formal_id)

    tracks: dict[str, dict[str, Any]] = {}
    redacted_optional_field_count = 0
    matured_eligibility_normalization_count = 0
    for row in track_rows:
        formal_id = str(row.get("正式节点ID") or "").strip()
        primary = str(row.get("主复习单元ID") or "").strip()
        if not FORMAL_ID_RE.fullmatch(formal_id) or formal_id in tracks:
            raise ProjectionError("invalid_or_duplicate_original_track")
        if formal_id not in mappings or primary != mappings[formal_id]["primary_review_unit_id"]:
            raise ProjectionError("original_track_mapping_mismatch")
        source_id_raw = str(row.get("来源ID") or "").strip()
        source_id = source_id_raw if SAFE_ID_RE.fullmatch(source_id_raw) else None
        optional_fields: dict[str, Any] = {}
        for key, column, maximum in (
            ("locator", "完整来源位置", 360),
            ("detail_entry", "详情入口", 360),
            ("locator_mark", "定位标记", 120),
            ("paragraph", "段落号", 80),
            ("activation_condition", "激活条件", 180),
            ("track_gaps", "待补事项", 180),
        ):
            raw_value = row.get(column)
            safe_value = _safe_text(raw_value, maximum=maximum)
            if str(raw_value or "").strip() not in PLACEHOLDERS and safe_value is None:
                redacted_optional_field_count += 1
            optional_fields[key] = safe_value
        source_eligibility = _safe_text(
            row.get("7天资格"), maximum=80, essential=True
        )
        if source_eligibility not in VALID_TRACK_ELIGIBILITY:
            raise ProjectionError("invalid_original_track_eligibility")
        eligibility = source_eligibility
        last_attempt_date = _safe_date(row.get("最近复做日期"))
        earliest_allowed_date = _safe_date(row.get("最早允许日期"))
        next_original_date = _safe_date(row.get("下次原题复做日期"))
        if last_attempt_date is None:
            if (
                earliest_allowed_date is not None
                or source_eligibility != "eligible_unrecorded_date"
            ):
                raise ProjectionError("inconsistent_unrecorded_original_track_gate")
        else:
            last_date = date.fromisoformat(last_attempt_date)
            if last_date > as_of_date:
                raise ProjectionError("original_track_last_attempt_after_as_of")
            expected_earliest = last_date + timedelta(days=7)
            if (
                earliest_allowed_date is None
                or date.fromisoformat(earliest_allowed_date) != expected_earliest
            ):
                raise ProjectionError("inconsistent_original_track_earliest_date")
            expected_eligibility = (
                "protected_until"
                if as_of_date < expected_earliest
                else "eligible"
            )
            if source_eligibility == "eligible_unrecorded_date":
                raise ProjectionError("inconsistent_original_track_eligibility")
            if (
                expected_eligibility == "protected_until"
                and source_eligibility != "protected_until"
            ):
                raise ProjectionError("inconsistent_original_track_eligibility")
            # A stored protected row naturally matures when the cold projection
            # is rebuilt on or after its earliest date.  Normalize only in the
            # derived object; never mutate the formal table merely because time
            # passed.
            eligibility = expected_eligibility
            if eligibility != source_eligibility:
                matured_eligibility_normalization_count += 1
        if (
            eligibility == "protected_until"
            and next_original_date is not None
            and earliest_allowed_date is not None
            and next_original_date < earliest_allowed_date
        ):
            raise ProjectionError("original_track_next_date_before_gate")
        tracks[formal_id] = {
            "formal_node_id": formal_id,
            "track_status": _safe_text(row.get("原题轨状态"), maximum=100, essential=True),
            "seven_day_eligibility": eligibility,
            "source_seven_day_eligibility": source_eligibility,
            "last_attempt_date": last_attempt_date,
            "earliest_allowed_date": earliest_allowed_date,
            "next_original_date": next_original_date,
            "source_id": source_id,
            "question_id": _specific_question_id(source_id),
            "year": _safe_text(row.get("年份"), maximum=20),
            **optional_fields,
        }

    if set(mappings) != set(tracks):
        raise ProjectionError("mapping_original_track_cardinality_mismatch")
    for unit_id, unit in units.items():
        covered = set(unit["covered_nodes"])
        assigned = set(unit["primary_nodes"]) | set(unit["secondary_nodes"])
        if covered != assigned:
            raise ProjectionError(f"review_unit_coverage_mismatch:{unit_id}")

    question_formals: dict[str, set[str]] = {}
    for formal_id, track in tracks.items():
        question_id = track.get("question_id")
        if question_id:
            question_formals.setdefault(str(question_id), set()).add(formal_id)
    question_alias_count = sum(
        1 for formal_ids in question_formals.values() if len(formal_ids) == 1
    )
    ambiguous_question_count = sum(
        1 for formal_ids in question_formals.values() if len(formal_ids) > 1
    )
    projection_counts = {
        "review_unit_count": len(units),
        "postanswer_feedback_count": len(units),
        "formal_node_count": len(mappings),
        "question_alias_count": question_alias_count,
        "ambiguous_question_count": ambiguous_question_count,
        "object_count": 2 * len(units) + len(mappings),
    }
    object_identities = sorted(
        [f"review_unit:{unit_id}" for unit_id in units]
        + [f"postanswer:{unit_id}" for unit_id in units]
        + [f"formal_node:{formal_id}" for formal_id in mappings]
    )
    object_set_sha = _sha256_bytes(
        _json_bytes(
            {
                "schema": "review_hot_object_identity_set_v1",
                "object_identities": object_identities,
                "projection_counts": projection_counts,
            }
        )
    )
    build_id = "RHP-" + _sha256_bytes(
        _json_bytes(
            {
                "schema": "review_hot_projection_build_identity_v1",
                "as_of": as_of,
                "source_table_sha256": {
                    SOURCE_FILES[0]: mapping_sha,
                    SOURCE_FILES[1]: unit_sha,
                    SOURCE_FILES[2]: track_sha,
                },
                "source_row_counts": {
                    SOURCE_FILES[0]: len(mapping_rows),
                    SOURCE_FILES[1]: len(unit_rows),
                    SOURCE_FILES[2]: len(track_rows),
                },
                "object_set_sha256": object_set_sha,
                "projection_counts": projection_counts,
            }
        )
    )[:24].upper()

    object_payloads: dict[str, bytes] = {}
    by_review_unit: dict[str, dict[str, str]] = {}
    postanswer_by_review_unit: dict[str, dict[str, str]] = {}
    by_formal: dict[str, dict[str, str]] = {}
    question_refs: dict[str, list[dict[str, str]]] = {}
    for unit_id, unit in sorted(units.items()):
        # Keep exactly the small original-track fields needed by the outcome
        # scheduler beside the RU object. This avoids a hot-path fan-out over
        # the formal track table or one object per covered node.
        primary_original_tracks = [
            {
                "formal_node_id": formal_id,
                "track_status": tracks[formal_id]["track_status"],
                "seven_day_eligibility": tracks[formal_id]["seven_day_eligibility"],
                "last_attempt_date": tracks[formal_id]["last_attempt_date"],
                "earliest_allowed_date": tracks[formal_id]["earliest_allowed_date"],
                "next_original_date": tracks[formal_id]["next_original_date"],
                "source_id": tracks[formal_id]["source_id"],
                "question_id": tracks[formal_id]["question_id"],
            }
            for formal_id in sorted(unit["primary_nodes"])
        ]
        payload = {
            "schema": SCHEMA,
            "kind": "review_hot_unit_object",
            "build_id": build_id,
            "object_set_sha256": object_set_sha,
            "projection_counts": projection_counts,
            "review_unit_id": unit_id,
            "schedule": unit["schedule"],
            "primary_formal_node_ids": sorted(unit["primary_nodes"]),
            "secondary_formal_node_ids": sorted(unit["secondary_nodes"]),
            "covered_formal_node_ids": sorted(unit["covered_nodes"]),
            "primary_original_tracks": primary_original_tracks,
            "source_fields_excluded": ["闭卷问题", "派生自查口径", "备注"],
            "answer_safe": True,
            "formal_write_count": 0,
        }
        _answer_safe(payload)
        raw = _json_bytes(payload, pretty=True)
        if len(raw) > MAX_OBJECT_BYTES:
            raise ProjectionError(f"unit_object_exceeds_hot_budget:{unit_id}")
        digest = _sha256_bytes(raw)
        ref = _content_ref("objects", digest)
        object_payloads[ref] = raw
        by_review_unit[unit_id] = {"object_ref": ref, "object_sha256": digest}

        # Feedback is deliberately published as a different object that has no
        # reference in any pre-answer result.  Only the receipt-gated adapter
        # may resolve this post-answer namespace.
        postanswer_payload = {
            "schema": SCHEMA,
            "kind": "review_hot_postanswer_feedback_object",
            "build_id": build_id,
            "object_set_sha256": object_set_sha,
            "projection_counts": projection_counts,
            "review_unit_id": unit_id,
            "feedback": unit["postanswer_feedback"],
            "exposure": "postanswer_only_after_verified_first_answer",
            "answer_safe": True,
            "answer_safe_question_surface": True,
            "formal_write_count": 0,
        }
        postanswer_raw = _json_bytes(postanswer_payload, pretty=True)
        if len(postanswer_raw) > MAX_OBJECT_BYTES:
            raise ProjectionError(
                f"postanswer_object_exceeds_hot_budget:{unit_id}"
            )
        postanswer_digest = _sha256_bytes(postanswer_raw)
        postanswer_ref = _content_ref("objects", postanswer_digest)
        object_payloads[postanswer_ref] = postanswer_raw
        postanswer_by_review_unit[unit_id] = {
            "object_ref": postanswer_ref,
            "object_sha256": postanswer_digest,
        }

    for formal_id, mapping in sorted(mappings.items()):
        primary = mapping["primary_review_unit_id"]
        payload = {
            "schema": SCHEMA,
            "kind": "review_hot_formal_object",
            "build_id": build_id,
            "object_set_sha256": object_set_sha,
            "projection_counts": projection_counts,
            "formal_node_id": formal_id,
            "mapping": mapping,
            "primary_review_unit": {
                "review_unit_id": primary,
                **units[primary]["schedule"],
            },
            "original_track": tracks[formal_id],
            "source_fields_excluded": ["闭卷问题", "派生自查口径", "备注"],
            "answer_safe": True,
            "formal_write_count": 0,
        }
        _answer_safe(payload)
        raw = _json_bytes(payload, pretty=True)
        if len(raw) > MAX_OBJECT_BYTES:
            raise ProjectionError(f"formal_object_exceeds_hot_budget:{formal_id}")
        digest = _sha256_bytes(raw)
        ref = _content_ref("objects", digest)
        object_payloads[ref] = raw
        entry = {"object_ref": ref, "object_sha256": digest}
        by_formal[formal_id] = entry
        question_id = tracks[formal_id].get("question_id")
        if question_id:
            question_refs.setdefault(question_id, []).append(entry)

    by_question: dict[str, dict[str, str]] = {}
    ambiguous_questions: dict[str, int] = {}
    for question_id, entries in sorted(question_refs.items()):
        unique = {
            (entry["object_ref"], entry["object_sha256"]): entry for entry in entries
        }
        if len(unique) == 1:
            by_question[question_id] = next(iter(unique.values()))
        else:
            ambiguous_questions[question_id] = len(unique)

    index = {
        "schema": SCHEMA,
        "kind": "review_hot_exact_index",
        "build_id": build_id,
        "as_of": as_of,
        "by_review_unit_id": by_review_unit,
        "postanswer_by_review_unit_id": postanswer_by_review_unit,
        "by_formal_node_id": by_formal,
        "by_question_id": by_question,
        "ambiguous_question_ids": ambiguous_questions,
        "review_unit_count": projection_counts["review_unit_count"],
        "postanswer_feedback_count": projection_counts[
            "postanswer_feedback_count"
        ],
        "formal_node_count": projection_counts["formal_node_count"],
        "question_alias_count": projection_counts["question_alias_count"],
        "ambiguous_question_count": projection_counts[
            "ambiguous_question_count"
        ],
        "object_count": projection_counts["object_count"],
        "object_set_sha256": object_set_sha,
        "projection_counts": projection_counts,
        "answer_safe": True,
        "formal_write_count": 0,
    }
    _answer_safe(index)
    index_raw = _json_bytes(index, pretty=True)
    index_sha = _sha256_bytes(index_raw)
    index_ref = _content_ref("indexes", index_sha)
    manifest = {
        "schema": SCHEMA,
        "kind": "review_hot_content_manifest",
        "build_id": build_id,
        "as_of": as_of,
        "source_table_sha256": {
            SOURCE_FILES[0]: mapping_sha,
            SOURCE_FILES[1]: unit_sha,
            SOURCE_FILES[2]: track_sha,
        },
        "source_row_counts": {
            SOURCE_FILES[0]: len(mapping_rows),
            SOURCE_FILES[1]: len(unit_rows),
            SOURCE_FILES[2]: len(track_rows),
        },
        "index_ref": index_ref,
        "index_sha256": index_sha,
        "object_set_sha256": object_set_sha,
        "review_unit_count": projection_counts["review_unit_count"],
        "postanswer_feedback_count": projection_counts[
            "postanswer_feedback_count"
        ],
        "formal_node_count": projection_counts["formal_node_count"],
        "question_alias_count": projection_counts["question_alias_count"],
        "ambiguous_question_count": projection_counts[
            "ambiguous_question_count"
        ],
        "object_count": projection_counts["object_count"],
        "projection_counts": projection_counts,
        "redacted_optional_field_count": redacted_optional_field_count,
        "matured_eligibility_normalization_count": (
            matured_eligibility_normalization_count
        ),
        "answer_safe": True,
        "formal_write_count": 0,
    }
    _answer_safe(manifest)
    return object_payloads, index, manifest


def build_projection(
    repo: str | Path,
    *,
    output_dir: str | Path | None = None,
    as_of: str,
) -> dict[str, Any]:
    """Cold-build and atomically activate a new projection generation."""

    started = time.perf_counter_ns()
    root = Path(repo).resolve()
    output = (
        Path(output_dir).resolve()
        if output_dir is not None
        else (root / DEFAULT_OUTPUT_RELATIVE).resolve()
    )
    object_payloads, index, manifest = _build_payloads(root, as_of=as_of)
    new_file_count = 0
    for ref, raw in sorted(object_payloads.items()):
        digest = Path(ref).stem
        if _write_content(output / ref, raw, digest):
            new_file_count += 1
    index_raw = _json_bytes(index, pretty=True)
    index_sha = _sha256_bytes(index_raw)
    index_ref = _content_ref("indexes", index_sha)
    if _write_content(output / index_ref, index_raw, index_sha):
        new_file_count += 1
    if manifest.get("index_sha256") != index_sha or manifest.get("index_ref") != index_ref:
        raise ProjectionError("manifest_index_binding_internal_error")
    manifest_raw = _json_bytes(manifest, pretty=True)
    manifest_sha = _sha256_bytes(manifest_raw)
    manifest_ref = _content_ref("manifests", manifest_sha)
    if _write_content(output / manifest_ref, manifest_raw, manifest_sha):
        new_file_count += 1
    pointer = {
        "schema": POINTER_SCHEMA,
        "kind": "review_hot_active_manifest_pointer",
        "manifest_ref": manifest_ref,
        "manifest_sha256": manifest_sha,
        "build_id": manifest["build_id"],
        "object_set_sha256": manifest["object_set_sha256"],
        "projection_counts": manifest["projection_counts"],
        "answer_safe": True,
        "formal_write_count": 0,
    }
    # The pointer is the sole mutable publication surface and is written last.
    _atomic_write_bytes(output / "manifest.json", _json_bytes(pointer, pretty=True))
    elapsed_ms = (time.perf_counter_ns() - started) / 1_000_000
    return {
        "schema": SCHEMA,
        "status": "built",
        "build_id": manifest["build_id"],
        "active_manifest_ref": manifest_ref,
        "active_manifest_sha256": manifest_sha,
        "review_unit_count": manifest["review_unit_count"],
        "postanswer_feedback_count": manifest["postanswer_feedback_count"],
        "formal_node_count": manifest["formal_node_count"],
        "question_alias_count": manifest["question_alias_count"],
        "ambiguous_question_count": manifest["ambiguous_question_count"],
        "redacted_optional_field_count": manifest["redacted_optional_field_count"],
        "matured_eligibility_normalization_count": manifest[
            "matured_eligibility_normalization_count"
        ],
        "source_table_read_count": len(SOURCE_FILES),
        "new_content_file_count": new_file_count,
        "elapsed_ms": round(elapsed_ms, 3),
        "answer_safe": True,
        "formal_write_count": 0,
        "output_scope": "caller_selected_derived_projection",
    }


def _read_json(
    path: Path,
    *,
    expected_sha256: str | None,
    stats: dict[str, Any],
    max_bytes: int,
) -> dict[str, Any]:
    if not isinstance(max_bytes, int) or isinstance(max_bytes, bool) or max_bytes < 1:
        raise ProjectionError("projection_artifact_invalid_input_bound")
    stats["file_read_attempt_count"] += 1
    try:
        before = path.lstat()
    except FileNotFoundError as exc:
        raise ProjectionError("projection_artifact_missing") from exc
    except OSError as exc:
        raise ProjectionError("projection_artifact_read_failed") from exc
    if stat.S_ISLNK(before.st_mode) or not stat.S_ISREG(before.st_mode):
        raise ProjectionError("projection_artifact_not_regular")
    if before.st_size > max_bytes:
        raise ProjectionError("projection_artifact_exceeds_input_bound")
    try:
        with path.open("rb") as handle:
            opened = os.fstat(handle.fileno())
            if (
                not stat.S_ISREG(opened.st_mode)
                or (opened.st_dev, opened.st_ino) != (before.st_dev, before.st_ino)
            ):
                raise ProjectionError("projection_artifact_changed_during_read")
            if opened.st_size > max_bytes:
                raise ProjectionError("projection_artifact_exceeds_input_bound")
            raw = handle.read(max_bytes + 1)
            after = os.fstat(handle.fileno())
        final = path.lstat()
    except FileNotFoundError as exc:
        raise ProjectionError("projection_artifact_changed_during_read") from exc
    except ProjectionError:
        raise
    except OSError as exc:
        raise ProjectionError("projection_artifact_read_failed") from exc
    if len(raw) > max_bytes:
        raise ProjectionError("projection_artifact_exceeds_input_bound")
    stable_identity = (before.st_dev, before.st_ino, before.st_size)
    if (
        (opened.st_dev, opened.st_ino, opened.st_size) != stable_identity
        or (after.st_dev, after.st_ino, after.st_size) != stable_identity
        or not stat.S_ISREG(final.st_mode)
        or (final.st_dev, final.st_ino, final.st_size) != stable_identity
        or len(raw) != before.st_size
    ):
        raise ProjectionError("projection_artifact_changed_during_read")
    stats["files_read"] += 1
    stats["bytes_read"] += len(raw)
    stats["paths_read"].append(
        path.name
        if path.name == "manifest.json"
        else path.parent.name + "/" + path.name
    )
    if expected_sha256 is not None and _sha256_bytes(raw) != expected_sha256:
        raise ProjectionError("projection_artifact_hash_mismatch")
    try:
        value = json.loads(raw)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ProjectionError("projection_artifact_invalid_json") from exc
    if not isinstance(value, dict):
        raise ProjectionError("projection_artifact_invalid")
    return value


def _projection_counts(value: Any) -> dict[str, int]:
    if not isinstance(value, dict) or set(value) != set(PROJECTION_COUNT_KEYS):
        raise ProjectionError("projection_counts_invalid")
    if any(
        not isinstance(value.get(key), int)
        or isinstance(value.get(key), bool)
        or value[key] < 0
        for key in PROJECTION_COUNT_KEYS
    ):
        raise ProjectionError("projection_counts_invalid")
    return {key: value[key] for key in PROJECTION_COUNT_KEYS}


def _validate_projection_chain(
    pointer: dict[str, Any],
    manifest: dict[str, Any],
    index: dict[str, Any],
) -> dict[str, int]:
    if (
        pointer.get("schema") != POINTER_SCHEMA
        or pointer.get("kind") != "review_hot_active_manifest_pointer"
        or manifest.get("schema") != SCHEMA
        or manifest.get("kind") != "review_hot_content_manifest"
        or index.get("schema") != SCHEMA
        or index.get("kind") != "review_hot_exact_index"
    ):
        raise ProjectionError("projection_chain_schema_invalid")
    artifacts = (pointer, manifest, index)
    if any(
        artifact.get("answer_safe") is not True
        or artifact.get("formal_write_count") != 0
        for artifact in artifacts
    ):
        raise ProjectionError("projection_chain_safety_invalid")
    build_id = pointer.get("build_id")
    object_set_sha = pointer.get("object_set_sha256")
    if (
        not isinstance(build_id, str)
        or not BUILD_ID_RE.fullmatch(build_id)
        or any(artifact.get("build_id") != build_id for artifact in artifacts)
        or not isinstance(object_set_sha, str)
        or not SHA_RE.fullmatch(object_set_sha)
        or any(
            artifact.get("object_set_sha256") != object_set_sha
            for artifact in artifacts
        )
    ):
        raise ProjectionError("projection_chain_identity_mismatch")
    counts = _projection_counts(pointer.get("projection_counts"))
    if any(
        _projection_counts(artifact.get("projection_counts")) != counts
        for artifact in artifacts[1:]
    ):
        raise ProjectionError("projection_chain_count_mismatch")
    for artifact in (manifest, index):
        if any(artifact.get(key) != counts[key] for key in PROJECTION_COUNT_KEYS):
            raise ProjectionError("projection_chain_count_mismatch")
    maps = {
        "review_unit_count": index.get("by_review_unit_id"),
        "postanswer_feedback_count": index.get("postanswer_by_review_unit_id"),
        "formal_node_count": index.get("by_formal_node_id"),
        "question_alias_count": index.get("by_question_id"),
        "ambiguous_question_count": index.get("ambiguous_question_ids"),
    }
    if any(not isinstance(value, dict) for value in maps.values()):
        raise ProjectionError("projection_chain_index_shape_invalid")
    if any(len(maps[key]) != counts[key] for key in maps):
        raise ProjectionError("projection_chain_count_mismatch")
    if set(maps["question_alias_count"]) & set(maps["ambiguous_question_count"]):
        raise ProjectionError("projection_chain_question_identity_overlap")
    if any(
        not isinstance(value, int) or isinstance(value, bool) or value < 2
        for value in maps["ambiguous_question_count"].values()
    ):
        raise ProjectionError("projection_chain_ambiguous_count_invalid")
    object_refs: set[str] = set()
    for key in (
        "review_unit_count",
        "postanswer_feedback_count",
        "formal_node_count",
    ):
        for entry in maps[key].values():
            if not isinstance(entry, dict):
                raise ProjectionError("projection_chain_index_entry_invalid")
            object_ref = entry.get("object_ref")
            object_sha = entry.get("object_sha256")
            _validate_ref(object_ref, "objects", object_sha)
            object_refs.add(object_ref)
    if len(object_refs) != counts["object_count"]:
        raise ProjectionError("projection_chain_count_mismatch")
    object_identities = sorted(
        [f"review_unit:{value}" for value in maps["review_unit_count"]]
        + [f"postanswer:{value}" for value in maps["postanswer_feedback_count"]]
        + [f"formal_node:{value}" for value in maps["formal_node_count"]]
    )
    expected_object_set_sha = _sha256_bytes(
        _json_bytes(
            {
                "schema": "review_hot_object_identity_set_v1",
                "object_identities": object_identities,
                "projection_counts": counts,
            }
        )
    )
    if object_set_sha != expected_object_set_sha:
        raise ProjectionError("projection_chain_object_set_mismatch")
    return counts


def _validate_object_binding(
    obj: dict[str, Any],
    *,
    manifest: dict[str, Any],
    expected_kind: str,
    identity_field: str,
    identity_value: str,
) -> None:
    if (
        obj.get("schema") != SCHEMA
        or obj.get("kind") != expected_kind
        or obj.get("build_id") != manifest.get("build_id")
        or obj.get("object_set_sha256") != manifest.get("object_set_sha256")
        or _projection_counts(obj.get("projection_counts"))
        != _projection_counts(manifest.get("projection_counts"))
        or obj.get("answer_safe") is not True
        or obj.get("formal_write_count") != 0
    ):
        raise ProjectionError("hot_object_binding_invalid")
    if identity_field == "question_id":
        actual = (obj.get("original_track") or {}).get("question_id")
    else:
        actual = obj.get(identity_field)
    if actual != identity_value:
        raise ProjectionError("query_object_identity_mismatch")


def _identity_entry(
    index: dict[str, Any],
    *,
    review_unit_id: str | None,
    formal_node_id: str | None,
    question_id: str | None,
) -> tuple[str, str, dict[str, str]]:
    provided = [review_unit_id is not None, formal_node_id is not None, question_id is not None]
    if sum(provided) != 1:
        raise ProjectionError("exactly_one_query_identity_required")
    if review_unit_id is not None:
        if not RU_ID_RE.fullmatch(review_unit_id):
            raise ProjectionError("invalid_review_unit_id")
        entry = (index.get("by_review_unit_id") or {}).get(review_unit_id)
        kind, value = "review_unit_id", review_unit_id
    elif formal_node_id is not None:
        if not FORMAL_ID_RE.fullmatch(formal_node_id):
            raise ProjectionError("invalid_formal_node_id")
        entry = (index.get("by_formal_node_id") or {}).get(formal_node_id)
        kind, value = "formal_node_id", formal_node_id
    else:
        assert question_id is not None
        if not SAFE_ID_RE.fullmatch(question_id):
            raise ProjectionError("invalid_question_id")
        if question_id in (index.get("ambiguous_question_ids") or {}):
            raise ProjectionError("question_identity_ambiguous")
        entry = (index.get("by_question_id") or {}).get(question_id)
        kind, value = "question_id", question_id
    if not isinstance(entry, dict):
        raise ProjectionError("query_identity_not_found")
    return kind, value, entry


def query_projection(
    projection_dir: str | Path,
    *,
    review_unit_id: str | None = None,
    formal_node_id: str | None = None,
    question_id: str | None = None,
    max_bytes: int = DEFAULT_MAX_BYTES,
    _stats: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Hot exact lookup: pointer, manifest, index, and exactly one object."""

    if not isinstance(max_bytes, int) or not MIN_MAX_BYTES <= max_bytes <= MAX_MAX_BYTES:
        raise ProjectionError("invalid_byte_budget")
    started = time.perf_counter_ns()
    output = Path(projection_dir).resolve()
    stats = _stats if _stats is not None else {
        "file_read_attempt_count": 0,
        "files_read": 0,
        "bytes_read": 0,
        "paths_read": [],
        "formal_table_reads": 0,
        "ledger_reads": 0,
    }
    pointer = _read_json(
        output / "manifest.json",
        expected_sha256=None,
        stats=stats,
        max_bytes=MAX_POINTER_INPUT_BYTES,
    )
    manifest_ref = _validate_ref(
        pointer.get("manifest_ref"), "manifests", pointer.get("manifest_sha256")
    )
    manifest = _read_json(
        output / manifest_ref,
        expected_sha256=pointer["manifest_sha256"],
        stats=stats,
        max_bytes=MAX_MANIFEST_INPUT_BYTES,
    )
    index_ref = _validate_ref(
        manifest.get("index_ref"), "indexes", manifest.get("index_sha256")
    )
    index = _read_json(
        output / index_ref,
        expected_sha256=manifest["index_sha256"],
        stats=stats,
        max_bytes=MAX_INDEX_INPUT_BYTES,
    )
    _validate_projection_chain(pointer, manifest, index)
    identity_kind, identity_value, entry = _identity_entry(
        index,
        review_unit_id=review_unit_id,
        formal_node_id=formal_node_id,
        question_id=question_id,
    )
    object_ref = _validate_ref(
        entry.get("object_ref"), "objects", entry.get("object_sha256")
    )
    obj = _read_json(
        output / object_ref,
        expected_sha256=entry["object_sha256"],
        stats=stats,
        max_bytes=MAX_OBJECT_BYTES,
    )
    _validate_object_binding(
        obj,
        manifest=manifest,
        expected_kind=(
            "review_hot_unit_object"
            if identity_kind == "review_unit_id"
            else "review_hot_formal_object"
        ),
        identity_field=identity_kind,
        identity_value=identity_value,
    )
    result = {
        "schema": SCHEMA,
        "status": "ready",
        "build_id": manifest.get("build_id"),
        "as_of": manifest.get("as_of"),
        "query": {"identity_kind": identity_kind, "identity_value": identity_value},
        "object_ref": object_ref,
        "object_sha256": entry["object_sha256"],
        "record": obj,
        "read_only": True,
        "formal_write_count": 0,
        "byte_budget": max_bytes,
        "scan_stats": stats,
    }
    result["elapsed_ms"] = round((time.perf_counter_ns() - started) / 1_000_000, 3)
    _answer_safe(result)
    for _ in range(4):
        size = len(_json_bytes(result))
        if result.get("byte_count") == size:
            break
        result["byte_count"] = size
    if len(_json_bytes(result)) > max_bytes:
        raise ProjectionError("query_result_exceeds_byte_budget")
    return result


def safe_query_projection(
    projection_dir: str | Path,
    *,
    review_unit_id: str | None = None,
    formal_node_id: str | None = None,
    question_id: str | None = None,
    max_bytes: int = DEFAULT_MAX_BYTES,
) -> dict[str, Any]:
    stats = {
        "file_read_attempt_count": 0,
        "files_read": 0,
        "bytes_read": 0,
        "paths_read": [],
        "formal_table_reads": 0,
        "ledger_reads": 0,
    }
    try:
        return query_projection(
            projection_dir,
            review_unit_id=review_unit_id,
            formal_node_id=formal_node_id,
            question_id=question_id,
            max_bytes=max_bytes,
            _stats=stats,
        )
    except ProjectionError as exc:
        return {
            "schema": SCHEMA,
            "status": "unavailable",
            "reason_code": str(exc),
            "read_only": True,
            "formal_write_count": 0,
            "scan_stats": stats,
        }
    except Exception:
        return {
            "schema": SCHEMA,
            "status": "unavailable",
            "reason_code": "unexpected_projection_failure",
            "read_only": True,
            "formal_write_count": 0,
            "scan_stats": stats,
        }


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo", type=Path, default=Path.cwd())
    parser.add_argument("--output-dir", type=Path)
    sub = parser.add_subparsers(dest="command", required=True)
    build = sub.add_parser("build")
    build.add_argument("--as-of", required=True)
    query = sub.add_parser("query")
    identity = query.add_mutually_exclusive_group(required=True)
    identity.add_argument("--review-unit-id")
    identity.add_argument("--formal-node-id")
    identity.add_argument("--question-id")
    query.add_argument("--max-bytes", type=int, default=DEFAULT_MAX_BYTES)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    output = args.output_dir or args.repo / DEFAULT_OUTPUT_RELATIVE
    try:
        if args.command == "build":
            result = build_projection(
                args.repo, output_dir=output, as_of=args.as_of
            )
        else:
            result = safe_query_projection(
                output,
                review_unit_id=args.review_unit_id,
                formal_node_id=args.formal_node_id,
                question_id=args.question_id,
                max_bytes=args.max_bytes,
            )
    except (ProjectionError, OSError) as exc:
        result = {
            "schema": SCHEMA,
            "status": "error",
            "reason_code": str(exc),
            "formal_write_count": 0,
        }
    print(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True))
    return 0 if result.get("status") in {"built", "ready"} else 2


if __name__ == "__main__":
    raise SystemExit(main())
