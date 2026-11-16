#!/usr/bin/env python3
"""Receipt-gated hot capture adapter for one managed morning first answer.

The adapter never scans the review, morning-session, or capture ledgers.  It
accepts one canonical review event plus the exact commit receipts already
returned by the managed answer transaction, re-verifies those receipts through
bounded indexes, builds an answer-safe capture payload, and commits exactly one
``fact_captured`` event through ``capture_hot_writer_408``.

Cold bootstrap is deliberately not performed here.  The capture hot state must
be prepared outside the active answer turn; missing or drifted state fails
closed before a capture is declared successful.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sys
from pathlib import Path
from typing import Any

import capture_hot_writer_408 as capture_hot
import intake_fact_capture_408 as capture_model
import morning_session_hot_state_408 as session_hot
import review_hot_state_408 as review_hot


CAPTURE_HOT_DIRNAME = ".capture-hot-writer-v1"
SESSION_HOT_DIRNAME = ".morning-session-hot-v1"
FAILURE_RESULTS = frozenset({"partial", "wrong", "uncertain"})
SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
CAPTURE_READINESS_SCHEMA = "capture_hot_readiness_v1"
BEFORE_LEARNER_ANSWER_PHASE = "before_learner_answer"


class MorningCaptureHotError(RuntimeError):
    """A managed morning capture failed before a verified joint commit."""


def capture_hot_root(repo_root: str | Path) -> Path:
    """Return the canonical derived hot directory for the capture ledger."""

    repo = Path(repo_root).expanduser().resolve()
    return capture_model.capture_root(repo) / CAPTURE_HOT_DIRNAME


def bootstrap_capture_hot_state(
    repo_root: str | Path, *, reconcile: bool = False
) -> dict[str, Any]:
    """Cold-build the capture hot projection outside an active answer turn."""

    repo = Path(repo_root).expanduser().resolve()
    if not repo.is_dir():
        raise MorningCaptureHotError("repository does not exist")
    root = capture_model.capture_root(repo)
    ledger = root / "events.jsonl"
    state = root / "state.json"
    derived = capture_hot_root(repo)
    if not ledger.is_file() or not state.is_file():
        raise MorningCaptureHotError(
            "capture truth files are missing; bootstrap never invents authoritative state"
        )
    try:
        with capture_hot.capture_ledger_lock(ledger) as lock_token:
            result = capture_hot.cold_bootstrap(
                ledger,
                state,
                derived,
                lock_token=lock_token,
                reconcile=reconcile,
            )
    except (capture_hot.CaptureHotWriterError, OSError) as exc:
        raise MorningCaptureHotError(f"capture cold bootstrap failed: {exc}") from exc
    if result.get("status") != "BOOTSTRAPPED" or result.get("formal_write_count") != 0:
        raise MorningCaptureHotError("capture cold bootstrap did not verify")
    return {
        **result,
        "repo": str(repo),
        "ledger_path": str(ledger),
        "state_path": str(state),
        "hot_root": str(derived),
        "answer_turn": False,
        "formal_write_count": 0,
    }


def capture_hot_readiness(repo_root: str | Path) -> dict[str, Any]:
    """Boundedly diagnose capture hot state without rebuilding it."""

    repo = Path(repo_root).expanduser().resolve()
    if not repo.is_dir():
        return {
            "schema": CAPTURE_READINESS_SCHEMA,
            "status": "unavailable",
            "phase": BEFORE_LEARNER_ANSWER_PHASE,
            "reason_code": "repository_missing",
            "rebuild_required": False,
            "read_only": True,
            "learner_evidence_write_count": 0,
            "formal_write_count": 0,
        }
    root = capture_model.capture_root(repo)
    ledger = root / "events.jsonl"
    state = root / "state.json"
    derived = capture_hot_root(repo)
    if not ledger.is_file() or not state.is_file():
        return {
            "schema": CAPTURE_READINESS_SCHEMA,
            "status": "unavailable",
            "phase": BEFORE_LEARNER_ANSWER_PHASE,
            "reason_code": "capture_truth_files_missing",
            "rebuild_required": False,
            "read_only": True,
            "learner_evidence_write_count": 0,
            "formal_write_count": 0,
        }
    if not (derived / capture_hot.JOINT_MANIFEST_FILENAME).is_file():
        return {
            "schema": CAPTURE_READINESS_SCHEMA,
            "status": "unavailable",
            "phase": BEFORE_LEARNER_ANSWER_PHASE,
            "reason_code": "capture_hot_joint_manifest_missing",
            "rebuild_required": True,
            "read_only": True,
            "learner_evidence_write_count": 0,
            "formal_write_count": 0,
        }
    pending_path = derived / capture_hot.PENDING_FILENAME
    if os.path.lexists(pending_path):
        return {
            "schema": CAPTURE_READINESS_SCHEMA,
            "status": "unavailable",
            "phase": BEFORE_LEARNER_ANSWER_PHASE,
            "reason_code": "capture_hot_pending_commit_requires_reconcile",
            "rebuild_required": False,
            "reconcile_required": True,
            "read_only": True,
            "learner_evidence_write_count": 0,
            "formal_write_count": 0,
        }
    try:
        verified = capture_hot.verify_joint_readiness(ledger, state, derived)
    except (capture_hot.CaptureHotWriterError, OSError, KeyError, ValueError) as exc:
        reason = str(exc)
        reconcile_required = (
            "pending" in reason
            or "incomplete joint capture commit" in reason
            or "cold capture reconcile" in reason
        )
        return {
            "schema": CAPTURE_READINESS_SCHEMA,
            "status": "unavailable",
            "phase": BEFORE_LEARNER_ANSWER_PHASE,
            "reason_code": reason,
            "rebuild_required": not reconcile_required,
            "reconcile_required": reconcile_required,
            "read_only": True,
            "learner_evidence_write_count": 0,
            "formal_write_count": 0,
        }
    return {
        "schema": CAPTURE_READINESS_SCHEMA,
        "status": "ready",
        "phase": BEFORE_LEARNER_ANSWER_PHASE,
        "rebuild_required": False,
        "joint_manifest_sha256": verified["joint_manifest_sha256"],
        "bounded_manifest_sha256": verified["bounded_manifest_sha256"],
        "event_count": verified["event_count"],
        "latest_event_id": verified["latest_event_id"],
        "latest_receipt_sha256": verified["latest_receipt_sha256"],
        "capture_ledger_full_read_count": 0,
        "state_read_count": verified["state_read_count"],
        "read_only": True,
        "learner_evidence_write_count": 0,
        "formal_write_count": 0,
    }


def prepare_capture_hot_state(repo_root: str | Path) -> dict[str, Any]:
    """Prepare capture hot state before a learner answer, never after it."""

    initial = capture_hot_readiness(repo_root)
    if initial.get("status") == "ready":
        return {
            **initial,
            "preparation_action": "already_ready",
            "rebuild_attempted": False,
            "rebuild_performed": False,
            "rebuild_required": False,
            "reconcile_required": initial.get("reconcile_required") is True,
            "capture_ledger_full_read_count": 0,
            "derived_projection_write_count": 0,
        }
    initial_reason = str(initial.get("reason_code") or "capture_hot_unavailable")
    if initial.get("rebuild_required") is not True:
        return {
            "schema": CAPTURE_READINESS_SCHEMA,
            "status": "blocked",
            "phase": BEFORE_LEARNER_ANSWER_PHASE,
            "preparation_action": (
                "reconcile_required"
                if initial.get("reconcile_required") is True
                else "not_repairable"
            ),
            "reason_code": initial_reason,
            "initial_reason_code": initial_reason,
            "rebuild_attempted": False,
            "rebuild_performed": False,
            "rebuild_required": False,
            "reconcile_required": initial.get("reconcile_required") is True,
            "capture_ledger_full_read_count": 0,
            "derived_projection_write_count": 0,
            "learner_evidence_write_count": 0,
            "formal_write_count": 0,
        }
    try:
        rebuilt = bootstrap_capture_hot_state(repo_root, reconcile=False)
    except MorningCaptureHotError as exc:
        return {
            "schema": CAPTURE_READINESS_SCHEMA,
            "status": "blocked",
            "phase": BEFORE_LEARNER_ANSWER_PHASE,
            "preparation_action": "cold_bootstrap_failed",
            "reason_code": str(exc),
            "initial_reason_code": initial_reason,
            "rebuild_attempted": True,
            "rebuild_performed": False,
            "rebuild_required": True,
            "reconcile_required": False,
            "capture_ledger_full_read_count": 0,
            "cold_bootstrap_attempt_count": 1,
            "derived_projection_write_count": 0,
            "learner_evidence_write_count": 0,
            "formal_write_count": 0,
        }
    verified = capture_hot_readiness(repo_root)
    if verified.get("status") != "ready":
        return {
            "schema": CAPTURE_READINESS_SCHEMA,
            "status": "blocked",
            "phase": BEFORE_LEARNER_ANSWER_PHASE,
            "preparation_action": "cold_bootstrap_verification_failed",
            "reason_code": str(
                verified.get("reason_code") or "capture_hot_unavailable"
            ),
            "initial_reason_code": initial_reason,
            "rebuild_attempted": True,
            "rebuild_performed": True,
            "rebuild_required": True,
            "reconcile_required": False,
            "capture_ledger_full_read_count": int(
                rebuilt.get("ledger_full_read_count", 1)
            ),
            "derived_projection_write_count": 1,
            "learner_evidence_write_count": 0,
            "formal_write_count": 0,
        }
    return {
        **verified,
        "preparation_action": "cold_bootstrapped",
        "initial_reason_code": initial_reason,
        "rebuild_attempted": True,
        "rebuild_performed": True,
        "capture_ledger_full_read_count": int(
            rebuilt.get("ledger_full_read_count", 1)
        ),
        "derived_projection_write_count": 1,
        "learner_evidence_write_count": 0,
        "formal_write_count": 0,
    }


def verify_capture_hot_commit(
    repo_root: str | Path,
    *,
    event_id: str | None = None,
    idempotency_key: str | None = None,
) -> dict[str, Any]:
    """Boundedly verify one exact capture joint commit outside answer-turn."""

    repo = Path(repo_root).expanduser().resolve()
    if not repo.is_dir():
        raise MorningCaptureHotError("repository does not exist")
    root = capture_model.capture_root(repo)
    ledger = root / "events.jsonl"
    state = root / "state.json"
    try:
        result = capture_hot.verify_joint_commit(
            ledger,
            state,
            capture_hot_root(repo),
            event_id=event_id,
            idempotency_key=idempotency_key,
        )
    except (capture_hot.CaptureHotWriterError, OSError) as exc:
        raise MorningCaptureHotError(f"capture joint verification failed: {exc}") from exc
    if result.get("status") != "PASS" or result.get("formal_write_count") != 0:
        raise MorningCaptureHotError("capture joint verification did not pass")
    return {
        **result,
        "repo": str(repo),
        "hot_root": str(capture_hot_root(repo)),
        "read_only": True,
        "formal_write_count": 0,
    }


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _sha(value: object, label: str) -> str:
    text = str(value or "")
    if not SHA256_RE.fullmatch(text):
        raise MorningCaptureHotError(f"{label} is not a lowercase SHA-256")
    return text


def _inside_repo(repo: Path, path: Path, label: str) -> str:
    try:
        return path.resolve().relative_to(repo).as_posix()
    except ValueError as exc:
        raise MorningCaptureHotError(f"{label} escaped the repository") from exc


def _session_proof(
    session_dir: Path,
    proof: dict[str, Any] | None,
    *,
    event_type: str,
    item_id: str,
    canonical_event_id: str | None = None,
) -> tuple[dict[str, Any], dict[str, Any], Path]:
    label = f"session {event_type} receipt"
    if not isinstance(proof, dict) or proof.get("status") not in {
        "committed",
        "already_committed",
        "pass",
    }:
        raise MorningCaptureHotError(
            f"{label} is missing; adopted state without an event receipt is insufficient"
        )
    receipt_sha = _sha(proof.get("receipt_sha256"), f"{label} sha256")
    try:
        verification = session_hot.verify_commit(session_dir, receipt_sha)
    except session_hot.MorningSessionHotStateError as exc:
        raise MorningCaptureHotError(f"{label} verification failed: {exc}") from exc
    if (
        verification.get("status") != "pass"
        or verification.get("receipt_sha256") != receipt_sha
        or verification.get("event_id") != proof.get("event_id")
        or verification.get("formal_write_count") != 0
    ):
        raise MorningCaptureHotError(f"{label} did not bind the supplied proof")
    try:
        found = session_hot.lookup_session_event(
            session_dir, str(verification["event_id"])
        )
    except session_hot.MorningSessionHotStateError as exc:
        raise MorningCaptureHotError(f"{label} exact event lookup failed: {exc}") from exc
    event = found.get("event") if isinstance(found, dict) else None
    payload = event.get("payload") if isinstance(event, dict) else None
    if (
        not isinstance(event, dict)
        or event.get("event_type") != event_type
        or not isinstance(payload, dict)
        or payload.get("item_id") != item_id
    ):
        raise MorningCaptureHotError(f"{label} points to the wrong session event")
    if (
        canonical_event_id is not None
        and payload.get("canonical_event_id") != canonical_event_id
    ):
        raise MorningCaptureHotError(f"{label} points to another canonical outcome")
    receipt_path = (
        session_dir
        / SESSION_HOT_DIRNAME
        / "receipts"
        / f"{receipt_sha}.json"
    )
    if (
        not receipt_path.is_file()
        or receipt_path.is_symlink()
        or _sha256_file(receipt_path) != receipt_sha
    ):
        raise MorningCaptureHotError(f"{label} artifact is missing or drifted")
    return event, verification, receipt_path


def _review_proof(
    repo: Path, event: dict[str, Any], receipt_sha256: object
) -> tuple[dict[str, Any], dict[str, Any], Path]:
    receipt_sha = _sha(receipt_sha256, "review commit receipt sha256")
    try:
        verification = review_hot.verify_commit(
            repo, receipt_sha256=receipt_sha
        )
        found = review_hot.lookup_event(repo, event_id=str(event.get("event_id") or ""))
    except review_hot.HotStateError as exc:
        raise MorningCaptureHotError(
            f"review commit receipt verification failed: {exc}"
        ) from exc
    if (
        verification.get("status") != "verified"
        or verification.get("receipt_sha256") != receipt_sha
        or verification.get("event_id") != event.get("event_id")
        or verification.get("formal_write_count") != 0
        or found.get("status") != "found"
        or found.get("event") != event
        or found.get("event_sha256") != verification.get("event_sha256")
    ):
        raise MorningCaptureHotError(
            "review commit receipt does not bind the supplied canonical event"
        )
    receipt_path = repo / review_hot.DEFAULT_STATE_RELATIVE / "receipts" / f"{receipt_sha}.json"
    if (
        not receipt_path.is_file()
        or receipt_path.is_symlink()
        or _sha256_file(receipt_path) != receipt_sha
    ):
        raise MorningCaptureHotError("review commit receipt artifact is missing or drifted")
    return found, verification, receipt_path


def _linked_exact_evidence(
    repo: Path,
    session_dir: Path,
    event: dict[str, Any],
) -> tuple[dict[str, Any] | None, dict[str, Any] | None]:
    if not event.get("practice_mode"):
        return None, None
    followup_event_id = str(event.get("followup_event_id") or "")
    parent_event_id = str(event.get("parent_trigger_event_id") or "")
    if not followup_event_id or not parent_event_id:
        raise MorningCaptureHotError("linked outcome lacks followup or parent identity")
    try:
        followup_found = session_hot.lookup_session_event(
            session_dir, followup_event_id
        )
        parent_found = review_hot.lookup_event(repo, event_id=parent_event_id)
    except (
        session_hot.MorningSessionHotStateError,
        review_hot.HotStateError,
    ) as exc:
        raise MorningCaptureHotError(
            f"linked outcome exact evidence lookup failed: {exc}"
        ) from exc
    followup = (
        followup_found.get("event") if isinstance(followup_found, dict) else None
    )
    parent = parent_found.get("event") if parent_found.get("status") == "found" else None
    if not isinstance(followup, dict) or not isinstance(parent, dict):
        raise MorningCaptureHotError("linked outcome evidence is not uniquely indexed")
    return followup, parent


def capture_managed_morning_outcome(
    repo_root: str | Path,
    event: dict[str, Any],
    *,
    review_commit_receipt_sha256: str,
    session_first_commit: dict[str, Any],
    session_binding_commit: dict[str, Any],
    hot_root: str | Path | None = None,
) -> dict[str, Any]:
    raise MorningCaptureHotError(
        "legacy_morning_capture_writer_retired_use_managed_current_question_turn"
    )
    """Hot-capture one eligible, receipt-bound managed morning outcome."""

    repo = Path(repo_root).expanduser().resolve()
    if not repo.is_dir() or not isinstance(event, dict):
        raise MorningCaptureHotError("managed morning capture input is invalid")
    if (
        event.get("schema") != capture_model.REVIEW_EVENT_SCHEMA
        or event.get("event_kind") != "outcome"
        or event.get("source") != "morning_review"
        or event.get("question_valid") is not True
        or event.get("formal_write_authorized") is not False
    ):
        raise MorningCaptureHotError("only an answer-safe managed morning outcome is accepted")
    first_result = str(event.get("first_result") or "")
    if first_result == "independent_correct":
        return {
            "status": "NOT_ELIGIBLE",
            "created": False,
            "capture_id": None,
            "quality_status": "not_eligible",
            "review_event_id": event.get("event_id"),
            "formal_write_authorized": False,
            "formal_write_count": 0,
            "review_or_session_ledger_full_scan_count": 0,
            "capture_ledger_full_scan_count": 0,
        }
    if first_result not in FAILURE_RESULTS:
        raise MorningCaptureHotError("morning outcome result is outside the capture policy")

    event_id = str(event.get("event_id") or "")
    session_id = str(event.get("session_id") or "")
    item_id = str(event.get("item_id") or "")
    session_dir = repo / capture_model.MORNING_SESSION_REL / session_id
    review_found, review_verification, review_receipt_path = _review_proof(
        repo, event, review_commit_receipt_sha256
    )
    first_event, first_verification, first_receipt_path = _session_proof(
        session_dir,
        session_first_commit,
        event_type="first_recorded",
        item_id=item_id,
    )
    binding_event, binding_verification, binding_receipt_path = _session_proof(
        session_dir,
        session_binding_commit,
        event_type="first_bound_to_loop",
        item_id=item_id,
        canonical_event_id=event_id,
    )
    try:
        started_found = session_hot.lookup_sequence(session_dir, 1)
    except session_hot.MorningSessionHotStateError as exc:
        raise MorningCaptureHotError(
            f"session start exact lookup failed: {exc}"
        ) from exc
    started = (
        started_found.get("event") if isinstance(started_found, dict) else None
    )
    if not isinstance(started, dict) or started.get("event_type") != "session_started":
        raise MorningCaptureHotError("session start is not bound by the hot projection")
    followup, parent = _linked_exact_evidence(repo, session_dir, event)

    receipt_refs = [
        {
            "kind": "review_hot_commit_receipt",
            "locator": _inside_repo(repo, review_receipt_path, "review receipt"),
            "sha256": _sha256_file(review_receipt_path),
        },
        {
            "kind": "morning_session_first_commit_receipt",
            "locator": _inside_repo(repo, first_receipt_path, "first receipt"),
            "sha256": _sha256_file(first_receipt_path),
        },
        {
            "kind": "morning_session_binding_commit_receipt",
            "locator": _inside_repo(repo, binding_receipt_path, "binding receipt"),
            "sha256": _sha256_file(binding_receipt_path),
        },
    ]
    try:
        payload = capture_model.build_verified_morning_capture_payload(
            repo,
            event,
            session_started_event=started,
            session_first_event=first_event,
            session_binding_event=binding_event,
            linked_followup_event=followup,
            parent_trigger_event=parent,
            receipt_refs=receipt_refs,
        )
    except capture_model.CaptureError as exc:
        raise MorningCaptureHotError(
            f"receipt-bound capture payload validation failed: {exc}"
        ) from exc

    ledger_root = capture_model.capture_root(repo)
    ledger = ledger_root / "events.jsonl"
    state = ledger_root / "state.json"
    derived = (
        Path(hot_root).expanduser().resolve()
        if hot_root is not None
        else capture_hot_root(repo)
    )
    if not ledger.is_file() or not state.is_file():
        raise MorningCaptureHotError(
            "capture truth files are not prepared; run cold bootstrap outside answer-turn"
        )
    expected_capture_event = capture_hot.canonical_fact_event_from_payload(payload)
    try:
        with capture_hot.capture_ledger_lock(ledger) as lock_token:
            committed = capture_hot.capture_fact(
                ledger,
                state,
                derived,
                lock_token=lock_token,
                validated_payload=payload,
            )
    except (capture_hot.CaptureHotWriterError, OSError, KeyError) as exc:
        raise MorningCaptureHotError(
            f"capture hot joint commit failed closed: {exc}"
        ) from exc
    verified = committed.get("joint_verification")
    if (
        committed.get("status") not in {"CAPTURED", "ALREADY_COMMITTED"}
        or not isinstance(verified, dict)
        or verified.get("status") != "PASS"
        or verified.get("event_id") != committed.get("event_id")
        or verified.get("event_id") != expected_capture_event.get("event_id")
        or verified.get("capture_id") != committed.get("capture_id")
        or verified.get("capture_id") != expected_capture_event.get("capture_id")
        or verified.get("receipt_sha256") != committed.get("receipt_sha256")
        or not SHA256_RE.fullmatch(str(committed.get("receipt_sha256") or ""))
        or int(committed.get("state_read_count", 99)) > 2
        or committed.get("formal_write_count") != 0
        or verified.get("formal_write_count") != 0
    ):
        raise MorningCaptureHotError("capture joint receipt did not verify")
    quality_status = str(
        committed.get("quality_status") or capture_model.PENDING_FORMAL_STATUS
    )
    if quality_status != capture_model.PENDING_FORMAL_STATUS:
        raise MorningCaptureHotError("morning failure escaped the daily-curation queue")
    return {
        **committed,
        "quality_status": quality_status,
        "review_event_id": event_id,
        "study_date": payload["study_date"],
        "policy_schema": capture_model.BACKFLOW_POLICY_SCHEMA,
        "formal_write_authorized": False,
        "formal_write_count": 0,
        "review_or_session_ledger_full_scan_count": 0,
        "capture_ledger_full_scan_count": 0,
        "review_commit_verification": review_verification,
        "review_lookup_scan_stats": review_found.get("scan_stats"),
        "session_first_verification": first_verification,
        "session_binding_verification": binding_verification,
        "capture_joint_verification": verified,
    }


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Cold maintenance and exact verification for managed morning capture hot state"
    )
    subparsers = parser.add_subparsers(dest="command", required=True)
    bootstrap = subparsers.add_parser(
        "bootstrap",
        help="cold-build or refresh the derived capture hot state outside answer-turn",
    )
    bootstrap.add_argument("--repo", required=True)
    bootstrap.add_argument(
        "--reconcile",
        action="store_true",
        help="explicitly rebuild after the authoritative capture state was reconciled",
    )
    readiness = subparsers.add_parser(
        "readiness",
        help="boundedly diagnose capture hot state without rebuilding",
    )
    readiness.add_argument("--repo", required=True)
    prepare = subparsers.add_parser(
        "prepare-study",
        help="cold-build missing or stale capture hot state before a learner answer",
    )
    prepare.add_argument("--repo", required=True)
    verify = subparsers.add_parser(
        "verify", help="boundedly verify one exact capture joint commit"
    )
    verify.add_argument("--repo", required=True)
    identity = verify.add_mutually_exclusive_group(required=True)
    identity.add_argument("--event-id")
    identity.add_argument("--idempotency-key")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        if args.command == "bootstrap":
            result = bootstrap_capture_hot_state(
                args.repo, reconcile=bool(args.reconcile)
            )
        elif args.command == "readiness":
            result = capture_hot_readiness(args.repo)
        elif args.command == "prepare-study":
            result = prepare_capture_hot_state(args.repo)
        else:
            result = verify_capture_hot_commit(
                args.repo,
                event_id=args.event_id,
                idempotency_key=args.idempotency_key,
            )
    except MorningCaptureHotError as exc:
        print(
            json.dumps(
                {
                    "status": "ERROR",
                    "error": str(exc),
                    "formal_write_count": 0,
                },
                ensure_ascii=False,
                sort_keys=True,
            ),
            file=sys.stderr,
        )
        return 2
    print(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True))
    return 0 if result.get("status") not in {"blocked", "unavailable"} else 2


__all__ = [
    "CAPTURE_HOT_DIRNAME",
    "MorningCaptureHotError",
    "bootstrap_capture_hot_state",
    "capture_hot_readiness",
    "capture_hot_root",
    "capture_managed_morning_outcome",
    "prepare_capture_hot_state",
    "verify_capture_hot_commit",
]


if __name__ == "__main__":
    raise SystemExit(main())
