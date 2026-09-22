"""A user-authorized question limit over frozen native D candidates.

The full native scope remains a scheduling reference. The web selects only the
requested number of questions; importing that return creates a local subset
decision without completing or deleting anything outside the selection.
"""
from __future__ import annotations

import copy
import datetime as dt
import hashlib
import json
from pathlib import Path
import re
import zipfile

SCHEMA = "cs408-morning-selection-input-v1"
CONTRACT_SCHEMA = "cs408-morning-selection-contract-v1"
POLICY_REL = Path(".private/study-bridge/morning-selection-policy.json")
REGISTRY_REL = Path(".private/study-bridge/morning-selection-inputs")


def encoded(value):
    return (json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2) + "\n").encode()


def sha(raw):
    return hashlib.sha256(raw).hexdigest()


def require(ok, message):
    if not ok:
        raise ValueError(message)


def read(path):
    return json.loads(Path(path).read_bytes())


def policy_for(repo):
    path = Path(repo) / POLICY_REL
    if not path.is_file():
        return None
    policy = read(path)
    require(policy.get("authorization_kind") == "explicit_user_question_limit"
            and policy.get("user_request") and policy.get("user_message_id"),
            "morning_selection_user_authorization_required")
    limit = policy.get("question_limit")
    require(isinstance(limit, int) and not isinstance(limit, bool) and 1 <= limit <= 100,
            "morning_selection_limit_invalid")
    return policy


def recent_evidence(profile, target_date, lookback_days=14):
    day = dt.date.fromisoformat(target_date)
    rows = []
    for row in profile.get("timeline", []):
        try:
            observed = dt.date.fromisoformat(str(row.get("study_date") or row.get("date")))
        except (ValueError, TypeError):
            continue
        if not 0 <= (day - observed).days <= lookback_days:
            continue
        score = 1
        if row.get("outcome") in {"wrong", "partial", "uncertain", "relearn_required", "incorrect"}:
            score = 3
        elif row.get("prompt_dependency") in {"hint_or_prompted", "minimal", "progressive", "full"}:
            score = 2
        rows.append({"study_date": observed.isoformat(), "summary": row.get("summary"),
                     "outcome": row.get("outcome"), "prompt_dependency": row.get("prompt_dependency"),
                     "source_refs": row.get("source_refs", []), "priority_evidence_score": score})
    rows.sort(key=lambda row: row["study_date"], reverse=True)
    return {"lookback_days": lookback_days, "priority_evidence_score": max((r["priority_evidence_score"] for r in rows), default=0),
            "recent_records": rows[:8], "recent_record_count": len(rows),
            "short_summary": profile.get("short_summary", ""), "next_check": profile.get("next_check", ""),
            "scope": "Recent observed difficulty ranks ties after actual contact date; this is not a mastery grade."}


def candidate_order_key(row):
    date = (row.get("last_learning_contact") or {}).get("study_date")
    return (date is None, date or "9999-12-31", -row.get("recent_priority_score", 0),
            row["true_item_id"], row["concept_id"])


def selection_capacity(rows, limit):
    """Maximum distinct-question/distinct-point count, without selecting for study."""
    adjacency = {}
    for row in rows:
        adjacency.setdefault(row["concept_id"], set()).add(row["true_item_id"])
    matched = {}

    def assign(point, seen):
        for identity in sorted(adjacency[point]):
            if identity in seen:
                continue
            seen.add(identity)
            if identity not in matched or assign(matched[identity], seen):
                matched[identity] = point
                return True
        return False

    for point in sorted(adjacency):
        assign(point, set())
    return min(limit, len(matched))


PROMPT = '''请用 GPT-6 Pro 从附件的合格真题候选池中，选择并准备 {target_count} 道用于 {day} 的408晨间复盘题。本轮上限 {limit} 道，只对最终选中的题做完整备题，不为全部知识点逐个生成题。

先读 selection-contract.json、candidate-pool.json、recent-learning.json 和 RETURN_CONTRACT.md。固定 source_commit={commit}，输入版本={version}。scope/ 和 return-template/due_concepts.json 的完整原生集合用于来源与调度对账，不是本轮全量备题任务，不能改掉其中原始身份。

选题优先级依次为：
1. 硬排除：仅已核验408统考真题；精确题面距目标日期不足7天的真实作答或展示一律排除，今天结束未作答但已展示的题也排除。同一真题只选一次；不自编、改题或以变式凑数。
2. 最高排序优先级：last_learning_contact.study_date 越早越优先；跨整个候选池比较，不先按知识点编号或题目年份取前30个。未知日期放在已知日期之后，不能把未知说成更久未做。
3. 次优先级：在上述日期优先顺序内，依据近期真实断点、错误、提示依赖与未独立验证边界选最合适的题。recent-learning.json 提供有来源的查阅入口，继续读取拟选点的完整概况和 subpoints，不把命中点自动当作薄弱点。不得通过较新的“更弱”题突破第一优先级。

candidate-pool.json 是已经通过本地精确题面门禁的候选清单，不是已选题单。请先作这次30题规模的全局选择，再只为所选题读取原图/必要解析和完整个人摘要，求解核对并写出私有解题、history、teaching。原选项与题面必须保真。若核验发现来源不符，从同一候选池按同样优先级补选；确实不足则保全已完成内容并报告真实缺项，禁止放宽7天或使用变式。

每题 rationale 说明上次实际接触日期及来源、为何适合当前哪个具体小点、提示前保护什么。当前最需要修复的小点用规范父代码和稳定KSP/keywords引用；提示后修复不能当成独立掌握。旧资料与本次正式更新的差异按真实条件保留。

一个知识点至多一题；总题数以 selected_items、supplemental_items、substitute_items 的唯一真实题面并集计算。主/副点说明不增加题数。本地不在网页阶段正式写入、评分或开始学习。每五道已选题保全一次阶段包，失败保留原始工具名、参数和错误，继续可独立完成的题。

按 RETURN_CONTRACT.md 交一个408_morning_review.zip，保留 selection-contract.json 原字节并列入manifest.files。完整原生due_concepts元数据照抄模板；coverage_report只声明本次实际选择并准备好的目标，deferred_concepts列明因本轮容量未选择的原生点，不写成知识未掌握或备题失败。回包由用户交给本地进行限额/身份/7天复查和预处理；网页不要执行本地命令或宣称已开始学习。
'''

RETURN_CONTRACT = '''408 限额选题回包合同

这是一次明确限额的选题。完整原生范围保存在scope/和模板中，本次只采用selection-contract.json要求的数量；未选择的范围保持未复习，不要求为其写题、解析或unresolved。此规则替代全量备题逐知识点覆盖要求。

回包使用现有D结构：manifest.json、START_HERE.md、due_concepts.json、selected_items.jsonl、supplemental_items.jsonl、substitute_items.jsonl、web_variants.jsonl（空）、asset_refs.jsonl、unresolved.jsonl、coverage_report.json、rationale.md、selection-contract.json、EXECUTION_REPORT.md、web_execution_report.json，以及实际选题的题目/私有解题/历史/teaching文件。

manifest采用schema_version=1、project=D、subject="408"、publication_target=local、asset_delivery=snapshot_refs_v1、ordering_policy=oldest_verified_actual_contact_first_unknown_last。source_commit/snapshot_id、target_date、due_version、concept_priority_version和input_version沿模板，run_id为本次稳定ASCII身份。排序字段必须保留，使原题与补充题合成一条按实际接触日期排序的队列。status=WEB_PREPARED只表示本次所选题语义备题完成；真实缺失所选题仍为PARTIAL。files覆盖除manifest自身以外所有实际文件，字段path、role、sha256、size。web_execution_report字段指向该文件，role也为web_execution_report。

due_concepts.json原样复制return-template中的完整集合：concept_ids、concept_requests、review_concept_ids、concept_priority_version均不裁剪。这个文件只为本地核对原生版本；本次备题目标以实际题目集合和selection-contract为准。本地从登记过的原始选题输入产生可信的subset decision，并保留所有未选择点，不接受网页自签本地批准。

每个选中项沿用原生D题目字段：question_id必须取candidate-pool中该知识点/真题组合的formal_ids，即本地登记候选锚点；review_target_concept_ids只有一个规范父点；真实preferred_true_item_id/true_item_id和surface_sha256来自同一候选行。实际原题另有正式定位ID时可保存在canonical_source_formal_id，不能用它替换登记锚点，也不能从锚点猜真题年份。source_refs必须包含登记锚点的同版安全卡来源，并保留实际原题的来源。problem_path、image_paths及asset_refs.question_id按登记锚点保持一致，公开图的package_path使用questions/<登记question_id>/assets/；source_path、真实题号和源图SHA仍指向实际采用的真题。

当前到期组里的精确原题用selected_items。同点其他合格原题用substitute_items，execution_kind=due_representative_substitute、substitution_reason说明为何改用同点原题，suppressed_same_concept_question_ids包含该点全部原到期ID，不提供伪造request_id。仅点级请求的题使用supplemental_items内真实request_id，不改其来源身份或把关联原题当作已经复做。

同一真实题面全场只选一次，同一主目标点至多一题。覆盖报告的covered_concept_ids是本次实际选中点；deferred_concepts列完整原生集合减去采用点，reason写“本轮限额未选择”，不混进unresolved_concept_ids。unresolved只用于本次需要的题仍真实未完成；不能写空数组掩盖选中的缺题。rationale逐题给实际接触日期证据和近期具体断点的匹配理由，最终展示顺序按接触日期早到晚。

已有原图按照contracts/asset-delivery-contract.md交asset_refs引用；以候选中的canonical_exam_materials为准，不能将同知识点另一正式卡的图当作实际真题。package_path是本地包内路径，published_path才是MCP路径。新产生的实际图表需要随包；私有解析与提示不进入未作答题面。

必须保留selection-contract.json原字节，不改限额、版本和日期。单项失败保全成功内容和真实执行报告；failures.input必须是对象，脚本工具的完整原始代码放在input.code中，不把input写成裸字符串。工具未加载、Session terminated、错误路径和成功空返回分别记录。全量native-return-contract.md只作为题级字段参考，其中全范围覆盖表述被本文件的明确限额替代。
'''


def export_selection(full_receipt, repo, policy):
    repo = Path(repo).resolve()
    root = Path(full_receipt["content_root"])
    original_manifest = read(root / "manifest.json")
    eligibility = read(root / "runtime-eligibility.json")
    all_scope = read(root / "return-template/due_concepts.json")
    index = read(root / "concept-index.json")
    concepts = {row["concept_id"]: read(root / row["path"]) for row in index}
    recent = {}
    for point, value in concepts.items():
        summary = value.get("personal_context", {}).get("summary", {})
        profile = summary.get("learning_profile") or {}
        for ref in value.get("full_personal_sources", []):
            if "reviewed-knowledge-profiles/" in ref.get("published_path", "") and ref.get("status") == "included":
                obj = read(root / ref["package_path"])
                profile = obj.get("profile", obj)
                break
        recent[point] = recent_evidence(profile, full_receipt["target_date"], policy.get("recent_learning_days", 14))
    rows = {}
    for point, concept in concepts.items():
        for candidate_file in concept.get("candidate_files", []):
            candidate = read(root / candidate_file)
            for surface in candidate.get("eligibility", []):
                identity = surface.get("true_item_id", "")
                if not surface.get("eligible") or surface.get("main_knowledge") != point or not re.fullmatch(r"EXAM408-\d{4}-Q\d{2}", identity):
                    continue
                contact = surface.get("last_learning_contact")
                if contact and contact.get("study_date"):
                    gap = (dt.date.fromisoformat(full_receipt["target_date"]) - dt.date.fromisoformat(contact["study_date"])).days
                    if gap < 7:
                        continue
                key = (point, identity, surface.get("surface_sha256"))
                row = rows.setdefault(key, {"concept_id": point, "true_item_id": identity,
                    "surface_sha256": surface.get("surface_sha256"), "last_learning_contact": contact,
                    "formal_ids": [], "candidate_files": [], "recent_priority_score": recent[point]["priority_evidence_score"]})
                fid = Path(candidate_file).stem
                if fid not in row["formal_ids"]:
                    row["formal_ids"].append(fid)
                    row["candidate_files"].append(candidate_file)
    pool = sorted(rows.values(), key=candidate_order_key)
    for rank, row in enumerate(pool, 1):
        row["rank"] = rank
    target_count = selection_capacity(pool, policy["question_limit"])
    pool_bytes = encoded({"status": "eligible_candidates_not_selected", "candidate_pair_count": len(pool),
                          "unique_true_exam_count": len({r['true_item_id'] for r in pool}), "candidates": pool})
    version = sha(encoded({"full_input_version": full_receipt["input_version"], "policy": policy,
                          "helper_sha256": sha(Path(__file__).read_bytes()), "candidate_pool_sha256": sha(pool_bytes)}))
    directory = Path(full_receipt["zip_path"]).parent / ("selection-" + version[:16])
    directory.mkdir(exist_ok=True)
    contract = {"schema": CONTRACT_SCHEMA, "input_version": version,
                "source_commit": full_receipt["source_commit"], "target_date": full_receipt["target_date"],
                "due_version": original_manifest["due_version"],
                "concept_priority_version": original_manifest["concept_priority_version"],
                "question_limit": policy["question_limit"], "target_question_count": target_count,
                "candidate_pool_sha256": sha(pool_bytes), "full_review_concept_ids": all_scope["review_concept_ids"],
                "priority_order": ["verified_true_exam_and_exact_7_day_gate", "oldest_actual_contact_known_first", "recent_learning_breaks"],
                "not_selected_means": "deferred_unanswered_not_completed"}
    prompt = PROMPT.format(target_count=target_count, limit=policy["question_limit"], day=contract["target_date"],
                           commit=contract["source_commit"], version=version)
    selected_points = {r['concept_id'] for r in pool}
    paths = set()

    def reachable(value):
        if isinstance(value, dict):
            if value.get("package_path") and (root / value["package_path"]).is_file():
                paths.add(value["package_path"])
            for child in value.values():
                reachable(child)
        elif isinstance(value, list):
            for child in value:
                reachable(child)

    for point in selected_points:
        paths.add("concepts/" + point + ".json")
        reachable(concepts[point])
    for row in pool:
        for candidate_file in row["candidate_files"]:
            paths.add(candidate_file)
            reachable(read(root / candidate_file))
    for p in (root / "scope").rglob("*"):
        if p.is_file():
            paths.add(p.relative_to(root).as_posix())
    for p in (root / "return-template").rglob("*"):
        if p.is_file():
            paths.add(p.relative_to(root).as_posix())
    for p in (root / "contracts").rglob("*"):
        if p.is_file() and p.name != "D_PROJECT_INSTRUCTIONS.txt":
            paths.add(p.relative_to(root).as_posix())
    paths.add("runtime-eligibility.json")
    data = {path: (root / path).read_bytes() for path in paths}
    source_map = [row for row in read(root / "source-map.json") if row["package_path"] in paths]
    data.update({"candidate-pool.json": pool_bytes, "selection-contract.json": encoded(contract),
                 "recent-learning.json": encoded({p: recent[p] for p in sorted(selected_points)}),
                 "concept-index.json": encoded([r for r in index if r["concept_id"] in selected_points]),
                 "source-map.json": encoded(source_map), "START_WEB_D.md": prompt.encode(),
                 "START_HERE.md": prompt.encode(), "RETURN_CONTRACT.md": RETURN_CONTRACT.encode(),
                 "contracts/native-return-contract.md": (root / "RETURN_CONTRACT.md").read_bytes(),
                 "contracts/D_PROJECT_INSTRUCTIONS.txt": prompt.encode(),
                 "return-template/selection-contract.json": encoded(contract)})
    template = read(root / "return-template/manifest.fields.json")
    template["input_version"] = version
    template["ordering_policy"] = "oldest_verified_actual_contact_first_unknown_last"
    data["return-template/manifest.fields.json"] = encoded(template)
    data["return-template/web_execution_report.json"] = encoded({
        "schema": "cs408-web-execution-report-v1", "source_commit": contract["source_commit"],
        "completed_units": [], "pending_unit_ids": [], "failures": []})
    data["return-template/README.md"] = (
        "这些是未完成的空模板。先确定本次采用题目，再把实际已完成/未完成的采用题写进执行报告。"
        "due_concepts的完整元数据必须保留，但不要将容量外所有知识点填为pending或unresolved。\n"
    ).encode()
    manifest = {"schema": SCHEMA, "artifact_kind": "morning_selection_input", "subject": "408", "project": "D",
                "source_commit": contract["source_commit"], "snapshot_id": contract["source_commit"],
                "target_date": contract["target_date"], "input_version": version, "question_limit": policy["question_limit"],
                "target_question_count": target_count, "candidate_pair_count": len(pool),
                "selected_representatives": False, "formal_write_count": 0, "learner_evidence_write_count": 0,
                "files": [{"path": path, "size": len(raw), "sha256": sha(raw), "role": "web_selection_input_private"}
                          for path, raw in sorted(data.items())]}
    archive = directory / f"408_morning_selection_{contract['target_date']}_{policy['question_limit']}.zip"
    with zipfile.ZipFile(archive, 'w', compression=zipfile.ZIP_DEFLATED, compresslevel=1) as z:
        for path, raw in sorted({**data, 'manifest.json': encoded(manifest)}.items()):
            info = zipfile.ZipInfo(path, (2026, 1, 1, 0, 0, 0))
            info.compress_type = zipfile.ZIP_DEFLATED
            z.writestr(info, raw)
    with zipfile.ZipFile(archive) as z:
        require(set(z.namelist()) == set(data) | {'manifest.json'}, 'morning_selection_inventory_mismatch')
        for row in manifest['files']:
            require(sha(z.read(row['path'])) == row['sha256'], 'morning_selection_hash_mismatch')
    prompt_path = directory / f"START_WEB_D_{contract['target_date']}_{policy['question_limit']}.md"
    prompt_path.write_text(prompt)
    registration = {'contract': contract, 'contract_sha256': sha(encoded(contract)), 'policy': policy,
                    'allowed_candidates': pool, 'input_zip': str(archive), 'input_zip_sha256': sha(archive.read_bytes())}
    register_path = repo / REGISTRY_REL / (version + '.json')
    register_path.parent.mkdir(parents=True, exist_ok=True)
    if register_path.exists():
        old = read(register_path)
        require(old['contract'] == contract and old['policy'] == policy, 'morning_selection_registration_conflict')
    register_path.write_bytes(encoded(registration))
    result = {'schema': SCHEMA, 'status': 'READY_FOR_WEB_PREPARATION', 'source_commit': contract['source_commit'],
              'target_date': contract['target_date'], 'input_version': version,
              'zip_path': str(archive), 'zip_sha256': registration['input_zip_sha256'], 'zip_bytes': archive.stat().st_size,
              'prompt_path': str(prompt_path), 'prompt_sha256': sha(prompt_path.read_bytes()),
              'question_limit': policy['question_limit'], 'target_question_count': target_count,
              'concept_count': len(selected_points), 'candidate_pair_count': len(pool),
              'unique_true_exam_count': len({r['true_item_id'] for r in pool}), 'part_count': 1,
              'source_full_input_version': full_receipt['input_version'], 'registration_path': str(register_path),
              'exporter_sha256': full_receipt['exporter_sha256'], 'formal_write_count': 0,
              'learner_evidence_write_count': 0, 'model_call_count': 0, 'learning_started': False}
    (directory / 'receipt.json').write_bytes(encoded(result))
    return result


def scope_for_return(repo, contract, body, source_zip_sha256):
    repo = Path(repo).resolve()
    version = str(contract.get('input_version', ''))
    require(re.fullmatch('[0-9a-f]{64}', version), 'D_selection_input_version_invalid')
    registration = read(repo / REGISTRY_REL / (version + '.json'))
    require(registration['contract'] == contract, 'D_selection_contract_drift')
    for field in ('source_commit', 'target_date', 'due_version', 'concept_priority_version'):
        require(body.get(field) == contract.get(field), 'D_selection_binding_drift:' + field)
    require(body['review_concept_ids'] == contract['full_review_concept_ids'], 'D_selection_full_scope_drift')
    all_items = [item for name in ('selected_items', 'supplemental_items', 'substitute_items') for item in body.get(name, [])]
    require(len(all_items) == contract['target_question_count'] and len(all_items) <= contract['question_limit'],
            'D_selection_question_count_mismatch')
    allowed = {(r['concept_id'], r['true_item_id'], fid, r.get('surface_sha256'))
               for r in registration['allowed_candidates'] for fid in r['formal_ids']}
    points, identities = [], []
    for item in all_items:
        targets = item.get('review_target_concept_ids', [])
        require(len(targets) == 1, 'D_selection_one_point_per_question_required')
        identity = item.get('preferred_true_item_id') or item.get('true_item_id')
        require((targets[0], identity, item['question_id'], item.get('surface_sha256')) in allowed,
                'D_selection_candidate_not_in_registered_input')
        points.extend(targets)
        identities.append(identity)
    require(len(set(points)) == len(points) and len(set(identities)) == len(identities), 'D_selection_duplicate_question_or_point')
    deferred = [p for p in body['review_concept_ids'] if p not in points]
    if not deferred:
        return None
    policy = registration['policy']
    decision = {"schema": "cs408-D-local-subset-decision-v1", "authorization_kind": "explicit_user_question_limit",
                "user_request": policy['user_request'], "user_message_id": policy['user_message_id'],
                **{key: body.get(key) for key in ('task_id', 'source_commit', 'target_date', 'due_version', 'concept_priority_version')},
                "source_zip_sha256": source_zip_sha256, "full_review_concept_ids": body['review_concept_ids'],
                "admitted_concept_ids": points, "question_limit": contract['question_limit'],
                "selection_input_version": version,
                "deferred_concepts": [{"concept_id": p, "reason": "explicit_user_question_limit_not_selected_unanswered"} for p in deferred]}
    path = repo / '.private/study-bridge/d-scope-decisions' / (sha(encoded(decision)) + '.json')
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(encoded(decision))
    return path
