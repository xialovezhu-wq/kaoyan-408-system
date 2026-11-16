#!/usr/bin/env python3
"""408 单题入库落盘后校验（只读）。

职责合并说明：原方案中的 detail_link_check_408 已并入本脚本
（--scope detail），因为详情卡/bridge 检查在正式入库 closeout 和
“只修详情卡/跳转”路径都要跑，保持一份逻辑。

用法：
  python3 scripts/intake_validate_408.py --id CO_2014_002 [--repo .]
      [--new]            # 新入库模式：要求所有表都有该 ID 行
      [--scope all|tables|detail]   # 默认 all
      [--http]           # 实际探测 bridge /api/open/{ID}
      [--full]           # 追加运行 network + safe-card + extended + review-unit-v2 四项全库审计
      [--json]

exit code：0 = 无 FAIL；1 = 有 FAIL。
"""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import urllib.error
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from intake_lib_408 import (  # noqa: E402
    DATE_INDEX_COLS, DATE_INDEX_FILE, EDGE_COLS, EDGE_FILE, HIST_ID_RE,
    MASTER_COLS, MASTER_FILE, NODE_LIB_FILE, REDO_FILE, REVIEW_UNIT_COLS,
    REVIEW_UNIT_FILE, YEAR_INDEX_FILE, Repo, find_table, iter_table_rows,
    normalize_tag, read_text, scan_leak, split_row,
)
from intake_preflight_408 import DETAILS_ROOT_DEFAULT, Report  # noqa: E402

import runtime_paths_408 as _paths

# Host-specific, like the details vault: override with ``KAOYAN_408_BRIDGE_SNAPSHOT``
# so a sandbox or second machine can run the closeout without editing tracked code.
SNAPSHOT_PATH = Path(
    os.environ.get(
        "KAOYAN_408_BRIDGE_SNAPSHOT",
        str(_paths.codex_home() / 'kaoyan-408-obsidian-bridge/records.json'),
    )
)
BRIDGE_URL = os.environ.get("KAOYAN_408_BRIDGE_URL", "http://127.0.0.1:8766")

SAFE_CARD_REQUIRED = [
    "安全题目摘要", "关键题设参数", "问法类型", "用户错误入口", "复做第一动作",
    "核心考点", "模糊概念", "错因标签", "详情入口", "复做保护",
]


# ---------------------------------------------------------------- tables scope

def check_tables(rep: Report, repo: Repo, fid: str, require_all: bool):
    master = repo.master()
    row = master.get(fid)
    if row is None:
        rep.fail("节点总表", f"无 {fid} 行")
        return
    if "_bad_colcount" in row:
        rep.fail("节点总表列数", f"{fid} 行 {row['_bad_colcount']} 列，应为 {len(MASTER_COLS)} 列")
    else:
        rep.ok("节点总表", f"{fid} 行存在且 16 列完整")

    subject = fid.split("_")[0]

    # 镜像行
    lib = read_text(repo.path(NODE_LIB_FILE[subject])).splitlines()
    lib_row = next((c for _, c in iter_table_rows(lib, "ID") if c and c[0] == fid), None)
    if lib_row is None:
        rep.fail("科目节点库", f"{NODE_LIB_FILE[subject]} 缺 {fid} 镜像行")
    elif lib_row[6] != row.get("最近复做日期"):
        rep.warn("科目节点库", f"镜像最近复做日期 {lib_row[6]} 与总表 {row.get('最近复做日期')} 不一致")
    else:
        rep.ok("科目节点库", "镜像行存在且复做日期一致")

    # 年份索引
    ytext = read_text(repo.path(YEAR_INDEX_FILE))
    if re.search(rf"^\|{re.escape(fid)}\|", ytext, re.M):
        rep.ok("年份索引", f"含 {fid}")
    else:
        (rep.fail if require_all else rep.warn)("年份索引", f"缺 {fid} 行")

    # 日期索引
    dlines = read_text(repo.path(DATE_INDEX_FILE)).splitlines()
    drow = next((c for _, c in iter_table_rows(dlines, "ID") if c and c[0] == fid), None)
    if drow is None:
        rep.fail("错题日期索引", f"缺 {fid} 行")
    elif len(drow) != len(DATE_INDEX_COLS):
        rep.fail("错题日期索引列数", f"{fid} 行 {len(drow)} 列，应为 {len(DATE_INDEX_COLS)}")
    elif drow[5] != row.get("最近复做日期"):
        rep.fail("错题日期索引", f"最近复做日期 {drow[5]} 与总表 {row.get('最近复做日期')} 不一致")
    else:
        rep.ok("错题日期索引", "行存在、列数与复做日期一致")

    # 复做记录
    rtext = read_text(repo.path(REDO_FILE))
    if re.search(rf"^\|[^|]*\|{re.escape(fid)}\|", rtext, re.M):
        rep.ok("错题复做记录", f"含 {fid} 记录")
    else:
        (rep.fail if require_all else rep.warn)("错题复做记录", f"缺 {fid} 记录行")

    # 复习单元
    utext = read_text(repo.path(REVIEW_UNIT_FILE))
    if f"RU_{fid}" in utext:
        rep.ok("复习单元总表", f"含 RU_{fid}")
    else:
        rep.warn("复习单元总表", f"缺 RU_{fid}（如属回滚调度管理可忽略）")

    # 命中索引：主知识点小节必须含该 ID（audit 检查10 的单点版）
    mk_code, _ = normalize_tag(row.get("主知识点", ""))
    htext = read_text(repo.path("知识点命中索引.md"))
    sec = re.search(rf"^### {re.escape(mk_code)} .*?(?=^### |\Z)", htext, re.M | re.S)
    if sec and re.search(rf"^- {re.escape(fid)}：", sec.group(0), re.M):
        rep.ok("知识点命中索引", f"{mk_code} 小节含 {fid}")
    else:
        rep.fail("知识点命中索引", f"{mk_code} 小节缺 {fid}")

    # 安全卡完整性
    card = repo.path(f"{fid}.md")
    if not card.exists():
        rep.fail("安全节点卡片", f"{fid}.md 缺失")
    else:
        text = read_text(card)
        missing = [k for k in SAFE_CARD_REQUIRED if k not in text]
        blank = [k for k in SAFE_CARD_REQUIRED
                 if re.search(rf"- {re.escape(k)}[：:]\s*$", text, re.M)]
        if missing:
            rep.fail("安全节点卡片", f"缺字段：{missing}")
        elif blank:
            rep.fail("安全节点卡片", f"字段为空：{blank}")
        elif "附件登记" not in text:
            rep.warn("安全节点卡片", "无 附件登记 区块（有题图/解析证据时必须补）")
        else:
            rep.ok("安全节点卡片", "字段齐全非空，含附件登记")

    # 答案泄露：总表行 + 日期索引行 + 安全卡全文
    leak_src = {"节点总表行": "|".join(row.get(c, "") for c in MASTER_COLS if not c.startswith("_")),
                "安全卡": read_text(card) if card.exists() else "",
                "日期索引行": "|".join(drow or [])}
    clean = True
    for name, text in leak_src.items():
        fails, warns = scan_leak(text)
        for f in fails:
            rep.fail(f"答案泄露({name})", f)
            clean = False
        for w in warns:
            rep.warn(f"答案泄露({name})", w)
    if clean:
        rep.ok("答案泄露扫描", "总表行 / 日期索引行 / 安全卡未发现答案模式")

    # 关系边端点合法性（该节点相关的边）
    bad = 0
    for e in repo.edges():
        if fid in (e["起点ID"], e["终点ID"]):
            other = e["终点ID"] if e["起点ID"] == fid else e["起点ID"]
            if HIST_ID_RE.match(other) or other not in master:
                rep.fail("关系边", f"{e['起点ID']}→{e['终点ID']} 端点非法或不存在")
                bad += 1
    if not bad:
        rep.ok("关系边", f"{fid} 相关边端点均合法")


def check_table_shapes(rep: Report, repo: Repo):
    """全表列数一致性快检（不深入语义）。"""
    for rel, first_col, ncols in [
        (MASTER_FILE, "ID", len(MASTER_COLS)),
        (DATE_INDEX_FILE, "ID", len(DATE_INDEX_COLS)),
        (EDGE_FILE, "起点ID", len(EDGE_COLS)),
        (REVIEW_UNIT_FILE, "复习单元ID", len(REVIEW_UNIT_COLS)),
    ]:
        lines = read_text(repo.path(rel)).splitlines()
        bad = [i + 1 for i, c in iter_table_rows(lines, first_col) if len(c) != ncols]
        if bad:
            rep.fail(f"表列数 {rel}", f"{len(bad)} 行列数异常（行号 {bad[:5]}…）")
        else:
            rep.ok(f"表列数 {rel}", f"全部 {ncols} 列")


# ---------------------------------------------------------------- detail scope

def check_detail(rep: Report, repo: Repo, fid: str, details_root: Path, do_http: bool,
                 snapshot_path: Path = SNAPSHOT_PATH):
    card = details_root / "cards" / f"{fid}.md"
    assets = details_root / "assets" / fid
    if not card.exists():
        rep.warn("详情卡", f"{card} 不存在（B 路径：详情入口按待补处理，勿输出误导性打开链接）")
    else:
        rep.ok("详情卡", str(card))
        text = read_text(card)
        has_embed = "![[" in text or "![](" in text
        if assets.is_dir() and any(assets.iterdir()):
            rep.ok("题图资产", f"{assets} 非空")
            if not has_embed:
                rep.warn("详情卡嵌入", "assets 有图但卡内无 ![[]] 嵌入，确认卡片可见题图")
        elif has_embed:
            rep.warn("题图资产", f"卡内有嵌入引用但 {assets} 缺失或为空")
        else:
            rep.warn("题图资产", "无 assets 目录且卡内无嵌入（纯文字语料卡可忽略）")

    # snapshot
    if not snapshot_path.exists():
        rep.fail("bridge snapshot", f"{snapshot_path} 不存在，先跑 python3 scripts/build_obsidian_bridge_snapshot.py")
        return
    try:
        snap = json.loads(read_text(snapshot_path))
        records = snap.get("records", snap) if isinstance(snap, dict) else snap
        if isinstance(records, dict):
            found = fid in records
        else:
            found = any(fid in (r.get("node_id"), r.get("formal_id"), r.get("id")) for r in records)
    except (json.JSONDecodeError, AttributeError) as e:
        rep.fail("bridge snapshot", f"解析失败：{e}")
        return
    if not found:
        rep.fail("bridge snapshot", f"不含 {fid}，需刷新：python3 scripts/build_obsidian_bridge_snapshot.py 或 GET {BRIDGE_URL}/refresh")
    else:
        rep.ok("bridge snapshot", f"含 {fid}")
        freshness_sources = [repo.path(f"{fid}.md"), repo.path(MASTER_FILE), repo.path(YEAR_INDEX_FILE)]
        if card.exists():
            freshness_sources.append(card)
        newest = max((p.stat().st_mtime for p in freshness_sources if p.exists()), default=0)
        if snapshot_path.stat().st_mtime < newest:
            rep.warn("bridge snapshot", "早于正式表/安全卡/详情卡最新修改时间，批量收尾前必须刷新")

    if do_http:
        url = f"{BRIDGE_URL}/api/open/{fid}"
        try:
            with urllib.request.urlopen(url, timeout=3) as resp:
                body = resp.read(10000).decode("utf-8", "replace")
                rep.ok("bridge 探测", f"GET /api/open/{fid} → {resp.status}")
                shows_detail = False
                try:
                    payload = json.loads(body)
                    record = payload.get("record", {}) if isinstance(payload, dict) else {}
                    launch = payload.get("launch", {}) if isinstance(payload, dict) else {}
                    target_note = str(record.get("target_note", ""))
                    detail_note = str(record.get("detail_note", ""))
                    launch_url = str(launch.get("url", ""))
                    shows_detail = target_note.startswith("cards/") or detail_note.startswith("cards/") or "cards%2F" in launch_url
                except json.JSONDecodeError:
                    shows_detail = "detail" in body or "cards" in body
                if not shows_detail:
                    rep.warn("bridge 探测", "响应未显示打开详情卡，确认桥优先打开 kaoyan-408-details")
        except (urllib.error.URLError, OSError) as e:
            rep.warn("bridge 探测", f"服务不可达（{e}）。需要即点即开时执行：python3 scripts/obsidian_link_bridge.py --host 127.0.0.1 --port 8766 --vault kaoyan-408")


# ---------------------------------------------------------------- full scope

def run_full_audits(rep: Report, repo: Repo, details_root: Path):
    audits = [
        ("audit_network_health.py", "全网健康审计", []),
        ("check_safe_node_cards.py", "安全卡全量检查", ["--list-missing"]),
        ("audit_extended.py", "扩展质量审计", ["--details-root", str(details_root)]),
        ("audit_review_schedule.py", "复习单元 v2 调度审计", []),
    ]
    for script, name, extra in audits:
        p = repo.path(f"scripts/{script}")
        if not p.exists():
            rep.fail(name, f"缺少审计脚本：{p}")
            continue
        try:
            r = subprocess.run([sys.executable, str(p), "--repo", str(repo.root), *extra],
                               capture_output=True, text=True, timeout=120)
        except subprocess.TimeoutExpired:
            rep.fail(name, "运行超过 120 秒，按硬失败处理")
            continue
        tail = (r.stdout or r.stderr).strip().splitlines()[-6:]
        if r.returncode == 0:
            rep.ok(name, " / ".join(tail[-1:]))
        else:
            rep.fail(name, "；".join(tail))


def main():
    ap = argparse.ArgumentParser(description="408 单题入库落盘后校验（只读）")
    ap.add_argument("--id", required=False, help="正式节点 ID")
    ap.add_argument("--repo", default=".")
    ap.add_argument("--details-root", default=DETAILS_ROOT_DEFAULT)
    ap.add_argument("--snapshot", default=str(SNAPSHOT_PATH), help="bridge snapshot JSON 路径")
    ap.add_argument("--scope", choices=["all", "tables", "detail"], default="all")
    ap.add_argument("--new", action="store_true", help="新入库：所有表必须含该 ID")
    ap.add_argument("--http", action="store_true", help="实际探测 bridge")
    ap.add_argument("--full", action="store_true", help="追加全库审计")
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args()

    repo = Repo(args.repo)
    rep = Report()
    if args.id and args.scope in ("all", "tables"):
        check_tables(rep, repo, args.id, args.new)
        check_table_shapes(rep, repo)
    if args.id and args.scope in ("all", "detail"):
        check_detail(rep, repo, args.id, Path(args.details_root), args.http, Path(args.snapshot))
    if not args.id and not args.full:
        ap.error("需要 --id，或用 --full 单独执行全局门禁")
    if args.full:
        run_full_audits(rep, repo, Path(args.details_root))

    rep.print_json() if args.json else rep.print_text()
    sys.exit(1 if rep.failed else 0)


if __name__ == "__main__":
    main()
