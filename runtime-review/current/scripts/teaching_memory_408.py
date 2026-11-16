"""Bounded teacher projection of a selected, already verified 408 profile."""
from __future__ import annotations

import datetime as dt
import json
from zoneinfo import ZoneInfo

from knowledge_profile_408 import CLAIMS_SCHEMA, teaching_focus_hot

SCOPE = "teaching"
DELIVERY_POLICY = "bounded-evidence-priority-v2"
_SHANGHAI = ZoneInfo("Asia/Shanghai")
_EPOCH = dt.datetime.min.replace(tzinfo=dt.timezone.utc)
_CLAIM_FIELDS = ("claim_id", "kind", "basis", "evidence_status", "content", "event_at",
                 "recorded_at", "valid_from", "valid_to", "supersedes")
_RECORD_FIELDS = ("schema", "knowledge_point", "subpoint_id", "syllabus_id", "title", "concept_name",
                  "status", "reason", "view", "content_fields_redacted", "has_personal_history",
                  "parent_profile_version", "brief_status", "concept_brief", "syllabus_context",
                  "obsidian_uri", "markdown_link", "personal_context")
_OPTIONAL_SURFACES = ("local_syllabus_lessons", "cached_visuals", "automatic_visual_teaching", "visual_dispatch")
_POINT_DETAILS = ("concept_brief", "markdown_link", "obsidian_uri")
_SYLLABUS_DETAILS = ("exam_frequency", "paths", "frequency_intro", "context_object", "frequency_intro_ref")
_DETAIL_ORDER = ("concept_brief", "exam_frequency", "markdown_link", "obsidian_uri",
                 "paths", "frequency_intro", "context_object", "frequency_intro_ref")


def _event_time(row):
    if not row.get("event_at"):
        return _EPOCH
    stamp = dt.datetime.fromisoformat(row["event_at"])
    return stamp if stamp.tzinfo is not None else stamp.replace(tzinfo=_SHANGHAI)


def _repair_claim(row):
    return bool(row.get("supersedes")) or row["evidence_status"] == "prompted_repair"


def selected_context(profile, identity, version):
    """Keep authored statements intact; provenance is the checked profile object."""
    structured = profile.get("claims_schema") == CLAIMS_SCHEMA
    claims = profile.get("claims", []) if structured else []
    active = [row for row in claims if row["lifecycle"] == "current"]
    withheld = {row["claim_id"] for row in active if _repair_claim(row) and
                not (row.get("source_refs") and all(ref.get("review_state") == "reviewed"
                                                   for ref in row["source_refs"]))}
    current = sorted((row for row in active if row["claim_id"] not in withheld),
                     key=lambda row: (_repair_claim(row), _event_time(row)), reverse=True)
    conflicts = sum(row["lifecycle"] == "conflicted" for row in claims)
    historical = len(claims) - len(active) - conflicts
    value = {"identity": identity, "status": "current", "complete": False, "profile_version": version,
             "teacher_evidence_only": True,
             "claim_view": {"status": "available" if structured else "legacy_unstructured",
                            "claims": [{key: row[key] for key in _CLAIM_FIELDS if row.get(key) is not None}
                                       for row in current],
                            "historical_claim_count": historical,
                            "conflicted_claim_count": conflicts}}
    if withheld:
        value["claim_view"]["unreviewed_repair_count"] = len(withheld)
    if (not structured or current or not claims) and profile.get("next_check"):
        value["next_check"] = profile["next_check"]
    # Inactive judgments stay in the complete read. A free-text synthesis that
    # still quotes them must not bring them back as current teaching advice.
    if not (conflicts or historical or withheld) and profile.get("short_summary"):
        value["short_summary"] = profile["short_summary"].split("\n细查小点：", 1)[0]
    if not structured and profile.get("teaching_focus"):
        value.update(teaching_focus_hot(profile["teaching_focus"]))
    return value


def selected_record(record):
    value = {key: record[key] for key in _RECORD_FIELDS if key in record}
    if scope := value.get("syllabus_context"):
        value["syllabus_context"] = {key: scope[key] for key in (
            "syllabus_year", "syllabus_ids", "paths", "scope_status", "teaching_boundary", "exam_frequency",
            "frequency_intro", "frequency_intro_ref", "context_object") if key in scope}
    return value


def _size(value):
    # Same compact UTF-8 representation as the native resolver.
    return len(json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode("utf-8")) + 1


def fit(result, limit):
    """Spend the shared budget on whole statements, before optional surfaces."""
    result.update(budgets={"max_bytes": limit}, serialized_bytes=0)

    def fits():
        while (size := _size(result)) != result["serialized_bytes"]:
            result["serialized_bytes"] = size
        return size <= limit

    candidates = {}
    for record in result.get("records", []):
        memory = record.get("personal_context", {})
        view = memory.get("claim_view")
        if view is None and "repair_evidence" not in memory:
            continue
        atoms = {key: memory.pop(key) for key in (
            "short_summary", "teaching_focus", "next_check", "repair_evidence") if key in memory}
        memory["omitted_fields"] = [key for key in atoms if key != "claims"]
        atoms["claims"] = []
        if view is not None:
            atoms["claims"], view["claims"] = view["claims"], []
            view["omitted_claim_count"] = len(atoms["claims"])
            repair_count = sum(_repair_claim(row) for row in atoms["claims"])
            if repair_count:
                view["omitted_repair_claim_count"] = repair_count
        candidates[memory["identity"]] = atoms

    surfaces = {key: result.pop(key) for key in _OPTIONAL_SURFACES if key in result}
    result["omitted_materials"] = list(surfaces)
    read_stats = (result.get("read_stats") or {}).pop("artifacts", None)
    if read_stats:
        result["read_stats"]["artifact_paths_omitted"] = True
    result.setdefault("truncated", False)
    result.setdefault("omitted_points", [])
    result.setdefault("omitted_subpoints", [])
    receipt = result.get("personal_read_receipt", {})
    records = result.get("records", [])

    # Reserve space for evidence across points. Keep the syllabus boundary
    # and verified profile receipt alongside each retained point.
    point_details = []
    for record in records:
        details = []
        for target, keys in (
            (record, _POINT_DETAILS),
            (record.get("syllabus_context", {}), _SYLLABUS_DETAILS),
        ):
            for key in keys:
                if key in target:
                    details.append((target, key, target.pop(key)))
        if details:
            record["omitted_fields"] = [key for _, key, _ in details]
        point_details.append(details)
    while records and not fits():
        row = records.pop()
        for key, target in (("subpoint_id", "omitted_subpoints"), ("syllabus_id", "omitted_syllabus_entries"),
                            ("knowledge_point", "omitted_points")):
            if identity := row.get(key):
                result.setdefault(target, []).append(identity)
                receipt["records"] = [item for item in receipt.get("records", []) if item["identity"] != identity]
                break
        receipt["complete"] = False
        result["returned"] = len(records)
        result["truncated"] = True
    if not fits():
        intro = result.get("question_exam_introduction")
        result = {key: result[key] for key in ("schema", "view", "source_version", "budgets") if key in result}
        result.update(status="budget_too_small", reason="teaching_context_exceeds_budget", records=[],
                      write_count=0, truncated=True, serialized_bytes=0)
        if intro:
            result["question_exam_introduction"] = intro
            if not fits():
                result["question_exam_introduction"] = {"status": "omitted_for_budget",
                    "question_identity": intro.get("question_identity")}
        fits()
        return result

    memories = [row["personal_context"] for row in records
                if (row.get("personal_context") or {}).get("identity") in candidates]

    def add_field(memory, key, value):
        memory[key] = value
        memory["omitted_fields"].remove(key)
        if not fits():
            memory.pop(key)
            memory["omitted_fields"].append(key)

    def add_claim(memory, claim):
        view = memory["claim_view"]
        view["claims"].append(claim)
        view["omitted_claim_count"] -= 1
        if _repair_claim(claim):
            view["omitted_repair_claim_count"] -= 1
        if not fits():
            view["claims"].pop()
            view["omitted_claim_count"] += 1
            if _repair_claim(claim):
                view["omitted_repair_claim_count"] += 1
            return False
        return True

    def add_surface(key, surface):
        result[key] = surface
        result["omitted_materials"].remove(key)
        if not fits():
            result.pop(key)
            result["omitted_materials"].append(key)

    def repair_missing(memory):
        view = memory.get("claim_view") or {}
        return view.get("omitted_repair_claim_count", 0) or view.get("unreviewed_repair_count", 0)

    # Give each point a first current statement before filling in extra
    # conditions. A repair claim and its conditions always travel together.
    for memory in memories:
        atoms = candidates[memory["identity"]]
        if "repair_evidence" in atoms:
            add_field(memory, "repair_evidence", atoms["repair_evidence"])
        elif atoms["claims"] and (not repair_missing(memory) or _repair_claim(atoms["claims"][0])):
            add_claim(memory, atoms["claims"][0])
    for memory in memories:
        view = memory.get("claim_view") or {}
        if not view.get("claims"):
            continue
        for claim in candidates[memory["identity"]]["claims"][1:]:
            if not _repair_claim(claim) or not add_claim(memory, claim):
                break
    for memory in memories:
        atoms = candidates[memory["identity"]]
        if not atoms["claims"] and "short_summary" in atoms and not repair_missing(memory):
            add_field(memory, "short_summary", atoms["short_summary"])
    for key in ("next_check", "teaching_focus", "short_summary"):
        for memory in memories:
            atoms = candidates[memory["identity"]]
            if (key in atoms and key in memory["omitted_fields"] and not repair_missing(memory)
                    and (not atoms["claims"] or memory["claim_view"]["claims"])):
                add_field(memory, key, atoms[key])
    if (course := surfaces.pop("local_syllabus_lessons", None)) is not None:
        add_surface("local_syllabus_lessons", course)
    # Return definitions and navigation where space permits. Allocate each
    # kind across the selected points before spending on the next kind.
    for detail_key in _DETAIL_ORDER:
        for record, details in zip(records, point_details):
            for target, key, value in details:
                if key != detail_key:
                    continue
                target[key] = value
                record["omitted_fields"].remove(key)
                if not fits():
                    target.pop(key)
                    record["omitted_fields"].append(key)
    for memory in memories:
        atoms = candidates[memory["identity"]]
        projected = (memory.get("claim_view") or {}).get("claims")
        if projected and not repair_missing(memory):
            for claim in atoms["claims"][len(projected):]:
                if not add_claim(memory, claim):
                    break
    for key, surface in surfaces.items():
        add_surface(key, surface)
    for record in records:
        if record.get("omitted_fields") == []:
            record.pop("omitted_fields")
    result["truncated"] = (result["truncated"] or bool(result["omitted_materials"])
        or any(row.get("omitted_fields") for row in records)
        or any(memory["omitted_fields"] or (memory.get("claim_view") or {}).get("omitted_claim_count")
               for memory in memories))
    fits()
    return result
