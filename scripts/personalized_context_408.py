#!/usr/bin/env python3
"""Build one bounded, answer-safe personalization slice for a 408 answer turn.

The hot path consumes disposable projections plus the canonical event already
available to the caller.  It never opens the review ledger, intake ledger,
formal Markdown tables, question cards, reference books, or an Obsidian vault.

The function exported to ``morning_review_session.py`` is
``safe_context_slice``.  It is deliberately fail-soft: projection problems
return a small unavailable receipt while the caller keeps the already-committed
learning evidence and feedback transaction intact.
"""

from __future__ import annotations

import argparse
import copy
import fcntl
import hashlib
import json
import os
import re
import sys
import tempfile
from contextlib import contextmanager
from datetime import date, datetime
from pathlib import Path
from typing import Any, Iterator
from zoneinfo import ZoneInfo

from personalization_index_408 import (
    PersonalizationIndexError,
    apply_event_to_projection,
)
from personalization_commonality_408 import (
    SIGNATURE_SCHEMA,
    apply_event_to_signature_index,
    build_response_plan,
    match_historical_signatures,
    match_recent_signatures,
)
from personalization_preanswer_408 import (
    _optional_nightly_corpus,
    _public_corpus_context,
)
from linked_practice_source_408 import selection_commitment_key
from morning_personalized_runtime_408 import (
    DEFAULT_RUNTIME_RELATIVE as DEFAULT_VARIANT_RUNTIME_RELATIVE,
    MorningRuntimeError,
    derive_variant_cooldown,
)


SCHEMA = "personalization_context_slice_v1"
SELECTION_RECEIPT_SCHEMA = "personalization_selection_receipt_v1"
BUDGET_VERSION = "bounded-v2"
DEFAULT_MAX_BYTES = 12_288
MIN_MAX_BYTES = 1_024
MAX_MAX_BYTES = 65_536
MAX_CROSS_PROJECTION_INPUT_BYTES = 8 * 1024 * 1024
MAX_CROSS_PROJECTION_RECORDS = 4096
DEFAULT_STATE_RELATIVE = Path(
    "wiki/study_vaults/408-full/state/personalization"
)
CROSS_FILE = "cross-subject-shadow.json"
VARIANT_CATALOG_FILE = "variant-catalog.json"
FAILURE_SIGNATURE_FILE = "failure_signatures.json"
HOT_FILES = {
    "manifest.json",
    "catalog.json",
    "learner_state.json",
    "question_index.json",
    CROSS_FILE,
    VARIANT_CATALOG_FILE,
    FAILURE_SIGNATURE_FILE,
    "corpus/manifest.json",
    "corpus/indexes/<sha256>.json",
    "corpus/objects/<sha256>.json",
}
FORBIDDEN_HOT_PATHS = {
    "review-loop/events.jsonl",
    "review-loop/state.json",
    "intake-curation/events.jsonl",
    "intake-curation/state.json",
    "event_mappings.json",
    "知识点命中索引.md",
    "节点总表.md",
    "关系边表.md",
}
FAILURE_RESULTS = {"partial", "wrong", "uncertain"}
MODES = {"off", "shadow", "verified"}
PROTECTED_RE = re.compile(
    r"完整题干|完整解析|标准答案|正确答案|错选答案|我的答案|我的错选|"
    r"选项字母|(?:答案|应选|错选)\s*[:：是为]?\s*[A-DＡ-Ｄ]"
)


class ContextSliceError(RuntimeError):
    """A sanitized projection or selection failure."""

    def __init__(self, reason_code: str):
        super().__init__(reason_code)
        self.reason_code = reason_code


def _retired_write_surface() -> None:
    raise ContextSliceError(
        "personalized_context_writes_retired_read_only_compatibility"
    )


def _json_bytes(value: Any, *, pretty: bool = False) -> bytes:
    kwargs = {
        "ensure_ascii": False,
        "sort_keys": True,
    }
    if pretty:
        kwargs["indent"] = 2
    else:
        kwargs["separators"] = (",", ":")
    return (json.dumps(value, **kwargs) + "\n").encode("utf-8")


def _sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _atomic_write(path: Path, value: Any) -> str:
    _retired_write_surface()

    # Historical implementation retained below for read-only source compatibility.
    data = _json_bytes(value, pretty=True)
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)
    return _sha256_bytes(data)


@contextmanager
def _projection_lock(state_dir: Path) -> Iterator[None]:
    lock_path = state_dir / ".hot-path.lock"
    with lock_path.open("a+", encoding="utf-8") as handle:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def _read_json_checked(
    path: Path,
    expected_hash: str | None = None,
    *,
    max_bytes: int | None = None,
) -> dict[str, Any]:
    try:
        if path.is_symlink() or not path.is_file():
            raise ContextSliceError("missing_projection")
        size = path.stat().st_size
        if max_bytes is not None and (size <= 0 or size > max_bytes):
            raise ContextSliceError("projection_input_limit_exceeded")
        data = path.read_bytes()
        if len(data) != size or (max_bytes is not None and len(data) > max_bytes):
            raise ContextSliceError("projection_changed_during_read")
    except ContextSliceError:
        raise
    except OSError as exc:
        raise ContextSliceError("missing_projection") from exc
    if expected_hash and _sha256_bytes(data) != expected_hash:
        raise ContextSliceError("projection_hash_mismatch")
    try:
        value = json.loads(data)
    except json.JSONDecodeError as exc:
        raise ContextSliceError("corrupt_projection") from exc
    if not isinstance(value, dict):
        raise ContextSliceError("corrupt_projection")
    return value


def _today() -> str:
    return datetime.now(ZoneInfo("Asia/Shanghai")).date().isoformat()


def _valid_iso_date(value: Any) -> str | None:
    text = str(value or "")
    try:
        return date.fromisoformat(text).isoformat()
    except ValueError:
        return None


def _event_date(event: dict[str, Any]) -> str:
    observed = _valid_iso_date(event.get("observed_date"))
    if observed:
        return observed
    raw = str(event.get("event_time") or "")
    try:
        return datetime.fromisoformat(raw.replace("Z", "+00:00")).astimezone(
            ZoneInfo("Asia/Shanghai")
        ).date().isoformat()
    except ValueError:
        return _today()


def _age_days(last_date: str | None, current_date: str) -> int | None:
    if not _valid_iso_date(last_date):
        return None
    return (date.fromisoformat(current_date) - date.fromisoformat(last_date)).days


def _safe_source_ref(question: dict[str, Any]) -> str | None:
    source = question.get("reliable_source") or {}
    digest = str(source.get("source_sha256") or "")
    if not all(
        (
            source.get("original_question_eligible") is True,
            source.get("surface_verified") is True,
            source.get("answer_isolated") is True,
            source.get("source_ref_kind") == "question_asset_manifest",
            re.fullmatch(r"[0-9a-f]{64}", digest) is not None,
        )
    ):
        return None
    value = str(source.get("source_ref") or "").strip()
    if (
        not value.startswith("personalization-source:question_sources/")
        or not value.endswith(".json")
        or PROTECTED_RE.search(value)
    ):
        return None
    return value


def _source_sha256(question: dict[str, Any]) -> str:
    source = question.get("reliable_source") or {}
    existing = str(source.get("source_sha256") or "").strip()
    if re.fullmatch(r"[0-9a-f]{64}", existing):
        return existing
    return ""


def _practice_targets(
    question: dict[str, Any], mapping: dict[str, Any] | None = None
) -> dict[str, Any]:
    question_points = list(
        dict.fromkeys(
            str(value)
            for value in (
                list(question.get("primary_point_ids") or [])
                + list(question.get("point_ids") or [])
            )
            if str(value)
        )
    )
    mapped_points = list(
        dict.fromkeys(
            str(value)
            for value in ((mapping or {}).get("point_ids") or [])
            if str(value)
        )
    )
    if mapped_points:
        # A multi-point formal question must follow the point explicitly bound
        # by this learning event.  Falling back to the question's first primary
        # point can silently route a failure to a different mechanism.
        point = (
            mapped_points[0]
            if len(mapped_points) == 1
            and mapped_points[0] in set(question_points)
            else ""
        )
    else:
        point = question_points[0] if question_points else ""

    declared_mechanisms = list(
        dict.fromkeys(
            str(value)
            for value in (question.get("mechanism_ids") or [])
            if str(value)
        )
    )
    mapped_mechanisms = list(
        dict.fromkeys(
            str(value)
            for value in ((mapping or {}).get("mechanism_ids") or [])
            if str(value)
        )
    )
    if mapped_mechanisms:
        candidate = mapped_mechanisms[0] if len(mapped_mechanisms) == 1 else ""
        point_mechanism = f"KP:{point}" if point else ""
        compatible = bool(
            candidate
            and candidate in set(declared_mechanisms)
            and (
                not candidate.startswith("KP:")
                or candidate == point_mechanism
            )
        )
        mechanism = candidate if compatible else ""
    else:
        review_units = [
            value for value in declared_mechanisms if value.startswith("RU_")
        ]
        mechanism = review_units[0] if len(review_units) == 1 else (
            f"KP:{point}" if point and f"KP:{point}" in declared_mechanisms else ""
        )
    review_unit = mechanism if mechanism.startswith("RU_") else None
    return {
        "knowledge_point": point,
        "mechanism_key": mechanism,
        "review_unit_id": review_unit,
    }


def _canonical_variant_mechanism_keys(
    question: dict[str, Any], targets: dict[str, Any]
) -> tuple[str, ...]:
    """Return exact canonical aliases for the selected question target.

    A review-unit mechanism and the formal ``KP:<point>`` mechanism are
    equivalent for variant lookup only when both are explicitly present on the
    same formal question and the point is the selected primary point.  This is
    deliberately narrower than accepting an arbitrary mechanism from the same
    anchor.
    """

    point = str(targets.get("knowledge_point") or "")
    primary = f"KP:{point}" if point else ""
    selected = str(targets.get("mechanism_key") or "")
    review_unit = str(targets.get("review_unit_id") or "")
    declared = {
        str(value)
        for value in (question.get("mechanism_ids") or [])
        if str(value)
    }
    aliases = {selected} if selected else set()
    if primary and primary in declared:
        aliases.add(primary)
    if review_unit and review_unit in declared:
        aliases.add(review_unit)
    return tuple(sorted(aliases))


def _load_hot_projection(state_dir: Path, current_date: str) -> dict[str, Any]:
    manifest = _read_json_checked(state_dir / "manifest.json")
    if manifest.get("answer_safe") is not True:
        raise ContextSliceError("unsafe_projection")
    if int(manifest.get("formal_write_count", -1)) != 0:
        raise ContextSliceError("formal_write_boundary_violation")
    if manifest.get("as_of") != current_date:
        raise ContextSliceError("stale_projection")
    hashes = manifest.get("artifact_hashes") or {}
    catalog = _read_json_checked(
        state_dir / "catalog.json", hashes.get("catalog.json")
    )
    learner = _read_json_checked(
        state_dir / "learner_state.json", hashes.get("learner_state.json")
    )
    applied_event_ids = learner.get("applied_performance_event_ids")
    if (
        not isinstance(applied_event_ids, list)
        or any(not isinstance(value, str) or not value for value in applied_event_ids)
        or len(applied_event_ids) != len(set(applied_event_ids))
    ):
        raise ContextSliceError("projection_missing_exact_idempotency_state")
    selection_commitments = learner.get("selection_commitments")
    if (
        not isinstance(selection_commitments, dict)
        or any(
            not isinstance(key, str) or not isinstance(value, dict)
            for key, value in selection_commitments.items()
        )
    ):
        raise ContextSliceError("projection_missing_selection_commitments")
    questions = _read_json_checked(
        state_dir / "question_index.json", hashes.get("question_index.json")
    )
    signature_hash = hashes.get(FAILURE_SIGNATURE_FILE)
    if signature_hash:
        signatures = _read_json_checked(
            state_dir / FAILURE_SIGNATURE_FILE, signature_hash
        )
        if (
            signatures.get("schema") != SIGNATURE_SCHEMA
            or signatures.get("kind") != "recent_failure_signature_index"
            or signatures.get("answer_safe") is not True
            or int(signatures.get("formal_write_count", -1)) != 0
        ):
            raise ContextSliceError("failure_signature_boundary_violation")
    else:
        signatures = {
            "schema": SIGNATURE_SCHEMA,
            "kind": "recent_failure_signature_index",
            "as_of": current_date,
            "signatures": {},
            "by_key": {},
            "answer_safe": True,
            "formal_write_count": 0,
            "projection_status": "legacy_not_available",
        }
    variant_catalog = _read_json_checked(
        state_dir / VARIANT_CATALOG_FILE,
        hashes.get(VARIANT_CATALOG_FILE),
    )
    if (
        variant_catalog.get("schema") != "variant_source_catalog_v1"
        or int(variant_catalog.get("formal_write_count", -1)) != 0
        or manifest.get("variant_catalog_sha256")
        != hashes.get(VARIANT_CATALOG_FILE)
    ):
        raise ContextSliceError("variant_catalog_boundary_violation")
    cross: dict[str, Any] | None = None
    expected_cross_hash = manifest.get("cross_subject_shadow_sha256")
    cross_path = state_dir / CROSS_FILE
    if expected_cross_hash:
        cross_binding = (manifest.get("independent_projections") or {}).get(
            "cross_subject_shadow"
        )
        if (
            isinstance(cross_binding, dict)
            and cross_binding.get("included_in_this_build") is True
            and cross_binding.get("file") == CROSS_FILE
        ):
            cross = _read_json_checked(
                cross_path,
                str(expected_cross_hash),
                max_bytes=MAX_CROSS_PROJECTION_INPUT_BYTES,
            )
            records = cross.get("records")
            if (
                cross.get("schema_version") != "cross_subject_shadow_v1"
                or int(cross.get("formal_write_count", -1)) != 0
                or not isinstance(records, list)
                or len(records) > MAX_CROSS_PROJECTION_RECORDS
            ):
                raise ContextSliceError("cross_projection_boundary_violation")
    return {
        "manifest": manifest,
        "catalog": catalog,
        "learner_state": learner,
        "question_index": questions,
        "failure_signatures": signatures,
        "variant_catalog": variant_catalog,
        "cross_subject": cross,
    }


def _state_view(entity_kind: str, entity_id: str, state: dict[str, Any]) -> dict[str, Any]:
    failures = int(state.get("failure_count", 0))
    recent_failures = int(state.get("recent_7d_failure_count", 0))
    fragile = int(state.get("fragile_count", 0))
    stable = int(state.get("stable_retention_count", 0))
    return {
        "entity_kind": entity_kind,
        "entity_id": entity_id,
        "priority_score": recent_failures * 8 + failures * 2 + fragile - min(stable, 3),
        "outcome_count": int(state.get("outcome_count", 0)),
        "failure_count": failures,
        "recent_7d_failure_count": recent_failures,
        "recent_30d_failure_count": int(state.get("recent_30d_failure_count", 0)),
        "fragile_count": fragile,
        "stable_retention_count": stable,
        "last_outcome_date": state.get("last_outcome_date"),
        "last_failure_date": state.get("last_failure_date"),
        "last_stable_retention_date": state.get("last_stable_retention_date"),
        "recent_event_ids": list(state.get("evidence_event_ids", []))[-3:],
    }


def _rank_states(
    learner: dict[str, Any], mapping: dict[str, Any], limit: int = 3
) -> list[dict[str, Any]]:
    values: list[dict[str, Any]] = []
    for point_id in mapping.get("point_ids", []):
        state = learner.get("points", {}).get(point_id)
        if state:
            values.append(_state_view("point", point_id, state))
    for mechanism_id in mapping.get("mechanism_ids", []):
        state = learner.get("mechanisms", {}).get(mechanism_id)
        if state:
            values.append(_state_view("mechanism", mechanism_id, state))
    values.sort(
        key=lambda item: (
            -int(item["priority_score"]),
            0 if item["entity_kind"] == "point" else 1,
            item["entity_id"],
        )
    )
    return values[:limit]


def _cross_context(
    cross: dict[str, Any] | None, point_ids: list[str], limit: int = 2
) -> tuple[list[dict[str, Any]], dict[tuple[str, str], dict[str, Any]]]:
    if not cross:
        return [], {}
    by_id = {str(row.get("edge_id")): row for row in cross.get("records", [])}
    seen: set[str] = set()
    views: list[dict[str, Any]] = []
    lookup: dict[tuple[str, str], dict[str, Any]] = {}
    adjacency = cross.get("routing_adjacency") or {}
    for source_point in point_ids:
        for item in adjacency.get(source_point, []):
            edge_id = str(item.get("edge_id") or "")
            target = str(item.get("target_point_id") or "")
            row = by_id.get(edge_id, {})
            if not edge_id or not target:
                continue
            lookup[(source_point, target)] = row or dict(item)
            if edge_id in seen:
                continue
            seen.add(edge_id)
            views.append(
                {
                    "edge_id": edge_id,
                    "source_point_id": source_point,
                    "target_point_id": target,
                    "relation_kind": item.get("relation_kind"),
                    "relation_code": item.get("relation_code"),
                    "confidence": item.get("confidence"),
                    "activation": item.get("activation"),
                    "activation_eligible": bool(item.get("activation_eligible")),
                    "enabled_for_live_routing": bool(
                        row.get(
                            "enabled_for_live_routing",
                            item.get("enabled_for_live_routing", False),
                        )
                    ),
                    "lifecycle": row.get("lifecycle"),
                }
            )
    views.sort(
        key=lambda item: (
            0 if item["enabled_for_live_routing"] else 1,
            0 if item["activation_eligible"] else 1,
            0 if item["lifecycle"] == "verified_existing" else 1,
            item["edge_id"],
        )
    )
    return views[:limit], lookup


def _candidate_references(
    question_index: dict[str, Any],
    point_ids: list[str],
    mechanism_ids: list[str],
    cross_views: list[dict[str, Any]],
) -> dict[str, dict[str, Any]]:
    candidates: dict[str, dict[str, Any]] = {}

    def add(ref: dict[str, Any], basis: str, edge: dict[str, Any] | None = None) -> None:
        node_id = str(ref.get("node_id") or "")
        question = question_index.get("questions", {}).get(node_id)
        if not question:
            return
        row = candidates.setdefault(
            node_id,
            {
                "question": question,
                "bases": set(),
                "cross_edge": None,
            },
        )
        row["bases"].add(basis)
        if edge and (not row["cross_edge"] or edge.get("activation_eligible")):
            row["cross_edge"] = edge

    for point_id in point_ids:
        for ref in question_index.get("by_point", {}).get(point_id, []):
            add(ref, f"point:{point_id}")
    for mechanism_id in mechanism_ids:
        for ref in question_index.get("by_mechanism", {}).get(mechanism_id, []):
            add(ref, f"mechanism:{mechanism_id}")
    for edge in cross_views:
        target = str(edge.get("target_point_id") or "")
        for ref in question_index.get("by_point", {}).get(target, []):
            add(ref, f"cross:{edge['edge_id']}", edge=edge)
    return candidates


def _rank_question_candidates(
    candidates: dict[str, dict[str, Any]],
    *,
    current_question_ids: set[str],
    current_subjects: set[str],
    current_date: str,
) -> list[dict[str, Any]]:
    ranked: list[dict[str, Any]] = []
    for node_id, value in candidates.items():
        if node_id in current_question_ids:
            # The current question is handled by the exact-identity cooldown
            # branch.  Leaving it in the generic candidate list would make a
            # replay observe today's overlaid date instead of the pre-event
            # date and would destabilize the otherwise idempotent slice.
            continue
        question = value["question"]
        source_ref = _safe_source_ref(question)
        if not source_ref:
            continue
        last_date = question.get("effective_last_attempt_date")
        age = _age_days(last_date, current_date)
        due = age is None or age >= 7
        cross_edge = value.get("cross_edge")
        candidate_subject = str(question.get("subject") or "").strip()
        # Discovery provenance is not a safety boundary.  A cross-subject
        # secondary/hit point can also appear under a local point/mechanism
        # basis, so compare the actual current-question subject with the actual
        # candidate subject and fail closed when the anchor subject is unknown.
        cross_required = (
            len(current_subjects) != 1 or candidate_subject not in current_subjects
        )
        score = 0
        score += 80 if cross_edge and cross_edge.get("activation_eligible") else 0
        score += 25 if cross_edge else 0
        score += 20 if due else -30
        # An unknown date is eligible under the repository's 7-day rule, but
        # ranks below an equally strong candidate with explicit due evidence.
        score += 2 if due and age is not None else 0
        score += 8 if any(str(item).startswith("mechanism:") for item in value["bases"]) else 0
        score += 4 if any(str(item).startswith("point:") for item in value["bases"]) else 0
        ranked.append(
            {
                "node_id": node_id,
                "subject": question.get("subject"),
                "effective_last_attempt_date": last_date,
                "days_since_attempt": age,
                "cooldown_eligible": due,
                "source_status": (question.get("reliable_source") or {}).get("status"),
                "source_ref": source_ref,
                "source_sha256": _source_sha256(question),
                "source_id": (question.get("reliable_source") or {}).get("source_id")
                or node_id,
                **_practice_targets(question),
                "relation_bases": sorted(value["bases"]),
                "cross_edge": cross_edge,
                "cross_required": cross_required,
                "score": score,
            }
        )
    ranked.sort(key=lambda row: (-int(row["score"]), row["node_id"]))
    return ranked


def _current_question_subjects(
    event: dict[str, Any],
    mapping: dict[str, Any],
    question_index: dict[str, Any],
) -> set[str]:
    questions = question_index.get("questions", {})
    subjects: set[str] = set()

    # Prefer the explicit actual question identity.  Point mappings may include
    # secondary cross-subject hits and therefore are only a last-resort signal.
    explicit_ids = [
        event.get("actual_question_formal_node_id"),
        event.get("formal_node_id"),
    ]
    for value in explicit_ids:
        node_id = str(value or "").strip()
        question = questions.get(node_id, {})
        subject = str(question.get("subject") or "").strip()
        if not subject and re.match(r"^(DS|CO|OS|CN)_", node_id):
            subject = node_id[:2]
        if subject:
            subjects.add(subject)
    if subjects:
        return subjects

    for value in mapping.get("question_node_ids", []):
        node_id = str(value or "").strip()
        question = questions.get(node_id, {})
        subject = str(question.get("subject") or "").strip()
        if not subject and re.match(r"^(DS|CO|OS|CN)_", node_id):
            subject = node_id[:2]
        if subject:
            subjects.add(subject)
    if subjects:
        return subjects

    point_values = [event.get("knowledge_point"), *mapping.get("point_ids", [])]
    for value in point_values:
        match = re.search(r"\b(DS|CO|OS|CN)\d{2}-\d{2}\b", str(value or ""))
        if match:
            subjects.add(match.group(1))
    return subjects


def _selection_id(value: dict[str, Any]) -> str:
    stable = {
        key: value.get(key)
        for key in (
            "practice_mode",
            "formal_node_id",
            "anchor_formal_node_id",
            "source_id",
            "source_sha256",
            "anchor_source_sha256",
            "gate_decision",
            "cross_edge_id",
        )
    }
    return "PSEL-" + _sha256_bytes(_json_bytes(stable))[:16].upper()


def _selection_receipt_payload(
    *,
    result: dict[str, Any],
    selected: dict[str, Any],
    max_bytes: int,
) -> dict[str, Any]:
    return {
        "schema": SELECTION_RECEIPT_SCHEMA,
        "slice_id": result["slice_id"],
        "trigger_event_id": result["trigger_event_id"],
        "projection_id": result["projection_id"],
        "projection_build_id": result["projection_build_id"],
        "projection_manifest_sha256": result["manifest_sha256"],
        "mode": result["mode"],
        "budget_version": BUDGET_VERSION,
        "max_bytes": int(max_bytes),
        "selected_practice_sha256": _sha256_bytes(_json_bytes(selected)),
        "selected_practice": copy.deepcopy(selected),
        "candidate_id": selected.get("candidate_id"),
        "practice_mode": selected.get("practice_mode"),
        "formal_node_id": selected.get("formal_node_id"),
        "anchor_formal_node_id": selected.get("anchor_formal_node_id"),
        "source_id": selected.get("source_id"),
        "source_ref": selected.get("source_ref"),
        "source_sha256": selected.get("source_sha256"),
        "mechanism_key": selected.get("mechanism_key"),
        "knowledge_point": selected.get("knowledge_point"),
        "review_unit_id": selected.get("review_unit_id"),
        "gate_decision": selected.get("gate_decision"),
        "cross_edge_id": selected.get("cross_edge_id"),
        "cross_edge_live_enabled": bool(
            selected.get("cross_edge_live_enabled")
        ),
        "schedule_allowed": selected.get("schedule_allowed") is True,
        "formal_write_count": 0,
    }


def _candidate_live_routable(row: dict[str, Any]) -> bool:
    if not row.get("cross_required"):
        return True
    edge = row.get("cross_edge") or {}
    return bool(
        edge.get("lifecycle") == "verified_existing"
        and edge.get("activation_eligible")
        and edge.get("enabled_for_live_routing")
    )


def _verified_variant_for_anchor(
    variant_catalog: dict[str, Any],
    *,
    anchor_formal_node_id: str,
    targets: dict[str, Any],
    variant_cooldown: dict[str, Any],
    current_date: str,
) -> dict[str, Any] | None:
    source_ids = (variant_catalog.get("by_anchor") or {}).get(
        anchor_formal_node_id, []
    )
    entries = {
        str(item.get("source_id") or ""): item
        for item in variant_catalog.get("entries", [])
        if isinstance(item, dict)
    }
    eligible: list[dict[str, Any]] = []
    semantic_conflict = False
    target_mechanisms = {
        str(value)
        for value in (
            targets.get("canonical_mechanism_keys")
            or [targets.get("mechanism_key")]
        )
        if str(value or "")
    }
    target_review_unit = str(targets.get("review_unit_id") or "")
    for source_id in source_ids:
        entry = entries.get(str(source_id), {})
        if (
            entry.get("status") != "verified_ready"
            or entry.get("anchor_formal_node_id") != anchor_formal_node_id
            or not entry.get("source_ref")
            or not re.fullmatch(
                r"[0-9a-f]{64}", str(entry.get("source_sha256") or "")
            )
            or entry.get("knowledge_point") != targets.get("knowledge_point")
        ):
            continue
        entry_mechanism = str(entry.get("mechanism_key") or "")
        if target_mechanisms and entry_mechanism not in target_mechanisms:
            continue
        entry_review_unit = str(entry.get("review_unit_id") or "")
        if target_review_unit and entry_review_unit != target_review_unit:
            semantic_conflict = True
            continue
        cooldown_row = (variant_cooldown.get("variants") or {}).get(
            str(source_id), {}
        )
        last_displayed = (
            cooldown_row.get("last_displayed_date")
            if isinstance(cooldown_row, dict)
            else None
        )
        display_age = _age_days(last_displayed, current_date)
        if display_age is not None and display_age < 7:
            continue
        eligible.append(
            {
                **entry,
                "last_displayed_date": last_displayed,
                "days_since_display": display_age,
            }
        )
    if not target_review_unit and len(
        {
            str(item.get("review_unit_id") or "")
            for item in eligible
        }
    ) > 1:
        semantic_conflict = True
    if semantic_conflict or not eligible:
        return None
    eligible.sort(
        key=lambda item: (
            item.get("days_since_display") is not None,
            int(item.get("days_since_display") or -1),
            str(item.get("verified_at") or ""),
            str(item.get("source_id") or ""),
        ),
        reverse=True,
    )
    return eligible[0]


def _select_practice(
    *,
    event: dict[str, Any],
    mapping: dict[str, Any],
    pre_learner: dict[str, Any],
    pre_questions: dict[str, Any],
    variant_catalog: dict[str, Any],
    variant_cooldown: dict[str, Any],
    cross_views: list[dict[str, Any]],
    mode: str,
    current_date: str,
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    if str(event.get("first_result")) not in FAILURE_RESULTS:
        return {
            "status": "not_needed",
            "reason_code": "no_failure_signal",
            "activation": "none",
        }, []

    current_ids = set(mapping.get("question_node_ids", []))
    candidates = _candidate_references(
        pre_questions,
        list(mapping.get("point_ids", [])),
        list(mapping.get("mechanism_ids", [])),
        cross_views,
    )
    current_subjects = _current_question_subjects(event, mapping, pre_questions)
    ranked = _rank_question_candidates(
        candidates,
        current_question_ids=current_ids,
        current_subjects=current_subjects,
        current_date=current_date,
    )

    if len(current_ids) == 1:
        current_id = next(iter(current_ids))
        history = pre_learner.get("questions", {}).get(current_id, {})
        prior_failure = int(history.get("failure_count", 0)) > 0
        current_question = pre_questions.get("questions", {}).get(current_id, {})
        current_targets = _practice_targets(current_question, mapping)
        variant_targets = {
            **current_targets,
            "canonical_mechanism_keys": _canonical_variant_mechanism_keys(
                current_question, current_targets
            ),
        }
        previous_date = current_question.get("effective_last_attempt_date")
        age = _age_days(previous_date, current_date)
        source_ref = _safe_source_ref(current_question)
        actual_original_id = str(
            event.get("actual_question_formal_node_id")
            or (
                event.get("formal_node_id")
                if event.get("presentation_kind") == "original_question"
                or event.get("practice_mode") == "original"
                else ""
            )
            or ""
        )
        current_was_original = actual_original_id == current_id
        if (
            prior_failure
            and not current_was_original
            and source_ref
            and (age is None or age >= 7)
        ):
            selected = {
                "status": "selected",
                "practice_mode": "original",
                "formal_node_id": current_id,
                "anchor_formal_node_id": current_id,
                "source_id": (current_question.get("reliable_source") or {}).get("source_id")
                or current_id,
                "source_ref": source_ref,
                "source_sha256": _source_sha256(current_question),
                **current_targets,
                "gate_decision": "original_due_over_7d",
                "relation_basis": "same_question_prior_failure",
                "cross_edge_id": None,
                "last_attempt_before_current": previous_date,
                "days_since_attempt": age,
            }
            selected["activation"] = "verified_active" if mode == "verified" else "shadow_only"
            selected["schedule_allowed"] = mode == "verified"
            selected["candidate_id"] = _selection_id(selected)
            return selected, ranked[:3]

        if current_was_original or (
            prior_failure and age is not None and 0 <= age < 7
        ):
            mechanism = next(iter(mapping.get("mechanism_ids", [])), None)
            if source_ref:
                verified_variant = _verified_variant_for_anchor(
                    variant_catalog,
                    anchor_formal_node_id=current_id,
                    targets=variant_targets,
                    variant_cooldown=variant_cooldown,
                    current_date=current_date,
                )
                if verified_variant:
                    selected = {
                        "status": "selected",
                        "practice_mode": "variant",
                        "formal_node_id": None,
                        "anchor_formal_node_id": current_id,
                        "source_id": verified_variant["source_id"],
                        "source_ref": verified_variant["source_ref"],
                        "source_sha256": verified_variant["source_sha256"],
                        "knowledge_point": verified_variant["knowledge_point"],
                        "mechanism_key": verified_variant["mechanism_key"],
                        "review_unit_id": verified_variant.get("review_unit_id"),
                        "last_displayed_date": verified_variant.get(
                            "last_displayed_date"
                        ),
                        "days_since_display": verified_variant.get(
                            "days_since_display"
                        ),
                        "gate_decision": "recent_original_verified_variant",
                        "relation_basis": mechanism or "same_question_mechanism",
                        "cross_edge_id": None,
                        "last_attempt_before_current": previous_date,
                        "days_since_attempt": 0 if current_was_original else age,
                        "attempted_in_trigger_event": current_was_original,
                    }
                    selected["activation"] = (
                        "verified_active" if mode == "verified" else "shadow_only"
                    )
                    selected["schedule_allowed"] = mode == "verified"
                    selected["candidate_id"] = _selection_id(selected)
                    return selected, ranked[:3]
                selected = {
                    "status": "variant_request",
                    "reason_code": "independent_variant_surface_required",
                    "practice_mode": "variant",
                    "formal_node_id": None,
                    "anchor_formal_node_id": current_id,
                    "source_id": f"VARIANT_OF:{current_id}",
                    "anchor_source_ref": source_ref,
                    "anchor_source_sha256": _source_sha256(current_question),
                    **current_targets,
                    "gate_decision": "recent_original_grounded_variant",
                    "relation_basis": mechanism or "same_question_mechanism",
                    "cross_edge_id": None,
                    "last_attempt_before_current": previous_date,
                    "days_since_attempt": 0 if current_was_original else age,
                    "attempted_in_trigger_event": current_was_original,
                }
                selected["activation"] = "generation_required"
                selected["schedule_allowed"] = False
                selected["candidate_id"] = _selection_id(selected)
                return selected, ranked[:3]
            selected = {
                "status": "source_gap",
                "reason_code": "anchor_source_required_for_independent_variant",
                "practice_mode": "variant",
                "formal_node_id": None,
                "anchor_formal_node_id": current_id,
                "source_id": f"VARIANT_OF:{current_id}",
                **current_targets,
                "gate_decision": "recent_original_variant_source_gap",
                "relation_basis": mechanism or "same_question_mechanism",
                "cross_edge_id": None,
                "last_attempt_before_current": previous_date,
                "days_since_attempt": 0 if current_was_original else age,
                "attempted_in_trigger_event": current_was_original,
                "activation": "source_required",
                "schedule_allowed": False,
            }
            selected["candidate_id"] = _selection_id(selected)
            return selected, ranked[:3]

    fallback = next(
        (
            row
            for row in ranked
            if row["node_id"] not in current_ids and row["cooldown_eligible"]
            and (mode != "verified" or _candidate_live_routable(row))
        ),
        None,
    )
    if fallback:
        edge = fallback.get("cross_edge") or {}
        can_activate = _candidate_live_routable(fallback)
        selected = {
            "status": "selected",
            "practice_mode": "original",
            "formal_node_id": fallback["node_id"],
            "anchor_formal_node_id": next(iter(current_ids), None),
            "source_id": fallback["source_id"],
            "source_ref": fallback["source_ref"],
            "source_sha256": fallback["source_sha256"],
            "knowledge_point": fallback["knowledge_point"],
            "mechanism_key": fallback["mechanism_key"],
            "review_unit_id": fallback["review_unit_id"],
            "gate_decision": "related_question_due_over_7d",
            "relation_basis": fallback["relation_bases"],
            "cross_edge_id": edge.get("edge_id"),
            "cross_edge_live_enabled": bool(edge.get("enabled_for_live_routing")),
            "last_attempt_before_current": fallback["effective_last_attempt_date"],
            "days_since_attempt": fallback["days_since_attempt"],
        }
        selected["activation"] = (
            "verified_active" if mode == "verified" and can_activate else "shadow_only"
        )
        selected["schedule_allowed"] = mode == "verified" and can_activate
        selected["candidate_id"] = _selection_id(selected)
        return selected, ranked[:3]

    return {
        "status": "source_gap",
        "reason_code": "no_reliable_due_question_or_grounded_variant",
        "activation": "none",
    }, ranked[:3]


def _recent_evidence(states: list[dict[str, Any]], current_event_id: str) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = [
        {"event_id": current_event_id, "role": "current_canonical_first_answer"}
    ]
    seen = {current_event_id}
    for state in states:
        for event_id in reversed(state.get("recent_event_ids", [])):
            if event_id in seen:
                continue
            seen.add(event_id)
            result.append(
                {
                    "event_id": event_id,
                    "role": "bounded_prior_evidence",
                    "entity_id": state["entity_id"],
                }
            )
            if len(result) >= 3:
                return result
    return result


def _fit_result(result: dict[str, Any], max_bytes: int) -> dict[str, Any]:
    context = result["context"]
    trim_order = (
        "question_candidates",
        "cross_subject_context",
        "recent_evidence",
        "learner_states",
    )
    def settled_size() -> int:
        for _ in range(8):
            size = len(_json_bytes(result))
            if result.get("byte_count") == size:
                return size
            result["byte_count"] = size
        return len(_json_bytes(result))

    result["byte_count"] = 0
    while settled_size() > max_bytes:
        trimmed = False
        for key in trim_order:
            values = context.get(key)
            if isinstance(values, list) and values:
                values.pop()
                result["truncated"] = True
                trimmed = True
                break
        if not trimmed:
            corpus = context.get("nightly_curation_context")
            entries = corpus.get("entries") if isinstance(corpus, dict) else None
            if isinstance(entries, list) and entries:
                entries.pop()
                result["truncated"] = True
                trimmed = True
        if not trimmed:
            plan = context.get("response_plan") or {}
            for key, minimum in (
                ("background_context", 0),
                ("verified_cross_connections", 0),
                ("direct_historical_errors", 1),
                ("direct_recent_errors", 2),
                ("common_patterns", 1),
                ("evidence_gaps", 1),
            ):
                values = plan.get(key)
                if isinstance(values, list) and len(values) > minimum:
                    values.pop()
                    result["truncated"] = True
                    trimmed = True
                    break
        if not trimmed:
            selected = context.get("selected_practice") or {}
            for field in ("source_ref", "anchor_source_ref"):
                if selected.get(field):
                    selected[field] = None
                    selected[f"{field}_withheld_by_budget"] = True
                    result["truncated"] = True
                    trimmed = True
                    break
        if not trimmed:
            plan = context.get("response_plan") or {}
            if isinstance(plan, dict) and not plan.get("budget_compacted"):
                strategy = plan.get("explanation_strategy") or {}
                context["response_plan"] = {
                    "current_target": plan.get("current_target") or {},
                    "direct_recent_errors": [],
                    "direct_historical_errors": [],
                    "common_patterns": [],
                    "verified_cross_connections": [],
                    "explanation_strategy": {
                        "strategy": strategy.get("strategy"),
                        "personalization_claim_allowed": bool(
                            strategy.get("personalization_claim_allowed")
                        ),
                    },
                    "first_boundary_to_check": {},
                    "optional_practice_action": {"action": "none"},
                    "evidence_gaps": list(plan.get("evidence_gaps") or [])[:1],
                    "budget_compacted": True,
                }
                result["truncated"] = True
                trimmed = True
        if not trimmed and result.get("hot_files"):
            result["hot_files"] = []
            result["truncated"] = True
            trimmed = True
        if not trimmed:
            raise ContextSliceError("slice_budget_too_small")
    result["byte_count"] = settled_size()
    if result["byte_count"] > max_bytes:
        raise ContextSliceError("slice_budget_too_small")
    return result


def _updated_manifest(
    manifest: dict[str, Any],
    learner: dict[str, Any],
    questions: dict[str, Any],
    signatures: dict[str, Any],
    event_id: str,
) -> tuple[dict[str, Any], bytes, bytes, bytes]:
    learner_bytes = _json_bytes(learner, pretty=True)
    question_bytes = _json_bytes(questions, pretty=True)
    signature_bytes = _json_bytes(signatures, pretty=True)
    updated = copy.deepcopy(manifest)
    hashes = updated.setdefault("artifact_hashes", {})
    hashes["learner_state.json"] = _sha256_bytes(learner_bytes)
    hashes["question_index.json"] = _sha256_bytes(question_bytes)
    hashes[FAILURE_SIGNATURE_FILE] = _sha256_bytes(signature_bytes)
    updated["runtime_projection_id"] = _sha256_bytes(
        _json_bytes(
            {
                "build_id": updated.get("build_id"),
                "learner": hashes["learner_state.json"],
                "questions": hashes["question_index.json"],
                "failure_signatures": hashes[FAILURE_SIGNATURE_FILE],
                "variants": hashes.get(VARIANT_CATALOG_FILE),
            }
        )
    )
    updated["last_incremental_event_id"] = event_id
    counts = updated.setdefault("counts", {})
    counts["sparse_point_states"] = int(learner.get("point_state_count", 0))
    counts["sparse_mechanism_states"] = int(learner.get("mechanism_state_count", 0))
    counts["sparse_question_states"] = int(learner.get("question_state_count", 0))
    counts["recent_failure_signatures"] = int(
        signatures.get("signature_count", 0)
    )
    return updated, learner_bytes, question_bytes, signature_bytes


def _persist_overlay(
    state_dir: Path,
    *,
    old_manifest: dict[str, Any],
    new_manifest: dict[str, Any],
    learner: dict[str, Any],
    questions: dict[str, Any],
    signatures: dict[str, Any],
) -> None:
    _retired_write_surface()

    # Historical implementation retained below for read-only source compatibility.
    if old_manifest == new_manifest:
        return
    # Manifest is the commit marker.  A crash before its final replace yields a
    # detectable hash mismatch and the next hot path returns unavailable.
    _atomic_write(state_dir / "learner_state.json", learner)
    _atomic_write(state_dir / "question_index.json", questions)
    _atomic_write(state_dir / FAILURE_SIGNATURE_FILE, signatures)
    _atomic_write(state_dir / "manifest.json", new_manifest)


def context_slice(
    repo: str | Path,
    *,
    current_event: dict[str, Any],
    state_dir: str | Path | None = None,
    max_bytes: int = DEFAULT_MAX_BYTES,
    mode: str = "verified",
) -> dict[str, Any]:
    """Apply one event overlay and return one deterministic fixed-size slice."""

    root = Path(repo).resolve()
    state = Path(state_dir) if state_dir is not None else root / DEFAULT_STATE_RELATIVE
    if not state.is_absolute():
        state = root / state
    state = state.resolve()
    if not state.is_dir():
        raise ContextSliceError("missing_projection")
    if mode not in MODES:
        raise ContextSliceError("invalid_mode")
    if not MIN_MAX_BYTES <= int(max_bytes) <= MAX_MAX_BYTES:
        raise ContextSliceError("invalid_slice_budget")
    event_id = str(current_event.get("event_id") or "").strip()
    if not event_id:
        raise ContextSliceError("missing_event_id")
    current_date = _event_date(current_event)
    try:
        variant_cooldown, variant_cooldown_evidence = derive_variant_cooldown(
            runtime_dir=root / DEFAULT_VARIANT_RUNTIME_RELATIVE,
            as_of=current_date,
        )
    except MorningRuntimeError as exc:
        raise ContextSliceError("variant_cooldown_unavailable") from exc

    with _projection_lock(state):
        projection = _load_hot_projection(state, current_date)
        old_manifest = projection["manifest"]
        pre_learner = projection["learner_state"]
        pre_questions = projection["question_index"]
        pre_signatures = projection["failure_signatures"]
        learner = copy.deepcopy(pre_learner)
        questions = copy.deepcopy(pre_questions)
        try:
            overlay = apply_event_to_projection(
                current_event,
                catalog=projection["catalog"],
                learner_state=learner,
                question_index=questions,
                as_of=current_date,
            )
        except PersonalizationIndexError as exc:
            raise ContextSliceError("event_mapping_unavailable") from exc
        mapping = overlay["mapping"]
        event_is_new = overlay.get("status") != "already_applied"
        signatures = apply_event_to_signature_index(
            copy.deepcopy(pre_signatures),
            current_event,
            mapping,
            as_of=current_date,
        )
        states = _rank_states(learner, mapping)
        cross_views, _ = _cross_context(
            projection.get("cross_subject"), list(mapping.get("point_ids", []))
        )
        selected, ranked = _select_practice(
            event=current_event,
            mapping=mapping,
            pre_learner=pre_learner,
            pre_questions=pre_questions,
            variant_catalog=projection["variant_catalog"],
            variant_cooldown=variant_cooldown,
            cross_views=cross_views,
            mode=mode,
            current_date=current_date,
        )
        cached_selection = old_manifest.get("last_personalization_selection") or {}
        if (
            old_manifest.get("last_incremental_event_id") == event_id
            and cached_selection.get("event_id") == event_id
            and cached_selection.get("mode") == mode
            and cached_selection.get("budget_version") == BUDGET_VERSION
            and int(cached_selection.get("max_bytes", -1)) == int(max_bytes)
            and "variant_cooldown_snapshot_sha256" in cached_selection
            and cached_selection.get("variant_cooldown_snapshot_sha256")
            == variant_cooldown_evidence.get("snapshot_sha256")
            and isinstance(cached_selection.get("selected_practice"), dict)
        ):
            selected = copy.deepcopy(cached_selection["selected_practice"])
        selection_commitment_changed = False
        if (
            mode == "verified"
            and selected.get("status") == "selected"
            and selected.get("schedule_allowed") is True
        ):
            selected_digest = _sha256_bytes(_json_bytes(selected))
            commitment_key = selection_commitment_key(
                event_id, mode, BUDGET_VERSION, int(max_bytes)
            )
            commitment = {
                "trigger_event_id": event_id,
                "mode": mode,
                "budget_version": BUDGET_VERSION,
                "max_bytes": int(max_bytes),
                "selected_practice_sha256": selected_digest,
            }
            commitments = learner.setdefault("selection_commitments", {})
            existing_commitment = commitments.get(commitment_key)
            if existing_commitment is not None and existing_commitment != commitment:
                raise ContextSliceError("selection_commitment_conflict")
            if existing_commitment is None:
                commitments[commitment_key] = commitment
                selection_commitment_changed = True
        projection_changed = event_is_new or selection_commitment_changed
        if projection_changed:
            new_manifest, _, _, _ = _updated_manifest(
                old_manifest, learner, questions, signatures, event_id
            )
            new_manifest["last_personalization_selection"] = {
                "event_id": event_id,
                "mode": mode,
                "budget_version": BUDGET_VERSION,
                "max_bytes": int(max_bytes),
                "variant_cooldown_snapshot_sha256": variant_cooldown_evidence.get(
                    "snapshot_sha256"
                ),
                "selected_practice": copy.deepcopy(selected),
            }
        else:
            new_manifest = old_manifest
            signatures = pre_signatures
        projection_id = str(
            new_manifest.get("runtime_projection_id") or new_manifest.get("build_id")
        )
        context = {
            "current_targets": {
                "mapping_status": mapping.get("mapping_status"),
                "point_ids": list(mapping.get("point_ids", []))[:3],
                "mechanism_ids": list(mapping.get("mechanism_ids", []))[:3],
                "question_node_ids": list(mapping.get("question_node_ids", []))[:2],
                "unresolved_reasons": list(mapping.get("unresolved_reasons", []))[:3],
            },
            "learner_states": states,
            "recent_evidence": _recent_evidence(states, event_id),
            "cross_subject_context": cross_views,
            "question_candidates": [
                {
                    key: row.get(key)
                    for key in (
                        "node_id",
                        "subject",
                        "effective_last_attempt_date",
                        "days_since_attempt",
                        "cooldown_eligible",
                        "source_status",
                        "relation_bases",
                        "cross_required",
                    )
                }
                for row in ranked
            ],
            "selected_practice": selected,
        }
        current_signature = next(
            (
                row
                for row in (signatures.get("signatures") or {}).values()
                if (row.get("evidence") or {}).get("event_id") == event_id
            ),
            {},
        )
        signature_targets = {
            "subjects": sorted(_current_question_subjects(current_event, mapping, questions)),
            "point_ids": list(mapping.get("point_ids", [])),
            "mechanism_ids": list(mapping.get("mechanism_ids", [])),
            "mechanism_primitive_ids": list(
                current_signature.get("mechanism_primitives", [])
            ),
            "formal_node_ids": list(mapping.get("formal_node_ids", [])),
            "module_ids": [
                point_id[:4] for point_id in mapping.get("point_ids", [])
            ],
            "error_pattern_ids": list(current_signature.get("error_patterns", [])),
            "method_ids": list(current_signature.get("method_templates", [])),
        }
        recent_direct, recent_background = match_recent_signatures(
            signatures, signature_targets
        )
        nightly_corpus = _optional_nightly_corpus(
            state, signature_targets, old_manifest
        )
        historical_direct = match_historical_signatures(
            list(nightly_corpus.get("entries") or []), signature_targets
        )
        context["nightly_curation_context"] = _public_corpus_context(
            nightly_corpus
        )
        context["response_plan"] = build_response_plan(
            targets=signature_targets,
            recent_direct=recent_direct,
            recent_background=recent_background,
            historical_direct=historical_direct,
            cross_rows=cross_views,
            question_candidates=ranked,
            corpus_status=str(nightly_corpus.get("status") or "not_available"),
        )
        slice_material = {
            "event_id": event_id,
            "projection_id": projection_id,
            "projection_build_id": str(new_manifest.get("build_id") or ""),
            "budget_version": BUDGET_VERSION,
            "mode": mode,
            "context": context,
        }
        result = {
            "schema": SCHEMA,
            "status": "ready" if mode != "off" else "disabled",
            "slice_id": "PS-" + _sha256_bytes(_json_bytes(slice_material))[:20].upper(),
            "trigger_event_id": event_id,
            "projection_id": projection_id,
            "projection_build_id": str(new_manifest.get("build_id") or ""),
            "manifest_sha256": _sha256_bytes(_json_bytes(new_manifest, pretty=True)),
            "mode": mode,
            "update_mode": overlay.get("update_mode"),
            "truncated": False,
            "byte_count": 0,
            "formal_write_count": 0,
            "compatibility_read_only": True,
            "hot_files": sorted(HOT_FILES),
            "context": context if mode != "off" else None,
        }
        if mode == "off":
            result["byte_count"] = len(_json_bytes(result))
        else:
            result = _fit_result(result, int(max_bytes))
        return result


def safe_context_slice(
    repo: str | Path,
    *,
    current_event: dict[str, Any],
    state_dir: str | Path | None = None,
    max_bytes: int = DEFAULT_MAX_BYTES,
    mode: str = "verified",
) -> dict[str, Any]:
    """Fail-soft wrapper used by answer-turn; never expose an exception string."""

    event_id = str(current_event.get("event_id") or "") or None
    try:
        return context_slice(
            repo,
            current_event=current_event,
            state_dir=state_dir,
            max_bytes=max_bytes,
            mode=mode,
        )
    except ContextSliceError as exc:
        return {
            "schema": SCHEMA,
            "status": "unavailable",
            "reason_code": exc.reason_code,
            "slice_id": None,
            "trigger_event_id": event_id,
            "context": None,
            "formal_write_count": 0,
        }
    except Exception:
        return {
            "schema": SCHEMA,
            "status": "unavailable",
            "reason_code": "internal_projection_error",
            "slice_id": None,
            "trigger_event_id": event_id,
            "context": None,
            "formal_write_count": 0,
        }


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo", type=Path, default=Path.cwd())
    parser.add_argument("--event-json", type=Path, required=True)
    parser.add_argument("--state-dir", type=Path)
    parser.add_argument("--max-bytes", type=int, default=DEFAULT_MAX_BYTES)
    parser.add_argument("--mode", choices=sorted(MODES), default="verified")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    try:
        event = json.loads(args.event_json.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        print(
            json.dumps(
                {
                    "schema": SCHEMA,
                    "status": "unavailable",
                    "reason_code": "invalid_event_input",
                },
                ensure_ascii=False,
                sort_keys=True,
            )
        )
        return 2
    result = safe_context_slice(
        args.repo,
        current_event=event,
        state_dir=args.state_dir,
        max_bytes=args.max_bytes,
        mode=args.mode,
    )
    print(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True))
    return 0 if result.get("status") != "unavailable" else 2


if __name__ == "__main__":
    sys.exit(main())
