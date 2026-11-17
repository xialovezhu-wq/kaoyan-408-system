import copy
import hashlib
import json
import sys
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
import exam_tutoring_context_408 as context
import knowledge_profile_408 as profiles
import test_exam_tutoring_context_408 as fixtures


class CompletePersonalContextTests(unittest.TestCase):
    setUp = fixtures.ExamTutoringContext408Tests.setUp
    tearDown = fixtures.ExamTutoringContext408Tests.tearDown
    refresh = fixtures.ExamTutoringContext408Tests.refresh

    def author(self):
        (self.repo / "知识点标签表.md").write_text(
            "# 知识点\n\n## OS03 内存管理\n- OS03-17 页表\n- OS03-14 页式管理\n")
        self.refresh()
        ledger = self.repo / context.REVIEW_LEDGER_REL
        ref = {"path": ledger.relative_to(self.repo).as_posix(),
               "sha256": hashlib.sha256(ledger.read_bytes()).hexdigest()}
        self.target = profiles.subpoint_key("OS03-17", "当前字段")
        sibling = profiles.subpoint_key("OS03-17", "兄弟机制")
        facet = {"subpoint_id": self.target, "title": "当前字段", "keywords": ["当前字段"],
                 "short_summary": "当前小点完整摘要", "next_check": "只核对当前字段", "source_refs": [ref],
                 "evidence": [{"record_id": "current-source-evidence", "condition": "只适用当前机制"}]}
        self.update = {"concept_key": "OS03-17", "expected_previous_version": None,
            "overview": "完整概况：保留适用条件和反证。" * 150,
            "short_summary": "这是有来源的短摘要。" * 75 + "末尾条件不能遗漏。",
            "progress": ["第一条已有证据", "第二条提示后修复", "第三条未验证边界" * 100],
            "next_check": "下一步必须检查当前边界。" * 100 + "最后一个必要条件。",
            "timeline": [{"summary": "早期错误"}, {"summary": "后续反证与更正"}],
            "source_refs": [ref], "question_refs": [{"formal_id": "OS_2020_001"}],
            "subpoints": [facet, {**facet, "subpoint_id": sibling, "title": "兄弟机制",
                "keywords": ["兄弟机制"], "short_summary": "SIBLING_PRIVATE_VALUE_97"}]}
        profiles.apply_updates(self.repo, [self.update], operation_id="full-personal-test",
            evidence_bindings={ref["path"]: ref["sha256"]}, expected_points={"OS03-17"})
        self.refresh()

    def resolve(self, view="after_attempt", **options):
        query = {"schema": context.QUERY_INPUT_SCHEMA,
                 "stage": "pre_answer" if view == "protected" else "post_answer",
                 "knowledge_points": ["OS03-17"], "max_profiles": 0}
        query.update(options.pop("query", {}))
        return context.query_context(self.repo, query, output_root=self.state,
                                     teaching_view=view, **options)

    def test_full_context_preserves_all_personal_semantics(self):
        self.author()
        result = self.resolve(materials="core")
        record = result["records"][0]
        for name in ("overview", "short_summary", "timeline", "progress", "next_check"):
            self.assertEqual(self.update[name], record["personal_context"][name])
            self.assertNotIn(name, record)
        self.assertEqual("personal_context", record["personal_fields_ref"])
        self.assertEqual(1, json.dumps(result, ensure_ascii=False).count(self.update["overview"]))
        self.assertTrue(result["personal_read_receipt"]["complete"])
        self.assertFalse(result["personal_read_receipt"]["raw_evidence_loaded"])
        self.assertEqual("legacy_unstructured", record["personal_context"]["claim_view"]["status"])
        self.assertNotIn("SIBLING_PRIVATE_VALUE_97", json.dumps(result))

    def test_legacy_all_materials_retains_record_fields_without_truncation(self):
        self.author()
        result = self.resolve(materials="all")
        record = result["records"][0]
        for name in ("overview", "short_summary", "timeline", "progress", "next_check"):
            self.assertEqual(self.update[name], record[name])
        self.assertTrue(result["personal_read_receipt"]["complete"])

    def test_summary_version_cannot_claim_full_read_or_cross_material_reuse(self):
        self.author()
        short = self.resolve(personal_scope="summary", materials="core")
        self.assertFalse(short["personal_read_receipt"]["complete"])
        full = self.resolve(known_version=short["version"], materials="core")
        self.assertEqual("available", full["status"])
        self.assertTrue(full["personal_read_receipt"]["complete"])
        again = self.resolve(known_version=full["version"], materials="core")
        self.assertEqual("unchanged", again["status"])
        self.assertIsNone(again["personal_read_receipt"]["complete"])
        self.assertEqual("reuse_same_scope", again["personal_read_receipt"]["status"])
        visual = self.resolve(known_version=full["version"], materials="visual")
        self.assertEqual("available", visual["status"])
        self.assertNotIn("personal_context", visual["records"][0])

    def test_protected_never_reads_personal_object(self):
        self.author()
        original = context._read_content_object
        def guarded(state, ref, *args, **kwargs):
            self.assertNotIn("reviewed-knowledge-profiles", str(ref))
            self.assertNotIn("knowledge-subpoint-records", str(ref))
            return original(state, ref, *args, **kwargs)
        with mock.patch.object(context, "_read_content_object", side_effect=guarded):
            result = self.resolve("protected", materials="core")
        text = json.dumps(result, ensure_ascii=False)
        for phrase in ("第三条未验证", "最后一个必要条件", "SIBLING_PRIVATE_VALUE_97"):
            self.assertNotIn(phrase, text)
        self.assertEqual("withheld", result["personal_read_receipt"]["status"])

    def test_selected_subpoint_does_not_expand_to_parent_or_siblings(self):
        self.author()
        result = self.resolve(materials="core", query={"mechanism_terms": ["当前字段"]})
        self.assertEqual("matched_subpoints_only", result["scope"])
        personal = result["records"][0]["personal_context"]
        self.assertEqual(self.target, personal["identity"])
        self.assertEqual(self.update["subpoints"][0]["evidence"], personal["evidence"])
        text = json.dumps(result, ensure_ascii=False)
        self.assertNotIn("完整概况：", text)
        self.assertNotIn("SIBLING_PRIVATE_VALUE_97", text)

    def test_independent_part_uses_its_bound_facet_and_rejects_other_facets(self):
        current = "EXAM408-2020-Q41-P01"
        mapping = self.repo / "support/syllabus-2027/question-syllabus-map.json"
        mapping.parent.mkdir(parents=True)
        mapping.write_text(json.dumps({"schema": "cs408-question-syllabus-map-v1",
            "subquestions": {current: {"review_status": "reviewed", "assessed_points": ["OS03-17"],
                                        "syllabus_ids": []}}}))
        self.author()
        query = {"current_question_id": current}
        sibling = self.update["subpoints"][1]["subpoint_id"]
        with mock.patch("exam_year_teaching_408.verified_personal_focus", return_value=(self.target,)) as bound:
            selected = self.resolve(query=query, personal_scope="teaching", materials="core")
            self.assertEqual([self.target], selected["requested_subpoints"])
            self.assertEqual("bounded", selected["personal_read_receipt"]["records"][0]["status"])
            self.assertEqual("verified", selected["question_scope"]["personal_subpoint_binding"])
            unknown_words = self.resolve(query={**query, "mechanism_terms": ["尚未登记的措辞"]},
                                         personal_scope="teaching", materials="core")
            self.assertEqual([self.target], unknown_words["requested_subpoints"])
            self.assertIn("尚未登记的措辞", unknown_words["missing_points"])
            wrong = self.resolve(query={**query, "mechanism_terms": [sibling]},
                                 known_version=selected["version"], materials="core")
            self.assertIn(sibling.lower(), wrong["missing_points"])
            self.assertEqual("withheld_scope", wrong["personal_read_receipt"]["records"][0]["status"])
            self.assertNotIn("SIBLING_PRIVATE_VALUE_97", json.dumps(wrong))
            bound.reset_mock()
            protected = self.resolve("protected", query=query, personal_scope="teaching", materials="core")
            bound.assert_not_called()
            self.assertNotIn(self.target, json.dumps(protected))
            bound.return_value = ()
            stale_binding = self.resolve(query=query, known_version=selected["version"],
                                         personal_scope="teaching", materials="core")
            self.assertNotEqual("unchanged", stale_binding["status"])
            self.assertEqual("withheld_scope", stale_binding["personal_read_receipt"]["records"][0]["status"])

    def test_independent_part_withholds_unscoped_parent_even_for_summary_and_direct(self):
        self.author()
        # A legacy parent overview could mix siblings without an explicit ID.
        # Protect it by scope, not by matching a particular sentinel string.
        for view in ("after_attempt", "direct"):
            for scope in ("summary", "full"):
                with self.subTest(view=view, scope=scope):
                    with mock.patch.object(context, "_read_content_object", wraps=context._read_content_object) as reads:
                        result = self.resolve(view, materials="all", personal_scope=scope,
                            query={"current_question_id": "EXAM408-2020-Q41-P01"})
                    row = result["records"][0]
                    self.assertEqual("unscoped_parent_personal_history", row["reason"])
                    self.assertFalse(result["personal_read_receipt"]["complete"])
                    text = json.dumps(result, ensure_ascii=False)
                    for phrase in ("完整概况：", "第三条未验证", "最后一个必要条件", "SIBLING_PRIVATE_VALUE_97"):
                        self.assertNotIn(phrase, text)
                    self.assertFalse(any("reviewed-knowledge-profiles" in str(call.args[1])
                                         for call in reads.call_args_list))
                    self.assertTrue(row["personal_context"]["profile_object"])

    def test_missing_or_tampered_full_object_is_visible_and_not_partial_success(self):
        self.author()
        result = self.resolve(materials="core")
        ref = result["records"][0]["personal_context"]["object_ref"]
        (self.state / ref["ref"]).write_text("{}")
        broken = self.resolve(materials="core")
        self.assertFalse(broken["personal_read_receipt"]["complete"])
        self.assertEqual("incomplete_requires_evidence_read", broken["personal_context_status"])
        self.assertNotIn("short_summary", broken["records"][0])

    def test_core_skips_large_current_question_visual_read(self):
        self.author()
        with mock.patch.object(context, "read_question_visual", side_effect=AssertionError("visual read")):
            result = self.resolve(materials="core")
        self.assertTrue(result["personal_read_receipt"]["complete"])

    def test_bounded_teaching_replays_correction_with_conditions_and_prompt_dependency(self):
        self.author()
        code = "OS03-17"
        old = profiles.load_profiles(self.repo)["profiles"][code]
        ledger = self.repo / context.REVIEW_LEDGER_REL
        ledger_sha = hashlib.sha256(ledger.read_bytes()).hexdigest()

        def source(name, texts):
            path = self.repo / (name + ".json")
            turns = [{"ordinal": i, "role": role, "kind": "answer" if role == "user" else "explanation",
                      "text": text} for i, (role, text) in enumerate(texts, 1)]
            path.write_text(json.dumps(dict(schema_version="cs408-full-conversation-v1",
                complete_order_preserved=True, truncated=False, turn_count=len(turns), turns=turns), ensure_ascii=False))
            return {"id": name, "path": str(path), "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
                    "review_state": "reviewed", "turns": [{"ordinal": row["ordinal"], "role": row["role"],
                                                          "quote": row["text"]} for row in turns]}

        early = source("SYNTH-408-01-T1", [("user", "TLB中没有就说明页不在内存")])
        prompted = source("SYNTH-408-01-PROMPT", [("assistant", "先确认页表项中的页框号"),
                                                 ("user", "提示后用页框号拼接偏移")])
        later = source("SYNTH-408-01-T2", [("user", "页表表明页面驻留，快表未命中不等于缺页")])

        def claim(anchor, state, text, ref, day):
            return {"claim_id": profiles.claim_key(code, anchor), "scope": code, "anchor": anchor,
                    "kind": "observation", "basis": "observed_attempt", "evidence_status": state,
                    "content": text, "event_at": day, "lifecycle": "current", "source_refs": [ref]}

        first = claim("initial", "explicit_gap", "旧误解：把TLB未命中直接当缺页。", early, "2026-08-26")
        repair = claim("prompted-offset", "prompted_repair", "查页框号的条件由提示给出；之后能拼接页内偏移。",
                       prompted, "2026-08-27")
        new = claim("independent-correction", "independent_demonstrated",
                    "页表项表明页面驻留且访问合法时，TLB未命中仍可完成转换；这一情境独立区分了缺页。",
                    later, "2026-08-28")
        # Another dated, long statement is eligible history but must not use
        # the space needed for the correction and prompt dependency.
        older = claim("large-context", "explicit_gap", "早先另一情境的说明。" * 620, early, "2026-08-25")
        bindings = {**{ref["path"]: ref["sha256"] for ref in self.update["source_refs"]},
                    **{ref["path"]: ref["sha256"] for ref in (early, prompted, later)}}
        t1 = {**self.update, "expected_previous_version": old["version"], "claims_schema": profiles.CLAIMS_SCHEMA,
              "claims": [older, first, repair], "next_check": "给驻留位并请先说明要检查的对象。"}
        receipt = profiles.apply_updates(self.repo, [t1], "SYNTH-408-01-T1", evidence_bindings=bindings)
        self.refresh()
        opts = {"materials": "core", "personal_scope": "teaching", "max_bytes": 4500}
        t1_view = self.resolve(**opts)
        self.assertIn(first["content"], json.dumps(t1_view, ensure_ascii=False))
        self.assertNotIn(new["content"], json.dumps(t1_view, ensure_ascii=False))
        self.assertEqual("unchanged", self.resolve(known_version=t1_view["version"], **opts)["status"])

        revised = {**first, "lifecycle": "superseded", "change_reason": "另有无提示作答显示条件内可以区分。"}
        t2 = {**t1, "expected_previous_version": receipt["profiles"][code]["sha256"],
              "claims": [older, revised, repair, {**new, "supersedes": [first["claim_id"]]}]}
        profiles.apply_updates(self.repo, [t2], "SYNTH-408-01-T2", evidence_bindings=bindings)
        self.assertEqual("already_applied", profiles.apply_updates(self.repo, [t2], "SYNTH-408-01-T2",
            evidence_bindings=bindings)["status"])
        with self.assertRaisesRegex(profiles.ProfileError, "profile_operation_content_conflict"):
            profiles.apply_updates(self.repo, [{**t2, "next_check": "冲突的重复输入"}], "SYNTH-408-01-T2",
                                   evidence_bindings=bindings)
        # The retry occurred with the projection still stale. Rebuilding the
        # derived layer preserves the writer receipt and the learning ledger.
        self.assertEqual("unavailable", self.resolve(known_version=t1_view["version"], **opts)["status"])
        self.refresh()
        result = self.resolve(known_version=t1_view["version"], **opts)
        memory = result["records"][0]["personal_context"]
        view = memory["claim_view"]
        self.assertEqual([new["content"], repair["content"]], [row["content"] for row in view["claims"]])
        self.assertEqual(1, view["historical_claim_count"])
        self.assertEqual(1, view["omitted_claim_count"])
        self.assertNotIn(first["content"], json.dumps(result, ensure_ascii=False))
        self.assertNotIn(later["path"], json.dumps(result, ensure_ascii=False))
        self.assertLessEqual(result["serialized_bytes"], 4500)
        self.assertEqual(len(context._canonical_bytes(result)), result["serialized_bytes"])
        self.assertFalse(result["personal_read_receipt"]["complete"])
        self.assertEqual(ledger_sha, hashlib.sha256(ledger.read_bytes()).hexdigest())
        self.assertEqual("available", self.resolve(known_version=result["version"], **{**opts, "max_bytes": 8192})["status"])

        hidden = self.resolve("protected", **opts)
        self.assertNotIn("claim_view", json.dumps(hidden))
        unavailable = self.resolve(query={"current_question_id": "EXAM408-2020-Q41-P01"}, **opts)
        self.assertEqual("withheld_scope", unavailable["personal_read_receipt"]["records"][0]["status"])

        same_day = claim("prompted-other-condition", "prompted_repair",
                         "仅在提示给出页框号且页面驻留时拼接偏移，访问非法时不可套用。",
                         prompted, new["event_at"])
        undated = [
            claim("undated-scope", "independent_demonstrated", "仅独立说明了驻留这一前提。", later, None),
            claim("undated-boundary", "explicit_gap", "越权访问情境仍需另证。", early, None),
        ]
        frozen = [*t2["claims"], same_day, *undated]
        expected = {row["claim_id"]: row for row in frozen}
        observed = []
        for turn, order in enumerate((frozen, list(reversed(frozen)), frozen[2:] + frozen[:2]), 1):
            previous = profiles.load_profiles(self.repo)["profiles"][code]["version"]
            update = {**t2, "expected_previous_version": previous, "claims": order}
            committed = profiles.apply_updates(self.repo, [update], f"SYNTH-408-ORDER-{turn}",
                                                evidence_bindings=bindings)
            current = profiles.load_profile_version(self.repo, code, committed["profiles"][code]["sha256"])
            self.assertEqual(set(expected), {row["claim_id"] for row in current["claims"]})
            for row in current["claims"]:
                original = expected[row["claim_id"]]
                for key in ("scope", "event_at", "lifecycle", "supersedes"):
                    self.assertEqual(original.get(key) or None, row.get(key) or None)
                self.assertEqual([ref["sha256"] for ref in original["source_refs"]],
                                 [ref["sha256"] for ref in row["source_refs"]])
            self.refresh()
            bounded = self.resolve(**opts)
            self.assertLessEqual(bounded["serialized_bytes"], opts["max_bytes"])
            selected = bounded["records"][0]["personal_context"]
            self.assertEqual(code, selected["identity"])
            self.assertNotIn("short_summary", selected)
            claims = selected["claim_view"]
            self.assertGreater(claims["omitted_claim_count"], 0)
            selected_ids = {row["claim_id"] for row in claims["claims"]}
            self.assertTrue(selected_ids & {new["claim_id"], same_day["claim_id"]})
            for row in claims["claims"]:
                original = expected[row["claim_id"]]
                for key in ("basis", "evidence_status", "content"):
                    self.assertEqual(original[key], row[key])
            observed.append(claims)
        self.assertEqual(observed[0], observed[1])
        self.assertEqual(observed[0], observed[2])
        self.assertEqual(ledger_sha, hashlib.sha256(ledger.read_bytes()).hexdigest())

        Path(later["path"]).write_text("已变化")
        invalidated = self.resolve(known_version=bounded["version"], **opts)
        self.assertNotEqual(bounded["version"], invalidated["version"])
        self.assertEqual("semantic_stale", invalidated["records"][0]["status"])
        stale = self.resolve(**opts, query={"knowledge_points": [code, "CO99-99"]})
        self.assertEqual("semantic_stale", stale["records"][0]["status"])
        self.assertIn("CO99-99", stale["missing_points"])
        self.assertNotIn(new["content"], json.dumps(stale, ensure_ascii=False))

    def test_bounded_teaching_marks_omitted_subpoints(self):
        self.author()
        result = self.resolve(personal_scope="teaching", materials="core", max_bytes=1800,
            query={"mechanism_terms": ["当前字段", "兄弟机制"]})
        self.assertEqual(len(context._canonical_bytes(result)), result["serialized_bytes"])
        self.assertLessEqual(result["serialized_bytes"], 1800)
        self.assertTrue(result["omitted_subpoints"])
        self.assertFalse(result["personal_read_receipt"]["complete"])

    def test_evidence_only_snapshot_matches_complete_build_without_teaching_work(self):
        self.author()
        complete = context._compute_snapshot(self.repo, "2026-08-29")
        with mock.patch.object(context, "build_artifacts", side_effect=AssertionError("duplicate artifact build")):
            facts = context.compute_evidence_snapshot(self.repo, "2026-08-29")
        for code, point in facts["points"].items():
            reference = complete["point_artifacts"]["index"]["point_records"][code]
            self.assertEqual(reference["sha256"], point["sha256"])
            self.assertEqual(complete["point_artifacts"]["objects"][reference["ref"]], point["records"])
        self.assertEqual(0, facts["teaching_artifact_build_count"])

    def test_refresh_preserves_old_derived_generation_by_default(self):
        self.refresh()
        with mock.patch.object(context, "_prune_unreachable_artifacts", side_effect=AssertionError("unexpected GC")):
            result = context.refresh_nightly(self.repo, "2026-08-29", output_root=self.state)
        self.assertTrue(result["pruned_artifacts"]["history_preserved"])

    def test_same_batch_reuses_only_unchanged_source_facts(self):
        self.author()
        context.compute_evidence_snapshot(self.repo, "2026-08-29")
        pointer = self.repo / profiles.CURRENT_REL
        pointer.write_bytes(pointer.read_bytes())
        with mock.patch.object(context, "_compute_snapshot_facts", side_effect=AssertionError("duplicate fact scan")):
            reused = context._compute_snapshot(self.repo, "2026-08-29")
        self.assertTrue(reused["facts_reused"])
        with mock.patch.object(context, "_compute_snapshot_facts", wraps=context._compute_snapshot_facts) as fresh:
            card = self.repo / "OS_2020_001.md"
            card.write_text(card.read_text() + "\n合法来源补充\n")
            changed = context._compute_snapshot(self.repo, "2026-08-29")
        self.assertFalse(changed["facts_reused"])
        fresh.assert_called_once()

    def test_new_source_inventory_prevents_fact_cache_reuse(self):
        self.author()
        context.compute_evidence_snapshot(self.repo, "2026-08-29")
        source = self.repo / "复盘输出/新来源.md"
        source.parent.mkdir(parents=True, exist_ok=True)
        source.write_text("# 补充来源\n")
        with mock.patch.object(context, "_compute_snapshot_facts", wraps=context._compute_snapshot_facts) as fresh:
            changed = context._compute_snapshot(self.repo, "2026-08-29")
        self.assertFalse(changed["facts_reused"])
        fresh.assert_called_once()


if __name__ == "__main__":
    unittest.main()
