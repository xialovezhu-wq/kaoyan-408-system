#!/usr/bin/env python3
"""Check top-level 408 safe node cards for blank or underspecified files."""

from __future__ import annotations

import argparse
import re
from pathlib import Path


NODE_RE = re.compile(r"^(DS|CO|OS|CN)_(?:\d{4}|UNK)_\d{3}\.md$")
REQUIRED_MARKERS = [
    "安全题目摘要",
    "关键题设参数",
    "问法类型",
    "用户错误入口",
    "复做第一动作",
    "核心考点",
    "模糊概念",
    "错因标签",
    "详情入口",
    "复做保护",
]


def parse_formal_ids(node_table: Path) -> set[str]:
    ids: set[str] = set()
    if not node_table.exists():
        return ids
    for line in node_table.read_text(encoding="utf-8").splitlines():
        if not line.startswith("|") or line.startswith("|ID|") or line.startswith("|---"):
            continue
        parts = [part.strip() for part in line.strip("|").split("|")]
        if parts:
            ids.add(parts[0])
    return ids


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo", default=".", help="kaoyan-408 repository root")
    parser.add_argument(
        "--list-missing",
        action="store_true",
        help="also list formal nodes that do not have a top-level card file",
    )
    args = parser.parse_args()

    root = Path(args.repo).resolve()
    formal_ids = parse_formal_ids(root / "节点总表.md")
    card_files = sorted(p for p in root.glob("*.md") if NODE_RE.match(p.name))
    card_ids = {p.stem for p in card_files}

    empty_cards: list[Path] = []
    incomplete_cards: list[tuple[Path, list[str]]] = []
    orphan_cards: list[Path] = []

    for path in card_files:
        text = path.read_text(encoding="utf-8")
        if not text.strip():
            empty_cards.append(path)
            continue
        missing = [marker for marker in REQUIRED_MARKERS if marker not in text]
        if missing:
            incomplete_cards.append((path, missing))
        if formal_ids and path.stem not in formal_ids:
            orphan_cards.append(path)

    print(f"safe_node_cards={len(card_files)}")
    print(f"empty_cards={len(empty_cards)}")
    for path in empty_cards:
        print(f"EMPTY\t{path.relative_to(root)}")

    print(f"incomplete_cards={len(incomplete_cards)}")
    for path, missing in incomplete_cards:
        print(f"INCOMPLETE\t{path.relative_to(root)}\tmissing={','.join(missing)}")

    print(f"orphan_cards={len(orphan_cards)}")
    for path in orphan_cards:
        print(f"ORPHAN\t{path.relative_to(root)}")

    missing_cards = sorted(formal_ids - card_ids) if formal_ids else []
    print(f"missing_cards={len(missing_cards)}")
    if args.list_missing:
        for node_id in missing_cards:
            print(f"MISSING\t{node_id}.md")

    return 1 if empty_cards or incomplete_cards or orphan_cards or missing_cards else 0


if __name__ == "__main__":
    raise SystemExit(main())
