#!/usr/bin/env python3
"""Read-only, versioned access to the existing 408 knowledge and evidence stores.

The catalog is built by the existing cold publisher.  It contains identities,
typed links and object references, never a second copy of learner evidence.
Queries validate the same native manifest and only open selected immutable
objects.  Discovery scores locate candidates; they never establish mastery.
"""
from __future__ import annotations

import argparse
import copy
import datetime as dt
import hashlib
import json
import re
import subprocess
import sys
import unicodedata
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any, Mapping
from zoneinfo import ZoneInfo

SCHEMA = "cs408-memory-access-v1"
CATALOG_SCHEMA = "cs408-memory-catalog-v1"
BUILDER_VERSION = "memory-access-408-v1"
DIRECTORY = "objects/memory-catalog"
MAX_OBJECT_BYTES = 16 * 1024 * 1024
MAX_CANDIDATES = 8
VIEWS = {"protected", "after_attempt", "direct"}
POINT_RE = re.compile(r"(?:DS|CO|OS|CN)\d{2}-\d{2}$")
SHA_RE = re.compile(r"[a-f0-9]{64}$")
EXAM_RE = re.compile(r"EXAM408-(\d{4})-Q\d{2}(?:-P\d{2})?$")
FORMAL_RE = re.compile(r"(?:DS|CO|OS|CN)_[A-Z0-9_]+$")
SAFE_LABEL_RE = re.compile(r"正确(?:答案|选项)|答案[为是:：]|应选[ABCD]|解得|因此.{0,8}选|/Users/|/Volumes/", re.I)


class MemoryAccessError(ValueError):
    pass


def _bytes(value):
    return (json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n").encode()


def _sha(value):
    return hashlib.sha256(_bytes(value)).hexdigest()


def _norm(value):
    return re.sub(r"\s+", " ", unicodedata.normalize("NFKC", str(value or ""))).strip().casefold()


def _label(value, fallback):
    text = str(value or "").strip()
    return text if 0 < len(text) <= 160 and not SAFE_LABEL_RE.search(text) else fallback


def _ref(value):
    return {"ref": value["ref"], "sha256": value["sha256"]} if isinstance(value, dict) and isinstance(value.get("ref"), str) and SHA_RE.fullmatch(str(value.get("sha256", ""))) else None


def build_catalog(point_artifacts, exact_index, *, exam_links=None, formal_relations=None, repo=None):
    """Pure cold compilation from already-verified native publication inputs."""
    index, source_objects = point_artifacts["index"], point_artifacts["objects"]
    entities, edges, objects = {}, defaultdict(list), {}

    def put(value):
        digest = _sha(value)
        ref = f"{DIRECTORY}/{digest}.json"
        objects[ref] = value
        return {"ref": ref, "sha256": digest}

    def read(reference):
        if not reference:
            return {}
        value = source_objects.get(reference["ref"])
        if value is None or _sha(value) != reference["sha256"]:
            raise MemoryAccessError("catalog_source_object_mismatch")
        return value

    def add(identity, kind, label=None, **fields):
        row = entities.setdefault(identity, {"id": identity, "kind": kind,
            "label": _label(label, identity), "aliases": [], "owner": "native_408",
            "source_refs": [], "dependencies": [], "views": sorted(VIEWS),
            "storage": "local_hot", "authority": "native_versioned_projection"})
        row.update(fields)
        return row

    def edge(start, target, relation, basis, **extra):
        if not isinstance(start, str) or not isinstance(target, str) or not start or not target:
            return
        value = {"source": start, "target": target, "type": relation,
            "direction": "source_to_target", "basis": basis, "status": "reviewed", **extra}
        if value not in edges[start]:
            edges[start].append(value)
        if value not in edges[target]:
            edges[target].append(value)

    for code, entry in sorted(index.get("points", {}).items()):
        summary = read(_ref(entry))
        protected = read(entry.get("teaching_views", {}).get("protected"))
        profile = summary.get("learning_profile") or {}
        row = add(code, "knowledge_point", protected.get("concept_name"),
            summary_object=_ref(entry), history_object=_ref(summary.get("history_object")),
            profile_object=_ref(profile.get("profile_object")),
            profile_status=profile.get("status", "not_authored"),
            profile_version=profile.get("version"), learning_path=copy.deepcopy(entry.get("learning_path")),
            syllabus_object=_ref((summary.get("syllabus_context") or {}).get("context_object")))
        row["dependencies"] = [r for r in (row.get("summary_object"), row.get("history_object"), row.get("profile_object"), row.get("syllabus_object")) if r]
        row["source_refs"] = [{"kind": "native_point", "id": code, **_ref(entry)}]
        reviewed = read(row.get("profile_object"))
        for other in reviewed.get("cross_point_sources", []):
            edge(code, other.get("concept_key"), "evidence_dependency", "reviewed_cross_point_sources",
                 record_keys=other.get("record_keys", []))
    for alias, codes in sorted(index.get("aliases", {}).items()):
        safe = _label(alias, "")
        if safe:
            for code in codes:
                if code in entities and safe not in entities[code]["aliases"]:
                    entities[code]["aliases"].append(safe)
    for key, entry in sorted(index.get("subpoints", {}).items()):
        row = add(key, "knowledge_subpoint", entry.get("title"),
            knowledge_point=entry["knowledge_point"], evidence_object=_ref(entry.get("evidence_object")))
        row["aliases"] = sorted({_label(x, "") for x in [entry.get("title"), *entry.get("keywords", [])]} - {""})
        row["dependencies"] = [row["evidence_object"]] if row["evidence_object"] else []
        row["source_refs"] = [{"kind": "native_subpoint", "id": key, **row["evidence_object"]}] if row["evidence_object"] else []
        edge(key, entry["knowledge_point"], "subpoint_of", "native_subpoint_identity")
    for key, entry in sorted(index.get("syllabus_entries", {}).items()):
        add(key, "syllabus_entry", entry.get("title"), dependencies=[entry["teaching_views"]["protected"]])
    for fid, entry in sorted(exact_index.get("profiles", {}).items()):
        # Formal/exam labels deliberately never contain the learner's wrong step.
        add(fid, "formal_question", fid, source_refs=[{"kind": "formal_question", "id": fid}],
            formal_profile_object={"ref": entry["profile_ref"], "sha256": entry["profile_sha256"]})
        for code in index.get("formal_points", {}).get(fid, []):
            edge(fid, code, "question_associated", "native_formal_point_binding")
    for qid in sorted(set(index.get("question_visuals", {})) | set(index.get("question_introductions", {}))):
        add(qid, "exam_question" if EXAM_RE.fullmatch(qid) else "question", qid)
    for group in ("questions", "subquestions"):
        for qid, row in sorted((exam_links or {}).get(group, {}).items()):
            add(qid, "exam_question", qid, contact_dependency_ids=list(row.get("contact_dependency_ids", [])))
            primary = row.get("frequency_primary_points") or ([row["primary_point"]] if row.get("primary_point") else [])
            for code in primary:
                edge(qid, code, "main_assessment", "reviewed_exam_point_links")
            for code in row.get("assessed_points", []):
                if code not in primary:
                    edge(qid, code, "secondary_assessment", "reviewed_exam_point_links")
            for field, relation in (("prerequisite_points", "prerequisite"), ("context_only_points", "background"), ("syllabus_ids", "syllabus_binding")):
                for code in row.get(field, []):
                    edge(qid, code, relation, "reviewed_exam_point_links")
    for fid, row in (exam_links or {}).get("formal_cards", {}).items():
        if fid in entities and row.get("canonical_exam_id"):
            edge(fid, row["canonical_exam_id"], "source_question", row.get("binding_basis", "reviewed_formal_binding"))
            entities[fid]["canonical_exam_id"] = row["canonical_exam_id"]
    for row in formal_relations or []:
        code = str(row.get("关系类型") or "").split()[0]
        if re.fullmatch(r"R0[1-9]", code):
            edge(row.get("起点ID"), row.get("终点ID"), code, "formal_relation_table",
                 strength=row.get("联系强度"), reason=row.get("关联原因"))
    source_coverage = read(index.get("source_coverage_object"))
    for revision in source_coverage.get("formal_annotation_revisions", []):
        for code in revision.get("knowledge_points", []):
            if code in entities:
                entities[code].setdefault("annotation_revisions", []).append(copy.deepcopy(revision))
    from memory_discovery_408 import build_search
    precision = build_search(point_artifacts, entities, repo=repo)
    objects.update(precision["objects"])
    buckets = defaultdict(dict)
    discovery = []
    for identity, row in sorted(entities.items()):
        row["relations_object"] = put({"schema": "cs408-memory-relations-v1", "id": identity,
            "relations": sorted(edges[identity], key=lambda x: (x["source"], x["target"], x["type"]))})
        row["dependencies"].append(row["relations_object"])
        prefix = hashlib.sha256(identity.encode()).hexdigest()[:2]
        buckets[prefix][identity] = row
        discovery.append({k: row[k] for k in ("id", "kind", "label", "aliases", "canonical_exam_id", "contact_dependency_ids") if k in row})
    directory = {prefix: put({"schema": "cs408-memory-directory-bucket-v1", "entries": rows}) for prefix, rows in sorted(buckets.items())}
    descriptor = {"schema": CATALOG_SCHEMA, "builder_version": BUILDER_VERSION,
        "buckets": directory, "discovery_object": put({"schema": "cs408-memory-discovery-v1", "documents": discovery}),
        "precision_search": precision["descriptor"],
        "entity_count": len(entities), "kind_counts": dict(sorted(Counter(e["kind"] for e in entities.values()).items())),
        "relations_coverage": {"exam_links": exam_links is not None, "formal_relations": formal_relations is not None},
        "legacy_wiki_authority": "historical_supplementary_unless_source_version_bound",
        "formal_write_count": 0}
    descriptor["sha256"] = _sha(descriptor)
    return {"descriptor": descriptor, "objects": objects}


class Snapshot:
    def __init__(self, repo, output_root=None):
        import exam_tutoring_context_408 as native
        self.native = native
        self.repo = native._resolve_repo(repo)
        self.state = native._resolve_state_root(self.repo, output_root)
        self.manifest, self.manifest_sha = native._load_manifest(self.state)
        reasons = native._freshness_reasons(self.repo, self.manifest)
        if reasons:
            raise MemoryAccessError("snapshot_stale:" + ",".join(reasons))
        self.reads, self.cache = {}, {}
        self.descriptor = self.manifest.get("memory_catalog")
        if self.descriptor:
            declared = self.descriptor.get("sha256")
            if (self.descriptor.get("schema") != CATALOG_SCHEMA or self.descriptor.get("builder_version") != BUILDER_VERSION
                    or declared != _sha({k: v for k, v in self.descriptor.items() if k != "sha256"})):
                raise MemoryAccessError("catalog_descriptor_invalid")

    def read(self, reference):
        if not _ref(reference):
            raise MemoryAccessError("object_reference_invalid")
        key = reference["ref"]
        if key not in self.cache:
            self.cache[key] = self.native._read_content_object(self.state, reference["ref"], reference["sha256"], "memory object", max_bytes=MAX_OBJECT_BYTES)
            self.reads[key] = reference["sha256"]
        return copy.deepcopy(self.cache[key])

    def entity(self, identity):
        if not self.descriptor:
            raise MemoryAccessError("catalog_not_built")
        prefix = hashlib.sha256(identity.encode()).hexdigest()[:2]
        ref = self.descriptor["buckets"].get(prefix)
        return self.read(ref).get("entries", {}).get(identity) if ref else None

    def documents(self):
        if not self.descriptor:
            raise MemoryAccessError("catalog_not_built")
        return self.read(self.descriptor["discovery_object"])["documents"]

    def finish(self, result):
        return {"schema": SCHEMA, "source_version": self.manifest["source_version"],
            "manifest_sha256": self.manifest_sha, "formal_write_count": 0,
            "learner_evidence_write_count": 0, "independent_mastery_inferred": False,
            **result, "read_receipt": {"objects": [{"ref": k, "sha256": v} for k, v in sorted(self.reads.items())],
                "source_version": self.manifest["source_version"], "write_count": 0}}


def _selection(selector):
    if selector is None:
        return {}
    if isinstance(selector, str):
        return {"id": selector}
    allowed = {"id", "concept", "formal_id", "source_id", "current_question_id", "question_part", "mechanisms"}
    if not isinstance(selector, dict) or set(selector) - allowed:
        raise MemoryAccessError("selector_invalid")
    for key, value in selector.items():
        if key == "mechanisms":
            if not isinstance(value, list) or len(value) > 16 or any(not isinstance(x, str) or not x.strip() or len(x) > 300 for x in value):
                raise MemoryAccessError("mechanism_scope_invalid")
        elif not isinstance(value, str) or not value.strip() or len(value) > 300:
            raise MemoryAccessError("selector_value_invalid")
    return selector


def _query(selector, view):
    import exam_tutoring_context_408 as native
    result = {"schema": native.QUERY_INPUT_SCHEMA, "stage": "pre_answer" if view == "protected" else "post_answer"}
    identity = selector.get("id")
    if identity:
        if FORMAL_RE.fullmatch(identity):
            result["formal_id"] = identity
        elif EXAM_RE.fullmatch(identity):
            result["current_question_id"] = identity
        else:
            result["knowledge_points"] = [identity]
    if selector.get("concept"):
        result["knowledge_points"] = [selector["concept"]]
    for key in ("formal_id", "source_id", "current_question_id"):
        if key in selector:
            result[key] = selector[key]
    if selector.get("question_part"):
        result["current_question_part"] = selector["question_part"]
    if selector.get("mechanisms"):
        result["mechanism_terms"] = selector["mechanisms"]
    return result


def _entities(snapshot, selector):
    native = snapshot.native
    query = native._validate_query(_query(selector, "direct"))
    from knowledge_hot_routes_408 import resolve
    routed = resolve(snapshot.manifest["knowledge_hot_routes"], query, snapshot.read, include_profiles=False)
    keys = list(routed["subpoint_entries"]) or list(routed["point_entries"])
    keys += list(routed["syllabus_entries"])
    explicit = selector.get("id") or selector.get("formal_id") or selector.get("current_question_id")
    if explicit and snapshot.entity(explicit):
        keys.insert(0, explicit)
    return [snapshot.entity(key) for key in dict.fromkeys(keys) if snapshot.entity(key)], routed


def _timestamp(value, *, end=False):
    if value is None:
        return None
    if not isinstance(value, str):
        raise MemoryAccessError("date_invalid")
    try:
        if re.fullmatch(r"\d{4}-\d{2}-\d{2}", value):
            date = dt.date.fromisoformat(value)
            return dt.datetime.combine(date, dt.time.max if end else dt.time.min, ZoneInfo("Asia/Shanghai"))
        stamp = dt.datetime.fromisoformat(value.replace("Z", "+00:00"))
        return stamp.replace(tzinfo=ZoneInfo("Asia/Shanghai")) if stamp.tzinfo is None else stamp
    except ValueError as exc:
        raise MemoryAccessError("date_invalid") from exc


def _record_time(row, field):
    value = row.get(field)
    if not value:
        return None
    try:
        return _timestamp(value)
    except MemoryAccessError:
        return None


def _point_rows(snapshot, entities):
    rows, annotations = {}, {}
    for entity in entities:
        if entity["kind"] == "knowledge_subpoint":
            entity = snapshot.entity(entity["knowledge_point"])
        if not entity or entity["kind"] != "knowledge_point":
            continue
        history = snapshot.read(entity["history_object"])
        values = snapshot.read(history["records_object"])
        if values.get("knowledge_point") != entity["id"]:
            raise MemoryAccessError("point_history_identity_mismatch")
        for key, row in values.get("records", {}).items():
            if row.get("knowledge_point") != entity["id"] or row.get("record_key") != key:
                raise MemoryAccessError("record_scope_mismatch")
            rows[key] = row
        for item in entity.get("annotation_revisions", []):
            sha = item.get("annotation_sha256") or item.get("semantic_annotation_sha256") or item.get("original_annotation_sha256")
            if sha:
                annotations[sha] = item
    return list(rows.values()), annotations


def _verified_record_scope(snapshot, row):
    descriptor = (snapshot.descriptor or {}).get("precision_search", {}).get("evidence_search", {})
    key = row.get("record_key")
    if not isinstance(key, str):
        return None
    reference = descriptor.get("record_scope_buckets", {}).get(hashlib.sha256(key.encode()).hexdigest()[:2])
    if not reference:
        return None
    value = snapshot.read(reference)
    if value.get("schema") != "cs408-record-source-scopes-v1":
        raise MemoryAccessError("record_source_scope_schema_invalid")
    proof = value.get("records", {}).get(key)
    fields = ("record_key", "knowledge_point", "source_id", "source_sha256", "source_fragment")
    if proof and all(proof.get(field) == row.get(field) for field in fields):
        return proof["scope_proof"]
    return None


def _question_identities(snapshot, row):
    names = [row.get("source_id"), row.get("source_question_id"), *row.get("formal_ids", [])]
    found = set()
    unresolved = False
    for name in names:
        if not isinstance(name, str):
            continue
        if EXAM_RE.fullmatch(name):
            found.add(name)
        elif FORMAL_RE.fullmatch(name):
            entity = snapshot.entity(name)
            canonical = (entity or {}).get("canonical_exam_id")
            if canonical:
                found.add(canonical)
            else:
                unresolved = True
    proof = _verified_record_scope(snapshot, row)
    if proof:
        return set(proof["question_ids"]), False
    return found, unresolved


def _record_scope(snapshot, rows, entities, selector, years, allowed):
    """Apply the same source scope before and after revision reconstruction."""
    points = {e.get("knowledge_point", e["id"]) for e in entities if e["kind"] in {"knowledge_point", "knowledge_subpoint"}}
    current = selector.get("current_question_id") or selector.get("id", "")
    part = current if re.fullmatch(r"EXAM408-\d{4}-Q\d{2}-P\d{2}", current) else None
    references = []
    subpoints = [e for e in entities if e["kind"] == "knowledge_subpoint"]
    for entity in subpoints:
        facet = snapshot.read(entity["evidence_object"])
        for container in (facet, facet.get("teaching_focus") or {}, facet.get("teaching_plan") or {}):
            references.extend(r for r in container.get("source_refs", []) if isinstance(r, dict))
    accepted, unknown = [], []
    for row in rows:
        if row.get("knowledge_point") not in points:
            continue
        if subpoints:
            source_refs = [row, *row.get("evidence_refs", []), *row.get("original_turn_refs", [])]
            source_hashes = {r.get("source_sha256") or r.get("sha256") for r in source_refs if isinstance(r, dict)} - {None, ""}
            source_ids = {row.get("source_id"), row.get("record_key")} - {None, ""}
            matched = any((ref.get("id") and ref["id"] in source_ids)
                or (ref.get("record_key") and ref["record_key"] in source_ids)
                or (ref.get("sha256") and ref["sha256"] in source_hashes)
                or (ref.get("source_sha256") and ref["source_sha256"] in source_hashes) for ref in references)
            if not matched:
                unknown.append({"record_key": row["record_key"], "reason": "subpoint_source_binding_not_proven"})
                continue
        questions, unresolved = _question_identities(snapshot, row)
        if years or part:
            if not questions or unresolved:
                unknown.append({"record_key": row["record_key"], "reason": "question_scope_not_proven"})
                continue
            proof = _verified_record_scope(snapshot, row)
            dependencies = set((proof or {}).get("contact_dependency_ids", []))
            for question in questions:
                dependencies.update((snapshot.entity(question) or {}).get("contact_dependency_ids", []))
            if any(not EXAM_RE.fullmatch(q) or int(EXAM_RE.fullmatch(q)[1]) in years for q in questions | dependencies):
                continue
            if part and questions != {part}:
                continue
        if allowed is not None and not ({row.get("knowledge_point"), row.get("record_key"), row.get("source_id"), *row.get("formal_ids", []), *questions} & allowed):
            continue
        accepted.append(row)
    return accepted, unknown


def _annotation_scope(snapshot, annotations, selector, years):
    current = selector.get("current_question_id") or selector.get("id", "")
    part = current if re.fullmatch(r"EXAM408-\d{4}-Q\d{2}-P\d{2}", current) else None
    if not years and not part:
        return annotations
    result = {}
    for key, annotation in annotations.items():
        identities, unresolved = _question_identities(snapshot, annotation)
        if not identities or unresolved or any(int(EXAM_RE.fullmatch(q)[1]) in years for q in identities):
            continue
        if part and identities != {part}:
            continue
        result[key] = annotation
    return result


def _narrow_question(selector):
    identity = selector.get("current_question_id") or selector.get("id", "")
    return identity if re.fullmatch(r"EXAM408-\d{4}-Q\d{2}-P\d{2}", identity) else None


def _time_projection(rows, *, event_after=None, event_before=None, known_before=None):
    lower, upper = _timestamp(event_after), _timestamp(event_before, end=True)
    known = _timestamp(known_before, end=True)
    if lower and upper and lower > upper:
        raise MemoryAccessError("date_range_inverted")
    selected, unknown, unavailable = [], [], []
    for original in rows:
        row = copy.deepcopy(original)
        observed = _record_time(row, "observed_date")
        recorded = _record_time(row, "recorded_at")
        # Event timestamps describe occurrence unless an owner explicitly binds
        # them as recording time.  Never use mtime/import date as a substitute.
        if known and (recorded is None or recorded > known):
            if recorded is None:
                unknown.append({"record_key": row["record_key"], "reason": "recorded_at_unknown"})
            continue
        corrections = row.get("correction_refs") or []
        if known and corrections and not row.get("historical_revision_sha256"):
            correction_times = [_record_time(c, "recorded_at") for c in corrections]
            if any(t is None or t > known for t in correction_times):
                unavailable.append({"record_key": row["record_key"], "reason": "historical_revision_requires_source_replay"})
                continue
        if (lower or upper) and observed is None:
            unknown.append({"record_key": row["record_key"], "reason": "event_date_unknown"})
            continue
        if observed and ((lower and observed < lower) or (upper and observed > upper)):
            continue
        row.update(event_at=row.get("observed_date"), recorded_at=row.get("recorded_at"),
            temporal_status="known" if observed and recorded else "partial_time_information")
        selected.append(row)
    selected.sort(key=lambda row: (row.get("event_at") or "", row.get("source_order") if isinstance(row.get("source_order"), int) else -1, row["record_key"]))
    return {"records": selected, "unknown_time": unknown, "unavailable_revisions": unavailable,
        "complete": not unknown and not unavailable,
        "temporal_contract": {"event_time": "observed_date", "knowledge_time": "recorded_at", "timezone": "Asia/Shanghai", "file_mtime_used": False}}


def _historical_formal_rows(snapshot, rows, annotations, known_before):
    """Replay only named immutable annotation chains, never a full source scan."""
    if not known_before:
        return rows, []
    cutoff = _timestamp(known_before, end=True)
    identities = set(annotations)
    identities.update(r["semantic_annotation_sha256"] for r in rows if r.get("semantic_annotation_sha256"))
    if not identities:
        return rows, []
    from seal_knowledge_observations_408 import load_formal_record_history
    source_rows = {r["record_key"]: r for r in rows if r.get("semantic_annotation_sha256") not in identities}
    unknown = []
    for identity in sorted(identities):
        try:
            history = load_formal_record_history(snapshot.repo, identity)
        except (ValueError, OSError, KeyError) as exc:
            unknown.append({"annotation_sha256": identity, "reason": "historical_source_unavailable:" + type(exc).__name__})
            continue
        revisions = history["revisions"]
        eligible = [r for r in revisions if _record_time(r, "recorded_at") is not None and _record_time(r, "recorded_at") <= cutoff]
        if not eligible:
            if any(r.get("recorded_at") is None for r in revisions):
                unknown.append({"annotation_sha256": identity, "reason": "recorded_at_unknown"})
            continue
        revision = eligible[-1]
        if revision["action"] == "retract":
            continue
        for source in revision["records"]:
            for code in source.get("knowledge_points", []):
                key_material = ["semantic_formal_intake", history["source_id"], source.get("source_fragment", ""), code]
                # KLR hashes compact JSON without the publication newline.
                key = "KLR-" + hashlib.sha256(json.dumps(key_material, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
                value = {**copy.deepcopy(source), "record_key": key, "knowledge_point": code,
                    "source_kind": "semantic_formal_intake", "source_id": history["source_id"],
                    "source_sha256": history["source_sha256"], "semantic_annotation_sha256": identity,
                    "recorded_at": revision["recorded_at"], "correction_refs": copy.deepcopy(revision["correction_refs"]),
                    "historical_revision_sha256": revision["revision_sha256"], "semantic_lifecycle_status": "active",
                    "establishes_independent_mastery": False}
                source_rows[key] = value
    return list(source_rows.values()), unknown


def _metadata_records(result):
    """after_attempt is not permission to expose historical answers or hints."""
    safe = {"record_key", "knowledge_point", "source_kind", "source_id", "source_sha256", "source_fragment",
        "source_order", "recorded_at", "observed_date", "event_at", "event_time", "chronology_precision",
        "evidence_role", "prompt_dependency", "event_kind", "point_binding", "temporal_status",
        "semantic_annotation_sha256", "semantic_correction_sha256", "semantic_lifecycle_status"}
    result = copy.deepcopy(result)
    if "records" in result:
        result["records"] = [{k: v for k, v in row.items() if k in safe} for row in result["records"]]
    result["content_fields_redacted"] = True
    return result


def _scope(options):
    if options.get("subject", "408") != "408":
        raise MemoryAccessError("subject_out_of_scope")
    years = options.get("excluded_years") or []
    if not isinstance(years, (list, tuple, set)) or any(type(y) is not int or y < 2009 for y in years):
        raise MemoryAccessError("excluded_years_invalid")
    allowed = options.get("allowed_ids")
    if allowed is not None and (not isinstance(allowed, (list, tuple, set)) or any(not isinstance(x, str) for x in allowed)):
        raise MemoryAccessError("allowed_ids_invalid")
    return set(years), set(allowed) if allowed is not None else None


def _allowed(entity, years, allowed):
    if allowed is not None and entity["id"] not in allowed:
        return False
    identities = [entity["id"], entity.get("canonical_exam_id", ""), *entity.get("contact_dependency_ids", [])]
    return not any((m := EXAM_RE.fullmatch(x)) and int(m[1]) in years for x in identities if isinstance(x, str))


def _tokens(text):
    value = _norm(text)
    ascii_words = set(re.findall(r"[a-z0-9][a-z0-9_+-]*", value))
    chinese = re.findall(r"[\u3400-\u9fff]+", value)
    return ascii_words | {word[i:i + 2] for word in chinese for i in range(max(0, len(word) - 1))} | {x for x in chinese if len(x) == 1}


def pending_catalog(repo):
    """Read signed capture hot state without replay, raw transcripts or writes."""
    import intake_fact_capture_408 as captures
    import capture_hot_writer_408 as hot
    root = captures.capture_root(repo)
    state_path, ledger = root / "state.json", root / "events.jsonl"
    if not state_path.exists():
        return {"status": "no_record", "documents": [], "write_count": 0}
    hot_root = root / ".capture-hot-writer-v1"
    try:
        hot._require_no_pending(hot_root)
        joint, joint_sha = hot._load_joint(hot_root)
        hot._assert_paths(joint, ledger, state_path)
        state_raw, state = hot._load_state(state_path, maximum=hot.DEFAULT_MAX_STATE_BYTES)
        hot._assert_state_binding(joint, state_raw, state)
        if ledger.stat().st_size != joint["bounded_jsonl"]["ledger_size_bytes"]:
            raise MemoryAccessError("pending_capture_ledger_head_changed")
        # Detect a publisher switching generations while the state was read.
        if hot._load_joint(hot_root)[1] != joint_sha:
            raise MemoryAccessError("pending_capture_generation_changed")
    except (ValueError, OSError, KeyError, hot.CaptureHotWriterError) as exc:
        return {"status": "unavailable", "reason": "pending_capture_head_unverified:" + type(exc).__name__, "documents": [], "write_count": 0}
    documents = []
    pending_ids = {identity for values in state.get("pending_by_date", {}).values() for identity in values}
    for identity in sorted(pending_ids):
        row = state.get("captures", {}).get(identity)
        if not isinstance(row, dict) or row.get("capture_id") != identity:
            raise MemoryAccessError("pending_capture_identity_mismatch")
        capture = row.get("capture") or {}
        source = (capture.get("source_facts") or {}).get("source_id")
        safe_source = source if isinstance(source, str) and (EXAM_RE.fullmatch(source) or FORMAL_RE.fullmatch(source)) else None
        references = [{"kind": ref.get("kind"), "sha256": ref.get("sha256")} for ref in capture.get("stable_evidence_refs", []) if isinstance(ref, dict) and SHA_RE.fullmatch(str(ref.get("sha256", "")))]
        documents.append({"id": identity, "kind": "pending_capture", "label": identity,
            "aliases": [safe_source] if safe_source else [], "canonical_exam_id": safe_source if safe_source and EXAM_RE.fullmatch(safe_source) else None,
            "source_refs": references, "study_date": row.get("study_date"), "recorded_at": row.get("recorded_at"),
            "status": "pending_formal_review", "native_status": row.get("quality_status"),
            "payload_sha256": row.get("payload_sha256"), "formal_claim_authority": False,
            "semantic_coverage": "exact_identity_only_no_unreviewed_topic_extraction"})
    return {"status": "available", "documents": documents, "capture_generation": joint.get("generation_id"),
        "joint_manifest_sha256": joint_sha, "state_sha256": hashlib.sha256(state_raw).hexdigest(), "write_count": 0}


def discover(repo, query, *, view="protected", limit=8, backend="native", output_root=None, allowed_ids=None,
             current_question_id=None, excluded_years=None, subject="408", backend_options=None, include_pending=False, selector=None,
             intent="auto", actor="user", event_after=None, event_before=None, known_before=None, latest=None):
    return access(repo, "discover", selector, view=view, output_root=output_root, query=query, limit=limit, backend=backend,
        allowed_ids=allowed_ids, current_question_id=current_question_id, excluded_years=excluded_years,
        subject=subject, backend_options=backend_options, include_pending=include_pending, intent=intent, actor=actor,
        event_after=event_after, event_before=event_before, known_before=known_before, latest=latest)


def _discovery_backend(snapshot, options):
    """Apply a locally qualified public backend inside the existing entrypoint."""
    requested = options.get("backend", "native")
    explicit = options.get("backend_options")
    from memory_discovery_408 import infer_intent
    if requested != "native" or explicit is not None or infer_intent(options.get("query") or "", options.get("intent", "auto")) != "concept":
        return requested, explicit, {}
    path = snapshot.repo / ".private/memory-retrieval-runtime/discovery-policy.json"
    if not path.is_file():
        return "native", None, {}
    meta = {}
    try:
        if path.is_symlink() or path.stat().st_size > 65536:
            raise ValueError("local_policy_file_invalid")
        raw = path.read_bytes()
        policy = json.loads(raw)
        meta["policy_sha256"] = hashlib.sha256(raw).hexdigest()
        if (policy.get("schema") != "cs408-local-discovery-policy-v1" or policy.get("status") != "qualified"
                or policy.get("default_backend") != "hybrid_local"):
            raise ValueError("local_backend_not_qualified")
        projection = (snapshot.descriptor or {}).get("precision_search", {}).get("public_semantic_projection_sha256")
        if not projection or policy.get("projection_sha256") != projection:
            raise ValueError("semantic_public_projection_changed")
        receipt = path.parent / policy.get("qualification_ref", "qualification.json")
        if (receipt.is_symlink() or not receipt.resolve().is_relative_to(path.parent.resolve())
                or hashlib.sha256(receipt.read_bytes()).hexdigest() != policy.get("qualification_sha256")):
            raise ValueError("local_backend_qualification_invalid")
        semantic = policy["backend_options"]["semantic"]
        required = {"runtime_root", "index_path", "manifest_path", "model_path", "expected_projection_hash", "trial"}
        if set(semantic) != required or semantic["trial"] is not True or semantic["expected_projection_hash"] != projection:
            raise ValueError("local_backend_options_invalid")
        private = (snapshot.repo / ".private").resolve()
        if any(not Path(semantic[key]).resolve().is_relative_to(private) for key in ("runtime_root", "index_path", "manifest_path", "model_path")):
            raise ValueError("local_backend_path_outside_private_runtime")
        return "hybrid_local", {"semantic": {**semantic, "timeout": 10}}, {**meta, "qualified": True}
    except (OSError, ValueError, KeyError, TypeError) as exc:
        return "native", None, {**meta, "degraded": str(exc) or "local_policy_unavailable"}


def access(repo, operation, selector=None, *, view="protected", output_root=None, **options):
    """Public API. Every operation is read-only and defaults to protected data."""
    if view not in VIEWS:
        raise MemoryAccessError("view_invalid")
    selector = _selection(selector)
    years, allowed = _scope(options)
    if operation not in {"catalog", "context", "history", "compare", "trace", "relations", "preferences", "discover", "lint", "coverage"}:
        raise MemoryAccessError("operation_invalid")
    if operation == "context":
        import exam_tutoring_context_408 as native
        public_question = selector.get("current_question_id") or selector.get("formal_id")
        if not public_question and (EXAM_RE.fullmatch(selector.get("id", "")) or FORMAL_RE.fullmatch(selector.get("id", ""))):
            public_question = selector["id"]
        if view == "protected" and public_question:
            scoped = Snapshot(repo, output_root)
            return scoped.finish({"operation": operation, "status": "protected", "identities": [public_question],
                "content_fields_redacted": True, "reason": "current_question_routes_and_evidence_withheld",
                "context": {"status": "protected", "records": [], "source_version": scoped.manifest["source_version"],
                    "personal_read_receipt": {"status": "withheld", "complete": None,
                        "reason": "answer_protection", "records": [], "raw_evidence_loaded": False}}})
        # Range filters are enforced before content-bearing native resolution.
        if years or allowed is not None:
            scoped = Snapshot(repo, output_root)
            selected, _ = _entities(scoped, selector)
            if not selected or any(not _allowed(e, years, allowed) for e in selected):
                return scoped.finish({"operation": operation, "status": "protected", "reason": "selector_outside_allowed_scope", "context": {}})
            if years:
                # Native full-personal profiles predate per-source year guards.
                # A point being allowed cannot grant every historical source
                # behind its free-text synthesis.  Keep this explicit rather
                # than returning the profile and trusting the final answer.
                return scoped.finish({"operation": operation, "status": "insufficient_evidence",
                    "reason": "full_context_source_year_scope_not_proven", "context": {}, "complete": False,
                    "fallback": "current_question_only_or_scoped_history"})
        result = native.query_context(repo, _query(selector, view), output_root=output_root,
            teaching_view=view, known_version=options.get("known_version"),
            personal_scope="full", materials=options.get("materials", "core"))
        return {"schema": SCHEMA, "operation": operation, "status": result.get("status"),
            "source_version": result.get("source_version"), "read_receipt": result.get("personal_read_receipt"),
            "context": result, "formal_write_count": 0, "learner_evidence_write_count": 0}
    snapshot = Snapshot(repo, output_root)
    expected_version = options.get("expected_source_version")
    if expected_version is not None and expected_version != snapshot.manifest["source_version"]:
        raise MemoryAccessError("snapshot_pagination_version_changed")
    pending = pending_catalog(repo) if options.get("include_pending") is True else {"status": "not_requested", "documents": []}
    pending_by_id = {row["id"]: row for row in pending["documents"]}
    public_question = selector.get("current_question_id") or (selector.get("id") if EXAM_RE.fullmatch(selector.get("id", "")) else None)
    if view == "protected" and public_question and operation != "discover":
        return snapshot.finish({"operation": operation, "status": "protected", "identities": [public_question],
            "content_fields_redacted": True, "reason": "current_question_routes_and_evidence_withheld"})
    if operation == "coverage":
        from formal_quality_index_408 import coverage
        if selector:
            documents, _ = _entities(snapshot, selector)
        else:
            documents = snapshot.documents()
        permitted = {row["id"] for row in documents if _allowed(row, years, allowed)}
        return snapshot.finish(coverage(snapshot, permitted, offset=options.get("offset", 0),
                                        limit=options.get("limit", 50)))
    if operation == "discover":
        from memory_discovery_408 import discover as precise_discover
        backend, backend_options, backend_policy = _discovery_backend(snapshot, options)
        request = dict(selector=selector, view=view,
            limit=options.get("limit", 8), intent=options.get("intent", "auto"), actor=options.get("actor", "user"),
            excluded_years=years, allowed_ids=allowed, current_question_id=options.get("current_question_id"),
            event_after=options.get("event_after"), event_before=options.get("event_before"),
            known_before=options.get("known_before"), latest=options.get("latest"),
            pending=pending, evidence_gap=options.get("evidence_gap"))
        try:
            result = precise_discover(snapshot, options.get("query"), backend=backend, backend_options=backend_options, **request)
        except (OSError, ValueError, subprocess.SubprocessError) as exc:
            if not backend_policy.get("qualified"):
                raise
            result = {"status": "unavailable", "reason": type(exc).__name__}
        if backend_policy.get("qualified") and result.get("status") == "unavailable":
            backend_policy["degraded"] = result.get("reason", "local_backend_unavailable")
            result = precise_discover(snapshot, options.get("query"), backend="native", **request)
            backend = "native"
        if backend_policy:
            result["backend_policy"] = {**backend_policy, "effective_backend": backend}
        for candidate in result.get("candidates", []):
            if candidate.get("kind") == "pending_capture":
                candidate.update(status="pending_formal_review", formal_claim_authority=False)
        return snapshot.finish(result)
    if operation == "catalog" and not selector:
        docs = [d for d in [*snapshot.documents(), *pending["documents"]] if _allowed(d, years, allowed)]
        offset, limit = options.get("offset", 0), options.get("limit", 50)
        if type(offset) is not int or offset < 0 or type(limit) is not int or not 1 <= limit <= 100:
            raise MemoryAccessError("catalog_page_invalid")
        return snapshot.finish({"operation": operation, "status": "available", "entities": docs[offset:offset + limit],
            "total": len(docs), "next_offset": offset + limit if offset + limit < len(docs) else None,
            "legacy_wiki_authority": snapshot.descriptor["legacy_wiki_authority"],
            "pending_index": {k: v for k, v in pending.items() if k != "documents"}})
    if operation == "catalog" and selector.get("id") in pending_by_id:
        document = pending_by_id[selector["id"]]
        return snapshot.finish({"operation": operation, "status": "available" if _allowed(document, years, allowed) else "protected",
            "entities": [document] if _allowed(document, years, allowed) else [],
            "pending_index": {k: v for k, v in pending.items() if k != "documents"}})
    entities, routed = _entities(snapshot, selector)
    entities = [e for e in entities if _allowed(e, years, allowed)]
    if not entities:
        return snapshot.finish({"operation": operation, "status": "not_found", "absence_meaning": "not_retrieved_not_proof_of_no_record", "missing": routed.get("missing_points", []), "ambiguous": routed.get("ambiguous_terms", [])})
    if operation == "catalog" and view == "protected" and (selector.get("current_question_id") or EXAM_RE.fullmatch(selector.get("id", ""))):
        return snapshot.finish({"operation": operation, "status": "protected", "entities": [], "reason": "current_question_concept_routes_withheld"})
    if operation == "catalog":
        fields = ("id", "kind", "label", "owner", "authority", "views", "storage", "source_refs", "dependencies", "profile_status", "profile_version", "learning_path")
        return snapshot.finish({"operation": operation, "status": "available", "entities": [{k: e[k] for k in fields if k in e} for e in entities]})
    if view == "protected":
        return snapshot.finish({"operation": operation, "status": "protected", "content_fields_redacted": True,
            "identities": [e["id"] for e in entities], "reason": "protected_view_withholds_personal_evidence_and_relations"})
    if operation == "relations":
        values = {}
        unknown_endpoint_count = 0
        narrow = _narrow_question(selector)
        for entity in entities:
            for row in snapshot.read(entity["relations_object"])["relations"]:
                endpoints = [snapshot.entity(row[k]) for k in ("source", "target")]
                if (years or allowed is not None or narrow) and any(e is None for e in endpoints):
                    unknown_endpoint_count += 1
                    continue
                if any(e is not None and not _allowed(e, years, allowed) for e in endpoints):
                    continue
                if narrow:
                    question_endpoints = [e.get("canonical_exam_id") or e["id"] for e in endpoints
                        if e and e["kind"] in {"exam_question", "formal_question", "question"}]
                    if any(q != narrow for q in question_endpoints):
                        continue
                values[_sha(row)] = row
        # Exact Pxx scope never expands to sibling question answers/identities.
        selected_q = selector.get("current_question_id") or selector.get("id", "")
        if re.fullmatch(r"EXAM408-\d{4}-Q\d{2}-P\d{2}", selected_q):
            values = {key: row for key, row in values.items() if all(not EXAM_RE.fullmatch(row[k]) or row[k] == selected_q for k in ("source", "target"))}
        returned = list(values.values())
        if view != "direct":
            returned = [{k: v for k, v in row.items() if k != "reason"} for row in returned]
        return snapshot.finish({"operation": operation, "status": "available", "relations": returned,
            "coverage": snapshot.descriptor["relations_coverage"], "scope_unverified_endpoint_count": unknown_endpoint_count,
            "recommendation_eligibility_checked": False})
    if operation == "preferences":
        if _narrow_question(selector) or years:
            return snapshot.finish({"operation": operation, "status": "insufficient_evidence", "preferences": [],
                "reason": "preference_source_question_scope_unverified", "complete": False})
        result, stale_sources = [], []
        for entity in entities:
            reference = entity.get("evidence_object") or entity.get("profile_object")
            if not reference:
                continue
            owner = snapshot.entity(entity["knowledge_point"]) if entity["kind"] == "knowledge_subpoint" else entity
            status = (owner or {}).get("profile_status")
            if status != "current":
                stale_sources.append({"scope": entity["id"], "profile_status": status or "unknown",
                    "source_object": reference, "reason": "personal_preference_source_not_current"})
                continue
            profile = snapshot.read(reference)
            feedback = (profile.get("teaching_plan") or {}).get("visual_feedback")
            if feedback:
                sources = (profile.get("teaching_plan") or {}).get("source_refs", [])
                if view != "direct":
                    feedback = {k: feedback[k] for k in ("scope", "form", "preference") if k in feedback}
                    sources = [{k: s[k] for k in ("id", "path", "sha256", "review_state") if k in s} for s in sources]
                result.append({"scope": entity["id"], "kind": "teaching_preference", "feedback": feedback, "source_refs": sources, "source_object": reference})
            for claim in profile.get("claims", []):
                if claim.get("kind") == "preference" and claim.get("lifecycle") == "current":
                    if view != "direct":
                        claim = {k: v for k, v in claim.items() if k != "source_refs"}
                    result.append({"scope": entity["id"], "claim": claim, "source_object": reference})
        return snapshot.finish({"operation": operation,
            "status": ("partial" if result else "insufficient_evidence") if stale_sources else "available" if result else "no_record",
            "preferences": result, "stale_sources": stale_sources, "complete": not stale_sources,
            "absence_meaning": "source_not_current_not_absence" if stale_sources else "no_scoped_explicit_preference_in_selected_native_objects", "global_instructions_owner": "AGENTS_and_installed_Skills",
            "source_quotes_redacted": view != "direct"})
    if operation == "lint":
        issues = []
        for entity in entities:
            for reference in entity.get("dependencies", []):
                snapshot.read(reference)
            if entity.get("profile_status") == "stale":
                issues.append({"id": entity["id"], "code": "personal_profile_stale"})
            if entity.get("profile_object"):
                profile = snapshot.read(entity["profile_object"])
                if not profile.get("claims"):
                    issues.append({"id": entity["id"], "code": "legacy_unstructured_claims", "severity": "information"})
        return snapshot.finish({"operation": operation, "status": "checked", "issues": issues, "scope": [e["id"] for e in entities], "repair_write_count": 0})
    rows, annotations = _point_rows(snapshot, entities)
    rows, scope_unknown = _record_scope(snapshot, rows, entities, selector, years, allowed)
    annotations = _annotation_scope(snapshot, annotations, selector, years)
    if operation == "trace":
        claim_id = options.get("claim_id")
        if claim_id:
            if _narrow_question(selector) or years:
                return snapshot.finish({"operation": operation, "status": "insufficient_evidence", "claims": [],
                    "reason": "claim_source_question_scope_unverified", "complete": False})
            found = []
            for entity in entities:
                reference = entity.get("evidence_object") or entity.get("profile_object")
                if reference:
                    profile = snapshot.read(reference)
                    found.extend({"scope": entity["id"], "claim": c, "source_object": reference,
                        "profile_status": entity.get("profile_status", "inherited_parent_state")}
                        for c in profile.get("claims", []) if c.get("claim_id") == claim_id)
            if view != "direct":
                safe_claim_fields = {"claim_id", "kind", "basis", "evidence_status", "lifecycle", "scope",
                    "event_at", "valid_from", "valid_to", "recorded_at", "claim_sha256"}
                found = [{**row, "claim": {k: v for k, v in row["claim"].items() if k in safe_claim_fields}} for row in found]
            return snapshot.finish({"operation": operation, "status": "available" if found else "not_found", "claims": found,
                "absence_meaning": "claim_not_in_selected_scope", "current_claim_inferred": False})
        key = options.get("record_key")
        matches = [r for r in rows if r["record_key"] == key]
        if not matches:
            return snapshot.finish({"operation": operation, "status": "not_found", "absence_meaning": "record_not_in_selected_scope", "records": []})
        row = matches[0]
        result = {"operation": operation, "status": "available", "records": [row],
            "source_read_required_for_verbatim": True, "correction_refs": row.get("correction_refs", [])}
        return snapshot.finish(_metadata_records(result) if view != "direct" else result)
    temporal_options = {name: options.get(name) for name in ("event_after", "event_before", "known_before")}
    if operation == "compare":
        left = options.get("left") or {}
        right = options.get("right") or {}
        if not isinstance(left, dict) or not isinstance(right, dict) or set(left) - set(temporal_options) or set(right) - set(temporal_options):
            raise MemoryAccessError("comparison_period_invalid")
        ar, au = _historical_formal_rows(snapshot, rows, annotations, left.get("known_before"))
        br, bu = _historical_formal_rows(snapshot, rows, annotations, right.get("known_before"))
        ar, ascope = _record_scope(snapshot, ar, entities, selector, years, allowed)
        br, bscope = _record_scope(snapshot, br, entities, selector, years, allowed)
        a, b = _time_projection(ar, **left), _time_projection(br, **right)
        a["unavailable_revisions"].extend(au)
        b["unavailable_revisions"].extend(bu)
        a["complete"] = a["complete"] and not au
        b["complete"] = b["complete"] and not bu
        a["scope_unverified"] = scope_unknown + ascope
        b["scope_unverified"] = scope_unknown + bscope
        a["complete"] = a["complete"] and not a["scope_unverified"]
        b["complete"] = b["complete"] and not b["scope_unverified"]
        akeys, bkeys = {r["record_key"] for r in a["records"]}, {r["record_key"] for r in b["records"]}
        return snapshot.finish({"operation": operation, "status": "available", "left": _metadata_records(a) if view != "direct" else a, "right": _metadata_records(b) if view != "direct" else b,
            "comparison": {"only_left": sorted(akeys - bkeys), "only_right": sorted(bkeys - akeys), "shared": sorted(akeys & bkeys)}, "mastery_change_inferred": False})
    rows, unavailable = _historical_formal_rows(snapshot, rows, annotations, temporal_options.get("known_before"))
    rows, replay_scope_unknown = _record_scope(snapshot, rows, entities, selector, years, allowed)
    projected = _time_projection(rows, **temporal_options)
    projected["unavailable_revisions"].extend(unavailable)
    projected["complete"] = projected["complete"] and not unavailable
    projected["scope_unverified"] = scope_unknown + replay_scope_unknown
    projected["complete"] = projected["complete"] and not projected["scope_unverified"]
    if view != "direct":
        projected = _metadata_records(projected)
    return snapshot.finish({"operation": operation, "status": "available" if projected["records"] else "insufficient_evidence" if projected["unknown_time"] or projected["unavailable_revisions"] or projected["scope_unverified"] else "no_record",
        **projected, "annotation_revisions": list(annotations.values()),
        "absence_meaning": "no_matching_observation_not_proof_of_mastery", "current_profile_reused_as_historical": False})


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("operation", choices=("catalog", "context", "history", "compare", "trace", "relations", "preferences", "discover", "lint", "coverage"))
    parser.add_argument("--repo", default=".")
    parser.add_argument("--id")
    parser.add_argument("--concept")
    parser.add_argument("--formal-id")
    parser.add_argument("--source-id")
    parser.add_argument("--current-question-id")
    parser.add_argument("--question-part")
    parser.add_argument("--mechanism", action="append", default=[])
    parser.add_argument("--view", choices=sorted(VIEWS), default="protected")
    parser.add_argument("--output-root")
    parser.add_argument("--query")
    parser.add_argument("--backend", default="native")
    parser.add_argument("--intent", choices=("auto", "concept", "experience"), default="auto")
    parser.add_argument("--actor", choices=("user", "assistant", "any"), default="user")
    parser.add_argument("--latest", action=argparse.BooleanOptionalAction, default=None)
    parser.add_argument("--limit", type=int)
    parser.add_argument("--offset", type=int, default=0)
    parser.add_argument("--expected-source-version")
    parser.add_argument("--evidence-gap")
    parser.add_argument("--allowed-id", action="append")
    parser.add_argument("--exclude-year", action="append", type=int, default=[])
    parser.add_argument("--include-pending", action="store_true")
    parser.add_argument("--event-after")
    parser.add_argument("--event-before")
    parser.add_argument("--known-before")
    parser.add_argument("--left-json")
    parser.add_argument("--right-json")
    parser.add_argument("--record-key")
    parser.add_argument("--claim-id")
    parser.add_argument("--known-version")
    parser.add_argument("--materials", choices=("all", "core", "personal", "visual"), default="core")
    args = parser.parse_args(argv)
    selector = {key: getattr(args, key) for key in ("id", "concept", "formal_id", "source_id", "current_question_id", "question_part") if getattr(args, key)}
    if args.mechanism:
        selector["mechanisms"] = args.mechanism
    options = {key: getattr(args, key) for key in ("query", "backend", "intent", "actor", "latest", "offset", "expected_source_version", "evidence_gap", "event_after", "event_before", "known_before", "record_key", "claim_id", "known_version", "materials") if getattr(args, key) is not None}
    if args.limit is not None:
        options["limit"] = args.limit
    options.update(allowed_ids=args.allowed_id, excluded_years=args.exclude_year, include_pending=args.include_pending)
    try:
        if args.left_json:
            options["left"] = json.loads(args.left_json)
        if args.right_json:
            options["right"] = json.loads(args.right_json)
        result = access(args.repo, args.operation, selector, view=args.view, output_root=args.output_root, **options)
    except (MemoryAccessError, ValueError, OSError, KeyError, RuntimeError) as exc:
        print(json.dumps({"schema": SCHEMA, "status": "unavailable", "reason": str(exc), "formal_write_count": 0, "learner_evidence_write_count": 0}, ensure_ascii=False))
        return 1
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
