"""Source-backed visual lesson proposals, separate from learner evidence.

Catalogs are read only during cold builds. Hot queries check only the source
guards attached to the selected point; they never discover or scan catalogs.
"""
from __future__ import annotations

import copy
import hashlib
import json
from pathlib import Path
import re
from interactive_teaching_408 import DELIVERY, attach, validate_recipe

SCHEMA = "cs408-visual-teaching-catalog-v1"
ROOT_REL = Path(".private/visual-teaching-408")
SUBJECTS = ("DS", "CO", "OS", "CN")
CATALOG_FILES = tuple((ROOT_REL / (subject + ".json")).as_posix() for subject in SUBJECTS)
SHA = re.compile(r"[0-9a-f]{64}")
SUBPOINT_ID = re.compile(r"KSP-[0-9a-f]{24}")
OUTLINE_ID = re.compile(r"S27-(?:DS|CO|OS|CN)(?:-\d{2})+")
OVERRIDE_FIELDS = ("focus", "scene", "steps", "checks", "personal_adaptation")


def _require(condition, reason):
    if not condition:
        raise ValueError("visual_template_" + reason)


def _bytes(value):
    return (json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n").encode()


def _source_guard(root, ref, cache):
    path = Path(ref["path"])
    path = path if path.is_absolute() else root / path
    # Keep both names: changing a compatible symlink must invalidate its guard.
    key = str(path.absolute())
    if key not in cache:
        _require(path.is_file(), "source_missing:" + key)
        target = path.resolve(strict=True)
        before = path.stat()
        digest = hashlib.sha256()
        with path.open("rb") as stream:
            for block in iter(lambda: stream.read(1024 * 1024), b""):
                digest.update(block)
        after = path.stat()
        _require((before.st_size, before.st_mtime_ns, before.st_ctime_ns) ==
                 (after.st_size, after.st_mtime_ns, after.st_ctime_ns), "source_changed_during_build:" + key)
        cache[key] = dict(path=key, resolved_path=str(target), size=after.st_size,
                          mtime_ns=after.st_mtime_ns, ctime_ns=after.st_ctime_ns,
                          sha256=digest.hexdigest())
    guard = cache[key]
    if "sha256" in ref:
        _require(isinstance(ref["sha256"], str) and SHA.fullmatch(ref["sha256"]), "source_sha_invalid")
        _require(ref["sha256"] == guard["sha256"], "source_hash_drift:" + key)
    return guard


def load_catalogs(repo_root, labels, *, syllabus_labels=None, allow_stale_sources=False):
    """Read four fixed catalogs; no catalog is an explicit legacy/missing state."""
    if repo_root is None:
        return {"templates": {}, "guards": {}, "outline_templates": {}, "outline_guards": {}}
    root = Path(repo_root)
    paths = [root / relative for relative in CATALOG_FILES]
    if not any(path.exists() for path in paths):
        return {"templates": {}, "guards": {}, "outline_templates": {}, "outline_guards": {}}
    templates, guards, outline_templates, outline_guards, source_cache = {}, {}, {}, {}, {}
    rebinding_proof_cache = {}
    for subject, path in zip(SUBJECTS, paths):
        _require(path.is_file() and not path.is_symlink(), "catalog_missing_or_unsafe:" + subject)
        _require(path.stat().st_size <= 8 * 1024 * 1024, "catalog_too_large:" + subject)
        catalog = json.loads(path.read_text(encoding="utf-8"))
        _require(isinstance(catalog, dict) and catalog.get("schema") == SCHEMA
                 and catalog.get("subject") == subject and isinstance(catalog.get("templates"), dict),
                 "catalog_schema_invalid:" + subject)
        expected = {code for code in labels if code.startswith(subject)}
        _require(set(catalog["templates"]) == expected, "catalog_coverage_mismatch:" + subject)
        outline = catalog.get("outline_templates", {})
        _require(isinstance(outline, dict), "outline_templates_invalid:" + subject)
        for key in outline:
            _require(isinstance(key, str) and OUTLINE_ID.fullmatch(key) and key.startswith("S27-" + subject + "-"),
                     "outline_identity_invalid:" + str(key))
            if syllabus_labels is not None:
                _require(key in syllabus_labels, "outline_identity_not_in_syllabus:" + key)
        for code, raw in [*catalog["templates"].items(), *outline.items()]:
            is_outline = code in outline
            expected_title = (syllabus_labels.get(code) if syllabus_labels is not None else None) if is_outline else labels[code]
            _require(isinstance(raw, dict) and raw.get("code") == code
                     and isinstance(raw.get("title"), str) and raw["title"].strip()
                     and (expected_title is None or raw["title"] == expected_title),
                     "identity_mismatch:" + code)
            for field in ("kind", "focus", "scene", "personal_adaptation"):
                _require(isinstance(raw.get(field), str) and raw[field].strip(), "text_required:" + code + ":" + field)
            for field in ("steps", "checks"):
                _require(isinstance(raw.get(field), list) and raw[field]
                         and all(isinstance(item, str) and item.strip() for item in raw[field]),
                         "items_required:" + code + ":" + field)
            overrides = raw.get("subpoint_overrides", {})
            _require(isinstance(overrides, dict), "subpoint_overrides_invalid:" + code)
            for key, override in overrides.items():
                _require(isinstance(key, str) and SUBPOINT_ID.fullmatch(key), "subpoint_override_id_invalid:" + code)
                _require(isinstance(override, dict) and set(OVERRIDE_FIELDS) <= set(override)
                         and set(override) <= set(OVERRIDE_FIELDS) | {"kind", "interaction"},
                         "subpoint_override_fields_invalid:" + code + ":" + key)
                if "kind" in override:
                    _require(isinstance(override["kind"], str) and override["kind"].strip(),
                             "subpoint_override_kind_invalid:" + code + ":" + key)
                for field in ("focus", "scene", "personal_adaptation"):
                    _require(isinstance(override[field], str) and override[field].strip(),
                             "subpoint_override_text_required:" + code + ":" + key + ":" + field)
                for field in ("steps", "checks"):
                    _require(isinstance(override[field], list) and override[field]
                             and all(isinstance(item, str) and item.strip() for item in override[field]),
                             "subpoint_override_items_required:" + code + ":" + key + ":" + field)
            _require(raw.get("evidence_status") == "source_reviewed_proposal"
                     and raw.get("effectiveness") == "unverified", "evidence_status_invalid:" + code)
            refs = raw.get("source_refs")
            _require(isinstance(refs, list) and refs, "sources_required:" + code)
            selected, source_issues, source_rebindings = [], [], []
            for ref in refs:
                _require(isinstance(ref, dict) and all(isinstance(ref.get(field), str) and ref[field].strip()
                         for field in ("path", "locator")), "source_locator_required:" + code)
                if allow_stale_sources:
                    source_path = Path(ref['path'])
                    source_path = source_path if source_path.is_absolute() else root / source_path
                    if not source_path.is_file():
                        guard = {'path': str(source_path.absolute()), 'source_missing': True}
                    else:
                        guard = _source_guard(root, {'path': ref['path']}, source_cache)
                    declared = ref.get('sha256')
                    if declared is not None:
                        _require(isinstance(declared, str) and SHA.fullmatch(declared), 'source_sha_invalid')
                    if guard.get('source_missing') or not declared or guard['sha256'] != declared:
                        rebinding = None
                        if declared and not guard.get('source_missing'):
                            from source_visual_408 import reference_rebinding
                            rebinding = reference_rebinding(root, code, ref, guard['sha256'],
                                                           proof_cache=rebinding_proof_cache)
                        if rebinding is not None:
                            proof_guard = _source_guard(root, rebinding['proof'], source_cache)
                            if proof_guard not in selected:
                                selected.append(proof_guard)
                            source_rebindings.append(rebinding)
                        else:
                            source_issues.append({'path': ref['path'], 'locator': ref['locator'],
                                'declared_sha256': declared, 'observed_sha256': guard.get('sha256'),
                                'reason': 'source_missing' if guard.get('source_missing') else
                                          'reviewed_source_hash_missing' if not declared else 'source_hash_changed'})
                else:
                    guard = _source_guard(root, ref, source_cache)
                if guard not in selected:
                    selected.append(guard)
            _require(len(_bytes(raw)) <= 48 * 1024, "template_too_large:" + code)
            prepared = copy.deepcopy(raw)
            if "interaction" in raw:
                validate_recipe(raw["interaction"], raw, allow_legacy_policy=True)
            if source_issues:
                prepared['_source_review_state'] = 'stale'
                prepared['_source_review_issues'] = source_issues
            if source_rebindings:
                prepared['_source_reference_rebindings'] = source_rebindings
            (outline_templates if is_outline else templates)[code] = prepared
            (outline_guards if is_outline else guards)[code] = selected
    return {"templates": templates, "guards": guards,
            "outline_templates": outline_templates, "outline_guards": outline_guards}


def source_guards_current(guards):
    for guard in guards:
        try:
            path = Path(guard["path"])
            if guard.get('source_missing'):
                if path.is_file():
                    return False
                continue
            info = path.stat()
            if not path.is_file() or str(path.resolve(strict=True)) != guard["resolved_path"] or (
                    info.st_size, info.st_mtime_ns, info.st_ctime_ns) != (
                    guard["size"], guard["mtime_ns"], guard["ctime_ns"]):
                return False
        except (OSError, ValueError, KeyError, TypeError):
            return False
    return True


def source_visual_guards(repo_root, source_visuals, cache=None):
    """Bind only selected image bytes; the atlas manifest binds plan semantics."""
    if repo_root is None or not source_visuals:
        return []
    selected, cache = [], {} if cache is None else cache
    for asset in source_visuals.get("assets", []):
        if not isinstance(asset, dict) or not asset.get("path"):
            continue
        _require(isinstance(asset.get("sha256"), str) and SHA.fullmatch(asset["sha256"]),
                 "source_visual_sha_invalid")
        guard = _source_guard(Path(repo_root), {"path": asset["path"], "sha256": asset["sha256"]}, cache)
        if guard not in selected:
            selected.append(guard)
    return selected


def project_template(template, evidence_object=None, *, subpoint=None, syllabus_id=None, repo_root=None):
    from semantic_teaching_408 import project_for_teaching
    if not template:
        return {"visual_template_status": "missing"}
    if template.get('_source_review_state') == 'stale':
        if repo_root is not None:
            from source_visual_408 import point_source_visuals
            source_visuals = point_source_visuals(repo_root, template["code"])
            if source_visuals is not None:
                # Keep stale authored prose out of every delivered field. The
                # independently rebuilt atlas remains usable with the current
                # formal personal summary, without re-certifying that prose.
                value = {"code": template["code"], "source_visuals": project_for_teaching(source_visuals),
                    "scope": "current_source_visual_only",
                    "origin": "current_source_visual_plan_not_learner_evidence",
                    "legacy_template_status": "stale_do_not_use",
                    "teacher_use": "只使用当前真题图源及同次考纲；依据当前正式个人摘要现场适配。旧场景、步骤与个人适配文本未开放。",
                    "delivery_instruction": DELIVERY, "effectiveness": "unverified",
                    "independent_mastery_inferred": False}
                identity = template["code"]
                if subpoint:
                    identity = subpoint["subpoint_id"]
                    value.update(subpoint_id=identity, current_subpoint_title=subpoint["title"])
                    value["source_visuals"].update(subpoint_id=identity,
                        adaptation_scope="current_subpoint_from_parent_source_visuals",
                        selection_boundary="仅选当前小点相关图区；父点候选不证明本小点已逐图审核，不自动推荐旧题。")
                elif syllabus_id:
                    identity = syllabus_id
                    value.update(syllabus_id=syllabus_id, has_personal_history=False, scheduling_eligible=False)
                return {"visual_template_status": "source_visual_only",
                    "visual_template_reason": "legacy_template_source_changed_current_atlas_is_independent",
                    "visual_template": attach(value, identity=identity, scope=value["scope"])}
        return {'visual_template_status': 'stale', 'visual_template_rebind_required': True,
                'visual_template_reason': 'bound_reference_source_changed_or_unavailable'}
    # Review inventories belong to the complete evidence object. A narrow KSP
    # query must not import every sibling's historical mechanism.
    fields = {"code", "title", "kind", "focus", "scene", "steps", "checks",
              "personal_adaptation", "evidence_status", "effectiveness",
              "scope_status", "source_review_note", "exam_source_binding_policy"}
    value = {key: copy.deepcopy(item) for key, item in template.items() if key in fields}
    value.update(origin="source_backed_teaching_proposal_not_learner_evidence",
                 delivery_instruction=DELIVERY, independent_mastery_inferred=False)
    if subpoint:
        value.update(scope="parent_template_adapt_to_current_subpoint", subpoint_id=subpoint["subpoint_id"],
                     current_subpoint_title=subpoint["title"],
                     adaptation_boundary="只选与当前小点有关的对象、条件和步骤；父模板不证明本小点已审核或用户已掌握。")
        override = template.get("subpoint_overrides", {}).get(subpoint["subpoint_id"])
        if override is not None:
            value.update({field: copy.deepcopy(override[field]) for field in OVERRIDE_FIELDS})
            value["kind"] = override.get("kind", "小点专用局部图解")
            value.update(scope="subpoint_specific_visual_override",
                         adaptation_boundary="此图解仅对应精确小点身份；不改变正式归属、个人摘要或掌握判断，教学效果尚未验证。")
    elif syllabus_id:
        value.update(scope="syllabus_only_visual_template", syllabus_id=syllabus_id,
                     has_personal_history=False, scheduling_eligible=False)
    else:
        value["scope"] = "canonical_point"
    if repo_root is not None:
        from source_visual_408 import point_source_visuals
        source_visuals = point_source_visuals(repo_root, template["code"])
        if source_visuals is not None:
            value["source_visuals"] = project_for_teaching(source_visuals)
            if subpoint:
                value["source_visuals"].update(
                    subpoint_id=subpoint["subpoint_id"],
                    adaptation_scope=value["scope"],
                    selection_boundary="只选当前小点及本题已公开对象相关的图区；父点候选不证明本小点已逐图审核，不自动推荐旧题。")
    value = attach(value, identity=value.get("subpoint_id") or value.get("syllabus_id") or value["code"],
                   scope=value["scope"])
    result = {"visual_template_status": "available", "visual_template": value}
    if evidence_object:
        result["visual_template_evidence_object"] = copy.deepcopy(evidence_object)
    return result


def redact_changed_source(record):
    value = copy.deepcopy(record)
    value.pop("visual_template", None)
    value.pop("visual_template_evidence_object", None)
    value.update(visual_template_status="source_changed_requires_refresh",
                 visual_template_reason="bound_reference_source_changed_or_unavailable")
    return value


def render_template(template):
    if not template:
        return ["可视化模板尚未编写；不以标签名称冒充已审阅方案。"]
    if template.get('_source_review_state') == 'stale':
        return ['可视化模板来源已变化或缺失，需重新核对绑定；保留原始证据，暂不展示旧图解。']
    lines = ["此处是参考资料支持的教学方案，效果尚未验证；不属于个人作答或掌握度证据。", "",
             "图解形式：" + template["kind"], "", "只解决：" + template["focus"], "",
             "画面布局：" + template["scene"], "", "呈现步骤："]
    lines.extend(str(i) + ". " + text for i, text in enumerate(template["steps"], 1))
    lines += ["", "条件与避错：", *["- " + text for text in template["checks"]], "",
              "按当前表现调整：" + template["personal_adaptation"], "", DELIVERY, "", "参考定位："]
    lines.extend("- " + ref["path"] + " · " + ref["locator"] for ref in template["source_refs"])
    if template.get("subpoint_overrides"):
        def cell(value):
            return str(value).replace("|", "\\|").replace("\n", "<br>")
        lines += ["", "局部小点专用图解（精确身份命中时替换父图画面，沿用以上参考资料）：", "",
                  "| 小点身份 | 图解重点 | 画面布局 |", "| --- | --- | --- |"]
        lines.extend("| " + " | ".join(cell(value) for value in (key, override["focus"], override["scene"])) + " |"
                     for key, override in template["subpoint_overrides"].items())
    return lines
