#!/usr/bin/env python3
"""Legacy pre-study knowledge-network rollback preview.

The default mode is read-only and prints to stdout.  New morning review work
should use ``generate_morning_review_queue.py``.  This compatibility tool no
longer writes ``复盘输出/`` by default.

It only generates a review checklist. It does not update review dates,
scores, nodes, concepts, or relation edges.
"""

from __future__ import annotations

import argparse
import dataclasses
import datetime as dt
import re
import sys
from collections import Counter
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SUBJECTS = ["数据结构", "计算机组成原理", "操作系统", "计算机网络"]
NODE_ID_RE = re.compile(r"\b(?:DS|CO|OS|CN)_(?:\d{4}|UNK)_\d{3}\b")


@dataclasses.dataclass
class ReviewUnit:
    unit_id: str
    subject: str
    module: str
    title: str
    unit_type: str
    first_date: str
    last_date: str
    next_date: str
    interval: int
    mastery: str
    mistakes: int
    importance: int
    difficulty: str
    method: str
    note: str
    score: float = 0.0
    overdue_days: int = 0

    @property
    def source_nodes(self) -> list[str]:
        return unique(NODE_ID_RE.findall(self.note + " " + self.method + " " + self.title))


@dataclasses.dataclass
class Node:
    node_id: str
    source_id: str
    year: str
    subject: str
    module: str
    main_knowledge: str
    hit_knowledge: str
    core_point: str
    fuzzy: str
    error_tags: str
    first_date: str
    last_date: str
    latest_error: str
    detail: str


@dataclasses.dataclass
class Edge:
    start: str
    end: str
    relation_type: str
    strength: str
    reason: str
    priority: str


@dataclasses.dataclass
class RedoRecord:
    date: dt.date | None
    raw_date: str
    item_id: str
    source_id: str
    action: str
    note: str


def unique(items: list[str]) -> list[str]:
    seen: set[str] = set()
    result: list[str] = []
    for item in items:
        if item and item not in seen:
            seen.add(item)
            result.append(item)
    return result


def parse_date(value: str) -> dt.date | None:
    value = value.strip()
    if not value or value in {"未记录", "待补充"}:
        return None
    try:
        return dt.date.fromisoformat(value)
    except ValueError:
        return None


def parse_int(value: str, default: int = 0) -> int:
    match = re.search(r"-?\d+", value or "")
    return int(match.group(0)) if match else default


def split_md_row(line: str) -> list[str]:
    return [cell.strip() for cell in line.strip().strip("|").split("|")]


def is_separator(cells: list[str]) -> bool:
    return bool(cells) and all(re.fullmatch(r":?-{3,}:?", cell.strip()) for cell in cells)


def read_markdown_table(path: Path) -> list[dict[str, str]]:
    rows: list[dict[str, str]] = []
    headers: list[str] | None = None
    if not path.exists():
        return rows
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip().startswith("|"):
            continue
        cells = split_md_row(line)
        if is_separator(cells):
            continue
        if headers is None:
            headers = cells
            continue
        if len(cells) < len(headers):
            cells.extend([""] * (len(headers) - len(cells)))
        rows.append(dict(zip(headers, cells)))
    return rows


def difficulty_weight(value: str) -> int:
    if value.startswith("A"):
        return 1
    if value.startswith("C"):
        return 3
    return 2


def mastery_penalty(value: str) -> int:
    value = value.strip()
    if value == "待评分":
        return 2
    return parse_int(value, default=3)


def load_review_units(today: dt.date) -> list[ReviewUnit]:
    units: list[ReviewUnit] = []
    for row in read_markdown_table(ROOT / "复习单元总表.md"):
        unit = ReviewUnit(
            unit_id=row.get("复习单元ID", ""),
            subject=row.get("科目", ""),
            module=row.get("模块", ""),
            title=row.get("内容名称", ""),
            unit_type=row.get("类型", ""),
            first_date=row.get("首次学习日期", ""),
            last_date=row.get("上次复习日期", ""),
            next_date=row.get("下次复习日期", ""),
            interval=parse_int(row.get("当前间隔天数", ""), 1),
            mastery=row.get("掌握度", ""),
            mistakes=parse_int(row.get("错误次数", ""), 0),
            importance=parse_int(row.get("重要程度", ""), 0),
            difficulty=row.get("难度等级", ""),
            method=row.get("复习方式", ""),
            note=row.get("备注", ""),
        )
        next_date = parse_date(unit.next_date)
        unit.overdue_days = max((today - next_date).days, 0) if next_date else 0
        type_bonus = 2 if unit.unit_type == "错题" else 1 if unit.unit_type in {"易混概念", "算法"} else 0
        wait_bonus = 1 if unit.mastery == "待评分" else 0
        unit.score = (
            unit.overdue_days * 3
            + unit.mistakes * 2
            + unit.importance * 2
            + difficulty_weight(unit.difficulty)
            + type_bonus
            + wait_bonus
            - mastery_penalty(unit.mastery)
        )
        units.append(unit)
    return units


def load_nodes() -> dict[str, Node]:
    nodes: dict[str, Node] = {}
    for row in read_markdown_table(ROOT / "节点总表.md"):
        node_id = row.get("ID", "")
        if not node_id:
            continue
        nodes[node_id] = Node(
            node_id=node_id,
            source_id=row.get("来源ID", ""),
            year=row.get("年份", ""),
            subject=row.get("科目", ""),
            module=row.get("主模块", ""),
            main_knowledge=row.get("主知识点", ""),
            hit_knowledge=row.get("命中知识点", ""),
            core_point=row.get("核心考点", ""),
            fuzzy=row.get("模糊概念", ""),
            error_tags=row.get("错因标签", ""),
            first_date=row.get("首次做题日期", ""),
            last_date=row.get("最近复做日期", ""),
            latest_error=row.get("最近错误记录", ""),
            detail=row.get("详情入口", ""),
        )
    return nodes


def load_edges() -> list[Edge]:
    edges: list[Edge] = []
    for row in read_markdown_table(ROOT / "关系边表.md"):
        if not row.get("起点ID") or not row.get("终点ID"):
            continue
        edges.append(
            Edge(
                start=row.get("起点ID", ""),
                end=row.get("终点ID", ""),
                relation_type=row.get("关系类型", ""),
                strength=row.get("联系强度", ""),
                reason=row.get("关联原因", ""),
                priority=row.get("复盘优先级", ""),
            )
        )
    return edges


def load_redo_records() -> list[RedoRecord]:
    records: list[RedoRecord] = []
    for row in read_markdown_table(ROOT / "错题复做记录.md"):
        raw_date = row.get("日期", "")
        records.append(
            RedoRecord(
                date=parse_date(raw_date),
                raw_date=raw_date,
                item_id=row.get("ID", ""),
                source_id=row.get("来源ID", ""),
                action=row.get("动作", ""),
                note=row.get("错误记录", ""),
            )
        )
    return records


def load_history_locations() -> dict[str, dict[str, str]]:
    result: dict[str, dict[str, str]] = {}
    for row in read_markdown_table(ROOT / "历史错题归档" / "定位索引.md"):
        source_id = row.get("历史归档ID", "")
        if source_id:
            result[source_id] = row
    return result


def due_units(units: list[ReviewUnit], today: dt.date) -> list[ReviewUnit]:
    due: list[ReviewUnit] = []
    for unit in units:
        next_date = parse_date(unit.next_date)
        if next_date is None or next_date <= today:
            due.append(unit)
    return sorted(due, key=lambda item: (-item.score, item.next_date, item.unit_id))


def recent_records(records: list[RedoRecord], today: dt.date, lookback_days: int) -> list[RedoRecord]:
    start = today - dt.timedelta(days=lookback_days)
    return [
        record
        for record in records
        if record.date is not None and start <= record.date <= today
    ]


def seed_nodes(due: list[ReviewUnit], records: list[RedoRecord]) -> list[str]:
    seeds: list[str] = []
    for unit in due:
        seeds.extend(unit.source_nodes)
    for record in records:
        if NODE_ID_RE.fullmatch(record.item_id):
            seeds.append(record.item_id)
    return unique(seeds)


def strength_rank(strength: str) -> int:
    return {"强": 0, "中": 1, "弱": 2}.get(strength, 3)


def relation_rank(relation_type: str) -> int:
    match = re.match(r"R(\d{2})", relation_type)
    if not match:
        return 99
    code = match.group(1)
    if code in {"01", "02", "03", "04", "05"}:
        return 0
    if code in {"06", "09"}:
        return 1
    return 2


def date_bonus(node: Node | None, today: dt.date) -> int:
    if node is None:
        return 0
    last = parse_date(node.last_date)
    if last is None:
        return 1
    return 2 if (today - last).days >= 7 else 0


def eligible_old_question(node: Node | None, today: dt.date) -> bool:
    """Apply the 7-day hard gate before ranking a concrete old question."""
    if node is None:
        return False
    last = parse_date(node.last_date)
    return last is None or (today - last).days >= 7


def network_candidates(
    seeds: list[str],
    nodes: dict[str, Node],
    edges: list[Edge],
    today: dt.date,
) -> list[tuple[float, str, str, Edge]]:
    seed_set = set(seeds)
    candidates: list[tuple[float, str, str, Edge]] = []
    for edge in edges:
        if edge.strength not in {"强", "中"}:
            continue
        if edge.start in seed_set:
            trigger, rollback = edge.start, edge.end
        elif edge.end in seed_set:
            trigger, rollback = edge.end, edge.start
        else:
            continue
        rollback_node = nodes.get(rollback)
        if not eligible_old_question(rollback_node, today):
            continue
        score = (
            (3 - strength_rank(edge.strength)) * 10
            + (3 - relation_rank(edge.relation_type)) * 4
            + date_bonus(rollback_node, today) * 3
        )
        candidates.append((score, trigger, rollback, edge))
    candidates.sort(key=lambda item: (-item[0], strength_rank(item[3].strength), item[1], item[2]))
    deduped: list[tuple[float, str, str, Edge]] = []
    seen: set[tuple[str, str]] = set()
    for item in candidates:
        key = tuple(sorted((item[1], item[2])))
        if key in seen:
            continue
        seen.add(key)
        deduped.append(item)
    return deduped


def truncate(text: str, length: int = 120) -> str:
    text = re.sub(r"\s+", " ", text).strip()
    if len(text) <= length:
        return text
    return text[: length - 1] + "…"


def protect_redo_text(text: str) -> str:
    """Mask answer-bearing fragments before rendering a redo location."""
    pattern = re.compile(
        r"(正确答案|标准答案|错误答案|我的答案|错选答案|我的错选)\s*[:：]?\s*[^；;|\n]*"
    )
    return pattern.sub(lambda match: f"{match.group(1)}：答案信息已隐藏（复做保护）", text or "")


def history_location(source_id: str, history: dict[str, dict[str, str]]) -> list[str]:
    if not source_id.startswith("H"):
        return ["历史归档定位：无"]
    row = history.get(source_id)
    if not row:
        return [f"历史归档定位：{source_id}；定位索引未找到"]
    return [
        f"历史归档ID：{source_id}",
        f"定位标记：{row.get('定位标记', '未提取') or '未提取'}",
        f"段落号：{row.get('段落号', '未提取') or '未提取'}",
        f"完整来源位置：{protect_redo_text(row.get('完整来源位置', '未记录') or '未记录')}",
    ]


def subject_counts(units: list[ReviewUnit]) -> Counter[str]:
    counts: Counter[str] = Counter()
    for unit in units:
        counts[unit.subject] += 1
    return counts


def build_main_line(due: list[ReviewUnit], recent: list[RedoRecord]) -> str:
    modules: list[str] = []
    for unit in due[:8]:
        modules.extend([part.strip() for part in re.split(r"[；;]", unit.module) if part.strip()])
    for record in recent:
        if record.source_id == "KNOWLEDGE_NOTE":
            modules.append(record.item_id)
    text = "；".join(unique([item for item in modules if item])) or "到期复习单元"
    if len(text) > 90:
        text = text[:89] + "…"
    return text


def unit_block(index: int, unit: ReviewUnit) -> list[str]:
    reason_bits = [
        f"下次复习日期：{unit.next_date or '未记录'}",
        f"已过期：{unit.overdue_days} 天",
        f"掌握度：{unit.mastery or '待评分'}",
        f"错误次数：{unit.mistakes}",
        f"重要程度：{unit.importance}",
    ]
    return [
        f"### {index}. {unit.title}",
        "",
        f"- 复习单元：`{unit.unit_id}`",
        f"- 科目 / 模块：{unit.subject} / {unit.module}",
        f"- 类型：{unit.unit_type}；难度：{unit.difficulty}",
        f"- 调度原因：{'；'.join(reason_bits)}。",
        f"- 本次复习方式：{unit.method}",
        f"- 来源依据：{truncate(unit.note, 180)}",
        "- 复习后需要评分：0-5 分",
        "",
    ]


def network_block(
    index: int,
    trigger_id: str,
    rollback_id: str,
    edge: Edge,
    nodes: dict[str, Node],
    history: dict[str, dict[str, str]],
) -> list[str]:
    trigger = nodes.get(trigger_id)
    rollback = nodes.get(rollback_id)
    trigger_desc = trigger.core_point if trigger else "节点未在节点总表中找到"
    rollback_desc = rollback.core_point if rollback else "节点未在节点总表中找到"
    lines = [
        f"### {index}. `{trigger_id}` -> `{rollback_id}`",
        "",
        f"- 触发节点概况：{trigger_desc}",
        f"- 回滚旧题概况：{rollback_desc}",
        f"- 关系类型：{edge.relation_type}；联系强度：{edge.strength}；复盘优先级：{edge.priority}",
        f"- 推荐原因：{edge.reason}",
    ]
    if rollback:
        lines.extend(
            [
                f"- 来源ID：{rollback.source_id}",
                f"- 年份：{rollback.year}",
                f"- 首次做题日期：{rollback.first_date}",
                f"- 最近复做日期：{rollback.last_date}",
                f"- 最近错误记录：{truncate(protect_redo_text(rollback.latest_error), 160)}",
            ]
        )
        for item in history_location(rollback.source_id, history):
            lines.append(f"- {item}")
        if not rollback.source_id.startswith("H"):
            lines.append(f"- 完整来源位置：{rollback.detail or '节点总表中未记录详情入口'}")
    lines.append("")
    return lines


def recent_knowledge_block(records: list[RedoRecord], limit: int) -> list[str]:
    notes = [record for record in records if record.source_id == "KNOWLEDGE_NOTE"]
    notes.sort(key=lambda record: (record.date or dt.date.min, record.item_id), reverse=True)
    if not notes:
        return ["近几天没有知识点复习记录。", ""]
    lines: list[str] = []
    for index, record in enumerate(notes[:limit], start=1):
        lines.extend(
            [
                f"### {index}. {record.item_id}",
                "",
                f"- 日期：{record.raw_date}",
                f"- 动作：{record.action}",
                f"- 学前补温：{truncate(record.note, 220)}",
                "",
            ]
        )
    return lines


def minute_plan(required: list[ReviewUnit], network: list[tuple[float, str, str, Edge]]) -> list[str]:
    top3 = " -> ".join(f"`{unit.unit_id}`" for unit in required[:3]) or "无"
    top4 = " -> ".join(f"`{unit.unit_id}`" for unit in required[:4]) or "无"
    top5 = " -> ".join(f"`{unit.unit_id}`" for unit in required[:5]) or "无"
    edge2 = " -> ".join(f"`{item[2]}`" for item in network[:2]) or "无"
    edge3 = " -> ".join(f"`{item[2]}`" for item in network[:3]) or "无"
    return [
        "- 15 分钟：只做 A 组前 3 个，顺序为：" + top3,
        "- 30 分钟：做 A 组前 4 个；有余力再回滚 1 条合格旧题：" + top4 + "；" + edge2,
        "- 45 分钟：最多做 A 组前 5 个，再回滚 B 组前 3 条合格关系边：" + edge3,
        "",
    ]


def render(args: argparse.Namespace) -> str:
    today = dt.date.fromisoformat(args.date)
    units = load_review_units(today)
    nodes = load_nodes()
    edges = load_edges()
    history = load_history_locations()
    records = load_redo_records()
    due = due_units(units, today)
    recent = recent_records(records, today, args.lookback_days)
    counts = subject_counts(due)
    main_line = build_main_line(due, recent)

    required = due[: args.max_units]
    # Relation expansion must start from what the user will actually review,
    # not from the entire backlog.
    seeds = seed_nodes(required, recent)
    network = network_candidates(seeds, nodes, edges, today)
    network_top = network[: args.max_network]

    lines: list[str] = [
        f"# {today.isoformat()} 学前知识网络回滚",
        "",
        "本文件用于正式开始今天新学习前预热。它由 `scripts/generate_knowledge_rollback.py` 根据复习单元、错题节点、关系边和近期复做记录生成；只保存复习顺序、检查点和来源定位，不保存完整题干、完整解析、图片或手写痕迹。",
        "",
        "## 一、今日启动口径",
        "",
        f"- 生成日期：{today.isoformat()}",
        f"- 回看窗口：最近 {args.lookback_days} 天",
        f"- 到期或过期复习单元：{len(due)} 个",
        f"- 关系网种子节点：{len(seeds)} 个",
        f"- 今日主线：{main_line}",
        "",
        "### 科目分布",
        "",
    ]
    for subject in SUBJECTS:
        lines.append(f"- {subject}：{counts.get(subject, 0)} 个")
    lines.extend(
        [
            "",
            "## 二、A 组：开始学习前必须完成",
            "",
            "这部分按到期/过期、错误次数、重要程度、难度和掌握度排序。先完成这里，再开始新内容。",
            "",
        ]
    )
    if required:
        for index, unit in enumerate(required, start=1):
            lines.extend(unit_block(index, unit))
        remaining = len(due) - len(required)
        if remaining > 0:
            lines.append(f"还有 {remaining} 个到期单元未列入 A 组，可在完成后继续运行脚本并调大 `--max-units`。")
            lines.append("")
    else:
        lines.extend(["今天没有到期复习单元。", ""])

    lines.extend(
        [
            "## 三、B 组：知识网络强关联回滚",
            "",
            "这部分从 A 组和近期错题出发，只使用 `关系边表.md` 已有关系，不临时强行建边。",
            "",
        ]
    )
    if network_top:
        for index, (_, trigger_id, rollback_id, edge) in enumerate(network_top, start=1):
            lines.extend(network_block(index, trigger_id, rollback_id, edge, nodes, history))
    else:
        lines.extend(["当前没有从到期单元扩展出的强/中关联旧题，不强行建边。", ""])

    lines.extend(
        [
            "## 四、C 组：近几天知识点补温",
            "",
            "这部分来自 `错题复做记录.md` 中的知识点复习记录，用来在进入新学习前先恢复概念手感。",
            "",
        ]
    )
    lines.extend(recent_knowledge_block(recent, args.max_recent_notes))

    lines.extend(
        [
            "## 五、时间不够时的执行方案",
            "",
        ]
    )
    lines.extend(minute_plan(required, network_top))

    lines.extend(
        [
            "## 六、复习后回传评分格式",
            "",
            "复习完成后，把下面这种格式回传，再更新 `复习单元总表.md` 的掌握度、下次复习日期和间隔。",
            "",
            "```text",
            f"完成 {today.isoformat()} 学前知识网络回滚：",
        ]
    )
    for unit in required:
        lines.append(f"{unit.unit_id} = 0-5 分")
    lines.extend(
        [
            "B组旧题：哪些仍不熟 = ",
            "```",
            "",
            "没有实际评分前，不提前改下次复习日期。",
            "",
        ]
    )
    return "\n".join(lines)


def parse_args(argv: list[str]) -> argparse.Namespace:
    today = dt.date.today().isoformat()
    parser = argparse.ArgumentParser(description="Generate a 408 pre-study knowledge rollback checklist.")
    parser.add_argument("--date", default=today, help="Target date in YYYY-MM-DD format.")
    parser.add_argument("--lookback-days", type=int, default=7, help="How many recent days of redo records to use.")
    parser.add_argument("--max-units", type=int, default=5, help="Maximum due review units in A group.")
    parser.add_argument("--max-network", type=int, default=2, help="Maximum relation edges in B group.")
    parser.add_argument("--max-recent-notes", type=int, default=3, help="Maximum recent knowledge notes in C group.")
    parser.add_argument("--output", help="Explicit safe output path; paths under 复盘输出 are rejected.")
    parser.add_argument("--dry-run", action="store_true", help="Print to stdout without writing a file.")
    return parser.parse_args(argv)


def main(argv: list[str]) -> int:
    args = parse_args(argv)
    try:
        dt.date.fromisoformat(args.date)
    except ValueError:
        print("--date must be YYYY-MM-DD", file=sys.stderr)
        return 2

    content = render(args)
    if args.dry_run or not args.output:
        print(content)
        if not args.dry_run:
            print(
                "legacy generator is read-only by default; use generate_morning_review_queue.py",
                file=sys.stderr,
            )
        return 0
    output = Path(args.output)
    if not output.is_absolute():
        output = ROOT / output
    resolved = output.resolve()
    protected_root = (ROOT / "复盘输出").resolve()
    try:
        resolved.relative_to(protected_root)
    except ValueError:
        pass
    else:
        print("generic morning queues may not be written to 复盘输出/", file=sys.stderr)
        return 2
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(content + "\n", encoding="utf-8")
    print(output)
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
