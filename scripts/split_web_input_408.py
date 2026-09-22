#!/usr/bin/env python3
"""Partition frozen web inputs; no model calls, learning or formal writes."""
from __future__ import annotations

import argparse
import copy
import datetime
import hashlib
import json
import math
from pathlib import Path
import tempfile
import zipfile


def encoded(value):
    return (json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2) + "\n").encode()


def sha(raw):
    return hashlib.sha256(raw).hexdigest()


def require(value, message):
    if not value:
        raise ValueError(message)


def read_archive(path):
    with zipfile.ZipFile(path) as archive:
        names = archive.namelist()
        require(len(names) == len(set(names)), "duplicate_zip_member")
        require(sum(r.file_size for r in archive.infolist()) <= 2 * 1024**3, "input_too_large")
        for name in names:
            require(not name.startswith("/") and ".." not in Path(name).parts and "\\" not in name,
                    "unsafe_zip_path")
        data = {name: archive.read(name) for name in names if not name.endswith("/")}
    manifest = json.loads(data["manifest.json"])
    listed = {}
    for row in manifest["files"]:
        name = row["path"]
        require(name not in listed and name != "manifest.json", "duplicate_manifest_member")
        raw = data[name]
        require(sha(raw) == row["sha256"] and len(raw) == row.get("size", row.get("size_bytes")),
                "input_hash_mismatch:" + name)
        listed[name] = row
    require(set(listed) == set(data) - {"manifest.json"}, "input_inventory_mismatch")
    return data, manifest


def save_archive(path, data, manifest, *, size_key="size", roles=None):
    manifest = copy.deepcopy(manifest)
    manifest["files"] = [{"path": name, "sha256": sha(raw), size_key: len(raw),
                           "role": (roles or {}).get(name, "web_preparation_input_private")}
                          for name, raw in sorted(data.items()) if name != "manifest.json"]
    payload = dict(data, **{"manifest.json": encoded(manifest)})
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(dir=path.parent, suffix=".zip") as temp:
        with zipfile.ZipFile(temp.name, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=1) as archive:
            for name, raw in sorted(payload.items()):
                info = zipfile.ZipInfo(name, (2026, 1, 1, 0, 0, 0))
                info.compress_type = zipfile.ZIP_DEFLATED
                archive.writestr(info, raw)
        raw = Path(temp.name).read_bytes()
    if path.exists():
        require(path.read_bytes() == raw, "existing_output_drift:" + str(path))
    else:
        path.write_bytes(raw)
    return sha(raw)


def rows(raw):
    return [json.loads(line) for line in raw.decode().splitlines() if line.strip()]


def lines(value):
    return b"".join((json.dumps(r, ensure_ascii=False, sort_keys=True) + "\n").encode() for r in value)


def chunks(values, count=None):
    require(len(values) == len(set(values)), "duplicate_input_unit")
    require(values, "empty_input_scope")
    # Around thirty units per conversation; large jobs default to 5–6 packs.
    count = count or (math.ceil(len(values) / 35) if len(values) > 210 else
                      6 if len(values) > 150 else 5 if len(values) > 120 else math.ceil(len(values) / 30))
    require(1 <= count <= len(values), "invalid_part_count")
    q, r = divmod(len(values), count)
    result, start = [], 0
    for i in range(count):
        end = start + q + (i < r)
        result.append(values[start:end])
        start = end
    return result


def strings(value):
    if isinstance(value, str):
        yield value
    elif isinstance(value, dict):
        for child in value.values():
            yield from strings(child)
    elif isinstance(value, list):
        for child in value:
            yield from strings(child)


def morning_part(data, parent, points, partition):
    from export_morning_input_408 import COLLECTIONS, validate_scope
    owned = set(points)
    scope = {c: rows(data[f"scope/{c}.jsonl"]) for c in COLLECTIONS}
    due_groups = [r for r in scope["due_concepts"] if r["concept_id"] in owned]
    due_ids = {fid for r in due_groups for fid in r["formal_node_ids"]}
    scope.update(
        review_concept_ids=points,
        review_concepts=[r for r in scope["review_concepts"] if r["concept_id"] in owned],
        due_concepts=due_groups,
        due_concept_ids=[p for p in scope["due_concept_ids"] if p in owned],
        due_items=[r for r in scope["due_items"] if r["formal_node_id"] in due_ids],
        concept_requests=[r for r in scope["concept_requests"] if r["concept_id"] in owned],
        # These have no canonical point; preserve once as explicitly global context.
        blocked=scope["blocked"] if partition["part_index"] == 1 else [])
    counts = validate_scope(scope)
    out = {name: raw for name, raw in data.items() if name.startswith("contracts/")}
    if "INPUT_LAYOUT.md" in data:
        out["INPUT_LAYOUT.md"] = data["INPUT_LAYOUT.md"]
    if "preparation_gaps.json" in data:
        out["preparation_gaps.json"] = encoded([r for r in json.loads(data["preparation_gaps.json"]) if r["concept_id"] in owned])
    pending = [f"concepts/{p}.json" for p in points]
    while pending:
        name = pending.pop()
        if name in out:
            continue
        out[name] = data[name]
        if name.endswith(".json"):
            for ref in strings(json.loads(data[name])):
                if ref in data and ref.startswith(("materials/", "candidates/", "concepts/")) and ref not in out:
                    pending.append(ref)
    sources = [r for r in json.loads(data["source-map.json"]) if r["package_path"] in out]
    for row in json.loads(data["source-map.json"]):
        if row["published_path"].startswith("support/d408/") or row["published_path"].endswith(("web-self-check-contract.md", "style-guide.md")):
            out[row["package_path"]] = data[row["package_path"]]
            if row not in sources:
                sources.append(row)
    out["source-map.json"] = encoded(sources)
    index = [r for r in json.loads(data["concept-index.json"]) if r["concept_id"] in owned]
    out["concept-index.json"] = encoded(index)
    out["batches.json"] = encoded([{"batch_id": f"batch-{i // 5 + 1:03}", "concept_ids": points[i:i+5],
                                  "concept_files": [f"concepts/{p}.json" for p in points[i:i+5]]}
                                 for i in range(0, len(points), 5)])
    for c, records in scope.items():
        out[f"scope/{c}.jsonl"] = lines(records)
    summary = json.loads(data["scope/summary.json"])
    summary["parent_native_summary"] = summary.pop("native_summary", {})
    summary.update(coverage=counts, partition=partition, scope_kind="assigned_partition")
    out["scope/summary.json"] = encoded(summary)
    out["scope/read-receipts.json"] = encoded({"mode": "derived_from_verified_parent", "parent_zip_sha256": partition["parent_zip_sha256"]})
    eligibility = json.loads(data["runtime-eligibility.json"])
    candidate_ids = {Path(n).stem for n in out if n.startswith("candidates/")}
    eligibility["formal_ids"] = sorted(candidate_ids)
    eligibility["candidates_by_formal_id"] = {k: v for k, v in eligibility["candidates_by_formal_id"].items() if k in candidate_ids}
    anchors = {cid for p in points for profile in json.loads(out[f"concepts/{p}.json"]).get("style_profiles", [])
               for cid in profile.get("canonical_ids", [])}
    if "canonical_anchor_eligibility" in eligibility:
        eligibility["canonical_anchor_eligibility"] = {cid: row for cid, row in eligibility["canonical_anchor_eligibility"].items() if cid in anchors}
    out["runtime-eligibility.json"] = encoded(eligibility)
    for name, raw in data.items():
        if name.startswith("return-template/"):
            out[name] = raw
    due = json.loads(out["return-template/due_concepts.json"])
    due.update(concept_ids=scope["due_concept_ids"], review_concept_ids=points, concept_requests=scope["concept_requests"])
    out["return-template/due_concepts.json"] = encoded(due)
    template = json.loads(out["return-template/manifest.fields.json"])
    template.update(partition=partition, input_version=partition["input_version"],
                    run_id=f'D408-{partition["partition_set_id"]}-{partition["part_id"]}')
    out["return-template/manifest.fields.json"] = encoded(template)
    report = json.loads(out["return-template/web_execution_report.json"])
    report.update(completed_units=[], pending_unit_ids=points, failures=[])
    out["return-template/web_execution_report.json"] = encoded(report)
    out["progress.json"] = encoded({"schema": "cs408-D-web-preparation-progress-v1", "partition": partition,
        "input_version": partition["input_version"], "source_commit": parent["source_commit"],
        "completed_concept_ids": [], "pending_concept_ids": points, "unresolved_concept_ids": [], "completed_files": []})
    contract = data["RETURN_CONTRACT.md"].decode()
    out["RETURN_CONTRACT.md"] = ("# 本分片交付范围\n\n本节限定下文的“完整”只指本分片 assigned_unit_ids，不要求重算父批或处理其他分片。"
        "沿用return-template/manifest.fields.json中的partition；输出文件名见START_WEB_D.md。"
        "本地收齐/补齐后恢复父批完整范围再prepare，单片不直接启动学习。\n\n" + contract).encode()
    out["partition.json"] = encoded(partition)
    helper = Path(__file__).with_name("web_morning_workspace_408.py")
    out["tools/web_morning_workspace_408.py"] = helper.read_bytes()
    coverage = {**counts, "concept_files": len(points), "candidate_formal_count": len(candidate_ids),
                "scope_export_complete": True, "scope_kind": "assigned_partition", "parent_concept_count": partition["parent_unit_count"],
                "web_semantic_preparation_complete": False, "material_reference_count": len(sources)}
    out["coverage_report.json"] = encoded(coverage)
    manifest = copy.deepcopy(parent)
    manifest.update(input_version=partition["input_version"], partition=partition, coverage=coverage,
                    eligibility_sha256=sha(out["runtime-eligibility.json"]))
    return out, manifest


def morning_prompt(parent, part, input_name):
    output_name = input_name.replace("morning_input", "morning_review")
    return f'''请用 GPT-6 Pro 处理附件 {input_name}，完成 {parent["target_date"]} 的408晨间备题分片 {part["part_index"]}/{part["part_count"]}。

本会话只负责 partition.json 的 assigned_unit_ids，共{len(part["assigned_unit_ids"])}个知识点；父批共{part["parent_unit_count"]}点，由{part["part_count"]}个独立会话并行处理。不要处理其他分片，不重新查询全量due，不把本片标成父批已完成。固定source_commit={parent["source_commit"]}；分片集={part["partition_set_id"]}。

先解压并确认真实输入根目录：manifest.json、concept-index.json、scope/summary.json和RETURN_CONTRACT.md必须同时存在。batches.json是列表；不要猜另一种结构。所有package_path相对于这个输入根目录。输出另建独立目录，输入目录与输出目录不得混用。可以使用随包tools/web_morning_workspace_408.py的init/validate/package命令，必须显式指定--input-root和--output-root；恢复时先读输出目录已有文件，禁止用空模板覆盖。

先读contracts/D_PROJECT_INSTRUCTIONS.txt（旧包缺失则沿既有合同）、INPUT_LAYOUT.md与preparation_gaps.json（旧输入没有则按现有字段），个人字段为personal_context/full_personal_sources，不猜personal_history；候选只取candidate_files，图片取真实package_path，不猜拼图名称。用python -I运行随包helper的read-unit避免临时inspect.py等遮蔽标准库；write-text支持字符串/段落数组，避免列表与字符串拼接以及大段Python嵌套字典。

按concept-index逐点读取本包真题候选、原图、解析和个人历史。只用核验过的EXAM408统考试题，不自编、不改数、不改条件、不用既有变式。精确同一题面近7天已使用则排除，继续查同点其他合格真题；没有就记unresolved，不用变式补位。同点合格真题优先上次实际做题或展示日期最早者，全部采用题按该日期从早到晚排列，同一true_item_id不得重复。日期依据runtime-eligibility.json的last_true_exam_contacts和候选last_learning_contact；未知日期排在已知日期之后并明确未知，不用真题年份、导入或入库时间替代。assigned_unit_ids只是分片责任集合，不覆盖上述题目排序；保留日期证据，跨片合并后再次按题面接触日期全局排序。

先实际调用本会话Study Library的library核对固定版本，再实际读取拟采用题的question_refs和相应personal_context，证明工具可用。工具没有加载与Session terminated分开报告；提示词不能凭空启用连接器。publication_target=local的快照号不交GitHub读取。其余资料优先使用包内同版内容，MCP按必要缺项有界补查。旧版查询使用allow_historical=true并保留freshness，不能宣称是最新版本。真实错误只重试一次，保全其他成果；不编造调用成功。

使用return-template与RETURN_CONTRACT.md。每完成一个知识点立即保存题面、私有解析、自检、历史、结构化items及进度；每5点导出一次带序号的可下载阶段ZIP。阶段包只包含实际成功文件与精确剩余集合，允许PARTIAL。运行时单点失败记录后继续其他独立点；只做一次有依据的恢复，不反复空重试。最终报告从实际输出目录文件盘点生成，不能因末尾报错清空之前成功。

最终交付 {output_name}，包括实际题目成稿、progress.json、EXECUTION_REPORT.md和web_execution_report.json。报告必须列成功、失败、未执行；失败提供真实工具名、参数、原始返回和受影响知识点，真实call_id不可得写null。保留partition字段及本片完整目标。原图可用asset_refs引用，网页新生成图表实际入包。没有成功文件时如实交故障报告，禁止宣称已完成若干题或把目标改成0。

仅完成本片全部必要语义才标WEB_PREPARED；缺项标PARTIAL并交已有成果。不要只给报告而漏交已存在的题目文件。交包后停止，不开始教学。所有分片回本地合并，失败部分由本地按真实证据补全，再生成DeepSeek V4.1 Flash启动提示词。
'''


def split_morning(zip_path, *, output_dir=None, parts=None):
    from export_morning_input_408 import verify_bundle
    source = Path(zip_path).expanduser().resolve()
    proof = verify_bundle(source)
    data, parent = read_archive(source)
    require(not parent.get("partition"), "cannot_split_a_partition")
    points = rows(data["scope/review_concept_ids.jsonl"])
    groups = chunks(points, parts)
    identity = sha(encoded({"parent": proof["zip_sha256"], "groups": groups,
                           "splitter": sha(Path(__file__).read_bytes()),
                           "workspace_helper": sha(Path(__file__).with_name("web_morning_workspace_408.py").read_bytes())}))[:20]
    base = Path(output_dir).expanduser().resolve() if output_dir else source.parent / ("parallel-" + identity)
    require(not any(p.is_symlink() for p in [base, *base.parents]), "unsafe_output")
    base.mkdir(parents=True, exist_ok=True)
    batch = {"schema": "cs408-web-partition-set-v1", "kind": "morning", "partition_set_id": identity,
             "source_commit": parent["source_commit"], "target_date": parent["target_date"],
             "parent_zip_path": str(source), "parent_zip_sha256": proof["zip_sha256"],
             "parent_input_version": parent["input_version"], "parent_unit_ids": points, "parts": []}
    for i, group in enumerate(groups, 1):
        part = {"schema": "cs408-web-partition-v1", "kind": "morning", "partition_set_id": identity,
                "parent_zip_sha256": proof["zip_sha256"], "parent_input_version": parent["input_version"],
                "parent_unit_count": len(points), "part_index": i, "part_count": len(groups),
                "part_id": f"part-{i:02}-of-{len(groups):02}", "assigned_unit_ids": group}
        part["input_version"] = sha(encoded(part))
        out, manifest = morning_part(data, parent, group, part)
        name = f'408_morning_input_{parent["target_date"]}_{part["part_id"]}.zip'
        prompt = morning_prompt(parent, part, name)
        out["START_WEB_D.md"] = prompt.encode()
        out["START_HERE.md"] = ("本包是独立备题分片。先读START_WEB_D.md、partition.json、RETURN_CONTRACT.md。\n"
            "只处理assigned_unit_ids；父批原件由本地保留。输入、输出目录分开；每点保存实际成果，每5点交阶段包。\n").encode()
        target = base / name
        digest = save_archive(target, out, manifest)
        verified = verify_bundle(target)
        require(verified["concept_count"] == len(group), "part_scope_mismatch")
        prompt_path = base / f'START_WEB_D_{parent["target_date"]}_{part["part_id"]}.md'
        prompt_path.write_text(prompt)
        batch["parts"].append({**part, "zip_path": str(target), "zip_sha256": digest,
                               "zip_bytes": target.stat().st_size, "prompt_path": str(prompt_path),
                               "prompt_sha256": sha(prompt.encode()), "verification": verified})
    require([p for r in batch["parts"] for p in r["assigned_unit_ids"]] == points, "partition_union_mismatch")
    index_path = base / "partition-set.json"
    index_path.write_bytes(encoded(batch))
    listing = [f'408晨间备题：{parent["target_date"]}，{len(points)}点，{len(groups)}个独立会话。',
               "每个会话只上传一份ZIP并粘贴对应启动词；不要把六包都上传同一会话。",
               "可以同时运行；遇到服务限流就减少同时运行的数量，保留原片身份继续。",
               "全部回包或阶段包一起交本地；本地保全并核对父批完整范围，再合并与prepare。", ""]
    listing += [f'{r["part_id"]}：{len(r["assigned_unit_ids"])}点；{Path(r["zip_path"]).name}；{Path(r["prompt_path"]).name}' for r in batch["parts"]]
    readme = base / "使用说明.md"
    readme.write_text("\n\n".join(listing) + "\n")
    return {"partition_set_path": str(index_path), "partition_set_id": identity,
            "part_count": len(groups), "parts": batch["parts"], "usage_path": str(readme),
            "formal_write_count": 0, "learner_evidence_write_count": 0}


def attach_partitions(receipt):
    if receipt.get("concept_count", 0) <= 40:
        return receipt
    return dict(receipt, **split_morning(receipt["zip_path"]))


def split_captures(zip_path, *, output_dir=None, parts=None, source_commit=None):
    source = Path(zip_path).expanduser().resolve()
    data, parent = read_archive(source)
    require(parent.get("schema") == "cs408-raw-capture-day-v1" and parent.get("subject") == "408"
            and isinstance(parent.get("captures"), list), "capture_input_required")
    require(not parent.get("partition"), "cannot_split_a_partition")
    ids = [r["capture_id"] for r in parent["captures"]]
    target_count = len(chunks(ids, parts))
    by_date = {}
    for capture_id in ids:
        path = f"captures/{capture_id}/capture.json"
        require(path in data, "capture_payload_missing:" + capture_id)
        payload = json.loads(data[path])
        day = payload.get("study_date")
        try:
            valid_day = isinstance(day, str) and datetime.date.fromisoformat(day).isoformat() == day
        except ValueError:
            valid_day = False
        require(valid_day, "capture_study_date_required:" + capture_id)
        by_date.setdefault(day, []).append(capture_id)
    # A target count never authorizes mixing dates. Allocate at least one
    # conversation per date, then split the largest remaining average load.
    allocation = {day: 1 for day in sorted(by_date)}
    target_count = max(target_count, len(allocation))
    while sum(allocation.values()) < target_count:
        day = max((d for d in allocation if allocation[d] < len(by_date[d])),
                  key=lambda d: len(by_date[d]) / allocation[d])
        allocation[day] += 1
    dated_groups = [(day, group) for day, count in allocation.items()
                    for group in chunks(by_date[day], count)]
    groups = [group for _, group in dated_groups]
    if source_commit is None:
        from export_morning_input_408 import LibraryStore, BRIDGE_ROOT
        store = LibraryStore(BRIDGE_ROOT)
        try:
            snapshot = store.snapshot("408", "")
            require(store.freshness(snapshot).get("status") == "current", "capture_snapshot_not_current")
            source_commit = snapshot.commit
        finally:
            store.close()
    require(isinstance(source_commit, str) and len(source_commit) == 40
            and all(c in "0123456789abcdef" for c in source_commit), "source_commit_required")
    parent_sha = sha(source.read_bytes())
    identity = sha(encoded({"parent": parent_sha, "source_commit": source_commit, "groups": groups,
                           "splitter": sha(Path(__file__).read_bytes())}))[:20]
    base = Path(output_dir).expanduser().resolve() if output_dir else source.parent / ("parallel-" + identity)
    base.mkdir(parents=True, exist_ok=True)
    result = {"schema": "cs408-web-partition-set-v1", "kind": "enhancement", "partition_set_id": identity,
              "source_commit": source_commit,
              "study_date": next(iter(by_date)) if len(by_date) == 1 else None,
              "study_dates": sorted(by_date), "parent_unit_ids": ids,
              "parent_zip_path": str(source), "parent_zip_sha256": parent_sha, "parts": []}
    for i, (study_date, group) in enumerate(dated_groups, 1):
        owned = set(group)
        part = {"schema": "cs408-web-partition-v1", "kind": "enhancement", "partition_set_id": identity,
                "part_id": f"part-{i:02}-of-{len(groups):02}", "part_index": i, "part_count": len(groups),
                "parent_zip_sha256": parent_sha, "parent_unit_count": len(ids), "assigned_unit_ids": group,
                "source_commit": source_commit, "study_date": study_date}
        selected = [r for r in parent["captures"] if r["capture_id"] in owned]
        out = {n: raw for n, raw in data.items() if n.startswith("captures/") and n.split("/")[1] in owned}
        out.update({n: raw for n, raw in data.items()
                    if n.startswith(("contracts/", "return-template/")) or n == "WEB_AUTHORING_GUIDE.md"})
        for name, raw in list(out.items()):
            if name.startswith("return-template/") and name.endswith(".json"):
                template = json.loads(raw)
                if isinstance(template, dict) and "study_date" in template:
                    template["study_date"] = study_date
                    out[name] = encoded(template)
        if "return-template/enhancement.json" in out:
            template = json.loads(out["return-template/enhancement.json"])
            template.update(run_id=f'WEB-408-{identity}-{part["part_id"]}', source_commit=source_commit, items=[])
            out["return-template/enhancement.json"] = encoded(template)
        index = json.loads(data["capture_index.json"])
        for key in ("captures", "packages", "legacy_identity_notes"):
            index[key] = [r for r in index.get(key, []) if r["capture_id"] in owned]
        if "study_date" in index:
            index["study_date"] = study_date
        out["capture_index.json"] = encoded(index)
        out["capture_events.jsonl"] = b"".join(line for line in data["capture_events.jsonl"].splitlines(keepends=True)
            if line.strip() and json.loads(line).get("capture_id") in owned)
        out["partition.json"] = encoded(part)
        prompt = f'''请用GPT-6 Pro完成{study_date}的408正式入库加强分片{i}/{len(groups)}，只处理partition.json列出的{len(group)}个Capture。
本片所有Capture的真实study_date均为{study_date}；正式回包manifest.study_date必须使用该日。父批可跨日期，本片不得混日。
父批共{len(ids)}个Capture。每个Capture的完整对话、附件及补充版本都在本片，不能拆开或省略；Capture不代表错误。
固定source_commit={source_commit}，所有并行会话用同一版本；MCP只读补查该版本正式历史，禁止切换current重新定基线。
先读WEB_AUTHORING_GUIDE.md和contracts（如随包提供）；return-template为空模板，必须逐项补全。网页先完成来源/证据绑定、合法附件可见性和本片同点语义合成，不因这些已给字段退回needs_user；本机回执与正式事务才留到本地。
沿用START_HERE中的正式增强与个人成稿合同，逐Capture完成真实决定、点级观察和证据绑定。重叠知识点的摘要只表示本片证据下的候选成稿，本地收齐后统一合成每点唯一最终版，不宣称覆盖其他分片。
每完成一个Capture立即保存实际成稿，每5个Capture交一次可下载阶段ZIP。单项失败记录真实工具名、参数、原始返回、受影响Capture及恢复结果，继续其他独立项。最终必须交实际成功文件、EXECUTION_REPORT.md、web_execution_report.json和partition.json；真实call_id不可得写null，尾部错误不清零已完成内容。
正式回包manifest只使用现有正式schema允许的字段，分片身份放partition.json并列入files，不擅加manifest顶层字段。本片只读不正式写入；不要把父批全部Capture放进本片的manifest.captures。
输出408_enhancement_{study_date}_{part["part_id"]}.zip。回本地先保全所有片、核对完整Capture并集、补齐失败项和合并重叠知识点摘要，再按原日期逐日生成现有schema的统一增强包，串行正式写入、发布一次、生成下一轮晨间分片。不能并发apply各片。
'''
        out["START_HERE.md"] = (prompt + "\n\n以下保留父批业务合同；其中日期、范围和文件名以本分片manifest及上文为准。\n\n").encode() + data["START_HERE.md"]
        manifest = copy.deepcopy(parent)
        manifest.update(study_date=study_date, captures=selected, capture_count=len(group), partition=part,
            source_commit=source_commit, input_set_sha256=sha(encoded(part)),
            capture_set_sha256=sha(json.dumps(group, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()))
        name = f'408_learning_{study_date}_{part["part_id"]}.zip'
        target = base / name
        digest = save_archive(target, out, manifest, size_key="size_bytes")
        read_archive(target)
        prompt_path = base / (Path(name).stem + "_START_WEB.md")
        prompt_path.write_text(prompt)
        result["parts"].append({**part, "zip_path": str(target), "zip_sha256": digest, "prompt_path": str(prompt_path)})
    assigned = [cid for part in result["parts"] for cid in part["assigned_unit_ids"]]
    require(len(assigned) == len(ids) and set(assigned) == set(ids), "partition_union_mismatch")
    index_path = base / "partition-set.json"
    index_path.write_bytes(encoded(result))
    return {"partition_set_path": str(index_path), "part_count": len(groups), "parts": result["parts"],
            "formal_write_count": 0, "learning_write_count": 0}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--zip", required=True)
    parser.add_argument("--output-dir", type=Path)
    parser.add_argument("--parts", type=int)
    parser.add_argument("--kind", choices=("morning", "enhancement"), default="morning")
    parser.add_argument("--source-commit")
    args = parser.parse_args()
    kwargs = {"output_dir": args.output_dir, "parts": args.parts}
    function = split_morning
    if args.kind == "enhancement":
        function = split_captures
        kwargs["source_commit"] = args.source_commit
    print(json.dumps(function(args.zip, **kwargs), ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
