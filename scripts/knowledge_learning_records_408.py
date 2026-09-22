"""Deterministic, answer-safe knowledge-point projections of existing evidence.

This module performs no I/O and no interpretation of learner mastery.  Cold records
retain every source/point pair; only query-facing representative text is bounded.
Question association is deliberately weaker than an observed point-level boundary.
"""
from __future__ import annotations

from collections import Counter, defaultdict
from datetime import date, datetime, timezone
import hashlib
import json
import re
from typing import Any, Mapping

from intake_lib_408 import scan_leak

SCHEMA = "knowledge_learning_memory_408_v1"
POINT_RE = re.compile(r"(?<![A-Z0-9])(?:DS|CO|OS|CN)\d{2}-\d{2}(?![0-9-])", re.I)
PLACEHOLDERS = {"", "无", "未记录", "未观察到", "未标明", "待补充", "暂无明确错因", "unknown", "none", "null", "n/a", "-"}
DIRECT_ROLES = {"direct_observation", "observed_understanding", "user_report", "visible_evidence", "direct", "repair", "hint", "learner_report", "observed_learner_evidence"}
CORRECTION_KINDS = {"date_correction", "grader_outcome_correction_v1"}
EDITORIAL_ROLES = {"historical_source_summary", "assistant_explanation", "review_summary"}
RESULTS = {"independent_correct", "partial", "wrong", "uncertain", "screened_correct", "relearn_required", "correct"}


def _hash(value: Any) -> str:
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def _values(value: Any) -> list[str]:
    if isinstance(value, str):
        return [value]
    if isinstance(value, (tuple, list, set)):
        return [str(item) for item in value if isinstance(item, str)]
    return []


def canonical_knowledge_points(value: Any) -> list[str]:
    """Extract only canonical leaf codes; labels and placeholders are not IDs."""
    return sorted({match.upper() for text in _values(value) for match in POINT_RE.findall(text)})


def _safe_text(value: Any) -> str:
    if not isinstance(value, str) or value.strip().lower() in PLACEHOLDERS:
        return ""
    text = value.strip()
    fails, warns = scan_leak(text)
    return "" if fails or warns else text


def _date(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    try:
        return date.fromisoformat(value).isoformat()
    except ValueError:
        return None


def _time(value: Any) -> datetime | None:
    if not isinstance(value, str) or "T" not in value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        return parsed.replace(tzinfo=timezone.utc) if parsed.tzinfo is None else parsed
    except ValueError:
        return None


def _source_order(record: Mapping[str, Any]) -> tuple[int, int]:
    explicit = record.get("source_order")
    if isinstance(explicit, int) and not isinstance(explicit, bool):
        return explicit, 0
    match = re.search(r"(?:^|/)turn:(\d+)(?:/segment:(\d+))?", str(record.get("source_fragment") or ""))
    return (int(match.group(1)), int(match.group(2) or 0)) if match else (-1, -1)


def _parent_source(record: Mapping[str, Any]) -> tuple[str, str]:
    return str(record.get("source_kind") or ""), str(record.get("source_locator") or record.get("source_id") or "")


def _chronology(record: Mapping[str, Any]) -> tuple:
    # The original study date wins over ingestion time.  An absent study date can
    # use event_time; unknown chronology never supersedes a dated observation.
    event_time = _time(record.get("event_time"))
    observed = _date(record.get("effective_observed_date")) or _date(record.get("observed_date"))
    day = observed or (event_time.date().isoformat() if event_time else "")
    seconds = event_time.timestamp() if event_time else float("-inf")
    return day, seconds, _parent_source(record), _source_order(record), str(record["record_key"])


def _dependency(event: Mapping[str, Any]) -> str:
    if event.get("hint_used") is True:
        return "hint_or_prompted"
    explicit = str(event.get("prompt_dependency") or "").lower()
    if explicit in {"hint_or_prompted", "prompted", "hint_assisted", "hinted", "assisted"}:
        return "hint_or_prompted"
    level = event.get("prompt_level")
    if isinstance(level, str) and level not in {"", "none", "unknown"}:
        return "hint_or_prompted"
    if explicit in {"independent", "none", "unprompted"} or level == "none":
        return "independent"
    return "unknown"


def _summary(record: Mapping[str, Any]) -> dict[str, Any]:
    fields = ("record_key", "knowledge_point", "source_kind", "source_id", "source_sha256", "source_fragment", "source_order", "source_locator", "chronology_precision", "observed_date", "event_time", "evidence_role", "point_binding", "event_kind", "outcome", "choice_result", "reasoning_result", "correction_refs", "effective_observed_date", "effective_outcome", "correction_result", "prompt_dependency", "conclusion_correct", "evidence_refs", "formal_ids", "source_available", "source_access", "review_marker")
    out = {field: record.get(field) for field in fields}
    if record.get("effective_observed_date"):
        out["stored_observed_date"] = record.get("observed_date")
        out["observed_date"] = record["effective_observed_date"]
    if "effective_outcome" in record:
        out["stored_outcome"] = record.get("outcome")
        out["outcome"] = record["effective_outcome"]
        out["choice_result"] = record.get("effective_choice_result")
        out["reasoning_result"] = record.get("effective_reasoning_result")
    text = str(record.get("effective_summary", record.get("summary")) or "")
    out["summary"] = text[:399] + "…" if len(text) > 400 else text
    out["summary_truncated"] = len(text) > 400
    out["establishes_independent_mastery"] = False
    return out


def _actual_observation(record: Mapping[str, Any]) -> bool:
    return (record.get("evidence_role") in DIRECT_ROLES
            and record.get("evidence_role") != "hint"
            and record.get("event_kind") not in {"hint", "prompt", "explanation"})


def _is_repair(record: Mapping[str, Any]) -> bool:
    return (_actual_observation(record)
            and (record.get("evidence_role") == "repair" or record.get("event_kind") == "repair"))


def _representative_summaries(records: list[dict[str, Any]], *, editorial: bool = False, undated: bool = False) -> list[dict[str, Any]]:
    result = []
    seen = set()
    for record in sorted(records, key=_chronology, reverse=True):
        compact = _summary(record)
        if not compact["summary"] or compact["summary"] in seen:
            continue
        seen.add(compact["summary"])
        if editorial:
            compact["evidence_status"] = "editorial_reference"
            compact["is_learner_observation"] = False
        if undated:
            compact["chronology_precision"] = "unknown"
            compact["temporal_status"] = "undated_no_latest_claim"
        result.append(compact)
        if len(result) == 3:
            break
    return result


def _latest_candidates(records: list[dict[str, Any]]) -> list[dict[str, Any]]:
    known = [item for item in records if _chronology(item)[0]]
    if not known:
        return []
    day = max(_chronology(item)[0] for item in known)
    same_day = [item for item in known if _chronology(item)[0] == day]
    candidates = []
    for item in same_day:
        def later(other):
            a, b = _time(item.get("event_time")), _time(other.get("event_time"))
            if a and b and a != b:
                return b > a
            return (_parent_source(item) == _parent_source(other)
                    and _source_order(other) > _source_order(item)
                    and _source_order(item)[0] >= 0)
        if not any(later(other) for other in same_day):
            candidates.append(item)
    return sorted(candidates, key=_chronology)


def _latest(records: list[dict[str, Any]]) -> dict[str, Any] | None:
    candidates = _latest_candidates(records)
    if not candidates:
        return None
    out = _summary(candidates[-1])
    out["latest_is_ambiguous"] = len(candidates) > 1
    out["latest_date_candidate_keys"] = [r["record_key"] for r in candidates]
    if len(candidates) > 1:
        out["chronology_precision"] = "date_only"
    return out


def _review_corrections(wrappers: Mapping[str, Mapping[str, Any]]) -> dict[str, dict[str, Any]]:
    """Verify declared canonical references; project corrections onto old facts.

    The ledger's canonical event digest is the same compact JSON as _hash; it
    differs from the wrapper's raw-line digest (which includes the newline).
    """
    overlays: dict[str, dict[str, Any]] = {}
    for correction_id, wrapper in sorted(wrappers.items()):
        event = wrapper.get("event") or {}
        kind = event.get("event_kind")
        if kind not in CORRECTION_KINDS:
            continue
        target_id = str(event.get("source_event_id") if kind == "date_correction" else event.get("source_outcome_event_id"))
        target_wrapper = wrappers.get(target_id)
        target = (target_wrapper or {}).get("event") or {}
        if not target_wrapper or target.get("event_kind") not in {"outcome", "repair"}:
            raise ValueError("knowledge correction target missing: " + correction_id)
        ref = {"source_kind": "review_event", "source_id": correction_id,
               "source_sha256": wrapper.get("event_sha256") or _hash(event),
               "event_kind": kind, "target_event_id": target_id,
               "target_event_sha256": target_wrapper.get("event_sha256") or _hash(target)}
        updates = {}
        if kind == "date_correction":
            effective = _date(event.get("effective_observed_date"))
            if not effective or event.get("stored_observed_date") != target.get("observed_date"):
                raise ValueError("knowledge date correction proof drifted: " + correction_id)
            updates["effective_observed_date"] = effective
        else:
            ensure_id = str(event.get("corrected_ensure_event_id") or "")
            ensure_wrapper = wrappers.get(ensure_id)
            ensure = (ensure_wrapper or {}).get("event") or {}
            if (target.get("event_kind") != "outcome"
                    or _hash(target) != event.get("source_outcome_event_sha256")
                    or ensure.get("event_kind") != "review_schedule_ensure_v1"
                    or _hash(ensure) != event.get("corrected_ensure_event_sha256")
                    or ensure.get("source_review_event_id") != target_id
                    or ensure.get("first_result") not in RESULTS):
                raise ValueError("knowledge grader correction canonical proof drifted: " + correction_id)
            ref.update({"corrected_ensure_event_id": ensure_id,
                        "corrected_ensure_event_sha256": ensure_wrapper.get("event_sha256") or _hash(ensure),
                        "target_canonical_sha256": event["source_outcome_event_sha256"],
                        "corrected_ensure_canonical_sha256": event["corrected_ensure_event_sha256"]})
            updates.update({"effective_outcome": ensure["first_result"],
                            "effective_choice_result": _safe_text(ensure.get("choice_result")) or None,
                            "effective_reasoning_result": _safe_text(ensure.get("reasoning_result")) or None,
                            "effective_summary": _safe_text(ensure.get("first_break"))
                                if ensure.get("first_break_provenance") in {"visible_evidence", "user_report"} else ""})
        overlay = overlays.setdefault(target_id, {"correction_refs": []})
        if any(key in overlay and overlay[key] != value for key, value in updates.items()):
            raise ValueError("conflicting knowledge corrections: " + target_id)
        overlay.update(updates)
        overlay["correction_refs"].append(ref)
    return overlays


def effective_review_records(records) -> list[dict[str, Any]]:
    """Pure corrected view for legacy aggregators; raw evidence stays available.

    Wrapper event_sha256 always identifies the original ledger bytes, while
    raw_event and correction_refs explain why event is an effective projection.
    Feed the original wrappers to build_knowledge_memory and evidence hashing.
    """
    records = list(records)
    wrappers = {str(w.get("event_id") or (w.get("event") or {}).get("event_id") or ""): w for w in records}
    overlays = _review_corrections(wrappers)
    projected = []
    for wrapper in records:
        result = dict(wrapper)
        event_id = str(wrapper.get("event_id") or (wrapper.get("event") or {}).get("event_id") or "")
        overlay = overlays.get(event_id)
        if overlay:
            raw = wrapper["event"]
            event = dict(raw)
            result["raw_event"] = dict(raw)
            result["correction_refs"] = [dict(ref) for ref in overlay["correction_refs"]]
            if "effective_observed_date" in overlay:
                event["stored_observed_date"] = raw.get("observed_date")
                event["observed_date"] = overlay["effective_observed_date"]
            if "effective_outcome" in overlay:
                for name in ("first_result", "choice_result", "reasoning_result", "first_break"):
                    event["stored_" + name] = raw.get(name)
                event["first_result"] = overlay["effective_outcome"]
                event["choice_result"] = overlay.get("effective_choice_result")
                event["reasoning_result"] = overlay.get("effective_reasoning_result")
                event["first_break"] = overlay.get("effective_summary", "")
                event["first_break_provenance"] = "visible_evidence" if event["first_break"] else "not_observed"
            event["knowledge_projection_corrected"] = True
            result["event"] = event
        projected.append(result)
    return projected


def build_knowledge_memory(profiles, master, cards, review_records, direct, associated, supplemental_records=()) -> dict[str, Any]:
    """Build deterministic cold records and compact point summaries.

    ``direct`` and ``associated`` bind review event IDs to formal question IDs,
    not to knowledge-level weakness. Supplemental inputs accept knowledge_points,
    source_kind/id/sha256, source_fragment/source_order, observed_date/event_time, summary, evidence_role,
    formal_ids, point_binding, event_kind, outcome/correction_result and prompting
    fields. Extra payload (including raw utterances, stems and answers) is ignored.
    """
    records: dict[str, dict[str, Any]] = {}
    aliases: dict[str, set[str]] = defaultdict(set)
    formal_points: dict[str, list[str]] = {}
    point_formals: dict[str, set[str]] = defaultdict(set)
    skipped = 0
    review_records = list(review_records)
    supplemental_records = list(supplemental_records)
    review_wrappers = {str(w.get("event_id") or (w.get("event") or {}).get("event_id") or ""): w for w in review_records}
    correction_overlays = _review_corrections(review_wrappers)
    reviewed_source_points: dict[str, set[str]] = defaultdict(set)
    source_binding_refs: dict[str, list[dict[str, Any]]] = defaultdict(list)
    source_binding_formals: dict[str, set[str]] = defaultdict(set)
    unbound_learning_ids = set()
    unbound_admin_ids = set()
    for annotation in supplemental_records:
        if annotation.get("source_kind") != "semantic_review_identity" or not annotation.get("review_source_id"):
            continue
        anchor = review_wrappers.get(str(annotation.get("anchor_event_id") or ""))
        source_identity = str(annotation["review_source_id"])
        if (not anchor or (anchor.get("event") or {}).get("source_id") != source_identity
                or anchor.get("event_sha256") != annotation.get("source_sha256")):
            raise ValueError("review identity anchor proof drifted: " + source_identity)
        reviewed_source_points[source_identity].update(canonical_knowledge_points(annotation.get("knowledge_points")))
        source_binding_refs[source_identity].append({"source_kind": "semantic_review_identity",
            "source_id": str(annotation.get("source_id") or ""), "source_sha256": annotation["source_sha256"],
            "anchor_event_id": annotation["anchor_event_id"], "review_source_id": source_identity})

    def add_labels(value):
        codes = canonical_knowledge_points(value)
        for label in _values(value):
            label = _safe_text(label)
            if label:
                aliases[label].update(canonical_knowledge_points(label))
        for code in codes:
            aliases[code].add(code)
        return codes

    for formal_id in sorted(set(master) | set(profiles)):
        profile = profiles.get(formal_id, {})
        row = master.get(formal_id, {})
        knowledge = profile.get("knowledge") or {}
        codes = set()
        for key, fallback in (("main", "主知识点"), ("sub", "副知识点"), ("hit", "命中知识点")):
            codes.update(add_labels(knowledge.get(key, []) if isinstance(knowledge, Mapping) else []))
            codes.update(add_labels(row.get(fallback, "")))
        formal_points[formal_id] = sorted(codes)
        for code in codes:
            point_formals[code].add(formal_id)

    for wrapper in review_records:
        event = wrapper.get("event") or {}
        formal_id = event.get("formal_node_id")
        source_identity = event.get("source_id")
        if (event.get("event_kind") != "formal_review_binding_v1"
                or event.get("formal_result_status") not in {"curated", "already_current"}
                or formal_id not in formal_points or not source_identity
                or not re.fullmatch(r"[0-9a-f]{64}", str(event.get("formal_result_receipt_sha256") or ""))):
            continue
        source_binding_formals[str(source_identity)].add(formal_id)
        source_binding_refs[str(source_identity)].append({"source_kind": "review_event",
            "source_id": str(wrapper.get("event_id") or event.get("event_id") or ""),
            "source_sha256": wrapper.get("event_sha256") or _hash(event),
            "event_kind": "formal_review_binding_v1", "formal_node_id": formal_id,
            "formal_result_receipt_sha256": event["formal_result_receipt_sha256"]})

    def add(source, codes, formal_ids, binding, role, correction=None, binding_refs=None):
        nonlocal skipped
        if not codes:
            skipped += 1
            return
        kind = _safe_text(source.get("source_kind")) or "unknown_source"
        source_id = str(source.get("source_id") or "").strip()
        if any(scan_leak(source_id)):
            source_id = ""
        # Do not derive missing identities from payload containing protected text.
        if not source_id:
            skipped += 1
            return
        sha = str(source.get("source_sha256") or "")
        fragment = _safe_text(source.get("source_fragment"))
        source_order = source.get("source_order")
        source_order = source_order if isinstance(source_order, int) and not isinstance(source_order, bool) else None
        precision = ("event_time" if _time(source.get("event_time")) else
                     "source_order" if _source_order(source)[0] >= 0 else
                     "date_only" if _date(source.get("observed_date")) else "unknown")
        for point in codes:
            key = "KLR-" + _hash([kind, source_id, fragment, point])
            refs = [{"source_kind": kind, "source_id": source_id, "source_sha256": sha,
                     "source_fragment": fragment}]
            item = {
                "record_key": key, "knowledge_point": point,
                "source_kind": kind, "source_id": source_id, "source_sha256": sha,
                "source_fragment": fragment, "source_order": source_order,
                "source_locator": _safe_text(source.get("source_locator")), "chronology_precision": precision,
                "observed_date": source.get("observed_date") if isinstance(source.get("observed_date"), str) else None,
                "event_time": source.get("event_time") if isinstance(source.get("event_time"), str) else None,
                "formal_ids": sorted(set(formal_ids)), "point_binding": binding,
                "evidence_role": role, "event_kind": _safe_text(source.get("event_kind")) or kind,
                "summary": _safe_text(source.get("summary")),
                "outcome": source.get("outcome") if source.get("outcome") in RESULTS else None,
                "correction_result": source.get("correction_result") if source.get("correction_result") in RESULTS else None,
                "prompt_dependency": _dependency(source),
                "conclusion_correct": source.get("conclusion_correct") if isinstance(source.get("conclusion_correct"), bool) else None,
                "evidence_refs": refs, "establishes_independent_mastery": False,
            }
            for field in ("review_marker", "review_session_id", "review_had_error", "source_available", "source_access"):
                if field in source:
                    item[field] = source[field]
            for field in ("effective_observed_date", "effective_outcome", "effective_choice_result", "effective_reasoning_result", "effective_summary", "correction_refs"):
                if correction and field in correction:
                    item[field] = correction[field]
            item["choice_result"] = _safe_text(source.get("choice_result")) or None
            item["reasoning_result"] = _safe_text(source.get("reasoning_result")) or None
            if correction and correction.get("correction_refs"):
                item["evidence_refs"].extend(correction["correction_refs"])
            if binding_refs:
                item["source_binding_refs"] = binding_refs
                item["evidence_refs"].extend(binding_refs)
            if role == "formal_summary":
                item["text_origin"] = "formal_summary"
            else:
                item["text_origin"] = "source_summary"
            if key in records:
                previous = records[key]
                # Repeated associations may merge, but conflicting evidence with
                # the same source identity must never silently overwrite history.
                common = {k: v for k, v in item.items() if k != "formal_ids"}
                old = {k: v for k, v in previous.items() if k != "formal_ids"}
                if common != old:
                    raise ValueError("conflicting knowledge learning source: " + source_id)
                previous["formal_ids"] = sorted(set(previous["formal_ids"]) | set(formal_ids))
            else:
                records[key] = item
            aliases[point].add(point)
            point_formals[point].update(formal_ids)

    for formal_id, codes in sorted(formal_points.items()):
        profile = profiles.get(formal_id, {})
        row = master.get(formal_id, {})
        boundary = _safe_text(profile.get("user_error_entry"))
        action = _safe_text(profile.get("redo_first_action"))
        text = "；".join(part for part in (boundary, action) if part)
        card_sha = (cards.get(formal_id) or {}).get("sha256")
        add({"source_kind": "formal_summary", "source_id": formal_id,
             "source_sha256": card_sha or _hash(profile or row),
             "observed_date": row.get("最近复做日期") or row.get("首次做题日期"),
             "summary": text}, codes, [formal_id], "question_associated", "formal_summary")

    review_ids = {str(w.get("event_id") or "") for w in review_records}
    for wrapper in review_records:
        event = wrapper.get("event") or {}
        event_id = str(wrapper.get("event_id") or event.get("event_id") or "")
        if event.get("event_kind") in CORRECTION_KINDS:
            continue  # represented by verified references on the corrected fact
        ids = sorted(set(direct.get(event_id, [])) | set(associated.get(event_id, []))
                     | source_binding_formals.get(str(event.get("source_id") or ""), set()))
        explicit = set()
        for field in ("knowledge_points", "knowledge_point", "knowledge_point_id", "point_id", "point_ids"):
            explicit.update(add_labels(event.get(field)))
        inferred = {code for formal_id in ids for code in formal_points.get(formal_id, [])}
        all_codes = explicit | inferred | reviewed_source_points.get(str(event.get("source_id") or ""), set())
        event_kind = str(event.get("event_kind") or "")
        if event_kind == "repair":
            role = "repair"
        elif event_kind in {"hint", "prompt", "explanation"}:
            role = "hint"
        elif event_kind == "outcome" or (event_kind == "review_schedule_ensure_v1"
                and event.get("source_review_event_id") not in review_ids):
            role = "direct_observation"
        else:
            role = "review_event"
        # Only semantic summaries with a supported provenance are read. Numeric
        # scores, answer strings and frozen feedback are never understanding text.
        text = _safe_text(event.get("understanding_summary"))
        if not text and event_kind == "repair":
            text = _safe_text(event.get("correction_summary"))
        if not text and event.get("first_break_provenance") in {"visible_evidence", "user_report"}:
            text = _safe_text(event.get("first_break"))
        source = {
            "source_kind": "review_event", "source_id": event_id,
            "source_sha256": wrapper.get("event_sha256") or _hash(event),
            "observed_date": event.get("observed_date"), "event_time": event.get("event_time"),
            "event_kind": event_kind, "summary": text, "outcome": event.get("first_result"),
            "choice_result": event.get("choice_result"), "reasoning_result": event.get("reasoning_result"),
            "correction_result": event.get("correction_result"),
            "prompt_dependency": event.get("prompt_dependency"), "prompt_level": event.get("prompt_level"),
            "hint_used": event.get("hint_used"),
            "conclusion_correct": True if event.get("correction_conclusion") == "correct" else False if event.get("correction_conclusion") == "wrong" else None,
        }
        if not all_codes:
            skipped += 1
            (unbound_learning_ids if event_kind in {"outcome", "repair"} else unbound_admin_ids).add(event_id)
        for point in sorted(all_codes):
            add(source, [point], ids, "explicit_point" if point in explicit else "question_associated", role, correction=correction_overlays.get(event_id), binding_refs=source_binding_refs.get(str(event.get("source_id") or "")))

    for source in supplemental_records:
        codes = add_labels(source.get("knowledge_points", []))
        role = _safe_text(source.get("evidence_role")) or "source_summary"
        binding = source.get("point_binding")
        if binding not in {"explicit_point", "question_associated"}:
            binding = "explicit_point" if role in DIRECT_ROLES else "question_associated"
        add(source, codes, _values(source.get("formal_ids")), binding, role)

    grouped: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for record in records.values():
        grouped[record["knowledge_point"]].append(record)
    points = {}
    for point in sorted(set(point_formals) | set(grouped)):
        ordered = sorted(grouped[point], key=_chronology)
        known = [record for record in ordered if _chronology(record)[0]]
        unknown = [record for record in ordered if not _chronology(record)[0]]
        understanding = [record for record in known if record["point_binding"] == "explicit_point" and _actual_observation(record)]
        latest = _latest(understanding)
        if latest and not latest["summary"]:
            semantic_status = ("repair_recorded_understanding_text_unobserved"
                               if _is_repair(latest) else "understanding_text_unobserved")
        else:
            semantic_status = "observed_summary_available" if latest else "no_dated_point_observation"
        if latest and latest["latest_is_ambiguous"]:
            semantic_status = "latest_date_order_ambiguous"
        historical = []
        seen = {latest["summary"]} if latest else set()
        latest_candidate_keys = set(latest["latest_date_candidate_keys"]) if latest else set()
        for record in reversed(understanding):
            if record["record_key"] in latest_candidate_keys:
                continue
            compact = _summary(record)
            if compact["summary"] and compact["summary"] not in seen:
                seen.add(compact["summary"])
                historical.append(compact)
            if len(historical) == 3:
                break
        point_records = grouped[point]
        outcomes = [r for r in point_records if _actual_observation(r) and not _is_repair(r)
                    and r["point_binding"] == "explicit_point" and r["outcome"]]
        repairs = [r for r in point_records if _is_repair(r) and r["point_binding"] == "explicit_point"]
        undated = [r for r in unknown if _actual_observation(r) and r["summary"]]
        instruction = [r for r in point_records if r["evidence_role"] in EDITORIAL_ROLES and r["summary"]]
        hints = [r for r in point_records if r["evidence_role"] == "hint" or r["prompt_dependency"] == "hint_or_prompted"]
        formal = [r for r in point_records if r["evidence_role"] == "formal_summary"]
        points[point] = {
            "knowledge_point": point, "formal_ids": sorted(point_formals[point]),
            "record_keys": [r["record_key"] for r in known],
            "unknown_date_record_keys": [r["record_key"] for r in unknown],
            "record_count": len(point_records), "count_semantics": "evidence_records_not_attempts",
            "concept_review_session_count": len({r["review_session_id"] for r in point_records if r.get("review_session_id")}),
            "concept_review_error_session_count": len({r["review_session_id"] for r in point_records if r.get("review_session_id") and r.get("review_had_error")}),
            "concept_review_count_semantics": "new_sealed_sessions_only_not_legacy_or_question_counts",
            "latest_question_associated": _latest([r for r in point_records if r["point_binding"] == "question_associated" and _actual_observation(r)]),
            "latest_understanding": latest,
            "latest_date_candidates": sorted(latest_candidate_keys),
            "latest_chronology_ambiguous": bool(latest and latest["latest_is_ambiguous"]),
            "historical_understanding": historical,
            "undated_understanding": _representative_summaries(undated, undated=True),
            "undated_understanding_record_count": len(undated),
            "prior_instruction": _representative_summaries(instruction, editorial=True),
            "prior_instruction_record_count": len(instruction),
            "latest_outcome": _latest(outcomes),
            "latest_repair": _latest(repairs), "latest_hint": _latest(hints),
            "outcome_count": len(outcomes), "repair_count": len(repairs), "hint_count": len(hints),
            "question_associated_count": sum(r["point_binding"] == "question_associated" for r in point_records),
            "formal_summaries": [_summary(r) for r in sorted(formal, key=_chronology, reverse=True)[:3]],
            "evidence_role_counts": dict(sorted(Counter(r["evidence_role"] for r in point_records).items())),
            "understanding_status": semantic_status, "latest_semantic_status": semantic_status,
            "establishes_independent_mastery": False,
        }
    return {
        "schema": SCHEMA, "records": dict(sorted(records.items())), "points": points,
        "aliases": {alias: sorted(codes) for alias, codes in sorted(aliases.items()) if codes},
        "formal_points": formal_points,
        "coverage": {"formal_count": len(formal_points), "formal_with_points_count": sum(bool(p) for p in formal_points.values()),
                     "point_count": len(points), "record_count": len(records),
                     "source_count": len({(r["source_kind"], r["source_id"]) for r in records.values()}),
                     "unknown_date_record_count": sum(not _chronology(r)[0] for r in records.values()),
                     "skipped_unbound_source_count": skipped,
                     "unbound_learning_event_count": len(unbound_learning_ids),
                     "unbound_learning_event_ids": sorted(unbound_learning_ids),
                     "unbound_admin_event_count": len(unbound_admin_ids),
                     "unbound_admin_event_ids": sorted(unbound_admin_ids),
                     "review_identity_mapping_count": len(reviewed_source_points),
                     "formal_source_binding_count": len(source_binding_formals),
                     "applied_correction_count": sum(len(v["correction_refs"]) for v in correction_overlays.values())},
    }
