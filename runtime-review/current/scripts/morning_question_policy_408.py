#!/usr/bin/env python3
"""Small deterministic ordering policy for future web-D morning returns."""
from __future__ import annotations

import re
from typing import Any, Iterable
from pathlib import Path
import copy
import datetime as dt
import hashlib
import json


CANONICAL_RE = re.compile(r"EXAM408-[0-9]{4}-Q[0-9]{2}(?:-P[0-9]{2})?")
FORMAL_ORIGINAL_RE = re.compile(r"FOS-[0-9A-F]{24}")
VARIANT_RE = re.compile(r"EVB-[0-9A-F]{24}")

SOURCE_ORDER = (
    "canonical_true_exam",
    "formal_source_original",
    "identity_unclassified",
    "variant",
)
RECENCY_POLICY = "oldest_verified_actual_contact_first_unknown_last"
KEYWORD_POLICY = "recent_errors_keyword_groups_oldest_equivalent_unknown_last_v2"
FREQUENCY_KEYWORD_POLICY = "high_frequency_then_keyword_groups_oldest_equivalent_unknown_last_v1"


def user_excluded_exams(repo):
    path = Path(repo) / "wiki/study_vaults/408-full/state/excluded-exam-items.json"
    if not path.exists():
        return {}
    rows = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(rows, dict) or any(not CANONICAL_RE.fullmatch(k) for k in rows):
        raise ValueError("invalid_user_excluded_exam_items")
    return rows


def apply_user_exclusions(value, repo):
    result = copy.deepcopy(value)
    excluded = user_excluded_exams(repo)
    result["user_excluded_exam_items"] = excluded
    for rows in result.get("candidates_by_formal_id", {}).values():
        for row in rows:
            if row.get("true_item_id") in excluded or str(row.get("true_item_id", "")).split("-P")[0] in excluded:
                row["eligible"] = False
                row["rejection_reasons"] = list(dict.fromkeys(
                    [*row.get("rejection_reasons", []), "user_permanently_excluded"]))
    for identity, row in result.get("canonical_anchor_eligibility", {}).items():
        if identity in excluded or identity.split("-P")[0] in excluded:
            row.update(production_usable=False, direct_replay_allowed=False,
                       style_observation_allowed=False, exclusion_reason="user_permanently_excluded")
    return result


def verified_exact_original_ids(node, formal_id, library):
    """Corroborate a frozen alias with its canonical original mapping."""
    aliases = set((library.get("question_refs", {}).get(formal_id) or {}).get("canonical_ids", []))
    mapped = {row["question_id"] for row in library.get("original_mappings", [])
              if formal_id in row.get("formal_ids", [])}
    return set(node.get("exact_sources", [])) | (aliases & mapped)


def ordered_learning_items(selected, original_surfaces, supplements, contacts, due_version, *, keyword_metadata=None):
    """One future D queue, retaining exact-original versus concept-only identity."""
    result = []
    for item in selected:
        points = item.get("review_target_concept_ids") or []
        if len(points) != 1:
            raise ValueError("D_ordered_original_requires_one_concept")
        surface = original_surfaces[item["question_id"]]
        material = [due_version, item["question_id"], points[0],
                    surface["true_item_id"], surface["surface_sha256"]]
        identity = hashlib.sha256(json.dumps(material, separators=(",", ":")).encode()).hexdigest()
        result.append({"request_id": "DORIG-" + identity[:24].upper(),
                       "request_kind": "due_representative_original",
                       "question_id": item["question_id"], "concept_id": points[0],
                       "execution_kind": "original_review", "selection": copy.deepcopy(surface)})
    result.extend({**copy.deepcopy(row), "execution_kind": "concept_supplement"}
                  for row in supplements)
    identities = [row["selection"]["true_item_id"] for row in result]
    if len(set(identities)) != len(identities) or any(not CANONICAL_RE.fullmatch(x) for x in identities):
        raise ValueError("D_ordered_queue_requires_unique_true_exams")
    result.sort(key=lambda row: contact_order_key(row["selection"]["true_item_id"], contacts))
    for row in result:
        row["last_learning_contact"] = copy.deepcopy(contacts.get(row["selection"]["true_item_id"]))
    if keyword_metadata is not None:
        from morning_selection_408 import normalized_keywords
        for row in result:
            identity = row["selection"]["true_item_id"]
            metadata = keyword_metadata.get(identity) or {}
            keywords = normalized_keywords(metadata.get("keywords", []))
            if not keywords or not metadata.get("recent_error_match"):
                raise ValueError("D_keyword_ordering_registered_metadata_required:" + identity)
            row["keywords"] = keywords
            row["recent_error_match"] = copy.deepcopy(metadata["recent_error_match"])
            row["parent_true_item_id"] = metadata.get("parent_true_item_id")
            if metadata.get("frequency_priority"):
                row["frequency_priority"] = copy.deepcopy(metadata["frequency_priority"])
        from exam_frequency_priority_408 import rank
        result.sort(key=lambda row: (rank(row), tuple(row["keywords"]),
                    *contact_order_key(row["selection"]["true_item_id"], contacts)))
    return result

def frozen_display_order_key(row, contacts, frozen_order):
    """Keep registered frequency ahead of keyword/date display tie-breaks."""
    from exam_frequency_priority_408 import rank
    identity = row['selection']['true_item_id']
    return (rank(row), tuple(row.get('keywords', [])),
            *contact_order_key(identity, contacts)[:2], frozen_order[identity])


def true_exam_only_eligibility(value: dict[str, Any]) -> dict[str, Any]:
    """Keep exact exposure rejections and disallow non-exam fallback surfaces."""
    result = copy.deepcopy(value)
    result["selection_policy"] = "true_exams_only_exact_item_cooldown_7_days"
    for rows in result.get("candidates_by_formal_id", {}).values():
        for row in rows:
            if source_class(row) != "canonical_true_exam":
                row["eligible"] = False
                row["rejection_reasons"] = list(dict.fromkeys(
                    [*row.get("rejection_reasons", []), "true_exam_only_policy"]))
    return result


def recent_formal_exams(nodes: dict[str, Any], question_refs: dict[str, Any], target_date: str,
                        *, window_days: int | None = 7) -> dict[str, Any]:
    """Bind recent formal study dates to exact exam aliases, never to a topic."""
    day = dt.date.fromisoformat(target_date)
    recent = {}
    for fid, node in nodes.items():
        row = node.get("row") or {}
        dates = []
        for field in ("首次做题日期", "最近复做日期"):
            try:
                dates.append(dt.date.fromisoformat(row.get(field, "")))
            except (ValueError, TypeError):
                pass
        if not dates or (window_days is not None and (day - max(dates)).days >= window_days):
            continue
        last = max(dates)
        identities = set(node.get("exact_sources", [])) | set((question_refs.get(fid) or {}).get("canonical_ids", []))
        for identity in identities:
            if not CANONICAL_RE.fullmatch(identity):
                continue
            if identity not in recent or recent[identity]["study_date"] < last.isoformat():
                recent[identity] = {"study_date": last.isoformat(), "formal_id": fid,
                    "formal_node_sha256": node.get("formal_node_sha256"),
                    "gap_days": (day - last).days,
                    "eligible_on": (last + dt.timedelta(days=7)).isoformat()}
    return recent


def _contact_binding_set_hash(identities):
    return hashlib.sha256(("\n".join(sorted(identities)) + "\n").encode()).hexdigest()


def prepare_hot_capture_contact_proof(repo: Path, study_date: str, *, native_original_ids=None) -> dict[str, Any]:
    """Certify immutable source bindings once while their canonical sources are available.

    This is proof of source identity, not a cached table of latest study dates.
    Include it in the trusted, hash-bound learning handoff. No learner write.
    """
    observed = set()
    recent_verified_capture_exams(repo, study_date, window_days=None, _verified_out=observed,
                                  native_original_ids=native_original_ids)
    return {"schema": "cs408-hot-capture-source-proof-v1", "repo": str(Path(repo).resolve()),
            "binding_count": len(observed), "binding_set_sha256": _contact_binding_set_hash(observed)}


def recent_verified_capture_exams(repo: Path, target_date: str, *, window_days: int | None = 7,
                                 hot_capture_proof: dict[str, Any] | None = None,
                                 _verified_out: set[str] | None = None, native_original_ids=None) -> dict[str, Any]:
    """Read reviewed source identities for concept-only/non-node Captures.

    These are source mappings, not new displays, attempts or mastery records.
    """
    root = Path(repo) / '.private/exam-source-bindings-v1'
    if native_original_ids is not None and (_verified_out is None or hot_capture_proof is not None):
        raise ValueError('native_exam_contact_requires_proof_builder')
    files = sorted(root.glob('*.json'))
    if hot_capture_proof is not None:
        if (_verified_out is not None or hot_capture_proof.get("schema") != "cs408-hot-capture-source-proof-v1"
                or hot_capture_proof.get("repo") != str(Path(repo).resolve())
                or hot_capture_proof.get("binding_count") != len(files)
                or hot_capture_proof.get("binding_set_sha256") != _contact_binding_set_hash(p.stem for p in files)):
            raise ValueError("hot_capture_source_proof_changed_refresh_required")
    if not files:
        return {}
    import intake_fact_capture_408 as captures
    import study_bridge_408 as published
    state = captures.replay(captures._load_events(captures.capture_root(repo)))['captures']
    day = dt.date.fromisoformat(target_date)
    libraries, result = {}, {}
    for path in files:
        raw = path.read_bytes()
        if path.is_symlink() or hashlib.sha256(raw).hexdigest() != path.stem:
            raise ValueError('exam_source_binding_hash_drift')
        row = json.loads(raw)
        capture = state.get(row['capture_id']) or {}
        if (row.get('schema') != 'reviewed-capture-exam-source-v1'
                or row['study_date'] != capture.get('study_date')
                or row['capture_payload_sha256'] != capture.get('payload_sha256')
                or not CANONICAL_RE.fullmatch(row['canonical_id'])):
            raise ValueError('exam_source_binding_capture_drift')
        additional = row.get('binding_role') == 'additional_result_exposure'
        if row.get('binding_role') not in (None, 'additional_result_exposure'):
            raise ValueError('exam_source_binding_role_invalid')
        if additional:
            from recent_contact_408 import validate_result_exposure_binding
            if hot_capture_proof is None:
                validate_result_exposure_binding(repo, row, capture)
            else:
                # The immutable turns/locator were checked at preparation. The
                # live capture must still bind this evidence and the same parent.
                validate_result_exposure_binding(repo, row, capture, verified_immutable_turns=True)
        gap = (day - dt.date.fromisoformat(row['study_date'])).days
        if window_days is not None and gap >= window_days:
            continue
        if native_original_ids is not None:
            # The original source review is already recorded in these immutable,
            # Capture-bound receipts. Reopen the current accepted exam identity
            # and the original evidence above without resolving a replica version.
            if (row['canonical_id'] not in native_original_ids or not str(row.get('reviewed_by') or '').strip()
                    or not str(row.get('basis') or '').strip()
                    or not re.fullmatch(r'[0-9a-f]{40}', str(row.get('source_commit') or ''))
                    or not re.fullmatch(r'[0-9a-f]{64}', str(row.get('original_mapping_sha256') or ''))):
                raise ValueError('native_exam_source_binding_unverified:' + path.stem)
        elif hot_capture_proof is None:
            commit = row['source_commit']
            if commit not in libraries:
                library = published._published_library(commit, repo, original_mappings_only=True)
                libraries[commit] = {r['question_id']: r for r in library['original_mappings']}
            original = libraries[commit].get(row['canonical_id'])
            digest = hashlib.sha256((json.dumps(original, ensure_ascii=False, sort_keys=True,
                separators=(',', ':'))+'\n').encode()).hexdigest()
            if original is None or digest != row['original_mapping_sha256']:
                raise ValueError('exam_source_binding_canonical_drift')
        if _verified_out is not None:
            _verified_out.add(path.stem)
        if row['study_date'] <= result.get(row['canonical_id'], {}).get('study_date', ''):
            continue
        result[row['canonical_id']] = {'study_date': row['study_date'], 'gap_days': gap,
            'eligible_on': (dt.date.fromisoformat(row['study_date'])+dt.timedelta(days=7)).isoformat(),
            'capture_id': row['capture_id'], 'source_binding_sha256': path.stem,
            'basis': 'verified_recent_capture_source_not_an_extra_attempt'}
        if additional:
            result[row['canonical_id']].update(binding_role='additional_result_exposure',
                basis='verified_additional_result_exposure_not_an_extra_attempt')
    return result


def last_true_exam_contacts(repo, question_refs, target_date, release_dir, *, hot_capture_proof=None):
    """Latest actual study/display of each exact exam, never import or mtime."""
    import question_exposure_408 as exposure
    import start_morning_coverage_campaign_408 as control
    import exam_variant_bank_408 as bank
    day = dt.date.fromisoformat(target_date)
    contacts = recent_formal_exams(control._node_snapshot(repo), question_refs, target_date,
                                  window_days=None)
    for value in contacts.values():
        value['basis'] = 'formal_actual_study_date'
    def accept(identity, fact):
        if (CANONICAL_RE.fullmatch(str(identity or '')) and fact['study_date'] <= target_date
                and fact['study_date'] > contacts.get(identity, {}).get('study_date', '')):
            contacts[identity] = fact
    options = {"hot_capture_proof": hot_capture_proof} if hot_capture_proof is not None else {}
    for identity, fact in recent_verified_capture_exams(repo, target_date, window_days=None, **options).items():
        accept(identity, fact)
    release = bank._release_snapshot(release_dir, require_final_production_release=False)
    raw = exposure.recent_exposure_set(repo, day, window_days=(day-dt.date(1970,1,1)).days+1)
    mapped = control.map_recent_exposures(raw, release)
    for row in mapped['rows']:
        accept(row.get('true_item_id'), {'study_date': row['review_date'],
            'basis': 'verified_actual_display', 'receipt_sha256': row['receipt_sha256'],
            'gap_days': row['gap_days']})
    # Include independently correct outcomes and bindings even when formal
    # recency deliberately remains unchanged. Used again by native D ordering.
    from recent_contact_408 import real_contacts
    for identity, fact in real_contacts(repo, day, strict=True, **options)['by_identity'].items():
        accept(identity, fact)
    return contacts


def contact_order_key(identity, contacts):
    value = contacts.get(identity) or {}
    day = value.get('study_date')
    # Unknown is explicitly separate; it is not proof of an ancient contact.
    return (day is None, day or '9999-12-31', identity)


def apply_formal_cooldown(value: dict[str, Any], recent: dict[str, Any]) -> dict[str, Any]:
    result = copy.deepcopy(value)
    result["recent_exact_formal_study"] = recent
    result.setdefault("snapshot", {})["formal_recency_sha256"] = hashlib.sha256(
        json.dumps(recent, sort_keys=True, ensure_ascii=False, separators=(",", ":")).encode()).hexdigest()
    for rows in result.get("candidates_by_formal_id", {}).values():
        for row in rows:
            fact = recent.get(row.get("true_item_id"))
            if fact is not None:
                row["eligible"] = False
                row["rejection_reasons"] = list(dict.fromkeys([*row.get("rejection_reasons", []), "exact_formal_study_cooldown"]))
                gap = row.get("cooldown_gap_days")
                if gap is None or fact["gap_days"] < gap:
                    row.update(cooldown_gap_days=fact["gap_days"], eligible_on=fact["eligible_on"])
                row["formal_study_evidence"] = fact
    return result


def require_true_exam_items(body: dict[str, Any], question_refs: dict[str, Any] | None = None,
                            recent_exams: dict[str, Any] | None = None, *, repo=None) -> None:
    """Future D preparation cannot introduce variants or unverifiable originals."""
    if body.get("web_variants"):
        raise ValueError("D_true_exam_only_web_variants_forbidden")
    seen = set()
    excluded = user_excluded_exams(repo) if repo is not None else {}
    for collection in ("selected_items", "supplemental_items", "substitute_items"):
        for item in body.get(collection, []):
            if source_class(item) != "canonical_true_exam":
                raise ValueError("D_true_exam_only_source_required:" + str(
                    item.get("question_id") or item.get("request_id")))
            identity = item.get("preferred_true_item_id") or item.get("true_item_id")
            if identity in excluded or str(identity).split("-P")[0] in excluded:
                raise ValueError("D_true_exam_user_permanently_excluded:" + identity)
            if identity in (recent_exams or {}):
                raise ValueError("D_exact_true_exam_used_within_7_days:" + identity)
            if collection == "selected_items" and question_refs is not None:
                if re.fullmatch(r"EXAM408-[0-9]{4}-Q[0-9]{2}-P[0-9]{2}", str(identity)):
                    raise ValueError("D_subquestion_requires_concept_supplement:" + identity)
                exact = (question_refs.get(item.get("question_id")) or {}).get("canonical_ids", [])
                if identity not in exact:
                    raise ValueError("D_alternative_true_exam_requires_substitute_item:" + identity)
            if identity in seen:
                raise ValueError("D_duplicate_true_exam_identity:" + identity)
            seen.add(identity)


def source_class(item: dict[str, Any]) -> str:
    """Classify only from the bound surface identity, never from review flags."""
    if item.get("web_variant_id") or item.get("variant_id"):
        return "variant"
    true_item_id = str(item.get("preferred_true_item_id") or item.get("true_item_id") or "")
    if CANONICAL_RE.fullmatch(true_item_id):
        return "canonical_true_exam"
    if FORMAL_ORIGINAL_RE.fullmatch(true_item_id):
        return "formal_source_original"
    if VARIANT_RE.fullmatch(true_item_id):
        return "variant"
    return "identity_unclassified"


def order_items(items: Iterable[dict[str, Any]], concept_ids: Iterable[str], *,
                unclassified_class: str = "identity_unclassified",
                contacts: dict[str, Any] | None = None) -> list[dict[str, Any]]:
    """Put originals first while preserving the native concept priority per tier."""
    concept_order = {concept_id: index for index, concept_id in enumerate(concept_ids)}
    tier_order = {name: index for index, name in enumerate(SOURCE_ORDER)}

    def key(item: dict[str, Any]) -> tuple[Any, ...]:
        targets = item.get("review_target_concept_ids") or []
        target_rank = min((concept_order.get(str(point), len(concept_order)) for point in targets),
                          default=len(concept_order))
        stable_identity = str(
            item.get("request_id")
            or item.get("question_id")
            or item.get("preferred_true_item_id")
            or item.get("true_item_id")
            or ""
        )
        item_class = source_class(item)
        if item_class == "identity_unclassified":
            item_class = unclassified_class
        if contacts is not None:
            identity = str(item.get('preferred_true_item_id') or item.get('true_item_id') or '')
            return tier_order[item_class], *contact_order_key(identity, contacts), target_rank, stable_identity
        return tier_order[item_class], target_rank, stable_identity

    return sorted(items, key=key)


def ordering_summary(items: Iterable[dict[str, Any]], *,
                     unclassified_class: str = "identity_unclassified") -> dict[str, Any]:
    rows = list(items)
    counts = {name: 0 for name in SOURCE_ORDER}
    for item in rows:
        item_class = source_class(item)
        if item_class == "identity_unclassified":
            item_class = unclassified_class
        counts[item_class] += 1
    return {
        "policy": "canonical_true_exam_then_formal_source_original_then_unclassified_then_variant",
        "identity_basis": "bound_true_item_id_not_review_flags",
        "counts": counts,
        "item_count": len(rows),
    }
