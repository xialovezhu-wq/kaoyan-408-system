#!/usr/bin/env python3
"""关系边结构清理与遗留候选审计。

判据（与 关系规则.md 对齐；2026-06-11 阶段3 扩展）：
- 删除 R09 跨科目边：R09 高频考点聚类按定义是同科目内聚类，跨科目相似应是稀疏 R07。
- 删除 R09 噪声边：两端主知识点不同章——只因过宽且未复核的专题链自动标签
  （见 历史错题归档/按专题链索引.md 头部声明）被连在一起，不构成同一高频考点。
  （2026-06-11 起 same_topic 只看主知识点章；主模块已是派生字段，不再参与判定，
  避免再被 DOCX 残留的失真主模块骗过。）
- 删除失效自动 R01：关联原因为"同一主知识点：…"但两端现主知识点已不同——
  节点重挂知识点后由阶段2遗留的满团残边。
- 删除冗余 R04：同一对若已有 R01（同一核心考点），其 R04（同一模糊概念）为冗余。
- 归一自动 R01 原因：两端同主知识点但原因里记录的是重挂前的旧知识点名，改写为现值。
- 遗留候选：同一主知识点却未建立任何关联边的题对，只报告、不默认补边。

重要：主知识点相同不等于题与题之间存在 R01 强关系。默认只读；只有显式
``--apply`` 才写结构清理结果。旧版“同主知识点补满团 / R09 自动升级 R01”必须
额外显式传入 ``--legacy-fill-same-kp``，仅用于复现历史行为，不得作为当前质量维护
流程。真正的语义关系必须逐题读取核心考点、问法、错误入口和详情证据后再写入。

写入时只动 关系边表.md；不删非重复的其它类型边；不改节点内容。
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

AUTO_R01_REASON = re.compile(r"^同一主知识点：")


def load_nodes(repo: Path) -> dict[str, dict[str, str]]:
    _, _, rows = read_table_with_preamble(repo / "节点总表.md")
    return {r.get("ID", ""): r for r in rows if r.get("ID")}


def subj(nid: str) -> str:
    return nid.split("_")[0]


def kp(nodes, nid: str) -> str:
    return nodes.get(nid, {}).get("主知识点", "").strip()


def kp_chap(nodes, nid: str):
    m = re.match(r"([A-Z]{2}\d{2})", kp(nodes, nid))
    return m.group(1) if m else None


def same_topic(nodes, a: str, b: str) -> bool:
    """同一高频考点聚类的最低要求：同科目且主知识点同章。"""
    if subj(a) != subj(b):
        return False
    ca, cb = kp_chap(nodes, a), kp_chap(nodes, b)
    return ca is not None and ca == cb


def pair(a: str, b: str) -> tuple[str, str]:
    return tuple(sorted([a, b]))


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--repo", default=".")
    parser.add_argument("--dry-run", action="store_true", help="兼容旧调用；默认本来就是只读")
    parser.add_argument("--apply", action="store_true", help="显式写入结构清理结果")
    parser.add_argument(
        "--legacy-fill-same-kp",
        action="store_true",
        help="危险的遗留行为：按同主知识点补满团并自动升级 R09；当前流程禁止使用",
    )
    parser.add_argument("--report", default="导入报告/关系边审核报告.md")
    args = parser.parse_args()
    repo = Path(args.repo).resolve()

    nodes = load_nodes(repo)
    preamble, headers, edges = read_table_with_preamble(repo / "关系边表.md")

    pair_types: dict[tuple[str, str], list[str]] = defaultdict(list)
    existing_pairs: set[tuple[str, str]] = set()
    for e in edges:
        p = pair(e.get("起点ID", ""), e.get("终点ID", ""))
        pair_types[p].append(e.get("关系类型", "").split()[0] if e.get("关系类型") else "")
        existing_pairs.add(p)

    rm_cross_subj, rm_noise, rm_dup_r04, rm_stale_r01 = [], [], [], []
    upgraded_r09, fixed_reasons = [], []
    kept: list[dict[str, str]] = []
    for e in edges:
        a, b, t = e.get("起点ID", ""), e.get("终点ID", ""), e.get("关系类型", "")
        reason = e.get("关联原因", "")
        same_kp = a in nodes and b in nodes and kp(nodes, a) == kp(nodes, b) and kp(nodes, a)
        if t.startswith("R01") and AUTO_R01_REASON.match(reason) and a in nodes and b in nodes:
            if not same_kp:
                rm_stale_r01.append(e)
                continue
            expected = f"同一主知识点：{kp(nodes, a)}。"
            if reason != expected:
                e["关联原因"] = expected
                fixed_reasons.append((a, b, reason, expected))
        if t.startswith("R09") and a in nodes and b in nodes:
            if subj(a) != subj(b):
                rm_cross_subj.append(e)
                continue
            if same_kp and args.legacy_fill_same_kp:
                e["关系类型"] = "R01 同一核心考点"
                e["联系强度"] = "强"
                e["关联原因"] = f"同一主知识点：{kp(nodes, a)}。"
                e["复盘优先级"] = "高"
                upgraded_r09.append(e)
                kept.append(e)
                continue
            if not same_topic(nodes, a, b):
                rm_noise.append(e)
                continue
        if t.startswith("R04"):
            if "R01" in pair_types[pair(a, b)]:
                rm_dup_r04.append(e)
                continue
        kept.append(e)

    # 重算现存对（删除后），再按主知识点补满团
    existing_pairs = {pair(e["起点ID"], e["终点ID"]) for e in kept}
    by_kp: dict[str, list[str]] = defaultdict(list)
    for nid in nodes:
        k = kp(nodes, nid)
        if k and k not in ("待补充", "未标明"):
            by_kp[k].append(nid)
    additions: list[dict[str, str]] = []
    add_log: list[tuple[str, str, str]] = []
    if args.legacy_fill_same_kp:
        for k, ids in by_kp.items():
            for a, b in combinations(sorted(ids), 2):
                if pair(a, b) in existing_pairs:
                    continue
                s, en = pair(a, b)
                row = {
                    "起点ID": s,
                    "终点ID": en,
                    "关系类型": "R01 同一核心考点",
                    "联系强度": "强",
                    "关联原因": f"同一主知识点：{k}。",
                    "复盘优先级": "高",
                }
                additions.append(row)
                add_log.append((s, en, k))
                existing_pairs.add(pair(a, b))

    if args.dry_run and args.apply:
        parser.error("--dry-run 与 --apply 不能同时使用")

    if not args.apply:
        mode = "遗留满团候选" if args.legacy_fill_same_kp else "安全结构清理候选"
        print(f"mode={mode}（只读；写入需显式 --apply）")
        print(f"删除 R09跨科目={len(rm_cross_subj)} R09噪声={len(rm_noise)} 冗余R04={len(rm_dup_r04)} 失效R01={len(rm_stale_r01)}")
        print(f"升级 同主知识点R09→R01={len(upgraded_r09)} 归一R01原因={len(fixed_reasons)}")
        print(f"新增 R01同主知识点={len(additions)}")
        print(f"边数 {len(edges)} -> {len(kept) + len(additions)}")
        if not args.legacy_fill_same_kp:
            print("NOTE: 同主知识点满团与 R09→R01 自动升级已禁用；请走逐题语义复核。")
        return 0

    new_edges = kept + additions
    write_table(repo / "关系边表.md", preamble, headers, new_edges)
    write_report(repo, args.report, nodes, rm_cross_subj, rm_noise, rm_dup_r04, rm_stale_r01,
                 upgraded_r09, fixed_reasons, add_log, len(edges), len(new_edges))

    print(f"removed_cross_subject={len(rm_cross_subj)}")
    print(f"removed_noise={len(rm_noise)}")
    print(f"removed_dup_r04={len(rm_dup_r04)}")
    print(f"removed_stale_r01={len(rm_stale_r01)}")
    print(f"upgraded_r09_to_r01={len(upgraded_r09)}")
    print(f"normalized_r01_reasons={len(fixed_reasons)}")
    print(f"added_r01={len(additions)}")
    print(f"edges {len(edges)} -> {len(new_edges)}")
    print(f"wrote={args.report}")
    return 0


def write_report(repo, report_rel, nodes, rm_cross_subj, rm_noise, rm_dup_r04, rm_stale_r01,
                 upgraded_r09, fixed_reasons, add_log, n_before, n_after) -> None:
    def line(nid):
        n = nodes.get(nid, {})
        return f"{n.get('主知识点','')[:20]} / {n.get('核心考点','')[:24]}"

    out = [
        "# 关系边审核报告（逐题关联链条复核）",
        "",
        f"- 生成日期：{date.today().isoformat()}",
        "- 范围：统一正式错题库全部关系边；只动 `关系边表.md`，不改节点内容。",
        "- 判据见 `scripts/curate_relation_edges.py` 顶部说明，与 `关系规则.md` 对齐。",
        f"- 边数变化：{n_before} -> {n_after}。",
        "",
        "## 一、删除：R09 跨科目（R09 按定义为同科目高频聚类）",
        "",
        f"合计 {len(rm_cross_subj)} 条。",
        "",
        "| 起点 | 终点 | 原因 |",
        "|---|---|---|",
    ]
    for e in rm_cross_subj:
        out.append(f"|{e['起点ID']}|{e['终点ID']}|{e.get('关联原因','')[:30]}|")
    out += [
        "",
        "## 二、删除：R09 噪声（两端主知识点不同章，仅靠过宽专题链标签相连）",
        "",
        f"合计 {len(rm_noise)} 条。专题链自动标签来自整理包建议、未经复核（见 `历史错题归档/按专题链索引.md` 头部）。",
        "",
        "| 起点（主知识点/核心考点） | 终点（主知识点/核心考点） |",
        "|---|---|",
    ]
    for e in rm_noise:
        out.append(f"|{e['起点ID']}：{line(e['起点ID'])}|{e['终点ID']}：{line(e['终点ID'])}|")
    out += [
        "",
        "## 三、删除：冗余 R04（同一对已有 R01）",
        "",
        f"合计 {len(rm_dup_r04)} 条。",
        "",
        "| 起点 | 终点 |",
        "|---|---|",
    ]
    for e in rm_dup_r04:
        out.append(f"|{e['起点ID']}|{e['终点ID']}|")
    out += [
        "",
        "## 四、删除：失效自动 R01（节点重挂后两端已不同主知识点）",
        "",
        f"合计 {len(rm_stale_r01)} 条。",
        "",
        "| 起点（现主知识点） | 终点（现主知识点） |",
        "|---|---|",
    ]
    for e in rm_stale_r01:
        out.append(f"|{e['起点ID']}：{kp(nodes, e['起点ID'])[:24]}|{e['终点ID']}：{kp(nodes, e['终点ID'])[:24]}|")
    out += [
        "",
        "## 五、升级：同主知识点 R09 → R01 强关联",
        "",
        f"合计 {len(upgraded_r09)} 条。",
        "",
        "| 起点 | 终点 | 主知识点 |",
        "|---|---|---|",
    ]
    for e in upgraded_r09:
        out.append(f"|{e['起点ID']}|{e['终点ID']}|{kp(nodes, e['起点ID'])}|")
    out += [
        "",
        "## 六、归一：自动 R01 关联原因改写为现主知识点",
        "",
        f"合计 {len(fixed_reasons)} 条。",
        "",
        "| 起点 | 终点 | 旧原因 | 新原因 |",
        "|---|---|---|---|",
    ]
    for a, b, old, new in fixed_reasons:
        out.append(f"|{a}|{b}|{old[:30]}|{new[:30]}|")
    out += [
        "",
        "## 七、新增：R01 同一核心考点（同主知识点却未连）",
        "",
        f"合计 {len(add_log)} 条，按主知识点分组。",
        "",
        "| 起点 | 终点 | 主知识点 |",
        "|---|---|---|",
    ]
    for s, en, k in add_log:
        out.append(f"|{s}|{en}|{k}|")
    (repo / report_rel).write_text("\n".join(out) + "\n", encoding="utf-8")


if __name__ == "__main__":
    raise SystemExit(main())
