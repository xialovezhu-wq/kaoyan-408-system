#!/usr/bin/env python3
"""Supplement MarginNote detail-vault exports from an OmniOutliner .oo3 backup.

The .docx export is the primary source. This script only tries to improve gaps:
nodes that are missing from the detail vault, or detail pages that lack a
question image. Full question/solution content is written only to the separate
detail vault; the formal 408 repo receives only obsidian:// links.
"""

from __future__ import annotations

import argparse
import csv
import gzip
import re
import shutil
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import quote
from xml.etree import ElementTree as ET

from marginnote_docx_to_details import (
    NodeInfo,
    load_nodes,
    read_excluded_nodes,
    update_formal_links,
    write_readme,
)


OO_NS = {"oo": "http://www.omnigroup.com/namespace/OmniOutliner/v3"}
ANSWER_SPLIT_RE = re.compile(
    r"(?m)^.*(?:答案|正确解法|正确思路|解析|错因|我的错误|错误原因|知识点总结|最终结论|"
    r"选项判断|判断与理由|做题思路|快速做题|记忆口诀).*$"
)
QUESTION_END_RE = re.compile(
    r"(?m)^.*(?:知识点速记|做题思路|解释各个选项|选项判断|判断与理由|错因|我的错误|"
    r"知识点整理|核心条件|错误原因|正确分析|快速做题|记忆口诀|🧩 一|一、).*$"
)
ANSWER_LEAK_RE = re.compile(r"(?:正确答案|答案[:：]|错选|我的答案|选项选|[A-D][.．、].*[✅❌])")


@dataclass
class ManifestRow:
    node_id: str
    subject: str
    source_id: str
    link: str
    question_images: int
    solution_images: int
    has_solution_text: str
    match_reason: str


@dataclass
class OO3Item:
    item_id: str
    title: str
    note: str
    cells: list[str]
    parent: "OO3Item | None" = None
    children: list["OO3Item"] = field(default_factory=list)

    @property
    def own_text(self) -> str:
        return "\n".join(part for part in [self.title, self.note] if part).strip()

    @property
    def path(self) -> str:
        parts: list[str] = []
        cur: OO3Item | None = self
        while cur is not None:
            label = first_line(cur.title) or cur.item_id
            if label:
                parts.append(label)
            cur = cur.parent
        return " > ".join(reversed(parts))


@dataclass
class OO3Detail:
    node: NodeInfo
    item: OO3Item
    question_text: str
    solution_text: str
    question_cells: list[str]
    solution_cells: list[str]
    match_reason: str


@dataclass
class MatchTerm:
    text: str
    reason: str
    priority: int


def first_line(text: str) -> str:
    for line in text.splitlines():
        clean = line.strip()
        if clean:
            return clean
    return ""


def normalize(text: str) -> str:
    return (
        text.replace("（", "(")
        .replace("）", ")")
        .replace("：", ":")
        .replace("，", ",")
        .replace("　", "")
        .replace(" ", "")
        .replace("\t", "")
        .replace("\n", "")
        .lower()
    )


def text_from(container: ET.Element | None) -> str:
    if container is None:
        return ""
    lines: list[str] = []
    for par in container.findall(".//oo:p", OO_NS):
        text = "".join(lit.text or "" for lit in par.findall(".//oo:lit", OO_NS))
        lines.append(text)
    if not lines:
        return "".join(lit.text or "" for lit in container.findall(".//oo:lit", OO_NS)).strip()
    return "\n".join(lines).strip()


def cells_from(container: ET.Element | None) -> list[str]:
    if container is None:
        return []
    return [
        cell.attrib["refid"]
        for cell in container.findall(".//oo:cell", OO_NS)
        if cell.attrib.get("refid")
    ]


def read_contents_xml(oo3_dir: Path) -> ET.Element:
    raw = (oo3_dir / "contents.xml").read_bytes()
    if raw[:2] == b"\x1f\x8b":
        raw = gzip.decompress(raw)
    return ET.fromstring(raw)


def load_oo3(oo3_dir: Path) -> tuple[list[OO3Item], dict[str, Path]]:
    root = read_contents_xml(oo3_dir)
    attachments: dict[str, Path] = {}
    for attachment in root.findall(".//oo:attachment", OO_NS):
        refid = attachment.attrib.get("id")
        href = attachment.attrib.get("href")
        if refid and href:
            attachments[refid] = oo3_dir / href

    items: list[OO3Item] = []

    def build(el: ET.Element, parent: OO3Item | None) -> OO3Item:
        values = el.find("oo:values", OO_NS)
        note = el.find("oo:note", OO_NS)
        item = OO3Item(
            item_id=el.attrib.get("id", ""),
            title=text_from(values),
            note=text_from(note),
            cells=cells_from(values) + cells_from(note),
            parent=parent,
        )
        items.append(item)
        children = el.find("oo:children", OO_NS)
        if children is not None:
            item.children = [build(child, item) for child in children.findall("oo:item", OO_NS)]
        return item

    root_items = root.find("oo:root", OO_NS)
    if root_items is not None:
        for child in root_items.findall("oo:item", OO_NS):
            build(child, None)
    return items, attachments


def locator_terms(locator: str) -> list[str]:
    if not locator or locator in {"未提取", "待补充"}:
        return []
    parts = [part.strip() for part in re.split(r"[；;]", locator) if part.strip()]
    out: list[str] = []
    for part in parts:
        clean = re.sub(r"^定位标记[:：]\s*", "", part).strip()
        if clean and clean not in out:
            out.append(clean)
        compact = re.sub(r"\s+", "", clean)
        if compact and compact not in out:
            out.append(compact)
    return out


def terms_for(node: NodeInfo) -> list[MatchTerm]:
    terms = [MatchTerm(node.node_id, "正式ID", 100)]
    if node.source_id and node.source_id not in {"VISUAL_PENDING", "待补充"}:
        terms.append(MatchTerm(node.source_id, "来源ID", 95))
    for loc in locator_terms(node.locator):
        terms.append(MatchTerm(loc, "定位标记", 90))
    if node.core_point and node.core_point != "待补充":
        terms.append(MatchTerm(node.core_point, "完整核心考点", 50))
        for chunk in re.split(r"[、，,；;：:\s（）()]+", node.core_point):
            if len(chunk) >= 8:
                terms.append(MatchTerm(chunk, "核心考点片段", 30))

    seen: set[str] = set()
    unique: list[MatchTerm] = []
    for term in terms:
        key = normalize(term.text)
        if key and key not in seen:
            unique.append(term)
            seen.add(key)
    return unique


def is_question_like(item: OO3Item) -> bool:
    text = item.own_text
    return bool(re.search(r"(题目|问题|真题|统考|错题回顾|下列|设|若|假设)", text))


def select_base_item(item: OO3Item) -> OO3Item:
    if item.cells:
        return item
    if item.parent and item.parent.cells:
        return item.parent
    return item


def find_match(items: list[OO3Item], node: NodeInfo) -> tuple[OO3Item | None, str]:
    best: tuple[int, OO3Item, str] | None = None
    for term in terms_for(node):
        term_norm = normalize(term.text)
        for item in items:
            own_norm = normalize(item.own_text)
            if term_norm not in own_norm:
                continue
            base = select_base_item(item)
            score = term.priority
            if normalize(first_line(item.title)) == term_norm:
                score += 15
            if base.cells:
                score += 10
            if item is not base:
                score += 5
            if term.priority < 50 and not base.cells and not is_question_like(item):
                continue
            reason = f"OO3 {term.reason}{'子卡片' if item is not base else ''}匹配：{term.text}"
            if best is None or score > best[0]:
                best = (score, base, reason)
    if best is None:
        return None, "OO3 未匹配"
    return best[1], best[2]


def split_question_solution(text: str) -> tuple[str, str]:
    clean = text.strip()
    if not clean:
        return "", ""
    matches = [m for m in [ANSWER_SPLIT_RE.search(clean), QUESTION_END_RE.search(clean)] if m]
    match = min(matches, key=lambda m: m.start()) if matches else None
    if not match:
        question = clean
        solution = ""
    else:
        question = clean[: match.start()].strip()
        solution = clean[match.start() :].strip()

    question_lines = question.splitlines()
    leaked: list[str] = []
    while question_lines and ANSWER_LEAK_RE.search(question_lines[0]):
        leaked.append(question_lines.pop(0))
    question = "\n".join(question_lines).strip()
    if leaked:
        solution = "\n".join(leaked + ([""] if solution else []) + ([solution] if solution else [])).strip()
    return question, solution


def collect_detail(items: list[OO3Item], node: NodeInfo) -> OO3Detail | None:
    item, reason = find_match(items, node)
    if item is None:
        return None
    return detail_from_item(node, item, reason)


def detail_from_item(node: NodeInfo, item: OO3Item, reason: str) -> OO3Detail:

    question_text, own_solution = split_question_solution(item.own_text)
    child_texts: list[str] = []
    solution_cells: list[str] = []
    for child in item.children:
        if child.own_text:
            child_texts.append(child.own_text)
        solution_cells.extend(child.cells)
    solution_parts = [own_solution] if own_solution else []
    solution_parts.extend(child_texts)
    return OO3Detail(
        node=node,
        item=item,
        question_text=question_text or f"{node.core_point}\n\n题目文字待人工补充；请优先补题图。" or node.node_id,
        solution_text="\n\n---\n\n".join(part for part in solution_parts if part).strip(),
        question_cells=item.cells,
        solution_cells=solution_cells,
        match_reason=f"{reason}；路径：{item.path}",
    )


def copy_cells(cells: list[str], attachments: dict[str, Path], asset_dir: Path, prefix: str) -> list[str]:
    links: list[str] = []
    seen: set[str] = set()
    for refid in cells:
        if refid in seen:
            continue
        seen.add(refid)
        src = attachments.get(refid)
        if src is None or not src.exists():
            continue
        name = f"{prefix}-{len(links) + 1:02d}{src.suffix.lower() or '.png'}"
        shutil.copyfile(src, asset_dir / name)
        links.append(f"![[assets/{asset_dir.name}/{name}]]")
    return links


def existing_answer_body(card_path: Path) -> str:
    if not card_path.exists():
        return ""
    lines = card_path.read_text(encoding="utf-8").splitlines()
    collected: list[str] = []
    in_answer = False
    for line in lines:
        if line.startswith("> [!answer]-"):
            in_answer = True
            continue
        if in_answer:
            if line.startswith("> [!") or line.startswith("## "):
                break
            if line.startswith("> "):
                collected.append(line[2:])
            elif line == ">":
                collected.append("")
            else:
                collected.append(line)
    body = "\n".join(collected).strip()
    placeholders = {"解析图片待补充", "解析文字待补充"}
    useful = [line.strip() for line in body.splitlines() if line.strip() and line.strip() not in placeholders]
    return body if useful else ""


def write_card(
    detail: OO3Detail,
    attachments: dict[str, Path],
    vault: Path,
    preserved_answer: str = "",
    preserve_existing_assets: bool = False,
) -> str:
    node = detail.node
    asset_dir = vault / "assets" / node.node_id
    card_dir = vault / "cards"
    if asset_dir.exists() and not preserve_existing_assets:
        shutil.rmtree(asset_dir)
    asset_dir.mkdir(parents=True, exist_ok=True)
    card_dir.mkdir(parents=True, exist_ok=True)

    question_links = copy_cells(detail.question_cells, attachments, asset_dir, "question")
    solution_links = copy_cells(detail.solution_cells, attachments, asset_dir, "solution")

    source_bits = [node.source_id, node.locator, node.paragraph_no]
    source_line = " / ".join(bit for bit in source_bits if bit and bit != "待补充")
    if question_links:
        question_block = "\n> ".join(question_links)
    else:
        question_block = detail.question_text.replace("\n", "\n> ") + "\n>\n> 题目图片待补充"
    solution_image_block = "\n> ".join(solution_links) if solution_links else "解析图片待补充"
    solution_text = (detail.solution_text or preserved_answer or "解析文字待补充").replace("\n", "\n> ")

    content = f"""# {node.node_id}

来源定位：{source_line}

正式节点：{node.node_id}
来源 ID：{node.source_id}
科目：{node.subject}
主知识点：{node.main_knowledge}
核心考点：{node.core_point}
匹配方式：{detail.match_reason}

## MarginNote 来源

- OO3 item：{detail.item.item_id}
- OO3 路径：{detail.item.path}

> [!question] 题目
> {question_block}

> [!answer]- 解析
> {solution_image_block}
>
> {solution_text}
"""
    (card_dir / f"{node.node_id}.md").write_text(content, encoding="utf-8")
    return f"obsidian://open?vault=kaoyan-408-details&file={quote('cards/' + node.node_id, safe='')}"


def read_manifest(path: Path) -> dict[str, ManifestRow]:
    if not path.exists():
        return {}
    rows: dict[str, ManifestRow] = {}
    with path.open(encoding="utf-8", newline="") as fh:
        reader = csv.DictReader(fh)
        for row in reader:
            node_id = row.get("node_id", "").strip()
            if not node_id:
                continue
            rows[node_id] = ManifestRow(
                node_id=node_id,
                subject=row.get("subject", ""),
                source_id=row.get("source_id", ""),
                link=row.get("link", ""),
                question_images=int(row.get("question_images") or 0),
                solution_images=int(row.get("solution_images") or 0),
                has_solution_text=row.get("has_solution_text", "no"),
                match_reason=row.get("match_reason", ""),
            )
    return rows


def write_combined_indexes(
    vault: Path,
    nodes: dict[str, NodeInfo],
    manifest: dict[str, ManifestRow],
    oo3_path: Path,
    supplemented: list[str],
    unmatched: list[str],
    unimproved: list[str],
) -> None:
    idx = vault / "indexes"
    idx.mkdir(parents=True, exist_ok=True)

    by_subject: dict[str, list[NodeInfo]] = {}
    for node_id in sorted(manifest):
        node = nodes.get(node_id)
        if node:
            by_subject.setdefault(node.subject, []).append(node)

    lines = ["# 408 题目详情索引", ""]
    for subject in sorted(by_subject):
        lines += [f"## {subject}", ""]
        for node in sorted(by_subject[subject], key=lambda n: n.node_id):
            lines.append(f"- [[cards/{node.node_id}|{node.node_id}]]：{node.core_point}")
        lines.append("")
    (idx / "by-subject.md").write_text("\n".join(lines), encoding="utf-8")

    missing = [nodes[node_id] for node_id in sorted(nodes) if node_id not in manifest]
    missing_lines = ["# 未匹配题目清单", ""]
    if not missing:
        missing_lines.append("本轮没有未匹配题目。")
    for node in missing:
        missing_lines += [
            f"## {node.node_id}",
            "",
            f"- 来源 ID：{node.source_id}",
            f"- 核心考点：{node.core_point}",
            f"- 详情入口：{node.detail_entry}",
            "- 状态：DOCX 与 OO3 自动匹配失败，待用 MarginNote 右侧搜索人工复核。",
            "",
        ]
    (idx / "missing-map.md").write_text("\n".join(missing_lines), encoding="utf-8")

    review = [
        row
        for row in manifest.values()
        if row.question_images == 0 or row.solution_images > 5
    ]
    review_lines = ["# 需人工复核的详情页", ""]
    if not review:
        review_lines.append("本轮没有自动发现需人工复核的详情页。")
    for row in sorted(review, key=lambda r: r.node_id):
        node = nodes.get(row.node_id, NodeInfo(node_id=row.node_id))
        flags = []
        if row.question_images == 0:
            flags.append("缺题目图片")
        if row.solution_images > 5:
            flags.append(f"解析图片过多：{row.solution_images}")
        review_lines += [
            f"## {row.node_id}",
            "",
            f"- 问题：{'；'.join(flags)}",
            f"- 来源 ID：{node.source_id}",
            f"- 核心考点：{node.core_point}",
            f"- 匹配方式：{row.match_reason}",
            "- 状态：已生成详情页，但暂不建议写入正式入口；需用 MarginNote 右侧搜索或备份源复核。",
            "",
        ]
    (idx / "needs-review.md").write_text("\n".join(review_lines), encoding="utf-8")

    manifest_path = idx / "manifest.csv"
    with manifest_path.open("w", encoding="utf-8", newline="") as fh:
        writer = csv.writer(fh)
        writer.writerow(
            [
                "node_id",
                "subject",
                "source_id",
                "link",
                "question_images",
                "solution_images",
                "has_solution_text",
                "match_reason",
            ]
        )
        for row in sorted(manifest.values(), key=lambda r: r.node_id):
            writer.writerow(
                [
                    row.node_id,
                    row.subject,
                    row.source_id,
                    row.link,
                    row.question_images,
                    row.solution_images,
                    row.has_solution_text,
                    row.match_reason,
                ]
            )

    question_image_pages = sum(1 for row in manifest.values() if row.question_images > 0)
    report_lines = [
        "# MarginNote 4 到 Obsidian 详情库迁移报告",
        "",
        f"- OO3 补充来源：{oo3_path}",
        f"- 目标节点数：{len(nodes)}",
        f"- 已生成详情页：{len(manifest)}",
        f"- 未匹配：{len(missing)}",
        f"- 需人工复核：{len(review)}",
        f"- 含题目图片页数：{question_image_pages}",
        f"- 题目图片数：{sum(row.question_images for row in manifest.values())}",
        f"- 解析图片数：{sum(row.solution_images for row in manifest.values())}",
        f"- 含解析文字页数：{sum(1 for row in manifest.values() if row.has_solution_text == 'yes')}",
        f"- 可写正式入口页数：{question_image_pages}",
        f"- 本轮 OO3 新增/改善：{len(supplemented)}",
        "",
        "## 说明",
        "",
        "完整题图、解析图和解析文字只保存在本详情库；正式 408 关系网只写入 `obsidian://open` 详情入口。",
        "",
        "## 本轮 OO3 已补",
        "",
    ]
    report_lines.extend(f"- {node_id}" for node_id in supplemented) if supplemented else report_lines.append("- 无")
    report_lines += ["", "## 本轮 OO3 未改善", ""]
    report_lines.extend(f"- {node_id}" for node_id in unimproved) if unimproved else report_lines.append("- 无")
    report_lines += ["", "## 自动匹配仍失败", ""]
    report_lines.extend(f"- {node_id}" for node_id in unmatched) if unmatched else report_lines.append("- 无")
    report_lines += ["", "## 仍未匹配", ""]
    report_lines.extend(f"- {node.node_id}：{node.core_point}" for node in missing) if missing else report_lines.append("- 无")
    (idx / "migration-report.md").write_text("\n".join(report_lines) + "\n", encoding="utf-8")


def gap_targets(nodes: dict[str, NodeInfo], manifest: dict[str, ManifestRow]) -> list[str]:
    targets: list[str] = []
    for node_id in sorted(nodes):
        row = manifest.get(node_id)
        if row is None or row.question_images == 0 or row.solution_images > 5:
            targets.append(node_id)
    return targets


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--oo3", required=True, type=Path)
    parser.add_argument("--repo", default=Path.cwd(), type=Path)
    parser.add_argument("--vault", default=Path("/Users/your-user/Documents/kaoyan-408-details"), type=Path)
    parser.add_argument("--targets", nargs="+")
    parser.add_argument(
        "--item-map",
        nargs="*",
        default=[],
        metavar="NODE_ID=OO3_ITEM_ID",
        help="Import exact OO3 items for nodes that cannot be matched automatically.",
    )
    parser.add_argument("--all-gaps", action="store_true")
    parser.add_argument("--update-formal-links", action="store_true")
    args = parser.parse_args()

    nodes = load_nodes(args.repo)
    manifest_path = args.vault / "indexes" / "manifest.csv"
    manifest = read_manifest(manifest_path)
    excluded = read_excluded_nodes(args.vault)
    if excluded:
        nodes = {node_id: node for node_id, node in nodes.items() if node_id not in excluded}
        manifest = {node_id: row for node_id, row in manifest.items() if node_id not in excluded}
    exact_items: dict[str, str] = {}
    for pair in args.item_map:
        if "=" not in pair:
            raise SystemExit(f"Invalid --item-map value: {pair}")
        node_id, item_id = pair.split("=", 1)
        node_id = node_id.strip()
        if node_id not in excluded:
            exact_items[node_id] = item_id.strip()
    targets = sorted(args.targets or gap_targets(nodes, manifest) if args.all_gaps else args.targets or [])
    targets = sorted((set(targets) | set(exact_items)) - excluded)
    if not targets:
        raise SystemExit("No targets supplied. Use --all-gaps or --targets.")

    args.vault.mkdir(parents=True, exist_ok=True)
    write_readme(args.vault)
    items, attachments = load_oo3(args.oo3)
    items_by_id = {item.item_id: item for item in items}

    formal_links: dict[str, str] = {}
    supplemented: list[str] = []
    unmatched: list[str] = []
    unimproved: list[str] = []

    for node_id in targets:
        node = nodes.get(node_id) or NodeInfo(node_id=node_id)
        before = manifest.get(node_id)
        if node_id in exact_items:
            item = items_by_id.get(exact_items[node_id])
            if item is None:
                unmatched.append(node_id)
                continue
            detail = detail_from_item(node, item, f"OO3 精确卡片ID匹配：{item.item_id}；路径：{item.path}")
        else:
            detail = collect_detail(items, node)
        if detail is None:
            unmatched.append(node_id)
            continue
        exact_import = node_id in exact_items
        if before and before.question_images == 0 and not detail.question_cells and not exact_import:
            unimproved.append(node_id)
            continue
        if before and before.question_images > 0 and len(detail.solution_cells) > 5 and not exact_import:
            unimproved.append(node_id)
            continue

        preserved_answer = ""
        if before and not detail.solution_text and not detail.solution_cells:
            preserved_answer = existing_answer_body(args.vault / "cards" / f"{node.node_id}.md")
        link = write_card(
            detail,
            attachments,
            args.vault,
            preserved_answer=preserved_answer,
            preserve_existing_assets=bool(preserved_answer),
        )
        row = ManifestRow(
            node_id=node.node_id,
            subject=node.subject,
            source_id=node.source_id,
            link=link,
            question_images=len({ref for ref in detail.question_cells if ref in attachments}),
            solution_images=len({ref for ref in detail.solution_cells if ref in attachments}),
            has_solution_text="yes" if detail.solution_text or preserved_answer else "no",
            match_reason=detail.match_reason,
        )
        manifest[node.node_id] = row
        supplemented.append(node.node_id)
        if row.question_images > 0 and row.solution_images <= 5:
            formal_links[node.node_id] = link

    touched = update_formal_links(args.repo, formal_links) if args.update_formal_links else []
    write_combined_indexes(args.vault, nodes, manifest, args.oo3, supplemented, unmatched, unimproved)

    print(f"targets={len(targets)}")
    print(f"supplemented={len(supplemented)}")
    print(f"unmatched={len(unmatched)}")
    print(f"unimproved={len(unimproved)}")
    for node_id in supplemented:
        row = manifest[node_id]
        print(f"OK\t{node_id}\tqimg={row.question_images}\tsimg={row.solution_images}\ttext={row.has_solution_text}\t{row.match_reason}")
    for node_id in unmatched:
        print(f"MISSING\t{node_id}")
    for node_id in unimproved:
        print(f"UNIMPROVED\t{node_id}")
    for path in touched:
        print(f"UPDATED\t{path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
