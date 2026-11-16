"""Cold artifact layout and bounded hot queries for point-keyed 408 memory."""
from __future__ import annotations

import copy
import hashlib
import json
import re
import unicodedata
from pathlib import Path
from typing import Any, Mapping

from knowledge_learning_records_408 import (canonical_knowledge_points, semantic_evidence_fingerprint,
    SEMANTIC_EVIDENCE_CONTRACT, CROSS_POINT_EVIDENCE_CONTRACT)
from knowledge_learning_paths_408 import PAGE_DIR, build_path
from knowledge_profile_408 import teaching_plan_hot, teaching_focus_hot, profile_teaching_source_state
from visual_teaching_templates_408 import load_catalogs, project_template, source_visual_guards
from question_visual_templates_408 import materialize as materialize_question_visuals

BUILD_VERSION = "point-memory-408-v16-complete-personal-context"
POINT_INDEX_SCHEMA = "knowledge_tutoring_point_index_408_v3"
# Cold audit index also binds exact-question template source guards. The existing
# point/KSP index is already ~6.9 MB; fixed question coverage must not be dropped
# to fit the former 8 MB cap. Hot teaching still follows small hashed buckets.
MAX_POINT_INDEX_BYTES = 16 * 1024 * 1024
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
    from syllabus_context_408 import compact_context, context_for, load_contexts, load_question_introductions
    from source_visual_408 import clear_cache
    from teaching_research_408 import load_catalog as load_research, project as project_research
    from teaching_research_408 import effective_route
    from lesson_alignment_408 import load_assessment_catalog, compile_point_bridge

    clear_cache()

    objects = {}
    pages = {}
    labels = {match.group(1): match.group(2) for line in taxonomy_text.splitlines()
              if (match := re.fullmatch(r"\s*-\s+((?:DS|CO|OS|CN)\d{2}-\d{2})\s+(.+?)\s*", line))}
    root = Path(repo_root) if repo_root is not None else None
    research_catalog = load_research(root)
    syllabus_contexts = load_contexts(root) if root is not None else {}
    assessment_catalog = load_assessment_catalog(root, syllabus_contexts)
    visual_catalog = load_catalogs(root, labels, allow_stale_sources=True, syllabus_labels={key: value["title"]
        for key, value in syllabus_contexts.items()
        if re.fullmatch(r"S27-(?:DS|CO|OS|CN)(?:-\d{2})*", key)})
    question_introductions = load_question_introductions(root) if root is not None else {}
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

    def materialize_syllabus_context(value):
        if not value:
            return None
        full = copy.deepcopy(value)
        # The complete immutable source excludes its own pointer. The compact
        # views reference it, so the portable exporter closes this reference too.
        full.pop("context_object", None)
        return {**full, "context_object": object_ref("syllabus-contexts", full)}

    # Records are partitioned by knowledge point. One shared record table made
    # every point's history reference the same object, so a single new record
    # changed the shared hash and rewrote all 599 histories and summaries.
    # Each record belongs to exactly one point, so per-point slices keep the
    # same bytes while confining a write to the point that actually changed.
    all_records = memory["records"]
    points = {}
    subpoints = {}
    syllabus_entries = {}
    question_introduction_entries = {}
    point_records = {}
    point_inputs = dict(memory["points"])
    for code in labels:
        point_inputs.setdefault(code, {"knowledge_point": code, "record_keys": [],
            "unknown_date_record_keys": [], "record_count": 0, "formal_ids": [],
            "understanding_status": "no_personal_history", "latest_semantic_status": "no_dated_point_observation",
            "establishes_independent_mastery": False})
    record_slices = {code: {"schema": memory["schema"], "knowledge_point": code,
        "records": {key: all_records[key] for key in
                    list(raw["record_keys"]) + list(raw["unknown_date_record_keys"])}}
        for code, raw in point_inputs.items()}
    source_visual_guard_cache = {}
    point_source_visual_count = 0
    point_source_visual_fallback_count = 0
    for code, raw in sorted(point_inputs.items()):
        visual_template = visual_catalog["templates"].get(code)
        visual_ref = object_ref("visual-teaching-templates", visual_template) if visual_template else None
        visual_projection = project_template(visual_template, visual_ref, repo_root=root)
        source_visuals = visual_projection.get("visual_template", {}).get("source_visuals")
        if source_visuals is not None:
            point_source_visual_count += 1
            point_source_visual_fallback_count += visual_projection.get("visual_template_status") == "source_visual_only"
            visual_catalog["guards"].setdefault(code, []).extend(
                source_visual_guards(root, source_visuals, source_visual_guard_cache))
        keys = list(raw["record_keys"]) + list(raw["unknown_date_record_keys"])
        records = object_ref("point-records", record_slices[code])
        point_records[code] = records
        history = object_ref("point-histories", {"knowledge_point": code,
            "record_keys": raw["record_keys"], "unknown_date_record_keys": raw["unknown_date_record_keys"],
            "records_object": records})
        summary = {key: value for key, value in raw.items()
                   if key not in {"record_keys", "unknown_date_record_keys"}}
        syllabus_context = materialize_syllabus_context(context_for(syllabus_contexts, code))
        if syllabus_context:
            summary["syllabus_context"] = compact_context(syllabus_context)
        summary["history_object"] = history
        summary.update(visual_projection)
        preferred = [fid for field in ("latest_understanding", "latest_repair", "latest_outcome")
                     for fid in (raw.get(field) or {}).get("formal_ids", [])]
        related = list(dict.fromkeys(preferred + list(raw.get("formal_ids", []))))
        available_refs = [question_references[fid] for fid in related if fid in (question_references or {})]
        summary["question_refs"] = available_refs[:3]
        summary["question_refs_truncated"] = len(available_refs) > 3
        reviewed = (reviewed_profiles or {}).get(code)
        parent_plan_state = {"current": True, "guards": []}
        cross_records = []
        cross_current = True
        if reviewed:
            reviewed = copy.deepcopy(reviewed)
            for ref in reviewed.get("question_refs", []):
                current_ref = (question_references or {}).get(ref.get("formal_id"))
                if current_ref:
                    ref.update({key: value for key, value in current_ref.items()
                                if key not in {"relationship"}})
            profile_ref = object_ref("reviewed-knowledge-profiles", reviewed)
            if reviewed.get("semantic_evidence_contract") == SEMANTIC_EVIDENCE_CONTRACT:
                current = reviewed.get("semantic_evidence_fingerprint") == semantic_evidence_fingerprint(objects[records["ref"]])
            else:
                # Historical accepted profiles remain under their original,
                # strict byte-fingerprint contract until a reviewed update.
                current = reviewed.get("evidence_fingerprint") == records["sha256"]
            if reviewed.get("cross_point_evidence_contract") == CROSS_POINT_EVIDENCE_CONTRACT:
                dependencies = reviewed.get("cross_point_sources", [])
                fingerprints = reviewed.get("cross_point_evidence_fingerprints", {})
                cross_current = set(fingerprints) == {d["concept_key"] for d in dependencies}
                for dependency in dependencies:
                    other = dependency["concept_key"]
                    other_slice = record_slices.get(other)
                    if (not other_slice or fingerprints.get(other) != semantic_evidence_fingerprint(other_slice)
                            or not set(dependency["record_keys"]) <= other_slice["records"].keys()):
                        cross_current = False
                        continue
                    cross_records.extend(copy.deepcopy(other_slice["records"][key])
                                         for key in dependency["record_keys"])
                current = current and cross_current
            parent_plan_state = profile_teaching_source_state(reviewed)
            current = current and parent_plan_state["current"]
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
                if reviewed.get("teaching_plan"):
                    summary.update(teaching_plan_hot(reviewed["teaching_plan"]))
                    summary["teaching_plan_evidence_object"] = profile_ref
                if reviewed.get("teaching_focus"):
                    summary.update(teaching_focus_hot(reviewed["teaching_focus"]))
                    summary["teaching_focus_evidence_object"] = profile_ref
            else:
                summary["learning_profile"]["reason"] = ("teaching_plan_original_source_changed"
                    if not parent_plan_state["current"] else "cross_point_evidence_changed_after_reviewed_profile"
                    if not cross_current else "point_evidence_changed_after_reviewed_profile")
        else:
            summary["learning_profile"] = {"status": "not_authored", "version": None}
        prepared = build_path(code, labels.get(code, code), raw,
            {key: all_records[key] for key in keys}, available_refs, reviewed,
            bool(reviewed and summary["learning_profile"]["status"] == "current"),
            repo_root=root, has_vault=bool(root and (root / ".obsidian").is_dir()),
            concept_brief=concept_briefs.get(code), syllabus_context=syllabus_context,
            reviewed_cross_records=cross_records, visual_template=visual_template)
        for view in ("after_attempt", "direct"):
            prepared["views"][view].update(copy.deepcopy(visual_projection))
            prepared["views"][view]["personal_source"] = {
                "status": summary["learning_profile"]["status"],
                "profile_version": summary["learning_profile"].get("version"),
                "profile_object": summary["learning_profile"].get("profile_object"),
                "records_object": records,
                "history_object": history,
                "summary_is_complete_personal_context": False,
            }
            if summary["learning_profile"].get("reason"):
                prepared["views"][view]["personal_source"]["reason"] = summary["learning_profile"]["reason"]
        if reviewed and reviewed.get("teaching_plan") and summary["learning_profile"]["status"] == "current":
            for view in ("after_attempt", "direct"):
                prepared["views"][view]["teaching_plan_evidence_object"] = profile_ref
        if reviewed and reviewed.get("teaching_focus") and summary["learning_profile"]["status"] == "current":
            for view in ("after_attempt", "direct"):
                prepared["views"][view]["teaching_focus_evidence_object"] = profile_ref
        summary.update(project_research(research_catalog, code, view='after_attempt',
            personal_status=summary['learning_profile']['status'], object_ref=object_ref,
            syllabus_context=summary.get('syllabus_context'), teaching_plan=summary.get('teaching_plan')))
        research_route = research_catalog['targets'].get(code)
        research_route = effective_route(research_route) if research_route else None
        summary['lesson_bridge'] = compile_point_bridge(code, knowledge_point=code,
            view='after_attempt', route=research_route, summary=summary,
            syllabus_context=summary.get('syllabus_context'), assessment_catalog=assessment_catalog)
        for view, value in prepared['views'].items():
            value.update(project_research(research_catalog, code, view=view,
                personal_status=summary['learning_profile']['status'], object_ref=object_ref,
                syllabus_context=value.get('syllabus_context'), teaching_plan=value.get('teaching_plan')))
            bridge = compile_point_bridge(code, knowledge_point=code, view=view,
                route=research_route, summary=value, syllabus_context=value.get('syllabus_context'),
                assessment_catalog=assessment_catalog)
            if bridge is not None:
                value['lesson_bridge'] = bridge
        pages[prepared["page_relpath"]] = prepared["markdown"]
        points[code] = {
            **object_ref("point-summaries", summary),
            "personal_source": copy.deepcopy(prepared["views"]["direct"]["personal_source"]),
            "teaching_views": {view: object_ref("teaching-summaries", value)
                               for view, value in prepared["views"].items()},
            "learning_path": {"relpath": prepared["page_relpath"],
                "sha256": hashlib.sha256(prepared["markdown"].encode("utf-8")).hexdigest()},
        }
        if visual_template and visual_template.get('_source_review_state') == 'stale':
            points[code]['stale_visual_template_evidence_object'] = visual_ref
        if parent_plan_state["guards"]:
            points[code]["teaching_plan_sources"] = parent_plan_state["guards"]
        if visual_catalog["guards"].get(code):
            points[code]["visual_template_sources"] = visual_catalog["guards"][code]
        # Each small, reviewed mechanism is independently addressable. A narrow
        # query never has to open the parent profile or its history object.
        for facet in (reviewed or {}).get("subpoints", []):
            key = facet["subpoint_id"]
            facet_syllabus_context = materialize_syllabus_context(context_for(syllabus_contexts, code, key))
            facet_plan_state = profile_teaching_source_state(facet)
            facet_current = summary["learning_profile"]["status"] == "current" and facet_plan_state["current"]
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
                if view != "protected":
                    value.update(project_template(visual_template, visual_ref, subpoint=facet, repo_root=root))
                # The objective exam scope remains usable even when learner
                # semantics are stale, but never hints at a protected answer.
                if view != "protected" and facet_syllabus_context:
                    value["syllabus_context"] = compact_context(facet_syllabus_context)
                if view != "protected" and facet_current:
                    value.update({field: copy.deepcopy(facet[field]) for field in
                        ("short_summary", "next_check")})
                    value["evidence_object"] = full_facet
                    if facet.get("teaching_plan"):
                        value.update(teaching_plan_hot(facet["teaching_plan"]))
                        value["teaching_plan_evidence_object"] = full_facet
                    if facet.get("teaching_focus"):
                        value.update(teaching_focus_hot(facet["teaching_focus"]))
                        value["teaching_focus_evidence_object"] = full_facet
                elif not facet_current:
                    value["reason"] = ("teaching_plan_original_source_changed" if not facet_plan_state["current"]
                                       else "parent_point_evidence_changed_after_review")
                value.update(project_research(research_catalog, key, view=view,
                    personal_status=shared["status"], object_ref=object_ref,
                    syllabus_context=value.get('syllabus_context'), teaching_plan=value.get('teaching_plan')))
                facet_route = research_catalog['targets'].get(key)
                bridge = compile_point_bridge(key, knowledge_point=code, view=view,
                    route=effective_route(facet_route) if facet_route else None, summary=value,
                    syllabus_context=value.get('syllabus_context'), assessment_catalog=assessment_catalog)
                if bridge is not None:
                    value['lesson_bridge'] = bridge
                views[view] = object_ref("knowledge-subpoints", value)
            subpoints[key] = {"knowledge_point": code, "title": facet["title"],
                "keywords": list(facet["keywords"]), "teaching_views": views,
                "evidence_object": full_facet,
                "syllabus_ids": [row["id"] for row in (facet_syllabus_context or {}).get("syllabus_entries", [])]
                if (facet_syllabus_context or {}).get("scope_status") in {"explicit", "within_scope"} else []}
            guards = parent_plan_state["guards"] + facet_plan_state["guards"]
            if guards:
                subpoints[key]["teaching_plan_sources"] = guards
            if visual_catalog["guards"].get(code):
                subpoints[key]["visual_template_sources"] = visual_catalog["guards"][code]
    # Official-outline identities are objective reference routes, not canonical
    # learner points. They must never enter records, histories or scheduling.
    for key, raw_context in sorted(syllabus_contexts.items()):
        if not re.fullmatch(r"S27-(?:DS|CO|OS|CN)(?:-\d{2})*", key):
            continue
        full_context = materialize_syllabus_context(raw_context)
        visual_template = visual_catalog["outline_templates"].get(key)
        visual_ref = object_ref("visual-teaching-templates", visual_template) if visual_template else None
        shared = {"schema": "knowledge_teaching_view_408_v1", "syllabus_id": key,
            "status": "syllabus_only", "source_kind": "syllabus_reference",
            "has_personal_history": False, "scheduling_eligible": False,
            "independent_mastery_inferred": False, "write_count": 0}
        views = {}
        for view in ("protected", "after_attempt", "direct"):
            value = {**shared, "view": view}
            if view == "protected":
                value["content_fields_redacted"] = True
            else:
                value["title"] = raw_context["title"]
                value["syllabus_context"] = compact_context(full_context)
                if visual_template:
                    value.update(project_template(visual_template, visual_ref, syllabus_id=key, repo_root=root))
            value.update(project_research(research_catalog, key, view=view,
                personal_status="no_personal_history", object_ref=object_ref,
                syllabus_context=value.get('syllabus_context')))
            views[view] = object_ref("syllabus-teaching-views", value)
        syllabus_entries[key] = {"syllabus_id": key, "title": raw_context["title"],
            "teaching_views": views}
        if visual_template and visual_template.get('_source_review_state') == 'stale':
            syllabus_entries[key]['stale_visual_template_evidence_object'] = visual_ref
        if visual_catalog["outline_guards"].get(key):
            syllabus_entries[key]["visual_template_sources"] = visual_catalog["outline_guards"][key]
    for identity, introduction in sorted(question_introductions.items()):
        if (not re.fullmatch(r"EXAM408-\d{4}-Q\d{2}(?:-P\d{2})?|(?:DS|CO|OS|CN)_(?:\d{4}|UNK)_\d{3}", identity)
                or introduction.get("schema") != "cs408-question-frequency-introduction-v1"
                or introduction.get("question_identity") != identity):
            raise ValueError("question_frequency_identity_invalid:" + str(identity))
        primary = introduction.get("primary_point")
        primary_rows = [row for row in introduction.get("points", [])
                        if row.get("knowledge_point") == primary]
        if (not isinstance(primary, str) or not re.fullmatch(r"(?:DS|CO|OS|CN)\d{2}-\d{2}", primary)
                or len(primary_rows) != 1 or not isinstance(introduction.get("primary_intro"), str)
                or not introduction["primary_intro"].strip()):
            raise ValueError("question_frequency_primary_invalid:" + identity)
        point = primary_rows[0]
        counts = [point.get(key) for key in ("total_count", "main_count", "secondary_only_count")]
        if (any(type(count) is not int or count < 0 for count in counts)
                or counts[0] != counts[1] + counts[2]):
            raise ValueError("question_frequency_counts_invalid:" + identity)
        complete = object_ref("question-exam-introductions", copy.deepcopy(introduction))
        short = {"schema": "cs408-question-frequency-view-v1", "question_identity": identity,
            "identity_kind": introduction["identity_kind"], "primary_point": primary,
            "primary_intro": introduction["primary_intro"],
            "count_basis": "unique_exam_questions", "frequency_scope": "canonical_point",
            **{key: copy.deepcopy(point[key]) for key in (
                "title", "total_count", "main_count", "secondary_only_count", "main_frequency_level")},
            "source_sha256": introduction["source_sha256"], "introduction_object": complete}
        question_introduction_entries[identity] = {
            "question_identity": identity, **object_ref("question-exam-intro-views", short)}
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
    question_visuals = materialize_question_visuals(root, object_ref, research_catalog=research_catalog,
        syllabus_contexts=syllabus_contexts, assessment_catalog=assessment_catalog)
    index = {"schema": POINT_INDEX_SCHEMA, "builder_version": BUILD_VERSION,
             "points": points, "aliases": normalized_aliases, "alias_modes": alias_modes,
             "subpoints": subpoints, "syllabus_entries": syllabus_entries,
             "question_introductions": question_introduction_entries,
             "question_scopes": assessment_catalog["exact_scopes"],
             "question_visuals": question_visuals["entries"],
             "question_visual_aliases": question_visuals["aliases"],
             "formal_points": memory["formal_points"], "point_records": point_records,
             "coverage": {**memory["coverage"], **question_visuals["coverage"], "point_count": len(points),
                 "canonical_point_count": len(labels),
                 "derived_teaching_route_count": len(research_catalog["targets"]),
                 "derived_teaching_route_coverage": research_catalog["coverage"],
                 "syllabus_entry_count": len(syllabus_entries),
                 "question_introduction_count": len(question_introduction_entries),
                 "concept_brief_count": len(set(labels) & concept_briefs.keys()),
                 "missing_concept_briefs": sorted(set(labels) - concept_briefs.keys()),
                 "visual_template_count": len(set(labels) & visual_catalog["templates"].keys()),
                 "point_source_visual_plan_count": point_source_visual_count,
                 "point_source_visual_fallback_count": point_source_visual_fallback_count,
                 "visual_source_rebound_template_count": sum(bool(t.get("_source_reference_rebindings"))
                     and t.get("_source_review_state") != "stale" for t in
                     [*visual_catalog["templates"].values(), *visual_catalog["outline_templates"].values()]),
                 "visual_source_rebinding_count": sum(len(t.get("_source_reference_rebindings", [])) for t in
                     [*visual_catalog["templates"].values(), *visual_catalog["outline_templates"].values()]),
                 "missing_visual_templates": sorted(set(labels) - visual_catalog["templates"].keys()),
                 "outline_visual_template_count": len(visual_catalog["outline_templates"]),
                 "outline_visual_template_ids": sorted(visual_catalog["outline_templates"]),
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
        corrections = []
        for ref in value["correction_refs"]:
            projected = {"event_id": ref["source_id"], "event_sha256": ref["source_sha256"]}
            kind = ref.get("event_kind") or ref.get("source_kind")
            if kind is not None:
                projected["kind"] = kind
            for field in ("source_kind", "action", "recorded_at",
                          "original_annotation_sha256", "previous_correction_sha256"):
                if field in ref:
                    projected[field] = ref[field]
            corrections.append(projected)
        result["record_correction_refs"] = corrections
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
            for field in ("visual_template_status", "visual_template", "visual_template_evidence_object", "visual_template_rebind_required", "visual_template_reason", "teaching_delivery_policy", "linear_teaching_route", "lesson_bridge"):
                if field in raw:
                    base[field] = copy.deepcopy(raw[field])
            if raw.get("syllabus_context"):
                base["syllabus_context"] = copy.deepcopy(raw["syllabus_context"])
            if raw.get("question_refs"):
                base["question_refs"] = copy.deepcopy(raw["question_refs"][:3])
                base["question_refs_truncated"] = raw.get("question_refs_truncated", len(raw["question_refs"]) > 3)
            if raw.get("learning_profile"):
                base["learning_profile"] = copy.deepcopy(raw["learning_profile"])
                if raw["learning_profile"].get("status") == "current":
                    for field in ("teaching_plan", "teaching_plan_status", "teaching_plan_omitted_for_budget",
                                  "teaching_plan_evidence_object", "teaching_focus", "teaching_focus_status",
                                  "teaching_focus_evidence_object"):
                        if field in raw:
                            base[field] = copy.deepcopy(raw[field])
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


def omit_teaching_plan_for_budget(summary):
    """Drop one whole hot plan, retaining its explicit immutable evidence route."""
    if "teaching_plan" not in summary:
        return False
    summary.pop("teaching_plan")
    summary.update(teaching_plan_status="omitted_for_budget", teaching_plan_omitted_for_budget=True)
    return True


def compact_summaries(result, level, *, preserve_evidence_fields=False):
    """Reduce each point fairly, preserving explicit all-point coverage."""
    for summary in result.get("knowledge_summaries", []):
        if level >= 1:
            omitted_advice = False
            for field in ('linear_teaching_route', 'teaching_delivery_policy'):
                if field in summary:
                    summary.pop(field)
                    omitted_advice = True
            if omitted_advice:
                summary['teaching_route_status'] = 'omitted_for_budget_use_resolve_teaching_view'
        if level >= 1 and summary.get('lesson_bridge'):
            # The legacy endpoint has a small shared response budget. Optional
            # teacher routing must yield before actual learner evidence does;
            # resolve's teaching view retains the complete prebuilt bridge.
            summary.pop('lesson_bridge')
            summary['lesson_bridge_status'] = 'omitted_for_budget_use_resolve_teaching_view'
        if level >= 2:
            omit_teaching_plan_for_budget(summary)
        if level >= 3 and "teaching_focus" in summary:
            summary.pop("teaching_focus")
            summary["teaching_focus_status"] = "omitted_for_budget"
        if level >= 3 and summary.get("visual_template"):
            # Raw legacy queries retain an explicit full-object route. Normal
            # teaching views do not use this byte-budget compactor.
            summary.pop("visual_template")
            summary["visual_template_status"] = "omitted_for_budget"
            summary["visual_template_next_action"] = (
                "原始查询字节预算不足；讲题前用 resolve 的 after_attempt/direct 视图读取完整模板。")
        profile = summary.get("learning_profile")
        if isinstance(profile, dict) and profile.get("status") == "current":
            if level >= 2 and profile.get("question_refs"):
                profile["question_refs"] = profile["question_refs"][:1]
                profile["question_refs_truncated"] = True
            if level >= 3 and not summary.get("teaching_plan_evidence_object"):
                profile.pop("short_summary", None)
                profile["content_omitted_for_budget"] = True
            # A plan may be wholly omitted, but its authored short preference /
            # scope / next-check reminder stays with the reviewed profile. If
            # even that cannot fit, the caller explicitly omits the whole point.
            # Never let a generic legacy compression silently erase the
            # reviewed-profile version/status or claim a partial text is whole.
            continue
        if level == 1:
            summary.pop("summary_sha256", None)
            # Exact record keys retain cold provenance. Avoid repeating an
            # identical package locator twice on every hot observation.
            for field, value in summary.items():
                if field == "syllabus_context":
                    continue
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
                    "historical_understanding", "latest_repair", "latest_hint", "undated_understanding",
                    "syllabus_context", "visual_template_status", "visual_template_evidence_object",
                    "visual_template_next_action"}
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
                if field == "syllabus_context":
                    continue
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
                if field not in {"knowledge_point", "requested_as_weak", "record_count", "content_fields_redacted",
                                 "syllabus_context", "visual_template_status", "visual_template_evidence_object",
                                 "visual_template_next_action"}:
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
                                 "understanding_status", "latest_chronology_ambiguous", "syllabus_context",
                                 "visual_template_status", "visual_template_evidence_object", "visual_template_next_action"}:
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
