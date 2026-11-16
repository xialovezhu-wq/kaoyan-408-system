"""Scope-aware discovery over objective mechanisms and owned learning evidence.

This is an internal adapter for memory_access_408, not another authoritative
store. The existing publisher owns materialization and source versions.
"""
from __future__ import annotations

import copy
import hashlib
import json
import re
from collections import Counter, defaultdict
from pathlib import Path

SCHEMA = "cs408-precision-discovery-v1"
BUILD_VERSION = "precision-discovery-v1"
MAX_OBJECT_BYTES = 16 * 1024 * 1024
MAX_RESULTS = 8


def _bytes(value):
    return (json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n").encode()


def _ref(value):
    return {"ref": value["ref"], "sha256": value["sha256"]}


def build_search(point_artifacts, entities, *, repo=None):
    """Pure compilation; no filesystem writes and no new learning judgments."""
    from memory_retrieval_408 import build_concept_documents, build_alias_routes, compile_index, VERSION
    from memory_evidence_search_408 import build_evidence_index
    objects = {}

    def put(directory, value):
        raw = _bytes(value)
        if len(raw) > MAX_OBJECT_BYTES:
            raise ValueError("precision_artifact_exceeds_limit:" + directory)
        digest = hashlib.sha256(raw).hexdigest()
        reference = "objects/" + directory + "/" + digest + ".json"
        objects[reference] = value
        return {"ref": reference, "sha256": digest}

    source = dict(point_artifacts)
    if "index_ref" not in source:
        digest = hashlib.sha256(_bytes(source["index"])).hexdigest()
        source["index_ref"] = {"ref": "objects/point-indexes/" + digest + ".json", "sha256": digest}
    documents = build_concept_documents(source, entities)
    # The directory and the ranker must advertise the same identity semantics.
    # Neither keyword mentions nor co-occurrence labels are identity aliases.
    for document in documents:
        if document["id"] in entities:
            entities[document["id"]]["aliases"] = copy.deepcopy(document.get("aliases", []))
    lexical = compile_index(documents, include_documents=True, shard_count=256)
    postings = {prefix: put("memory-concept-postings", {
        "schema": "cs408-concept-postings-v1", "prefix": prefix, "postings": values})
        for prefix, values in sorted(lexical.pop("postings_shards").items())}
    concept = {"schema": "cs408-concept-search-v1", "version": VERSION,
        "index_object": put("memory-concept-lexical", lexical), "postings": postings,
        "document_count": len(documents)}
    resolver = None
    if repo is not None:
        from memory_episode_source_408 import EpisodeSourceResolver
        resolver = EpisodeSourceResolver(repo, entities=entities)
    private = build_evidence_index(source, entities=entities, source_resolver=resolver)
    objects.update(private["objects"])
    descriptor = {"schema": SCHEMA, "version": BUILD_VERSION,
        "concept_search": concept, "evidence_search": private["descriptor"],
        "alias_routes_object": put("memory-alias-routes", build_alias_routes(documents)),
        "source_binding": "owning_native_manifest", "concept_document_count": len(documents),
        "evidence_document_count": private["statistics"]["document_count"],
        "evidence_coverage": private["statistics"], "formal_write_count": 0}
    from memory_semantic_precision_408 import public_semantic_projection
    descriptor["public_semantic_projection_sha256"] = public_semantic_projection(documents)["projection_sha256"]
    return {"descriptor": descriptor, "objects": objects, "documents": documents}


def fuse_candidates(lexical, semantic, *, limit=20):
    """Frozen weighted reciprocal rank fusion, before optional public reranking."""
    scores, entries = defaultdict(float), {}
    for weight, rows in ((2.0, lexical), (1.0, semantic)):
        seen = set()
        for rank, row in enumerate(rows):
            identity = row["id"]
            if identity in seen:
                continue
            seen.add(identity)
            entries.setdefault(identity, row)
            scores[identity] += weight / (20 + rank + 1)
    return [{**entries[identity], "score": scores[identity], "match_type": "local_lexical_semantic_fusion"}
            for identity in sorted(scores, key=lambda identity: (-scores[identity], identity))[:limit]]


def _public_hybrid(query, lexical, permitted, descriptor, options):
    from memory_semantic_precision_408 import rank_semantic
    if not permitted:
        return lexical
    opts = dict(options or {})
    semantic_options = dict(opts.get("semantic") or {})
    expected = descriptor.get("public_semantic_projection_sha256")
    if not expected or semantic_options.get("expected_projection_hash", expected) != expected:
        return {"status": "unavailable", "reason": "semantic_projection_not_bound", "candidates": []}
    semantic_options["expected_projection_hash"] = expected
    manifest_path = semantic_options.get("manifest_path")
    if not manifest_path:
        return {"status": "unavailable", "reason": "local_semantic_not_configured", "candidates": []}
    manifest = json.loads(Path(manifest_path).read_text())
    semantic_ids = manifest.get("documents", [])
    semantic_ids = set(semantic_ids) if isinstance(semantic_ids, dict) else {item["id"] for item in semantic_ids}
    semantic = rank_semantic(query, permitted & semantic_ids, limit=40, **semantic_options)
    if semantic.get("status") != "ready":
        return {"status": "unavailable", "reason": semantic.get("reason", "local_semantic_unavailable"), "candidates": []}
    candidates = fuse_candidates(lexical.get("candidates", []), semantic.get("candidates", []))
    if not candidates:
        return {**lexical, "semantic_attempted": True, "semantic_projection_sha256": expected}
    if opts.get("rerank"):
        from memory_semantic_precision_408 import rerank_semantic
        rerank_options = {**opts["rerank"], "expected_projection_hash": expected,
                          "allowed_ids": permitted & semantic_ids, "limit": min(20, len(candidates))}
        candidates = [row for row in candidates if row["id"] in semantic_ids]
        reranked = rerank_semantic(query, [row["id"] for row in candidates], **rerank_options)
        if reranked.get("status") != "ready":
            return {"status": "unavailable", "reason": reranked.get("reason", "local_reranker_unavailable"), "candidates": []}
        candidates = [{**row, "match_type": "local_hybrid_reranked"} for row in reranked["candidates"]]
    return {"status": "ready", "reason": "local_hybrid_candidates", "candidates": candidates,
        "semantic_projection_sha256": expected, "model_call_count": semantic.get("model_call_count", 1)}


def infer_intent(query, requested="auto"):
    if requested not in {"auto", "concept", "experience"}:
        raise ValueError("discovery_intent_invalid")
    if requested != "auto":
        return requested
    if re.match(r"^(?:KLR|EVU?408)-", query.strip(), re.I):
        return "experience"
    subject_query = re.sub(r"(?:帮|给|为)我", "", query)
    if (re.search(r"找|查|定位|回看|回顾", subject_query)
            and re.search(r"我|我的|助手|你当时|你之前", subject_query)
            and re.search(r"记录|作答|回答|首答|原始代码|那段|那次|修复|解释|讲解|判断|思路|推理|说法", subject_query)):
        return "experience"
    if re.search(r"我(?:上次|之前|以前|曾经|当时|最近|那次)|(?:上次|之前|以前|曾经|哪次|哪道|什么时候).{0,12}我|"
                 r"(?:哪次|哪道).{0,16}(?:弄混|做错|说错|误解)|(?:你|助手).{0,6}(?:之前|上次|以前).{0,8}(?:讲|解释)|"
                 r"找.{0,8}(?:学习记录|作答记录|我的记录)", query):
        return "experience"
    return "concept"


def _current_question(snapshot, selector, explicit=None):
    """Return a proven canonical question; never guess a private part binding."""
    import memory_access_408 as native
    identity = explicit or selector.get("current_question_id") or selector.get("formal_id")
    if not identity and (native.EXAM_RE.fullmatch(selector.get("id", "")) or native.FORMAL_RE.fullmatch(selector.get("id", ""))):
        identity = selector["id"]
    if not identity and (native.EXAM_RE.fullmatch(selector.get("source_id", "")) or native.FORMAL_RE.fullmatch(selector.get("source_id", ""))):
        identity = selector["source_id"]
    if not identity:
        return None, "question_part_without_parent" if selector.get("question_part") else None
    if re.fullmatch(native.EXAM_RE.pattern, identity, re.I) or re.fullmatch(native.FORMAL_RE.pattern, identity, re.I):
        identity = identity.upper()
    entity = snapshot.entity(identity)
    if entity is None:
        return None, "current_question_not_registered"
    canonical = identity if native.EXAM_RE.fullmatch(identity) else (entity or {}).get("canonical_exam_id")
    if not canonical:
        return None, "current_question_canonical_scope_unverified"
    part = selector.get("question_part")
    if part:
        if not re.fullmatch(r"P\d{2,3}", part):
            return None, "current_question_part_invalid"
        if "-P" in canonical and canonical.rsplit("-", 1)[-1] != part:
            return None, "current_question_part_conflict"
        canonical = canonical.split("-P")[0] + "-" + part
        if snapshot.entity(canonical) is None:
            return None, "current_question_part_scope_unverified"
    return canonical, None


def _point_scope(snapshot, selector, *, view, current_question, years, for_evidence=False):
    """Use the existing identity route and KSP evidence-binding checks."""
    import memory_access_408 as native
    selected = {k: v for k, v in selector.items() if k != "question_part"}
    source = selected.get("source_id")
    if for_evidence and source and not native.EXAM_RE.fullmatch(source) and not native.FORMAL_RE.fullmatch(source):
        selected.pop("source_id", None)
    if not selected:
        return None, None, None
    entities, routed = native._entities(snapshot, selected)
    if routed.get("ambiguous_terms"):
        return set(), set(), "ambiguous_selector"
    points = {e.get("knowledge_point", e["id"]) for e in entities if e["kind"] in {"knowledge_point", "knowledge_subpoint"}}
    if not points:
        return set(), set(), "selector_has_no_verified_points"
    facets = [e for e in entities if e["kind"] == "knowledge_subpoint"]
    keys = None
    if facets and for_evidence:
        if view != "direct":
            return points, set(), "subpoint_record_scope_requires_precise_evidence_read"
        rows, _ = native._point_rows(snapshot, entities)
        narrowed = {**selected}
        if current_question:
            narrowed["current_question_id"] = current_question
        rows, unknown = native._record_scope(snapshot, rows, entities, narrowed, years, None)
        keys = {row["record_key"] for row in rows}
        if not keys:
            return points, keys, "subpoint_record_scope_unverified" if unknown else "no_subpoint_records"
    return points, keys, None


def discover(snapshot, query, *, selector=None, view="protected", limit=8, intent="auto", actor="user",
             excluded_years=None, allowed_ids=None, current_question_id=None,
             event_after=None, event_before=None, known_before=None, latest=None, backend="native",
             backend_options=None, pending=None, evidence_gap=None):
    import memory_access_408 as native
    from memory_retrieval_408 import rank_documents, RetrievalError
    from memory_evidence_search_408 import search_experiences
    if not isinstance(query, str) or not query.strip() or len(query) > 1000 or any(ord(c) < 32 for c in query):
        raise ValueError("discovery_query_invalid")
    if type(limit) is not int or not 1 <= limit <= MAX_RESULTS:
        raise ValueError("discovery_limit_invalid")
    if view not in native.VIEWS or actor not in {"user", "assistant", "any"}:
        raise ValueError("discovery_view_or_actor_invalid")
    selector = native._selection(selector)
    chosen_intent = infer_intent(query, intent)
    years, allowed = native._scope({"excluded_years": excluded_years, "allowed_ids": allowed_ids})
    public_question = current_question_id or selector.get("current_question_id") or selector.get("formal_id")
    for key in ("id", "source_id"):
        text = selector.get(key, "")
        if not public_question and (re.fullmatch(native.EXAM_RE.pattern, text, re.I) or re.fullmatch(native.FORMAL_RE.pattern, text, re.I)):
            public_question = text
    if view == "protected" and public_question:
        return {"operation": "discover", "intent": chosen_intent, "status": "protected", "candidates": [],
                "reason": "protected_current_question", "private_artifact_reads": 0}
    if chosen_intent == "experience" and view == "protected":
        return {"operation": "discover", "intent": chosen_intent, "status": "protected", "candidates": [],
                "reason": "protected_experience_lookup", "private_artifact_reads": 0}
    descriptor = (snapshot.descriptor or {}).get("precision_search")
    if not descriptor or descriptor.get("schema") != SCHEMA or descriptor.get("version") != BUILD_VERSION:
        return {"operation": "discover", "intent": chosen_intent, "status": "unavailable", "candidates": [],
                "reason": "precision_search_index_not_built"}
    canonical, scope_error = _current_question(snapshot, selector, current_question_id)
    if scope_error and chosen_intent == "experience":
        return {"operation": "discover", "intent": chosen_intent, "status": "insufficient_evidence",
                "candidates": [], "reason": scope_error, "private_artifact_reads": 0}
    if canonical and any(int(native.EXAM_RE.fullmatch(q)[1]) in years for q in [canonical]):
        return {"operation": "discover", "intent": chosen_intent, "status": "protected", "candidates": [],
                "reason": "question_year_excluded", "private_artifact_reads": 0}
    point_ids, record_keys, point_error = _point_scope(snapshot, selector, view=view, current_question=canonical,
                                                     years=years, for_evidence=chosen_intent == "experience")
    if point_error:
        return {"operation": "discover", "intent": chosen_intent, "status": "insufficient_evidence",
                "candidates": [], "reason": point_error, "private_artifact_reads": 0}
    pending = pending or {"status": "not_requested", "documents": []}
    pending_matches = [d for d in pending.get("documents", []) if native._norm(d["id"]) == native._norm(query) and native._allowed(d, years, allowed)]
    if pending_matches:
        return {"operation": "discover", "intent": chosen_intent, "status": "candidates",
                "candidates": [{**d, "match_type": "exact_pending_identity", "evidence_read_required": True} for d in pending_matches[:limit]],
                "pending_index": {k: v for k, v in pending.items() if k != "documents"}}
    typed_query = query.strip().upper()
    if chosen_intent == "concept" and not selector and not current_question_id and re.fullmatch(
            r"(?:DS|CO|OS|CN)\d{2}-\d{2}|(?:DS|CO|OS|CN)_[A-Z0-9_]+|EXAM408-\d{4}-Q\d{2}(?:-P\d{2,3})?|(?:KSP|S27)-[A-Z0-9-]+", typed_query):
        entity = snapshot.entity(query.strip())
        if entity is None:
            canonical_query = ("KSP-" + typed_query[4:].lower()
                               if re.fullmatch(r"KSP-[A-F0-9]+", typed_query) else typed_query)
            entity = snapshot.entity(canonical_query)
        rows = []
        if entity and native._allowed(entity, years, allowed):
            rows = [{"id": entity["id"], "kind": entity["kind"], "label": entity["label"],
                "match_type": "exact_identity", "score": 1.0, "source_refs": entity.get("source_refs", []),
                "evidence_read_required": True}]
        return {"operation": "discover", "intent": chosen_intent, "status": "candidates" if rows else "not_found",
            "candidates": rows, "reason": "exact_identity" if rows else "exact_identity_unavailable_in_scope",
            "retrieval_mode": "exact_identity", "model_call_count": 0,
            "absence_meaning": "not_retrieved_not_proof_of_no_record"}
    if chosen_intent == "experience":
        if backend not in {"native", "lexical", "hybrid_local"}:
            return {"operation": "discover", "intent": chosen_intent, "status": "unavailable",
                    "candidates": [], "reason": "private_evidence_backend_not_enabled", "private_artifact_reads": 0}
        source = selector.get("source_id")
        source_ids = [source] if source and not native.EXAM_RE.fullmatch(source) and not native.FORMAL_RE.fullmatch(source) else None
        result = search_experiences(query, descriptor["evidence_search"], read_object=snapshot.read,
            view=view, limit=limit, actor=actor, current_question_id=canonical,
            excluded_years=years, allowed_ids=allowed, point_ids=point_ids, record_keys=record_keys,
            source_ids=source_ids, event_after=event_after, event_before=event_before,
            known_before=known_before, latest=latest)
        result = {k: v for k, v in result.items() if k not in {"schema", "source_version"}}
        result["status"] = {"ready": "candidates", "no_match": "not_found", "withheld": "protected",
                            "history_required": "insufficient_evidence"}.get(result.get("status"), result.get("status"))
        for candidate in result.get("candidates", []):
            candidate["evidence_read_required"] = True
            if "snippet" in candidate:
                candidate.setdefault("snippet_kind", "native_record_summary")
            candidate["source_read_required_for_verbatim"] = True
        return {**result, "operation": "discover", "intent": chosen_intent,
                "scope": {"subject": "408", "excluded_years": sorted(years), "current_question_id": canonical},
                "retrieval_mode": result.get("retrieval_mode", "scoped_evidence_bm25")}
    documents = snapshot.documents()
    permitted = {d["id"] for d in documents if native._allowed(d, years, allowed)}
    if point_ids is not None:
        scoped = set(point_ids)
        for doc in documents:
            if doc["kind"] == "knowledge_subpoint" and (snapshot.entity(doc["id"]) or {}).get("knowledge_point") in point_ids:
                scoped.add(doc["id"])
        if selector.get("mechanisms") or any(selector.get(key, "").startswith("KSP-") for key in ("id", "concept")):
            selected, _ = native._entities(snapshot, selector)
            scoped = {entity["id"] for entity in selected if entity["kind"] == "knowledge_subpoint"}
        permitted &= scoped
    concept = descriptor["concept_search"]
    lexical = snapshot.read(concept["index_object"])
    def load_postings(prefix):
        reference = concept["postings"].get(prefix)
        if reference is None:
            return {}
        value = snapshot.read(reference)
        if value.get("schema") != "cs408-concept-postings-v1" or value.get("prefix") != prefix:
            raise ValueError("concept_postings_scope_invalid")
        return value["postings"]
    result = rank_documents(query, lexical, limit=8 if backend == "hybrid_local" else limit, kind="concept", allowed_ids=permitted,
                            view=view, current_question_id=public_question, load_postings=load_postings)
    if backend == "hybrid_local" and result.get("reason") not in {"exact_identity", "exact_canonical_title", "exact_synonym", "exact_subpoint_title", "unknown_exact_identity", "identity_outside_allowed_scope"}:
        result = _public_hybrid(query, result, permitted, descriptor, backend_options)
        if result.get("status") != "ready":
            return {**result, "status": "not_found" if result.get("status") == "no_match" else result.get("status"),
                    "operation": "discover", "intent": chosen_intent}
        families, selected = Counter(), []
        for row in result["candidates"]:
            entity = snapshot.entity(row["id"]) or {}
            family = entity.get("knowledge_point") or row["id"]
            if families[family] >= 2:
                continue
            selected.append(row)
            families[family] += 1
            if len(selected) == limit:
                break
        result["candidates"] = selected
    elif backend not in {"native", "lexical", "hybrid_local"} and result.get("reason") not in {"exact_identity", "exact_canonical_title", "exact_synonym", "exact_subpoint_title", "unknown_exact_identity", "identity_outside_allowed_scope"}:
        from memory_search_backends_408 import search_candidates
        external = search_candidates(query, [d for d in documents if d["id"] in permitted],
            backend=backend, limit=limit, **(backend_options or {}))
        if external.get("status") != "ready":
            return {"operation": "discover", "intent": chosen_intent, "status": "unavailable", "candidates": [],
                    "reason": external.get("degraded", "backend_unavailable"), "backend_result": external}
        result = {"status": "ready", "reason": "explicit_backend_candidates", "candidates": [
            {**row, "kind": (snapshot.entity(row["id"]) or {}).get("kind"),
             "title": (snapshot.entity(row["id"]) or {}).get("label"), "match_type": backend}
            for row in external.get("candidates", []) if row["id"] in permitted]}
    supplement = None
    initial = result.get("candidates", [])
    exact_reasons = {"exact_identity", "exact_canonical_title", "exact_synonym", "exact_subpoint_title",
                     "unknown_exact_identity", "identity_outside_allowed_scope"}
    if evidence_gap and result.get("reason") not in exact_reasons:
        from formal_quality_index_408 import anchored_ids, safe_text
        gap = safe_text(evidence_gap, "followup", 240)
        seeds = [row["id"] for row in initial[:3]]
        # Private-derived relations are never surfaced in the protected view.
        anchors = (anchored_ids(snapshot, seeds, permitted, excluded_years=years, current_question=canonical)
                   if view != "protected" and not scope_error else [])
        followup = rank_documents(query + " " + gap, lexical, limit=8, kind="concept",
                                  allowed_ids=permitted, view=view, current_question_id=public_question,
                                  load_postings=load_postings)
        added, seen = [], {row["id"] for row in initial}
        for row in [*({"id": identity, "match_type": "verified_relation_endpoint"} for identity in anchors),
                    *followup.get("candidates", [])]:
            if row["id"] not in seen and len(added) < 8:
                added.append({**row, "evidence_read_required": True})
                seen.add(row["id"])
        combined = initial + added
        result = {**result, "candidates": combined[:limit]}
        if added and not initial:
            result["status"] = "ready"
            result["reason"] = "bounded_followup_candidates"
        supplement = {"rounds": 1, "initial_count": len(initial), "additional_count": len(added),
                      "omitted_ids": [row["id"] for row in combined[limit:]],
                      "stop_reason": "bounded_followup" if added else "no_new_scoped_evidence"}
    candidates = []
    for row in result.get("candidates", []):
        entity = snapshot.entity(row["id"])
        if not entity or row["id"] not in permitted:
            continue
        candidates.append({**row, "label": row.get("title", entity["label"]),
            "kind": entity["kind"], "source_refs": row.get("source_refs") or entity.get("source_refs", []),
            "evidence_read_required": True})
    status = {"ready": "candidates", "no_match": "not_found", "withheld": "protected"}.get(result.get("status"), result.get("status"))
    return {"operation": "discover", "intent": chosen_intent, "status": status,
        "candidates": candidates, "reason": result.get("reason"), "ambiguity": result.get("ambiguity"),
        "retrieval_mode": "objective_mechanism_bm25" if backend == "native" else backend,
        "scope": {"subject": "408", "excluded_years": sorted(years)},
        "absence_meaning": "not_retrieved_not_proof_of_no_record", "automatic_selection_allowed": result.get("reason") in {
            "exact_identity", "exact_canonical_title", "exact_synonym", "exact_subpoint_title"} and len(candidates) == 1,
        "ranking_scope_count": result.get("ranking_scope_count"),
        **({"followup": supplement} if supplement else {}),
        "pending_index": {k: v for k, v in pending.items() if k != "documents"}}
