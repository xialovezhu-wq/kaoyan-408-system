# Morning campaign display bridge v1

`morning_campaign_display_408.py` is the answer-safe publication boundary for
`morning-coverage-campaign-v1`. It is separate from every legacy morning
session and does not create or mutate a legacy display, session, Capture,
review outcome, or formal artifact.

The current-display pointer and newly published public display expose the
answer-safe routing fields `saving_mode` and `episode_capture_every_item`; v2
also exposes `saving_contract_sha256`. They come from the immutable runtime run
contract, so a resumed task can choose the v2 terminal without reopening grading
material or runtime internals. Historical receipts without these fields remain
readable.

## Callable API

```python
publish_current_display(
    repo,
    *,
    review_date,
    run_id,
    control_root=None,
    runtime_root=None,
    private_root=None,
    displayed_at=None,
) -> dict
```

The production call must identify the repository, exact campaign date, and
active run. Optional injected exposure/release/variant-pool/formal-original-
pool snapshots exist only for bounded deterministic testing.

Before any display write, the bridge reopens and verifies:

1. the current control pointer and `ready` preparation;
2. the runtime `run.json`, immutable manifest, active state, segment, and item
   frontier;
3. the preparation manifest, selection manifest, selected surface, and item
   commitment;
4. the current style release and its manifest bytes;
5. the prepared seven-day exposure fingerprint;
6. the selected public surface bytes and private grading capsule bytes.

Any missing readiness, non-active runtime, frontier disagreement, manifest or
hash drift, style drift, exposure-cutoff drift, unsafe path, or answer-isolation
failure returns an error before writing a turn receipt.

## Public result

The only learner content is:

```json
{
  "question_provenance": {
    "schema": "morning-campaign-question-provenance-v1",
    "kind": "true_exam|generated_variant|local_original",
    "label": "真题｜2016 年考研 408 第 4 题",
    "surface_kind": "canonical_original",
    "true_item_id": "EXAM408-2016-Q04",
    "true_exam_id": "EXAM408-2016-Q04",
    "based_on_true_exam_id": null,
    "exam_year": 2016,
    "exam_question_number": 4,
    "basis": "verified_canonical_true_item_id",
    "answer_safe": true
  },
  "learner_surface": {
    "stem": "...",
    "options": {"A": "...", "B": "...", "C": "...", "D": "..."}
  }
}
```

The result envelope also carries answer-safe routing metadata: run, segment,
item, private receipt locator/SHA-256, `display_receipt_path`, exposure status, and zero-formal-write
counts. It never returns the answer, grading capsule, stable option mapping,
explanation, refutation, or verifier material.

`display_receipt_path` points to the already published receipt under the exact private
root. It is returned for both fresh publication and read-only reuse, without changing
historical receipt bytes or creating a second exposure. Pass it directly to the answer
bridge; this removes path-discovery reads from the learner turn.

Every question is preceded by the exact `question_provenance.label`:

- a verified canonical original uses `真题｜YYYY 年考研 408 第 N 题`;
- a verified variant uses `变式题｜基于 YYYY 年考研 408 真题第 N 题` and binds
  `based_on_true_exam_id` to the same canonical identity carried by both the
  runtime item and frozen selection;
- a same-actor web self-checked variant uses `网页自检变式｜基于 YYYY 年考研 408 真题第 N 题`, carries `acceptance_kind=web_self_checked_ready` and `independent_reviewed=false`;
- a verified formal-original surface without canonical true-exam identity uses
  `来源原题｜非系统生成变式｜真题年份未确认`.

The bridge never treats `formal_node_id`, node-name years, study dates, file paths, or
free text as true-exam provenance. A missing or conflicting canonical identity fails
closed before a new display receipt is written. Historical receipts without the new
field derive it read-only from the current verified runtime item and frozen selection;
the receipt bytes are not rewritten.

新写的 `<control>/current-display/YYYY-MM-DD.json` pointer 直接携带受保护的完整 `learner_surface`、`question_assets`、非答案的 `option_ids_by_letter`、完整 answer-safe `question_provenance`、`surface_kind`、`acceptance_kind` 与 `source_label`。恢复只需读取 pointer；旧 display receipt 字节保持不变。

Canonical originals are rendered from the verified question image commitment;
the Markdown image is the `stem` and the A-D values point to the corresponding
labels in the image. Verified local formal originals render either their bound
question asset or their frozen answer-safe text surface. Verified variants
render the frozen learner permutation.

## Private receipt

The bridge reuses `current_question_evidence_408.py` content-addressed turn
storage:

- path: `<private_root>/turns/<sha256>.json`
- locator: `current-question-turn://sha256/<sha256>`
- envelope schema: `current-question-turn-receipt-v1`
- receipt schema: `morning-campaign-display-receipt-v1`
- status: `campaign_display_published`

The successor-specific status is intentional. The shared exposure reader
validates it under the campaign receipt contract and returns its exact
`true_item_id` without trying to resolve the run as a legacy session.

Required receipt bindings are:

| Group | Fields |
|---|---|
| Frontier | `run_id`, `campaign_date`, `segment_id`, `segment_ordinal`, `item_offset`, `item_id`, `formal_node_id`, `true_item_id`, `variant_id`, `surface_kind` |
| Immutable campaign | `runtime_manifest_sha256`, `campaign_manifest_sha256`, `segment_commitment_sha256`, `campaign_item_commitment_sha256`, `pack_commitment_sha256` |
| Surface and grading | `surface_sha256`, `surface_object_path`, `surface_object_sha256`, `question_assets[]`, `grading_sha256`, `grading_capsule_ref`, `grading_capsule_path`, `grading_capsule_sha256` |
| Release and selection | `style_release_id`, `style_release_content_sha256`, `style_release_manifest_sha256`, `selection_id`, `selection_sha256`, `selection_manifest_sha256` |
| Display | `option_ids_by_letter`, `question_provenance`, `learner_surface`, `display_content_sha256`, optional question-asset binding, `displayed_date`, `displayed_at` |
| Exposure | `exposure_cutoff` with the seven-day window, prepared and verified fingerprints, legacy exposure count, and prior successor receipt count |
| Safety | `answer_safe=true`, `answer_content_in_receipt=false`, `explanation_content_in_receipt=false`, `capture_write_count=0`, `formal_write_count=0` |

`option_ids_by_letter` and all grading fields are private receipt fields. They
must never be copied into `learner_surface`.

Every new receipt includes the private `question_assets` array. Canonical and
image-backed formal originals retain every verified page as
`ref|path|sha256|size_bytes|page_locator`; the legacy singular
`question_asset_ref|question_asset_sha256` fields remain the first-page head.
Text-backed formal originals and generated variants use an empty array. The
public result never exposes the private `question_assets` array or its metadata.
The learner-visible Markdown stem may still contain the absolute local paths
required by Codex to render those same public question images; those render
targets are not grading, package or Capture evidence. Historical receipts
without the array remain readable.

For a verified variant or verified local formal original,
`grading_capsule_ref` is relative to the details root and
`grading_capsule_path` is the resolved absolute path. For a canonical original,
the ref remains
`private-sibling://answer-capsules/<canonical-id>.json`; the absolute path
resolves through the private sibling of the bound style release. The capsule
bytes must equal both `grading_capsule_sha256` and the runtime
`grading_sha256`.

## Idempotency and exact exposure

The frontier identity is the tuple
`(run_id, segment_ordinal, item_offset, item_id)`. Under the existing runtime
lock, the bridge scans verified campaign turn receipts before publication.
Exactly one matching receipt is returned as `already_published`; a second
receipt is never created. Multiple matching receipts fail closed.

The first receipt is itself the learner-visible exact exposure event. Before a
new frontier is displayed, any prior successor receipt for the same
`true_item_id` with `0 <= gap_days < 7` blocks publication. This gate is bound
to the exact original or variant identity, not to the whole formal node or
knowledge point.

The preparation exposure fingerprint remains frozen for the whole active run.
When it is rechecked after the first display, receipts produced by that same
run are excluded from the frozen-cutoff comparison; they are still checked by
the separate exact-item gate above. Receipts from another run remain part of
the live cutoff and can fail publication as drift.

The bridge writes no formal data. A healthy first display writes one private
turn receipt and reports `learner_evidence_write_count=1` and
`formal_write_count=0`; an idempotent replay reports zero new learner-evidence
writes.
