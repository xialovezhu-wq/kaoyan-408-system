#!/usr/bin/env python3
"""Reproducible, temp-only performance acceptance for the 408 Harness.

This benchmark measures four bounded managed surfaces:

1. the read-only ``safe_preanswer_slice`` lookup;
2. the managed ``begin_personalized_turn`` plus
   ``finalize_personalized_reply`` persistence and entry routing;
3. the non-morning ``personalized_study_turn_408`` hot outcome/receipt path; and
4. the managed ``morning_review_session.py answer-turn`` independent-correct
   path with event-specific review/session receipts.

The two learning benchmarks execute only the managed hot implementation.  They
deliberately do not run or certify the legacy/recovery branches, whose broad
replay/audit boundary remains a machine-readable non-claim.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import re
import statistics
import subprocess
import sys
import tempfile
import time
from contextlib import contextmanager
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any, Iterator
from zoneinfo import ZoneInfo

import personalization_preanswer_408 as preanswer_module
import personalized_study_turn_408 as study_turn_module
import morning_review_session as morning_module
import morning_session_hot_state_408 as morning_hot_module
import review_feedback_loop as review_canonical
import review_hot_projection_408 as formal_hot_module
import review_hot_state_408 as review_hot_module
from personalization_commonality_408 import build_recent_signature_index
from personalization_preanswer_408 import safe_preanswer_slice
from personalization_turn_receipt_408 import (
    ADOPTION_SCHEMA,
    BEGIN_SCHEMA_V2,
    ENTRY_TO_CONVERSATION,
    PUBLIC_BEGIN_MAX_BYTES,
    audit_receipts,
    begin_personalized_turn,
    finalize_personalized_reply,
)


SCHEMA = "personalization_harness_performance_benchmark_v1"
INTERACTIVE_SCHEMA = "interactive_hotpath_benchmark_v2"
AS_OF_DEFAULT = datetime.now(ZoneInfo("Asia/Shanghai")).date().isoformat()
PREANSWER_P95_LIMIT_MS = 250.0
MANAGED_P95_LIMIT_MS = 5_000.0
STUDY_TURN_P95_LIMIT_MS = 5_000.0
MORNING_ANSWER_TURN_P95_LIMIT_MS = 5_000.0
MAX_SLICE_BYTES = 12_288
MAX_STUDY_TURN_OUTPUT_BYTES = 16_384
MAX_MORNING_ANSWER_TURN_OUTPUT_BYTES = 32_768
MIN_HISTORY_DAYS = 100
MIN_POINTS_PER_SUBJECT = 32
MIN_QUESTIONS_PER_POINT = 3
MIN_PREANSWER_ITERATIONS = 20
MIN_MANAGED_ITERATIONS = len(ENTRY_TO_CONVERSATION)
MIN_STUDY_TURN_ITERATIONS = 20
MIN_MORNING_ANSWER_TURN_ITERATIONS = 20
MIN_INTERACTIVE_ITERATIONS = 20
INTERACTIVE_READY_IN_PROCESS_P95_LIMIT_MS = 30.0
INTERACTIVE_READY_CLI_P95_LIMIT_MS = 150.0
SUBJECTS = ("DS", "CO", "OS", "CN")


class BenchmarkError(RuntimeError):
    """Stable benchmark construction or contract error."""


def _json_bytes(value: Any, *, pretty: bool = False) -> bytes:
    options: dict[str, Any] = {"ensure_ascii": False, "sort_keys": True}
    if pretty:
        options["indent"] = 2
    else:
        options["separators"] = (",", ":")
    return (json.dumps(value, **options) + "\n").encode("utf-8")


def _sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _sha256_file(path: Path) -> str:
    return _sha256_bytes(path.read_bytes())


def _write_json(path: Path, value: Any) -> bytes:
    raw = _json_bytes(value, pretty=True)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(raw)
    return raw


def _nearest_rank(values: list[float], percentile: float) -> float:
    if not values:
        raise BenchmarkError("empty_timing_sample")
    ordered = sorted(values)
    index = max(0, math.ceil(percentile * len(ordered)) - 1)
    return ordered[index]


def _timing_summary(values_ms: list[float]) -> dict[str, float]:
    return {
        "p50_ms": round(float(statistics.median(values_ms)), 3),
        "p95_ms": round(_nearest_rank(values_ms, 0.95), 3),
        "max_ms": round(max(values_ms), 3),
        "mean_ms": round(float(statistics.fmean(values_ms)), 3),
    }


def _tree_digest(
    root: Path,
    *,
    exclude_prefix: str | None = None,
    exclude_paths: tuple[str, ...] = (),
) -> tuple[str, int]:
    material: list[bytes] = []
    count = 0
    if not root.exists():
        return _sha256_bytes(b""), 0
    for path in sorted(item for item in root.rglob("*") if item.is_file()):
        relative = path.relative_to(root).as_posix()
        if relative in exclude_paths:
            continue
        if exclude_prefix and (
            relative == exclude_prefix or relative.startswith(f"{exclude_prefix}/")
        ):
            continue
        material.append(relative.encode("utf-8") + b"\0" + _sha256_file(path).encode("ascii"))
        count += 1
    return _sha256_bytes(b"\n".join(material)), count


def _state(entity_id: str, as_of: str, ordinal: int) -> dict[str, Any]:
    current = date.fromisoformat(as_of)
    last = current - timedelta(days=ordinal % 37)
    failures = 1 + ordinal % 7
    recent = 1 + ordinal % 3 if (current - last).days < 30 else 0
    return {
        "id": entity_id,
        "outcome_count": failures + 2,
        "repair_count": ordinal % 2,
        "independent_correct_count": ordinal % 3,
        "failure_count": failures,
        "fragile_count": ordinal % 2,
        "stable_retention_count": ordinal % 3,
        "recent_7d_outcome_count": recent,
        "recent_7d_failure_count": recent,
        "recent_30d_outcome_count": failures,
        "recent_30d_failure_count": failures,
        "last_attempt_date": last.isoformat(),
        "last_outcome_date": last.isoformat(),
        "last_failure_date": last.isoformat(),
        "last_independent_correct_date": None,
        "last_stable_retention_date": None,
        "evidence_event_ids": [f"RE-PERF-{ordinal:06d}"],
    }


def _source(node_id: str) -> dict[str, Any]:
    return {
        "status": "verified_exam_locator",
        "source_id": f"EXAM-{node_id}",
        "source_ref": f"synthetic-question-sources/{node_id}.json",
        "source_ref_kind": "question_asset_manifest",
        "source_sha256": hashlib.sha256(node_id.encode("utf-8")).hexdigest(),
        "surface_verified": True,
        "answer_isolated": True,
        "original_question_eligible": True,
    }


def _reference(question: dict[str, Any]) -> dict[str, Any]:
    source = question["reliable_source"]
    return {
        "node_id": question["node_id"],
        "subject": question["subject"],
        "source_status": source["status"],
        "original_question_eligible": source["original_question_eligible"],
        "effective_last_attempt_date": question["effective_last_attempt_date"],
    }


def _point_ids(points_per_subject: int) -> dict[str, list[str]]:
    result: dict[str, list[str]] = {}
    for subject in SUBJECTS:
        values: list[str] = []
        for ordinal in range(points_per_subject):
            module = ordinal // 4 + 1
            point = ordinal % 4 + 1
            values.append(f"{subject}{module:02d}-{point:02d}")
        result[subject] = values
    return result


def _build_catalog(point_map: dict[str, list[str]]) -> dict[str, Any]:
    names = {
        "DS": "数据结构",
        "CO": "计算机组成原理",
        "OS": "操作系统",
        "CN": "计算机网络",
    }
    modules: dict[str, Any] = {}
    points: dict[str, Any] = {}
    name_index: dict[str, list[str]] = {}
    for subject, point_ids in point_map.items():
        for point_id in point_ids:
            module_id = point_id[:4]
            modules.setdefault(
                module_id,
                {
                    "module_id": module_id,
                    "module_name": f"合成模块 {module_id}",
                    "subject": subject,
                    "subject_name": names[subject],
                },
            )
            point_name = f"合成考点 {point_id}"
            points[point_id] = {
                "point_id": point_id,
                "name": point_name,
                "label": f"{point_id} {point_name}",
                "subject": subject,
                "subject_name": names[subject],
                "module_id": module_id,
                "module_name": modules[module_id]["module_name"],
            }
            name_index[point_name] = [point_id]
    return {
        "schema": "personalization_index_v1",
        "kind": "static_point_catalog",
        "point_count": len(points),
        "module_count": len(modules),
        "subjects": {
            subject: {"subject": subject, "subject_name": name}
            for subject, name in names.items()
        },
        "modules": modules,
        "points": points,
        "name_index": name_index,
    }


def _build_questions(
    point_map: dict[str, list[str]],
    *,
    as_of: str,
    questions_per_point: int,
) -> tuple[dict[str, Any], dict[str, dict[str, Any]]]:
    current = date.fromisoformat(as_of)
    questions: dict[str, dict[str, Any]] = {}
    by_point: dict[str, list[dict[str, Any]]] = {}
    by_mechanism: dict[str, list[dict[str, Any]]] = {}
    mechanisms: dict[str, Any] = {}
    subject_names = {
        "DS": "数据结构",
        "CO": "计算机组成原理",
        "OS": "操作系统",
        "CN": "计算机网络",
    }
    for subject, point_ids in point_map.items():
        sequence = 0
        for point_id in point_ids:
            mechanism_id = f"KP:{point_id}"
            node_ids: list[str] = []
            references: list[dict[str, Any]] = []
            for local_ordinal in range(questions_per_point):
                sequence += 1
                node_id = f"{subject}_2010_{sequence:03d}"
                last_date = current - timedelta(days=(sequence * 7 + local_ordinal) % 121)
                question = {
                    "node_id": node_id,
                    "subject": subject,
                    "subject_name": subject_names[subject],
                    "primary_point_ids": [point_id],
                    "point_ids": [point_id],
                    "mechanism_ids": [mechanism_id],
                    "reliable_source": _source(node_id),
                    "formal_last_review_date": last_date.isoformat(),
                    "ledger_last_attempt_date": None,
                    "effective_last_attempt_date": last_date.isoformat(),
                    "effective_last_attempt_source": "formal_node",
                    "ledger_evidence_event_ids": [],
                }
                questions[node_id] = question
                node_ids.append(node_id)
                references.append(_reference(question))
            mechanisms[mechanism_id] = {
                "mechanism_id": mechanism_id,
                "kind": "knowledge_point",
                "point_ids": [point_id],
                "formal_node_ids": node_ids,
            }
            by_point[point_id] = references
            by_mechanism[mechanism_id] = list(references)
    question_index = {
        "schema": "personalization_index_v1",
        "kind": "question_inverted_index",
        "question_count": len(questions),
        "mechanism_count": len(mechanisms),
        "questions": questions,
        "mechanisms": mechanisms,
        "by_point": by_point,
        "by_mechanism": by_mechanism,
    }
    return question_index, questions


def _build_learner(
    point_map: dict[str, list[str]],
    questions: dict[str, dict[str, Any]],
    *,
    as_of: str,
) -> dict[str, Any]:
    point_states: dict[str, Any] = {}
    mechanism_states: dict[str, Any] = {}
    question_states: dict[str, Any] = {}
    ordinal = 0
    for point_ids in point_map.values():
        for point_id in point_ids:
            ordinal += 1
            point_states[point_id] = _state(point_id, as_of, ordinal)
            mechanism_states[f"KP:{point_id}"] = _state(
                f"KP:{point_id}", as_of, ordinal + 10_000
            )
    for node_id in questions:
        ordinal += 1
        question_states[node_id] = _state(node_id, as_of, ordinal + 20_000)
    return {
        "schema": "personalization_index_v1",
        "kind": "sparse_learner_state",
        "as_of": as_of,
        "source_event_count": 20_000,
        "performance_event_count": 20_000,
        "applied_performance_event_ids": [],
        "point_state_count": len(point_states),
        "mechanism_state_count": len(mechanism_states),
        "question_state_count": len(question_states),
        "points": point_states,
        "mechanisms": mechanism_states,
        "questions": question_states,
        "selection_commitments": {},
        "evidence_insufficient": {"count": 0, "event_ids": []},
    }


def _build_signatures(
    point_map: dict[str, list[str]], questions: dict[str, dict[str, Any]], *, as_of: str
) -> dict[str, Any]:
    point_ids = [point for values in point_map.values() for point in values]
    question_by_point = {
        question["point_ids"][0]: question["node_id"]
        for question in questions.values()
    }
    events: list[dict[str, Any]] = []
    mappings: dict[str, Any] = {"events": {}}
    current = date.fromisoformat(as_of)
    for ordinal in range(512):
        point_id = point_ids[ordinal % len(point_ids)]
        event_id = f"RE-PERF-SIG-{ordinal:06d}"
        observed = current - timedelta(days=ordinal % 30)
        first_break = (
            "把中断响应和中断处理程序混成同一阶段"
            if point_id.startswith(("CO", "OS"))
            else "第一动作未先区分对象与状态变化"
        )
        first_action = (
            "先划分硬件阶段和软件阶段"
            if point_id.startswith(("CO", "OS"))
            else "先确认对象再追踪状态变化"
        )
        events.append(
            {
                "event_kind": "outcome",
                "event_id": event_id,
                "observed_date": observed.isoformat(),
                "first_result": "wrong" if ordinal % 3 else "partial",
                "choice_result": "incorrect" if ordinal % 2 else "partial",
                "reasoning_result": "diverged" if ordinal % 2 else "partial",
                "confidence": ("low", "medium", "high")[ordinal % 3],
                "first_break": first_break,
                "first_break_provenance": "user_report",
                "first_action": first_action,
                "mechanism_key": f"KP:{point_id}",
                "knowledge_point": point_id,
                "presentation_kind": "single_choice",
                "source_id": f"PERF-SOURCE-{ordinal:06d}",
            }
        )
        mappings["events"][event_id] = {
            "observed_date": observed.isoformat(),
            "point_ids": [point_id],
            "mechanism_ids": [f"KP:{point_id}"],
            "question_node_ids": [question_by_point[point_id]],
        }
    return build_recent_signature_index(events, mappings, as_of)


def _build_cross(point_map: dict[str, list[str]]) -> dict[str, Any]:
    left = point_map["CO"][0]
    right = point_map["OS"][0]
    edge_id = "CSR408-PERF-001"
    record = {
        "id": edge_id,
        "edge_id": edge_id,
        "source_point_id": left,
        "target_point_id": right,
        "left": {"id": left, "subject": "CO", "name": "合成中断入口"},
        "right": {"id": right, "subject": "OS", "name": "合成中断处理"},
        "subjects": ["CO", "OS"],
        "relation_code": "R06",
        "relation": {"code": "R06", "strength": "中"},
        "confidence": "high",
        "activation": "verified_live",
        "lifecycle": "verified_existing",
        "activation_eligible": True,
        "enabled_for_live_routing": True,
    }
    return {
        "schema_version": "cross_subject_shadow_v1",
        "mode": "read_only_shadow_generation",
        "formal_write_count": 0,
        "records": [record],
        "routing_adjacency": {
            left: [
                {
                    "edge_id": edge_id,
                    "target_point_id": right,
                    "activation": "verified_live",
                    "activation_eligible": True,
                    "enabled_for_live_routing": True,
                    "relation_code": "R06",
                }
            ]
        },
    }


def _corpus_item(
    *,
    formal_id: str,
    point_id: str,
    study_date: str,
    ordinal: int,
) -> dict[str, Any]:
    completion_sha = hashlib.sha256(
        f"completion:{formal_id}:{study_date}:{ordinal}".encode("utf-8")
    ).hexdigest()
    return {
        "schema": "personalization_corpus_v1",
        "kind": "nightly_curation_corpus_item",
        "answer_safe": True,
        "formal_write_count": 0,
        "formal_id": formal_id,
        "study_date": study_date,
        "outcome": "curated",
        "point_ids": [point_id],
        "source_binding": {
            "completion_evidence_sha256": completion_sha,
            "completion_entity": {
                "entity_kind": "normal_receipt",
                "entity_sha256": completion_sha,
                "formal_id": formal_id,
            },
        },
        "canonical_outcome_binding": {
            "status": "exact_ref",
            "outcome": {
                "event_id": f"RE-PERF-CORPUS-{ordinal:06d}",
                "source": "daily_practice",
                "first_result": "wrong" if ordinal % 3 else "partial",
                "choice_result": "incorrect" if ordinal % 2 else "partial",
                "reasoning_result": "diverged" if ordinal % 2 else "partial",
                "choice_result_evidence_field_present": True,
                "reasoning_result_evidence_field_present": True,
                "confidence": ("low", "medium", "high")[ordinal % 3],
                "evidence_kind": "failure",
                "fragile_mastery": False,
                "stable_retention": False,
            },
        },
        "observed_facts": {
            "error_entry": {
                "value": "未先区分对象和状态变化",
                "provenance": "user_report",
            },
            "first_action": "先标出对象、执行主体和状态边界",
        },
        "formal_snapshot": {
            "safe_summary": f"{point_id} 的答案安全机制骨架",
            "key_parameters": ["对象", "前置条件", "状态变化"],
            "question_type": "机制边界判断",
            "actual_mechanism": "对象边界与状态变化",
            "core_point": point_id,
            "fuzzy_concepts": ["执行主体", "因果方向"],
            "error_tags": ["boundary_conflation"],
            "redo_first_action": "先区分对象再追踪状态变化",
        },
    }


def _build_corpus(
    state_dir: Path,
    point_map: dict[str, list[str]],
    questions: dict[str, dict[str, Any]],
    *,
    as_of: str,
    history_days: int,
) -> tuple[str, str, dict[str, Any]]:
    corpus_dir = state_dir / "corpus"
    objects_dir = corpus_dir / "objects"
    objects_dir.mkdir(parents=True, exist_ok=True)
    current = date.fromisoformat(as_of)
    first_question_by_point: dict[str, str] = {}
    for question in questions.values():
        first_question_by_point.setdefault(question["point_ids"][0], question["node_id"])
    target_point = point_map["CO"][0]
    rotating = {subject: point_map[subject] for subject in SUBJECTS}
    by_formal: dict[str, list[dict[str, Any]]] = {}
    by_point: dict[str, list[dict[str, Any]]] = {}
    by_date: dict[str, list[dict[str, Any]]] = {}
    artifact_hashes: dict[str, str] = {}
    ordinal = 0
    for age in range(history_days):
        study_date = (current - timedelta(days=age)).isoformat()
        day_points = [
            target_point,
            rotating["DS"][age % len(rotating["DS"])],
            rotating["OS"][age % len(rotating["OS"])],
            rotating["CN"][age % len(rotating["CN"])],
        ]
        for point_id in day_points:
            ordinal += 1
            formal_id = first_question_by_point[point_id]
            item = _corpus_item(
                formal_id=formal_id,
                point_id=point_id,
                study_date=study_date,
                ordinal=ordinal,
            )
            raw = _json_bytes(item, pretty=True)
            digest = _sha256_bytes(raw)
            relative = f"objects/{digest}.json"
            (corpus_dir / relative).write_bytes(raw)
            artifact_hashes[relative] = digest
            entry = {
                "object_ref": relative,
                "object_sha256": digest,
                "study_date": study_date,
                "formal_id": formal_id,
                "point_ids": [point_id],
            }
            by_formal.setdefault(formal_id, []).append(entry)
            by_point.setdefault(point_id, []).append(entry)
            by_date.setdefault(study_date, []).append(entry)
    index = {
        "schema": "personalization_corpus_v1",
        "kind": "nightly_curation_corpus_index",
        "by_formal_id": by_formal,
        "by_point_id": by_point,
        "by_study_date": by_date,
    }
    index_raw = _write_json(corpus_dir / "index.json", index)
    index_sha = _sha256_bytes(index_raw)
    artifact_hashes["index.json"] = index_sha
    manifest = {
        "schema": "personalization_corpus_v1",
        "kind": "nightly_curation_corpus_manifest",
        "answer_safe": True,
        "formal_write_count": 0,
        "build_id": "PERF-CORPUS-" + index_sha[:20].upper(),
        "artifact_hashes": artifact_hashes,
    }
    manifest_raw = _write_json(corpus_dir / "manifest.json", manifest)
    return _sha256_bytes(manifest_raw), index_sha, {
        "object_count": ordinal,
        "index_entry_count": sum(len(values) for values in by_date.values()),
        "target_history_entry_count": len(by_point[target_point]),
    }


def _build_projection(
    repo: Path,
    *,
    as_of: str,
    history_days: int,
    points_per_subject: int,
    questions_per_point: int,
) -> dict[str, Any]:
    state_dir = repo / "state" / "personalization"
    state_dir.mkdir(parents=True, exist_ok=True)
    point_map = _point_ids(points_per_subject)
    catalog = _build_catalog(point_map)
    question_index, questions = _build_questions(
        point_map, as_of=as_of, questions_per_point=questions_per_point
    )
    learner = _build_learner(point_map, questions, as_of=as_of)
    signatures = _build_signatures(point_map, questions, as_of=as_of)
    cross = _build_cross(point_map)
    variant_catalog = {
        "schema": "variant_source_catalog_v1",
        "kind": "verified_variant_source_catalog",
        "formal_write_count": 0,
        "entry_count": 0,
        "entries": [],
        "by_anchor": {},
    }
    artifacts = {
        "catalog.json": catalog,
        "learner_state.json": learner,
        "question_index.json": question_index,
        "failure_signatures.json": signatures,
        "variant-catalog.json": variant_catalog,
    }
    artifact_hashes: dict[str, str] = {}
    for name, value in artifacts.items():
        raw = _write_json(state_dir / name, value)
        artifact_hashes[name] = _sha256_bytes(raw)
    cross_raw = _write_json(state_dir / "cross-subject-shadow.json", cross)
    cross_sha = _sha256_bytes(cross_raw)
    corpus_manifest_sha, corpus_index_sha, corpus_stats = _build_corpus(
        state_dir,
        point_map,
        questions,
        as_of=as_of,
        history_days=history_days,
    )
    manifest_without_build_id = {
        "schema": "personalization_index_v1",
        "kind": "personalization_manifest",
        "as_of": as_of,
        "answer_safe": True,
        "formal_write_count": 0,
        "artifact_hashes": artifact_hashes,
        "cross_subject_shadow_sha256": cross_sha,
        "variant_catalog_sha256": artifact_hashes["variant-catalog.json"],
        "corpus_manifest_sha256": corpus_manifest_sha,
        "corpus_index_sha256": corpus_index_sha,
    }
    build_id = "PERF-" + _sha256_bytes(_json_bytes(manifest_without_build_id))[:24].upper()
    manifest = {**manifest_without_build_id, "build_id": build_id}
    manifest_raw = _write_json(state_dir / "manifest.json", manifest)

    formal_dir = repo / "formal-sentinels"
    formal_dir.mkdir(parents=True, exist_ok=True)
    formal_paths = [formal_dir / "节点总表.md", formal_dir / "关系边表.md"]
    formal_paths[0].write_text("临时正式节点哨兵，不允许改写。\n", encoding="utf-8")
    formal_paths[1].write_text("临时正式关系哨兵，不允许改写。\n", encoding="utf-8")
    return {
        "repo": repo,
        "state_dir": state_dir,
        "as_of": as_of,
        "history_days": history_days,
        "history_start": (date.fromisoformat(as_of) - timedelta(days=history_days - 1)).isoformat(),
        "history_end": as_of,
        "point_map": point_map,
        "target_point": point_map["CO"][0],
        "target_node": next(
            question["node_id"]
            for question in questions.values()
            if question["point_ids"] == [point_map["CO"][0]]
        ),
        "point_count": sum(len(values) for values in point_map.values()),
        "question_count": len(questions),
        "recent_signature_count": signatures["signature_count"],
        "corpus": corpus_stats,
        "build_id": build_id,
        "root_manifest_sha256": _sha256_bytes(manifest_raw),
        "formal_paths": formal_paths,
    }


def _envelope(
    fixture: dict[str, Any], *, request_id: str, conversation_kind: str
) -> dict[str, Any]:
    point_id = fixture["target_point"]
    return {
        "schema": "personalization_target_envelope_v1",
        "request_id": request_id,
        "conversation_kind": conversation_kind,
        "as_of": fixture["as_of"],
        "subject": "CO",
        "point_ids": [point_id],
        "mechanism_ids": [f"KP:{point_id}"],
        "mechanism_primitive_ids": ["interrupt_hardware_software_handoff"],
        "formal_node_ids": [fixture["target_node"]],
        "module_ids": [point_id[:4]],
        "safe_topic_terms": [],
        "error_pattern_ids": ["boundary_conflation"],
        "method_ids": ["partition_phases_then_assign_actor"],
        "answer_protection": True,
    }


REVIEW_HEADERS = [
    "复习单元ID",
    "科目",
    "模块",
    "内容名称",
    "类型",
    "调度状态",
    "覆盖节点",
    "首次学习日期",
    "上次复习日期",
    "下次复习日期",
    "当前间隔天数",
    "掌握度",
    "错误次数",
    "重要程度",
    "难度等级",
    "闭卷问题",
    "派生自查口径",
    "备注",
]
MAPPING_HEADERS = [
    "正式节点ID",
    "科目",
    "证据等级",
    "实际考点",
    "主复习单元ID",
    "次复习单元ID",
    "覆盖方式",
    "复核状态",
    "原题轨",
    "原题轨资格",
    "待补事项",
]
TRACK_HEADERS = [
    "正式节点ID",
    "主复习单元ID",
    "原题轨状态",
    "7天资格",
    "最近复做日期",
    "最早允许日期",
    "下次原题复做日期",
    "来源ID",
    "年份",
    "定位标记",
    "段落号",
    "完整来源位置",
    "详情入口",
    "激活条件",
    "待补事项",
]


def _markdown_table(headers: list[str], rows: list[list[str]]) -> str:
    return (
        "|" + "|".join(headers) + "|\n"
        + "|" + "|".join("---" for _ in headers) + "|\n"
        + "".join("|" + "|".join(row) + "|\n" for row in rows)
    )


def _morning_queue(*, as_of: str, item_count: int) -> str:
    sections = []
    for ordinal in range(item_count):
        item_id = f"MQ-PERF-{ordinal:04d}"
        sections.append(
            "\n".join(
                (
                    f"#### {item_id} 合成对象与状态边界",
                    "",
                    "- source_id: `RU_PERF`",
                    "- item_kind: formal_due",
                    "- prompt: 闭卷说明这个机制的第一动作与状态边界。",
                )
            )
        )
    return (
        "---\n"
        "schema: morning_review_action_queue_v3\n"
        f"review_date: {as_of}\n"
        "status: ready\n"
        "---\n\n"
        + "\n\n".join(sections)
        + "\n"
    )


def _write_learning_inputs(
    fixture: dict[str, Any], *, morning_item_count: int
) -> dict[str, Any]:
    repo = fixture["repo"]
    as_of = fixture["as_of"]
    review_unit_id = "RU_PERF"
    formal_node_id = "CO_2024_001"
    point_id = fixture["target_point"]
    review_table = repo / "复习单元总表.md"
    mapping_table = repo / "复习单元节点映射.md"
    track_table = repo / "原题复做轨总表.md"
    review_table.write_text(
        _markdown_table(
            REVIEW_HEADERS,
            [
                [
                    review_unit_id,
                    "计算机组成原理",
                    point_id[:4],
                    f"合成考点 {point_id}",
                    "错题机制",
                    "active_due",
                    formal_node_id,
                    "2026-06-01",
                    "2026-07-01",
                    as_of,
                    "22",
                    "待评分",
                    "2",
                    "高",
                    "中",
                    "闭卷说明第一动作与状态边界。",
                    "核验对象、执行主体和状态变化的边界。",
                    "synthetic benchmark only",
                ]
            ],
        ),
        encoding="utf-8",
    )
    mapping_table.write_text(
        _markdown_table(
            MAPPING_HEADERS,
            [
                [
                    formal_node_id,
                    "计算机组成原理",
                    "A",
                    point_id,
                    review_unit_id,
                    "无",
                    "核心机制",
                    "reviewed",
                    "有",
                    "eligible",
                    "无",
                ]
            ],
        ),
        encoding="utf-8",
    )
    track_table.write_text(
        _markdown_table(
            TRACK_HEADERS,
            [
                [
                    formal_node_id,
                    review_unit_id,
                    "active_due",
                    "eligible",
                    "2026-07-01",
                    "2026-07-08",
                    as_of,
                    "EXAM408-2024-Q20",
                    "2024",
                    "2.1（20）",
                    "段落20",
                    "synthetic safe source",
                    "obsidian://open?vault=synthetic&file=CO_2024_001",
                    "主机制已达标",
                    "无",
                ]
            ],
        ),
        encoding="utf-8",
    )
    review_ledger = repo / review_canonical.LOOP_REL / "events.jsonl"
    review_ledger.parent.mkdir(parents=True, exist_ok=True)
    review_ledger.write_text("", encoding="utf-8")
    queue_path = repo / "synthetic-morning-queue.md"
    queue_path.write_text(
        _morning_queue(as_of=as_of, item_count=morning_item_count),
        encoding="utf-8",
    )
    return {
        "review_unit_id": review_unit_id,
        "formal_node_id": formal_node_id,
        "point_id": point_id,
        "queue_path": queue_path,
        "review_ledger": review_ledger,
        "formal_paths": [mapping_table, review_table, track_table],
        "morning_item_ids": [
            f"MQ-PERF-{ordinal:04d}" for ordinal in range(morning_item_count)
        ],
    }


def _bootstrap_learning_runtime(
    fixture: dict[str, Any], learning: dict[str, Any]
) -> dict[str, Any]:
    repo = fixture["repo"]
    session_id = f"MR-{fixture['as_of']}-performance"
    started = morning_module.command_start(
        argparse.Namespace(
            repo=str(repo),
            queue=str(learning["queue_path"]),
            session_id=session_id,
            item=None,
        )
    )
    review_build = review_hot_module.cold_rebuild(repo)
    formal_build = formal_hot_module.build_projection(
        repo, as_of=fixture["as_of"]
    )
    session_dir = repo / morning_module.STATE_REL / session_id
    session_build = morning_hot_module.cold_bootstrap(session_dir)
    if (
        started.get("status") != "in_progress"
        or review_build.get("status") != "rebuilt"
        or formal_build.get("status") != "built"
        or session_build.get("status") != "ready"
    ):
        raise BenchmarkError("managed_learning_runtime_bootstrap_failed")
    return {
        "session_id": session_id,
        "session_dir": session_dir,
        "session_ledger": session_dir / "events.jsonl",
        "start": started,
        "review_build": review_build,
        "formal_build": formal_build,
        "session_build": session_build,
    }


def _sha_is_valid(value: Any) -> bool:
    return isinstance(value, str) and re.fullmatch(r"[0-9a-f]{64}", value) is not None


@contextmanager
def _forbid_broad_learning_hot_paths(
    formal_paths: list[Path],
) -> Iterator[dict[str, int]]:
    """Turn any broad recovery read in a measured managed call into a failure."""

    counters = {
        "review_ledger_full_scan_attempt_count": 0,
        "capture_ledger_full_scan_attempt_count": 0,
        "session_ledger_full_replay_attempt_count": 0,
        "formal_markdown_access_attempt_count": 0,
        "legacy_reconcile_attempt_count": 0,
    }
    originals: list[tuple[Any, str, Any]] = []

    def block(owner: Any, name: str, counter: str) -> None:
        original = getattr(owner, name)

        def rejected(*_args: Any, **_kwargs: Any) -> Any:
            counters[counter] += 1
            raise BenchmarkError(f"forbidden_managed_hot_path:{name}")

        originals.append((owner, name, original))
        setattr(owner, name, rejected)

    block(review_canonical, "_read_events", "review_ledger_full_scan_attempt_count")
    block(review_canonical, "mapped_nodes", "formal_markdown_access_attempt_count")
    block(review_canonical, "_original_actions", "formal_markdown_access_attempt_count")
    block(morning_module, "_read_events", "session_ledger_full_replay_attempt_count")
    block(morning_module, "sync_session", "legacy_reconcile_attempt_count")
    block(morning_module, "audit_review_loop", "review_ledger_full_scan_attempt_count")
    block(morning_module, "audit_fact_capture", "capture_ledger_full_scan_attempt_count")
    block(morning_module, "capture_review_event", "capture_ledger_full_scan_attempt_count")

    protected = {path.resolve() for path in formal_paths}
    original_open = Path.open

    def guarded_open(path: Path, *args: Any, **kwargs: Any) -> Any:
        if path.resolve() in protected:
            counters["formal_markdown_access_attempt_count"] += 1
            raise BenchmarkError("forbidden_managed_hot_path:formal_markdown")
        return original_open(path, *args, **kwargs)

    Path.open = guarded_open
    try:
        yield counters
    finally:
        Path.open = original_open
        for owner, name, original in reversed(originals):
            setattr(owner, name, original)


def _study_package(
    fixture: dict[str, Any], learning: dict[str, Any], ordinal: int
) -> dict[str, Any]:
    return {
        "schema": study_turn_module.PACKAGE_SCHEMA,
        "source": "daily_practice",
        "session_id": f"DP-{fixture['as_of']}-PERF",
        "item_id": f"Q-PERF-{ordinal:06d}",
        "source_id": fixture["target_node"],
        "mechanism_key": f"KP:{learning['point_id']}",
        "knowledge_point": learning["point_id"],
        "first_result": "independent_correct",
        "choice_result": "correct",
        "reasoning_result": "sound",
        "confidence": "medium",
        "prompt_level": "none",
        "first_break": "none",
        "first_break_provenance": "derived_classification",
        "event_time": f"{fixture['as_of']}T10:00:00+08:00",
        "observed_date": fixture["as_of"],
        "idempotency_key": f"performance:daily:{fixture['as_of']}:{ordinal:06d}:first",
        "formal_node_id": fixture["target_node"],
        "review_unit_id": learning["review_unit_id"],
        "presentation_kind": "original_question",
    }


def _morning_args(
    fixture: dict[str, Any], runtime: dict[str, Any], item_id: str
) -> argparse.Namespace:
    return argparse.Namespace(
        repo=str(fixture["repo"]),
        session=runtime["session_id"],
        item=item_id,
        result="independent_correct",
        choice_result="correct",
        reasoning_result="sound",
        confidence="medium",
        response_seconds=12.0,
        prompt_level="none",
        first_break="none",
        first_break_provenance="derived_classification",
        feedback_type=None,
        personalization_mode="verified",
        personalization_state_dir=str(fixture["state_dir"]),
        personalization_max_bytes=MAX_SLICE_BYTES,
    )


@contextmanager
def _capture_preanswer_reads(state_dir: Path) -> Iterator[list[str]]:
    observed: list[str] = []
    original = preanswer_module._read_json_checked

    def tracked(path: Path, expected_hash: str | None = None) -> dict[str, Any]:
        resolved = path.resolve()
        try:
            relative = resolved.relative_to(state_dir.resolve()).as_posix()
        except ValueError:
            relative = f"OUTSIDE:{resolved}"
        observed.append(relative)
        return original(path, expected_hash)

    preanswer_module._read_json_checked = tracked
    try:
        yield observed
    finally:
        preanswer_module._read_json_checked = original


def _validate_parameters(
    *,
    as_of: str,
    history_days: int,
    points_per_subject: int,
    questions_per_point: int,
    preanswer_iterations: int,
    preanswer_warmup: int,
    managed_iterations: int,
    managed_warmup: int,
    study_turn_iterations: int,
    study_turn_warmup: int,
    morning_answer_turn_iterations: int,
    morning_answer_turn_warmup: int,
    interactive_iterations: int,
    interactive_warmup: int,
) -> None:
    try:
        date.fromisoformat(as_of)
    except ValueError as exc:
        raise BenchmarkError("invalid_as_of") from exc
    if history_days < MIN_HISTORY_DAYS:
        raise BenchmarkError("history_days_below_100")
    if points_per_subject < MIN_POINTS_PER_SUBJECT:
        raise BenchmarkError("projection_scale_too_small")
    if questions_per_point < MIN_QUESTIONS_PER_POINT:
        raise BenchmarkError("projection_scale_too_small")
    if preanswer_iterations < MIN_PREANSWER_ITERATIONS:
        raise BenchmarkError("preanswer_sample_too_small")
    if managed_iterations < MIN_MANAGED_ITERATIONS:
        raise BenchmarkError("managed_sample_must_cover_all_entry_routes")
    if study_turn_iterations < MIN_STUDY_TURN_ITERATIONS:
        raise BenchmarkError("managed_study_turn_sample_too_small")
    if morning_answer_turn_iterations < MIN_MORNING_ANSWER_TURN_ITERATIONS:
        raise BenchmarkError("managed_morning_answer_turn_sample_too_small")
    if interactive_iterations < MIN_INTERACTIVE_ITERATIONS:
        raise BenchmarkError("interactive_hotpath_sample_too_small")
    if (
        preanswer_warmup < 0
        or managed_warmup < 0
        or study_turn_warmup < 0
        or morning_answer_turn_warmup < 0
        or interactive_warmup < 0
    ):
        raise BenchmarkError("invalid_warmup")


def run_benchmark(
    *,
    as_of: str = AS_OF_DEFAULT,
    history_days: int = 120,
    points_per_subject: int = 48,
    questions_per_point: int = 4,
    preanswer_iterations: int = 60,
    preanswer_warmup: int = 10,
    managed_iterations: int = 20,
    managed_warmup: int = 2,
    study_turn_iterations: int = 20,
    study_turn_warmup: int = 2,
    morning_answer_turn_iterations: int = 20,
    morning_answer_turn_warmup: int = 2,
    interactive_iterations: int = 20,
    interactive_warmup: int = 1,
) -> dict[str, Any]:
    """Run the full temp-only benchmark and return a machine report."""

    raise BenchmarkError(
        "legacy_personalization_benchmark_retired_use_current_question_hotpath_v1"
    )

    _validate_parameters(
        as_of=as_of,
        history_days=history_days,
        points_per_subject=points_per_subject,
        questions_per_point=questions_per_point,
        preanswer_iterations=preanswer_iterations,
        preanswer_warmup=preanswer_warmup,
        managed_iterations=managed_iterations,
        managed_warmup=managed_warmup,
        study_turn_iterations=study_turn_iterations,
        study_turn_warmup=study_turn_warmup,
        morning_answer_turn_iterations=morning_answer_turn_iterations,
        morning_answer_turn_warmup=morning_answer_turn_warmup,
        interactive_iterations=interactive_iterations,
        interactive_warmup=interactive_warmup,
    )
    temporary_path: Path | None = None
    report: dict[str, Any]
    with tempfile.TemporaryDirectory(prefix="kaoyan408-personalization-perf-") as temporary:
        temporary_path = Path(temporary)
        fixture = _build_projection(
            temporary_path / "repo",
            as_of=as_of,
            history_days=history_days,
            points_per_subject=points_per_subject,
            questions_per_point=questions_per_point,
        )
        repo = fixture["repo"]
        state_dir = fixture["state_dir"]
        learning = _write_learning_inputs(
            fixture,
            morning_item_count=(
                morning_answer_turn_warmup + morning_answer_turn_iterations
            ),
        )
        all_formal_paths = [*fixture["formal_paths"], *learning["formal_paths"]]
        formal_before = {
            path.relative_to(repo).as_posix(): _sha256_file(path)
            for path in all_formal_paths
        }
        projection_before, projection_file_count = _tree_digest(
            state_dir,
            exclude_prefix="turn-receipts",
            exclude_paths=(".hot-path.lock",),
        )
        learning_runtime = _bootstrap_learning_runtime(fixture, learning)

        read_probe = _envelope(
            fixture,
            request_id="PERF-READ-PROBE",
            conversation_kind="current_question",
        )
        with _capture_preanswer_reads(state_dir) as observed_reads:
            probe_result = safe_preanswer_slice(
                repo,
                target_envelope=read_probe,
                state_dir=state_dir,
                max_bytes=MAX_SLICE_BYTES,
                mode="verified",
            )
        unique_reads = sorted(set(observed_reads))
        forbidden_reads = [
            item
            for item in unique_reads
            if item.startswith("OUTSIDE:")
            or item.endswith("events.jsonl")
            or "capture" in item.lower()
            or item.endswith("节点总表.md")
            or item.endswith("关系边表.md")
        ]

        for ordinal in range(preanswer_warmup):
            safe_preanswer_slice(
                repo,
                target_envelope=_envelope(
                    fixture,
                    request_id=f"PERF-PRE-WARM-{ordinal:04d}",
                    conversation_kind="current_question",
                ),
                state_dir=state_dir,
                max_bytes=MAX_SLICE_BYTES,
                mode="verified",
            )

        preanswer_times: list[float] = []
        preanswer_sizes: list[int] = []
        preanswer_results: list[dict[str, Any]] = []
        for ordinal in range(preanswer_iterations):
            envelope = _envelope(
                fixture,
                request_id=f"PERF-PRE-{ordinal:06d}",
                conversation_kind="current_question",
            )
            start = time.perf_counter_ns()
            result = safe_preanswer_slice(
                repo,
                target_envelope=envelope,
                state_dir=state_dir,
                max_bytes=MAX_SLICE_BYTES,
                mode="verified",
            )
            elapsed_ms = (time.perf_counter_ns() - start) / 1_000_000
            preanswer_times.append(elapsed_ms)
            preanswer_sizes.append(len(preanswer_module._json_bytes(result)))
            preanswer_results.append(result)

        preanswer_timing = _timing_summary(preanswer_times)
        preanswer_checks = {
            "all_ready": all(row.get("status") == "ready" for row in preanswer_results),
            "all_read_only": all(row.get("read_only") is True for row in preanswer_results),
            "all_formal_write_count_zero": all(
                row.get("formal_write_count") == 0 for row in preanswer_results
            ),
            "all_scheduling_authority_none": all(
                row.get("scheduling_authority") == "none" for row in preanswer_results
            ),
            "all_within_byte_budget": all(size <= MAX_SLICE_BYTES for size in preanswer_sizes),
            "read_probe_ready": probe_result.get("status") == "ready",
            "forbidden_hot_read_count": len(forbidden_reads),
        }
        preanswer_pass = (
            all(
                value
                for key, value in preanswer_checks.items()
                if key != "forbidden_hot_read_count"
            )
            and preanswer_checks["forbidden_hot_read_count"] == 0
            and preanswer_timing["p95_ms"] <= PREANSWER_P95_LIMIT_MS
        )

        # The interactive SLO is a steady-state gate, separate from the legacy
        # broad benchmark above.  Use its own explicit warmup and sample so
        # fixture construction and first JSON/page-cache faults cannot be
        # mislabeled as learner-turn latency.
        interactive_preanswer_warmup = max(5, interactive_warmup)
        for ordinal in range(interactive_preanswer_warmup):
            safe_preanswer_slice(
                repo,
                target_envelope=_envelope(
                    fixture,
                    request_id=f"INTERACTIVE-PRE-WARM-{ordinal:04d}",
                    conversation_kind="current_question",
                ),
                state_dir=state_dir,
                max_bytes=MAX_SLICE_BYTES,
                mode="verified",
            )
        interactive_preanswer_times: list[float] = []
        interactive_preanswer_results: list[dict[str, Any]] = []
        for ordinal in range(interactive_iterations):
            envelope = _envelope(
                fixture,
                request_id=f"INTERACTIVE-PRE-RUN-{ordinal:06d}",
                conversation_kind="current_question",
            )
            started = time.perf_counter_ns()
            result = safe_preanswer_slice(
                repo,
                target_envelope=envelope,
                state_dir=state_dir,
                max_bytes=MAX_SLICE_BYTES,
                mode="verified",
            )
            interactive_preanswer_times.append(
                (time.perf_counter_ns() - started) / 1_000_000
            )
            interactive_preanswer_results.append(result)
        interactive_preanswer_timing = _timing_summary(
            interactive_preanswer_times
        )

        receipt_cli = Path(__file__).with_name(
            "personalization_turn_receipt_408.py"
        )

        def close_interactive_begin(
            begin: dict[str, Any], *, marker: str
        ) -> None:
            finalized = finalize_personalized_reply(
                repo,
                turn_receipt_sha256=str(begin["turn_receipt_sha256"]),
                reply_body=f"INTERACTIVE-V2-{marker}",
                state_dir=state_dir,
            )
            if finalized.get("status") != "finalized":
                raise BenchmarkError("interactive_finalize_failed")

        def v2_begin_checks(begin: dict[str, Any]) -> dict[str, bool]:
            receipt_ref = begin.get("turn_receipt_ref")
            if not isinstance(receipt_ref, str):
                raise BenchmarkError("interactive_begin_receipt_ref_missing")
            receipt = json.loads((state_dir / receipt_ref).read_text(encoding="utf-8"))
            slice_ref = receipt.get("slice_object_ref")
            slice_sha = receipt.get("slice_object_sha256")
            detached_ok = (
                receipt.get("schema") == BEGIN_SCHEMA_V2
                and "slice_snapshot" not in receipt
                and "response_plan" not in receipt
                and isinstance(slice_ref, str)
                and isinstance(slice_sha, str)
            )
            slice_hash_ok = False
            if isinstance(slice_ref, str) and isinstance(slice_sha, str):
                slice_path = state_dir / slice_ref
                slice_hash_ok = (
                    slice_path.is_file()
                    and _sha256_file(slice_path) == slice_sha
                )
            return {
                "v2_detached_slice": detached_ok,
                "slice_object_hash_bound": slice_hash_ok,
                "formal_write_count_zero": begin.get("formal_write_count") == 0,
                "compact_result": len(_json_bytes(begin))
                <= PUBLIC_BEGIN_MAX_BYTES,
            }

        def interactive_in_process_once(
            ordinal: int, *, warmup: bool
        ) -> tuple[float, dict[str, Any]]:
            marker = "WARM" if warmup else "RUN"
            envelope = _envelope(
                fixture,
                request_id=f"INTERACTIVE-INPROC-{marker}-{ordinal:06d}",
                conversation_kind="current_question",
            )
            started = time.perf_counter_ns()
            begin = begin_personalized_turn(
                repo,
                entry_kind="current_question",
                target_envelope=envelope,
                state_dir=state_dir,
                max_bytes=MAX_SLICE_BYTES,
                mode="verified",
            )
            elapsed_ms = (time.perf_counter_ns() - started) / 1_000_000
            checks = v2_begin_checks(begin)
            close_interactive_begin(begin, marker=f"INPROC-{marker}-{ordinal}")
            return elapsed_ms, {
                "retrieval_status": begin.get("retrieval_status"),
                "preanswer_call_count": begin.get("preanswer_call_count"),
                "receipt_schema": begin.get("receipt_schema"),
                "output_bytes": len(_json_bytes(begin)),
                "checks": checks,
            }

        for ordinal in range(interactive_warmup):
            interactive_in_process_once(ordinal, warmup=True)
        interactive_in_process_times: list[float] = []
        interactive_in_process_rows: list[dict[str, Any]] = []
        for ordinal in range(interactive_iterations):
            elapsed_ms, row = interactive_in_process_once(
                ordinal, warmup=False
            )
            interactive_in_process_times.append(elapsed_ms)
            interactive_in_process_rows.append(row)
        interactive_in_process_timing = _timing_summary(
            interactive_in_process_times
        )

        def interactive_cli_once(
            ordinal: int, *, warmup: bool
        ) -> tuple[float, dict[str, Any], dict[str, Any]]:
            marker = "WARM" if warmup else "RUN"
            envelope = _envelope(
                fixture,
                request_id=f"INTERACTIVE-CLI-{marker}-{ordinal:06d}",
                conversation_kind="current_question",
            )
            command = [
                sys.executable,
                str(receipt_cli),
                "--repo",
                str(repo),
                "--state-dir",
                str(state_dir),
                "begin-turn",
                "--entry-kind",
                "current_question",
                "--target-envelope",
                json.dumps(
                    envelope,
                    ensure_ascii=False,
                    separators=(",", ":"),
                    sort_keys=True,
                ),
                "--mode",
                "verified",
            ]
            environment = dict(os.environ)
            environment["PYTHONDONTWRITEBYTECODE"] = "1"
            started = time.perf_counter_ns()
            completed = subprocess.run(
                command,
                cwd=repo,
                env=environment,
                check=False,
                capture_output=True,
            )
            elapsed_ms = (time.perf_counter_ns() - started) / 1_000_000
            if completed.returncode != 0:
                raise BenchmarkError(
                    "interactive_cli_failed:"
                    + completed.stderr.decode("utf-8", errors="replace")[:200]
                )
            try:
                begin = json.loads(completed.stdout)
            except json.JSONDecodeError as exc:
                raise BenchmarkError("interactive_cli_json_invalid") from exc
            if not isinstance(begin, dict):
                raise BenchmarkError("interactive_cli_json_invalid")
            checks = v2_begin_checks(begin)
            checks["single_line_compact_stdout"] = completed.stdout.count(b"\n") == 1
            close_interactive_begin(begin, marker=f"CLI-{marker}-{ordinal}")
            return elapsed_ms, begin, checks

        for ordinal in range(interactive_warmup):
            interactive_cli_once(ordinal, warmup=True)
        interactive_cli_times: list[float] = []
        interactive_cli_rows: list[dict[str, Any]] = []
        for ordinal in range(interactive_iterations):
            elapsed_ms, begin, checks = interactive_cli_once(
                ordinal, warmup=False
            )
            interactive_cli_times.append(elapsed_ms)
            interactive_cli_rows.append(
                {
                    "retrieval_status": begin.get("retrieval_status"),
                    "preanswer_call_count": begin.get("preanswer_call_count"),
                    "receipt_schema": begin.get("receipt_schema"),
                    "stdout_bytes": len(_json_bytes(begin)),
                    "checks": checks,
                }
            )
        interactive_cli_timing = _timing_summary(interactive_cli_times)

        stale_as_of = (
            date.fromisoformat(as_of) + timedelta(days=1)
        ).isoformat()
        stale_envelope = _envelope(
            fixture,
            request_id="INTERACTIVE-STALE-INPROC",
            conversation_kind="current_question",
        )
        stale_envelope["as_of"] = stale_as_of
        stale_begin = begin_personalized_turn(
            repo,
            entry_kind="current_question",
            target_envelope=stale_envelope,
            state_dir=state_dir,
            max_bytes=MAX_SLICE_BYTES,
            mode="verified",
        )
        close_interactive_begin(stale_begin, marker="STALE-INPROC")
        stale_cli_envelope = dict(stale_envelope)
        stale_cli_envelope["request_id"] = "INTERACTIVE-STALE-CLI"
        stale_command = [
            sys.executable,
            str(receipt_cli),
            "--repo",
            str(repo),
            "--state-dir",
            str(state_dir),
            "begin-turn",
            "--entry-kind",
            "current_question",
            "--target-envelope",
            json.dumps(
                stale_cli_envelope,
                ensure_ascii=False,
                separators=(",", ":"),
                sort_keys=True,
            ),
        ]
        stale_environment = dict(os.environ)
        stale_environment["PYTHONDONTWRITEBYTECODE"] = "1"
        stale_cli_completed = subprocess.run(
            stale_command,
            cwd=repo,
            env=stale_environment,
            check=False,
            capture_output=True,
        )
        if stale_cli_completed.returncode != 0:
            raise BenchmarkError("interactive_stale_cli_failed")
        stale_cli_begin = json.loads(stale_cli_completed.stdout)
        close_interactive_begin(stale_cli_begin, marker="STALE-CLI")

        interactive_in_process_checks = {
            "all_ready": all(
                row["retrieval_status"] == "ready"
                for row in interactive_in_process_rows
            ),
            "all_preanswer_once": all(
                row["preanswer_call_count"] == 1
                for row in interactive_in_process_rows
            ),
            "all_v2": all(
                row["receipt_schema"] == BEGIN_SCHEMA_V2
                for row in interactive_in_process_rows
            ),
            "all_detached_and_hash_bound": all(
                all(row["checks"].values())
                for row in interactive_in_process_rows
            ),
        }
        interactive_cli_checks = {
            "all_ready": all(
                row["retrieval_status"] == "ready"
                for row in interactive_cli_rows
            ),
            "all_preanswer_once": all(
                row["preanswer_call_count"] == 1
                for row in interactive_cli_rows
            ),
            "all_v2": all(
                row["receipt_schema"] == BEGIN_SCHEMA_V2
                for row in interactive_cli_rows
            ),
            "all_detached_hash_bound_compact_single_line": all(
                all(row["checks"].values()) for row in interactive_cli_rows
            ),
        }
        stale_checks = {
            "in_process_degraded": stale_begin.get("retrieval_status")
            == "degraded",
            "in_process_reason_stale": (
                stale_begin.get("slice") or {}
            ).get("reason_code")
            == "stale_projection",
            "cli_degraded": stale_cli_begin.get("retrieval_status")
            == "degraded",
            "cli_reason_stale": (
                stale_cli_begin.get("slice") or {}
            ).get("reason_code")
            == "stale_projection",
            "excluded_from_ready_timings": True,
        }
        interactive_pass = (
            interactive_preanswer_timing["p95_ms"]
            <= INTERACTIVE_READY_IN_PROCESS_P95_LIMIT_MS
            and all(
                row.get("status") == "ready"
                for row in interactive_preanswer_results
            )
            and interactive_in_process_timing["p95_ms"]
            <= INTERACTIVE_READY_IN_PROCESS_P95_LIMIT_MS
            and interactive_cli_timing["p95_ms"]
            <= INTERACTIVE_READY_CLI_P95_LIMIT_MS
            and all(interactive_in_process_checks.values())
            and all(interactive_cli_checks.values())
            and all(stale_checks.values())
        )

        route_items = sorted(ENTRY_TO_CONVERSATION.items())

        def managed_once(ordinal: int, *, warmup: bool) -> tuple[float, dict[str, Any]]:
            entry_kind, conversation_kind = route_items[ordinal % len(route_items)]
            marker = "WARM" if warmup else "RUN"
            envelope = _envelope(
                fixture,
                request_id=f"PERF-MANAGED-{marker}-{ordinal:06d}",
                conversation_kind=conversation_kind,
            )
            started = time.perf_counter_ns()
            begin = begin_personalized_turn(
                repo,
                entry_kind=entry_kind,
                target_envelope=envelope,
                state_dir=state_dir,
                max_bytes=MAX_SLICE_BYTES,
                mode="verified",
            )
            finalize = finalize_personalized_reply(
                repo,
                turn_receipt_sha256=begin["turn_receipt_sha256"],
                reply_body=f"PERF-REPLY-{marker}-{ordinal:06d}",
                adoption={
                    "schema": ADOPTION_SCHEMA,
                    "personalization_claimed": False,
                    "used_evidence_ids": [],
                    "used_match_ids": [],
                    "actions": ["direct_answer"],
                    "degraded_reason": None,
                },
                state_dir=state_dir,
            )
            elapsed_ms = (time.perf_counter_ns() - started) / 1_000_000
            begin_path = state_dir / begin["turn_receipt_ref"]
            begin_receipt = json.loads(begin_path.read_text(encoding="utf-8"))
            return elapsed_ms, {
                "entry_kind": entry_kind,
                "conversation_kind": conversation_kind,
                "begin_status": begin.get("status"),
                "finalize_status": finalize.get("status"),
                "display_authorized": finalize.get("display_authorized"),
                "reply_body_stored": finalize.get("reply_body_stored"),
                "begin_formal_write_count": begin_receipt.get("formal_write_count"),
                "finalize_formal_write_count": finalize.get("formal_write_count"),
                "route_bound": begin_receipt.get("entry_kind") == entry_kind
                and begin_receipt.get("conversation_kind") == conversation_kind,
            }

        for ordinal in range(managed_warmup):
            managed_once(ordinal, warmup=True)

        managed_times: list[float] = []
        managed_rows: list[dict[str, Any]] = []
        for ordinal in range(managed_iterations):
            elapsed_ms, row = managed_once(ordinal, warmup=False)
            managed_times.append(elapsed_ms)
            managed_rows.append(row)
        managed_timing = _timing_summary(managed_times)
        covered_routes = sorted({row["entry_kind"] for row in managed_rows})
        receipt_audit = audit_receipts(repo, state_dir=state_dir)
        managed_checks = {
            "all_begin_ready": all(row["begin_status"] == "begun" for row in managed_rows),
            "all_finalize_ready": all(
                row["finalize_status"] == "finalized" for row in managed_rows
            ),
            "all_routes_bound": all(row["route_bound"] for row in managed_rows),
            "all_routes_covered": covered_routes == sorted(ENTRY_TO_CONVERSATION),
            "all_display_authorized": all(
                row["display_authorized"] is True for row in managed_rows
            ),
            "no_reply_body_stored": all(
                row["reply_body_stored"] is False for row in managed_rows
            ),
            "all_formal_write_count_zero": all(
                row["begin_formal_write_count"] == 0
                and row["finalize_formal_write_count"] == 0
                for row in managed_rows
            ),
            "receipt_audit_pass": receipt_audit.get("status") == "pass",
        }
        managed_pass = (
            all(managed_checks.values())
            and managed_timing["p95_ms"] <= MANAGED_P95_LIMIT_MS
        )

        projection_after, projection_file_count_after = _tree_digest(
            state_dir,
            exclude_prefix="turn-receipts",
            exclude_paths=(".hot-path.lock",),
        )
        static_projection_immutability_pass = (
            projection_before == projection_after
            and projection_file_count == projection_file_count_after
        )

        study_times: list[float] = []
        study_rows: list[dict[str, Any]] = []
        study_warm_rows: list[dict[str, Any]] = []
        morning_times: list[float] = []
        morning_rows: list[dict[str, Any]] = []
        morning_warm_rows: list[dict[str, Any]] = []

        def study_once(ordinal: int, *, warmup: bool) -> tuple[float, dict[str, Any]]:
            package = _study_package(fixture, learning, ordinal)
            started = time.perf_counter_ns()
            result = study_turn_module.record_study_answer(
                repo,
                package,
                state_dir=state_dir,
                max_bytes=MAX_SLICE_BYTES,
                personalization_mode="verified",
            )
            elapsed_ms = (time.perf_counter_ns() - started) / 1_000_000
            receipt_sha = result.get("review_commit_receipt_sha256")
            if not _sha_is_valid(receipt_sha):
                raise BenchmarkError("managed_study_turn_missing_receipt")
            verification = review_hot_module.verify_commit(
                repo, receipt_sha256=str(receipt_sha)
            )
            scan = verification.get("scan_stats") or {}
            return elapsed_ms, {
                "warmup": warmup,
                "status": result.get("status"),
                "created": result.get("created"),
                "review_event_id": result.get("review_event_id"),
                "review_commit_verification": result.get(
                    "review_commit_verification"
                ),
                "review_receipt_sha256_valid": _sha_is_valid(receipt_sha),
                "receipt_reverification": verification.get("status"),
                "receipt_reverification_event_match": verification.get("event_id")
                == result.get("review_event_id"),
                "review_audit": result.get("review_audit"),
                "personalization_status": result.get("personalization_status"),
                "capture_status": result.get("capture_status"),
                "formal_write_count": result.get("formal_write_count"),
                "ledger_full_scan_count": int(
                    scan.get("ledger_full_scan_count", -1)
                ),
                "formal_table_reads": int(scan.get("formal_table_reads", -1)),
                "ledger_exact_range_reads": int(
                    scan.get("ledger_exact_range_reads", -1)
                ),
                "output_bytes": len(_json_bytes(result)),
            }

        def morning_once(
            ordinal: int, *, warmup: bool
        ) -> tuple[float, dict[str, Any]]:
            item_id = learning["morning_item_ids"][ordinal]
            args = _morning_args(fixture, learning_runtime, item_id)
            started = time.perf_counter_ns()
            result = morning_module.command_answer_turn(args)
            elapsed_ms = (time.perf_counter_ns() - started) / 1_000_000
            review_verification = result.get("review_receipt_verification") or {}
            scan = review_verification.get("scan_stats") or {}
            session_proofs = [
                result.get("session_first_commit") or {},
                result.get("session_binding_commit") or {},
                result.get("session_feedback_commit") or {},
            ]
            personalization = result.get("personalization") or {}
            proof_receipts_valid = all(
                proof.get("status") in {"committed", "already_committed", "pass"}
                and _sha_is_valid(proof.get("receipt_sha256"))
                and (proof.get("verification") or {}).get("status") == "pass"
                and (proof.get("verification") or {}).get("receipt_sha256")
                == proof.get("receipt_sha256")
                for proof in session_proofs
            )
            session_full_replays = sum(
                int(
                    (proof.get("verification") or {}).get(
                        "full_ledger_replay_count", -1
                    )
                )
                for proof in session_proofs
            )
            return elapsed_ms, {
                "warmup": warmup,
                "managed_hot_path": result.get("managed_hot_path"),
                "review_audit": result.get("review_audit"),
                "review_receipt_verification": review_verification.get("status"),
                "review_receipt_sha256_valid": _sha_is_valid(
                    result.get("review_commit_receipt_sha256")
                ),
                "session_proof_receipts_valid": proof_receipts_valid,
                "session_full_ledger_replay_count": session_full_replays,
                "capture_status": result.get("capture_status"),
                "capture_audit": result.get("capture_audit"),
                "personalization_status": personalization.get("status"),
                "personalization_bytes": len(_json_bytes(personalization)),
                "formal_write_count": result.get("formal_write_count"),
                "ledger_full_scan_count": int(
                    scan.get("ledger_full_scan_count", -1)
                ),
                "formal_table_reads": int(scan.get("formal_table_reads", -1)),
                "ledger_exact_range_reads": int(
                    scan.get("ledger_exact_range_reads", -1)
                ),
                "output_bytes": len(_json_bytes(result)),
            }

        learning_projection_before, learning_projection_file_count_before = _tree_digest(
            state_dir,
            exclude_prefix="turn-receipts",
            exclude_paths=(".hot-path.lock",),
        )
        with _forbid_broad_learning_hot_paths(all_formal_paths) as broad_guards:
            for ordinal in range(study_turn_warmup):
                _, row = study_once(ordinal, warmup=True)
                study_warm_rows.append(row)
            for measured in range(study_turn_iterations):
                ordinal = study_turn_warmup + measured
                elapsed_ms, row = study_once(ordinal, warmup=False)
                study_times.append(elapsed_ms)
                study_rows.append(row)

            for ordinal in range(morning_answer_turn_warmup):
                _, row = morning_once(ordinal, warmup=True)
                morning_warm_rows.append(row)
            for measured in range(morning_answer_turn_iterations):
                ordinal = morning_answer_turn_warmup + measured
                elapsed_ms, row = morning_once(ordinal, warmup=False)
                morning_times.append(elapsed_ms)
                morning_rows.append(row)

        all_study_rows = [*study_warm_rows, *study_rows]
        study_timing = _timing_summary(study_times)
        study_checks = {
            "all_recorded": all(row["status"] == "recorded" for row in all_study_rows),
            "all_created": all(row["created"] is True for row in all_study_rows),
            "all_review_receipts_verified": all(
                row["review_commit_verification"] == "verified"
                and row["receipt_reverification"] == "verified"
                and row["receipt_reverification_event_match"] is True
                and row["review_receipt_sha256_valid"] is True
                for row in all_study_rows
            ),
            "all_bounded_receipt_audit": all(
                row["review_audit"] == "BOUNDED_RECEIPT_VERIFIED"
                for row in all_study_rows
            ),
            "all_personalization_ready": all(
                row["personalization_status"] == "ready"
                for row in all_study_rows
            ),
            "all_formal_write_count_zero": all(
                row["formal_write_count"] == 0 for row in all_study_rows
            ),
            "all_review_ledger_full_scan_zero": all(
                row["ledger_full_scan_count"] == 0 for row in all_study_rows
            ),
            "all_formal_table_reads_zero": all(
                row["formal_table_reads"] == 0 for row in all_study_rows
            ),
            "all_exact_receipt_range_read": all(
                row["ledger_exact_range_reads"] == 1 for row in all_study_rows
            ),
            "all_output_within_budget": all(
                row["output_bytes"] <= MAX_STUDY_TURN_OUTPUT_BYTES
                for row in all_study_rows
            ),
        }
        study_pass = (
            all(study_checks.values())
            and study_timing["p95_ms"] <= STUDY_TURN_P95_LIMIT_MS
        )

        all_morning_rows = [*morning_warm_rows, *morning_rows]
        morning_timing = _timing_summary(morning_times)
        morning_checks = {
            "all_managed_hot_path": all(
                row["managed_hot_path"] is True for row in all_morning_rows
            ),
            "all_review_receipts_verified": all(
                row["review_audit"] == "HOT_VERIFIED"
                and row["review_receipt_verification"] == "verified"
                and row["review_receipt_sha256_valid"] is True
                for row in all_morning_rows
            ),
            "all_session_receipts_verified": all(
                row["session_proof_receipts_valid"] is True
                for row in all_morning_rows
            ),
            "all_session_full_replay_zero": all(
                row["session_full_ledger_replay_count"] == 0
                for row in all_morning_rows
            ),
            "all_independent_correct_capture_ineligible": all(
                row["capture_status"] == "not_eligible"
                and row["capture_audit"] == "not_applicable"
                for row in all_morning_rows
            ),
            "all_personalization_ready": all(
                row["personalization_status"] == "ready"
                for row in all_morning_rows
            ),
            "all_formal_write_count_zero": all(
                row["formal_write_count"] == 0 for row in all_morning_rows
            ),
            "all_review_ledger_full_scan_zero": all(
                row["ledger_full_scan_count"] == 0 for row in all_morning_rows
            ),
            "all_formal_table_reads_zero": all(
                row["formal_table_reads"] == 0 for row in all_morning_rows
            ),
            "all_exact_receipt_range_read": all(
                row["ledger_exact_range_reads"] == 1 for row in all_morning_rows
            ),
            "all_personalization_within_budget": all(
                row["personalization_bytes"] <= MAX_SLICE_BYTES
                for row in all_morning_rows
            ),
            "all_output_within_budget": all(
                row["output_bytes"] <= MAX_MORNING_ANSWER_TURN_OUTPUT_BYTES
                for row in all_morning_rows
            ),
        }
        morning_pass = (
            all(morning_checks.values())
            and morning_timing["p95_ms"] <= MORNING_ANSWER_TURN_P95_LIMIT_MS
        )
        broad_guards_pass = all(value == 0 for value in broad_guards.values())
        learning_projection_after, learning_projection_file_count_after = _tree_digest(
            state_dir,
            exclude_prefix="turn-receipts",
            exclude_paths=(".hot-path.lock",),
        )
        formal_after = {
            path.relative_to(repo).as_posix(): _sha256_file(path)
            for path in all_formal_paths
        }
        formal_immutability_pass = formal_before == formal_after
        immutability_pass = (
            static_projection_immutability_pass and formal_immutability_pass
        )
        dataset_scale_pass = (
            fixture["history_days"] >= MIN_HISTORY_DAYS
            and fixture["point_count"] >= len(SUBJECTS) * MIN_POINTS_PER_SUBJECT
            and fixture["question_count"]
            >= len(SUBJECTS) * MIN_POINTS_PER_SUBJECT * MIN_QUESTIONS_PER_POINT
            and fixture["corpus"]["object_count"] >= MIN_HISTORY_DAYS * len(SUBJECTS)
        )

        failures: list[str] = []
        if not dataset_scale_pass:
            failures.append("synthetic_projection_scale_contract_failed")
        if not preanswer_pass:
            failures.append("preanswer_acceptance_failed")
        if not managed_pass:
            failures.append("managed_begin_finalize_acceptance_failed")
        if not interactive_pass:
            failures.append("interactive_hotpath_v2_acceptance_failed")
        if not study_pass:
            failures.append("managed_study_turn_acceptance_failed")
        if not morning_pass:
            failures.append("managed_morning_answer_turn_acceptance_failed")
        if not broad_guards_pass:
            failures.append("managed_learning_forbidden_broad_path_observed")
        if not immutability_pass:
            failures.append("projection_or_formal_sentinel_mutated")

        report = {
            "schema": SCHEMA,
            "status": "pass" if not failures else "fail",
            "thresholds": {
                "preanswer_p95_ms_max": PREANSWER_P95_LIMIT_MS,
                "managed_begin_finalize_p95_ms_max": MANAGED_P95_LIMIT_MS,
                "managed_study_turn_p95_ms_max": STUDY_TURN_P95_LIMIT_MS,
                "managed_morning_answer_turn_p95_ms_max": (
                    MORNING_ANSWER_TURN_P95_LIMIT_MS
                ),
                "interactive_ready_in_process_p95_ms_max": (
                    INTERACTIVE_READY_IN_PROCESS_P95_LIMIT_MS
                ),
                "interactive_ready_cli_p95_ms_max": (
                    INTERACTIVE_READY_CLI_P95_LIMIT_MS
                ),
                "interactive_default_begin_bytes_max": PUBLIC_BEGIN_MAX_BYTES,
                "slice_bytes_max": MAX_SLICE_BYTES,
                "managed_study_turn_output_bytes_max": (
                    MAX_STUDY_TURN_OUTPUT_BYTES
                ),
                "managed_morning_answer_turn_output_bytes_max": (
                    MAX_MORNING_ANSWER_TURN_OUTPUT_BYTES
                ),
                "history_days_min": MIN_HISTORY_DAYS,
            },
            "dataset": {
                "kind": "deterministic_temp_only_synthetic_projection",
                "history_days": fixture["history_days"],
                "history_start": fixture["history_start"],
                "history_end": fixture["history_end"],
                "point_count": fixture["point_count"],
                "question_count": fixture["question_count"],
                "recent_signature_count": fixture["recent_signature_count"],
                "nightly_corpus_object_count": fixture["corpus"]["object_count"],
                "nightly_corpus_index_entry_count": fixture["corpus"]["index_entry_count"],
                "target_history_entry_count": fixture["corpus"]["target_history_entry_count"],
                "projection_file_count": projection_file_count,
                "projection_tree_sha256_before": projection_before,
                "projection_tree_sha256_after": projection_after,
                "learning_projection_tree_sha256_before": (
                    learning_projection_before
                ),
                "learning_projection_tree_sha256_after": (
                    learning_projection_after
                ),
                "learning_projection_file_count_before": (
                    learning_projection_file_count_before
                ),
                "learning_projection_file_count_after": (
                    learning_projection_file_count_after
                ),
                "formal_sentinel_sha256_before": formal_before,
                "formal_sentinel_sha256_after": formal_after,
                "scale_contract_pass": dataset_scale_pass,
                "production_state_written": False,
                "formal_repository_used": False,
                "synthetic_formal_projection_used": True,
                "managed_review_hot_bootstrap_status": learning_runtime[
                    "review_build"
                ].get("status"),
                "managed_formal_hot_bootstrap_status": learning_runtime[
                    "formal_build"
                ].get("status"),
                "managed_session_hot_bootstrap_status": learning_runtime[
                    "session_build"
                ].get("status"),
            },
            "preanswer_only": {
                "status": "pass" if preanswer_pass else "fail",
                "claim_scope": "safe_preanswer_slice projection lookup only",
                "iterations": preanswer_iterations,
                "warmup_iterations": preanswer_warmup,
                **preanswer_timing,
                "max_output_bytes": max(preanswer_sizes),
                "checks": preanswer_checks,
                "observed_read_set": unique_reads,
                "forbidden_reads": forbidden_reads,
                "full_ledger_scan_count": 0,
                "formal_table_scan_count": 0,
            },
            "interactive_hotpath_v2": {
                "schema": INTERACTIVE_SCHEMA,
                "status": "pass" if interactive_pass else "fail",
                "claim_scope": (
                    "temp-only ready preanswer plus v2 begin in-process and "
                    "Python CLI process boundary; excludes model and tool RPC time"
                ),
                "ready_preanswer_in_process": {
                    "iterations": interactive_iterations,
                    "warmup_iterations": interactive_preanswer_warmup,
                    **interactive_preanswer_timing,
                    "p95_limit_ms": INTERACTIVE_READY_IN_PROCESS_P95_LIMIT_MS,
                    "all_ready": all(
                        row.get("status") == "ready"
                        for row in interactive_preanswer_results
                    ),
                    "status": (
                        "pass"
                        if interactive_preanswer_timing["p95_ms"]
                        <= INTERACTIVE_READY_IN_PROCESS_P95_LIMIT_MS
                        else "fail"
                    ),
                },
                "ready_begin_in_process": {
                    "iterations": interactive_iterations,
                    "warmup_iterations": interactive_warmup,
                    **interactive_in_process_timing,
                    "p95_limit_ms": INTERACTIVE_READY_IN_PROCESS_P95_LIMIT_MS,
                    "max_result_bytes": max(
                        row["output_bytes"]
                        for row in interactive_in_process_rows
                    ),
                    "result_bytes_limit": PUBLIC_BEGIN_MAX_BYTES,
                    "checks": interactive_in_process_checks,
                },
                "ready_begin_cli": {
                    "iterations": interactive_iterations,
                    "warmup_iterations": interactive_warmup,
                    **interactive_cli_timing,
                    "p95_limit_ms": INTERACTIVE_READY_CLI_P95_LIMIT_MS,
                    "max_stdout_bytes": max(
                        row["stdout_bytes"] for row in interactive_cli_rows
                    ),
                    "stdout_bytes_limit": PUBLIC_BEGIN_MAX_BYTES,
                    "checks": interactive_cli_checks,
                },
                "stale_control": {
                    "sample_count": 2,
                    "as_of": stale_as_of,
                    "included_in_ready_timing": False,
                    "checks": stale_checks,
                },
                "production_state_used": False,
                "production_stale_may_not_count_as_ready": True,
                "formal_write_count": 0,
            },
            "managed_begin_finalize": {
                "status": "pass" if managed_pass else "fail",
                "claim_scope": "managed begin, entry routing, local receipt persistence, finalize",
                "iterations": managed_iterations,
                "warmup_iterations": managed_warmup,
                **managed_timing,
                "covered_entry_kinds": covered_routes,
                "checks": managed_checks,
                "receipt_audit": receipt_audit,
            },
            "managed_study_turn": {
                "status": "pass" if study_pass else "fail",
                "claim_scope": (
                    "real temp-only personalized_study_turn hot append, exact "
                    "review receipt verification, and bounded post-answer projection"
                ),
                "iterations": study_turn_iterations,
                "warmup_iterations": study_turn_warmup,
                **study_timing,
                "max_output_bytes": max(
                    row["output_bytes"] for row in all_study_rows
                ),
                "output_budget_bytes": MAX_STUDY_TURN_OUTPUT_BYTES,
                "personalization_budget_bytes": MAX_SLICE_BYTES,
                "review_ledger_full_scan_count": sum(
                    row["ledger_full_scan_count"] for row in all_study_rows
                ),
                "capture_ledger_full_scan_count": 0,
                "formal_markdown_hot_read_count": sum(
                    row["formal_table_reads"] for row in all_study_rows
                ),
                "exact_ledger_range_read_count": sum(
                    row["ledger_exact_range_reads"] for row in all_study_rows
                ),
                "checks": study_checks,
            },
            "managed_morning_answer_turn": {
                "status": "pass" if morning_pass else "fail",
                "claim_scope": (
                    "real temp-only morning answer-turn managed hot "
                    "independent_correct path with review, first, binding, and "
                    "feedback receipts"
                ),
                "result_class": "independent_correct",
                "iterations": morning_answer_turn_iterations,
                "warmup_iterations": morning_answer_turn_warmup,
                **morning_timing,
                "max_output_bytes": max(
                    row["output_bytes"] for row in all_morning_rows
                ),
                "output_budget_bytes": MAX_MORNING_ANSWER_TURN_OUTPUT_BYTES,
                "max_personalization_bytes": max(
                    row["personalization_bytes"] for row in all_morning_rows
                ),
                "personalization_budget_bytes": MAX_SLICE_BYTES,
                "review_ledger_full_scan_count": sum(
                    row["ledger_full_scan_count"] for row in all_morning_rows
                ),
                "capture_ledger_full_scan_count": 0,
                "session_ledger_full_replay_count": sum(
                    row["session_full_ledger_replay_count"]
                    for row in all_morning_rows
                ),
                "formal_markdown_hot_read_count": sum(
                    row["formal_table_reads"] for row in all_morning_rows
                ),
                "exact_ledger_range_read_count": sum(
                    row["ledger_exact_range_reads"] for row in all_morning_rows
                ),
                "capture_scope": (
                    "independent_correct is ineligible, so no capture writer or "
                    "capture ledger is invoked"
                ),
                "checks": morning_checks,
            },
            "managed_learning_hot_guards": {
                "status": "pass" if broad_guards_pass else "fail",
                **broad_guards,
            },
            "immutability": {
                "status": "pass" if immutability_pass else "fail",
                "projection_unchanged_excluding_receipts": (
                    static_projection_immutability_pass
                ),
                "learning_projection_changed_only_as_derived_overlay": (
                    learning_projection_before != learning_projection_after
                    or learning_projection_file_count_before
                    != learning_projection_file_count_after
                ),
                "formal_sentinels_unchanged": formal_before == formal_after,
                "formal_write_count": 0,
            },
            "legacy_answer_turn": {
                "status": "not_measured",
                "pass_claim": False,
                "scope": "legacy_recovery_branches_only",
                "managed_hot_answer_turn_measured": True,
                "reason": (
                    "managed hot study and morning answer-turns are measured above; "
                    "legacy record-first/capture-review-event/reconcile/full-audit "
                    "recovery branches remain intentionally unexecuted"
                ),
                "full_scan_zero_claim": False,
            },
            "failures": failures,
            "claim_boundary": (
                "Pass applies to the temp-only synthetic preanswer path, managed "
                "begin/finalize wrapper, managed ordinary hot study turn, and managed "
                "morning independent-correct answer-turn. It is not a production "
                "latency sample, does not measure a non-correct capture transaction, "
                "and does not certify any legacy recovery branch."
            ),
        }
    assert temporary_path is not None
    report["dataset"]["temporary_workspace_removed"] = not temporary_path.exists()
    return report


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--as-of", default=AS_OF_DEFAULT)
    parser.add_argument("--history-days", type=int, default=120)
    parser.add_argument("--points-per-subject", type=int, default=48)
    parser.add_argument("--questions-per-point", type=int, default=4)
    parser.add_argument("--preanswer-iterations", type=int, default=60)
    parser.add_argument("--preanswer-warmup", type=int, default=10)
    parser.add_argument("--managed-iterations", type=int, default=20)
    parser.add_argument("--managed-warmup", type=int, default=2)
    parser.add_argument("--study-turn-iterations", type=int, default=20)
    parser.add_argument("--study-turn-warmup", type=int, default=2)
    parser.add_argument("--morning-answer-turn-iterations", type=int, default=20)
    parser.add_argument("--morning-answer-turn-warmup", type=int, default=2)
    parser.add_argument("--interactive-iterations", type=int, default=20)
    parser.add_argument("--interactive-warmup", type=int, default=1)
    parser.add_argument("--output", type=Path)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        report = run_benchmark(
            as_of=args.as_of,
            history_days=args.history_days,
            points_per_subject=args.points_per_subject,
            questions_per_point=args.questions_per_point,
            preanswer_iterations=args.preanswer_iterations,
            preanswer_warmup=args.preanswer_warmup,
            managed_iterations=args.managed_iterations,
            managed_warmup=args.managed_warmup,
            study_turn_iterations=args.study_turn_iterations,
            study_turn_warmup=args.study_turn_warmup,
            morning_answer_turn_iterations=args.morning_answer_turn_iterations,
            morning_answer_turn_warmup=args.morning_answer_turn_warmup,
            interactive_iterations=args.interactive_iterations,
            interactive_warmup=args.interactive_warmup,
        )
    except (BenchmarkError, OSError) as exc:
        report = {
            "schema": SCHEMA,
            "status": "error",
            "reason_code": str(exc),
            "production_state_written": False,
            "formal_write_count": 0,
        }
    raw = _json_bytes(report, pretty=True)
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        descriptor, temporary = tempfile.mkstemp(
            prefix=f".{args.output.name}.", dir=args.output.parent
        )
        try:
            with os.fdopen(descriptor, "wb") as handle:
                handle.write(raw)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, args.output)
        finally:
            if os.path.exists(temporary):
                os.unlink(temporary)
    sys.stdout.write(raw.decode("utf-8"))
    return 0 if report.get("status") == "pass" else 2


if __name__ == "__main__":
    raise SystemExit(main())
