"""A user-authorized question limit over frozen native D candidates.

The full native scope remains a scheduling reference. The web selects only the
requested number of questions; importing that return creates a local subset
decision without completing or deleting anything outside the selection.
"""
from __future__ import annotations

import copy
import datetime as dt
import hashlib
import json
from pathlib import Path
import re
import unicodedata
import zipfile

import recent_contact_408 as recent_contact
import exam_frequency_priority_408 as exam_priority
import exam_year_plan_408 as exam_year_plan

SCHEMA = "cs408-morning-selection-input-v1"
CONTRACT_SCHEMA = "cs408-morning-selection-contract-v1"
EXCLUSIONS_SCHEMA = "cs408-morning-recent-contact-exclusions-v1"
POLICY_REL = Path(".private/study-bridge/morning-selection-policy.json")
REGISTRY_REL = Path(".private/study-bridge/morning-selection-inputs")
PROJECT_INSTRUCTIONS_REL = Path("planning/2026-09-13-d-deepseek-optimization/D-408晨间备题-网页提示词-v7.3.txt")
# Legacy inputs may register 7 days. Current production policy is explicitly 14
# days; current_cooldown_days prevents a returned old input from weakening it.
DEFAULT_COOLDOWN_DAYS = 7
MAX_COOLDOWN_DAYS = 60
KEYWORD_SELECTION_POLICY = "recent_errors_keyword_diversity_v2"
EXAM_ID_RE = re.compile(r"EXAM408-\d{4}-Q\d{2}(?:-P\d{2})?")


def encoded(value):
    return (json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2) + "\n").encode()


def sha(raw):
    return hashlib.sha256(raw).hexdigest()


def require(ok, message):
    if not ok:
        raise ValueError(message)


def validate_active_cooldown(eligibility, day, window):
    """Fail before delivery if the active gate and its date proofs disagree."""
    for field in ("exposure_window_days", "window_days", "recent_contact_exclusion_days"):
        require(eligibility.get(field) == window, "morning_cooldown_declaration_conflict:" + field)
    label = str(eligibility.get("selection_policy", ""))
    if label.startswith("true_exams_only_exact_item_cooldown_"):
        require(label == f"true_exams_only_exact_item_cooldown_{window}_days",
                "morning_cooldown_policy_label_conflict")
    count = 0
    for rows in eligibility.get("candidates_by_formal_id", {}).values():
        for row in rows:
            require(row.get("recent_contact_exclusion_days") == window,
                    "morning_candidate_cooldown_declaration_conflict")
            fact = row.get("last_learning_contact")
            gap = recent_contact.gap_days(fact, day)
            expected = ((dt.date.fromisoformat(fact["study_date"]) + dt.timedelta(days=window)).isoformat()
                        if gap is not None and gap < window else None)
            require(row.get("eligible_on") == expected, "morning_candidate_eligible_date_conflict")
            require(not (gap is not None and gap < window and row.get("eligible")),
                    "morning_candidate_inside_active_cooldown")
            count += 1

    def proofs(value):
        if isinstance(value, dict):
            if value.get("study_date") and value.get("eligible_on"):
                expected = (dt.date.fromisoformat(value["study_date"]) + dt.timedelta(days=window)).isoformat()
                require(value["eligible_on"] == expected, "morning_contact_proof_eligible_date_conflict")
            for child in value.values():
                proofs(child)
        elif isinstance(value, list):
            for child in value:
                proofs(child)
    proofs(eligibility)
    return {"schema": "cs408-morning-cooldown-consistency-v1", "status": "VERIFIED",
            "target_date": day.isoformat(), "active_window_days": window,
            "checked_candidate_rows": count, "formal_write_count": 0, "learner_evidence_write_count": 0}


def read(path):
    return json.loads(Path(path).read_bytes())


def policy_for(repo):
    path = Path(repo) / POLICY_REL
    if not path.is_file():
        return None
    policy = read(path)
    require(policy.get("authorization_kind") == "explicit_user_question_limit"
            and policy.get("user_request") and policy.get("user_message_id"),
            "morning_selection_user_authorization_required")
    limit = policy.get("question_limit")
    require(isinstance(limit, int) and not isinstance(limit, bool) and 1 <= limit <= 100,
            "morning_selection_limit_invalid")
    cooldown_days(policy)
    subquestion_minimum(policy)
    return policy


def subquestion_minimum(policy):
    """User-required count of independent big-question parts (2026-10-05)."""
    value = (policy or {}).get("min_independent_subquestions", 0)
    require(isinstance(value, int) and not isinstance(value, bool)
            and 0 <= value <= (policy or {}).get("question_limit", 0),
            "morning_selection_subquestion_minimum_invalid")
    return value


def is_subquestion(row):
    return bool(re.fullmatch(r"EXAM408-\d{4}-Q\d{2}-P\d{2}", str(row.get("true_item_id") or "")))


def cooldown_days(policy):
    """Exact-question real-contact exclusion window for the limited selection."""
    value = (policy or {}).get("exact_item_cooldown_days", DEFAULT_COOLDOWN_DAYS)
    require(isinstance(value, int) and not isinstance(value, bool)
            and DEFAULT_COOLDOWN_DAYS <= value <= MAX_COOLDOWN_DAYS,
            "morning_selection_cooldown_invalid")
    return value


def latest_contact(*facts):
    return recent_contact.latest_contact(*facts)


def current_cooldown_days(repo, contract=None):
    """A returned old package cannot weaken the user's current preparation rule."""
    registered = (contract or {}).get("recent_contact_exclusion_days", DEFAULT_COOLDOWN_DAYS)
    return max(cooldown_days({"exact_item_cooldown_days": registered}), cooldown_days(policy_for(repo)))


def require_current_contacts(repo, body, contract=None, frozen_contacts=None, *, hot_capture_proof=None):
    window = current_cooldown_days(repo, contract)
    day = dt.date.fromisoformat(body["target_date"])
    options = {"hot_capture_proof": hot_capture_proof} if hot_capture_proof is not None else {}
    live = recent_contact.real_contacts(repo, day, strict=True, **options)["by_identity"]
    stale = set()
    for name in ("selected_items", "supplemental_items", "substitute_items"):
        for item in body.get(name, []):
            preferred, actual = item.get("preferred_true_item_id"), item.get("true_item_id")
            require(not (preferred and actual and preferred != actual), "D_selection_true_item_identity_conflict")
            identity = preferred or actual
            fact = latest_contact(live.get(identity), (frozen_contacts or {}).get(identity))
            gap = recent_contact.gap_days(fact, day)
            if gap is not None and gap < window:
                stale.add(identity)
    require(not stale, f'D_selection_recent_real_contact_within_{window}_days:' + ','.join(sorted(stale)))


def recent_evidence(profile, target_date, lookback_days=14):
    day = dt.date.fromisoformat(target_date)
    rows = []
    for row in profile.get("timeline", []):
        try:
            observed = dt.date.fromisoformat(str(row.get("study_date") or row.get("observed_date") or row.get("date")))
        except (ValueError, TypeError):
            continue
        if not 0 <= (day - observed).days <= lookback_days:
            continue
        # A teaching turn is not a learner error or evidence of hint dependence.
        if row.get("evidence_role") == "assistant_explanation" or row.get("event_kind") == "explanation":
            continue
        score = 0
        if (row.get("outcome") in {"wrong", "partial", "uncertain", "relearn_required", "incorrect"}
                or row.get("reasoning_result") in {"incorrect", "partial", "unsound"}
                or row.get("choice_result") == "incorrect"):
            score = 3
        elif row.get("prompt_dependency") in {"hint_or_prompted", "minimal", "progressive", "full"}:
            score = 2
        rows.append({"study_date": observed.isoformat(), "summary": row.get("summary"),
                     "outcome": row.get("outcome"), "prompt_dependency": row.get("prompt_dependency"),
                     "source_refs": row.get("source_refs", row.get("evidence_refs", [])),
                     "record_key": row.get("record_key"), "priority_evidence_score": score})
    rows.sort(key=lambda row: row["study_date"], reverse=True)
    recent_subpoints = []
    for subpoint in profile.get("subpoints", []):
        evidence = recent_evidence({"timeline": subpoint.get("evidence", [])}, target_date, lookback_days)
        if evidence["priority_evidence_score"] > 0:
            recent_subpoints.append({"subpoint_id": subpoint.get("subpoint_id"),
                                    "title": subpoint.get("title"), "keywords": subpoint.get("keywords", []),
                                    "recent_records": evidence["recent_difficulty_records"]})
    difficulties = [row for row in rows if row["priority_evidence_score"] > 0]
    return {"lookback_days": lookback_days, "priority_evidence_score": max((r["priority_evidence_score"] for r in rows), default=0),
            "recent_records": rows[:8], "recent_record_count": len(rows),
            "recent_difficulty_records": difficulties[:8], "recent_difficulty_count": len(difficulties),
            "recent_subpoints": recent_subpoints,
            "short_summary": profile.get("short_summary", ""), "next_check": profile.get("next_check", ""),
            "scope": "Only dated learner difficulty establishes recent-error relevance; this is not a mastery grade."}


def normalized_keywords(values):
    return sorted({re.sub(r"\s+", " ", unicodedata.normalize("NFKC", str(value))).strip().casefold()
                   for value in values if isinstance(value, str) and value.strip()})


def candidate_keywords(surface, material, point):
    """Use verified question semantics; a parent point is an explicit fallback."""
    values = surface.get("keywords") or material.get("keywords") or []
    if not values:
        values = [part.get("focus", "") for part in (material.get("exam_point_context") or {}).get("parts", [])
                  if point in part.get("canonical_points", [])]
    return normalized_keywords(values) or [point]


def difficulty_match(recent, keywords):
    if not recent.get("recent_difficulty_count"):
        return None
    matches = []
    for point in recent.get("recent_subpoints", []):
        terms = normalized_keywords(point.get("keywords", []))
        if any(a in b or b in a for a in keywords for b in terms):
            matches.append({"subpoint_id": point.get("subpoint_id"), "title": point.get("title"),
                            "keywords": terms, "recent_records": point["recent_records"]})
    # Existing choices may have only parent-level reviewed semantics. Preserve
    # that weaker, truthful match rather than inventing a fine-grained error.
    return {"level": "verified_subpoint_keyword" if matches else "canonical_point_recent_error",
            "matched_subpoints": matches, "recent_records": recent["recent_difficulty_records"]}


def keyword_signature(row):
    return tuple(normalized_keywords(row.get("keywords", [])) or [row["concept_id"]])


def task_identities(row):
    return {row['true_item_id'], *(row.get('contact_dependency_ids') or [])}


def targets_conflict(left, right):
    """Do not test a result supplied by another unit; common given inputs are fine."""
    return (left['true_item_id'] in task_identities(right)
            or right['true_item_id'] in task_identities(left)
            or left['true_item_id'] == right.get('parent_true_item_id')
            or right['true_item_id'] == left.get('parent_true_item_id'))


def dated_equivalent_exists(row, rows):
    """A dateless pick is rejected when the same point and keywords have a dated alternative."""
    if (row.get("last_learning_contact") or {}).get("study_date"):
        return False
    return any(other is not row and other["concept_id"] == row["concept_id"]
               and other.get("recent_error_match")
               and keyword_signature(other) == keyword_signature(row)
               and (other.get("last_learning_contact") or {}).get("study_date")
               for other in rows)


def annual_stage(row):
    return row.get('exam_year_selection_stage', 0)


def annual_frequency_first(row):
    return row.get('exam_year_point_overlap_policy') == exam_year_plan.FREQUENCY_FIRST_FALLBACK_POLICY


def annual_priority_key(row):
    rank, stage = exam_priority.rank(row), annual_stage(row)
    return (rank, stage) if annual_frequency_first(row) else (stage, rank)


def compatible_with(row, chosen):
    return not any(row['concept_id'] == prior['concept_id']
                   or row['true_item_id'] == prior['true_item_id']
                   or targets_conflict(row, prior) for prior in chosen)


def compatible_capacity_plan(rows, limit):
    """A maximum feasible witness for a bounded pool, including given answers.

    The common case reaches the bipartite upper bound greedily. The remaining
    search is only needed when parent/given-answer conflicts reduce capacity;
    it never relaxes a point, exact-item or prerequisite-result constraint.
    """
    rows = [row for row in rows if row.get('recent_error_match')]
    upper = selection_capacity(rows, limit)
    if not upper:
        return []
    best = []
    for row in sorted(rows, key=candidate_order_key):
        if compatible_with(row, best):
            best.append(row)
        if len(best) == upper:
            return best
    groups = {}
    for row in rows:
        groups.setdefault(row['concept_id'], []).append(row)

    def search(pending, chosen):
        nonlocal best
        if len(chosen) > len(best):
            best = list(chosen)
        if len(best) == upper or not pending:
            return
        available = {point: [row for row in options if compatible_with(row, chosen)]
                     for point, options in pending.items()}
        available = {point: options for point, options in available.items() if options}
        if len(chosen) + len(available) <= len(best):
            return
        remaining_rows = [row for options in available.values() for row in options]
        if len(chosen) + selection_capacity(remaining_rows, upper - len(chosen)) <= len(best):
            return
        point = min(available, key=lambda p: (len(available[p]),
                    min(candidate_order_key(row) for row in available[p])))
        remaining = {p: options for p, options in available.items() if p != point}
        seen = set()
        for row in sorted(available[point], key=candidate_order_key):
            signature = (row['true_item_id'], tuple(sorted(row.get('contact_dependency_ids') or [])))
            if signature in seen:
                continue
            seen.add(signature)
            search(remaining, chosen + [row])
            if len(best) == upper:
                return
        search(remaining, chosen)

    search(groups, [])
    return best


def require_annual_stage_priorities(chosen, pool, limit):
    """Enforce the date's explicit frequency/stage order on verified candidates."""
    if not any('exam_year_selection_stage' in row for row in pool):
        return
    if any(annual_frequency_first(row) for row in pool):
        require(all(annual_frequency_first(row) and row.get('frequency_priority') for row in pool),
                'D_selection_annual_frequency_mode_mixed_or_unbound')
        bins = []
        for key in sorted({annual_priority_key(row) for row in pool}):
            earlier = [row for row in chosen if annual_priority_key(row) < key]
            available = [row for row in pool if annual_priority_key(row) == key
                         and compatible_with(row, earlier)]
            capacity = len(compatible_capacity_plan(available, max(0, limit-len(earlier))))
            selected = sum(annual_priority_key(row) == key for row in chosen)
            later = any(annual_priority_key(row) > key for row in chosen)
            require(not later or selected == capacity,
                    f'D_selection_annual_frequency_bin_not_exhausted:{key}:{selected}<{capacity}')
            bins.append({'frequency_rank':key[0], 'overlap_stage':key[1],
                         'capacity_after_earlier_picks':capacity, 'selected':selected})
        return {'priority_mode':exam_year_plan.FREQUENCY_FIRST_FALLBACK_POLICY,
                'bins':bins, 'preferred_selected':sum(annual_stage(row)==0 for row in chosen),
                'fallback_selected':sum(annual_stage(row)==1 for row in chosen), 'question_limit':limit}
    preferred = [row for row in pool if annual_stage(row) == 0]
    preferred_capacity = len(compatible_capacity_plan(preferred, limit))
    preferred_count = sum(annual_stage(row) == 0 for row in chosen)
    fallback_count = len(chosen) - preferred_count
    if fallback_count:
        require(preferred_capacity < limit and preferred_count == preferred_capacity,
                f'D_selection_annual_preferred_pool_not_exhausted:{preferred_count}<{preferred_capacity}')
        require(fallback_count <= limit - preferred_capacity,
                'D_selection_annual_fallback_exceeds_shortfall')
    return {'preferred_capacity': preferred_capacity, 'preferred_selected': preferred_count,
            'fallback_selected': fallback_count, 'question_limit': limit}


def current_annual_pool(repo, registration, execution_date, exclusion, *, hot_capture_proof=None):
    day = dt.date.fromisoformat(execution_date)
    from native_morning_sources_408 import SOURCE_BASIS, contact_proof, details_for
    if hot_capture_proof is None and registration["contract"].get("source_basis") == SOURCE_BASIS:
        hot_capture_proof = contact_proof(repo, execution_date, details_for(repo))
    options = {"hot_capture_proof": hot_capture_proof} if hot_capture_proof is not None else {}
    live = recent_contact.real_contacts(repo, day, strict=True, **options)['by_identity']
    window = current_cooldown_days(repo, registration['contract'])
    pool = []
    for frozen in registration['allowed_candidates']:
        row = copy.deepcopy(frozen)
        row['last_learning_contact'] = latest_contact(row.get('last_learning_contact'), live.get(row['true_item_id']))
        gap = recent_contact.gap_days(row['last_learning_contact'], day)
        assessment = exam_year_plan.assess_item(repo, row, exclusion)
        if (gap is None or gap >= window) and assessment['allowed']:
            if exam_year_plan.permits_point_fallback(exclusion):
                row['exam_year_selection_stage'] = assessment['exam_year_selection_stage']
                row['planned_year_overlap_points'] = assessment['overlap_points']
                row['exam_year_point_overlap_policy'] = exclusion['point_overlap_policy']
            pool.append(row)
    return pool


def require_annual_start_priorities(repo, plan, execution_date, exclusion):
    """Recheck a conditional fallback against its registered pool before display."""
    version = plan.get('selection_input_version') or (plan.get('local_scope_decision') or {}).get('selection_input_version')
    require(isinstance(version, str) and re.fullmatch('[0-9a-f]{64}', version),
            'D_exam_year_start_fallback_registration_required')
    registration = read(Path(repo) / REGISTRY_REL / (version + '.json'))
    contract = registration['contract']
    require(contract.get('target_date') == execution_date
            and contract.get('exam_year_exclusion_sha256') == exclusion['exclusion_sha256']
            and contract.get('exam_year_point_overlap_policy') == exclusion['point_overlap_policy'],
            'D_exam_year_start_fallback_plan_changed')
    pool = current_annual_pool(repo, registration, execution_date, exclusion)
    chosen = []
    for item in plan.get('learning_items', plan.get('supplemental_items', [])):
        surface = item.get('selection', item)
        identity = surface.get('preferred_true_item_id') or surface.get('true_item_id')
        matches = [row for row in pool if row['true_item_id'] == identity
                   and row['concept_id'] == item.get('concept_id')
                   and row.get('surface_sha256') == surface.get('surface_sha256')
                   and item.get('question_id') in row['formal_ids']]
        require(len(matches) == 1, 'D_exam_year_start_fallback_candidate_unavailable:' + str(identity))
        chosen.append(matches[0])
    require(len(chosen) == contract['target_question_count'], 'D_exam_year_start_fallback_count_mismatch')
    audit = require_annual_stage_priorities(chosen, pool, contract['question_limit'])
    require_keyword_priorities(chosen, pool)
    if contract.get('frequency_priority_policy') == exam_priority.POLICY:
        require_frequency_priorities(chosen, pool)
    return audit


def keyword_selection_plan(rows, limit, min_subquestions=0):
    """An inspectable recommendation, not an exclusive web-selected set."""
    if any('exam_year_selection_stage' in row for row in rows):
        chosen = []
        frequency_first = any(annual_frequency_first(row) for row in rows)
        if frequency_first:
            require(all(annual_frequency_first(row) and row.get('frequency_priority') for row in rows),
                    'annual_frequency_mode_requires_bound_frequency_for_every_candidate')
            phases = sorted({annual_priority_key(row) for row in rows})
        else:
            phases = (0, 1)
        for phase in phases:
            if len(chosen) >= limit:
                break
            available = [row for row in rows
                         if (annual_priority_key(row) if frequency_first else annual_stage(row)) == phase
                         and compatible_with(row, chosen)]
            stripped = [{k: v for k, v in row.items()
                         if k not in {'exam_year_selection_stage','exam_year_point_overlap_policy'}}
                        for row in available]
            remaining_min = max(0, min_subquestions - sum(is_subquestion(row) for row in chosen))
            picks = keyword_selection_plan(stripped, limit - len(chosen), remaining_min)
            # The primary pool must be exhausted before the explicit exception.
            # Prefer the existing frequency/keyword recipe; exact capacity only
            # repairs a loss caused by a parent/given-answer conflict.
            if (frequency_first or phase == 0) and len(picks) < limit-len(chosen):
                witness = compatible_capacity_plan(stripped, limit-len(chosen))
                if len(witness) > len(picks):
                    picks = witness
            chosen.extend(next(row for row in available if row['concept_id'] == pick['concept_id']
                               and row['true_item_id'] == pick['true_item_id']) for pick in picks)
        return sorted(chosen, key=lambda row: (*annual_priority_key(row),
                                               keyword_signature(row), *candidate_order_key(row)))
    if any(row.get("frequency_priority") for row in rows):
        # Exhaust the eligible higher tier before considering the next tier.
        # Reserve independent parts inside each tier, so a dateless high-tier
        # part is not displaced by a dated lower-tier choice.
        chosen = []
        for tier in sorted({exam_priority.rank(row) for row in rows}):
            points = {row["concept_id"] for row in chosen}
            identities = {row["true_item_id"] for row in chosen}
            available = [row for row in rows if exam_priority.rank(row) == tier
                         and row["concept_id"] not in points and row["true_item_id"] not in identities
                         and not any(targets_conflict(row, prior) for prior in chosen)]
            if len(chosen) >= limit:
                break
            remaining_min = max(0, min_subquestions - sum(is_subquestion(row) for row in chosen))
            picks = keyword_selection_plan(
                [{key: value for key, value in row.items() if key != "frequency_priority"}
                 for row in available], limit - len(chosen), remaining_min)
            chosen.extend(next(row for row in available if row["concept_id"] == pick["concept_id"]
                               and row["true_item_id"] == pick["true_item_id"]) for pick in picks)
        return sorted(chosen, key=lambda row: (exam_priority.rank(row), keyword_signature(row),
                                               *candidate_order_key(row)))
    pending = [row for row in rows if row.get("recent_error_match")]
    everything = list(pending)
    target = selection_capacity(pending, limit)
    chosen, points, identities, covered = [], set(), set(), set()

    def key(row):
        date = (row.get("last_learning_contact") or {}).get("study_date")
        overlap = set(keyword_signature(row)) & covered
        return (date is None, bool(overlap), len(overlap), *candidate_order_key(row))

    def feasible(row):
        remaining = [item for item in pending if item["concept_id"] != row["concept_id"]
                     and item["true_item_id"] != row["true_item_id"]]
        return selection_capacity(remaining, target) >= target - len(chosen) - 1

    def take(row):
        nonlocal pending
        chosen.append(row)
        points.add(row["concept_id"])
        identities.add(row["true_item_id"])
        covered.update(keyword_signature(row))
        pending = [item for item in pending if item["concept_id"] not in points
                   and item["true_item_id"] not in identities
                   and not any(targets_conflict(item,row) for row in chosen)]

    def match_recency(row):
        match = row.get("recent_error_match") or {}
        records = [record for point in match.get("matched_subpoints") or [] for record in point.get("recent_records") or []]
        dates = [str(record.get("study_date")) for record in records or match.get("recent_records") or []
                 if record.get("study_date")]
        return -dt.date.fromisoformat(max(dates)).toordinal() if dates else 0

    def reserve_key(row):
        date = (row.get("last_learning_contact") or {}).get("study_date")
        overlap = set(keyword_signature(row)) & covered
        same_parent = row.get("parent_true_item_id") in {item.get("parent_true_item_id") for item in chosen}
        fine = bool((row.get("recent_error_match") or {}).get("matched_subpoints"))
        subject_load = sum(1 for item in chosen if item["concept_id"][:2] == row["concept_id"][:2])
        return (date is None, same_parent, not fine, subject_load, match_recency(row),
                bool(overlap), len(overlap), *candidate_order_key(row))

    # 2026-10-05: reserve the user-required independent big-question parts
    # first, spread over parents and subjects and led by the finest recent
    # breakpoint match; dateless parts are not crowded out by the cross-type
    # known-date rule. The rest of the plan is unchanged.
    while len(chosen) < min(min_subquestions, target):
        options = [row for row in sorted(pending, key=reserve_key)
                   if is_subquestion(row) and not dated_equivalent_exists(row, everything) and feasible(row)]
        if not options:
            break
        take(options[0])
    while pending and len(chosen) < target:
        for row in sorted(pending, key=key):
            if feasible(row):
                break
        take(row)
    return sorted(chosen, key=lambda row: (keyword_signature(row), *candidate_order_key(row)))


def require_keyword_priorities(chosen, pool):
    """Permit equivalent semantic choices, reject provably dominated repeats."""
    identities = {row["true_item_id"] for row in chosen}
    points = {row["concept_id"] for row in chosen}
    for index,row in enumerate(chosen):
        require(not any(targets_conflict(row,prior) for prior in chosen[:index]),
                'D_selection_target_answer_already_supplied')
    for selected in chosen:
        require(bool(selected.get("recent_error_match")), "D_selection_recent_error_relation_required")
        selected_keywords = set(keyword_signature(selected))
        other_keywords = {keyword for row in chosen if row is not selected for keyword in keyword_signature(row)}
        for candidate in pool:
            if annual_stage(candidate) != annual_stage(selected):
                continue
            if candidate["true_item_id"] in identities or not candidate.get("recent_error_match"):
                continue
            if any(targets_conflict(candidate,row) for row in chosen if row is not selected):
                continue
            if candidate["concept_id"] in points and candidate["concept_id"] != selected["concept_id"]:
                continue
            candidate_keywords_set = set(keyword_signature(candidate))
            # Stronger diversity only when the alternative shares the target
            # point, so this cannot overrule the user's actual recent error.
            if (candidate["concept_id"] == selected["concept_id"]
                    and selected_keywords <= other_keywords and not candidate_keywords_set & other_keywords
                    and (candidate.get("last_learning_contact") or {}).get("study_date")):
                raise ValueError("D_selection_avoidable_keyword_repeat:" + selected["true_item_id"])
            if candidate["concept_id"] != selected["concept_id"] or keyword_signature(candidate) != keyword_signature(selected):
                continue
            old = (candidate.get("last_learning_contact") or {}).get("study_date")
            new = (selected.get("last_learning_contact") or {}).get("study_date")
            require(not old or (new is not None and new <= old),
                    "D_selection_older_equivalent_candidate_required:" + candidate["true_item_id"])


def require_frequency_priorities(chosen, pool):
    """Reject a lower tier when an eligible higher-tier replacement remains."""
    for selected in chosen:
        remaining = [row for row in chosen if row is not selected]
        for candidate in pool:
            if not annual_frequency_first(selected) and annual_stage(candidate) != annual_stage(selected):
                continue
            if not candidate.get("recent_error_match") or exam_priority.rank(candidate) >= exam_priority.rank(selected):
                continue
            if any(candidate["concept_id"] == row["concept_id"]
                   or candidate["true_item_id"] == row["true_item_id"]
                   or targets_conflict(candidate, row) for row in remaining):
                continue
            raise ValueError("D_selection_higher_frequency_candidate_required:" + candidate["true_item_id"])


def registered_keyword_metadata(repo, body):
    """Native import cannot promote arbitrary web tags to registered semantics."""
    version = str(body.get('selection_input_version', ''))
    require(re.fullmatch('[0-9a-f]{64}', version), 'D_keyword_selection_registration_required')
    registration = read(Path(repo) / REGISTRY_REL / (version + '.json'))
    contract = registration['contract']
    require(contract.get('selection_policy') == KEYWORD_SELECTION_POLICY,
            'D_keyword_selection_policy_mismatch')
    for field in ('source_commit', 'source_basis', 'target_date', 'due_version', 'concept_priority_version'):
        require(body.get(field) == contract.get(field), 'D_selection_binding_drift:' + field)
    result = {}
    for name in ('selected_items', 'supplemental_items', 'substitute_items'):
        for item in body.get(name, []):
            identity = item.get('preferred_true_item_id') or item.get('true_item_id')
            candidates = [row for row in registration['allowed_candidates']
                          if row['true_item_id'] == identity and row['concept_id'] in item.get('review_target_concept_ids', [])
                          and item['question_id'] in row['formal_ids']
                          and row.get('surface_sha256') == item.get('surface_sha256')]
            require(bool(candidates), 'D_selection_candidate_not_in_registered_input')
            row = candidates[0]
            result[identity] = {'keywords': list(keyword_signature(row)),
                                'recent_error_match': row['recent_error_match'],
                                'parent_true_item_id': row.get('parent_true_item_id')}
            if contract.get('frequency_priority_policy') == exam_priority.POLICY:
                result[identity]['frequency_priority'] = copy.deepcopy(row['frequency_priority'])
    return result


def candidate_order_key(row):
    date = (row.get("last_learning_contact") or {}).get("study_date")
    return (*annual_priority_key(row), date is None, date or "9999-12-31", -row.get("recent_priority_score", 0),
            row["true_item_id"], row["concept_id"])


def selection_capacity(rows, limit):
    """Maximum distinct-question/distinct-point count, without selecting for study."""
    adjacency = {}
    for row in rows:
        adjacency.setdefault(row["concept_id"], set()).add(row["true_item_id"])
    matched = {}

    def assign(point, seen):
        for identity in sorted(adjacency[point]):
            if identity in seen:
                continue
            seen.add(identity)
            if identity not in matched or assign(matched[identity], seen):
                matched[identity] = point
                return True
        return False

    for point in sorted(adjacency):
        assign(point, set())
    return min(limit, len(matched))


INDEPENDENT_SUBQUESTION_RULES = '''选择题与本地已预处理的综合大题独立小问共同进入候选比较，不只筛选择题，也不现场重新拆大题。每个EXAM408-YYYY-QNN-Pxx按一道题计数，保留实际true_item_id、surface_sha256、parent_true_item_id；Pxx是本地稳定细分序号，原卷小问编号需按complete_payload.source_part_label核对。先比较同点全部合格候选，再为所选题完整备题。

独立小问必须读取canonical_exam_materials中的complete_question、complete_payload及专属question_images，保持必要共同条件、单位、图表、初态与当前问法。dependency_kind=supplied_prerequisite_results时，直接把已核验的provided_prerequisite_results作为已知输入，prerequisite_part_nos不是待用户完成的任务；例如只问复杂度就直接给完整算法。当前答案只进private，不退回父题整题或要求先做前问。缺件使本问无法独立作答时保留缺项，不依答案补造题设。

全部-Pxx只走substitute_items或supplemental_items自由作答入口，不放selected_items或ABCD专用campaign。question_id仍用候选行formal_ids的登记锚点，真实-Pxx身份不能被父题或另一个正式定位ID替换。整题近期接触约束其所有小问；单问不伪造兄弟小问作答，给定前问结果只记展示、不记掌握。检查contact_dependency_ids，不能同时测试另一题已经给出答案的前问，也不能同包选父题整题和子问；共同已知输入本身可重复使用。
'''


SUBQUESTION_QUOTA = '''本轮硬性要求（用户2026-10-05明确要求“一定要选大题”）：最终{target_count}题中至少{minimum}道必须是综合应用题的独立小问（true_item_id形如EXAM408-YYYY-QNN-Pxx），其余可以是选择题，也可以是更多小问；回包里的小问少于{minimum}道，本地导入会拒绝整包。候选池里的小问大多查不到接触日期，“已知日期优先于未知日期”只在同一类候选之间比较，不能据此把小问挤出这{minimum}个名额。selection-recommendation.json已按同样的近期错误相关、关键词去重和日期规则预留了{minimum}道小问，可以在同等或更好的覆盖下替换。这一条优先于contracts/D_PROJECT_INSTRUCTIONS.txt里“不为题型比例强行留名额”的旧说法；名额内的小问之间仍按近期错误相关、关键词去重和接触日期比较。

带小问的大题要特别注意：
1. 每道小问只考当前这一问。题面要给齐这一问需要的共同条件、单位、图表、初态和当前问法：读取canonical_exam_materials里的complete_question、complete_payload和专属question_images，不能只截父题的一部分，也不能把其他小问的问法混进来。
2. dependency_kind=supplied_prerequisite_results时，把已核验的前问结果（provided_prerequisite_results）直接写成已知条件，不要求学习者先做前问；前问结果只是已知输入，不是本问答案，也不算学习者作答。
3. 同一包里不得同时选同一大题的整题和它的小问，也不得选另一道其答案已作为本问已知输入给出的小问（看contact_dependency_ids）；共同已知条件可以重复使用。尽量把小问分散到不同大题和不同科目。
4. 小问全部放进substitute_items或supplemental_items，按自由作答（计算、推导、写代码）准备，不能放进只收ABCD的selected_items；当前小问的答案只写进private。
5. 每道小问的rationale写清它对应哪个近期断点，以及题面里给出了哪些前问结果。
'''


PARALLEL_WEB_PREPARATION = '''本次用户已授权网页端使用可用的原生子代理并发工作；这取代旧说明中“不得启动子代理”的限制，不改变数据版本或选题门禁。沿当前网页会话的模型与原生能力，不另行指定或切换子代理模型、数量、思考档位；没有原生子代理时如实说明，不能伪造并发记录。
主代理先固定同一source_commit、snapshot_id、input_version、target_date及合同，把互不重叠的候选规范点交给子代理并发核对近期断点、资格、频次和题源。子代理交回有证据的候选建议，不能各自独立选满30题。主代理汇总后，在全局统一确定合同要求的题数，核对14天真实接触、高频优先、每主规范点至多一题、小问下限，以及父子题和给定前问答案的冲突。只有最终入选的题，才按互不重叠的题目身份并发完成原图/条件核验、私有解析和个性化备课。
各子代理只写分配给自己的题目目录或独立工作目录，不改共享manifest、selection-contract、due元数据、最终题单或其他代理文件。主代理负责合并与最后一次全局校验，交一个真实完整的408_morning_review.zip；独立任务可并发，依赖前一阶段结果的步骤等待它完成。限流时缩小并发，只续接失败项，保留成功文件。
工作过程可以并发，讲解内容仍按逻辑线性展开：每道题的teaching先用文字独立讲清题设线索、条件、步骤和因果，再配对应的可点击辅助图方案；不把主线藏在点击里。当前提示级、精确-Pxx范围与答案保护在生成前过滤，教师全解不因after_attempt自动公开。研究提案、生成或点击不产生新的学习表现。'''


PROMPT = '''请使用本网页会话中用户选定的模型，从附件的合格真题候选池中，选择并准备 {target_count} 道用于 {day} 的408晨间复盘题。本轮上限 {limit} 道，只对最终选中的题做完整备题，不为全部知识点逐个生成题。

API辅助：精确题ID沿原读取；名称未命中才用query的semantic_candidates发现候选，不代替14天、年度和本问资格检查。辨识困难的已发布原件可用material_prepare，contains按library公布的JSON传pages/mode/purpose/view/review；review默认auto仅疑义页，review_pages只选本次pages。无原页图或失败保留not_screened；未标记不是审核通过。protected/after_attempt只回元数据，direct须当前答案范围授权及allow_answer_text=true。未发布附件用网页原生读图，不把沙盒路径传MCP；提取稿、缓存和复核提示不代替实际原图与本人证据。

先读 contracts/D_PROJECT_INSTRUCTIONS.txt、selection-contract.json、candidate-pool.json、recent-contact-exclusions.json、recent-learning.json 和 RETURN_CONTRACT.md。固定 source_commit={commit}，输入版本={version}。scope/ 和 return-template/due_concepts.json 的完整原生集合用于来源与调度对账，不是本轮全量备题任务，不能改掉其中原始身份。

''' + PARALLEL_WEB_PREPARATION + '''

按本包项目指令实际调用Study Library的library，再query拟采用题的question_refs和知识点personal_context；保持固定版本，工具缺失与Session terminated分别如实记录。题面、原图及完整个人资料优先读包内同版文件。

对最终入选题，在同一source_commit查询精确true_item_id的personal_context/current_question_id，并按该问已核条件命中的KSP读取分项摘要；完整对象从结构化source_ref补齐，不把概要、截断片段或旧history当作完整概况。记录本次读到的source_version、template_native_version、source_visual_plan_version；个人事实只来自已核摘要和真实原始证据，项目上传的考纲/真题只作客观来源。teaching采用当前精确question_visual_template、lesson_bridge和exam_visual_binding，保留原图中有意义的布局、对象、拓扑、字段、标签及初态；文字连续讲清本问逻辑，可点击图用于局部核对。没有原图的题明确标为机制示意。逐题的KSP选择、图解来源、步骤和知识范围写入现有teaching_path文件，沿contracts中的knowledge-profile、question-visual、source-visual合同理解字段，不向manifest或题目行增造字段。完整教师全解仅direct/L5材料，提示和-Pxx必须单独编译安全局部，兄弟答案及未公开终值不得藏入前端。旧备题包或已经冻结的队列不随这次说明重写。

{subquestion_rules}

选题优先级依次为：
1. 硬排除：仅已核验408统考真题；同一精确题面距目标日期不足{cooldown}天的任何真实接触一律排除。真实接触包括：晨间展示与作答（含独立正确、只记复习不记错题的 correct_review）、原题复做、日常做题与快速入库、概念补充中展示过的原题；今天已展示但未作答的也算。正式节点的“最近复做日期”按合同不随独立正确复习更新，不能用它判断远近，只看候选的 last_learning_contact。本地已按这一口径把近{cooldown}天接触过的题移出 candidate-pool.json，名单见 recent-contact-exclusions.json：这些题不是候选，不得通过替代题、别名、换截图或 MCP 查询换回来。同一真题只选一次；不自编、改题或以变式凑数。
2. 最高排序优先级：last_learning_contact.study_date 越早越优先；跨整个候选池比较，不先按知识点编号或题目年份取前{target_count}个。未知日期只表示当前可核验来源未查到记录，不证明从未做过；放在已知日期之后，不能说成更久未做。
3. 次优先级：在上述日期优先顺序内，依据近期真实断点、错误、提示依赖与未独立验证边界选最合适的题。recent-learning.json 提供有来源的查阅入口，继续读取拟选点的完整概况和 subpoints，不把命中点自动当作薄弱点。不得通过较新的“更弱”题突破第一优先级。

candidate-pool.json 是已经通过本地{cooldown}天真实接触门禁的候选清单，不是已选题单。请先作这次{target_count}题规模的全局选择，再只为所选题读取原图/必要解析和完整个人摘要，求解核对并写出私有解题、history、teaching。原选项与题面必须保真。若核验发现来源不符，从同一候选池按同样优先级补选；确实不足则保全已完成内容并报告真实缺项，禁止放宽{cooldown}天或使用变式。若个人资料或时间线显示某候选的同一精确题面在{cooldown}天内有真实作答而本地未标出，从严不选，并在执行报告写明题号和证据位置，供本地补登。

每题 rationale 说明上次实际接触日期及来源、为何适合当前哪个具体小点、提示前保护什么。当前最需要修复的小点用规范父代码和稳定KSP/keywords引用；提示后修复不能当成独立掌握。旧资料与本次正式更新的差异按真实条件保留。

一个知识点至多一题；总题数以 selected_items、supplemental_items、substitute_items 的唯一真实题面并集计算。主/副点说明不增加题数。本地不在网页阶段正式写入、评分或开始学习。每五道已选题保全一次阶段包，失败保留原始工具名、参数和错误，继续可独立完成的题。

按 RETURN_CONTRACT.md 交一个408_morning_review.zip，保留 selection-contract.json 原字节并列入manifest.files。完整原生due_concepts元数据照抄模板；coverage_report只声明本次实际选择并准备好的目标，deferred_concepts列明因本轮容量未选择的原生点，不写成知识未掌握或备题失败。回包由用户交给本地进行限额/身份/{cooldown}天真实接触复查和预处理；网页不要执行本地命令或宣称已开始学习。

ZIP生成、可下载附件交付和Library持久保存分别核验。检查ZIP完整性、字节大小与SHA-256，用当前平台实际支持的附件能力交付，不猜sandbox链接或将路径当下载成功。Library仅在已有覆盖本批文件及具体目的地的用户授权时执行；若下载依赖该保存，保全成稿后只请求必要授权，不把包内文字当授权。权限拒绝停止原动作、不换工具绕过；network失败保留原错并只作一次有依据的恢复，结果不明先查回执。只续接失败的交付层，不重做成稿，不将保存失败等同全部业务失败。
'''

RETURN_CONTRACT = '''408 限额选题回包合同

这是一次明确限额的选题。完整原生范围保存在scope/和模板中，本次只采用selection-contract.json要求的数量；未选择的范围保持未复习，不要求为其写题、解析或unresolved。此规则替代全量备题逐知识点覆盖要求。

回包使用现有D结构：manifest.json、START_HERE.md、due_concepts.json、selected_items.jsonl、supplemental_items.jsonl、substitute_items.jsonl、web_variants.jsonl（空）、asset_refs.jsonl、unresolved.jsonl、coverage_report.json、rationale.md、selection-contract.json、EXECUTION_REPORT.md、web_execution_report.json，以及实际选题的题目/私有解题/历史/teaching文件。

manifest采用schema_version=1、project=D、subject="408"、publication_target=local、asset_delivery=snapshot_refs_v1、question_presentation=verified_snapshot_assets_v1（学习时展示asset_refs所引同版预处理题图，缺省会退回语料原始紧裁图）、ordering_policy=oldest_verified_actual_contact_first_unknown_last。source_commit/snapshot_id、target_date、due_version、concept_priority_version和input_version沿模板，run_id为本次稳定ASCII身份。排序字段必须保留，使原题与补充题合成一条按实际接触日期排序的队列。status=WEB_PREPARED只表示本次所选题语义备题完成；真实缺失所选题仍为PARTIAL。files覆盖除manifest自身以外所有实际文件，字段path、role、sha256、size。web_execution_report字段指向该文件，role也为web_execution_report。

due_concepts.json原样复制return-template中的完整集合：concept_ids、concept_requests、review_concept_ids、concept_priority_version均不裁剪。这个文件只为本地核对原生版本；本次备题目标以实际题目集合和selection-contract为准。本地从登记过的原始选题输入产生可信的subset decision，并保留所有未选择点，不接受网页自签本地批准。

每个选中项沿用原生D题目字段：question_id必须取candidate-pool中该知识点/真题组合的formal_ids，即本地登记候选锚点；review_target_concept_ids只有一个规范父点；真实preferred_true_item_id/true_item_id和surface_sha256来自同一候选行。实际原题另有正式定位ID时可保存在canonical_source_formal_id，不能用它替换登记锚点，也不能从锚点猜真题年份。source_refs必须包含登记锚点的同版安全卡来源，并保留实际原题的来源。problem_path、image_paths及asset_refs.question_id按登记锚点保持一致，公开图的package_path使用questions/<登记question_id>/assets/；source_path、真实题号和源图SHA仍指向实际采用的真题。

当前到期组里的精确选择题原题用selected_items。所有-Pxx独立子题必须走substitute_items或supplemental_items，保留独立子题身份与自由作答，不能送入只处理ABCD的原题campaign。同点其他合格原题用substitute_items，execution_kind=due_representative_substitute、substitution_reason说明为何改用同点原题，suppressed_same_concept_question_ids包含该点全部原到期ID，不提供伪造request_id。仅点级请求的题使用supplemental_items内真实request_id，不改其来源身份或把关联原题当作已经复做。

同一真实题面全场只选一次，同一主目标点至多一题。本地预处理会按selection-contract.json的recent_contact_exclusion_days，用当时最新的真实接触记录（展示、作答、独立正确复习、复做、日常做题、概念补充）重新核对全部采用题；窗口内的题会使整包被拒，不能带入学习。覆盖报告的covered_concept_ids是本次实际选中点；deferred_concepts列完整原生集合减去采用点，reason写“本轮限额未选择”，不混进unresolved_concept_ids。unresolved只用于本次需要的题仍真实未完成；不能写空数组掩盖选中的缺题。rationale逐题给实际接触日期证据和近期具体断点的匹配理由，最终展示顺序按接触日期早到晚。

已有原图按照contracts/asset-delivery-contract.md交asset_refs引用；以候选中的canonical_exam_materials为准，不能将同知识点另一正式卡的图当作实际真题。package_path是本地包内路径，published_path才是MCP路径。新产生的实际图表需要随包；私有解析与提示不进入未作答题面。

必须保留selection-contract.json原字节，不改限额、版本和日期。单项失败保全成功内容和真实执行报告；failures.input必须是对象，脚本工具的完整原始代码放在input.code中，不把input写成裸字符串。工具未加载、Session terminated、错误路径和成功空返回分别记录。全量native-return-contract.md只作为题级字段参考，其中全范围覆盖表述被本文件的明确限额替代。
'''


def preparation_evidence(path, target_date, *, annual_all=False):
    """Read-only raw-study overlay; never changes formal profiles or scheduling."""
    if path is None:
        return None, {}
    path = Path(path).resolve()
    doc = read(path)
    require(doc.get('schema') == 'cs408-preparation-evidence-v1'
            and doc.get('target_date') == target_date, 'preparation_evidence_scope_invalid')
    files = {}
    for row in doc.get('files', []):
        relative = Path(row['path'])
        require(not relative.is_absolute() and '..' not in relative.parts, 'preparation_evidence_path_invalid')
        source = path.parent / relative
        require(not source.is_symlink() and source.resolve().is_relative_to(path.parent),
                'preparation_evidence_path_outside_root')
        raw = source.read_bytes()
        require(sha(raw) == row['sha256'], 'preparation_evidence_source_drift')
        files['preparation-evidence/' + relative.as_posix()] = raw
    available = {row['path'] for row in doc.get('files', [])}
    for row in doc.get('focus_records', []) + doc.get('recent_contact_overrides', []):
        require(row.get('source_path') in available and isinstance(row.get('summary'), str)
                and row['summary'].strip(), 'preparation_focus_source_missing')
        observed = dt.date.fromisoformat(row['study_date'])
        age = (dt.date.fromisoformat(target_date) - observed).days
        require(age >= 0 and (annual_all or age <= 14), 'preparation_focus_outside_recent_window')
    focused_preparation_scope(doc)
    files['preparation-evidence/context.json'] = encoded(doc)
    return doc, files


def focused_preparation_scope(preparation):
    """An explicit per-export scope, backed by the already verified evidence files."""
    scope = (preparation or {}).get('selection_scope')
    if scope is None:
        return None
    require(isinstance(scope, dict)
            and scope.get('schema') == 'cs408-focused-selection-scope-v1'
            and scope.get('source') == 'explicit_user'
            and isinstance(scope.get('user_statement'), str) and scope['user_statement'].strip()
            and isinstance(scope.get('user_message_id'), str) and scope['user_message_id'].strip(),
            'preparation_selection_scope_authorization_missing')
    points = scope.get('allowed_concept_ids')
    years = scope.get('source_question_years')
    excluded = scope.get('excluded_exam_years', [])
    closeout = scope.get('daily_closeout')
    if closeout is not None:
        require(scope.get('mode') == 'annual_weak_representatives' and isinstance(closeout, dict)
                and 'study_date' in closeout and set(closeout) <= {'study_date', 'studied_exam_year'},
                'preparation_daily_closeout_scope_invalid')
        study_date = closeout['study_date']
        require(isinstance(study_date, str) and dt.date.fromisoformat(study_date).isoformat() == study_date
                and study_date <= preparation['target_date'], 'preparation_daily_closeout_date_invalid')
    require(isinstance(points, list) and points and len(points) == len(set(points))
            and all(isinstance(p, str) and re.fullmatch(r'(?:DS|CO|OS|CN)\d{2}-\d{2}', p) for p in points),
            'preparation_selection_scope_points_invalid')
    for values in (years, excluded):
        require(isinstance(values, list) and len(values) == len(set(values))
                and all(type(y) is int and 2009 <= y <= 2100 for y in values),
                'preparation_selection_scope_years_invalid')
    if closeout is None:
        require(bool(years), 'preparation_selection_scope_source_years_missing')
    else:
        studied = closeout.get('studied_exam_year')
        require(studied is None or (type(studied) is int and studied in years and studied < 2100),
                'preparation_daily_closeout_studied_exam_year_invalid')
    supported = set()
    for row in preparation.get('focus_records', []):
        identities = row.get('question_identity_hints', [])
        if closeout is not None:
            require(row['study_date'] == study_date, 'preparation_daily_closeout_focus_date_mismatch')
            require(isinstance(identities, list), 'preparation_daily_closeout_question_hints_invalid')
        if (row.get('concept_id') in points and (identities or closeout is not None)
                and all(isinstance(q, str) and EXAM_ID_RE.fullmatch(q) and int(q.split('-')[1]) in years
                        for q in identities)):
            supported.add(row['concept_id'])
    require(set(points) <= supported, 'preparation_selection_scope_point_without_year_evidence')
    return {**scope, 'allowed_concept_ids': sorted(points),
            'source_question_years': sorted(years), 'excluded_exam_years': sorted(excluded)}


def focused_candidate_allowed(scope, point, identity, dependencies=()):
    if scope is None:
        return True
    if point not in scope['allowed_concept_ids']:
        return False
    for question in [identity, *(dependencies or [])]:
        if (EXAM_ID_RE.fullmatch(question)
                and int(question.split('-')[1]) in scope['excluded_exam_years']):
            return False
    return True


def _local_authoring_inputs(directory, root, full, repo, policy, contract, pool, pool_bytes,
                             recommendation, exclusions, annual, evidence_files, recent, frequency):
    from prepare_d_review_408 import write

    points = {row["concept_id"] for row in pool}
    files = {"selection-contract.json": encoded(contract), "candidate-pool.json": pool_bytes,
             "selection-recommendation.json": encoded({"items": recommendation, "binding": "non_exclusive"}),
             "recent-contact-exclusions.json": exclusions,
             "recent-learning.json": encoded({p: recent[p] for p in sorted(points)}), **evidence_files}
    if annual["active"]:
        files["exam-year-exclusions.json"] = encoded(annual)
    if frequency:
        files["frequency-priority.json"] = encoded(frequency)
    for name, raw in files.items():
        path = directory / name
        path.parent.mkdir(parents=True, exist_ok=True)
        if path.exists():
            require(path.read_bytes() == raw, "morning_local_authoring_input_drift:" + name)
        else:
            write(path, raw.decode())
    manifest = {"schema": SCHEMA, "delivery": "local_authoring_directory", "project": "D", "subject": "408",
                "target_date": contract["target_date"], "source_commit": contract["source_commit"],
                "input_version": contract["input_version"], "source_content_root": str(root),
                "source_manifest_sha256": full["manifest_sha256"],
                "files": [{"path": k, "sha256": sha(v), "size": len(v)} for k, v in sorted(files.items())]}
    if contract.get("source_basis"):
        manifest["source_basis"] = contract["source_basis"]
    write(directory / "manifest.json", manifest)
    registration = {"contract": contract, "contract_sha256": sha(encoded(contract)), "policy": policy,
                    "allowed_candidates": pool, "local_input_manifest": str(directory / "manifest.json"),
                    "local_input_manifest_sha256": sha((directory / "manifest.json").read_bytes())}
    register = repo / REGISTRY_REL / (contract["input_version"] + ".json")
    if register.exists():
        require(read(register) == registration, "morning_local_registration_conflict")
    else:
        write(register, registration)
    return {"status": "LOCAL_AUTHORING_READY", "workspace": str(directory), "content_root": str(root),
            "source_commit": contract["source_commit"], "input_version": contract["input_version"],
            "target_question_count": contract["target_question_count"], "candidate_count": len(pool),
            "registration": str(register), "selection_contract": str(directory / "selection-contract.json"),
            "semantic_complete": False, "learning_started": False,
            "next": "在当前本地会话完成逐题语义备课并用 prepare_d_review_408.py --local-dir <定稿目录> 验收。"}


def export_selection(full_receipt, repo, policy, preparation_context_path=None, *, local_authoring=False,
                     hot_capture_proof=None):
    repo = Path(repo).resolve()
    year_exclusion = exam_year_plan.build_exclusion(repo, full_receipt['target_date'])
    root = Path(full_receipt["content_root"])
    original_manifest = read(root / "manifest.json")
    eligibility = read(root / "runtime-eligibility.json")
    all_scope = read(root / "return-template/due_concepts.json")
    index = read(root / "concept-index.json")
    concepts = {row["concept_id"]: read(root / row["path"]) for row in index}
    keyword_policy = policy.get("selection_policy") == KEYWORD_SELECTION_POLICY
    frequency_policy = policy.get("frequency_priority_policy") == exam_priority.POLICY
    frequency = (exam_priority.priority_bundle(
        {point: value['personal_context'] for point, value in concepts.items()}, full_receipt['source_commit'])
        if frequency_policy else None)
    recent = {}
    preparation, preparation_files = preparation_evidence(preparation_context_path, full_receipt['target_date'])
    focused_scope = focused_preparation_scope(preparation)
    require((focused_scope or {}).get('mode') != 'annual_weak_representatives', 'use_local_focused_training_annual_sources_for_complete_paper')
    for point, value in concepts.items():
        summary = value.get("personal_context", {}).get("summary", {})
        profile = summary.get("learning_profile") or {}
        for ref in value.get("full_personal_sources", []):
            if "reviewed-knowledge-profiles/" in (ref.get("source_path") or ref.get("published_path", "")) and ref.get("status") == "included":
                obj = read(root / ref["package_path"])
                profile = obj.get("profile", obj)
                break
        recent[point] = recent_evidence(profile, full_receipt["target_date"], policy.get("recent_learning_days", 14))
    if preparation:
        for row in preparation.get('focus_records', []):
            point = row['concept_id']
            if point not in recent:
                continue
            record = {**row, 'priority_evidence_score': 2,
                      'evidence_role': 'raw_capture_preparation_advisory_not_formal_profile',
                      'source_refs': [{'path': 'preparation-evidence/' + row['source_path']}],
                      'independent_mastery_inferred': False}
            recent[point]['recent_records'].append(record)
            recent[point]['recent_difficulty_records'].append(record)
            recent[point]['recent_difficulty_count'] += 1
            recent[point]['recent_record_count'] += 1
            recent[point]['priority_evidence_score'] = max(2, recent[point]['priority_evidence_score'])
    day = dt.date.fromisoformat(full_receipt["target_date"])
    cooldown = cooldown_days(policy)
    # The frozen export only sees displays, formal dates and reviewed Capture
    # sources. Refresh with every real contact, including correct reviews that
    # intentionally leave the formal last-review date unchanged.
    options = {"hot_capture_proof": hot_capture_proof} if hot_capture_proof is not None else {}
    live_contacts = recent_contact.real_contacts(repo, day, strict=True, **options)["by_identity"]
    if preparation:
        for row in preparation.get('recent_contact_overrides', []):
            for identity in row.get('question_ids', []):
                require(EXAM_ID_RE.fullmatch(identity), 'preparation_contact_identity_invalid')
                fact = {'study_date': row['study_date'],
                        'source_role': 'annual_review_contact_not_original_attempt_date',
                        'evidence': 'preparation-evidence/' + row['source_path'],
                        'independent_mastery_inferred': False}
                live_contacts[identity] = latest_contact(live_contacts.get(identity), fact)
    candidates = {path: read(root / path) for concept in concepts.values()
                  for path in concept.get("candidate_files", [])}
    # Keep all three machine-readable views consistent. The frozen originals
    # remain intact; only this new package receives the current eligibility.
    candidate_view = recent_contact.apply_cooldown({
        "last_true_exam_contacts": eligibility.get("last_true_exam_contacts", {}),
        "candidates_by_formal_id": {path: value.get("eligibility", []) for path, value in candidates.items()},
    }, live_contacts, day, cooldown)
    eligibility = recent_contact.apply_cooldown(
        eligibility, candidate_view["last_true_exam_contacts"], day, cooldown)
    year_assessments = {}
    if year_exclusion['active']:
        for view in (candidate_view, eligibility):
            for surfaces in view.get('candidates_by_formal_id', {}).values():
                for surface in surfaces:
                    assessment = exam_year_plan.assess_item(repo, surface, year_exclusion)
                    key = (surface.get('true_item_id', ''), surface.get('main_knowledge', ''))
                    year_assessments[key] = {**assessment, 'concept_id': surface.get('main_knowledge')}
                    if not assessment['allowed']:
                        surface['eligible'] = False
                        surface['rejection_reasons'] = sorted(set(surface.get('rejection_reasons', []))
                                                              | {'planned_exam_year_exclusion'})
    validate_active_cooldown(candidate_view, day, cooldown)
    gate_consistency = validate_active_cooldown(eligibility, day, cooldown)
    if year_exclusion['active']:
        gate_consistency['exam_year_exclusion_sha256'] = year_exclusion['exclusion_sha256']
    contacts = eligibility["last_true_exam_contacts"]
    for path, candidate in candidates.items():
        candidate["eligibility"] = candidate_view["candidates_by_formal_id"][path]
        for material in candidate.get("canonical_exam_materials", []):
            material["last_learning_contact"] = contacts.get(material["true_item_id"])
    excluded = {}
    for identity, contact in contacts.items():
        gap = recent_contact.gap_days(contact, day)
        if EXAM_ID_RE.fullmatch(identity) and gap is not None and gap < cooldown:
            excluded[identity] = {"true_item_id": identity, "concept_ids": [],
                                  "last_learning_contact": contact, "gap_days": gap,
                                  "eligible_on": (dt.date.fromisoformat(contact["study_date"]) + dt.timedelta(days=cooldown)).isoformat()}
    rows = {}
    relevance_gaps = set()
    for point, concept in concepts.items():
        for candidate_file in concept.get("candidate_files", []):
            candidate = candidates[candidate_file]
            for surface in candidate.get("eligibility", []):
                identity = surface.get("true_item_id", "")
                if identity in excluded and point not in excluded[identity]["concept_ids"]:
                    excluded[identity]["concept_ids"].append(point)
                if not surface.get("eligible") or surface.get("main_knowledge") != point or not EXAM_ID_RE.fullmatch(identity):
                    continue
                if not focused_candidate_allowed(focused_scope, point, identity,
                                                 surface.get('contact_dependency_ids', [])):
                    continue
                material = next((item for item in candidate.get("canonical_exam_materials", [])
                                 if item.get("true_item_id") == identity), {})
                keywords = candidate_keywords(surface, material, point)
                match = difficulty_match(recent[point], keywords)
                if keyword_policy and match is None:
                    relevance_gaps.add(point)
                    continue
                contact = latest_contact(surface.get("last_learning_contact"), live_contacts.get(identity))
                gap = recent_contact.gap_days(contact, day)
                if gap is not None and gap < cooldown:
                    row = excluded.setdefault(identity, {"true_item_id": identity, "concept_ids": [],
                                                         "last_learning_contact": contact, "gap_days": gap})
                    if point not in row["concept_ids"]:
                        row["concept_ids"].append(point)
                    continue
                key = (point, identity, surface.get("surface_sha256"))
                row = rows.setdefault(key, {"concept_id": point, "true_item_id": identity,
                    "surface_sha256": surface.get("surface_sha256"), "last_learning_contact": contact,
                    "formal_ids": [], "candidate_files": [], "recent_priority_score": recent[point]["priority_evidence_score"]})
                row.update(keywords=keywords, recent_error_match=match,
                           parent_true_item_id=surface.get("parent_true_item_id"),
                           dependency_kind=surface.get("dependency_kind"),
                           contact_dependency_ids=surface.get("contact_dependency_ids", []),
                           question_type=surface.get("question_type", material.get("question_type")))
                if frequency_policy:
                    row['frequency_priority'] = copy.deepcopy(frequency['points'][point])
                if exam_year_plan.permits_point_fallback(year_exclusion):
                    assessment = year_assessments[(identity, point)]
                    row['exam_year_selection_stage'] = assessment['exam_year_selection_stage']
                    row['planned_year_overlap_points'] = assessment['overlap_points']
                    row['exam_year_point_overlap_policy'] = year_exclusion['point_overlap_policy']
                fid = Path(candidate_file).stem
                if fid not in row["formal_ids"]:
                    row["formal_ids"].append(fid)
                    row["candidate_files"].append(candidate_file)
    pool = sorted(rows.values(), key=candidate_order_key)
    for rank, row in enumerate(pool, 1):
        row["rank"] = rank
    target_count = selection_capacity(pool, policy["question_limit"])
    requested_subquestions = subquestion_minimum(policy) if keyword_policy else 0
    recommendation = keyword_selection_plan(pool, policy["question_limit"], requested_subquestions) if keyword_policy else []
    if keyword_policy:
        target_count = len(recommendation)
    annual_priority_audit = None
    if exam_year_plan.permits_point_fallback(year_exclusion):
        require(keyword_policy, 'annual_point_fallback_requires_recent_error_selection')
        if exam_year_plan.frequency_first_fallback(year_exclusion):
            require(frequency_policy, 'annual_frequency_fallback_requires_frequency_policy')
        annual_priority_audit = require_annual_stage_priorities(recommendation, pool, policy['question_limit'])
    # The binding minimum is what the registered pool can really supply; a
    # shortfall is reported, never filled with variants or relaxed gates.
    required_subquestions = min(requested_subquestions, sum(1 for row in recommendation if is_subquestion(row)))
    pool_bytes = encoded({"status": "eligible_candidates_not_selected", "candidate_pair_count": len(pool),
                          "unique_true_exam_count": len({r['true_item_id'] for r in pool}), "candidates": pool,
                          "selection_policy": policy.get("selection_policy", "oldest_actual_contact_known_first"),
                          "recent_error_relevance_required": keyword_policy})
    exclusion_rows = sorted(excluded.values(), key=lambda r: (r["gap_days"], r["true_item_id"]))
    exclusions_bytes = encoded({
        "schema": EXCLUSIONS_SCHEMA, "target_date": full_receipt["target_date"], "exclusion_days": cooldown,
        "count": len(exclusion_rows),
        "meaning": "这些真题在目标日期前不足 exclusion_days 天有真实接触，已移出候选池；不是候选，也不代表掌握程度。",
        "items": exclusion_rows})
    version = sha(encoded({"full_input_version": full_receipt["input_version"], "policy": policy,
                          "helper_sha256": sha(Path(__file__).read_bytes()), "candidate_pool_sha256": sha(pool_bytes),
                          "recent_contact_exclusions_sha256": sha(exclusions_bytes),
                          "project_instructions_sha256": None if local_authoring else sha((repo / PROJECT_INSTRUCTIONS_REL).read_bytes()),
                          "delivery": "local_directory" if local_authoring else "web_transfer",
                          "runtime_eligibility_sha256": sha(encoded(eligibility)),
                          "exam_year_exclusion_sha256": year_exclusion.get('exclusion_sha256'),
                          "preparation_evidence_sha256": sha(encoded(preparation)) if preparation else None}))
    directory = root.parent / ("selection-" + version[:16]) if local_authoring else Path(full_receipt["zip_path"]).parent / ("selection-" + version[:16])
    directory.mkdir(exist_ok=True)
    contract = {"schema": CONTRACT_SCHEMA, "input_version": version,
                "source_commit": full_receipt["source_commit"], "target_date": full_receipt["target_date"],
                "due_version": original_manifest["due_version"],
                "concept_priority_version": original_manifest["concept_priority_version"],
                "question_limit": policy["question_limit"], "target_question_count": target_count,
                "candidate_pool_sha256": sha(pool_bytes), "full_review_concept_ids": all_scope["review_concept_ids"],
                "priority_order": [f"verified_true_exam_and_exact_{cooldown}_day_real_contact_gate",
                                   "oldest_actual_contact_known_first", "recent_learning_breaks"],
                "recent_contact_exclusion_days": cooldown,
                "recent_contact_sources": sorted(recent_contact.BASIS_LABEL),
                "recent_contact_exclusions_sha256": sha(exclusions_bytes),
                "not_selected_means": "deferred_unanswered_not_completed"}
    if original_manifest.get("source_basis"):
        require(local_authoring and original_manifest["source_basis"] == full_receipt.get("source_basis"),
                "D_native_selection_source_mismatch")
        contract["source_basis"] = original_manifest["source_basis"]
    if preparation:
        contract['preparation_evidence_sha256'] = sha(encoded(preparation))
    if focused_scope:
        contract['focused_selection_scope'] = focused_scope
    if year_exclusion['active']:
        contract.update(exam_year_exclusion_sha256=year_exclusion['exclusion_sha256'],
                        exam_year_plan_sha256=year_exclusion['plan_sha256'],
                        exam_year_mapping_sha256=year_exclusion['mapping_sha256'],
                        planned_exam_year=year_exclusion['exam_year'])
        contract['priority_order'].insert(0, 'planned_annual_paper_and_assessed_point_exclusion')
    if keyword_policy:
        from morning_question_policy_408 import KEYWORD_POLICY, FREQUENCY_KEYWORD_POLICY
        order_policy = FREQUENCY_KEYWORD_POLICY if frequency_policy else KEYWORD_POLICY
        contract.update(selection_policy=KEYWORD_SELECTION_POLICY, ordering_policy=order_policy,
                        recent_learning_days=policy.get("recent_learning_days", 14),
                        recent_error_relevance_required=True,
                        diversity_constraint="one_question_per_canonical_point_then_minimize_keyword_overlap",
                        priority_order=[f"verified_true_exam_and_exact_{cooldown}_day_real_contact_gate",
                                        "recent_learner_error_relation", "keyword_diversity",
                                        "oldest_equivalent_actual_contact_known_first", "keyword_display_order"])
        if requested_subquestions:
            contract.update(min_independent_subquestions=required_subquestions,
                            requested_min_independent_subquestions=requested_subquestions)
    if frequency_policy:
        contract.update(frequency_priority_policy=exam_priority.POLICY,
                        frequency_priority_sha256=sha(encoded(frequency)))
        contract['priority_order'].insert(1, 'eligible_high_frequency_exhaustion_before_lower_tiers')
    if year_exclusion['active']:
        # The keyword branch replaces priority_order above; restore the hard
        # annual-paper gate ahead of every ranking preference.
        gate = 'planned_annual_paper_and_assessed_point_exclusion'
        contract['priority_order'] = [gate] + [p for p in contract['priority_order'] if p != gate]
        if exam_year_plan.permits_point_fallback(year_exclusion):
            contract['exam_year_point_overlap_policy'] = year_exclusion['point_overlap_policy']
            contract['annual_stage_capacity'] = annual_priority_audit
            contract['priority_order'] = ['planned_annual_exact_identity_and_given_answer_exclusion',
                'nonoverlap_pool_exhaustion_before_overlap_fallback'] + [
                    p for p in contract['priority_order'] if p != gate]
            if exam_year_plan.frequency_first_fallback(year_exclusion):
                contract['priority_order'] = ['planned_annual_exact_identity_and_given_answer_exclusion',
                    'eligible_high_frequency_exhaustion_before_lower_tiers',
                    'nonoverlap_before_overlap_within_same_frequency_tier'] + [
                    p for p in contract['priority_order'] if p not in {
                        'planned_annual_exact_identity_and_given_answer_exclusion',
                        'nonoverlap_pool_exhaustion_before_overlap_fallback',
                        'eligible_high_frequency_exhaustion_before_lower_tiers'}]
    if local_authoring:
        return _local_authoring_inputs(directory, root, full_receipt, repo, policy, contract, pool,
            pool_bytes, recommendation, exclusions_bytes, year_exclusion, preparation_files, recent, frequency)
    prompt = PROMPT.format(target_count=target_count, limit=policy["question_limit"], day=contract["target_date"],
                           commit=contract["source_commit"], version=version, cooldown=cooldown,
                           subquestion_rules=INDEPENDENT_SUBQUESTION_RULES)
    return_contract = RETURN_CONTRACT
    if keyword_policy:
        policy_text = (
            "本包采用近期错误与关键词规则，替代旧的全场仅按日期顺序：所有采用题必须来自候选池中"
            "有 recent_error_match 的记录；先覆盖尽量不同的细粒度关键词，同点同关键词候选优先"
            "最近真实接触更早者，已知日期优先于未知日期。选择题与大题子问同等竞争，例如40天前"
            "的相关子问优先于20天前的同关键词选择题。子问只能使用完整已验证题面；"
            "每个小问只要求当前任务；依赖前问时必须直接提供已核验的前问结果作为输入，不能要求学习者先做前问。当前小问答案仍隔离。"
            "最终按关键词组织排序，同关键词按日期从早到晚。selection-recommendation.json"
            "是可复算建议集合，允许在同等或更好关键词覆盖与接触优先级下作语义选择；"
            "rationale说明实际错误匹配、关键词、接触日期和与建议不同的原因。本地拒绝"
            "同点同关键词明有更早合格题却选更近题的回包。当前原生D仍限制每个主规范点至多一题，"
            "不同子问共享该点时参与同点竞争；不足30题如实保留缺项，不扩入无近期错误依据的点。\n"
        )
        prompt = policy_text + "\n" + prompt
        prompt = re.sub(r"2\. 最高排序优先级：.*?\n3\. 次优先级：.*?\n", (
            "2. 相关性硬门槛：只采用 recent_error_match 有来源的候选；与近期错误没有关系的旧题不进入本轮。\n"
            "3. 尽量覆盖不同关键词，同点同关键词候选按 last_learning_contact.study_date 早到晚，"
            "未知日期在已知日期之后，不区分选择题和已验证的大题子问；最终展示按关键词组织。\n"),
            prompt, flags=re.DOTALL)
        prompt = prompt.replace("最终展示顺序按接触日期早到晚", "最终展示顺序按关键词、同关键词接触日期早到晚")
        return_contract = policy_text + "\n" + RETURN_CONTRACT.replace(
            "最终展示顺序按接触日期早到晚", "最终展示顺序按关键词、同关键词接触日期早到晚")
        return_contract = return_contract.replace("oldest_verified_actual_contact_first_unknown_last", order_policy).replace(
            "按实际接触日期排序的队列", "按关键词、同关键词实际接触日期排序的队列")
    if required_subquestions:
        prompt = SUBQUESTION_QUOTA.format(target_count=target_count, minimum=required_subquestions) + "\n" + prompt
        return_contract = (f"本包selection-contract.json登记min_independent_subquestions={required_subquestions}："
                           f"回包中EXAM408-YYYY-QNN-Pxx独立小问少于{required_subquestions}道，本地导入会拒绝整包。\n\n"
                           + return_contract)
    if frequency_policy:
        frequency_text = (
            "2026-10-07高频优先：先守真题、14天真实接触及近期错误关联门禁，再按frequency-priority.json"
            "和candidate.frequency_priority选择。优先选尽当前合格高频点，再补中频、低频；不能因日期更早、"
            "关键词不同、题型或易读取而跳过仍可选择的高频点。仅涉及、未知、未见考查保留原标签。"
            "小问先在同一频次层内预留，严格高频优先导致小问不足时如实报告requested与实际可供数量，"
            "不为凑小问反向挤出高频。每主规范点至多一题，已选点的其他题不要求全选。"
            "rationale逐题记录主考/总考次、同版频次来源和低层补位的高层用尽依据。MCP只在固定版本核验，"
            "不能通过另查低频题或更换版本绕过候选池。本地会拒绝高频未用尽而采用低层的回包。"
            "最终按频次层、层内关键词和同关键词实际接触日期组织；本条优先于下面旧排序说明。\n\n")
        prompt = frequency_text + prompt
        return_contract = frequency_text + return_contract
    if year_exclusion['active']:
        year_text = (
            f"本轮目标日期 {contract['target_date']} 已明确计划完成 {year_exclusion['exam_year']} 年整卷真题。"
            "先读 exam-year-exclusions.json 与 exam-year-candidate-audit.json：该年原题、独立小问、"
            "涉及其主考或兼考规范点的题，以及泄露相应前问结果的题均已禁止。"
            "候选自身全部 assessed_points 都须无交集，不能只比较本次主复盘点；不把纯背景点或前置标签冒充考点。"
            "此硬门禁优先于30题目标、小问数量、高频排序、近期错误和14天门禁。"
            "不得从其他版本、别名、截图或MCP重新补回被排除题；不足按本包实际target_question_count交付。"
            "selection-contract保留年度排除哈希，回包前本地还会按最新明确年份计划复查。\n\n")
        if exam_year_plan.permits_point_fallback(year_exclusion):
            year_text = (
                f"本轮目标日期{contract['target_date']}计划做{year_exclusion['exam_year']}年真题。"
                "用户仅为这一天明确授权考点不足补位，先读exam-year-exclusions.json、"
                "exam-year-candidate-audit.json及selection-contract.annual_stage_capacity。"
                f"{year_exclusion['exam_year']}年原题、独立小问及提供该年题答案的前问依赖始终硬禁。"
                "候选exam_year_selection_stage=0为与该年全部主兼考点无重叠的第一层；"
                "stage=1为其他年份的同考点补位层。先在第一层内按高频、中频、低频等现行频次顺序"
                "选尽所有合法可选点；只有第一层在唯一题面、每主点一题、父子/给定答案冲突约束下"
                f"确实不足{contract['question_limit']}题，才从第二层补其差额。不能因第二层是高频就"
                "跳过第一层仍可选的中低频点；两层内部各守高频优先。补位仍守14天真实接触、"
                "近期真实错误关联与独立小问合同，不允许选计划年份原题。rationale记录每题层级、"
                "考点交集及第一层用尽/补位数量依据；不能靠选择冲突题或无依据宣称不足。"
                "原题、教学与轻微拓展也不得带入被禁年份原题或答案。回包与首次开题均重核登记候选池。"
                "下面旧项目说明的“所有年度考点硬禁”由这项当天显式例外覆盖；其他日期不继承例外。"
                "最终可按层级、层内频次和关键词组织；不把备题当成已学习。\n\n")
        if exam_year_plan.frequency_first_fallback(year_exclusion):
            year_text = (
                f"本轮目标日期{contract['target_date']}计划做{year_exclusion['exam_year']}年真题。"
                "用户最新明确选择“高频优先，允许重叠考点补位”，仅适用于该日期。"
                "先读exam-year-exclusions.json、exam-year-candidate-audit.json及"
                "selection-contract.annual_stage_capacity。"
                f"{year_exclusion['exam_year']}年原题、独立小问及提供该年题答案的前问依赖始终硬禁。"
                "candidate.exam_year_selection_stage=0为考点不重叠，stage=1为其他年份但考点重叠。"
                "选题顺序必须是：非重叠高频→其他年份重叠高频→非重叠中频→重叠中频→"
                "非重叠低频→重叠低频，其他频次标签保持同版原级别。"
                "只有当前频次层全部合法可选点用尽，才进入下一频次层；同一频次层内非重叠优先。"
                "不能因为中低频不重叠就挤出仍可选的重叠高频；也不能在同频次非重叠候选未用尽时"
                "直接采用重叠候选。每个优先级格按唯一题面、每主点一题和父子/给定答案冲突核验实际容量。"
                "14天真实接触、近期真实错误关联、独立小问登记下限仍保持，不选计划年份原题；"
                "小问在同频次内预留，高频不能被低频为凑比例挤出。"
                "rationale逐题说明频次、是否重叠、考点交集和更高优先级候选用尽依据。"
                "原题、讲解与拓展不得带入被禁年份原题或答案。回包和首次开题均核最新计划及登记池，"
                "先前“所有非重叠中低频也用尽后才补重叠”的旧Oct26输入不可继续使用，应保全并重备。"
                "这条最新日计划优先于下文旧项目措辞，其他日期与已开会话不自动改动。\n\n")
        prompt = year_text + prompt
        return_contract = year_text + return_contract
    selected_points = {r['concept_id'] for r in pool}
    paths = set()
    candidate_paths = {path for row in pool for path in row["candidate_files"]}
    for point in selected_points:
        concepts[point]["candidate_files"] = [path for path in concepts[point].get("candidate_files", [])
                                              if path in candidate_paths]

    def reachable(value):
        if isinstance(value, dict):
            if value.get("package_path") and (root / value["package_path"]).is_file():
                paths.add(value["package_path"])
            for child in value.values():
                reachable(child)
        elif isinstance(value, list):
            for child in value:
                reachable(child)

    for point in selected_points:
        paths.add("concepts/" + point + ".json")
        reachable(concepts[point])
    for row in pool:
        for candidate_file in row["candidate_files"]:
            paths.add(candidate_file)
            reachable(candidates[candidate_file])
    for p in (root / "scope").rglob("*"):
        if p.is_file():
            paths.add(p.relative_to(root).as_posix())
    for p in (root / "return-template").rglob("*"):
        if p.is_file():
            paths.add(p.relative_to(root).as_posix())
    for p in (root / "contracts").rglob("*"):
        if p.is_file() and p.name != "D_PROJECT_INSTRUCTIONS.txt":
            paths.add(p.relative_to(root).as_posix())
    paths.add("runtime-eligibility.json")
    if preparation:
        prompt = ('先读取 preparation-evidence/context.json 及其中引用的完整真实对话。'
                  '它补充2010、2011卷与近期尚待增强的实际断点，只作备题依据，不冒充正式掌握度。'
                  '先在本包机器候选池与高频优先门禁内按这些具体断点核对选题；'
                  '原始正确、选对理由不稳、提示后复述和助手错误须分开。\n\n'
                  + preparation.get('authoring_notes', '') + '\n\n' + prompt)
    if focused_scope:
        scope_text = ('本次用户限定范围是硬门禁：所有采用题的主复盘点必须在'
                      'selection-contract.focused_selection_scope.allowed_concept_ids内，'
                      '每点有指定年份真实学习证据；不得用其他到期点补足题数。'
                      'excluded_exam_years中的原题、小问和给定答案依赖均不进入候选池。'
                      '在这个有界池内继续执行14天、高频优先、小问名额及全局冲突检查。\n\n')
        prompt = scope_text + prompt
        return_contract = scope_text + return_contract
    data = {path: (root / path).read_bytes() for path in paths}
    data.update(preparation_files)
    data.update({path: encoded(candidates[path]) for path in candidate_paths})
    data.update({"concepts/" + point + ".json": encoded(concepts[point]) for point in selected_points})
    data["runtime-eligibility.json"] = encoded(eligibility)
    data["gate-consistency.json"] = encoded(gate_consistency)
    if year_exclusion['active']:
        data['exam-year-exclusions.json'] = encoded(year_exclusion)
        assessments = [year_assessments[key] for key in sorted(year_assessments)]
        data['exam-year-candidate-audit.json'] = encoded({
            'schema': 'cs408-exam-year-candidate-audit-v1', 'target_date': contract['target_date'],
            'exclusion_sha256': year_exclusion['exclusion_sha256'],
            'checked_count': len(assessments),
            'blocked_count': sum(not item['allowed'] for item in assessments), 'items': assessments,
            'formal_write_count': 0, 'learner_evidence_write_count': 0})
    source_map = [row for row in read(root / "source-map.json") if row["package_path"] in paths]
    data.update({"candidate-pool.json": pool_bytes, "selection-contract.json": encoded(contract),
                 "recent-learning.json": encoded({p: recent[p] for p in sorted(selected_points)}),
                 "recent-contact-exclusions.json": exclusions_bytes,
                 "concept-index.json": encoded([r for r in index if r["concept_id"] in selected_points]),
                 "source-map.json": encoded(source_map), "START_WEB_D.md": prompt.encode(),
                 "START_HERE.md": prompt.encode(), "RETURN_CONTRACT.md": return_contract.encode(),
                 "contracts/native-return-contract.md": (root / "RETURN_CONTRACT.md").read_bytes(),
                 "contracts/D_PROJECT_INSTRUCTIONS.txt": (repo / PROJECT_INSTRUCTIONS_REL).read_bytes(),
                 "return-template/selection-contract.json": encoded(contract)})
    if frequency_policy:
        data['frequency-priority.json'] = encoded(frequency)
    template = read(root / "return-template/manifest.fields.json")
    template["input_version"] = version
    template["ordering_policy"] = contract.get("ordering_policy", "oldest_verified_actual_contact_first_unknown_last")
    if keyword_policy:
        data["selection-recommendation.json"] = encoded({
            "binding": "non_exclusive_semantic_recommendation", "items": recommendation,
            "target_question_count": target_count, "requested_count": policy["question_limit"],
            "shortfall": policy["question_limit"] - target_count,
            "without_recent_error_evidence": sorted(relevance_gaps),
            "constraint": contract["diversity_constraint"], "selection_performed_for_study": False})
        if frequency_policy:
            recommendation_doc = json.loads(data['selection-recommendation.json'])
            recommendation_doc.update(frequency_priority_policy=exam_priority.POLICY,
                requested_min_independent_subquestions=requested_subquestions,
                supplied_min_independent_subquestions=required_subquestions,
                subquestion_shortfall=requested_subquestions - required_subquestions)
            data['selection-recommendation.json'] = encoded(recommendation_doc)
    # Show the verified preprocessed snapshot images; without it prepare falls back to the tight raw corpus crops.
    template["question_presentation"] = "verified_snapshot_assets_v1"
    data["return-template/manifest.fields.json"] = encoded(template)
    data["return-template/web_execution_report.json"] = encoded({
        "schema": "cs408-web-execution-report-v1", "source_commit": contract["source_commit"],
        "completed_units": [], "pending_unit_ids": [], "failures": []})
    data["return-template/README.md"] = (
        "这些是未完成的空模板。先确定本次采用题目，再把实际已完成/未完成的采用题写进执行报告。"
        "due_concepts的完整元数据必须保留，但不要将容量外所有知识点填为pending或unresolved。\n"
    ).encode()
    manifest = {"schema": SCHEMA, "artifact_kind": "morning_selection_input", "subject": "408", "project": "D",
                "source_commit": contract["source_commit"], "snapshot_id": contract["source_commit"],
                "target_date": contract["target_date"], "input_version": version, "question_limit": policy["question_limit"],
                "target_question_count": target_count, "candidate_pair_count": len(pool),
                "selected_representatives": False, "formal_write_count": 0, "learner_evidence_write_count": 0,
                "files": [{"path": path, "size": len(raw), "sha256": sha(raw), "role": "web_selection_input_private"}
                          for path, raw in sorted(data.items())]}
    archive = directory / f"408_morning_selection_{contract['target_date']}_{policy['question_limit']}.zip"
    with zipfile.ZipFile(archive, 'w', compression=zipfile.ZIP_DEFLATED, compresslevel=1) as z:
        for path, raw in sorted({**data, 'manifest.json': encoded(manifest)}.items()):
            info = zipfile.ZipInfo(path, (2026, 1, 1, 0, 0, 0))
            info.compress_type = zipfile.ZIP_DEFLATED
            z.writestr(info, raw)
    with zipfile.ZipFile(archive) as z:
        require(set(z.namelist()) == set(data) | {'manifest.json'}, 'morning_selection_inventory_mismatch')
        for row in manifest['files']:
            require(sha(z.read(row['path'])) == row['sha256'], 'morning_selection_hash_mismatch')
    prompt_path = directory / f"START_WEB_D_{contract['target_date']}_{policy['question_limit']}.md"
    prompt_path.write_text(prompt)
    registration = {'contract': contract, 'contract_sha256': sha(encoded(contract)), 'policy': policy,
                    'allowed_candidates': pool, 'input_zip': str(archive), 'input_zip_sha256': sha(archive.read_bytes())}
    register_path = repo / REGISTRY_REL / (version + '.json')
    register_path.parent.mkdir(parents=True, exist_ok=True)
    if register_path.exists():
        old = read(register_path)
        require(old['contract'] == contract and old['policy'] == policy, 'morning_selection_registration_conflict')
    register_path.write_bytes(encoded(registration))
    result = {'schema': SCHEMA, 'status': 'READY_FOR_WEB_PREPARATION', 'source_commit': contract['source_commit'],
              'target_date': contract['target_date'], 'input_version': version,
              'zip_path': str(archive), 'zip_sha256': registration['input_zip_sha256'], 'zip_bytes': archive.stat().st_size,
              'prompt_path': str(prompt_path), 'prompt_sha256': sha(prompt_path.read_bytes()),
              'question_limit': policy['question_limit'], 'target_question_count': target_count,
              'concept_count': len(selected_points), 'candidate_pair_count': len(pool),
              'unique_true_exam_count': len({r['true_item_id'] for r in pool}), 'part_count': 1,
              'recent_contact_exclusion_days': cooldown, 'recent_contact_exclusion_count': len(exclusion_rows),
              'source_full_input_version': full_receipt['input_version'], 'registration_path': str(register_path),
              'exporter_sha256': full_receipt['exporter_sha256'], 'formal_write_count': 0,
              'learner_evidence_write_count': 0, 'model_call_count': 0, 'learning_started': False}
    if year_exclusion['active']:
        result.update(exam_year_exclusion_sha256=year_exclusion['exclusion_sha256'],
                      planned_exam_year=year_exclusion['exam_year'],
                      exam_year_excluded_candidate_count=sum(not r['allowed'] for r in year_assessments.values()))
    (directory / 'receipt.json').write_bytes(encoded(result))
    return result


def scope_for_return(repo, contract, body, source_zip_sha256, *, local_directory=False, hot_capture_proof=None):
    repo = Path(repo).resolve()
    version = str(contract.get('input_version', ''))
    require(re.fullmatch('[0-9a-f]{64}', version), 'D_selection_input_version_invalid')
    registration = read(repo / REGISTRY_REL / (version + '.json'))
    require(registration['contract'] == contract, 'D_selection_contract_drift')
    for field in ('source_commit', 'source_basis', 'target_date', 'due_version', 'concept_priority_version'):
        require(body.get(field) == contract.get(field), 'D_selection_binding_drift:' + field)
    exam_year_plan.require_return(repo, body, contract)
    require(body['review_concept_ids'] == contract['full_review_concept_ids'], 'D_selection_full_scope_drift')
    require(not any('-P' in str(item.get('preferred_true_item_id') or item.get('true_item_id') or '')
                    for item in body.get('selected_items', [])),
            'D_subquestion_requires_concept_supplement_or_substitute')
    all_items = [item for name in ('selected_items', 'supplemental_items', 'substitute_items') for item in body.get(name, [])]
    require(len(all_items) == contract['target_question_count'] and len(all_items) <= contract['question_limit'],
            'D_selection_question_count_mismatch')
    allowed = {(r['concept_id'], r['true_item_id'], fid, r.get('surface_sha256'))
               for r in registration['allowed_candidates'] for fid in r['formal_ids']}
    points, identities, chosen = [], [], []
    for item in all_items:
        targets = item.get('review_target_concept_ids', [])
        require(len(targets) == 1, 'D_selection_one_point_per_question_required')
        identity = item.get('preferred_true_item_id') or item.get('true_item_id')
        require((targets[0], identity, item['question_id'], item.get('surface_sha256')) in allowed,
                'D_selection_candidate_not_in_registered_input')
        points.extend(targets)
        identities.append(identity)
        chosen.append(next(r for r in registration['allowed_candidates']
                           if r['concept_id'] == targets[0] and r['true_item_id'] == identity
                           and r.get('surface_sha256') == item.get('surface_sha256')
                           and item['question_id'] in r['formal_ids']))
    require(len(set(points)) == len(points) and len(set(identities)) == len(identities), 'D_selection_duplicate_question_or_point')
    minimum = contract.get('min_independent_subquestions', 0)
    if minimum:
        count = sum(1 for identity in identities if is_subquestion({'true_item_id': identity}))
        require(count >= minimum, f'D_selection_independent_subquestion_minimum:{count}<{minimum}')
    # Re-check with the newest real contacts: learning may have happened
    # between export and return, and older registrations predate the refresh.
    frozen_contacts = {}
    for candidate in registration['allowed_candidates']:
        identity = candidate['true_item_id']
        frozen_contacts[identity] = latest_contact(frozen_contacts.get(identity), candidate.get('last_learning_contact'))
    require_current_contacts(repo, body, contract, frozen_contacts, hot_capture_proof=hot_capture_proof)
    if contract.get('selection_policy') == KEYWORD_SELECTION_POLICY:
        current_year_exclusion = exam_year_plan.build_exclusion(repo, body['target_date'])
        refreshed_pool = current_annual_pool(repo, registration, body['target_date'], current_year_exclusion,
                                            hot_capture_proof=hot_capture_proof)
        refreshed_chosen = [next(row for row in refreshed_pool
                                 if row['concept_id'] == selected['concept_id']
                                 and row['true_item_id'] == selected['true_item_id']) for selected in chosen]
        if exam_year_plan.permits_point_fallback(current_year_exclusion):
            require_annual_stage_priorities(refreshed_chosen, refreshed_pool, contract['question_limit'])
        require_keyword_priorities(refreshed_chosen, refreshed_pool)
        if contract.get('frequency_priority_policy') == exam_priority.POLICY:
            require_frequency_priorities(refreshed_chosen, refreshed_pool)
        # Read from the locally registered, immutable candidate records rather
        # than trusting web-provided tags to dictate the displayed order.
        body['selection_input_version'] = version
        body['selection_keywords_by_identity'] = registered_keyword_metadata(repo, body)
    elif contract.get("source_basis"):
        body["selection_input_version"] = version
    deferred = [p for p in body['review_concept_ids'] if p not in points]
    if not deferred:
        return None
    policy = registration['policy']
    decision = {"schema": "cs408-D-local-subset-decision-v1", "authorization_kind": "explicit_user_question_limit",
                "user_request": policy['user_request'], "user_message_id": policy['user_message_id'],
                **{key: body.get(key) for key in ('task_id', 'source_commit', 'target_date', 'due_version', 'concept_priority_version')},
                ("source_manifest_sha256" if local_directory else "source_zip_sha256"): source_zip_sha256,
                "full_review_concept_ids": body['review_concept_ids'],
                "admitted_concept_ids": points, "question_limit": contract['question_limit'],
                "selection_input_version": version,
                "deferred_concepts": [{"concept_id": p, "reason": "explicit_user_question_limit_not_selected_unanswered"} for p in deferred]}
    path = repo / '.private/study-bridge/d-scope-decisions' / (sha(encoded(decision)) + '.json')
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(encoded(decision))
    return path
