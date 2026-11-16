"""Local cache freshness after a verified commit, with opt-in publication.

Nested public owners share one in-process boundary. Low-level ledger writes only
invalidate; refresh happens after the outer owner releases its transaction locks.
"""
from __future__ import annotations

import argparse
import contextvars
import datetime as dt
import functools
import hashlib
import inspect
import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Any, Callable
from zoneinfo import ZoneInfo

import runtime_paths_408 as _paths

CANONICAL_REPO = _paths.repo_root()
PUBLICATION_SCRIPT = _paths.bridge_root() / 'study_publication.py'
LEDGER_REL = Path("wiki/study_vaults/408-full/state/review-loop/events.jsonl")
_pending: contextvars.ContextVar[list[tuple[Path, str]] | None] = contextvars.ContextVar(
    "cs408_verified_commit_followups", default=None
)


def _production(repo: Path) -> bool:
    return repo.resolve() == CANONICAL_REPO.resolve()


def note_ledger_commit(path: Path, event: dict[str, Any]) -> None:
    """Invalidate immediately after fsync, even if later projection work fails."""
    if path.resolve() != (CANONICAL_REPO / LEDGER_REL).resolve():
        return
    event_id = str(event.get("event_id") or "")
    if not event_id:
        return
    from exam_tutoring_context_408 import ExamTutoringContextError, invalidate_after_commit
    try:
        invalidate_after_commit(CANONICAL_REPO, event_id)
    except (ExamTutoringContextError, OSError):
        # Strict source guards also reject every ledger append. Cache IO must
        # never interrupt completion of the already fsynced review transaction.
        pass


def _request_publication(repo: Path, event_id: str) -> dict[str, Any]:
    if not PUBLICATION_SCRIPT.is_file():
        return {"status": "PENDING", "reason": "publication_entrypoint_unavailable"}
    try:
        completed = subprocess.run(
            [sys.executable, str(PUBLICATION_SCRIPT), "request", "--subject", "408",
             "--event-id", event_id, "--repo", str(repo)],
            capture_output=True, text=True, timeout=5, check=False,
            env={**os.environ, "KAOYAN_408_PROJECT_ROOT": str(repo.resolve())},
        )
        result = json.loads(completed.stdout)
        if not isinstance(result, dict):
            raise ValueError("publication response is not an object")
        return result
    except (OSError, ValueError, subprocess.TimeoutExpired):
        # The request itself may already be durable. The same event is safe to
        # request again; never replay its learning/transaction owner.
        return {"status": "PENDING", "reason": "publication_request_needs_recovery"}


def complete_after_commit(repo: Path, event_id: str, *, publish: bool = False) -> dict[str, Any]:
    root = Path(repo).expanduser().resolve()
    if not _production(root):
        return {"status": "NOT_PRODUCTION_REPO"}
    from formal_quality_408 import allow_publication, quality_marker, ROOT_REL
    delivery = {"status": "NOT_REQUESTED", "reason": "explicit_publication_required"}
    if not allow_publication(root, event_id=event_id, native_event=True):
        return {"status": "COMMITTED_QUALITY_PENDING", "event_id": event_id,
                "local_tutoring": {"status": "PENDING", "reason": "quality_native_generation_not_ready"},
                "publication": delivery if not publish else
                {"status": "PENDING", "reason": "quality_native_generation_not_ready"}}
    from exam_tutoring_context_408 import (
        ExamTutoringContextError, refresh_after_commit,
    )
    try:
        from syllabus_context_408 import ensure_formal_frequency_links
        ensure_formal_frequency_links(root)
        local = refresh_after_commit(root, dt.datetime.now(ZoneInfo("Asia/Shanghai")).date().isoformat())
    except (ExamTutoringContextError, OSError, ValueError) as exc:
        local = {"status": "PENDING", "reason": type(exc).__name__,
                 "fallback": "current_question_only"}
    publication_event_id = None
    if publish and local.get("status") in {"refreshed", "already_current"} and local.get("source_version"):
        from study_personalization_408 import current_identity
        personal = current_identity(root)
        if personal["status"] == "ready":
            publication_event_id = "personalization-408:" + hashlib.sha256(
                json.dumps([event_id, personal["version"]], ensure_ascii=False,
                           separators=(",", ":")).encode()).hexdigest()
            pointer = root / ROOT_REL / "current.json"
            binding = quality_marker(root) or (json.loads(pointer.read_bytes()) if pointer.is_file() else {})
            if (binding.get("native_event_id") == event_id and binding.get("source_version", local["source_version"]) == local["source_version"]
                    and binding.get("publication_event_id")):
                publication_event_id = binding["publication_event_id"]
            delivery = _request_publication(root, publication_event_id)
        else:
            delivery = {"status": "PENDING", "reason": "personalization_not_ready"}
    elif publish:
        delivery = {"status": "PENDING", "reason": "local_tutoring_refresh_pending"}
    result = {"event_id": event_id, "publication_event_id": publication_event_id, "local_tutoring": local, "publication": delivery}
    from exam_tutoring_context_408 import DEFAULT_STATE_REL, _atomic_write, _canonical_bytes
    try:
        _atomic_write(root / DEFAULT_STATE_REL / "last-commit-followup.json", _canonical_bytes(result), mode=0o600)
    except OSError:
        result["followup_receipt_status"] = "PENDING"
    return result


def _committed_identity(result: Any) -> str | None:
    value = result if isinstance(result, dict) else (
        result.as_dict() if hasattr(result, "as_dict") else {}
    )
    event = value.get("event")
    if isinstance(event, dict) and event.get("event_id"):
        verification = value.get("verification")
        if verification is None or (isinstance(verification, dict) and verification.get("status") == "verified"):
            return str(event["event_id"])
    if value.get("correction_review_commit_verified") is True and value.get("correction_review_event_id"):
        return str(value["correction_review_event_id"])
    if value.get("status") in {"COMMITTED", "ALREADY_COMMITTED", "COMMITTED_DISPLAY_ASSETS_PENDING"}:
        identity = value.get("transaction_id") or value.get("idempotency_key")
        if identity:
            return "formal-408:" + hashlib.sha256(str(identity).encode()).hexdigest()[:32]
    if (value.get("schema_version") == "sol-curation-terminal-binding-408-v2"
            and value.get("status") == "bound"
            and value.get("outcome") in {"curated", "created", "updated", "already_current"}
            and str(value.get("publication_event_id") or "").startswith("formal-terminal-408:")):
        return value["publication_event_id"]
    return None


class defer_automatic_followups:
    """Let a compound owner refresh once, after its profile commit succeeds.

    Native writers still save their facts/invalidation normally. Only automatic
    draining is suppressed; the compound owner must call complete_after_commit
    and retain a recoverable pending state on failure.
    """

    def __enter__(self):
        self.token = _pending.set([])
        return self

    def __exit__(self, exc_type, exc_value, traceback):
        _pending.reset(self.token)

    def __call__(self, function):
        @functools.wraps(function)
        def wrapped(*args, **kwargs):
            with type(self)():
                return function(*args, **kwargs)
        return wrapped


def study_commit_boundary(function: Callable[..., Any]) -> Callable[..., Any]:
    """Drain verified nested commits only after the outer public owner returns."""
    signature = inspect.signature(function)

    @functools.wraps(function)
    def wrapped(*args: Any, **kwargs: Any) -> Any:
        outer = _pending.get()
        pending = outer if outer is not None else []
        token = _pending.set(pending) if outer is None else None
        try:
            result = function(*args, **kwargs)
            identity = _committed_identity(result)
            binding = signature.bind_partial(*args, **kwargs)
            binding.apply_defaults()
            bound = binding.arguments
            repo = bound.get("repo_root", bound.get("repo"))
            if identity and repo is not None:
                root = Path(repo).expanduser().resolve()
                if _production(root):
                    if not isinstance(result, dict) and getattr(result, "status", "").startswith("COMMITTED"):
                        from exam_tutoring_context_408 import ExamTutoringContextError, invalidate_after_commit
                        try:
                            invalidate_after_commit(root, identity)
                        except (ExamTutoringContextError, OSError):
                            # Source guards still revoke changed formal inputs.
                            pass
                    pending.append((root, identity))
            if token is not None and pending:
                # The native snapshot includes all verified nested commits.
                outcomes = []
                for root, event_id in dict.fromkeys(pending):
                    try:
                        outcomes.append(complete_after_commit(root, event_id))
                    except Exception as exc:
                        outcomes.append({"event_id": event_id, "status": "PENDING",
                                         "reason": "followup_failed:" + type(exc).__name__})
                if isinstance(result, dict):
                    result = {**result, "study_followup": outcomes[-1]}
                elif hasattr(result, "study_followup"):
                    result.study_followup = outcomes[-1]
            return result
        finally:
            if token is not None:
                _pending.reset(token)

    return wrapped


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="Resume local refresh for an already verified 408 commit")
    parser.add_argument("--repo", type=Path, default=CANONICAL_REPO)
    parser.add_argument("--event-id", required=True, help="Original verified business event; never create a learning event")
    parser.add_argument("--publish", action="store_true", help="Request a replica for an explicitly authorized external handoff")
    args = parser.parse_args(argv)
    result = complete_after_commit(args.repo, args.event_id, publish=args.publish)
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    return 0 if result.get("local_tutoring", {}).get("status") in {"refreshed", "already_current"} else 2


if __name__ == "__main__":
    raise SystemExit(main())
