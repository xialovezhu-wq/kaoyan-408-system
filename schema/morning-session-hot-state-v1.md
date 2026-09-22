Morning session hot state v1

Purpose

This adapter keeps the existing morning-review ledger as truth while removing a
full ledger replay from the ordinary session load and commit path. It is a
derived, answer-safe projection. It does not write the canonical review-loop
ledger, formal wrong-question nodes, mastery, relation edges, or question text.

Files

- Existing truth: one session directory containing `events.jsonl` and
  `state.json`.
- Hot manifest: `.morning-session-hot-v1/manifest.json`, signed with a local
  mode-0600 HMAC key.
- Exact index: `.morning-session-hot-v1/index/`, implemented by
  `bounded_jsonl_index_v1`.
- Content-addressed state snapshots, sequence projections, and commit receipts.
- One signed idempotency pointer per committed request.
- `pending.json` while a transaction is in progress.

Cold bootstrap

`cold_bootstrap` is the only operation allowed to read the complete session
ledger. It must run while the session is stable. It verifies that the existing
state equals replay of exactly that ledger. For already-active legacy sessions,
the only accepted difference is omission of known neutral fields introduced by
later schema-compatible code, such as empty follow-up maps, optional `null`
fields, and the default split judgment values. Observed value differences still
fail closed. It then rebuilds the exact index, binds the last event and state
snapshot, and atomically publishes a signed hot manifest.
The function acquires the existing session lock unless the caller declares that
the lock is already held.

Hot load

`load_state_bounded` reads at most 64 KiB from `state.json`. It verifies the
signed hot manifest, the signed bounded-index manifest, and ledger identity,
size, mtime, event count, and Merkle-style chunk-root commitments. It performs
no open or read of `events.jsonl`. A tail, stale state, stale index, changed
identity, pending transaction, or invalid signature returns
`reconcile_required`. A state larger than 64 KiB returns `exit_hot_path`.

Commit order

The caller must already hold `morning_review_session.session_lock`. The adapter
cannot prove this mechanically because the existing context manager exposes no
lock token; `session_lock_held=True` is therefore a trusted local API contract.

One commit performs these durable steps:

1. Verify the current bounded state and idempotency key.
2. Use `morning_review_session._apply_event` on a deep copy to compute the next
   state, and reject it before append if it exceeds 64 KiB.
3. Publish a signed pending marker.
4. Append and fsync exactly one complete session event.
5. Atomically replace and fsync `state.json`.
6. Recover exactly the bounded ledger tail into `bounded_jsonl_index_v1`.
7. Write content-addressed sequence and state objects.
8. Write a signed content-addressed receipt binding the event byte range and
   hash, prior state, next state, bounded index, and ledger root.
9. Write the signed idempotency pointer.
10. Atomically publish the new signed hot manifest.
11. Remove and directory-fsync the pending marker.

Any exception after the pending marker is published leaves it in place. The
next hot operation must return `reconcile_required`; it must not silently
recover, replay, reveal feedback, or append another event. Explicit cold
bootstrap may re-bind only when the existing state already equals full ledger
replay.

Feedback gate

`feedback_revealed` is rejected before any append unless the addressed item has
a recorded first answer and that answer already carries a non-empty
`canonical_event_id`. This restores the gate that exists in the higher-level
morning command path but is not enforced by `_apply_event` alone.

Idempotency

Every hot commit requires an answer-safe idempotency key. A fully committed
retry returns its verified prior receipt, even if the caller supplied the old
pre-commit state. Reuse with different event type or payload fails. An indexed
event without a complete hot receipt is treated as an interrupted transaction
and requires reconciliation.

Exact reads and verification

`lookup_session_event` reads one indexed event and the bounded chunk or chunks
covering it. `lookup_sequence` first resolves a bounded per-session sequence
projection, then verifies the exact event. `verify_commit` loads the prior and
next state snapshots, reapplies the event through `_apply_event`, and checks all
receipt hashes without replaying the full ledger. `verify_idempotency` resolves a
signed idempotency pointer and returns the same receipt verification; a missing
pointer returns `not_found` without scanning the session ledger.

Budgets

- State file and state snapshot: 65,536 bytes.
- One event: 65,536 bytes, inherited from `bounded_jsonl_index_v1`.
- Sequence projection: 524,288 bytes and at most 4,096 events.
- Hot manifest or receipt: 131,072 bytes.
- Tail recovery: one bounded-index tail operation, with the generic index hard
  limits still applying.

Answer and write boundaries

Events, state, receipts, and indexes must pass the protected-field scan. The
adapter does not store complete stems, options, answers, complete responses, or
solutions. Every derived surface declares `answer_safe=true` and
`formal_write_count=0`.
