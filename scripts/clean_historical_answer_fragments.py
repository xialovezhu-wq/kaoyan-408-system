#!/usr/bin/env python3
"""Mask answer-bearing fragments in 历史错题归档 files."""

from __future__ import annotations

import argparse
import re
from collections import Counter
from dataclasses import dataclass
from pathlib import Path


MASK = "答案信息已隐藏（复做保护）"

TARGET_FILES = [
    "历史错题归档/历史错题清单.md",
    "历史错题归档/按专题链索引.md",
    "历史错题归档/按知识点索引.md",
    "历史错题归档/按错因索引.md",
    "历史错题归档/定位索引.md",
    "历史错题归档/正式ID映射.md",
]


@dataclass(frozen=True)
class Rule:
    name: str
    pattern: re.Pattern[str]


ANSWER_TOKEN = r"(?:[A-Za-zＡ-Ｚａ-ｚ](?:\s*[、,，/＋+和及]\s*[A-Za-zＡ-Ｚａ-ｚ])*|[0-9０-９]+(?:\.[0-9０-９]+)?(?:\s*(?:ns|ms|s|KB|MB|GB|bit|Byte|B|Hz|MHz|GHz|bps|次|个|页|块|字节|位))?)"

RULES = [
    Rule("answer_label_with_value", re.compile(r"(?:我的错误选择|错误选择|我的错误答案|错选答案|我的选择|你的选择)[:：]\s*[^|/；;\n]+")),
    Rule("answer_missing_placeholder", re.compile(r"(?:我的错误答案/错选|我的错误答案|错误答案|正确答案|我的答案|错选|误选)(?:未明确记录|未记录|不明|缺失)")),
    Rule("correct_option_x", re.compile(rf"正确选项选\s*{ANSWER_TOKEN}")),
    Rule("answer_field", re.compile(rf"(?:正确答案|标准答案|错误答案|我的答案|我的选择|你的选择|正确选项|我的错误选择|错误选择|我的错误答案|错选答案)[:：]?\s*{ANSWER_TOKEN}")),
    Rule("wrong_choice_token", re.compile(rf"(?:错选|误选)(?:为|是|了)?[:：]?\s*{ANSWER_TOKEN}")),
    Rule("answer_is", re.compile(rf"(?:答案是|答案为)\s*{ANSWER_TOKEN}")),
    Rule("standalone_answer_label", re.compile(r"(?:正确答案|标准答案|错误答案|我的答案|我的选择|你的选择|正确选项|我的错误选择|错误选择|我的错误答案|错选答案)")),
]

RISK_RE = re.compile(
    r"标准答案|正确答案|错误答案|我的答案|我的选择|错选|误选|正确选项|你的选择|答案是|答案为"
)
SAFE_RE = re.compile(r"答案信息已隐藏（复做保护）|不保存|不输出|不写|禁止|已隐藏|复做保护|不复制|不得|脱敏|未写入|不暴露")


def mask_line(line: str) -> tuple[str, list[str]]:
    hits: list[str] = []
    masked = line
    for rule in RULES:
        masked, count = rule.pattern.subn(MASK, masked)
        hits.extend([rule.name] * count)
    return masked, hits


def table_row_count_text(text: str) -> int:
    header: list[str] | None = None
    count = 0
    for line in text.splitlines():
        if not line.startswith("|"):
            continue
        cells = [cell.strip() for cell in line.strip().strip("|").split("|")]
        if cells and cells[0] == "历史归档ID":
            header = cells
            continue
        if header and len(cells) == len(header) and set(cells[0]) - {"-", ":"}:
            count += 1
    return count


def table_row_count(path: Path) -> int:
    return table_row_count_text(path.read_text(encoding="utf-8"))


def locator_snapshot_text(text: str) -> dict[str, tuple[str, str]]:
    header: list[str] | None = None
    result: dict[str, tuple[str, str]] = {}
    for line in text.splitlines():
        if not line.startswith("|"):
            continue
        cells = [cell.strip() for cell in line.strip().strip("|").split("|")]
        if cells and cells[0] == "历史归档ID":
            header = cells
            continue
        if header and len(cells) == len(header) and set(cells[0]) - {"-", ":"}:
            row = dict(zip(header, cells))
            result[row["历史归档ID"]] = (row["定位标记"], row["段落号"])
    return result


def locator_snapshot(path: Path) -> dict[str, tuple[str, str]]:
    return locator_snapshot_text(path.read_text(encoding="utf-8"))


def residual_risk_text(text: str) -> list[int]:
    lines = []
    for lineno, line in enumerate(text.splitlines(), 1):
        if RISK_RE.search(line) and not SAFE_RE.search(line):
            lines.append(lineno)
    return lines


def run(repo: Path, apply: bool, report: Path | None) -> int:
    before_locator = locator_snapshot(repo / "历史错题归档/定位索引.md")
    before_counts = {
        "历史错题归档/历史错题清单.md": table_row_count(repo / "历史错题归档/历史错题清单.md"),
        "历史错题归档/正式ID映射.md": table_row_count(repo / "历史错题归档/正式ID映射.md"),
    }
    preview: list[tuple[str, int, str]] = []
    totals: Counter[str] = Counter()
    projected_text: dict[str, str] = {}

    for rel in TARGET_FILES:
        path = repo / rel
        lines = path.read_text(encoding="utf-8").splitlines()
        new_lines: list[str] = []
        changed = False
        for lineno, line in enumerate(lines, 1):
            masked, hits = mask_line(line)
            if hits:
                changed = True
                for hit in hits:
                    preview.append((rel, lineno, hit))
                    totals[hit] += 1
            new_lines.append(masked)
        new_text = "\n".join(new_lines) + "\n"
        projected_text[rel] = new_text
        if apply and changed:
            path.write_text(new_text, encoding="utf-8")

    if apply:
        after_locator = locator_snapshot(repo / "历史错题归档/定位索引.md")
    else:
        after_locator = locator_snapshot_text(projected_text["历史错题归档/定位索引.md"])
    locator_ok = before_locator == after_locator
    if apply:
        after_counts = {
            "历史错题归档/历史错题清单.md": table_row_count(repo / "历史错题归档/历史错题清单.md"),
            "历史错题归档/正式ID映射.md": table_row_count(repo / "历史错题归档/正式ID映射.md"),
        }
        residual = {rel: residual_risk_text((repo / rel).read_text(encoding="utf-8")) for rel in TARGET_FILES}
    else:
        after_counts = {
            "历史错题归档/历史错题清单.md": table_row_count_text(projected_text["历史错题归档/历史错题清单.md"]),
            "历史错题归档/正式ID映射.md": table_row_count_text(projected_text["历史错题归档/正式ID映射.md"]),
        }
        residual = {rel: residual_risk_text(projected_text[rel]) for rel in TARGET_FILES}

    print(f"mode={'apply' if apply else 'dry-run'}")
    print(f"match_count={len(preview)}")
    for name, count in sorted(totals.items()):
        print(f"{name}={count}")
    print("preview_first_20")
    for rel, lineno, category in preview[:20]:
        print(f"{rel}|{lineno}|{category}")
    print(f"locator_columns_unchanged={locator_ok}")
    print(f"row_counts={after_counts}")
    print("residual_risk_lines")
    for rel, lines in residual.items():
        if lines:
            print(f"{rel}:{','.join(map(str, lines))}")

    if report:
        report.parent.mkdir(parents=True, exist_ok=True)
        report.write_text(
            "# 2026-07-04 T4 历史归档清洗预览与结果\n\n"
            "本报告不复述任何被清洗的原文，只记录文件、行号和模式类别。\n\n"
            f"- 模式：{'apply' if apply else 'dry-run'}\n"
            f"- 匹配片段数：{len(preview)}\n"
            f"- 定位标记/段落号列保持不变：{locator_ok}\n"
            f"- 历史清单行数：{after_counts['历史错题归档/历史错题清单.md']}\n"
            f"- 正式ID映射行数：{after_counts['历史错题归档/正式ID映射.md']}\n"
            f"- 清洗前行数基准：{before_counts}\n\n"
            "## 模式统计\n\n"
            + "\n".join(f"- {name}: {count}" for name, count in sorted(totals.items()))
            + "\n\n## 前 20 条预览\n\n"
            + "\n".join(f"- {rel} | 行 {lineno} | {category}" for rel, lineno, category in preview[:20])
            + "\n\n## 遗留清单\n\n"
            + (
                "\n".join(
                    f"- {rel}: {','.join(map(str, lines))}"
                    for rel, lines in residual.items()
                    if lines
                )
                or "- 无"
            )
            + "\n",
            encoding="utf-8",
        )

    if not locator_ok:
        return 2
    if before_counts != after_counts:
        return 3
    return 0


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--repo", default=".", help="kaoyan-408 repository root")
    parser.add_argument("--apply", action="store_true", help="write masked files")
    parser.add_argument("--report", help="write preview/result report")
    args = parser.parse_args()
    return run(Path(args.repo), args.apply, Path(args.report) if args.report else None)


if __name__ == "__main__":
    raise SystemExit(main())
