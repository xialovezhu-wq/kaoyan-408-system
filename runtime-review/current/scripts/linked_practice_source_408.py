#!/usr/bin/env python3
"""Resolve and verify durable answer-safe sources for linked 408 practice.

The resolver classifies a source by its resolved file and manifest schema, not
by the spelling of the locator.  This prevents a repository-relative, absolute,
or symlink alias from bypassing the same manifest and per-asset checks.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import re
import stat
from pathlib import Path
from typing import Any

from question_source_attestation_408 import (
    SCHEMA as QUESTION_ISOLATION_ATTESTATION_SCHEMA,
    QuestionSourceAttestationError,
    asset_set_sha256 as question_asset_set_sha256,
    validate_attestation_chain,
)

import runtime_paths_408 as _paths


DEFAULT_PERSONALIZATION_STATE_RELATIVE = Path(
    "wiki/study_vaults/408-full/state/personalization"
)
DEFAULT_LINKED_PRACTICE_RELATIVE = Path(
    "wiki/study_vaults/408-full/state/linked-practice"
)
DEFAULT_BRIDGE_SNAPSHOT = (
    _paths.codex_home() / 'kaoyan-408-obsidian-bridge/records.json'
)
QUESTION_SOURCE_SCHEMA = "question_source_manifest_v1"
VARIANT_SOURCE_SCHEMA = "variant_question_source_manifest_v1"
CROSS_VARIANT_SOURCE_SCHEMA = "cross_variant_question_source_manifest_v1"
VARIANT_RECEIPT_SCHEMA = "variant_answer_verification_receipt_v1"
VARIANT_REQUEST_SCHEMA = "variant_generation_request_v1"
VARIANT_EVIDENCE_SCHEMA = "variant_authoring_evidence_v1"
VARIANT_ATTESTATION_SCHEMA = "variant_independent_attestation_v1"
VARIANT_IDENTITY_SCHEMA = "variant_actor_execution_identity_v1"
SELECTION_RECEIPT_SCHEMA = "personalization_selection_receipt_v1"
PERSONALIZATION_SOURCE_PREFIX = "personalization-source:"
PERSONALIZATION_SELECTION_PREFIX = "personalization-selection:"
FORMAL_NODE_RE = re.compile(r"^(?:DS|CO|OS|CN)_(?:\d{4}|UNK)_\d{3}$")
SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
QUESTION_ASSET_RE = re.compile(
    r"^question-\d{2}\.(?:png|jpe?g|webp)$", re.IGNORECASE
)
SOLUTION_ASSET_RE = re.compile(
    r"^solution-\d{2}\.(?:png|jpe?g|webp)$", re.IGNORECASE
)
VARIANT_ASSET_RE = re.compile(
    r"^variant-question-\d{2}\.(?:png|jpe?g|webp|md)$", re.IGNORECASE
)
VARIANT_SOURCE_ID_RE = re.compile(r"^VAR-[A-Za-z0-9][A-Za-z0-9._:-]{2,120}$")
RECEIPT_ID_RE = re.compile(r"^VVR-[A-Za-z0-9][A-Za-z0-9._:-]{2,120}$")
ACTOR_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{2,80}$")
VARIANT_REQUEST_ID_RE = re.compile(r"^VRQ-[0-9A-F]{20}$")
TEMP_MARKERS = (
    "/tmp/",
    "/private/tmp/",
    "/var/folders/",
    "/private/var/folders/",
)
PROTECTED_VARIANT_TEXT_RE = re.compile(
    r"(?:标准答案|正确答案|答案\s*(?:[:：]|是|为)|解析\s*[:：]|正确选项|"
    r"本题\s*(?:应选|选)|[A-HＡ-Ｈ]\s*项\s*(?:正确|为真)|"
    r"solution\s*(?::|is\b)|answer\s*(?::|is\b)|answer\s+key)",
    re.IGNORECASE,
)
VARIANT_PREPARE_KEY_FILENAME = ".variant-pipeline-prepare.key"


class LinkedPracticeSourceError(RuntimeError):
    """A stable, non-sensitive linked-source validation failure."""


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def selection_commitment_key(
    trigger_event_id: object,
    mode: object,
    budget_version: object,
    max_bytes: object,
) -> str:
    material = {
        "trigger_event_id": str(trigger_event_id or ""),
        "mode": str(mode or ""),
        "budget_version": str(budget_version or ""),
        "max_bytes": int(max_bytes),
    }
    digest = hashlib.sha256(
        json.dumps(
            material,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()
    return "PSC-" + digest[:24].upper()


def _inside(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
        return True
    except ValueError:
        return False


def _state_root(repo: Path, state_dir: str | Path | None) -> Path:
    root = (
        Path(state_dir)
        if state_dir is not None
        else repo / DEFAULT_PERSONALIZATION_STATE_RELATIVE
    )
    if not root.is_absolute():
        root = repo / root
    return root.expanduser().resolve()


def resolve_source(
    repo: str | Path,
    locator: object,
    *,
    personalization_state_dir: str | Path | None = None,
) -> tuple[Path, str]:
    root = Path(repo).expanduser().resolve()
    raw = str(locator or "").strip()
    if not raw or "#" in raw:
        raise LinkedPracticeSourceError("source_ref must name one durable file")
    state_root = _state_root(root, personalization_state_dir)
    if raw.startswith(PERSONALIZATION_SOURCE_PREFIX):
        relative = raw.removeprefix(PERSONALIZATION_SOURCE_PREFIX)
        relative_path = Path(relative)
        if not relative or relative_path.is_absolute() or ".." in relative_path.parts:
            raise LinkedPracticeSourceError("personalization source_ref is invalid")
        path = (state_root / relative_path).resolve()
        if not _inside(path, state_root):
            raise LinkedPracticeSourceError("personalization source_ref escapes projection")
    elif raw.startswith("exam-corpus:"):
        relative = raw.removeprefix("exam-corpus:")
        relative_path = Path(relative)
        if not relative or relative_path.is_absolute() or ".." in relative_path.parts:
            raise LinkedPracticeSourceError("exam-corpus source_ref is invalid")
        path = (root.parent / "kaoyan-408-details" / "corpus" / relative_path).resolve()
    else:
        candidate = Path(raw).expanduser()
        if candidate.is_absolute() and any(marker in raw.lower() for marker in TEMP_MARKERS):
            raise LinkedPracticeSourceError("source_ref must not use a temporary file")
        path = candidate.resolve() if candidate.is_absolute() else (root / candidate).resolve()
    if not path.is_file():
        raise LinkedPracticeSourceError("source_ref is not a durable file")

    # Canonicalize every alias of a managed default question source to the same
    # portable locator.  A custom projection is persisted as its actual stable
    # path so later capture does not silently resolve it against the default.
    default_state = (root / DEFAULT_PERSONALIZATION_STATE_RELATIVE).resolve()
    managed_default = default_state / "question_sources"
    if _inside(path, managed_default):
        normalized = PERSONALIZATION_SOURCE_PREFIX + path.relative_to(default_state).as_posix()
    else:
        try:
            normalized = path.relative_to(root).as_posix()
        except ValueError:
            normalized = str(path)
    return path, normalized


def resolve_selection_receipt(
    repo: str | Path,
    locator: object,
    *,
    personalization_state_dir: str | Path | None = None,
) -> tuple[Path, str, Path]:
    root = Path(repo).expanduser().resolve()
    raw = str(locator or "").strip()
    if not raw or "#" in raw:
        raise LinkedPracticeSourceError("selection receipt must name one durable file")
    state_root = _state_root(root, personalization_state_dir)
    if raw.startswith(PERSONALIZATION_SELECTION_PREFIX):
        relative = raw.removeprefix(PERSONALIZATION_SELECTION_PREFIX)
        relative_path = Path(relative)
        if not relative or relative_path.is_absolute() or ".." in relative_path.parts:
            raise LinkedPracticeSourceError("selection receipt locator is invalid")
        path = (state_root / relative_path).resolve()
        if not _inside(path, state_root):
            raise LinkedPracticeSourceError("selection receipt escapes projection")
    else:
        candidate = Path(raw).expanduser()
        if candidate.is_absolute() and any(marker in raw.lower() for marker in TEMP_MARKERS):
            raise LinkedPracticeSourceError("selection receipt must not use a temporary file")
        path = candidate.resolve() if candidate.is_absolute() else (root / candidate).resolve()
    managed_receipts = (state_root / "selection_receipts").resolve()
    if not _inside(path, managed_receipts):
        raise LinkedPracticeSourceError("selection receipt is outside its managed directory")
    if not path.is_file() or path.is_symlink():
        raise LinkedPracticeSourceError("selection receipt is not a durable regular file")
    default_state = (root / DEFAULT_PERSONALIZATION_STATE_RELATIVE).resolve()
    if _inside(path, default_state / "selection_receipts"):
        normalized = (
            PERSONALIZATION_SELECTION_PREFIX
            + path.relative_to(default_state).as_posix()
        )
    else:
        try:
            normalized = path.relative_to(root).as_posix()
        except ValueError:
            normalized = str(path)
    return path, normalized, state_root.resolve()


def validate_selection_receipt(
    repo: str | Path,
    locator: object,
    receipt_sha256: object,
    *,
    expected: dict[str, Any],
    source_path: Path,
    personalization_state_dir: str | Path | None = None,
) -> dict[str, Any]:
    path, normalized, state_root = resolve_selection_receipt(
        repo,
        locator,
        personalization_state_dir=personalization_state_dir,
    )
    digest = str(receipt_sha256 or "")
    if not SHA256_RE.fullmatch(digest) or sha256_file(path) != digest:
        raise LinkedPracticeSourceError("selection receipt hash mismatch")
    payload = _load_json_object(path)
    if (
        payload.get("schema") != SELECTION_RECEIPT_SCHEMA
        or payload.get("mode") != "verified"
        or payload.get("schedule_allowed") is not True
        or int(payload.get("formal_write_count", -1)) != 0
        or payload.get("gate_decision")
        not in {
            "original_due_over_7d",
            "related_question_due_over_7d",
            "recent_original_verified_variant",
        }
    ):
        raise LinkedPracticeSourceError("selection receipt did not pass routing gates")
    manifest = state_root / "manifest.json"
    if not manifest.is_file():
        raise LinkedPracticeSourceError("selection receipt projection is missing")
    manifest_payload = _load_json_object(manifest)
    manifest_sha256 = sha256_file(manifest)
    current_projection_id = str(
        manifest_payload.get("runtime_projection_id")
        or manifest_payload.get("build_id")
        or ""
    )
    if (
        not SHA256_RE.fullmatch(str(payload.get("projection_id") or ""))
        or not SHA256_RE.fullmatch(
            str(payload.get("projection_manifest_sha256") or "")
        )
        or payload.get("projection_manifest_sha256") != manifest_sha256
        or payload.get("projection_id") != current_projection_id
        or payload.get("projection_build_id") != manifest_payload.get("build_id")
    ):
        raise LinkedPracticeSourceError("selection receipt projection identity mismatch")
    learner_path = state_root / "learner_state.json"
    learner_hash = (manifest_payload.get("artifact_hashes") or {}).get(
        "learner_state.json"
    )
    if (
        not learner_path.is_file()
        or not SHA256_RE.fullmatch(str(learner_hash or ""))
        or sha256_file(learner_path) != learner_hash
    ):
        raise LinkedPracticeSourceError("selection receipt learner state has drifted")
    learner_payload = _load_json_object(learner_path)
    selected_practice = payload.get("selected_practice")
    if not isinstance(selected_practice, dict):
        raise LinkedPracticeSourceError("selection receipt selection is invalid")
    selected_digest = hashlib.sha256(
        (
            json.dumps(
                selected_practice,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            )
            + "\n"
        ).encode("utf-8")
    ).hexdigest()
    if payload.get("selected_practice_sha256") != selected_digest:
        raise LinkedPracticeSourceError("selection receipt selection hash mismatch")
    try:
        commitment_key = selection_commitment_key(
            payload.get("trigger_event_id"),
            payload.get("mode"),
            payload.get("budget_version"),
            payload.get("max_bytes"),
        )
    except (TypeError, ValueError) as exc:
        raise LinkedPracticeSourceError("selection receipt budget is invalid") from exc
    commitments = learner_payload.get("selection_commitments")
    commitment = commitments.get(commitment_key) if isinstance(commitments, dict) else None
    if (
        not isinstance(commitment, dict)
        or commitment.get("trigger_event_id") != payload.get("trigger_event_id")
        or commitment.get("mode") != payload.get("mode")
        or commitment.get("budget_version") != payload.get("budget_version")
        or commitment.get("max_bytes") != payload.get("max_bytes")
        or commitment.get("selected_practice_sha256")
        != payload.get("selected_practice_sha256")
    ):
        raise LinkedPracticeSourceError("selection receipt is not the committed selection")
    for field in (
        "slice_id",
        "trigger_event_id",
        "candidate_id",
        "practice_mode",
        "formal_node_id",
        "anchor_formal_node_id",
        "source_id",
        "source_ref",
        "source_sha256",
        "mechanism_key",
        "knowledge_point",
        "review_unit_id",
    ):
        if payload.get(field) != expected.get(field):
            raise LinkedPracticeSourceError(
                f"selection receipt {field} does not match followup"
            )
    for field in (
        "candidate_id",
        "practice_mode",
        "formal_node_id",
        "anchor_formal_node_id",
        "source_id",
        "source_ref",
        "source_sha256",
        "mechanism_key",
        "knowledge_point",
        "review_unit_id",
        "gate_decision",
        "cross_edge_id",
    ):
        if payload.get(field) != selected_practice.get(field):
            raise LinkedPracticeSourceError(
                f"selection receipt {field} does not match selected practice"
            )
    if selected_practice.get("schedule_allowed") is not True:
        raise LinkedPracticeSourceError("selection receipt selection is not scheduleable")
    receipt_source_path, _ = resolve_source(
        repo,
        payload.get("source_ref"),
        personalization_state_dir=state_root,
    )
    if receipt_source_path != source_path:
        raise LinkedPracticeSourceError("selection receipt source identity mismatch")
    if payload.get("cross_edge_id") and payload.get(
        "cross_edge_live_enabled"
    ) is not True:
        raise LinkedPracticeSourceError("selection receipt cross edge is not live-enabled")
    cross_edge_id = str(payload.get("cross_edge_id") or "")
    if cross_edge_id:
        try:
            from cross_variant_pipeline_408 import (
                CrossVariantPipelineError,
                validate_current_live_cross_edge,
            )

            validate_current_live_cross_edge(
                repo,
                edge_id=cross_edge_id,
                required_point_id=str(payload.get("knowledge_point") or ""),
            )
        except (CrossVariantPipelineError, ImportError) as exc:
            raise LinkedPracticeSourceError(
                "selection receipt cross edge is no longer live or current projection is invalid"
            ) from exc
    return {
        "path": path,
        "selection_receipt_ref": normalized,
        "selection_receipt_sha256": digest,
        "state_root": state_root,
        "payload": payload,
    }


def _load_json_object(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise LinkedPracticeSourceError("linked source manifest is unreadable") from exc
    if not isinstance(value, dict):
        raise LinkedPracticeSourceError("linked source manifest must be an object")
    return value


def _details_root(repo: Path, manifest: dict[str, Any]) -> Path:
    root_spec = manifest.get("asset_root")
    if isinstance(root_spec, dict) and root_spec.get("kind") == "details_bridge_root":
        expected_vault = str(root_spec.get("details_vault") or "")
        vault_path = Path(expected_vault)
        if (
            not expected_vault
            or vault_path.is_absolute()
            or vault_path.name != expected_vault
            or expected_vault in {".", ".."}
            or not re.fullmatch(r"[A-Za-z0-9._-]+", expected_vault)
        ):
            raise LinkedPracticeSourceError("details vault identity is invalid")
        candidates: list[Path] = []
        if DEFAULT_BRIDGE_SNAPSHOT.is_file():
            try:
                bridge = json.loads(DEFAULT_BRIDGE_SNAPSHOT.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                bridge = None
            if (
                isinstance(bridge, dict)
                and Path(str(bridge.get("project_root") or "")).expanduser().resolve()
                == repo
                and (not expected_vault or bridge.get("details_vault") == expected_vault)
            ):
                candidates.append(
                    Path(str(bridge.get("details_root") or "")).expanduser().resolve()
                )
        if expected_vault == "kaoyan-408-details":
            candidates.append((repo.parent / expected_vault).resolve())
        for candidate in candidates:
            if candidate.is_dir():
                return candidate
        raise LinkedPracticeSourceError("trusted details root is unavailable")

    # Legacy v1 manifests used absolute paths.  They remain readable only when
    # every asset is within a trusted details root for this repository.
    sibling = (repo.parent / "kaoyan-408-details").resolve()
    if sibling.is_dir():
        return sibling
    if DEFAULT_BRIDGE_SNAPSHOT.is_file():
        try:
            bridge = json.loads(DEFAULT_BRIDGE_SNAPSHOT.read_text(encoding="utf-8"))
            if (
                isinstance(bridge, dict)
                and Path(str(bridge.get("project_root") or "")).expanduser().resolve()
                == repo
            ):
                candidate = Path(str(bridge.get("details_root") or "")).expanduser().resolve()
                if candidate.is_dir():
                    return candidate
        except (OSError, json.JSONDecodeError):
            pass
    raise LinkedPracticeSourceError("trusted details root is unavailable")


def _asset_path(raw: object, *, root: Path, allow_absolute: bool) -> Path:
    locator = Path(str(raw or "")).expanduser()
    if not str(raw or "") or ".." in locator.parts:
        raise LinkedPracticeSourceError("linked source asset locator is invalid")
    if locator.is_absolute():
        if not allow_absolute:
            raise LinkedPracticeSourceError("linked source asset must be relative")
        candidate = locator
    else:
        candidate = root / locator
    if candidate.is_symlink():
        raise LinkedPracticeSourceError("linked source asset must not be a symlink")
    path = candidate.resolve()
    if not _inside(path, root) or not path.is_file() or path.stat().st_size <= 0:
        raise LinkedPracticeSourceError("linked source asset is outside its trusted root")
    return path


def _validate_asset_entry(
    entry: object,
    *,
    root: Path,
    filename_re: re.Pattern[str],
    allow_absolute: bool,
) -> tuple[Path, str]:
    if not isinstance(entry, dict) or set(entry) != {"path", "sha256", "size_bytes"}:
        raise LinkedPracticeSourceError("linked source asset entry is invalid")
    path = _asset_path(entry.get("path"), root=root, allow_absolute=allow_absolute)
    digest = str(entry.get("sha256") or "")
    size = entry.get("size_bytes")
    if (
        not filename_re.fullmatch(path.name)
        or not SHA256_RE.fullmatch(digest)
        or not isinstance(size, int)
        or size <= 0
        or path.stat().st_size != size
        or sha256_file(path) != digest
    ):
        raise LinkedPracticeSourceError("linked source asset verification failed")
    return path, digest


def _validate_original_manifest(
    repo: Path, manifest_path: Path, payload: dict[str, Any], formal_node_id: str
) -> dict[str, Any]:
    assets = payload.get("question_assets")
    if (
        payload.get("schema") != QUESTION_SOURCE_SCHEMA
        or payload.get("answer_isolated") is not True
        or payload.get("formal_node_id") != formal_node_id
        or not FORMAL_NODE_RE.fullmatch(formal_node_id)
        or not isinstance(assets, list)
        or not assets
        or payload.get("question_asset_count") != len(assets)
    ):
        raise LinkedPracticeSourceError("original question manifest is not answer-isolated")
    trusted_root = _details_root(repo, payload)
    allow_absolute = not isinstance(payload.get("asset_root"), dict)
    verified: list[tuple[Path, str]] = [
        _validate_asset_entry(
            item,
            root=trusted_root,
            filename_re=QUESTION_ASSET_RE,
            allow_absolute=allow_absolute,
        )
        for item in assets
    ]
    if any(path.parent.name != formal_node_id for path, _ in verified):
        raise LinkedPracticeSourceError("question assets are not in the formal node directory")
    question_hashes = {digest for _, digest in verified}
    solution_hashes = {
        sha256_file(path)
        for path in verified[0][0].parent.iterdir()
        if path.is_file()
        and not path.is_symlink()
        and SOLUTION_ASSET_RE.fullmatch(path.name)
    }
    if question_hashes & solution_hashes:
        raise LinkedPracticeSourceError("question and solution assets overlap")
    attestation_spec = payload.get("answer_isolation_verification")
    if not isinstance(attestation_spec, dict) or set(attestation_spec) != {
        "schema",
        "attestation_ref",
        "attestation_sha256",
        "question_asset_set_sha256",
    }:
        raise LinkedPracticeSourceError(
            "original question manifest lacks independent answer-isolation verification"
        )
    attestation_ref = str(attestation_spec.get("attestation_ref") or "")
    attestation_locator = Path(attestation_ref)
    if (
        attestation_spec.get("schema") != QUESTION_ISOLATION_ATTESTATION_SCHEMA
        or not attestation_ref
        or attestation_locator.is_absolute()
        or ".." in attestation_locator.parts
        or not SHA256_RE.fullmatch(
            str(attestation_spec.get("attestation_sha256") or "")
        )
        or not SHA256_RE.fullmatch(
            str(attestation_spec.get("question_asset_set_sha256") or "")
        )
    ):
        raise LinkedPracticeSourceError(
            "original answer-isolation verification reference is invalid"
        )
    attestation_path = _asset_path(
        attestation_ref,
        root=trusted_root,
        allow_absolute=False,
    )
    if sha256_file(attestation_path) != attestation_spec["attestation_sha256"]:
        raise LinkedPracticeSourceError(
            "original answer-isolation verification receipt drifted"
        )
    expected_assets = [
        {
            "path": path.relative_to(trusted_root).as_posix(),
            "sha256": digest,
            "size_bytes": path.stat().st_size,
        }
        for path, digest in verified
    ]
    asset_set_sha = question_asset_set_sha256(expected_assets)
    if attestation_spec.get("question_asset_set_sha256") != asset_set_sha:
        raise LinkedPracticeSourceError(
            "original answer-isolation verification receipt is invalid"
        )
    try:
        validate_attestation_chain(
            details_root=trusted_root,
            attestation_path=attestation_path,
            formal_node_id=formal_node_id,
            expected_assets=expected_assets,
        )
    except QuestionSourceAttestationError as exc:
        raise LinkedPracticeSourceError(
            "original answer-isolation verification chain is invalid"
        ) from exc
    return {
        "kind": "original_manifest",
        "manifest_path": str(manifest_path),
        "formal_node_id": formal_node_id,
        "asset_count": len(verified),
        "answer_isolation_attestation_sha256": attestation_spec[
            "attestation_sha256"
        ],
    }


def _canonical_json_bytes(value: object) -> bytes:
    return (
        json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        + "\n"
    ).encode("utf-8")


def _canonical_digest(value: object) -> str:
    return hashlib.sha256(_canonical_json_bytes(value)).hexdigest()


def _provenance_file(
    repo: Path, locator: object, *, label: str
) -> tuple[Path, str]:
    raw = str(locator or "").strip()
    candidate = Path(raw).expanduser()
    if (
        not raw
        or "#" in raw
        or ".." in candidate.parts
        or any(marker in raw.lower() for marker in TEMP_MARKERS)
    ):
        raise LinkedPracticeSourceError(f"{label} locator is invalid")
    unresolved = candidate if candidate.is_absolute() else repo / candidate
    if unresolved.is_symlink():
        raise LinkedPracticeSourceError(f"{label} must not be a symlink")
    path = unresolved.resolve()
    if not path.is_file() or path.is_symlink() or path.stat().st_size <= 0:
        raise LinkedPracticeSourceError(f"{label} is not a durable regular file")
    try:
        normalized = path.relative_to(repo).as_posix()
    except ValueError:
        normalized = str(path)
    return path, normalized


def _variant_identity(raw: object, *, role: str) -> dict[str, str]:
    if not isinstance(raw, dict) or set(raw) != {
        "schema",
        "actor_id",
        "execution_id",
        "role",
    }:
        raise LinkedPracticeSourceError("variant actor identity is incomplete")
    result = {key: str(raw.get(key) or "") for key in raw}
    if (
        result["schema"] != VARIANT_IDENTITY_SCHEMA
        or result["role"] != role
        or not ACTOR_ID_RE.fullmatch(result["actor_id"])
        or not ACTOR_ID_RE.fullmatch(result["execution_id"])
    ):
        raise LinkedPracticeSourceError("variant actor identity is invalid")
    return result


def _validate_variant_provenance(
    repo: Path,
    manifest_path: Path,
    payload: dict[str, Any],
    receipt: dict[str, Any],
    verified: list[tuple[Path, str]],
    anchor: str,
) -> None:
    """Recheck the prepare-issued request and its complete identity/source chain."""

    durable = (repo / DEFAULT_LINKED_PRACTICE_RELATIVE).resolve()
    source_id = str(payload.get("source_id") or "")
    managed_source_dir = durable / "variant-sources" / source_id
    expected_source_dir = managed_source_dir.resolve()
    if (
        manifest_path.parent.resolve() != expected_source_dir
        or manifest_path.is_symlink()
        or managed_source_dir.is_symlink()
        or len(verified) != 1
    ):
        raise LinkedPracticeSourceError(
            "variant source is outside the managed verified-source directory"
        )

    request_path, request_ref = _provenance_file(
        repo, receipt.get("generation_request_ref"), label="generation request"
    )
    request_sha = sha256_file(request_path)
    request = _load_json_object(request_path)
    request_id = str(request.get("request_id") or "")
    expected_request_path = (
        durable / "variant-work" / request_id / "requests" / f"{request_sha}.json"
    ).resolve()
    key_path = durable / VARIANT_PREPARE_KEY_FILENAME
    try:
        key_info = key_path.lstat()
        key = key_path.read_bytes()
    except OSError as exc:
        raise LinkedPracticeSourceError(
            "variant prepare issuance key is unavailable"
        ) from exc
    unsigned_request = dict(request)
    prepare_signature = str(unsigned_request.pop("prepare_signature", ""))
    expected_signature = hmac.new(
        key, _canonical_json_bytes(unsigned_request), hashlib.sha256
    ).hexdigest()
    author_identity = _variant_identity(
        request.get("author_identity"), role="variant_author"
    )
    verifier_identity = _variant_identity(
        request.get("verifier_identity"), role="variant_verifier"
    )
    try:
        request_formal_write_count = int(request.get("formal_write_count", -1))
    except (TypeError, ValueError) as exc:
        raise LinkedPracticeSourceError(
            "variant generation request boundary is invalid"
        ) from exc
    if (
        request_path != expected_request_path
        or request_path.is_symlink()
        or request.get("schema") != VARIANT_REQUEST_SCHEMA
        or not VARIANT_REQUEST_ID_RE.fullmatch(request_id)
        or request.get("status")
        != "awaiting_independent_authoring_and_verification"
        or request.get("anchor_formal_node_id") != anchor
        or payload.get("knowledge_point") != request.get("knowledge_point")
        or payload.get("mechanism_key") != request.get("mechanism_key")
        or payload.get("review_unit_id") != request.get("review_unit_id")
        or receipt.get("generation_request_sha256") != request_sha
        or receipt.get("generation_request_ref") != request_ref
        or not stat.S_ISREG(key_info.st_mode)
        or stat.S_ISLNK(key_info.st_mode)
        or key_info.st_mode & 0o077
        or len(key) != 32
        or request.get("prepare_key_id") != hashlib.sha256(key).hexdigest()
        or receipt.get("prepare_key_id") != request.get("prepare_key_id")
        or not hmac.compare_digest(prepare_signature, expected_signature)
        or request.get("author_id") != author_identity["actor_id"]
        or request.get("author_execution_id") != author_identity["execution_id"]
        or request.get("author_identity_sha256")
        != _canonical_digest(author_identity)
        or request.get("verifier_id") != verifier_identity["actor_id"]
        or request.get("verifier_execution_id")
        != verifier_identity["execution_id"]
        or request.get("verifier_identity_sha256")
        != _canonical_digest(verifier_identity)
        or author_identity["actor_id"] == verifier_identity["actor_id"]
        or author_identity["execution_id"] == verifier_identity["execution_id"]
        or request_formal_write_count != 0
    ):
        raise LinkedPracticeSourceError(
            "variant generation request is not an authentic prepare artifact"
        )

    evidence_path, evidence_ref = _provenance_file(
        repo, request.get("authoring_evidence_ref"), label="authoring evidence"
    )
    evidence = _load_json_object(evidence_path)
    if (
        sha256_file(evidence_path) != request.get("authoring_evidence_sha256")
        or evidence_ref != request.get("authoring_evidence_ref")
        or evidence.get("schema") != VARIANT_EVIDENCE_SCHEMA
        or evidence.get("evidence_scope")
        != "full_question_and_authoritative_solution"
        or evidence.get("authoring_authority_verified") is not True
        or evidence.get("anchor_formal_node_id") != anchor
        or evidence.get("author_identity") != author_identity
        or evidence.get("verifier_identity") != verifier_identity
    ):
        raise LinkedPracticeSourceError("variant authoring evidence has drifted")

    original_path, original_ref = resolve_source(
        repo, request.get("anchor_source_ref")
    )
    if (
        original_ref != request.get("anchor_source_ref")
        or sha256_file(original_path) != request.get("anchor_source_sha256")
        or evidence.get("anchor_source_ref") != original_ref
        or evidence.get("anchor_source_sha256")
        != request.get("anchor_source_sha256")
    ):
        raise LinkedPracticeSourceError("variant anchor source has drifted")
    _validate_original_manifest(
        repo, original_path, _load_json_object(original_path), anchor
    )

    solution_path, solution_ref = _provenance_file(
        repo, request.get("sealed_solution_ref"), label="sealed solution"
    )
    if (
        solution_ref != request.get("sealed_solution_ref")
        or sha256_file(solution_path) != request.get("sealed_solution_sha256")
        or evidence.get("sealed_solution_ref") != solution_ref
        or evidence.get("sealed_solution_sha256")
        != request.get("sealed_solution_sha256")
        or solution_path == original_path
    ):
        raise LinkedPracticeSourceError("variant sealed solution has drifted")

    attestation_path, attestation_ref = _provenance_file(
        repo, receipt.get("attestation_ref"), label="variant attestation"
    )
    attestation_sha = sha256_file(attestation_path)
    attestation = _load_json_object(attestation_path)
    surface_sha = verified[0][1]
    identity_suffix = _canonical_digest(
        {
            "request_sha256": request_sha,
            "surface_sha256": surface_sha,
            "attestation_sha256": attestation_sha,
            "author_identity_sha256": request.get("author_identity_sha256"),
            "verifier_identity_sha256": request.get("verifier_identity_sha256"),
        }
    )[:16].upper()
    expected_source_id = f"VAR-{anchor[:2]}-{identity_suffix}"
    expected_receipt_id = f"VVR-{anchor[:2]}-{identity_suffix}"
    commitment = _canonical_digest(
        {
            "schema": "variant_authoring_chain_commitment_v1",
            "generation_request_id": request_id,
            "generation_request_sha256": request_sha,
            "anchor_formal_node_id": anchor,
            "anchor_source_sha256": request.get("anchor_source_sha256"),
            "authoring_evidence_sha256": request.get(
                "authoring_evidence_sha256"
            ),
            "sealed_solution_sha256": request.get("sealed_solution_sha256"),
            "author_identity_sha256": request.get("author_identity_sha256"),
            "verifier_identity_sha256": request.get("verifier_identity_sha256"),
            "question_surface_sha256": surface_sha,
        }
    )
    if (
        receipt.get("attestation_ref") != attestation_ref
        or receipt.get("attestation_sha256") != attestation_sha
        or payload.get("source_id") != expected_source_id
        or receipt.get("source_id") != expected_source_id
        or receipt.get("receipt_id") != expected_receipt_id
        or receipt.get("verified_at") != attestation.get("verified_at")
        or payload.get("verified_at") != attestation.get("verified_at")
        or attestation.get("schema") != VARIANT_ATTESTATION_SCHEMA
        or attestation.get("generation_request_id") != request_id
        or attestation.get("generation_request_sha256") != request_sha
        or attestation.get("author_id") != author_identity["actor_id"]
        or attestation.get("author_execution_id") != author_identity["execution_id"]
        or attestation.get("author_identity_sha256")
        != request.get("author_identity_sha256")
        or attestation.get("verifier_id") != verifier_identity["actor_id"]
        or attestation.get("verifier_execution_id")
        != verifier_identity["execution_id"]
        or attestation.get("verifier_identity_sha256")
        != request.get("verifier_identity_sha256")
        or attestation.get("anchor_source_sha256")
        != request.get("anchor_source_sha256")
        or attestation.get("authoring_evidence_sha256")
        != request.get("authoring_evidence_sha256")
        or attestation.get("sealed_solution_sha256")
        != request.get("sealed_solution_sha256")
        or attestation.get("question_surface_sha256") != surface_sha
        or attestation.get("authoring_chain_commitment_sha256") != commitment
        or receipt.get("authoring_chain_commitment_sha256") != commitment
        or any(
            attestation.get(field) is not True
            for field in (
                "answer_isolated",
                "unique_answer_verified",
                "distractors_verified",
                "same_target_verified",
                "changed_surface_verified",
            )
        )
    ):
        raise LinkedPracticeSourceError(
            "variant attestation is not bound to the prepared identity chain"
        )

    for field in (
        "author_id",
        "author_execution_id",
        "author_identity_sha256",
        "verifier_id",
        "verifier_execution_id",
        "verifier_identity_sha256",
        "anchor_source_ref",
        "anchor_source_sha256",
        "authoring_evidence_ref",
        "authoring_evidence_sha256",
        "sealed_solution_ref",
        "sealed_solution_sha256",
    ):
        if receipt.get(field) != request.get(field):
            raise LinkedPracticeSourceError(
                "variant receipt does not match the prepared request"
            )


def _validate_variant_manifest(
    repo: Path,
    manifest_path: Path,
    payload: dict[str, Any],
    variant_of_formal_node_id: str,
) -> dict[str, Any]:
    assets = payload.get("question_assets")
    source_id = str(payload.get("source_id") or "")
    if (
        payload.get("schema") != VARIANT_SOURCE_SCHEMA
        or payload.get("answer_isolated") is not True
        or payload.get("unique_answer_verified") is not True
        or payload.get("anchor_formal_node_id") != variant_of_formal_node_id
        or not FORMAL_NODE_RE.fullmatch(variant_of_formal_node_id)
        or not VARIANT_SOURCE_ID_RE.fullmatch(source_id)
        or not isinstance(assets, list)
        or not assets
        or payload.get("question_asset_count") != len(assets)
    ):
        raise LinkedPracticeSourceError("variant manifest lacks independent verification")
    root = manifest_path.parent.resolve()
    verified = [
        _validate_asset_entry(
            item,
            root=root,
            filename_re=VARIANT_ASSET_RE,
            allow_absolute=False,
        )
        for item in assets
    ]
    for path, _ in verified:
        if path.suffix.lower() == ".md":
            try:
                text = path.read_text(encoding="utf-8")
            except UnicodeError as exc:
                raise LinkedPracticeSourceError("variant text surface is not UTF-8") from exc
            if PROTECTED_VARIANT_TEXT_RE.search(text):
                raise LinkedPracticeSourceError("variant surface contains protected answer material")
    receipt_entry = payload.get("verification_receipt")
    if not isinstance(receipt_entry, dict) or set(receipt_entry) != {"path", "sha256"}:
        raise LinkedPracticeSourceError("variant verification receipt is missing")
    receipt_path = _asset_path(
        receipt_entry.get("path"), root=root, allow_absolute=False
    )
    receipt_sha = str(receipt_entry.get("sha256") or "")
    if (
        not SHA256_RE.fullmatch(receipt_sha)
        or sha256_file(receipt_path) != receipt_sha
    ):
        raise LinkedPracticeSourceError("variant verification receipt hash mismatch")
    receipt = _load_json_object(receipt_path)
    author_id = str(receipt.get("author_id") or "")
    verifier_id = str(receipt.get("verifier_id") or "")
    try:
        receipt_formal_write_count = int(receipt.get("formal_write_count", -1))
    except (TypeError, ValueError) as exc:
        raise LinkedPracticeSourceError(
            "variant verification receipt is invalid"
        ) from exc
    if (
        receipt.get("schema") != VARIANT_RECEIPT_SCHEMA
        or not RECEIPT_ID_RE.fullmatch(str(receipt.get("receipt_id") or ""))
        or receipt.get("source_id") != source_id
        or receipt.get("anchor_formal_node_id") != variant_of_formal_node_id
        or receipt.get("answer_isolated") is not True
        or receipt.get("unique_answer_verified") is not True
        or receipt.get("distractors_verified") is not True
        or receipt.get("same_target_verified") is not True
        or receipt.get("changed_surface_verified") is not True
        or not ACTOR_ID_RE.fullmatch(author_id)
        or not ACTOR_ID_RE.fullmatch(verifier_id)
        or author_id == verifier_id
        or not SHA256_RE.fullmatch(
            str(receipt.get("generation_request_sha256") or "")
        )
        or not SHA256_RE.fullmatch(
            str(receipt.get("attestation_sha256") or "")
        )
        or receipt_formal_write_count != 0
        or receipt.get("question_asset_sha256")
        != [digest for _, digest in verified]
    ):
        raise LinkedPracticeSourceError("variant verification receipt is invalid")
    _validate_variant_provenance(
        repo,
        manifest_path,
        payload,
        receipt,
        verified,
        variant_of_formal_node_id,
    )
    return {
        "kind": "variant_manifest",
        "manifest_path": str(manifest_path),
        "source_id": source_id,
        "anchor_formal_node_id": variant_of_formal_node_id,
        "asset_count": len(verified),
        "verification_receipt_id": receipt["receipt_id"],
    }


def validate_source_for_practice(
    repo: str | Path,
    locator: object,
    source_sha256: object,
    *,
    practice_mode: str,
    formal_node_id: str | None = None,
    variant_of_formal_node_id: str | None = None,
    personalization_state_dir: str | Path | None = None,
) -> dict[str, Any]:
    root = Path(repo).expanduser().resolve()
    path, normalized = resolve_source(
        root,
        locator,
        personalization_state_dir=personalization_state_dir,
    )
    expected_sha = str(source_sha256 or "")
    if not SHA256_RE.fullmatch(expected_sha) or sha256_file(path) != expected_sha:
        raise LinkedPracticeSourceError("source_ref does not match source_sha256")
    payload = _load_json_object(path)
    schema = payload.get("schema")
    if practice_mode == "original":
        if schema != QUESTION_SOURCE_SCHEMA or not formal_node_id:
            raise LinkedPracticeSourceError(
                "verified original requires a question source manifest"
            )
        details = _validate_original_manifest(root, path, payload, formal_node_id)
    elif practice_mode == "variant":
        if schema == QUESTION_SOURCE_SCHEMA:
            raise LinkedPracticeSourceError(
                "an original question manifest cannot be used as a variant surface"
            )
        if not variant_of_formal_node_id:
            raise LinkedPracticeSourceError(
                "verified variant requires an independent variant manifest"
            )
        if schema == VARIANT_SOURCE_SCHEMA:
            details = _validate_variant_manifest(
                root, path, payload, variant_of_formal_node_id
            )
        elif schema == CROSS_VARIANT_SOURCE_SCHEMA:
            # Delayed import avoids the intentional reverse dependency used by
            # cross-variant prepare when it validates its original question.
            try:
                from cross_variant_pipeline_408 import (
                    CrossVariantPipelineError,
                    validate_published_cross_variant_source,
                )

                details = validate_published_cross_variant_source(
                    root,
                    path,
                    expected_original_formal_node_id=variant_of_formal_node_id,
                )
            except CrossVariantPipelineError as exc:
                raise LinkedPracticeSourceError(
                    "cross variant failed its complete blind verification chain"
                ) from exc
        else:
            raise LinkedPracticeSourceError(
                "verified variant requires an independent variant manifest"
            )
    else:
        raise LinkedPracticeSourceError("practice_mode must be original or variant")
    return {
        "path": path,
        "source_ref": normalized,
        "source_sha256": expected_sha,
        "schema": schema,
        **details,
    }
