#!/usr/bin/env python3
"""Wire manually added detail-vault images into cards and formal links.

Use this after manually placing images under:

    /Users/your-user/Documents/kaoyan-408-details/assets/NODE_ID/question-01.png
    /Users/your-user/Documents/kaoyan-408-details/assets/NODE_ID/solution-01.png

The full images remain in the separate detail vault. The formal 408 repo only
gets obsidian:// links for cards that now have at least one question image.
"""

from __future__ import annotations

import argparse
import csv
from pathlib import Path
from urllib.parse import quote

from marginnote_docx_to_details import (
    NodeInfo,
    load_nodes,
    read_excluded_nodes,
    update_formal_links,
    write_readme,
)


IMAGE_SUFFIXES = {".png", ".jpg", ".jpeg", ".webp", ".gif"}


def image_files(asset_dir: Path, prefix: str) -> list[Path]:
    if not asset_dir.exists():
        return []
    return sorted(
        path
        for path in asset_dir.iterdir()
        if path.is_file()
        and path.suffix.lower() in IMAGE_SUFFIXES
        and path.name.startswith(prefix)
    )


def obsidian_image_lines(node_id: str, files: list[Path]) -> list[str]:
    return [f"> ![[assets/{node_id}/{path.name}]]" for path in files]


def replace_callout_block(text: str, marker: str, new_lines: list[str]) -> str:
    lines = text.splitlines()
    try:
        start = next(i for i, line in enumerate(lines) if line.strip() == marker)
    except StopIteration:
        return text.rstrip() + "\n\n" + marker + "\n" + "\n".join(new_lines) + "\n"

    end = start + 1
    while end < len(lines):
        line = lines[end]
        if line.startswith("> [!") and end > start + 1:
            break
        if not line.strip() and end > start + 1:
            next_line = lines[end + 1] if end + 1 < len(lines) else ""
            if next_line.startswith("> [!"):
                break
        end += 1
    return "\n".join(lines[: start + 1] + new_lines + [""] + lines[end:]).rstrip() + "\n"


def inject_solution_images(text: str, node_id: str, solution: list[Path]) -> str:
    if not solution:
        return text
    image_lines = obsidian_image_lines(node_id, solution)
    lines = text.splitlines()
    placeholder = "> 解析图片待补充"
    if placeholder in lines:
        idx = lines.index(placeholder)
        return "\n".join(lines[:idx] + image_lines + lines[idx + 1 :]).rstrip() + "\n"
    try:
        answer_idx = next(i for i, line in enumerate(lines) if line.strip() == "> [!answer]- 解析")
    except StopIteration:
        return text.rstrip() + "\n\n> [!answer]- 解析\n" + "\n".join(image_lines) + "\n"
    return "\n".join(lines[: answer_idx + 1] + image_lines + lines[answer_idx + 1 :]).rstrip() + "\n"


def create_card(node: NodeInfo, question: list[Path], solution: list[Path], vault: Path) -> str:
    question_block = "\n".join(obsidian_image_lines(node.node_id, question))
    if solution:
        solution_block = "\n".join(obsidian_image_lines(node.node_id, solution))
    else:
        solution_block = "> 解析图片待补充"
    source_bits = [node.source_id, node.locator, node.paragraph_no]
    source_line = " / ".join(bit for bit in source_bits if bit and bit != "待补充")
    content = f"""# {node.node_id}

来源定位：{source_line}

正式节点：{node.node_id}
来源 ID：{node.source_id}
科目：{node.subject}
主知识点：{node.main_knowledge}
核心考点：{node.core_point}
匹配方式：手动补图同步

## MarginNote 链接

- 手动补图

> [!question] 题目
{question_block}

> [!answer]- 解析
{solution_block}
>
> 解析文字待补充
"""
    card = vault / "cards" / f"{node.node_id}.md"
    card.parent.mkdir(parents=True, exist_ok=True)
    card.write_text(content, encoding="utf-8")
    return f"obsidian://open?vault=kaoyan-408-details&file={quote('cards/' + node.node_id, safe='')}"


def sync_card(node: NodeInfo, question: list[Path], solution: list[Path], vault: Path) -> str:
    card = vault / "cards" / f"{node.node_id}.md"
    if not card.exists():
        return create_card(node, question, solution, vault)

    text = card.read_text(encoding="utf-8")
    text = replace_callout_block(
        text,
        "> [!question] 题目",
        obsidian_image_lines(node.node_id, question),
    )
    text = inject_solution_images(text, node.node_id, solution)
    card.write_text(text, encoding="utf-8")
    return f"obsidian://open?vault=kaoyan-408-details&file={quote('cards/' + node.node_id, safe='')}"


def read_manifest(path: Path) -> dict[str, dict[str, str]]:
    if not path.exists():
        return {}
    with path.open(encoding="utf-8") as fh:
        return {row["node_id"]: row for row in csv.DictReader(fh)}


def write_manifest(path: Path, rows: dict[str, dict[str, str]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
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
    with path.open("w", encoding="utf-8", newline="") as fh:
        writer = csv.DictWriter(fh, fieldnames=fields)
        writer.writeheader()
        for node_id in sorted(rows):
            writer.writerow(rows[node_id])


def write_missing_and_review(vault: Path, nodes: dict[str, NodeInfo], rows: dict[str, dict[str, str]]) -> None:
    missing = [node for node_id, node in nodes.items() if node_id not in rows]
    missing_lines = ["# 未匹配题目清单", ""]
    if not missing:
        missing_lines.append("本轮没有未匹配题目。")
    for node in sorted(missing, key=lambda n: n.node_id):
        missing_lines += [
            f"## {node.node_id}",
            "",
            f"- 来源 ID：{node.source_id}",
            f"- 核心考点：{node.core_point}",
            f"- 详情入口：{node.detail_entry}",
            "- 状态：尚未生成详情页，待用 MarginNote 右侧搜索或手动补图。",
            "",
        ]
    (vault / "indexes" / "missing-map.md").write_text("\n".join(missing_lines), encoding="utf-8")

    review = [
        row
        for row in rows.values()
        if int(row.get("question_images", "0") or 0) == 0
    ]
    review_lines = ["# 需人工复核的详情页", ""]
    if not review:
        review_lines.append("本轮没有自动发现需人工复核的详情页。")
    for row in sorted(review, key=lambda r: r["node_id"]):
        node = nodes.get(row["node_id"], NodeInfo(node_id=row["node_id"]))
        review_lines += [
            f"## {row['node_id']}",
            "",
            "- 问题：缺题目图片",
            f"- 来源 ID：{row.get('source_id') or node.source_id}",
            f"- 核心考点：{node.core_point}",
            f"- 匹配方式：{row.get('match_reason', '待补充')}",
            "- 状态：已生成详情页，但暂不建议写入正式入口；需用 MarginNote 右侧搜索或手动补题图。",
            "",
        ]
    (vault / "indexes" / "needs-review.md").write_text("\n".join(review_lines), encoding="utf-8")


def write_report(vault: Path, rows: dict[str, dict[str, str]], nodes: dict[str, NodeInfo]) -> None:
    generated = len(rows)
    missing = len(nodes) - generated
    review = sum(1 for r in rows.values() if int(r.get("question_images", "0") or 0) == 0)
    qimg_pages = sum(1 for r in rows.values() if int(r.get("question_images", "0") or 0) > 0)
    report = [
        "# MarginNote 4 到 Obsidian 详情库迁移报告",
        "",
        "- 来源：DOCX / OO3 / 手动补图同步",
        f"- 目标节点数：{len(nodes)}",
        f"- 已生成详情页：{generated}",
        f"- 未匹配：{missing}",
        f"- 需人工复核：{review}",
        f"- 含题目图片页数：{qimg_pages}",
        f"- 题目图片数：{sum(int(r.get('question_images', '0') or 0) for r in rows.values())}",
        f"- 解析图片数：{sum(int(r.get('solution_images', '0') or 0) for r in rows.values())}",
        f"- 含解析文字页数：{sum(1 for r in rows.values() if r.get('has_solution_text') == 'yes')}",
        f"- 可写正式入口页数：{qimg_pages}",
        "",
        "## 说明",
        "",
        "完整题图、解析图和解析文字只保存在本详情库；正式 408 关系网只写入 `obsidian://open` 详情入口。",
        "",
    ]
    (vault / "indexes" / "migration-report.md").write_text("\n".join(report) + "\n", encoding="utf-8")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--repo", default=Path.cwd(), type=Path)
    parser.add_argument("--vault", default=Path("/Users/your-user/Documents/kaoyan-408-details"), type=Path)
    parser.add_argument("--update-formal-links", action="store_true")
    parser.add_argument("--force", action="store_true", help="also resync nodes that already have question images in manifest")
    args = parser.parse_args()

    nodes = load_nodes(args.repo)
    args.vault.mkdir(parents=True, exist_ok=True)
    write_readme(args.vault)
    excluded = read_excluded_nodes(args.vault)
    if excluded:
        nodes = {node_id: node for node_id, node in nodes.items() if node_id not in excluded}

    manifest_path = args.vault / "indexes" / "manifest.csv"
    rows = read_manifest(manifest_path)
    if excluded:
        rows = {node_id: row for node_id, row in rows.items() if node_id not in excluded}
    formal_links: dict[str, str] = {}
    changed: list[str] = []

    for node_id, node in sorted(nodes.items()):
        asset_dir = args.vault / "assets" / node_id
        question = image_files(asset_dir, "question-")
        solution = image_files(asset_dir, "solution-")
        if not question:
            continue

        existing = rows.get(node_id, {})
        existing_qimg = int(existing.get("question_images", "0") or 0)
        if existing and existing_qimg > 0 and not args.force:
            continue

        link = sync_card(node, question, solution, args.vault)
        rows[node_id] = {
            "node_id": node_id,
            "subject": node.subject,
            "source_id": node.source_id,
            "link": existing.get("link") or link,
            "question_images": str(len(question)),
            "solution_images": str(len(solution)),
            "has_solution_text": existing.get("has_solution_text", "no"),
            "match_reason": existing.get("match_reason") or "手动补图同步",
        }
        formal_links[node_id] = rows[node_id]["link"]
        changed.append(node_id)

    write_manifest(manifest_path, rows)
    write_missing_and_review(args.vault, nodes, rows)
    write_report(args.vault, rows, nodes)
    touched = update_formal_links(args.repo, formal_links) if args.update_formal_links else []

    print(f"manual_synced={len(changed)}")
    for node_id in changed:
        print(f"SYNCED\t{node_id}")
    for path in touched:
        print(f"UPDATED\t{path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
