import hashlib
import io
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

import exam_tutoring_context_408 as context


MASTER_HEADER = [
    "ID",
    "来源ID",
    "年份",
    "科目",
    "主模块",
    "主知识点",
    "副知识点",
    "命中知识点",
    "题型",
    "核心考点",
    "模糊概念",
    "错因标签",
    "首次做题日期",
    "最近复做日期",
    "最近错误记录",
    "详情入口",
]

MAPPING_HEADER = [
    "正式节点ID",
    "科目",
    "证据等级",
    "实际考点",
    "主复习单元ID",
    "次复习单元ID",
    "覆盖方式",
    "复核状态",
    "原题轨",
    "原题轨资格",
    "待补事项",
]


def table(header, rows):
    separator = ["---"] * len(header)
    values = [header, separator] + rows
    return "\n".join("|" + "|".join(row) + "|" for row in values) + "\n"


class ExamTutoringContext408Tests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.repo = Path(self.temporary.name)
        self.state = self.repo / "derived"
        self.nodes = [
            (
                "OS_2020_001",
                "SRC-A",
                "RU_OS_ADDR_A",
                "先得到 S/N=1000 再检查页表地址字段边界",
            ),
            ("OS_2020_002", "SRC-B", "RU_OS_ADDR_B", "页表项容量边界"),
            ("OS_2020_003", "SRC-C", "RU_OS_ADDR_C", "页目录索引边界"),
            ("OS_2020_004", "SRC-D", "RU_OS_ADDR_D", "逻辑页号边界"),
        ]
        master_rows = []
        mapping_rows = []
        for formal_id, source_id, review_unit, mechanism in self.nodes:
            error_entry = (
                "把 S/N=1000 这个具体中间值放进了错误字段。"
                if formal_id == "OS_2020_001"
                else "把字段位数与表项个数混在一起。"
            )
            redo_action = (
                "先写出 S/N=1000，再逐字段核对。"
                if formal_id == "OS_2020_001"
                else "先写地址字段角色，再分别计算位数。"
            )
            master_rows.append(
                [
                    formal_id,
                    source_id,
                    "2020",
                    "操作系统",
                    "OS03 内存管理",
                    "OS03-17 页表",
                    "OS03-14 页式管理",
                    "OS03-17 页表；OS03-14 页式管理",
                    "单项选择题 / 地址字段辨析题",
                    mechanism,
                    "页号与页内偏移边界",
                    "E01 概念边界混淆",
                    "2026-08-20",
                    "2026-08-20",
                    error_entry,
                    "本地安全入口",
                ]
            )
            mapping_rows.append(
                [
                    formal_id,
                    "操作系统",
                    "A",
                    mechanism,
                    review_unit,
                    "无",
                    "核心机制＋第一动作",
                    "reviewed",
                    "required",
                    "eligible",
                    "无",
                ]
            )
            (self.repo / (formal_id + ".md")).write_text(
                "\n".join(
                    [
                        "# " + formal_id,
                        "",
                        "## 安全节点卡片",
                        "",
                        "- 正式节点 ID：" + formal_id,
                        "- 来源 ID：" + source_id,
                        "- 主知识点：OS03-17 页表",
                        "",
                        "## 复做安全信息",
                        "",
                        "- 安全题目摘要：这一字段不得进入教学 profile。",
                        "- 关键题设参数：这一字段也不得进入教学 profile。",
                        "- 问法类型：地址字段辨析题",
                        "- 实际考查机制：" + mechanism,
                        "- 用户错误入口：" + error_entry,
                        "- 复做第一动作：" + redo_action,
                        "- 核心考点：" + mechanism,
                        "- 模糊概念：页号 vs 页内偏移",
                        "",
                        "## 复做保护",
                        "",
                        "- 复做保护说明：不保存完整题干、完整解析、标准答案、正确选项、用户错选项、选项字母组合、图片或手写痕迹。",
                    ]
                )
                + "\n",
                encoding="utf-8",
            )
        (self.repo / "节点总表.md").write_text(
            "# 节点总表\n\n" + table(MASTER_HEADER, master_rows), encoding="utf-8"
        )
        (self.repo / "复习单元节点映射.md").write_text(
            "# 复习单元节点映射\n\n" + table(MAPPING_HEADER, mapping_rows),
            encoding="utf-8",
        )
        ledger = (
            self.repo
            / "wiki/study_vaults/408-full/state/review-loop/events.jsonl"
        )
        ledger.parent.mkdir(parents=True)
        events = []
        for ordinal, (formal_id, source_id, review_unit, _) in enumerate(
            self.nodes[:3], 1
        ):
            first_break = (
                "先把 S/N=1000 误当成了页号位数"
                if formal_id == "OS_2020_001"
                else "先把页号位数误写成表项字节数"
            )
            events.extend(
                [
                    {
                        "schema": "review_event_v1",
                        "event_id": "RE-%s-1" % ordinal,
                        "event_kind": "outcome",
                        "event_time": "2026-08-28T20:0%d:00+08:00" % ordinal,
                        "observed_date": "2026-08-28",
                        "source": "daily_practice",
                        "source_id": source_id,
                        "formal_node_id": formal_id,
                        "review_unit_id": review_unit,
                        "mechanism_key": review_unit,
                        "first_result": "wrong",
                        "confidence": "low",
                        "prompt_level": "minimal",
                        "hint_used": True,
                        "first_break": first_break,
                        "first_break_provenance": "visible_evidence",
                        "formal_write_authorized": False,
                    },
                    {
                        "schema": "review_event_v1",
                        "event_id": "RE-%s-2" % ordinal,
                        "event_kind": "outcome",
                        "event_time": "2026-08-28T21:0%d:00+08:00" % ordinal,
                        "observed_date": "2026-08-28",
                        "source": "morning_review",
                        "source_id": source_id,
                        "formal_node_id": formal_id,
                        "review_unit_id": review_unit,
                        "mechanism_key": review_unit,
                        "first_result": "partial",
                        "confidence": "medium",
                        "prompt_level": "none",
                        "hint_used": False,
                        "first_break": first_break,
                        "first_break_provenance": "user_report",
                        "formal_write_authorized": False,
                    },
                ]
            )
        ledger.write_text(
            "".join(
                json.dumps(event, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
                + "\n"
                for event in events
            ),
            encoding="utf-8",
        )

    def tearDown(self):
        self.temporary.cleanup()

    def refresh(self):
        return context.refresh_nightly(
            self.repo, "2026-08-29", output_root=self.state
        )

    def query_payload(self, **values):
        payload = {
            "schema": "exam_tutoring_context_query_v1",
            "stage": "post_answer",
            "formal_id": None,
            "source_id": None,
            "subject": "OS",
            "knowledge_points": [],
            "mechanism_terms": [],
            "ask_type_terms": [],
            "max_profiles": 3,
            "max_bytes": 8192,
        }
        payload.update(values)
        return payload

    def test_resolve_cli_selects_repeated_mechanisms_with_parent_and_preserves_views(self):
        import knowledge_profile_408 as profiles

        (self.repo / "知识点标签表.md").write_text(
            "# 知识点\n\n## OS03 内存管理\n- OS03-17 页表\n- OS03-14 页式管理\n",
            encoding="utf-8")
        self.refresh()
        ledger = self.repo / context.REVIEW_LEDGER_REL
        ref = {"path": ledger.relative_to(self.repo).as_posix(),
               "sha256": hashlib.sha256(ledger.read_bytes()).hexdigest()}
        facets = [dict(subpoint_id=profiles.subpoint_key("OS03-17", title), title=title,
                       keywords=[term], short_summary=summary, next_check="核对当前字段角色",
                       evidence=[{"source": ref["path"]}], source_refs=[ref])
                  for title, term, summary in (
                      ("页表项位数", "页表项字段", "区分页号位数与页框号位数。"),
                      ("页表项数量", "页表项个数", "根据虚拟页数确定页表项个数。"))]
        profiles.apply_updates(self.repo, [{"concept_key": "OS03-17",
            "expected_previous_version": None, "overview": "不应在小点查询展开的父概况",
            "short_summary": "父知识点摘要", "next_check": "父点检查", "timeline": [],
            "progress": [], "question_refs": [], "source_refs": [ref], "subpoints": facets}],
            operation_id="cli-subpoints", evidence_bindings={ref["path"]: ref["sha256"]},
            expected_points={"OS03-17"})
        context.refresh_after_commit(self.repo, "2026-08-29", output_root=self.state)
        argv = ["resolve", "--repo", str(self.repo), "--output-root", str(self.state),
                "--concept", "OS03-17", "--mechanism", "页表项字段",
                "--mechanism", "页表项个数"]

        def invoke(extra):
            output = io.BytesIO()
            with mock.patch.object(sys, "stdout", mock.Mock(buffer=output)):
                self.assertEqual(0, context.main(argv + extra))
            return json.loads(output.getvalue())

        result = invoke(["--view", "after_attempt"])
        self.assertEqual("matched_subpoints_only", result["scope"])
        self.assertEqual({facet["subpoint_id"] for facet in facets},
                         {record["subpoint_id"] for record in result["records"]})
        self.assertEqual({facet["short_summary"] for facet in facets},
                         {record["short_summary"] for record in result["records"]})
        cached = invoke(["--view", "after_attempt", "--known-version", result["version"]])
        self.assertEqual("unchanged", cached["status"])
        self.assertEqual([], cached["records"])
        protected = invoke([])
        self.assertEqual("protected", protected["view"])
        self.assertEqual(2, len(protected["records"]))
        for record in protected["records"]:
            self.assertNotIn("short_summary", record)
            self.assertNotIn("next_check", record)
            self.assertNotIn("evidence_object", record)

    def test_nightly_refresh_receipt_is_idempotent_and_auditable(self):
        first = self.refresh()
        second = self.refresh()
        self.assertEqual(first["receipt_sha256"], second["receipt_sha256"])
        self.assertEqual(first["manifest_sha256"], second["manifest_sha256"])
        self.assertRegex(first["receipt_sha256"], r"^[0-9a-f]{64}$")
        manifest = json.loads(
            (self.state / "manifest.json").read_text(encoding="utf-8")
        )
        self.assertEqual("manifest_last", manifest["publication_order"])
        self.assertTrue(
            (self.state / manifest["receipt_ref"]).is_file(), manifest
        )
        audited = context.audit_receipt(
            self.repo,
            first["receipt_sha256"],
            output_root=self.state,
        )
        self.assertEqual("pass", audited["status"])
        self.assertTrue(audited["full_recompute"])
        with self.assertRaises(context.ExamTutoringContextError):
            context.audit_receipt(
                self.repo, "0" * 64, output_root=self.state
            )

    def test_exact_and_term_queries_return_only_actionable_history(self):
        self.refresh()
        queries = [
            self.query_payload(formal_id="OS_2020_001"),
            self.query_payload(source_id="SRC-A"),
            self.query_payload(knowledge_points=["OS03-17"]),
            self.query_payload(mechanism_terms=["RU_OS_ADDR_A"]),
            self.query_payload(ask_type_terms=["地址字段辨析题"]),
        ]
        for query in queries:
            result = context.query_context(
                self.repo, query, output_root=self.state
            )
            self.assertEqual("ready", result["status"], query)
            self.assertEqual("exam_tutoring_context_slice_v1", result["schema"])
            self.assertRegex(result["projection_build_sha256"], r"^[0-9a-f]{64}$")
            self.assertEqual(
                "current_question_only", result["current_correctness_source"]
            )
            self.assertEqual("MATCH", result["match_status"], query)
            self.assertGreaterEqual(len(result["profiles"]), 1, query)
            self.assertLessEqual(len(result["profiles"]), 3, query)
            self.assertTrue(result["why_history_changes_tutoring"], query)
            self.assertTrue(result["correctness_policy"]["current_evidence_first"])
            self.assertEqual(
                "current_question_only",
                result["correctness_policy"]["scope"],
            )
        exact = context.query_context(
            self.repo,
            self.query_payload(formal_id="OS_2020_001"),
            output_root=self.state,
        )
        profile = exact["profiles"][0]
        self.assertEqual(2, profile["history"]["direct"]["event_count"])
        self.assertEqual(1, profile["history"]["direct"]["result_counts"]["wrong"])
        dimensions = {item["dimension"] for item in profile["teaching_actions"]}
        self.assertEqual(
            {
                "diagnostic_question",
                "hint_start",
                "representation_choice",
                "short_transfer_check",
            },
            dimensions,
        )
        support = next(
            item
            for item in profile["teaching_actions"]
            if item["dimension"] == "representation_choice"
        )
        self.assertEqual(
            "if_current_break_recurs_jump_to_minimal_worked_step_then_fade",
            support["action"],
        )
        self.assertEqual("only_after_current_break_recurs", support["condition"])
        self.assertTrue(support["does_not_establish_current_recurrence"])
        exact_card_only = context.query_context(
            self.repo,
            self.query_payload(formal_id="OS_2020_004"),
            output_root=self.state,
        )
        self.assertEqual("ready", exact_card_only["status"])
        self.assertEqual("MATCH", exact_card_only["match_status"])
        self.assertEqual(1, len(exact_card_only["profiles"]))
        card_action = exact_card_only["profiles"][0]["teaching_actions"][0]
        self.assertEqual(
            "after_current_first_action_check_formal_card_boundary",
            card_action["action"],
        )
        self.assertTrue(card_action["does_not_establish_current_recurrence"])
        self.assertRegex(
            card_action["formal_field_binding"]["card_sha256"],
            r"^[0-9a-f]{64}$",
        )
        broad_weak = context.query_context(
            self.repo,
            self.query_payload(mechanism_terms=["RU_OS_ADDR_D"]),
            output_root=self.state,
        )
        self.assertEqual("ready", broad_weak["status"])
        self.assertEqual("NO_MATCH", broad_weak["match_status"])
        self.assertEqual([], broad_weak["profiles"])
        with self.assertRaisesRegex(
            context.ExamTutoringContextError, "query schema"
        ):
            context.query_context(
                self.repo,
                {**self.query_payload(), "schema": "wrong_schema"},
                output_root=self.state,
            )

    def test_exact_identity_conflict_is_current_only_before_profile_reads(self):
        self.refresh()
        original = context._read_content_object

        def no_profiles(state, relative, *args, **kwargs):
            self.assertNotIn("objects/profiles/", relative)
            return original(state, relative, *args, **kwargs)

        for stage in ("pre_answer", "post_answer"):
            with mock.patch.object(context, "_read_content_object", side_effect=no_profiles):
                result = context.query_context(
                    self.repo, self.query_payload(
                        stage=stage, formal_id="OS_2020_004", source_id="SRC-C",
                        knowledge_points=["OS03-17"],
                    ), output_root=self.state,
                )
            self.assertEqual("unavailable", result["status"])
            self.assertEqual(["exact_identity_ambiguous"], result["reasons"])
            self.assertEqual([], result["profiles"])
            self.assertEqual("current_question_only", result["current_correctness_source"])
        # Subject remains caller metadata, not an identity filter.
        matching = context.query_context(
            self.repo, self.query_payload(formal_id="os_2020_004", source_id="src-d", subject="CN"),
            output_root=self.state,
        )
        self.assertEqual("ready", matching["status"])
        self.assertEqual("CN", matching["subject"])
        self.assertEqual(["OS_2020_004"], [p["formal_id"] for p in matching["profiles"]])
        for formal_id, source_id in (("OS_2020_099", "SRC-C"), ("OS_2020_004", "UNKNOWN")):
            missing = context.query_context(
                self.repo, self.query_payload(formal_id=formal_id, source_id=source_id),
                output_root=self.state,
            )
            self.assertEqual(["exact_identity_unresolved"], missing["reasons"])
            self.assertEqual([], missing["profiles"])

    def test_final_envelope_respects_inclusive_byte_budget(self):
        self.refresh()
        for stage in ("pre_answer", "post_answer"):
            for budget in (1024, 1100, 2048, 4096, 8192):
                for profiles in (0, 3):
                    with self.subTest(stage=stage, budget=budget, profiles=profiles):
                        result = context.query_context(
                            self.repo, self.query_payload(
                                stage=stage, formal_id="OS_2020_001", max_profiles=profiles,
                                max_bytes=budget,
                            ), output_root=self.state,
                        )
                        size = len(context._canonical_bytes(result))
                        self.assertEqual(size, result["serialized_bytes"])
                        self.assertLessEqual(size, budget)
                        if budget == 1024 and profiles:
                            self.assertEqual("unavailable", result["status"])
                            self.assertEqual(["result_envelope_exceeds_budget"], result["reasons"])
                            self.assertEqual([], result["profiles"])

    def test_low_confidence_correct_can_only_change_hint_start(self):
        profile = {
            "history": {
                "direct": {
                    "event_count": 1,
                    "result_counts": {"independent_correct": 1},
                    "confidence_counts": {"low": 1},
                    "prompt_dependency_counts": {"independent": 1},
                    "event_refs": [
                        {
                            "event_id": "RE-low-confidence",
                            "event_sha256": "a" * 64,
                            "observed_date": "2026-08-28",
                        }
                    ],
                },
                "top_direct_first_breaks": [],
            }
        }
        actions = context._teaching_actions(profile)
        self.assertEqual(1, len(actions))
        self.assertEqual("hint_start", actions[0]["dimension"])
        self.assertEqual("prior_low_confidence", actions[0]["basis"])

    def test_pre_answer_is_route_only_and_post_answer_keeps_bounded_fields(self):
        self.refresh()
        pre_answer = context.query_context(
            self.repo,
            self.query_payload(stage="pre_answer", formal_id="OS_2020_001"),
            output_root=self.state,
        )
        self.assertEqual("ready", pre_answer["status"])
        self.assertEqual("MATCH", pre_answer["match_status"])
        self.assertEqual(1, len(pre_answer["profiles"]))
        pre_profile = pre_answer["profiles"][0]
        self.assertEqual(
            "exam_tutoring_profile_pre_answer_route_v1",
            pre_profile["schema"],
        )
        self.assertTrue(pre_profile["content_fields_redacted"])
        self.assertTrue(pre_profile["teaching_actions"])
        pre_text = json.dumps(pre_answer, ensure_ascii=False, sort_keys=True)
        for forbidden in (
            "S/N=1000",
            "用户错误入口",
            "复做第一动作",
            "user_error_entry",
            "redo_first_action",
            "core_mechanism",
            "top_direct_first_breaks",
            "prior_break",
            "先把 S/N=1000 误当成了页号位数",
        ):
            self.assertNotIn(forbidden, pre_text)
        for action in pre_profile["teaching_actions"]:
            self.assertIn("dimension", action)
            self.assertIn("action", action)

        post_answer = context.query_context(
            self.repo,
            self.query_payload(stage="post_answer", formal_id="OS_2020_001"),
            output_root=self.state,
        )
        self.assertEqual("ready", post_answer["status"])
        post_profile = post_answer["profiles"][0]
        self.assertEqual("exam_tutoring_profile_v1", post_profile["schema"])
        self.assertIn("S/N=1000", post_profile["core_mechanism"])
        self.assertIn("S/N=1000", post_profile["user_error_entry"])
        self.assertIn("S/N=1000", post_profile["redo_first_action"])
        self.assertIn(
            "S/N=1000",
            post_profile["history"]["top_direct_first_breaks"][0]["text"],
        )
        self.assertTrue(post_profile["teaching_actions"])

    def test_review_append_invalidates_until_commit_refresh_includes_new_evidence(self):
        refreshed = self.refresh()
        ledger = self.repo / context.REVIEW_LEDGER_REL
        event = {
            "schema": "review_event_v1",
            "event_id": "RE-later",
            "event_kind": "outcome",
            "event_time": "2026-08-29T08:00:00+08:00",
            "observed_date": "2026-08-29",
            "source": "daily_practice",
            "source_id": "SRC-A",
            "formal_node_id": "OS_2020_001",
            "first_result": "independent_correct",
            "confidence": "high",
            "prompt_level": "none",
            "hint_used": False,
            "first_break": "",
            "first_break_provenance": "not_observed",
            "formal_write_authorized": False,
        }
        with ledger.open("a", encoding="utf-8") as handle:
            handle.write(
                json.dumps(event, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
                + "\n"
            )
        result = context.query_context(
            self.repo,
            self.query_payload(formal_id="OS_2020_001"),
            output_root=self.state,
        )
        self.assertEqual("unavailable", result["status"])
        self.assertIn("review_ledger_append_suffix_requires_refresh", result["reasons"])
        self.assertEqual([], result["profiles"])
        with self.assertRaisesRegex(
            context.ExamTutoringContextError, "receipt drift"
        ):
            context.audit_receipt(
                self.repo,
                refreshed["receipt_sha256"],
                output_root=self.state,
            )
        context.refresh_after_commit(self.repo, "2026-08-29", output_root=self.state)
        current = context.query_context(self.repo, self.query_payload(formal_id="OS_2020_001"), output_root=self.state)
        self.assertEqual("ready", current["status"])
        self.assertEqual("current", current["snapshot_lag"]["status"])
        self.assertEqual(3, current["profiles"][0]["history"]["direct"]["event_count"])
        self.assertIn("RE-later", json.dumps(current))
        replay = context.refresh_after_commit(self.repo, "2026-08-29", output_root=self.state)
        self.assertEqual("already_current", replay["status"])

    def test_commit_invalidation_and_related_source_changes_fail_closed(self):
        self.refresh()
        context.invalidate_after_commit(self.repo, "RE-committed-correction")
        result = context.query_context(self.repo, self.query_payload(formal_id="OS_2020_001"), output_root=self.state)
        self.assertEqual("unavailable", result["status"])
        self.assertIn("committed_source_invalidated", result["reasons"])
        context.refresh_after_commit(self.repo, "2026-08-29", output_root=self.state)
        related = self.repo / "概念词典.md"
        related.write_text("新概念别名\n", encoding="utf-8")
        result = context.query_context(self.repo, self.query_payload(formal_id="OS_2020_001"), output_root=self.state)
        self.assertEqual("unavailable", result["status"])
        self.assertIn("related_source_drift:概念词典.md", result["reasons"])

    def test_source_change_during_refresh_does_not_publish_mixed_snapshot(self):
        self.refresh()
        old_manifest = (self.state / "manifest.json").read_bytes()
        original = context._read_review_events
        def changed(path):
            value = original(path)
            with path.open("a") as handle:
                handle.write("\n")
            return value
        with mock.patch.object(context, "_read_review_events", side_effect=changed):
            with self.assertRaisesRegex(context.ExamTutoringContextError, "source_changed_during_refresh"):
                self.refresh()
        self.assertEqual(old_manifest, (self.state / "manifest.json").read_bytes())

    def test_repair_is_latest_teaching_evidence_without_replacing_first_result(self):
        self.refresh()
        ledger = self.repo / context.REVIEW_LEDGER_REL
        event = {"schema": "review_event_v1", "event_kind": "repair", "event_id": "RE-terminal-repair",
                 "observed_date": "2026-08-29", "event_time": "2026-08-29T08:05:00+08:00",
                 "formal_node_id": "OS_2020_001", "source_id": "SRC-B", "correction_result": "partial",
                 "correction_conclusion": "correct", "confidence": "high", "prompt_level": "progressive",
                 "hint_used": True, "formal_write_authorized": False}
        with ledger.open("a") as handle:
            handle.write(json.dumps(event) + "\n")
        context.refresh_after_commit(self.repo, "2026-08-29", output_root=self.state)
        query = context.query_context(self.repo, self.query_payload(formal_id="OS_2020_001"), output_root=self.state)
        history = query["profiles"][0]["history"]
        self.assertEqual(2, history["direct"]["event_count"])
        self.assertEqual(1, history["direct_repairs"]["event_count"])
        self.assertTrue(history["direct_repairs"]["latest"]["conclusion_correct"])
        self.assertFalse(history["direct_repairs"]["latest"]["establishes_independent_mastery"])
        self.assertEqual("RE-terminal-repair", query["profiles"][0]["teaching_actions"][0]["latest_correction"]["event_id"])
        other = context.query_context(self.repo, self.query_payload(formal_id="OS_2020_002"), output_root=self.state)
        self.assertEqual(0, other["profiles"][0]["history"]["direct_repairs"]["event_count"])

    def test_ledger_edit_with_restored_mtime_cannot_keep_old_snapshot_current(self):
        self.refresh()
        ledger = self.repo / context.REVIEW_LEDGER_REL
        before = ledger.stat()
        raw = ledger.read_bytes()
        ledger.write_bytes(raw.replace(b"SRC-A", b"SRC-X", 1))
        os.utime(ledger, ns=(before.st_atime_ns, before.st_mtime_ns))
        result = context.query_context(self.repo, self.query_payload(formal_id="OS_2020_001"), output_root=self.state)
        self.assertEqual("unavailable", result["status"])

    def test_selected_card_and_master_drift_fail_soft(self):
        self.refresh()
        selected = self.repo / "OS_2020_001.md"
        selected.write_text(
            selected.read_text(encoding="utf-8") + "\n",
            encoding="utf-8",
        )
        unavailable = context.query_context(
            self.repo,
            self.query_payload(formal_id="OS_2020_001"),
            output_root=self.state,
        )
        self.assertEqual("unavailable", unavailable["status"])
        self.assertIn("selected_card_drift", unavailable["reasons"][0])
        # Drift of an unselected card does not expand hot-path reads.
        unaffected = context.query_context(
            self.repo,
            self.query_payload(formal_id="OS_2020_002"),
            output_root=self.state,
        )
        self.assertEqual("ready", unaffected["status"])
        master = self.repo / "节点总表.md"
        master.write_text(master.read_text(encoding="utf-8") + "\n", encoding="utf-8")
        master_drift = context.query_context(
            self.repo,
            self.query_payload(formal_id="OS_2020_002"),
            output_root=self.state,
        )
        self.assertEqual("unavailable", master_drift["status"])
        self.assertIn("formal_master_drift", master_drift["reasons"])

    def test_card_only_drift_is_recovered_by_cold_refresh(self):
        old = self.refresh()
        selected = self.repo / "OS_2020_001.md"
        selected.write_text(selected.read_text() + "\n", encoding="utf-8")
        stale = context.query_context(self.repo, self.query_payload(formal_id="OS_2020_001"), output_root=self.state)
        self.assertEqual("unavailable", stale["status"])
        current = context.refresh_after_commit(self.repo, "2026-08-29", output_root=self.state)
        self.assertNotEqual("already_current", current["status"])
        self.assertNotEqual(old["receipt_sha256"], current["receipt_sha256"])
        recovered = context.query_context(self.repo, self.query_payload(formal_id="OS_2020_001"), output_root=self.state)
        self.assertEqual("ready", recovered["status"])
        self.assertEqual(current["source_version"], recovered["source_version"])
        replay = context.refresh_after_commit(self.repo, "2026-08-29", output_root=self.state)
        self.assertEqual("already_current", replay["status"])

    def test_answer_safe_bounded_query_does_not_recompute_sources(self):
        self.refresh()
        manifest = json.loads(
            (self.state / "manifest.json").read_text(encoding="utf-8")
        )
        index = json.loads(
            (self.state / manifest["index_ref"]).read_text(encoding="utf-8")
        )
        forbidden_keys = {
            "safe_summary",
            "key_parameters",
            "detail_entry",
            "relations",
            "answer",
            "options",
            "solution",
        }

        def walk(value):
            if isinstance(value, dict):
                for key, child in value.items():
                    self.assertNotIn(key, forbidden_keys)
                    walk(child)
            elif isinstance(value, list):
                for child in value:
                    walk(child)

        for entry in index["profiles"].values():
            profile = json.loads(
                (self.state / entry["profile_ref"]).read_text(encoding="utf-8")
            )
            walk(profile)
            profile_text = json.dumps(profile, ensure_ascii=False)
            self.assertNotIn("这一字段不得进入教学 profile", profile_text)
            self.assertNotIn("这一字段也不得进入教学 profile", profile_text)

        with mock.patch.object(
            context, "_compute_snapshot", side_effect=AssertionError("cold scan")
        ), mock.patch.object(
            context, "_cold_card_freshness_reasons", side_effect=AssertionError("all cards scan")
        ), mock.patch.object(
            context.Repo, "master", side_effect=AssertionError("master scan")
        ), mock.patch.object(
            Path, "read_text", side_effect=AssertionError("source text scan")
        ):
            result = context.query_context(
                self.repo,
                self.query_payload(
                    knowledge_points=["OS03-17"], max_profiles=9
                ),
                output_root=self.state,
                max_bytes=8192,
            )
        self.assertEqual("ready", result["status"])
        self.assertLessEqual(len(result["profiles"]), 3)
        self.assertLessEqual(len(context._canonical_bytes(result)), 8192)
        self.assertEqual(len(context._canonical_bytes(result)), result["serialized_bytes"])

    def test_presentation_change_does_not_revoke_personal_evidence(self):
        self.refresh()
        manifest, _ = context._load_manifest(self.state)
        original = context._sha256_file

        def changed(path):
            if path.name in {"render_interactive_408.py", "render_semantic_408.py"}:
                return "a" * 64
            return original(path)

        with mock.patch.object(context, "_sha256_file", side_effect=changed):
            self.assertEqual([], context._freshness_reasons(self.repo, manifest))

    def test_runtime_path_provider_change_revokes_prepared_context(self):
        context.refresh_nightly(self.repo, "2026-08-29", output_root=self.state)
        original_hash = context._sha256_file

        def changed_provider(path):
            if path.name == "runtime_paths_408.py":
                return "0" * 64
            return original_hash(path)

        with mock.patch.object(context, "_sha256_file", side_effect=changed_provider):
            result = context.query_context(
                self.repo, self.query_payload(knowledge_points=["OS03-17"]),
                output_root=self.state,
            )
        self.assertEqual(result["status"], "unavailable")
        self.assertIn("point_memory_builder_changed", result["reasons"])


if __name__ == "__main__":
    unittest.main()
