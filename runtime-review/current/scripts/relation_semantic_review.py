#!/usr/bin/env python3
"""408 全库逐题关系语义复核的只读快照与校验工具。

``snapshot`` 冻结正式核心文件、安全卡与 details 证据的 SHA-256，并在显式
提供 ``--out-dir`` 时生成按科目拆分的 JSONL 工作清单。工作清单只包含正式层
安全字段、证据路径和哈希，不复制题干、答案、选项或解析正文。

``validate`` 读取冻结快照和 reviewer 产出的 JSONL，检查节点/基线边是否恰好
复核一次、枚举与证据是否合规、基线是否漂移。默认只打印结果；只有显式提供
``--out-dir`` 才会写 ``validation-report.json``。

本工具永不修改正式表、安全卡、details 或 ``复盘输出/``。
"""

from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import os
import re
import sys
import tempfile
from urllib.parse import unquote
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any, Iterable

import runtime_paths_408 as _paths


SNAPSHOT_SCHEMA = "relation_semantic_snapshot_v1"
VALIDATION_SCHEMA = "relation_semantic_validation_report_v1"
DEFAULT_DETAILS_ROOT = _paths.details_root()
SUBJECTS = ("DS", "CO", "OS", "CN")
SUBJECT_VIEW_FILES = {
    "DS": "节点库/数据结构节点.md",
    "CO": "节点库/计算机组成原理节点.md",
    "OS": "节点库/操作系统节点.md",
    "CN": "节点库/计算机网络节点.md",
}
CORE_FILES = (
    "节点总表.md",
    "关系边表.md",
    "知识点命中索引.md",
    *SUBJECT_VIEW_FILES.values(),
)
FORMAL_ID_RE = re.compile(r"^(DS|CO|OS|CN)_(?:\d{4}|UNK)_\d{3}$")
EDGE_TYPE_RE = re.compile(r"^(R0[1-9])(?:\s|$)")
TEMPLATE_REASON_RE = re.compile(r"^\s*(?:同一主知识点|同属专题链|同一模糊概念)\s*[：:]")
PROTECTED_OUTPUT_RE = re.compile(
    r"(?:正确答案|标准答案|错误答案|我的答案|我的选择)\s*[：:]"
    r"|(?:错选|误选|应选|故选)\s*[A-DＡ-Ｄ]"
    r"|答案(?:是|为)\s*[A-DＡ-Ｄ]"
)
PROTECTED_FRAGMENT_RE = re.compile(
    r"(?:正确答案|标准答案|错误答案|我的答案|我的选择)\s*[：:]\s*[^；。,.，]{0,80}"
    r"|(?:错选|误选|应选|故选)\s*[A-DＡ-Ｄ](?:\s*[(（][^)）]*[)）])?"
    r"|答案(?:是|为)\s*[A-DＡ-Ｄ]"
)
SOLUTION_CALLOUT_RE = re.compile(r"^>\s*\[!answer\][^\n]*$", re.M | re.I)
SOLUTION_TEXT_THRESHOLD = 50
OBSIDIAN_EMBED_RE = re.compile(r"!\[\[([^\]]+)\]\]")
MARKDOWN_IMAGE_RE = re.compile(r"!\[[^\]]*\]\(([^)]+)\)")

NODE_DECISIONS = {"keep", "replace", "needs_user"}
CORE_DECISIONS = {"keep", "refine", "needs_user"}
RELATION_STATUSES = {"connected", "reviewed_no_reliable_edge", "needs_user"}
CONFIDENCES = {"high", "medium", "low"}
EVIDENCE_GRADES = {"A", "B", "C", "D"}
EVIDENCE_GRADE_RANK = {"D": 0, "C": 1, "B": 2, "A": 3}
EDGE_DECISIONS = {"keep", "remove", "replace"}
EDGE_TYPES = {f"R0{i}" for i in range(1, 10)}
STRENGTHS = {"强", "中", "弱"}
PRIORITIES = {"高", "中", "低"}


class ReviewToolError(RuntimeError):
    """快照或校验无法安全继续。"""


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _split_row(line: str) -> list[str]:
    stripped = line.strip()
    if not (stripped.startswith("|") and stripped.endswith("|")):
        return []
    return [cell.strip() for cell in stripped[1:-1].split("|")]


def _is_separator(cells: list[str]) -> bool:
    return bool(cells) and all(re.fullmatch(r":?-{2,}:?", cell or "") for cell in cells)


def read_markdown_table(path: Path, first_column: str) -> list[dict[str, str]]:
    if not path.is_file():
        raise ReviewToolError(f"缺少必需文件：{path}")
    lines = path.read_text(encoding="utf-8").splitlines()
    for index, line in enumerate(lines[:-1]):
        headers = _split_row(line)
        if not headers or headers[0] != first_column:
            continue
        if not _is_separator(_split_row(lines[index + 1])):
            continue
        rows: list[dict[str, str]] = []
        for row_line in lines[index + 2 :]:
            cells = _split_row(row_line)
            if not cells:
                break
            if len(cells) != len(headers):
                raise ReviewToolError(
                    f"{path} 表格列数异常：应为 {len(headers)} 列，实际 {len(cells)} 列"
                )
            rows.append(dict(zip(headers, cells)))
        return rows
    raise ReviewToolError(f"{path} 找不到首列为 {first_column!r} 的 Markdown 表格")


def edge_type_code(value: str) -> str:
    match = EDGE_TYPE_RE.match((value or "").strip())
    return match.group(1) if match else ""


def unordered_pair(a: str, b: str) -> tuple[str, str]:
    return tuple(sorted((a, b)))


def pair_key(a: str, b: str) -> str:
    left, right = unordered_pair(a, b)
    return f"{left}::{right}"


def _safe_output_text(value: str, limit: int = 240) -> str:
    text = re.sub(r"\s+", " ", (value or "").strip())
    text = PROTECTED_FRAGMENT_RE.sub("[受保护内容已隐藏]", text)
    return text[:limit]


def _contains_protected_output(value: Any) -> bool:
    return isinstance(value, str) and bool(PROTECTED_OUTPUT_RE.search(value))


def _file_record(root: Path, path: Path, namespace: str, role: str) -> dict[str, Any]:
    resolved = path.resolve()
    try:
        relative = resolved.relative_to(root.resolve()).as_posix()
    except ValueError as exc:
        raise ReviewToolError(f"证据路径越过 {namespace} 根目录：{path}") from exc
    return {
        "path": f"{namespace}:{relative}",
        "sha256": sha256_file(resolved),
        "size": resolved.stat().st_size,
        "role": role,
    }


def _solution_text_chars(card_text: str) -> int:
    match = SOLUTION_CALLOUT_RE.search(card_text)
    if not match:
        return 0
    body = card_text[match.end() :]
    kept: list[str] = []
    placeholders = (
        "解析文字待补充",
        "解析图片待补充",
        "题目图片待补充",
        "暂无解析",
        "待补充",
    )
    started = False
    for raw in body.splitlines():
        if not raw.strip() and not started:
            continue
        if not raw.lstrip().startswith(">"):
            if raw.strip():
                break
            continue
        started = True
        line = re.sub(r"^\s*>\s?", "", raw).strip()
        if not line or line.startswith("![[") or line.startswith("![]("):
            continue
        if any(marker in line for marker in placeholders):
            continue
        kept.append(line)
    normalized = re.sub(r"[^0-9A-Za-z\u3400-\u9fff]", "", "".join(kept))
    return len(normalized)


def _embedded_detail_assets(
    details_root: Path,
    detail_card: Path,
    card_text: str,
) -> list[Path]:
    """Resolve local image embeds without allowing evidence to leave the details vault."""
    raw_targets = OBSIDIAN_EMBED_RE.findall(card_text) + MARKDOWN_IMAGE_RE.findall(card_text)
    assets: set[Path] = set()
    root = details_root.resolve()
    for raw_target in raw_targets:
        target = unquote(raw_target.strip().split("|", 1)[0].split("#", 1)[0]).strip()
        if not target or "://" in target:
            continue
        relative = Path(target.lstrip("/"))
        candidates = (root / relative, detail_card.parent / relative)
        for candidate in candidates:
            resolved = candidate.resolve()
            try:
                resolved.relative_to(root)
            except ValueError:
                continue
            if resolved.is_file():
                assets.add(resolved)
                break
    return sorted(assets)


def _evidence_for_node(repo: Path, details_root: Path, node_id: str) -> tuple[str, list[dict[str, Any]], int]:
    safe_card = repo / f"{node_id}.md"
    if not safe_card.is_file():
        raise ReviewToolError(f"正式节点缺根安全卡：{node_id}.md")
    evidence = [_file_record(repo, safe_card, "formal", "safe_card")]

    detail_card = details_root / "cards" / f"{node_id}.md"
    solution_chars = 0
    detail_card_text = ""
    if detail_card.is_file():
        evidence.append(_file_record(details_root, detail_card, "details", "detail_card"))
        detail_card_text = detail_card.read_text(encoding="utf-8", errors="ignore")
        solution_chars = _solution_text_chars(detail_card_text)

    asset_dir = details_root / "assets" / node_id
    question_assets: list[Path] = []
    solution_assets: list[Path] = []
    recorded_assets: set[Path] = set()
    if asset_dir.is_dir():
        for path in sorted(candidate for candidate in asset_dir.rglob("*") if candidate.is_file()):
            if path.name.startswith("question-"):
                role = "question_image"
                if path.stat().st_size:
                    question_assets.append(path)
            elif path.name.startswith("solution-"):
                role = "solution_image"
                if path.stat().st_size:
                    solution_assets.append(path)
            else:
                role = "detail_asset"
            evidence.append(_file_record(details_root, path, "details", role))
            recorded_assets.add(path.resolve())

    if detail_card_text:
        for path in _embedded_detail_assets(details_root, detail_card, detail_card_text):
            if path in recorded_assets:
                continue
            if path.name.startswith("question-"):
                role = "question_image"
                if path.stat().st_size:
                    question_assets.append(path)
            elif path.name.startswith("solution-"):
                role = "solution_image"
                if path.stat().st_size:
                    solution_assets.append(path)
            else:
                role = "detail_asset"
            evidence.append(_file_record(details_root, path, "details", role))
            recorded_assets.add(path)

    if not question_assets:
        grade = "D"
    elif solution_assets:
        grade = "A"
    elif solution_chars >= SOLUTION_TEXT_THRESHOLD:
        grade = "B"
    else:
        grade = "C"
    return grade, evidence, solution_chars


def _core_file_records(repo: Path) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    for relative in CORE_FILES:
        path = repo / relative
        if not path.is_file():
            raise ReviewToolError(f"缺少核心文件：{relative}")
        records.append(_file_record(repo, path, "formal", "core_file"))
    return records


def build_snapshot(
    repo: Path,
    details_root: Path,
    *,
    expected_nodes: int | None = None,
    expected_edges: int | None = None,
) -> dict[str, Any]:
    repo = repo.expanduser().resolve()
    details_root = details_root.expanduser().resolve()
    node_rows = read_markdown_table(repo / "节点总表.md", "ID")
    edge_rows = read_markdown_table(repo / "关系边表.md", "起点ID")

    nodes_by_id: dict[str, dict[str, str]] = {}
    for row in node_rows:
        node_id = row.get("ID", "").strip()
        if not FORMAL_ID_RE.fullmatch(node_id):
            raise ReviewToolError(f"正式节点 ID 非法：{node_id!r}")
        if node_id in nodes_by_id:
            raise ReviewToolError(f"节点总表 ID 重复：{node_id}")
        nodes_by_id[node_id] = row
    if expected_nodes is not None and len(nodes_by_id) != expected_nodes:
        raise ReviewToolError(f"节点基线数量不符：期望 {expected_nodes}，实际 {len(nodes_by_id)}")

    baseline_edges: list[dict[str, Any]] = []
    seen_pairs: set[tuple[str, str]] = set()
    for row in edge_rows:
        start = row.get("起点ID", "").strip()
        end = row.get("终点ID", "").strip()
        if start not in nodes_by_id or end not in nodes_by_id:
            raise ReviewToolError(f"关系边端点不在节点基线：{start} -> {end}")
        if start == end:
            raise ReviewToolError(f"关系边自环：{start}")
        pair = unordered_pair(start, end)
        if pair in seen_pairs:
            raise ReviewToolError(f"无序题对存在多条基线边：{pair[0]} <-> {pair[1]}")
        seen_pairs.add(pair)
        relation_type = edge_type_code(row.get("关系类型", ""))
        if relation_type not in EDGE_TYPES:
            raise ReviewToolError(f"关系类型非法：{start} -> {end} {row.get('关系类型', '')!r}")
        baseline_edges.append(
            {
                "key": pair_key(*pair),
                "a": pair[0],
                "b": pair[1],
                "from": start,
                "to": end,
                "current_type": relation_type,
                "current_strength": row.get("联系强度", "").strip(),
                "current_priority": row.get("复盘优先级", "").strip(),
                "current_reason": _safe_output_text(row.get("关联原因", "")),
                "template_reason": bool(TEMPLATE_REASON_RE.match(row.get("关联原因", ""))),
            }
        )
    if expected_edges is not None and len(baseline_edges) != expected_edges:
        raise ReviewToolError(f"关系边基线数量不符：期望 {expected_edges}，实际 {len(baseline_edges)}")

    baseline_nodes: list[dict[str, Any]] = []
    grade_counts: Counter[str] = Counter()
    for node_id, row in sorted(nodes_by_id.items()):
        subject = node_id.split("_", 1)[0]
        grade, evidence_files, solution_chars = _evidence_for_node(repo, details_root, node_id)
        grade_counts[grade] += 1
        baseline_nodes.append(
            {
                "id": node_id,
                "subject": subject,
                "year": _safe_output_text(row.get("年份", ""), 24),
                "module": _safe_output_text(row.get("主模块", ""), 120),
                "main_knowledge": _safe_output_text(row.get("主知识点", ""), 160),
                "question_type": _safe_output_text(row.get("题型", ""), 80),
                "core_point": _safe_output_text(row.get("核心考点", ""), 240),
                "evidence_grade": grade,
                "solution_text_chars": solution_chars,
                "evidence_files": evidence_files,
            }
        )

    return {
        "schema_version": SNAPSHOT_SCHEMA,
        "created_at": dt.datetime.now(dt.timezone.utc).isoformat(),
        "repo_root": str(repo),
        "details_root": str(details_root),
        "policy": {
            "formal_content_boundary": "safe_metadata_only",
            "solution_text_threshold": SOLUTION_TEXT_THRESHOLD,
            "review_output_must_not_copy_protected_content": True,
        },
        "counts": {
            "nodes": len(baseline_nodes),
            "edges": len(baseline_edges),
            "safe_cards": len(baseline_nodes),
            "evidence_grades": dict(sorted(grade_counts.items())),
        },
        "core_files": _core_file_records(repo),
        "nodes": baseline_nodes,
        "edges": sorted(baseline_edges, key=lambda item: item["key"]),
    }


def _node_task(node: dict[str, Any]) -> dict[str, Any]:
    return {
        "record_type": "node_task",
        "id": node["id"],
        "subject": node["subject"],
        "evidence_grade": node["evidence_grade"],
        "evidence_files": node["evidence_files"],
        "current_safe_fields": {
            "module": node["module"],
            "main_knowledge": node["main_knowledge"],
            "question_type": node["question_type"],
            "core_point": node["core_point"],
        },
        "required_output": "node_review",
    }


def _edge_task(edge: dict[str, Any]) -> dict[str, Any]:
    return {
        "record_type": "edge_task",
        "a": edge["a"],
        "b": edge["b"],
        "from": edge["from"],
        "to": edge["to"],
        "current_type": edge["current_type"],
        "current_strength": edge["current_strength"],
        "current_priority": edge["current_priority"],
        "current_reason": edge["current_reason"],
        "template_reason": edge["template_reason"],
        "required_output": "edge_review",
    }


def build_worklists(snapshot: dict[str, Any]) -> dict[str, list[dict[str, Any]]]:
    worklists: dict[str, list[dict[str, Any]]] = {subject: [] for subject in SUBJECTS}
    worklists["CENTRAL"] = []
    for node in snapshot["nodes"]:
        worklists[node["subject"]].append(_node_task(node))
    for edge in snapshot["edges"]:
        subject_a = edge["a"].split("_", 1)[0]
        subject_b = edge["b"].split("_", 1)[0]
        owner = subject_a if subject_a == subject_b else "CENTRAL"
        worklists[owner].append(_edge_task(edge))
    return worklists


def _is_within(path: Path, parent: Path) -> bool:
    try:
        path.resolve().relative_to(parent.resolve())
        return True
    except ValueError:
        return False


def _validate_out_dir(out_dir: Path, repo: Path, details_root: Path) -> Path:
    resolved = out_dir.expanduser().resolve()
    if _is_within(resolved, repo) or _is_within(resolved, details_root):
        raise ReviewToolError("--out-dir 必须位于正式仓库和 details 库之外")
    if "复盘输出" in resolved.parts:
        raise ReviewToolError("--out-dir 不得位于复盘输出目录")
    return resolved


def _atomic_write(path: Path, data: bytes, *, overwrite: bool) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists() and not overwrite:
        raise ReviewToolError(f"输出已存在，拒绝覆盖；如确认重建请加 --overwrite：{path}")
    fd, temporary_name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    temporary = Path(temporary_name)
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def write_snapshot_artifacts(
    snapshot: dict[str, Any],
    out_dir: Path,
    *,
    overwrite: bool = False,
) -> list[str]:
    repo = Path(snapshot["repo_root"])
    details_root = Path(snapshot["details_root"])
    out_dir = _validate_out_dir(out_dir, repo, details_root)
    written: list[str] = []
    snapshot_path = out_dir / "snapshot.json"
    _atomic_write(
        snapshot_path,
        json.dumps(snapshot, ensure_ascii=False, indent=2, sort_keys=True).encode("utf-8") + b"\n",
        overwrite=overwrite,
    )
    written.append(str(snapshot_path))
    for owner, records in build_worklists(snapshot).items():
        path = out_dir / f"worklist-{owner}.jsonl"
        payload = "".join(json.dumps(record, ensure_ascii=False, sort_keys=True) + "\n" for record in records)
        _atomic_write(path, payload.encode("utf-8"), overwrite=overwrite)
        written.append(str(path))
    return written


def _resolve_qualified_path(value: str, repo: Path, details_root: Path) -> Path:
    if value.startswith("formal:"):
        root, relative = repo, value.removeprefix("formal:")
    elif value.startswith("details:"):
        root, relative = details_root, value.removeprefix("details:")
    else:
        raise ReviewToolError(f"证据路径缺 formal:/details: 命名空间：{value!r}")
    target = (root / relative).resolve()
    if not _is_within(target, root):
        raise ReviewToolError(f"证据路径越界：{value}")
    return target


def _canonical_review_evidence_path(value: str, repo: Path, details_root: Path) -> str:
    """把 reviewer 的命名空间、绝对路径或仓库相对路径统一为 snapshot 路径。"""
    if value.startswith(("formal:", "details:")):
        _resolve_qualified_path(value, repo, details_root)
        return value

    raw = Path(value).expanduser()
    if raw.is_absolute():
        resolved = raw.resolve()
        if _is_within(resolved, repo):
            return f"formal:{resolved.relative_to(repo).as_posix()}"
        if _is_within(resolved, details_root):
            return f"details:{resolved.relative_to(details_root).as_posix()}"
        raise ReviewToolError(f"review evidence 绝对路径越过正式仓库/details：{value}")

    formal = (repo / raw).resolve()
    details = (details_root / raw).resolve()
    if formal.is_file() and _is_within(formal, repo):
        return f"formal:{formal.relative_to(repo).as_posix()}"
    if details.is_file() and _is_within(details, details_root):
        return f"details:{details.relative_to(details_root).as_posix()}"
    raise ReviewToolError(f"review evidence 相对路径无法定位：{value}")


def _load_json(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ReviewToolError(f"JSON 读取失败 {path}：{exc}") from exc
    if not isinstance(value, dict):
        raise ReviewToolError(f"JSON 顶层必须是 object：{path}")
    return value


def _review_files(paths: Iterable[Path]) -> list[Path]:
    files: list[Path] = []
    for raw in paths:
        path = raw.expanduser().resolve()
        if path.is_dir():
            files.extend(sorted(candidate for candidate in path.glob("*.jsonl") if candidate.is_file()))
        elif path.is_file():
            files.append(path)
        else:
            raise ReviewToolError(f"review 路径不存在：{path}")
    unique = sorted(set(files))
    if not unique:
        raise ReviewToolError("没有找到 review JSONL")
    return unique


def load_review_records(paths: Iterable[Path]) -> tuple[list[dict[str, Any]], list[str]]:
    records: list[dict[str, Any]] = []
    errors: list[str] = []
    for path in _review_files(paths):
        for line_number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
            if not line.strip():
                continue
            try:
                value = json.loads(line)
            except json.JSONDecodeError as exc:
                errors.append(f"{path}:{line_number} JSON 非法：{exc.msg}")
                continue
            if not isinstance(value, dict):
                errors.append(f"{path}:{line_number} 必须是 JSON object")
                continue
            if value.get("record_type") not in {"node_review", "edge_review"}:
                errors.append(f"{path}:{line_number} record_type 非法或误传 worklist")
                continue
            records.append(value)
    return records, errors


def _check_snapshot_hashes(
    snapshot: dict[str, Any], repo: Path, details_root: Path, errors: list[str]
) -> dict[str, dict[str, Any]]:
    expected: dict[str, dict[str, Any]] = {}
    for record in snapshot.get("core_files", []):
        expected[record.get("path", "")] = record
    for node in snapshot.get("nodes", []):
        for record in node.get("evidence_files", []):
            path_value = record.get("path", "")
            prior = expected.get(path_value)
            if prior and prior.get("sha256") != record.get("sha256"):
                errors.append(f"snapshot 内同路径哈希冲突：{path_value}")
            expected[path_value] = record

    for path_value, record in sorted(expected.items()):
        try:
            path = _resolve_qualified_path(path_value, repo, details_root)
        except ReviewToolError as exc:
            errors.append(str(exc))
            continue
        if not path.is_file():
            errors.append(f"基线证据缺失：{path_value}")
            continue
        actual = sha256_file(path)
        if actual != record.get("sha256"):
            errors.append(f"evidence hash 漂移：{path_value}")
    return expected


def _required_string(record: dict[str, Any], field: str, label: str, errors: list[str]) -> str:
    value = record.get(field)
    if not isinstance(value, str) or not value.strip():
        errors.append(f"{label} 缺非空字段 {field}")
        return ""
    return value.strip()


def _check_enum(value: Any, allowed: set[str], field: str, label: str, errors: list[str]) -> str:
    if value not in allowed:
        errors.append(f"{label} {field} 枚举非法：{value!r}")
        return ""
    return str(value)


def _reject_pending_state(record: dict[str, Any], label: str, errors: list[str]) -> None:
    for field in (
        "status",
        "review_status",
        "decision",
        "main_knowledge_decision",
        "core_point_decision",
        "relation_status",
        "confidence",
    ):
        value = record.get(field)
        if isinstance(value, str) and value.strip().lower() == "pending":
            errors.append(f"{label} 不接受 pending 状态：{field}")


def _validate_node_review(
    record: dict[str, Any],
    baseline: dict[str, Any],
    repo: Path,
    details_root: Path,
    snapshot_evidence: dict[str, dict[str, Any]],
    errors: list[str],
) -> None:
    node_id = baseline["id"]
    label = f"node_review[{node_id}]"
    _reject_pending_state(record, label, errors)
    if record.get("subject") != baseline["subject"]:
        errors.append(f"{label} subject 与基线不一致")
    grade = _check_enum(record.get("evidence_grade"), EVIDENCE_GRADES, "evidence_grade", label, errors)
    baseline_grade = baseline["evidence_grade"]
    if grade and EVIDENCE_GRADE_RANK[grade] > EVIDENCE_GRADE_RANK[baseline_grade]:
        errors.append(f"{label} evidence_grade 不得高于 snapshot 证据提示 {baseline_grade}")
    main_decision = _check_enum(
        record.get("main_knowledge_decision"), NODE_DECISIONS,
        "main_knowledge_decision", label, errors,
    )
    core_decision = _check_enum(
        record.get("core_point_decision"), CORE_DECISIONS,
        "core_point_decision", label, errors,
    )
    relation_status = _check_enum(
        record.get("relation_status"), RELATION_STATUSES,
        "relation_status", label, errors,
    )
    confidence = _check_enum(record.get("confidence"), CONFIDENCES, "confidence", label, errors)
    main_knowledge = _required_string(record, "main_knowledge", label, errors)
    core_point = _required_string(record, "core_point", label, errors)
    question_mechanism = _required_string(record, "question_mechanism", label, errors)
    notes = record.get("notes", "")
    if not isinstance(notes, str):
        errors.append(f"{label} notes 必须是字符串")

    for field, value in (
        ("main_knowledge", main_knowledge),
        ("core_point", core_point),
        ("question_mechanism", question_mechanism),
        ("notes", notes),
    ):
        if _contains_protected_output(value):
            errors.append(f"{label} {field} 含受保护答案/错选模式")

    evidence = record.get("evidence_files")
    if not isinstance(evidence, list):
        errors.append(f"{label} evidence_files 必须是数组")
    else:
        expected = {item["path"]: item["sha256"] for item in baseline["evidence_files"]}
        actual: dict[str, str] = {}
        for item in evidence:
            if not isinstance(item, dict):
                errors.append(f"{label} evidence_files 每项必须是 object")
                continue
            path_value, digest = item.get("path"), item.get("sha256")
            if not isinstance(path_value, str) or not isinstance(digest, str):
                errors.append(f"{label} evidence_files 缺 path/sha256")
                continue
            try:
                canonical = _canonical_review_evidence_path(path_value, repo, details_root)
            except ReviewToolError as exc:
                errors.append(f"{label} {exc}")
                continue
            if canonical in actual:
                errors.append(f"{label} evidence_files 路径重复：{canonical}")
            actual[canonical] = digest
        missing = set(expected) - set(actual)
        if missing:
            errors.append(f"{label} evidence_files 缺 snapshot 节点证据：{sorted(missing)}")
        unknown = set(actual) - set(snapshot_evidence)
        if unknown:
            errors.append(f"{label} evidence_files 含非 snapshot 证据：{sorted(unknown)}")
        for path_value in set(actual) & set(snapshot_evidence):
            if actual[path_value] != snapshot_evidence[path_value]["sha256"]:
                errors.append(f"{label} evidence sha256 与 snapshot 不一致：{path_value}")

    if main_decision in {"keep", "replace"} and not main_knowledge:
        errors.append(f"{label} 已确认主知识点但未给安全值")
    if core_decision in {"keep", "refine"} and not core_point:
        errors.append(f"{label} 已确认核心考点但未给安全值")

    if grade == "D" or baseline_grade == "D":
        if main_decision != "needs_user":
            errors.append(f"{label} D 级证据的 main_knowledge_decision 必须 needs_user")
        if core_decision != "needs_user":
            errors.append(f"{label} D 级证据的 core_point_decision 必须 needs_user")
        if relation_status != "needs_user":
            errors.append(f"{label} D 级证据的 relation_status 必须 needs_user")
        if confidence != "low":
            errors.append(f"{label} D 级证据的 confidence 必须 low")


def _validate_edge_review(
    record: dict[str, Any],
    baseline: dict[str, Any],
    errors: list[str],
) -> None:
    label = f"edge_review[{baseline['a']}::{baseline['b']}]"
    _reject_pending_state(record, label, errors)
    current_type = edge_type_code(str(record.get("current_type", "")))
    if current_type != baseline["current_type"]:
        errors.append(f"{label} current_type 与基线不一致")
    decision = _check_enum(record.get("decision"), EDGE_DECISIONS, "decision", label, errors)
    _check_enum(record.get("confidence"), CONFIDENCES, "confidence", label, errors)
    reason = _required_string(record, "reason", label, errors)
    if reason and len(reason) < 10:
        errors.append(f"{label} reason 过短，必须写具体机制或删除依据")
    if _contains_protected_output(reason):
        errors.append(f"{label} reason 含受保护答案/错选模式")

    if decision == "keep":
        if baseline.get("template_reason"):
            errors.append(f"{label} 模板基线理由不能 decision=keep，须 replace 改写或 remove")
        if TEMPLATE_REASON_RE.match(reason):
            errors.append(f"{label} 模板 review 理由不能 keep")
        supplied = {
            "new_type": edge_type_code(str(record.get("new_type", ""))) if record.get("new_type") else "",
            "new_strength": record.get("new_strength", ""),
            "new_priority": record.get("new_priority", ""),
        }
        expected = {
            "new_type": baseline["current_type"],
            "new_strength": baseline["current_strength"],
            "new_priority": baseline["current_priority"],
        }
        for field, value in supplied.items():
            if value and value != expected[field]:
                errors.append(f"{label} keep 不得改变 {field}；应使用 replace")
    elif decision == "remove":
        if any(record.get(field) not in (None, "") for field in ("new_type", "new_strength", "new_priority")):
            errors.append(f"{label} remove 不得携带 new_type/new_strength/new_priority")
    elif decision == "replace":
        new_type = edge_type_code(str(record.get("new_type", "")))
        if new_type not in EDGE_TYPES:
            errors.append(f"{label} new_type 枚举非法：{record.get('new_type')!r}")
        new_strength = _check_enum(record.get("new_strength"), STRENGTHS, "new_strength", label, errors)
        _check_enum(record.get("new_priority"), PRIORITIES, "new_priority", label, errors)
        if TEMPLATE_REASON_RE.match(reason):
            errors.append(f"{label} replace 后 reason 仍是模板理由")
        if new_type in {"R07", "R08"} and new_strength and new_strength != "弱":
            errors.append(f"{label} {new_type} 只能使用弱关系")


def validate_reviews(
    snapshot: dict[str, Any],
    review_records: list[dict[str, Any]],
    *,
    repo: Path | None = None,
    details_root: Path | None = None,
    initial_errors: Iterable[str] = (),
) -> dict[str, Any]:
    errors = list(initial_errors)
    if snapshot.get("schema_version") != SNAPSHOT_SCHEMA:
        raise ReviewToolError(f"snapshot schema 非法：{snapshot.get('schema_version')!r}")
    repo = (repo or Path(snapshot.get("repo_root", ""))).expanduser().resolve()
    details_root = (details_root or Path(snapshot.get("details_root", ""))).expanduser().resolve()

    baseline_nodes_list = snapshot.get("nodes")
    baseline_edges_list = snapshot.get("edges")
    if not isinstance(baseline_nodes_list, list) or not isinstance(baseline_edges_list, list):
        raise ReviewToolError("snapshot 缺 nodes/edges 数组")
    baseline_nodes = {node.get("id"): node for node in baseline_nodes_list if isinstance(node, dict)}
    baseline_edges = {
        pair_key(str(edge.get("a", "")), str(edge.get("b", ""))): edge
        for edge in baseline_edges_list if isinstance(edge, dict)
    }
    expected_counts = snapshot.get("counts", {})
    if len(baseline_nodes) != expected_counts.get("nodes"):
        errors.append("snapshot 节点列表与 counts.nodes 不一致")
    if len(baseline_edges) != expected_counts.get("edges"):
        errors.append("snapshot 关系列表与 counts.edges 不一致")

    snapshot_evidence = _check_snapshot_hashes(snapshot, repo, details_root, errors)

    node_records: dict[str, list[dict[str, Any]]] = defaultdict(list)
    edge_records: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for record in review_records:
        if record.get("record_type") == "node_review":
            node_records[str(record.get("id", ""))].append(record)
        elif record.get("record_type") == "edge_review":
            edge_records[pair_key(str(record.get("a", "")), str(record.get("b", "")))].append(record)

    for node_id in sorted(baseline_nodes):
        count = len(node_records.get(node_id, []))
        if count != 1:
            errors.append(f"baseline 节点 {node_id} 必须恰好一条 node_review，实际 {count}")
    for node_id in sorted(set(node_records) - set(baseline_nodes)):
        errors.append(f"node_review 含非 baseline 节点：{node_id}")

    for key in sorted(baseline_edges):
        count = len(edge_records.get(key, []))
        if count != 1:
            errors.append(f"baseline 边 {key} 必须恰好一条 edge_review，实际 {count}")
    for key in sorted(set(edge_records) - set(baseline_edges)):
        errors.append(f"edge_review 含非 baseline 题对：{key}")

    for node_id, records in node_records.items():
        if node_id in baseline_nodes and len(records) == 1:
            _validate_node_review(
                records[0], baseline_nodes[node_id], repo, details_root, snapshot_evidence, errors
            )
    for key, records in edge_records.items():
        if key in baseline_edges and len(records) == 1:
            _validate_edge_review(records[0], baseline_edges[key], errors)

    # 待确认身份/证据不能在同一批次中一边标 needs_user，一边把新字段或关系
    # 发布成已确认事实。keep 只表示保留原字段，不产生正式字段变更。
    needs_user_ids: set[str] = set()
    for node_id, records in node_records.items():
        if len(records) != 1:
            continue
        record = records[0]
        if record.get("relation_status") != "needs_user":
            continue
        needs_user_ids.add(node_id)
        if record.get("main_knowledge_decision") == "replace":
            errors.append(f"node_review[{node_id}] needs_user 不得同时 replace 主知识点")
        if record.get("core_point_decision") == "refine":
            errors.append(f"node_review[{node_id}] needs_user 不得同时 refine 核心考点")
    for key, records in edge_records.items():
        if len(records) != 1:
            continue
        record = records[0]
        endpoints = {str(record.get("a", "")), str(record.get("b", ""))}
        if endpoints & needs_user_ids and record.get("decision") != "remove":
            errors.append(f"edge_review[{key}] 含 needs_user 端点，只能 decision=remove")

    report = {
        "schema_version": VALIDATION_SCHEMA,
        "validated_at": dt.datetime.now(dt.timezone.utc).isoformat(),
        "status": "PASS" if not errors else "FAIL",
        "baseline": {
            "nodes": len(baseline_nodes),
            "edges": len(baseline_edges),
        },
        "reviews": {
            "node_review_records": sum(len(records) for records in node_records.values()),
            "edge_review_records": sum(len(records) for records in edge_records.values()),
        },
        "errors": errors,
    }
    return report


def _summary(snapshot: dict[str, Any], written: list[str] | None = None) -> dict[str, Any]:
    return {
        "status": "OK",
        "mode": "write_artifacts" if written is not None else "read_only",
        "schema_version": snapshot["schema_version"],
        "counts": snapshot["counts"],
        "repo_root": snapshot["repo_root"],
        "details_root": snapshot["details_root"],
        "written": written or [],
    }


def _snapshot_command(args: argparse.Namespace) -> int:
    repo = Path(args.repo).expanduser().resolve()
    details_root = Path(args.details_root).expanduser().resolve()
    snapshot = build_snapshot(
        repo,
        details_root,
        expected_nodes=args.expect_nodes,
        expected_edges=args.expect_edges,
    )
    written = None
    if args.out_dir:
        written = write_snapshot_artifacts(
            snapshot,
            Path(args.out_dir),
            overwrite=args.overwrite,
        )
    print(json.dumps(_summary(snapshot, written), ensure_ascii=False, indent=2, sort_keys=True))
    return 0


def _validate_command(args: argparse.Namespace) -> int:
    snapshot = _load_json(Path(args.snapshot).expanduser().resolve())
    records, load_errors = load_review_records(Path(value) for value in args.reviews)
    repo = Path(args.repo).expanduser().resolve() if args.repo else None
    details_root = Path(args.details_root).expanduser().resolve() if args.details_root else None
    report = validate_reviews(
        snapshot,
        records,
        repo=repo,
        details_root=details_root,
        initial_errors=load_errors,
    )
    if args.out_dir:
        resolved_repo = repo or Path(snapshot["repo_root"])
        resolved_details = details_root or Path(snapshot["details_root"])
        out_dir = _validate_out_dir(Path(args.out_dir), resolved_repo, resolved_details)
        _atomic_write(
            out_dir / "validation-report.json",
            json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True).encode("utf-8") + b"\n",
            overwrite=args.overwrite,
        )
    print(json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True))
    return 0 if report["status"] == "PASS" else 1


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)

    snapshot = subparsers.add_parser("snapshot", help="只读冻结当前全库基线；显式 --out-dir 才写 worklist")
    snapshot.add_argument("--repo", default=".")
    snapshot.add_argument("--details-root", default=str(DEFAULT_DETAILS_ROOT))
    snapshot.add_argument("--expect-nodes", type=int)
    snapshot.add_argument("--expect-edges", type=int)
    snapshot.add_argument("--out-dir")
    snapshot.add_argument("--overwrite", action="store_true")
    snapshot.set_defaults(func=_snapshot_command)

    validate = subparsers.add_parser("validate", help="校验全量 node_review / edge_review 与冻结证据")
    validate.add_argument("--snapshot", required=True)
    validate.add_argument("--reviews", nargs="+", required=True)
    validate.add_argument("--repo", default=None)
    validate.add_argument("--details-root", default=None)
    validate.add_argument("--out-dir")
    validate.add_argument("--overwrite", action="store_true")
    validate.set_defaults(func=_validate_command)
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        return int(args.func(args))
    except ReviewToolError as exc:
        print(json.dumps({"status": "ERROR", "error": str(exc)}, ensure_ascii=False), file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
