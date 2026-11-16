"""Read-only candidate eligibility snapshot for project D morning input.

The module enumerates candidates without selecting a representative, reserving a
surface, consuming a variant, publishing a display, or writing learning state.
"""
from __future__ import annotations

import datetime as dt
import re
from pathlib import Path
from typing import Any

import exam_variant_bank_408 as variant_bank
import formal_original_surface_408 as formal_original
import question_exposure_408 as question_exposure
import start_morning_coverage_campaign_408 as control


SCHEMA = "morning_input_eligibility_snapshot_v1"
WINDOW_DAYS = 7


def _cooldown(
    rows: list[dict[str, Any]], true_item_id: str, day: dt.date
) -> tuple[int | None, dict[str, Any] | None]:
    matches = [
        row
        for row in rows
        if isinstance(row, dict) and row.get("true_item_id") == true_item_id
    ]
    if not matches:
        return None, None
    latest = min(
        matches,
        key=lambda row: (
            int(row.get("gap_days", WINDOW_DAYS)),
            str(row.get("review_date") or ""),
            str(row.get("receipt_sha256") or ""),
        ),
    )
    gap = int(latest["gap_days"])
    return gap, latest


def _candidate(
    *,
    true_item_id: str,
    surface_kind: str,
    main_knowledge: str,
    reasons: list[str],
    day: dt.date,
    exposure_rows: list[dict[str, Any]],
    variant_id: str | None = None,
    surface_sha256: str | None = None,
    acceptance_kind: str,
    canonical_id: str | None = None,
) -> dict[str, Any]:
    gap, latest = _cooldown(exposure_rows, true_item_id, day)
    normalized_reasons = list(dict.fromkeys(reasons))
    if not variant_bank.SAFE_ID_RE.fullmatch(true_item_id):
        normalized_reasons.append("candidate_true_item_id_invalid")
    if gap is not None and gap < WINDOW_DAYS:
        normalized_reasons.append("exact_item_cooldown")
    result = {
        "true_item_id": true_item_id,
        "surface_kind": surface_kind,
        "main_knowledge": main_knowledge,
        "eligible": not normalized_reasons,
        "rejection_reasons": normalized_reasons,
        "variant_id": variant_id,
        "surface_sha256": surface_sha256,
        "acceptance_kind": acceptance_kind,
        "canonical_id": canonical_id,
        "cooldown_gap_days": gap,
        "eligible_on": (
            (day + dt.timedelta(days=WINDOW_DAYS - gap)).isoformat()
            if gap is not None and gap < WINDOW_DAYS
            else None
        ),
        "latest_exposure_receipt_sha256": (
            latest.get("receipt_sha256") if latest is not None else None
        ),
    }
    return result


def _safe_unmapped(row: dict[str, Any]) -> dict[str, Any]:
    allowed = {
        "receipt_sha256",
        "review_date",
        "gap_days",
        "session_id",
        "run_id",
        "item_id",
        "surface_kind",
        "surface_sha256",
        "surface_identity_kind",
        "surface_identity_sha256",
        "question_asset_sha256",
        "mapping_reason",
    }
    return {key: row.get(key) for key in sorted(allowed) if key in row}


def canonical_targets(node, release, requested_points=None):
    """Keep the formal source identity separate from the requested review point.

    A registered formal node may support several existing concept requests.
    Additional targets use the release's actual primary-point or explicitly
    assessed practice-unit membership; its unrelated exact original is not
    silently relabeled as a question about the additional point.
    """
    original = str(node.get("main_knowledge") or "")
    points = list(dict.fromkeys([original, *(requested_points or [])]))
    result = []
    for point in points:
        scoped = {**node, "main_knowledge": point,
                  "exact_sources": node.get("exact_sources", []) if point == original else []}
        identities = list(dict.fromkeys([
            *scoped["exact_sources"],
            *control._candidate_ids(scoped, release, include_practice=True),
        ]))
        result.extend((point, identity) for identity in identities)
    return result


def build_eligibility(
    *,
    repo: Path,
    details: Path,
    target_date: str,
    formal_ids: list[str],
    release_dir: Path,
    review_points_by_formal_id: dict[str, list[str]] | None = None,
) -> dict[str, Any]:
    """Return all locally known candidate qualifications for exact formal IDs."""

    repo = Path(repo).expanduser().resolve(strict=True)
    details = Path(details).expanduser().resolve(strict=True)
    release_dir = Path(release_dir).expanduser().resolve(strict=True)
    try:
        day = dt.date.fromisoformat(target_date)
    except ValueError as exc:
        raise ValueError("target_date_invalid") from exc
    if (
        not isinstance(formal_ids, list)
        or len(formal_ids) != len(set(formal_ids))
        or any(
            not isinstance(formal_id, str)
            or not control.FORMAL_NODE_RE.fullmatch(formal_id)
            for formal_id in formal_ids
        )
    ):
        raise ValueError("formal_ids_invalid_or_duplicate")
    if review_points_by_formal_id is not None:
        if (not isinstance(review_points_by_formal_id, dict)
                or not set(review_points_by_formal_id) <= set(formal_ids)
                or any(not isinstance(points, list) or not points
                       or len(points) != len(set(points))
                       or any(not isinstance(point, str) or not variant_bank.KNOWLEDGE_RE.fullmatch(point)
                              for point in points)
                       for points in review_points_by_formal_id.values())):
            raise ValueError("review_points_by_formal_id_invalid")

    nodes = control._node_snapshot(repo)
    missing = [formal_id for formal_id in formal_ids if formal_id not in nodes]
    if missing:
        raise ValueError("formal_ids_not_in_current_master:" + ",".join(missing))

    release = variant_bank._release_snapshot(
        release_dir,
        require_final_production_release=False,
    )
    from exam_practice_units_408 import overlay_release
    release = overlay_release(release, details)
    variant_pool = variant_bank.pool_status(
        details,
        release_dir,
        persist_index=False,
        require_final_production_release=False,
    )
    formal_pool = formal_original.pool_status(details)
    raw_exposure = question_exposure.recent_exposure_set(
        repo,
        day,
        window_days=WINDOW_DAYS,
    )
    mapped_exposure = control.map_recent_exposures(
        raw_exposure,
        release,
        variant_entries=list(variant_pool.get("entries") or []),
        formal_original_entries=list(formal_pool.get("entries") or []),
    )
    exposure_rows = list(mapped_exposure.get("rows") or [])

    candidates_by_formal_id: dict[str, list[dict[str, Any]]] = {}
    for formal_id in formal_ids:
        node = nodes[formal_id]
        main_knowledge = str(node.get("main_knowledge") or "")
        candidates: list[dict[str, Any]] = []

        requested = (review_points_by_formal_id or {}).get(formal_id)
        for review_point, canonical_id in canonical_targets(node, release, requested):
            reasons: list[str] = []
            surface_sha256 = None
            exposure = release["exposure_by_id"].get(canonical_id)
            if exposure is None:
                reasons.append("canonical_not_in_release")
            else:
                if exposure.get("exposure_mode") != "direct_pool":
                    reasons.append("original_not_verified_direct_pool")
                if exposure.get("direct_replay_allowed") is not True:
                    reasons.append("direct_replay_forbidden")
                if exposure.get("production_usable") is not True:
                    reasons.append("original_not_production_usable")
                if exposure.get("direct_surface_status", "ready") != "ready":
                    reasons.append("original_surface_not_ready")
                if not reasons:
                    try:
                        commitments = variant_bank._verified_original_commitments(
                            details, release, exposure
                        )
                        surface_sha256 = str(commitments["surface_sha256"])
                    except variant_bank.VariantBankError as exc:
                        reasons.append("original_commitment_invalid:" + str(exc))
            candidates.append(
                _candidate(
                    true_item_id=canonical_id,
                    surface_kind="canonical_original",
                    main_knowledge=review_point,
                    reasons=reasons,
                    day=day,
                    exposure_rows=exposure_rows,
                    surface_sha256=surface_sha256,
                    acceptance_kind="canonical_original_ready",
                    canonical_id=canonical_id,
                )
            )
            if exposure:
                candidates[-1].update({key: exposure.get(key) for key in (
                    "practice_unit_id", "parent_true_item_id", "knowledge_points", "keywords",
                    "question_type", "dependency_kind", "contact_dependency_ids")})

        for row in formal_pool.get("entries") or []:
            if row.get("formal_node_id") != formal_id:
                continue
            reasons = []
            if row.get("pool_status") != "verified":
                reasons.append(
                    "formal_original_"
                    + str(row.get("status_reason") or row.get("pool_status") or "invalid")
                )
            if row.get("formal_node_sha256") != node["formal_node_sha256"]:
                reasons.append("formal_node_sha256_drift")
            candidates.append(
                _candidate(
                    true_item_id=str(row.get("true_item_id") or ""),
                    surface_kind="formal_original",
                    main_knowledge=main_knowledge,
                    reasons=reasons,
                    day=day,
                    exposure_rows=exposure_rows,
                    surface_sha256=(
                        str(row["surface_sha256"])
                        if variant_bank.SHA_RE.fullmatch(str(row.get("surface_sha256") or ""))
                        else None
                    ),
                    acceptance_kind="verified_original_ready",
                )
            )

        for row in variant_pool.get("entries") or []:
            if row.get("formal_node_id") != formal_id or not row.get("variant_id"):
                continue
            reasons = []
            if row.get("pool_status") != "verified":
                reasons.append(
                    "variant_"
                    + str(row.get("status_reason") or row.get("pool_status") or "invalid")
                )
            candidate_knowledge = str(row.get("main_knowledge") or "")
            if not variant_bank.KNOWLEDGE_RE.fullmatch(candidate_knowledge):
                reasons.append("variant_main_knowledge_invalid")
            candidates.append(
                _candidate(
                    true_item_id=str(row["variant_id"]),
                    surface_kind="verified_variant",
                    main_knowledge=candidate_knowledge,
                    reasons=reasons,
                    day=day,
                    exposure_rows=exposure_rows,
                    variant_id=str(row["variant_id"]),
                    surface_sha256=(
                        str(row["surface_sha256"])
                        if variant_bank.SHA_RE.fullmatch(str(row.get("surface_sha256") or ""))
                        else None
                    ),
                    acceptance_kind=str(
                        row.get("acceptance_kind")
                        or variant_bank.INDEPENDENT_ACCEPTANCE_KIND
                    ),
                    canonical_id=(
                        str((row.get("canonical_anchor_ids") or [None])[0])
                        if row.get("canonical_anchor_ids")
                        else None
                    ),
                )
            )

        candidates.sort(
            key=lambda row: (
                row["surface_kind"],
                row["true_item_id"],
                row.get("surface_sha256") or "",
            )
        )
        candidates_by_formal_id[formal_id] = candidates

    manifest_path = release_dir / "manifest.json"
    from morning_question_policy_408 import apply_user_exclusions
    return apply_user_exclusions({
        "schema": SCHEMA,
        "target_date": day.isoformat(),
        "window_days": WINDOW_DAYS,
        "formal_ids": formal_ids,
        "snapshot": {
            "exam_style_release_id": release["release_id"],
            "exam_style_release_content_sha256": release["content_sha256"],
            "exam_style_release_manifest_sha256": variant_bank._sha256_file(
                manifest_path
            ),
            "raw_exposure_fingerprint_sha256": raw_exposure["fingerprint_sha256"],
            "mapped_exposure_fingerprint_sha256": mapped_exposure[
                "fingerprint_sha256"
            ],
            "exposure_count": mapped_exposure["exposure_count"],
            "variant_pool_content_sha256": variant_pool["content_sha256"],
            "formal_original_pool_content_sha256": formal_pool["content_sha256"],
            "variant_index_persisted": False,
        },
        "unmapped_exposures": [
            _safe_unmapped(row) for row in mapped_exposure.get("unmapped") or []
        ],
        "candidates_by_formal_id": candidates_by_formal_id,
        "canonical_anchor_eligibility": {
            cid: {"production_usable": row.get("production_usable") is True,
                  "style_observation_allowed": row.get("style_observation_allowed") is True,
                  "direct_replay_allowed": row.get("direct_replay_allowed") is True,
                  "direct_surface_status": row.get("direct_surface_status"),
                  "source": "pinned_local_exam_style_release"}
            for cid, row in release["exposure_by_id"].items()
        },
        "answer_content_included": False,
        "selection_performed": False,
        "consumption_performed": False,
        "display_performed": False,
        "learner_evidence_write_count": 0,
        "formal_write_count": 0,
    }, repo)


__all__ = ["SCHEMA", "WINDOW_DAYS", "build_eligibility"]
