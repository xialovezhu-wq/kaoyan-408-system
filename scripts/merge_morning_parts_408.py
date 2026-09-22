#!/usr/bin/env python3
"""Preserve parallel D returns and merge their exact scope before native prepare.

This assembler does not attest semantic correctness or bypass native D gates.
Malformed or conflicting packs remain saved for bounded local repair.
"""
from __future__ import annotations

import argparse
import copy
import json
from pathlib import Path
import shutil

from split_web_input_408 import encoded, lines, read_archive, require, rows, save_archive, sha
from morning_question_policy_408 import order_items

TABLES = ("selected_items", "supplemental_items", "substitute_items", "web_variants", "asset_refs", "unresolved")
CONTROL = {"manifest.json", "manifest.fields.json", "partition.json", "due_concepts.json", "coverage_report.json",
           "progress.json", "web_execution_report.json", "EXECUTION_REPORT.md", "START_HERE.md", "rationale.md",
           "workspace-input-index.json", "workspace-validation.json", "items.jsonl", "items_ordered.jsonl",
           "QUESTION_INDEX.md", "FINAL_FILE_AUDIT.json"} | {n + ".jsonl" for n in TABLES}


def coalesce_asset_refs(values):
    """Combine additive metadata for an identical source; retain real conflicts."""
    result = []
    for row in values:
        compatible = next((old for old in result
            if old.get("package_path") == row.get("package_path")
            and all(old.get(k) == row.get(k) for k in
                    ("question_id", "source_path", "role", "visual_status"))
            and all(old[k] == row[k] for k in set(old) & set(row)
                    if old[k] is not None and row[k] is not None)), None)
        if compatible is None:
            result.append(copy.deepcopy(row))
        else:
            compatible.update({k: v for k, v in row.items() if v is not None})
    return result


def merge_parts(index_path, zip_paths, output_dir):
    index = json.loads(Path(index_path).read_text())
    require(index.get("schema") == "cs408-web-partition-set-v1" and index.get("kind") == "morning", "morning_partition_index_required")
    parent_path = Path(index["parent_zip_path"])
    require(sha(parent_path.read_bytes()) == index["parent_zip_sha256"], "parent_input_drift")
    parent_data, parent = read_archive(parent_path)
    targets = rows(parent_data["scope/review_concept_ids.jsonl"])
    expected = {r["part_id"]: r for r in index["parts"]}
    require(index["parent_unit_ids"] == targets and
        [p for r in index["parts"] for p in r["assigned_unit_ids"]] == targets and len(set(targets)) == len(targets),
        "partition_index_scope_drift")
    base = Path(output_dir).expanduser().resolve()
    base.mkdir(parents=True, exist_ok=True)
    out, roles = {}, {}
    tables = {n: [] for n in TABLES}
    accepted, rejected, covered, seen_parts = [], [], set(), set()
    completed_units, failures, rationales = [], [], []
    for source in map(Path, zip_paths):
        digest = sha(source.read_bytes())
        original = base / "originals" / (digest + ".zip")
        original.parent.mkdir(exist_ok=True)
        if not original.exists():
            shutil.copyfile(source, original)
        try:
            data, manifest = read_archive(original)
            part = manifest.get("partition") or json.loads(data.get("partition.json", b"{}"))
            pid = part.get("part_id")
            require(pid in expected and pid not in seen_parts, "unknown_or_duplicate_part")
            exp = expected[pid]
            require(all(part.get(k) == exp[k] for k in ("partition_set_id", "parent_zip_sha256", "parent_input_version",
                "input_version", "assigned_unit_ids", "part_index", "part_count")), "partition_binding_drift")
            require(all(manifest.get(k) == parent[k] for k in ("source_commit", "target_date", "due_version", "concept_priority_version")),
                    "partition_source_drift")
            scope = json.loads(data["due_concepts.json"])
            require(scope["review_concept_ids"] == exp["assigned_unit_ids"], "return_scope_drift")
            assigned = set(exp["assigned_unit_ids"])
            local = {n: rows(data.get(n + ".jsonl", b"")) for n in TABLES}
            represented = set()
            for name in ("selected_items", "supplemental_items", "substitute_items"):
                for row in local[name]:
                    points = row.get("review_target_concept_ids", [])
                    require(points and set(points) <= assigned, "row_outside_assigned_scope")
                    require(not represented.intersection(points), "duplicate_point_representative")
                    for field in ("problem_path", "solution_path", "history_path"):
                        require(row.get(field) in data and data[row[field]].strip(), "substantive_file_missing:" + field)
                    represented.update(points)
            coverage = json.loads(data.get("coverage_report.json", b"{}"))
            done = set(coverage.get("covered_concept_ids", []))
            unresolved = set(coverage.get("unresolved_concept_ids", []))
            require(done <= represented and done <= assigned and unresolved <= assigned and not done & unresolved,
                    "unsupported_coverage_claim")
            files = {n: raw for n, raw in data.items() if n not in CONTROL}
            # Reports are scoped to their producing conversation. Identical
            # audit filenames in independent parts are not question conflicts.
            incidental = {n for n in files if n.startswith("audit/") or
                          n in {"private/final-audit.json", "private/final-file-audit.json"}}
            for name in incidental:
                out[f"private/partition-records/{pid}/{name}"] = files.pop(name)
            for name, raw in files.items():
                require(name not in out or out[name] == raw, "conflicting_file:" + name)
            # Adopt the whole validated part only after every local check passes.
            out.update(files)
            roles.update({r["path"]: r["role"] for r in manifest["files"] if r["path"] in files})
            for name in TABLES:
                tables[name].extend(local[name])
            if "web_execution_report.json" in data:
                report = json.loads(data["web_execution_report.json"])
                completed_units.extend(report.get("completed_units", []))
                failures.extend(report.get("failures", []))
            rationales.append(pid + "\n\n" + data.get("rationale.md", b"").decode())
            for name in CONTROL:
                if name in data:
                    out[f"private/partition-records/{pid}/{name}"] = data[name]
            seen_parts.add(pid)
            covered.update(done)
            accepted.append({"part_id": pid, "original_zip": str(original), "sha256": digest, "covered_count": len(done)})
        except (ValueError, KeyError, TypeError, OSError) as exc:
            rejected.append({"original_zip": str(original), "sha256": digest, "reason": str(exc)})
    missing = [p for p in targets if p not in covered]
    for point in missing:
        if not any(point in r.get("review_target_concept_ids", []) or r.get("concept_id") == point for r in tables["unresolved"]):
            tables["unresolved"].append({"concept_id": point, "review_target_concept_ids": [point], "reason": "parallel_return_local_completion_required"})
    for name, value in tables.items():
        if name in {"selected_items", "supplemental_items", "substitute_items"}:
            value = order_items(value, targets, unclassified_class=(
                "variant" if name == "supplemental_items" else "identity_unclassified"
            ))
        # Identical shared asset references are harmless; conflicting targets remain native-gated.
        if name == "asset_refs":
            value = coalesce_asset_refs(value)
        out[name + ".jsonl"] = lines(value)
    out["due_concepts.json"] = parent_data["return-template/due_concepts.json"]
    out["coverage_report.json"] = encoded({"covered_concept_ids": [p for p in targets if p in covered],
        "unresolved_concept_ids": missing, "parent_target_count": len(targets)})
    status = "MERGED_PENDING_NATIVE_PREPARE" if not missing and not rejected and len(seen_parts) == len(expected) else "PARTIAL_LOCAL_COMPLETION_REQUIRED"
    proof = {"schema": "cs408-morning-partition-merge-v1", "status": status, "partition_set_id": index["partition_set_id"],
        "parent_zip_sha256": index["parent_zip_sha256"], "accepted": accepted, "rejected": rejected,
        "missing_part_ids": sorted(set(expected) - seen_parts), "pending_concept_ids": missing,
        "covered_count": len(covered), "target_count": len(targets), "formal_write_count": 0, "learning_started": False}
    out["private/partition-merge.json"] = encoded(proof)
    out["progress.json"] = encoded({"completed_concept_ids": sorted(covered), "pending_concept_ids": missing})
    out["rationale.md"] = "\n\n".join(rationales).encode()
    out["START_HERE.md"] = b"Local merged D delivery. Complete missing units, then run native prepare_d_review_408.py.\n"
    # Failures retain their original per-part call identities in the saved reports.
    for i, failure in enumerate(failures, 1):
        failure["sequence"] = i
    out["web_execution_report.json"] = encoded({"schema": "cs408-web-execution-report-v1", "source_commit": parent["source_commit"],
        "completed_units": completed_units, "pending_unit_ids": missing, "failures": failures})
    roles["web_execution_report.json"] = "web_execution_report"
    out["EXECUTION_REPORT.md"] = (f"本地合并：{len(covered)}/{len(targets)}点，待补{len(missing)}点。\n\n"
        "原始分片ZIP及逐片报告全部保留；本结果仍需原生身份、来源、曝光和题目验收。\n" + json.dumps(proof, ensure_ascii=False, indent=2)).encode()
    manifest = json.loads(parent_data["return-template/manifest.fields.json"])
    manifest.update(run_id="D408-MERGED-" + index["partition_set_id"], status="WEB_PREPARED" if status.startswith("MERGED_") else "PARTIAL")
    target = base / ("408_morning_review_" + parent["target_date"] + "_merged.zip")
    digest = save_archive(target, out, manifest, roles=roles)
    read_archive(target)
    result = dict(proof, zip_path=str(target), zip_sha256=digest)
    (base / "merge-receipt.json").write_bytes(encoded(result))
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--partition-set", required=True)
    parser.add_argument("--zip", action="append", required=True)
    parser.add_argument("--output-dir", required=True)
    args = parser.parse_args()
    print(json.dumps(merge_parts(args.partition_set, args.zip, args.output_dir), ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
