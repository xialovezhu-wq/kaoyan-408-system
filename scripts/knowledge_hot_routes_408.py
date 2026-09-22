"""Content-addressed hash routes for bounded 408 tutoring queries.

The cold builder may still keep complete indexes for audit and recovery.  The
learner path follows only the fixed manifest, the hash buckets needed by the
submitted keys, and the selected point/profile objects.
"""
from __future__ import annotations

from collections import Counter, defaultdict
import hashlib
import json
import re
import unicodedata
from typing import Any, Callable, Mapping

from knowledge_learning_records_408 import canonical_knowledge_points


SCHEMA = "knowledge_hot_routes_408_v1"
BUCKET_SCHEMA = "knowledge_hot_route_bucket_408_v1"
BUILD_VERSION = "knowledge-hot-routes-408-v1"
DIRECTORY = "objects/knowledge-hot-routes"
PREFIX_LENGTH = 2
MAX_BUCKET_BYTES = 512 * 1024


def _bytes(value: Any) -> bytes:
    return (json.dumps(value, ensure_ascii=False, sort_keys=True,
                       separators=(",", ":")) + "\n").encode("utf-8")


def _norm(value: object) -> str:
    text = unicodedata.normalize("NFKC", str(value or ""))
    return re.sub(r"\s+", " ", text).strip().lower()


def _token(kind: str, key: object) -> str:
    normalized = _norm(key)
    if not normalized:
        raise ValueError("hot_route_key_empty")
    return kind + ":" + normalized


def _prefix(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()[:PREFIX_LENGTH]


def build_routes(exact_index: Mapping[str, Any], knowledge_index: Mapping[str, Any],
                 point_index: Mapping[str, Any]) -> dict[str, Any]:
    """Build sparse route buckets; no complete key table is copied into manifest."""
    buckets: dict[str, dict[str, Any]] = defaultdict(dict)

    def put(kind: str, key: object, value: Any) -> None:
        token = _token(kind, key)
        buckets[_prefix(token)][token] = value

    profiles = exact_index.get("profiles")
    sources = exact_index.get("by_source_id")
    points = point_index.get("points")
    formal_points = point_index.get("formal_points")
    aliases = point_index.get("aliases")
    alias_modes = point_index.get("alias_modes") or {}
    if not all(isinstance(value, dict) for value in
               (profiles, sources, points, formal_points, aliases, alias_modes)):
        raise ValueError("hot_route_source_index_malformed")

    for formal_id, entry in sorted(profiles.items()):
        put("formal", formal_id, {
            "profile": entry,
            "knowledge_points": list(formal_points.get(formal_id) or []),
        })
    for source_id, formal_ids in sorted(sources.items()):
        put("source", source_id, list(formal_ids))
    for code, entry in sorted(points.items()):
        put("point", code, entry)
    subpoint_aliases = defaultdict(list)
    for key, entry in sorted((point_index.get("subpoints") or {}).items()):
        put("subpoint", key, entry)
        for term in {entry["title"], *entry["keywords"]}:
            subpoint_aliases[_norm(term)].append(key)
    for term, keys in sorted(subpoint_aliases.items()):
        put("subpoint-alias", term, sorted(set(keys)))
    for alias, codes in sorted(aliases.items()):
        mode = alias_modes.get(alias)
        if mode not in {"unique", "expansion", "ambiguous"}:
            mode = "unique" if len(codes) == 1 else "ambiguous"
        put("alias", alias, {"codes": list(codes), "resolution": mode})

    tables = (
        ("profile-knowledge", "by_knowledge_term"),
        ("profile-mechanism", "by_mechanism_term"),
        ("profile-ask-type", "by_ask_type_term"),
    )
    for kind, field in tables:
        table = knowledge_index.get(field)
        if not isinstance(table, dict):
            raise ValueError("hot_route_knowledge_index_malformed:" + field)
        for term, formal_ids in sorted(table.items()):
            put(kind, term, list(formal_ids))

    objects: dict[str, Any] = {}
    refs: dict[str, dict[str, str]] = {}
    for prefix, routes in sorted(buckets.items()):
        value = {"schema": BUCKET_SCHEMA, "prefix": prefix,
                 "routes": dict(sorted(routes.items()))}
        raw = _bytes(value)
        if len(raw) > MAX_BUCKET_BYTES:
            raise ValueError("hot_route_bucket_too_large:" + prefix)
        digest = hashlib.sha256(raw).hexdigest()
        ref = f"{DIRECTORY}/{digest}.json"
        objects[ref] = value
        refs[prefix] = {"ref": ref, "sha256": digest}

    route_count = sum(len(routes) for routes in buckets.values())
    set_sha256 = hashlib.sha256(_bytes({"buckets": refs,
                                       "route_count": route_count})).hexdigest()
    coverage = point_index.get("coverage") or {}
    compact_coverage = {
        key: value for key, value in coverage.items()
        if isinstance(value, (str, int, float, bool))
    }
    descriptor = {
        "schema": SCHEMA,
        "builder_version": BUILD_VERSION,
        "prefix_length": PREFIX_LENGTH,
        "buckets": refs,
        "bucket_count": len(refs),
        "route_count": route_count,
        "set_sha256": set_sha256,
        "coverage": compact_coverage,
        "source_coverage": point_index.get("source_coverage") or {},
    }
    return {"descriptor": descriptor, "objects": objects}


class Reader:
    """Validate and cache only the hash buckets actually selected by keys."""

    def __init__(self, descriptor: Mapping[str, Any],
                 read_object: Callable[[Mapping[str, str]], Mapping[str, Any]]):
        buckets = descriptor.get("buckets")
        if (descriptor.get("schema") != SCHEMA
                or descriptor.get("builder_version") != BUILD_VERSION
                or descriptor.get("prefix_length") != PREFIX_LENGTH
                or not isinstance(buckets, dict)
                or descriptor.get("bucket_count") != len(buckets)
                or not isinstance(descriptor.get("route_count"), int)
                or any(not re.fullmatch(r"[0-9a-f]{2}", key)
                       for key in buckets)):
            raise ValueError("hot_route_manifest_invalid")
        expected_set = hashlib.sha256(_bytes({
            "buckets": buckets,
            "route_count": descriptor["route_count"],
        })).hexdigest()
        if descriptor.get("set_sha256") != expected_set:
            raise ValueError("hot_route_set_hash_invalid")
        self.descriptor = descriptor
        self.read_object = read_object
        self.cache: dict[str, Mapping[str, Any]] = {}

    def get(self, kind: str, key: object) -> Any:
        token = _token(kind, key)
        prefix = _prefix(token)
        entry = self.descriptor["buckets"].get(prefix)
        if entry is None:
            return None
        if not isinstance(entry, dict):
            raise ValueError("hot_route_bucket_reference_invalid")
        if prefix not in self.cache:
            value = self.read_object(entry)
            if (value.get("schema") != BUCKET_SCHEMA
                    or value.get("prefix") != prefix
                    or not isinstance(value.get("routes"), dict)):
                raise ValueError("hot_route_bucket_invalid:" + prefix)
            self.cache[prefix] = value
        return self.cache[prefix]["routes"].get(token)


def resolve(descriptor: Mapping[str, Any], normalized: Mapping[str, Any],
            read_object: Callable[[Mapping[str, str]], Mapping[str, Any]],
            *, include_profiles: bool = True) -> dict[str, Any]:
    """Resolve exact identities, aliases and point refs without global indexes."""
    reader = Reader(descriptor, read_object)
    formal_routes: dict[str, Mapping[str, Any]] = {}

    def formal_route(formal_id: str) -> Mapping[str, Any] | None:
        canonical = formal_id.upper()
        if canonical not in formal_routes:
            value = reader.get("formal", canonical)
            if value is not None:
                if (not isinstance(value, dict)
                        or not isinstance(value.get("profile"), dict)
                        or not isinstance(value.get("knowledge_points"), list)):
                    raise ValueError("hot_route_formal_invalid:" + canonical)
                formal_routes[canonical] = value
        return formal_routes.get(canonical)

    source_lookup: dict[str, list[str]] = {}
    for source in normalized.get("source", []):
        values = reader.get("source", source)
        if values is None:
            source_lookup[source] = []
        elif not isinstance(values, list) or any(not isinstance(v, str) for v in values):
            raise ValueError("hot_route_source_invalid")
        else:
            source_lookup[source] = values
            for formal_id in values:
                formal_route(formal_id)
    for formal_id in normalized.get("formal", []):
        formal_route(formal_id)

    requested: list[str] = []
    missing: list[str] = []
    ambiguous: list[dict[str, Any]] = []
    weak: set[str] = set()
    ambiguous_knowledge_terms: set[str] = set()
    subpoint_entries = {}
    explicit_points = {code for term in normalized.get("knowledge", [])
                       + normalized.get("weak_knowledge", [])
                       for code in canonical_knowledge_points(term)}

    def add_subpoint(term):
        direct = reader.get("subpoint", term)
        keys = ["KSP-" + term.strip()[4:].lower()] if direct else reader.get("subpoint-alias", term)
        if not keys:
            return False
        candidates = {key: reader.get("subpoint", key) for key in keys}
        if any(not isinstance(row, dict) or not isinstance(row.get("teaching_views"), dict)
               for row in candidates.values()):
            raise ValueError("hot_route_subpoint_invalid")
        candidates = {key: row for key, row in candidates.items()
                      if not explicit_points or row["knowledge_point"] in explicit_points}
        if len(candidates) == 1:
            subpoint_entries.update(candidates)
        elif len(candidates) > 1:
            ambiguous.append({"term": term, "candidate_subpoints": [
                {"subpoint_id": key, "knowledge_point": row["knowledge_point"], "title": row["title"]}
                for key, row in candidates.items()]})
        else:
            missing.append(term)
        return True

    def add_code(code: str, is_weak: bool) -> None:
        canonical = code.upper()
        if canonical not in requested:
            requested.append(canonical)
        if is_weak:
            weak.add(canonical)

    def add_term(term: str, is_weak: bool = False) -> None:
        if add_subpoint(term):
            return
        codes = canonical_knowledge_points(term)
        if not codes:
            alias = reader.get("alias", term)
            if alias is None:
                if term not in missing:
                    missing.append(term)
                return
            if (not isinstance(alias, dict) or not isinstance(alias.get("codes"), list)
                    or alias.get("resolution") not in {"unique", "expansion", "ambiguous"}):
                raise ValueError("hot_route_alias_invalid")
            codes = alias["codes"]
            if alias["resolution"] == "ambiguous":
                value = {"term": term, "candidate_points": list(codes)}
                if value not in ambiguous:
                    ambiguous.append(value)
                ambiguous_knowledge_terms.add(term)
                return
        for code in codes:
            add_code(code, is_weak)

    for term in normalized.get("weak_knowledge", []):
        add_term(term, True)
    for term in normalized.get("knowledge", []):
        add_term(term)
    for term in normalized.get("mechanism", []):
        add_subpoint(term)

    exact_formals: list[str] = []
    for value in normalized.get("formal", []):
        canonical = value.upper()
        route = formal_route(canonical)
        if route:
            exact_formals.append(canonical)
    for source in normalized.get("source", []):
        exact_formals.extend(source_lookup.get(source, []))
    for formal_id in dict.fromkeys(exact_formals):
        route = formal_route(formal_id)
        if route:
            for code in route["knowledge_points"]:
                add_code(code, False)

    point_entries: dict[str, Mapping[str, str]] = {}
    for code in requested:
        entry = reader.get("point", code)
        if entry is None:
            if code not in missing:
                missing.append(code)
        elif (not isinstance(entry, dict) or not isinstance(entry.get("ref"), str)
              or not isinstance(entry.get("sha256"), str)):
            raise ValueError("hot_route_point_invalid:" + code)
        else:
            point_entries[code] = entry

    score: Counter[str] = Counter()
    match_keys: dict[str, set[str]] = defaultdict(set)
    if include_profiles:
        for value in normalized.get("formal", []):
            canonical = value.upper()
            if formal_route(canonical):
                score[canonical] += 1000
                match_keys[canonical].add("formal_id")
        for source in normalized.get("source", []):
            for formal_id in source_lookup.get(source, []):
                if formal_route(formal_id):
                    score[formal_id] += 800
                    match_keys[formal_id].add("source_id")
        specs = (
            ("knowledge", "profile-knowledge", 120),
            ("mechanism", "profile-mechanism", 80),
            ("ask_type", "profile-ask-type", 40),
        )
        for input_key, route_kind, weight in specs:
            for term in normalized.get(input_key, []):
                if input_key == "knowledge" and term in ambiguous_knowledge_terms:
                    continue
                values = reader.get(route_kind, term)
                if values is None:
                    continue
                if not isinstance(values, list) or any(not isinstance(v, str) for v in values):
                    raise ValueError("hot_route_profile_lookup_invalid:" + route_kind)
                for formal_id in values:
                    if formal_route(formal_id):
                        score[formal_id] += weight
                        match_keys[formal_id].add(input_key)

    return {
        "formal_routes": formal_routes,
        "source_lookup": source_lookup,
        "exact_formals": list(dict.fromkeys(exact_formals)),
        "requested_points": requested,
        "missing_points": list(dict.fromkeys(missing)),
        "ambiguous_terms": ambiguous,
        "weak_points": weak,
        "point_entries": point_entries,
        "subpoint_entries": subpoint_entries,
        "score": score,
        "match_keys": match_keys,
        "coverage": descriptor.get("coverage") or {},
        "source_coverage": descriptor.get("source_coverage") or {},
    }
