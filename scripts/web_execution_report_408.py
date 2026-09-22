"""Validate a web execution report without executing any reported command."""
from __future__ import annotations

import hashlib
import json
from pathlib import Path, PurePosixPath
import re
from typing import Any


REPORT_SCHEMA = "cs408-web-execution-report-v1"
INSPECTION_SCHEMA = "cs408-web-execution-report-inspection-v1"
SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
SHA1_RE = re.compile(r"^[0-9a-f]{40}$")
SAFE_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,191}$")
MAX_REPORT_BYTES = 8 * 1024 * 1024
MAX_UNITS = 4096
MAX_FAILURES = 4096


class WebExecutionReportError(ValueError):
    pass


def _require(ok: Any, reason: str) -> None:
    if not ok:
        raise WebExecutionReportError(reason)


def _encoded(value: Any) -> bytes:
    return (json.dumps(value, ensure_ascii=False, sort_keys=True,
                       separators=(",", ":")) + "\n").encode("utf-8")


def _digest(value: Any) -> str:
    return hashlib.sha256(_encoded(value)).hexdigest()


def _sha_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _relative(value: Any) -> str:
    _require(isinstance(value, str) and value, "web_report_relative_path_required")
    path = PurePosixPath(value)
    _require(
        not path.is_absolute()
        and path.as_posix() == value
        and all(part not in {"", ".", ".."} for part in path.parts),
        "web_report_relative_path_invalid",
    )
    return value


def _manifest_files(root: Path, manifest: dict[str, Any]) -> dict[str, dict[str, Any]]:
    rows = manifest.get("files")
    _require(isinstance(rows, list), "web_report_manifest_files_invalid")
    result = {}
    for row in rows:
        _require(
            isinstance(row, dict)
            and isinstance(row.get("path"), str)
            and SHA256_RE.fullmatch(str(row.get("sha256") or "")),
            "web_report_manifest_file_binding_invalid",
        )
        relative = _relative(row["path"])
        _require(relative not in result, "web_report_manifest_file_duplicate")
        path = root / relative
        _require(
            path.is_file()
            and not path.is_symlink()
            and _sha_file(path) == row["sha256"],
            "web_report_manifest_file_drift:" + relative,
        )
        result[relative] = row
    return result


def _source_bindings(
    manifest: dict[str, Any], files: dict[str, dict[str, Any]],
    published: dict[str, Any] | None,
) -> dict[str, str]:
    bindings = {path: str(row["sha256"]) for path, row in files.items()}
    run_id = str(manifest.get("run_id") or "")
    for path, digest in list(bindings.items()):
        bindings[f"enhancement-package://{run_id}/{path}"] = digest
    for row in manifest.get("captures") or []:
        if not isinstance(row, dict):
            continue
        capture_id = str(row.get("capture_id") or "")
        payload_sha = str(row.get("capture_payload_sha256") or "")
        locator = str(row.get("evidence_locator") or "")
        evidence_sha = str(row.get("evidence_sha256") or "")
        if capture_id and SHA256_RE.fullmatch(payload_sha):
            bindings[f"capture://{capture_id}"] = payload_sha
        if locator and SHA256_RE.fullmatch(evidence_sha):
            bindings[locator] = evidence_sha
    if not isinstance(published, dict):
        return bindings
    source_commit = str(manifest.get("source_commit") or "")
    # This metadata is supplied by the trusted publication reader, not by the
    # web return. Expanded inventories are not the original manifest bytes.
    publication = published.get("_verified_publication") or {}
    if (isinstance(publication, dict)
            and publication.get("source_commit") == source_commit
            and SHA256_RE.fullmatch(str(publication.get("manifest_sha256") or ""))):
        bindings[f"study-library://{source_commit}/PUBLISHED_LIBRARY.json"] = publication["manifest_sha256"]
        bindings.setdefault("PUBLISHED_LIBRARY.json", publication["manifest_sha256"])
    for row in published.get("files") or []:
        if (
            isinstance(row, dict)
            and isinstance(row.get("path"), str)
            and SHA256_RE.fullmatch(str(row.get("sha256") or ""))
        ):
            bindings[f"study-library://{source_commit}/{row['path']}"] = row["sha256"]
            # D's existing source_refs use published relative paths. Preserve
            # the qualified URI when a package member would be ambiguous.
            bindings.setdefault(row["path"], row["sha256"])
    for row in published.get("original_mappings") or []:
        if isinstance(row, dict) and isinstance(row.get("question_id"), str):
            bindings[
                f"study-library://{source_commit}/original-mapping/{row['question_id']}"
            ] = _digest(row)
    for formal_id, row in (published.get("question_refs") or {}).items():
        if isinstance(formal_id, str) and isinstance(row, dict):
            bindings[
                f"study-library://{source_commit}/question-ref/{formal_id}"
            ] = _digest(row)
    return bindings


def inspect_report(
    root: Path, manifest: dict[str, Any], published: dict[str, Any] | None = None
) -> dict[str, Any]:
    """Read and verify one optional report; never execute its tool records."""

    package_root = Path(root).expanduser().resolve()
    _require(package_root.is_dir() and not package_root.is_symlink(),
             "web_report_package_root_invalid")
    marker = manifest.get("web_execution_report")
    if marker is None:
        return {
            "schema": INSPECTION_SCHEMA,
            "status": "report_not_provided",
            "report_path": None,
            "report_sha256": None,
            "source_commit": manifest.get("source_commit"),
            "completed_unit_count": 0,
            "reported_completed_unit_count": 0,
            "completed_unit_ids": [],
            "completed_units": [],
            "blocked_units": [],
            "blocked_unit_ids": [],
            "successful_file_count": 0,
            "pending_unit_ids": [],
            "failure_count": 0,
            "verified_failure_count": 0,
            "failures": [],
            "unverified_failure_count": 0,
            "unverified_failures": [],
            "local_completion_required": [],
            "unassigned_local_completion_required": False,
            "maintenance_pending": [],
            "commands_executed": 0,
            "write_count": 0,
        }
    relative = _relative(marker)
    files = _manifest_files(package_root, manifest)
    _require(relative in files, "web_report_not_listed_in_manifest")
    _require(files[relative].get("role") == "web_execution_report",
             "web_report_manifest_role_invalid")
    report_path = package_root / relative
    _require(report_path.stat().st_size <= MAX_REPORT_BYTES,
             "web_report_too_large")
    try:
        report = json.loads(report_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise WebExecutionReportError(f"web_report_invalid_json:{exc}") from exc
    _require(
        isinstance(report, dict)
        and set(report) == {
            "schema", "source_commit", "completed_units", "pending_unit_ids",
            "failures",
        }
        and report.get("schema") == REPORT_SCHEMA,
        "web_report_schema_or_fields_invalid",
    )
    source_commit = str(report.get("source_commit") or "")
    _require(
        SHA1_RE.fullmatch(source_commit)
        and source_commit == manifest.get("source_commit"),
        "web_report_source_commit_drift",
    )
    if published is not None:
        _require(isinstance(published, dict) and published.get("subject") == "408",
                 "web_report_published_subject_invalid")
        if manifest.get("local_version") is not None:
            _require(
                published.get("local_version") == manifest.get("local_version"),
                "web_report_published_local_version_drift",
            )
    bindings = _source_bindings(manifest, files, published)
    completed = report.get("completed_units")
    pending = report.get("pending_unit_ids")
    failures = report.get("failures")
    _require(isinstance(completed, list) and len(completed) <= MAX_UNITS,
             "web_report_completed_units_invalid")
    _require(isinstance(pending, list) and len(pending) <= MAX_UNITS,
             "web_report_pending_units_invalid")
    _require(isinstance(failures, list) and len(failures) <= MAX_FAILURES,
             "web_report_failures_invalid")
    completed_ids = []
    normalized_completed = []
    blocked_units = []
    for index, unit in enumerate(completed, 1):
        raw_unit_id = unit.get("unit_id") if isinstance(unit, dict) else None
        unit_id = str(raw_unit_id or "")
        try:
            _require(
                isinstance(unit, dict)
                and set(unit) == {"unit_id", "files", "source_refs"},
                "web_report_completed_unit_fields_invalid",
            )
            _require(
                SAFE_ID_RE.fullmatch(unit_id) and unit_id not in completed_ids,
                "web_report_completed_unit_id_invalid",
            )
            unit_files = unit.get("files")
            refs = unit.get("source_refs")
            _require(isinstance(unit_files, list) and unit_files,
                     "web_report_success_files_required")
            _require(isinstance(refs, list) and refs,
                     "web_report_success_source_refs_required")
            normalized_files = []
            for value in unit_files:
                path = _relative(value)
                _require(path in files,
                         "web_report_success_file_not_manifest_bound:" + path)
                normalized_files.append(path)
            _require(len(normalized_files) == len(set(normalized_files)),
                     "web_report_success_file_duplicate")
            normalized_refs = []
            for ref in refs:
                _require(
                    isinstance(ref, dict)
                    and set(ref) == {"path", "sha256"}
                    and isinstance(ref.get("path"), str)
                    and SHA256_RE.fullmatch(str(ref.get("sha256") or "")),
                    "web_report_source_ref_invalid",
                )
                _require(bindings.get(ref["path"]) == ref["sha256"],
                         "web_report_source_ref_unverified:" + ref["path"])
                _require(
                    ref["path"] not in {
                        relative,
                        f"enhancement-package://{manifest.get('run_id')}/{relative}",
                    },
                    "web_report_cannot_cite_itself_as_source",
                )
                normalized_refs.append(dict(ref))
            completed_ids.append(unit_id)
            normalized_completed.append({
                "unit_id": unit_id,
                "files": normalized_files,
                "source_refs": normalized_refs,
            })
        except WebExecutionReportError as exc:
            blocked_units.append({
                "inspection_sequence": index,
                "unit_id": unit_id if SAFE_ID_RE.fullmatch(unit_id) else None,
                "reason": str(exc),
            })
    normalized_pending = []
    invalid_pending = []
    for index, unit_id in enumerate(pending, 1):
        if (
            isinstance(unit_id, str)
            and SAFE_ID_RE.fullmatch(unit_id)
            and unit_id not in normalized_pending
        ):
            normalized_pending.append(unit_id)
        else:
            invalid_pending.append({
                "inspection_sequence": index,
                "reported_unit_id": unit_id,
                "reason": "web_report_pending_unit_id_invalid",
            })
    overlap = set(completed_ids) & set(normalized_pending)
    if overlap:
        blocked_units.extend({
            "inspection_sequence": None,
            "unit_id": unit_id,
            "reason": "web_report_completed_pending_overlap",
        } for unit_id in sorted(overlap))
        normalized_completed = [
            row for row in normalized_completed if row["unit_id"] not in overlap
        ]
        completed_ids = [unit_id for unit_id in completed_ids if unit_id not in overlap]
    normalized_failures = []
    unverified_failures = []
    sequences = set()
    unresolved_units = set()
    for index, failure in enumerate(failures, 1):
        try:
            _require(
                isinstance(failure, dict)
                and set(failure) == {
                    "sequence", "call_id", "tool", "input",
                    "raw_error_or_response", "affected_unit_ids",
                    "recovery_status",
                },
                "web_report_failure_fields_invalid",
            )
            sequence = failure.get("sequence")
            call_id = failure.get("call_id")
            tool = failure.get("tool")
            raw = failure.get("raw_error_or_response")
            affected = failure.get("affected_unit_ids")
            recovery = failure.get("recovery_status")
            _require(
                isinstance(sequence, int) and not isinstance(sequence, bool)
                and sequence > 0 and sequence not in sequences,
                "web_report_failure_sequence_invalid",
            )
            _require(
                call_id is None
                or (isinstance(call_id, str) and SAFE_ID_RE.fullmatch(call_id)),
                "web_report_failure_call_id_invalid",
            )
            _require(isinstance(tool, str) and tool.strip(),
                     "web_report_failure_tool_required")
            _require(isinstance(failure.get("input"), dict),
                     "web_report_failure_input_object_required")
            _require(
                raw is not None and raw != ""
                and len(_encoded(raw)) <= MAX_REPORT_BYTES,
                "web_report_failure_raw_return_required",
            )
            _require(
                isinstance(affected, list)
                and all(
                    isinstance(value, str) and SAFE_ID_RE.fullmatch(value)
                    for value in affected
                )
                and len(affected) == len(set(affected)),
                "web_report_failure_affected_units_invalid",
            )
            _require(recovery in {"unresolved", "recovered"},
                     "web_report_failure_recovery_status_invalid")
            if recovery == "unresolved":
                unresolved_units.update(affected)
            sequences.add(sequence)
            normalized_failures.append(dict(failure))
        except WebExecutionReportError as exc:
            affected = failure.get("affected_unit_ids") if isinstance(failure, dict) else []
            safe_affected = [
                value for value in affected
                if isinstance(value, str) and SAFE_ID_RE.fullmatch(value)
            ] if isinstance(affected, list) else []
            unresolved_units.update(safe_affected)
            unverified_failures.append({
                "inspection_sequence": index,
                "reason": str(exc),
                "reported_record": failure,
                "affected_unit_ids": safe_affected,
                "failure_located": False,
            })
    sequence_order_invalid = [
        row["sequence"] for row in normalized_failures
    ] != sorted(row["sequence"] for row in normalized_failures)
    local_completion_required = set(normalized_pending) | unresolved_units
    local_completion_required.update(
        row["unit_id"] for row in blocked_units if row.get("unit_id")
    )
    unassigned_local_completion = bool(
        invalid_pending
        or any(row.get("unit_id") is None for row in blocked_units)
    )
    maintenance_pending = [
        {"kind": "unresolved_web_call", **row}
        for row in normalized_failures if row["recovery_status"] == "unresolved"
    ]
    maintenance_pending.extend(
        {"kind": "untrusted_completed_unit", **row} for row in blocked_units
    )
    maintenance_pending.extend(
        {"kind": "unverified_failure_record", **row} for row in unverified_failures
    )
    maintenance_pending.extend(
        {"kind": "invalid_pending_unit", **row} for row in invalid_pending
    )
    if sequence_order_invalid:
        maintenance_pending.append({
            "kind": "failure_sequence_order",
            "reason": "web_report_failure_sequence_order_invalid",
        })
    successful_files = {
        path for row in normalized_completed for path in row["files"]
    }
    return {
        "schema": INSPECTION_SCHEMA,
        "status": "READ_MAINTENANCE_PENDING" if maintenance_pending else "verified",
        "report_path": str(report_path),
        "report_sha256": files[relative]["sha256"],
        "source_commit": source_commit,
        "reported_completed_unit_count": len(completed),
        "completed_unit_count": len(completed_ids),
        "completed_unit_ids": completed_ids,
        "completed_units": normalized_completed,
        "blocked_units": blocked_units,
        "blocked_unit_ids": sorted({
            row["unit_id"] for row in blocked_units if row.get("unit_id")
        }),
        "successful_file_count": len(successful_files),
        "pending_unit_ids": normalized_pending,
        "failure_count": len(failures),
        "verified_failure_count": len(normalized_failures),
        "failures": normalized_failures,
        "unverified_failure_count": len(unverified_failures),
        "unverified_failures": unverified_failures,
        "local_completion_required": sorted(local_completion_required),
        "unassigned_local_completion_required": unassigned_local_completion,
        "maintenance_pending": maintenance_pending,
        "commands_executed": 0,
        "write_count": 0,
    }
