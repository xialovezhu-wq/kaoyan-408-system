#!/usr/bin/env python3
"""Bounded, read-only personalization lookup before any 408 reply.

Callers provide an answer-safe target envelope instead of a canonical post-answer
event.  The lookup reads only the manifest-bound personalization projection and
returns controlled IDs, counters and routing hints.  It never scans a ledger,
formal table, vault, reference book or question corpus and never mutates state.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
from datetime import date
from pathlib import Path
from typing import Any

from intake_lib_408 import scan_leak
from personalization_commonality_408 import (
    SIGNATURE_SCHEMA,
    build_response_plan,
    match_historical_signatures,
    match_recent_signatures,
    match_verified_cross_recent_signatures,
)


SCHEMA = "personalization_preanswer_slice_v1"
ENVELOPE_SCHEMA = "personalization_target_envelope_v1"
PROJECTION_SCHEMA = "personalization_index_v1"
CORPUS_SCHEMA = "personalization_corpus_v1"
CORPUS_MANIFEST_KIND = "nightly_curation_corpus_manifest"
CORPUS_INDEX_KIND = "nightly_curation_corpus_index"
CORPUS_ITEM_KIND = "nightly_curation_corpus_item"
FAILURE_SIGNATURE_FILE = "failure_signatures.json"
DEFAULT_MAX_BYTES = 12_288
MIN_MAX_BYTES = 1_024
MAX_MAX_BYTES = 65_536
DEFAULT_STATE_RELATIVE = Path(
    "wiki/study_vaults/408-full/state/personalization"
)

CONVERSATION_KINDS = {
    "current_question",
    "past_exam_review",
    "morning_review",
    "evening_review",
    "chapter_preview",
    "wrong_intake",
    "knowledge_explanation",
    "general_408",
}
SUBJECTS = {"DS", "CO", "OS", "CN"}
ALLOWED_ENVELOPE_KEYS = {
    "schema",
    "request_id",
    "conversation_kind",
    "as_of",
    "subject",
    "point_ids",
    "mechanism_ids",
    "mechanism_primitive_ids",
    "formal_node_ids",
    "module_ids",
    "safe_topic_terms",
    "error_pattern_ids",
    "method_ids",
    "answer_protection",
}
PROTECTED_ENVELOPE_KEYS = {
    "question",
    "stem",
    "options",
    "answer",
    "solution",
    "analysis",
    "learner_response",
    "full_response",
    "feedback",
    "first_break",
    "image",
    "attachment",
}
POINT_RE = re.compile(r"^(DS|CO|OS|CN)\d{2}-\d{2}$")
MODULE_RE = re.compile(r"^(DS|CO|OS|CN)\d{2}$")
FORMAL_NODE_RE = re.compile(r"^(DS|CO|OS|CN)_(?:\d{4}|UNK)_\d{3}$")
SAFE_ID_RE = re.compile(r"^[A-Za-z0-9_.:@/-]{1,160}$")


class PreanswerContextError(RuntimeError):
    """Controlled error whose message is safe to expose as a reason code."""


def _json_bytes(value: Any) -> bytes:
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


def _age_days(last_date: Any, current_date: str) -> int | None:
    parsed = _valid_date(last_date)
    if parsed is None:
        return None
    return (date.fromisoformat(current_date) - date.fromisoformat(parsed)).days


def _read_json_checked(path: Path, expected_hash: str | None = None) -> dict[str, Any]:
    try:
        raw = path.read_bytes()
    except FileNotFoundError as exc:
        raise PreanswerContextError("missing_projection") from exc
    if expected_hash and _sha256_bytes(raw) != expected_hash:
        raise PreanswerContextError("projection_hash_mismatch")
    try:
        value = json.loads(raw)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise PreanswerContextError("invalid_projection") from exc
    if not isinstance(value, dict):
        raise PreanswerContextError("invalid_projection")
    return value


def _load_projection(state_dir: Path, as_of: str) -> dict[str, Any]:
    manifest = _read_json_checked(state_dir / "manifest.json")
    if manifest.get("schema") != PROJECTION_SCHEMA:
        raise PreanswerContextError("invalid_projection_schema")
    if manifest.get("kind") != "personalization_manifest":
        raise PreanswerContextError("invalid_projection")
    if manifest.get("answer_safe") is not True:
        raise PreanswerContextError("projection_not_answer_safe")
    if manifest.get("formal_write_count") != 0:
        raise PreanswerContextError("projection_formal_write_violation")
    if manifest.get("as_of") != as_of:
        raise PreanswerContextError("stale_projection")

    hashes = manifest.get("artifact_hashes")
    if not isinstance(hashes, dict):
        raise PreanswerContextError("invalid_projection")
    required = ("catalog.json", "learner_state.json", "question_index.json")
    values: dict[str, dict[str, Any]] = {"manifest": manifest}
    for name in required:
        expected = hashes.get(name)
        if not isinstance(expected, str) or len(expected) != 64:
            raise PreanswerContextError("invalid_projection")
        values[name] = _read_json_checked(state_dir / name, expected)

    cross_hash = manifest.get("cross_subject_shadow_sha256")
    if cross_hash is not None:
        if not isinstance(cross_hash, str) or len(cross_hash) != 64:
            raise PreanswerContextError("invalid_projection")
        values["cross-subject-shadow.json"] = _read_json_checked(
            state_dir / "cross-subject-shadow.json", cross_hash
        )
    else:
        values["cross-subject-shadow.json"] = {}

    signature_hash = hashes.get(FAILURE_SIGNATURE_FILE)
    if signature_hash is not None:
        if not isinstance(signature_hash, str) or len(signature_hash) != 64:
            raise PreanswerContextError("invalid_projection")
        signatures = _read_json_checked(
            state_dir / FAILURE_SIGNATURE_FILE, signature_hash
        )
        if (
            signatures.get("schema") != SIGNATURE_SCHEMA
            or signatures.get("kind") != "recent_failure_signature_index"
            or signatures.get("answer_safe") is not True
            or signatures.get("formal_write_count") != 0
        ):
            raise PreanswerContextError("invalid_failure_signature_projection")
        values[FAILURE_SIGNATURE_FILE] = signatures
    else:
        values[FAILURE_SIGNATURE_FILE] = {
            "schema": SIGNATURE_SCHEMA,
            "kind": "recent_failure_signature_index",
            "as_of": as_of,
            "signatures": {},
            "by_key": {},
            "answer_safe": True,
            "formal_write_count": 0,
            "projection_status": "legacy_not_available",
        }

    catalog = values["catalog.json"]
    learner = values["learner_state.json"]
    questions = values["question_index.json"]
    if catalog.get("kind") != "static_point_catalog":
        raise PreanswerContextError("invalid_projection")
    if learner.get("kind") != "sparse_learner_state":
        raise PreanswerContextError("invalid_projection")
    if questions.get("kind") != "question_inverted_index":
        raise PreanswerContextError("invalid_projection")
    return values


def _bounded_string_list(
    value: Any,
    *,
    maximum: int,
    pattern: re.Pattern[str] | None = None,
    max_length: int = 80,
) -> list[str]:
    if value is None:
        return []
    if not isinstance(value, list) or len(value) > maximum:
        raise PreanswerContextError("invalid_target_envelope")
    result: list[str] = []
    for item in value:
        if not isinstance(item, str):
            raise PreanswerContextError("invalid_target_envelope")
        normalized = item.strip()
        if not normalized or len(normalized) > max_length:
            raise PreanswerContextError("invalid_target_envelope")
        if pattern is not None and not pattern.fullmatch(normalized):
            raise PreanswerContextError("invalid_target_envelope")
        if normalized not in result:
            result.append(normalized)
    return result


def _validate_envelope(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise PreanswerContextError("invalid_target_envelope")
    keys = set(value)
    if keys & PROTECTED_ENVELOPE_KEYS or keys - ALLOWED_ENVELOPE_KEYS:
        raise PreanswerContextError("protected_content_in_envelope")
    if value.get("schema") != ENVELOPE_SCHEMA:
        raise PreanswerContextError("invalid_target_envelope")
    kind = value.get("conversation_kind")
    if kind not in CONVERSATION_KINDS:
        raise PreanswerContextError("invalid_target_envelope")
    as_of = _valid_date(value.get("as_of"))
    if as_of is None:
        raise PreanswerContextError("invalid_target_envelope")
    subject = value.get("subject")
    if subject is not None and subject not in SUBJECTS:
        raise PreanswerContextError("invalid_target_envelope")
    request_id = value.get("request_id")
    if request_id is not None and (
        not isinstance(request_id, str) or not SAFE_ID_RE.fullmatch(request_id)
    ):
        raise PreanswerContextError("invalid_target_envelope")
    if value.get("answer_protection", True) is not True:
        raise PreanswerContextError("answer_protection_required")

    return {
        "schema": ENVELOPE_SCHEMA,
        "request_id": request_id,
        "conversation_kind": kind,
        "as_of": as_of,
        "subject": subject,
        "point_ids": _bounded_string_list(
            value.get("point_ids"), maximum=8, pattern=POINT_RE
        ),
        "mechanism_ids": _bounded_string_list(
            value.get("mechanism_ids"), maximum=8, pattern=SAFE_ID_RE, max_length=160
        ),
        "mechanism_primitive_ids": _bounded_string_list(
            value.get("mechanism_primitive_ids"),
            maximum=8,
            pattern=SAFE_ID_RE,
            max_length=160,
        ),
        "formal_node_ids": _bounded_string_list(
            value.get("formal_node_ids"), maximum=4, pattern=FORMAL_NODE_RE
        ),
        "module_ids": _bounded_string_list(
            value.get("module_ids"), maximum=4, pattern=MODULE_RE
        ),
        "safe_topic_terms": _bounded_string_list(
            value.get("safe_topic_terms"), maximum=8, max_length=40
        ),
        "error_pattern_ids": _bounded_string_list(
            value.get("error_pattern_ids"),
            maximum=6,
            pattern=SAFE_ID_RE,
            max_length=80,
        ),
        "method_ids": _bounded_string_list(
            value.get("method_ids"),
            maximum=6,
            pattern=SAFE_ID_RE,
            max_length=80,
        ),
        "answer_protection": True,
    }


def _resolve_targets(
    envelope: dict[str, Any],
    catalog: dict[str, Any],
    question_index: dict[str, Any],
) -> dict[str, Any]:
    points = catalog.get("points") if isinstance(catalog.get("points"), dict) else {}
    modules = (
        catalog.get("modules") if isinstance(catalog.get("modules"), dict) else {}
    )
    names = (
        catalog.get("name_index")
        if isinstance(catalog.get("name_index"), dict)
        else {}
    )
    questions = (
        question_index.get("questions")
        if isinstance(question_index.get("questions"), dict)
        else {}
    )
    mechanisms = (
        question_index.get("mechanisms")
        if isinstance(question_index.get("mechanisms"), dict)
        else {}
    )

    point_ids = [item for item in envelope["point_ids"] if item in points]
    mechanism_ids = [
        item for item in envelope["mechanism_ids"] if item in mechanisms
    ]
    module_ids = [item for item in envelope["module_ids"] if item in modules]
    formal_node_ids = [
        item for item in envelope["formal_node_ids"] if item in questions
    ]
    resolution_sources: set[str] = set()
    if point_ids:
        resolution_sources.add("point_id")
    if mechanism_ids:
        resolution_sources.add("mechanism_id")
    if module_ids:
        resolution_sources.add("module_id")
    if formal_node_ids:
        resolution_sources.add("formal_node_id")

    for node_id in formal_node_ids:
        row = questions[node_id]
        for point_id in row.get("point_ids", []):
            if point_id in points and point_id not in point_ids and len(point_ids) < 8:
                point_ids.append(point_id)
        for mechanism_id in row.get("mechanism_ids", []):
            if (
                mechanism_id in mechanisms
                and mechanism_id not in mechanism_ids
                and len(mechanism_ids) < 8
            ):
                mechanism_ids.append(mechanism_id)

    for term in envelope["safe_topic_terms"]:
        matches = names.get(term, [])
        if not isinstance(matches, list):
            continue
        for point_id in matches:
            if point_id in points and point_id not in point_ids and len(point_ids) < 8:
                point_ids.append(point_id)
                resolution_sources.add("exact_safe_term")

    for point_id in list(point_ids):
        mechanism_id = f"KP:{point_id}"
        if (
            mechanism_id in mechanisms
            and mechanism_id not in mechanism_ids
            and len(mechanism_ids) < 8
        ):
            mechanism_ids.append(mechanism_id)

    inferred_subjects: set[str] = set()
    for point_id in point_ids:
        row = points.get(point_id, {})
        if row.get("subject") in SUBJECTS:
            inferred_subjects.add(row["subject"])
    for node_id in formal_node_ids:
        row = questions.get(node_id, {})
        if row.get("subject") in SUBJECTS:
            inferred_subjects.add(row["subject"])
    for module_id in module_ids:
        row = modules.get(module_id, {})
        if row.get("subject") in SUBJECTS:
            inferred_subjects.add(row["subject"])
    if envelope["subject"]:
        inferred_subjects.add(envelope["subject"])

    point_views = []
    for point_id in point_ids[:6]:
        row = points[point_id]
        point_views.append(
            {
                "point_id": point_id,
                "label": row.get("label") or f"{point_id} {row.get('name', '')}".strip(),
                "subject": row.get("subject"),
                "module_id": row.get("module_id"),
            }
        )

    return {
        "subjects": sorted(inferred_subjects),
        "point_ids": point_ids[:8],
        "mechanism_ids": mechanism_ids[:8],
        "mechanism_primitive_ids": list(
            envelope.get("mechanism_primitive_ids", [])
        ),
        "formal_node_ids": formal_node_ids[:4],
        "module_ids": module_ids[:4],
        "point_views": point_views,
        "resolution_sources": sorted(resolution_sources),
        "error_pattern_ids": list(envelope.get("error_pattern_ids", [])),
        "method_ids": list(envelope.get("method_ids", [])),
    }


def _state_score(state: dict[str, Any]) -> tuple[Any, ...]:
    return (
        int(state.get("recent_7d_failure_count") or 0),
        int(state.get("recent_30d_failure_count") or 0),
        int(state.get("failure_count") or 0),
        int(state.get("fragile_count") or 0),
        -int(state.get("stable_retention_count") or 0),
        str(state.get("last_failure_date") or ""),
        str(state.get("id") or ""),
    )


def _state_view(
    kind: str,
    entity_id: str,
    state: dict[str, Any],
    *,
    match_basis: str,
) -> dict[str, Any]:
    direct = match_basis in {"exact_question", "exact_point", "same_mechanism"}
    return {
        "entity_kind": kind,
        "entity_id": entity_id,
        "match_basis": match_basis,
        "evidence_strength": "strong" if direct else "weak",
        "personalization_eligible": direct,
        "recent_7d_failure_count": int(state.get("recent_7d_failure_count") or 0),
        "recent_30d_failure_count": int(state.get("recent_30d_failure_count") or 0),
        "failure_count": int(state.get("failure_count") or 0),
        "fragile_count": int(state.get("fragile_count") or 0),
        "stable_retention_count": int(state.get("stable_retention_count") or 0),
        "last_attempt_date": _valid_date(state.get("last_attempt_date")),
        "last_failure_date": _valid_date(state.get("last_failure_date")),
        "last_stable_retention_date": _valid_date(
            state.get("last_stable_retention_date")
        ),
        "evidence_event_ids": [
            item
            for item in state.get("evidence_event_ids", [])[-3:]
            if isinstance(item, str) and SAFE_ID_RE.fullmatch(item)
        ],
    }


def _learner_states(
    targets: dict[str, Any], learner: dict[str, Any]
) -> list[dict[str, Any]]:
    point_states = learner.get("points") if isinstance(learner.get("points"), dict) else {}
    mechanism_states = (
        learner.get("mechanisms")
        if isinstance(learner.get("mechanisms"), dict)
        else {}
    )
    question_states = (
        learner.get("questions")
        if isinstance(learner.get("questions"), dict)
        else {}
    )
    rows: list[tuple[str, str, dict[str, Any], str]] = []
    for entity_id in targets["point_ids"]:
        if entity_id in point_states:
            rows.append(("point", entity_id, point_states[entity_id], "exact_point"))
    for entity_id in targets["mechanism_ids"]:
        if entity_id in mechanism_states:
            rows.append(
                ("mechanism", entity_id, mechanism_states[entity_id], "same_mechanism")
            )
    for entity_id in targets["formal_node_ids"]:
        if entity_id in question_states:
            rows.append(
                ("question", entity_id, question_states[entity_id], "exact_question")
            )

    if not rows:
        subjects = set(targets["subjects"])
        modules = set(targets["module_ids"]) | {
            point_id[:4] for point_id in targets.get("point_ids", [])
        }
        for entity_id, state in point_states.items():
            if not isinstance(state, dict):
                continue
            if modules and entity_id[:4] in modules:
                rows.append(("point", entity_id, state, "module_fallback"))
            elif subjects and entity_id[:2] in subjects:
                rows.append(("point", entity_id, state, "subject_fallback"))
        for entity_id, state in mechanism_states.items():
            if not isinstance(state, dict) or not entity_id.startswith("KP:"):
                continue
            point_id = entity_id[3:]
            if modules and point_id[:4] in modules:
                rows.append(("mechanism", entity_id, state, "module_fallback"))
            elif subjects and point_id[:2] in subjects:
                rows.append(("mechanism", entity_id, state, "subject_fallback"))

    kind_rank = {"point": 2, "mechanism": 1, "question": 0}
    rows.sort(
        key=lambda row: (
            row[3] in {"exact_question", "exact_point", "same_mechanism"},
            *_state_score(row[2])[:-1],
            kind_rank[row[0]],
            row[1],
        ),
        reverse=True,
    )
    seen: set[tuple[str, str]] = set()
    result: list[dict[str, Any]] = []
    for kind, entity_id, state, match_basis in rows:
        key = (kind, entity_id)
        if key in seen:
            continue
        seen.add(key)
        result.append(
            _state_view(kind, entity_id, state, match_basis=match_basis)
        )
        if len(result) == 3:
            break
    return result


def _cross_context(
    targets: dict[str, Any],
    cross: dict[str, Any],
    catalog: dict[str, Any],
    *,
    mode: str,
) -> list[dict[str, Any]]:
    if mode == "off":
        return []
    adjacency = (
        cross.get("routing_adjacency")
        if isinstance(cross.get("routing_adjacency"), dict)
        else {}
    )
    records = cross.get("records") if isinstance(cross.get("records"), list) else []
    by_id = {
        row.get("edge_id") or row.get("id"): row
        for row in records
        if isinstance(row, dict) and (row.get("edge_id") or row.get("id"))
    }
    points = catalog.get("points") if isinstance(catalog.get("points"), dict) else {}
    current_subjects = set(targets["subjects"])
    rows: list[dict[str, Any]] = []
    seen: set[str] = set()
    for point_id in targets["point_ids"]:
        for summary in adjacency.get(point_id, []):
            if not isinstance(summary, dict):
                continue
            edge_id = summary.get("edge_id")
            record = by_id.get(edge_id, summary)
            target_id = summary.get("target_point_id") or record.get("target_point_id")
            if not isinstance(edge_id, str) or edge_id in seen or target_id not in points:
                continue
            target_subject = points[target_id].get("subject")
            if target_subject in current_subjects:
                continue
            # Activation is a runtime state (for example ``verified_live``),
            # whereas lifecycle is the stable evidence class.  Live routing
            # must be grounded in an existing verified relation regardless of
            # the activation label used by the projection writer.
            verified = record.get("lifecycle") == "verified_existing"
            eligible = record.get("activation_eligible") is True
            enabled = record.get("enabled_for_live_routing") is True
            live = mode == "verified" and verified and eligible and enabled
            strength = (record.get("relation") or {}).get("strength") or record.get(
                "strength"
            )
            rows.append(
                {
                    "edge_id": edge_id,
                    "source_point_id": point_id,
                    "target_point_id": target_id,
                    "target_label": points[target_id].get("label"),
                    "target_subject": target_subject,
                    "relation_code": record.get("relation_code"),
                    "strength": strength,
                    "confidence": record.get("confidence"),
                    "lifecycle": record.get("lifecycle") or record.get("activation"),
                    "activation_eligible": eligible,
                    "enabled_for_live_routing": enabled,
                    "live_routable": live,
                    "use_scope": (
                        "verified_question_routing" if live else "reply_context_only"
                    ),
                }
            )
            seen.add(edge_id)
    strength_rank = {"强": 2, "中": 1, "弱": 0}
    rows.sort(
        key=lambda row: (
            row["live_routable"],
            row["activation_eligible"],
            strength_rank.get(row.get("strength"), -1),
            row["edge_id"],
        ),
        reverse=True,
    )
    return rows[:2]


def _corpus_object_path(object_ref: Any, object_sha256: Any) -> str | None:
    if not isinstance(object_ref, str) or not isinstance(object_sha256, str):
        return None
    if not re.fullmatch(r"[0-9a-f]{64}", object_sha256):
        return None
    relative = object_ref.rsplit(":", 1)[-1]
    expected = f"objects/{object_sha256}.json"
    return relative if relative == expected else None


def _safe_text(value: Any, *, limit: int = 320) -> str | None:
    if not isinstance(value, str):
        return None
    normalized = " ".join(value.split())
    if not normalized:
        return None
    return normalized[:limit]


def _safe_text_list(value: Any, *, maximum: int = 6, limit: int = 160) -> list[str]:
    if isinstance(value, str):
        item = _safe_text(value, limit=limit)
        return [item] if item else []
    if not isinstance(value, list):
        return []
    result: list[str] = []
    for raw in value[:maximum]:
        item = _safe_text(raw, limit=limit)
        if item and item not in result:
            result.append(item)
    return result


def _safe_fact_value(value: Any) -> Any:
    if isinstance(value, str):
        return _safe_text(value)
    if isinstance(value, list):
        return _safe_text_list(value)
    if not isinstance(value, dict):
        return None
    allowed = {
        "value",
        "label",
        "status",
        "provenance",
        "provenance_kind",
        "observed",
    }
    result: dict[str, Any] = {}
    for key in sorted(set(value) & allowed):
        if key == "observed" and isinstance(value[key], bool):
            result[key] = value[key]
            continue
        safe = _safe_fact_value(value[key])
        if safe not in (None, [], {}):
            result[key] = safe
    return result or None


def _safe_corpus_object(value: dict[str, Any], object_sha256: str) -> dict[str, Any] | None:
    if (
        value.get("schema") != CORPUS_SCHEMA
        or value.get("kind") != CORPUS_ITEM_KIND
        or value.get("answer_safe") is not True
        or value.get("formal_write_count") != 0
    ):
        return None
    formal_id = value.get("formal_id")
    study_date = _valid_date(value.get("study_date"))
    point_ids = [
        item
        for item in value.get("point_ids", [])[:8]
        if isinstance(item, str) and POINT_RE.fullmatch(item)
    ] if isinstance(value.get("point_ids"), list) else []
    if not isinstance(formal_id, str) or not FORMAL_NODE_RE.fullmatch(formal_id):
        return None
    if study_date is None:
        return None
    source_binding = (
        value.get("source_binding")
        if isinstance(value.get("source_binding"), dict)
        else {}
    )
    completion_entity = (
        source_binding.get("completion_entity")
        if isinstance(source_binding.get("completion_entity"), dict)
        else {}
    )
    completion_sha = source_binding.get("completion_evidence_sha256")
    if (
        completion_entity.get("entity_kind")
        not in {"normal_receipt", "already_current_verification"}
        or completion_entity.get("formal_id") != formal_id
        or not isinstance(completion_sha, str)
        or not re.fullmatch(r"[0-9a-f]{64}", completion_sha)
        or completion_entity.get("entity_sha256") != completion_sha
    ):
        return None
    curation_outcome = value.get("outcome")
    if curation_outcome not in {"curated", "already_current"}:
        curation_outcome = "not_observed"
    binding = (
        value.get("canonical_outcome_binding")
        if isinstance(value.get("canonical_outcome_binding"), dict)
        else {}
    )
    learning_source = (
        binding.get("outcome") if isinstance(binding.get("outcome"), dict) else {}
    )
    learning_outcome = learning_source.get("first_result")
    if learning_outcome not in {
        "independent_correct",
        "partial",
        "wrong",
        "uncertain",
        "fragile_correct",
        "not_observed",
    }:
        learning_outcome = "not_observed"
    choice_result = learning_source.get("choice_result", "not_applicable")
    reasoning_result = learning_source.get("reasoning_result", "not_observed")
    if not isinstance(choice_result, str) or choice_result not in {
        "correct",
        "incorrect",
        "partial",
        "uncertain",
        "not_applicable",
    }:
        return None
    if not isinstance(reasoning_result, str) or reasoning_result not in {
        "sound",
        "partial",
        "diverged",
        "not_observed",
    }:
        return None
    choice_present = learning_source.get(
        "choice_result_evidence_field_present", False
    )
    reasoning_present = learning_source.get(
        "reasoning_result_evidence_field_present", False
    )
    if not isinstance(choice_present, bool) or not isinstance(reasoning_present, bool):
        return None
    learning_evidence: dict[str, Any] = {}
    for key in ("event_id", "source", "confidence", "evidence_kind"):
        safe = _safe_text(learning_source.get(key))
        if safe:
            learning_evidence[key] = safe
    for key in ("fragile_mastery", "stable_retention"):
        if isinstance(learning_source.get(key), bool):
            learning_evidence[key] = learning_source[key]

    observed_source = (
        value.get("observed_facts")
        if isinstance(value.get("observed_facts"), dict)
        else {}
    )
    observed_allowed = {
        "user_error_entry",
        "learner_error_entry",
        "error_entry",
        "first_action",
        "provenance",
        "user_error_provenance",
        "error_entry_provenance",
        "first_action_provenance",
    }
    observed: dict[str, Any] = {}
    for key in sorted(set(observed_source) & observed_allowed):
        safe = _safe_fact_value(observed_source[key])
        if safe not in (None, [], {}):
            observed[key] = safe

    snapshot_source = (
        value.get("formal_snapshot")
        if isinstance(value.get("formal_snapshot"), dict)
        else {}
    )
    scalar_fields = {
        "safe_summary",
        "question_type",
        "actual_mechanism",
        "core_point",
        "redo_first_action",
    }
    list_fields = {"key_parameters", "fuzzy_concepts", "error_tags"}
    snapshot: dict[str, Any] = {}
    for key in sorted(set(snapshot_source) & scalar_fields):
        safe = _safe_text(snapshot_source[key])
        if safe:
            snapshot[key] = safe
    for key in sorted(set(snapshot_source) & list_fields):
        safe = _safe_text_list(snapshot_source[key])
        if safe:
            snapshot[key] = safe

    return {
        "formal_id": formal_id,
        "study_date": study_date,
        "curation_outcome": curation_outcome,
        "learning_outcome": learning_outcome,
        "choice_result": choice_result,
        "reasoning_result": reasoning_result,
        "result_evidence_field_present": {
            "choice_result": choice_present,
            "reasoning_result": reasoning_present,
        },
        "learning_evidence": learning_evidence,
        "point_ids": point_ids,
        "observed_facts": observed,
        "formal_snapshot": snapshot,
        "object_sha256": object_sha256,
    }


def _optional_nightly_corpus(
    state_dir: Path,
    targets: dict[str, Any],
    root_manifest: dict[str, Any],
) -> dict[str, Any]:
    """Read only target-addressed, content-hashed nightly corpus objects."""

    corpus_dir = state_dir / "corpus"
    manifest_path = corpus_dir / "manifest.json"
    bound_manifest_hash = root_manifest.get("corpus_manifest_sha256")
    bound_index_hash = root_manifest.get("corpus_index_sha256")
    if bound_manifest_hash is None:
        return {
            "status": "not_available",
            "reason_code": "corpus_not_bound_by_root_manifest",
            "entries": [],
        }
    if (
        not isinstance(bound_manifest_hash, str)
        or not re.fullmatch(r"[0-9a-f]{64}", bound_manifest_hash)
        or not isinstance(bound_index_hash, str)
        or not re.fullmatch(r"[0-9a-f]{64}", bound_index_hash)
    ):
        return {
            "status": "unavailable",
            "reason_code": "invalid_root_corpus_binding",
            "entries": [],
        }
    try:
        manifest = _read_json_checked(manifest_path, bound_manifest_hash)
        if (
            manifest.get("schema") != CORPUS_SCHEMA
            or manifest.get("kind") != CORPUS_MANIFEST_KIND
            or manifest.get("answer_safe") is not True
            or manifest.get("formal_write_count") != 0
        ):
            raise PreanswerContextError("invalid_corpus_manifest")
        artifact_hashes = manifest.get("artifact_hashes")
        if not isinstance(artifact_hashes, dict):
            raise PreanswerContextError("invalid_corpus_manifest")
        index_ref = manifest.get("index_ref", "index.json")
        if index_ref != "index.json" and (
            not isinstance(index_ref, str)
            or not re.fullmatch(r"indexes/[0-9a-f]{64}\.json", index_ref)
        ):
            raise PreanswerContextError("invalid_corpus_manifest")
        index_hash = artifact_hashes.get(index_ref)
        if (
            not isinstance(index_hash, str)
            or len(index_hash) != 64
            or index_hash != bound_index_hash
            or (
                index_ref != "index.json"
                and Path(index_ref).stem != index_hash
            )
        ):
            raise PreanswerContextError("invalid_corpus_manifest")
        index = _read_json_checked(corpus_dir / index_ref, index_hash)
        if index.get("schema") != CORPUS_SCHEMA or index.get("kind") != CORPUS_INDEX_KIND:
            raise PreanswerContextError("invalid_corpus_index")

        raw_entries: list[tuple[int, dict[str, Any]]] = []
        selectors = (
            (0, "by_formal_id", targets["formal_node_ids"]),
            (1, "by_point_id", targets["point_ids"]),
            (
                2,
                "by_mechanism_id",
                [
                    *targets.get("mechanism_ids", []),
                    *targets.get("mechanism_primitive_ids", []),
                ],
            ),
            (3, "by_error_pattern", targets.get("error_pattern_ids", [])),
            (4, "by_method_id", targets.get("method_ids", [])),
        )
        for selector_rank, index_name, target_ids in selectors:
            lookup = index.get(index_name)
            if not isinstance(lookup, dict):
                continue
            for target_id in target_ids:
                values = lookup.get(target_id, [])
                if isinstance(values, dict):
                    values = [values]
                if not isinstance(values, list):
                    continue
                raw_entries.extend(
                    (selector_rank, item)
                    for item in values[:8]
                    if isinstance(item, dict)
                )

        unique: dict[str, tuple[int, dict[str, Any]]] = {}
        for selector_rank, entry in raw_entries:
            object_sha256 = entry.get("object_sha256")
            relative = _corpus_object_path(entry.get("object_ref"), object_sha256)
            if relative is None:
                raise PreanswerContextError("invalid_corpus_object_ref")
            expected_hash = artifact_hashes.get(relative)
            if expected_hash != object_sha256:
                raise PreanswerContextError("corpus_object_hash_unbound")
            previous = unique.get(relative)
            if previous is None or selector_rank < previous[0]:
                unique[relative] = (selector_rank, entry)

        ranked = sorted(
            unique.items(),
            key=lambda item: (
                -item[1][0],
                _valid_date(item[1][1].get("study_date")) or "",
                item[0],
            ),
            reverse=True,
        )[:6]
        result: list[dict[str, Any]] = []
        for relative, (_selector_rank, entry) in ranked:
            object_sha256 = entry["object_sha256"]
            obj = _read_json_checked(corpus_dir / relative, object_sha256)
            safe = _safe_corpus_object(obj, object_sha256)
            if safe is None:
                raise PreanswerContextError("invalid_corpus_object")
            entry_formal = entry.get("formal_id")
            entry_date = _valid_date(entry.get("study_date"))
            entry_points = entry.get("point_ids")
            if (
                entry_formal != safe["formal_id"]
                or entry_date != safe["study_date"]
                or not isinstance(entry_points, list)
                or sorted(entry_points) != sorted(safe["point_ids"])
            ):
                raise PreanswerContextError("corpus_index_object_mismatch")
            result.append(safe)
        return {
            "status": "ready",
            "corpus_build_id": manifest.get("build_id"),
            "entries": result,
        }
    except PreanswerContextError as exc:
        return {"status": "unavailable", "reason_code": str(exc), "entries": []}
    except TimeoutError:
        return {
            "status": "unavailable",
            "reason_code": "corpus_lookup_timeout",
            "entries": [],
        }
    except Exception:
        return {
            "status": "unavailable",
            "reason_code": "unexpected_corpus_failure",
            "entries": [],
        }


def _question_candidates(
    targets: dict[str, Any],
    cross_rows: list[dict[str, Any]],
    question_index: dict[str, Any],
    learner: dict[str, Any],
    *,
    as_of: str,
) -> list[dict[str, Any]]:
    by_point = (
        question_index.get("by_point")
        if isinstance(question_index.get("by_point"), dict)
        else {}
    )
    by_mechanism = (
        question_index.get("by_mechanism")
        if isinstance(question_index.get("by_mechanism"), dict)
        else {}
    )
    questions = (
        question_index.get("questions")
        if isinstance(question_index.get("questions"), dict)
        else {}
    )
    question_states = (
        learner.get("questions")
        if isinstance(learner.get("questions"), dict)
        else {}
    )
    current_nodes = set(targets["formal_node_ids"])
    seeds: list[tuple[str, str, str | None]] = []
    for point_id in targets["point_ids"]:
        seeds.append(("current_point", point_id, None))
    for mechanism_id in targets["mechanism_ids"]:
        seeds.append(("current_mechanism", mechanism_id, None))
    for cross_row in cross_rows:
        if cross_row.get("live_routable"):
            seeds.append(
                (
                    "verified_cross_point",
                    cross_row["target_point_id"],
                    cross_row["edge_id"],
                )
            )

    candidates: dict[str, dict[str, Any]] = {}
    for origin, seed_id, edge_id in seeds:
        references = (
            by_mechanism.get(seed_id, [])
            if origin == "current_mechanism"
            else by_point.get(seed_id, [])
        )
        if not isinstance(references, list):
            continue
        for reference in references:
            if not isinstance(reference, dict):
                continue
            node_id = reference.get("node_id")
            if node_id in current_nodes or node_id not in questions:
                continue
            row = questions[node_id]
            source = row.get("reliable_source") or {}
            if source.get("original_question_eligible") is not True:
                continue
            last_date = row.get("effective_last_attempt_date")
            state_date = (question_states.get(node_id) or {}).get("last_attempt_date")
            valid_dates = [item for item in (_valid_date(last_date), _valid_date(state_date)) if item]
            effective = max(valid_dates) if valid_dates else None
            age = _age_days(effective, as_of)
            cooldown_eligible = age is None or age >= 7
            candidate = {
                "formal_node_id": node_id,
                "subject": row.get("subject"),
                "origin": origin,
                "cross_edge_id": edge_id,
                "matched_target_id": seed_id,
                "source_status": source.get("status"),
                "effective_last_attempt_date": effective,
                "days_since_attempt": age,
                "date_evidence": "unknown" if effective is None else "explicit",
                "cooldown_eligible": cooldown_eligible,
                "cooldown_days": 7,
                "scheduling_authority": "none_preanswer_read_only",
            }
            previous = candidates.get(node_id)
            if previous is None or (
                origin == "verified_cross_point"
                and previous.get("origin") != "verified_cross_point"
            ):
                candidates[node_id] = candidate

    rows = list(candidates.values())
    origin_rank = {"current_point": 2, "current_mechanism": 1, "verified_cross_point": 0}
    rows.sort(
        key=lambda row: (
            row["cooldown_eligible"],
            row["date_evidence"] == "explicit",
            origin_rank.get(row["origin"], -1),
            row["days_since_attempt"] if row["days_since_attempt"] is not None else -1,
            row["formal_node_id"],
        ),
        reverse=True,
    )
    return rows[:3]


def _response_guidance(
    states: list[dict[str, Any]], cross_rows: list[dict[str, Any]]
) -> dict[str, Any]:
    direct_states = [row for row in states if row.get("personalization_eligible")]
    if any(row["recent_7d_failure_count"] >= 2 for row in direct_states):
        strategy = "repair_repeated_recent_failure"
        explanation_depth = "minimum_framework_then_first_action"
    elif any(row["recent_7d_failure_count"] == 1 for row in direct_states):
        strategy = "target_recent_failure"
        explanation_depth = "targeted_explanation_then_boundary_check"
    elif any(
        row["fragile_count"] > row["stable_retention_count"]
        for row in direct_states
    ):
        strategy = "test_fragile_understanding"
        explanation_depth = "compressed_explanation_then_transfer_check"
    elif direct_states and all(
        row["stable_retention_count"] > 0 for row in direct_states
    ):
        strategy = "avoid_reteaching_stable_content"
        explanation_depth = "compressed_direct_answer"
    else:
        strategy = "neutral_targeted_answer"
        explanation_depth = "minimum_sufficient_framework"
    return {
        "strategy": strategy,
        "explanation_depth": explanation_depth,
        "cross_subject_connection_available": bool(cross_rows),
        "verified_cross_question_available": any(
            row.get("live_routable") for row in cross_rows
        ),
        "caller_must_preserve_answer_protection": True,
        "direct_evidence_only": True,
        "background_fallback_count": len(states) - len(direct_states),
    }


def _public_corpus_context(corpus: dict[str, Any]) -> dict[str, Any]:
    """Expose only bindings; semantic facts already live in the response plan."""

    result = {
        "status": corpus.get("status"),
        "corpus_build_id": corpus.get("corpus_build_id"),
    }
    if corpus.get("reason_code"):
        result["reason_code"] = corpus.get("reason_code")
    entries: list[dict[str, Any]] = []
    for row in corpus.get("entries") or []:
        if not isinstance(row, dict):
            continue
        evidence = row.get("learning_evidence") or {}
        entries.append(
            {
                "formal_id": row.get("formal_id"),
                "study_date": row.get("study_date"),
                "curation_outcome": row.get("curation_outcome"),
                "learning_outcome": row.get("learning_outcome"),
                "choice_result": row.get("choice_result"),
                "reasoning_result": row.get("reasoning_result"),
                "result_evidence_field_present": row.get(
                    "result_evidence_field_present"
                ),
                "learning_evidence": {
                    key: evidence.get(key)
                    for key in (
                        "event_id",
                        "confidence",
                        "evidence_kind",
                        "fragile_mastery",
                        "stable_retention",
                    )
                    if evidence.get(key) is not None
                },
                "evidence_event_id": evidence.get("event_id"),
                "point_ids": list(row.get("point_ids") or [])[:8],
                "object_sha256": row.get("object_sha256"),
            }
        )
    result["entries"] = entries[:6]
    return result


def _fit_result(result: dict[str, Any], max_bytes: int) -> dict[str, Any]:
    context = result["context"]
    trim_order = (
        ("question_candidates", 0),
        ("cross_subject_context", 0),
        ("recent_evidence", 0),
        ("learner_states", 1),
    )
    truncated = False
    while len(_json_bytes(result)) > max_bytes:
        changed = False
        for key, minimum in trim_order:
            value = context.get(key)
            if isinstance(value, list) and len(value) > minimum:
                value.pop()
                truncated = True
                changed = True
                break
        if changed:
            continue
        corpus = context.get("nightly_curation_context")
        if isinstance(corpus, dict):
            entries = corpus.get("entries")
            if isinstance(entries, list) and entries:
                entries.pop()
                truncated = True
                continue
        plan = context.get("response_plan")
        if isinstance(plan, dict):
            for key, minimum in (
                ("background_context", 0),
                ("verified_cross_connections", 0),
                ("direct_historical_errors", 1),
                ("direct_recent_errors", 2),
                ("common_patterns", 1),
                ("evidence_gaps", 1),
            ):
                value = plan.get(key)
                if isinstance(value, list) and len(value) > minimum:
                    value.pop()
                    truncated = True
                    changed = True
                    break
            if changed:
                continue
            for key in ("direct_recent_errors", "direct_historical_errors"):
                values = plan.get(key)
                if not isinstance(values, list):
                    continue
                for row in values:
                    if not isinstance(row, dict):
                        continue
                    signature = row.get("failure_signature")
                    if not isinstance(signature, dict):
                        continue
                    for field in (
                        "preconditions",
                        "state_transitions",
                        "causal_directions",
                        "objects",
                        "method_templates",
                        "error_patterns",
                        "mechanism_primitives",
                        "first_action",
                        "first_break",
                    ):
                        if field in signature:
                            signature.pop(field)
                            truncated = True
                            changed = True
                            break
                    if changed:
                        break
                if changed:
                    break
            if changed:
                continue
        learner_states = context.get("learner_states")
        if isinstance(learner_states, list) and learner_states:
            learner_states.pop()
            truncated = True
            continue
        if not changed:
            targets = context.get("current_targets", {})
            point_views = targets.get("point_views")
            if isinstance(point_views, list) and point_views:
                point_views.pop()
                truncated = True
                continue
            for key, minimum in (
                ("mechanism_ids", 0),
                ("formal_node_ids", 0),
                ("module_ids", 0),
                ("point_ids", 1),
                ("resolution_sources", 0),
            ):
                value = targets.get(key)
                if isinstance(value, list) and len(value) > minimum:
                    value.pop()
                    truncated = True
                    changed = True
                    break
            if changed:
                continue
            if (
                isinstance(corpus, dict)
                and not corpus.get("entries")
                and context.pop("nightly_curation_context", None) is not None
            ):
                truncated = True
                continue
            if isinstance(plan, dict) and not plan.get("budget_compacted"):
                strategy = plan.get("explanation_strategy") or {}
                boundary = plan.get("first_boundary_to_check") or {}
                practice = plan.get("optional_practice_action") or {}
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
                    "first_boundary_to_check": {
                        "boundary_id": boundary.get("boundary_id")
                    },
                    "optional_practice_action": {
                        "action": practice.get("action"),
                        "scheduling_authority": "none",
                    },
                    "evidence_gaps": list(plan.get("evidence_gaps") or [])[:1],
                    "budget_compacted": True,
                }
                plan = context["response_plan"]
                truncated = True
                continue
            if context.pop("response_guidance", None) is not None:
                truncated = True
                continue
            if isinstance(plan, dict) and plan.pop("background_context", None) is not None:
                truncated = True
                continue
            plan = context.get("response_plan") or {}
            target = context.get("current_targets") or {}
            strategy = plan.get("explanation_strategy") or {}
            minimal = {
                "schema": result.get("schema"),
                "status": result.get("status"),
                "slice_id": result.get("slice_id"),
                "request_id": result.get("request_id"),
                "request_sha256": result.get("request_sha256"),
                "conversation_kind": result.get("conversation_kind"),
                "as_of": result.get("as_of"),
                "mode": result.get("mode"),
                "read_only": True,
                "formal_write_count": 0,
                "scheduling_authority": "none",
                "truncated": True,
                "byte_count": 0,
                "context": {
                    "current_targets": {
                        "point_ids": list(target.get("point_ids") or [])[:1]
                    },
                    "cross_subject_context": [],
                    "response_guidance": {
                        "cross_subject_connection_available": False,
                        "verified_cross_question_available": False,
                    },
                    "response_plan": {
                        "current_target": {
                            "point_ids": list(
                                (plan.get("current_target") or {}).get("point_ids")
                                or []
                            )[:1]
                        },
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
                        "evidence_gaps": [],
                    },
                },
            }
            for _ in range(4):
                minimal["byte_count"] = len(_json_bytes(minimal))
            if len(_json_bytes(minimal)) <= max_bytes:
                return minimal
            raise PreanswerContextError("context_budget_too_small")
    guidance = context.get("response_guidance")
    if isinstance(guidance, dict):
        visible_cross = context.get("cross_subject_context")
        guidance["cross_subject_connection_available"] = bool(visible_cross)
        guidance["verified_cross_question_available"] = any(
            isinstance(row, dict) and row.get("live_routable")
            for row in visible_cross or []
        )
    result["truncated"] = truncated
    for _ in range(5):
        size = len(_json_bytes(result))
        if result.get("byte_count") == size:
            break
        result["byte_count"] = size
    if len(_json_bytes(result)) > max_bytes:
        return _fit_result(result, max_bytes)
    return result


def _assert_answer_safe_result(result: dict[str, Any]) -> None:
    """Apply the shared leak gate to the final bounded slice as defence in depth."""

    serialized = json.dumps(result, ensure_ascii=False, sort_keys=True)
    fails, warns = scan_leak(serialized)
    if fails or warns:
        raise PreanswerContextError("answer_safety_scan_failed")


def preanswer_slice(
    repo: str | Path,
    *,
    target_envelope: dict[str, Any],
    state_dir: str | Path | None = None,
    max_bytes: int = DEFAULT_MAX_BYTES,
    mode: str = "verified",
) -> dict[str, Any]:
    """Return one deterministic, bounded and read-only pre-answer slice."""

    if not isinstance(max_bytes, int) or not MIN_MAX_BYTES <= max_bytes <= MAX_MAX_BYTES:
        raise PreanswerContextError("invalid_budget")
    if mode not in {"off", "shadow", "verified"}:
        raise PreanswerContextError("invalid_mode")
    envelope = _validate_envelope(target_envelope)
    root = Path(repo).resolve()
    projection_dir = (
        Path(state_dir).resolve()
        if state_dir is not None
        else (root / DEFAULT_STATE_RELATIVE).resolve()
    )
    projection = _load_projection(projection_dir, envelope["as_of"])
    manifest = projection["manifest"]
    catalog = projection["catalog.json"]
    learner = projection["learner_state.json"]
    question_index = projection["question_index.json"]
    cross = projection["cross-subject-shadow.json"]

    targets = _resolve_targets(envelope, catalog, question_index)
    if not any(
        (
            targets["subjects"],
            targets["point_ids"],
            targets["mechanism_ids"],
            targets["mechanism_primitive_ids"],
            targets["formal_node_ids"],
            targets["module_ids"],
            targets["error_pattern_ids"],
            targets["method_ids"],
        )
    ):
        raise PreanswerContextError("target_unresolved")
    states = _learner_states(targets, learner)
    cross_rows = _cross_context(targets, cross, catalog, mode=mode)
    nightly_corpus = _optional_nightly_corpus(
        projection_dir, targets, manifest
    )
    candidates = _question_candidates(
        targets,
        cross_rows,
        question_index,
        learner,
        as_of=envelope["as_of"],
    )
    recent_direct, recent_background = match_recent_signatures(
        projection[FAILURE_SIGNATURE_FILE], targets
    )
    historical_direct = match_historical_signatures(
        list(nightly_corpus.get("entries") or []), targets
    )
    cross_evidence_matches = match_verified_cross_recent_signatures(
        projection[FAILURE_SIGNATURE_FILE], cross_rows
    )
    response_plan = build_response_plan(
        targets=targets,
        recent_direct=recent_direct,
        recent_background=recent_background,
        historical_direct=historical_direct,
        cross_rows=cross_rows,
        cross_evidence_matches=cross_evidence_matches,
        question_candidates=candidates,
        corpus_status=str(nightly_corpus.get("status") or "not_available"),
    )
    evidence: list[dict[str, str]] = []
    seen_evidence: set[str] = set()
    for row in states:
        if not row.get("personalization_eligible"):
            continue
        for event_id in reversed(row.get("evidence_event_ids", [])):
            if event_id in seen_evidence:
                continue
            seen_evidence.add(event_id)
            evidence.append({"event_id": event_id, "role": "bounded_state_evidence"})
            if len(evidence) == 3:
                break
        if len(evidence) == 3:
            break

    safe_targets = {
        "subjects": targets["subjects"],
        "point_ids": targets["point_ids"][:6],
        "mechanism_ids": targets["mechanism_ids"][:6],
        "mechanism_primitive_ids": targets["mechanism_primitive_ids"][:6],
        "formal_node_ids": targets["formal_node_ids"][:2],
        "module_ids": targets["module_ids"],
        "point_views": targets["point_views"],
        "resolution_sources": targets["resolution_sources"],
        "error_pattern_ids": targets["error_pattern_ids"],
        "method_ids": targets["method_ids"],
    }
    context = {
        "current_targets": safe_targets,
        "learner_states": states,
        "recent_evidence": evidence,
        "cross_subject_context": cross_rows,
        "nightly_curation_context": _public_corpus_context(nightly_corpus),
        "question_candidates": candidates,
        "response_guidance": _response_guidance(states, cross_rows),
        "response_plan": response_plan,
    }
    request_sha256 = _sha256_bytes(
        _json_bytes(
            {
                "envelope": envelope,
                "mode": mode,
                "max_bytes": max_bytes,
            }
        )
    )
    identity_payload = {
        "schema": SCHEMA,
        "projection_build_id": manifest.get("build_id"),
        "as_of": envelope["as_of"],
        "conversation_kind": envelope["conversation_kind"],
        "mode": mode,
        "max_bytes": max_bytes,
        "current_targets": safe_targets,
        "context": context,
    }
    slice_id = "PPA-" + _sha256_bytes(_json_bytes(identity_payload))[:20].upper()
    result = {
        "schema": SCHEMA,
        "status": "ready",
        "slice_id": slice_id,
        "request_id": envelope.get("request_id"),
        "request_sha256": request_sha256,
        "conversation_kind": envelope["conversation_kind"],
        "as_of": envelope["as_of"],
        "projection_build_id": manifest.get("build_id"),
        "layer_hashes": {
            "recent_incremental_sha256": (
                manifest.get("artifact_hashes") or {}
            ).get(FAILURE_SIGNATURE_FILE),
            "historical_manifest_sha256": manifest.get(
                "corpus_manifest_sha256"
            ),
            "historical_index_sha256": manifest.get("corpus_index_sha256"),
        },
        "mode": mode,
        "read_only": True,
        "formal_write_count": 0,
        "scheduling_authority": "none",
        "truncated": False,
        "byte_count": 0,
        "context": context,
    }
    fitted = _fit_result(result, max_bytes)
    _assert_answer_safe_result(fitted)
    return fitted


def safe_preanswer_slice(
    repo: str | Path,
    *,
    target_envelope: dict[str, Any],
    state_dir: str | Path | None = None,
    max_bytes: int = DEFAULT_MAX_BYTES,
    mode: str = "verified",
) -> dict[str, Any]:
    """Fail-soft wrapper returning only controlled reason codes on failure."""

    request_id = (
        target_envelope.get("request_id")
        if isinstance(target_envelope, dict)
        and isinstance(target_envelope.get("request_id"), str)
        and SAFE_ID_RE.fullmatch(target_envelope["request_id"])
        else None
    )
    try:
        return preanswer_slice(
            repo,
            target_envelope=target_envelope,
            state_dir=state_dir,
            max_bytes=max_bytes,
            mode=mode,
        )
    except PreanswerContextError as exc:
        return {
            "schema": SCHEMA,
            "status": "unavailable",
            "request_id": request_id,
            "reason_code": str(exc),
            "read_only": True,
            "formal_write_count": 0,
            "scheduling_authority": "none",
        }
    except TimeoutError:
        return {
            "schema": SCHEMA,
            "status": "unavailable",
            "request_id": request_id,
            "reason_code": "lookup_timeout",
            "read_only": True,
            "formal_write_count": 0,
            "scheduling_authority": "none",
        }
    except Exception:
        return {
            "schema": SCHEMA,
            "status": "unavailable",
            "request_id": request_id,
            "reason_code": "unexpected_lookup_failure",
            "read_only": True,
            "formal_write_count": 0,
            "scheduling_authority": "none",
        }


def _read_envelope(path: str) -> dict[str, Any]:
    try:
        raw = sys.stdin.buffer.read() if path == "-" else Path(path).read_bytes()
        value = json.loads(raw)
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise PreanswerContextError("invalid_target_envelope") from exc
    if not isinstance(value, dict):
        raise PreanswerContextError("invalid_target_envelope")
    return value


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Read one bounded personalization slice before a 408 reply"
    )
    parser.add_argument("--repo", default=".")
    parser.add_argument("--target-envelope", required=True, help="JSON file or - for stdin")
    parser.add_argument("--state-dir")
    parser.add_argument("--max-bytes", type=int, default=DEFAULT_MAX_BYTES)
    parser.add_argument("--mode", choices=("off", "shadow", "verified"), default="verified")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    try:
        envelope = _read_envelope(args.target_envelope)
    except PreanswerContextError as exc:
        result = {
            "schema": SCHEMA,
            "status": "unavailable",
            "request_id": None,
            "reason_code": str(exc),
            "read_only": True,
            "formal_write_count": 0,
            "scheduling_authority": "none",
        }
    else:
        result = safe_preanswer_slice(
            args.repo,
            target_envelope=envelope,
            state_dir=args.state_dir,
            max_bytes=args.max_bytes,
            mode=args.mode,
        )
    print(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True))
    return 0 if result.get("status") == "ready" else 2


if __name__ == "__main__":
    raise SystemExit(main())
