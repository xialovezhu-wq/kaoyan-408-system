#!/usr/bin/env python3
"""Content-addressed, answer-isolated surfaces for formal 408 originals.

The module never creates or mutates a formal node.  It publishes an existing
formal node's local A-D question assets as a learner surface only after two
independent verifiers agree on the unique key.  The key and verifier material
are kept in a private answer capsule; learner-facing selections contain only a
capsule commitment.
"""

from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import os
import re
import tempfile
import unicodedata
from pathlib import Path
from typing import Any, Iterable


SUBMISSION_SCHEMA = "formal_original_surface_submission_v1"
BLIND_SCHEMA = "formal_original_blind_solve_verification_v1"
SOURCE_SCHEMA = "formal_original_source_solution_verification_v1"
SURFACE_SCHEMA = "formal_original_learner_surface_v1"
CAPSULE_SCHEMA = "formal_original_answer_capsule_v1"
MANIFEST_SCHEMA = "formal_original_surface_manifest_v1"
QUARANTINE_SCHEMA = "formal_original_surface_quarantine_v1"
POOL_SCHEMA = "formal_original_surface_pool_status_v1"
SELECTION_SCHEMA = "formal_original_surface_selection_v1"
CONSUMPTION_SCHEMA = "formal_original_surface_consumption_v1"

FORMAL_NODE_RE = re.compile(r"^(?:DS|CO|OS|CN)_(?:\d{4}|UNK)_\d{3}$")
SHA_RE = re.compile(r"^[0-9a-f]{64}$")
SAFE_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:-]{2,127}$")
FOS_RE = re.compile(r"^FOS-[0-9A-F]{24}$")
ANSWER_LEAK_RE = re.compile(
    r"(?:正确答案|参考答案|答案(?:是|为)|correct\s+answer|answer\s*[:：])\s*[A-D]",
    re.IGNORECASE,
)
LABELS = ("A", "B", "C", "D")
MAX_JSON_BYTES = 4 * 1024 * 1024
STORE_REL = Path("formal-original-surfaces/v1")


class FormalOriginalSurfaceError(RuntimeError):
    """Stable fail-closed error for the formal-original surface layer."""


def _canonical_bytes(value: Any) -> bytes:
    return (
        json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        + "\n"
    ).encode("utf-8")


def _pretty_bytes(value: Any) -> bytes:
    return (
        json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2) + "\n"
    ).encode("utf-8")


def _sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _digest(value: Any) -> str:
    return _sha256_bytes(_canonical_bytes(value))


def _normalize(value: object) -> str:
    return re.sub(
        r"\s+", " ", unicodedata.normalize("NFKC", str(value or ""))
    ).strip()


def _root(path: str | Path) -> tuple[Path, Path]:
    details = Path(path).expanduser().resolve()
    details.mkdir(parents=True, exist_ok=True)
    if details.is_symlink() or not details.is_dir():
        raise FormalOriginalSurfaceError("details_root_unsafe")
    store = details / STORE_REL
    store.mkdir(parents=True, exist_ok=True)
    return details, store


def _relative(path: Path, root: Path) -> str:
    return path.resolve(strict=True).relative_to(root.resolve(strict=True)).as_posix()


def _managed_ref(root: Path, ref: str | Path, label: str) -> Path:
    raw = Path(str(ref))
    if ".." in raw.parts:
        raise FormalOriginalSurfaceError(f"{label}_unsafe_ref")
    lexical = raw if raw.is_absolute() else root / raw
    if lexical.is_symlink():
        raise FormalOriginalSurfaceError(f"{label}_unsafe_ref")
    try:
        path = lexical.resolve(strict=True)
        path.relative_to(root.resolve(strict=True))
    except (OSError, ValueError) as exc:
        raise FormalOriginalSurfaceError(f"{label}_outside_details_root") from exc
    if path.is_symlink() or not path.is_file():
        raise FormalOriginalSurfaceError(f"{label}_missing_or_unsafe")
    return path


def _load_json(path: Path, label: str) -> dict[str, Any]:
    if path.is_symlink() or not path.is_file():
        raise FormalOriginalSurfaceError(f"{label}_missing_or_unsafe")
    raw = path.read_bytes()
    if not 0 < len(raw) <= MAX_JSON_BYTES:
        raise FormalOriginalSurfaceError(f"{label}_size_invalid")
    try:
        value = json.loads(raw)
    except (UnicodeError, json.JSONDecodeError) as exc:
        raise FormalOriginalSurfaceError(f"{label}_invalid_json") from exc
    if not isinstance(value, dict):
        raise FormalOriginalSurfaceError(f"{label}_root_not_object")
    return value


def _atomic_write(path: Path, payload: bytes, *, private: bool = False) -> str:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700 if private else 0o755)
    if private:
        os.chmod(path.parent, 0o700)
    descriptor, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        if private:
            os.fchmod(descriptor, 0o600)
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
        if private:
            os.chmod(path, 0o600)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)
    return _sha256_bytes(payload)


def _write_immutable(path: Path, value: Any, *, private: bool = False) -> str:
    payload = _pretty_bytes(value)
    digest = _sha256_bytes(payload)
    if path.exists():
        if path.is_symlink() or not path.is_file() or path.read_bytes() != payload:
            raise FormalOriginalSurfaceError("immutable_object_conflict")
        return digest
    return _atomic_write(path, payload, private=private)


def _content_write(
    directory: Path, value: Any, *, private: bool = False
) -> tuple[Path, str]:
    payload = _pretty_bytes(value)
    digest = _sha256_bytes(payload)
    path = directory / f"{digest}.json"
    _write_immutable(path, value, private=private)
    return path, digest


def _content_ref(
    root: Path, ref: str | Path, label: str
) -> tuple[dict[str, Any], Path, str]:
    path = _managed_ref(root, ref, label)
    digest = _sha256_file(path)
    if path.suffix != ".json" or path.stem != digest:
        raise FormalOriginalSurfaceError(f"{label}_content_address_drift")
    return _load_json(path, label), path, digest


def _iso_date(value: object, label: str) -> dt.date:
    try:
        return dt.date.fromisoformat(str(value or ""))
    except ValueError as exc:
        raise FormalOriginalSurfaceError(f"{label}_invalid") from exc


def _asset_rows(
    root: Path, value: object, label: str, *, required: bool
) -> list[dict[str, Any]]:
    if not isinstance(value, list) or (required and not value):
        raise FormalOriginalSurfaceError(f"{label}_required")
    rows: list[dict[str, Any]] = []
    for row in value:
        if not isinstance(row, dict):
            raise FormalOriginalSurfaceError(f"{label}_invalid")
        ref = str(row.get("ref") or "")
        expected_sha = str(row.get("sha256") or "")
        path = _managed_ref(root, ref, label)
        if not SHA_RE.fullmatch(expected_sha) or _sha256_file(path) != expected_sha:
            raise FormalOriginalSurfaceError(f"{label}_hash_drift")
        size = path.stat().st_size
        if row.get("size_bytes") is not None and row.get("size_bytes") != size:
            raise FormalOriginalSurfaceError(f"{label}_size_drift")
        rows.append({"ref": _relative(path, root), "sha256": expected_sha, "size_bytes": size})
    if len({row["ref"] for row in rows}) != len(rows):
        raise FormalOriginalSurfaceError(f"{label}_duplicate")
    return rows


def _safe_text_surface(value: object) -> dict[str, Any] | None:
    if value is None:
        return None
    if not isinstance(value, dict):
        raise FormalOriginalSurfaceError("answer_safe_content_invalid")
    stem = str(value.get("stem") or "").strip()
    options = value.get("options")
    if not stem or ANSWER_LEAK_RE.search(stem) or not isinstance(options, list):
        raise FormalOriginalSurfaceError("answer_safe_content_invalid_or_leaks")
    normalized: list[dict[str, str]] = []
    for row in options:
        if not isinstance(row, dict):
            raise FormalOriginalSurfaceError("answer_safe_option_invalid")
        label = str(row.get("label") or "")
        text = str(row.get("text") or "").strip()
        if label not in LABELS or not text or ANSWER_LEAK_RE.search(text):
            raise FormalOriginalSurfaceError("answer_safe_option_invalid_or_leaks")
        normalized.append({"label": label, "text": text})
    if tuple(row["label"] for row in normalized) != LABELS:
        raise FormalOriginalSurfaceError("four_choice_labels_required")
    if len({_normalize(row["text"]) for row in normalized}) != 4:
        raise FormalOriginalSurfaceError("four_distinct_options_required")
    extra = set(value) - {"stem", "options"}
    if extra:
        raise FormalOriginalSurfaceError("answer_safe_content_has_unapproved_fields")
    return {"stem": stem, "options": normalized}


def _normalize_submission(root: Path, submission: dict[str, Any]) -> dict[str, Any]:
    node_id = str(submission.get("formal_node_id") or "")
    node_sha = str(submission.get("formal_node_sha256") or "")
    details_ref = str(submission.get("details_card_ref") or "")
    details_sha = str(submission.get("details_card_sha256") or "")
    exposure_identity = str(submission.get("exact_exposure_identity") or "")
    if (
        submission.get("schema") != SUBMISSION_SCHEMA
        or not FORMAL_NODE_RE.fullmatch(node_id)
        or not SHA_RE.fullmatch(node_sha)
        or not SAFE_ID_RE.fullmatch(exposure_identity)
        or submission.get("question_kind") != "single_choice_4"
        or submission.get("option_labels") != list(LABELS)
    ):
        raise FormalOriginalSurfaceError("submission_identity_or_four_choice_invalid")
    raw_study_date = submission.get("study_date")
    if raw_study_date in {None, "", "未记录", "unknown", "unrecorded"}:
        study_date = None
    else:
        study_date = _iso_date(raw_study_date, "study_date").isoformat()
    details_path = _managed_ref(root, details_ref, "details_card")
    if not SHA_RE.fullmatch(details_sha) or _sha256_file(details_path) != details_sha:
        raise FormalOriginalSurfaceError("details_card_hash_drift")
    questions = _asset_rows(
        root, submission.get("question_asset_refs", []), "question_asset", required=False
    )
    solutions = _asset_rows(
        root, submission.get("solution_asset_refs"), "solution_asset", required=True
    )
    safe_content = _safe_text_surface(submission.get("answer_safe_content"))
    if not questions and safe_content is None:
        raise FormalOriginalSurfaceError("question_asset_or_answer_safe_content_required")
    normalized = {
        "schema": SUBMISSION_SCHEMA,
        "formal_node_id": node_id,
        "formal_node_sha256": node_sha,
        "details_card_ref": _relative(details_path, root),
        "details_card_sha256": details_sha,
        "question_kind": "single_choice_4",
        "option_labels": list(LABELS),
        "question_asset_refs": questions,
        "solution_asset_refs": solutions,
        "answer_safe_content": safe_content,
        "study_date": study_date,
        "exact_exposure_identity": exposure_identity,
        "formal_node_created": False,
        "formal_write_count": 0,
    }
    return normalized


def _verification(
    value: dict[str, Any], *, schema: str, submission_sha: str, label: str
) -> dict[str, Any]:
    verifier_identity = str(value.get("verifier_identity") or "")
    verification_id = str(value.get("verification_id") or "")
    option = str(value.get("derived_correct_option") or "")
    if (
        value.get("schema") != schema
        or value.get("submission_sha256") != submission_sha
        or not SAFE_ID_RE.fullmatch(verifier_identity)
        or not SAFE_ID_RE.fullmatch(verification_id)
        or option not in LABELS
        or value.get("option_labels") != list(LABELS)
        or value.get("unique_option") is not True
        or value.get("verdict") != "accept"
    ):
        raise FormalOriginalSurfaceError(f"{label}_invalid")
    normalized = {
        "schema": schema,
        "verification_id": verification_id,
        "verifier_identity": verifier_identity,
        "submission_sha256": submission_sha,
        "option_labels": list(LABELS),
        "derived_correct_option": option,
        "unique_option": True,
        "verdict": "accept",
    }
    if schema == BLIND_SCHEMA:
        if value.get("answer_key_exposed") is not False:
            raise FormalOriginalSurfaceError("blind_verification_not_blind")
        normalized["answer_key_exposed"] = False
    else:
        solution_hashes = value.get("verified_solution_asset_sha256s")
        if (
            not isinstance(solution_hashes, list)
            or not solution_hashes
            or any(not SHA_RE.fullmatch(str(item)) for item in solution_hashes)
        ):
            raise FormalOriginalSurfaceError("source_solution_verification_assets_invalid")
        normalized["verified_solution_asset_sha256s"] = list(solution_hashes)
    return normalized


def _true_item_id(submission: dict[str, Any]) -> str:
    identity = {
        "formal_node_id": submission["formal_node_id"],
        "formal_node_sha256": submission["formal_node_sha256"],
        "details_card_sha256": submission["details_card_sha256"],
        "question_assets": submission["question_asset_refs"],
        "answer_safe_content_sha256": (
            _digest(submission["answer_safe_content"])
            if submission["answer_safe_content"] is not None
            else None
        ),
        "exact_exposure_identity": submission["exact_exposure_identity"],
    }
    return "FOS-" + _digest(identity)[:24].upper()


def prepare_submission(
    details_root: str | Path, submission: dict[str, Any]
) -> dict[str, Any]:
    """Freeze normalized input and an answer-free packet for both verifiers."""

    details, store = _root(details_root)
    normalized = _normalize_submission(details, submission)
    submission_path, submission_sha = _content_write(
        store / "work" / "submissions", normalized
    )
    blind_packet = {
        "schema": "formal_original_blind_input_v1",
        "submission_sha256": submission_sha,
        "formal_node_id": normalized["formal_node_id"],
        "formal_node_sha256": normalized["formal_node_sha256"],
        "question_kind": "single_choice_4",
        "option_labels": list(LABELS),
        "question_asset_refs": normalized["question_asset_refs"],
        "answer_safe_content": normalized["answer_safe_content"],
        "answer_fields_included": False,
        "solution_assets_included": False,
        "formal_write_count": 0,
    }
    blind_path, blind_sha = _content_write(store / "work" / "blind-inputs", blind_packet)
    source_packet = {
        "schema": "formal_original_source_solution_input_v1",
        "submission_sha256": submission_sha,
        "formal_node_id": normalized["formal_node_id"],
        "formal_node_sha256": normalized["formal_node_sha256"],
        "question_kind": "single_choice_4",
        "option_labels": list(LABELS),
        "question_asset_refs": normalized["question_asset_refs"],
        "solution_asset_refs": normalized["solution_asset_refs"],
        "answer_safe_content": normalized["answer_safe_content"],
        "details_text_answer_authority": False,
        "formal_write_count": 0,
    }
    source_path, source_sha = _content_write(
        store / "work" / "source-solution-inputs", source_packet
    )
    return {
        "status": "prepared",
        "true_item_id": _true_item_id(normalized),
        "normalized_submission": normalized,
        "submission_ref": _relative(submission_path, details),
        "submission_sha256": submission_sha,
        "blind_input_ref": _relative(blind_path, details),
        "blind_input_sha256": blind_sha,
        "source_solution_input_ref": _relative(source_path, details),
        "source_solution_input_sha256": source_sha,
        "answer_fields_in_blind_input": False,
        "formal_write_count": 0,
    }


def _quarantine(
    details: Path,
    store: Path,
    submission: dict[str, Any],
    submission_sha: str,
    blind: dict[str, Any],
    source: dict[str, Any],
    reason: str,
) -> dict[str, Any]:
    true_item_id = _true_item_id(submission)
    record = {
        "schema": QUARANTINE_SCHEMA,
        "status": "quarantine",
        "reason": reason,
        "true_item_id": true_item_id,
        "formal_node_id": submission["formal_node_id"],
        "formal_node_sha256": submission["formal_node_sha256"],
        "submission_sha256": submission_sha,
        "blind_verification_commitment_sha256": _digest(blind),
        "source_solution_verification_commitment_sha256": _digest(source),
        "formal_node_created": False,
        "formal_write_count": 0,
    }
    path, digest = _content_write(store / "quarantine", record)
    return {
        **record,
        "quarantine_ref": _relative(path, details),
        "quarantine_sha256": digest,
    }


def publish(
    details_root: str | Path,
    submission: dict[str, Any],
    blind_solve_verification: dict[str, Any],
    source_solution_verification: dict[str, Any],
) -> dict[str, Any]:
    """Publish one verified local formal-original surface, or quarantine conflict."""

    details, store = _root(details_root)
    normalized = _normalize_submission(details, submission)
    # Verifiers bind the immutable on-disk object hash returned by
    # prepare_submission, not the separate compact canonical digest helper.
    _, submission_sha = _content_write(store / "work" / "submissions", normalized)
    blind = _verification(
        blind_solve_verification,
        schema=BLIND_SCHEMA,
        submission_sha=submission_sha,
        label="blind_solve_verification",
    )
    source = _verification(
        source_solution_verification,
        schema=SOURCE_SCHEMA,
        submission_sha=submission_sha,
        label="source_solution_verification",
    )
    if blind["verifier_identity"] == source["verifier_identity"]:
        return _quarantine(
            details,
            store,
            normalized,
            submission_sha,
            blind,
            source,
            "verifier_identity_not_independent",
        )
    expected_solution_hashes = [row["sha256"] for row in normalized["solution_asset_refs"]]
    if source["verified_solution_asset_sha256s"] != expected_solution_hashes:
        return _quarantine(
            details,
            store,
            normalized,
            submission_sha,
            blind,
            source,
            "source_solution_asset_binding_conflict",
        )
    if blind["derived_correct_option"] != source["derived_correct_option"]:
        return _quarantine(
            details,
            store,
            normalized,
            submission_sha,
            blind,
            source,
            "verifier_correct_option_conflict",
        )

    true_item_id = _true_item_id(normalized)
    surface = {
        "schema": SURFACE_SCHEMA,
        "true_item_id": true_item_id,
        "formal_node_id": normalized["formal_node_id"],
        "formal_node_sha256": normalized["formal_node_sha256"],
        "question_kind": "single_choice_4",
        "option_labels": list(LABELS),
        "question_asset_refs": normalized["question_asset_refs"],
        "answer_safe_content": normalized["answer_safe_content"],
        "study_date": normalized["study_date"],
        "exact_exposure_identity": normalized["exact_exposure_identity"],
        "answer_content_in_surface": False,
        "formal_node_created": False,
        "formal_write_count": 0,
    }
    surface_path, surface_sha = _content_write(store / "published" / "surfaces", surface)
    blind_path, blind_sha = _content_write(
        store / "private" / "verifications" / "blind", blind, private=True
    )
    source_path, source_sha = _content_write(
        store / "private" / "verifications" / "source-solution", source, private=True
    )
    capsule = {
        "schema": CAPSULE_SCHEMA,
        "true_item_id": true_item_id,
        "submission_sha256": submission_sha,
        "surface_sha256": surface_sha,
        "correct_option": blind["derived_correct_option"],
        "blind_verification_ref": _relative(blind_path, details),
        "blind_verification_sha256": blind_sha,
        "source_solution_verification_ref": _relative(source_path, details),
        "source_solution_verification_sha256": source_sha,
        "verifier_identities_distinct": True,
        "verifiers_agree_unique_option": True,
        "details_text_answer_trusted": False,
        "formal_write_count": 0,
    }
    capsule_path, capsule_sha = _content_write(
        store / "private" / "answer-capsules", capsule, private=True
    )
    manifest = {
        "schema": MANIFEST_SCHEMA,
        "status": "verified_ready",
        "true_item_id": true_item_id,
        "formal_node_id": normalized["formal_node_id"],
        "formal_node_sha256": normalized["formal_node_sha256"],
        "details_card_ref": normalized["details_card_ref"],
        "details_card_sha256": normalized["details_card_sha256"],
        "question_asset_refs": normalized["question_asset_refs"],
        "solution_asset_refs": normalized["solution_asset_refs"],
        "study_date": normalized["study_date"],
        "exact_exposure_identity": normalized["exact_exposure_identity"],
        "submission_sha256": submission_sha,
        "surface_ref": _relative(surface_path, details),
        "surface_sha256": surface_sha,
        "answer_capsule_ref": _relative(capsule_path, details),
        "answer_capsule_sha256": capsule_sha,
        "blind_verification_sha256": blind_sha,
        "source_solution_verification_sha256": source_sha,
        "answer_isolated": True,
        "formal_node_created": False,
        "formal_write_count": 0,
    }
    manifest_path, manifest_sha = _content_write(store / "published" / "manifests", manifest)
    return {
        "status": "published",
        "true_item_id": true_item_id,
        "manifest_ref": _relative(manifest_path, details),
        "manifest_sha256": manifest_sha,
        "surface_ref": manifest["surface_ref"],
        "surface_sha256": surface_sha,
        "answer_capsule_ref": manifest["answer_capsule_ref"],
        "answer_capsule_sha256": capsule_sha,
        "answer_isolated": True,
        "formal_node_created": False,
        "formal_write_count": 0,
    }


def _asset_bindings_current(details: Path, manifest: dict[str, Any]) -> None:
    details_path = _managed_ref(details, manifest.get("details_card_ref", ""), "details_card")
    if _sha256_file(details_path) != manifest.get("details_card_sha256"):
        raise FormalOriginalSurfaceError("details_card_hash_drift")
    for label in ("question_asset_refs", "solution_asset_refs"):
        rows = manifest.get(label)
        if not isinstance(rows, list):
            raise FormalOriginalSurfaceError(f"{label}_invalid")
        for row in rows:
            if not isinstance(row, dict):
                raise FormalOriginalSurfaceError(f"{label}_invalid")
            path = _managed_ref(details, row.get("ref", ""), label)
            if (
                _sha256_file(path) != row.get("sha256")
                or path.stat().st_size != row.get("size_bytes")
            ):
                raise FormalOriginalSurfaceError(f"{label}_hash_drift")


def _consumptions(details: Path, store: Path) -> dict[tuple[str, str], dict[str, Any]]:
    result: dict[tuple[str, str], dict[str, Any]] = {}
    root = store / "consumptions"
    if not root.exists():
        return result
    for path in sorted(root.glob("*.json")):
        if path.is_symlink() or _sha256_file(path) != path.stem:
            raise FormalOriginalSurfaceError("consumption_content_address_drift")
        row = _load_json(path, "consumption")
        if row.get("schema") != CONSUMPTION_SCHEMA:
            raise FormalOriginalSurfaceError("consumption_schema_invalid")
        key = (str(row.get("campaign_commitment_id") or ""), str(row.get("true_item_id") or ""))
        if key in result and result[key] != row:
            raise FormalOriginalSurfaceError("campaign_surface_consumed_more_than_once")
        result[key] = row
    return result


def _learner_identity_commitments(surface: dict[str, Any]) -> dict[str, Any]:
    asset_hashes = [
        str(row.get("sha256"))
        for row in surface.get("question_asset_refs") or []
        if isinstance(row, dict) and SHA_RE.fullmatch(str(row.get("sha256") or ""))
    ]
    safe = surface.get("answer_safe_content")
    text_identity_sha256 = None
    if isinstance(safe, dict):
        options = safe.get("options")
        if isinstance(options, list):
            by_label = {
                str(row.get("label")): _normalize(row.get("text"))
                for row in options
                if isinstance(row, dict)
            }
            if set(by_label) == set(LABELS):
                material = {
                    "kind": "stem_and_abcd",
                    "stem": _normalize(safe.get("stem")),
                    "options": {label: by_label[label] for label in LABELS},
                }
                text_identity_sha256 = _digest(material)
    return {
        "question_asset_sha256s": asset_hashes,
        "text_surface_identity_sha256": text_identity_sha256,
    }


def pool_status(details_root: str | Path) -> dict[str, Any]:
    """Reopen every manifest and report verified, consumed, or quarantined state."""

    details, store = _root(details_root)
    consumed = _consumptions(details, store)
    consumed_ids = {true_item_id for _, true_item_id in consumed}
    entries: list[dict[str, Any]] = []
    manifest_root = store / "published" / "manifests"
    if manifest_root.exists():
        for path in sorted(manifest_root.glob("*.json")):
            reason = None
            status = "verified"
            manifest: dict[str, Any] = {}
            try:
                if path.is_symlink() or _sha256_file(path) != path.stem:
                    raise FormalOriginalSurfaceError("manifest_content_address_drift")
                manifest = _load_json(path, "manifest")
                if manifest.get("schema") != MANIFEST_SCHEMA:
                    raise FormalOriginalSurfaceError("manifest_schema_invalid")
                true_item_id = str(manifest.get("true_item_id") or "")
                if not FOS_RE.fullmatch(true_item_id):
                    raise FormalOriginalSurfaceError("true_item_id_invalid")
                surface, _, surface_sha = _content_ref(details, manifest.get("surface_ref", ""), "surface")
                capsule, _, capsule_sha = _content_ref(details, manifest.get("answer_capsule_ref", ""), "answer_capsule")
                if (
                    surface.get("schema") != SURFACE_SCHEMA
                    or surface.get("answer_content_in_surface") is not False
                    or any(key in surface for key in ("correct_option", "answer", "solution_asset_refs"))
                    or surface_sha != manifest.get("surface_sha256")
                    or capsule.get("schema") != CAPSULE_SCHEMA
                    or capsule_sha != manifest.get("answer_capsule_sha256")
                    or capsule.get("surface_sha256") != surface_sha
                    or capsule.get("true_item_id") != true_item_id
                    or capsule.get("correct_option") not in LABELS
                    or manifest.get("answer_isolated") is not True
                ):
                    raise FormalOriginalSurfaceError("published_chain_mismatch")
                _asset_bindings_current(details, manifest)
            except (KeyError, TypeError, FormalOriginalSurfaceError) as exc:
                status, reason = "quarantine", str(exc)
            entries.append(
                {
                    "true_item_id": manifest.get("true_item_id", path.stem),
                    "formal_node_id": manifest.get("formal_node_id"),
                    "formal_node_sha256": manifest.get("formal_node_sha256"),
                    "manifest_ref": _relative(path, details),
                    "manifest_sha256": path.stem,
                    "surface_ref": manifest.get("surface_ref"),
                    "surface_sha256": manifest.get("surface_sha256"),
                    "answer_capsule_ref": manifest.get("answer_capsule_ref"),
                    "answer_capsule_sha256": manifest.get("answer_capsule_sha256"),
                    "exact_exposure_identity": manifest.get("exact_exposure_identity"),
                    **(
                        _learner_identity_commitments(surface)
                        if status == "verified"
                        else {
                            "question_asset_sha256s": [],
                            "text_surface_identity_sha256": None,
                        }
                    ),
                    "pool_status": status,
                    "status_reason": reason,
                    "ever_consumed": manifest.get("true_item_id") in consumed_ids,
                }
            )
    quarantine_root = store / "quarantine"
    if quarantine_root.exists():
        for path in sorted(quarantine_root.glob("*.json")):
            if path.is_symlink() or _sha256_file(path) != path.stem:
                raise FormalOriginalSurfaceError("quarantine_content_address_drift")
            row = _load_json(path, "quarantine")
            entries.append(
                {
                    "true_item_id": row.get("true_item_id"),
                    "formal_node_id": row.get("formal_node_id"),
                    "formal_node_sha256": row.get("formal_node_sha256"),
                    "quarantine_ref": _relative(path, details),
                    "quarantine_sha256": path.stem,
                    "pool_status": "quarantine",
                    "status_reason": row.get("reason"),
                    "ever_consumed": False,
                }
            )
    entries.sort(key=lambda row: (str(row.get("true_item_id")), str(row.get("manifest_sha256", ""))))
    counts = {"verified": 0, "quarantine": 0}
    for row in entries:
        counts[row["pool_status"]] += 1
    material = {
        "schema": POOL_SCHEMA,
        "counts": counts,
        "consumption_count": len(consumed),
        "entries": entries,
        "formal_write_count": 0,
    }
    return {**material, "content_sha256": _digest(material)}


def _recent_gap(exposure_set: dict[str, Any], true_item_id: str, day: dt.date) -> int | None:
    rows = exposure_set.get("rows")
    if not isinstance(rows, list):
        raise FormalOriginalSurfaceError("exposure_set_rows_invalid")
    gaps: list[int] = []
    for row in rows:
        if not isinstance(row, dict):
            raise FormalOriginalSurfaceError("exposure_set_row_invalid")
        if row.get("true_item_id") != true_item_id:
            continue
        raw = row.get("display_date", row.get("review_date"))
        gap = (day - _iso_date(raw, "exposure_date")).days
        if gap < 0:
            raise FormalOriginalSurfaceError("future_exposure_invalid")
        gaps.append(gap)
    return min(gaps) if gaps else None


def select_surface(
    details_root: str | Path,
    *,
    review_date: str,
    formal_node_id: str,
    formal_node_sha256: str,
    exposure_set: dict[str, Any],
    campaign_commitment_id: str,
    exact_exposure_identity: str | None = None,
    preferred_true_item_id: str | None = None,
    allow_existing_campaign_reservation: bool = False,
) -> dict[str, Any]:
    """Select an eligible formal original without exposing its answer capsule."""

    details, store = _root(details_root)
    day = _iso_date(review_date, "review_date")
    if (
        not FORMAL_NODE_RE.fullmatch(formal_node_id)
        or not SHA_RE.fullmatch(formal_node_sha256)
        or not SAFE_ID_RE.fullmatch(campaign_commitment_id)
        or (exact_exposure_identity is not None and not SAFE_ID_RE.fullmatch(exact_exposure_identity))
        or (preferred_true_item_id is not None and not re.fullmatch(r"FOS-[0-9A-F]{24}", preferred_true_item_id))
    ):
        raise FormalOriginalSurfaceError("selection_identity_invalid")
    pool = pool_status(details_root)
    consumed = _consumptions(details, store)
    blocked: list[dict[str, Any]] = []
    eligible: list[dict[str, Any]] = []
    for row in pool["entries"]:
        if (
            row.get("pool_status") != "verified"
            or row.get("formal_node_id") != formal_node_id
            or row.get("formal_node_sha256") != formal_node_sha256
            or (preferred_true_item_id is not None and row.get("true_item_id") != preferred_true_item_id)
            or (
                exact_exposure_identity is not None
                and row.get("exact_exposure_identity") != exact_exposure_identity
            )
        ):
            continue
        true_item_id = str(row["true_item_id"])
        gap = _recent_gap(exposure_set, true_item_id, day)
        if gap is not None and gap < 7:
            blocked.append(
                {
                    "true_item_id": true_item_id,
                    "reason": "exact_item_cooldown",
                    "gap_days": gap,
                    "eligible_on": (day + dt.timedelta(days=7 - gap)).isoformat(),
                }
            )
        elif (
            (campaign_commitment_id, true_item_id) in consumed
            and not allow_existing_campaign_reservation
        ):
            blocked.append({"true_item_id": true_item_id, "reason": "already_consumed_by_campaign"})
        else:
            eligible.append({**row, "cooldown_gap_days": gap})
    eligible.sort(key=lambda row: str(row["true_item_id"]))
    if not eligible:
        return {
            "schema": SELECTION_SCHEMA,
            "status": "unavailable",
            "formal_node_id": formal_node_id,
            "formal_node_sha256": formal_node_sha256,
            "review_date": day.isoformat(),
            "campaign_commitment_id": campaign_commitment_id,
            "blocked": blocked,
            "fallback_to_unverified_surface": False,
            "formal_write_count": 0,
        }
    chosen = eligible[0]
    manifest, _, manifest_sha = _content_ref(details, chosen["manifest_ref"], "manifest")
    surface, _, surface_sha = _content_ref(details, chosen["surface_ref"], "surface")
    if (
        manifest_sha != chosen["manifest_sha256"]
        or surface_sha != chosen["surface_sha256"]
        or surface.get("answer_content_in_surface") is not False
    ):
        raise FormalOriginalSurfaceError("selection_binding_drift")
    material = {
        "schema": SELECTION_SCHEMA,
        "status": "selected",
        "selection_kind": "formal_original",
        "review_date": day.isoformat(),
        "campaign_commitment_id": campaign_commitment_id,
        "formal_node_id": formal_node_id,
        "formal_node_sha256": formal_node_sha256,
        "true_item_id": chosen["true_item_id"],
        "exact_exposure_identity": chosen["exact_exposure_identity"],
        "cooldown_gap_days": chosen["cooldown_gap_days"],
        "manifest_ref": chosen["manifest_ref"],
        "manifest_sha256": chosen["manifest_sha256"],
        "surface_ref": chosen["surface_ref"],
        "surface_sha256": surface_sha,
        "answer_capsule_commitment_sha256": chosen["answer_capsule_sha256"],
        "learner_surface": surface,
        "answer_content_in_selection": False,
        "formal_node_created": False,
        "formal_write_count": 0,
    }
    selection_id = "FOSSEL-" + _digest(material)[:24].upper()
    selection = {**material, "selection_id": selection_id}
    path, digest = _content_write(store / "selections", selection)
    return {
        **selection,
        "selection_ref": _relative(path, details),
        "selection_sha256": digest,
    }


def commit_consumption(
    details_root: str | Path, selection_ref: str | Path
) -> dict[str, Any]:
    """Commit one selected surface at most once for its bound campaign."""

    details, store = _root(details_root)
    selection, selection_path, selection_sha = _content_ref(
        details, selection_ref, "selection"
    )
    if (
        selection.get("schema") != SELECTION_SCHEMA
        or selection.get("status") != "selected"
        or selection.get("selection_kind") != "formal_original"
        or selection.get("answer_content_in_selection") is not False
        or not FOS_RE.fullmatch(str(selection.get("true_item_id") or ""))
        or not SAFE_ID_RE.fullmatch(str(selection.get("campaign_commitment_id") or ""))
    ):
        raise FormalOriginalSurfaceError("selection_invalid")
    pool = pool_status(details_root)
    matching = [
        row
        for row in pool["entries"]
        if row.get("pool_status") == "verified"
        and row.get("true_item_id") == selection["true_item_id"]
        and row.get("manifest_sha256") == selection.get("manifest_sha256")
        and row.get("surface_sha256") == selection.get("surface_sha256")
        and row.get("answer_capsule_sha256")
        == selection.get("answer_capsule_commitment_sha256")
    ]
    if len(matching) != 1:
        raise FormalOriginalSurfaceError("selected_surface_not_verified_at_commit")
    key = (selection["campaign_commitment_id"], selection["true_item_id"])
    previous = _consumptions(details, store).get(key)
    receipt = {
        "schema": CONSUMPTION_SCHEMA,
        "campaign_commitment_id": selection["campaign_commitment_id"],
        "true_item_id": selection["true_item_id"],
        "selection_ref": _relative(selection_path, details),
        "selection_sha256": selection_sha,
        "manifest_sha256": selection["manifest_sha256"],
        "surface_sha256": selection["surface_sha256"],
        "answer_capsule_commitment_sha256": selection["answer_capsule_commitment_sha256"],
        "review_date": selection["review_date"],
        "answer_isolated": True,
        "formal_node_created": False,
        "formal_write_count": 0,
    }
    if previous is not None:
        if previous != receipt:
            raise FormalOriginalSurfaceError("campaign_surface_consumption_conflict")
        previous_sha = _sha256_bytes(_pretty_bytes(previous))
        return {
            "status": "already_committed",
            "consumption_ref": _relative(store / "consumptions" / f"{previous_sha}.json", details),
            "consumption_sha256": previous_sha,
            "true_item_id": selection["true_item_id"],
            "campaign_commitment_id": selection["campaign_commitment_id"],
            "formal_write_count": 0,
        }
    path, digest = _content_write(store / "consumptions", receipt)
    return {
        "status": "committed",
        "consumption_ref": _relative(path, details),
        "consumption_sha256": digest,
        "true_item_id": selection["true_item_id"],
        "campaign_commitment_id": selection["campaign_commitment_id"],
        "formal_write_count": 0,
    }


def _json_arg(path: str, label: str) -> dict[str, Any]:
    return _load_json(Path(path).expanduser().resolve(strict=True), label)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    prepare = sub.add_parser("prepare-submission")
    prepare.add_argument("--details-root", required=True)
    prepare.add_argument("--submission", required=True)
    publish_parser = sub.add_parser("publish")
    publish_parser.add_argument("--details-root", required=True)
    publish_parser.add_argument("--submission", required=True)
    publish_parser.add_argument("--blind-verification", required=True)
    publish_parser.add_argument("--source-verification", required=True)
    pool = sub.add_parser("pool-status")
    pool.add_argument("--details-root", required=True)
    select = sub.add_parser("select-surface")
    select.add_argument("--details-root", required=True)
    select.add_argument("--review-date", required=True)
    select.add_argument("--formal-node-id", required=True)
    select.add_argument("--formal-node-sha256", required=True)
    select.add_argument("--exposure-set", required=True)
    select.add_argument("--campaign-commitment-id", required=True)
    select.add_argument("--exact-exposure-identity")
    select.add_argument("--allow-existing-campaign-reservation", action="store_true")
    commit = sub.add_parser("commit-consumption")
    commit.add_argument("--details-root", required=True)
    commit.add_argument("--selection-ref", required=True)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        if args.command == "prepare-submission":
            result = prepare_submission(
                args.details_root,
                _json_arg(args.submission, "submission"),
            )
        elif args.command == "publish":
            result = publish(
                args.details_root,
                _json_arg(args.submission, "submission"),
                _json_arg(args.blind_verification, "blind_verification"),
                _json_arg(args.source_verification, "source_verification"),
            )
        elif args.command == "pool-status":
            result = pool_status(args.details_root)
        elif args.command == "select-surface":
            result = select_surface(
                args.details_root,
                review_date=args.review_date,
                formal_node_id=args.formal_node_id,
                formal_node_sha256=args.formal_node_sha256,
                exposure_set=_json_arg(args.exposure_set, "exposure_set"),
                campaign_commitment_id=args.campaign_commitment_id,
                exact_exposure_identity=args.exact_exposure_identity,
                allow_existing_campaign_reservation=args.allow_existing_campaign_reservation,
            )
        else:
            result = commit_consumption(args.details_root, args.selection_ref)
    except (OSError, FormalOriginalSurfaceError) as exc:
        print(json.dumps({"status": "ERROR", "error": str(exc)}, ensure_ascii=False))
        return 2
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())


__all__ = [
    "BLIND_SCHEMA",
    "FormalOriginalSurfaceError",
    "SOURCE_SCHEMA",
    "SUBMISSION_SCHEMA",
    "commit_consumption",
    "pool_status",
    "prepare_submission",
    "publish",
    "select_surface",
]
