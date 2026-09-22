#!/usr/bin/env python3
"""Export a coherent read-only native scheduler dependency snapshot for publication.

Only the explicitly supplied external engineering output directory is written.
No cohort, due list, learning event, Capture, or admission is created in the repo.
"""
from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
import shutil
import sys
import tempfile
from pathlib import Path
from typing import Any

REPO = Path(__file__).resolve().parents[1]
INPUTS = (
    ("节点总表.md", "节点总表.md", "formal_cohort_and_main_concepts"),
    ("原题复做轨总表.md", "原题复做轨总表.md", "accepted_original_track_eligibility"),
    ("wiki/study_vaults/408-full/state/review-loop/events.jsonl",
     "learning-state/review-loop/events.jsonl", "full_review_event_history"),
)
ADMISSIONS = ("wiki/study_vaults/408-full/state/review-campaign/admissions.jsonl",
              "learning-state/review-campaign/admissions.jsonl")
ALGORITHM = "scripts/morning_coverage_campaign_408.py"
RETIREMENT_SNAPSHOT = "support/d408/formal-retirement-evidence.json"


def raw_json(value: Any) -> bytes:
    return (json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2) + "\n").encode()


def sha(path: Path) -> str:
    if path.is_symlink() or not path.is_file() or any(p.is_symlink() for p in path.parents):
        raise ValueError("missing or unsafe dependency: " + str(path))
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _prune_export_snapshots(output_dir: Path, *, keep: int = 5) -> int:
    """Keep only the newest due-support snapshots.

    A snapshot is an immutable derived copy of the same inputs; an older one is
    only a fallback if the newest is damaged, so a few generations is enough.
    """
    if not output_dir.is_dir() or output_dir.is_symlink():
        return 0
    entries = []
    for path in output_dir.iterdir():
        if path.is_dir() and not path.is_symlink() and not path.name.startswith(".build-"):
            try:
                entries.append((path.stat().st_mtime, path))
            except OSError:
                continue
    entries.sort(reverse=True)
    removed = 0
    for _mtime, path in entries[keep:]:
        shutil.rmtree(path, ignore_errors=True)
        removed += 1
    return removed


def export(repo: Path, output_dir: Path, *, retirement_runtime_root: Path | None = None) -> dict[str, Any]:
    repo = repo.resolve(strict=True)
    if output_dir.is_symlink() or any(p.is_symlink() for p in output_dir.parents):
        raise ValueError("engineering output directory must not traverse symlinks")
    output_dir = output_dir.resolve()
    if output_dir == repo or output_dir.is_relative_to(repo):
        raise ValueError("engineering output directory must be outside the input repository")
    from study_bridge_408 import _source_locks
    with _source_locks(repo):
        output_dir.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(prefix=".build-", dir=output_dir) as temporary:
            stage = Path(temporary) / "snapshot"
            stage.mkdir()
            observed = []
            rows = {}

            def copy(source: Path, destination: str, kind: str) -> dict[str, Any]:
                digest = sha(source)
                size = source.stat().st_size
                target = stage / destination
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(source, target)
                if sha(target) != digest or target.stat().st_size != size:
                    raise ValueError("source changed while copying: " + destination)
                observed.append({"source_path": str(source), "sha256": digest, "bytes": size})
                row = {"publish_path": destination, "kind": kind, "sha256": digest,
                       "bytes": size, "formal_ids": []}
                rows[destination] = row
                return row

            inputs = []
            for native, published, role in INPUTS:
                row = copy(repo / native, published, "native_scheduler_input")
                inputs.append({"native_path": native, "published_path": published,
                               "role": role, "sha256": row["sha256"], "bytes": row["bytes"]})
            absent = []
            admission_path = repo / ADMISSIONS[0]
            if admission_path.exists():
                row = copy(admission_path, ADMISSIONS[1], "native_scheduler_input")
                inputs.append({"native_path": ADMISSIONS[0], "published_path": ADMISSIONS[1],
                               "role": "native_campaign_admissions", "sha256": row["sha256"],
                               "bytes": row["bytes"]})
            else:
                absent.append({"native_path": ADMISSIONS[0],
                               "published_paths_to_check": list(ADMISSIONS),
                               "meaning": "empty under native absent-ledger semantics; not a formal-admission ledger"})
            algorithm = copy(repo / ALGORITHM, ALGORITHM, "scheduler_support_code_or_config")
            from concept_review_priority_408 import collect_priority_records, digest, SNAPSHOT_REL
            copy(repo / "scripts/concept_review_priority_408.py", "scripts/concept_review_priority_408.py", "scheduler_support_code_or_config")
            concept_records = collect_priority_records(repo)
            concept_snapshot = {"schema": "cs408-concept-evidence-snapshot-v1", "records": concept_records,
                                "records_sha256": digest(concept_records)}
            target = stage / SNAPSHOT_REL
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(raw_json(concept_snapshot))
            rows[SNAPSHOT_REL] = {"publish_path": SNAPSHOT_REL, "kind": "concept_priority_evidence",
                "sha256": sha(target), "bytes": target.stat().st_size, "formal_ids": []}
            inputs.append({"native_path": SNAPSHOT_REL, "published_path": SNAPSHOT_REL,
                "role": "concept_priority_evidence", "sha256": sha(target), "bytes": target.stat().st_size})
            support = repo / "support/d408"
            for path in sorted(support.rglob("*")):
                if path.is_dir() or path.name.endswith(".pyc") or "__pycache__" in path.parts:
                    continue
                if path.relative_to(repo).as_posix() in {SNAPSHOT_REL, RETIREMENT_SNAPSHOT}:
                    continue
                copy(path, path.relative_to(repo).as_posix(), "scheduler_support_code_or_config")
            if "support/d408/read_only_due.py" not in rows:
                raise ValueError("native read-only replay entry is missing")
            spec = importlib.util.spec_from_file_location("publication_native_scheduler", stage / ALGORITHM)
            native = importlib.util.module_from_spec(spec)
            sys.modules[spec.name] = native
            previous = sys.dont_write_bytecode
            try:
                sys.dont_write_bytecode = True
                spec.loader.exec_module(native)
            finally:
                sys.dont_write_bytecode = previous
            config = native.default_config()
            # Only the verified, minimal retirement proof crosses the publication
            # boundary. The private transaction and raw deletion reason stay local.
            retirement_ledger = repo / native.DELETION_LEDGER_REL
            retirement_ledger_present = retirement_ledger.exists()
            retirement = native.read_retirement_evidence(repo, config, runtime_root=retirement_runtime_root)
            if retirement is not None:
                target = stage / RETIREMENT_SNAPSHOT
                target.write_bytes(native._canonical_bytes(retirement))
                rows[RETIREMENT_SNAPSHOT] = {"publish_path": RETIREMENT_SNAPSHOT,
                    "kind": "formal_retirement_evidence", "sha256": sha(target),
                    "bytes": target.stat().st_size, "formal_ids": []}
                inputs.append({"native_path": RETIREMENT_SNAPSHOT, "published_path": RETIREMENT_SNAPSHOT,
                    "role": "verified_formal_retirements", "sha256": sha(target), "bytes": target.stat().st_size})
                proof_source = retirement_ledger if retirement_ledger_present else repo / RETIREMENT_SNAPSHOT
                observed.append({"source_path": str(proof_source), "sha256": sha(proof_source),
                                 "bytes": proof_source.stat().st_size})
            config_path = "support/d408/default-config.json"
            (stage / config_path).write_bytes(raw_json(config))
            rows[config_path] = {"publish_path": config_path, "kind": "scheduler_support_code_or_config",
                                 "sha256": sha(stage / config_path), "bytes": (stage / config_path).stat().st_size,
                                 "formal_ids": []}
            # Existing formal-cohort snapshots are historical files. Select only
            # exact master-byte matches; they do not supply the date's due run.
            current_cohorts = []
            cohort_dir = repo / "wiki/study_vaults/408-full/state/review-campaign/formal-admission/cohorts"
            for path in sorted(cohort_dir.glob("*.json")):
                value = json.loads(path.read_bytes())
                if (value.get("schema") == "campaign_formal_node_cohort_snapshot_v1"
                        and value.get("master_ref") == "节点总表.md"
                        and value.get("master_sha256") == inputs[0]["sha256"]):
                    destination = "learning-state/" + path.relative_to(repo / "wiki/study_vaults/408-full/state").as_posix()
                    row = copy(path, destination, "review_schedule_evidence")
                    current_cohorts.append({"published_path": destination, "sha256": row["sha256"],
                                            "bytes": row["bytes"], "formal_node_count": value["formal_node_count"],
                                            "cohort_sha256": value["cohort_sha256"]})
            bindings = [{"ref": row["native_path"], "sha256": row["sha256"]} for row in inputs[:3]]
            bindings.append({"ref": ALGORITHM, "sha256": algorithm["sha256"]})
            bindings.extend({"ref": row["native_path"], "sha256": row["sha256"]} for row in inputs[3:]
                            if row["role"] != "concept_priority_evidence")
            components = [{"published_path": row["publish_path"], "sha256": row["sha256"], "bytes": row["bytes"]}
                          for row in rows.values() if row["kind"] == "scheduler_support_code_or_config"]
            manifest = {"schema": "cs408-readonly-due-support-v2", "subject": "408", "project": "D",
                        "source_binding": "same_publication_inventory_and_exact_input_hashes",
                        "target_date_policy": "explicit_replay_argument",
                        "algorithm": {"canonical_source_ref": ALGORITHM, "published_path": ALGORITHM,
                                      "sha256": algorithm["sha256"], "policy_version": native.POLICY_VERSION,
                                      "fsrs": native.FSRS_VERSION},
                        "config_path": config_path, "config_canonical_sha256": native.canonical_sha256(config),
                        "dependency_runtime_path": "support/d408/vendor", "components": components,
                        "inputs": inputs, "absent_inputs": absent, "native_source_bindings": bindings,
                        "current_cohort_selector": {
                            "authoritative_master_path": "节点总表.md", "master_sha256": inputs[0]["sha256"],
                            "rule": "snapshot.master_ref and master_sha256 must match this exact master",
                            "matching_existing_snapshots": current_cohorts,
                            "rebuild_when_no_match": "run read_only_due.py with the explicit target date",
                            "snapshot_is_due_version": False},
                        "formal_writes_allowed": False, "semantic_history_scan_proven": False,
                        "preselected_due_list_included": False, "representative_selection_included": False}
            manifest_path = "support/d408/manifest.json"
            (stage / manifest_path).write_bytes(raw_json(manifest))
            rows[manifest_path] = {"publish_path": manifest_path, "kind": "scheduler_support_manifest",
                                  "sha256": sha(stage / manifest_path), "bytes": (stage / manifest_path).stat().st_size,
                                  "formal_ids": []}
            for row in observed:
                if sha(Path(row["source_path"])) != row["sha256"]:
                    raise ValueError("source changed during support export: " + row["source_path"])
            if digest(collect_priority_records(repo)) != concept_snapshot["records_sha256"]:
                raise ValueError("concept evidence changed during support export")
            if bool(absent) == admission_path.exists():
                raise ValueError("campaign admission presence changed during export")
            if retirement_ledger_present != retirement_ledger.exists():
                raise ValueError("formal deletion ledger presence changed during export")
            snapshot_id = hashlib.sha256(raw_json(sorted(rows.values(), key=lambda row: row["publish_path"]))).hexdigest()
            final = output_dir / snapshot_id
            if final.exists():
                for row in rows.values():
                    if sha(final / row["publish_path"]) != row["sha256"]:
                        raise ValueError("existing engineering snapshot has changed")
            else:
                stage.rename(final)
            _prune_export_snapshots(output_dir, keep=5)
            sources = [dict(row, source_path=str(final / row["publish_path"]))
                       for row in sorted(rows.values(), key=lambda row: row["publish_path"])]
            return {"schema": "cs408-due-support-export-v1", "subject": "408", "sources": sources,
                    "merge_entrypoints": {"due_support": manifest_path}, "input_bindings": observed,
                    "absent_input_bindings": [{"source_path": str(admission_path)}] if absent else [],
                    "snapshot_id": snapshot_id, "formal_write_count": 0, "learner_evidence_write_count": 0}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    command = commands.add_parser("export")
    command.add_argument("--repo", type=Path, required=True)
    command.add_argument("--output-dir", type=Path, required=True)
    args = parser.parse_args()
    try:
        print(json.dumps(export(args.repo, args.output_dir), ensure_ascii=False, sort_keys=True))
    except (ValueError, OSError, RuntimeError) as exc:
        print("DUE_SUPPORT_EXPORT_FAILED: " + str(exc), file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
