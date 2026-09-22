# Ordinary Capture Hot Adapter v1

状态：只读兼容协议。新版生产入口是 `managed_408_current_turn.py`；本 adapter 不再接收新题，也不能作为显式授权或自动捕获入口。

## Purpose

`scripts/ordinary_capture_hot_adapter_408.py` is the bounded capture adapter for
one already committed `daily_practice` or `evening_d0` outcome. It replaces the
legacy ordinary-capture path that replayed the canonical review ledger.

It is not a formal intake operation. It never allocates a formal node, updates
mastery, or writes a relationship. The compatibility route does not authorize
nightly curation. A current-message explicit fast-intake route may append one
separate, receipt-bound `capture_authorized` event in the same capture lock; this
authorizes only entry into nightly curation and still authorizes no formal write.

## Required input binding

The public ordinary-study entrypoint orchestrates this adapter automatically
for every non-correct outcome. The adapter itself still requires all three
values returned by the same managed study transaction:

- `event_id`
- `event_sha256`
- `review_commit_receipt_sha256`

For a live authorization it additionally requires both
`authorization_receipt_ref` and `authorization_receipt_sha256`. This
content-addressed `managed_408_study_authorization_receipt_v1` binds the exact
canonical outcome and references the exact `managed_408_fast_intake_intent_v1`.
The adapter independently verifies both objects, including request ID, display
receipt, current raw message SHA-256 and byte length, authorization-binding
SHA-256, review event SHA-256, and review commit receipt. Neither object stores
the raw authorization message.

The adapter verifies the content-addressed receipt file, verifies the exact
commit against the review hot state, and performs one exact indexed event read.
Any identity, hash, offset, receipt, or formal-write mismatch fails closed.

## Eligibility

- `partial`, `wrong`, and `uncertain` produce one answer-safe capture.
- `independent_correct` returns `NOT_ELIGIBLE` and writes no capture when the
  adapter is called directly; the public study-turn normally skips that call
  and emits the same no-capture status itself.
- Any other result or any non-outcome/non-ordinary source fails closed.

## Output state

Without current-message explicit authorization, an eligible ordinary outcome
becomes `captured_unconfirmed` with:

- `formalization_authorized=false`
- `formal_write_authorized=false`
- `formal_write_count=0`

This is intentional. The canonical outcome proves a learning event, not the
separate user authorization required for nightly formal curation.

With current-message explicit authorization, the adapter appends exactly one
fact event and then exactly one independent authorization event under one
capture lock. Only a verified authorization receipt permits the terminal state
`awaiting_daily_curation`. If fact succeeds but authorization does not verify,
the adapter returns a content-addressed recovery ticket and must not report the
intake as complete. Replaying the same idempotency keys may add only the missing
authorization event.

`formalization_authorized=true` without both managed receipt fields fails
before review lookup, fact capture, pending publication, or any ledger write.
A naked boolean, a standalone message hash, and the old `--authorize` switch do
not grant authority.

## Hot-path limits

The healthy path requires a cold-prepared review hot state and capture hot
writer. During one turn it permits:

- two exact indexed review-event range verifications;
- one fact append/fsync plus bounded joint capture-state update;
- conditionally one authorization append/fsync plus bounded joint update in the
  same lock;
- at most two bounded capture-state reads;
- zero full review-ledger replays;
- zero full capture-ledger replays;
- zero formal-table reads;
- zero formal writes.

Cold rebuild, recovery, reconciliation, and full audits remain outside the
single-question path.

The standalone CLI and the former personalized study entrypoint are retired
rejection surfaces. They must not bridge an existing event, append capture or
authorization, or serve a new ordinary, true-exam or evening-D0 turn. Existing
receipt objects remain readable by the canonical ledger audit code.

## CLI

The historical command shape is intentionally not reproduced as an active recipe.

Do not add `--authorize`. The former broker and rich-intake commands are retired.
Existing receipts remain readable for replay and audit, but no new production
capture may enter through this adapter. Current-question capture and recovery use
`managed_408_current_turn.py` and its original idempotency key.
