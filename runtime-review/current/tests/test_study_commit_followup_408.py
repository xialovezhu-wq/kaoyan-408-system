from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
sys.path.insert(0, str(ROOT / "tests"))

import exam_tutoring_context_408 as context
import study_commit_followup_408 as followup
from intake_batch_apply_408 import BatchResult
import test_exam_tutoring_context_408 as context_fixtures


class StudyCommitFollowup408Tests(unittest.TestCase):
    def setUp(self):
        fixture = context_fixtures.ExamTutoringContext408Tests()
        fixture.setUp()
        self.addCleanup(fixture.temporary.cleanup)
        self.fixture = fixture
        self.repo = fixture.repo
        self.activation = mock.patch.object(followup, "CANONICAL_REPO", self.repo)
        self.activation.start()
        self.addCleanup(self.activation.stop)

    def append_event(self):
        event = {
            "schema": "review_event_v1", "event_id": "RE-new-verified",
            "event_kind": "outcome", "event_time": "2026-08-29T08:00:00+08:00",
            "observed_date": "2026-08-29", "source": "daily_practice",
            "source_id": "SRC-A", "formal_node_id": "OS_2020_001",
            "first_result": "independent_correct", "confidence": "high",
            "prompt_level": "none", "hint_used": False, "first_break": "",
            "first_break_provenance": "not_observed", "formal_write_authorized": False,
        }
        path = self.repo / context.REVIEW_LEDGER_REL
        with path.open("a") as handle:
            handle.write(json.dumps(event) + "\n")
        followup.note_ledger_commit(path, event)
        return event

    def test_nested_owner_finishes_after_outer_lock_and_deduplicates_event(self):
        held = []
        calls = []
        @followup.study_commit_boundary
        def inner(repo):
            return {"event": {"event_id": "RE-one"}, "verification": {"status": "verified"}}
        @followup.study_commit_boundary
        def outer(repo):
            held.append(True)
            try:
                inner(repo)
                inner(repo)
            finally:
                held.pop()
            return {"status": "saved"}
        def finish(repo, event_id):
            self.assertEqual([], held)
            calls.append(event_id)
            return {"event_id": event_id, "publication": {"status": "STARTED"}}
        with mock.patch.object(followup, "complete_after_commit", side_effect=finish):
            result = outer(self.repo)
        self.assertEqual(["RE-one"], calls)
        self.assertEqual("saved", result["status"])
        self.assertEqual("RE-one", result["study_followup"]["event_id"])

    def test_formal_default_repo_and_cache_io_failure_preserve_committed_result(self):
        @followup.study_commit_boundary
        def commit(*, repo_root=self.repo):
            return BatchResult(status="COMMITTED", idempotency_key="formal-key",
                               transaction_id="TX-one", items=[])
        with mock.patch.object(context, "invalidate_after_commit", side_effect=OSError("cache unavailable")), \
             mock.patch.object(followup, "complete_after_commit", return_value={"publication": {"status": "PENDING"}}) as finish:
            result = commit()
        self.assertEqual("COMMITTED", result.status)
        self.assertEqual("PENDING", result.as_dict()["study_followup"]["publication"]["status"])
        finish.assert_called_once()

    def test_verified_commit_refreshes_next_query_and_cloud_failure_does_not_replay(self):
        context.refresh_nightly(self.repo, "2026-08-29")
        state = self.repo / context.DEFAULT_STATE_REL
        before = json.loads((state / "manifest.json").read_text())
        event = self.append_event()
        ledger = self.repo / context.REVIEW_LEDGER_REL
        raw = ledger.read_bytes()
        delivery = [{"status": "PENDING", "reason": "publication_request_needs_recovery"},
                    {"status": "PUBLISHED"}]
        with mock.patch.object(followup, "_request_publication", side_effect=delivery) as publisher:
            first = followup.complete_after_commit(self.repo, event["event_id"], publish=True)
            second = followup.complete_after_commit(self.repo, event["event_id"], publish=True)
        result = context.query_context(self.repo, self.fixture.query_payload(formal_id="OS_2020_001"))
        self.assertEqual("ready", result["status"])
        after = json.loads((state / "manifest.json").read_text())
        exact = json.loads((state / after["index_ref"]).read_text())
        full_profile = json.loads((state / exact["profiles"]["OS_2020_001"]["profile_ref"]).read_text())
        self.assertEqual(3, full_profile["history"]["direct"]["event_count"])
        self.assertIn(event["event_id"], json.dumps(full_profile))
        teaching = context.query_context(self.repo,
            self.fixture.query_payload(knowledge_points=["OS03-17"], max_profiles=0),
            teaching_view="after_attempt", max_bytes=4500)
        self.assertEqual("available", teaching["status"])
        self.assertEqual(after["source_version"], teaching["source_version"])
        self.assertEqual("OS03-17", teaching["records"][0]["knowledge_point"])
        self.assertNotEqual(before["point_index_sha256"], after["point_index_sha256"])
        index = json.loads((state / after["point_index_ref"]).read_text())
        added = []
        for code in ("OS03-17", "OS03-14"):
            slice_records = json.loads((state / index["point_records"][code]["ref"]).read_text())["records"]
            added.extend(r for r in slice_records.values() if r["source_id"] == event["event_id"])
            prepared = index["points"][code]
            self.assertEqual({"protected", "after_attempt", "direct"}, set(prepared["teaching_views"]))
            page = self.repo / prepared["learning_path"]["relpath"]
            for record in slice_records.values():
                if record["source_id"] == event["event_id"]:
                    self.assertIn(record["record_key"], page.read_text())
        self.assertEqual({r["knowledge_point"] for r in added}, {"OS03-17", "OS03-14"})
        self.assertTrue(result["knowledge_coverage"]["all_requested_points_queried"])
        self.assertTrue(any(s.get("latest_question_associated", {}).get("record_key") in
                            {r["record_key"] for r in added} for s in result["knowledge_summaries"]))
        self.assertEqual("already_current", second["local_tutoring"]["status"])
        self.assertEqual("refreshed", first["local_tutoring"]["status"])
        self.assertEqual(delivery[0], first["publication"])
        self.assertEqual(delivery[1], second["publication"])
        self.assertEqual(first["publication_event_id"], second["publication_event_id"])
        self.assertEqual([mock.call(self.repo, first["publication_event_id"])] * 2, publisher.call_args_list)
        self.assertEqual(raw, ledger.read_bytes())

    def test_refresh_failure_keeps_old_cache_disabled_and_real_ledger_unchanged(self):
        context.refresh_nightly(self.repo, "2026-08-29")
        event = self.append_event()
        ledger = self.repo / context.REVIEW_LEDGER_REL
        raw = ledger.read_bytes()
        with mock.patch.object(context, "refresh_after_commit", side_effect=context.ExamTutoringContextError("source drift")), \
             mock.patch.object(followup, "_request_publication", return_value={"status": "PENDING"}):
            result = followup.complete_after_commit(self.repo, event["event_id"])
        self.assertEqual("PENDING", result["local_tutoring"]["status"])
        query = context.query_context(self.repo, self.fixture.query_payload(formal_id="OS_2020_001"))
        self.assertEqual("unavailable", query["status"])
        self.assertEqual([], query["profiles"])
        self.assertEqual(raw, ledger.read_bytes())

    def test_same_business_event_gets_distinct_publication_after_new_source_version(self):
        context.refresh_nightly(self.repo, "2026-08-29")
        with mock.patch.object(followup, "_request_publication", return_value={"status": "PUBLISHED"}) as publish:
            first = followup.complete_after_commit(self.repo, "formal-same-business-event", publish=True)
            self.append_event()
            second = followup.complete_after_commit(self.repo, "formal-same-business-event", publish=True)
            third = followup.complete_after_commit(self.repo, "formal-same-business-event", publish=True)
        self.assertEqual(first["event_id"], second["event_id"])
        self.assertLessEqual(len(second["publication_event_id"]), 200)
        self.assertNotEqual(first["publication_event_id"], second["publication_event_id"])
        self.assertEqual(second["publication_event_id"], third["publication_event_id"])
        self.assertEqual(publish.call_args_list[1].args[1], second["publication_event_id"])

    def test_refresh_failure_never_requests_or_claims_successful_personal_publication(self):
        with mock.patch.object(context, "refresh_after_commit", side_effect=OSError("refresh unavailable")), \
             mock.patch.object(followup, "_request_publication", return_value={"status": "PUBLISHED"}) as publish:
            result = followup.complete_after_commit(self.repo, "verified-business-event", publish=True)
        self.assertEqual("PENDING", result["publication"]["status"])
        self.assertIsNone(result["publication_event_id"])
        publish.assert_not_called()

    def test_default_refresh_never_requests_publication(self):
        context.refresh_nightly(self.repo, "2026-08-29")
        with mock.patch.object(followup, "_request_publication", side_effect=AssertionError("unexpected publication")) as publish:
            result = followup.complete_after_commit(self.repo, "verified-business-event")
        self.assertEqual("already_current", result["local_tutoring"]["status"])
        self.assertEqual("NOT_REQUESTED", result["publication"]["status"])
        publish.assert_not_called()

    def test_nonproduction_fixture_never_requests_publication(self):
        with tempfile.TemporaryDirectory() as directory, \
             mock.patch.object(followup, "_request_publication") as publisher:
            result = followup.complete_after_commit(Path(directory), "RE-fixture")
        self.assertEqual("NOT_PRODUCTION_REPO", result["status"])
        publisher.assert_not_called()

    def test_failed_outer_owner_does_not_publish_unconfirmed_partial_operation(self):
        @followup.study_commit_boundary
        def outer(repo):
            inner(repo)
            raise RuntimeError("receipt failed")
        @followup.study_commit_boundary
        def inner(repo):
            return {"event": {"event_id": "RE-one"}, "verification": {"status": "verified"}}
        with mock.patch.object(followup, "complete_after_commit") as finish:
            with self.assertRaisesRegex(RuntimeError, "receipt failed"):
                outer(self.repo)
        finish.assert_not_called()
        self.assertIsNone(followup._pending.get())


if __name__ == "__main__":
    unittest.main()
