#!/usr/bin/env python3
"""Normalize high-confidence legacy knowledge-point strings to official codes.

This script applies only curated mappings whose target labels already exist in
`知识点标签表.md`. It updates the formal node table and derived index views.
It does not add full questions, explanations, images, or handwritten traces.
"""

from __future__ import annotations

import argparse
import re
from collections import defaultdict
from dataclasses import dataclass
from datetime import date
from pathlib import Path

from audit_408_docx_import import KNOWLEDGE_CODE_RE, read_markdown_table


@dataclass(frozen=True)
class Mapping:
    node_id: str
    target: str
    reason: str


MAPPINGS: list[Mapping] = [
    Mapping("DS_2020_001", "DS03-15 特殊矩阵压缩存储", "核心考点明确为特殊矩阵压缩存储。"),
    Mapping("CO_2013_002", "CO04-01 指令系统基本概念", "核心考点为 ISA 与指令系统判断。"),
    Mapping("CO_2016_002", "CO06-06 I/O 接口", "上下文为通道技术等 I/O 硬件机制识别；当前标签表无通道专项，归入 I/O 接口。"),
    Mapping("CO_2023_003", "CO06-18 DMA 方式", "上下文直接考查能否发起 DMA 请求取决于 DMA 接口能力。"),
    Mapping("CO_2024_003", "CO06-20 DMA 传送过程", "核心考点为 DMA 预处理与后处理。"),
    Mapping("CO_UNK_002", "CO03-21 平均访存时间", "DOCX 上下文直接考查 Cache 平均访问时间与命中率加权计算。"),
    Mapping("CO_UNK_006", "CO04-16 相对寻址", "条件转移偏移计算属于相对寻址。"),
    Mapping("CO_UNK_008", "CO04-08 寻址方式", "IMUL 指令与寻址归入寻址方式。"),
    Mapping("CO_UNK_009", "CO04-25 函数调用机器级表示", "调用指令描述归入函数调用机器级表示。"),
    Mapping("CO_UNK_022", "CO05-02 CPU 基本结构", "n 位 CPU 含义在本地上下文中归入 CPU 基本结构判断。"),
    Mapping("CO_UNK_024", "CO05-05 寄存器", "程序计数器 PC 属于 CPU 寄存器。"),
    Mapping("CO_UNK_028", "CO05-02 CPU 基本结构", "题干围绕 CPU 组成部件、PC 与状态寄存器等基本结构判断。"),
    Mapping("CO_UNK_029", "CO05-05 寄存器", "程序计数器 PC 位数判断先归入 CPU 寄存器。"),
    Mapping("CO_UNK_030", "CO05-32 多处理器基本概念", "MIMD 属于多处理器基本概念。"),
    Mapping("CO_UNK_031", "CO05-32 多处理器基本概念", "SIMD 属于多处理器基本概念。"),
    Mapping("CO_UNK_032", "CO05-32 多处理器基本概念", "多核处理器属于多处理器基本概念。"),
    Mapping("CO_UNK_055", "CO05-22 指令流水线", "高级流水线技术先归入指令流水线。"),
    Mapping("CO_UNK_056", "CO06-03 总线性能指标", "核心考点为总线性能指标。"),
    Mapping("CO_UNK_057", "CO06-03 总线性能指标", "核心考点为总线性能指标。"),
    Mapping("CO_UNK_058", "CO06-01 总线基本概念", "串行/并行数据传输方式归入总线基本概念。"),
    Mapping("CO_UNK_059", "CO06-02 总线组成", "系统总线结构归入总线组成。"),
    Mapping("CO_UNK_060", "CO06-02 总线组成", "系统总线结构归入总线组成。"),
    Mapping("CO_UNK_061", "CO06-05 总线定时", "异步定时方式属于总线定时。"),
    Mapping("CO_UNK_068", "CO03-14 Cache 地址划分", "Cache 与主存编址关系归入 Cache 地址划分。"),
    Mapping("OS_2016_001", "OS02-32 同步与互斥", "进程互斥归入同步与互斥。"),
    Mapping("OS_2016_002", "OS02-32 同步与互斥", "DOCX 上下文为管程特性，当前标签表无管程专项，先归入同步与互斥。"),
    Mapping("OS_2017_001", "OS02-28 时间片轮转 RR", "交互式系统常用调度算法归入时间片轮转。"),
    Mapping("OS_2018_003", "OS02-21 调度目标", "调度算法评价指标归入调度目标。"),
    Mapping("OS_2019_001", "OS02-44 死锁基本概念", "2019 真题上下文直接考查死锁判断与死锁解除/预防/避免边界。"),
    Mapping("OS_2022_001", "OS02-22 调度时机", "进程调度时机与切换归入调度时机。"),
    Mapping("OS_2023_001", "OS02-21 调度目标", "调度时间线题来自调度算法评价指标模块。"),
    Mapping("OS_2023_002", "OS03-23 虚拟内存基本概念", "2023 真题上下文直接考查虚拟地址空间与虚拟内存基本概念。"),
    Mapping("OS_UNK_003", "OS01-04 操作系统发展历程", "OS 发展分类归入操作系统发展历程。"),
    Mapping("OS_UNK_007", "OS01-01 操作系统基本概念", "操作系统概念与功能先归入基本概念。"),
    Mapping("OS_UNK_008", "OS01-01 操作系统基本概念", "操作系统概念与功能先归入基本概念。"),
    Mapping("OS_UNK_009", "OS01-15 CPU 核心态", "上下文直接考查哪些程序必须在内核态执行。"),
    Mapping("OS_UNK_012", "OS01-22 操作系统引导", "定位路径与上下文均指向计算机系统引导。"),
    Mapping("OS_UNK_014", "OS01-22 操作系统引导", "核心考点为计算机系统引导。"),
    Mapping("OS_UNK_015", "OS02-33 临界区", "上下文直接区分临界资源与访问临界资源的代码段。"),
    Mapping("OS_UNK_016", "OS02-32 同步与互斥", "上下文考查临界资源与互斥访问的基本概念。"),
    Mapping("OS_UNK_017", "OS02-32 同步与互斥", "上下文考查并发异步性与同步机制的必要性。"),
    Mapping("OS_UNK_018", "OS02-36 锁", "上下文定位为互斥锁，内容围绕加锁顺序与死锁场景。"),
    Mapping("OS_UNK_019", "OS02-40 条件变量", "上下文为管程 signal 与信号量 V 操作的差异，归入条件变量。"),
    Mapping("OS_UNK_027", "OS02-20 CPU 调度基本概念", "中级调度目的归入 CPU 调度基本概念。"),
    Mapping("OS_UNK_034", "OS02-11 线程基本概念", "引入线程机制后的变化归入线程基本概念。"),
    Mapping("OS_UNK_035", "OS02-11 线程基本概念", "线程实现方式先归入线程基本概念。"),
    Mapping("OS_UNK_036", "OS02-11 线程基本概念", "线程属性归入线程基本概念。"),
    Mapping("OS_UNK_037", "OS02-11 线程基本概念", "线程属性归入线程基本概念。"),
    Mapping("OS_UNK_038", "OS02-21 调度目标", "调度算法设计指标归入调度目标。"),
    Mapping("OS_UNK_039", "OS02-22 调度时机", "题目直接考查进程调度触发时机。"),
    Mapping("OS_UNK_021", "OS02-47 死锁避免", "上下文用资源总量与最大需求判断系统是否会死锁，归入死锁避免。"),
    Mapping("OS_UNK_043", "OS02-01 进程基本概念", "进程概念、组成、特征归入进程基本概念。"),
    Mapping("OS_UNK_044", "OS02-01 进程基本概念", "进程概念、组成、特征归入进程基本概念。"),
    Mapping("OS_UNK_045", "OS02-05 进程状态转换", "进程状态与转换归入进程状态转换。"),
    Mapping("OS_UNK_046", "OS02-05 进程状态转换", "进程状态与转换归入进程状态转换。"),
    Mapping("OS_UNK_047", "OS02-05 进程状态转换", "进程状态与转换归入进程状态转换。"),
    Mapping("OS_UNK_048", "OS03-20 段式管理", "上下文直接考查段式存储管理中逻辑分段的确定时机。"),
    Mapping("OS_UNK_049", "OS03-20 段式管理", "上下文直接考查段式管理的可见性、段长和段表项。"),
    Mapping("OS_UNK_050", "OS03-22 段页式管理", "上下文直接考查段页式管理的段表与页表组织。"),
    Mapping("OS_UNK_051", "OS03-22 段页式管理", "上下文直接考查段页式中逻辑空间分段、物理空间分页。"),
    Mapping("OS_UNK_052", "OS03-23 虚拟内存基本概念", "上下文直接考查虚拟存储器最大容量与地址位数。"),
    Mapping("OS_UNK_053", "OS05-23 SPOOLing 技术", "上下文直接说明虚拟设备利用 SPOOLing 将独占设备虚拟为逻辑设备。"),
    Mapping("OS_UNK_055", "OS03-18 快表 TLB", "上下文直接考查 TLB 命中率下的平均访存次数。"),
    Mapping("OS_UNK_060", "OS03-01 内存管理基本概念", "内存管理的概念归入内存管理基本概念。"),
    Mapping("OS_UNK_061", "OS03-01 内存管理基本概念", "内存管理的概念归入内存管理基本概念。"),
    Mapping("OS_UNK_062", "OS03-01 内存管理基本概念", "内存管理的概念归入内存管理基本概念。"),
    Mapping("OS_UNK_065", "OS03-20 段式管理", "段式存储管理中的异常归入段式管理。"),
    Mapping("OS_UNK_072", "OS04-22 硬链接", "链接与引用计数归入硬链接。"),
    Mapping("OS_UNK_074", "OS04-25 外存空闲空间管理", "文件存储空间管理归入外存空闲空间管理。"),
    Mapping("OS_UNK_075", "OS04-28 成组链接法", "空闲盘块号栈归入成组链接法。"),
    Mapping("OS_UNK_081", "OS04-05 文件操作", "文件基本操作知识点回顾归入文件操作。"),
    Mapping("OS_UNK_083", "OS04-21 目录操作", "目录检索归入目录操作。"),
    Mapping("OS_UNK_084", "OS04-20 树形目录", "多级目录目的归入树形目录。"),
    Mapping("OS_UNK_085", "OS04-24 文件系统布局", "文件系统常识归入文件系统布局。"),
    Mapping("OS_UNK_089", "OS05-05 I/O 控制方式", "定位路径为 I/O 控制方式，内容考查通道、设备控制器和设备的控制关系。"),
    Mapping("OS_UNK_090", "OS05-09 I/O 软件层次结构", "定位路径为 I/O 软件层次结构，内容考查设备编号体系。"),
    Mapping("OS_UNK_092", "OS05-12 设备驱动程序", "设备寄存器写命令归入设备驱动程序。"),
    Mapping("OS_UNK_093", "OS05-13 中断处理程序", "输入设备中断处理软硬件分工归入中断处理程序。"),
    Mapping("OS_UNK_094", "OS05-11 设备独立性软件", "设备独立性归入设备独立性软件。"),
    Mapping("OS_UNK_101", "OS05-17 缓冲区管理", "缓冲技术应用场景归入缓冲区管理。"),
    Mapping("OS_UNK_103", "OS05-02 设备分类", "独占设备与共享设备归入设备分类。"),
    Mapping("OS_UNK_105", "OS05-23 SPOOLing 技术", "提高单机资源利用率的关键技术指向 SPOOLing。"),
    Mapping("OS_UNK_107", "OS05-23 SPOOLing 技术", "SPOOLing 系统组成归入 SPOOLing 技术。"),
    Mapping("OS_UNK_108", "OS05-23 SPOOLing 技术", "SPOOLing 系统基本条件归入 SPOOLing 技术。"),
    Mapping("OS_UNK_109", "OS05-23 SPOOLing 技术", "上下文直接说明以空间换取时间的技术为 SPOOLing。"),
    Mapping("OS_UNK_113", "OS05-25 磁盘访问时间", "磁盘旋转时间与扇区访问时间归入磁盘访问时间。"),
    Mapping("OS_UNK_114", "OS05-25 磁盘访问时间", "磁盘数据传输率计算归入磁盘访问时间。"),
    Mapping("OS_UNK_115", "OS05-24 磁盘结构", "上下文为外存介质记录密度、块间隙和利用率计算，当前标签表先归入磁盘结构。"),
    Mapping("OS_UNK_117", "OS05-24 磁盘结构", "扇区控制信息创建阶段归入磁盘结构。"),
    Mapping("OS_UNK_119", "OS05-25 磁盘访问时间", "旋转延迟影响因素归入磁盘访问时间。"),
    Mapping("OS_UNK_120", "OS05-26 先来先服务 FCFS 磁盘调度", "题目核心是 FCFS 磁盘调度可随请求顺序改变磁头方向。"),
]


def split_md_row(line: str) -> list[str]:
    return [cell.strip() for cell in line.strip().strip("|").split("|")]


def is_separator(line: str) -> bool:
    cells = split_md_row(line)
    return bool(cells) and all(re.fullmatch(r":?-{3,}:?", cell or "") for cell in cells)


def write_table(path: Path, title_lines: list[str], headers: list[str], rows: list[dict[str, str]]) -> None:
    lines = list(title_lines)
    if lines and lines[-1] != "":
        lines.append("")
    lines.append("|" + "|".join(headers) + "|")
    lines.append("|" + "|".join("---" for _ in headers) + "|")
    for row in rows:
        lines.append("|" + "|".join(row.get(header, "") for header in headers) + "|")
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def read_table_with_preamble(path: Path) -> tuple[list[str], list[str], list[dict[str, str]]]:
    lines = path.read_text(encoding="utf-8").splitlines()
    preamble: list[str] = []
    headers: list[str] = []
    rows: list[dict[str, str]] = []
    in_table = False
    for line in lines:
        if line.strip().startswith("|"):
            cells = split_md_row(line)
            if not in_table:
                headers = cells
                in_table = True
                continue
            if is_separator(line):
                continue
            if len(cells) < len(headers):
                cells.extend([""] * (len(headers) - len(cells)))
            rows.append(dict(zip(headers, cells)))
        elif not in_table:
            preamble.append(line)
    return preamble, headers, rows


def load_valid_labels(repo: Path) -> set[str]:
    labels: set[str] = set()
    for line in (repo / "知识点标签表.md").read_text(encoding="utf-8").splitlines():
        m = re.match(r"-\s*((?:DS|CO|OS|CN)\d{2}-\d{2}\s+.+)$", line.strip())
        if m:
            labels.add(m.group(1).strip())
    return labels


def update_node_rows(rows: list[dict[str, str]], mapping_by_id: dict[str, Mapping]) -> dict[str, str]:
    old_main_by_id: dict[str, str] = {}
    for row in rows:
        mapping = mapping_by_id.get(row.get("ID", ""))
        if not mapping:
            continue
        old_main = row.get("主知识点", "")
        old_hits = row.get("命中知识点", "")
        old_main_by_id[row["ID"]] = old_main
        row["主知识点"] = mapping.target
        if not KNOWLEDGE_CODE_RE.search(old_hits) or old_hits == old_main:
            row["命中知识点"] = mapping.target
    return old_main_by_id


def update_simple_table(path: Path, mapping_by_id: dict[str, Mapping], target_column: str = "主知识点") -> int:
    preamble, headers, rows = read_table_with_preamble(path)
    if not headers or target_column not in headers:
        return 0
    changed = 0
    for row in rows:
        mapping = mapping_by_id.get(row.get("ID", ""))
        if mapping and row.get(target_column) != mapping.target:
            row[target_column] = mapping.target
            changed += 1
    if changed:
        write_table(path, preamble, headers, rows)
    return changed


def update_year_index(path: Path, mapping_by_id: dict[str, Mapping]) -> int:
    lines = path.read_text(encoding="utf-8").splitlines()
    headers: list[str] = []
    changed = 0
    out: list[str] = []
    for line in lines:
        stripped = line.strip()
        if not stripped.startswith("|"):
            out.append(line)
            continue
        cells = split_md_row(line)
        if is_separator(line):
            out.append(line)
            continue
        if "ID" in cells and "主知识点" in cells:
            headers = cells
            out.append(line)
            continue
        if headers and len(cells) >= len(headers):
            row = dict(zip(headers, cells))
            mapping = mapping_by_id.get(row.get("ID", ""))
            if mapping and "主知识点" in headers:
                idx = headers.index("主知识点")
                if cells[idx] != mapping.target:
                    cells[idx] = mapping.target
                    changed += 1
                    out.append("|" + "|".join(cells) + "|")
                    continue
        out.append(line)
    if changed:
        path.write_text("\n".join(out) + "\n", encoding="utf-8")
    return changed


def update_relation_reasons(path: Path, old_main_by_id: dict[str, str], mapping_by_id: dict[str, Mapping]) -> int:
    preamble, headers, rows = read_table_with_preamble(path)
    if not headers or "关联原因" not in headers:
        return 0
    changed = 0
    for row in rows:
        endpoints = [row.get("起点ID", ""), row.get("终点ID", "")]
        reason = row.get("关联原因", "")
        new_reason = reason
        for node_id in endpoints:
            old = old_main_by_id.get(node_id)
            mapping = mapping_by_id.get(node_id)
            if old and mapping and old in new_reason:
                new_reason = new_reason.replace(old, mapping.target)
        if new_reason != reason:
            row["关联原因"] = new_reason
            changed += 1
    if changed:
        write_table(path, preamble, headers, rows)
    return changed


def regenerate_knowledge_index(path: Path, node_rows: list[dict[str, str]]) -> None:
    grouped: dict[str, list[dict[str, str]]] = defaultdict(list)
    for row in node_rows:
        hits = [hit.strip() for hit in row.get("命中知识点", "").split("；") if hit.strip()]
        for hit in hits:
            grouped[hit].append(row)

    def sort_key(item: tuple[str, list[dict[str, str]]]) -> tuple[str, str]:
        key = item[0]
        m = KNOWLEDGE_CODE_RE.search(key)
        return (m.group(0) if m else "ZZ" + key, key)

    lines = [
        "# 知识点命中索引",
        "",
        "本文件按 408 知识点聚合统一正式错题节点。历史归档题已映射为正式节点 ID；来源定位见 `历史错题归档/正式ID映射.md` 和 `历史错题归档/定位索引.md`。",
        "",
        "知识点命中不是错因。即使当前没有明确错因，只要节点明确考查、使用或提到某个知识点，也归入这里。",
        "",
    ]
    for knowledge, rows in sorted(grouped.items(), key=sort_key):
        rows = sorted(rows, key=lambda r: r.get("ID", ""))
        lines.extend(
            [
                f"### {knowledge}",
                "",
                f"- 节点数量：{len(rows)}",
                "",
                "#### 主知识点命中",
                "",
            ]
        )
        for row in rows:
            if row.get("主知识点") == knowledge:
                lines.append(
                    f"- {row.get('ID', '')}：{row.get('核心考点', '')}；最近复做日期：{row.get('最近复做日期', '')}。"
                )
        lines.extend(["", "#### 错因触发记录", ""])
        for row in rows:
            lines.append(f"- {row.get('ID', '')}：{row.get('错因标签', '')}。")
        lines.extend(["", "---", ""])
    path.write_text("\n".join(lines), encoding="utf-8")


def existing_report_rows(repo: Path) -> dict[str, tuple[str, str, str]]:
    report_path = repo / "导入报告/408-docx-2026-05-07知识点归一化记录.md"
    if not report_path.exists():
        return {}
    rows: dict[str, tuple[str, str, str]] = {}
    for row in read_markdown_table(report_path):
        node_id = row.get("正式节点ID", "")
        if node_id:
            rows[node_id] = (
                row.get("原主知识点", ""),
                row.get("新主知识点", ""),
                row.get("依据", ""),
            )
    return rows


def write_normalization_report(repo: Path, applied: list[tuple[Mapping, str]]) -> None:
    report_dir = repo / "导入报告"
    report_dir.mkdir(exist_ok=True)
    merged_rows = existing_report_rows(repo)
    for mapping, old_main in applied:
        merged_rows[mapping.node_id] = (old_main, mapping.target, mapping.reason)

    lines = [
        "# 408 Word 旧错题知识点归一化记录",
        "",
        f"- 生成日期：{date.today().isoformat()}",
        "- 范围：只处理本地规则可确认的高置信度历史节点。",
        "- 原则：只替换主知识点/命中知识点中的长路径或摘要为正式知识点编号；不补写题干、解析或图片。",
        "",
        "| 正式节点ID | 原主知识点 | 新主知识点 | 依据 |",
        "|---|---|---|---|",
    ]
    for node_id in sorted(merged_rows):
        old_main, target, reason = merged_rows[node_id]
        lines.append(f"|{node_id}|{old_main}|{target}|{reason}|")
    (report_dir / "408-docx-2026-05-07知识点归一化记录.md").write_text(
        "\n".join(lines) + "\n", encoding="utf-8"
    )


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--repo", default=".")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    repo = Path(args.repo).resolve()
    valid_labels = load_valid_labels(repo)
    mapping_by_id = {mapping.node_id: mapping for mapping in MAPPINGS}
    missing_labels = sorted({mapping.target for mapping in MAPPINGS if mapping.target not in valid_labels})
    if missing_labels:
        raise SystemExit("知识点标签表中缺少目标标签：\n" + "\n".join(missing_labels))

    preamble, headers, node_rows = read_table_with_preamble(repo / "节点总表.md")
    old_rows_by_id = {row["ID"]: dict(row) for row in node_rows}
    old_main_by_id = {}
    applied: list[tuple[Mapping, str]] = []
    for row in node_rows:
        mapping = mapping_by_id.get(row.get("ID", ""))
        if mapping:
            old_main = row.get("主知识点", "")
            if old_main != mapping.target:
                applied.append((mapping, old_main))
    if args.dry_run:
        for mapping, old_main in applied:
            print(f"{mapping.node_id}: {old_main} -> {mapping.target}")
        print(f"planned_updates={len(applied)}")
        return 0

    old_main_by_id = update_node_rows(node_rows, mapping_by_id)
    write_table(repo / "节点总表.md", preamble, headers, node_rows)

    subject_files = [
        "节点库/数据结构节点.md",
        "节点库/计算机组成原理节点.md",
        "节点库/操作系统节点.md",
        "节点库/计算机网络节点.md",
    ]
    subject_changes = sum(update_simple_table(repo / rel, mapping_by_id) for rel in subject_files)
    year_changes = update_year_index(repo / "年份索引.md", mapping_by_id)
    relation_changes = update_relation_reasons(repo / "关系边表.md", old_main_by_id, mapping_by_id)
    regenerate_knowledge_index(repo / "知识点命中索引.md", node_rows)
    write_normalization_report(repo, applied)

    print(f"applied_node_updates={len(applied)}")
    print(f"subject_view_updates={subject_changes}")
    print(f"year_index_updates={year_changes}")
    print(f"relation_reason_updates={relation_changes}")
    print("regenerated=知识点命中索引.md")
    print("wrote=导入报告/408-docx-2026-05-07知识点归一化记录.md")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
