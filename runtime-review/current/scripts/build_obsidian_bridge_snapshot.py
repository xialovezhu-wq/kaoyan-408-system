#!/usr/bin/env python3
"""Build a safe Codex-to-Obsidian open-link snapshot for 408 detail cards."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import tempfile
from datetime import datetime
from pathlib import Path
from urllib.parse import parse_qs, quote, unquote, urlencode, urlparse
from question_visual_links_408 import BRIDGE_BASE

import runtime_paths_408 as _paths

try:
    from relation_semantic_review import _embedded_detail_assets
except ModuleNotFoundError:  # Support importing as scripts.build_obsidian_bridge_snapshot.
    from scripts.relation_semantic_review import _embedded_detail_assets


PROJECT_ROOT = Path(__file__).resolve().parents[1]
DETAILS_ROOT = _paths.details_root()
DEFAULT_SNAPSHOT = _paths.codex_home() / 'kaoyan-408-obsidian-bridge/records.json'
DEFAULT_VAULT = "kaoyan-408"
DEFAULT_DETAILS_VAULT = "kaoyan-408-details"
NODE_ID_RE = re.compile(r"^(DS|CO|OS|CN)_(?:\d{4}|UNK)_\d{3}$")
NODE_CARD_RE = re.compile(r"^(DS|CO|OS|CN)_(?:\d{4}|UNK)_\d{3}\.md$")
QUESTION_ASSET_RE = re.compile(
    r"^question-(?:\d{2}|[0-9a-f]{16}|[0-9a-f]{64})\.(?:png|jpe?g|webp)$", re.IGNORECASE
)
SOLUTION_ASSET_RE = re.compile(
    r"^solution-(?:\d{2}|[0-9a-f]{16}|[0-9a-f]{64})\.(?:png|jpe?g|webp)$", re.IGNORECASE
)
ANSWER_ISOLATION_ATTESTATION_FILENAME = "answer-isolation-attestation.json"

SAFE_SEARCH_LABELS = [
    "正式节点 ID",
    "来源 ID",
    "年份",
    "科目",
    "主模块",
    "主知识点",
    "副知识点",
    "命中知识点",
    "详情入口",
    "MarginNote 定位",
    "MarginNote 定位状态",
    "安全题目摘要",
    "关键题设参数",
    "问法类型",
    "用户错误入口",
    "复做第一动作",
    "核心考点",
    "模糊概念",
    "错因标签",
]

FIELD_LABELS = [
    "来源 ID",
    "年份",
    "科目",
    "主模块",
    "主知识点",
    "详情入口",
    "MarginNote 定位",
    "MarginNote 定位状态",
    "核心考点",
]

PROTECTED_LINE_MARKERS = [
    "标准答案",
    "正确答案",
    "错选",
    "选项字母",
    "完整题干",
    "完整解析",
    "图片",
    "手写痕迹",
]


def _asset_entry(details_root: Path, path: Path) -> dict[str, object]:
    root = details_root.resolve()
    resolved = path.resolve()
    return {
        "path": resolved.relative_to(root).as_posix(),
        "sha256": hashlib.sha256(resolved.read_bytes()).hexdigest(),
        "size_bytes": resolved.stat().st_size,
    }


def obsidian_url(vault: str, note: str) -> str:
    return "obsidian://open?" + urlencode({"vault": vault, "file": note}, quote_via=quote)


def parse_obsidian_url(value: str) -> tuple[str, str] | tuple[None, None]:
    match = re.search(r"obsidian://open\?[^|\s]+", value)
    if not match:
        return None, None
    parsed = urlparse(match.group(0))
    params = parse_qs(parsed.query)
    vault = params.get("vault", [None])[0]
    note = params.get("file", [None])[0]
    return vault, unquote(note) if note else note


def normalize_note_path(
    vault: str | None,
    note: str | None,
    repo: Path,
    details_root: Path,
    default_vault: str,
    details_vault: str,
) -> str | None:
    if not note or note.endswith(".md"):
        return note

    root: Path | None = None
    if vault == details_vault:
        root = details_root
    elif vault == default_vault:
        root = repo

    if not root:
        return note

    if (root / note).exists():
        return note

    markdown_note = f"{note}.md"
    if (root / markdown_note).exists():
        return markdown_note

    return note


def extract_field(text: str, label: str) -> str:
    pattern = re.compile(rf"^\s*-\s*{re.escape(label)}\s*[:：]\s*(.+?)\s*$", re.MULTILINE)
    match = pattern.search(text)
    return match.group(1).strip() if match else ""


def heading(text: str, fallback: str) -> str:
    for line in text.splitlines():
        stripped = line.strip()
        if stripped.startswith("# "):
            return stripped[2:].strip() or fallback
    return fallback


def safe_search_text(text: str) -> str:
    values: list[str] = []
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped.startswith("- "):
            continue
        if any(marker in stripped for marker in PROTECTED_LINE_MARKERS):
            continue
        for label in SAFE_SEARCH_LABELS:
            if stripped.startswith(f"- {label}：") or stripped.startswith(f"- {label}:"):
                values.append(stripped[2:])
                break
    return "\n".join(values)


def load_safe_cards(repo: Path) -> dict[str, dict]:
    records: dict[str, dict] = {}
    for card in sorted(repo.iterdir()):
        if not card.is_file() or not NODE_CARD_RE.match(card.name):
            continue
        node_id = card.stem
        text = card.read_text(encoding="utf-8", errors="ignore")
        fields = {label: extract_field(text, label) for label in FIELD_LABELS}
        records[node_id] = {
            "safe_card": card.relative_to(repo).as_posix(),
            "safe_title": heading(text, node_id),
            "safe_search_text": safe_search_text(text),
            "safe_fields": fields,
        }
    return records


def load_date_index(repo: Path) -> dict[str, dict]:
    path = repo / "年份索引.md"
    records: dict[str, dict] = {}
    if not path.exists():
        return records
    for line in path.read_text(encoding="utf-8", errors="ignore").splitlines():
        stripped = line.strip()
        if not stripped.startswith("|") or stripped.startswith("|---") or stripped.startswith("| ID "):
            continue
        parts = [part.strip() for part in stripped.strip("|").split("|")]
        if len(parts) < 8 or not NODE_ID_RE.match(parts[0]):
            continue
        detail_vault, detail_note = parse_obsidian_url(parts[7])
        records[parts[0]] = {
            "source_id": parts[1] or "未记录",
            "subject": parts[2] or "未记录",
            "main_module": parts[3] or "未记录",
            "main_knowledge": parts[4] or "未记录",
            "first_done_date": parts[5] or "未记录",
            "recent_redo_date": parts[6] or "未记录",
            "detail_entry": parts[7],
            "indexed_detail_vault": detail_vault,
            "indexed_detail_note": detail_note,
        }
    return records


def detail_cards(details_root: Path, details_vault: str) -> dict[str, dict]:
    cards_dir = details_root / "cards"
    assets_dir = details_root / "assets"
    records: dict[str, dict] = {}
    if not cards_dir.exists():
        return records
    for card in sorted(cards_dir.glob("*.md")):
        node_id = card.stem
        if not NODE_ID_RE.match(node_id):
            continue
        node_assets = assets_dir / node_id
        canonical_question_assets = {
            path.resolve()
            for path in node_assets.iterdir()
            if path.is_file()
            and not path.is_symlink()
            and path.stat().st_size
            and QUESTION_ASSET_RE.fullmatch(path.name)
        } if node_assets.exists() else set()
        canonical_solution_assets = {
            path.resolve()
            for path in node_assets.iterdir()
            if path.is_file()
            and not path.is_symlink()
            and path.stat().st_size
            and SOLUTION_ASSET_RE.fullmatch(path.name)
        } if node_assets.exists() else set()
        embedded_question_assets: set[Path] = set()
        embedded_solution_assets: set[Path] = set()
        card_text = card.read_text(encoding="utf-8", errors="ignore")
        for path in _embedded_detail_assets(details_root, card, card_text):
            if not path.stat().st_size:
                continue
            if not path.is_symlink() and QUESTION_ASSET_RE.fullmatch(path.name):
                embedded_question_assets.add(path.resolve())
            elif not path.is_symlink() and SOLUTION_ASSET_RE.fullmatch(path.name):
                embedded_solution_assets.add(path.resolve())
        referenced_question_count = len(embedded_question_assets)
        embedded_question_assets -= canonical_question_assets
        embedded_solution_assets -= canonical_solution_assets
        canonical_question_entries = [
            _asset_entry(details_root, path)
            for path in sorted(canonical_question_assets)
        ]
        canonical_solution_entries = [
            _asset_entry(details_root, path)
            for path in sorted(canonical_solution_assets)
        ]
        embedded_question_entries = [
            _asset_entry(details_root, path)
            for path in sorted(embedded_question_assets)
        ]
        embedded_solution_entries = [
            _asset_entry(details_root, path)
            for path in sorted(embedded_solution_assets)
        ]
        detail_note = card.relative_to(details_root).as_posix()
        attestation = node_assets / ANSWER_ISOLATION_ATTESTATION_FILENAME
        attestation_ref = ""
        attestation_sha256 = ""
        if (
            attestation.is_file()
            and not attestation.is_symlink()
            and attestation.stat().st_size > 0
        ):
            attestation_ref = attestation.relative_to(details_root).as_posix()
            attestation_sha256 = hashlib.sha256(attestation.read_bytes()).hexdigest()
        records[node_id] = {
            "detail_vault": details_vault,
            "detail_note": detail_note,
            "detail_card": detail_note,
            "detail_card_exists": True,
            "referenced_question_asset_count": referenced_question_count,
            "question_asset_count": len(canonical_question_entries),
            "solution_asset_count": len(canonical_solution_entries),
            "canonical_question_assets": canonical_question_entries,
            "canonical_solution_assets": canonical_solution_entries,
            "embedded_question_asset_count": len(embedded_question_entries),
            "embedded_solution_asset_count": len(embedded_solution_entries),
            "embedded_question_assets": embedded_question_entries,
            "embedded_solution_assets": embedded_solution_entries,
            "asset_dir": node_assets.relative_to(details_root).as_posix() if node_assets.exists() else "",
            "answer_isolation_attestation_ref": attestation_ref,
            "answer_isolation_attestation_sha256": attestation_sha256,
        }
    return records


def safe_field(safe: dict, label: str) -> str:
    return safe.get("safe_fields", {}).get(label, "")


def build_record(
    node_id: str,
    indexed: dict,
    detail: dict,
    safe: dict,
    repo: Path,
    details_root: Path,
    default_vault: str,
    details_vault: str,
) -> dict:
    source_id = indexed.get("source_id") or safe_field(safe, "来源 ID") or "未记录"
    subject = indexed.get("subject") or safe_field(safe, "科目") or node_id.split("_", 1)[0]
    main_module = indexed.get("main_module") or safe_field(safe, "主模块") or "未记录"
    main_knowledge = indexed.get("main_knowledge") or safe_field(safe, "主知识点") or "未记录"
    core_point = safe_field(safe, "核心考点") or "未记录"

    target_vault = details_vault
    target_note = f"cards/{node_id}.md"
    link_status = "detail_card_missing"
    if detail:
        target_vault = detail["detail_vault"]
        target_note = detail["detail_note"]
        has_assets = detail.get("question_asset_count", 0) or detail.get("solution_asset_count", 0)
        link_status = "detail_card_with_assets" if has_assets else "detail_card_without_assets"
    elif indexed.get("indexed_detail_vault") and indexed.get("indexed_detail_note"):
        target_vault = indexed["indexed_detail_vault"]
        target_note = normalize_note_path(
            target_vault,
            indexed["indexed_detail_note"],
            repo,
            details_root,
            default_vault,
            details_vault,
        )
        link_status = "indexed_detail_unverified"
    elif safe.get("safe_card"):
        target_vault = default_vault
        target_note = safe["safe_card"]
        link_status = "safe_node_card_fallback"

    search_bits = [
        node_id,
        source_id,
        subject,
        main_module,
        main_knowledge,
        core_point,
        indexed.get("detail_entry", ""),
        safe.get("safe_search_text", ""),
    ]
    return {
        "node_id": node_id,
        "formal_id": node_id,
        "wrongnet_id": node_id,
        "visual_id": source_id if source_id != "未记录" else node_id,
        "source_id": source_id,
        "year": node_id.split("_", 2)[1],
        "subject": subject,
        "main_module": main_module,
        "main_knowledge": main_knowledge,
        "core_point": core_point,
        "title": node_id,
        "target_vault": target_vault,
        "target_note": target_note,
        "detail_vault": detail.get("detail_vault") or indexed.get("indexed_detail_vault") or "",
        "detail_note": detail.get("detail_note")
        or normalize_note_path(
            indexed.get("indexed_detail_vault"),
            indexed.get("indexed_detail_note"),
            repo,
            details_root,
            default_vault,
            details_vault,
        )
        or "",
        "safe_vault": default_vault if safe.get("safe_card") else "",
        "safe_note": safe.get("safe_card", ""),
        "obsidian_url": obsidian_url(target_vault, target_note),
        "visual_ready": target_vault == details_vault and detail.get("referenced_question_asset_count", 0) > 0,
        "open_url": f"{BRIDGE_BASE}/open/{node_id}",
        "link_status": link_status,
        "question_asset_count": detail.get("question_asset_count", 0),
        "solution_asset_count": detail.get("solution_asset_count", 0),
        "canonical_question_assets": detail.get("canonical_question_assets", []),
        "canonical_solution_assets": detail.get("canonical_solution_assets", []),
        "embedded_question_asset_count": detail.get(
            "embedded_question_asset_count", 0
        ),
        "embedded_solution_asset_count": detail.get(
            "embedded_solution_asset_count", 0
        ),
        "embedded_question_assets": detail.get("embedded_question_assets", []),
        "embedded_solution_assets": detail.get("embedded_solution_assets", []),
        "asset_dir": detail.get("asset_dir", ""),
        "answer_isolation_attestation_ref": detail.get(
            "answer_isolation_attestation_ref", ""
        ),
        "answer_isolation_attestation_sha256": detail.get(
            "answer_isolation_attestation_sha256", ""
        ),
        "first_done_date": indexed.get("first_done_date") or "未记录",
        "recent_redo_date": indexed.get("recent_redo_date") or "未记录",
        "detail_entry": indexed.get("detail_entry") or safe_field(safe, "详情入口") or "未记录",
        "search_text": "\n".join(bit for bit in search_bits if bit),
    }


def build_snapshot(
    repo: Path,
    vault: str,
    details_root: Path = DETAILS_ROOT,
    details_vault: str = DEFAULT_DETAILS_VAULT,
) -> dict:
    repo = repo.resolve()
    details_root = details_root.resolve()
    safe_records = load_safe_cards(repo)
    indexed_records = load_date_index(repo)
    detail_records = detail_cards(details_root, details_vault)
    all_ids = sorted(set(safe_records) | set(indexed_records) | set(detail_records))
    records = [
        build_record(
            node_id,
            indexed_records.get(node_id, {}),
            detail_records.get(node_id, {}),
            safe_records.get(node_id, {}),
            repo,
            details_root,
            vault,
            details_vault,
        )
        for node_id in all_ids
    ]
    detail_count = sum(1 for record in records if record["link_status"].startswith("detail_card"))
    fallback_count = sum(1 for record in records if record["link_status"] == "safe_node_card_fallback")
    return {
        "system": "kaoyan-408",
        "purpose": "codex-clickable-obsidian-open-links",
        "default_vault": vault,
        "details_vault": details_vault,
        "project_root": str(repo),
        "details_root": str(details_root),
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "record_count": len(records),
        "detail_record_count": detail_count,
        "safe_fallback_count": fallback_count,
        "records": records,
    }


def write_snapshot(out: Path, snapshot: dict) -> None:
    out = out.expanduser()
    out.parent.mkdir(parents=True, exist_ok=True)
    payload = json.dumps(snapshot, ensure_ascii=False, indent=2) + "\n"
    fd, tmp_name = tempfile.mkstemp(prefix=f".{out.name}.", suffix=".tmp", dir=out.parent)
    tmp = Path(tmp_name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(payload)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, out)
        dir_fd = os.open(out.parent, os.O_RDONLY)
        try:
            os.fsync(dir_fd)
        finally:
            os.close(dir_fd)
    finally:
        tmp.unlink(missing_ok=True)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo", default=str(PROJECT_ROOT), help="408 repo root")
    parser.add_argument("--vault", default=DEFAULT_VAULT, help="408 safe-card Obsidian vault name")
    parser.add_argument("--details-root", default=str(DETAILS_ROOT), help="408 detail vault path")
    parser.add_argument("--details-vault", default=DEFAULT_DETAILS_VAULT, help="408 detail Obsidian vault name")
    parser.add_argument("--out", default=str(DEFAULT_SNAPSHOT), help="snapshot JSON path")
    parser.add_argument("--print", action="store_true", help="print snapshot instead of writing")
    args = parser.parse_args()

    snapshot = build_snapshot(Path(args.repo), args.vault, Path(args.details_root), args.details_vault)
    if args.print:
        print(json.dumps(snapshot, ensure_ascii=False, indent=2))
        return 0

    out = Path(args.out).expanduser()
    write_snapshot(out, snapshot)
    print(
        f"Wrote {snapshot['record_count']} records "
        f"({snapshot['detail_record_count']} detail cards, {snapshot['safe_fallback_count']} safe fallbacks) "
        f"to {out}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
