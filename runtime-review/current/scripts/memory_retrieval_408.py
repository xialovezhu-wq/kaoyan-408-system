"""Source-bound lexical discovery for 408; no facts or identities are inferred.

The source compiler is pure; the ranker uses a pinned local word tokenizer
and its runtime cache. It performs no network, model, or learning-state
writes. Native readers remain the authority after
candidate discovery.  The postings format can be stored in immutable shards;
ranking needs only eligible IDs and the query's posting shards, never all
private record bodies.
"""
from __future__ import annotations

import copy
import hashlib
import importlib
import json
import logging
import math
import re
import sys
import unicodedata
import warnings
from bisect import bisect_left, bisect_right
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any, Callable, Iterable, Mapping

SCHEMA = "cs408-fielded-lexical-index-v1"
VERSION = "408-source-grounded-lexical-v3-word-tokens"
POINT_RE = re.compile(r"(?:DS|CO|OS|CN)\d{2}-\d{2}$", re.I)
ID_RE = re.compile(r"(?:EXAM408-\d{4}-Q\d{2}(?:-P\d{2,3})?|(?:DS|CO|OS|CN)\d{2}(?:-\d{2})?|(?:DS|CO|OS|CN)_[A-Z0-9_]+|KSP-[A-Z0-9-]+|S27-[A-Z0-9-]+|KLR-[A-Z0-9-]+|EVU?408-[A-Z0-9-]+)$", re.I)
CODE_IN_TEXT = re.compile(r"(?<![A-Za-z0-9])(?:EXAM408-\d{4}-Q\d{2}(?:-P\d{2,3})?|(?:DS|CO|OS|CN)\d{2}(?:-\d{2})?|(?:DS|CO|OS|CN)_[A-Z0-9_]+|KSP-[A-Z0-9-]+|S27-[A-Z0-9-]+)(?![A-Za-z0-9])", re.I)
HAN_RE = re.compile(r"[\u3400-\u9fff]+")
NUMBER_PATTERN = r"[+-]?(?:\d+(?:\.\d+)?|\.\d+)(?:[eE][+-]?\d+)?"
UNIT_PATTERN = (r"(?:(?:bits?|bytes?|[kmgtpe]i?[Bb]|[Bb]|[kmg]?hz|ns|us|μs|µs|ms|s)(?![a-z])|"
                r"字节|比特|毫秒|微秒|纳秒|赫兹|位|字|页|块|组|路|级|个|条|次|秒)")
PARAMETER_RE = re.compile(r"(?P<number>" + NUMBER_PATTERN + r")\s*(?P<unit>" + UNIT_PATTERN + r")?$", re.I)
WORD_RE = re.compile(
    r"0x[0-9a-f]+|0b[01]+|" + NUMBER_PATTERN + r"(?:\s*" + UNIT_PATTERN + r")?|"
    r"[\u3400-\u9fff]+|[a-z_][a-z0-9_]*(?:[./-][a-z0-9_]+)*|"
    r"\+\+|--|<<|>>|==|!=|<=|>=|&&|\|\|", re.I)
CODE_OPERATOR_RE = re.compile(r"(?:\+\+|--|<<|>>|==|!=|<=|>=|&&|\|\|)$")
SHA_RE = re.compile(r"[a-f0-9]{64}$")
ANSWER_RE = re.compile(r"正确(?:答案|选项)|答案\s*(?:为|是|[:：])|(?:应选|选了|我选)\s*[ABCD]|解得|/Users/|/Volumes/", re.I)
PERSONAL_RE = re.compile(r"用户(?:已经|仍|容易|不会)|学习者(?:已经|仍|容易|不会)|我(?:总是|不懂|不会)|已(?:经)?掌握|提示后(?:完成|理解)|独立做对")
NOISE = ("帮我找一下", "帮我找", "我想知道", "我想问", "请问", "这个问题", "这道题", "为什么", "如何", "怎么", "是什么", "哪些", "哪一个", "有没有", "是否", "是不是", "能否", "之前", "以前", "上次", "到底", "一下")
STOP_TERMS = {"一个", "这个", "那个", "以及", "进行", "需要", "可以", "应该", "时候", "什么", "怎么", "如何", "是否", "问题", "处理", "情况", "时候", "的时", "的话", "中的", "之间", "the", "and", "or", "of", "to", "for", "with", "from", "is", "a", "an"}
CONCEPT_KINDS = {"concept", "knowledge_point", "knowledge_subpoint", "syllabus_entry"}
CONCEPT_WEIGHTS = {"title": 4.8, "synonyms": 4.2, "keywords": 3.0,
                   "mechanism": 2.8, "description": 3.2, "exam_entry": 1.8,
                   "scope": 0.45, "parent_scope": 0.08,
                   "association_terms": 0.45, "terms": 0.45}
EVIDENCE_WEIGHTS = {"summary": 5.0, "statement": 5.0, "title": 0.35,
                    "mechanism": 1.0, "terms": 0.45, "keywords": 0.65,
                    "synonyms": 0.1, "association_terms": 0.15}
ALLOWED_FIELDS = set(CONCEPT_WEIGHTS) | set(EVIDENCE_WEIGHTS)
RUNTIME_ROOT = Path(__file__).resolve().parents[1] / ".private/memory-retrieval-runtime"
TOKENIZER_VERSION = "0.42.1"
_TOKENIZERS = {}
_TOKENIZER_IDENTITY = None
_WORD_TAGS = {}
FUNCTION_CHARACTERS = set("的地得了着过和与或而但是有在把被从到对将为由及就才会能可要都再先更不未无吗呢这那我你他它其所")


class RetrievalError(ValueError):
    pass


def _bytes(value):
    return (json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n").encode()


def _norm(value):
    return re.sub(r"\s+", " ", unicodedata.normalize("NFKC", str(value or ""))).strip().casefold()


def _alias_norm(value):
    text = _norm(value)
    return re.sub(r"\s+", "", text) if HAN_RE.search(text) else text


def _ref(value):
    if not isinstance(value, Mapping):
        return None
    if isinstance(value.get("ref"), str) and SHA_RE.fullmatch(str(value.get("sha256", ""))):
        return {"ref": value["ref"], "sha256": value["sha256"]}
    return None


def _text(value):
    if isinstance(value, str):
        return value
    if isinstance(value, (list, tuple)):
        return "\n".join(x for x in value if isinstance(x, str))
    return ""


def _safe_public(value, *, label=False):
    text = _text(value).strip()
    if not text or ANSWER_RE.search(text) or PERSONAL_RE.search(text):
        return ""
    if label and (len(text) > 160 or "\n" in text):
        return ""
    return text


def _scope_text(value):
    """Excluded/prerequisite-only syllabus clauses are constraints, not hits."""
    text = _safe_public(value)
    clauses = re.split(r"[。；;，,\n]+", text)
    return "；".join(clause.strip() for clause in clauses if clause.strip() and not re.search(
        r"不讲|不涉及|不要求|不扩展|不自动|不默认|仅(?:作|为|是)?前置|只(?:作|为|是)?前置|超出|纲外|out_of_scope", clause))


def _title_synonyms(title):
    """Only orthographic normalization and explicit title acronyms are derived."""
    title = _norm(title)
    values = {title}
    # A standalone acronym in a title is an authored public designation; an
    # acronym mentioned inside a sentence is not promoted into an alias.
    values.update(x.casefold() for x in re.findall(r"(?<![\w])([A-Za-z][A-Za-z0-9+/-]{1,15})(?![\w])", title))
    without_ascii = re.sub(r"(?<![\w])[a-z][a-z0-9+/-]{1,15}(?![\w])", "", title)
    without_ascii = re.sub(r"[（）()\[\]]", " ", without_ascii)
    without_ascii = _norm(without_ascii)
    if HAN_RE.search(without_ascii) and len(without_ascii) >= 2:
        values.add(without_ascii)
    return {x for x in values if x and not ID_RE.fullmatch(x)}


def build_concept_documents(point_artifacts, entities):
    """Compile only public objective fields and verified identity metadata.

    Unique table membership does not prove that a phrase is a synonym.
    Historical association labels, expansions, ambiguity and co-occurrence
    stay separate from canonical titles and explicitly reviewed aliases.
    """
    index, objects = point_artifacts["index"], point_artifacts["objects"]
    index_provenance = {"kind": "native_index_metadata", "source_id": "native_point_index",
                        "sha256": hashlib.sha256(_bytes(index)).hexdigest()}
    if _ref(point_artifacts.get("index_ref")):
        if point_artifacts["index_ref"]["sha256"] != index_provenance["sha256"]:
            raise RetrievalError("concept_source_index_mismatch")
        index_provenance.update(_ref(point_artifacts["index_ref"]))
    entity_map = entities if isinstance(entities, Mapping) else {e["id"]: e for e in entities}
    rows, source_titles, authored_aliases = {}, {}, defaultdict(set)

    def read(reference):
        reference = _ref(reference)
        if not reference:
            return {}
        obj = objects.get(reference["ref"])
        if obj is None or hashlib.sha256(_bytes(obj)).hexdigest() != reference["sha256"]:
            raise RetrievalError("concept_source_object_mismatch")
        return obj

    def add(identity, kind, title, parent=None):
        title = _safe_public(title, label=True) or identity
        row = {"id": identity, "kind": kind, "title": title,
               "knowledge_point": parent or (identity if POINT_RE.fullmatch(identity) else None),
               "aliases": [], "ambiguous_aliases": [], "expansion_aliases": [],
               "cooccurrence_ids": [], "fields": {}, "source_refs": [],
               "authority": "public_objective_projection", "alias_rejections": {},
               "scope": {"subject": "408", "public_concept": True},
               "searchable": kind in CONCEPT_KINDS}
        rows[identity] = row
        source_titles[identity] = _norm(title)
        authored_aliases[identity].update(_title_synonyms(title) if kind == "knowledge_point"
                                          else {_norm(title)})
        return row

    def field(row, name, value, reference, source_field):
        value = _safe_public(value)
        if value:
            row["fields"][name] = value
            if _ref(reference):
                row["source_refs"].append({**_ref(reference), "field": source_field,
                                           "kind": "public_objective"})

    for identity, entry in sorted(index.get("points", {}).items()):
        protected_ref = entry.get("teaching_views", {}).get("protected")
        direct_ref = entry.get("teaching_views", {}).get("direct")
        protected, direct, summary = read(protected_ref), read(direct_ref), read(entry)
        row = add(identity, "knowledge_point", protected.get("concept_name") or entity_map.get(identity, {}).get("label"))
        field(row, "title", row["title"], protected_ref, "concept_name")
        brief = direct.get("concept_brief") or {}
        if brief.get("origin") == "general_exam_knowledge_not_personal_evidence":
            field(row, "description", brief.get("description"), direct_ref, "concept_brief.description")
            field(row, "exam_entry", brief.get("exam_entry"), direct_ref, "concept_brief.exam_entry")
        syllabus = direct.get("syllabus_context") or {}
        row["scope"]["syllabus_status"] = syllabus.get("scope_status", "unknown")
        if syllabus.get("scope_status") in {"explicit", "within_scope", "prerequisite_only"}:
            field(row, "scope", _scope_text(syllabus.get("teaching_boundary")), direct_ref, "syllabus_context.teaching_boundary")
        if syllabus.get("scope_status") in {"out_of_scope", "needs_review"}:
            row["searchable"] = False
        profile = summary.get("learning_profile") or {}
        if profile.get("status") == "current" and _ref(profile.get("profile_object")):
            reviewed = read(profile["profile_object"])
            for alias in reviewed.get("aliases", []):
                safe = _safe_public(alias, label=True)
                if safe:
                    authored_aliases[identity].add(_norm(safe))
                    row["source_refs"].append({**_ref(profile["profile_object"]), "field": "aliases", "kind": "reviewed_alias"})

    for identity, entry in sorted(index.get("subpoints", {}).items()):
        row = add(identity, "knowledge_subpoint", entry.get("title"), entry.get("knowledge_point"))
        direct_ref = entry.get("teaching_views", {}).get("direct")
        direct = read(direct_ref)
        field(row, "title", row["title"], direct_ref or entry.get("evidence_object"), "title")
        keywords = [_safe_public(x, label=True) for x in entry.get("keywords", [])]
        if any(keywords):
            row["fields"]["keywords"] = [x for x in keywords if x]
            row["source_refs"].append({**index_provenance, "field": "subpoints",
                                       "entity_id": identity, "subfield": "keywords"})
        syllabus = direct.get("syllabus_context") or {}
        row["scope"]["syllabus_status"] = syllabus.get("scope_status", "unknown")
        if syllabus.get("scope_status") in {"explicit", "within_scope", "prerequisite_only"}:
            field(row, "parent_scope", _scope_text(syllabus.get("teaching_boundary")), direct_ref, "syllabus_context.teaching_boundary")
        if syllabus.get("scope_status") in {"out_of_scope", "needs_review"}:
            row["searchable"] = False

    for identity, entry in sorted(index.get("syllabus_entries", {}).items()):
        direct_ref = entry.get("teaching_views", {}).get("direct")
        direct = read(direct_ref)
        row = add(identity, "syllabus_entry", entry.get("title") or direct.get("title"))
        field(row, "title", row["title"], direct_ref, "title")
        syllabus = direct.get("syllabus_context") or {}
        if syllabus.get("scope_status") in {"explicit", "within_scope", "prerequisite_only"}:
            field(row, "scope", _scope_text(syllabus.get("teaching_boundary")), direct_ref, "syllabus_context.teaching_boundary")
        row["scope"]["syllabus_status"] = syllabus.get("scope_status", "unknown")

    # Identity-only rows allow exact formal/exam lookup without putting any
    # problem, solution, learner history or reference image into this corpus.
    for identity, entity in sorted(entity_map.items()):
        if identity not in rows:
            row = add(identity, entity.get("kind", "identity"), identity)
            row["source_refs"] = copy.deepcopy(entity.get("source_refs", []))
            row["searchable"] = False

    known_titles = {key: {_alias_norm(alias) for alias in _title_synonyms(text)
                          if HAN_RE.search(alias) and len(alias) >= 2}
                    for key, text in source_titles.items()
                    if rows[key]["kind"] == "knowledge_point" and not ID_RE.fullmatch(text)}

    def classify(alias, owners, mode=None):
        alias = _norm(alias)
        codes = {value.upper() for value in CODE_IN_TEXT.findall(alias)}
        allowed = set(owners) | {rows[key].get("knowledge_point") for key in owners if key in rows}
        if mode == "expansion":
            allowed.update(code for code in codes if re.fullmatch(r"(?:DS|CO|OS|CN)\d{2}", code)
                           and all(owner.startswith(code + "-") for owner in owners))
        if codes - allowed:
            return "foreign_identity"
        if len(codes) > 1 or re.search(r"[;；|]", alias):
            return "cooccurrence"
        if any(alias == source_titles.get(owner) for owner in owners):
            return "safe"
        compact = _alias_norm(alias)
        titles = {key: max((title for title in labels if title in compact), key=len, default="")
                  for key, labels in known_titles.items()}
        titles = {key: title for key, title in titles.items() if title}
        if re.search(r"对比|区别|区分|混淆", alias):
            return "cooccurrence"
        if len(titles) > 1:
            if re.search(r"与|和|以及|、|/|↔|\bvs\b", alias):
                return "cooccurrence"
            # Nested labels (e.g. 树/二叉树) are not automatically two claims.
            maximal = {key for key in titles if not any(titles[key] != titles[other]
                and titles[key] in titles[other] for other in titles)}
            if len({titles[key] for key in maximal}) > 1:
                return "cooccurrence"
        if ANSWER_RE.search(alias) or PERSONAL_RE.search(alias) or not alias:
            return "non_public_label"
        return "safe"

    for identity, aliases in authored_aliases.items():
        for alias in sorted(aliases):
            reason = classify(alias, [identity])
            if reason == "safe":
                row = rows[identity]
                # Strip a source's own code prefix; it is already an identity.
                alias = _norm(re.sub(r"^" + re.escape(identity) + r"\s*", "", alias, flags=re.I))
                if alias and not ID_RE.fullmatch(alias):
                    row["aliases"].append(alias)
            else:
                rows[identity]["alias_rejections"][reason] = rows[identity]["alias_rejections"].get(reason, 0) + 1
    for alias, raw_owners in sorted(index.get("aliases", {}).items()):
        owners = sorted({key for key in raw_owners if key in rows})
        if not owners:
            continue
        mode = index.get("alias_modes", {}).get(alias, "unique" if len(owners) == 1 else "ambiguous")
        reason = classify(alias, owners, mode=mode)
        if reason != "safe":
            for identity in owners:
                row = rows[identity]
                row["alias_rejections"][reason] = row["alias_rejections"].get(reason, 0) + 1
                if reason == "cooccurrence":
                    row["cooccurrence_ids"] = sorted(set(row["cooccurrence_ids"]) | set(owners) - {identity})
            continue
        alias = _norm(alias)
        if ID_RE.fullmatch(alias) and mode != "expansion":
            continue
        for identity in owners:
            row = rows[identity]
            if mode == "expansion":
                row["expansion_aliases"].append(alias)
            elif mode == "ambiguous" or len(owners) > 1:
                row["ambiguous_aliases"].append(alias)
            elif alias not in row["aliases"]:
                row["fields"].setdefault("association_terms", []).append(alias)
            row["source_refs"].append({**index_provenance, "field": "aliases", "owner_id": identity,
                                       "alias_mode": mode})
    for row in rows.values():
        row["aliases"] = sorted(set(row["aliases"]))
        row["fields"]["synonyms"] = row["aliases"]
        row["source_refs"] = list({json.dumps(ref, sort_keys=True): ref for ref in row["source_refs"]}.values())
    return [rows[key] for key in sorted(rows)]


def build_alias_routes(documents):
    """Expose typed identity routes for the native public catalog as well.

    In particular, a KSP keyword is not an alias and a KSP title acronym is
    never extracted as a competing identity for its parent concept.
    """
    routes = {name: defaultdict(set) for name in
              ("canonical_titles", "parent_synonyms", "subpoint_titles", "syllabus_titles", "expansion", "ambiguous")}
    for row in documents:
        kind, identity = row.get("kind", "concept"), row["id"]
        if kind not in CONCEPT_KINDS:
            continue
        title = _alias_norm(row.get("title"))
        title_group = ("subpoint_titles" if kind == "knowledge_subpoint" else
                       "syllabus_titles" if kind == "syllabus_entry" else "canonical_titles")
        if title and not ID_RE.fullmatch(title):
            routes[title_group][title].add(identity)
        if kind in {"concept", "knowledge_point"}:
            for alias in row.get("aliases", []):
                key = _alias_norm(alias)
                if key and not ID_RE.fullmatch(key):
                    routes["parent_synonyms"][key].add(identity)
        for source, target in (("expansion_aliases", "expansion"), ("ambiguous_aliases", "ambiguous")):
            for alias in row.get(source, []):
                routes[target][_alias_norm(alias)].add(identity)
    return {"schema": "cs408-public-alias-routes-v1",
            **{name: {key: sorted(ids) for key, ids in sorted(rows.items())} for name, rows in routes.items()}}


def _get_tokenizer(lexicon=()):
    """An independent tokenizer per serialized dictionary; no global add_word."""
    global _TOKENIZER_IDENTITY
    vocabulary = tuple(sorted(set(lexicon)))
    key = hashlib.sha256(_bytes(vocabulary)).hexdigest()
    if key in _TOKENIZERS:
        return _TOKENIZERS[key]
    runtime = RUNTIME_ROOT / "python"
    if not (runtime / "jieba/__init__.py").is_file():
        raise RetrievalError("word_tokenizer_unavailable:install_pinned_local_jieba")
    if str(runtime) not in sys.path:
        sys.path.insert(0, str(runtime))
    with warnings.catch_warnings():
        warnings.filterwarnings("ignore", message="pkg_resources is deprecated as an API.*", category=UserWarning)
        jieba = importlib.import_module("jieba")
    if (getattr(jieba, "__version__", None) != TOKENIZER_VERSION
            or not Path(jieba.__file__).resolve().is_relative_to(runtime.resolve())):
        raise RetrievalError("word_tokenizer_runtime_identity_mismatch")
    dictionary = Path(jieba.__file__).with_name("dict.txt")
    if _TOKENIZER_IDENTITY is None:
        dictionary_bytes = dictionary.read_bytes()
        for line in dictionary_bytes.decode("utf-8").splitlines():
            parts = line.rsplit(" ", 2)
            if len(parts) == 3:
                _WORD_TAGS[parts[0].casefold()] = parts[2]
        _TOKENIZER_IDENTITY = {"name": "jieba", "version": TOKENIZER_VERSION,
            "dictionary_sha256": hashlib.sha256(dictionary_bytes).hexdigest(), "hmm": False,
            "dictionary_search_subwords": True,
            "concept_coverage": "content_words_with_source_domain_terms_preserved",
            "fallback": "within_word_ngrams_low_weight"}
    jieba.setLogLevel(logging.ERROR)
    cache = RUNTIME_ROOT / "cache"
    cache.mkdir(parents=True, exist_ok=True)
    tokenizer = jieba.Tokenizer()
    tokenizer.tmp_dir = str(cache)
    tokenizer.cache_file = "jieba-0.42.1-" + _TOKENIZER_IDENTITY["dictionary_sha256"][:12] + ".cache"
    tokenizer.initialize()
    for word in vocabulary:
        tokenizer.add_word(word, freq=max(tokenizer.FREQ.get(word, 0), 10000))
    if len(_TOKENIZERS) >= 2:
        _TOKENIZERS.pop(next(iter(_TOKENIZERS)))
    _TOKENIZERS[key] = tokenizer
    return tokenizer


def _domain_lexicon(documents):
    values = set()
    for document in documents:
        fields = document.get("fields", {})
        sources = [document.get("title", ""), *document.get("aliases", [])]
        for field in ("keywords", "terms", "mechanism"):
            value = fields.get(field, [])
            sources.extend(value if isinstance(value, list) else re.split(r"[，,；;\n]", str(value)))
        for value in sources:
            term = _norm(CODE_IN_TEXT.sub(" ", str(value)))
            count = sum(len(part) for part in HAN_RE.findall(term))
            if (1 <= count <= 8 and len(term) <= 16 and term not in STOP_TERMS
                    and re.fullmatch(r"[\u3400-\u9fffa-z0-9 ]+", term)
                    and not re.search(r"的|与|和|或者|是否|能否|不能|需要|必须|然后|先.+再", term)
                    and (count > 1 or term not in FUNCTION_CHARACTERS)):
                values.add(term)
    return sorted(values)


def _display_term(term):
    return term[1:] if term.startswith("~") else term


def _han_tokens(text, tokenizer, lexicon):
    out, unknown = [], []

    def fragments(word):
        for width in (2, 3):
            out.extend("~" + word[i:i + width] for i in range(len(word) - width + 1)
                       if word[i:i + width] not in STOP_TERMS)

    def flush():
        if unknown:
            fragments("".join(unknown))
            unknown.clear()

    for word in tokenizer.lcut(text, HMM=False):
        if len(word) == 1 and word not in lexicon and not tokenizer.FREQ.get(word, 0):
            if word in FUNCTION_CHARACTERS:
                flush()
            else:
                unknown.append(word)
            continue
        flush()
        if word not in STOP_TERMS and word not in FUNCTION_CHARACTERS:
            out.append(word)
            # Search-mode subwords are real dictionary words, unlike arbitrary
            # high-IDF character windows. This keeps a source compound and a
            # query using its constituent noun aligned without query-specific
            # synonym injections.
            out.extend(part for part in tokenizer.cut_for_search(word, HMM=False)
                       if part != word and len(part) >= 2 and part not in STOP_TERMS)
            fragments(word)
    flush()
    return out


def _tokens(text, *, lexicon=(), tokenizer=None):
    # Keep the raw ASCII case until unit/symbol parsing: 64KB and 64Kb must
    # not collapse into the same byte/bit parameter token.
    normalized = re.sub(r"\s+", " ", unicodedata.normalize("NFKC", str(text or ""))).strip()
    for noise in NOISE:
        normalized = normalized.replace(noise, " ")
    normalized = CODE_IN_TEXT.sub(" ", normalized)
    out = []
    for match in WORD_RE.finditer(normalized):
        raw_word = match.group()
        word = raw_word.casefold()
        if HAN_RE.fullmatch(word):
            tokenizer = tokenizer or _get_tokenizer(lexicon)
            out.extend(_han_tokens(word, tokenizer, lexicon))
        elif re.fullmatch(r"0x[0-9a-f]+|0b[01]+", word):
            out.append(word)
        elif PARAMETER_RE.fullmatch(raw_word):
            parameter = PARAMETER_RE.fullmatch(raw_word)
            number = parameter["number"].casefold()
            out.append(number)
            unit = parameter["unit"]
            if unit:
                canonical_units = {"bit": "b", "bits": "b", "比特": "b", "位": "b",
                    "byte": "B", "bytes": "B", "字节": "B", "秒": "s", "毫秒": "ms",
                    "微秒": "us", "纳秒": "ns", "μs": "us", "µs": "us", "赫兹": "hz"}
                if unit in {"B", "b"}:
                    canonical = unit
                elif re.fullmatch(r"[kmgtpe]i?[Bb]", unit, re.I):
                    canonical = unit[:-1].casefold() + unit[-1]
                else:
                    canonical = canonical_units.get(unit.casefold(), unit.casefold())
                out.append(number + canonical)
        elif CODE_OPERATOR_RE.fullmatch(word):
            out.append(word)
        else:
            pieces = re.split(r"[./-]", raw_word)
            left = normalized[:match.start()].rstrip()
            right = normalized[match.end():].lstrip()
            code_word_context = (bool(right and (right[0] == "(" or HAN_RE.match(right)))
                                 or bool(left and HAN_RE.fullmatch(left[-1])))
            if len(word) > 1 and (word not in STOP_TERMS or code_word_context):
                out.append(word)
            for piece in pieces:
                value = piece.casefold()
                if PARAMETER_RE.fullmatch(piece):
                    out.append(value)
                    continue
                if len(value) > 1:
                    if len(pieces) > 1 and value not in STOP_TERMS:
                        out.append(value)
                    continue
                if not re.fullmatch(r"[a-z]", value):
                    continue
                left = normalized[:match.start()].rstrip()
                right = normalized[match.end():].lstrip()
                code_context = (len(pieces) > 1 or
                    bool(left and re.search(r"[\u3400-\u9fff(\[{}=<>+*/%&|^~,:;\-]$", left)) or
                    bool(right and re.match(r"[\u3400-\u9fff()\[\]{}=<>+*/%&|^~,:;\-]", right)) or
                    bool(re.search(r"\b(?:int|char|float|double|bool|var|variable)\s*$", left, re.I)))
                if code_context or (piece.isupper() and value not in {"a", "i"}):
                    out.append(value)
    return out


def _parameter_token(term):
    return bool(re.fullmatch(r"0x[0-9a-f]+|0b[01]+", term, re.I) or PARAMETER_RE.fullmatch(term))


def _term_weight(term):
    if term.startswith("~"):
        return 0.12
    if re.fullmatch(NUMBER_PATTERN, term):
        return 0.35
    if _parameter_token(term):
        return 0.9
    if len(term) == 1 or CODE_OPERATOR_RE.fullmatch(term):
        return 0.5
    return 1.0


def _concept_coverage_terms(terms, lexicon):
    """Query syntax is not a required mechanism condition.

    All tokens still participate in scoring. Only the coverage denominator
    excludes grammatical fillers by the frozen general-language dictionary;
    source-authored domain terms, parameters and code symbols are preserved.
    No case-specific nouns or paraphrases are registered here.
    """
    domain = set(lexicon)
    selected = set()
    for term in terms:
        if term.startswith("~"):
            continue
        if term in domain or _parameter_token(term) or re.search(r"[a-zA-Z]", term) or CODE_OPERATOR_RE.fullmatch(term):
            selected.add(term)
            continue
        tag = _WORD_TAGS.get(term, "")
        if tag[:1] in {"u", "p", "c", "r", "d", "f", "m"} or len(term) == 1:
            continue
        selected.add(term)
    return selected


def _negative_terms(text, terms, *, lexicon=(), tokenizer=None):
    normalized = _norm(text)
    negative = set()
    candidates = set()
    negative_pattern = r"不能|不会|没有|无需|不必|并非|并不|不是|无法|尚未|未曾|未能|不得|不可|未|不|无|没"
    uncertainty_pattern = r"是否|是不是|能否|会不会|不一定|不代表一定|未必|不确定|不清楚"
    segments = []
    for segment in re.finditer(r"[^，,。.;；!?！？\n]+", normalized):
        clause = segment.group()
        tokenizer = tokenizer or _get_tokenizer(lexicon)
        words = list(tokenizer.tokenize(clause, mode="default", HMM=False))
        boundaries = {position for _, start, end in words for position in (start, end)}
        def cues(pattern):
            return [cue for cue in re.finditer(pattern, clause)
                    if cue.start() in boundaries and cue.end() in boundaries]
        uncertain = cues(uncertainty_pattern)
        events = [(cue.start(), "uncertain") for cue in uncertain]
        events.extend((cue.start(), "positive") for cue in cues(r"而是|但是|然而|不过|但"))
        events.extend((cue.start(), "negative") for cue in cues(negative_pattern)
                      if not any(span.start() <= cue.start() < span.end() for span in uncertain))
        events.sort(key=lambda item: item[0])
        segments.append((segment.start(), [position for position, _ in events], [state for _, state in events]))
        if any(state == "negative" for _, state in events):
            candidates.update(_tokens(clause, lexicon=lexicon, tokenizer=tokenizer))
    starts = [segment[0] for segment in segments]
    for term in candidates & set(terms):
        flags = []
        for match in re.finditer(re.escape(_display_term(term).casefold()), normalized):
            number = bisect_right(starts, match.start()) - 1
            start, positions, states = segments[number]
            event = bisect_left(positions, match.start() - start) - 1
            flags.append(event >= 0 and states[event] == "negative")
        if flags and all(flags):
            negative.add(term)
    return negative


def _family(document):
    return str(document.get("knowledge_point") or document.get("family") or document["id"])


def posting_shard(term):
    return hashlib.sha256(term.encode()).hexdigest()[:2]


def compile_index(documents, *, include_documents=True, shard_count=0):
    """Compile postings and field lengths; indexed bodies are never retained.

    include_documents controls safe result metadata only, never source prose.
    shard_count=256 separates term postings for immutable lazy loading.  A
    publisher may replace postings_shards with references and provide the
    corresponding load_postings(prefix) callback to rank_documents.
    """
    if shard_count not in {0, 256}:
        raise RetrievalError("shard_count_must_be_zero_or_256")
    docs = list(documents)
    if any(not isinstance(row, Mapping) or not isinstance(row.get("id"), str) or not row["id"] for row in docs):
        raise RetrievalError("duplicate_or_invalid_document_identity")
    docs.sort(key=lambda row: row["id"])
    ids = [row["id"] for row in docs]
    if len(ids) != len(set(ids)) or any(not isinstance(i, str) or not i for i in ids):
        raise RetrievalError("duplicate_or_invalid_document_identity")
    lexicon = _domain_lexicon(docs)
    tokenizer = _get_tokenizer(lexicon)
    lengths, postings = defaultdict(lambda: [0] * len(docs)), defaultdict(lambda: defaultdict(list))
    aliases, ambiguous, expansions, negative = defaultdict(list), defaultdict(list), defaultdict(list), defaultdict(list)
    metadata = {}
    for number, doc in enumerate(docs):
        if include_documents:
            metadata[str(number)] = {key: copy.deepcopy(doc[key]) for key in
                ("title", "source_refs", "scope", "knowledge_point") if key in doc}
        for source, target in ((doc.get("aliases", []), aliases), (doc.get("ambiguous_aliases", []), ambiguous),
                               (doc.get("expansion_aliases", []), expansions)):
            for alias in source:
                normalized = _alias_norm(alias)
                if normalized:
                    target[normalized].append(number)
        if not doc.get("searchable", True):
            continue
        fields = {"title": doc.get("title", ""), **doc.get("fields", {})}
        all_text, all_terms = [], set()
        for field, value in fields.items():
            if field not in ALLOWED_FIELDS:
                continue
            text = _text(value)
            counter = Counter(_tokens(text, lexicon=lexicon, tokenizer=tokenizer))
            lengths[field][number] = round(sum(count * _term_weight(term) for term, count in counter.items()), 3)
            for term, count in counter.items():
                postings[term][field].extend((number, min(count, 255)))
            if field not in {"association_terms", "terms", "scope", "parent_scope"}:
                all_text.append(text)
                all_terms.update(counter)
        for term in _negative_terms("\n".join(all_text), all_terms, lexicon=lexicon, tokenizer=tokenizer):
            negative[term].append(number)
    result = {"schema": SCHEMA, "version": VERSION, "doc_ids": ids,
        "doc_kinds": [doc.get("kind", "concept") for doc in docs],
        "families": [_family(doc) for doc in docs],
        "searchable": [bool(doc.get("searchable", True)) for doc in docs],
        "field_lengths": dict(lengths), "metadata": metadata,
        "aliases": dict(aliases), "ambiguous_aliases": dict(ambiguous), "expansion_aliases": dict(expansions),
        "identity_lookup": {_norm(identity): number for number, identity in enumerate(ids)},
        "document_count": len(ids), "contains_document_bodies": False, "shard_count": shard_count,
        "domain_lexicon": lexicon, "tokenizer": {**_TOKENIZER_IDENTITY,
            "domain_lexicon_sha256": hashlib.sha256(_bytes(lexicon)).hexdigest()}}
    id_numbers = {identity: number for number, identity in enumerate(ids)}
    alias_routes = build_alias_routes(docs)
    result["alias_routes"] = {name: {alias: [id_numbers[identity] for identity in owners]
                                    for alias, owners in mapping.items()}
                              for name, mapping in alias_routes.items() if name != "schema"}
    packed = {term: {"fields": dict(fields), "negative": negative.get(term, [])}
              for term, fields in postings.items()}
    if shard_count:
        shards = defaultdict(dict)
        for term, value in packed.items():
            shards[posting_shard(term)][term] = value
        result["postings_shards"] = dict(shards)
    else:
        result["postings"] = packed
    return result


def rank_documents(query, documents, limit=8, kind="concept", *, allowed_ids=None,
                   current_question_id=None, view="protected", load_postings: Callable | None = None):
    """Rank only the permitted candidate set; relevance is not truth/mastery."""
    if kind not in {"concept", "evidence"} or view not in {"protected", "after_attempt", "direct"}:
        raise RetrievalError("invalid_retrieval_kind_or_view")
    if type(limit) is not int or not 1 <= limit <= 256:
        raise RetrievalError("limit_must_be_between_1_and_256")
    if not isinstance(query, str) or not query.strip() or len(query) > 2000:
        raise RetrievalError("invalid_query")
    base = {"schema": "cs408-lexical-candidates-v1", "candidates": [], "ambiguity": None,
            "matched_terms": [], "score_kind": "lexical_relevance_not_probability", "write_count": 0}
    if (kind == "evidence" and view != "direct") or (kind == "concept" and current_question_id and view == "protected"):
        return {**base, "status": "withheld", "reason": "protected_current_question_or_private_evidence"}
    index = documents if isinstance(documents, Mapping) and documents.get("schema") == SCHEMA else compile_index(documents)
    if index.get("version") != VERSION:
        raise RetrievalError("retrieval_index_version_mismatch")
    ids, kinds = index["doc_ids"], index["doc_kinds"]
    allowed = set(allowed_ids) if allowed_ids is not None else None
    eligible = {n for n, identity in enumerate(ids) if (allowed is None or identity in allowed)
                and (kinds[n] == "evidence" if kind == "evidence" else kinds[n] in CONCEPT_KINDS)}
    normalized = _norm(query)

    def candidate(number, score, match_type, fields=None, terms=None, coverage=None):
        meta = copy.deepcopy(index.get("metadata", {}).get(str(number), {}))
        return {**meta, "id": ids[number], "kind": kinds[number], "score": round(score, 6),
                "match_type": match_type, "matched_fields": fields or [], "matched_terms": terms or [],
                **({"query_coverage": round(coverage, 4)} if coverage is not None else {})}

    exact = index["identity_lookup"].get(normalized)
    if exact is not None:
        if (kinds[exact] == "evidence") != (kind == "evidence"):
            return {**base, "status": "no_match", "reason": "identity_outside_requested_kind"}
        if allowed is not None and ids[exact] not in allowed:
            return {**base, "status": "no_match", "reason": "identity_outside_allowed_scope"}
        return {**base, "status": "ready", "reason": "exact_identity", "candidates": [candidate(exact, 1.0, "exact_identity")]}
    exact_like = bool(ID_RE.fullmatch(normalized))
    if kind == "concept" and _parameter_token(normalized.replace(" ", "")):
        return {**base, "status": "no_match", "reason": "numeric_parameter_requires_concept_context"}
    # Canonical code aliases must never redirect a nonexistent concept ID.
    if exact_like and kind == "concept" and normalized not in index.get("expansion_aliases", {}):
        return {**base, "status": "no_match", "reason": "unknown_exact_identity"}
    if kind == "concept":
        mentioned = list(dict.fromkeys(_norm(identity) for identity in CODE_IN_TEXT.findall(query)))
        if mentioned:
            unknown = [identity for identity in mentioned if identity not in index["identity_lookup"]
                       and identity not in index.get("expansion_aliases", {})]
            if unknown:
                return {**base, "status": "no_match", "reason": "unknown_exact_identity"}
            explicit = [index["identity_lookup"][identity] for identity in mentioned
                        if identity in index["identity_lookup"]]
            if any((allowed is not None and ids[number] not in allowed) or kinds[number] == "evidence"
                   for number in explicit):
                return {**base, "status": "no_match", "reason": "identity_outside_allowed_scope"}
            selected = explicit
            if selected:
                return {**base, "status": "ready", "reason": "explicit_identity_set",
                        "candidates": [candidate(n, 1.0, "exact_identity") for n in selected[:limit]]}
        route_table = index.get("alias_routes", {})
        route_order = (("canonical_titles", "exact_title"), ("parent_synonyms", "exact_synonym"),
                       ("subpoint_titles", "exact_subpoint_title"), ("expansion", "scope_expansion"),
                       ("ambiguous", "ambiguous_alias"), ("syllabus_titles", "exact_syllabus_title"))
    else:
        route_table = index
        route_order = (("aliases", "exact_locator"),)
    for field, mode in route_order:
        numbers = sorted(set(route_table.get(field, {}).get(_alias_norm(normalized), [])) & eligible)
        if numbers:
            is_ambiguous = len(numbers) != 1 or mode in {"scope_expansion", "ambiguous_alias"}
            return {**base, "status": "ambiguous" if is_ambiguous else "ready", "reason": mode,
                "ambiguity": {"type": mode, "candidate_count": len(numbers), "requires_scope_check": True} if is_ambiguous else None,
                "candidates": [candidate(n, 1.0, mode, [field], [normalized]) for n in numbers[:limit]],
                "matched_terms": [normalized]}
    if exact_like:
        return {**base, "status": "no_match", "reason": "unknown_exact_identity"}
    if not eligible:
        return {**base, "status": "no_match", "reason": "empty_allowed_scope"}
    weights = EVIDENCE_WEIGHTS if kind == "evidence" else CONCEPT_WEIGHTS
    lexicon = index.get("domain_lexicon", [])
    tokenizer = _get_tokenizer(lexicon)
    expected_tokenizer = {**_TOKENIZER_IDENTITY, "domain_lexicon_sha256": hashlib.sha256(_bytes(lexicon)).hexdigest()}
    if index.get("tokenizer") != expected_tokenizer:
        raise RetrievalError("query_index_tokenizer_mismatch")
    terms = set(_tokens(query, lexicon=lexicon, tokenizer=tokenizer))
    if not terms:
        return {**base, "status": "no_match", "reason": "no_informative_query_terms"}
    if kind == "concept" and all(_parameter_token(term) for term in terms):
        return {**base, "status": "no_match", "reason": "numeric_parameter_requires_concept_context"}
    if kind == "concept" and all(re.fullmatch(r"[a-z]", term) or CODE_OPERATOR_RE.fullmatch(term) for term in terms):
        return {**base, "status": "no_match", "reason": "single_symbol_requires_concept_context"}
    shard_cache = {}

    def posting(term):
        if "postings" in index:
            return index["postings"].get(term, {})
        prefix = posting_shard(term)
        if prefix not in shard_cache:
            if load_postings is not None:
                shard_cache[prefix] = load_postings(prefix) or {}
            else:
                shard_cache[prefix] = index.get("postings_shards", {}).get(prefix, {})
        return shard_cache[prefix].get(term, {})

    averages = {field: max(1.0, sum(values[n] for n in eligible) / len(eligible))
                for field, values in index["field_lengths"].items()}
    scores, matches, fields_hit, core_hits = defaultdict(float), defaultdict(set), defaultdict(set), defaultdict(set)
    query_negative = _negative_terms(query, terms, lexicon=lexicon, tokenizer=tokenizer)
    idfs, supported = {}, set()
    for term in sorted(terms):
        payload = posting(term)
        field_postings = payload.get("fields", {})
        found = {flat[i] for flat in field_postings.values() for i in range(0, len(flat), 2) if flat[i] in eligible}
        if not found:
            continue
        supported.add(term)
        idf = math.log1p((len(eligible) - len(found) + 0.5) / (len(found) + 0.5))
        idfs[term] = idf * _term_weight(term)
        weighted_tf = defaultdict(float)
        for field, flat in field_postings.items():
            weight = weights.get(field, 0.0)
            if not weight:
                continue
            for offset in range(0, len(flat), 2):
                number, tf = flat[offset:offset + 2]
                if number not in eligible:
                    continue
                length = index["field_lengths"][field][number]
                weighted_tf[number] += weight * tf / (0.45 + 0.55 * length / averages[field])
                fields_hit[number].add(field)
                if field not in {"terms", "association_terms", "synonyms", "scope", "parent_scope"}:
                    core_hits[number].add(term)
        negative = set(payload.get("negative", []))
        for number, tf in weighted_tf.items():
            polarity = 0.55 if ((term in query_negative) != (number in negative)) else 1.0
            scores[number] += idfs[term] * (tf * 2.2 / (tf + 1.2)) * polarity
            matches[number].add(term)
    if not supported:
        return {**base, "status": "no_match", "reason": "no_source_grounded_terms"}
    primary_terms = _concept_coverage_terms(terms, lexicon)
    supported_primary = primary_terms & supported
    coverage_terms = supported_primary or supported
    denominator = sum(idfs[term] for term in coverage_terms)
    ranked = []
    for number, score in scores.items():
        coverage = sum(idfs[t] for t in matches[number] & coverage_terms) / max(denominator, 1e-9)
        core_primary = {term for term in core_hits[number] if not term.startswith("~")}
        raw_coverage = len(matches[number] & primary_terms) / max(1, len(primary_terms))
        anchor = any(len(term) >= 2 and not _parameter_token(term) for term in core_primary)
        discriminative = sum(idfs[t] >= 0.45 for t in core_primary)
        single_term_search = len(primary_terms) == 1 and discriminative >= 1
        if not core_hits[number] or (not anchor and discriminative < 2 and not single_term_search) or coverage < 0.28 or raw_coverage < 0.12:
            continue
        ranked.append((number, score * (0.6 + 0.4 * coverage), coverage))
    ranked.sort(key=lambda item: (-item[1], ids[item[0]]))
    if not ranked:
        return {**base, "status": "no_match", "reason": "insufficient_lexical_evidence", "matched_terms": sorted(supported)}
    # Exclude weak incidental matches before diversity. Never fill the result
    # list with unrelated material merely to reach its requested size.
    ranked = [item for item in ranked if item[1] >= ranked[0][1] * 0.24]
    selected, family_counts = [], Counter()
    remaining = list(ranked)
    while remaining and len(selected) < limit:
        position = max(range(len(remaining)), key=lambda i: remaining[i][1] *
                       (0.72 ** family_counts[index["families"][remaining[i][0]]]))
        item = remaining.pop(position)
        family = index["families"][item[0]]
        if kind == "concept" and family_counts[family] >= 2:
            continue
        selected.append(item)
        family_counts[family] += 1
    distinct_second = next((item for item in ranked[1:]
                            if index["families"][item[0]] != index["families"][ranked[0][0]]), None)
    ambiguous = bool(distinct_second and distinct_second[1] >= ranked[0][1] * 0.88)
    result = [candidate(n, score, "fielded_bm25", sorted(fields_hit[n]), sorted({_display_term(t) for t in matches[n]}), coverage)
              for n, score, coverage in selected]
    return {**base, "status": "ambiguous" if ambiguous else "ready", "reason": "near_tied_concepts" if ambiguous else "source_grounded_lexical_match",
        "ambiguity": {"type": "near_tie", "requires_scope_check": True} if ambiguous else None,
        "candidates": result, "matched_terms": sorted({_display_term(t) for t in supported}),
        "ranking_scope_count": len(eligible), "postings_shards_read": sorted(shard_cache)}
