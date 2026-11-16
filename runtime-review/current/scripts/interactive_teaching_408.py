"""Deterministic interaction recipes, separate from reviewed learning evidence.

Recipes bind existing semantic fields, not inferred physical edges. The teacher
compiles the allowed part into a scene for render_interactive_408.py; a recipe is
neither an independently authored webpage nor evidence of learner interaction.
"""
from __future__ import annotations

import hashlib
import json

SCHEMA = "cs408-interaction-recipe-v1"
DELIVERY = (
    "同一轮同时给出文字线性讲解与可点击图解：聊天正文为主，按题目线索、条件、动作、原因和结果讲清本轮获准推理。"
    "可视化为辅，绑定正文的具体步骤；点击对象、关系或步骤只展开补充解释并突出真实关联，不能隐藏主线必需理由。"
    "页面也应有无需点击即可顺序阅读的正文；文字和图解使用相同对象、条件与结论。"
    "使用当前Visualize内容引用协议；静态图可作界面内容，路径或仅放大图片不算交互交付。"
    "正文与前端都须先按当前题/Pxx和提示层级过滤；折叠不等于答案保护，点击不计作学习或掌握。"
)


def _digest(value):
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True,
                                    separators=(",", ":")).encode()).hexdigest()


def _personal_adaptation(feedback):
    """Bind reviewed feedback for the teacher, never for student click targets.

    The profile writer owns evidence validation. These references point into
    that same selected plan; raw conversation references never enter a recipe.
    A proposed adjustment is not itself a user preference or a learning effect.
    """
    required = {"form", "scope", "preference", "observed_effect", "next_adjustment", "next_check"}
    if not required <= feedback.keys():
        # Historical partial feedback may be retained by a projection, but it
        # cannot establish an actionable preference/effect binding. New formal
        # plans still pass the profile writer's complete evidence validation.
        return None
    preference, effect = feedback["preference"], feedback["observed_effect"]
    application = {
        "explicit_preference": "use_within_stated_scope",
        "observed_tendency": "tentative_within_scope_not_confirmed_preference",
        "unknown": "no_preference_claim",
    }
    if preference.get("status") not in application:
        raise ValueError("interaction_personal_preference_status_invalid")
    if effect.get("support") not in {
            "proposed_only", "user_reported_helpful", "prompted_repair", "independent_transfer"}:
        raise ValueError("interaction_personal_effect_support_invalid")
    # Whitelist summarized semantic fields; evidence paths/quotes and any
    # unrelated keys stay solely in the original, evidence-bound profile.
    semantic = {key: feedback[key] for key in ("form", "scope", "next_adjustment", "next_check")}
    semantic["preference"] = {key: preference[key] for key in ("status", "text", "source_ids")}
    semantic["observed_effect"] = {key: effect[key] for key in ("support", "text", "source_ids")}
    if "comparison" in feedback:
        semantic["comparison"] = {key: feedback["comparison"][key]
                                  for key in ("reference", "finding", "source_ids")}
    result = {
        "schema": "cs408-personal-adaptation-binding-v1", "audience": "teacher_only",
        "binding_sha256": _digest(semantic), "source_ref": "visual_feedback",
        "applies_to": ["chat_linear_text", "auxiliary_mechanism_scene"],
        "form": {"ref": "visual_feedback.form", "text": semantic["form"]},
        "scope": {"ref": "visual_feedback.scope", "text": semantic["scope"]},
        "preference": {"ref": "visual_feedback.preference", "status": preference["status"],
            "text": preference["text"], "source_ids_ref": "visual_feedback.preference.source_ids",
            "application": application[preference["status"]]},
        "observed_effect": {"ref": "visual_feedback.observed_effect", "support": effect["support"],
            "text": effect["text"], "source_ids_ref": "visual_feedback.observed_effect.source_ids"},
        "next_adjustment": {"ref": "visual_feedback.next_adjustment", "text": semantic["next_adjustment"],
            "role": "teacher_proposal_requires_current_question_and_scope"},
        "next_check": {"ref": "visual_feedback.next_check", "text": semantic["next_check"],
            "role": "unperformed_check_not_learning_evidence"},
        "authority": "current_question_syllabus_hint_level_and_current_user_instruction_first",
        "effectiveness_inferred_from_preference": False,
        "student_click_target": False,
    }
    if "comparison" in semantic:
        result["comparison_ref"] = "visual_feedback.comparison"
    return result


def recipe(value, *, identity=None, scope=None):
    """Bind every existing object/step/boundary without inventing their relations."""
    personal = value.get("schema") == "cs408-teaching-plan-v1"
    if personal:
        visual = value.get("visual", {})
        fields = {"focus": visual.get("focus", ""), "steps": visual.get("steps", []), "elements": visual.get("elements", []),
                  "first_action": value.get("first_action", ""),
                  "explanation": value.get("explanation", {}).get("strategy", ""),
                  "avoid": value.get("avoid", ""), "check": value.get("check", "")}
        bindings = [("first_action", "先做的动作"), ("visual.focus", "当前机制"),
                    ("explanation.strategy", "解释桥梁")]
        bindings += [(f"visual.elements.{i}", text) for i, text in enumerate(visual.get("elements", []))]
        bindings += [(key, label) for key, label in (("avoid", "需要避开的混淆"), ("check", "待检验边界")) if value.get(key)]
        steps = visual.get("steps", [])
        prefix = "visual.steps"
        if value.get("source_visual_policy"):
            fields["source_visual_policy"] = value["source_visual_policy"]
        adaptation = _personal_adaptation(value["visual_feedback"]) if value.get("visual_feedback") else None
        if adaptation:
            fields["personal_adaptation_binding"] = adaptation["binding_sha256"]
    else:
        fields = {k: value[k] for k in ("scene", "focus", "given_objects", "initial_state",
                  "target", "steps", "checks", "personal_adaptation", "adaptation_rules") if k in value}
        bindings = [("scene", "场景对象与关系")] if "scene" in value else []
        if "initial_state" in value: bindings += [("initial_state", "初始状态")]
        if "target" in value: bindings += [("target", "当前所求")]
        bindings += [(f"given_objects.{i}", str(v) if isinstance(v, str) else "已知对象")
                     for i, v in enumerate(value.get("given_objects", []))]
        bindings += [(f"checks.{i}", v) for i, v in enumerate(value.get("checks", []))]
        steps, prefix = value.get("steps", []), "steps"
    source_visuals = value.get("source_visuals")
    semantic_redraw = bool(source_visuals and source_visuals.get("presentation_policy"))
    if source_visuals is not None:
        fields["source_visuals"] = source_visuals
    targets = [{"id": path, "label": label, "expand_ref": path,
                "highlight_refs": [path], "action": "expand_collapse_and_focus"}
               for path, label in bindings]
    targets += [{"id": f"{prefix}.{i}", "label": step, "expand_ref": f"{prefix}.{i}",
                 "highlight_refs": [f"{prefix}.{i}"], "action": "select_step_and_focus"}
                for i, step in enumerate(steps)]
    if semantic_redraw:
        design = source_visuals.get("teaching_design", {})
        for panel_index, panel in enumerate(design.get("panels", [])):
            for node_index, item in enumerate(panel.get("nodes", [])):
                path = f"source_visuals.teaching_design.panels.{panel_index}.nodes.{node_index}"
                targets.append({"id": path, "label": item["label"], "expand_ref": path + ".explanation",
                    "highlight_refs": [path], "action": "expand_semantic_object_and_actual_relations"})
        for index, item in enumerate(design.get("stages", [])):
            path = f"source_visuals.teaching_design.stages.{index}"
            targets.append({"id": path, "label": item["label"], "expand_ref": path + ".explanation",
                "highlight_refs": [path], "action": "select_mechanism_state"})
    elif source_visuals:
        for index, item in enumerate(source_visuals.get("overview", [])):
            path = f"source_visuals.overview.{index}"
            targets.append({"id": path, "label": str(item.get("label") or item.get("id") or "流程步骤"),
                "expand_ref": path, "highlight_refs": [path], "action": "select_overview_and_focus_source_region"})
        for index, item in enumerate(source_visuals.get("region_bindings", [])):
            path = f"source_visuals.region_bindings.{index}"
            targets.append({"id": path, "label": str(item.get("label") or item.get("region_id") or "原图机制区域"),
                "expand_ref": path, "highlight_refs": [path], "action": "expand_source_region_explanation"})
    result = {
        "schema": SCHEMA, "delivery_policy_version": "linear-text-primary-408-v1", "identity": identity or value.get("question_identity") or value.get("code"),
        "scope": scope or ("personal_plan" if personal else "exact_question" if "question_identity" in value else "point"),
        "semantic_sha256": _digest(fields),
        "delivery": "visualize_inline_interactive", "click_targets": targets,
        "step_order": [f"{prefix}.{i}" for i in range(len(steps))],
        "step_mode": "teacher_selects_state_changes_from_bound_steps" if len(steps) > 1 else "expand_only",
        "scene_binding": {
            "source": "visual.focus" if personal else "scene",
            "compile": "把本方案的具体对象布置为可点击区域；每个区域展开本对象在当前动作中的角色、条件或变化。",
            "relationships": "只按原scene、步骤及本题条件连线；点击高亮真实依赖/对应对象。引用位置不是物理因果边，不自动按列表顺序推断因果。",
            "step_changes": "有状态变化时，为所选原步骤填写同一组对象的前后值与活动关系；纯结构只点击展开，不强加动画。",
        },
        "first_screen": "先显示本轮获准的线性文字主线，依次讲清目标、条件、动作及原因；对应局部图解辅助核对，必需理由无需点击。",
        "linear_text": {"primary": True, "in_chat": True, "visible_without_click": True,
            "step_refs": [f"{prefix}.{i}" for i in range(len(steps))]},
        "answer_protection": {
            "protected": "不输出此教师配方或解题场景；只从已公开题设独立编译允许的界面。",
            "after_attempt": "不是自动开放全部答案；逐对象/解释/边/状态按本轮提示层级过滤后再序列化。",
            "frontend_excludes": ["未公开答案", "最终选项或数值", "可反推出答案的状态", "兄弟小问", "原始教师模板及证据路径"],
        },
        "personalization": "同次有效个人摘要和当前首答决定文字从哪一步开始、哪里放慢及何时停止；过期摘要只保留状态，不当作当前依据。",
        "evidence_status": "derived_delivery_recipe_not_learning_evidence",
        "effectiveness": "unverified",
    }
    if personal and adaptation:
        result["personal_adaptation"] = adaptation
    if semantic_redraw:
        result["source_visual_binding"] = {
            "source": "source_visuals", "design": "source_visuals.teaching_design",
            "narration": "source_visuals.narrative_plan", "presentation_policy": source_visuals["presentation_policy"],
            "layout": "线性正文先说明观察、原因与结果；独立重绘按正文步骤辅助核对，点击只补局部细节；源图后台参考。",
            "explanations": "口述必须说明这一步为什么发生、改变谁、下一步受何约束；使用可选择文字，不复制原板书或套空泛段落。",
            "selection": "只编译当前精确题与当前小点；无可用本问设计时由讲解者按本问条件重绘，不能回退扫描图热点。",
            "scope": "同次考纲边界、当前提示级和-Pxx范围先于前端序列化；父题模型不直接用于独立小问。",
        }
        result["first_screen"] = "顶部先给本轮获准的文字推理路径并顺序展开因果；对应图解辅助理解对象、方向和状态，必需理由保持可见。"
        result["scene_binding"]["source"] = "source_visuals.teaching_design" if source_visuals.get("teaching_design") else "scene"
        result["scene_binding"]["compile"] = "以精确题事实和图解方法为依据，独立重绘当前范围内的核心对象、实际关系和状态；扫描图只在后台参考。"
    elif source_visuals:
        result["source_visual_binding"] = {
            "source": "source_visuals", "overview": "source_visuals.overview",
            "regions": "source_visuals.region_bindings",
            "layout": "先用线性正文讲清当前条件、步骤与机制，图源仅供核对；点击图中对象补充局部解释并高亮对应关系。",
            "explanations": "口述讲解使用可选择的正文文本；原题图只承担真实结构，不将讲解栅格化。",
            "selection": "只用当前精确题及有效小点范围；父点图源是教师备课候选，不是通过14天门禁的旧题推荐。",
            "scope": "遵守同次syllabus_context.teaching_boundary及每张图的publication_scope；教师图源不等于前端可公开。",
        }
        if source_visuals.get("assets"):
            result["first_screen"] = "上方先显示本题已获准的文字步骤与因果，图源结构辅助核对；点击只补细节。"
        else:
            result["first_screen"] = "上方先显示本题已获准的文字步骤与因果；按当前条件重绘辅助图，点击只补局部细节。"
    return result


def validate_recipe(value, source, *, identity=None, scope=None, allow_legacy_policy=False):
    expected = recipe(source, identity=identity, scope=scope)
    if value != expected:
        # Old stored recipes are immutable evidence-adjacent derived objects.
        # Only their exact semantic bindings may be admitted for re-projection;
        # no old policy text is reused by attach(). New-policy drift still fails.
        legacy = (allow_legacy_policy and not value.get('delivery_policy_version')
                  and all(value.get(key) == expected.get(key) for key in
                    ('schema', 'identity', 'scope', 'semantic_sha256', 'delivery',
                     'click_targets', 'step_order', 'step_mode', 'personal_adaptation')))
        # Old personal recipes did not bind feedback. Admit only their exact
        # former projection when migration is explicitly requested; attach()
        # always rebuilds the new binding. This never accepts a changed binding
        # in an already-upgraded recipe or changes non-personal validation.
        if (not legacy and allow_legacy_policy and source.get('schema') == 'cs408-teaching-plan-v1'
                and source.get('visual_feedback') and 'personal_adaptation' not in value):
            previous_source = {key: item for key, item in source.items() if key != 'visual_feedback'}
            previous = recipe(previous_source, identity=identity, scope=scope)
            legacy = value == previous
        if not legacy:
            raise ValueError("interaction_recipe_semantic_binding_or_policy_drift")
    return value


def attach(value, *, identity=None, scope=None):
    result = dict(value)
    result["interaction"] = recipe(value, identity=identity, scope=scope)
    return result


def compact_recipe(value):
    """Same field bindings without repeating protocol prose in a small plan."""
    full = recipe(value)
    result = {"schema": "cs408-interaction-recipe-ref-v1", "semantic_sha256": full["semantic_sha256"],
            "delivery": full["delivery"], "text_primary": True,
            "delivery_policy_version": full["delivery_policy_version"], "targets": [target["expand_ref"] for target in full["click_targets"]],
            "action": "expand_bound_field_and_highlight_scene_objects",
            "step_order": full["step_order"]}
    if full.get("personal_adaptation"):
        binding = full["personal_adaptation"]
        result["personal_adaptation"] = {
            "schema": "cs408-personal-adaptation-ref-v1", "audience": "teacher_only",
            "binding_sha256": binding["binding_sha256"], "source_ref": binding["source_ref"],
            "form_ref": "visual_feedback.form", "scope_ref": "visual_feedback.scope",
            "preference": {key: binding["preference"][key] for key in ("ref", "status", "application")},
            "observed_effect": {key: binding["observed_effect"][key] for key in ("ref", "support")},
            "next_adjustment_ref": "visual_feedback.next_adjustment",
            "next_check_ref": "visual_feedback.next_check",
            "effectiveness_inferred_from_preference": False, "student_click_target": False,
        }
        if "comparison_ref" in binding:
            result["personal_adaptation"]["comparison_ref"] = binding["comparison_ref"]
    return result
