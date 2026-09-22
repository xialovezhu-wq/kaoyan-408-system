# 408 Managed Transaction Attestation v1

Status: legacy read-only compatibility. The prepared-option broker control plane is
retired. Existing attestations may be inspected or replay-verified, but no new turn
may publish this object; active turns use `managed_current_question_turn_v1`.

## Purpose

`managed_408_transaction_attestation_v1` binds the existing receipts produced by one
compiled frozen morning option turn:

```text
verified display receipt
-> managed begin-turn
-> existing morning answer-turn
-> deterministic reply compiler
-> managed finalize-reply
-> transaction attestation
```

It is metadata for verification and exact recovery. It is not another transaction
engine, review ledger, capture ledger, session ledger, or formal record. The existing
writers remain the only truth writers.

## Entry contract

`prepared-option-turn` accepts one main-surface display receipt, one ephemeral
`A/B/C/D` choice or `BLANK/UNCERTAIN`, an optional low/medium/high confidence value,
one unique request ID, and an optional trusted response time. Omission is accepted
only when the verified display receipt records `omitted_means_high`; it then maps to
`high`. A receipt with `required_explicit` fails with `confidence_not_reported`
before any outcome writer runs.

The CLI rollout switch is `--rollout-mode shadow|live` and defaults to `shadow`.
Only an explicit `--rollout-mode live` may enter the managed begin, outcome,
capture, finalize, and attestation path documented below. The Python
`prepared_option_turn` function remains the internal live API.

## Default shadow contract

Shadow mode performs only current display-receipt revalidation, private grader
revalidation, and deterministic classification. It does not call begin-turn,
answer-turn, capture, finalize-reply, or transaction-attestation publication.
Therefore it writes no review outcome, session event, capture, personalization
receipt, transaction object, or formal record.

The compact `managed_408_prepared_option_turn_shadow_envelope_v1` returns
`legacy_authoritative=true`, `display_authorized=false`, one
`comparison_sha256`, explicit zero write counts, and
`answer_feedback_included=false`. It does not return the classified result,
correctness, canonical outcome, feedback, reply text, learner choice, confidence,
or comparison body. The comparison material exists only ephemerally for hashing.

For an option-only reply it uses these deterministic mappings:

| Choice projection | Confidence | Session result | Canonical result |
|---|---|---|---|
| correct | high | `independent_correct` | `independent_correct` |
| correct | medium or low | `fragile_correct` | `independent_correct` with fragile evidence |
| incorrect | any resolved level | `wrong` | `wrong` |
| blank or uncertain | any resolved level | `blank` | `uncertain` |

No reasoning text means:

```json
{
  "reasoning_result": "not_observed",
  "first_break": "not_observed",
  "first_break_provenance": "not_observed"
}
```

The evaluator's choice-specific first-break candidate may shape prepared generic
feedback, but it must not be persisted as a learner-observed break. Missing response
time is `null` in the canonical review outcome, transaction attestation, and compact
envelope. The session-v1 adapter may receive its existing internal zero sentinel;
that sentinel has no measured-time meaning and must not cross the broker boundary.

## Stored attestation

The content-addressed object contains:

- stable request, session, item, display, and review-event identities;
- session and canonical result, exact confidence, and the fixed unobserved reasoning fields;
- nullable response time and capture status;
- SHA-256 values for the display, begin, review commit, session first, session binding,
  session feedback, optional capture, finalize binding, exact reply, and optional next
  display receipt;
- the SHA-256 of the exact broker build that compiled the turn;
- the review commit generation, exact byte offset, byte length, and line SHA-256;
- for session first, binding, and feedback commits, each exact event byte offset,
  byte length, line SHA-256, event identity and sequence, plus the current signed
  session generation ID, manifest SHA-256, and head sequence observed when the
  attestation was published;
- when capture is required, its exact event ordinal, byte offset, byte length, line
  SHA-256, current joint generation number and ID, and joint-manifest SHA-256;
- `managed_hot_path=true`, `display_authorized=true`,
  `reply_body_stored=false`, and `formal_write_count=0`.

It must never store the learner's option, question text, options, evaluator key,
feedback text, complete learner response, or reply body.

The content identity is `MTA-` plus the first 24 uppercase hex characters of the
canonical payload SHA-256. The object SHA-256 is bound once to the request ID through
an immutable transaction pointer.

## Display authorization

The broker may return the exact reply only after:

1. the incoming display receipt revalidates against the live frozen pack;
2. the existing managed answer-turn returns verified review, session, feedback, and
   conditional capture receipts with zero formal writes;
3. each returned verified receipt is boundedly reopened and projected into the exact
   generation/range/line commitment described above, without a ledger scan;
4. the exact deterministic reply bytes are bound by `finalize_personalized_reply`;
5. the transaction attestation is content-addressed and pointer-bound.

A failure before these gates returns `display_authorized=false` and no
answer-bearing reply. A retry uses the same request ID and existing idempotency keys;
it must not create a second outcome or capture.

## Compact and inspection surfaces

The normal `managed_408_prepared_option_turn_envelope_v1` contains only the exact
reply text, reduced result fields, capture status, reply hash, transaction-attestation
hash, optional next display-receipt hash, replay flag, and zero formal writes. Its
compact JSON encoding must not exceed 4,096 UTF-8 bytes.

Full receipt inspection is a separate `inspect-transaction` command. The
compatibility alias `inspect-receipt` accepts the same `--attestation-sha256` and
returns the identical result. Inspection verifies the
attestation object and pointer, current display receipt, managed begin receipt,
detached slice object when present, review exact range, all three session exact
ranges, the conditional capture exact range, current reachable hot generations,
broker build, and finalize binding before returning their full metadata. These
checks use event-specific receipt lookup and bounded exact-range reads; they do not
replay or scan a ledger. Normal reply composition must not call this inspection
path.

A later session or capture generation may legitimately advance. Verification still
requires the attested receipt coordinates to remain exact and the current head to
be at least as new as the attested head. If the head sequence/generation number has
not advanced, its generation ID and manifest SHA-256 must still match exactly.

## Storage

```text
wiki/study_vaults/408-full/state/managed-408-turn-broker/
  objects/transactions/<attestation-sha256>.json
  pointers/transaction/<request-key-sha256>.json
```

This broker state is receipt metadata only. It does not replace the canonical review
ledger, managed session hot state, capture joint receipts, or personalization
begin/finalize receipts.
