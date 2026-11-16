#!/usr/bin/env python3
"""Seal direct-evidence Sol decisions for one frozen 408 package."""

from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import os
import re
import stat
import tempfile
from pathlib import Path
from typing import Any

import conversation_package_408 as conversation_package
import current_question_evidence_408 as legacy_evidence
import display_assets_408 as display_assets
import formal_problem_document_408 as formal_document
import intake_fact_capture_408 as capture_model
import personalization_corpus_408 as already_current_store
from study_commit_followup_408 import study_commit_boundary

import runtime_paths_408 as _paths


DECISION_SCHEMA = "sol-curation-decision-408-v2"
TERMINAL_SCHEMA = "sol-curation-terminal-binding-408-v2"
DECISION_LOCATOR_PREFIX = "cs408-sol-decision://sha256/"
TERMINAL_LOCATOR_PREFIX = "cs408-sol-terminal://sha256/"
DEFAULT_DECISION_ROOT = _paths.decision_root().expanduser()
DEFAULT_FORMAL_RUNTIME_ROOT = _paths.intake_runtime_root().expanduser()
SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
FORMAL_ID_RE = re.compile(r"^(DS|CO|OS|CN)_(?:\d{4}|UNK)_\d{3}$")


class SolCurationDecisionError(RuntimeError):
    pass


def _json_bytes(value: Any, *, pretty: bool = False) -> bytes:
    options: dict[str, Any] = {"ensure_ascii": False, "sort_keys": True}
    options.update({"indent": 2} if pretty else {"separators": (",", ":")})
    return (json.dumps(value, **options) + "\n").encode("utf-8")


def _sha256(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


def _read_json(path: str | Path) -> dict[str, Any]:
    try:
        value = json.loads(Path(path).expanduser().read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise SolCurationDecisionError(f"JSON unreadable: {path}") from exc
    if not isinstance(value, dict):
        raise SolCurationDecisionError("JSON root must be an object")
    return value


def _root(value: str | Path | None) -> Path:
    path = Path(value).expanduser() if value is not None else DEFAULT_DECISION_ROOT
    if path.exists() and stat.S_ISLNK(path.lstat().st_mode):
        raise SolCurationDecisionError("decision root cannot be a symlink")
    root = path.resolve()
    if root == Path(root.anchor):
        raise SolCurationDecisionError("decision root is too broad")
    root.mkdir(parents=True, exist_ok=True, mode=0o700)
    os.chmod(root, 0o700)
    return root


def _write_object(root: Path, group: str, value: dict[str, Any]) -> dict[str, str]:
    raw = _json_bytes(value, pretty=True)
    digest = _sha256(raw)
    directory = root / group
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    os.chmod(directory, 0o700)
    path = directory / f"{digest}.json"
    if path.exists():
        if path.is_symlink() or path.read_bytes() != raw:
            raise SolCurationDecisionError("content-addressed object conflict")
        os.chmod(path, 0o600)
    else:
        descriptor, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=directory)
        tmp = Path(temporary)
        try:
            os.fchmod(descriptor, 0o600)
            with os.fdopen(descriptor, "wb") as handle:
                handle.write(raw)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(tmp, path)
            os.chmod(path, 0o600)
        finally:
            if tmp.exists():
                tmp.unlink()
    return {"sha256": digest, "path": str(path)}


def _read_object(root: Path, group: str, locator: str, prefix: str) -> dict[str, Any]:
    if not locator.startswith(prefix):
        raise SolCurationDecisionError("private object locator invalid")
    digest = locator.removeprefix(prefix)
    if not SHA256_RE.fullmatch(digest):
        raise SolCurationDecisionError("private object locator invalid")
    raw = (root / group / f"{digest}.json").read_bytes()
    if _sha256(raw) != digest:
        raise SolCurationDecisionError("private object hash drifted")
    value = json.loads(raw.decode("utf-8"))
    if not isinstance(value, dict):
        raise SolCurationDecisionError("private object invalid")
    return value


def read_terminal_binding(
    locator: str,
    expected_sha256: str,
    *,
    decision_root: str | Path | None = None,
    repo: str | Path | None = None,
) -> dict[str, Any]:
    if (
        not SHA256_RE.fullmatch(expected_sha256)
        or not locator.startswith(TERMINAL_LOCATOR_PREFIX)
        or locator.removeprefix(TERMINAL_LOCATOR_PREFIX) != expected_sha256
    ):
        raise SolCurationDecisionError("terminal locator/hash mismatch")
    value = _read_object(
        _root(decision_root), "terminals", locator, TERMINAL_LOCATOR_PREFIX
    )
    if value.get("schema_version") != TERMINAL_SCHEMA:
        raise SolCurationDecisionError("terminal schema invalid")
    if repo is not None:
        ledger = (
            capture_model.capture_root(Path(repo).expanduser().resolve())
            / "events.jsonl"
        )
        matches: list[bytes] = []
        for line in ledger.read_bytes().splitlines(keepends=True):
            if not line.strip():
                continue
            event = json.loads(line.decode("utf-8"))
            if (
                isinstance(event, dict)
                and event.get("event_id") == value.get("item_result_event_id")
            ):
                matches.append(line)
        if (
            len(matches) != 1
            or _sha256(matches[0]) != value.get("item_result_event_sha256")
        ):
            raise SolCurationDecisionError("terminal item-result bytes drifted")
        event = json.loads(matches[0].decode("utf-8"))
        if (
            event.get("capture_id") != value.get("capture_id")
            or event.get("outcome") != value.get("outcome")
            or event.get("formal_id") != value.get("formal_id")
        ):
            raise SolCurationDecisionError("terminal outcome/formal ID drifted from ledger")
        if value.get("outcome") in {"curated", "created", "updated"} and (
            event.get("receipt_sha256") != value.get("normal_receipt_sha256")
        ):
            raise SolCurationDecisionError("terminal formal receipt drifted from ledger")
        if value.get("outcome") == "already_current" and (
            event.get("verification_sha256") != value.get("verification_sha256")
        ):
            raise SolCurationDecisionError("terminal verification drifted from ledger")
        if value.get("outcome") == "concept_only" and (
            event.get("concept_source_id") != value.get("concept_source_id")
            or event.get("concept_receipt_sha256")
            != value.get("concept_receipt_sha256")
        ):
            raise SolCurationDecisionError("terminal concept-review proof drifted from ledger")
    return value


def read_sealed_decision(
    locator: str,
    expected_sha256: str,
    *,
    decision_root: str | Path | None = None,
) -> dict[str, Any]:
    if (
        not SHA256_RE.fullmatch(expected_sha256)
        or not locator.startswith(DECISION_LOCATOR_PREFIX)
        or locator.removeprefix(DECISION_LOCATOR_PREFIX) != expected_sha256
    ):
        raise SolCurationDecisionError("decision locator/hash mismatch")
    value = _read_object(
        _root(decision_root), "decisions", locator, DECISION_LOCATOR_PREFIX
    )
    if value.get("schema_version") != DECISION_SCHEMA:
        raise SolCurationDecisionError("decision schema invalid")
    return value


def _frozen_capture(repo: Path, batch_id: str, capture_id: str) -> tuple[dict[str, Any], dict[str, Any]]:
    state = _read_json(capture_model.capture_root(repo) / "state.json")
    batch = (state.get("batches") or {}).get(batch_id)
    capture = (state.get("captures") or {}).get(capture_id)
    if not isinstance(batch, dict) or batch.get("status") != "active":
        raise SolCurationDecisionError("active frozen batch missing")
    order = list(batch.get("capture_ids") or [])
    if not isinstance(capture, dict) or capture_id not in order:
        raise SolCurationDecisionError("capture is not in frozen batch")
    prior = order[: order.index(capture_id)]
    if any(item not in (batch.get("results") or {}) for item in prior):
        raise SolCurationDecisionError("frozen order has unfinished prior capture")
    return batch, capture


def reopen_direct_evidence(
    locator: str,
    expected_sha256: str,
    *,
    package_root: str | Path | None = None,
    legacy_root: str | Path | None = None,
) -> dict[str, Any]:
    if not SHA256_RE.fullmatch(expected_sha256):
        raise SolCurationDecisionError("expected evidence SHA invalid")
    if locator.startswith(conversation_package.LOCATOR_PREFIX):
        opened = conversation_package.read_package(locator, private_root=package_root)
        actual = str(opened["manifest"].get("canonical_sha256") or "")
        if actual != expected_sha256:
            raise SolCurationDecisionError("conversation package SHA drifted")
        return {
            "kind": "cs408_conversation_package_v1",
            "sha256": actual,
            "missing_fields": opened["manifest"].get("missing_fields") or [],
            "package": opened,
        }
    if locator.startswith(legacy_evidence.LOCATOR_PREFIX):
        # read_bundle returns the verified manifest itself, not the small
        # creation receipt.  Its content address is the locator suffix and the
        # reader has already reopened that exact manifest/object pair.
        try:
            manifest, bundle = legacy_evidence.read_bundle(
                locator, private_root=legacy_root
            )
            evidence_kind = "legacy_current_question_evidence"
        except legacy_evidence.CurrentQuestionEvidenceError:
            try:
                manifest, bundle = formal_document.read_formal_problem_document_bundle(
                    locator, private_root=legacy_root
                )
                evidence_kind = "formal_problem_document_v4"
            except formal_document.FormalProblemDocumentError as exc:
                raise SolCurationDecisionError(
                    "current-question evidence bundle could not be reopened"
                ) from exc
        actual = locator.removeprefix(legacy_evidence.LOCATOR_PREFIX)
        if actual != expected_sha256:
            raise SolCurationDecisionError("legacy evidence SHA drifted")
        return {
            "kind": evidence_kind,
            "sha256": actual,
            "manifest": manifest,
            "bundle": bundle,
        }
    raise SolCurationDecisionError("unsupported evidence locator")


def _validate_decision(value: dict[str, Any]) -> dict[str, Any]:
    allowed = {
        "decision", "formal_id", "canonical_package", "evidence_refs", "basis",
        "counterevidence", "confidence", "unresolved_gaps",
        "review_backflow_assessment", "knowledge_observations",
    }
    if set(value) - allowed:
        raise SolCurationDecisionError("decision has unknown fields")
    kind = str(value.get("decision") or "")
    if kind not in {
        "new", "redo", "correct_review", "concept_only", "already_current",
        "needs_user", "skip",
    }:
        raise SolCurationDecisionError("decision kind invalid")
    formal_id = value.get("formal_id")
    if kind in {"redo", "correct_review", "already_current"}:
        if not FORMAL_ID_RE.fullmatch(str(formal_id or "")):
            raise SolCurationDecisionError("existing decision requires formal ID")
    elif formal_id is not None:
        raise SolCurationDecisionError("new or unresolved decision cannot preallocate ID")
    canonical = value.get("canonical_package")
    if (kind in {"new", "redo"}) != isinstance(canonical, dict):
        raise SolCurationDecisionError("canonical package presence is inconsistent")
    refs = value.get("evidence_refs")
    if not isinstance(refs, list) or not refs:
        raise SolCurationDecisionError("direct evidence refs required")
    for ref in refs:
        if (
            not isinstance(ref, dict)
            or set(ref) != {"locator", "sha256"}
            or not isinstance(ref.get("locator"), str)
            or not SHA256_RE.fullmatch(str(ref.get("sha256") or ""))
        ):
            raise SolCurationDecisionError("direct evidence ref invalid")
    basis = value.get("basis")
    counter = value.get("counterevidence") or []
    gaps = value.get("unresolved_gaps") or []
    if not isinstance(basis, list) or not basis or not all(isinstance(item, str) and item for item in basis):
        raise SolCurationDecisionError("decision basis invalid")
    if not isinstance(counter, list) or not all(isinstance(item, str) for item in counter):
        raise SolCurationDecisionError("counterevidence invalid")
    if not isinstance(gaps, list) or not all(isinstance(item, str) for item in gaps):
        raise SolCurationDecisionError("unresolved gaps invalid")
    if value.get("confidence") not in {"high", "medium", "low"}:
        raise SolCurationDecisionError("confidence invalid")
    if kind in {"new", "redo"} and gaps:
        raise SolCurationDecisionError("formal decision retains unresolved gaps")
    normalized = {
        "decision": kind,
        "formal_id": formal_id,
        "canonical_package": canonical,
        "evidence_refs": refs,
        "basis": basis,
        "counterevidence": counter,
        "confidence": value["confidence"],
        "unresolved_gaps": gaps,
    }
    if "knowledge_observations" in value:
        normalized["knowledge_observations"] = value["knowledge_observations"]
    assessment = value.get("review_backflow_assessment")
    if assessment is not None:
        if not isinstance(assessment, dict):
            raise SolCurationDecisionError("review backflow assessment must be an object")
        normalized["review_backflow_assessment"] = assessment
    return normalized


def derive_episode_date_binding(
    repo: Path, batch: dict[str, Any], capture: dict[str, Any],
    reopened: dict[str, Any], canonical: dict[str, Any],
) -> dict[str, Any]:
    """Recompute dates from source bytes; never accept a template as date evidence."""
    def actual_date(value: Any, label: str) -> str:
        if not isinstance(value, str):
            raise SolCurationDecisionError(f"{label} missing")
        try:
            parsed = dt.date.fromisoformat(value)
        except ValueError as exc:
            raise SolCurationDecisionError(f"{label} invalid") from exc
        if parsed.isoformat() != value:
            raise SolCurationDecisionError(f"{label} must be YYYY-MM-DD")
        return value

    facts = capture.get("capture") or {}
    day = actual_date(facts.get("study_date"), "capture study_date")
    if batch.get("study_date") != day:
        raise SolCurationDecisionError("frozen batch/capture study_date mismatch")
    if reopened["kind"] == "cs408_conversation_package_v1":
        opened = reopened["package"]
        evidence_day = opened["manifest"].get("study_date")
        source = (opened.get("source") or {}).get("source") or {}
    else:
        evidence_day = (reopened.get("bundle") or {}).get("study_date")
        source = (reopened.get("bundle") or {}).get("source_facts") or {}
    if evidence_day != day:
        raise SolCurationDecisionError("reopened package/capture study_date mismatch")
    if canonical.get("latest_review_date") != day:
        raise SolCurationDecisionError("latest_review_date must match actual episode study_date")
    mode = canonical.get("mode") or "new"
    if mode == "redo":
        from intake_lib_408 import Repo
        row = Repo(repo).master().get(str(canonical.get("formal_id") or ""))
        if row is None:
            raise SolCurationDecisionError("redo date binding requires existing formal node")
        first = row.get("首次做题日期") or "未记录"
        first_basis = "existing_formal_node"
    else:
        # Earlier history is accepted only when present in the reopened source,
        # rather than a date_source explanation invented in the formal template.
        first = actual_date(source.get("first_done_date", day), "source first_done_date")
        if first > day:
            raise SolCurationDecisionError("first_done_date is later than episode study_date")
        first_basis = "reopened_source.first_done_date" if "first_done_date" in source else "episode_study_date"
    if canonical.get("first_done_date") != first:
        raise SolCurationDecisionError("first_done_date does not match verified first evidence")
    return {"schema_version": "cs408-episode-date-binding-v1", "study_date": day,
            "first_done_date": first, "first_done_basis": first_basis}


def frozen_episode_evidence(batch: dict[str, Any], capture_id: str, capture: dict[str, Any]) -> list[dict[str, str]]:
    roots = [{"locator": ref["locator"], "sha256": ref["sha256"]}
             for ref in (capture.get("capture") or {}).get("stable_evidence_refs", [])]
    supplements = (batch.get("supplement_evidence") or {}).get(capture_id, [])
    return roots + list(supplements)


def verify_supplement_binding(batch: dict[str, Any], capture_id: str, reopened: dict[str, Any],
                              normalized: dict[str, Any], *, package_root=None) -> dict[str, Any] | None:
    refs = (batch.get("supplement_evidence") or {}).get(capture_id, [])
    if not refs:
        return None
    cited = normalized.get("evidence_refs") or []
    if any(ref not in cited for ref in refs):
        raise SolCurationDecisionError("decision must cite all frozen supplement evidence")
    for ref in refs:
        supplement = reopen_direct_evidence(ref["locator"], ref["sha256"], package_root=package_root)
        metadata = supplement["package"]["source"]["source"]
        if (metadata.get("episode_capture_id") != capture_id
                or supplement["package"]["manifest"].get("study_date") != batch.get("study_date")):
            raise SolCurationDecisionError("supplement episode/date binding mismatch")
    if reopened.get("sha256") != refs[-1]["sha256"]:
        raise SolCurationDecisionError("formal evidence must use latest frozen cumulative supplement")
    prior_id = (batch.get("supplement_prior_formal_ids") or {}).get(capture_id)
    # A cumulative evidence supplement can correct an earlier completion label
    # without introducing a new mistake.  Preserve the original question ID;
    # select redo/correct_review/already_current from the actual source evidence,
    # rather than forcing every supplemented original through the redo writer.
    if prior_id and (normalized.get("decision") not in {"redo", "correct_review", "already_current"}
                     or normalized.get("formal_id") != prior_id):
        raise SolCurationDecisionError("episode supplement must preserve original formal ID and existing-question action")
    return {"evidence_refs": refs, "prior_formal_id": prior_id}


def verify_episode_date_binding(repo: Path, sealed: dict[str, Any]) -> None:
    """Writer-side reopening, independent of the claimed date binding in a seal."""
    state = _read_json(capture_model.capture_root(repo) / "state.json")
    batch = (state.get("batches") or {}).get(sealed.get("batch_id")) or {}
    capture = (state.get("captures") or {}).get(sealed.get("capture_id")) or {}
    if (sealed.get("capture_id") not in (batch.get("capture_ids") or [])
            or capture.get("payload_sha256") != sealed.get("capture_payload_sha256")
            or (batch.get("capture_set_sha256") or batch.get("capture_set_hash")) != sealed.get("capture_set_sha256")):
        raise SolCurationDecisionError("writer frozen capture binding drifted")
    refs = frozen_episode_evidence(batch, str(sealed.get("capture_id")), capture)
    if not any(ref.get("locator") == sealed.get("evidence_locator")
               and ref.get("sha256") == sealed.get("capture_original_evidence_sha256") for ref in refs):
        raise SolCurationDecisionError("writer capture evidence binding drifted")
    reopened = reopen_direct_evidence(sealed["evidence_locator"], sealed["reopened_evidence_sha256"])
    supplement = verify_supplement_binding(batch, str(sealed["capture_id"]), reopened, sealed["decision"])
    if sealed.get("supplement_binding") != supplement:
        raise SolCurationDecisionError("sealed supplement binding drifted")
    actual = derive_episode_date_binding(repo, batch, capture, reopened, sealed["decision"]["canonical_package"])
    if sealed.get("episode_date_binding") != actual:
        raise SolCurationDecisionError("sealed episode_date_binding missing or drifted")


def seal_decision(
    *, repo: str | Path, batch_id: str, capture_id: str, evidence_locator: str,
    evidence_sha256: str, decision: dict[str, Any], package_root: str | Path | None = None,
    legacy_root: str | Path | None = None, decision_root: str | Path | None = None,
) -> dict[str, Any]:
    from formal_quality_408 import require_owner
    require_owner(repo, batch_id=batch_id, capture_id=capture_id)
    batch, capture = _frozen_capture(Path(repo).expanduser().resolve(), batch_id, capture_id)
    refs = frozen_episode_evidence(batch, capture_id, capture)
    matching = [ref for ref in refs if ref.get("locator") == evidence_locator]
    if len(matching) != 1:
        raise SolCurationDecisionError("capture does not bind evidence locator")
    reopened = reopen_direct_evidence(
        evidence_locator, evidence_sha256, package_root=package_root, legacy_root=legacy_root
    )
    normalized = _validate_decision(decision)
    if not any(
        ref["locator"] == evidence_locator and ref["sha256"] == evidence_sha256
        for ref in normalized["evidence_refs"]
    ):
        raise SolCurationDecisionError("decision does not cite reopened bytes")
    supplement_binding = verify_supplement_binding(batch, capture_id, reopened, normalized, package_root=package_root)
    display_asset_plan_sha256: str | None = None
    episode_date_binding = None
    if normalized["decision"] in {"new", "redo"}:
        canonical = normalized["canonical_package"] or {}
        episode_date_binding = derive_episode_date_binding(Path(repo), batch, capture, reopened, canonical)
        raw_plan = canonical.get("display_asset_plan")
        if reopened["kind"] == "cs408_conversation_package_v1":
            if canonical.get("asset_promotions"):
                raise SolCurationDecisionError(
                    "conversation-package display assets must use display_asset_plan, not arbitrary asset_promotions"
                )
            try:
                normalized_plan = display_assets.normalize_plan(
                    raw_plan,
                    reopened["package"],
                    mode=normalized["decision"],
                    formal_id=normalized["formal_id"],
                    package_locator=evidence_locator,
                    package_sha256=evidence_sha256,
                )
            except display_assets.DisplayAssetError as exc:
                raise SolCurationDecisionError(str(exc)) from exc
            canonical["display_asset_plan"] = normalized_plan
            display_asset_plan_sha256 = display_assets.plan_sha256(normalized_plan)
        elif raw_plan is not None:
            raise SolCurationDecisionError(
                "legacy direct evidence cannot fabricate a conversation-package display plan"
            )
    observation_review = normalized.get("knowledge_observations")
    required = batch.get("knowledge_observation_contract") == "formal-point-observations-408-v1"
    if normalized["decision"] not in {"needs_user"} and (required or observation_review is not None):
        from seal_knowledge_observations_408 import validate_formal_review
        normalized["knowledge_observations"] = validate_formal_review(
            Path(repo), observation_review, reopened, canonical=normalized.get("canonical_package"))
    set_sha = str(batch.get("capture_set_sha256") or batch.get("capture_set_hash") or "")
    if not SHA256_RE.fullmatch(set_sha):
        raise SolCurationDecisionError("capture-set SHA missing")
    sealed = {
        "schema_version": DECISION_SCHEMA,
        "subject": "cs408",
        "batch_id": batch_id,
        "capture_set_sha256": set_sha,
        "capture_id": capture_id,
        "capture_payload_sha256": capture["payload_sha256"],
        "evidence_locator": evidence_locator,
        "capture_original_evidence_sha256": matching[0]["sha256"],
        "reopened_evidence_sha256": evidence_sha256,
        "evidence_kind": reopened["kind"],
        "display_asset_plan_sha256": display_asset_plan_sha256,
        "episode_date_binding": episode_date_binding,
        "supplement_binding": supplement_binding,
        "decision": normalized,
        "processing_mode": "sol_direct_evidence",
        "formal_write_count": 0,
    }
    stored = _write_object(_root(decision_root), "decisions", sealed)
    return {
        "schema_version": DECISION_SCHEMA,
        "status": "sealed",
        "locator": DECISION_LOCATOR_PREFIX + stored["sha256"],
        "sha256": stored["sha256"],
        "capture_id": capture_id,
        "decision": normalized["decision"],
        "formal_write_count": 0,
    }


def seal_already_current(
    *, repo: str | Path, decision_locator: str, formal_id: str,
    formal_runtime_root: str | Path | None = None,
    decision_root: str | Path | None = None,
) -> dict[str, Any]:
    """Seal the current formal row/card for one direct-evidence no-write decision.

    The retired personalization corpus writer used to expose this small receipt
    helper.  Direct curation still needs the same verification contract, so the
    Sol decision surface now owns it without enabling any corpus/projection write.
    """

    repo_path = Path(repo).expanduser().resolve(strict=True)
    decision = _read_object(
        _root(decision_root), "decisions", decision_locator, DECISION_LOCATOR_PREFIX
    )
    normalized = decision.get("decision") or {}
    if (
        normalized.get("decision") not in {"already_current", "correct_review"}
        or normalized.get("formal_id") != formal_id
        or not FORMAL_ID_RE.fullmatch(formal_id)
    ):
        raise SolCurationDecisionError("already-current decision/formal ID mismatch")
    batch, capture = _frozen_capture(
        repo_path, str(decision.get("batch_id") or ""), str(decision.get("capture_id") or "")
    )
    if str(batch.get("capture_set_sha256") or "") != decision.get("capture_set_sha256"):
        raise SolCurationDecisionError("already-current capture set drifted")
    if capture.get("payload_sha256") != decision.get("capture_payload_sha256"):
        raise SolCurationDecisionError("already-current capture payload drifted")
    try:
        capture_model.audit(repo_root=repo_path)
        rows = already_current_store._load_master_rows(repo_path)
        row = rows.get(formal_id)
        if row is None:
            raise SolCurationDecisionError("already-current formal row missing")
        _, safe_card_sha = already_current_store._load_safe_card(repo_path, formal_id)
        material = already_current_store._already_current_material(
            repo=repo_path,
            batch_id=str(decision["batch_id"]),
            capture_id=str(decision["capture_id"]),
            study_date=str(batch.get("study_date") or ""),
            formal_id=formal_id,
            capture_payload_sha256=str(capture["payload_sha256"]),
            formal_row_sha256=already_current_store.sha256_value(row),
            safe_card_sha256=safe_card_sha,
        )
        already_current_store._assert_safe_payload(material)
    except (capture_model.CaptureError, already_current_store.PersonalizationCorpusError) as exc:
        raise SolCurationDecisionError(str(exc)) from exc
    content = already_current_store.canonical_json_bytes(material)
    digest = already_current_store.sha256_bytes(content)
    runtime = (
        Path(formal_runtime_root).expanduser().resolve()
        if formal_runtime_root is not None
        else DEFAULT_FORMAL_RUNTIME_ROOT.resolve()
    )
    receipt_root = runtime / "verification-receipts" / already_current_store._repo_key(repo_path)
    receipt_root.mkdir(parents=True, exist_ok=True, mode=0o700)
    if receipt_root.is_symlink():
        raise SolCurationDecisionError("already-current verification root is unsafe")
    os.chmod(receipt_root, 0o700)
    path = receipt_root / f"{digest}.json"
    if path.exists():
        if path.is_symlink() or not path.is_file() or path.read_bytes() != content:
            raise SolCurationDecisionError("already-current verification path conflict")
        os.chmod(path, 0o600)
    else:
        already_current_store._atomic_write_private(path, content)
        os.chmod(path, 0o600)
    return {
        "schema_version": "already_current_verification_v1",
        "status": "SEALED",
        "verification_sha256": digest,
        "verification_path": str(path),
        "decision_locator": decision_locator,
        "batch_id": str(decision["batch_id"]),
        "capture_id": str(decision["capture_id"]),
        "formal_id": formal_id,
        "formal_write_count": 0,
    }


@study_commit_boundary
def terminal_binding(
    *, repo: str | Path, decision_locator: str, item_result_event_id: str,
    item_result_event_sha256: str, outcome: str, formal_id: str | None,
    normal_receipt_sha256: str | None, verification_sha256: str | None,
    concept_source_id: str | None = None,
    concept_receipt_sha256: str | None = None,
    decision_root: str | Path | None = None,
    observation_root: str | Path | None = None, package_root: str | Path | None = None,
) -> dict[str, Any]:
    root = _root(decision_root)
    decision = _read_object(root, "decisions", decision_locator, DECISION_LOCATOR_PREFIX)
    if outcome not in {
        "curated", "created", "updated", "already_current", "needs_user",
        "failed", "concept_only", "skip", "skipped", "noop", "duplicate",
    }:
        raise SolCurationDecisionError("terminal outcome invalid")
    ledger = capture_model.capture_root(Path(repo).expanduser().resolve()) / "events.jsonl"
    matches: list[bytes] = []
    for line in ledger.read_bytes().splitlines(keepends=True):
        if line.strip():
            event = json.loads(line.decode("utf-8"))
            if isinstance(event, dict) and event.get("event_id") == item_result_event_id:
                matches.append(line)
    if len(matches) != 1 or _sha256(matches[0]) != item_result_event_sha256:
        raise SolCurationDecisionError("item-result bytes did not reopen exactly")
    event = json.loads(matches[0].decode("utf-8"))
    if event.get("capture_id") != decision["capture_id"] or event.get("outcome") != outcome:
        raise SolCurationDecisionError("terminal event binding drifted")
    if outcome in {"curated", "created", "updated"}:
        if not FORMAL_ID_RE.fullmatch(str(formal_id or "")) or not SHA256_RE.fullmatch(str(normal_receipt_sha256 or "")) or verification_sha256 is not None:
            raise SolCurationDecisionError("curated proof incomplete")
    elif outcome == "already_current":
        if not FORMAL_ID_RE.fullmatch(str(formal_id or "")) or not SHA256_RE.fullmatch(str(verification_sha256 or "")) or normal_receipt_sha256 is not None:
            raise SolCurationDecisionError("already-current proof incomplete")
    elif outcome == "concept_only":
        if any(item is not None for item in (formal_id, normal_receipt_sha256, verification_sha256)):
            raise SolCurationDecisionError("concept-only terminal forbids question proof")
        try:
            capture_model._validate_concept_only_receipt(
                Path(repo).expanduser().resolve(),
                capture_id=str(decision.get("capture_id") or ""),
                concept_source_id=str(concept_source_id or ""),
                concept_receipt_sha256=str(concept_receipt_sha256 or ""),
            )
        except (ValueError, OSError, RuntimeError, KeyError) as exc:
            raise SolCurationDecisionError(
                "concept-only proof incomplete: " + str(exc)
            ) from exc
    elif outcome in {"skip", "skipped", "noop", "duplicate"}:
        if formal_id is not None and not FORMAL_ID_RE.fullmatch(str(formal_id)):
            raise SolCurationDecisionError("completed no-write formal ID is invalid")
        if normal_receipt_sha256 is not None or verification_sha256 is not None:
            raise SolCurationDecisionError("completed no-write outcome forbids write proof")
    elif any(item is not None for item in (formal_id, normal_receipt_sha256, verification_sha256)):
        raise SolCurationDecisionError("non-success terminal forbids formal proof")
    terminal = {
        "schema_version": TERMINAL_SCHEMA,
        "subject": "cs408",
        "decision_locator": decision_locator,
        "decision_sha256": decision_locator.removeprefix(DECISION_LOCATOR_PREFIX),
        "batch_id": decision["batch_id"],
        "capture_id": decision["capture_id"],
        "item_result_event_id": item_result_event_id,
        "item_result_event_sha256": item_result_event_sha256,
        "outcome": outcome,
        "formal_id": formal_id,
        "normal_receipt_sha256": normal_receipt_sha256,
        "verification_sha256": verification_sha256,
        "concept_source_id": concept_source_id,
        "concept_receipt_sha256": concept_receipt_sha256,
        "formal_write_count": 0,
    }
    if outcome in {"curated", "created", "updated", "already_current", "concept_only"}:
        terminal["publication_event_id"] = "formal-terminal-408:" + item_result_event_id
    stored = _write_object(root, "terminals", terminal)
    observation_receipt = None
    if (decision["decision"].get("knowledge_observations")
            and outcome not in {"needs_user", "failed", "concept_only"}):
        from seal_knowledge_observations_408 import seal_formal
        try:
            observation_receipt = seal_formal(repo, TERMINAL_LOCATOR_PREFIX + stored["sha256"],
                stored["sha256"], output_root=observation_root, decision_root=decision_root,
                package_root=package_root)
        except (ValueError, OSError, RuntimeError, KeyError) as exc:
            raise SolCurationDecisionError(
                "FORMAL_COMMITTED_KNOWLEDGE_OBSERVATIONS_PENDING: " + str(exc)) from exc
    return {
        "schema_version": TERMINAL_SCHEMA,
        "status": "bound",
        "knowledge_observation_receipt": observation_receipt,
        "locator": TERMINAL_LOCATOR_PREFIX + stored["sha256"],
        "sha256": stored["sha256"],
        "outcome": outcome,
        "formal_write_count": 0,
        "publication_event_id": terminal.get("publication_event_id"),
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    subs = parser.add_subparsers(dest="command", required=True)
    seal = subs.add_parser("seal")
    for flag in ("repo", "batch-id", "capture-id", "evidence-locator", "evidence-sha256", "decision-json"):
        seal.add_argument(f"--{flag}", required=True)
    seal.add_argument("--package-root")
    seal.add_argument("--legacy-root")
    seal.add_argument("--decision-root")
    already = subs.add_parser("seal-already-current")
    for flag in ("repo", "decision-locator", "formal-id"):
        already.add_argument(f"--{flag}", required=True)
    already.add_argument("--formal-runtime-root")
    already.add_argument("--decision-root")
    terminal = subs.add_parser("terminal-binding")
    for flag in ("repo", "decision-locator", "item-result-event-id", "item-result-event-sha256", "outcome"):
        terminal.add_argument(f"--{flag}", required=True)
    terminal.add_argument("--formal-id")
    terminal.add_argument("--normal-receipt-sha256")
    terminal.add_argument("--verification-sha256")
    terminal.add_argument("--concept-source-id")
    terminal.add_argument("--concept-receipt-sha256")
    terminal.add_argument("--decision-root")
    args = parser.parse_args()
    if args.command == "seal":
        result = seal_decision(
            repo=args.repo, batch_id=args.batch_id, capture_id=args.capture_id,
            evidence_locator=args.evidence_locator, evidence_sha256=args.evidence_sha256,
            decision=_read_json(args.decision_json), package_root=args.package_root,
            legacy_root=args.legacy_root, decision_root=args.decision_root,
        )
    elif args.command == "seal-already-current":
        result = seal_already_current(
            repo=args.repo,
            decision_locator=args.decision_locator,
            formal_id=args.formal_id,
            formal_runtime_root=args.formal_runtime_root,
            decision_root=args.decision_root,
        )
    else:
        result = terminal_binding(
            repo=args.repo, decision_locator=args.decision_locator,
            item_result_event_id=args.item_result_event_id,
            item_result_event_sha256=args.item_result_event_sha256,
            outcome=args.outcome, formal_id=args.formal_id,
            normal_receipt_sha256=args.normal_receipt_sha256,
            verification_sha256=args.verification_sha256,
            concept_source_id=args.concept_source_id,
            concept_receipt_sha256=args.concept_receipt_sha256,
            decision_root=args.decision_root,
        )
    print(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
