#!/usr/bin/env python3
"""Private full-conversation packages for one 408 study item.

New packages are subject-local and do not depend on Study Intake runtime state.
The tracked repository stores only opaque locators and hashes.  Full question,
answer, feedback, dialogue, and attachment bytes stay below the private root.
"""

from __future__ import annotations

import contextlib
import datetime as dt
import fcntl
import hashlib
import json
import mimetypes
import os
import re
import stat
import tempfile
from pathlib import Path
from typing import Any, Iterator


SCHEMA = "cs408-conversation-package-v1"
MANIFEST_SCHEMA = "cs408-conversation-package-manifest-v1"
CONVERSATION_SCHEMA = "cs408-full-conversation-v1"
SOURCE_SCHEMA = "cs408-conversation-source-v1"
RECEIPT_SCHEMA = "cs408-conversation-package-receipt-v1"
LEGACY_VIEW_SCHEMA = "cs408-legacy-evidence-view-v1"
LOCATOR_PREFIX = "cs408-conversation-package://"
BUFFERING_STATUS = "buffering_current_question"
PENDING_FORMAL_STATUS = "awaiting_sol_formalization"
DEFAULT_ROOT = Path("/Users/your-user/.codex/kaoyan-408-conversation-packages")
TIMEZONE = "Asia/Shanghai"
SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
SAFE_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{2,159}$")
ATTACHMENT_ROLES = {
    "question_image",
    "solution_image",
    "explanation_image",
    "user_work_image",
    "source_article_image",
    "other_attachment",
}
MAX_ATTACHMENT_BYTES = 64 * 1024 * 1024
MAX_ATTACHMENTS_PER_REVISION = 128
MAX_CONVERSATION_BYTES = 64 * 1024 * 1024
LEGACY_USER_EVIDENCE_ROOTS = (
    "objects",
    "attachments",
    "manifests",
    "answer-display-lifecycles",
    "answer-operations",
    "evaluation-capsules",
    "formal-problem-document-receipts",
    "recoveries",
    "trace-supplements",
    "turns",
    "observations",
)
LEGACY_RETIRED_EXCLUDED_ROOTS = (
    "background-handoffs",
    "knowledge-snapshots",
)


class ConversationPackageError(RuntimeError):
    """The private package is unsafe, malformed, or hash-drifted."""


def _json_bytes(value: Any, *, pretty: bool = True) -> bytes:
    options: dict[str, Any] = {"ensure_ascii": False, "sort_keys": True}
    if pretty:
        options["indent"] = 2
    else:
        options["separators"] = (",", ":")
    return (json.dumps(value, **options) + "\n").encode("utf-8")


def _sha256(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


def _root(value: str | Path | None) -> Path:
    configured = os.environ.get("KAOYAN_408_CONVERSATION_PACKAGE_ROOT")
    path = (
        Path(value).expanduser()
        if value is not None
        else Path(configured).expanduser()
        if configured
        else DEFAULT_ROOT
    )
    if path.exists() and stat.S_ISLNK(path.lstat().st_mode):
        raise ConversationPackageError("conversation package root cannot be a symlink")
    resolved = path.resolve()
    if resolved == Path(resolved.anchor):
        raise ConversationPackageError("conversation package root is too broad")
    return resolved


def _ensure_private_dir(path: Path, *, root: Path) -> None:
    try:
        path.relative_to(root)
    except ValueError as exc:
        raise ConversationPackageError("conversation package path escaped root") from exc
    root.mkdir(parents=True, exist_ok=True, mode=0o700)
    cursor = root
    os.chmod(cursor, 0o700)
    for part in path.relative_to(root).parts:
        cursor = cursor / part
        if cursor.exists():
            info = cursor.lstat()
            if stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode):
                raise ConversationPackageError("conversation package directory is unsafe")
        else:
            cursor.mkdir(mode=0o700)
        if cursor.stat().st_uid != os.getuid():
            raise ConversationPackageError("conversation package ownership drifted")
        os.chmod(cursor, 0o700)


def _atomic_write(path: Path, raw: bytes, *, root: Path) -> None:
    _ensure_private_dir(path.parent, root=root)
    descriptor, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    tmp = Path(temporary)
    try:
        os.fchmod(descriptor, 0o600)
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(raw)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp, path)
        os.chmod(path, 0o600)
    finally:
        if tmp.exists():
            tmp.unlink()


@contextlib.contextmanager
def _package_lock(package_id: str, *, root: Path) -> Iterator[None]:
    lock_dir = root / ".locks"
    _ensure_private_dir(lock_dir, root=root)
    path = lock_dir / f"{package_id}.lock"
    descriptor = os.open(path, os.O_CREAT | os.O_RDWR, 0o600)
    try:
        os.fchmod(descriptor, 0o600)
        fcntl.flock(descriptor, fcntl.LOCK_EX)
        yield
    finally:
        fcntl.flock(descriptor, fcntl.LOCK_UN)
        os.close(descriptor)


def _read_json(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise ConversationPackageError(f"conversation package JSON unreadable: {path}") from exc
    if not isinstance(value, dict):
        raise ConversationPackageError(f"conversation package JSON is not an object: {path}")
    return value


def package_id_for(
    *, study_date: str, session_id: str | None, item_id: str | None, source_id: str
) -> str:
    try:
        dt.date.fromisoformat(study_date)
    except ValueError as exc:
        raise ConversationPackageError("study_date_invalid") from exc
    material = _json_bytes(
        {
            "subject": "cs408",
            "study_date": study_date,
            "session_id": session_id,
            "item_id": item_id,
            "source_id": source_id,
        },
        pretty=False,
    )
    return f"CS408-{study_date.replace('-', '')}-{_sha256(material)[:16]}"


def _normalize_turns(raw: object) -> list[dict[str, Any]]:
    if raw is None:
        events = []
    elif isinstance(raw, dict):
        events = raw.get("events")
    else:
        events = raw
    if not isinstance(events, list):
        raise ConversationPackageError("conversation events must be a list")
    result: list[dict[str, Any]] = []
    for ordinal, event in enumerate(events, start=1):
        if not isinstance(event, dict):
            raise ConversationPackageError("conversation event must be an object")
        role = str(event.get("role") or "")
        if role == "learner":
            role = "user"
        if role not in {"user", "assistant"}:
            raise ConversationPackageError("conversation role invalid")
        kind = str(event.get("kind") or "utterance")
        text = event.get("text", event.get("content"))
        if not isinstance(text, str) or not text:
            raise ConversationPackageError("conversation text invalid")
        result.append(
            {
                "ordinal": ordinal,
                "role": role,
                "kind": kind,
                "text": text,
                "observed_at": event.get("observed_at"),
            }
        )
    raw_bytes = _json_bytes(result, pretty=False)
    if len(raw_bytes) > MAX_CONVERSATION_BYTES:
        raise ConversationPackageError("conversation exceeds private safety ceiling")
    return result


def _merge_conversation(
    existing: list[dict[str, Any]], incoming: list[dict[str, Any]]
) -> list[dict[str, Any]]:
    def material(row: dict[str, Any]) -> tuple[str, str, str]:
        return (str(row["role"]), str(row["kind"]), str(row["text"]))

    existing_material = [material(row) for row in existing]
    incoming_material = [material(row) for row in incoming]
    if incoming_material[: len(existing_material)] == existing_material:
        merged = incoming
    elif existing_material[: len(incoming_material)] == incoming_material:
        merged = existing
    else:
        merged = [*existing, *incoming]
    return [{**row, "ordinal": index} for index, row in enumerate(merged, start=1)]


def _normalize_attachment_descriptor(value: dict[str, Any]) -> tuple[dict[str, Any], bytes]:
    if not isinstance(value, dict):
        raise ConversationPackageError("attachment descriptor invalid")
    data = value.get("data")
    if not isinstance(data, bytes) or not 0 < len(data) <= MAX_ATTACHMENT_BYTES:
        raise ConversationPackageError("attachment bytes invalid")
    role = str(value.get("role") or "other_attachment")
    if role not in ATTACHMENT_ROLES:
        raise ConversationPackageError("attachment role invalid")
    mime_type = str(value.get("mime_type") or "application/octet-stream").lower()
    label = str(value.get("label") or role)
    if not label or len(label.encode("utf-8")) > 512:
        raise ConversationPackageError("attachment label invalid")
    digest = _sha256(data)
    return (
        {
            "role": role,
            "mime_type": mime_type,
            "label": label,
            "byte_count": len(data),
            "sha256": digest,
        },
        data,
    )


def _attachment_name(ordinal: int, digest: str, mime_type: str) -> str:
    extension = mimetypes.guess_extension(mime_type) or ".bin"
    if not re.fullmatch(r"\.[A-Za-z0-9]{1,8}", extension):
        extension = ".bin"
    return f"{ordinal:04d}-{digest}{extension}"


def _package_paths(root: Path, study_date: str, package_id: str) -> tuple[Path, Path]:
    if not SAFE_ID_RE.fullmatch(package_id):
        raise ConversationPackageError("package_id_invalid")
    day = root / study_date
    return day, day / package_id


def record_package(
    *,
    study_date: str,
    session_id: str | None,
    item_id: str | None,
    source_id: str,
    source: dict[str, Any],
    conversation: object,
    frozen_feedback: str,
    attachments: list[dict[str, Any]] | None = None,
    missing_fields: list[str] | None = None,
    seal: bool = False,
    private_root: str | Path | None = None,
) -> dict[str, Any]:
    """Create or append the complete package for one current item.

    A revision snapshot is immutable.  The five top-level files are the current
    projection and may advance until ``seal`` is true.
    """

    root = _root(private_root)
    package_id = package_id_for(
        study_date=study_date,
        session_id=session_id,
        item_id=item_id,
        source_id=source_id,
    )
    _, package_dir = _package_paths(root, study_date, package_id)
    turns = _normalize_turns(conversation)
    with _package_lock(package_id, root=root):
        _ensure_private_dir(package_dir / "attachments", root=root)
        receipt_path = package_dir / "receipt.json"
        if receipt_path.is_file():
            prior_receipt = _read_json(receipt_path)
            current_conversation = _read_json(package_dir / "conversation.json")
            prior_turns = current_conversation.get("turns")
            if not isinstance(prior_turns, list):
                raise ConversationPackageError("existing conversation is invalid")
            prior_core = [
                row for row in prior_turns if row.get("kind") != "frozen_feedback"
            ]
            turns = _merge_conversation(prior_core, turns)
            revision = int(prior_receipt.get("revision", 0)) + 1
            prior_attachments = list(
                (_read_json(package_dir / "manifest.json").get("attachments") or [])
            )
        else:
            revision = 1
            prior_attachments = []

        if frozen_feedback:
            turns.append(
                {
                    "ordinal": len(turns) + 1,
                    "role": "assistant",
                    "kind": "frozen_feedback",
                    "text": frozen_feedback,
                    "observed_at": None,
                }
            )
        if receipt_path.is_file() and prior_receipt.get("sealed") is True:
            current_conversation = _read_json(package_dir / "conversation.json")
            if current_conversation.get("turns") != turns or attachments:
                raise ConversationPackageError("sealed conversation package is immutable")
            # Exact retries of a sealed terminal package are byte-idempotent.
            # Do not manufacture a new revision whose manifest would collide
            # with the existing content-addressed snapshot.
            return {
                **prior_receipt,
                "manifest_sha256": prior_receipt["canonical_sha256"],
                "evidence_status": "ready",
            }

        attachment_rows = list(prior_attachments)
        known = {(row.get("sha256"), row.get("role"), row.get("label")) for row in attachment_rows}
        for descriptor in attachments or []:
            metadata, data = _normalize_attachment_descriptor(descriptor)
            identity = (metadata["sha256"], metadata["role"], metadata["label"])
            if identity in known:
                continue
            if len(attachment_rows) >= MAX_ATTACHMENTS_PER_REVISION:
                raise ConversationPackageError("too many package attachments")
            filename = _attachment_name(
                len(attachment_rows) + 1,
                metadata["sha256"],
                metadata["mime_type"],
            )
            relpath = str(Path("attachments") / filename)
            _atomic_write(package_dir / relpath, data, root=root)
            attachment_rows.append({**metadata, "relative_path": relpath})
            known.add(identity)

        conversation_value = {
            "schema_version": CONVERSATION_SCHEMA,
            "turns": turns,
            "turn_count": len(turns),
            "complete_order_preserved": True,
            "truncated": False,
        }
        source_value = {
            "schema_version": SOURCE_SCHEMA,
            "subject": "cs408",
            "study_date": study_date,
            "timezone": TIMEZONE,
            "session_id": session_id,
            "item_id": item_id,
            "source_id": source_id,
            "source": source,
        }
        conversation_raw = _json_bytes(conversation_value)
        source_raw = _json_bytes(source_value)
        file_rows = [
            {
                "relative_path": "conversation.json",
                "role": "conversation",
                "mime_type": "application/json",
                "byte_count": len(conversation_raw),
                "sha256": _sha256(conversation_raw),
            },
            {
                "relative_path": "source.json",
                "role": "source",
                "mime_type": "application/json",
                "byte_count": len(source_raw),
                "sha256": _sha256(source_raw),
            },
        ]
        file_rows.extend(
            {
                "relative_path": row["relative_path"],
                "role": row["role"],
                "mime_type": row["mime_type"],
                "byte_count": row["byte_count"],
                "sha256": row["sha256"],
            }
            for row in attachment_rows
        )
        canonical_material = {
            "schema_version": SCHEMA,
            "package_id": package_id,
            "subject": "cs408",
            "study_date": study_date,
            "timezone": TIMEZONE,
            "session_id": session_id,
            "item_id": item_id,
            "source_id": source_id,
            # Sealing is part of the durable evidence identity.  Without this
            # bit, an unsealed draft and its terminal projection can share a
            # canonical hash even though only one is Capture-admissible.
            "sealed": bool(seal),
            "files": file_rows,
            "missing_fields": sorted({str(item) for item in missing_fields or [] if str(item)}),
            "formal_write_count": 0,
            "background_processing": "none",
        }
        canonical_sha = _sha256(_json_bytes(canonical_material, pretty=False))
        manifest = {
            **canonical_material,
            "schema_version": MANIFEST_SCHEMA,
            "revision": revision,
            "sealed": bool(seal),
            "attachments": attachment_rows,
            "canonical_sha256": canonical_sha,
        }
        receipt = {
            "schema_version": RECEIPT_SCHEMA,
            "package_id": package_id,
            "locator": LOCATOR_PREFIX + package_id,
            "study_date": study_date,
            "revision": revision,
            "sealed": bool(seal),
            "canonical_sha256": canonical_sha,
            # An unsealed revision is only the private same-item draft.  It is
            # deliberately not a pending-formal item and is never admissible
            # as Capture evidence.  Only the terminal sealed projection can
            # cross the Capture boundary.
            "status": (
                PENDING_FORMAL_STATUS if seal else BUFFERING_STATUS
            ),
            "formal_write_count": 0,
            "background_processing": "none",
        }
        revision_dir = package_dir / ".revisions" / canonical_sha
        _ensure_private_dir(revision_dir, root=root)
        for name, raw in (
            ("conversation.json", conversation_raw),
            ("source.json", source_raw),
            ("manifest.json", _json_bytes(manifest)),
            ("receipt.json", _json_bytes(receipt)),
        ):
            target = revision_dir / name
            if target.exists() and target.read_bytes() != raw:
                raise ConversationPackageError("immutable revision conflict")
            if not target.exists():
                _atomic_write(target, raw, root=root)
        # Publish the mutable current projection only after the immutable
        # revision is fully materialized and conflict-free.  A revision
        # conflict can therefore never leave a half-switched top-level view.
        _atomic_write(package_dir / "conversation.json", conversation_raw, root=root)
        _atomic_write(package_dir / "source.json", source_raw, root=root)
        _atomic_write(package_dir / "manifest.json", _json_bytes(manifest), root=root)
        _atomic_write(package_dir / "receipt.json", _json_bytes(receipt), root=root)
        return {
            **receipt,
            "manifest_sha256": canonical_sha,
            "evidence_status": "ready",
        }


def read_package(locator: str, *, private_root: str | Path | None = None) -> dict[str, Any]:
    if not isinstance(locator, str) or not locator.startswith(LOCATOR_PREFIX):
        raise ConversationPackageError("conversation package locator invalid")
    package_id = locator.removeprefix(LOCATOR_PREFIX)
    if not SAFE_ID_RE.fullmatch(package_id):
        raise ConversationPackageError("conversation package locator invalid")
    root = _root(private_root)
    matches = list(root.glob(f"????-??-??/{package_id}"))
    if len(matches) != 1:
        raise ConversationPackageError("conversation package is missing or ambiguous")
    package_dir = matches[0]
    manifest = _read_json(package_dir / "manifest.json")
    conversation = _read_json(package_dir / "conversation.json")
    source = _read_json(package_dir / "source.json")
    receipt = _read_json(package_dir / "receipt.json")
    for row in manifest.get("files") or []:
        if not isinstance(row, dict):
            raise ConversationPackageError("manifest file row invalid")
        path = package_dir / str(row.get("relative_path") or "")
        try:
            path.resolve(strict=True).relative_to(package_dir.resolve())
            raw = path.read_bytes()
        except (OSError, ValueError) as exc:
            raise ConversationPackageError("manifest file missing or escaped") from exc
        if len(raw) != row.get("byte_count") or _sha256(raw) != row.get("sha256"):
            raise ConversationPackageError("manifest file hash drifted")
    if receipt.get("canonical_sha256") != manifest.get("canonical_sha256"):
        raise ConversationPackageError("package receipt hash drifted")
    return {
        "package_dir": str(package_dir),
        "manifest": manifest,
        "conversation": conversation,
        "source": source,
        "receipt": receipt,
    }


def verify_package_binding(
    locator: str,
    *,
    expected_canonical_sha256: str | None = None,
    require_sealed: bool | None = None,
    private_root: str | Path | None = None,
) -> dict[str, Any]:
    """Reopen one current-format package and recompute its canonical identity.

    ``read_package`` verifies every manifest-listed byte and the receipt-to-
    manifest hash equality.  This stronger public verifier additionally
    rebuilds the canonical material from the manifest, checks the package
    identity and surface topology, and optionally binds the result to an
    independently supplied canonical SHA.  New transaction consumers should
    use this function at trust boundaries; historical readers may continue to
    use ``read_package`` for compatibility.
    """

    opened = read_package(locator, private_root=private_root)
    manifest = opened["manifest"]
    conversation = opened["conversation"]
    source = opened["source"]
    receipt = opened["receipt"]
    package_id = locator.removeprefix(LOCATOR_PREFIX)

    if (
        manifest.get("schema_version") != MANIFEST_SCHEMA
        or conversation.get("schema_version") != CONVERSATION_SCHEMA
        or source.get("schema_version") != SOURCE_SCHEMA
        or receipt.get("schema_version") != RECEIPT_SCHEMA
    ):
        raise ConversationPackageError("conversation package schema drifted")

    canonical_material = {
        key: value
        for key, value in manifest.items()
        if key not in {"schema_version", "revision", "attachments", "canonical_sha256"}
    }
    canonical_material["schema_version"] = SCHEMA
    recomputed = _sha256(_json_bytes(canonical_material, pretty=False))
    canonical_sha = manifest.get("canonical_sha256")
    if (
        not isinstance(canonical_sha, str)
        or not SHA256_RE.fullmatch(canonical_sha)
        or recomputed != canonical_sha
    ):
        raise ConversationPackageError("package canonical hash drifted")
    if expected_canonical_sha256 is not None and canonical_sha != expected_canonical_sha256:
        raise ConversationPackageError("package canonical binding drifted")

    study_date = manifest.get("study_date")
    session_id = manifest.get("session_id")
    item_id = manifest.get("item_id")
    source_id = manifest.get("source_id")
    if not all(isinstance(value, str) and value for value in (study_date, source_id)):
        raise ConversationPackageError("package identity is incomplete")
    expected_package_id = package_id_for(
        study_date=str(study_date),
        session_id=session_id if isinstance(session_id, str) else None,
        item_id=item_id if isinstance(item_id, str) else None,
        source_id=str(source_id),
    )
    sealed = manifest.get("sealed")
    expected_status = PENDING_FORMAL_STATUS if sealed is True else BUFFERING_STATUS
    if (
        package_id != expected_package_id
        or manifest.get("package_id") != package_id
        or manifest.get("subject") != "cs408"
        or manifest.get("timezone") != TIMEZONE
        or not isinstance(sealed, bool)
        or manifest.get("formal_write_count") != 0
        or manifest.get("background_processing") != "none"
        or source.get("subject") != "cs408"
        or source.get("study_date") != study_date
        or source.get("timezone") != TIMEZONE
        or source.get("session_id") != session_id
        or source.get("item_id") != item_id
        or source.get("source_id") != source_id
        or receipt.get("package_id") != package_id
        or receipt.get("locator") != locator
        or receipt.get("study_date") != study_date
        or receipt.get("revision") != manifest.get("revision")
        or receipt.get("sealed") is not sealed
        or receipt.get("canonical_sha256") != canonical_sha
        or receipt.get("status") != expected_status
        or receipt.get("formal_write_count") != 0
        or receipt.get("background_processing") != "none"
    ):
        raise ConversationPackageError("package identity binding drifted")
    if require_sealed is not None and sealed is not require_sealed:
        raise ConversationPackageError("package seal binding drifted")

    files = manifest.get("files")
    attachments = manifest.get("attachments")
    if not isinstance(files, list) or not isinstance(attachments, list):
        raise ConversationPackageError("package manifest topology invalid")
    relative_paths = [
        row.get("relative_path") if isinstance(row, dict) else None for row in files
    ]
    if (
        any(not isinstance(value, str) or not value for value in relative_paths)
        or len(relative_paths) != len(set(relative_paths))
        or relative_paths.count("conversation.json") != 1
        or relative_paths.count("source.json") != 1
    ):
        raise ConversationPackageError("package manifest file topology drifted")
    attachment_paths = [
        row.get("relative_path") if isinstance(row, dict) else None
        for row in attachments
    ]
    if (
        any(
            not isinstance(value, str)
            or not value.startswith("attachments/")
            for value in attachment_paths
        )
        or len(attachment_paths) != len(set(attachment_paths))
        or set(relative_paths) != {"conversation.json", "source.json", *attachment_paths}
    ):
        raise ConversationPackageError("package attachment topology drifted")
    file_by_path = {
        str(row["relative_path"]): row for row in files if isinstance(row, dict)
    }
    for attachment in attachments:
        if not isinstance(attachment, dict):
            raise ConversationPackageError("package attachment row invalid")
        file_row = file_by_path.get(str(attachment.get("relative_path") or ""))
        if file_row is None or any(
            file_row.get(field) != attachment.get(field)
            for field in ("relative_path", "role", "mime_type", "byte_count", "sha256")
        ):
            raise ConversationPackageError("package attachment binding drifted")

    turns = conversation.get("turns")
    if (
        not isinstance(turns, list)
        or conversation.get("turn_count") != len(turns)
        or conversation.get("complete_order_preserved") is not True
        or conversation.get("truncated") is not False
        or any(
            not isinstance(row, dict) or row.get("ordinal") != ordinal
            for ordinal, row in enumerate(turns, start=1)
        )
    ):
        raise ConversationPackageError("package conversation topology drifted")

    return {
        **opened,
        "canonical_sha256": canonical_sha,
        "package_id": package_id,
        "sealed": sealed,
        "status": expected_status,
        "files_verified": True,
        "canonical_recomputed": True,
    }


def _legacy_tree_rows(root: Path, groups: tuple[str, ...]) -> list[dict[str, Any]]:
    if not root.exists():
        return []
    if stat.S_ISLNK(root.lstat().st_mode) or not root.is_dir():
        raise ConversationPackageError("legacy evidence root is unsafe")
    root_resolved = root.resolve(strict=True)
    rows: list[dict[str, Any]] = []
    for group in groups:
        directory = root / group
        if not directory.exists():
            continue
        directory_info = directory.lstat()
        if stat.S_ISLNK(directory_info.st_mode) or not stat.S_ISDIR(directory_info.st_mode):
            raise ConversationPackageError("legacy evidence group is unsafe")
        for path in sorted(directory.rglob("*")):
            info = path.lstat()
            if stat.S_ISLNK(info.st_mode):
                raise ConversationPackageError("legacy evidence symlink is forbidden")
            if stat.S_ISDIR(info.st_mode):
                continue
            if not stat.S_ISREG(info.st_mode):
                raise ConversationPackageError("legacy evidence entry is not a regular file")
            try:
                path.resolve(strict=True).relative_to(root_resolved)
            except (OSError, ValueError) as exc:
                raise ConversationPackageError("legacy evidence path escaped root") from exc
            raw = path.read_bytes()
            relative_path = str(path.relative_to(root))
            rows.append(
                {
                    "group": group,
                    "relative_path": relative_path,
                    "sha256": _sha256(raw),
                    "byte_count": len(raw),
                    "source_path": str(path),
                }
            )
    return rows


def legacy_inventory(legacy_root: str | Path | None = None) -> list[dict[str, Any]]:
    if legacy_root is None:
        raise ConversationPackageError("legacy_root must be explicit for read-only migration")
    root = Path(legacy_root).expanduser()
    return _legacy_tree_rows(root, LEGACY_USER_EVIDENCE_ROOTS)


def _retired_excluded_inventory(root: Path) -> list[dict[str, Any]]:
    rows = _legacy_tree_rows(root, LEGACY_RETIRED_EXCLUDED_ROOTS)
    result: list[dict[str, Any]] = []
    for group in LEGACY_RETIRED_EXCLUDED_ROOTS:
        group_rows = [row for row in rows if row["group"] == group]
        if not group_rows and not (root / group).exists():
            continue
        material = [
            {
                "relative_path": row["relative_path"],
                "sha256": row["sha256"],
                "byte_count": row["byte_count"],
            }
            for row in group_rows
        ]
        result.append(
            {
                "root": group,
                "object_count": len(group_rows),
                "tree_sha256": _sha256(_json_bytes(material, pretty=False)),
                "reason": "retired_background_only_not_user_learning_evidence",
            }
        )
    return result


def migrate_legacy(
    *,
    mode: str,
    legacy_root: str | Path | None = None,
    private_root: str | Path | None = None,
) -> dict[str, Any]:
    if mode not in {"dry-run", "migrate", "verify"}:
        raise ConversationPackageError("migration mode invalid")
    if legacy_root is None:
        raise ConversationPackageError("legacy_root must be explicit for read-only migration")
    source_root = Path(legacy_root).expanduser()
    target_root = _root(private_root)
    rows = legacy_inventory(source_root)
    retired_excluded = _retired_excluded_inventory(source_root)
    migrated = 0
    mismatches: list[str] = []
    for row in rows:
        relative = Path(row["relative_path"])
        if relative.is_absolute() or ".." in relative.parts:
            raise ConversationPackageError("legacy inventory relative path is unsafe")
        target = target_root / "legacy-verified" / relative
        if mode == "migrate":
            raw = Path(row["source_path"]).read_bytes()
            if target.exists():
                if target.is_symlink() or not target.is_file() or target.read_bytes() != raw:
                    raise ConversationPackageError("legacy migration target conflict")
            else:
                _atomic_write(target, raw, root=target_root)
            migrated += 1
        if mode == "verify":
            if target.is_symlink() or not target.is_file():
                mismatches.append(f"missing:{row['relative_path']}")
            else:
                raw = target.read_bytes()
                if len(raw) != row["byte_count"] or _sha256(raw) != row["sha256"]:
                    mismatches.append(f"hash:{row['relative_path']}")
    inventory_material = [
        {
            "relative_path": row["relative_path"],
            "sha256": row["sha256"],
            "byte_count": row["byte_count"],
        }
        for row in rows
    ]
    inventory_sha = _sha256(_json_bytes(inventory_material, pretty=False))
    excluded_count = sum(int(item["object_count"]) for item in retired_excluded)
    view = {
        "schema_version": LEGACY_VIEW_SCHEMA,
        "source_root": str(source_root.resolve()),
        "target_root": str(target_root),
        "included_roots": list(LEGACY_USER_EVIDENCE_ROOTS),
        "object_count": len(rows),
        "inventory_sha256": inventory_sha,
        "tree_sha256": inventory_sha,
        "retired_excluded": retired_excluded,
        "retired_excluded_object_count": excluded_count,
        "formal_write_count": 0,
        "background_processing": "none",
    }
    if mode == "migrate":
        _atomic_write(
            target_root / "legacy-verified" / "verified-view.json",
            _json_bytes(view),
            root=target_root,
        )
    return {
        **view,
        "mode": mode,
        "migrated_count": migrated,
        "mismatch_count": len(mismatches),
        "mismatches": mismatches,
    }
__all__ = [
    "BUFFERING_STATUS",
    "ConversationPackageError",
    "DEFAULT_ROOT",
    "LOCATOR_PREFIX",
    "PENDING_FORMAL_STATUS",
    "migrate_legacy",
    "package_id_for",
    "read_package",
    "record_package",
    "verify_package_binding",
]
