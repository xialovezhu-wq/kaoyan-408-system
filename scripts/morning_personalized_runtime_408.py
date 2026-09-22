#!/usr/bin/env python3
"""Production adapter for the bounded morning personalized 408 router.

The adapter performs a constant number of manifest-bound projection reads.  It
never opens a review ledger or formal repository table.  Routing writes only a
content-addressed answer-safe receipt.  Variant cooldown changes require the
separate ``record-display`` command after an actual question surface was
rendered; selection or queueing alone cannot advance cooldown.
"""

from __future__ import annotations

import argparse
import fcntl
import hashlib
import hmac
import json
import os
import re
import secrets
import stat
import sys
import tempfile
from contextlib import contextmanager
from datetime import date
from pathlib import Path
from typing import Any, Iterable, Iterator

from morning_personalized_router_408 import (
    COOLDOWN_DAYS,
    CROSS_VARIANT_CATALOG_KIND,
    CROSS_VARIANT_CATALOG_SCHEMA,
    MAX_CANDIDATES,
    MAX_INPUT_BYTES,
    MorningRouterError,
    _current_live_cross_edges,
    _validate_catalog,
    _validate_question_index,
    route_morning_practice,
)
from linked_practice_source_408 import (
    LinkedPracticeSourceError,
    PROTECTED_VARIANT_TEXT_RE,
    validate_source_for_practice,
)


ROOT_MANIFEST_SCHEMA = "personalization_index_v1"
ROOT_MANIFEST_KIND = "personalization_manifest"
ROUTE_RECEIPT_SCHEMA = "morning_personalized_route_receipt_v1"
QUESTION_SURFACE_VIEW_SCHEMA = "verified_variant_question_surface_view_v1"
QUESTION_SURFACE_COMMITMENT_SCHEMA = (
    "verified_variant_question_surface_commitment_v1"
)
QUESTION_SURFACE_RENDER_RECEIPT_SCHEMA = (
    "verified_variant_question_surface_render_receipt_v1"
)
RENDERER_ATTESTATION_SCHEMA = "trusted_local_renderer_attestation_v1"
DISPLAY_EVENT_SCHEMA = "variant_display_event_v1"
DISPLAY_SNAPSHOT_SCHEMA = "morning_variant_cooldown_store_v1"
DISPLAY_POINTER_SCHEMA = "morning_variant_cooldown_pointer_v1"
DISPLAY_IDEMPOTENCY_SCHEMA = "variant_display_idempotency_receipt_v1"
LEARNER_COOLDOWN_SCHEMA = "morning_learner_cooldown_v1"
VARIANT_COOLDOWN_SCHEMA = "morning_variant_cooldown_v1"
RENDER_CALLBACK_STATUS = "managed_output_stream_write_and_flush_completed"
MANAGED_RENDERER_ACTOR_ID = "managed-question-stream-renderer"
RENDERER_TRUST_BOUNDARY = (
    "managed_output_stream_write_not_platform_or_human_view_proof"
)
TIMEZONE = "Asia/Shanghai"
DEFAULT_STATE_RELATIVE = Path("wiki/study_vaults/408-full/state/personalization")
DEFAULT_RUNTIME_RELATIVE = Path(
    "wiki/study_vaults/408-full/state/personalization-runtime/morning-router"
)
QUESTION_INDEX_FILENAME = "question_index.json"
VARIANT_CATALOG_FILENAME = "variant-catalog.json"
CROSS_BINDING_KEY = "cross_variant_catalog"
CROSS_PROJECTION_BINDING_KEY = "cross_subject_shadow"
CROSS_PROJECTION_FILENAME = "cross-subject-shadow.json"
SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
SAFE_SOURCE_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{2,160}$")
SAFE_IDEMPOTENCY_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{2,160}$")
KEY_FILENAME = ".variant-display.key"
MAX_MANIFEST_BYTES = 1024 * 1024
MAX_QUESTION_SURFACE_BYTES = 1024 * 1024
QUESTION_SURFACE_SUFFIXES = {
    ".md": "text/markdown; charset=utf-8",
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".webp": "image/webp",
}


class MorningRuntimeError(RuntimeError):
    """A stable fail-closed production integration error."""


def _retired_write_surface() -> None:
    raise MorningRuntimeError(
        "morning_personalized_runtime_retired_read_only_compatibility"
    )


def _canonical_bytes(value: Any) -> bytes:
    return (
        json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        + "\n"
    ).encode("utf-8")


def _pretty_bytes(value: Any) -> bytes:
    return (
        json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2) + "\n"
    ).encode("utf-8")


def _sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _digest(value: Any) -> str:
    return _sha256_bytes(_canonical_bytes(value))


def _valid_date(value: object, label: str) -> str:
    raw = str(value or "")
    try:
        return date.fromisoformat(raw).isoformat()
    except ValueError as exc:
        raise MorningRuntimeError(f"{label} must be YYYY-MM-DD") from exc


def _inside(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
        return True
    except ValueError:
        return False


def _read_object(path: Path, label: str, *, max_bytes: int = MAX_INPUT_BYTES) -> dict[str, Any]:
    try:
        info = path.lstat()
        if stat.S_ISLNK(info.st_mode) or not stat.S_ISREG(info.st_mode):
            raise MorningRuntimeError(f"{label} must be a regular non-symlink file")
        if info.st_size > max_bytes:
            raise MorningRuntimeError(f"{label} exceeds the bounded input size")
        value = json.loads(path.read_text(encoding="utf-8"))
    except MorningRuntimeError:
        raise
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise MorningRuntimeError(f"{label} is unreadable") from exc
    if not isinstance(value, dict):
        raise MorningRuntimeError(f"{label} must be a JSON object")
    return value


def _atomic_write(path: Path, payload: bytes, *, mode: int = 0o600) -> str:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    descriptor, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        os.fchmod(descriptor, mode)
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)
    return _sha256_bytes(payload)


def _content_write(directory: Path, payload: dict[str, Any]) -> tuple[Path, str]:
    body = _pretty_bytes(payload)
    digest = _sha256_bytes(body)
    path = directory / f"{digest}.json"
    if path.exists():
        if path.is_symlink() or path.read_bytes() != body:
            raise MorningRuntimeError("content-addressed artifact collision")
        return path, digest
    _atomic_write(path, body)
    return path, digest


@contextmanager
def _exclusive_lock(path: Path) -> Iterator[None]:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    with path.open("a+", encoding="utf-8") as handle:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def _state_paths(repo: str | Path, state_dir: str | Path | None) -> tuple[Path, Path]:
    root = Path(repo).expanduser().resolve()
    state = Path(state_dir).expanduser() if state_dir is not None else root / DEFAULT_STATE_RELATIVE
    if not state.is_absolute():
        state = root / state
    return root, state.resolve()


def _runtime_path(repo: Path, runtime_dir: str | Path | None) -> Path:
    path = Path(runtime_dir).expanduser() if runtime_dir is not None else repo / DEFAULT_RUNTIME_RELATIVE
    if not path.is_absolute():
        path = repo / path
    return path.resolve()


def _manifest_bound_object(
    state: Path,
    manifest: dict[str, Any],
    filename: str,
    label: str,
) -> tuple[dict[str, Any], str]:
    if Path(filename).name != filename:
        raise MorningRuntimeError(f"{label} binding must be one state-local file")
    artifact_hashes = manifest.get("artifact_hashes")
    expected = str((artifact_hashes or {}).get(filename) or "")
    path = state / filename
    if not SHA256_RE.fullmatch(expected):
        raise MorningRuntimeError(f"{label} has no valid root-manifest hash binding")
    if not path.is_file() or path.is_symlink() or _sha256_file(path) != expected:
        raise MorningRuntimeError(f"{label} drifted from the root manifest")
    return _read_object(path, label), expected


def load_verified_runtime_inputs(
    *,
    repo: str | Path,
    as_of: str,
    state_dir: str | Path | None = None,
) -> dict[str, Any]:
    """Read a constant set of root-manifest-bound runtime projections."""

    as_of = _valid_date(as_of, "as_of")
    root, state = _state_paths(repo, state_dir)
    manifest_path = state / "manifest.json"
    manifest = _read_object(
        manifest_path, "personalization root manifest", max_bytes=MAX_MANIFEST_BYTES
    )
    manifest_sha = _sha256_file(manifest_path)
    try:
        formal_write_count = int(manifest.get("formal_write_count", -1))
    except (TypeError, ValueError) as exc:
        raise MorningRuntimeError("personalization root manifest is stale or unsafe") from exc
    if (
        manifest.get("schema") != ROOT_MANIFEST_SCHEMA
        or manifest.get("kind") != ROOT_MANIFEST_KIND
        or manifest.get("answer_safe") is not True
        or formal_write_count != 0
        or manifest.get("as_of") != as_of
        or not SHA256_RE.fullmatch(str(manifest.get("build_id") or ""))
        or not isinstance(manifest.get("artifact_hashes"), dict)
        or not isinstance(manifest.get("independent_projections"), dict)
    ):
        raise MorningRuntimeError("personalization root manifest is stale or unsafe")

    question_index, question_sha = _manifest_bound_object(
        state, manifest, QUESTION_INDEX_FILENAME, "question_index"
    )
    variant_catalog, variant_sha = _manifest_bound_object(
        state, manifest, VARIANT_CATALOG_FILENAME, "variant catalog"
    )
    variant_binding = manifest["independent_projections"].get("variant_source_catalog")
    if (
        not isinstance(variant_binding, dict)
        or variant_binding.get("included_in_this_build") is not True
        or variant_binding.get("file") != VARIANT_CATALOG_FILENAME
        or manifest.get("variant_catalog_sha256") != variant_sha
    ):
        raise MorningRuntimeError("ordinary variant catalog is not root-manifest bound")
    try:
        _validate_question_index(question_index)
        _validate_catalog(variant_catalog, cross=False)
    except (MorningRouterError, TypeError, ValueError) as exc:
        raise MorningRuntimeError(str(exc)) from exc

    cross_catalog: dict[str, Any] | None = None
    cross_projection: dict[str, Any] | None = None
    cross_sha: str | None = None
    cross_status: dict[str, Any]
    cross_binding = manifest["independent_projections"].get(CROSS_BINDING_KEY)
    if not isinstance(cross_binding, dict) or cross_binding.get("included_in_this_build") is not True:
        cross_status = {
            "status": "degraded",
            "reason_code": "cross_variant_catalog_not_bound",
            "routing_effect": "ordinary_verified_variants_only",
        }
    else:
        filename = str(cross_binding.get("file") or "")
        cross_catalog, cross_sha = _manifest_bound_object(
            state, manifest, filename, "cross variant catalog"
        )
        if manifest.get("cross_variant_catalog_sha256") != cross_sha:
            raise MorningRuntimeError("cross variant catalog top-level hash binding drifted")
        try:
            _validate_catalog(cross_catalog, cross=True)
        except (MorningRouterError, TypeError, ValueError) as exc:
            raise MorningRuntimeError(str(exc)) from exc
        cross_status = {
            "status": "ready",
            "reason_code": None,
            "routing_effect": "verified_live_cross_variants_allowed",
        }

    projection_binding = manifest["independent_projections"].get(
        CROSS_PROJECTION_BINDING_KEY
    )
    if (
        isinstance(projection_binding, dict)
        and projection_binding.get("included_in_this_build") is True
        and projection_binding.get("file") == CROSS_PROJECTION_FILENAME
    ):
        expected_cross_projection_sha = str(
            manifest.get("cross_subject_shadow_sha256") or ""
        )
        cross_projection_path = state / CROSS_PROJECTION_FILENAME
        if (
            not SHA256_RE.fullmatch(expected_cross_projection_sha)
            or not cross_projection_path.is_file()
            or cross_projection_path.is_symlink()
            or _sha256_file(cross_projection_path) != expected_cross_projection_sha
        ):
            raise MorningRuntimeError("cross projection drifted from the root manifest")
        cross_projection = _read_object(
            cross_projection_path, "cross projection"
        )
        try:
            _current_live_cross_edges(cross_projection)
        except (MorningRouterError, TypeError, ValueError) as exc:
            raise MorningRuntimeError("cross projection is not a bounded safe build") from exc
    elif cross_catalog is not None:
        cross_status = {
            "status": "degraded",
            "reason_code": "current_cross_projection_not_bound",
            "routing_effect": "ordinary_verified_variants_only",
        }

    return {
        "repo": root,
        "state_dir": state,
        "manifest": manifest,
        "manifest_sha256": manifest_sha,
        "question_index": question_index,
        "question_index_sha256": question_sha,
        "variant_catalog": variant_catalog,
        "variant_catalog_sha256": variant_sha,
        "cross_variant_catalog": cross_catalog,
        "cross_projection": cross_projection,
        "cross_variant_catalog_sha256": cross_sha,
        "cross_variant_status": cross_status,
        "projection_file_read_count": 3
        + int(cross_catalog is not None)
        + int(cross_projection is not None),
    }


def derive_learner_cooldown(
    question_index: dict[str, Any], *, as_of: str
) -> dict[str, Any]:
    """Derive a bounded cooldown view without reading learner or review ledgers."""

    as_of = _valid_date(as_of, "as_of")
    questions = question_index.get("questions")
    if not isinstance(questions, dict) or len(questions) > MAX_CANDIDATES:
        raise MorningRuntimeError("question_index cannot produce bounded cooldown")
    rows: dict[str, dict[str, Any]] = {}
    for node_id, row in sorted(questions.items()):
        if not isinstance(row, dict):
            continue
        last_date = row.get("effective_last_attempt_date")
        if last_date not in (None, "", "unknown", "unrecorded", "未记录"):
            parsed = _valid_date(last_date, f"question {node_id} effective date")
            if parsed > as_of:
                raise MorningRuntimeError("question cooldown contains a future date")
            last_date = parsed
        else:
            last_date = None
        rows[str(node_id)] = {"last_attempt_date": last_date}
    return {
        "schema": LEARNER_COOLDOWN_SCHEMA,
        "as_of": as_of,
        "questions": rows,
    }


def _verified_variant_question_surface(
    inputs: dict[str, Any], selected: dict[str, Any]
) -> dict[str, Any]:
    """Reopen one selected source and bind its sole question-only asset.

    The root manifest and catalog prove which source manifest was selected.
    This additional bounded check detects later source or asset drift before a
    selection receipt can authorize display.  It deliberately returns no
    question text, answer, solution, or learner evidence.
    """

    if (
        selected.get("kind") != "verified_variant"
        or selected.get("practice_mode") != "variant"
        or not selected.get("anchor_formal_node_id")
    ):
        raise MorningRuntimeError("selected item is not a verified variant")
    try:
        validated = validate_source_for_practice(
            inputs["repo"],
            selected.get("source_ref"),
            selected.get("source_sha256"),
            practice_mode="variant",
            variant_of_formal_node_id=str(
                selected.get("anchor_formal_node_id") or ""
            ),
            personalization_state_dir=inputs["state_dir"],
        )
    except LinkedPracticeSourceError as exc:
        raise MorningRuntimeError(
            "selected variant source failed current deep validation"
        ) from exc
    source_path = Path(str(validated.get("path") or "")).resolve()
    payload = _read_object(
        source_path, "selected variant source", max_bytes=MAX_MANIFEST_BYTES
    )
    assets = payload.get("question_assets")
    if (
        payload.get("answer_isolated") is not True
        or payload.get("unique_answer_verified") is not True
        or payload.get("source_id") != selected.get("source_id")
        or not isinstance(assets, list)
        or len(assets) != 1
        or payload.get("question_asset_count") != 1
        or not isinstance(assets[0], dict)
        or set(assets[0]) != {"path", "sha256", "size_bytes"}
    ):
        raise MorningRuntimeError(
            "selected variant source is not a one-surface question-only manifest"
        )
    entry = assets[0]
    relative = Path(str(entry.get("path") or ""))
    if (
        not str(entry.get("path") or "")
        or relative.is_absolute()
        or ".." in relative.parts
        or relative.name != str(entry.get("path") or "")
    ):
        raise MorningRuntimeError("selected variant question locator is unsafe")
    asset_path = (source_path.parent / relative).resolve()
    suffix = asset_path.suffix.lower()
    declared_sha = str(entry.get("sha256") or "")
    declared_size = entry.get("size_bytes")
    try:
        asset_info = asset_path.lstat()
    except OSError as exc:
        raise MorningRuntimeError("selected variant question surface is missing") from exc
    if (
        not _inside(asset_path, source_path.parent.resolve())
        or stat.S_ISLNK(asset_info.st_mode)
        or not stat.S_ISREG(asset_info.st_mode)
        or suffix not in QUESTION_SURFACE_SUFFIXES
        or not SHA256_RE.fullmatch(declared_sha)
        or not isinstance(declared_size, int)
        or declared_size <= 0
        or declared_size > MAX_QUESTION_SURFACE_BYTES
        or asset_info.st_size != declared_size
        or _sha256_file(asset_path) != declared_sha
    ):
        raise MorningRuntimeError("selected variant question surface drifted")
    if suffix == ".md":
        try:
            surface_text = asset_path.read_text(encoding="utf-8")
        except UnicodeError as exc:
            raise MorningRuntimeError(
                "selected variant text surface is not UTF-8"
            ) from exc
        if PROTECTED_VARIANT_TEXT_RE.search(surface_text):
            raise MorningRuntimeError(
                "selected variant surface contains protected answer material"
            )
    try:
        asset_ref = asset_path.relative_to(inputs["repo"]).as_posix()
    except ValueError as exc:
        raise MorningRuntimeError(
            "selected variant question surface is outside the repository"
        ) from exc
    return {
        "schema": QUESTION_SURFACE_COMMITMENT_SCHEMA,
        "question_only": True,
        "answer_isolated": True,
        "source_id": selected["source_id"],
        "source_manifest_ref": validated["source_ref"],
        "source_manifest_sha256": selected["source_sha256"],
        "question_asset_ref": asset_ref,
        "question_asset_sha256": declared_sha,
        "question_asset_size_bytes": declared_size,
        "media_type": QUESTION_SURFACE_SUFFIXES[suffix],
    }


def _display_root(runtime_dir: Path) -> Path:
    return runtime_dir / "variant-display"


def _key_path(display_root: Path) -> Path:
    return display_root / KEY_FILENAME


def _load_key(display_root: Path, *, create: bool) -> bytes | None:
    path = _key_path(display_root)
    if not path.exists():
        if not create:
            return None
        display_root.mkdir(parents=True, exist_ok=True, mode=0o700)
        descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        try:
            os.write(descriptor, os.urandom(32))
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
    info = path.lstat()
    if stat.S_ISLNK(info.st_mode) or not stat.S_ISREG(info.st_mode) or info.st_mode & 0o077:
        raise MorningRuntimeError("variant display signing key is unsafe")
    key = path.read_bytes()
    if len(key) != 32:
        raise MorningRuntimeError("variant display signing key is invalid")
    return key


def _signed(value: dict[str, Any], key: bytes) -> dict[str, Any]:
    core = dict(value)
    core.pop("hmac_sha256", None)
    return {
        **core,
        "hmac_sha256": hmac.new(key, _canonical_bytes(core), hashlib.sha256).hexdigest(),
    }


def _verify_signed(value: dict[str, Any], key: bytes, label: str) -> dict[str, Any]:
    signature = str(value.get("hmac_sha256") or "")
    core = dict(value)
    core.pop("hmac_sha256", None)
    expected = hmac.new(key, _canonical_bytes(core), hashlib.sha256).hexdigest()
    if not SHA256_RE.fullmatch(signature) or not hmac.compare_digest(signature, expected):
        raise MorningRuntimeError(f"{label} signature drifted")
    return core


def _empty_display_snapshot() -> dict[str, Any]:
    return {
        "schema": DISPLAY_SNAPSHOT_SCHEMA,
        "event_count": 0,
        "chain_head_sha256": None,
        "variants": {},
        "answer_safe": True,
        "formal_write_count": 0,
    }


def _load_display_snapshot(runtime_dir: Path) -> tuple[dict[str, Any], dict[str, Any]]:
    display_root = _display_root(runtime_dir)
    pointer_path = display_root / "current.json"
    key = _load_key(display_root, create=False)
    if key is None and not pointer_path.exists():
        return _empty_display_snapshot(), {
            "status": "empty",
            "snapshot_sha256": None,
            "event_count": 0,
            "file_read_count": 0,
        }
    if key is not None and not pointer_path.exists():
        material_exists = any(
            (display_root / name).exists()
            for name in ("events", "snapshots", "idempotency")
        )
        if not material_exists:
            return _empty_display_snapshot(), {
                "status": "empty_initialized",
                "snapshot_sha256": None,
                "event_count": 0,
                "file_read_count": 1,
            }
        raise MorningRuntimeError("variant cooldown pointer/key state is incomplete")
    if key is None or not pointer_path.is_file() or pointer_path.is_symlink():
        raise MorningRuntimeError("variant cooldown pointer/key state is incomplete")
    pointer = _read_object(pointer_path, "variant cooldown pointer", max_bytes=MAX_MANIFEST_BYTES)
    pointer_core = _verify_signed(pointer, key, "variant cooldown pointer")
    snapshot_sha = str(pointer_core.get("snapshot_sha256") or "")
    snapshot_ref = str(pointer_core.get("snapshot_ref") or "")
    try:
        pointer_formal_writes = int(pointer_core.get("formal_write_count", -1))
    except (TypeError, ValueError) as exc:
        raise MorningRuntimeError("variant cooldown pointer is invalid") from exc
    if (
        pointer_core.get("schema") != DISPLAY_POINTER_SCHEMA
        or pointer_core.get("answer_safe") is not True
        or pointer_formal_writes != 0
        or not SHA256_RE.fullmatch(snapshot_sha)
        or snapshot_ref != f"snapshots/{snapshot_sha}.json"
    ):
        raise MorningRuntimeError("variant cooldown pointer is invalid")
    snapshot_path = (display_root / snapshot_ref).resolve()
    if not _inside(snapshot_path, display_root.resolve()):
        raise MorningRuntimeError("variant cooldown snapshot escapes its root")
    if not snapshot_path.is_file() or snapshot_path.is_symlink() or _sha256_file(snapshot_path) != snapshot_sha:
        raise MorningRuntimeError("variant cooldown snapshot drifted")
    snapshot = _read_object(snapshot_path, "variant cooldown snapshot")
    variants = snapshot.get("variants")
    try:
        snapshot_formal_writes = int(snapshot.get("formal_write_count", -1))
        snapshot_event_count = int(snapshot.get("event_count", -1))
        pointer_event_count = int(pointer_core.get("event_count", -2))
    except (TypeError, ValueError) as exc:
        raise MorningRuntimeError("variant cooldown snapshot is invalid") from exc
    if (
        snapshot.get("schema") != DISPLAY_SNAPSHOT_SCHEMA
        or snapshot.get("answer_safe") is not True
        or snapshot_formal_writes != 0
        or not isinstance(variants, dict)
        or len(variants) > MAX_CANDIDATES
        or snapshot_event_count != pointer_event_count
        or snapshot.get("chain_head_sha256") != pointer_core.get("chain_head_sha256")
    ):
        raise MorningRuntimeError("variant cooldown snapshot is invalid")
    for source_id, row in variants.items():
        if (
            not SAFE_SOURCE_ID_RE.fullmatch(str(source_id or ""))
            or not isinstance(row, dict)
            or not SHA256_RE.fullmatch(str(row.get("last_event_sha256") or ""))
            or not SHA256_RE.fullmatch(
                str(row.get("last_route_receipt_sha256") or "")
            )
            or not SHA256_RE.fullmatch(
                str(row.get("last_question_asset_sha256") or "")
            )
            or not SHA256_RE.fullmatch(
                str(row.get("last_render_receipt_sha256") or "")
            )
            or (
                row.get("last_renderer_attestation_sha256") is not None
                and not SHA256_RE.fullmatch(
                    str(row.get("last_renderer_attestation_sha256") or "")
                )
            )
            or not SHA256_RE.fullmatch(
                str(row.get("last_idempotency_key_sha256") or "")
            )
            or not SHA256_RE.fullmatch(
                str(row.get("last_payload_commitment") or "")
            )
        ):
            raise MorningRuntimeError("variant cooldown row is invalid")
        _valid_date(row.get("last_displayed_date"), "variant display date")
    head = snapshot.get("chain_head_sha256")
    # Signing key, pointer, and content-addressed snapshot are three bounded
    # reads.  The current chain head adds at most one more read.
    reads = 3
    if head is not None:
        if not SHA256_RE.fullmatch(str(head)):
            raise MorningRuntimeError("variant cooldown chain head is invalid")
        event_path = display_root / "events" / f"{head}.json"
        if not event_path.is_file() or event_path.is_symlink() or _sha256_file(event_path) != head:
            raise MorningRuntimeError("variant cooldown chain head drifted")
        event = _read_object(event_path, "variant display chain head", max_bytes=MAX_MANIFEST_BYTES)
        if (
            event.get("schema") != DISPLAY_EVENT_SCHEMA
            or event.get("answer_safe") is not True
            or event.get("formal_write_count") != 0
            or event.get("timezone") != TIMEZONE
            or event.get("selection_or_queue_event") is not False
            or event.get("anchor_original_attempt_date_mutated") is not False
            or event.get("render_callback_attested") is not True
            or event.get("output_stream_write_completed") is not True
            or event.get("renderer_trust_boundary") != RENDERER_TRUST_BOUNDARY
            or event.get("platform_ui_render_cryptographically_proven") is not False
            or event.get("human_view_proven") is not False
            or not SHA256_RE.fullmatch(
                str(event.get("renderer_attestation_sha256") or "")
            )
            or not SHA256_RE.fullmatch(
                str(event.get("question_asset_sha256") or "")
            )
            or not SHA256_RE.fullmatch(
                str(event.get("render_receipt_sha256") or "")
            )
            or not isinstance(event.get("question_asset_ref"), str)
            or not event.get("question_asset_ref")
        ):
            raise MorningRuntimeError("variant cooldown chain head is invalid")
        reads += 1
    return snapshot, {
        "status": "ready",
        "snapshot_sha256": snapshot_sha,
        "event_count": int(snapshot["event_count"]),
        "file_read_count": reads,
    }


def derive_variant_cooldown(
    *, runtime_dir: str | Path, as_of: str
) -> tuple[dict[str, Any], dict[str, Any]]:
    as_of = _valid_date(as_of, "as_of")
    snapshot, evidence = _load_display_snapshot(Path(runtime_dir).resolve())
    rows: dict[str, dict[str, Any]] = {}
    for source_id, row in sorted(snapshot.get("variants", {}).items()):
        if not isinstance(row, dict):
            raise MorningRuntimeError("variant cooldown row is invalid")
        last_date = _valid_date(row.get("last_displayed_date"), "variant display date")
        if last_date > as_of:
            raise MorningRuntimeError("variant cooldown contains a future display")
        rows[str(source_id)] = {"last_displayed_date": last_date}
    return {
        "schema": VARIANT_COOLDOWN_SCHEMA,
        "as_of": as_of,
        "variants": rows,
    }, evidence


def _route_receipt(
    runtime_dir: Path,
    *,
    result: dict[str, Any],
    inputs: dict[str, Any],
    target: dict[str, Any],
    cooldown_evidence: dict[str, Any],
    user_requested_original_id: str | None,
    followup_depth: int,
) -> dict[str, Any]:
    selected = result["selected_item"]
    question_surface = selected.get("question_surface")
    payload = {
        "schema": ROUTE_RECEIPT_SCHEMA,
        "receipt_role": "selection_receipt",
        "as_of": result["as_of"],
        "projection_build_id": inputs["manifest"]["build_id"],
        "projection_manifest_sha256": inputs["manifest_sha256"],
        "question_index_sha256": inputs["question_index_sha256"],
        "variant_catalog_sha256": inputs["variant_catalog_sha256"],
        "cross_variant_catalog_sha256": inputs["cross_variant_catalog_sha256"],
        "cross_variant_status": inputs["cross_variant_status"],
        "variant_cooldown_snapshot_sha256": cooldown_evidence["snapshot_sha256"],
        "target_sha256": _digest(target),
        "user_requested_original_id": user_requested_original_id,
        "followup_depth": followup_depth,
        "selected_item": selected,
        "selected_item_sha256": _digest(selected),
        "selected_source_manifest_sha256": selected.get("source_sha256"),
        "selected_question_surface_sha256": (
            question_surface.get("question_asset_sha256")
            if isinstance(question_surface, dict)
            else None
        ),
        "selection_is_not_display_evidence": True,
        "answer_safe": True,
        "formal_write_count": 0,
    }
    path, digest = _content_write(runtime_dir / "route-receipts", payload)
    return {
        "schema": ROUTE_RECEIPT_SCHEMA,
        "receipt_path": str(path),
        "receipt_sha256": digest,
    }


def production_route(
    *,
    repo: str | Path,
    target: dict[str, Any],
    as_of: str,
    state_dir: str | Path | None = None,
    runtime_dir: str | Path | None = None,
    demand_dir: str | Path | None = None,
    user_requested_original_id: str | None = None,
    followup_depth: int = 0,
) -> dict[str, Any]:
    """Load verified projections, route once, and persist an audit receipt."""

    _retired_write_surface()
    inputs = load_verified_runtime_inputs(repo=repo, state_dir=state_dir, as_of=as_of)
    runtime = _runtime_path(inputs["repo"], runtime_dir)
    learner = derive_learner_cooldown(inputs["question_index"], as_of=as_of)
    variant, cooldown_evidence = derive_variant_cooldown(runtime_dir=runtime, as_of=as_of)
    try:
        result = route_morning_practice(
            question_index=inputs["question_index"],
            variant_catalog=inputs["variant_catalog"],
            cross_variant_catalog=inputs["cross_variant_catalog"],
            cross_projection=inputs["cross_projection"],
            learner_cooldown=learner,
            variant_cooldown=variant,
            target=target,
            as_of=as_of,
            demand_dir=(Path(demand_dir).resolve() if demand_dir else runtime / "variant-demand"),
            user_requested_original_id=user_requested_original_id,
            followup_depth=followup_depth,
        )
    except MorningRouterError as exc:
        raise MorningRuntimeError(str(exc)) from exc
    if result["selected_item"].get("kind") == "verified_variant":
        result["selected_item"]["question_surface"] = (
            _verified_variant_question_surface(inputs, result["selected_item"])
        )
        result["selected_item"]["source_deep_validated"] = True
    receipt = _route_receipt(
        runtime,
        result=result,
        inputs=inputs,
        target=target,
        cooldown_evidence=cooldown_evidence,
        user_requested_original_id=user_requested_original_id,
        followup_depth=followup_depth,
    )
    result["production_integration"] = {
        "projection_build_id": inputs["manifest"]["build_id"],
        "projection_manifest_sha256": inputs["manifest_sha256"],
        "projection_file_read_count": inputs["projection_file_read_count"],
        "cooldown_file_read_count": cooldown_evidence["file_read_count"],
        "cross_variant_status": inputs["cross_variant_status"],
        "learner_cooldown_source": "question_index.effective_last_attempt_date",
        "variant_cooldown_source": "explicit_actual_display_events_only",
        "selection_advanced_variant_cooldown": False,
        "route_receipt": receipt,
        "selection_receipt": receipt,
    }
    return result


def _load_route_receipt(
    runtime_dir: Path, path_value: str | Path, expected_sha: str
) -> dict[str, Any]:
    if not SHA256_RE.fullmatch(str(expected_sha or "")):
        raise MorningRuntimeError("route receipt hash is invalid")
    path = Path(path_value).expanduser().resolve()
    root = (runtime_dir / "route-receipts").resolve()
    if not _inside(path, root) or path.name != f"{expected_sha}.json":
        raise MorningRuntimeError("route receipt is outside its managed directory")
    if not path.is_file() or path.is_symlink() or _sha256_file(path) != expected_sha:
        raise MorningRuntimeError("route receipt drifted")
    payload = _read_object(path, "route receipt", max_bytes=MAX_MANIFEST_BYTES)
    selected = payload.get("selected_item")
    question_surface = (
        selected.get("question_surface") if isinstance(selected, dict) else None
    )
    if (
        payload.get("schema") != ROUTE_RECEIPT_SCHEMA
        or payload.get("receipt_role") != "selection_receipt"
        or payload.get("selection_is_not_display_evidence") is not True
        or payload.get("answer_safe") is not True
        or payload.get("formal_write_count") != 0
        or not isinstance(selected, dict)
        or payload.get("selected_item_sha256") != _digest(selected)
        or payload.get("selected_source_manifest_sha256")
        != selected.get("source_sha256")
        or payload.get("selected_question_surface_sha256")
        != (
            question_surface.get("question_asset_sha256")
            if isinstance(question_surface, dict)
            else None
        )
    ):
        raise MorningRuntimeError("route receipt is invalid")
    return payload


def _catalog_entry(inputs: dict[str, Any], source_id: str) -> tuple[dict[str, Any], str]:
    for scope, catalog in (
        ("ordinary", inputs["variant_catalog"]),
        ("cross_subject", inputs["cross_variant_catalog"]),
    ):
        if catalog is None:
            continue
        for entry in catalog.get("entries", []):
            if isinstance(entry, dict) and entry.get("source_id") == source_id:
                return entry, scope
    raise MorningRuntimeError("displayed variant is absent from the verified catalog")


def _load_render_receipt(
    runtime_dir: Path,
    path_value: str | Path,
    expected_sha: str,
    *,
    selection_receipt_sha256: str,
    inputs: dict[str, Any],
    selected: dict[str, Any],
    current_surface: dict[str, Any],
) -> dict[str, Any]:
    if not SHA256_RE.fullmatch(str(expected_sha or "")):
        raise MorningRuntimeError("question surface render receipt hash is invalid")
    path = Path(path_value).expanduser().resolve()
    root = (runtime_dir / "question-surface-receipts").resolve()
    if not _inside(path, root) or path.name != f"{expected_sha}.json":
        raise MorningRuntimeError(
            "question surface render receipt is outside its managed directory"
        )
    if not path.is_file() or path.is_symlink() or _sha256_file(path) != expected_sha:
        raise MorningRuntimeError("question surface render receipt drifted")
    payload = _read_object(
        path, "question surface render receipt", max_bytes=MAX_MANIFEST_BYTES
    )
    if (
        payload.get("schema") != QUESTION_SURFACE_RENDER_RECEIPT_SCHEMA
        or payload.get("as_of") != inputs["manifest"].get("as_of")
        or payload.get("projection_manifest_sha256") != inputs["manifest_sha256"]
        or payload.get("variant_catalog_sha256")
        != inputs["variant_catalog_sha256"]
        or payload.get("selection_receipt_sha256")
        != selection_receipt_sha256
        or payload.get("source_id") != selected.get("source_id")
        or payload.get("source_ref") != selected.get("source_ref")
        or payload.get("source_sha256") != selected.get("source_sha256")
        or payload.get("question_surface") != current_surface
        or payload.get("question_surface_sha256")
        != current_surface.get("question_asset_sha256")
        or payload.get("surface_release")
        != "question_only_surface_returned_to_caller"
        or payload.get("surface_release_is_not_display_evidence") is not True
        or payload.get("contains_question_text") is not False
        or payload.get("answer_safe") is not True
        or payload.get("formal_write_count") != 0
    ):
        raise MorningRuntimeError("question surface render receipt is invalid")
    return payload


def view_selected_question_surface(
    *,
    repo: str | Path,
    route_receipt_path: str | Path,
    route_receipt_sha256: str,
    state_dir: str | Path | None = None,
    runtime_dir: str | Path | None = None,
) -> dict[str, Any]:
    """Return exactly one currently verified question-only surface for rendering.

    This formal- and learning-state-read-only boundary revalidates the manifest,
    catalog, source chain and question asset, writing only a metadata receipt.
    It does not record a display; the caller must do that only after the returned
    surface was actually rendered.
    """

    _retired_write_surface()
    root, _ = _state_paths(repo, state_dir)
    runtime = _runtime_path(root, runtime_dir)
    receipt = _load_route_receipt(
        runtime, route_receipt_path, route_receipt_sha256
    )
    as_of = _valid_date(receipt.get("as_of"), "selection receipt as_of")
    inputs = load_verified_runtime_inputs(
        repo=root, state_dir=state_dir, as_of=as_of
    )
    selected = receipt["selected_item"]
    if (
        receipt.get("projection_manifest_sha256") != inputs["manifest_sha256"]
        or selected.get("kind") != "verified_variant"
    ):
        raise MorningRuntimeError(
            "selection receipt no longer proves a current verified variant"
        )
    entry, scope = _catalog_entry(inputs, str(selected.get("source_id") or ""))
    if (
        selected.get("source_ref") != entry.get("source_ref")
        or selected.get("source_sha256") != entry.get("source_sha256")
    ):
        raise MorningRuntimeError("selected variant source drifted from its catalog")
    current_surface = _verified_variant_question_surface(inputs, selected)
    if selected.get("question_surface") != current_surface:
        raise MorningRuntimeError(
            "selection receipt question surface drifted from its source"
        )
    asset_path = (root / current_surface["question_asset_ref"]).resolve()
    try:
        surface_bytes = asset_path.read_bytes()
    except OSError as exc:
        raise MorningRuntimeError(
            "selected variant question surface became unreadable"
        ) from exc
    if (
        len(surface_bytes) != current_surface["question_asset_size_bytes"]
        or _sha256_bytes(surface_bytes)
        != current_surface["question_asset_sha256"]
    ):
        raise MorningRuntimeError(
            "selected variant question surface drifted during render preparation"
        )
    surface_text: str | None = None
    if current_surface["media_type"].startswith("text/"):
        try:
            surface_text = surface_bytes.decode("utf-8")
        except UnicodeError as exc:
            raise MorningRuntimeError(
                "selected variant text surface is not UTF-8"
            ) from exc
        if PROTECTED_VARIANT_TEXT_RE.search(surface_text):
            raise MorningRuntimeError(
                "selected variant surface contains protected answer material"
            )
    render_receipt_payload = {
        "schema": QUESTION_SURFACE_RENDER_RECEIPT_SCHEMA,
        "as_of": as_of,
        "projection_manifest_sha256": inputs["manifest_sha256"],
        "variant_catalog_sha256": inputs["variant_catalog_sha256"],
        "selection_receipt_sha256": route_receipt_sha256,
        "source_id": selected["source_id"],
        "source_ref": selected["source_ref"],
        "source_sha256": selected["source_sha256"],
        "question_surface": current_surface,
        "question_surface_sha256": current_surface["question_asset_sha256"],
        "surface_release": "question_only_surface_returned_to_caller",
        "surface_release_is_not_display_evidence": True,
        "contains_question_text": False,
        "answer_safe": True,
        "formal_write_count": 0,
    }
    render_receipt_path, render_receipt_sha = _content_write(
        runtime / "question-surface-receipts", render_receipt_payload
    )
    result: dict[str, Any] = {
        "schema": QUESTION_SURFACE_VIEW_SCHEMA,
        "status": "ready_to_render",
        "as_of": as_of,
        "catalog_scope": scope,
        "selection_receipt_sha256": route_receipt_sha256,
        "question_surface": current_surface,
        "render_receipt": {
            "schema": QUESTION_SURFACE_RENDER_RECEIPT_SCHEMA,
            "receipt_path": str(render_receipt_path),
            "receipt_sha256": render_receipt_sha,
        },
        "display_record_required_after_actual_render": True,
        "selection_advanced_variant_cooldown": False,
        "anchor_original_attempt_date_mutated": False,
        "answer_safe": True,
        "formal_write_count": 0,
    }
    if surface_text is not None:
        result["surface_text"] = surface_text
    else:
        result["surface_path"] = str(asset_path)
    return result


def render_selected_question_surface(
    *,
    repo: str | Path,
    route_receipt_path: str | Path,
    route_receipt_sha256: str,
    state_dir: str | Path | None = None,
    runtime_dir: str | Path | None = None,
) -> dict[str, Any]:
    """Write one verified UTF-8 question to stdout, then attest that write.

    The managed renderer has no public callback-status or caller-selected
    identity surface.  It writes and flushes the exact question text before it
    creates any renderer attestation.  A failed write or flush therefore leaves
    no display evidence.  Successful stdout delivery remains a local output
    boundary, not platform-level proof that a human saw the pixels.
    """

    _retired_write_surface()
    viewed = view_selected_question_surface(
        repo=repo,
        state_dir=state_dir,
        runtime_dir=runtime_dir,
        route_receipt_path=route_receipt_path,
        route_receipt_sha256=route_receipt_sha256,
    )
    surface_text = viewed.get("surface_text")
    if not isinstance(surface_text, str):
        raise MorningRuntimeError(
            "managed output-stream renderer supports UTF-8 text surfaces only"
        )
    try:
        written = sys.stdout.write(surface_text)
        sys.stdout.flush()
    except (OSError, UnicodeError) as exc:
        raise MorningRuntimeError("managed renderer output write failed") from exc
    if written != len(surface_text):
        raise MorningRuntimeError("managed renderer output write was incomplete")

    runtime = _runtime_path(Path(repo).expanduser().resolve(), runtime_dir)
    receipt = _load_route_receipt(
        runtime, route_receipt_path, route_receipt_sha256
    )
    displayed_date = _valid_date(receipt.get("as_of"), "selection receipt as_of")
    inputs = load_verified_runtime_inputs(
        repo=repo, state_dir=state_dir, as_of=displayed_date
    )
    selected = receipt["selected_item"]
    source_id = str(selected.get("source_id") or "")
    if (
        receipt.get("projection_manifest_sha256") != inputs["manifest_sha256"]
        or selected.get("kind") != "verified_variant"
        or not SAFE_SOURCE_ID_RE.fullmatch(source_id)
    ):
        raise MorningRuntimeError(
            "route receipt no longer proves this rendered variant selection"
        )
    entry, scope = _catalog_entry(inputs, source_id)
    if (
        selected.get("source_ref") != entry.get("source_ref")
        or selected.get("source_sha256") != entry.get("source_sha256")
    ):
        raise MorningRuntimeError("rendered variant source drifted from its catalog")
    current_surface = _verified_variant_question_surface(inputs, selected)
    if selected.get("question_surface") != current_surface:
        raise MorningRuntimeError(
            "selected variant question surface drifted after renderer output"
        )
    render_receipt = viewed["render_receipt"]
    _load_render_receipt(
        runtime,
        render_receipt["receipt_path"],
        render_receipt["receipt_sha256"],
        selection_receipt_sha256=route_receipt_sha256,
        inputs=inputs,
        selected=selected,
        current_surface=current_surface,
    )
    execution_id = "RND-" + secrets.token_hex(16).upper()
    render_event_id = "RPE-" + secrets.token_hex(16).upper()
    display_root = _display_root(runtime)
    key = _load_key(display_root, create=True)
    assert key is not None
    core = {
        "schema": RENDERER_ATTESTATION_SCHEMA,
        "as_of": displayed_date,
        "source_id": source_id,
        "catalog_scope": scope,
        "projection_manifest_sha256": inputs["manifest_sha256"],
        "selection_receipt_sha256": route_receipt_sha256,
        "render_receipt_sha256": render_receipt["receipt_sha256"],
        "question_asset_ref": current_surface["question_asset_ref"],
        "question_asset_sha256": current_surface["question_asset_sha256"],
        "rendered_utf8_byte_count": len(surface_text.encode("utf-8")),
        "output_stream_write_completed": True,
        "renderer_identity": {
            "actor_id": MANAGED_RENDERER_ACTOR_ID,
            "execution_id": execution_id,
        },
        "render_event_id": render_event_id,
        "callback_status": RENDER_CALLBACK_STATUS,
        "trust_boundary": RENDERER_TRUST_BOUNDARY,
        "platform_ui_render_cryptographically_proven": False,
        "human_view_proven": False,
        "contains_question_text": False,
        "answer_safe": True,
        "formal_write_count": 0,
    }
    signed = _signed(core, key)
    path, digest = _content_write(runtime / "renderer-attestations", signed)
    return {
        "schema": RENDERER_ATTESTATION_SCHEMA,
        "status": "managed_output_stream_rendered_and_attested",
        "attestation_path": str(path),
        "attestation_sha256": digest,
        "render_receipt": render_receipt,
        "render_event_id": render_event_id,
        "renderer_identity": core["renderer_identity"],
        "rendered_utf8_byte_count": core["rendered_utf8_byte_count"],
        "trust_boundary": RENDERER_TRUST_BOUNDARY,
        "platform_ui_render_cryptographically_proven": False,
        "human_view_proven": False,
        "cooldown_advanced": False,
        "anchor_original_attempt_date_mutated": False,
        "answer_safe": True,
        "formal_write_count": 0,
    }


def _idempotency_path(display_root: Path, digest: str) -> Path:
    return display_root / "idempotency" / f"{digest}.json"


def _load_idempotency(
    display_root: Path,
    key: bytes,
    key_digest: str,
    payload_commitment: str,
) -> dict[str, Any] | None:
    path = _idempotency_path(display_root, key_digest)
    if not path.exists():
        return None
    receipt = _read_object(path, "variant display idempotency receipt", max_bytes=MAX_MANIFEST_BYTES)
    core = _verify_signed(receipt, key, "variant display idempotency receipt")
    if (
        core.get("schema") != DISPLAY_IDEMPOTENCY_SCHEMA
        or core.get("idempotency_key_sha256") != key_digest
        or core.get("payload_commitment") != payload_commitment
        or not SHA256_RE.fullmatch(str(core.get("event_sha256") or ""))
        or not SHA256_RE.fullmatch(str(core.get("snapshot_sha256") or ""))
        or core.get("answer_safe") is not True
        or core.get("formal_write_count") != 0
    ):
        raise MorningRuntimeError("idempotency key was reused with different display facts")
    event_path = display_root / "events" / f"{core['event_sha256']}.json"
    if not event_path.is_file() or event_path.is_symlink() or _sha256_file(event_path) != core["event_sha256"]:
        raise MorningRuntimeError("idempotent display event drifted")
    snapshot_path = display_root / "snapshots" / f"{core['snapshot_sha256']}.json"
    if (
        not snapshot_path.is_file()
        or snapshot_path.is_symlink()
        or _sha256_file(snapshot_path) != core["snapshot_sha256"]
    ):
        raise MorningRuntimeError("idempotent display snapshot drifted")
    return {
        "status": "already_recorded",
        "event_sha256": core["event_sha256"],
        "snapshot_sha256": core["snapshot_sha256"],
        "idempotency_receipt": str(path),
    }


def _load_renderer_attestation(
    runtime: Path,
    path_value: str | Path,
    expected_sha: str,
    *,
    displayed_date: str,
    source_id: str,
    route_receipt_sha256: str,
    render_receipt_sha256: str,
    projection_manifest_sha256: str,
    current_surface: dict[str, Any],
) -> dict[str, Any]:
    if not SHA256_RE.fullmatch(str(expected_sha or "")):
        raise MorningRuntimeError("renderer attestation hash is invalid")
    path = Path(path_value).expanduser().resolve()
    root = (runtime / "renderer-attestations").resolve()
    if not _inside(path, root) or path.name != f"{expected_sha}.json":
        raise MorningRuntimeError("renderer attestation is outside its managed directory")
    if not path.is_file() or path.is_symlink() or _sha256_file(path) != expected_sha:
        raise MorningRuntimeError("renderer attestation drifted")
    payload = _read_object(path, "renderer attestation", max_bytes=MAX_MANIFEST_BYTES)
    key = _load_key(_display_root(runtime), create=False)
    if key is None:
        raise MorningRuntimeError("renderer attestation signing key is unavailable")
    core = _verify_signed(payload, key, "renderer attestation")
    identity = core.get("renderer_identity")
    if (
        core.get("schema") != RENDERER_ATTESTATION_SCHEMA
        or core.get("as_of") != displayed_date
        or core.get("source_id") != source_id
        or core.get("projection_manifest_sha256")
        != projection_manifest_sha256
        or core.get("selection_receipt_sha256") != route_receipt_sha256
        or core.get("render_receipt_sha256") != render_receipt_sha256
        or core.get("question_asset_ref")
        != current_surface.get("question_asset_ref")
        or core.get("question_asset_sha256")
        != current_surface.get("question_asset_sha256")
        or not isinstance(identity, dict)
        or identity.get("actor_id") != MANAGED_RENDERER_ACTOR_ID
        or re.fullmatch(r"RND-[0-9A-F]{32}", str(identity.get("execution_id") or ""))
        is None
        or re.fullmatch(r"RPE-[0-9A-F]{32}", str(core.get("render_event_id") or ""))
        is None
        or core.get("rendered_utf8_byte_count")
        != current_surface.get("question_asset_size_bytes")
        or core.get("output_stream_write_completed") is not True
        or core.get("callback_status") != RENDER_CALLBACK_STATUS
        or core.get("trust_boundary") != RENDERER_TRUST_BOUNDARY
        or core.get("platform_ui_render_cryptographically_proven") is not False
        or core.get("human_view_proven") is not False
        or core.get("contains_question_text") is not False
        or core.get("answer_safe") is not True
        or core.get("formal_write_count") != 0
    ):
        raise MorningRuntimeError("renderer attestation is invalid")
    return core


def record_variant_display(
    *,
    repo: str | Path,
    source_id: str,
    displayed_date: str,
    route_receipt_path: str | Path,
    route_receipt_sha256: str,
    render_receipt_path: str | Path,
    render_receipt_sha256: str,
    renderer_attestation_path: str | Path,
    renderer_attestation_sha256: str,
    idempotency_key: str,
    state_dir: str | Path | None = None,
    runtime_dir: str | Path | None = None,
) -> dict[str, Any]:
    """Record one trusted-local renderer-attested variant-surface display."""

    _retired_write_surface()
    displayed_date = _valid_date(displayed_date, "displayed_date")
    if not SAFE_SOURCE_ID_RE.fullmatch(str(source_id or "")):
        raise MorningRuntimeError("source_id is invalid")
    if not SAFE_IDEMPOTENCY_RE.fullmatch(str(idempotency_key or "")):
        raise MorningRuntimeError("idempotency_key is invalid")
    inputs = load_verified_runtime_inputs(
        repo=repo, state_dir=state_dir, as_of=displayed_date
    )
    runtime = _runtime_path(inputs["repo"], runtime_dir)
    receipt = _load_route_receipt(runtime, route_receipt_path, route_receipt_sha256)
    selected = receipt["selected_item"]
    if (
        receipt.get("as_of") != displayed_date
        or receipt.get("projection_manifest_sha256") != inputs["manifest_sha256"]
        or selected.get("kind") != "verified_variant"
        or selected.get("source_id") != source_id
    ):
        raise MorningRuntimeError("route receipt does not prove this variant selection")
    entry, scope = _catalog_entry(inputs, source_id)
    if (
        selected.get("source_ref") != entry.get("source_ref")
        or selected.get("source_sha256") != entry.get("source_sha256")
    ):
        raise MorningRuntimeError("selected variant source drifted from its catalog")
    current_surface = _verified_variant_question_surface(inputs, selected)
    if selected.get("question_surface") != current_surface:
        raise MorningRuntimeError(
            "selected variant question surface drifted before display commit"
        )
    _load_render_receipt(
        runtime,
        render_receipt_path,
        render_receipt_sha256,
        selection_receipt_sha256=route_receipt_sha256,
        inputs=inputs,
        selected=selected,
        current_surface=current_surface,
    )
    renderer_attestation = _load_renderer_attestation(
        runtime,
        renderer_attestation_path,
        renderer_attestation_sha256,
        displayed_date=displayed_date,
        source_id=source_id,
        route_receipt_sha256=route_receipt_sha256,
        render_receipt_sha256=render_receipt_sha256,
        projection_manifest_sha256=inputs["manifest_sha256"],
        current_surface=current_surface,
    )

    display_root = _display_root(runtime)
    with _exclusive_lock(display_root / ".lock"):
        snapshot, snapshot_evidence = _load_display_snapshot(runtime)
        key = _load_key(display_root, create=True)
        assert key is not None
        prior = (snapshot.get("variants") or {}).get(source_id) or {}
        prior_date = prior.get("last_displayed_date")
        idempotency_digest = _sha256_bytes(str(idempotency_key).encode("utf-8"))
        commitment_payload = {
            "source_id": source_id,
            "displayed_date": displayed_date,
            "route_receipt_sha256": route_receipt_sha256,
            "render_receipt_sha256": render_receipt_sha256,
            "renderer_attestation_sha256": renderer_attestation_sha256,
            "projection_manifest_sha256": inputs["manifest_sha256"],
            "question_asset_sha256": current_surface[
                "question_asset_sha256"
            ],
            "render_event_id": renderer_attestation["render_event_id"],
        }
        payload_commitment = _digest(commitment_payload)
        existing = _load_idempotency(
            display_root, key, idempotency_digest, payload_commitment
        )
        if existing is not None:
            return existing
        # A crash after publishing current.json but before publishing the
        # idempotency pointer leaves a fully applied event.  Recover that exact
        # transaction instead of appending a duplicate display.
        if (
            prior.get("last_idempotency_key_sha256") == idempotency_digest
            and prior.get("last_payload_commitment") == payload_commitment
            and SHA256_RE.fullmatch(str(prior.get("last_event_sha256") or ""))
            and SHA256_RE.fullmatch(
                str(snapshot_evidence.get("snapshot_sha256") or "")
            )
        ):
            recovered_core = {
                "schema": DISPLAY_IDEMPOTENCY_SCHEMA,
                "idempotency_key_sha256": idempotency_digest,
                "payload_commitment": payload_commitment,
                "event_sha256": prior["last_event_sha256"],
                "snapshot_sha256": snapshot_evidence["snapshot_sha256"],
                "answer_safe": True,
                "formal_write_count": 0,
            }
            recovered = _signed(recovered_core, key)
            recovered_path = _idempotency_path(display_root, idempotency_digest)
            _atomic_write(recovered_path, _pretty_bytes(recovered))
            return {
                "status": "recovered_idempotent_commit",
                "event_sha256": prior["last_event_sha256"],
                "snapshot_sha256": snapshot_evidence["snapshot_sha256"],
                "idempotency_receipt": str(recovered_path),
            }
        if prior_date:
            valid_prior_date = _valid_date(prior_date, "prior display date")
            display_age = (
                date.fromisoformat(displayed_date)
                - date.fromisoformat(valid_prior_date)
            ).days
            if display_age < 0:
                raise MorningRuntimeError("display date would regress variant cooldown")
            # Routing is intentionally read-only, so two callers may both obtain
            # valid receipts from the same pre-display snapshot.  The actual
            # display commit must therefore repeat the seven-day gate while
            # holding the display-store lock.  Exact retries have already
            # returned through the idempotency paths above; a different
            # idempotency key is a new display attempt and must fail closed.
            if display_age < COOLDOWN_DAYS:
                raise MorningRuntimeError(
                    "variant display cooldown is still active at commit"
                )

        event_core = {
            "schema": DISPLAY_EVENT_SCHEMA,
            "source_id": source_id,
            "source_ref": entry.get("source_ref"),
            "source_sha256": entry.get("source_sha256"),
            "catalog_scope": scope,
            "displayed_date": displayed_date,
            "timezone": TIMEZONE,
            "render_callback_attested": True,
            "render_event_id": renderer_attestation["render_event_id"],
            "renderer_identity": renderer_attestation["renderer_identity"],
            "renderer_attestation_sha256": renderer_attestation_sha256,
            "renderer_trust_boundary": RENDERER_TRUST_BOUNDARY,
            "platform_ui_render_cryptographically_proven": False,
            "human_view_proven": False,
            "output_stream_write_completed": True,
            "route_receipt_sha256": route_receipt_sha256,
            "render_receipt_sha256": render_receipt_sha256,
            "projection_manifest_sha256": inputs["manifest_sha256"],
            "question_asset_ref": current_surface["question_asset_ref"],
            "question_asset_sha256": current_surface["question_asset_sha256"],
            "previous_event_sha256": snapshot.get("chain_head_sha256"),
            "idempotency_key_sha256": idempotency_digest,
            "payload_commitment": payload_commitment,
            "selection_or_queue_event": False,
            "anchor_original_attempt_date_mutated": False,
            "answer_safe": True,
            "formal_write_count": 0,
        }
        event_id = "VDE-" + _digest(event_core)[:24].upper()
        event = {**event_core, "event_id": event_id}
        _, event_sha = _content_write(display_root / "events", event)

        variants = dict(snapshot.get("variants") or {})
        variants[source_id] = {
            "last_displayed_date": max(str(prior_date or ""), displayed_date),
            "last_event_sha256": event_sha,
            "last_route_receipt_sha256": route_receipt_sha256,
            "last_render_receipt_sha256": render_receipt_sha256,
            "last_renderer_attestation_sha256": renderer_attestation_sha256,
            "last_question_asset_sha256": current_surface[
                "question_asset_sha256"
            ],
            "last_idempotency_key_sha256": idempotency_digest,
            "last_payload_commitment": payload_commitment,
        }
        new_snapshot = {
            "schema": DISPLAY_SNAPSHOT_SCHEMA,
            "event_count": int(snapshot.get("event_count", 0)) + 1,
            "chain_head_sha256": event_sha,
            "variants": dict(sorted(variants.items())),
            "answer_safe": True,
            "formal_write_count": 0,
        }
        _, snapshot_sha = _content_write(display_root / "snapshots", new_snapshot)
        pointer_core = {
            "schema": DISPLAY_POINTER_SCHEMA,
            "snapshot_ref": f"snapshots/{snapshot_sha}.json",
            "snapshot_sha256": snapshot_sha,
            "chain_head_sha256": event_sha,
            "event_count": new_snapshot["event_count"],
            "updated_date": displayed_date,
            "answer_safe": True,
            "formal_write_count": 0,
        }
        pointer = _signed(pointer_core, key)
        _atomic_write(display_root / "current.json", _pretty_bytes(pointer))
        idempotency_core = {
            "schema": DISPLAY_IDEMPOTENCY_SCHEMA,
            "idempotency_key_sha256": idempotency_digest,
            "payload_commitment": payload_commitment,
            "event_sha256": event_sha,
            "snapshot_sha256": snapshot_sha,
            "answer_safe": True,
            "formal_write_count": 0,
        }
        idempotency_receipt = _signed(idempotency_core, key)
        idempotency_path = _idempotency_path(display_root, idempotency_digest)
        if idempotency_path.exists():
            raise MorningRuntimeError("idempotency receipt appeared concurrently")
        _atomic_write(idempotency_path, _pretty_bytes(idempotency_receipt))
        return {
            "status": "recorded",
            "event_id": event_id,
            "event_sha256": event_sha,
            "snapshot_sha256": snapshot_sha,
            "idempotency_receipt": str(idempotency_path),
            "displayed_date": displayed_date,
            "timezone": TIMEZONE,
            "question_asset_sha256": current_surface[
                "question_asset_sha256"
            ],
            "renderer_attestation_sha256": renderer_attestation_sha256,
            "renderer_trust_boundary": RENDERER_TRUST_BOUNDARY,
            "platform_ui_render_cryptographically_proven": False,
            "human_view_proven": False,
            "anchor_original_attempt_date_mutated": False,
            "formal_write_count": 0,
        }


def _json_arg(value: str, label: str) -> dict[str, Any]:
    try:
        parsed = json.loads(value)
    except json.JSONDecodeError as exc:
        raise MorningRuntimeError(f"{label} is invalid JSON") from exc
    if not isinstance(parsed, dict):
        raise MorningRuntimeError(f"{label} must decode to an object")
    return parsed


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo", default=".")
    parser.add_argument("--state-dir")
    parser.add_argument("--runtime-dir")
    sub = parser.add_subparsers(dest="command", required=True)

    route = sub.add_parser("route", help="select exactly one morning practice item")
    route.add_argument("--target-json", required=True)
    route.add_argument("--as-of", required=True)
    route.add_argument("--demand-dir")
    route.add_argument("--user-requested-original-id")
    route.add_argument("--followup-depth", type=int, default=0)

    view = sub.add_parser(
        "show-question",
        help="revalidate and return only the selected question-only surface",
    )
    view.add_argument("--route-receipt", required=True)
    view.add_argument("--route-receipt-sha256", required=True)

    render = sub.add_parser(
        "render-question",
        help="write the verified UTF-8 question to stdout, then attest that write",
    )
    render.add_argument("--route-receipt", required=True)
    render.add_argument("--route-receipt-sha256", required=True)

    display = sub.add_parser(
        "record-display", help="record a renderer-attested verified variant"
    )
    display.add_argument("--source-id", required=True)
    display.add_argument("--displayed-date", required=True)
    display.add_argument("--route-receipt", required=True)
    display.add_argument("--route-receipt-sha256", required=True)
    display.add_argument("--render-receipt", required=True)
    display.add_argument("--render-receipt-sha256", required=True)
    display.add_argument("--renderer-attestation", required=True)
    display.add_argument("--renderer-attestation-sha256", required=True)
    display.add_argument("--idempotency-key", required=True)
    return parser


def main(argv: Iterable[str] | None = None) -> int:
    args = _parser().parse_args(list(argv) if argv is not None else None)
    try:
        if args.command == "route":
            result = production_route(
                repo=args.repo,
                state_dir=args.state_dir,
                runtime_dir=args.runtime_dir,
                target=_json_arg(args.target_json, "target-json"),
                as_of=args.as_of,
                demand_dir=args.demand_dir,
                user_requested_original_id=args.user_requested_original_id,
                followup_depth=args.followup_depth,
            )
        elif args.command == "show-question":
            result = view_selected_question_surface(
                repo=args.repo,
                state_dir=args.state_dir,
                runtime_dir=args.runtime_dir,
                route_receipt_path=args.route_receipt,
                route_receipt_sha256=args.route_receipt_sha256,
            )
        elif args.command == "render-question":
            result = render_selected_question_surface(
                repo=args.repo,
                state_dir=args.state_dir,
                runtime_dir=args.runtime_dir,
                route_receipt_path=args.route_receipt,
                route_receipt_sha256=args.route_receipt_sha256,
            )
        else:
            result = record_variant_display(
                repo=args.repo,
                state_dir=args.state_dir,
                runtime_dir=args.runtime_dir,
                source_id=args.source_id,
                displayed_date=args.displayed_date,
                route_receipt_path=args.route_receipt,
                route_receipt_sha256=args.route_receipt_sha256,
                render_receipt_path=args.render_receipt,
                render_receipt_sha256=args.render_receipt_sha256,
                renderer_attestation_path=args.renderer_attestation,
                renderer_attestation_sha256=args.renderer_attestation_sha256,
                idempotency_key=args.idempotency_key,
            )
    except MorningRuntimeError as exc:
        print(json.dumps({"status": "error", "reason": str(exc)}, ensure_ascii=False))
        return 2
    if args.command == "render-question":
        sys.stdout.write("\n")
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    sys.exit(main())
