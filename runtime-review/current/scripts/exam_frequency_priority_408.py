"""Rank reviewed exam frequency without changing learner facts or counts."""
from __future__ import annotations

import copy

POLICY = "high_frequency_first_v1"
LEVELS = ("高频", "中频", "低频", "仅附带涉及", "频次未知", "未见考查")


def point_priority(point, summary):
    """Use the frozen personal-context syllabus overlay, never live statistics."""
    context = summary.get("syllabus_context") or {}
    frequency = context.get("exam_frequency") or {}
    level = frequency.get("main_frequency_level")
    if level not in LEVELS:
        raise ValueError("frequency_context_missing_or_unreviewed:" + point)
    for key in ("main_count", "total_count", "secondary_only_count"):
        if type(frequency.get(key)) is not int or frequency[key] < 0:
            raise ValueError("frequency_count_invalid:" + point + ":" + key)
    if frequency["total_count"] != frequency["main_count"] + frequency["secondary_only_count"]:
        raise ValueError("frequency_count_basis_conflict:" + point)
    result = {"concept_key": point, "frequency_level": level, "frequency_rank": LEVELS.index(level),
            **{key: frequency[key] for key in ("main_count", "total_count", "secondary_only_count")},
            "frequency_scope": frequency.get("frequency_scope"), "coverage": context.get("coverage"),
            "context_object": copy.deepcopy(context.get("context_object")),
            "summary_sha256": summary.get("summary_sha256"),
            "count_basis": "unique_exam_parent_questions"}
    if frequency.get("primary_count_policy"):
        result["primary_count_policy"] = frequency["primary_count_policy"]
    return result


def priority_bundle(records, source_commit):
    points = {point: point_priority(point, value.get("summary", {}))
              for point, value in records.items()}
    return {"schema": "cs408-frequency-priority-v1", "policy": POLICY,
            "source_commit": source_commit, "points": points,
            "classification_source": "same_snapshot.personal_context.syllabus_context.exam_frequency",
            "frequency_definition": "沿用本地同版频次等级；主考取选择题或独立小问的主考点，仍按父题去重；高频为主考至少5题且跨至少4年，低频为主考1至2题。",
            "fallback_rule": "当前合格高频候选用尽后才补中频、低频；仅涉及、未知、未见考查保留原标签。"}


def rank(row):
    # Old registered inputs keep their existing ordering and lack this field.
    return (row.get("frequency_priority") or {}).get("frequency_rank", 0)

