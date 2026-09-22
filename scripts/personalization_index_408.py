#!/usr/bin/env python3
"""Build answer-safe, replayable personalization indexes for the 408 study loop.

The builder reads the compact knowledge-point catalogue, formal-node index,
optional review-unit mapping, canonical review event ledger, and an optional
details-bridge inventory.  When the bridge exists it hashes question-only asset
files and emits small source manifests; it never copies question pixels, card
text, options, answers, explanations, solution assets, or handwriting into an
index.

All generated files are disposable projections.  ``build`` recreates them from
the sources; ``query`` only reads an existing projection and never rebuilds or
touches a source of truth.
"""

from __future__ import annotations

import argparse
import fcntl
import hashlib
import json
import os
import re
import sys
import tempfile
import unicodedata
from collections import defaultdict
from contextlib import contextmanager
from datetime import date, datetime
from pathlib import Path
from typing import Any, Iterable, Iterator
from zoneinfo import ZoneInfo

from cross_subject_candidates_408 import (
    ShadowCatalogError,
    validate_projection as validate_cross_projection,
)
from cross_variant_pipeline_408 import (
    CATALOG_FILENAME as CROSS_VARIANT_CATALOG_FILENAME,
    CrossVariantPipelineError,
    rebuild_cross_variant_catalog,
    validate_cross_variant_catalog,
)
from linked_practice_source_408 import QUESTION_ASSET_RE, SOLUTION_ASSET_RE
from personalization_commonality_408 import build_recent_signature_index
from question_source_attestation_408 import (
    SCHEMA as QUESTION_ISOLATION_ATTESTATION_SCHEMA,
    QuestionSourceAttestationError,
    asset_set_sha256,
    validate_attestation_chain,
)
from variant_source_pipeline_408 import (
    CATALOG_FILENAME as VARIANT_CATALOG_FILENAME,
    VariantPipelineError,
    rebuild_variant_catalog,
)


SCHEMA = "personalization_index_v1"
EXPECTED_POINT_COUNT = 629
SUBJECT_NAMES = {
    "DS": "数据结构",
    "CO": "计算机组成原理",
    "OS": "操作系统",
    "CN": "计算机网络",
}
POINT_RE = re.compile(r"\b((?:DS|CO|OS|CN)\d{2}-\d{2})\b", re.IGNORECASE)
MODULE_RE = re.compile(r"^##\s+((?:DS|CO|OS|CN)\d{2})\s+(.+?)\s*$")
POINT_LINE_RE = re.compile(
    r"^-\s+((?:DS|CO|OS|CN)\d{2}-\d{2})\s+(.+?)\s*$"
)
NODE_RE = re.compile(r"^(?:DS|CO|OS|CN)_(?:\d{4}|UNK)_\d{3}$")
REVIEW_UNIT_RE = re.compile(r"^RU_[A-Z0-9_-]+$", re.IGNORECASE)
ISO_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
OFFICIAL_SOURCE_RE = re.compile(r"^EXAM408-\d{4}-Q\d+$", re.IGNORECASE)
OFFICIAL_SOURCE_SEARCH_RE = re.compile(r"EXAM408-\d{4}-Q\d+", re.IGNORECASE)
OBSIDIAN_URI_RE = re.compile(r"obsidian://[^\s；;|]+", re.IGNORECASE)
PERFORMANCE_EVENT_KINDS = {"outcome", "repair"}
FAILURE_RESULTS = {"partial", "wrong", "uncertain"}
DEFAULT_OUTPUT_RELATIVE = Path(
    "wiki/study_vaults/408-full/state/personalization"
)
DEFAULT_EVENTS_RELATIVE = Path(
    "wiki/study_vaults/408-full/state/review-loop/events.jsonl"
)
MORNING_STATE_RELATIVE = Path(
    "wiki/study_vaults/408-full/state/morning-review"
)
MAX_RECENT_EVENT_IDS_PER_ENTITY = 5
MAX_RECENT_UNRESOLVED_EVENT_IDS = 64
DEFAULT_BRIDGE_SNAPSHOT = Path.home() / ".codex/kaoyan-408-obsidian-bridge/records.json"
CROSS_PROJECTION_FILENAME = "cross-subject-shadow.json"
FAILURE_SIGNATURE_FILENAME = "failure_signatures.json"
CORPUS_MANIFEST_RELATIVE = Path("corpus/manifest.json")
QUESTION_SOURCE_SCHEMA = "question_source_manifest_v1"
PERSONALIZATION_SOURCE_PREFIX = "personalization-source:"


class PersonalizationIndexError(RuntimeError):
    """Raised when a source cannot safely or deterministically be compiled."""


def _retired_write_surface() -> None:
    raise PersonalizationIndexError(
        "personalization_index_writes_retired_read_only_compatibility"
    )


def canonical_json_bytes(value: Any) -> bytes:
    return (
        json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        + "\n"
    ).encode("utf-8")


def output_json_bytes(value: Any) -> bytes:
    return json.dumps(
        value, ensure_ascii=False, sort_keys=True, indent=2
    ).encode("utf-8") + b"\n"


def sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def sha256_file(path: Path) -> str:
    return sha256_bytes(path.read_bytes())


def _normal_text(value: Any) -> str:
    text = unicodedata.normalize("NFKC", str(value or "")).upper()
    return "".join(ch for ch in text if ch.isalnum())


def _valid_date(value: Any) -> str | None:
    text = str(value or "").strip()
    if not ISO_DATE_RE.fullmatch(text):
        return None
    try:
        date.fromisoformat(text)
    except ValueError:
        return None
    return text


def _max_date(*values: str | None) -> str | None:
    valid = [value for value in values if _valid_date(value)]
    return max(valid) if valid else None


def _days_between(older: str | None, newer: str) -> int | None:
    if not older:
        return None
    return (date.fromisoformat(newer) - date.fromisoformat(older)).days


def _split_cells(line: str) -> list[str]:
    return [cell.strip() for cell in line.strip().strip("|").split("|")]


def read_markdown_table(path: Path) -> list[dict[str, str]]:
    """Read the first Markdown table without carrying prose into the result."""

    header: list[str] | None = None
    rows: list[dict[str, str]] = []
    for line_number, line in enumerate(
        path.read_text(encoding="utf-8").splitlines(), start=1
    ):
        if not line.strip().startswith("|"):
            continue
        cells = _split_cells(line)
        if cells and all(re.fullmatch(r":?-{3,}:?", cell) for cell in cells):
            continue
        if header is None:
            header = cells
            continue
        if len(cells) != len(header):
            raise PersonalizationIndexError(
                f"malformed Markdown table row at {path}:{line_number}: "
                f"expected {len(header)} cells, got {len(cells)}"
            )
        rows.append(dict(zip(header, cells)))
    if header is None:
        raise PersonalizationIndexError(f"no Markdown table found: {path}")
    return rows


def compile_point_catalog(
    path: Path, expected_count: int | None = None
) -> dict[str, Any]:
    """Compile the human-maintained point list into a deterministic catalogue."""

    modules: dict[str, dict[str, str]] = {}
    points: dict[str, dict[str, str]] = {}
    name_index: dict[str, list[str]] = defaultdict(list)

    for line_number, line in enumerate(
        path.read_text(encoding="utf-8").splitlines(), start=1
    ):
        module_match = MODULE_RE.match(line.strip())
        if module_match:
            module_id, module_name = module_match.groups()
            subject = module_id[:2]
            modules[module_id] = {
                "module_id": module_id,
                "module_name": module_name.strip(),
                "subject": subject,
                "subject_name": SUBJECT_NAMES[subject],
            }
            continue

        point_match = POINT_LINE_RE.match(line.strip())
        if not point_match:
            continue
        point_id, point_name = point_match.groups()
        module_id = point_id[:4]
        # The explanatory examples before the real subject sections are not
        # catalogue entries because their module has not been declared yet.
        if module_id not in modules:
            continue
        if point_id in points:
            raise PersonalizationIndexError(
                f"duplicate knowledge point {point_id} at {path}:{line_number}"
            )
        subject = point_id[:2]
        point = {
            "point_id": point_id,
            "name": point_name.strip(),
            "label": f"{point_id} {point_name.strip()}",
            "subject": subject,
            "subject_name": SUBJECT_NAMES[subject],
            "module_id": module_id,
            "module_name": modules[module_id]["module_name"],
        }
        points[point_id] = point
        name_index[_normal_text(point_name)].append(point_id)
        name_index[_normal_text(point["label"])].append(point_id)

    if expected_count is not None and len(points) != expected_count:
        raise PersonalizationIndexError(
            f"knowledge-point count mismatch: expected {expected_count}, got {len(points)}"
        )
    if not points:
        raise PersonalizationIndexError(f"no knowledge points compiled from {path}")

    subjects: dict[str, dict[str, Any]] = {}
    for subject, subject_name in SUBJECT_NAMES.items():
        subjects[subject] = {
            "subject": subject,
            "subject_name": subject_name,
            "module_ids": sorted(
                module_id
                for module_id, item in modules.items()
                if item["subject"] == subject
            ),
            "point_count": sum(
                1 for point in points.values() if point["subject"] == subject
            ),
        }

    return {
        "schema": SCHEMA,
        "kind": "static_point_catalog",
        "point_count": len(points),
        "module_count": len(modules),
        "subjects": subjects,
        "modules": dict(sorted(modules.items())),
        "points": dict(sorted(points.items())),
        "name_index": {
            key: sorted(set(value)) for key, value in sorted(name_index.items())
        },
    }


def resolve_point_ids(value: Any, catalog: dict[str, Any]) -> list[str]:
    """Resolve an event label conservatively; ambiguity returns no mapping."""

    text = str(value or "").strip()
    if not text:
        return []
    points: dict[str, Any] = catalog["points"]
    direct = {
        match.upper()
        for match in POINT_RE.findall(text)
        if match.upper() in points
    }
    if direct:
        return sorted(direct)

    normalized = _normal_text(text)
    exact = catalog["name_index"].get(normalized, [])
    if len(exact) == 1:
        return list(exact)

    substring_candidates: set[str] = set()
    for point_id, point in points.items():
        name = _normal_text(point["name"])
        if not name:
            continue
        # Four normalized characters is a conservative Chinese boundary.  A
        # short all-ASCII acronym such as CPI/GBN/SR is allowed only when unique.
        ascii_name = bool(re.fullmatch(r"[A-Z0-9]+", name))
        if (len(name) >= 4 or (ascii_name and len(name) >= 2)) and (
            name in normalized or normalized in name
        ):
            substring_candidates.add(point_id)

    for acronym in re.findall(r"[A-Za-z][A-Za-z0-9]{1,}", text):
        token = _normal_text(acronym)
        token_hits = {
            point_id
            for point_id, point in points.items()
            if re.search(
                rf"(?<![A-Z0-9]){re.escape(token)}(?![A-Z0-9])",
                unicodedata.normalize("NFKC", point["name"]).upper(),
            )
        }
        if len(token_hits) == 1:
            substring_candidates.update(token_hits)

    return sorted(substring_candidates) if len(substring_candidates) == 1 else []


def _point_ids_from_cell(value: Any, catalog: dict[str, Any]) -> list[str]:
    direct = {
        match.upper()
        for match in POINT_RE.findall(str(value or ""))
        if match.upper() in catalog["points"]
    }
    if direct:
        return sorted(direct)
    return resolve_point_ids(value, catalog)


def _safe_reliable_source(row: dict[str, str]) -> dict[str, Any]:
    """Return source identity and a URI only; never copy detail prose."""

    source_id = row.get("来源ID", "").strip() or None
    details = row.get("详情入口", "")
    uri_match = OBSIDIAN_URI_RE.search(details)
    detail_uri = uri_match.group(0) if uri_match else None
    canonical_match = OFFICIAL_SOURCE_SEARCH_RE.search(
        " ".join(value for value in (source_id, details) if value)
    )
    canonical_exam_id = canonical_match.group(0).upper() if canonical_match else None
    official = canonical_exam_id is not None

    if official:
        status = "verified_exam_locator"
        eligible_original = True
    elif detail_uri:
        status = "detail_locator_only"
        eligible_original = False
    else:
        status = "unverified_or_missing"
        eligible_original = False
    if canonical_exam_id:
        source_ref = f"exam-corpus:408统考真题/cards/{canonical_exam_id}.md"
        source_ref_kind = "canonical_exam_card"
    elif detail_uri:
        source_ref = detail_uri
        source_ref_kind = "obsidian_detail"
    elif source_id:
        source_ref = f"source-id:{source_id}"
        source_ref_kind = "source_id_only"
    else:
        source_ref = None
        source_ref_kind = "missing"
    return {
        "status": status,
        "source_id": source_id,
        "canonical_exam_id": canonical_exam_id,
        "detail_uri": detail_uri,
        "source_ref": source_ref,
        "source_ref_kind": source_ref_kind,
        "reference_fingerprint": sha256_bytes((source_ref or "").encode("utf-8")),
        "original_question_eligible": eligible_original,
    }


def load_formal_nodes(
    path: Path, catalog: dict[str, Any]
) -> dict[str, dict[str, Any]]:
    nodes: dict[str, dict[str, Any]] = {}
    for row in read_markdown_table(path):
        node_id = row.get("ID", "").strip()
        if not NODE_RE.fullmatch(node_id):
            continue
        if node_id in nodes:
            raise PersonalizationIndexError(f"duplicate formal node: {node_id}")
        primary = _point_ids_from_cell(row.get("主知识点"), catalog)
        all_points: set[str] = set(primary)
        all_points.update(_point_ids_from_cell(row.get("副知识点"), catalog))
        all_points.update(_point_ids_from_cell(row.get("命中知识点"), catalog))
        subject = node_id[:2]
        nodes[node_id] = {
            "node_id": node_id,
            "subject": subject,
            "subject_name": SUBJECT_NAMES[subject],
            "primary_point_ids": sorted(primary),
            "point_ids": sorted(all_points),
            "formal_last_review_date": _valid_date(row.get("最近复做日期")),
            "reliable_source": _safe_reliable_source(row),
        }
    if not nodes:
        raise PersonalizationIndexError(f"no formal nodes loaded from {path}")
    return dict(sorted(nodes.items()))


def _inside(path: Path, root: Path) -> bool:
    try:
        path.resolve().relative_to(root.resolve())
        return True
    except ValueError:
        return False


def enrich_question_sources(
    *,
    repo: Path,
    output_dir: Path,
    nodes: dict[str, dict[str, Any]],
    bridge_path: Path | None = None,
) -> dict[str, Any]:
    """Bind formal nodes to answer-isolated question-asset manifests.

    The bridge is optional so fixtures and portable clones remain buildable.
    A node becomes original-question eligible only when every declared question
    asset exists, no solution asset is admitted, and the emitted manifest is
    content-addressed.  The hot path later reads only the manifest locator and
    hash from ``question_index.json``.
    """

    repo = repo.resolve()
    output_dir = output_dir.resolve()
    bridge = (bridge_path or DEFAULT_BRIDGE_SNAPSHOT).expanduser().resolve()
    source_dir = output_dir / "question_sources"
    source_dir.mkdir(parents=True, exist_ok=True)
    result = {
        "bridge_available": False,
        "bridge_sha256": None,
        "bridge_semantic_sha256": None,
        "verified_question_surfaces": 0,
        "verified_answer_isolation_attestations": 0,
        "surface_gaps": len(nodes),
        "diagnostics": [],
    }
    def downgrade(source: dict[str, Any]) -> None:
        source["status"] = (
            "detail_locator_only"
            if source.get("detail_uri")
            else "unverified_or_missing"
        )
        source["surface_verified"] = False
        source["source_sha256"] = None
        source["answer_isolated"] = False
        source["original_question_eligible"] = False

    def mark_unverified() -> None:
        for node in nodes.values():
            downgrade(node["reliable_source"])

    def remove_stale_manifests(expected: set[str] | None = None) -> None:
        keep = expected or set()
        for path in source_dir.glob("*.json"):
            if path.name not in keep:
                path.unlink()

    if not bridge.is_file():
        mark_unverified()
        remove_stale_manifests()
        result["diagnostics"].append("bridge_snapshot_missing")
        return result
    try:
        raw = json.loads(bridge.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise PersonalizationIndexError("details bridge snapshot is corrupt") from exc
    records = raw.get("records") if isinstance(raw, dict) else None
    if not isinstance(records, list):
        raise PersonalizationIndexError("details bridge records must be a list")
    bridge_project = Path(str(raw.get("project_root") or "")).expanduser().resolve()
    if bridge_project != repo:
        mark_unverified()
        remove_stale_manifests()
        result["diagnostics"].append("bridge_project_mismatch")
        return result
    details_root = Path(str(raw.get("details_root") or "")).expanduser().resolve()
    if not details_root.is_dir():
        raise PersonalizationIndexError("details bridge root is unavailable")
    by_node = {
        str(row.get("formal_id") or row.get("node_id") or ""): row
        for row in records
        if isinstance(row, dict)
    }
    verified = 0
    diagnostics: list[str] = []
    expected_manifests: set[str] = set()
    for node_id, node in nodes.items():
        source = node["reliable_source"]
        downgrade(source)
        row = by_node.get(node_id)
        if not row or row.get("link_status") != "detail_card_with_assets":
            diagnostics.append(f"{node_id}:bridge_record_missing")
            continue
        asset_dir = (details_root / str(row.get("asset_dir") or "")).resolve()
        if not _inside(asset_dir, details_root) or not asset_dir.is_dir():
            diagnostics.append(f"{node_id}:asset_dir_invalid")
            continue
        declared_assets = row.get("canonical_question_assets")
        if not isinstance(declared_assets, list) or not declared_assets:
            diagnostics.append(f"{node_id}:canonical_question_assets_missing")
            continue
        question_assets: list[Path] = []
        question_asset_entries: list[dict[str, Any]] = []
        declared_paths: set[Path] = set()
        canonical_assets_valid = True
        for entry in declared_assets:
            if not isinstance(entry, dict) or set(entry) != {
                "path",
                "sha256",
                "size_bytes",
            }:
                canonical_assets_valid = False
                break
            locator = Path(str(entry.get("path") or ""))
            if locator.is_absolute() or ".." in locator.parts:
                canonical_assets_valid = False
                break
            unresolved = details_root / locator
            if unresolved.is_symlink():
                canonical_assets_valid = False
                break
            path = unresolved.resolve()
            if (
                path in declared_paths
                or not _inside(path, details_root)
                or not path.is_file()
                or path.is_symlink()
                or path.parent != asset_dir
                or path.parent.name != node_id
                or not QUESTION_ASSET_RE.fullmatch(path.name)
                or not isinstance(entry.get("size_bytes"), int)
                or entry.get("size_bytes") != path.stat().st_size
                or not re.fullmatch(r"[0-9a-f]{64}", str(entry.get("sha256") or ""))
                or entry.get("sha256") != sha256_file(path)
            ):
                canonical_assets_valid = False
                break
            declared_paths.add(path)
            question_assets.append(path)
            question_asset_entries.append(
                {
                    "path": path.relative_to(details_root).as_posix(),
                    "sha256": str(entry["sha256"]),
                    "size_bytes": path.stat().st_size,
                }
            )
        if not canonical_assets_valid:
            diagnostics.append(f"{node_id}:canonical_question_assets_invalid")
            continue
        question_assets.sort()
        question_asset_entries.sort(key=lambda entry: entry["path"])
        solution_hashes = {
            sha256_file(path)
            for path in asset_dir.iterdir()
            if path.is_file()
            and not path.is_symlink()
            and path.stat().st_size > 0
            and SOLUTION_ASSET_RE.fullmatch(path.name)
        }
        declared_count = row.get("question_asset_count")
        if (
            not question_assets
            or not isinstance(declared_count, int)
            or declared_count != len(question_assets)
            or asset_dir.name != node_id
            or any(sha256_file(path) in solution_hashes for path in question_assets)
        ):
            diagnostics.append(f"{node_id}:question_asset_count_mismatch")
            continue
        attestation_ref = str(
            row.get("answer_isolation_attestation_ref") or ""
        ).strip()
        attestation_sha = str(
            row.get("answer_isolation_attestation_sha256") or ""
        ).strip()
        attestation_locator = Path(attestation_ref)
        if (
            not attestation_ref
            or attestation_locator.is_absolute()
            or ".." in attestation_locator.parts
            or not re.fullmatch(r"[0-9a-f]{64}", attestation_sha)
        ):
            diagnostics.append(f"{node_id}:answer_isolation_attestation_missing")
            continue
        unresolved_attestation = details_root / attestation_locator
        if unresolved_attestation.is_symlink():
            diagnostics.append(f"{node_id}:answer_isolation_attestation_unsafe")
            continue
        attestation_path = unresolved_attestation.resolve()
        if (
            not _inside(attestation_path, details_root)
            or not attestation_path.is_file()
            or attestation_path.stat().st_size <= 0
            or sha256_file(attestation_path) != attestation_sha
        ):
            diagnostics.append(f"{node_id}:answer_isolation_attestation_drift")
            continue
        try:
            validate_attestation_chain(
                details_root=details_root,
                attestation_path=attestation_path,
                formal_node_id=node_id,
                expected_assets=question_asset_entries,
            )
        except QuestionSourceAttestationError:
            diagnostics.append(f"{node_id}:answer_isolation_attestation_invalid")
            continue
        question_asset_set_sha = asset_set_sha256(question_asset_entries)
        manifest = {
            "schema": QUESTION_SOURCE_SCHEMA,
            "formal_node_id": node_id,
            "source_id": source.get("source_id"),
            "answer_isolated": True,
            "asset_root": {
                "kind": "details_bridge_root",
                "details_vault": str(raw.get("details_vault") or "kaoyan-408-details"),
            },
            "question_asset_count": len(question_assets),
            "question_assets": question_asset_entries,
            "answer_isolation_verification": {
                "schema": QUESTION_ISOLATION_ATTESTATION_SCHEMA,
                "attestation_ref": attestation_ref,
                "attestation_sha256": attestation_sha,
                "question_asset_set_sha256": question_asset_set_sha,
            },
        }
        manifest_path = source_dir / f"{node_id}.json"
        expected_manifests.add(manifest_path.name)
        manifest_sha = _atomic_write_json(manifest_path, manifest)
        # Keep the compiled index independent of its output directory.  Runtime
        # consumers resolve this projection-relative locator against the active
        # personalization state directory; absolute build paths must never enter
        # a content-addressed artifact.
        source_ref = f"{PERSONALIZATION_SOURCE_PREFIX}question_sources/{node_id}.json"
        source.update(
            {
                "status": (
                    "verified_exam_locator"
                    if source.get("canonical_exam_id")
                    else "verified_question_surface"
                ),
                "source_ref": source_ref,
                "source_ref_kind": "question_asset_manifest",
                "reference_fingerprint": manifest_sha,
                "source_sha256": manifest_sha,
                "surface_verified": True,
                "answer_isolated": True,
                "answer_isolation_attestation_sha256": attestation_sha,
                "question_asset_count": len(question_assets),
                "original_question_eligible": True,
            }
        )
        verified += 1
    remove_stale_manifests(expected_manifests)
    semantic_bridge = {
        "details_vault": str(raw.get("details_vault") or ""),
        "records": sorted(
            (
                {
                    "formal_id": str(row.get("formal_id") or row.get("node_id") or ""),
                    "link_status": row.get("link_status"),
                    "asset_dir": row.get("asset_dir"),
                    "question_asset_count": row.get("question_asset_count"),
                    "canonical_question_assets": row.get(
                        "canonical_question_assets"
                    ),
                    "answer_isolation_attestation_ref": row.get(
                        "answer_isolation_attestation_ref"
                    ),
                    "answer_isolation_attestation_sha256": row.get(
                        "answer_isolation_attestation_sha256"
                    ),
                }
                for row in records
                if isinstance(row, dict)
            ),
            key=lambda row: row["formal_id"],
        ),
    }
    result.update(
        {
            "bridge_available": True,
            "bridge_sha256": sha256_file(bridge),
            "bridge_semantic_sha256": sha256_bytes(
                canonical_json_bytes(semantic_bridge)
            ),
            "verified_question_surfaces": verified,
            "verified_answer_isolation_attestations": verified,
            "surface_gaps": len(nodes) - verified,
            "diagnostics": diagnostics[:64],
        }
    )
    return result


def load_review_unit_mapping(
    path: Path | None, nodes: dict[str, dict[str, Any]]
) -> tuple[dict[str, list[str]], dict[str, list[str]]]:
    """Load only formal-node and unit IDs from the optional safe mapping table."""

    node_to_units: dict[str, set[str]] = defaultdict(set)
    unit_to_nodes: dict[str, set[str]] = defaultdict(set)
    if path is None or not path.exists():
        return {}, {}
    for row in read_markdown_table(path):
        node_id = row.get("正式节点ID", "").strip()
        if node_id not in nodes:
            continue
        values = [row.get("主复习单元ID", ""), row.get("次复习单元ID", "")]
        for raw in values:
            for value in re.split(r"[；;、,，\s]+", raw.strip()):
                unit_id = value.strip().upper()
                if not REVIEW_UNIT_RE.fullmatch(unit_id):
                    continue
                node_to_units[node_id].add(unit_id)
                unit_to_nodes[unit_id].add(node_id)
    return (
        {key: sorted(value) for key, value in sorted(node_to_units.items())},
        {key: sorted(value) for key, value in sorted(unit_to_nodes.items())},
    )


def load_events(path: Path) -> list[dict[str, Any]]:
    events: list[dict[str, Any]] = []
    seen: set[str] = set()
    for line_number, line in enumerate(
        path.read_text(encoding="utf-8").splitlines(), start=1
    ):
        if not line.strip():
            continue
        try:
            value = json.loads(line)
        except json.JSONDecodeError as exc:
            raise PersonalizationIndexError(
                f"invalid JSON at {path}:{line_number}: {exc}"
            ) from exc
        if not isinstance(value, dict):
            raise PersonalizationIndexError(
                f"expected JSON object at {path}:{line_number}"
            )
        event_id = str(value.get("event_id") or "").strip()
        if not event_id:
            raise PersonalizationIndexError(
                f"event without event_id at {path}:{line_number}"
            )
        if event_id in seen:
            raise PersonalizationIndexError(f"duplicate event_id: {event_id}")
        seen.add(event_id)
        events.append(value)
    return events


def load_morning_presentation_index(
    repo: Path,
) -> tuple[dict[tuple[str, str], str], str]:
    """Recover immutable item presentation kinds for legacy outcome events."""

    root = repo / MORNING_STATE_RELATIVE
    values: dict[tuple[str, str], str] = {}
    if root.is_dir():
        for ledger in sorted(root.glob("*/events.jsonl")):
            rows: list[dict[str, Any]] = []
            for line_number, line in enumerate(
                ledger.read_text(encoding="utf-8").splitlines(), start=1
            ):
                if not line.strip():
                    continue
                try:
                    row = json.loads(line)
                except json.JSONDecodeError as exc:
                    raise PersonalizationIndexError(
                        f"invalid morning JSON at {ledger}:{line_number}"
                    ) from exc
                if isinstance(row, dict):
                    rows.append(row)
            if not rows or rows[0].get("event_type") != "session_started":
                continue
            session_id = str(rows[0].get("session_id") or "")
            items = list((rows[0].get("payload") or {}).get("items") or [])
            items.extend(
                (row.get("payload") or {})
                for row in rows[1:]
                if row.get("event_type") == "followup_scheduled"
            )
            for item in items:
                if not isinstance(item, dict):
                    continue
                item_id = str(item.get("item_id") or "")
                kind = str(item.get("item_kind") or "")
                if not session_id or not item_id or not kind:
                    continue
                key = (session_id, item_id)
                if key in values and values[key] != kind:
                    raise PersonalizationIndexError(
                        f"conflicting morning presentation kind: {session_id}/{item_id}"
                    )
                values[key] = kind
    semantic = [
        {"session_id": key[0], "item_id": key[1], "presentation_kind": value}
        for key, value in sorted(values.items())
    ]
    return values, sha256_bytes(canonical_json_bytes(semantic))


def enrich_event_presentations(
    events: list[dict[str, Any]],
    presentation_index: dict[tuple[str, str], str],
) -> list[dict[str, Any]]:
    enriched: list[dict[str, Any]] = []
    for event in events:
        row = dict(event)
        if not row.get("presentation_kind") and row.get("source") == "morning_review":
            kind = presentation_index.get(
                (str(row.get("session_id") or ""), str(row.get("item_id") or ""))
            )
            if kind:
                row["presentation_kind"] = kind
        enriched.append(row)
    return enriched


def _event_date(event: dict[str, Any]) -> str | None:
    observed = _valid_date(event.get("observed_date"))
    if observed:
        return observed
    raw = str(event.get("event_time") or "")
    try:
        return datetime.fromisoformat(raw.replace("Z", "+00:00")).astimezone(
            ZoneInfo("Asia/Shanghai")
        ).date().isoformat()
    except ValueError:
        return None


def _canonical_review_unit(value: Any) -> str | None:
    text = str(value or "").strip().upper()
    return text if REVIEW_UNIT_RE.fullmatch(text) else None


def map_events(
    events: list[dict[str, Any]],
    catalog: dict[str, Any],
    nodes: dict[str, dict[str, Any]],
    node_to_units: dict[str, list[str]],
    unit_to_nodes: dict[str, list[str]],
) -> dict[str, Any]:
    """Map events without promoting ambiguous evidence into point/question facts."""

    source_to_nodes: dict[str, set[str]] = defaultdict(set)
    for node_id, node in nodes.items():
        source_to_nodes[node_id].add(node_id)
        source_id = node["reliable_source"].get("source_id")
        if source_id:
            source_to_nodes[source_id].add(node_id)

    mapped: dict[str, dict[str, Any]] = {}
    by_identity: dict[tuple[str, str], dict[str, Any]] = {}
    unresolved: list[str] = []

    for event in events:
        event_id = str(event["event_id"])
        event_kind = str(event.get("event_kind") or "")
        practice_mode = str(event.get("practice_mode") or "") or None
        if practice_mode not in {None, "original", "variant"}:
            practice_mode = None
        reasons: list[str] = []
        mapping_sources: list[str] = []
        formal_ids: set[str] = set()
        direct_question_ids: set[str] = set()
        variant_origin_ids: set[str] = set()
        presentation_kind = str(event.get("presentation_kind") or "") or None

        actual_question_formal = str(
            event.get("actual_question_formal_node_id") or ""
        ).strip()
        if actual_question_formal:
            if actual_question_formal in nodes:
                formal_ids.add(actual_question_formal)
                direct_question_ids.add(actual_question_formal)
                mapping_sources.append("actual_question_formal_node_id")
            else:
                reasons.append(
                    f"unknown_actual_question_formal_node_id:{actual_question_formal}"
                )

        variant_of_formal = str(
            event.get("variant_of_formal_node_id") or ""
        ).strip()
        if variant_of_formal:
            if variant_of_formal in nodes:
                formal_ids.add(variant_of_formal)
                variant_origin_ids.add(variant_of_formal)
                mapping_sources.append("variant_of_formal_node_id")
            else:
                reasons.append(
                    f"unknown_variant_of_formal_node_id:{variant_of_formal}"
                )

        explicit_formal = str(event.get("formal_node_id") or "").strip()
        if explicit_formal:
            if explicit_formal in nodes:
                formal_ids.add(explicit_formal)
                mapping_sources.append("formal_node_id")
            else:
                reasons.append(f"unknown_formal_node_id:{explicit_formal}")

        mapped_formal = event.get("mapped_formal_node_ids") or []
        if isinstance(mapped_formal, str):
            mapped_formal = [mapped_formal]
        if isinstance(mapped_formal, list):
            valid_mapped = {
                str(value).strip()
                for value in mapped_formal
                if str(value).strip() in nodes
            }
            invalid_mapped = sorted(
                {
                    str(value).strip()
                    for value in mapped_formal
                    if str(value).strip() and str(value).strip() not in nodes
                }
            )
            if valid_mapped:
                formal_ids.update(valid_mapped)
                mapping_sources.append("mapped_formal_node_ids")
            reasons.extend(f"unknown_mapped_formal_node_id:{value}" for value in invalid_mapped)

        source_id = str(event.get("source_id") or "").strip()
        source_matches = source_to_nodes.get(source_id, set())
        if source_matches:
            formal_ids.update(source_matches)
            mapping_sources.append("source_id")

        explicit_unit = _canonical_review_unit(event.get("review_unit_id"))
        mechanism_unit = _canonical_review_unit(event.get("mechanism_key"))
        event_unit = explicit_unit or mechanism_unit
        if event_unit and event_unit in unit_to_nodes:
            unit_nodes = set(unit_to_nodes[event_unit])
            formal_ids.update(unit_nodes)
            mapping_sources.append("review_unit_mapping")

        identity = (
            str(event.get("session_id") or ""),
            str(event.get("item_id") or ""),
        )
        inherited: dict[str, Any] | None = None
        if event_kind == "repair" and all(identity):
            inherited = by_identity.get(identity)
            if inherited:
                formal_ids.update(inherited["formal_node_ids"])
                direct_question_ids.update(inherited["question_node_ids"])
                mapping_sources.append("prior_outcome_identity")
                if practice_mode is None:
                    practice_mode = inherited.get("practice_mode")

        explicit_points = resolve_point_ids(event.get("knowledge_point"), catalog)
        if event.get("knowledge_point") and not explicit_points:
            reasons.append("knowledge_point_unresolved")

        if practice_mode == "variant" and variant_origin_ids:
            point_formal_ids = variant_origin_ids
        elif practice_mode == "original" and actual_question_formal in nodes:
            point_formal_ids = {actual_question_formal}
        elif practice_mode == "original" and explicit_formal in nodes:
            point_formal_ids = {explicit_formal}
        else:
            point_formal_ids = formal_ids
        formal_primary_points = {
            point_id
            for node_id in point_formal_ids
            for point_id in nodes[node_id]["primary_point_ids"]
        }
        if explicit_points:
            point_ids = set(explicit_points)
            mapping_sources.append("knowledge_point")
        elif len(formal_primary_points) == 1:
            point_ids = set(formal_primary_points)
            mapping_sources.append("formal_node_primary_point")
        elif len(formal_primary_points) > 1:
            point_ids = set()
            reasons.append("ambiguous_formal_node_primary_points")
        elif inherited:
            point_ids = set(inherited["point_ids"])
            if point_ids:
                mapping_sources.append("prior_outcome_point")
        else:
            point_ids = set()

        context_point_ids = {
            point_id
            for node_id in formal_ids
            for point_id in nodes[node_id]["point_ids"]
        }
        context_point_ids.update(point_ids)

        explicit_mechanism_ids: set[str] = set()
        if event_unit:
            explicit_mechanism_ids.add(event_unit)
            mapping_sources.append("event_review_unit")
        mechanism_point_ids = resolve_point_ids(event.get("mechanism_key"), catalog)
        explicit_mechanism_ids.update(f"KP:{value}" for value in mechanism_point_ids)

        if explicit_mechanism_ids:
            mechanism_ids = explicit_mechanism_ids
        else:
            node_units = {
                unit_id
                for node_id in formal_ids
                for unit_id in node_to_units.get(node_id, [])
            }
            if len(node_units) == 1:
                mechanism_ids = node_units
                mapping_sources.append("formal_node_review_unit")
            elif len(node_units) > 1:
                mechanism_ids = set()
                reasons.append("ambiguous_formal_node_mechanisms")
            elif inherited and inherited["mechanism_ids"]:
                mechanism_ids = set(inherited["mechanism_ids"])
                mapping_sources.append("prior_outcome_mechanism")
            else:
                mechanism_ids = {f"KP:{value}" for value in point_ids}
                if mechanism_ids:
                    mapping_sources.append("knowledge_point_mechanism")

        # Knowledge/review-unit mappings establish context, not presentation
        # evidence.  A question cooldown advances only when the event proves an
        # actual original surface was attempted.  Linked variants keep their
        # anchor as context and never advance the anchor's cooldown.
        if practice_mode == "variant":
            direct_question_ids.clear()
            mapping_sources.append("variant_anchor_not_question_identity")
        elif practice_mode == "original":
            if actual_question_formal in nodes:
                direct_question_ids = {actual_question_formal}
            elif explicit_formal in nodes:
                direct_question_ids = {explicit_formal}
            else:
                direct_question_ids.clear()
                reasons.append("linked_original_identity_missing")
        elif event_kind == "repair" and inherited:
            direct_question_ids = set(inherited.get("question_node_ids") or [])
            if direct_question_ids:
                mapping_sources.append("prior_outcome_question_identity")
        elif presentation_kind == "original_question" or (
            event.get("source") == "daily_practice" and explicit_formal in nodes
        ):
            if explicit_formal in nodes:
                direct_question_ids = {explicit_formal}
                mapping_sources.append("original_presentation_evidence")
            elif presentation_kind == "original_question" and len(formal_ids) == 1:
                direct_question_ids = set(formal_ids)
                mapping_sources.append(
                    "original_presentation_unique_formal_identity"
                )
            else:
                direct_question_ids.clear()
                reasons.append("original_presentation_identity_missing")
        else:
            direct_question_ids.clear()
            if formal_ids:
                mapping_sources.append("context_only_formal_mapping")
        if len(direct_question_ids) > 1:
            direct_question_ids.clear()
            reasons.append("ambiguous_question_identity")

        has_resolution = bool(point_ids or mechanism_ids or direct_question_ids)
        if has_resolution and reasons:
            status = "partially_resolved"
        elif has_resolution:
            status = "resolved"
        else:
            status = "evidence_insufficient"

        result = {
            "event_id": event_id,
            "event_kind": event_kind,
            "practice_mode": practice_mode,
            "presentation_kind": presentation_kind,
            "actual_question_formal_node_id": actual_question_formal or None,
            "variant_of_formal_node_id": variant_of_formal or None,
            "observed_date": _event_date(event),
            "mapping_status": status,
            "mapping_sources": sorted(set(mapping_sources)),
            "point_ids": sorted(point_ids),
            "context_point_ids": sorted(context_point_ids),
            "mechanism_ids": sorted(mechanism_ids),
            "formal_node_ids": sorted(formal_ids),
            "question_node_ids": sorted(direct_question_ids),
            "unresolved_reasons": sorted(set(reasons)),
            "first_result": event.get("first_result") if event_kind == "outcome" else None,
            "correction_result": event.get("correction_result")
            if event_kind == "repair"
            else None,
            "evidence_kind": event.get("evidence_kind"),
            "confidence": event.get("confidence"),
            "fragile_mastery": bool(event.get("fragile_mastery", False)),
            "stable_retention": bool(event.get("stable_retention", False)),
        }
        mapped[event_id] = result
        if status != "resolved":
            unresolved.append(event_id)
        if event_kind == "outcome" and all(identity):
            by_identity[identity] = result

    counts: dict[str, int] = defaultdict(int)
    performance_counts: dict[str, int] = defaultdict(int)
    unresolved_performance: list[str] = []
    for value in mapped.values():
        counts[value["mapping_status"]] += 1
        if value["event_kind"] in PERFORMANCE_EVENT_KINDS:
            performance_counts[value["mapping_status"]] += 1
            if value["mapping_status"] != "resolved":
                unresolved_performance.append(value["event_id"])
    return {
        "schema": SCHEMA,
        "kind": "event_mappings",
        "event_count": len(mapped),
        "mapping_status_counts": dict(sorted(counts.items())),
        "performance_mapping_status_counts": dict(sorted(performance_counts.items())),
        "unresolved_event_ids": unresolved,
        "unresolved_performance_event_ids": unresolved_performance,
        "events": mapped,
    }


def _new_sparse_state(entity_id: str) -> dict[str, Any]:
    return {
        "id": entity_id,
        "outcome_count": 0,
        "repair_count": 0,
        "independent_correct_count": 0,
        "failure_count": 0,
        "fragile_count": 0,
        "stable_retention_count": 0,
        "recent_7d_outcome_count": 0,
        "recent_7d_failure_count": 0,
        "recent_30d_outcome_count": 0,
        "recent_30d_failure_count": 0,
        "last_attempt_date": None,
        "last_outcome_date": None,
        "last_failure_date": None,
        "last_independent_correct_date": None,
        "last_stable_retention_date": None,
        "evidence_event_ids": [],
    }


def _apply_performance(
    state: dict[str, Any],
    event: dict[str, Any],
    mapping: dict[str, Any],
    as_of: str,
) -> bool:
    event_kind = mapping["event_kind"]
    observed = mapping["observed_date"]
    if mapping["event_id"] in state["evidence_event_ids"]:
        return False
    state["evidence_event_ids"].append(mapping["event_id"])
    if len(state["evidence_event_ids"]) > MAX_RECENT_EVENT_IDS_PER_ENTITY:
        state["evidence_event_ids"] = state["evidence_event_ids"][-MAX_RECENT_EVENT_IDS_PER_ENTITY:]
    state["last_attempt_date"] = _max_date(state["last_attempt_date"], observed)
    age = _days_between(observed, as_of)

    if event_kind == "repair":
        state["repair_count"] += 1
        return True
    if event_kind != "outcome":
        return True

    state["outcome_count"] += 1
    state["last_outcome_date"] = _max_date(state["last_outcome_date"], observed)
    result = str(event.get("first_result") or "")
    if result == "independent_correct":
        state["independent_correct_count"] += 1
        state["last_independent_correct_date"] = _max_date(
            state["last_independent_correct_date"], observed
        )
    if result in FAILURE_RESULTS:
        state["failure_count"] += 1
        state["last_failure_date"] = _max_date(state["last_failure_date"], observed)
    if mapping["fragile_mastery"] or mapping["evidence_kind"] == "fragile":
        state["fragile_count"] += 1
    if mapping["stable_retention"]:
        state["stable_retention_count"] += 1
        state["last_stable_retention_date"] = _max_date(
            state["last_stable_retention_date"], observed
        )
    if age is not None and 0 <= age < 7:
        state["recent_7d_outcome_count"] += 1
        if result in FAILURE_RESULTS:
            state["recent_7d_failure_count"] += 1
    if age is not None and 0 <= age < 30:
        state["recent_30d_outcome_count"] += 1
        if result in FAILURE_RESULTS:
            state["recent_30d_failure_count"] += 1
    return True


def replay_sparse_state(
    events: list[dict[str, Any]], mappings: dict[str, Any], as_of: str
) -> dict[str, Any]:
    """Replay only resolved entity keys; unresolved evidence remains visible."""

    point_state: dict[str, dict[str, Any]] = {}
    mechanism_state: dict[str, dict[str, Any]] = {}
    question_state: dict[str, dict[str, Any]] = {}
    performance_count = 0

    for event in events:
        event_id = str(event["event_id"])
        mapping = mappings["events"][event_id]
        if mapping["event_kind"] not in PERFORMANCE_EVENT_KINDS:
            continue
        performance_count += 1
        for point_id in mapping["point_ids"]:
            state = point_state.setdefault(point_id, _new_sparse_state(point_id))
            _apply_performance(state, event, mapping, as_of)
        for mechanism_id in mapping["mechanism_ids"]:
            state = mechanism_state.setdefault(
                mechanism_id, _new_sparse_state(mechanism_id)
            )
            _apply_performance(state, event, mapping, as_of)
        if mapping["event_kind"] == "outcome" and mapping.get("practice_mode") != "variant":
            for node_id in mapping["question_node_ids"]:
                state = question_state.setdefault(node_id, _new_sparse_state(node_id))
                _apply_performance(state, event, mapping, as_of)

    return {
        "schema": SCHEMA,
        "kind": "sparse_learner_state",
        "as_of": as_of,
        "source_event_count": len(events),
        "performance_event_count": performance_count,
        "point_state_count": len(point_state),
        "mechanism_state_count": len(mechanism_state),
        "question_state_count": len(question_state),
        "applied_performance_event_ids": [
            str(event["event_id"])
            for event in events
            if mappings["events"][str(event["event_id"])]["event_kind"]
            in PERFORMANCE_EVENT_KINDS
        ],
        "selection_commitments": {},
        "points": dict(sorted(point_state.items())),
        "mechanisms": dict(sorted(mechanism_state.items())),
        "questions": dict(sorted(question_state.items())),
        "evidence_insufficient": {
            "count": len(mappings["unresolved_performance_event_ids"]),
            "event_ids": mappings["unresolved_performance_event_ids"][
                -MAX_RECENT_UNRESOLVED_EVENT_IDS:
            ],
            "event_ids_truncated": len(
                mappings["unresolved_performance_event_ids"]
            )
            > MAX_RECENT_UNRESOLVED_EVENT_IDS,
        },
    }


def build_question_index(
    catalog: dict[str, Any],
    nodes: dict[str, dict[str, Any]],
    node_to_units: dict[str, list[str]],
    unit_to_nodes: dict[str, list[str]],
    mappings: dict[str, Any],
    learner_state: dict[str, Any],
) -> dict[str, Any]:
    """Build point/mechanism -> question references and effective attempt dates."""

    mechanism_to_nodes: dict[str, set[str]] = defaultdict(set)
    mechanism_to_points: dict[str, set[str]] = defaultdict(set)
    for node_id, node in nodes.items():
        for point_id in node["point_ids"]:
            mechanism_id = f"KP:{point_id}"
            mechanism_to_nodes[mechanism_id].add(node_id)
            mechanism_to_points[mechanism_id].add(point_id)
        for mechanism_id in node_to_units.get(node_id, []):
            mechanism_to_nodes[mechanism_id].add(node_id)
            mechanism_to_points[mechanism_id].update(node["point_ids"])

    # Event mechanisms may be newer than the formal mapping.  They are safe
    # retrieval candidates, but only their explicit/candidate formal IDs are used.
    for mapping in mappings["events"].values():
        for mechanism_id in mapping["mechanism_ids"]:
            mechanism_to_nodes[mechanism_id].update(mapping["formal_node_ids"])
            mechanism_to_points[mechanism_id].update(mapping["point_ids"])

    questions: dict[str, dict[str, Any]] = {}
    by_point: dict[str, list[dict[str, Any]]] = defaultdict(list)
    by_mechanism: dict[str, list[dict[str, Any]]] = defaultdict(list)

    for node_id, node in nodes.items():
        sparse = learner_state["questions"].get(node_id, {})
        ledger_last = sparse.get("last_attempt_date")
        formal_last = node["formal_last_review_date"]
        effective = _max_date(formal_last, ledger_last)
        if effective and formal_last == effective and ledger_last == effective:
            effective_source = "formal_and_ledger"
        elif effective and ledger_last == effective:
            effective_source = "review_ledger"
        elif effective:
            effective_source = "formal_node"
        else:
            effective_source = "unrecorded"

        mechanism_ids = sorted(
            {
                f"KP:{point_id}" for point_id in node["point_ids"]
            }
            | set(node_to_units.get(node_id, []))
            | {
                mechanism_id
                for mechanism_id, members in mechanism_to_nodes.items()
                if node_id in members
            }
        )
        question = {
            "node_id": node_id,
            "subject": node["subject"],
            "subject_name": node["subject_name"],
            "primary_point_ids": node["primary_point_ids"],
            "point_ids": node["point_ids"],
            "mechanism_ids": mechanism_ids,
            "reliable_source": node["reliable_source"],
            "formal_last_review_date": formal_last,
            "ledger_last_attempt_date": ledger_last,
            "effective_last_attempt_date": effective,
            "effective_last_attempt_source": effective_source,
            "ledger_evidence_event_ids": sparse.get("evidence_event_ids", []),
        }
        questions[node_id] = question
        reference = {
            "node_id": node_id,
            "subject": node["subject"],
            "source_status": node["reliable_source"]["status"],
            "original_question_eligible": node["reliable_source"][
                "original_question_eligible"
            ],
            "effective_last_attempt_date": effective,
        }
        for point_id in node["point_ids"]:
            by_point[point_id].append(reference)
        for mechanism_id in mechanism_ids:
            by_mechanism[mechanism_id].append(reference)

    def sorted_references(values: Iterable[dict[str, Any]]) -> list[dict[str, Any]]:
        return sorted(values, key=lambda value: value["node_id"])

    mechanisms: dict[str, dict[str, Any]] = {}
    for mechanism_id in sorted(mechanism_to_nodes):
        mechanisms[mechanism_id] = {
            "mechanism_id": mechanism_id,
            "kind": "knowledge_point" if mechanism_id.startswith("KP:") else "review_unit",
            "point_ids": sorted(mechanism_to_points[mechanism_id]),
            "formal_node_ids": sorted(mechanism_to_nodes[mechanism_id]),
        }

    return {
        "schema": SCHEMA,
        "kind": "question_inverted_index",
        "question_count": len(questions),
        "mechanism_count": len(mechanisms),
        "questions": dict(sorted(questions.items())),
        "mechanisms": mechanisms,
        "by_point": {
            key: sorted_references(value) for key, value in sorted(by_point.items())
        },
        "by_mechanism": {
            key: sorted_references(value)
            for key, value in sorted(by_mechanism.items())
        },
    }


def _atomic_write_json(path: Path, value: Any) -> str:
    data = output_json_bytes(value)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temp_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp_name, path)
    finally:
        if os.path.exists(temp_name):
            os.unlink(temp_name)
    return sha256_bytes(data)


@contextmanager
def _projection_lock(output_dir: Path) -> Iterator[None]:
    output_dir.mkdir(parents=True, exist_ok=True)
    with (output_dir / ".hot-path.lock").open("a+", encoding="utf-8") as handle:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def _build_artifacts_unlocked(
    repo: Path,
    output_dir: Path | None = None,
    as_of: str | None = None,
    expected_point_count: int | None = EXPECTED_POINT_COUNT,
) -> dict[str, Any]:
    _retired_write_surface()
    repo = repo.resolve()
    output_dir = (output_dir or repo / DEFAULT_OUTPUT_RELATIVE).resolve()
    as_of = as_of or datetime.now(ZoneInfo("Asia/Shanghai")).date().isoformat()
    if not _valid_date(as_of):
        raise PersonalizationIndexError(f"invalid --as-of date: {as_of}")

    source_paths = {
        "knowledge_points": repo / "知识点标签表.md",
        "formal_nodes": repo / "节点总表.md",
        "events": repo / DEFAULT_EVENTS_RELATIVE,
    }
    optional_review_map = repo / "复习单元节点映射.md"
    for name, path in source_paths.items():
        if not path.exists():
            raise PersonalizationIndexError(f"missing {name} source: {path}")

    catalog = compile_point_catalog(
        source_paths["knowledge_points"], expected_count=expected_point_count
    )
    nodes = load_formal_nodes(source_paths["formal_nodes"], catalog)
    source_surface = enrich_question_sources(
        repo=repo,
        output_dir=output_dir,
        nodes=nodes,
    )
    node_to_units, unit_to_nodes = load_review_unit_mapping(
        optional_review_map if optional_review_map.exists() else None, nodes
    )
    presentation_index, presentation_hash = load_morning_presentation_index(repo)
    events = enrich_event_presentations(
        load_events(source_paths["events"]), presentation_index
    )
    mappings = map_events(events, catalog, nodes, node_to_units, unit_to_nodes)
    learner_state = replay_sparse_state(events, mappings, as_of)
    failure_signatures = build_recent_signature_index(events, mappings, as_of)
    question_index = build_question_index(
        catalog,
        nodes,
        node_to_units,
        unit_to_nodes,
        mappings,
        learner_state,
    )
    try:
        variant_catalog = rebuild_variant_catalog(repo)
    except VariantPipelineError as exc:
        raise PersonalizationIndexError(
            "verified variant source catalog failed full rebuild validation"
        ) from exc
    try:
        cross_variant_catalog = rebuild_cross_variant_catalog(repo)
        validate_cross_variant_catalog(repo, cross_variant_catalog)
    except CrossVariantPipelineError as exc:
        raise PersonalizationIndexError(
            "verified cross-variant catalog failed full rebuild validation"
        ) from exc

    artifacts = {
        "catalog.json": catalog,
        "event_mappings.json": mappings,
        "learner_state.json": learner_state,
        FAILURE_SIGNATURE_FILENAME: failure_signatures,
        "question_index.json": question_index,
        VARIANT_CATALOG_FILENAME: variant_catalog,
        CROSS_VARIANT_CATALOG_FILENAME: cross_variant_catalog,
    }
    artifact_hashes = {
        name: sha256_bytes(output_json_bytes(value))
        for name, value in artifacts.items()
    }
    source_hashes = {
        name: sha256_file(path) for name, path in source_paths.items()
    }
    source_hashes["morning_presentation_index"] = presentation_hash
    if source_surface.get("bridge_semantic_sha256"):
        source_hashes["details_bridge_semantic"] = str(
            source_surface["bridge_semantic_sha256"]
        )
    if optional_review_map.exists():
        source_hashes["review_unit_mapping"] = sha256_file(optional_review_map)
    cross_path = output_dir / CROSS_PROJECTION_FILENAME
    cross_hash: str | None = None
    if cross_path.is_file():
        try:
            cross_payload = json.loads(cross_path.read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            raise PersonalizationIndexError(
                "cross-subject shadow projection is corrupt"
            ) from exc
        try:
            validate_cross_projection(repo, cross_payload)
        except ShadowCatalogError as exc:
            raise PersonalizationIndexError(
                "cross-subject shadow projection does not match a current full rebuild"
            ) from exc
        cross_hash = sha256_file(cross_path)
    corpus_manifest_path = output_dir / CORPUS_MANIFEST_RELATIVE
    corpus_manifest_hash: str | None = None
    corpus_index_hash: str | None = None
    corpus_build_id: str | None = None
    if corpus_manifest_path.is_file():
        try:
            corpus_manifest = json.loads(
                corpus_manifest_path.read_text(encoding="utf-8")
            )
        except json.JSONDecodeError as exc:
            raise PersonalizationIndexError(
                "personalization corpus manifest is corrupt"
            ) from exc
        corpus_hashes = corpus_manifest.get("artifact_hashes")
        corpus_index_ref = str(corpus_manifest.get("index_ref") or "index.json")
        if corpus_index_ref != "index.json" and not re.fullmatch(
            r"indexes/[0-9a-f]{64}\.json", corpus_index_ref
        ):
            raise PersonalizationIndexError(
                "personalization corpus index_ref is invalid"
            )
        corpus_index_path = output_dir / "corpus" / corpus_index_ref
        if (
            corpus_manifest.get("schema") != "personalization_corpus_v1"
            or corpus_manifest.get("kind")
            != "nightly_curation_corpus_manifest"
            or corpus_manifest.get("answer_safe") is not True
            or int(corpus_manifest.get("formal_write_count", -1)) != 0
            or not isinstance(corpus_hashes, dict)
            or not corpus_index_path.is_file()
        ):
            raise PersonalizationIndexError(
                "personalization corpus cannot be bound into the root manifest"
            )
        corpus_index_hash = str(corpus_hashes.get(corpus_index_ref) or "")
        if (
            not re.fullmatch(r"[0-9a-f]{64}", corpus_index_hash)
            or sha256_file(corpus_index_path) != corpus_index_hash
            or (
                corpus_index_ref != "index.json"
                and Path(corpus_index_ref).stem != corpus_index_hash
            )
        ):
            raise PersonalizationIndexError(
                "personalization corpus index hash does not match its manifest"
            )
        corpus_manifest_hash = sha256_file(corpus_manifest_path)
        corpus_build_id = str(corpus_manifest.get("build_id") or "") or None
    build_id = sha256_bytes(
        canonical_json_bytes(
            {
                "schema": SCHEMA,
                "as_of": as_of,
                "source_hashes": source_hashes,
                "artifact_hashes": artifact_hashes,
                "cross_subject_shadow_sha256": cross_hash,
                "corpus_manifest_sha256": corpus_manifest_hash,
                "corpus_index_sha256": corpus_index_hash,
                "variant_catalog_sha256": artifact_hashes[
                    VARIANT_CATALOG_FILENAME
                ],
                "cross_variant_catalog_sha256": artifact_hashes[
                    CROSS_VARIANT_CATALOG_FILENAME
                ],
            }
        )
    )
    manifest = {
        "schema": SCHEMA,
        "kind": "personalization_manifest",
        "build_id": build_id,
        "as_of": as_of,
        "source_hashes": source_hashes,
        "artifact_hashes": artifact_hashes,
        "counts": {
            "points": catalog["point_count"],
            "formal_nodes": len(nodes),
            "events": len(events),
            "resolved_events": mappings["mapping_status_counts"].get("resolved", 0),
            "partially_resolved_events": mappings["mapping_status_counts"].get(
                "partially_resolved", 0
            ),
            "evidence_insufficient_events": mappings["mapping_status_counts"].get(
                "evidence_insufficient", 0
            ),
            "evidence_insufficient_performance_events": mappings[
                "performance_mapping_status_counts"
            ].get("evidence_insufficient", 0),
            "sparse_point_states": learner_state["point_state_count"],
            "sparse_mechanism_states": learner_state["mechanism_state_count"],
            "sparse_question_states": learner_state["question_state_count"],
            "recent_failure_signatures": failure_signatures["signature_count"],
            "verified_question_surfaces": int(
                source_surface.get("verified_question_surfaces", 0)
            ),
            "verified_answer_isolation_attestations": int(
                source_surface.get("verified_answer_isolation_attestations", 0)
            ),
            "question_surface_gaps": int(source_surface.get("surface_gaps", 0)),
            "verified_variant_sources": int(variant_catalog["entry_count"]),
            "verified_cross_variant_sources": int(
                cross_variant_catalog["entry_count"]
            ),
        },
        "answer_safe": True,
        "formal_write_count": 0,
        "question_source_projection": {
            "schema": QUESTION_SOURCE_SCHEMA,
            "bridge_available": bool(source_surface.get("bridge_available")),
            "verified_count": int(
                source_surface.get("verified_question_surfaces", 0)
            ),
            "gap_count": int(source_surface.get("surface_gaps", 0)),
            "diagnostics": list(source_surface.get("diagnostics", [])),
        },
        "independent_projections": {
            "cross_subject_shadow": {
                "file": CROSS_PROJECTION_FILENAME,
                "included_in_this_build": cross_hash is not None,
            },
            "variant_source_catalog": {
                "file": VARIANT_CATALOG_FILENAME,
                "included_in_this_build": True,
            },
            "cross_variant_catalog": {
                "file": CROSS_VARIANT_CATALOG_FILENAME,
                "included_in_this_build": True,
            },
            "nightly_curation_corpus": {
                "file": CORPUS_MANIFEST_RELATIVE.as_posix(),
                "included_in_this_build": corpus_manifest_hash is not None,
            },
        },
        "variant_catalog_sha256": artifact_hashes[VARIANT_CATALOG_FILENAME],
        "cross_variant_catalog_sha256": artifact_hashes[
            CROSS_VARIANT_CATALOG_FILENAME
        ],
    }
    if cross_hash is not None:
        manifest["cross_subject_shadow_sha256"] = cross_hash
    if corpus_manifest_hash is not None:
        manifest["corpus_manifest_sha256"] = corpus_manifest_hash
        manifest["corpus_index_sha256"] = corpus_index_hash
        manifest["corpus_build_id"] = corpus_build_id
    artifacts["manifest.json"] = manifest

    written_hashes: dict[str, str] = {}
    for name, value in artifacts.items():
        if name == "manifest.json":
            continue
        written_hashes[name] = _atomic_write_json(output_dir / name, value)
    written_hashes["manifest.json"] = _atomic_write_json(
        output_dir / "manifest.json", manifest
    )
    receipt_dir = output_dir / "selection_receipts"
    if receipt_dir.is_dir():
        for stale_receipt in receipt_dir.glob("*.json"):
            if stale_receipt.is_file() and not stale_receipt.is_symlink():
                stale_receipt.unlink()

    return {
        "status": "built",
        "schema": SCHEMA,
        "build_id": build_id,
        "output_dir": str(output_dir),
        "counts": manifest["counts"],
        "written_sha256": written_hashes,
    }


def build_artifacts(
    repo: Path,
    output_dir: Path | None = None,
    as_of: str | None = None,
    expected_point_count: int | None = EXPECTED_POINT_COUNT,
) -> dict[str, Any]:
    _retired_write_surface()
    root = repo.resolve()
    target = (output_dir or root / DEFAULT_OUTPUT_RELATIVE).resolve()
    with _projection_lock(target):
        return _build_artifacts_unlocked(
            root,
            output_dir=target,
            as_of=as_of,
            expected_point_count=expected_point_count,
        )


def build_all(
    repo: str | Path,
    today: str | None = None,
    out_dir: str | Path | None = None,
) -> dict[str, Any]:
    """Stable public wrapper used by the Harness cold/rebuild path."""

    _retired_write_surface()
    return build_artifacts(
        repo=Path(repo),
        output_dir=Path(out_dir) if out_dir is not None else None,
        as_of=today,
        expected_point_count=EXPECTED_POINT_COUNT,
    )


def load_projection(state_dir: str | Path) -> dict[str, dict[str, Any]]:
    """Load bounded runtime projections without loading event_mappings.json."""

    state_dir = Path(state_dir)
    names = (
        "manifest",
        "catalog",
        "learner_state",
        "question_index",
        "failure_signatures",
    )
    paths = {name: state_dir / f"{name}.json" for name in names}
    missing = [str(path) for path in paths.values() if not path.exists()]
    if missing:
        raise PersonalizationIndexError(
            "personalization projection unavailable: " + ", ".join(missing)
        )
    return {
        name: json.loads(path.read_text(encoding="utf-8"))
        for name, path in paths.items()
    }


def apply_event_to_projection(
    event: dict[str, Any],
    *,
    catalog: dict[str, Any],
    learner_state: dict[str, Any],
    question_index: dict[str, Any],
    as_of: str | None = None,
    prior_mapping: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Apply one canonical event as an in-memory, idempotent hot-path overlay.

    The function never reads ``events.jsonl`` or ``event_mappings.json``.  It
    mutates only the supplied derived ``learner_state`` and ``question_index``
    objects.  The caller decides whether to persist those disposable objects.
    ``prior_mapping`` is only needed when a repair event must inherit its outcome
    identity; ordinary answer-turn outcomes do not need it.
    """

    if not isinstance(event, dict) or not str(event.get("event_id") or "").strip():
        raise PersonalizationIndexError("incremental event requires event_id")
    as_of = as_of or datetime.now(ZoneInfo("Asia/Shanghai")).date().isoformat()
    if not _valid_date(as_of):
        raise PersonalizationIndexError(f"invalid overlay as_of date: {as_of}")

    nodes: dict[str, dict[str, Any]] = {}
    node_to_units: dict[str, list[str]] = {}
    unit_to_nodes: dict[str, list[str]] = {}
    for node_id, question in question_index.get("questions", {}).items():
        nodes[node_id] = {
            "node_id": node_id,
            "subject": question.get("subject", node_id[:2]),
            "subject_name": question.get(
                "subject_name", SUBJECT_NAMES.get(node_id[:2], "")
            ),
            "primary_point_ids": list(question.get("primary_point_ids", [])),
            "point_ids": list(question.get("point_ids", [])),
            "formal_last_review_date": question.get("formal_last_review_date"),
            "reliable_source": question.get(
                "reliable_source",
                {
                    "status": "unverified_or_missing",
                    "source_id": None,
                    "canonical_exam_id": None,
                    "detail_uri": None,
                    "source_ref": None,
                    "source_ref_kind": "missing",
                    "reference_fingerprint": sha256_bytes(b""),
                    "original_question_eligible": False,
                },
            ),
        }
        units = sorted(
            mechanism_id
            for mechanism_id in question.get("mechanism_ids", [])
            if REVIEW_UNIT_RE.fullmatch(mechanism_id)
        )
        if units:
            node_to_units[node_id] = units
            for unit_id in units:
                unit_to_nodes.setdefault(unit_id, []).append(node_id)
    unit_to_nodes = {
        key: sorted(set(value)) for key, value in unit_to_nodes.items()
    }

    single_event = dict(event)
    # map_events inherits repairs through a preceding outcome.  For an isolated
    # hot-path repair, seed that compact mapping through controlled fields only.
    seed_events: list[dict[str, Any]] = []
    if single_event.get("event_kind") == "repair" and prior_mapping:
        seed_events.append(
            {
                "event_id": f"OVERLAY-SEED-{single_event['event_id']}",
                "event_kind": "outcome",
                "session_id": single_event.get("session_id"),
                "item_id": single_event.get("item_id"),
                "observed_date": prior_mapping.get("observed_date"),
                "mapped_formal_node_ids": prior_mapping.get("formal_node_ids", []),
                "knowledge_point": (
                    prior_mapping.get("point_ids") or [None]
                )[0],
                "review_unit_id": (
                    prior_mapping.get("mechanism_ids") or [None]
                )[0],
                "first_result": "uncertain",
            }
        )
    overlay_mappings = map_events(
        seed_events + [single_event],
        catalog,
        nodes,
        node_to_units,
        unit_to_nodes,
    )
    mapping = overlay_mappings["events"][str(single_event["event_id"])]

    applied_event_ids = learner_state.setdefault(
        "applied_performance_event_ids", []
    )
    if (
        mapping["event_kind"] in PERFORMANCE_EVENT_KINDS
        and mapping["event_id"] in applied_event_ids
    ):
        return {
            "status": "already_applied",
            "update_mode": "incremental_overlay",
            "event_id": str(single_event["event_id"]),
            "mapping": mapping,
            "affected": {
                "point_ids": [],
                "mechanism_ids": [],
                "question_node_ids": [],
            },
        }

    affected = {"point_ids": [], "mechanism_ids": [], "question_node_ids": []}
    unresolved_applied = False
    if mapping["event_kind"] in PERFORMANCE_EVENT_KINDS:
        for point_id in mapping["point_ids"]:
            state = learner_state.setdefault("points", {}).setdefault(
                point_id, _new_sparse_state(point_id)
            )
            if _apply_performance(state, single_event, mapping, as_of):
                affected["point_ids"].append(point_id)
        for mechanism_id in mapping["mechanism_ids"]:
            state = learner_state.setdefault("mechanisms", {}).setdefault(
                mechanism_id, _new_sparse_state(mechanism_id)
            )
            if _apply_performance(state, single_event, mapping, as_of):
                affected["mechanism_ids"].append(mechanism_id)
        if mapping["event_kind"] == "outcome" and mapping.get("practice_mode") != "variant":
            for node_id in mapping["question_node_ids"]:
                state = learner_state.setdefault("questions", {}).setdefault(
                    node_id, _new_sparse_state(node_id)
                )
                if _apply_performance(state, single_event, mapping, as_of):
                    affected["question_node_ids"].append(node_id)
                question = question_index.get("questions", {}).get(node_id)
                if question is not None:
                    ledger_last = state.get("last_attempt_date")
                    formal_last = question.get("formal_last_review_date")
                    effective = _max_date(formal_last, ledger_last)
                    question["ledger_last_attempt_date"] = ledger_last
                    question["effective_last_attempt_date"] = effective
                    question["ledger_evidence_event_ids"] = state.get(
                        "evidence_event_ids", []
                    )
                    if effective and formal_last == effective and ledger_last == effective:
                        source = "formal_and_ledger"
                    elif effective and ledger_last == effective:
                        source = "review_ledger"
                    elif effective:
                        source = "formal_node"
                    else:
                        source = "unrecorded"
                    question["effective_last_attempt_source"] = source

                for point_id in question.get("point_ids", []):
                    for reference in question_index.get("by_point", {}).get(
                        point_id, []
                    ):
                        if reference.get("node_id") == node_id:
                            reference["effective_last_attempt_date"] = effective
                for mechanism_id in question.get("mechanism_ids", []):
                    for reference in question_index.get("by_mechanism", {}).get(
                        mechanism_id, []
                    ):
                        if reference.get("node_id") == node_id:
                            reference["effective_last_attempt_date"] = effective

        if mapping["mapping_status"] != "resolved":
            unresolved_state = learner_state.setdefault(
                "evidence_insufficient",
                {"count": 0, "event_ids": [], "event_ids_truncated": False},
            )
            if mapping["event_id"] not in unresolved_state["event_ids"]:
                unresolved_state["count"] = int(unresolved_state.get("count", 0)) + 1
                unresolved_state["event_ids"].append(mapping["event_id"])
                if len(unresolved_state["event_ids"]) > MAX_RECENT_UNRESOLVED_EVENT_IDS:
                    unresolved_state["event_ids"] = unresolved_state["event_ids"][
                        -MAX_RECENT_UNRESOLVED_EVENT_IDS:
                    ]
                    unresolved_state["event_ids_truncated"] = True
                unresolved_applied = True

    event_applied = any(affected.values()) or unresolved_applied
    if event_applied:
        learner_state["source_event_count"] = int(
            learner_state.get("source_event_count", 0)
        ) + 1
        learner_state["performance_event_count"] = int(
            learner_state.get("performance_event_count", 0)
        ) + 1
    if mapping["event_kind"] in PERFORMANCE_EVENT_KINDS:
        applied_event_ids.append(mapping["event_id"])

    learner_state["point_state_count"] = len(learner_state.get("points", {}))
    learner_state["mechanism_state_count"] = len(
        learner_state.get("mechanisms", {})
    )
    learner_state["question_state_count"] = len(
        learner_state.get("questions", {})
    )
    learner_state["as_of"] = as_of
    return {
        "status": "applied" if event_applied else "no_state_change",
        "update_mode": "incremental_overlay",
        "event_id": str(single_event["event_id"]),
        "mapping": mapping,
        "affected": affected,
    }


def query_projection(state_dir: Path, kind: str, entity_id: str) -> dict[str, Any]:
    """Read one bounded entity from existing projections; never rebuild."""

    projection = load_projection(state_dir)
    loaded = {
        "manifest": projection["manifest"],
        "catalog": projection["catalog"],
        "learner": projection["learner_state"],
        "questions": projection["question_index"],
    }
    manifest = projection["manifest"]

    if kind == "point":
        canonical_id = entity_id.upper()
        static = loaded["catalog"]["points"].get(canonical_id)
        if static is None:
            raise PersonalizationIndexError(f"unknown point: {entity_id}")
        return {
            "status": "ok",
            "kind": kind,
            "id": canonical_id,
            "build_id": manifest["build_id"],
            "catalog": static,
            "state": loaded["learner"]["points"].get(canonical_id),
            "questions": loaded["questions"]["by_point"].get(canonical_id, []),
        }
    if kind == "mechanism":
        canonical_id = entity_id.upper()
        static = loaded["questions"]["mechanisms"].get(canonical_id)
        state = loaded["learner"]["mechanisms"].get(canonical_id)
        if static is None and state is None:
            raise PersonalizationIndexError(f"unknown mechanism: {entity_id}")
        return {
            "status": "ok",
            "kind": kind,
            "id": canonical_id,
            "build_id": manifest["build_id"],
            "catalog": static,
            "state": state,
            "questions": loaded["questions"]["by_mechanism"].get(canonical_id, []),
        }
    if kind == "question":
        canonical_id = entity_id.upper()
        question = loaded["questions"]["questions"].get(canonical_id)
        if question is None:
            raise PersonalizationIndexError(f"unknown question: {entity_id}")
        return {
            "status": "ok",
            "kind": kind,
            "id": canonical_id,
            "build_id": manifest["build_id"],
            "question": question,
            "state": loaded["learner"]["questions"].get(canonical_id),
        }
    raise PersonalizationIndexError(f"unsupported query kind: {kind}")


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)

    build = subparsers.add_parser("build", help="rebuild all disposable projections")
    build.add_argument("--repo", type=Path, default=Path.cwd())
    build.add_argument("--output-dir", type=Path)
    build.add_argument("--as-of")
    build.add_argument(
        "--expect-point-count", type=int, default=EXPECTED_POINT_COUNT
    )

    query = subparsers.add_parser("query", help="read one existing projected entity")
    query.add_argument("--repo", type=Path, default=Path.cwd())
    query.add_argument("--state-dir", type=Path)
    query.add_argument(
        "--kind", choices=("point", "mechanism", "question"), required=True
    )
    query.add_argument("--id", required=True)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    try:
        if args.command == "build":
            result = build_artifacts(
                repo=args.repo,
                output_dir=args.output_dir,
                as_of=args.as_of,
                expected_point_count=args.expect_point_count,
            )
        else:
            state_dir = (
                args.state_dir or args.repo.resolve() / DEFAULT_OUTPUT_RELATIVE
            ).resolve()
            result = query_projection(state_dir, args.kind, args.id)
        print(json.dumps(result, ensure_ascii=False, sort_keys=True, indent=2))
        return 0
    except (OSError, ValueError, PersonalizationIndexError) as exc:
        print(
            json.dumps(
                {"status": "error", "schema": SCHEMA, "error": str(exc)},
                ensure_ascii=False,
                sort_keys=True,
            ),
            file=sys.stderr,
        )
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
