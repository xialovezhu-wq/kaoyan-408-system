from __future__ import annotations

import hashlib
import io
import json
import os
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import bounded_jsonl_index_408 as module  # noqa: E402
from bounded_jsonl_index_408 import (  # noqa: E402
    BoundedJsonlIndexError,
    append_event,
    audit_index,
    locked_writer,
    lookup_event,
    lookup_idempotency,
    main,
    rebuild_index,
    recover_tail,
)


def line(value: object) -> bytes:
    return (
        json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        + "\n"
    ).encode("utf-8")


class BoundedJsonlIndex408Tests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        base = Path(self.temp.name)
        self.ledger = base / "truth/events.jsonl"
        self.index = base / "derived/index"
        self.ledger.parent.mkdir(parents=True)
        self.ledger.write_bytes(b"")

    def tearDown(self) -> None:
        self.temp.cleanup()

    def event(self, number: int, *, idem: str | None = None) -> dict[str, object]:
        value: dict[str, object] = {
            "event_id": f"RE-{number:04d}",
            "event_kind": "outcome",
            "observed_date": "2026-07-23",
            "knowledge_point": "OS04-06",
            "first_result": "partial",
        }
        if idem is not None:
            value["idempotency_key"] = idem
        return value

    def manifest(self) -> dict[str, object]:
        return json.loads((self.index / "manifest.json").read_text(encoding="utf-8"))

    def test_exact_event_and_idempotency_lookup_has_byte_range_and_hashes(self) -> None:
        rebuild_index(self.ledger, self.index)
        first = append_event(
            self.ledger, self.index, self.event(1, idem="turn:0001")
        )
        append_event(self.ledger, self.index, self.event(2, idem="turn:0002"))
        found = lookup_event(self.ledger, self.index, "RE-0001")
        self.assertEqual("found", found["status"])
        self.assertEqual(self.event(1, idem="turn:0001"), found["event"])
        record = found["record"]
        self.assertEqual(0, record["byte_offset"])
        self.assertEqual(len(line(self.event(1, idem="turn:0001"))), record["byte_length"])
        self.assertRegex(record["line_sha256"], r"^[0-9a-f]{64}$")
        self.assertRegex(record["event_sha256"], r"^[0-9a-f]{64}$")
        by_idem = lookup_idempotency(self.ledger, self.index, "turn:0001")
        self.assertEqual("RE-0001", by_idem["event"]["event_id"])
        self.assertRegex(first["receipt_sha256"], r"^[0-9a-f]{64}$")

    def test_idempotent_append_returns_existing_and_conflict_fails(self) -> None:
        first = self.event(1, idem="turn:stable")
        committed = append_event(self.ledger, self.index, first)
        size = self.ledger.stat().st_size
        repeated = append_event(self.ledger, self.index, first)
        self.assertEqual("already_committed", repeated["status"])
        self.assertEqual(size, self.ledger.stat().st_size)
        conflict = dict(first)
        conflict["first_result"] = "wrong"
        with self.assertRaisesRegex(BoundedJsonlIndexError, "different event content"):
            append_event(self.ledger, self.index, conflict)
        self.assertEqual(size, self.ledger.stat().st_size)
        self.assertEqual(committed["event_sha256"], repeated["event_sha256"])

    def test_duplicate_event_id_without_same_idempotency_fails(self) -> None:
        append_event(self.ledger, self.index, self.event(1, idem="turn:first"))
        duplicate = self.event(1, idem="turn:second")
        with self.assertRaisesRegex(BoundedJsonlIndexError, "event_id already exists"):
            append_event(self.ledger, self.index, duplicate)

    def test_same_event_id_and_content_is_idempotent_without_explicit_key(self) -> None:
        event = self.event(1)
        first = append_event(self.ledger, self.index, event)
        size = self.ledger.stat().st_size
        second = append_event(self.ledger, self.index, event)
        self.assertEqual("published", first["status"])
        self.assertEqual("already_committed", second["status"])
        self.assertEqual("event_id", second["matched_by"])
        self.assertEqual(size, self.ledger.stat().st_size)

    def test_lock_held_writer_api_commits_multiple_events(self) -> None:
        with locked_writer(self.ledger, self.index) as writer:
            first = writer.append(self.event(1, idem="locked:one"))
            second = writer.append(self.event(2, idem="locked:two"))
        self.assertEqual("published", first["status"])
        self.assertEqual("published", second["status"])
        self.assertEqual(2, audit_index(self.ledger, self.index)["event_count"])

    def test_cold_rebuild_and_full_audit(self) -> None:
        events = [self.event(1, idem="cold:one"), self.event(2)]
        self.ledger.write_bytes(b"".join(line(event) for event in events))
        rebuilt = rebuild_index(self.ledger, self.index, chunk_size=128)
        audit = audit_index(self.ledger, self.index)
        self.assertEqual(2, rebuilt["event_count"])
        self.assertEqual("pass", audit["status"])
        self.assertEqual(2, audit["event_count"])
        self.assertEqual(1, audit["idempotency_count"])

    def test_append_rehashes_only_bounded_tail_chunks_across_boundary(self) -> None:
        first = self.event(1, idem="chunk:first")
        first["bounded_note"] = "x" * 180
        self.ledger.write_bytes(line(first))
        rebuild_index(self.ledger, self.index, chunk_size=128)
        second = self.event(2, idem="chunk:second")
        second["bounded_note"] = "y" * 180
        append_event(self.ledger, self.index, second)
        audit = audit_index(self.ledger, self.index)
        self.assertEqual("pass", audit["status"])
        found = lookup_event(self.ledger, self.index, "RE-0002")
        self.assertEqual(len(line(first)), found["record"]["byte_offset"])

    def test_crash_after_ledger_fsync_recovers_bounded_tail(self) -> None:
        rebuild_index(self.ledger, self.index)
        original_atomic = module._atomic_write
        failed = False

        def crash_manifest(path: Path, payload: bytes, *, mode: int = 0o600) -> str:
            nonlocal failed
            if path.name == "manifest.json" and self.ledger.stat().st_size > 0 and not failed:
                failed = True
                raise OSError("simulated crash before manifest publication")
            return original_atomic(path, payload, mode=mode)

        with mock.patch.object(module, "_atomic_write", side_effect=crash_manifest):
            with self.assertRaisesRegex(OSError, "simulated crash"):
                append_event(self.ledger, self.index, self.event(1, idem="crash:one"))
        self.assertGreater(self.ledger.stat().st_size, 0)
        with self.assertRaisesRegex(BoundedJsonlIndexError, "unindexed tail"):
            lookup_event(self.ledger, self.index, "RE-0001")
        recovered = recover_tail(self.ledger, self.index)
        self.assertEqual(1, recovered["recovered_event_count"])
        self.assertEqual(
            "RE-0001",
            lookup_idempotency(self.ledger, self.index, "crash:one")["event"][
                "event_id"
            ],
        )

    def test_partial_tail_fails_without_publishing_new_manifest(self) -> None:
        rebuild_index(self.ledger, self.index)
        before = hashlib.sha256((self.index / "manifest.json").read_bytes()).hexdigest()
        with self.ledger.open("ab") as handle:
            handle.write(b'{"event_id":"RE-partial"')
            handle.flush()
            os.fsync(handle.fileno())
        with self.assertRaisesRegex(BoundedJsonlIndexError, "incomplete JSONL"):
            recover_tail(self.ledger, self.index)
        after = hashlib.sha256((self.index / "manifest.json").read_bytes()).hexdigest()
        self.assertEqual(before, after)

    def test_tail_byte_budget_is_hard(self) -> None:
        rebuild_index(self.ledger, self.index)
        with self.ledger.open("ab") as handle:
            handle.write(line(self.event(1)))
            handle.flush()
            os.fsync(handle.fileno())
        with self.assertRaisesRegex(BoundedJsonlIndexError, "byte budget exceeded"):
            recover_tail(self.ledger, self.index, max_tail_bytes=16)

    def test_tail_event_budget_is_hard(self) -> None:
        rebuild_index(self.ledger, self.index)
        with self.ledger.open("ab") as handle:
            handle.write(line(self.event(1)) + line(self.event(2)))
            handle.flush()
            os.fsync(handle.fileno())
        with self.assertRaisesRegex(BoundedJsonlIndexError, "event budget exceeded"):
            recover_tail(self.ledger, self.index, max_tail_events=1)

    def test_ledger_content_drift_fails_closed_even_if_mtime_is_restored(self) -> None:
        append_event(self.ledger, self.index, self.event(1, idem="drift:one"))
        committed_mtime = int(self.manifest()["ledger"]["mtime_ns"])
        with self.ledger.open("r+b") as handle:
            first = handle.read(1)
            handle.seek(0)
            handle.write(b"[" if first != b"[" else b"{")
            handle.flush()
            os.fsync(handle.fileno())
        os.utime(self.ledger, ns=(self.ledger.stat().st_atime_ns, committed_mtime))
        with self.assertRaisesRegex(
            BoundedJsonlIndexError, "chunk commitment drifted|event drifted"
        ):
            lookup_event(self.ledger, self.index, "RE-0001")
        with self.assertRaisesRegex(BoundedJsonlIndexError, "chunk drift"):
            audit_index(self.ledger, self.index)

    def test_unexplained_mtime_change_fails_closed(self) -> None:
        append_event(self.ledger, self.index, self.event(1))
        info = self.ledger.stat()
        os.utime(self.ledger, ns=(info.st_atime_ns, info.st_mtime_ns + 1_000_000))
        with self.assertRaisesRegex(BoundedJsonlIndexError, "mtime drifted"):
            lookup_event(self.ledger, self.index, "RE-0001")

    def test_manifest_signature_drift_fails_closed(self) -> None:
        append_event(self.ledger, self.index, self.event(1))
        manifest_path = self.index / "manifest.json"
        payload = json.loads(manifest_path.read_text(encoding="utf-8"))
        payload["event_count"] = 999
        manifest_path.write_bytes(module._pretty_bytes(payload))
        with self.assertRaisesRegex(BoundedJsonlIndexError, "signature drifted"):
            lookup_event(self.ledger, self.index, "RE-0001")

    def test_shard_object_drift_fails_closed(self) -> None:
        append_event(self.ledger, self.index, self.event(1))
        manifest = self.manifest()
        digest = next(iter(manifest["event_shards"].values()))
        shard = self.index / "objects" / f"{digest}.json"
        shard.write_bytes(shard.read_bytes() + b" ")
        with self.assertRaisesRegex(BoundedJsonlIndexError, "shard drifted"):
            lookup_event(self.ledger, self.index, "RE-0001")

    def test_commit_receipt_drift_fails_cold_audit(self) -> None:
        append_event(self.ledger, self.index, self.event(1))
        receipt_sha = self.manifest()["last_commit_receipt_sha256"]
        receipt = self.index / "receipts" / f"{receipt_sha}.json"
        receipt.write_bytes(receipt.read_bytes() + b" ")
        with self.assertRaisesRegex(BoundedJsonlIndexError, "receipt drifted"):
            audit_index(self.ledger, self.index)

    def test_question_or_answer_payload_is_rejected_before_ledger_write(self) -> None:
        unsafe = self.event(1, idem="unsafe:one")
        unsafe["question_stem"] = "protected content"
        with self.assertRaisesRegex(BoundedJsonlIndexError, "protected"):
            append_event(self.ledger, self.index, unsafe)
        self.assertEqual(b"", self.ledger.read_bytes())
        unsafe = self.event(2, idem="unsafe:two")
        unsafe["nested"] = {"correct_answer": "hidden"}
        with self.assertRaisesRegex(BoundedJsonlIndexError, "protected"):
            append_event(self.ledger, self.index, unsafe)

    def test_derived_index_does_not_copy_safe_free_text(self) -> None:
        event = self.event(1, idem="safe:text")
        event["bounded_note"] = "UNIQUE-LEARNER-TEXT-MUST-STAY-IN-LEDGER"
        append_event(self.ledger, self.index, event)
        for path in self.index.rglob("*.json"):
            self.assertNotIn(b"UNIQUE-LEARNER-TEXT", path.read_bytes())
            self.assertNotIn(b"safe:text", path.read_bytes())

    def test_cli_rebuild_and_audit_are_runnable(self) -> None:
        output = io.StringIO()
        with redirect_stdout(output):
            code = main(
                [
                    "--ledger",
                    str(self.ledger),
                    "--index-dir",
                    str(self.index),
                    "rebuild",
                ]
            )
        self.assertEqual(0, code)
        self.assertEqual("published", json.loads(output.getvalue())["status"])
        output = io.StringIO()
        with redirect_stdout(output):
            code = main(
                [
                    "--ledger",
                    str(self.ledger),
                    "--index-dir",
                    str(self.index),
                    "audit",
                ]
            )
        self.assertEqual(0, code)
        self.assertEqual("pass", json.loads(output.getvalue())["status"])


if __name__ == "__main__":
    unittest.main()
