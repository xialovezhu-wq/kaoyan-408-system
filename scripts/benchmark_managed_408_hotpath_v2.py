#!/usr/bin/env python3
"""Disposable end-to-end acceptance for the unified managed 408 current turn.

The runner creates a complete prepared-pack/session/hot-state fixture inside a
fresh ``TemporaryDirectory`` for every sample.  Production learning state is
never opened as a turn target.  Successful paths are timed through the real
CLI; deterministic fault injection is deliberately in-process because the
production CLI exposes no fault-injection surface.
"""

from __future__ import annotations

import argparse
import contextlib
import datetime as dt
import hashlib
import json
import math
import statistics
import subprocess
import sys
import time
from pathlib import Path
from typing import Any, Iterator
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"
TESTS = ROOT / "tests"
for entry in (str(SCRIPTS), str(TESTS)):
    if entry not in sys.path:
        sys.path.insert(0, entry)

import intake_fact_capture_408 as capture_model  # noqa: E402
import managed_408_current_turn as current_turn  # noqa: E402
import morning_review_prepared_pack_408 as prepared_pack  # noqa: E402
import morning_review_session as morning_session  # noqa: E402
import review_feedback_loop as review_loop  # noqa: E402
import review_hot_state_408 as review_hot  # noqa: E402
import review_outcome_hot_408 as review_outcome  # noqa: E402
from test_morning_review_prepared_pack_managed_hot_408 import (  # noqa: E402
    PreparedPackManagedHotPath408Tests as PreparedFixture,
)


SCHEMA = "managed-408-hotpath-acceptance-v2"
DEFAULT_ARTIFACT_DIR = ROOT / "benchmarks/managed-408-hotpath-v2"
RUNTIME_BINDINGS = (
    "scripts/managed_408_current_turn.py",
    "scripts/morning_review_prepared_pack_408.py",
    "scripts/morning_review_session.py",
    "scripts/current_question_evidence_408.py",
    "scripts/review_outcome_hot_408.py",
    "scripts/capture_hot_writer_408.py",
    "scripts/benchmark_managed_408_hotpath_v2.py",
    "tests/test_morning_review_prepared_pack_managed_hot_408.py",
)
PERFORMANCE_LIMITS_MS = {
    "correct_cli": {"p95_ms": 250.0, "max_ms": 500.0},
    "wrong_first_cli": {"p95_ms": 250.0, "max_ms": 500.0},
    "wrong_resolve_cli": {"p95_ms": 250.0, "max_ms": 500.0},
    "idempotent_replay_cli": {"p95_ms": 150.0, "max_ms": 250.0},
    "wrong_episode_cli": {"p95_ms": 400.0, "max_ms": 800.0},
    "capture_failure_in_process": {"p95_ms": 150.0, "max_ms": 300.0},
    "capture_recovery_in_process": {"p95_ms": 150.0, "max_ms": 300.0},
    "capture_failure_episode_in_process": {
        "p95_ms": 250.0,
        "max_ms": 500.0,
    },
    "advance_failure_in_process": {"p95_ms": 150.0, "max_ms": 300.0},
    "advance_retry_in_process": {"p95_ms": 150.0, "max_ms": 300.0},
    "advance_failure_episode_in_process": {
        "p95_ms": 250.0,
        "max_ms": 500.0,
    },
}
EXPECTED_LEDGER_DELTAS = {
    "correct_cli": {"review": 1, "capture": 1, "session": 3},
    "idempotent_replay_cli": {"review": 0, "capture": 0, "session": 0},
    "wrong_first_cli": {"review": 1, "capture": 1, "session": 3},
    "wrong_resolve_cli": {"review": 0, "capture": 0, "session": 1},
    "capture_failure_in_process": {"review": 1, "capture": 0, "session": 3},
    "capture_recovery_in_process": {"review": 0, "capture": 1, "session": 0},
    "advance_failure_in_process": {"review": 1, "capture": 1, "session": 3},
    "advance_retry_in_process": {"review": 0, "capture": 0, "session": 0},
}


class AcceptanceError(RuntimeError):
    """Raised when the disposable acceptance fixture violates its contract."""


def _canonical(value: Any) -> bytes:
    return (
        json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        + "\n"
    ).encode("utf-8")


def _sha256_bytes(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


def _sha256_file(path: Path) -> str:
    return _sha256_bytes(path.read_bytes())


def _runtime_bindings() -> dict[str, str]:
    return {relative: _sha256_file(ROOT / relative) for relative in RUNTIME_BINDINGS}


def _nonempty_lines(path: Path) -> int:
    if not path.is_file():
        return 0
    return sum(1 for line in path.read_text(encoding="utf-8").splitlines() if line)


def _file_map(root: Path) -> dict[str, str]:
    if not root.exists():
        return {}
    return {
        path.relative_to(root).as_posix(): _sha256_file(path)
        for path in sorted(root.rglob("*"))
        if path.is_file()
    }


def _tree_sha(files: dict[str, str]) -> str:
    return _sha256_bytes(_canonical(files))


def _snapshot(fixture: PreparedFixture, private_root: Path) -> dict[str, Any]:
    private_files = _file_map(private_root)
    ledgers = {
        "review": {
            "events": _nonempty_lines(fixture.review_ledger),
            "sha256": _sha256_file(fixture.review_ledger),
        },
        "capture": {
            "events": _nonempty_lines(fixture.capture_ledger),
            "sha256": _sha256_file(fixture.capture_ledger),
        },
        "session": {
            "events": _nonempty_lines(fixture.session_ledger),
            "sha256": _sha256_file(fixture.session_ledger),
        },
    }
    return {
        "ledgers": ledgers,
        "private_files": private_files,
        "private_tree_sha256": _tree_sha(private_files),
    }


def _snapshot_delta(before: dict[str, Any], after: dict[str, Any]) -> dict[str, Any]:
    before_files = before["private_files"]
    after_files = after["private_files"]
    shared = set(before_files) & set(after_files)
    return {
        "ledger_events": {
            name: after["ledgers"][name]["events"]
            - before["ledgers"][name]["events"]
            for name in ("review", "capture", "session")
        },
        "private_new_file_count": len(set(after_files) - set(before_files)),
        "private_changed_file_count": sum(
            before_files[name] != after_files[name] for name in shared
        ),
        "private_deleted_file_count": len(set(before_files) - set(after_files)),
        "before_private_tree_sha256": before["private_tree_sha256"],
        "after_private_tree_sha256": after["private_tree_sha256"],
    }


def _result_counts(value: Any) -> dict[str, int]:
    counts = {"luna_call_count": 0, "formal_write_count": 0}

    def visit(node: Any) -> None:
        if isinstance(node, dict):
            for key, child in node.items():
                if key in counts and isinstance(child, int):
                    counts[key] += child
                visit(child)
        elif isinstance(node, list):
            for child in node:
                visit(child)

    visit(value)
    return counts


@contextlib.contextmanager
def _fixture() -> Iterator[tuple[PreparedFixture, Path, dict[str, str]]]:
    fixture = PreparedFixture(methodName="test_answer_current_and_next_real_cli_e2e")
    fixture.setUp()
    temporary_root = Path(fixture.tmp.name).resolve()
    private_root = temporary_root / "private-v2-acceptance"
    display = fixture._publish_current_display(private_root)
    try:
        if not fixture.repo.is_relative_to(temporary_root):
            raise AcceptanceError("fixture repository escaped TemporaryDirectory")
        if not private_root.is_relative_to(temporary_root):
            raise AcceptanceError("private root escaped TemporaryDirectory")
        yield fixture, private_root, display
    finally:
        fixture.tearDown()


def _run_cli(
    fixture: PreparedFixture,
    private_root: Path,
    display_locator: str,
    *,
    choice: str,
    confidence: str,
    prompt_level: str,
    trace: list[dict[str, str]],
) -> tuple[dict[str, Any], float]:
    command = [
        sys.executable,
        str(ROOT / "scripts/managed_408_current_turn.py"),
        "--repo",
        str(fixture.repo),
        "--private-root",
        str(private_root),
        "answer-current-and-next",
        "--display-receipt-locator",
        display_locator,
        "--choice",
        choice,
        "--confidence",
        confidence,
        "--prompt-level",
        prompt_level,
        "--trace-json",
        json.dumps(trace, ensure_ascii=False),
    ]
    started = time.perf_counter()
    completed = subprocess.run(
        command,
        cwd=fixture.repo,
        text=True,
        capture_output=True,
        timeout=5,
        check=False,
    )
    elapsed_ms = (time.perf_counter() - started) * 1000.0
    try:
        result = json.loads(completed.stdout)
    except json.JSONDecodeError as exc:
        raise AcceptanceError(
            f"managed CLI returned invalid JSON: {completed.stderr}"
        ) from exc
    if completed.returncode != 0:
        raise AcceptanceError(
            f"managed CLI failed with {completed.returncode}: {result}"
        )
    return result, elapsed_ms


class ForbiddenSemanticGuard:
    """Count semantic cold/global operations forbidden inside the learner turn."""

    TARGETS = (
        (review_loop, "_read_events"),
        (review_loop, "audit"),
        (review_loop, "reconcile_runtime_views"),
        (review_loop, "mapped_nodes"),
        (review_loop, "_original_actions"),
        (capture_model, "_load_events"),
        (capture_model, "audit"),
        (capture_model, "_execute_global_audit"),
        (review_hot, "cold_rebuild"),
        (prepared_pack, "audit_pack"),
        (review_outcome, "_formal_context"),
    )

    def __init__(self) -> None:
        self.counts = {
            f"{module.__name__}.{name}": 0
            for module, name in self.TARGETS
            if hasattr(module, name)
        }
        self._stack = contextlib.ExitStack()

    def __enter__(self) -> "ForbiddenSemanticGuard":
        for module, name in self.TARGETS:
            if not hasattr(module, name):
                continue
            label = f"{module.__name__}.{name}"

            def forbidden(*_args: Any, _label: str = label, **_kwargs: Any) -> Any:
                self.counts[_label] += 1
                raise AcceptanceError(f"forbidden learner-turn operation: {_label}")

            self._stack.enter_context(mock.patch.object(module, name, side_effect=forbidden))
        return self

    def __exit__(self, *exc: object) -> None:
        self._stack.close()

    @property
    def total(self) -> int:
        return sum(self.counts.values())


def _assert_zero_counts(result: Any, label: str) -> None:
    counts = _result_counts(result)
    if counts != {"luna_call_count": 0, "formal_write_count": 0}:
        raise AcceptanceError(f"{label} crossed Luna/formal boundary: {counts}")


def _record_delta(
    rows: dict[str, list[dict[str, Any]]],
    scenario: str,
    before: dict[str, Any],
    after: dict[str, Any],
    *,
    measured: bool,
) -> dict[str, Any]:
    delta = _snapshot_delta(before, after)
    expected = EXPECTED_LEDGER_DELTAS[scenario]
    if delta["ledger_events"] != expected:
        raise AcceptanceError(
            f"{scenario} ledger delta drifted: {delta['ledger_events']} != {expected}"
        )
    if measured:
        rows[scenario].append(delta)
    return delta


def _run_one_iteration(
    timings: dict[str, list[float]],
    deltas: dict[str, list[dict[str, Any]]],
    semantic_counts: dict[str, int],
    *,
    measured: bool,
) -> None:
    with _fixture() as (fixture, private_root, display):
        before = _snapshot(fixture, private_root)
        correct, elapsed = _run_cli(
            fixture,
            private_root,
            display["locator"],
            choice="C",
            confidence="high",
            prompt_level="none",
            trace=[{"role": "learner", "kind": "answer", "text": "C"}],
        )
        after_correct = _snapshot(fixture, private_root)
        if correct.get("status") != "feedback_and_next_ready":
            raise AcceptanceError(f"correct status drifted: {correct}")
        if not correct.get("next_item_published") or not (
            (correct.get("next_item") or {}).get("display_text")
        ):
            raise AcceptanceError("correct turn did not return the successor surface")
        _assert_zero_counts(correct, "correct")
        _record_delta(
            deltas, "correct_cli", before, after_correct, measured=measured
        )

        replay, replay_ms = _run_cli(
            fixture,
            private_root,
            display["locator"],
            choice="C",
            confidence="high",
            prompt_level="none",
            trace=[{"role": "learner", "kind": "answer", "text": "C"}],
        )
        after_replay = _snapshot(fixture, private_root)
        replay_delta = _record_delta(
            deltas,
            "idempotent_replay_cli",
            after_correct,
            after_replay,
            measured=measured,
        )
        if not replay.get("idempotent_replay"):
            raise AcceptanceError("exact replay was not marked idempotent")
        if replay.get("learner_evidence_write_count") != 0:
            raise AcceptanceError("exact replay wrote learner evidence")
        if replay_delta["before_private_tree_sha256"] != replay_delta[
            "after_private_tree_sha256"
        ]:
            raise AcceptanceError("exact replay changed the private object tree")
        if (correct.get("next_item") or {}).get("display_receipt_locator") != (
            replay.get("next_item") or {}
        ).get("display_receipt_locator"):
            raise AcceptanceError("exact replay changed the successor receipt")
        _assert_zero_counts(replay, "idempotent replay")
        if measured:
            timings["correct_cli"].append(elapsed)
            timings["idempotent_replay_cli"].append(replay_ms)

    with _fixture() as (fixture, private_root, display):
        before = _snapshot(fixture, private_root)
        wrong, wrong_ms = _run_cli(
            fixture,
            private_root,
            display["locator"],
            choice="A",
            confidence="high",
            prompt_level="none",
            trace=[
                {
                    "role": "learner",
                    "kind": "reasoning",
                    "text": "首轮只检查条件甲",
                }
            ],
        )
        after_wrong = _snapshot(fixture, private_root)
        if wrong.get("status") != "feedback_ready_continue_current":
            raise AcceptanceError(f"wrong-first status drifted: {wrong}")
        if wrong.get("next_item_published"):
            raise AcceptanceError("wrong first answer advanced before correction")
        _assert_zero_counts(wrong, "wrong first")
        _record_delta(
            deltas, "wrong_first_cli", before, after_wrong, measured=measured
        )

        resolved, resolve_ms = _run_cli(
            fixture,
            private_root,
            display["locator"],
            choice="C",
            confidence="high",
            prompt_level="L2",
            trace=[
                {"role": "assistant", "kind": "correction", "text": "分开检查"},
                {"role": "learner", "kind": "answer", "text": "C"},
            ],
        )
        after_resolved = _snapshot(fixture, private_root)
        if resolved.get("status") != "feedback_and_next_ready":
            raise AcceptanceError(f"wrong-resolve status drifted: {resolved}")
        if not resolved.get("next_item_published") or not (
            (resolved.get("next_item") or {}).get("display_text")
        ):
            raise AcceptanceError("resolved wrong turn did not return successor surface")
        if resolved.get("capture_id") != wrong.get("capture_id"):
            raise AcceptanceError("correction changed the original capture identity")
        _assert_zero_counts(resolved, "wrong resolution")
        _record_delta(
            deltas,
            "wrong_resolve_cli",
            after_wrong,
            after_resolved,
            measured=measured,
        )
        if measured:
            timings["wrong_first_cli"].append(wrong_ms)
            timings["wrong_resolve_cli"].append(resolve_ms)
            timings["wrong_episode_cli"].append(wrong_ms + resolve_ms)

    with _fixture() as (fixture, private_root, display):
        before = _snapshot(fixture, private_root)
        with ForbiddenSemanticGuard() as guard, mock.patch.object(
            current_turn,
            "_commit_capture",
            side_effect=current_turn.ManagedCurrentTurnError(
                "injected-v2-capture-failure"
            ),
        ):
            started = time.perf_counter()
            failed = current_turn.answer_current_and_next(
                fixture.repo,
                display_receipt_locator=display["locator"],
                choice="C",
                confidence="medium",
                prompt_level="none",
                interaction_trace=[
                    {"role": "learner", "kind": "answer", "text": "C"}
                ],
                private_root=private_root,
            )
            failure_ms = (time.perf_counter() - started) * 1000.0
        for name, count in guard.counts.items():
            semantic_counts[name] += count
        after_failure = _snapshot(fixture, private_root)
        if failed.get("capture_status") != "capture_pending_recovery":
            raise AcceptanceError(f"capture failure did not expose recovery: {failed}")
        if failed.get("next_item_published"):
            raise AcceptanceError("capture failure advanced the session")
        _assert_zero_counts(failed, "capture failure")
        _record_delta(
            deltas,
            "capture_failure_in_process",
            before,
            after_failure,
            measured=measured,
        )

        with ForbiddenSemanticGuard() as guard:
            started = time.perf_counter()
            recovered = current_turn.recover_current_capture(
                fixture.repo,
                failed["recovery_locator"],
                private_root=private_root,
            )
            recovery_ms = (time.perf_counter() - started) * 1000.0
        for name, count in guard.counts.items():
            semantic_counts[name] += count
        after_recovery = _snapshot(fixture, private_root)
        if not recovered.get("position_changed") or not (
            (recovered.get("next_item") or {}).get("display_text")
        ):
            raise AcceptanceError("capture recovery did not close and advance")
        _assert_zero_counts(recovered, "capture recovery")
        _record_delta(
            deltas,
            "capture_recovery_in_process",
            after_failure,
            after_recovery,
            measured=measured,
        )
        replay_before = _snapshot(fixture, private_root)
        exact_replay = current_turn.answer_current_and_next(
            fixture.repo,
            display_receipt_locator=display["locator"],
            choice="C",
            confidence="medium",
            prompt_level="none",
            interaction_trace=[{"role": "learner", "kind": "answer", "text": "C"}],
            private_root=private_root,
        )
        replay_after = _snapshot(fixture, private_root)
        if not exact_replay.get("idempotent_replay") or replay_before != replay_after:
            raise AcceptanceError("capture recovery did not seal exact replay")
        if measured:
            timings["capture_failure_in_process"].append(failure_ms)
            timings["capture_recovery_in_process"].append(recovery_ms)
            timings["capture_failure_episode_in_process"].append(
                failure_ms + recovery_ms
            )

    with _fixture() as (fixture, private_root, display):
        before = _snapshot(fixture, private_root)
        original_next = current_turn.next_item
        published: dict[str, Any] = {}

        def publish_then_fail(*args: Any, **kwargs: Any) -> Any:
            result = original_next(*args, **kwargs)
            published.update(result)
            raise current_turn.ManagedCurrentTurnError(
                "injected-v2-after-next-publication"
            )

        with ForbiddenSemanticGuard() as guard, mock.patch.object(
            current_turn, "next_item", side_effect=publish_then_fail
        ):
            started = time.perf_counter()
            try:
                current_turn.answer_current_and_next(
                    fixture.repo,
                    display_receipt_locator=display["locator"],
                    choice="C",
                    confidence="high",
                    prompt_level="none",
                    interaction_trace=[
                        {"role": "learner", "kind": "answer", "text": "C"}
                    ],
                    private_root=private_root,
                )
            except current_turn.ManagedCurrentTurnError as exc:
                if "injected-v2-after-next-publication" not in str(exc):
                    raise
            else:
                raise AcceptanceError("advance failure injection did not fire")
            advance_failure_ms = (time.perf_counter() - started) * 1000.0
        for name, count in guard.counts.items():
            semantic_counts[name] += count
        after_advance_failure = _snapshot(fixture, private_root)
        _record_delta(
            deltas,
            "advance_failure_in_process",
            before,
            after_advance_failure,
            measured=measured,
        )

        with ForbiddenSemanticGuard() as guard:
            started = time.perf_counter()
            retried = current_turn.answer_current_and_next(
                fixture.repo,
                display_receipt_locator=display["locator"],
                choice="C",
                confidence="high",
                prompt_level="none",
                interaction_trace=[
                    {"role": "learner", "kind": "answer", "text": "C"}
                ],
                private_root=private_root,
            )
            retry_ms = (time.perf_counter() - started) * 1000.0
        for name, count in guard.counts.items():
            semantic_counts[name] += count
        after_retry = _snapshot(fixture, private_root)
        _record_delta(
            deltas,
            "advance_retry_in_process",
            after_advance_failure,
            after_retry,
            measured=measured,
        )
        if not retried.get("next_item_published"):
            raise AcceptanceError(f"advance retry did not return successor: {retried}")
        if published.get("display_receipt_locator") != (
            retried.get("next_item") or {}
        ).get("display_receipt_locator"):
            raise AcceptanceError("advance retry duplicated successor receipt")
        _assert_zero_counts(retried, "advance retry")
        replay_before = _snapshot(fixture, private_root)
        exact_replay = current_turn.answer_current_and_next(
            fixture.repo,
            display_receipt_locator=display["locator"],
            choice="C",
            confidence="high",
            prompt_level="none",
            interaction_trace=[{"role": "learner", "kind": "answer", "text": "C"}],
            private_root=private_root,
        )
        replay_after = _snapshot(fixture, private_root)
        if not exact_replay.get("idempotent_replay") or replay_before != replay_after:
            raise AcceptanceError("advance retry did not seal exact replay")
        if measured:
            timings["advance_failure_in_process"].append(advance_failure_ms)
            timings["advance_retry_in_process"].append(retry_ms)
            timings["advance_failure_episode_in_process"].append(
                advance_failure_ms + retry_ms
            )


def _percentile(samples: list[float], percentile: int) -> float:
    ordered = sorted(samples)
    index = max(0, math.ceil((percentile / 100.0) * len(ordered)) - 1)
    return ordered[index]


def _timing_summary(samples: list[float]) -> dict[str, Any]:
    if not samples:
        raise AcceptanceError("timing scenario has no measured samples")
    return {
        "sample_count": len(samples),
        "p50_ms": round(statistics.median(samples), 3),
        "p95_ms": round(_percentile(samples, 95), 3),
        "max_ms": round(max(samples), 3),
    }


def _delta_summary(rows: list[dict[str, Any]]) -> dict[str, Any]:
    ledger_set = {
        tuple(sorted(row["ledger_events"].items())) for row in rows
    }
    return {
        "sample_count": len(rows),
        "ledger_delta_values": [dict(values) for values in sorted(ledger_set)],
        "private_new_file_count": {
            "min": min(row["private_new_file_count"] for row in rows),
            "max": max(row["private_new_file_count"] for row in rows),
        },
        "private_changed_file_count": {
            "min": min(row["private_changed_file_count"] for row in rows),
            "max": max(row["private_changed_file_count"] for row in rows),
        },
        "private_deleted_file_count": {
            "min": min(row["private_deleted_file_count"] for row in rows),
            "max": max(row["private_deleted_file_count"] for row in rows),
        },
        "private_tree_unchanged_count": sum(
            row["before_private_tree_sha256"]
            == row["after_private_tree_sha256"]
            for row in rows
        ),
    }


def run_acceptance(
    *,
    warmups: int = 5,
    iterations: int = 50,
    enforce_thresholds: bool = True,
) -> dict[str, Any]:
    if warmups < 0 or warmups > 20:
        raise ValueError("warmups must be between 0 and 20")
    if iterations < 1 or iterations > 200:
        raise ValueError("iterations must be between 1 and 200")
    build_before = _runtime_bindings()
    timings = {name: [] for name in PERFORMANCE_LIMITS_MS}
    deltas = {name: [] for name in EXPECTED_LEDGER_DELTAS}
    semantic_counts = {
        f"{module.__name__}.{name}": 0
        for module, name in ForbiddenSemanticGuard.TARGETS
        if hasattr(module, name)
    }
    failures: list[str] = []
    for ordinal in range(warmups + iterations):
        try:
            _run_one_iteration(
                timings,
                deltas,
                semantic_counts,
                measured=ordinal >= warmups,
            )
        except (AssertionError, AcceptanceError, OSError, subprocess.SubprocessError) as exc:
            failures.append(f"iteration-{ordinal + 1}:{exc}")
            break

    timing_report = {
        name: _timing_summary(samples)
        for name, samples in timings.items()
        if samples
    }
    performance_gates: dict[str, Any] = {}
    if not failures:
        for name, limits in PERFORMANCE_LIMITS_MS.items():
            observed = timing_report[name]
            passed = (
                observed["p95_ms"] <= limits["p95_ms"]
                and observed["max_ms"] <= limits["max_ms"]
            )
            performance_gates[name] = {
                "status": "pass" if passed else "fail",
                "limits_ms": limits,
                "observed": observed,
            }
            if enforce_thresholds and not passed:
                failures.append(f"{name}:performance_threshold")

    build_after = _runtime_bindings()
    build_unchanged = build_before == build_after
    if not build_unchanged:
        failures.append("runtime_source_changed_during_acceptance")
    semantic_total = sum(semantic_counts.values())
    if semantic_total:
        failures.append("forbidden_semantic_operation_observed")
    delta_report = {
        name: _delta_summary(rows) for name, rows in deltas.items() if rows
    }
    if not failures:
        for name, expected in EXPECTED_LEDGER_DELTAS.items():
            if delta_report[name]["ledger_delta_values"] != [expected]:
                failures.append(f"{name}:ledger_delta_gate")
    if not failures:
        replay_delta = delta_report["idempotent_replay_cli"]
        if replay_delta["private_tree_unchanged_count"] != iterations:
            failures.append("idempotent_replay_private_tree_changed")

    exact_replay_count = delta_report.get("idempotent_replay_cli", {}).get(
        "private_tree_unchanged_count", 0
    )
    functional_iteration_failed = any(
        failure.startswith("iteration-") for failure in failures
    )
    idempotency_passed = (
        not functional_iteration_failed
        and exact_replay_count == iterations
        and delta_report.get("advance_retry_in_process", {}).get(
            "sample_count", 0
        )
        == iterations
        and delta_report.get("capture_recovery_in_process", {}).get(
            "sample_count", 0
        )
        == iterations
    )

    evidence = {
        "schema": SCHEMA,
        "generated_at": dt.datetime.now(dt.timezone.utc).isoformat(),
        "status": "pass" if not failures else "fail",
        "claim_boundary": {
            "all_turn_writes_scoped_to_temporary_directories": True,
            "successful_paths_use_real_cli_subprocess": True,
            "fault_injection_runner": "in_process",
            "model_call_claimed": False,
            "ui_or_human_latency_claimed": False,
            "production_learning_state_latency_claimed": False,
        },
        "configuration": {
            "warmups": warmups,
            "iterations_per_scenario": iterations,
            "thresholds_enforced": enforce_thresholds,
        },
        "build_bindings": build_after,
        "build_bindings_unchanged_during_run": build_unchanged,
        "timings": timing_report,
        "performance_gates": performance_gates,
        "ledger_and_private_write_deltas": delta_report,
        "forbidden_semantic_operations": {
            "total_count": semantic_total,
            "counts": semantic_counts,
        },
        "boundary_counts": {
            "luna_call_count": 0,
            "formal_write_count": 0,
        },
        "idempotency_gate": {
            "status": "pass" if idempotency_passed else "fail",
            "exact_cli_replay_count": exact_replay_count,
            "advance_retry_reuses_published_successor": (
                not functional_iteration_failed
                and delta_report.get("advance_retry_in_process", {}).get(
                    "sample_count", 0
                )
                == iterations
            ),
            "capture_recovery_seals_original_operation": (
                not functional_iteration_failed
                and delta_report.get("capture_recovery_in_process", {}).get(
                    "sample_count", 0
                )
                == iterations
            ),
        },
        "failures": failures,
    }
    evidence_sha = _sha256_bytes(_canonical(evidence))
    return {
        **evidence,
        "artifact_id": f"MHV2-{evidence_sha[:24].upper()}",
        "evidence_sha256": evidence_sha,
    }


def write_content_addressed_artifact(
    artifact: dict[str, Any], artifact_dir: Path
) -> Path:
    artifact_dir.mkdir(parents=True, exist_ok=True)
    raw = _canonical(artifact)
    digest = _sha256_bytes(raw)
    path = artifact_dir / f"{digest}.json"
    if path.exists() and path.read_bytes() != raw:
        raise AcceptanceError("content-addressed artifact collision")
    path.write_bytes(raw)
    return path


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--warmups", type=int, default=5)
    parser.add_argument("--iterations", type=int, default=50)
    parser.add_argument("--no-enforce-thresholds", action="store_true")
    parser.add_argument("--write-artifact", action="store_true")
    parser.add_argument("--artifact-dir", type=Path, default=DEFAULT_ARTIFACT_DIR)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    artifact = run_acceptance(
        warmups=args.warmups,
        iterations=args.iterations,
        enforce_thresholds=not args.no_enforce_thresholds,
    )
    output = dict(artifact)
    if args.write_artifact:
        path = write_content_addressed_artifact(artifact, args.artifact_dir)
        output["artifact_path"] = str(path)
        output["artifact_file_sha256"] = path.stem
    print(json.dumps(output, ensure_ascii=False, indent=2, sort_keys=True))
    return 0 if artifact["status"] == "pass" else 2


if __name__ == "__main__":
    raise SystemExit(main())
