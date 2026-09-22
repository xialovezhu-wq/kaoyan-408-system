#!/usr/bin/env python3
"""408 强关联旧题候选生成（只读，自动执行 7 天外硬筛选）。

数据源：节点总表 + 关系边表（+ 历史归档定位索引，仅当候选来源是历史 ID）。
默认候选信号只有经过正式关系表登记且理由具体的关系边。纯
``同一主知识点：...`` / ``同属专题链：...`` 模板边在完成逐题语义复核前不用于
主动推荐。

``--include-unreviewed-signals`` 仅用于人工审计召回：同主知识点、命中知识点交集、
模糊概念词面重叠和同错因都只标为“未审阅候选”，不冒充正式 R01/R04/R09。
``--kp`` 查询没有基准节点时也按未审阅知识点候选输出。

7 天外硬筛选先于强度排序执行：
  最近复做日期距 today < 7 天 → 一律排除并单独列出；
  日期未记录 → 保留为 7 天外候选，但排序低于日期明确者。

用法：
  python3 scripts/related_candidates_408.py --id CO_2014_002 [--repo .]
      [--today 2026-07-04] [--limit 5] [--include-unreviewed-signals]
      [--with-etag] [--json]
  python3 scripts/related_candidates_408.py --kp "CO03-04 DRAM" [...]

输出即 强关联旧题回顾 的字段素材；最近错误记录含答案模式时自动脱敏。
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from intake_lib_408 import (  # noqa: E402
    HIST_ID_RE, Repo, days_since, is_outside_7d, normalize_tag, read_text,
    scan_leak, split_multi, today_str,
)

BRIDGE = "http://127.0.0.1:8766/open/{fid}"
LOCATOR_FILE = "历史错题归档/定位索引.md"

STRENGTH_ORDER = {"强": 0, "中": 1, "弱": 2}
TEMPLATE_REASON_RE = re.compile(r"^(?:同一主知识点：|同属专题链：)")


def redact(text: str) -> str:
    fails, _ = scan_leak(text)
    if fails:
        return re.sub(r"(正确答案|标准答案|答案[是为选:：]\s*[A-DＡ-Ｄ]|应选\s*[A-DＡ-Ｄ]|故选\s*[A-DＡ-Ｄ]|正确选项[^；。]*)",
                      "答案信息已隐藏（复做保护）", text)
    return text


def fuzzy_tokens(s: str) -> set[str]:
    toks = set()
    placeholders = {"待补充", "未标明", "未记录", "暂无明确模糊概念", "暂无明确错因"}
    for part in split_multi(s):
        for piece in re.split(r"\s+vs\s+|与|、", part):
            piece = piece.strip()
            if len(piece) >= 3 and piece not in placeholders and not piece.startswith("待补"):
                toks.add(piece)
    return toks


def load_locators(repo: Repo) -> dict[str, dict]:
    """历史归档定位索引：仅当需要时调用。返回 {历史ID: {定位标记, 段落号, 来源位置}}。"""
    p = repo.path(LOCATOR_FILE)
    if not p.exists():
        return {}
    out = {}
    # 表结构：| 历史归档ID | 科目 | 年份 | 定位标记 | 段落号 | 核心考点 | 完整来源位置 |
    for line in read_text(p).splitlines():
        cells = [c.strip() for c in line.strip().strip("|").split("|")] if line.strip().startswith("|") else []
        if len(cells) >= 7 and HIST_ID_RE.match(cells[0]):
            out[cells[0]] = {"定位标记": cells[3] or "未提取",
                             "段落号": cells[4],
                             "来源位置": cells[6]}
    return out


def gather(repo: Repo, fid: str | None, kp: str | None, fuzzy: str | None,
           etag_on: bool, today: str, include_unreviewed: bool = False):
    master = repo.master()
    if fid:
        if fid not in master:
            print(f"[错误] {fid} 不在节点总表", file=sys.stderr)
            sys.exit(2)
        base = master[fid]
        kp = kp or base["主知识点"]
        fuzzy = fuzzy or base["模糊概念"]
        base_hits = {normalize_tag(h)[0] for h in split_multi(base["命中知识点"])}
        base_etags = set(split_multi(base["错因标签"]))
    else:
        base = None
        base_hits, base_etags = set(), set()
    kp_code = normalize_tag(kp or "")[0]
    base_fuzzy = fuzzy_tokens(fuzzy or "")

    # 信号 1：已有边
    edge_map: dict[str, dict] = {}
    if fid:
        for e in repo.edges():
            other = None
            if e["起点ID"] == fid:
                other = e["终点ID"]
            elif e["终点ID"] == fid:
                other = e["起点ID"]
            if other and other in master:
                edge_map[other] = e

    cands: dict[str, dict] = {}

    def add(nid: str, strength: str, why: str, rtype: str):
        if nid == fid or nid not in master:
            return
        cur = cands.get(nid)
        if cur is None or STRENGTH_ORDER[strength] < STRENGTH_ORDER[cur["strength"]]:
            cands[nid] = {"strength": strength, "why": why, "rtype": rtype}
        elif cur and STRENGTH_ORDER[strength] == STRENGTH_ORDER[cur["strength"]] and why not in cur["why"]:
            cur["why"] += f"；{why}"

    for nid, e in edge_map.items():
        reason = e["关联原因"].strip()
        if TEMPLATE_REASON_RE.match(reason):
            if include_unreviewed:
                add(nid, "中", f"待语义复核的模板边：{reason}", "候选未审阅（不等于正式关系）")
            continue
        add(nid, e["联系强度"], f"已有关系边：{reason}", e["关系类型"])

    if include_unreviewed or fid is None:
        for nid, row in master.items():
            if nid == fid:
                continue
            if kp_code and normalize_tag(row["主知识点"])[0] == kp_code:
                add(nid, "中", f"未审阅候选：同主知识点 {kp_code}", "候选未审阅（不等于 R01）")
            if base_hits:
                inter = base_hits & {normalize_tag(h)[0] for h in split_multi(row["命中知识点"])}
                inter.discard(kp_code)
                if inter:
                    add(nid, "弱", f"未审阅候选：命中知识点交集 {'、'.join(sorted(inter))}", "候选未审阅")
            if base_fuzzy:
                f_inter = base_fuzzy & fuzzy_tokens(row["模糊概念"])
                if f_inter:
                    add(nid, "弱", f"未审阅候选：模糊概念重叠 {'、'.join(sorted(f_inter)[:3])}", "候选未审阅")
            if etag_on and base_etags:
                e_inter = base_etags & set(split_multi(row["错因标签"]))
                e_inter = {t for t in e_inter if t != "暂无明确错因"}
                if e_inter:
                    add(nid, "弱", f"未审阅候选：同错因 {'、'.join(sorted(e_inter)[:2])}", "候选未审阅")

    # 7 天外硬筛选
    passed, filtered = [], []
    for nid, meta in cands.items():
        row = master[nid]
        last = row["最近复做日期"]
        n = days_since(last, today)
        item = {"id": nid, "row": row, **meta, "days": n}
        (passed if is_outside_7d(last, today) else filtered).append(item)

    passed.sort(key=lambda x: (STRENGTH_ORDER[x["strength"]],
                               0 if x["days"] is not None else 1,
                               -(x["days"] or 0)))
    return passed, filtered


def render_item(item: dict, locators: dict) -> str:
    row = item["row"]
    nid = row["ID"]
    sid = row["来源ID"]
    lines = [
        f"- 正式节点ID：{nid}",
        f"  Obsidian可点击打开：{BRIDGE.format(fid=nid)}",
        f"  来源ID：{sid}｜年份：{row['年份']}",
        f"  首次做题日期：{row['首次做题日期']}｜最近复做日期：{row['最近复做日期']}"
        + ("（未记录，按 7 天外候选处理）" if item["days"] is None else f"（{item['days']} 天前）"),
        f"  最近错误记录：{redact(row['最近错误记录'])[:120]}",
        f"  核心考点/旧题概况：{row['核心考点']}",
        f"  关系类型：{item['rtype']}｜联系强度：{item['strength']}",
        f"  推荐原因：{item['why']}",
    ]
    if HIST_ID_RE.match(sid):
        loc = locators.get(sid)
        if loc:
            lines.append(f"  历史归档定位：{sid}｜定位标记：{loc['定位标记']}｜段落号：{loc['段落号']}｜完整来源位置：{redact(loc['来源位置'])}")
        else:
            lines.append(f"  历史归档定位：{sid}｜定位索引未收录，需人工补")
    else:
        lines.append("  历史归档定位：无（非历史来源）")
    lines.append("  复做保护：未输出答案/选项字母；错误记录已按需脱敏")
    return "\n".join(lines)


def main():
    ap = argparse.ArgumentParser(description="408 强关联旧题候选（自动 7 天外硬筛选）")
    ap.add_argument("--id", help="以该正式节点为基准")
    ap.add_argument("--kp", help="或：直接给主知识点（编码 名称）")
    ap.add_argument("--fuzzy", help="可选：模糊概念串")
    ap.add_argument("--repo", default=".")
    ap.add_argument("--today", help="覆盖当前日期 YYYY-MM-DD")
    ap.add_argument("--limit", type=int, default=5)
    ap.add_argument("--with-etag", action="store_true", help="纳入同错因弱信号")
    ap.add_argument(
        "--include-unreviewed-signals",
        action="store_true",
        help="仅供人工审计召回字段相似题；输出不等于正式关系",
    )
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args()
    if not args.id and not args.kp:
        ap.error("需要 --id 或 --kp")

    repo = Repo(args.repo)
    today = today_str(args.today)
    passed, filtered = gather(
        repo,
        args.id,
        args.kp,
        args.fuzzy,
        args.with_etag,
        today,
        include_unreviewed=args.include_unreviewed_signals,
    )
    top = passed[: args.limit]

    need_locator = any(HIST_ID_RE.match(i["row"]["来源ID"]) for i in top)
    locators = load_locators(repo) if need_locator else {}

    if args.json:
        print(json.dumps({
            "today": today,
            "passed": [{k: v for k, v in i.items() if k != "row"} | {"来源ID": i["row"]["来源ID"]} for i in top],
            "filtered_within_7d": [{"id": i["id"], "days": i["days"], "strength": i["strength"]} for i in filtered],
        }, ensure_ascii=False, indent=2))
        return

    print(f"# 强关联旧题候选（today={today}，已执行 7 天外硬筛选）\n")
    if not top:
        print("当前无符合 7 天外硬规则的强/中关联旧题，不用近 7 天题凑数。")
        print("可改为输出知识点、概念或非题目复盘动作。\n")
    for item in top:
        print(render_item(item, locators))
        print()
    if filtered:
        print(f"## 被 7 天硬规则过滤（{len(filtered)} 道，禁止用于凑数）\n")
        for i in sorted(filtered, key=lambda x: x["days"] or 0):
            print(f"- {i['id']}（{i['strength']}关联，最近复做 {i['days']} 天前）")
    print(f"\n筛选统计：候选 {len(passed) + len(filtered)} → 7 天外合格 {len(passed)} → 输出 {len(top)}")


if __name__ == "__main__":
    main()
