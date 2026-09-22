#!/usr/bin/env python3
"""Validate full-library question/error-evidence review records.

This tool is read-only unless ``--out-dir`` is supplied.  It validates the
second review layer that distinguishes a real user-error record from an
analysis-derived review focus.  It never reads or writes ``复盘输出/``.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


SCHEMA = "question_evidence_review_v1"
ERROR_STATUSES = {
    "explicit_cause",
    "explicit_response",
    "formal_specific_record",
    "historical_specific_record",
    "none_recorded",
    "needs_user",
}
ENTRY_DECISIONS = {"keep", "refine", "downgrade_to_no_record", "needs_user"}
LABEL_DECISIONS = {"keep", "refine", "clear", "needs_user"}
ACTION_DECISIONS = {"keep", "refine", "needs_user"}
CONFIDENCE = {"high", "medium", "low"}
EVIDENCE_GRADES = {"A", "B", "C", "D"}
NO_ERROR_TEXT = "暂无明确错因；本轮仅依据题图与解析确认考查机制，不推断个人错误原因。"
GENERIC_ERROR_FRAGMENTS = (
    "历史归档导入：有错因描述",
    "历史归档导入：错因待复核",
)
GENERIC_ACTION_FRAGMENTS = (
    "先用主知识点复述判定规则",
    "先写出初始状态和更新规则，再逐步维护状态表",
    "先列出题设中的地址、容量、字段、单位和边界条件",
)
PROTECTED_PATTERNS = (
    re.compile(r"(?:正确|标准|错误|我的|用户)(?:答案|选择)\s*[:：]"),
    re.compile(r"答案是\s*[A-D0-9]", re.I),
    re.compile(r"(?:选|选择|选项)\s*[A-D](?:\b|[、，,。])", re.I),
)
LABEL_RE = re.compile(r"^E(?:0[1-9]|10)(?:\s+.+)?$")
EDGE_DECISIONS = {"change_type", "refine_reason", "remove", "add"}
RELATION_TYPES = {f"R{index:02d}" for index in range(1, 10)}


class ReviewError(RuntimeError):
    pass


def _load_json(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ReviewError(f"JSON object required: {path}")
    return value


def _load_records(paths: list[Path]) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    for path in paths:
        for lineno, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
            if not line.strip():
                continue
            try:
                value = json.loads(line)
            except json.JSONDecodeError as exc:
                raise ReviewError(f"{path}:{lineno}: invalid JSON: {exc}") from exc
            if not isinstance(value, dict):
                raise ReviewError(f"{path}:{lineno}: object required")
            value["_source"] = f"{path}:{lineno}"
            records.append(value)
    return records


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _protected_hits(value: Any, prefix: str = "") -> list[str]:
    hits: list[str] = []
    if isinstance(value, str):
        for pattern in PROTECTED_PATTERNS:
            if pattern.search(value):
                hits.append(prefix or "<string>")
                break
    elif isinstance(value, list):
        for index, item in enumerate(value):
            hits.extend(_protected_hits(item, f"{prefix}[{index}]"))
    elif isinstance(value, dict):
        for key, item in value.items():
            if key == "_source":
                continue
            hits.extend(_protected_hits(item, f"{prefix}.{key}" if prefix else key))
    return hits


def _card_field(text: str, label: str) -> str:
    match = re.search(rf"^-\s*{re.escape(label)}[：:]\s*(.*)$", text, re.M)
    return match.group(1).strip() if match else ""


def _label_list(value: str) -> list[str]:
    if not value or value in {"暂无明确错因", "未记录", "待补充", "待确认"}:
        return []
    return [item.strip() for item in value.split("；") if item.strip()]


def _load_edge_records(paths: list[Path] | None) -> list[dict[str, Any]]:
    if not paths:
        return []
    records: list[dict[str, Any]] = []
    for path in paths:
        for lineno, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
            if not line.strip():
                continue
            value = json.loads(line)
            if not isinstance(value, dict):
                raise ReviewError(f"{path}:{lineno}: edge review object required")
            value["_source"] = f"{path}:{lineno}"
            records.append(value)
    return records


def _pair(a: str, b: str) -> str:
    return "::".join(sorted((a, b)))


def validate(
    snapshot_path: Path,
    review_paths: list[Path],
    edge_review_paths: list[Path] | None = None,
) -> dict[str, Any]:
    snapshot = _load_json(snapshot_path)
    if snapshot.get("schema_version") != "relation_semantic_snapshot_v1":
        raise ReviewError("snapshot schema must be relation_semantic_snapshot_v1")
    expected = {str(item["id"]): item for item in snapshot.get("nodes", [])}
    records = _load_records(review_paths)
    errors: list[str] = []
    by_id: dict[str, dict[str, Any]] = {}
    action_counts: Counter[str] = Counter()
    status_counts: Counter[str] = Counter()
    subject_counts: Counter[str] = Counter()
    decision_counts: Counter[str] = Counter()
    live_repo_value = str(snapshot.get("repo_root", "")).strip()
    live_repo = Path(live_repo_value).expanduser().resolve() if live_repo_value else None

    for record in records:
        source = str(record.get("_source", "<unknown>"))
        if record.get("record_type") != SCHEMA:
            errors.append(f"{source}: record_type must be {SCHEMA}")
            continue
        node_id = str(record.get("id", ""))
        if node_id in by_id:
            errors.append(f"{source}: duplicate id {node_id}")
            continue
        by_id[node_id] = record
        node = expected.get(node_id)
        if node is None:
            errors.append(f"{source}: id not in snapshot: {node_id}")
            continue
        subject = str(record.get("subject", ""))
        if subject != node.get("subject"):
            errors.append(f"{source}: subject mismatch for {node_id}")
        grade = str(record.get("evidence_grade", ""))
        snapshot_grade = str(node.get("evidence_grade", ""))
        if grade not in EVIDENCE_GRADES or (
            grade != snapshot_grade and not (snapshot_grade == "A" and grade == "B")
        ):
            errors.append(f"{source}: evidence_grade mismatch for {node_id}")
        question_checked = record.get("question_checked")
        analysis_checked = record.get("analysis_checked")
        if not isinstance(question_checked, bool) or not isinstance(analysis_checked, bool):
            errors.append(f"{source}: checked fields must be boolean")
        if grade in {"A", "B", "C"} and not question_checked:
            errors.append(f"{source}: {grade}-grade question must be checked")
        if grade in {"A", "B", "C"} and not analysis_checked:
            errors.append(f"{source}: {grade}-grade analysis must be checked")

        status = str(record.get("user_error_status", ""))
        if status not in ERROR_STATUSES:
            errors.append(f"{source}: invalid user_error_status {status!r}")
        if grade == "D" and status != "needs_user":
            errors.append(f"{source}: D-grade node must be needs_user")
        sources = record.get("user_error_sources")
        if not isinstance(sources, list):
            errors.append(f"{source}: user_error_sources must be a list")
        elif status in {
            "explicit_cause",
            "explicit_response",
            "formal_specific_record",
            "historical_specific_record",
        } and not sources:
            errors.append(f"{source}: specific error status requires a source")
        elif isinstance(sources, list):
            source_values = [str(item) for item in sources]
            if any(not item.startswith(("formal:", "details:")) for item in source_values):
                errors.append(f"{source}: evidence sources must use formal:/details: paths")
            if status in {"explicit_cause", "explicit_response"} and not any(
                item.startswith("details:") for item in source_values
            ):
                errors.append(f"{source}: details-based status requires a details: source")
            if status == "formal_specific_record" and not any(
                item.startswith("formal:") for item in source_values
            ):
                errors.append(f"{source}: formal_specific_record requires a formal: source")
            if status == "historical_specific_record" and not any(
                "历史错题归档" in item for item in source_values
            ):
                errors.append(f"{source}: historical_specific_record requires a historical path")

        entry_decision = str(record.get("user_error_entry_decision", ""))
        label_decision = str(record.get("error_labels_decision", ""))
        action_decision = str(record.get("redo_first_action_decision", ""))
        if entry_decision not in ENTRY_DECISIONS:
            errors.append(f"{source}: invalid user_error_entry_decision")
        if label_decision not in LABEL_DECISIONS:
            errors.append(f"{source}: invalid error_labels_decision")
        if action_decision not in ACTION_DECISIONS:
            errors.append(f"{source}: invalid redo_first_action_decision")

        entry = str(record.get("user_error_entry", "")).strip()
        labels = record.get("error_labels")
        action = str(record.get("redo_first_action", "")).strip()
        actual = str(record.get("actual_exam_point", "")).strip()
        mechanism = str(record.get("mechanism_key", "")).strip()
        implications = str(record.get("relation_implications", "")).strip()
        confidence = str(record.get("confidence", ""))
        if status == "none_recorded":
            if entry != NO_ERROR_TEXT:
                errors.append(f"{source}: none_recorded must use canonical no-error text")
            if labels != []:
                errors.append(f"{source}: none_recorded must have empty error_labels")
            if label_decision != "clear":
                errors.append(f"{source}: none_recorded must clear inferred labels")
        if status == "explicit_response" and labels not in ([], None):
            errors.append(f"{source}: explicit_response alone cannot support error labels")
        if status == "needs_user" and labels not in ([], None):
            errors.append(f"{source}: needs_user cannot publish error labels")
        if not isinstance(labels, list):
            errors.append(f"{source}: error_labels must be a list")
        else:
            if len(labels) > 2:
                errors.append(f"{source}: keep at most two directly evidenced error labels")
            for label in labels:
                if not isinstance(label, str) or not LABEL_RE.match(label):
                    errors.append(f"{source}: invalid error label {label!r}")
        if not entry or not action or not actual or not mechanism or not implications:
            errors.append(f"{source}: required safe semantic field is empty")
        for fragment in GENERIC_ERROR_FRAGMENTS:
            if fragment in entry:
                errors.append(f"{source}: generic error provenance remains")
        for fragment in GENERIC_ACTION_FRAGMENTS:
            if fragment in action:
                errors.append(f"{source}: generic redo action remains")
        if confidence not in CONFIDENCE:
            errors.append(f"{source}: invalid confidence")

        # On a real frozen snapshot, decisions must describe an actual delta
        # from the current safe card rather than being decorative metadata.
        if live_repo is not None:
            card = live_repo / f"{node_id}.md"
            if card.is_file():
                current_text = card.read_text(encoding="utf-8")
                current_entry = _card_field(current_text, "用户错误入口")
                current_action = _card_field(current_text, "复做第一动作")
                current_labels = _label_list(_card_field(current_text, "错因标签"))
                current_relation_status = _card_field(current_text, "关系复核")
                if current_relation_status == "needs_user" and status != "needs_user":
                    errors.append(
                        f"{source}: identity/relation needs_user node must keep error evidence unconfirmed"
                    )
                if status == "needs_user":
                    if (
                        entry_decision != "needs_user"
                        or label_decision != "needs_user"
                        or action_decision != "needs_user"
                    ):
                        errors.append(f"{source}: needs_user decisions must remain needs_user")
                else:
                    expected_entry = (
                        "keep" if entry == current_entry
                        else "downgrade_to_no_record" if status == "none_recorded"
                        else "refine"
                    )
                    if entry_decision != expected_entry:
                        errors.append(
                            f"{source}: user_error_entry_decision should be {expected_entry}"
                        )
                    expected_action = "keep" if action == current_action else "refine"
                    if action_decision != expected_action:
                        errors.append(
                            f"{source}: redo_first_action_decision should be {expected_action}"
                        )
                    if status == "none_recorded":
                        expected_label = "clear"
                    elif labels == current_labels:
                        expected_label = "keep"
                    elif labels:
                        expected_label = "refine"
                    else:
                        expected_label = "clear"
                    if label_decision != expected_label:
                        errors.append(
                            f"{source}: error_labels_decision should be {expected_label}"
                        )
        hits = _protected_hits(record)
        if hits:
            errors.append(f"{source}: protected answer/option pattern in {', '.join(hits[:4])}")
        action_counts[action] += 1
        status_counts[status] += 1
        subject_counts[subject] += 1
        decision_counts[f"entry:{entry_decision}"] += 1
        decision_counts[f"labels:{label_decision}"] += 1
        decision_counts[f"action:{action_decision}"] += 1

    missing = sorted(set(expected) - set(by_id))
    extra = sorted(set(by_id) - set(expected))
    if missing:
        errors.append(f"missing node reviews: {len(missing)} ({', '.join(missing[:12])})")
    if extra:
        errors.append(f"extra node reviews: {len(extra)} ({', '.join(extra[:12])})")

    edge_records = _load_edge_records(edge_review_paths)
    snapshot_edges = {
        _pair(str(edge.get("a", "")), str(edge.get("b", ""))): edge
        for edge in snapshot.get("edges", []) if isinstance(edge, dict)
    }
    edge_adjustments: dict[str, dict[str, Any]] = {}
    for edge in edge_records:
        source = str(edge.get("_source", "<unknown>"))
        if edge.get("record_type") != "edge_evidence_review_v1":
            errors.append(f"{source}: invalid edge record_type")
            continue
        a, b = str(edge.get("a", "")), str(edge.get("b", ""))
        key = _pair(a, b)
        if not a or not b or a == b or key in edge_adjustments:
            errors.append(f"{source}: invalid or duplicate edge pair {key}")
            continue
        decision = str(edge.get("decision", ""))
        if decision not in EDGE_DECISIONS:
            errors.append(f"{source}: invalid edge decision {decision!r}")
            continue
        baseline = snapshot_edges.get(key)
        if decision == "add":
            if baseline is not None or edge.get("new_edge_candidate") is not True:
                errors.append(f"{source}: add must be a new candidate pair")
        else:
            if baseline is None:
                errors.append(f"{source}: adjusted edge not in snapshot: {key}")
            elif str(edge.get("current_type", "")).split()[0] != str(
                baseline.get("current_type", "")
            ).split()[0]:
                errors.append(f"{source}: current_type differs from snapshot")
        proposed = str(edge.get("proposed_type", "")).split()[0]
        if decision in {"change_type", "refine_reason", "add"} and proposed not in RELATION_TYPES:
            errors.append(f"{source}: proposed_type must be R01-R09")
        reason = str(edge.get("reason", "")).strip()
        if decision != "remove" and len(reason) < 25:
            errors.append(f"{source}: edge reason is not specific enough")
        if str(edge.get("confidence", "")) != "high":
            errors.append(f"{source}: formal edge adjustment requires high confidence")
        if _protected_hits(edge):
            errors.append(f"{source}: protected content in edge review")
        edge_adjustments[key] = edge

    # The current post-semantic-review graph is part of this snapshot.  Only
    # R03 depends directly on personal-error evidence; it must not survive a
    # cleanup when either endpoint has no recorded user error.
    for edge in snapshot.get("edges", []):
        if not isinstance(edge, dict):
            errors.append("snapshot edge must be an object")
            continue
        a, b = str(edge.get("a", "")), str(edge.get("b", ""))
        left, right = by_id.get(a), by_id.get(b)
        if left is None or right is None:
            continue
        adjustment = edge_adjustments.get(_pair(a, b))
        removing_or_retyping = adjustment and adjustment.get("decision") in {"remove", "change_type"}
        if str(edge.get("current_type", "")).split()[0] != "R03":
            continue
        if removing_or_retyping:
            continue
        allowed_specific = {
            "explicit_cause",
            "formal_specific_record",
            "historical_specific_record",
        }
        if left.get("user_error_status") not in allowed_specific or right.get("user_error_status") not in allowed_specific:
            errors.append(f"R03 lacks explicit cause evidence: {a}::{b}")
        left_codes = {str(value).split()[0] for value in left.get("error_labels", [])}
        right_codes = {str(value).split()[0] for value in right.get("error_labels", [])}
        if not left_codes.intersection(right_codes):
            errors.append(f"R03 endpoints have no shared evidence-backed label: {a}::{b}")
    duplicates = {value: count for value, count in action_counts.items() if value and count > 3}
    if duplicates:
        sample = sorted(duplicates.items(), key=lambda item: (-item[1], item[0]))[:8]
        errors.append(f"redo_first_action duplicated more than 3 times: {sample}")

    return {
        "schema_version": "question_evidence_validation_v1",
        "created_at": datetime.now(timezone.utc).isoformat(),
        "snapshot": str(snapshot_path.resolve()),
        "snapshot_sha256": _sha256(snapshot_path),
        "expected_nodes": len(expected),
        "reviewed_nodes": len(by_id),
        "status": "PASS" if not errors else "FAIL",
        "counts": {
            "subjects": dict(sorted(subject_counts.items())),
            "user_error_status": dict(sorted(status_counts.items())),
            "decisions": dict(sorted(decision_counts.items())),
            "edge_adjustments": len(edge_adjustments),
        },
        "errors": errors,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--snapshot", required=True, type=Path)
    parser.add_argument("--reviews", required=True, nargs="+", type=Path)
    parser.add_argument("--edge-reviews", nargs="*", type=Path, default=[])
    parser.add_argument("--out-dir", type=Path)
    parser.add_argument("--overwrite", action="store_true")
    args = parser.parse_args()
    try:
        report = validate(args.snapshot, args.reviews, args.edge_reviews)
        if args.out_dir:
            args.out_dir.mkdir(parents=True, exist_ok=True)
            target = args.out_dir / "validation-report.json"
            if target.exists() and not args.overwrite:
                raise ReviewError(f"refusing to overwrite {target}")
            target.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        print(json.dumps(report, ensure_ascii=False, indent=2))
        return 0 if report["status"] == "PASS" else 1
    except (OSError, json.JSONDecodeError, ReviewError) as exc:
        print(json.dumps({"status": "ERROR", "error": str(exc)}, ensure_ascii=False, indent=2))
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
