"""Resolve exact formal IDs to existing visual cards, never metadata fallbacks."""
from __future__ import annotations

import hashlib
from pathlib import Path
import re
from urllib.parse import quote, urlencode

REPO = Path(__file__).resolve().parents[1]
DETAILS = Path("/Users/your-user/Documents/kaoyan-408-details")
BRIDGE_PORT = 8769
BRIDGE_BASE = f"http://127.0.0.1:{BRIDGE_PORT}"
FORMAL = re.compile(r"(?:DS|CO|OS|CN)_(?:\d{4}|UNK)_\d{3}")
QUESTION = re.compile(r"question-(?:\d{2}|[0-9a-f]{16}|[0-9a-f]{64})\.(?:png|jpe?g|webp)", re.I)


def details_for_repo(repo: Path) -> Path:
    return DETAILS if Path(repo).resolve() == REPO else Path(repo) / "details"


def visual_link(repo: Path, formal_id: str, *, details_root: Path | None = None) -> dict:
    if not isinstance(formal_id, str) or not FORMAL.fullmatch(formal_id):
        raise ValueError("visual_link_formal_id_invalid")
    root = Path(details_root) if details_root is not None else details_for_repo(repo)
    relative = f"cards/{formal_id}.md"
    note = root / relative
    unavailable = {"link_status": "visual_card_missing", "obsidian_uri": None,
                   "open_url": None, "markdown_link": None}
    if not note.is_file() or note.is_symlink() or not note.resolve().is_relative_to(root.resolve()):
        return unavailable
    raw = note.read_bytes()
    from relation_semantic_review import _embedded_detail_assets
    images = [path for path in _embedded_detail_assets(root, note, raw.decode("utf-8"))
              if QUESTION.fullmatch(path.name) and path.is_file() and not path.is_symlink()
              and path.stat().st_size > 0 and path.resolve().is_relative_to(root.resolve())]
    if not images:
        return {**unavailable, "link_status": "visual_question_image_missing"}
    uri = "obsidian://open?" + urlencode({"vault": "kaoyan-408-details", "file": relative}, quote_via=quote)
    url = f"{BRIDGE_BASE}/open/{formal_id}"
    return {"link_status": "visual_card_ready", "obsidian_uri": uri, "open_url": url,
            "markdown_link": f"[{formal_id}（可视化卡）]({url})",
            "visual_note_path": str(note.resolve()), "visual_note_relpath": relative,
            "visual_note_sha256": hashlib.sha256(raw).hexdigest(),
            "visual_question_image_count": len(images)}
