#!/usr/bin/env python3
"""Answer-safe failure signatures and conservative commonality matching.

This module deliberately prefers an explicit evidence gap to a broad semantic
guess.  It compiles a bounded recent layer from canonical outcomes, projects
formal nightly corpus entries into the same shape, and labels every match with
one of the contract match bases.  Module/subject/weak matches are background
only and can never drive a personalized claim or practice route.
"""

from __future__ import annotations

import hashlib
import json
import re
import unicodedata
from collections import Counter, defaultdict
from datetime import date, datetime
from typing import Any, Iterable
from zoneinfo import ZoneInfo

from intake_lib_408 import scan_leak


SIGNATURE_SCHEMA = "failure_signature_index_v1"
SIGNATURE_ITEM_SCHEMA = "failure_signature_v1"
RESPONSE_PLAN_SCHEMA = "personalization_response_plan_v1"
FAILURE_RESULTS = {"partial", "wrong", "uncertain"}
FAILURE_REASONING_RESULTS = {"partial", "diverged"}
FAILURE_CHOICE_RESULTS = {"incorrect", "partial", "uncertain"}
STRONG_MATCH_BASES = {
    "exact_question",
    "exact_point",
    "same_mechanism",
    "same_error_pattern",
    "same_method",
    "verified_cross_subject",
}
BACKGROUND_MATCH_BASES = {
    "module_fallback",
    "subject_fallback",
    "weak_semantic_candidate",
}
MATCH_BASIS_RANK = {
    "exact_question": 8,
    "exact_point": 7,
    "same_mechanism": 6,
    "verified_cross_subject": 5,
    "same_error_pattern": 4,
    "same_method": 3,
    "module_fallback": 2,
    "subject_fallback": 1,
    "weak_semantic_candidate": 0,
}
MAX_RECENT_DAYS = 30
MAX_SIGNATURES_PER_KEY = 8
MAX_TOTAL_SIGNATURES = 512
SAFE_ID_RE = re.compile(r"^[A-Za-z0-9_.:@/-]{1,180}$")
POINT_RE = re.compile(r"^(DS|CO|OS|CN)\d{2}-\d{2}$")
NODE_RE = re.compile(r"^(DS|CO|OS|CN)_(?:\d{4}|UNK)_\d{3}$")
NONE_BREAK_VALUES = {
    "",
    "none",
    "无",
    "未观察到",
    "未记录",
    "未可靠观察到",
    "not_observed",
}
FIRST_BREAK_PROVENANCES = {
    "user_report",
    "visible_evidence",
    "system_observed",
    "derived_classification",
    "not_observed",
    "legacy_unverified",
}


def _observed_at(value: Any, observed_date: str) -> str | None:
    raw = str(value or "").strip()
    if not raw:
        return None
    try:
        moment = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return None
    if moment.tzinfo is None:
        return None
    local = moment.astimezone(ZoneInfo("Asia/Shanghai"))
    if local.date().isoformat() != observed_date:
        return None
    return local.isoformat(timespec="seconds")


def _signature_recency_key(row: dict[str, Any]) -> tuple[str, str, str]:
    evidence = row.get("evidence") or {}
    return (
        str(evidence.get("observed_date") or ""),
        str(evidence.get("observed_at") or ""),
        str(evidence.get("event_id") or ""),
    )


# These are mechanism primitives, not keyword similarity.  Each rule groups
# terms that name the same object/lifecycle boundary.  A shared surface word
# such as "缓存" or "队列" has no rule and therefore cannot become a strong
# commonality by itself.
FACET_RULES: dict[str, tuple[tuple[str, tuple[str, ...]], ...]] = {
    "mechanism_primitives": (
        (
            "file_open_reference_lifecycle",
            (
                "打开引用计数",
                "打开文件引用数",
                "系统打开文件表引用数",
                "active inode open count",
                "open reference count",
                "硬链接计数",
                "磁盘 inode",
                "内存 inode",
                "文件打开引用链",
            ),
        ),
        (
            "interrupt_hardware_software_handoff",
            (
                "中断响应",
                "中断服务",
                "中断处理程序",
                "中断请求",
                "硬件阶段",
                "软件阶段",
                "设备管理",
                "interrupt response",
                "interrupt handler",
            ),
        ),
        (
            "window_cycle_throughput",
            (
                "窗口内可发送数据量",
                "确认返回周期",
                "平均吞吐率",
                "窗口吞吐率",
                "window throughput",
            ),
        ),
        (
            "identity_field_separation",
            (
                "结点自身地址",
                "next 域",
                "后继地址",
                "逻辑次序",
                "物理地址",
            ),
        ),
        (
            "encoded_field_boundary",
            (
                "阶码字段",
                "真实指数",
                "保留编码",
                "隐藏位",
                "尾数字段",
            ),
        ),
        (
            "cache_page_residency_chain",
            (
                "cache命中",
                "cache 命中",
                "页面驻留",
                "页命中",
                "cache miss",
            ),
        ),
    ),
    "objects": (
        ("process_open_file_entry", ("进程打开文件表", "文件描述符表")),
        ("system_open_file_entry", ("系统打开文件表", "打开文件表项")),
        ("active_memory_inode", ("内存 inode", "活动 inode", "active inode")),
        ("disk_inode", ("磁盘 inode", "disk inode")),
        ("hard_link_count", ("硬链接计数", "链接计数", "link count")),
        ("open_reference_count", ("打开引用计数", "打开文件引用数", "open count")),
        ("interrupt_request", ("中断请求", "interrupt request")),
        ("interrupt_response", ("中断响应", "interrupt response")),
        ("interrupt_handler", ("中断服务", "中断处理程序", "interrupt handler")),
        ("node_address", ("结点自身地址", "物理地址")),
        ("link_field", ("next 域", "链接字段", "后继地址")),
        ("encoded_exponent", ("阶码字段", "偏置阶码")),
        ("true_exponent", ("真实指数",)),
    ),
    "preconditions": (
        ("reference_reaches_zero", ("引用归零", "计数归零", "减为 0")),
        ("special_encoding_excluded", ("排除阶码全 0", "排除阶码全 1", "特殊值边界")),
        ("window_filled", ("发送窗口", "窗口内")),
    ),
    "state_transitions": (
        ("open_close_changes_memory_reference", ("open", "close", "打开引用", "关闭文件")),
        ("link_unlink_changes_disk_link_count", ("link", "unlink", "硬链接计数")),
        ("request_response_service_sequence", ("中断请求", "中断响应", "中断服务")),
        ("logical_link_rewire", ("插入", "next 域", "链接字段更新")),
    ),
    "causal_directions": (
        ("memory_reference_before_disk_reclaim", ("内存", "磁盘", "回收")),
        ("hardware_response_before_software_service", ("中断响应", "中断服务")),
        ("capacity_per_cycle_before_physical_cap", ("窗口", "周期", "链路速率")),
    ),
    "boundaries_and_units": (
        ("memory_object_vs_disk_object", ("内存 inode", "磁盘 inode")),
        ("open_reference_vs_hard_link", ("打开引用", "硬链接计数")),
        ("hardware_phase_vs_software_phase", ("硬件阶段", "软件阶段")),
        ("byte_vs_bit", ("byte", "bit", "字节", "比特")),
        ("encoded_field_vs_semantic_value", ("阶码字段", "真实指数")),
        ("object_address_vs_link_value", ("结点自身地址", "next 域")),
    ),
    "error_patterns": (
        ("boundary_conflation", ("混淆", "混成", "边界", "误套")),
        ("causal_direction_reversed", ("写反", "反向", "因果方向")),
        ("unit_conversion_omitted", ("单位换算", "byte", "bit", "字节", "比特")),
        ("state_transition_omitted", ("未将", "未落实", "没有落实", "状态变化")),
        ("object_identity_conflated", ("自身地址", "next 域", "对象混淆")),
        ("field_role_conflated", ("字段", "隐藏位", "真实指数", "保留编码")),
        ("first_action_missing", ("第一步", "第一动作", "不会开始", "无从下手")),
    ),
    "method_templates": (
        ("separate_objects_then_trace_counts", ("对象", "计数", "引用链", "硬链接")),
        ("partition_phases_then_assign_actor", ("划分", "阶段", "执行主体")),
        ("normalize_units_then_compute", ("统一", "byte", "bit")),
        ("draw_timeline_then_take_bottleneck", ("时间轴", "周期", "共同取限")),
        ("restore_logical_order_then_rewire", ("恢复逻辑次序", "链接字段")),
        ("split_encoded_fields_then_decode", ("画出 s/e/f", "阶码字段", "真实指数")),
    ),
}

FACET_LABELS = {
    "file_open_reference_lifecycle": "文件打开引用链、硬链接链与回收条件",
    "interrupt_hardware_software_handoff": "中断响应的硬件阶段与中断处理的软件阶段",
    "open_reference_vs_hard_link": "内存打开引用链与磁盘硬链接链的对象边界",
    "memory_object_vs_disk_object": "内存活动对象与磁盘持久对象的边界",
    "hardware_phase_vs_software_phase": "硬件响应阶段与软件处理阶段的职责边界",
    "byte_vs_bit": "Byte 与 bit 的单位边界",
    "encoded_field_vs_semantic_value": "编码字段与其语义值的边界",
    "object_address_vs_link_value": "对象自身地址与链接字段值的边界",
}


def canonical_json_bytes(value: Any) -> bytes:
    return (
        json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        + "\n"
    ).encode("utf-8")


def sha256_value(value: Any) -> str:
    return hashlib.sha256(canonical_json_bytes(value)).hexdigest()


def _normal_text(value: Any) -> str:
    return " ".join(unicodedata.normalize("NFKC", str(value or "")).lower().split())


def _safe_text(value: Any, *, limit: int = 240) -> str | None:
    text = " ".join(str(value or "").split())[:limit]
    if not text:
        return None
    fails, warns = scan_leak(text)
    return None if fails or warns else text


def _safe_fact_text(value: Any, *, limit: int = 240) -> str | None:
    if isinstance(value, dict):
        value = value.get("value")
    return _safe_text(value, limit=limit)


def _safe_id(value: Any) -> str | None:
    text = str(value or "").strip()
    return text if SAFE_ID_RE.fullmatch(text) else None


def _date(value: Any) -> str | None:
    try:
        return date.fromisoformat(str(value or "")).isoformat()
    except ValueError:
        return None


def _first_break_provenance(value: Any, first_break: Any) -> str:
    """Match canonical legacy normalization without upgrading absent evidence."""

    raw = str(value or "").strip()
    if raw in FIRST_BREAK_PROVENANCES:
        return raw
    if raw:
        return "not_observed"
    return (
        "not_observed"
        if str(first_break or "").strip().lower() in NONE_BREAK_VALUES
        else "derived_classification"
    )


def _as_texts(values: Iterable[Any]) -> list[str]:
    result: list[str] = []
    for value in values:
        if isinstance(value, dict):
            for key in ("value", "label", "status"):
                if value.get(key):
                    result.append(str(value[key]))
        elif isinstance(value, list):
            result.extend(str(item) for item in value)
        elif value:
            result.append(str(value))
    return result


def extract_facets(values: Iterable[Any]) -> dict[str, list[str]]:
    """Extract controlled facets; no free-text similarity becomes strong evidence."""

    normalized = _normal_text(" ; ".join(_as_texts(values)))
    facets: dict[str, list[str]] = {}
    for facet, rules in FACET_RULES.items():
        found: list[str] = []
        for facet_id, terms in rules:
            term_hits = [term for term in terms if _normal_text(term) in normalized]
            # Single distinctive terms are enough for objects.  Relational
            # facets require either two terms or an explicitly identifying phrase.
            if facet in {"objects", "error_patterns"}:
                matched = bool(term_hits)
            else:
                matched = len(term_hits) >= min(2, len(terms)) or any(
                    len(_normal_text(term)) >= 8 for term in term_hits
                )
            if matched:
                found.append(facet_id)
        facets[facet] = sorted(set(found))
    return facets


def event_is_failure(event: dict[str, Any]) -> bool:
    return bool(
        str(event.get("first_result") or "") in FAILURE_RESULTS
        or str(event.get("reasoning_result") or "") in FAILURE_REASONING_RESULTS
        or str(event.get("choice_result") or "") in FAILURE_CHOICE_RESULTS
    )


def _signature_id(material: dict[str, Any]) -> str:
    return "FS-" + sha256_value(material)[:24].upper()


def signature_from_event(
    event: dict[str, Any], mapping: dict[str, Any]
) -> dict[str, Any] | None:
    """Project one canonical outcome into a minimal answer-safe signature."""

    if str(event.get("event_kind") or "") != "outcome" or not event_is_failure(event):
        return None
    event_id = _safe_id(event.get("event_id"))
    observed_date = _date(mapping.get("observed_date") or event.get("observed_date"))
    if event_id is None or observed_date is None:
        return None
    observed_at = _observed_at(event.get("event_time"), observed_date)
    points = sorted(
        {
            str(value)
            for value in mapping.get("point_ids", [])
            if POINT_RE.fullmatch(str(value))
        }
    )
    mechanisms = sorted(
        {
            str(value)
            for value in mapping.get("mechanism_ids", [])
            if _safe_id(value)
        }
    )
    question_ids = sorted(
        {
            str(value)
            for value in (
                mapping.get("question_node_ids", [])
                or mapping.get("formal_node_ids", [])
            )
            if NODE_RE.fullmatch(str(value))
        }
    )
    raw_first_break = _safe_text(event.get("first_break"))
    provenance = _first_break_provenance(
        event.get("first_break_provenance"), raw_first_break
    )
    # An explicit not_observed marker is stronger than an accompanying label.
    # Do not let such text drive a learner claim or controlled facet match.
    first_break = None if provenance == "not_observed" else raw_first_break
    first_action = _safe_text(event.get("first_action"))
    source_id = _safe_id(event.get("source_id"))
    evidence_text = [
        first_break,
        first_action,
        event.get("mechanism_key"),
        event.get("knowledge_point"),
    ]
    facets = extract_facets(evidence_text)
    evidence_gaps: list[str] = []
    if not points:
        evidence_gaps.append("point_unresolved")
    if not mechanisms:
        evidence_gaps.append("mechanism_unresolved")
    if not first_break:
        evidence_gaps.append("first_break_not_observed")
    material = {
        "layer": "recent_incremental",
        "event_id": event_id,
        "observed_date": observed_date,
        "observed_at": observed_at,
        "point_ids": points,
        "mechanism_ids": mechanisms,
        "question_ids": question_ids,
        "first_result": str(event.get("first_result") or "not_observed"),
        "choice_result": str(event.get("choice_result") or "not_applicable"),
        "reasoning_result": str(event.get("reasoning_result") or "not_observed"),
        "confidence": str(event.get("confidence") or "not_observed"),
        "first_break": first_break,
        "first_break_provenance": provenance,
        "facets": facets,
    }
    signature = {
        "schema": SIGNATURE_ITEM_SCHEMA,
        "signature_id": _signature_id(material),
        "layer": "recent_incremental",
        "subject": points[0][:2] if points else (
            question_ids[0][:2] if question_ids else None
        ),
        "point_ids": points,
        "mechanism_ids": mechanisms,
        "question_ids": question_ids,
        **facets,
        "first_action": {
            "value": first_action,
            "provenance": "event_field" if first_action else "not_observed",
        },
        "first_break": {"value": first_break, "provenance": provenance},
        "question_type": _safe_text(event.get("presentation_kind"), limit=80),
        "presentation_kind": _safe_text(event.get("presentation_kind"), limit=80),
        "conclusion_result": str(event.get("choice_result") or "not_applicable"),
        "reasoning_result": str(event.get("reasoning_result") or "not_observed"),
        "first_result": str(event.get("first_result") or "not_observed"),
        "confidence": str(event.get("confidence") or "not_observed"),
        "evidence": {
            "event_id": event_id,
            "question_id": question_ids[0] if len(question_ids) == 1 else None,
            "observed_date": observed_date,
            "observed_at": observed_at,
            "source_id": source_id,
            "provenance": "canonical_outcome",
            "content_sha256": sha256_value(material),
        },
        "evidence_gaps": evidence_gaps,
        "answer_safe": True,
    }
    return signature


def signature_from_corpus(entry: dict[str, Any]) -> dict[str, Any] | None:
    """Project one already validated nightly corpus entry into a formal signature."""

    formal_id = str(entry.get("formal_id") or "")
    study_date = _date(entry.get("study_date"))
    if not NODE_RE.fullmatch(formal_id) or study_date is None:
        return None
    observed = entry.get("observed_facts") or {}
    snapshot = entry.get("formal_snapshot") or {}
    learning = entry.get("learning_evidence") or {}
    event_id = _safe_id(learning.get("event_id"))
    values = [
        observed.get("user_error_entry"),
        observed.get("learner_error_entry"),
        observed.get("error_entry"),
        observed.get("first_action"),
        snapshot.get("actual_mechanism"),
        snapshot.get("core_point"),
        snapshot.get("redo_first_action"),
        snapshot.get("key_parameters"),
        snapshot.get("fuzzy_concepts"),
        snapshot.get("error_tags"),
    ]
    facets = extract_facets(values)
    first_action = _safe_fact_text(
        observed.get("first_action") or snapshot.get("redo_first_action")
    )
    first_break_source = (
        observed.get("user_error_entry")
        or observed.get("learner_error_entry")
        or observed.get("error_entry")
    )
    first_break = _safe_fact_text(first_break_source)
    observed_provenance = (
        str(first_break_source.get("provenance") or "").strip()
        if isinstance(first_break_source, dict)
        else ""
    )
    points = sorted(
        {
            str(value)
            for value in entry.get("point_ids", [])
            if POINT_RE.fullmatch(str(value))
        }
    )
    mechanisms = sorted(
        {
            *[f"KP:{point_id}" for point_id in points],
            *[
                value
                for value in facets["mechanism_primitives"]
                if _safe_id(value)
            ],
        }
    )
    material = {
        "layer": "formal_history",
        "formal_id": formal_id,
        "study_date": study_date,
        "object_sha256": entry.get("object_sha256"),
        "event_id": event_id,
        "point_ids": points,
        "facets": facets,
    }
    return {
        "schema": SIGNATURE_ITEM_SCHEMA,
        "signature_id": _signature_id(material),
        "layer": "formal_history",
        "subject": formal_id[:2],
        "point_ids": points,
        "mechanism_ids": mechanisms,
        "question_ids": [formal_id],
        **facets,
        "first_action": {
            "value": first_action,
            "provenance": "observed_or_formal_safe_snapshot"
            if first_action
            else "not_observed",
        },
        "first_break": {
            "value": first_break,
            "provenance": observed_provenance
            or ("observed_fact" if first_break else "not_observed"),
        },
        "question_type": _safe_text(snapshot.get("question_type"), limit=100),
        "presentation_kind": None,
        "conclusion_result": str(entry.get("choice_result") or "not_applicable"),
        "reasoning_result": str(entry.get("reasoning_result") or "not_observed"),
        "first_result": str(entry.get("learning_outcome") or "not_observed"),
        "confidence": str(learning.get("confidence") or "not_observed"),
        "evidence": {
            "event_id": event_id,
            "question_id": formal_id,
            "observed_date": study_date,
            "source_id": None,
            "provenance": "nightly_formal_corpus",
            "content_sha256": str(entry.get("object_sha256") or sha256_value(material)),
        },
        "evidence_gaps": [] if event_id else ["canonical_event_not_bound"],
        "answer_safe": True,
    }


def build_recent_signature_index(
    events: list[dict[str, Any]], mappings: dict[str, Any], as_of: str
) -> dict[str, Any]:
    """Build a bounded, recent-only inverted index from canonical outcomes."""

    current = date.fromisoformat(as_of)
    signatures: list[dict[str, Any]] = []
    for event in events:
        event_id = str(event.get("event_id") or "")
        mapping = (mappings.get("events") or {}).get(event_id)
        if not isinstance(mapping, dict):
            continue
        observed = _date(mapping.get("observed_date"))
        if observed is None:
            continue
        age = (current - date.fromisoformat(observed)).days
        if age < 0 or age >= MAX_RECENT_DAYS:
            continue
        signature = signature_from_event(event, mapping)
        if signature:
            signatures.append(signature)
    signatures.sort(key=_signature_recency_key, reverse=True)
    signatures = signatures[:MAX_TOTAL_SIGNATURES]
    return _index_signatures(signatures, as_of=as_of)


def _index_signatures(
    signatures: list[dict[str, Any]], *, as_of: str
) -> dict[str, Any]:
    by_key: dict[str, list[str]] = defaultdict(list)
    for row in signatures:
        signature_id = row["signature_id"]
        for point_id in row.get("point_ids", []):
            by_key[f"point:{point_id}"].append(signature_id)
            by_key[f"module:{point_id[:4]}"].append(signature_id)
            by_key[f"subject:{point_id[:2]}"].append(signature_id)
        for mechanism_id in row.get("mechanism_ids", []):
            by_key[f"mechanism:{mechanism_id}"].append(signature_id)
        for mechanism_id in row.get("mechanism_primitives", []):
            by_key[f"mechanism:{mechanism_id}"].append(signature_id)
        for question_id in row.get("question_ids", []):
            by_key[f"question:{question_id}"].append(signature_id)
        for error_pattern in row.get("error_patterns", []):
            by_key[f"error:{error_pattern}"].append(signature_id)
        for method in row.get("method_templates", []):
            by_key[f"method:{method}"].append(signature_id)
    compact = {
        key: values[:MAX_SIGNATURES_PER_KEY]
        for key, values in sorted(by_key.items())
    }
    return {
        "schema": SIGNATURE_SCHEMA,
        "kind": "recent_failure_signature_index",
        "as_of": as_of,
        "window_days": MAX_RECENT_DAYS,
        "signature_count": len(signatures),
        "signatures": {row["signature_id"]: row for row in signatures},
        "by_key": compact,
        "answer_safe": True,
        "formal_write_count": 0,
    }


def apply_event_to_signature_index(
    index: dict[str, Any], event: dict[str, Any], mapping: dict[str, Any], *, as_of: str
) -> dict[str, Any]:
    """Idempotently add one current failure without scanning the ledger."""

    existing = list((index.get("signatures") or {}).values())
    signature = signature_from_event(event, mapping)
    if signature is None:
        return _index_signatures(existing, as_of=as_of)
    evidence_id = (signature.get("evidence") or {}).get("event_id")
    existing = [
        row
        for row in existing
        if (row.get("evidence") or {}).get("event_id") != evidence_id
    ]
    existing.append(signature)
    existing.sort(key=_signature_recency_key, reverse=True)
    return _index_signatures(existing[:MAX_TOTAL_SIGNATURES], as_of=as_of)


def _overlap(left: Iterable[str], right: Iterable[str]) -> list[str]:
    return sorted(set(left) & set(right))


def _match_signature(
    signature: dict[str, Any], targets: dict[str, Any]
) -> dict[str, Any] | None:
    shared_questions = _overlap(signature.get("question_ids", []), targets.get("formal_node_ids", []))
    shared_points = _overlap(signature.get("point_ids", []), targets.get("point_ids", []))
    target_mechanisms = list(targets.get("mechanism_ids", [])) + list(
        targets.get("mechanism_primitive_ids", [])
    )
    signature_mechanisms = list(signature.get("mechanism_ids", [])) + list(
        signature.get("mechanism_primitives", [])
    )
    shared_mechanisms = _overlap(signature_mechanisms, target_mechanisms)
    shared_errors = _overlap(signature.get("error_patterns", []), targets.get("error_pattern_ids", []))
    shared_methods = _overlap(signature.get("method_templates", []), targets.get("method_ids", []))
    modules = set(targets.get("module_ids", [])) | {
        point[:4] for point in targets.get("point_ids", [])
    }
    subjects = set(targets.get("subjects", []))
    if shared_questions:
        basis, strength = "exact_question", "strong"
        reason = "当前正式题目与该学习证据身份一致"
    elif shared_points:
        basis, strength = "exact_point", "strong"
        reason = "当前目标与该学习证据命中同一正式考点"
    elif shared_mechanisms:
        basis, strength = "same_mechanism", "strong"
        reason = "当前目标与该学习证据共享同一受控机制"
    elif shared_errors:
        basis, strength = "same_error_pattern", "medium"
        reason = "当前已识别错误入口与历史证据共享受控错误模式"
    elif shared_methods:
        basis, strength = "same_method", "medium"
        reason = "当前目标与历史证据共享解题方法模板"
    elif modules and any(point[:4] in modules for point in signature.get("point_ids", [])):
        basis, strength = "module_fallback", "weak"
        reason = "仅同模块，无直接考点、机制或错误模式证据"
    elif subjects and signature.get("subject") in subjects:
        basis, strength = "subject_fallback", "weak"
        reason = "仅同科，无直接考点、机制或错误模式证据"
    else:
        return None
    evidence = signature.get("evidence") or {}
    return {
        "signature_id": signature.get("signature_id"),
        "evidence_event_id": evidence.get("event_id"),
        "question_id": evidence.get("question_id"),
        "observed_date": evidence.get("observed_date"),
        "observed_at": evidence.get("observed_at"),
        "layer": signature.get("layer"),
        "match_basis": basis,
        "shared_point_ids": shared_points,
        "shared_mechanisms": shared_mechanisms,
        "shared_error_patterns": shared_errors,
        "shared_methods": shared_methods,
        "shared_boundaries": list(signature.get("boundaries_and_units", [])),
        "shared_objects": list(signature.get("objects", [])),
        "relation_reason": reason,
        "evidence_strength": strength,
        "counterevidence": list(signature.get("evidence_gaps", [])),
        "usable_for_personal_claim": basis in STRONG_MATCH_BASES,
        "usable_for_practice_routing": basis in {
            "exact_question",
            "exact_point",
            "same_mechanism",
            "verified_cross_subject",
        },
        "signature": signature,
    }


def match_recent_signatures(
    index: dict[str, Any], targets: dict[str, Any], *, limit: int = 6
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Return direct and background matches without scanning unrelated signatures."""

    by_key = index.get("by_key") or {}
    ids: list[str] = []
    for question_id in targets.get("formal_node_ids", []):
        ids.extend(by_key.get(f"question:{question_id}", []))
    for point_id in targets.get("point_ids", []):
        ids.extend(by_key.get(f"point:{point_id}", []))
    for mechanism_id in targets.get("mechanism_ids", []):
        ids.extend(by_key.get(f"mechanism:{mechanism_id}", []))
    for mechanism_id in targets.get("mechanism_primitive_ids", []):
        ids.extend(by_key.get(f"mechanism:{mechanism_id}", []))
    for error_id in targets.get("error_pattern_ids", []):
        ids.extend(by_key.get(f"error:{error_id}", []))
    for method_id in targets.get("method_ids", []):
        ids.extend(by_key.get(f"method:{method_id}", []))
    direct_requested = bool(ids)
    if not ids:
        for module_id in set(targets.get("module_ids", [])) | {
            value[:4] for value in targets.get("point_ids", [])
        }:
            ids.extend(by_key.get(f"module:{module_id}", []))
        for subject in targets.get("subjects", []):
            ids.extend(by_key.get(f"subject:{subject}", []))
    signatures = index.get("signatures") or {}
    seen: set[str] = set()
    matches: list[dict[str, Any]] = []
    for signature_id in ids:
        if signature_id in seen:
            continue
        seen.add(signature_id)
        signature = signatures.get(signature_id)
        if not isinstance(signature, dict):
            continue
        match = _match_signature(signature, targets)
        if match:
            matches.append(match)
    strength_rank = {"strong": 2, "medium": 1, "weak": 0}
    matches.sort(
        key=lambda row: (
            strength_rank.get(str(row.get("evidence_strength")), -1),
            MATCH_BASIS_RANK.get(str(row.get("match_basis")), -1),
            str(row.get("observed_date") or ""),
            str(row.get("observed_at") or ""),
            str(row.get("signature_id") or ""),
        ),
        reverse=True,
    )
    direct = [row for row in matches if row["match_basis"] in STRONG_MATCH_BASES]
    background = [row for row in matches if row["match_basis"] in BACKGROUND_MATCH_BASES]
    # If direct keys were requested but none survived, do not widen implicitly.
    if direct_requested and not direct:
        background = []
    return direct[:limit], background[:3]


def match_historical_signatures(
    entries: list[dict[str, Any]], targets: dict[str, Any], *, limit: int = 4
) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    for entry in entries:
        signature = signature_from_corpus(entry)
        if signature is None:
            continue
        match = _match_signature(signature, targets)
        if match and match["match_basis"] in STRONG_MATCH_BASES:
            result.append(match)
    result.sort(
        key=lambda row: (
            MATCH_BASIS_RANK.get(str(row.get("match_basis")), -1),
            str(row.get("observed_date") or ""),
            str(row.get("signature_id") or ""),
        ),
        reverse=True,
    )
    return result[:limit]


def match_verified_cross_recent_signatures(
    index: dict[str, Any],
    cross_rows: list[dict[str, Any]],
    *,
    limit: int = 4,
) -> list[dict[str, Any]]:
    """Bind other-subject learner evidence through verified graph edges only.

    A point name or shared word never reaches this route.  Only bounded
    ``verified_existing`` rows can produce ``verified_cross_subject`` evidence.
    Practice eligibility additionally requires the independent live gate.
    """

    by_key = index.get("by_key") if isinstance(index.get("by_key"), dict) else {}
    signatures = (
        index.get("signatures")
        if isinstance(index.get("signatures"), dict)
        else {}
    )
    result: list[dict[str, Any]] = []
    seen: set[tuple[str, str]] = set()
    for edge in cross_rows:
        if not isinstance(edge, dict) or edge.get("lifecycle") != "verified_existing":
            continue
        edge_id = str(edge.get("edge_id") or "")
        other_point = str(
            edge.get("target_point_id") or edge.get("other_point_id") or ""
        )
        if not edge_id or not POINT_RE.fullmatch(other_point):
            continue
        for signature_id in by_key.get(f"point:{other_point}", [])[:MAX_SIGNATURES_PER_KEY]:
            key = (edge_id, str(signature_id))
            if key in seen:
                continue
            seen.add(key)
            signature = signatures.get(signature_id)
            if not isinstance(signature, dict) or other_point not in signature.get(
                "point_ids", []
            ):
                continue
            evidence = signature.get("evidence") or {}
            result.append(
                {
                    "signature_id": signature.get("signature_id"),
                    "evidence_event_id": evidence.get("event_id"),
                    "question_id": evidence.get("question_id"),
                    "observed_date": evidence.get("observed_date"),
                    "layer": signature.get("layer"),
                    "match_basis": "verified_cross_subject",
                    "cross_edge_id": edge_id,
                    "source_point_id": edge.get("source_point_id"),
                    "other_point_id": other_point,
                    "shared_point_ids": [],
                    "shared_mechanisms": list(
                        signature.get("mechanism_primitives", [])
                    ),
                    "shared_error_patterns": list(
                        signature.get("error_patterns", [])
                    ),
                    "shared_methods": list(signature.get("method_templates", [])),
                    "shared_boundaries": list(
                        signature.get("boundaries_and_units", [])
                    ),
                    "shared_objects": list(signature.get("objects", [])),
                    "relation_reason": (
                        "该证据位于已验证跨科关系的另一端；可用于联系，"
                        "但仍须分别核对两科对象、阶段和因果边界"
                    ),
                    "evidence_strength": (
                        "strong" if edge.get("confidence") == "high" else "medium"
                    ),
                    "counterevidence": list(signature.get("evidence_gaps", [])),
                    "usable_for_personal_claim": True,
                    "usable_for_practice_routing": bool(edge.get("live_routable")),
                    "signature": signature,
                }
            )
    result.sort(
        key=lambda row: (
            row.get("evidence_strength") == "strong",
            str(row.get("observed_date") or ""),
            str(row.get("cross_edge_id") or ""),
            str(row.get("signature_id") or ""),
        ),
        reverse=True,
    )
    return result[:limit]


def common_patterns(matches: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Require at least two distinct failure outcomes before claiming repetition."""

    buckets: dict[tuple[str, str], list[dict[str, Any]]] = defaultdict(list)
    for match in matches:
        signature = match.get("signature") or {}
        for value in signature.get("mechanism_primitives", []):
            buckets[("mechanism", value)].append(match)
        for value in signature.get("error_patterns", []):
            buckets[("error_pattern", value)].append(match)
        for value in signature.get("boundaries_and_units", []):
            buckets[("boundary", value)].append(match)
    rows: list[dict[str, Any]] = []
    for (kind, value), evidence in buckets.items():
        distinct_events = {
            str(row.get("evidence_event_id"))
            for row in evidence
            if row.get("evidence_event_id")
        }
        # A formal item without a canonical event remains useful historical
        # context but cannot help prove a repeated learner error.
        if len(distinct_events) < 2:
            continue
        questions = sorted(
            {str(row.get("question_id")) for row in evidence if row.get("question_id")}
        )
        dates = sorted(
            {str(row.get("observed_date")) for row in evidence if row.get("observed_date")}
        )
        counter = sorted(
            {
                str(item)
                for row in evidence
                for item in row.get("counterevidence", [])
                if item
            }
        )
        material = {
            "kind": kind,
            "value": value,
            "events": sorted(distinct_events),
            "questions": questions,
            "dates": dates,
        }
        rows.append(
            {
                "pattern_id": "CP-" + sha256_value(material)[:20].upper(),
                "status": "repeated",
                "pattern_kind": kind,
                "pattern_value": value,
                "pattern_label": FACET_LABELS.get(value, value),
                "distinct_outcome_event_count": len(distinct_events),
                "evidence_event_ids": sorted(distinct_events),
                "question_ids": questions,
                "observed_dates": dates,
                "relation_reason": "至少两个不同 canonical outcome 共享同一受控机制、错误模式或边界",
                "evidence_strength": "strong" if kind == "mechanism" else "medium",
                "counterevidence": counter,
                "practice_eligible": kind == "mechanism",
            }
        )
    rows.sort(
        key=lambda row: (
            int(row["distinct_outcome_event_count"]),
            str(row["pattern_id"]),
        ),
        reverse=True,
    )
    return rows[:4]


def _compact_match(match: dict[str, Any]) -> dict[str, Any]:
    compact = {key: value for key, value in match.items() if key != "signature"}
    signature = match.get("signature") or {}
    evidence = signature.get("evidence") or {}
    compact["failure_signature"] = {
        key: signature.get(key)
        for key in (
            "signature_id",
            "layer",
            "subject",
            "point_ids",
            "first_action",
            "first_break",
            "conclusion_result",
            "reasoning_result",
            "first_result",
            "confidence",
            "evidence_gaps",
        )
        if key in signature
    }
    compact["failure_signature"]["evidence"] = {
        key: evidence.get(key)
        for key in (
            "event_id",
            "question_id",
            "observed_date",
            "observed_at",
            "provenance",
            "content_sha256",
        )
        if evidence.get(key) is not None
    }
    return compact


def build_response_plan(
    *,
    targets: dict[str, Any],
    recent_direct: list[dict[str, Any]],
    recent_background: list[dict[str, Any]],
    historical_direct: list[dict[str, Any]],
    cross_rows: list[dict[str, Any]],
    cross_evidence_matches: list[dict[str, Any]] | None = None,
    question_candidates: list[dict[str, Any]],
    corpus_status: str,
) -> dict[str, Any]:
    cross_evidence_matches = list(cross_evidence_matches or [])
    all_direct = recent_direct + historical_direct
    patterns = common_patterns(
        all_direct + (cross_evidence_matches if all_direct else [])
    )
    if patterns:
        strategy = "repair_verified_repeated_pattern"
        depth = "minimum_framework_then_first_boundary"
    elif recent_direct:
        strategy = "target_direct_recent_error"
        depth = "targeted_explanation_then_boundary_check"
    elif historical_direct:
        strategy = "connect_direct_formal_history"
        depth = "targeted_explanation_with_historical_transfer"
    else:
        strategy = "neutral_targeted_answer"
        depth = "minimum_sufficient_framework"
    boundaries = [
        boundary
        for row in all_direct
        for boundary in (row.get("shared_boundaries") or [])
    ]
    boundary_counts = Counter(boundaries)
    first_boundary = (
        boundary_counts.most_common(1)[0][0] if boundary_counts else None
    )
    direct_candidates = [
        row
        for row in question_candidates
        if row.get("origin") in {"current_point", "current_mechanism"}
        and row.get("cooldown_eligible") is True
    ]
    if direct_candidates and all_direct:
        practice = {
            "action": "eligible_question_candidate_available",
            "candidate_id": direct_candidates[0].get("formal_node_id"),
            "requires_independent_selection_receipt": True,
            "scheduling_authority": "none_preanswer_read_only",
        }
    elif all_direct:
        practice = {
            "action": "mechanism_check_only",
            "reason": "没有通过独立题源与 7 天门禁的可调题对象",
            "scheduling_authority": "none_preanswer_read_only",
        }
    else:
        practice = {
            "action": "none",
            "reason": "没有直接个人学习证据，不由同科兜底触发练习",
            "scheduling_authority": "none_preanswer_read_only",
        }
    gaps: list[str] = []
    if not targets.get("point_ids") and not targets.get("mechanism_ids") and not targets.get("formal_node_ids"):
        gaps.append("target_only_broadly_resolved")
    if not recent_direct:
        gaps.append("no_direct_recent_error")
    if not historical_direct:
        gaps.append("no_direct_formal_history")
    if corpus_status != "ready":
        gaps.append(f"formal_history_{corpus_status}")
    if not patterns and len(
        {row.get("evidence_event_id") for row in all_direct if row.get("evidence_event_id")}
    ) < 2:
        gaps.append("insufficient_distinct_outcomes_for_repeated_pattern")
    return {
        "schema": RESPONSE_PLAN_SCHEMA,
        "current_target": {
            "subjects": list(targets.get("subjects", [])),
            "point_ids": list(targets.get("point_ids", [])),
            "mechanism_ids": list(targets.get("mechanism_ids", [])),
            "mechanism_primitive_ids": list(
                targets.get("mechanism_primitive_ids", [])
            ),
            "formal_node_ids": list(targets.get("formal_node_ids", [])),
        },
        "direct_recent_errors": [_compact_match(row) for row in recent_direct[:4]],
        "direct_historical_errors": [
            _compact_match(row) for row in historical_direct[:3]
        ],
        "common_patterns": patterns,
        "verified_cross_connections": [
            {
                "edge_id": row.get("edge_id"),
                "other_point_id": row.get("target_point_id")
                or row.get("other_point_id"),
                "relation_code": row.get("relation_code"),
                "strength": row.get("strength"),
                "live_routable": bool(row.get("live_routable")),
                "match_basis": "verified_cross_subject",
                "user_evidence": [
                    _compact_match(match)
                    for match in cross_evidence_matches
                    if match.get("cross_edge_id") == row.get("edge_id")
                ][:2],
            }
            for row in cross_rows
            if row.get("lifecycle") == "verified_existing"
        ][:2],
        "explanation_strategy": {
            "strategy": strategy,
            "depth": depth,
            "personalization_claim_allowed": bool(all_direct),
            "fallback_must_not_drive_core": True,
        },
        "first_boundary_to_check": {
            "boundary_id": first_boundary,
            "boundary_label": FACET_LABELS.get(first_boundary, first_boundary),
            "status": "evidence_backed" if first_boundary else "not_identified",
        },
        "optional_practice_action": practice,
        "evidence_gaps": sorted(set(gaps)),
        "background_context": [_compact_match(row) for row in recent_background[:3]],
    }
