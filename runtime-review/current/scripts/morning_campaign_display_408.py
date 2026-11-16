#!/usr/bin/env python3
"""Answer-safe display bridge for the successor 408 morning campaign.

The bridge publishes exactly one private, content-addressed turn receipt for
the active campaign frontier.  The shared exposure reader replays that same
immutable receipt, while the preparation cutoff remains frozen by excluding
receipts produced by the currently active run from the cutoff comparison.
"""

from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import os
import re
import stat
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import current_question_evidence_408 as private_evidence
import exam_variant_bank_408 as variant_bank
import formal_original_surface_408 as formal_original
import morning_campaign_runtime_408 as campaign_runtime
import question_exposure_408 as legacy_exposure
import start_morning_coverage_campaign_408 as campaign_control


DISPLAY_RECEIPT_SCHEMA = "morning-campaign-display-receipt-v1"
DISPLAY_RESULT_SCHEMA = "morning-campaign-display-v1"
QUESTION_PROVENANCE_SCHEMA = "morning-campaign-question-provenance-v1"
TURN_SCHEMA = private_evidence.TURN_RECEIPT_SCHEMA
DISPLAY_STATUS = "campaign_display_published"
OPTIONS = ("A", "B", "C", "D")
WINDOW_DAYS = 7
MAX_JSON_BYTES = 8 * 1024 * 1024
MAX_TURN_BYTES = private_evidence.MAX_METADATA_BYTES

SHA_RE = re.compile(r"^[0-9a-f]{64}$")
CANONICAL_TRUE_ITEM_RE = re.compile(r"^EXAM408-(\d{4})-Q(\d{2})(?:-P(\d{2}))?$")


class CampaignDisplayError(RuntimeError):
    """Stable fail-closed error for campaign display publication."""


def _canonical_bytes(value: Any) -> bytes:
    return (
        json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        + "\n"
    ).encode("utf-8")


def _digest(value: Any) -> str:
    return hashlib.sha256(_canonical_bytes(value)).hexdigest()


def _file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _read_json(path: Path, label: str, maximum: int = MAX_JSON_BYTES) -> dict[str, Any]:
    try:
        info = path.lstat()
    except OSError as exc:
        raise CampaignDisplayError(f"{label}_missing") from exc
    if stat.S_ISLNK(info.st_mode) or not stat.S_ISREG(info.st_mode):
        raise CampaignDisplayError(f"{label}_unsafe")
    if not 0 < info.st_size <= maximum:
        raise CampaignDisplayError(f"{label}_size_invalid")
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise CampaignDisplayError(f"{label}_invalid_json") from exc
    if not isinstance(value, dict):
        raise CampaignDisplayError(f"{label}_root_invalid")
    return value


def _safe_time(value: str | None) -> str:
    if value is None:
        parsed = dt.datetime.now(dt.timezone.utc).replace(microsecond=0)
    else:
        try:
            parsed = dt.datetime.fromisoformat(value)
        except ValueError as exc:
            raise CampaignDisplayError("displayed_at_invalid") from exc
        if parsed.tzinfo is None:
            raise CampaignDisplayError("displayed_at_requires_timezone")
        parsed = parsed.replace(microsecond=0)
    return parsed.isoformat()


def _private_root(repo: Path, private_root: str | Path | None) -> Path:
    if private_root is None:
        return legacy_exposure.private_root_for_repo(repo)
    root = Path(private_root).expanduser().resolve()
    if root == Path(root.anchor):
        raise CampaignDisplayError("private_root_too_broad")
    if root.exists() and root.is_symlink():
        raise CampaignDisplayError("private_root_unsafe")
    return root


def _surface_content_sha(stem: str, options: dict[str, str]) -> str:
    return _digest({"stem": stem, "options": options})


def _normalize_public_surface(value: object) -> tuple[str, dict[str, str]]:
    if not isinstance(value, dict):
        raise CampaignDisplayError("selected_surface_object_invalid")
    stem = str(value.get("stem") or "").strip()
    raw_options = value.get("options")
    if not stem or not isinstance(raw_options, dict) or set(raw_options) != set(OPTIONS):
        raise CampaignDisplayError("selected_surface_text_incomplete")
    options = {label: str(raw_options[label] or "").strip() for label in OPTIONS}
    if any(not text for text in options.values()):
        raise CampaignDisplayError("selected_surface_options_incomplete")
    return stem, options


def _resolve_managed_ref(root: Path, value: object, label: str) -> Path:
    ref = Path(str(value or ""))
    if ref.is_absolute() or not ref.parts or ".." in ref.parts:
        raise CampaignDisplayError(f"{label}_ref_unsafe")
    try:
        path = (root / ref).resolve(strict=True)
        path.relative_to(root.resolve(strict=True))
    except (OSError, ValueError) as exc:
        raise CampaignDisplayError(f"{label}_ref_escape") from exc
    if path.is_symlink() or not path.is_file():
        raise CampaignDisplayError(f"{label}_unsafe")
    return path


def _require_private_grading_file(path: Path, label: str) -> None:
    if stat.S_IMODE(path.stat().st_mode) & 0o077:
        raise CampaignDisplayError(f"{label}_permissions_not_private")


def _terminal_evidence_asset(path: Path, *, role: str, label: str) -> dict[str, Any]:
    if path.is_symlink() or not path.is_file() or path.stat().st_size <= 0:
        raise CampaignDisplayError("terminal_evidence_asset_unsafe")
    return {
        "path": str(path),
        "sha256": _file_sha256(path),
        "size_bytes": path.stat().st_size,
        "role": role,
        "label": label,
    }


def _find_unique(rows: object, predicate: Any, label: str) -> dict[str, Any]:
    matches = [row for row in rows or [] if isinstance(row, dict) and predicate(row)]
    if len(matches) != 1:
        raise CampaignDisplayError(f"{label}_not_unique")
    return matches[0]


def _canonical_exam_parts(value: object, label: str) -> tuple[str, int, int]:
    canonical_id = str(value or "")
    matched = CANONICAL_TRUE_ITEM_RE.fullmatch(canonical_id)
    if matched is None:
        raise CampaignDisplayError(f"{label}_invalid")
    year = int(matched.group(1))
    question_number = int(matched.group(2))
    if not 2009 <= year <= 2100 or not 1 <= question_number <= 47:
        raise CampaignDisplayError(f"{label}_out_of_range")
    return canonical_id, year, question_number


def _question_provenance(
    item: dict[str, Any], selection: dict[str, Any]
) -> dict[str, Any]:
    surface_kind = str(item.get("surface_kind") or "")
    true_item_id = str(item.get("true_item_id") or "")
    if (
        selection.get("surface_kind") != surface_kind
        or selection.get("true_item_id") != true_item_id
    ):
        raise CampaignDisplayError("question_provenance_selection_drift")

    if surface_kind == "canonical_original":
        canonical_id, year, question_number = _canonical_exam_parts(
            item.get("source_id"), "canonical_question_source_id"
        )
        if (
            true_item_id != canonical_id
            or selection.get("canonical_id") != canonical_id
        ):
            raise CampaignDisplayError("canonical_question_provenance_drift")
        return {
            "schema": QUESTION_PROVENANCE_SCHEMA,
            "kind": "true_exam",
            "label": f"真题｜{year} 年考研 408 第 {question_number} 题" + (f" · {selection['source_part_label']}" if selection.get("source_part_label") else ""),
            "surface_kind": surface_kind,
            "true_item_id": true_item_id,
            "true_exam_id": canonical_id,
            "based_on_true_exam_id": None,
            "exam_year": year,
            "exam_question_number": question_number,
            "basis": "verified_canonical_true_item_id",
            "acceptance_kind": "canonical_original_verified_ready",
            "independent_reviewed": False,
            "answer_safe": True,
        }

    if surface_kind == "verified_variant":
        base_id, year, question_number = _canonical_exam_parts(
            item.get("source_id"), "variant_base_source_id"
        )
        if (
            item.get("variant_id") != true_item_id
            or selection.get("variant_id") != true_item_id
            or selection.get("blocked_original_true_item_id") != base_id
        ):
            raise CampaignDisplayError("variant_question_provenance_drift")
        acceptance_kind = str(selection.get("acceptance_kind") or "verified_ready")
        if acceptance_kind not in {
            "verified_ready",
            "web_self_checked_ready",
        }:
            raise CampaignDisplayError("variant_acceptance_kind_invalid")
        web_self_checked = acceptance_kind == "web_self_checked_ready"
        return {
            "schema": QUESTION_PROVENANCE_SCHEMA,
            "kind": "web_self_checked_variant" if web_self_checked else "generated_variant",
            "label": (
                f"网页自检变式｜基于 {year} 年考研 408 真题第 {question_number} 题"
                if web_self_checked
                else f"变式题｜基于 {year} 年考研 408 真题第 {question_number} 题"
            ),
            "surface_kind": surface_kind,
            "true_item_id": true_item_id,
            "true_exam_id": None,
            "based_on_true_exam_id": base_id,
            "exam_year": year,
            "exam_question_number": question_number,
            "basis": (
                "web_same_actor_solution_and_counterexample_self_check"
                if web_self_checked
                else "verified_variant_base_true_item_id"
            ),
            "acceptance_kind": acceptance_kind,
            "independent_reviewed": not web_self_checked,
            "answer_safe": True,
        }

    if surface_kind == "formal_original":
        if (
            item.get("variant_id") is not None
            or item.get("source_id") != item.get("formal_node_id")
            or selection.get("selection_kind") != "formal_original"
            or selection.get("formal_node_id") != item.get("formal_node_id")
        ):
            raise CampaignDisplayError("formal_original_provenance_drift")
        return {
            "schema": QUESTION_PROVENANCE_SCHEMA,
            "kind": "local_original",
            "label": "来源原题｜非系统生成变式｜真题年份未确认",
            "surface_kind": surface_kind,
            "true_item_id": true_item_id,
            "true_exam_id": None,
            "based_on_true_exam_id": None,
            "exam_year": None,
            "exam_question_number": None,
            "basis": "verified_formal_original_without_true_exam_identity",
            "acceptance_kind": "formal_original_verified_ready",
            "independent_reviewed": False,
            "answer_safe": True,
        }

    raise CampaignDisplayError("question_provenance_surface_kind_invalid")


def _variant_surface(
    *,
    details_root: Path,
    item: dict[str, Any],
    selection: dict[str, Any],
    pool: dict[str, Any],
) -> dict[str, Any]:
    entry = _find_unique(
        pool.get("entries"),
        lambda row: row.get("variant_id") == item["variant_id"],
        "selected_variant_pool_entry",
    )
    surface_path = _resolve_managed_ref(details_root, entry.get("surface_ref"), "variant_surface")
    if _file_sha256(surface_path) != item["surface_sha256"] or entry.get("surface_sha256") != item["surface_sha256"]:
        raise CampaignDisplayError("variant_surface_hash_drift")
    surface = _read_json(surface_path, "variant_surface")
    if surface.get("schema") != variant_bank.SURFACE_SCHEMA or surface.get("answer_content_in_surface") is not False:
        raise CampaignDisplayError("variant_surface_answer_isolation_failed")
    acceptance_kind = str(entry.get("acceptance_kind") or "verified_ready")
    if (
        selection.get("acceptance_kind", "verified_ready") != acceptance_kind
        or surface.get("acceptance_kind", "verified_ready") != acceptance_kind
    ):
        raise CampaignDisplayError("variant_surface_acceptance_kind_drift")
    stem, options = _normalize_public_surface(selection.get("learner_surface"))
    stable_rows = surface.get("options")
    if not isinstance(stable_rows, list) or len(stable_rows) != 4:
        raise CampaignDisplayError("variant_stable_options_invalid")
    by_text: dict[str, list[str]] = {}
    for row in stable_rows:
        if not isinstance(row, dict):
            raise CampaignDisplayError("variant_stable_options_invalid")
        option_id = str(row.get("option_id") or "")
        text = str(row.get("text") or "").strip()
        if not option_id or not text:
            raise CampaignDisplayError("variant_stable_options_invalid")
        by_text.setdefault(text, []).append(option_id)
    mapping: dict[str, str] = {}
    for label in OPTIONS:
        ids = by_text.get(options[label], [])
        if len(ids) != 1:
            raise CampaignDisplayError("variant_display_permutation_ambiguous")
        mapping[label] = ids[0]
    if len(set(mapping.values())) != 4 or selection.get("permutation_sha256") != variant_bank._digest(list(mapping.values())):  # type: ignore[attr-defined]
        raise CampaignDisplayError("variant_display_permutation_drift")
    answer_path = _resolve_managed_ref(details_root, entry.get("answer_capsule_ref"), "variant_grading_capsule")
    _require_private_grading_file(answer_path, "variant_grading_capsule")
    answer_sha = _file_sha256(answer_path)
    if answer_sha != item["grading_sha256"] or entry.get("answer_capsule_sha256") != answer_sha:
        raise CampaignDisplayError("variant_grading_capsule_hash_drift")
    terminal_assets = [
        _terminal_evidence_asset(
            answer_path, role="other_attachment", label="verified grading and explanation capsule"
        )
    ]
    if acceptance_kind == "web_self_checked_ready":
        manifest_path = _resolve_managed_ref(
            details_root, entry.get("manifest_ref"), "variant_manifest"
        )
        if _file_sha256(manifest_path) != entry.get("manifest_sha256"):
            raise CampaignDisplayError("variant_manifest_hash_drift")
        manifest = _read_json(manifest_path, "variant_manifest")
        for ref_field, sha_field, label in (
            ("web_solution_ref", "web_solution_sha256", "web solution and explanation"),
            ("web_self_check_ref", "web_self_check_sha256", "web counterexample self-check"),
        ):
            evidence_path = _resolve_managed_ref(
                details_root, manifest.get(ref_field), ref_field
            )
            if _file_sha256(evidence_path) != manifest.get(sha_field):
                raise CampaignDisplayError(f"{ref_field}_hash_drift")
            terminal_assets.append(
                _terminal_evidence_asset(
                    evidence_path, role="other_attachment", label=label
                )
            )
    return {
        "stem": stem,
        "options": options,
        "option_ids_by_letter": mapping,
        "surface_object_path": str(surface_path),
        "surface_object_sha256": item["surface_sha256"],
        "grading_capsule_ref": str(entry["answer_capsule_ref"]),
        "grading_capsule_path": str(answer_path),
        "grading_capsule_sha256": answer_sha,
        "question_asset_ref": None,
        "question_asset_sha256": None,
        "question_assets": [],
        "terminal_evidence_assets": terminal_assets,
    }


def _canonical_surface(
    *,
    details_root: Path,
    release_dir: Path,
    release_id: str,
    item: dict[str, Any],
    selection: dict[str, Any],
) -> dict[str, Any]:
    if selection.get("practice_unit_id"):
        from exam_practice_units_408 import learner_surface
        return learner_surface(details_root, item, selection)
    raw_assets = selection.get("question_asset_refs")
    if not isinstance(raw_assets, list) or not raw_assets:
        raise CampaignDisplayError("canonical_question_asset_missing")
    assets: list[dict[str, Any]] = []
    resolved: list[Path] = []
    for row in raw_assets:
        if not isinstance(row, dict):
            raise CampaignDisplayError("canonical_question_asset_invalid")
        path = _resolve_managed_ref(details_root, row.get("ref"), "canonical_question_asset")
        sha = str(row.get("sha256") or "")
        if not SHA_RE.fullmatch(sha) or _file_sha256(path) != sha or path.stat().st_size != row.get("size_bytes"):
            raise CampaignDisplayError("canonical_question_asset_hash_drift")
        assets.append(
            {
                "ref": str(row["ref"]),
                "sha256": sha,
                "size_bytes": row["size_bytes"],
                "page_locator": row.get("page_locator"),
            }
        )
        resolved.append(path)
    material = {
        "schema": "canonical_original_surface_commitment_v1",
        "canonical_id": item["source_id"],
        "question_asset_refs": assets,
        "exam_style_release_content_sha256": selection["exam_style_release_content_sha256"],
    }
    if variant_bank._digest(material) != item["surface_sha256"]:  # type: ignore[attr-defined]
        raise CampaignDisplayError("canonical_surface_commitment_drift")
    capsule_ref = str(selection.get("answer_capsule_commitment_ref") or "")
    prefix = "private-sibling://answer-capsules/"
    if not capsule_ref.startswith(prefix):
        raise CampaignDisplayError("canonical_grading_capsule_ref_invalid")
    leaf = Path(capsule_ref[len(prefix) :])
    if leaf.is_absolute() or ".." in leaf.parts:
        raise CampaignDisplayError("canonical_grading_capsule_ref_invalid")
    capsule_path = (
        release_dir.parent.parent
        / "style-releases-private"
        / release_id
        / "answer-capsules"
        / leaf
    ).resolve(strict=True)
    if capsule_path.is_symlink() or not capsule_path.is_file() or _file_sha256(capsule_path) != item["grading_sha256"]:
        raise CampaignDisplayError("canonical_grading_capsule_hash_drift")
    _require_private_grading_file(capsule_path, "canonical_grading_capsule")
    image_lines = [f"![题目 {index}]({path})" for index, path in enumerate(resolved, 1)]
    return {
        "stem": "\n".join(image_lines),
        "options": {label: f"见题图中的 {label} 选项" for label in OPTIONS},
        "option_ids_by_letter": {label: label for label in OPTIONS},
        "surface_object_path": str(resolved[0]),
        "surface_object_sha256": item["surface_sha256"],
        "grading_capsule_ref": capsule_ref,
        "grading_capsule_path": str(capsule_path),
        "grading_capsule_sha256": item["grading_sha256"],
        "question_asset_ref": str(raw_assets[0]["ref"]),
        "question_asset_sha256": str(raw_assets[0]["sha256"]),
        "question_assets": [
            {
                **asset,
                "path": str(path),
            }
            for asset, path in zip(assets, resolved, strict=True)
        ],
        "terminal_evidence_assets": [
            _terminal_evidence_asset(
                capsule_path,
                role="other_attachment",
                label="verified grading and explanation capsule",
            )
        ],
    }


def _formal_original_surface(
    *,
    details_root: Path,
    item: dict[str, Any],
    selection: dict[str, Any],
    pool: dict[str, Any],
) -> dict[str, Any]:
    entry = _find_unique(
        pool.get("entries"),
        lambda row: row.get("pool_status") == "verified"
        and row.get("true_item_id") == item["true_item_id"]
        and row.get("formal_node_id") == item["formal_node_id"],
        "selected_formal_original_pool_entry",
    )
    surface_path = _resolve_managed_ref(
        details_root, entry.get("surface_ref"), "formal_original_surface"
    )
    if (
        _file_sha256(surface_path) != item["surface_sha256"]
        or entry.get("surface_sha256") != item["surface_sha256"]
    ):
        raise CampaignDisplayError("formal_original_surface_hash_drift")
    surface = _read_json(surface_path, "formal_original_surface")
    if (
        surface.get("schema") != formal_original.SURFACE_SCHEMA
        or surface.get("true_item_id") != item["true_item_id"]
        or surface.get("formal_node_id") != item["formal_node_id"]
        or surface.get("answer_content_in_surface") is not False
    ):
        raise CampaignDisplayError("formal_original_surface_identity_drift")
    safe_content = surface.get("answer_safe_content")
    if safe_content is not None:
        if not isinstance(safe_content, dict):
            raise CampaignDisplayError("formal_original_text_surface_invalid")
        rows = safe_content.get("options")
        if not isinstance(rows, list):
            raise CampaignDisplayError("formal_original_text_surface_invalid")
        options = {
            str(row.get("label")): str(row.get("text") or "").strip()
            for row in rows
            if isinstance(row, dict)
        }
        stem, options = _normalize_public_surface(
            {"stem": safe_content.get("stem"), "options": options}
        )
        question_asset_ref = None
        question_asset_sha = None
        question_assets: list[dict[str, Any]] = []
        surface_object_path = str(surface_path)
    else:
        raw_assets = surface.get("question_asset_refs")
        if not isinstance(raw_assets, list) or not raw_assets:
            raise CampaignDisplayError("formal_original_question_asset_missing")
        resolved: list[Path] = []
        question_assets = []
        for row in raw_assets:
            if not isinstance(row, dict):
                raise CampaignDisplayError("formal_original_question_asset_invalid")
            path = _resolve_managed_ref(
                details_root, row.get("ref"), "formal_original_question_asset"
            )
            if (
                _file_sha256(path) != row.get("sha256")
                or path.stat().st_size != row.get("size_bytes")
            ):
                raise CampaignDisplayError("formal_original_question_asset_hash_drift")
            resolved.append(path)
            question_assets.append(
                {
                    "ref": str(row["ref"]),
                    "path": str(path),
                    "sha256": str(row["sha256"]),
                    "size_bytes": row["size_bytes"],
                    "page_locator": row.get("page_locator"),
                }
            )
        stem = "\n".join(
            f"![题目 {index}]({path})" for index, path in enumerate(resolved, 1)
        )
        options = {label: f"见题图中的 {label} 选项" for label in OPTIONS}
        question_asset_ref = str(raw_assets[0]["ref"])
        question_asset_sha = str(raw_assets[0]["sha256"])
        surface_object_path = str(resolved[0])
    answer_path = _resolve_managed_ref(
        details_root, entry.get("answer_capsule_ref"), "formal_original_grading_capsule"
    )
    _require_private_grading_file(answer_path, "formal_original_grading_capsule")
    answer_sha = _file_sha256(answer_path)
    if answer_sha != item["grading_sha256"] or entry.get("answer_capsule_sha256") != answer_sha:
        raise CampaignDisplayError("formal_original_grading_capsule_hash_drift")
    return {
        "stem": stem,
        "options": options,
        "option_ids_by_letter": {label: label for label in OPTIONS},
        "surface_object_path": surface_object_path,
        "surface_object_sha256": item["surface_sha256"],
        "grading_capsule_ref": str(entry["answer_capsule_ref"]),
        "grading_capsule_path": str(answer_path),
        "grading_capsule_sha256": answer_sha,
        "question_asset_ref": question_asset_ref,
        "question_asset_sha256": question_asset_sha,
        "question_assets": question_assets,
        "terminal_evidence_assets": [
            _terminal_evidence_asset(
                answer_path,
                role="other_attachment",
                label="verified grading and explanation capsule",
            )
        ],
    }


def _campaign_turns(private_root: Path) -> list[tuple[str, dict[str, Any]]]:
    turns = private_root / "turns"
    if not turns.exists():
        return []
    if turns.is_symlink() or not turns.is_dir():
        raise CampaignDisplayError("private_turn_store_unsafe")
    rows: list[tuple[str, dict[str, Any]]] = []
    for path in sorted(turns.glob("*.json"), key=lambda row: row.name):
        if not SHA_RE.fullmatch(path.stem):
            raise CampaignDisplayError("private_turn_filename_invalid")
        value = _read_json(path, "private_turn", MAX_TURN_BYTES)
        if _file_sha256(path) != path.stem:
            raise CampaignDisplayError("private_turn_content_address_drift")
        if value.get("schema") == TURN_SCHEMA and value.get("status") == DISPLAY_STATUS:
            rows.append((path.stem, value))
    return rows


def _validate_existing_receipt(receipt: dict[str, Any], snapshot: dict[str, Any]) -> None:
    expected = {
        "receipt_schema": DISPLAY_RECEIPT_SCHEMA,
        "run_id": snapshot["run_id"],
        "segment_id": snapshot["segment"]["segment_id"],
        "segment_ordinal": snapshot["segment_ordinal"],
        "item_offset": snapshot["item_offset"],
        "item_id": snapshot["item"]["item_id"],
        "surface_sha256": snapshot["item"]["surface_sha256"],
        "runtime_manifest_sha256": snapshot["runtime_manifest_sha256"],
    }
    if any(receipt.get(key) != value for key, value in expected.items()):
        raise CampaignDisplayError("existing_display_receipt_binding_drift")
    if receipt.get("answer_safe") is not True or receipt.get("formal_write_count") != 0:
        raise CampaignDisplayError("existing_display_receipt_unsafe")


def _public_result(
    receipt: dict[str, Any],
    receipt_sha: str,
    *,
    repeated: bool,
    item: dict[str, Any],
    selection: dict[str, Any],
    private_root: Path,
) -> dict[str, Any]:
    public = receipt.get("learner_surface")
    if not isinstance(public, dict) or set(public) != {"stem", "options"}:
        raise CampaignDisplayError("private_receipt_public_surface_invalid")
    provenance = _question_provenance(item, selection)
    stored_provenance = receipt.get("question_provenance")
    if stored_provenance is not None and stored_provenance != provenance:
        raise CampaignDisplayError("private_receipt_question_provenance_drift")
    return {
        "schema": DISPLAY_RESULT_SCHEMA,
        "status": "already_published" if repeated else "display_published",
        "run_id": receipt["run_id"],
        "segment_id": receipt["segment_id"],
        "item_id": receipt["item_id"],
        "question_provenance": provenance,
        **({"review_scope": receipt["review_scope"]} if receipt.get("review_scope") else {}),
        "learner_surface": public,
        "display_receipt_locator": private_evidence.TURN_LOCATOR_PREFIX + receipt_sha,
        "display_receipt_sha256": receipt_sha,
        "display_receipt_path": str(private_root / "turns" / f"{receipt_sha}.json"),
        "exposure_recorded": True,
        "learner_evidence_write_count": 0 if repeated else 1,
        "formal_write_count": 0,
        **(
            {
                "saving_mode": receipt["saving_mode"],
                "episode_capture_every_item": receipt[
                    "episode_capture_every_item"
                ],
                **(
                    {
                        "saving_contract_sha256": receipt[
                            "saving_contract_sha256"
                        ]
                    }
                    if receipt.get("saving_contract_sha256")
                    else {}
                ),
            }
            if "episode_capture_every_item" in receipt
            else {}
        ),
    }


def _frontier_snapshot(
    repo: Path,
    *,
    review_date: str,
    run_id: str,
    control_root: Path,
    runtime_root: Path,
    private_root: Path,
    exposure_set: dict[str, Any] | None,
    release_snapshot: dict[str, Any] | None,
    pool_snapshot: dict[str, Any] | None,
    formal_pool_snapshot: dict[str, Any] | None,
    verified_frontier: tuple[
        Path,
        dict[str, Any],
        dict[str, Any],
        dict[str, Any] | None,
    ]
    | None = None,
) -> dict[str, Any]:
    state, prep_dir = campaign_control._current(control_root, review_date)  # type: ignore[attr-defined]
    if (
        state.get("status") != "ready"
        or state.get("all_segments_ready") is not True
        or state.get("run_id") != run_id
        or state.get("review_date") != review_date
    ):
        raise CampaignDisplayError("campaign_preparation_not_ready")
    try:
        run_dir, run, active, verified_item = (
            verified_frontier
            if verified_frontier is not None
            else campaign_runtime.reopen_verified_frontier(runtime_root, run_id)
        )
    except campaign_runtime.CampaignRuntimeError as exc:
        raise CampaignDisplayError(f"campaign_runtime_unverified:{exc}") from exc
    manifest_path = run_dir / "manifest.json"
    manifest = campaign_runtime.normalize_manifest(_read_json(manifest_path, "runtime_manifest"))
    runtime_manifest_sha = _file_sha256(manifest_path)
    if run.get("manifest_sha256") != runtime_manifest_sha:
        raise CampaignDisplayError("runtime_manifest_hash_drift")
    if active.get("status") != "active":
        raise CampaignDisplayError("campaign_runtime_not_active")
    segment_ordinal = active.get("current_segment_ordinal")
    item_offset = active.get("current_item_offset")
    if not isinstance(segment_ordinal, int) or not isinstance(item_offset, int):
        raise CampaignDisplayError("campaign_frontier_invalid")
    try:
        segment = manifest["segments"][segment_ordinal]
        item = segment["items"][item_offset]
    except (IndexError, KeyError, TypeError) as exc:
        raise CampaignDisplayError("campaign_frontier_out_of_range") from exc
    if verified_item != item:
        raise CampaignDisplayError("campaign_verified_frontier_item_drift")
    active_segment = active.get("active_segment")
    active_items = active_segment.get("items") if isinstance(active_segment, dict) else None
    if (
        not isinstance(active_segment, dict)
        or not isinstance(active_items, list)
        or not 0 <= item_offset < len(active_items)
        or active_segment.get("segment_id") != segment["segment_id"]
        or active_segment.get("commitment_sha256") != segment["commitment_sha256"]
        or active_items[item_offset] != item
    ):
        raise CampaignDisplayError("campaign_frontier_manifest_drift")
    preparation_manifest = campaign_runtime.normalize_manifest(
        _read_json(prep_dir / "campaign-manifest.json", "preparation_manifest")
    )
    if preparation_manifest != manifest or _digest(preparation_manifest) != state.get("campaign_manifest_sha256"):
        raise CampaignDisplayError("preparation_runtime_manifest_drift")
    selection_manifest = _read_json(prep_dir / "selection-manifest.json", "selection_manifest")
    if _digest(selection_manifest) != state.get("selection_manifest_sha256"):
        raise CampaignDisplayError("selection_manifest_hash_drift")
    commitment = _find_unique(
        selection_manifest.get("items"),
        lambda row: row.get("item_id") == item["item_id"],
        "campaign_item_commitment",
    )
    for field in (
        "formal_node_id",
        "true_item_id",
        "variant_id",
        "surface_kind",
        "surface_sha256",
        "grading_sha256",
        "pack_commitment_sha256",
    ):
        expected = item["campaign_item_commitment_sha256"] if field == "pack_commitment_sha256" else item[field]
        if commitment.get(field) != expected:
            raise CampaignDisplayError(f"campaign_item_{field}_drift")
    variant_selection_path = Path(str(state.get("variant_selection_manifest_ref") or "")).expanduser().resolve(strict=True)
    variant_selection = _read_json(variant_selection_path, "variant_selection_manifest")
    if (
        _file_sha256(variant_selection_path) != state.get("variant_selection_manifest_sha256")
        or variant_selection.get("campaign_commitment_sha256") != state.get("campaign_manifest_sha256")
    ):
        raise CampaignDisplayError("variant_selection_manifest_binding_drift")
    selection = _find_unique(
        variant_selection.get("selections"),
        lambda row: row.get("selection_id") == commitment.get("selection_id"),
        "selected_surface",
    )
    if _digest(selection) != commitment.get("selection_sha256"):
        raise CampaignDisplayError("selected_surface_hash_drift")
    release_dir = Path(str(state.get("release_dir") or "")).expanduser().resolve(strict=True)
    release = release_snapshot or campaign_control.load_style_release(release_dir)
    if release.get("release_id") != state.get("release_id") or release.get("content_sha256") != state.get("release_content_sha256"):
        raise CampaignDisplayError("style_release_drift")
    release_manifest_path = release_dir / "manifest.json"
    if _file_sha256(release_manifest_path) != state.get("release_manifest_sha256"):
        raise CampaignDisplayError("style_release_manifest_hash_drift")
    details_root = Path(str(state.get("details_root") or "")).expanduser().resolve(strict=True)
    pool = pool_snapshot or variant_bank.pool_status(details_root, release_dir)
    formal_pool = formal_pool_snapshot or formal_original.pool_status(details_root)
    raw_exposure = exposure_set or legacy_exposure.recent_exposure_set(
        repo,
        dt.date.fromisoformat(review_date),
        private_root=private_root,
        window_days=WINDOW_DAYS,
    )
    raw_rows = raw_exposure.get("rows")
    if not isinstance(raw_rows, list) or any(
        not isinstance(row, dict) for row in raw_rows
    ):
        raise CampaignDisplayError("recent_exposure_set_invalid")
    # The prepared cutoff stays stable throughout one active run. Displays
    # from this run are checked separately below for exact-item cooldown and
    # idempotency; including them here would invalidate preparation after the
    # first published question.
    cutoff_exposure = {
        "schema": raw_exposure.get("schema"),
        "review_date": raw_exposure.get("review_date"),
        "window_days": raw_exposure.get("window_days", WINDOW_DAYS),
        "rows": [dict(row) for row in raw_rows if row.get("run_id") != run_id],
    }
    mapped = campaign_control.map_recent_exposures(
        cutoff_exposure,
        release,
        variant_entries=list(pool.get("entries") or []),
        formal_original_entries=list(formal_pool.get("entries") or []),
    )
    if mapped.get("fingerprint_sha256") != state.get("exposure_fingerprint_sha256"):
        raise CampaignDisplayError("seven_day_exposure_cutoff_drift")
    return {
        "state": state,
        "prep_dir": prep_dir,
        "manifest": manifest,
        "runtime_manifest_sha256": runtime_manifest_sha,
        "run_id": run_id,
        "segment_ordinal": segment_ordinal,
        "item_offset": item_offset,
        "segment": segment,
        "item": item,
        "commitment": commitment,
        "selection": selection,
        "selection_manifest": selection_manifest,
        "release": release,
        "release_dir": release_dir,
        "details_root": details_root,
        "pool": pool,
        "formal_pool": formal_pool,
        "mapped_exposure": mapped,
    }


CURRENT_DISPLAY_SCHEMA = "morning-campaign-current-display-v1"
CURRENT_DISPLAY_INPUTS_SCHEMA = "morning-campaign-current-display-inputs-v1"


def _current_display_paths(control: Path, day: str) -> tuple[Path, Path]:
    directory = control / "current-display"
    return directory / f"{day}.json", directory / f"{day}-inputs.json"


def _stem_image_path(receipt: dict[str, Any]) -> str | None:
    assets = receipt.get("question_assets")
    if isinstance(assets, list):
        for entry in assets:
            path = entry.get("path") if isinstance(entry, dict) else None
            if isinstance(path, str) and path.startswith("/"):
                return path
    surface = receipt.get("surface_object_path")
    return surface if isinstance(surface, str) and surface.startswith("/") else None


def _write_private_json(path: Path, payload: dict[str, Any], label: str) -> None:
    """Write one private pointer atomically, then re-read it and verify the bytes."""

    directory = path.parent
    os.makedirs(directory, mode=0o700, exist_ok=True)
    temporary = directory / f".{path.name}.{os.getpid()}.tmp"
    with temporary.open("wb") as handle:
        handle.write(_canonical_bytes(payload))
        handle.flush()
        os.fsync(handle.fileno())
    os.chmod(temporary, 0o600)
    os.replace(temporary, path)
    if _read_json(path, label) != payload:
        raise CampaignDisplayError(f"{label}_drift")


def _publish_current_display_pointer(
    *,
    control: Path,
    private: Path,
    root: Path,
    runtime: Path,
    day: str,
    run_id: str,
    snapshot: dict[str, Any],
    receipt: dict[str, Any],
    sha256: str,
) -> None:
    """Record the current display and its prefilled input skeleton.

    2026-09-10 user ruling: a fresh session or a compacted context reads this
    pointer first instead of scanning private state for the display receipt.
    """

    item = snapshot["item"]
    provenance = receipt.get("question_provenance")
    if provenance is None:
        provenance = _question_provenance(item, snapshot["selection"])
    if not isinstance(provenance, dict) or provenance.get("answer_safe") is not True:
        raise CampaignDisplayError("current_display_question_provenance_unsafe")
    label = provenance.get("label")
    learner_surface = receipt.get("learner_surface")
    question_assets = receipt.get("question_assets")
    option_ids = receipt.get("option_ids_by_letter")
    if (
        not isinstance(learner_surface, dict)
        or set(learner_surface) != {"stem", "options"}
        or not isinstance(question_assets, list)
        or not isinstance(option_ids, dict)
        or set(option_ids) != set(OPTIONS)
    ):
        raise CampaignDisplayError("current_display_public_surface_invalid")
    receipt_path = str(Path(private) / "turns" / f"{sha256}.json")
    saving = campaign_runtime.saving_contract_for_run(runtime, run_id)
    pointer = {
        "schema": CURRENT_DISPLAY_SCHEMA,
        "review_date": day,
        "displayed_at": receipt.get("displayed_at"),
        "run_id": run_id,
        "item_id": receipt.get("item_id"),
        "segment_id": receipt.get("segment_id"),
        "source_id": item.get("source_id"),
        "true_item_id": receipt.get("true_item_id"),
        "formal_node_id": receipt.get("formal_node_id"),
        "surface_kind": receipt.get("surface_kind"),
        "acceptance_kind": provenance.get("acceptance_kind"),
        "source_label": label,
        "question_provenance": provenance,
        "learner_surface": learner_surface,
        "question_assets": question_assets,
        "option_ids_by_letter": option_ids,
        "stem": _stem_image_path(receipt),
        "first_variant": item.get("first_variant") is True,
        "recent_recurrence": item.get("recent_recurrence") is True,
        "stable_hash_audit": item.get("stable_hash_audit") is True,
        "mandatory_audit": item.get("mandatory_audit") is True,
        "display_receipt_path": receipt_path,
        "display_receipt_sha256": sha256,
        "saving_mode": (
            "episode_capture_every_item"
            if saving["episode_capture_every_item"]
            else "legacy_canonical_review"
        ),
        "episode_capture_every_item": saving["episode_capture_every_item"],
        **(
            {"saving_contract_sha256": saving["saving_contract_sha256"]}
            if saving["episode_capture_every_item"]
            else {}
        ),
    }
    operations = {
        "answer-choice": {
            "schema": None,
            "fill": [
                "idempotency_key", "response", "reasoning_result",
                "prompt_dependency", "learning_turns",
            ],
        },
        "finalize-reason": {
            "schema": None,
            "fill": [
                "idempotency_key", "choice_idempotency_key", "response",
                "reasoning_result", "prompt_dependency", "learning_turns",
            ],
        },
    }
    if saving["episode_capture_every_item"]:
        operations.update(
            {
                "append-turn": {
                    "schema": "morning-campaign-episode-append-input-v2",
                    "fill": [
                        "idempotency_key", "expected_prior_turn_count",
                        "expected_prior_trace_sha256", "new_turns",
                    ],
                },
                "finish-item": {
                    "schema": "morning-campaign-episode-terminal-input-v2",
                    "fill": [
                        "idempotency_key", "expected_prior_turn_count",
                        "expected_prior_trace_sha256", "new_turns",
                        "terminal_state", "final_response", "reasoning_result",
                        "prompt_dependency", "frozen_feedback", "missing_fields",
                        "attachments", "navigation_policy",
                    ],
                },
                "close-unresolved": {
                    "schema": "morning-campaign-episode-terminal-input-v2",
                    "fill": [
                        "idempotency_key", "expected_prior_turn_count",
                        "expected_prior_trace_sha256", "new_turns",
                        "missing_fields", "attachments",
                        "navigation_policy",
                    ],
                },
            }
        )
    else:
        operations.update(
            {
                "append-turn": {
                    "schema": "morning-campaign-failure-append-input-v1",
                    "fill": [
                        "idempotency_key", "expected_prior_turn_count",
                        "expected_prior_trace_sha256", "new_turns",
                    ],
                },
                "finalize-appended-and-next": {
                    "schema": "morning-campaign-failure-terminal-append-input-v1",
                    "fill": [
                        "idempotency_key", "corrected_response",
                        "reasoning_result", "prompt_dependency", "confidence",
                        "frozen_feedback", "user_error_entry",
                        "user_error_provenance", "first_action",
                        "first_action_provenance", "missing_fields",
                        "attachments", "new_turns",
                    ],
                },
            }
        )
    inputs = {
        "schema": CURRENT_DISPLAY_INPUTS_SCHEMA,
        "review_date": day,
        "display_receipt_path": receipt_path,
        "display_receipt_sha256": sha256,
        "run_id": run_id,
        "item_id": receipt.get("item_id"),
        "written_at": receipt.get("displayed_at"),
        "roots": {
            "repo_root": str(root),
            "runtime_root": str(runtime),
            "package_root": str(private),
            "private_root": str(private),
            "draft_root": str(Path(private) / ".campaign-failure-drafts-v1"),
        },
        **(
            {"saving_contract_sha256": saving["saving_contract_sha256"]}
            if saving["episode_capture_every_item"]
            else {}
        ),
        "operations": operations,
    }
    pointer_path, inputs_path = _current_display_paths(control, day)
    _write_private_json(pointer_path, pointer, "current_display_pointer")
    _write_private_json(inputs_path, inputs, "current_display_inputs")


def publish_current_display(
    repo: str | Path,
    *,
    review_date: str,
    run_id: str,
    control_root: str | Path | None = None,
    runtime_root: str | Path | None = None,
    private_root: str | Path | None = None,
    displayed_at: str | None = None,
    exposure_set: dict[str, Any] | None = None,
    release_snapshot: dict[str, Any] | None = None,
    pool_snapshot: dict[str, Any] | None = None,
    formal_pool_snapshot: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Publish the first/current active frontier and return an answer-safe surface."""

    root = campaign_control._repo_root(repo)  # type: ignore[attr-defined]
    day = campaign_control._safe_date(review_date)  # type: ignore[attr-defined]
    control, runtime = campaign_control._roots(root, control_root, runtime_root)  # type: ignore[attr-defined]
    private = _private_root(root, private_root)
    timestamp = _safe_time(displayed_at)
    actual_displayed_date = (
        dt.datetime.fromisoformat(timestamp)
        .astimezone(ZoneInfo("Asia/Shanghai"))
        .date()
        .isoformat()
    )

    # This pass is intentionally before any directory or lock-file creation.
    initial = _frontier_snapshot(
        root,
        review_date=day,
        run_id=run_id,
        control_root=control,
        runtime_root=runtime,
        private_root=private,
        exposure_set=exposure_set,
        release_snapshot=release_snapshot,
        pool_snapshot=pool_snapshot,
        formal_pool_snapshot=formal_pool_snapshot,
    )
    def _publish_locked(
        locked_frontier: tuple[
            Path,
            dict[str, Any],
            dict[str, Any],
            dict[str, Any] | None,
        ],
    ) -> dict[str, Any]:
        snapshot = _frontier_snapshot(
            root,
            review_date=day,
            run_id=run_id,
            control_root=control,
            runtime_root=runtime,
            private_root=private,
            exposure_set=exposure_set,
            release_snapshot=release_snapshot,
            pool_snapshot=pool_snapshot,
            formal_pool_snapshot=formal_pool_snapshot,
            verified_frontier=locked_frontier,
        )
        if (
            snapshot["item"]["item_id"] != initial["item"]["item_id"]
            or snapshot["runtime_manifest_sha256"] != initial["runtime_manifest_sha256"]
        ):
            raise CampaignDisplayError("campaign_frontier_changed_during_publication")
        turns = _campaign_turns(private)
        same_frontier = [
            (sha, receipt)
            for sha, receipt in turns
            if receipt.get("run_id") == run_id
            and receipt.get("segment_ordinal") == snapshot["segment_ordinal"]
            and receipt.get("item_offset") == snapshot["item_offset"]
        ]
        if len(same_frontier) > 1:
            raise CampaignDisplayError("campaign_frontier_has_multiple_display_receipts")
        if same_frontier:
            sha, receipt = same_frontier[0]
            _validate_existing_receipt(receipt, snapshot)
            _publish_current_display_pointer(
                control=control,
                private=private,
                root=root,
                runtime=runtime,
                day=day,
                run_id=run_id,
                snapshot=snapshot,
                receipt=receipt,
                sha256=sha,
            )
            return _public_result(
                receipt,
                sha,
                repeated=True,
                item=snapshot["item"],
                selection=snapshot["selection"],
                private_root=private,
            )
        current_date = dt.date.fromisoformat(actual_displayed_date)
        for _, receipt in turns:
            if receipt.get("true_item_id") != snapshot["item"]["true_item_id"]:
                continue
            try:
                prior = dt.date.fromisoformat(str(receipt.get("displayed_date") or ""))
            except ValueError as exc:
                raise CampaignDisplayError("campaign_exposure_date_invalid") from exc
            gap = (current_date - prior).days
            if 0 <= gap < WINDOW_DAYS:
                raise CampaignDisplayError("exact_true_item_recently_exposed")
        if snapshot["item"]["surface_kind"] == "verified_variant":
            surface = _variant_surface(
                details_root=snapshot["details_root"],
                item=snapshot["item"],
                selection=snapshot["selection"],
                pool=snapshot["pool"],
            )
        elif snapshot["item"]["surface_kind"] == "formal_original":
            surface = _formal_original_surface(
                details_root=snapshot["details_root"],
                item=snapshot["item"],
                selection=snapshot["selection"],
                pool=snapshot["formal_pool"],
            )
        else:
            surface = _canonical_surface(
                details_root=snapshot["details_root"],
                release_dir=snapshot["release_dir"],
                release_id=snapshot["release"]["release_id"],
                item=snapshot["item"],
                selection=snapshot["selection"],
            )
        learner_surface = {"stem": surface["stem"], "options": surface["options"]}
        question_provenance = _question_provenance(
            snapshot["item"], snapshot["selection"]
        )
        receipt = {
            "schema": TURN_SCHEMA,
            "receipt_schema": DISPLAY_RECEIPT_SCHEMA,
            "status": DISPLAY_STATUS,
            "run_id": run_id,
            "campaign_date": snapshot["manifest"]["campaign_date"],
            "segment_id": snapshot["segment"]["segment_id"],
            "segment_ordinal": snapshot["segment_ordinal"],
            "item_offset": snapshot["item_offset"],
            "item_id": snapshot["item"]["item_id"],
            "formal_node_id": snapshot["item"]["formal_node_id"],
            "true_item_id": snapshot["item"]["true_item_id"],
            "variant_id": snapshot["item"]["variant_id"],
            "surface_kind": snapshot["item"]["surface_kind"],
            "acceptance_kind": question_provenance["acceptance_kind"],
            "saving_mode": (
                "episode_capture_every_item"
                if snapshot["manifest"].get("saving_contract") is not None
                else "legacy_canonical_review"
            ),
            "episode_capture_every_item": snapshot["manifest"].get(
                "saving_contract"
            ) is not None,
            **(
                {
                    "saving_contract_sha256": snapshot["manifest"][
                        "saving_contract_sha256"
                    ]
                }
                if snapshot["manifest"].get("saving_contract_sha256")
                else {}
            ),
            "runtime_manifest_sha256": snapshot["runtime_manifest_sha256"],
            "campaign_manifest_sha256": snapshot["state"]["campaign_manifest_sha256"],
            "segment_commitment_sha256": snapshot["segment"]["commitment_sha256"],
            "campaign_item_commitment_sha256": snapshot["item"]["campaign_item_commitment_sha256"],
            "pack_commitment_sha256": snapshot["item"]["pack_commitment_sha256"],
            "surface_sha256": snapshot["item"]["surface_sha256"],
            "surface_object_path": surface["surface_object_path"],
            "surface_object_sha256": surface["surface_object_sha256"],
            "grading_sha256": snapshot["item"]["grading_sha256"],
            "grading_capsule_ref": surface["grading_capsule_ref"],
            "grading_capsule_path": surface["grading_capsule_path"],
            "grading_capsule_sha256": surface["grading_capsule_sha256"],
            "style_release_id": snapshot["release"]["release_id"],
            "style_release_content_sha256": snapshot["release"]["content_sha256"],
            "style_release_manifest_sha256": snapshot["state"]["release_manifest_sha256"],
            "selection_id": snapshot["selection"]["selection_id"],
            "selection_sha256": snapshot["commitment"]["selection_sha256"],
            "selection_manifest_sha256": snapshot["state"]["selection_manifest_sha256"],
            "option_ids_by_letter": surface["option_ids_by_letter"],
            "learner_surface": learner_surface,
            "question_provenance": question_provenance,
            **({"review_scope": snapshot["state"]["review_scope"]} if snapshot["state"].get("review_scope") else {}),
            "display_content_sha256": _surface_content_sha(surface["stem"], surface["options"]),
            "question_asset_ref": surface["question_asset_ref"],
            "question_asset_sha256": surface["question_asset_sha256"],
            "question_assets": surface["question_assets"],
            "terminal_evidence_assets": surface["terminal_evidence_assets"],
            "displayed_date": actual_displayed_date,
            "displayed_at": timestamp,
            "exposure_cutoff": {
                "schema": "morning-campaign-exposure-cutoff-v1",
                "window_days": WINDOW_DAYS,
                "review_date": day,
                "prepared_fingerprint_sha256": snapshot["state"]["exposure_fingerprint_sha256"],
                "verified_fingerprint_sha256": snapshot["mapped_exposure"]["fingerprint_sha256"],
                "legacy_exposure_count": snapshot["mapped_exposure"]["exposure_count"],
                "successor_receipt_count_before_display": len(turns),
            },
            "answer_safe": True,
            "answer_content_in_receipt": False,
            "explanation_content_in_receipt": False,
            "capture_write_count": 0,
            "formal_write_count": 0,
        }
        published = private_evidence.publish_metadata_object(
            receipt, kind="turns", private_root=private
        )
        _publish_current_display_pointer(
            control=control,
            private=private,
            root=root,
            runtime=runtime,
            day=day,
            run_id=run_id,
            snapshot=snapshot,
            receipt=receipt,
            sha256=published["sha256"],
        )
        return _public_result(
            receipt,
            published["sha256"],
            repeated=False,
            item=snapshot["item"],
            selection=snapshot["selection"],
            private_root=private,
        )

    try:
        with campaign_runtime.verified_frontier_guard(
            runtime, run_id
        ) as locked_frontier:
            return _publish_locked(locked_frontier)
    except private_evidence.CurrentQuestionEvidenceError as exc:
        raise CampaignDisplayError(str(exc)) from exc


def read_display_receipt(
    locator: str, *, private_root: str | Path | None = None
) -> dict[str, Any]:
    value = private_evidence.read_metadata_object(
        locator, kind="turns", private_root=private_root
    )
    if (
        value.get("receipt_schema") != DISPLAY_RECEIPT_SCHEMA
        or value.get("status") != DISPLAY_STATUS
        or value.get("answer_safe") is not True
        or value.get("formal_write_count") != 0
    ):
        raise CampaignDisplayError("campaign_display_receipt_invalid")
    return value


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo", default=".")
    parser.add_argument("--control-root")
    parser.add_argument("--runtime-root")
    parser.add_argument("--private-root")
    sub = parser.add_subparsers(dest="command", required=True)
    publish = sub.add_parser("publish-current")
    publish.add_argument("--review-date", required=True)
    publish.add_argument("--run-id", required=True)
    publish.add_argument("--displayed-at")
    return parser


def main() -> int:
    args = build_parser().parse_args()
    try:
        result = publish_current_display(
            args.repo,
            review_date=args.review_date,
            run_id=args.run_id,
            control_root=args.control_root,
            runtime_root=args.runtime_root,
            private_root=args.private_root,
            displayed_at=args.displayed_at,
        )
    except (
        CampaignDisplayError,
        campaign_control.CampaignControlError,
        campaign_runtime.CampaignRuntimeError,
        variant_bank.VariantBankError,
        formal_original.FormalOriginalSurfaceError,
        legacy_exposure.QuestionExposureError,
    ) as exc:
        print(json.dumps({"status": "blocked", "error": str(exc), "formal_write_count": 0}, ensure_ascii=False))
        return 2
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())


__all__ = [
    "CampaignDisplayError",
    "DISPLAY_RECEIPT_SCHEMA",
    "DISPLAY_RESULT_SCHEMA",
    "DISPLAY_STATUS",
    "QUESTION_PROVENANCE_SCHEMA",
    "publish_current_display",
    "read_display_receipt",
]
