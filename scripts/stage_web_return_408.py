#!/usr/bin/env python3
"""Preserve complete or partial D/enhancement returns for local completion.

Staging never applies, grades, starts a session, or executes package code. Valid
files survive even when another member or the web's final status is incomplete.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import shutil
import sys
import tempfile

BRIDGE_ROOT = Path.home() / "Documents/Study-Pro-Bridge"
sys.path.insert(0, str(BRIDGE_ROOT))
from bridge_archives import ArchiveBudget, extract_zip, file_hash, safe_path, source_file

DETAILS = Path.home() / "Documents/kaoyan-408-details"
REPO = Path(__file__).resolve().parents[1]


def stage_return(archive: str | Path, *, kind: str, output_dir: str | Path | None = None):
    if kind not in {"morning", "enhancement"}:
        raise ValueError("web_return_kind_required")
    archive = source_file(Path(archive).expanduser().resolve())
    sha = file_hash(archive)
    base = Path(output_dir).expanduser() if output_dir else DETAILS / "review-deliveries/web-return-staging"
    if any(p.is_symlink() for p in [base, *base.parents]):
        raise ValueError("web_return_unsafe_output")
    if base.resolve().is_relative_to(REPO):
        raise ValueError("web_return_output_must_be_outside_formal_repo")
    delivery = base.resolve() / kind / sha
    delivery.mkdir(parents=True, exist_ok=True)
    original = delivery / "original.zip"
    if original.exists():
        if file_hash(original) != sha:
            raise ValueError("web_return_preserved_original_drift")
    else:
        with tempfile.TemporaryDirectory(prefix=".copy-", dir=delivery) as temp:
            copy = Path(temp) / "original.zip"
            shutil.copyfile(archive, copy)
            if file_hash(copy) != sha:
                raise ValueError("web_return_changed_during_copy")
            copy.rename(original)
    root = delivery / "web-output"
    if not root.exists():
        with tempfile.TemporaryDirectory(prefix=".extract-", dir=delivery) as temp:
            content = Path(temp) / "content"
            extract_zip(original, content, ArchiveBudget(content, max_bytes=2 * 1024**3))
            content.rename(root)
    issues, valid_files = [], []
    try:
        manifest = json.loads(source_file(root / "manifest.json", root).read_text())
        if not isinstance(manifest, dict) or manifest.get("subject") != "408":
            raise ValueError("408_manifest_required")
    except (OSError, ValueError) as exc:
        manifest = {}
        issues.append({"kind": "manifest", "reason": str(exc)})
    seen = set()
    for row in manifest.get("files", []):
        try:
            path = safe_path(row["path"])
            if path in seen or path == "manifest.json":
                raise ValueError("duplicate_file_binding")
            seen.add(path)
            file = source_file(root / path, root)
            size = row.get("size_bytes", row.get("size"))
            if file.stat().st_size != size or file_hash(file) != row.get("sha256"):
                raise ValueError("file_bytes_mismatch")
            valid_files.append({"path": path, "size": size, "sha256": row["sha256"]})
        except (OSError, ValueError, KeyError, TypeError) as exc:
            issues.append({"kind": "file", "path": row.get("path") if isinstance(row, dict) else None,
                           "reason": str(exc)})
    actual = {p.relative_to(root).as_posix() for p in root.rglob("*") if p.is_file()}
    for path in sorted(actual - seen - {"manifest.json"}):
        issues.append({"kind": "unlisted_file", "path": path, "reason": "preserved_but_not_manifest_verified"})
    report = None
    if manifest.get("web_execution_report"):
        try:
            from web_execution_report_408 import inspect_report
            from study_bridge import Published
            publication, published = Published(BRIDGE_ROOT / "publication").inspect_version("408", manifest.get("source_commit", ""))
            published["_verified_publication"] = {
                "source_commit": manifest.get("source_commit", ""),
                "manifest_sha256": publication["manifest_sha256"],
            }
            report = inspect_report(root, manifest, published)
        except (OSError, ValueError) as exc:
            issues.append({"kind": "execution_report", "reason": str(exc)})
    rows_by_file = {}
    for name in ("selected_items.jsonl", "supplemental_items.jsonl", "substitute_items.jsonl", "web_variants.jsonl", "unresolved.jsonl"):
        path = root / name
        if not path.is_file():
            continue
        values = []
        try:
            lines = path.read_text(encoding="utf-8").splitlines()
        except (OSError, UnicodeError) as exc:
            issues.append({"kind": "row_file", "path": name, "reason": str(exc)})
            continue
        for number, line in enumerate(lines, 1):
            if not line.strip():
                continue
            try:
                row = json.loads(line)
                if not isinstance(row, dict):
                    raise ValueError("row_not_object")
                values.append(row)
            except ValueError as exc:
                issues.append({"kind": "row", "path": name, "line": number, "reason": str(exc)})
        rows_by_file[name] = values
    web_partial = manifest.get("status") in {"PARTIAL", "BLOCKED", "INCOMPLETE"}
    if kind == "morning":
        missing = [name for name in ("due_concepts.json", "selected_items.jsonl", "coverage_report.json") if not (root / name).is_file()]
        partial = web_partial or bool(rows_by_file.get("unresolved.jsonl")) or bool(missing)
    else:
        missing = [name for name in ("enhancement.json", "knowledge_updates.json") if not (root / name).is_file()]
        partial = web_partial or bool(missing)
    for path in missing:
        issues.append({"kind": "missing_stage_material", "path": path, "reason": "local_completion_required"})
    report_pending = isinstance(report, dict) and (report.get("local_completion_required") or report.get("unassigned_local_completion_required"))
    result = {"schema": "cs408-web-return-stage-v1", "kind": kind,
        "status": "LOCAL_COMPLETION_REQUIRED" if partial or issues or report_pending else "PRESERVED_FOR_NATIVE_VALIDATION",
        "source_zip": str(original), "source_zip_sha256": sha, "output_root": str(root),
        "source_commit": manifest.get("source_commit"), "input_version": manifest.get("input_version"),
        "web_status": manifest.get("status"), "verified_success_files": valid_files,
        "preserved_file_count": len(actual), "file_issues": issues, "parsed_unit_records": rows_by_file,
        "execution_report": report, "semantic_completion_inferred": False,
        "next_action": "preserve_successes_complete_only_missing_units_then_native_prepare_apply",
        "formal_write_count": 0, "learner_evidence_write_count": 0, "learning_started": False}
    path = delivery / "stage-receipt.json"
    raw = (json.dumps(result, ensure_ascii=False, indent=2) + "\n").encode()
    with tempfile.NamedTemporaryFile(dir=delivery, prefix=".receipt-", delete=False) as file:
        file.write(raw)
        file.flush()
        os.fsync(file.fileno())
        temp_path = Path(file.name)
    os.replace(temp_path, path)
    return {**result, "receipt_path": str(path)}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--zip", type=Path, required=True)
    parser.add_argument("--kind", choices=["morning", "enhancement"], required=True)
    parser.add_argument("--output-dir", type=Path)
    args = parser.parse_args()
    result = stage_return(args.zip, kind=args.kind, output_dir=args.output_dir)
    print(json.dumps({key: value for key, value in result.items() if key not in {"parsed_unit_records", "verified_success_files"}}, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
