#!/usr/bin/env python3
"""Own the 408 morning control plane from start request to first display."""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from pathlib import Path
from typing import Any

import current_question_evidence_408 as current_evidence
import morning_review_session as morning_session
import morning_review_prepared_pack_408 as prepared_pack
import prepare_next_morning_408 as preparation


CONTROL_SCHEMA = "morning-review-control-start-v1"
MODEL = "gpt-5.6-sol"
REASONING = "max"


class MorningControlError(RuntimeError):
    pass


def _sha256(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def publish_first_display(
    repo: str | Path,
    *,
    session_id: str,
    private_root: str | Path | None = None,
) -> dict[str, Any]:
    """Control-plane publication of the first unanswered prepared surface."""

    root = Path(repo).expanduser().resolve()
    session_dir = morning_session._session_dir(root, session_id)
    with morning_session.session_lock(session_dir):
        state = morning_session._load_state(session_dir, session_id)
        morning_session._ensure_active(state)
        item_id = next(
            (
                candidate
                for candidate in state["item_order"]
                if state["items"][candidate].get("first") is None
                and state["items"][candidate].get("resolution") != "dismissed"
            ),
            None,
        )
        if item_id is None:
            return {
                "schema": "managed-408-next-item-v1",
                "status": "complete",
                "session_id": session_id,
                "item_id": None,
                "formal_write_count": 0,
            }

    shown = prepared_pack.show_item(root, session_id, item_id)
    surface = dict(shown["surface"])
    question_asset = prepared_pack.resolve_question_asset(surface)
    lines = [str(surface["stem"]).strip(), ""]
    lines.extend(
        f"{key}. {str(surface['options'][key]).strip()}"
        for key in ("A", "B", "C", "D")
    )
    lines.extend(["", str(surface["response_instruction"]).strip()])
    display_text = "\n".join(lines).strip() + "\n"
    display = {
        "schema": current_evidence.TURN_RECEIPT_SCHEMA,
        "status": "display_published",
        "context_id": None,
        "request_id": None,
        "session_id": session_id,
        "item_id": item_id,
        "feedback_sha256": None,
        "first_result": None,
        "first_answer_receipt_sha256": None,
        "capture_id": None,
        "capture_receipt_sha256": None,
        "capture_status": "not_started",
        "recovery_locator": None,
        "advance_allowed": False,
        "surface_sha256": shown["surface_sha256"],
        "display_sha256": _sha256(display_text.encode("utf-8")),
        "next_display_receipt_sha256": None,
        "question_asset_ref": (
            question_asset["relative_path"] if question_asset is not None else None
        ),
        "question_asset_sha256": (
            question_asset["sha256"] if question_asset is not None else None
        ),
        "formal_write_count": 0,
    }
    receipt = current_evidence.publish_metadata_object(
        display, kind="turns", private_root=private_root
    )
    return {
        "schema": "managed-408-next-item-v1",
        "status": "published",
        "session_id": session_id,
        "item_id": item_id,
        "surface_sha256": shown["surface_sha256"],
        "display_text": display_text,
        "display_receipt_locator": receipt["locator"],
        "display_receipt_sha256": receipt["sha256"],
        "question_asset_path": (
            question_asset["absolute_path"] if question_asset is not None else None
        ),
        "question_asset_sha256": (
            question_asset["sha256"] if question_asset is not None else None
        ),
        "formal_write_count": 0,
    }


def _session_state(repo: Path, session_id: str) -> dict[str, Any] | None:
    session_dir = morning_session._session_dir(repo, session_id)
    if not session_dir.is_dir():
        return None
    with morning_session.session_lock(session_dir):
        return dict(morning_session._load_state(session_dir, session_id))


def _resume_same_date(
    repo: Path,
    *,
    session_id: str,
    private_root: Path,
) -> dict[str, Any]:
    state = _session_state(repo, session_id)
    if state is None:
        raise MorningControlError("active session state is missing")
    if state.get("status") == "paused":
        morning_session.command_resume(
            argparse.Namespace(repo=str(repo), session=session_id)
        )
    deferred = preparation.record_late_actions_for_active_session(
        repo,
        session_id,
        review_date=str(state.get("review_date") or ""),
    )
    display = publish_first_display(
        repo,
        session_id=session_id,
        private_root=private_root,
    )
    return {
        "schema": CONTROL_SCHEMA,
        "status": "ready_existing_session",
        "session_id": session_id,
        "display": display,
        "generation_id": state.get("generation_id"),
        "queue_binding_status": "queue_bound",
        "pack_binding_status": (
            "pack_bound" if state.get("delivery_pack_sha256") else "not_bound"
        ),
        "session_binding_status": "session_bound",
        "late_action_deferred_receipts": deferred["receipts"],
        "late_action_deferred_count": deferred["deferred_count"],
        "requested_model": MODEL,
        "requested_reasoning_effort": REASONING,
        "runtime_identity_status": "not_invoked_for_existing_session",
        "learner_evidence_write_count": 0,
        "formal_write_count": 0,
    }


def _clear_old_session_gate(repo: Path, review_date: str) -> dict[str, Any] | None:
    runtime = preparation._runtime(repo)
    active_session = runtime.get("active_session_id")
    if not isinstance(active_session, str) or not active_session:
        return None
    active_date = str(runtime.get("review_date") or "")
    if active_date == review_date:
        return {
            "status": "same_date_active",
            "session_id": active_session,
        }
    closeout = morning_session.auto_close_completed_session(repo, active_session)
    if closeout.get("status") not in {"closed", "already_closed"}:
        return {
            "status": "blocked_active_session",
            "active_session_id": active_session,
            "active_review_date": active_date or None,
            "closeout": closeout,
            "learner_evidence_write_count": 0,
            "formal_write_count": 0,
        }
    return {"status": "old_session_closed", "closeout": closeout}


def start_morning(
    repo: str | Path,
    *,
    review_date: str,
    preparation_job_id: str | None = None,
    private_root: str | Path | None = None,
    timeout_seconds: int = preparation.DEFAULT_TIMEOUT_SECONDS,
) -> dict[str, Any]:
    del timeout_seconds  # retained only for CLI compatibility; start never authors inline
    root = preparation._resolved_repo(Path(repo))
    day = preparation._date(review_date)
    private = current_evidence._private_root(private_root)
    gate = _clear_old_session_gate(root, day)
    if gate and gate.get("status") == "same_date_active":
        return _resume_same_date(
            root,
            session_id=str(gate["session_id"]),
            private_root=private,
        )
    if gate and gate.get("status") == "blocked_active_session":
        return {"schema": CONTROL_SCHEMA, **gate}

    queue = preparation._queue_for_date(root, day)
    if preparation_job_id is None:
        generation = preparation.ensure_fresh_generation(root, day)
        queue = Path(generation["queue"])
    elif not queue.is_file():
        queue = preparation._ensure_queue(root, day) or queue
    if not queue.is_file():
        return {
            "schema": CONTROL_SCHEMA,
            "status": "blocked",
            "error_code": "morning_action_queue_missing",
            "review_date": day,
            "learner_evidence_write_count": 0,
            "formal_write_count": 0,
        }

    try:
        if preparation_job_id:
            job = preparation.adopt_current_job(
                root,
                preparation_job_id,
                supersede_duplicates=True,
            )
        else:
            current = preparation._load_current_pointer(root, day)
            if current is not None:
                job = preparation.adopt_current_job(
                    root,
                    str(current["job_id"]),
                    supersede_duplicates=True,
                )
            else:
                job = preparation.enqueue_job(
                    root,
                    review_date=day,
                    queue=queue,
                    trigger="manual",
                )
    except preparation.PreparationError:
        candidate_queue = queue
        if preparation_job_id:
            _, candidate_state = preparation._load_state(
                root, preparation_job_id
            )
            candidate_queue = preparation._repo_file(
                root, candidate_state["queue_ref"], "named job queue"
            )
        freshness = preparation.queue_generation.validate_generation_freshness(
            root, candidate_queue
        )
        if not freshness.get("generation_id") or freshness.get("fresh"):
            raise
        replacement = preparation.ensure_fresh_generation(root, day)
        next_job = preparation.enqueue_job(
            root,
            review_date=day,
            queue=Path(replacement["queue"]),
            trigger="manual",
        )
        return {
            "schema": CONTROL_SCHEMA,
            "status": "preparation_in_progress",
            "review_date": day,
            "preparation": next_job,
            "stale_pre_session_replaced": True,
            "queue_binding_status": "queue_bound",
            "pack_binding_status": "not_bound",
            "session_binding_status": "not_bound",
            "requested_model": MODEL,
            "requested_reasoning_effort": REASONING,
            "runtime_identity_status": "requested_unverified",
            "learner_evidence_write_count": 0,
            "formal_write_count": 0,
        }
    if job.get("status") != "ready":
        return {
            "schema": CONTROL_SCHEMA,
            "status": "preparation_in_progress",
            "review_date": day,
            "preparation": job,
            "queue_binding_status": "queue_bound",
            "pack_binding_status": "not_bound",
            "session_binding_status": "not_bound",
            "requested_model": MODEL,
            "requested_reasoning_effort": REASONING,
            "runtime_identity_status": "requested_unverified",
            "learner_evidence_write_count": 0,
            "formal_write_count": 0,
        }

    bound_queue_ref = job.get("queue_ref")
    if isinstance(bound_queue_ref, str) and bound_queue_ref:
        queue = preparation._repo_file(root, bound_queue_ref, "ready job queue")

    manifest_ref = str(job.get("manifest_ref") or "")
    session_id = f"MR-{day}-{str(job['queue_sha256'])[:8]}"
    existing = _session_state(root, session_id)
    if existing is not None:
        if existing.get("status") in {"in_progress", "paused"}:
            return _resume_same_date(
                root,
                session_id=session_id,
                private_root=private,
            )
        return {
            "schema": CONTROL_SCHEMA,
            "status": "date_session_already_closed",
            "review_date": day,
            "session_id": session_id,
            "pack_id": job.get("pack_id"),
            "manifest_ref": manifest_ref,
            "manifest_sha256": job.get("manifest_sha256"),
            "learner_evidence_write_count": 0,
            "formal_write_count": 0,
        }
    try:
        started = morning_session.command_start(
            argparse.Namespace(
                repo=str(root),
                queue=str(queue),
                session_id=session_id,
                item=[],
                prepared_manifest=manifest_ref,
            )
        )
    except morning_session.SessionError as exc:
        if "queue_generation_stale_pre_session" not in str(exc):
            raise
        replacement = preparation.ensure_fresh_generation(root, day)
        replacement_queue = Path(replacement["queue"])
        next_job = preparation.enqueue_job(
            root,
            review_date=day,
            queue=replacement_queue,
            trigger="manual",
        )
        return {
            "schema": CONTROL_SCHEMA,
            "status": "preparation_in_progress",
            "review_date": day,
            "preparation": next_job,
            "stale_pre_session_replaced": True,
            "requested_model": MODEL,
            "requested_reasoning_effort": REASONING,
            "runtime_identity_status": "requested_unverified",
            "learner_evidence_write_count": 0,
            "formal_write_count": 0,
        }
    display = publish_first_display(
        root,
        session_id=session_id,
        private_root=private,
    )
    return {
        "schema": CONTROL_SCHEMA,
        "status": "ready",
        "review_date": day,
        "preparation_job_id": job["job_id"],
        "pack_id": job["pack_id"],
        "manifest_ref": manifest_ref,
        "manifest_sha256": job["manifest_sha256"],
        "generation_id": job.get("generation_id"),
        "queue_binding_status": "queue_bound",
        "pack_binding_status": "pack_bound",
        "session_binding_status": "session_bound",
        "session_id": session_id,
        "session": started,
        "display": display,
        "requested_model": MODEL,
        "requested_reasoning_effort": REASONING,
        "runtime_identity_status": "requested_unverified",
        "confirmed_model": None,
        "confirmed_reasoning_effort": None,
        "luna_call_count": 0,
        "learner_evidence_write_count": 0,
        "formal_write_count": 0,
    }


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo", type=Path, default=Path.cwd())
    parser.add_argument("--private-root", type=Path)
    parser.add_argument("--date", required=True)
    parser.add_argument("--preparation-job")
    parser.add_argument(
        "--timeout-seconds",
        type=int,
        default=preparation.DEFAULT_TIMEOUT_SECONDS,
        help="deprecated compatibility option; preparation is never run inline",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        result = start_morning(
            args.repo,
            review_date=args.date,
            preparation_job_id=args.preparation_job,
            private_root=args.private_root,
            timeout_seconds=args.timeout_seconds,
        )
    except (OSError, ValueError, RuntimeError) as exc:
        result = {
            "schema": CONTROL_SCHEMA,
            "status": "blocked",
            "error_code": str(exc),
            "learner_evidence_write_count": 0,
            "formal_write_count": 0,
        }
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    return 0 if result.get("status") in {
        "ready", "ready_existing_session", "preparation_in_progress"
    } else 2


if __name__ == "__main__":
    raise SystemExit(main())
