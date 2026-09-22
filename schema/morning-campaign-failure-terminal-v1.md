# Morning campaign failure terminal v1

This contract owns one successor-campaign item only after the answer runtime has
committed `rapid_review_failure_pending` and remains at
`failure_pending_capture`.

This is a legacy-only contract. A run bound to
`morning-campaign-saving-contract-v2` uses the per-item terminal in
`morning-campaign-episode-terminal-v2.md` for correct, wrong, and unresolved
items alike.

## Commands

```text
python3 scripts/morning_campaign_failure_terminal_408.py append-turn --input-json TURN.json
python3 scripts/morning_campaign_failure_terminal_408.py finalize-appended-and-next --input-json TERMINAL.json
python3 scripts/morning_campaign_failure_terminal_408.py draft-cursor --input-json IDENTITY.json
python3 scripts/morning_campaign_failure_terminal_408.py buffer-turn --input-json TURN.json
python3 scripts/morning_campaign_failure_terminal_408.py finalize-and-next --input-json TERMINAL.json
```

The compact learner path uses `morning-campaign-failure-append-input-v1` and
`morning-campaign-failure-terminal-append-input-v1`. In addition to the same roots,
display path/SHA and idempotency key, it requires `run_id`, `item_id`,
`expected_prior_turn_count`, `expected_prior_trace_sha256` and `new_turns` without
ordinals. Terminal semantic/attachment fields remain unchanged. The model supplies
only newly observed exact turns; the code verifies the old v1 draft and each immutable
turn object, checks the expected prefix, assigns ordinals and calls the original owner
with its complete cumulative input.

A fresh draft requires count zero, the canonical SHA-256 of `[]`, and the actual
displayed assistant `utterance` as the first new turn. No question/assistant text is
reconstructed from metadata. A terminal append may have `new_turns=[]` only if its
verified assembled trace is nonempty. Existing full-input commands remain compatible.

The same-item lock covers prefix comparison, assembly and the original owner call.
Before invoking the owner, the adapter privately seals the complete input under
`<draft_root>/append-v1/requests/<sha256(idempotency_key)>.json`, bound to request,
roots, display identity and exact prior prefix. Identical retries use this sealed input
even after a draft grows or a terminal advances. Changed requests, stale cursors and
tampered snapshots fail closed rather than rebuilding a different history.

`draft-cursor` takes only repo/runtime/draft roots, run/item and display path/SHA; it
returns the current `turn_count` and `trace_sha256` without creating files/locks or
returning dialogue/answers. Reuse the last append response's cursor on healthy turns;
cursor reads are for context recovery, not a new mandatory call before every append.

`buffer-turn` reads repo/runtime/draft roots. `finalize-and-next` reads
repo/runtime/package/draft roots from the private input JSON.
The terminal caller never supplies a Capture ID, Capture receipt, campaign receipt or
navigation target.

## Draft

`morning-campaign-failure-draft-v1` is item-stable under exact
`run_id|segment_id|item_id|display_receipt_sha256`. The owner receives the complete
ordered trace, either directly or assembled by the compact adapter. Existing turns must be an exact prefix. Turn objects are content
addressed by SHA-256 and accept only `user|assistant` plus
`utterance|first_action|reasoning|hint|correction|restatement|answer`.

Draft writes have:

```text
package_write_count=0
capture_write_count=0
formal_write_count=0
navigation_write_count=0
background_processing=none
```

## Final transaction

### Answer-safe Capture summaries

The terminal conversation and feedback may contain complete protected question and
answer content. Capture `user_error_entry` and `first_action` are narrower index
summaries. The owner checks each with the unchanged Capture answer-safe scanner;
unsafe values become explicit notices referring to the sealed package, retaining
their observed provenance and adding `capture_user_error_entry_summary_deferred` or
`capture_first_action_summary_deferred` to Capture missing-summary metadata. It does
not infer replacement error causes or declare observed evidence absent.

This is a Capture projection only: original terminal inputs, compact journals, package
bytes, source references and their hashes stay unchanged. A previously rejected row
can therefore retry with its same identities and create the first valid Capture.
Already committed proof/receipt recovery is unchanged. Source identity, schema and
all other invalid payload fields still fail closed; the scanner is not relaxed.
The actual package locator and hash remain in `stable_evidence_refs`. A deferred
summary notice cannot supply a formal review first break: fallback backflow must
request a direct assessment grounded in that package instead of treating the notice
as a learner error. Canonical review evidence and explicit direct assessments retain
their existing paths.

The one legal order is:

```text
exact trace sync
→ corrected response and semantic terminal check
→ sealed cs408-conversation-package-v1
→ package file/hash/identity reopen
→ one intake_fact_capture_v1 bound only to package locator/SHA
→ canonical Capture ledger reopen
→ capture-hot-writer joint reopen
→ rapid-review-durable-capture-receipt-v2
→ runtime canonical Again receipt reopen and finalization
→ direct successor display or campaign complete
→ answer bridge receipt
```

Package identity is stable on `study_date|run_id|item_id|source_id`. Capture identity is
stable on `study_date|run_id|item_id|final-package:v2`. Exact replay only reopens an
already sealed package and committed Capture.

For new campaign display receipts, the owner reopens every entry in the private
`question_assets` array and verifies its path, byte count and SHA-256 before sealing.
All verified pages enter the final package as ordered `question_image` attachments.
Historical receipts without this array retain their prior single-asset compatibility;
the owner never infers omitted pages from the learner-surface Markdown.

## Durable campaign receipt v2

New production receipts use `rapid-review-durable-capture-receipt-v2`. They preserve
all v1 pending bindings and add:

```text
schema=rapid-review-durable-capture-receipt-v2
status=durable
run_id
segment_id
item_id
capture_request_id
pending_outcome_sha256
conversation_package_locator
conversation_package_sha256
conversation_package_root
capture_id
capture_payload_sha256
capture_idempotency_key
capture_event_id
capture_event_sha256
capture_ledger_event_sha256
capture_joint_receipt_sha256
capture_joint_manifest_sha256
capture_bounded_manifest_sha256
capture_status=awaiting_sol_formalization
formalization_authorized=true
producer_schema=cs408-campaign-package-capture-proof-v1
receipt_content_sha256
formal_write_count=0
```

Runtime recomputes `receipt_content_sha256`, reopens the canonical Capture binding,
reopens the hot joint proof, and requires the unique stable evidence ref to equal the
sealed package locator/SHA. A self-asserted JSON receipt is insufficient.

Historical v1 receipts remain immutable and readable. New terminal owners emit v2
only.

## Recovery

Byte-identical replay with the same idempotency identity fills the first missing layer.
Package, Capture, canonical Again, runtime finalization, successor display and bridge
receipt are each at-most-once. Runtime-finalized/display-failed recovery must replay the
same runtime receipt and publish only the direct successor; a farther frontier fails as
`post_finalize_frontier_drift`.

Package `receipt.json` is published only after conversation, source, manifest and
attachment files. A directory without receipt is a recoverable partial write under the
same package identity. A present sealed receipt with a missing or hash-drifted file is
tamper evidence and fails closed; it is never repaired by rewriting sealed bytes.

The terminal never writes formal knowledge, T9, Obsidian or background work.
