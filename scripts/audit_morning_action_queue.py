#!/usr/bin/env python3
"""Read-only integrity audit for a ``morning_review_action_queue_v3`` file."""

from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import re
import sys
from collections import Counter
from pathlib import Path

from generate_morning_action_queue import (
    classify_units,
    compound_source_hash,
    render_action_queue,
)
from generate_morning_review_queue import load_original_tracks, load_units
from review_feedback_loop import LOOP_REL, MORNING_REL, audit as audit_review_loop
from review_feedback_loop import scheduling_drift_audit


ROOT = Path(__file__).resolve().parents[1]
INT_FIELDS = {
    "target_minutes",
    "max_items",
    "baseline_quota",
    "max_original",
    "inventory_count",
    "formal_due_count",
    "baseline_backlog_count",
    "original_question_due_count",
    "derived_d0_count",
    "scheduled_count",
    "selected_formal_due_count",
    "selected_baseline_count",
    "selected_original_question_count",
    "selected_derived_d0_count",
    "same_day_extension_count",
    "baseline_remaining_count",
    "blocked_count",
}
PROTECTED_RE = re.compile(
    r"(?:正确答案|标准答案|错误答案|错选答案|我的答案|我的错选|用户此前答案|选项字母)"
)


def parse_frontmatter(text: str) -> dict[str, str]:
    if not text.startswith("---\n"):
        raise ValueError("queue is missing YAML frontmatter")
    end = text.find("\n---\n", 4)
    if end < 0:
        raise ValueError("queue frontmatter is not closed")
    result: dict[str, str] = {}
    for line in text[4:end].splitlines():
        if ":" not in line:
            continue
        key, value = line.split(":", 1)
        result[key.strip()] = value.strip()
    return result


def locate_current_queue(repo: Path) -> Path:
    dashboard = repo / "wiki" / "study_vaults" / "408-full" / "StudyVault" / "00-Dashboard"
    pointer = dashboard / "当前晨间复盘.md"
    if not pointer.exists():
        raise ValueError("missing 当前晨间复盘.md")
    match = re.search(r"\[\[([^]|]+)", pointer.read_text(encoding="utf-8"))
    if not match:
        raise ValueError("current morning pointer has no queue link")
    queue = dashboard / f"{match.group(1)}.md"
    if not queue.exists():
        raise ValueError(f"current action queue does not exist: {queue}")
    return queue


def audit(repo: Path, queue: Path) -> dict[str, object]:
    text = queue.read_text(encoding="utf-8")
    frontmatter = parse_frontmatter(text)
    errors: list[str] = []
    if frontmatter.get("schema") != "morning_review_action_queue_v3":
        errors.append("queue schema is not morning_review_action_queue_v3")

    integers: dict[str, int] = {}
    for field in sorted(INT_FIELDS):
        raw = frontmatter.get(field)
        try:
            integers[field] = int(raw or "")
        except ValueError:
            errors.append(f"invalid or missing integer field: {field}={raw!r}")

    review_date: dt.date | None = None
    try:
        review_date = dt.date.fromisoformat(frontmatter.get("review_date", ""))
    except ValueError:
        errors.append("invalid review_date")

    def number(name: str) -> int:
        return integers.get(name, -1)

    if number("inventory_count") != (
        number("formal_due_count")
        + number("baseline_backlog_count")
        + number("original_question_due_count")
        + number("derived_d0_count")
    ):
        errors.append("inventory_count does not close over source inventories")
    if number("scheduled_count") != (
        number("selected_formal_due_count")
        + number("selected_baseline_count")
        + number("selected_original_question_count")
        + number("selected_derived_d0_count")
    ):
        errors.append("scheduled_count does not close over selected inventories")
    if number("baseline_remaining_count") != (
        number("baseline_backlog_count") - number("selected_baseline_count")
    ):
        errors.append("baseline_remaining_count is inconsistent")
    expected_extension = (
        number("formal_due_count")
        - number("selected_formal_due_count")
        + number("original_question_due_count")
        - number("selected_original_question_count")
        + number("derived_d0_count")
        - number("selected_derived_d0_count")
    )
    if number("same_day_extension_count") != expected_extension:
        errors.append("same_day_extension_count is inconsistent")

    headings = re.findall(r"(?m)^####\s+(MQ-[A-Za-z0-9._-]+)\s+", text)
    result_rows = re.findall(r"(?m)^\|\s*(MQ-[A-Za-z0-9._-]+)\s*\|", text)
    source_ids = re.findall(r"(?m)^- source_id:\s*`([^`]+)`\s*$", text)
    action_ids = re.findall(r"(?m)^- action_id:\s*`([^`]+)`\s*$", text)
    for label, values in [
        ("heading Item", headings),
        ("result Item", result_rows),
        ("source_id", source_ids),
    ]:
        duplicates = sorted(item for item, count in Counter(values).items() if count > 1)
        if duplicates:
            errors.append(f"duplicate {label}: {duplicates[:10]}")
    scheduled = number("scheduled_count")
    if len(headings) != scheduled:
        errors.append(f"scheduled_count={scheduled} but headings={len(headings)}")
    if len(result_rows) != scheduled:
        errors.append(f"scheduled_count={scheduled} but result_rows={len(result_rows)}")
    if len(source_ids) != scheduled:
        errors.append(f"scheduled_count={scheduled} but source_ids={len(source_ids)}")
    if headings != result_rows:
        errors.append("heading Item order differs from result-table Item order")

    scheduling_report = scheduling_drift_audit(repo)
    answered_pending = {
        str(row["action_id"])
        for row in scheduling_report.get("gaps") or []
        if row.get("action_status") == "pending"
        and row.get("status") != "reconciled"
    }
    leaked_scheduling_gaps = sorted(answered_pending.intersection(action_ids))
    if leaked_scheduling_gaps:
        errors.append(
            "queue contains answered-but-pending actions: "
            + ", ".join(leaked_scheduling_gaps[:20])
        )
    try:
        queue_scheduling_gap_count = int(
            frontmatter.get("scheduling_gap_count", "0")
        )
    except ValueError:
        queue_scheduling_gap_count = -1
        errors.append("invalid scheduling_gap_count")
    if queue_scheduling_gap_count != len(answered_pending):
        errors.append(
            "scheduling_gap_count drift: "
            f"queue={queue_scheduling_gap_count} live={len(answered_pending)}"
        )

    if "[!answer]" in text:
        errors.append("action queue embeds an answer callout; feedback gate is bypassed")
    protected = sorted(set(PROTECTED_RE.findall(text)))
    if protected:
        errors.append(f"protected answer labels found: {protected}")

    runtime_path = repo / LOOP_REL / "runtime.json"
    runtime: dict[str, object] | None = None
    session_bound = False
    if runtime_path.is_file():
        try:
            loaded_runtime = json.loads(runtime_path.read_text(encoding="utf-8"))
            runtime = loaded_runtime if isinstance(loaded_runtime, dict) else None
        except json.JSONDecodeError:
            errors.append("review-loop runtime.json is corrupt")

    queue_rel = str(queue.resolve().relative_to(repo.resolve()))
    current_hash = hashlib.sha256(queue.read_bytes()).hexdigest()
    bound_sessions: list[dict[str, object]] = []
    sessions_root = repo / MORNING_REL
    if sessions_root.is_dir():
        for state_path in sorted(sessions_root.glob("*/state.json")):
            try:
                session_state = json.loads(state_path.read_text(encoding="utf-8"))
            except json.JSONDecodeError:
                continue
            if (
                isinstance(session_state, dict)
                and session_state.get("schema") == "morning_review_session_state_v1"
                and session_state.get("queue_path") == queue_rel
            ):
                bound_sessions.append(session_state)

    if bound_sessions:
        session_bound = True
        drifted = sorted(
            str(state.get("session_id", "unknown"))
            for state in bound_sessions
            if state.get("queue_sha256") != current_hash
        )
        if drifted:
            errors.append(
                "session-bound queue hash drift: " + ", ".join(drifted)
            )
    elif (
        runtime
        and runtime.get("queue_path") == queue_rel
        and runtime.get("active_session_id")
    ):
        errors.append("runtime points to an unreadable or mismatched active session")

    if review_date is not None and not session_bound:
        live_hash = compound_source_hash(repo, review_date)
        if frontmatter.get("source_hash") != live_hash:
            errors.append(
                f"source_hash drift: queue={frontmatter.get('source_hash')} live={live_hash}"
            )
        try:
            expected = render_action_queue(
                repo,
                review_date,
                max_items=number("max_items"),
                baseline_quota=number("baseline_quota"),
                max_original=number("max_original"),
                target_minutes=number("target_minutes"),
            ).rstrip() + "\n"
            if text != expected:
                errors.append("queue content differs from the current deterministic projection")
        except (OSError, ValueError) as exc:
            errors.append(f"could not rebuild deterministic projection: {exc}")

        _, _, blocked_units = classify_units(load_units(repo), review_date)
        blocked_original = [
            item
            for item in load_original_tracks(repo)
            if item.state.startswith("blocked")
        ]
        live_blocked = len(blocked_units) + len(blocked_original)
        if number("blocked_count") != live_blocked:
            errors.append(
                f"blocked_count drift: queue={number('blocked_count')} live={live_blocked}"
            )

    loop_report: dict[str, object] | None = None
    if runtime_path.is_file():
        loop_report = audit_review_loop(repo)
        if loop_report.get("status") != "PASS":
            errors.append("review-loop state audit failed")

    return {
        "schema": "morning_review_action_queue_audit_v1",
        "queue": str(queue),
        "status": "PASS" if not errors else "FAIL",
        "scheduled_count": scheduled,
        "heading_count": len(headings),
        "result_row_count": len(result_rows),
        "source_id_count": len(source_ids),
        "inventory_count": number("inventory_count"),
        "baseline_backlog_count": number("baseline_backlog_count"),
        "same_day_extension_count": number("same_day_extension_count"),
        "scheduling_gap_count": queue_scheduling_gap_count,
        "answered_pending_action_ids": sorted(answered_pending),
        "protected_findings": protected,
        "session_bound": session_bound,
        "runtime_status": runtime.get("status") if runtime else None,
        "review_loop_status": loop_report.get("status") if loop_report else None,
        "errors": errors,
    }


def render_markdown(report: dict[str, object]) -> str:
    return "\n".join(
        [
            "# 晨间行动队列只读审计",
            "",
            f"- 状态：{report['status']}",
            f"- scheduled / headings / result rows / sources：{report['scheduled_count']} / {report['heading_count']} / {report['result_row_count']} / {report['source_id_count']}",
            f"- inventory / baseline / extension：{report['inventory_count']} / {report['baseline_backlog_count']} / {report['same_day_extension_count']}",
            f"- errors：{len(report['errors'])}",
            "",
        ]
        + [f"- {item}" for item in report["errors"]]
    )


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo", default=str(ROOT))
    parser.add_argument("--queue")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)
    repo = Path(args.repo).resolve()
    try:
        queue = (
            Path(args.queue).resolve()
            if args.queue
            else locate_current_queue(repo)
        )
        queue.relative_to(repo)
        report = audit(repo, queue)
    except (OSError, ValueError) as exc:
        print(f"action queue audit failed: {exc}", file=sys.stderr)
        return 2
    print(
        json.dumps(report, ensure_ascii=False, indent=2)
        if args.json
        else render_markdown(report)
    )
    return 0 if report["status"] == "PASS" else 1


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
