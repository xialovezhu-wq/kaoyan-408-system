#!/usr/bin/env python3
"""Audit the live formal repo after a question-evidence review publish."""

from __future__ import annotations

import argparse
import json
import re
import sys
from collections import Counter
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))

from apply_question_evidence_review import (  # noqa: E402
    NEEDS_ERROR_TEXT,
    NEEDS_RECENT,
    _labels,
    _needs_action,
)
from normalize_408_knowledge_points import read_table_with_preamble  # noqa: E402
from question_evidence_review import (  # noqa: E402
    GENERIC_ACTION_FRAGMENTS,
    GENERIC_ERROR_FRAGMENTS,
    _load_edge_records,
    _load_records,
)
from relation_semantic_review import pair_key  # noqa: E402


def _field(text: str, label: str) -> str:
    match = re.search(rf"^-\s*{re.escape(label)}[：:]\s*(.*)$", text, re.M)
    return match.group(1).strip() if match else ""


def audit(
    repo: Path,
    review_files: list[Path],
    edge_files: list[Path],
    expected_nodes: int | None,
    expected_edges: int | None,
) -> dict[str, Any]:
    errors: list[str] = []
    raw_records = _load_records(review_files)
    records: dict[str, dict[str, Any]] = {}
    for record in raw_records:
        node_id = str(record.get("id", ""))
        if node_id in records:
            errors.append(f"duplicate review id: {node_id}")
        records[node_id] = {k: v for k, v in record.items() if k != "_source"}
    if expected_nodes is not None and len(records) != expected_nodes:
        errors.append(f"review count {len(records)} != {expected_nodes}")

    _, _, master_rows = read_table_with_preamble(repo / "节点总表.md")
    master = {row["ID"]: row for row in master_rows}
    if set(master) != set(records):
        errors.append("review id set differs from live master table")

    status_counts: Counter[str] = Counter()
    generic_actions: list[str] = []
    generic_entries: list[str] = []
    for node_id, record in sorted(records.items()):
        row = master.get(node_id)
        card_path = repo / f"{node_id}.md"
        if row is None or not card_path.is_file():
            errors.append(f"missing live row/card: {node_id}")
            continue
        status = str(record["user_error_status"])
        expected_label = _labels(record)
        expected_entry = (
            NEEDS_ERROR_TEXT if status == "needs_user" else str(record["user_error_entry"]).strip()
        )
        expected_action = (
            _needs_action(record) if status == "needs_user" else str(record["redo_first_action"]).strip()
        )
        text = card_path.read_text(encoding="utf-8")
        actual_status = _field(text, "错因证据状态")
        actual_entry = _field(text, "用户错误入口")
        actual_action = _field(text, "复做第一动作")
        actual_label = _field(text, "错因标签")
        if actual_status != status:
            errors.append(f"{node_id}: evidence status mismatch")
        if actual_entry != expected_entry:
            errors.append(f"{node_id}: user-error entry mismatch")
        if actual_action != expected_action:
            errors.append(f"{node_id}: redo first action mismatch")
        if actual_label != expected_label or row.get("错因标签") != expected_label:
            errors.append(f"{node_id}: error labels not reconciled")
        if status == "needs_user" and row.get("最近错误记录") != NEEDS_RECENT:
            errors.append(f"{node_id}: needs_user recent error not neutralized")
        if len(re.findall(r"E(?:0[1-9]|10)", expected_label)) > 2:
            errors.append(f"{node_id}: more than two live error labels")
        if any(fragment in actual_action for fragment in GENERIC_ACTION_FRAGMENTS):
            generic_actions.append(node_id)
        if any(fragment in actual_entry for fragment in GENERIC_ERROR_FRAGMENTS):
            generic_entries.append(node_id)
        status_counts[status] += 1
    if generic_actions:
        errors.append(f"generic redo actions remain: {generic_actions[:20]}")
    if generic_entries:
        errors.append(f"generic error entries remain: {generic_entries[:20]}")

    _, _, relation_rows = read_table_with_preamble(repo / "关系边表.md")
    if expected_edges is not None and len(relation_rows) != expected_edges:
        errors.append(f"live edge count {len(relation_rows)} != {expected_edges}")
    by_pair = {
        pair_key(row["起点ID"], row["终点ID"]): row for row in relation_rows
    }
    edge_records = [
        {k: v for k, v in record.items() if k != "_source"}
        for record in _load_edge_records(edge_files)
    ]
    for record in edge_records:
        key = pair_key(str(record["a"]), str(record["b"]))
        row = by_pair.get(key)
        decision = str(record["decision"])
        if decision == "remove":
            if row is not None:
                errors.append(f"removed edge still present: {key}")
            continue
        if row is None:
            errors.append(f"published edge missing: {key}")
            continue
        code = str(record.get("proposed_type") or row["关系类型"]).split()[0]
        if not row["关系类型"].startswith(code):
            errors.append(f"{key}: relation type mismatch")
        if record.get("proposed_strength") and row["联系强度"] != record["proposed_strength"]:
            errors.append(f"{key}: relation strength mismatch")
        if record.get("proposed_priority") and row["复盘优先级"] != record["proposed_priority"]:
            errors.append(f"{key}: relation priority mismatch")
        if row["关联原因"] != str(record["reason"]).strip():
            errors.append(f"{key}: relation reason mismatch")

    r03_count = 0
    for row in relation_rows:
        if not row["关系类型"].startswith("R03"):
            continue
        r03_count += 1
        a, b = row["起点ID"], row["终点ID"]
        allowed = {
            "explicit_cause",
            "formal_specific_record",
            "historical_specific_record",
        }
        if records[a]["user_error_status"] not in allowed or records[b]["user_error_status"] not in allowed:
            errors.append(f"R03 lacks explicit user-error evidence: {a}::{b}")
        left = {str(value).split()[0] for value in records[a]["error_labels"]}
        right = {str(value).split()[0] for value in records[b]["error_labels"]}
        if not left.intersection(right):
            errors.append(f"R03 lacks shared label: {a}::{b}")

    return {
        "status": "PASS" if not errors else "FAIL",
        "nodes": len(master),
        "edges": len(relation_rows),
        "user_error_status": dict(sorted(status_counts.items())),
        "edge_adjustments_verified": len(edge_records),
        "r03_edges": r03_count,
        "generic_redo_actions": len(generic_actions),
        "generic_error_entries": len(generic_entries),
        "errors": errors,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo", default=".", type=Path)
    parser.add_argument("--reviews", required=True, nargs="+", type=Path)
    parser.add_argument("--edge-reviews", nargs="*", default=[], type=Path)
    parser.add_argument("--expect-nodes", type=int)
    parser.add_argument("--expect-edges", type=int)
    parser.add_argument("--json-out", type=Path)
    args = parser.parse_args()
    report = audit(
        args.repo.expanduser().resolve(),
        [path.expanduser().resolve() for path in args.reviews],
        [path.expanduser().resolve() for path in args.edge_reviews],
        args.expect_nodes,
        args.expect_edges,
    )
    if args.json_out:
        args.json_out.parent.mkdir(parents=True, exist_ok=True)
        args.json_out.write_text(
            json.dumps(report, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0 if report["status"] == "PASS" else 1


if __name__ == "__main__":
    raise SystemExit(main())
