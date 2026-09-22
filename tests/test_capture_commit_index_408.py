from __future__ import annotations

import hashlib
import json
import sys
import tempfile
import unittest
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = PROJECT_ROOT / "scripts"
sys.path.insert(0, str(SCRIPTS))

import capture_commit_index_408 as commit_index  # noqa: E402
import intake_fact_capture_408 as capture  # noqa: E402


class CaptureCommitIndexTests(unittest.TestCase):
    def _payload(
        self, *, key: str, subject: str, authorized: bool
    ) -> dict[str, object]:
        return {
            "schema": capture.SCHEMA,
            "study_date": "2026-07-23",
            "timezone": "Asia/Shanghai",
            "idempotency_key": key,
            "stable_evidence_refs": [
                {
                    "kind": "details_card",
                    "locator": (
                        "obsidian://open?vault=408-details&file="
                        + key.replace(":", "%3A")
                    ),
                    "sha256": hashlib.sha256(key.encode("utf-8")).hexdigest(),
                }
            ],
            "source_facts": {
                "year": "2021",
                "subject": subject,
                "question_type": "单项选择题",
                "source_id": "VISUAL_PENDING",
            },
            "user_facts": {
                "user_error_entry": "未观察到",
                "user_error_provenance": "not_observed",
            },
            "identity_hint": {
                "status": "unknown",
                "mode": "unknown",
                "basis": "尚未执行正式库查重",
            },
            "answer_safe_context_anchor": "只保留机制与状态转换骨架",
            "missing_fields": ["formal_identity", "main_knowledge"],
            "formalization_authorized": authorized,
        }

    def _capture_payload(
        self, root: Path, payload: dict[str, object], filename: str
    ) -> dict[str, object]:
        payload_path = root / filename
        payload_path.write_text(
            json.dumps(payload, ensure_ascii=False), encoding="utf-8"
        )
        return capture.capture(payload_path, repo_root=root)

    def _fixture(
        self, root: Path
    ) -> tuple[Path, Path, dict[str, object], dict[str, object]]:
        first = self._capture_payload(
            root,
            self._payload(
                key="capture:commit-index:first",
                subject="OS",
                authorized=False,
            ),
            "first.json",
        )
        capture_root = capture.capture_root(root)
        ledger = capture_root / "events.jsonl"
        state = capture_root / "state.json"
        events = [
            json.loads(line)
            for line in ledger.read_text(encoding="utf-8").splitlines()
            if line.strip()
        ]
        key = "authorize:commit-index:first"
        events.append(
            {
                "schema": capture.EVENT_SCHEMA,
                "event_type": "capture_authorized",
                "event_id": "CE-"
                + hashlib.sha256(
                    ("authorize:" + key).encode("utf-8")
                ).hexdigest()[:20],
                "idempotency_key": key,
                "created_at": "2026-07-23T02:00:00+00:00",
                "capture_id": first["capture_id"],
            }
        )
        ledger.write_text(
            "".join(
                json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n"
                for row in events
            ),
            encoding="utf-8",
        )
        state.write_text(
            json.dumps(
                capture.replay(events),
                ensure_ascii=False,
                indent=2,
                sort_keys=True,
            )
            + "\n",
            encoding="utf-8",
        )
        second = self._capture_payload(
            root,
            self._payload(
                key="capture:commit-index:second",
                subject="CN",
                authorized=True,
            ),
            "second.json",
        )
        return capture_root / "events.jsonl", capture_root / "state.json", first, second

    def _written_bundle(
        self, root: Path
    ) -> tuple[Path, Path, Path, dict[str, object]]:
        ledger, state, first, second = self._fixture(root)
        bundle = commit_index.cold_rebuild(ledger, state)
        output = root / "derived-capture-commit-index"
        commit_index.write_bundle(bundle, output)
        return ledger, state, output, {"first": first, "second": second}

    def test_cold_rebuild_lookup_and_bounded_verify(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            ledger, state, output, captures = self._written_bundle(root)
            index = json.loads((output / "index.json").read_text(encoding="utf-8"))

            self.assertEqual(index["counts"]["ledger_event_count"], 3)
            self.assertEqual(index["counts"]["fact_capture_receipt_count"], 2)
            self.assertEqual(index["formal_write_count"], 0)
            self.assertFalse((root / "节点总表.md").exists())

            first_capture_id = str(captures["first"]["capture_id"])
            found = commit_index.lookup(
                output,
                key_kind="capture_id",
                value=first_capture_id,
            )
            receipt = found["receipt"]
            self.assertEqual(receipt["capture_id"], first_capture_id)
            self.assertEqual(receipt["event_ordinal"], 1)
            self.assertEqual(receipt["byte_offset"], 0)
            self.assertEqual(receipt["formal_write_count"], 0)
            self.assertRegex(receipt["receipt_sha256"], r"^[0-9a-f]{64}$")
            first_event = json.loads(
                ledger.read_text(encoding="utf-8").splitlines()[0]
            )
            committed_state = {
                **capture.replay([first_event])["captures"][first_capture_id]
            }
            self.assertEqual(
                committed_state["recorded_at"], first_event["created_at"]
            )
            self.assertEqual(
                receipt["state_commitment"]["capture_state_sha256"],
                commit_index._sha256_value(committed_state),
            )

            for key_kind in (
                "capture_id",
                "event_id",
                "idempotency_key",
            ):
                again = commit_index.lookup(
                    output,
                    key_kind=key_kind,
                    value=str(receipt[key_kind]),
                )
                self.assertEqual(
                    again["receipt"]["receipt_sha256"],
                    receipt["receipt_sha256"],
                )

            verified = commit_index.bounded_verify(
                ledger,
                state,
                output,
                receipt_sha256=receipt["receipt_sha256"],
            )
            self.assertEqual(verified["status"], "PASS")
            self.assertTrue(verified["exact_event_range_verified"])
            self.assertTrue(verified["current_state_file_verified"])
            self.assertTrue(verified["capture_commit_state_verified"])
            self.assertFalse(verified["whole_ledger_rescanned"])
            self.assertFalse(verified["historical_post_event_state_replayed"])

            second_found = commit_index.lookup(
                output,
                key_kind="capture_id",
                value=str(captures["second"]["capture_id"]),
            )
            second_verified = commit_index.bounded_verify(
                ledger,
                state,
                output,
                receipt_sha256=second_found["receipt"]["receipt_sha256"],
            )
            self.assertTrue(second_verified["historical_post_event_state_replayed"])

    def test_rebuild_is_deterministic_and_cold_audit_replays_everything(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            ledger, state, _, _ = self._fixture(root)
            first = commit_index.cold_rebuild(ledger, state)
            second = commit_index.cold_rebuild(ledger, state)
            self.assertEqual(first, second)

            output = root / "derived"
            write_result = commit_index.write_bundle(first, output)
            self.assertEqual(write_result["status"], "WRITTEN")
            audit = commit_index.cold_audit(ledger, state, output)
            self.assertEqual(audit["status"], "PASS")
            self.assertTrue(audit["whole_ledger_replayed"])
            self.assertTrue(audit["historical_post_event_states_replayed"])
            self.assertEqual(audit["receipt_count"], 2)

    def test_build_append_receipt_is_pure_stable_interface(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            ledger, _, _, _ = self._fixture(root)
            first_line = ledger.read_bytes().splitlines(keepends=True)[0]
            first_event = json.loads(first_line.decode("utf-8"))
            post_state = capture.replay([first_event])

            receipt_one = commit_index.build_append_receipt(
                event_bytes=first_line,
                byte_offset=0,
                event_ordinal=1,
                previous_chain_sha256=commit_index.GENESIS_CHAIN_SHA256,
                post_event_state=post_state,
            )
            receipt_two = commit_index.build_append_receipt(
                event_bytes=first_line,
                byte_offset=0,
                event_ordinal=1,
                previous_chain_sha256=commit_index.GENESIS_CHAIN_SHA256,
                post_event_state=post_state,
            )
            self.assertEqual(receipt_one, receipt_two)
            self.assertEqual(
                receipt_one["event_sha256"], hashlib.sha256(first_line).hexdigest()
            )
            self.assertEqual(
                receipt_one["ledger_commitment"]["previous_sha256"],
                commit_index.GENESIS_CHAIN_SHA256,
            )
            self.assertEqual(receipt_one["formal_write_count"], 0)

    def test_bounded_verify_fails_closed_on_ledger_append_or_event_drift(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            ledger, state, output, captures = self._written_bundle(root)
            found = commit_index.lookup(
                output,
                key_kind="capture_id",
                value=str(captures["first"]["capture_id"]),
            )
            digest = found["receipt"]["receipt_sha256"]
            original = ledger.read_bytes()

            ledger.write_bytes(original + b"{}\n")
            with self.assertRaisesRegex(
                commit_index.CommitIndexError, "ledger size drifted"
            ):
                commit_index.bounded_verify(
                    ledger, state, output, receipt_sha256=digest
                )

            ledger.write_bytes(original)
            mutated = bytearray(original)
            offset = int(found["receipt"]["byte_offset"])
            mutated[offset] = ord("[") if mutated[offset] != ord("[") else ord("{")
            ledger.write_bytes(bytes(mutated))
            with self.assertRaisesRegex(
                commit_index.CommitIndexError, "event bytes drifted"
            ):
                commit_index.bounded_verify(
                    ledger, state, output, receipt_sha256=digest
                )

    def test_bounded_verify_fails_closed_on_state_and_receipt_drift(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            ledger, state, output, captures = self._written_bundle(root)
            found = commit_index.lookup(
                output,
                key_kind="capture_id",
                value=str(captures["second"]["capture_id"]),
            )
            digest = found["receipt"]["receipt_sha256"]

            state.write_bytes(state.read_bytes() + b" ")
            with self.assertRaisesRegex(
                commit_index.CommitIndexError, "state size drifted"
            ):
                commit_index.bounded_verify(
                    ledger, state, output, receipt_sha256=digest
                )

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            ledger, state, output, captures = self._written_bundle(root)
            found = commit_index.lookup(
                output,
                key_kind="capture_id",
                value=str(captures["second"]["capture_id"]),
            )
            digest = found["receipt"]["receipt_sha256"]
            receipt_path = output / "receipts" / f"{digest}.json"
            receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
            receipt["capture_id"] = "CAP-tampered"
            receipt_path.write_text(
                json.dumps(receipt, ensure_ascii=False, sort_keys=True),
                encoding="utf-8",
            )
            with self.assertRaisesRegex(
                commit_index.CommitIndexError, "content address mismatch"
            ):
                commit_index.bounded_verify(
                    ledger, state, output, receipt_sha256=digest
                )

    def test_index_drift_and_bounds_fail_closed(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            ledger, state, output, captures = self._written_bundle(root)
            found = commit_index.lookup(
                output,
                key_kind="capture_id",
                value=str(captures["first"]["capture_id"]),
            )
            digest = found["receipt"]["receipt_sha256"]
            with self.assertRaisesRegex(
                commit_index.CommitIndexError, "event exceeds bounded"
            ):
                commit_index.bounded_verify(
                    ledger,
                    state,
                    output,
                    receipt_sha256=digest,
                    max_event_bytes=1,
                )

            index_path = output / "index.json"
            index = json.loads(index_path.read_text(encoding="utf-8"))
            index["counts"]["ledger_event_count"] += 1
            index_path.write_text(
                json.dumps(index, ensure_ascii=False, sort_keys=True),
                encoding="utf-8",
            )
            with self.assertRaisesRegex(
                commit_index.CommitIndexError, "content address mismatch"
            ):
                commit_index.lookup(
                    output,
                    key_kind="capture_id",
                    value=str(captures["first"]["capture_id"]),
                )

    def test_cold_rebuild_and_audit_detect_payload_or_off_range_drift(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            ledger, state, output, captures = self._written_bundle(root)
            found = commit_index.lookup(
                output,
                key_kind="capture_id",
                value=str(captures["second"]["capture_id"]),
            )
            target_offset = int(found["receipt"]["byte_offset"])
            mutated = bytearray(ledger.read_bytes())
            self.assertGreater(target_offset, 0)
            mutated[0] = ord("[")
            ledger.write_bytes(bytes(mutated))
            with self.assertRaises(commit_index.CommitIndexError):
                commit_index.cold_audit(ledger, state, output)

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            ledger, state, _, _ = self._fixture(root)
            lines = ledger.read_text(encoding="utf-8").splitlines()
            first = json.loads(lines[0])
            first["payload_sha256"] = "0" * 64
            lines[0] = json.dumps(first, ensure_ascii=False, sort_keys=True)
            ledger.write_text("\n".join(lines) + "\n", encoding="utf-8")
            with self.assertRaisesRegex(
                commit_index.CommitIndexError, "payload hash mismatch"
            ):
                commit_index.cold_rebuild(ledger, state)


if __name__ == "__main__":
    unittest.main()
