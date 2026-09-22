#!/usr/bin/env python3
"""Evaluate conservative 408 commonality matching on a checked-in curated set."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

from intake_lib_408 import scan_leak
from personalization_commonality_408 import (
    build_recent_signature_index,
    match_recent_signatures,
    match_verified_cross_recent_signatures,
)


SCHEMA = "personalization_commonality_benchmark_v1"
REPORT_SCHEMA = "personalization_commonality_evaluation_v1"
STRONG_BASES = {
    "exact_question",
    "exact_point",
    "same_mechanism",
    "same_error_pattern",
    "same_method",
    "verified_cross_subject",
}


class EvaluationError(RuntimeError):
    pass


def _load(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise EvaluationError("benchmark_unreadable") from exc
    if not isinstance(value, dict) or value.get("schema") != SCHEMA:
        raise EvaluationError("benchmark_schema_invalid")
    cases = value.get("cases")
    if not isinstance(cases, list) or not cases:
        raise EvaluationError("benchmark_cases_missing")
    return value


def _evaluate_case(case: dict[str, Any], as_of: str) -> dict[str, Any]:
    rows = case.get("history")
    if not isinstance(rows, list):
        raise EvaluationError("case_history_invalid")
    events: list[dict[str, Any]] = []
    mapping_rows: dict[str, dict[str, Any]] = {}
    for row in rows:
        if not isinstance(row, dict) or not isinstance(row.get("event"), dict) or not isinstance(row.get("mapping"), dict):
            raise EvaluationError("case_history_invalid")
        event = row["event"]
        event_id = str(event.get("event_id") or "")
        if not event_id or event_id in mapping_rows:
            raise EvaluationError("case_event_identity_invalid")
        events.append(event)
        mapping_rows[event_id] = row["mapping"]
    index = build_recent_signature_index(events, {"events": mapping_rows}, as_of)
    target = case.get("target")
    if not isinstance(target, dict):
        raise EvaluationError("case_target_invalid")
    direct, background = match_recent_signatures(index, target, limit=5)
    cross = match_verified_cross_recent_signatures(
        index,
        case.get("cross_rows") if isinstance(case.get("cross_rows"), list) else [],
        limit=5,
    )
    predicted_rows = [
        row
        for row in [*direct, *cross]
        if row.get("match_basis") in STRONG_BASES
        and row.get("usable_for_personal_claim") is True
    ][:5]
    predicted = [
        str(row.get("evidence_event_id"))
        for row in predicted_rows
        if row.get("evidence_event_id")
    ]
    expected = [str(value) for value in case.get("expected_relevant_event_ids", [])]
    expected_basis = case.get("expected_match_basis") or {}
    basis_errors = [
        event_id
        for event_id, basis in expected_basis.items()
        if next(
            (
                row.get("match_basis")
                for row in predicted_rows
                if row.get("evidence_event_id") == event_id
            ),
            None,
        )
        != basis
    ]
    serialized = json.dumps(predicted_rows, ensure_ascii=False, sort_keys=True)
    fails, warns = scan_leak(serialized)
    expected_set = set(expected)
    predicted_set = set(predicted)
    return {
        "case_id": str(case.get("case_id") or ""),
        "category": str(case.get("category") or ""),
        "subjects": list(case.get("subjects") or []),
        "predicted_event_ids": predicted,
        "expected_event_ids": expected,
        "true_positive_count": len(predicted_set & expected_set),
        "false_positive_count": len(predicted_set - expected_set),
        "miss_count": len(expected_set - predicted_set),
        "basis_errors": basis_errors,
        "background_match_bases": [row.get("match_basis") for row in background],
        "answer_safety_findings": sorted(set([*fails, *warns])),
        "negative_case": not expected,
        "visibility_case": case.get("category") == "next_turn_visibility",
    }


def evaluate(path: str | Path) -> dict[str, Any]:
    benchmark_path = Path(path).expanduser().resolve()
    benchmark = _load(benchmark_path)
    as_of = str(benchmark.get("as_of") or "")
    case_results = [_evaluate_case(case, as_of) for case in benchmark["cases"]]
    true_positive = sum(row["true_positive_count"] for row in case_results)
    false_positive = sum(row["false_positive_count"] for row in case_results)
    misses = sum(row["miss_count"] for row in case_results)
    negative = [row for row in case_results if row["negative_case"]]
    negative_false = [row for row in negative if row["false_positive_count"]]
    visibility = [row for row in case_results if row["visibility_case"]]
    precision = true_positive / max(1, true_positive + false_positive)
    recall_at_5 = true_positive / max(1, true_positive + misses)
    no_relation_false_positive_rate = len(negative_false) / max(1, len(negative))
    visibility_rate = sum(row["miss_count"] == 0 for row in visibility) / max(
        1, len(visibility)
    )
    answer_leak_count = sum(bool(row["answer_safety_findings"]) for row in case_results)
    basis_error_count = sum(len(row["basis_errors"]) for row in case_results)
    subjects = sorted(
        {
            subject
            for case in benchmark["cases"]
            for subject in case.get("subjects", [])
        }
    )
    thresholds = {
        "strong_precision_min": 0.95,
        "recall_at_5_min": 0.85,
        "no_relation_false_positive_rate_max": 0.05,
        "next_turn_visibility_min": 1.0,
        "answer_leak_count_max": 0,
        "basis_error_count_max": 0,
    }
    metrics = {
        "strong_precision": precision,
        "recall_at_5": recall_at_5,
        "no_relation_false_positive_rate": no_relation_false_positive_rate,
        "next_turn_visibility_rate": visibility_rate,
        "answer_leak_count": answer_leak_count,
        "basis_error_count": basis_error_count,
    }
    passed = all(
        (
            precision >= thresholds["strong_precision_min"],
            recall_at_5 >= thresholds["recall_at_5_min"],
            no_relation_false_positive_rate
            <= thresholds["no_relation_false_positive_rate_max"],
            visibility_rate >= thresholds["next_turn_visibility_min"],
            answer_leak_count <= thresholds["answer_leak_count_max"],
            basis_error_count <= thresholds["basis_error_count_max"],
            subjects == ["CN", "CO", "DS", "OS"],
        )
    )
    return {
        "schema": REPORT_SCHEMA,
        "status": "PASS" if passed else "FAIL",
        "benchmark_path": str(benchmark_path),
        "annotation_kind": benchmark.get("annotation_kind"),
        "human_label_audit_complete": benchmark.get("human_label_audit_complete")
        is True,
        "case_count": len(case_results),
        "subjects_covered": subjects,
        "metrics": metrics,
        "thresholds": thresholds,
        "counts": {
            "true_positive": true_positive,
            "false_positive": false_positive,
            "miss": misses,
            "negative_cases": len(negative),
            "negative_cases_with_false_positive": len(negative_false),
            "visibility_cases": len(visibility),
        },
        "case_results": case_results,
        "formal_write_count": 0,
        "claim_boundary": (
            "This score describes only the checked-in curated benchmark. The "
            "current labels have not been independently confirmed by the user, "
            "so this is not a completed human-label audit or a population-wide "
            "guarantee for arbitrary future questions."
        ),
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--benchmark",
        type=Path,
        default=Path("benchmarks/personalization-commonality-v1.json"),
    )
    args = parser.parse_args(argv)
    try:
        report = evaluate(args.benchmark)
    except EvaluationError as exc:
        print(json.dumps({"status": "ERROR", "reason_code": str(exc)}))
        return 2
    print(json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True))
    return 0 if report["status"] == "PASS" else 1


if __name__ == "__main__":
    sys.exit(main())
