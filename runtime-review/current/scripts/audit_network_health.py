#!/usr/bin/env python3
"""408 错题关系网健康审计（2026-06-11 关系网重构 阶段6 固化）。

把本次重构的全部诊断项固化为例行体检，任何一项 FAIL 退出码非零：
 1. 边端点有效性：无悬空边、无自环、无同对同类型重复、无同对多边；
 2. R07/R08/R09 合规：跨科类比、同年边和高频聚类满足正式结构语义；
 3. R01 合规：无失效自动 R01（"同一主知识点：…"但两端主知识点已不同）；
 4. R04 冗余：同对已有 R01 的 R04 应为 0；
 5. 核心考点干净：无答案残渣/章节标题/题干残句等模式（含复做保护）；
 6. 主模块派生一致：标准 `XXnn 名称` 格式且与主知识点同章；
 7. 主知识点合法：均为叶子编码且存在于 知识点标签表.md；
 8. 度分布：未审阅孤立节点=0；明确标记 reviewed_no_reliable_edge 的节点允许无边；
 9. 镜像覆盖：节点库分科视图与总表分科 ID 集合完全一致；
    年份索引 / 知识点命中索引 / 错题日期索引 覆盖全部节点 ID；
10. 命中索引 exact reconciliation：主命中、全部命中和错因触发 pair
    与节点总表完全相等且无重复。

用法：python3 scripts/audit_network_health.py [--repo .] [--report 导入报告/xxx.md]
"""
from __future__ import annotations

import argparse
import re
import sys
from collections import Counter, defaultdict
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from normalize_408_knowledge_points import read_table_with_preamble, load_valid_labels

SUBJECT_VIEWS = {
    "DS": "节点库/数据结构节点.md",
    "CO": "节点库/计算机组成原理节点.md",
    "OS": "节点库/操作系统节点.md",
    "CN": "节点库/计算机网络节点.md",
}
ID_RE = r"(?:DS|CO|OS|CN)_(?:\d{4}|UNK)_\d{3}"
JUNK_CORE = re.compile(
    r"^(错题回顾|我的答案|我的错选|我的误选|我的选择|错选|错误答案|正确答案|正确思路|正确结论|正确知识点|关键知识点|知识点回顾|\+|🌟|考纲扩展|答案|解析)"
    r"|^第[一二三四五六七八九十\d]+章"
    r"|应为（\s*）"
    r"|^.{0,3}$"
)


def kp_code(kp: str):
    m = re.match(r"([A-Z]{2}\d{2})-\d{2}\s", kp.strip())
    return m.group(1) if m else None


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--repo", default=".")
    parser.add_argument("--report", default=None)
    args = parser.parse_args()
    repo = Path(args.repo).resolve()

    _, _, node_rows = read_table_with_preamble(repo / "节点总表.md")
    _, _, edges = read_table_with_preamble(repo / "关系边表.md")
    nodes = {r["ID"]: r for r in node_rows if r.get("ID")}
    valid_labels = load_valid_labels(repo)

    results: list[tuple[str, bool, str]] = []

    def check(name: str, ok: bool, detail: str = ""):
        results.append((name, ok, detail))

    # 1. 边端点
    dangling = [(e["起点ID"], e["终点ID"]) for e in edges if e["起点ID"] not in nodes or e["终点ID"] not in nodes]
    selfloop = [e for e in edges if e["起点ID"] == e["终点ID"]]
    pair_counter = Counter(tuple(sorted([e["起点ID"], e["终点ID"]])) for e in edges)
    multi = {p: c for p, c in pair_counter.items() if c > 1}
    check("悬空边=0", not dangling, f"{len(dangling)} {dangling[:3]}")
    check("自环=0", not selfloop, str(len(selfloop)))
    check("同对多边=0", not multi, f"{len(multi)} {list(multi)[:3]}")

    # 2. R09
    def kp(nid):
        return nodes.get(nid, {}).get("主知识点", "").strip()

    r9_cross_subj = [e for e in edges if e["关系类型"].startswith("R09") and e["起点ID"].split("_")[0] != e["终点ID"].split("_")[0]]
    r9_cross_chap = [e for e in edges if e["关系类型"].startswith("R09")
                     and kp_code(kp(e["起点ID"])) and kp_code(kp(e["终点ID"]))
                     and kp_code(kp(e["起点ID"])) != kp_code(kp(e["终点ID"]))]
    check("R09跨科目=0", not r9_cross_subj, str(len(r9_cross_subj)))
    check("R09跨章=0", not r9_cross_chap, f"{len(r9_cross_chap)}")
    r7_bad = [
        e for e in edges
        if e["关系类型"].startswith("R07")
        and (
            e["起点ID"].split("_")[0] == e["终点ID"].split("_")[0]
            or e.get("联系强度") != "弱"
        )
    ]
    r8_bad = [
        e for e in edges
        if e["关系类型"].startswith("R08")
        and (
            not re.fullmatch(r"\d{4}", nodes[e["起点ID"]].get("年份", ""))
            or nodes[e["起点ID"]].get("年份") != nodes[e["终点ID"]].get("年份")
            or nodes[e["起点ID"]].get("主模块") != nodes[e["终点ID"]].get("主模块")
            or e.get("联系强度") != "弱"
        )
    ]
    check("R07跨科且为弱关系", not r7_bad, f"{len(r7_bad)}")
    check("R08同年同模块且为弱关系", not r8_bad, f"{len(r8_bad)}")

    # 3. 失效自动 R01
    stale = [e for e in edges if e["关系类型"].startswith("R01") and e.get("关联原因", "").startswith("同一主知识点")
             and kp(e["起点ID"]) != kp(e["终点ID"])]
    check("失效自动R01=0", not stale, f"{len(stale)} {[(e['起点ID'], e['终点ID']) for e in stale[:3]]}")

    # 4. 冗余 R04
    pair_types = defaultdict(set)
    for e in edges:
        pair_types[tuple(sorted([e["起点ID"], e["终点ID"]]))].add(e["关系类型"].split()[0])
    dup_r04 = [p for p, ts in pair_types.items() if "R04" in ts and "R01" in ts]
    check("冗余R04=0", not dup_r04, str(len(dup_r04)))

    # 5. 核心考点
    dirty = [(r["ID"], r["核心考点"][:30]) for r in node_rows if JUNK_CORE.search(r.get("核心考点", "").strip())]
    check("脏核心考点=0", not dirty, f"{len(dirty)} {dirty[:5]}")

    # 6. 主模块
    bad_mod_fmt = [r["ID"] for r in node_rows if not re.match(r"^[A-Z]{2}\d{2}\s\S", r.get("主模块", "").strip())]
    mod_mismatch = [r["ID"] for r in node_rows
                    if kp_code(r.get("主知识点", "")) and r.get("主模块", "")[:4] != kp_code(r.get("主知识点", ""))]
    check("主模块标准格式", not bad_mod_fmt, f"{len(bad_mod_fmt)} {bad_mod_fmt[:5]}")
    check("主模块与主知识点同章", not mod_mismatch, f"{len(mod_mismatch)} {mod_mismatch[:5]}")

    # 7. 主知识点合法
    bad_kp = [(r["ID"], r.get("主知识点", "")[:24]) for r in node_rows if r.get("主知识点", "").strip() not in valid_labels]
    check("主知识点在标签表", not bad_kp, f"{len(bad_kp)} {bad_kp[:5]}")
    kp_load = Counter(r.get("主知识点", "") for r in node_rows)
    overload = [(k, v) for k, v in kp_load.most_common(5) if v > 10]
    check("无超载知识点(>10题)", not overload, str(overload))

    # 8. 度分布
    deg = Counter()
    for e in edges:
        deg[e["起点ID"]] += 1
        deg[e["终点ID"]] += 1
    relation_status: dict[str, str] = {}
    for node_id in nodes:
        card = repo / f"{node_id}.md"
        if not card.exists():
            continue
        status = re.search(
            r"^- 关系复核[：:]\s*(connected|reviewed_no_reliable_edge|needs_user)\b",
            card.read_text(encoding="utf-8", errors="ignore"),
            re.M,
        )
        if status:
            relation_status[node_id] = status.group(1)
    iso = [n for n in nodes if deg.get(n, 0) == 0]
    reviewed_iso = [n for n in iso if relation_status.get(n) == "reviewed_no_reliable_edge"]
    needs_user_iso = [n for n in iso if relation_status.get(n) == "needs_user"]
    unreviewed_iso = sorted(set(iso) - set(reviewed_iso) - set(needs_user_iso))
    status_mismatch = sorted(
        node_id for node_id in nodes
        if (
            relation_status.get(node_id) == "connected" and deg.get(node_id, 0) == 0
        )
        or (
            relation_status.get(node_id) in {"reviewed_no_reliable_edge", "needs_user"}
            and deg.get(node_id, 0) != 0
        )
        or relation_status.get(node_id) not in {
            "connected", "reviewed_no_reliable_edge", "needs_user"
        }
    )
    deg1 = [n for n in nodes if deg.get(n, 0) == 1]
    check("未审阅孤立节点=0", not unreviewed_iso,
          f"{len(unreviewed_iso)} {unreviewed_iso[:5]}；已审阅无可靠边 {len(reviewed_iso)}；待补证据/身份 {len(needs_user_iso)}")
    check("关系复核状态与边度数闭合", not status_mismatch,
          f"{len(status_mismatch)} {status_mismatch[:8]}")

    # 9. 镜像覆盖
    for sub, rel in SUBJECT_VIEWS.items():
        txt = (repo / rel).read_text(encoding="utf-8")
        view_ids = set(re.findall(rf"\b({sub}_(?:\d{{4}}|UNK)_\d{{3}})\b", txt))
        table_ids = {n for n in nodes if n.startswith(sub + "_")}
        check(f"节点库视图一致[{sub}]", view_ids == table_ids,
              f"缺{sorted(table_ids - view_ids)[:3]} 多{sorted(view_ids - table_ids)[:3]}")
    for rel in ["年份索引.md", "知识点命中索引.md", "错题日期索引.md"]:
        txt = (repo / rel).read_text(encoding="utf-8")
        ids = set(re.findall(rf"\b({ID_RE})\b", txt))
        missing = sorted(n for n in nodes if n not in ids)
        check(f"覆盖全部节点[{rel}]", not missing, f"缺{len(missing)} {missing[:5]}")

    # 10. 命中索引 exact reconciliation
    idx_txt = (repo / "知识点命中索引.md").read_text(encoding="utf-8")
    sections = re.split(r"\n### ", idx_txt)
    actual_main: Counter[tuple[str, str]] = Counter()
    actual_hit: Counter[tuple[str, str]] = Counter()
    actual_error: Counter[tuple[str, str]] = Counter()
    for sec in sections[1:]:
        title = sec.splitlines()[0].strip()
        for block_title, counter in (
            ("主知识点命中", actual_main),
            ("命中知识点", actual_hit),
            ("错因触发记录", actual_error),
        ):
            block = re.search(rf"#### {block_title}\n(.*?)(?=####|\Z)", sec, re.S)
            if block:
                for nid in re.findall(rf"-\s*({ID_RE})：", block.group(1)):
                    counter[(title, nid)] += 1
    expected_main: set[tuple[str, str]] = set()
    expected_all: set[tuple[str, str]] = set()
    for node_id, row in nodes.items():
        main = row.get("主知识点", "").strip()
        expected_main.add((main, node_id))
        hits = [part.strip() for part in re.split(r"[；;]", row.get("命中知识点", "")) if part.strip()]
        if main not in hits:
            hits.insert(0, main)
        expected_all.update((hit, node_id) for hit in hits)
    actual_all = set(actual_main) | set(actual_hit)
    duplicate_hits = [
        pair for counter in (actual_main, actual_hit, actual_error)
        for pair, count in counter.items() if count != 1
    ]
    overlap_hits = set(actual_main) & set(actual_hit)
    hit_exact = (
        set(actual_main) == expected_main
        and actual_all == expected_all
        and set(actual_error) == expected_all
        and not duplicate_hits
        and not overlap_hits
    )
    hit_detail = (
        f"主缺{len(expected_main - set(actual_main))}/多{len(set(actual_main) - expected_main)}；"
        f"命中缺{len(expected_all - actual_all)}/多{len(actual_all - expected_all)}；"
        f"错因缺{len(expected_all - set(actual_error))}/多{len(set(actual_error) - expected_all)}；"
        f"重复{len(duplicate_hits)}；主/普通重叠{len(overlap_hits)}"
    )
    check("命中索引与节点总表完全一致", hit_exact, hit_detail)

    # 输出
    etype = Counter(e["关系类型"].split()[0] for e in edges)
    print(f"节点 {len(nodes)} | 边 {len(edges)} | 类型 {dict(etype.most_common())}")
    print(f"度1节点 {len(deg1)}（仅报告）: {sorted(deg1)}")
    failed = [r for r in results if not r[1]]
    for name, ok, detail in results:
        print(f"  [{'PASS' if ok else 'FAIL'}] {name}" + (f"  -> {detail}" if not ok else ""))
    print(f"\n{'全部通过 ✅' if not failed else f'失败 {len(failed)} 项 ❌'}")

    if args.report:
        lines = [
            "# 408 关系网健康审计报告",
            "",
            f"- 生成日期：{date.today().isoformat()}",
            f"- 节点 {len(nodes)}；边 {len(edges)}；类型分布：{dict(etype.most_common())}",
            f"- 度1节点（仅报告）：{len(deg1)}",
            "",
            "| 检查项 | 结果 | 说明 |",
            "|---|---|---|",
        ]
        for name, ok, detail in results:
            lines.append(f"|{name}|{'PASS' if ok else 'FAIL'}|{detail if not ok else ''}|")
        (repo / args.report).write_text("\n".join(lines) + "\n", encoding="utf-8")
        print(f"wrote={args.report}")

    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
