#!/usr/bin/env python3
"""Stable Obsidian display assets for sealed 408 conversation packages.

The complete package is temporary local evidence and later moves to T9.  A formal
details card may therefore embed only bytes promoted into the stable details vault.
This module validates the sealed package-bound plan, deterministically derives every
target, renders one package-scoped managed block, and verifies the closure receipt.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import mimetypes
import re
from pathlib import Path
from typing import Any

import conversation_package_408 as package
from image_integrity_408 import ImageIntegrityError, validate_image_bytes


PLAN_SCHEMA = "cs408-display-asset-plan-v1"
CLOSURE_SCHEMA = "cs408-obsidian-asset-closure-receipt-v1"
STABLE_PATH_POLICY = "kaoyan-408-details-formal-id-content-hash-v1"
CLOSURE_ROOT = Path(".cs408-display-assets/closures")
FORMAL_ID_RE = re.compile(r"^(?:DS|CO|OS|CN)_(?:\d{4}|UNK)_\d{3}$")
SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
EMBED_RE = re.compile(r"!\[\[([^\]|#]+)(?:[|#][^\]]*)?\]\]")
MARKDOWN_IMAGE_RE = re.compile(r"!\[[^\]]*\]\(([^)]+)\)")
IMAGE_ROLES = {
    "question_image",
    "solution_image",
    "explanation_image",
    "user_work_image",
    "source_article_image",
}
ROLE_VISIBILITY = {
    "question_image": "practice_safe",
    "source_article_image": "practice_safe",
    "solution_image": "protected",
    "explanation_image": "protected",
    "user_work_image": "protected",
}
ROLE_PREFIX = {
    "question_image": "question",
    "solution_image": "solution",
    "explanation_image": "explanation",
    "user_work_image": "user-work",
    "source_article_image": "source-article",
}
FORBIDDEN_REFERENCE_FRAGMENTS = (
    "/.codex/kaoyan-408-conversation-packages/",
    "~/.codex/kaoyan-408-conversation-packages/",
    "/Volumes/T9-Data/",
    "cs408-conversation-package://",
)


class DisplayAssetError(RuntimeError):
    """A display plan or closure failed closed."""


def _canonical_bytes(value: Any) -> bytes:
    return json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")


def _pretty_bytes(value: Any) -> bytes:
    return (
        json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    ).encode("utf-8")


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _safe_relative(value: Any, label: str) -> Path:
    path = Path(str(value or ""))
    if (
        path.is_absolute()
        or not path.parts
        or any(part in {"", ".", ".."} for part in path.parts)
    ):
        raise DisplayAssetError(f"{label} must be a safe relative path")
    return path


def _manifest_attachment_rows(opened: dict[str, Any]) -> list[dict[str, Any]]:
    manifest = opened.get("manifest") or {}
    rows = manifest.get("attachments") or []
    if not isinstance(rows, list) or any(not isinstance(row, dict) for row in rows):
        raise DisplayAssetError("conversation package attachment manifest is invalid")
    return rows


def normalize_plan(
    plan: Any,
    opened: dict[str, Any],
    *,
    mode: str,
    formal_id: str | None,
    package_locator: str,
    package_sha256: str,
) -> dict[str, Any]:
    """Validate and normalize a Sol-selected plan against exact package bytes.

    Sol selects only disposition and visibility.  It cannot choose a filesystem
    target; the writer derives targets from formal ID, role and source hash.
    """

    if not isinstance(plan, dict):
        raise DisplayAssetError("display_asset_plan must be an object")
    allowed = {
        "schema_version",
        "subject",
        "package_id",
        "package_locator",
        "package_sha256",
        "mode",
        "formal_id",
        "stable_path_policy",
        "items",
        "image_attachment_count",
        "no_display",
    }
    if set(plan) - allowed:
        raise DisplayAssetError("display_asset_plan contains unknown fields")
    manifest = opened.get("manifest") or {}
    package_id = str(manifest.get("package_id") or "")
    if (
        plan.get("schema_version") != PLAN_SCHEMA
        or plan.get("subject") != "cs408"
        or plan.get("package_id") != package_id
        or plan.get("package_locator") != package_locator
        or plan.get("package_sha256") != package_sha256
        or plan.get("mode") != mode
        or plan.get("formal_id") != formal_id
        or plan.get("stable_path_policy") != STABLE_PATH_POLICY
    ):
        raise DisplayAssetError("display_asset_plan identity drifted")
    if mode not in {"new", "redo"}:
        raise DisplayAssetError("display_asset_plan mode must be new or redo")
    if mode == "new" and formal_id is not None:
        raise DisplayAssetError("new display plan cannot preallocate a formal ID")
    if mode == "redo" and not FORMAL_ID_RE.fullmatch(str(formal_id or "")):
        raise DisplayAssetError("redo display plan requires an exact formal ID")

    rows = _manifest_attachment_rows(opened)
    by_path = {str(row.get("relative_path") or ""): row for row in rows}
    if len(by_path) != len(rows):
        raise DisplayAssetError("package attachment paths are not unique")
    raw_items = plan.get("items")
    if not isinstance(raw_items, list):
        raise DisplayAssetError("display_asset_plan.items must be a list")
    normalized_items: list[dict[str, Any]] = []
    seen: set[str] = set()
    for item in raw_items:
        if not isinstance(item, dict):
            raise DisplayAssetError("display_asset_plan item must be an object")
        item_allowed = {
            "package_relative_path",
            "attachment_role",
            "mime_type",
            "byte_count",
            "source_sha256",
            "visibility",
            "disposition",
        }
        if set(item) != item_allowed:
            raise DisplayAssetError("display_asset_plan item fields drifted")
        relative = str(_safe_relative(item.get("package_relative_path"), "package attachment"))
        if relative in seen or relative not in by_path:
            raise DisplayAssetError("display_asset_plan item is duplicate or unbound")
        seen.add(relative)
        source = by_path[relative]
        role = str(source.get("role") or "")
        expected = {
            "package_relative_path": relative,
            "attachment_role": role,
            "mime_type": str(source.get("mime_type") or "").lower(),
            "byte_count": source.get("byte_count"),
            "source_sha256": str(source.get("sha256") or "").lower(),
            "visibility": item.get("visibility"),
            "disposition": item.get("disposition"),
        }
        if any(item.get(key) != value for key, value in expected.items()):
            raise DisplayAssetError("display_asset_plan item drifted from manifest")
        if role in IMAGE_ROLES:
            if item["visibility"] != ROLE_VISIBILITY[role]:
                raise DisplayAssetError("display asset visibility violates answer protection")
            if item["disposition"] not in {"promoted", "archive_only"}:
                raise DisplayAssetError("image disposition must be promoted or archive_only")
        elif role == "other_attachment":
            if item["disposition"] != "archive_only" or item["visibility"] != "protected":
                raise DisplayAssetError("other_attachment is archive-only and protected")
        else:
            raise DisplayAssetError("display_asset_plan attachment role is unsupported")
        if not SHA256_RE.fullmatch(expected["source_sha256"]):
            raise DisplayAssetError("display asset source SHA is invalid")
        normalized_items.append(expected)

    relevant_paths = {
        str(row.get("relative_path") or "")
        for row in rows
        if str(row.get("role") or "") in IMAGE_ROLES | {"other_attachment"}
    }
    if seen != relevant_paths:
        raise DisplayAssetError("display_asset_plan must classify every package attachment")
    image_count = sum(
        1 for row in rows if str(row.get("role") or "") in IMAGE_ROLES
    )
    promoted_count = sum(
        1 for item in normalized_items if item["disposition"] == "promoted"
    )
    no_display = promoted_count == 0
    if plan.get("image_attachment_count") != image_count or plan.get("no_display") is not no_display:
        raise DisplayAssetError("display_asset_plan image count/no-display proof drifted")
    normalized = {
        "schema_version": PLAN_SCHEMA,
        "subject": "cs408",
        "package_id": package_id,
        "package_locator": package_locator,
        "package_sha256": package_sha256,
        "mode": mode,
        "formal_id": formal_id,
        "stable_path_policy": STABLE_PATH_POLICY,
        "items": normalized_items,
        "image_attachment_count": image_count,
        "no_display": no_display,
    }
    if _canonical_bytes(plan) != _canonical_bytes(normalized):
        raise DisplayAssetError("display_asset_plan is not in canonical normalized form")
    return normalized


def build_default_plan(
    opened: dict[str, Any],
    *,
    package_locator: str,
    package_sha256: str,
    mode: str,
    formal_id: str | None,
    archive_only_paths: set[str] | None = None,
) -> dict[str, Any]:
    """Build a deterministic starting plan; callers may only demote named images."""

    archive_only = archive_only_paths or set()
    items: list[dict[str, Any]] = []
    for row in _manifest_attachment_rows(opened):
        role = str(row.get("role") or "")
        if role not in IMAGE_ROLES | {"other_attachment"}:
            continue
        relative = str(row.get("relative_path") or "")
        disposition = (
            "archive_only"
            if role == "other_attachment" or relative in archive_only
            else "promoted"
        )
        items.append(
            {
                "package_relative_path": relative,
                "attachment_role": role,
                "mime_type": str(row.get("mime_type") or "").lower(),
                "byte_count": row.get("byte_count"),
                "source_sha256": str(row.get("sha256") or "").lower(),
                "visibility": ROLE_VISIBILITY.get(role, "protected"),
                "disposition": disposition,
            }
        )
    plan = {
        "schema_version": PLAN_SCHEMA,
        "subject": "cs408",
        "package_id": str((opened.get("manifest") or {}).get("package_id") or ""),
        "package_locator": package_locator,
        "package_sha256": package_sha256,
        "mode": mode,
        "formal_id": formal_id,
        "stable_path_policy": STABLE_PATH_POLICY,
        "items": items,
        "image_attachment_count": sum(
            1 for item in items if item["attachment_role"] in IMAGE_ROLES
        ),
        "no_display": not any(item["disposition"] == "promoted" for item in items),
    }
    return normalize_plan(
        plan,
        opened,
        mode=mode,
        formal_id=formal_id,
        package_locator=package_locator,
        package_sha256=package_sha256,
    )


def plan_sha256(plan: dict[str, Any]) -> str:
    return _sha256(_canonical_bytes(plan))


def _extension(mime_type: str, detected_format: str) -> str:
    return {"png": ".png", "jpeg": ".jpg", "webp": ".webp"}.get(
        detected_format,
        mimetypes.guess_extension(mime_type) or ".bin",
    )


def _managed_block(package_id: str, assets: list[dict[str, Any]]) -> str:
    start = f"<!-- cs408-display-assets-v1:{package_id}:start -->"
    end = f"<!-- cs408-display-assets-v1:{package_id}:end -->"
    lines = [start, "", "## 原始资料与归档", ""]
    for item in assets:
        if item["disposition"] == "archive_only":
            continue
        if item["visibility"] == "practice_safe":
            lines.extend(["> [!question]- 原始题面资料", f"> ![[{item['embed_target']}]]", ""])
        else:
            lines.extend(["> [!answer]- 受保护原始资料", f"> ![[{item['embed_target']}]]", ""])
    lines.append(end)
    return "\n".join(lines)


def _patch_managed_block(text: str, package_id: str, block: str | None) -> str:
    start = f"<!-- cs408-display-assets-v1:{package_id}:start -->"
    end = f"<!-- cs408-display-assets-v1:{package_id}:end -->"
    starts = [match.start() for match in re.finditer(re.escape(start), text)]
    ends = [match.end() for match in re.finditer(re.escape(end), text)]
    if len(starts) != len(ends) or len(starts) > 1:
        raise DisplayAssetError("details card managed display block is malformed")
    if block is None:
        if starts:
            raise DisplayAssetError("no-display plan conflicts with an existing package block")
        return text
    if starts:
        if starts[0] >= ends[0]:
            raise DisplayAssetError("details card managed display block order is invalid")
        return text[: starts[0]] + block + text[ends[0] :]
    return text.rstrip() + "\n\n" + block + "\n"


def unsafe_reference_rows(text: str, *, markdown_path: str) -> list[dict[str, str]]:
    rows: list[dict[str, str]] = []
    for target in [*EMBED_RE.findall(text), *MARKDOWN_IMAGE_RE.findall(text)]:
        normalized = target.strip().strip("<>")
        if (
            any(fragment in normalized for fragment in FORBIDDEN_REFERENCE_FRAGMENTS)
            or normalized.startswith("attachments/")
            or normalized.startswith("inbox/")
        ):
            rows.append({"markdown_path": markdown_path, "target": normalized})
    return rows


def project_package_assets(
    *,
    plan: dict[str, Any],
    formal_id: str,
    mode: str,
    details_root: Path,
    card_bytes: bytes,
    private_root: str | Path | None = None,
) -> tuple[dict[Path, bytes], dict[str, Any], list[str]]:
    """Return WAL-ready details files and one closure receipt."""

    if not FORMAL_ID_RE.fullmatch(formal_id):
        raise DisplayAssetError("formal ID is invalid for display projection")
    opened = package.read_package(plan["package_locator"], private_root=private_root)
    normalized = normalize_plan(
        plan,
        opened,
        mode=mode,
        formal_id=None if mode == "new" else formal_id,
        package_locator=plan["package_locator"],
        package_sha256=plan["package_sha256"],
    )
    package_dir = Path(str(opened["package_dir"])).resolve()
    details = details_root.resolve()
    try:
        card_text = card_bytes.decode("utf-8")
    except UnicodeError as exc:
        raise DisplayAssetError("details card is not UTF-8") from exc
    projected: dict[Path, bytes] = {}
    closure_items: list[dict[str, Any]] = []
    logs: list[str] = []
    for item in normalized["items"]:
        base = {
            "package_relative_path": item["package_relative_path"],
            "attachment_role": item["attachment_role"],
            "mime_type": item["mime_type"],
            "byte_count": item["byte_count"],
            "source_sha256": item["source_sha256"],
            "visibility": item["visibility"],
        }
        if item["disposition"] == "archive_only":
            closure_items.append(
                {**base, "disposition": "archive_only", "stable_vault_relative_path": None, "stable_sha256": None, "embed_target": None}
            )
            continue
        source = (package_dir / _safe_relative(item["package_relative_path"], "package attachment")).resolve()
        try:
            source.relative_to(package_dir)
        except ValueError as exc:
            raise DisplayAssetError("package attachment escaped package root") from exc
        raw = source.read_bytes()
        if len(raw) != item["byte_count"] or _sha256(raw) != item["source_sha256"]:
            raise DisplayAssetError("package attachment bytes drifted")
        try:
            detected_format, detected_mime = validate_image_bytes(raw)
        except ImageIntegrityError as exc:
            raise DisplayAssetError(f"package image integrity failed: {exc.code}") from exc
        if detected_mime != item["mime_type"]:
            raise DisplayAssetError("package image declared/detected MIME mismatch")
        filename = (
            f"{ROLE_PREFIX[item['attachment_role']]}-"
            f"{item['source_sha256'][:16]}{_extension(item['mime_type'], detected_format)}"
        )
        target = (details / "assets" / formal_id / filename).resolve()
        try:
            target.relative_to(details / "assets" / formal_id)
        except ValueError as exc:
            raise DisplayAssetError("derived stable asset target escaped details vault") from exc
        if target.exists():
            if target.is_symlink() or not target.is_file() or _sha256(target.read_bytes()) != item["source_sha256"]:
                raise DisplayAssetError("stable asset target exists with different bytes")
            disposition = "already_stable"
        else:
            disposition = "promoted"
        projected[target] = raw
        relative = target.relative_to(details).as_posix()
        closure_items.append(
            {
                **base,
                "disposition": disposition,
                "stable_vault_relative_path": relative,
                "stable_sha256": item["source_sha256"],
                "embed_target": relative,
            }
        )
        logs.append(f"details stable asset: {relative} ({disposition})")

    promoted = [item for item in closure_items if item["embed_target"]]
    block = _managed_block(normalized["package_id"], promoted) if promoted else None
    final_card_text = _patch_managed_block(card_text, normalized["package_id"], block)
    card_relative = f"cards/{formal_id}.md"
    unsafe = unsafe_reference_rows(final_card_text, markdown_path=card_relative)
    if unsafe:
        raise DisplayAssetError("details card still references temporary/package/T9 image paths")
    for item in promoted:
        if f"![[{item['embed_target']}]]" not in final_card_text:
            raise DisplayAssetError("stable asset embed was not rendered")
    final_card = final_card_text.encode("utf-8")
    card_path = details / card_relative
    projected[card_path] = final_card
    receipt = {
        "schema_version": CLOSURE_SCHEMA,
        "subject": "cs408",
        "package_id": normalized["package_id"],
        "package_locator": normalized["package_locator"],
        "package_sha256": normalized["package_sha256"],
        "formal_id": formal_id,
        "mode": mode,
        "display_asset_plan_sha256": plan_sha256(normalized),
        "stable_path_policy": STABLE_PATH_POLICY,
        "markdown_vault_relative_path": card_relative,
        "markdown_sha256": _sha256(final_card),
        "items": closure_items,
        "stable_asset_count": len(promoted),
        "no_display": not promoted,
        "no_display_proof": (
            "sealed_plan_has_no_promoted_assets" if not promoted else None
        ),
        "embed_resolution": "verified",
        "obsidian_reread": "verified",
        "verification_algorithm": "cs408-display-asset-closure-v1",
    }
    receipt["receipt_id"] = "CS408-DISPLAY-" + _sha256(_canonical_bytes(receipt))[:20]
    closure_path = details / closure_relative_path(normalized["package_id"], formal_id)
    receipt_raw = _pretty_bytes(receipt)
    if closure_path.exists() and closure_path.read_bytes() != receipt_raw:
        raise DisplayAssetError("display closure receipt target conflicts")
    projected[closure_path] = receipt_raw
    logs.append(f"details display closure: {closure_path.relative_to(details).as_posix()}")
    return projected, receipt, logs


def closure_relative_path(package_id: str, formal_id: str) -> Path:
    if not package.SAFE_ID_RE.fullmatch(package_id) or not FORMAL_ID_RE.fullmatch(formal_id):
        raise DisplayAssetError("closure identity is invalid")
    return CLOSURE_ROOT / package_id / f"{formal_id}.json"


def verify_closure_receipt(
    *,
    details_root: str | Path,
    package_id: str,
    package_sha256: str,
    formal_id: str,
    display_asset_plan_sha256: str,
) -> dict[str, Any]:
    details = Path(details_root).expanduser().resolve()
    path = details / closure_relative_path(package_id, formal_id)
    raw = path.read_bytes()
    try:
        receipt = json.loads(raw.decode("utf-8"))
    except (UnicodeError, json.JSONDecodeError) as exc:
        raise DisplayAssetError("display closure receipt is unreadable") from exc
    if not isinstance(receipt, dict) or receipt.get("schema_version") != CLOSURE_SCHEMA:
        raise DisplayAssetError("display closure receipt schema invalid")
    if (
        receipt.get("package_id") != package_id
        or receipt.get("package_sha256") != package_sha256
        or receipt.get("formal_id") != formal_id
        or receipt.get("display_asset_plan_sha256") != display_asset_plan_sha256
        or receipt.get("embed_resolution") != "verified"
        or receipt.get("obsidian_reread") != "verified"
    ):
        raise DisplayAssetError("display closure receipt identity drifted")
    card_rel = _safe_relative(receipt.get("markdown_vault_relative_path"), "details card")
    card = (details / card_rel).resolve()
    try:
        card.relative_to(details)
    except ValueError as exc:
        raise DisplayAssetError("display closure card escaped details root") from exc
    card_raw = card.read_bytes()
    if _sha256(card_raw) != receipt.get("markdown_sha256"):
        raise DisplayAssetError("display closure card hash drifted")
    card_text = card_raw.decode("utf-8")
    if unsafe_reference_rows(card_text, markdown_path=card_rel.as_posix()):
        raise DisplayAssetError("display closure card has a temporary binary reference")
    stable_count = 0
    for item in receipt.get("items") or []:
        if not isinstance(item, dict):
            raise DisplayAssetError("display closure item invalid")
        stable_rel = item.get("stable_vault_relative_path")
        if item.get("disposition") == "archive_only":
            if any(item.get(key) is not None for key in ("stable_vault_relative_path", "stable_sha256", "embed_target")):
                raise DisplayAssetError("archive-only closure item claims a stable display")
            continue
        rel = _safe_relative(stable_rel, "stable asset")
        target = (details / rel).resolve()
        try:
            target.relative_to(details / "assets" / formal_id)
        except ValueError as exc:
            raise DisplayAssetError("stable asset escaped formal details directory") from exc
        asset_raw = target.read_bytes()
        if (
            len(asset_raw) != item.get("byte_count")
            or _sha256(asset_raw) != item.get("stable_sha256")
            or item.get("stable_sha256") != item.get("source_sha256")
        ):
            raise DisplayAssetError("stable asset bytes drifted")
        if f"![[{item.get('embed_target')}]]" not in card_text:
            raise DisplayAssetError("stable asset embed no longer resolves from card")
        stable_count += 1
    if stable_count != receipt.get("stable_asset_count"):
        raise DisplayAssetError("display closure stable asset count drifted")
    if bool(receipt.get("no_display")) != (stable_count == 0):
        raise DisplayAssetError("display closure no-display proof drifted")
    return {
        "schema_version": CLOSURE_SCHEMA,
        "status": "verified",
        "receipt_id": receipt.get("receipt_id"),
        "receipt_relative_path": path.relative_to(details).as_posix(),
        "receipt_sha256": _sha256(raw),
        "stable_asset_count": stable_count,
        "no_display": stable_count == 0,
        "markdown_vault_relative_path": card_rel.as_posix(),
        "markdown_sha256": receipt.get("markdown_sha256"),
    }


def audit_details_vault(details_root: str | Path) -> dict[str, Any]:
    """Read-only audit; it never rewrites the existing production cards."""

    root = Path(details_root).expanduser().resolve()
    cards = root / "cards"
    embedded = 0
    broken: list[dict[str, str]] = []
    unsafe: list[dict[str, str]] = []
    for card in sorted(cards.glob("*.md")):
        text = card.read_text(encoding="utf-8")
        relative = card.relative_to(root).as_posix()
        unsafe.extend(unsafe_reference_rows(text, markdown_path=relative))
        for target_text in EMBED_RE.findall(text):
            target = target_text.strip()
            suffix = Path(target).suffix.lower()
            if suffix not in {".png", ".jpg", ".jpeg", ".webp", ".gif"}:
                continue
            embedded += 1
            resolved = (
                (card.parent / target).resolve()
                if target.startswith("./") or target.startswith("../")
                else (root / target).resolve()
            )
            try:
                resolved.relative_to(root)
            except ValueError:
                broken.append({"markdown_path": relative, "target": target, "reason": "escaped_root"})
                continue
            if not resolved.is_file():
                broken.append({"markdown_path": relative, "target": target, "reason": "missing"})
    material = {"embedded": embedded, "broken": broken, "unsafe": unsafe}
    return {
        "schema_version": "cs408-details-display-asset-audit-v1",
        "status": "pass" if not broken and not unsafe else "fail",
        "card_count": len(list(cards.glob("*.md"))),
        "image_embed_count": embedded,
        "broken_embed_count": len(broken),
        "unsafe_reference_count": len(unsafe),
        "broken": broken,
        "unsafe": unsafe,
        "audit_sha256": _sha256(_canonical_bytes(material)),
        "write_count": 0,
    }


def formal_reference_scan(
    *, repo: str | Path, details_root: str | Path
) -> dict[str, Any]:
    repo_path = Path(repo).expanduser().resolve()
    details = Path(details_root).expanduser().resolve()
    findings: list[dict[str, str]] = []
    for root, label in ((repo_path, "formal"), (details, "details")):
        for path in sorted(root.rglob("*.md")):
            if ".git" in path.parts:
                continue
            text = path.read_text(encoding="utf-8")
            rel = f"{label}:{path.relative_to(root).as_posix()}"
            findings.extend(unsafe_reference_rows(text, markdown_path=rel))
    material = {"roots": [str(repo_path), str(details)], "findings": findings}
    return {
        "schema_version": "cs408-formal-binary-reference-scan-v1",
        "status": "pass" if not findings else "fail",
        "finding_count": len(findings),
        "findings": findings,
        "scan_sha256": _sha256(_canonical_bytes(material)),
        "write_count": 0,
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    subs = parser.add_subparsers(dest="command", required=True)
    audit = subs.add_parser("audit")
    audit.add_argument("--details-root", required=True)
    scan = subs.add_parser("formal-reference-scan")
    scan.add_argument("--repo", required=True)
    scan.add_argument("--details-root", required=True)
    build = subs.add_parser("build-plan")
    build.add_argument("--package-locator", required=True)
    build.add_argument("--package-sha256", required=True)
    build.add_argument("--mode", choices=("new", "redo"), required=True)
    build.add_argument("--formal-id")
    build.add_argument("--archive-only-path", action="append", default=[])
    args = parser.parse_args()
    try:
        if args.command == "audit":
            result = audit_details_vault(args.details_root)
        elif args.command == "formal-reference-scan":
            result = formal_reference_scan(repo=args.repo, details_root=args.details_root)
        else:
            opened = package.read_package(args.package_locator)
            if opened["manifest"].get("canonical_sha256") != args.package_sha256:
                raise DisplayAssetError("package SHA drifted")
            result = build_default_plan(
                opened,
                package_locator=args.package_locator,
                package_sha256=args.package_sha256,
                mode=args.mode,
                formal_id=args.formal_id,
                archive_only_paths=set(args.archive_only_path),
            )
        print(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True))
        if args.command in {"audit", "formal-reference-scan"}:
            return 0 if result["status"] == "pass" else 1
        return 0
    except (DisplayAssetError, OSError, UnicodeError) as exc:
        print(json.dumps({"status": "failed", "error": str(exc)}, ensure_ascii=False))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
