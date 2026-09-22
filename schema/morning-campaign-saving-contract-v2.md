# Morning campaign saving contract v2

Newly prepared morning campaign manifests bind this complete immutable object and
its canonical SHA-256:

```json
{
  "schema": "morning-campaign-saving-contract-v2",
  "mode": "episode_capture_every_item",
  "episode_capture_schema": "cs408-study-episode-capture-v1",
  "capture_before_advance": true,
  "canonical_review_write": false,
  "fsrs_write": false,
  "personal_summary_write": false,
  "formal_write": false,
  "background_processing": "none"
}
```

The contract participates in `run_id`, is copied to `run.json`, and its hash is
included in `run_started`. A manifest without these fields is a legacy contract;
existing active, paused, or failure-pending runs retain that path and are never
migrated or replayed.

For v2, the private grading capsule determines only the observed choice result.
`campaign_response_observed` then freezes the real first response separately
from the later reasoning judgment. It advances no frontier and writes no rating.
Every terminal state must call the shared `study_episode_capture_408.record_episode`,
reopen its package/Capture/joint receipt, and append
`campaign_episode_finalized` before navigation. A real original uses
`execution_kind=original_review`. A campaign variant uses
`execution_kind=concept_supplement` plus `source.origin_lane=campaign_variant`,
so its knowledge-point evidence cannot change the linked original's review date,
error count, or FSRS state.

The full ordered dialogue and every available question, explanation, solution,
self-check, and user-work attachment belong to that episode. Hints and corrections
remain in the same private draft until terminal. An explicit skip/end is
`terminal_state=unresolved`, `first_response=null`, and `final_response=null`;
the system must not invent a corrected response. If the item already has a real
first response, an unresolved close preserves it; null first response is only
for ending or skipping before any answer.

Completion increments only `episode_outcomes`. `ratings` stays all-zero, and the
path must not call canonical review, FSRS, formal intake, personal-summary refresh,
publication, or background processing.
