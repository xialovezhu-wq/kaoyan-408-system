# 408 Study Episode Capture v1

`scripts/study_episode_capture_408.py` seals one completed learning episode as
the existing private conversation package plus one existing Capture hot-writer
event. It does not create a second evidence ledger.

## Python API

```python
record_episode(repo, payload, *, package_root=None, fault_injector=None)
verify_episode_capture(repo, receipt_ref, *, package_root=None)
```

`record_episode` is terminal-only. Intermediate hints and corrections remain in
the owning same-question draft until the caller has a real terminal outcome.
Every distinct real episode receives a new `episode_id`; a retry of the same
episode must reuse the same ID and identical bytes. Reusing an ID with different
content fails closed.

## Payload

Required top-level fields:

- `schema`: `cs408-study-episode-capture-v1`
- `episode_id`: caller-owned safe ID
- `execution_kind`: `original_review`, `concept_supplement`, or
  `standalone_question`
- `study_date`: real `YYYY-MM-DD` date; timezone is `Asia/Shanghai`
- `source`: non-empty object containing `source_id`; for a standalone new image
  without an external identity, `source_id=null` is allowed and the local writer
  derives a marked local-episode key while preserving `external_source_id=null`
  and the missing-field entry. This is not a fabricated formal/visual/source ID.
  The object may preserve
  `session_id`, `item_id`, subject/year/type, the complete public question and
  options, `surface_kind`, `acceptance_kind`, and other source metadata
- `turns`: complete, ordered, non-empty user/assistant conversation
- `attachments`: array; the Python API accepts exactly one of `path` or `data`
  bytes per row, while the JSON CLI accepts `path`
- `observed_outcome`: exact private outcome object
- `authorization`: non-empty JSON object containing the owning flow's real
  authorization evidence
- `display_binding`: non-empty JSON object binding the episode to the actual
  display or standalone source

Optional fields are `missing_fields`, `identity_hint`, `frozen_feedback`, and
an explicit `timezone` equal to `Asia/Shanghai`.

The exact `observed_outcome` fields are:

```json
{
  "first_response": "private exact text or null",
  "first_result": "correct|incorrect|blank|uncertain|not_observed",
  "final_response": "private exact text or null",
  "terminal_state": "correct|screened_correct|prompted_correct|wrong|unresolved",
  "prompt_dependency": "none|minimal|progressive|full|not_observed",
  "reasoning_result": "sound|partial|diverged|not_observed"
}
```

`first_response` and `final_response` live only in the private sealed package.
The answer-safe Capture does not copy answers, options, explanations, error
content, or a guessed first action. Its `source_facts` include the explicit
`episode_id`, `execution_kind`, and web-enhancement wait markers.

Real original morning questions use `original_review`. Generated variants and
D's independent point supplement use `concept_supplement`; a campaign variant
also retains `source.origin_lane=campaign_variant` and its real run/item/display
identity. This is point-level evidence, not a new original-question attempt or
an original-question FSRS update. Explicitly saved standalone teaching uses
`standalone_question`. Existing active/paused runs keep their original contract.

### D supplement adapter

`d_concept_supplement_408.py commit` accepts
`cs408-d-concept-supplement-session-v2` with the exact top-level fields
`schema`, `subject`, `session_id`, `study_date`, `started_at`, `ended_at`, and
`episodes`. Each episode contains `request_id`, `episode_id`, `turns`,
`attachments`, `observed_outcome`, `missing_fields`, and `frozen_feedback`.
Every displayed request must have exactly one completed episode and every
episode must bind one verified display receipt. The adapter records one
`concept_supplement` Capture per display and emits a progress receipt with no
mastery claim, concept-review commit, question write, original-question FSRS
write, formal write, or personal-summary refresh.

For immediate per-question saving, pass only the just-completed episode and its
one display receipt to `commit`; retain the real session ID and time interval.
Progress receipts bind the submitted request set, so the next question can be
saved under the same session without replaying earlier episodes. A progress
receipt describes only its submitted display set, not completion of the whole
morning plan.

If a session ID already names a sealed legacy `concept-review-session-v1`, the
adapter invokes only that old session's native recovery contract. It does not
create episode Captures for historical sessions.

## Result and verification

The durable receipt returns the episode identity, package locator/hash, Capture
ID/payload/event hashes, joint receipt/manifest/index hashes, `receipt_ref`, and
`receipt_sha256`. `verify_episode_capture` reopens all of those bindings.

Capture processing may later move from awaiting to curating/curated/concept_only.
That does not invalidate its original save proof. Reopening reports the separate
`current_quality_status` and `currently_awaiting_web_enhancement`; it never edits
the original receipt or turns a formalized episode back into a new pending one.

Successful records have:

```text
status = awaiting_web_enhancement
package_verified = true
capture_verified = true
requires_web_enhancement = true
awaiting_web_enhancement = true
formal_write_count = 0
fsrs_write_count = 0
personal_summary_write_count = 0
background_processing = none
```

The Capture uses `cs408_awaiting_web_enhancement_policy_v1`. Ordinary cutoff
backlog planning excludes that policy. After the web return, the enhancement
owner may admit it only through the existing exact `capture_ids` or exact
package-locator subset. No waiting episode is converted into `needs_user` merely
because its web enhancement has not returned.
