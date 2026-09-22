#!/usr/bin/env python3
"""Activate per-question redo tracks after a verified mechanism baseline.

The command is dry-run by default.  It never assigns a mastery score.  ``--apply``
requires an open ``morning_review_queue_v2`` whose result row already records an
independent success for the requested primary review unit.  Eligible question
tracks are appended to that same open queue; tracks still inside the seven-day
protection window are scheduled for their earliest allowed date.
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import re
import shutil
import sys
import uuid
from pathlib import Path
from typing import Any

from generate_morning_review_queue import (
    is_separator,
    parse_date_strict,
    protect_location_text,
    read_markdown_table,
    split_md_row,
)
from intake_lock_408 import RepoLock
from rebuild_review_units_v2 import RebuildError, copy_path, remove_path, rollback_from_wal, write_atomic


ROOT = Path(__file__).resolve().parents[1]
RESULT_COLUMNS = {"Item", "首答", "是否提示", "修复再提取"}
RESULT_VALUES = {"independent_correct", "fragile_correct", "partial", "wrong", "blank"}
NO_HINT_VALUES = {"no", "none", "false", "0", "否", "无", "未使用"}
EMPTY_REPAIR_VALUES = {"", "none", "无", "待记录", "未记录", "not_applicable", "n/a"}
VALID_TRACK_ELIGIBILITY = {"eligible", "eligible_unrecorded_date", "protected_until"}


def locate_current_queue(repo: Path) -> Path:
    dashboard = repo / "wiki" / "study_vaults" / "408-full" / "StudyVault" / "00-Dashboard"
    pointer = dashboard / "当前晨间复盘.md"
    if not pointer.exists():
        raise RebuildError("missing 当前晨间复盘.md")
    match = re.search(r"\[\[([^]|]+)", pointer.read_text(encoding="utf-8"))
    if not match:
        raise RebuildError("current morning pointer has no queue link")
    queue = dashboard / f"{match.group(1)}.md"
    if not queue.exists():
        raise RebuildError(f"missing current queue: {queue}")
    return queue


def normalize_result_cell(value: str) -> str:
    return (value or "").strip().strip("`").lower()


def result_row_for_item(queue_text: str, item_id: str) -> dict[str, str]:
    """Return one structurally parsed result row for ``item_id``.

    Only Markdown tables whose first column is ``Item`` are considered.  This
    deliberately prevents status words in feedback, prose, or another column
    from satisfying the activation gate.
    """

    lines = queue_text.splitlines()
    matches: list[dict[str, str]] = []
    index = 0
    while index < len(lines):
        line = lines[index]
        if not line.strip().startswith("|"):
            index += 1
            continue
        headers = split_md_row(line)
        if (
            not headers
            or headers[0] != "Item"
            or index + 1 >= len(lines)
            or not lines[index + 1].strip().startswith("|")
            or not is_separator(split_md_row(lines[index + 1]))
        ):
            index += 1
            continue
        row_index = index + 2
        while row_index < len(lines) and lines[row_index].strip().startswith("|"):
            cells = split_md_row(lines[row_index])
            if len(cells) != len(headers):
                raise RebuildError(
                    f"malformed queue result row at line {row_index + 1}: "
                    f"expected {len(headers)} cells, got {len(cells)}"
                )
            row = dict(zip(headers, cells))
            if row.get("Item", "").strip().strip("`") == item_id:
                missing = sorted(RESULT_COLUMNS - set(headers))
                if missing:
                    raise RebuildError(
                        f"result table for {item_id} is missing required columns: {missing}"
                    )
                matches.append(row)
            row_index += 1
        index = row_index
    if not matches:
        raise RebuildError(f"queue has no structured result row for {item_id}")
    if len(matches) != 1:
        raise RebuildError(f"queue has duplicate structured result rows for {item_id}")
    return matches[0]


def require_final_unhinted_success(row: dict[str, str], review_unit_id: str, item_id: str) -> None:
    first_result = normalize_result_cell(row.get("首答", ""))
    repair_result = normalize_result_cell(row.get("修复再提取", ""))
    hint_state = normalize_result_cell(row.get("是否提示", ""))

    if first_result not in RESULT_VALUES:
        raise RebuildError(
            f"{review_unit_id} has invalid first result in {item_id}: {row.get('首答', '')!r}"
        )
    if repair_result in EMPTY_REPAIR_VALUES:
        final_result = first_result
    elif repair_result in RESULT_VALUES:
        final_result = repair_result
    else:
        raise RebuildError(
            f"{review_unit_id} has invalid repair result in {item_id}: "
            f"{row.get('修复再提取', '')!r}"
        )
    if final_result != "independent_correct" or hint_state not in NO_HINT_VALUES:
        raise RebuildError(
            f"{review_unit_id} final result in {item_id} is not an unhinted "
            "independent_correct; activation refused"
        )


def mechanism_item_for_unit(queue_text: str, review_unit_id: str) -> str:
    headings = list(re.finditer(r"(?m)^####\s+.*$", queue_text))
    matching_items: list[str] = []
    source_pattern = re.compile(
        rf"(?m)^- 来源复习单元：`{re.escape(review_unit_id)}`\s*$"
    )
    for index, heading in enumerate(headings):
        item_match = re.match(r"^####\s+(MQ-\d+)\s+", heading.group(0))
        if not item_match:
            continue
        section_end = headings[index + 1].start() if index + 1 < len(headings) else len(queue_text)
        section_text = queue_text[heading.start() : section_end]
        if source_pattern.search(section_text):
            matching_items.append(item_match.group(1))
    if not matching_items:
        raise RebuildError(f"queue does not contain mechanism unit: {review_unit_id}")
    if len(matching_items) != 1:
        raise RebuildError(f"queue contains duplicate mechanism unit: {review_unit_id}")
    return matching_items[0]


def verified_mechanism_success(queue_text: str, review_unit_id: str) -> tuple[str, str]:
    if "schema: morning_review_queue_v2" not in queue_text:
        raise RebuildError("activation requires morning_review_queue_v2")
    status = re.search(r"(?m)^status:\s*(\S+)", queue_text)
    if not status or status.group(1) not in {"open", "in_progress"}:
        raise RebuildError("queue must be open or in_progress before activation")
    review_date = re.search(r"(?m)^review_date:\s*(\d{4}-\d{2}-\d{2})", queue_text)
    if not review_date:
        raise RebuildError("queue is missing review_date")
    item_id = mechanism_item_for_unit(queue_text, review_unit_id)
    result_row = result_row_for_item(queue_text, item_id)
    require_final_unhinted_success(result_row, review_unit_id, item_id)
    return review_date.group(1), item_id


def table_document(path: Path) -> tuple[list[str], list[str], list[dict[str, str]]]:
    lines = path.read_text(encoding="utf-8").splitlines()
    header_index = next(
        (index for index, line in enumerate(lines) if line.startswith("|") and "正式节点ID" in line),
        None,
    )
    if header_index is None:
        raise RebuildError(f"missing original-track table header: {path}")
    headers = [cell.strip() for cell in lines[header_index].strip().strip("|").split("|")]
    rows = read_markdown_table(path)
    return lines[:header_index], headers, rows


def render_table(preamble: list[str], headers: list[str], rows: list[dict[str, str]]) -> str:
    output = list(preamble)
    output.append("| " + " | ".join(headers) + " |")
    output.append("|" + "|".join("---" for _ in headers) + "|")
    for row in rows:
        cells = [re.sub(r"\s+", " ", row.get(header, "")).strip().replace("|", "｜") for header in headers]
        output.append("| " + " | ".join(cells) + " |")
    output.append("")
    return "\n".join(output)


def update_tracks(
    rows: list[dict[str, str]], review_unit_id: str, review_date: dt.date
) -> tuple[list[dict[str, str]], list[dict[str, str]], list[dict[str, str]]]:
    matched = [row for row in rows if row.get("主复习单元ID") == review_unit_id]
    if not matched:
        raise RebuildError(f"no original tracks map to {review_unit_id}")
    due_now: list[dict[str, str]] = []
    protected: list[dict[str, str]] = []
    for row in matched:
        state = row.get("原题轨状态", "")
        if state == "blocked_needs_user":
            continue
        if state not in {"awaiting_primary_baseline", "active", "active_due", "active_scheduled"}:
            raise RebuildError(f"unknown original-track state: {row.get('正式节点ID')}={state}")
        node_id = row.get("正式节点ID") or "未标明节点"
        eligibility = (row.get("7天资格") or "").strip()
        if eligibility not in VALID_TRACK_ELIGIBILITY:
            raise RebuildError(f"{node_id}.7天资格 missing or invalid: {eligibility!r}")
        last = parse_date_strict(row.get("最近复做日期", ""), f"{node_id}.最近复做日期")
        earliest = parse_date_strict(row.get("最早允许日期", ""), f"{node_id}.最早允许日期")

        if last is None:
            if earliest is not None or eligibility != "eligible_unrecorded_date":
                raise RebuildError(
                    f"{node_id} has inconsistent 7-day gate: unrecorded last date requires "
                    "unrecorded earliest date and eligible_unrecorded_date"
                )
            row["7天资格"] = "eligible_unrecorded_date"
            row["原题轨状态"] = "active_due"
            row["下次原题复做日期"] = review_date.isoformat()
            row["激活条件"] = "主机制基线已通过；日期未记录，按 7 天外候选进入独立原题轨"
            due_now.append(row)
            continue

        if last > review_date:
            raise RebuildError(f"{node_id}.最近复做日期 is after review date")
        expected_earliest = last + dt.timedelta(days=7)
        if earliest is None:
            raise RebuildError(f"{node_id}.最早允许日期 is required when 最近复做日期 is recorded")
        if earliest != expected_earliest:
            raise RebuildError(
                f"{node_id} has inconsistent 7-day gate: 最早允许日期={earliest.isoformat()}, "
                f"expected {expected_earliest.isoformat()} from 最近复做日期+7"
            )
        expected_eligibility = "protected_until" if review_date < expected_earliest else "eligible"
        if eligibility != expected_eligibility:
            raise RebuildError(
                f"{node_id} has inconsistent 7-day eligibility: {eligibility!r}, "
                f"expected {expected_eligibility!r} on {review_date.isoformat()}"
            )

        row["最早允许日期"] = expected_earliest.isoformat()
        row["7天资格"] = expected_eligibility
        if review_date < expected_earliest:
            row["原题轨状态"] = "active_scheduled"
            row["下次原题复做日期"] = expected_earliest.isoformat()
            row["激活条件"] = f"主机制基线已通过；等待 7 天保护至 {expected_earliest.isoformat()}"
            protected.append(row)
        else:
            row["原题轨状态"] = "active_due"
            row["下次原题复做日期"] = review_date.isoformat()
            row["激活条件"] = "主机制基线已通过；本轮立即进入独立原题轨"
            due_now.append(row)
    return rows, due_now, protected


def append_due_tracks(queue_text: str, due_rows: list[dict[str, str]], event_id: str) -> str:
    new_rows = [row for row in due_rows if row.get("正式节点ID") not in queue_text]
    if not new_rows:
        return queue_text
    counts: dict[str, int] = {}
    for key in ["due_count", "included_count", "original_question_due_count"]:
        match = re.search(rf"(?m)^{key}:\s*(\d+)", queue_text)
        if not match:
            raise RebuildError(f"queue missing {key}")
        counts[key] = int(match.group(1))
        queue_text = re.sub(
            rf"(?m)^{key}:\s*\d+",
            f"{key}: {counts[key] + len(new_rows)}",
            queue_text,
            count=1,
        )
    old_total = counts["included_count"]
    new_total = old_total + len(new_rows)
    queue_text = queue_text.replace(
        f"`queue_coverage = 已首答数 / {old_total}`",
        f"`queue_coverage = 已首答数 / {new_total}`",
    ).replace(
        f"`criterion_coverage = 已至少一次无提示独立正确数 / {old_total}`",
        f"`criterion_coverage = 已至少一次无提示独立正确数 / {new_total}`",
    )
    lines = [
        "",
        f"## 原题轨即时激活增量 · {event_id}",
        "",
        f"> 主机制已在本轮独立成功；以下 {len(new_rows)} 道题已满足 7 天保护，立即加入同一 open 队列，不等到次日，也不按时长删项。",
        "",
    ]
    for index, row in enumerate(new_rows, start=1):
        item_id = f"AOQ-{event_id[-6:]}-{index:02d}"
        lines += [
            f"### {item_id} {row.get('正式节点ID')}",
            "",
            f"- 主机制单元：`{row.get('主复习单元ID')}`",
            f"- 来源 ID / 年份：{row.get('来源ID')} / {row.get('年份')}",
            f"- 最近复做 / 最早允许：{row.get('最近复做日期')} / {row.get('最早允许日期')}",
            f"- 定位：{row.get('定位标记')} / {row.get('段落号')}",
            f"- 完整来源位置：{protect_location_text(row.get('完整来源位置', ''))}",
            f"- 详情入口：{protect_location_text(row.get('详情入口', ''))}",
            "- 闭卷任务：打开原题但不展开答案或解析，独立完成判型、第一动作与完整求解。",
            "",
        ]
    lines += [
        "| Item | 首答 | 置信度 | 反应时 | 是否提示 | 核验 | 修复再提取 |",
        "|---|---|---|---|---|---|---|",
    ]
    for index, _ in enumerate(new_rows, start=1):
        item_id = f"AOQ-{event_id[-6:]}-{index:02d}"
        lines.append(f"| {item_id} | 待作答 | 待记录 | 待记录 | 待记录 | 详情核验 | 待记录 |")
    lines += [
        "",
        f"- 激活后总分母：{new_total}；本增量未完成前队列保持 open。",
        "",
    ]
    return queue_text.rstrip() + "\n" + "\n".join(lines)


def apply_transaction(
    repo: Path,
    tracks_path: Path,
    queue_path: Path,
    tracks_content: str,
    queue_content: str,
    receipt_path: Path,
    receipt: dict[str, Any],
) -> None:
    wal_path = receipt_path.parent / "original-track-activation-wal.json"
    with RepoLock(repo, timeout=30.0, timeout_error=RebuildError):
        rollback_from_wal(repo, wal_path)
        tx_id = str(receipt["event_id"])
        tx_root = receipt_path.parent / ".activation-transaction" / tx_id
        staging = tx_root / "staging"
        backup = tx_root / "backup"
        staging.mkdir(parents=True)
        backup.mkdir(parents=True)
        staged = [
            (tracks_path, staging / "原题复做轨总表.md", tracks_content),
            (queue_path, staging / queue_path.name, queue_content),
            (receipt_path, staging / receipt_path.name, json.dumps(receipt, ensure_ascii=False, indent=2)),
        ]
        for _, path, content in staged:
            path.write_text(content.rstrip() + "\n", encoding="utf-8")
        targets: list[dict[str, Any]] = []
        for index, (target, _, _) in enumerate(staged):
            backup_rel = Path(f"target-{index:03d}")
            if target.exists():
                copy_path(target, backup / backup_rel)
            targets.append(
                {
                    "target": str(target.relative_to(repo)),
                    "backup": str(Path("backup") / backup_rel),
                    "existed": target.exists(),
                }
            )
        wal = {
            "schema": "original_track_activation_wal_v1",
            "transaction_id": tx_id,
            "state": "applying",
            "tx_dir": str(tx_root.relative_to(repo)),
            "targets": targets,
        }
        write_atomic(wal_path, json.dumps(wal, ensure_ascii=False, indent=2))
        try:
            for target, staged_path, _ in staged:
                target.parent.mkdir(parents=True, exist_ok=True)
                remove_path(target)
                os.replace(staged_path, target)
            wal["state"] = "complete"
            write_atomic(wal_path, json.dumps(wal, ensure_ascii=False, indent=2))
            shutil.rmtree(tx_root)
        except BaseException:
            rollback_from_wal(repo, wal_path)
            raise


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description="Activate independent original-question tracks.")
    parser.add_argument("--repo", default=str(ROOT))
    parser.add_argument("--primary-unit", required=True)
    parser.add_argument("--queue")
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args(argv)
    repo = Path(args.repo).resolve()
    try:
        queue_path = Path(args.queue).resolve() if args.queue else locate_current_queue(repo)
        queue_path.relative_to(repo)
        queue_text = queue_path.read_text(encoding="utf-8")
        review_date_raw, item_id = verified_mechanism_success(queue_text, args.primary_unit)
        review_date = dt.date.fromisoformat(review_date_raw)
        tracks_path = repo / "原题复做轨总表.md"
        preamble, headers, rows = table_document(tracks_path)
        rows, due_now, protected = update_tracks(rows, args.primary_unit, review_date)
        tracks_content = render_table(preamble, headers, rows)
        event_id = f"ACT-{review_date_raw}-{uuid.uuid4().hex[:8]}"
        queue_content = append_due_tracks(queue_text, due_now, event_id)
        receipt = {
            "schema": "original_track_activation_receipt_v1",
            "event_id": event_id,
            "review_date": review_date_raw,
            "mechanism_item": item_id,
            "primary_unit": args.primary_unit,
            "activated_due_nodes": [row.get("正式节点ID") for row in due_now],
            "protected_scheduled_nodes": [row.get("正式节点ID") for row in protected],
            "mastery_modified": False,
        }
        if args.apply:
            receipt_path = (
                repo
                / "wiki"
                / "lint_reports"
                / "2026-07-10-复习单元重构"
                / "activation_receipts"
                / f"{event_id}.json"
            )
            apply_transaction(
                repo, tracks_path, queue_path, tracks_content, queue_content, receipt_path, receipt
            )
        print(json.dumps({"mode": "apply" if args.apply else "dry-run", **receipt}, ensure_ascii=False, indent=2))
        return 0
    except (OSError, ValueError, RebuildError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
