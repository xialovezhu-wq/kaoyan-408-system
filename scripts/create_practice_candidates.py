#!/usr/bin/env python3
"""Create the isolated practice-candidate layer for T6."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path


ROOT = Path(".")
OUT = ROOT / "题库候选"
CARDS = OUT / "cards"
CREATED = "2026-07-04"


@dataclass(frozen=True)
class Candidate:
    tq_id: str
    subject: str
    target: str
    template: str
    skeleton: str
    ask_type: str
    error_target: str
    source: str
    check_entry: str


CORPUS_BASE = "/Users/your-user/Documents/kaoyan-408-details/corpus/CO03-存储系统/cards"

CANDIDATES: list[Candidate] = []  # Public export: private migration input intentionally omitted.


def card_text(candidate: Candidate) -> str:
    return f"""# {candidate.tq_id}

- TQ_ID：{candidate.tq_id}
- 科目：{candidate.subject}
- 目标知识点：{candidate.target}
- 题型模板：{candidate.template}
- 关键参数骨架：{candidate.skeleton}
- 问法类型：{candidate.ask_type}
- 针对错因：{candidate.error_target}
- 来源：{candidate.source}
- 自检入口：{candidate.check_entry}
- 状态：候选
- 创建日期：{CREATED}

## 使用边界

本卡只保存自测模板、参数骨架和来源指针。若用户真实做错并补齐日期、来源和唯一主知识点，才可按正式 408 入库流程转为正式节点。
"""


def table_text(candidates: list[Candidate]) -> str:
    lines = [
        "# 题库候选总表",
        "",
        "| TQ_ID | 科目 | 目标知识点 | 题型模板 | 关键参数骨架 | 问法类型 | 针对错因 | 来源 | 自检入口 | 状态 | 创建日期 |",
        "|---|---|---|---|---|---|---|---|---|---|---|",
    ]
    for candidate in candidates:
        lines.append(
            "| "
            + " | ".join(
                [
                    candidate.tq_id,
                    candidate.subject,
                    candidate.target,
                    candidate.template,
                    candidate.skeleton,
                    candidate.ask_type,
                    candidate.error_target,
                    candidate.source,
                    candidate.check_entry,
                    "候选",
                    CREATED,
                ]
            )
            + " |"
        )
    return "\n".join(lines) + "\n"


def readme_text() -> str:
    return """# 408 题库候选层

本目录是练习候选区，不是正式错题库。

## 硬隔离规则

- `TQ_*` 编号永不写入 `节点总表.md`、`关系边表.md`、`知识点命中索引.md` 或 `专题链/`。
- 候选卡不存答案，只存题型模板、关键参数骨架和自检入口。
- 候选卡不能作为正式错题节点、不能进入复习单元、不能建立正式关系边。
- 转正条件：用户真实做错、有日期、有定位来源、主知识点唯一，并且必须走 `AGENTS.md` 的完整入库流程。

## 目录结构

- `题库候选总表.md`：候选题总索引。
- `cards/`：一题一卡的自测模板。

## 状态

- 候选：仅可用于自测。
- 已练：用户做过，但未确认做错或不具备转正条件。
- 待转正：用户真实做错且证据足够，等待正式入库。
- 废弃：模板质量不够或来源不适合继续使用。
"""


def main() -> int:
    OUT.mkdir(exist_ok=True)
    CARDS.mkdir(exist_ok=True)
    (OUT / "README.md").write_text(readme_text(), encoding="utf-8")
    (OUT / "题库候选总表.md").write_text(table_text(CANDIDATES), encoding="utf-8")
    for candidate in CANDIDATES:
        (CARDS / f"{candidate.tq_id}.md").write_text(card_text(candidate), encoding="utf-8")
    print(f"created_candidates={len(CANDIDATES)}")
    print(f"output_dir={OUT}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
