#!/usr/bin/env python3
"""Create placeholder detail-vault cards for formal nodes not yet exported.

This does not write formal 408 links. It only makes the independent detail
vault complete enough to show a clear "image pending" page for every formal
node while keeping full question/solution content out of the relationship net.
"""

from __future__ import annotations

import argparse
import csv
from pathlib import Path
from urllib.parse import quote

from marginnote_docx_to_details import NodeInfo, load_nodes, read_excluded_nodes, write_readme
from sync_manual_detail_assets import write_missing_and_review, write_report


def read_manifest(path: Path) -> dict[str, dict[str, str]]:
    if not path.exists():
        return {}
    with path.open(encoding="utf-8") as fh:
        return {row["node_id"]: row for row in csv.DictReader(fh)}


def write_manifest(path: Path, rows: dict[str, dict[str, str]]) -> None:
    fields = [
        "node_id",
        "subject",
        "source_id",
        "link",
        "question_images",
        "solution_images",
        "has_solution_text",
        "match_reason",
    ]
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="") as fh:
        writer = csv.DictWriter(fh, fieldnames=fields)
        writer.writeheader()
        for node_id in sorted(rows):
            writer.writerow(rows[node_id])


def source_line(node: NodeInfo) -> str:
    bits = [node.source_id, node.locator, node.paragraph_no]
    value = " / ".join(bit for bit in bits if bit and bit != "待补充")
    return value or "待补充"


def placeholder_card(node: NodeInfo) -> str:
    return f"""# {node.node_id}

来源定位：{source_line(node)}

正式节点：{node.node_id}
来源 ID：{node.source_id}
年份：{node.year}
科目：{node.subject}
主模块：{node.module}
主知识点：{node.main_knowledge}
核心考点：{node.core_point}
匹配方式：占位详情页；MarginNote 原题图待人工补充

## MarginNote 来源

- 当前状态：尚未自动定位到可安全绑定的原题图。
- 下一步：在 MarginNote 4 右侧搜索面板中人工确认题图后，放入本节点 assets 目录。

> [!question] 题目
> 题目图片待补充
>
> 补图路径：`assets/{node.node_id}/question-01.png`

> [!answer]- 解析
> 解析图片待补充
>
> 解析文字待补充
"""


def create_placeholder(node: NodeInfo, vault: Path) -> str:
    (vault / "cards").mkdir(parents=True, exist_ok=True)
    (vault / "assets" / node.node_id).mkdir(parents=True, exist_ok=True)
    card = vault / "cards" / f"{node.node_id}.md"
    if not card.exists():
        card.write_text(placeholder_card(node), encoding="utf-8")
    return f"obsidian://open?vault=kaoyan-408-details&file={quote('cards/' + node.node_id, safe='')}"


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--repo", default=Path.cwd(), type=Path)
    parser.add_argument("--vault", default=Path("/Users/your-user/Documents/kaoyan-408-details"), type=Path)
    parser.add_argument("node_ids", nargs="*", help="optional node IDs; default creates placeholders for all missing nodes")
    args = parser.parse_args()

    all_nodes = load_nodes(args.repo)
    write_readme(args.vault)
    rows = read_manifest(args.vault / "indexes" / "manifest.csv")
    excluded = read_excluded_nodes(args.vault)
    nodes = {node_id: node for node_id, node in all_nodes.items() if node_id not in excluded}
    rows = {node_id: row for node_id, row in rows.items() if node_id not in excluded}

    targets = args.node_ids or [node_id for node_id in sorted(nodes) if node_id not in rows]
    created: list[str] = []
    skipped: list[str] = []
    for node_id in targets:
        if node_id in excluded:
            skipped.append(node_id)
            continue
        if node_id not in nodes:
            raise SystemExit(f"unknown node_id: {node_id}")
        if node_id in rows:
            continue
        node = nodes[node_id]
        link = create_placeholder(node, args.vault)
        rows[node_id] = {
            "node_id": node_id,
            "subject": node.subject,
            "source_id": node.source_id,
            "link": link,
            "question_images": "0",
            "solution_images": "0",
            "has_solution_text": "no",
            "match_reason": "占位详情页；MarginNote 原题图待人工补充",
        }
        created.append(node_id)

    write_manifest(args.vault / "indexes" / "manifest.csv", rows)
    write_missing_and_review(args.vault, nodes, rows)
    write_report(args.vault, rows, nodes)

    print(f"placeholders_created={len(created)}")
    for node_id in created:
        print(f"PLACEHOLDER\t{node_id}")
    for node_id in skipped:
        print(f"EXCLUDED\t{node_id}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
