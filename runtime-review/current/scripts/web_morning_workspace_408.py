#!/usr/bin/env python3
"""Strict workspace helper for a web 408 morning-preparation run.

This script never selects or writes questions.  It validates an extracted input
package, initializes a non-destructive return workspace, inventories actual
saved work, and packages that work for continuation.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import sys
import tempfile
from typing import Any, Iterable
import zipfile


INPUT_SCHEMA = "cs408-morning-input-v1"
PROGRESS_SCHEMA = "cs408-D-web-preparation-progress-v1"
CATALOG_SCHEMA = "cs408-web-workspace-input-catalog-v1"
VALIDATION_SCHEMA = "cs408-web-workspace-validation-v1"
CONCEPT_RE = re.compile(r"^(?:DS|CO|OS|CN)\d{2}-\d{2}$")
CORE_FILES = ("manifest.json", "concept-index.json", "batches.json",
              "source-map.json", "progress.json")
ITEM_FILES = ("selected_items.jsonl", "supplemental_items.jsonl", "substitute_items.jsonl",
              "web_variants.jsonl")
CONTROL_NAMES = {
    "manifest.json", "progress.json", "web_execution_report.json",
    "EXECUTION_REPORT.md", "workspace-input-index.json",
    "workspace-validation.json", "due_concepts.json",
}


class WorkspaceError(ValueError):
    def __init__(self, code: str, detail: Any = None):
        super().__init__(code if detail is None else f"{code}: {detail}")
        self.code = code
        self.detail = detail


def require(condition: bool, code: str, detail: Any = None) -> None:
    if not condition:
        raise WorkspaceError(code, detail)


def canonical_bytes(value: Any) -> bytes:
    return (json.dumps(value, ensure_ascii=False, sort_keys=True,
                       separators=(",", ":")) + "\n").encode("utf-8")


def digest_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def safe_relative(value: Any, code: str) -> str:
    require(isinstance(value, str) and value and "\x00" not in value, code)
    path = Path(value)
    require(not path.is_absolute() and value == path.as_posix()
            and all(part not in {"", ".", ".."} for part in path.parts),
            code, value)
    return value


def input_root(path: Path | str) -> Path:
    root = Path(path).expanduser()
    require(root.exists(), "INPUT_ROOT_MISSING", str(root))
    require(root.is_dir() and not root.is_symlink(), "INPUT_ROOT_NOT_DIRECTORY", str(root))
    return root.resolve()


def output_root(path: Path | str) -> Path:
    root = Path(path).expanduser()
    require(root.exists(), "OUTPUT_ROOT_MISSING", str(root))
    require(root.is_dir() and not root.is_symlink(), "OUTPUT_ROOT_NOT_DIRECTORY", str(root))
    return root.resolve()


def read_json(path: Path, expected: type | None = None) -> Any:
    require(path.is_file() and not path.is_symlink(), "JSON_FILE_MISSING", str(path))
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise WorkspaceError("JSON_FILE_INVALID", str(path)) from exc
    if expected is not None:
        require(isinstance(value, expected), "JSON_SCHEMA_TYPE_INVALID",
                {"path": str(path), "expected": expected.__name__,
                 "actual": type(value).__name__})
    return value


def read_jsonl(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    require(path.is_file() and not path.is_symlink(), "JSONL_FILE_INVALID", str(path))
    rows = []
    try:
        for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
            if not line.strip():
                continue
            value = json.loads(line)
            require(isinstance(value, dict), "JSONL_ROW_NOT_OBJECT",
                    {"path": str(path), "line": number})
            rows.append(value)
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise WorkspaceError("JSONL_FILE_INVALID", str(path)) from exc
    return rows


def checked_file(root: Path, relative: str, *, expected_size: int | None = None,
                 expected_sha256: str | None = None, hash_content: bool = True) -> Path:
    relative = safe_relative(relative, "UNSAFE_INPUT_PATH")
    path = root / relative
    require(path.is_file() and not path.is_symlink(), "INPUT_FILE_MISSING", relative)
    info = path.stat()
    if expected_size is not None:
        require(info.st_size == expected_size, "INPUT_FILE_SIZE_MISMATCH", relative)
    if expected_sha256 is not None and hash_content:
        require(digest_bytes(path.read_bytes()) == expected_sha256,
                "INPUT_FILE_HASH_MISMATCH", relative)
    return path


def string_values(value: Any) -> Iterable[str]:
    if isinstance(value, str):
        yield value
    elif isinstance(value, dict):
        for child in value.values():
            yield from string_values(child)
    elif isinstance(value, list):
        for child in value:
            yield from string_values(child)


def validate_input(path: Path | str) -> dict[str, Any]:
    root = input_root(path)
    manifest = read_json(root / "manifest.json", dict)
    require(manifest.get("schema") == INPUT_SCHEMA
            and manifest.get("artifact_kind") == "morning_preparation_input"
            and manifest.get("project") == "D" and manifest.get("subject") == "408",
            "INPUT_MANIFEST_SCHEMA_INVALID")
    source_commit = manifest.get("source_commit")
    input_version = manifest.get("input_version")
    require(isinstance(source_commit, str) and len(source_commit) == 40,
            "INPUT_SOURCE_COMMIT_INVALID")
    require(isinstance(input_version, str) and len(input_version) == 64,
            "INPUT_VERSION_INVALID")

    inventory_rows = manifest.get("files")
    require(isinstance(inventory_rows, list), "INPUT_MANIFEST_FILES_INVALID")
    inventory = {}
    for row in inventory_rows:
        require(isinstance(row, dict), "INPUT_MANIFEST_FILE_INVALID")
        relative = safe_relative(row.get("path"), "INPUT_MANIFEST_PATH_INVALID")
        require(relative not in inventory and isinstance(row.get("size"), int)
                and isinstance(row.get("sha256"), str),
                "INPUT_MANIFEST_FILE_INVALID", relative)
        inventory[relative] = row
    for relative in CORE_FILES[1:]:
        row = inventory.get(relative)
        require(row is not None, "INPUT_CORE_FILE_NOT_IN_MANIFEST", relative)
        checked_file(root, relative, expected_size=row["size"],
                     expected_sha256=row["sha256"])

    concept_index = read_json(root / "concept-index.json", list)
    require(concept_index, "CONCEPT_INDEX_EMPTY")
    concepts: dict[str, dict[str, Any]] = {}
    ordered_targets = []
    for row in concept_index:
        require(isinstance(row, dict), "CONCEPT_INDEX_ROW_INVALID")
        concept_id = row.get("concept_id")
        require(isinstance(concept_id, str) and CONCEPT_RE.fullmatch(concept_id),
                "CONCEPT_ID_INVALID", concept_id)
        require(concept_id not in concepts, "CONCEPT_ID_DUPLICATE", concept_id)
        expected_path = f"concepts/{concept_id}.json"
        require(row.get("path") == expected_path, "CONCEPT_PATH_INVALID", concept_id)
        concepts[concept_id] = {"index": row, "concept_file": expected_path}
        ordered_targets.append(concept_id)

    batches = read_json(root / "batches.json")
    require(isinstance(batches, list), "BATCHES_MUST_BE_LIST",
            type(batches).__name__)
    require(batches, "BATCHES_EMPTY")
    batched_targets: list[str] = []
    batch_catalog = []
    seen_batches = set()
    for row in batches:
        require(isinstance(row, dict), "BATCH_ROW_INVALID")
        batch_id = row.get("batch_id")
        concept_ids = row.get("concept_ids")
        concept_files = row.get("concept_files")
        require(isinstance(batch_id, str) and batch_id and batch_id not in seen_batches,
                "BATCH_ID_INVALID", batch_id)
        require(isinstance(concept_ids, list) and concept_ids
                and all(isinstance(x, str) for x in concept_ids),
                "BATCH_CONCEPT_IDS_INVALID", batch_id)
        require(isinstance(concept_files, list)
                and concept_files == [f"concepts/{x}.json" for x in concept_ids],
                "BATCH_CONCEPT_FILES_INVALID", batch_id)
        seen_batches.add(batch_id)
        batched_targets.extend(concept_ids)
        batch_catalog.append({"batch_id": batch_id,
                              "concept_ids": list(concept_ids),
                              "concept_files": list(concept_files)})
    require(len(batched_targets) == len(set(batched_targets)),
            "BATCH_TARGET_DUPLICATE")
    require(batched_targets == ordered_targets, "BATCH_TARGET_SET_MISMATCH",
            {"batch_count": len(batches), "batched_count": len(batched_targets),
             "concept_index_count": len(ordered_targets)})
    coverage = manifest.get("coverage") or {}
    require(coverage.get("concept_count") == len(ordered_targets)
            and coverage.get("concept_files") == len(ordered_targets),
            "INPUT_COVERAGE_TARGET_MISMATCH")

    source_rows = read_json(root / "source-map.json", list)
    require(source_rows, "SOURCE_MAP_EMPTY")
    by_published: dict[str, list[dict[str, Any]]] = {}
    by_package: dict[str, dict[str, Any]] = {}
    for row in source_rows:
        require(isinstance(row, dict), "SOURCE_MAP_ROW_INVALID")
        package_path = safe_relative(row.get("package_path"), "SOURCE_PACKAGE_PATH_INVALID")
        published_path = safe_relative(row.get("published_path"), "SOURCE_PUBLISHED_PATH_INVALID")
        require(isinstance(row.get("size"), int) and isinstance(row.get("sha256"), str)
                and len(row["sha256"]) == 64, "SOURCE_MAP_ROW_INVALID", package_path)
        require(package_path not in by_package
                or by_package[package_path]["sha256"] == row["sha256"],
                "SOURCE_PACKAGE_COLLISION", package_path)
        checked_file(root, package_path, expected_size=row["size"],
                     expected_sha256=row["sha256"], hash_content=False)
        by_package[package_path] = row
        by_published.setdefault(published_path, []).append(row)

    for concept_id in ordered_targets:
        row = concepts[concept_id]
        concept_path = row["concept_file"]
        inv = inventory.get(concept_path)
        require(inv is not None, "CONCEPT_FILE_NOT_IN_MANIFEST", concept_path)
        body = read_json(checked_file(root, concept_path, expected_size=inv["size"],
                                      expected_sha256=inv["sha256"]), dict)
        require(body.get("concept_id") == concept_id
                and body.get("source_commit") == source_commit,
                "CONCEPT_FILE_IDENTITY_INVALID", concept_id)
        candidate_files = body.get("candidate_files")
        require(isinstance(candidate_files, list)
                and all(isinstance(x, str) for x in candidate_files),
                "CONCEPT_CANDIDATE_FILES_INVALID", concept_id)
        source_matches: dict[str, dict[str, Any]] = {}
        for candidate_path in candidate_files:
            safe_relative(candidate_path, "CANDIDATE_PATH_INVALID")
            require(candidate_path.startswith("candidates/")
                    and candidate_path.endswith(".json"),
                    "CANDIDATE_PATH_INVALID", candidate_path)
            inv = inventory.get(candidate_path)
            require(inv is not None, "CANDIDATE_NOT_IN_MANIFEST", candidate_path)
            candidate = read_json(checked_file(
                root, candidate_path, expected_size=inv["size"],
                expected_sha256=inv["sha256"]), dict)
            for value in string_values(candidate):
                for source in by_published.get(value, []):
                    source_matches[source["package_path"]] = source
                if value in by_package:
                    source_matches[value] = by_package[value]
        for value in string_values(body):
            for source in by_published.get(value, []):
                source_matches[source["package_path"]] = source
            if value in by_package:
                source_matches[value] = by_package[value]
        row.update({
            "candidate_files": list(candidate_files),
            "sources": [source_matches[key] for key in sorted(source_matches)],
        })

    progress = read_json(root / "progress.json", dict)
    require(progress.get("schema") == PROGRESS_SCHEMA
            and progress.get("source_commit") == source_commit
            and progress.get("input_version") == input_version,
            "INPUT_PROGRESS_IDENTITY_INVALID")
    progress_sets = []
    for key in ("completed_concept_ids", "pending_concept_ids",
                "unresolved_concept_ids"):
        values = progress.get(key)
        require(isinstance(values, list) and all(isinstance(x, str) for x in values),
                "INPUT_PROGRESS_LIST_INVALID", key)
        progress_sets.append(values)
    flat_progress = [value for values in progress_sets for value in values]
    require(len(flat_progress) == len(set(flat_progress))
            and set(flat_progress) == set(ordered_targets),
            "INPUT_PROGRESS_TARGET_MISMATCH")

    return {
        "schema": CATALOG_SCHEMA,
        "input_root": str(root),
        "source_commit": source_commit,
        "snapshot_id": manifest.get("snapshot_id"),
        "input_version": input_version,
        "target_date": manifest.get("target_date"),
        "target_count": len(ordered_targets),
        "target_concept_ids": ordered_targets,
        "batches": batch_catalog,
        "concepts": concepts,
        "source_map_count": len(source_rows),
    }


def atomic_write(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix="." + path.name + ".", dir=path.parent)
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def read_unit(input_path: Path | str, concept_id: str) -> dict[str, Any]:
    """Return one real input record and its referenced candidates, without guessing keys."""
    catalog = validate_input(input_path)
    require(concept_id in catalog["concepts"], "CONCEPT_NOT_ASSIGNED", concept_id)
    root = Path(catalog["input_root"])
    concept = read_json(root / catalog["concepts"][concept_id]["concept_file"], dict)
    candidates = []
    for relative in concept.get("candidate_files", []):
        relative = safe_relative(relative, "CANDIDATE_PATH_INVALID")
        candidates.append({"package_path": relative, "record": read_json(root / relative, dict)})
    return {"concept": concept, "candidates": candidates,
            "personal_context_field": "concept.personal_context",
            "personal_original_sources_field": "concept.full_personal_sources",
            "input_root": str(root), "source_commit": catalog["source_commit"]}


def write_text_sections(output_path: Path | str, relative: str, content: Any) -> dict[str, Any]:
    """Write a string or a list of text sections; never concatenate lists to strings."""
    root = output_root(output_path)
    relative = safe_relative(relative, "OUTPUT_ARTIFACT_PATH_INVALID")
    require(Path(relative).suffix in {".md", ".txt"}, "TEXT_ARTIFACT_EXTENSION_REQUIRED")
    if isinstance(content, list):
        require(all(isinstance(section, str) for section in content), "TEXT_SECTIONS_MUST_BE_STRINGS")
        text = "\n\n".join(content)
    else:
        require(isinstance(content, str), "TEXT_OR_STRING_LIST_REQUIRED")
        text = content
    target = root / relative
    require(not any(p.is_symlink() for p in [target, *target.parents]), "OUTPUT_ARTIFACT_SYMLINK")
    data = text.encode("utf-8")
    # Same-byte recovery is safe. A revision must use a new path, keeping prior work.
    if target.exists():
        require(target.read_bytes() == data, "TEXT_ARTIFACT_REVISION_REQUIRES_NEW_PATH", relative)
    else:
        atomic_write(target, data)
    return {"path": relative, "sha256": digest_bytes(data), "size": len(data)}


def initialize(input_path: Path | str, output_path: Path | str) -> dict[str, Any]:
    catalog = validate_input(input_path)
    source = Path(catalog["input_root"])
    output = Path(output_path).expanduser()
    require(not output.exists() or (output.is_dir() and not output.is_symlink()),
            "OUTPUT_ROOT_NOT_DIRECTORY", str(output))
    output.mkdir(parents=True, exist_ok=True)
    output = output.resolve()
    copied = []
    template = source / "return-template"
    require(template.is_dir() and not template.is_symlink(),
            "RETURN_TEMPLATE_MISSING", str(template))
    for path in sorted(template.rglob("*")):
        relative = path.relative_to(template)
        target = output / relative
        if path.is_dir():
            target.mkdir(parents=True, exist_ok=True)
        elif path.is_file() and not path.is_symlink() and not target.exists():
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(path, target)
            copied.append(relative.as_posix())
    progress_path = output / "progress.json"
    if not progress_path.exists():
        shutil.copy2(source / "progress.json", progress_path)
        copied.append("progress.json")
    index_path = output / "workspace-input-index.json"
    index_bytes = canonical_bytes({key: value for key, value in catalog.items()
                                   if key != "input_root"})
    if index_path.exists():
        require(index_path.read_bytes() == index_bytes,
                "WORKSPACE_INPUT_INDEX_MISMATCH")
    else:
        atomic_write(index_path, index_bytes)
        copied.append("workspace-input-index.json")
    return {"status": "WORKSPACE_READY", "output_root": str(output),
            "target_count": catalog["target_count"], "copied_files": copied,
            "preserved_existing_files": len(copied) == 0}


def concept_ids(value: Any) -> set[str]:
    found = set()
    if isinstance(value, dict):
        for key, child in value.items():
            if key in {"concept_id", "unit_id"} and isinstance(child, str):
                found.add(child)
            elif key in {"concept_ids", "review_target_concept_ids",
                         "covered_concept_ids"} and isinstance(child, list):
                found.update(x for x in child if isinstance(x, str))
            else:
                found.update(concept_ids(child))
    elif isinstance(value, list):
        for child in value:
            found.update(concept_ids(child))
    return found


def row_source_references(value: Any) -> list[str]:
    refs = []
    if isinstance(value, dict):
        for key, child in value.items():
            if key in {"published_path", "package_path"} and isinstance(child, str):
                refs.append(child)
            else:
                refs.extend(row_source_references(child))
    elif isinstance(value, list):
        for child in value:
            refs.extend(row_source_references(child))
    return refs


def validate_output(input_path: Path | str, output_path: Path | str) -> dict[str, Any]:
    catalog = validate_input(input_path)
    output = output_root(output_path)
    targets = catalog["target_concept_ids"]
    target_set = set(targets)
    progress = read_json(output / "progress.json", dict)
    require(progress.get("source_commit") == catalog["source_commit"]
            and progress.get("input_version") == catalog["input_version"],
            "OUTPUT_PROGRESS_IDENTITY_INVALID")
    progress_values: dict[str, list[str]] = {}
    for key in ("completed_concept_ids", "pending_concept_ids",
                "unresolved_concept_ids"):
        values = progress.get(key)
        require(isinstance(values, list) and all(isinstance(x, str) for x in values),
                "OUTPUT_PROGRESS_LIST_INVALID", key)
        progress_values[key] = values

    source_map = read_json(Path(catalog["input_root"]) / "source-map.json", list)
    valid_source_paths = {
        row[key] for row in source_map for key in ("published_path", "package_path")
        if isinstance(row, dict) and isinstance(row.get(key), str)
    }
    evidence: dict[str, set[str]] = {concept_id: set() for concept_id in targets}
    out_of_scope = set(value for values in progress_values.values()
                       for value in values if value not in target_set)
    invalid_source_references = []

    for name in ITEM_FILES:
        path = output / name
        for ordinal, row in enumerate(read_jsonl(path), 1):
            row_ids = concept_ids(row)
            out_of_scope.update(row_ids - target_set)
            references = row_source_references(row)
            invalid = sorted(set(references) - valid_source_paths)
            missing_files = []
            for key in ("problem_path", "solution_path", "history_path", "teaching_path"):
                value = row.get(key)
                if isinstance(value, str):
                    try:
                        relative = safe_relative(value, "OUTPUT_ARTIFACT_PATH_INVALID")
                    except WorkspaceError:
                        missing_files.append(value)
                        continue
                    if not (output / relative).is_file():
                        missing_files.append(value)
            if invalid:
                invalid_source_references.append({"file": name, "ordinal": ordinal,
                                                  "references": invalid})
            if not invalid and not missing_files:
                for concept_id in row_ids & target_set:
                    evidence[concept_id].add(name)

    report_path = output / "web_execution_report.json"
    if report_path.exists():
        report = read_json(report_path, dict)
        completed_units = report.get("completed_units")
        if isinstance(completed_units, list):
            for unit in completed_units:
                if not isinstance(unit, dict):
                    continue
                unit_id = unit.get("unit_id")
                if isinstance(unit_id, str) and unit_id not in target_set:
                    out_of_scope.add(unit_id)
                    continue
                files = unit.get("files")
                if not isinstance(unit_id, str) or not isinstance(files, list) or not files:
                    continue
                actual = []
                for value in files:
                    relative = value.get("path") if isinstance(value, dict) else value
                    try:
                        relative = safe_relative(relative, "OUTPUT_ARTIFACT_PATH_INVALID")
                    except WorkspaceError:
                        continue
                    if (output / relative).is_file() and relative not in CONTROL_NAMES:
                        actual.append(relative)
                references = row_source_references(unit.get("source_refs") or [])
                invalid = sorted(set(references) - valid_source_paths)
                if invalid:
                    invalid_source_references.append({"file": "web_execution_report.json",
                                                      "unit_id": unit_id,
                                                      "references": invalid})
                elif actual and unit_id in target_set:
                    evidence[unit_id].update(actual)

    evidenced = [concept_id for concept_id in targets if evidence[concept_id]]
    claimed = set(progress_values["completed_concept_ids"]) & target_set
    remaining = [concept_id for concept_id in targets if concept_id not in evidence
                 or not evidence[concept_id]]
    substantive = []
    for path in sorted(output.rglob("*")):
        if not path.is_file() or path.is_symlink():
            continue
        relative = path.relative_to(output).as_posix()
        if (relative in CONTROL_NAMES or relative.startswith("audit/")
                or relative.endswith(".fields.json")):
            continue
        if path.stat().st_size > 0:
            substantive.append(relative)
    return {
        "schema": VALIDATION_SCHEMA,
        "status": "COMPLETE" if not remaining else "PARTIAL",
        "source_commit": catalog["source_commit"],
        "input_version": catalog["input_version"],
        "target_count": len(targets),
        "target_concept_ids": targets,
        "evidenced_completed_count": len(evidenced),
        "evidenced_completed_concept_ids": evidenced,
        "remaining_count": len(remaining),
        "remaining_concept_ids": remaining,
        "progress_claims_without_evidence": sorted(claimed - set(evidenced)),
        "evidence_not_in_progress": sorted(set(evidenced) - claimed),
        "ignored_out_of_scope_concept_ids": sorted(out_of_scope),
        "invalid_source_references": invalid_source_references,
        "substantive_file_count": len(substantive),
        "substantive_files": substantive,
        "evidence_paths": {key: sorted(value) for key, value in evidence.items() if value},
        "ready_for_learning": False,
    }


def package_workspace(input_path: Path | str, output_path: Path | str,
                      zip_path: Path | str) -> dict[str, Any]:
    initialize(input_path, output_path)
    validation = validate_output(input_path, output_path)
    require(validation["substantive_file_count"] > 0,
            "NO_SAVED_WORK_TO_PACKAGE",
            {"target_count": validation["target_count"],
             "evidenced_completed_count": validation["evidenced_completed_count"]})
    output = output_root(output_path)
    # Assemble inventory from actual output bytes. Previously the helper could
    # emit a ZIP containing only manifest.fields.json, which native import rejects.
    template = read_json(input_root(input_path) / "return-template/manifest.fields.json", dict)
    current_manifest = output / "manifest.json"
    manifest = read_json(current_manifest, dict) if current_manifest.exists() else dict(template)
    for key, value in template.items():
        if key in {"files", "status", "run_id"}:
            continue
        require(key not in manifest or manifest[key] == value, "OUTPUT_MANIFEST_BINDING_DRIFT", key)
        manifest[key] = value
    if not manifest.get("run_id"):
        manifest["run_id"] = "D408-WEB-" + validation["input_version"][:24]
    complete = validation["remaining_count"] == 0 and not validation["invalid_source_references"]
    coverage_path = output / "coverage_report.json"
    coverage = read_json(coverage_path, dict) if coverage_path.exists() else {}
    complete = complete and set(coverage.get("covered_concept_ids", [])) == set(validation["target_concept_ids"]) \
        and not coverage.get("unresolved_concept_ids")
    # Stage evidence can be useful without constituting complete question units.
    for name in ("selected_items.jsonl", "supplemental_items.jsonl", "substitute_items.jsonl"):
        for row in read_jsonl(output / name):
            for key in ("problem_path", "solution_path", "history_path", "teaching_path"):
                relative = row.get(key)
                if not isinstance(relative, str):
                    complete = False
                else:
                    relative = safe_relative(relative, "OUTPUT_ARTIFACT_PATH_INVALID")
                    if not (output / relative).is_file() or not (output / relative).stat().st_size:
                        complete = False
    manifest["status"] = "WEB_PREPARED" if complete and manifest.get("status") == "WEB_PREPARED" else "PARTIAL"
    atomic_write(output / "workspace-validation.json", canonical_bytes(validation))
    prior_roles = {r["path"]: r.get("role", "support") for r in manifest.get("files", [])}
    manifest["files"] = []
    for path in sorted(output.rglob("*")):
        if path.is_file() and not path.is_symlink() and path != current_manifest:
            relative = path.relative_to(output).as_posix()
            data = path.read_bytes()
            manifest["files"].append({"path": relative, "sha256": digest_bytes(data), "size": len(data),
                "role": "web_execution_report" if relative == "web_execution_report.json" else prior_roles.get(relative, "support")})
    atomic_write(current_manifest, canonical_bytes(manifest))
    destination = Path(zip_path).expanduser()
    require(not destination.exists(), "PACKAGE_ALREADY_EXISTS", str(destination))
    destination.parent.mkdir(parents=True, exist_ok=True)
    require(not destination.parent.is_symlink(), "PACKAGE_PARENT_SYMLINK", str(destination.parent))
    fd, temporary = tempfile.mkstemp(prefix="." + destination.name + ".",
                                     dir=destination.parent)
    os.close(fd)
    try:
        with zipfile.ZipFile(temporary, "w", compression=zipfile.ZIP_DEFLATED) as archive:
            for path in sorted(output.rglob("*")):
                if path.is_file() and not path.is_symlink():
                    relative = path.relative_to(output).as_posix()
                    archive.write(path, relative)
        os.replace(temporary, destination)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)
    return {
        "status": "PACKAGED_" + validation["status"],
        "zip_path": str(destination.resolve()),
        "zip_bytes": destination.stat().st_size,
        "zip_sha256": digest_bytes(destination.read_bytes()),
        "target_count": validation["target_count"],
        "evidenced_completed_count": validation["evidenced_completed_count"],
        "remaining_count": validation["remaining_count"],
        "ready_for_learning": False,
    }


def parser() -> argparse.ArgumentParser:
    value = argparse.ArgumentParser(description="Validate and preserve a 408 web morning workspace")
    sub = value.add_subparsers(dest="command", required=True)
    for name in ("init", "validate"):
        current = sub.add_parser(name)
        current.add_argument("--input-root", required=True)
        current.add_argument("--output-root", required=True)
    current = sub.add_parser("package")
    current.add_argument("--input-root", required=True)
    current.add_argument("--output-root", required=True)
    current.add_argument("--zip", required=True)
    current = sub.add_parser("read-unit")
    current.add_argument("--input-root", required=True)
    current.add_argument("--concept-id", required=True)
    current = sub.add_parser("write-text")
    current.add_argument("--output-root", required=True)
    current.add_argument("--path", required=True)
    current.add_argument("--content-json", required=True)
    return value


def main(argv: list[str] | None = None) -> int:
    args = parser().parse_args(argv)
    try:
        if args.command == "init":
            result = initialize(args.input_root, args.output_root)
        elif args.command == "validate":
            result = validate_output(args.input_root, args.output_root)
        elif args.command == "read-unit":
            result = read_unit(args.input_root, args.concept_id)
        elif args.command == "write-text":
            result = write_text_sections(args.output_root, args.path,
                json.loads(Path(args.content_json).read_text(encoding="utf-8")))
        else:
            result = package_workspace(args.input_root, args.output_root, args.zip)
    except WorkspaceError as exc:
        sys.stderr.buffer.write(canonical_bytes({"status": "error", "error": exc.code,
                                                 "detail": exc.detail}))
        return 2
    sys.stdout.buffer.write(canonical_bytes(result))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
