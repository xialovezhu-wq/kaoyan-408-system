"""Durable, evidence-bound web-authored 408 learning profiles.

This store owns reviewed wording, not question outcomes or FSRS. Derived tutoring
refreshes copy these profiles without rewriting their semantic content.
"""
from __future__ import annotations

import argparse
from contextlib import contextmanager
import copy
import datetime as dt
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import tempfile
from urllib.parse import urlencode
from zoneinfo import ZoneInfo

ROOT_REL = Path(".private/knowledge-profiles-v1")
CURRENT_REL = ROOT_REL / "current.json"
SCHEMA = "cs408-reviewed-knowledge-profile-v1"
UPDATE_SCHEMA = "cs408-knowledge-profile-updates-v1"
INDEX_SCHEMA = "cs408-reviewed-knowledge-profile-index-v1"
SHA = re.compile(r"[0-9a-f]{64}")
CODE = re.compile(r"(?:DS|CO|OS|CN)\d{2}-\d{2}")
FORMAL = re.compile(r"(?:DS|CO|OS|CN)_(?:\d{4}|UNK)_\d{3}")
TEACHING_PLAN_SCHEMA = "cs408-teaching-plan-v1"
TEACHING_PLAN_MAX_BYTES = 12 * 1024
PROFILE_UPDATE_MAX_BYTES = 1024 * 1024
TEACHING_PLAN_HOT_BYTES = 2400
TEACHING_SUPPORT = {"proposed_only", "user_reported_helpful", "prompted_repair", "independent_transfer"}
TEACHING_TEXT_TURN_KINDS = {"utterance", "question", "answer", "feedback", "explanation", "frozen_feedback"}
TEACHING_FOCUS_SCHEMA = "cs408-evidence-teaching-focus-v1"
TEACHING_FOCUS_STATES = {"independent_demonstrated", "prompted_repair", "explicit_gap", "unverified"}
TEACHING_FOCUS_HOT_BYTES = 2400
CLAIMS_SCHEMA = "cs408-evidence-claims-v1"
CLAIM_KINDS = {"preference", "observation", "assessment", "review_need"}
CLAIM_BASES = {"explicit_statement", "observed_attempt", "reviewed_inference"}
CLAIM_STATES = TEACHING_FOCUS_STATES | {"user_reported"}
CLAIM_LIFECYCLES = {"current", "superseded", "retracted", "conflicted"}


def claim_key(scope, anchor):
    """An author-chosen stable anchor, never the changing claim wording."""
    require(isinstance(scope, str) and scope, "claim_scope_required")
    require(isinstance(anchor, str) and anchor.strip(), "claim_anchor_required")
    return "KCL-" + digest([scope, anchor.strip()])[:24]


def validate_claims(owner, value, bindings, previous=None):
    """Validate explicit authored judgments without mining old prose for facts."""
    if "claims" not in value and "claims_schema" not in value:
        require(not (previous or {}).get("claims"), "profile_claims_cannot_disappear")
        return
    require(value.get("claims_schema") == CLAIMS_SCHEMA, "profile_claims_schema_invalid")
    rows = value.get("claims")
    require(isinstance(rows, list), "profile_claims_array_required")
    prior = {row["claim_id"]: row for row in (previous or {}).get("claims", [])}
    seen = set()
    for row in rows:
        require(isinstance(row, dict), "profile_claim_invalid")
        anchor = row.get("anchor")
        require(isinstance(anchor, str) and 0 < len(anchor.strip()) <= 160, "claim_anchor_required")
        identity = claim_key(owner, anchor)
        require(row.get("claim_id") == identity and identity not in seen, "claim_identity_invalid")
        seen.add(identity)
        require(row.get("scope") == owner, "claim_scope_mismatch")
        require(row.get("kind") in CLAIM_KINDS, "claim_kind_invalid")
        require(row.get("basis") in CLAIM_BASES, "claim_basis_invalid")
        require(row.get("evidence_status") in CLAIM_STATES, "claim_evidence_status_invalid")
        require(row.get("lifecycle") in CLAIM_LIFECYCLES, "claim_lifecycle_invalid")
        require(isinstance(row.get("content"), str) and row["content"].strip(), "claim_content_required")
        for field in ("event_at", "valid_from", "valid_to"):
            stamp = row.get(field)
            if stamp is not None:
                require(isinstance(stamp, str), "claim_time_invalid:" + field)
                try:
                    if len(stamp) == 10:
                        dt.date.fromisoformat(stamp)
                    else:
                        parsed = dt.datetime.fromisoformat(stamp.replace("Z", "+00:00"))
                        require(parsed.tzinfo is not None, "claim_time_offset_required:" + field)
                except ValueError as exc:
                    raise ProfileError("claim_time_invalid:" + field) from exc
        sources, _ = _teaching_sources(row, bindings=bindings)
        require(any("user" in source["roles"] for source in sources.values()), "claim_requires_user_evidence")
        if row["kind"] == "preference":
            require(row["basis"] == "explicit_statement", "claim_preference_must_be_explicit")
        if row["evidence_status"] == "independent_demonstrated":
            require(row["basis"] == "observed_attempt" and
                    all(source["review_state"] == "reviewed" for source in sources.values()),
                    "claim_independent_requires_reviewed_observation")
        if row["basis"] == "reviewed_inference":
            require(row["evidence_status"] == "unverified", "claim_inference_is_not_observed_performance")
        if row["evidence_status"] == "prompted_repair":
            require(any(source["positions"].get("assistant") and source["positions"].get("user")
                        and min(source["positions"]["assistant"]) < max(source["positions"]["user"])
                        for source in sources.values()), "claim_repair_requires_ordered_response")
        supports = row.get("supports", [])
        require(isinstance(supports, list) and all(isinstance(item, str) for item in supports),
                "claim_supports_invalid")
        for item in supports:
            parts = item.split(":")
            require(parts[0] in {"overview", "short_summary", "next_check", "progress", "timeline"},
                    "claim_support_field_invalid")
            if parts[0] in {"progress", "timeline"}:
                require(len(parts) == 2 and parts[1].isdigit() and
                        int(parts[1]) < len(value.get(parts[0], [])), "claim_support_index_invalid")
            else:
                require(len(parts) == 1 and isinstance(value.get(parts[0]), str), "claim_support_field_missing")
        if row["lifecycle"] != "current" or identity in prior and _claim_content(row) != _claim_content(prior[identity]):
            require(isinstance(row.get("change_reason"), str) and row["change_reason"].strip(),
                    "claim_change_reason_required")
        replaces = row.get("supersedes", [])
        require(isinstance(replaces, list) and len(replaces) == len(set(replaces)) and
                all(key in prior and key != identity for key in replaces), "claim_supersedes_invalid")
        if any(prior[key]["basis"] == "observed_attempt" and prior[key]["lifecycle"] == "current"
               for key in replaces):
            require(row["lifecycle"] == "current" and row["evidence_status"] != "unverified"
                    and all(source["review_state"] == "reviewed" for source in sources.values()),
                    "claim_observed_supersession_requires_confirmed_evidence")
    require(set(prior) <= seen, "profile_claims_cannot_disappear")
    states = {row["claim_id"]: row["lifecycle"] for row in rows}
    for row in rows:
        require(all(states.get(key) == "superseded" for key in row.get("supersedes", [])),
                "claim_superseded_predecessor_must_be_inactive")


def _claim_content(row):
    return {key: value for key, value in row.items()
            if key not in {"recorded_at", "claim_sha256", "previous_claim_sha256"}}


def _stamp_claims(value, timestamp, previous=None):
    prior = {row["claim_id"]: row for row in (previous or {}).get("claims", [])}
    for row in value.get("claims", []):
        old = prior.get(row["claim_id"])
        row.pop("previous_claim_sha256", None)
        if old and _claim_content(old) == _claim_content(row):
            row["recorded_at"] = old.get("recorded_at")
            if old.get("previous_claim_sha256"):
                row["previous_claim_sha256"] = old["previous_claim_sha256"]
        else:
            row["recorded_at"] = timestamp
            if old:
                row["previous_claim_sha256"] = old.get("claim_sha256") or digest(old)
        row.pop("claim_sha256", None)
        row["claim_sha256"] = digest(row)


def claims_view(profile, view="direct"):
    """Expose provenance, not a new assessment or a fabricated legacy claim."""
    require(view in {"protected", "after_attempt", "direct"}, "claims_view_invalid")
    result = {"schema": CLAIMS_SCHEMA, "status": "legacy_unstructured", "claims": [],
              "legacy_fields_have_no_inferred_claims": True, "independent_mastery_inferred": False}
    if view == "protected":
        return {**result, "status": "protected", "content_fields_redacted": True}
    if profile.get("claims_schema") == CLAIMS_SCHEMA:
        result.update(status="available", claims=copy.deepcopy(profile.get("claims", [])))
    return result


def teaching_source_turns(document):
    """Read original conversation roles without converting or rewriting evidence.

    The archived v4 document bundle is already a native, fully hash-bound
    dialogue. Its learner role corresponds to user only in this read projection;
    original turn ordinals, source bytes, document scope and dates stay intact.
    """
    require(isinstance(document, dict), "teaching_plan_native_conversation_required")
    if document.get("schema_version") == "cs408-full-conversation-v1":
        require(document.get("complete_order_preserved") is True and document.get("truncated") is False,
                "teaching_plan_native_conversation_required")
        turns = document.get("turns")
        require(isinstance(turns, list) and document.get("turn_count") == len(turns),
                "teaching_plan_conversation_turn_count_invalid")
        return turns
    if document.get("schema") == "sealed-concept-review-408-v1":
        from concept_review_408 import SCHEMA as review_schema, digest as review_digest

        session = document.get("session")
        require(isinstance(session, dict) and session.get("schema") == review_schema
                and session.get("subject") == "408" and review_digest(session) == document.get("session_sha256"),
                "teaching_plan_concept_review_binding_invalid")
        history = session.get("conversation")
        require(isinstance(history, list) and history and all(isinstance(turn, dict)
                and turn.get("role") in {"user", "assistant", "system"}
                and isinstance(turn.get("text"), str) and turn["text"].strip() for turn in history),
                "teaching_plan_concept_review_turns_invalid")
        return [{**turn, "ordinal": i, "kind": "utterance"} for i, turn in enumerate(history, 1)]
    if document.get("schema_version") == "current-question-evidence-bundle-v4":
        from formal_problem_document_408 import _validate_v4_bundle, FormalProblemDocumentError
        try:
            _validate_v4_bundle(document)
        except FormalProblemDocumentError as exc:
            raise ProfileError("teaching_plan_legacy_dialogue_invalid:" + str(exc)) from exc
        turns = []
        for original in document["interaction_trace"]["turns"]:
            texts = [block["text"] for block in original["content_blocks"]
                     if isinstance(block, dict) and isinstance(block.get("text"), str)]
            turns.append({"ordinal": original["ordinal"],
                "role": "user" if original["role"] == "learner" else "assistant",
                "kind": "utterance", "text": "\n\n".join(texts)})
        return turns
    raise ProfileError("teaching_plan_native_conversation_required")


def _teaching_sources(plan, *, bindings=None):
    """Verify native original turns, without turning them into learning events."""
    refs = plan.get("source_refs")
    require(isinstance(refs, list) and 0 < len(refs) <= 8, "teaching_plan_sources_required")
    if bindings is not None:
        _source_refs(refs, bindings)
    sources, guards = {}, []
    for ref in refs:
        require(isinstance(ref, dict), "teaching_plan_source_invalid")
        sid, path_text = ref.get("id"), ref.get("path")
        require(isinstance(sid, str) and re.fullmatch(r"[A-Za-z0-9_-]{1,32}", sid)
                and sid not in sources, "teaching_plan_source_id_invalid")
        require(isinstance(path_text, str) and Path(path_text).is_absolute()
                and SHA.fullmatch(str(ref.get("sha256", ""))), "teaching_plan_source_path_invalid")
        require(ref.get("review_state") in {"reviewed", "pending_capture_teaching_only"},
                "teaching_plan_review_state_invalid")
        path = Path(path_text)
        require(path.is_file() and not path.is_symlink(), "teaching_plan_source_missing:" + sid)
        before = path.stat()
        require(before.st_size <= 16 * 1024 * 1024, "teaching_plan_source_too_large")
        raw = path.read_bytes()
        after = path.stat()
        require((before.st_size, before.st_mtime_ns, before.st_ctime_ns) ==
                (after.st_size, after.st_mtime_ns, after.st_ctime_ns), "teaching_plan_source_changed_during_read")
        require(hashlib.sha256(raw).hexdigest() == ref["sha256"], "teaching_plan_source_hash_drift:" + sid)
        document = json.loads(raw)
        turns = teaching_source_turns(document)
        indexed = {}
        for turn in turns:
            require(isinstance(turn, dict) and isinstance(turn.get("ordinal"), int)
                    and not isinstance(turn["ordinal"], bool) and turn["ordinal"] > 0
                    and turn["ordinal"] not in indexed, "teaching_plan_native_turn_identity_invalid")
            indexed[turn["ordinal"]] = turn
        require(list(indexed) == list(range(1, len(turns) + 1)),
                "teaching_plan_native_turn_order_invalid")
        selected = ref.get("turns")
        require(isinstance(selected, list) and 0 < len(selected) <= 16, "teaching_plan_turns_required")
        roles, role_positions, seen = set(), {}, set()
        for target in selected:
            require(isinstance(target, dict), "teaching_plan_turn_locator_invalid")
            ordinal, role, quote = target.get("ordinal"), target.get("role"), target.get("quote")
            require(isinstance(ordinal, int) and not isinstance(ordinal, bool)
                    and ordinal in indexed and ordinal not in seen, "teaching_plan_turn_locator_invalid")
            seen.add(ordinal)
            actual = indexed[ordinal]
            require(role in {"user", "assistant"} and actual.get("role") == role,
                    "teaching_plan_turn_role_mismatch")
            require(actual.get("kind") in TEACHING_TEXT_TURN_KINDS,
                    "teaching_plan_non_text_turn_kind")
            require(isinstance(quote, str) and quote.strip() and isinstance(actual.get("text"), str)
                    and quote in actual["text"], "teaching_plan_quote_not_in_original_turn")
            roles.add(role)
            role_positions.setdefault(role, []).append(ordinal)
        sources[sid] = {"roles": roles, "positions": role_positions, "review_state": ref["review_state"]}
        guards.append({"id": sid, "path": path_text, "sha256": ref["sha256"],
                       "size": after.st_size, "mtime_ns": after.st_mtime_ns, "ctime_ns": after.st_ctime_ns})
    return sources, guards


def validate_teaching_plan(plan, bindings):
    if plan is None:
        return
    require(isinstance(plan, dict) and plan.get("schema") == TEACHING_PLAN_SCHEMA,
            "teaching_plan_schema_invalid")
    require(isinstance(bindings, dict), "teaching_plan_verified_evidence_bindings_required")
    require(len(encoded(plan)) <= TEACHING_PLAN_MAX_BYTES, "teaching_plan_too_large")
    sources, _ = _teaching_sources(plan, bindings=bindings)

    def text_field(value, label):
        require(isinstance(value, str) and value.strip(), "teaching_plan_text_required:" + label)

    def source_ids(value, *, nonempty=True):
        require(isinstance(value, list) and (value or not nonempty)
                and all(isinstance(sid, str) and sid in sources for sid in value)
                and len(value) == len(set(value)), "teaching_plan_source_ids_invalid")
        return value

    for field in ("learner_entry", "first_breakpoint"):
        item = plan.get(field)
        require(isinstance(item, dict), "teaching_plan_fact_required:" + field)
        text_field(item.get("text"), field)
        ids = source_ids(item.get("source_ids"))
        require(any("user" in sources[sid]["roles"] for sid in ids),
                "teaching_plan_learner_fact_requires_user_turn")
    text_field(plan.get("first_action"), "first_action")
    explanation = plan.get("explanation")
    require(isinstance(explanation, dict) and explanation.get("support") in TEACHING_SUPPORT,
            "teaching_plan_support_invalid")
    text_field(explanation.get("strategy"), "explanation.strategy")
    support = explanation["support"]
    ids = source_ids(explanation.get("source_ids"), nonempty=support != "proposed_only")
    if support != "proposed_only":
        require(any("user" in sources[sid]["roles"] for sid in ids),
                "teaching_plan_effectiveness_requires_user_turn")
    if support in {"prompted_repair", "independent_transfer"}:
        require(any(sources[sid]["positions"].get("assistant") and sources[sid]["positions"].get("user")
                    and min(sources[sid]["positions"]["assistant"]) < max(sources[sid]["positions"]["user"])
                    for sid in ids), "teaching_plan_repair_requires_ordered_response")
    if support == "independent_transfer":
        require(all(source["review_state"] == "reviewed" for source in sources.values()),
                "teaching_plan_pending_capture_cannot_prove_independent_transfer")
    visual = plan.get("visual")
    require(isinstance(visual, dict) and isinstance(visual.get("kind"), str)
            and visual["kind"].strip() and len(visual["kind"]) <= 64, "teaching_plan_visual_kind_invalid")
    text_field(visual.get("focus"), "visual.focus")
    for field in ("elements", "steps"):
        if field in visual:
            require(isinstance(visual[field], list) and len(visual[field]) <= 6
                    and all(isinstance(v, str) and v.strip() for v in visual[field]),
                    "teaching_plan_visual_items_invalid:" + field)
    for field in ("avoid", "check"):
        if field in plan:
            text_field(plan[field], field)
    if "visual_feedback" in plan:
        _validate_visual_feedback(plan["visual_feedback"], sources, source_ids)


def _validate_visual_feedback(feedback, sources, source_ids):
    """Validate cited turns, keeping preference separate from observed learning.

    The writer adjudicates what the quoted words mean. Structural validation
    cannot turn a liking statement or an assistant's assessment into success.
    """
    required = {"form", "scope", "preference", "observed_effect", "next_adjustment", "next_check"}
    require(isinstance(feedback, dict) and required <= feedback.keys()
            and feedback.keys() <= required | {"comparison"}, "visual_feedback_fields_invalid")

    def text(value, field, maximum=400):
        require(isinstance(value, str) and value.strip() and len(value) <= maximum,
                "visual_feedback_text_invalid:" + field)

    def user_only(ids, field):
        require(all(sources[sid]["roles"] == {"user"} for sid in ids),
                "visual_feedback_user_only_evidence_required:" + field)

    text(feedback["form"], "form", 64)
    text(feedback["scope"], "scope", 240)
    text(feedback["next_adjustment"], "next_adjustment")
    text(feedback["next_check"], "next_check")
    preference = feedback["preference"]
    require(isinstance(preference, dict) and set(preference) == {"status", "text", "source_ids"}
            and preference["status"] in {"explicit_preference", "observed_tendency", "unknown"},
            "visual_feedback_preference_invalid")
    text(preference["text"], "preference.text")
    ids = source_ids(preference["source_ids"], nonempty=preference["status"] != "unknown")
    user_only(ids, "preference")
    effect = feedback["observed_effect"]
    require(isinstance(effect, dict) and set(effect) == {"support", "text", "source_ids"}
            and effect["support"] in TEACHING_SUPPORT, "visual_feedback_effect_invalid")
    text(effect["text"], "observed_effect.text")
    support = effect["support"]
    ids = source_ids(effect["source_ids"], nonempty=support != "proposed_only")
    if support != "proposed_only":
        require(any("user" in sources[sid]["roles"] for sid in ids),
                "visual_feedback_effect_requires_user_turn")
    if support in {"prompted_repair", "independent_transfer"}:
        require(any(sources[sid]["positions"].get("assistant") and sources[sid]["positions"].get("user")
                    and min(sources[sid]["positions"]["assistant"]) < max(sources[sid]["positions"]["user"])
                    for sid in ids), "visual_feedback_repair_requires_ordered_response")
    if support == "independent_transfer":
        require(all(source["review_state"] == "reviewed" for source in sources.values()),
                "visual_feedback_pending_capture_cannot_prove_independent_transfer")
    if "comparison" in feedback:
        comparison = feedback["comparison"]
        require(isinstance(comparison, dict) and set(comparison) == {"reference", "finding", "source_ids"},
                "visual_feedback_comparison_invalid")
        text(comparison["reference"], "comparison.reference", 240)
        text(comparison["finding"], "comparison.finding")
        user_only(source_ids(comparison["source_ids"]), "comparison")


def teaching_plan_source_state(plan):
    """Cold verification; selected-source guards are kept only in route metadata."""
    if not plan:
        return {"current": True, "guards": []}
    try:
        _, guards = _teaching_sources(plan)
        return {"current": True, "guards": guards}
    except (ProfileError, OSError, ValueError, TypeError, KeyError) as exc:
        return {"current": False, "guards": [], "reason": str(exc)}


def validate_teaching_focus(focus, bindings):
    """Validate an author's narrow evidence judgments; never infer mastery.

    Quotes establish provenance, not correctness. The reviewing author still
    owns whether a response was independent, prompted or an unresolved gap.
    """
    if focus is None:
        return
    require(isinstance(focus, dict) and focus.get("schema") == TEACHING_FOCUS_SCHEMA,
            "teaching_focus_schema_invalid")
    require(isinstance(bindings, dict), "teaching_focus_verified_evidence_bindings_required")
    require(len(encoded(focus)) <= TEACHING_PLAN_MAX_BYTES, "teaching_focus_too_large")
    sources, _ = _teaching_sources(focus, bindings=bindings)
    facets = focus.get("facets")
    require(isinstance(facets, list) and 0 < len(facets) <= 8, "teaching_focus_facets_required")
    mechanisms = set()
    for facet in facets:
        require(isinstance(facet, dict), "teaching_focus_facet_invalid")
        for key, maximum in (("mechanism", 160), ("basis", 500)):
            value = facet.get(key)
            require(isinstance(value, str) and value.strip() and len(value) <= maximum,
                    "teaching_focus_text_invalid:" + key)
        require(facet["mechanism"] not in mechanisms, "teaching_focus_duplicate_mechanism")
        mechanisms.add(facet["mechanism"])
        state, treatment = facet.get("status"), facet.get("treatment")
        require(state in TEACHING_FOCUS_STATES, "teaching_focus_status_invalid")
        allowed = {"independent_demonstrated": {"brief"}, "explicit_gap": {"focus"},
                   "prompted_repair": {"focus", "check"}, "unverified": {"focus", "check"}}
        require(treatment in allowed[state], "teaching_focus_treatment_conflicts_with_evidence")
        ids = facet.get("source_ids")
        require(isinstance(ids, list) and ids and len(ids) == len(set(ids))
                and all(isinstance(sid, str) and sid in sources for sid in ids),
                "teaching_focus_source_ids_invalid")
        require(any("user" in sources[sid]["roles"] for sid in ids),
                "teaching_focus_requires_user_turn")
        if state == "independent_demonstrated":
            require(all(sources[sid]["review_state"] == "reviewed" for sid in ids),
                    "teaching_focus_pending_cannot_prove_independent_response")
        if state == "prompted_repair":
            require(any(sources[sid]["positions"].get("assistant") and sources[sid]["positions"].get("user")
                        and min(sources[sid]["positions"]["assistant"]) < max(sources[sid]["positions"]["user"])
                        for sid in ids), "teaching_focus_repair_requires_ordered_response")


def teaching_focus_hot(focus):
    if not focus:
        return {}
    compact = {"schema": TEACHING_FOCUS_SCHEMA, "facets": copy.deepcopy(focus["facets"]),
               "evidence_scope": "reviewed_narrow_performance_not_whole_point_mastery",
               "current_answer_overrides_historical_treatment": True}
    if len(encoded(compact)) > TEACHING_FOCUS_HOT_BYTES:
        return {"teaching_focus_status": "omitted_for_budget"}
    return {"teaching_focus_status": "available", "teaching_focus": compact}


def profile_teaching_source_state(value):
    """Both kinds of personal teaching advice share exact-source invalidation."""
    states = [teaching_plan_source_state((value or {}).get(key))
              for key in ("teaching_plan", "teaching_focus")]
    states.extend(teaching_plan_source_state(claim) for claim in (value or {}).get("claims", [])
                  if claim.get("lifecycle") in {"current", "conflicted"})
    guards = {guard["path"]: guard for state in states for guard in state["guards"]}
    return {"current": all(state["current"] for state in states), "guards": list(guards.values()),
            "reasons": [state["reason"] for state in states if not state["current"]]}


def teaching_plan_hot(plan):
    """A whole compact plan or an explicit omission; never clip its semantics."""
    if not plan:
        return {}
    compact = {key: copy.deepcopy(plan[key]) for key in (
        "schema", "learner_entry", "first_breakpoint", "first_action", "explanation", "visual", "avoid", "check",
        "visual_feedback")
        if key in plan}
    compact["evidence_scope"] = "teaching_only_no_learning_event_or_mastery_update"
    compact["source_states"] = {ref["id"]: ref["review_state"] for ref in plan["source_refs"]}
    if len(encoded(compact)) > TEACHING_PLAN_HOT_BYTES:
        return {"teaching_plan_status": "omitted_for_budget", "teaching_plan_omitted_for_budget": True}
    from interactive_teaching_408 import compact_recipe
    from teaching_research_408 import POLICY_VERSION, requires_visual_recompile, current_strategy
    # Historical feedback and its source IDs stay exact. Current delivery is a
    # separate presentation decision made by the user's latest instruction.
    compact['current_delivery_override'] = {
        'policy_version': POLICY_VERSION, 'text_primary': True,
        'visual_required_for_substantive_teaching': True,
        'exceptions': ['current_explicit_text_only', 'single_fact_confirmation'],
        'historical_feedback_is_not_current_execution': True,
        'effectiveness': 'unverified'}
    if requires_visual_recompile(compact.get('visual', {})):
        old_visual = compact['visual']
        compact['historical_delivery_source'] = 'teaching_plan_evidence_object_preserves_original_plan_and_feedback'
        compact['visual'] = {
            'kind': 'current_question_clickable_compile_required',
            'focus': compact.get('first_action') or compact.get('first_breakpoint', {}).get('text') or '当前题机制',
            'elements': list(old_visual.get('elements', [])),
            'steps': list(old_visual.get('steps', [])),
            'status': 'compile_required_not_yet_rendered_or_effect_verified'}
        compact['explanation'] = {'strategy': current_strategy(compact.get('explanation', {}).get('strategy', ''),
                compact.get('first_action', '从当前题条件开始')),
            'support': 'proposed_only', 'source_ids': []}
    # Delivery is a derived projection, not a revision of the evidence-backed
    # plan or of the user's historical preference/effectiveness assertions.
    compact["source_visual_policy"] = {
        "version": POLICY_VERSION, "chat_linear_text_required": True,
        "visible_main_text_required": True, "visual_secondary": True,
        "source": "question_visual_template.source_visuals",
        "layout": "先用聊天线性正文讲清本题获准的条件、步骤与因果；对应可点击图解辅助理解，主线理由无需点击，源图后台参考。",
        "scope": "只重绘本题及当前小点允许对象，遵守syllabus_context.teaching_boundary；不复制扫描布局或父题答案。",
        "evidence_status": "derived_delivery_policy_not_learning_evidence",
    }
    compact["interaction"] = compact_recipe(compact)
    return {"teaching_plan_status": "available", "teaching_plan": compact}


def teaching_plan_guards_current(guards):
    """Hot boundary: stat only the selected point's explicitly bound originals."""
    for guard in guards:
        try:
            path = Path(guard["path"])
            if path.is_symlink():
                return False
            info = path.stat()
            if not path.is_file() or (info.st_size, info.st_mtime_ns, info.st_ctime_ns) != (
                    guard["size"], guard["mtime_ns"], guard["ctime_ns"]):
                return False
        except (OSError, KeyError, TypeError):
            return False
    return True


class ProfileError(ValueError):
    pass


def encoded(value):
    return (json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n").encode("utf-8")


def digest(value):
    return hashlib.sha256(encoded(value)).hexdigest()


def subpoint_key(concept_key, title):
    """Stable identity under a canonical point; wording revisions change only content SHA."""
    import unicodedata
    label = re.sub(r"\s+", " ", unicodedata.normalize("NFKC", title)).strip().casefold()
    return "KSP-" + digest([concept_key, label])[:24]


def validate_subpoints(code, rows, bindings):
    require(isinstance(rows, list), "profile_subpoints_array_required")
    seen = set()
    for row in rows:
        require(isinstance(row, dict), "profile_subpoint_invalid")
        title = row.get("title")
        require(isinstance(title, str) and title.strip(), "profile_subpoint_title_required")
        key = subpoint_key(code, title)
        require(row.get("subpoint_id") == key and key not in seen,
                "profile_subpoint_identity_invalid")
        seen.add(key)
        for field in ("short_summary", "next_check"):
            require(isinstance(row.get(field), str) and row[field].strip(),
                    "profile_subpoint_text_required:" + field)
        require(len(row["short_summary"].encode()) <= 2400, "profile_subpoint_summary_too_large")
        require(isinstance(row.get("keywords"), list) and row["keywords"]
                and all(isinstance(v, str) and v.strip() for v in row["keywords"]),
                "profile_subpoint_keywords_required")
        require(isinstance(row.get("evidence"), list) and row["evidence"],
                "profile_subpoint_evidence_required")
        _source_refs(row.get("source_refs"), bindings)
        validate_teaching_plan(row.get("teaching_plan"), bindings)
        validate_teaching_focus(row.get("teaching_focus"), bindings)


def _validate_subpoint_continuity(repo, code, raw, previous):
    """A full replacement must account for every point in its stated baseline."""
    old_ids = set()
    prior = load_profile_version(repo, code, previous)
    if prior is not None:
        old_ids = {row["subpoint_id"] for row in prior.get("subpoints", [])}
    new_ids = {row["subpoint_id"] for row in raw.get("subpoints", [])}
    removed = raw.get("removed_subpoint_ids", [])
    reasons = raw.get("removal_reasons", {})
    require(isinstance(removed, list) and all(isinstance(key, str) for key in removed)
            and len(removed) == len(set(removed)), "profile_removed_subpoint_ids_invalid:" + code)
    removed_ids = set(removed)
    require(isinstance(reasons, dict) and set(reasons) == removed_ids
            and all(isinstance(reason, str) and reason.strip() for reason in reasons.values()),
            "profile_subpoint_removal_reasons_invalid:" + code)
    require(removed_ids <= old_ids, "profile_removed_subpoint_not_in_previous:" + code)
    require(not removed_ids & new_ids, "profile_removed_subpoint_still_present:" + code)
    missing = old_ids - new_ids - removed_ids
    require(not missing, "profile_subpoint_loss_requires_explicit_removal:" + code + ":" + ",".join(sorted(missing)))
    _validate_teaching_plan_update(raw, prior or {}, code)
    _validate_teaching_focus_continuity(raw, prior or {}, code)
    old_subpoints = {row["subpoint_id"]: row for row in (prior or {}).get("subpoints", [])}
    for row in raw.get("subpoints", []):
        _validate_teaching_plan_update(row, old_subpoints.get(row["subpoint_id"], {}), row["subpoint_id"])
        _validate_teaching_focus_continuity(row, old_subpoints.get(row["subpoint_id"], {}), row["subpoint_id"])
    # Removing a whole subpoint already requires its explicit ID and reason;
    # its plan retires with that point, rather than creating a phantom target.


def _validate_teaching_focus_continuity(target, previous, identity):
    old, new = previous.get("teaching_focus"), target.get("teaching_focus")
    update = target.get("teaching_focus_update")
    if update is not None:
        require(isinstance(update, dict) and set(update) == {"action", "reason"}
                and update["action"] in {"retain", "revise", "retire"}
                and isinstance(update["reason"], str) and update["reason"].strip()
                and len(update["reason"]) <= 400, "teaching_focus_update_invalid:" + identity)
        action = update["action"]
        require((action == "retain" and old is not None and new == old)
                or (action == "revise" and new is not None and new != old)
                or (action == "retire" and old is not None and new is None),
                "teaching_focus_update_action_mismatch:" + identity)
    elif old is not None:
        require(new is not None, "teaching_focus_loss_requires_explicit_retirement:" + identity)
        require(new == old, "teaching_focus_change_requires_explicit_revision:" + identity)


def load_profile_version(repo, code, version):
    """Read one immutable predecessor, including during operation recovery."""
    if version is None:
        return None
    require(isinstance(version, str) and SHA.fullmatch(version), "profile_previous_version_invalid")
    prior = read(Path(repo) / ROOT_REL / "objects" / (version + ".json"))
    require(digest(prior) == version and prior.get("schema") == SCHEMA
            and prior.get("concept_key") == code, "profile_previous_object_binding_mismatch:" + code)
    return prior


def _validate_teaching_plan_update(target, previous, identity):
    old, new = previous.get("teaching_plan"), target.get("teaching_plan")
    update = target.get("teaching_plan_update")
    if update is not None:
        require(isinstance(update, dict) and set(update) == {"action", "reason"}
                and update["action"] in {"retain", "revise", "retire"}
                and isinstance(update["reason"], str) and update["reason"].strip()
                and len(update["reason"]) <= 400, "teaching_plan_update_invalid:" + identity)
        action = update["action"]
        require((action == "retain" and old is not None and new == old)
                or (action == "revise" and new is not None and new != old)
                or (action == "retire" and old is not None and new is None),
                "teaching_plan_update_action_mismatch:" + identity)
    elif old is not None:
        require(new is not None, "teaching_plan_loss_requires_explicit_retirement:" + identity)
        require(new == old, "teaching_plan_change_requires_explicit_revision:" + identity)


def require(ok, reason):
    if not ok:
        raise ProfileError(reason)


def read(path):
    require(path.is_file() and not path.is_symlink(), "profile_file_missing_or_unsafe:" + str(path))
    return json.loads(path.read_text(encoding="utf-8"))


def atomic(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    require(not any(p.is_symlink() for p in (path, *path.parents)), "profile_destination_symlink")
    raw = encoded(value)
    fd, temporary = tempfile.mkstemp(prefix=".profile-", dir=path.parent)
    try:
        with os.fdopen(fd, "wb") as stream:
            stream.write(raw)
            stream.flush()
            os.fsync(stream.fileno())
        os.chmod(temporary, 0o600)
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)
    return hashlib.sha256(raw).hexdigest()


def content_write(root, value):
    sha = digest(value)
    path = root / "objects" / (sha + ".json")
    if path.exists():
        require(path.read_bytes() == encoded(value), "profile_content_address_conflict")
    else:
        atomic(path, value)
    return {"sha256": sha, "path": "objects/" + sha + ".json"}


def current_index(repo):
    path = Path(repo) / CURRENT_REL
    if not path.exists():
        return {"schema": INDEX_SCHEMA, "profiles": {}, "operations": {}, "generation": None}
    value = read(path)
    require(value.get("schema") == INDEX_SCHEMA and isinstance(value.get("profiles"), dict)
            and isinstance(value.get("operations"), dict), "profile_index_invalid")
    require(value.get("generation") == digest({k: v for k, v in value.items() if k != "generation"}),
            "profile_index_generation_mismatch")
    return value


def load_profiles(repo):
    root = Path(repo) / ROOT_REL
    index = current_index(repo)
    values = {}
    for code, ref in index["profiles"].items():
        require(CODE.fullmatch(code) and SHA.fullmatch(str(ref.get("sha256", ""))), "profile_reference_invalid")
        expected = "objects/" + ref["sha256"] + ".json"
        require(ref.get("path") == expected, "profile_reference_path_invalid")
        value = read(root / expected)
        require(digest(value) == ref["sha256"] and value.get("schema") == SCHEMA
                and value.get("concept_key") == code, "profile_object_binding_mismatch")
        values[code] = {**value, "version": ref["sha256"]}
    return {"generation": index["generation"], "profiles": values}


def _taxonomy(repo):
    result = {}
    for line in (Path(repo) / "知识点标签表.md").read_text(encoding="utf-8").splitlines():
        match = re.fullmatch(r"\s*-\s+((?:DS|CO|OS|CN)\d{2}-\d{2})\s+(.+?)\s*", line)
        if match:
            result[match[1]] = match[2]
    require(result, "profile_canonical_knowledge_table_missing")
    return result


def _updates(value):
    if isinstance(value, dict):
        require(value.get("schema") == UPDATE_SCHEMA, "profile_update_schema_invalid")
        value = value.get("updates")
    require(isinstance(value, list) and all(isinstance(row, dict) for row in value), "profile_updates_array_required")
    return value


def _source_refs(refs, bindings):
    require(isinstance(refs, list) and refs, "profile_source_refs_required")
    require(isinstance(bindings, dict), "profile_verified_evidence_bindings_required")
    for ref in refs:
        require(isinstance(ref, dict) and isinstance(ref.get("path"), str)
                and SHA.fullmatch(str(ref.get("sha256", ""))), "profile_source_ref_invalid")
        bound = bindings.get(ref["path"])
        expected = bound.get("sha256") if isinstance(bound, dict) else bound
        require(expected == ref["sha256"], "profile_source_ref_not_verified:" + ref["path"])
    return copy.deepcopy(refs)


def _question_refs(repo, refs, formal_bindings):
    require(isinstance(refs, list), "profile_question_refs_array_required")
    result = []
    for raw in refs:
        require(isinstance(raw, dict), "profile_question_ref_invalid")
        row = copy.deepcopy(raw)
        fid = row.get("formal_id")
        logical = row.get("logical_question_id")
        if logical and formal_bindings is not None:
            resolved = formal_bindings.get(logical)
            if isinstance(resolved, dict):
                resolved = resolved.get("formal_id")
            require(resolved and (not fid or fid == resolved), "profile_logical_question_binding_missing:" + str(logical))
            fid = row["formal_id"] = resolved
        require(fid or logical, "profile_question_identity_required")
        if not fid:
            # prepare can validate a future local ID; apply must bind it.
            require(formal_bindings is None, "profile_unresolved_logical_question")
            row.pop("obsidian_uri", None)
            row.pop("card_path", None)
            result.append(row)
            continue
        require(isinstance(fid, str) and FORMAL.fullmatch(fid), "profile_formal_id_invalid")
        card = Path(repo) / (fid + ".md")
        require(card.is_file() and not card.is_symlink(), "profile_formal_card_missing:" + fid)
        actual_sha = hashlib.sha256(card.read_bytes()).hexdigest()
        row["card_path"] = str(card.resolve())
        row["card_relpath"] = card.name
        row["card_sha256"] = actual_sha
        # URI points to a real safe card. A formal ID is not evidence of a true
        # exam year; supplied canonical identity remains source metadata only.
        from question_visual_links_408 import visual_link
        row.update(visual_link(Path(repo), fid))
        result.append(row)
    return result


def validate_updates(repo, updates, expected_points=None, evidence_bindings=None,
                     formal_bindings=None, check_versions=True):
    repo = Path(repo).resolve()
    taxonomy = _taxonomy(repo)
    index = current_index(repo)
    result, seen = [], set()
    for raw in _updates(updates):
        code = raw.get("concept_key")
        require(code in taxonomy and code not in seen, "profile_unknown_or_duplicate_concept:" + str(code))
        seen.add(code)
        require("expected_previous_version" in raw, "profile_expected_previous_version_required")
        previous = raw["expected_previous_version"]
        require(previous is None or (isinstance(previous, str) and SHA.fullmatch(previous)), "profile_previous_version_invalid")
        if check_versions:
            require(previous == index["profiles"].get(code, {}).get("sha256"), "profile_version_conflict:" + code)
        for field in ("overview", "short_summary", "next_check"):
            require(isinstance(raw.get(field), str) and raw[field].strip(), "profile_text_required:" + field)
        require(len(raw["short_summary"].encode("utf-8")) <= 2400, "profile_short_summary_exceeds_2400_bytes")
        # Complete historical evidence grows as new observations are appended.
        # Keep a bounded cold-object budget without trimming the old evidence.
        require(len(encoded(raw)) <= PROFILE_UPDATE_MAX_BYTES, "profile_update_too_large")
        for field in ("timeline", "progress"):
            require(isinstance(raw.get(field), list), "profile_list_required:" + field)
        aliases = raw.get("aliases", [])
        require(isinstance(aliases, list) and all(isinstance(a, str) and a.strip() for a in aliases), "profile_aliases_invalid")
        for alias in aliases:
            require(not CODE.fullmatch(alias) or alias == code, "profile_alias_cannot_renumber_concept")
        value = copy.deepcopy(raw)
        require(value.get("authorship_origin", "web_final_semantic_content") in {
            "web_final_semantic_content", "local_explicitly_authorized_semantic_content"
        }, "profile_authorship_origin_invalid")
        validate_subpoints(code, raw.get("subpoints", []), evidence_bindings)
        validate_teaching_plan(raw.get("teaching_plan"), evidence_bindings)
        validate_teaching_focus(raw.get("teaching_focus"), evidence_bindings)
        previous_profile = load_profile_version(repo, code, previous)
        validate_claims(code, raw, evidence_bindings, previous_profile)
        previous_subpoints = {item["subpoint_id"]: item for item in (previous_profile or {}).get("subpoints", [])}
        for item in raw.get("subpoints", []):
            validate_claims(item["subpoint_id"], item, evidence_bindings,
                            previous_subpoints.get(item["subpoint_id"]))
        dependencies = raw.get("cross_point_sources", [])
        require(isinstance(dependencies, list), "profile_cross_point_sources_array_required")
        dependency_codes = set()
        for dependency in dependencies:
            require(isinstance(dependency, dict), "profile_cross_point_source_invalid")
            other = dependency.get("concept_key")
            require(other in taxonomy and other != code and other not in dependency_codes,
                    "profile_cross_point_identity_invalid:" + code)
            dependency_codes.add(other)
            keys = dependency.get("record_keys")
            require(isinstance(keys, list) and keys and all(isinstance(key, str) and key for key in keys)
                    and len(keys) == len(set(keys)), "profile_cross_point_records_required:" + code)
            require(isinstance(dependency.get("reason"), str) and dependency["reason"].strip(),
                    "profile_cross_point_reason_required:" + code)
        # Even historical validation uses its expected immutable predecessor,
        # not the current index, so later additions do not invalidate replay.
        _validate_subpoint_continuity(repo, code, raw, previous)
        value.update(source_refs=_source_refs(raw.get("source_refs"), evidence_bindings),
                     question_refs=_question_refs(repo, raw.get("question_refs", []), formal_bindings),
                     aliases=sorted(set(aliases)))
        result.append(value)
    if expected_points is not None:
        require(seen == set(expected_points), "profile_updates_must_cover_all_affected_points")
    alias_owner = {}
    for code, label in taxonomy.items():
        alias_owner.setdefault(label.strip().casefold(), set()).add(code)
    stored = load_profiles(repo)["profiles"]
    for code, profile in stored.items():
        if code not in seen:
            for alias in profile.get("aliases", []):
                alias_owner.setdefault(alias.strip().casefold(), set()).add(code)
    for value in result:
        code = value["concept_key"]
        for alias in value["aliases"]:
            other = alias_owner.setdefault(alias.strip().casefold(), set())
            require(not other or other == {code}, "profile_alias_ambiguous:" + alias)
            other.add(code)
    return result


@contextmanager
def _lock(root):
    root.mkdir(parents=True, exist_ok=True)
    require(not any(p.is_symlink() for p in (root, *root.parents)), "profile_root_symlink")
    with (root / ".lock").open("a+b") as stream:
        fcntl.flock(stream, fcntl.LOCK_EX)
        yield


def apply_updates(repo, updates, operation_id, formal_bindings=None,
                  evidence_bindings=None, expected_points=None):
    repo = Path(repo).resolve()
    require(isinstance(operation_id, str) and operation_id, "profile_operation_identity_required")
    raw_updates = _updates(updates)
    request_sha = digest({"updates": raw_updates, "formal_bindings": formal_bindings or {},
                          "expected_points": sorted(expected_points) if expected_points is not None else None})
    op_key = hashlib.sha256(operation_id.encode()).hexdigest()
    root = repo / ROOT_REL
    receipt_path = root / "receipts" / (op_key + ".json")
    journal_path = root / "operations" / (op_key + ".json")
    with _lock(root):
        if receipt_path.exists():
            receipt = read(receipt_path)
            require(receipt.get("request_sha256") == request_sha, "profile_operation_content_conflict")
            for ref in receipt["profiles"].values():
                require(digest(read(root / ref["path"])) == ref["sha256"], "profile_committed_object_drift")
            return {**receipt, "status": "already_applied", "receipt_path": str(receipt_path)}
        index = current_index(repo)
        prior_op = index["operations"].get(op_key)
        if prior_op:
            require(prior_op["request_sha256"] == request_sha, "profile_committed_operation_conflict")
            journal = read(journal_path)
            require(journal["request_sha256"] == request_sha, "profile_operation_journal_conflict")
            profile_refs = journal["profiles"]
            generation = journal["published_generation"]
        else:
            from formal_quality_408 import require_idle_or_owner, require_owner
            require_idle_or_owner(repo)
            if operation_id.startswith("enhancement-profile:"):
                from formal_quality_408 import _OWNED
                owned = _OWNED.get() or {}
                if not owned.get("legacy"):
                    require_owner(repo)
            normalized = validate_updates(repo, raw_updates, expected_points=expected_points,
                                          evidence_bindings=evidence_bindings,
                                          formal_bindings=formal_bindings or {}, check_versions=True)
            # Cold boundary only: bind each authored profile to the actual
            # post-formal point evidence. Later unrelated points can change
            # without making this profile stale; changed same-point evidence
            # must never silently reuse old reviewed wording as current.
            from exam_tutoring_context_408 import compute_evidence_snapshot
            from knowledge_learning_records_408 import (semantic_evidence_fingerprint,
                SEMANTIC_EVIDENCE_CONTRACT, CROSS_POINT_EVIDENCE_CONTRACT)
            snapshot = compute_evidence_snapshot(repo, dt.datetime.now(ZoneInfo("Asia/Shanghai")).date().isoformat())
            point_records = snapshot["points"]
            profile_refs = {}
            timestamp = dt.datetime.now(dt.timezone.utc).isoformat()
            for value in normalized:
                code = value["concept_key"]
                previous_profile = load_profile_version(repo, code, value.get("expected_previous_version"))
                _stamp_claims(value, timestamp, previous_profile)
                previous_subpoints = {item["subpoint_id"]: item for item in (previous_profile or {}).get("subpoints", [])}
                for item in value.get("subpoints", []):
                    _stamp_claims(item, timestamp, previous_subpoints.get(item["subpoint_id"]))
                profile = {**value, "schema": SCHEMA,
                           "origin": value.get("authorship_origin", "web_final_semantic_content"),
                           "operation_id": operation_id, "accepted_at": timestamp,
                           "evidence_fingerprint": point_records.get(code, {}).get("sha256"),
                           "semantic_evidence_contract": SEMANTIC_EVIDENCE_CONTRACT,
                           "semantic_evidence_fingerprint": semantic_evidence_fingerprint(
                               point_records[code]["records"]),
                           "independent_mastery_inferred": False}
                require(profile["evidence_fingerprint"], "profile_point_has_no_materialized_evidence:" + code)
                require(point_records[code].get("record_count", 0) > 0,
                        "profile_point_has_no_materialized_evidence:" + code)
                cross_fingerprints = {}
                for dependency in value.get("cross_point_sources", []):
                    other = dependency["concept_key"]
                    require(other in point_records, "profile_cross_point_missing:" + other)
                    other_records = point_records[other]["records"]
                    require(set(dependency["record_keys"]) <= other_records["records"].keys(),
                            "profile_cross_point_record_missing:" + other)
                    cross_fingerprints[other] = semantic_evidence_fingerprint(other_records)
                # Bind both named evidence and the source point's complete state:
                # a later contradictory observation must invalidate its dependents.
                profile["cross_point_evidence_contract"] = CROSS_POINT_EVIDENCE_CONTRACT
                profile["cross_point_evidence_fingerprints"] = cross_fingerprints
                profile_refs[code] = content_write(root, profile)
            generation = index["generation"]
            journal = {"schema": "cs408-knowledge-profile-operation-v1", "operation_id": operation_id,
                       "request_sha256": request_sha, "profiles": profile_refs,
                       "previous_generation": generation}
            next_index = copy.deepcopy(index)
            next_index["profiles"].update(profile_refs)
            next_index["operations"][op_key] = {"request_sha256": request_sha,
                                              "generation_before_publication": generation}
            next_index.pop("generation", None)
            next_index["generation"] = digest(next_index)
            journal["published_generation"] = next_index["generation"]
            atomic(journal_path, journal)
            atomic(repo / CURRENT_REL, next_index)
            generation = next_index["generation"]
        receipt = {"schema": "cs408-knowledge-profile-commit-v1", "status": "applied",
                   "operation_id": operation_id, "request_sha256": request_sha,
                   "profiles": profile_refs, "profile_count": len(profile_refs),
                   "generation": generation, "formal_question_write_count": 0,
                   "fsrs_write_count": 0, "model_call_count": 0}
        atomic(receipt_path, receipt)
    return {**receipt, "receipt_path": str(receipt_path)}


def status(repo):
    value = load_profiles(repo)
    return {"status": "ready", "schema": INDEX_SCHEMA, "generation": value["generation"],
            "profile_count": len(value["profiles"]),
            "versions": {code: row["version"] for code, row in value["profiles"].items()},
            "write_count": 0}


def operation_applied(repo, operation_id):
    """Read-only recovery gate, including pointer-published/receipt-missing commits.

    The caller still invokes apply_updates with its original payload to verify
    request identity and recover the exact durable receipt.
    """
    key = hashlib.sha256(operation_id.encode()).hexdigest()
    root = Path(repo) / ROOT_REL
    receipt = root / "receipts" / (key + ".json")
    if receipt.is_file():
        value = read(receipt)
        require(value.get("operation_id") == operation_id, "profile_operation_identity_drift")
        return True
    return key in current_index(repo)["operations"]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("status", "get"))
    parser.add_argument("--repo", type=Path, default=Path(__file__).resolve().parents[1])
    parser.add_argument("--code")
    args = parser.parse_args()
    result = status(args.repo) if args.command == "status" else load_profiles(args.repo)["profiles"].get(args.code)
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
