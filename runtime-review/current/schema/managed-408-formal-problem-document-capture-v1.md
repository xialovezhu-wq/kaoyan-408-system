Managed 408 Formal Problem Document Capture v1

External command

Exactly one source-document scope is submitted per call:

```text
python3 scripts/managed_408_current_turn.py --repo <repo> --private-root <root> capture-formal-problem-document --source-docx <regular-non-symlink-docx> --scope <part1|part2>
```

`--repo` and optional `--private-root` precede the subcommand. The caller supplies
only an invocation path and one fixed scope selector. There is no argument for a
title, page/block range, hash, Capture ID, event ID, date, release, activation, or
authority. `all`, `both`, and combined scopes are rejected.

Producer-derived identity

After exact OOXML and byte verification, the producer derives:

```text
idempotency_key = formal-problem-document:v1:<source_document_sha256>:<scope_id>
capture_id = CAP-<YYYYMMDD>-<first-12-hex-of-sha256(idempotency_key)>
event_id = CE-<first-20-hex-of-sha256("capture:" + idempotency_key)>
```

The key intentionally does not include `scope_sha256`. Exact replay returns the same
Capture and event with zero new raw event writes. Scope content, title, range,
dialogue, learner-answer, correction, or attachment drift therefore reaches the same
key and fails as a content conflict rather than creating a substitute Capture ID.

Commit order

The managed route performs these bounded steps:

1. Verify the regular non-symlink DOCX bytes, OOXML title and selected section
   closure, exact source hash, and ordered embedded-image hashes.
2. Publish and immediately reopen one private
   `current-question-evidence-bundle-v4` manifest and all source objects.
3. Publish the private background handoff in a pending state.
4. Jointly commit one existing `intake_fact_capture_event_v1` through
   `capture_hot_writer_408.capture_fact` and verify its hot receipt.
5. Publish the release-neutral producer-binding attestation.
6. Promote the same handoff to `ready/document_complete`, seal the private producer
   receipt, and reopen it by hash.

Producer receipt

The immutable private receipt schema is
`formal-problem-document-capture-receipt-v1` at:

```text
formal-problem-document-capture-receipt://sha256/<receipt_sha256>
```

It binds the Capture/event identities, idempotency-key SHA-256, Capture payload
SHA-256, complete `document_scope`, source-document object, ordered attachment
objects, v4 evidence manifest/object, producer attestation, and ready background
handoff. Its `raw_event` points to:

```text
wiki/study_vaults/408-full/state/intake-curation/events.jsonl
```

and binds `event_id`, `event_sha256`, `capture_id`, and `payload_sha256`. Its hot
receipt points to:

```text
wiki/study_vaults/408-full/state/intake-curation/.capture-hot-writer-v1/capture-receipts/<receipt_sha256>.json
```

The receipt is evidence for reopening natural producer artifacts, not a caller-owned
success assertion. A collector must reopen the raw event, hot receipt, v4 manifest,
private attachment bytes, producer attestation, and background handoff and revalidate
their hashes.

Privacy and authority

The public event contains no question/dialogue body, learner answer, final correction,
DOCX host path, or private attachment path. It contains one opaque evidence locator
and answer-safe content identities. The raw Capture is release-neutral. Release,
activation, and authority are absent until the current Dispatcher injects them during
freeze/materialization. `model_call_count`, `luna_call_count`, and
`formal_write_count` are all zero.
