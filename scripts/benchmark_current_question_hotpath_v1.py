#!/usr/bin/env python3
"""Temp-only acceptance benchmark for current-question 408 hot turns."""

from __future__ import annotations

import argparse
import json
import statistics
import tempfile
import time
from pathlib import Path
from typing import Any
from unittest import mock

import managed_408_current_turn as current_turn
import capture_hot_writer_408 as capture_hot


SCHEMA = "current-question-hotpath-benchmark-v1"
SCENARIOS = (
    ("correct_high", "correct", "high", "not_observed", 1),
    ("correct_medium", "correct", "medium", "not_observed", 1),
    ("correct_low", "correct", "low", "not_observed", 1),
    ("incorrect", "incorrect", "high", "not_observed", 1),
    ("partial", "partial", "high", "partial", 1),
    ("blank", "blank", "low", "not_observed", 1),
    ("uncertain", "uncertain", "medium", "not_observed", 1),
)


def _context(
    ordinal: int,
    *,
    choice_result: str,
    confidence: str,
    reasoning_result: str,
) -> dict[str, Any]:
    event_time = "2026-08-04T08:10:00+08:00"
    first_action = "先检查当前条件"
    learner_choice = "BLANK" if choice_result in {"blank", "uncertain"} else "A"
    return {
        "schema": "current-question-context-v1",
        "source": "ordinary_question",
        "request_id": f"benchmark-current-{ordinal:05d}",
        "session_id": "benchmark-current-session",
        "item_id": f"BQ-{ordinal:05d}",
        "source_id": f"CURRENT-Q-{ordinal:05d}",
        "source_stable": True,
        "source_binding_sha256": f"{ordinal:064x}"[-64:],
        "grader_capsule_id": f"benchmark-grader-{ordinal:05d}",
        "event_time": event_time,
        "study_date": "2026-08-04",
        "timezone": "Asia/Shanghai",
        "idempotency_key": f"benchmark-current:{ordinal}:first",
        "display_receipt_sha256": None,
        "question_valid": True,
        "question_evidence": {
            "public_text": "仅供基准的当前题公开表面",
            "options": ["甲", "乙", "丙", "丁"],
            "response_instruction": "回答当前题",
            "public_surface_sha256": "a" * 64,
            "attachment_sha256s": [],
        },
        "learner_evidence": {
            "answer_text": "" if learner_choice == "BLANK" else "当前作答",
            "choice": learner_choice,
            "confidence": confidence,
            "first_action": first_action,
            "reasoning": "只处理当前条件",
            "prompt_level": "none",
            "observed_at": event_time,
        },
        "assessment": {
            "choice_result": choice_result,
            "reasoning_result": reasoning_result,
            "confidence": confidence,
            "prompt_level": "none",
            "first_break": "未观察到",
            "first_break_provenance": "not_observed",
            "first_action": first_action,
            "first_action_provenance": "user_report",
        },
        "formal_write_count": 0,
    }


def _private_evaluation(choice_result: str) -> dict[str, str]:
    result = {
        "grader_basis": "当前题私有核验",
        "grader_result": "blank" if choice_result == "blank" else choice_result,
    }
    if choice_result in {"correct", "incorrect"}:
        result["correct_option"] = "A" if choice_result == "correct" else "B"
    return result


def _capture_result(payload: dict[str, Any]) -> dict[str, Any]:
    return {
        "status": "CAPTURED",
        "quality_status": "awaiting_sol_formalization",
        "capture_id": capture_hot.canonical_fact_event_from_payload(payload)[
            "capture_id"
        ],
        "receipt_sha256": "b" * 64,
        "joint_verification": {"status": "PASS"},
        "formal_write_count": 0,
    }


def run_benchmark(*, iterations: int = 3) -> dict[str, Any]:
    if iterations < 1 or iterations > 100:
        raise ValueError("iterations must be between 1 and 100")
    timings: list[float] = []
    rows: list[dict[str, Any]] = []
    failures: list[str] = []
    ordinal = 0
    with tempfile.TemporaryDirectory(prefix="current-question-benchmark-") as tmp:
        repo = Path(tmp) / "repo"
        private_root = Path(tmp) / "private"
        repo.mkdir()
        for _ in range(iterations):
            for name, choice, confidence, reasoning, expected_capture in SCENARIOS:
                ordinal += 1
                capture_calls = 0

                def commit(_repo: Path, _payload: dict[str, Any]) -> dict[str, Any]:
                    nonlocal capture_calls
                    capture_calls += 1
                    return _capture_result(_payload)

                first = {
                    "commit_receipt_sha256": "c" * 64,
                    "verification": {"status": "verified"},
                }
                started = time.perf_counter()
                with (
                    mock.patch.object(
                        current_turn, "_record_first_answer", return_value=first
                    ),
                    mock.patch.object(current_turn, "_commit_capture", side_effect=commit),
                ):
                    result = current_turn.run_current_question_turn(
                        repo,
                        _context(
                            ordinal,
                            choice_result=choice,
                            confidence=confidence,
                            reasoning_result=reasoning,
                        ),
                        feedback_text="只针对当前题的冻结反馈。",
                        private_evaluation=_private_evaluation(choice),
                        private_root=private_root,
                    )
                elapsed_ms = (time.perf_counter() - started) * 1000
                timings.append(elapsed_ms)
                row = {
                    "scenario": name,
                    "elapsed_ms": round(elapsed_ms, 3),
                    "capture_calls": capture_calls,
                    "capture_status": result["capture_status"],
                    "feedback_authorized": result["feedback_authorized"],
                    "next_item_published": result["next_item_published"],
                    "personalization_read_count": result["personalization_read_count"],
                    "luna_call_count": result["luna_call_count"],
                    "formal_write_count": result["formal_write_count"],
                }
                rows.append(row)
                if capture_calls != expected_capture:
                    failures.append(f"{name}:capture_boundary")
                if expected_capture and result["capture_status"] != "awaiting_sol_formalization":
                    failures.append(f"{name}:capture_terminal")
                if not expected_capture and result["capture_status"] != "not_eligible":
                    failures.append(f"{name}:unexpected_capture")
                if result["next_item_published"]:
                    failures.append(f"{name}:same_turn_next")
                if result["personalization_read_count"] or result["luna_call_count"]:
                    failures.append(f"{name}:forbidden_hot_dependency")

        failure_context = _context(
            ordinal + 1,
            choice_result="incorrect",
            confidence="high",
            reasoning_result="not_observed",
        )
        with (
            mock.patch.object(
                current_turn,
                "_record_first_answer",
                return_value={
                    "commit_receipt_sha256": "c" * 64,
                    "verification": {"status": "verified"},
                },
            ),
            mock.patch.object(
                current_turn,
                "_commit_capture",
                side_effect=current_turn.ManagedCurrentTurnError("injected"),
            ),
        ):
            isolated = current_turn.run_current_question_turn(
                repo,
                failure_context,
                feedback_text="capture 失败也不扣留当前反馈。",
                private_evaluation={"correct_option": "B", "grader_basis": "核验"},
                private_root=private_root,
            )
        if not isolated["feedback_authorized"] or isolated["capture_status"] != "capture_pending_recovery":
            failures.append("capture_failure_isolation")
        if isolated["advance_allowed"]:
            failures.append("capture_failure_advanced")

    p95 = sorted(timings)[max(0, int(len(timings) * 0.95) - 1)]
    return {
        "schema": SCHEMA,
        "status": "pass" if not failures else "fail",
        "iterations_per_scenario": iterations,
        "scenario_count": len(SCENARIOS),
        "sample_count": len(rows),
        "latency_ms": {
            "median": round(statistics.median(timings), 3),
            "p95": round(p95, 3),
            "maximum": round(max(timings), 3),
        },
        "capture_failure_isolated": isolated["feedback_authorized"] is True,
        "same_turn_next_item_count": sum(
            1 for row in rows if row["next_item_published"]
        ),
        "personalization_read_count": sum(
            row["personalization_read_count"] for row in rows
        ),
        "luna_call_count": sum(row["luna_call_count"] for row in rows),
        "formal_write_count": sum(row["formal_write_count"] for row in rows),
        "failures": sorted(set(failures)),
        "rows": rows,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--iterations", type=int, default=3)
    args = parser.parse_args()
    result = run_benchmark(iterations=args.iterations)
    print(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True))
    return 0 if result["status"] == "pass" else 2


if __name__ == "__main__":
    raise SystemExit(main())
