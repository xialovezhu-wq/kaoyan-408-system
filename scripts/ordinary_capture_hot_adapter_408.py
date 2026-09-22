#!/usr/bin/env python3
"""Receipt-gated hot capture for one non-morning canonical 408 outcome.

The adapter is the bounded counterpart of
``intake_fact_capture_408.capture_ordinary_review_event``.  It accepts only an
exact event identity, exact event SHA-256, and the exact hot commit receipt
returned by the canonical outcome transaction.  It then performs bounded indexed
verification and commits one answer-safe capture through the shared capture hot
writer.  On an explicitly authorized intake turn, the same capture lock also
commits a separate ``capture_authorized`` event.  Neither authoritative JSONL
ledger is replayed.

An ordinary canonical outcome is learning evidence, not formalization
authorization.  Callers must pass ``formalization_authorized=True`` only from a
current-message explicit fast-intake route.  The default compatibility route
still leaves the capture ``captured_unconfirmed``.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import stat
import sys
from pathlib import Path
from typing import Any, Callable

import capture_hot_writer_408 as capture_hot
import intake_fact_capture_408 as capture_model
import review_hot_state_408 as review_hot


SCHEMA = "ordinary_capture_hot_adapter_v1"
FAILURE_RESULTS = frozenset({"partial", "wrong", "uncertain"})
NON_CAPTURE_RESULTS = frozenset({"independent_correct", "fragile_correct"})
SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
CAPTURE_HOT_DIRNAME = ".capture-hot-writer-v1"
RECOVERY_SCHEMA = "ordinary_capture_authorization_recovery_v1"
FAST_INTENT_SCHEMA = "managed_408_fast_intake_intent_v1"
AUTHORIZATION_BINDING_SCHEMA = "managed_408_explicit_authorization_binding_v1"
MANAGED_AUTHORIZATION_RECEIPT_SCHEMA = (
    "managed_408_study_authorization_receipt_v1"
)
MANAGED_AUTHORIZATION_RECEIPT_KIND = "verified_current_message_intent"
AUTHORIZATION_PROOF_SCHEMA = "managed_408_capture_authorization_proof_v1"
RICH_INTENT_REL = Path(
    "wiki/study_vaults/408-full/state/managed-408-rich-intake-turn/objects/intent"
)
MANAGED_AUTHORIZATION_REL = Path(
    "wiki/study_vaults/408-full/state/managed-408-study-authorizations/objects"
)
MAX_AUTHORIZATION_OBJECT_BYTES = 64 * 1024


FaultInjector = Callable[[str], None]


class OrdinaryCaptureHotError(RuntimeError):
    """An ordinary hot capture failed before a verified joint commit."""


def capture_hot_root(repo_root: str | Path) -> Path:
    repo = Path(repo_root).expanduser().resolve()
    return capture_model.capture_root(repo) / CAPTURE_HOT_DIRNAME


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _sha(value: object, label: str) -> str:
    text = str(value or "")
    if not SHA256_RE.fullmatch(text):
        raise OrdinaryCaptureHotError(f"{label} is not a lowercase SHA-256")
    return text


def _json_bytes(value: Any, *, pretty: bool = False) -> bytes:
    options: dict[str, Any] = {"ensure_ascii": False, "sort_keys": True}
    if pretty:
        options["indent"] = 2
    else:
        options["separators"] = (",", ":")
    return (json.dumps(value, **options) + "\n").encode("utf-8")


def _value_sha256(value: Any) -> str:
    return hashlib.sha256(
        json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()


def _read_content_addressed_json(
    repo: Path,
    *,
    relative_ref: str,
    expected_ref: Path,
    expected_sha256: str,
    label: str,
) -> dict[str, Any]:
    digest = _sha(expected_sha256, label)
    ref = Path(str(relative_ref or ""))
    if ref.is_absolute() or ".." in ref.parts or ref != expected_ref:
        raise OrdinaryCaptureHotError(f"{label} reference is invalid")
    cursor = repo
    for part in ref.parts:
        cursor = cursor / part
        if cursor.exists() and stat.S_ISLNK(cursor.lstat().st_mode):
            raise OrdinaryCaptureHotError(f"{label} symlink is rejected")
    path = repo / ref
    try:
        info = path.lstat()
        if (
            stat.S_ISLNK(info.st_mode)
            or not stat.S_ISREG(info.st_mode)
            or info.st_size > MAX_AUTHORIZATION_OBJECT_BYTES
        ):
            raise OrdinaryCaptureHotError(f"{label} artifact is invalid")
        raw = path.read_bytes()
        value = json.loads(raw.decode("utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise OrdinaryCaptureHotError(f"{label} artifact is invalid") from exc
    if len(raw) != info.st_size or hashlib.sha256(raw).hexdigest() != digest:
        raise OrdinaryCaptureHotError(f"{label} content address drifted")
    if not isinstance(value, dict):
        raise OrdinaryCaptureHotError(f"{label} artifact is invalid")
    return value


def _managed_authorization_proof(
    repo: Path,
    *,
    event: dict[str, Any],
    event_sha256: str,
    review_commit_receipt_sha256: str,
    authorization_receipt_ref: str | None,
    authorization_receipt_sha256: str | None,
) -> dict[str, Any]:
    receipt_sha = _sha(
        authorization_receipt_sha256,
        "managed authorization receipt SHA-256",
    )
    receipt_ref = str(authorization_receipt_ref or "")
    receipt = _read_content_addressed_json(
        repo,
        relative_ref=receipt_ref,
        expected_ref=MANAGED_AUTHORIZATION_REL / f"{receipt_sha}.json",
        expected_sha256=receipt_sha,
        label="managed authorization receipt",
    )
    core = dict(receipt)
    receipt_id = core.pop("receipt_id", None)
    core_sha = hashlib.sha256(_json_bytes(core)).hexdigest()
    if (
        receipt.get("schema") != MANAGED_AUTHORIZATION_RECEIPT_SCHEMA
        or receipt.get("kind") != MANAGED_AUTHORIZATION_RECEIPT_KIND
        or receipt_id != f"MSAR-{core_sha[:24].upper()}"
        or receipt.get("authorization_body_stored") is not False
        or receipt.get("formal_write_count") != 0
        or receipt.get("review_event_id") != event.get("event_id")
        or receipt.get("review_event_sha256") != event_sha256
        or receipt.get("review_commit_receipt_sha256")
        != review_commit_receipt_sha256
    ):
        raise OrdinaryCaptureHotError(
            "managed authorization receipt binding is invalid"
        )
    intent_sha = _sha(
        receipt.get("intent_receipt_sha256"),
        "managed authorization intent SHA-256",
    )
    intent_ref = str(receipt.get("intent_receipt_ref") or "")
    intent = _read_content_addressed_json(
        repo,
        relative_ref=intent_ref,
        expected_ref=RICH_INTENT_REL / f"{intent_sha}.json",
        expected_sha256=intent_sha,
        label="managed authorization intent",
    )
    if (
        intent.get("schema") != FAST_INTENT_SCHEMA
        or intent.get("kind") != "explicit_fast_intake_intent"
        or intent.get("authorization_body_stored") is not False
        or intent.get("formal_write_count") != 0
        or intent.get("session_id") != event.get("session_id")
        or intent.get("item_id") != event.get("item_id")
        or intent.get("canonical_first_result") != event.get("first_result")
        or intent.get("choice_result") != event.get("choice_result")
        or intent.get("confidence") != event.get("confidence")
        or intent.get("reasoning_result") != event.get("reasoning_result")
        or intent.get("event_time") != event.get("event_time")
        or intent.get("observed_date") != event.get("observed_date")
        or intent.get("idempotency_key") != event.get("idempotency_key")
    ):
        raise OrdinaryCaptureHotError(
            "managed authorization intent does not bind the canonical outcome"
        )
    binding_material = {
        "schema": AUTHORIZATION_BINDING_SCHEMA,
        "request_id": intent.get("request_id"),
        "display_receipt_sha256": intent.get("display_receipt_sha256"),
        "authorization_message_sha256": intent.get(
            "authorization_message_sha256"
        ),
        "authorization_message_size_bytes": intent.get(
            "authorization_message_size_bytes"
        ),
    }
    binding_sha = hashlib.sha256(_json_bytes(binding_material)).hexdigest()
    for field in (
        "request_id",
        "display_receipt_sha256",
        "authorization_message_sha256",
        "authorization_message_size_bytes",
        "authorization_binding_sha256",
    ):
        if receipt.get(field) != intent.get(field):
            raise OrdinaryCaptureHotError(
                "managed authorization receipt and intent differ"
            )
    if intent.get("authorization_binding_sha256") != binding_sha:
        raise OrdinaryCaptureHotError(
            "managed authorization message binding is invalid"
        )
    if (
        not isinstance(intent.get("authorization_message_size_bytes"), int)
        or not 0 < intent["authorization_message_size_bytes"] <= 4096
        or not SHA256_RE.fullmatch(
            str(intent.get("authorization_message_sha256") or "")
        )
        or not SHA256_RE.fullmatch(
            str(intent.get("display_receipt_sha256") or "")
        )
    ):
        raise OrdinaryCaptureHotError(
            "managed authorization message metadata is invalid"
        )
    return {
        "schema": AUTHORIZATION_PROOF_SCHEMA,
        "proof_kind": "managed_current_message_intent",
        "study_authorization_receipt_ref": receipt_ref,
        "study_authorization_receipt_sha256": receipt_sha,
        "intent_receipt_ref": intent_ref,
        "intent_receipt_sha256": intent_sha,
        "request_id": intent["request_id"],
        "display_receipt_sha256": intent["display_receipt_sha256"],
        "authorization_message_sha256": intent[
            "authorization_message_sha256"
        ],
        "authorization_message_size_bytes": intent[
            "authorization_message_size_bytes"
        ],
        "authorization_binding_sha256": binding_sha,
        "authorization_body_stored": False,
        "formal_write_count": 0,
    }


def authorization_idempotency_key(event_id: str, capture_id: str) -> str:
    """Bind explicit authorization to one canonical outcome and capture."""

    material = f"{event_id}:{capture_id}".encode("utf-8")
    return "authorize:ordinary:" + hashlib.sha256(material).hexdigest()


def _fault(injector: FaultInjector | None, phase: str) -> None:
    if injector is not None:
        injector(phase)


def _authorization_recovery_ticket(
    *,
    capture_id: str,
    fact_event_id: str,
    fact_receipt_sha256: str,
    authorization_idempotency_key: str,
    review_event_id: str,
    review_event_sha256: str,
    review_commit_receipt_sha256: str,
    authorization_proof: dict[str, Any],
    pending_reconcile_required: bool,
) -> dict[str, Any]:
    material = {
        "schema": RECOVERY_SCHEMA,
        "capture_id": capture_id,
        "fact_event_id": fact_event_id,
        "fact_receipt_sha256": fact_receipt_sha256,
        "authorization_idempotency_key": authorization_idempotency_key,
        "review_event_id": review_event_id,
        "review_event_sha256": review_event_sha256,
        "review_commit_receipt_sha256": review_commit_receipt_sha256,
        "authorization_proof": authorization_proof,
        "authorization_proof_sha256": _value_sha256(
            authorization_proof
        ),
        "pending_reconcile_required": pending_reconcile_required,
        "retry_with_same_idempotency_key": not pending_reconcile_required,
        "formal_write_count": 0,
    }
    digest = hashlib.sha256(
        json.dumps(
            material,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()
    return {
        **material,
        "ticket_id": f"OAR-{digest[:24]}",
        "ticket_sha256": digest,
    }


def _review_state_root(repo: Path, raw: str | Path | None) -> Path:
    state = (
        Path(raw).expanduser()
        if raw is not None
        else repo / review_hot.DEFAULT_STATE_RELATIVE
    )
    if not state.is_absolute():
        state = repo / state
    state = state.resolve()
    try:
        state.relative_to(repo)
    except ValueError as exc:
        raise OrdinaryCaptureHotError(
            "review hot state escaped the repository"
        ) from exc
    return state


def _regular_receipt(path: Path, expected_sha256: str) -> None:
    try:
        info = path.lstat()
    except OSError as exc:
        raise OrdinaryCaptureHotError(
            "review commit receipt artifact is missing"
        ) from exc
    if stat.S_ISLNK(info.st_mode) or not stat.S_ISREG(info.st_mode):
        raise OrdinaryCaptureHotError(
            "review commit receipt artifact is not a regular file"
        )
    if _sha256_file(path) != expected_sha256:
        raise OrdinaryCaptureHotError("review commit receipt artifact drifted")


def _exact_review_event(
    repo: Path,
    *,
    event_id: str,
    event_sha256: str,
    receipt_sha256: str,
    review_state_dir: str | Path | None,
) -> tuple[dict[str, Any], dict[str, Any], dict[str, Any]]:
    state = _review_state_root(repo, review_state_dir)
    receipt_path = state / "receipts" / f"{receipt_sha256}.json"
    _regular_receipt(receipt_path, receipt_sha256)
    try:
        verification = review_hot.verify_commit(
            repo,
            receipt_sha256=receipt_sha256,
            state_dir=state,
        )
        found = review_hot.lookup_event(
            repo,
            event_id=event_id,
            state_dir=state,
        )
    except (review_hot.HotStateError, OSError) as exc:
        raise OrdinaryCaptureHotError(
            f"exact review commit verification failed: {exc}"
        ) from exc
    verification_stats = verification.get("scan_stats") or {}
    lookup_stats = found.get("scan_stats") or {}
    if (
        verification.get("status") != "verified"
        or verification.get("event_id") != event_id
        or verification.get("event_sha256") != event_sha256
        or verification.get("receipt_sha256") != receipt_sha256
        or verification.get("formal_write_count") != 0
        or found.get("status") != "found"
        or found.get("event_sha256") != event_sha256
        or found.get("formal_write_count") != 0
        or int(verification_stats.get("ledger_full_scan_count", -1)) != 0
        or int(lookup_stats.get("ledger_full_scan_count", -1)) != 0
        or int(verification_stats.get("ledger_exact_range_reads", 0)) != 1
        or int(lookup_stats.get("ledger_exact_range_reads", 0)) != 1
    ):
        raise OrdinaryCaptureHotError(
            "review commit receipt did not bind the exact canonical event"
        )
    event = found.get("event")
    if not isinstance(event, dict):
        raise OrdinaryCaptureHotError("indexed canonical event is invalid")
    return event, verification, found


def capture_managed_ordinary_outcome(
    repo_root: str | Path,
    *,
    event_id: str,
    event_sha256: str,
    review_commit_receipt_sha256: str,
    review_state_dir: str | Path | None = None,
    hot_root: str | Path | None = None,
    formalization_authorized: bool = False,
    authorization_receipt_ref: str | None = None,
    authorization_receipt_sha256: str | None = None,
    include_diagnostics: bool = True,
    fault_injector: FaultInjector | None = None,
) -> dict[str, Any]:
    """Conditionally commit one exact ordinary failure as a hot capture.

    ``formalization_authorized`` is an assertion made only by the explicit
    fast-intake orchestrator.  It never derives from result, confidence, or the
    canonical outcome itself.
    """

    raise OrdinaryCaptureHotError(
        "legacy_ordinary_capture_writer_retired_use_managed_current_question_turn"
    )

    repo = Path(repo_root).expanduser().resolve()
    if not repo.is_dir():
        raise OrdinaryCaptureHotError("repository does not exist")
    if not isinstance(formalization_authorized, bool):
        raise OrdinaryCaptureHotError(
            "formalization_authorized must be an explicit boolean"
        )
    if formalization_authorized and (
        not authorization_receipt_ref or not authorization_receipt_sha256
    ):
        raise OrdinaryCaptureHotError(
            "managed_authorization_receipt_required"
        )
    if not formalization_authorized and (
        authorization_receipt_ref is not None
        or authorization_receipt_sha256 is not None
    ):
        raise OrdinaryCaptureHotError(
            "managed authorization receipt requires explicit authorization mode"
        )
    if not isinstance(include_diagnostics, bool):
        raise OrdinaryCaptureHotError(
            "include_diagnostics must be a boolean"
        )
    event_id = str(event_id or "").strip()
    if not event_id:
        raise OrdinaryCaptureHotError("event_id is required")
    event_sha = _sha(event_sha256, "event_sha256")
    receipt_sha = _sha(
        review_commit_receipt_sha256,
        "review_commit_receipt_sha256",
    )
    event, verification, found = _exact_review_event(
        repo,
        event_id=event_id,
        event_sha256=event_sha,
        receipt_sha256=receipt_sha,
        review_state_dir=review_state_dir,
    )
    if (
        event.get("schema") != capture_model.REVIEW_EVENT_SCHEMA
        or event.get("event_kind") != "outcome"
        or event.get("source") not in capture_model.ORDINARY_REVIEW_SOURCES
        or event.get("question_valid") is not True
        or event.get("formal_write_authorized") is not False
    ):
        raise OrdinaryCaptureHotError(
            "only an answer-safe ordinary canonical outcome is accepted"
        )
    authorization_proof: dict[str, Any] | None = None
    if formalization_authorized:
        authorization_proof = _managed_authorization_proof(
            repo,
            event=event,
            event_sha256=event_sha,
            review_commit_receipt_sha256=receipt_sha,
            authorization_receipt_ref=authorization_receipt_ref,
            authorization_receipt_sha256=authorization_receipt_sha256,
        )
    first_result = str(event.get("first_result") or "")
    if first_result in NON_CAPTURE_RESULTS:
        return {
            "schema": SCHEMA,
            "status": "NOT_ELIGIBLE",
            "created": False,
            "capture_id": None,
            "quality_status": "not_eligible",
            "review_event_id": event_id,
            "review_event_sha256": event_sha,
            "review_commit_receipt_sha256": receipt_sha,
            "formalization_authorized": False,
            "formal_write_authorized": False,
            "formal_write_count": 0,
            "review_ledger_full_scan_count": 0,
            "capture_ledger_full_scan_count": 0,
        }
    if first_result not in FAILURE_RESULTS:
        raise OrdinaryCaptureHotError(
            "ordinary outcome result is outside the capture policy"
        )
    try:
        payload = capture_model.build_ordinary_review_capture_payload(event)
    except capture_model.CaptureError as exc:
        raise OrdinaryCaptureHotError(
            f"ordinary capture payload validation failed: {exc}"
        ) from exc
    ledger_root = capture_model.capture_root(repo)
    ledger = ledger_root / "events.jsonl"
    state = ledger_root / "state.json"
    if hot_root is None:
        derived = capture_hot_root(repo)
    else:
        derived = Path(hot_root).expanduser()
        if not derived.is_absolute():
            derived = repo / derived
        derived = derived.resolve()
    if not ledger.is_file() or not state.is_file():
        raise OrdinaryCaptureHotError(
            "capture truth files are not prepared; cold bootstrap is required"
        )
    try:
        derived.relative_to(repo)
    except ValueError as exc:
        raise OrdinaryCaptureHotError(
            "capture hot state escaped the repository"
        ) from exc
    expected = capture_hot.canonical_fact_event_from_payload(payload)
    authorization: dict[str, Any] | None = None
    authorization_key: str | None = None
    try:
        with capture_hot.capture_ledger_lock(ledger) as lock_token:
            committed = capture_hot.capture_fact(
                ledger,
                state,
                derived,
                lock_token=lock_token,
                validated_payload=payload,
            )
            fact_joint = committed.get("joint_verification")
            fact_quality = str(
                committed.get("quality_status") or "captured_unconfirmed"
            )
            if (
                committed.get("status") not in {"CAPTURED", "ALREADY_COMMITTED"}
                or not isinstance(fact_joint, dict)
                or fact_joint.get("status") != "PASS"
                or committed.get("event_id") != expected.get("event_id")
                or fact_joint.get("event_id") != expected.get("event_id")
                or committed.get("capture_id") != expected.get("capture_id")
                or fact_joint.get("capture_id") != expected.get("capture_id")
                or committed.get("receipt_sha256")
                != fact_joint.get("receipt_sha256")
                or not SHA256_RE.fullmatch(
                    str(committed.get("receipt_sha256") or "")
                )
                or fact_quality
                not in {"captured_unconfirmed", capture_model.PENDING_FORMAL_STATUS}
                or int(committed.get("state_read_count", 99)) > 2
                or int(committed.get("ledger_full_read_count", -1)) != 0
                or int(
                    committed.get("review_or_session_ledger_read_count", -1)
                )
                != 0
                or committed.get("formal_write_count") != 0
                or fact_joint.get("formal_write_count") != 0
            ):
                raise OrdinaryCaptureHotError(
                    "capture fact joint receipt did not verify"
                )
            if formalization_authorized:
                authorization_key = authorization_idempotency_key(
                    event_id, str(expected["capture_id"])
                )
                try:
                    _fault(
                        fault_injector,
                        "after_fact_capture_before_authorization",
                    )
                    authorization = capture_hot.authorize_capture(
                        ledger,
                        state,
                        derived,
                        lock_token=lock_token,
                        capture_id=str(expected["capture_id"]),
                        idempotency_key=authorization_key,
                        authorization_proof=authorization_proof,
                        fault_injector=fault_injector,
                    )
                except Exception as exc:
                    pending_reconcile = (
                        derived / capture_hot.PENDING_FILENAME
                    ).is_file()
                    ticket = _authorization_recovery_ticket(
                        capture_id=str(expected["capture_id"]),
                        fact_event_id=str(expected["event_id"]),
                        fact_receipt_sha256=str(committed["receipt_sha256"]),
                        authorization_idempotency_key=authorization_key,
                        review_event_id=event_id,
                        review_event_sha256=event_sha,
                        review_commit_receipt_sha256=receipt_sha,
                        authorization_proof=dict(
                            authorization_proof or {}
                        ),
                        pending_reconcile_required=pending_reconcile,
                    )
                    value = {
                        "schema": SCHEMA,
                        "status": "AUTHORIZATION_RECOVERY_REQUIRED",
                        "reason_code": "authorization_commit_failed",
                        "created": bool(committed.get("created")),
                        "capture_id": expected["capture_id"],
                        "quality_status": "unverified_after_authorization_failure",
                        "fact_event_id": expected["event_id"],
                        "fact_receipt_sha256": committed["receipt_sha256"],
                        "review_event_id": event_id,
                        "review_event_sha256": event_sha,
                        "review_commit_receipt_sha256": receipt_sha,
                        "formalization_authorized": False,
                        "formal_write_authorized": False,
                        "formal_write_count": 0,
                        "recovery_ticket": ticket,
                    }
                    if include_diagnostics:
                        value["diagnostic_reason"] = str(exc)
                        value["fact_commit"] = committed
                    return value
    except (capture_hot.CaptureHotWriterError, OSError, KeyError) as exc:
        raise OrdinaryCaptureHotError(
            f"capture hot joint commit failed closed: {exc}"
        ) from exc
    fact_joint = committed["joint_verification"]
    quality_status = "captured_unconfirmed"
    terminal_status = str(committed["status"])
    terminal_receipt_sha256 = str(committed["receipt_sha256"])
    authorization_event_id: str | None = None
    authorization_created = False
    if formalization_authorized:
        if not isinstance(authorization, dict):
            raise OrdinaryCaptureHotError(
                "explicit intake did not produce an authorization result"
            )
        authorization_joint = authorization.get("joint_verification")
        expected_authorization_proof_sha = _value_sha256(
            authorization_proof
        )
        if (
            authorization.get("status") not in {
                "AUTHORIZED",
                "ALREADY_AUTHORIZED",
            }
            or not isinstance(authorization_joint, dict)
            or authorization_joint.get("status") != "PASS"
            or authorization.get("capture_id") != expected.get("capture_id")
            or authorization_joint.get("capture_id")
            != expected.get("capture_id")
            or authorization_joint.get("event_type") != "capture_authorized"
            or authorization.get("receipt_sha256")
            != authorization_joint.get("receipt_sha256")
            or not SHA256_RE.fullmatch(
                str(authorization.get("receipt_sha256") or "")
            )
            or authorization.get("quality_status")
            != capture_model.PENDING_FORMAL_STATUS
            or authorization.get("formalization_authorized") is not True
            or int(authorization.get("ledger_full_read_count", -1)) != 0
            or authorization.get("formal_write_count") != 0
            or authorization_joint.get("formal_write_count") != 0
            or authorization_joint.get("authorization_proof_sha256")
            != expected_authorization_proof_sha
        ):
            raise OrdinaryCaptureHotError(
                "capture authorization joint receipt did not verify"
            )
        quality_status = capture_model.PENDING_FORMAL_STATUS
        terminal_status = (
            "CAPTURED_AND_AUTHORIZED"
            if committed.get("created") or authorization.get("created")
            else "ALREADY_AUTHORIZED"
        )
        terminal_receipt_sha256 = str(authorization["receipt_sha256"])
        authorization_event_id = str(authorization["event_id"])
        authorization_created = bool(authorization.get("created"))
    compact = {
        "schema": SCHEMA,
        "status": terminal_status,
        "created": bool(committed.get("created")) or authorization_created,
        "capture_created": bool(committed.get("created")),
        "authorization_created": authorization_created,
        "capture_id": expected["capture_id"],
        "quality_status": quality_status,
        "receipt_sha256": terminal_receipt_sha256,
        "fact_event_id": expected["event_id"],
        "fact_receipt_sha256": committed["receipt_sha256"],
        "authorization_event_id": authorization_event_id,
        "authorization_receipt_sha256": (
            authorization.get("receipt_sha256")
            if isinstance(authorization, dict)
            else None
        ),
        "authorization_idempotency_key": authorization_key,
        "authorization_proof_sha256": (
            _value_sha256(authorization_proof)
            if isinstance(authorization_proof, dict)
            else None
        ),
        "study_authorization_receipt_ref": authorization_receipt_ref,
        "study_authorization_receipt_sha256": authorization_receipt_sha256,
        "review_event_id": event_id,
        "review_event_sha256": event_sha,
        "review_commit_receipt_sha256": receipt_sha,
        "study_date": payload["study_date"],
        "formalization_authorized": formalization_authorized,
        "formal_write_authorized": False,
        "formal_write_count": 0,
        "review_ledger_full_scan_count": 0,
        "capture_ledger_full_scan_count": 0,
        "retry_with_same_idempotency_key": False,
    }
    if not include_diagnostics:
        return compact
    return {
        **committed,
        **compact,
        "review_commit_verification": verification,
        "review_lookup_scan_stats": found.get("scan_stats"),
        "fact_commit": committed,
        "capture_joint_verification": (
            authorization.get("joint_verification")
            if isinstance(authorization, dict)
            else fact_joint
        ),
        "authorization_commit": authorization,
    }


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo", default=".")
    parser.add_argument("--event-id", required=True)
    parser.add_argument("--event-sha256", required=True)
    parser.add_argument("--review-commit-receipt-sha256", required=True)
    parser.add_argument("--review-state-dir")
    parser.add_argument("--capture-hot-root")
    parser.add_argument(
        "--authorize",
        action="store_true",
        help=argparse.SUPPRESS,
    )
    parser.add_argument("--authorization-receipt-ref")
    parser.add_argument("--authorization-receipt-sha256")
    parser.add_argument(
        "--full",
        action="store_true",
        help="include bounded diagnostic subreceipts",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        if args.authorize:
            raise OrdinaryCaptureHotError(
                "naked_authorize_forbidden_use_managed_authorization_receipt"
            )
        authorization_requested = bool(
            args.authorization_receipt_ref
            or args.authorization_receipt_sha256
        )
        if authorization_requested and not (
            args.authorization_receipt_ref
            and args.authorization_receipt_sha256
        ):
            raise OrdinaryCaptureHotError(
                "managed_authorization_receipt_required"
            )
        result = capture_managed_ordinary_outcome(
            args.repo,
            event_id=args.event_id,
            event_sha256=args.event_sha256,
            review_commit_receipt_sha256=(
                args.review_commit_receipt_sha256
            ),
            review_state_dir=args.review_state_dir,
            hot_root=args.capture_hot_root,
            formalization_authorized=authorization_requested,
            authorization_receipt_ref=args.authorization_receipt_ref,
            authorization_receipt_sha256=(
                args.authorization_receipt_sha256
            ),
            include_diagnostics=args.full,
        )
    except OrdinaryCaptureHotError as exc:
        result = {
            "schema": SCHEMA,
            "status": "ERROR",
            "reason_code": str(exc),
            "formal_write_count": 0,
        }
        status = 2
    else:
        status = 0
    print(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True))
    return status


__all__ = [
    "CAPTURE_HOT_DIRNAME",
    "FAILURE_RESULTS",
    "NON_CAPTURE_RESULTS",
    "OrdinaryCaptureHotError",
    "authorization_idempotency_key",
    "capture_hot_root",
    "capture_managed_ordinary_outcome",
]


if __name__ == "__main__":
    sys.exit(main())
