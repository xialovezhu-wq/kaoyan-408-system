#!/usr/bin/env python3
"""Terminal owner for saving-contract-v2 morning campaign items.

The owner keeps exact current-item turns in a private draft, delegates the one
durable Capture to ``study_episode_capture_408``, reopens that proof, and only
then advances the campaign.  It does not write review, FSRS, formal knowledge,
personal summaries, publication requests, or background jobs.
"""
from __future__ import annotations

import hashlib
import json
import mimetypes
import os
import tempfile
from functools import partial
from pathlib import Path
from typing import Any, Callable, Mapping

import morning_campaign_answer_408 as answer
import morning_campaign_runtime_408 as runtime
import study_episode_capture_408 as episode_capture

import runtime_paths_408 as _paths


APPEND_INPUT_SCHEMA = "morning-campaign-episode-append-input-v2"
TERMINAL_INPUT_SCHEMA = "morning-campaign-episode-terminal-input-v2"
PUBLIC_SCHEMA = "morning-campaign-episode-terminal-response-v2"
DEFAULT_PACKAGE_ROOT = _paths.package_root()
DEFAULT_DRAFT_ROOT = DEFAULT_PACKAGE_ROOT / ".campaign-episode-drafts-v2"
TERMINAL_STATES = {
    "correct", "screened_correct", "prompted_correct", "wrong", "unresolved"
}
REASONING_RESULTS = {"sound", "partial", "diverged", "not_observed"}
PROMPT_DEPENDENCIES = {"none", "minimal", "progressive", "full", "not_observed"}
RESPONSES = {"A", "B", "C", "D", "BLANK", "UNCERTAIN"}
NAVIGATION_POLICIES = {"publish_successor", "hold_after_capture"}


class CampaignTerminalCaptureError(RuntimeError):
    """The current item remains open and recoverable."""


def _bytes(value: object) -> bytes:
    return (json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2) + "\n").encode("utf-8")


def _digest(value: object) -> str:
    return hashlib.sha256(
        json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()


def _atomic(path: Path, value: object) -> None:
    raw = _bytes(value)
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    descriptor, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        os.fchmod(descriptor, 0o600)
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(raw)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def _read(path: Path, label: str) -> dict[str, Any]:
    if path.is_symlink() or not path.is_file():
        raise CampaignTerminalCaptureError(f"{label}_missing")
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise CampaignTerminalCaptureError(f"{label}_invalid") from exc
    if not isinstance(value, dict):
        raise CampaignTerminalCaptureError(f"{label}_invalid")
    return value


def _turns(value: object) -> list[dict[str, Any]]:
    if not isinstance(value, list) or len(value) > 4096:
        raise CampaignTerminalCaptureError("turns_invalid")
    rows: list[dict[str, Any]] = []
    for index, raw in enumerate(value):
        if not isinstance(raw, dict) or set(raw) - {"role", "kind", "text", "observed_at"}:
            raise CampaignTerminalCaptureError(f"turn_{index}_invalid")
        role = str(raw.get("role") or "")
        if role == "learner":
            role = "user"
        text = raw.get("text")
        kind = raw.get("kind", "utterance")
        observed_at = raw.get("observed_at")
        if (
            role not in {"user", "assistant"}
            or not isinstance(kind, str)
            or not kind
            or not isinstance(text, str)
            or (not text and kind != 'image_attachment')
            or (observed_at is not None and not isinstance(observed_at, str))
        ):
            raise CampaignTerminalCaptureError(f"turn_{index}_invalid")
        rows.append({"role": role, "kind": kind, "text": text, "observed_at": observed_at})
    return rows


def _open_display(path: str | Path, sha256: str) -> tuple[dict[str, Any], str]:
    _path, display, digest = answer.reopen_campaign_display_receipt(path, sha256)
    return display, digest


def _pending(runtime_root: Path, display: Mapping[str, Any]) -> tuple[dict[str, Any], dict[str, Any]]:
    _run_dir, _run, state, item = runtime.reopen_verified_frontier(
        runtime_root, str(display["run_id"])
    )
    if state.get("status") != "episode_pending_capture" or not isinstance(item, dict):
        raise CampaignTerminalCaptureError("episode_capture_not_pending")
    pending = state.get("pending_episode")
    if not isinstance(pending, dict) or pending.get("item_id") != display.get("item_id"):
        raise CampaignTerminalCaptureError("pending_episode_binding_drift")
    for key, expected in {
        "item_id": item.get("item_id"),
        "true_item_id": item.get("true_item_id"),
        "variant_id": item.get("variant_id"),
        "surface_kind": item.get("surface_kind"),
        "campaign_item_commitment_sha256": item.get("campaign_item_commitment_sha256"),
    }.items():
        if display.get(key) != expected:
            raise CampaignTerminalCaptureError(f"display_frontier_drift:{key}")
    payload = pending.get("payload")
    if not isinstance(payload, dict):
        raise CampaignTerminalCaptureError("pending_episode_payload_missing")
    return state, payload


def _completed_observation(
    runtime_root: Path,
    display: Mapping[str, Any],
    *,
    terminal_state: str,
    final_response: str | None,
    reasoning_result: str,
    prompt_dependency: str,
) -> dict[str, Any] | None:
    """Recover a v2 terminal after Capture/runtime commit but before display."""

    run_dir = runtime.runtime_run_directory(runtime_root, str(display["run_id"]))
    observed: dict[str, Any] | None = None
    finalized = False
    for event in runtime._read_events(run_dir):
        payload = event.get("payload")
        if not isinstance(payload, dict) or payload.get("item_id") != display.get("item_id"):
            continue
        if event.get("event_type") == "campaign_response_observed":
            observed = payload
        elif event.get("event_type") == "campaign_episode_finalized":
            if (
                payload.get("terminal_state") != terminal_state
                or payload.get("final_response") != final_response
                or payload.get("reasoning_result") != reasoning_result
                or payload.get("prompt_dependency") != prompt_dependency
            ):
                raise CampaignTerminalCaptureError("completed_episode_terminal_drift")
            finalized = True
    return observed if finalized else None


def _state_path(draft_root: Path, display: Mapping[str, Any], display_sha: str) -> Path:
    identity = _digest([display["run_id"], display["item_id"], display_sha])
    return draft_root / "states" / f"{identity}.json"


def _draft(
    *,
    draft_root: Path,
    display: Mapping[str, Any],
    display_sha: str,
) -> tuple[Path, dict[str, Any]]:
    path = _state_path(draft_root, display, display_sha)
    if path.exists():
        state = _read(path, "episode_draft")
    else:
        state = {
            "schema": "morning-campaign-episode-draft-v2",
            "run_id": display["run_id"],
            "item_id": display["item_id"],
            "display_receipt_sha256": display_sha,
            "turns": [],
            "turn_count": 0,
            "trace_sha256": _digest([]),
            "formal_write_count": 0,
            "fsrs_write_count": 0,
            "personal_summary_write_count": 0,
        }
    if (
        state.get("run_id") != display.get("run_id")
        or state.get("item_id") != display.get("item_id")
        or state.get("display_receipt_sha256") != display_sha
        or state.get("turn_count") != len(state.get("turns") or [])
        or state.get("trace_sha256") != _digest(state.get("turns") or [])
    ):
        raise CampaignTerminalCaptureError("episode_draft_drift")
    return path, state


def append_turns(
    raw: Mapping[str, Any],
    *,
    runtime_root: str | Path,
    draft_root: str | Path = DEFAULT_DRAFT_ROOT,
) -> dict[str, Any]:
    required = {
        "schema", "display_receipt_path", "display_receipt_sha256",
        "expected_prior_turn_count", "expected_prior_trace_sha256", "new_turns",
        "idempotency_key",
    }
    if set(raw) != required or raw.get("schema") != APPEND_INPUT_SCHEMA:
        raise CampaignTerminalCaptureError("episode_append_input_invalid")
    display, display_sha = _open_display(raw["display_receipt_path"], raw["display_receipt_sha256"])
    runtime_path = Path(runtime_root).expanduser().resolve()
    saving = runtime.saving_contract_for_run(runtime_path, str(display["run_id"]))
    if (
        not saving["episode_capture_every_item"]
        or display.get("episode_capture_every_item") is not True
        or display.get("saving_contract_sha256")
        != saving["saving_contract_sha256"]
    ):
        raise CampaignTerminalCaptureError("display_saving_contract_drift")
    try:
        _pending(runtime_path, display)
    except CampaignTerminalCaptureError:
        # Concept discussion before the first choice is evidence, not a response.
        _, _, frontier, current = runtime.reopen_verified_frontier(runtime_path, str(display['run_id']))
        if (frontier.get('status') != 'active' or not current
                or current.get('item_id') != display.get('item_id')):
            raise
    path, state = _draft(
        draft_root=Path(draft_root).expanduser().resolve(),
        display=display,
        display_sha=display_sha,
    )
    additions = _turns(raw.get("new_turns"))
    if (
        raw.get("expected_prior_turn_count") != state["turn_count"]
        or raw.get("expected_prior_trace_sha256") != state["trace_sha256"]
    ):
        prior_count = raw.get("expected_prior_turn_count")
        if not (
            isinstance(prior_count, int)
            and 0 <= prior_count <= state["turn_count"]
            and raw.get("expected_prior_trace_sha256")
            == _digest(state["turns"][:prior_count])
            and state["turns"][prior_count:] == additions
        ):
            raise CampaignTerminalCaptureError("episode_draft_cursor_stale")
        return {
            "schema": PUBLIC_SCHEMA,
            "status": "already_buffered",
            "run_id": display["run_id"],
            "item_id": display["item_id"],
            "episode_id": f"{display['run_id']}:{display['item_id']}",
            "turn_count": state["turn_count"],
            "trace_sha256": state["trace_sha256"],
            "capture_verified": False,
            "frontier_advanced": False,
            "formal_write_count": 0,
            "fsrs_write_count": 0,
            "personal_summary_write_count": 0,
            "background_processing": "none",
        }
    turns = [*state["turns"], *additions]
    next_state = {
        **state,
        "turns": turns,
        "turn_count": len(turns),
        "trace_sha256": _digest(turns),
    }
    _atomic(path, next_state)
    return {
        "schema": PUBLIC_SCHEMA,
        "status": "buffered",
        "run_id": display["run_id"],
        "item_id": display["item_id"],
        "episode_id": f"{display['run_id']}:{display['item_id']}",
        "turn_count": next_state["turn_count"],
        "trace_sha256": next_state["trace_sha256"],
        "capture_verified": False,
        "frontier_advanced": False,
        "formal_write_count": 0,
        "fsrs_write_count": 0,
        "personal_summary_write_count": 0,
        "background_processing": "none",
    }


def sync_turns(
    *,
    display_receipt_path: str | Path,
    display_receipt_sha256: str,
    complete_turns: object,
    idempotency_key: str,
    runtime_root: str | Path,
    draft_root: str | Path = DEFAULT_DRAFT_ROOT,
) -> dict[str, Any]:
    """Append only the unseen suffix of one caller-held complete conversation."""

    desired = _turns(complete_turns)
    display, display_sha = _open_display(
        display_receipt_path, display_receipt_sha256
    )
    runtime_path = Path(runtime_root).expanduser().resolve()
    saving = runtime.saving_contract_for_run(runtime_path, str(display["run_id"]))
    if (
        not saving["episode_capture_every_item"]
        or display.get("episode_capture_every_item") is not True
        or display.get("saving_contract_sha256")
        != saving["saving_contract_sha256"]
    ):
        raise CampaignTerminalCaptureError("display_saving_contract_drift")
    _pending(runtime_path, display)
    _path, state = _draft(
        draft_root=Path(draft_root).expanduser().resolve(),
        display=display,
        display_sha=display_sha,
    )
    prior = state["turns"]
    if len(desired) < len(prior) or desired[: len(prior)] != prior:
        raise CampaignTerminalCaptureError("complete_turns_prefix_drift")
    if len(desired) == len(prior):
        return {
            "schema": PUBLIC_SCHEMA,
            "status": "already_buffered",
            "run_id": display["run_id"],
            "item_id": display["item_id"],
            "episode_id": f"{display['run_id']}:{display['item_id']}",
            "turn_count": state["turn_count"],
            "trace_sha256": state["trace_sha256"],
            "capture_verified": False,
            "frontier_advanced": False,
            "formal_write_count": 0,
            "fsrs_write_count": 0,
            "personal_summary_write_count": 0,
            "background_processing": "none",
        }
    return append_turns(
        {
            "schema": APPEND_INPUT_SCHEMA,
            "display_receipt_path": str(display_receipt_path),
            "display_receipt_sha256": display_sha,
            "expected_prior_turn_count": state["turn_count"],
            "expected_prior_trace_sha256": state["trace_sha256"],
            "new_turns": desired[len(prior) :],
            "idempotency_key": idempotency_key,
        },
        runtime_root=runtime_root,
        draft_root=draft_root,
    )


def _attachments(display: Mapping[str, Any], supplied: object) -> tuple[list[dict[str, Any]], list[str]]:
    rows: list[dict[str, Any]] = []
    missing: list[str] = []
    for raw in display.get("question_assets") or []:
        path = Path(str(raw.get("path") or "")).expanduser()
        if (
            not path.is_file()
            or path.is_symlink()
            or runtime._sha256_file(path) != raw.get("sha256")
            or path.stat().st_size != raw.get("size_bytes")
        ):
            raise CampaignTerminalCaptureError("question_asset_drift")
        rows.append({
            "path": str(path.resolve()), "role": "question_image",
            "mime_type": mimetypes.guess_type(path.name)[0] or "application/octet-stream",
            "label": str(raw.get("page_locator") or "question image"),
        })
    terminal_rows = display.get("terminal_evidence_assets")
    if not isinstance(terminal_rows, list) or not terminal_rows:
        # Historical receipts can still be completed from their verified grading capsule.
        grading_path = display.get("grading_capsule_path")
        if grading_path:
            terminal_rows = [{
                "path": grading_path,
                "role": "other_attachment",
                "label": "verified grading and explanation capsule",
            }]
        else:
            terminal_rows = []
            missing.append("terminal_reference_material")
    for raw in terminal_rows:
        path = Path(str(raw.get("path") or "")).expanduser()
        if (
            not path.is_file()
            or path.is_symlink()
            or (raw.get("sha256") and runtime._sha256_file(path) != raw.get("sha256"))
            or (raw.get("size_bytes") and path.stat().st_size != raw.get("size_bytes"))
        ):
            raise CampaignTerminalCaptureError("terminal_evidence_asset_drift")
        rows.append({
            "path": str(path.resolve()),
            "role": str(raw.get("role") or "other_attachment"),
            "label": str(raw.get("label") or "terminal evidence"),
        })
    if not isinstance(supplied, list):
        raise CampaignTerminalCaptureError("attachments_invalid")
    for raw in supplied:
        if not isinstance(raw, dict):
            raise CampaignTerminalCaptureError("attachment_invalid")
        rows.append(dict(raw))
    return rows, missing


def _terminal_state(
    display: Mapping[str, Any],
    pending: Mapping[str, Any],
    requested: str,
    final_response: str | None,
    reasoning_result: str,
    prompt_dependency: str,
) -> None:
    if requested == "unresolved":
        if final_response is not None:
            raise CampaignTerminalCaptureError("unresolved_final_response_must_be_null")
        return
    if final_response not in RESPONSES:
        raise CampaignTerminalCaptureError("final_response_invalid")
    correct = answer.correct_letter_for_display(dict(display))
    is_correct = final_response == correct
    if requested in {"correct", "screened_correct", "prompted_correct"} and not is_correct:
        raise CampaignTerminalCaptureError("terminal_correct_choice_mismatch")
    if requested == "wrong" and is_correct and reasoning_result not in {
        "partial", "diverged"
    }:
        raise CampaignTerminalCaptureError("terminal_wrong_choice_mismatch")
    if requested in {"correct", "screened_correct"} and prompt_dependency != "none":
        raise CampaignTerminalCaptureError("independent_terminal_has_prompt_dependency")
    if (requested in {"correct", "screened_correct"}
            and pending.get('initial_prompt_dependency') in {'minimal', 'progressive', 'full'}):
        raise CampaignTerminalCaptureError("independent_terminal_erases_observed_hint")
    if requested == "prompted_correct" and prompt_dependency == "none":
        raise CampaignTerminalCaptureError("prompted_terminal_missing_prompt_dependency")
    if requested == "screened_correct" and pending.get("first_result") != "correct":
        raise CampaignTerminalCaptureError("screened_terminal_first_result_mismatch")
    if requested in {"correct", "screened_correct", "prompted_correct"} and reasoning_result != "sound":
        raise CampaignTerminalCaptureError("correct_terminal_requires_sound_reasoning")


def finish_item(
    raw: Mapping[str, Any],
    *,
    repo_root: str | Path,
    runtime_root: str | Path,
    package_root: str | Path = DEFAULT_PACKAGE_ROOT,
    draft_root: str | Path = DEFAULT_DRAFT_ROOT,
    display_publisher: Callable[..., dict[str, Any]] | None = None,
) -> dict[str, Any]:
    required = {
        "schema", "display_receipt_path", "display_receipt_sha256",
        "expected_prior_turn_count", "expected_prior_trace_sha256", "new_turns",
        "terminal_state", "final_response", "reasoning_result", "prompt_dependency",
        "attachments", "missing_fields", "navigation_policy", "idempotency_key",
    }
    optional = {"frozen_feedback", "event_time"}
    if (
        set(raw) - required - optional
        or required - set(raw)
        or raw.get("schema") != TERMINAL_INPUT_SCHEMA
    ):
        raise CampaignTerminalCaptureError("episode_terminal_input_invalid")
    terminal_state = str(raw.get("terminal_state") or "")
    reasoning = str(raw.get("reasoning_result") or "")
    prompt = str(raw.get("prompt_dependency") or "")
    navigation_policy = str(raw.get("navigation_policy") or "")
    final_response = raw.get("final_response")
    if (
        terminal_state not in TERMINAL_STATES
        or reasoning not in REASONING_RESULTS
        or prompt not in PROMPT_DEPENDENCIES
        or navigation_policy not in NAVIGATION_POLICIES
    ):
        raise CampaignTerminalCaptureError("episode_terminal_semantics_invalid")
    if final_response is not None:
        final_response = str(final_response).upper()
    display, display_sha = _open_display(raw["display_receipt_path"], raw["display_receipt_sha256"])
    runtime_path = Path(runtime_root).expanduser().resolve()
    saving = runtime.saving_contract_for_run(
        runtime_path, str(display["run_id"])
    )
    if (
        not saving["episode_capture_every_item"]
        or display.get("episode_capture_every_item") is not True
        or display.get("saving_mode") != "episode_capture_every_item"
        or display.get("saving_contract_sha256")
        != saving["saving_contract_sha256"]
    ):
        raise CampaignTerminalCaptureError("display_saving_contract_drift")
    already_finalized = False
    try:
        _state, pending = _pending(runtime_path, display)
    except CampaignTerminalCaptureError:
        pending = _completed_observation(
            runtime_path,
            display,
            terminal_state=terminal_state,
            final_response=final_response,
            reasoning_result=reasoning,
            prompt_dependency=prompt,
        )
        already_finalized = pending is not None
        if pending is None:
            if terminal_state != "unresolved" or not saving["episode_capture_every_item"]:
                raise
            current_status = runtime.status_run(
                runtime_path, str(display["run_id"])
            ).get("status")
            if current_status == "paused":
                runtime.resume_run(
                    runtime_path,
                    str(display["run_id"]),
                    idempotency_key=f"episode-close-resume:{raw['idempotency_key']}",
                    event_time=raw.get("event_time"),
                )
            runtime.begin_unresolved_episode(
                runtime_path,
                str(display["run_id"]),
                item_id=str(display["item_id"]),
                observed_date=str(display.get("displayed_date") or display["campaign_date"]),
                idempotency_key=f"episode-unresolved:{raw['idempotency_key']}",
                event_time=raw.get("event_time"),
            )
            _state, pending = _pending(runtime_path, display)
    path, draft = _draft(
        draft_root=Path(draft_root).expanduser().resolve(), display=display, display_sha=display_sha
    )
    additions = _turns(raw.get("new_turns"))
    if (
        raw.get("expected_prior_turn_count") != draft["turn_count"]
        or raw.get("expected_prior_trace_sha256") != draft["trace_sha256"]
    ):
        prior_count = raw.get("expected_prior_turn_count")
        if not (
            isinstance(prior_count, int)
            and 0 <= prior_count <= draft["turn_count"]
            and raw.get("expected_prior_trace_sha256")
            == _digest(draft["turns"][:prior_count])
            and draft["turns"][prior_count:] == additions
            and draft.get("capture_verified") is True
        ):
            raise CampaignTerminalCaptureError("episode_draft_cursor_stale")
        turns = list(draft["turns"])
    else:
        turns = [*draft["turns"], *additions]
    if not turns:
        raise CampaignTerminalCaptureError("complete_episode_turns_required")
    if not already_finalized:
        _terminal_state(display, pending, terminal_state, final_response, reasoning, prompt)
    attachments, inferred_missing = _attachments(display, raw.get("attachments"))
    explicit_missing = raw.get("missing_fields")
    if not isinstance(explicit_missing, list) or not all(isinstance(row, str) for row in explicit_missing):
        raise CampaignTerminalCaptureError("missing_fields_invalid")
    provenance = display.get("question_provenance") or {}
    execution_kind = (
        "concept_supplement"
        if display.get("surface_kind") == "verified_variant"
        or display.get("variant_id") is not None
        else "original_review"
    )
    payload = {
        "schema": episode_capture.SCHEMA,
        "episode_id": pending["episode_id"],
        "execution_kind": execution_kind,
        "study_date": str(display.get("displayed_date") or pending.get("observed_date")),
        "source": {
            "source_id": display["true_item_id"],
            "session_id": display["run_id"],
            "item_id": display["item_id"],
            "year": provenance.get("exam_year"),
            "subject": "408",
            "question_type": "multiple_choice",
            "locator": "current-question-turn://sha256/" + display_sha,
            "displayed_at": display.get("displayed_at"),
            "question": display["learner_surface"]["stem"],
            "options": display["learner_surface"]["options"],
            "surface_kind": display["surface_kind"],
            "acceptance_kind": display.get("acceptance_kind"),
            "question_provenance": provenance,
            "formal_node_id": display.get("formal_node_id"),
            "variant_id": display.get("variant_id"),
            "origin_lane": (
                "campaign_variant"
                if execution_kind == "concept_supplement"
                else "campaign_original"
            ),
        },
        "turns": turns,
        "attachments": attachments,
        "observed_outcome": {
            "first_response": pending.get("first_response"),
            "first_result": {
                "correct": "correct", "incorrect": "incorrect", "partial": "blank",
                "uncertain": "uncertain", "not_observed": "not_observed",
            }[str(pending.get("first_result"))],
            "final_response": final_response,
            "terminal_state": terminal_state,
            "prompt_dependency": prompt,
            "reasoning_result": reasoning,
        },
        "authorization": {
            "policy": runtime.SAVING_CONTRACT_V2_SCHEMA,
            "per_item_capture_authorized": True,
            "formal_review_authorized": False,
            "fsrs_authorized": False,
            "personal_summary_refresh_authorized": False,
            "navigation_policy": navigation_policy,
        },
        "display_binding": {
            "run_id": display["run_id"],
            "item_id": display["item_id"],
            "display_receipt_sha256": display_sha,
            "campaign_item_commitment_sha256": display["campaign_item_commitment_sha256"],
            "saving_contract_sha256": pending["saving_contract_sha256"],
            "surface_kind": display["surface_kind"],
            "acceptance_kind": display.get("acceptance_kind"),
            "question_provenance": provenance,
            "navigation_policy": navigation_policy,
        },
        "missing_fields": sorted(set([*explicit_missing, *inferred_missing])),
        "frozen_feedback": str(raw.get("frozen_feedback") or ""),
    }
    proof = episode_capture.record_episode(
        Path(repo_root).expanduser().resolve(), payload, package_root=package_root
    )
    verified = episode_capture.verify_episode_capture(
        Path(repo_root).expanduser().resolve(), proof["receipt_ref"], package_root=package_root
    )
    finalized = runtime.finalize_episode_capture(
        runtime_path,
        str(display["run_id"]),
        repo_root=repo_root,
        item_id=str(display["item_id"]),
        terminal_state=terminal_state,
        final_response=final_response,
        reasoning_result=reasoning,
        prompt_dependency=prompt,
        capture_proof=verified,
        package_root=package_root,
        idempotency_key=f"episode-finalize:{raw['idempotency_key']}",
        event_time=raw.get("event_time"),
    )
    next_state = runtime.status_run(runtime_path, str(display["run_id"]))
    if (
        navigation_policy == "hold_after_capture"
        and next_state.get("status") == "active"
    ):
        runtime.pause_run(
            runtime_path,
            str(display["run_id"]),
            reason="capture_hold",
            idempotency_key=f"episode-hold:{raw['idempotency_key']}",
            event_time=raw.get("event_time"),
        )
        next_state = runtime.status_run(runtime_path, str(display["run_id"]))
    public = {
        "schema": PUBLIC_SCHEMA,
        "status": "episode_captured",
        "run_id": display["run_id"],
        "campaign_date": display["campaign_date"],
        "study_date": payload["study_date"],
        "item_id": display["item_id"],
        "episode_id": pending["episode_id"],
        "episode": {
            "episode_id": pending["episode_id"],
            "execution_kind": execution_kind,
            "study_date": payload["study_date"],
            "terminal_state": terminal_state,
            "capture_id": verified["capture_id"],
            "conversation_package_locator": verified[
                "conversation_package_locator"
            ],
        },
        "terminal_state": terminal_state,
        "choice_result": pending.get("first_result"),
        "rating": None,
        "capture_required": True,
        "capture_verified": True,
        "package_verified": True,
        "capture_id": verified["capture_id"],
        "capture_receipt_ref": verified["receipt_ref"],
        "capture_receipt_sha256": verified["receipt_sha256"],
        "conversation_package_locator": verified["conversation_package_locator"],
        "conversation_package_sha256": verified["conversation_package_sha256"],
        "source": payload["source"],
        "question_provenance": provenance,
        "provenance": provenance,
        "frontier_advanced": True,
        "runtime_operation": finalized.get("operation"),
        "runtime_status": finalized.get("state_status"),
        "progress": {
            "completed_items": next_state["completed_items"],
            "item_count": next_state["item_count"],
            "remaining_items": next_state["item_count"] - next_state["completed_items"],
        },
        "formal_write_count": 0,
        "fsrs_write_count": 0,
        "personal_summary_write_count": 0,
        "background_processing": "none",
    }
    publisher = display_publisher
    if publisher is None:
        from morning_campaign_display_408 import publish_current_display
        publisher = publish_current_display
    if navigation_policy == "hold_after_capture":
        public.update(
            status=(
                "complete"
                if next_state.get("status") == "completed"
                else "episode_captured_held"
            ),
            navigation_policy=navigation_policy,
            navigation_held=next_state.get("status") == "paused",
            campaign_complete=next_state.get("status") == "completed",
            runtime_status=next_state.get("status"),
        )
    else:
        public["navigation_policy"] = navigation_policy
        public = answer._publish_after_advance(
            runtime_path,
            repo_root=repo_root,
            display=dict(display),
            public=public,
            display_publisher=publisher,
            display_publisher_kwargs={},
        )
    final_draft = {
        **draft,
        "turns": turns,
        "turn_count": len(turns),
        "trace_sha256": _digest(turns),
        "terminal_receipt_ref": verified["receipt_ref"],
        "capture_verified": True,
    }
    _atomic(path, final_draft)
    return public
