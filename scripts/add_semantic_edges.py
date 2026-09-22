#!/usr/bin/env python3
"""语义边适度补强（2026-06-11 关系网重构 阶段4）。

背景：边重构后科内语义边（R02 题型/R03 错因/R05 易混对比/R06 上下游）极度稀缺
（OS 14 条、CN 2 条），网络呈"同考点团内强连、团间真空"；另有 9 个孤立节点、
30+ 个度 1 节点缺少锚定。

本脚本按人工逐条审定的清单补边（不批量生成）：
- 锚定孤立/低度节点：每题 1-3 条有真实理由的边；
- R05 易混对比：依据概念边界（连续 vs 离散、虚电路 vs 数据报、硬布线 vs 微程序等）；
- R06 上下游：依据知识体系同章前置关系（基本分页→请求分页、置换→抖动对策等）；
- R09 同章高频聚类：源题同节/相邻、同一高频小题群；
- R07 跨科目：仅 LINK01"按位切分"思想与 CO/OS 虚拟存储互证两处，保持稀疏。

规则对齐 AGENTS.md 第十一节：每条边写明具体关联原因与复盘优先级，禁止空话；
已存在任意类型边的题对跳过并在报告中说明（不制造同对多边）。
R06 方向约定：起点=前置/基础，终点=应用/下游。
"""
from __future__ import annotations

import argparse
import sys
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from normalize_408_knowledge_points import read_table_with_preamble, write_table

# (起点ID, 终点ID, 关系类型, 联系强度, 关联原因, 复盘优先级)
EDGES: list[tuple[str, str, str, str, str, str]] = []  # Public export: private migration input intentionally omitted.


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--repo", default=".")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()
    repo = Path(args.repo).resolve()

    _, _, node_rows = read_table_with_preamble(repo / "节点总表.md")
    nodes = {r["ID"]: r for r in node_rows}
    preamble, headers, edges = read_table_with_preamble(repo / "关系边表.md")
    existing = {tuple(sorted([e["起点ID"], e["终点ID"]])) for e in edges}

    bad = [(a, b) for a, b, *_ in EDGES if a not in nodes or b not in nodes]
    if bad:
        raise SystemExit(f"端点不存在：{bad}")

    added, skipped = [], []
    for a, b, t, s, reason, prio in EDGES:
        if tuple(sorted([a, b])) in existing:
            skipped.append((a, b, t))
            continue
        edges.append({"起点ID": a, "终点ID": b, "关系类型": t, "联系强度": s, "关联原因": reason, "复盘优先级": prio})
        existing.add(tuple(sorted([a, b])))
        added.append((a, b, t, s, reason))

    if args.dry_run:
        for a, b, t, *_ in added:
            print(f"+ {a} -- {b} {t}")
        for a, b, t in skipped:
            print(f"skip(已有边) {a} -- {b} {t}")
        print(f"planned_add={len(added)} skipped={len(skipped)}")
        return 0

    write_table(repo / "关系边表.md", preamble, headers, edges)

    report = [
        "# 2026-06-11 语义边补强记录（阶段4）",
        "",
        f"- 生成日期：{date.today().isoformat()}",
        "- 目标：锚定孤立/低度节点，补 R05 易混对比与 R06 上下游桥边，消除'团内强连、团间真空'。",
        "- 每条边均为人工审定，关联原因具体（AGENTS.md 第十一节）；已有边的题对跳过。",
        f"- 新增 {len(added)} 条；跳过 {len(skipped)} 条（题对已有边）。",
        "",
        "| 起点 | 终点 | 类型 | 强度 | 关联原因 |",
        "|---|---|---|---|---|",
    ]
    for a, b, t, s, reason in added:
        report.append(f"|{a}|{b}|{t}|{s}|{reason}|")
    if skipped:
        report += ["", "## 跳过（题对已有边）", ""]
        for a, b, t in skipped:
            report.append(f"- {a} -- {b}（拟加 {t}）")
    (repo / "导入报告/2026-06-11语义边补强记录.md").write_text("\n".join(report) + "\n", encoding="utf-8")

    print(f"added={len(added)} skipped={len(skipped)}")
    print("wrote=导入报告/2026-06-11语义边补强记录.md")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
