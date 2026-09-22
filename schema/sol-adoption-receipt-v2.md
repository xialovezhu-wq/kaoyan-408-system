# Sol adoption receipt v2

## Purpose

`sol_adoption_receipt_v2` is a private, append-only observability sidecar written by
the external study-intake preprocessor after a Sol-reviewed capture reaches a
canonical terminal. It records how a token-bound Luna report related to Sol's
decision and outcome. It is not formal truth and has `formal_write_count=0`.

## Required bindings

The original v2 offer supplies subject, date, batch ID, ordered capture-set
SHA-256, capture ID and payload SHA-256, evidence manifest/bundle hashes, Luna
package ID/hash, report ID and JSON/Markdown refs and hashes, quality-receipt hash,
processing contract/fingerprint, renderer build, pipeline status, and the adoption
token.

The recorder additionally binds:

- `sol_decision_sha256`;
- `canonical_package_sha256`, required only for `curated`;
- canonical `item_result_event_id` and the SHA-256 of its exact raw JSONL line;
- `terminal_outcome` in `curated|already_current|needs_user|failed`;
- `formal_id` and normal receipt SHA-256 for `curated`;
- `formal_id` and decision-bound verification SHA-256 for `already_current`;
- no formal-success fields for `needs_user` or `failed`;
- adoption outcome and an answer-safe reason code.

The resulting sidecar therefore keeps two distinct layers: the Sol decision has
been sealed, while formal curation has independently reached its named terminal.
The read-only workbench may project these as `sol_review_status` and
`formal_curation_status` but must not merge them.

## Adoption outcomes

- `direct_adopted`: the formal result used Luna claims without material changes.
- `modified_adopted`: Sol changed at least one used claim before publication.
- `rejected`: Sol reviewed the offered report but used none of its claims.
- `fallback`: a once-valid report was not usable for the final decision and Sol
  relied on direct evidence.

Only a token-bearing v2 offer can produce this receipt. A consume result alone is
not adoption. `single_pass_degraded` may be adopted only after heightened Sol
review.

## Command shape

```bash
python3 ~/.codex/study-intake-preprocessor/bin/preprocess_consumer.py record-adoption \
  --adoption-token TOKEN --outcome OUTCOME --reason-code REASON \
  --sol-decision-sha256 SOL_SHA \
  --canonical-package-sha256 PACKAGE_SHA \
  --item-result-event-id EVENT_ID \
  --item-result-event-sha256 EXACT_JSONL_LINE_SHA \
  --terminal-outcome curated --formal-id FORMAL_ID \
  --normal-receipt-sha256 RECEIPT_SHA
```

Omit parameters that are forbidden for the selected terminal. For
`already_current`, replace the canonical-package and normal-receipt arguments with
`--verification-sha256`. For `needs_user` or `failed`, pass neither formal ID nor
formal-success hashes.

## Failure boundary

The capture ledger, formal receipt/WAL, and already-current verification remain the
authorities. A missing or conflicting adoption sidecar changes only preprocessing
observability. It must never roll back, invalidate, delay, or replay formal work.
