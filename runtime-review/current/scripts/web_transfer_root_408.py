#!/usr/bin/env python3
"""Where 408 web handoff files live: the verified T9 data disk (user rule of 2026-09-27).

Everything that only travels between this Mac and the GPT-6 Pro web projects is kept on
T9 instead of the internal disk:

* files generated here for upload (end-of-day enhancement input and its shards, morning
  selection input, evening review input) are written under T9 from the start;
* preserved copies of web returns are staged on T9;
* a web return downloaded to ~/Downloads is moved (or, when a verified T9 copy already
  exists, deleted) only after its local step has completed -- see ``archive_downloads``.

Only the real production details root is redirected; tests and temporary repositories keep
their own roots. New production artifacts require the verified T9 volume and registration
in Study-Storage-Map. An unavailable disk never silently sends cold files to local storage.
Historical local paths remain readable. Nothing is removed before its verified T9 copy
and durable file locator have been recorded.
"""
from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import shutil
import sys
import tempfile
from typing import Any

import runtime_paths_408 as _paths

sys.dont_write_bytecode = True
PRODUCTION_DETAILS = _paths.details_root()
T9_VOLUME = Path("/Volumes/T9-Data")
TRANSFER_ROOT = T9_VOLUME / "01_408" / "408网页中转"
KIND_DIRS = {
    "capture-day": "日终加强输入",
    "morning-input": "晨间选题输入",
    "evening-input": "晚间复盘输入",
    "web-return-staging": "网页回包保全",
    "downloads": "下载原件归档",
    # Finished D learning workspaces and other historical delivery directories (2026-09-27 move).
    "d-workspace": "D学习工作区",
    "history": "历史交付目录",
}
LOCAL_OVERRIDE_ENV = "KAOYAN408_WEB_TRANSFER_LOCAL"
COLD_STORAGE_MODULE = _paths.storage_map_root() / 'cold_storage.py'
_storage_module = None


def _cold_storage():
    global _storage_module
    if _storage_module is None:
        spec = importlib.util.spec_from_file_location("study_cold_storage_408", COLD_STORAGE_MODULE)
        if spec is None or spec.loader is None:
            raise RuntimeError("study_storage_registry_unavailable")
        module = importlib.util.module_from_spec(spec)
        sys.modules[spec.name] = module
        spec.loader.exec_module(module)
        _storage_module = module
    return _storage_module


def validate_cold_output(path: str | Path, *, details: str | Path = PRODUCTION_DETAILS) -> Path:
    """Production output guard; isolated repository fixtures retain their own roots."""
    path = Path(path).expanduser()
    if not _is_production(details):
        return path.resolve()
    return _cold_storage().ensure_output_path(path, "408")


def register_output(path: str | Path, *, details: str | Path = PRODUCTION_DETAILS,
                    purpose: str, aliases=()) -> dict[str, Any]:
    if not _is_production(details):
        return {"status": "NOT_PRODUCTION", "reason": "isolated_details_root"}
    return _cold_storage().register_tree(path, subject="408", purpose=purpose, aliases=aliases)


def t9_status() -> dict[str, Any]:
    """Verify the canonical T9 sentinel without writing anything."""
    try:
        from archive_408_conversation_package import verify_volume
        proof = verify_volume(T9_VOLUME)
    except Exception as exc:  # unmounted, wrong disk or sentinel drift: never write there
        return {"ready": False, "reason": f"{type(exc).__name__}: {exc}"}
    parent = T9_VOLUME / "01_408"
    if not parent.is_dir() or parent.is_symlink() or not os.access(parent, os.W_OK):
        return {"ready": False, "reason": "t9_01_408_not_writable"}
    return {"ready": True, "root": str(TRANSFER_ROOT), "sentinel": proof}


def _is_production(details: str | Path) -> bool:
    try:
        return Path(details).expanduser().resolve() == PRODUCTION_DETAILS.resolve()
    except OSError:
        return False


def base_for(kind: str, details: str | Path, local_rel: str) -> tuple[Path, dict[str, Any]]:
    """Return (directory, info) for one kind of web transfer file.

    ``local_rel`` stays the answer only for isolated, non-production repositories.
    """
    if kind not in KIND_DIRS:
        raise ValueError("web_transfer_kind_invalid:" + str(kind))
    local = Path(details).expanduser() / local_rel
    if not _is_production(details):
        return local, {"storage": "local", "reason": "non_production_details_root"}
    if os.environ.get(LOCAL_OVERRIDE_ENV) == "1":
        raise RuntimeError("local_web_transfer_override_disabled_use_verified_t9")
    status = t9_status()
    if not status["ready"]:
        raise RuntimeError("t9_unavailable_no_local_fallback:" + status["reason"])
    return validate_cold_output(TRANSFER_ROOT / KIND_DIRS[kind], details=details), {
        "storage": "t9", "root": str(TRANSFER_ROOT)}


def candidate_bases(kind: str, details: str | Path, local_rel: str) -> list[Path]:
    """Every location a previously exported file of this kind may legitimately be in."""
    if kind not in KIND_DIRS:
        raise ValueError("web_transfer_kind_invalid:" + str(kind))
    local = Path(details).expanduser() / local_rel
    rows = []
    if _is_production(details):
        rows.append(TRANSFER_ROOT / KIND_DIRS[kind])
    rows.append(local)
    return list(dict.fromkeys(rows))


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _copy_verified(source: Path, target: Path) -> str:
    """Copy one regular file to T9 atomically and return its verified SHA-256."""
    expected = _sha256(source)
    target.parent.mkdir(parents=True, exist_ok=True)
    if target.exists():
        if target.is_file() and not target.is_symlink() and _sha256(target) == expected:
            return expected
        raise ValueError("t9_destination_exists_with_other_bytes:" + str(target))
    fd, temporary = tempfile.mkstemp(prefix=".transfer-", dir=target.parent)
    os.close(fd)
    try:
        shutil.copy2(source, temporary)  # keep the original mode and timestamps
        with open(temporary, "rb") as stream:
            os.fsync(stream.fileno())
        if _sha256(Path(temporary)) != expected:
            raise ValueError("t9_copy_hash_mismatch:" + str(source))
        os.replace(temporary, target)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)
    return expected


def archive_downloads(paths: list[str | Path], *, kind: str, label: str,
                      keep_source: bool = False) -> dict[str, Any]:
    """Move downloaded web files (already consumed locally) into T9, verified by SHA-256.

    ``label`` is a date or batch name used as the folder, e.g. ``2026-09-26``.  A source is
    removed only after its T9 copy verifies; ``keep_source`` leaves it in place.
    """
    status = t9_status()
    if not status["ready"]:
        return {"status": "T9_UNAVAILABLE", "reason": status["reason"], "moved": [], "write_count": 0}
    if not label or "/" in label or label.startswith("."):
        raise ValueError("web_transfer_label_invalid")
    if kind not in {"enhancement", "morning-D", "evening", "other"}:
        raise ValueError("web_transfer_download_kind_invalid")
    folder = validate_cold_output(TRANSFER_ROOT / KIND_DIRS["downloads"] / kind / label)
    stage_kind = {"enhancement": "enhancement", "morning-D": "morning"}.get(kind)
    rows = []
    for raw in paths:
        source = Path(raw).expanduser()
        if not source.is_file() or source.is_symlink():
            rows.append({"source": str(source), "status": "SOURCE_MISSING"})
            continue
        digest = _sha256(source)
        staged = (TRANSFER_ROOT / KIND_DIRS["web-return-staging"] / stage_kind / digest / "original.zip"
                  if stage_kind else None)
        if staged is not None and staged.is_file() and not staged.is_symlink() and _sha256(staged) == digest:
            # The preserved web-return copy on T9 is already byte-identical: keep one copy only.
            target = staged
        else:
            target = folder / source.name
            digest = _copy_verified(source, target)
        # Registration persists the verified target and its old locator before removing
        # a local duplicate. Failure leaves the source available for a safe retry.
        registration = register_output(target, purpose="consumed-web-download", aliases=(source,))
        removed = False
        if not keep_source and source.resolve() != target.resolve():
            if source.is_symlink() or _sha256(source) != digest:
                raise ValueError("web_transfer_source_changed_before_remove:" + str(source))
            source.unlink()
            removed = True
        rows.append({"source": str(source), "t9_path": str(target), "sha256": digest,
                     "size_bytes": target.stat().st_size, "source_removed": removed,
                     "storage_registration": registration,
                     "status": "ARCHIVED_ON_T9"})
    receipt = {"schema": "cs408-web-transfer-archive-v1", "kind": kind, "label": label,
               "archived_at": dt.datetime.now(dt.timezone.utc).isoformat(), "rows": rows,
               "formal_write_count": 0, "learning_write_count": 0}
    folder.mkdir(parents=True, exist_ok=True)
    stamp = dt.datetime.now().strftime("%Y%m%dT%H%M%S%f")
    receipt_path = folder / f"archive-receipt-{stamp}.json"
    receipt_path.write_text(
        json.dumps(receipt, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    registration = register_output(receipt_path, purpose="web-download-archive-receipt")
    ok = all(row["status"] == "ARCHIVED_ON_T9" for row in rows)
    return {**receipt, "status": "ARCHIVED" if ok else "PARTIAL", "folder": str(folder),
            "storage_registration": registration}


def move_tree(source: str | Path, *, kind: str, relative: str) -> dict[str, Any]:
    """Move a finished local transfer directory (e.g. an old staging copy) under T9.

    Every file is copied and verified before the local tree is removed.
    """
    status = t9_status()
    if not status["ready"]:
        return {"status": "T9_UNAVAILABLE", "reason": status["reason"], "write_count": 0}
    source = Path(source).expanduser()
    if not source.is_dir() or source.is_symlink():
        raise ValueError("web_transfer_source_tree_invalid:" + str(source))
    source = source.resolve()
    if any(p.is_symlink() for p in source.rglob("*")):
        raise ValueError("web_transfer_source_tree_has_symlink")
    if (kind not in KIND_DIRS or not relative or relative == "."
            or Path(relative).is_absolute() or ".." in Path(relative).parts):
        raise ValueError("web_transfer_target_invalid")
    target = validate_cold_output(TRANSFER_ROOT / KIND_DIRS[kind] / relative)
    if source == target or source in target.parents or target in source.parents:
        raise ValueError("web_transfer_source_target_overlap")
    files = sorted(p for p in source.rglob("*") if p.is_file())
    digests = {p.relative_to(source).as_posix(): _sha256(p) for p in files}
    total = 0
    target.mkdir(parents=True, exist_ok=True)
    for directory in sorted(p for p in source.rglob("*") if p.is_dir()):
        (target / directory.relative_to(source)).mkdir(parents=True, exist_ok=True)
    for path in files:
        _copy_verified(path, target / path.relative_to(source))
        total += path.stat().st_size
    registration = register_output(target, purpose="completed-web-workspace", aliases=(source,))
    current = {p.relative_to(source).as_posix(): _sha256(p)
               for p in source.rglob("*") if p.is_file()}
    if any(p.is_symlink() for p in source.rglob("*")) or current != digests:
        raise ValueError("web_transfer_source_tree_changed_before_remove")
    shutil.rmtree(source)
    return {"status": "MOVED_TO_T9", "source": str(source), "t9_path": str(target),
            "file_count": len(files), "bytes": total, "formal_write_count": 0,
            "storage_registration": registration}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("status")
    downloads = sub.add_parser("archive-downloads")
    downloads.add_argument("--kind", required=True, choices=["enhancement", "morning-D", "evening", "other"])
    downloads.add_argument("--label", required=True)
    downloads.add_argument("--file", action="append", required=True, type=Path)
    downloads.add_argument("--keep-source", action="store_true")
    tree = sub.add_parser("move-tree")
    tree.add_argument("--kind", required=True, choices=sorted(KIND_DIRS))
    tree.add_argument("--source", required=True, type=Path)
    tree.add_argument("--relative", required=True)
    args = parser.parse_args()
    if args.command == "status":
        result = t9_status()
    elif args.command == "archive-downloads":
        result = archive_downloads(args.file, kind=args.kind, label=args.label, keep_source=args.keep_source)
    else:
        result = move_tree(args.source, kind=args.kind, relative=args.relative)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if result.get("status") not in {"T9_UNAVAILABLE", "PARTIAL"} and result.get("ready", True) else 2


if __name__ == "__main__":
    raise SystemExit(main())
