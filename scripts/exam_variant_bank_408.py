#!/usr/bin/env python3
"""Offline, content-addressed 408 exam-variant bank and surface resolver.

The workflow deliberately has no model or network integration.  An external
author and two independent verifiers submit JSON files; this module validates
and binds those submissions before a learner-visible surface can be selected.
Full stems, options, keys, explanations, and verifier material live only below
the caller-supplied private details root.
"""

from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import itertools
import json
import os
import re
import tempfile
import unicodedata
from pathlib import Path
from typing import Any, Iterable


BANK_SCHEMA = "exam_variant_bank_v1"
TARGET_SET_SCHEMA = "exam_variant_target_set_v1"
REQUEST_SCHEMA = "exam_variant_plan_request_v1"
AUTHOR_SCHEMA = "exam_variant_author_submission_v1"
BLIND_INPUT_SCHEMA = "exam_variant_blind_input_v1"
BLIND_SCHEMA = "exam_variant_blind_verification_v1"
ADVERSARIAL_SCHEMA = "exam_variant_adversarial_verification_v1"
SURFACE_SCHEMA = "exam_variant_question_surface_v1"
ANSWER_SCHEMA = "exam_variant_answer_capsule_v1"
MANIFEST_SCHEMA = "exam_variant_published_manifest_v1"
INDEX_SCHEMA = "exam_variant_bank_index_v1"
SELECTION_SCHEMA = "exam_variant_surface_selection_v1"
POOL_STATUS_SCHEMA = "exam_variant_pool_status_v1"
FORECAST_SCHEMA = "exam_variant_forecast_due_set_v1"
CONSUMPTION_SCHEMA = "exam_variant_consumption_receipt_v1"
CONSUMPTION_REQUEST_SCHEMA = "exam_variant_consumption_request_v1"
SELECTION_MANIFEST_SCHEMA = "exam_variant_selection_manifest_v1"
CONSUMPTION_BATCH_SCHEMA = "exam_variant_consumption_batch_receipt_v1"
CONSUMPTION_REBIND_SCHEMA = "exam_variant_consumption_batch_rebind_receipt_v1"
CAMPAIGN_COMMITMENT_SCHEMA = "exam_variant_campaign_commitment_set_v1"
WEB_SUBMISSION_SCHEMA = "exam_variant_web_self_check_submission_v1"
WEB_GRADING_SCHEMA = "exam_variant_web_grading_v1"
WEB_SOLUTION_SCHEMA = "exam_variant_web_solution_v1"
WEB_SELF_CHECK_SCHEMA = "exam_variant_web_self_check_v1"
WEB_ACCEPTANCE_KIND = "web_self_checked_ready"
INDEPENDENT_ACCEPTANCE_KIND = "verified_ready"

FORMAL_NODE_RE = re.compile(r"^(?:DS|CO|OS|CN)_(?:\d{4}|UNK)_\d{3}$")
KNOWLEDGE_RE = re.compile(r"^(?:DS|CO|OS|CN)\d{2}-\d{2}$")
STYLE_PROFILE_RE = re.compile(r"^ESP-[0-9a-f]{16}$")
CANONICAL_RE = re.compile(r"^EXAM408-20(?:0[9]|1\d|2[0-5])-Q(?:0[1-9]|[1-3]\d|4[0-7])$")
SHA_RE = re.compile(r"^[0-9a-f]{64}$")
SAFE_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:-]{2,127}$")
OPTION_ID_RE = re.compile(r"^OPT-[A-Za-z0-9][A-Za-z0-9_-]{0,31}$")
LEVELS = {"V2", "V3"}
LABELS = ("A", "B", "C", "D")
MAX_JSON_BYTES = 4 * 1024 * 1024
DEFAULT_BANK_REL = Path("exam-variant-bank/v1")
FINAL_PRODUCTION_RELEASE_ID = "ESR-965a1372f11756c6d55f170b"
ACCEPTED_PRODUCTION_RELEASE_IDS = frozenset({
    FINAL_PRODUCTION_RELEASE_ID,
    "ESR-1f9b072c86af0b66e7cdee19",  # Accepted complete preprocessed images; old runs retain their release.
})
PRODUCTION_RELEASE_ROOT = Path(
    "/Users/your-user/Documents/kaoyan-408-details/corpus/408统考真题/style-releases"
)
ANSWER_LEAK_RE = re.compile(
    r"(?:正确答案|答案(?:是|为)|correct\s+answer|answer\s*[:：])\s*[A-DOPT0-9_-]*",
    re.IGNORECASE,
)

BLIND_CHECKS = {
    "four_distinct_options",
    "one_best_answer_derived_without_key",
    "style_profile_match",
    "target_knowledge_match",
    "no_answer_leakage",
    "facts_traceable",
}
ADVERSARIAL_CHECKS = {
    "unique_key",
    "all_distractors_plausible",
    "all_misconceptions_specific",
    "all_refutations_valid",
    "permutation_stable",
    "facts_source_bound",
    "answer_isolated",
    "variant_level_gate",
    "no_unintended_structural_change",
}
WEB_SELF_CHECKS = {
    "answer_isolated",
    "counterexample_search_complete",
    "distractors_plausible",
    "facts_traceable",
    "four_distinct_options",
    "one_best_answer_derived",
    "refutations_consistent",
    "style_profile_match",
    "target_knowledge_match",
    "unique_key",
    "variant_level_gate",
}
STYLE_SPEC_FIELDS = {
    "condition_density",
    "data_representation",
    "legal_parameter_ranges",
    "calculation_scale",
    "polarity",
    "option_parallelism",
    "reasoning_depth",
    "distractor_mechanisms",
    "v2_invariant_axes",
    "v2_mutable_axes",
    "v3_invariant_axes",
    "v3_mutable_axes",
}
BLIND_BOUNDARY_FIELDS = {
    "unit",
    "bit_width",
    "boundary",
    "state_transition",
    "protocol_direction",
}


class VariantBankError(RuntimeError):
    """Stable fail-closed error for the offline variant bank."""


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


def _same_campaign_variant_reservation(
    left: object, right: object
) -> bool:
    if not isinstance(left, dict) or not isinstance(right, dict):
        return False
    return {
        key: value for key, value in left.items() if key != "selection_id"
    } == {key: value for key, value in right.items() if key != "selection_id"}


def _normalize(value: object) -> str:
    return re.sub(
        r"\s+", " ", unicodedata.normalize("NFKC", str(value or ""))
    ).strip()


def _load_json(path: Path, label: str) -> dict[str, Any]:
    if path.is_symlink() or not path.is_file():
        raise VariantBankError(f"{label}_missing_or_unsafe")
    raw = path.read_bytes()
    if not 0 < len(raw) <= MAX_JSON_BYTES:
        raise VariantBankError(f"{label}_size_invalid")
    try:
        value = json.loads(raw)
    except (UnicodeError, json.JSONDecodeError) as exc:
        raise VariantBankError(f"{label}_invalid_json") from exc
    if not isinstance(value, dict):
        raise VariantBankError(f"{label}_root_not_object")
    return value


def _load_jsonl(path: Path, label: str) -> list[dict[str, Any]]:
    if path.is_symlink() or not path.is_file():
        raise VariantBankError(f"{label}_missing_or_unsafe")
    rows: list[dict[str, Any]] = []
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except (OSError, UnicodeError) as exc:
        raise VariantBankError(f"{label}_unreadable") from exc
    for line_number, line in enumerate(lines, 1):
        if not line.strip():
            continue
        try:
            row = json.loads(line)
        except json.JSONDecodeError as exc:
            raise VariantBankError(f"{label}_invalid_jsonl_{line_number}") from exc
        if not isinstance(row, dict):
            raise VariantBankError(f"{label}_row_not_object_{line_number}")
        rows.append(row)
    return rows


def _atomic_write(path: Path, payload: bytes) -> str:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)
    return _sha256_bytes(payload)


def _write_immutable_json(path: Path, value: Any) -> str:
    payload = _pretty_bytes(value)
    digest = _sha256_bytes(payload)
    if path.exists():
        if path.is_symlink() or not path.is_file() or path.read_bytes() != payload:
            raise VariantBankError("immutable_object_conflict")
        return digest
    _atomic_write(path, payload)
    return digest


def _content_write(directory: Path, value: Any) -> tuple[Path, str]:
    payload = _pretty_bytes(value)
    digest = _sha256_bytes(payload)
    path = directory / f"{digest}.json"
    _write_immutable_json(path, value)
    return path, digest


def _root(path: str | Path) -> Path:
    root = Path(path).expanduser().resolve()
    root.mkdir(parents=True, exist_ok=True)
    if root.is_symlink() or not root.is_dir():
        raise VariantBankError("details_root_unsafe")
    return root


def _bank_root(details_root: str | Path) -> tuple[Path, Path]:
    details = _root(details_root)
    bank = details / DEFAULT_BANK_REL
    bank.mkdir(parents=True, exist_ok=True)
    return details, bank


def _relative(path: Path, root: Path) -> str:
    return path.resolve(strict=True).relative_to(root.resolve(strict=True)).as_posix()


def _managed_ref(root: Path, ref: str | Path, label: str) -> Path:
    raw = Path(ref)
    if ".." in raw.parts:
        raise VariantBankError(f"{label}_unsafe_ref")
    lexical = raw if raw.is_absolute() else root / raw
    if lexical.is_symlink():
        raise VariantBankError(f"{label}_unsafe_ref")
    try:
        path = lexical.resolve(strict=True)
        path.relative_to(root.resolve(strict=True))
    except (OSError, ValueError) as exc:
        raise VariantBankError(f"{label}_outside_details_root") from exc
    if path.is_symlink() or not path.is_file():
        raise VariantBankError(f"{label}_missing_or_unsafe")
    return path


def _web_output_json(
    output_root: str | Path, ref: object, expected_sha256: object, label: str
) -> tuple[dict[str, Any], Path, str]:
    """Open one hash-bound JSON object from an already verified D output tree."""

    root = Path(output_root).expanduser().resolve(strict=True)
    if root.is_symlink() or not root.is_dir():
        raise VariantBankError("web_output_root_unsafe")
    raw = Path(str(ref or ""))
    if raw.is_absolute() or not raw.parts or ".." in raw.parts:
        raise VariantBankError(f"{label}_unsafe_ref")
    lexical = root / raw
    if lexical.is_symlink() or any(parent.is_symlink() for parent in lexical.parents):
        raise VariantBankError(f"{label}_unsafe_ref")
    try:
        path = lexical.resolve(strict=True)
        path.relative_to(root)
    except (OSError, ValueError) as exc:
        raise VariantBankError(f"{label}_outside_output_root") from exc
    digest = _sha256_file(path)
    if not SHA_RE.fullmatch(str(expected_sha256 or "")) or digest != expected_sha256:
        raise VariantBankError(f"{label}_hash_drift")
    return _load_json(path, label), path, digest


def _content_ref(root: Path, ref: str | Path, label: str) -> tuple[dict[str, Any], Path, str]:
    path = _managed_ref(root, ref, label)
    digest = _sha256_file(path)
    if path.stem != digest or path.suffix != ".json":
        raise VariantBankError(f"{label}_not_content_addressed")
    return _load_json(path, label), path, digest


def _iso_date(value: object, label: str) -> dt.date:
    try:
        return dt.date.fromisoformat(str(value or ""))
    except ValueError as exc:
        raise VariantBankError(f"{label}_invalid") from exc


def _release_snapshot(
    release_dir: str | Path,
    *,
    require_final_production_release: bool = True,
) -> dict[str, Any]:
    release = Path(release_dir).expanduser().resolve(strict=True)
    if release.is_symlink() or not release.is_dir():
        raise VariantBankError("exam_style_release_unsafe")
    if (
        require_final_production_release
        and release.parent == PRODUCTION_RELEASE_ROOT.resolve()
        and release.name not in ACCEPTED_PRODUCTION_RELEASE_IDS
    ):
        raise VariantBankError("intermediate_exam_style_release_forbidden")
    manifest = _load_json(release / "manifest.json", "exam_style_manifest")
    if manifest.get("schema_version") != "exam_style_release_v1":
        raise VariantBankError("exam_style_release_schema_invalid")
    content = {
        key: value
        for key, value in manifest.items()
        if key not in {"release_id", "content_sha256"}
    }
    content_sha = _digest(content)
    release_id = f"ESR-{content_sha[:24]}"
    if (
        manifest.get("content_sha256") != content_sha
        or manifest.get("release_id") != release_id
        or release.name != release_id
    ):
        raise VariantBankError("exam_style_release_content_binding_invalid")
    artifacts = manifest.get("artifacts")
    if not isinstance(artifacts, dict):
        raise VariantBankError("exam_style_release_artifacts_invalid")
    for name, record in artifacts.items():
        if not isinstance(record, dict) or Path(name).name != name:
            raise VariantBankError("exam_style_release_artifact_record_invalid")
        path = release / name
        if (
            path.is_symlink()
            or not path.is_file()
            or _sha256_file(path) != record.get("sha256")
            or path.stat().st_size != record.get("bytes")
        ):
            raise VariantBankError(f"exam_style_release_artifact_drift:{name}")
    gates = manifest.get("gates") or {}
    v2 = gates.get("v2_canonical_exposure") or {}
    v3 = gates.get("v3_style_generation") or {}
    if (
        v2.get("status") != "pass"
        or v3.get("requires_independent_author") is not True
        or v3.get("requires_independent_verifier") is not True
        or v3.get("requires_source_commitment") is not True
        or v3.get("requires_answer_isolation") is not True
        or v3.get("release_itself_grants_generation_authority") is not False
        or (manifest.get("provisional_2026") or {}).get("production_usable_question_count") != 0
    ):
        raise VariantBankError("exam_style_release_generation_gate_invalid")
    exposures = _load_jsonl(release / "canonical-exposure.jsonl", "canonical_exposure")
    profiles = _load_jsonl(release / "style-profiles.jsonl", "style_profiles")
    exposure_by_id: dict[str, dict[str, Any]] = {}
    for row in exposures:
        canonical_id = str(row.get("canonical_id") or "")
        if (
            row.get("schema_version") != "canonical_question_exposure_v1"
            or not CANONICAL_RE.fullmatch(canonical_id)
            or row.get("production_usable") not in {True, False}
            or row.get("style_observation_allowed") is not True
            or row.get("answer_content_in_release") is not False
            or canonical_id in exposure_by_id
        ):
            raise VariantBankError("canonical_exposure_invalid")
        exposure_by_id[canonical_id] = row
        if (
            release.parent == PRODUCTION_RELEASE_ROOT.resolve()
            and row.get("production_usable") is True
            and row.get("exposure_mode") == "direct_pool"
        ):
            sibling = (
                release.parent.parent
                / "style-releases-private"
                / release_id
                / "answer-capsules"
                / f"{canonical_id}.json"
            )
            commitment = str(row.get("answer_capsule_commitment_sha256") or "")
            if (
                not SHA_RE.fullmatch(commitment)
                or sibling.is_symlink()
                or not sibling.is_file()
                or _sha256_file(sibling) != commitment
            ):
                raise VariantBankError("production_answer_capsule_binding_invalid")
    profile_by_id: dict[str, dict[str, Any]] = {}
    for row in profiles:
        profile_id = str(row.get("profile_id") or "")
        canonical_ids = row.get("canonical_ids")
        if (
            row.get("schema_version") != "exam_style_profile_v1"
            or not STYLE_PROFILE_RE.fullmatch(profile_id)
            or not isinstance(canonical_ids, list)
            or not canonical_ids
            or any(item not in exposure_by_id for item in canonical_ids)
            or profile_id in profile_by_id
        ):
            raise VariantBankError("style_profile_invalid")
        profile_by_id[profile_id] = row
    if sum(int(row.get("question_count", -1)) for row in profiles) != len(exposures):
        raise VariantBankError("style_profile_aggregate_invalid")
    return {
        "release_dir": release,
        "manifest": manifest,
        "release_id": release_id,
        "content_sha256": content_sha,
        "exposure_by_id": exposure_by_id,
        "profile_by_id": profile_by_id,
    }


def _targets_value(value: dict[str, Any] | list[dict[str, Any]]) -> list[dict[str, Any]]:
    if isinstance(value, dict):
        if value.get("schema") != TARGET_SET_SCHEMA or not isinstance(value.get("targets"), list):
            raise VariantBankError("target_set_invalid")
        targets = value["targets"]
    else:
        targets = value
    if not isinstance(targets, list) or not targets:
        raise VariantBankError("targets_missing")
    if any(not isinstance(row, dict) for row in targets):
        raise VariantBankError("target_not_object")
    return targets


def _string_list(value: object, label: str, *, minimum: int = 1) -> list[str]:
    if not isinstance(value, list):
        raise VariantBankError(f"{label}_invalid")
    rows = [_normalize(item) for item in value]
    if len(rows) < minimum or any(not item for item in rows) or len(set(rows)) != len(rows):
        raise VariantBankError(f"{label}_invalid")
    return rows


def _deep_style_spec(value: object, *, web_profile: dict[str, Any] | None = None,
                     variant_level: str | None = None) -> dict[str, Any]:
    """Freeze the successor-only style constraints absent from ESR v1."""

    web_metadata = {"cognitive_action", "reasoning_depth_bucket", "representation_note"}
    if not isinstance(value, dict) or (
        set(value) != STYLE_SPEC_FIELDS if web_profile is None else
        not STYLE_SPEC_FIELDS <= set(value) or not set(value) <= STYLE_SPEC_FIELDS | web_metadata
    ):
        raise VariantBankError("deep_style_spec_incomplete")
    if web_profile is not None:
        if "cognitive_action" in value and value["cognitive_action"] != web_profile.get("cognitive_action"):
            raise VariantBankError("web_style_cognitive_action_drift")
        if "reasoning_depth_bucket" in value and value["reasoning_depth_bucket"] not in (web_profile.get("reasoning_depth") or {}):
            raise VariantBankError("web_style_reasoning_bucket_drift")
        if "representation_note" in value and not isinstance(value["representation_note"], str):
            raise VariantBankError("web_style_representation_note_invalid")
    condition_density = str(value.get("condition_density") or "")
    data_representation = str(value.get("data_representation") or "")
    calculation_scale = str(value.get("calculation_scale") or "")
    polarity = str(value.get("polarity") or "")
    option_parallelism = str(value.get("option_parallelism") or "")
    reasoning_depth = value.get("reasoning_depth")
    # ESR's published profile vocabulary is a separate, more descriptive
    # dialect. Keep its actual values on the web path rather than inventing a
    # different semantic label or rewriting the author's question.
    web_representations = {"address_or_bit_layout", "code_or_pseudocode", "formula_or_symbolic",
        "graph_tree_or_topology", "multi_asset_visual", "natural_language_scenario",
        "sequence_or_trace", "table_or_matrix", "timing_or_state_diagram"} if web_profile is not None else set()
    web_calculations = {"local_single_step", "short_multi_step", "extended_multi_stage"} if web_profile is not None else set()
    web_parallelism = {"low", "medium", "high"} if web_profile is not None else set()
    if (
        condition_density not in {"low", "medium", "high"}
        or data_representation not in web_representations | {
            "plain_text",
            "statement_set",
            "numeric_parameters",
            "sequence",
            "table",
            "diagram_description",
            "protocol_trace",
            "state_trace",
        }
        or calculation_scale not in web_calculations | {"none", "single_step", "multi_step", "mixed_symbolic_numeric"}
        or polarity not in {"positive", "negative", "mixed", "neutral_or_implicit"}
        or option_parallelism not in web_parallelism | {"strict", "paired", "controlled_asymmetry"}
        or not isinstance(reasoning_depth, int)
        or not 1 <= reasoning_depth <= 5
    ):
        raise VariantBankError("deep_style_spec_controlled_value_invalid")
    ranges = value.get("legal_parameter_ranges")
    if not isinstance(ranges, list) or not ranges:
        raise VariantBankError("legal_parameter_ranges_missing")
    normalized_ranges: list[dict[str, str]] = []
    for row in ranges:
        if not isinstance(row, dict) or set(row) != {"parameter", "constraint", "unit"}:
            raise VariantBankError("legal_parameter_range_invalid")
        parameter = _normalize(row.get("parameter"))
        constraint = _normalize(row.get("constraint"))
        unit = _normalize(row.get("unit"))
        if not parameter or not constraint or not unit:
            raise VariantBankError("legal_parameter_range_invalid")
        normalized_ranges.append(
            {"parameter": parameter, "constraint": constraint, "unit": unit}
        )
    distractor_mechanisms = _string_list(
        value.get("distractor_mechanisms"), "distractor_mechanisms", minimum=1 if web_profile is not None else 3
    )
    v2_invariant = _string_list(value.get("v2_invariant_axes"), "v2_invariant_axes")
    v2_mutable = _string_list(value.get("v2_mutable_axes"), "v2_mutable_axes", minimum=2)
    v3_invariant = _string_list(value.get("v3_invariant_axes"), "v3_invariant_axes")
    v3_mutable = _string_list(value.get("v3_mutable_axes"), "v3_mutable_axes",
                              minimum=0 if web_profile is not None and variant_level == "V2" else 1)
    if set(v2_invariant) & set(v2_mutable) or set(v3_invariant) & set(v3_mutable):
        raise VariantBankError("style_invariant_mutable_axes_overlap")
    return {
        "condition_density": condition_density,
        "data_representation": data_representation,
        "legal_parameter_ranges": normalized_ranges,
        "calculation_scale": calculation_scale,
        "polarity": polarity,
        "option_parallelism": option_parallelism,
        "reasoning_depth": reasoning_depth,
        "distractor_mechanisms": distractor_mechanisms,
        "v2_invariant_axes": v2_invariant,
        "v2_mutable_axes": v2_mutable,
        "v3_invariant_axes": v3_invariant,
        "v3_mutable_axes": v3_mutable,
    }


def _style_fallback(
    value: object,
    *,
    target_main_knowledge: str,
    profile: dict[str, Any],
) -> dict[str, str] | None:
    profile_knowledge = str(profile.get("main_knowledge") or "")
    if profile_knowledge == target_main_knowledge:
        if value is not None:
            raise VariantBankError("exact_style_profile_must_not_claim_fallback")
        return None
    if not isinstance(value, dict) or set(value) != {
        "scope",
        "profile_main_knowledge",
        "cognitive_action",
        "question_form",
        "confidence",
    }:
        raise VariantBankError("style_profile_fallback_required")
    if (
        value.get("scope") != "same_module_same_cognitive_action"
        or value.get("confidence") != "low"
        or value.get("profile_main_knowledge") != profile_knowledge
        or value.get("cognitive_action") != profile.get("cognitive_action")
        or value.get("question_form") != profile.get("question_form")
        or not str(profile.get("question_form") or "").startswith("single_choice")
        or target_main_knowledge.split("-", 1)[0]
        != profile_knowledge.split("-", 1)[0]
    ):
        raise VariantBankError("style_profile_fallback_scope_invalid")
    return {key: str(value[key]) for key in sorted(value)}


def _profile_axis_constraints(profile: dict[str, Any]) -> dict[str, Any]:
    v2 = profile.get("v2_axes")
    v3 = profile.get("v3_allowed_single_structural_edges")
    if not isinstance(v2, dict) or not isinstance(v3, list) or not v3:
        raise VariantBankError("style_profile_axis_contract_missing")
    invariants = _string_list(v2.get("invariant_axes"), "profile_v2_invariant_axes")
    mutable = _string_list(v2.get("mutable_surface_axes"), "profile_v2_mutable_axes")
    if set(invariants) & set(mutable):
        raise VariantBankError("style_profile_axis_contract_overlap")
    allowed_edges: list[dict[str, Any]] = []
    for row in v3:
        if not isinstance(row, dict):
            raise VariantBankError("style_profile_v3_edge_invalid")
        axis = _normalize(row.get("mutable_axis"))
        edge = _normalize(row.get("edge"))
        transition = _normalize(row.get("allowed_transition"))
        if (
            not axis
            or not edge
            or not transition
            or row.get("max_changed_structural_axes") != 1
        ):
            raise VariantBankError("style_profile_v3_edge_invalid")
        allowed_edges.append(
            {
                "mutable_axis": axis,
                "edge": edge,
                "allowed_transition": transition,
                "max_changed_structural_axes": 1,
            }
        )
    if len({(row["mutable_axis"], row["edge"]) for row in allowed_edges}) != len(allowed_edges):
        raise VariantBankError("style_profile_v3_edge_duplicate")
    return {
        "v2_invariant_axes": invariants,
        "v2_mutable_surface_axes": mutable,
        "v3_allowed_single_structural_edges": allowed_edges,
    }


def plan_request(
    details_root: str | Path,
    release_dir: str | Path,
    targets: dict[str, Any] | list[dict[str, Any]],
) -> dict[str, Any]:
    """Validate target bindings and create 3-5 immutable candidate slots each."""

    details, bank = _bank_root(details_root)
    release = _release_snapshot(release_dir)
    planned_targets: list[dict[str, Any]] = []
    seen_nodes: set[str] = set()
    for target in _targets_value(targets):
        formal_node_id = str(target.get("formal_node_id") or "")
        formal_node_sha = str(target.get("formal_node_sha256") or "")
        profile_id = str(target.get("style_profile_id") or "")
        main_knowledge = str(target.get("main_knowledge") or "")
        anchors = target.get("verified_canonical_anchor_ids")
        slot_count = target.get("candidate_slots", 3)
        levels = target.get("candidate_levels")
        deep_style_spec = _deep_style_spec(target.get("deep_style_spec"))
        if (
            not FORMAL_NODE_RE.fullmatch(formal_node_id)
            or formal_node_id in seen_nodes
            or not SHA_RE.fullmatch(formal_node_sha)
            or not isinstance(anchors, list)
            or not anchors
            or len(set(anchors)) != len(anchors)
            or not isinstance(slot_count, int)
            or not 3 <= slot_count <= 5
        ):
            raise VariantBankError("target_binding_invalid")
        profile = release["profile_by_id"].get(profile_id)
        if profile is None:
            raise VariantBankError("target_style_profile_mismatch")
        style_fallback = _style_fallback(
            target.get("style_fallback"),
            target_main_knowledge=main_knowledge,
            profile=profile,
        )
        profile_constraints = _profile_axis_constraints(profile)
        profile_invariants = set(profile_constraints["v2_invariant_axes"])
        profile_v2_mutable = set(profile_constraints["v2_mutable_surface_axes"])
        profile_v3_mutable = {
            row["mutable_axis"]
            for row in profile_constraints["v3_allowed_single_structural_edges"]
        }
        if not profile_invariants.issubset(set(deep_style_spec["v2_invariant_axes"])):
            raise VariantBankError("target_v2_invariants_do_not_preserve_profile")
        if not set(deep_style_spec["v2_mutable_axes"]).issubset(profile_v2_mutable):
            raise VariantBankError("target_v2_mutable_axes_exceed_profile")
        if not set(deep_style_spec["v3_mutable_axes"]).issubset(profile_v3_mutable):
            raise VariantBankError("target_v3_mutable_axes_exceed_profile")
        profile_anchors = set(profile["canonical_ids"])
        if any(
            not isinstance(anchor, str)
            or anchor not in profile_anchors
            or anchor not in release["exposure_by_id"]
            or release["exposure_by_id"][anchor].get("production_usable") is not True
            for anchor in anchors
        ):
            raise VariantBankError("target_canonical_anchor_unverified")
        if levels is None:
            levels = ["V2" if index % 3 != 2 else "V3" for index in range(slot_count)]
        if (
            not isinstance(levels, list)
            or len(levels) != slot_count
            or any(level not in LEVELS for level in levels)
        ):
            raise VariantBankError("candidate_levels_invalid_or_v1_forbidden")
        target_material = {
            "formal_node_id": formal_node_id,
            "formal_node_sha256": formal_node_sha,
            "main_knowledge": main_knowledge,
            "style_profile_id": profile_id,
            "style_fallback": style_fallback,
            "verified_canonical_anchor_ids": anchors,
            "deep_style_spec": deep_style_spec,
            "deep_style_spec_sha256": _digest(deep_style_spec),
            "exam_style_profile_axis_constraints": profile_constraints,
            "exam_style_profile_axis_constraints_sha256": _digest(profile_constraints),
        }
        slots = []
        for ordinal, level in enumerate(levels, 1):
            slot_material = {**target_material, "ordinal": ordinal, "variant_level": level}
            slots.append(
                {
                    "slot_id": "EVS-" + _digest(slot_material)[:24].upper(),
                    "ordinal": ordinal,
                    "variant_level": level,
                    "status": "awaiting_author",
                }
            )
        planned_targets.append({**target_material, "candidate_slots": slots})
        seen_nodes.add(formal_node_id)
    material = {
        "schema": REQUEST_SCHEMA,
        "bank_schema": BANK_SCHEMA,
        "exam_style_release": {
            "release_id": release["release_id"],
            "content_sha256": release["content_sha256"],
            "manifest_sha256": _sha256_file(release["release_dir"] / "manifest.json"),
            "canonical_exposure_sha256": release["manifest"]["artifacts"]["canonical-exposure.jsonl"]["sha256"],
            "style_profiles_sha256": release["manifest"]["artifacts"]["style-profiles.jsonl"]["sha256"],
        },
        "generation_mode": "offline_external_submissions_only",
        "model_call_count": 0,
        "formal_write_count": 0,
        "targets": planned_targets,
    }
    request_id = "EVR-" + _digest(material)[:24].upper()
    request = {**material, "request_id": request_id}
    path, digest = _content_write(bank / "requests", request)
    return {
        "status": "planned",
        "request_id": request_id,
        "request_ref": _relative(path, details),
        "request_sha256": digest,
        "target_count": len(planned_targets),
        "candidate_slot_count": sum(len(row["candidate_slots"]) for row in planned_targets),
        "model_call_count": 0,
        "formal_write_count": 0,
    }


def _request_context(
    details: Path, release_dir: str | Path, request_ref: str | Path
) -> tuple[dict[str, Any], Path, str, dict[str, Any]]:
    request, path, digest = _content_ref(details, request_ref, "plan_request")
    if request.get("schema") != REQUEST_SCHEMA:
        raise VariantBankError("plan_request_schema_invalid")
    release = _release_snapshot(release_dir)
    binding = request.get("exam_style_release") or {}
    if (
        binding.get("release_id") != release["release_id"]
        or binding.get("content_sha256") != release["content_sha256"]
        or binding.get("manifest_sha256") != _sha256_file(release["release_dir"] / "manifest.json")
    ):
        raise VariantBankError("plan_request_release_stale")
    return request, path, digest, release


def _slot(request: dict[str, Any], slot_id: object) -> tuple[dict[str, Any], dict[str, Any]]:
    matches: list[tuple[dict[str, Any], dict[str, Any]]] = []
    for target in request.get("targets") or []:
        if not isinstance(target, dict):
            continue
        for slot in target.get("candidate_slots") or []:
            if isinstance(slot, dict) and slot.get("slot_id") == slot_id:
                matches.append((target, slot))
    if len(matches) != 1:
        raise VariantBankError("candidate_slot_not_unique")
    return matches[0]


def _axes(candidate: dict[str, Any], level: str) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    axes = candidate.get("transformation_axes")
    edges = candidate.get("changed_structural_edges")
    if not isinstance(axes, list) or not isinstance(edges, list):
        raise VariantBankError("variant_transformation_metadata_invalid")
    normalized_axes: list[dict[str, Any]] = []
    for axis in axes:
        if not isinstance(axis, dict):
            raise VariantBankError("variant_axis_invalid")
        name = _normalize(axis.get("axis"))
        kind = str(axis.get("kind") or "")
        before = _normalize(axis.get("before"))
        after = _normalize(axis.get("after"))
        if not name or kind not in {"numeric", "non_numeric"} or not before or not after or before == after:
            raise VariantBankError("variant_axis_invalid")
        normalized_axes.append({"axis": name, "kind": kind, "before": before, "after": after})
    if len({row["axis"] for row in normalized_axes}) != len(normalized_axes):
        raise VariantBankError("variant_axis_duplicate")
    normalized_edges: list[dict[str, Any]] = []
    for edge in edges:
        if not isinstance(edge, dict):
            raise VariantBankError("changed_structural_edge_invalid")
        source = _normalize(edge.get("from"))
        target = _normalize(edge.get("to"))
        axis = _normalize(edge.get("axis"))
        relation = _normalize(edge.get("relation"))
        if not source or not target or not axis or not relation or source == target:
            raise VariantBankError("changed_structural_edge_invalid")
        normalized_edges.append(
            {"from": source, "to": target, "axis": axis, "relation": relation}
        )
    if level == "V2":
        if len(normalized_axes) < 2 or not any(row["kind"] == "non_numeric" for row in normalized_axes):
            raise VariantBankError("v2_requires_two_axes_with_non_numeric")
        if normalized_edges:
            raise VariantBankError("v2_changed_structural_edge_must_be_empty")
    elif level == "V3":
        if len(normalized_edges) != 1:
            raise VariantBankError("v3_requires_exactly_one_changed_structural_edge")
    else:
        raise VariantBankError("v1_forbidden")
    return normalized_axes, normalized_edges


def _candidate_value(
    request: dict[str, Any], request_sha: str, submission: dict[str, Any]
) -> tuple[dict[str, Any], dict[str, Any], dict[str, Any], str]:
    if submission.get("schema") != AUTHOR_SCHEMA or submission.get("request_sha256") != request_sha:
        raise VariantBankError("author_submission_binding_invalid")
    author_id = str(submission.get("author_id") or "")
    if not SAFE_ID_RE.fullmatch(author_id):
        raise VariantBankError("author_identity_invalid")
    target, slot = _slot(request, submission.get("slot_id"))
    candidate = submission.get("candidate")
    if not isinstance(candidate, dict):
        raise VariantBankError("author_candidate_missing")
    level = str(candidate.get("variant_level") or "")
    anchors = candidate.get("canonical_anchor_ids")
    if (
        level != slot.get("variant_level")
        or level not in LEVELS
        or candidate.get("formal_node_id") != target.get("formal_node_id")
        or candidate.get("formal_node_sha256") != target.get("formal_node_sha256")
        or candidate.get("main_knowledge") != target.get("main_knowledge")
        or candidate.get("style_profile_id") != target.get("style_profile_id")
        or candidate.get("deep_style_spec_sha256") != target.get("deep_style_spec_sha256")
        or not isinstance(anchors, list)
        or not anchors
        or not set(anchors).issubset(set(target.get("verified_canonical_anchor_ids") or []))
    ):
        raise VariantBankError("author_candidate_target_drift")
    axes, edges = _axes(candidate, level)
    mutable_axes = set(
        target["deep_style_spec"][
            "v2_mutable_axes" if level == "V2" else "v3_mutable_axes"
        ]
    )
    invariant_axes = set(
        target["deep_style_spec"][
            "v2_invariant_axes" if level == "V2" else "v3_invariant_axes"
        ]
    )
    if any(row["axis"] not in mutable_axes for row in axes) or any(
        row["axis"] in invariant_axes for row in axes
    ):
        raise VariantBankError("variant_axes_violate_deep_style_spec")
    if level == "V3":
        edge = edges[0]
        allowed_edges = target["exam_style_profile_axis_constraints"][
            "v3_allowed_single_structural_edges"
        ]
        if edge["axis"] not in mutable_axes or not any(
            row["mutable_axis"] == edge["axis"]
            and row["edge"] == edge["relation"]
            for row in allowed_edges
        ):
            raise VariantBankError("v3_changed_structural_edge_not_allowed_by_profile")
    stem = str(candidate.get("stem") or "").strip()
    options = candidate.get("options")
    if not stem or ANSWER_LEAK_RE.search(stem):
        raise VariantBankError("question_stem_missing_or_leaks_answer")
    if not isinstance(options, list) or len(options) != 4:
        raise VariantBankError("question_requires_four_options")
    option_rows: list[dict[str, str]] = []
    for row in options:
        if not isinstance(row, dict):
            raise VariantBankError("question_option_invalid")
        option_id = str(row.get("option_id") or "")
        text = str(row.get("text") or "").strip()
        if not OPTION_ID_RE.fullmatch(option_id) or not text or ANSWER_LEAK_RE.search(text):
            raise VariantBankError("question_option_invalid_or_leaks_answer")
        option_rows.append({"option_id": option_id, "text": text})
    option_ids = [row["option_id"] for row in option_rows]
    if len(set(option_ids)) != 4 or len({_normalize(row["text"]) for row in option_rows}) != 4:
        raise VariantBankError("question_options_not_unique")
    correct = str(candidate.get("correct_option_id") or "")
    if option_ids.count(correct) != 1:
        raise VariantBankError("question_unique_key_invalid")
    distractors = candidate.get("distractors")
    if not isinstance(distractors, list) or len(distractors) != 3:
        raise VariantBankError("distractor_metadata_incomplete")
    distractor_rows: list[dict[str, str]] = []
    allowed_distractor_mechanisms = set(target["deep_style_spec"]["distractor_mechanisms"])
    for row in distractors:
        if not isinstance(row, dict):
            raise VariantBankError("distractor_metadata_invalid")
        option_id = str(row.get("option_id") or "")
        misconception = _normalize(row.get("misconception"))
        refutation = _normalize(row.get("refutation"))
        mechanism = _normalize(row.get("mechanism"))
        if (
            option_id == correct
            or option_id not in option_ids
            or not misconception
            or not refutation
            or mechanism not in allowed_distractor_mechanisms
        ):
            raise VariantBankError("distractor_metadata_invalid")
        distractor_rows.append(
            {
                "option_id": option_id,
                "mechanism": mechanism,
                "misconception": misconception,
                "refutation": refutation,
            }
        )
    if {row["option_id"] for row in distractor_rows} != set(option_ids) - {correct}:
        raise VariantBankError("distractor_metadata_not_exact")
    commitments = candidate.get("source_commitments")
    if not isinstance(commitments, list) or not commitments:
        raise VariantBankError("fact_source_commitment_missing")
    commitment_rows: list[dict[str, str]] = []
    release_sha = str((request.get("exam_style_release") or {}).get("content_sha256") or "")
    for row in commitments:
        if not isinstance(row, dict):
            raise VariantBankError("fact_source_commitment_invalid")
        fact_id = str(row.get("fact_id") or "")
        source_kind = str(row.get("source_kind") or "")
        source_id = str(row.get("source_id") or "")
        source_sha = str(row.get("source_sha256") or "")
        if not SAFE_ID_RE.fullmatch(fact_id) or source_kind not in {"canonical_anchor", "authoritative_reference"} or not source_id or not SHA_RE.fullmatch(source_sha):
            raise VariantBankError("fact_source_commitment_invalid")
        if source_kind == "canonical_anchor" and (source_id not in anchors or source_sha != release_sha):
            raise VariantBankError("canonical_fact_source_unverified")
        commitment_rows.append(
            {"fact_id": fact_id, "source_kind": source_kind, "source_id": source_id, "source_sha256": source_sha}
        )
    if len({row["fact_id"] for row in commitment_rows}) != len(commitment_rows):
        raise VariantBankError("fact_source_commitment_duplicate")
    explanation = str(candidate.get("explanation") or "").strip()
    if not explanation:
        raise VariantBankError("authoritative_explanation_missing")
    normalized = {
        "variant_level": level,
        "formal_node_id": target["formal_node_id"],
        "formal_node_sha256": target["formal_node_sha256"],
        "main_knowledge": target["main_knowledge"],
        "style_profile_id": target["style_profile_id"],
        "deep_style_spec": target["deep_style_spec"],
        "deep_style_spec_sha256": target["deep_style_spec_sha256"],
        "canonical_anchor_ids": anchors,
        "transformation_axes": axes,
        "changed_structural_edges": edges,
        "source_commitments": commitment_rows,
        "stem": stem,
        "options": option_rows,
        "correct_option_id": correct,
        "distractors": distractor_rows,
        "explanation": explanation,
    }
    candidate_id = "EVC-" + _digest(
        {"request_sha256": request_sha, "slot_id": slot["slot_id"], "candidate": normalized}
    )[:24].upper()
    return target, slot, normalized, candidate_id


def _permutation_commitment(option_ids: Iterable[str], correct: str) -> str:
    rows = []
    for order in itertools.permutations(option_ids):
        rows.append(
            {
                "order": list(order),
                "correct_label": LABELS[order.index(correct)],
                "correct_option_id": correct,
            }
        )
    return _digest({"schema": "exam_variant_permutation_check_v1", "rows": rows})


def submit_author(
    details_root: str | Path,
    release_dir: str | Path,
    request_ref: str | Path,
    submission: dict[str, Any],
) -> dict[str, Any]:
    details, bank = _bank_root(details_root)
    request, _, request_sha, _ = _request_context(details, release_dir, request_ref)
    target, slot, candidate, candidate_id = _candidate_value(request, request_sha, submission)
    author = {
        "schema": AUTHOR_SCHEMA,
        "candidate_id": candidate_id,
        "request_ref": str(request_ref),
        "request_sha256": request_sha,
        "slot_id": slot["slot_id"],
        "author_id": str(submission["author_id"]),
        "candidate": candidate,
        "permutation_count": 24,
        "permutation_commitment_sha256": _permutation_commitment(
            [row["option_id"] for row in candidate["options"]], candidate["correct_option_id"]
        ),
        "model_call_count": 0,
        "formal_write_count": 0,
    }
    author_path, author_sha = _content_write(bank / "work" / candidate_id / "author", author)
    blind_input = {
        "schema": BLIND_INPUT_SCHEMA,
        "candidate_id": candidate_id,
        "request_sha256": request_sha,
        "author_submission_sha256": author_sha,
        "slot_id": slot["slot_id"],
        "variant_level": candidate["variant_level"],
        "formal_node_id": target["formal_node_id"],
        "main_knowledge": target["main_knowledge"],
        "style_profile_id": target["style_profile_id"],
        "deep_style_spec": target["deep_style_spec"],
        "deep_style_spec_sha256": target["deep_style_spec_sha256"],
        "canonical_anchor_ids": candidate["canonical_anchor_ids"],
        "transformation_axes": candidate["transformation_axes"],
        "changed_structural_edges": candidate["changed_structural_edges"],
        "source_commitments": candidate["source_commitments"],
        "stem": candidate["stem"],
        "options": candidate["options"],
        "answer_fields_included": False,
    }
    blind_path, blind_sha = _content_write(bank / "work" / candidate_id / "blind-input", blind_input)
    return {
        "status": "author_submitted",
        "candidate_id": candidate_id,
        "author_ref": _relative(author_path, details),
        "author_sha256": author_sha,
        "blind_input_ref": _relative(blind_path, details),
        "blind_input_sha256": blind_sha,
        "answer_fields_in_blind_input": False,
        "model_call_count": 0,
        "formal_write_count": 0,
    }


def _all_checks(value: object, required: set[str], label: str) -> dict[str, bool]:
    if not isinstance(value, dict) or set(value) != required or any(item is not True for item in value.values()):
        raise VariantBankError(f"{label}_checks_incomplete")
    return {key: True for key in sorted(required)}


def _blind_semantic_review(
    submission: dict[str, Any], option_ids: list[str]
) -> dict[str, Any]:
    open_answer = submission.get("open_answer")
    if not isinstance(open_answer, dict):
        raise VariantBankError("blind_open_answer_missing")
    derived = str(open_answer.get("derived_option_id") or "")
    reasoning = _normalize(open_answer.get("reasoning"))
    if derived not in option_ids or not reasoning:
        raise VariantBankError("blind_open_answer_invalid")
    refutations = submission.get("option_refutations")
    if not isinstance(refutations, list) or len(refutations) != 4:
        raise VariantBankError("blind_option_refutations_incomplete")
    normalized_refutations: list[dict[str, str]] = []
    for row in refutations:
        if not isinstance(row, dict):
            raise VariantBankError("blind_option_refutation_invalid")
        option_id = str(row.get("option_id") or "")
        judgment = str(row.get("judgment") or "")
        analysis = _normalize(row.get("analysis"))
        if option_id not in option_ids or judgment not in {"correct", "incorrect"} or not analysis:
            raise VariantBankError("blind_option_refutation_invalid")
        normalized_refutations.append(
            {"option_id": option_id, "judgment": judgment, "analysis": analysis}
        )
    if (
        {row["option_id"] for row in normalized_refutations} != set(option_ids)
        or [row["option_id"] for row in normalized_refutations if row["judgment"] == "correct"] != [derived]
    ):
        raise VariantBankError("blind_option_refutations_do_not_prove_unique_key")
    second = submission.get("second_correct_option_search")
    if (
        not isinstance(second, dict)
        or second.get("found") is not False
        or second.get("candidate_option_id") is not None
        or not _normalize(second.get("analysis"))
    ):
        raise VariantBankError("blind_second_correct_option_not_cleared")
    trials = submission.get("permutation_trials")
    if not isinstance(trials, list) or len(trials) != 4:
        raise VariantBankError("blind_requires_four_permutation_trials")
    normalized_trials: list[dict[str, Any]] = []
    seen_orders: set[tuple[str, ...]] = set()
    for row in trials:
        if not isinstance(row, dict):
            raise VariantBankError("blind_permutation_trial_invalid")
        order = row.get("order")
        trial_derived = str(row.get("derived_option_id") or "")
        trial_reasoning = _normalize(row.get("reasoning"))
        if (
            not isinstance(order, list)
            or len(order) != 4
            or set(order) != set(option_ids)
            or len(set(order)) != 4
            or trial_derived != derived
            or not trial_reasoning
            or tuple(order) in seen_orders
        ):
            raise VariantBankError("blind_permutation_trial_invalid")
        seen_orders.add(tuple(order))
        normalized_trials.append(
            {"order": order, "derived_option_id": trial_derived, "reasoning": trial_reasoning}
        )
    boundaries = submission.get("boundary_checks")
    if not isinstance(boundaries, dict) or set(boundaries) != BLIND_BOUNDARY_FIELDS:
        raise VariantBankError("blind_boundary_checks_incomplete")
    normalized_boundaries: dict[str, dict[str, str]] = {}
    for name in sorted(BLIND_BOUNDARY_FIELDS):
        row = boundaries[name]
        if not isinstance(row, dict):
            raise VariantBankError("blind_boundary_check_invalid")
        status = str(row.get("status") or "")
        analysis = _normalize(row.get("analysis"))
        if status not in {"pass", "not_applicable"} or not analysis:
            raise VariantBankError("blind_boundary_check_invalid")
        normalized_boundaries[name] = {"status": status, "analysis": analysis}
    syllabus = submission.get("out_of_syllabus_dependency")
    clues = submission.get("clue_scan")
    for value, label in ((syllabus, "out_of_syllabus_dependency"), (clues, "clue_scan")):
        if (
            not isinstance(value, dict)
            or value.get("found") is not False
            or not _normalize(value.get("analysis"))
        ):
            raise VariantBankError(f"blind_{label}_not_cleared")
    return {
        "open_answer": {"derived_option_id": derived, "reasoning": reasoning},
        "option_refutations": normalized_refutations,
        "second_correct_option_search": {
            "found": False,
            "candidate_option_id": None,
            "analysis": _normalize(second["analysis"]),
        },
        "permutation_trials": normalized_trials,
        "boundary_checks": normalized_boundaries,
        "out_of_syllabus_dependency": {
            "found": False,
            "analysis": _normalize(syllabus["analysis"]),
        },
        "clue_scan": {"found": False, "analysis": _normalize(clues["analysis"])},
    }


def _author_context(
    details: Path,
    release_dir: str | Path,
    request_ref: str | Path,
    author_ref: str | Path,
) -> tuple[dict[str, Any], str, dict[str, Any], str]:
    request, _, request_sha, _ = _request_context(details, release_dir, request_ref)
    author, _, author_sha = _content_ref(details, author_ref, "author_submission")
    if (
        author.get("schema") != AUTHOR_SCHEMA
        or author.get("request_sha256") != request_sha
        or author.get("candidate_id") is None
    ):
        raise VariantBankError("author_submission_binding_drift")
    _, _, normalized, expected_id = _candidate_value(
        request,
        request_sha,
        {
            "schema": AUTHOR_SCHEMA,
            "request_sha256": request_sha,
            "slot_id": author.get("slot_id"),
            "author_id": author.get("author_id"),
            "candidate": author.get("candidate"),
        },
    )
    if author.get("candidate_id") != expected_id or author.get("candidate") != normalized:
        raise VariantBankError("author_candidate_content_drift")
    return request, request_sha, author, author_sha


def submit_blind_verifier(
    details_root: str | Path,
    release_dir: str | Path,
    request_ref: str | Path,
    author_ref: str | Path,
    blind_input_ref: str | Path,
    submission: dict[str, Any],
) -> dict[str, Any]:
    details, bank = _bank_root(details_root)
    _, request_sha, author, author_sha = _author_context(details, release_dir, request_ref, author_ref)
    blind_input, _, blind_input_sha = _content_ref(details, blind_input_ref, "blind_input")
    candidate_id = str(author["candidate_id"])
    if (
        blind_input.get("schema") != BLIND_INPUT_SCHEMA
        or blind_input.get("candidate_id") != candidate_id
        or blind_input.get("request_sha256") != request_sha
        or blind_input.get("author_submission_sha256") != author_sha
        or blind_input.get("answer_fields_included") is not False
        or any(key in blind_input for key in ("correct_option_id", "distractors", "explanation"))
    ):
        raise VariantBankError("blind_input_not_answer_isolated")
    verifier_id = str(submission.get("verifier_id") or "")
    option_ids = [row["option_id"] for row in (author.get("candidate") or {}).get("options", [])]
    semantic_review = _blind_semantic_review(submission, option_ids)
    derived = semantic_review["open_answer"]["derived_option_id"]
    if (
        submission.get("schema") != BLIND_SCHEMA
        or submission.get("candidate_id") != candidate_id
        or submission.get("blind_input_sha256") != blind_input_sha
        or submission.get("verdict") != "accept"
        or not SAFE_ID_RE.fullmatch(verifier_id)
        or verifier_id == author.get("author_id")
        or derived != (author.get("candidate") or {}).get("correct_option_id")
    ):
        raise VariantBankError("blind_verifier_rejected_or_not_independent")
    receipt = {
        "schema": BLIND_SCHEMA,
        "candidate_id": candidate_id,
        "request_sha256": request_sha,
        "author_submission_sha256": author_sha,
        "blind_input_sha256": blind_input_sha,
        "verifier_id": verifier_id,
        "verdict": "accept",
        "derived_correct_option_id": derived,
        "semantic_review": semantic_review,
        "checks": _all_checks(submission.get("checks"), BLIND_CHECKS, "blind_verifier"),
        "blind_input_answer_fields_included": False,
        "model_call_count": 0,
        "formal_write_count": 0,
    }
    path, digest = _content_write(bank / "work" / candidate_id / "blind-verifier", receipt)
    return {
        "status": "blind_verified",
        "candidate_id": candidate_id,
        "blind_verifier_ref": _relative(path, details),
        "blind_verifier_sha256": digest,
        "model_call_count": 0,
        "formal_write_count": 0,
    }


def submit_adversarial_verifier(
    details_root: str | Path,
    release_dir: str | Path,
    request_ref: str | Path,
    author_ref: str | Path,
    blind_verifier_ref: str | Path,
    submission: dict[str, Any],
) -> dict[str, Any]:
    details, bank = _bank_root(details_root)
    _, request_sha, author, author_sha = _author_context(details, release_dir, request_ref, author_ref)
    blind, _, blind_sha = _content_ref(details, blind_verifier_ref, "blind_verifier")
    candidate_id = str(author["candidate_id"])
    if (
        blind.get("schema") != BLIND_SCHEMA
        or blind.get("candidate_id") != candidate_id
        or blind.get("author_submission_sha256") != author_sha
        or blind.get("verdict") != "accept"
    ):
        raise VariantBankError("blind_verifier_binding_drift")
    verifier_id = str(submission.get("verifier_id") or "")
    if (
        submission.get("schema") != ADVERSARIAL_SCHEMA
        or submission.get("candidate_id") != candidate_id
        or submission.get("author_submission_sha256") != author_sha
        or submission.get("blind_verifier_sha256") != blind_sha
        or submission.get("verdict") != "accept"
        or not SAFE_ID_RE.fullmatch(verifier_id)
        or verifier_id in {author.get("author_id"), blind.get("verifier_id")}
        or submission.get("permutation_commitment_sha256") != author.get("permutation_commitment_sha256")
    ):
        raise VariantBankError("adversarial_verifier_rejected_or_not_independent")
    receipt = {
        "schema": ADVERSARIAL_SCHEMA,
        "candidate_id": candidate_id,
        "request_sha256": request_sha,
        "author_submission_sha256": author_sha,
        "blind_verifier_sha256": blind_sha,
        "verifier_id": verifier_id,
        "verdict": "accept",
        "checks": _all_checks(submission.get("checks"), ADVERSARIAL_CHECKS, "adversarial_verifier"),
        "permutation_count": 24,
        "permutation_commitment_sha256": author["permutation_commitment_sha256"],
        "model_call_count": 0,
        "formal_write_count": 0,
    }
    path, digest = _content_write(bank / "work" / candidate_id / "adversarial-verifier", receipt)
    return {
        "status": "adversarial_verified",
        "candidate_id": candidate_id,
        "adversarial_verifier_ref": _relative(path, details),
        "adversarial_verifier_sha256": digest,
        "model_call_count": 0,
        "formal_write_count": 0,
    }


def _web_target_material(
    release: dict[str, Any],
    submission: dict[str, Any],
    *,
    expected_formal_node_id: str,
    expected_formal_node_sha256: str,
    expected_target_knowledge: str,
    expected_due_version: str,
    expected_concept_priority_version: str | None,
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Rebuild the frozen author target without creating a local prebuild plan."""

    if submission.get("schema") != WEB_SUBMISSION_SCHEMA:
        raise VariantBankError("web_variant_submission_schema_invalid")
    web_variant_id = str(submission.get("web_variant_id") or "")
    if not SAFE_ID_RE.fullmatch(web_variant_id):
        raise VariantBankError("web_variant_identity_invalid")
    if (
        submission.get("question_id") != expected_formal_node_id
        or submission.get("formal_node_sha256")
        not in (None, expected_formal_node_sha256)
        or submission.get("target_concept_id") != expected_target_knowledge
        or submission.get("due_version") != expected_due_version
        or submission.get("concept_priority_version")
        != expected_concept_priority_version
    ):
        raise VariantBankError("web_variant_native_snapshot_binding_drift")
    binding = submission.get("exam_style_release")
    expected_release_binding = {
        "release_id": release["release_id"],
        "content_sha256": release["content_sha256"],
        "manifest_sha256": _sha256_file(release["release_dir"] / "manifest.json"),
    }
    if binding is not None and (
        not isinstance(binding, dict)
        or any(
            binding.get(key) != value
            for key, value in expected_release_binding.items()
            if key in binding
        )
        or not set(binding).issubset(expected_release_binding)
    ):
        raise VariantBankError("web_variant_release_binding_drift")
    profile_id = str(submission.get("style_profile_id") or "")
    profile = release["profile_by_id"].get(profile_id)
    if profile is None:
        raise VariantBankError("web_variant_style_profile_invalid")
    deep_style_spec = _deep_style_spec(submission.get("deep_style_spec"), web_profile=profile,
                                       variant_level=str(submission.get("variant_level") or ""))
    style_fallback = _style_fallback(
        submission.get("style_fallback"),
        target_main_knowledge=expected_target_knowledge,
        profile=profile,
    )
    profile_constraints = _profile_axis_constraints(profile)
    if not set(profile_constraints["v2_invariant_axes"]).issubset(
        set(deep_style_spec["v2_invariant_axes"])
    ):
        raise VariantBankError("web_variant_v2_invariants_do_not_preserve_profile")
    if not set(deep_style_spec["v2_mutable_axes"]).issubset(
        set(profile_constraints["v2_mutable_surface_axes"])
    ):
        raise VariantBankError("web_variant_v2_mutable_axes_exceed_profile")
    profile_v3_axes = {
        row["mutable_axis"]
        for row in profile_constraints["v3_allowed_single_structural_edges"]
    }
    if not set(deep_style_spec["v3_mutable_axes"]).issubset(profile_v3_axes):
        raise VariantBankError("web_variant_v3_mutable_axes_exceed_profile")
    commitments = submission.get("canonical_anchor_commitments")
    if not isinstance(commitments, list) or not commitments:
        raise VariantBankError("web_variant_anchor_commitments_missing")
    anchors: list[str] = []
    normalized_commitments: list[dict[str, str]] = []
    for row in commitments:
        if not isinstance(row, dict):
            raise VariantBankError("web_variant_anchor_commitment_invalid")
        canonical_id = str(row.get("canonical_id") or "")
        exposure = release["exposure_by_id"].get(canonical_id)
        if (
            exposure is None
            or canonical_id not in set(profile.get("canonical_ids") or [])
            or exposure.get("production_usable") is not True
            or row.get("canonical_exposure_sha256")
            not in (None, _digest(exposure))
            or row.get("release_content_sha256")
            not in (None, release["content_sha256"])
        ):
            raise VariantBankError("web_variant_anchor_commitment_drift")
        anchors.append(canonical_id)
        normalized_commitments.append(
            {
                "canonical_id": canonical_id,
                "canonical_exposure_sha256": _digest(exposure),
                "release_content_sha256": release["content_sha256"],
            }
        )
    if len(set(anchors)) != len(anchors):
        raise VariantBankError("web_variant_anchor_identity_duplicate")
    level = str(submission.get("variant_level") or "")
    if level not in LEVELS:
        raise VariantBankError("web_variant_v1_forbidden")
    target = {
        "formal_node_id": expected_formal_node_id,
        "formal_node_sha256": expected_formal_node_sha256,
        "main_knowledge": expected_target_knowledge,
        "style_profile_id": profile_id,
        "style_fallback": style_fallback,
        "verified_canonical_anchor_ids": anchors,
        "deep_style_spec": deep_style_spec,
        "deep_style_spec_sha256": _digest(deep_style_spec),
        "exam_style_profile_axis_constraints": profile_constraints,
        "exam_style_profile_axis_constraints_sha256": _digest(profile_constraints),
    }
    slot_material = {**target, "ordinal": 1, "variant_level": level}
    slot = {
        "slot_id": "EVS-" + _digest(slot_material)[:24].upper(),
        "ordinal": 1,
        "variant_level": level,
        "status": "awaiting_web_author_self_check",
    }
    return {**target, "candidate_slots": [slot]}, {
        "web_variant_id": web_variant_id,
        "canonical_anchor_commitments": normalized_commitments,
        "exam_style_release": expected_release_binding,
    }


def _prepare_web_self_checked_variant(
    output_root: str | Path,
    release: dict[str, Any],
    submission: dict[str, Any],
    *,
    expected_formal_node_id: str,
    expected_formal_node_sha256: str,
    expected_target_knowledge: str,
    expected_due_version: str,
    expected_concept_priority_version: str | None,
) -> dict[str, Any]:
    target, web_identity = _web_target_material(
        release,
        submission,
        expected_formal_node_id=expected_formal_node_id,
        expected_formal_node_sha256=expected_formal_node_sha256,
        expected_target_knowledge=expected_target_knowledge,
        expected_due_version=expected_due_version,
        expected_concept_priority_version=expected_concept_priority_version,
    )
    private = submission.get("private_material")
    public = submission.get("public_surface")
    if not isinstance(private, dict) or not isinstance(public, dict):
        raise VariantBankError("web_variant_surface_or_private_material_missing")
    grading, _, grading_source_sha = _web_output_json(
        output_root, private.get("grading_path"), private.get("grading_sha256"), "web_grading"
    )
    solution, _, solution_source_sha = _web_output_json(
        output_root, private.get("solution_path"), private.get("solution_sha256"), "web_solution"
    )
    self_check, _, self_check_source_sha = _web_output_json(
        output_root, private.get("self_check_path"), private.get("self_check_sha256"), "web_self_check"
    )
    web_variant_id = web_identity["web_variant_id"]
    if (
        grading.get("schema") != WEB_GRADING_SCHEMA
        or solution.get("schema") != WEB_SOLUTION_SCHEMA
        or self_check.get("schema") != WEB_SELF_CHECK_SCHEMA
        or any(row.get("web_variant_id") != web_variant_id for row in (grading, solution, self_check))
    ):
        raise VariantBankError("web_variant_private_material_binding_drift")
    actor_id = str(submission.get("web_actor_id") or "")
    if not SAFE_ID_RE.fullmatch(actor_id):
        raise VariantBankError("web_variant_actor_identity_invalid")
    if any(row.get("web_actor_id") != actor_id for row in (grading, solution, self_check)):
        raise VariantBankError("web_variant_same_actor_binding_drift")
    if (
        self_check.get("self_check_kind") != "same_actor_solution_and_counterexample"
        or self_check.get("verdict") != "accept"
        or self_check.get("independent_reviewed") is not False
        or self_check.get("independent_actor_ids") != []
        or _all_checks(self_check.get("checks"), WEB_SELF_CHECKS, "web_self_check")
        != {key: True for key in sorted(WEB_SELF_CHECKS)}
    ):
        raise VariantBankError("web_variant_self_check_contract_invalid")
    raw_source_commitments = solution.get("source_commitments")
    if not isinstance(raw_source_commitments, list):
        raise VariantBankError("web_variant_source_commitments_missing")
    source_commitments: list[object] = []
    for raw_commitment in raw_source_commitments:
        if not isinstance(raw_commitment, dict):
            source_commitments.append(raw_commitment)
            continue
        commitment = dict(raw_commitment)
        if (
            commitment.get("source_kind") == "canonical_anchor"
            and commitment.get("source_id") in target["verified_canonical_anchor_ids"]
            and commitment.get("source_sha256") is None
        ):
            commitment["source_sha256"] = release["content_sha256"]
        source_commitments.append(commitment)
    candidate = {
        "variant_level": submission.get("variant_level"),
        "formal_node_id": expected_formal_node_id,
        "formal_node_sha256": expected_formal_node_sha256,
        "main_knowledge": expected_target_knowledge,
        "style_profile_id": submission.get("style_profile_id"),
        "deep_style_spec_sha256": target["deep_style_spec_sha256"],
        "canonical_anchor_ids": target["verified_canonical_anchor_ids"],
        "transformation_axes": solution.get("transformation_axes"),
        "changed_structural_edges": solution.get("changed_structural_edges"),
        "source_commitments": source_commitments,
        "stem": public.get("stem"),
        "options": public.get("options"),
        "correct_option_id": grading.get("correct_option_id"),
        "distractors": grading.get("distractors"),
        "explanation": solution.get("explanation"),
    }
    request = {
        "schema": REQUEST_SCHEMA,
        "bank_schema": BANK_SCHEMA,
        "exam_style_release": web_identity["exam_style_release"],
        "generation_mode": "web_same_actor_solution_and_counterexample_self_check",
        "model_call_count": 0,
        "formal_write_count": 0,
        "targets": [target],
    }
    request_id = "EVR-WEB-" + _digest(request)[:20].upper()
    request = {**request, "request_id": request_id}
    request_sha = _digest(request)
    _, _, normalized_candidate, candidate_id = _candidate_value(
        request,
        request_sha,
        {
            "schema": AUTHOR_SCHEMA,
            "request_sha256": request_sha,
            "slot_id": target["candidate_slots"][0]["slot_id"],
            "author_id": actor_id,
            "candidate": candidate,
        },
    )
    option_ids = [row["option_id"] for row in normalized_candidate["options"]]
    option_checks = self_check.get("option_checks")
    if not isinstance(option_checks, list) or len(option_checks) != 4:
        raise VariantBankError("web_variant_option_checks_incomplete")
    normalized_option_checks: list[dict[str, str]] = []
    for row in option_checks:
        if not isinstance(row, dict) or set(row) != {
            "option_id", "judgment", "counterexample_or_proof"
        }:
            raise VariantBankError("web_variant_option_check_invalid")
        option_id = str(row.get("option_id") or "")
        judgment = str(row.get("judgment") or "")
        evidence = _normalize(row.get("counterexample_or_proof"))
        expected_judgment = (
            "correct" if option_id == normalized_candidate["correct_option_id"] else "incorrect"
        )
        if option_id not in option_ids or judgment != expected_judgment or not evidence:
            raise VariantBankError("web_variant_option_check_invalid")
        normalized_option_checks.append(
            {"option_id": option_id, "judgment": judgment, "counterexample_or_proof": evidence}
        )
    if {row["option_id"] for row in normalized_option_checks} != set(option_ids):
        raise VariantBankError("web_variant_option_check_identity_mismatch")
    second_best = self_check.get("second_best_search")
    if (
        not isinstance(second_best, dict)
        or second_best.get("found") is not False
        or not _normalize(second_best.get("analysis"))
        or self_check.get("derived_option_id") != normalized_candidate["correct_option_id"]
        or not _normalize(self_check.get("reasoning"))
    ):
        raise VariantBankError("web_variant_unique_answer_self_check_invalid")
    permutation_sha = _permutation_commitment(
        option_ids, normalized_candidate["correct_option_id"]
    )
    normalized_self_check = {
        "schema": WEB_SELF_CHECK_SCHEMA,
        "web_variant_id": web_variant_id,
        "web_actor_id": actor_id,
        "self_check_kind": "same_actor_solution_and_counterexample",
        "verdict": "accept",
        "derived_option_id": normalized_candidate["correct_option_id"],
        "reasoning": _normalize(self_check.get("reasoning")),
        "option_checks": sorted(normalized_option_checks, key=lambda row: row["option_id"]),
        "second_best_search": {"found": False, "analysis": _normalize(second_best.get("analysis"))},
        "checks": {key: True for key in sorted(WEB_SELF_CHECKS)},
        "independent_reviewed": False,
        "independent_actor_ids": [],
        "permutation_count": 24,
        "permutation_commitment_sha256": permutation_sha,
    }
    normalized_grading = {
        "schema": WEB_GRADING_SCHEMA,
        "web_variant_id": web_variant_id,
        "web_actor_id": actor_id,
        "correct_option_id": normalized_candidate["correct_option_id"],
        "distractors": normalized_candidate["distractors"],
    }
    normalized_solution = {
        "schema": WEB_SOLUTION_SCHEMA,
        "web_variant_id": web_variant_id,
        "web_actor_id": actor_id,
        "explanation": normalized_candidate["explanation"],
        "source_commitments": normalized_candidate["source_commitments"],
        "transformation_axes": normalized_candidate["transformation_axes"],
        "changed_structural_edges": normalized_candidate["changed_structural_edges"],
    }
    evidence = {
        "schema": WEB_SUBMISSION_SCHEMA,
        "web_variant_id": web_variant_id,
        "web_actor_id": actor_id,
        "acceptance_kind": WEB_ACCEPTANCE_KIND,
        "question_id": expected_formal_node_id,
        "formal_node_sha256": expected_formal_node_sha256,
        "target_concept_id": expected_target_knowledge,
        "due_version": expected_due_version,
        "concept_priority_version": expected_concept_priority_version,
        "exam_style_release": web_identity["exam_style_release"],
        "style_profile_id": target["style_profile_id"],
        "style_fallback": target["style_fallback"],
        "deep_style_spec": target["deep_style_spec"],
        **({"provided_deep_style_spec": submission.get("deep_style_spec")}
           if submission.get("deep_style_spec") != target["deep_style_spec"] else {}),
        "canonical_anchor_commitments": web_identity["canonical_anchor_commitments"],
        "variant_level": normalized_candidate["variant_level"],
        "candidate": normalized_candidate,
        "source_private_material_sha256": {
            "grading": grading_source_sha,
            "solution": solution_source_sha,
            "self_check": self_check_source_sha,
        },
        "independent_reviewed": False,
        "independent_actor_count": 0,
    }
    return {
        "web_variant_id": web_variant_id,
        "actor_id": actor_id,
        "candidate_id": candidate_id,
        "candidate": normalized_candidate,
        "request_id": request_id,
        "request_sha256": request_sha,
        "evidence": evidence,
        "grading": normalized_grading,
        "solution": normalized_solution,
        "self_check": normalized_self_check,
        "permutation_commitment_sha256": permutation_sha,
    }


def import_web_self_checked_variants(
    details_root: str | Path,
    release_dir: str | Path,
    output_root: str | Path,
    submissions: list[dict[str, Any]],
    *,
    expected_targets: dict[str, dict[str, Any]],
    expected_due_version: str,
    expected_concept_priority_version: str | None,
) -> dict[str, Any]:
    """Deterministically admit same-actor web variants into the existing bank."""

    if not isinstance(submissions, list):
        raise VariantBankError("web_variants_array_required")
    details, bank = _bank_root(details_root)
    release = _release_snapshot(release_dir)
    prepared: list[dict[str, Any]] = []
    seen_web_ids: set[str] = set()
    for submission in submissions:
        if not isinstance(submission, dict):
            raise VariantBankError("web_variant_submission_invalid")
        key = str(submission.get("web_variant_id") or "")
        target = expected_targets.get(key)
        if target is None or key in seen_web_ids:
            raise VariantBankError("web_variant_target_or_identity_duplicate")
        seen_web_ids.add(key)
        prepared.append(
            _prepare_web_self_checked_variant(
                output_root,
                release,
                submission,
                expected_formal_node_id=str(target["formal_node_id"]),
                expected_formal_node_sha256=str(target["formal_node_sha256"]),
                expected_target_knowledge=str(target["target_knowledge"]),
                expected_due_version=expected_due_version,
                expected_concept_priority_version=expected_concept_priority_version,
            )
        )
    existing = _rebuild_index(details, bank)
    existing_by_web: dict[str, dict[str, Any]] = {}
    surface_keys: dict[tuple[str, str, str], str] = {}
    for row in existing["entries"]:
        manifest = _load_json(_managed_ref(details, row["manifest_ref"], "published_manifest"), "published_manifest")
        web_id = manifest.get("web_variant_id")
        if isinstance(web_id, str):
            existing_by_web[web_id] = row
        surface_keys[(row["formal_node_id"], row["main_knowledge"], row["surface_sha256"])] = row["variant_id"]
    plans: list[dict[str, Any]] = []
    batch_surface_keys: set[tuple[str, str, str]] = set()
    for row in prepared:
        candidate = row["candidate"]
        surface = {
            "schema": SURFACE_SCHEMA,
            "candidate_id": row["candidate_id"],
            "variant_level": candidate["variant_level"],
            "formal_node_id": candidate["formal_node_id"],
            "main_knowledge": candidate["main_knowledge"],
            "style_profile_id": candidate["style_profile_id"],
            "canonical_anchor_ids": candidate["canonical_anchor_ids"],
            "stem": candidate["stem"],
            "options": candidate["options"],
            "acceptance_kind": WEB_ACCEPTANCE_KIND,
            "answer_content_in_surface": False,
        }
        evidence_sha = _sha256_bytes(_pretty_bytes(row["evidence"]))
        grading_sha = _sha256_bytes(_pretty_bytes(row["grading"]))
        solution_sha = _sha256_bytes(_pretty_bytes(row["solution"]))
        self_check_sha = _sha256_bytes(_pretty_bytes(row["self_check"]))
        answer = {
            "schema": ANSWER_SCHEMA,
            "candidate_id": row["candidate_id"],
            "correct_option_id": candidate["correct_option_id"],
            "distractors": candidate["distractors"],
            "explanation": candidate["explanation"],
            "source_commitments": candidate["source_commitments"],
            "transformation_axes": candidate["transformation_axes"],
            "changed_structural_edges": candidate["changed_structural_edges"],
            "acceptance_kind": WEB_ACCEPTANCE_KIND,
            "web_submission_sha256": evidence_sha,
            "web_grading_sha256": grading_sha,
            "web_solution_sha256": solution_sha,
            "web_self_check_sha256": self_check_sha,
            "permutation_commitment_sha256": row["permutation_commitment_sha256"],
            "independent_reviewed": False,
            "independent_actor_count": 0,
        }
        surface_sha = _sha256_bytes(_pretty_bytes(surface))
        answer_sha = _sha256_bytes(_pretty_bytes(answer))
        variant_id = "EVB-" + _digest(
            {
                "candidate_id": row["candidate_id"],
                "surface_sha256": surface_sha,
                "answer_capsule_sha256": answer_sha,
                "web_submission_sha256": evidence_sha,
            }
        )[:24].upper()
        surface_key = (candidate["formal_node_id"], candidate["main_knowledge"], surface_sha)
        existing_row = existing_by_web.get(row["web_variant_id"])
        if existing_row is not None:
            if existing_row["variant_id"] != variant_id:
                raise VariantBankError("web_variant_identity_reused_with_changed_content")
            plans.append({"status": "already_imported", "existing": existing_row, **row})
            continue
        if surface_key in surface_keys or surface_key in batch_surface_keys:
            raise VariantBankError("web_variant_surface_duplicate")
        batch_surface_keys.add(surface_key)
        plans.append(
            {
                **row,
                "status": "ready_to_import",
                "surface": surface,
                "surface_sha256": surface_sha,
                "answer": answer,
                "answer_sha256": answer_sha,
                "variant_id": variant_id,
            }
        )
    results: list[dict[str, Any]] = []
    for row in plans:
        if row["status"] == "already_imported":
            existing_row = row["existing"]
            results.append(
                {
                    "status": "already_imported",
                    "web_variant_id": row["web_variant_id"],
                    "variant_id": existing_row["variant_id"],
                    "surface_sha256": existing_row["surface_sha256"],
                    "answer_capsule_sha256": existing_row["answer_capsule_sha256"],
                    "manifest_ref": existing_row["manifest_ref"],
                    "manifest_sha256": existing_row["manifest_sha256"],
                    "acceptance_kind": WEB_ACCEPTANCE_KIND,
                }
            )
            continue
        evidence_path, evidence_sha = _content_write(bank / "published" / "objects" / "web-evidence", row["evidence"])
        grading_path, grading_sha = _content_write(bank / "published" / "objects" / "web-evidence", row["grading"])
        solution_path, solution_sha = _content_write(bank / "published" / "objects" / "web-evidence", row["solution"])
        self_check_path, self_check_sha = _content_write(bank / "published" / "objects" / "web-evidence", row["self_check"])
        surface_path, surface_sha = _content_write(bank / "published" / "objects" / "surfaces", row["surface"])
        answer_path, answer_sha = _content_write(bank / "published" / "objects" / "answers", row["answer"])
        candidate = row["candidate"]
        manifest = {
            "schema": MANIFEST_SCHEMA,
            "variant_id": row["variant_id"],
            "candidate_id": row["candidate_id"],
            "web_variant_id": row["web_variant_id"],
            "variant_level": candidate["variant_level"],
            "formal_node_id": candidate["formal_node_id"],
            "formal_node_sha256": candidate["formal_node_sha256"],
            "main_knowledge": candidate["main_knowledge"],
            "style_profile_id": candidate["style_profile_id"],
            "canonical_anchor_ids": candidate["canonical_anchor_ids"],
            "exam_style_release_id": release["release_id"],
            "exam_style_release_content_sha256": release["content_sha256"],
            "web_submission_ref": _relative(evidence_path, details),
            "web_submission_sha256": evidence_sha,
            "web_grading_ref": _relative(grading_path, details),
            "web_grading_sha256": grading_sha,
            "web_solution_ref": _relative(solution_path, details),
            "web_solution_sha256": solution_sha,
            "web_self_check_ref": _relative(self_check_path, details),
            "web_self_check_sha256": self_check_sha,
            "surface_ref": _relative(surface_path, details),
            "surface_sha256": surface_sha,
            "answer_capsule_ref": _relative(answer_path, details),
            "answer_capsule_sha256": answer_sha,
            "answer_isolated": True,
            "permutation_count": 24,
            "permutation_commitment_sha256": row["permutation_commitment_sha256"],
            "acceptance_kind": WEB_ACCEPTANCE_KIND,
            "status": WEB_ACCEPTANCE_KIND,
            "independent_reviewed": False,
            "independent_actor_count": 0,
            "formal_node_created": False,
            "formal_write_count": 0,
        }
        manifest_path, manifest_sha = _content_write(bank / "published" / "objects" / "manifests", manifest)
        results.append(
            {
                "status": "imported",
                "web_variant_id": row["web_variant_id"],
                "variant_id": row["variant_id"],
                "surface_sha256": surface_sha,
                "answer_capsule_sha256": answer_sha,
                "manifest_ref": _relative(manifest_path, details),
                "manifest_sha256": manifest_sha,
                "acceptance_kind": WEB_ACCEPTANCE_KIND,
            }
        )
    index = _rebuild_index(details, bank)
    return {
        "status": "imported" if any(row["status"] == "imported" for row in results) else "already_imported",
        "acceptance_kind": WEB_ACCEPTANCE_KIND,
        "items": results,
        "index_content_sha256": index["content_sha256"],
        "formal_write_count": 0,
        "model_call_count": 0,
    }


def _published_objects(
    author: dict[str, Any], author_sha: str, blind_sha: str, adversarial_sha: str
) -> tuple[dict[str, Any], dict[str, Any]]:
    candidate = author["candidate"]
    surface = {
        "schema": SURFACE_SCHEMA,
        "candidate_id": author["candidate_id"],
        "variant_level": candidate["variant_level"],
        "formal_node_id": candidate["formal_node_id"],
        "main_knowledge": candidate["main_knowledge"],
        "style_profile_id": candidate["style_profile_id"],
        "canonical_anchor_ids": candidate["canonical_anchor_ids"],
        "stem": candidate["stem"],
        "options": candidate["options"],
        "answer_content_in_surface": False,
    }
    answer = {
        "schema": ANSWER_SCHEMA,
        "candidate_id": author["candidate_id"],
        "correct_option_id": candidate["correct_option_id"],
        "distractors": candidate["distractors"],
        "explanation": candidate["explanation"],
        "source_commitments": candidate["source_commitments"],
        "transformation_axes": candidate["transformation_axes"],
        "changed_structural_edges": candidate["changed_structural_edges"],
        "author_submission_sha256": author_sha,
        "blind_verifier_sha256": blind_sha,
        "adversarial_verifier_sha256": adversarial_sha,
        "permutation_commitment_sha256": author["permutation_commitment_sha256"],
    }
    return surface, answer


def _rebuild_index(
    details: Path,
    bank: Path,
    *,
    persist: bool = True,
) -> dict[str, Any]:
    manifests_dir = bank / "published" / "objects" / "manifests"
    entries: list[dict[str, Any]] = []
    if manifests_dir.exists():
        for path in sorted(manifests_dir.glob("*.json"), key=lambda item: item.name):
            if path.is_symlink() or _sha256_file(path) != path.stem:
                raise VariantBankError("published_manifest_content_address_drift")
            manifest = _load_json(path, "published_manifest")
            if manifest.get("schema") != MANIFEST_SCHEMA:
                raise VariantBankError("published_manifest_schema_invalid")
            acceptance_kind = str(
                manifest.get("acceptance_kind") or INDEPENDENT_ACCEPTANCE_KIND
            )
            if acceptance_kind not in {
                INDEPENDENT_ACCEPTANCE_KIND,
                WEB_ACCEPTANCE_KIND,
            } or manifest.get("status") != acceptance_kind:
                raise VariantBankError("published_manifest_acceptance_kind_invalid")
            entries.append(
                {
                    "variant_id": manifest["variant_id"],
                    "variant_level": manifest["variant_level"],
                    "formal_node_id": manifest["formal_node_id"],
                    "formal_node_sha256": manifest["formal_node_sha256"],
                    "main_knowledge": manifest["main_knowledge"],
                    "style_profile_id": manifest["style_profile_id"],
                    "canonical_anchor_ids": manifest["canonical_anchor_ids"],
                    "exam_style_release_id": manifest["exam_style_release_id"],
                    "exam_style_release_content_sha256": manifest["exam_style_release_content_sha256"],
                    "surface_ref": manifest["surface_ref"],
                    "surface_sha256": manifest["surface_sha256"],
                    "answer_capsule_ref": manifest["answer_capsule_ref"],
                    "answer_capsule_sha256": manifest["answer_capsule_sha256"],
                    "manifest_ref": _relative(path, details),
                    "manifest_sha256": path.stem,
                    "status": acceptance_kind,
                    "acceptance_kind": acceptance_kind,
                    "web_variant_id": manifest.get("web_variant_id"),
                    "independent_reviewed": acceptance_kind
                    == INDEPENDENT_ACCEPTANCE_KIND,
                    "formal_node_created": False,
                }
            )
    entries.sort(key=lambda row: row["variant_id"])
    if len({row["variant_id"] for row in entries}) != len(entries):
        raise VariantBankError("published_variant_id_duplicate")
    material = {
        "schema": INDEX_SCHEMA,
        "bank_schema": BANK_SCHEMA,
        "entry_count": len(entries),
        "entries": entries,
        "formal_write_count": 0,
        "generated_content_creates_formal_node": False,
    }
    index = {**material, "content_sha256": _digest(material)}
    if persist:
        _atomic_write(bank / "index.json", _pretty_bytes(index))
    return index


def pool_status(
    details_root: str | Path,
    release_dir: str | Path,
    *,
    persist_index: bool = True,
    require_final_production_release: bool = True,
) -> dict[str, Any]:
    details, bank = _bank_root(details_root)
    release = _release_snapshot(
        release_dir,
        require_final_production_release=require_final_production_release,
    )
    index = _rebuild_index(details, bank, persist=persist_index)
    consumed: dict[str, dict[str, Any]] = {}
    consumption_root = bank / "consumption"
    if consumption_root.exists():
        for path in sorted(consumption_root.glob("*.json")):
            if path.is_symlink() or _sha256_file(path) != path.stem:
                raise VariantBankError("consumption_receipt_content_address_drift")
            receipt = _load_json(path, "consumption_receipt")
            if receipt.get("schema") != CONSUMPTION_SCHEMA:
                raise VariantBankError("consumption_receipt_schema_invalid")
            variant_id = str(receipt.get("variant_id") or "")
            if variant_id in consumed and consumed[variant_id] != receipt:
                raise VariantBankError("variant_consumed_more_than_once")
            consumed[variant_id] = receipt
    batch_root = bank / "consumption-batches"
    if batch_root.exists():
        for path in sorted(batch_root.glob("*.json")):
            if path.is_symlink() or _sha256_file(path) != path.stem:
                raise VariantBankError("consumption_batch_content_address_drift")
            receipt = _load_json(path, "consumption_batch_receipt")
            items = receipt.get("items")
            if receipt.get("schema") != CONSUMPTION_BATCH_SCHEMA or not isinstance(items, list):
                raise VariantBankError("consumption_batch_schema_invalid")
            for item in items:
                if not isinstance(item, dict):
                    raise VariantBankError("consumption_batch_item_invalid")
                variant_id = str(item.get("variant_id") or "")
                previous = consumed.get(variant_id)
                marker = {
                    "batch_receipt_sha256": path.stem,
                    "batch_receipt_ref": _relative(path, details),
                    "campaign_commitment_id": receipt.get("campaign_commitment_id"),
                    "selection_manifest_sha256": receipt.get("selection_manifest_sha256"),
                    "campaign_commitment_sha256": receipt.get("campaign_commitment_sha256"),
                    "consumption_request": item,
                }
                if previous is not None and previous != marker:
                    if not (
                        previous.get("campaign_commitment_id")
                        == marker.get("campaign_commitment_id")
                        and _same_campaign_variant_reservation(
                            previous.get("consumption_request"),
                            marker.get("consumption_request"),
                        )
                    ):
                        raise VariantBankError("variant_consumed_more_than_once")
                else:
                    consumed[variant_id] = marker
    entries: list[dict[str, Any]] = []
    published_candidates: set[str] = set()
    for row in index["entries"]:
        manifest = _load_json(_managed_ref(details, row["manifest_ref"], "published_manifest"), "published_manifest")
        published_candidates.add(str(manifest.get("candidate_id")))
        status = "verified"
        reason = None
        if (
            row.get("exam_style_release_id") != release["release_id"]
            or row.get("exam_style_release_content_sha256") != release["content_sha256"]
        ):
            status, reason = "expired", "exam_style_release_drift"
        else:
            try:
                surface = _managed_ref(details, row["surface_ref"], "published_surface")
                answer = _managed_ref(details, row["answer_capsule_ref"], "answer_capsule")
                if _sha256_file(surface) != row["surface_sha256"] or _sha256_file(answer) != row["answer_capsule_sha256"]:
                    raise VariantBankError("published_object_hash_drift")
            except VariantBankError as exc:
                status, reason = "quarantine", str(exc)
        if row["variant_id"] in consumed and status == "verified":
            status, reason = "consumed", "campaign_commitment_bound"
        consumption = consumed.get(row["variant_id"], {})
        entries.append(
            {
                **row,
                "pool_status": status,
                "status_reason": reason,
                "consumption_campaign_commitment_id": consumption.get(
                    "campaign_commitment_id"
                ),
                "consumption_campaign_commitment_sha256": consumption.get(
                    "campaign_commitment_sha256"
                ),
                "consumption_batch_ref": consumption.get("batch_receipt_ref"),
                "consumption_batch_sha256": consumption.get("batch_receipt_sha256"),
                "consumption_request": consumption.get("consumption_request"),
            }
        )
    work_root = bank / "work"
    if work_root.exists():
        for path in sorted(work_root.glob("*/author/*.json")):
            if path.is_symlink() or _sha256_file(path) != path.stem:
                continue
            author = _load_json(path, "draft_author")
            if str(author.get("candidate_id")) not in published_candidates:
                entries.append(
                    {
                        "candidate_id": author.get("candidate_id"),
                        "formal_node_id": (author.get("candidate") or {}).get("formal_node_id"),
                        "variant_level": (author.get("candidate") or {}).get("variant_level"),
                        "pool_status": "draft",
                        "status_reason": "awaiting_verification_or_publish",
                    }
                )
    counts = {name: 0 for name in ("draft", "verified", "quarantine", "consumed", "expired")}
    for row in entries:
        counts[row["pool_status"]] += 1
    material = {
        "schema": POOL_STATUS_SCHEMA,
        "exam_style_release_id": release["release_id"],
        "counts": counts,
        "entries": entries,
        "formal_write_count": 0,
    }
    return {**material, "content_sha256": _digest(material)}


def forecast_fill(
    details_root: str | Path,
    release_dir: str | Path,
    due_set: dict[str, Any],
) -> dict[str, Any]:
    if due_set.get("schema") != FORECAST_SCHEMA or not isinstance(due_set.get("items"), list):
        raise VariantBankError("forecast_due_set_invalid")
    as_of = _iso_date(due_set.get("as_of"), "forecast_as_of")
    final_day = as_of + dt.timedelta(days=6)
    pool = pool_status(details_root, release_dir)
    verified_by_node: dict[str, int] = {}
    for row in pool["entries"]:
        if row.get("pool_status") == "verified":
            node = str(row.get("formal_node_id") or "")
            verified_by_node[node] = verified_by_node.get(node, 0) + 1
    gaps: list[dict[str, Any]] = []
    for row in due_set["items"]:
        if not isinstance(row, dict):
            raise VariantBankError("forecast_due_item_invalid")
        due_date = _iso_date(row.get("due_date"), "forecast_due_date")
        if not as_of <= due_date <= final_day:
            continue
        required = int(row.get("required_verified_surfaces", 1))
        node = str(row.get("formal_node_id") or "")
        if verified_by_node.get(node, 0) < required:
            gaps.append(
                {
                    key: row[key]
                    for key in (
                        "formal_node_id",
                        "formal_node_sha256",
                        "main_knowledge",
                        "style_profile_id",
                        "verified_canonical_anchor_ids",
                        "deep_style_spec",
                    )
                }
                | {"candidate_slots": min(5, max(3, required - verified_by_node.get(node, 0) + 2))}
            )
    if not gaps:
        return {
            "status": "forecast_sufficient",
            "as_of": as_of.isoformat(),
            "through": final_day.isoformat(),
            "gap_count": 0,
            "formal_write_count": 0,
        }
    planned = plan_request(details_root, release_dir, gaps)
    return {
        "status": "forecast_gap_planned",
        "as_of": as_of.isoformat(),
        "through": final_day.isoformat(),
        "gap_count": len(gaps),
        **planned,
    }


def publish(
    details_root: str | Path,
    release_dir: str | Path,
    request_ref: str | Path,
    author_ref: str | Path,
    blind_verifier_ref: str | Path,
    adversarial_verifier_ref: str | Path,
) -> dict[str, Any]:
    details, bank = _bank_root(details_root)
    request, request_sha, author, author_sha = _author_context(details, release_dir, request_ref, author_ref)
    blind, _, blind_sha = _content_ref(details, blind_verifier_ref, "blind_verifier")
    adversarial, _, adversarial_sha = _content_ref(details, adversarial_verifier_ref, "adversarial_verifier")
    candidate_id = str(author["candidate_id"])
    if (
        blind.get("schema") != BLIND_SCHEMA
        or blind.get("candidate_id") != candidate_id
        or blind.get("author_submission_sha256") != author_sha
        or blind.get("verdict") != "accept"
        or adversarial.get("schema") != ADVERSARIAL_SCHEMA
        or adversarial.get("candidate_id") != candidate_id
        or adversarial.get("author_submission_sha256") != author_sha
        or adversarial.get("blind_verifier_sha256") != blind_sha
        or adversarial.get("verdict") != "accept"
        or len({author.get("author_id"), blind.get("verifier_id"), adversarial.get("verifier_id")}) != 3
    ):
        raise VariantBankError("publication_verifier_chain_invalid")
    candidate = author["candidate"]
    _axes(candidate, candidate["variant_level"])
    surface, answer = _published_objects(author, author_sha, blind_sha, adversarial_sha)
    surface_path, surface_sha = _content_write(bank / "published" / "objects" / "surfaces", surface)
    answer_path, answer_sha = _content_write(bank / "published" / "objects" / "answers", answer)
    variant_id = "EVB-" + _digest(
        {
            "candidate_id": candidate_id,
            "surface_sha256": surface_sha,
            "answer_capsule_sha256": answer_sha,
            "request_sha256": request_sha,
        }
    )[:24].upper()
    release_binding = request["exam_style_release"]
    manifest = {
        "schema": MANIFEST_SCHEMA,
        "variant_id": variant_id,
        "candidate_id": candidate_id,
        "variant_level": candidate["variant_level"],
        "formal_node_id": candidate["formal_node_id"],
        "formal_node_sha256": candidate["formal_node_sha256"],
        "main_knowledge": candidate["main_knowledge"],
        "style_profile_id": candidate["style_profile_id"],
        "canonical_anchor_ids": candidate["canonical_anchor_ids"],
        "exam_style_release_id": release_binding["release_id"],
        "exam_style_release_content_sha256": release_binding["content_sha256"],
        "request_ref": str(request_ref),
        "request_sha256": request_sha,
        "author_ref": str(author_ref),
        "author_sha256": author_sha,
        "blind_verifier_ref": str(blind_verifier_ref),
        "blind_verifier_sha256": blind_sha,
        "adversarial_verifier_ref": str(adversarial_verifier_ref),
        "adversarial_verifier_sha256": adversarial_sha,
        "surface_ref": _relative(surface_path, details),
        "surface_sha256": surface_sha,
        "answer_capsule_ref": _relative(answer_path, details),
        "answer_capsule_sha256": answer_sha,
        "answer_isolated": True,
        "permutation_count": 24,
        "permutation_commitment_sha256": author["permutation_commitment_sha256"],
        "acceptance_kind": INDEPENDENT_ACCEPTANCE_KIND,
        "status": INDEPENDENT_ACCEPTANCE_KIND,
        "independent_reviewed": True,
        "independent_actor_count": 2,
        "formal_node_created": False,
        "formal_write_count": 0,
    }
    manifest_path, manifest_sha = _content_write(
        bank / "published" / "objects" / "manifests", manifest
    )
    index = _rebuild_index(details, bank)
    return {
        "status": "published",
        "variant_id": variant_id,
        "manifest_ref": _relative(manifest_path, details),
        "manifest_sha256": manifest_sha,
        "surface_sha256": surface_sha,
        "answer_capsule_sha256": answer_sha,
        "answer_isolated": True,
        "index_content_sha256": index["content_sha256"],
        "formal_node_created": False,
        "formal_write_count": 0,
        "model_call_count": 0,
    }


def audit(details_root: str | Path, release_dir: str | Path) -> dict[str, Any]:
    """Validate each entry according to its explicit acceptance kind."""

    details, bank = _bank_root(details_root)
    release = _release_snapshot(release_dir)
    index = _rebuild_index(details, bank)
    failures: list[str] = []
    for entry in index["entries"]:
        try:
            manifest, _, manifest_sha = _content_ref(details, entry["manifest_ref"], "published_manifest")
            surface, _, surface_sha = _content_ref(details, manifest["surface_ref"], "published_surface")
            answer, _, answer_sha = _content_ref(details, manifest["answer_capsule_ref"], "answer_capsule")
            acceptance_kind = str(
                manifest.get("acceptance_kind") or INDEPENDENT_ACCEPTANCE_KIND
            )
            if acceptance_kind == WEB_ACCEPTANCE_KIND:
                evidence, _, evidence_sha = _content_ref(
                    details, manifest["web_submission_ref"], "web_submission"
                )
                grading, _, grading_sha = _content_ref(
                    details, manifest["web_grading_ref"], "web_grading"
                )
                solution, _, solution_sha = _content_ref(
                    details, manifest["web_solution_ref"], "web_solution"
                )
                self_check, _, self_check_sha = _content_ref(
                    details, manifest["web_self_check_ref"], "web_self_check"
                )
                candidate = evidence.get("candidate")
                if (
                    manifest.get("schema") != MANIFEST_SCHEMA
                    or manifest_sha != entry["manifest_sha256"]
                    or manifest.get("variant_id") != entry["variant_id"]
                    or manifest.get("exam_style_release_id") != release["release_id"]
                    or manifest.get("exam_style_release_content_sha256") != release["content_sha256"]
                    or manifest.get("status") != WEB_ACCEPTANCE_KIND
                    or manifest.get("independent_reviewed") is not False
                    or manifest.get("independent_actor_count") != 0
                    or evidence.get("schema") != WEB_SUBMISSION_SCHEMA
                    or evidence.get("acceptance_kind") != WEB_ACCEPTANCE_KIND
                    or evidence_sha != manifest.get("web_submission_sha256")
                    or grading.get("schema") != WEB_GRADING_SCHEMA
                    or grading_sha != manifest.get("web_grading_sha256")
                    or solution.get("schema") != WEB_SOLUTION_SCHEMA
                    or solution_sha != manifest.get("web_solution_sha256")
                    or self_check.get("schema") != WEB_SELF_CHECK_SCHEMA
                    or self_check.get("self_check_kind")
                    != "same_actor_solution_and_counterexample"
                    or self_check.get("independent_reviewed") is not False
                    or self_check.get("independent_actor_ids") != []
                    or self_check_sha != manifest.get("web_self_check_sha256")
                    or surface.get("schema") != SURFACE_SCHEMA
                    or surface.get("acceptance_kind") != WEB_ACCEPTANCE_KIND
                    or surface.get("answer_content_in_surface") is not False
                    or surface_sha != manifest.get("surface_sha256")
                    or answer.get("schema") != ANSWER_SCHEMA
                    or answer.get("acceptance_kind") != WEB_ACCEPTANCE_KIND
                    or answer.get("independent_reviewed") is not False
                    or answer_sha != manifest.get("answer_capsule_sha256")
                    or not isinstance(candidate, dict)
                ):
                    raise VariantBankError("published_web_self_check_chain_mismatch")
                _axes(candidate, str(candidate.get("variant_level") or ""))
                continue
            request, request_sha, author, author_sha = _author_context(
                details, release_dir, manifest["request_ref"], manifest["author_ref"]
            )
            blind, _, blind_sha = _content_ref(details, manifest["blind_verifier_ref"], "blind_verifier")
            adversarial, _, adversarial_sha = _content_ref(details, manifest["adversarial_verifier_ref"], "adversarial_verifier")
            expected_surface, expected_answer = _published_objects(author, author_sha, blind_sha, adversarial_sha)
            if (
                manifest.get("schema") != MANIFEST_SCHEMA
                or manifest_sha != entry["manifest_sha256"]
                or manifest.get("variant_id") != entry["variant_id"]
                or manifest.get("exam_style_release_id") != release["release_id"]
                or manifest.get("exam_style_release_content_sha256") != release["content_sha256"]
                or manifest.get("request_sha256") != request_sha
                or surface.get("schema") != SURFACE_SCHEMA
                or surface.get("answer_content_in_surface") is not False
                or surface != expected_surface
                or surface_sha != manifest.get("surface_sha256")
                or answer.get("schema") != ANSWER_SCHEMA
                or answer != expected_answer
                or answer_sha != manifest.get("answer_capsule_sha256")
                or blind.get("verdict") != "accept"
                or adversarial.get("verdict") != "accept"
                or len({author.get("author_id"), blind.get("verifier_id"), adversarial.get("verifier_id")}) != 3
                or request.get("schema") != REQUEST_SCHEMA
                or acceptance_kind != INDEPENDENT_ACCEPTANCE_KIND
            ):
                raise VariantBankError("published_chain_mismatch")
            _axes(author["candidate"], author["candidate"]["variant_level"])
        except (KeyError, TypeError, VariantBankError) as exc:
            failures.append(f"{entry.get('variant_id')}:{exc}")
    return {
        "status": "PASS" if not failures else "FAIL",
        "bank_schema": BANK_SCHEMA,
        "exam_style_release_id": release["release_id"],
        "published_variant_count": index["entry_count"],
        "index_content_sha256": index["content_sha256"],
        "failures": failures,
        "formal_write_count": 0,
        "model_call_count": 0,
    }


def _recent_exact_ids(exposure_set: dict[str, Any], day: dt.date) -> dict[str, int]:
    rows = exposure_set.get("rows")
    if not isinstance(rows, list):
        raise VariantBankError("exposure_set_rows_invalid")
    result: dict[str, int] = {}
    for row in rows:
        if not isinstance(row, dict):
            raise VariantBankError("exposure_set_row_invalid")
        true_item_id = str(row.get("true_item_id") or "")
        if not true_item_id:
            continue
        if row.get("gap_days") is not None:
            try:
                gap = int(row["gap_days"])
            except (TypeError, ValueError) as exc:
                raise VariantBankError("exposure_gap_days_invalid") from exc
        else:
            raw_date = row.get("display_date", row.get("review_date"))
            gap = (day - _iso_date(raw_date, "exposure_display_date")).days
        if gap < 0:
            raise VariantBankError("future_exposure_invalid")
        if true_item_id not in result or gap < result[true_item_id]:
            result[true_item_id] = gap
    return result


def validate_campaign_commitments(
    commitment_set: dict[str, Any],
    *,
    expected_count: int,
    exam_style_release_content_sha256: str,
) -> dict[str, Any]:
    """All-or-nothing completeness gate for 30/40/100 prepared commitments."""

    rows = commitment_set.get("items")
    if (
        commitment_set.get("schema") != CAMPAIGN_COMMITMENT_SCHEMA
        or expected_count not in {30, 40, 100}
        or not SHA_RE.fullmatch(exam_style_release_content_sha256)
        or not isinstance(rows, list)
        or len(rows) != expected_count
    ):
        raise VariantBankError("campaign_commitment_count_incomplete")
    normalized: list[dict[str, Any]] = []
    for row in rows:
        if not isinstance(row, dict):
            raise VariantBankError("campaign_commitment_invalid")
        required = {
            "commitment_id",
            "true_item_id",
            "surface_sha256",
            "answer_capsule_sha256",
            "exam_style_release_content_sha256",
            "answer_isolated",
        }
        if (
            set(row) != required
            or not SAFE_ID_RE.fullmatch(str(row.get("commitment_id") or ""))
            or not SAFE_ID_RE.fullmatch(str(row.get("true_item_id") or ""))
            or not SHA_RE.fullmatch(str(row.get("surface_sha256") or ""))
            or not SHA_RE.fullmatch(str(row.get("answer_capsule_sha256") or ""))
            or row.get("exam_style_release_content_sha256") != exam_style_release_content_sha256
            or row.get("answer_isolated") is not True
        ):
            raise VariantBankError("campaign_commitment_invalid")
        normalized.append(row)
    if (
        len({row["commitment_id"] for row in normalized}) != expected_count
        or len({row["true_item_id"] for row in normalized}) != expected_count
    ):
        raise VariantBankError("campaign_commitment_identity_duplicate")
    material = {
        "schema": CAMPAIGN_COMMITMENT_SCHEMA,
        "expected_count": expected_count,
        "exam_style_release_content_sha256": exam_style_release_content_sha256,
        "items": normalized,
    }
    return {
        "status": "PASS",
        "commitment_count": expected_count,
        "content_sha256": _digest(material),
        "all_answer_isolated": True,
    }


def _learner_variant_surface(
    surface: dict[str, Any], variant_id: str, day: dt.date, seed: str
) -> tuple[dict[str, Any], str]:
    rows = surface.get("options")
    if not isinstance(rows, list) or len(rows) != 4:
        raise VariantBankError("published_surface_options_invalid")
    ordered = sorted(
        rows,
        key=lambda row: _digest(
            {"variant_id": variant_id, "review_date": day.isoformat(), "seed": seed, "option_id": row.get("option_id")}
        ),
    )
    learner = {
        "schema": "exam_variant_learner_surface_v1",
        "variant_id": variant_id,
        "stem": surface["stem"],
        "options": {label: ordered[index]["text"] for index, label in enumerate(LABELS)},
        "response_instruction": "请选择 A、B、C 或 D。",
        "answer_content_in_surface": False,
    }
    return learner, _digest([row["option_id"] for row in ordered])


def _verified_original_commitments(
    details: Path, release: dict[str, Any], exposure: dict[str, Any]
) -> dict[str, Any]:
    assets = exposure.get("question_asset_refs")
    if not isinstance(assets, list) or not assets:
        raise VariantBankError("original_question_asset_commitment_missing")
    verified_assets: list[dict[str, Any]] = []
    for row in assets:
        if not isinstance(row, dict):
            raise VariantBankError("original_question_asset_commitment_invalid")
        ref = str(row.get("ref") or "")
        expected_sha = str(row.get("sha256") or "")
        expected_size = row.get("size_bytes")
        path = _managed_ref(details, ref, "original_question_asset")
        if (
            not SHA_RE.fullmatch(expected_sha)
            or _sha256_file(path) != expected_sha
            or path.stat().st_size != expected_size
        ):
            raise VariantBankError("original_question_asset_commitment_drift")
        verified_assets.append(
            {
                "ref": ref,
                "sha256": expected_sha,
                "size_bytes": expected_size,
                "page_locator": row.get("page_locator"),
            }
        )
    capsule_ref = str(exposure.get("answer_capsule_commitment_ref") or "")
    capsule_sha = str(exposure.get("answer_capsule_commitment_sha256") or "")
    prefix = "private-sibling://answer-capsules/"
    if not capsule_ref.startswith(prefix) or not SHA_RE.fullmatch(capsule_sha):
        raise VariantBankError("original_answer_capsule_commitment_invalid")
    relative = Path(capsule_ref[len(prefix) :])
    if relative.is_absolute() or ".." in relative.parts or relative.name != f"{exposure['canonical_id']}.json":
        raise VariantBankError("original_answer_capsule_commitment_invalid")
    capsule_path = (
        release["release_dir"].parent.parent
        / "style-releases-private"
        / release["release_id"]
        / "answer-capsules"
        / relative
    )
    if capsule_path.is_symlink() or not capsule_path.is_file() or _sha256_file(capsule_path) != capsule_sha:
        raise VariantBankError("original_answer_capsule_commitment_drift")
    surface_material = {
        "schema": "canonical_original_surface_commitment_v1",
        "canonical_id": exposure["canonical_id"],
        "question_asset_refs": verified_assets,
        "exam_style_release_content_sha256": release["content_sha256"],
    }
    return {
        "question_asset_refs": verified_assets,
        "surface_commitment": surface_material,
        "surface_sha256": _digest(surface_material),
        "answer_capsule_commitment_ref": capsule_ref,
        "answer_capsule_commitment_sha256": capsule_sha,
        "answer_capsule_id": capsule_sha,
    }


def select_surface(
    details_root: str | Path,
    release_dir: str | Path,
    *,
    review_date: str,
    formal_node_id: str,
    formal_node_sha256: str,
    true_item_id: str,
    original_canonical_id: str,
    exposure_set: dict[str, Any],
    seed: str = "default",
    campaign_commitment_id: str | None = None,
    consume: bool = False,
    variant_only: bool = False,
    formal_node_main_knowledge: str | None = None,
    required_target_knowledge: str | None = None,
    preferred_variant_id: str | None = None,
) -> dict[str, Any]:
    """Resolve an original or verified V2/V3 surface with exact-item cooldown."""

    if required_target_knowledge is not None and (not variant_only or not KNOWLEDGE_RE.fullmatch(required_target_knowledge)):
        raise VariantBankError("point_target_requires_variant_only_and_canonical_knowledge")
    if preferred_variant_id is not None and not SAFE_ID_RE.fullmatch(preferred_variant_id):
        raise VariantBankError("preferred_variant_identity_invalid")

    details, bank = _bank_root(details_root)
    day = _iso_date(review_date, "review_date")
    if (
        not FORMAL_NODE_RE.fullmatch(formal_node_id)
        or not SHA_RE.fullmatch(formal_node_sha256)
        or not SAFE_ID_RE.fullmatch(true_item_id)
        or not CANONICAL_RE.fullmatch(original_canonical_id)
        or (
            variant_only
            and not KNOWLEDGE_RE.fullmatch(
                str(formal_node_main_knowledge or "")
            )
        )
    ):
        raise VariantBankError("surface_selection_identity_invalid")
    release = _release_snapshot(release_dir)
    exposure = release["exposure_by_id"].get(original_canonical_id)
    usable_style_anchor = bool(exposure and variant_only
        and exposure.get("exposure_mode") == "style_only"
        and exposure.get("production_usable") is True)
    if (
        exposure is None
        or (not usable_style_anchor and (
            exposure.get("exposure_mode") != "direct_pool"
            or exposure.get("direct_replay_allowed") is not True))
    ):
        return {
            "schema": SELECTION_SCHEMA,
            "status": "content_blocked",
            "reason": "original_not_verified_direct_pool",
            "formal_node_id": formal_node_id,
            "formal_write_count": 0,
        }
    original_available = (
        exposure.get("production_usable") is True
        and exposure.get("direct_surface_status", "ready") == "ready"
    )
    exact_gaps = _recent_exact_ids(exposure_set, day)
    original_gap = exact_gaps.get(true_item_id)
    if (
        not variant_only
        and preferred_variant_id is None
        and original_available
        and (original_gap is None or original_gap >= 7)
    ):
        original_commitments = _verified_original_commitments(details, release, exposure)
        material = {
            "schema": SELECTION_SCHEMA,
            "status": "selected",
            "surface_kind": "canonical_original",
            "review_date": day.isoformat(),
            "formal_node_id": formal_node_id,
            "formal_node_sha256": formal_node_sha256,
            "true_item_id": true_item_id,
            "canonical_id": original_canonical_id,
            "exam_style_release_id": release["release_id"],
            "exam_style_release_content_sha256": release["content_sha256"],
            "cooldown_gap_days": original_gap,
            **original_commitments,
            "answer_content_in_selection": False,
            "formal_node_created": False,
            "formal_write_count": 0,
        }
        return {**material, "selection_id": "EVSEL-" + _digest(material)[:24].upper()}
    pool = pool_status(details, release_dir)
    candidates = [
        row
        for row in pool["entries"]
        if (
            row.get("pool_status") == "verified"
            or (
                variant_only
                and row.get("pool_status") == "consumed"
                and row.get("consumption_campaign_commitment_id")
                == campaign_commitment_id
            )
        )
        and row.get("formal_node_id") == formal_node_id
        and (not (required_target_knowledge or formal_node_main_knowledge)
             or row.get("main_knowledge") == (required_target_knowledge or formal_node_main_knowledge))
        and (
            not variant_only
            or original_canonical_id in (row.get("canonical_anchor_ids") or [])
        )
        and (
            row.get("formal_node_sha256") == formal_node_sha256
            or (
                variant_only
                and row.get("main_knowledge") == formal_node_main_knowledge
            )
        )
        and row.get("variant_level") in LEVELS
        and row.get("exam_style_release_id") == release["release_id"]
        and row.get("exam_style_release_content_sha256") == release["content_sha256"]
        and exact_gaps.get(str(row.get("variant_id")), 7) >= 7
        and (
            preferred_variant_id is None
            or row.get("variant_id") == preferred_variant_id
        )
    ]
    candidates.sort(
        key=lambda row: _digest(
            {"review_date": day.isoformat(), "formal_node_id": formal_node_id, "seed": seed, "variant_id": row["variant_id"]}
        )
    )
    if not candidates:
        if not original_available:
            return {
                "schema": SELECTION_SCHEMA,
                "status": "ORIGINAL_ANSWER_CAPSULE_UNAVAILABLE",
                "reason": "verified_original_answer_capsule_missing",
                "formal_node_id": formal_node_id,
                "true_item_id": true_item_id,
                "canonical_id": original_canonical_id,
                "fallback_to_unverified_original": False,
                "formal_write_count": 0,
            }
        return {
            "schema": SELECTION_SCHEMA,
            "status": "VERIFIED_VARIANT_UNAVAILABLE",
            "reason": "recent_exact_original_without_verified_variant",
            "formal_node_id": formal_node_id,
            "true_item_id": true_item_id,
            "cooldown_gap_days": original_gap,
            "fallback_to_original": False,
            "formal_write_count": 0,
        }
    chosen = candidates[0]
    if consume:
        raise VariantBankError("direct_consumption_forbidden_use_commit_consumption")
    if campaign_commitment_id is None or not SAFE_ID_RE.fullmatch(campaign_commitment_id):
        raise VariantBankError("campaign_commitment_id_required_for_variant_consumption")
    surface, _, surface_sha = _content_ref(details, chosen["surface_ref"], "published_surface")
    if surface_sha != chosen["surface_sha256"] or surface.get("answer_content_in_surface") is not False:
        raise VariantBankError("selected_variant_surface_drift")
    learner_surface, permutation_sha = _learner_variant_surface(surface, chosen["variant_id"], day, seed)
    material = {
        "schema": SELECTION_SCHEMA,
        "status": "selected",
        "surface_kind": "verified_variant",
        "review_date": day.isoformat(),
        "formal_node_id": formal_node_id,
        "formal_node_sha256": formal_node_sha256,
        "authored_formal_node_sha256": chosen["formal_node_sha256"],
        "formal_node_rebound": chosen["formal_node_sha256"] != formal_node_sha256,
        "formal_node_rebind_basis": (
            "stable_formal_node_id_and_main_knowledge"
            if chosen["formal_node_sha256"] != formal_node_sha256
            else None
        ),
        "true_item_id": chosen["variant_id"],
        "blocked_original_true_item_id": true_item_id,
        "blocked_original_gap_days": original_gap,
        "blocked_original_reason": (
            "formal_node_has_no_direct_original_surface"
            if variant_only
            else (
                "verified_original_answer_capsule_missing"
                if not original_available
                else "recent_exact_original"
            )
        ),
        "variant_id": chosen["variant_id"],
        "variant_level": chosen["variant_level"],
        "acceptance_kind": chosen.get("acceptance_kind", INDEPENDENT_ACCEPTANCE_KIND),
        "web_variant_id": chosen.get("web_variant_id"),
        "variant_cooldown_gap_days": exact_gaps.get(chosen["variant_id"]),
        "surface_sha256": surface_sha,
        "answer_capsule_id": chosen["answer_capsule_sha256"],
        "permutation_sha256": permutation_sha,
        "learner_surface": learner_surface,
        "answer_content_in_selection": False,
        "fallback_to_original": False,
        "formal_node_created": False,
        "formal_write_count": 0,
    }
    selection_id = "EVSEL-" + _digest(material)[:24].upper()
    consumption_request = {
        "schema": CONSUMPTION_REQUEST_SCHEMA,
        "variant_id": chosen["variant_id"],
        "manifest_sha256": chosen["manifest_sha256"],
        "campaign_commitment_id": campaign_commitment_id,
        "selection_id": selection_id,
        "review_date": day.isoformat(),
        "surface_sha256": surface_sha,
        "answer_capsule_sha256": chosen["answer_capsule_sha256"],
        "exam_style_release_id": release["release_id"],
        "exam_style_release_content_sha256": release["content_sha256"],
        "answer_isolated": True,
        "formal_write_count": 0,
    }
    return {
        **material,
        "selection_id": selection_id,
        "campaign_commitment_id": campaign_commitment_id,
        "consume": False,
        "consumption_request": consumption_request,
        "consumption_request_sha256": _digest(consumption_request),
    }


def commit_consumption(
    details_root: str | Path,
    release_dir: str | Path,
    selection_manifest_ref: str | Path,
) -> dict[str, Any]:
    """Atomically consume every variant in one fully frozen selection manifest."""

    details, bank = _bank_root(details_root)
    release = _release_snapshot(release_dir)
    selection_manifest, selection_manifest_path, selection_manifest_sha = _content_ref(
        details, selection_manifest_ref, "selection_manifest"
    )
    rows = selection_manifest.get("selections")
    campaign_id = str(selection_manifest.get("campaign_commitment_id") or "")
    campaign_sha = str(selection_manifest.get("campaign_commitment_sha256") or "")
    if (
        selection_manifest.get("schema") != SELECTION_MANIFEST_SCHEMA
        or not SAFE_ID_RE.fullmatch(campaign_id)
        or not SHA_RE.fullmatch(campaign_sha)
        or not isinstance(rows, list)
        or not rows
        or selection_manifest.get("selection_count") != len(rows)
    ):
        raise VariantBankError("selection_manifest_invalid")
    requests: list[dict[str, Any]] = []
    selection_ids: set[str] = set()
    for row in rows:
        if not isinstance(row, dict) or row.get("status") != "selected":
            raise VariantBankError("selection_manifest_not_fully_ready")
        selection_id = str(row.get("selection_id") or "")
        if not SAFE_ID_RE.fullmatch(selection_id) or selection_id in selection_ids:
            raise VariantBankError("selection_manifest_identity_invalid")
        selection_ids.add(selection_id)
        if row.get("surface_kind") != "verified_variant":
            continue
        request = row.get("consumption_request")
        if (
            not isinstance(request, dict)
            or request.get("schema") != CONSUMPTION_REQUEST_SCHEMA
            or request.get("selection_id") != selection_id
            or request.get("campaign_commitment_id") != campaign_id
            or row.get("consumption_request_sha256") != _digest(request)
            or request.get("exam_style_release_id") != release["release_id"]
            or request.get("exam_style_release_content_sha256") != release["content_sha256"]
            or request.get("answer_isolated") is not True
        ):
            raise VariantBankError("selection_consumption_request_invalid")
        requests.append(request)
    if len({str(row.get("variant_id")) for row in requests}) != len(requests):
        raise VariantBankError("selection_manifest_variant_duplicate")
    receipt = {
        "schema": CONSUMPTION_BATCH_SCHEMA,
        "selection_manifest_ref": _relative(selection_manifest_path, details),
        "selection_manifest_sha256": selection_manifest_sha,
        "campaign_commitment_id": campaign_id,
        "campaign_commitment_sha256": campaign_sha,
        "exam_style_release_id": release["release_id"],
        "exam_style_release_content_sha256": release["content_sha256"],
        "selection_count": len(rows),
        "variant_consumption_count": len(requests),
        "items": requests,
        "formal_write_count": 0,
    }
    receipt_payload = _pretty_bytes(receipt)
    receipt_sha = _sha256_bytes(receipt_payload)
    receipt_path = bank / "consumption-batches" / f"{receipt_sha}.json"
    if receipt_path.exists():
        if receipt_path.is_symlink() or receipt_path.read_bytes() != receipt_payload:
            raise VariantBankError("consumption_batch_idempotency_conflict")
        return {
            "status": "already_committed",
            "selection_manifest_sha256": selection_manifest_sha,
            "campaign_commitment_sha256": campaign_sha,
            "consumption_batch_ref": _relative(receipt_path, details),
            "consumption_batch_sha256": receipt_sha,
            "variant_consumption_count": len(requests),
            "formal_write_count": 0,
        }
    pool = pool_status(details, release_dir)
    available = {
        row["variant_id"]: row
        for row in pool["entries"]
        if row.get("pool_status") in {"verified", "consumed"}
        and row.get("variant_id")
    }
    reusable: list[dict[str, Any]] = []
    fresh: list[dict[str, Any]] = []
    for request in requests:
        entry = available.get(str(request.get("variant_id")))
        if (
            entry is None
            or request.get("manifest_sha256") != entry.get("manifest_sha256")
            or request.get("surface_sha256") != entry.get("surface_sha256")
            or request.get("answer_capsule_sha256") != entry.get("answer_capsule_sha256")
        ):
            raise VariantBankError("selection_variant_not_verified_at_commit")
        if entry.get("pool_status") == "verified":
            fresh.append(entry)
        elif (
            entry.get("consumption_campaign_commitment_id") == campaign_id
            and _same_campaign_variant_reservation(
                entry.get("consumption_request"), request
            )
            and SHA_RE.fullmatch(str(entry.get("consumption_batch_sha256") or ""))
        ):
            reusable.append(entry)
        else:
            raise VariantBankError("selection_variant_consumption_conflict")
    if reusable and not fresh:
        if len(reusable) != len(requests):
            raise VariantBankError("reused_variant_batch_incomplete")
        prior_batches = sorted(
            {
                (
                    str(row["consumption_batch_ref"]),
                    str(row["consumption_batch_sha256"]),
                )
                for row in reusable
            }
        )
        rebind = {
            "schema": CONSUMPTION_REBIND_SCHEMA,
            "selection_manifest_ref": _relative(selection_manifest_path, details),
            "selection_manifest_sha256": selection_manifest_sha,
            "campaign_commitment_id": campaign_id,
            "campaign_commitment_sha256": campaign_sha,
            "prior_consumption_batches": [
                {"ref": ref, "sha256": digest} for ref, digest in prior_batches
            ],
            "variant_consumption_count": len(requests),
            "variant_ids": sorted(str(row["variant_id"]) for row in reusable),
            "reuse_reason": "same_campaign_prestart_preparation_rebuild",
            "formal_write_count": 0,
        }
        rebind_payload = _pretty_bytes(rebind)
        rebind_sha = _sha256_bytes(rebind_payload)
        rebind_path = bank / "consumption-batch-rebindings" / f"{rebind_sha}.json"
        repeated_rebind = rebind_path.exists()
        _write_immutable_json(rebind_path, rebind)
        return {
            "status": "already_rebound" if repeated_rebind else "rebound",
            "selection_manifest_sha256": selection_manifest_sha,
            "campaign_commitment_sha256": campaign_sha,
            "consumption_batch_ref": _relative(rebind_path, details),
            "consumption_batch_sha256": rebind_sha,
            "variant_consumption_count": len(requests),
            "formal_write_count": 0,
        }
    _write_immutable_json(receipt_path, receipt)
    return {
        "status": "committed",
        "selection_manifest_sha256": selection_manifest_sha,
        "campaign_commitment_sha256": campaign_sha,
        "consumption_batch_ref": _relative(receipt_path, details),
        "consumption_batch_sha256": receipt_sha,
        "variant_consumption_count": len(requests),
        "formal_write_count": 0,
    }


def _json_arg(path: str, label: str) -> dict[str, Any]:
    return _load_json(Path(path).expanduser().resolve(strict=True), label)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)

    plan = sub.add_parser("prebuild-plan")
    plan.add_argument("--details-root", required=True)
    plan.add_argument("--release", required=True)
    plan.add_argument("--targets", required=True)

    author = sub.add_parser("submit-author")
    author.add_argument("--details-root", required=True)
    author.add_argument("--release", required=True)
    author.add_argument("--request-ref", required=True)
    author.add_argument("--submission", required=True)

    blind = sub.add_parser("submit-blind-verifier")
    blind.add_argument("--details-root", required=True)
    blind.add_argument("--release", required=True)
    blind.add_argument("--request-ref", required=True)
    blind.add_argument("--author-ref", required=True)
    blind.add_argument("--blind-input-ref", required=True)
    blind.add_argument("--submission", required=True)

    adversarial = sub.add_parser("submit-adversarial-verifier")
    adversarial.add_argument("--details-root", required=True)
    adversarial.add_argument("--release", required=True)
    adversarial.add_argument("--request-ref", required=True)
    adversarial.add_argument("--author-ref", required=True)
    adversarial.add_argument("--blind-verifier-ref", required=True)
    adversarial.add_argument("--submission", required=True)

    publish_parser = sub.add_parser("publish")
    publish_parser.add_argument("--details-root", required=True)
    publish_parser.add_argument("--release", required=True)
    publish_parser.add_argument("--request-ref", required=True)
    publish_parser.add_argument("--author-ref", required=True)
    publish_parser.add_argument("--blind-verifier-ref", required=True)
    publish_parser.add_argument("--adversarial-verifier-ref", required=True)

    commit = sub.add_parser("commit-consumption")
    commit.add_argument("--details-root", required=True)
    commit.add_argument("--release", required=True)
    commit.add_argument("--selection-manifest-ref", required=True)

    audit_parser = sub.add_parser("audit")
    audit_parser.add_argument("--details-root", required=True)
    audit_parser.add_argument("--release", required=True)

    pool = sub.add_parser("pool-status")
    pool.add_argument("--details-root", required=True)
    pool.add_argument("--release", required=True)

    forecast = sub.add_parser("forecast-fill")
    forecast.add_argument("--details-root", required=True)
    forecast.add_argument("--release", required=True)
    forecast.add_argument("--due-set", required=True)

    select = sub.add_parser("select-surface")
    select.add_argument("--details-root", required=True)
    select.add_argument("--release", required=True)
    select.add_argument("--review-date", required=True)
    select.add_argument("--formal-node-id", required=True)
    select.add_argument("--formal-node-sha256", required=True)
    select.add_argument("--true-item-id", required=True)
    select.add_argument("--original-canonical-id", required=True)
    select.add_argument("--exposure-set", required=True)
    select.add_argument("--seed", default="default")
    select.add_argument("--campaign-commitment-id")
    select.add_argument("--consume", action="store_true")
    select.add_argument("--variant-only", action="store_true")
    select.add_argument("--formal-node-main-knowledge")
    select.add_argument("--preferred-variant-id")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        if args.command == "prebuild-plan":
            result = plan_request(args.details_root, args.release, _json_arg(args.targets, "targets"))
        elif args.command == "submit-author":
            result = submit_author(
                args.details_root,
                args.release,
                args.request_ref,
                _json_arg(args.submission, "author_submission"),
            )
        elif args.command == "submit-blind-verifier":
            result = submit_blind_verifier(
                args.details_root,
                args.release,
                args.request_ref,
                args.author_ref,
                args.blind_input_ref,
                _json_arg(args.submission, "blind_submission"),
            )
        elif args.command == "submit-adversarial-verifier":
            result = submit_adversarial_verifier(
                args.details_root,
                args.release,
                args.request_ref,
                args.author_ref,
                args.blind_verifier_ref,
                _json_arg(args.submission, "adversarial_submission"),
            )
        elif args.command == "publish":
            result = publish(
                args.details_root,
                args.release,
                args.request_ref,
                args.author_ref,
                args.blind_verifier_ref,
                args.adversarial_verifier_ref,
            )
        elif args.command == "commit-consumption":
            result = commit_consumption(
                args.details_root,
                args.release,
                args.selection_manifest_ref,
            )
        elif args.command == "audit":
            result = audit(args.details_root, args.release)
        elif args.command == "pool-status":
            result = pool_status(args.details_root, args.release)
        elif args.command == "forecast-fill":
            result = forecast_fill(
                args.details_root,
                args.release,
                _json_arg(args.due_set, "forecast_due_set"),
            )
        else:
            result = select_surface(
                args.details_root,
                args.release,
                review_date=args.review_date,
                formal_node_id=args.formal_node_id,
                formal_node_sha256=args.formal_node_sha256,
                true_item_id=args.true_item_id,
                original_canonical_id=args.original_canonical_id,
                exposure_set=_json_arg(args.exposure_set, "exposure_set"),
                seed=args.seed,
                campaign_commitment_id=args.campaign_commitment_id,
                consume=args.consume,
                variant_only=args.variant_only,
                formal_node_main_knowledge=args.formal_node_main_knowledge,
                preferred_variant_id=args.preferred_variant_id,
            )
    except (OSError, VariantBankError) as exc:
        print(json.dumps({"status": "ERROR", "error": str(exc)}, ensure_ascii=False))
        return 2
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    return 0 if result.get("status") not in {"FAIL", "ERROR"} else 1


if __name__ == "__main__":
    raise SystemExit(main())


__all__ = [
    "ADVERSARIAL_CHECKS",
    "BLIND_CHECKS",
    "INDEPENDENT_ACCEPTANCE_KIND",
    "WEB_ACCEPTANCE_KIND",
    "WEB_GRADING_SCHEMA",
    "WEB_SELF_CHECK_SCHEMA",
    "WEB_SELF_CHECKS",
    "WEB_SOLUTION_SCHEMA",
    "WEB_SUBMISSION_SCHEMA",
    "VariantBankError",
    "audit",
    "commit_consumption",
    "plan_request",
    "pool_status",
    "forecast_fill",
    "import_web_self_checked_variants",
    "publish",
    "select_surface",
    "submit_adversarial_verifier",
    "submit_author",
    "submit_blind_verifier",
    "validate_campaign_commitments",
]
