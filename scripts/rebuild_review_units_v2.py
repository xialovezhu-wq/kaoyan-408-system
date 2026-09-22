#!/usr/bin/env python3
"""Validate and rebuild the formal 408 review-unit layer.

The command is dry-run by default.  ``--apply`` performs one bounded migration:

* preserve the legacy review table verbatim in a dated archive;
* rebuild the formal review table from independently reviewed subject manifests;
* create one safe card per atomic review unit;
* map every formal question node to exactly one primary review unit;
* preserve legacy-to-v2 migration evidence and emit a receipt.

It never reads or writes ``复盘输出/`` and never modifies question nodes,
relations, details, mastery scores, or redo records.
"""

from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import os
import re
import shutil
import sys
import tempfile
import uuid
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any

from intake_lock_408 import RepoLock, RepoLockTimeout


ROOT = Path(__file__).resolve().parents[1]
SUBJECT_CODES = ("DS", "CO", "OS", "CN")
NODE_RE = re.compile(r"^(?:DS|CO|OS|CN)_(?:\d{4}|UNK)_\d{3}$")
PROTECTED_PROMPT_RE = re.compile(
    r"正确答案|标准答案|错误答案|错选答案|我的答案|我的错选|用户此前答案|答案是"
)
PROTECTED_ANSWER_FRAGMENT_RE = re.compile(
    r"(?:正确答案|标准答案|错误答案|错选答案|我的答案|我的错选|用户此前答案|答案是)"
    r"\s*[:：]?\s*[^/；;|\n]{0,120}"
)
OPTION_COMBO_RE = re.compile(r"(?<![A-Za-z])[ABCD](?:\s*[/、,，+]\s*[ABCD])+(?![A-Za-z])")
VALID_SCORES = {"待评分", "0", "1", "2", "3", "4", "5"}


class RebuildError(RuntimeError):
    pass


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def sha256_file(path: Path) -> str:
    return sha256_bytes(path.read_bytes())


def clean_cell(value: Any) -> str:
    text = "" if value is None else str(value)
    return re.sub(r"\s+", " ", text).strip().replace("|", "｜")


def redact_answer_fragments(value: Any) -> str:
    return PROTECTED_ANSWER_FRAGMENT_RE.sub(
        "答案信息已隐藏（复做保护）", clean_cell(value)
    )


def parse_md_rows(path: Path) -> tuple[list[str], list[dict[str, str]]]:
    header: list[str] | None = None
    rows: list[dict[str, str]] = []
    for line_number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
        if not line.strip().startswith("|"):
            continue
        cells = [cell.strip() for cell in line.strip().strip("|").split("|")]
        if cells and all(re.fullmatch(r":?-{3,}:?", cell) for cell in cells):
            continue
        if header is None:
            header = cells
        elif len(cells) == len(header):
            rows.append(dict(zip(header, cells)))
        else:
            raise RebuildError(
                f"malformed markdown table row at {path}:{line_number}: "
                f"expected {len(header)} cells, got {len(cells)}"
            )
    if header is None:
        raise RebuildError(f"no markdown table found: {path}")
    return header, rows


def formal_node_ids(repo: Path) -> list[str]:
    _, rows = parse_md_rows(repo / "节点总表.md")
    result: list[str] = []
    for row in rows:
        node_id = row.get("ID", "").strip()
        if NODE_RE.fullmatch(node_id):
            result.append(node_id)
    return result


def formal_node_rows(repo: Path) -> dict[str, dict[str, str]]:
    _, rows = parse_md_rows(repo / "节点总表.md")
    return {
        row.get("ID", "").strip(): row
        for row in rows
        if NODE_RE.fullmatch(row.get("ID", "").strip())
    }


def historical_locations(repo: Path) -> dict[str, dict[str, str]]:
    path = repo / "历史错题归档" / "定位索引.md"
    if not path.exists():
        return {}
    _, rows = parse_md_rows(path)
    return {
        row.get("历史归档ID", "").strip(): row
        for row in rows
        if row.get("历史归档ID", "").strip()
    }


def load_json(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise RebuildError(f"expected JSON object: {path}")
    return value


def load_jsonl(path: Path) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
        if not line.strip():
            continue
        value = json.loads(line)
        if not isinstance(value, dict):
            raise RebuildError(f"expected JSON object at {path}:{number}")
        result.append(value)
    return result


def formal_unit_id(candidate_id: str) -> str:
    if not candidate_id.startswith("CAND_RU_"):
        raise RebuildError(f"invalid candidate unit id: {candidate_id}")
    return candidate_id.removeprefix("CAND_")


def safe_date_min(values: list[str]) -> str:
    valid = sorted(value for value in values if re.fullmatch(r"\d{4}-\d{2}-\d{2}", value or ""))
    return valid[0] if valid else "未记录"


def safe_date_max(values: list[str]) -> str:
    valid = sorted(value for value in values if re.fullmatch(r"\d{4}-\d{2}-\d{2}", value or ""))
    return valid[-1] if valid else "未记录"


def candidate_schedule(
    candidate: dict[str, Any],
    legacy_rows_by_id: dict[str, dict[str, str]] | None = None,
) -> dict[str, str]:
    """Return a conservative v2 baseline state from verified formal history.

    Candidate manifests are analytical suggestions, not write authority.  Their
    embedded snapshots never directly populate formal scores or dates.  The old
    table is preserved verbatim in the archive, while every rebuilt semantic
    unit receives a new baseline.
    """
    legacy_rows_by_id = legacy_rows_by_id or {}
    legacy_ids = [clean_cell(item) for item in candidate.get("legacy_review_units") or []]
    verified_rows = [legacy_rows_by_id[item] for item in legacy_ids if item in legacy_rows_by_id]
    state = "recalibration_required_after_rebuild" if verified_rows else "uncalibrated_baseline_due"
    activation = clean_cell(candidate.get("activation", "active"))
    # A rebuilt atomic unit did not itself exist on the parent unit's first date.
    # Keep that history only in the archive/migration evidence.
    first = "未记录"
    score = "待评分"
    last = "未记录"
    mistakes = "未记录"
    importance = "未记录"
    difficulty = "待确认"

    if activation == "blocked_needs_user":
        state = "blocked_needs_user"

    return {
        "state": state,
        "first": first,
        "last": last,
        # A rebuilt or semantically changed unit has no trustworthy next date.
        # ``未记录`` is deliberately due for baseline; blocked units are excluded
        # by state, not by inventing a future date.
        "next": "未记录",
        "interval": "0",
        "mastery": score,
        "mistakes": mistakes,
        "importance": importance,
        "difficulty": difficulty,
    }


def load_manifests(source_dir: Path) -> tuple[list[dict[str, Any]], list[dict[str, Any]], dict[str, Any]]:
    candidates: list[dict[str, Any]] = []
    designs: list[dict[str, Any]] = []
    summaries: dict[str, Any] = {}
    for code in SUBJECT_CODES:
        candidate_path = source_dir / f"{code}_review_unit_candidates.json"
        design_path = source_dir / f"{code}_review_unit_design.jsonl"
        if not candidate_path.exists() or not design_path.exists():
            raise RebuildError(f"missing subject manifests for {code}: {source_dir}")
        document = load_json(candidate_path)
        items = document.get("candidate_units")
        if not isinstance(items, list):
            raise RebuildError(f"candidate_units must be a list: {candidate_path}")
        candidates.extend(items)
        designs.extend(load_jsonl(design_path))
        summaries[code] = document.get("scope_summary", {})
    return candidates, designs, summaries


def validate(
    repo: Path,
    candidates: list[dict[str, Any]],
    designs: list[dict[str, Any]],
) -> dict[str, Any]:
    errors: list[str] = []
    warnings: list[str] = []
    node_ids = formal_node_ids(repo)
    node_set = set(node_ids)
    if len(node_ids) != len(node_set):
        errors.append("duplicate formal node IDs in 节点总表.md")

    candidate_ids = [clean_cell(item.get("candidate_unit_id")) for item in candidates]
    duplicate_candidates = sorted(item for item, count in Counter(candidate_ids).items() if count > 1)
    if duplicate_candidates:
        errors.append(f"duplicate candidate IDs: {duplicate_candidates[:12]}")
    candidate_by_id = {clean_cell(item.get("candidate_unit_id")): item for item in candidates}

    design_ids = [clean_cell(item.get("id")) for item in designs]
    duplicate_designs = sorted(item for item, count in Counter(design_ids).items() if count > 1)
    if duplicate_designs:
        errors.append(f"duplicate node designs: {duplicate_designs[:12]}")
    design_set = set(design_ids)
    missing_designs = sorted(node_set - design_set)
    extra_designs = sorted(design_set - node_set)
    if missing_designs:
        errors.append(f"formal nodes without individual review: {missing_designs[:20]}")
    if extra_designs:
        errors.append(f"design records not in formal table: {extra_designs[:20]}")

    primary_counts: Counter[str] = Counter()
    node_to_candidate: dict[str, str] = {}
    for design in designs:
        node_id = clean_cell(design.get("id"))
        relation_status = clean_cell(design.get("relation_status"))
        if relation_status not in {"connected", "reviewed_no_reliable_edge", "needs_user"}:
            errors.append(f"unknown design relation status: {node_id}={relation_status}")
        primary = design.get("primary_atomic_review_unit") or {}
        candidate_id = clean_cell(primary.get("id"))
        if candidate_id not in candidate_by_id:
            errors.append(f"{node_id} primary endpoint missing: {candidate_id}")
            continue
        primary_counts[node_id] += 1
        node_to_candidate[node_id] = candidate_id
    bad_primary = sorted(node for node in node_set if primary_counts[node] != 1)
    if bad_primary:
        errors.append(f"nodes without exactly one primary mapping: {bad_primary[:20]}")

    candidate_source_nodes: set[str] = set()
    prompt_findings: list[str] = []
    source_mismatches: list[str] = []
    for candidate_id, candidate in candidate_by_id.items():
        formal_unit_id(candidate_id)
        activation = clean_cell(candidate.get("activation", "active"))
        if activation not in {"active", "blocked_needs_user"}:
            errors.append(f"unknown candidate activation: {candidate_id}={activation}")
        candidate_kind = clean_cell(candidate.get("candidate_kind", ""))
        if candidate_kind not in {"formal_node_cluster", "legacy_learning_only"}:
            errors.append(f"unknown candidate kind: {candidate_id}={candidate_kind}")
        prompt = clean_cell(candidate.get("safe_closed_book_question"))
        if not prompt:
            errors.append(f"missing safe closed-book prompt: {candidate_id}")
        if PROTECTED_PROMPT_RE.search(prompt) or OPTION_COMBO_RE.search(prompt):
            prompt_findings.append(candidate_id)
        source_nodes = candidate.get("source_nodes") or []
        if not isinstance(source_nodes, list):
            errors.append(f"source_nodes must be a list: {candidate_id}")
            continue
        for node_id in source_nodes:
            node_id = clean_cell(node_id)
            candidate_source_nodes.add(node_id)
            if node_id not in node_set:
                errors.append(f"candidate source node is not formal: {candidate_id} -> {node_id}")
            if node_to_candidate.get(node_id) != candidate_id:
                source_mismatches.append(f"{node_id}:{candidate_id}!={node_to_candidate.get(node_id)}")
    if prompt_findings:
        errors.append(f"protected content in safe prompts: {prompt_findings[:20]}")
    if source_mismatches:
        errors.append(f"candidate/design source mismatch: {source_mismatches[:20]}")
    missing_candidate_sources = sorted(node_set - candidate_source_nodes)
    if missing_candidate_sources:
        errors.append(f"nodes absent from candidate source coverage: {missing_candidate_sources[:20]}")

    _, legacy_rows = parse_md_rows(repo / "复习单元总表.md")
    legacy_ids = [row.get("复习单元ID", "") for row in legacy_rows if row.get("复习单元ID")]
    legacy_targets: defaultdict[str, set[str]] = defaultdict(set)
    for candidate in candidates:
        new_id = formal_unit_id(clean_cell(candidate.get("candidate_unit_id")))
        for legacy_id in candidate.get("legacy_review_units") or []:
            legacy_targets[clean_cell(legacy_id)].add(new_id)
    unmapped_legacy = sorted(set(legacy_ids) - set(legacy_targets))
    unknown_legacy = sorted(set(legacy_targets) - set(legacy_ids))
    if unmapped_legacy:
        errors.append(f"legacy units without migration target: {unmapped_legacy[:20]}")
    if unknown_legacy:
        warnings.append(f"candidate manifests mention non-current legacy IDs: {unknown_legacy[:20]}")

    blocked = [
        formal_unit_id(clean_cell(item.get("candidate_unit_id")))
        for item in candidates
        if clean_cell(item.get("activation", "active")) == "blocked_needs_user"
    ]
    needs_user_nodes = sorted(
        clean_cell(item.get("id"))
        for item in designs
        if clean_cell(item.get("relation_status")) == "needs_user"
    )
    report = {
        "formal_nodes": len(node_set),
        "individual_node_reviews": len(design_set),
        "candidate_units": len(candidates),
        "active_units": len(candidates) - len(blocked),
        "blocked_units": len(blocked),
        "blocked_unit_ids": sorted(blocked),
        "needs_user_nodes": len(needs_user_nodes),
        "needs_user_node_ids": needs_user_nodes,
        "legacy_units": len(legacy_ids),
        "legacy_units_mapped": len(set(legacy_ids) & set(legacy_targets)),
        "safe_prompt_findings": len(prompt_findings),
        "errors": errors,
        "warnings": warnings,
    }
    if errors:
        raise RebuildError(json.dumps(report, ensure_ascii=False, indent=2))
    return report


def render_review_table(
    candidates: list[dict[str, Any]],
    date: str,
    legacy_rows_by_id: dict[str, dict[str, str]],
) -> str:
    header = [
        "复习单元ID", "科目", "模块", "内容名称", "类型", "调度状态", "覆盖节点",
        "首次学习日期", "上次复习日期", "下次复习日期", "当前间隔天数", "掌握度",
        "错误次数", "重要程度", "难度等级", "闭卷问题", "派生自查口径", "备注",
    ]
    lines = [
        "# 复习单元总表",
        "",
        "> 本表已于 " + date + " 按 `review-unit-v2` 重构。旧表原样保存在 `历史复习单元归档/`。",
        "> 每个 active 且日期未记录的单元都属于待基线全覆盖集合；批次和预计时间不得删项。",
        "> 旧评分与日期只保存在归档和迁移记录中，不直接灌入重构后的新语义单元；全部 active 单元重新做闭卷基线，由用户重新评分。",
        "",
        "| " + " | ".join(header) + " |",
        "|" + "|".join("---" for _ in header) + "|",
    ]
    for candidate in sorted(candidates, key=lambda item: formal_unit_id(clean_cell(item.get("candidate_unit_id")))):
        unit_id = formal_unit_id(clean_cell(candidate.get("candidate_unit_id")))
        schedule = candidate_schedule(candidate, legacy_rows_by_id)
        sources = [clean_cell(item) for item in candidate.get("source_nodes") or []]
        kind = clean_cell(candidate.get("candidate_kind", "formal_node_cluster"))
        unit_type = "待补证据" if schedule["state"] == "blocked_needs_user" else (
            "错题机制" if kind == "formal_node_cluster" else "知识机制"
        )
        criteria = [clean_cell(item) for item in candidate.get("feedback_verification_criteria") or []]
        feedback = "核心机制：" + redact_answer_fragments(candidate.get("atomic_mechanism"))
        if criteria:
            feedback += "；达标：" + "；".join(redact_answer_fragments(item) for item in criteria)
        initial = candidate.get("suggested_initial_schedule_state") or {}
        note = "；".join(
            part for part in [
                clean_cell(initial.get("migration_rule")),
                f"旧单元历史快照 {len(candidate.get('legacy_history_snapshots') or [])} 条",
                f"详见 [[复习单元卡/{unit_id}]]",
            ] if part
        )
        row = [
            unit_id,
            clean_cell(candidate.get("subject")),
            clean_cell(candidate.get("module")),
            clean_cell(candidate.get("name")),
            unit_type,
            schedule["state"],
            "、".join(sources) if sources else "知识学习记录",
            schedule["first"], schedule["last"], schedule["next"], schedule["interval"],
            schedule["mastery"], schedule["mistakes"], schedule["importance"], schedule["difficulty"],
            clean_cell(candidate.get("safe_closed_book_question")), feedback, note,
        ]
        lines.append("| " + " | ".join(clean_cell(item) for item in row) + " |")
    lines.append("")
    return "\n".join(lines)


def render_mapping(designs: list[dict[str, Any]], date: str) -> str:
    header = [
        "正式节点ID", "科目", "证据等级", "实际考点", "主复习单元ID", "次复习单元ID",
        "覆盖方式", "复核状态", "原题轨", "原题轨资格", "待补事项",
    ]
    subject_names = {"DS": "数据结构", "CO": "计算机组成原理", "OS": "操作系统", "CN": "计算机网络"}
    lines = [
        "# 复习单元节点映射",
        "",
        f"> {date} 全库逐题重构产物。每个正式节点必须恰好出现一次并指向一个存在的主复习单元。",
        "> 本表只保存安全知识骨架；独立原题轨仍执行最近复做满 7 天的硬筛选。",
        "",
        "| " + " | ".join(header) + " |",
        "|" + "|".join("---" for _ in header) + "|",
    ]
    for design in sorted(designs, key=lambda item: clean_cell(item.get("id"))):
        node_id = clean_cell(design.get("id"))
        primary = design.get("primary_atomic_review_unit") or {}
        track = design.get("original_question_track") or {}
        issues = design.get("issues") or []
        status = "needs_user" if clean_cell(design.get("relation_status")) == "needs_user" else "reviewed"
        track_need = clean_cell(track.get("need", "required"))
        coverage = "核心机制＋第一动作"
        if track_need == "required":
            coverage += "＋原题轮换"
        elif track_need.startswith("blocked"):
            coverage += "（原题轨待补证据）"
        row = [
            node_id,
            subject_names.get(node_id.split("_", 1)[0], "待确认"),
            clean_cell(design.get("evidence_grade", "待确认")),
            redact_answer_fragments(design.get("actual_exam_point")),
            formal_unit_id(clean_cell(primary.get("id"))),
            "无",
            coverage,
            status,
            track_need,
            redact_answer_fragments(track.get("eligibility", "待确认")),
            "；".join(redact_answer_fragments(item) for item in issues) if issues else "无",
        ]
        lines.append("| " + " | ".join(clean_cell(item) for item in row) + " |")
    lines.append("")
    return "\n".join(lines)


def render_original_tracks(repo: Path, designs: list[dict[str, Any]], date: str) -> str:
    nodes = formal_node_rows(repo)
    locations = historical_locations(repo)
    as_of = dt.date.fromisoformat(date)
    header = [
        "正式节点ID", "主复习单元ID", "原题轨状态", "7天资格", "最近复做日期", "最早允许日期",
        "下次原题复做日期", "来源ID", "年份", "定位标记", "段落号", "完整来源位置", "详情入口", "激活条件", "待补事项",
    ]
    lines = [
        "# 原题复做轨总表",
        "",
        f"> {date} 逐题重构产物，共应覆盖全部正式节点。机制单元可以合并，但每道题的独立原题轨不得合并或删除。",
        "> 初始状态为 `awaiting_primary_baseline`：主机制先取得一次无提示独立成功，再结合 7 天保护激活原题轨；这属于先决条件，不是按时间裁剪。",
        "",
        "| " + " | ".join(header) + " |",
        "|" + "|".join("---" for _ in header) + "|",
    ]
    for design in sorted(designs, key=lambda item: clean_cell(item.get("id"))):
        node_id = clean_cell(design.get("id"))
        node = nodes[node_id]
        primary = design.get("primary_atomic_review_unit") or {}
        track = design.get("original_question_track") or {}
        issues = design.get("issues") or []
        blocked = clean_cell(design.get("relation_status")) == "needs_user"
        last_raw = clean_cell(node.get("最近复做日期")) or "未记录"
        if last_raw in {"未记录", "待补充", ""}:
            last_date = None
        else:
            try:
                last_date = dt.date.fromisoformat(last_raw)
            except ValueError as exc:
                raise RebuildError(f"invalid 最近复做日期 for {node_id}: {last_raw}") from exc
        if last_date is None:
            eligibility = "eligible_unrecorded_date"
            earliest = "未记录"
        else:
            earliest_date = last_date + dt.timedelta(days=7)
            earliest = earliest_date.isoformat()
            eligibility = "eligible" if earliest_date <= as_of else "protected_until"

        if blocked:
            status = "blocked_needs_user"
        else:
            status = "awaiting_primary_baseline"
        if status == "blocked_needs_user":
            activation = "补齐待确认事项并通过主机制基线后再激活"
        elif eligibility == "protected_until":
            activation = f"主机制基线独立成功，且不早于 {earliest}"
        else:
            activation = "主机制基线取得一次无提示独立成功后立即进入原题轨基线"

        source_id = clean_cell(node.get("来源ID")) or "未记录"
        location = locations.get(source_id, {})
        if location:
            marker = clean_cell(location.get("定位标记")) or "未提取"
            paragraph = clean_cell(location.get("段落号")) or "未记录"
            full_location = redact_answer_fragments(location.get("完整来源位置")) or "未记录"
        else:
            marker = "无"
            paragraph = "无"
            full_location = "节点总表.md；" + redact_answer_fragments(node.get("详情入口", "待补充"))
        detail = redact_answer_fragments(node.get("详情入口")) or "待补充"
        row = [
            node_id,
            formal_unit_id(clean_cell(primary.get("id"))),
            status,
            eligibility,
            last_raw,
            earliest,
            "未记录",
            source_id,
            clean_cell(node.get("年份")) or "未标明",
            marker,
            paragraph,
            full_location,
            detail,
            activation,
            "；".join(clean_cell(item) for item in issues) if issues else "无",
        ]
        lines.append("| " + " | ".join(clean_cell(item) for item in row) + " |")
    lines.append("")
    return "\n".join(lines)


def render_card(
    candidate: dict[str, Any],
    legacy_rows_by_id: dict[str, dict[str, str]],
) -> str:
    unit_id = formal_unit_id(clean_cell(candidate.get("candidate_unit_id")))
    schedule = candidate_schedule(candidate, legacy_rows_by_id)
    sources = [clean_cell(item) for item in candidate.get("source_nodes") or []]
    criteria = [clean_cell(item) for item in candidate.get("feedback_verification_criteria") or []]
    initial = candidate.get("suggested_initial_schedule_state") or {}
    legacy = [clean_cell(item) for item in candidate.get("legacy_review_units") or []]
    lines = [
        "---",
        "schema: review_unit_card_v2",
        f"review_unit_id: {unit_id}",
        f"subject: {clean_cell(candidate.get('subject'))}",
        f"module: {clean_cell(candidate.get('module'))}",
        f"schedule_state: {schedule['state']}",
        "---",
        "",
        f"# {unit_id} · {clean_cell(candidate.get('name'))}",
        "",
        f"- 覆盖正式节点：{'、'.join(sources) if sources else '知识学习记录'}",
        f"- 旧复习单元：{'、'.join(legacy) if legacy else '无'}",
        f"- 当前状态：{schedule['state']}",
        "",
        "## 闭卷提取",
        "",
        clean_cell(candidate.get("safe_closed_book_question")),
        "",
        "先独立作答并记录置信度、反应时与是否使用提示，再展开下方自查口径。",
        "",
        "> [!answer]- 派生自查口径（首答后展开）",
        f"> 核心机制：{redact_answer_fragments(candidate.get('atomic_mechanism'))}",
        ">",
        "> 达标标准：",
    ]
    if criteria:
        lines.extend(f"> - {redact_answer_fragments(item)}" for item in criteria)
    else:
        lines.append("> - 当前缺少可靠达标口径，必须先补证据。")
    lines += [
        "",
        "## 迁移生成规则",
        "",
        "更换表面情境、参数或问法，但保持同一底层机制；先说明识别信号与第一动作，再完成边界判断。迁移通过与保持通过分开记录。",
        "",
        "## 调度与证据边界",
        "",
        f"- 迁移规则：{clean_cell(initial.get('migration_rule', '待补充'))}",
        "- 当日到期集合必须全覆盖；批次只组织顺序，不删除项目。",
        "- 同日反馈后重答成功不冒充跨日保持成功。",
        "- 未经用户明确给出 0–5 分，不更新正式掌握度。",
        "- 原题复做轨另行执行 7 天外硬筛选；本卡不保存完整题干、答案或解析。",
        "",
    ]
    return "\n".join(lines)


def render_migration(
    legacy_rows: list[dict[str, str]],
    candidates: list[dict[str, Any]],
    date: str,
) -> str:
    targets: defaultdict[str, list[dict[str, Any]]] = defaultdict(list)
    for candidate in candidates:
        for legacy_id in candidate.get("legacy_review_units") or []:
            targets[clean_cell(legacy_id)].append(candidate)
    lines = [
        "# 复习单元 v1 → v2 迁移记录",
        "",
        f"> 迁移日期：{date}。旧表原样归档；本表只解释语义去向，不改写历史事实。",
        "",
        "| 旧复习单元ID | 迁移判定 | v2 目标单元 | 历史处理 |",
        "|---|---|---|---|",
    ]
    for row in legacy_rows:
        legacy_id = clean_cell(row.get("复习单元ID"))
        items = targets.get(legacy_id, [])
        new_ids = sorted(formal_unit_id(clean_cell(item.get("candidate_unit_id"))) for item in items)
        if len(new_ids) > 1:
            decision = "拆分"
        elif items and len(items[0].get("legacy_review_units") or []) > 1:
            decision = "合并"
        else:
            decision = "一对一保留并重写"
        history = "旧表原样归档；v2 不继承旧综合掌握度/下次日期，全部进入新语义基线"
        lines.append(f"| {legacy_id} | {decision} | {'、'.join(new_ids)} | {history} |")
    lines.append("")
    return "\n".join(lines)


def write_atomic(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".tmp")
    temporary.write_text(content.rstrip() + "\n", encoding="utf-8")
    os.replace(temporary, path)


def remove_path(path: Path) -> None:
    if path.is_dir() and not path.is_symlink():
        shutil.rmtree(path)
    elif path.exists() or path.is_symlink():
        path.unlink()


def copy_path(source: Path, target: Path) -> None:
    target.parent.mkdir(parents=True, exist_ok=True)
    if source.is_dir() and not source.is_symlink():
        shutil.copytree(source, target)
    else:
        shutil.copy2(source, target)


def rebuild_wal_path(source_dir: Path) -> Path:
    return source_dir.parent / "review-unit-v2-rebuild-wal.json"


def rollback_from_wal(repo: Path, wal_path: Path) -> dict[str, Any]:
    if not wal_path.exists():
        return {"status": "no_wal"}
    wal = load_json(wal_path)
    if wal.get("state") not in {"prepared", "applying"}:
        return {"status": str(wal.get("state", "unknown"))}
    tx_dir = Path(str(wal.get("tx_dir", "")))
    if not tx_dir.is_absolute():
        tx_dir = repo / tx_dir
    targets = wal.get("targets") or []
    for item in reversed(targets):
        target = repo / str(item["target"])
        remove_path(target)
        if item.get("existed"):
            backup = tx_dir / str(item["backup"])
            if not backup.exists():
                raise RebuildError(f"cannot recover missing backup: {backup}")
            copy_path(backup, target)
    wal["state"] = "rolled_back"
    wal["rolled_back_at"] = dt.datetime.now(dt.timezone.utc).isoformat()
    write_atomic(wal_path, json.dumps(wal, ensure_ascii=False, indent=2))
    return {"status": "rolled_back", "transaction_id": wal.get("transaction_id")}


def assert_generated_safe(paths: list[Path]) -> None:
    value_leak_re = re.compile(
        r"(?:正确答案|标准答案|错误答案|错选答案|我的答案|我的错选|用户此前答案)"
        r"\s*[:：]\s*\S+|答案是\s*\S+"
    )
    findings: list[str] = []
    for path in paths:
        if path.is_dir():
            files = sorted(path.rglob("*.md"))
        else:
            files = [path]
        for file_path in files:
            match = value_leak_re.search(file_path.read_text(encoding="utf-8"))
            if match:
                findings.append(f"{file_path}:{match.group(0)[:80]}")
    if findings:
        raise RebuildError(f"protected answer leakage in staged outputs: {findings[:20]}")


def build_preview(
    repo: Path,
    candidates: list[dict[str, Any]],
    designs: list[dict[str, Any]],
    date: str,
) -> dict[str, Any]:
    _, legacy_rows = parse_md_rows(repo / "复习单元总表.md")
    legacy_rows_by_id = {
        clean_cell(row.get("复习单元ID")): row
        for row in legacy_rows
        if clean_cell(row.get("复习单元ID"))
    }
    contents = {
        "review_table": render_review_table(candidates, date, legacy_rows_by_id),
        "mapping": render_mapping(designs, date),
        "original_tracks": render_original_tracks(repo, designs, date),
        "migration": render_migration(legacy_rows, candidates, date),
    }
    with tempfile.TemporaryDirectory(prefix="review-unit-v2-preview-") as tmp:
        root = Path(tmp)
        files: dict[str, Path] = {}
        for key, content in contents.items():
            path = root / f"{key}.md"
            path.write_text(content.rstrip() + "\n", encoding="utf-8")
            files[key] = path
        cards = root / "cards"
        cards.mkdir()
        for candidate in candidates:
            unit_id = formal_unit_id(clean_cell(candidate.get("candidate_unit_id")))
            (cards / f"{unit_id}.md").write_text(
                render_card(candidate, legacy_rows_by_id).rstrip() + "\n", encoding="utf-8"
            )
        _, unit_rows = parse_md_rows(files["review_table"])
        _, mapping_rows = parse_md_rows(files["mapping"])
        _, track_rows = parse_md_rows(files["original_tracks"])
        if len(unit_rows) != len(candidates):
            raise RebuildError("preview review-table cardinality mismatch")
        if len(mapping_rows) != len(designs) or len(track_rows) != len(designs):
            raise RebuildError("preview mapping/original-track cardinality mismatch")
        card_count = len(list(cards.glob("RU_*.md")))
        if card_count != len(candidates):
            raise RebuildError("preview card cardinality mismatch")
        assert_generated_safe(list(files.values()) + [cards])
        return {
            "review_units": len(unit_rows),
            "node_mappings": len(mapping_rows),
            "original_tracks": len(track_rows),
            "cards": card_count,
            "protected_content_findings": 0,
            "review_table_sha256": sha256_file(files["review_table"]),
            "mapping_sha256": sha256_file(files["mapping"]),
            "original_tracks_sha256": sha256_file(files["original_tracks"]),
        }


def apply_rebuild(
    repo: Path,
    source_dir: Path,
    candidates: list[dict[str, Any]],
    designs: list[dict[str, Any]],
    validation: dict[str, Any],
    date: str,
) -> dict[str, Any]:
    # Validate every path-dependent argument before any write.
    source_rel = source_dir.resolve().relative_to(repo.resolve())
    legacy_table = repo / "复习单元总表.md"
    legacy_bytes = legacy_table.read_bytes()
    _, legacy_rows = parse_md_rows(legacy_table)
    legacy_rows_by_id = {
        clean_cell(row.get("复习单元ID")): row
        for row in legacy_rows
        if clean_cell(row.get("复习单元ID"))
    }
    table_content = render_review_table(candidates, date, legacy_rows_by_id)
    mapping_content = render_mapping(designs, date)
    original_tracks_content = render_original_tracks(repo, designs, date)
    migration_content = render_migration(legacy_rows, candidates, date)

    archive_dir = repo / "历史复习单元归档"
    archive = archive_dir / f"{date}-复习单元总表-v1.md"
    cards_dir = repo / "复习单元卡"
    receipt_path = source_dir.parent / "review-unit-v2-rebuild-receipt.json"
    wal_path = rebuild_wal_path(source_dir)
    backup_cards = archive_dir / f"{date}-复习单元卡-v1"

    with RepoLock(repo, timeout=30.0, timeout_error=RebuildError):
        rollback_from_wal(repo, wal_path)
        if archive.exists() and archive.read_bytes() != legacy_bytes:
            raise RebuildError(f"archive exists with different content: {archive}")
        if cards_dir.exists() and backup_cards.exists():
            raise RebuildError(f"card backup already exists: {backup_cards}")

        transaction_id = f"{date}-{uuid.uuid4().hex[:12]}"
        tx_root = source_dir.parent / ".review-unit-v2-transaction" / transaction_id
        staging = tx_root / "staging"
        backup = tx_root / "backup"
        staging.mkdir(parents=True, exist_ok=False)
        backup.mkdir(parents=True, exist_ok=False)

        staged_files = {
            repo / "复习单元总表.md": staging / "复习单元总表.md",
            repo / "复习单元节点映射.md": staging / "复习单元节点映射.md",
            repo / "原题复做轨总表.md": staging / "原题复做轨总表.md",
            repo / "复习单元迁移记录.md": staging / "复习单元迁移记录.md",
        }
        for target, content in [
            (repo / "复习单元总表.md", table_content),
            (repo / "复习单元节点映射.md", mapping_content),
            (repo / "原题复做轨总表.md", original_tracks_content),
            (repo / "复习单元迁移记录.md", migration_content),
        ]:
            staged_files[target].write_text(content.rstrip() + "\n", encoding="utf-8")

        staged_cards = staging / "复习单元卡"
        staged_cards.mkdir()
        for candidate in candidates:
            unit_id = formal_unit_id(clean_cell(candidate.get("candidate_unit_id")))
            (staged_cards / f"{unit_id}.md").write_text(
                render_card(candidate, legacy_rows_by_id).rstrip() + "\n", encoding="utf-8"
            )

        staged_archive = staging / "历史复习单元归档" / archive.name
        staged_archive.parent.mkdir(parents=True)
        staged_archive.write_bytes(legacy_bytes)
        staged_backup_cards: Path | None = None
        if cards_dir.exists():
            staged_backup_cards = staging / "历史复习单元归档" / backup_cards.name
            shutil.copytree(cards_dir, staged_backup_cards)

        # Independently reparse all staged formal tables and compare cardinalities.
        _, staged_unit_rows = parse_md_rows(staged_files[repo / "复习单元总表.md"])
        _, staged_mapping_rows = parse_md_rows(staged_files[repo / "复习单元节点映射.md"])
        _, staged_track_rows = parse_md_rows(staged_files[repo / "原题复做轨总表.md"])
        if len(staged_unit_rows) != len(candidates):
            raise RebuildError("staged review table cardinality mismatch")
        if len(staged_mapping_rows) != len(designs) or len(staged_track_rows) != len(designs):
            raise RebuildError("staged node mapping/original-track cardinality mismatch")
        if len(list(staged_cards.glob("RU_*.md"))) != len(candidates):
            raise RebuildError("staged review-card cardinality mismatch")
        assert_generated_safe(list(staged_files.values()) + [staged_cards])

        receipt = {
            "schema": "review_unit_rebuild_receipt_v2",
            "date": date,
            "transaction_id": transaction_id,
            "source_dir": str(source_rel),
            "legacy_table_sha256": sha256_bytes(legacy_bytes),
            "new_table_sha256": sha256_file(staged_files[repo / "复习单元总表.md"]),
            "mapping_sha256": sha256_file(staged_files[repo / "复习单元节点映射.md"]),
            "original_tracks_sha256": sha256_file(staged_files[repo / "原题复做轨总表.md"]),
            "migration_sha256": sha256_file(staged_files[repo / "复习单元迁移记录.md"]),
            "card_count": len(list(staged_cards.glob("RU_*.md"))),
            "validation": validation,
            "write_scope": [
                "复习单元总表.md", "复习单元节点映射.md", "原题复做轨总表.md",
                "复习单元迁移记录.md", "复习单元卡/", "历史复习单元归档/",
            ],
            "explicit_exclusions": ["复盘输出/", "节点总表.md", "关系边表.md", "错题复做记录.md"],
        }
        staged_receipt = staging / "review-unit-v2-rebuild-receipt.json"
        staged_receipt.write_text(json.dumps(receipt, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

        publish_pairs: list[tuple[Path, Path]] = list(staged_files.items()) + [(cards_dir, staged_cards)]
        if not archive.exists():
            publish_pairs.append((archive, staged_archive))
        if staged_backup_cards is not None:
            publish_pairs.append((backup_cards, staged_backup_cards))
        publish_pairs.append((receipt_path, staged_receipt))

        target_meta: list[dict[str, Any]] = []
        for index, (target, staged) in enumerate(publish_pairs):
            existed = target.exists()
            backup_rel = Path(f"target-{index:03d}")
            if existed:
                copy_path(target, backup / backup_rel)
            target_meta.append(
                {
                    "target": str(target.relative_to(repo)),
                    "staged": str(staged.relative_to(tx_root)),
                    "backup": str((Path("backup") / backup_rel)),
                    "existed": existed,
                    "kind": "dir" if staged.is_dir() else "file",
                }
            )

        wal = {
            "schema": "review_unit_rebuild_wal_v2",
            "transaction_id": transaction_id,
            "state": "prepared",
            "created_at": dt.datetime.now(dt.timezone.utc).isoformat(),
            "tx_dir": str(tx_root.relative_to(repo)),
            "targets": target_meta,
        }
        write_atomic(wal_path, json.dumps(wal, ensure_ascii=False, indent=2))
        wal["state"] = "applying"
        write_atomic(wal_path, json.dumps(wal, ensure_ascii=False, indent=2))
        try:
            fail_after = int(os.environ.get("REVIEW_UNIT_V2_FAIL_AFTER_PUBLISH", "0") or "0")
            for publish_index, (target, staged) in enumerate(publish_pairs, start=1):
                target.parent.mkdir(parents=True, exist_ok=True)
                remove_path(target)
                os.replace(staged, target)
                if fail_after and publish_index == fail_after:
                    raise RebuildError(f"injected failure after publish target {publish_index}")
            wal["state"] = "complete"
            wal["completed_at"] = dt.datetime.now(dt.timezone.utc).isoformat()
            write_atomic(wal_path, json.dumps(wal, ensure_ascii=False, indent=2))
        except BaseException:
            rollback_from_wal(repo, wal_path)
            raise
        finally:
            if tx_root.exists() and wal.get("state") == "complete":
                shutil.rmtree(tx_root)
        return receipt


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Validate/rebuild review-unit v2; dry-run by default.")
    parser.add_argument("--repo", default=str(ROOT))
    parser.add_argument(
        "--source-dir",
        default="wiki/lint_reports/2026-07-10-复习单元重构/source",
        help="Path relative to repo or absolute path containing four subject manifests.",
    )
    parser.add_argument("--date", default=dt.date.today().isoformat())
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--recover-only", action="store_true")
    return parser.parse_args(argv)


def main(argv: list[str]) -> int:
    args = parse_args(argv)
    repo = Path(args.repo).resolve()
    source_dir = Path(args.source_dir)
    if not source_dir.is_absolute():
        source_dir = repo / source_dir
    try:
        dt.date.fromisoformat(args.date)
        source_dir.resolve().relative_to(repo)
        if args.recover_only:
            with RepoLock(repo, timeout=30.0, timeout_error=RebuildError):
                recovery = rollback_from_wal(repo, rebuild_wal_path(source_dir))
            print(json.dumps({"mode": "recover-only", "recovery": recovery}, ensure_ascii=False, indent=2))
            return 0
        candidates, designs, summaries = load_manifests(source_dir)
        validation = validate(repo, candidates, designs)
        preview = build_preview(repo, candidates, designs, args.date)
        result: dict[str, Any] = {
            "mode": "apply" if args.apply else "dry-run",
            "validation": validation,
            "preview": preview,
            "subject_summaries": summaries,
        }
        if args.apply:
            result["receipt"] = apply_rebuild(
                repo, source_dir, candidates, designs, validation, args.date
            )
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0
    except (OSError, ValueError, json.JSONDecodeError, RebuildError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
