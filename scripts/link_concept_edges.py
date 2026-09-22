#!/usr/bin/env python3
"""审计概念词典中的潜在「同一模糊概念」关系候选。

概念词典每个模糊概念已手工标注「相关节点」。本脚本把这些已判定、却尚无任何
关系边的节点对补成边：
- 同科目 -> R04 同一模糊概念（强 / 高）。
- 跨科目 -> R07 跨科目相似思想（弱 / 中），按 关系规则.md 的类型归属。

同列在一个宽泛概念下并不自动证明每一对题都值得建立关系边。默认只读，仅报告
候选。旧版全对物化必须同时显式传入 ``--apply`` 和
``--legacy-materialize-all-pairs``；该模式仅用于复现历史行为，不得用于当前逐题质量
维护。当前流程应逐题读取核心考点、问法、错误入口和详情证据后，再人工确认少量
高价值 R04/R07。

遗留写入模式只动 关系边表.md。
"""
from __future__ import annotations

import argparse
import re
import sys
from collections import defaultdict
from datetime import date
from itertools import combinations
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from normalize_408_knowledge_points import read_table_with_preamble, write_table

NODE_RE = re.compile(r"(?:DS|CO|OS|CN)_(?:UNK|\d{4})_\d+")


def parse_concepts(path: Path) -> list[tuple[str, list[str]]]:
    text = path.read_text(encoding="utf-8")
    out: list[tuple[str, list[str]]] = []
    for block in text.split("\n## "):
        lines = block.splitlines()
        if not lines:
            continue
        name = lines[0].strip().lstrip("# ").strip()
        if name in ("概念模板", "概念词典") or not name:
            continue
        m = re.search(r"相关节点[:：](.+)", block)
        if not m:
            continue
        ids = NODE_RE.findall(m.group(1))
        if len(set(ids)) >= 2:
            out.append((name, sorted(set(ids))))
    return out


def subj(nid: str) -> str:
    return nid.split("_")[0]


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--repo", default=".")
    parser.add_argument("--dry-run", action="store_true", help="兼容旧调用；默认本来就是只读")
    parser.add_argument("--apply", action="store_true", help="显式允许写入")
    parser.add_argument(
        "--legacy-materialize-all-pairs",
        action="store_true",
        help="危险的遗留行为：把同一概念下所有节点两两连边",
    )
    args = parser.parse_args()
    repo = Path(args.repo).resolve()

    _, _, node_rows = read_table_with_preamble(repo / "节点总表.md")
    node_ids = {r.get("ID", "") for r in node_rows if r.get("ID")}
    concepts = parse_concepts(repo / "概念词典.md")

    preamble, headers, edges = read_table_with_preamble(repo / "关系边表.md")
    existing = set(tuple(sorted([e.get("起点ID", ""), e.get("终点ID", "")])) for e in edges)

    # collect missing pairs -> concept names (a pair may sit under several concepts)
    pair_concepts: dict[tuple[str, str], list[str]] = defaultdict(list)
    for name, ids in concepts:
        ids = [i for i in ids if i in node_ids]
        for a, b in combinations(sorted(ids), 2):
            p = (a, b)
            if p in existing:
                continue
            pair_concepts[p].append(name)

    additions: list[dict[str, str]] = []
    log: list[tuple[str, str, str, str]] = []
    for (a, b), names in sorted(pair_concepts.items()):
        concept = "；".join(dict.fromkeys(names))
        if subj(a) == subj(b):
            rel, strength, prio = "R04 同一模糊概念", "强", "高"
        else:
            rel, strength, prio = "R07 跨科目相似思想", "弱", "中"
        additions.append({
            "起点ID": a, "终点ID": b, "关系类型": rel,
            "联系强度": strength, "关联原因": f"同一模糊概念：{concept}。", "复盘优先级": prio,
        })
        log.append((a, b, rel, concept))
        existing.add((a, b))

    r04 = sum(1 for r in additions if r["关系类型"].startswith("R04"))
    r07 = sum(1 for r in additions if r["关系类型"].startswith("R07"))
    if args.dry_run and args.apply:
        parser.error("--dry-run 与 --apply 不能同时使用")

    if not (args.apply and args.legacy_materialize_all_pairs):
        print("mode=概念关系候选审计（只读）")
        print(f"概念数(≥2节点)={len(concepts)}  待补边={len(additions)}  R04={r04}  R07(跨科目)={r07}")
        for a, b, rel, c in log[:20]:
            print(f"  {a} ↔ {b}  {rel.split()[0]}  「{c[:24]}」")
        print("NOTE: 全对物化已禁用；逐题确认后再维护少量可靠关系边。")
        return 0

    write_table(repo / "关系边表.md", preamble, headers, edges + additions)
    write_report(repo, log, r04, r07)
    print(f"added={len(additions)} (R04={r04}, R07跨科目={r07})")
    print(f"edges {len(edges)} -> {len(edges)+len(additions)}")
    print("wrote=导入报告/概念词典物化边报告.md")
    return 0


def write_report(repo: Path, log, r04: int, r07: int) -> None:
    out = [
        "# 概念词典「同一模糊概念」物化边报告（P1）",
        "",
        f"- 生成日期：{date.today().isoformat()}",
        f"- 来源：`概念词典.md` 各模糊概念的「相关节点」；只连用户已判定同概念、却尚无任何边的节点对。",
        f"- 同科目 → R04 同一模糊概念（强）{r04} 条；跨科目 → R07 跨科目相似思想（弱）{r07} 条。",
        "",
        "| 起点 | 终点 | 类型 | 模糊概念 |",
        "|---|---|---|---|",
    ]
    for a, b, rel, c in log:
        out.append(f"|{a}|{b}|{rel.split()[0]}|{c}|")
    (repo / "导入报告/概念词典物化边报告.md").write_text("\n".join(out) + "\n", encoding="utf-8")


if __name__ == "__main__":
    raise SystemExit(main())
