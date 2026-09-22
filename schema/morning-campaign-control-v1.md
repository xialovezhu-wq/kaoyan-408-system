Morning campaign control v1

Purpose

`scripts/start_morning_coverage_campaign_408.py` is the shadow producer and
control surface for `morning_due_run_v1` plus `morning-coverage-campaign-v1`.
It is separate from every legacy queue, prepared pack, session, display,
Capture, and formal writer.

Commands

```text
python3 scripts/start_morning_coverage_campaign_408.py --repo REPO prepare --date YYYY-MM-DD --details-root DETAILS --release RELEASE
python3 scripts/start_morning_coverage_campaign_408.py --repo REPO status --date YYYY-MM-DD
python3 scripts/start_morning_coverage_campaign_408.py --repo REPO start --date YYYY-MM-DD --private-root PRIVATE
python3 scripts/start_morning_coverage_campaign_408.py --repo REPO audit --date YYYY-MM-DD
```

`DETAILS` is the kaoyan-408-details vault root. Release asset locators already
start with `corpus/408统考真题/`; the control plane resolves them exactly once
against that vault root. The default release remains the final production ESR
below `DETAILS/corpus/408统考真题/style-releases/`.

Legacy priority gate

`prepare` and `start` first inspect the legacy review-loop runtime pointer and
the bounded legacy session state directory. Any `in_progress` or `paused`
legacy session returns `blocked_legacy_session`. That result performs zero
control, runtime, learner-evidence, Capture, and formal writes. The control
plane never closes, cancels, migrates, expands, or otherwise changes a legacy
session.

Preparation

The producer calls the deterministic scheduler dry-run API and requires the
complete due identity:

```text
due_count == scheduled_count
skipped_due_count == 0
coverage_identity_holds == true
```

The verified seven-day exposure set is normalized to exact `true_item_id`
values by verified legacy `source_id`, canonical question assets, normalized
stem identity, verified variant identity, and verified local-formal question
assets/text identity. Cooldown is therefore item-specific, not a whole
knowledge-point exclusion. A preparation rebuild after improved mapping must
replace the recent original before start; it cannot grandfather a now-known
duplicate merely because an older shadow run was ready.

For each due formal node, the producer prefers a verified direct canonical
source explicitly bound by the node. If that exact source is absent, lacks a
usable answer capsule, or is recent and has no verified variant, it may try
another production-usable direct canonical source with the same main knowledge
code. The cooldown excludes only the same exact `true_item_id`; it must not
exclude a different, unexposed official question merely because the two items
share a knowledge code. The producer never creates a V1 surface clone.

The surface authority is the union of the verified true-exam release, the
verified V2/V3 bank, and `formal_original_surface_408`'s independently solved
local A-D pool. A local formal original is eligible only for its exact formal
node/SHA and exact `FOS-` cooldown identity. Every variant or formal-original
selection is a write-free preview containing a bound consumption request. The
control plane does not generate questions, invoke a model, use the network, or
accept draft, expired, quarantined, conflicting, or release-drifted material.

Every prepared item binds all of the following:

- formal node and scheduler item hashes;
- exact source and `true_item_id`;
- learner surface hash;
- private grading or answer-capsule hash;
- pack commitment hash;
- exam-style release ID and content hash;
- author commitment hash;
- blind and adversarial verifier commitment hashes;
- answer isolation and mandatory-audit status.

All-or-nothing publication

Items are divided into immutable segments of at most 16. Each segment receives
one real `morning-campaign-segment-verification-v1` receipt binding all item
pack hashes, the selection manifest, and the style release. For 30, 40, and
100 item packs, the existing variant-bank completeness validator is also
required to pass.

If any item is unavailable, the preparation status is `content_blocked` and
the blocker remains listed by formal node. No campaign manifest or runtime run
is created, even when other items were individually resolvable. Previewed
variants are not consumed and no selection manifest is published. There is no
partial-run or partial-consumption state.

Only after every item and segment is ready does the producer build the final
campaign manifest, freeze one content-addressed
`exam_variant_selection_manifest_v1`, and bind its global campaign commitment
to the final campaign-manifest SHA-256. It then calls
`exam_variant_bank_408.commit_consumption` once for the whole frozen selection.
That batch commit and every selected formal-original consumption receipt must
succeed before runtime `create_run`; retry reuses the same immutable manifest
and idempotent receipts. `start` independently rechecks the legacy-session gate,
readiness, full campaign audit, and style release freshness before calling
runtime `start_run`, then publishes exactly the active first frontier through
`morning_campaign_display_408`. A display failure preserves the started run as
recoverable `campaign_started_display_pending` rather than starting a second
run.

Freshness and audit

An unstarted prepared run is `stale_style_release` when the currently reopened
release content hash differs from the prepared release hash. It cannot start.
`audit` reopens the control pointer, selection manifest, all segment receipts,
campaign manifest, runtime files, and current release. Drift produces `FAIL`;
history is not rewritten.

Write boundary

Control artifacts are private preparation and runtime state only. Preparation,
status, and audit declare `learner_evidence_write_count=0`; successful start
returns the first display's actual learner-evidence write count. Every path
keeps `formal_write_count=0`. The producer does not call the legacy session
writer, Capture writer, review-loop outcome writer, intake writer, or any
formal knowledge writer.
