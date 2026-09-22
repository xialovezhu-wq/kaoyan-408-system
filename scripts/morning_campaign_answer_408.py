#!/usr/bin/env python3
"""Answer-safe A-D bridge for the successor 408 morning campaign.

The bridge reopens one private display receipt and one hash-bound private
grading capsule, derives only the deterministic choice result, and hands a
terminal outcome to ``morning_campaign_runtime_408``.  A forced reason audit is
kept on the same campaign item until the root Sol supplies a frozen
``reasoning_result`` through :func:`finalize_reason`.

No public response produced here contains the correct option, a grading
locator, or an answer commitment.
"""

from __future__ import annotations

import argparse
import fcntl
import hashlib
import json
import os
import re
import stat
import sys
import tempfile
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Callable, Iterator

import morning_campaign_runtime_408 as campaign_runtime
from study_commit_followup_408 import study_commit_boundary


DISPLAY_SCHEMA = "morning-campaign-display-receipt-v1"
DISPLAY_ENVELOPE_SCHEMA = "current-question-turn-receipt-v1"
BRIDGE_RECEIPT_SCHEMA = "morning-campaign-answer-bridge-receipt-v1"
PUBLIC_SCHEMA = "morning-campaign-answer-response-v1"
QUESTION_PROVENANCE_SCHEMA = "morning-campaign-question-provenance-v1"

SHA_RE = re.compile(r"^[0-9a-f]{64}$")
SAFE_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:@/+\-]{0,191}$")
RUN_ID_RE = re.compile(r"^MCR-[0-9]{8}-[0-9A-F]{16}$")
CHOICES = {"A", "B", "C", "D", "BLANK", "UNCERTAIN"}
CONFIDENCE_LEVELS = {"low", "medium", "high"}
REASONING_RESULTS = {"sound", "partial", "diverged"}
PROMPT_DEPENDENCIES = {"none", "minimal", "progressive", "full"}
INITIAL_REASONING_RESULTS = REASONING_RESULTS | {"not_observed"}
MAX_JSON_BYTES = 512 * 1024


class CampaignAnswerError(RuntimeError):
    """Stable fail-closed error that never includes private answer content."""


def _canonical_bytes(value: Any) -> bytes:
    return json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")


def _pretty_bytes(value: Any) -> bytes:
    return (
        json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2) + "\n"
    ).encode("utf-8")


def _digest(value: Any) -> str:
    return hashlib.sha256(_canonical_bytes(value)).hexdigest()


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _safe_id(value: object, label: str, pattern: re.Pattern[str] = SAFE_ID_RE) -> str:
    if not isinstance(value, str) or not pattern.fullmatch(value):
        raise CampaignAnswerError(f"{label}_invalid")
    return value


def _safe_sha(value: object, label: str) -> str:
    if not isinstance(value, str) or not SHA_RE.fullmatch(value):
        raise CampaignAnswerError(f"{label}_invalid")
    return value


def _regular_json(path_value: str | Path, label: str) -> tuple[Path, dict[str, Any]]:
    try:
        unresolved = Path(path_value).expanduser()
        source_info = unresolved.lstat()
        if stat.S_ISLNK(source_info.st_mode):
            raise CampaignAnswerError(f"{label}_unsafe")
        path = unresolved.resolve(strict=True)
        info = path.lstat()
    except OSError as exc:
        raise CampaignAnswerError(f"{label}_unavailable") from exc
    if stat.S_ISLNK(info.st_mode) or not stat.S_ISREG(info.st_mode):
        raise CampaignAnswerError(f"{label}_unsafe")
    if info.st_size > MAX_JSON_BYTES:
        raise CampaignAnswerError(f"{label}_too_large")
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise CampaignAnswerError(f"{label}_invalid_json") from exc
    if not isinstance(value, dict):
        raise CampaignAnswerError(f"{label}_root_not_object")
    return path, value


def _ensure_private_file(path: Path, label: str) -> None:
    if path.stat().st_mode & 0o077:
        raise CampaignAnswerError(f"{label}_permissions_not_private")


def _ensure_dir(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True, mode=0o700)
    info = path.lstat()
    if stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode):
        raise CampaignAnswerError("bridge_directory_unsafe")
    os.chmod(path, 0o700)


def _atomic_json(path: Path, value: Any) -> None:
    payload = _pretty_bytes(value)
    _ensure_dir(path.parent)
    descriptor, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        os.fchmod(descriptor, 0o600)
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
        directory_fd = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


@contextmanager
def _bridge_lock(runtime_root: Path) -> Iterator[None]:
    _ensure_dir(runtime_root)
    path = runtime_root / ".morning-campaign-answer.lock"
    descriptor = os.open(path, os.O_RDWR | os.O_CREAT, 0o600)
    try:
        fcntl.flock(descriptor, fcntl.LOCK_EX)
        yield
    finally:
        fcntl.flock(descriptor, fcntl.LOCK_UN)
        os.close(descriptor)


def _normalize_display(
    display_receipt_path: str | Path, display_receipt_sha256: str
) -> tuple[Path, dict[str, Any], str]:
    path, raw = _regular_json(display_receipt_path, "display_receipt")
    expected = _safe_sha(display_receipt_sha256, "display_receipt_sha256")
    if _sha256_file(path) != expected:
        raise CampaignAnswerError("display_receipt_hash_drift")
    _ensure_private_file(path, "display_receipt")
    direct = raw.get("schema") == DISPLAY_SCHEMA
    enveloped = (
        raw.get("schema") == DISPLAY_ENVELOPE_SCHEMA
        and raw.get("receipt_schema") == DISPLAY_SCHEMA
    )
    if not direct and not enveloped:
        raise CampaignAnswerError("display_receipt_schema_invalid")
    if raw.get("status") not in {"display_published", "campaign_display_published"}:
        raise CampaignAnswerError("display_receipt_not_published")
    required = {
        "run_id",
        "campaign_date",
        "segment_id",
        "segment_ordinal",
        "item_offset",
        "item_id",
        "formal_node_id",
        "true_item_id",
        "variant_id",
        "surface_kind",
        "surface_sha256",
        "grading_sha256",
        "grading_capsule_path",
        "grading_capsule_sha256",
        "campaign_item_commitment_sha256",
        "formal_write_count",
    }
    if required - set(raw):
        raise CampaignAnswerError("display_receipt_required_binding_missing")
    if raw.get("formal_write_count") != 0:
        raise CampaignAnswerError("display_receipt_formal_write_invalid")
    scope = raw.get("review_scope")
    if scope is not None:
        counts = ("full_due_node_count", "representative_count", "suppressed_unanswered_node_count")
        if (not isinstance(scope, dict) or scope.get("kind") != "D_concept_representatives"
                or any(type(scope.get(key)) is not int or scope[key] < 0 for key in counts)
                or scope["full_due_node_count"] != scope["representative_count"] + scope["suppressed_unanswered_node_count"]):
            raise CampaignAnswerError("display_review_scope_invalid")
    if raw.get("answer_safe") is not True:
        raise CampaignAnswerError("display_receipt_not_answer_safe")
    if raw.get("answer_content_in_receipt", False) is not False:
        raise CampaignAnswerError("display_receipt_contains_answer_content")
    _safe_id(raw.get("run_id"), "run_id", RUN_ID_RE)
    for field in ("segment_id", "item_id", "formal_node_id", "true_item_id"):
        _safe_id(raw.get(field), field)
    if not isinstance(raw.get("segment_ordinal"), int) or raw["segment_ordinal"] < 0:
        raise CampaignAnswerError("segment_ordinal_invalid")
    if raw.get("surface_kind") not in {"canonical_original", "formal_original", "verified_variant"}:
        raise CampaignAnswerError("surface_kind_invalid")
    variant_id = raw.get("variant_id")
    if variant_id is not None:
        _safe_id(variant_id, "variant_id")
    for field in (
        "surface_sha256",
        "grading_sha256",
        "campaign_item_commitment_sha256",
    ):
        _safe_sha(raw.get(field), field)
    manifest_sha = raw.get("runtime_manifest_sha256", raw.get("manifest_sha256"))
    _safe_sha(manifest_sha, "runtime_manifest_sha256")
    display_sha = raw.get("display_content_sha256", raw.get("display_sha256"))
    _safe_sha(display_sha, "display_content_sha256")
    learner_surface = raw.get("learner_surface")
    if not isinstance(learner_surface, dict) or set(learner_surface) != {"stem", "options"}:
        raise CampaignAnswerError("display_receipt_public_surface_invalid")
    stem = learner_surface.get("stem")
    options = learner_surface.get("options")
    if (
        not isinstance(stem, str)
        or not stem.strip()
        or not isinstance(options, dict)
        or set(options) != {"A", "B", "C", "D"}
        or any(not isinstance(options[key], str) or not options[key].strip() for key in "ABCD")
    ):
        raise CampaignAnswerError("display_receipt_public_surface_invalid")
    surface_payload = (
        json.dumps(
            {"stem": stem.strip(), "options": {key: options[key].strip() for key in "ABCD"}},
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        + "\n"
    ).encode("utf-8")
    if hashlib.sha256(surface_payload).hexdigest() != display_sha:
        raise CampaignAnswerError("display_content_hash_drift")
    if "question_assets" in raw:
        assets = raw.get("question_assets")
        if not isinstance(assets, list) or len(assets) > 128:
            raise CampaignAnswerError("display_question_assets_invalid")
        seen_assets: set[tuple[str, str]] = set()
        for row in assets:
            if not isinstance(row, dict) or set(row) != {
                "ref",
                "path",
                "sha256",
                "size_bytes",
                "page_locator",
            }:
                raise CampaignAnswerError("display_question_assets_invalid")
            ref = row.get("ref")
            asset_path = row.get("path")
            size = row.get("size_bytes")
            sha = _safe_sha(row.get("sha256"), "question_asset_sha256")
            if (
                not isinstance(ref, str)
                or not ref
                or not isinstance(asset_path, str)
                or not Path(asset_path).expanduser().is_absolute()
                or not isinstance(size, int)
                or size <= 0
                or (asset_path, sha) in seen_assets
            ):
                raise CampaignAnswerError("display_question_assets_invalid")
            seen_assets.add((asset_path, sha))
        if assets:
            if (
                raw.get("question_asset_ref") != assets[0]["ref"]
                or raw.get("question_asset_sha256") != assets[0]["sha256"]
                or raw.get("surface_object_path") != assets[0]["path"]
            ):
                raise CampaignAnswerError("display_question_asset_head_drift")
        elif (
            raw.get("question_asset_ref") is not None
            or raw.get("question_asset_sha256") is not None
        ):
            raise CampaignAnswerError("display_question_asset_empty_drift")
    _safe_sha(raw.get("grading_capsule_sha256"), "grading_capsule_sha256")
    if raw.get("grading_capsule_sha256") != raw.get("grading_sha256"):
        raise CampaignAnswerError("grading_commitment_binding_drift")
    if not isinstance(raw.get("grading_capsule_path"), str) or not raw[
        "grading_capsule_path"
    ]:
        raise CampaignAnswerError("grading_capsule_path_missing")
    if "exposure_event_id" in raw or "exposure_event_sha256" in raw:
        _safe_id(raw.get("exposure_event_id"), "exposure_event_id")
        _safe_sha(raw.get("exposure_event_sha256"), "exposure_event_sha256")
    else:
        cutoff = raw.get("exposure_cutoff")
        if (
            not isinstance(cutoff, dict)
            or cutoff.get("schema") != "morning-campaign-exposure-cutoff-v1"
            or cutoff.get("review_date") != raw.get("campaign_date")
        ):
            raise CampaignAnswerError("display_receipt_exposure_binding_missing")
    return path, {**raw, "_runtime_manifest_sha256": manifest_sha}, expected


def reopen_campaign_display_receipt(
    display_receipt_path: str | Path, display_receipt_sha256: str
) -> tuple[Path, dict[str, Any], str]:
    """Public strict reopen for the managed campaign failure owner."""

    return _normalize_display(display_receipt_path, display_receipt_sha256)


def _frontier(
    runtime_root: Path,
    display: dict[str, Any],
    *,
    allow_episode_pending: bool = False,
) -> tuple[Path, dict[str, Any]]:
    run_id = str(display["run_id"])
    try:
        run_dir, run, state, item = campaign_runtime.reopen_verified_frontier(
            runtime_root, run_id
        )
    except campaign_runtime.CampaignRuntimeError as exc:
        raise CampaignAnswerError(f"runtime_frontier_unavailable:{exc}") from exc
    if item is None:
        raise CampaignAnswerError("runtime_frontier_unavailable:no current item")
    allowed_statuses = {"active"}
    if allow_episode_pending:
        allowed_statuses.add("episode_pending_capture")
    if state.get("status") not in allowed_statuses:
        raise CampaignAnswerError("runtime_frontier_not_active")
    active = state.get("active_segment") or {}
    comparisons = {
        "run_id": state.get("run_id"),
        "campaign_date": state.get("campaign_date"),
        "segment_id": active.get("segment_id"),
        "segment_ordinal": state.get("current_segment_ordinal"),
        "item_id": item.get("item_id"),
        "formal_node_id": item.get("formal_node_id"),
        "true_item_id": item.get("true_item_id"),
        "variant_id": item.get("variant_id"),
        "surface_kind": item.get("surface_kind"),
        "surface_sha256": item.get("surface_sha256"),
        "grading_sha256": item.get("grading_sha256"),
        "campaign_item_commitment_sha256": item.get(
            "campaign_item_commitment_sha256"
        ),
    }
    for field, current in comparisons.items():
        if display.get(field) != current:
            raise CampaignAnswerError(f"runtime_frontier_drift:{field}")
    if display["_runtime_manifest_sha256"] != run.get("manifest_sha256"):
        raise CampaignAnswerError("runtime_frontier_drift:manifest_sha256")
    saving = campaign_runtime.saving_contract_for_run(runtime_root, run_id)
    saving_fields_present = "episode_capture_every_item" in display
    if (
        (saving["episode_capture_every_item"] and not saving_fields_present)
        or (
            saving_fields_present
            and (
                display.get("episode_capture_every_item")
                is not saving["episode_capture_every_item"]
                or display.get("saving_mode")
                != (
                    "episode_capture_every_item"
                    if saving["episode_capture_every_item"]
                    else "legacy_canonical_review"
                )
                or (
                    saving["episode_capture_every_item"]
                    and display.get("saving_contract_sha256")
                    != saving["saving_contract_sha256"]
                )
            )
        )
    ):
        raise CampaignAnswerError("runtime_frontier_drift:saving_contract")
    if "item_offset" in display and display.get("item_offset") != state.get(
        "current_item_offset"
    ):
        raise CampaignAnswerError("runtime_frontier_drift:item_offset")
    if "segment_commitment_sha256" in display and display.get(
        "segment_commitment_sha256"
    ) != active.get("commitment_sha256"):
        raise CampaignAnswerError("runtime_frontier_drift:segment_commitment_sha256")
    if "pack_commitment_sha256" in display and display.get(
        "pack_commitment_sha256"
    ) != item.get("pack_commitment_sha256"):
        raise CampaignAnswerError("runtime_frontier_drift:pack_commitment_sha256")
    return run_dir, item


def _option_order(display: dict[str, Any]) -> dict[str, str]:
    raw = display.get("option_ids_by_letter")
    if not isinstance(raw, dict) or set(raw) != {"A", "B", "C", "D"}:
        raise CampaignAnswerError("variant_option_order_binding_missing")
    result = {letter: _safe_id(raw[letter], f"option_id_{letter}") for letter in "ABCD"}
    if len(set(result.values())) != 4:
        raise CampaignAnswerError("variant_option_order_binding_invalid")
    return result


def _grading_path(
    display: dict[str, Any], grading_capsule_path: str | Path | None
) -> str | Path:
    bound = display.get("grading_capsule_path")
    if grading_capsule_path is None:
        if not isinstance(bound, str) or not bound:
            raise CampaignAnswerError("grading_capsule_path_missing")
        return bound
    if bound is not None:
        try:
            supplied = Path(grading_capsule_path).expanduser().resolve(strict=True)
            recorded = Path(str(bound)).expanduser().resolve(strict=True)
        except OSError as exc:
            raise CampaignAnswerError("grading_capsule_path_unavailable") from exc
        if supplied != recorded:
            raise CampaignAnswerError("grading_capsule_path_binding_drift")
    return grading_capsule_path


def _correct_letter(
    display: dict[str, Any], grading_capsule_path: str | Path | None
) -> str:
    grading_capsule_path = _grading_path(display, grading_capsule_path)
    capsule_path, capsule = _regular_json(grading_capsule_path, "grading_capsule")
    _ensure_private_file(capsule_path, "grading_capsule")
    if _sha256_file(capsule_path) != display["grading_sha256"]:
        raise CampaignAnswerError("grading_commitment_drift")
    if "grading_capsule_sha256" in display and display.get(
        "grading_capsule_sha256"
    ) != display["grading_sha256"]:
        raise CampaignAnswerError("grading_commitment_binding_drift")
    if display["surface_kind"] == "canonical_original":
        if not (
            capsule.get("schema_version") == "canonical_answer_capsule_v1"
            or capsule.get("schema") == "canonical_exam_answer_capsule_v1"
        ):
            raise CampaignAnswerError("grading_capsule_schema_invalid")
        if capsule.get("parser_status", "resolved") != "resolved":
            raise CampaignAnswerError("grading_capsule_not_resolved")
        canonical_id = capsule.get("canonical_id")
        if canonical_id is not None and canonical_id != display["true_item_id"]:
            raise CampaignAnswerError("grading_capsule_identity_drift")
        correct = capsule.get("correct_option")
        if correct not in {"A", "B", "C", "D"}:
            raise CampaignAnswerError("grading_capsule_answer_invalid")
        return str(correct)
    if display["surface_kind"] == "formal_original":
        if capsule.get("schema") != "formal_original_answer_capsule_v1":
            raise CampaignAnswerError("grading_capsule_schema_invalid")
        if capsule.get("true_item_id") != display["true_item_id"]:
            raise CampaignAnswerError("grading_capsule_identity_drift")
        declared_node = capsule.get("formal_node_id")
        if declared_node is None:
            # The answer-isolated publisher binds a capsule by true_item_id and the
            # exact published surface, and writes no formal-node field. Fall back to
            # that surface binding instead of rejecting every published capsule.
            surface_sha = display.get("surface_sha256")
            if not surface_sha or capsule.get("surface_sha256") != surface_sha:
                raise CampaignAnswerError(
                    "grading_capsule_surface_binding_drift"
                )
        elif declared_node != display["formal_node_id"]:
            raise CampaignAnswerError("grading_capsule_formal_node_drift")
        correct = capsule.get("correct_option")
        if correct not in {"A", "B", "C", "D"}:
            raise CampaignAnswerError("grading_capsule_answer_invalid")
        return str(correct)
    if capsule.get("schema") != "exam_variant_answer_capsule_v1":
        raise CampaignAnswerError("grading_capsule_schema_invalid")
    correct_id = capsule.get("correct_option_id")
    order = _option_order(display)
    matches = [letter for letter, option_id in order.items() if option_id == correct_id]
    if len(matches) != 1:
        raise CampaignAnswerError("grading_capsule_answer_invalid")
    return matches[0]


def correct_letter_for_display(display: dict[str, Any]) -> str:
    """Return the private correct letter after all capsule/display bindings verify."""

    return _correct_letter(display, None)


def _normalize_inputs(
    *,
    response: str,
    confidence: str | None,
    recognized_original: bool,
    response_seconds: float | None,
) -> tuple[str, str, bool, float | None]:
    normalized_response = response.strip().upper()
    if normalized_response not in CHOICES:
        raise CampaignAnswerError("response_invalid")
    normalized_confidence = confidence or "high"
    if normalized_confidence not in CONFIDENCE_LEVELS:
        raise CampaignAnswerError("confidence_invalid")
    if not isinstance(recognized_original, bool):
        raise CampaignAnswerError("recognized_original_must_be_explicit_boolean")
    if response_seconds is not None:
        if isinstance(response_seconds, bool) or not isinstance(response_seconds, (int, float)):
            raise CampaignAnswerError("response_seconds_invalid")
        response_seconds = float(response_seconds)
        if not 0 < response_seconds <= 7200:
            raise CampaignAnswerError("response_seconds_invalid")
    return normalized_response, normalized_confidence, recognized_original, response_seconds


def _choice_result(response: str, correct_letter: str) -> str:
    if response == "BLANK":
        return "partial"
    if response == "UNCERTAIN":
        return "uncertain"
    return "correct" if response == correct_letter else "incorrect"


def _audit_reasons(
    item: dict[str, Any], *, confidence: str, recognized_original: bool
) -> list[str]:
    """Return the forced-reason triggers for one campaign item.

    2026-09-10 user ruling: only a mandatory surface audit or a non-high
    confidence still forces a reason.  stable_hash_sample, first_variant,
    recent_recurrence and recognized_original no longer do; a real reasoning
    break or hint dependence supplied by the root still holds the item.
    """

    reasons: list[str] = []
    if item.get("mandatory_audit") is True:
        reasons.append("mandatory_surface_audit")
    if confidence != "high":
        reasons.append("confidence_not_high")
    return reasons


def _initial_semantics(
    reasoning_result: str | None, prompt_dependency: str | None
) -> tuple[str, str]:
    """Keep an observed reasoning break or hint independent of the chosen letter."""
    if reasoning_result is not None and reasoning_result not in INITIAL_REASONING_RESULTS:
        raise CampaignAnswerError("reasoning_result_invalid")
    if prompt_dependency is not None and prompt_dependency not in PROMPT_DEPENDENCIES:
        raise CampaignAnswerError("prompt_dependency_invalid")
    return reasoning_result or "not_observed", prompt_dependency or "none"


def _bridge_dir(run_dir: Path) -> Path:
    return run_dir / "answer-bridge"


def _pointer_path(run_dir: Path, idempotency_key: str) -> Path:
    return _bridge_dir(run_dir) / "idempotency" / (
        hashlib.sha256(idempotency_key.encode("utf-8")).hexdigest() + ".json"
    )


def _idempotent_lookup(
    run_dir: Path, *, idempotency_key: str, request_sha256: str
) -> dict[str, Any] | None:
    pointer_path = _pointer_path(run_dir, idempotency_key)
    if not pointer_path.exists():
        receipts_dir = _bridge_dir(run_dir) / "receipts"
        if not receipts_dir.exists():
            return None
        if receipts_dir.is_symlink() or not receipts_dir.is_dir():
            raise CampaignAnswerError("bridge_receipt_directory_invalid")
        matches: list[tuple[Path, dict[str, Any], str]] = []
        for candidate in sorted(receipts_dir.glob("*.json")):
            path, receipt = _regular_json(candidate, "orphan_bridge_receipt")
            if (
                receipt.get("schema") == BRIDGE_RECEIPT_SCHEMA
                and receipt.get("idempotency_key") == idempotency_key
                and receipt.get("request_sha256") == request_sha256
            ):
                digest = _sha256_file(path)
                if path.stem != digest:
                    raise CampaignAnswerError("orphan_bridge_receipt_hash_drift")
                matches.append((path, receipt, digest))
        if not matches:
            return None
        if len(matches) != 1:
            raise CampaignAnswerError("orphan_bridge_receipt_ambiguous")
        path, receipt, digest = matches[0]
        pointer = {
            "schema": "morning-campaign-answer-idempotency-pointer-v1",
            "idempotency_key": idempotency_key,
            "request_sha256": request_sha256,
            "receipt_ref": str(
                path.resolve().relative_to(_bridge_dir(run_dir).resolve())
            ),
            "receipt_sha256": digest,
        }
        _atomic_json(pointer_path, pointer)
        response = receipt.get("public_response")
        if not isinstance(response, dict):
            raise CampaignAnswerError("bridge_public_response_missing")
        return {**response, "replayed": True}
    _, pointer = _regular_json(pointer_path, "bridge_idempotency_pointer")
    if (
        pointer.get("idempotency_key") != idempotency_key
        or pointer.get("request_sha256") != request_sha256
    ):
        raise CampaignAnswerError("idempotency_key_reused_differently")
    receipt_ref = pointer.get("receipt_ref")
    if not isinstance(receipt_ref, str):
        raise CampaignAnswerError("bridge_receipt_pointer_invalid")
    try:
        receipt_path = (_bridge_dir(run_dir) / receipt_ref).resolve(strict=True)
    except OSError:
        # Runtime finalization and display publication are independently
        # idempotent; replay them to reconstruct the missing bridge receipt.
        return None
    try:
        receipt_path.relative_to(_bridge_dir(run_dir).resolve())
    except ValueError as exc:
        raise CampaignAnswerError("bridge_receipt_pointer_escape") from exc
    _, receipt = _regular_json(receipt_path, "bridge_receipt")
    if _sha256_file(receipt_path) != pointer.get("receipt_sha256"):
        raise CampaignAnswerError("bridge_receipt_hash_drift")
    if receipt.get("schema") != BRIDGE_RECEIPT_SCHEMA:
        raise CampaignAnswerError("bridge_receipt_schema_invalid")
    response = receipt.get("public_response")
    if not isinstance(response, dict):
        raise CampaignAnswerError("bridge_public_response_missing")
    return {**response, "replayed": True}


def _commit_bridge_receipt(
    run_dir: Path,
    *,
    idempotency_key: str,
    request_sha256: str,
    operation: str,
    display_receipt_sha256: str,
    public_response: dict[str, Any],
) -> dict[str, Any]:
    receipt = {
        "schema": BRIDGE_RECEIPT_SCHEMA,
        "operation": operation,
        "idempotency_key": idempotency_key,
        "request_sha256": request_sha256,
        "display_receipt_sha256": display_receipt_sha256,
        "public_response": public_response,
        "answer_content_stored": False,
        "formal_write_count": 0,
    }
    payload = _pretty_bytes(receipt)
    receipt_sha = hashlib.sha256(payload).hexdigest()
    receipt_rel = Path("receipts") / f"{receipt_sha}.json"
    receipt_path = _bridge_dir(run_dir) / receipt_rel
    if receipt_path.exists():
        if receipt_path.read_bytes() != payload:
            raise CampaignAnswerError("bridge_receipt_collision")
    else:
        _atomic_json(receipt_path, receipt)
    pointer = {
        "schema": "morning-campaign-answer-idempotency-pointer-v1",
        "idempotency_key": idempotency_key,
        "request_sha256": request_sha256,
        "receipt_ref": str(receipt_rel),
        "receipt_sha256": receipt_sha,
    }
    pointer_path = _pointer_path(run_dir, idempotency_key)
    if pointer_path.exists():
        _, prior = _regular_json(pointer_path, "bridge_idempotency_pointer")
        if prior != pointer:
            raise CampaignAnswerError("bridge_idempotency_pointer_conflict")
    else:
        _atomic_json(pointer_path, pointer)
    return public_response


def _public_from_runtime(
    *,
    display: dict[str, Any],
    choice_result: str,
    confidence: str,
    recognized_original: bool,
    response_seconds: float | None,
    runtime_receipt: dict[str, Any],
) -> dict[str, Any]:
    payload = runtime_receipt.get("payload")
    if not isinstance(payload, dict):
        raise CampaignAnswerError("runtime_receipt_payload_missing")
    if payload.get("schema") == "morning-campaign-response-observation-v2":
        return {
            "schema": PUBLIC_SCHEMA,
            "status": "episode_pending_terminal",
            "run_id": display["run_id"],
            "campaign_date": display["campaign_date"],
            "item_id": display["item_id"],
            "episode_id": payload.get("episode_id"),
            "choice_result": choice_result,
            "confidence": confidence,
            "recognized_original": recognized_original,
            "response_seconds": response_seconds,
            "reason_required": False,
            "rating": None,
            "capture_required": True,
            "capture_verified": False,
            "frontier_advanced": False,
            "runtime_operation": runtime_receipt.get("operation"),
            "runtime_status": runtime_receipt.get("state_status"),
            "formal_write_count": 0,
            "fsrs_write_count": 0,
            "personal_summary_write_count": 0,
            "replayed": runtime_receipt.get("status") == "already_committed",
        }
    rating = payload.get("rating")
    capture_required = payload.get("capture_required") is True
    if capture_required:
        status = "failure_pending_capture"
    elif payload.get("outcome_class") == "screened_correct":
        status = "screened_correct"
    else:
        status = "reason_audited"
    public = {
        "schema": PUBLIC_SCHEMA,
        "status": status,
        "run_id": display["run_id"],
        "campaign_date": display["campaign_date"],
        "item_id": display["item_id"],
        "choice_result": choice_result,
        "confidence": confidence,
        "recognized_original": recognized_original,
        "response_seconds": response_seconds,
        "reason_required": False,
        "rating": rating,
        "capture_required": capture_required,
        "frontier_advanced": not capture_required,
        "runtime_operation": runtime_receipt.get("operation"),
        "runtime_status": runtime_receipt.get("state_status"),
        "formal_write_count": 0,
        "replayed": runtime_receipt.get("status") == "already_committed",
    }
    serialized = json.dumps(public, ensure_ascii=False, sort_keys=True)
    for forbidden in ("correct_option", "grading_sha256", "capsule", "answer_sha256"):
        if forbidden in serialized:
            raise CampaignAnswerError("public_response_answer_isolation_failed")
    return public


def _answer_safe_delivery(value: dict[str, Any]) -> dict[str, Any]:
    serialized = json.dumps(value, ensure_ascii=False, sort_keys=True)
    for forbidden in (
        "correct_option",
        "grading_sha256",
        "grading_capsule",
        "answer_capsule",
        "answer_sha256",
    ):
        if forbidden in serialized:
            raise CampaignAnswerError("next_display_answer_isolation_failed")
    return value


def _public_question_provenance(value: object) -> dict[str, Any]:
    required = {
        "schema",
        "kind",
        "label",
        "surface_kind",
        "true_item_id",
        "true_exam_id",
        "based_on_true_exam_id",
        "exam_year",
        "exam_question_number",
        "basis",
        "answer_safe",
    }
    optional = {"acceptance_kind", "independent_reviewed"}
    if not isinstance(value, dict) or set(value) - required - optional or required - set(value):
        raise CampaignAnswerError("next_display_question_provenance_invalid")
    if (
        value.get("schema") != QUESTION_PROVENANCE_SCHEMA
        or value.get("kind")
        not in {"true_exam", "generated_variant", "web_self_checked_variant", "local_original"}
        or not isinstance(value.get("label"), str)
        or not value["label"]
        or value.get("answer_safe") is not True
    ):
        raise CampaignAnswerError("next_display_question_provenance_invalid")
    return dict(value)


def _default_display_publisher(**kwargs: Any) -> dict[str, Any]:
    try:
        import morning_campaign_display_408
    except ImportError as exc:
        raise CampaignAnswerError("display_publisher_unavailable") from exc
    try:
        value = morning_campaign_display_408.publish_current_display(**kwargs)
    except Exception as exc:
        raise CampaignAnswerError(f"next_display_publish_failed:{exc}") from exc
    if not isinstance(value, dict):
        raise CampaignAnswerError("next_display_publish_result_invalid")
    return value


def _publish_after_advance(
    runtime_root: Path,
    *,
    repo_root: str | Path,
    display: dict[str, Any],
    public: dict[str, Any],
    display_publisher: Callable[..., dict[str, Any]] | None,
    display_publisher_kwargs: dict[str, Any] | None,
) -> dict[str, Any]:
    if display.get("review_scope"):
        public = {**public, "review_scope": display["review_scope"],
                  "concept_coverage_complete": False,
                  "unanswered_node_count": display["review_scope"]["suppressed_unanswered_node_count"]}
    if public.get("frontier_advanced") is not True:
        return {**public, "campaign_complete": False, "next_display": None}
    try:
        status = campaign_runtime.status_run(runtime_root, str(display["run_id"]))
    except campaign_runtime.CampaignRuntimeError as exc:
        raise CampaignAnswerError(f"runtime_status_unavailable:{exc}") from exc
    if status.get("status") == "completed":
        return {
            **public,
            "status": "complete",
            "campaign_complete": True,
            "next_display": None,
            **({"concept_coverage_complete": True,
                "all_due_nodes_answered": public["unanswered_node_count"] == 0,
                "completion_message": f"今日概念覆盖已完成；另有 {public['unanswered_node_count']} 个未答节点保留后续复习。"}
               if display.get("review_scope") else {}),
        }
    if status.get("status") != "active":
        raise CampaignAnswerError("advanced_runtime_not_publishable")
    publisher = display_publisher or _default_display_publisher
    kwargs = dict(display_publisher_kwargs or {})
    kwargs.setdefault("repo", repo_root)
    kwargs.setdefault("review_date", display["campaign_date"])
    kwargs.setdefault("run_id", display["run_id"])
    kwargs.setdefault("runtime_root", runtime_root)
    try:
        next_display = publisher(**kwargs)
    except CampaignAnswerError:
        raise
    except Exception as exc:
        raise CampaignAnswerError(f"next_display_publish_failed:{exc}") from exc
    if not isinstance(next_display, dict):
        raise CampaignAnswerError("next_display_publish_result_invalid")
    if (
        next_display.get("schema")
        not in {"morning-campaign-display-v1", "morning-campaign-display-public-v1"}
        or next_display.get("status") not in {"display_published", "already_published"}
        or next_display.get("run_id") != display.get("run_id")
        or next_display.get("item_id") != status.get("current_item_id")
        or next_display.get("formal_write_count") != 0
        or not isinstance(next_display.get("display_receipt_locator"), str)
    ):
        raise CampaignAnswerError("next_display_identity_invalid")
    _safe_sha(
        next_display.get("display_receipt_sha256"),
        "next_display_receipt_sha256",
    )
    surface = next_display.get("learner_surface")
    if not isinstance(surface, dict) or not isinstance(surface.get("stem"), str):
        raise CampaignAnswerError("next_display_surface_invalid")
    options = surface.get("options")
    if not (
        isinstance(options, dict)
        and set(options) == {"A", "B", "C", "D"}
        or isinstance(options, list)
        and len(options) == 4
    ):
        raise CampaignAnswerError("next_display_surface_invalid")
    provenance = _public_question_provenance(
        next_display.get("question_provenance")
    )
    if display_publisher is None:
        try:
            import morning_campaign_display_408

            reopened = morning_campaign_display_408.read_display_receipt(
                str(next_display["display_receipt_locator"]),
                private_root=kwargs.get("private_root"),
            )
        except Exception as exc:
            raise CampaignAnswerError(
                f"next_display_receipt_reopen_failed:{exc}"
            ) from exc
        if (
            reopened.get("run_id") != display.get("run_id")
            or reopened.get("item_id") != status.get("current_item_id")
            or reopened.get("question_provenance") != provenance
            or str(next_display["display_receipt_locator"]).rsplit("/", 1)[-1]
            != next_display.get("display_receipt_sha256")
        ):
            raise CampaignAnswerError("next_display_receipt_binding_drift")
    return {
        **public,
        "campaign_complete": False,
        "next_display": _answer_safe_delivery(next_display),
    }


@study_commit_boundary
def answer_choice(
    runtime_root: str | Path,
    *,
    repo_root: str | Path,
    display_receipt_path: str | Path,
    display_receipt_sha256: str,
    grading_capsule_path: str | Path | None = None,
    response: str,
    recognized_original: bool,
    response_seconds: float | None,
    idempotency_key: str,
    confidence: str | None = None,
    reasoning_result: str | None = None,
    prompt_dependency: str | None = None,
    event_time: str | None = None,
    display_publisher: Callable[..., dict[str, Any]] | None = None,
    display_publisher_kwargs: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Grade one A-D/BLANK/UNCERTAIN response without exposing the key.

    ``confidence`` defaults to ``high``.  ``recognized_original`` and
    ``response_seconds`` intentionally have no defaults: callers must preserve
    whether recognition and timing were actually observed.
    """

    root = Path(runtime_root).expanduser().resolve()
    reasoning, dependency = _initial_semantics(reasoning_result, prompt_dependency)
    choice, confidence_value, recognized, seconds = _normalize_inputs(
        response=response,
        confidence=confidence,
        recognized_original=recognized_original,
        response_seconds=response_seconds,
    )
    key = _safe_id(idempotency_key, "idempotency_key")
    _display_path, display, display_sha = _normalize_display(
        display_receipt_path, display_receipt_sha256
    )
    run_dir = campaign_runtime.runtime_run_directory(
        root, str(display["run_id"])
    )
    saving = campaign_runtime.saving_contract_for_run(
        root, str(display["run_id"])
    )
    request = {
        "operation": "answer_choice",
        "display_receipt_sha256": display_sha,
        "response": choice,
        "confidence": confidence_value,
        "recognized_original": recognized,
        "response_seconds": seconds,
    }
    # Omitted fields preserve byte identity of historical choice-only requests.
    # Explicit current evidence is part of the immutable request, never a UI hint.
    if reasoning != "not_observed":
        request["reasoning_result"] = reasoning
    if dependency != "none":
        request["prompt_dependency"] = dependency
    request_sha = _digest(request)
    with _bridge_lock(root):
        repeated = _idempotent_lookup(
            run_dir, idempotency_key=key, request_sha256=request_sha
        )
        if repeated is not None:
            return repeated
        run_dir, item = _frontier(root, display)
        if recognized and display["surface_kind"] not in {
            "canonical_original",
            "formal_original",
        }:
            raise CampaignAnswerError("recognized_original_requires_original_surface")
        correct = _correct_letter(display, grading_capsule_path)
        result = _choice_result(choice, correct)
        audit_reasons = _audit_reasons(
            item, confidence=confidence_value, recognized_original=recognized
        )
        if saving["episode_capture_every_item"]:
            audit_reasons = [
                reason for reason in audit_reasons
                if reason != "mandatory_surface_audit"
            ]
        observed_reasoning = reasoning != "not_observed"
        known_failure = reasoning in {"partial", "diverged"} or dependency != "none"
        if result == "correct" and audit_reasons and not observed_reasoning and not known_failure:
            if saving["episode_capture_every_item"]:
                try:
                    runtime_receipt = campaign_runtime.record_outcome(
                        root,
                        str(display["run_id"]),
                        repo_root=repo_root,
                        item_id=str(display["item_id"]),
                        observed_date=str(
                            display.get("displayed_date") or display["campaign_date"]
                        ),
                        choice_result=result,
                        reasoning_result=reasoning,
                        confidence=confidence_value,
                        prompt_dependency=dependency,
                        audit_performed=False,
                        recognized_original=recognized,
                        response_seconds=seconds,
                        response_token=choice,
                        idempotency_key=f"answer-bridge:{key}",
                        event_time=event_time,
                    )
                except campaign_runtime.CampaignRuntimeError as exc:
                    raise CampaignAnswerError(
                        f"runtime_outcome_rejected:{exc}"
                    ) from exc
            public = {
                "schema": PUBLIC_SCHEMA,
                "status": "reason_required",
                "run_id": display["run_id"],
                "campaign_date": display["campaign_date"],
                "item_id": display["item_id"],
                "choice_result": "correct",
                "confidence": confidence_value,
                "recognized_original": recognized,
                "response_seconds": seconds,
                "reason_required": True,
                "reason_requirement": audit_reasons,
                "rating": None,
                "capture_required": saving["episode_capture_every_item"],
                "capture_verified": False,
                "frontier_advanced": False,
                "formal_write_count": 0,
                "fsrs_write_count": 0,
                "personal_summary_write_count": 0,
                "replayed": False,
            }
            if saving["episode_capture_every_item"]:
                public.update(
                    episode_id=(runtime_receipt.get("payload") or {}).get("episode_id"),
                    runtime_operation=runtime_receipt.get("operation"),
                    runtime_status=runtime_receipt.get("state_status"),
                )
            return _commit_bridge_receipt(
                run_dir,
                idempotency_key=key,
                request_sha256=request_sha,
                operation="answer_choice",
                display_receipt_sha256=display_sha,
                public_response=public,
            )
        try:
            runtime_receipt = campaign_runtime.record_outcome(
                root,
                str(display["run_id"]),
                repo_root=repo_root,
                item_id=str(display["item_id"]),
                observed_date=str(
                    display.get("displayed_date") or display["campaign_date"]
                ),
                choice_result=result,
                reasoning_result=reasoning,
                confidence=confidence_value,
                prompt_dependency=dependency,
                audit_performed=observed_reasoning,
                recognized_original=recognized,
                response_seconds=seconds,
                response_token=choice,
                idempotency_key=f"answer-bridge:{key}",
                event_time=event_time,
            )
        except campaign_runtime.CampaignRuntimeError as exc:
            raise CampaignAnswerError(f"runtime_outcome_rejected:{exc}") from exc
        public = _public_from_runtime(
            display=display,
            choice_result=result,
            confidence=confidence_value,
            recognized_original=recognized,
            response_seconds=seconds,
            runtime_receipt=runtime_receipt,
        )
        public = _publish_after_advance(
            root,
            repo_root=repo_root,
            display=display,
            public=public,
            display_publisher=display_publisher,
            display_publisher_kwargs=display_publisher_kwargs,
        )
        return _commit_bridge_receipt(
            run_dir,
            idempotency_key=key,
            request_sha256=request_sha,
            operation="answer_choice",
            display_receipt_sha256=display_sha,
            public_response=public,
        )


@study_commit_boundary
def finalize_reason(
    runtime_root: str | Path,
    *,
    repo_root: str | Path,
    display_receipt_path: str | Path,
    display_receipt_sha256: str,
    grading_capsule_path: str | Path | None = None,
    response: str,
    recognized_original: bool,
    response_seconds: float | None,
    choice_idempotency_key: str,
    reasoning_result: str,
    prompt_dependency: str,
    idempotency_key: str,
    confidence: str | None = None,
    event_time: str | None = None,
    display_publisher: Callable[..., dict[str, Any]] | None = None,
    display_publisher_kwargs: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Commit a forced audit after root Sol freezes the reasoning result."""

    if reasoning_result not in REASONING_RESULTS:
        raise CampaignAnswerError("reasoning_result_invalid")
    if prompt_dependency not in PROMPT_DEPENDENCIES:
        raise CampaignAnswerError("prompt_dependency_invalid")
    root = Path(runtime_root).expanduser().resolve()
    choice, confidence_value, recognized, seconds = _normalize_inputs(
        response=response,
        confidence=confidence,
        recognized_original=recognized_original,
        response_seconds=response_seconds,
    )
    key = _safe_id(idempotency_key, "idempotency_key")
    choice_key = _safe_id(choice_idempotency_key, "choice_idempotency_key")
    _display_path, display, display_sha = _normalize_display(
        display_receipt_path, display_receipt_sha256
    )
    run_dir = campaign_runtime.runtime_run_directory(
        root, str(display["run_id"])
    )
    request = {
        "operation": "finalize_reason",
        "display_receipt_sha256": display_sha,
        "response": choice,
        "confidence": confidence_value,
        "recognized_original": recognized,
        "response_seconds": seconds,
        "choice_idempotency_key": choice_key,
        "reasoning_result": reasoning_result,
        "prompt_dependency": prompt_dependency,
    }
    request_sha = _digest(request)
    with _bridge_lock(root):
        repeated = _idempotent_lookup(
            run_dir, idempotency_key=key, request_sha256=request_sha
        )
        if repeated is not None:
            return repeated
        choice_request = {
            "operation": "answer_choice",
            "display_receipt_sha256": display_sha,
            "response": choice,
            "confidence": confidence_value,
            "recognized_original": recognized,
            "response_seconds": seconds,
        }
        prior = _idempotent_lookup(
            run_dir,
            idempotency_key=choice_key,
            request_sha256=_digest(choice_request),
        )
        if prior is None or prior.get("status") != "reason_required":
            raise CampaignAnswerError("reason_required_receipt_missing")
        run_dir, item = _frontier(root, display, allow_episode_pending=True)
        correct = _correct_letter(display, grading_capsule_path)
        if _choice_result(choice, correct) != "correct":
            raise CampaignAnswerError("reason_finalization_requires_correct_choice")
        if not _audit_reasons(
            item, confidence=confidence_value, recognized_original=recognized
        ):
            raise CampaignAnswerError("reason_finalization_not_required")
        saving = campaign_runtime.saving_contract_for_run(
            root, str(display["run_id"])
        )
        if saving["episode_capture_every_item"]:
            public = {
                "schema": PUBLIC_SCHEMA,
                "status": "episode_pending_terminal",
                "run_id": display["run_id"],
                "campaign_date": display["campaign_date"],
                "item_id": display["item_id"],
                "episode_id": f"{display['run_id']}:{display['item_id']}",
                "choice_result": "correct",
                "confidence": confidence_value,
                "recognized_original": recognized,
                "response_seconds": seconds,
                "reason_required": False,
                "rating": None,
                "capture_required": True,
                "capture_verified": False,
                "frontier_advanced": False,
                "runtime_operation": "campaign_response_observed",
                "runtime_status": "episode_pending_capture",
                "formal_write_count": 0,
                "fsrs_write_count": 0,
                "personal_summary_write_count": 0,
                "replayed": False,
            }
            return _commit_bridge_receipt(
                run_dir,
                idempotency_key=key,
                request_sha256=request_sha,
                operation="finalize_reason",
                display_receipt_sha256=display_sha,
                public_response=public,
            )
        try:
            runtime_receipt = campaign_runtime.record_outcome(
                root,
                str(display["run_id"]),
                repo_root=repo_root,
                item_id=str(display["item_id"]),
                observed_date=str(
                    display.get("displayed_date") or display["campaign_date"]
                ),
                choice_result="correct",
                reasoning_result=reasoning_result,
                confidence=confidence_value,
                prompt_dependency=prompt_dependency,
                audit_performed=True,
                recognized_original=recognized,
                response_seconds=seconds,
                idempotency_key=f"answer-bridge:{key}",
                event_time=event_time,
            )
        except campaign_runtime.CampaignRuntimeError as exc:
            raise CampaignAnswerError(f"runtime_outcome_rejected:{exc}") from exc
        public = _public_from_runtime(
            display=display,
            choice_result="correct",
            confidence=confidence_value,
            recognized_original=recognized,
            response_seconds=seconds,
            runtime_receipt=runtime_receipt,
        )
        public = _publish_after_advance(
            root,
            repo_root=repo_root,
            display=display,
            public=public,
            display_publisher=display_publisher,
            display_publisher_kwargs=display_publisher_kwargs,
        )
        return _commit_bridge_receipt(
            run_dir,
            idempotency_key=key,
            request_sha256=request_sha,
            operation="finalize_reason",
            display_receipt_sha256=display_sha,
            public_response=public,
        )


@study_commit_boundary
def finalize_failure_and_next(
    runtime_root: str | Path,
    *,
    repo_root: str | Path,
    display_receipt_path: str | Path,
    display_receipt_sha256: str,
    capture_receipt_path: str | Path,
    capture_receipt_sha256: str,
    idempotency_key: str,
    event_time: str | None = None,
    display_publisher: Callable[..., dict[str, Any]] | None = None,
    display_publisher_kwargs: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Verify the durable failure Capture, advance, then publish the frontier."""

    root = Path(runtime_root).expanduser().resolve()
    key = _safe_id(idempotency_key, "idempotency_key")
    _display_path, display, display_sha = _normalize_display(
        display_receipt_path, display_receipt_sha256
    )
    run_dir = campaign_runtime.runtime_run_directory(
        root, str(display["run_id"])
    )
    capture_sha = _safe_sha(capture_receipt_sha256, "capture_receipt_sha256")
    request = {
        "operation": "finalize_failure_and_next",
        "display_receipt_sha256": display_sha,
        "capture_receipt_sha256": capture_sha,
    }
    request_sha = _digest(request)
    with _bridge_lock(root):
        repeated = _idempotent_lookup(
            run_dir, idempotency_key=key, request_sha256=request_sha
        )
        if repeated is not None:
            return repeated
        try:
            _verified_run_dir, _run, state, verified_item = (
                campaign_runtime.reopen_verified_frontier(
                    root, str(display["run_id"])
                )
            )
        except campaign_runtime.CampaignRuntimeError as exc:
            raise CampaignAnswerError(f"runtime_frontier_unavailable:{exc}") from exc
        pending_before = state.get("status") == "failure_pending_capture"
        if pending_before:
            item = verified_item
            if item is None:
                raise CampaignAnswerError("runtime_frontier_unavailable:no current item")
            active = state.get("active_segment") or {}
            for field, current in {
                "run_id": state.get("run_id"),
                "campaign_date": state.get("campaign_date"),
                "segment_id": active.get("segment_id"),
                "segment_ordinal": state.get("current_segment_ordinal"),
                "item_id": item.get("item_id"),
                "formal_node_id": item.get("formal_node_id"),
                "true_item_id": item.get("true_item_id"),
                "variant_id": item.get("variant_id"),
                "surface_kind": item.get("surface_kind"),
                "surface_sha256": item.get("surface_sha256"),
                "grading_sha256": item.get("grading_sha256"),
                "campaign_item_commitment_sha256": item.get(
                    "campaign_item_commitment_sha256"
                ),
            }.items():
                if display.get(field) != current:
                    raise CampaignAnswerError(f"runtime_frontier_drift:{field}")
        elif state.get("status") not in {"active", "completed"}:
            raise CampaignAnswerError("failure_capture_not_pending")
        runtime_finalize_started = time.perf_counter()
        try:
            finalized = campaign_runtime.finalize_capture(
                root,
                str(display["run_id"]),
                repo_root=repo_root,
                capture_receipt_path=capture_receipt_path,
                capture_receipt_sha256=capture_sha,
                idempotency_key=f"answer-bridge:{key}",
                event_time=event_time,
            )
        except campaign_runtime.CampaignRuntimeError as exc:
            raise CampaignAnswerError(f"capture_finalization_rejected:{exc}") from exc
        runtime_finalize_ms = (
            time.perf_counter() - runtime_finalize_started
        ) * 1000
        payload = finalized.get("payload")
        if not isinstance(payload, dict) or payload.get("capture_verified") is not True:
            raise CampaignAnswerError("capture_finalization_unverified")
        if not pending_before and finalized.get("status") != "already_committed":
            raise CampaignAnswerError("post_finalize_runtime_receipt_missing")
        for field in (
            "run_id",
            "campaign_date",
            "segment_id",
            "item_id",
            "formal_node_id",
            "true_item_id",
            "variant_id",
            "surface_kind",
            "campaign_item_commitment_sha256",
        ):
            if payload.get(field) != display.get(field):
                raise CampaignAnswerError(
                    f"runtime_finalization_binding_drift:{field}"
                )
        if payload.get("capture_receipt_sha256") != capture_sha:
            raise CampaignAnswerError("runtime_finalization_capture_drift")
        try:
            _advanced_run_dir, _run, advanced_state, _item = (
                campaign_runtime.reopen_verified_frontier(
                    root, str(display["run_id"])
                )
            )
        except campaign_runtime.CampaignRuntimeError as exc:
            raise CampaignAnswerError(
                f"post_finalize_frontier_unavailable:{exc}"
            ) from exc
        old_segment = int(display["segment_ordinal"])
        old_offset = int(display.get("item_offset", 0))
        if not 0 <= old_offset < campaign_runtime.SEGMENT_SIZE:
            raise CampaignAnswerError("post_finalize_item_offset_invalid")
        expected_completed = (
            old_segment * campaign_runtime.SEGMENT_SIZE + old_offset + 1
        )
        if advanced_state.get("completed_items") != expected_completed:
            raise CampaignAnswerError("post_finalize_frontier_drift")
        if advanced_state.get("status") == "active":
            if old_offset + 1 < campaign_runtime.SEGMENT_SIZE:
                expected_segment, expected_offset = old_segment, old_offset + 1
            else:
                expected_segment, expected_offset = old_segment + 1, 0
            if (
                advanced_state.get("current_segment_ordinal") != expected_segment
                or advanced_state.get("current_item_offset") != expected_offset
            ):
                raise CampaignAnswerError("post_finalize_frontier_drift")
        elif advanced_state.get("status") != "completed":
            raise CampaignAnswerError("post_finalize_frontier_drift")
        public = {
            "schema": PUBLIC_SCHEMA,
            "status": "failure_finalized",
            "run_id": display["run_id"],
            "campaign_date": display["campaign_date"],
            "item_id": display["item_id"],
            "choice_result": None,
            "confidence": None,
            "recognized_original": None,
            "response_seconds": None,
            "reason_required": False,
            "rating": "Again",
            "capture_required": True,
            "capture_verified": True,
            "frontier_advanced": True,
            "runtime_operation": finalized.get("operation"),
            "runtime_status": finalized.get("state_status"),
            "formal_write_count": 0,
            "replayed": finalized.get("status") == "already_committed",
        }
        publish_started = time.perf_counter()
        public = _publish_after_advance(
            root,
            repo_root=repo_root,
            display=display,
            public=public,
            display_publisher=display_publisher,
            display_publisher_kwargs=display_publisher_kwargs,
        )
        publish_ms = (time.perf_counter() - publish_started) * 1000
        public = {
            **public,
            "timing_ms": {
                "runtime_finalize_ms": round(runtime_finalize_ms, 3),
                "next_display_publish_ms": round(publish_ms, 3),
            },
        }
        return _commit_bridge_receipt(
            run_dir,
            idempotency_key=key,
            request_sha256=request_sha,
            operation="finalize_failure_and_next",
            display_receipt_sha256=display_sha,
            public_response=public,
        )


def _response_seconds(value: str) -> float | None:
    if value == "unmeasured":
        return None
    try:
        return float(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("use a positive number or unmeasured") from exc


def _explicit_bool(value: str) -> bool:
    if value == "true":
        return True
    if value == "false":
        return False
    raise argparse.ArgumentTypeError("use true or false")


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    for name in ("answer-choice", "finalize-reason"):
        command = commands.add_parser(name)
        command.add_argument("--runtime-root", required=True)
        command.add_argument("--repo-root", required=True)
        command.add_argument("--display-receipt-path", required=True)
        command.add_argument("--display-receipt-sha256", required=True)
        command.add_argument(
            "--grading-capsule-path",
            help="Optional override; otherwise use the capsule bound to the verified display receipt.",
        )
        command.add_argument("--response", required=True)
        command.add_argument("--confidence", choices=sorted(CONFIDENCE_LEVELS))
        command.add_argument("--recognized-original", type=_explicit_bool, required=True)
        command.add_argument("--response-seconds", type=_response_seconds, required=True)
        command.add_argument("--idempotency-key", required=True)
        command.add_argument("--event-time")
    reason = commands.choices["finalize-reason"]
    choice = commands.choices["answer-choice"]
    choice.add_argument("--reasoning-result", choices=sorted(INITIAL_REASONING_RESULTS))
    choice.add_argument("--prompt-dependency", choices=sorted(PROMPT_DEPENDENCIES))
    reason.add_argument("--choice-idempotency-key", required=True)
    reason.add_argument("--reasoning-result", choices=sorted(REASONING_RESULTS), required=True)
    reason.add_argument("--prompt-dependency", choices=sorted(PROMPT_DEPENDENCIES), required=True)
    failure = commands.add_parser("finalize-failure-and-next")
    failure.add_argument("--runtime-root", required=True)
    failure.add_argument("--repo-root", required=True)
    failure.add_argument("--display-receipt-path", required=True)
    failure.add_argument("--display-receipt-sha256", required=True)
    failure.add_argument("--capture-receipt-path", required=True)
    failure.add_argument("--capture-receipt-sha256", required=True)
    failure.add_argument("--idempotency-key", required=True)
    failure.add_argument("--event-time")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    if args.command == "finalize-failure-and-next":
        try:
            result = finalize_failure_and_next(
                args.runtime_root,
                repo_root=args.repo_root,
                display_receipt_path=args.display_receipt_path,
                display_receipt_sha256=args.display_receipt_sha256,
                capture_receipt_path=args.capture_receipt_path,
                capture_receipt_sha256=args.capture_receipt_sha256,
                idempotency_key=args.idempotency_key,
                event_time=args.event_time,
            )
        except CampaignAnswerError as exc:
            print(
                json.dumps(
                    {"schema": PUBLIC_SCHEMA, "status": "blocked", "reason": str(exc)},
                    ensure_ascii=False,
                    sort_keys=True,
                )
            )
            return 2
        print(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True))
        return 0
    common = {
        "runtime_root": args.runtime_root,
        "repo_root": args.repo_root,
        "display_receipt_path": args.display_receipt_path,
        "display_receipt_sha256": args.display_receipt_sha256,
        "grading_capsule_path": args.grading_capsule_path,
        "response": args.response,
        "confidence": args.confidence,
        "recognized_original": args.recognized_original,
        "response_seconds": args.response_seconds,
        "idempotency_key": args.idempotency_key,
        "event_time": args.event_time,
    }
    try:
        if args.command == "answer-choice":
            result = answer_choice(
                **common,
                reasoning_result=args.reasoning_result,
                prompt_dependency=args.prompt_dependency,
            )
        else:
            result = finalize_reason(
                **common,
                choice_idempotency_key=args.choice_idempotency_key,
                reasoning_result=args.reasoning_result,
                prompt_dependency=args.prompt_dependency,
            )
    except CampaignAnswerError as exc:
        print(
            json.dumps(
                {"schema": PUBLIC_SCHEMA, "status": "blocked", "reason": str(exc)},
                ensure_ascii=False,
                sort_keys=True,
            )
        )
        return 2
    print(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    sys.exit(main())
