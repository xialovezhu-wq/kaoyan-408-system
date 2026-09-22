#!/usr/bin/env python3
"""Legacy 2026-07-04 backlog reshuffler (write mode permanently disabled).

Use ``audit_review_schedule.py`` for read-only diagnosis and
``generate_morning_review_queue.py`` for capacity-limited daily sampling.
"""

from __future__ import annotations

import argparse
from collections import Counter, defaultdict
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from pathlib import Path


NOTE = "2026-07-04 清账顺延"
TABLE = "复习单元总表.md"
REPORT = "复盘输出/2026-07-04回滚清账排程.md"
MAX_PER_DAY = 12


@dataclass
class Row:
    line_index: int
    cells: list[str]
    row: dict[str, str]
    score: float
    overdue_days: int
    new_date: date | None = None


def split_row(line: str) -> list[str]:
    return [cell.strip() for cell in line.strip().strip("|").split("|")]


def render_row(cells: list[str]) -> str:
    return "| " + " | ".join(cells) + " |"


def parse_date(value: str) -> date | None:
    try:
        return datetime.strptime(value.strip(), "%Y-%m-%d").date()
    except ValueError:
        return None


def int_value(value: str, default: int = 0) -> int:
    try:
        return int(value.strip())
    except ValueError:
        return default


def difficulty_weight(value: str) -> int:
    if value.startswith("C"):
        return 3
    if value.startswith("B"):
        return 2
    if value.startswith("A"):
        return 1
    return 2


def mastery_for_score(value: str) -> int:
    if value == "待评分":
        return 3
    return int_value(value, 3)


def priority_score(row: dict[str, str], today: date) -> tuple[float, int]:
    next_date = parse_date(row["下次复习日期"])
    overdue = max((today - next_date).days, 0) if next_date else 0
    score = (
        overdue * 3
        + int_value(row["错误次数"]) * 2
        + int_value(row["重要程度"]) * 2
        + difficulty_weight(row["难度等级"])
        - mastery_for_score(row["掌握度"])
    )
    return score, overdue


def parse_units(path: Path, today: date) -> tuple[list[str], list[str], list[Row]]:
    lines = path.read_text(encoding="utf-8").splitlines()
    header: list[str] | None = None
    rows: list[Row] = []
    for index, line in enumerate(lines):
        if not line.startswith("|"):
            continue
        cells = split_row(line)
        if cells and cells[0] == "复习单元ID":
            header = cells
            continue
        if not header or len(cells) != len(header):
            continue
        if all(set(cell) <= {"-", ":", " "} for cell in cells):
            continue
        row = dict(zip(header, cells))
        score, overdue = priority_score(row, today)
        rows.append(Row(index, cells, row, score, overdue))
    if header is None:
        raise ValueError("复习单元总表缺少表头")
    return lines, header, rows


def schedule(rows: list[Row], today: date) -> None:
    pending = sorted(
        [row for row in rows if row.row["掌握度"] == "待评分"],
        key=lambda row: (-row.score, -row.overdue_days, row.row["复习单元ID"]),
    )
    scored = sorted(
        [row for row in rows if row.row["掌握度"] != "待评分"],
        key=lambda row: (-row.score, -row.overdue_days, mastery_for_score(row.row["掌握度"]), row.row["复习单元ID"]),
    )

    for index, row in enumerate(pending):
        row.new_date = today + timedelta(days=index // MAX_PER_DAY)

    start_day = today + timedelta(days=(len(pending) + MAX_PER_DAY - 1) // MAX_PER_DAY)
    for index, row in enumerate(scored):
        row.new_date = start_day + timedelta(days=index // MAX_PER_DAY)


def update_table(lines: list[str], header: list[str], rows: list[Row]) -> str:
    next_idx = header.index("下次复习日期")
    note_idx = header.index("备注")
    for row in rows:
        assert row.new_date is not None
        row.cells[next_idx] = row.new_date.isoformat()
        if NOTE not in row.cells[note_idx]:
            row.cells[note_idx] = f"{row.cells[note_idx]}；{NOTE}" if row.cells[note_idx] else NOTE
        lines[row.line_index] = render_row(row.cells)
    return "\n".join(lines) + "\n"


def bucket(rows: list[Row]) -> dict[date, list[Row]]:
    result: dict[date, list[Row]] = defaultdict(list)
    for row in rows:
        assert row.new_date is not None
        result[row.new_date].append(row)
    return dict(result)


def priority_class(row: Row) -> str:
    mastery = row.row["掌握度"]
    if row.row["类型"] == "错题" or mastery in {"0", "1", "2", "待评分"} or row.overdue_days >= 14:
        return "必须完成"
    if mastery == "3" or int_value(row.row["重要程度"]) >= 4:
        return "尽量完成"
    return "有空再做"


def report(rows: list[Row], today: date) -> str:
    by_day = bucket(rows)
    lines: list[str] = [
        "# 2026-07-04 408 回滚清账排程",
        "",
        "本排程只服务 408 回滚复习清账；未修改掌握度、错误次数或当前间隔天数。掌握度仍需用户复习后按 0-5 分给出。",
        "",
        "## 清账规则",
        "",
        f"- 总复习单元：{len(rows)}",
        f"- 每日上限：{MAX_PER_DAY} 个",
        "- 待评分单元优先排入前三天",
        f"- 所有单元备注追加：{NOTE}",
        "",
        "## 未来两周铺排总表",
        "",
        "| 日期 | 单元数 | 待评分 | 掌握度0-2 | 掌握度3 | 掌握度4-5 | 主要科目 |",
        "|---|---:|---:|---:|---:|---:|---|",
    ]
    for offset in range(14):
        day = today + timedelta(days=offset)
        day_rows = by_day.get(day, [])
        mastery = Counter(row.row["掌握度"] for row in day_rows)
        subjects = Counter(row.row["科目"] for row in day_rows)
        subject_text = "；".join(f"{k}{v}" for k, v in subjects.most_common()) or "无"
        low = sum(mastery[str(i)] for i in range(3))
        high = mastery["4"] + mastery["5"]
        lines.append(
            f"| {day.isoformat()} | {len(day_rows)} | {mastery['待评分']} | {low} | {mastery['3']} | {high} | {subject_text} |"
        )

    first_day = sorted(by_day.get(today, []), key=lambda row: (-row.score, row.row["复习单元ID"]))
    by_class: dict[str, list[Row]] = defaultdict(list)
    for row in first_day:
        by_class[priority_class(row)].append(row)

    lines += [
        "",
        "## 2026-07-04 三档执行方案",
        "",
        "### 15 分钟",
        "",
        "优先处理排在最前的 3 个待评分单元。每个单元只做闭卷核心回忆，复习后立刻给掌握度。",
        "",
    ]
    for row in first_day[:3]:
        lines.append(f"- {row.row['复习单元ID']}：{row.row['内容名称']}（{row.row['科目']}，{row.row['类型']}）")

    lines += [
        "",
        "### 30 分钟",
        "",
        "完成前 6 个单元。先按复习方式做最小闭卷动作，再记录 0-5 分。",
        "",
    ]
    for row in first_day[:6]:
        lines.append(f"- {row.row['复习单元ID']}：{row.row['内容名称']}（掌握度：{row.row['掌握度']}）")

    lines += [
        "",
        "### 45 分钟",
        "",
        "完成当天 12 个单元。若时间不足，先做“必须完成”，再做“尽量完成”。",
        "",
    ]
    for name in ["必须完成", "尽量完成", "有空再做"]:
        lines.append(f"#### {name}")
        items = by_class.get(name, [])
        if not items:
            lines.append("- 无")
        else:
            for row in items:
                lines.append(f"- {row.row['复习单元ID']}：{row.row['内容名称']}（{row.row['科目']}）")
        lines.append("")

    lines += [
        "## 待评分提醒",
        "",
        "前三天安排的待评分单元必须在复习后由用户给出 0-5 分；本次清账没有代填掌握度。",
        "",
    ]
    return "\n".join(lines)


def validate(header: list[str], before: list[Row], after_rows: list[Row]) -> list[str]:
    issues: list[str] = []
    immutable = ["当前间隔天数", "掌握度", "错误次数"]
    before_by_id = {row.row["复习单元ID"]: row.row for row in before}
    after_by_id = {row.row["复习单元ID"]: row.row for row in after_rows}
    if set(before_by_id) != set(after_by_id):
        issues.append("复习单元ID集合变化")
    for unit_id, old in before_by_id.items():
        new = after_by_id.get(unit_id)
        if not new:
            continue
        for field in immutable:
            if old[field] != new[field]:
                issues.append(f"{unit_id} {field} 被修改")
    for day, count in Counter(row.new_date for row in before).items():
        if count > MAX_PER_DAY:
            issues.append(f"{day} 单日负载 {count} 超过 {MAX_PER_DAY}")
    pending_days = {row.new_date for row in before if row.row["掌握度"] == "待评分"}
    if pending_days - {date(2026, 7, 4), date(2026, 7, 5), date(2026, 7, 6)}:
        issues.append("待评分单元未全部排进前三天")
    return issues


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--repo", default=".")
    parser.add_argument("--date", default="2026-07-04")
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()

    if args.apply:
        print(
            "ERROR: legacy bulk rescheduling is disabled; it may rewrite formal dates before validation. "
            "Use audit_review_schedule.py and update dates only after an actual scored review."
        )
        return 2
    print("mode=legacy-read-only")
    print("legacy scheduler disabled; use scripts/audit_review_schedule.py")
    return 0

    repo = Path(args.repo)
    today = datetime.strptime(args.date, "%Y-%m-%d").date()
    path = repo / TABLE
    lines, header, rows = parse_units(path, today)
    before_snapshot = [Row(row.line_index, row.cells[:], row.row.copy(), row.score, row.overdue_days, row.new_date) for row in rows]
    schedule(rows, today)
    new_text = update_table(lines[:], header, rows)
    report_text = report(rows, today)

    if args.apply:
        path.write_text(new_text, encoding="utf-8")
        report_path = repo / REPORT
        report_path.parent.mkdir(parents=True, exist_ok=True)
        report_path.write_text(report_text.rstrip() + "\n", encoding="utf-8")

    _, _, projected = parse_units_from_text(new_text, today)
    issues = validate(header, rows, projected)
    by_day = Counter(row.new_date for row in rows)
    print(f"mode={'apply' if args.apply else 'dry-run'}")
    print(f"review_units={len(rows)}")
    print(f"pending_score={sum(1 for row in rows if row.row['掌握度'] == '待评分')}")
    print(f"days_used={len(by_day)}")
    print(f"max_daily_load={max(by_day.values()) if by_day else 0}")
    print(f"first_day={min(by_day).isoformat() if by_day else '无'}")
    print(f"last_day={max(by_day).isoformat() if by_day else '无'}")
    print(f"validation_issues={len(issues)}")
    for issue in issues:
        print(f"ISSUE {issue}")
    if issues:
        return 2
    return 0


def parse_units_from_text(text: str, today: date) -> tuple[list[str], list[str], list[Row]]:
    lines = text.splitlines()
    header: list[str] | None = None
    rows: list[Row] = []
    for index, line in enumerate(lines):
        if not line.startswith("|"):
            continue
        cells = split_row(line)
        if cells and cells[0] == "复习单元ID":
            header = cells
            continue
        if not header or len(cells) != len(header):
            continue
        if all(set(cell) <= {"-", ":", " "} for cell in cells):
            continue
        row = dict(zip(header, cells))
        score, overdue = priority_score(row, today)
        rows.append(Row(index, cells, row, score, overdue))
    if header is None:
        raise ValueError("复习单元总表缺少表头")
    return lines, header, rows


if __name__ == "__main__":
    raise SystemExit(main())
