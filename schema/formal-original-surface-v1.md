# Formal Original Surface v1

## Purpose

`formal_original_surface_408.py` turns an existing formal node's local A-D
original-question evidence into a reusable, answer-isolated learner surface.
It is a fallback between verified true-exam surfaces and generated V2/V3
surfaces. It never creates or edits a formal node.

The store is rooted at:

```text
DETAILS_ROOT/formal-original-surfaces/v1/
```

All published objects, selections, verifier records, quarantines, and
consumption receipts are immutable JSON objects named by the SHA-256 of their
pretty-printed canonical content.

## API and commands

Python API:

```python
prepare_submission(details_root, submission)
publish(details_root, submission, blind_verification, source_verification)
pool_status(details_root)
select_surface(
    details_root,
    review_date=...,
    formal_node_id=...,
    formal_node_sha256=...,
    exposure_set=...,
    campaign_commitment_id=...,
    exact_exposure_identity=...,
)
commit_consumption(details_root, selection_ref)
```

Equivalent CLI commands are `prepare-submission`, `publish`, `pool-status`,
`select-surface`, and `commit-consumption`.

## Submission contract

`formal_original_surface_submission_v1` binds:

- `formal_node_id` and `formal_node_sha256`;
- `details_card_ref` and `details_card_sha256`;
- `question_asset_refs[]` and their SHA-256/size commitments;
- non-empty `solution_asset_refs[]` and their SHA-256/size commitments;
- verified `study_date` or `null` when unrecorded, plus a stable `exact_exposure_identity`; never infer the date from mtime or the current campaign;
- `question_kind=single_choice_4` and exact `option_labels=[A,B,C,D]`;
- either at least one question asset or an `answer_safe_content` object.

`answer_safe_content`, when present, contains only `stem` and four ordered,
distinct `{label,text}` options. Answer-like phrases are rejected. The details
card is bound as bytes only; text in that card never provides the answer key.

`prepare-submission` verifies current bytes, normalizes refs and sizes, freezes
the content-addressed submission, and creates two packets:

- `formal_original_blind_input_v1`: question surface only, no answer and no
  solution assets;
- `formal_original_source_solution_input_v1`: question and solution assets,
  with `details_text_answer_authority=false`.

## Independent verification

The blind result has schema
`formal_original_blind_solve_verification_v1` and requires:

```json
{
  "verification_id": "stable-id",
  "verifier_identity": "independent-identity-a",
  "submission_sha256": "64 lowercase hex",
  "option_labels": ["A", "B", "C", "D"],
  "derived_correct_option": "A",
  "unique_option": true,
  "verdict": "accept",
  "answer_key_exposed": false
}
```

The source result has schema
`formal_original_source_solution_verification_v1` and requires:

```json
{
  "verification_id": "stable-id",
  "verifier_identity": "independent-identity-b",
  "submission_sha256": "64 lowercase hex",
  "option_labels": ["A", "B", "C", "D"],
  "derived_correct_option": "A",
  "unique_option": true,
  "verdict": "accept",
  "verified_solution_asset_sha256s": ["64 lowercase hex"]
}
```

The verifier identities must differ. Both must independently select the same
unique A-D option. The source verifier's solution hash list must exactly equal
the submission's solution assets in order. Identity collision, key conflict,
or source binding conflict produces a content-addressed quarantine record and
no learner surface.

## Published objects and answer isolation

The stable `true_item_id` is `FOS-` followed by 24 uppercase hexadecimal
characters derived from the formal node binding, details-card hash, question
surface commitment, and exact exposure identity. `study_date` is intentionally
excluded: correcting or recovering a historical date must not silently create
a second exact question identity.

`formal_original_learner_surface_v1` contains question asset commitments and/or
the answer-safe stem/options. It contains no correct option, answer, solution
assets, verification reasoning, or details-card text.

`formal_original_answer_capsule_v1` is written under a mode-0700 private
directory as a mode-0600 content-addressed object. It contains the correct
option and commitments to both verifier objects. Public manifests and
selections include only its ref and SHA-256 commitment; selections do not load
or copy the capsule.

`pool-status` reopens the full published chain and current source bytes. Any
manifest, learner-surface, capsule, details card, question asset, or solution
asset drift changes the entry to `quarantine`.
Verified pool rows also expose answer-safe question-asset SHA-256 values and a
normalized stem/A-D identity commitment so legacy display receipts can recover
the exact `FOS-` exposure without reading an answer capsule.

## Selection, cooldown, and consumption

`select-surface` requires an exact formal node ID/SHA, review date, exposure
set, and campaign commitment ID. Optional `exact_exposure_identity` narrows the
pool. Exposure rows use `true_item_id` plus `display_date` or `review_date`.

For the exact `FOS-` item:

- gap days 0 through 6 are blocked;
- gap day 7 is eligible;
- future exposure dates fail closed.

Selection writes a content-addressed `formal_original_surface_selection_v1`.
It includes the learner surface and answer-capsule commitment, never the key.
Selection does not itself consume the surface.

An unstarted preparation rebuild may set
`allow_existing_campaign_reservation=true`. This only reuses the same
`(campaign_commitment_id, true_item_id)` reservation and produces the same
selection identity; a newly discovered `gap_days < 7` exposure still wins and
blocks that original. It cannot move the surface to another campaign.

`commit-consumption` reopens the selection and current pool chain, then writes
one `formal_original_surface_consumption_v1`. The uniqueness key is
`(campaign_commitment_id, true_item_id)`: identical replay is idempotent; a
different selection for the same campaign/item is a conflict. A later campaign
may reuse the same original after the exact seven-day exposure gate permits it.

Every object reports `formal_node_created=false` where relevant and
`formal_write_count=0`.
