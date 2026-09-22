#!/usr/bin/env python3
"""Build a bounded, answer-safe personalized cross-subject shadow overlay.

The overlay joins only two disposable projections that are byte-bound by the
current personalization manifest:

* ``failure_signatures.json`` supplies learner evidence for exact endpoint
  knowledge points.
* ``cross-subject-shadow.json`` supplies already source-backed relation bridges.

The result is a review surface.  It never enables live routing and never writes
the formal relation graph.  By default it writes nothing; the optional writer
is restricted to the fixed personalization-derived path.
"""

from __future__ import annotations

import argparse
import fcntl
import hashlib
import json
import os
import re
import sys
import tempfile
from collections import Counter
from contextlib import contextmanager
from datetime import date
from pathlib import Path
from typing import Any, Iterator

from intake_lib_408 import scan_leak


SCHEMA = "personalized_cross_candidates_v1"
KIND = "personalized_cross_candidate_overlay"
GENERATOR_VERSION = "1.0.0"
MANIFEST_SCHEMA = "personalization_index_v1"
FAILURE_SCHEMA = "failure_signature_index_v1"
FAILURE_ITEM_SCHEMA = "failure_signature_v1"
CROSS_SCHEMA = "cross_subject_shadow_v1"
DEFAULT_STATE_RELATIVE = Path("wiki/study_vaults/408-full/state/personalization")
DEFAULT_OUTPUT_NAME = "personalized-cross-candidates.json"
FAILURE_FILE = "failure_signatures.json"
CROSS_FILE = "cross-subject-shadow.json"
DEFAULT_CANDIDATE_LIMIT = 64
MAX_CANDIDATE_LIMIT = 256
DEFAULT_EVIDENCE_LIMIT = 3
MAX_EVIDENCE_LIMIT = 6
MAX_SIGNATURES = 1024
MAX_CROSS_RECORDS = 2048
MAX_REFS = 12
MAX_DIAGNOSTICS = 32
POINT_RE = re.compile(r"^(DS|CO|OS|CN)\d{2}-\d{2}$")
SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
SAFE_ID_RE = re.compile(r"^[A-Za-z0-9_.:@/-]{1,180}$")
TRUSTED_BRIDGE_METHODS = {"formal_edge_projection", "curated_mechanism_spec"}
KEYWORD_METHOD_MARKERS = (
    "keyword",
    "lexical",
    "surface",
    "name_overlap",
    "cooccurrence",
    "co_occurrence",
)


class PersonalizedCrossCandidateError(RuntimeError):
    """Controlled validation error for an untrusted or stale projection."""


def _retired_write_surface() -> None:
    raise PersonalizedCrossCandidateError(
        "personalized_cross_candidate_writes_retired_read_only_compatibility"
    )


def canonical_json_bytes(value: Any) -> bytes:
    return (
        json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        + "\n"
    ).encode("utf-8")


def _sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _valid_date(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    try:
        return date.fromisoformat(value).isoformat()
    except ValueError:
        return None


def _safe_id(value: Any) -> str | None:
    text = str(value or "").strip()
    return text if SAFE_ID_RE.fullmatch(text) else None


def _safe_text(value: Any, *, limit: int = 320) -> str | None:
    if not isinstance(value, str):
        return None
    text = " ".join(value.split())[:limit]
    if not text:
        return None
    fails, warns = scan_leak(text)
    return None if fails or warns else text


def _safe_texts(
    values: Any,
    *,
    maximum: int,
    limit: int = 220,
) -> list[str]:
    if not isinstance(values, list):
        return []
    result: list[str] = []
    for raw in values[:maximum]:
        item = _safe_text(raw, limit=limit)
        if item and item not in result:
            result.append(item)
    return result


def _read_json(path: Path, *, expected_sha256: str | None = None) -> tuple[dict[str, Any], bytes]:
    if path.is_symlink() or not path.is_file():
        raise PersonalizedCrossCandidateError(f"missing_or_unsafe_projection:{path.name}")
    raw = path.read_bytes()
    if expected_sha256 is not None and _sha256_bytes(raw) != expected_sha256:
        raise PersonalizedCrossCandidateError(f"projection_hash_mismatch:{path.name}")
    try:
        value = json.loads(raw)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise PersonalizedCrossCandidateError(f"invalid_json:{path.name}") from exc
    if not isinstance(value, dict):
        raise PersonalizedCrossCandidateError(f"invalid_projection:{path.name}")
    return value, raw


def _manifest_hash(value: Any, *, field: str) -> str:
    if not isinstance(value, str) or not SHA256_RE.fullmatch(value):
        raise PersonalizedCrossCandidateError(f"invalid_manifest_hash:{field}")
    return value


def _load_manifest_bound_inputs(
    state_dir: Path,
) -> tuple[dict[str, Any], bytes, dict[str, Any], bytes, dict[str, Any], bytes]:
    manifest, manifest_raw = _read_json(state_dir / "manifest.json")
    if (
        manifest.get("schema") != MANIFEST_SCHEMA
        or manifest.get("kind") != "personalization_manifest"
        or manifest.get("answer_safe") is not True
        or manifest.get("formal_write_count") != 0
    ):
        raise PersonalizedCrossCandidateError("invalid_personalization_manifest")
    as_of = _valid_date(manifest.get("as_of"))
    if as_of is None:
        raise PersonalizedCrossCandidateError("invalid_personalization_manifest_date")

    artifact_hashes = manifest.get("artifact_hashes")
    if not isinstance(artifact_hashes, dict):
        raise PersonalizedCrossCandidateError("missing_manifest_artifact_hashes")
    failure_hash = _manifest_hash(
        artifact_hashes.get(FAILURE_FILE), field=FAILURE_FILE
    )
    cross_hash = _manifest_hash(
        manifest.get("cross_subject_shadow_sha256"), field=CROSS_FILE
    )
    failures, failures_raw = _read_json(
        state_dir / FAILURE_FILE, expected_sha256=failure_hash
    )
    cross, cross_raw = _read_json(
        state_dir / CROSS_FILE, expected_sha256=cross_hash
    )
    if (
        failures.get("schema") != FAILURE_SCHEMA
        or failures.get("kind") != "recent_failure_signature_index"
        or failures.get("answer_safe") is not True
        or failures.get("formal_write_count") != 0
        or failures.get("as_of") != as_of
    ):
        raise PersonalizedCrossCandidateError("invalid_failure_signature_projection")
    if (
        cross.get("schema_version") != CROSS_SCHEMA
        or cross.get("formal_write_count") != 0
        or not isinstance(cross.get("source_fingerprint"), str)
    ):
        raise PersonalizedCrossCandidateError("invalid_cross_subject_shadow_projection")
    return manifest, manifest_raw, failures, failures_raw, cross, cross_raw


def _validated_signatures(payload: dict[str, Any]) -> dict[str, dict[str, Any]]:
    signatures = payload.get("signatures")
    by_key = payload.get("by_key")
    if not isinstance(signatures, dict) or not isinstance(by_key, dict):
        raise PersonalizedCrossCandidateError("invalid_failure_signature_index")
    if len(signatures) > MAX_SIGNATURES:
        raise PersonalizedCrossCandidateError("failure_signature_bound_exceeded")
    declared = payload.get("signature_count")
    if declared is not None and declared != len(signatures):
        raise PersonalizedCrossCandidateError("failure_signature_count_mismatch")

    result: dict[str, dict[str, Any]] = {}
    for key in sorted(signatures):
        row = signatures[key]
        if not isinstance(row, dict):
            raise PersonalizedCrossCandidateError("invalid_failure_signature_item")
        if (
            row.get("schema") != FAILURE_ITEM_SCHEMA
            or row.get("signature_id") != key
            or row.get("answer_safe") is not True
        ):
            raise PersonalizedCrossCandidateError("invalid_failure_signature_item")
        point_ids = row.get("point_ids")
        if not isinstance(point_ids, list) or any(
            not isinstance(item, str) or not POINT_RE.fullmatch(item)
            for item in point_ids
        ):
            raise PersonalizedCrossCandidateError("invalid_failure_signature_points")
        for field in (
            "mechanism_ids",
            "question_ids",
            "mechanism_primitives",
            "objects",
            "preconditions",
            "state_transitions",
            "causal_directions",
            "boundaries_and_units",
            "error_patterns",
            "method_templates",
            "evidence_gaps",
        ):
            values = row.get(field, [])
            if not isinstance(values, list) or any(
                not isinstance(item, str) or _safe_id(item) is None
                for item in values
            ):
                raise PersonalizedCrossCandidateError(
                    f"invalid_failure_signature_field:{field}"
                )
        evidence = row.get("evidence")
        if not isinstance(evidence, dict):
            raise PersonalizedCrossCandidateError("invalid_failure_signature_evidence")
        observed_date = evidence.get("observed_date")
        if observed_date is not None and _valid_date(observed_date) is None:
            raise PersonalizedCrossCandidateError("invalid_failure_signature_date")
        result[key] = row

    for key, signature_ids in by_key.items():
        if not isinstance(key, str) or not isinstance(signature_ids, list):
            raise PersonalizedCrossCandidateError("invalid_failure_signature_inverted_index")
        if len(signature_ids) > 16 or any(item not in result for item in signature_ids):
            raise PersonalizedCrossCandidateError("invalid_failure_signature_inverted_index")
    return result


def _validated_cross_records(payload: dict[str, Any]) -> list[dict[str, Any]]:
    records = payload.get("records")
    if not isinstance(records, list):
        raise PersonalizedCrossCandidateError("invalid_cross_subject_records")
    if len(records) > MAX_CROSS_RECORDS:
        raise PersonalizedCrossCandidateError("cross_subject_record_bound_exceeded")
    result: list[dict[str, Any]] = []
    seen: set[str] = set()
    for row in records:
        if not isinstance(row, dict):
            raise PersonalizedCrossCandidateError("invalid_cross_subject_record")
        edge_id = _safe_id(row.get("edge_id") or row.get("id"))
        source = row.get("source_point_id")
        target = row.get("target_point_id")
        if (
            edge_id is None
            or edge_id in seen
            or not isinstance(source, str)
            or not POINT_RE.fullmatch(source)
            or not isinstance(target, str)
            or not POINT_RE.fullmatch(target)
            or source[:2] == target[:2]
        ):
            raise PersonalizedCrossCandidateError("invalid_cross_subject_record")
        if _relation_code(row) is None:
            raise PersonalizedCrossCandidateError("invalid_cross_subject_relation_code")
        seen.add(edge_id)
        result.append(row)
    return result


def _signature_evidence(
    signature: dict[str, Any], endpoint: str
) -> dict[str, Any]:
    evidence = signature.get("evidence") or {}
    question_ids = [
        item
        for item in signature.get("question_ids", [])
        if isinstance(item, str) and _safe_id(item)
    ] if isinstance(signature.get("question_ids"), list) else []
    question_id = _safe_id(evidence.get("question_id"))
    if question_id is None and len(question_ids) == 1:
        question_id = question_ids[0]
    gaps = [
        item
        for item in signature.get("evidence_gaps", [])
        if isinstance(item, str) and _safe_id(item)
    ] if isinstance(signature.get("evidence_gaps"), list) else []
    event_id = _safe_id(evidence.get("event_id"))
    observed_date = _valid_date(evidence.get("observed_date"))
    if event_id is None:
        gaps.append("event_id_missing")
    if question_id is None:
        gaps.append("question_id_missing")
    if observed_date is None:
        gaps.append("observed_date_missing")
    layer = _safe_id(signature.get("layer"))
    if layer is None:
        gaps.append("layer_missing")
    return {
        "signature_id": signature["signature_id"],
        "event_id": event_id,
        "question_id": question_id,
        "observed_date": observed_date,
        "layer": layer,
        "point_id": endpoint,
        "provenance": _safe_id(evidence.get("provenance")),
        "mechanism_primitive_ids": sorted(
            {
                item
                for item in signature.get("mechanism_primitives", [])
                if isinstance(item, str) and _safe_id(item)
            }
        )[:12],
        "error_pattern_ids": sorted(
            {
                item
                for item in signature.get("error_patterns", [])
                if isinstance(item, str) and _safe_id(item)
            }
        )[:8],
        "boundary_ids": sorted(
            {
                item
                for item in signature.get("boundaries_and_units", [])
                if isinstance(item, str) and _safe_id(item)
            }
        )[:8],
        "evidence_gaps": sorted(set(gaps)),
    }


def _evidence_identity(row: dict[str, Any]) -> tuple[str, str]:
    for key in ("event_id", "question_id", "signature_id"):
        value = row.get(key)
        if value:
            return key, str(value)
    return "missing", "missing"


def _evidence_complete(row: dict[str, Any]) -> bool:
    return all(
        row.get(key) is not None
        for key in ("event_id", "question_id", "observed_date", "layer")
    )


def _evidence_for_endpoint(
    signatures: dict[str, dict[str, Any]],
    endpoint: str,
    *,
    limit: int,
) -> list[dict[str, Any]]:
    rows = [
        _signature_evidence(signature, endpoint)
        for signature in signatures.values()
        if endpoint in signature.get("point_ids", [])
    ]
    rows.sort(
        key=lambda row: (
            str(row.get("observed_date") or ""),
            str(row.get("event_id") or ""),
            str(row.get("question_id") or ""),
            str(row.get("signature_id") or ""),
        ),
        reverse=True,
    )
    return rows[:limit]


def _point_view(record: dict[str, Any], point_id: str) -> dict[str, Any]:
    for key in ("left", "right"):
        value = record.get(key)
        if isinstance(value, dict) and value.get("id") == point_id:
            return {
                "point_id": point_id,
                "subject": point_id[:2],
                "label": _safe_text(value.get("name"), limit=100),
            }
    return {"point_id": point_id, "subject": point_id[:2], "label": None}


def _relation_code(record: dict[str, Any]) -> str | None:
    relation = record.get("relation")
    value = record.get("relation_code")
    if not value and isinstance(relation, dict):
        value = relation.get("code")
    text = str(value or "").strip()
    return text if re.fullmatch(r"R0[1-9]", text) else None


def _bridge_kind(record: dict[str, Any], relation_code: str | None) -> str:
    if relation_code == "R06":
        return "causal_or_pipeline"
    if relation_code == "R07":
        return "analogy_only"
    category = _safe_id(record.get("relation_kind"))
    if category in {
        "same_core_mechanism",
        "same_solution_pattern",
        "shared_concept_boundary",
        "contrastive_concept_boundary",
        "same_observed_error_mode",
    }:
        return "structural"
    return "source_described_structure"


def _bridge(record: dict[str, Any]) -> dict[str, Any]:
    method = _safe_id(record.get("generation_method"))
    lower_method = str(method or "").lower()
    keyword_only = any(marker in lower_method for marker in KEYWORD_METHOD_MARKERS)
    summary = _safe_text(record.get("evidence_summary"), limit=420)
    refs = _safe_texts(record.get("source_refs"), maximum=MAX_REFS, limit=240)
    relation_code = _relation_code(record)
    direction = record.get("direction")
    safe_direction = {
        "kind": None,
        "from": record.get("source_point_id"),
        "to": record.get("target_point_id"),
    }
    if isinstance(direction, dict):
        kind = direction.get("kind")
        if kind in {"directed", "bidirectional"}:
            safe_direction["kind"] = kind
        if direction.get("from") in {
            record.get("source_point_id"),
            record.get("target_point_id"),
        }:
            safe_direction["from"] = direction.get("from")
        if direction.get("to") in {
            record.get("source_point_id"),
            record.get("target_point_id"),
        }:
            safe_direction["to"] = direction.get("to")
    direction_valid = bool(
        safe_direction["kind"] in {"directed", "bidirectional"}
        and safe_direction["from"]
        in {record.get("source_point_id"), record.get("target_point_id")}
        and safe_direction["to"]
        in {record.get("source_point_id"), record.get("target_point_id")}
        and safe_direction["from"] != safe_direction["to"]
    )
    source_backed = bool(
        method in TRUSTED_BRIDGE_METHODS
        and summary
        and refs
        and direction_valid
        and not keyword_only
    )
    return {
        "kind": _bridge_kind(record, relation_code),
        "relation_code": relation_code,
        "direction": safe_direction,
        "summary": summary,
        "source_refs": refs,
        "generation_method": method,
        "source_backed": source_backed,
        "direction_valid": direction_valid,
        "keyword_only": keyword_only,
    }


def _common_mechanism(
    source_evidence: list[dict[str, Any]],
    target_evidence: list[dict[str, Any]],
    *,
    bridge_source_backed: bool,
) -> dict[str, Any]:
    source_primitives = {
        item
        for row in source_evidence
        for item in row.get("mechanism_primitive_ids", [])
    }
    target_primitives = {
        item
        for row in target_evidence
        for item in row.get("mechanism_primitive_ids", [])
    }
    shared = sorted(source_primitives & target_primitives)
    if shared:
        return {
            "basis": "controlled_mechanism_primitive_overlap",
            "primitive_ids": shared[:12],
            "claim": "两侧失败证据共享同一受控机制原语。",
        }
    if bridge_source_backed:
        claim = (
            "两侧受控机制原语没有交集；这里只保留来源支持的因果或结构桥，不能声称机制相同。"
            if source_evidence and target_evidence
            else "当前缺少一侧失败证据，无法确认受控机制原语交集；这里只保留来源支持的桥。"
        )
        return {
            "basis": "source_backed_bridge_only",
            "primitive_ids": [],
            "claim": claim,
        }
    return {
        "basis": "unproven_no_controlled_overlap",
        "primitive_ids": [],
        "claim": "没有受控机制原语交集，也没有通过门禁的来源桥。",
    }


def _review_state(
    *,
    source_evidence: list[dict[str, Any]],
    target_evidence: list[dict[str, Any]],
    complete_evidence_on_both_sides: bool,
    distinct_across_sides: bool,
    bridge: dict[str, Any],
) -> tuple[str, bool]:
    if not source_evidence or not target_evidence:
        return "shadow_needs_other_side_evidence", False
    if not complete_evidence_on_both_sides:
        return "shadow_needs_complete_side_evidence", False
    if bridge["keyword_only"]:
        return "shadow_keyword_only_rejected", False
    if bridge["relation_code"] == "R07" or bridge["kind"] == "analogy_only":
        return "shadow_analogy_only", False
    if not bridge["source_backed"]:
        return "shadow_needs_source_backed_bridge", False
    if not distinct_across_sides:
        return "shadow_needs_distinct_side_evidence", False
    return "shadow_review_ready", True


def _evidence_gaps(
    *,
    source_evidence: list[dict[str, Any]],
    target_evidence: list[dict[str, Any]],
    complete_evidence_on_both_sides: bool,
    distinct_across_sides: bool,
    bridge: dict[str, Any],
    common_mechanism: dict[str, Any],
) -> list[str]:
    gaps: set[str] = set()
    if not source_evidence:
        gaps.add("source_side_user_evidence_missing")
    if not target_evidence:
        gaps.add("target_side_user_evidence_missing")
    if source_evidence and target_evidence and not complete_evidence_on_both_sides:
        gaps.add("complete_event_question_date_layer_evidence_missing")
    if source_evidence and target_evidence and not distinct_across_sides:
        gaps.add("distinct_cross_side_outcomes_missing")
    if bridge["keyword_only"]:
        gaps.add("keyword_only_relation_forbidden")
    if bridge["relation_code"] == "R07":
        gaps.add("r07_analogy_not_mechanism_bridge")
    if not bridge["direction_valid"]:
        gaps.add("bridge_direction_invalid")
    if not bridge["source_backed"]:
        gaps.add("source_backed_bridge_missing")
    if common_mechanism["basis"] != "controlled_mechanism_primitive_overlap":
        gaps.add("controlled_mechanism_primitive_overlap_missing")
    for prefix, rows in (("source", source_evidence), ("target", target_evidence)):
        for row in rows:
            for gap in row.get("evidence_gaps", []):
                gaps.add(f"{prefix}_evidence:{gap}")
    return sorted(gaps)


def _teaching_value(
    *,
    source_evidence: list[dict[str, Any]],
    target_evidence: list[dict[str, Any]],
    common_mechanism: dict[str, Any],
) -> str:
    if not source_evidence or not target_evidence:
        return (
            "只可作为另一科定向检查候选；另一侧尚无用户失败证据，不能声称用户在两科重复犯错。"
        )
    if common_mechanism["basis"] == "controlled_mechanism_primitive_overlap":
        return (
            "可用于检查用户能否把同一受控机制原语迁移到两科不同对象、阶段和边界；仍需人工审阅题目适配性。"
        )
    return (
        "可沿来源支持的因果或结构桥检查跨科迁移；当前不能把该桥表述为两侧共享同一受控机制。"
    )


def _counterexamples_and_limits(bridge: dict[str, Any]) -> list[str]:
    rows = [
        "考点之间存在桥，不等于用户在两端具有相同错因，也不证明任一端已经掌握。",
        "知识点级关系不能替代具体题目的条件、对象、单位、状态变化和答案独立校验。",
    ]
    if bridge["kind"] == "causal_or_pipeline":
        rows.append("有向前置或责任链不能自动反向使用；下游结论不能倒推上游全部条件成立。")
    elif bridge["kind"] == "analogy_only":
        rows.append("R07 只允许教学类比；对象、触发条件或代价不同即不适用，不能据此调题。")
    else:
        rows.append("结构相似不等于执行主体、时间尺度或边界条件相同。")
    return rows


def _candidate_id(
    edge_id: str,
    source_evidence: list[dict[str, Any]],
    target_evidence: list[dict[str, Any]],
) -> str:
    material = {
        "edge_id": edge_id,
        "source_signatures": [row["signature_id"] for row in source_evidence],
        "target_signatures": [row["signature_id"] for row in target_evidence],
    }
    return "PCX408-" + _sha256_bytes(canonical_json_bytes(material))[:20].upper()


def _candidate(
    record: dict[str, Any],
    signatures: dict[str, dict[str, Any]],
    *,
    evidence_limit: int,
) -> dict[str, Any] | None:
    source_id = str(record["source_point_id"])
    target_id = str(record["target_point_id"])
    source_evidence = _evidence_for_endpoint(
        signatures, source_id, limit=evidence_limit
    )
    target_evidence = _evidence_for_endpoint(
        signatures, target_id, limit=evidence_limit
    )
    if not source_evidence and not target_evidence:
        return None
    complete_source = [row for row in source_evidence if _evidence_complete(row)]
    complete_target = [row for row in target_evidence if _evidence_complete(row)]
    complete_evidence_on_both_sides = bool(complete_source and complete_target)
    source_identities = {_evidence_identity(row) for row in complete_source}
    target_identities = {_evidence_identity(row) for row in complete_target}
    distinct_across_sides = bool(
        complete_evidence_on_both_sides
        and (source_identities - target_identities)
        and (target_identities - source_identities)
    )
    bridge = _bridge(record)
    common_mechanism = _common_mechanism(
        source_evidence,
        target_evidence,
        bridge_source_backed=bool(bridge["source_backed"]),
    )
    review_state, review_ready = _review_state(
        source_evidence=source_evidence,
        target_evidence=target_evidence,
        complete_evidence_on_both_sides=complete_evidence_on_both_sides,
        distinct_across_sides=distinct_across_sides,
        bridge=bridge,
    )
    edge_id = str(record.get("edge_id") or record.get("id"))
    return {
        "candidate_id": _candidate_id(edge_id, source_evidence, target_evidence),
        "edge_id": edge_id,
        "lifecycle": "personalized_shadow_candidate",
        "review_state": review_state,
        "review_ready": review_ready,
        "source_point": _point_view(record, source_id),
        "target_point": _point_view(record, target_id),
        "common_mechanism": common_mechanism,
        "causal_or_structural_bridge": bridge,
        "user_evidence": {
            "source": source_evidence,
            "target": target_evidence,
            "complete_evidence_on_both_sides": complete_evidence_on_both_sides,
            "distinct_evidence_across_sides": distinct_across_sides,
        },
        "teaching_value": _teaching_value(
            source_evidence=source_evidence,
            target_evidence=target_evidence,
            common_mechanism=common_mechanism,
        ),
        "counterexamples_and_limits": _counterexamples_and_limits(bridge),
        "evidence_gaps": _evidence_gaps(
            source_evidence=source_evidence,
            target_evidence=target_evidence,
            complete_evidence_on_both_sides=complete_evidence_on_both_sides,
            distinct_across_sides=distinct_across_sides,
            bridge=bridge,
            common_mechanism=common_mechanism,
        ),
        "enabled_for_live_routing": False,
        "formal_write_count": 0,
    }


def build_overlay(
    repo: Path | str,
    *,
    candidate_limit: int = DEFAULT_CANDIDATE_LIMIT,
    evidence_limit: int = DEFAULT_EVIDENCE_LIMIT,
) -> dict[str, Any]:
    """Build the deterministic review overlay without mutating any file."""

    if not 1 <= candidate_limit <= MAX_CANDIDATE_LIMIT:
        raise PersonalizedCrossCandidateError("candidate_limit_out_of_range")
    if not 1 <= evidence_limit <= MAX_EVIDENCE_LIMIT:
        raise PersonalizedCrossCandidateError("evidence_limit_out_of_range")
    root = Path(repo).resolve()
    state_dir = (root / DEFAULT_STATE_RELATIVE).resolve()
    try:
        state_dir.relative_to(root)
    except ValueError as exc:
        raise PersonalizedCrossCandidateError(
            "personalization_state_outside_repository"
        ) from exc
    (
        manifest,
        manifest_raw,
        failures,
        failures_raw,
        cross,
        cross_raw,
    ) = _load_manifest_bound_inputs(state_dir)
    signatures = _validated_signatures(failures)
    records = _validated_cross_records(cross)
    candidates = [
        candidate
        for record in records
        if (candidate := _candidate(record, signatures, evidence_limit=evidence_limit))
        is not None
    ]
    status_rank = {
        "shadow_review_ready": 0,
        "shadow_needs_distinct_side_evidence": 1,
        "shadow_needs_complete_side_evidence": 2,
        "shadow_needs_other_side_evidence": 3,
        "shadow_analogy_only": 4,
        "shadow_needs_source_backed_bridge": 5,
        "shadow_keyword_only_rejected": 6,
    }
    candidates.sort(
        key=lambda row: (
            status_rank.get(str(row["review_state"]), 99),
            str(row["source_point"]["point_id"]),
            str(row["target_point"]["point_id"]),
            str(row["edge_id"]),
            str(row["candidate_id"]),
        )
    )
    total_candidates = len(candidates)
    candidates = candidates[:candidate_limit]
    state_counts = Counter(str(row["review_state"]) for row in candidates)
    input_hashes = {
        "manifest.json": _sha256_bytes(manifest_raw),
        FAILURE_FILE: _sha256_bytes(failures_raw),
        CROSS_FILE: _sha256_bytes(cross_raw),
    }
    build_fingerprint = _sha256_bytes(
        canonical_json_bytes(
            {
                "schema": SCHEMA,
                "generator_version": GENERATOR_VERSION,
                "input_hashes": input_hashes,
                "candidate_limit": candidate_limit,
                "evidence_limit": evidence_limit,
            }
        )
    )
    diagnostics: list[str] = []
    if total_candidates > candidate_limit:
        diagnostics.append(
            f"candidate_limit_trimmed:{total_candidates - candidate_limit}"
        )
    if not candidates:
        diagnostics.append("no_cross_record_has_endpoint_failure_evidence")
    return {
        "schema": SCHEMA,
        "kind": KIND,
        "generator_version": GENERATOR_VERSION,
        "as_of": manifest["as_of"],
        "mode": "read_only_shadow_review",
        "answer_safe": True,
        "build_fingerprint": build_fingerprint,
        "input_binding": {
            "manifest_build_id": manifest.get("build_id"),
            "hashes": input_hashes,
            "hash_verification": "pass",
            "allowed_inputs": [FAILURE_FILE, CROSS_FILE],
        },
        "bounds": {
            "candidate_limit": candidate_limit,
            "evidence_per_side_limit": evidence_limit,
            "input_signature_limit": MAX_SIGNATURES,
            "input_cross_record_limit": MAX_CROSS_RECORDS,
        },
        "counts": {
            "failure_signatures": len(signatures),
            "cross_records": len(records),
            "cross_records_with_user_evidence": total_candidates,
            "candidates_emitted": len(candidates),
            "candidates_trimmed": max(0, total_candidates - len(candidates)),
            "review_ready": sum(bool(row["review_ready"]) for row in candidates),
            "by_review_state": dict(sorted(state_counts.items())),
        },
        "candidates": candidates,
        "diagnostics": diagnostics[:MAX_DIAGNOSTICS],
        "enabled_for_live_routing": False,
        "live_routing_authority": "none_shadow_review_only",
        "formal_write_count": 0,
        "formal_relation_write_count": 0,
    }


@contextmanager
def _output_lock(state_dir: Path) -> Iterator[None]:
    lock_path = state_dir / ".personalized-cross-candidates.lock"
    with lock_path.open("a+", encoding="utf-8") as handle:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def write_overlay(repo: Path | str, payload: dict[str, Any]) -> dict[str, Any]:
    """Write only the fixed disposable personalization-derived projection."""

    _retired_write_surface()

    # Historical implementation retained below for read-only source compatibility.
    root = Path(repo).resolve()
    state_dir = (root / DEFAULT_STATE_RELATIVE).resolve()
    try:
        state_dir.relative_to(root)
    except ValueError as exc:
        raise PersonalizedCrossCandidateError(
            "personalization_state_outside_repository"
        ) from exc
    target = (state_dir / DEFAULT_OUTPUT_NAME).resolve()
    if target.parent != state_dir:
        raise PersonalizedCrossCandidateError("unsafe_overlay_output_path")
    bounds = payload.get("bounds") if isinstance(payload, dict) else None
    if not isinstance(bounds, dict):
        raise PersonalizedCrossCandidateError("invalid_overlay_payload")
    try:
        candidate_limit = int(bounds.get("candidate_limit", 0))
        evidence_limit = int(bounds.get("evidence_per_side_limit", 0))
    except (TypeError, ValueError) as exc:
        raise PersonalizedCrossCandidateError("invalid_overlay_payload") from exc
    expected = build_overlay(
        root,
        candidate_limit=candidate_limit,
        evidence_limit=evidence_limit,
    )
    if canonical_json_bytes(expected) != canonical_json_bytes(payload):
        raise PersonalizedCrossCandidateError("overlay_payload_not_current_rebuild")
    if (
        payload.get("formal_write_count") != 0
        or payload.get("formal_relation_write_count") != 0
        or payload.get("enabled_for_live_routing") is not False
        or any(
            row.get("enabled_for_live_routing") is not False
            or row.get("formal_write_count") != 0
            for row in payload.get("candidates", [])
        )
    ):
        raise PersonalizedCrossCandidateError("overlay_write_boundary_violation")
    state_dir.mkdir(parents=True, exist_ok=True)
    data = canonical_json_bytes(payload)
    with _output_lock(state_dir):
        fd, temporary = tempfile.mkstemp(prefix=f".{DEFAULT_OUTPUT_NAME}.", dir=state_dir)
        try:
            with os.fdopen(fd, "wb") as handle:
                handle.write(data)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, target)
        finally:
            if os.path.exists(temporary):
                os.unlink(temporary)
    return {
        "status": "written_disposable_projection",
        "path": str(target),
        "sha256": _sha256_bytes(data),
        "candidate_count": len(payload["candidates"]),
        "enabled_for_live_routing": False,
        "formal_write_count": 0,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=(
            "从 manifest 绑定的 failure signatures 与 cross shadow 生成个性化跨科影子审阅面"
        )
    )
    parser.add_argument("--repo", default=str(Path(__file__).resolve().parents[1]))
    parser.add_argument(
        "--candidate-limit",
        type=int,
        default=DEFAULT_CANDIDATE_LIMIT,
    )
    parser.add_argument(
        "--evidence-limit",
        type=int,
        default=DEFAULT_EVIDENCE_LIMIT,
    )
    args = parser.parse_args(argv)
    try:
        payload = build_overlay(
            args.repo,
            candidate_limit=args.candidate_limit,
            evidence_limit=args.evidence_limit,
        )
    except PersonalizedCrossCandidateError as exc:
        print(f"[错误] {exc}", file=sys.stderr)
        return 2
    print(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
