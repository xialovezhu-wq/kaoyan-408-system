#!/usr/bin/env python3
"""Accept an immutable web D ZIP, prepare its exact concept view, and write a handoff.

Preparation only: no model calls, learner display, answer, Capture or formal apply.
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import os
from pathlib import Path
import re
import shutil
import sys
import tempfile

sys.dont_write_bytecode = True
BRIDGE_ROOT = Path.home() / "Documents/Study-Pro-Bridge"
sys.path.insert(0, str(BRIDGE_ROOT))
from bridge_archives import ArchiveBudget, check_manifest, extract_zip, file_hash, source_file
import local_concept_review_408 as local_review
from morning_question_policy_408 import order_items, ordering_summary, require_true_exam_items, recent_formal_exams, recent_verified_capture_exams
import start_morning_coverage_campaign_408 as control
import study_bridge_408 as native
from d_asset_delivery_408 import assemble_delivery
from morning_image_preflight_408 import inspect_plan


def require(condition, reason):
    if not condition:
        raise ValueError(reason)


def read(path):
    return json.loads(path.read_text(encoding="utf-8"))


def rows(path):
    result = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]
    require(all(isinstance(row, dict) for row in result), "D_jsonl_objects_required:" + path.name)
    return result


def write(path, value):
    raw = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False, indent=2) + "\n"
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, name = tempfile.mkstemp(prefix=".d-handoff-", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            stream.write(raw)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(name, path)
    finally:
        if os.path.exists(name):
            os.unlink(name)


def unpack(archive, deliveries, max_bytes):
    source_file(archive)
    digest = file_hash(archive)
    delivery = deliveries / digest
    delivery.mkdir(parents=True, exist_ok=True)
    require(not delivery.is_symlink(), "D_delivery_symlink")
    original = delivery / "original.zip"
    if original.exists():
        require(file_hash(original) == digest, "D_original_zip_drift")
    else:
        with tempfile.TemporaryDirectory(prefix=".zip-copy-", dir=delivery) as work:
            copied = Path(work) / "original.zip"
            shutil.copyfile(archive, copied)
            require(file_hash(copied) == digest, "D_source_zip_changed_during_copy")
            os.replace(copied, original)
    output = delivery / "web-output"
    if not output.exists():
        with tempfile.TemporaryDirectory(prefix=".unpack-", dir=delivery) as work:
            stage = Path(work) / "content"
            extract_zip(original, stage, ArchiveBudget(stage, max_bytes=max_bytes))
            check_manifest(stage)
            os.replace(stage, output)
    return delivery, output, check_manifest(output), digest


def payload_from(root, manifest):
    require(not manifest.get("partition") and not (root / "partition.json").exists(),
            "D_partition_requires_local_merge")
    require(manifest.get("project") == "D" and manifest.get("subject") == "408", "D_408_package_required")
    require(re.fullmatch(r"[0-9a-f]{40}", str(manifest.get("source_commit", ""))), "D_snapshot_identity_required")
    require(manifest.get("snapshot_id", manifest["source_commit"]) == manifest["source_commit"], "D_snapshot_mismatch")
    require(manifest.get("publication_target", "local") == "local", "D_local_snapshot_required")
    require(re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,159}", str(manifest.get("run_id", ""))), "D_run_id_required")
    require(manifest.get("status") not in {"BLOCKED", "PARTIAL"}, "D_web_preparation_not_complete")
    require(manifest.get("artifact_kind") not in {"single_point_preparation_check", "incomplete_preparation_preservation_package", "morning_preparation_input"},
            "D_diagnostic_package_is_not_full_morning_delivery")
    day = manifest.get("target_date")
    require(isinstance(day, str) and dt.date.fromisoformat(day).isoformat() == day, "D_target_date_required")
    required = ("START_HERE.md", "due_concepts.json", "selected_items.jsonl", "supplemental_items.jsonl",
                "unresolved.jsonl", "coverage_report.json", "rationale.md")
    for name in required:
        source_file(root / name, root)
    concepts = read(root / "due_concepts.json")
    coverage = read(root / "coverage_report.json")
    unresolved = rows(root / "unresolved.jsonl")
    require(not unresolved and not coverage.get("unresolved_concept_ids"), "D_required_coverage_unresolved")
    body = {
        "subject": "408", "project": "D", "task_id": manifest["run_id"],
        "source_commit": manifest["source_commit"], "target_date": day,
        "due_version": manifest.get("due_version"), "due_concept_ids": concepts.get("concept_ids"),
        "selected_items": rows(root / "selected_items.jsonl"),
        "supplemental_items": rows(root / "supplemental_items.jsonl"),
        "unresolved": unresolved, "coverage": coverage, "output_root": str(root),
    }
    if (root / "substitute_items.jsonl").is_file():
        body["substitute_items"] = rows(root / "substitute_items.jsonl")
    if manifest.get("ordering_policy"):
        from morning_question_policy_408 import RECENCY_POLICY
        require(manifest["ordering_policy"] == RECENCY_POLICY, "D_ordering_policy_invalid")
        body["ordering_policy"] = RECENCY_POLICY
    for key in ("concept_priority_version", "concept_requests", "review_concept_ids"):
        require(key in manifest or key in concepts, "D_concept_context_missing:" + key)
        if key in manifest and key in concepts:
            require(manifest[key] == concepts[key], "D_concept_context_conflict:" + key)
        body[key] = manifest[key] if key in manifest else concepts[key]
    web_path = root / "web_variants.jsonl"
    if web_path.exists():
        body["web_variants"] = rows(web_path)
    if "asset_delivery" in manifest:
        body["asset_delivery"] = manifest["asset_delivery"]
        body["asset_refs"] = rows(root / "asset_refs.jsonl")
    if "question_presentation" in manifest:
        body["question_presentation"] = manifest["question_presentation"]
        if "question_presentation_crops" in manifest:
            body["question_presentation_crops"] = manifest["question_presentation_crops"]
    if manifest.get("web_execution_report"):
        from web_execution_report_408 import inspect_report
        published = native._published_library(body["source_commit"], native.REPO)
        report = inspect_report(root, manifest, published)
        require(not report.get("local_completion_required") and not report.get("unassigned_local_completion_required"),
                "D_local_completion_required_stage_return_and_preserve_successes")
        body["web_execution_report"] = report
    return body


def prepare(archive, *, repo, details, release, target_date=None, max_bytes=2 * 1024**3,
            scope_decision=None):
    repo, details = repo.resolve(), details.resolve()
    require(not details.is_relative_to(repo), "D_private_delivery_must_be_outside_formal_repository")
    deliveries = details / "review-deliveries/web-D"
    deliveries.mkdir(parents=True, exist_ok=True)
    delivery, root, manifest, digest = unpack(archive.resolve(), deliveries, max_bytes)
    root, manifest, asset_assembly = assemble_delivery(root, delivery, manifest, max_bytes=max_bytes)
    body = payload_from(root, manifest)
    if manifest.get("native_release_id") not in (None, release.name):
        raise ValueError("D_native_release_requires_matching_prepared_package")
    body["native_release_dir"] = str(release.resolve())
    if (root / "selection-contract.json").is_file():
        require(scope_decision is None, "D_selection_has_registered_scope_do_not_override")
        from morning_selection_408 import scope_for_return
        selection_contract = read(root / "selection-contract.json")
        require(manifest.get("input_version") == selection_contract.get("input_version"),
                "D_selection_manifest_input_version_drift")
        scope_decision = scope_for_return(repo, selection_contract, body, digest)
        # The registered limit contract requires one chronological queue across
        # originals and supplements, including returns omitting the optional
        # legacy manifest field. Registration was verified above.
        from morning_question_policy_408 import RECENCY_POLICY
        require(selection_contract.get("priority_order", [None, None])[1]
                == "oldest_actual_contact_known_first", "D_selection_ordering_contract_invalid")
        body["ordering_policy"] = RECENCY_POLICY
    if scope_decision is not None:
        source_file(scope_decision)
        body["local_scope_ref"] = {"path": str(scope_decision.resolve()), "sha256": file_hash(scope_decision)}
        from d_review_scope_408 import load_scope
        scope = load_scope(repo, body["local_scope_ref"], body, body["review_concept_ids"])
        require(scope.get("source_zip_sha256") == digest, "D_local_subset_zip_drift")
    require(target_date is None or target_date == body["target_date"], "D_requested_date_requires_matching_package")
    # This also protects a paused session from another date. Downloaded bytes
    # remain available; no accepted selection or active frontier is replaced.
    local_review.assert_no_active(repo)
    question_refs = native._published_library(body["source_commit"], repo).get("question_refs", {})
    recent = recent_formal_exams(control._node_snapshot(repo), question_refs, body["target_date"])
    recent.update(recent_verified_capture_exams(repo, body["target_date"]))
    require_true_exam_items(body, question_refs, recent, repo=repo)
    body["selected_items"] = order_items(body["selected_items"], body["review_concept_ids"])
    body["supplemental_items"] = order_items(
        body["supplemental_items"], body["review_concept_ids"], unclassified_class="variant"
    )
    body["substitute_items"] = order_items(body["substitute_items"], body["review_concept_ids"])
    source_order = {
        "selected_items": ordering_summary(body["selected_items"]),
        "supplemental_items": ordering_summary(
            body["supplemental_items"], unclassified_class="variant"
        ),
        "substitute_items": ordering_summary(body["substitute_items"]),
    }
    imported = native.import_d(body, repo=repo, details_root=details)
    write(delivery / "native-import-result.json", imported)
    require(imported.get("status") in {"IMPORTED_FOR_NATIVE_PREPARATION", "ALREADY_IMPORTED", "NO_DUE_CONCEPTS"},
            "D_import_not_admitted:" + str(imported.get("status")))
    receipt_path = Path(imported["receipt_path"])
    require(file_hash(receipt_path) == imported["receipt_sha256"], "D_import_receipt_hash_drift")
    receipt = read(receipt_path)
    require(receipt.get("task_id") == body["task_id"] and receipt.get("source_commit") == body["source_commit"],
            "D_import_receipt_identity_drift")
    execution = receipt["execution_due_run"]
    require(execution.get("execution_scope") == "D_concept_representatives", "D_concept_execution_scope_required")
    require(execution.get("full_due_run_sha256") == body["due_version"], "D_full_due_binding_drift")
    require(not receipt.get("unresolved_concept_ids"), "D_native_coverage_unresolved")
    prepared = None
    ordered = receipt.get("learning_items")
    if execution.get("due_count") and not ordered:
        # Explicit argument is intentional: a missing D pointer must never select
        # the generic full-due fallback or silently change the web representatives.
        current = native.load_imported_d_run(repo, body["target_date"])
        require(current == execution, "D_imported_execution_changed")
        prepared = control.prepare_campaign(repo, review_date=body["target_date"], details_root=details,
                                            release_dir=release, scheduler_run=execution)
        write(delivery / "native-preparation.json", prepared)
        require(prepared.get("status") == "ready" and prepared.get("all_segments_ready") is True,
                "D_native_preparation_incomplete:" + str(prepared.get("status")))
    supplements = receipt.get("supplemental_items", [])
    pre_campaign_ids = receipt.get("pre_campaign_supplemental_request_ids", [])
    post_campaign_ids = [row["request_id"] for row in supplements if row["request_id"] not in pre_campaign_ids]
    from intake_lib_408 import Repo
    from exam_tutoring_context_408 import _load_manifest, DEFAULT_STATE_REL, _freshness_reasons
    master = Repo(repo).master()
    source_points = {
        fid: list(dict.fromkeys(re.findall(r"(?:DS|CO|OS|CN)\d{2}-\d{2}",
            " ".join(str(master[fid].get(field) or "") for field in ("主知识点", "副知识点", "命中知识点")))))
        for fid in {item["question_id"] for item in body["selected_items"] + body["supplemental_items"] + body.get("substitute_items", [])}
        if fid in master
    }
    tutoring_manifest, _ = _load_manifest(repo / DEFAULT_STATE_REL)
    tutoring_reasons = _freshness_reasons(repo, tutoring_manifest)
    require(not tutoring_reasons, "D_tutoring_context_not_current:" + ",".join(tutoring_reasons))
    saving_contract = None
    if prepared:
        prepared_manifest = read(Path(prepared["preparation_ref"]) / "campaign-manifest.json")
        saving_contract = prepared_manifest.get("saving_contract")
    files_by_path = {row["relative_path"]: row for row in receipt["output_files"]}
    teaching_index = {}
    substitute_ids = {(row["question_id"], row["concept_id"]): row["request_id"]
                      for row in supplements if row.get("request_kind") == "due_representative_substitute"}
    if ordered:
        substitute_ids.update({(row["question_id"], row["concept_id"]): row["request_id"] for row in ordered})
    for item in receipt.get("selected_items", []) + body["supplemental_items"] + body.get("substitute_items", []):
        substitute_key = (item["question_id"], (item.get("review_target_concept_ids") or [None])[0])
        key = substitute_ids.get(substitute_key) or item.get("request_id") or item["question_id"]
        refs = {name: files_by_path[item[name]] for name in ("history_path", "teaching_path", "solution_path")
                if item.get(name) in files_by_path}
        teaching_index[key] = {"question_id": item["question_id"],
                               "review_target_concept_ids": item.get("review_target_concept_ids", []),
                               "source_knowledge_points": source_points.get(item["question_id"], []),
                               "source_points_are_not_automatic_current_weaknesses": True,
                               "source_commit": body["source_commit"], "private_after_attempt": refs}
        if asset_assembly:
            teaching_index[key]["private_source_images"] = [
                {**files_by_path[ref["package_path"]], "source_path": ref["source_path"],
                 "role": ref["role"], "visual_status": ref["visual_status"]}
                for ref in body["asset_refs"]
                if ref["question_id"] == item["question_id"] and ref["role"] in {"solution", "reference"}
                and ref.get("true_item_id") in (None, item.get("preferred_true_item_id") or item.get("true_item_id"))
            ]
    teaching_path = delivery / "teaching-index.json"
    write(teaching_path, teaching_index)
    image_report_path = delivery / "question-image-preflight.json"
    image_report = inspect_plan(receipt, details)
    image_report.update(plan_path=str(receipt_path), plan_sha256=imported["receipt_sha256"])
    write(image_report_path, image_report)
    handoff = {
        "schema": "cs408-D-deepseek-handoff-v1", "status": "PREPARED_NOT_STARTED" if prepared or supplements or ordered else "NO_DUE_CONCEPTS",
        "project": "D", "subject": "408", "target_date": body["target_date"],
        "native_release_dir": str(release.resolve()),
        "source_commit": body["source_commit"], "snapshot_id": manifest.get("snapshot_id", body["source_commit"]),
        "publication_target": "local", "due_version": body["due_version"],
        "concept_priority_version": body["concept_priority_version"], "source_zip_sha256": digest,
        "source_zip": str(delivery / "original.zip"), "output_root": str(root),
        "native_import_receipt": str(receipt_path), "native_import_sha256": imported["receipt_sha256"],
        "teaching_index_path": str(teaching_path), "teaching_index_sha256": file_hash(teaching_path),
        "question_image_preflight": {"path": str(image_report_path), "sha256": file_hash(image_report_path),
                                     "image_count": image_report["image_count"],
                                     "warning_image_count": image_report["warning_image_count"]},
        "image_delivery": "prepared_markdown", "learning_image_inspection_required": False,
        "knowledge_points_by_source_question": source_points,
        "tutoring_source_version_at_preparation": tutoring_manifest["source_version"],
        "saving_contract": saving_contract,
        "run_id": (prepared or {}).get("run_id"), "preparation_id": (prepared or {}).get("preparation_id"),
        "preparation_ref": (prepared or {}).get("preparation_ref"),
        "due_representative_count": execution["due_count"],
        "full_due_node_count": execution["full_due_count"],
        "supplemental_request_ids": [row["request_id"] for row in supplements],
        "pre_campaign_supplemental_request_ids": pre_campaign_ids,
        "post_campaign_supplemental_request_ids": post_campaign_ids,
        "due_representative_substitute_request_ids": [row["request_id"] for row in supplements
                                                     if row.get("request_kind") == "due_representative_substitute"],
        "suppressed_carryover_formal_node_ids": execution["suppressed_carryover_formal_node_ids"],
        "current_display_pointer": str(repo / ".private/morning-campaign-control/current-display" / (body["target_date"] + ".json")),
        "recommended_model": "MiMo V2.6 Flash", "learning_started": False,
        "question_source_order": source_order,
        "formal_write_count": 0, "learner_evidence_write_count": 0,
    }
    if receipt.get("local_scope_decision"):
        scope = receipt["local_scope_decision"]
        handoff["local_scope_ref"] = body["local_scope_ref"]
        handoff["admitted_review_concept_ids"] = scope["admitted_concept_ids"]
        handoff["deferred_concepts"] = scope["deferred_concepts"]
        handoff["full_review_concept_count"] = len(scope["full_review_concept_ids"])
        handoff["full_scope_learning_complete"] = False
    if ordered:
        handoff.update(learning_mode="ordered_native_D", ordering_policy=receipt["ordering_policy"],
                       learning_request_ids=[row["request_id"] for row in ordered],
                       learning_order=[{k: row[k] for k in ("request_id", "question_id", "concept_id", "execution_kind", "last_learning_contact")}
                           | {"true_item_id": row["selection"]["true_item_id"]} for row in ordered],
                       total_learning_items=len(ordered))
    handoff_path = delivery / "handoff.json"
    if asset_assembly:
        handoff["asset_assembly"] = asset_assembly
    write(handoff_path, handoff)
    prompt_path = delivery / "START_MIMO.md"
    write(prompt_path, f'''你是我的408晨间复盘学习会话。沿用我选定的 MiMo V2.6 Flash，不自动切换。

所有面向我的进度说明、判断依据摘要、错误说明和教学正文都用中文，代码、命令与必要原文除外。目标20–30秒内给出首段实质个性化反馈；保存故障先说明实际缺口并给当前题反馈，保持本题和原请求，不让源码排障先于教学。

仓库：{repo}
details：{details}
学习目标日期：{body["target_date"]}；实际学习/曝光仍用发生日期，不回填成目标日期。
本地准备交接：{handoff_path}
交接SHA256：{file_hash(handoff_path)}

先加载 kaoyan-408-morning-control，按交接身份开始或恢复。校验交接SHA；已有当前题时按one-call-turn的context只读命令合并恢复题面、首答、最近讨论和摘要，本轮不再另查status/resolve，也不追加恢复话术。尚未确认启动状态且run_id非null时只读一次目标日期status，核对 preparation_id/run_id；run_id为null则无campaign，按补充入口继续。若有active/paused则保留并恢复它。已准备的D集合每知识点一题，不能重跑选题、出题、独立审核或回退全量题级campaign。只有身份/来源已变化才报告具体缺口。

本场原due代表 {execution["due_count"]} 题，独立补充 {len(supplements)} 项；完整due分母 {execution["full_due_count"]}，被抑制题保持未答carry-over。网页自检变式与独立审核变式显示各自真实标签，不要求我再审核题目。

本交接已按绑定的实际题面身份确定性排序：统考真题在先，已核实来源原题其次，身份仍待原生核验者保持单列，变式排最后；同一层沿用原生知识点优先级。来源分类只看本次实际题面绑定，经固定快照原图/原文与原生门禁核实；不得凭关联源节点ID中的年份、verified或independent_reviewed标记分类。prepare已拒绝前一学习阶段含较低层题面而后一阶段仍有更高层题面的跨阶段倒置。任何active/paused会话继续原顺序，不用本交接重排。

开始后只按 kaoyan-408-daily-study-loop 逐题学习：先展示当前公开题面及来源，等我作答；分别判断选项与推理，一轮一个推进问题。每个相关轮次只读一次当前知识点摘要，复用题图和身份，不扫源码/全账本/全部历史。包内history_summary只是备题时的证据，不能覆盖更新后的current source_version。

新v2逐题只使用daily-study-loop/references/learning-entry.md的learning_turn_408.py固定入口，首次答题加载question-worker和wrong-intake，后续复用。入口返回必要个人摘要、当前版本和缺口，模型完整读取，不自行过滤。正文直接纠正当前断点，历史默认内部使用；只有我问历史才展开已核实证据和链接。只给字母时推理未观察，保存后问一个理由，不封正确或推进。

handoff中的teaching_index_path按正式question_id或补充request_id索引私有讲解/历史文件，并绑定规范知识点代码；变式优先用当前review_target_concept_ids，不能把原题所有涉及点直接说成本变式考点或弱点。首次需要答后讲解时只读当前材料，与一次小路由查询合并；同题复用。摘要每个相关轮次取当前source_version，不能永远沿用准备时版本。网页材料不代替当前判断，未作答页面不泄露答案。

若handoff带asset_assembly，已有原图已按固定快照和哈希装配在output_root内，不再让学习会话取件。private_source_images仅供答后或来源核对，原型参考图不是当前变式题面，不能放到未作答页面；装配不等于已完成视觉阅读。

新saving-contract-v2每题结束都封完整Capture，答对也保存；讨论只同题草稿。模型向固定入口交新增原话/真实消息引用、判断和intent，由程序衔接首答、追加和终态，完整Capture验证后才能到下一题，不需要我再说入库。不要手工串联answer-choice/finish-item、临时写包装程序或靠改终态标签绕过理由门禁。学习只存事实/曝光/进度，不写正式判分、FSRS或正式摘要。真实原题original_review；系统变式和D补充是concept_supplement，正式时不改关联原题FSRS。D独立补充仍按其v2 commit。旧active/paused保持原合同，不伪造历史Capture。

先完成已准备campaign，再按handoff中的supplemental_request_ids执行补充，以native_import_receipt作为补充display的plan；没有campaign时直接补充。答案与评分依据留在私有层，未作答时不读出答案。完成分别报告代表题、补充题与未答carry-over；下载、准备与展示不算学习完成。

我明确结束学习时，用固定入口intent=end封当前未解题并hold，不再发布下一题；暂歇用pause不封终态。已封题后到更正用correct-late绑定原题追加证据，保留原包和当前导航。日终按export_capture_day_408.py汇总已保存Capture及补充，未封题/缺件单列。总包交GPT-6 Pro正式增强，加强包返回后由enhancement-intake统一正式写入、判分、FSRS及个人概况/短摘要/路由更新，再发布同版快照。
''')
    prompt = prompt_path.read_text()
    if pre_campaign_ids:
        prompt = prompt.replace(
            "先加载 kaoyan-408-morning-control，按交接身份开始或恢复。",
            "先加载 kaoyan-408-morning-control，按交接身份开始或恢复。"
            "本交接有明确的pre_campaign_supplemental_request_ids：在campaign尚未启动时，"
            "先按d-concept-supplement入口逐题完成这些统考真题，然后才start已准备campaign。"
            "已active/paused的campaign仍优先恢复，不插入补充题。"
            "跨会话先按真实d-supplement-progress回执核对已封题，仅执行尚未完成的request_id，不重放已答题。")
        prompt = prompt.replace(
            "先完成已准备campaign，再按handoff中的supplemental_request_ids执行补充，以native_import_receipt作为补充display的plan；没有campaign时直接补充。",
            "严格按三个阶段执行：handoff.pre_campaign_supplemental_request_ids中的统考真题；"
            "已准备campaign；handoff.post_campaign_supplemental_request_ids中的其余补充。"
            "两个补充阶段均以native_import_receipt作为display的plan。"
            "每道题保存成功后再推进；阶段一完成前不能启动campaign，已活动campaign不得插题。")
    if receipt.get("local_scope_decision"):
        scope = receipt["local_scope_decision"]
        prompt = ("请用 MiMo V2.6 Flash，通过“复盘 skill”、“错题讲解 skill”和“快速入库 skill”进行学习。\n\n"
            "复盘使用 kaoyan-408-morning-control 和 kaoyan-408-daily-study-loop；"
            "讲解使用 kaoyan-408-question-worker；快速保存遵守 kaoyan-408-wrong-intake 与当前v2逐题Capture合同。"
            "按当前阶段加载所需入口，不一开始展开全部工程文档。\n\n"
            + ("我的要求是全部使用真实408统考题，不使用变式题。" if scope.get("authorization_kind") == "explicit_user_true_exams_only"
             else "我已明确选择本次只用完成且通过本地验收的题目。")
            + f"本次采用{len(scope['admitted_concept_ids'])}个知识点，"
            f"其余{len(scope['deferred_concepts'])}个点保持待处理、未作答；不能把本次结束记作原完整范围全部完成。\n\n"
            + prompt)
    if ordered:
        scope = receipt.get("local_scope_decision") or {}
        first = ordered[0]["request_id"]
        prompt = f'''请沿用我选择的 MiMo V2.6 Flash，开始 {body["target_date"]} 的408晨间复盘。

讲解使用 kaoyan-408-question-worker（错题讲解 Skill），每题保存使用 kaoyan-408-wrong-intake（快速入库 Skill）的完整证据合同。先加载 kaoyan-408-morning-control，再读取其 references/d-concept-supplement.md 固定入口；逐题使用 v9 d-supplement-context / d-supplement-turn，按当前阶段加载，不遍历工程文档。

仓库：{repo}
本地交接：{handoff_path}
交接 SHA256：{file_hash(handoff_path)}
本地导入回执：{receipt_path}
导入回执 SHA256：{handoff["native_import_sha256"]}
学习会话身份：D-ORDERED-{body["target_date"]}-{digest[:12]}

先读取并验证交接及回执。此交接 learning_mode=ordered_native_D，已准备但未开始，采用原生D固定逐题入口统一执行原题与同点补充。run_id为空是此入口的正常状态，不调用campaign start/prepare，不重选、不出题、不重做预处理。已有其他active/paused会话时保护它，不强关。

全部 {len(ordered)} 道都必须是已核验EXAM408统考真题，同一题只做一次。严格使用交接 learning_request_ids 与回执 learning_items 的共同顺序：最近一次真实做题或展示日期越早越靠前；日期未知排在已知之后。同日期保持冻结顺序，不按考试年份、科目、知识点优先级、导入或入库日期改排。

本批有 {execution["due_count"]} 道正式原题复做、{len(supplements)} 道同知识点真题补充，二者按日期交错排列。原题保持 original_review 和准确 formal_id；补充保持 concept_supplement，不能改关联原题的复做日期、错误次数或FSRS。其余 {len(scope.get("deferred_concepts", []))} 个目标点保留待处理、未作答，不能把本队列结束记作全范围已学。

首次启动仅展示队列第一题，request_id={first}。先核对本会话原始对话及已保存的d-learning结果，若此前已开始则只读恢复当前题，不重放已完成题或曝光。全新会话可执行：

/usr/local/bin/python3 {repo}/scripts/d_concept_supplement_408.py display --repo {repo} --plan {receipt_path} --request-id {first} --idempotency-key {body["target_date"]}-ordered-{first} --review-date {body["target_date"]}

实际学习日期若已变化，review-date与新曝光键使用实际当天，不回填目标日期。展示返回的全部公开题图与真实来源，等我作答；用返回的receipt_path及receipt_sha256建立固定入口input_context，handoff使用上述路径，session_id使用上述学习会话身份。后续由固定入口从已绑定队列自动取下一题，不手填后继ID。

答案、解析、讲解和历史依据在私有层；未作答不泄露答案，不预先求解下一题。作答后按当前证据分别核对答案与理由，定位第一个断点；只给字母时先问一个关键理由，不直接封正确。每轮最多一个推进问题并等待。明确要求直接讲解时直接讲。个人依据按当前题小点和current source_version读取一次，历史默认内部使用，包内旧历史不覆盖最新摘要。

每个终态都保存完整真实对话、原图和附件，答对也保存，不需要我再说入库。固定入口验证Capture和完整包后再导航；提示后完成与独立完成分开。未完成讨论继续本题，不冒充终态。此次学习只记录事实、曝光和进度，正式判分、FSRS和个人摘要待网页加强包后统一更新。明确结束使用intent=end并hold；单题跳过用skip，不伪造作答。保存结果不明时保留同一请求和身份，只恢复缺层。

所有说明与讲解使用中文，遵守仓库聊天排版要求。目标20–30秒给出首段实质反馈，不把进度占位当教学，不为速度遗漏原始记录或必要推导。现在开始第一道尚未完成的题。
'''
    prompt = prompt.replace("v9 d-supplement-context", "v10 d-supplement-context")
    prompt = prompt.replace("目标20–30秒", "目标10–20秒")
    prompt += '''
晨间题图在备题阶段处理，学习时直接展示返回的presentation_markdown或全部原图链接，不调用view_image、不裁图、放大、OCR或修图，不在展示下一题前求解。已有显示缺陷也不走学习中修图兜底，我会在网站核对原题；保留真实题号、来源、答案依据和我的具体断点。
答后D context一次返回当前题grading_reference和必要个人摘要，已有依据复用；不单独读答案文件。答案和理由核实后先给简短实质反馈，同轮继续保存；反馈不声称已保存，Capture与完整包校验通过后才展示后继。真实反馈由原生消息入口完整封存。
functions.exec使用v10固定助手；纯shell以yield_time_ms=30000调用同一Python入口，返回后直接展示题图链接，不再读图。超过等待窗口沿同一session继续，不能重提保存。保留当前模型和思考档位，不为速度省略证据、答案保护或必要推导。
'''
    write(prompt_path, prompt.replace("v10", "v11"))
    return {"status": handoff["status"], "handoff_path": str(handoff_path), "prompt_path": str(prompt_path),
            "run_id": handoff["run_id"], "due_representative_count": execution["due_count"],
            "supplemental_count": len(supplements), "formal_write_count": 0, "learner_evidence_write_count": 0}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--zip", type=Path, required=True)
    parser.add_argument("--repo", type=Path, default=native.REPO)
    parser.add_argument("--details-root", type=Path, default=control.DEFAULT_DETAILS_ROOT)
    parser.add_argument("--date")
    parser.add_argument("--release", type=Path, default=control.DEFAULT_RELEASE)
    parser.add_argument("--scope-decision", type=Path,
                        help="Existing local decision recording an explicit user-approved narrowed scope")
    parser.add_argument("--max-unpacked-bytes", type=int, default=2 * 1024**3)
    args = parser.parse_args()
    try:
        result = prepare(args.zip, repo=args.repo, details=args.details_root, release=args.release,
                         target_date=args.date, max_bytes=args.max_unpacked_bytes, scope_decision=args.scope_decision)
    except Exception as exc:
        print(json.dumps({"status": "BLOCKED", "reason": str(exc),
                          "detail": getattr(exc, "detail", None),
                          "recovery": "Keep the same ZIP and native receipts; resume only the unfinished preparation step."}, ensure_ascii=False))
        return 1
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
