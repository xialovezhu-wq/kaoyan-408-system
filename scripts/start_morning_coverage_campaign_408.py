#!/usr/bin/env python3
"""Shadow control plane for the due-driven 408 morning coverage campaign.

The producer is deterministic and offline.  It reuses the node scheduler,
canonical exposure release, verified variant bank, and bounded campaign runtime.
It never authors a question, invokes a model, mutates a legacy session, or
writes the formal knowledge/review stores.
"""

from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import os
import re
import stat
import tempfile
from pathlib import Path
from typing import Any, Callable, Sequence

import exam_variant_bank_408 as variant_bank
import formal_original_surface_408 as formal_original
import morning_campaign_runtime_408 as campaign_runtime
import morning_coverage_campaign_408 as scheduler
import question_exposure_408 as question_exposure


CONTROL_SCHEMA = "morning-campaign-control-v1"
PREPARATION_SCHEMA = "morning-campaign-preparation-v1"
SELECTION_MANIFEST_SCHEMA = "morning-campaign-selection-manifest-v1"
ITEM_COMMITMENT_SCHEMA = "morning-campaign-item-commitment-v1"
SEGMENT_VERIFICATION_SCHEMA = "morning-campaign-segment-verification-v1"
POINTER_SCHEMA = "morning-campaign-current-pointer-v1"
POLICY_VERSION = "morning-campaign-shadow-control-v1"

FORMAL_NODE_RE = re.compile(r"^(?:DS|CO|OS|CN)_(?:\d{4}|UNK)_\d{3}$")
CANONICAL_RE = re.compile(r"^EXAM408-20(?:0[9]|1\d|2[0-5])-Q(?:0[1-9]|[1-3]\d|4[0-7])$")
KNOWLEDGE_RE = re.compile(r"^(?:DS|CO|OS|CN)\d{2}-\d{2}$")
# 本题路径：详情入口 记录本节点自己的规范真题来源定位。它不是别处的同点题，
# 因此与 来源ID 一样属于 exact_sources；只接受带“规范真题语料(定位)”前缀的
# 编号，避免把单元格里顺带提到的其它真题当成本节点来源。
DETAIL_LOCATOR_RE = re.compile(r"规范真题语料(?:定位)?\s*(EXAM408-20(?:0[9]|1\d|2[0-5])-Q(?:0[1-9]|[1-3]\d|4[0-7]))")
SHA_RE = re.compile(r"^[0-9a-f]{64}$")
SAFE_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:@/+\-]{0,191}$")

DEFAULT_DETAILS_ROOT = Path(
    "/Users/your-user/Documents/kaoyan-408-details"
)
DEFAULT_RELEASE = (
    DEFAULT_DETAILS_ROOT
    / "corpus/408统考真题/style-releases"
    / variant_bank.FINAL_PRODUCTION_RELEASE_ID
)
DEFAULT_CONTROL_REL = Path(".private/morning-campaign-control")
DEFAULT_RUNTIME_REL = Path(".private/morning-campaign-runtime")
LEGACY_RUNTIME_REL = Path("wiki/study_vaults/408-full/state/review-loop/runtime.json")
LEGACY_SESSIONS_REL = Path("wiki/study_vaults/408-full/state/morning-review")


class CampaignControlError(RuntimeError):
    """Stable fail-closed error for the shadow control plane."""


def _canonical_bytes(value: Any) -> bytes:
    return (
        json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        + "\n"
    ).encode("utf-8")


def _pretty_bytes(value: Any) -> bytes:
    return (
        json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2) + "\n"
    ).encode("utf-8")


def _digest(value: Any) -> str:
    raw = value if isinstance(value, bytes) else _canonical_bytes(value)
    return hashlib.sha256(raw).hexdigest()


def _file_sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _safe_date(value: str) -> str:
    try:
        return dt.date.fromisoformat(value).isoformat()
    except ValueError as exc:
        raise CampaignControlError("review_date must be YYYY-MM-DD") from exc


def _regular_json(path: Path, label: str) -> dict[str, Any]:
    try:
        info = path.lstat()
    except OSError as exc:
        raise CampaignControlError(f"{label}_missing") from exc
    if stat.S_ISLNK(info.st_mode) or not stat.S_ISREG(info.st_mode):
        raise CampaignControlError(f"{label}_unsafe")
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise CampaignControlError(f"{label}_invalid_json") from exc
    if not isinstance(value, dict):
        raise CampaignControlError(f"{label}_root_not_object")
    return value


def _resolve_details_ref(details_root: Path, value: object, label: str) -> Path:
    raw = Path(str(value or ""))
    if not raw.parts or ".." in raw.parts:
        raise CampaignControlError(f"{label}_ref_unsafe")
    lexical = raw if raw.is_absolute() else details_root / raw
    try:
        path = lexical.resolve(strict=True)
        path.relative_to(details_root.resolve(strict=True))
    except (OSError, ValueError) as exc:
        raise CampaignControlError(f"{label}_ref_escape") from exc
    if path.is_symlink() or not path.is_file():
        raise CampaignControlError(f"{label}_unsafe")
    return path


def _ensure_dir(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True, mode=0o700)
    info = path.lstat()
    if stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode):
        raise CampaignControlError(f"managed_directory_unsafe:{path}")
    os.chmod(path, 0o700)


def _atomic_json(path: Path, value: Any) -> str:
    payload = _pretty_bytes(value)
    _ensure_dir(path.parent)
    descriptor, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        os.fchmod(descriptor, 0o600)
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
        os.chmod(path, 0o600)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)
    return hashlib.sha256(payload).hexdigest()


def _content_json(directory: Path, value: Any) -> tuple[Path, str]:
    payload = _pretty_bytes(value)
    digest = hashlib.sha256(payload).hexdigest()
    path = directory / f"{digest}.json"
    if path.exists():
        if path.is_symlink() or path.read_bytes() != payload:
            raise CampaignControlError("content_addressed_object_conflict")
    else:
        _atomic_json(path, value)
    return path, digest


def _repo_root(repo: str | Path) -> Path:
    root = Path(repo).expanduser().resolve(strict=True)
    if root.is_symlink() or not root.is_dir():
        raise CampaignControlError("repo_unsafe")
    return root


def _roots(
    repo: Path,
    control_root: str | Path | None,
    runtime_root: str | Path | None,
) -> tuple[Path, Path]:
    control = (
        Path(control_root).expanduser().resolve()
        if control_root is not None
        else (repo / DEFAULT_CONTROL_REL).resolve()
    )
    runtime = (
        Path(runtime_root).expanduser().resolve()
        if runtime_root is not None
        else (repo / DEFAULT_RUNTIME_REL).resolve()
    )
    return control, runtime


def _legacy_state(repo: Path, session_id: str) -> dict[str, Any]:
    if not SAFE_ID_RE.fullmatch(session_id):
        raise CampaignControlError("legacy_session_id_unsafe")
    return _regular_json(
        repo / LEGACY_SESSIONS_REL / session_id / "state.json", "legacy_session_state"
    )


def legacy_session_gate(repo: str | Path) -> dict[str, Any]:
    """Return a zero-write blocker for any legacy active or paused session.

    The runtime pointer is authoritative when present.  A bounded directory scan
    catches an active/paused state hidden by a stale or missing pointer.
    """

    root = _repo_root(repo)
    runtime_path = root / LEGACY_RUNTIME_REL
    runtime: dict[str, Any] = {}
    if runtime_path.exists():
        runtime = _regular_json(runtime_path, "legacy_runtime")
    pointer = runtime.get("active_session_id")
    candidates: list[dict[str, Any]] = []
    if isinstance(pointer, str) and pointer:
        state = _legacy_state(root, pointer)
        if state.get("status") in {"in_progress", "paused"}:
            candidates.append(
                {
                    "session_id": pointer,
                    "status": state.get("status"),
                    "review_date": state.get("review_date"),
                    "source": "legacy_runtime_pointer",
                }
            )
        else:
            raise CampaignControlError("legacy_runtime_pointer_not_active")
    sessions_root = root / LEGACY_SESSIONS_REL
    if sessions_root.exists():
        if sessions_root.is_symlink() or not sessions_root.is_dir():
            raise CampaignControlError("legacy_sessions_root_unsafe")
        for state_path in sorted(sessions_root.glob("*/state.json")):
            session_id = state_path.parent.name
            if any(row["session_id"] == session_id for row in candidates):
                continue
            state = _regular_json(state_path, "legacy_session_state")
            if state.get("status") in {"in_progress", "paused"}:
                candidates.append(
                    {
                        "session_id": session_id,
                        "status": state.get("status"),
                        "review_date": state.get("review_date"),
                        "source": "legacy_session_scan",
                    }
                )
    if candidates:
        candidates.sort(key=lambda row: (str(row.get("review_date") or ""), row["session_id"]))
        return {
            "status": "blocked_legacy_session",
            "legacy_sessions": candidates,
            "legacy_session_count": len(candidates),
            "write_count": 0,
            "formal_write_count": 0,
        }
    return {
        "status": "clear",
        "legacy_sessions": [],
        "legacy_session_count": 0,
        "write_count": 0,
        "formal_write_count": 0,
    }


def _markdown_table(path: Path, id_column: str) -> list[dict[str, str]]:
    lines = path.read_text(encoding="utf-8").splitlines()
    header: list[str] | None = None
    rows: list[dict[str, str]] = []
    for line in lines:
        if not line.lstrip().startswith("|"):
            continue
        cells = [cell.strip() for cell in line.strip().strip("|").split("|")]
        if header is None:
            if id_column in cells:
                header = cells
            continue
        if all(cell and set(cell) <= {"-", ":"} for cell in cells):
            continue
        if len(cells) == len(header):
            rows.append(dict(zip(header, cells)))
    if header is None:
        raise CampaignControlError(f"table_header_missing:{path.name}")
    return rows


def _node_snapshot(repo: Path) -> dict[str, dict[str, Any]]:
    rows = _markdown_table(repo / "节点总表.md", "ID")
    result: dict[str, dict[str, Any]] = {}
    for raw in rows:
        node_id = raw.get("ID", "")
        if not FORMAL_NODE_RE.fullmatch(node_id) or node_id in result:
            raise CampaignControlError("formal_node_table_identity_invalid")
        row = {key: value for key, value in raw.items()}
        exact_sources = set(CANONICAL_RE.findall(row.get("来源ID", "")))
        exact_sources.update(DETAIL_LOCATOR_RE.findall(str(row.get("详情入口", ""))))
        result[node_id] = {
            "row": row,
            "formal_node_sha256": _digest(row),
            "main_knowledge": _main_knowledge(row.get("主知识点", "")),
            "exact_sources": sorted(exact_sources),
        }
    return result


def _main_knowledge(value: object) -> str:
    token = str(value or "").strip().split(maxsplit=1)[0]
    return token if KNOWLEDGE_RE.fullmatch(token) else ""


def load_style_release(release_dir: str | Path) -> dict[str, Any]:
    """Load the variant-bank verified release snapshot without widening authority."""

    try:
        return variant_bank._release_snapshot(release_dir)  # type: ignore[attr-defined]
    except variant_bank.VariantBankError as exc:
        raise CampaignControlError(f"exam_style_release_invalid:{exc}") from exc


def map_recent_exposures(
    exposure_set: dict[str, Any],
    release: dict[str, Any],
    *,
    variant_entries: list[dict[str, Any]] | None = None,
    formal_original_entries: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Bind legacy asset/stem identities to exact canonical/variant true_item_id."""

    asset_map: dict[str, set[str]] = {}
    identity_map: dict[str, set[str]] = {}
    exposure_versions = [release["exposure_by_id"]]
    release_path = Path(release.get("release_dir") or ".")
    if release_path.parent == variant_bank.PRODUCTION_RELEASE_ROOT.resolve():
        # A repaired crop is the same exam. Retain aliases of both accepted
        # releases so an old image exposure cannot evade the seven-day gate.
        for release_id in sorted(variant_bank.ACCEPTED_PRODUCTION_RELEASE_IDS - {release.get("release_id")}):
            other_path = release_path.parent / release_id
            if other_path.is_dir():
                exposure_versions.append(load_style_release(other_path)["exposure_by_id"])
    for canonical_id, exposure in (pair for version in exposure_versions for pair in version.items()):
        for asset in exposure.get("question_asset_refs") or []:
            if not isinstance(asset, dict) or not SHA_RE.fullmatch(str(asset.get("sha256") or "")):
                continue
            asset_sha = str(asset["sha256"])
            asset_map.setdefault(asset_sha, set()).add(canonical_id)
            material = {"kind": "question_asset_sha256", "question_asset_sha256": asset_sha}
            identity_map.setdefault(_digest(material), set()).add(canonical_id)
    variant_surface_map: dict[str, set[str]] = {}
    known_true_items = set(release["exposure_by_id"])
    for entry in variant_entries or []:
        surface_sha = str(entry.get("surface_sha256") or "")
        true_id = str(entry.get("variant_id") or "")
        if SHA_RE.fullmatch(surface_sha) and true_id:
            variant_surface_map.setdefault(surface_sha, set()).add(true_id)
            known_true_items.add(true_id)
    formal_asset_map: dict[str, set[str]] = {}
    formal_identity_map: dict[str, set[str]] = {}
    for entry in formal_original_entries or []:
        if entry.get("pool_status") != "verified":
            continue
        true_id = str(entry.get("true_item_id") or "")
        if not formal_original.FOS_RE.fullmatch(true_id):
            continue
        known_true_items.add(true_id)
        for asset_sha in entry.get("question_asset_sha256s") or []:
            if SHA_RE.fullmatch(str(asset_sha)):
                formal_asset_map.setdefault(str(asset_sha), set()).add(true_id)
        identity_sha = str(entry.get("text_surface_identity_sha256") or "")
        if SHA_RE.fullmatch(identity_sha):
            formal_identity_map.setdefault(identity_sha, set()).add(true_id)
    mapped: list[dict[str, Any]] = []
    unmapped: list[dict[str, Any]] = []
    for raw in exposure_set.get("rows") or []:
        if not isinstance(raw, dict):
            raise CampaignControlError("recent_exposure_row_invalid")
        candidates: set[str] = set()
        existing = str(raw.get("true_item_id") or "")
        if existing:
            candidates.add(existing)
        source_id = str(raw.get("source_id") or "")
        if source_id in known_true_items:
            candidates.add(source_id)
        asset_sha = str(raw.get("question_asset_sha256") or "")
        candidates.update(asset_map.get(asset_sha, set()))
        candidates.update(formal_asset_map.get(asset_sha, set()))
        identity_sha = str(raw.get("surface_identity_sha256") or "")
        candidates.update(identity_map.get(identity_sha, set()))
        candidates.update(formal_identity_map.get(identity_sha, set()))
        surface_sha = str(raw.get("surface_sha256") or "")
        candidates.update(variant_surface_map.get(surface_sha, set()))
        if len(candidates) == 1:
            mapped.append({**raw, "true_item_id": next(iter(candidates))})
        elif not candidates:
            unmapped.append({**raw, "mapping_reason": "identity_not_in_verified_release_or_bank"})
            mapped.append(dict(raw))
        else:
            raise CampaignControlError("recent_exposure_identity_ambiguous")
    material = {
        "schema": "learner_visible_question_exposure_set_v1",
        "review_date": exposure_set.get("review_date"),
        "window_days": exposure_set.get("window_days", 7),
        "rows": mapped,
    }
    return {
        **material,
        "exposure_count": len(mapped),
        "mapped_true_item_count": sum(bool(row.get("true_item_id")) for row in mapped),
        "unmapped_count": len(unmapped),
        "unmapped": unmapped,
        "fingerprint_sha256": _digest(material),
    }


def _candidate_ids(
    node: dict[str, Any], release: dict[str, Any]
) -> list[str]:
    exact = [
        canonical_id
        for canonical_id in node["exact_sources"]
        if canonical_id in release["exposure_by_id"]
        and release["exposure_by_id"][canonical_id].get("exposure_mode") == "direct_pool"
    ]
    same_knowledge = sorted(
        canonical_id
        for canonical_id, row in release["exposure_by_id"].items()
        if row.get("main_knowledge") == node["main_knowledge"]
        and row.get("exposure_mode") == "direct_pool"
        and row.get("direct_replay_allowed") is True
        and row.get("production_usable") is True
    )
    return exact + [candidate for candidate in same_knowledge if candidate not in exact]


def _selection(
    *,
    details_root: Path,
    release_dir: Path,
    review_date: str,
    formal_node_id: str,
    node: dict[str, Any],
    release: dict[str, Any],
    mapped_exposures: dict[str, Any],
    used_true_items: set[str],
    selector: Callable[..., dict[str, Any]],
    formal_original_selector: Callable[..., dict[str, Any]],
    variant_entries: list[dict[str, Any]],
    campaign_commitment_id: str,
    preferred_variant_id: str | None = None,
    preferred_original: dict[str, str] | None = None,
) -> tuple[dict[str, Any] | None, list[dict[str, Any]]]:
    blockers: list[dict[str, Any]] = []
    candidates = _candidate_ids(node, release)
    exact_candidates = [
        canonical_id
        for canonical_id in candidates
        if canonical_id in set(node["exact_sources"])
    ]
    fallback_candidates = [
        canonical_id
        for canonical_id in candidates
        if canonical_id not in set(exact_candidates)
    ]
    if not node["main_knowledge"]:
        return None, [{"reason": "node_main_knowledge_missing"}]
    if not candidates:
        blockers.append({"reason": "verified_direct_candidate_missing"})

    if preferred_original is not None:
        true_id = preferred_original.get("true_item_id", "")
        expected_sha = preferred_original.get("surface_sha256")
        if preferred_variant_id or true_id in used_true_items:
            return None, [{"reason": "preferred_original_conflicting_or_reserved"}]
        try:
            if CANONICAL_RE.fullmatch(true_id):
                exposure = release["exposure_by_id"].get(true_id, {})
                # The exact same exam can have a corpus primary label that is
                # an explicitly recorded secondary point of the formal node
                # (for example heap versus heapsort). Preserve exact identity
                # and require that label to be bound on the same formal row.
                bound_points = {node["main_knowledge"]}
                for field in ("副知识点", "命中知识点"):
                    bound_points.update(re.findall(r"(?:DS|CO|OS|CN)\d{2}-\d{2}",
                        str((node.get("row") or {}).get(field) or "")))
                if true_id not in exact_candidates or exposure.get("main_knowledge") not in bound_points:
                    return None, [{"reason": "preferred_original_target_binding_mismatch"}]
                selected = selector(
                    details_root, release_dir, review_date=review_date,
                    formal_node_id=formal_node_id, formal_node_sha256=node["formal_node_sha256"],
                    true_item_id=true_id, original_canonical_id=true_id,
                    exposure_set=mapped_exposures, seed=f"{review_date}:{formal_node_id}",
                    campaign_commitment_id=campaign_commitment_id, consume=False,
                    formal_node_main_knowledge=node["main_knowledge"],
                )
                required_kind = "canonical_original"
            elif re.fullmatch(r"FOS-[0-9A-F]{24}", true_id):
                entries = [row for row in formal_original.pool_status(details_root).get("entries", [])
                           if row.get("true_item_id") == true_id
                           and row.get("formal_node_id") == formal_node_id
                           and row.get("formal_node_sha256") == node["formal_node_sha256"]
                           and row.get("pool_status") == "verified"]
                if len(entries) != 1:
                    return None, [{"reason": "preferred_original_target_binding_mismatch"}]
                selected = formal_original_selector(
                    details_root, review_date=review_date, formal_node_id=formal_node_id,
                    formal_node_sha256=node["formal_node_sha256"], exposure_set=mapped_exposures,
                    campaign_commitment_id=campaign_commitment_id,
                    exact_exposure_identity=entries[0]["exact_exposure_identity"],
                    preferred_true_item_id=true_id,
                    allow_existing_campaign_reservation=True,
                )
                selected = {**selected, "surface_kind": "formal_original", "variant_id": None}
                required_kind = "formal_original"
            else:
                return None, [{"reason": "preferred_original_identity_invalid"}]
        except (variant_bank.VariantBankError, formal_original.FormalOriginalSurfaceError) as exc:
            return None, [{"reason": f"preferred_original:{exc}"}]
        if (selected.get("status") != "selected" or selected.get("true_item_id") != true_id
                or selected.get("surface_kind") != required_kind
                or (expected_sha is not None and selected.get("surface_sha256") != expected_sha)):
            return None, [{"reason": "preferred_original_unavailable_or_surface_drift"}]
        return selected, blockers

    if preferred_variant_id is not None:
        preferred = [
            row
            for row in variant_entries
            if row.get("variant_id") == preferred_variant_id
            and row.get("formal_node_id") == formal_node_id
            and row.get("main_knowledge") == node["main_knowledge"]
            and row.get("pool_status") in {"verified", "consumed"}
        ]
        if len(preferred) != 1:
            return None, [{"reason": "preferred_variant_not_available"}]
        anchors = preferred[0].get("canonical_anchor_ids") or []
        if not isinstance(anchors, list) or not anchors:
            return None, [{"reason": "preferred_variant_anchor_missing"}]
        try:
            selected = selector(
                details_root,
                release_dir,
                review_date=review_date,
                formal_node_id=formal_node_id,
                formal_node_sha256=node["formal_node_sha256"],
                true_item_id=str(anchors[0]),
                original_canonical_id=str(anchors[0]),
                exposure_set=mapped_exposures,
                seed=f"{review_date}:{formal_node_id}",
                campaign_commitment_id=campaign_commitment_id,
                consume=False,
                variant_only=True,
                formal_node_main_knowledge=node["main_knowledge"],
                preferred_variant_id=preferred_variant_id,
            )
        except variant_bank.VariantBankError as exc:
            return None, [{"reason": f"preferred_variant:{exc}"}]
        if (
            selected.get("status") != "selected"
            or selected.get("true_item_id") != preferred_variant_id
            or preferred_variant_id in used_true_items
        ):
            return None, [
                {
                    "reason": str(
                        selected.get("reason")
                        or selected.get("status")
                        or "preferred_variant_reserved_or_unavailable"
                    )
                }
            ]
        return selected, blockers

    def try_canonical(
        candidate_ids: list[str], *, variant_only: bool = False
    ) -> dict[str, Any] | None:
        for canonical_id in candidate_ids:
            if not variant_only and canonical_id in used_true_items:
                blockers.append(
                    {
                        "canonical_id": canonical_id,
                        "reason": "true_item_already_reserved",
                    }
                )
                continue
            try:
                selected = selector(
                    details_root,
                    release_dir,
                    review_date=review_date,
                    formal_node_id=formal_node_id,
                    formal_node_sha256=node["formal_node_sha256"],
                    true_item_id=canonical_id,
                    original_canonical_id=canonical_id,
                    exposure_set=mapped_exposures,
                    seed=f"{review_date}:{formal_node_id}",
                    campaign_commitment_id=campaign_commitment_id,
                    consume=False,
                    variant_only=variant_only,
                    formal_node_main_knowledge=node["main_knowledge"],
                )
            except variant_bank.VariantBankError as exc:
                blockers.append({"canonical_id": canonical_id, "reason": str(exc)})
                continue
            if selected.get("status") == "selected":
                true_item_id = str(selected.get("true_item_id") or "")
                if not true_item_id or true_item_id in used_true_items:
                    blockers.append(
                        {
                            "canonical_id": canonical_id,
                            "reason": "selected_true_item_duplicate",
                        }
                    )
                    continue
                return selected
            reason = str(
                selected.get("reason")
                or selected.get("status")
                or "surface_unavailable"
            )
            blockers.append({"canonical_id": canonical_id, "reason": reason})
        return None

    selected = try_canonical(exact_candidates)
    if selected is not None:
        return selected, blockers
    try:
        formal_selected = formal_original_selector(
            details_root,
            review_date=review_date,
            formal_node_id=formal_node_id,
            formal_node_sha256=node["formal_node_sha256"],
            exposure_set=mapped_exposures,
            campaign_commitment_id=campaign_commitment_id,
            allow_existing_campaign_reservation=True,
        )
    except formal_original.FormalOriginalSurfaceError as exc:
        blockers.append({"reason": f"formal_original:{exc}"})
    else:
        if formal_selected.get("status") == "selected":
            true_item_id = str(formal_selected.get("true_item_id") or "")
            if true_item_id and true_item_id not in used_true_items:
                return {
                    **formal_selected,
                    "surface_kind": "formal_original",
                    "variant_id": None,
                }, blockers
            blockers.append({"reason": "selected_formal_original_duplicate"})
        else:
            blockers.append({"reason": "verified_formal_original_unavailable"})
    variant_anchor_candidates = sorted(
        {
            str(anchor)
            for row in variant_entries
            if isinstance(row, dict)
            and (
                row.get("pool_status") == "verified"
                or (
                    row.get("pool_status") == "consumed"
                    and row.get("consumption_campaign_commitment_id")
                    == campaign_commitment_id
                )
            )
            and row.get("formal_node_id") == formal_node_id
            and (
                row.get("formal_node_sha256") == node["formal_node_sha256"]
                or row.get("main_knowledge") == node["main_knowledge"]
            )
            for anchor in row.get("canonical_anchor_ids") or []
            if str(anchor) in release["exposure_by_id"]
        }
    )
    selected = try_canonical(variant_anchor_candidates, variant_only=True)
    if selected is not None:
        return selected, blockers
    selected = try_canonical(fallback_candidates)
    if selected is not None:
        return selected, blockers
    return None, blockers


def _variant_manifest(
    details_root: Path, selection: dict[str, Any], pool_entries: list[dict[str, Any]]
) -> dict[str, Any] | None:
    if selection.get("surface_kind") != "verified_variant":
        return None
    matches = [
        row for row in pool_entries if row.get("variant_id") == selection.get("variant_id")
    ]
    if len(matches) != 1:
        raise CampaignControlError("selected_variant_manifest_not_unique")
    ref = Path(str(matches[0]["manifest_ref"]))
    path = ref if ref.is_absolute() else details_root / ref
    manifest = _regular_json(path, "selected_variant_manifest")
    if _file_sha256(path) != matches[0].get("manifest_sha256"):
        raise CampaignControlError("selected_variant_manifest_hash_drift")
    return manifest


def _item_commitment(
    *,
    due: dict[str, Any],
    node: dict[str, Any],
    selection: dict[str, Any],
    release: dict[str, Any],
    details_root: Path,
    pool_entries: list[dict[str, Any]],
    ordinal: int,
) -> dict[str, Any]:
    surface_kind = str(selection.get("surface_kind") or "")
    canonical_id = str(selection.get("canonical_id") or selection.get("blocked_original_true_item_id") or "")
    variant_manifest = _variant_manifest(details_root, selection, pool_entries)
    acceptance_kind = str(selection.get("acceptance_kind") or "")
    web_solution_sha: str | None = None
    web_self_check_sha: str | None = None
    if surface_kind == "formal_original":
        manifest_ref = selection.get("manifest_ref")
        manifest_path = _resolve_details_ref(details_root, manifest_ref, "formal_original_manifest")
        formal_manifest = _regular_json(manifest_path, "formal_original_manifest")
        if (
            _file_sha256(manifest_path) != selection.get("manifest_sha256")
            or formal_manifest.get("schema") != formal_original.MANIFEST_SCHEMA
            or formal_manifest.get("true_item_id") != selection.get("true_item_id")
            or formal_manifest.get("formal_node_id") != due["formal_node_id"]
        ):
            raise CampaignControlError("formal_original_manifest_binding_drift")
        canonical_id = due["formal_node_id"]
        author_sha = str(formal_manifest.get("details_card_sha256") or "")
        blind_sha = str(formal_manifest.get("blind_verification_sha256") or "")
        adversarial_sha = str(
            formal_manifest.get("source_solution_verification_sha256") or ""
        )
        grading_sha = str(selection.get("answer_capsule_commitment_sha256") or "")
        acceptance_kind = "formal_original_verified_ready"
    elif variant_manifest is None:
        exposure = release["exposure_by_id"].get(canonical_id)
        if not isinstance(exposure, dict):
            raise CampaignControlError("selected_original_exposure_missing")
        author_sha = _digest(exposure)
        blind_sha = _digest(
            {
                "question_asset_refs": selection.get("question_asset_refs"),
                "answer_capsule_commitment_ref": selection.get("answer_capsule_commitment_ref"),
                "answer_capsule_commitment_sha256": selection.get("answer_capsule_commitment_sha256"),
                "direct_replay_allowed": exposure.get("direct_replay_allowed"),
                "production_usable": exposure.get("production_usable"),
            }
        )
        adversarial_sha = _digest(
            {
                "selection_id": selection.get("selection_id"),
                "formal_node_sha256": node["formal_node_sha256"],
                "release_content_sha256": release["content_sha256"],
            }
        )
        grading_sha = str(selection.get("answer_capsule_commitment_sha256") or "")
        acceptance_kind = "canonical_original_verified_ready"
    else:
        manifest_acceptance = str(
            variant_manifest.get("acceptance_kind")
            or variant_bank.INDEPENDENT_ACCEPTANCE_KIND
        )
        if acceptance_kind and acceptance_kind != manifest_acceptance:
            raise CampaignControlError("variant_acceptance_kind_drift")
        acceptance_kind = manifest_acceptance
        if acceptance_kind == variant_bank.WEB_ACCEPTANCE_KIND:
            author_sha = str(variant_manifest.get("web_submission_sha256") or "")
            blind_sha = None
            adversarial_sha = None
            web_solution_sha = str(variant_manifest.get("web_solution_sha256") or "")
            web_self_check_sha = str(variant_manifest.get("web_self_check_sha256") or "")
            evidence_path = _resolve_details_ref(
                details_root,
                variant_manifest.get("web_submission_ref"),
                "web_variant_submission",
            )
            solution_path = _resolve_details_ref(
                details_root,
                variant_manifest.get("web_solution_ref"),
                "web_variant_solution",
            )
            grading_path = _resolve_details_ref(
                details_root,
                variant_manifest.get("web_grading_ref"),
                "web_variant_grading",
            )
            self_check_path = _resolve_details_ref(
                details_root,
                variant_manifest.get("web_self_check_ref"),
                "web_variant_self_check",
            )
            evidence = _regular_json(evidence_path, "web_variant_submission")
            grading_evidence = _regular_json(grading_path, "web_variant_grading")
            solution_evidence = _regular_json(solution_path, "web_variant_solution")
            self_check_evidence = _regular_json(self_check_path, "web_variant_self_check")
            if (
                variant_manifest.get("independent_reviewed") is not False
                or variant_manifest.get("independent_actor_count") != 0
                or _file_sha256(evidence_path) != author_sha
                or _file_sha256(grading_path)
                != variant_manifest.get("web_grading_sha256")
                or _file_sha256(solution_path) != web_solution_sha
                or _file_sha256(self_check_path) != web_self_check_sha
                or evidence.get("schema") != variant_bank.WEB_SUBMISSION_SCHEMA
                or evidence.get("question_id") != due["formal_node_id"]
                or evidence.get("formal_node_sha256") != node["formal_node_sha256"]
                or evidence.get("target_concept_id") != node["main_knowledge"]
                or evidence.get("acceptance_kind") != variant_bank.WEB_ACCEPTANCE_KIND
                or grading_evidence.get("schema") != variant_bank.WEB_GRADING_SCHEMA
                or solution_evidence.get("schema") != variant_bank.WEB_SOLUTION_SCHEMA
                or self_check_evidence.get("schema") != variant_bank.WEB_SELF_CHECK_SCHEMA
                or self_check_evidence.get("self_check_kind")
                != "same_actor_solution_and_counterexample"
                or self_check_evidence.get("independent_reviewed") is not False
                or self_check_evidence.get("independent_actor_ids") != []
                or set(self_check_evidence.get("checks") or {})
                != variant_bank.WEB_SELF_CHECKS
                or any(
                    value is not True
                    for value in (self_check_evidence.get("checks") or {}).values()
                )
            ):
                raise CampaignControlError("web_variant_acceptance_evidence_invalid")
        elif acceptance_kind == variant_bank.INDEPENDENT_ACCEPTANCE_KIND:
            author_sha = str(variant_manifest.get("author_sha256") or "")
            blind_sha = str(variant_manifest.get("blind_verifier_sha256") or "")
            adversarial_sha = str(variant_manifest.get("adversarial_verifier_sha256") or "")
        else:
            raise CampaignControlError("variant_acceptance_kind_invalid")
        grading_sha = str(selection.get("answer_capsule_id") or "")
    hashes = [
        str(selection.get("surface_sha256") or ""),
        grading_sha,
        str(release.get("content_sha256") or ""),
        author_sha,
    ]
    if acceptance_kind == variant_bank.WEB_ACCEPTANCE_KIND:
        hashes.extend([web_solution_sha, web_self_check_sha])
    else:
        hashes.extend([blind_sha, adversarial_sha])
    if any(not SHA_RE.fullmatch(value) for value in hashes):
        raise CampaignControlError("item_commitment_hash_missing")
    core = {
        "schema": ITEM_COMMITMENT_SCHEMA,
        "ordinal": ordinal,
        "item_id": f"MCQ-{ordinal:04d}",
        "formal_node_id": due["formal_node_id"],
        "formal_node_sha256": node["formal_node_sha256"],
        "scheduler_item_sha256": due["item_sha256"],
        "source_id": canonical_id,
        "true_item_id": selection["true_item_id"],
        "variant_id": selection.get("variant_id"),
        "surface_kind": selection["surface_kind"],
        "acceptance_kind": acceptance_kind,
        "surface_sha256": hashes[0],
        "grading_sha256": grading_sha,
        "style_release_id": release["release_id"],
        "style_commitment_sha256": hashes[2],
        "author_commitment_sha256": author_sha,
        "blind_verifier_commitment_sha256": blind_sha,
        "adversarial_verifier_commitment_sha256": adversarial_sha,
        "web_solution_commitment_sha256": web_solution_sha,
        "web_self_check_commitment_sha256": web_self_check_sha,
        "independent_reviewed": acceptance_kind
        == variant_bank.INDEPENDENT_ACCEPTANCE_KIND,
        "selection_id": selection["selection_id"],
        "selection_sha256": _digest(selection),
        "answer_isolated": True,
        "mandatory_audit": selection["surface_kind"] in {"verified_variant", "formal_original"},
        "first_variant": bool(
            selection["surface_kind"] == "verified_variant"
            and selection.get("variant_cooldown_gap_days") is None
        ),
        "recent_recurrence": bool(due.get("recent_recurrence")),
        "formal_write_count": 0,
    }
    pack_sha = _digest(core)
    return {**core, "pack_commitment_sha256": pack_sha}


def _write_preparation(
    control_root: Path,
    review_date: str,
    state: dict[str, Any],
    *,
    selection_manifest: dict[str, Any] | None = None,
    segment_receipts: list[dict[str, Any]] | None = None,
    campaign_manifest: dict[str, Any] | None = None,
) -> tuple[Path, dict[str, Any]]:
    basis = {
        "review_date": review_date,
        "scheduler_run_sha256": state.get("scheduler_run_sha256"),
        "release_content_sha256": state.get("release_content_sha256"),
        "selection_manifest_sha256": state.get("selection_manifest_sha256"),
        "status": state["status"],
    }
    preparation_id = "MCPREP-" + _digest(basis)[:24].upper()
    prep_dir = control_root / "preparations" / review_date / preparation_id
    _ensure_dir(prep_dir)
    if selection_manifest is not None:
        _atomic_json(prep_dir / "selection-manifest.json", selection_manifest)
    for receipt in segment_receipts or []:
        _atomic_json(
            prep_dir / "segment-verifications" / f"{int(receipt['segment_ordinal']):04d}.json",
            receipt,
        )
    if campaign_manifest is not None:
        _atomic_json(prep_dir / "campaign-manifest.json", campaign_manifest)
    finalized = {**state, "preparation_id": preparation_id}
    state_sha = _atomic_json(prep_dir / "state.json", finalized)
    pointer = {
        "schema": POINTER_SCHEMA,
        "review_date": review_date,
        "preparation_id": preparation_id,
        "state_ref": (prep_dir / "state.json").relative_to(control_root).as_posix(),
        "state_sha256": state_sha,
    }
    _atomic_json(control_root / "current" / f"{review_date}.json", pointer)
    return prep_dir, finalized


def prepare_campaign(
    repo: str | Path,
    *,
    review_date: str,
    details_root: str | Path = DEFAULT_DETAILS_ROOT,
    release_dir: str | Path = DEFAULT_RELEASE,
    control_root: str | Path | None = None,
    runtime_root: str | Path | None = None,
    config_path: str | Path | None = None,
    private_root: str | Path | None = None,
    scheduler_run: dict[str, Any] | None = None,
    exposure_set: dict[str, Any] | None = None,
    release_snapshot: dict[str, Any] | None = None,
    surface_selector: Callable[..., dict[str, Any]] = variant_bank.select_surface,
    formal_original_selector: Callable[..., dict[str, Any]] = formal_original.select_surface,
    pool_snapshot: dict[str, Any] | None = None,
    consumption_committer: Callable[..., dict[str, Any]] = variant_bank.commit_consumption,
) -> dict[str, Any]:
    """Prepare all due items, verify every segment, then create one ready run."""

    root = _repo_root(repo)
    day = _safe_date(review_date)
    control, runtime = _roots(root, control_root, runtime_root)
    gate = legacy_session_gate(root)
    if gate["status"] != "clear":
        return {"schema": CONTROL_SCHEMA, **gate}
    details = Path(details_root).expanduser().resolve()
    release_path = Path(release_dir).expanduser().resolve()
    release = release_snapshot or load_style_release(release_path)
    if scheduler_run is None:
        from study_bridge_408 import BridgeError, load_imported_d_run
        try:
            scheduler_run = load_imported_d_run(root, day)
        except (BridgeError, OSError, ValueError) as exc:
            raise CampaignControlError("imported_D_not_current:" + str(exc)) from exc
    if scheduler_run is None:
        config, cohort, memory, bindings, admissions = scheduler.load_projection(
            root,
            Path(config_path).expanduser().resolve() if config_path else None,
            dt.date.fromisoformat(day),
        )
        del admissions
        scheduler_run = scheduler.build_due_run(
            cohort, memory, config, dt.date.fromisoformat(day), source_bindings=bindings
        )
    if (
        scheduler_run.get("schema") != scheduler.DUE_RUN_SCHEMA
        or scheduler_run.get("run_date") != day
        or scheduler_run.get("coverage_identity_holds") is not True
        or scheduler_run.get("due_count") != scheduler_run.get("scheduled_count")
        or scheduler_run.get("skipped_due_count") != 0
    ):
        raise CampaignControlError("scheduler_due_run_not_complete")
    due_items = scheduler_run.get("due_items")
    if not isinstance(due_items, list):
        raise CampaignControlError("scheduler_due_items_invalid")
    scheduler_run_sha = str(scheduler_run.get("run_sha256") or _digest(scheduler_run))
    if scheduler_run.get("run_sha256") and scheduler_run_sha != scheduler.canonical_sha256(
        {key: value for key, value in scheduler_run.items() if key != "run_sha256"}
    ):
        raise CampaignControlError("scheduler_due_run_hash_drift")
    pool = pool_snapshot or variant_bank.pool_status(details, release_path)
    if pool.get("exam_style_release_id") != release["release_id"]:
        raise CampaignControlError("variant_pool_release_drift")
    formal_pool = formal_original.pool_status(details)
    raw_exposure = exposure_set or question_exposure.recent_exposure_set(
        root,
        dt.date.fromisoformat(day),
        private_root=private_root,
        window_days=7,
    )
    mapped = map_recent_exposures(
        raw_exposure,
        release,
        variant_entries=pool.get("entries") or [],
        formal_original_entries=formal_pool.get("entries") or [],
    )
    nodes = _node_snapshot(root)
    primary_due_points = {nodes[row["formal_node_id"]].get("main_knowledge")
                          for row in due_items if row["formal_node_id"] in nodes}
    supplemental_ids = set(scheduler_run.get("independent_supplemental_request_ids", []))
    deferred_points = set()
    admitted_supplement_points = set()
    if scheduler_run.get("local_scope_ref"):
        import study_bridge_408 as bridge
        if bridge.load_imported_d_run(root, day) != scheduler_run:
            raise CampaignControlError("D_local_subset_run_not_current_import")
        deferred_points = set(scheduler_run.get("deferred_concept_ids", []))
        ref = _regular_json(root / ".private/study-bridge/d-current" / (day + ".json"), "D_current_pointer")
        receipt = _regular_json(Path(ref["receipt_path"]), "D_current_receipt")
        admitted_supplement_points = {row["concept_id"] for row in receipt.get("supplemental_items", [])
                                     if row["request_id"] in supplemental_ids}
    missing_point_requests = [request for request in scheduler_run.get("concept_review_priority", {}).get("requests", [])
                              if request["concept_id"] not in primary_due_points
                              and request["request_id"] not in supplemental_ids
                              and request["concept_id"] not in admitted_supplement_points
                              and request["concept_id"] not in deferred_points]
    if missing_point_requests:
        return {"schema": CONTROL_SCHEMA, "status": "content_blocked", "reason": "independent_concept_supplement_plan_required",
                "concept_requests": missing_point_requests, "full_due_count": scheduler_run["due_count"],
                "next_action": "local_concept_review_408.py plan; fill targeted verified variants; prepare",
                "partial_run_created": False, "formal_write_count": 0, "learner_evidence_write_count": 0}
    commitments: list[dict[str, Any]] = []
    selected_surfaces: list[dict[str, Any]] = []
    blockers: list[dict[str, Any]] = []
    used_true_items: set[str] = set()
    preferred_variants = scheduler_run.get("preferred_variant_ids_by_formal_node", {})
    due_node_ids = {str(row.get("formal_node_id") or "") for row in due_items}
    if (
        not isinstance(preferred_variants, dict)
        or not set(preferred_variants).issubset(due_node_ids)
        or any(
            not isinstance(value, str) or not SAFE_ID_RE.fullmatch(value)
            for value in preferred_variants.values()
        )
        or len(set(preferred_variants.values())) != len(preferred_variants)
    ):
        raise CampaignControlError("preferred_variant_map_invalid")
    preferred_originals = scheduler_run.get("preferred_original_items_by_formal_node", {})
    if (not isinstance(preferred_originals, dict)
            or not set(preferred_originals).issubset(due_node_ids)
            or set(preferred_originals) & set(preferred_variants)
            or any(not isinstance(value, dict)
                   or set(value) != {"true_item_id", "surface_sha256"}
                   or not (CANONICAL_RE.fullmatch(str(value.get("true_item_id", "")))
                           or re.fullmatch(r"FOS-[0-9A-F]{24}", str(value.get("true_item_id", ""))))
                   or not SHA_RE.fullmatch(str(value.get("surface_sha256", "")))
                   for value in preferred_originals.values())
            or len({value["true_item_id"] for value in preferred_originals.values()}) != len(preferred_originals)):
        raise CampaignControlError("preferred_original_map_invalid")
    campaign_commitment_id = "MCC-" + _digest(
        {
            "review_date": day,
            "scheduler_run_sha256": scheduler_run_sha,
            "release_content_sha256": release["content_sha256"],
        }
    )[:24].upper()
    for index, due in enumerate(due_items, 1):
        if not isinstance(due, dict) or not FORMAL_NODE_RE.fullmatch(str(due.get("formal_node_id") or "")):
            raise CampaignControlError("scheduler_due_item_identity_invalid")
        node_id = str(due["formal_node_id"])
        node = nodes.get(node_id)
        if node is None:
            blockers.append({"formal_node_id": node_id, "status": "content_blocked", "reasons": [{"reason": "formal_node_missing"}]})
            continue
        selected, reasons = _selection(
            details_root=details,
            release_dir=release_path,
            review_date=day,
            formal_node_id=node_id,
            node=node,
            release=release,
            mapped_exposures=mapped,
            used_true_items=used_true_items,
            selector=surface_selector,
            formal_original_selector=formal_original_selector,
            variant_entries=list(pool.get("entries") or []),
            campaign_commitment_id=campaign_commitment_id,
            preferred_variant_id=preferred_variants.get(node_id),
            preferred_original=preferred_originals.get(node_id),
        )
        if selected is None:
            blockers.append({"formal_node_id": node_id, "status": "content_blocked", "reasons": reasons})
            continue
        try:
            commitment = _item_commitment(
                due=due,
                node=node,
                selection=selected,
                release=release,
                details_root=details,
                pool_entries=pool.get("entries") or [],
                ordinal=index,
            )
        except CampaignControlError as exc:
            blockers.append({"formal_node_id": node_id, "status": "content_blocked", "reasons": reasons + [{"reason": str(exc)}]})
            continue
        commitments.append(commitment)
        selected_surfaces.append(selected)
        used_true_items.add(str(commitment["true_item_id"]))
    base_state = {
        "schema": PREPARATION_SCHEMA,
        "policy_version": POLICY_VERSION,
        "review_date": day,
        "timezone": "Asia/Shanghai",
        "scheduler_run_sha256": scheduler_run_sha,
        "due_count": len(due_items),
        "prepared_count": len(commitments),
        "content_blocked_count": len(blockers),
        "content_blocked": blockers,
        "release_id": release["release_id"],
        "release_content_sha256": release["content_sha256"],
        "release_manifest_sha256": _file_sha256(release_path / "manifest.json") if (release_path / "manifest.json").is_file() else str(release.get("manifest_sha256") or _digest(release.get("manifest", {}))),
        "release_dir": str(release_path),
        "details_root": str(details),
        "exposure_fingerprint_sha256": mapped["fingerprint_sha256"],
        "mapped_exposure_count": mapped["mapped_true_item_count"],
        "unmapped_exposure_count": mapped["unmapped_count"],
        "learner_evidence_write_count": 0,
        "formal_write_count": 0,
        "model_call_count": 0,
    }
    if scheduler_run.get("execution_scope") == "D_concept_representatives":
        full_count = int(scheduler_run["full_due_count"])
        suppressed = scheduler_run["suppressed_carryover_formal_node_ids"]
        if full_count != len(due_items) + len(suppressed):
            raise CampaignControlError("D_full_due_denominator_mismatch")
        base_state["review_scope"] = {
            "kind": "D_concept_representatives", "full_due_node_count": full_count,
            "representative_count": len(due_items), "suppressed_unanswered_node_count": len(suppressed),
            "completion_means": "concept_tasks_not_all_due_nodes",
        }
        if scheduler_run.get("deferred_concept_ids"):
            base_state["review_scope"]["deferred_concept_ids"] = scheduler_run["deferred_concept_ids"]
            base_state["review_scope"]["completion_means"] = "user_selected_subset_not_full_concept_scope"
    if not due_items:
        prep_dir, final = _write_preparation(control, day, {**base_state, "status": "ready_empty", "selection_manifest_sha256": None, "run_id": None})
        return {"schema": CONTROL_SCHEMA, **final, "preparation_ref": str(prep_dir)}
    if blockers:
        prep_dir, final = _write_preparation(
            control,
            day,
            {**base_state, "status": "content_blocked", "selection_manifest_sha256": None, "run_id": None, "partial_run_created": False},
        )
        return {"schema": CONTROL_SCHEMA, **final, "preparation_ref": str(prep_dir)}
    selection_manifest = {
        "schema": SELECTION_MANIFEST_SCHEMA,
        "review_date": day,
        "scheduler_run_sha256": scheduler_run_sha,
        "release_id": release["release_id"],
        "release_content_sha256": release["content_sha256"],
        "item_count": len(commitments),
        "items": commitments,
        "answer_safe": True,
        "formal_write_count": 0,
        "model_call_count": 0,
    }
    selection_sha = _digest(selection_manifest)
    if len(commitments) in {30, 40, 100}:
        variant_bank.validate_campaign_commitments(
            {
                "schema": variant_bank.CAMPAIGN_COMMITMENT_SCHEMA,
                "items": [
                    {
                        "commitment_id": f"MCC-{day.replace('-', '')}-{index:04d}",
                        "true_item_id": row["true_item_id"],
                        "surface_sha256": row["surface_sha256"],
                        "answer_capsule_sha256": row["grading_sha256"],
                        "exam_style_release_content_sha256": row["style_commitment_sha256"],
                        "answer_isolated": True,
                    }
                    for index, row in enumerate(commitments, 1)
                ],
            },
            expected_count=len(commitments),
            exam_style_release_content_sha256=release["content_sha256"],
        )
    receipts: list[dict[str, Any]] = []
    for offset in range(0, len(commitments), campaign_runtime.SEGMENT_SIZE):
        chunk = commitments[offset : offset + campaign_runtime.SEGMENT_SIZE]
        core = {
            "schema": SEGMENT_VERIFICATION_SCHEMA,
            "review_date": day,
            "segment_ordinal": len(receipts),
            "item_count": len(chunk),
            "item_ids": [row["item_id"] for row in chunk],
            "pack_commitment_sha256s": [row["pack_commitment_sha256"] for row in chunk],
            "selection_manifest_sha256": selection_sha,
            "style_release_content_sha256": release["content_sha256"],
            "all_surface_bound": all(SHA_RE.fullmatch(row["surface_sha256"]) for row in chunk),
            "all_grading_bound": all(SHA_RE.fullmatch(row["grading_sha256"]) for row in chunk),
            "all_style_bound": all(row["style_commitment_sha256"] == release["content_sha256"] for row in chunk),
            "all_author_bound": all(SHA_RE.fullmatch(row["author_commitment_sha256"]) for row in chunk),
            "all_verifiers_bound": all(
                row["acceptance_kind"] != variant_bank.INDEPENDENT_ACCEPTANCE_KIND
                or (
                    SHA_RE.fullmatch(str(row["blind_verifier_commitment_sha256"] or ""))
                    and SHA_RE.fullmatch(str(row["adversarial_verifier_commitment_sha256"] or ""))
                )
                for row in chunk
            ),
            "all_web_self_checks_bound": all(
                row["acceptance_kind"] != variant_bank.WEB_ACCEPTANCE_KIND
                or (
                    row["independent_reviewed"] is False
                    and row["blind_verifier_commitment_sha256"] is None
                    and row["adversarial_verifier_commitment_sha256"] is None
                    and SHA_RE.fullmatch(str(row["web_solution_commitment_sha256"] or ""))
                    and SHA_RE.fullmatch(str(row["web_self_check_commitment_sha256"] or ""))
                )
                for row in chunk
            ),
            "answer_safe": True,
            "formal_write_count": 0,
        }
        verification_sha = _digest(core)
        receipts.append({**core, "status": "PASS", "verification_sha256": verification_sha})
    runtime_items = [
        {
            "item_id": row["item_id"],
            "source_id": row["source_id"],
            "formal_node_id": row["formal_node_id"],
            "action_id": None,
            "surface_sha256": row["surface_sha256"],
            "grading_sha256": row["grading_sha256"],
            "pack_commitment_sha256": row["pack_commitment_sha256"],
            "mandatory_audit": row["mandatory_audit"],
            "true_item_id": row["true_item_id"],
            "variant_id": row.get("variant_id"),
            "surface_kind": row["surface_kind"],
            "campaign_item_commitment_sha256": row[
                "pack_commitment_sha256"
            ],
            "first_variant": row["first_variant"],
            "recent_recurrence": row["recent_recurrence"],
        }
        for row in commitments
    ]
    manifest = campaign_runtime.build_campaign_manifest(
        day,
        runtime_items,
        segment_verification_sha256s=[row["verification_sha256"] for row in receipts],
        all_ready=True,
    )
    campaign_manifest_sha = _digest(manifest)
    variant_selection_manifest = {
        "schema": variant_bank.SELECTION_MANIFEST_SCHEMA,
        "campaign_commitment_id": campaign_commitment_id,
        "campaign_commitment_sha256": campaign_manifest_sha,
        "selection_count": len(selected_surfaces),
        "selections": selected_surfaces,
    }
    variant_selection_path, variant_selection_sha = _content_json(
        details / variant_bank.DEFAULT_BANK_REL / "selection-manifests",
        variant_selection_manifest,
    )
    if any(row.get("surface_kind") == "verified_variant" for row in selected_surfaces):
        consumption = consumption_committer(
            details,
            release_path,
            variant_selection_path,
        )
        if consumption.get("status") not in {
            "committed",
            "already_committed",
            "rebound",
            "already_rebound",
        }:
            raise CampaignControlError("variant_consumption_not_committed")
        if (
            consumption.get("selection_manifest_sha256") != variant_selection_sha
            or consumption.get("campaign_commitment_sha256") != campaign_manifest_sha
        ):
            raise CampaignControlError("variant_consumption_binding_drift")
    else:
        consumption = {
            "status": "not_required",
            "selection_manifest_sha256": variant_selection_sha,
            "campaign_commitment_sha256": campaign_manifest_sha,
            "variant_consumption_count": 0,
            "formal_write_count": 0,
        }
    formal_original_consumptions: list[dict[str, Any]] = []
    for selected in selected_surfaces:
        if selected.get("surface_kind") != "formal_original":
            continue
        committed = formal_original.commit_consumption(
            details, str(selected.get("selection_ref") or "")
        )
        if (
            committed.get("status") not in {"committed", "already_committed"}
            or committed.get("campaign_commitment_id") != campaign_commitment_id
            or committed.get("true_item_id") != selected.get("true_item_id")
        ):
            raise CampaignControlError("formal_original_consumption_binding_drift")
        formal_original_consumptions.append(committed)
    state = {
        **base_state,
        "status": "ready",
        "selection_manifest_sha256": selection_sha,
        "segment_count": len(receipts),
        "segment_verification_sha256s": [row["verification_sha256"] for row in receipts],
        "run_id": manifest["run_id"],
        "campaign_manifest_sha256": campaign_manifest_sha,
        "campaign_commitment_id": campaign_commitment_id,
        "variant_selection_manifest_ref": str(variant_selection_path),
        "variant_selection_manifest_sha256": variant_selection_sha,
        "variant_consumption": consumption,
        "formal_original_consumptions": formal_original_consumptions,
        "runtime_root": str(runtime),
        "all_segments_ready": True,
        "partial_run_created": False,
    }
    prep_dir, final = _write_preparation(
        control,
        day,
        state,
        selection_manifest=selection_manifest,
        segment_receipts=receipts,
        campaign_manifest=manifest,
    )
    manifest_path = prep_dir / "campaign-manifest.json"
    created = campaign_runtime.create_run(runtime, manifest_path)
    return {
        "schema": CONTROL_SCHEMA,
        **final,
        "preparation_ref": str(prep_dir),
        "campaign_manifest_ref": str(manifest_path),
        "runtime_create": created,
    }


def _current(control_root: Path, review_date: str) -> tuple[dict[str, Any], Path]:
    pointer = _regular_json(control_root / "current" / f"{review_date}.json", "campaign_control_pointer")
    if pointer.get("schema") != POINTER_SCHEMA or pointer.get("review_date") != review_date:
        raise CampaignControlError("campaign_control_pointer_invalid")
    ref = Path(str(pointer.get("state_ref") or ""))
    if ref.is_absolute() or ".." in ref.parts:
        raise CampaignControlError("campaign_control_state_ref_unsafe")
    state_path = (control_root / ref).resolve(strict=True)
    if state_path.parent.parent.parent.parent != control_root.resolve():
        # The exact depth is preparations/date/id/state.json.
        try:
            state_path.relative_to(control_root.resolve())
        except ValueError as exc:
            raise CampaignControlError("campaign_control_state_ref_escape") from exc
    state = _regular_json(state_path, "campaign_control_state")
    if _file_sha256(state_path) != pointer.get("state_sha256"):
        raise CampaignControlError("campaign_control_state_hash_drift")
    return state, state_path.parent


def status_campaign(
    repo: str | Path,
    *,
    review_date: str,
    control_root: str | Path | None = None,
    runtime_root: str | Path | None = None,
) -> dict[str, Any]:
    root = _repo_root(repo)
    day = _safe_date(review_date)
    control, runtime = _roots(root, control_root, runtime_root)
    state, prep_dir = _current(control, day)
    current_release = load_style_release(state["release_dir"])
    stale = current_release["content_sha256"] != state.get("release_content_sha256")
    runtime_status = None
    if state.get("run_id"):
        runtime_status = campaign_runtime.status_run(runtime, state["run_id"])
    open_episode = None
    if runtime_status and runtime_status.get("status") in {
        "active", "paused", "episode_pending_capture"
    }:
        pointer_path = control / "current-display" / f"{day}.json"
        if pointer_path.is_file():
            pointer = _regular_json(pointer_path, "current_display_pointer")
            if (
                pointer.get("run_id") == state.get("run_id")
                and pointer.get("item_id") == runtime_status.get("current_item_id")
            ):
                receipt_path = Path(str(pointer.get("display_receipt_path") or ""))
                receipt = _regular_json(receipt_path, "current_display_receipt")
                if _file_sha256(receipt_path) != pointer.get("display_receipt_sha256"):
                    raise CampaignControlError("current_display_receipt_hash_drift")
                saving = campaign_runtime.saving_contract_for_run(
                    runtime, str(state["run_id"])
                )
                if saving["episode_capture_every_item"]:
                    open_episode = {
                        "episode_id": f"{state['run_id']}:{pointer['item_id']}",
                        "study_date": receipt.get("displayed_date"),
                        "campaign_date": day,
                        "run_id": state["run_id"],
                        "item_id": pointer["item_id"],
                        "status": (
                            "capture_pending"
                            if runtime_status.get("status") == "episode_pending_capture"
                            else (
                                "displayed_paused"
                                if runtime_status.get("status") == "paused"
                                else "displayed_unanswered"
                            )
                        ),
                        "capture_verified": False,
                    }
    return {
        "schema": CONTROL_SCHEMA,
        "status": "stale_style_release" if stale else state["status"],
        "preparation_id": state["preparation_id"],
        "preparation_ref": str(prep_dir),
        "review_date": day,
        "run_id": state.get("run_id"),
        "due_count": state["due_count"],
        "prepared_count": state["prepared_count"],
        "content_blocked_count": state["content_blocked_count"],
        "all_segments_ready": state.get("all_segments_ready", False),
        "style_release_stale": stale,
        "prepared_release_content_sha256": state.get("release_content_sha256"),
        "current_release_content_sha256": current_release["content_sha256"],
        "runtime": runtime_status,
        **(
            {"open_episode": open_episode}
            if runtime_status and runtime_status.get("saving_contract_sha256")
            else {}
        ),
        **({"review_scope": state["review_scope"],
            "concept_coverage_complete": bool(runtime_status and runtime_status.get("status") == "completed"
                                              and not state["review_scope"].get("deferred_concept_ids")),
            "unanswered_node_count": state["review_scope"]["suppressed_unanswered_node_count"]}
           if state.get("review_scope") else {}),
        "learner_evidence_write_count": 0,
        "formal_write_count": 0,
    }


def pending_for_date(
    repo: str | Path,
    *,
    study_date: str,
    control_root: str | Path | None = None,
    runtime_root: str | Path | None = None,
) -> dict[str, Any]:
    """List displayed v2 items that have not sealed their episode Capture."""

    root = _repo_root(repo)
    day = _safe_date(study_date)
    control, runtime = _roots(root, control_root, runtime_root)
    directory = control / "current-display"
    episodes: list[dict[str, Any]] = []
    if directory.is_dir() and not directory.is_symlink():
        for pointer_path in sorted(directory.glob("????-??-??.json")):
            pointer = _regular_json(pointer_path, "current_display_pointer")
            run_id = pointer.get("run_id")
            item_id = pointer.get("item_id")
            if not isinstance(run_id, str) or not isinstance(item_id, str):
                continue
            try:
                saving = campaign_runtime.saving_contract_for_run(runtime, run_id)
                status = campaign_runtime.status_run(runtime, run_id)
            except campaign_runtime.CampaignRuntimeError:
                continue
            if (
                not saving["episode_capture_every_item"]
                or status.get("status")
                not in {"active", "paused", "episode_pending_capture"}
                or status.get("current_item_id") != item_id
            ):
                continue
            receipt_path = Path(str(pointer.get("display_receipt_path") or ""))
            receipt = _regular_json(receipt_path, "current_display_receipt")
            if (
                _file_sha256(receipt_path) != pointer.get("display_receipt_sha256")
                or receipt.get("displayed_date") != day
            ):
                continue
            episodes.append(
                {
                    "episode_id": f"{run_id}:{item_id}",
                    "study_date": day,
                    "campaign_date": receipt.get("campaign_date"),
                    "run_id": run_id,
                    "item_id": item_id,
                    "status": (
                        "capture_pending"
                        if status.get("status") == "episode_pending_capture"
                        else (
                            "displayed_paused"
                            if status.get("status") == "paused"
                            else "displayed_unanswered"
                        )
                    ),
                    "capture_verified": False,
                }
            )
    return {
        "schema": "morning-campaign-open-episodes-v2",
        "study_date": day,
        "open_episode_count": len(episodes),
        "episodes": episodes,
        "formal_write_count": 0,
        "fsrs_write_count": 0,
        "personal_summary_write_count": 0,
    }


def start_campaign(
    repo: str | Path,
    *,
    review_date: str,
    idempotency_key: str | None = None,
    control_root: str | Path | None = None,
    runtime_root: str | Path | None = None,
    private_root: str | Path | None = None,
    displayed_at: str | None = None,
) -> dict[str, Any]:
    root = _repo_root(repo)
    gate = legacy_session_gate(root)
    if gate["status"] != "clear":
        return {"schema": CONTROL_SCHEMA, **gate}
    day = _safe_date(review_date)
    control, runtime = _roots(root, control_root, runtime_root)
    current = status_campaign(root, review_date=day, control_root=control, runtime_root=runtime)
    if current["status"] != "ready" or current["all_segments_ready"] is not True:
        raise CampaignControlError(f"campaign_not_startable:{current['status']}")
    d_pointer = root / ".private/study-bridge/d-current" / (day + ".json")
    if d_pointer.is_file():
        import study_bridge_408 as bridge
        from d_review_scope_408 import pending_pre_campaign
        bridge.load_imported_d_run(root, day)
        d_reference = _regular_json(d_pointer, "D_current_pointer")
        d_receipt = _regular_json(Path(d_reference["receipt_path"]), "D_current_receipt")
        pending = pending_pre_campaign(root, d_receipt)
        if pending:
            raise CampaignControlError("D_pre_campaign_supplements_pending:" + ",".join(pending))
    preflight = audit_campaign(
        root,
        review_date=day,
        control_root=control,
        runtime_root=runtime,
    )
    if preflight.get("status") != "PASS":
        raise CampaignControlError("campaign_prestart_audit_failed")
    run_id = str(current["run_id"])
    receipt = campaign_runtime.start_run(
        runtime,
        run_id,
        idempotency_key=idempotency_key or f"start:{run_id}",
    )
    try:
        import morning_campaign_display_408 as campaign_display

        display = campaign_display.publish_current_display(
            root,
            review_date=day,
            run_id=run_id,
            control_root=control,
            runtime_root=runtime,
            private_root=private_root,
            displayed_at=displayed_at,
        )
    except Exception as exc:
        raise CampaignControlError(
            f"campaign_started_display_pending:{exc}"
        ) from exc
    return {
        "schema": CONTROL_SCHEMA,
        "status": "started" if receipt["status"] == "committed" else receipt["status"],
        "run_id": run_id,
        "runtime_receipt": receipt,
        **(
            {
                "saving_contract_sha256": (receipt.get("payload") or {})[
                    "saving_contract_sha256"
                ]
            }
            if (receipt.get("payload") or {}).get("saving_contract_sha256")
            else {}
        ),
        "display": display,
        "learner_evidence_write_count": display.get(
            "learner_evidence_write_count", 0
        ),
        "formal_write_count": 0,
    }


def audit_campaign(
    repo: str | Path,
    *,
    review_date: str,
    control_root: str | Path | None = None,
    runtime_root: str | Path | None = None,
) -> dict[str, Any]:
    root = _repo_root(repo)
    day = _safe_date(review_date)
    control, runtime = _roots(root, control_root, runtime_root)
    errors: list[str] = []
    try:
        state, prep_dir = _current(control, day)
        status = status_campaign(root, review_date=day, control_root=control, runtime_root=runtime)
        if status["style_release_stale"]:
            errors.append("style_release_drift")
        if state["status"] == "ready":
            selection_path = prep_dir / "selection-manifest.json"
            selection = _regular_json(selection_path, "selection_manifest")
            if _digest(selection) != state.get("selection_manifest_sha256"):
                errors.append("selection_manifest_hash_drift")
            manifest_path = prep_dir / "campaign-manifest.json"
            manifest = campaign_runtime.normalize_manifest(_regular_json(manifest_path, "campaign_manifest"))
            if _digest(manifest) != state.get("campaign_manifest_sha256"):
                errors.append("campaign_manifest_hash_drift")
            variant_selection_path = Path(str(state.get("variant_selection_manifest_ref") or ""))
            variant_selection = _regular_json(
                variant_selection_path, "variant_selection_manifest"
            )
            if (
                _file_sha256(variant_selection_path)
                != state.get("variant_selection_manifest_sha256")
                or variant_selection.get("schema")
                != variant_bank.SELECTION_MANIFEST_SCHEMA
                or variant_selection.get("campaign_commitment_id")
                != state.get("campaign_commitment_id")
                or variant_selection.get("campaign_commitment_sha256")
                != state.get("campaign_manifest_sha256")
            ):
                errors.append("variant_selection_manifest_binding_drift")
            variant_count = sum(
                isinstance(row, dict)
                and row.get("surface_kind") == "verified_variant"
                for row in variant_selection.get("selections") or []
            )
            consumption = state.get("variant_consumption") or {}
            if variant_count:
                if (
                    consumption.get("status")
                    not in {
                        "committed",
                        "already_committed",
                        "rebound",
                        "already_rebound",
                    }
                    or consumption.get("selection_manifest_sha256")
                    != state.get("variant_selection_manifest_sha256")
                    or consumption.get("campaign_commitment_sha256")
                    != state.get("campaign_manifest_sha256")
                    or consumption.get("variant_consumption_count") != variant_count
                ):
                    errors.append("variant_consumption_binding_drift")
            elif consumption.get("status") != "not_required":
                errors.append("unexpected_variant_consumption")
            formal_selection_rows = [
                row
                for row in variant_selection.get("selections") or []
                if isinstance(row, dict)
                and row.get("surface_kind") == "formal_original"
            ]
            formal_receipts = state.get("formal_original_consumptions") or []
            if len(formal_receipts) != len(formal_selection_rows):
                errors.append("formal_original_consumption_count_drift")
            else:
                expected_ids = {str(row.get("true_item_id")) for row in formal_selection_rows}
                observed_ids: set[str] = set()
                details_root = Path(str(state.get("details_root") or "")).expanduser().resolve()
                for row in formal_receipts:
                    if not isinstance(row, dict):
                        errors.append("formal_original_consumption_receipt_invalid")
                        continue
                    try:
                        receipt_path = _resolve_details_ref(
                            details_root,
                            row.get("consumption_ref"),
                            "formal_original_consumption",
                        )
                        receipt = _regular_json(
                            receipt_path, "formal_original_consumption"
                        )
                    except CampaignControlError as exc:
                        errors.append(str(exc))
                        continue
                    if (
                        _file_sha256(receipt_path) != row.get("consumption_sha256")
                        or receipt.get("schema") != formal_original.CONSUMPTION_SCHEMA
                        or receipt.get("campaign_commitment_id")
                        != state.get("campaign_commitment_id")
                    ):
                        errors.append("formal_original_consumption_binding_drift")
                    observed_ids.add(str(receipt.get("true_item_id")))
                if observed_ids != expected_ids:
                    errors.append("formal_original_consumption_identity_drift")
            verification = []
            for ordinal in range(state["segment_count"]):
                receipt = _regular_json(
                    prep_dir / "segment-verifications" / f"{ordinal:04d}.json",
                    "segment_verification",
                )
                core = {key: value for key, value in receipt.items() if key not in {"status", "verification_sha256"}}
                if receipt.get("status") != "PASS" or receipt.get("verification_sha256") != _digest(core):
                    errors.append(f"segment_verification_drift:{ordinal}")
                verification.append(receipt.get("verification_sha256"))
            if verification != state.get("segment_verification_sha256s"):
                errors.append("segment_verification_set_drift")
            runtime_audit = campaign_runtime.audit_run(
                runtime, state["run_id"], repo_root=root
            )
            if runtime_audit.get("status") != "PASS":
                errors.extend(f"runtime:{value}" for value in runtime_audit.get("errors") or [])
        else:
            runtime_audit = None
            if state.get("run_id") is not None:
                errors.append("non_ready_preparation_has_run")
    except (CampaignControlError, campaign_runtime.CampaignRuntimeError) as exc:
        errors.append(str(exc))
        state = {}
        runtime_audit = None
    return {
        "schema": CONTROL_SCHEMA,
        "status": "PASS" if not errors else "FAIL",
        "review_date": day,
        "preparation_id": state.get("preparation_id"),
        "run_id": state.get("run_id"),
        "errors": errors,
        "runtime_audit": runtime_audit,
        "learner_evidence_write_count": 0,
        "formal_write_count": 0,
    }


prepare = prepare_campaign
status = status_campaign
start = start_campaign
audit = audit_campaign


def _print(value: dict[str, Any]) -> None:
    print(json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True))


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo", default=str(Path.cwd()))
    parser.add_argument("--control-root")
    parser.add_argument("--runtime-root")
    sub = parser.add_subparsers(dest="command", required=True)
    prepare_parser = sub.add_parser("prepare")
    prepare_parser.add_argument("--date", required=True)
    prepare_parser.add_argument("--details-root", default=str(DEFAULT_DETAILS_ROOT))
    prepare_parser.add_argument("--release", default=str(DEFAULT_RELEASE))
    prepare_parser.add_argument("--config")
    prepare_parser.add_argument("--private-root")
    for name in ("status", "start", "audit", "pending-for-date"):
        child = sub.add_parser(name)
        child.add_argument("--date", required=True)
        if name == "start":
            child.add_argument("--idempotency-key")
            child.add_argument("--private-root")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        common = {
            "repo": args.repo,
            "review_date": args.date,
            "control_root": args.control_root,
            "runtime_root": args.runtime_root,
        }
        if args.command == "prepare":
            result = prepare_campaign(
                **common,
                details_root=args.details_root,
                release_dir=args.release,
                config_path=args.config,
                private_root=args.private_root,
            )
        elif args.command == "status":
            result = status_campaign(**common)
        elif args.command == "pending-for-date":
            result = pending_for_date(
                args.repo,
                study_date=args.date,
                control_root=args.control_root,
                runtime_root=args.runtime_root,
            )
        elif args.command == "start":
            result = start_campaign(
                **common,
                idempotency_key=args.idempotency_key,
                private_root=args.private_root,
            )
        else:
            result = audit_campaign(**common)
    except (CampaignControlError, scheduler.CampaignError, variant_bank.VariantBankError, formal_original.FormalOriginalSurfaceError, campaign_runtime.CampaignRuntimeError, question_exposure.QuestionExposureError) as exc:
        _print(
            {
                "schema": CONTROL_SCHEMA,
                "status": "blocked",
                "reason": str(exc),
                "learner_evidence_write_count": 0,
                "formal_write_count": 0,
            }
        )
        return 2
    _print(result)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
