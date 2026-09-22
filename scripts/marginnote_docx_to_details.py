#!/usr/bin/env python3
"""Export MarginNote Word outliner content into an Obsidian detail vault.

The detail vault is intentionally separate from the formal 408 relationship
network. Full question images and solution text stay in the detail vault; the
formal repo receives only obsidian:// links when requested.
"""

from __future__ import annotations

import argparse
import csv
import re
import shutil
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import quote
from zipfile import ZipFile
from xml.etree import ElementTree as ET


NS = {
    "w": "http://schemas.openxmlformats.org/wordprocessingml/2006/main",
    "r": "http://schemas.openxmlformats.org/officeDocument/2006/relationships",
    "a": "http://schemas.openxmlformats.org/drawingml/2006/main",
}

ANSWER_TITLE_RE = re.compile(r"^(正确|答案|解析|错题回顾|第一次错|我的错误)")
QUESTION_TITLE_RE = re.compile(
    r"(?:[12][0-9]{3}年.*真题|[DCOSN]{2}_[0-9A-Z]{3,4}_[0-9A-Z]{3}|"
    r"H(?:DS|CO|OS|CN)_\d{4}|(?:\d+\.){1,2}\d+\s*[（(]\d+[）)]|"
    r"^\d+\.\d+(?:\.\d+)?(?:\s+\S.*)?$)"
)


@dataclass
class Paragraph:
    index: int
    text: str
    images: list[str] = field(default_factory=list)
    links: list[str] = field(default_factory=list)

    @property
    def is_title(self) -> bool:
        return self.text.strip().endswith(">>")

    @property
    def title(self) -> str:
        return self.text.strip().removesuffix(">>").strip()


@dataclass
class NodeInfo:
    node_id: str
    source_id: str = "待补充"
    year: str = "待补充"
    subject: str = "待补充"
    module: str = "待补充"
    main_knowledge: str = "待补充"
    core_point: str = "待补充"
    detail_entry: str = "待补充"
    locator: str = "未提取"
    paragraph_no: str = "待补充"
    source_position: str = "待补充"


@dataclass
class DetailExport:
    node: NodeInfo
    anchor_index: int
    question_title: str
    question_images: list[str]
    solution_images: list[str]
    solution_text: str
    margin_links: list[str]
    match_reason: str


@dataclass
class MatchTerm:
    text: str
    reason: str


def read_excluded_nodes(vault: Path) -> set[str]:
    path = vault / "indexes" / "excluded-nodes.csv"
    if not path.exists():
        return set()
    with path.open(encoding="utf-8", newline="") as fh:
        return {
            (row.get("node_id") or "").strip()
            for row in csv.DictReader(fh)
            if (row.get("node_id") or "").strip()
        }


def read_docx(docx_path: Path) -> tuple[list[Paragraph], dict[str, str]]:
    with ZipFile(docx_path) as zf:
        rel_root = ET.fromstring(zf.read("word/_rels/document.xml.rels"))
        rels = {rel.attrib["Id"]: rel.attrib.get("Target", "") for rel in rel_root}
        doc_root = ET.fromstring(zf.read("word/document.xml"))

    paragraphs: list[Paragraph] = []
    for idx, par in enumerate(doc_root.findall(".//w:p", NS)):
        text = "".join(t.text or "" for t in par.findall(".//w:t", NS)).strip()
        images = []
        for blip in par.findall(".//a:blip", NS):
            rid = blip.attrib.get(f"{{{NS['r']}}}embed")
            if rid and rels.get(rid):
                images.append(rels[rid])
        links = []
        for link in par.findall(".//w:hyperlink", NS):
            rid = link.attrib.get(f"{{{NS['r']}}}id")
            if rid and rels.get(rid):
                links.append(rels[rid])
        paragraphs.append(Paragraph(idx, text, images, links))
    return paragraphs, rels


def parse_table(path: Path) -> list[dict[str, str]]:
    rows: list[dict[str, str]] = []
    if not path.exists():
        return rows
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.startswith("|") or line.startswith("|---") or line.startswith("|ID|"):
            continue
        cells = [c.strip() for c in line.strip().strip("|").split("|")]
        if len(cells) < 16:
            continue
        rows.append(
            {
                "ID": cells[0],
                "来源ID": cells[1],
                "年份": cells[2],
                "科目": cells[3],
                "主模块": cells[4],
                "主知识点": cells[5],
                "核心考点": cells[9],
                "详情入口": cells[15],
            }
        )
    return rows


def load_nodes(repo: Path) -> dict[str, NodeInfo]:
    nodes = {}
    for row in parse_table(repo / "节点总表.md"):
        nodes[row["ID"]] = NodeInfo(
            node_id=row["ID"],
            source_id=row["来源ID"],
            year=row["年份"],
            subject=row["科目"],
            module=row["主模块"],
            main_knowledge=row["主知识点"],
            core_point=row["核心考点"],
            detail_entry=row["详情入口"],
        )
    add_locator_info(repo, nodes)
    return nodes


def add_locator_info(repo: Path, nodes: dict[str, NodeInfo]) -> None:
    mapping_path = repo / "历史错题归档" / "正式ID映射.md"
    locator_path = repo / "历史错题归档" / "定位索引.md"
    source_to_formal: dict[str, str] = {}
    if mapping_path.exists():
        for line in mapping_path.read_text(encoding="utf-8").splitlines():
            if not line.startswith("|") or line.startswith("|---") or "历史归档ID" in line:
                continue
            cells = [c.strip() for c in line.strip().strip("|").split("|")]
            if len(cells) >= 2:
                source_to_formal[cells[0]] = cells[1]
    if not locator_path.exists():
        return
    for line in locator_path.read_text(encoding="utf-8").splitlines():
        if not line.startswith("|") or line.startswith("|---") or "历史归档ID" in line:
            continue
        cells = [c.strip() for c in line.strip().strip("|").split("|")]
        if len(cells) < 7:
            continue
        source_id = cells[0]
        formal_id = source_to_formal.get(source_id)
        if not formal_id or formal_id not in nodes:
            continue
        nodes[formal_id].locator = cells[3] or "未提取"
        nodes[formal_id].paragraph_no = cells[4] or "待补充"
        nodes[formal_id].source_position = cells[6] or "待补充"


def title_starts_question(par: Paragraph) -> bool:
    title = par.title
    if not par.is_title or not title:
        return False
    if ANSWER_TITLE_RE.search(title):
        return False
    return bool(QUESTION_TITLE_RE.search(title))


def find_previous_question_title(paragraphs: list[Paragraph], index: int) -> int | None:
    for i in range(index, -1, -1):
        if title_starts_question(paragraphs[i]):
            return i
    return None


def search_terms(node: NodeInfo, allow_chunk_match: bool = False) -> list[MatchTerm]:
    terms = [MatchTerm(node.node_id, "正式ID")]
    if node.source_id and node.source_id not in {"VISUAL_PENDING", "待补充"}:
        terms.append(MatchTerm(node.source_id, "来源ID"))
    if node.locator and node.locator not in {"未提取", "待补充"}:
        terms.append(MatchTerm(node.locator, "定位标记"))
    if node.core_point and node.core_point != "待补充":
        terms.append(MatchTerm(node.core_point, "完整核心考点"))
        if allow_chunk_match:
            for chunk in re.split(r"[、，,；;：:\s]+", node.core_point):
                if len(chunk) >= 8:
                    terms.append(MatchTerm(chunk, "核心考点片段"))
    seen: set[str] = set()
    unique: list[MatchTerm] = []
    for term in terms:
        if term.text and term.text not in seen:
            unique.append(term)
            seen.add(term.text)
    return unique


def find_anchor(
    paragraphs: list[Paragraph], node: NodeInfo, allow_chunk_match: bool = False
) -> tuple[int | None, str]:
    for term in search_terms(node, allow_chunk_match=allow_chunk_match):
        for par in paragraphs:
            if term.text in par.text:
                if title_starts_question(par):
                    return par.index, f"{term.reason}标题匹配：{term.text}"
                prev = find_previous_question_title(paragraphs, par.index)
                if prev is not None:
                    return prev, f"{term.reason}正文匹配：{term.text}"
                return par.index, f"{term.reason}段落匹配：{term.text}"
    return None, "未匹配"


def collect_detail(
    paragraphs: list[Paragraph], node: NodeInfo, allow_chunk_match: bool = False
) -> DetailExport | None:
    anchor, reason = find_anchor(paragraphs, node, allow_chunk_match=allow_chunk_match)
    if anchor is None:
        return None

    question_images: list[str] = []
    solution_images: list[str] = []
    solution_text_parts: list[str] = []
    margin_links: list[str] = []
    in_solution = False

    i = anchor + 1
    while i < len(paragraphs):
        par = paragraphs[i]
        if i > anchor + 1 and title_starts_question(par):
            break
        if par.links:
            margin_links.extend(par.links)
        title = par.title if par.is_title else ""
        if par.is_title:
            in_solution = True
            if title:
                solution_text_parts.append(title)
            i += 1
            continue
        if not in_solution and par.text and not par.is_title:
            in_solution = True
        if par.images:
            if in_solution:
                solution_images.extend(par.images)
            else:
                question_images.extend(par.images)
        if in_solution and par.text and not par.is_title:
            solution_text_parts.append(par.text)
        i += 1

    return DetailExport(
        node=node,
        anchor_index=anchor,
        question_title=paragraphs[anchor].title or paragraphs[anchor].text,
        question_images=question_images,
        solution_images=solution_images,
        solution_text="\n\n".join(part for part in solution_text_parts if part).strip(),
        margin_links=list(dict.fromkeys(margin_links)),
        match_reason=reason,
    )


def safe_name_from_media(media_target: str, prefix: str, index: int) -> str:
    suffix = Path(media_target).suffix.lower() or ".png"
    return f"{prefix}-{index:02d}{suffix}"


def extract_media(zf: ZipFile, media_target: str, dest: Path) -> None:
    target = media_target.replace("\\", "/")
    zip_name = target if target.startswith("word/") else f"word/{target}"
    with zf.open(zip_name) as src, dest.open("wb") as out:
        shutil.copyfileobj(src, out)


def write_card(detail: DetailExport, docx_path: Path, vault: Path) -> str:
    node = detail.node
    asset_dir = vault / "assets" / node.node_id
    card_dir = vault / "cards"
    if asset_dir.exists():
        shutil.rmtree(asset_dir)
    asset_dir.mkdir(parents=True, exist_ok=True)
    card_dir.mkdir(parents=True, exist_ok=True)

    question_links: list[str] = []
    solution_links: list[str] = []
    with ZipFile(docx_path) as zf:
        for idx, media in enumerate(detail.question_images, 1):
            name = safe_name_from_media(media, "question", idx)
            extract_media(zf, media, asset_dir / name)
            question_links.append(f"![[assets/{node.node_id}/{name}]]")
        for idx, media in enumerate(detail.solution_images, 1):
            name = safe_name_from_media(media, "solution", idx)
            extract_media(zf, media, asset_dir / name)
            solution_links.append(f"![[assets/{node.node_id}/{name}]]")

    source_bits = [node.source_id, node.locator, node.paragraph_no]
    source_line = " / ".join(bit for bit in source_bits if bit and bit != "待补充")
    margin_links = "\n".join(f"- {link}" for link in detail.margin_links) or "- 未提取"
    if question_links:
        question_block = "\n> ".join(question_links)
    else:
        fallback_title = (detail.question_title or node.node_id).replace("\n", "\n> ")
        question_block = f"{fallback_title}\n>\n> 题目图片待补充"
    solution_image_block = "\n> ".join(solution_links) if solution_links else "解析图片待补充"
    solution_text = detail.solution_text or "解析文字待补充"
    solution_text = solution_text.replace("\n", "\n> ")

    content = f"""# {node.node_id}

来源定位：{source_line}

正式节点：{node.node_id}
来源 ID：{node.source_id}
科目：{node.subject}
主知识点：{node.main_knowledge}
核心考点：{node.core_point}
匹配方式：{detail.match_reason}

## MarginNote 链接

{margin_links}

> [!question] 题目
> {question_block}

> [!answer]- 解析
> {solution_image_block}
>
> {solution_text}
"""
    (card_dir / f"{node.node_id}.md").write_text(content, encoding="utf-8")
    return f"obsidian://open?vault=kaoyan-408-details&file={quote('cards/' + node.node_id, safe='')}"


def replace_detail_link(line: str, link: str) -> str:
    cells = line.rstrip("\n").split("|")
    if len(cells) < 4:
        return line
    # Leading and trailing table separators produce empty cells at both ends.
    # The detail/source cell is the last content cell in both node and year tables.
    detail_idx = len(cells) - 2
    detail = cells[detail_idx].strip()
    if "kaoyan-408-details" in detail:
        return line
    cells[detail_idx] = f"{detail}；详情库：{link}"
    return "|".join(cells) + "\n"


def update_formal_links(repo: Path, links: dict[str, str]) -> list[Path]:
    touched: list[Path] = []
    for rel in ["节点总表.md", "年份索引.md"]:
        path = repo / rel
        if not path.exists():
            continue
        changed = False
        out = []
        for line in path.read_text(encoding="utf-8").splitlines(keepends=True):
            updated = line
            for node_id, link in links.items():
                if line.startswith(f"|{node_id}|"):
                    updated = replace_detail_link(line, link)
                    break
            if updated != line:
                changed = True
            out.append(updated)
        if changed:
            path.write_text("".join(out), encoding="utf-8")
            touched.append(path)
    return touched


def write_indexes(
    vault: Path,
    exports: list[DetailExport],
    links: dict[str, str],
    missing: list[NodeInfo],
    docx_path: Path,
    total_targets: int,
) -> None:
    idx = vault / "indexes"
    idx.mkdir(parents=True, exist_ok=True)
    by_subject: dict[str, list[NodeInfo]] = {}
    for detail in exports:
        by_subject.setdefault(detail.node.subject, []).append(detail.node)

    lines = ["# 408 题目详情索引", ""]
    for subject in sorted(by_subject):
        lines += [f"## {subject}", ""]
        for node in sorted(by_subject[subject], key=lambda n: n.node_id):
            lines.append(f"- [[cards/{node.node_id}|{node.node_id}]]：{node.core_point}")
        lines.append("")
    (idx / "by-subject.md").write_text("\n".join(lines), encoding="utf-8")

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
            "- 状态：docx 自动匹配失败，待用 MarginNote 右侧搜索或 oo3 层级复核。",
            "",
        ]
    (idx / "missing-map.md").write_text("\n".join(missing_lines), encoding="utf-8")

    review = [
        detail
        for detail in exports
        if not detail.question_images or len(detail.solution_images) > 5
    ]
    review_lines = ["# 需人工复核的详情页", ""]
    if not review:
        review_lines.append("本轮没有自动发现需人工复核的详情页。")
    for detail in sorted(review, key=lambda d: d.node.node_id):
        flags = []
        if not detail.question_images:
            flags.append("缺题目图片")
        if len(detail.solution_images) > 5:
            flags.append(f"解析图片过多：{len(detail.solution_images)}")
        review_lines += [
            f"## {detail.node.node_id}",
            "",
            f"- 问题：{'；'.join(flags)}",
            f"- 来源 ID：{detail.node.source_id}",
            f"- 核心考点：{detail.node.core_point}",
            f"- 匹配方式：{detail.match_reason}",
            "- 状态：已生成详情页，但暂不建议写入正式入口；需用 MarginNote 右侧搜索或备份源复核。",
            "",
        ]
    (idx / "needs-review.md").write_text("\n".join(review_lines), encoding="utf-8")

    with (idx / "manifest.csv").open("w", encoding="utf-8", newline="") as fh:
        writer = csv.writer(fh)
        writer.writerow(["node_id", "subject", "source_id", "link", "question_images", "solution_images", "has_solution_text", "match_reason"])
        for detail in sorted(exports, key=lambda d: d.node.node_id):
            writer.writerow(
                [
                    detail.node.node_id,
                    detail.node.subject,
                    detail.node.source_id,
                    links[detail.node.node_id],
                    len(detail.question_images),
                    len(detail.solution_images),
                    "yes" if detail.solution_text else "no",
                    detail.match_reason,
                ]
            )

    report_lines = [
        "# MarginNote 4 到 Obsidian 详情库迁移报告",
        "",
        f"- 输入 docx：{docx_path}",
        f"- 目标节点数：{total_targets}",
        f"- 已生成详情页：{len(exports)}",
        f"- 未匹配：{len(missing)}",
        f"- 需人工复核：{len(review)}",
        f"- 题目图片数：{sum(len(d.question_images) for d in exports)}",
        f"- 解析图片数：{sum(len(d.solution_images) for d in exports)}",
        f"- 含解析文字页数：{sum(1 for d in exports if d.solution_text)}",
        f"- 可写正式入口页数：{sum(1 for d in exports if d.question_images and len(d.solution_images) <= 5)}",
        "",
        "## 说明",
        "",
        "完整题图、解析图和解析文字只保存在本详情库；正式 408 关系网只写入 `obsidian://open` 详情入口。",
        "",
        "## 未匹配",
        "",
    ]
    if missing:
        report_lines.extend(f"- {node.node_id}：{node.core_point}" for node in missing)
    else:
        report_lines.append("- 无")
    (idx / "migration-report.md").write_text("\n".join(report_lines) + "\n", encoding="utf-8")


def write_readme(vault: Path) -> None:
    readme = vault / "README.md"
    if readme.exists():
        return
    readme.write_text(
        """# kaoyan-408-details

这是 408 错题的 Obsidian 题目详情库，用于保存从 MarginNote 4 导出的题目图片、解析图片和解析文字。

- `cards/`：每道题一个详情页。
- `assets/`：按正式节点 ID 存放图片。
- `indexes/by-subject.md`：按科目浏览。
- `indexes/missing-map.md`：自动匹配失败的题。

解析默认使用折叠 callout，做题时先看题目图片，完成后再展开解析。
""",
        encoding="utf-8",
    )


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--docx", required=True, type=Path)
    parser.add_argument("--repo", default=Path.cwd(), type=Path)
    parser.add_argument("--vault", default=Path("/Users/your-user/Documents/kaoyan-408-details"), type=Path)
    target_group = parser.add_mutually_exclusive_group(required=True)
    target_group.add_argument("--targets", nargs="+")
    target_group.add_argument("--all", action="store_true")
    parser.add_argument("--allow-chunk-match", action="store_true")
    parser.add_argument("--update-formal-links", action="store_true")
    args = parser.parse_args()

    paragraphs, _ = read_docx(args.docx)
    nodes = load_nodes(args.repo)
    excluded = read_excluded_nodes(args.vault)
    if excluded:
        nodes = {node_id: node for node_id, node in nodes.items() if node_id not in excluded}
    exports: list[DetailExport] = []
    links: dict[str, str] = {}
    formal_links: dict[str, str] = {}
    missing: list[NodeInfo] = []

    args.vault.mkdir(parents=True, exist_ok=True)
    write_readme(args.vault)

    targets = sorted(nodes) if args.all else [target for target in args.targets if target not in excluded]
    for target in targets:
        node = nodes.get(target) or NodeInfo(node_id=target)
        detail = collect_detail(paragraphs, node, allow_chunk_match=args.allow_chunk_match)
        if detail is None:
            missing.append(node)
            continue
        link = write_card(detail, args.docx, args.vault)
        exports.append(detail)
        links[node.node_id] = link
        if detail.question_images and len(detail.solution_images) <= 5:
            formal_links[node.node_id] = link

    write_indexes(args.vault, exports, links, missing, args.docx, len(targets))
    touched = update_formal_links(args.repo, formal_links) if args.update_formal_links else []

    print(f"exported={len(exports)}")
    print(f"missing={len(missing)}")
    for detail in exports:
        print(
            f"OK\t{detail.node.node_id}\tqimg={len(detail.question_images)}\t"
            f"simg={len(detail.solution_images)}\ttext={'yes' if detail.solution_text else 'no'}\t"
            f"{detail.match_reason}"
        )
    for node in missing:
        print(f"MISSING\t{node.node_id}\t{node.core_point}")
    for path in touched:
        print(f"UPDATED\t{path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
