#!/usr/bin/env python3
"""Hide answer-like text from question callouts on detail cards without images."""

from __future__ import annotations

import argparse
import csv
import re
from pathlib import Path


LEAK_RE = re.compile(r"(?:正确答案|答案[:：]|错选|我的答案|选项选|[A-D][.．、].*[✅❌])")
QUESTION_RE = re.compile(r"(> \[!question\] 题目\n)(.*?)(\n> \[!answer\]- 解析\n)", re.S)
ANSWER_RE = re.compile(r"(\n> \[!answer\]- 解析\n)(.*)\Z", re.S)


def quote_block(text: str) -> str:
    lines = text.strip("\n").splitlines()
    return "\n".join("> " + line if line else ">" for line in lines)


def unquote_block(text: str) -> str:
    out: list[str] = []
    for line in text.splitlines():
        if line.startswith("> "):
            out.append(line[2:])
        elif line == ">":
            out.append("")
        else:
            out.append(line)
    return "\n".join(out).strip()


def core_point(text: str, node_id: str) -> str:
    match = re.search(r"^核心考点：(.+)$", text, re.M)
    return match.group(1).strip() if match else node_id


def sanitize_card(card: Path, node_id: str) -> bool:
    text = card.read_text(encoding="utf-8")
    original_text = text
    qmatch = QUESTION_RE.search(text)
    if not qmatch:
        normalized = re.sub(r"([^\n])\n> \[!answer\]-", r"\1\n\n> [!answer]-", text)
        if normalized != original_text:
            card.write_text(normalized, encoding="utf-8")
            return True
        return False
    question_body = qmatch.group(2)
    if not LEAK_RE.search(question_body):
        normalized = re.sub(r"([^\n])\n> \[!answer\]-", r"\1\n\n> [!answer]-", text)
        if normalized != original_text:
            card.write_text(normalized, encoding="utf-8")
            return True
        return False

    original_question = unquote_block(question_body)
    safe_question = quote_block(
        f"{core_point(text, node_id)}\n\n题目图片待补充；题目文字含答案或解析痕迹，已放入折叠解析区。"
    )
    text = text[: qmatch.start(2)] + safe_question + text[qmatch.end(2) :]

    amatch = ANSWER_RE.search(text)
    if not amatch:
        return False
    answer_body = amatch.group(2)
    if "## 原题文字与解析（待补题图前隐藏）" not in answer_body:
        insert = quote_block(
            "\n## 原题文字与解析（待补题图前隐藏）\n\n" + original_question
        )
        answer_body = answer_body.rstrip() + "\n>\n" + insert + "\n"
        text = text[: amatch.start(2)] + answer_body

    text = re.sub(r"([^\n])\n> \[!answer\]-", r"\1\n\n> [!answer]-", text)
    if text != original_text:
        card.write_text(text, encoding="utf-8")
        return True
    return False


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--vault", default=Path("/Users/your-user/Documents/kaoyan-408-details"), type=Path)
    args = parser.parse_args()

    manifest = args.vault / "indexes" / "manifest.csv"
    changed: list[str] = []
    with manifest.open(encoding="utf-8", newline="") as fh:
        for row in csv.DictReader(fh):
            if int(row.get("question_images") or 0) != 0:
                continue
            node_id = row["node_id"]
            card = args.vault / "cards" / f"{node_id}.md"
            if card.exists() and sanitize_card(card, node_id):
                changed.append(node_id)

    print(f"sanitized={len(changed)}")
    for node_id in changed:
        print(f"OK\t{node_id}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
