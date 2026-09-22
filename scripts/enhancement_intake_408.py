#!/usr/bin/env python3
"""Prepare and apply one web-authored 408 formal enhancement package.

The package is typed data.  No package member is executed and no package-selected
destination is honored.  Formal nodes use the existing RepoLock/WAL writer;
point-only episodes use the existing concept-review writer; reviewed learning
profiles use knowledge_profile_408.
"""
from __future__ import annotations

import argparse
import copy
import datetime as dt
import fcntl
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import shutil
import stat
import tempfile
from typing import Any, Callable
import zipfile
from zoneinfo import ZoneInfo

import concept_review_408 as concept_review
import formal_intake_review_backflow_408 as backflow
import intake_batch_apply_408 as formal_writer
import intake_fact_capture_408 as capture_writer
from intake_lib_408 import FORMAL_ID_RE, Repo
from intake_preflight_408 import DETAILS_ROOT_DEFAULT
import knowledge_profile_408 as profiles
import review_feedback_loop as review
import sol_curation_decision_408 as decisions
import study_bridge_408 as bridge
from study_commit_followup_408 import complete_after_commit, defer_automatic_followups
from study_personalization_408 import current_identity as personalization_identity
import web_execution_report_408 as web_reports


MANIFEST_SCHEMA = "cs408-formal-enhancement-manifest-v1"
ENHANCEMENT_SCHEMA = "cs408-formal-enhancement-v1"
PREPARED_SCHEMA = "cs408-formal-enhancement-prepared-receipt-v1"
STATE_SCHEMA = "cs408-formal-enhancement-operation-state-v1"
RESULT_SCHEMA = "cs408-formal-enhancement-apply-receipt-v1"
SUPPORTED_ACTIONS = {
    "new", "redo", "correct_review", "concept_only", "already_applied",
    "needs_user",
}
DECISION_BY_ACTION = {
    "new": "new",
    "redo": "redo",
    "correct_review": "correct_review",
    "concept_only": "concept_only",
    "already_applied": "already_current",
    "needs_user": "needs_user",
}
SHA256_RE = re.compile(r"[0-9a-f]{64}")
SHA1_RE = re.compile(r"[0-9a-f]{40}")
SAFE_ID_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,159}")
POINT_RE = re.compile(r"(?:DS|CO|OS|CN)\d{2}-\d{2}")
MAX_FILES = 256
MAX_FILE_BYTES = 128 * 1024 * 1024
MAX_TOTAL_BYTES = 1024 * 1024 * 1024
MAX_JSON_BYTES = 16 * 1024 * 1024
ROOT_NAME = "enhancement-intake"


class EnhancementError(RuntimeError):
    pass


def _encoded(value: Any) -> bytes:
    return (json.dumps(value, ensure_ascii=False, sort_keys=True,
                       separators=(",", ":")) + "\n").encode("utf-8")


def _digest_value(value: Any) -> str:
    return hashlib.sha256(_encoded(value)).hexdigest()


def _digest_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _require(ok: Any, reason: str) -> None:
    if not ok:
        raise EnhancementError(reason)


def _read_json(path: Path, label: str) -> dict[str, Any]:
    _require(path.is_file() and not path.is_symlink(), f"{label}_missing_or_unsafe")
    _require(path.stat().st_size <= MAX_JSON_BYTES, f"{label}_too_large")
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise EnhancementError(f"{label}_invalid_json:{exc}") from exc
    _require(isinstance(value, dict), f"{label}_object_required")
    return value


def _atomic(path: Path, value: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    _require(not any(part.is_symlink() for part in (path, *path.parents)),
             "enhancement_destination_symlink")
    descriptor, temporary = tempfile.mkstemp(prefix=".enhancement-", dir=path.parent)
    try:
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(_encoded(value))
            stream.flush()
            os.fsync(stream.fileno())
        os.chmod(temporary, 0o600)
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def _root(details_root: str | Path) -> Path:
    details = Path(details_root).expanduser().resolve()
    _require(details.is_dir() and not details.is_symlink(), "details_root_missing_or_unsafe")
    root = details / ROOT_NAME
    root.mkdir(parents=True, exist_ok=True, mode=0o700)
    _require(not root.is_symlink(), "enhancement_root_symlink")
    return root


def _safe_relative(value: Any) -> str:
    _require(isinstance(value, str) and value, "package_relative_path_required")
    path = PurePosixPath(value)
    _require(not path.is_absolute() and value == path.as_posix(), "package_path_not_normalized")
    _require(all(part not in {"", ".", ".."} for part in path.parts), "package_path_unsafe")
    return value


def _tree_inventory(root: Path) -> dict[str, tuple[str, int]]:
    _require(root.is_dir() and not root.is_symlink(), "package_directory_missing_or_unsafe")
    resolved = root.resolve()
    rows: dict[str, tuple[str, int]] = {}
    total = 0
    for path in sorted(root.rglob("*")):
        info = path.lstat()
        _require(not stat.S_ISLNK(info.st_mode), "package_symlink_forbidden")
        if stat.S_ISDIR(info.st_mode):
            continue
        _require(stat.S_ISREG(info.st_mode), "package_non_regular_file_forbidden")
        path.resolve(strict=True).relative_to(resolved)
        relative = path.relative_to(root).as_posix()
        _safe_relative(relative)
        _require(relative not in rows, "package_duplicate_path")
        _require(info.st_size <= MAX_FILE_BYTES, "package_file_too_large:" + relative)
        total += info.st_size
        _require(total <= MAX_TOTAL_BYTES, "package_total_too_large")
        _require(len(rows) < MAX_FILES, "package_file_count_exceeded")
        rows[relative] = (_digest_file(path), info.st_size)
    return rows


def _copy_directory(source: Path, staging: Path) -> None:
    inventory = _tree_inventory(source)
    for relative in inventory:
        target = staging / relative
        target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        with (source / relative).open("rb") as reader, target.open("xb") as writer:
            shutil.copyfileobj(reader, writer, length=1024 * 1024)
            writer.flush()
            os.fsync(writer.fileno())


def _extract_zip(source: Path, staging: Path) -> None:
    _require(source.is_file() and not source.is_symlink(), "enhancement_zip_missing_or_unsafe")
    try:
        archive = zipfile.ZipFile(source)
    except (OSError, zipfile.BadZipFile) as exc:
        raise EnhancementError(f"enhancement_zip_invalid:{exc}") from exc
    with archive:
        infos = archive.infolist()
        _require(0 < len(infos) <= MAX_FILES, "enhancement_zip_file_count_invalid")
        names: set[str] = set()
        total = 0
        for info in infos:
            name = _safe_relative(info.filename.rstrip("/"))
            _require(name not in names, "enhancement_zip_duplicate_path")
            names.add(name)
            mode = (info.external_attr >> 16) & 0o170000
            _require(mode not in {stat.S_IFLNK, stat.S_IFCHR, stat.S_IFBLK, stat.S_IFIFO},
                     "enhancement_zip_special_file_forbidden")
            _require(not (info.flag_bits & 0x1), "enhancement_zip_encrypted_member_forbidden")
            if info.is_dir():
                (staging / name).mkdir(parents=True, exist_ok=True, mode=0o700)
                continue
            _require(info.file_size <= MAX_FILE_BYTES, "enhancement_zip_member_too_large")
            total += info.file_size
            _require(total <= MAX_TOTAL_BYTES, "enhancement_zip_total_too_large")
            if info.compress_size:
                _require(info.file_size <= info.compress_size * 200 + 1024 * 1024,
                         "enhancement_zip_ratio_rejected")
            target = staging / name
            target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            written = 0
            with archive.open(info, "r") as reader, target.open("xb") as writer:
                while True:
                    chunk = reader.read(1024 * 1024)
                    if not chunk:
                        break
                    written += len(chunk)
                    _require(written <= info.file_size and written <= MAX_FILE_BYTES,
                             "enhancement_zip_expansion_drift")
                    writer.write(chunk)
                writer.flush()
                os.fsync(writer.fileno())
            _require(written == info.file_size, "enhancement_zip_size_drift")


def _file_bindings(manifest: dict[str, Any], package_root: Path) -> dict[str, str]:
    rows = manifest.get("files")
    _require(isinstance(rows, list) and rows, "manifest_files_required")
    bindings: dict[str, str] = {}
    total = 0
    for row in rows:
        _require(isinstance(row, dict) and set(row) == {
            "path", "role", "sha256", "size_bytes"
        }, "manifest_file_row_invalid")
        relative = _safe_relative(row.get("path"))
        digest = str(row.get("sha256") or "")
        size = row.get("size_bytes")
        _require(SHA256_RE.fullmatch(digest) is not None, "manifest_file_sha_invalid")
        _require(isinstance(size, int) and not isinstance(size, bool)
                 and 0 <= size <= MAX_FILE_BYTES, "manifest_file_size_invalid")
        _require(isinstance(row.get("role"), str) and row["role"], "manifest_file_role_invalid")
        _require(relative not in bindings, "manifest_file_duplicate")
        path = package_root / relative
        _require(path.is_file() and not path.is_symlink(), "manifest_file_missing:" + relative)
        _require(path.stat().st_size == size and _digest_file(path) == digest,
                 "manifest_file_binding_drift:" + relative)
        bindings[relative] = digest
        total += size
    inventory = _tree_inventory(package_root)
    _require(set(inventory) == {"manifest.json", *bindings}, "package_contains_unlisted_files")
    _require(total + inventory["manifest.json"][1] <= MAX_TOTAL_BYTES,
             "manifest_total_size_exceeded")
    for required in ("enhancement.json", "knowledge_updates.json", "START_HERE.md"):
        _require(required in bindings, "manifest_required_file_missing:" + required)
    return bindings


def _capture_set_sha(capture_ids: list[str]) -> str:
    return hashlib.sha256(json.dumps(capture_ids, ensure_ascii=False, sort_keys=True,
                                     separators=(",", ":")).encode("utf-8")).hexdigest()


def _formal_state(repo: Path, formal_id: str) -> dict[str, str]:
    row = Repo(repo).master().get(formal_id)
    _require(row is not None, "existing_formal_id_missing:" + formal_id)
    card = repo / f"{formal_id}.md"
    _require(card.is_file() and not card.is_symlink(), "existing_safe_card_missing:" + formal_id)
    return {
        "formal_row_sha256": hashlib.sha256(
            json.dumps(row, ensure_ascii=False, sort_keys=True,
                       separators=(",", ":")).encode("utf-8")
        ).hexdigest(),
        "safe_card_sha256": _digest_file(card),
    }


def _verified_evidence_bindings(manifest: dict[str, Any], files: dict[str, str],
                                published: dict[str, Any]) -> dict[str, str]:
    bindings = dict(files)
    run_id = manifest["run_id"]
    for path, digest in files.items():
        bindings[f"enhancement-package://{run_id}/{path}"] = digest
    for row in manifest["captures"]:
        bindings[f"capture://{row['capture_id']}"] = row["capture_payload_sha256"]
        bindings[row["evidence_locator"]] = row["evidence_sha256"]
    commit = manifest["source_commit"]
    publication = published.get("_verified_publication") or {}
    if (
        isinstance(publication, dict)
        and publication.get("source_commit") == commit
        and SHA256_RE.fullmatch(str(publication.get("manifest_sha256") or ""))
    ):
        bindings[f"study-library://{commit}/PUBLISHED_LIBRARY.json"] = publication["manifest_sha256"]
    for row in published.get("files") or []:
        if isinstance(row, dict) and isinstance(row.get("path"), str) \
                and SHA256_RE.fullmatch(str(row.get("sha256") or "")):
            bindings[f"study-library://{commit}/{row['path']}"] = row["sha256"]
    for row in published.get("original_mappings") or []:
        if isinstance(row, dict) and isinstance(row.get("question_id"), str):
            bindings[f"study-library://{commit}/original-mapping/{row['question_id']}"] = _digest_value(row)
    for fid, row in (published.get("question_refs") or {}).items():
        if isinstance(fid, str) and isinstance(row, dict):
            bindings[f"study-library://{commit}/question-ref/{fid}"] = _digest_value(row)
    return bindings


def _validate_profile_question_sources(manifest: dict[str, Any], enhancement: dict[str, Any],
                                       updates: dict[str, Any], published: dict[str, Any]) -> None:
    logical_to_capture = {
        row["logical_question_id"]: row["capture_id"]
        for row in enhancement["items"] if row.get("logical_question_id")
    }
    capture_sources = {row["capture_id"]: row for row in manifest["captures"]}
    published_refs = published.get("question_refs") or {}
    _require(isinstance(published_refs, dict), "published_question_refs_invalid")
    for update in updates.get("updates") or []:
        for ref in update.get("question_refs") or []:
            _require(isinstance(ref, dict), "profile_question_ref_invalid")
            status = ref.get("source_identity_status", "unverified")
            _require(status in {"verified_published", "verified_capture", "unverified"},
                     "profile_question_source_status_invalid")
            fid = ref.get("formal_id")
            logical = ref.get("logical_question_id")
            if status == "verified_published":
                _require(isinstance(fid, str) and isinstance(published_refs.get(fid), dict),
                         "profile_published_question_ref_missing:" + str(fid))
                expected = published_refs[fid]
                for field in ("source_id", "canonical_ids"):
                    if field in ref:
                        _require(ref[field] == expected.get(field),
                                 f"profile_published_question_{field}_drift:{fid}")
            elif status == "verified_capture":
                capture_id = logical_to_capture.get(logical)
                _require(capture_id in capture_sources, "profile_capture_question_ref_missing")
                if "source_id" in ref:
                    opened = decisions.reopen_direct_evidence(
                        capture_sources[capture_id]["evidence_locator"],
                        capture_sources[capture_id]["evidence_sha256"],
                    )
                    source_document = (
                        (opened.get("package") or {}).get("source") or {}
                    )
                    episode = source_document.get("source") or {}
                    actual = (
                        (episode.get("source") or {}).get("source_id")
                        if episode.get("entry_kind") == "study_episode_capture"
                        else source_document.get("source_id")
                    )
                    _require(ref["source_id"] == actual, "profile_capture_source_id_drift")
            else:
                _require(not any(field in ref for field in (
                    "year", "official_question_no", "canonical_ids", "source_label"
                )), "unverified_profile_question_cannot_claim_source_metadata")


def _capture_state(repo: Path) -> dict[str, Any]:
    root = capture_writer.capture_root(repo)
    events = capture_writer._load_events(root)
    return capture_writer.replay(events)


def _validate_manifest(repo: Path, manifest: dict[str, Any]) -> dict[str, Any]:
    required = {
        "schema", "subject", "run_id", "study_date", "timezone", "source_commit",
        "snapshot_id", "local_version", "publication_target", "capture_set_sha256", "captures",
        "affected_points", "files",
    }
    _require(
        required <= set(manifest)
        and set(manifest) <= required | {"web_execution_report"},
        "manifest_fields_invalid",
    )
    _require(manifest.get("schema") == MANIFEST_SCHEMA and manifest.get("subject") == "408",
             "manifest_schema_or_subject_invalid")
    _require(SAFE_ID_RE.fullmatch(str(manifest.get("run_id") or "")) is not None,
             "manifest_run_id_invalid")
    try:
        dt.date.fromisoformat(str(manifest.get("study_date") or ""))
    except ValueError as exc:
        raise EnhancementError("manifest_study_date_invalid") from exc
    _require(manifest.get("timezone") == "Asia/Shanghai"
             and manifest.get("publication_target") == "local", "manifest_scope_invalid")
    source_commit = str(manifest.get("source_commit") or "")
    _require(SHA1_RE.fullmatch(source_commit) is not None, "manifest_source_commit_invalid")
    try:
        published = bridge._published_library(source_commit, repo)
    except (bridge.BridgeError, OSError, ValueError, KeyError) as exc:
        raise EnhancementError(f"manifest_source_commit_not_verified:{exc}") from exc
    _require(manifest.get("snapshot_id") == source_commit,
             "manifest_snapshot_id_must_equal_verified_source_commit")
    _require(manifest.get("local_version") == published.get("local_version")
             and SHA256_RE.fullmatch(str(manifest.get("local_version") or "")),
             "manifest_local_version_drift")
    captures = manifest.get("captures")
    _require(isinstance(captures, list) and captures, "manifest_captures_required")
    capture_ids: list[str] = []
    state = _capture_state(repo)
    normalized: list[dict[str, Any]] = []
    for row in captures:
        _require(isinstance(row, dict) and set(row) == {
            "capture_id", "capture_payload_sha256", "episode_id", "execution_kind",
            "evidence_locator", "evidence_sha256",
        }, "manifest_capture_fields_invalid")
        capture_id = str(row.get("capture_id") or "")
        _require(SAFE_ID_RE.fullmatch(capture_id) is not None, "manifest_capture_id_invalid")
        _require(row.get("execution_kind") in {
            "original_review", "concept_supplement", "standalone_question"
        }, "manifest_execution_kind_invalid")
        _require(SAFE_ID_RE.fullmatch(str(row.get("episode_id") or "")) is not None,
                 "manifest_episode_id_invalid")
        for field in ("capture_payload_sha256", "evidence_sha256"):
            _require(SHA256_RE.fullmatch(str(row.get(field) or "")) is not None,
                     "manifest_capture_sha_invalid:" + field)
        current = state.get("captures", {}).get(capture_id)
        _require(isinstance(current, dict), "manifest_capture_unknown:" + capture_id)
        _require(current.get("study_date") == manifest["study_date"]
                 and current.get("payload_sha256") == row["capture_payload_sha256"],
                 "manifest_capture_state_drift:" + capture_id)
        refs = list((current.get("capture") or {}).get("stable_evidence_refs") or [])
        refs.extend(current.get("supplement_history") or [])
        _require(any(ref.get("locator") == row["evidence_locator"]
                     and ref.get("sha256") == row["evidence_sha256"] for ref in refs),
                 "manifest_capture_evidence_not_bound:" + capture_id)
        try:
            reopened = decisions.reopen_direct_evidence(
                row["evidence_locator"], row["evidence_sha256"]
            )
        except (decisions.SolCurationDecisionError, OSError, ValueError) as exc:
            raise EnhancementError(f"manifest_capture_evidence_drift:{capture_id}:{exc}") from exc
        source = ((reopened.get("package") or {}).get("source") or {}).get("source") or {}
        actual_kind = source.get("execution_kind")
        actual_episode = source.get("episode_id") or source.get("episode_capture_id")
        if actual_kind is not None:
            _require(actual_kind == row["execution_kind"], "manifest_execution_kind_drift:" + capture_id)
        if actual_episode is not None:
            _require(actual_episode == row["episode_id"], "manifest_episode_id_drift:" + capture_id)
        capture_ids.append(capture_id)
        normalized.append(copy.deepcopy(row))
    _require(capture_ids == sorted(set(capture_ids)), "manifest_captures_must_be_sorted_unique")
    _require(manifest.get("capture_set_sha256") == _capture_set_sha(capture_ids),
             "manifest_capture_set_sha256_invalid")
    points = manifest.get("affected_points")
    _require(isinstance(points, list) and points == sorted(set(points))
             and all(POINT_RE.fullmatch(str(value or "")) for value in points),
             "manifest_affected_points_invalid")
    return {"capture_ids": capture_ids, "captures": normalized, "published": published}


def _observed_points(decision: dict[str, Any]) -> set[str]:
    observations = decision.get("knowledge_observations") or {}
    points = observations.get("points") if isinstance(observations, dict) else None
    if not isinstance(points, list):
        return set()
    return {
        str(row.get("knowledge_point")) for row in points
        if isinstance(row, dict) and row.get("status") == "observed"
    }


def _validate_review_assessment(decision: dict[str, Any], action: str) -> None:
    if action in {"concept_only", "needs_user"}:
        return
    canonical = decision.get("canonical_package")
    assessment = (
        canonical.get("review_backflow_assessment")
        if isinstance(canonical, dict)
        else decision.get("review_backflow_assessment")
    )
    required = {
        "schema", "source", "source_id", "source_identity", "review_unit_id",
        "formal_node_id", "first_result", "confidence", "prompt_level",
        "reasoning_break", "first_break", "first_break_provenance", "event_time",
    }
    correction = {
        "supersedes_source_event_id", "source_event_sha256", "supersession_reason"
    }
    reconciliation = {"deferred_outcome_reconciliation"}
    _require(isinstance(assessment, dict)
             and frozenset(assessment) in {
                 frozenset(required), frozenset(required | correction),
                 frozenset(required | reconciliation),
                 frozenset(required | correction | reconciliation),
             },
             "enhancement_review_assessment_fields_invalid")
    _require(assessment.get("schema") == backflow.DIRECT_ASSESSMENT_SCHEMA
             and assessment.get("source") in review.SOURCES
             and assessment.get("first_result") in review.FIRST_RESULTS | {"blank"}
             and assessment.get("confidence") in review.CONFIDENCE_LEVELS
             and assessment.get("prompt_level") in review.PROMPT_LEVELS
             and isinstance(assessment.get("reasoning_break"), bool)
             and assessment.get("first_break_provenance") in review.FIRST_BREAK_PROVENANCES,
             "enhancement_review_assessment_invalid")
    normalized = backflow._normalize_no_break_assessment(assessment)
    no_break = str(normalized.get("first_break") or "").strip().lower() in backflow.NONE_BREAKS
    _require(not no_break or (normalized.get("first_result") == "independent_correct"
                              and normalized.get("reasoning_break") is False),
             "enhancement_assessment_requires_explicit_evidence_boundary")


def _validate_enhancement(repo: Path, manifest: dict[str, Any], enhancement: dict[str, Any],
                          published: dict[str, Any], completed: set[str] | None = None) -> None:
    _require(set(enhancement) == {"schema", "run_id", "source_commit", "items"},
             "enhancement_fields_invalid")
    _require(enhancement.get("schema") == ENHANCEMENT_SCHEMA
             and enhancement.get("run_id") == manifest["run_id"]
             and enhancement.get("source_commit") == manifest["source_commit"],
             "enhancement_identity_drift")
    items = enhancement.get("items")
    _require(isinstance(items, list) and items, "enhancement_items_required")
    expected_ids = [row["capture_id"] for row in manifest["captures"]]
    actual_ids: list[str] = []
    item_ids: set[str] = set()
    observed_points: set[str] = set()
    logical_ids: set[str] = set()
    manifest_by_capture = {row["capture_id"]: row for row in manifest["captures"]}
    for item in items:
        required = {
            "item_id", "capture_id", "action", "logical_question_id", "formal_id",
            "expected_formal_state", "decision", "concept_review", "needs_user_reason",
        }
        _require(isinstance(item, dict) and set(item) == required, "enhancement_item_fields_invalid")
        item_id = str(item.get("item_id") or "")
        _require(SAFE_ID_RE.fullmatch(item_id) is not None and item_id not in item_ids,
                 "enhancement_item_id_invalid")
        item_ids.add(item_id)
        capture_id = str(item.get("capture_id") or "")
        action = str(item.get("action") or "")
        _require(capture_id in manifest_by_capture and action in SUPPORTED_ACTIONS,
                 "enhancement_item_scope_or_action_invalid")
        decision = item.get("decision")
        _require(isinstance(decision, dict)
                 and decision.get("decision") == DECISION_BY_ACTION[action],
                 "enhancement_decision_action_mismatch:" + capture_id)
        try:
            decisions._validate_decision(decision)
        except decisions.SolCurationDecisionError as exc:
            raise EnhancementError(f"enhancement_decision_invalid:{capture_id}:{exc}") from exc
        _validate_review_assessment(decision, action)
        refs = decision.get("evidence_refs") or []
        capture = manifest_by_capture[capture_id]
        _require(capture["execution_kind"] != "concept_supplement"
                 or action in {"concept_only", "needs_user"},
                 "point_only_capture_cannot_create_or_review_original_question")
        _require({"locator": capture["evidence_locator"], "sha256": capture["evidence_sha256"]} in refs,
                 "enhancement_decision_missing_primary_evidence:" + capture_id)
        if action != "needs_user":
            reopened = decisions.reopen_direct_evidence(
                capture["evidence_locator"], capture["evidence_sha256"]
            )
            try:
                from seal_knowledge_observations_408 import validate_formal_review
                validated_observations = validate_formal_review(
                    repo, decision.get("knowledge_observations"), reopened,
                    canonical=decision.get("canonical_package"),
                )
                canonical_assessment = decision.get("canonical_package") or decision
                assessment = canonical_assessment.get("review_backflow_assessment") or {}
                correction = assessment.get("deferred_outcome_reconciliation")
                if correction is not None:
                    first = (correction.get("first_response_ref") or {}).get("turn")
                    _require(any(r.get("event_kind") == "outcome"
                                 and r.get("source_order") == first
                                 for point in validated_observations["points"]
                                 for r in point.get("records", [])),
                             "enhancement_first_response_observation_missing:" + capture_id)
            except (ValueError, OSError, RuntimeError, KeyError) as exc:
                raise EnhancementError(
                    f"enhancement_knowledge_observations_invalid:{capture_id}:{exc}"
                ) from exc
            observed_points.update(_observed_points(decision))
        formal_id = item.get("formal_id")
        logical_id = item.get("logical_question_id")
        if action == "new":
            _require(isinstance(logical_id, str) and SAFE_ID_RE.fullmatch(logical_id)
                     and logical_id not in logical_ids and formal_id is None,
                     "enhancement_new_identity_invalid")
            logical_ids.add(logical_id)
        elif action in {"redo", "correct_review", "already_applied"}:
            _require(isinstance(formal_id, str) and FORMAL_ID_RE.fullmatch(formal_id)
                     and logical_id is None and decision.get("formal_id") == formal_id,
                     "enhancement_existing_identity_invalid:" + capture_id)
            expected = item.get("expected_formal_state")
            if capture_id not in (completed or set()):
                published_state = ((published.get("question_refs") or {}).get(formal_id) or {}).get("formal_state")
                _require(isinstance(expected, dict) and expected == published_state
                         and expected == _formal_state(repo, formal_id),
                         "enhancement_expected_formal_state_drift:" + formal_id)
        else:
            _require(formal_id is None and logical_id is None
                     and item.get("expected_formal_state") is None,
                     "enhancement_nonquestion_identity_invalid:" + capture_id)
        canonical = decision.get("canonical_package")
        if action in {"new", "redo"}:
            _require(isinstance(canonical, dict)
                     and canonical.get("contract") == formal_writer.APPROVED_CONTRACT
                     and canonical.get("schema_version") == formal_writer.APPROVED_CONTRACT
                     and canonical.get("mode") == action,
                     "enhancement_canonical_package_contract_invalid:" + capture_id)
            if action == "new":
                _require(canonical.get("formal_id") is None and canonical.get("auto_id") is True,
                         "enhancement_new_canonical_identity_invalid")
                detail = canonical.get("detail_card_source")
                _require(isinstance(detail, dict) and "package_path" in detail,
                         "enhancement_new_detail_card_package_source_required")
            else:
                _require(canonical.get("formal_id") == formal_id and canonical.get("auto_id") is False,
                         "enhancement_redo_canonical_identity_invalid")
        else:
            _require(canonical is None, "enhancement_nonformal_canonical_package_forbidden")
        if action == "concept_only":
            concept = item.get("concept_review")
            _require(isinstance(concept, dict) and set(concept) == {
                "session_id", "started_at", "ended_at", "observations"
            }, "enhancement_concept_review_required")
            observations = concept.get("observations")
            _require(isinstance(observations, list) and observations
                     and all(isinstance(row, dict) and row.get("question_ref") == capture_id
                             for row in observations), "enhancement_concept_observations_invalid")
            _require({str(row.get("concept_key")) for row in observations}
                     == _observed_points(decision), "enhancement_concept_point_set_mismatch")
        else:
            _require(item.get("concept_review") is None, "enhancement_unexpected_concept_review")
        if action == "needs_user":
            _require(isinstance(item.get("needs_user_reason"), str)
                     and item["needs_user_reason"].strip(), "enhancement_needs_user_reason_required")
        else:
            _require(item.get("needs_user_reason") is None, "enhancement_unexpected_needs_user_reason")
        actual_ids.append(capture_id)
    _require(actual_ids == expected_ids, "enhancement_items_must_match_manifest_order")
    _require(observed_points == set(manifest["affected_points"]),
             "enhancement_observed_points_must_equal_affected_points")


def _revalidate_package(receipt: dict[str, Any]) -> tuple[Path, dict[str, Any], dict[str, Any], dict[str, Any], dict[str, str]]:
    package_root = Path(str(receipt.get("package_root") or "")).expanduser().resolve()
    _require(package_root.is_dir() and not package_root.is_symlink(), "prepared_package_root_missing")
    manifest = _read_json(package_root / "manifest.json", "manifest")
    bindings = _file_bindings(manifest, package_root)
    _require(_digest_value({"manifest": manifest, "files": bindings}) == receipt.get("package_sha256"),
             "prepared_package_sha_drift")
    enhancement = _read_json(package_root / "enhancement.json", "enhancement")
    updates = _read_json(package_root / "knowledge_updates.json", "knowledge_updates")
    return package_root, manifest, enhancement, updates, bindings


def _inspect_web_execution(
    package_root: Path, manifest: dict[str, Any], repo: Path
) -> dict[str, Any]:
    if manifest.get("web_execution_report") is None:
        return web_reports.inspect_report(package_root, manifest, None)
    try:
        published = bridge._published_library(
            str(manifest.get("source_commit") or ""), repo
        )
    except (bridge.BridgeError, OSError, ValueError, KeyError) as exc:
        raise EnhancementError(
            f"web_report_source_publication_unavailable:{exc}"
        ) from exc
    return web_reports.inspect_report(package_root, manifest, published)


def prepare(*, source: str | Path, repo: str | Path, details_root: str | Path,
            resume_prepared: str | Path | None = None) -> dict[str, Any]:
    repo_path = Path(repo).expanduser().resolve()
    Repo(repo_path)
    source_path = Path(source).expanduser().resolve(strict=True)
    root = _root(details_root)
    staging = Path(tempfile.mkdtemp(prefix=".prepare-", dir=root))
    try:
        if source_path.is_dir():
            _copy_directory(source_path, staging)
        else:
            _extract_zip(source_path, staging)
        manifest = _read_json(staging / "manifest.json", "manifest")
        bindings = _file_bindings(manifest, staging)
        _require(not (staging / "partition.json").exists(), "enhancement_partition_requires_local_merge")
        scope = _validate_manifest(repo_path, manifest)
        enhancement = _read_json(staging / "enhancement.json", "enhancement")
        parent_state = None
        parent_sha = None
        completed = set()
        if resume_prepared is not None:
            parent, _, parent_sha = _read_prepared(resume_prepared, details_root)
            _, parent_manifest, parent_enhancement, _, _ = _revalidate_package(parent)
            _require(Path(parent["repo_root"]).resolve() == repo_path
                     and parent_manifest["captures"] == manifest["captures"]
                     and parent_manifest["study_date"] == manifest["study_date"]
                     and parent_manifest["source_commit"] == manifest["source_commit"],
                     "repair_must_preserve_exact_parent_scope")
            parent_state = _load_operation(parent, parent_sha)
            _require(parent_state.get("profile_receipt") is None
                     and not profiles.operation_applied(repo_path, "enhancement-profile:" + parent_sha)
                     and parent_state.get("batch_close") is None,
                     "repair_requires_unfinished_formal_items")
            completed = {cid for cid, row in (parent_state.get("items") or {}).items()
                         if row.get("status") in {"COMPLETE", "NEEDS_USER"}}
            old_items = {row["capture_id"]: row for row in parent_enhancement["items"]}
            new_items = {row["capture_id"]: row for row in enhancement["items"]}
            _require(all(new_items.get(cid) == old_items[cid] for cid in completed),
                     "repair_cannot_change_committed_item")
        _validate_enhancement(repo_path, manifest, enhancement, scope["published"], completed=completed)
        updates = _read_json(staging / "knowledge_updates.json", "knowledge_updates")
        evidence_bindings = _verified_evidence_bindings(
            manifest, bindings, scope["published"]
        )
        web_reports.inspect_report(staging, manifest, scope["published"])
        _validate_profile_question_sources(
            manifest, enhancement, updates, scope["published"]
        )
        profiles.validate_updates(
            repo_path, updates, expected_points=manifest["affected_points"],
            evidence_bindings=evidence_bindings, formal_bindings=None,
            check_versions=True,
        )
        package_sha = _digest_value({"manifest": manifest, "files": bindings})
        package_root = root / "packages" / package_sha
        lock_path = root / ".prepare.lock"
        with lock_path.open("a+b") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            if package_root.exists():
                existing_manifest = _read_json(package_root / "manifest.json", "existing_manifest")
                existing_bindings = _file_bindings(existing_manifest, package_root)
                _require(_digest_value({"manifest": existing_manifest, "files": existing_bindings}) == package_sha,
                         "prepared_package_content_conflict")
            else:
                package_root.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
                os.rename(staging, package_root)
                staging = Path("")
        web_execution_report = web_reports.inspect_report(
            package_root, manifest, scope["published"]
        )
        prepared = {
            "schema": PREPARED_SCHEMA,
            "status": "PREPARED",
            "repo_root": str(repo_path),
            "details_root": str(Path(details_root).expanduser().resolve()),
            "package_root": str(package_root),
            "package_sha256": package_sha,
            "run_id": manifest["run_id"],
            "study_date": manifest["study_date"],
            "source_commit": manifest["source_commit"],
            "snapshot_id": manifest["snapshot_id"],
            "local_version": manifest["local_version"],
            "capture_ids": scope["capture_ids"],
            "capture_set_sha256": manifest["capture_set_sha256"],
            "affected_points": manifest["affected_points"],
            "file_bindings": bindings,
            "evidence_bindings": evidence_bindings,
            "web_execution_report": web_execution_report,
            "formal_write_count": 0,
            "profile_write_count": 0,
            "model_call_count": 0,
        }
        if parent_sha is not None:
            prepared["repair_parent_prepared_sha256"] = parent_sha
        prepared_sha = _digest_value(prepared)
        receipt_path = root / "prepared-receipts" / f"{prepared_sha}.json"
        if receipt_path.exists():
            _require(_read_json(receipt_path, "prepared_receipt") == prepared,
                     "prepared_receipt_content_conflict")
        else:
            _atomic(receipt_path, prepared)
        if parent_state is not None:
            state_path, _ = _operation_paths(prepared, prepared_sha)
            if not state_path.exists():
                resumed = copy.deepcopy(parent_state)
                resumed.update(prepared_sha256=prepared_sha, status="PREPARED",
                               last_error=None, repair_parent_prepared_sha256=parent_sha)
                _save_operation(prepared, prepared_sha, resumed)
        return {**prepared, "prepared_sha256": prepared_sha, "receipt_path": str(receipt_path)}
    finally:
        if staging and str(staging) and staging.exists() and staging != Path("."):
            shutil.rmtree(staging)


def _prepared_path(value: str | Path, details_root: str | Path | None = None) -> Path:
    raw = Path(value).expanduser()
    if raw.is_file():
        return raw.resolve()
    text = str(value)
    _require(SHA256_RE.fullmatch(text) is not None and details_root is not None,
             "prepared_receipt_path_or_sha_required")
    return (_root(details_root) / "prepared-receipts" / f"{text}.json").resolve()


def _read_prepared(value: str | Path, details_root: str | Path | None = None) -> tuple[dict[str, Any], Path, str]:
    path = _prepared_path(value, details_root)
    receipt = _read_json(path, "prepared_receipt")
    _require(receipt.get("schema") == PREPARED_SCHEMA and receipt.get("status") == "PREPARED",
             "prepared_receipt_schema_invalid")
    digest = _digest_value(receipt)
    _require(path.name == f"{digest}.json", "prepared_receipt_name_hash_mismatch")
    return receipt, path, digest


def _operation_paths(receipt: dict[str, Any], prepared_sha: str) -> tuple[Path, Path]:
    root = _root(receipt["details_root"])
    return (root / "operations" / f"{prepared_sha}.json",
            root / "apply-receipts" / f"{prepared_sha}.json")


def _own_formal_commit_exists(repo: Path, prepared_sha: str, capture_id: str) -> bool:
    key = f"enhancement-formal:{prepared_sha}:{capture_id}"
    path = formal_writer._receipt_path(
        formal_writer.DEFAULT_RUNTIME_ROOT, repo, key
    )
    if not path.is_file() or path.is_symlink():
        return False
    value = _read_json(path, "owned_formal_receipt")
    return (
        value.get("schema") == "intake_batch_receipt_v1"
        and value.get("status") == "COMMITTED"
        and value.get("idempotency_key") == key
        and Path(str(value.get("repo_root") or "")).resolve() == repo
    )


def _publication_receipt(repo: Path, operation_event_id: str,
                         followup: dict[str, Any]) -> dict[str, Any] | None:
    local = followup.get("local_tutoring") or {}
    if local.get("status") not in {"refreshed", "already_current"}:
        return None
    identity = personalization_identity(repo)
    if identity.get("status") != "ready" or identity.get("source_version") != local.get("source_version"):
        return None
    expected_event = "personalization-408:" + hashlib.sha256(
        json.dumps([operation_event_id, identity["version"]], ensure_ascii=False,
                   separators=(",", ":")).encode("utf-8")
    ).hexdigest()
    if followup.get("publication_event_id") != expected_event:
        return None
    matches = []
    for path in (bridge.PUBLICATION_ROOT / "receipts").glob("*.json"):
        try:
            value = _read_json(path, "publication_receipt")
        except EnhancementError:
            continue
        if value.get("event_id") == expected_event and value.get("subject") == "408":
            matches.append(value)
    if len(matches) != 1 or matches[0].get("status") != "PUBLISHED":
        return None
    receipt = matches[0]
    if (
        receipt.get("event_id") != expected_event
        or receipt.get("subject") != "408"
        or not SHA1_RE.fullmatch(str(receipt.get("source_commit") or ""))
    ):
        return None
    return receipt


def _publication_complete(repo: Path, operation_event_id: str,
                          followup: dict[str, Any]) -> bool:
    return _publication_receipt(repo, operation_event_id, followup) is not None


def _resolve_morning_target(
    state: dict[str, Any], requested: str | None
) -> tuple[str, bool]:
    current = state.get("morning_target_date")
    if requested is not None:
        try:
            requested = dt.date.fromisoformat(requested).isoformat()
        except ValueError as exc:
            raise EnhancementError("morning_target_date_invalid") from exc
    if current is not None:
        try:
            current = dt.date.fromisoformat(str(current)).isoformat()
        except ValueError as exc:
            raise EnhancementError("persisted_morning_target_date_invalid") from exc
        _require(requested in {None, current}, "morning_target_date_conflict")
        return current, False
    target = requested or (
        dt.datetime.now(ZoneInfo("Asia/Shanghai")).date()
        + dt.timedelta(days=1)
    ).isoformat()
    state["morning_target_date"] = target
    return target, True


def _formal_layers_complete(state: dict[str, Any]) -> bool:
    items = state.get("items") or {}
    return bool(
        items
        and all(row.get("status") in {"COMPLETE", "NEEDS_USER"} for row in items.values())
        and state.get("profile_receipt") is not None
        and state.get("batch_close") is not None
    )


def _exporter_function(
    override: Callable[..., dict[str, Any]] | None,
) -> Callable[..., dict[str, Any]]:
    if override is not None:
        return override
    try:
        from export_morning_input_408 import export_bundle
    except ImportError as exc:
        raise EnhancementError("morning_input_exporter_unavailable") from exc
    return export_bundle


def _morning_paths_ready(value: dict[str, Any]) -> bool:
    for field in ("zip_path", "prompt_path"):
        raw = value.get(field)
        if not isinstance(raw, str):
            return False
        path = Path(raw).expanduser()
        if not path.is_absolute() or not path.is_file() or path.is_symlink():
            return False
    if value.get("schema") == "cs408-morning-selection-input-v1":
        limit, target = value.get("question_limit"), value.get("target_question_count")
        return (isinstance(limit, int) and not isinstance(limit, bool) and 1 <= limit <= 100
                and isinstance(target, int) and not isinstance(target, bool)
                and 0 <= target <= limit and value.get("part_count") == 1)
    if value.get("concept_count", 0) > 40:
        parts = value.get("parts") or []
        if len(parts) < 2 or value.get("part_count") != len(parts):
            return False
        index = value.get("partition_set_path")
        if not isinstance(index, str) or not Path(index).is_file():
            return False
        for part in parts:
            for field in ("zip_path", "prompt_path"):
                raw = part.get(field)
                if not isinstance(raw, str) or not Path(raw).is_absolute() or not Path(raw).is_file() or Path(raw).is_symlink():
                    return False
    return True


def _ensure_morning_input(
    *,
    repo: Path,
    state: dict[str, Any],
    target_date: str,
    publication: dict[str, Any],
    exporter: Callable[..., dict[str, Any]] | None,
) -> dict[str, Any]:
    source_commit = str(publication.get("source_commit") or "")
    _require(SHA1_RE.fullmatch(source_commit) is not None,
             "morning_input_publication_source_commit_invalid")
    existing = state.get("morning_input")
    if isinstance(existing, dict) and existing.get("status") == "READY_FOR_WEB_PREPARATION":
        _require(existing.get("target_date") == target_date, "morning_input_identity_drift")
        exporter_current = (exporter is not None or existing.get("exporter_sha256") ==
                            _digest_file(Path(__file__).with_name("export_morning_input_408.py")))
        if existing.get("source_commit") == source_commit and _morning_paths_ready(existing) and exporter_current:
            return existing
        if existing.get("source_commit") != source_commit:
            history = state.setdefault("morning_input_history", [])
            if not any(row.get("input_version") == existing.get("input_version")
                       and row.get("source_commit") == existing.get("source_commit") for row in history):
                history.append(copy.deepcopy(existing))
    function = _exporter_function(exporter)
    try:
        result = function(
            target_date=target_date,
            source_commit=source_commit,
            repo=repo,
        )
        _require(isinstance(result, dict), "morning_input_export_result_invalid")
        _require(
            result.get("status") == "READY_FOR_WEB_PREPARATION"
            and result.get("target_date") == target_date
            and result.get("source_commit") == source_commit
            and result.get("formal_write_count") == 0
            and result.get("learner_evidence_write_count") == 0,
            "morning_input_export_result_binding_invalid",
        )
        concept_count = result.get("concept_count")
        _require(
            isinstance(concept_count, int)
            and not isinstance(concept_count, bool)
            and concept_count >= 0,
            "morning_input_concept_count_invalid",
        )
        _require(_morning_paths_ready(result), "morning_input_export_path_invalid")
        state["morning_input"] = copy.deepcopy(result)
    except Exception as exc:
        state["morning_input"] = {
            "status": "FORMAL_COMPLETE_MORNING_INPUT_PENDING",
            "target_date": target_date,
            "source_commit": source_commit,
            "reason": f"{type(exc).__name__}:{exc}",
            "formal_write_count": 0,
            "learner_evidence_write_count": 0,
        }
    return state["morning_input"]


def _load_operation(receipt: dict[str, Any], prepared_sha: str) -> dict[str, Any]:
    state_path, _ = _operation_paths(receipt, prepared_sha)
    if not state_path.exists():
        return {
            "schema": STATE_SCHEMA,
            "prepared_sha256": prepared_sha,
            "status": "PREPARED",
            "batch": None,
            "items": {},
            "formal_bindings": {},
            "profile_receipt": None,
            "batch_close": None,
            "archives": {},
            "followup": None,
            "morning_target_date": None,
            "morning_input": None,
            "web_execution_report": None,
            "formal_completion_status": None,
            "last_error": None,
        }
    state = _read_json(state_path, "enhancement_operation_state")
    _require(state.get("schema") == STATE_SCHEMA
             and state.get("prepared_sha256") == prepared_sha, "enhancement_operation_state_invalid")
    return state


def _save_operation(receipt: dict[str, Any], prepared_sha: str, state: dict[str, Any]) -> None:
    state_path, _ = _operation_paths(receipt, prepared_sha)
    _atomic(state_path, state)


def _resolve_package_specs(value: Any, package_root: Path, bindings: dict[str, str]) -> Any:
    """Resolve only package_path transport fields into the prepared details root."""
    if isinstance(value, list):
        return [_resolve_package_specs(row, package_root, bindings) for row in value]
    if not isinstance(value, dict):
        return copy.deepcopy(value)
    if "package_path" in value:
        _require(set(value) <= {"package_path", "sha256", "relative_path", "role"},
                 "package_transport_spec_fields_invalid")
        relative = _safe_relative(value["package_path"])
        expected = str(value.get("sha256") or "")
        _require(bindings.get(relative) == expected, "package_transport_spec_not_manifest_bound")
        path = (package_root / relative).resolve(strict=True)
        path.relative_to(package_root.resolve())
        result = {key: copy.deepcopy(child) for key, child in value.items() if key != "package_path"}
        result["path"] = str(path)
        return result
    return {key: _resolve_package_specs(child, package_root, bindings)
            for key, child in value.items()}


def _ensure_exact_batch(repo: Path, manifest: dict[str, Any], prepared_sha: str) -> dict[str, Any]:
    state = _capture_state(repo)
    active = [row for row in state.get("batches", {}).values() if row.get("status") == "active"]
    capture_ids = [row["capture_id"] for row in manifest["captures"]]
    if active:
        _require(len(active) == 1 and active[0].get("study_date") == manifest["study_date"]
                 and active[0].get("capture_ids") == capture_ids,
                 "different_active_batch_blocks_enhancement")
        return active[0]
    started = capture_writer.start_batch(
        manifest["study_date"], f"enhancement-start:{prepared_sha}",
        capture_ids=capture_ids, repo_root=repo,
    )
    _require(started.get("status") in {"CURATION_BATCH_STARTED", "ALREADY_STARTED"}
             and started.get("capture_ids") == capture_ids,
             "exact_enhancement_batch_not_started")
    return ( _capture_state(repo).get("batches", {}).get(started["batch_id"]) or started )


def _concept_session_id(item: dict[str, Any]) -> str:
    return "CAPREVIEW-" + hashlib.sha256(item["capture_id"].encode()).hexdigest()[:24]


def _concept_payload(item: dict[str, Any], evidence: dict[str, Any], study_date: str, repo: Path) -> dict[str, Any]:
    opened = evidence.get("package") or {}
    turns = (opened.get("conversation") or {}).get("turns") or []
    conversation = [{"role": row["role"], "text": row["text"]} for row in turns]
    package_dir = Path(str(opened.get("package_dir") or ""))
    artifacts = []
    for row in (opened.get("manifest") or {}).get("attachments") or []:
        path = (package_dir / str(row["relative_path"])).resolve(strict=True)
        path.relative_to(package_dir.resolve())
        artifacts.append({"path": str(path), "sha256": row["sha256"], "role": row["role"]})
    concept = item["concept_review"]
    start, end = concept.get("started_at"), concept.get("ended_at")
    time_quality = "session_interval"
    if start is None or end is None:
        require_both_unknown = start is None and end is None
        _require(require_both_unknown, "concept_time_bounds_must_both_be_known_or_null")
        capture = _capture_state(repo)["captures"][item["capture_id"]]
        recorded = capture["recorded_at"]
        envelope = (opened.get("source") or {}).get("source") or {}
        actual_source = envelope.get("source", {}) if envelope.get("entry_kind") == "study_episode_capture" else envelope
        start = actual_source.get("displayed_at") or recorded
        end = recorded
        time_quality = "recorded_bounds_not_user_timestamps"
    return {
        "schema": concept_review.SCHEMA,
        "subject": "408",
        "session_id": _concept_session_id(item),
        "study_date": study_date,
        "started_at": start,
        "ended_at": end,
        "time_quality": time_quality,
        "conversation": conversation,
        "artifacts": artifacts,
        "observations": copy.deepcopy(concept["observations"]),
    }


def _event_line(repo: Path, event_id: str) -> tuple[dict[str, Any], str]:
    path = capture_writer.capture_root(repo) / "events.jsonl"
    matches = []
    for line in path.read_bytes().splitlines(keepends=True):
        if not line.strip():
            continue
        value = json.loads(line)
        if value.get("event_id") == event_id:
            matches.append((value, hashlib.sha256(line).hexdigest()))
    _require(len(matches) == 1, "capture_result_event_not_unique")
    return matches[0]


def _canonical_with_verified_first_date(repo: Path, canonical: dict[str, Any]) -> dict[str, Any]:
    value = copy.deepcopy(canonical)
    if value.get("mode") == "redo" and "first_done_date" not in value:
        row = Repo(repo).master().get(str(value.get("formal_id") or ""))
        _require(row is not None, "redo_first_date_requires_existing_formal_node")
        value["first_done_date"] = row.get("首次做题日期") or "未记录"
    return value


def _process_item(repo: Path, details_root: Path, package_root: Path,
                  bindings: dict[str, str], manifest: dict[str, Any], item: dict[str, Any],
                  batch: dict[str, Any], prepared_sha: str) -> dict[str, Any]:
    capture_id = item["capture_id"]
    capture_meta = next(row for row in manifest["captures"] if row["capture_id"] == capture_id)
    decision_value = _resolve_package_specs(item["decision"], package_root, bindings)
    if isinstance(decision_value.get("canonical_package"), dict):
        canonical = _canonical_with_verified_first_date(repo, decision_value["canonical_package"])
        decision_value["canonical_package"] = canonical
        binding = {
            "batch_id": batch["batch_id"], "capture_id": capture_id,
            "capture_payload_sha256": capture_meta["capture_payload_sha256"],
            "capture_set_sha256": batch["capture_set_sha256"],
        }
        canonical["curation_evidence_binding"] = copy.deepcopy(binding)
        canonical["capture_binding"] = copy.deepcopy(binding)
        for field in ("batch_id", "capture_id", "capture_payload_sha256"):
            if field in canonical:
                canonical[field] = binding[field]
    sealed = decisions.seal_decision(
        repo=repo, batch_id=batch["batch_id"], capture_id=capture_id,
        evidence_locator=capture_meta["evidence_locator"],
        evidence_sha256=capture_meta["evidence_sha256"], decision=decision_value,
    )
    action = item["action"]
    outcome: str
    formal_id: str | None = item.get("formal_id")
    receipt_sha: str | None = None
    verification_sha: str | None = None
    ensure: dict[str, Any] | None = None
    final: dict[str, Any] | None = None
    concept_source_id: str | None = None
    concept_receipt_sha: str | None = None
    if action in {"new", "redo", "correct_review", "already_applied"}:
        ensure = backflow.ensure_review_backflow(
            repo_root=repo, batch_id=batch["batch_id"], capture_id=capture_id,
            policy_version=review.SCHEDULING_POLICY_VERSION,
            idempotency_key=f"enhancement-ensure:{prepared_sha}:{capture_id}",
            decision_locator=sealed["locator"], decision_sha256=sealed["sha256"],
        )
    if action in {"new", "redo"}:
        canonical = decision_value["canonical_package"]
        transport = {**canonical, "sol_decision_locator": sealed["locator"],
                     "sol_decision_sha256": sealed["sha256"]}
        transport_path = (
            details_root / ROOT_NAME / "transports" / prepared_sha
            / f"{sealed['sha256']}.json"
        )
        if transport_path.exists():
            _require(_read_json(transport_path, "formal_transport") == transport,
                     "formal_transport_content_conflict")
        else:
            _atomic(transport_path, transport)
        apply_function = getattr(formal_writer.apply_batch, "__wrapped__", formal_writer.apply_batch)
        applied = apply_function(
            [transport], repo_root=repo, details_root=details_root,
            idempotency_key=f"enhancement-formal:{prepared_sha}:{capture_id}",
            manifest_meta={"input_path": str(transport_path)}, dry_run=False,
            refresh_bridge=False,
        )
        _require(applied.status in {"COMMITTED", "COMMITTED_DISPLAY_ASSETS_PENDING"},
                 "formal_writer_not_committed")
        _require(applied.receipt_path is not None and Path(applied.receipt_path).is_file(),
                 "formal_receipt_missing")
        formal_id = applied.formal_ids[0]
        receipt_sha = _digest_file(Path(applied.receipt_path))
        final = backflow.bind_formal_intake(
            repo_root=repo, batch_id=batch["batch_id"], capture_id=capture_id,
            ensure_receipt_sha256=ensure["receipt_sha256"],
            formal_result_status="curated", formal_result_receipt_sha256=receipt_sha,
            formal_id=formal_id, formal_proof_path=applied.receipt_path,
            idempotency_key=f"enhancement-bind:{prepared_sha}:{capture_id}",
        )
        _require(final.get("status") in {"satisfied", "maintenance_noop"},
                 "formal_backflow_pending")
        outcome = "curated"
    elif action in {"correct_review", "already_applied"}:
        verification = decisions.seal_already_current(
            repo=repo, decision_locator=sealed["locator"], formal_id=formal_id,
        )
        verification_sha = verification["verification_sha256"]
        final = backflow.bind_formal_intake(
            repo_root=repo, batch_id=batch["batch_id"], capture_id=capture_id,
            ensure_receipt_sha256=ensure["receipt_sha256"],
            formal_result_status="already_current",
            formal_result_receipt_sha256=verification_sha, formal_id=formal_id,
            formal_proof_path=verification["verification_path"],
            idempotency_key=f"enhancement-bind:{prepared_sha}:{capture_id}",
        )
        _require(final.get("status") in {"satisfied", "maintenance_noop"},
                 "already_current_backflow_pending")
        outcome = "already_current"
    elif action == "concept_only":
        evidence = decisions.reopen_direct_evidence(
            capture_meta["evidence_locator"], capture_meta["evidence_sha256"]
        )
        committed = concept_review.commit(
            repo, _concept_payload(item, evidence, manifest["study_date"], repo), followup=False,
        )
        concept_receipt_sha = committed["session_sha256"]
        concept_source_id = "CR408-" + concept_review.digest(_concept_session_id(item))
        outcome = "concept_only"
    else:
        outcome = "needs_user"
    marked = capture_writer.mark_result(
        batch_id=batch["batch_id"], capture_id=capture_id, outcome=outcome,
        idempotency_key=f"enhancement-result:{prepared_sha}:{capture_id}",
        formal_id=formal_id if outcome in {"curated", "already_current"} else None,
        receipt_sha256=receipt_sha, verification_sha256=verification_sha,
        reason=item["needs_user_reason"] if outcome == "needs_user" else None,
        review_schedule_ensure_receipt_sha256=(ensure or {}).get("receipt_sha256"),
        formal_review_binding_receipt_sha256=(final or {}).get("receipt_sha256"),
        concept_source_id=concept_source_id, concept_receipt_sha256=concept_receipt_sha,
        repo_root=repo,
    )
    event_id = "CE-" + hashlib.sha256(("result:" + f"enhancement-result:{prepared_sha}:{capture_id}").encode()).hexdigest()[:20]
    event, event_sha = _event_line(repo, event_id)
    terminal_function = getattr(decisions.terminal_binding, "__wrapped__", decisions.terminal_binding)
    terminal = terminal_function(
        repo=repo, decision_locator=sealed["locator"],
        item_result_event_id=event_id, item_result_event_sha256=event_sha,
        outcome=outcome, formal_id=formal_id if outcome in {"curated", "already_current"} else None,
        normal_receipt_sha256=receipt_sha, verification_sha256=verification_sha,
        concept_source_id=concept_source_id, concept_receipt_sha256=concept_receipt_sha,
    )
    return {
        "status": "COMPLETE" if outcome != "needs_user" else "NEEDS_USER",
        "action": action, "capture_id": capture_id, "formal_id": formal_id,
        "logical_question_id": item.get("logical_question_id"),
        "decision": sealed, "ensure_receipt_sha256": (ensure or {}).get("receipt_sha256"),
        "formal_receipt_sha256": receipt_sha, "verification_sha256": verification_sha,
        "formal_binding_receipt_sha256": (final or {}).get("receipt_sha256"),
        "concept_source_id": concept_source_id,
        "concept_receipt_sha256": concept_receipt_sha,
        "result_event_id": event["event_id"], "result_event_sha256": event_sha,
        "terminal": terminal, "mark_result": marked,
    }


def _has_unresolved_user_items(repo: Path, state: dict[str, Any]) -> bool:
    pending = [cid for cid, row in (state.get("items") or {}).items() if row.get("status") == "NEEDS_USER"]
    if not pending:
        return False
    current = _capture_state(repo).get("captures", {})
    return any(current.get(cid, {}).get("quality_status") != "user_removed" for cid in pending)


def _render_apply_result(
    receipt: dict[str, Any], prepared_sha: str, state: dict[str, Any]
) -> dict[str, Any]:
    return {
        "schema": RESULT_SCHEMA,
        "status": state["status"],
        "prepared_sha256": prepared_sha,
        "package_sha256": receipt["package_sha256"],
        "batch": state.get("batch"),
        "items": state.get("items") or {},
        "formal_bindings": state.get("formal_bindings") or {},
        "profile_receipt": state.get("profile_receipt"),
        "batch_close": state.get("batch_close"),
        "archives": state.get("archives") or {},
        "archive_status": "COMPLETE" if _archives_complete(state) else "PENDING",
        "campaign_successors": state.get("campaign_successors") or {},
        "followup": state.get("followup"),
        "morning_target_date": state.get("morning_target_date"),
        "morning_input": state.get("morning_input"),
        "web_execution_report": state.get("web_execution_report"),
        "read_maintenance_status": (
            (state.get("web_execution_report") or {}).get("status")
            or "report_not_provided"
        ),
        "formal_completion_status": state.get(
            "formal_completion_status", state.get("status")
        ),
        "needs_user": _has_unresolved_user_items(Path(receipt["repo_root"]), state),
        "formal_apply_replayed": False,
        "model_call_count": 0,
    }


def _campaign_successors_complete(state: dict[str, Any]) -> bool:
    required = {cid for cid, row in (state.get("items") or {}).items()
                if row.get("action") in {"new", "redo", "correct_review", "already_applied"}}
    return required <= set(state.get("campaign_successors") or {})


def _ensure_campaign_successors(repo: Path, receipt: dict[str, Any], prepared_sha: str,
                                state: dict[str, Any]) -> None:
    if _campaign_successors_complete(state):
        return
    import campaign_admission_backflow_408 as admission
    from campaign_admission_scheduler_bridge_408 import bridge as bridge_admission, register_admission
    candidates = [(cid, row) for cid, row in (state.get("items") or {}).items()
                  if row.get("action") in {"new", "redo", "correct_review", "already_applied"}]
    candidates.sort(key=lambda pair: (pair[1].get("action") != "new", pair[0]))
    # All new rows already exist in the formal table. Admit the complete new
    # cohort before any forecast validates its baseline denominator.
    newly_admitted = {}
    for cid, row in candidates:
        if row.get("action") == "new" and cid not in (state.get("campaign_successors") or {}):
            newly_admitted[cid] = admission.recover_campaign_admission(
                repo, batch_id=state["batch"]["batch_id"], capture_id=cid)
            admission_sha = newly_admitted[cid].get("admission_receipt_sha256")
            _require(admission_sha, "campaign_successor_admission_not_ready:" + cid)
            register_admission(repo, admission_receipt_sha256=admission_sha)
    for cid, row in candidates:
        if cid in (state.get("campaign_successors") or {}):
            continue
        recovered = newly_admitted.get(cid) or admission.recover_campaign_admission(
            repo, batch_id=state["batch"]["batch_id"], capture_id=cid)
        admission_sha = recovered.get("admission_receipt_sha256")
        _require(admission_sha, "campaign_successor_admission_not_ready:" + cid)
        if recovered.get("status") == admission.STATUS_COMPLETE:
            result = recovered
        else:
            _require(recovered.get("status") == admission.STATUS_FORECAST_PENDING,
                     "campaign_successor_not_ready:" + cid)
            result = bridge_admission(repo, admission_receipt_sha256=admission_sha,
                target_date=state["morning_target_date"],
                output_dir=Path(receipt["details_root"]) / ROOT_NAME / "campaign-successors" / prepared_sha / cid)
            _require(result.get("status") == "SCHEDULER_ADMISSION_FORECAST_BOUND",
                     "campaign_successor_forecast_pending:" + cid)
        state.setdefault("campaign_successors", {})[cid] = {**result, "admission_receipt_sha256": admission_sha}
        _save_operation(receipt, prepared_sha, state)


def _postcommit_event_id(state: dict[str, Any], prepared_sha: str) -> str:
    base = "enhancement-408:" + prepared_sha
    successors = state.get("campaign_successors") or {}
    if not successors:
        return base
    # A repaired admission layer must publish a fresh snapshot even when the
    # formal/profile versions were already published before that layer existed.
    identity = {cid: row["admission_receipt_sha256"] for cid, row in successors.items()}
    return base + ":admissions-" + _digest_value(identity)[:16]


def _archives_complete(state: dict[str, Any]) -> bool:
    archives = state.get("archives") or {}
    for cid, row in (state.get("items") or {}).items():
        if row.get("status") != "COMPLETE":
            continue
        proof = archives.get(cid) or {}
        terminal_sha = (row.get("terminal") or {}).get("sha256")
        if (not terminal_sha or proof.get("terminal_sha256") != terminal_sha
                or proof.get("status") != "COMPLETE"
                or not proof.get("packages")
                or any(result.get("status") != "ARCHIVED_AND_LOCATOR_BOUND"
                       for result in proof["packages"].values())):
            return False
    return True


def _ensure_archives(repo: Path, receipt: dict[str, Any], prepared_sha: str,
                     state: dict[str, Any]) -> None:
    """Archive only the committed batch's original and frozen supplement evidence."""
    if _archives_complete(state):
        return
    import archive_408_conversation_package as archive
    current = _capture_state(repo)
    batch = current["batches"][state["batch"]["batch_id"]]
    archived = state.setdefault("archives", {})
    for cid, row in (state.get("items") or {}).items():
        if row.get("status") != "COMPLETE":
            continue
        if _archives_complete({"items": {cid: row}, "archives": archived}):
            continue
        terminal = row.get("terminal") or {}
        proof = {"status": "PENDING", "terminal_sha256": terminal.get("sha256"),
                 "packages": {}, "formal_write_count": 0}
        try:
            _require(terminal.get("locator") and terminal.get("sha256"),
                     "archive_terminal_binding_missing:" + cid)
            capture = current["captures"][cid]["capture"]
            refs = list(capture.get("stable_evidence_refs") or [])
            refs.extend((batch.get("supplement_evidence") or {}).get(cid, []))
            packages = {}
            for ref in refs:
                locator = str(ref.get("locator") or "")
                if locator.startswith("cs408-conversation-package://"):
                    _require(locator not in packages or packages[locator] == ref["sha256"],
                             "archive_package_identity_conflict:" + cid)
                    packages[locator] = ref["sha256"]
            if packages:
                for locator, digest in packages.items():
                    proof["packages"][locator] = archive.archive_after_formal_commit(
                        repo=repo, package_locator=locator, package_sha256=digest,
                        formal_ids=[row["formal_id"]] if row.get("formal_id") else [],
                        formal_terminal_locator=terminal["locator"],
                        formal_terminal_sha256=terminal["sha256"],
                        details_root=receipt["details_root"],
                    )
            else:
                preview = archive.archive_legacy_evidence_after_formal_commit(
                    repo=repo, capture_id=cid)
                result = preview
                if preview.get("status") == archive.LEGACY_PREVIEW_STATUS:
                    result = archive.archive_legacy_evidence_after_formal_commit(
                        repo=repo, capture_id=cid, apply=True,
                        authorization=preview["authorization"])
                proof["packages"][str(preview.get("evidence_locator") or cid)] = result
            if all(r.get("status") == "ARCHIVED_AND_LOCATOR_BOUND"
                   for r in proof["packages"].values()):
                proof["status"] = "COMPLETE"
        except Exception as exc:
            proof["reason"] = f"{type(exc).__name__}:{exc}"
        archived[cid] = proof
        _save_operation(receipt, prepared_sha, state)


def _finish_postcommit(
    *,
    repo: Path,
    receipt: dict[str, Any],
    prepared_sha: str,
    state: dict[str, Any],
    morning_exporter: Callable[..., dict[str, Any]] | None,
) -> dict[str, Any]:
    try:
        _ensure_campaign_successors(repo, receipt, prepared_sha, state)
    except Exception as exc:
        state["status"] = "FORMAL_BOUND_CAMPAIGN_ADMISSION_PENDING"
        state["formal_completion_status"] = "FORMAL_BOUND_CAMPAIGN_ADMISSION_PENDING"
        state["last_error"] = f"{type(exc).__name__}:{exc}"
        _save_operation(receipt, prepared_sha, state)
        _, result_path = _operation_paths(receipt, prepared_sha)
        result = _render_apply_result(receipt, prepared_sha, state)
        _atomic(result_path, result)
        return {**result, "receipt_path": str(result_path)}
    try:
        _ensure_archives(repo, receipt, prepared_sha, state)
        archive_error = "formal_committed_archive_pending"
    except Exception as exc:
        archive_error = f"{type(exc).__name__}:{exc}"
    if not _archives_complete(state):
        state["status"] = "FORMAL_COMPLETE_ARCHIVE_PENDING"
        state["formal_completion_status"] = "FORMAL_COMPLETE_ARCHIVE_PENDING"
        state["last_error"] = archive_error
        _save_operation(receipt, prepared_sha, state)
        _, result_path = _operation_paths(receipt, prepared_sha)
        result = _render_apply_result(receipt, prepared_sha, state)
        _atomic(result_path, result)
        return {**result, "receipt_path": str(result_path)}
    operation_event_id = _postcommit_event_id(state, prepared_sha)
    publication = _publication_receipt(
        repo, operation_event_id, state.get("followup") or {}
    )
    if publication is None:
        state["followup"] = complete_after_commit(repo, operation_event_id)
        _save_operation(receipt, prepared_sha, state)
        publication = _publication_receipt(
            repo, operation_event_id, state.get("followup") or {}
        )
    needs_user = _has_unresolved_user_items(repo, state)
    if publication is None:
        state["status"] = "POSTCOMMIT_PENDING"
        state["formal_completion_status"] = "POSTCOMMIT_PENDING"
        state["last_error"] = "profile_committed_refresh_or_publication_pending"
    else:
        morning = _ensure_morning_input(
            repo=repo,
            state=state,
            target_date=str(state["morning_target_date"]),
            publication=publication,
            exporter=morning_exporter,
        )
        if morning.get("status") == "READY_FOR_WEB_PREPARATION":
            completed_status = (
                "COMPLETE_WITH_NEEDS_USER" if needs_user else "COMPLETE"
            )
            state["formal_completion_status"] = completed_status
            if (state.get("web_execution_report") or {}).get("status") \
                    == "READ_MAINTENANCE_PENDING":
                state["status"] = "READ_MAINTENANCE_PENDING"
                state["last_error"] = "web_read_maintenance_pending"
            else:
                state["status"] = completed_status
                state["last_error"] = None
        else:
            state["status"] = "FORMAL_COMPLETE_MORNING_INPUT_PENDING"
            state["formal_completion_status"] = "FORMAL_COMPLETE_MORNING_INPUT_PENDING"
            state["last_error"] = str(
                morning.get("reason") or "morning_input_export_pending"
            )
    _save_operation(receipt, prepared_sha, state)
    _, result_path = _operation_paths(receipt, prepared_sha)
    result = _render_apply_result(receipt, prepared_sha, state)
    _atomic(result_path, result)
    return {**result, "receipt_path": str(result_path)}


@defer_automatic_followups()
def apply(*, prepared: str | Path, repo: str | Path | None = None,
          details_root: str | Path | None = None,
          morning_target_date: str | None = None,
          morning_exporter: Callable[..., dict[str, Any]] | None = None) -> dict[str, Any]:
    receipt, _, prepared_sha = _read_prepared(prepared, details_root)
    repo_path = Path(repo or receipt["repo_root"]).expanduser().resolve()
    details = Path(receipt["details_root"]).expanduser().resolve()
    _require(repo_path == Path(receipt["repo_root"]).resolve(), "prepared_repo_override_forbidden")
    package_root, manifest, enhancement, updates, bindings = _revalidate_package(receipt)
    state = _load_operation(receipt, prepared_sha)
    report_inspection = _inspect_web_execution(
        package_root, manifest, repo_path
    )
    prepared_report = receipt.get("web_execution_report")
    if prepared_report is not None:
        _require(prepared_report == report_inspection,
                 "prepared_web_execution_report_drift")
    report_changed = state.get("web_execution_report") != report_inspection
    state["web_execution_report"] = report_inspection
    target_date, target_created = _resolve_morning_target(
        state, morning_target_date
    )
    if target_created or report_changed:
        _save_operation(receipt, prepared_sha, state)
    local_completion_required = bool(
        report_inspection.get("local_completion_required")
        or report_inspection.get("unassigned_local_completion_required")
    )
    if local_completion_required and not _formal_layers_complete(state):
        state["status"] = "LOCAL_COMPLETION_REQUIRED"
        state["formal_completion_status"] = (
            "PARTIAL_FORMAL_COMMIT_PRESERVED"
            if state.get("items")
            else "NOT_STARTED"
        )
        state["last_error"] = "web_partial_requires_local_completion"
        _save_operation(receipt, prepared_sha, state)
        _, result_path = _operation_paths(receipt, prepared_sha)
        result = _render_apply_result(receipt, prepared_sha, state)
        _atomic(result_path, result)
        return {**result, "receipt_path": str(result_path)}
    if _formal_layers_complete(state):
        was_complete = state.get("status") in {
            "COMPLETE", "COMPLETE_WITH_NEEDS_USER"
        } and _archives_complete(state) and (state.get("morning_input") or {}).get("status") \
            == "READY_FOR_WEB_PREPARATION"
        result = _finish_postcommit(
            repo=repo_path,
            receipt=receipt,
            prepared_sha=prepared_sha,
            state=state,
            morning_exporter=morning_exporter,
        )
        return {**result, "status": "ALREADY_COMPLETE"} if was_complete and result.get("status") in {
            "COMPLETE", "COMPLETE_WITH_NEEDS_USER"
        } else result
    scope = _validate_manifest(repo_path, manifest)
    completed = {
        capture_id for capture_id, row in (state.get("items") or {}).items()
        if row.get("status") in {"COMPLETE", "NEEDS_USER"}
    }
    completed.update(
        row["capture_id"] for row in enhancement["items"]
        if row["action"] in {"new", "redo"}
        and _own_formal_commit_exists(repo_path, prepared_sha, row["capture_id"])
    )
    _validate_enhancement(
        repo_path, manifest, enhancement, scope["published"], completed=completed
    )
    evidence_bindings = _verified_evidence_bindings(
        manifest, bindings, scope["published"]
    )
    _require(evidence_bindings == receipt.get("evidence_bindings"),
             "prepared_evidence_bindings_drift")
    _validate_profile_question_sources(
        manifest, enhancement, updates, scope["published"]
    )
    profile_operation_id = "enhancement-profile:" + prepared_sha
    profile_operation_applied = profiles.operation_applied(
        repo_path, profile_operation_id
    )
    if state.get("profile_receipt") is None and not profile_operation_applied:
        profiles.validate_updates(
            repo_path, updates, expected_points=manifest["affected_points"],
            evidence_bindings=evidence_bindings, formal_bindings=None,
            check_versions=True,
        )
    try:
        batch = (
            state.get("batch")
            if state.get("batch_close") is not None
            else _ensure_exact_batch(repo_path, manifest, prepared_sha)
        )
        state.update(status="APPLYING", batch={key: batch.get(key) for key in (
            "batch_id", "study_date", "capture_ids", "capture_set_sha256")}, last_error=None)
        _save_operation(receipt, prepared_sha, state)
        by_capture = {row["capture_id"]: row for row in enhancement["items"]}
        for capture_id in manifest["captures"]:
            cid = capture_id["capture_id"]
            if (state.get("items") or {}).get(cid, {}).get("status") in {"COMPLETE", "NEEDS_USER"}:
                continue
            result = _process_item(
                repo_path, details, package_root, bindings, manifest,
                by_capture[cid], batch, prepared_sha,
            )
            state.setdefault("items", {})[cid] = result
            logical = result.get("logical_question_id")
            if logical and result.get("formal_id"):
                state.setdefault("formal_bindings", {})[logical] = {
                    "formal_id": result["formal_id"]
                }
            _save_operation(receipt, prepared_sha, state)
        if state.get("profile_receipt") is None:
            profile_receipt = profiles.apply_updates(
                repo_path, updates, operation_id=profile_operation_id,
                formal_bindings=state.get("formal_bindings") or {},
                evidence_bindings=evidence_bindings,
                expected_points=manifest["affected_points"],
            )
            state["profile_receipt"] = profile_receipt
            _save_operation(receipt, prepared_sha, state)
        needs_user = any(row.get("status") == "NEEDS_USER" for row in state["items"].values())
        if state.get("batch_close") is None:
            if state.get("coverage_refresh") is None and any(
                row.get("action") == "new" for row in state["items"].values()
            ):
                from refresh_coverage_index import refresh as refresh_coverage
                state["coverage_refresh"] = refresh_coverage(repo_path)
                _save_operation(receipt, prepared_sha, state)
            if needs_user:
                closed = capture_writer.close_batch(
                    batch_id=batch["batch_id"], global_closeout="failed",
                    idempotency_key=f"enhancement-close:{prepared_sha}", repo_root=repo_path,
                )
            else:
                audit = capture_writer.seal_global_audit(
                    batch_id=batch["batch_id"], repo_root=repo_path
                )
                closed = capture_writer.close_batch(
                    batch_id=batch["batch_id"], global_closeout="pass",
                    global_audit_receipt_sha256=audit["global_audit_receipt_sha256"],
                    idempotency_key=f"enhancement-close:{prepared_sha}", repo_root=repo_path,
                )
                closed["global_audit"] = audit
            state["batch_close"] = closed
            _save_operation(receipt, prepared_sha, state)
        return _finish_postcommit(
            repo=repo_path,
            receipt=receipt,
            prepared_sha=prepared_sha,
            state=state,
            morning_exporter=morning_exporter,
        )
    except Exception as exc:
        state["status"] = "PENDING_RECOVERY"
        state["last_error"] = f"{type(exc).__name__}:{exc}"
        _save_operation(receipt, prepared_sha, state)
        raise


def status(*, prepared: str | Path, details_root: str | Path | None = None) -> dict[str, Any]:
    receipt, path, prepared_sha = _read_prepared(prepared, details_root)
    package_root, manifest, _, _, _ = _revalidate_package(receipt)
    report_inspection = _inspect_web_execution(
        package_root, manifest, Path(receipt["repo_root"])
    )
    if receipt.get("web_execution_report") is not None:
        _require(
            receipt["web_execution_report"] == report_inspection,
            "prepared_web_execution_report_drift",
        )
    state = _load_operation(receipt, prepared_sha)
    _, result_path = _operation_paths(receipt, prepared_sha)
    published = _publication_complete(
        Path(receipt["repo_root"]), _postcommit_event_id(state, prepared_sha),
        state.get("followup") or {},
    )
    effective_status = state["status"]
    morning = state.get("morning_input") or {}
    morning_status = morning.get("status") or "NOT_STARTED"
    if morning_status == "READY_FOR_WEB_PREPARATION" and not _morning_paths_ready(morning):
        morning_status = "MORNING_INPUT_OUTPUT_MISSING"
    if published and _formal_layers_complete(state) and _campaign_successors_complete(state):
        needs_user = _has_unresolved_user_items(Path(receipt["repo_root"]), state)
        if morning_status == "READY_FOR_WEB_PREPARATION":
            effective_status = "COMPLETE_WITH_NEEDS_USER" if needs_user else "COMPLETE"
            if report_inspection.get("status") == "READ_MAINTENANCE_PENDING":
                effective_status = "READ_MAINTENANCE_PENDING"
        else:
            effective_status = "FORMAL_COMPLETE_MORNING_INPUT_PENDING"
    if _formal_layers_complete(state) and not _archives_complete(state):
        effective_status = "FORMAL_COMPLETE_ARCHIVE_PENDING"
    return {
        "schema": STATE_SCHEMA,
        "status": effective_status,
        "prepared_sha256": prepared_sha,
        "prepared_receipt_path": str(path),
        "package_sha256": receipt["package_sha256"],
        "batch": state.get("batch"),
        "item_statuses": {key: value.get("status") for key, value in state.get("items", {}).items()},
        "formal_bindings": state.get("formal_bindings") or {},
        "profile_status": (state.get("profile_receipt") or {}).get("status"),
        "batch_close_status": (state.get("batch_close") or {}).get("status"),
        "archive_status": "COMPLETE" if _archives_complete(state) else "PENDING",
        "archive_capture_statuses": {
            cid: row.get("status") for cid, row in (state.get("archives") or {}).items()
        },
        "followup": state.get("followup"),
        "publication_readback": "PUBLISHED" if published else "PENDING",
        "morning_target_date": state.get("morning_target_date"),
        "morning_input_status": morning_status,
        "morning_input_source_commit": morning.get("source_commit"),
        "morning_input_zip_path": morning.get("zip_path"),
        "morning_input_prompt_path": morning.get("prompt_path"),
        "morning_input_parts": morning.get("parts") or [],
        "morning_input_partition_set_path": morning.get("partition_set_path"),
        "web_execution_report": report_inspection,
        "read_maintenance_status": report_inspection.get("status"),
        "formal_completion_status": state.get("formal_completion_status"),
        "last_error": state.get("last_error"),
        "apply_receipt_path": str(result_path) if result_path.is_file() else None,
        "write_count": 0,
    }


def describe(repo: str | Path) -> dict[str, Any]:
    root = Path(repo).expanduser().resolve()
    dependencies = {
        "formal_writer": hasattr(formal_writer, "apply_batch"),
        "capture_writer": hasattr(capture_writer, "mark_result"),
        "concept_writer": hasattr(concept_review, "commit"),
        "profile_writer": hasattr(profiles, "apply_updates"),
        "review_backflow": hasattr(backflow, "ensure_review_backflow"),
        "deferred_morning_outcome": (
            hasattr(backflow, "_ensure_deferred_morning_outcome")
            and getattr(review, "DEFERRED_MORNING_BINDING_SCHEMA", None)
            == "cs408-deferred-morning-outcome-binding-v1"
        ),
        "publication_readback": hasattr(bridge, "_published_library"),
        "morning_input_exporter": Path(__file__).with_name(
            "export_morning_input_408.py"
        ).is_file(),
    }
    return {
        "schema": "cs408-formal-enhancement-describe-v1",
        "status": "AVAILABLE" if all(dependencies.values()) else "DEPENDENCY_MISSING",
        "contract": MANIFEST_SCHEMA,
        "enhancement_schema": ENHANCEMENT_SCHEMA,
        "profile_schema": profiles.UPDATE_SCHEMA,
        "actions": sorted(SUPPORTED_ACTIONS),
        "commands": ["prepare", "apply", "status", "describe"],
        "dependencies": dependencies,
        "repo_root": str(root),
        "formal_write_count": 0,
        "model_call_count": 0,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo", type=Path, default=Path(__file__).resolve().parents[1])
    parser.add_argument("--details-root", type=Path, default=Path(DETAILS_ROOT_DEFAULT))
    sub = parser.add_subparsers(dest="command", required=True)
    prepare_parser = sub.add_parser("prepare")
    prepare_parser.add_argument("--zip", required=True, dest="source")
    prepare_parser.add_argument("--resume-prepared", help="Preserve completed items while repairing an unfinished exact batch")
    apply_parser = sub.add_parser("apply")
    apply_parser.add_argument("--prepared", required=True)
    apply_parser.add_argument("--morning-date")
    status_parser = sub.add_parser("status")
    status_parser.add_argument("--prepared", required=True)
    sub.add_parser("describe")
    args = parser.parse_args(argv)
    try:
        if args.command == "prepare":
            result = prepare(source=args.source, repo=args.repo, details_root=args.details_root,
                             resume_prepared=args.resume_prepared)
        elif args.command == "apply":
            result = apply(
                prepared=args.prepared,
                repo=args.repo,
                details_root=args.details_root,
                morning_target_date=args.morning_date,
            )
        elif args.command == "status":
            result = status(prepared=args.prepared, details_root=args.details_root)
        else:
            result = describe(args.repo)
        print(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True))
        return 0
    except (EnhancementError, OSError, ValueError, RuntimeError, KeyError, TypeError) as exc:
        print(json.dumps({"status": "REJECTED", "reason": str(exc)}, ensure_ascii=False))
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
