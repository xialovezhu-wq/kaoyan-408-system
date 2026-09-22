#!/usr/bin/env python3
"""Refresh only question/solution image counts in the details manifest.

The details asset directories are the evidence truth for these two projection
columns.  All other manifest fields are preserved byte-for-byte where their
row does not change, and semantically unchanged where a count row is rewritten.
Backup or quarantine filenames such as ``question-01-original.png`` are not
counted as current surfaces.
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import io
import json
import os
import re
import tempfile
from pathlib import Path
from typing import Iterable


EXPECTED_HEADER = [
    "node_id",
    "subject",
    "source_id",
    "link",
    "question_images",
    "solution_images",
    "has_solution_text",
    "match_reason",
]
QUESTION_RE = re.compile(r"^question-\d{2}\.(?:png|jpe?g|webp)$", re.IGNORECASE)
SOLUTION_RE = re.compile(r"^solution-\d{2}\.(?:png|jpe?g|webp)$", re.IGNORECASE)


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def newline_suffix(raw_line: bytes) -> bytes:
    if raw_line.endswith(b"\r\n"):
        return b"\r\n"
    if raw_line.endswith(b"\n"):
        return b"\n"
    if raw_line.endswith(b"\r"):
        return b"\r"
    return b""


def parse_single_record(raw_line: bytes, line_number: int) -> list[str]:
    newline = newline_suffix(raw_line)
    body = raw_line[: -len(newline)] if newline else raw_line
    try:
        text = body.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise ValueError(f"manifest line {line_number} is not UTF-8") from exc
    rows = list(csv.reader([text]))
    if len(rows) != 1 or len(rows[0]) != len(EXPECTED_HEADER):
        raise ValueError(
            f"manifest line {line_number} must contain exactly "
            f"{len(EXPECTED_HEADER)} columns"
        )
    return rows[0]


def render_single_record(row: Iterable[str], newline: bytes) -> bytes:
    sink = io.StringIO(newline="")
    writer = csv.writer(sink, lineterminator="")
    writer.writerow(list(row))
    return sink.getvalue().encode("utf-8") + newline


def current_counts(asset_dir: Path) -> tuple[int, int]:
    if not asset_dir.is_dir():
        return 0, 0
    names = [entry.name for entry in asset_dir.iterdir() if entry.is_file()]
    return (
        sum(1 for name in names if QUESTION_RE.fullmatch(name)),
        sum(1 for name in names if SOLUTION_RE.fullmatch(name)),
    )


def build_refresh(manifest: Path, assets_root: Path) -> tuple[bytes, dict[str, object]]:
    before = manifest.read_bytes()
    lines = before.splitlines(keepends=True)
    if not lines:
        raise ValueError("details manifest is empty")

    header = parse_single_record(lines[0], 1)
    if header != EXPECTED_HEADER:
        raise ValueError(f"unexpected details manifest header: {header!r}")

    output: list[bytes] = [lines[0]]
    changes: list[dict[str, object]] = []
    seen: set[str] = set()

    for line_number, raw_line in enumerate(lines[1:], start=2):
        if not raw_line.strip():
            output.append(raw_line)
            continue
        row = parse_single_record(raw_line, line_number)
        node_id = row[0].strip()
        if not node_id:
            raise ValueError(f"manifest line {line_number} has an empty node_id")
        if node_id in seen:
            raise ValueError(f"duplicate node_id in details manifest: {node_id}")
        seen.add(node_id)

        expected_question, expected_solution = current_counts(assets_root / node_id)
        try:
            current_question = int(row[4])
            current_solution = int(row[5])
        except ValueError as exc:
            raise ValueError(f"invalid image count at manifest line {line_number}") from exc

        if (current_question, current_solution) == (expected_question, expected_solution):
            output.append(raw_line)
            continue

        updated = list(row)
        updated[4] = str(expected_question)
        updated[5] = str(expected_solution)
        output.append(render_single_record(updated, newline_suffix(raw_line)))
        changes.append(
            {
                "node_id": node_id,
                "question_images_before": current_question,
                "question_images_after": expected_question,
                "solution_images_before": current_solution,
                "solution_images_after": expected_solution,
            }
        )

    after = b"".join(output)
    summary: dict[str, object] = {
        "manifest": str(manifest),
        "assets_root": str(assets_root),
        "row_count": len(seen),
        "changed_row_count": len(changes),
        "question_count_changes": sum(
            1
            for item in changes
            if item["question_images_before"] != item["question_images_after"]
        ),
        "solution_count_changes": sum(
            1
            for item in changes
            if item["solution_images_before"] != item["solution_images_after"]
        ),
        "before_sha256": sha256_bytes(before),
        "after_sha256": sha256_bytes(after),
        "changes": changes,
    }
    return after, summary


def atomic_write(target: Path, data: bytes) -> None:
    mode = target.stat().st_mode
    temporary_name: str | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="wb", prefix=f".{target.name}.", suffix=".tmp", dir=target.parent, delete=False
        ) as handle:
            temporary_name = handle.name
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(temporary_name, mode)
        os.replace(temporary_name, target)
        temporary_name = None
    finally:
        if temporary_name is not None:
            Path(temporary_name).unlink(missing_ok=True)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--details-root", type=Path, required=True)
    parser.add_argument("--apply", action="store_true")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    details_root = args.details_root.expanduser().resolve()
    manifest = details_root / "indexes" / "manifest.csv"
    assets_root = details_root / "assets"
    if not manifest.is_file():
        raise SystemExit(f"details manifest not found: {manifest}")
    if not assets_root.is_dir():
        raise SystemExit(f"details assets root not found: {assets_root}")

    after, summary = build_refresh(manifest, assets_root)
    summary["status"] = "DRY_RUN"
    if args.apply and summary["changed_row_count"]:
        atomic_write(manifest, after)
        verified_after, verified_summary = build_refresh(manifest, assets_root)
        if verified_summary["changed_row_count"] != 0 or verified_after != manifest.read_bytes():
            raise SystemExit("post-write details manifest verification failed")
        summary["status"] = "APPLIED"
        summary["verified_sha256"] = hashlib.sha256(manifest.read_bytes()).hexdigest()
    elif args.apply:
        summary["status"] = "ALREADY_CURRENT"

    print(json.dumps(summary, ensure_ascii=False, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
