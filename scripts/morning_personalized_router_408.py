#!/usr/bin/env python3
"""Route exactly one bounded, answer-safe morning 408 practice item.

The router consumes already-built projections supplied by its caller.  It does
not know the repository layout and never opens a formal table, review ledger,
Obsidian vault, or question corpus.  Its only write is an optional
content-addressed variant-demand request when no safe question surface can be
selected.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sys
import tempfile
from datetime import date
from pathlib import Path
from typing import Any, Iterable


ROUTE_SCHEMA = "morning_personalized_route_v1"
DEMAND_SCHEMA = "morning_variant_demand_v1"
QUESTION_INDEX_SCHEMA = "personalization_index_v1"
QUESTION_INDEX_KIND = "question_inverted_index"
VARIANT_CATALOG_SCHEMA = "variant_source_catalog_v1"
VARIANT_CATALOG_KIND = "verified_variant_source_catalog"
CROSS_VARIANT_CATALOG_SCHEMA = "cross_variant_catalog_v1"
CROSS_VARIANT_CATALOG_KIND = "verified_cross_variant_catalog"
LEARNER_COOLDOWN_SCHEMA = "morning_learner_cooldown_v1"
VARIANT_COOLDOWN_SCHEMA = "morning_variant_cooldown_v1"
COOLDOWN_DAYS = 7
MAX_INPUT_BYTES = 8 * 1024 * 1024
MAX_TARGET_IDS = 24
MAX_CANDIDATES = 4096
FORMAL_NODE_RE = re.compile(r"^(?:DS|CO|OS|CN)_(?:\d{4}|UNK)_\d{3}$")
POINT_RE = re.compile(r"^(?:DS|CO|OS|CN)\d{2}-\d{2}$")
CROSS_EDGE_RE = re.compile(r"^CSR408-[0-9A-F]{16}$")
SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
SAFE_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,159}$")
VERIFIED_ORIGINAL_STATUSES = {
    "verified_exam_locator",
    "verified_question_surface",
}
UNKNOWN_DATE_VALUES = {"", "unknown", "unrecorded", "未记录"}


class MorningRouterError(RuntimeError):
    """A stable fail-closed routing error."""


def _retired_write_surface() -> None:
    raise MorningRouterError(
        "morning_personalized_router_writes_retired_read_only_compatibility"
    )


def _canonical_bytes(value: Any) -> bytes:
    return (
        json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        + "\n"
    ).encode("utf-8")


def _pretty_bytes(value: Any) -> bytes:
    return (
        json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2) + "\n"
    ).encode("utf-8")


def _sha256(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _digest(value: Any) -> str:
    return _sha256(_canonical_bytes(value))


def _as_date(value: object, label: str, *, allow_unknown: bool = False) -> str | None:
    raw = str(value or "").strip()
    if allow_unknown and raw.lower() in UNKNOWN_DATE_VALUES:
        return None
    try:
        parsed = date.fromisoformat(raw)
    except ValueError as exc:
        raise MorningRouterError(f"{label} must be YYYY-MM-DD or explicit unknown") from exc
    return parsed.isoformat()


def _age(last_date: str | None, as_of: str, label: str) -> int | None:
    if last_date is None:
        return None
    days = (date.fromisoformat(as_of) - date.fromisoformat(last_date)).days
    if days < 0:
        raise MorningRouterError(f"{label} is later than as_of")
    return days


def _safe_ids(values: object, label: str, pattern: re.Pattern[str] = SAFE_ID_RE) -> list[str]:
    if values is None:
        return []
    if not isinstance(values, list) or len(values) > MAX_TARGET_IDS:
        raise MorningRouterError(f"{label} must be a bounded list")
    result: list[str] = []
    for raw in values:
        value = str(raw or "").strip()
        if not pattern.fullmatch(value):
            raise MorningRouterError(f"{label} contains an unsafe identifier")
        if value not in result:
            result.append(value)
    return result


def _validate_target(target: dict[str, Any]) -> dict[str, list[str]]:
    if not isinstance(target, dict):
        raise MorningRouterError("target must be a JSON object")
    allowed = {"subjects", "point_ids", "mechanism_ids", "formal_node_ids"}
    if set(target) - allowed:
        raise MorningRouterError("target contains unsupported fields")
    subjects = _safe_ids(target.get("subjects", []), "target.subjects")
    if any(value not in {"DS", "CO", "OS", "CN"} for value in subjects):
        raise MorningRouterError("target.subjects contains an unsupported subject")
    formal_ids = _safe_ids(
        target.get("formal_node_ids", []),
        "target.formal_node_ids",
        FORMAL_NODE_RE,
    )
    normalized = {
        "subjects": subjects,
        "point_ids": _safe_ids(target.get("point_ids", []), "target.point_ids"),
        "mechanism_ids": _safe_ids(
            target.get("mechanism_ids", []), "target.mechanism_ids"
        ),
        "formal_node_ids": formal_ids,
    }
    if not any(normalized.values()):
        raise MorningRouterError("target must contain at least one controlled identifier")
    return normalized


def _validate_question_index(payload: dict[str, Any]) -> dict[str, Any]:
    if (
        not isinstance(payload, dict)
        or payload.get("schema") != QUESTION_INDEX_SCHEMA
        or payload.get("kind") != QUESTION_INDEX_KIND
        or not isinstance(payload.get("questions"), dict)
    ):
        raise MorningRouterError("question_index is not a verified projection")
    questions = payload["questions"]
    if len(questions) > MAX_CANDIDATES:
        raise MorningRouterError("question_index exceeds the bounded router limit")
    declared = payload.get("question_count")
    if declared is not None and int(declared) != len(questions):
        raise MorningRouterError("question_index count mismatch")
    return payload


def _validate_catalog(
    payload: dict[str, Any] | None, *, cross: bool
) -> dict[str, Any] | None:
    if payload is None:
        return None
    schema = CROSS_VARIANT_CATALOG_SCHEMA if cross else VARIANT_CATALOG_SCHEMA
    kind = CROSS_VARIANT_CATALOG_KIND if cross else VARIANT_CATALOG_KIND
    if (
        not isinstance(payload, dict)
        or payload.get("schema") != schema
        or payload.get("kind") != kind
        or int(payload.get("formal_write_count", -1)) != 0
        or not isinstance(payload.get("entries"), list)
        or int(payload.get("entry_count", -1)) != len(payload["entries"])
        or len(payload["entries"]) > MAX_CANDIDATES
    ):
        raise MorningRouterError("variant catalog is not a verified bounded projection")
    return payload


def _current_live_cross_edges(payload: dict[str, Any] | None) -> dict[str, dict[str, Any]]:
    if payload is None:
        return {}
    records = payload.get("records") if isinstance(payload, dict) else None
    if (
        payload.get("schema_version") != "cross_subject_shadow_v1"
        or int(payload.get("formal_write_count", -1)) != 0
        or not isinstance(records, list)
        or len(records) > MAX_CANDIDATES
    ):
        raise MorningRouterError("cross projection is invalid")
    live: dict[str, dict[str, Any]] = {}
    for row in records:
        if not isinstance(row, dict):
            raise MorningRouterError("cross projection record is invalid")
        if not row.get("enabled_for_live_routing"):
            continue
        edge_id = str(row.get("edge_id") or "")
        if (
            not CROSS_EDGE_RE.fullmatch(edge_id)
            or row.get("lifecycle") != "verified_existing"
            or row.get("activation_eligible") is not True
            or row.get("activation") != "verified_live"
        ):
            raise MorningRouterError("cross projection contains an unsafe live edge")
        live[edge_id] = row
    return live


def _validate_cooldown(
    payload: dict[str, Any], *, schema: str, collection: str, as_of: str
) -> dict[str, Any]:
    if (
        not isinstance(payload, dict)
        or payload.get("schema") != schema
        or payload.get("as_of") != as_of
        or not isinstance(payload.get(collection), dict)
        or len(payload[collection]) > MAX_CANDIDATES
    ):
        raise MorningRouterError(f"{collection} cooldown is stale or invalid")
    return payload[collection]


def _row_date(
    rows: dict[str, Any], identifier: str, field: str, fallback: object, label: str
) -> str | None:
    row = rows.get(identifier)
    if row is None:
        return _as_date(fallback, label, allow_unknown=True)
    if not isinstance(row, dict) or set(row) - {field}:
        raise MorningRouterError(f"{label} cooldown row is invalid")
    return _as_date(row.get(field), label, allow_unknown=True)


def _verified_original(row: dict[str, Any]) -> bool:
    source = row.get("reliable_source")
    return bool(
        isinstance(source, dict)
        and source.get("status") in VERIFIED_ORIGINAL_STATUSES
        and source.get("original_question_eligible") is True
        and source.get("surface_verified") is True
        and source.get("answer_isolated") is True
        and isinstance(source.get("source_ref"), str)
        and 0 < len(source["source_ref"]) <= 512
        and "\n" not in source["source_ref"]
        and SHA256_RE.fullmatch(str(source.get("source_sha256") or ""))
    )


def _candidate_ids(index: dict[str, Any], target: dict[str, list[str]]) -> dict[str, int]:
    questions = index["questions"]
    ranks: dict[str, int] = {}

    def add(node_id: str, rank: int) -> None:
        if node_id in questions and FORMAL_NODE_RE.fullmatch(node_id):
            ranks[node_id] = min(rank, ranks.get(node_id, rank))

    for node_id in target["formal_node_ids"]:
        add(node_id, 0)
    for mechanism in target["mechanism_ids"]:
        for ref in (index.get("by_mechanism") or {}).get(mechanism, []):
            if isinstance(ref, dict):
                add(str(ref.get("node_id") or ""), 1)
    for point in target["point_ids"]:
        for ref in (index.get("by_point") or {}).get(point, []):
            if isinstance(ref, dict):
                add(str(ref.get("node_id") or ""), 2)

    # A valid projection may omit one inverted bucket while still carrying the
    # question row.  This bounded projection-only fallback never reads a source.
    if not ranks:
        mechanisms = set(target["mechanism_ids"])
        points = set(target["point_ids"])
        for node_id, row in questions.items():
            if not isinstance(row, dict):
                continue
            if mechanisms.intersection(map(str, row.get("mechanism_ids", []))):
                add(str(node_id), 1)
            elif points.intersection(map(str, row.get("point_ids", []))):
                add(str(node_id), 2)
    return ranks


def _original_candidates(
    index: dict[str, Any],
    target: dict[str, list[str]],
    cooldown: dict[str, Any],
    as_of: str,
) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for node_id, match_rank in _candidate_ids(index, target).items():
        question = index["questions"].get(node_id)
        if not isinstance(question, dict) or not _verified_original(question):
            continue
        last_date = _row_date(
            cooldown,
            node_id,
            "last_attempt_date",
            question.get("effective_last_attempt_date"),
            f"question {node_id}",
        )
        age = _age(last_date, as_of, f"question {node_id}")
        source = question["reliable_source"]
        mechanisms = [
            value
            for value in map(str, question.get("mechanism_ids", []))
            if SAFE_ID_RE.fullmatch(value)
        ][:MAX_TARGET_IDS]
        points = [
            value
            for value in map(str, question.get("point_ids", []))
            if SAFE_ID_RE.fullmatch(value)
        ][:MAX_TARGET_IDS]
        rows.append(
            {
                "node_id": node_id,
                "match_rank": match_rank,
                "subject": question.get("subject"),
                "point_ids": points,
                "mechanism_ids": mechanisms,
                "last_attempt_date": last_date,
                "days_since_attempt": age,
                "cooldown_eligible": age is None or age >= COOLDOWN_DAYS,
                "date_evidence": "unknown_low_priority" if age is None else "known",
                "source_id": source.get("source_id") or node_id,
                "source_ref": source["source_ref"],
                "source_sha256": source["source_sha256"],
            }
        )
    return rows


def _due_original_key(row: dict[str, Any]) -> tuple[Any, ...]:
    age = row["days_since_attempt"]
    return (
        age is None,
        int(row["match_rank"]),
        -(int(age) if age is not None else -1),
        row["node_id"],
    )


def _focus_key(row: dict[str, Any]) -> tuple[Any, ...]:
    return (int(row["match_rank"]), row["node_id"])


def _variant_rows(
    catalog: dict[str, Any] | None,
    cross_catalog: dict[str, Any] | None,
    variant_cooldown: dict[str, Any],
    as_of: str,
    points: set[str],
    mechanisms: set[str],
    preferred_anchors: set[str],
    live_cross_edges: dict[str, dict[str, Any]],
) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for scope, payload in (("ordinary", catalog), ("cross_subject", cross_catalog)):
        if payload is None:
            continue
        for entry in payload["entries"]:
            if not isinstance(entry, dict) or entry.get("status") != "verified_ready":
                continue
            source_id = str(entry.get("source_id") or "")
            verified_cross_edge_id: str | None = None
            matched_cross_target: dict[str, str] | None = None
            cross_targets: list[dict[str, str]] | None = None
            cross_match_basis: list[str] | None = None
            if scope == "cross_subject":
                anchor = str(entry.get("original_formal_node_id") or "")
                verified_cross_edge_id = str(
                    entry.get("verified_cross_edge_id") or ""
                )
                raw_targets = entry.get("targets")
                if (
                    not CROSS_EDGE_RE.fullmatch(verified_cross_edge_id)
                    or not isinstance(raw_targets, list)
                    or len(raw_targets) != 2
                ):
                    continue
                cross_targets = []
                valid_targets = True
                for raw_target in raw_targets:
                    if not isinstance(raw_target, dict):
                        valid_targets = False
                        break
                    target = {
                        "subject": str(raw_target.get("subject") or ""),
                        "point_id": str(raw_target.get("point_id") or ""),
                        "mechanism_key": str(raw_target.get("mechanism_key") or ""),
                    }
                    if (
                        target["subject"] not in {"DS", "CO", "OS", "CN"}
                        or not POINT_RE.fullmatch(target["point_id"])
                        or target["point_id"][:2] != target["subject"]
                        or not SAFE_ID_RE.fullmatch(target["mechanism_key"])
                    ):
                        valid_targets = False
                        break
                    cross_targets.append(target)
                if (
                    not valid_targets
                    or len({target["subject"] for target in cross_targets}) != 2
                ):
                    continue
                live_edge = live_cross_edges.get(verified_cross_edge_id)
                live_endpoints = (
                    {
                        str((live_edge.get("left") or {}).get("id") or ""),
                        str((live_edge.get("right") or {}).get("id") or ""),
                    }
                    if isinstance(live_edge, dict)
                    else set()
                )
                if live_endpoints != {
                    target["point_id"] for target in cross_targets
                }:
                    continue
                matches = [
                    target
                    for target in cross_targets
                    if (points and target["point_id"] in points)
                    or (mechanisms and target["mechanism_key"] in mechanisms)
                ]
                if len(matches) != 1:
                    continue
                matched_cross_target = matches[0]
                cross_match_basis = []
                if matched_cross_target["point_id"] in points:
                    cross_match_basis.append("point")
                if matched_cross_target["mechanism_key"] in mechanisms:
                    cross_match_basis.append("mechanism")
                knowledge_point = matched_cross_target["point_id"]
                mechanism = matched_cross_target["mechanism_key"]
                review_unit_id = None
            else:
                anchor = str(entry.get("anchor_formal_node_id") or "")
                knowledge_point = entry.get("knowledge_point")
                mechanism = str(entry.get("mechanism_key") or "")
                review_unit_id = entry.get("review_unit_id")
            if (
                not SAFE_ID_RE.fullmatch(source_id)
                or (scope == "ordinary" and mechanism not in mechanisms)
                or not FORMAL_NODE_RE.fullmatch(anchor)
                or not isinstance(entry.get("source_ref"), str)
                or not entry["source_ref"]
                or len(entry["source_ref"]) > 512
                or "\n" in entry["source_ref"]
                or not SHA256_RE.fullmatch(str(entry.get("source_sha256") or ""))
            ):
                continue
            last_date = _row_date(
                variant_cooldown,
                source_id,
                "last_displayed_date",
                None,
                f"variant {source_id}",
            )
            age = _age(last_date, as_of, f"variant {source_id}")
            if age is not None and age < COOLDOWN_DAYS:
                continue
            rows.append(
                {
                    "source_id": source_id,
                    "source_ref": entry["source_ref"],
                    "source_sha256": entry["source_sha256"],
                    "anchor_formal_node_id": anchor,
                    "knowledge_point": knowledge_point,
                    "mechanism_key": mechanism,
                    "review_unit_id": review_unit_id,
                    "catalog_scope": scope,
                    "verified_cross_edge_id": verified_cross_edge_id,
                    "matched_cross_target": matched_cross_target,
                    "cross_targets": cross_targets,
                    "cross_match_basis": cross_match_basis,
                    "last_displayed_date": last_date,
                    "days_since_display": age,
                    "date_evidence": "unknown_low_priority" if age is None else "known",
                    "preferred_anchor": anchor in preferred_anchors,
                }
            )
    rows.sort(
        key=lambda row: (
            not row["preferred_anchor"],
            row["days_since_display"] is None,
            -(int(row["days_since_display"]) if row["days_since_display"] is not None else -1),
            row["catalog_scope"] != "ordinary",
            row["source_id"],
        )
    )
    return rows


def _original_selection(row: dict[str, Any], gate: str) -> dict[str, Any]:
    return {
        "kind": "original_question",
        "practice_mode": "original",
        "gate_decision": gate,
        "formal_node_id": row["node_id"],
        "source_id": row["source_id"],
        "source_ref": row["source_ref"],
        "source_sha256": row["source_sha256"],
        "last_attempt_date": row["last_attempt_date"],
        "days_since_attempt": row["days_since_attempt"],
        "date_evidence": row["date_evidence"],
        "match_rank": row["match_rank"],
        "schedule_allowed": True,
        "answer_safe": True,
    }


def _variant_selection(row: dict[str, Any]) -> dict[str, Any]:
    return {
        "kind": "verified_variant",
        "practice_mode": "variant",
        "gate_decision": "recent_original_verified_same_mechanism_variant",
        "formal_node_id": None,
        "anchor_formal_node_id": row["anchor_formal_node_id"],
        "source_id": row["source_id"],
        "source_ref": row["source_ref"],
        "source_sha256": row["source_sha256"],
        "knowledge_point": row["knowledge_point"],
        "mechanism_key": row["mechanism_key"],
        "review_unit_id": row["review_unit_id"],
        "catalog_scope": row["catalog_scope"],
        "verified_cross_edge_id": row["verified_cross_edge_id"],
        "matched_cross_target": row["matched_cross_target"],
        "cross_targets": row["cross_targets"],
        "cross_match_basis": row["cross_match_basis"],
        "last_displayed_date": row["last_displayed_date"],
        "days_since_display": row["days_since_display"],
        "date_evidence": row["date_evidence"],
        "schedule_allowed": True,
        "answer_safe": True,
    }


def _atomic_content_addressed_write(directory: Path, payload: dict[str, Any]) -> tuple[Path, str]:
    _retired_write_surface()

    # Historical implementation retained below for read-only source compatibility.
    body = _pretty_bytes(payload)
    digest = _sha256(body)
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    target = directory / f"{digest}.json"
    if target.exists():
        if target.is_symlink() or target.read_bytes() != body:
            raise MorningRouterError("content-addressed demand path collision")
        return target, digest
    descriptor, temporary = tempfile.mkstemp(prefix=".morning-demand.", dir=directory)
    try:
        os.fchmod(descriptor, 0o600)
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(body)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, target)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)
    return target, digest


def _write_demand(
    demand_dir: str | Path,
    *,
    as_of: str,
    target: dict[str, list[str]],
    anchor: str | None,
    question_index: dict[str, Any],
    variant_catalog: dict[str, Any] | None,
    cross_variant_catalog: dict[str, Any] | None,
) -> dict[str, Any]:
    _retired_write_surface()

    # Historical implementation retained below for read-only source compatibility.
    core = {
        "schema": DEMAND_SCHEMA,
        "as_of": as_of,
        "anchor_formal_node_id": anchor,
        "target": target,
        "reason_code": "no_due_original_or_verified_same_mechanism_variant",
        "required_output": "independently_verified_answer_isolated_variant",
        "required_gates": [
            "reliable_original_surface",
            "distinct_author_and_verifier",
            "unique_answer_verified",
            "same_mechanism_verified",
            "changed_surface_verified",
            "answer_isolation_verified",
        ],
        "projection_commitments": {
            "question_index_sha256": _digest(question_index),
            "variant_catalog_sha256": _digest(variant_catalog) if variant_catalog else None,
            "cross_variant_catalog_sha256": (
                _digest(cross_variant_catalog) if cross_variant_catalog else None
            ),
        },
        "answer_safe": True,
        "formal_write_count": 0,
        "question_content_count": 0,
    }
    request_id = "MVD-" + _digest(core)[:24].upper()
    payload = {**core, "request_id": request_id}
    path, digest = _atomic_content_addressed_write(Path(demand_dir).resolve(), payload)
    return {
        "request_id": request_id,
        "request_path": str(path),
        "request_sha256": digest,
        "status": "durable_pending_nightly_generation",
    }


def route_morning_practice(
    *,
    question_index: dict[str, Any],
    variant_catalog: dict[str, Any],
    learner_cooldown: dict[str, Any],
    variant_cooldown: dict[str, Any],
    target: dict[str, Any],
    as_of: str,
    demand_dir: str | Path | None = None,
    cross_variant_catalog: dict[str, Any] | None = None,
    cross_projection: dict[str, Any] | None = None,
    user_requested_original_id: str | None = None,
    followup_depth: int = 0,
) -> dict[str, Any]:
    """Return one selected item without recursively scheduling a follow-up."""

    as_of = _as_date(as_of, "as_of") or ""
    if not isinstance(followup_depth, int) or followup_depth < 0 or followup_depth > 32:
        raise MorningRouterError("followup_depth must be an integer from 0 through 32")
    question_index = _validate_question_index(question_index)
    variant_catalog = _validate_catalog(variant_catalog, cross=False) or {}
    cross_variant_catalog = _validate_catalog(cross_variant_catalog, cross=True)
    live_cross_edges = _current_live_cross_edges(cross_projection)
    normalized_target = _validate_target(target)
    learner_rows = _validate_cooldown(
        learner_cooldown,
        schema=LEARNER_COOLDOWN_SCHEMA,
        collection="questions",
        as_of=as_of,
    )
    variant_rows = _validate_cooldown(
        variant_cooldown,
        schema=VARIANT_COOLDOWN_SCHEMA,
        collection="variants",
        as_of=as_of,
    )
    candidates = _original_candidates(
        question_index, normalized_target, learner_rows, as_of
    )

    requested = str(user_requested_original_id or "").strip()
    if requested:
        if not FORMAL_NODE_RE.fullmatch(requested):
            raise MorningRouterError("user_requested_original_id is invalid")
        question = question_index["questions"].get(requested)
        if not isinstance(question, dict) or not _verified_original(question):
            raise MorningRouterError("user-requested original has no verified question surface")
        requested_rows = _original_candidates(
            question_index,
            {**normalized_target, "formal_node_ids": [requested]},
            learner_rows,
            as_of,
        )
        row = next((item for item in requested_rows if item["node_id"] == requested), None)
        if row is None:
            raise MorningRouterError("user-requested original could not be resolved")
        selected = _original_selection(row, "user_requested_exception")
        selected["exception_reason"] = "explicit_user_request_overrides_normal_0_to_6_day_block"
    else:
        focus = min(candidates, key=_focus_key) if candidates else None
        exact_ids = set(normalized_target["formal_node_ids"])
        exact = [row for row in candidates if row["node_id"] in exact_ids]
        due_exact = sorted(
            (row for row in exact if row["cooldown_eligible"]),
            key=_due_original_key,
        )
        if due_exact:
            selected = _original_selection(due_exact[0], "original_due_at_least_7d")
        elif focus is not None and not focus["cooldown_eligible"]:
            mechanisms = set(normalized_target["mechanism_ids"])
            if not mechanisms:
                mechanisms.update(focus["mechanism_ids"])
            variants = _variant_rows(
                variant_catalog,
                cross_variant_catalog,
                variant_rows,
                as_of,
                set(normalized_target["point_ids"]),
                mechanisms,
                exact_ids or {focus["node_id"]},
                live_cross_edges,
            )
            if variants:
                selected = _variant_selection(variants[0])
            else:
                alternate_due = sorted(
                    (
                        row
                        for row in candidates
                        if row["cooldown_eligible"] and row["node_id"] not in exact_ids
                    ),
                    key=_due_original_key,
                )
                if alternate_due:
                    selected = _original_selection(
                        alternate_due[0], "alternate_original_due_at_least_7d"
                    )
                else:
                    demand = _write_demand(
                        demand_dir,
                        as_of=as_of,
                        target=normalized_target,
                        anchor=focus["node_id"],
                        question_index=question_index,
                        variant_catalog=variant_catalog,
                        cross_variant_catalog=cross_variant_catalog,
                    )
                    selected = {
                        "kind": "mechanism_check",
                        "practice_mode": "mechanism_check",
                        "gate_decision": "mechanism_check_with_durable_variant_demand",
                        "target": normalized_target,
                        "anchor_formal_node_id": focus["node_id"],
                        "prompt_contract": "check_first_boundary_without_revealing_an_answer",
                        "variant_demand": demand,
                        "schedule_allowed": True,
                        "answer_safe": True,
                    }
        else:
            due = sorted(
                (row for row in candidates if row["cooldown_eligible"]),
                key=_due_original_key,
            )
            if due:
                selected = _original_selection(due[0], "original_due_at_least_7d")
            else:
                demand = _write_demand(
                    demand_dir,
                    as_of=as_of,
                    target=normalized_target,
                    anchor=focus["node_id"] if focus else None,
                    question_index=question_index,
                    variant_catalog=variant_catalog,
                    cross_variant_catalog=cross_variant_catalog,
                )
                selected = {
                    "kind": "mechanism_check",
                    "practice_mode": "mechanism_check",
                    "gate_decision": "mechanism_check_with_durable_variant_demand",
                    "target": normalized_target,
                    "anchor_formal_node_id": focus["node_id"] if focus else None,
                    "prompt_contract": "check_first_boundary_without_revealing_an_answer",
                    "variant_demand": demand,
                    "schedule_allowed": True,
                    "answer_safe": True,
                }

    return {
        "schema": ROUTE_SCHEMA,
        "status": "selected",
        "as_of": as_of,
        "cooldown_days": COOLDOWN_DAYS,
        "selected_item_count": 1,
        "selected_item": selected,
        "followup_depth": followup_depth,
        "recursive_followup_allowed": False,
        "recursion_blocked": followup_depth > 0,
        "answer_safe": True,
        "formal_write_count": 0,
        "runtime_source_policy": {
            "question_index_only": True,
            "formal_repository_scan_count": 0,
            "review_ledger_scan_count": 0,
        },
    }


def _load_json(path: str, label: str) -> dict[str, Any]:
    file_path = Path(path)
    try:
        if file_path.stat().st_size > MAX_INPUT_BYTES:
            raise MorningRouterError(f"{label} exceeds the bounded input size")
        value = json.loads(file_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise MorningRouterError(f"{label} is unreadable") from exc
    if not isinstance(value, dict):
        raise MorningRouterError(f"{label} must be a JSON object")
    return value


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--question-index", required=True)
    parser.add_argument("--variant-catalog", required=True)
    parser.add_argument("--cross-variant-catalog")
    parser.add_argument("--cross-projection")
    parser.add_argument("--learner-cooldown", required=True)
    parser.add_argument("--variant-cooldown", required=True)
    parser.add_argument("--target-json", required=True)
    parser.add_argument("--as-of", required=True)
    parser.add_argument("--user-requested-original-id")
    parser.add_argument("--followup-depth", type=int, default=0)
    return parser


def main(argv: Iterable[str] | None = None) -> int:
    args = _parser().parse_args(list(argv) if argv is not None else None)
    try:
        target = json.loads(args.target_json)
        if not isinstance(target, dict):
            raise MorningRouterError("target-json must decode to an object")
        result = route_morning_practice(
            question_index=_load_json(args.question_index, "question_index"),
            variant_catalog=_load_json(args.variant_catalog, "variant_catalog"),
            cross_variant_catalog=(
                _load_json(args.cross_variant_catalog, "cross_variant_catalog")
                if args.cross_variant_catalog
                else None
            ),
            cross_projection=(
                _load_json(args.cross_projection, "cross_projection")
                if args.cross_projection
                else None
            ),
            learner_cooldown=_load_json(args.learner_cooldown, "learner_cooldown"),
            variant_cooldown=_load_json(args.variant_cooldown, "variant_cooldown"),
            target=target,
            as_of=args.as_of,
            user_requested_original_id=args.user_requested_original_id,
            followup_depth=args.followup_depth,
        )
    except (MorningRouterError, json.JSONDecodeError) as exc:
        print(json.dumps({"status": "error", "reason": str(exc)}, ensure_ascii=False))
        return 2
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    sys.exit(main())
