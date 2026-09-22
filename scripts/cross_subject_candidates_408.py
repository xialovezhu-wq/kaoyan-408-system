#!/usr/bin/env python3
"""Build a deterministic, read-only 408 cross-subject relation shadow catalog.

The generator has two evidence lanes:

* ``verified_existing`` projects already reviewed cross-subject question edges
  from ``关系边表.md`` back to their main knowledge points.
* ``shadow_candidate`` evaluates a small, source-backed mechanism map against
  the current four-subject catalogs.  These records are review candidates only.

The command writes nothing.  JSON and compact Markdown are emitted to stdout;
there is deliberately no apply or output-path option.  In particular, neither
``关系边表.md`` nor any other formal surface is ever mutated by this module.
"""

from __future__ import annotations

import argparse
import fcntl
import hashlib
import json
import os
import re
import sys
import tempfile
from collections import defaultdict
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterable, Iterator


SCHEMA_VERSION = "cross_subject_shadow_v1"
GENERATOR_VERSION = "1.1.0"
DEFAULT_ADJACENCY_LIMIT = 5
MAX_ADJACENCY_LIMIT = 20
DEFAULT_PROJECTION_RELATIVE = Path(
    "wiki/study_vaults/408-full/state/personalization/cross-subject-shadow.json"
)
SUBJECTS = ("DS", "CO", "OS", "CN")
SUBJECT_FILES = {
    "DS": "知识体系/数据结构知识体系.md",
    "CO": "知识体系/计算机组成原理知识体系.md",
    "OS": "知识体系/操作系统知识体系.md",
    "CN": "知识体系/计算机网络知识体系.md",
}
INPUT_FILES = (
    "知识点标签表.md",
    *SUBJECT_FILES.values(),
    "节点总表.md",
    "关系边表.md",
)

KNOWLEDGE_RE = re.compile(r"^-\s+((?:DS|CO|OS|CN)\d{2}-\d{2})\s+(.+?)\s*$")
KNOWLEDGE_VALUE_RE = re.compile(r"^((?:DS|CO|OS|CN)\d{2}-\d{2})(?:\s|$)")
FORMAL_ID_RE = re.compile(r"^(DS|CO|OS|CN)_(?:\d{4}|UNK)_\d{3}$")
RELATION_RE = re.compile(r"^(R0[1-9])(?:\s|$)")
TEMPLATE_REASON_RE = re.compile(
    r"^\s*(?:同一主知识点|同属专题链|同一模糊概念)\s*[：:]"
)
PROTECTED_FRAGMENT_RE = re.compile(
    r"(?:正确答案|标准答案|错误答案|我的答案|我的选择)\s*[：:]\s*[^；。,.，]{0,80}"
    r"|(?:错选|误选|应选|故选)\s*[A-DＡ-Ｄ]"
    r"|答案(?:是|为)\s*[A-DＡ-Ｄ]"
)

RELATION_NAMES = {
    "R01": "同一核心考点",
    "R02": "同一题型模板",
    "R03": "同一错因标签",
    "R04": "同一模糊概念",
    "R05": "易混概念对比",
    "R06": "上下游知识链",
    "R07": "跨科目相似思想",
    "R08": "同年份同模块",
    "R09": "高频考点聚类",
}
RELATION_CATEGORIES = {
    "R01": "same_core_mechanism",
    "R02": "same_solution_pattern",
    "R03": "same_observed_error_mode",
    "R04": "shared_concept_boundary",
    "R05": "contrastive_concept_boundary",
    "R06": "prerequisite_or_pipeline",
    "R07": "cross_subject_analogy",
    "R08": "same_year_module",
    "R09": "high_frequency_cluster",
}
STRENGTH_RANK = {"弱": 0, "中": 1, "强": 2}


class ShadowCatalogError(RuntimeError):
    """The source catalog is incomplete or structurally unsafe to consume."""


def _retired_write_surface() -> None:
    raise ShadowCatalogError(
        "cross_subject_projection_writes_retired_read_only_compatibility"
    )


# This is an explicit mechanism map, not a keyword matcher.  Every proposal has
# a mechanism-level explanation and exact endpoints that must exist in both the
# consolidated catalog and the corresponding subject-system source.  The map is
# intentionally conservative: even high-confidence entries remain shadow-only
# until separately reviewed and promoted through the formal relation workflow.
MECHANISM_SPECS: tuple[dict[str, str], ...] = (
    {
        "left": "CO03-25",
        "right": "OS03-18",
        "relation_code": "R06",
        "direction_kind": "left_to_right",
        "confidence": "high",
        "strength": "强",
        "evidence_summary": (
            "组成原理给出 TLB 的硬件查找与命中边界，操作系统在分页地址变换中使用同一快表机制；"
            "前者是理解后者访存路径与代价分支的直接前置。"
        ),
    },
    {
        "left": "CO03-13",
        "right": "OS03-24",
        "relation_code": "R06",
        "direction_kind": "left_to_right",
        "confidence": "high",
        "strength": "强",
        "evidence_summary": (
            "程序局部性解释请求分页为何能按需调页并保持可接受的命中行为，构成从硬件存储层次原理"
            "到操作系统请求分页策略的因果前置。"
        ),
    },
    {
        "left": "CO03-18",
        "right": "OS03-27",
        "relation_code": "R07",
        "direction_kind": "bidirectional",
        "confidence": "medium",
        "strength": "中",
        "evidence_summary": (
            "Cache 块替换与页面置换共享有限容量下选择淘汰对象的抽象，但对象、触发条件和代价不同；"
            "只能用于机制迁移检查，不能据此认定为同一考点。"
        ),
    },
    {
        "left": "CO05-19",
        "right": "OS01-16",
        "relation_code": "R06",
        "direction_kind": "left_to_right",
        "confidence": "high",
        "strength": "强",
        "evidence_summary": (
            "组成原理描述中断和异常的检测、请求与硬件入口，操作系统继续处理核心态入口和服务语义；"
            "两端构成硬件事件到系统软件处理的责任链。"
        ),
    },
    {
        "left": "CO06-14",
        "right": "OS05-13",
        "relation_code": "R06",
        "direction_kind": "left_to_right",
        "confidence": "high",
        "strength": "强",
        "evidence_summary": (
            "硬件中断响应过程先完成入口与最小现场动作，操作系统中断处理程序随后执行设备服务；"
            "该关系用于区分硬件自动动作和软件服务动作。"
        ),
    },
    {
        "left": "CO06-18",
        "right": "OS05-08",
        "relation_code": "R06",
        "direction_kind": "left_to_right",
        "confidence": "high",
        "strength": "强",
        "evidence_summary": (
            "组成原理给出 DMA 控制器、总线占用和传送过程，操作系统把 DMA 放入 I/O 控制方式与设备管理；"
            "硬件机制是判断系统软件控制边界的前置。"
        ),
    },
    {
        "left": "DS06-18",
        "right": "CN04-10",
        "relation_code": "R06",
        "direction_kind": "left_to_right",
        "confidence": "high",
        "strength": "强",
        "evidence_summary": (
            "Dijkstra 的最短路径状态更新是链路状态路由计算最短路径树的算法核心；"
            "数据结构算法是理解网络协议计算阶段的直接前置。"
        ),
    },
    {
        "left": "DS06-17",
        "right": "CN04-06",
        "relation_code": "R07",
        "direction_kind": "left_to_right",
        "confidence": "medium",
        "strength": "中",
        "evidence_summary": (
            "图最短路径为路由算法提供抽象问题模型，但实际路由还受分布式信息、收敛和协议状态约束；"
            "适合迁移检查，不可只凭“路径”词面直接启用。"
        ),
    },
    {
        "left": "DS03-09",
        "right": "OS05-20",
        "relation_code": "R06",
        "direction_kind": "left_to_right",
        "confidence": "medium",
        "strength": "中",
        "evidence_summary": (
            "循环队列的首尾指针与判空判满约定，是理解 I/O 循环缓冲区槽位复用的实现基础；"
            "应用层仍需额外核对生产者消费者和缓冲管理语义。"
        ),
    },
    {
        "left": "DS03-07",
        "right": "OS02-20",
        "relation_code": "R07",
        "direction_kind": "left_to_right",
        "confidence": "low",
        "strength": "弱",
        "evidence_summary": (
            "调度实现常使用队列保存候选实体，但队列数据结构本身不能推出调度策略、抢占条件或公平性；"
            "仅保留为低置信教学类比，不进入自动调题。"
        ),
    },
    {
        "left": "OS01-09",
        "right": "CN01-18",
        "relation_code": "R07",
        "direction_kind": "bidirectional",
        "confidence": "low",
        "strength": "弱",
        "evidence_summary": (
            "操作系统分层和网络体系结构都使用接口隔离复杂性，但分层对象与通信语义不同；"
            "该联系只能帮助解释抽象思想，不能作为知识掌握迁移证据。"
        ),
    },
    {
        "left": "CO05-22",
        "right": "CN01-08",
        "relation_code": "R07",
        "direction_kind": "bidirectional",
        "confidence": "low",
        "strength": "弱",
        "evidence_summary": (
            "指令流水线和分组交换都可能呈现阶段重叠后的吞吐效果，但阻塞、缓存和完成条件完全不同；"
            "仅用于低置信时间线类比，禁止据此自动启用。"
        ),
    },
    {
        "left": "CO01-05",
        "right": "CN01-18",
        "relation_code": "R07",
        "direction_kind": "bidirectional",
        "confidence": "low",
        "strength": "弱",
        "evidence_summary": (
            "计算机系统层次与网络体系结构都通过抽象层提供服务，但层次职责不可互换；"
            "这是教学类比而非直接机制链。"
        ),
    },
)


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _safe_text(value: str, limit: int = 300) -> str:
    text = re.sub(r"\s+", " ", (value or "").strip())
    text = PROTECTED_FRAGMENT_RE.sub("[受保护内容已隐藏]", text)
    return text[:limit]


def _split_markdown_row(line: str) -> list[str]:
    stripped = line.strip()
    if not (stripped.startswith("|") and stripped.endswith("|")):
        return []
    cells = re.split(r"(?<!\\)\|", stripped[1:-1])
    return [cell.replace(r"\|", "|").strip() for cell in cells]


def _is_separator(cells: list[str]) -> bool:
    return bool(cells) and all(re.fullmatch(r":?-{2,}:?", cell or "") for cell in cells)


def read_markdown_table(path: Path, first_column: str) -> list[dict[str, Any]]:
    if not path.is_file():
        raise ShadowCatalogError(f"缺少必需文件：{path}")
    lines = path.read_text(encoding="utf-8").splitlines()
    for index, line in enumerate(lines[:-1]):
        headers = _split_markdown_row(line)
        if not headers or headers[0] != first_column:
            continue
        if not _is_separator(_split_markdown_row(lines[index + 1])):
            continue
        rows: list[dict[str, Any]] = []
        for line_number, row_line in enumerate(lines[index + 2 :], start=index + 3):
            cells = _split_markdown_row(row_line)
            if not cells:
                break
            if len(cells) != len(headers):
                raise ShadowCatalogError(
                    f"{path} 第 {line_number} 行列数异常：应为 {len(headers)}，实际 {len(cells)}"
                )
            row: dict[str, Any] = dict(zip(headers, cells))
            row["_line"] = line_number
            rows.append(row)
        return rows
    raise ShadowCatalogError(f"{path} 找不到首列为 {first_column!r} 的 Markdown 表格")


def read_knowledge_file(path: Path, relative_path: str) -> dict[str, dict[str, Any]]:
    if not path.is_file():
        raise ShadowCatalogError(f"缺少必需文件：{path}")
    points: dict[str, dict[str, Any]] = {}
    for line_number, raw in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
        match = KNOWLEDGE_RE.match(raw)
        if not match:
            continue
        point_id, name = match.groups()
        if point_id in points:
            raise ShadowCatalogError(f"{relative_path} 中知识点重复：{point_id}")
        points[point_id] = {
            "id": point_id,
            "name": name.strip(),
            "subject": point_id[:2],
            "path": relative_path,
            "line": line_number,
        }
    if not points:
        raise ShadowCatalogError(f"{relative_path} 未解析到知识点")
    return points


def _knowledge_id(value: str) -> str:
    match = KNOWLEDGE_VALUE_RE.match((value or "").strip())
    return match.group(1) if match else ""


def _relation_code(value: str) -> str:
    match = RELATION_RE.match((value or "").strip())
    return match.group(1) if match else ""


def _formal_subject(value: str) -> str:
    match = FORMAL_ID_RE.match((value or "").strip())
    return match.group(1) if match else ""


def _manifest(repo: Path) -> list[dict[str, Any]]:
    manifest = []
    for relative in INPUT_FILES:
        path = repo / relative
        if not path.is_file():
            raise ShadowCatalogError(f"缺少必需输入：{relative}")
        manifest.append(
            {
                "path": relative,
                "sha256": sha256_file(path),
                "size": path.stat().st_size,
            }
        )
    return manifest


def _source_fingerprint(manifest: Iterable[dict[str, Any]]) -> str:
    compact = [{"path": item["path"], "sha256": item["sha256"]} for item in manifest]
    encoded = json.dumps(compact, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def _canonical_endpoints(
    left: str,
    right: str,
    direction_kind: str,
) -> tuple[str, str, str, str]:
    """Return display endpoints plus explicit direction endpoints."""
    if direction_kind == "bidirectional":
        display_left, display_right = sorted((left, right))
        return display_left, display_right, display_left, display_right
    if direction_kind in {"left_to_right", "formal_edge_order"}:
        return left, right, left, right
    raise ShadowCatalogError(f"未知方向类型：{direction_kind}")


def _semantic_key(
    left: str,
    right: str,
    relation_code: str,
    direction_kind: str,
) -> tuple[str, str, str, str, str, str]:
    display_left, display_right, source, target = _canonical_endpoints(
        left, right, direction_kind
    )
    normalized_kind = "bidirectional" if direction_kind == "bidirectional" else "directed"
    return display_left, display_right, relation_code, normalized_kind, source, target


def stable_relation_id(
    left: str,
    right: str,
    relation_code: str,
    direction_kind: str,
) -> str:
    payload = "|".join(_semantic_key(left, right, relation_code, direction_kind))
    digest = hashlib.sha256(payload.encode("utf-8")).hexdigest()[:16].upper()
    return f"CSR408-{digest}"


def _point_payload(point: dict[str, Any]) -> dict[str, str]:
    return {"id": point["id"], "name": point["name"], "subject": point["subject"]}


def _knowledge_refs(
    point_ids: Iterable[str],
    catalog: dict[str, dict[str, Any]],
    systems: dict[str, dict[str, Any]],
) -> list[str]:
    refs: set[str] = set()
    for point_id in point_ids:
        catalog_point = catalog[point_id]
        system_point = systems[point_id]
        refs.add(f"{catalog_point['path']}:L{catalog_point['line']}")
        refs.add(f"{system_point['path']}:L{system_point['line']}")
    return sorted(refs)


def _activation_fields(
    lifecycle: str,
    confidence: str,
    relation_code: str,
    strength: str,
) -> tuple[bool, str]:
    if lifecycle != "verified_existing":
        return False, "影子候选必须先完成人工或正式语义复核，不能直接参与实时调题。"
    if confidence == "low" or strength == "弱":
        return False, "低置信或弱关系即使已有正式题对，也不进入自动跨科调题。"
    if relation_code == "R07":
        return False, "R07 仅表示跨科类比，默认不得作为自动调题的机制依据。"
    return True, "已有具体正式跨科题对证据，可进入后续只读影子路由评测。"


def _record(
    *,
    left_id: str,
    right_id: str,
    relation_code: str,
    direction_kind: str,
    lifecycle: str,
    confidence: str,
    strength: str,
    evidence_summary: str,
    source_refs: list[str],
    generation_method: str,
    catalog: dict[str, dict[str, Any]],
) -> dict[str, Any]:
    display_left, display_right, source, target = _canonical_endpoints(
        left_id, right_id, direction_kind
    )
    eligible, activation_reason = _activation_fields(
        lifecycle, confidence, relation_code, strength
    )
    edge_id = stable_relation_id(left_id, right_id, relation_code, direction_kind)
    refs = sorted(set(source_refs))
    return {
        "id": edge_id,
        # Compatibility fields consumed by personalization_index_408.py.  The
        # richer nested representation below remains the review surface.
        "edge_id": edge_id,
        "source_point_id": source,
        "target_point_id": target,
        "subjects": [catalog[source]["subject"], catalog[target]["subject"]],
        "relation_kind": RELATION_CATEGORIES[relation_code],
        "relation_code": relation_code,
        "lifecycle": lifecycle,
        "left": _point_payload(catalog[display_left]),
        "right": _point_payload(catalog[display_right]),
        "relation": {
            "code": relation_code,
            "name": RELATION_NAMES[relation_code],
            "category": RELATION_CATEGORIES[relation_code],
            "strength": strength,
        },
        "direction": {
            "kind": "bidirectional" if direction_kind == "bidirectional" else "directed",
            "from": source,
            "to": target,
        },
        "evidence_summary": _safe_text(evidence_summary),
        "confidence": confidence,
        "source_refs": refs,
        "evidence_refs": list(refs),
        "generation_method": generation_method,
        "review_state": (
            "formal_edge_present" if lifecycle == "verified_existing" else "pending_semantic_review"
        ),
        "activation_eligible": eligible,
        "activation": (
            "verified_existing" if lifecycle == "verified_existing" else "shadow_only"
        ),
        "enabled_for_live_routing": False,
        "activation_reason": activation_reason,
    }


def _confidence_for_formal(code: str, strength: str) -> str:
    if strength == "弱" or code == "R07":
        return "low"
    if strength == "强":
        return "high"
    return "medium"


def _strongest(values: Iterable[str]) -> str:
    present = [value for value in values if value in STRENGTH_RANK]
    return max(present, key=lambda value: STRENGTH_RANK[value]) if present else "弱"


def _load_sources(repo: Path) -> tuple[
    dict[str, dict[str, Any]],
    dict[str, dict[str, Any]],
    list[dict[str, Any]],
    list[dict[str, Any]],
    list[str],
]:
    diagnostics: list[str] = []
    catalog = read_knowledge_file(repo / "知识点标签表.md", "知识点标签表.md")
    systems: dict[str, dict[str, Any]] = {}
    for subject, relative in SUBJECT_FILES.items():
        subject_points = read_knowledge_file(repo / relative, relative)
        wrong_prefix = sorted(point_id for point_id in subject_points if not point_id.startswith(subject))
        if wrong_prefix:
            raise ShadowCatalogError(f"{relative} 含错误科目前缀：{wrong_prefix[:3]}")
        overlap = set(systems) & set(subject_points)
        if overlap:
            raise ShadowCatalogError(f"四科知识体系出现重复 ID：{sorted(overlap)[:3]}")
        systems.update(subject_points)

    for point_id in sorted(set(catalog) & set(systems)):
        if catalog[point_id]["name"] != systems[point_id]["name"]:
            diagnostics.append(
                f"名称口径不一致：{point_id}；目录={catalog[point_id]['name']}；"
                f"知识体系={systems[point_id]['name']}"
            )
    for point_id in sorted(set(catalog) - set(systems)):
        diagnostics.append(f"目录知识点未进入科目知识体系：{point_id}")
    for point_id in sorted(set(systems) - set(catalog)):
        diagnostics.append(f"科目知识体系知识点未进入目录：{point_id}")

    nodes = read_markdown_table(repo / "节点总表.md", "ID")
    edges = read_markdown_table(repo / "关系边表.md", "起点ID")
    return catalog, systems, nodes, edges, diagnostics


def _build_verified_records(
    *,
    catalog: dict[str, dict[str, Any]],
    systems: dict[str, dict[str, Any]],
    nodes: list[dict[str, Any]],
    edges: list[dict[str, Any]],
    diagnostics: list[str],
) -> tuple[list[dict[str, Any]], dict[tuple[str, str, str, str, str, str], dict[str, Any]]]:
    node_map = {str(row.get("ID", "")): row for row in nodes}
    groups: dict[tuple[str, str, str, str, str, str], list[dict[str, Any]]] = defaultdict(list)

    for edge in edges:
        start = str(edge.get("起点ID", "")).strip()
        end = str(edge.get("终点ID", "")).strip()
        start_subject = _formal_subject(start)
        end_subject = _formal_subject(end)
        if not start_subject or not end_subject or start_subject == end_subject:
            continue
        relation_code = _relation_code(str(edge.get("关系类型", "")))
        if not relation_code:
            diagnostics.append(f"跨科正式边关系类型无法解析：关系边表.md:L{edge['_line']}")
            continue
        reason = str(edge.get("关联原因", "")).strip()
        if TEMPLATE_REASON_RE.match(reason):
            diagnostics.append(f"模板理由跨科边未投影为 verified：关系边表.md:L{edge['_line']}")
            continue
        start_node = node_map.get(start)
        end_node = node_map.get(end)
        if not start_node or not end_node:
            diagnostics.append(f"跨科正式边端点不在节点总表：{start} / {end}")
            continue
        left_id = _knowledge_id(str(start_node.get("主知识点", "")))
        right_id = _knowledge_id(str(end_node.get("主知识点", "")))
        if not left_id or not right_id:
            diagnostics.append(f"跨科正式边主知识点无法归一：{start} / {end}")
            continue
        if left_id not in catalog or right_id not in catalog:
            diagnostics.append(f"跨科正式边知识点不在总目录：{left_id or '?'} / {right_id or '?'}")
            continue
        if left_id not in systems or right_id not in systems:
            diagnostics.append(f"跨科正式边知识点缺科目体系来源：{left_id} / {right_id}")
            continue

        direction_kind = "formal_edge_order" if relation_code == "R06" else "bidirectional"
        key = _semantic_key(left_id, right_id, relation_code, direction_kind)
        groups[key].append(
            {
                "left": left_id,
                "right": right_id,
                "direction_kind": direction_kind,
                "relation_code": relation_code,
                "strength": str(edge.get("联系强度", "")).strip(),
                "reason": reason,
                "edge_ref": f"关系边表.md:L{edge['_line']}",
                "start_node_ref": f"节点总表.md:L{start_node['_line']}",
                "end_node_ref": f"节点总表.md:L{end_node['_line']}",
            }
        )

    records: list[dict[str, Any]] = []
    by_key: dict[tuple[str, str, str, str, str, str], dict[str, Any]] = {}
    for key in sorted(groups):
        evidence = groups[key]
        base = evidence[0]
        strength = _strongest(item["strength"] for item in evidence)
        reasons = []
        for item in evidence:
            safe = _safe_text(item["reason"], 220)
            if safe and safe not in reasons:
                reasons.append(safe)
        summary = "；".join(reasons[:2])
        if len(evidence) > 1:
            summary += f"；已有 {len(evidence)} 条正式跨科题对证据。"
        refs = _knowledge_refs((base["left"], base["right"]), catalog, systems)
        for item in evidence:
            refs.extend((item["edge_ref"], item["start_node_ref"], item["end_node_ref"]))
        record = _record(
            left_id=base["left"],
            right_id=base["right"],
            relation_code=base["relation_code"],
            direction_kind=base["direction_kind"],
            lifecycle="verified_existing",
            confidence=_confidence_for_formal(base["relation_code"], strength),
            strength=strength,
            evidence_summary=summary,
            source_refs=refs,
            generation_method="formal_edge_projection",
            catalog=catalog,
        )
        record["formal_edge_count"] = len(evidence)
        records.append(record)
        by_key[key] = record
    return records, by_key


def _build_shadow_records(
    *,
    catalog: dict[str, dict[str, Any]],
    systems: dict[str, dict[str, Any]],
    verified_by_key: dict[tuple[str, str, str, str, str, str], dict[str, Any]],
    diagnostics: list[str],
) -> list[dict[str, Any]]:
    shadow: list[dict[str, Any]] = []
    seen_spec_keys: set[tuple[str, str, str, str, str, str]] = set()
    for spec_index, spec in enumerate(MECHANISM_SPECS, start=1):
        left_id = spec["left"]
        right_id = spec["right"]
        missing_catalog = [point_id for point_id in (left_id, right_id) if point_id not in catalog]
        missing_system = [point_id for point_id in (left_id, right_id) if point_id not in systems]
        if missing_catalog or missing_system:
            diagnostics.append(
                f"机制规格 {spec_index} 暂不可生成：目录缺失={missing_catalog or '无'}；"
                f"知识体系缺失={missing_system or '无'}"
            )
            continue
        if catalog[left_id]["subject"] == catalog[right_id]["subject"]:
            raise ShadowCatalogError(f"机制规格 {spec_index} 不是跨科关系：{left_id} / {right_id}")
        relation_code = spec["relation_code"]
        if relation_code not in RELATION_NAMES:
            raise ShadowCatalogError(f"机制规格 {spec_index} 关系类型非法：{relation_code}")
        if spec["confidence"] not in {"high", "medium", "low"}:
            raise ShadowCatalogError(f"机制规格 {spec_index} 置信度非法")
        if spec["strength"] not in STRENGTH_RANK:
            raise ShadowCatalogError(f"机制规格 {spec_index} 联系强度非法")
        key = _semantic_key(left_id, right_id, relation_code, spec["direction_kind"])
        if key in seen_spec_keys:
            raise ShadowCatalogError(f"机制规格出现重复语义键：{left_id} / {right_id} / {relation_code}")
        seen_spec_keys.add(key)

        if key in verified_by_key:
            verified = verified_by_key[key]
            verified["mechanism_spec_match"] = True
            verified["source_refs"] = sorted(
                set(verified["source_refs"] + [f"generator:MECHANISM_SPECS[{spec_index}]"])
            )
            verified["evidence_refs"] = list(verified["source_refs"])
            continue

        refs = _knowledge_refs((left_id, right_id), catalog, systems)
        refs.append(f"generator:MECHANISM_SPECS[{spec_index}]")
        shadow.append(
            _record(
                left_id=left_id,
                right_id=right_id,
                relation_code=relation_code,
                direction_kind=spec["direction_kind"],
                lifecycle="shadow_candidate",
                confidence=spec["confidence"],
                strength=spec["strength"],
                evidence_summary=spec["evidence_summary"],
                source_refs=refs,
                generation_method="curated_mechanism_spec",
                catalog=catalog,
            )
        )
    return shadow


def _adjacency_item(record: dict[str, Any], target_point_id: str) -> dict[str, Any]:
    return {
        "edge_id": record["edge_id"],
        "target_point_id": target_point_id,
        "relation_kind": record["relation_kind"],
        "relation_code": record["relation_code"],
        "confidence": record["confidence"],
        "activation": record["activation"],
        "activation_eligible": record["activation_eligible"],
        "enabled_for_live_routing": record["enabled_for_live_routing"],
    }


def _bounded_adjacency(
    records: Iterable[dict[str, Any]],
    limit: int,
) -> tuple[dict[str, list[dict[str, Any]]], int]:
    """Compile bounded point neighbors without producing a Cartesian product."""
    if not 1 <= limit <= MAX_ADJACENCY_LIMIT:
        raise ShadowCatalogError(
            f"adjacency_limit 必须在 1..{MAX_ADJACENCY_LIMIT}，实际为 {limit}"
        )
    confidence_rank = {"high": 0, "medium": 1, "low": 2}
    strength_rank = {"强": 0, "中": 1, "弱": 2}
    pending: dict[str, list[tuple[tuple[Any, ...], dict[str, Any]]]] = defaultdict(list)
    for record in records:
        source = record["source_point_id"]
        target = record["target_point_id"]
        rank = (
            0 if record["enabled_for_live_routing"] else 1,
            0 if record["activation_eligible"] else 1,
            0 if record["lifecycle"] == "verified_existing" else 1,
            confidence_rank[record["confidence"]],
            strength_rank[record["relation"]["strength"]],
            record["edge_id"],
        )
        pending[source].append((rank, _adjacency_item(record, target)))
        if record["direction"]["kind"] == "bidirectional":
            pending[target].append((rank, _adjacency_item(record, source)))

    adjacency: dict[str, list[dict[str, Any]]] = {}
    trimmed = 0
    for source in sorted(pending):
        ordered = [item for _, item in sorted(pending[source], key=lambda pair: pair[0])]
        trimmed += max(0, len(ordered) - limit)
        adjacency[source] = ordered[:limit]
    return adjacency, trimmed


def _apply_live_allowlist(
    records: list[dict[str, Any]], enabled_edge_ids: Iterable[str]
) -> None:
    """Enable only explicitly named, already verified and eligible edges.

    This mutates the disposable projection only.  It does not create or promote a
    formal relation.  Unknown, shadow, weak, low-confidence, or otherwise ineligible
    IDs fail closed so an operator cannot turn a candidate into a live route merely
    by knowing its stable ID.
    """

    requested = {str(value).strip() for value in enabled_edge_ids if str(value).strip()}
    if not requested:
        return
    by_id = {str(record["edge_id"]): record for record in records}
    unknown = sorted(requested - set(by_id))
    if unknown:
        raise ShadowCatalogError(
            "实时调题 allowlist 包含未知关系：" + "、".join(unknown)
        )
    for edge_id in sorted(requested):
        record = by_id[edge_id]
        if (
            record.get("lifecycle") != "verified_existing"
            or not bool(record.get("activation_eligible"))
        ):
            raise ShadowCatalogError(
                f"关系 {edge_id} 尚未满足 verified_existing 激活门禁"
            )
        record["enabled_for_live_routing"] = True
        record["activation"] = "verified_live"
        record["activation_reason"] = (
            "该既有正式关系已被显式加入派生实时调题 allowlist；"
            "仍不修改正式关系边。"
        )


def _build_shadow_catalog_with_live_allowlist(
    repo: Path | str,
    adjacency_limit: int = DEFAULT_ADJACENCY_LIMIT,
    *,
    enabled_live_edge_ids: Iterable[str] = (),
) -> dict[str, Any]:
    root = Path(repo).resolve()
    manifest = _manifest(root)
    catalog, systems, nodes, edges, diagnostics = _load_sources(root)
    verified, verified_by_key = _build_verified_records(
        catalog=catalog,
        systems=systems,
        nodes=nodes,
        edges=edges,
        diagnostics=diagnostics,
    )
    shadow = _build_shadow_records(
        catalog=catalog,
        systems=systems,
        verified_by_key=verified_by_key,
        diagnostics=diagnostics,
    )
    records = sorted(
        [*verified, *shadow],
        key=lambda item: (
            0 if item["lifecycle"] == "verified_existing" else 1,
            item["id"],
        ),
    )
    ids = [record["id"] for record in records]
    if len(ids) != len(set(ids)):
        raise ShadowCatalogError("稳定关系 ID 发生碰撞或重复")

    _apply_live_allowlist(records, enabled_live_edge_ids)

    subject_pairs: dict[str, int] = defaultdict(int)
    covered: set[str] = set()
    for record in records:
        subjects = sorted((record["left"]["subject"], record["right"]["subject"]))
        subject_pairs["-".join(subjects)] += 1
        covered.update(subjects)

    routing_adjacency, adjacency_trimmed = _bounded_adjacency(records, adjacency_limit)

    counts = {
        "knowledge_catalog_points": len(catalog),
        "subject_system_points": len(systems),
        "formal_nodes": len(nodes),
        "formal_edges": len(edges),
        "records": len(records),
        "verified_existing": sum(r["lifecycle"] == "verified_existing" for r in records),
        "shadow_candidate": sum(r["lifecycle"] == "shadow_candidate" for r in records),
        "activation_eligible": sum(bool(r["activation_eligible"]) for r in records),
        "live_enabled": sum(bool(r["enabled_for_live_routing"]) for r in records),
        "low_confidence": sum(r["confidence"] == "low" for r in records),
        "adjacency_points": len(routing_adjacency),
        "adjacency_entries": sum(len(items) for items in routing_adjacency.values()),
        "adjacency_trimmed": adjacency_trimmed,
    }
    return {
        "schema_version": SCHEMA_VERSION,
        "generator_version": GENERATOR_VERSION,
        "mode": "read_only_shadow_generation",
        "formal_write_count": 0,
        "source_fingerprint": _source_fingerprint(manifest),
        "source_manifest": manifest,
        "counts": counts,
        "subjects_covered": [subject for subject in SUBJECTS if subject in covered],
        "subject_pairs": dict(sorted(subject_pairs.items())),
        "adjacency_limit": adjacency_limit,
        "routing_adjacency": routing_adjacency,
        "records": records,
        "diagnostics": diagnostics,
    }


def build_shadow_catalog(
    repo: Path | str,
    adjacency_limit: int = DEFAULT_ADJACENCY_LIMIT,
) -> dict[str, Any]:
    """Build the public read-only catalog with every live flag disabled.

    Live allowlists are deliberately unavailable from this public builder and
    its CLI.  Only ``cross_shadow_activation_408`` may call the private builder
    after validating its signed, cumulative evaluation ledger.
    """

    return _build_shadow_catalog_with_live_allowlist(
        repo,
        adjacency_limit=adjacency_limit,
    )


def validate_projection(
    repo: Path | str,
    payload: dict[str, Any],
) -> dict[str, Any]:
    """Strictly prove that a projection is a complete current-source rebuild.

    Live routing remains an explicit disposable allowlist: the only state carried
    into the rebuild is the set of already verified, eligible records whose
    ``enabled_for_live_routing`` flag is true.  Every other field must match a
    fresh deterministic rebuild from the repository sources.
    """

    root = Path(repo).resolve()
    if not isinstance(payload, dict):
        raise ShadowCatalogError("跨科投影必须是 JSON object")
    if payload.get("schema_version") != SCHEMA_VERSION:
        raise ShadowCatalogError("跨科投影 schema 不匹配")
    try:
        formal_write_count = int(payload.get("formal_write_count", -1))
    except (TypeError, ValueError) as exc:
        raise ShadowCatalogError("跨科投影 formal_write_count 非法") from exc
    if formal_write_count != 0:
        raise ShadowCatalogError("跨科投影越过了派生边界")
    records = payload.get("records")
    if not isinstance(records, list):
        raise ShadowCatalogError("跨科投影 records 缺失")
    try:
        adjacency_limit = int(payload.get("adjacency_limit"))
    except (TypeError, ValueError) as exc:
        raise ShadowCatalogError("跨科投影 adjacency_limit 非法") from exc
    if not 1 <= adjacency_limit <= MAX_ADJACENCY_LIMIT:
        raise ShadowCatalogError("跨科投影 adjacency_limit 越界")

    enabled_edge_ids: list[str] = []
    for record in records:
        if not isinstance(record, dict):
            raise ShadowCatalogError("跨科投影 record 非法")
        if not bool(record.get("enabled_for_live_routing")):
            continue
        edge_id = str(record.get("edge_id") or "").strip()
        if not edge_id:
            raise ShadowCatalogError("实时跨科关系缺少 edge_id")
        if (
            record.get("lifecycle") != "verified_existing"
            or not bool(record.get("activation_eligible"))
        ):
            raise ShadowCatalogError("实时跨科关系未通过 verified 门禁")
        enabled_edge_ids.append(edge_id)

    expected = _build_shadow_catalog_with_live_allowlist(
        root,
        adjacency_limit=adjacency_limit,
        enabled_live_edge_ids=enabled_edge_ids,
    )
    if _canonical_json_bytes(payload) != _canonical_json_bytes(expected):
        raise ShadowCatalogError(
            "跨科投影与当前 source manifest、fingerprint 或完整重建不一致"
        )
    return expected


def _markdown_cell(value: Any, limit: int = 90) -> str:
    text = _safe_text(str(value), limit).replace("|", "\\|")
    return text


def render_markdown(payload: dict[str, Any]) -> str:
    counts = payload["counts"]
    lines = [
        "# 408 跨科关系影子审阅面",
        "",
        f"- schema：`{payload['schema_version']}`",
        f"- source fingerprint：`{payload['source_fingerprint']}`",
        f"- 正式写入数：{payload['formal_write_count']}",
        (
            f"- 记录：{counts['records']}；已存在正式证据：{counts['verified_existing']}；"
            f"影子候选：{counts['shadow_candidate']}；可进入后续影子路由评测："
            f"{counts['activation_eligible']}"
        ),
        f"- 科目覆盖：{'、'.join(payload['subjects_covered']) or '无'}",
        "",
        "|稳定ID|状态|知识点方向|关系|置信度|后续评测资格|证据摘要|",
        "|---|---|---|---|---|---|---|",
    ]
    for record in payload["records"]:
        direction = record["direction"]
        arrow = "↔" if direction["kind"] == "bidirectional" else "→"
        endpoints = f"{direction['from']} {arrow} {direction['to']}"
        relation = f"{record['relation']['code']} {record['relation']['name']}"
        lines.append(
            "|"
            + "|".join(
                (
                    record["id"],
                    record["lifecycle"],
                    endpoints,
                    relation,
                    record["confidence"],
                    "eligible" if record["activation_eligible"] else "shadow_only",
                    _markdown_cell(record["evidence_summary"]),
                )
            )
            + "|"
        )
    if payload["diagnostics"]:
        lines.extend(("", "## 数据诊断", ""))
        lines.extend(f"- {_markdown_cell(item, 180)}" for item in payload["diagnostics"])
    lines.extend(
        (
            "",
            f"实时调题 allowlist 已启用 {counts.get('live_enabled', 0)} 条；"
            "未显式启用的记录保持 `enabled_for_live_routing=false`。"
            "本审阅面不修改正式关系边。",
        )
    )
    return "\n".join(lines) + "\n"


def _filter_lifecycle(payload: dict[str, Any], lifecycle: str) -> dict[str, Any]:
    if lifecycle == "all":
        return payload
    copied = dict(payload)
    copied["records"] = [
        record for record in payload["records"] if record["lifecycle"] == lifecycle
    ]
    copied["counts"] = dict(payload["counts"])
    copied["counts"]["records"] = len(copied["records"])
    copied["counts"]["verified_existing"] = sum(
        record["lifecycle"] == "verified_existing" for record in copied["records"]
    )
    copied["counts"]["shadow_candidate"] = sum(
        record["lifecycle"] == "shadow_candidate" for record in copied["records"]
    )
    copied["counts"]["activation_eligible"] = sum(
        bool(record["activation_eligible"]) for record in copied["records"]
    )
    copied["counts"]["live_enabled"] = sum(
        bool(record["enabled_for_live_routing"]) for record in copied["records"]
    )
    copied["counts"]["low_confidence"] = sum(
        record["confidence"] == "low" for record in copied["records"]
    )
    covered: set[str] = set()
    subject_pairs: dict[str, int] = defaultdict(int)
    for record in copied["records"]:
        subjects = sorted(record["subjects"])
        covered.update(subjects)
        subject_pairs["-".join(subjects)] += 1
    copied["subjects_covered"] = [subject for subject in SUBJECTS if subject in covered]
    copied["subject_pairs"] = dict(sorted(subject_pairs.items()))
    # Lifecycle filtering is an audit view.  Avoid showing an adjacency compiled
    # from records that are no longer present in that view.
    adjacency, trimmed = _bounded_adjacency(
        copied["records"], int(copied["adjacency_limit"])
    )
    copied["routing_adjacency"] = adjacency
    copied["counts"]["adjacency_points"] = len(adjacency)
    copied["counts"]["adjacency_entries"] = sum(len(items) for items in adjacency.values())
    copied["counts"]["adjacency_trimmed"] = trimmed
    return copied


def _canonical_json_bytes(value: Any) -> bytes:
    return (
        json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        + "\n"
    ).encode("utf-8")


@contextmanager
def _projection_lock(state_dir: Path) -> Iterator[None]:
    lock_path = state_dir / ".hot-path.lock"
    with lock_path.open("a+", encoding="utf-8") as handle:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def _write_projection(
    repo: Path | str,
    payload: dict[str, Any],
    output_path: Path | str | None = None,
    *,
    activation_internal: bool = False,
) -> dict[str, Any]:
    """Atomically write the disposable projection, never a formal source."""
    _retired_write_surface()

    # Historical implementation retained below for read-only source compatibility.
    root = Path(repo).resolve()
    allowed_target = (root / DEFAULT_PROJECTION_RELATIVE).resolve()
    try:
        allowed_target.relative_to(root)
    except ValueError as exc:
        raise ShadowCatalogError("默认投影目录越出仓库边界") from exc
    target = Path(output_path) if output_path is not None else allowed_target
    if not target.is_absolute():
        target = root / target
    target = target.resolve()
    if target != allowed_target:
        raise ShadowCatalogError(
            "跨科投影只能写入仓库内默认 personalization 派生文件"
        )
    target.parent.mkdir(parents=True, exist_ok=True)
    validated = validate_projection(root, payload)
    if not activation_internal and int(validated["counts"].get("live_enabled", 0)):
        raise ShadowCatalogError(
            "实时跨科关系只能由 cross_shadow_activation_408 累计评估后写入"
        )
    data = _canonical_json_bytes(payload)
    with _projection_lock(target.parent):
        fd, temporary = tempfile.mkstemp(prefix=f".{target.name}.", dir=target.parent)
        try:
            with os.fdopen(fd, "wb") as handle:
                handle.write(data)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, target)
        finally:
            if os.path.exists(temporary):
                os.unlink(temporary)
    return {
        "status": "built",
        "path": str(target),
        "sha256": hashlib.sha256(data).hexdigest(),
        "record_count": len(payload["records"]),
        "formal_write_count": 0,
    }


def write_projection(
    repo: Path | str,
    payload: dict[str, Any],
    output_path: Path | str | None = None,
) -> dict[str, Any]:
    """Write a disposable public projection, rejecting every live edge."""

    _retired_write_surface()
    return _write_projection(repo, payload, output_path, activation_internal=False)


def _write_activated_projection(
    repo: Path | str,
    payload: dict[str, Any],
) -> dict[str, Any]:
    """Private activation writer used only after cumulative policy validation."""

    _retired_write_surface()
    return _write_projection(repo, payload, activation_internal=True)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="只读生成 408 跨科 verified/shadow 候选；不写正式关系边"
    )
    parser.add_argument("--repo", default=str(Path(__file__).resolve().parents[1]))
    parser.add_argument("--format", choices=("json", "markdown"), default="json")
    parser.add_argument(
        "--adjacency-limit",
        type=int,
        default=DEFAULT_ADJACENCY_LIMIT,
        help=f"每个源考点最多保留的热路径邻接数，范围 1..{MAX_ADJACENCY_LIMIT}",
    )
    parser.add_argument(
        "--lifecycle",
        choices=("all", "verified_existing", "shadow_candidate"),
        default="all",
        help="只过滤 stdout 审阅面，不改变任何源文件",
    )
    args = parser.parse_args(argv)
    try:
        full_payload = build_shadow_catalog(
            args.repo,
            adjacency_limit=args.adjacency_limit,
        )
        payload = _filter_lifecycle(full_payload, args.lifecycle)
    except ShadowCatalogError as exc:
        print(f"[错误] {exc}", file=sys.stderr)
        return 2
    if args.format == "markdown":
        sys.stdout.write(render_markdown(payload))
    else:
        print(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
