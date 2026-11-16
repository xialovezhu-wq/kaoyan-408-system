"""Per-claim provenance and lifecycle cannot promote inferred learning facts."""
import copy
import hashlib
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import knowledge_profile_408 as profile


class KnowledgeClaimsTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / "conversation.json"
        self.turns = [
            dict(ordinal=1, role="user", kind="answer", text="抢占后仍然可以运行，只是等待CPU。"),
            dict(ordinal=2, role="assistant", kind="explanation", text="请区分失去CPU与等待事件。"),
            dict(ordinal=3, role="user", kind="answer", text="提示后我能区分就绪与阻塞。"),
            dict(ordinal=4, role="user", kind="utterance", text="这种题先给我状态图。"),
        ]
        self.path.write_text(json.dumps(dict(schema_version="cs408-full-conversation-v1",
            complete_order_preserved=True, truncated=False, turn_count=4, turns=self.turns), ensure_ascii=False))
        self.sha = hashlib.sha256(self.path.read_bytes()).hexdigest()
        self.bindings = {str(self.path): self.sha}
        self.scope = "OS02-05"
        self.row = dict(claim_id=profile.claim_key(self.scope, "preemption"), anchor="preemption",
            scope=self.scope, kind="observation", basis="observed_attempt",
            evidence_status="independent_demonstrated", lifecycle="current",
            content="仅抢占条件下给出了独立理由。", event_at="2026-11-05", supports=["progress:0"],
            source_refs=[self.ref([1])])
        self.value = dict(claims_schema=profile.CLAIMS_SCHEMA, claims=[self.row],
                          progress=["抢占条件的理由"], overview="仅本条件。")

    def ref(self, ordinals):
        return dict(id="source", path=str(self.path), sha256=self.sha, review_state="reviewed",
                    turns=[dict(ordinal=i, role=self.turns[i-1]["role"], quote=self.turns[i-1]["text"])
                           for i in ordinals])

    def test_verified_claim_preserves_whole_quote_and_narrow_scope(self):
        profile.validate_claims(self.scope, self.value, self.bindings)
        profile._stamp_claims(self.value, "2026-11-06T01:00:00+00:00")
        result = profile.claims_view(self.value)
        self.assertEqual(self.turns[0]["text"], result["claims"][0]["source_refs"][0]["turns"][0]["quote"])
        self.assertEqual(self.scope, result["claims"][0]["scope"])
        self.assertFalse(result["independent_mastery_inferred"])

    def test_assistant_text_cannot_be_personal_observation(self):
        self.row["source_refs"] = [self.ref([2])]
        with self.assertRaisesRegex(profile.ProfileError, "user_evidence"):
            profile.validate_claims(self.scope, self.value, self.bindings)

    def test_inference_cannot_claim_independent_mastery(self):
        self.row["basis"] = "reviewed_inference"
        with self.assertRaises(profile.ProfileError):
            profile.validate_claims(self.scope, self.value, self.bindings)
        self.row["evidence_status"] = "unverified"
        profile.validate_claims(self.scope, self.value, self.bindings)

    def test_unverified_inference_cannot_supersede_current_observation(self):
        previous = copy.deepcopy(self.value)
        observed_id = self.row["claim_id"]
        retired = {**self.row, "lifecycle": "superseded", "change_reason": "另一种解释待核"}
        anchor = "tentative-reading"
        inference = {**self.row, "claim_id": profile.claim_key(self.scope, anchor), "anchor": anchor,
                     "kind": "assessment", "basis": "reviewed_inference", "evidence_status": "unverified",
                     "content": "另一种解释待核", "source_refs": [self.ref([2, 3])]}
        submitted = {**self.value, "claims": [retired, {**inference, "supersedes": [observed_id]}]}
        with self.assertRaisesRegex(profile.ProfileError, "claim_observed_supersession_requires_confirmed_evidence"):
            profile.validate_claims(self.scope, submitted, self.bindings, previous)

        conflicted = {**inference, "lifecycle": "conflicted", "change_reason": "本条件需核实"}
        retained = {**self.value, "claims": [self.row, conflicted]}
        profile.validate_claims(self.scope, retained, self.bindings, previous)

    def test_reported_preference_is_scoped_and_cites_user(self):
        self.row.update(kind="preference", basis="explicit_statement", evidence_status="user_reported",
                        source_refs=[self.ref([4])], content="这种状态题先展示状态图。")
        profile.validate_claims(self.scope, self.value, self.bindings)
        self.row["scope"] = "all_subjects"
        with self.assertRaisesRegex(profile.ProfileError, "scope_mismatch"):
            profile.validate_claims(self.scope, self.value, self.bindings)

    def test_prompted_repair_requires_real_ordered_turns(self):
        self.row.update(evidence_status="prompted_repair", source_refs=[self.ref([1])])
        with self.assertRaisesRegex(profile.ProfileError, "ordered_response"):
            profile.validate_claims(self.scope, self.value, self.bindings)
        self.row["source_refs"] = [self.ref([2, 3])]
        profile.validate_claims(self.scope, self.value, self.bindings)

    def test_repeated_revision_keeps_identity_and_prior_hash(self):
        profile._stamp_claims(self.value, "2026-11-06T01:00:00+00:00")
        previous = copy.deepcopy(self.value)
        self.row.update(content="更正：仅这个抢占场景有证据，其他转换尚未验证。", change_reason="限定适用范围")
        profile.validate_claims(self.scope, self.value, self.bindings, previous)
        profile._stamp_claims(self.value, "2026-11-06T02:00:00+00:00", previous)
        self.assertEqual(previous["claims"][0]["claim_id"], self.row["claim_id"])
        self.assertEqual(previous["claims"][0]["claim_sha256"], self.row["previous_claim_sha256"])
        previous = copy.deepcopy(self.value)
        self.row.update(lifecycle="retracted", change_reason="用户撤回这项判断")
        profile.validate_claims(self.scope, self.value, self.bindings, previous)
        profile._stamp_claims(self.value, "2026-11-06T03:00:00+00:00", previous)
        self.assertEqual(previous["claims"][0]["claim_sha256"], self.row["previous_claim_sha256"])
        self.assertEqual("retracted", profile.claims_view(self.value)["claims"][0]["lifecycle"])

    def test_cannot_silently_remove_claim_or_forge_quote(self):
        with self.assertRaisesRegex(profile.ProfileError, "cannot_disappear"):
            profile.validate_claims(self.scope, dict(claims_schema=profile.CLAIMS_SCHEMA, claims=[]),
                                    self.bindings, self.value)
        self.row["source_refs"][0]["turns"][0]["quote"] = "我已经掌握全部操作系统"
        with self.assertRaisesRegex(profile.ProfileError, "quote_not_in_original"):
            profile.validate_claims(self.scope, self.value, self.bindings)

    def test_legacy_prose_is_not_fabricated_into_claim_and_protected_is_empty(self):
        self.assertEqual("legacy_unstructured", profile.claims_view({"overview": "历史判断"})["status"])
        protected = profile.claims_view(self.value, view="protected")
        self.assertEqual([], protected["claims"])
        self.assertNotIn(self.row["content"], json.dumps(protected, ensure_ascii=False))

    def test_unchanged_claim_keeps_recorded_time_and_source_drift_invalidates(self):
        profile._stamp_claims(self.value, "2026-11-06T01:00:00+00:00")
        previous = copy.deepcopy(self.value)
        profile._stamp_claims(self.value, "2026-11-06T02:00:00+00:00", previous)
        self.assertEqual(previous["claims"], self.value["claims"])
        self.assertTrue(profile.profile_teaching_source_state(self.value)["current"])
        self.path.write_text("changed source")
        self.assertFalse(profile.profile_teaching_source_state(self.value)["current"])

    def test_native_writer_stamps_claims_once_without_learning_writes(self):
        repo = Path(self.tmp.name) / "repo"
        repo.mkdir()
        (repo / "知识点标签表.md").write_text("- OS02-05 进程状态转换\n")
        update = copy.deepcopy(self.value)
        update.update(concept_key=self.scope, expected_previous_version=None,
            short_summary="抢占条件有独立依据，其他条件待检验。", next_check="检查等待事件条件。",
            timeline=[], aliases=[], question_refs=[],
            source_refs=[dict(path=str(self.path), sha256=self.sha)])
        records = {"schema": "knowledge_learning_records_v1", "knowledge_point": self.scope,
                   "records": {"fixture-event": {"record_key": "fixture-event", "summary": "真实fixture首答"}}}
        snapshot = {"points": {self.scope: {"records": records, "sha256": "a" * 64, "record_count": 1}}}
        with mock.patch("exam_tutoring_context_408.compute_evidence_snapshot", return_value=snapshot) as compute:
            first = profile.apply_updates(repo, [update], "claim-fixture", evidence_bindings=self.bindings)
            stored = profile.load_profiles(repo)["profiles"][self.scope]
            second = profile.apply_updates(repo, [update], "claim-fixture", evidence_bindings=self.bindings)
        self.assertEqual(1, compute.call_count)
        self.assertEqual("already_applied", second["status"])
        self.assertEqual(first["generation"], second["generation"])
        self.assertEqual(0, first["formal_question_write_count"])
        self.assertEqual(0, first["fsrs_write_count"])
        self.assertEqual(stored["accepted_at"], stored["claims"][0]["recorded_at"])
        self.assertEqual("available", profile.claims_view(stored)["status"])

    def test_pending_capture_cannot_prove_independent_performance(self):
        self.row["source_refs"][0]["review_state"] = "pending_capture_teaching_only"
        with self.assertRaisesRegex(profile.ProfileError, "reviewed_observation"):
            profile.validate_claims(self.scope, self.value, self.bindings)


if __name__ == "__main__":
    unittest.main()
