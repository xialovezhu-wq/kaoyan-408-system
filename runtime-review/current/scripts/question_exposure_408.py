#!/usr/bin/env python3
"""Verified learner-visible question exposure for 408 morning review.

The private ``turns`` directory is the display truth source.  A display is
accepted only when its content-addressed receipt, session, immutable prepared
pack, and public surface all agree.  Receipt mtimes are intentionally ignored;
legacy receipts without a display timestamp inherit the bound session
``review_date``.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import json
import re
import unicodedata
from pathlib import Path
from typing import Any, Iterable

import runtime_paths_408 as _paths


TURN_SCHEMA = "current-question-turn-receipt-v1"
DISPLAY_STATUS = "display_published"
CAMPAIGN_DISPLAY_STATUS = "campaign_display_published"
CAMPAIGN_DISPLAY_RECEIPT_SCHEMA = "morning-campaign-display-receipt-v1"
SESSION_SCHEMA = "morning_review_session_state_v1"
SESSION_EVENT_SCHEMA = "morning_review_session_event_v1"
PACK_SCHEMA = "morning_review_prepared_pack_manifest_v1"
SURFACE_SCHEMA = "morning_review_question_surface_v1"
EXPOSURE_SET_SCHEMA = "learner_visible_question_exposure_set_v1"
DEFAULT_PRIVATE_ROOT = _paths.package_root()
CANONICAL_REPO_ROOT = _paths.repo_root()
SESSION_ROOT_REL = Path("wiki/study_vaults/408-full/state/morning-review")
PACK_ROOT_REL = Path(
    "wiki/study_vaults/408-full/state/morning-review-prepared-packs"
)
SHA_RE = re.compile(r"^[0-9a-f]{64}$")
SAFE_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}$")
OPTIONS = ("A", "B", "C", "D")
MAX_OBJECT_BYTES = 1024 * 1024


class QuestionExposureError(RuntimeError):
    """A stable fail-closed exposure validation error."""


def _sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _canonical_bytes(value: Any) -> bytes:
    return (
        json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        + "\n"
    ).encode("utf-8")


def _digest(value: Any) -> str:
    return _sha256_bytes(_canonical_bytes(value))


def _date(value: object, label: str) -> dt.date:
    try:
        return dt.date.fromisoformat(str(value or ""))
    except ValueError as exc:
        raise QuestionExposureError(f"{label} is not YYYY-MM-DD") from exc


def _read_json(path: Path, label: str) -> tuple[dict[str, Any], bytes]:
    if path.is_symlink() or not path.is_file():
        raise QuestionExposureError(f"{label} is missing or unsafe")
    raw = path.read_bytes()
    if not 0 < len(raw) <= MAX_OBJECT_BYTES:
        raise QuestionExposureError(f"{label} exceeds its size boundary")
    try:
        value = json.loads(raw)
    except (UnicodeError, json.JSONDecodeError) as exc:
        raise QuestionExposureError(f"{label} is invalid JSON") from exc
    if not isinstance(value, dict):
        raise QuestionExposureError(f"{label} root is not an object")
    return value, raw


def _content_object(path: Path, label: str, expected_sha256: str | None = None) -> dict[str, Any]:
    if not SHA_RE.fullmatch(path.stem):
        raise QuestionExposureError(f"{label} filename is not a SHA-256")
    value, raw = _read_json(path, label)
    actual = _sha256_bytes(raw)
    if actual != path.stem or (expected_sha256 is not None and actual != expected_sha256):
        raise QuestionExposureError(f"{label} content-address binding drifted")
    return value


def _repo_ref(repo: Path, value: object, label: str) -> Path:
    relative = Path(str(value or ""))
    if relative.is_absolute() or not relative.parts or ".." in relative.parts:
        raise QuestionExposureError(f"{label} is not a safe repository reference")
    lexical = repo / relative
    if lexical.is_symlink():
        raise QuestionExposureError(f"{label} is a symlink")
    try:
        resolved = lexical.resolve(strict=True)
        resolved.relative_to(repo.resolve(strict=True))
    except (OSError, ValueError) as exc:
        raise QuestionExposureError(f"{label} is missing or escapes the repository") from exc
    return resolved


def _normalized_text(value: object) -> str:
    return re.sub(r"\s+", " ", unicodedata.normalize("NFKC", str(value or ""))).strip()


def surface_identity(surface: dict[str, Any]) -> dict[str, Any]:
    """Return the normalized question identity, excluding item/session identity."""

    if not isinstance(surface, dict) or surface.get("schema") != SURFACE_SCHEMA:
        raise QuestionExposureError("question surface schema is invalid")
    asset = surface.get("question_asset")
    if asset is not None:
        if not isinstance(asset, dict) or not SHA_RE.fullmatch(str(asset.get("sha256") or "")):
            raise QuestionExposureError("question surface asset binding is invalid")
        material = {
            "kind": "question_asset_sha256",
            "question_asset_sha256": str(asset["sha256"]),
        }
    else:
        options = surface.get("options")
        if not isinstance(options, dict) or set(options) != set(OPTIONS):
            raise QuestionExposureError("question surface options are invalid")
        stem = _normalized_text(surface.get("stem"))
        normalized_options = {
            label: _normalized_text(options[label]) for label in OPTIONS
        }
        if not stem or any(not value for value in normalized_options.values()):
            raise QuestionExposureError("question surface text identity is incomplete")
        material = {
            "kind": "stem_and_abcd",
            "stem": stem,
            "options": normalized_options,
        }
    return {**material, "identity_sha256": _digest(material)}


def _session_started_payload(session_dir: Path, session_id: str) -> dict[str, Any]:
    events_path = session_dir / "events.jsonl"
    if events_path.is_symlink() or not events_path.is_file():
        raise QuestionExposureError("morning session event chain is missing or unsafe")
    try:
        first_line = next(
            line for line in events_path.read_text(encoding="utf-8").splitlines() if line.strip()
        )
        event = json.loads(first_line)
    except (StopIteration, UnicodeError, json.JSONDecodeError) as exc:
        raise QuestionExposureError("morning session event chain is invalid") from exc
    if (
        not isinstance(event, dict)
        or event.get("schema") != SESSION_EVENT_SCHEMA
        or event.get("event_type") != "session_started"
        or event.get("session_id") != session_id
        or event.get("sequence") != 1
        or not isinstance(event.get("payload"), dict)
    ):
        raise QuestionExposureError("morning session start binding is invalid")
    return event["payload"]


def _session_state(repo: Path, session_id: str) -> tuple[dict[str, Any], Path]:
    if not SAFE_ID_RE.fullmatch(session_id):
        raise QuestionExposureError("display receipt session_id is invalid")
    session_dir = repo / SESSION_ROOT_REL / session_id
    if session_dir.is_symlink() or not session_dir.is_dir():
        raise QuestionExposureError("display receipt session is missing or unsafe")
    state, _ = _read_json(session_dir / "state.json", "morning session state")
    if state.get("schema") != SESSION_SCHEMA or state.get("session_id") != session_id:
        raise QuestionExposureError("display receipt session binding is invalid")
    return state, session_dir


def _manifest_and_surface(
    repo: Path,
    state: dict[str, Any],
    started: dict[str, Any],
    item_id: str,
    surface_sha256: str,
) -> tuple[dict[str, Any], dict[str, Any], dict[str, Any]]:
    manifest_sha = str(state.get("delivery_pack_sha256") or "")
    if not SHA_RE.fullmatch(manifest_sha):
        raise QuestionExposureError("morning session prepared-pack hash is invalid")
    manifest_path = _repo_ref(repo, state.get("delivery_pack_ref"), "prepared-pack manifest")
    expected_root = (repo / PACK_ROOT_REL / "objects" / "manifests").resolve(strict=True)
    if manifest_path.parent != expected_root or manifest_path.name != f"{manifest_sha}.json":
        raise QuestionExposureError("prepared-pack manifest is not at its immutable location")
    manifest = _content_object(manifest_path, "prepared-pack manifest", manifest_sha)
    if (
        manifest.get("schema") != PACK_SCHEMA
        or manifest.get("pack_id") != state.get("delivery_pack_id")
        or manifest.get("review_date") != state.get("review_date")
        or manifest.get("queue_sha256") != state.get("queue_sha256")
    ):
        raise QuestionExposureError("prepared-pack/session binding drifted")
    for field in (
        "delivery_pack_ref",
        "delivery_pack_sha256",
        "delivery_pack_id",
        "review_date",
        "queue_sha256",
    ):
        if started.get(field) != state.get(field):
            raise QuestionExposureError("session-start/prepared-pack binding drifted")
    rows = manifest.get("items")
    matches = [
        row for row in rows or []
        if isinstance(row, dict) and row.get("item_id") == item_id
    ]
    if len(matches) != 1:
        raise QuestionExposureError("prepared-pack item binding is not unique")
    row = matches[0]
    if row.get("surface_sha256") != surface_sha256:
        raise QuestionExposureError("prepared-pack surface binding drifted")
    started_rows = started.get("items")
    started_matches = [
        row for row in started_rows or []
        if isinstance(row, dict) and row.get("item_id") == item_id
    ]
    if len(started_matches) != 1 or started_matches[0].get("surface_sha256") != surface_sha256:
        raise QuestionExposureError("session-start surface binding drifted")
    surface_path = repo / PACK_ROOT_REL / "objects" / "surfaces" / f"{surface_sha256}.json"
    surface = _content_object(surface_path, "prepared question surface", surface_sha256)
    if (
        surface.get("schema") != SURFACE_SCHEMA
        or surface.get("surface_role") != "main"
        or surface.get("item_id") != item_id
    ):
        raise QuestionExposureError("prepared question surface identity drifted")
    return manifest, row, surface


def _verified_display(
    repo: Path,
    receipt: dict[str, Any],
    receipt_sha256: str,
    day: dt.date,
    *,
    window_days: int,
) -> dict[str, Any] | None:
    if receipt.get("schema") != TURN_SCHEMA or receipt.get("status") != DISPLAY_STATUS:
        raise QuestionExposureError("display receipt schema or status is invalid")
    session_id = str(receipt.get("session_id") or "")
    item_id = str(receipt.get("item_id") or "")
    surface_sha = str(receipt.get("surface_sha256") or "")
    if not SAFE_ID_RE.fullmatch(item_id) or not SHA_RE.fullmatch(surface_sha):
        raise QuestionExposureError("display receipt item or surface identity is invalid")
    state, session_dir = _session_state(repo, session_id)
    review_date = _date(state.get("review_date"), "morning session review_date")
    items = state.get("items")
    item = items.get(item_id) if isinstance(items, dict) else None
    if not isinstance(item, dict) or item.get("surface_sha256") != surface_sha:
        raise QuestionExposureError("display receipt/session surface binding drifted")
    started = _session_started_payload(session_dir, session_id)
    _, manifest_item, surface = _manifest_and_surface(
        repo, state, started, item_id, surface_sha
    )
    gap_days = (day - review_date).days
    if gap_days < 0 or gap_days >= window_days:
        return None
    asset = surface.get("question_asset")
    expected_asset_sha = asset.get("sha256") if isinstance(asset, dict) else None
    expected_asset_ref = asset.get("relative_path") if isinstance(asset, dict) else None
    if (
        receipt.get("question_asset_sha256") != expected_asset_sha
        or receipt.get("question_asset_ref") != expected_asset_ref
    ):
        raise QuestionExposureError("display receipt question-asset binding drifted")
    rendered = [str(surface["stem"]).strip(), ""]
    rendered.extend(
        f"{label}. {str(surface['options'][label]).strip()}" for label in OPTIONS
    )
    rendered.extend(["", str(surface["response_instruction"]).strip()])
    display_sha = _sha256_bytes(("\n".join(rendered).strip() + "\n").encode("utf-8"))
    if receipt.get("display_sha256") != display_sha:
        raise QuestionExposureError("display receipt rendered surface drifted")
    schedule = receipt.get("scheduling_binding")
    if schedule is not None:
        if (
            not isinstance(schedule, dict)
            or schedule.get("schema") != "morning-schedule-binding-v1"
            or schedule.get("session_id") != session_id
            or schedule.get("item_id") != item_id
            or schedule.get("surface_sha256") != surface_sha
            or schedule.get("prepared_pack_sha256")
            != state.get("delivery_pack_sha256")
            or schedule.get("queue_sha256") != state.get("queue_sha256")
        ):
            raise QuestionExposureError("display receipt scheduling binding drifted")
    identity = surface_identity(surface)
    return {
        "receipt_sha256": receipt_sha256,
        "review_date": review_date.isoformat(),
        "gap_days": gap_days,
        "session_id": session_id,
        "item_id": item_id,
        "formal_node_id": item.get("formal_node_id") or manifest_item.get("formal_node_id"),
        "review_unit_id": item.get("review_unit_id") or manifest_item.get("review_unit_id"),
        "source_id": item.get("source_id") or manifest_item.get("source_id"),
        "surface_sha256": surface_sha,
        "prepared_pack_sha256": state.get("delivery_pack_sha256"),
        "surface_identity_kind": identity["kind"],
        "surface_identity_sha256": identity["identity_sha256"],
        "question_asset_sha256": expected_asset_sha,
    }


def _verified_campaign_display(
    receipt: dict[str, Any],
    receipt_sha256: str,
    day: dt.date,
    *,
    window_days: int,
) -> dict[str, Any] | None:
    if (
        receipt.get("schema") != TURN_SCHEMA
        or receipt.get("receipt_schema") != CAMPAIGN_DISPLAY_RECEIPT_SCHEMA
        or receipt.get("status") != CAMPAIGN_DISPLAY_STATUS
        or receipt.get("answer_safe") is not True
        or receipt.get("answer_content_in_receipt") is not False
        or receipt.get("formal_write_count") != 0
    ):
        raise QuestionExposureError("campaign display receipt contract is invalid")
    review_date = _date(receipt.get("displayed_date"), "campaign display date")
    gap_days = (day - review_date).days
    if gap_days < 0 or gap_days >= window_days:
        return None
    run_id = str(receipt.get("run_id") or "")
    item_id = str(receipt.get("item_id") or "")
    true_item_id = str(receipt.get("true_item_id") or "")
    surface_sha = str(receipt.get("surface_sha256") or "")
    if (
        not SAFE_ID_RE.fullmatch(run_id)
        or not SAFE_ID_RE.fullmatch(item_id)
        or not SAFE_ID_RE.fullmatch(true_item_id)
        or not SHA_RE.fullmatch(surface_sha)
    ):
        raise QuestionExposureError("campaign display identity is invalid")
    if receipt.get("surface_kind") not in {
        "canonical_original",
        "formal_original",
        "verified_variant",
    }:
        raise QuestionExposureError("campaign display surface kind is invalid")
    learner = receipt.get("learner_surface")
    if not isinstance(learner, dict) or set(learner) != {"stem", "options"}:
        raise QuestionExposureError("campaign learner surface is invalid")
    stem = _normalized_text(learner.get("stem"))
    options = learner.get("options")
    if (
        not stem
        or not isinstance(options, dict)
        or set(options) != set(OPTIONS)
        or any(not _normalized_text(options[label]) for label in OPTIONS)
    ):
        raise QuestionExposureError("campaign learner surface is incomplete")
    display_material = {
        "stem": str(learner["stem"]).strip(),
        "options": {label: str(options[label]).strip() for label in OPTIONS},
    }
    if receipt.get("display_content_sha256") != _digest(display_material):
        raise QuestionExposureError("campaign display content hash drifted")
    identity = {
        "kind": "campaign_true_item_id",
        "true_item_id": true_item_id,
    }
    return {
        "receipt_sha256": receipt_sha256,
        "review_date": review_date.isoformat(),
        "gap_days": gap_days,
        "session_id": run_id,
        "run_id": run_id,
        "item_id": item_id,
        "true_item_id": true_item_id,
        "surface_kind": receipt.get("surface_kind"),
        "surface_sha256": surface_sha,
        "prepared_pack_sha256": receipt.get("campaign_manifest_sha256"),
        "surface_identity_kind": identity["kind"],
        "surface_identity_sha256": _digest(identity),
        "question_asset_sha256": receipt.get("question_asset_sha256"),
    }


def recent_exposure_set(
    repo: str | Path,
    day: dt.date,
    *,
    private_root: str | Path | None = None,
    window_days: int = 7,
) -> dict[str, Any]:
    """Return all verified displays in ``[day-window+1, day]`` and a fingerprint."""

    if window_days <= 0:
        raise QuestionExposureError("exposure window must be positive")
    root = Path(repo).expanduser().resolve(strict=True)
    private = (
        Path(private_root).expanduser().resolve()
        if private_root is not None
        else private_root_for_repo(root)
    )
    turns = private / "turns"
    if not turns.exists():
        rows: list[dict[str, Any]] = []
    else:
        if turns.is_symlink() or not turns.is_dir():
            raise QuestionExposureError("private display receipt directory is unsafe")
        rows = []
        for path in sorted(turns.glob("*.json"), key=lambda value: value.name):
            receipt = _content_object(path, "private turn receipt")
            if receipt.get("schema") != TURN_SCHEMA:
                raise QuestionExposureError("private turn receipt schema drifted")
            if receipt.get("status") == CAMPAIGN_DISPLAY_STATUS:
                verified = _verified_campaign_display(
                    receipt,
                    path.stem,
                    day,
                    window_days=window_days,
                )
                if verified is not None:
                    rows.append(verified)
                continue
            if receipt.get("status") != DISPLAY_STATUS:
                continue
            verified = _verified_display(
                root,
                receipt,
                path.stem,
                day,
                window_days=window_days,
            )
            if verified is not None:
                rows.append(verified)
    from d_concept_supplement_408 import exposure_rows
    rows.extend(exposure_rows(root, day, window_days))
    rows.sort(
        key=lambda row: (
            row["review_date"],
            row["session_id"],
            row["item_id"],
            row["receipt_sha256"],
        )
    )
    material = {
        "schema": EXPOSURE_SET_SCHEMA,
        "review_date": day.isoformat(),
        "window_days": window_days,
        "rows": rows,
    }
    return {
        **material,
        "exposure_count": len(rows),
        "fingerprint_sha256": _digest(material),
    }


def private_root_for_repo(repo: str | Path) -> Path:
    """Avoid coupling temporary/test repositories to the user's real receipts."""

    root = Path(repo).expanduser().resolve()
    if root == CANONICAL_REPO_ROOT.expanduser().resolve():
        return DEFAULT_PRIVATE_ROOT.expanduser().resolve()
    return (root / ".private/kaoyan-408-conversation-packages").resolve()


def matching_identity_rows(
    exposure_set: dict[str, Any], identity_sha256: str
) -> list[dict[str, Any]]:
    return [
        row
        for row in exposure_set.get("rows") or []
        if isinstance(row, dict)
        and row.get("surface_identity_sha256") == identity_sha256
    ]


def assert_no_recent_surface_reuse(
    repo: str | Path,
    day: dt.date,
    surfaces: Iterable[dict[str, Any]],
    *,
    private_root: str | Path | None = None,
    exclude_prepared_pack_sha256: str | None = None,
) -> dict[str, Any]:
    """Fail when a proposed main surface exactly repeats a recent display."""

    if (
        exclude_prepared_pack_sha256 is not None
        and not SHA_RE.fullmatch(exclude_prepared_pack_sha256)
    ):
        raise QuestionExposureError("excluded prepared-pack SHA-256 is invalid")
    exposure_set = recent_exposure_set(
        repo, day, private_root=private_root, window_days=7
    )
    for surface in surfaces:
        identity = surface_identity(surface)
        matches = matching_identity_rows(
            exposure_set, str(identity["identity_sha256"])
        )
        if exclude_prepared_pack_sha256 is not None:
            matches = [
                row
                for row in matches
                if row.get("prepared_pack_sha256")
                != exclude_prepared_pack_sha256
            ]
        if matches:
            latest = max(matches, key=lambda row: (row["review_date"], row["receipt_sha256"]))
            raise QuestionExposureError(
                f"{surface.get('item_id')} repeats learner-visible question surface "
                f"from {latest['review_date']}"
            )
    return exposure_set


__all__ = [
    "DEFAULT_PRIVATE_ROOT",
    "EXPOSURE_SET_SCHEMA",
    "QuestionExposureError",
    "assert_no_recent_surface_reuse",
    "matching_identity_rows",
    "private_root_for_repo",
    "recent_exposure_set",
    "surface_identity",
]
