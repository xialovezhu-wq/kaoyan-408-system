"""Cold artifact layout and bounded hot queries for point-keyed 408 memory."""
from __future__ import annotations

import copy
import hashlib
import json
import re
import unicodedata
from pathlib import Path
from typing import Any, Mapping

from knowledge_learning_records_408 import canonical_knowledge_points
from knowledge_learning_paths_408 import PAGE_DIR, build_path

BUILD_VERSION = "point-memory-408-v5-concept-briefs"
POINT_INDEX_SCHEMA = "knowledge_tutoring_point_index_408_v3"
MAX_POINT_INDEX_BYTES = 8 * 1024 * 1024
MAX_POINT_SUMMARY_BYTES = 128 * 1024


def _bytes(value):
    return (json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n").encode()


def _norm(value):
    return re.sub(r"\s+", " ", unicodedata.normalize("NFKC", str(value))).strip().lower()


def taxonomy_aliases(taxonomy_text: str, present_points, *, with_expansions=False):
    """Resolve exact official leaf/module labels at build time, without fuzzy search.

    A module expands only to its listed leaf codes with materialized history.
    Introductory numbering examples are excluded by the strict heading/list
    syntax used by the canonical knowledge-point table.
    """
    present = set(present_points)
    leaves, modules = {}, {}
    for line in taxonomy_text.splitlines():
        leaf = re.fullmatch(r"\s*-\s+((?:DS|CO|OS|CN)\d{2}-\d{2})\s+(.+?)\s*", line)
        module = re.fullmatch(r"\s*#{1,6}\s+((?:DS|CO|OS|CN)\d{2})\s+(.+?)\s*", line)
        if leaf:
            leaves[leaf.group(1)] = leaf.group(2)
        elif module:
            modules[module.group(1)] = module.group(2)
    aliases = {}
    expansions = set()

    def bind(labels, codes):
        for label in labels:
            aliases.setdefault(_norm(label), set()).update(codes)

    for code in sorted(present & leaves.keys()):
        bind((code, leaves[code], code + " " + leaves[code]), [code])
    for code, label in sorted(modules.items()):
        children = sorted(point for point in present & leaves.keys() if point.startswith(code + "-"))
        if children:
            labels = (code, label, code + " " + label)
            bind(labels, children)
            expansions.update(_norm(value) for value in labels)
    return (aliases, expansions) if with_expansions else aliases


def build_artifacts(memory: Mapping[str, Any], source_coverage=None, *, taxonomy_text="", reviewed_profiles=None, question_references=None, repo_root=None):
    """Split unlimited histories from compact query summaries, before publication."""
    objects = {}
    pages = {}
    labels = {match.group(1): match.group(2) for line in taxonomy_text.splitlines()
              if (match := re.fullmatch(r"\s*-\s+((?:DS|CO|OS|CN)\d{2}-\d{2})\s+(.+?)\s*", line))}
    root = Path(repo_root) if repo_root is not None else None
    concept_briefs = {}
    for subject in ("DS", "CO", "OS", "CN"):
        path = root / "support/concept-briefs" / (subject + ".json") if root else None
        if path and path.is_file():
            content = json.loads(path.read_text(encoding="utf-8"))
            for code, brief in content.items():
                if not isinstance(brief, list) or len(brief) != 2 or not all(isinstance(v, str) and v.strip() for v in brief):
                    raise ValueError("invalid_concept_brief:" + code)
            concept_briefs.update(content)

    def object_ref(directory, value):
        digest = hashlib.sha256(_bytes(value)).hexdigest()
        ref = "objects/" + directory + "/" + digest + ".json"
        objects[ref] = value
        return {"ref": ref, "sha256": digest}

    # Records are partitioned by knowledge point. One shared record table made
    # every point's history reference the same object, so a single new record
    # changed the shared hash and rewrote all 599 histories and summaries.
    # Each record belongs to exactly one point, so per-point slices keep the
    # same bytes while confining a write to the point that actually changed.
    all_records = memory["records"]
    points = {}
    subpoints = {}
    point_records = {}
    point_inputs = dict(memory["points"])
    for code in labels:
        point_inputs.setdefault(code, {"knowledge_point": code, "record_keys": [],
            "unknown_date_record_keys": [], "record_count": 0, "formal_ids": [],
            "understanding_status": "no_personal_history", "latest_semantic_status": "no_dated_point_observation",
            "establishes_independent_mastery": False})
    for code, raw in sorted(point_inputs.items()):
        keys = list(raw["record_keys"]) + list(raw["unknown_date_record_keys"])
        records = object_ref("point-records", {"schema": memory["schema"], "knowledge_point": code,
                                              "records": {key: all_records[key] for key in keys}})
        point_records[code] = records
        history = object_ref("point-histories", {"knowledge_point": code,
            "record_keys": raw["record_keys"], "unknown_date_record_keys": raw["unknown_date_record_keys"],
            "records_object": records})
        summary = {key: value for key, value in raw.items()
                   if key not in {"record_keys", "unknown_date_record_keys"}}
        summary["history_object"] = history
        preferred = [fid for field in ("latest_understanding", "latest_repair", "latest_outcome")
                     for fid in (raw.get(field) or {}).get("formal_ids", [])]
        related = list(dict.fromkeys(preferred + list(raw.get("formal_ids", []))))
        available_refs = [question_references[fid] for fid in related if fid in (question_references or {})]
        summary["question_refs"] = available_refs[:3]
        summary["question_refs_truncated"] = len(available_refs) > 3
        reviewed = (reviewed_profiles or {}).get(code)
        if reviewed:
            reviewed = copy.deepcopy(reviewed)
            for ref in reviewed.get("question_refs", []):
                current_ref = (question_references or {}).get(ref.get("formal_id"))
                if current_ref:
                    ref.update({key: value for key, value in current_ref.items()
                                if key not in {"relationship"}})
            profile_ref = object_ref("reviewed-knowledge-profiles", reviewed)
            current = reviewed.get("evidence_fingerprint") == records["sha256"]
            summary["learning_profile"] = {
                "status": "current" if current else "stale",
                "version": reviewed["version"],
                "profile_object": profile_ref,
                "origin": reviewed.get("origin", "web_final_semantic_content"),
            }
            if current:
                summary["learning_profile"].update(
                    short_summary=reviewed["short_summary"], next_check=reviewed["next_check"],
                    question_refs=reviewed.get("question_refs", [])[:3],
                    question_refs_truncated=len(reviewed.get("question_refs", [])) > 3)
            else:
                summary["learning_profile"]["reason"] = "point_evidence_changed_after_reviewed_profile"
        else:
            summary["learning_profile"] = {"status": "not_authored", "version": None}
        prepared = build_path(code, labels.get(code, code), raw,
            {key: all_records[key] for key in keys}, available_refs, reviewed,
            bool(reviewed and summary["learning_profile"]["status"] == "current"),
            repo_root=root, has_vault=bool(root and (root / ".obsidian").is_dir()),
            concept_brief=concept_briefs.get(code))
        pages[prepared["page_relpath"]] = prepared["markdown"]
        points[code] = {
            **object_ref("point-summaries", summary),
            "teaching_views": {view: object_ref("teaching-summaries", value)
                               for view, value in prepared["views"].items()},
            "learning_path": {"relpath": prepared["page_relpath"],
                "sha256": hashlib.sha256(prepared["markdown"].encode("utf-8")).hexdigest()},
        }
        # Each small, reviewed mechanism is independently addressable. A narrow
        # query never has to open the parent profile or its history object.
        for facet in (reviewed or {}).get("subpoints", []):
            key = facet["subpoint_id"]
            facet_current = summary["learning_profile"]["status"] == "current"
            full_facet = object_ref("knowledge-subpoint-records", {
                "schema": "knowledge_subpoint_record_408_v1", "knowledge_point": code,
                "parent_profile_version": reviewed["version"], **copy.deepcopy(facet)})
            shared = {"schema": "knowledge_teaching_view_408_v1", "knowledge_point": code,
                "subpoint_id": key, "title": facet["title"],
                "status": "semantic_current" if facet_current else "semantic_stale",
                "parent_profile_version": reviewed["version"],
                "independent_mastery_inferred": False, "questions": []}
            views = {}
            for view in ("protected", "after_attempt", "direct"):
                value = {**shared, "view": view}
                if view != "protected" and facet_current:
                    value.update({field: copy.deepcopy(facet[field]) for field in
                        ("short_summary", "next_check")})
                    value["evidence_object"] = full_facet
                elif not facet_current:
                    value["reason"] = "parent_point_evidence_changed_after_review"
                views[view] = object_ref("knowledge-subpoints", value)
            subpoints[key] = {"knowledge_point": code, "title": facet["title"],
                "keywords": list(facet["keywords"]), "teaching_views": views,
                "evidence_object": full_facet}
    pages[(PAGE_DIR / "索引.md").as_posix()] = "\n".join([
        "# 408 知识点学习路径", "",
        "按知识点保存完整已记录过程，讲题读取预生成短摘要。事实整理与正式语义概况分别标明。", "",
        *[f"- [[{(PAGE_DIR / code).as_posix()}|{code} {labels.get(code, code)}]]" for code in sorted(points)], ""])
    aliases = {}
    for label, codes in memory["aliases"].items():
        variants = {_norm(label)}
        variants.update(_norm(code) for code in codes)
        # Human labels may carry a canonical code prefix.
        variants.add(_norm(re.sub(r"^(?:DS|CO|OS|CN)\d{2}-\d{2}\s*", "", label, flags=re.I)))
        for alias in variants - {"", "无", "未记录", "待补充", "none", "unknown"}:
            aliases.setdefault(alias, set()).update(codes)
    taxonomy, taxonomy_expansions = taxonomy_aliases(
        taxonomy_text, points, with_expansions=True)
    for alias, codes in taxonomy.items():
        aliases.setdefault(alias, set()).update(codes)
    for code, reviewed in (reviewed_profiles or {}).items():
        if code in points:
            for alias in reviewed.get("aliases", []):
                aliases.setdefault(_norm(alias), set()).add(code)
    source_coverage = source_coverage or {}
    coverage_ref = object_ref("point-source-coverage", source_coverage)
    compact_coverage = {key: value for key, value in source_coverage.items()
        if isinstance(value, (str, int, float, bool)) or
        (isinstance(value, dict) and all(isinstance(v, (int, float, bool)) for v in value.values()))}
    normalized_aliases = {key: sorted(value) for key, value in sorted(aliases.items())}
    alias_modes = {}
    for alias, codes in normalized_aliases.items():
        taxonomy_codes = sorted(taxonomy.get(alias, set()))
        alias_modes[alias] = (
            "unique" if len(codes) == 1 else
            "expansion" if alias in taxonomy_expansions and codes == taxonomy_codes else
            "ambiguous"
        )
    index = {"schema": POINT_INDEX_SCHEMA, "builder_version": BUILD_VERSION,
             "points": points, "aliases": normalized_aliases, "alias_modes": alias_modes,
             "subpoints": subpoints,
             "formal_points": memory["formal_points"], "point_records": point_records,
             "coverage": {**memory["coverage"], "point_count": len(points),
                 "canonical_point_count": len(labels),
                 "concept_brief_count": len(set(labels) & concept_briefs.keys()),
                 "missing_concept_briefs": sorted(set(labels) - concept_briefs.keys()),
                 "points_with_records": sum(bool(raw.get("record_count")) for raw in point_inputs.values()),
                 "empty_point_count": sum(not raw.get("record_count") for raw in point_inputs.values())},
             "source_coverage": compact_coverage,
             "learning_paths_index": {"relpath": (PAGE_DIR / "索引.md").as_posix(),
                 "sha256": hashlib.sha256(pages[(PAGE_DIR / "索引.md").as_posix()].encode("utf-8")).hexdigest()},
             "source_coverage_object": coverage_ref}
    index_ref = object_ref("point-indexes", index)
    return {"objects": objects, "pages": pages, "index": index, "index_ref": index_ref}


def validate_index(index):
    if index.get("schema") != POINT_INDEX_SCHEMA or index.get("builder_version") != BUILD_VERSION:
        raise ValueError("point_index_schema_mismatch")
    for field in ("points", "aliases", "formal_points", "point_records", "coverage"):
        if not isinstance(index.get(field), dict):
            raise ValueError("point_index_malformed:" + field)
    if set(index["points"]) != set(index["point_records"]):
        raise ValueError("point_index_record_partition_mismatch")


def _short_text(text, limit):
    """Prefer a complete clause when fitting historical reasoning into a budget."""
    if len(text) <= limit:
        return text
    prefix = text[:limit - 1]
    for separators in (("。", "；", ";"), ("，", ",")):
        end = max(prefix.rfind(mark) for mark in separators)
        if end >= limit // 3:
            return prefix[:end + 1] + "…"
    return prefix + "…"


def _observation(value):
    if not isinstance(value, dict):
        return None
    fields = ("record_key", "observed_date", "evidence_role", "point_binding", "summary", "outcome", "correction_result", "prompt_dependency", "conclusion_correct", "latest_is_ambiguous", "chronology_precision", "temporal_status", "evidence_status", "stored_observed_date", "stored_outcome", "choice_result", "reasoning_result")
    result = {key: value[key] for key in fields if value.get(key) is not None}
    if value.get("source_kind") in {"concept_review", "semantic_formal_intake"}:
        for field in ("source_kind", "source_id", "source_sha256", "source_fragment", "source_locator", "source_available", "source_access", "review_marker"):
            if field in value:
                result[field] = value[field]
    if value.get("correction_refs"):
        result["record_correction_refs"] = [
            {"event_id": ref["source_id"], "event_sha256": ref["source_sha256"],
             "kind": ref["event_kind"]} for ref in value["correction_refs"]]
        result["correction_is_not_a_new_learning_attempt"] = True
    if len(result.get("summary", "")) > 160:
        result["summary"] = _short_text(result["summary"], 160)
        result["text_truncated"] = True
    return result


def project_points(requested, missing, weak, source_coverage, normalized,
                   point_entries, read_summary, *, ambiguous_terms=None):
    """Project already-resolved point refs without reading a global index."""
    requested = list(dict.fromkeys(requested))
    missing = list(dict.fromkeys(missing))
    weak = set(weak)
    ambiguous_terms = list(ambiguous_terms or [])
    summaries = []
    for code in requested:
        entry = point_entries.get(code)
        if not entry:
            if code not in missing:
                missing.append(code)
            continue
        raw = read_summary(entry)
        if raw.get("knowledge_point") != code:
            raise ValueError("point_summary_identity_mismatch:" + code)
        base = {"knowledge_point": code, "requested_as_weak": code in weak,
                "record_count": raw.get("record_count", 0),
                "understanding_status": raw.get("understanding_status"),
                "latest_semantic_status": raw.get("latest_semantic_status"),
                "latest_chronology_ambiguous": raw.get("latest_chronology_ambiguous", False),
                "question_associated_count": raw.get("question_associated_count", 0),
                "summary_sha256": entry["sha256"]}
        if raw.get("concept_review_session_count"):
            for field in ("concept_review_session_count", "concept_review_error_session_count", "concept_review_count_semantics"):
                base[field] = raw[field]
        if normalized["stage"] == "pre_answer":
            base["content_fields_redacted"] = True
        else:
            if raw.get("question_refs"):
                base["question_refs"] = copy.deepcopy(raw["question_refs"][:3])
                base["question_refs_truncated"] = raw.get("question_refs_truncated", len(raw["question_refs"]) > 3)
            if raw.get("learning_profile"):
                base["learning_profile"] = copy.deepcopy(raw["learning_profile"])
                if raw["learning_profile"].get("status") == "current":
                    if raw["learning_profile"].get("question_refs"):
                        base.pop("question_refs", None)
                        base.pop("question_refs_truncated", None)
                    base["history_object"] = raw["history_object"]
                    summaries.append(base)
                    continue
            for field in ("latest_understanding", "latest_outcome", "latest_repair", "latest_hint", "latest_question_associated"):
                item = _observation(raw.get(field))
                if item:
                    base[field] = item
            formal_summaries = raw.get("formal_summaries", [])
            formal_ids = set(normalized.get("resolved_formal_ids") or [])
            relevant = next((item for item in formal_summaries if set(item.get("formal_ids", [])) & formal_ids),
                            formal_summaries[0] if formal_summaries else None)
            if relevant:
                base["formal_summary"] = _observation(relevant)
            base["historical_understanding"] = [_observation(item) for item in raw.get("historical_understanding", [])]
            undated = [_observation(item) for item in raw.get("undated_understanding", [])]
            instructions = [_observation(item) for item in raw.get("prior_instruction", [])]
            if undated:
                base["undated_understanding"] = undated
                base["undated_understanding_record_count"] = raw.get("undated_understanding_record_count", len(undated))
            if instructions:
                base["prior_instruction"] = instructions[:1]
                base["prior_instruction_record_count"] = raw.get("prior_instruction_record_count", len(instructions))
            base["history_summary_truncated"] = raw.get("record_count", 0) > len(base["historical_understanding"]) + int(bool(base.get("latest_understanding")))
        summaries.append(base)
    source_coverage = {key: value for key, value in source_coverage.items()
                       if isinstance(value, (int, float, bool)) or
                       (isinstance(value, str) and len(value) <= 120)}
    coverage = {"requested_points": requested, "queried_point_count": len(summaries),
                "missing_points": missing, "omitted_points": [],
                "all_requested_points_queried": not missing and not ambiguous_terms,
                "source_coverage": source_coverage,
                "independent_mastery_inferred": False,
                "record_count_semantics": "evidence_records_not_attempts"}
    if ambiguous_terms:
        coverage["ambiguous_terms"] = ambiguous_terms
    return summaries, coverage


def query_points(index, normalized, formal_ids, read_summary):
    """Read every requested point summary; never read any full record/history object."""
    validate_index(index)
    requested, missing, ambiguous, weak = [], [], [], set()

    def add_term(term, is_weak=False):
        codes = canonical_knowledge_points(term)
        if not codes:
            alias = _norm(term)
            codes = index["aliases"].get(alias, [])
            mode = (index.get("alias_modes") or {}).get(alias,
                "unique" if len(codes) == 1 else "ambiguous")
            if codes and mode == "ambiguous":
                ambiguous.append({"term": term, "candidate_points": list(codes)})
                return
        if not codes:
            if term not in missing:
                missing.append(term)
            return
        for code in codes:
            if code not in requested:
                requested.append(code)
            if is_weak:
                weak.add(code)

    for term in normalized.get("weak_knowledge", []):
        add_term(term, True)
    for term in normalized.get("knowledge", []):
        add_term(term)
    for formal_id in formal_ids:
        for code in index["formal_points"].get(formal_id, []):
            add_term(code)
    routed = dict(normalized)
    routed["resolved_formal_ids"] = list(formal_ids)
    return project_points(requested, missing, weak, index.get("source_coverage", {}),
                          routed, index["points"], read_summary,
                          ambiguous_terms=ambiguous)


def compact_summaries(result, level, *, preserve_evidence_fields=False):
    """Reduce each point fairly, preserving explicit all-point coverage."""
    for summary in result.get("knowledge_summaries", []):
        profile = summary.get("learning_profile")
        if isinstance(profile, dict) and profile.get("status") == "current":
            if level >= 2 and profile.get("question_refs"):
                profile["question_refs"] = profile["question_refs"][:1]
                profile["question_refs_truncated"] = True
            if level >= 3:
                profile.pop("short_summary", None)
                profile["content_omitted_for_budget"] = True
            # Never let a generic legacy compression silently erase the
            # reviewed-profile version/status or claim a partial text is whole.
            continue
        if level == 1:
            summary.pop("summary_sha256", None)
            # Exact record keys retain cold provenance. Avoid repeating an
            # identical package locator twice on every hot observation.
            for value in summary.values():
                observations = value if isinstance(value, list) else [value] if isinstance(value, dict) else []
                for observation in observations:
                    if observation.get("source_locator") == observation.get("source_id"):
                        observation.pop("source_locator", None)
        elif level == 2:
            summary["historical_understanding"] = summary.get("historical_understanding", [])[:1]
            summary.pop("latest_outcome", None)
            hint = summary.get("latest_hint") or {}
            if (hint.get("point_binding") != "explicit_point" or
                    any(hint.get("record_key") == (summary.get(field) or {}).get("record_key")
                        for field in ("latest_understanding", "latest_repair"))):
                # Prompt dependence stays on the actual repair/understanding.
                # Retain a distinct point-specific hint, not duplicate evidence.
                summary.pop("latest_hint", None)
            if summary.get("undated_understanding"):
                summary["undated_understanding"] = summary["undated_understanding"][:1]
            if any(summary.get(field) for field in ("latest_understanding", "historical_understanding",
                    "formal_summary", "undated_understanding", "latest_question_associated")):
                summary.pop("prior_instruction", None)
            if summary.get("latest_understanding") or summary.get("latest_question_associated"):
                summary.pop("formal_summary", None)
            for field in ("latest_understanding", "latest_repair", "latest_hint"):
                if summary.get(field, {}).get("summary"):
                    summary[field]["summary"] = _short_text(summary[field]["summary"], 80)
        elif summary.get("requested_as_weak"):
            # Preserve the requested weak point's latest and prior observation
            # before compressing unrelated editorial/reference material.
            keep = {"knowledge_point", "requested_as_weak", "record_count", "content_fields_redacted",
                    "understanding_status", "latest_chronology_ambiguous", "latest_understanding",
                    "historical_understanding", "latest_repair", "latest_hint", "undated_understanding"}
            has_observation = any(summary.get(field) for field in (
                "latest_understanding", "latest_repair", "historical_understanding", "undated_understanding"))
            if not has_observation:
                keep.update({"latest_question_associated", "formal_summary", "prior_instruction", "representative_evidence"})
            for field in list(summary):
                if field not in keep:
                    summary.pop(field)
            for field in ("historical_understanding", "undated_understanding", "prior_instruction"):
                if summary.get(field):
                    summary[field] = summary[field][:1]
            for field, value in summary.items():
                observations = value if isinstance(value, list) else [value] if isinstance(value, dict) else []
                for observation in observations:
                    if observation.get("summary") and len(observation["summary"]) > 80:
                        observation["summary"] = _short_text(observation["summary"], 80)
                    # In a wide query the immutable record key is the precise
                    # source pointer; long package paths stay in cold records.
                    # Keep source type, fragment, date and hash for claim checks.
                    if observation.get("record_key"):
                        observation.pop("source_locator", None)
                        observation.pop("source_access", None)
                        if len(str(observation.get("source_id", ""))) > 160:
                            observation.pop("source_id", None)
        elif level >= 4:
            for field in list(summary):
                if field not in {"knowledge_point", "requested_as_weak", "record_count", "content_fields_redacted"}:
                    summary.pop(field)
        else:
            best_field = next((field for field in (
                "latest_understanding", "latest_repair", "latest_question_associated", "formal_summary"
            ) if summary.get(field)), None)
            best = summary.get(best_field)
            if best is None:
                best = next(iter(summary.get("undated_understanding") or summary.get("prior_instruction") or []), None)
            for field in list(summary):
                if field not in {"knowledge_point", "requested_as_weak", "record_count", "content_fields_redacted",
                                 "understanding_status", "latest_chronology_ambiguous"}:
                    summary.pop(field)
            if best:
                target = best_field if preserve_evidence_fields and best_field else "representative_evidence"
                summary[target] = {key: value for key, value in best.items()
                    if key in {"record_key", "summary", "observed_date", "evidence_role", "point_binding",
                               "latest_is_ambiguous", "correction_result", "prompt_dependency",
                               "chronology_precision", "temporal_status", "evidence_status",
                               "stored_observed_date", "record_correction_refs", "correction_is_not_a_new_learning_attempt",
                               "source_kind", "source_sha256", "source_fragment"}}
                if len(summary[target].get("summary", "")) > 80:
                    summary[target]["summary"] = _short_text(summary[target]["summary"], 80)
        summary["truncated"] = True
    result["truncated"] = True
