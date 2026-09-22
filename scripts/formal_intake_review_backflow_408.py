#!/usr/bin/env python3
"""Append-only formal-intake backflow into the canonical 408 review ledger.

The command never calls the formal writer. ``ensure`` reuses or creates one
policy-approved review action. For a sealed new morning study-episode Capture
that intentionally has no earlier outcome, it first records or reopens the one
deferred canonical outcome bound to that Capture and episode. ``bind`` only
proves an already committed formal result and appends a non-learner binding event.
"""

from __future__ import annotations

import argparse
import datetime as dt
import fcntl
import hashlib
import json
import os
import re
import sys
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator

import intake_fact_capture_408 as capture_ledger
import conversation_package_408 as conversation_package
import current_question_context_408 as current_question_context
import current_question_evidence_408 as current_question_evidence
import formal_problem_document_408 as formal_problem_document
import review_feedback_loop as review
import sol_curation_decision_408 as sol_decision
from formal_intake_formal_proof_408 import (
    DEFAULT_RUNTIME_ROOT,
    FormalProof,
    FormalProofError,
    verify_formal_proof,
)


ENSURE_RECEIPT_SCHEMA = "review_backflow_ensure_receipt_v1"
FINAL_RECEIPT_SCHEMA = "formal_intake_review_binding_receipt_v1"
ENSURE_SCHEMA = ENSURE_RECEIPT_SCHEMA
FINAL_SCHEMA = FINAL_RECEIPT_SCHEMA
HISTORY_AUDIT_SCHEMA = "formal_intake_review_backflow_history_audit_v1"
BACKFLOW_REL = Path(
    "wiki/study_vaults/408-full/state/review-loop/formal-intake-backflow"
)
INTAKE_LEDGER_REL = Path(
    "wiki/study_vaults/408-full/state/intake-curation/events.jsonl"
)
REVIEW_LEDGER_REL = Path(
    "wiki/study_vaults/408-full/state/review-loop/events.jsonl"
)
TERMINALS = (
    "reuse_existing_action",
    "create_missing_action",
    "maintenance_noop",
    "needs_user",
)
ACTIONABLE_TERMINALS = frozenset(TERMINALS) - {"needs_user"}
SUCCESS_FINAL_STATUSES = {"satisfied", "maintenance_noop"}
SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
NONE_BREAKS = {
    "",
    "none",
    "无",
    "未观察到",
    "未记录",
    "未可靠观察到",
    "not_observed",
}
ZERO_COUNTS = {
    "formal_write_count": 0,
    "capture_write_count": 0,
    "learner_outcome_write_count": 0,
    "luna_call_count": 0,
    "provider_call_count": 0,
    "mcp_call_count": 0,
    "dashboard_call_count": 0,
}
DEFAULT_POLICY_VERSION = review.SCHEDULING_POLICY_VERSION
DIRECT_ASSESSMENT_SCHEMA = "formal_intake_direct_schedule_assessment_v1"
MIGRATED_EVIDENCE_ROOT = Path(
    "~/.codex/kaoyan-408-conversation-packages/legacy-verified"
).expanduser().resolve()


class BackflowError(RuntimeError):
    """Formal backflow failed closed without replaying formal apply."""


def _episode_event_date_matches(event: dict[str, Any], study_date: str) -> bool:
    return review.source_study_date_matches(event, study_date)


def _canonical(value: Any) -> bytes:
    return json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")


def _sha_value(value: Any) -> str:
    return hashlib.sha256(_canonical(value)).hexdigest()


def _sha_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _resolve_repo(repo_root: str | Path) -> Path:
    repo = Path(repo_root).expanduser().resolve()
    if not repo.is_dir():
        raise BackflowError(f"repo does not exist: {repo}")
    return repo


def _safe_id(value: str, label: str) -> str:
    text = str(value or "").strip()
    if not text or len(text) > 256 or not review.SAFE_ID_RE.fullmatch(text):
        raise BackflowError(f"{label} is invalid")
    return text


def _date(value: Any, label: str) -> str:
    try:
        return dt.date.fromisoformat(str(value or "")).isoformat()
    except ValueError as exc:
        raise BackflowError(f"{label} must be YYYY-MM-DD") from exc


def _event_time_for_capture(fact_event: dict[str, Any], study_date: str) -> str:
    raw = str(fact_event.get("created_at") or "")
    try:
        parsed = dt.datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return f"{study_date}T23:59:59+08:00"
    if parsed.tzinfo is None:
        return f"{study_date}T23:59:59+08:00"
    local = parsed.astimezone(dt.timezone(dt.timedelta(hours=8)))
    if local.date().isoformat() != study_date:
        return f"{study_date}T23:59:59+08:00"
    return local.replace(microsecond=0).isoformat()


@contextmanager
def _transaction(repo: Path) -> Iterator[None]:
    root = repo / BACKFLOW_REL
    root.mkdir(parents=True, exist_ok=True, mode=0o700)
    descriptor = os.open(root / ".lock", os.O_RDWR | os.O_CREAT, 0o600)
    try:
        fcntl.flock(descriptor, fcntl.LOCK_EX)
        yield
    finally:
        fcntl.flock(descriptor, fcntl.LOCK_UN)
        os.close(descriptor)


def _read_jsonl(path: Path, label: str) -> list[dict[str, Any]]:
    if not path.is_file():
        return []
    rows: list[dict[str, Any]] = []
    for line_no, raw in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        try:
            value = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise BackflowError(f"{label} is corrupt at line {line_no}") from exc
        if not isinstance(value, dict):
            raise BackflowError(f"{label} line {line_no} is not an object")
        rows.append(value)
    return rows


def _intake_snapshot(
    repo: Path, batch_id: str, capture_id: str
) -> tuple[list[dict[str, Any]], dict[str, Any], dict[str, Any], dict[str, Any]]:
    events = _read_jsonl(repo / INTAKE_LEDGER_REL, "intake ledger")
    try:
        state = capture_ledger.replay(events)
    except capture_ledger.CaptureError as exc:
        raise BackflowError(str(exc)) from exc
    batch = state.get("batches", {}).get(batch_id)
    captured = state.get("captures", {}).get(capture_id)
    if not isinstance(batch, dict) or not isinstance(captured, dict):
        raise BackflowError("batch/capture is not canonical")
    if capture_id not in list(batch.get("capture_ids") or []):
        raise BackflowError("capture is outside the specified batch")
    fact_rows = [
        row
        for row in events
        if row.get("event_type") == "fact_captured"
        and row.get("capture_id") == capture_id
    ]
    if len(fact_rows) != 1:
        raise BackflowError("capture does not have one canonical fact event")
    return events, state, captured, fact_rows[0]


def _capture_payload(captured: dict[str, Any]) -> dict[str, Any]:
    payload = captured.get("capture")
    if not isinstance(payload, dict):
        raise BackflowError("canonical capture payload is missing")
    if payload.get("timezone") != "Asia/Shanghai":
        raise BackflowError("capture timezone must be Asia/Shanghai")
    if not captured.get("formalization_authorized", payload.get("formalization_authorized")):
        raise BackflowError("capture is not authorized for formal curation")
    return payload


def _review_ref_event_id(ref: dict[str, Any]) -> str | None:
    if ref.get("kind") not in {
        "ordinary_review_loop_event",
        "review_loop_event",
        "review_outcome",
    }:
        return None
    locator = str(ref.get("locator") or "")
    if "#event_id=" in locator:
        return locator.rsplit("#event_id=", 1)[1]
    fragment = locator.rsplit("#", 1)[-1]
    return fragment if fragment.startswith("RE-") else None


def _source_review_event(
    repo: Path, payload: dict[str, Any]
) -> tuple[dict[str, Any] | None, str | None]:
    refs = payload.get("stable_evidence_refs") or []
    wanted: list[tuple[str, str]] = []
    managed_bundle_refs: list[str] = []
    conversation_package_refs: list[tuple[str, str]] = []
    for ref in refs:
        if not isinstance(ref, dict):
            continue
        event_id = _review_ref_event_id(ref)
        if event_id:
            wanted.append((event_id, str(ref.get("sha256") or "")))
            continue
        if ref.get("kind") == "current_question_evidence_bundle_v1":
            locator = str(ref.get("locator") or "")
            digest = str(ref.get("sha256") or "")
            prefix = "current-question-evidence://sha256/"
            if (
                not locator.startswith(prefix)
                or locator.removeprefix(prefix) != digest
                or not SHA256_RE.fullmatch(digest)
            ):
                raise BackflowError("current-question evidence locator/hash mismatch")
            managed_bundle_refs.append(digest)
            continue
        if ref.get("kind") == "cs408_conversation_package_v1":
            locator = str(ref.get("locator") or "")
            digest = str(ref.get("sha256") or "")
            prefix = "cs408-conversation-package://"
            if not locator.startswith(prefix) or not SHA256_RE.fullmatch(digest):
                raise BackflowError("conversation package locator/hash mismatch")
            conversation_package_refs.append((locator, digest))
    if sum(bool(group) for group in (wanted, managed_bundle_refs, conversation_package_refs)) > 1:
        raise BackflowError("capture mixes direct review and managed bundle identities")
    if conversation_package_refs:
        if len(set(conversation_package_refs)) != 1:
            raise BackflowError("capture references conflicting conversation packages")
        package_locator, package_sha = conversation_package_refs[0]
        source_id = str((payload.get("source_facts") or {}).get("source_id") or "")
        study_date = str(payload.get("study_date") or "")
        events = review._read_events(repo)
        capture_root = capture_ledger.capture_root(repo)
        if (capture_root / "events.jsonl").is_file():
            capture_state = capture_ledger.replay(capture_ledger._load_events(capture_root))
            supplemented = [row for row in capture_state.get("captures", {}).values()
                            if row.get("capture") == payload and row.get("supplement_history")]
            if len(supplemented) > 1:
                raise BackflowError("supplemented capture identity is ambiguous")
            if supplemented:
                latest = supplemented[0]["supplement_history"][-1]
                package_locator, package_sha = latest["locator"], latest["sha256"]
        matches = [
            row
            for row in events
            if row.get("event_kind") == "outcome"
            and row.get("managed_user_reply_binding_sha256") == package_sha
            and (not study_date or row.get("observed_date") == study_date)
            and (not source_id or row.get("source_id") == source_id)
        ]
        if len(matches) == 1:
            event = matches[0]
            return event, _sha_value(event)
        if len(matches) > 1:
            raise BackflowError(
                "conversation package resolves to multiple canonical outcomes"
            )

        # Compatibility for an immutable morning item whose canonical review
        # outcome retained the old human-readable source label while the
        # current-question package and capture use the verified formal-node
        # machine identity.  Reopen the exact package; never infer this mapping
        # from a label, a formal node, or the review ledger alone.
        try:
            opened = conversation_package.read_package(package_locator)
        except (conversation_package.ConversationPackageError, OSError) as exc:
            raise BackflowError(
                "conversation package source-identity evidence cannot be reopened"
            ) from exc
        manifest = opened.get("manifest")
        package_source = opened.get("source")
        receipt = opened.get("receipt")
        if not all(isinstance(value, dict) for value in (manifest, package_source, receipt)):
            raise BackflowError("conversation package source-identity evidence is incomplete")
        if (
            manifest.get("canonical_sha256") != package_sha
            or receipt.get("canonical_sha256") != package_sha
        ):
            raise BackflowError("conversation package source-identity hash conflict")

        nested_source = package_source.get("source")
        if isinstance(nested_source, dict) and nested_source.get("entry_kind") == "study_episode_capture":
            # v2 packages intentionally have no pre-enhancement outcome. Their
            # outer source_id is an episode key, not the actual exam source ID.
            actual = nested_source.get("source") or {}
            facts = payload.get("source_facts") or {}
            episode_id = nested_source.get("episode_id")
            if (
                episode_id != facts.get("episode_id")
                or nested_source.get("execution_kind") != facts.get("execution_kind")
                or actual.get("source_id") != source_id
                or package_source.get("study_date") != study_date
                or any(manifest.get(k) != package_source.get(k)
                       for k in ("source_id", "study_date", "session_id", "item_id"))
                or (nested_source.get("execution_kind") == "original_review" and (
                    actual.get("session_id") != package_source.get("session_id")
                    or actual.get("item_id") != package_source.get("item_id")))
            ):
                raise BackflowError("study episode package source identity conflict")
            if nested_source.get("execution_kind") != "original_review":
                # Standalone episodes have no prior morning outcome and do not
                # carry a campaign session/item pair inside their source.
                return None, None
            matching = [row for row in events if row.get("event_kind") == "outcome"
                        and (row.get("deferred_morning_binding") or {}).get("episode_id") == episode_id]
            if not matching:
                return None, None
            if len(matching) != 1:
                raise BackflowError("study episode has multiple deferred outcomes")
            event = matching[0]
            binding = event.get("deferred_morning_binding") or {}
            if (
                binding.get("evidence_locator") != package_locator
                or binding.get("evidence_sha256") != package_sha
                or binding.get("display_binding_sha256") != _sha_value(nested_source.get("display_binding"))
                or binding.get("observed_outcome_sha256") != _sha_value(nested_source.get("observed_outcome"))
                or event.get("source_id") != source_id
                or not _episode_event_date_matches(event, study_date)
                or event.get("session_id") != package_source.get("session_id")
                or event.get("item_id") != package_source.get("item_id")
            ):
                raise BackflowError("study episode deferred outcome proof conflict")
            return event, _sha_value(event)
        if isinstance(nested_source, dict) and nested_source.get("source_kind") == "morning_coverage_campaign_v1":
            # Early complete packages kept the campaign identity flat. Recover
            # the missing commitment only from their hash-bound display receipt.
            import morning_campaign_display_408 as campaign_display
            display_sha = str(nested_source.get("display_receipt_sha256") or "")
            if not SHA256_RE.fullmatch(display_sha):
                raise BackflowError("flat campaign package display hash missing")
            try:
                display = campaign_display.read_display_receipt(
                    campaign_display.private_evidence.TURN_LOCATOR_PREFIX + display_sha
                )
            except (ValueError, RuntimeError, OSError) as exc:
                raise BackflowError("flat campaign package display proof unverified") from exc
            expected = {
                "run_id": package_source.get("session_id"),
                "item_id": package_source.get("item_id"),
                "segment_id": nested_source.get("segment_id"),
                "formal_node_id": nested_source.get("formal_node_id"),
                "true_item_id": source_id,
                "surface_sha256": nested_source.get("surface_sha256"),
                "question_asset_sha256": nested_source.get("question_asset_sha256"),
                "grading_capsule_sha256": nested_source.get("grading_capsule_sha256"),
            }
            if (
                package_source.get("source_id") != source_id
                or package_source.get("study_date") != study_date
                or display.get("campaign_date") != study_date
                or nested_source.get("run_id") != expected["run_id"]
                or nested_source.get("true_item_id") != source_id
                or any(manifest.get(k) != package_source.get(k) for k in ("source_id", "study_date", "session_id", "item_id"))
                or any(not value or display.get(key) != value for key, value in expected.items())
                or not SHA256_RE.fullmatch(str(display.get("campaign_item_commitment_sha256") or ""))
            ):
                raise BackflowError("flat campaign package display identity conflict")
            campaign_matches = [row for row in events if
                row.get("event_kind") == "outcome"
                and row.get("evidence_kind") == "campaign_rapid_review"
                and row.get("campaign_run_id") == expected["run_id"]
                and row.get("campaign_segment_id") == expected["segment_id"]
                and row.get("item_id") == expected["item_id"]
                and row.get("source_id") == source_id
                and row.get("true_item_id") == source_id
                and row.get("formal_node_id") == expected["formal_node_id"]
                and row.get("observed_date") == study_date
                and row.get("campaign_item_commitment_sha256") == display["campaign_item_commitment_sha256"]]
            if len(campaign_matches) != 1:
                raise BackflowError("flat campaign package must bind one canonical outcome")
            return campaign_matches[0], _sha_value(campaign_matches[0])
        if isinstance(nested_source, dict) and nested_source.get("schema") == "cs408-campaign-package-source-v1" and nested_source.get("origin") == "morning_coverage_campaign_v1":
            binding = nested_source.get("campaign_binding") or {}
            if (
                package_source.get("source_id") != source_id
                or package_source.get("study_date") != study_date
                or binding.get("true_item_id") != source_id
                or binding.get("run_id") != package_source.get("session_id")
                or binding.get("item_id") != package_source.get("item_id")
                or any(manifest.get(k) != package_source.get(k) for k in ("source_id", "study_date", "session_id", "item_id"))
                or not review.FORMAL_NODE_RE.fullmatch(str(binding.get("formal_node_id") or ""))
            ):
                raise BackflowError("campaign conversation package identity conflict")
            campaign_matches = [row for row in events if
                row.get("event_kind") == "outcome"
                and row.get("evidence_kind") == "campaign_rapid_review"
                and row.get("campaign_run_id") == binding.get("run_id")
                and row.get("item_id") == binding.get("item_id")
                and (
                    row.get("source_id") == source_id
                    or (
                        binding.get("variant_id")
                        and row.get("true_item_id") == source_id
                        and row.get("variant_id") == binding.get("variant_id")
                    )
                    or (
                        # A verified formal-original surface keeps the formal
                        # node as the campaign source label while the package
                        # binds the exact surface identity.  Both sides stay
                        # hash-bound through the campaign binding below.
                        binding.get("surface_kind") == "formal_original"
                        and binding.get("variant_id") is None
                        and row.get("source_id") == binding.get("formal_node_id")
                        and row.get("variant_id") is None
                    )
                )
                and row.get("true_item_id") == source_id
                and row.get("formal_node_id") == binding.get("formal_node_id")
                and row.get("observed_date") == study_date
                and row.get("campaign_item_commitment_sha256") == binding.get("campaign_item_commitment_sha256")]
            if len(campaign_matches) != 1:
                raise BackflowError("campaign conversation package must bind one canonical outcome")
            return campaign_matches[0], _sha_value(campaign_matches[0])
        if (
            package_source.get("session_id") is None
            and isinstance(nested_source, dict)
            and nested_source.get("entry_kind") == "standalone_question_intake"
        ):
            return None, None

        machine_source_id = str(package_source.get("source_id") or "")
        package_study_date = str(package_source.get("study_date") or "")
        session_id = str(package_source.get("session_id") or "")
        item_id = str(package_source.get("item_id") or "")
        if (
            not source_id
            or not review.FORMAL_NODE_RE.fullmatch(source_id)
            or machine_source_id != source_id
            or package_study_date != study_date
            or not session_id
            or not item_id
            or manifest.get("source_id") != machine_source_id
            or manifest.get("study_date") != package_study_date
            or manifest.get("session_id") != session_id
            or manifest.get("item_id") != item_id
        ):
            raise BackflowError(
                "conversation package source-identity binding is incomplete or conflicting"
            )
        provenance = (
            nested_source.get("source_identity_provenance")
            if isinstance(nested_source, dict)
            else None
        )
        expected_provenance_keys = {
            "raw_source_label",
            "machine_source_id",
            "normalization_basis",
            "scheduling_binding_sha256",
            "binding_verification",
        }
        if not isinstance(provenance, dict) or set(provenance) != expected_provenance_keys:
            raise BackflowError(
                "conversation package source-identity provenance is missing or invalid"
            )
        raw_source_label = str(provenance.get("raw_source_label") or "")
        if (
            not raw_source_label
            or current_question_context.is_stable_source_id(raw_source_label)
            or provenance.get("machine_source_id") != machine_source_id
            or provenance.get("normalization_basis")
            != current_question_context.SOURCE_ID_FALLBACK_BASIS
            or not SHA256_RE.fullmatch(
                str(provenance.get("scheduling_binding_sha256") or "")
            )
            or provenance.get("binding_verification")
            not in {
                "verified_embedded_scheduling_binding",
                "reconstructed_legacy_display_session_pack_binding",
            }
        ):
            raise BackflowError(
                "conversation package source-identity provenance is missing or invalid"
            )
        compatibility_matches = [
            row
            for row in events
            if row.get("event_kind") == "outcome"
            and row.get("managed_user_reply_binding_sha256") == package_sha
            and row.get("session_id") == session_id
            and row.get("item_id") == item_id
            and row.get("observed_date") == package_study_date
            and row.get("source_id") == raw_source_label
        ]
        if len(compatibility_matches) == 1:
            event = compatibility_matches[0]
            return event, _sha_value(event)
        if len(compatibility_matches) > 1:
            raise BackflowError(
                "conversation package legacy source identity resolves to multiple canonical outcomes"
            )
        raise BackflowError(
            "conversation package does not resolve to one canonical outcome"
        )
    if managed_bundle_refs:
        if len(set(managed_bundle_refs)) != 1:
            raise BackflowError("capture references conflicting managed evidence bundles")
        bundle_sha = managed_bundle_refs[0]
        source_id = str((payload.get("source_facts") or {}).get("source_id") or "")
        study_date = str(payload.get("study_date") or "")
        matches = [
            row
            for row in review._read_events(repo)
            if row.get("event_kind") == "outcome"
            and row.get("managed_user_reply_binding_sha256") == bundle_sha
            and (not study_date or row.get("observed_date") == study_date)
            and (not source_id or row.get("source_id") == source_id)
        ]
        if len(matches) == 1:
            event = matches[0]
            return event, _sha_value(event)
        if len(matches) > 1:
            raise BackflowError(
                "managed current-question evidence resolves to multiple canonical outcomes"
            )

        # Historical formal_problem_document v4 packages are complete direct
        # learning evidence but intentionally have no synthetic review outcome.
        # Reopen the exact content-addressed bundle and derive only the frozen
        # scheduling input; do not create a learner outcome or formal fact.
        locator = f"current-question-evidence://sha256/{bundle_sha}"
        reopened: dict[str, Any] | None = None
        for private_root in (
            current_question_evidence.DEFAULT_PRIVATE_ROOT,
            MIGRATED_EVIDENCE_ROOT,
        ):
            try:
                _, bundle = formal_problem_document.read_formal_problem_document_bundle(
                    locator, private_root=private_root
                )
            except (
                formal_problem_document.FormalProblemDocumentError,
                current_question_evidence.CurrentQuestionEvidenceError,
                OSError,
            ):
                continue
            reopened = bundle
            break
        if reopened is None:
            raise BackflowError(
                "managed current-question evidence does not resolve to one canonical outcome or formal document"
            )
        assessment = reopened.get("assessment") or {}
        first_break = str(assessment.get("first_break") or "").strip()
        first_result = str(assessment.get("first_result") or "wrong").strip()
        if first_break.lower() in NONE_BREAKS or first_result not in review.FIRST_RESULTS:
            raise BackflowError("formal document lacks direct learning-failure evidence")
        event = {
            "event_kind": "outcome",
            "event_id": None,
            "source": "daily_practice",
            "source_id": str(reopened.get("source_id") or source_id),
            "observed_date": str(reopened.get("study_date") or study_date),
            "event_time": str(
                reopened.get("event_time")
                or reopened.get("provenance", {}).get("created_at")
                or f"{study_date}T00:00:00+08:00"
            ),
            "first_result": first_result,
            "confidence": (
                str(assessment.get("confidence"))
                if assessment.get("confidence") in review.CONFIDENCE_LEVELS
                else "medium"
            ),
            "prompt_level": (
                str(assessment.get("prompt_level"))
                if assessment.get("prompt_level") in review.PROMPT_LEVELS
                else "none"
            ),
            "reasoning_result": str(assessment.get("reasoning_result") or "diverged"),
            "first_break": first_break,
            "first_break_provenance": "visible_evidence",
            "review_unit_id": None,
            "formal_node_id": None,
            "managed_user_reply_binding_sha256": bundle_sha,
            "direct_evidence_kind": "formal_problem_document_v4",
        }
        return event, _sha_value(event)
    if not wanted:
        return None, None
    unique_ids = {item[0] for item in wanted}
    if len(unique_ids) != 1:
        raise BackflowError("capture references conflicting review outcomes")
    event_id = next(iter(unique_ids))
    matches = [
        row for row in review._read_events(repo) if row.get("event_id") == event_id
    ]
    if len(matches) != 1:
        raise BackflowError("capture review event is not canonical")
    event = matches[0]
    if event.get("event_kind") != "outcome":
        raise BackflowError("capture review reference is not an outcome")
    actual_sha = _sha_value(event)
    if any(digest != actual_sha for _, digest in wanted):
        raise BackflowError("capture review event locator/hash mismatch")
    return event, actual_sha


def _has_reasoning_break(event: dict[str, Any]) -> bool:
    if event.get("reasoning_result") in {"partial", "diverged"}:
        return True
    first_break = str(event.get("first_break") or "").strip().lower()
    return first_break not in NONE_BREAKS


def _direct_assessment_binds_source_outcome(
    direct_assessment: dict[str, Any],
    source_event: dict[str, Any],
    source_event_sha: str | None,
) -> bool:
    legacy_grader_correction = (
        direct_assessment.get("supersedes_source_event_id")
        == source_event.get("event_id")
        and direct_assessment.get("source_event_sha256") == source_event_sha
        and direct_assessment.get("supersession_reason")
        == "prepared_grader_key_conflict"
    )
    if legacy_grader_correction:
        return True
    aligned_direct_assessment = bool(
        direct_assessment.get("source_id") == source_event.get("source_id")
        and direct_assessment.get("first_result")
        == source_event.get("first_result")
        and direct_assessment.get("confidence") == source_event.get("confidence")
        and direct_assessment.get("prompt_level")
        == source_event.get("prompt_level")
    )
    if aligned_direct_assessment:
        return True
    return bool(
        direct_assessment.get("_choice_only_reasoning_override")
        and source_event.get("first_result") == "independent_correct"
        and direct_assessment.get("first_result") in {"wrong", "partial"}
        and direct_assessment.get("reasoning_break") is True
        and direct_assessment.get("first_break_provenance") == "visible_evidence"
        and direct_assessment.get("source_id") == source_event.get("source_id")
    )


def _verify_episode_display_fields(display: dict[str, Any], canonical_display: dict[str, Any], authorization: dict[str, Any]) -> None:
    navigation = display.get("navigation_policy")
    if navigation is not None and (
        navigation not in {"publish_successor", "hold_after_capture"}
        or authorization.get("navigation_policy") != navigation
    ):
        raise BackflowError("deferred morning terminal navigation binding drifted")
    compared = {key: value for key, value in display.items()
                if key not in {"display_receipt_sha256", "navigation_policy"}}
    if any(canonical_display.get(key) != value for key, value in compared.items()):
        raise BackflowError("deferred morning display receipt fields drifted")


def _verify_d_original_episode_display(repo, display, source, authorization, formal_id):
    """Verify ordered-D exact originals without relabeling concept supplements."""
    import d_concept_supplement_408 as d_display
    if (
        display.get("schema") != "cs408-D-supplement-display-v1"
        or display.get("execution_kind") != "original_review"
        or display.get("request_kind") != "due_representative_original"
        or source.get("question_type") != "D_original_review"
        or source.get("surface_kind") != "canonical_original"
        or display.get("source_formal_id") != formal_id
        or source.get("source_formal_id") != formal_id
        or display.get("true_item_id") != source.get("source_id")
        or display.get("request_id") != source.get("item_id")
        or display.get("concept_id") != source.get("concept_id")
        or authorization.get("kind") != "d_concept_supplement_session"
        or authorization.get("session_id") != source.get("session_id")
        or authorization.get("request_id") != display.get("request_id")
        or authorization.get("started_at") != display.get("displayed_at")
    ):
        raise BackflowError("deferred D original display/source identity drifted")
    import_sha = str(display.get("native_import_receipt_sha256") or "")
    receipt_sha = str(display.get("receipt_sha256") or "")
    if not SHA256_RE.fullmatch(import_sha) or not SHA256_RE.fullmatch(receipt_sha):
        raise BackflowError("deferred D original receipt hash invalid")
    import_path = Path(repo) / ".private/study-bridge/d-imports" / (import_sha + ".json")
    if import_path.is_symlink() or not import_path.is_file():
        raise BackflowError("deferred D original import receipt missing")
    raw = import_path.read_bytes()
    if hashlib.sha256(raw).hexdigest() != import_sha:
        raise BackflowError("deferred D original import receipt drifted")
    plan = json.loads(raw)
    matches = [row for row in plan.get("learning_items", [])
               if row.get("request_id") == display["request_id"]]
    if (len(matches) != 1 or matches[0].get("execution_kind") != "original_review"
            or matches[0].get("question_id") != formal_id
            or any((matches[0].get("selection") or {}).get(key) != display.get(key)
                   for key in ("true_item_id", "surface_sha256"))):
        raise BackflowError("deferred D original is not an exact original in its import")
    canonical = []
    for path in d_display.receipt_root(repo).glob("*.json"):
        value = json.loads(path.read_bytes())
        if value.get("receipt_sha256") == receipt_sha:
            canonical.append(d_display.read_receipt(path))
    if len(canonical) != 1 or canonical[0] != display:
        raise BackflowError("deferred D original canonical display receipt drifted")


def _reconcile_deferred_outcome(
    repo: Path,
    reopened: dict[str, Any],
    sealed_decision: dict[str, Any],
    assessment: dict[str, Any],
    observed: dict[str, Any],
) -> dict[str, str] | None:
    """Verify an explicit owner's correction without modifying provisional facts.

    The sealed decision binds this correction and the per-point semantics. Raw
    Capture/display/runtime hashes remain checked by the caller. An aggregate
    label alone (including a later prompted success) is never evidence of an
    independent first response.
    """
    correction = assessment.get("deferred_outcome_reconciliation")
    if correction is None:
        return None
    fields = {
        "schema", "observed_outcome_sha256", "first_response_ref",
        "prior_assistant_turns", "choice_result", "reasoning_result", "reason",
    }
    if (
        not isinstance(correction, dict)
        or frozenset(correction) not in {
            frozenset(fields), frozenset(fields | {"earlier_nonattempt_turns"}),
        }
        or correction.get("schema") != "cs408-deferred-outcome-reconciliation-v1"
        or correction.get("observed_outcome_sha256") != _sha_value(observed)
        or not isinstance(correction.get("reason"), str)
        or not correction["reason"].strip()
        or correction.get("choice_result") not in review.CHOICE_RESULTS
        or correction.get("reasoning_result") not in review.REASONING_RESULTS
    ):
        raise BackflowError("deferred reconciliation fields or provisional hash invalid")
    decision = sealed_decision.get("decision") or {}
    canonical = decision.get("canonical_package")
    sealed_assessment = (
        canonical.get("review_backflow_assessment")
        if isinstance(canonical, dict) else decision.get("review_backflow_assessment")
    )
    if (
        not isinstance(sealed_assessment, dict)
        or sealed_assessment.get("deferred_outcome_reconciliation") != correction
        or not SHA256_RE.fullmatch(str(assessment.get("decision_sha256") or ""))
    ):
        raise BackflowError("deferred reconciliation is not bound to the sealed decision")
    for key in ("first_result", "prompt_level", "reasoning_break", "source_id"):
        expected = sealed_assessment.get(key)
        if key == "first_result" and expected == "blank":
            expected = "uncertain"
        if assessment.get(key) != expected:
            raise BackflowError("deferred reconciliation assessment drifted from decision")
    conversation = reopened["package"].get("conversation") or {}
    turns = conversation.get("turns") or []
    if (conversation.get("complete_order_preserved") is not True
            or conversation.get("truncated") is not False):
        raise BackflowError("deferred reconciliation requires complete ordered turns")

    def verify_ref(ref: Any, *, prior: bool = False, nonattempt: bool = False) -> int:
        required = {"turn", "sha256"} | ({"content_role"} if prior else set())
        if nonattempt:
            required.add("reason")
        if (not isinstance(ref, dict) or set(ref) != required
                or type(ref.get("turn")) is not int
                or not 1 <= ref["turn"] <= len(turns)
                or _sha_value(turns[ref["turn"] - 1]) != ref.get("sha256")):
            raise BackflowError("deferred reconciliation original turn hash invalid")
        return ref["turn"]

    first = verify_ref(correction["first_response_ref"])
    learner_turns = [n for n, turn in enumerate(turns, 1)
                     if turn.get("role") in {"user", "learner"}]
    if first not in learner_turns:
        raise BackflowError("deferred reconciliation must anchor the first learner turn")
    nonattempts = correction.get("earlier_nonattempt_turns", [])
    if not isinstance(nonattempts, list):
        raise BackflowError("deferred reconciliation earlier nonattempt coverage invalid")
    nonattempt_numbers = []
    for ref in nonattempts:
        nonattempt_numbers.append(verify_ref(ref, nonattempt=True))
        if not isinstance(ref["reason"], str) or not ref["reason"].strip():
            raise BackflowError("deferred reconciliation earlier nonattempt reason required")
    if nonattempt_numbers != [n for n in learner_turns if n < first]:
        raise BackflowError(
            "deferred reconciliation first learner exclusions are incomplete"
        )
    prior = correction["prior_assistant_turns"]
    if not isinstance(prior, list):
        raise BackflowError("deferred reconciliation prior assistant coverage invalid")
    prior_numbers = []
    for ref in prior:
        prior_numbers.append(verify_ref(ref, prior=True))
        if ref["content_role"] not in {"question_only", "hint", "explanation"}:
            raise BackflowError("deferred reconciliation prior assistant role invalid")
    expected_prior = [n for n, turn in enumerate(turns[:first - 1], 1)
                      if turn.get("role") in {"assistant", "tutor"}]
    if prior_numbers != expected_prior:
        raise BackflowError("deferred reconciliation prior assistant coverage incomplete")
    try:
        from seal_knowledge_observations_408 import validate_formal_review
        points = validate_formal_review(
            repo, decision.get("knowledge_observations"), reopened,
            canonical=canonical,
        )["points"]
    except (ValueError, OSError, KeyError, TypeError) as exc:
        raise BackflowError(f"deferred reconciliation point evidence invalid: {exc}") from exc
    records = [record for point in points for record in point.get("records", [])]
    first_records = [r for r in records if r.get("event_kind") == "outcome"
                     and r.get("source_order") == first
                     and any(ref["turn"] == first for ref in r["evidence_refs"])]
    if not first_records:
        raise BackflowError("deferred reconciliation lacks first learner outcome evidence")
    prompt = str(assessment.get("prompt_level") or "")
    first_result = assessment.get("first_result")
    choice = correction["choice_result"]
    reasoning = correction["reasoning_result"]
    if first_result == "independent_correct":
        if (
            prompt != "none" or choice != "correct" or reasoning != "sound"
            or assessment.get("reasoning_break") is not False
            or any(ref["content_role"] != "question_only" for ref in prior)
            or not any(r.get("outcome") == "independent_correct"
                       and r.get("prompt_dependency") == "independent"
                       and r.get("evidence_role") == "observed_learner_evidence"
                       for r in first_records)
        ):
            raise BackflowError("deferred reconciliation lacks independent first-response proof")
    elif first_result == "wrong":
        if choice != "incorrect" and reasoning not in {"partial", "diverged"}:
            raise BackflowError("deferred reconciliation wrong result has no failure component")
    elif first_result == "partial":
        if reasoning not in {"partial", "diverged"}:
            raise BackflowError("deferred reconciliation partial result has no reasoning gap")
    elif first_result == "uncertain":
        if choice not in {"uncertain", "not_applicable"} and reasoning not in {"not_observed", "partial"}:
            raise BackflowError("deferred reconciliation uncertain result has no uncertainty component")
    else:
        raise BackflowError("deferred reconciliation first result invalid")
    if prompt not in review.PROMPT_LEVELS:
        raise BackflowError("deferred reconciliation prompt level invalid")
    if prompt != "none" and not any(
        r.get("event_kind") in {"hint", "explanation"}
        and r.get("evidence_role") == "assistant_explanation"
        for r in records
    ):
        raise BackflowError("deferred reconciliation lacks original prompt evidence")
    if reasoning in {"partial", "diverged"} and assessment.get("reasoning_break") is not True:
        raise BackflowError("deferred reconciliation reasoning break was suppressed")
    return {"choice_result": choice, "reasoning_result": reasoning, "prompt_level": prompt}


def _ensure_deferred_morning_outcome(
    repo: Path,
    *,
    batch_id: str,
    capture_id: str,
    sealed_decision: dict[str, Any],
    direct_assessment: dict[str, Any],
) -> dict[str, Any] | None:
    """Write or reopen one delayed morning outcome from a sealed episode.

    Historical Captures that already bind an outcome keep that event.  New
    study-episode Captures deliberately contain no learning-ledger write; their
    first and only outcome is appended here, during authorized enhancement.
    """

    if direct_assessment.get("source") != "morning_review":
        return None
    _, _, captured, _ = _intake_snapshot(repo, batch_id, capture_id)
    payload = _capture_payload(captured)
    bound_event, _ = _source_review_event(repo, payload)
    if bound_event is not None:
        return None
    source_facts = payload.get("source_facts") or {}
    episode_id = str(source_facts.get("episode_id") or "")
    execution_kind = str(source_facts.get("execution_kind") or "")
    if (
        not review.SAFE_ID_RE.fullmatch(episode_id)
        or execution_kind != "original_review"
    ):
        raise BackflowError(
            "deferred morning outcome requires one original_review episode Capture"
        )
    if (
        sealed_decision.get("capture_id") != capture_id
        or sealed_decision.get("capture_payload_sha256")
        != captured.get("payload_sha256")
    ):
        raise BackflowError("deferred morning decision/Capture binding drifted")
    try:
        reopened = sol_decision.reopen_direct_evidence(
            str(sealed_decision.get("evidence_locator") or ""),
            str(sealed_decision.get("reopened_evidence_sha256") or ""),
        )
    except (sol_decision.SolCurationDecisionError, OSError, ValueError) as exc:
        raise BackflowError(f"deferred morning evidence could not reopen: {exc}") from exc
    if reopened.get("kind") != "cs408_conversation_package_v1":
        raise BackflowError("deferred morning outcome requires a complete conversation package")
    package = reopened["package"]
    episode = (package.get("source") or {}).get("source") or {}
    source = episode.get("source") or {}
    display = episode.get("display_binding")
    observed = episode.get("observed_outcome")
    d_original = (isinstance(display, dict)
                  and display.get("schema") == "cs408-D-supplement-display-v1")
    if (
        episode.get("entry_kind") != "study_episode_capture"
        or episode.get("episode_id") != episode_id
        or episode.get("execution_kind") != "original_review"
        or not isinstance(source, dict)
        or (source.get("origin_lane") != "campaign_original" and not d_original)
        or not isinstance(display, dict)
        or not display
        or not isinstance(observed, dict)
        or not observed
    ):
        raise BackflowError("deferred morning episode/display/outcome binding invalid")
    session_id = str(source.get("session_id") or "")
    item_id = str(source.get("item_id") or "")
    source_id = str(source.get("source_id") or "")
    display_sha = str(display.get("display_receipt_sha256") or "")
    display_locator = str(source.get("locator") or "")
    if (
        not review.SAFE_ID_RE.fullmatch(session_id)
        or not review.SAFE_ID_RE.fullmatch(item_id)
        or direct_assessment.get("source_id") != source_id
        or (not d_original and (
            display.get("run_id") != session_id
            or display.get("item_id") != item_id
            or not SHA256_RE.fullmatch(display_sha)
            or display_locator != "current-question-turn://sha256/" + display_sha))
    ):
        raise BackflowError("deferred morning source/display identity drifted")
    if d_original:
        if episode_id != session_id + ":" + item_id or display.get("review_date") != payload.get("study_date"):
            raise BackflowError("deferred D original episode/date drifted")
        _verify_d_original_episode_display(repo, display, source, episode.get("authorization") or {},
                                           direct_assessment.get("formal_node_id"))
    else:
        try:
            import morning_campaign_display_408 as campaign_display
            canonical_display = campaign_display.read_display_receipt(display_locator)
        except (ImportError, OSError, ValueError, RuntimeError, KeyError) as exc:
            raise BackflowError(f"deferred morning display receipt could not reopen: {exc}") from exc
        _verify_episode_display_fields(display, canonical_display, episode.get("authorization") or {})
    claimed_event_time = direct_assessment.get("event_time")
    user_times = [
        str(row.get("observed_at"))
        for row in (package.get("conversation") or {}).get("turns") or []
        if isinstance(row, dict)
        and row.get("role") == "user"
        and row.get("observed_at")
    ]
    time_quality = "user_turn_observed_at"
    time_source_event_id = None
    time_source_event_sha256 = None
    if user_times:
        event_time = str(claimed_event_time or user_times[-1])
        if event_time not in user_times:
            raise BackflowError(
                "deferred morning event_time is not an exact learner turn time"
            )
    else:
        if d_original:
            raise BackflowError("deferred D original requires an actual learner timestamp")
        if claimed_event_time not in (None, ""):
            raise BackflowError(
                "deferred morning web event_time cannot replace a missing learner timestamp"
            )
        try:
            import morning_campaign_runtime_408 as campaign_runtime

            runtime_root = repo / ".private/morning-campaign-runtime"
            audited = campaign_runtime.audit_run(
                runtime_root, session_id, repo_root=repo
            )
            run_dir = campaign_runtime.runtime_run_directory(
                runtime_root, session_id
            )
            runtime_events = campaign_runtime._read_events(run_dir)
        except (ImportError, OSError, ValueError, RuntimeError, KeyError) as exc:
            raise BackflowError(
                f"deferred morning runtime observation could not reopen: {exc}"
            ) from exc
        if audited.get("status") != "PASS":
            raise BackflowError("deferred morning runtime audit did not pass")
        observation_events = [
            row for row in runtime_events
            if row.get("event_type") == "campaign_response_observed"
            and row.get("run_id") == session_id
            and (row.get("payload") or {}).get("item_id") == item_id
            and (row.get("payload") or {}).get("episode_id") == episode_id
        ]
        if len(observation_events) != 1:
            raise BackflowError(
                "deferred morning runtime observation is missing or ambiguous"
            )
        runtime_event = observation_events[0]
        runtime_payload = runtime_event.get("payload") or {}
        expected_private_first = {
            "correct": "correct",
            "incorrect": "incorrect",
            "partial": "blank",
            "uncertain": "uncertain",
            "not_observed": "not_observed",
        }.get(str(runtime_payload.get("first_result") or ""))
        if (
            runtime_payload.get("first_response")
            != observed.get("first_response")
            or expected_private_first != observed.get("first_result")
            or runtime_payload.get("true_item_id") != source_id
            or runtime_payload.get("campaign_item_commitment_sha256")
            != display.get("campaign_item_commitment_sha256")
            or runtime_payload.get("surface_kind") != display.get("surface_kind")
            or runtime_payload.get("formal_node_id") != source.get("formal_node_id")
        ):
            raise BackflowError(
                "deferred morning runtime observation does not bind the sealed episode"
            )
        event_time = str(runtime_event.get("event_time") or "")
        time_quality = "runtime_observation_recorded_at"
        time_source_event_id = str(runtime_event.get("event_id") or "")
        time_source_event_sha256 = str(
            runtime_event.get("event_sha256") or ""
        )
        if (
            not review.SAFE_ID_RE.fullmatch(time_source_event_id)
            or not SHA256_RE.fullmatch(time_source_event_sha256)
        ):
            raise BackflowError(
                "deferred morning runtime observation identity is invalid"
            )
    try:
        actual_day = review._parse_datetime(event_time).astimezone(
            review.LOCAL_TZ
        ).date().isoformat()
    except (review.ReviewLoopError, ValueError, TypeError) as exc:
        raise BackflowError("deferred morning event_time is invalid") from exc
    if actual_day != payload.get("study_date"):
        # The fallback is the recorder's timestamp, not an invented learner
        # timestamp. A response saved the next day may still explicitly bind
        # the original Capture day. Keep both dates: the review loop already
        # stores its timestamp-derived day separately from session_review_date.
        if (time_quality != "runtime_observation_recorded_at"
                or runtime_payload.get("observed_date") != payload.get("study_date")
                or actual_day < str(payload.get("study_date") or "")):
            raise BackflowError("deferred morning observed time crosses the Capture study date")
    direct_assessment["event_time"] = event_time
    prompt = str(observed.get("prompt_dependency") or "")
    reasoning = str(observed.get("reasoning_result") or "")
    raw_first = str(observed.get("first_result") or "")
    if (
        prompt not in review.PROMPT_LEVELS
        or reasoning not in review.REASONING_RESULTS
        or (raw_first not in {"correct", "incorrect", "blank", "uncertain"}
            and not (raw_first == "not_observed"
                     and direct_assessment.get("deferred_outcome_reconciliation") is not None))
    ):
        raise BackflowError("deferred morning private outcome is incomplete or drifted")
    reconciled = _reconcile_deferred_outcome(
        repo, reopened, sealed_decision, direct_assessment, observed,
    )
    if reconciled is None and direct_assessment.get("prompt_level") != prompt:
        raise BackflowError("deferred morning private outcome is incomplete or drifted")
    assessed_first = str(direct_assessment.get("first_result") or "")
    normalized_first = "uncertain" if assessed_first == "blank" else assessed_first
    if raw_first == "blank" and normalized_first != "uncertain":
        raise BackflowError("deferred morning blank response was overstated")
    if reconciled is None and normalized_first == "independent_correct" and not (
        raw_first == "correct"
        and prompt == "none"
        and reasoning == "sound"
        and observed.get("terminal_state") in {"correct", "screened_correct"}
    ):
        raise BackflowError("deferred morning assessment overstates independent correctness")
    if reconciled is None and reasoning in {"partial", "diverged"} and direct_assessment.get("reasoning_break") is not True:
        raise BackflowError("deferred morning reasoning break was suppressed")
    if reconciled is not None:
        prompt = reconciled["prompt_level"]
        reasoning = reconciled["reasoning_result"]
    identity = str(
        direct_assessment.get("review_unit_id")
        or direct_assessment.get("formal_node_id")
        or source_id
    )
    if direct_assessment.get("source_identity") != identity:
        raise BackflowError("deferred morning source_identity is not canonical")
    formal_id = direct_assessment.get("formal_node_id")
    decision_formal_id = (sealed_decision.get("decision") or {}).get("formal_id")
    if formal_id is not None and formal_id != decision_formal_id:
        raise BackflowError("deferred morning formal identity conflicts with decision")
    decision_sha = str(direct_assessment.get("decision_sha256") or "")
    binding = {
        "schema": review.DEFERRED_MORNING_BINDING_SCHEMA,
        "capture_id": capture_id,
        "capture_payload_sha256": str(captured.get("payload_sha256") or ""),
        "episode_id": episode_id,
        "execution_kind": "original_review",
        "evidence_locator": str(sealed_decision.get("evidence_locator") or ""),
        "evidence_sha256": str(sealed_decision.get("reopened_evidence_sha256") or ""),
        "display_binding_sha256": _sha_value(display),
        "observed_outcome_sha256": _sha_value(observed),
        "decision_sha256": decision_sha,
        "time_quality": time_quality,
        "time_source_event_id": time_source_event_id,
        "time_source_event_sha256": time_source_event_sha256,
    }
    key = "formal-enhancement-morning:" + hashlib.sha256(
        review.canonical_json([episode_id, capture_id]).encode("utf-8")
    ).hexdigest()
    existing = [
        row for row in review._read_events(repo)
        if row.get("event_kind") == "outcome"
        and row.get("source") == "morning_review"
        and row.get("session_id") == session_id
        and row.get("item_id") == item_id
    ]
    if existing:
        if (
            len(existing) != 1
            or existing[0].get("event_time") != event_time
            or existing[0].get("observed_date") != actual_day
            or not _direct_assessment_binds_source_outcome(
                direct_assessment, existing[0], _sha_value(existing[0])
            )
        ):
            raise BackflowError("existing morning outcome conflicts with enhancement evidence")
        old_binding = existing[0].get("deferred_morning_binding")
        if old_binding is not None and old_binding != binding:
            raise BackflowError("existing deferred morning outcome binding drifted")
        return {
            "event": existing[0],
            "status": (
                "created_deferred_outcome"
                if old_binding is not None
                else "reused_existing_outcome"
            ),
            "binding": old_binding,
            "learner_outcome_write_count": 1 if old_binding is not None else 0,
        }
    point_codes = sorted(
        {
            str(row.get("knowledge_point"))
            for row in (
                (sealed_decision.get("decision") or {})
                .get("knowledge_observations", {})
                .get("points", [])
            )
            if isinstance(row, dict) and row.get("status") == "observed"
        }
    )
    knowledge_point = point_codes[0] if point_codes else ""
    choice_result = reconciled["choice_result"] if reconciled is not None else {
        "correct": "correct",
        "incorrect": "incorrect",
        "blank": "not_applicable",
        "uncertain": "uncertain",
    }[raw_first]
    record = getattr(review.record_outcome, "__wrapped__", review.record_outcome)
    try:
        result = record(
            repo,
            source="morning_review",
            session_id=session_id,
            item_id=item_id,
            source_id=source_id,
            mechanism_key=knowledge_point or identity,
            knowledge_point=knowledge_point,
            first_result=normalized_first,
            choice_result=choice_result,
            reasoning_result=reasoning,
            confidence=str(direct_assessment.get("confidence") or ""),
            prompt_level=prompt,
            first_break=str(direct_assessment.get("first_break") or ""),
            event_time=event_time,
            observed_date=str(payload.get("study_date") or ""),
            session_review_date=str(payload.get("study_date") or ""),
            idempotency_key=key,
            correction_result="not_attempted",
            latency_state="unmeasured",
            cross_day=True,
            question_valid=True,
            formal_node_id=formal_id,
            review_unit_id=direct_assessment.get("review_unit_id"),
            first_break_provenance=str(
                direct_assessment.get("first_break_provenance") or ""
            ),
            deferred_morning_binding=binding,
            _from_session_sync=True,
        )
    except review.ReviewLoopError as exc:
        raise BackflowError(f"deferred morning outcome rejected: {exc}") from exc
    event = result.get("event") if isinstance(result, dict) else None
    if not isinstance(event, dict) or event.get("deferred_morning_binding") != binding:
        raise BackflowError("deferred morning outcome readback failed")
    return {
        "event": event,
        "status": "created_deferred_outcome",
        "binding": binding,
        "learner_outcome_write_count": 1,
    }


def _freeze_schedule_input(
    repo: Path,
    *,
    batch_id: str,
    capture_id: str,
    policy_version: str,
    direct_assessment: dict[str, Any] | None = None,
    source_event_override: dict[str, Any] | None = None,
) -> dict[str, Any]:
    _, _, captured, fact_event = _intake_snapshot(repo, batch_id, capture_id)
    payload = _capture_payload(captured)
    study_date = _date(payload.get("study_date"), "capture study_date")
    if _date(captured.get("study_date", study_date), "state study_date") != study_date:
        raise BackflowError("capture study date drifted during replay")
    source_facts = payload.get("source_facts") or {}
    source_id = str(source_facts.get("source_id") or capture_id).strip()
    if not source_id:
        source_id = capture_id
    source_event, source_event_sha = _source_review_event(repo, payload)
    if source_event_override is not None:
        if (
            source_event_override.get("event_kind") != "outcome"
            or source_event_override.get("source") != "morning_review"
            or (
                source_event is not None
                and source_event.get("event_id")
                != source_event_override.get("event_id")
            )
        ):
            raise BackflowError("deferred morning source outcome override is invalid")
        source_event = source_event_override
        source_event_sha = _sha_value(source_event_override)
    user_facts = payload.get("user_facts") or {}
    direct_decision_sha256: str | None = None
    if source_event is not None and direct_assessment is None:
        source = str(source_event.get("source") or "")
        first_result = str(source_event.get("first_result") or "")
        confidence = str(source_event.get("confidence") or "")
        prompt_level = str(source_event.get("prompt_level") or "")
        first_break = str(source_event.get("first_break") or "none")
        provenance = review.normalize_first_break_provenance(
            source_event.get("first_break_provenance"), first_break
        )
        reasoning_break = _has_reasoning_break(source_event)
        raw_source_identity = str(
            source_event.get("review_unit_id")
            or source_event.get("formal_node_id")
            or source_event.get("source_id")
            or source_id
        )
        source_identity = (
            raw_source_identity
            if review.SAFE_ID_RE.fullmatch(raw_source_identity)
            else None
        )
        review_unit_id = source_event.get("review_unit_id")
        formal_node_id = source_event.get("formal_node_id")
    elif direct_assessment is not None:
        if source_event is not None and not _direct_assessment_binds_source_outcome(
            direct_assessment, source_event, source_event_sha
        ):
            raise BackflowError(
                "direct grader correction does not bind the canonical source outcome"
            )
        source = str(direct_assessment["source"])
        source_id = str(direct_assessment["source_id"])
        if source_event is not None and direct_assessment.get(
            "_choice_only_reasoning_override"
        ):
            source_identity = str(
                source_event.get("review_unit_id")
                or source_event.get("formal_node_id")
                or source_event.get("source_id")
                or source_id
            )
        else:
            source_identity = str(direct_assessment["source_identity"])
        review_unit_id = direct_assessment.get("review_unit_id")
        formal_node_id = direct_assessment.get("formal_node_id")
        first_result = str(direct_assessment["first_result"])
        confidence = str(direct_assessment["confidence"])
        prompt_level = str(direct_assessment["prompt_level"])
        reasoning_break = bool(direct_assessment["reasoning_break"])
        first_break = str(direct_assessment["first_break"])
        provenance = str(direct_assessment["first_break_provenance"])
        direct_decision_sha256 = str(direct_assessment["decision_sha256"])
    else:
        if "capture_user_error_entry_summary_deferred" in payload.get("missing_fields", []):
            raise BackflowError(
                "capture error summary deferred; require package-grounded direct assessment"
            )
        first_break = str(user_facts.get("user_error_entry") or "").strip()
        provenance = str(user_facts.get("user_error_provenance") or "not_observed")
        if provenance not in {"user_report", "visible_evidence"} or first_break.lower() in NONE_BREAKS:
            raise BackflowError("capture lacks direct learning-failure evidence")
        source = "daily_practice"
        first_result = "wrong"
        confidence = "medium"
        prompt_level = "none"
        reasoning_break = True
        source_identity = capture_id
        review_unit_id = None
        formal_node_id = None
    policy = _safe_id(policy_version, "policy_version")
    frozen = {
        "schema": "formal_intake_schedule_input_v1",
        "batch_id": batch_id,
        "capture_id": capture_id,
        "capture_payload_sha256": str(captured.get("payload_sha256") or ""),
        "fact_event_id": fact_event.get("event_id"),
        "fact_event_sha256": _sha_value(fact_event),
        "study_date": study_date,
        "timezone": "Asia/Shanghai",
        "source": source,
        "source_id": source_id,
        "source_review_event_id": source_event.get("event_id") if source_event else None,
        "source_review_event_sha256": source_event_sha,
        "source_review_event_identity": source_identity,
        "review_unit_id": review_unit_id,
        "formal_node_id": formal_node_id,
        "first_result": first_result,
        "confidence": confidence,
        "prompt_level": prompt_level,
        "reasoning_break": reasoning_break,
        "first_break": first_break,
        "first_break_provenance": provenance,
        "policy_version": policy,
        "event_time": (
            str(direct_assessment["event_time"])
            if direct_assessment is not None and direct_assessment.get("event_time") is not None
            else str(source_event["event_time"])
            if source_event is not None and source_event.get("event_time")
            else _event_time_for_capture(fact_event, study_date)
        ),
        "direct_decision_sha256": direct_decision_sha256,
    }
    if direct_assessment is not None:
        frozen["schedule_contract_version"] = "formal_backflow_reasoning_v6"
        if (
            source_event is not None
            and direct_assessment.get("supersedes_source_event_id") == source_event.get("event_id")
            and direct_assessment.get("source_event_sha256") == source_event_sha
            and direct_assessment.get("supersession_reason") == "prepared_grader_key_conflict"
        ):
            # Keep the old event's identity as its evidence proof. The corrected
            # target is separately represented by source_id/formal_node_id; a
            # wrong old node binding must not become the new question's target.
            frozen["source_review_event_identity"] = str(
                source_event.get("review_unit_id") or source_event.get("formal_node_id")
                or source_event.get("source_id")
            )
    if source_event is not None and source_event.get(
        "deferred_morning_binding"
    ):
        frozen["deferred_morning_outcome_binding_sha256"] = _sha_value(
            source_event["deferred_morning_binding"]
        )
    if (
        direct_assessment is not None
        and direct_assessment.get("_choice_only_reasoning_override")
        and source_event is not None
        and source_event.get("first_result") == "independent_correct"
        and first_result in {"wrong", "partial"}
    ):
        frozen["source_outcome_override"] = (
            "choice_only_grader_reasoning_conflict"
        )
    if (
        direct_assessment is not None
        and direct_assessment.get("_blank_normalized_to_uncertain")
    ):
        frozen["original_first_result"] = "blank"
    frozen["input_sha256"] = _sha_value(frozen)
    return frozen


def _receipt_dir(repo: Path, schema: str, *, create: bool) -> Path:
    name = "ensure-receipts" if schema == ENSURE_RECEIPT_SCHEMA else "final-receipts"
    directory = repo / BACKFLOW_REL / name
    if create:
        directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    return directory


def _write_receipt(repo: Path, payload: dict[str, Any]) -> dict[str, Any]:
    schema = str(payload.get("schema") or "")
    if schema not in {ENSURE_RECEIPT_SCHEMA, FINAL_RECEIPT_SCHEMA}:
        raise BackflowError("unsupported backflow receipt schema")
    content = dict(payload)
    for field, value in ZERO_COUNTS.items():
        content.setdefault(field, value)
    digest = _sha_value(content)
    path = _receipt_dir(repo, schema, create=True) / f"{digest}.json"
    if path.exists():
        try:
            current = json.loads(path.read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            raise BackflowError("existing backflow receipt is corrupt") from exc
        if current != content:
            raise BackflowError("backflow receipt content-address collision")
    else:
        review.atomic_write_json(path, content)
    returned = dict(content)
    returned["receipt_sha256"] = digest
    returned["receipt_path"] = path.relative_to(repo).as_posix()
    return returned


def _receipt_candidates(repo: Path, key: str, schema: str) -> list[dict[str, Any]]:
    directory = _receipt_dir(repo, schema, create=False)
    if not directory.is_dir():
        return []
    result = []
    for path in sorted(directory.glob("*.json")):
        if path.is_symlink() or not path.is_file():
            continue
        try:
            value = json.loads(path.read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            raise BackflowError("backflow receipt store is corrupt") from exc
        if (
            not isinstance(value, dict)
            or value.get("schema") != schema
            or _sha_value(value) != path.stem
        ):
            raise BackflowError("backflow receipt store has a content/hash mismatch")
        if value.get("idempotency_key") == key:
            value = dict(value)
            value["receipt_sha256"] = path.stem
            value["receipt_path"] = path.relative_to(repo).as_posix()
            result.append(value)
    return result


def _control_event(repo: Path, event_id: str) -> dict[str, Any]:
    events = review._read_events(repo)
    matches = [row for row in events if row.get("event_id") == event_id]
    if len(matches) != 1:
        raise BackflowError("receipt-bound review control event is missing")
    return matches[0]


def _historical_result_event(
    repo: Path, event_id: str | None
) -> dict[str, Any] | None:
    if event_id is None:
        return None
    event_value = _safe_id(event_id, "historical_result_event_id")
    matches = [
        row
        for row in _read_jsonl(repo / INTAKE_LEDGER_REL, "intake ledger")
        if row.get("event_id") == event_value
    ]
    if len(matches) != 1 or matches[0].get("event_type") != "curation_item_result":
        raise BackflowError("historical result event is not canonical")
    return matches[0]


def _normalize_no_break_assessment(value: dict[str, Any]) -> dict[str, Any]:
    normalized = dict(value)
    # This generic commentary is not the mechanism break and exposes no answer;
    # keep the actual break, while the complete wording stays in sealed evidence.
    if isinstance(normalized.get("first_break"), str):
        normalized["first_break"] = normalized["first_break"].replace(
            "；正确选项不足以证明推理完成。", "。"
        )
    if value.get("first_result") == "independent_correct" and value.get("reasoning_break") is False:
        normalized["first_break"] = "none"
        normalized["first_break_provenance"] = "not_observed"
    return normalized


def ensure_review_backflow(
    repo_root: str | Path,
    *,
    batch_id: str,
    capture_id: str,
    policy_version: str,
    idempotency_key: str | None = None,
    decision_locator: str | None = None,
    decision_sha256: str | None = None,
) -> dict[str, Any]:
    """Reuse/create one policy action and close the deferred-morning gap once."""

    repo = _resolve_repo(repo_root)
    batch = _safe_id(batch_id, "batch_id")
    capture = _safe_id(capture_id, "capture_id")
    direct_assessment: dict[str, Any] | None = None
    sealed_direct: dict[str, Any] | None = None
    if bool(decision_locator) != bool(decision_sha256):
        raise BackflowError("direct decision locator and SHA must be supplied together")
    if decision_locator and decision_sha256:
        sealed = sol_decision.read_sealed_decision(decision_locator, decision_sha256)
        sealed_direct = sealed
        if sealed.get("batch_id") != batch or sealed.get("capture_id") != capture:
            raise BackflowError("direct schedule decision identity mismatch")
        normalized_decision = sealed.get("decision") or {}
        canonical = normalized_decision.get("canonical_package")
        assessment = (
            canonical.get("review_backflow_assessment")
            if isinstance(canonical, dict)
            else normalized_decision.get("review_backflow_assessment")
        )
        required = {
            "schema", "source", "source_id", "source_identity",
            "review_unit_id", "formal_node_id", "first_result", "confidence",
            "prompt_level", "reasoning_break", "first_break",
            "first_break_provenance", "event_time",
        }
        correction_fields = {
            "supersedes_source_event_id", "source_event_sha256",
            "supersession_reason",
        }
        reconciliation_fields = {"deferred_outcome_reconciliation"}
        if not isinstance(assessment, dict) or frozenset(assessment) not in {
            frozenset(required), frozenset(required | correction_fields),
            frozenset(required | reconciliation_fields),
            frozenset(required | correction_fields | reconciliation_fields),
        }:
            raise BackflowError("sealed decision lacks one direct schedule assessment")
        assessment = _normalize_no_break_assessment(assessment)
        assessment_first_break = str(
            assessment.get("first_break") or ""
        ).strip().lower()
        independent_no_break = (
            assessment.get("first_result") == "independent_correct"
            and assessment.get("reasoning_break") is False
            and assessment_first_break in NONE_BREAKS
        )
        if (
            assessment.get("schema") != DIRECT_ASSESSMENT_SCHEMA
            or assessment.get("source") not in review.SOURCES
            or assessment.get("first_result")
            not in (review.FIRST_RESULTS | {"blank"})
            or assessment.get("confidence") not in review.CONFIDENCE_LEVELS
            or assessment.get("prompt_level") not in review.PROMPT_LEVELS
            or assessment.get("first_break_provenance")
            not in review.FIRST_BREAK_PROVENANCES
            or not isinstance(assessment.get("reasoning_break"), bool)
            or (assessment_first_break in NONE_BREAKS and not independent_no_break)
        ):
            raise BackflowError("sealed direct schedule assessment is invalid")
        for label in ("source_id", "source_identity"):
            if not review.SAFE_ID_RE.fullmatch(str(assessment.get(label) or "")):
                raise BackflowError(f"sealed direct schedule {label} is invalid")
        if correction_fields <= set(assessment) and (
            not review.SAFE_ID_RE.fullmatch(
                str(assessment.get("supersedes_source_event_id") or "")
            )
            or not SHA256_RE.fullmatch(str(assessment.get("source_event_sha256") or ""))
            or assessment.get("supersession_reason")
            != "prepared_grader_key_conflict"
        ):
            raise BackflowError("sealed direct grader correction is invalid")
        choice_only_reasoning_override = False
        if sealed.get("evidence_kind") == "cs408_conversation_package_v1":
            reopened = sol_decision.reopen_direct_evidence(
                str(sealed.get("evidence_locator") or ""),
                str(sealed.get("reopened_evidence_sha256") or ""),
            )
            conversation = (reopened.get("package") or {}).get("conversation") or {}
            turns = conversation.get("turns") or []
            choice_only_reasoning_override = bool(
                conversation.get("complete_order_preserved") is True
                and conversation.get("truncated") is False
                and any(
                    isinstance(turn, dict)
                    and turn.get("role") == "user"
                    and turn.get("kind") in {"reasoning", "first_action", "restatement"}
                    and str(turn.get("text") or "").strip()
                    for turn in turns
                )
            )
        direct_assessment = {
            **assessment,
            "first_result": (
                "uncertain"
                if assessment.get("first_result") == "blank"
                else assessment.get("first_result")
            ),
            "decision_sha256": decision_sha256,
            "_choice_only_reasoning_override": choice_only_reasoning_override,
            "_blank_normalized_to_uncertain": assessment.get("first_result")
            == "blank",
        }
    deferred_morning_outcome = None
    if (
        direct_assessment is not None
        and sealed_direct is not None
        and direct_assessment.get("source") == "morning_review"
    ):
        deferred_morning_outcome = _ensure_deferred_morning_outcome(
            repo,
            batch_id=batch,
            capture_id=capture,
            sealed_decision=sealed_direct,
            direct_assessment=direct_assessment,
        )
    frozen = _freeze_schedule_input(
        repo,
        batch_id=batch,
        capture_id=capture,
        policy_version=policy_version,
        direct_assessment=direct_assessment,
        source_event_override=(
            deferred_morning_outcome.get("event")
            if deferred_morning_outcome is not None
            else None
        ),
    )
    input_sha = str(frozen["input_sha256"])
    key = idempotency_key or f"formal-backflow:ensure:{batch}:{capture}:{input_sha}"
    key = _safe_id(key, "idempotency_key")
    request_sha = _sha_value(
        {
            "schema": "review_backflow_ensure_request_v1",
            "input_sha256": input_sha,
            "idempotency_key": key,
        }
    )
    with _transaction(repo):
        prior = _receipt_candidates(repo, key, ENSURE_RECEIPT_SCHEMA)
        if prior:
            if len(prior) != 1 or prior[0].get("request_sha256") != request_sha:
                raise BackflowError("ensure idempotency key was reused with drifted input")
            return validate_review_backflow_receipt(repo, prior[0]["receipt_sha256"])
        events = review._read_events(repo)
        evidence_sha = str(
            frozen.get("source_review_event_sha256")
            or frozen.get("capture_payload_sha256")
            or frozen.get("fact_event_sha256")
        )
        event_key = f"formal-backflow:ensure-event:{batch}:{capture}:{input_sha}"
        try:
            event = review.build_review_schedule_ensure_event(
                events,
                batch_id=batch,
                capture_id=capture,
                source=str(frozen["source"]),
                study_date=str(frozen["study_date"]),
                source_id=str(frozen["source_id"]),
                source_review_event_id=frozen.get("source_review_event_id"),
                source_review_event_identity=frozen.get("source_review_event_identity"),
                review_unit_id=frozen.get("review_unit_id"),
                formal_node_id=frozen.get("formal_node_id"),
                first_result=str(frozen["first_result"]),
                confidence=str(frozen["confidence"]),
                prompt_level=str(frozen["prompt_level"]),
                reasoning_break=bool(frozen["reasoning_break"]),
                first_break=str(frozen["first_break"]),
                first_break_provenance=str(frozen["first_break_provenance"]),
                policy_version=str(frozen["policy_version"]),
                evidence_receipt_sha256=evidence_sha,
                event_time=str(frozen["event_time"]),
                idempotency_key=event_key,
            )
            appended = review.append_review_control_event(repo, event)
        except review.ReviewLoopError as exc:
            raise BackflowError(str(exc)) from exc
        canonical_event = appended.get("event", event) if isinstance(appended, dict) else event
        if (
            frozen.get("source_outcome_override")
            == "choice_only_grader_reasoning_conflict"
            and frozen.get("source_review_event_id")
            and frozen.get("source_review_event_sha256")
        ):
            correction_key = (
                f"formal-backflow:grader-correction:{batch}:{capture}:"
                f"{direct_assessment['decision_sha256']}"
            )
            try:
                correction_events = review._read_events(repo)
                existing_corrections = [
                    row
                    for row in correction_events
                    if row.get("event_kind")
                    == review.GRADER_OUTCOME_CORRECTION_KIND
                    and row.get("batch_id") == batch
                    and row.get("capture_id") == capture
                    and row.get("source_outcome_event_id")
                    == frozen["source_review_event_id"]
                    and row.get("reason_code")
                    == "choice_only_grader_reasoning_conflict"
                ]
                if len(existing_corrections) > 1:
                    raise review.ReviewLoopError(
                        "multiple choice-only reasoning corrections exist"
                    )
                if not existing_corrections:
                    correction = review.build_grader_outcome_correction_event(
                        correction_events,
                        batch_id=batch,
                        capture_id=capture,
                        source_outcome_event_id=str(
                            frozen["source_review_event_id"]
                        ),
                        source_outcome_event_sha256=str(
                            frozen["source_review_event_sha256"]
                        ),
                        corrected_ensure_event_id=str(canonical_event["event_id"]),
                        corrected_ensure_event_sha256=_sha_value(canonical_event),
                        event_time=dt.datetime.now(dt.timezone.utc).isoformat(),
                        idempotency_key=correction_key,
                        reason_code="choice_only_grader_reasoning_conflict",
                    )
                    review.append_review_control_event(repo, correction)
            except review.ReviewLoopError as exc:
                raise BackflowError(str(exc)) from exc
        disposition = str(canonical_event.get("schedule_disposition") or "")
        status_map = {
            "created_action": "created_action",
            "reused_existing_action": "reused_existing_action",
            "maintenance_noop": "maintenance_noop",
        }
        status = status_map.get(disposition)
        if status is None:
            raise BackflowError("ensure event has an invalid schedule disposition")
        action_id = canonical_event.get("review_action_id")
        due_date = None
        if action_id:
            state = review.load_state(repo)
            action = next(
                (row for row in state.get("actions", []) if row.get("action_id") == action_id),
                None,
            )
            if action is None:
                raise BackflowError("ensure event did not project its canonical action")
            due_date = action.get("due_date")
        payload = {
            "schema": ENSURE_RECEIPT_SCHEMA,
            "status": status,
            "idempotency_key": key,
            "request_sha256": request_sha,
            "batch_id": batch,
            "capture_id": capture,
            "study_date": frozen["study_date"],
            "policy_version": frozen["policy_version"],
            "schedule_input_sha256": input_sha,
            "schedule_input": frozen,
            "ensure_event_id": canonical_event["event_id"],
            "action_id": action_id,
            "due_date": due_date,
            "action_family": canonical_event.get("action_family"),
            "source_review_event_id": frozen.get("source_review_event_id"),
            "formal_write_authorized": False,
        }
        if deferred_morning_outcome is not None:
            event_value = deferred_morning_outcome["event"]
            payload["deferred_morning_outcome"] = {
                "status": deferred_morning_outcome["status"],
                "event_id": event_value["event_id"],
                "event_sha256": _sha_value(event_value),
                "binding_sha256": (
                    _sha_value(deferred_morning_outcome["binding"])
                    if deferred_morning_outcome.get("binding") is not None
                    else None
                ),
            }
            payload["learner_outcome_write_count"] = deferred_morning_outcome[
                "learner_outcome_write_count"
            ]
        return _write_receipt(repo, payload)


def _read_receipt(repo: Path, digest: str) -> tuple[dict[str, Any], Path]:
    if not SHA256_RE.fullmatch(str(digest or "")):
        raise BackflowError("receipt_sha256 is invalid")
    matches: list[Path] = []
    for schema in (ENSURE_RECEIPT_SCHEMA, FINAL_RECEIPT_SCHEMA):
        path = _receipt_dir(repo, schema, create=False) / f"{digest}.json"
        if path.is_file() and not path.is_symlink():
            matches.append(path)
    if len(matches) != 1:
        raise BackflowError("backflow receipt was not found uniquely")
    path = matches[0]
    if _sha_file(path) != digest:
        # atomic_write_json uses pretty JSON while the receipt identity is the
        # canonical object digest.  Recompute from the parsed object below.
        pass
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise BackflowError("backflow receipt is invalid JSON") from exc
    if not isinstance(value, dict) or _sha_value(value) != digest:
        raise BackflowError("backflow receipt content hash drifted")
    return value, path


def record_formal_committed_backflow_pending(
    repo_root: str | Path,
    *,
    batch_id: str,
    capture_id: str,
    ensure_receipt_sha256: str,
    formal_result_status: str,
    formal_result_receipt_sha256: str,
    formal_id: str,
    error_code: str,
    formal_proof_ref: str | None = None,
    historical_result_event_id: str | None = None,
    idempotency_key: str | None = None,
) -> dict[str, Any]:
    """Publish a content-addressed recovery terminal after formal commit.

    This entry point never appends a review event or invokes formal apply.  A
    later ``bind_formal_intake`` call with the same request can supersede the
    pending receipt with a ``satisfied`` receipt.
    """

    repo = _resolve_repo(repo_root)
    batch = _safe_id(batch_id, "batch_id")
    capture = _safe_id(capture_id, "capture_id")
    formal = _safe_id(formal_id, "formal_id")
    error = _safe_id(error_code, "error_code")
    ensure = validate_review_backflow_receipt(
        repo, ensure_receipt_sha256, expected_schema=ENSURE_RECEIPT_SCHEMA
    )
    if ensure.get("batch_id") != batch or ensure.get("capture_id") != capture:
        raise BackflowError("ensure receipt belongs to another batch/capture")
    request = {
        "schema": "formal_intake_review_binding_request_v1",
        "batch_id": batch,
        "capture_id": capture,
        "ensure_receipt_sha256": ensure_receipt_sha256,
        "formal_result_status": formal_result_status,
        "formal_result_receipt_sha256": formal_result_receipt_sha256,
        "formal_id": formal,
        "historical_result_event_id": historical_result_event_id,
    }
    request_sha = _sha_value(request)
    key = idempotency_key or f"formal-backflow:bind:{batch}:{capture}:{request_sha}"
    key = _safe_id(key, "idempotency_key")
    with _transaction(repo):
        prior = _receipt_candidates(repo, key, FINAL_RECEIPT_SCHEMA)
        successful = [row for row in prior if row.get("status") in SUCCESS_FINAL_STATUSES]
        if successful:
            if len(successful) != 1 or successful[0].get("request_sha256") != request_sha:
                raise BackflowError("pending idempotency key conflicts with satisfied binding")
            return validate_review_backflow_receipt(repo, successful[0]["receipt_sha256"])
        if any(row.get("request_sha256") != request_sha for row in prior):
            raise BackflowError("pending idempotency key was reused with drifted input")
        same = [row for row in prior if row.get("error_code") == error]
        if same:
            return validate_review_backflow_receipt(repo, same[-1]["receipt_sha256"])
        payload = {
            "schema": FINAL_RECEIPT_SCHEMA,
            "status": "formal_committed_backflow_pending",
            "idempotency_key": key,
            "request_sha256": request_sha,
            "batch_id": batch,
            "capture_id": capture,
            "study_date": ensure.get("study_date"),
            "ensure_receipt_sha256": ensure_receipt_sha256,
            "formal_id": formal,
            "formal_node_id": formal,
            "review_unit_id": None,
            "action_id": ensure.get("action_id"),
            "due_date": ensure.get("due_date"),
            "formal_proof_kind": (
                "normal_receipt"
                if formal_result_status == "curated"
                else "already_current_verification"
            ),
            "formal_result_status": formal_result_status,
            "formal_proof_sha256": formal_result_receipt_sha256,
            "formal_proof_ref": formal_proof_ref,
            "formal_binding_event_id": None,
            "historical_result_event_id": historical_result_event_id,
            "recovery_of_receipt_sha256": prior[-1]["receipt_sha256"] if prior else None,
            "error_code": error,
            "formal_write_authorized": False,
        }
        return _write_receipt(repo, payload)


def bind_formal_intake(
    repo_root: str | Path,
    *,
    batch_id: str,
    capture_id: str,
    ensure_receipt_sha256: str,
    formal_result_status: str,
    formal_result_receipt_sha256: str,
    formal_id: str,
    formal_proof_path: str | Path | None = None,
    runtime_root: str | Path = DEFAULT_RUNTIME_ROOT,
    historical_result_event_id: str | None = None,
    idempotency_key: str | None = None,
) -> dict[str, Any]:
    """Bind one proven formal result; never invoke or replay formal apply."""

    repo = _resolve_repo(repo_root)
    batch = _safe_id(batch_id, "batch_id")
    capture = _safe_id(capture_id, "capture_id")
    formal = _safe_id(formal_id, "formal_id")
    ensure = validate_review_backflow_receipt(
        repo, ensure_receipt_sha256, expected_schema=ENSURE_RECEIPT_SCHEMA
    )
    if ensure.get("batch_id") != batch or ensure.get("capture_id") != capture:
        raise BackflowError("ensure receipt belongs to another batch/capture")
    proof_digest = str(formal_result_receipt_sha256 or "")
    historical_result = _historical_result_event(repo, historical_result_event_id)
    request = {
        "schema": "formal_intake_review_binding_request_v1",
        "batch_id": batch,
        "capture_id": capture,
        "ensure_receipt_sha256": ensure_receipt_sha256,
        "formal_result_status": formal_result_status,
        "formal_result_receipt_sha256": proof_digest,
        "formal_id": formal,
        "historical_result_event_id": historical_result_event_id,
    }
    request_sha = _sha_value(request)
    key = idempotency_key or f"formal-backflow:bind:{batch}:{capture}:{request_sha}"
    key = _safe_id(key, "idempotency_key")
    with _transaction(repo):
        prior = _receipt_candidates(repo, key, FINAL_RECEIPT_SCHEMA)
        successful = [row for row in prior if row.get("status") in SUCCESS_FINAL_STATUSES]
        if successful:
            if len(successful) != 1 or successful[0].get("request_sha256") != request_sha:
                raise BackflowError("bind idempotency key was reused with drifted input")
            return validate_review_backflow_receipt(repo, successful[0]["receipt_sha256"])
        if any(row.get("request_sha256") != request_sha for row in prior):
            raise BackflowError("bind idempotency key was reused with drifted input")
        recovery_of = prior[-1]["receipt_sha256"] if prior else None
        try:
            proof = verify_formal_proof(
                repo,
                batch_id=batch,
                capture_id=capture,
                formal_id=formal,
                formal_result_status=formal_result_status,
                formal_result_receipt_sha256=proof_digest,
                formal_proof_path=formal_proof_path,
                runtime_root=runtime_root,
                historical_result_event=historical_result,
            )
        except (FormalProofError, OSError) as exc:
            pending = {
                "schema": FINAL_RECEIPT_SCHEMA,
                "status": "formal_committed_backflow_pending",
                "idempotency_key": key,
                "request_sha256": request_sha,
                "batch_id": batch,
                "capture_id": capture,
                "study_date": ensure.get("study_date"),
                "ensure_receipt_sha256": ensure_receipt_sha256,
                "formal_id": formal,
                "formal_node_id": formal,
                "review_unit_id": None,
                "action_id": ensure.get("action_id"),
                "due_date": ensure.get("due_date"),
                "formal_proof_kind": (
                    "normal_receipt"
                    if formal_result_status == "curated"
                    else "already_current_verification"
                ),
                "formal_result_status": formal_result_status,
                "formal_proof_sha256": proof_digest,
                "formal_proof_ref": str(formal_proof_path) if formal_proof_path else None,
                "formal_binding_event_id": None,
                "historical_result_event_id": historical_result_event_id,
                "recovery_of_receipt_sha256": recovery_of,
                "error_code": "formal_proof_unverified",
                "formal_write_authorized": False,
            }
            return _write_receipt(repo, pending)
        events = review._read_events(repo)
        binding_key = (
            f"formal-backflow:binding-event:{batch}:{capture}:"
            f"{ensure['ensure_event_id']}:{proof.sha256}"
        )
        try:
            event = review.build_formal_review_binding_event(
                events,
                batch_id=batch,
                capture_id=capture,
                review_action_id=ensure.get("action_id"),
                ensure_event_id=str(ensure["ensure_event_id"]),
                source_id=str(ensure["schedule_input"]["source_id"]),
                source_review_event_id=ensure.get("source_review_event_id"),
                formal_result_status=formal_result_status,
                formal_result_receipt_sha256=proof.sha256,
                ensure_receipt_sha256=ensure_receipt_sha256,
                formal_node_id=formal,
                review_unit_id=proof.review_unit_id,
                observed_date=str(ensure["study_date"]),
                event_time=str(ensure["schedule_input"]["event_time"]),
                idempotency_key=binding_key,
            )
            appended = review.append_review_control_event(repo, event)
        except review.ReviewLoopError as exc:
            pending = {
                "schema": FINAL_RECEIPT_SCHEMA,
                "status": "formal_committed_backflow_pending",
                "idempotency_key": key,
                "request_sha256": request_sha,
                "batch_id": batch,
                "capture_id": capture,
                "study_date": ensure.get("study_date"),
                "ensure_receipt_sha256": ensure_receipt_sha256,
                "formal_id": formal,
                "formal_node_id": formal,
                "review_unit_id": proof.review_unit_id,
                "action_id": ensure.get("action_id"),
                "due_date": ensure.get("due_date"),
                "formal_proof_kind": proof.kind,
                "formal_result_status": formal_result_status,
                "formal_proof_sha256": proof.sha256,
                "formal_proof_ref": proof.ref,
                "formal_binding_event_id": None,
                "historical_result_event_id": historical_result_event_id,
                "recovery_of_receipt_sha256": recovery_of,
                "error_code": "formal_binding_append_failed",
                "formal_write_authorized": False,
            }
            return _write_receipt(repo, pending)
        canonical_event = appended.get("event", event) if isinstance(appended, dict) else event
        status = "maintenance_noop" if ensure.get("status") == "maintenance_noop" else "satisfied"
        final = {
            "schema": FINAL_RECEIPT_SCHEMA,
            "status": status,
            "idempotency_key": key,
            "request_sha256": request_sha,
            "batch_id": batch,
            "capture_id": capture,
            "study_date": ensure.get("study_date"),
            "ensure_receipt_sha256": ensure_receipt_sha256,
            "formal_id": formal,
            "formal_node_id": formal,
            "review_unit_id": proof.review_unit_id,
            "action_id": canonical_event.get("canonical_review_action_id", ensure.get("action_id")),
            "due_date": ensure.get("due_date"),
            "formal_proof_kind": proof.kind,
            "formal_result_status": formal_result_status,
            "formal_proof_sha256": proof.sha256,
            "formal_proof_ref": proof.ref,
            "formal_mapping_basis": proof.mapping_basis,
            "formal_mapping_sha256": proof.mapping_sha256,
            "formal_binding_event_id": canonical_event["event_id"],
            "historical_result_event_id": historical_result_event_id,
            "recovery_of_receipt_sha256": recovery_of,
            "generated_actions": [],
            "formal_write_authorized": False,
        }
        return _write_receipt(repo, final)


def validate_review_backflow_receipt(
    repo_root: str | Path,
    receipt_sha256: str,
    *,
    expected_schema: str | None = None,
) -> dict[str, Any]:
    repo = _resolve_repo(repo_root)
    value, path = _read_receipt(repo, receipt_sha256)
    schema = value.get("schema")
    if expected_schema is not None and schema != expected_schema:
        raise BackflowError("backflow receipt schema mismatch")
    if schema not in {ENSURE_RECEIPT_SCHEMA, FINAL_RECEIPT_SCHEMA}:
        raise BackflowError("unknown backflow receipt schema")
    if any(
        value.get(field) != 0
        for field in ZERO_COUNTS
        if field != "learner_outcome_write_count"
    ):
        raise BackflowError("backflow receipt claims a forbidden write or call")
    outcome_write_count = value.get("learner_outcome_write_count")
    if outcome_write_count not in {0, 1}:
        raise BackflowError("backflow learner outcome count is invalid")
    if schema == ENSURE_RECEIPT_SCHEMA:
        event = _control_event(repo, str(value.get("ensure_event_id") or ""))
        if event.get("event_kind") != getattr(
            review, "REVIEW_SCHEDULE_ENSURE_KIND", "review_schedule_ensure_v1"
        ):
            raise BackflowError("ensure receipt references the wrong event kind")
        if (
            event.get("batch_id") != value.get("batch_id")
            or event.get("capture_id") != value.get("capture_id")
            or event.get("review_action_id") != value.get("action_id")
        ):
            raise BackflowError("ensure receipt/event binding drifted")
        if value.get("status") not in {
            "created_action",
            "reused_existing_action",
            "maintenance_noop",
        }:
            raise BackflowError("ensure receipt status is invalid")
        deferred = value.get("deferred_morning_outcome")
        if deferred is None:
            if outcome_write_count != 0:
                raise BackflowError("ordinary ensure cannot claim a learner outcome write")
        else:
            if not isinstance(deferred, dict) or set(deferred) != {
                "status", "event_id", "event_sha256", "binding_sha256"
            }:
                raise BackflowError("deferred morning receipt fields are invalid")
            outcome_event = _control_event(repo, str(deferred.get("event_id") or ""))
            binding = outcome_event.get("deferred_morning_binding")
            if (
                outcome_event.get("event_kind") != "outcome"
                or outcome_event.get("source") != "morning_review"
                or _sha_value(outcome_event) != deferred.get("event_sha256")
                or value.get("source_review_event_id") != outcome_event.get("event_id")
                or (value.get("schedule_input") or {}).get(
                    "source_review_event_sha256"
                )
                != deferred.get("event_sha256")
            ):
                raise BackflowError("deferred morning outcome receipt binding drifted")
            status_value = deferred.get("status")
            if status_value == "created_deferred_outcome":
                if (
                    outcome_write_count != 1
                    or not isinstance(binding, dict)
                    or _sha_value(binding) != deferred.get("binding_sha256")
                ):
                    raise BackflowError("created deferred morning outcome proof is invalid")
            elif status_value == "reused_existing_outcome":
                if outcome_write_count != 0 or deferred.get("binding_sha256") is not None:
                    raise BackflowError("reused morning outcome proof is invalid")
            else:
                raise BackflowError("deferred morning outcome status is invalid")
    else:
        if outcome_write_count != 0:
            raise BackflowError("final binding receipt cannot claim a learner outcome write")
        ensure_sha = str(value.get("ensure_receipt_sha256") or "")
        ensure = validate_review_backflow_receipt(
            repo, ensure_sha, expected_schema=ENSURE_RECEIPT_SCHEMA
        )
        if (
            ensure.get("batch_id") != value.get("batch_id")
            or ensure.get("capture_id") != value.get("capture_id")
        ):
            raise BackflowError("final/ensure receipt identity mismatch")
        status = value.get("status")
        if status in SUCCESS_FINAL_STATUSES:
            event = _control_event(repo, str(value.get("formal_binding_event_id") or ""))
            if event.get("event_kind") != getattr(
                review, "FORMAL_REVIEW_BINDING_KIND", "formal_review_binding_v1"
            ):
                raise BackflowError("final receipt references the wrong event kind")
            if event.get("generated_actions") != []:
                raise BackflowError("formal binding event must not generate an action")
            if (
                event.get("batch_id") != value.get("batch_id")
                or event.get("capture_id") != value.get("capture_id")
                or event.get("formal_node_id") != value.get("formal_node_id")
                or event.get("review_unit_id") != value.get("review_unit_id")
                or event.get("formal_result_status")
                != value.get("formal_result_status")
                or event.get("formal_result_receipt_sha256")
                != value.get("formal_proof_sha256")
            ):
                raise BackflowError("formal binding receipt/event drifted")
            proof_ref = Path(str(value.get("formal_proof_ref") or "")).expanduser()
            try:
                runtime = proof_ref.resolve(strict=True).parents[2]
            except (OSError, IndexError) as exc:
                raise BackflowError("formal proof locator is not recoverable") from exc
            result_status = str(value.get("formal_result_status") or "")
            expected_kind = {
                "curated": "normal_receipt",
                "already_current": "already_current_verification",
            }.get(result_status)
            if expected_kind is None or value.get("formal_proof_kind") != expected_kind:
                raise BackflowError("formal result status/proof kind mismatch")
            historical = _historical_result_event(
                repo, value.get("historical_result_event_id")
            )
            try:
                proof = verify_formal_proof(
                    repo,
                    batch_id=str(value.get("batch_id") or ""),
                    capture_id=str(value.get("capture_id") or ""),
                    formal_id=str(value.get("formal_id") or ""),
                    formal_result_status=result_status,
                    formal_result_receipt_sha256=str(
                        value.get("formal_proof_sha256") or ""
                    ),
                    formal_proof_path=proof_ref,
                    runtime_root=runtime,
                    historical_result_event=historical,
                )
            except (FormalProofError, OSError) as exc:
                raise BackflowError(str(exc)) from exc
            # A normal receipt freezes the exact committed mapping stage, so
            # its hash is immutable.  An already-current verification resolves
            # the target row from the live append-only mapping table; unrelated
            # later node additions legitimately change the whole-file hash.
            # In that case the verifier above has already rechecked the exact
            # formal_id -> review_unit_id binding, so do not reject on global
            # mapping-file drift alone.
            mapping_hash_drift = (
                proof.kind == "normal_receipt"
                and proof.mapping_sha256 != value.get("formal_mapping_sha256")
            )
            if (
                proof.review_unit_id != value.get("review_unit_id")
                or mapping_hash_drift
                or proof.mapping_basis != value.get("formal_mapping_basis")
            ):
                raise BackflowError("formal proof/RU binding drifted")
        elif status != "formal_committed_backflow_pending":
            raise BackflowError("final receipt status is invalid")
    returned = dict(value)
    returned["receipt_sha256"] = receipt_sha256
    returned["receipt_path"] = path.relative_to(repo).as_posix()
    return returned


def _formal_success_rows(events: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [
        row
        for row in events
        if row.get("event_type") == "curation_item_result"
        and row.get("outcome") in {"curated", "already_current"}
    ]


def _existing_causal_action(
    repo: Path, source_event: dict[str, Any] | None
) -> str | None:
    if source_event is None:
        return None
    source_id = str(source_event.get("event_id") or "")
    state = review._reduce(review._read_events(repo))
    matches = []
    for action in state.get("actions", []):
        if action.get("action_type") != "review":
            continue
        triggers = set(
            action.get("trigger_event_ids")
            or ([action.get("trigger_event_id")] if action.get("trigger_event_id") else [])
        )
        if source_id in triggers:
            matches.append(action)
    if not matches:
        return None
    families = {
        review.review_action_family(str(action.get("stage") or ""))
        for action in matches
    }
    if len(families) != 1:
        raise BackflowError("one source outcome causally owns conflicting action families")
    selected = sorted(matches, key=review._prefer_d1_family_action)[0]
    rescue = [action for action in matches if action.get("stage") == "D1_rescue"]
    if rescue:
        selected = sorted(rescue, key=review._prefer_d1_family_action)[0]
    return str(selected["action_id"])


def audit_history(
    repo_root: str | Path,
    *,
    runtime_root: str | Path = DEFAULT_RUNTIME_ROOT,
    policy_version: str = DEFAULT_POLICY_VERSION,
) -> dict[str, Any]:
    """Build a read-only, exact-denominator historical repair manifest."""

    repo = _resolve_repo(repo_root)
    intake_path = repo / INTAKE_LEDGER_REL
    intake_events = _read_jsonl(intake_path, "intake ledger")
    successes = _formal_success_rows(intake_events)
    rows: list[dict[str, Any]] = []
    counts = {terminal: 0 for terminal in TERMINALS}
    for result in successes:
        batch_id = str(result.get("batch_id") or "")
        capture_id = str(result.get("capture_id") or "")
        terminal = "needs_user"
        reason = "unclassified"
        action_id = None
        review_unit_id = None
        proof_digest = str(
            result.get("receipt_sha256")
            if result.get("outcome") == "curated"
            else result.get("verification_sha256")
            or ""
        )
        try:
            frozen = _freeze_schedule_input(
                repo,
                batch_id=batch_id,
                capture_id=capture_id,
                policy_version=policy_version,
            )
            proof = verify_formal_proof(
                repo,
                batch_id=batch_id,
                capture_id=capture_id,
                formal_id=str(result.get("formal_id") or ""),
                formal_result_status=str(result.get("outcome") or ""),
                formal_result_receipt_sha256=proof_digest,
                runtime_root=runtime_root,
                historical_result_event=result,
            )
            review_unit_id = proof.review_unit_id
            source_event = None
            if frozen.get("source_review_event_id"):
                source_matches = [
                    event
                    for event in review._read_events(repo)
                    if event.get("event_id") == frozen["source_review_event_id"]
                ]
                if len(source_matches) != 1:
                    raise BackflowError("source review event is not canonical")
                source_event = source_matches[0]
            action_id = _existing_causal_action(repo, source_event)
            stable_correct = (
                frozen.get("source") in {"daily_practice", "evening_d0"}
                and frozen.get("first_result") == "independent_correct"
                and frozen.get("confidence") == "high"
                and frozen.get("prompt_level") == "none"
                and not frozen.get("reasoning_break")
            )
            if stable_correct:
                terminal = "maintenance_noop"
                reason = "ordinary stable independent correct needs no morning action"
            elif action_id:
                terminal = "reuse_existing_action"
                reason = "source outcome causally owns one canonical action"
            else:
                terminal = "create_missing_action"
                reason = "learning evidence and formal proof are complete but no causal action exists"
        except (BackflowError, FormalProofError, review.ReviewLoopError, OSError) as exc:
            terminal = "needs_user"
            reason = str(exc)
        counts[terminal] += 1
        rows.append(
            {
                "result_event_id": result.get("event_id"),
                "batch_id": batch_id,
                "capture_id": capture_id,
                "outcome": result.get("outcome"),
                "formal_id": result.get("formal_id"),
                "formal_result_receipt_sha256": proof_digest or None,
                "review_unit_id": review_unit_id,
                "action_id": action_id,
                "terminal": terminal,
                "reason": reason,
            }
        )
    intake_sha = _sha_file(intake_path) if intake_path.is_file() else _sha_value([])
    review_path = repo / REVIEW_LEDGER_REL
    review_bytes = review_path.read_bytes() if review_path.is_file() else b""
    review_sha = hashlib.sha256(review_bytes).hexdigest()
    payload = {
        "schema": HISTORY_AUDIT_SCHEMA,
        "timezone": "Asia/Shanghai",
        "policy_version": policy_version,
        "intake_ledger_sha256": intake_sha,
        "review_ledger_cutoff_sha256": review_sha,
        "review_ledger_cutoff_bytes": len(review_bytes),
        "review_ledger_cutoff_event_count": len(review._read_events(repo)),
        "formal_success_denominator": len(successes),
        "terminal_counts": counts,
        "rows": rows,
        "read_only": True,
        **ZERO_COUNTS,
    }
    payload["manifest_sha256"] = _sha_value(payload)
    return payload


def repair_history(
    repo_root: str | Path,
    *,
    manifest: dict[str, Any] | None = None,
    apply: bool = False,
    runtime_root: str | Path = DEFAULT_RUNTIME_ROOT,
    classifications: set[str] | None = None,
    limit: int | None = None,
) -> dict[str, Any]:
    """Apply only verifiable manifest rows; default is a zero-write dry run."""

    repo = _resolve_repo(repo_root)
    frozen = manifest or audit_history(repo, runtime_root=runtime_root)
    supplied_sha = str(frozen.get("manifest_sha256") or "")
    material = dict(frozen)
    material.pop("manifest_sha256", None)
    if frozen.get("schema") != HISTORY_AUDIT_SCHEMA or _sha_value(material) != supplied_sha:
        raise BackflowError("history manifest identity is invalid")
    current_intake = repo / INTAKE_LEDGER_REL
    if not current_intake.is_file() or _sha_file(current_intake) != frozen.get(
        "intake_ledger_sha256"
    ):
        raise BackflowError("intake ledger changed after history audit freeze")
    current_review = repo / REVIEW_LEDGER_REL
    current_review_bytes = (
        current_review.read_bytes() if current_review.is_file() else b""
    )
    cutoff_bytes = int(frozen.get("review_ledger_cutoff_bytes", -1))
    cutoff_count = int(frozen.get("review_ledger_cutoff_event_count", -1))
    if (
        cutoff_bytes < 0
        or cutoff_count < 0
        or len(current_review_bytes) < cutoff_bytes
        or hashlib.sha256(current_review_bytes[:cutoff_bytes]).hexdigest()
        != frozen.get("review_ledger_cutoff_sha256")
    ):
        raise BackflowError("review ledger prefix changed after history audit freeze")
    current_review_events = review._read_events(repo)
    if len(current_review_events) < cutoff_count:
        raise BackflowError("review ledger event count regressed after history audit freeze")
    actionable_pairs = {
        (str(row.get("batch_id") or ""), str(row.get("capture_id") or ""))
        for row in frozen.get("rows", [])
        if row.get("terminal") in ACTIONABLE_TERMINALS
    }
    for suffix_event in current_review_events[cutoff_count:]:
        if (
            suffix_event.get("event_kind") not in {
                getattr(
                    review,
                    "REVIEW_SCHEDULE_ENSURE_KIND",
                    "review_schedule_ensure_v1",
                ),
                getattr(
                    review,
                    "FORMAL_REVIEW_BINDING_KIND",
                    "formal_review_binding_v1",
                ),
            }
            or (
                str(suffix_event.get("batch_id") or ""),
                str(suffix_event.get("capture_id") or ""),
            )
            not in actionable_pairs
        ):
            raise BackflowError(
                "review ledger has an unrelated suffix after history audit freeze"
            )
    if sum(int(value) for value in frozen.get("terminal_counts", {}).values()) != int(
        frozen.get("formal_success_denominator", -1)
    ):
        raise BackflowError("history manifest terminal partition is incomplete")
    if classifications is not None and not classifications <= ACTIONABLE_TERMINALS:
        raise BackflowError("needs_user history rows cannot be repaired automatically")
    allowed = classifications or set(ACTIONABLE_TERMINALS)
    selected = [row for row in frozen.get("rows", []) if row.get("terminal") in allowed]
    if limit is not None:
        if limit < 0:
            raise BackflowError("limit must be non-negative")
        selected = selected[:limit]
    if not apply:
        return {
            "status": "DRY_RUN",
            "manifest_sha256": supplied_sha,
            "selected_count": len(selected),
            "write_count": 0,
            "formal_apply_count": 0,
            "rows": selected,
        }
    results: list[dict[str, Any]] = []
    created_events = 0
    for row in selected:
        before = len(review._read_events(repo))
        ensured = ensure_review_backflow(
            repo,
            batch_id=str(row["batch_id"]),
            capture_id=str(row["capture_id"]),
            policy_version=str(frozen["policy_version"]),
        )
        bound = bind_formal_intake(
            repo,
            batch_id=str(row["batch_id"]),
            capture_id=str(row["capture_id"]),
            ensure_receipt_sha256=str(ensured["receipt_sha256"]),
            formal_result_status=str(row["outcome"]),
            formal_result_receipt_sha256=str(row["formal_result_receipt_sha256"]),
            formal_id=str(row["formal_id"]),
            runtime_root=runtime_root,
            historical_result_event_id=str(row["result_event_id"]),
        )
        after = len(review._read_events(repo))
        created_events += max(0, after - before)
        results.append(
            {
                "result_event_id": row["result_event_id"],
                "ensure_receipt_sha256": ensured["receipt_sha256"],
                "final_receipt_sha256": bound["receipt_sha256"],
                "status": bound["status"],
            }
        )
    return {
        "status": "APPLIED",
        "manifest_sha256": supplied_sha,
        "selected_count": len(selected),
        "write_count": created_events,
        "formal_apply_count": 0,
        "results": results,
    }


def _load_json(path_value: str, label: str) -> dict[str, Any]:
    path = Path(path_value).expanduser()
    if path.is_symlink() or not path.is_file():
        raise BackflowError(f"{label} must be a regular JSON file")
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise BackflowError(f"{label} is invalid JSON") from exc
    if not isinstance(value, dict):
        raise BackflowError(f"{label} must contain one object")
    return value


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="408 formal-intake review backflow")
    parser.add_argument("--repo", default=".")
    commands = parser.add_subparsers(dest="command", required=True)

    ensure = commands.add_parser("ensure")
    ensure.add_argument("--batch-id", required=True)
    ensure.add_argument("--capture-id", required=True)
    ensure.add_argument("--policy-version", required=True)
    ensure.add_argument("--idempotency-key")
    ensure.add_argument("--decision-locator")
    ensure.add_argument("--decision-sha256")

    bind = commands.add_parser("bind")
    bind.add_argument("--batch-id", required=True)
    bind.add_argument("--capture-id", required=True)
    bind.add_argument("--ensure-receipt-sha256", required=True)
    bind.add_argument("--formal-result-status", choices=("curated", "already_current"), required=True)
    bind.add_argument("--formal-result-receipt-sha256", required=True)
    bind.add_argument("--formal-id", required=True)
    bind.add_argument("--formal-proof-path")
    bind.add_argument("--runtime-root", default=str(DEFAULT_RUNTIME_ROOT))
    bind.add_argument("--historical-result-event-id")
    bind.add_argument("--idempotency-key")

    validate = commands.add_parser("validate")
    validate.add_argument("--receipt-sha256", required=True)
    validate.add_argument("--expected-schema", choices=(ENSURE_RECEIPT_SCHEMA, FINAL_RECEIPT_SCHEMA))

    audit = commands.add_parser("audit-history")
    audit.add_argument("--runtime-root", default=str(DEFAULT_RUNTIME_ROOT))
    audit.add_argument("--policy-version", default=DEFAULT_POLICY_VERSION)

    repair = commands.add_parser("repair-history")
    repair.add_argument("--manifest")
    repair.add_argument("--runtime-root", default=str(DEFAULT_RUNTIME_ROOT))
    repair.add_argument("--apply", action="store_true")
    repair.add_argument(
        "--classification", action="append", choices=sorted(ACTIONABLE_TERMINALS)
    )
    repair.add_argument("--limit", type=int)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        if args.command == "ensure":
            result = ensure_review_backflow(
                args.repo,
                batch_id=args.batch_id,
                capture_id=args.capture_id,
                policy_version=args.policy_version,
                idempotency_key=args.idempotency_key,
                decision_locator=args.decision_locator,
                decision_sha256=args.decision_sha256,
            )
        elif args.command == "bind":
            result = bind_formal_intake(
                args.repo,
                batch_id=args.batch_id,
                capture_id=args.capture_id,
                ensure_receipt_sha256=args.ensure_receipt_sha256,
                formal_result_status=args.formal_result_status,
                formal_result_receipt_sha256=args.formal_result_receipt_sha256,
                formal_id=args.formal_id,
                formal_proof_path=args.formal_proof_path,
                runtime_root=args.runtime_root,
                historical_result_event_id=args.historical_result_event_id,
                idempotency_key=args.idempotency_key,
            )
        elif args.command == "validate":
            result = validate_review_backflow_receipt(
                args.repo,
                args.receipt_sha256,
                expected_schema=args.expected_schema,
            )
        elif args.command == "audit-history":
            result = audit_history(
                args.repo,
                runtime_root=args.runtime_root,
                policy_version=args.policy_version,
            )
        else:
            manifest = _load_json(args.manifest, "manifest") if args.manifest else None
            result = repair_history(
                args.repo,
                manifest=manifest,
                apply=args.apply,
                runtime_root=args.runtime_root,
                classifications=set(args.classification or []) or None,
                limit=args.limit,
            )
    except (
        BackflowError,
        FormalProofError,
        capture_ledger.CaptureError,
        review.ReviewLoopError,
        OSError,
    ) as exc:
        print(
            json.dumps({"status": "FAILED", "error": str(exc)}, ensure_ascii=False),
            file=sys.stderr,
        )
        return 2
    print(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
