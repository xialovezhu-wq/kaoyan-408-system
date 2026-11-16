"""Private, reproducible 408 retrieval projection for the formal author."""
from __future__ import annotations

import hashlib
import math
from collections import Counter, defaultdict
from pathlib import Path
import re

from formal_quality_api_408 import (
    DIMENSIONS, EMBEDDING, Journal, QualityError, atomic, digest, read, read_vector,
)

ROOT_REL = Path(".private/formal-quality-v1")
PROJECTION = "cs408-formal-semantic-v1"
POINT = re.compile(r"(?:DS|CO|OS|CN)\d{2}-\d{2}")
EXAM = re.compile(r"EXAM408-(\d{4})-Q\d{2}(?:-P\d{2,3})?")
FORMAL = re.compile(r"(?:DS|CO|OS|CN)_(\d{4}|UNK)_\d{3}")
# These checks supplement the author's explicit redaction and the native view
# filters. Only typed projections enter requests, never a source body.
UNSAFE = re.compile(
    r"(?:file://|obsidian://|https?://|/Users/|/Volumes/|[A-Za-z]:\\|"
    r"[\w.+-]+@[\w.-]+\.[A-Za-z]{2,}|(?<!\d)1[3-9]\d{9}(?!\d)|"
    r"sk-(?:proj-|[A-Za-z0-9]{16})|"
    r"正确(?:答案|选项)|答案(?:为|是|[:：])|(?:应|故|所以)选\s*[ABCDＡＢＣＤ]|"
    r"(?:^|\n)\s*[ABCDＡＢＣＤ][.、．)]|(?:系统提示词|API_KEY))",
    re.I,
)


def sha_file(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def safe_text(value, label, maximum=7000):
    if (not isinstance(value, str) or not value.strip() or "\x00" in value
            or len(value.encode()) > maximum or UNSAFE.search(value)):
        raise QualityError("quality_unsafe_or_oversize:" + label)
    return value.strip()


def _point_set(row):
    if isinstance(row, dict):
        return sorted(set(POINT.findall(" ".join(str(v) for v in row.values()))))
    return []


def public_projection(repo, *, snapshot=None):
    """Reconstruct the native public projection and verify its pinned digest."""
    from memory_access_408 import Snapshot
    from memory_retrieval_408 import build_concept_documents
    from memory_semantic_precision_408 import public_semantic_projection

    snap = snapshot if snapshot is not None else Snapshot(repo)
    manifest = snap.manifest
    index_ref = {"ref": manifest["point_index_ref"], "sha256": manifest["point_index_sha256"]}
    point_index = snap.read(index_ref)
    entities = {row["id"]: row for row in snap.documents()}

    class Objects:
        def get(self, reference):
            return snap.read({"ref": reference, "sha256": Path(reference).stem})

    documents = build_concept_documents({"index": point_index, "index_ref": index_ref,
                                         "objects": Objects()}, entities)
    projection = public_semantic_projection(documents)
    expected = snap.descriptor["precision_search"]["public_semantic_projection_sha256"]
    if expected != projection["projection_sha256"]:
        raise QualityError("quality_public_projection_drift")
    return snap, projection, documents, entities


def public_catalog(repo, *, snapshot=None):
    """Return checked public fragments and metadata, without starting a service."""
    snap, projection, documents, entities = public_projection(repo, snapshot=snapshot)
    manifest = snap.manifest
    expected = projection["projection_sha256"]
    rows = {}
    gaps = []
    meta = {doc["id"]: doc for doc in documents}
    for doc in projection["documents"]:
        key = doc["id"]
        raw = meta[key]
        parts = [doc["title"], raw["fields"].get("description"), raw["fields"].get("exam_entry"),
                 raw["fields"].get("keywords"), "、".join(raw.get("aliases", []))]
        text = "\n".join("；".join(x) if isinstance(x, list) else x for x in parts if x)
        try:
            text = safe_text(text, key)
        except QualityError:
            gaps.append({"id": key, "reason": "public_text_requires_review"})
            continue
        rows[key] = {"text": text, "title": doc["title"], "kind": doc["kind"],
                     "refs": doc["source_refs"], "points": [raw["knowledge_point"]] if raw["knowledge_point"] else [],
                     "aliases": raw.get("aliases", [])}
    links_path = Path(repo) / "support/syllabus-2027/point-question-links.json"
    links = read(links_path)
    links_sha = sha_file(links_path)
    for group in ("questions", "subquestions", "formal_cards"):
        for key, ref in links[group].items():
            points = [p for p in _point_set(ref) if p in rows]
            if not points:
                gaps.append({"id": key, "reason": "verified_public_mechanism_missing"})
                continue
            if group == "formal_cards":
                card = Path(repo) / f"{key}.md"
                if not card.is_file() or card.is_symlink():
                    gaps.append({"id": key, "reason": "safe_card_missing"})
                    continue
                raw_card = card.read_text()
                selected = re.findall(r"(?m)^- (?:实际考查机制|核心考点|主知识点)[：:]\s*(.+)$", raw_card)
                approved = []
                for text in selected:
                    try:
                        approved.append(safe_text(text, key, 900))
                    except QualityError:
                        continue
                source = [{"path": card.name, "sha256": sha_file(card),
                           "fields": ["实际考查机制", "核心考点", "主知识点"]}]
            else:
                approved = []
                source = [{"path": "support/syllabus-2027/point-question-links.json",
                           "sha256": links_sha, "field": group + "." + key}]
            entity = entities.get(key, {})
            if source_id := entity.get("canonical_exam_id"):
                source[0]["canonical_exam_id"] = source_id
            # Public mechanisms, not the question body. The known year and ID
            # remain local metadata used to filter the candidate.
            titles = [rows[p]["title"] for p in points]
            text = "；".join(dict.fromkeys([*titles, *approved]))
            rows[key] = {"text": safe_text(text, key), "title": "、".join(titles), "kind": group,
                         "refs": source, "points": points, "aliases": [],
                         "contact_dependency_ids": entity.get("contact_dependency_ids", [])}
    return {"source_version": manifest["source_version"], "public_projection_sha256": expected,
            "manifest_sha256": snap.manifest_sha, "rows": rows, "gaps": gaps,
            "native_entity_count": snap.descriptor["entity_count"]}


def _vector_key(text):
    return digest([PROJECTION, EMBEDDING, DIMENSIONS, text])


def _row_years(key, row):
    identities = (key, *row.get("contact_dependency_ids", []),
                  *(ref.get("canonical_exam_id") for ref in row["refs"]))
    return sorted({year for identity in identities if (year := exam_year(identity)) is not None})


def scoped_catalog(catalog, excluded_years):
    years = set(excluded_years)
    rows = {key: row for key, row in catalog["rows"].items() if not years.intersection(_row_years(key, row))}
    gaps = [row for row in catalog.get("gaps", []) if exam_year(row["id"]) not in years]
    return {**catalog, "rows": rows, "gaps": gaps, "excluded_years": sorted(years),
            "protected_entries": len(catalog["rows"]) - len(rows)}


def objective_routes(repo, catalog, *, request=None):
    stats = {}
    keys = materialize(Path(repo) / ROOT_REL, catalog, request=request, stats=stats)
    return {key: {"vector_key": v, "refs": catalog["rows"][key]["refs"]}
            for key, v in keys.items()}, stats


def _read_cached_vector(path, key, mode):
    cached = read(path)
    if (not isinstance(cached, dict) or cached.get("key") != key or cached.get("mode") != mode
            or not re.fullmatch(r"q408-[0-9a-f]{32}", cached.get("origin") or "")):
        raise QualityError("quality_history_vector_key_drift")
    return read_vector(cached["vector"])


def materialize(root, catalog, *, request=None, stats=None):
    """One versioned bootstrap/update. Identical old inputs use an on-disk cache."""
    root = Path(root)
    keys = {identity: _vector_key(row["text"]) for identity, row in catalog["rows"].items()}
    texts = {key: catalog["rows"][identity]["text"] for identity, key in keys.items()}
    todo = {}
    mode = "live" if request is None else "mock"
    for key, text in texts.items():
        try:
            _read_cached_vector(root / "vectors" / (key + ".json"), key, mode)
        except (OSError, ValueError, KeyError, TypeError, QualityError):
            todo[key] = safe_text(text, key)
            if _vector_key(todo[key]) != key:
                raise QualityError("quality_catalog_text_not_normalized:" + key)
    new_inputs = 0
    if todo:
        index_id = digest([PROJECTION, sorted(texts)])
        journal = Journal(root / "bootstrap" / index_id, "BASE-" + index_id, request=request)
        for key, value in journal.stage("historical-vectors", "embeddings", todo).items():
            path = root / "vectors" / (key + ".json")
            atomic(path, {"key": key, "vector": value["value"], "mode": mode,
                          "origin": value["request"]})
            _read_cached_vector(path, key, mode)
            new_inputs += not value["reused"]
    if stats is not None:
        stats.update(eligible_entries=len(keys), distinct_inputs=len(texts), cache_hits=len(texts) - len(todo),
                     restored_from_receipt=len(todo) - new_inputs, new_embedding_inputs=new_inputs,
                     protected_entries=catalog.get("protected_entries", 0),
                     by_kind=dict(Counter(row["kind"] for row in catalog["rows"].values())))
    return keys


def personalization_inventory(snapshot, affected_points=(), *, include_unreviewed=False, verify_routes=False):
    """Inspect every native route and its reviewed cross-point dependencies."""
    manifest = snapshot.manifest
    points = snapshot.read({"ref": manifest["point_index_ref"], "sha256": manifest["point_index_sha256"]})
    affected = set(affected_points)
    states, dependents, needs_review, unauthored = Counter(), {}, {}, []
    if verify_routes:
        from knowledge_hot_routes_408 import Reader

        routes = Reader(manifest["knowledge_hot_routes"], snapshot.read)
        for name, entries in (("point", points["points"]), ("subpoint", points["subpoints"])):
            for identity, entry in entries.items():
                if routes.get(name, identity) != entry:
                    raise QualityError("quality_personal_route_drift:" + identity)
                view = snapshot.read(entry["teaching_views"]["direct"])
                if ((view.get("subpoint_id") or view.get("knowledge_point")) != identity
                        or view.get("view", "direct") != "direct"
                        or (name == "subpoint" and view.get("knowledge_point") != entry["knowledge_point"])):
                    raise QualityError("quality_personal_view_drift:" + identity)
                if view["status"] == "semantic_current":
                    ref = (view.get("personal_source") or {}).get("profile_object") or entry.get("evidence_object")
                    obj = snapshot.read(ref)
                    version = view.get("parent_profile_version") if name == "subpoint" else view["personal_source"]["profile_version"]
                    if ((obj.get("subpoint_id") or obj.get("concept_key")) != identity
                            or (obj.get("parent_profile_version") or obj.get("version")) != version):
                        raise QualityError("quality_personal_evidence_drift:" + identity)
    for code, entry in points["points"].items():
        source = entry["personal_source"]
        state = source["status"]
        if state == "stale":
            reason = source.get("reason") or "personal_evidence_changed"
            needs_review[code] = reason
        elif state == "not_authored":
            state = "evidence_only" if snapshot.read(points["point_records"][code])["records"] else "no_records"
            if state == "evidence_only":
                unauthored.append(code)
        states[state] += 1
        if affected and source.get("profile_object") and code not in affected:
            profile = snapshot.read(source["profile_object"])
            sources = sorted({row["concept_key"] for row in profile.get("cross_point_sources", [])} & affected)
            if sources:
                dependents[code] = {"source_points": sources, "status": state,
                                    "profile_version": source["profile_version"]}
    return {"source_version": manifest["source_version"], "points": len(points["points"]),
            "subpoints": len(points["subpoints"]), "states": dict(states),
            "needs_review": needs_review, "affected_dependents": dependents,
            "routes_verified": len(points["points"]) + len(points["subpoints"]) if verify_routes else None,
            **({"evidence_without_reviewed_profile": sorted(unauthored)} if include_unreviewed else {})}


def exam_year(key):
    match = (EXAM.fullmatch(key) or FORMAL.fullmatch(key)) if isinstance(key, str) else None
    return int(match[1]) if match and match[1].isdigit() else None


def lexical_index(rows):
    from memory_retrieval_408 import compile_index

    return compile_index([{"id": key, "title": row["title"], "kind": "knowledge_point",
                           "aliases": row["aliases"], "fields": {"description": row["text"]}}
                          for key, row in rows.items()])


def select(root, rows, vector_keys, query, query_vector, *, exact, excluded_years, current,
           points, lexical, cached_vectors):
    """One union of exact, fielded BM25 and vector candidates."""
    from memory_retrieval_408 import rank_documents

    family = current.split("-P")[0] if current and EXAM.fullmatch(current) else None

    def allowed(key, row):
        mapped = next((ref.get("canonical_exam_id") for ref in row["refs"]
                       if ref.get("canonical_exam_id")), None)
        scoped = mapped or key
        if exam_year(scoped) in excluded_years:
            return False
        if row.get("privacy") == "private":
            return (bool(set(points).intersection(row["points"])) and not (current and "-P" in current)
                    and None not in row["years"] and not set(row["years"]).intersection(excluded_years))
        return not (family and scoped.split("-P")[0] == family and scoped != current)

    scope = {key: row for key, row in rows.items() if allowed(key, row)}
    ranked = rank_documents(query, lexical, limit=12, kind="concept", allowed_ids=set(scope))
    lexical_ids = [x["id"] for x in ranked.get("candidates", [])]
    query_values = read_vector(query_vector)
    scored = []
    for key in scope:
        if key not in vector_keys:
            continue
        vector_key = vector_keys[key]
        if vector_key not in cached_vectors:
            cached = read(root / "vectors" / (vector_key + ".json"))
            if cached["key"] != vector_key:
                raise QualityError("quality_history_vector_key_drift")
            cached_vectors[vector_key] = read_vector(cached["vector"]) if cached["mode"] == "live" else None
        values = cached_vectors[vector_key]
        if values is not None:
            scored.append((math.sumprod(query_values, values), key))
    nearest = [key for _, key in sorted(scored, key=lambda row: (-row[0], row[1]))[:12]]
    result = {}
    seen = {}
    for method, keys in (("exact", sorted(exact)), ("lexical", lexical_ids), ("vector", nearest)):
        for key in keys:
            if key not in scope:
                continue
            row = scope[key]
            identity = digest([row["text"], row["refs"]])
            retained = seen.setdefault(identity, key)
            result.setdefault(retained, [])
            if method not in result[retained]:
                result[retained].append(method)
    return result, {"eligible": len(scope), "vector_scanned": len(scored),
                    "lexical_limit": 12, "vector_limit": 12, "out_of_scope": len(rows) - len(scope)}


def private_history(repo, *, generation=None):
    from knowledge_profile_408 import current_index

    if generation is None:
        root = Path(repo) / ROOT_REL
        try:
            pointer = read(root / "current.json")
        except FileNotFoundError:
            return {}
        generation, _ = current_generation(repo, pointer["source_version"])
    profiles = current_index(repo)["profiles"]
    return {key: row for key, row in (generation or {}).get("private_history", {}).items()
            if profiles.get(row["points"][0], {}).get("sha256") == row["profile_sha256"]}


def _relation_hints(catalog, objective, frozen, candidate, proof):
    hints = defaultdict(list)
    for unit_id, unit in candidate["units"].items():
        source_unit = frozen["units"][unit_id]
        capture = frozen["captures"][source_unit["capture_id"]]
        canonical = capture.get("canonical_id") or ""
        if not EXAM.fullmatch(canonical) or "-P" in canonical or canonical not in catalog["rows"]:
            continue
        origin_years = _row_years(canonical, catalog["rows"][canonical])
        if not origin_years:
            continue
        for group, judgment in unit["judgments"].items():
            target = judgment["candidate_id"]
            use = proof["uses"][group]
            if (use["action"] not in {"adopt", "revise", "keep_conditions"} or target not in objective
                    or catalog["rows"][target]["kind"] not in {"knowledge_point", "knowledge_subpoint", "syllabus_entry"}):
                continue
            if judgment.get("text_sha256") != digest(catalog["rows"][target]["text"]):
                continue
            for point in source_unit["points"]:
                if point in objective and point != target and candidate.get("point_vectors", {}).get(point) == objective[point]["vector_key"]:
                    hints[point].append({"id": target, "unit": unit_id, "judgment": group,
                                         "field": use["location"], "origin_years": origin_years,
                                         "origin_question": canonical, "origin_run_id": proof["run_id"]})
    return hints


def retained_relations(repo, catalog, objective, *, batch=None):
    """Carry accepted hints whose original public text and source scope still match."""
    root = Path(repo) / ROOT_REL
    try:
        pointer = read(root / "current.json")
        previous, _ = current_generation(repo, pointer["source_version"])
    except FileNotFoundError:
        previous = None
    previous = previous or {}
    old_objective = previous.get("objective", {})
    result = _relation_hints(catalog, objective, *batch) if batch else defaultdict(list)
    for point, hints in previous.get("relations", {}).items():
        if point not in objective or old_objective.get(point, {}).get("vector_key") != objective[point]["vector_key"]:
            continue
        for hint in hints:
            target = hint["id"]
            old_target = old_objective.get(target, {})
            if target not in objective or old_target.get("vector_key") != objective[target]["vector_key"]:
                continue
            question = hint.get("origin_question")
            if question not in catalog["rows"] or hint.get("origin_years") != _row_years(question, catalog["rows"][question]):
                continue
            # Question projection references include the shared links file;
            # bind the historical comparison's typed question and text key.
            if old_objective.get(question, {}).get("vector_key") != objective[question]["vector_key"]:
                continue
            result[point].append(hint)
    merged = {}
    for point, hints in result.items():
        distinct = {}
        for row in hints:
            distinct.setdefault((row["id"], row["origin_question"]), row)
        merged[point] = list(distinct.values())
    return merged


def current_generation(repo, source_version):
    root = Path(repo) / ROOT_REL
    try:
        current = read(root / "current.json")
        if current["source_version"] != source_version:
            return None, "quality_projection_version_changed"
        path = (Path(repo) / current["generation_ref"]).resolve()
        if path.parent != (root / "generations").resolve():
            return None, "quality_projection_path_invalid"
        generation = read(path)
        if digest(generation) != current["generation_sha256"] or generation["source_version"] != source_version:
            return None, "quality_projection_hash_drift"
        return generation, None
    except FileNotFoundError:
        return None, "quality_projection_not_published"
    except (OSError, ValueError, KeyError, TypeError, QualityError):
        return None, "quality_projection_unavailable"


def _binding_current(snapshot, ref):
    if "ref" in ref:
        return bool(snapshot.read({"ref": ref["ref"], "sha256": ref["sha256"]}))
    if ref.get("path"):
        path = (snapshot.repo / ref["path"]).resolve()
        if path.is_relative_to(snapshot.repo) and path.is_file():
            return sha_file(path) == ref["sha256"]
    return False


def coverage(snapshot, permitted, *, offset=0, limit=50):
    if type(offset) is not int or offset < 0 or type(limit) is not int or not 1 <= limit <= 100:
        raise QualityError("quality_coverage_page_invalid")
    gen, degraded = current_generation(snapshot.repo, snapshot.manifest["source_version"])
    concept = snapshot.descriptor["precision_search"]["concept_search"]
    words = snapshot.read(concept["index_object"])
    textual = {key for key, supported in zip(words["doc_ids"], words["searchable"]) if supported}
    exact = set(words["doc_ids"])
    history = snapshot.descriptor["precision_search"].get("evidence_coverage", {})
    objective = (gen or {}).get("objective", {})
    personal = (gen or {}).get("personalization_coverage") or {}
    page = sorted(permitted)
    return {"operation": "coverage", "status": "ready" if gen else "partial",
            "total": len(page), "offset": offset,
            "next_offset": offset + limit if offset + limit < len(page) else None,
            "scopes": {"native_entities": len(page), "native_exact": len(exact & permitted),
                       "public_lexical": len(textual & permitted),
                       "openai_objective_vectors": len(objective.keys() & permitted),
                       "full_snapshot_profile_states": personal.get("states"),
                       "personal_evidence": history, "quality_publication": "current" if gen else degraded},
            "quality_gaps": [row for row in (gen or {}).get("source_gaps", []) if row["id"] in permitted],
            "entries": [{"id": key, "exact": key in exact, "public_lexical": key in textual,
                         "openai_vector": key in objective}
                        for key in page[offset:offset + limit]],
            "inventory_scope": "exact_declared_entities_not_semantic_recall"}


def anchored_ids(snapshot, seeds, permitted, *, excluded_years=(), current_question=None):
    gen, _ = current_generation(snapshot.repo, snapshot.manifest["source_version"])
    if not gen:
        return []
    if current_question and "-P" in current_question:
        return []
    from memory_access_408 import _allowed

    years = set(excluded_years) | set(gen.get("excluded_years", []))
    result = []
    for seed in seeds:
        for row in gen.get("relations", {}).get(seed, []):
            identity = row["id"]
            if identity not in permitted or identity in result:
                continue
            target = snapshot.entity(identity)
            if not target or target["kind"] not in {"knowledge_point", "knowledge_subpoint", "syllabus_entry"}:
                continue
            origin = row.get("origin_question")
            if not origin or not row.get("origin_years") or years.intersection(row["origin_years"]):
                continue
            source = snapshot.entity(origin)
            if source is None or not _allowed(source, years, None):
                continue
            refs = gen["objective"].get(identity, {}).get("refs", [])
            if refs and all(_binding_current(snapshot, ref) for ref in refs):
                result.append(identity)
            if len(result) == 3:
                return result
    return result
