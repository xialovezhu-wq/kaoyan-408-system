#!/usr/bin/env python3
"""Prepare day-scoped 408 study material backed by completed intake."""
from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import os
from pathlib import Path
import re
import stat
import sys
import tempfile
import zipfile
from zoneinfo import ZoneInfo

import runtime_paths_408 as _paths

sys.dont_write_bytecode = True
REPO = Path(__file__).resolve().parents[1]
BRIDGE = _paths.bridge_root()
sys.path.insert(0, str(BRIDGE))
from bridge_archives import safe_path

INPUT_SCHEMA = "cs408-evening-input-v1"
PLAN_SCHEMA = "cs408-evening-plan-v1"
FOCUSED_POLICY = "focused-qa-v1"
TERMINAL = {"curated", "already_current", "concept_only", "user_removed"}
NATIVE_SOURCE = "native_teaching"
ZERO_WRITES = {"formal_write_count": 0, "capture_write_count": 0,
               "learning_write_count": 0, "model_call_count": 0}
RESOURCE = Path("codex-skill-sources/kaoyan-408-evening-review/references")


def require(ok, reason):
    if not ok:
        raise ValueError(reason)


def encoded(value):
    return (json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2) + "\n").encode()


def sha(raw):
    return hashlib.sha256(raw).hexdigest()


def read_json(path):
    return json.loads(Path(path).read_bytes())


def details_for(repo):
    return repo.parent / ("kaoyan-408-details" if repo == REPO else "details")


def day_scope(repo, day):
    import enhancement_intake_408 as native
    state = native._capture_state(repo)
    rows = []
    for cid, value in sorted(state.get("captures", {}).items()):
        if value.get("study_date") != day:
            continue
        rows.append({"capture_id": cid, "capture_payload_sha256": value["payload_sha256"],
                     "quality_status": value.get("quality_status"), "formal_id": value.get("formal_id"),
                     "evidence_refs": value["capture"].get("stable_evidence_refs", []),
                     "supplement_history": value.get("supplement_history", [])})
    require(rows, "当天没有已记录Capture：" + day)
    pending = [r["capture_id"] + ":" + str(r["quality_status"]) for r in rows
               if r["quality_status"] not in TERMINAL]
    require(not pending, "当天增强尚未完成：" + ", ".join(pending))
    return rows


def local_completion_proof(repo, prepared_sha, state):
    """Keep historical closeout proof separate from the current teaching read."""
    import enhancement_intake_408 as native
    require(state.get("formal_completion_status") in {"COMPLETE", "COMPLETE_WITH_NEEDS_USER"},
            "增强本机收尾未完成")
    event_id = native._postcommit_event_id(state, prepared_sha)
    followup = state.get("followup") or {}
    local = followup.get("local_tutoring") or {}
    source_version = local.get("source_version") or ""
    require(followup.get("event_id") == event_id
            and local.get("status") in {"refreshed", "already_current"}
            and native.SHA256_RE.fullmatch(source_version), "缺少绑定的本机讲题刷新回执")
    readback = state.get("personalization_readback")
    if readback is not None:
        require((readback.get("status") == "VERIFIED_CURRENT" and readback.get("source_version") == source_version)
                or (readback.get("status") == "NOT_APPLICABLE" and readback.get("point_count") == 0),
                "本机摘要验回未完成或版本不符")
    proof = {"basis": "local_followup", "event_id": event_id, "source_version": source_version}
    quality = state.get("formal_quality")
    path = native.quality._completed_path(repo, prepared_sha)
    if quality or path.exists():
        require(path.is_file() and not path.is_symlink(), "缺少本机正式质量回执")
        raw = path.read_bytes()
        require(quality and json.loads(raw) == quality
                and quality.get("status") == "VERIFIED_CURRENT"
                and quality.get("prepared_sha256") == prepared_sha
                and quality.get("native_event_id", event_id) == event_id
                and quality.get("source_version") == source_version, "本机正式质量回执身份不符")
        proof.update(basis="formal_quality", receipt_sha256=sha(raw))
    return proof


def completed_enhancements(repo, details, day, scope):
    """Use existing immutable prepared identities; do not replay any writer."""
    import enhancement_intake_408 as native
    required = {r["capture_id"] for r in scope if r["quality_status"] != "user_removed"}
    found, points, bundles, errors = set(), set(), {}, []
    for path in sorted((details / "enhancement-intake/prepared-receipts").glob("*.json")):
        candidate = read_json(path)
        if candidate.get("study_date") != day or not (set(candidate.get("capture_ids", [])) & required):
            continue
        try:
            receipt, _, identity = native._read_prepared(path)
            require(Path(receipt["repo_root"]).resolve() == repo, "增强回执属于其他项目")
            state = native._load_operation(receipt, identity)
            if (not native._formal_layers_complete(state) or not native._archives_complete(state)
                    or not native._campaign_successors_complete(state)):
                continue
            completion = local_completion_proof(repo, identity, state)
            complete = {cid for cid, row in state["items"].items() if row["status"] == "COMPLETE"} & required
            if not complete:
                continue
            root, _, authored, _, bindings = native._revalidate_package(receipt)
            points.update(receipt["affected_points"])
            members = ["enhancement.json"]
            if "evening_review_seed.json" in bindings:
                members.append("evening_review_seed.json")
            files = {name: (root / name).read_bytes() for name in members}
            require(all(sha(raw) == bindings[name] for name, raw in files.items()), "增强材料在读取中变化")
            files["completion.json"] = encoded({"prepared_receipt_sha256": identity,
                "capture_ids": sorted(complete), "local_completion": completion})
            bundles[identity] = {"files": files, "capture_ids": sorted(complete),
                                 "prepared_receipt_sha256": identity,
                                 "items": [i for i in authored["items"] if i["capture_id"] in complete]}
            found.update(complete)
        except (ValueError, OSError, KeyError, native.EnhancementError) as exc:
            errors.append(path.name + ": " + str(exc))
    missing = sorted(required - found)
    require(not missing, "缺少增强完成、归档或本机验回证明：" + ", ".join(missing) +
            ("；" + "；".join(errors) if errors else ""))
    return sorted(points), bundles


def native_context(repo, points):
    from exam_tutoring_context_408 import QUERY_INPUT_SCHEMA, query_context
    from study_personalization_408 import current_identity

    current = current_identity(repo)
    require(current.get("status") == "ready", "本机讲题视图未就绪：" + ", ".join(current.get("reasons", [])))
    version = current["source_version"]
    records, pages = {}, []
    for start in range(0, len(points), 32):
        batch = points[start:start + 32]
        view = query_context(repo, {"schema": QUERY_INPUT_SCHEMA, "stage": "post_answer",
                                   "knowledge_points": batch, "max_profiles": 0},
                             teaching_view="direct", materials="core")
        require(view.get("status") == "available" and view.get("source_version") == version
                and view.get("view") == "direct", "本机讲题视图读取失败或版本变化：" + ", ".join(batch))
        summaries = {row["knowledge_point"]: row for row in view.get("records", [])}
        require(set(summaries) == set(batch) and len(summaries) == len(view["records"]),
                "本机摘要范围不完整：" + ", ".join(sorted(set(batch) - set(summaries))))
        stale = [point for point, row in summaries.items() if row.get("status") != "semantic_current"
                 or (row.get("personal_context") or {}).get("complete") is not True]
        require(not stale, "个人摘要尚未完成增强：" + ", ".join(stale))
        records.update({point: {"source_version": version, "summary": row}
                        for point, row in summaries.items()})
        pages.append({"requested_points": batch, "returned_points": list(summaries),
                      "source_version": version, "complete": True, "missing_points": []})
    identity = {"source_basis": NATIVE_SOURCE, "source_commit": version,
                "local_version": current["version"], "formal_version": current["formal_version"]}
    return identity, {"records": records, "pages": pages}


def snapshot_context(points):
    from study_mcp import LibraryStore
    from export_morning_input_408 import personal_records
    store = LibraryStore(BRIDGE)
    try:
        snap = store.snapshot("408")
        freshness = store.freshness(snap)
        require(freshness.get("status") == "current", "Study Library当前快照未就绪")
        # The local reader can return a complete profile even when it exceeds
        # the page budget for web handoffs.
        personal, pages = personal_records(store, snap.commit, points, max_bytes=1024 * 1024)
        stale = [p for p, r in personal.items()
                 if r.get("summary", {}).get("learning_profile", {}).get("status") != "current"]
        require(not stale, "个人摘要尚未完成增强：" + ", ".join(stale))
        identity = {"source_commit": snap.commit, "snapshot_id": snap.commit,
                    "local_version": snap.library["local_version"],
                    "freshness": freshness, "personalization_entrypoint": snap.library["entrypoints"]["personalization"]}
        return identity, {"records": personal, "pages": pages}
    finally:
        store.close()


def zip_inventory(archive, max_bytes):
    require(archive.infolist(), "空ZIP")
    names, total = set(), 0
    for member in archive.infolist():
        name = member.filename
        # Directory entries may be produced by ordinary ZIP applications.
        safe_path(name[:-1] if member.is_dir() else name)
        require(name not in names and not stat.S_ISLNK(member.external_attr >> 16), "ZIP重复路径或符号链接")
        names.add(name)
        require(not member.flag_bits & 1, "不接受加密ZIP")
        total += member.file_size
        require(total <= max_bytes, "ZIP超过允许大小")
    return {m.filename for m in archive.infolist() if not m.is_dir()}


def verified_input(path):
    with zipfile.ZipFile(path) as archive:
        names = zip_inventory(archive, 2 * 1024**3)
        manifest = json.loads(archive.read("manifest.json"))
        require(manifest.get("schema") == INPUT_SCHEMA and manifest.get("subject") == "408", "不是408晚间输入包")
        rows = manifest["files"]
        expected = {r["path"] for r in rows}
        require(len(expected) == len(rows) and expected == names - {"manifest.json"}, "输入文件清单不完整")
        for row in rows:
            safe_path(row["path"])
            raw = archive.read(row["path"])
            require(len(raw) == row["size_bytes"] and sha(raw) == row["sha256"], "输入文件哈希改变：" + row["path"])
        raw = archive.read("handoff.json")
        require(sha(raw) == manifest["handoff_sha256"], "handoff哈希不匹配")
        handoff = json.loads(raw)
        require(handoff["study_date"] == manifest["study_date"] and
                handoff["source_commit"] == manifest["source_commit"], "输入日期或版本不一致")
        return manifest, handoff


def validate_live(repo, handoff):
    require(day_scope(repo, handoff["study_date"]) == handoff["captures"], "当天学习资料已变化，请在本地重新准备本次材料")
    if handoff.get("authoring_mode") == "local":
        from study_personalization_408 import current_identity
        current = current_identity(repo)
        if handoff.get("source_basis") == NATIVE_SOURCE:
            same = (current.get("source_version"), current.get("version"), current.get("formal_version")) == (
                handoff["source_commit"], handoff["local_version"], handoff["formal_version"])
        else:
            # Older local packs recorded the native fingerprints alongside the replica token.
            saved = handoff.get("freshness") or {}
            same = all(saved.get("current_" + field) and saved["current_" + field] == current.get(key)
                       for field, key in (("formal_version", "formal_version"), ("personalization_version", "version")))
        same = same and current.get("status") == "ready"
    else:
        identity, _ = snapshot_context(handoff["knowledge_points"])
        same = (identity["source_commit"], identity["local_version"]) == (handoff["source_commit"], handoff["local_version"])
    require(same, "知识库版本已变化，请在本地重新准备材料并校对问答")


def export(day, repo=REPO, *, local_author=True):
    from export_capture_day_408 import _export_day
    repo = Path(repo).resolve()
    require(dt.date.fromisoformat(day).isoformat() == day, "学习日期格式错误")
    require(day <= dt.datetime.now(ZoneInfo("Asia/Shanghai")).date().isoformat(), "不能准备未来的学习事实")
    scope = day_scope(repo, day)
    details = details_for(repo)
    points, bundles = completed_enhancements(repo, details, day, scope)
    identity, personal = native_context(repo, points) if local_author else snapshot_context(points)
    from morning_selection_408 import policy_for
    import exam_frequency_priority_408 as exam_priority
    frequency = (exam_priority.priority_bundle(personal['records'], identity['source_commit'])
                 if (policy_for(repo) or {}).get('frequency_priority_policy') == exam_priority.POLICY else None)
    handoff = {"schema": INPUT_SCHEMA, "subject": "408", "study_date": day,
               "timezone": "Asia/Shanghai", "duration_seconds": 900, "review_policy": FOCUSED_POLICY, **identity,
               "captures": scope, "knowledge_points": points,
               "enhancement_receipts": sorted(bundles),
               "raw_index": "raw/capture_index.json", "personal_context": "personal-context.json",
               "contract_sha256": sha((repo / "schema/evening-review-408-v1.md").read_bytes()),
               "scope_note": "全部当天已记录并封存Capture；不声称覆盖未保存的对话。", **ZERO_WRITES}
    if local_author:
        handoff["authoring_mode"] = "local"
    else:
        handoff["web_prompt_sha256"] = sha((repo / RESOURCE / "WEB_PROJECT_PROMPT.txt").read_bytes())
    if frequency is not None:
        require(set(frequency['points']) == set(points), '晚间同版频次点范围不完整')
        handoff.update(frequency_priority_policy=exam_priority.POLICY, frequency_priority=frequency,
                       question_source_policy='new_mechanism_scenarios_no_exact_question_replay',
                       exact_item_cooldown_days=14)
    handoff_raw = encoded(handoff)
    binding = sha(handoff_raw)
    from web_transfer_root_408 import base_for
    root, _ = base_for("evening-input", details, "evening-review/inputs")
    base = root / day
    base.mkdir(parents=True, exist_ok=True)
    target = base / binding[:20]
    raw_scope_sha = sha(encoded([{key: row[key] for key in (
        "capture_id", "capture_payload_sha256", "evidence_refs", "supplement_history")}
        for row in scope]))
    # The raw exporter registers its exact ZIP path. Keep that source outside
    # the temporary evening assembly so its registration remains usable.
    raw_dir = "local-raw-sources" if local_author else "raw-sources"
    raw_path = base / raw_dir / identity["source_commit"] / raw_scope_sha[:20] / "408_raw_capture_day.zip"
    raw_source_binding = None
    if target.exists():
        old, _ = verified_input(target / "408_evening_input.zip")
        require(old["handoff_sha256"] == binding, "已有交接内容冲突")
        with zipfile.ZipFile(target / "408_evening_input.zip") as archive:
            if not local_author:
                require((target / "START_WEB.txt").read_bytes() == archive.read("START_WEB.txt"), "随包提示词已变化")
            if "raw-source-binding.json" in archive.namelist():
                raw_source_binding = json.loads(archive.read("raw-source-binding.json"))
                require(raw_source_binding["zip_path"] == str(raw_path)
                        and raw_path.is_file() and not raw_path.is_symlink()
                        and sha(raw_path.read_bytes()) == raw_source_binding["sha256"], "晚间原始来源ZIP缺失或漂移")
    else:
        raw_path.parent.mkdir(parents=True, exist_ok=True)
        source = ({"evening_source_version": identity["source_commit"]} if local_author else
                  {"baseline_snapshot_id": identity["source_commit"], "web_handoff": True})
        result = _export_day(repo, day, output=raw_path, details_root=details, **source)
        require(result.get("status") in {"EXPORTED", "ALREADY_EXPORTED"}, "完整封存未就绪：" + json.dumps(result, ensure_ascii=False))
        raw_source_binding = {"zip_path": str(raw_path), "sha256": sha(raw_path.read_bytes()),
                              "source_commit": identity["source_commit"], "capture_ids": [r["capture_id"] for r in scope],
                              "teaching_alignment_registration": result.get("teaching_alignment_registration")}
        with tempfile.TemporaryDirectory(prefix=".evening-", dir=base) as temp:
            stage = Path(temp)
            with zipfile.ZipFile(raw_path) as raw_zip:
                zip_inventory(raw_zip, 2 * 1024**3)
                raw_manifest = json.loads(raw_zip.read("manifest.json"))
                require(raw_manifest.get("source_commit") == identity["source_commit"]
                        and {(r["capture_id"], r["capture_payload_sha256"]) for r in raw_manifest["captures"]}
                        == {(r["capture_id"], r["capture_payload_sha256"]) for r in scope}, "原包范围或本机版本变化")
                additions = {"handoff.json": handoff_raw, "personal-context.json": encoded(personal),
                             "raw-source-binding.json": encoded(raw_source_binding),
                             "contracts/evening-review-408-v1.md": (repo / "schema/evening-review-408-v1.md").read_bytes()}
                if frequency is not None:
                    additions['frequency-priority.json'] = encoded(frequency)
                for key, bundle in bundles.items():
                    for name, raw in bundle["files"].items():
                        additions[f"enhancements/{key}/{name}"] = raw
                if not local_author:
                    prompt = (repo / RESOURCE / "WEB_PROJECT_PROMPT.txt").read_text()
                    prompt += f"\n本次学习日期：{day}\n固定source_commit：{identity['source_commit']}\nhandoff_sha256：{binding}\n当天Capture数：{len(scope)}。上传408_evening_input.zip后开始备课。\n"
                    additions["START_WEB.txt"] = prompt.encode()
                inventory = []
                with zipfile.ZipFile(stage / "408_evening_input.zip", "w", zipfile.ZIP_DEFLATED) as out:
                    for name in raw_zip.namelist():
                        if name.endswith("/"):
                            continue
                        # Preserve source bytes and their paths for exact evidence lookup.
                        dest = "raw/" + name
                        require(dest not in additions, "交接成员重名")
                        data = raw_zip.read(name)
                        out.writestr(dest, data)
                        inventory.append({"path": dest, "sha256": sha(data), "size_bytes": len(data)})
                    for name, data in sorted(additions.items()):
                        out.writestr(name, data)
                        inventory.append({"path": name, "sha256": sha(data), "size_bytes": len(data)})
                    manifest = {"schema": INPUT_SCHEMA, "subject": "408", "study_date": day,
                                "source_commit": identity["source_commit"], "handoff_sha256": binding,
                                "files": inventory, **ZERO_WRITES}
                    out.writestr("manifest.json", encoded(manifest))
                if not local_author:
                    (stage / "START_WEB.txt").write_bytes(additions["START_WEB.txt"])
            validate_live(repo, handoff)
            verified_input(stage / "408_evening_input.zip")
            os.rename(stage, target)
    from web_transfer_root_408 import register_output
    registration = register_output(target, details=details, purpose="evening-review-local-source" if local_author else "evening-review-web-input")
    raw_registration = (register_output(raw_path, details=details,
                        purpose="evening-review-native-raw-source" if local_author else "evening-review-fixed-snapshot-raw-source")
                        if raw_source_binding is not None else None)
    return {"status": "READY_FOR_LOCAL_AUTHORING" if local_author else "READY_FOR_WEB_PREPARATION",
            "study_date": day,
            "capture_count": len(scope), "source_commit": identity["source_commit"],
            "zip_path": str(target / "408_evening_input.zip"),
            **({"handoff_sha256": binding} if local_author else {"prompt_path": str(target / "START_WEB.txt")}),
            "raw_source_binding": raw_source_binding,
            "raw_source_storage_registration": raw_registration,
            "storage_registration": registration, **ZERO_WRITES}


def load_plan(path):
    path = Path(path)
    if zipfile.is_zipfile(path):
        with zipfile.ZipFile(path) as archive:
            names = zip_inventory(archive, 4 * 1024**2)
            require("plan.json" in names and names <= {"plan.json", "EXECUTION_REPORT.md"}, "回包只接受根目录plan.json及可选执行报告")
            return json.loads(archive.read("plan.json"))
    require(path.suffix == ".json" and path.stat().st_size <= 4 * 1024**2, "需要本地 plan.json 或兼容的旧 ZIP")
    return read_json(path)


def validate_plan(plan, manifest, handoff):
    from evening_visual_408 import DAILY_READING_SCHEMA, SCHEMA, validate, validate_daily
    if isinstance(plan, dict) and plan.get("schema") == DAILY_READING_SCHEMA:
        return validate_daily(plan, manifest, handoff)
    if isinstance(plan, dict) and plan.get("schema") == SCHEMA:
        return validate(plan, manifest, handoff)
    require(isinstance(plan, dict) and plan.get("schema") == PLAN_SCHEMA and plan.get("subject") == "408", "晚间plan身份错误")
    require(plan.get("status") == "READY", "问答计划尚未完成；保留现有成稿，在本地续办缺项")
    require(plan.get("review_policy") in {None, FOCUSED_POLICY}, "未知晚间问答规则")
    focused = plan.get("review_policy") == FOCUSED_POLICY
    require(handoff.get("review_policy") != FOCUSED_POLICY or focused, "新输入须使用focused-qa-v1少量问答规则")
    for key in ("study_date", "source_commit", "handoff_sha256"):
        require(plan.get(key) == manifest[key], "晚间plan绑定错误：" + key)
    require(plan.get("duration_seconds") == 900, "晚间时长上限须为900秒")
    wrapup = plan.get("wrapup_seconds")
    require(type(wrapup) is int and 30 <= wrapup <= 180, "收尾预算须为30至180秒")
    questions = plan.get("questions")
    require(isinstance(questions, list) and 1 <= len(questions) <= (5 if focused else 15), "问题数量无效")
    captures = {r["capture_id"] for r in handoff["captures"]}
    points = set(handoff["knowledge_points"])
    ids, core_seconds, core_count = set(), 0, 0
    for q in questions:
        qid = q.get("question_id")
        require(isinstance(qid, str) and re.fullmatch(r"E408-\d{2}", qid) and qid not in ids, "题号无效或重复")
        ids.add(qid)
        require(q.get("concept_key") in points, "题目知识点不在当天增强范围：" + qid)
        require(q.get("slot") in ({"core"} if focused else {"core", "reserve"}),
                "少量问答只接受core，不附备用题" if focused else "题目必须标core或reserve")
        seconds = q.get("budget_seconds")
        require(type(seconds) is int and 30 <= seconds <= 300, "单题预算须为30至300秒")
        if q["slot"] == "core":
            core_seconds += seconds
            core_count += 1
        refs = q.get("source_capture_ids")
        require(isinstance(refs, list) and refs and all(isinstance(c, str) for c in refs)
                and len(set(refs)) == len(refs) and set(refs) <= captures, "题目引用了范围外Capture")
        require(set(q) == {"question_id", "concept_key", "mechanism", "slot", "budget_seconds", "source_capture_ids", "student_prompt", "teacher"}, "题目含未知或缺失字段")
        for key in ("mechanism", "student_prompt"):
            require(isinstance(q.get(key), str) and q[key].strip(), "缺少题面或机制：" + qid)
        teacher = q.get("teacher")
        require(isinstance(teacher, dict), "缺少教师材料")
        require(set(teacher) == {"expected_answer", "essential_points", "common_misconceptions", "hints", "followup_prompt"}, "教师材料字段不完整")
        require(isinstance(teacher["expected_answer"], str) and teacher["expected_answer"].strip(), "教师文本为空")
        require(isinstance(teacher["followup_prompt"], str)
                and (focused or teacher["followup_prompt"].strip()), "追问须为文本；新问答无需追问时可留空")
        for key in ("essential_points", "common_misconceptions", "hints"):
            require(isinstance(teacher[key], list) and all(isinstance(t, str) and t.strip() for t in teacher[key]), "教师要点格式错误")
        require(teacher["essential_points"] and teacher["hints"], "缺少判断要点或提示")
    minimum_seconds = 1 if focused else 600
    require(core_count and minimum_seconds <= core_seconds + wrapup <= 900,
            "主问答及收尾不得超过15分钟，不凑最低时长" if focused else "主问答及收尾须为10至15分钟，备用不计入")
    coverage = plan.get("coverage")
    require(isinstance(coverage, list) and len(coverage) == len(captures), "全天coverage数量不一致")
    seen = set()
    by_id = {q["question_id"]: set(q["source_capture_ids"]) for q in questions}
    for row in coverage:
        cid = row.get("capture_id")
        require(cid in captures and cid not in seen, "coverage缺项、重复或多出Capture")
        seen.add(cid)
        disposition = row.get("disposition")
        require(disposition in {"asked", "grouped", "deferred", "no_question"}, "coverage处理类型无效")
        refs = row.get("question_ids")
        require(isinstance(refs, list) and all(isinstance(q, str) for q in refs)
                and len(refs) == len(set(refs)) and set(refs) <= ids, "coverage题号无效")
        require(bool(refs) == (disposition in {"asked", "grouped"}), "coverage题号与处理类型不符")
        require(set(refs) == {qid for qid, sources in by_id.items() if cid in sources}, "coverage与题源未双向绑定")
        require(isinstance(row.get("reason"), str) and row["reason"].strip(), "coverage缺少取舍依据")
    if handoff.get('frequency_priority_policy'):
        validate_frequency_selection(plan, handoff)
    return core_seconds + wrapup


def validate_frequency_selection(plan, handoff):
    """Check declared selection; the evidence reasons still need semantic review."""
    from exam_frequency_priority_408 import POLICY
    require(handoff.get('frequency_priority_policy') == POLICY, '晚间频次策略未知')
    audit = plan.get('selection_audit')
    require(isinstance(audit, dict) and set(audit) == {'frequency_priority_policy', 'points', 'question_sources'}
            and audit.get('frequency_priority_policy') == POLICY, '缺少晚间频次选题核验')
    frequency = handoff['frequency_priority']['points']
    questions = {q['question_id']: q for q in plan['questions']}
    captures = {row['capture_id'] for row in handoff['captures']}
    reviews = audit.get('points')
    require(isinstance(reviews, list) and len(reviews) == len(frequency), '晚间频次核验范围不完整')
    seen = {}
    for row in reviews:
        require(isinstance(row, dict) and set(row) == {'concept_key', 'disposition', 'question_ids', 'source_capture_ids', 'reason'},
                '晚间频次核验字段错误')
        point = row['concept_key']
        require(point in frequency and point not in seen, '晚间频次核验点缺失或重复')
        seen[point] = row
        expected = {qid for qid, question in questions.items() if question['concept_key'] == point}
        refs = row['question_ids']
        require(isinstance(refs, list) and all(isinstance(qid, str) for qid in refs)
                and len(set(refs)) == len(refs) and set(refs) == expected, '晚间频次核验题号未绑定')
        require(row['disposition'] in {'scheduled', 'no_question', 'deferred'}
                and bool(refs) == (row['disposition'] == 'scheduled'), '晚间频次核验状态错误')
        evidence = row['source_capture_ids']
        require(isinstance(evidence, list) and evidence and all(isinstance(cid, str) for cid in evidence)
                and len(set(evidence)) == len(evidence) and set(evidence) <= captures,
                '晚间频次核验缺少当天真实证据')
        require(isinstance(row['reason'], str) and row['reason'].strip(), '晚间频次核验缺少取舍依据')
        require(all(set(questions[qid]['source_capture_ids']) <= set(evidence) for qid in refs),
                '晚间频次核验与题源不一致')
    for question in questions.values():
        tier = frequency[question['concept_key']]['frequency_rank']
        skipped = [point for point, value in frequency.items() if value['frequency_rank'] < tier
                   and seen[point]['disposition'] == 'deferred']
        require(not skipped, '晚间高频未用尽不得补低层：' + ','.join(sorted(skipped)))
    sources = audit['question_sources']
    require(isinstance(sources, list) and len(sources) == len(questions), '晚间新场景来源核验缺失')
    source_ids = set()
    for row in sources:
        require(isinstance(row, dict) and set(row) == {'question_id', 'kind', 'true_item_id'}, '晚间场景来源字段错误')
        qid = row['question_id']
        require(isinstance(qid, str) and qid in questions and qid not in source_ids, '晚间场景题号无效或重复')
        source_ids.add(qid)
        require(row['kind'] == 'new_mechanism_scenario' and row['true_item_id'] is None,
                '晚间不得重出当天或近14天精确原题，须独立新机制场景')
        require(not re.search(r'EXAM408-\d{4}-Q\d{2}(?:-P\d{2})?', questions[qid]['student_prompt']),
                '晚间新场景不得伪装为精确统考原题')


def prepare(input_path, return_path, repo=REPO):
    repo = Path(repo).resolve()
    manifest, handoff = verified_input(input_path)
    from web_transfer_root_408 import candidate_bases
    wanted = sha(Path(input_path).read_bytes())
    exported = [base / handoff["study_date"] / manifest["handoff_sha256"][:20] / "408_evening_input.zip"
                for base in candidate_bases("evening-input", details_for(repo), "evening-review/inputs")]
    require(any(path.is_file() and sha(path.read_bytes()) == wanted for path in exported),
            "输入未匹配本地已导出的原交接包")
    plan = load_plan(return_path)
    seconds = validate_plan(plan, manifest, handoff)
    validate_live(repo, handoff)
    plan_raw = encoded(plan)
    identity = sha(manifest["handoff_sha256"].encode() + plan_raw)
    base = details_for(repo) / "evening-review/prepared" / handoff["study_date"]
    target = base / identity[:20]
    from evening_visual_408 import DAILY_READING_SCHEMA, SCHEMA, render, render_daily
    reading = plan.get("schema") in {SCHEMA, DAILY_READING_SCHEMA}
    if plan.get("schema") == DAILY_READING_SCHEMA:
        start = f"408 全天交互复盘已准备。学习日期 {handoff['study_date']}。直接打开同目录 review.html 阅读。\n"
        files = {"review.html": render_daily(plan), "plan.json": plan_raw, "START_STUDY.md": start.encode()}
    elif reading:
        start = f"""408极简可视化晚间复盘已准备，尚未开始。
学习日期：{handoff['study_date']}。预计阅读{seconds}秒，约15分钟。
请直接打开本目录review.html供用户阅读；不出题、不等待作答、不要求复述。
所有内容直接可见。用户主动追问时才局部解释。开始阅读后计时，不能仅因打开就记作读完或掌握。
prepare和阅读不产生Capture、评分、FSRS或正式个人摘要。
"""
        files = {"review.html": render(plan), "plan.json": plan_raw, "START_STUDY.md": start.encode()}
    else:
        student = [{k: q[k] for k in ("question_id", "concept_key", "slot", "budget_seconds", "student_prompt")} for q in plan["questions"]]
        teacher = {q["question_id"]: {"mechanism": q["mechanism"], "source_capture_ids": q["source_capture_ids"], **q["teacher"]} for q in plan["questions"]}
        start = f"""408晚间复盘已准备，尚未开始。

学习日期：{handoff['study_date']}。目标15分钟，主计划含收尾{seconds}秒。
请使用 kaoyan-408-evening-review。用户明确开始后才计时；当前模型沿用用户选择。
读取本目录student.jsonl，一次只展示一个core的student_prompt并等待真实作答。
teacher.json仅供当前题答后判断，不能提前泄露后题答案；必要时按实际断点给提示并检查复述。
prepare不记作已学习，不产生Capture、评分、FSRS或正式个人摘要。到时/用户结束即收尾。
本目录plan.json与原输入是备课证据，不能视为用户已经回答。
"""
        if plan.get("review_policy") == FOCUSED_POLICY:
            start = f"""408晚间少量问答已准备，尚未开始。

学习日期：{handoff['study_date']}。主计划含反馈与收尾{seconds}秒；约10–15分钟，重点少可提前结束。
请使用kaoyan-408-evening-review，沿用用户当前选择的模型。用户明确开始后才记录真实开始时间。
读取student.jsonl，一次只展示一个core的完整student_prompt并等待真实作答，不先给纠错总结或答案。
每题应独立给全条件与必要前置结果，不依赖原题记忆或上一题答案；只重算有真实证据的计算断点。
teacher.json仅用于当前题判断，不能向用户提前展示。答清楚就简短反馈并下一题；有歧义才定点追问。
followup_prompt可为空，有内容也非必问；不固定复述、不追加备用题。卡住给最小提示，两次仍无进展或用户要讲解时直接讲清。
追问和讲解计入预算。约15分钟或用户结束即按实际范围收尾，不把未问、未答或听过解释记成独立掌握。
prepare不产生Capture、评分、FSRS或正式个人摘要。保存整场或正式更新按真实证据和用户授权另行处理。
本目录plan.json与原输入是备课证据，不能视为用户已经回答。
"""
        files = {"student.jsonl": b"".join((json.dumps(r, ensure_ascii=False) + "\n").encode() for r in student),
                 "teacher.json": encoded(teacher), "plan.json": plan_raw, "START_STUDY.md": start.encode()}
    receipt = {"schema": "cs408-evening-prepared-v1", "status": "PREPARED_NOT_STARTED",
               "prepared_id": identity, "study_date": handoff["study_date"],
               "source_commit": handoff["source_commit"], "handoff_sha256": manifest["handoff_sha256"],
               "input_path": str(Path(input_path).resolve()), "return_path": str(Path(return_path).resolve()),
               "source_return_sha256": sha(Path(return_path).read_bytes()),
               "started_at": None, "planned_seconds": seconds,
               "files": {k: sha(v) for k, v in files.items()}, **ZERO_WRITES}
    if not target.exists():
        base.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(prefix=".prepare-", dir=base) as temp:
            stage = Path(temp)
            for name, data in files.items():
                (stage / name).write_bytes(data)
            (stage / "receipt.json").write_bytes(encoded(receipt))
            try:
                os.rename(stage, target)
            except OSError:
                if not target.is_dir():
                    raise
    old = read_json(target / "receipt.json")
    require(old["prepared_id"] == identity and old["files"] == receipt["files"], "已有prepared内容冲突")
    require(all(sha((target / name).read_bytes()) == digest for name, digest in old["files"].items()), "已有prepared文件被修改")
    return {"status": "PREPARED_NOT_STARTED", "prepared_dir": str(target),
            "start_study_path": str(target / "START_STUDY.md"), "receipt_path": str(target / "receipt.json"),
            "planned_seconds": seconds, **({"review_path": str(target / "review.html")} if reading else {}), **ZERO_WRITES}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    local = commands.add_parser("local-context", help="读取本地固定版本的材料，准备当天阅读复盘或问答")
    local.add_argument("--date", required=True)
    exporter = commands.add_parser("export", help="准备晚间复盘资料，默认在本地续办")
    exporter.add_argument("--web-handoff", action="store_true", help="仅在明确要求外发时生成网页交接")
    exporter.add_argument("--date", required=True)
    importer = commands.add_parser("prepare")
    importer.add_argument("--input", required=True, type=Path)
    importer.add_argument("--plan", "--return-file", dest="plan", required=True, type=Path,
                          help="当前会话编写的计划 JSON，兼容已有回包")
    args = parser.parse_args()
    try:
        if args.command == "prepare":
            result = prepare(args.input, args.plan)
        else:
            result = export(args.date, local_author=not getattr(args, "web_handoff", False))
    except (ValueError, OSError, KeyError, TypeError, zipfile.BadZipFile) as exc:
        result = {"status": "BLOCKED", "reason": str(exc), **ZERO_WRITES}
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return int(result["status"] == "BLOCKED")


if __name__ == "__main__":
    raise SystemExit(main())
