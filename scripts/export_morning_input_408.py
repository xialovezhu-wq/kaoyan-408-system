#!/usr/bin/env python3
"""Export complete, source-bound preparation input for web D, without teaching.

Reads the published snapshot and a separately fingerprinted local eligibility
view. Does not choose representatives, author questions, import a run or grade.
"""
from __future__ import annotations

import argparse
import csv
import io
from collections import defaultdict
import datetime as dt
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import sys
import tempfile
from typing import Any
import zipfile

sys.dont_write_bytecode = True
REPO = Path(__file__).resolve().parents[1]
BRIDGE_ROOT = Path.home() / "Documents/Study-Pro-Bridge"
DETAILS = Path.home() / "Documents/kaoyan-408-details"
if str(BRIDGE_ROOT) not in sys.path:
    sys.path.insert(0, str(BRIDGE_ROOT))
from bridge_archives import check_manifest, file_hash, require, safe_path, source_file
from study_mcp import LibraryStore
from morning_question_policy_408 import true_exam_only_eligibility, recent_formal_exams, recent_verified_capture_exams, apply_formal_cooldown, last_true_exam_contacts, contact_order_key

SCHEMA = "cs408-morning-input-v1"
READY = "READY_FOR_WEB_PREPARATION"
COLLECTIONS = ("due_items", "due_concepts", "due_concept_ids", "concept_requests",
               "review_concepts", "review_concept_ids", "blocked", "source_bindings")
CONTRACT_PATHS = ("support/d408/asset-delivery-contract.md",
                  "support/d408/web-execution-report-contract.md")
MAX_BYTES = 2 * 1024**3


def encoded(value):
    return (json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2) + "\n").encode()


def digest(value):
    return hashlib.sha256(encoded(value)).hexdigest()


def write_json(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(encoded(value))


def write_rows(path, rows):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n" for row in rows), encoding="utf-8")


def workflow_collection(store, commit, day, collection):
    records, receipts, offset, total = [], [], 0, None
    while True:
        page = store.workflow("D", "408", day, commit, collection=collection, offset=offset, limit=200)
        require(page.get("source_commit") == commit and page.get("collection") == collection,
                "MORNING_INPUT_COLLECTION_IDENTITY_DRIFT")
        rows = page.get("records")
        require(isinstance(rows, list) and page.get("returned_records") == len(rows)
                and page.get("offset") == offset, "MORNING_INPUT_INVALID_PAGE")
        total = page["total_records"] if total is None else total
        require(total == page["total_records"], "MORNING_INPUT_PAGE_TOTAL_DRIFT")
        receipts.append({key: page.get(key) for key in ("offset", "returned_records", "total_records", "next_offset", "eof")})
        records.extend(rows)
        if page.get("eof") is True:
            require(page.get("next_offset") is None and len(records) == total, "MORNING_INPUT_EOF_MISMATCH")
            return records, receipts
        require(rows and page.get("next_offset") == offset + len(rows), "MORNING_INPUT_PAGINATION_STALLED")
        offset = page["next_offset"]


def validate_scope(scope):
    def unique(values, label):
        require(len(values) == len(set(values)), "MORNING_INPUT_DUPLICATE_" + label)
        return set(values)
    targets = unique(scope["review_concept_ids"], "TARGET")
    require(targets == unique([row["concept_id"] for row in scope["review_concepts"]], "REVIEW_CONCEPT"),
            "MORNING_INPUT_TARGET_MAPPING_MISMATCH")
    due_points = unique(scope["due_concept_ids"], "DUE_POINT")
    require(due_points == {row["concept_id"] for row in scope["due_concepts"]}, "MORNING_INPUT_DUE_POINT_MISMATCH")
    due_ids = unique([row["formal_node_id"] for row in scope["due_items"]], "DUE_NODE")
    grouped = [fid for row in scope["due_concepts"] for fid in row["formal_node_ids"]]
    require(due_ids == unique(grouped, "PRIMARY_BINDING"), "MORNING_INPUT_DUE_NODE_MISMATCH")
    unique([row["request_id"] for row in scope["concept_requests"]], "REQUEST")
    require(targets == due_points | {row["concept_id"] for row in scope["concept_requests"]},
            "MORNING_INPUT_SCOPE_UNION_MISMATCH")
    return {"concept_count": len(targets), "due_concept_count": len(due_points),
            "due_node_count": len(due_ids), "concept_request_count": len(scope["concept_requests"]),
            "request_only_concept_count": len(targets - due_points), "blocked_node_count": len(scope["blocked"])}


def formal_records(store, commit, ids):
    result = {}
    for start in range(0, len(ids), 160):
        batch, offset = ids[start:start + 160], 0
        while True:
            page = store.query("408", "formal_nodes", commit, ids=batch, offset=offset, limit=200, max_bytes=524288)
            require(page.get("source_commit") == commit, "MORNING_INPUT_FORMAL_VERSION_DRIFT")
            for row in page["records"]:
                require(row["id"] not in result, "MORNING_INPUT_DUPLICATE_FORMAL_RECORD")
                result[row["id"]] = row
            if page["eof"]:
                break
            require(page.get("next_offset", 0) > offset, "MORNING_INPUT_FORMAL_PAGINATION_STALLED")
            offset = page["next_offset"]
    require(set(result) == set(ids), "MORNING_INPUT_FORMAL_RECORDS_INCOMPLETE")
    return result


def personal_records(store, commit, points):
    result, proofs = {}, []
    for start in range(0, len(points), 32):
        batch, cursor = points[start:start + 32], ""
        seen_cursors = set()
        while True:
            page = store.personal_context("408", batch, source_commit=commit, view="direct", max_bytes=65536, cursor=cursor)
            require(page.get("source_commit") == commit and page.get("freshness", {}).get("status") == "current",
                    "MORNING_INPUT_PERSONAL_NOT_CURRENT")
            value = page.get("personal_context", {})
            require(value.get("status") == "ready", "MORNING_INPUT_PERSONAL_READ_FAILED", value.get("status"))
            for row in value.get("knowledge_summaries", []):
                point = row["knowledge_point"]
                require(point in batch and point not in result, "MORNING_INPUT_PERSONAL_POINT_MISMATCH")
                result[point] = {"source_version": value.get("source_version"), "summary": row,
                                 "source_mappings": [r for r in page.get("source_mappings", [])
                                     if r.get("sha256") == row.get("summary_sha256")],
                                 "source_refs": [r for r in value.get("sources", []) if r.get("knowledge_point") == point]}
            proofs.append({"requested_points": batch, "source_version": value.get("source_version"),
                           "returned_points": [r["knowledge_point"] for r in value.get("knowledge_summaries", [])],
                           "complete": value.get("complete"), "missing_points": value.get("knowledge_coverage", {}).get("missing_points", [])})
            if value.get("complete"):
                break
            next_cursor = value.get("next_cursor")
            require(next_cursor and next_cursor != cursor and next_cursor not in seen_cursors,
                    "MORNING_INPUT_PERSONAL_PAGINATION_STALLED")
            seen_cursors.add(next_cursor)
            cursor = next_cursor
    for point in points:
        if point not in result:
            result[point] = {"summary": {"knowledge_point": point, "status": "missing",
                                         "independent_mastery_inferred": False}, "source_refs": [], "source_mappings": []}
    return result, proofs


def strip_capsule_refs(value):
    if isinstance(value, dict):
        return {key: strip_capsule_refs(child) for key, child in value.items()
                if "capsule" not in key and key not in {"correct_option_id", "answer", "grading"}}
    if isinstance(value, list):
        return [strip_capsule_refs(child) for child in value]
    return value


class Materials:
    def __init__(self, snapshot, root):
        self.snapshot, self.root = snapshot, root
        self.sources, self.blobs, self.bytes = {}, {}, 0

    def add(self, path, *, optional=False):
        if path in self.sources:
            return self.sources[path]
        safe_path(path)
        row = self.snapshot._row(path)
        if row is None and optional:
            return {"published_path": path, "status": "not_published"}
        require(row is not None, "MORNING_INPUT_SOURCE_NOT_PUBLISHED", path)
        # The source inventory is the only read allowlist; never follow native
        # private paths, answer-capsule refs or paths supplied by a web return.
        require("/answers/" not in path and "capsule" not in path, "MORNING_INPUT_PRIVATE_CAPSULE_REJECTED")
        source = self.snapshot.path(path)
        sha = row["sha256"]
        if sha not in self.blobs:
            suffix = Path(path).suffix.lower() or ".txt"
            destination = f"materials/{sha[:2]}/{sha}{suffix}"
            target = self.root / destination
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(source, target)
            require(file_hash(target) == sha, "MORNING_INPUT_MATERIAL_HASH_MISMATCH", path)
            self.bytes += target.stat().st_size
            require(self.bytes <= MAX_BYTES, "MORNING_INPUT_MATERIAL_BUDGET_EXCEEDED")
            self.blobs[sha] = destination
        ref = {"published_path": path, "package_path": self.blobs[sha], "sha256": sha,
               "size": source.stat().st_size, "status": "included", "kind": row.get("kind"),
               "read_scope": "local_bytes_verified_not_model_visual_review"}
        self.sources[path] = ref
        return ref


def verify_bundle(zip_path):
    with zipfile.ZipFile(source_file(Path(zip_path))) as archive:
        names = archive.namelist()
        require(len(names) == len(set(names)), "MORNING_INPUT_ZIP_DUPLICATE_PATH")
        manifest = json.loads(archive.read("manifest.json"))
        require(manifest.get("schema") == SCHEMA and manifest.get("artifact_kind") == "morning_preparation_input",
                "MORNING_INPUT_MANIFEST_REQUIRED")
        listed = set()
        for row in manifest["files"]:
            path = safe_path(row["path"])
            require(path not in listed and path != "manifest.json", "MORNING_INPUT_DUPLICATE_FILE")
            listed.add(path)
            data = archive.read(path)
            require(len(data) == row["size"] and hashlib.sha256(data).hexdigest() == row["sha256"],
                    "MORNING_INPUT_ARCHIVE_HASH_MISMATCH", path)
        require(listed == set(names) - {"manifest.json"}, "MORNING_INPUT_ARCHIVE_INVENTORY_MISMATCH")
        scope = {}
        for name in COLLECTIONS:
            scope[name] = [json.loads(line) for line in archive.read(f"scope/{name}.jsonl").decode().splitlines() if line.strip()]
        counts = validate_scope(scope)
        require(all(manifest["coverage"][key] == value for key, value in counts.items()), "MORNING_INPUT_ARCHIVE_SCOPE_MISMATCH")
        require(all(f"concepts/{point}.json" in names for point in scope["review_concept_ids"]),
                "MORNING_INPUT_CONCEPT_FILE_MISSING")
        for point in scope["review_concept_ids"]:
            item = json.loads(archive.read(f"concepts/{point}.json"))
            require(item["concept_id"] == point and item["source_commit"] == manifest["source_commit"],
                    "MORNING_INPUT_CONCEPT_IDENTITY_MISMATCH")
        for row in json.loads(archive.read("source-map.json")):
            require(row["package_path"] in listed, "MORNING_INPUT_SOURCE_COPY_MISSING")
            require(hashlib.sha256(archive.read(row["package_path"])).hexdigest() == row["sha256"],
                    "MORNING_INPUT_SOURCE_MAP_HASH_MISMATCH")
    return {"status": "VERIFIED_PREPARATION_INPUT", "file_count": len(names), **counts,
            "zip_sha256": file_hash(Path(zip_path)), "zip_bytes": Path(zip_path).stat().st_size}


def startup_prompt(day, commit, counts, version):
    return f'''请用GPT-6 Pro处理附件408_morning_input_{day}.zip，完成{day}的408晨间真题备题。
输入版本{version}，固定source_commit={commit}；共{counts["concept_count"]}个目标知识点。
先读contracts/D_PROJECT_INSTRUCTIONS.txt、START_HERE.md、concept-index.json和RETURN_CONTRACT.md。
只用已核验的408统考真题，保留原题与EXAM408身份；不自编、不改题、不用既有变式。精确题面近7天已使用则排除，查同点其他合格真题；无合格真题就记unresolved，不凑题。
选题符合目标知识点后，优先上次实际做题或展示日期最早的合格真题；全部最终选中题再按这一日期由早到晚统一排序。日期来自runtime-eligibility.json的last_true_exam_contacts及各候选last_learning_contact，不用年份、导入时间、文件时间或入库时间代替。未记录日期单列在已知日期之后，不冒称更久未做。跨分片回包保留精确题面和日期证据，最终合并再次全局排序。
先实际调用本会话的Study Library library核对固定版本，再实际读取拟采用题的question_refs和对应personal_context。工具缺失与Session terminated分开记录；local快照号不能拿到GitHub读取。
包内资料优先使用，MCP按真实缺项有界补查。个人材料分别记录独立正确、提示后修正和未验证边界。
每题及时保存，每5个目标交阶段ZIP；最后交真实成稿、进度、执行报告及未覆盖清单，按现有D合同回传408_morning_review.zip，不开始学习。'''


RETURN_CONTRACT = '''# 网页D回包

本输入只把已有资料准备好，网页负责语义成稿。使用return-template作为固定字段模板；空items不是完成。

完整回包仍为408_morning_review.zip，保留schema_version=1、project=D、subject=408、同一source_commit/snapshot_id、target_date、due_version、concept_priority_version和asset_delivery=snapshot_refs_v1。run_id使用新的安全ASCII批次身份；同一批恢复沿用该身份。

due_concepts.json保留本输入的原样concept_requests和完整review_concept_ids。每个原到期主点选一代表，selected_items中的question_id是正式来源ID；suppressed_same_concept_question_ids包含同点其他全部到期ID。新增的请求独有点使用supplemental_items的真实request_id、来源ID及目标点，不冒用原题评分。每题提供problem_path、私有solution_path、history_path、teaching_path、公开image_paths、source_label和同版source_refs。

每点只从实际核验的统考真题中选取，精确题面距离目标日期不足7天则排除。不得自编、改题或使用既有变式。没有合格真题时列unresolved并保留目标；不以知识点为单位封禁其他合格真题。每个采用项须给真实EXAM408身份，同一题面全场只选一次。

selected_items代表的question_id必须属于该点due_formal_node_ids。若这些到期源受阻，但本点candidate_files里另有合格来源，使用substitute_items.jsonl：保留真实question_id/true_item_id/surface_sha256和所有题面、私有讲解、source_refs；execution_kind=due_representative_substitute，substitution_reason写具体原因，suppressed_same_concept_question_ids列该点全部原到期ID，不提供request_id、不改concept_requests。它是本次D执行的点级替代，不宣称关联原题已作答；本地生成执行ID，学习只封concept_supplement证据。没有替代则仍进unresolved。

所有采用项的preferred_true_item_id或true_item_id必须是真实EXAM408身份。web_variants.jsonl为空，不提供variant_id或web_variant_id；普通来源题和未知年份题不可充当统考真题。

源文件引用使用source-map的published_path/sha256，不能拿package_path当MCP路径。已有本地原图按contracts/中的asset-delivery合同写asset_refs.jsonl，目标package_path为回包预定路径；原解析/原型只进private。网页新生成图表必须实际入包。模板中的原图引用仅供查找，网页需按实际选题、实际阅读情况生成最终引用，不把本地字节复制当成已视觉阅读。

所有必要语义备题完成才进入covered_concept_ids，真实未解决点保留unresolved.jsonl；二者互斥并集是完整review_concept_ids。仅待本地装配的已读原图引用不算语义缺项。只有完整语义成稿才能标WEB_PREPARED；不能标本地ready或已学习。

每完成一个内部批次，保存实际题目文件和结构化items，再更新progress.json中的completed_concept_ids、pending_concept_ids、unresolved_concept_ids及对应文件路径。需要跨轮接续时交出这些真实文件，不以最终总结重置已完成记录。

单项失败记下真实调用和依赖，继续其他独立项。最终无论COMPLETE/PARTIAL/BLOCKED都必须交EXECUTION_REPORT.md、web_execution_report.json与已有实际成果，说明成功、失败、未执行和本地需补全项。manifest增加web_execution_report="web_execution_report.json"，该文件的role固定为web_execution_report。报告精确顶层只有schema、source_commit、completed_units、pending_unit_ids、failures；schema为cs408-web-execution-report-v1。completed_units每项含unit_id、已保存files、source_refs；pending_unit_ids列未完单元；failures每项含sequence、真实call_id或null、tool、input、raw_error_or_response、affected_unit_ids、recovery_status（unresolved/recovered）。本地要核对成功文件和来源，不能只列工具名称。若平台没有原始错误或调用ID，明确缺失，不编造。真实零成功如实写零。完整字段参见contracts/web-execution-report-contract.md。

PARTIAL包不是学习ready，但仍必须交给本地接续。保存成功成稿，不以缺失项或未闭环工具问题丢弃整包；本地只补失败部分，正式提交后再维护本次暴露的读取问题。

manifest.files覆盖除manifest自身外全部实际回包文件，含path/sha256/size/role，哈希来自真实字节；不列未实际入包的原图。START_HERE.md只说明由用户手动交本地prepare_d_review_408预处理；不执行本地命令，不开始教学，不伪造本地回执。
'''


INPUT_LAYOUT = '''# 输入字段与安全脚本用法

每点读取concepts/<code>.json：个人摘要字段为personal_context，不存在personal_history；完整个人来源在full_personal_sources。候选路径只取candidate_files，不按ID猜文件。候选中的canonical_exam_materials按true_item_id区分各道合格真题；is_exact_formal_original=false不得作为关联节点的原题复做，需用substitute_items或请求独有点的supplemental_items。每个原图引用的package_path来自实际JSON/source-map，使用前检查文件存在；自制拼图如实记录生成路径，不猜gallery序号。

用python -I tools/web_morning_workspace_408.py read-unit --input-root <真实输入根> --concept-id <本片代码>可取得实际对象及候选。不额外在工作目录创建inspect.py、json.py、typing.py等标准库同名脚本；-I用于隔离临时PYTHONPATH和当前目录遮蔽。

文本直接保存为Markdown；需要组合多个段落时，把字符串或字符串数组保存到JSON文件，然后用python -I tools/web_morning_workspace_408.py write-text --output-root <输出根> --path <新.md路径> --content-json <文本JSON>确定性写入。不要把列表直接加到字符串，也不要把大段成稿嵌入多层Python字典或f-string。修改使用新的成稿文件路径并更新items，保留旧阶段文件。

preparation_gaps.json提前列出本地未发现合格同点题面的知识点。空eligible不等于无原图，不把搜索无结果写成MCP执行失败。先完成其他独立点；本地根据真实原图/解析补齐必要准入后接续，目标不删除。
'''


def _export_full_bundle(*, target_date: str, source_commit: str | None = None,
                  output_dir: str | Path | None = None, repo: str | Path = REPO,
                  bridge_root: str | Path = BRIDGE_ROOT, progress=None):
    day = dt.date.fromisoformat(target_date).isoformat()
    exporter_sha = file_hash(Path(__file__))
    require(day == target_date, "MORNING_INPUT_DATE_REQUIRED")
    repo, bridge_root = Path(repo).resolve(), Path(bridge_root).resolve()
    details = repo.parent / "kaoyan-408-details" if repo == REPO else repo.parent / "details"
    base = Path(output_dir).expanduser() if output_dir else details / "review-deliveries/morning-input"
    require(not any(p.is_symlink() for p in [base, *base.parents]), "MORNING_INPUT_UNSAFE_OUTPUT")
    base = base.resolve()
    require(not base.is_relative_to(repo), "MORNING_INPUT_OUTPUT_MUST_BE_OUTSIDE_FORMAL_REPO")
    base.mkdir(parents=True, exist_ok=True)
    def report(stage, **values):
        if progress:
            progress({"stage": stage, **values})
    store = LibraryStore(bridge_root)
    try:
        snap = store.snapshot("408", source_commit or "")
        commit = snap.commit
        freshness = store.freshness(snap)
        require(freshness.get("status") == "current", "MORNING_INPUT_SNAPSHOT_NOT_CURRENT", freshness)
        report("scope", source_commit=commit)
        summary = store.workflow("D", "408", day, commit)
        scope, pages = {}, {}
        for collection in COLLECTIONS:
            scope[collection], pages[collection] = workflow_collection(store, commit, day, collection)
        counts = validate_scope(scope)
        points = scope["review_concept_ids"]
        groups = {row["concept_id"]: row["formal_node_ids"] for row in scope["due_concepts"]}
        requests = defaultdict(list)
        for row in scope["concept_requests"]:
            requests[row["concept_id"]].append(row)
        candidate_ids = sorted({fid for row in scope["review_concepts"]
                                for fid in row.get("candidate_formal_node_ids", []) + row.get("formal_node_ids", [])})
        records = formal_records(store, commit, candidate_ids) if candidate_ids else {}
        style_entry = snap.library["entrypoints"]["variant_style_guide"]
        release_id = style_entry.split("/")[2]
        release_dir = details / "corpus/408统考真题/style-releases" / release_id
        from morning_input_eligibility_408 import build_eligibility
        eligibility = build_eligibility(repo=repo, details=details, target_date=day,
                                        formal_ids=candidate_ids, release_dir=release_dir)
        eligibility = true_exam_only_eligibility(eligibility)
        from start_morning_coverage_campaign_408 import _node_snapshot
        recent = recent_formal_exams(_node_snapshot(repo), snap.library.get("question_refs", {}), day)
        for identity, fact in recent_verified_capture_exams(repo, day).items():
            if identity not in recent or fact["study_date"] > recent[identity]["study_date"]:
                recent[identity] = fact
        eligibility = apply_formal_cooldown(eligibility, recent)
        contacts = last_true_exam_contacts(repo, snap.library.get("question_refs", {}), day, release_dir)
        eligibility["last_true_exam_contacts"] = contacts
        eligibility["ordering_policy"] = "oldest_verified_actual_contact_first_unknown_last"
        for candidate_rows in eligibility.get("candidates_by_formal_id", {}).values():
            for candidate_row in candidate_rows:
                candidate_row["last_learning_contact"] = contacts.get(candidate_row["true_item_id"])
            candidate_rows.sort(key=lambda row: contact_order_key(row["true_item_id"], contacts))
        project_instructions = repo / "planning/2026-09-13-d-deepseek-optimization/D-408晨间备题-网页提示词-v7.0.txt"
        require(project_instructions.is_file(), "MORNING_PROJECT_INSTRUCTIONS_MISSING")
        version = digest({"source_commit": commit, "target_date": day,
                          "exporter_sha256": exporter_sha, "eligibility": eligibility,
                          "project_instructions_sha256": file_hash(project_instructions),
                          "return_contract": RETURN_CONTRACT})
        generation = base / day / version
        receipt_path = generation / "receipt.json"
        if receipt_path.is_file():
            receipt = json.loads(receipt_path.read_text())
            require(receipt.get("input_version") == version and receipt.get("source_commit") == commit,
                    "MORNING_INPUT_EXISTING_IDENTITY_DRIFT")
            proof = verify_bundle(receipt["zip_path"])
            require(proof["zip_sha256"] == receipt["zip_sha256"] and file_hash(Path(receipt["prompt_path"])) == receipt["prompt_sha256"],
                    "MORNING_INPUT_EXISTING_ARTIFACT_DRIFT")
            return dict(receipt, reused=True)
        require(not generation.exists(), "MORNING_INPUT_INCOMPLETE_GENERATION_REQUIRES_RECOVERY")
        generation.parent.mkdir(parents=True, exist_ok=True)
        report("personal_context", **counts)
        personal, personal_proofs = personal_records(store, commit, points) if points else ({}, [])
        profiles_path = style_entry.rsplit("/", 1)[0] + "/style-profiles.jsonl"
        profiles = []  # No variant style anchors or generation instructions in new inputs.
        by_point = defaultdict(list)
        for row in profiles:
            if row.get("main_knowledge") in points:
                by_point[row["main_knowledge"]].append(row)
        bank_path = snap.library["entrypoints"]["variant_bank"]
        variants = []
        with tempfile.TemporaryDirectory(prefix=".morning-input-", dir=generation.parent) as temp:
            work = Path(temp) / "generation"
            root = work / "content"
            root.mkdir(parents=True)
            materials = Materials(snap, root)
            write_json(root / "scope/summary.json", {"source_commit": commit, "target_date": day,
                "snapshot_id": commit, "publication_target": "local", "native_summary": summary["result"],
                "coverage": counts, "freshness": freshness, "collection_files": {c: f"scope/{c}.jsonl" for c in COLLECTIONS}})
            for collection in COLLECTIONS:
                write_rows(root / f"scope/{collection}.jsonl", scope[collection])
            write_json(root / "scope/read-receipts.json", {"workflow_pages": pages, "personal_pages": personal_proofs})
            write_json(root / "runtime-eligibility.json", strip_capsule_refs(eligibility))
            (root / "contracts").mkdir()
            shutil.copyfile(project_instructions, root / "contracts/D_PROJECT_INSTRUCTIONS.txt")
            for path in CONTRACT_PATHS:
                ref = materials.add(path)
                shutil.copyfile(root / ref["package_path"], root / "contracts" / Path(path).name)
            candidates = {}
            qrefs = snap.library.get("question_refs", {})
            canonical_sources = {row["question_id"]: row for row in snap.library.get("original_mappings", [])}
            for shard in snap.library.get("original_mapping_inventory", []):
                records_in_shard = [json.loads(line) for line in snap.text(shard["path"]).splitlines() if line.strip()]
                require(len(records_in_shard) == shard["count"], "MORNING_ORIGINAL_MAPPING_INCOMPLETE")
                for row in records_in_shard:
                    canonical_sources[row["question_id"]] = row
            catalog_path = snap.library.get("entrypoints", {}).get("canonical_exam_index")
            catalog = {}
            catalog_ref = None
            if catalog_path:
                catalog = {row["exam_question_id"]: row for row in
                           csv.DictReader(io.StringIO(snap.text(catalog_path).lstrip('\ufeff')))}
                catalog_ref = {"published_path": catalog_path, "sha256": file_hash(snap.path(catalog_path))}
            report("source_materials", formal_candidates=len(candidate_ids))
            for index, fid in enumerate(candidate_ids):
                q = qrefs.get(fid, {})
                eligible = [row for row in eligibility.get("candidates_by_formal_id", {}).get(fid, [])
                            if row.get("eligible")]
                allowed_ids = {row["true_item_id"] for row in eligible}
                if not allowed_ids:
                    continue
                exam_materials = []
                for identity in sorted(allowed_ids, key=lambda key: contact_order_key(key, contacts)):
                    original = canonical_sources.get(identity, {})
                    original_questions = original.get("question_images") or [
                        path for path in q.get("question_images", []) if identity in path.split('/')]
                    original_solutions = original.get("solution_images") or [
                        path for path in q.get("solution_images", []) if identity in path.split('/')]
                    text_record = catalog.get(identity, {})
                    exam_materials.append({"true_item_id": identity,
                        "last_learning_contact": contacts.get(identity),
                        "is_exact_formal_original": identity in q.get("canonical_ids", []),
                        "question_images": [materials.add(path, optional=True) for path in original_questions],
                        "solution_images": [materials.add(path, optional=True) for path in original_solutions],
                        "question_ocr_candidate": text_record.get("question_ocr"),
                        "solution_ocr_candidate": text_record.get("solution_ocr"),
                        "ocr_requires_image_verification": True, "catalog_source": catalog_ref,
                        "original_mapping": original})
                candidate = {"formal_id": fid, "formal_record": records[fid], "question_refs": q,
                    "safe_card": materials.add(q["safe_card"], optional=True) if q.get("safe_card") else None,
                    "detail_card": materials.add(q["detail_card"], optional=True) if q.get("detail_card") else None,
                    "canonical_exam_materials": exam_materials,
                    "question_images": [ref for exam in exam_materials for ref in exam["question_images"]],
                    "solution_images": [ref for exam in exam_materials for ref in exam["solution_images"]],
                    "eligibility": strip_capsule_refs(eligible)}
                candidates[fid] = candidate
                write_json(root / f"candidates/{fid}.json", candidate)
                if index % 50 == 0:
                    report("source_materials", completed=index + 1, total=len(candidate_ids), bytes=materials.bytes)
            anchors = {cid for rows in by_point.values() for row in rows for cid in row.get("canonical_ids", [])}
            anchor_materials = defaultdict(list)
            for path in snap.files:
                if not path.startswith("details/corpus/") or "/assets/" not in path:
                    continue
                matches = anchors.intersection(path.split("/"))
                if matches:
                    ref = materials.add(path)
                    for cid in matches:
                        anchor_materials[cid].append(ref)
            variant_records = {}
            for row in variants:
                public_path = "variant-bank/surfaces/" + row["surface_sha256"] + ".json"
                ref = materials.add(public_path, optional=True)
                record = strip_capsule_refs(row)
                record["surface_path"] = public_path
                record["public_surface_file"] = ref
                if ref["status"] == "included":
                    record["public_surface"] = json.loads((root / ref["package_path"]).read_text())
                variant_records[row["variant_id"]] = record
            index_rows = []
            for point_record in scope["review_concepts"]:
                point = point_record["concept_id"]
                ids = list(dict.fromkeys(point_record.get("candidate_formal_node_ids", []) + point_record.get("formal_node_ids", [])))
                ids = [fid for fid in ids if fid in candidates]
                p = personal[point]
                full_personal_sources = []
                for ref in p.get("source_refs", []):
                    if ref.get("path"):
                        full_personal_sources.append(materials.add(ref["path"], optional=True))
                profile_object = (p.get("summary", {}).get("learning_profile") or {}).get("profile_object")
                if profile_object:
                    prefix = Path(snap.library["entrypoints"]["personalization"]).parent.as_posix() + "/"
                    relative = safe_path(profile_object["ref"])
                    profile_path = relative if relative.startswith(prefix) else prefix + relative
                    full_profile = materials.add(profile_path)
                    require(full_profile["sha256"] == profile_object["sha256"], "MORNING_FULL_PROFILE_HASH_MISMATCH")
                    if full_profile not in full_personal_sources:
                        full_personal_sources.append(full_profile)
                item_profiles = [{**row, "anchor_materials": {cid: anchor_materials.get(cid, []) for cid in row.get("canonical_ids", [])},
                                  "usable_canonical_anchor_ids": [cid for cid in row.get("canonical_ids", [])
                                      if eligibility.get("canonical_anchor_eligibility", {}).get(cid, {}).get("production_usable") is True],
                                  "canonical_anchor_eligibility": {cid: eligibility.get("canonical_anchor_eligibility", {}).get(cid,
                                      {"status": "not_available_do_not_infer_permission"}) for cid in row.get("canonical_ids", [])}}
                                 for row in by_point.get(point, [])]
                item_variants = [row for row in variant_records.values()
                                 if row.get("formal_node_id") in ids and row.get("main_knowledge") == point]
                item = {"concept_id": point, "source_commit": commit, "native_target": point_record,
                        "due_formal_node_ids": groups.get(point, []), "concept_requests": requests.get(point, []),
                        "candidate_files": [f"candidates/{fid}.json" for fid in ids],
                        "personal_context": p, "full_personal_sources": full_personal_sources,
                        "style_profiles": item_profiles, "existing_variants": item_variants,
                        "web_work_required": ["select_representative_or_request_supplement", "inspect_necessary_assets",
                                              "prepare_solution_history_and_teaching", "verify_exact_exam_source"],
                        "request_only_originals_are_sources_not_question_level_reviews": point not in groups,
                        "semantic_preparation_complete": False}
                eligible_surface = any(
                    candidate.get("eligible") and candidate.get("main_knowledge") == point
                    for fid in ids for candidate in candidates[fid].get("eligibility", []))
                item["source_readiness"] = {
                    "has_eligible_matching_surface": eligible_surface,
                    "has_variant_style_profile": bool(item_profiles),
                    "has_usable_variant_anchor": any(row["usable_canonical_anchor_ids"] for row in item_profiles),
                    "needs_local_preparation_before_web": not eligible_surface,
                    "raw_sources_may_still_exist": True}
                write_json(root / f"concepts/{point}.json", item)
                index_rows.append({"concept_id": point, "path": f"concepts/{point}.json", "due_candidate_count": len(groups.get(point, [])),
                                   "candidate_count": len(ids), "existing_variant_count": len(item_variants),
                                   "personal_status": p["summary"].get("status", "available"),
                                   "request_ids": [r["request_id"] for r in requests.get(point, [])]})
            write_json(root / "concept-index.json", index_rows)
            gaps = []
            for row in index_rows:
                item = json.loads((root / row["path"]).read_text())
                if item["source_readiness"]["needs_local_preparation_before_web"]:
                    gaps.append({"concept_id": row["concept_id"], "candidate_files": item["candidate_files"],
                                 "reason": "no_eligible_true_exam_outside_exact_7_day_window",
                                 "raw_sources_are_not_automatically_display_authorized": True})
            write_json(root / "preparation_gaps.json", gaps)
            (root / "INPUT_LAYOUT.md").write_text(INPUT_LAYOUT, encoding="utf-8")
            batches = [{"batch_id": f"batch-{n // 8 + 1:03d}", "concept_ids": [r["concept_id"] for r in index_rows[n:n + 8]],
                        "concept_files": [r["path"] for r in index_rows[n:n + 8]]} for n in range(0, len(index_rows), 8)]
            write_json(root / "batches.json", batches)
            write_json(root / "progress.json", {"schema": "cs408-D-web-preparation-progress-v1", "input_version": version,
                "source_commit": commit, "completed_concept_ids": [], "pending_concept_ids": points,
                "unresolved_concept_ids": [], "completed_files": [], "all_batches_required": True})
            write_json(root / "source-map.json", sorted(materials.sources.values(), key=lambda r: r["published_path"]))
            template = root / "return-template"
            template.mkdir()
            write_json(template / "due_concepts.json", {"concept_ids": scope["due_concept_ids"],
                "concept_requests": scope["concept_requests"], "review_concept_ids": points,
                "concept_priority_version": summary["result"].get("concept_priority_version")})
            write_json(template / "manifest.fields.json", {"schema_version": 1, "project": "D", "subject": "408",
                "source_commit": commit, "snapshot_id": commit, "target_date": day, "publication_target": "local",
                "due_version": summary["result"]["due_version"], "concept_priority_version": summary["result"].get("concept_priority_version"),
                "asset_delivery": "snapshot_refs_v1", "input_version": version,
                "web_execution_report": "web_execution_report.json",
                "status": "TEMPLATE_NOT_PREPARED", "run_id": None, "files": []})
            write_json(template / "web_execution_report.json", {"schema": "cs408-web-execution-report-v1",
                "source_commit": commit, "completed_units": [], "pending_unit_ids": points,
                "failures": []})
            for name in ("selected_items", "supplemental_items", "substitute_items", "web_variants", "asset_refs", "unresolved"):
                write_rows(template / (name + ".jsonl"), [])
            (root / "RETURN_CONTRACT.md").write_text(RETURN_CONTRACT, encoding="utf-8")
            prompt = startup_prompt(day, commit, counts, version)
            (root / "START_WEB_D.md").write_text(prompt, encoding="utf-8")
            (root / "START_HERE.md").write_text(f'''# 408晨间备题输入：{day}

本包为本地准备的真实资料，不是已经选好题的晨间回包，不可用于启动DeepSeek学习。

完整范围{counts["concept_count"]}个知识点；到期{counts["due_node_count"]}题/{counts["due_concept_count"]}点，点级请求{counts["concept_request_count"]}条，另有{counts["blocked_node_count"]}个原生阻塞节点。候选正式ID共{len(candidate_ids)}个。每个目标点都在concept-index.json中有独立文件；本地没有删点或预先选唯一代表。

固定source_commit：{commit}。scope/保留原生完整集合和分页EOF证明。runtime-eligibility.json是另外冻结的本地精确曝光/候选资格，不能冒称它属于发布提交；回包时本地还会重查。个人摘要保留原正式证据及其缺项，not_authored不是伪造的网页成稿。

从concept-index.json读取每个concepts/<code>.json，再按candidate_files打开候选。所有package_path都相对于本包根，材料在materials/按真实哈希去重。题面、解析和历史是教师备题资料，勿显示给未作答用户。本轮不提供或采用变式。

batches.json每批8点仅用于内部处理与保存进度，必须覆盖全部批次。每批真实成稿后更新progress.json和实际文件，不以“已保存”文字替代文件。需要结束时交付真实阶段成果和未完成清单，不能把已经成功读取的来源归零。

最终按RETURN_CONTRACT.md交408_morning_review.zip；已有原图回包仅交引用，新生成材料实际随包。然后停止，由用户手动交本地预处理。网页启动用语见START_WEB_D.md。
''', encoding="utf-8")
            coverage = {**counts, "candidate_formal_count": len(candidate_ids), "concept_files": len(index_rows),
                        "scope_export_complete": True, "web_semantic_preparation_complete": False,
                        "material_reference_count": len(materials.sources), "unique_material_count": len(materials.blobs),
                        "unique_material_bytes": materials.bytes}
            write_json(root / "coverage_report.json", coverage)
            manifest = {"schema": SCHEMA, "schema_version": 1, "project": "D", "subject": "408",
                        "artifact_kind": "morning_preparation_input", "status": READY, "input_version": version,
                        "source_commit": commit, "snapshot_id": commit, "publication_target": "local", "target_date": day,
                        "due_version": summary["result"]["due_version"], "concept_priority_version": summary["result"].get("concept_priority_version"),
                        "eligibility_sha256": digest(eligibility), "coverage": coverage,
                        "formal_write_count": 0, "learner_evidence_write_count": 0, "model_call_count": 0,
                        "selected_representatives": False, "learning_started": False, "files": []}
            manifest["files"] = [{"path": path.relative_to(root).as_posix(), "size": path.stat().st_size,
                                  "sha256": file_hash(path), "role": "web_preparation_input_private"}
                                 for path in sorted(root.rglob("*")) if path.is_file()]
            write_json(root / "manifest.json", manifest)
            check_manifest(root)
            require(store.freshness(snap).get("status") == "current", "MORNING_INPUT_SOURCE_CHANGED_DURING_EXPORT")
            archive = work / f"408_morning_input_{day}.zip"
            report("archive", files=len(manifest["files"]) + 1, material_bytes=materials.bytes)
            with zipfile.ZipFile(archive, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=1, allowZip64=True) as z:
                for path in sorted(root.rglob("*")):
                    if path.is_file():
                        z.write(path, path.relative_to(root).as_posix())
            proof = verify_bundle(archive)
            prompt_file = work / f"START_WEB_D_{day}.md"
            prompt_file.write_text(prompt, encoding="utf-8")
            receipt = {"status": READY, "schema": SCHEMA, "input_version": version, "source_commit": commit,
                       "exporter_sha256": exporter_sha,
                       "target_date": day, **counts, "zip_path": str(generation / archive.name),
                       "prompt_path": str(generation / prompt_file.name), "content_root": str(generation / "content"),
                       "zip_sha256": proof["zip_sha256"], "zip_bytes": proof["zip_bytes"],
                       "prompt_sha256": file_hash(prompt_file), "manifest_sha256": file_hash(root / "manifest.json"),
                       "verification": proof, "formal_write_count": 0, "learner_evidence_write_count": 0,
                       "model_call_count": 0, "learning_started": False, "reused": False}
            write_json(work / "receipt.json", receipt)
            require(file_hash(Path(__file__)) == exporter_sha, "MORNING_INPUT_EXPORTER_CHANGED_DURING_EXPORT")
            work.rename(generation)
            return receipt
    finally:
        store.close()


def export_bundle(*, target_date: str, source_commit: str | None = None,
                  output_dir: str | Path | None = None, repo: str | Path = REPO,
                  bridge_root: str | Path = BRIDGE_ROOT, progress=None):
    from split_web_input_408 import attach_partitions
    receipt = _export_full_bundle(target_date=target_date, source_commit=source_commit,
        output_dir=output_dir, repo=repo, bridge_root=bridge_root, progress=progress)
    from morning_selection_408 import policy_for, export_selection
    selection_policy = policy_for(repo)
    if selection_policy is not None:
        return export_selection(receipt, repo, selection_policy)
    return attach_partitions(receipt)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--date", required=True)
    parser.add_argument("--source-commit")
    parser.add_argument("--output-dir", type=Path)
    parser.add_argument("--repo", type=Path, default=REPO)
    parser.add_argument("--progress", action="store_true")
    args = parser.parse_args()
    try:
        result = export_bundle(target_date=args.date, source_commit=args.source_commit, output_dir=args.output_dir, repo=args.repo,
            progress=(lambda value: print(json.dumps(value, ensure_ascii=False), file=sys.stderr, flush=True)) if args.progress else None)
    except Exception as exc:
        print(json.dumps({"status": "MORNING_INPUT_PENDING", "reason": str(exc), "detail": getattr(exc, "detail", None),
                          "formal_write_count": 0, "learner_evidence_write_count": 0}, ensure_ascii=False))
        return 1
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
