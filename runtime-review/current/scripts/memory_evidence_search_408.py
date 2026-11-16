"""Private, source-bound discovery of current 408 learning observations.

Cold construction consumes verified point-record objects, not raw conversations.
Queries open scope metadata and only the postings needed by the query. They load
body buckets for at most eight selected source fragments. Direct reads may
return source summaries. After-attempt historical discovery uses postings and
scope metadata only, never bodies or answer-bearing output; a bound protected
current question and protected views refuse that lookup altogether.
"""
from __future__ import annotations

import copy
import datetime as dt
import hashlib
import json
import re
from collections import Counter, defaultdict
from typing import Any, Callable, Mapping
from zoneinfo import ZoneInfo


SCHEMA = "cs408-private-evidence-search-v1"
DESCRIPTOR_SCHEMA = "cs408-private-evidence-index-v1"
SCOPE_SCHEMA = "cs408-private-evidence-scope-v1"
BODY_SCHEMA = "cs408-private-evidence-bucket-v1"
POSTINGS_SCHEMA = "cs408-private-evidence-postings-v1"
MAX_OBJECT_BYTES = 16 * 1024 * 1024
MAX_RESULTS = 8
MAX_RANK_CANDIDATES = 256
SHA_RE = re.compile(r"[a-f0-9]{64}$")
OBJECT_RE = re.compile(r"objects/[a-z-]+/([a-f0-9]{64})\.json$")
EXAM_RE = re.compile(r"EXAM408-(\d{4})-Q\d{2}(?:-P\d{2,3})?$")
FORMAL_RE = re.compile(r"(?:DS|CO|OS|CN)_[A-Z0-9_]+$")
PART_RE = re.compile(r"EXAM408-\d{4}-Q\d{2}-P\d{2,3}$")
POINT_RE = re.compile(r"(?:DS|CO|OS|CN)\d{2}-\d{2}$")
LEARNER_ROLES = {"learner_report", "observed_learner_evidence", "direct_observation",
    "observed_understanding", "user_report", "visible_evidence", "direct", "repair"}
ASSISTANT_ROLES = {"assistant_explanation", "hint"}
EDITORIAL_ROLES = {"formal_summary", "review_summary", "historical_source_summary", "source_summary"}
ROLE_LABELS = {"user_performance": "用户作答", "user_statement": "用户陈述",
               "assistant_instruction": "助手讲解", "editorial_summary": "整理摘要", "unknown": "来源角色未分类"}


class EvidenceSearchError(ValueError):
    pass


def _bytes(value):
    return (json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n").encode()


def _sha(value):
    return hashlib.sha256(_bytes(value)).hexdigest()


def _checked(reference, read_object):
    if (not isinstance(reference, Mapping) or not isinstance(reference.get("ref"), str)
            or not (match := OBJECT_RE.fullmatch(reference["ref"]))
            or reference.get("sha256") != match[1]):
        raise EvidenceSearchError("evidence_reference_invalid")
    value = read_object(reference)
    if not isinstance(value, dict):
        raise EvidenceSearchError("evidence_object_required")
    raw = _bytes(value)
    if len(raw) > MAX_OBJECT_BYTES:
        raise EvidenceSearchError("evidence_artifact_exceeds_16_mib")
    if hashlib.sha256(raw).hexdigest() != reference["sha256"]:
        raise EvidenceSearchError("evidence_source_hash_mismatch")
    return value


def _strings(value):
    return sorted({x for x in (value or []) if isinstance(x, str) and x}) if isinstance(value, (list, tuple, set)) else []


def _role(row):
    role, kind = row.get("evidence_role"), row.get("event_kind")
    if role in ASSISTANT_ROLES or kind in {"hint", "prompt", "explanation"}:
        return "assistant_instruction"
    if role in LEARNER_ROLES:
        return "user_performance" if kind in {"outcome", "repair"} else "user_statement"
    if role in EDITORIAL_ROLES:
        return "editorial_summary"
    return "unknown"


def _question_scope(row, entities):
    questions, unresolved = set(), set()
    for name in [row.get("source_id"), row.get("source_question_id"), *row.get("formal_ids", [])]:
        if not isinstance(name, str):
            continue
        if EXAM_RE.fullmatch(name):
            questions.add(name)
        elif FORMAL_RE.fullmatch(name):
            canonical = (entities.get(name) or {}).get("canonical_exam_id")
            if isinstance(canonical, str) and EXAM_RE.fullmatch(canonical):
                questions.add(canonical)
            else:
                unresolved.add(name)
    dependencies, unresolved_dependencies = set(), set()
    for identity in questions:
        for dependency in (entities.get(identity) or {}).get("contact_dependency_ids", []):
            if isinstance(dependency, str) and EXAM_RE.fullmatch(dependency):
                dependencies.add(dependency)
            elif isinstance(dependency, str) and FORMAL_RE.fullmatch(dependency):
                canonical = (entities.get(dependency) or {}).get("canonical_exam_id")
                if isinstance(canonical, str) and EXAM_RE.fullmatch(canonical):
                    dependencies.add(canonical)
                else:
                    unresolved_dependencies.add(dependency)
            else:
                unresolved_dependencies.add(str(dependency))
    return {"question_ids": sorted(questions), "unresolved_formal_ids": sorted(unresolved),
            "contact_dependency_ids": sorted(dependencies), "unresolved_dependency_ids": sorted(unresolved_dependencies)}


def _point_record_refs(point_artifacts, read):
    index = point_artifacts["index"]
    refs = index.get("point_records")
    if isinstance(refs, dict):
        return refs
    # Older published fixtures/layouts bind records through the point history.
    result = {}
    for code, entry in index.get("points", {}).items():
        summary = read({"ref": entry["ref"], "sha256": entry["sha256"]})
        history = read(summary["history_object"])
        result[code] = history["records_object"]
    return result


def _build_documents(point_artifacts, entities=None, source_resolver=None):
    entities = entities or {}
    objects = point_artifacts.get("objects") or {}
    def read(reference):
        return _checked(reference, lambda ref: objects[ref["ref"]])
    groups, skipped = {}, Counter()
    total_rows = 0
    for code, reference in sorted(_point_record_refs(point_artifacts, read).items()):
        owner = read(reference)
        if owner.get("knowledge_point") != code or not isinstance(owner.get("records"), dict):
            raise EvidenceSearchError("evidence_point_record_identity_mismatch")
        for key, original in sorted(owner["records"].items()):
            total_rows += 1
            if original.get("record_key") != key or original.get("knowledge_point") != code:
                raise EvidenceSearchError("evidence_record_identity_mismatch")
            lifecycle = original.get("semantic_lifecycle_status", original.get("lifecycle_status", original.get("lifecycle", "active")))
            if lifecycle not in {None, "", "active", "current"}:
                skipped["inactive_semantic_interpretation"] += 1
                continue
            summary = original.get("effective_summary", original.get("summary"))
            if not isinstance(summary, str) or not summary.strip():
                skipped["no_summary"] += 1
                continue
            source_id, sha, fragment = (original.get(k) for k in ("source_id", "source_sha256", "source_fragment"))
            if (not isinstance(source_id, str) or not source_id
                    or not isinstance(fragment, str) or not fragment
                    or not isinstance(sha, str) or not SHA_RE.fullmatch(sha)):
                skipped["incomplete_source_identity"] += 1
                continue
            identity = "EV408-" + _sha([source_id, sha, fragment])
            row = {k: copy.deepcopy(original[k]) for k in (
                "record_key", "knowledge_point", "source_kind", "source_id", "source_locator", "source_sha256",
                "source_fragment", "source_order", "source_question_id", "formal_ids", "evidence_role", "event_kind",
                "point_binding", "observed_date", "event_time", "recorded_at", "prompt_dependency", "outcome",
                "correction_result", "evidence_refs", "original_turn_refs", "correction_refs", "source_available",
                "source_access", "semantic_annotation_sha256", "semantic_correction_sha256") if k in original}
            if "effective_observed_date" in original:
                row.update(stored_observed_date=row.get("observed_date"), observed_date=original["effective_observed_date"])
            if "effective_outcome" in original:
                row.update(stored_outcome=row.get("outcome"), outcome=original["effective_outcome"])
            row.update(summary=summary, role_category=_role(original), owner_object=copy.deepcopy(reference),
                       formal_ids=_strings(original.get("formal_ids")), **_question_scope(original, entities))
            if source_resolver is not None:
                proof = source_resolver.resolve(original)
                if proof.get("conflict"):
                    skipped["conflicting_original_source"] += 1
                    continue
                if proof.get("status") == "verified":
                    scope = proof["scope_proof"]
                    row["episode_scope_proof"] = copy.deepcopy(scope)
                    row["question_ids"] = scope["question_ids"]
                    row["contact_dependency_ids"] = scope["contact_dependency_ids"]
                    row["unresolved_formal_ids"] = []
                    row["unresolved_dependency_ids"] = []
                    if proof.get("original_turn_usable"):
                        row["original_turn"] = proof["original_turn"]
            document = groups.setdefault(identity, {"id": identity, "kind": "evidence",
                "source_id": source_id, "source_sha256": sha, "source_fragment": fragment, "projections": []})
            document["projections"].append(row)
    documents = []
    for identity, document in sorted(groups.items()):
        projections = document["projections"]
        document.update(title="原始学习记录", aliases=[document["source_id"]],
            fields={"summary": "\n".join(dict.fromkeys(p["summary"] for p in projections))},
            source_refs=[{"source_id": document["source_id"], "source_sha256": document["source_sha256"],
                          "source_fragment": document["source_fragment"]}],
            scope={"point_ids": sorted({p["knowledge_point"] for p in projections}),
                   "record_keys": [p["record_key"] for p in projections]})
        documents.append(document)
    return documents, {"projected_record_count": total_rows, "document_count": len(documents),
                       "searchable_projection_count": sum(len(d["projections"]) for d in documents),
                       "excluded_records": dict(skipped), "coverage": "current_owned_summary_and_verified_original_turns",
                       "original_turn_projection_count": sum("original_turn" in p for d in documents for p in d["projections"])}


def build_evidence_documents(point_artifacts, *, entities=None):
    """Cold-only complete private documents; never mix these into public discovery."""
    return _build_documents(point_artifacts, entities)[0]


def _unit_signature(row):
    # Different content/actors/times/question ownership get separate postings,
    # so filtering one projection never ranks another projection's private text.
    keys = ("knowledge_point", "summary", "evidence_role", "event_kind", "role_category", "prompt_dependency", "outcome",
            "correction_result", "observed_date", "event_time", "recorded_at", "point_binding", "formal_ids",
            "question_ids", "unresolved_formal_ids", "contact_dependency_ids", "unresolved_dependency_ids", "semantic_correction_sha256", "correction_refs")
    return {key: row.get(key) for key in keys}


def build_evidence_index(point_artifacts, *, entities=None, source_version=None, source_resolver=None):
    """Return content-addressed private metadata, postings and body buckets."""
    from memory_retrieval_408 import compile_index
    entities = entities or {}
    documents, statistics = _build_documents(point_artifacts, entities, source_resolver)
    objects, scopes, statement_units, bodies, units = {}, {}, {}, defaultdict(dict), []
    record_scopes = defaultdict(dict)
    def put(directory, value):
        raw = _bytes(value)
        if len(raw) > MAX_OBJECT_BYTES:
            raise EvidenceSearchError("evidence_artifact_exceeds_16_mib:" + directory)
        sha = hashlib.sha256(raw).hexdigest()
        ref = f"objects/{directory}/{sha}.json"
        objects[ref] = value
        return {"ref": ref, "sha256": sha}
    for document in documents:
        bodies[document["id"][6:8]][document["id"]] = document
        variants = defaultdict(list)
        for row in document["projections"]:
            if row.get("episode_scope_proof"):
                prefix = hashlib.sha256(row["record_key"].encode()).hexdigest()[:2]
                record_scopes[prefix][row["record_key"]] = {"record_key": row["record_key"],
                    "knowledge_point": row["knowledge_point"], "source_id": row["source_id"],
                    "source_sha256": row["source_sha256"], "source_fragment": row["source_fragment"],
                    "scope_proof": row["episode_scope_proof"]}
            variants[_sha(_unit_signature(row))].append(row)
        for signature, projections in sorted(variants.items()):
            first = projections[0]
            unit_id = "EVU408-" + _sha([document["id"], signature])
            title = (first.get("observed_date") or "日期未记录") + " · " + ROLE_LABELS[first["role_category"]]
            # A retrieval unit is point-scoped. The whole source fragment is
            # deduplicated only after ranking, so another point's terms cannot
            # enter a restricted query through a shared source fragment.
            point = entities.get(first["knowledge_point"]) or {}
            units.append({"id": unit_id, "kind": "evidence", "title": title,
                "family": document["id"],
                "aliases": [document["source_id"]], "fields": {"summary": first["summary"],
                    "mechanism": point.get("label", first["knowledge_point"]),
                    "terms": _strings(point.get("aliases"))}, "source_refs": []})
            scopes[unit_id] = {"document_id": document["id"], "source_id": document["source_id"],
                "source_sha256": document["source_sha256"], "source_fragment": document["source_fragment"],
                "role_category": first["role_category"], "evidence_role": first.get("evidence_role"),
                "event_kind": first.get("event_kind"), "prompt_dependency": first.get("prompt_dependency"),
                "observed_date": first.get("observed_date"),
                "point_binding": first.get("point_binding"),
                "source_order": first.get("source_order"),
                "event_time": first.get("event_time"), "recorded_at": first.get("recorded_at"),
                "formal_ids": first["formal_ids"], "question_ids": first["question_ids"],
                "unresolved_formal_ids": first["unresolved_formal_ids"],
                "contact_dependency_ids": first["contact_dependency_ids"],
                "unresolved_dependency_ids": first["unresolved_dependency_ids"],
                "projections": [{"record_key": p["record_key"], "knowledge_point": p["knowledge_point"]} for p in projections]}
            # A verified original turn is an independent retrieval field, not
            # a replacement interpretation. It is never used in point/Pxx
            # restricted search: one whole turn may discuss sibling points.
            if first.get("original_turn"):
                turn = first["original_turn"]
                statement_id = "EVU408-" + _sha([document["id"], signature, "original_turn", turn["turn_sha256"]])
                units.append({"id": statement_id, "kind": "evidence", "title": title,
                    "family": document["id"], "aliases": [], "fields": {"statement": turn["text"]}, "source_refs": []})
                mentioned = re.findall(r"EXAM408-\d{4}-Q\d{2}(?:-P\d{2,3})?|(?:DS|CO|OS|CN)_(?:\d{4}|UNK)_\d{3}", turn["text"], re.I)
                mention_scope = _question_scope({"formal_ids": [identity.upper() for identity in mentioned]}, entities)
                statement_units[statement_id] = {"summary_unit_id": unit_id, "original_turn_sha256": turn["turn_sha256"],
                    "mentioned_question_ids": mention_scope["question_ids"] + mention_scope["contact_dependency_ids"],
                    "unresolved_mentions": mention_scope["unresolved_formal_ids"] + mention_scope["unresolved_dependency_ids"]}
    compiled = compile_index(units, include_documents=False, shard_count=256)
    postings = compiled.pop("postings_shards", {})
    if not isinstance(postings, dict):
        raise EvidenceSearchError("evidence_postings_layout_invalid")
    posting_refs = {}
    for prefix, shard in sorted(postings.items()):
        if not re.fullmatch(r"[a-f0-9]{2}", prefix):
            raise EvidenceSearchError("evidence_postings_prefix_invalid")
        posting_refs[prefix] = put("memory-evidence-postings", {"schema": POSTINGS_SCHEMA,
            "prefix": prefix, "postings": shard})
    descriptor = {"schema": DESCRIPTOR_SCHEMA, "scope_object": put("memory-evidence-scope", {
        "schema": SCOPE_SCHEMA, "units": scopes, "original_turn_units": statement_units}),
        "lexical_object": put("memory-evidence-lexical", compiled), "postings": posting_refs,
        "body_buckets": {prefix: put("memory-evidence-bodies", {"schema": BODY_SCHEMA,
            "prefix": prefix, "documents": values}) for prefix, values in sorted(bodies.items())},
        "source_version": source_version, "document_count": len(documents), "unit_count": len(units),
        "views": ["after_attempt", "direct"], "body_views": ["direct"],
        "after_attempt_policy": "unbound_history_locator_only_no_body_load",
        "allowed_purposes": ["history_lookup", "teacher"],
        "summary_coverage": statistics["coverage"], "formal_write_count": 0, "learner_evidence_write_count": 0}
    descriptor["record_scope_buckets"] = {prefix: put("memory-record-source-scopes", {
        "schema": "cs408-record-source-scopes-v1", "prefix": prefix, "records": records})
        for prefix, records in sorted(record_scopes.items())}
    if source_resolver is not None:
        guards = source_resolver.dependency_guards()
        descriptor["source_dependencies_object"] = put("memory-evidence-source-dependencies", {
            "schema": "cs408-evidence-source-dependencies-v1", "guards": guards})
        statistics["original_source_verification"] = source_resolver.statistics()
    sizes = [len(_bytes(value)) for value in objects.values()]
    statistics.update(unit_count=len(units), artifact_count=len(objects), total_artifact_bytes=sum(sizes),
        max_artifact_bytes=max(sizes, default=0), scope_bytes=len(_bytes(objects[descriptor["scope_object"]["ref"]])),
        lexical_bytes=len(_bytes(compiled)), body_bucket_count=len(descriptor["body_buckets"]),
        postings_bucket_count=len(posting_refs))
    return {"descriptor": descriptor, "objects": objects, "statistics": statistics}


def _timestamp(value, *, end=False):
    if value is None:
        return None
    if not isinstance(value, str):
        raise EvidenceSearchError("evidence_date_invalid")
    try:
        if re.fullmatch(r"\d{4}-\d{2}-\d{2}", value):
            day = dt.date.fromisoformat(value)
            return dt.datetime.combine(day, dt.time.max if end else dt.time.min, ZoneInfo("Asia/Shanghai"))
        stamp = dt.datetime.fromisoformat(value.replace("Z", "+00:00"))
        return stamp.replace(tzinfo=ZoneInfo("Asia/Shanghai")) if stamp.tzinfo is None else stamp
    except ValueError as exc:
        raise EvidenceSearchError("evidence_date_invalid") from exc


def _event_interval(row):
    value = row.get("observed_date") or row.get("event_time")
    if not value:
        return None, None
    try:
        return _timestamp(value), _timestamp(value, end=True)
    except EvidenceSearchError:
        return None, None


def _sets(values, label):
    if values is None:
        return None
    if not isinstance(values, (list, tuple, set)) or any(not isinstance(v, str) or not v for v in values):
        raise EvidenceSearchError(label + "_invalid")
    return set(values)


def _content_query(query):
    # Retrieval-stage exclusions are controls, not positive content terms.
    # Scientific negatives (e.g. 没有翻转/不需要刷新) remain untouched.
    query = re.sub(r"(?:^|[，,；;。])\s*(?:不是|不要|排除|不找|而非)(?:后来|后面|随后|助手|老师|我的首答)[^，,；;。]*", " ", query)
    return re.sub(r"我(?:上次|最近一次|之前|以前|曾经)|(?:最近|最后)一次|请(?:帮我)?(?:找|查)(?:一下)?|还记得|"
                  r"(?:请)?(?:定位|找出|找到|查找)|(?:原始|原来|当时|那次|那段|这次|这个|第一段|开头|首答)(?:的)?|"
                  r"(?:作答|回答|记录|判断|思路|推理|解释|说明|复述)(?:记录)?", " ", query).strip()


def _requested_stage(query):
    content = re.split(r"[，,；;。]\s*(?:不是|不要|排除|不找|而非)(?:后来|后面|随后|助手|老师)", query)[0]
    if re.search(r"首答|原始作答|第一段|开头|初答", content):
        return "initial"
    if re.search(r"(?:要|找|查|定位).{0,6}(?:我|用户).{0,4}(?:修复|改正|纠正后)|讲解(?:以)?后.*(?:算清|答对|改正)", content):
        return "repair"
    return None


def _snippet(text, terms, maximum=320):
    if len(text) <= maximum:
        return {"text": text, "start": 0, "end": len(text), "truncated": False}
    folded = text.casefold()
    words = sorted({t.casefold() for t in terms if isinstance(t, str) and len(t) >= 2}, key=lambda t: (-len(t), t))[:48]
    positions = {0}
    for word in words:
        start = 0
        for _ in range(100):
            pos = folded.find(word, start)
            if pos < 0:
                break
            positions.add(max(0, min(len(text) - maximum, pos - maximum // 3)))
            start = pos + max(1, len(word))
    def score(start):
        window = folded[start:start + maximum]
        return sum(len(word) ** 2 / max(1, folded.count(word)) for word in words if word in window)
    start = max(positions, key=lambda pos: (score(pos), -pos))
    return {"text": text[start:start + maximum], "start": start, "end": start + maximum, "truncated": True}


def search_experiences(query, index_or_loader, *, intent="experience", view="protected", purpose="history_lookup",
                       limit=8, read_object=None, current_question_id=None, excluded_years=None, allowed_ids=None,
                       point_ids=None, record_keys=None, source_ids=None, actor="user", event_after=None, event_before=None,
                       known_before=None, latest=None, ranker=None):
    """Find exact personal records; no private load/ranking before authorization."""
    base = {"schema": SCHEMA, "intent": intent, "coverage": "current_owned_summary_not_full_conversation",
            "formal_write_count": 0, "learner_evidence_write_count": 0, "independent_mastery_inferred": False}
    if intent != "experience":
        raise EvidenceSearchError("evidence_intent_required")
    if view not in {"protected", "after_attempt", "direct"}:
        raise EvidenceSearchError("evidence_view_invalid")
    metadata_only = view == "after_attempt"
    if view == "protected" or (metadata_only and (current_question_id or purpose != "history_lookup")):
        return {**base, "status": "withheld", "candidates": [], "scope": "metadata_only",
                "reason": "protected_current_question_or_unapproved_history_lookup", "private_artifact_reads": 0}
    if purpose not in {"history_lookup", "teacher"}:
        raise EvidenceSearchError("evidence_purpose_invalid")
    if not isinstance(query, str) or not query.strip() or len(query) > 1000:
        raise EvidenceSearchError("evidence_query_invalid")
    identity_text = query.strip().casefold()
    typed_identity = bool(re.match(r"^(?:evu?408|klr)(?:-|$)", identity_text))
    if typed_identity and not re.fullmatch(r"(?:evu?408|klr)-[a-z0-9-]+", identity_text):
        return {**base, "status": "no_match", "candidates": [], "reason": "invalid_exact_evidence_identity",
                "private_artifact_reads": 0}
    if type(limit) is not int or not 1 <= limit <= MAX_RESULTS:
        raise EvidenceSearchError("evidence_limit_invalid")
    if actor not in {"user", "assistant", "editorial", "all", "any"}:
        raise EvidenceSearchError("evidence_actor_invalid")
    if latest is not None and not isinstance(latest, bool):
        raise EvidenceSearchError("evidence_latest_invalid")
    lower, upper = _timestamp(event_after), _timestamp(event_before, end=True)
    if lower and upper and lower > upper:
        raise EvidenceSearchError("evidence_date_range_inverted")
    if known_before is not None:
        _timestamp(known_before, end=True)
        return {**base, "status": "history_required", "candidates": [], "known_before": known_before,
                "reason": "past_interpretations_require_existing_versioned_history_reader", "private_artifact_reads": 0}
    allowed, selected_points, selected_keys = (_sets(allowed_ids, "allowed_ids"), _sets(point_ids, "point_ids"), _sets(record_keys, "record_keys"))
    selected_sources = _sets(source_ids, "source_ids")
    years = excluded_years or []
    if not isinstance(years, (list, tuple, set)) or any(type(y) is not int or y < 2009 for y in years):
        raise EvidenceSearchError("excluded_years_invalid")
    years = set(years)
    if current_question_id is not None and (not isinstance(current_question_id, str) or not EXAM_RE.fullmatch(current_question_id)):
        raise EvidenceSearchError("evidence_current_question_identity_invalid")
    part = current_question_id if current_question_id and PART_RE.fullmatch(current_question_id) else None
    envelope = index_or_loader() if callable(index_or_loader) else index_or_loader
    descriptor = envelope.get("descriptor", envelope) if isinstance(envelope, dict) else None
    if not isinstance(descriptor, dict) or descriptor.get("schema") != DESCRIPTOR_SCHEMA:
        raise EvidenceSearchError("evidence_index_unavailable")
    base["coverage"] = descriptor.get("summary_coverage", base["coverage"])
    if read_object is None:
        objects = envelope.get("objects") if isinstance(envelope, dict) else None
        if not isinstance(objects, dict):
            raise EvidenceSearchError("evidence_object_reader_required")
        read_object = lambda reference: objects[reference["ref"]]
    reads = {}
    def read(reference):
        ref = reference["ref"]
        if ref not in reads:
            reads[ref] = _checked(reference, read_object)
        return reads[ref]
    metadata = read(descriptor["scope_object"])
    if metadata.get("schema") != SCOPE_SCHEMA or not isinstance(metadata.get("units"), dict):
        raise EvidenceSearchError("evidence_scope_index_invalid")
    eligible, filtered = {}, Counter()
    stage = _requested_stage(query)
    scoped_units = metadata["units"]
    if not (current_question_id or selected_points is not None or selected_keys is not None or allowed is not None):
        scoped_units = {**scoped_units}
        for identity, link in metadata.get("original_turn_units", {}).items():
            parent = metadata["units"].get(link["summary_unit_id"])
            if parent is None:
                raise EvidenceSearchError("original_turn_unit_binding_missing")
            scoped_units[identity] = {**parent, "unbound_history_only": True,
                "original_turn_sha256": link["original_turn_sha256"],
                "contact_dependency_ids": sorted(set(parent.get("contact_dependency_ids", []) + link.get("mentioned_question_ids", []))),
                "unresolved_dependency_ids": parent.get("unresolved_dependency_ids", []) + link.get("unresolved_mentions", [])}
    for identity, item in scoped_units.items():
        if item.get("unbound_history_only") and (current_question_id or selected_points is not None or selected_keys is not None or allowed is not None):
            filtered["original_turn_outside_unbound_history"] += 1
            continue
        if selected_sources is not None and item.get("source_id") not in selected_sources:
            filtered["source_identity"] += 1
            continue
        role = item.get("role_category")
        if ((actor == "user" and role not in {"user_performance", "user_statement"})
                or (actor == "assistant" and role != "assistant_instruction")
                or (actor == "editorial" and role != "editorial_summary")):
            filtered["actor"] += 1
            continue
        if ((stage == "initial" and item.get("event_kind") == "repair")
                or (stage == "repair" and item.get("event_kind") != "repair")):
            filtered["requested_event_stage"] += 1
            continue
        questions = set(item.get("question_ids") or [])
        if years or current_question_id:
            if not questions or item.get("unresolved_formal_ids") or item.get("unresolved_dependency_ids"):
                filtered["question_scope_unproven"] += 1
                continue
            if any(int(EXAM_RE.fullmatch(q)[1]) in years for q in
                   questions | set(item.get("contact_dependency_ids") or [])):
                filtered["excluded_year"] += 1
                continue
            if part and questions != {part}:
                filtered["independent_part"] += 1
                continue
            if current_question_id and not part and any(
                    q != current_question_id and not q.startswith(current_question_id + "-P") for q in questions):
                filtered["current_question_parent"] += 1
                continue
        projections = []
        for projection in item.get("projections") or []:
            point, key = projection.get("knowledge_point"), projection.get("record_key")
            if selected_points is not None and point not in selected_points:
                continue
            if selected_keys is not None and key not in selected_keys:
                continue
            if allowed is not None and not ({point, key, item.get("source_id"), *item.get("formal_ids", []), *questions} & allowed):
                continue
            projections.append(projection)
        if not projections:
            filtered["allowed_scope"] += 1
            continue
        start, end = _event_interval(item)
        if lower or upper:
            if start is None:
                filtered["event_date_unknown"] += 1
                continue
            if (lower and end < lower) or (upper and start > upper):
                filtered["event_date_outside"] += 1
                continue
            if (lower and start < lower) or (upper and end > upper):
                filtered["event_date_precision_insufficient"] += 1
                continue
        eligible[identity] = {**item, "projections": projections}
    if not eligible:
        return {**base, "status": "no_match", "candidates": [], "filtered_counts": dict(filtered),
                "absence_meaning": "no_scoped_candidate_not_proof_of_no_experience", "private_artifact_reads": len(reads)}
    def load_postings(prefix):
        reference = descriptor.get("postings", {}).get(prefix)
        if reference is None:
            return {}
        shard = read(reference)
        if shard.get("schema") != POSTINGS_SCHEMA or shard.get("prefix") != prefix:
            raise EvidenceSearchError("evidence_postings_binding_invalid")
        return shard["postings"]
    if typed_identity:
        exact_units = []
        for unit_id, item in eligible.items():
            matched_projections = [p for p in item["projections"] if p["record_key"].casefold() == identity_text]
            if (unit_id.casefold() == identity_text or item["document_id"].casefold() == identity_text
                    or matched_projections):
                if identity_text.startswith("klr-"):
                    eligible[unit_id] = {**item, "projections": matched_projections}
                exact_units.append(unit_id)
        if identity_text.startswith("klr-") and len({eligible[key]["document_id"] for key in exact_units}) > 1:
            return {**base, "status": "identity_conflict", "candidates": [],
                    "reason": "record_identity_not_unique", "private_artifact_reads": len(reads)}
        ranked = {"status": "ready" if exact_units else "no_match", "ambiguity": None,
            "candidates": [{"id": identity, "score": 1.0, "match_type": "exact_identity",
                            "matched_fields": ["identity"], "matched_terms": []} for identity in exact_units]}
    else:
        lexical = read(descriptor["lexical_object"])
        if ranker is None:
            from memory_retrieval_408 import rank_documents
            ranker = rank_documents
        ranked = ranker(_content_query(query) or query, lexical, kind="evidence", view="direct",
            allowed_ids=set(eligible), limit=MAX_RANK_CANDIDATES, load_postings=load_postings)
    matches = [dict(row) for row in ranked.get("candidates", [])
               if row.get("id") in eligible and isinstance(row.get("score"), (int, float)) and row["score"] > 0]
    # Merely sharing a question's point tag is insufficient to date a personal
    # experience about that point. Require a text match or an explicit native
    # point observation; exact source identities remain valid lookup requests.
    relevant = []
    for row in matches:
        item = eligible[row["id"]]
        if (set(row.get("matched_fields") or []) & {"summary", "statement"}
                or (item.get("point_binding") == "explicit_point"
                    and set(row.get("matched_fields") or []) & {"mechanism", "terms"})
                or row.get("match_type") in {"exact_identity", "exact_synonym"}):
            relevant.append(row)
        else:
            filtered["question_association_only_match"] += 1
    matches = relevant
    latest = bool(re.search(r"上次|最近(?:一次)?|最后(?:一次)?", query)) if latest is None else latest
    date_sources = defaultdict(set)
    for row in matches:
        item = eligible[row["id"]]
        stamp, _ = _event_interval(item)
        if stamp:
            date_sources[stamp].add(item["source_id"])
    # Recency acts only inside the ranker's semantic/lexical candidate set.
    # Undated records remain undated; a latest dated hit is never called proof
    # that no later, undated observation exists.
    def order(row):
        item = eligible[row["id"]]
        stamp, _ = _event_interval(item)
        role_priority = 0 if item.get("role_category") == "user_performance" else 1
        # Turn order is comparable only inside one original source, never
        # between two conversations that happen to share a calendar date.
        order_in_source = item.get("source_order")
        comparable_order = (order_in_source if type(order_in_source) is int
                            and stamp and len(date_sources[stamp]) == 1 else -1)
        return ((stamp is not None, stamp.timestamp() if stamp else float("-inf"), comparable_order, row["score"], -role_priority)
                if latest else (row["score"], -role_priority, stamp.timestamp() if stamp else float("-inf")))
    matches.sort(key=order, reverse=True)
    selected, seen = [], set()
    related_units = defaultdict(list)
    for match in matches:
        related_units[eligible[match["id"]]["document_id"]].append(eligible[match["id"]])
    for match in matches:
        item = eligible[match["id"]]
        source_turn = (item["source_id"], item["source_sha256"], item["source_fragment"].split("#")[0])
        duplicate_key = source_turn if item["source_fragment"].startswith("turn:") else item["document_id"]
        if duplicate_key in seen:
            continue
        seen.add(duplicate_key)
        selected.append((match, item))
        if len(selected) == limit:
            break
    candidates = []
    for match, item in selected:
        doc_id = item["document_id"]
        if metadata_only:
            projections = [p for unit in related_units[doc_id] for p in unit["projections"]]
            primary = item["projections"][0]
            trace = {"operation": "trace", "selector": {"id": primary["knowledge_point"]},
                     "view": "after_attempt", "record_key": primary["record_key"]}
            if years:
                trace["excluded_years"] = sorted(years)
            if allowed is not None:
                trace["allowed_ids"] = sorted(allowed)
            candidates.append({"id": doc_id, "kind": "evidence", "record_key": primary["record_key"],
                "record_keys": sorted({p["record_key"] for p in projections}),
                "knowledge_point": primary["knowledge_point"],
                "knowledge_points": sorted({p["knowledge_point"] for p in projections}),
                "source_id": item["source_id"], "source_sha256": item["source_sha256"],
                "source_fragment": item["source_fragment"], "observed_date": item.get("observed_date"),
                "event_time": item.get("event_time"), "recorded_at": item.get("recorded_at"),
                "role_category": item["role_category"], "evidence_role": item.get("evidence_role"),
                "event_kind": item.get("event_kind"), "prompt_dependency": item.get("prompt_dependency"),
                "formal_ids": item.get("formal_ids", []), "question_ids": item.get("question_ids", []),
                "match_type": match.get("match_type", "fielded_bm25"),
                "source_refs": [{"source_id": item["source_id"], "source_sha256": item["source_sha256"],
                                 "source_fragment": item["source_fragment"]}],
                "trace_request": trace, "content_fields_redacted": True,
                "independent_mastery_inferred": False, "point_error_inferred": False})
            continue
        prefix = doc_id[6:8]
        reference = descriptor.get("body_buckets", {}).get(prefix)
        if reference is None:
            raise EvidenceSearchError("evidence_body_bucket_missing")
        bucket = read(reference)
        if bucket.get("schema") != BODY_SCHEMA or bucket.get("prefix") != prefix:
            raise EvidenceSearchError("evidence_body_bucket_invalid")
        document = bucket.get("documents", {}).get(doc_id)
        if not isinstance(document, dict) or document.get("id") != doc_id:
            raise EvidenceSearchError("evidence_document_missing")
        keys = {p["record_key"] for unit in related_units[doc_id] for p in unit["projections"]}
        rows = [p for p in document["projections"] if p["record_key"] in keys]
        if len(rows) != len(keys) or any(p.get("source_sha256") != item["source_sha256"] for p in rows):
            raise EvidenceSearchError("evidence_scope_body_mismatch")
        primary_keys = {p["record_key"] for p in item["projections"]}
        first = next(p for p in rows if p["record_key"] in primary_keys)
        original_match = bool(item.get("unbound_history_only") and first.get("original_turn"))
        snippet_text = first["original_turn"]["text"] if original_match else first["summary"]
        snippet = _snippet(snippet_text, match.get("matched_terms") or [])
        source_refs = [{"source_kind": first.get("source_kind"), "source_id": item["source_id"],
            "source_sha256": item["source_sha256"], "source_fragment": item["source_fragment"]}]
        for row in rows:
            source_refs.append({"kind": "native_record", "record_key": row["record_key"],
                "knowledge_point": row["knowledge_point"], **row["owner_object"]})
            for correction in row.get("correction_refs") or []:
                if correction not in source_refs:
                    source_refs.append(copy.deepcopy(correction))
        if original_match:
            source_refs.append({k: v for k, v in first["original_turn"].items() if k != "text"})
        selector = {"id": first["knowledge_point"]}
        if current_question_id:
            selector["current_question_id"] = current_question_id
        trace = {"operation": "trace", "selector": selector, "view": "direct", "record_key": first["record_key"]}
        if years:
            trace["excluded_years"] = sorted(years)
        if allowed is not None:
            trace["allowed_ids"] = sorted(allowed)
        candidates.append({"id": doc_id, "kind": "evidence", "title":
            (item.get("observed_date") or "日期未记录") + " · " + ROLE_LABELS[item["role_category"]],
            "snippet": snippet["text"], "snippet_span": {k: snippet[k] for k in ("start", "end", "truncated")},
            "summary_char_count": len(first["summary"]), "snippet_kind": "verified_original_turn" if original_match else "native_record_summary",
            "score": match["score"],
            "match_type": match.get("match_type", "fielded_bm25"), "matched_fields": match.get("matched_fields", []),
            "matched_terms": match.get("matched_terms", []), "role_category": item["role_category"],
            "evidence_role": first.get("evidence_role"), "event_kind": first.get("event_kind"),
            "prompt_dependency": first.get("prompt_dependency"), "reported_outcome": first.get("outcome"),
            "source_available": first.get("source_available"), "source_access": first.get("source_access"),
            "evidence_read_required": True,
            "point_bindings": {p["knowledge_point"]: p.get("point_binding") for p in rows},
            "observed_date": item.get("observed_date"), "event_time": item.get("event_time"), "recorded_at": item.get("recorded_at"),
            "source_order": item.get("source_order"),
            "knowledge_points": sorted({p["knowledge_point"] for p in rows}), "formal_ids": first["formal_ids"],
            "knowledge_point": first["knowledge_point"], "record_key": first["record_key"],
            "source_id": item["source_id"], "source_sha256": item["source_sha256"], "source_fragment": item["source_fragment"],
            "question_ids": first["question_ids"], "record_keys": sorted(keys), "source_refs": source_refs,
            "trace_request": trace, "correction_refs": copy.deepcopy(first.get("correction_refs") or []),
            "independent_mastery_inferred": False, "point_error_inferred": False,
            "summary_coverage": base["coverage"]})
    unknown_relevant = sum(_event_interval(eligible[row["id"]])[0] is None for row in matches)
    return {**base, "status": ("ambiguous" if not latest and ranked.get("status") == "ambiguous" and len(candidates) > 1 else "ready") if candidates else "no_match", "candidates": candidates,
        "ambiguity": ranked.get("ambiguity"), "content_fields_redacted": metadata_only,
        "retrieval_mode": "exact_identity" if typed_identity else "fielded_bm25",
        **({"reason": "exact_identity_not_available_in_scope"} if typed_identity and not candidates else {}),
        "actor": actor, "requested_event_stage": stage, "recency_requested": latest, "recency_scope": "ranked_related_records_only",
        "undated_related_candidates": unknown_relevant,
        "latest_date_tied_sources": len(date_sources[max(date_sources)]) if date_sources else 0,
        "latest_is_proven": False, "latest_uncertainty": "undated_related_records" if latest and unknown_relevant else "bounded_retrieval_coverage",
        "filtered_counts": dict(filtered), "ranked_candidate_count": len(matches),
        "ranking_candidate_limit": MAX_RANK_CANDIDATES, "candidate_window_may_be_incomplete": len(matches) >= MAX_RANK_CANDIDATES,
        "private_artifact_reads": len(reads), "body_bucket_reads": sum(v.get("schema") == BODY_SCHEMA for v in reads.values()),
        "absence_meaning": "not_retrieved_not_proof_of_no_experience"}
