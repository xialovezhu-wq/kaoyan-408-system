#!/usr/bin/env python3
"""Freeze, validate and publish the native-Luna 408 morning selection flow."""

from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import re
from pathlib import Path
from typing import Any

import generate_morning_action_queue as legacy_queue
import generate_morning_review_queue as inventory
import current_question_context_408 as current_context
import question_exposure_408 as question_exposure
import review_feedback_loop as review_loop

import runtime_paths_408 as _paths


SNAPSHOT_SCHEMA = "morning_selection_snapshot_v1"
LUNA_SCHEMA = "luna_morning_candidate_set_v1"
LUNA_RECEIPT_SCHEMA = "native_luna_runtime_receipt_v1"
LUNA_VALIDATED_SCHEMA = "validated_luna_morning_candidate_set_v1"
LUNA_SHARD_BUNDLE_SCHEMA = "luna_morning_candidate_shard_bundle_v1"
LUNA_SHARD_SCOPE_SCHEMA = "luna_morning_candidate_shard_scope_v1"
LUNA_SHARD_PLAN_SCHEMA = "luna_morning_candidate_shard_plan_v1"
LUNA_SHARD_RECEIPT_SCHEMA = "native_luna_shard_runtime_receipt_v1"
LUNA_SHARD_ADVISORY_SCHEMA = "luna_morning_candidate_shard_advisory_v1"
SOL_SCHEMA = "sol_morning_selection_decision_v1"
SELECTION_MODE = "semantic_sol_v1"
POLICY_VERSION = "morning-semantic-selection-v1"
MAX_FINAL_ITEMS = 5
MAX_CANDIDATES = 80
STATE_REL = Path("wiki/study_vaults/408-full/state/morning-semantic-selection")
DETAILS_ROOT = _paths.details_root()
BRIDGE_PATH = _paths.codex_home() / 'kaoyan-408-obsidian-bridge/records.json'
NODE_RE = re.compile(r"^(?:DS|CO|OS|CN)_(?:\d{4}|UNK)_\d{3}$")
RU_RE = re.compile(r"^RU_[A-Za-z0-9._-]+$")
SHA_RE = re.compile(r"^[0-9a-f]{64}$")
STAGE_RE = re.compile(r"^(?:D1(?:_rescue)?|D3|D7|D14|D30|D60|D90|original_track)$")
EXPLICIT_CORRECT_ANSWER_RE = re.compile(
    r"(?:标准答案|正确答案)[：:]\s*(?:选项\s*)?([A-D])(?:\b|[，。.,])"
)
GENERIC_ANSWER_RE = re.compile(
    r"(?<!错误)(?<!错选)答案[：:]\s*(?:选项\s*)?([A-D])(?:\b|[，。.,])"
)
SAFE_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:#/-]{0,255}$")
SOURCE_ID_PROVENANCE = {
    "action_source_id",
    "formal_node_fallback",
}


class SelectionError(RuntimeError):
    pass


def canonical_answer_option(card_text: str) -> str | None:
    """Return one canonical key without matching a historical wrong answer."""

    explicit = {
        match.group(1)
        for match in EXPLICIT_CORRECT_ANSWER_RE.finditer(card_text)
    }
    if len(explicit) > 1:
        raise SelectionError("canonical correct answer is ambiguous")
    if explicit:
        return next(iter(explicit))
    generic = {match.group(1) for match in GENERIC_ANSWER_RE.finditer(card_text)}
    if len(generic) > 1:
        raise SelectionError("canonical answer is ambiguous")
    return next(iter(generic)) if generic else None


def _canonical_bytes(value: Any) -> bytes:
    return (json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n").encode("utf-8")


def _sha(value: Any) -> str:
    raw = value if isinstance(value, bytes) else _canonical_bytes(value)
    return hashlib.sha256(raw).hexdigest()


def _file_sha(path: Path) -> str:
    if path.is_symlink() or not path.is_file():
        raise SelectionError(f"unsafe or missing file: {path}")
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _date(value: str) -> dt.date:
    try:
        return dt.date.fromisoformat(value)
    except ValueError as exc:
        raise SelectionError("date must be YYYY-MM-DD") from exc


def _safe_relative(root: Path, value: str, label: str) -> Path:
    relative = Path(value)
    if relative.is_absolute() or not relative.parts or ".." in relative.parts:
        raise SelectionError(f"{label} is not a safe relative path")
    lexical = root / relative
    if lexical.is_symlink():
        raise SelectionError(f"{label} is a symlink")
    try:
        resolved = lexical.resolve(strict=True)
        resolved.relative_to(root.resolve(strict=True))
    except (OSError, ValueError) as exc:
        raise SelectionError(f"{label} is missing or escapes its root") from exc
    if not resolved.is_file() or resolved.is_symlink():
        raise SelectionError(f"{label} is not a regular file")
    return resolved


def _read_json(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise SelectionError(f"invalid JSON: {path}") from exc
    if not isinstance(value, dict):
        raise SelectionError(f"JSON root is not an object: {path}")
    return value


def _write_object(repo: Path, kind: str, value: dict[str, Any]) -> dict[str, str]:
    digest = _sha(value)
    path = repo / STATE_REL / "objects" / kind / f"{digest}.json"
    raw = json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    if path.exists():
        if path.read_text(encoding="utf-8") != raw:
            raise SelectionError("content-addressed object collision")
    else:
        review_loop.atomic_write(path, raw)
    return {"ref": path.relative_to(repo).as_posix(), "sha256": digest}


def _row_hash(row: dict[str, Any]) -> str:
    return _sha(row)


def _split_points(value: str) -> list[str]:
    parts = [part.strip() for part in re.split(r"[；;,，]", value or "") if part.strip()]
    seen: set[str] = set()
    result: list[str] = []
    for part in parts:
        if part not in seen:
            seen.add(part)
            result.append(part)
    return result[:12]


def _single_line_source_label(value: object) -> str | None:
    """Preserve a human source label without letting it become a machine ID."""

    text = " ".join(str(value or "").split()).strip()
    if not text:
        return None
    if len(text.encode("utf-8")) > 500:
        raise SelectionError("source label is too large")
    return text


def resolve_action_source_identity(
    action: dict[str, Any],
    resolved_nodes: list[str],
    canonical_node_ids: set[str],
) -> tuple[dict[str, Any], list[str]]:
    """Return a CurrentQuestionContext-compatible source identity.

    Human labels such as ``2014统考第25题`` are not stable transaction IDs. A
    fallback is allowed only when the action resolves to exactly one canonical
    formal node; otherwise the candidate remains in the frozen evidence as
    ``needs_user`` and can never reach a ready queue.
    """

    raw_source = str(action.get("source_id") or "").strip()
    explicit_label = action.get("source_label")
    source_label = _single_line_source_label(
        explicit_label if explicit_label is not None else raw_source
    )
    if current_context.SAFE_ID_RE.fullmatch(raw_source):
        return (
            {
                "source_id": raw_source,
                "source_label": source_label or raw_source,
                "source_id_provenance": "action_source_id",
            },
            [],
        )

    unique_nodes = sorted(
        {
            node
            for node in resolved_nodes
            if NODE_RE.fullmatch(node) and node in canonical_node_ids
        }
    )
    if len(resolved_nodes) == 1 and len(unique_nodes) == 1:
        formal_node_id = unique_nodes[0]
        return (
            {
                "source_id": formal_node_id,
                "source_label": source_label or formal_node_id,
                "source_id_provenance": "formal_node_fallback",
            },
            [],
        )

    return (
        {
            "source_id": None,
            "source_label": source_label,
            "source_id_provenance": "unresolved",
        },
        [
            "source_id_not_current_question_compatible",
            "source_id_formal_node_fallback_not_unique",
        ],
    )


def _node_rows(repo: Path) -> dict[str, dict[str, str]]:
    return {
        row["ID"]: row
        for row in inventory.read_markdown_table(repo / "节点总表.md")
        if NODE_RE.fullmatch(row.get("ID", ""))
    }


def _track_rows(repo: Path) -> dict[str, list[dict[str, str]]]:
    result: dict[str, list[dict[str, str]]] = {}
    for row in inventory.read_markdown_table(repo / "原题复做轨总表.md"):
        node = row.get("正式节点ID", "")
        if NODE_RE.fullmatch(node):
            result.setdefault(node, []).append(row)
    return result


def _mapping(repo: Path) -> tuple[dict[str, list[str]], dict[str, str]]:
    by_unit: dict[str, list[str]] = {}
    by_node: dict[str, str] = {}
    for row in inventory.read_markdown_table(repo / "复习单元节点映射.md"):
        node = row.get("正式节点ID", "")
        unit = row.get("主复习单元ID", "")
        if NODE_RE.fullmatch(node) and RU_RE.fullmatch(unit):
            by_unit.setdefault(unit, []).append(node)
            by_node[node] = unit
    return ({key: sorted(set(value)) for key, value in by_unit.items()}, by_node)


def _relation_rows(repo: Path) -> dict[str, list[dict[str, str]]]:
    result: dict[str, list[dict[str, str]]] = {}
    for row in inventory.read_markdown_table(repo / "关系边表.md"):
        start = row.get("起点ID", "")
        end = row.get("终点ID", "")
        if not (NODE_RE.fullmatch(start) and NODE_RE.fullmatch(end)):
            continue
        material = {
            "start": start,
            "end": end,
            "type": row.get("关系类型", ""),
            "strength": row.get("联系强度", ""),
            "reason": row.get("关联原因", ""),
            "priority": row.get("复盘优先级", ""),
        }
        material["row_sha256"] = _row_hash(material)
        result.setdefault(start, []).append(material)
        result.setdefault(end, []).append(material)
    rank = {"强": 0, "中": 1, "弱": 2}
    return {
        key: sorted(value, key=lambda row: (rank.get(row["strength"], 9), row["type"], row["row_sha256"]))[:12]
        for key, value in result.items()
    }


def _bridge_rows() -> dict[str, dict[str, Any]]:
    value = _read_json(BRIDGE_PATH)
    rows = value.get("records")
    if not isinstance(rows, list):
        raise SelectionError("408 bridge records are invalid")
    result: dict[str, dict[str, Any]] = {}
    for row in rows:
        if not isinstance(row, dict):
            continue
        node = str(row.get("formal_id") or row.get("node_id") or "")
        if NODE_RE.fullmatch(node):
            result[node] = row
    return result


def _event_nodes(event: dict[str, Any], track_source: str) -> set[str]:
    nodes: set[str] = set()
    formal = str(event.get("formal_node_id") or "")
    if NODE_RE.fullmatch(formal):
        nodes.add(formal)
    for value in event.get("mapped_formal_node_ids") or []:
        if NODE_RE.fullmatch(str(value)):
            nodes.add(str(value))
    source = str(event.get("source_id") or "")
    if NODE_RE.fullmatch(source):
        nodes.add(source)
    if track_source and source == track_source:
        nodes.add("__TRACK_SOURCE__")
    return nodes


def _closed_book_outcomes(events: list[dict[str, Any]], node: str, track_source: str) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for event in events:
        if event.get("event_kind") != "outcome" or event.get("question_valid") is not True:
            continue
        if event.get("prompt_level") != "none" or event.get("hint_used") is True:
            continue
        bound = _event_nodes(event, track_source)
        if node not in bound and "__TRACK_SOURCE__" not in bound:
            continue
        try:
            observed = _date(str(event.get("observed_date") or ""))
        except SelectionError:
            continue
        rows.append({**event, "_observed": observed})
    return sorted(rows, key=lambda row: (row["_observed"], str(row.get("event_time") or ""), str(row.get("event_id") or "")))


def original_surface_mode(day: dt.date, last_display_date: dt.date | None) -> str | None:
    """Choose the surface from verified learner-visible display evidence.

    Closed-book outcomes remain useful semantic evidence, but they do not prove
    whether the exact original was recently shown.  No prior display therefore
    permits the original; a future display date is invalid.
    """

    if last_display_date is None:
        return "original_question"
    if last_display_date > day:
        return None
    return "derived_variant" if (day - last_display_date).days < 7 else "original_question"


def original_surface_from_exposure_set(
    day: dt.date,
    question_asset_sha256: str,
    exposure_set: dict[str, Any],
) -> tuple[str | None, dt.date | None, list[dict[str, Any]]]:
    matches = [
        row
        for row in exposure_set.get("rows") or []
        if isinstance(row, dict)
        and row.get("question_asset_sha256") == question_asset_sha256
    ]
    last_display_date = (
        max(_date(str(row["review_date"])) for row in matches)
        if matches
        else None
    )
    return original_surface_mode(day, last_display_date), last_display_date, matches


def _exposures(events: list[dict[str, Any]], day: dt.date, node: str, unit: str | None, track_source: str) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    for event in events:
        if event.get("event_kind") != "outcome":
            continue
        try:
            observed = _date(str(event.get("observed_date") or ""))
        except SelectionError:
            continue
        if not 0 <= (day - observed).days < 7:
            continue
        exact = node in _event_nodes(event, track_source) or "__TRACK_SOURCE__" in _event_nodes(event, track_source)
        mechanism = unit is not None and event.get("review_unit_id") == unit
        if not exact and not mechanism:
            continue
        result.append({
            "event_id": event.get("event_id"),
            "observed_date": observed.isoformat(),
            "first_result": event.get("first_result"),
            "prompt_level": event.get("prompt_level"),
            "exact_original_binding": exact,
            "event_sha256": _sha({key: value for key, value in event.items() if key != "_observed"}),
        })
    return sorted(result, key=lambda row: (row["observed_date"], str(row["event_id"])), reverse=True)[:8]


def _action_nodes(action: dict[str, Any], unit_nodes: dict[str, list[str]]) -> list[str]:
    result: set[str] = set()
    formal = str(action.get("formal_node_id") or "")
    if NODE_RE.fullmatch(formal):
        result.add(formal)
    for value in action.get("formal_node_ids") or []:
        if NODE_RE.fullmatch(str(value)):
            result.add(str(value))
    identity = str(action.get("identity") or "")
    source = str(action.get("source_id") or "")
    if NODE_RE.fullmatch(identity):
        result.add(identity)
    if NODE_RE.fullmatch(source):
        result.add(source)
    unit = str(action.get("review_unit_id") or "")
    if RU_RE.fullmatch(unit):
        result.update(unit_nodes.get(unit, []))
    return sorted(result)


def _original_binding(node: str, bridge: dict[str, Any]) -> tuple[dict[str, Any] | None, list[str]]:
    reasons: list[str] = []
    if not bridge:
        return None, ["bridge_record_missing"]
    question_rows = bridge.get("canonical_question_assets")
    if not isinstance(question_rows, list) or len(question_rows) != 1 or not isinstance(question_rows[0], dict):
        reasons.append("canonical_question_asset_not_unique")
        return None, reasons
    asset = question_rows[0]
    relative = str(asset.get("path") or "")
    try:
        asset_path = _safe_relative(DETAILS_ROOT, relative, f"{node} question asset")
    except SelectionError:
        return None, ["canonical_question_asset_missing"]
    expected = str(asset.get("sha256") or "")
    if not SHA_RE.fullmatch(expected) or _file_sha(asset_path) != expected:
        return None, ["canonical_question_asset_hash_drift"]
    card_ref = str(bridge.get("detail_note") or f"cards/{node}.md")
    try:
        card_path = _safe_relative(DETAILS_ROOT, card_ref, f"{node} details card")
    except SelectionError:
        return None, ["details_card_missing"]
    card_text = card_path.read_text(encoding="utf-8")
    answer = canonical_answer_option(card_text)
    if answer is None:
        return None, ["canonical_answer_option_missing"]
    return {
        "vault": "kaoyan-408-details",
        "question_asset_ref": relative,
        "question_asset_sha256": expected,
        "question_asset_size_bytes": asset_path.stat().st_size,
        "details_card_ref": card_ref,
        "details_card_sha256": _file_sha(card_path),
        "answer_option_commitment_sha256": _sha({"node": node, "answer": answer, "card_sha256": _file_sha(card_path)}),
    }, reasons


def _source_bindings(repo: Path) -> list[dict[str, Any]]:
    paths = [
        repo / "节点总表.md", repo / "复习单元总表.md", repo / "复习单元节点映射.md",
        repo / "原题复做轨总表.md", repo / "关系边表.md", repo / "知识点命中索引.md",
        repo / review_loop.LOOP_REL / "events.jsonl",
        Path(current_context.__file__).resolve(),
        Path(question_exposure.__file__).resolve(),
        BRIDGE_PATH,
    ]
    bindings: list[dict[str, str]] = []
    for path in paths:
        if path == BRIDGE_PATH:
            ref = "private://kaoyan-408-obsidian-bridge/records.json"
        else:
            resolved = path.resolve(strict=True)
            try:
                ref = resolved.relative_to(repo).as_posix()
            except ValueError:
                ref = str(resolved)
        bindings.append({"ref": ref, "sha256": _file_sha(path)})
    return bindings


def freeze(repo: Path, day: dt.date, *, max_candidates: int = MAX_CANDIDATES) -> dict[str, Any]:
    repo = repo.resolve(strict=True)
    audit = review_loop.audit(repo)
    if audit.get("status") != "PASS" or audit.get("active_session_count") != 0:
        raise SelectionError("review state must be audited with zero active sessions")
    events = review_loop._read_events(repo)
    state = review_loop._reduce(events)
    nodes = _node_rows(repo)
    tracks = _track_rows(repo)
    unit_nodes, node_unit = _mapping(repo)
    relations = _relation_rows(repo)
    bridges = _bridge_rows()
    try:
        display_exposure_set = question_exposure.recent_exposure_set(repo, day)
    except question_exposure.QuestionExposureError as exc:
        raise SelectionError(f"learner-visible exposure validation failed: {exc}") from exc
    due_actions = [
        action for action in state.get("pending_actions") or []
        if action.get("action_type") == "review"
        and STAGE_RE.fullmatch(str(action.get("stage") or ""))
        and _date(str(action.get("due_date") or "")) <= day
    ]
    action_rows = sorted(
        [
            {key: action.get(key) for key in (
                "action_id", "source_id", "source_label", "identity", "review_unit_id", "formal_node_id",
                "formal_node_ids", "due_date", "stage", "first_break", "reason", "origin_date",
                "trigger_event_id", "trigger_event_ids", "status",
            )}
            for action in due_actions
        ],
        key=lambda row: (str(row["due_date"]), str(row["action_id"])),
    )
    eligible_sha = _sha(action_rows)
    candidates: list[dict[str, Any]] = []
    for action in due_actions:
        action_id = str(action.get("action_id") or "")
        unit = str(action.get("review_unit_id") or "") or None
        resolved_action_nodes = _action_nodes(action, unit_nodes)
        source_identity, source_reason_codes = resolve_action_source_identity(
            action,
            resolved_action_nodes,
            set(nodes),
        )
        for node in resolved_action_nodes:
            reasons: list[str] = list(source_reason_codes)
            track_list = tracks.get(node, [])
            if len(track_list) != 1:
                reasons.append("canonical_original_track_not_unique")
                track = None
            else:
                track = track_list[0]
            node_row = nodes.get(node)
            if node_row is None:
                reasons.append("formal_node_missing")
            track_source = str((track or {}).get("来源ID") or (node_row or {}).get("来源ID") or "")
            outcomes = _closed_book_outcomes(events, node, track_source)
            latest = outcomes[-1] if outcomes else None
            if latest is None:
                last_date = None
                gap = None
            else:
                last_date = latest["_observed"]
                gap = (day - last_date).days
                if gap < 0:
                    reasons.append("last_answer_date_in_future")
            original, asset_reasons = _original_binding(node, bridges.get(node, {}))
            reasons.extend(asset_reasons)
            recent_original_displays: list[dict[str, Any]] = []
            last_display_date = None
            mode = None
            if original is not None:
                mode, last_display_date, recent_original_displays = (
                    original_surface_from_exposure_set(
                        day,
                        original["question_asset_sha256"],
                        display_exposure_set,
                    )
                )
                if mode is None:
                    reasons.append("last_original_display_date_in_future")
            points = _split_points(str((node_row or {}).get("命中知识点") or ""))
            main_point = str((node_row or {}).get("主知识点") or "")
            if main_point and main_point not in points:
                points.insert(0, main_point)
            if not points:
                reasons.append("knowledge_point_missing")
            exposures = _exposures(events, day, node, unit or node_unit.get(node), track_source)
            latest_view = None if latest is None else {
                "event_id": latest.get("event_id"),
                "event_sha256": _sha({key: value for key, value in latest.items() if key != "_observed"}),
                "observed_date": last_date.isoformat(),
                "first_result": latest.get("first_result"),
                "confidence": latest.get("confidence"),
                "prompt_level": latest.get("prompt_level"),
                "hint_used": bool(latest.get("hint_used")),
                "first_break": latest.get("first_break"),
            }
            candidate_id = "MC-" + _sha({"action_id": action_id, "formal_node_id": node})[:20]
            candidate = {
                "candidate_id": candidate_id,
                "action_id": action_id,
                **source_identity,
                "review_unit_id": unit or node_unit.get(node),
                "formal_node_id": node,
                "stage": action.get("stage"),
                "due_date": action.get("due_date"),
                "overdue_days": max((day - _date(str(action.get("due_date")))).days, 0),
                "first_break": action.get("first_break"),
                "action_reason": action.get("reason"),
                "trigger_event_id": action.get("trigger_event_id"),
                "knowledge_points": points[:12],
                "node_summary": {
                    "subject": (node_row or {}).get("科目"),
                    "module": (node_row or {}).get("主模块"),
                    "core_point": (node_row or {}).get("核心考点"),
                    "error_tags": (node_row or {}).get("错因标签"),
                },
                "relations": relations.get(node, []),
                "latest_original_answer": latest_view,
                "last_answer_date": None if last_date is None else last_date.isoformat(),
                "last_answer_result": None if latest is None else latest.get("first_result"),
                "original_gap_days": gap,
                "surface_mode": mode,
                "recent_exposure": exposures,
                "recent_learner_visible_displays": recent_original_displays,
                "last_original_display_date": (
                    None if last_display_date is None else last_display_date.isoformat()
                ),
                "original_question": original,
                "eligibility_status": "eligible" if not reasons else "needs_user",
                "eligibility_reason_codes": sorted(set(reasons)),
                "evidence_refs": [
                    {"ref": "review-event://" + str(latest.get("event_id")), "sha256": latest_view["event_sha256"], "role": "latest_exact_original_outcome"}
                    for _ in [0] if latest is not None
                ] + [
                    {"ref": f"row://节点总表.md/{node}", "sha256": _row_hash(node_row), "role": "formal_node"}
                    for _ in [0] if node_row is not None
                ] + [
                    {"ref": f"row://原题复做轨总表.md/{node}", "sha256": _row_hash(track), "role": "original_track"}
                    for _ in [0] if track is not None
                ],
            }
            candidate["candidate_sha256"] = _sha(candidate)
            candidates.append(candidate)
    stage_rank = {"D1_rescue": 0, "D1": 1, "D3": 2, "D7": 3, "D14": 4, "D30": 5, "D60": 6, "D90": 7, "original_track": 8}
    candidates = sorted(
        candidates,
        key=lambda row: (
            0 if row["eligibility_status"] == "eligible" else 1,
            stage_rank.get(str(row["stage"]), 99),
            -int(row["overdue_days"]),
            row["formal_node_id"],
            row["action_id"],
        ),
    )[:max_candidates]
    ledger = repo / review_loop.LOOP_REL / "events.jsonl"
    snapshot = {
        "schema": SNAPSHOT_SCHEMA,
        "review_date": day.isoformat(),
        "max_items": MAX_FINAL_ITEMS,
        "seven_day_rule": {"derived_variant_when_gap_days_lt": 7, "original_allowed_when_gap_days_gte": 7},
        "review_ledger_cutoff": {"byte_size": ledger.stat().st_size, "event_count": len(events), "sha256": _file_sha(ledger)},
        "eligible_due_actions": action_rows,
        "eligible_due_action_set_sha256": eligible_sha,
        "learner_visible_exposure_set": display_exposure_set,
        "learner_visible_exposure_set_sha256": display_exposure_set[
            "fingerprint_sha256"
        ],
        "source_bindings": _source_bindings(repo),
        "candidate_count": len(candidates),
        "candidates": candidates,
        "formal_write_count": 0,
        "learner_evidence_write_count": 0,
    }
    receipt = _write_object(repo, "snapshots", snapshot)
    return {"status": "frozen", "snapshot_ref": receipt["ref"], "snapshot_sha256": receipt["sha256"], "candidate_count": len(candidates), "eligible_candidate_count": sum(row["eligibility_status"] == "eligible" for row in candidates), "formal_write_count": 0}


def _object(repo: Path, ref: str, expected: str) -> dict[str, Any]:
    path = _safe_relative(repo, ref, "selection object")
    value = _read_json(path)
    if _sha(value) != expected:
        raise SelectionError("selection object hash drifted")
    return value


def _validated_luna_advisory_fields(item: Any, label: str) -> tuple[str, str, list[str], int]:
    required = {
        "candidate_id",
        "recommendation_reason",
        "counterevidence",
        "semantic_score",
    }
    if not isinstance(item, dict) or set(item) != required:
        raise SelectionError(f"{label} advisory row fields are invalid")
    candidate_id = str(item.get("candidate_id") or "")
    reason = item.get("recommendation_reason")
    counterevidence = item.get("counterevidence")
    score = item.get("semantic_score")
    if not candidate_id:
        raise SelectionError(f"{label} advisory candidate ID is empty")
    if not isinstance(reason, str) or not reason.strip() or len(reason) > 1200:
        raise SelectionError(f"{label} advisory recommendation is invalid")
    if (
        not isinstance(counterevidence, list)
        or len(counterevidence) > MAX_CANDIDATES
        or any(
            not isinstance(row, str) or len(row) > 500
            for row in counterevidence
        )
    ):
        raise SelectionError(f"{label} advisory counterevidence is invalid")
    if isinstance(score, bool) or not isinstance(score, int) or not 0 <= score <= 100:
        raise SelectionError(f"{label} advisory semantic score is invalid")
    return candidate_id, reason, counterevidence, score


def _luna_shard_scope_value(
    snapshot_sha: str,
    shard_id: str,
    authorized_candidate_ids: list[str],
    by_id: dict[str, dict[str, Any]],
) -> dict[str, Any]:
    return {
        "schema": LUNA_SHARD_SCOPE_SCHEMA,
        "snapshot_sha256": snapshot_sha,
        "shard_id": shard_id,
        "authorized_candidate_bindings": [
            {
                "candidate_id": candidate_id,
                "snapshot_candidate_sha256": _sha(by_id[candidate_id]),
            }
            for candidate_id in sorted(authorized_candidate_ids)
        ],
        "formal_write_count": 0,
    }


def luna_shard_scope_sha256(
    snapshot_sha: str,
    shard_id: str,
    authorized_candidate_ids: list[str],
    snapshot: dict[str, Any],
) -> str:
    """Materialize the deterministic scope hash used by one native Luna shard."""

    candidates = snapshot.get("candidates")
    if not isinstance(candidates, list):
        raise SelectionError("selection snapshot candidates are invalid")
    by_id = {
        str(row.get("candidate_id") or ""): row
        for row in candidates
        if isinstance(row, dict) and row.get("candidate_id")
    }
    if len(by_id) != len(candidates):
        raise SelectionError("selection snapshot candidate IDs are invalid or repeated")
    if not isinstance(shard_id, str) or not SAFE_ID_RE.fullmatch(shard_id):
        raise SelectionError("Luna shard ID is invalid")
    if (
        not isinstance(authorized_candidate_ids, list)
        or not authorized_candidate_ids
        or any(not isinstance(row, str) or not row for row in authorized_candidate_ids)
        or len(set(authorized_candidate_ids)) != len(authorized_candidate_ids)
    ):
        raise SelectionError("Luna shard authorized candidate IDs are invalid")
    if any(candidate_id not in by_id for candidate_id in authorized_candidate_ids):
        raise SelectionError("Luna shard authorized an unknown candidate ID")
    return _sha(
        _luna_shard_scope_value(
            snapshot_sha,
            shard_id,
            authorized_candidate_ids,
            by_id,
        )
    )


def _luna_shard_plan_value(
    snapshot_sha: str,
    planning_reason: str,
    shard_scopes: list[dict[str, Any]],
    delegation_mode: str = "native_luna_shards",
) -> dict[str, Any]:
    return {
        "schema": LUNA_SHARD_PLAN_SCHEMA,
        "snapshot_sha256": snapshot_sha,
        "planner": "root_sol",
        "delegation_mode": delegation_mode,
        "planning_reason": planning_reason,
        "shards": sorted(
            [
                {
                    "shard_id": row["shard_id"],
                    "evidence_domain": row["evidence_domain"],
                    "authorized_candidate_ids": sorted(
                        row["authorized_candidate_ids"]
                    ),
                    "authorized_scope_sha256": row[
                        "authorized_scope_sha256"
                    ],
                }
                for row in shard_scopes
            ],
            key=lambda row: row["shard_id"],
        ),
        "formal_write_count": 0,
    }


def validate_luna_shards(
    repo: Path,
    snapshot_ref: str,
    snapshot_sha: str,
    bundle_path: Path,
) -> dict[str, Any]:
    """Validate and fan in one dynamically sized, read-only Luna shard bundle."""

    snapshot = _object(repo, snapshot_ref, snapshot_sha)
    candidates = snapshot.get("candidates")
    if not isinstance(candidates, list):
        raise SelectionError("selection snapshot candidates are invalid")
    by_id = {
        str(row.get("candidate_id") or ""): row
        for row in candidates
        if isinstance(row, dict) and row.get("candidate_id")
    }
    if len(by_id) != len(candidates):
        raise SelectionError("selection snapshot candidate IDs are invalid or repeated")
    bundle = _read_json(bundle_path)
    required_bundle = {
        "schema",
        "snapshot_sha256",
        "planner",
        "delegation_mode",
        "planning_reason",
        "shard_plan_sha256",
        "shards",
        "formal_write_count",
    }
    if set(bundle) != required_bundle:
        raise SelectionError("Luna shard bundle fields are invalid")
    delegation_mode = bundle.get("delegation_mode")
    if (
        bundle.get("schema") != LUNA_SHARD_BUNDLE_SCHEMA
        or bundle.get("snapshot_sha256") != snapshot_sha
        or bundle.get("planner") != "root_sol"
        or delegation_mode not in {"root_direct", "native_luna_shards"}
        or bundle.get("formal_write_count") != 0
    ):
        raise SelectionError("Luna shard bundle binding is invalid")
    planning_reason = bundle.get("planning_reason")
    if (
        not isinstance(planning_reason, str)
        or not planning_reason.strip()
        or len(planning_reason) > 1200
    ):
        raise SelectionError("Luna shard planning reason is invalid")
    shards = bundle.get("shards")
    if not isinstance(shards, list):
        raise SelectionError("Luna shard bundle shards are invalid")
    if delegation_mode == "root_direct" and shards:
        raise SelectionError("root_direct shard bundle must not contain native shards")
    if delegation_mode == "native_luna_shards" and not shards:
        raise SelectionError("native Luna shard bundle must contain at least one shard")
    safe_upper = min(MAX_CANDIDATES, len(by_id))
    if len(shards) > safe_upper:
        raise SelectionError("Luna shard count exceeds the frozen candidate safety bound")

    required_shard = {
        "shard_id",
        "evidence_domain",
        "authorized_candidate_ids",
        "authorized_scope_sha256",
        "runtime_identity_receipt",
        "runtime_identity_receipt_sha256",
        "advisory",
    }
    required_advisory = {
        "schema",
        "snapshot_sha256",
        "shard_id",
        "authorized_scope_sha256",
        "runtime_identity_receipt_sha256",
        "candidates",
        "formal_write_count",
    }
    seen_shards: set[str] = set()
    globally_authorized: set[str] = set()
    globally_advised: set[str] = set()
    shard_scopes: list[dict[str, Any]] = []
    shard_provenance: list[dict[str, Any]] = []
    shard_bundle_bindings: list[dict[str, Any]] = []
    validated_candidates: list[dict[str, Any]] = []
    for shard in shards:
        if not isinstance(shard, dict) or set(shard) != required_shard:
            raise SelectionError("Luna shard fields are invalid")
        shard_id = shard.get("shard_id")
        evidence_domain = shard.get("evidence_domain")
        authorized_ids = shard.get("authorized_candidate_ids")
        scope_sha = shard.get("authorized_scope_sha256")
        if (
            not isinstance(shard_id, str)
            or not SAFE_ID_RE.fullmatch(shard_id)
            or shard_id in seen_shards
        ):
            raise SelectionError("Luna shard ID is invalid or repeated")
        seen_shards.add(shard_id)
        if (
            not isinstance(evidence_domain, str)
            or not evidence_domain.strip()
            or len(evidence_domain) > 300
        ):
            raise SelectionError("Luna shard evidence domain is invalid")
        if (
            not isinstance(authorized_ids, list)
            or not authorized_ids
            or any(not isinstance(row, str) or not row for row in authorized_ids)
            or len(set(authorized_ids)) != len(authorized_ids)
        ):
            raise SelectionError("Luna shard authorized scope is empty or invalid")
        unknown_ids = set(authorized_ids) - set(by_id)
        if unknown_ids:
            raise SelectionError("Luna shard authorized an unknown candidate ID")
        if globally_authorized.intersection(authorized_ids):
            raise SelectionError("Luna shard authorized scopes overlap")
        globally_authorized.update(authorized_ids)
        expected_scope_sha = luna_shard_scope_sha256(
            snapshot_sha,
            shard_id,
            authorized_ids,
            snapshot,
        )
        if scope_sha != expected_scope_sha:
            raise SelectionError("Luna shard scope hash drifted")

        runtime = shard.get("runtime_identity_receipt")
        expected_runtime = {
            "schema": LUNA_SHARD_RECEIPT_SCHEMA,
            "model": "gpt-5.6-luna",
            "reasoning_effort": "max",
            "service_tier": "fast",
            "native_leaf": True,
            "identity_status": "runtime_confirmed",
            "snapshot_sha256": snapshot_sha,
            "shard_id": shard_id,
            "authorized_scope_sha256": scope_sha,
            "formal_write_count": 0,
        }
        if runtime != expected_runtime:
            raise SelectionError("native Luna shard Fast identity is not confirmed")
        runtime_sha = _sha(runtime)
        if shard.get("runtime_identity_receipt_sha256") != runtime_sha:
            raise SelectionError("Luna shard runtime receipt hash drifted")

        advisory = shard.get("advisory")
        if not isinstance(advisory, dict) or set(advisory) != required_advisory:
            raise SelectionError("Luna shard advisory fields are invalid")
        if (
            advisory.get("schema") != LUNA_SHARD_ADVISORY_SCHEMA
            or advisory.get("snapshot_sha256") != snapshot_sha
            or advisory.get("shard_id") != shard_id
            or advisory.get("authorized_scope_sha256") != scope_sha
            or advisory.get("runtime_identity_receipt_sha256") != runtime_sha
            or advisory.get("formal_write_count") != 0
        ):
            raise SelectionError("Luna shard advisory binding is invalid")
        advisory_rows = advisory.get("candidates")
        if not isinstance(advisory_rows, list) or len(advisory_rows) > len(authorized_ids):
            raise SelectionError("Luna shard advisory cardinality is invalid")
        local_seen: set[str] = set()
        advisory_candidate_ids: list[str] = []
        for item in advisory_rows:
            candidate_id, reason, counterevidence, score = (
                _validated_luna_advisory_fields(item, f"Luna shard {shard_id}")
            )
            if candidate_id in globally_advised:
                raise SelectionError("Luna candidate repeated across shards")
            if candidate_id in local_seen:
                raise SelectionError("Luna shard returned a duplicate candidate ID")
            if candidate_id not in authorized_ids:
                raise SelectionError("Luna shard returned an unauthorized candidate ID")
            local_seen.add(candidate_id)
            globally_advised.add(candidate_id)
            advisory_candidate_ids.append(candidate_id)
            source = by_id[candidate_id]
            validated_candidates.append(
                {
                    **source,
                    "luna_recommendation_reason": reason,
                    "luna_counterevidence": counterevidence,
                    "luna_semantic_score": score,
                    "luna_shard_provenance": {
                        "shard_id": shard_id,
                        "evidence_domain": evidence_domain,
                        "authorized_scope_sha256": scope_sha,
                        "runtime_identity_receipt_sha256": runtime_sha,
                    },
                }
            )
        shard_scope = {
            "shard_id": shard_id,
            "evidence_domain": evidence_domain,
            "authorized_candidate_ids": authorized_ids,
            "authorized_scope_sha256": scope_sha,
        }
        shard_scopes.append(shard_scope)
        shard_provenance.append(
            {
                **shard_scope,
                "runtime_identity_receipt_sha256": runtime_sha,
                "advisory_candidate_ids": sorted(advisory_candidate_ids),
            }
        )
        shard_bundle_bindings.append(
            {
                "shard_id": shard_id,
                "authorized_scope_sha256": scope_sha,
                "runtime_identity_receipt_sha256": runtime_sha,
                "advisory_sha256": _sha(advisory),
            }
        )

    plan_value = _luna_shard_plan_value(
        snapshot_sha,
        planning_reason,
        shard_scopes,
        delegation_mode,
    )
    plan_sha = _sha(plan_value)
    if bundle.get("shard_plan_sha256") != plan_sha:
        raise SelectionError("Luna shard plan hash drifted")
    bundle_binding = {
        "schema": "luna_morning_candidate_shard_bundle_binding_v1",
        "snapshot_sha256": snapshot_sha,
        "delegation_mode": delegation_mode,
        "shard_plan_sha256": plan_sha,
        "shards": sorted(
            shard_bundle_bindings,
            key=lambda row: row["shard_id"],
        ),
        "formal_write_count": 0,
    }
    if delegation_mode == "root_direct":
        validated_candidates = [
            {
                **source,
                "root_direct_provenance": {
                    "delegation_mode": "root_direct",
                    "snapshot_sha256": snapshot_sha,
                    "snapshot_candidate_sha256": _sha(source),
                },
            }
            for source in sorted(
                (
                    row
                    for row in candidates
                    if row.get("eligibility_status") == "eligible"
                ),
                key=lambda row: row["candidate_id"],
            )
        ]
    validated_value = {
        "schema": LUNA_VALIDATED_SCHEMA,
        "validation_mode": (
            "root_direct"
            if delegation_mode == "root_direct"
            else "dynamic_native_luna_shards"
        ),
        "delegation_mode": delegation_mode,
        "snapshot_ref": snapshot_ref,
        "snapshot_sha256": snapshot_sha,
        "shard_plan_sha256": plan_sha,
        "shard_bundle_sha256": _sha(bundle_binding),
        "shard_count": len(shards),
        "shard_provenance": sorted(
            shard_provenance,
            key=lambda row: row["shard_id"],
        ),
        "candidates": sorted(
            validated_candidates,
            key=lambda row: (
                row.get("luna_shard_provenance", {}).get("shard_id", ""),
                row["candidate_id"],
            ),
        ),
        "formal_write_count": 0,
    }
    receipt = _write_object(repo, "luna", validated_value)
    return {
        "status": "validated",
        "validation_mode": validated_value["validation_mode"],
        "delegation_mode": delegation_mode,
        "candidate_set_ref": receipt["ref"],
        "candidate_set_sha256": receipt["sha256"],
        "candidate_count": len(validated_candidates),
        "shard_count": len(shards),
        "shard_plan_sha256": plan_sha,
        "formal_write_count": 0,
    }


def validate_luna(repo: Path, snapshot_ref: str, snapshot_sha: str, candidate_path: Path, runtime_path: Path) -> dict[str, Any]:
    snapshot = _object(repo, snapshot_ref, snapshot_sha)
    candidate_set = _read_json(candidate_path)
    runtime = _read_json(runtime_path)
    runtime_sha = _sha(runtime)
    if runtime != {
        "schema": LUNA_RECEIPT_SCHEMA,
        "model": "gpt-5.6-luna",
        "reasoning_effort": "max",
        "service_tier": "fast",
        "native_leaf": True,
        "identity_status": "runtime_confirmed",
        "formal_write_count": 0,
    }:
        raise SelectionError("native Luna Fast identity is not confirmed")
    if set(candidate_set) != {"schema", "snapshot_sha256", "runtime_identity_receipt_sha256", "candidates", "formal_write_count"}:
        raise SelectionError("Luna candidate set fields are invalid")
    if candidate_set.get("schema") != LUNA_SCHEMA or candidate_set.get("snapshot_sha256") != snapshot_sha or candidate_set.get("runtime_identity_receipt_sha256") != runtime_sha or candidate_set.get("formal_write_count") != 0:
        raise SelectionError("Luna candidate set binding is invalid")
    by_id = {row["candidate_id"]: row for row in snapshot["candidates"]}
    validated: list[dict[str, Any]] = []
    seen: set[str] = set()
    required = {"candidate_id", "action_id", "review_unit_id", "formal_node_id", "knowledge_points", "evidence_refs", "last_answer_date", "last_answer_result", "recent_exposure", "recommendation_reason", "counterevidence", "semantic_score"}
    for row in candidate_set.get("candidates") or []:
        if not isinstance(row, dict) or set(row) != required:
            raise SelectionError("Luna candidate row fields are invalid")
        candidate_id = str(row.get("candidate_id") or "")
        source = by_id.get(candidate_id)
        if source is None or candidate_id in seen:
            raise SelectionError("Luna returned an unknown or duplicate candidate ID")
        seen.add(candidate_id)
        for field in ("action_id", "review_unit_id", "formal_node_id", "knowledge_points", "evidence_refs", "last_answer_date", "last_answer_result", "recent_exposure"):
            if row.get(field) != source.get(field):
                raise SelectionError(f"Luna candidate {field} drifted")
        score = row.get("semantic_score")
        if not isinstance(score, int) or not 0 <= score <= 100:
            raise SelectionError("Luna semantic score is invalid")
        if not isinstance(row.get("recommendation_reason"), str) or not isinstance(row.get("counterevidence"), list):
            raise SelectionError("Luna advisory text is invalid")
        validated.append({**source, "luna_recommendation_reason": row["recommendation_reason"], "luna_counterevidence": row["counterevidence"], "luna_semantic_score": score})
    validated_value = {
        "schema": LUNA_VALIDATED_SCHEMA,
        "snapshot_ref": snapshot_ref,
        "snapshot_sha256": snapshot_sha,
        "runtime_identity_receipt_sha256": runtime_sha,
        "candidates": validated,
        "formal_write_count": 0,
    }
    receipt = _write_object(repo, "luna", validated_value)
    return {"status": "validated", "candidate_set_ref": receipt["ref"], "candidate_set_sha256": receipt["sha256"], "candidate_count": len(validated), "formal_write_count": 0}


def materialize_luna(repo: Path, snapshot_ref: str, snapshot_sha: str, advisory_path: Path, runtime_path: Path) -> dict[str, Any]:
    snapshot = _object(repo, snapshot_ref, snapshot_sha)
    advisory = _read_json(advisory_path)
    runtime = _read_json(runtime_path)
    if advisory.get("schema") != "luna_morning_advisory_v1" or set(advisory) != {"schema", "snapshot_sha256", "candidates", "formal_write_count"} or advisory.get("snapshot_sha256") != snapshot_sha or advisory.get("formal_write_count") != 0:
        raise SelectionError("Luna advisory binding is invalid")
    by_id = {row["candidate_id"]: row for row in snapshot["candidates"]}
    rows: list[dict[str, Any]] = []
    seen: set[str] = set()
    for item in advisory.get("candidates") or []:
        if not isinstance(item, dict) or set(item) != {"candidate_id", "recommendation_reason", "counterevidence", "semantic_score"}:
            raise SelectionError("Luna advisory row is invalid")
        source = by_id.get(str(item.get("candidate_id") or ""))
        if source is None or source["candidate_id"] in seen:
            raise SelectionError("Luna advisory returned unknown or duplicate candidate")
        seen.add(source["candidate_id"])
        rows.append({
            **{key: source.get(key) for key in (
                "candidate_id", "action_id", "review_unit_id", "formal_node_id",
                "knowledge_points", "evidence_refs", "last_answer_date",
                "last_answer_result", "recent_exposure",
            )},
            "recommendation_reason": item["recommendation_reason"],
            "counterevidence": item["counterevidence"],
            "semantic_score": item["semantic_score"],
        })
    raw = {
        "schema": LUNA_SCHEMA,
        "snapshot_sha256": snapshot_sha,
        "runtime_identity_receipt_sha256": _sha(runtime),
        "candidates": rows,
        "formal_write_count": 0,
    }
    receipt = _write_object(repo, "luna-raw", raw)
    return {"status": "materialized", "candidate_set_ref": receipt["ref"], "candidate_set_sha256": receipt["sha256"], "candidate_count": len(rows), "formal_write_count": 0}


def seal_sol(repo: Path, snapshot_ref: str, snapshot_sha: str, luna_ref: str, luna_sha: str, decision_path: Path) -> dict[str, Any]:
    snapshot = _object(repo, snapshot_ref, snapshot_sha)
    luna = _object(repo, luna_ref, luna_sha)
    if (
        luna.get("schema") != LUNA_VALIDATED_SCHEMA
        or luna.get("snapshot_ref") != snapshot_ref
        or luna.get("snapshot_sha256") != snapshot_sha
        or luna.get("formal_write_count") != 0
        or not isinstance(luna.get("candidates"), list)
    ):
        raise SelectionError("validated Luna candidate set binding is invalid")
    decision = _read_json(decision_path)
    required = {"schema", "decision_maker", "snapshot_sha256", "luna_candidate_set_sha256", "max_items", "selected", "rejected", "formal_write_count"}
    if set(decision) != required or decision.get("schema") != SOL_SCHEMA or decision.get("decision_maker") != "root_sol" or decision.get("snapshot_sha256") != snapshot_sha or decision.get("luna_candidate_set_sha256") != luna_sha or decision.get("max_items") != 5 or decision.get("formal_write_count") != 0:
        raise SelectionError("Sol decision binding is invalid")
    snapshot_by_id = {
        row["candidate_id"]: row
        for row in snapshot.get("candidates") or []
        if isinstance(row, dict) and row.get("candidate_id")
    }
    by_id: dict[str, dict[str, Any]] = {}
    for row in luna["candidates"]:
        if not isinstance(row, dict) or not row.get("candidate_id"):
            raise SelectionError("validated Luna candidate row is invalid")
        candidate_id = row["candidate_id"]
        source = snapshot_by_id.get(candidate_id)
        if source is None or candidate_id in by_id:
            raise SelectionError("validated Luna candidate ID is unknown or repeated")
        if any(row.get(key) != value for key, value in source.items()):
            raise SelectionError("validated Luna candidate drifted from the snapshot")
        by_id[candidate_id] = row
    selected = decision.get("selected")
    rejected = decision.get("rejected")
    if not isinstance(selected, list) or len(selected) > 5 or not isinstance(rejected, list):
        raise SelectionError("Sol decision cardinality is invalid")
    selected_ids: list[str] = []
    selected_rows: list[dict[str, Any]] = []
    nodes: set[str] = set()
    points: set[str] = set()
    for row in selected:
        if not isinstance(row, dict) or set(row) != {"candidate_id", "selection_reason"}:
            raise SelectionError("Sol selected row is invalid")
        candidate = by_id.get(str(row.get("candidate_id") or ""))
        if candidate is None or candidate["candidate_id"] in selected_ids:
            raise SelectionError("Sol selected unknown or duplicate candidate")
        if candidate.get("eligibility_status") != "eligible" or candidate.get("surface_mode") not in {"original_question", "derived_variant"}:
            raise SelectionError("Sol selected an ineligible candidate")
        if (
            candidate.get("surface_mode") == "original_question"
            and candidate.get("recent_learner_visible_displays")
        ):
            raise SelectionError(
                "Sol selected a recently displayed original question"
            )
        node = candidate["formal_node_id"]
        primary = (candidate.get("knowledge_points") or [None])[0]
        if node in nodes or primary is None or primary in points:
            raise SelectionError("Sol selection duplicates a node or primary knowledge point")
        nodes.add(node)
        points.add(primary)
        selected_ids.append(candidate["candidate_id"])
        selected_rows.append({**candidate, "sol_selection_reason": row["selection_reason"]})
    rejected_ids = []
    for row in rejected:
        if not isinstance(row, dict) or set(row) != {"candidate_id", "reason_code", "reason"} or row.get("candidate_id") not in by_id:
            raise SelectionError("Sol rejected row is invalid")
        rejected_ids.append(row["candidate_id"])
    if set(selected_ids) | set(rejected_ids) != set(by_id) or set(selected_ids) & set(rejected_ids):
        raise SelectionError("Sol decision must account for every Luna candidate exactly once")
    sealed = {
        **decision,
        "snapshot_ref": snapshot_ref,
        "luna_candidate_set_ref": luna_ref,
        "selected_candidates": selected_rows,
        "selected_count": len(selected_rows),
        "selection_policy": POLICY_VERSION,
        "learner_evidence_write_count": 0,
    }
    receipt = _write_object(repo, "sol", sealed)
    return {"status": "sealed", "decision_ref": receipt["ref"], "decision_sha256": receipt["sha256"], "selected_count": len(selected_rows), "formal_write_count": 0}


def _queue_section(index: int, candidate: dict[str, Any]) -> list[str]:
    item_id = f"MQ-{index:02d}"
    mode = candidate["surface_mode"]
    kind = "original_question" if mode == "original_question" else "derived_d0"
    point = (candidate.get("knowledge_points") or [candidate["formal_node_id"]])[0]
    source_id = str(candidate.get("source_id") or "").strip()
    provenance = str(candidate.get("source_id_provenance") or "").strip()
    source_label = _single_line_source_label(candidate.get("source_label"))
    if not current_context.SAFE_ID_RE.fullmatch(source_id):
        raise SelectionError(
            f"{item_id} source_id is incompatible with CurrentQuestionContext"
        )
    if provenance not in SOURCE_ID_PROVENANCE:
        raise SelectionError(f"{item_id} source_id provenance is invalid")
    if provenance == "formal_node_fallback" and source_id != candidate.get(
        "formal_node_id"
    ):
        raise SelectionError(f"{item_id} formal-node source fallback drifted")
    lines = [
        f"#### {item_id} {candidate['stage']} 语义推荐 · {point}", "",
        f"- source_id: `{source_id}`",
        "- source_label: "
        + json.dumps(source_label or source_id, ensure_ascii=False),
        f"- source_id_provenance: {provenance}",
        f"- item_kind: {kind}",
        f"- selection_reason: {candidate['sol_selection_reason']}",
        "- prompt: 不看资料，先独立作答并记录置信度；未作答前不得展开答案或解析。",
        f"- action_id: `{candidate['action_id']}`",
        f"- trigger_event_id: `{candidate.get('trigger_event_id') or 'none'}`",
        f"- review_stage: {candidate['stage']}",
        f"- formal_node_id: `{candidate['formal_node_id']}`",
        f"- review_unit_id: `{candidate.get('review_unit_id') or 'none'}`",
        f"- surface_mode: {mode}",
        f"- semantic_candidate_id: `{candidate['candidate_id']}`",
        f"- knowledge_point: {point}",
    ]
    if mode == "original_question":
        original = candidate["original_question"]
        lines.extend([
            f"- original_question_asset_ref: `{original['question_asset_ref']}`",
            f"- original_question_asset_sha256: `{original['question_asset_sha256']}`",
            f"- original_question_asset_size_bytes: {original['question_asset_size_bytes']}",
            f"- original_details_card_ref: `{original['details_card_ref']}`",
            f"- original_details_card_sha256: `{original['details_card_sha256']}`",
        ])
    lines.extend(["- first_attempt: 先无提示作答，再记录答后置信度与反应时。", ""])
    return lines


def publish_queue(repo: Path, decision_ref: str, decision_sha: str) -> dict[str, Any]:
    repo = repo.resolve(strict=True)
    decision = _object(repo, decision_ref, decision_sha)
    snapshot = _object(repo, decision["snapshot_ref"], decision["snapshot_sha256"])
    day = _date(snapshot["review_date"])
    if not 1 <= len(decision["selected_candidates"]) <= 5:
        raise SelectionError("semantic selection has no publishable candidates")
    current = validate_snapshot_freshness(repo, snapshot)
    if current["status"] != "fresh":
        raise SelectionError("semantic selection snapshot is stale")
    renderer_sha = _file_sha(Path(__file__).resolve())
    queue_schema_sha = _file_sha(repo / "schema/morning-review-action-v3.md")
    render_input_sha = _sha({"decision_sha256": decision_sha, "selected": [row["candidate_id"] for row in decision["selected_candidates"]]})
    generation_input = _sha({
        "snapshot_sha256": decision["snapshot_sha256"], "decision_sha256": decision_sha,
        "eligible_due_action_set_sha256": snapshot["eligible_due_action_set_sha256"],
        "queue_render_input_sha256": render_input_sha, "queue_renderer_sha256": renderer_sha,
        "queue_schema_sha256": queue_schema_sha, "policy": POLICY_VERSION,
    })
    generation_id = f"MRG-{day.isoformat()}-{generation_input}"
    cutoff = snapshot["review_ledger_cutoff"]
    front = [
        "---", "schema: morning_review_action_queue_v3", f"review_date: {day.isoformat()}",
        f"generation_id: {generation_id}", f"generation_input_sha256: {generation_input}",
        "generation_status: queue_bound", f"eligible_due_action_set_sha256: {snapshot['eligible_due_action_set_sha256']}",
        f"formal_due_source_sha256: {_sha(snapshot['source_bindings'])}",
        f"queue_render_input_sha256: {render_input_sha}", f"scheduling_policy_version: {POLICY_VERSION}",
        f"review_ledger_cutoff_sha256: {cutoff['sha256']}", f"review_ledger_cutoff_bytes: {cutoff['byte_size']}",
        f"review_ledger_cutoff_event_count: {cutoff['event_count']}", f"queue_renderer_sha256: {renderer_sha}",
        f"queue_schema_sha256: {queue_schema_sha}", "definition_status: immutable_ready_snapshot",
        f"selection_mode: {SELECTION_MODE}", f"selection_snapshot_ref: {decision['snapshot_ref']}",
        f"selection_snapshot_sha256: {decision['snapshot_sha256']}",
        f"luna_candidate_set_ref: {decision['luna_candidate_set_ref']}",
        f"luna_candidate_set_sha256: {decision['luna_candidate_set_sha256']}",
        f"sol_selection_decision_ref: {decision_ref}", f"sol_selection_decision_sha256: {decision_sha}",
        "execution_mode: daily_loop", "coverage_mode: semantic_five", "target_minutes: 20",
        "max_items: 5", "baseline_quota: 0", "max_original: 5", f"inventory_count: {snapshot['candidate_count']}",
        f"scheduled_count: {len(decision['selected_candidates'])}", "formal_write_count: 0", "---", "",
        f"# {day.isoformat()} 408 晨间语义精选队列", "",
        "> [!important] 根 Sol 从原生 Luna 候选集中最终选择；少于五题时不补弱题。", "",
        "## 晨间项目", "",
    ]
    lines = list(front)
    for index, candidate in enumerate(decision["selected_candidates"], start=1):
        lines.extend(_queue_section(index, candidate))
    content = "\n".join(lines).rstrip() + "\n"
    root = repo / legacy_queue.QUEUE_GENERATION_ROOT_REL
    generation_dir = root / "generations" / day.isoformat() / generation_id
    queue = generation_dir / f"{generation_id}.md"
    queue_sha = hashlib.sha256(content.encode("utf-8")).hexdigest()
    queue_ref = queue.relative_to(repo).as_posix()
    manifest = {
        "schema": legacy_queue.QUEUE_GENERATION_SCHEMA, "status": "queue_bound",
        "generation_id": generation_id, "generation_input_sha256": generation_input,
        "review_date": day.isoformat(), "eligible_due_action_set_sha256": snapshot["eligible_due_action_set_sha256"],
        "formal_due_source_sha256": _sha(snapshot["source_bindings"]), "queue_render_input_sha256": render_input_sha,
        "scheduling_policy_version": POLICY_VERSION, "review_ledger_cutoff": cutoff,
        "queue_renderer_sha256": renderer_sha, "queue_schema_sha256": queue_schema_sha,
        "selection_mode": SELECTION_MODE, "selection_snapshot_ref": decision["snapshot_ref"],
        "selection_snapshot_sha256": decision["snapshot_sha256"], "luna_candidate_set_ref": decision["luna_candidate_set_ref"],
        "luna_candidate_set_sha256": decision["luna_candidate_set_sha256"], "sol_selection_decision_ref": decision_ref,
        "sol_selection_decision_sha256": decision_sha, "queue_ref": queue_ref, "queue_sha256": queue_sha,
        "formal_write_count": 0, "learner_evidence_write_count": 0,
    }
    manifest_path = generation_dir / "manifest.json"
    with review_loop.runtime_lock(repo):
        review_loop._assert_queue_publication_allowed(repo)
        if queue.exists() and queue.read_text(encoding="utf-8") != content:
            raise SelectionError("semantic queue hash collision")
        if not queue.exists():
            review_loop.atomic_write(queue, content)
        if manifest_path.exists() and _read_json(manifest_path) != manifest:
            raise SelectionError("semantic manifest hash collision")
        if not manifest_path.exists():
            review_loop.atomic_write_json(manifest_path, manifest)
        pointer = {
            "schema": legacy_queue.QUEUE_GENERATION_POINTER_SCHEMA, "review_date": day.isoformat(),
            "generation_id": generation_id, "generation_input_sha256": generation_input,
            "queue_ref": queue_ref, "queue_sha256": queue_sha,
            "manifest_ref": manifest_path.relative_to(repo).as_posix(), "manifest_sha256": _file_sha(manifest_path),
        }
        review_loop.atomic_write_json(root / "current" / f"{day.isoformat()}.json", pointer)
        # The immutable generation remains the audit source.  This fixed-date
        # Dashboard file is only the current MCP read projection and must
        # follow the selected generation; the sealed MCP intentionally reads
        # no generation pointer or arbitrary path supplied by a caller.
        dashboard_queue = (
            repo
            / review_loop.DASHBOARD_REL
            / f"{day.isoformat()}-408晨间行动队列.md"
        )
        review_loop.atomic_write(dashboard_queue, content)
        runtime = review_loop._write_runtime_views_locked(repo, queue)
    return {"status": "queue_bound", "generation_id": generation_id, "queue_ref": queue_ref, "queue_sha256": queue_sha, "scheduled_count": len(decision["selected_candidates"]), "runtime": runtime, "formal_write_count": 0}


def _current_due_action_sha(repo: Path, day: dt.date) -> str:
    state = review_loop._reduce(review_loop._read_events(repo))
    rows = sorted([
        {key: action.get(key) for key in (
            "action_id", "source_id", "source_label", "identity", "review_unit_id", "formal_node_id", "formal_node_ids",
            "due_date", "stage", "first_break", "reason", "origin_date", "trigger_event_id", "trigger_event_ids", "status",
        )}
        for action in state.get("pending_actions") or []
        if action.get("action_type") == "review" and STAGE_RE.fullmatch(str(action.get("stage") or "")) and _date(str(action.get("due_date") or "")) <= day
    ], key=lambda row: (str(row["due_date"]), str(row["action_id"])))
    return _sha(rows)


def validate_snapshot_freshness(repo: Path, snapshot: dict[str, Any]) -> dict[str, Any]:
    day = _date(snapshot["review_date"])
    reasons: list[str] = []
    ledger = repo / review_loop.LOOP_REL / "events.jsonl"
    cutoff = snapshot["review_ledger_cutoff"]
    with ledger.open("rb") as handle:
        prefix = handle.read(int(cutoff["byte_size"]))
    if len(prefix) != cutoff["byte_size"] or hashlib.sha256(prefix).hexdigest() != cutoff["sha256"]:
        reasons.append("review_ledger_prefix_changed")
    if _current_due_action_sha(repo, day) != snapshot["eligible_due_action_set_sha256"]:
        reasons.append("eligible_due_action_set_changed")
    try:
        current_exposure_set = question_exposure.recent_exposure_set(repo, day)
    except question_exposure.QuestionExposureError as exc:
        raise SelectionError(f"learner-visible exposure validation failed: {exc}") from exc
    if (
        current_exposure_set["fingerprint_sha256"]
        != snapshot.get("learner_visible_exposure_set_sha256")
    ):
        reasons.append("learner_visible_exposure_set_changed")
    for source in snapshot["source_bindings"]:
        ref = source["ref"]
        path = BRIDGE_PATH if ref == "private://kaoyan-408-obsidian-bridge/records.json" else repo / ref
        if not path.is_file() or _file_sha(path) != source["sha256"]:
            reasons.append("source_binding_changed")
            break
    return {
        "status": "fresh" if not reasons else "stale_pre_session",
        "fresh": not reasons,
        "reason_codes": reasons,
        "current_eligible_due_action_set_sha256": _current_due_action_sha(repo, day),
        "current_learner_visible_exposure_set_sha256": current_exposure_set[
            "fingerprint_sha256"
        ],
    }


def validate_semantic_queue_freshness(repo: Path, queue_path: Path) -> dict[str, Any]:
    front = legacy_queue._parse_frontmatter(queue_path.read_text(encoding="utf-8"))
    snapshot_ref = front.get("selection_snapshot_ref", "")
    snapshot_sha = front.get("selection_snapshot_sha256", "")
    snapshot = _object(repo, snapshot_ref, snapshot_sha)
    current = validate_snapshot_freshness(repo, snapshot)
    renderer_sha = _file_sha(Path(__file__).resolve())
    if front.get("queue_renderer_sha256") != renderer_sha:
        current["reason_codes"].append("queue_renderer_changed")
    current["fresh"] = not current["reason_codes"]
    current["status"] = "fresh" if current["fresh"] else "stale_pre_session"
    metadata = legacy_queue.queue_generation_metadata(queue_path)
    return {**metadata, **current, "selection_mode": SELECTION_MODE, "current_generation_id": metadata["generation_id"], "current_generation_input_sha256": metadata["generation_input_sha256"], "current_formal_due_source_sha256": metadata["formal_due_source_sha256"], "current_review_ledger_cutoff": snapshot["review_ledger_cutoff"]}


def original_private_evidence(surface_item: dict[str, Any]) -> dict[str, Any]:
    if surface_item.get("surface_mode") != "original_question":
        raise SelectionError("item is not an original question")
    original = surface_item.get("original_question")
    if not isinstance(original, dict):
        raise SelectionError("original question binding is missing")
    card = _safe_relative(DETAILS_ROOT, str(original["details_card_ref"]), "details card")
    asset = _safe_relative(DETAILS_ROOT, str(original["question_asset_ref"]), "question asset")
    if _file_sha(card) != original["details_card_sha256"] or _file_sha(asset) != original["question_asset_sha256"]:
        raise SelectionError("original question evidence drifted")
    answer = canonical_answer_option(card.read_text(encoding="utf-8"))
    if answer is None:
        raise SelectionError("original answer option is missing")
    return {"question_asset": {"vault": "kaoyan-408-details", "relative_path": original["question_asset_ref"], "sha256": original["question_asset_sha256"], "size_bytes": asset.stat().st_size}, "details_card_ref": original["details_card_ref"], "details_card_sha256": original["details_card_sha256"], "details_card_content": card.read_text(encoding="utf-8"), "correct_option": answer}


def enrich_mcp_bundle(repo: Path, queue_ref: str, bundle: dict[str, Any]) -> dict[str, Any]:
    """Add exact original assets only after the MCP returned its bounded evidence."""

    queue = _safe_relative(repo, queue_ref, "semantic queue")
    text = queue.read_text(encoding="utf-8")
    front = legacy_queue._parse_frontmatter(text)
    if front.get("selection_mode") != SELECTION_MODE:
        return bundle
    decision_ref = front.get("sol_selection_decision_ref", "")
    decision_sha = front.get("sol_selection_decision_sha256", "")
    decision = _object(repo, decision_ref, decision_sha)
    candidates = {row["candidate_id"]: row for row in decision["selected_candidates"]}
    headings = list(re.finditer(r"(?m)^#{3,6}\s+(?P<item>MQ-[A-Za-z0-9._-]+).*$", text))
    queue_candidates: dict[str, str] = {}
    for index, heading in enumerate(headings):
        end = headings[index + 1].start() if index + 1 < len(headings) else len(text)
        section = text[heading.start():end]
        match = re.search(r"(?m)^-\s+semantic_candidate_id:\s+`([^`]+)`\s*$", section)
        if match is None:
            raise SelectionError("semantic queue item lacks candidate binding")
        queue_candidates[heading.group("item")] = match.group(1)
    enriched = json.loads(json.dumps(bundle))
    for row in enriched.get("items") or []:
        candidate_id = queue_candidates.get(str(row.get("item_id") or ""))
        candidate = candidates.get(str(candidate_id or ""))
        if candidate is None:
            raise SelectionError("semantic MCP item is not in the Sol decision")
        row["semantic_candidate_id"] = candidate_id
        row["surface_mode"] = candidate["surface_mode"]
        if candidate["surface_mode"] == "original_question":
            row["original_private_evidence"] = original_private_evidence(candidate)
    return enriched


def decision_candidate_for_queue_item(repo: Path, queue: Path, item_id: str) -> dict[str, Any] | None:
    text = queue.read_text(encoding="utf-8")
    front = legacy_queue._parse_frontmatter(text)
    if front.get("selection_mode") != SELECTION_MODE:
        return None
    decision = _object(
        repo,
        front.get("sol_selection_decision_ref", ""),
        front.get("sol_selection_decision_sha256", ""),
    )
    match = re.search(
        rf"(?ms)^#{{3,6}}\s+{re.escape(item_id)}\b.*?(?=^#{{3,6}}\s+MQ-|\Z)",
        text,
    )
    if match is None:
        raise SelectionError("semantic queue item is missing")
    candidate_match = re.search(
        r"(?m)^-\s+semantic_candidate_id:\s+`([^`]+)`\s*$", match.group(0)
    )
    if candidate_match is None:
        raise SelectionError("semantic queue item lacks candidate ID")
    candidate_id = candidate_match.group(1)
    return next(
        (row for row in decision["selected_candidates"] if row["candidate_id"] == candidate_id),
        None,
    )


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo", type=Path, default=Path.cwd())
    sub = parser.add_subparsers(dest="command", required=True)
    freeze_parser = sub.add_parser("freeze")
    freeze_parser.add_argument("--date", required=True)
    freeze_parser.add_argument("--max-candidates", type=int, default=MAX_CANDIDATES)
    luna = sub.add_parser("validate-luna")
    luna.add_argument("--snapshot-ref", required=True)
    luna.add_argument("--snapshot-sha256", required=True)
    luna.add_argument("--candidate-set", type=Path, required=True)
    luna.add_argument("--runtime-receipt", type=Path, required=True)
    shards = sub.add_parser("validate-luna-shards")
    shards.add_argument("--snapshot-ref", required=True)
    shards.add_argument("--snapshot-sha256", required=True)
    shards.add_argument("--bundle", type=Path, required=True)
    materialize = sub.add_parser("materialize-luna")
    materialize.add_argument("--snapshot-ref", required=True)
    materialize.add_argument("--snapshot-sha256", required=True)
    materialize.add_argument("--advisory", type=Path, required=True)
    materialize.add_argument("--runtime-receipt", type=Path, required=True)
    sol = sub.add_parser("seal-sol")
    sol.add_argument("--snapshot-ref", required=True)
    sol.add_argument("--snapshot-sha256", required=True)
    sol.add_argument("--luna-ref", required=True)
    sol.add_argument("--luna-sha256", required=True)
    sol.add_argument("--decision", type=Path, required=True)
    queue = sub.add_parser("publish-queue")
    queue.add_argument("--decision-ref", required=True)
    queue.add_argument("--decision-sha256", required=True)
    freshness = sub.add_parser("validate-queue")
    freshness.add_argument("--queue", type=Path, required=True)
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    repo = args.repo.expanduser().resolve(strict=True)
    try:
        if args.command == "freeze":
            result = freeze(repo, _date(args.date), max_candidates=args.max_candidates)
        elif args.command == "validate-luna":
            result = validate_luna(repo, args.snapshot_ref, args.snapshot_sha256, args.candidate_set, args.runtime_receipt)
        elif args.command == "validate-luna-shards":
            bundle = args.bundle if args.bundle.is_absolute() else repo / args.bundle
            result = validate_luna_shards(
                repo,
                args.snapshot_ref,
                args.snapshot_sha256,
                bundle,
            )
        elif args.command == "materialize-luna":
            result = materialize_luna(repo, args.snapshot_ref, args.snapshot_sha256, args.advisory, args.runtime_receipt)
        elif args.command == "seal-sol":
            result = seal_sol(repo, args.snapshot_ref, args.snapshot_sha256, args.luna_ref, args.luna_sha256, args.decision)
        elif args.command == "publish-queue":
            result = publish_queue(repo, args.decision_ref, args.decision_sha256)
        else:
            queue = args.queue if args.queue.is_absolute() else repo / args.queue
            result = validate_semantic_queue_freshness(repo, queue)
    except (OSError, ValueError, KeyError, SelectionError, review_loop.ReviewLoopError) as exc:
        print(json.dumps({"status": "blocked", "error_code": str(exc), "formal_write_count": 0}, ensure_ascii=False, sort_keys=True))
        return 2
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
