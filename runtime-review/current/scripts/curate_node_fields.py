#!/usr/bin/env python3
"""逐题修复脏字段 + 重挂主知识点（2026-06-11 关系网重构 阶段2）。

修复对象（诊断详见 导入报告/2026-06-11关系网重构报告.md）：
- 核心考点为 DOCX 解析残渣（"错题回顾"/"我的答案"/"正确答案：…"/章节标题）的节点；
- 挂在"垃圾桶"主知识点（CN01-18 网络体系结构、OS02-36 锁、OS03-15 页）但
  历史归档定位面包屑指向更细粒度知识点的节点。

依据来源（不编造信息）：
- 历史错题归档/定位索引.md 的完整来源位置面包屑；
- 导入报告/408-docx-2026-05-07知识点归一化记录.md 中记录的 DOCX 上下文；
- 知识点标签表.md（所有目标主知识点必须在标签表中存在）。

联动更新：节点总表、年份索引（主知识点列）、节点库分科视图（整体重建）、
知识点命中索引（外科手术式迁移：保留手工维护的"命中知识点"交叉小节）。
关系边的关联原因/失效满团边在阶段3 curate_relation_edges.py 统一处理。

证据冲突的节点只改核心考点为"待人工核对：…"并列入报告待确认清单，不改主知识点。
"""
from __future__ import annotations

import argparse
import re
import sys
from dataclasses import dataclass
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from normalize_408_knowledge_points import (
    Mapping,
    load_valid_labels,
    read_table_with_preamble,
    update_year_index,
    write_table,
)


@dataclass(frozen=True)
class Curation:
    node_id: str
    new_kp: str | None      # None = 主知识点不变
    new_core: str | None    # None = 核心考点不变
    reason: str
    pending: bool = False   # True = 证据冲突，列入待人工确认清单


CURATIONS: list[Curation] = [
    # ---- CN01-18 垃圾桶分流（18 题中 17 题重挂，CN_UNK_013 确属体系结构概念保留）----
    Curation("CN_UNK_001", "CN01-22 OSI 七层模型", None, "面包屑：1.2 OSI 参考模型 1.2.4（11）；考查 OSI 模型描述"),
    Curation("CN_UNK_002", "CN01-22 OSI 七层模型", None, "面包屑：OSI 参考模型 1.2.4（13）；考查 OSI 物理层功能定位，属模型层功能"),
    Curation("CN_UNK_004", "CN01-22 OSI 七层模型", None, "面包屑：OSI 参考模型 1.2.4（17）；OSI 功能定位"),
    Curation("CN_UNK_006", "CN01-22 OSI 七层模型", None, "面包屑：OSI 参考模型 1.2.7（20）；表示层功能属 OSI 模型层功能"),
    Curation("CN_UNK_007", "CN01-27 封装与解封装", None, "面包屑：OSI 参考模型 1.2.7（22）；核心考点即数据封装过程"),
    Curation("CN_UNK_009", "CN01-22 OSI 七层模型", None, "面包屑：OSI 参考模型 1.2.7（28）；考查各层差错控制分布，属 OSI 层功能"),
    Curation("CN_UNK_010", "CN01-19 协议", None, "面包屑：OSI vs TCP/IP 1.2.4（10）；协议三要素为主、TCP 首部为载体"),
    Curation("CN_UNK_011", "CN01-20 接口", None, "面包屑：1.2.4（27）；SAP 服务访问点即层间接口概念"),
    Curation("CN_UNK_012", "CN01-08 分组交换", "三种交换方式（电路/报文/分组）的优缺点对比", "面包屑：1.1 概述/三种交换方式的优缺点 1.1.7（08）；原核心考点为'错题回顾'残渣"),
    Curation("CN_UNK_014", "CN01-02 互联网基本概念", None, "面包屑：1.2.4（25）；互联网核心技术属互联网基本概念"),
    Curation("CN_UNK_015", "CN01-01 计算机网络基本概念", "计算机网络的功能（考纲扩展）", "面包屑：1.1 概述/计算机网络的功能 1.1.7（03）；原核心考点为'+ 🌟 考纲扩展'残渣"),
    Curation("CN_UNK_016", "CN01-10 计算机网络性能指标", "计算机网络性能指标（源题1.1.7（10），细分待复核）", "面包屑：1.1 概述/计算机网络的性能指标；原核心考点'错题回顾'残渣"),
    Curation("CN_UNK_017", "CN01-10 计算机网络性能指标", "计算机网络性能指标（源题1.1.7（11），细分待复核）", "面包屑：1.1 概述/计算机网络的性能指标；原核心考点'错题回顾'残渣"),
    Curation("CN_UNK_018", "CN01-10 计算机网络性能指标", "计算机网络性能指标（源题1.1.7（12），细分待复核）", "面包屑：1.1 概述/计算机网络的性能指标；原核心考点'错题回顾'残渣"),
    Curation("CN_UNK_019", "CN01-10 计算机网络性能指标", "计算机网络性能指标（源题1.1.7（13），细分待复核）", "面包屑：1.1 概述/计算机网络的性能指标；原核心考点'错题回顾'残渣"),
    Curation("CN_UNK_022", "CN01-03 网络组成", "计算机网络的组成（源题1.1.7（04））", "面包屑：1.1 概述/计算机网络的组成；原核心考点'错题回顾'残渣"),
    Curation("CN_UNK_062", "CN04-33 SDN", None, "面包屑：第四章 网络层/4.1 网络层的功能 4.1（21）；SDN 有专项标签"),
    # ---- CN03 残渣核心考点修复 ----
    Curation("CN_UNK_047", None, "CSMA/CD 协议（2019 真题，源3.5.4（40））", "原核心考点'错题回顾（2019 真题）'残渣；面包屑：3.5.2 CSMA/CD 协议"),
    Curation("CN_UNK_050", None, "停止-等待协议（源题3.4.3（02），细分待复核）", "原核心考点'错题回顾'残渣；面包屑：3.4 停止-等待协议（S-W）"),
    Curation("CN_UNK_051", None, "停止-等待协议（源题3.4.3（03），细分待复核）", "原核心考点'错题回顾'残渣；面包屑：3.4 停止-等待协议（S-W）"),
    Curation("CN_UNK_052", "CN03-12 后退 N 帧 GBN", "后退 N 帧协议 GBN（源题3.4.3（08），细分待复核）", "面包屑：3.4/后退 N 帧协议（GBN）；原挂停止等待协议且核心考点为残渣"),
    Curation("CN_UNK_053", "CN03-12 后退 N 帧 GBN", "后退 N 帧协议 GBN（源题3.4.3（09），细分待复核）", "面包屑：3.4/后退 N 帧协议（GBN）；原挂流量控制且核心考点为残渣"),
    # ---- CO ----
    Curation("CO_2012_001", None, "I/O 接口的工作原理（2012 真题）", "原核心考点'我的错选：D'答案残渣；面包屑：I/O接口/I/O 接口的工作原理/2012年真题"),
    Curation("CO_2016_001", None, "I/O 接口的作用（2016 真题）", "原核心考点'SPOOLing 技术'与 CO06-06 错位；归一前主模块路径为 I/O接口/I/O 接口的作用"),
    Curation("CO_2023_004", None, "待人工核对：源路径为置换选择排序（2023 应用题），与主知识点 CO05-18 异常 冲突", "面包屑指向 DS 排序章置换选择排序应用题，与 CO 异常标签矛盾；原核心考点为答案残渣", pending=True),
    Curation("CO_UNK_069", "CO05-22 指令流水线", "指令流水线的基本概念与性能指标", "面包屑深层：拓展部分/指令流水线的基本概念性能指标 5.6.6（01）；原核心考点'正确答案'残渣、原挂 CO01-08 依据不足"),
    # ---- OS 同步互斥/死锁 ----
    Curation("OS_2016_002", None, "管程特性（2016 真题）", "面包屑：2.3.6 管程 2.3.8（56）；原核心考点为章标题；标签表无管程专项，主知识点维持 OS02-32"),
    Curation("OS_2016_003", "OS02-49 死锁检测", "处理死锁的基本方法（检测与解除，2016 真题）", "面包屑：2.4.4 死锁的检测和解除；原挂 OS02-36 锁"),
    Curation("OS_2019_001", None, "死锁判断与解除/预防/避免的边界（2019 真题）", "原核心考点为章标题；DOCX 归一化记录：考查死锁判断与解除/预防/避免边界"),
    Curation("OS_2020_001", None, "同步与互斥的基本概念（2020 真题）", "原核心考点为章标题；面包屑：2.3.1 同步与互斥的基本概念"),
    Curation("OS_UNK_015", None, "临界资源与临界区代码段的区分", "原核心考点为章标题；归一化记录：区分临界资源与访问临界资源的代码段"),
    Curation("OS_UNK_016", None, "临界资源与互斥访问的基本概念", "原核心考点为章标题；归一化记录上下文"),
    Curation("OS_UNK_017", None, "并发异步性与同步机制的必要性", "原核心考点为章标题；归一化记录上下文"),
    Curation("OS_UNK_018", None, "互斥锁的加锁顺序与死锁场景", "原核心考点为章标题；归一化记录：定位为互斥锁，内容围绕加锁顺序与死锁场景"),
    Curation("OS_UNK_019", None, "管程 signal 与信号量 V 操作的差异", "原核心考点为章标题；归一化记录上下文"),
    Curation("OS_UNK_020", "OS02-44 死锁基本概念", "死锁的概念（源题2.2.8（31））", "面包屑：2.4.1 死锁的概念；原挂 OS02-36 锁、原核心考点为章标题"),
    Curation("OS_UNK_021", None, "由资源总量与最大需求判断是否死锁", "原核心考点为章标题；归一化记录：用资源总量与最大需求判断系统是否会死锁"),
    Curation("OS_UNK_023", "OS02-44 死锁基本概念", "死锁的概念（源题2.4.6（32））", "面包屑：2.4.1 死锁的概念；原挂 OS02-36 锁、原核心考点为章标题"),
    Curation("OS_UNK_029", "OS02-46 死锁预防", "死锁预防（静态策略）（源题2.4.6（03））", "面包屑：2.4.2 死锁预防（静态策略）；原挂锁、核心考点为节标题"),
    Curation("OS_UNK_030", "OS02-46 死锁预防", "死锁预防（静态策略）（源题2.4.6（04））", "面包屑：2.4.2 死锁预防（静态策略）；原挂锁、核心考点为节标题"),
    Curation("OS_UNK_031", "OS02-49 死锁检测", "死锁的检测和解除（源题2.4.6（12））", "面包屑：2.4.4 死锁的检测和解除；原挂锁、核心考点为节标题"),
    # ---- OS 内存管理 ----
    Curation("OS_2019_006", None, "共享段表、进程私有段表项与共享段回收条件", "题图与定位括号标注为 2019 年真题；旧占位 ID 已按题图校准为 OS_2019_006"),
    Curation("OS_2019_002", None, "动态分区分配算法（顺序搜索，2019 真题）", "原核心考点'我的答案'残渣；面包屑：3.1.2 动态分区分配算法（顺序搜索分区）"),
    Curation("OS_2021_001", "OS03-32 改进型 Clock 置换", None, "面包屑：3.2.4 页面置换算法 3.2.11（54）；核心考点已明确为改进型CLOCK，原挂 OS03-15 页过粗"),
    Curation("OS_2023_002", None, "虚拟地址空间与虚拟内存基本概念（2023 真题）", "原核心考点'我的答案'残渣；面包屑：3.2.1 虚拟内存的基本概念"),
    Curation("OS_2023_003", None, "页框分配策略/工作集（源3.2.11（43），年份标注冲突待核）", "面包屑同时含 2023 应用题与 2011 年真题；原核心考点'我的答案'残渣", pending=True),
    Curation("OS_2024_001", "OS03-27 页面置换", "页面置换算法对比（2024 真题，源3.2.11（15），细分待复核）", "面包屑：3.2.4 页面置换算法（对比）；原核心考点'正确答案：C. n'残渣、原挂 OS03-15 页过粗"),
    Curation("OS_UNK_012", None, "计算机系统引导（源题1.6.2（11））", "原核心考点'我的错选：B'残渣；面包屑：第一章/计算机系统引导"),
    Curation("OS_UNK_048", None, "段式管理：逻辑分段的确定时机", "原核心考点为章标题；归一化记录：逻辑分段的确定时机"),
    Curation("OS_UNK_049", None, "段式管理的可见性、段长与段表项", "原核心考点为章标题；归一化记录上下文"),
    Curation("OS_UNK_050", None, "段页式管理的段表与页表组织", "原核心考点为章标题；归一化记录上下文"),
    Curation("OS_UNK_051", None, "段页式：逻辑空间分段、物理空间分页", "原核心考点为章标题；归一化记录上下文"),
    Curation("OS_UNK_052", None, "虚拟存储器最大容量与地址位数", "原核心考点为章标题；归一化记录上下文"),
    Curation("OS_UNK_054", "OS03-24 请求分页管理", "请求分页管理方式（源题3.2.11（1），细分待复核）", "面包屑：3.2.2 请求分页管理方式；原核心考点为答案残渣、原挂 OS03-15 页过粗"),
    Curation("OS_UNK_056", "OS03-24 请求分页管理", "请求分页管理方式（源题3.2.11（19），细分待复核）", "面包屑：3.2.2 请求分页管理方式；原核心考点为章标题、原挂 OS03-02 逻辑地址依据不足"),
    Curation("OS_UNK_057", "OS03-24 请求分页管理", "请求分页页表项扩充字段判断（源题3.2.11（23））", "面包屑：3.2.2 请求分页管理方式；原核心考点为章标题、原挂 OS03-15 页过粗"),
    Curation("OS_UNK_058", "OS03-24 请求分页管理", "请求分页管理方式（源题3.2.11（26），细分待复核）", "面包屑：3.2.2 请求分页管理方式；原核心考点为章标题"),
    Curation("OS_UNK_059", "OS03-27 页面置换", "页面置换算法对比（源题3.2.11（30），细分待复核）", "面包屑：3.2.4 页面置换算法（对比）；原核心考点为章标题、原挂 OS03-15 页过粗"),
    Curation("OS_UNK_063", "OS03-14 页式管理", None, "面包屑：3.1.3 基本分页存储管理；核心考点与页式管理一致，原挂 OS03-15 页过粗"),
    Curation("OS_UNK_088", "OS05-07 中断方式", None, "面包屑：第五章 5.1.6（18）；核心考点'中断驱动 I/O 的 CPU 占用计算'属 I/O 中断方式，原挂 OS03-15 页明显错章"),
    # ---- 补遗批次（2026-06-11 阶段4锚定时发现的错挂与漏网脏核心考点）----
    Curation("CN_UNK_061", "CN04-03 虚电路服务", None, "面包屑：第四章 网络层/4.1 网络层的功能 4.1（20）；核心考点'虚电路网络的特性'与原挂 CN01-12 带宽无关"),
    Curation("OS_UNK_112", "OS05-24 磁盘结构", None, "面包屑：第五章/5.3 磁盘和固态硬盘/磁盘的结构 5.3.6（16）；柱面-盘面-扇区属磁盘结构，原挂 OS03-03 物理地址错章"),
    Curation("CN_UNK_026", "CN02-01 物理层基本概念", None, "面包屑：第二章物理层/2.2 传输介质 2.2.3（11）；物理层接口四大特性属物理层基本概念，原挂 CN01-20 层间接口"),
    Curation("CO_UNK_041", "CO05-16 微指令", None, "面包屑：第五章 中央处理器/控制器/微指令的设计 5.4.4（06）；微指令格式与编码属 CO05 微指令，原挂 CO04-02 指令格式错章"),
    Curation("OS_UNK_004", "OS01-08 操作系统结构", "操作系统体系结构（源题1.6.2（05），细分待复核）", "面包屑：第一章/操作系统体系结构 1.6.2（05）；原挂 OS02-15 进程通信错章"),
    Curation("OS_UNK_005", "OS01-08 操作系统结构", "操作系统体系结构（源题1.6.2（06），细分待复核）", "面包屑：第一章/操作系统体系结构 1.6.2（06）；原挂 OS02-15 进程通信错章，原核心考点泄露错选答案（复做保护）"),
    Curation("OS_UNK_055", None, "TLB 命中率下的平均访存次数计算（源题3.2.11（13））", "原核心考点'正确思路'残渣；导入期归一化记录：考查 TLB 命中率下的平均访存次数"),
    Curation("OS_UNK_032", None, "用信号量机制实现互斥/同步/前驱关系（2010 真题，源2.3.8（50））", "原核心考点'我的误选：C（1，2）'泄露答案（复做保护）；面包屑：2.3.4 信号量"),
    Curation("OS_UNK_041", None, "进程创建与父子进程 PID 关系（源题2.1.8（21））", "原核心考点为错选项内容（复做保护）；面包屑：2.1 线程与进程/进程控制"),
    Curation("OS_2022_001", None, "进程调度的时机与方式（2022 真题）", "原核心考点'正确结论'残渣；面包屑：2.2 CPU调度/调度的实现/进程调度的时机"),
    Curation("CO_UNK_048", None, "微程序控制器的基本原理（源题5.4.4（14），细分待复核）", "原核心考点'正确知识点'残渣；面包屑：微程序控制器的基本原理"),
    Curation("CO_UNK_049", None, "微程序控制器的基本原理（源题5.4.4（16），细分待复核）", "原核心考点'关键知识点'残渣；面包屑：微程序控制器的基本原理"),
    Curation("OS_UNK_078", None, "文件的基本操作（源题4.1.8（3），细分待复核）", "原核心考点'知识点回顾'残渣；面包屑：第四章/文件的基本操作"),
    Curation("OS_UNK_081", None, "文件的基本操作（考纲要求，源题4.1.8（6））", "原核心考点'知识点回顾（考纲要求）'残渣；面包屑：第四章/文件的基本操作"),
    Curation("OS_2013_002", "OS02-29 优先级调度", "进程优先级设置的合理原则（2013 真题）", "原核心考点为题干残句'合理的进程优先级应为（ ）。'；题考优先级设置原则，较调度目标更精确"),
]

SUBJECT_VIEWS = {
    "数据结构": "节点库/数据结构节点.md",
    "计算机组成原理": "节点库/计算机组成原理节点.md",
    "操作系统": "节点库/操作系统节点.md",
    "计算机网络": "节点库/计算机网络节点.md",
}


def rebuild_subject_views(repo: Path, node_rows: list[dict[str, str]]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for subject, rel in SUBJECT_VIEWS.items():
        path = repo / rel
        preamble, headers, _ = read_table_with_preamble(path)
        rows = [r for r in node_rows if r.get("科目", "").strip() == subject]
        rows.sort(key=lambda r: r.get("ID", ""))
        view_rows = [{h: r.get(h, "") for h in headers} for r in rows]
        write_table(path, preamble, headers, view_rows)
        counts[rel] = len(view_rows)
    return counts


# ---------- 知识点命中索引：外科手术式迁移 ----------

@dataclass
class Section:
    kp: str
    lines: list[str]

    def code(self) -> str:
        m = re.match(r"([A-Z]{2}\d{2}-\d{2})", self.kp)
        return m.group(0) if m else "ZZ" + self.kp


def parse_index(path: Path) -> tuple[list[str], list[Section]]:
    lines = path.read_text(encoding="utf-8").splitlines()
    head: list[str] = []
    sections: list[Section] = []
    cur: Section | None = None
    for line in lines:
        m = re.match(r"^###\s+(.+)$", line)
        if m:
            cur = Section(m.group(1).strip(), [line])
            sections.append(cur)
            continue
        if cur is None:
            head.append(line)
        else:
            cur.lines.append(line)
    return head, sections


def block_bounds(sec: Section, block_title: str) -> tuple[int, int] | None:
    """返回 (块内首条目行号, 块结束行号) —— 即 #### 标题后的条目区间。"""
    start = None
    for i, line in enumerate(sec.lines):
        if line.strip() == f"#### {block_title}":
            start = i + 1
            break
    if start is None:
        return None
    end = start
    for j in range(start, len(sec.lines)):
        if sec.lines[j].startswith("####") or sec.lines[j].strip() == "---":
            break
        end = j + 1
    return (start, end)


def block_entries(sec: Section, block_title: str) -> list[str]:
    b = block_bounds(sec, block_title)
    if not b:
        return []
    return [l for l in sec.lines[b[0]:b[1]] if l.strip().startswith("- ")]


def remove_entry(sec: Section, block_title: str, node_id: str) -> str | None:
    b = block_bounds(sec, block_title)
    if not b:
        return None
    for i in range(b[0], b[1]):
        if re.match(rf"^-\s*{re.escape(node_id)}：", sec.lines[i].strip()):
            return sec.lines.pop(i)
    return None


def insert_entry(sec: Section, block_title: str, entry: str, node_id: str) -> None:
    b = block_bounds(sec, block_title)
    if not b:
        # 在 --- 前补一个块
        tail = len(sec.lines)
        for i, l in enumerate(sec.lines):
            if l.strip() == "---":
                tail = i
                break
        sec.lines[tail:tail] = [f"#### {block_title}", "", entry, ""]
        return
    start, end = b
    pos = end
    for i in range(start, end):
        m = re.match(r"^-\s*([A-Z]{2}_(?:\d{4}|UNK)_\d{3})：", sec.lines[i].strip())
        if m and m.group(1) > node_id:
            pos = i
            break
    sec.lines.insert(pos, entry)


def recount(sec: Section) -> None:
    ids: set[str] = set()
    for title in ("主知识点命中", "命中知识点"):
        for l in block_entries(sec, title):
            m = re.match(r"^-\s*([A-Z]{2}_(?:\d{4}|UNK)_\d{3})：", l.strip())
            if m:
                ids.add(m.group(1))
    for i, l in enumerate(sec.lines):
        if re.match(r"^-\s*节点数量：", l.strip()):
            sec.lines[i] = f"- 节点数量：{len(ids)}"
            return


def section_is_empty(sec: Section) -> bool:
    return not (block_entries(sec, "主知识点命中") or block_entries(sec, "命中知识点") or block_entries(sec, "错因触发记录"))


def new_section(kp: str) -> Section:
    return Section(kp, [f"### {kp}", "", "- 节点数量：0", "", "#### 主知识点命中", "", "#### 错因触发记录", "", "---", ""])


def normalize_section_blanks(sec: Section) -> None:
    out: list[str] = []
    prev_blank = False
    for l in sec.lines:
        blank = not l.strip()
        if blank and prev_blank:
            continue
        out.append(l)
        prev_blank = blank
    # 确保条目块与标题之间有空行
    fixed: list[str] = []
    for i, l in enumerate(out):
        fixed.append(l)
        if l.startswith("####") and i + 1 < len(out) and out[i + 1].strip().startswith("- "):
            fixed.append("")
    sec.lines = fixed


def apply_index_surgery(path: Path, moves: list[tuple[str, str, str]], core_edits: dict[str, tuple[str, str]],
                        nodes: dict[str, dict[str, str]]) -> tuple[int, int, list[str]]:
    """moves: (node_id, old_kp_full, new_kp_full)；core_edits: node_id -> (kp_full, new_core)。"""
    head, sections = parse_index(path)
    by_kp = {s.kp: s for s in sections}
    moved = 0
    edited = 0
    dropped: list[str] = []

    for node_id, old_kp, new_kp in moves:
        old_sec = by_kp.get(old_kp)
        main_line = remove_entry(old_sec, "主知识点命中", node_id) if old_sec else None
        err_line = remove_entry(old_sec, "错因触发记录", node_id) if old_sec else None
        if new_kp not in by_kp:
            sec = new_section(new_kp)
            sections.append(sec)
            by_kp[new_kp] = sec
        new_sec = by_kp[new_kp]
        row = nodes[node_id]
        if main_line is None:
            main_line = f"- {node_id}：{row.get('核心考点','')}；最近复做日期：{row.get('最近复做日期','')}。"
        if node_id in core_edits:
            kept = re.search(r"；(最近复做日期：.*)$", main_line.strip())
            tail = kept.group(1) if kept else f"最近复做日期：{row.get('最近复做日期','')}。"
            main_line = f"- {node_id}：{core_edits[node_id][1]}；{tail}"
        if err_line is None:
            err_line = f"- {node_id}：{row.get('错因标签','')}。"
        insert_entry(new_sec, "主知识点命中", main_line, node_id)
        insert_entry(new_sec, "错因触发记录", err_line, node_id)
        moved += 1
        for sec in (old_sec, new_sec):
            if sec:
                recount(sec)

    for node_id, (kp_full, new_core) in core_edits.items():
        if any(node_id == m[0] for m in moves):
            continue  # 已在迁移时改写
        sec = by_kp.get(kp_full)
        if not sec:
            continue
        b = block_bounds(sec, "主知识点命中")
        if not b:
            continue
        for i in range(*b):
            stripped = sec.lines[i].strip()
            if re.match(rf"^-\s*{re.escape(node_id)}：", stripped):
                kept = re.search(r"；(最近复做日期：.*)$", stripped)
                tail = kept.group(1) if kept else "最近复做日期：未记录。"
                sec.lines[i] = f"- {node_id}：{new_core}；{tail}"
                edited += 1
                break

    kept_sections = []
    for sec in sections:
        if section_is_empty(sec):
            dropped.append(sec.kp)
            continue
        normalize_section_blanks(sec)
        kept_sections.append(sec)
    kept_sections.sort(key=lambda s: s.code())

    out = list(head)
    while out and not out[-1].strip():
        out.pop()
    out.append("")
    for sec in kept_sections:
        if sec.lines and sec.lines[-1].strip() == "":
            sec_lines = sec.lines
        else:
            sec_lines = sec.lines + [""]
        out.extend(sec_lines)
    path.write_text("\n".join(out).rstrip("\n") + "\n", encoding="utf-8")
    return moved, edited, dropped


def write_report(repo: Path, applied: list[tuple[Curation, str, str]], dropped_sections: list[str]) -> None:
    lines = [
        "# 2026-06-11 节点字段修复对照表（阶段2）",
        "",
        f"- 生成日期：{date.today().isoformat()}",
        "- 依据：历史错题归档/定位索引.md 面包屑、DOCX 导入期知识点归一化记录；不编造题目内容。",
        "- 同步范围：节点总表、年份索引、节点库分科视图（整体重建）、知识点命中索引（条目迁移）。",
        "- 关系边的满团重建与关联原因更新在阶段3处理。",
        "",
        "## 一、字段修复对照",
        "",
        "| ID | 旧主知识点 | 新主知识点 | 旧核心考点 | 新核心考点 | 依据 |",
        "|---|---|---|---|---|---|",
    ]
    for cur, old_kp, old_core in applied:
        new_kp = cur.new_kp or "（不变）"
        new_core = cur.new_core or "（不变）"
        lines.append(f"|{cur.node_id}|{old_kp}|{new_kp}|{old_core}|{new_core}|{cur.reason}|")
    lines += [
        "",
        "## 二、待人工确认清单（证据冲突，未改主知识点）",
        "",
    ]
    for cur, old_kp, old_core in applied:
        if cur.pending:
            lines.append(f"- {cur.node_id}：{cur.reason}")
    lines += [
        "",
        "## 三、保留判定",
        "",
        "- CN_UNK_013：面包屑为 1.2 计算机网络的分层结构，确属网络体系结构总概念，保留 CN01-18。",
        "",
        "## 四、命中索引清理",
        "",
        f"- 迁移后空分节已删除：{('、'.join(dropped_sections)) if dropped_sections else '无'}",
    ]
    (repo / "导入报告/2026-06-11节点字段修复对照表.md").write_text("\n".join(lines) + "\n", encoding="utf-8")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--repo", default=".")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()
    repo = Path(args.repo).resolve()

    valid = load_valid_labels(repo)
    bad_targets = sorted({c.new_kp for c in CURATIONS if c.new_kp and c.new_kp not in valid})
    if bad_targets:
        raise SystemExit("目标主知识点不在知识点标签表中：\n" + "\n".join(bad_targets))

    preamble, headers, rows = read_table_with_preamble(repo / "节点总表.md")
    nodes = {r["ID"]: r for r in rows}
    missing = [c.node_id for c in CURATIONS if c.node_id not in nodes]
    if missing:
        raise SystemExit(f"节点不存在：{missing}")

    applied: list[tuple[Curation, str, str]] = []
    moves: list[tuple[str, str, str]] = []
    core_edits: dict[str, tuple[str, str]] = {}
    kp_mappings: dict[str, Mapping] = {}

    for c in CURATIONS:
        row = nodes[c.node_id]
        old_kp = row.get("主知识点", "").strip()
        old_core = row.get("核心考点", "").strip()
        applied.append((c, old_kp, old_core))
        if c.new_kp and c.new_kp != old_kp:
            moves.append((c.node_id, old_kp, c.new_kp))
            kp_mappings[c.node_id] = Mapping(c.node_id, c.new_kp, c.reason)
        if c.new_core and c.new_core != old_core:
            core_edits[c.node_id] = (c.new_kp or old_kp, c.new_core)

    if args.dry_run:
        for c, old_kp, old_core in applied:
            kp_part = f" KP[{old_kp}]->[{c.new_kp}]" if c.new_kp and c.new_kp != old_kp else ""
            core_part = f" 核心[{old_core[:24]}]->[{(c.new_core or '')[:36]}]" if c.node_id in core_edits else ""
            flag = " [待人工确认]" if c.pending else ""
            print(f"{c.node_id}:{kp_part}{core_part}{flag}")
        print(f"planned: kp_moves={len(moves)} core_edits={len(core_edits)} pending={sum(1 for c in CURATIONS if c.pending)}")
        return 0

    # 1. 节点总表
    for c in CURATIONS:
        row = nodes[c.node_id]
        old_kp = row.get("主知识点", "").strip()
        if c.new_kp and c.new_kp != old_kp:
            hits = row.get("命中知识点", "")
            row["命中知识点"] = hits.replace(old_kp, c.new_kp) if old_kp and old_kp in hits else (c.new_kp if not hits.strip() else hits)
            row["主知识点"] = c.new_kp
        if c.new_core:
            row["核心考点"] = c.new_core
    write_table(repo / "节点总表.md", preamble, headers, rows)

    # 2. 年份索引（主知识点列）
    year_changes = update_year_index(repo / "年份索引.md", kp_mappings)

    # 3. 节点库分科视图整体重建
    view_counts = rebuild_subject_views(repo, rows)

    # 4. 知识点命中索引外科迁移
    moved, edited, dropped = apply_index_surgery(repo / "知识点命中索引.md", moves, core_edits, nodes)

    # 5. 报告
    write_report(repo, applied, dropped)

    print(f"node_field_updates={len(applied)} (kp_moves={len(moves)}, core_edits={len(core_edits)})")
    print(f"year_index_updates={year_changes}")
    print(f"subject_views_rebuilt={view_counts}")
    print(f"hit_index: moved={moved} core_edited={edited} dropped_sections={dropped}")
    print("wrote=导入报告/2026-06-11节点字段修复对照表.md")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
