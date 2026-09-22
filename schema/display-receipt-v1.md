# 408 Managed Display Receipt v1

Status: legacy read-only compatibility. The publisher and verifier described below
are retired and must not be invoked for a new turn. Existing receipt objects remain
valid historical evidence; CurrentQuestionContext v1 receives only the current
public surface plus an opaque private capsule binding.

## Purpose

`managed_408_display_receipt_v1` proves which already verified frozen prepared-pack
surface was compiled for display. It is display metadata, not learner evidence, a
cooldown event, a review outcome, a capture, or a formal write.

The former publisher was `scripts/managed_408_turn_broker.py publish-display`; its
former verifier was `verify-display`. Both used to re-enter the existing
`morning_review_prepared_pack_408.py show_item` gate, so this schema remains
compatible with `morning_review_prepared_pack_manifest_v1` without changing that
pack format.

## Stored receipt

The content-addressed receipt contains exactly:

| Field | Contract |
|---|---|
| `schema` | `managed_408_display_receipt_v1` |
| `kind` | `frozen_prepared_surface_display` |
| `display_id` | `MDR-` plus the first 24 uppercase hex characters of the canonical payload SHA-256 |
| `session_id` | Existing frozen managed morning session |
| `item_id` | Existing selected item |
| `surface_role` | `main`, `guided`, or `repair` |
| `pack_id` | Bound v1 prepared-pack identity |
| `pack_manifest_ref` | Repository-relative content-addressed prepared-pack manifest reference |
| `pack_manifest_sha256` | Exact prepared-pack manifest SHA-256 |
| `queue_snapshot` | Exact repository-relative queue reference and SHA-256 |
| `source_snapshots` | Sorted current-item evidence references and SHA-256 values |
| `surface_sha256` | Exact prepared public-surface commitment |
| `display_sha256` | SHA-256 of the broker's deterministic UTF-8 rendering |
| `private_grading_ref` | Repository-relative private evaluator reference; never its body |
| `private_grading_sha256` | Exact private evaluator SHA-256 for this surface role |
| `feedback_mapping_sha256` | SHA-256 of the canonical private `feedback_by_result` mapping; never its text |
| `continuation_surface_refs` | Sorted answer-safe item, role, reference, and SHA-256 commitments for the bounded manifest-derived continuation superset |
| `answer_protection_policy_id` | `frozen_prepared_pack_answer_isolation_v1` |
| `answer_protection_policy_sha256` | SHA-256 of the fixed answer-protection policy material |
| `confidence_omission_policy` | `omitted_means_high` or `required_explicit` |
| `display_body_stored` | Always `false` |
| `answer_safe` | Always `true` |
| `learner_evidence_write_count` | Always `0` |
| `formal_write_count` | Always `0` |

The receipt must not contain the question stem, options, answer, evaluator,
feedback, learner choice, learner response, or reply body. The already authorized
public surface remains in the prepared pack; the broker returns its exact rendering
only in the live display envelope.

The private evaluator reference and hashes are safe metadata. The evaluator body,
correct option, first-break mapping, feedback mapping body, and any answer-bearing
text remain only in the prepared pack's private object.

## Deterministic rendering

The live display bytes are:

```text
<stem>

A. <option A>
B. <option B>
C. <option C>
D. <option D>

<response instruction>
```

The trailing newline is part of `display_sha256`. A prepared answer may proceed
only when the receipt object, its immutable pointer, current session hot state,
pack manifest, queue snapshot, current-item source snapshots, public surface,
private grader, feedback mapping, continuation references, answer-protection
policy, surface SHA-256, and deterministic display SHA-256 all rebind and verify.

## Confidence omission policy

The broker searches the exact rendered public surface for this exact sentence:

```text
回复选项即可；若不是高置信度，请注明中或低
```

If and only if that exact text is present, the receipt records
`omitted_means_high`; an omitted confidence value then deterministically means
`high`. Every older or differently worded surface records `required_explicit`.
Omission against such a receipt fails with `confidence_not_reported` before
grading, managed begin-turn, canonical outcome, session event, or capture.

## Storage and idempotency

Objects are stored below:

```text
wiki/study_vaults/408-full/state/managed-408-turn-broker/
  objects/displays/<receipt-sha256>.json
  pointers/display/<logical-key-sha256>.json
```

The logical key is the exact session, item, and surface role. Re-publishing the same
binding is idempotent. A different binding for the same logical key fails closed.
Symlinked managed components, non-regular objects, hash drift, pointer drift, pack
drift, or session hot-state drift all block grading before a learner-evidence write.

## Compact display envelope

`publish-display` returns `managed_408_display_envelope_v1` with the exact public
`display_text`, receipt SHA-256, display SHA-256, surface SHA-256, identity, and zero
formal writes. Its compact JSON encoding must not exceed 4,096 UTF-8 bytes. An
oversized v1 surface must be rejected during display preparation and rebuilt as a
bounded prepared surface; it must not silently bypass the receipt.

## Context separation and ordinary-system receipt

The legacy `managed_408_display_receipt_v1` kind
`frozen_prepared_surface_display` always means `morning_review`; readers infer
that context so existing frozen v1 receipts remain valid. It is never accepted
by `fast-intake-turn`.

An ordinary generated question uses
`managed_408_system_display_receipt_v1` with kind
`ordinary_system_prepared_surface_display` and
`context_type=ordinary_system`. It has a `SYS-` session, an `OQ-` or `AOQ-`
item, one main surface, no morning continuations, and the same immutable
surface, private-grader, source, queue, manifest, answer-isolation, and feedback
commitments. `publish-system-display` rejects a pack currently bound to the
active morning session. Reverification repeats that check before grading.

`prepared-option-turn` accepts only the morning context. `fast-intake-turn`
accepts only the ordinary-system context. A display receipt can therefore not
be reused to append both a morning outcome and a `daily_practice` outcome.

For a morning first answer, the verified display receipt is the authority for
`session_id`, `item_id`, and `surface_role`. `prepared-option-turn` therefore
requires only `display_receipt_sha256`; legacy `--session` and `--item` values
are optional compatibility assertions and must exactly match the receipt when
present.

The first-answer writer accepts only `surface_role=main`. A guided surface fails
closed with `guided_display_requires_guided_check_route`; a repair surface fails
closed with `repair_display_requires_record_repair_route`. Those roles have
different session semantics and must never be appended as a second first answer.
