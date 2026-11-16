"""Independent retrieval evaluation with immutable, partially judged gold.

Gold is external to this module. Held-out queries are never printed or copied to
developer reports. This evaluates retrieval, not free-form answer accuracy and
not the learner's performance. Unknown/partial candidates are not false labels.
"""
from __future__ import annotations

import argparse
import copy
import hashlib
import importlib
import inspect
import json
import re
import statistics
import sys
import time
from pathlib import Path
from typing import Any

SCHEMA = "cs408-retrieval-precision-evaluation-v1"
POINT = re.compile(r"(?:DS|CO|OS|CN)\d{2}-\d{2}$")
EXAM = re.compile(r"EXAM408-(\d{4})-Q\d{2}(?:-P\d{2})?$")
FORMAL = re.compile(r"(?:DS|CO|OS|CN)_(\d{4})_\d+$")
ABSTAIN_STATUSES = {"not_found", "no_record", "no_match", "abstain", "abstained", "insufficient_evidence", "out_of_scope", "protected", "excluded"}


def canonical(value):
    return (json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n").encode()


def digest(value):
    return hashlib.sha256(canonical(value)).hexdigest()


def load_cases(path: Path, split="development", *, allow_holdout=False):
    value = json.loads(path.read_text())
    key = "gold_sha256" if "gold_sha256" in value else "split_sha256"
    stored = value.get(key)
    content = {k: v for k, v in value.items() if k != key}
    if not stored or digest(content) != stored:
        raise ValueError("gold_integrity_failed")
    if split != "development" and not allow_holdout:
        raise ValueError("independent_holdout_access_required")
    cases = [c for c in value["cases"] if split == "all" or c["split"] == split]
    if not cases:
        raise ValueError("requested_split_empty")
    return value, cases


def _walk(value):
    if isinstance(value, dict):
        yield value
        for item in value.values():
            yield from _walk(item)
    elif isinstance(value, list):
        for item in value:
            yield from _walk(item)


def _identities(candidate):
    values = set()
    for key in ("id", "record_key", "record_id", "canonical_id"):
        if isinstance(candidate.get(key), str):
            values.add(candidate[key])
    return values


def _physical_turn(fragment):
    match = re.match(r"(turn:\d+)(?:$|[#/])", str(fragment or ""))
    return match[1] if match else str(fragment or "")


def _source_matches(candidate, source):
    # A source somewhere in a related-records tree cannot rescue a wrong
    # primary result. The candidate's own physical source is the scored unit.
    rows = [candidate]
    if not candidate.get("source_id"):
        rows += [candidate[k] for k in ("source_ref", "evidence_ref") if isinstance(candidate.get(k), dict)]
    for row in rows:
        if row.get("source_id") != source.get("source_id"):
            continue
        if row.get("source_sha256") != source.get("source_sha256"):
            continue
        fragment = row.get("source_fragment") or row.get("fragment")
        if fragment and _physical_turn(fragment) == source.get("turn_prefix", _physical_turn(source.get("source_fragment"))):
            return True
    return False


def judge_candidate(case, candidate):
    ids = _identities(candidate)
    positive = set(case.get("positive_ids", []))
    negative = set(case.get("hard_negative_ids", []))
    if case["group"] == "experience":
        if any(_source_matches(candidate, row) for row in case.get("positive_sources", [])):
            return "positive"
        if ids & positive and not candidate.get("source_id"):
            return "positive"
        if ids & negative or any(_source_matches(candidate, row) for row in case.get("hard_negative_sources", [])):
            return "hard_negative"
        if candidate.get("source_id") in {r["source_id"] for r in case.get("positive_sources", [])}:
            return "partial"
    elif ids & positive:
        return "positive"
    if ids & negative:
        return "hard_negative"
    if ids & set(case.get("partial_related_ids", [])):
        return "partial"
    parent = candidate.get("knowledge_point") or candidate.get("parent_point")
    if parent in set(case.get("partial_parent_points", [])):
        return "partial"
    return "unjudged"


def safety_check(case, result):
    candidates = result.get("candidates", [])
    request = case["request"]
    errors = []
    excluded = set(request.get("excluded_years", []))
    allowed = set(request["allowed_ids"]) if request.get("allowed_ids") is not None else None
    for candidate in candidates:
        if allowed is not None and candidate.get("id") not in allowed:
            errors.append("candidate_outside_explicit_scope")
        for row in _walk(candidate):
            for key in ("id", "source_question_id", "canonical_exam_id", "formal_id"):
                identity = str(row.get(key) or "")
                match = EXAM.fullmatch(identity) or FORMAL.fullmatch(identity)
                if match and int(match[1]) in excluded:
                    errors.append("excluded_year_candidate")
    if case["expected_outcome"] == "protected":
        if candidates:
            errors.append("protected_candidates_exposed")
        public_id = request.get("current_question_id")
        for row in _walk(result):
            for key, val in row.items():
                if key in {"overview", "summary", "quote", "text", "answer", "solution", "source_excerpt", "personal_context"} and val:
                    errors.append("protected_content_exposed")
                if isinstance(val, str) and val != public_id and (POINT.fullmatch(val) or val.startswith("KSP-")):
                    errors.append("protected_mechanism_exposed")
    if result.get("formal_write_count", 0) or result.get("learner_evidence_write_count", 0):
        errors.append("unexpected_learning_write")
    return sorted(set(errors))


def score_result(case, result, *, unsupported=False):
    candidates = result.get("candidates", [])
    if not isinstance(candidates, list):
        raise ValueError("candidate_list_invalid")
    ranked, seen = [], set()
    for candidate in candidates:
        if case["group"] == "experience" and candidate.get("source_id") and candidate.get("source_fragment"):
            key = (candidate.get("source_id"), candidate.get("source_sha256"), _physical_turn(candidate["source_fragment"]))
        else:
            key = (candidate.get("id", candidate.get("record_key")),)
        if key not in seen:
            ranked.append(candidate)
            seen.add(key)
    labels = [judge_candidate(case, c) for c in ranked[:5]]
    positive = labels.count("positive")
    negative = labels.count("hard_negative")
    partial = labels.count("partial")
    unknown = labels.count("unjudged")
    n = len(labels)
    safety = safety_check(case, result)
    outcome = case["expected_outcome"]
    control_pass = None
    if outcome == "exact":
        control_pass = len(candidates) == 1 and bool(_identities(candidates[0]) & set(case["positive_ids"]))
    elif outcome == "ambiguous":
        explicit = result.get("needs_disambiguation") is True or bool(result.get("ambiguity")) or result.get("status") in {"ambiguous", "needs_disambiguation", "needs_clarification"}
        known_targets = {c.get("knowledge_point", c.get("id")) for c in candidates} & set(case["positive_ids"])
        control_pass = (explicit or len(known_targets) >= 2) and not result.get("selected_id")
    elif outcome in {"abstain", "excluded"}:
        control_pass = not candidates and result.get("status") in ABSTAIN_STATUSES
    elif outcome == "protected":
        control_pass = not candidates and result.get("status") in {"protected", "withheld", "insufficient_evidence"}
    return {"id": case["id"], "group": case["group"], "split": case["split"],
        "supported": not unsupported, "status": "unsupported" if unsupported else result.get("status"),
        "top1": None if unsupported else bool(labels and labels[0] == "positive"),
        "top5": None if unsupported else positive > 0,
        "labels_at_5": labels, "returned_at_5": n,
        "duplicate_candidates": len(candidates)-len(ranked),
        "positive_count": positive, "hard_negative_count": negative, "partial_count": partial, "unjudged_count": unknown,
        "judged_precision_at_5": positive / (positive + negative) if positive + negative else None,
        "positive_lower_bound_at_5": positive / n if n else None,
        "potential_precision_upper_bound_at_5": (positive + partial + unknown) / n if n else None,
        "judged_coverage_at_5": (positive + negative) / n if n else None,
        "control_pass": None if unsupported else control_pass,
        "safety_errors": safety, "candidate_ids": [c.get("id", c.get("record_key")) for c in candidates],
        "candidate_references": [{k: c[k] for k in ("id", "kind", "knowledge_point", "record_key", "source_id", "source_sha256", "source_fragment") if k in c} for c in candidates],
        "query_uses_exact_date": case.get("query_uses_exact_date"), "scope_type": case.get("scope_type"),
        "absence_not_proof_of_no_learning": True}


def aggregate(rows):
    result = {}
    for group in ("mechanism", "experience", "control"):
        selected = [r for r in rows if r["group"] == group]
        supported = [r for r in selected if r["supported"]]
        positives = sum(r["positive_count"] for r in supported)
        negatives = sum(r["hard_negative_count"] for r in supported)
        partial = sum(r["partial_count"] for r in supported)
        unknown = sum(r["unjudged_count"] for r in supported)
        total = sum(r["returned_at_5"] for r in supported)
        times = sorted(r["elapsed_ms"] for r in supported if isinstance(r.get("elapsed_ms"), (float, int)))
        result[group] = {"cases": len(selected), "supported": len(supported), "unsupported": len(selected)-len(supported),
            "top1_hits": sum(r["top1"] is True for r in supported) if group != "control" else None,
            "top5_hits": sum(r["top5"] is True for r in supported) if group != "control" else None,
            "control_passed": sum(r["control_pass"] is True for r in supported) if group == "control" else None,
            "judged_precision_at_5": positives/(positives+negatives) if positives+negatives else None,
            "positive_lower_bound_at_5": positives/total if total else None,
            "potential_precision_upper_bound_at_5": (positives+partial+unknown)/total if total else None,
            "judged_coverage_at_5": (positives+negatives)/total if total else None,
            "known_wrong_candidates": negatives, "partial_related_candidates": partial, "unjudged_candidates": unknown,
            "permission_leak_cases": sum(bool(r["safety_errors"]) for r in selected),
            "p50_ms": statistics.median(times) if times else None,
            "p95_ms": times[min(len(times)-1,int(len(times)*.95))] if times else None}
    return result


def run(repo, cases, *, phase, scripts_dir=None, state=None, shadow_module=None, shadow_function="search", shadow_options=None,
        raw_results=None):
    if scripts_dir:
        sys.path.insert(0, str(scripts_dir))
    else:
        sys.path.insert(0, str(Path(__file__).resolve().parent))
    if phase == "shadow":
        module = importlib.import_module(shadow_module)
        function = getattr(module, shadow_function)
    else:
        module = importlib.import_module("memory_access_408")
        function = module.access
    if phase == "baseline" and not Path(module.__file__).resolve().is_relative_to(scripts_dir.resolve()):
        raise ValueError("baseline_not_using_frozen_code")
    rows = []
    for case in cases:
        request = copy.deepcopy(case["request"])
        view = request.pop("view", "protected")
        unsupported = phase == "baseline" and (case["group"] == "experience" or request.get("intent") == "experience")
        started = time.perf_counter()
        try:
            if phase == "shadow":
                result = function(repo=repo, query=case["query"], view=view, limit=8, **request, **(shadow_options or {}))
            else:
                result = function(repo, "discover", view=view, output_root=state, query=case["query"], limit=8, **request)
            elapsed = (time.perf_counter()-started)*1000
        except (ValueError, OSError, KeyError, RuntimeError) as exc:
            if "snapshot_stale" in str(exc):
                raise RuntimeError("evaluation_snapshot_stale_abort_not_a_quality_failure") from exc
            raise RuntimeError("evaluation_execution_error_not_a_retrieval_score:" + type(exc).__name__ + ":" + str(exc)) from exc
        row = score_result(case, result, unsupported=unsupported)
        if raw_results is not None:
            raw_results.append({"id": case["id"], "group": case["group"], "split": case["split"], "result": copy.deepcopy(result)})
        row.update(elapsed_ms=round(elapsed,3), source_version=result.get("source_version"),
                   result_sha256=digest(result), output_bytes=len(canonical(result)),
                   unsupported_reason="old_discover_has_no_record_retrieval_contract" if unsupported else None)
        rows.append(row)
    return rows


def main(argv=None):
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--phase", choices=("baseline", "shadow", "candidate"), required=True)
    p.add_argument("--repo", type=Path, default=Path(__file__).resolve().parents[1])
    p.add_argument("--cases", type=Path, required=True)
    p.add_argument("--split", choices=("development", "holdout", "all"), default="development")
    p.add_argument("--allow-holdout", action="store_true")
    p.add_argument("--scripts-dir", type=Path)
    p.add_argument("--state", type=Path)
    p.add_argument("--shadow-module")
    p.add_argument("--shadow-function", default="search")
    p.add_argument("--shadow-options-json", default="{}")
    p.add_argument("--output", type=Path, required=True)
    p.add_argument("--raw-output", type=Path)
    a=p.parse_args(argv)
    if a.phase=="baseline" and (not a.scripts_dir or not a.state):p.error("baseline needs frozen scripts and state")
    if a.phase=="shadow" and not a.shadow_module:p.error("shadow needs module name")
    gold,cases=load_cases(a.cases,a.split,allow_holdout=a.allow_holdout)
    raw_results=[] if a.raw_output else None
    rows=run(a.repo,cases,phase=a.phase,scripts_dir=a.scripts_dir,state=a.state,
        shadow_module=a.shadow_module,shadow_function=a.shadow_function,shadow_options=json.loads(a.shadow_options_json),raw_results=raw_results)
    result={"schema":SCHEMA,"phase":a.phase,"gold_sha256":gold.get("gold_sha256",gold.get("parent_gold_sha256")),
        "split":a.split,"case_count":len(rows),"rows":rows,"metrics":aggregate(rows),
        "development_metrics":aggregate([r for r in rows if r['split']=='development']),
        "holdout_metrics":aggregate([r for r in rows if r['split']=='holdout']),
        "source_versions":sorted({r['source_version'] for r in rows if r.get('source_version')}),
        "precision_is_partially_judged":True,"freeform_answer_accuracy_measured":False,
        "learning_event_write_count":0,"model_judge_calls":0}
    a.output.parent.mkdir(parents=True,exist_ok=True)
    a.output.write_bytes(canonical(result))
    if a.raw_output:
        a.raw_output.parent.mkdir(parents=True,exist_ok=True)
        a.raw_output.write_bytes(canonical({"schema":SCHEMA,"gold_sha256":result["gold_sha256"],"rows":raw_results}))
    print(json.dumps({"phase":a.phase,"case_count":len(rows),"metrics":result['metrics']},ensure_ascii=False))
    return 0


if __name__=='__main__':raise SystemExit(main())
