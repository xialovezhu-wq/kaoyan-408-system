#!/usr/bin/env python3
"""Transactionally delete exactly one explicitly authorized formal 408 node.

The writer removes active projections while preserving historical audit files
and canonical exam-corpus evidence.  Every invocation is batch-size one and
uses the shared repository lock, WAL, compare-and-swap guards, and receipt
engine.  Deletion is a dry run unless ``--apply`` is present.
"""

from __future__ import annotations

import argparse
import csv
import datetime as dt
import hashlib
import io
import json
import re
import subprocess
import sys
from pathlib import Path
from typing import Any, Callable

sys.path.insert(0, str(Path(__file__).resolve().parent))

from intake_batch_apply_408 import (  # noqa: E402
    DEFAULT_RUNTIME_ROOT,
    BatchError,
    TransactionRolledBack,
    _assert_dry_run_wal_clean,
    _canonical_bytes,
    _commit_transaction,
    _read_json_object,
    _receipt_path,
    _sha256_bytes,
    recover_transactions,
)
from intake_lib_408 import FORMAL_ID_RE, NODE_LIB_FILE  # noqa: E402
from intake_lock_408 import RepoLock, RepoLockTimeout  # noqa: E402


SCHEMA = "formal_node_deletion_v1"
ACTOR = "408-formal-node-deletion-writer"
DEFAULT_DETAILS_ROOT = Path("/Users/your-user/Documents/kaoyan-408-details")
LEDGER = Path("wiki/study_vaults/408-full/state/formal-node-deletions.jsonl")


class DeletionError(RuntimeError):
    pass


def _sha_file(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _emit(value: dict[str, Any]) -> None:
    print(json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True))


def _cells(line: str) -> list[str] | None:
    stripped = line.strip()
    if not (stripped.startswith("|") and stripped.endswith("|")):
        return None
    return [cell.strip() for cell in stripped[1:-1].split("|")]


def _render_cells(cells: list[str]) -> str:
    return "|" + "|".join(cells) + "|"


def _remove_table_rows(text: str, predicate: Callable[[list[str]], bool]) -> tuple[str, int]:
    lines = text.splitlines()
    removed = 0
    output: list[str] = []
    for line in lines:
        cells = _cells(line)
        if cells is not None and predicate(cells):
            removed += 1
            continue
        output.append(line)
    return "\n".join(output) + ("\n" if text.endswith("\n") else ""), removed


def _remove_first_cell_row(text: str, node_id: str) -> tuple[str, int]:
    return _remove_table_rows(text, lambda cells: bool(cells) and cells[0] == node_id)


def _remove_bullets(text: str, node_id: str) -> tuple[str, int]:
    pattern = re.compile(rf"^\s*-\s*{re.escape(node_id)}\s*[：:]")
    lines = text.splitlines()
    kept = [line for line in lines if not pattern.match(line)]
    removed = len(lines) - len(kept)
    return "\n".join(kept) + ("\n" if text.endswith("\n") else ""), removed


def _clean_related_nodes(text: str, node_id: str) -> tuple[str, int]:
    changed = 0
    output: list[str] = []
    for line in text.splitlines():
        if node_id not in line:
            output.append(line)
            continue
        if "相关节点：" not in line:
            raise DeletionError(f"概念词典出现未识别引用：{line}")
        prefix, raw = line.split("相关节点：", 1)
        nodes = [item.strip() for item in raw.split("；") if item.strip()]
        target = re.compile(rf"^{re.escape(node_id)}(?:（[^）]*）|\([^)]*\))?$")
        filtered = [item for item in nodes if not target.fullmatch(item)]
        if len(filtered) == len(nodes):
            raise DeletionError(f"概念词典引用无法精确删除：{line}")
        output.append(prefix + "相关节点：" + ("；".join(filtered) if filtered else "无"))
        changed += 1
    result = "\n".join(output) + ("\n" if text.endswith("\n") else "")
    if node_id in result:
        raise DeletionError("概念词典仍残留目标 ID")
    return result, changed


def _clean_topic_chain(text: str, node_id: str) -> tuple[str, int]:
    changed = 0
    output: list[str] = []
    bullet = re.compile(rf"^\s*-\s*{re.escape(node_id)}\s*[：:]")
    for line in text.splitlines():
        if bullet.match(line):
            changed += 1
            continue
        if node_id in line and " → " in line:
            parts = [part.strip() for part in line.split(" → ")]
            filtered = [part for part in parts if not re.match(rf"^{re.escape(node_id)}(?:\s|$)", part)]
            if len(filtered) == len(parts):
                raise DeletionError(f"专题链箭头引用无法精确删除：{line}")
            output.append(" → ".join(filtered))
            changed += 1
            continue
        if node_id in line:
            raise DeletionError(f"专题链出现未识别引用：{line}")
        output.append(line)
    result = "\n".join(output) + ("\n" if text.endswith("\n") else "")
    return result, changed


def _remove_section(text: str, heading: str) -> tuple[str, int]:
    lines = text.splitlines()
    start = next((i for i, line in enumerate(lines) if line.strip() == heading), None)
    if start is None:
        return text, 0
    end = start + 1
    while end < len(lines) and not lines[end].startswith("## "):
        end += 1
    del lines[start:end]
    while start < len(lines) - 1 and not lines[start].strip() and not lines[start + 1].strip():
        del lines[start]
    return "\n".join(lines) + ("\n" if text.endswith("\n") else ""), 1


def _filter_inline_ids(line: str, node_id: str) -> str:
    ids = re.findall(r"`((?:DS|CO|OS|CN)_(?:\d{4}|UNK)_\d{3})`", line)
    if node_id not in ids:
        return line
    remaining = [item for item in ids if item != node_id]
    line = re.sub(rf"`{re.escape(node_id)}`(?:、)?", "", line)
    line = line.replace("（、", "（").replace("、、", "、")
    if "正式安全卡有但详情页缺失：" in line:
        line = re.sub(r"正式安全卡有但详情页缺失：\d+", f"正式安全卡有但详情页缺失：{len(remaining)}", line)
    if "个节点正式安全卡仍保留" in line:
        line = re.sub(r"\d+ 个节点正式安全卡仍保留", f"{len(remaining)} 个节点正式安全卡仍保留", line)
    return line


def _clean_image_worklist(text: str, node_id: str) -> tuple[str, int]:
    changed = 0
    output: list[str] = []
    decrements = {
        "- 详情页总数：": 1,
        "- 已有题图页：": 1,
    }
    for line in text.splitlines():
        original = line
        for prefix, delta in decrements.items():
            if line.startswith(prefix):
                line = re.sub(r"\d+", lambda m: str(int(m.group()) - delta), line, count=1)
        line = _filter_inline_ids(line, node_id)
        if line == original:
            output.append(line)
            continue
        changed += 1
        output.append(line)
    result = "\n".join(output) + ("\n" if text.endswith("\n") else "")
    if node_id in result:
        raise DeletionError("图片质量工作队列仍残留目标 ID")
    return result, changed


def _filter_line_csv(raw: bytes, column: str, node_id: str) -> tuple[bytes, int]:
    bom = raw.startswith(b"\xef\xbb\xbf")
    body = raw[3:] if bom else raw
    lines = body.splitlines(keepends=True)
    if not lines:
        raise DeletionError("CSV 为空")
    header = next(csv.reader([lines[0].decode("utf-8").rstrip("\r\n")]))
    try:
        index = header.index(column)
    except ValueError as exc:
        raise DeletionError(f"CSV 缺字段 {column}") from exc
    output = [lines[0]]
    removed = 0
    for raw_line in lines[1:]:
        if not raw_line.strip():
            output.append(raw_line)
            continue
        row = next(csv.reader([raw_line.decode("utf-8").rstrip("\r\n")]))
        if len(row) != len(header):
            raise DeletionError("单行 CSV 含多行字段，不能用 line filter")
        if row[index].strip() == node_id:
            removed += 1
        else:
            output.append(raw_line)
    return (b"\xef\xbb\xbf" if bom else b"") + b"".join(output), removed


def _filter_duplicate_clusters(raw: bytes, node_id: str) -> tuple[bytes, int]:
    bom = raw.startswith(b"\xef\xbb\xbf")
    body = raw[3:] if bom else raw
    lines = body.splitlines(keepends=True)
    header = next(csv.reader([lines[0].decode("utf-8").rstrip("\r\n")]))
    idx_ids, idx_count = header.index("formal_node_ids"), header.index("node_count")
    output = [lines[0]]
    changed = 0
    for raw_line in lines[1:]:
        row = next(csv.reader([raw_line.decode("utf-8").rstrip("\r\n")]))
        ids = [item for item in row[idx_ids].split("；") if item]
        if node_id not in ids:
            output.append(raw_line)
            continue
        remaining = [item for item in ids if item != node_id]
        changed += 1
        if len(remaining) < 2:
            continue
        row[idx_ids] = "；".join(remaining)
        row[idx_count] = str(len(remaining))
        sink = io.StringIO(newline="")
        csv.writer(sink, lineterminator="\n").writerow(row)
        output.append(sink.getvalue().encode("utf-8"))
    return (b"\xef\xbb\xbf" if bom else b"") + b"".join(output), changed


def _update_multiline_question_index(raw: bytes, node_id: str) -> tuple[bytes, int]:
    bom = raw.startswith(b"\xef\xbb\xbf")
    text = (raw[3:] if bom else raw).decode("utf-8")
    physical = text.splitlines(keepends=True)
    reader = csv.DictReader(io.StringIO(text, newline=""))
    if not reader.fieldnames or "formal_node_ids" not in reader.fieldnames:
        raise DeletionError("question-index 缺 formal_node_ids")
    output = physical[:1]
    previous = 1
    changed = 0
    for row in reader:
        end = reader.line_num
        raw_record = physical[previous:end]
        previous = end
        ids = [item for item in row["formal_node_ids"].split("；") if item]
        if node_id not in ids:
            output.extend(raw_record)
            continue
        remaining = [item for item in ids if item != node_id]
        row["formal_node_ids"] = "；".join(remaining)
        if not remaining:
            row["canonical_status"] = "canonical_new"
            row["duplicate_review_status"] = "no_exact_existing_match"
        sink = io.StringIO(newline="")
        csv.DictWriter(sink, fieldnames=reader.fieldnames, lineterminator="\n").writerow(row)
        output.append(sink.getvalue())
        changed += 1
    output.extend(physical[previous:])
    rendered = "".join(output).encode("utf-8")
    return (b"\xef\xbb\xbf" if bom else b"") + rendered, changed


def _update_corpus_card(text: str, node_id: str) -> tuple[str, int]:
    changed = 0
    output: list[str] = []
    for line in text.splitlines():
        if line.startswith("- 正式错题节点："):
            raw = line.split("：", 1)[1]
            ids = [] if raw == "未匹配" else [item for item in raw.split("；") if item]
            if node_id in ids:
                ids = [item for item in ids if item != node_id]
                line = "- 正式错题节点：" + ("；".join(ids) if ids else "未匹配")
                changed += 1
        output.append(line)
    return "\n".join(output) + ("\n" if text.endswith("\n") else ""), changed


def _card_fields(path: Path) -> dict[str, str]:
    fields: dict[str, str] = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        match = re.match(r"^- ([^：]+)：(.*)$", line)
        if match and match.group(1) not in fields:
            fields[match.group(1)] = match.group(2).strip()
    return fields


def _project_review_units(repo: Path, node_id: str, outputs: dict[Path, bytes], deletes: set[Path]) -> str:
    mapping_path = repo / "复习单元节点映射.md"
    mapping_text = mapping_path.read_text(encoding="utf-8")
    unit_id = ""
    for line in mapping_text.splitlines():
        cells = _cells(line)
        if cells and cells[0] == node_id:
            unit_id = cells[4]
            break
    if not unit_id:
        raise DeletionError(f"找不到 {node_id} 的主复习单元")
    mapping_after, removed = _remove_first_cell_row(mapping_text, node_id)
    if removed != 1:
        raise DeletionError(f"{node_id} 复习单元映射行数量异常：{removed}")
    outputs[mapping_path.resolve()] = mapping_after.encode("utf-8")

    track_path = repo / "原题复做轨总表.md"
    track_after, removed = _remove_first_cell_row(track_path.read_text(encoding="utf-8"), node_id)
    if removed != 1:
        raise DeletionError(f"{node_id} 原题轨行数量异常：{removed}")
    outputs[track_path.resolve()] = track_after.encode("utf-8")

    table_path = repo / "复习单元总表.md"
    text = table_path.read_text(encoding="utf-8")
    lines = text.splitlines()
    header: list[str] | None = None
    unit_index = -1
    unit_row: list[str] | None = None
    for index, line in enumerate(lines):
        cells = _cells(line)
        if cells and cells and cells[0] == "复习单元ID":
            header = cells
        elif cells and cells and cells[0] == unit_id:
            unit_index, unit_row = index, cells
            break
    if header is None or unit_row is None:
        raise DeletionError(f"复习单元总表缺 {unit_id}")
    row = dict(zip(header, unit_row))
    coverage = [item for item in row["覆盖节点"].split("、") if item]
    if node_id not in coverage:
        raise DeletionError(f"{unit_id} 覆盖节点不含 {node_id}")
    survivors = [item for item in coverage if item != node_id]
    card_path = (repo / "复习单元卡" / f"{unit_id}.md").resolve()
    if not survivors:
        del lines[unit_index]
        outputs[table_path.resolve()] = ("\n".join(lines) + ("\n" if text.endswith("\n") else "")).encode("utf-8")
        deletes.add(card_path)
        return unit_id

    criteria: list[str] = []
    mechanisms: list[str] = []
    for survivor in survivors:
        fields = _card_fields(repo / f"{survivor}.md")
        required = ["主知识点", "安全题目摘要", "关键题设参数", "用户错误入口", "复做第一动作"]
        missing = [label for label in required if not fields.get(label)]
        if missing:
            raise DeletionError(f"共享单元幸存节点 {survivor} 缺字段：{missing}")
        mechanism = fields.get("实际考查机制") or fields.get("核心考点") or fields["主知识点"]
        mechanisms.append(mechanism)
        criteria.extend([
            f"{survivor} 知识点：{fields['主知识点']}",
            f"{survivor} 安全摘要：{fields['安全题目摘要']}",
            f"{survivor} 关键参数核验：{fields['关键题设参数']}",
            f"{survivor} 错误入口核验：{fields['用户错误入口']}",
            f"{survivor} 第一动作核验：{fields['复做第一动作']}",
            f"{survivor} 机制边界：{mechanism}",
        ])
    prompt = "闭卷且不看原题、选项或答案：围绕“" + "；".join(mechanisms) + "”，先说出第一动作，再说明关键边界和完整判断链。"
    feedback = "核心机制：" + "；".join(mechanisms) + "；达标：" + "；".join(criteria)
    row["覆盖节点"] = "、".join(survivors)
    row["闭卷问题"] = prompt
    row["派生自查口径"] = feedback
    lines[unit_index] = _render_cells([row[name] for name in header])
    outputs[table_path.resolve()] = ("\n".join(lines) + ("\n" if text.endswith("\n") else "")).encode("utf-8")

    old_card = card_path.read_text(encoding="utf-8")
    legacy = "无"
    for line in old_card.splitlines():
        if line.startswith("- 旧复习单元："):
            legacy = line.split("：", 1)[1].strip()
            break
    if node_id in legacy:
        legacy = "历史单元已归档"
    card_lines = [
        "---", "schema: review_unit_card_v2", f"review_unit_id: {unit_id}",
        f"subject: {row['科目']}", f"module: {row['模块']}",
        f"schedule_state: {row['调度状态']}", "---", "",
        f"# {unit_id} · {row['内容名称']}", "", f"- 覆盖正式节点：{'、'.join(survivors)}",
        f"- 旧复习单元：{legacy}", f"- 当前状态：{row['调度状态']}", "", "## 闭卷提取", "",
        prompt, "", "先独立作答并记录置信度、反应时与是否使用提示，再展开下方自查口径。", "",
        "> [!answer]- 派生自查口径（首答后展开）", f"> 核心机制：{'；'.join(mechanisms)}", ">", "> 达标标准：",
    ]
    card_lines.extend(f"> - {item}" for item in criteria)
    card_lines.extend([
        "", "## 迁移生成规则", "",
        "更换表面情境、参数或问法，但保持同一底层机制；先说明识别信号与第一动作，再完成边界判断。迁移通过与保持通过分开记录。",
        "", "## 调度与证据边界", "",
        f"- 迁移规则：{row['备注']}",
        "- 当日到期集合必须全覆盖；批次只组织顺序，不删除项目。",
        "- 同日反馈后重答成功不冒充跨日保持成功。",
        "- 未经用户明确给出 0–5 分，不更新正式掌握度。",
        "- 原题复做轨另行执行 7 天外硬筛选；本卡不保存完整题干、答案或解析。", "",
    ])
    outputs[card_path] = "\n".join(card_lines).encode("utf-8")
    return unit_id


def _append_ledger(repo: Path, record: dict[str, Any], outputs: dict[Path, bytes]) -> None:
    path = (repo / LEDGER).resolve()
    before = path.read_bytes() if path.exists() else b""
    line = json.dumps(record, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8") + b"\n"
    outputs[path] = before + line


def _project(repo: Path, details: Path, node_id: str, identity: dict[str, Any]) -> tuple[dict[Path, bytes], set[Path], dict[str, Any]]:
    outputs: dict[Path, bytes] = {}
    deletes: set[Path] = set()
    master_path = repo / "节点总表.md"
    master = master_path.read_text(encoding="utf-8")
    master_row: list[str] | None = None
    for line in master.splitlines():
        cells = _cells(line)
        if cells and cells[0] == node_id:
            master_row = cells
            break
    if master_row is None or len(master_row) < 16:
        raise DeletionError(f"正式节点不存在或主行损坏：{node_id}")
    subject_prefix = node_id.split("_", 1)[0]
    master_after, removed = _remove_first_cell_row(master, node_id)
    if removed != 1:
        raise DeletionError(f"节点总表目标行数量异常：{removed}")
    count = sum(1 for line in master_after.splitlines() if (cells := _cells(line)) and FORMAL_ID_RE.fullmatch(cells[0]))
    master_after, replacements = re.subn(r"(共\s*)\d+(\s*条)", rf"\g<1>{count}\g<2>", master_after, count=1)
    if replacements != 1:
        raise DeletionError("节点总表总数说明未更新")
    outputs[master_path.resolve()] = master_after.encode("utf-8")

    keyed_files = [
        repo / NODE_LIB_FILE[subject_prefix], repo / "年份索引.md", repo / "错题日期索引.md",
    ]
    for path in keyed_files:
        after, n = _remove_first_cell_row(path.read_text(encoding="utf-8"), node_id)
        if n != 1:
            raise DeletionError(f"{path.name} 目标行数量异常：{n}")
        outputs[path.resolve()] = after.encode("utf-8")

    hit_path = repo / "知识点命中索引.md"
    hit_after, hit_count = _remove_bullets(hit_path.read_text(encoding="utf-8"), node_id)
    if not hit_count:
        raise DeletionError("知识点命中索引未找到目标")
    outputs[hit_path.resolve()] = hit_after.encode("utf-8")

    edge_path = repo / "关系边表.md"
    edge_after, edge_count = _remove_table_rows(
        edge_path.read_text(encoding="utf-8"),
        lambda cells: len(cells) >= 2 and node_id in {cells[0], cells[1]},
    )
    if edge_count:
        outputs[edge_path.resolve()] = edge_after.encode("utf-8")

    concept_path = repo / "概念词典.md"
    concept_after, concept_count = _clean_related_nodes(concept_path.read_text(encoding="utf-8"), node_id)
    if concept_count:
        outputs[concept_path.resolve()] = concept_after.encode("utf-8")

    for path in sorted((repo / "专题链").glob("*.md")):
        if node_id not in path.read_text(encoding="utf-8"):
            continue
        after, n = _clean_topic_chain(path.read_text(encoding="utf-8"), node_id)
        if n:
            outputs[path.resolve()] = after.encode("utf-8")

    mapping_path = repo / "历史错题归档/正式ID映射.md"
    mapping_after, mapping_count = _remove_table_rows(
        mapping_path.read_text(encoding="utf-8"),
        lambda cells: len(cells) >= 2 and cells[1] == node_id,
    )
    if mapping_count:
        outputs[mapping_path.resolve()] = mapping_after.encode("utf-8")

    for path in sorted((repo / "wiki/concepts").glob("*.md")):
        if node_id not in path.read_text(encoding="utf-8"):
            continue
        after, n = _remove_first_cell_row(path.read_text(encoding="utf-8"), node_id)
        if n != 1:
            raise DeletionError(f"wiki concept 引用无法精确删除：{path}")
        outputs[path.resolve()] = after.encode("utf-8")

    unit_id = _project_review_units(repo, node_id, outputs, deletes)
    deletes.add((repo / f"{node_id}.md").resolve())

    audit_path = repo / "scripts/audit_extended.py"
    audit_lines = audit_path.read_text(encoding="utf-8").splitlines()
    filtered = [line for line in audit_lines if line.strip() != f'"{node_id}",']
    if len(filtered) != len(audit_lines):
        outputs[audit_path.resolve()] = ("\n".join(filtered) + "\n").encode("utf-8")

    details_card = (details / "cards" / f"{node_id}.md").resolve()
    deletes.add(details_card)
    asset_dir = (details / "assets" / node_id).resolve()
    if not asset_dir.is_dir() or asset_dir.is_symlink():
        raise DeletionError(f"节点资产目录不存在或非法：{asset_dir}")
    for entry in asset_dir.iterdir():
        if not entry.is_file() or entry.is_symlink():
            raise DeletionError(f"节点资产目录含非普通文件：{entry}")
        deletes.add(entry.resolve())

    manifest_path = details / "indexes/manifest.csv"
    manifest_after, n = _filter_line_csv(manifest_path.read_bytes(), "node_id", node_id)
    if n != 1:
        raise DeletionError(f"details manifest 目标行数量异常：{n}")
    outputs[manifest_path.resolve()] = manifest_after

    by_subject = details / "indexes/by-subject.md"
    by_after, _ = _remove_bullets(by_subject.read_text(encoding="utf-8"), node_id)
    if by_after != by_subject.read_text(encoding="utf-8"):
        outputs[by_subject.resolve()] = by_after.encode("utf-8")

    needs = details / "indexes/needs-review.md"
    needs_after, _ = _remove_section(needs.read_text(encoding="utf-8"), f"## {node_id}")
    if needs_after != needs.read_text(encoding="utf-8"):
        outputs[needs.resolve()] = needs_after.encode("utf-8")

    for name in ("manual-migration-queue.md", "manual-migration-shortlist.md"):
        path = details / "indexes" / name
        after, _ = _remove_first_cell_row(path.read_text(encoding="utf-8"), node_id)
        if after != path.read_text(encoding="utf-8"):
            outputs[path.resolve()] = after.encode("utf-8")

    worklist = details / "indexes/image-quality-worklist.md"
    work_after, work_count = _clean_image_worklist(worklist.read_text(encoding="utf-8"), node_id)
    if work_count:
        outputs[worklist.resolve()] = work_after.encode("utf-8")

    corpus = details / "corpus/408统考真题"
    crosswalk = corpus / "indexes/formal-node-crosswalk.csv"
    cross_after, n = _filter_line_csv(crosswalk.read_bytes(), "formal_node_id", node_id)
    if n != 1:
        raise DeletionError(f"formal crosswalk 目标行数量异常：{n}")
    outputs[crosswalk.resolve()] = cross_after

    links = corpus / "indexes/wrong-question-knowledge-links.csv"
    links_after, n = _filter_line_csv(links.read_bytes(), "formal_node_id", node_id)
    if n != 1:
        raise DeletionError(f"wrong-question knowledge link 目标行数量异常：{n}")
    outputs[links.resolve()] = links_after

    clusters = corpus / "indexes/formal-duplicate-clusters.csv"
    cluster_after, cluster_count = _filter_duplicate_clusters(clusters.read_bytes(), node_id)
    if cluster_count:
        outputs[clusters.resolve()] = cluster_after

    question_index = corpus / "indexes/question-index.csv"
    question_after, n = _update_multiline_question_index(question_index.read_bytes(), node_id)
    if n != 1:
        raise DeletionError(f"question-index 目标记录数量异常：{n}")
    outputs[question_index.resolve()] = question_after

    corpus_cards_changed = 0
    for path in sorted((corpus / "cards").glob("*.md")):
        text = path.read_text(encoding="utf-8")
        if node_id not in text:
            continue
        after, n = _update_corpus_card(text, node_id)
        if n != 1:
            raise DeletionError(f"规范真题卡引用无法精确删除：{path}")
        outputs[path.resolve()] = after.encode("utf-8")
        corpus_cards_changed += 1
    if corpus_cards_changed != 1:
        raise DeletionError(f"规范真题卡命中数量异常：{corpus_cards_changed}")

    record = {
        "schema_version": SCHEMA,
        "node_id": node_id,
        "source_id": master_row[1],
        "year": master_row[2],
        "subject": master_row[3],
        "main_knowledge": master_row[5],
        "authorized_at": identity["authorized_at"],
        "authorization": identity["authorization"],
        "reason": identity["reason"],
        "idempotency_key": identity["idempotency_key"],
        "master_row_sha256": _sha256_bytes(_canonical_bytes(master_row)),
        "review_unit_id": unit_id,
    }
    _append_ledger(repo, record, outputs)

    outputs = {path: data for path, data in outputs.items() if not path.exists() or path.read_bytes() != data}
    missing_deletes = [path for path in deletes if not path.is_file() or path.is_symlink()]
    if missing_deletes:
        raise DeletionError(f"删除目标不是普通文件：{missing_deletes}")
    stats = {
        "node_id": node_id,
        "source_id": master_row[1],
        "review_unit_id": unit_id,
        "relation_rows_removed": edge_count,
        "knowledge_bullets_removed": hit_count,
        "concept_references_changed": concept_count,
        "historical_mapping_rows_removed": mapping_count,
        "replacement_files": len(outputs),
        "deleted_files": len(deletes),
        "formal_nodes_after": count,
    }
    return outputs, deletes, stats


def _expected_hashes(outputs: dict[Path, bytes], deletes: set[Path]) -> dict[Path, str | None]:
    return {path: (_sha_file(path) if path.exists() else None) for path in set(outputs) | deletes}


def _historical_skip(path: Path, root: Path) -> bool:
    rel = path.relative_to(root)
    skip_dirs = {"wiki/lint_reports", "导入报告", "复盘输出", "历史复习单元归档"}
    if any(str(rel).startswith(prefix + "/") for prefix in skip_dirs):
        return True
    if str(rel) in {"错题复做记录.md", "复习单元迁移记录.md", "wiki/log.md", str(LEDGER)}:
        return True
    return False


def _active_residuals(repo: Path, details: Path, node_id: str) -> list[str]:
    """Check live navigation, not immutable imports, evidence, backups or code."""
    paths = [p for p in repo.glob("*.md") if not _historical_skip(p, repo)]
    for folder in ("节点库", "专题链", "复习单元卡", "wiki/concepts"):
        paths.extend((repo / folder).glob("*.md"))
    paths.extend([repo / "历史错题归档/正式ID映射.md"])
    paths.extend((details / "cards").glob("*.md"))
    for name in ("manifest.csv", "by-subject.md", "needs-review.md",
                 "manual-migration-queue.md", "manual-migration-shortlist.md", "image-quality-worklist.md"):
        paths.append(details / "indexes" / name)
    corpus = details / "corpus/408统考真题"
    paths.extend((corpus / "cards").glob("*.md"))
    for name in ("formal-node-crosswalk.csv", "wrong-question-knowledge-links.csv",
                 "formal-duplicate-clusters.csv", "question-index.csv"):
        paths.append(corpus / "indexes" / name)
    residuals = [str(p) for p in paths if p.is_file() and node_id in p.read_text(encoding="utf-8-sig")]
    # Knowledge pages retain source evidence, but must not offer a deleted card.
    for p in (repo / "知识点").rglob("*.md"):
        text = p.read_text(encoding="utf-8")
        if f"[[{node_id}]]" in text or re.search(rf"\]\([^\n)]*(?:/|%2F){re.escape(node_id)}(?:[)#?]|$)", text):
            residuals.append(str(p))
    return residuals


def _run_closeout(repo: Path, details: Path, receipt_path: Path, review_date: str) -> list[dict[str, Any]]:
    receipt = _read_json_object(receipt_path)
    if receipt.get("actor") != ACTOR or receipt.get("status") != "COMMITTED":
        raise DeletionError("receipt 不属于正式节点删除 writer")
    journal = _read_json_object(Path(str(receipt.get("journal") or "")))
    identity = journal.get("payload_identity") or {}
    if identity.get("mode") != SCHEMA:
        raise DeletionError("deletion receipt payload mode 不匹配")
    for item in journal.get("files") or []:
        target = Path(item["target"])
        if item.get("operation", "replace") == "delete":
            if target.exists():
                raise DeletionError(f"delete after-state 验收失败：{target}")
        elif not target.is_file() or _sha_file(target) != item.get("after_sha256"):
            raise DeletionError(f"replace after hash 验收失败：{target}")
    node_id = str(identity["node_id"])
    asset_dir = details / "assets" / node_id
    if asset_dir.exists():
        try:
            asset_dir.rmdir()
        except OSError as exc:
            raise DeletionError(f"节点资产目录不是空目录，拒绝扩大删除：{asset_dir}") from exc

    commands = [
        [sys.executable, str(repo / "scripts/refresh_coverage_index.py"), "--repo", str(repo)],
        [sys.executable, str(repo / "scripts/build_obsidian_bridge_snapshot.py"), "--repo", str(repo), "--details-root", str(details)],
        [sys.executable, str(repo / "scripts/check_safe_node_cards.py"), "--repo", str(repo), "--list-missing"],
        [sys.executable, str(repo / "scripts/audit_network_health.py"), "--repo", str(repo)],
        [sys.executable, str(repo / "scripts/audit_extended.py"), "--repo", str(repo), "--details-root", str(details)],
        [sys.executable, str(repo / "scripts/audit_review_schedule.py"), "--repo", str(repo), "--date", review_date, "--json"],
        [sys.executable, str(repo / "scripts/intake_validate_408.py"), "--repo", str(repo), "--details-root", str(details), "--full", "--json"],
        [sys.executable, str(repo / "scripts/audit_true_exam_corpus.py")],
    ]
    results: list[dict[str, Any]] = []
    for command in commands:
        completed = subprocess.run(command, cwd=repo, capture_output=True, text=True)
        result = {
            "command": " ".join(map(str, command)),
            "returncode": completed.returncode,
            "stdout": completed.stdout[-2000:],
            "stderr": completed.stderr[-1200:],
        }
        results.append(result)
        if completed.returncode:
            raise DeletionError(
                "closeout 失败；事务已提交，禁止重新 apply，只能凭 receipt 重跑 closeout：\n"
                + result["command"] + "\n" + (completed.stdout or completed.stderr)[-2500:]
            )
    residuals = _active_residuals(repo, details, node_id)
    if residuals:
        raise DeletionError(f"活动表面仍残留 {node_id}：{residuals}")
    return results


def _identity(args: argparse.Namespace, repo: Path, details: Path) -> dict[str, Any]:
    if args.authorization != "user_explicit_delete":
        raise DeletionError("authorization 必须为 user_explicit_delete")
    try:
        parsed = dt.date.fromisoformat(args.authorized_at)
    except ValueError as exc:
        raise DeletionError("authorized-at 必须为 YYYY-MM-DD") from exc
    if parsed > dt.date.today():
        raise DeletionError("authorized-at 不能晚于今天")
    key = args.idempotency_key or f"{SCHEMA}:{args.node_id}:{args.authorized_at}"
    return {
        "mode": SCHEMA,
        "node_id": args.node_id,
        "authorization": args.authorization,
        "authorized_at": args.authorized_at,
        "reason": args.reason.strip(),
        "idempotency_key": key,
        "repo_root": str(repo),
        "details_root": str(details),
    }


def publish(args: argparse.Namespace) -> int:
    repo = Path(args.repo).expanduser().resolve()
    details = Path(args.details_root).expanduser().resolve()
    runtime = Path(args.runtime_root).expanduser().resolve()
    if not FORMAL_ID_RE.fullmatch(args.node_id):
        raise DeletionError(f"正式节点 ID 非法：{args.node_id}")
    if not args.reason.strip():
        raise DeletionError("reason 不能为空")
    if (repo / ".git").is_dir() and runtime != DEFAULT_RUNTIME_ROOT:
        raise DeletionError(f"生产仓库 runtime_root 固定为 {DEFAULT_RUNTIME_ROOT}")
    identity = _identity(args, repo, details)
    payload_sha = _sha256_bytes(_canonical_bytes(identity))
    key = identity["idempotency_key"]
    try:
        with RepoLock(repo, runtime, args.lock_timeout, timeout_error=RepoLockTimeout):
            if args.apply:
                recovered = recover_transactions(repo, runtime)
            else:
                _assert_dry_run_wal_clean(repo, runtime)
                recovered = []
            receipt_path = _receipt_path(runtime, repo, key)
            if receipt_path.exists():
                receipt = _read_json_object(receipt_path)
                if receipt.get("payload_sha256") != payload_sha:
                    raise DeletionError(f"幂等键已用于不同 payload：{key}")
                if not args.apply:
                    _emit({"status": "ALREADY_COMMITTED", "receipt": str(receipt_path)})
                    return 0
                already = True
            else:
                already = False
                outputs, deletes, stats = _project(repo, details, args.node_id, identity)
                expected = _expected_hashes(outputs, deletes)
                if not args.apply:
                    _emit({
                        "status": "DRY_RUN",
                        "idempotency_key": key,
                        "stats": stats,
                        "replace_targets": [str(path) for path in sorted(outputs, key=str)],
                        "delete_targets": [str(path) for path in sorted(deletes, key=str)],
                    })
                    return 0
                txid, receipt_path, changed = _commit_transaction(
                    repo, runtime, outputs, key, payload_sha,
                    [{"job_id": "", "formal_id": args.node_id, "mode": "delete"}],
                    [repo, details], ACTOR, None, [], f"DELETE-{args.node_id}-{args.authorized_at}",
                    {"contract": SCHEMA}, identity, details,
                    expected_before_hashes=expected, delete_targets=deletes,
                )
        closeout = _run_closeout(repo, details, Path(receipt_path), args.authorized_at)
        _emit({
            "status": "ALREADY_COMMITTED_CLOSEOUT_PASS" if already else "COMMITTED_CLOSEOUT_PASS",
            "transaction_id": None if already else txid,
            "receipt": str(receipt_path),
            "changed_files": [] if already else changed,
            "wal_recovered_before_commit": recovered,
            "closeout": [{"command": item["command"], "returncode": item["returncode"]} for item in closeout],
        })
        return 0
    except RepoLockTimeout as exc:
        raise DeletionError(str(exc)) from exc
    except TransactionRolledBack as exc:
        raise DeletionError(str(exc)) from exc
    except BatchError as exc:
        raise DeletionError(str(exc)) from exc


def closeout(args: argparse.Namespace) -> int:
    repo = Path(args.repo).expanduser().resolve()
    details = Path(args.details_root).expanduser().resolve()
    receipt = Path(args.receipt).expanduser().resolve()
    results = _run_closeout(repo, details, receipt, args.review_date)
    _emit({"status": "CLOSEOUT_PASS", "receipt": str(receipt), "commands": len(results)})
    return 0


def repair_historical_source(args: argparse.Namespace) -> int:
    repo = Path(args.repo).expanduser().resolve()
    details = Path(args.details_root).expanduser().resolve()
    runtime = Path(args.runtime_root).expanduser().resolve()
    deletion_receipt = Path(args.deletion_receipt).expanduser().resolve()
    deleted = args.deleted_node_id
    survivor = args.surviving_node_id
    if not FORMAL_ID_RE.fullmatch(deleted) or not FORMAL_ID_RE.fullmatch(survivor):
        raise DeletionError("deleted/surviving node ID 非法")
    if deleted == survivor:
        raise DeletionError("deleted/surviving node 不能相同")
    receipt = _read_json_object(deletion_receipt)
    if receipt.get("actor") != ACTOR or receipt.get("status") != "COMMITTED":
        raise DeletionError("deletion receipt 非法")
    deletion_journal = _read_json_object(Path(str(receipt.get("journal") or "")))
    deletion_identity = deletion_journal.get("payload_identity") or {}
    if deletion_identity.get("mode") != SCHEMA or deletion_identity.get("node_id") != deleted:
        raise DeletionError("deletion receipt 与 deleted node 不匹配")
    mapping_path = (repo / "历史错题归档/正式ID映射.md").resolve()
    mapping_item = next(
        (item for item in deletion_journal.get("files") or [] if Path(item["target"]).resolve() == mapping_path),
        None,
    )
    if mapping_item is None or not mapping_item.get("backup"):
        raise DeletionError("deletion receipt 未保存历史映射 before bytes")
    backup = Path(str(receipt["journal"])).parent / str(mapping_item["backup"])
    if not backup.is_file() or _sha_file(backup) != mapping_item.get("before_sha256"):
        raise DeletionError("历史映射 WAL backup 缺失或损坏")
    before_text = backup.read_text(encoding="utf-8")
    old_line = ""
    old_cells: list[str] | None = None
    old_index = -1
    for index, line in enumerate(before_text.splitlines()):
        cells = _cells(line)
        if cells and len(cells) >= 2 and cells[0] == args.source_id and cells[1] == deleted:
            old_line, old_cells, old_index = line, cells, index
            break
    if old_cells is None:
        raise DeletionError("WAL before state 找不到精确历史来源映射")
    expected_current, removed = _remove_table_rows(
        before_text, lambda cells: len(cells) >= 2 and cells[0] == args.source_id and cells[1] == deleted
    )
    if removed != 1 or mapping_path.read_text(encoding="utf-8") != expected_current:
        raise DeletionError("历史映射在删除后发生并发变化，拒绝自动重绑")

    master = (repo / "节点总表.md").read_text(encoding="utf-8")
    survivor_row = next(
        (cells for line in master.splitlines() if (cells := _cells(line)) and cells[0] == survivor),
        None,
    )
    if survivor_row is None:
        raise DeletionError(f"保留节点不存在：{survivor}")
    if survivor_row[2] != old_cells[3] or survivor_row[3] != old_cells[2]:
        raise DeletionError("历史来源与保留节点的年份或科目不一致")
    corpus_index = details / "corpus/408统考真题/indexes/question-index.csv"
    corpus_text = corpus_index.read_text(encoding="utf-8-sig")
    if survivor not in corpus_text:
        raise DeletionError("规范真题交叉索引未绑定保留节点")

    rebound = list(old_cells)
    rebound[1] = survivor
    lines = expected_current.splitlines()
    lines.insert(old_index, _render_cells(rebound))
    mapping_after = "\n".join(lines) + ("\n" if expected_current.endswith("\n") else "")
    key = args.idempotency_key or f"{SCHEMA}:{deleted}:rebind:{args.source_id}:{survivor}:{args.review_date}"
    identity = {
        "mode": SCHEMA,
        "operation": "historical_source_rebind",
        "node_id": deleted,
        "source_id": args.source_id,
        "surviving_node_id": survivor,
        "review_date": args.review_date,
        "idempotency_key": key,
        "repo_root": str(repo),
        "details_root": str(details),
        "deletion_receipt": str(deletion_receipt),
    }
    ledger_path = (repo / LEDGER).resolve()
    ledger_before = ledger_path.read_bytes()
    record = {
        "schema_version": SCHEMA,
        "operation": "historical_source_rebind",
        "deleted_node_id": deleted,
        "source_id": args.source_id,
        "surviving_node_id": survivor,
        "review_date": args.review_date,
        "basis": "same canonical exam crosswalk; rebind preserves historical retrieval without copying user error facts",
        "deletion_receipt": str(deletion_receipt),
        "idempotency_key": key,
    }
    ledger_after = ledger_before + json.dumps(
        record, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8") + b"\n"
    outputs = {mapping_path: mapping_after.encode("utf-8"), ledger_path: ledger_after}
    payload_sha = _sha256_bytes(_canonical_bytes(identity))
    try:
        with RepoLock(repo, runtime, args.lock_timeout, timeout_error=RepoLockTimeout):
            if args.apply:
                recovered = recover_transactions(repo, runtime)
            else:
                _assert_dry_run_wal_clean(repo, runtime)
                recovered = []
            receipt_path = _receipt_path(runtime, repo, key)
            if receipt_path.exists():
                existing = _read_json_object(receipt_path)
                if existing.get("payload_sha256") != payload_sha:
                    raise DeletionError(f"幂等键已用于不同 payload：{key}")
                if not args.apply:
                    _emit({"status": "ALREADY_COMMITTED", "receipt": str(receipt_path)})
                    return 0
                already = True
            else:
                already = False
                expected = _expected_hashes(outputs, set())
                if not args.apply:
                    _emit({
                        "status": "DRY_RUN",
                        "operation": "historical_source_rebind",
                        "source_id": args.source_id,
                        "from": deleted,
                        "to": survivor,
                        "targets": [str(path) for path in outputs],
                    })
                    return 0
                txid, receipt_path, changed = _commit_transaction(
                    repo, runtime, outputs, key, payload_sha,
                    [{"job_id": "", "formal_id": survivor, "mode": "historical_source_rebind"}],
                    [repo, details], ACTOR, None, [], f"REBIND-{args.source_id}-{args.review_date}",
                    {"contract": SCHEMA, "supersedes": receipt.get("transaction_id")},
                    identity, details, expected_before_hashes=expected,
                )
        results = _run_closeout(repo, details, Path(receipt_path), args.review_date)
        _emit({
            "status": "ALREADY_COMMITTED_CLOSEOUT_PASS" if already else "COMMITTED_CLOSEOUT_PASS",
            "transaction_id": None if already else txid,
            "receipt": str(receipt_path),
            "changed_files": [] if already else changed,
            "wal_recovered_before_commit": recovered,
            "closeout": [{"command": item["command"], "returncode": item["returncode"]} for item in results],
        })
        return 0
    except RepoLockTimeout as exc:
        raise DeletionError(str(exc)) from exc


def tombstone_historical_source(args: argparse.Namespace) -> int:
    repo = Path(args.repo).expanduser().resolve()
    details = Path(args.details_root).expanduser().resolve()
    runtime = Path(args.runtime_root).expanduser().resolve()
    rebind_receipt = Path(args.rebind_receipt).expanduser().resolve()
    deleted = args.deleted_node_id
    survivor = args.surviving_node_id
    receipt = _read_json_object(rebind_receipt)
    if receipt.get("actor") != ACTOR or receipt.get("status") != "COMMITTED":
        raise DeletionError("rebind receipt 非法")
    journal = _read_json_object(Path(str(receipt.get("journal") or "")))
    prior = journal.get("payload_identity") or {}
    if (
        prior.get("mode") != SCHEMA
        or prior.get("operation") != "historical_source_rebind"
        or prior.get("node_id") != deleted
        or prior.get("source_id") != args.source_id
        or prior.get("surviving_node_id") != survivor
    ):
        raise DeletionError("rebind receipt 与待撤销映射不匹配")
    key = args.idempotency_key or f"{SCHEMA}:{deleted}:tombstone:{args.source_id}:{args.review_date}"
    identity = {
        "mode": SCHEMA,
        "operation": "historical_source_tombstone",
        "node_id": deleted,
        "source_id": args.source_id,
        "review_date": args.review_date,
        "idempotency_key": key,
        "repo_root": str(repo),
        "details_root": str(details),
        "rebind_receipt": str(rebind_receipt),
    }
    payload_sha = _sha256_bytes(_canonical_bytes(identity))
    existing_receipt = _receipt_path(runtime, repo, key)
    if existing_receipt.exists():
        existing = _read_json_object(existing_receipt)
        if existing.get("payload_sha256") != payload_sha:
            raise DeletionError(f"幂等键已用于不同 payload：{key}")
        if not args.apply:
            _emit({"status": "ALREADY_COMMITTED", "receipt": str(existing_receipt)})
            return 0
        results = _run_closeout(repo, details, existing_receipt, args.review_date)
        _emit({
            "status": "ALREADY_COMMITTED_CLOSEOUT_PASS",
            "transaction_id": None,
            "receipt": str(existing_receipt),
            "changed_files": [],
            "wal_recovered_before_commit": [],
            "closeout": [{"command": item["command"], "returncode": item["returncode"]} for item in results],
        })
        return 0
    mapping_path = (repo / "历史错题归档/正式ID映射.md").resolve()
    mapping_after, removed = _remove_table_rows(
        mapping_path.read_text(encoding="utf-8"),
        lambda cells: len(cells) >= 2 and cells[0] == args.source_id and cells[1] == survivor,
    )
    if removed != 1:
        raise DeletionError("当前活动映射不是唯一精确 rebind 结果")
    ledger_path = (repo / LEDGER).resolve()
    ledger_before = ledger_path.read_bytes()
    record = {
        "schema_version": SCHEMA,
        "operation": "historical_source_tombstone",
        "source_id": args.source_id,
        "deleted_node_id": deleted,
        "review_date": args.review_date,
        "reason": "formal node explicitly deleted; historical source remains immutable without an active primary-source mapping",
        "supersedes_rebind_receipt": str(rebind_receipt),
        "idempotency_key": key,
    }
    ledger_after = ledger_before + json.dumps(
        record, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8") + b"\n"
    outputs = {mapping_path: mapping_after.encode("utf-8"), ledger_path: ledger_after}
    try:
        with RepoLock(repo, runtime, args.lock_timeout, timeout_error=RepoLockTimeout):
            if args.apply:
                recovered = recover_transactions(repo, runtime)
            else:
                _assert_dry_run_wal_clean(repo, runtime)
                recovered = []
            receipt_path = _receipt_path(runtime, repo, key)
            if receipt_path.exists():
                existing = _read_json_object(receipt_path)
                if existing.get("payload_sha256") != payload_sha:
                    raise DeletionError(f"幂等键已用于不同 payload：{key}")
                if not args.apply:
                    _emit({"status": "ALREADY_COMMITTED", "receipt": str(receipt_path)})
                    return 0
                already = True
            else:
                already = False
                expected = _expected_hashes(outputs, set())
                if not args.apply:
                    _emit({
                        "status": "DRY_RUN",
                        "operation": "historical_source_tombstone",
                        "source_id": args.source_id,
                        "deleted_node_id": deleted,
                        "targets": [str(path) for path in outputs],
                    })
                    return 0
                txid, receipt_path, changed = _commit_transaction(
                    repo, runtime, outputs, key, payload_sha,
                    [{"job_id": "", "formal_id": deleted, "mode": "historical_source_tombstone"}],
                    [repo, details], ACTOR, None, [], f"TOMBSTONE-{args.source_id}-{args.review_date}",
                    {"contract": SCHEMA, "supersedes": receipt.get("transaction_id")},
                    identity, details, expected_before_hashes=expected,
                )
        results = _run_closeout(repo, details, Path(receipt_path), args.review_date)
        _emit({
            "status": "ALREADY_COMMITTED_CLOSEOUT_PASS" if already else "COMMITTED_CLOSEOUT_PASS",
            "transaction_id": None if already else txid,
            "receipt": str(receipt_path),
            "changed_files": [] if already else changed,
            "wal_recovered_before_commit": recovered,
            "closeout": [{"command": item["command"], "returncode": item["returncode"]} for item in results],
        })
        return 0
    except RepoLockTimeout as exc:
        raise DeletionError(str(exc)) from exc
    except TransactionRolledBack as exc:
        raise DeletionError(str(exc)) from exc
    except BatchError as exc:
        raise DeletionError(str(exc)) from exc
    except TransactionRolledBack as exc:
        raise DeletionError(str(exc)) from exc
    except BatchError as exc:
        raise DeletionError(str(exc)) from exc


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    pub = sub.add_parser("publish")
    pub.add_argument("--repo", default=".")
    pub.add_argument("--details-root", default=str(DEFAULT_DETAILS_ROOT))
    pub.add_argument("--runtime-root", default=str(DEFAULT_RUNTIME_ROOT))
    pub.add_argument("--node-id", required=True)
    pub.add_argument("--authorization", required=True)
    pub.add_argument("--authorized-at", required=True)
    pub.add_argument("--reason", required=True)
    pub.add_argument("--idempotency-key")
    pub.add_argument("--lock-timeout", type=float, default=30.0)
    pub.add_argument("--apply", action="store_true")
    pub.set_defaults(func=publish)
    close = sub.add_parser("closeout")
    close.add_argument("--repo", default=".")
    close.add_argument("--details-root", default=str(DEFAULT_DETAILS_ROOT))
    close.add_argument("--receipt", required=True)
    close.add_argument("--review-date", required=True)
    close.set_defaults(func=closeout)
    repair = sub.add_parser("repair-historical-source")
    repair.add_argument("--repo", default=".")
    repair.add_argument("--details-root", default=str(DEFAULT_DETAILS_ROOT))
    repair.add_argument("--runtime-root", default=str(DEFAULT_RUNTIME_ROOT))
    repair.add_argument("--deletion-receipt", required=True)
    repair.add_argument("--deleted-node-id", required=True)
    repair.add_argument("--source-id", required=True)
    repair.add_argument("--surviving-node-id", required=True)
    repair.add_argument("--review-date", required=True)
    repair.add_argument("--idempotency-key")
    repair.add_argument("--lock-timeout", type=float, default=30.0)
    repair.add_argument("--apply", action="store_true")
    repair.set_defaults(func=repair_historical_source)
    tombstone = sub.add_parser("tombstone-historical-source")
    tombstone.add_argument("--repo", default=".")
    tombstone.add_argument("--details-root", default=str(DEFAULT_DETAILS_ROOT))
    tombstone.add_argument("--runtime-root", default=str(DEFAULT_RUNTIME_ROOT))
    tombstone.add_argument("--rebind-receipt", required=True)
    tombstone.add_argument("--deleted-node-id", required=True)
    tombstone.add_argument("--source-id", required=True)
    tombstone.add_argument("--surviving-node-id", required=True)
    tombstone.add_argument("--review-date", required=True)
    tombstone.add_argument("--idempotency-key")
    tombstone.add_argument("--lock-timeout", type=float, default=30.0)
    tombstone.add_argument("--apply", action="store_true")
    tombstone.set_defaults(func=tombstone_historical_source)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        return args.func(args)
    except DeletionError as exc:
        _emit({"status": "ERROR", "error": str(exc)})
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
