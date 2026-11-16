#!/usr/bin/env python3
"""Read-only data-quality audit for the formal 408 review schedule."""

from __future__ import annotations

import argparse
import datetime as dt
import json
import re
import sys
from collections import Counter
from pathlib import Path

from generate_morning_review_queue import (
    NODE_ID_RE,
    VALID_ORIGINAL_ELIGIBILITY,
    VALID_ORIGINAL_TRACK_STATES,
    due_original_tracks,
    due_units,
    load_original_tracks,
    load_units,
    parse_date,
    parse_date_strict,
    read_markdown_table,
)
from generate_morning_action_queue import classify_units


ROOT = Path(__file__).resolve().parents[1]
VALID_MASTERY = {"待评分", "0", "1", "2", "3", "4", "5"}
REQUIRED_REVIEW_UNIT_CARD_SECTIONS = (
    "## 闭卷提取",
    "## 迁移生成规则",
    "## 调度与证据边界",
)


def _frontmatter_scalar(raw: str) -> str:
    value = raw.strip()
    if value.startswith('"'):
        try:
            parsed = json.loads(value)
        except json.JSONDecodeError:
            return value
        return str(parsed)
    if len(value) >= 2 and value.startswith("'") and value.endswith("'"):
        return value[1:-1].replace("''", "'")
    return value


def _review_unit_card_errors(path: Path, unit: object) -> list[str]:
    """Validate one review-unit-v2 card without mutating it."""
    label = path.name
    lines = path.read_text(encoding="utf-8").splitlines()
    errors: list[str] = []
    frontmatter: dict[str, list[str]] = {}

    if not lines or lines[0].strip() != "---":
        errors.append(f"{label}:frontmatter 缺失")
        end = None
    else:
        end = next(
            (index for index in range(1, len(lines)) if lines[index].strip() == "---"),
            None,
        )
        if end is None:
            errors.append(f"{label}:frontmatter 未闭合")
        else:
            for line in lines[1:end]:
                match = re.match(r"^([A-Za-z_][A-Za-z0-9_-]*)\s*:\s*(.*)$", line.strip())
                if match:
                    frontmatter.setdefault(match.group(1), []).append(
                        _frontmatter_scalar(match.group(2))
                    )

    def one(key: str) -> str | None:
        values = frontmatter.get(key, [])
        if len(values) != 1:
            errors.append(f"{label}:frontmatter {key} 必须恰好 1 项，实际 {len(values)}")
            return None
        return values[0]

    schema = one("schema")
    card_unit_id = one("review_unit_id")
    card_schedule = one("schedule_state")
    if schema is not None and schema != "review_unit_card_v2":
        errors.append(f"{label}:schema 错误 {schema}")
    if card_unit_id is not None:
        if card_unit_id != path.stem:
            errors.append(
                f"{label}:review_unit_id {card_unit_id} 与文件名 {path.stem} 不一致"
            )
        if card_unit_id != unit.unit_id:
            errors.append(
                f"{label}:review_unit_id {card_unit_id} 与总表 {unit.unit_id} 不一致"
            )
    if card_schedule is not None and card_schedule != unit.schedule_state:
        errors.append(
            f"{label}:schedule_state {card_schedule} 与总表 {unit.schedule_state} 不一致"
        )

    for section in REQUIRED_REVIEW_UNIT_CARD_SECTIONS:
        count = sum(line.strip() == section for line in lines)
        if count != 1:
            errors.append(f"{label}:必需章节 {section} 必须恰好 1 个，实际 {count}")

    coverage_lines = [
        match.group(1).strip()
        for line in lines
        if (match := re.match(r"^\s*-\s*覆盖正式节点\s*[:：]\s*(.*?)\s*$", line))
    ]
    if len(coverage_lines) != 1:
        errors.append(f"{label}:覆盖正式节点必须恰好 1 行，实际 {len(coverage_lines)}")
    else:
        card_nodes = set(NODE_ID_RE.findall(coverage_lines[0]))
        table_nodes = set(NODE_ID_RE.findall(unit.source_nodes))
        if card_nodes != table_nodes:
            errors.append(
                f"{label}:覆盖节点与总表不一致 "
                f"card={sorted(card_nodes)} table={sorted(table_nodes)}"
            )
    return errors


def audit(repo: Path, today: dt.date) -> dict[str, object]:
    units = load_units(repo)
    ids = [item.unit_id for item in units]
    duplicate_ids = sorted(item for item, count in Counter(ids).items() if count > 1)
    invalid_mastery = sorted(item.unit_id for item in units if item.mastery not in VALID_MASTERY)
    invalid_dates: list[str] = []
    interval_drift: list[str] = []
    due: list[object] = []
    baseline_backlog: list[object] = []
    mapped_nodes: set[str] = set()

    for item in units:
        for field, raw in [("首次学习日期", item.first_date), ("上次复习日期", item.last_date), ("下次复习日期", item.next_date)]:
            if raw not in {"", "未记录", "待补充"} and parse_date(raw) is None:
                invalid_dates.append(f"{item.unit_id}:{field}={raw}")
        last = parse_date(item.last_date)
        next_date = parse_date(item.next_date)
        if last and next_date and (next_date - last).days != item.interval:
            interval_drift.append(item.unit_id)
        mapped_nodes.update(NODE_ID_RE.findall(" ".join([item.title, item.method, item.note])))

    if not invalid_dates:
        due, baseline_backlog, _ = classify_units(units, today)

    node_rows = 0
    formal_ids: set[str] = set()
    node_table = repo / "节点总表.md"
    if node_table.exists():
        formal_ids = {
            match.group(1)
            for line in node_table.read_text(encoding="utf-8").splitlines()
            if (match := re.match(r"^\|\s*((?:DS|CO|OS|CN)_(?:\d{4}|UNK)_\d{3})\s*\|", line))
        }
        node_rows = len(formal_ids)

    v2_mode = "调度状态" in (repo / "复习单元总表.md").read_text(encoding="utf-8")

    reschedule_marked = sum("2026-07-04 清账顺延" in item.note for item in units)
    mapping_path = repo / "复习单元节点映射.md"
    duplicate_primary_mapping: list[str] = []
    missing_review_unit_endpoint: list[str] = []
    mapping_missing_nodes: list[str] = []
    mapping_extra_nodes: list[str] = []
    if mapping_path.exists():
        mapping_rows = read_markdown_table(mapping_path)
        mapping_ids = [row.get("正式节点ID", "") for row in mapping_rows if row.get("正式节点ID")]
        mapping_counts = Counter(mapping_ids)
        duplicate_primary_mapping = sorted(item for item, count in mapping_counts.items() if count != 1)
        mapped_nodes = set(mapping_ids)
        mapping_missing_nodes = sorted(formal_ids - mapped_nodes)
        mapping_extra_nodes = sorted(mapped_nodes - formal_ids)
        unit_ids = set(ids)
        missing_review_unit_endpoint = sorted(
            row.get("主复习单元ID", "")
            for row in mapping_rows
            if row.get("主复习单元ID", "") not in unit_ids
        )
    mapping_errors = (
        len(duplicate_primary_mapping)
        + len(missing_review_unit_endpoint)
        + len(mapping_missing_nodes)
        + len(mapping_extra_nodes)
    )
    if v2_mode and not mapping_path.exists():
        mapping_errors += 1
        mapping_missing_nodes = sorted(formal_ids)
    original_track_path = repo / "原题复做轨总表.md"
    original_track_status: Counter[str] = Counter()
    original_track_eligibility: Counter[str] = Counter()
    original_track_rows = 0
    original_track_missing_nodes: list[str] = []
    original_track_extra_nodes: list[str] = []
    original_track_duplicate_nodes: list[str] = []
    original_track_missing_endpoints: list[str] = []
    original_track_invalid_dates: list[str] = []
    original_track_eligibility_refresh_due: list[str] = []
    original_due = []
    if original_track_path.exists():
        track_rows = read_markdown_table(original_track_path)
        original_track_rows = len(track_rows)
        track_ids = [row.get("正式节点ID", "") for row in track_rows]
        track_counts = Counter(track_ids)
        original_track_duplicate_nodes = sorted(item for item, count in track_counts.items() if count > 1)
        track_id_set = set(track_ids)
        original_track_missing_nodes = sorted(formal_ids - track_id_set)
        original_track_extra_nodes = sorted(track_id_set - formal_ids)
        unit_id_set = set(ids)
        original_track_missing_endpoints = sorted(
            row.get("主复习单元ID", "")
            for row in track_rows
            if row.get("主复习单元ID", "") not in unit_id_set
        )
        original_track_status.update(row.get("原题轨状态", "未记录") for row in track_rows)
        original_track_eligibility.update(row.get("7天资格", "未记录") for row in track_rows)
        tracks = load_original_tracks(repo)
        for track in tracks:
            for field, raw in [
                ("最近复做日期", track.last_date),
                ("最早允许日期", track.earliest_date),
                ("下次原题复做日期", track.next_date),
            ]:
                try:
                    parse_date_strict(raw, f"{track.node_id}.{field}")
                except ValueError:
                    original_track_invalid_dates.append(f"{track.node_id}:{field}={raw}")
            last = parse_date(track.last_date)
            earliest = parse_date(track.earliest_date)
            if last and earliest and earliest != last + dt.timedelta(days=7):
                original_track_invalid_dates.append(
                    f"{track.node_id}:最早允许日期应为{(last + dt.timedelta(days=7)).isoformat()}"
                )
            if track.eligibility == "protected_until" and earliest is not None and earliest <= today:
                original_track_eligibility_refresh_due.append(track.node_id)
            if track.eligibility == "eligible" and earliest is not None and earliest > today:
                original_track_invalid_dates.append(f"{track.node_id}:尚未满7天却标eligible")
            if last is None and track.eligibility != "eligible_unrecorded_date":
                original_track_invalid_dates.append(f"{track.node_id}:日期未记录时资格必须为eligible_unrecorded_date")
        if not original_track_invalid_dates:
            original_due = due_original_tracks(tracks, today)
        mapping_errors += (
            len(original_track_duplicate_nodes)
            + len(original_track_missing_nodes)
            + len(original_track_extra_nodes)
            + len(original_track_missing_endpoints)
            + len(original_track_invalid_dates)
        )
    elif v2_mode:
        mapping_errors += 1
        original_track_missing_nodes = sorted(formal_ids)

    cards_dir = repo / "复习单元卡"
    card_ids = {path.stem for path in cards_dir.glob("RU_*.md")} if cards_dir.is_dir() else set()
    missing_cards = sorted(set(ids) - card_ids) if v2_mode else []
    extra_cards = sorted(card_ids - set(ids)) if v2_mode else []
    review_unit_card_errors: list[str] = []
    if v2_mode and not cards_dir.is_dir():
        mapping_errors += 1
    mapping_errors += len(missing_cards) + len(extra_cards)
    if v2_mode and cards_dir.is_dir():
        units_by_id = {item.unit_id: item for item in units}
        for unit_id in sorted(set(ids) & card_ids):
            # 重复总表 ID 已由 duplicate_ids 单独报错；卡片端点仍以该 ID 复核。
            review_unit_card_errors.extend(
                _review_unit_card_errors(cards_dir / f"{unit_id}.md", units_by_id[unit_id])
            )
        mapping_errors += len(review_unit_card_errors)

    leak_re = re.compile(
        r"(?:正确答案|标准答案|错误答案|错选答案|我的答案|我的错选|用户此前答案)"
        r"\s*(?:[:：]\s*)?\S+|答案是\s*\S+"
    )
    protected_content_findings: list[str] = []
    if v2_mode:
        scan_paths = [repo / "复习单元总表.md", mapping_path, original_track_path]
        if cards_dir.is_dir():
            scan_paths.extend(sorted(cards_dir.glob("RU_*.md")))
        for path in scan_paths:
            if path.exists() and (match := leak_re.search(path.read_text(encoding="utf-8"))):
                protected_content_findings.append(f"{path.relative_to(repo)}:{match.group(0)[:80]}")
        mapping_errors += len(protected_content_findings)
    report: dict[str, object] = {
        "date": today.isoformat(),
        "review_units": len(units),
        "due_or_overdue": len(due),
        "baseline_backlog_count": len(baseline_backlog),
        "original_question_due": len(original_due),
        "total_due": len(due) + len(original_due),
        "future_active": sum(
            1
            for item in units
            if not item.schedule_state.startswith("blocked")
            and item not in due
            and item not in baseline_backlog
        ),
        "due_by_subject": dict(Counter(item.subject for item in due)),
        "due_by_mastery": dict(Counter(item.mastery for item in due)),
        "duplicate_ids": duplicate_ids,
        "invalid_mastery": invalid_mastery,
        "invalid_dates": invalid_dates,
        "interval_date_drift_count": len(interval_drift),
        "interval_date_drift_sample": interval_drift[:12],
        "legacy_reschedule_marker_count": reschedule_marked,
        "blocked_review_units": sum(item.schedule_state.startswith("blocked") for item in units),
        "formal_nodes": node_rows,
        "explicitly_mapped_nodes": len(mapped_nodes),
        "unmapped_or_indirect_nodes": max(node_rows - len(mapped_nodes), 0),
        "duplicate_primary_mapping": duplicate_primary_mapping,
        "missing_review_unit_endpoint": missing_review_unit_endpoint,
        "mapping_missing_nodes": mapping_missing_nodes,
        "mapping_extra_nodes": mapping_extra_nodes,
        "original_track_rows": original_track_rows,
        "original_track_status": dict(original_track_status),
        "original_track_eligibility": dict(original_track_eligibility),
        "original_track_missing_nodes": original_track_missing_nodes,
        "original_track_extra_nodes": original_track_extra_nodes,
        "original_track_duplicate_nodes": original_track_duplicate_nodes,
        "original_track_missing_endpoints": original_track_missing_endpoints,
        "original_track_invalid_dates": original_track_invalid_dates,
        "original_track_eligibility_refresh_due": original_track_eligibility_refresh_due,
        "missing_cards": missing_cards,
        "extra_cards": extra_cards,
        "review_unit_card_errors": review_unit_card_errors,
        "protected_content_findings": protected_content_findings,
        "hard_errors": len(duplicate_ids) + len(invalid_mastery) + len(invalid_dates) + mapping_errors,
    }
    return report


def render_markdown(report: dict[str, object]) -> str:
    due_by_subject = report["due_by_subject"]
    due_by_mastery = report["due_by_mastery"]
    assert isinstance(due_by_subject, dict)
    assert isinstance(due_by_mastery, dict)
    return "\n".join(
        [
            f"# {report['date']} 408 复盘调度只读审计",
            "",
            f"- 正式复习单元：{report['review_units']}；机制到期/过期：{report['due_or_overdue']}；原题轨到期：{report['original_question_due']}；合计：{report['total_due']}。",
            f"- 待个体化校准库存：{report['baseline_backlog_count']}；它们属于 inventory，不自动获得“今天到期”语义。",
            "- 到期科目：" + "；".join(f"{key}{value}" for key, value in due_by_subject.items()) + "。",
            "- 到期掌握度：" + "；".join(f"{key}={value}" for key, value in due_by_mastery.items()) + "。",
            f"- 日期—间隔语义漂移：{report['interval_date_drift_count']} 项；旧清账标记：{report['legacy_reschedule_marker_count']} 项。",
            f"- 正式节点显式映射：{report['explicitly_mapped_nodes']}/{report['formal_nodes']}；其余可能只有知识点级间接覆盖，当前不可审计。",
            f"- 待补证据复习单元：{report['blocked_review_units']} 项；阻塞项不进入可评分晨间分母。",
            f"- 独立原题轨：{report['original_track_rows']}/{report['formal_nodes']}；机制单元可合并，但原题轨必须一题一行。",
            "- 原题轨状态：" + "；".join(
                f"{key}={value}" for key, value in report["original_track_status"].items()
            ) + "。",
            f"- 结构硬错误：{report['hard_errors']}。",
            "",
            "## 判读",
            "",
            "- 日期—间隔不一致是数据语义风险，不代表用户实际学习失败；不得据此批量改日期。",
            "- 真实到期集合与待校准库存必须分别核算；晨间行动队列只对已选分母承诺逐项完成，未选库存继续显式留在 baseline campaign。",
            "- 正式日期和掌握度只在真实复盘完成、且用户明确给分后更新。",
            "",
        ]
    )


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description="Audit the formal 408 review schedule without writing files.")
    parser.add_argument("--repo", default=str(ROOT))
    parser.add_argument("--date", default=dt.date.today().isoformat())
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)
    try:
        today = dt.date.fromisoformat(args.date)
    except ValueError:
        print("--date must be YYYY-MM-DD", file=sys.stderr)
        return 2
    try:
        report = audit(Path(args.repo).resolve(), today)
    except (OSError, ValueError) as exc:
        print(f"audit failed: {exc}", file=sys.stderr)
        return 2
    print(json.dumps(report, ensure_ascii=False, indent=2) if args.json else render_markdown(report))
    return 1 if report["hard_errors"] else 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
