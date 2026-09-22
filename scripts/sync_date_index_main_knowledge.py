#!/usr/bin/env python3
"""Sync 错题日期索引.md 主知识点 from 节点总表.md."""

from __future__ import annotations

import argparse
from pathlib import Path


def split_table_row(line: str) -> list[str] | None:
    if not line.startswith("|"):
        return None
    return [cell.strip() for cell in line.strip().strip("|").split("|")]


def load_main_knowledge(node_table: Path) -> dict[str, str]:
    header: list[str] | None = None
    id_idx = main_idx = -1
    values: dict[str, str] = {}

    for line in node_table.read_text(encoding="utf-8").splitlines():
        cells = split_table_row(line)
        if not cells:
            continue
        if cells[0] == "ID":
            header = cells
            id_idx = header.index("ID")
            main_idx = header.index("主知识点")
            continue
        if header and len(cells) == len(header) and set(cells[0]) - {"-", ":"}:
            values[cells[id_idx]] = cells[main_idx]
    return values


def sync_date_index(repo: Path, apply: bool) -> int:
    node_table = repo / "节点总表.md"
    date_index = repo / "错题日期索引.md"
    main_by_id = load_main_knowledge(node_table)

    lines = date_index.read_text(encoding="utf-8").splitlines()
    header: list[str] | None = None
    id_idx = main_idx = -1
    mismatches: list[tuple[str, str, str]] = []
    new_lines: list[str] = []

    for line in lines:
        cells = split_table_row(line)
        if cells and cells[0] == "ID":
            header = cells
            id_idx = header.index("ID")
            main_idx = header.index("主知识点")
            new_lines.append(line)
            continue

        if (
            header
            and cells
            and len(cells) == len(header)
            and set(cells[0]) - {"-", ":"}
            and cells[id_idx] in main_by_id
        ):
            node_id = cells[id_idx]
            expected = main_by_id[node_id]
            current = cells[main_idx]
            if current != expected:
                mismatches.append((node_id, current, expected))
                cells[main_idx] = expected
                line = "|" + "|".join(cells) + "|"
        new_lines.append(line)

    for node_id, current, expected in mismatches:
        print(f"{node_id}\t{current}\t=>\t{expected}")
    print(f"mismatch_count={len(mismatches)}")

    if apply and mismatches:
        date_index.write_text("\n".join(new_lines) + "\n", encoding="utf-8")
    return 1 if mismatches and not apply else 0


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--repo", default=".", help="kaoyan-408 repository root")
    parser.add_argument("--apply", action="store_true", help="write changes")
    args = parser.parse_args()
    return sync_date_index(Path(args.repo), args.apply)


if __name__ == "__main__":
    raise SystemExit(main())
