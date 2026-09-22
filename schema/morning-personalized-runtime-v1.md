# Morning Personalized Runtime v1

## Purpose

`scripts/morning_personalized_runtime_408.py` is the production entrypoint for
`morning_personalized_router_408.py`. It turns the router's in-memory contract
into a manifest-bound, drift-checked command without adding a formal-repository
or review-ledger read.

The runtime has four explicitly separate actions:

1. `route` selects exactly one practice item and writes an answer-safe routing
   receipt. Selection does not advance variant cooldown.
2. `show-question` reopens the selected source through the complete verified
   source chain, verifies its sole question-only asset, returns only that asset,
   and writes a content-addressed render receipt without question text.
3. `render-question` writes the exact verified UTF-8 question to its managed
   stdout stream, flushes it, and only then signs a renderer attestation. A
   failed write or flush creates no attestation and advances no cooldown.
4. `record-display` records a variant only after it receives the selection,
   render, and renderer-attestation receipts. Queue creation, route selection,
   source prefetch, answer generation, and render-receipt creation are not
   display evidence.

Every runtime artifact has `formal_write_count=0`. None of these artifacts is a
formal node, learning outcome, mastery update, or formal-write authorization.

## Production projection loader

The default state directory is:

```text
wiki/study_vaults/408-full/state/personalization
```

For a normal route, the adapter reads exactly three projection files:

1. `manifest.json`;
2. root-manifest-bound `question_index.json`;
3. root-manifest-bound `variant-catalog.json`.

It reads one additional file when the root manifest explicitly binds a
cross-variant catalog, and one bounded `cross-subject-shadow.json` when that
catalog may route. Both use exact manifest SHA-256 bindings. The live projection
is capped at 8 MiB and 4096 records; overflow fails closed. It never rebuilds
the relation network, globs directories, or scans formal sources.

The root manifest must prove:

```text
schema = personalization_index_v1
kind = personalization_manifest
answer_safe = true
formal_write_count = 0
as_of = requested Asia/Shanghai local date
build_id = SHA-256
```

`artifact_hashes` must bind both ordinary files. The ordinary variant catalog
must also be named by
`independent_projections.variant_source_catalog.file`, have
`included_in_this_build=true`, and match `variant_catalog_sha256`.

The optional cross catalog is trusted only when
`independent_projections.cross_variant_catalog` explicitly binds it and
its artifact hash equals `cross_variant_catalog_sha256`. If no such binding
exists, the route continues with:

```json
{
  "status": "degraded",
  "reason_code": "cross_variant_catalog_not_bound",
  "routing_effect": "ordinary_verified_variants_only"
}
```

An unbound stray cross-catalog file is ignored. A declared catalog that is
missing, malformed, or hash-drifted fails closed; it is not silently downgraded.

## Learner cooldown derivation

The adapter builds `morning_learner_cooldown_v1` in memory from the already
loaded `question_index.questions[*].effective_last_attempt_date`. The question
index remains bounded to the router's maximum entity count. This derivation adds
zero file reads and does not inspect `events.jsonl`, `learner_state.json`, formal
tables, Obsidian notes, or question assets.

The upstream personalization build remains responsible for ensuring that an
effective original-attempt date comes only from a uniquely identified real first
answer. Routing does not mutate that date.

## Route receipt

Every successful production route writes one content-addressed
`morning_personalized_route_receipt_v1` under:

```text
personalization-runtime/morning-router/route-receipts/<sha256>.json
```

The receipt binds:

- root manifest build ID and SHA-256;
- question and ordinary variant catalog hashes;
- optional cross catalog hash or explicit degraded state;
- current display-cooldown snapshot hash;
- target hash;
- user-requested exception and follow-up depth;
- the one selected item and its hash.

For a selected variant, the adapter also reopens the exact catalog-selected
source through `validate_source_for_practice`, repeats the complete independent
verification chain, verifies the sole current asset, and adds a
`verified_variant_question_surface_commitment_v1`. The receipt therefore binds
the root manifest, question index, variant catalog, source manifest and question
asset SHA-256 values directly. Source, provenance, manifest, or asset drift
fails before the receipt is written.

The receipt states `selection_is_not_display_evidence=true`. Repeating an
identical route over identical inputs reuses the same content-addressed receipt.

## Question-only render receipt

`show-question` accepts only a managed selection receipt. It rechecks the
current root manifest, catalog, complete source chain and asset hash, then
returns either UTF-8 Markdown question text or one verified image path. It never
opens or returns a solution asset, and its top-level result states
`answer_safe=true`.

The metadata-only
`verified_variant_question_surface_render_receipt_v1` is stored under:

```text
personalization-runtime/morning-router/question-surface-receipts/<sha256>.json
```

It binds the selection receipt, root manifest, catalog, source manifest and
question asset hashes. It stores no question text and states
`surface_release_is_not_display_evidence=true`; actual managed text output uses
`render-question`.

## Managed output-stream renderer attestation

`render-question` has no caller-supplied callback status, renderer actor,
renderer execution, or render-event ID. It revalidates the selection and writes
the verified text to stdout; only after `write` and `flush` succeed does it use
the fixed managed renderer identity and fresh random execution/event IDs to
HMAC-sign a content-addressed `trusted_local_renderer_attestation_v1`. The
receipt binds the manifest, selection, metadata render receipt, exact asset hash
and byte count. A fixed public confirmation string is not accepted by
`record-display`.

This proves delivery to the managed local output stream, not platform-level
cryptographic proof or human perception. Every result and display event states
`platform_ui_render_cryptographically_proven=false` and `human_view_proven=false`.
Image surfaces fail closed until a separate trusted image renderer exists.

## Actual variant display recorder

The default cooldown store is:

```text
wiki/study_vaults/408-full/state/personalization-runtime/morning-router/variant-display
```

`record-display` requires all of the following:

- a managed route receipt and exact receipt SHA-256;
- a managed question-surface render receipt and exact receipt SHA-256;
- a managed HMAC-bound renderer attestation and exact receipt SHA-256;
- `selected_item.kind=verified_variant`;
- the same `source_id`, source locator, and source hash as the current verified
  catalog;
- the same root-manifest hash used for route selection;
- `displayed_date` equal to the manifest and route local date;
- a bounded idempotency key.

The commit repeats the variant's seven-day display gate while holding the
display-store lock. A route receipt is selection evidence from one bounded
snapshot, not a reservation. If another actual display of the same variant was
committed after two callers obtained route receipts, the later commit fails
closed for ages zero through six even when it uses a different idempotency key.
A fresh route at age seven may commit. An exact retry with the same idempotency
key and display facts returns the original receipt before this cooldown check
and never appends a second event.

The recorder stores only source identity, hashes, `Asia/Shanghai` date, catalog
scope, all three receipt commitments, question-asset locator and hash, and audit
metadata. It explicitly records `anchor_original_attempt_date_mutated=false`.
It never stores a question stem, option, answer, solution, learner response, or
explanation.

## Cooldown integrity and bounded reads

Display state uses:

```text
.variant-display.key
current.json
snapshots/<sha256>.json
events/<sha256>.json
idempotency/<sha256-of-idempotency-key>.json
```

The key is mode `0600`. `current.json` is HMAC-bound to one
content-addressed snapshot. A route reads at most the key, pointer, snapshot, and
current chain-head event: four files regardless of history length. An empty
store reads none.

Each actual display event is content-addressed and includes the preceding chain
head. The snapshot keeps only the latest display date and commitment per bounded
variant source. Direct idempotency addressing makes retry cost constant. Reusing
an idempotency key with changed display facts fails. A crash after publishing a
snapshot but before publishing its idempotency pointer is recovered without
appending a second display event.

Snapshot, pointer, chain-head, route-receipt, catalog, or manifest drift causes a
fail-closed error.

## CLI

Production route using default directories:

```bash
python3 scripts/morning_personalized_runtime_408.py --repo . route \
  --as-of 2026-07-23 \
  --target-json '{"subjects":["OS"],"point_ids":["OS04-06"],"mechanism_ids":["RU_OS04_LINK_COUNT"],"formal_node_ids":["OS_2099_918"]}'
```

The command returns one selected item plus
`production_integration.route_receipt`.

Optional non-display prefetch or source inspection may use:

```bash
python3 scripts/morning_personalized_runtime_408.py --repo . show-question \
  --route-receipt /absolute/managed/selection-receipt.json \
  --route-receipt-sha256 SELECTION_RECEIPT_SHA256
```

This step never advances cooldown and must not be reported as user-facing
delivery. To emit the question-only text and create its post-write attestation:

```bash
python3 scripts/morning_personalized_runtime_408.py --repo . render-question \
  --route-receipt /absolute/managed/route-receipt.json \
  --route-receipt-sha256 RECEIPT_SHA256
```

Then commit cooldown evidence using all three receipts:

```bash
python3 scripts/morning_personalized_runtime_408.py --repo . record-display \
  --source-id VAR-OS-LINK-01 \
  --displayed-date 2026-07-23 \
  --route-receipt /absolute/managed/route-receipt.json \
  --route-receipt-sha256 RECEIPT_SHA256 \
  --render-receipt /absolute/managed/render-receipt.json \
  --render-receipt-sha256 RENDER_RECEIPT_SHA256 \
  --renderer-attestation /absolute/managed/renderer-attestation.json \
  --renderer-attestation-sha256 RENDERER_ATTESTATION_SHA256 \
  --idempotency-key morning-session:item-id:render-1
```

Calling `route` alone, including repeated queue generation or prefetch, never
creates a display event and never changes a variant's seven-day cooldown.
Pre-signing multiple route receipts also does not reserve multiple displays;
the lock-held commit gate is authoritative.
