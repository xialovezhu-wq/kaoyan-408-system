# Morning campaign episode terminal v2

The compact facade remains `scripts/morning_campaign_turn_408.py`.

`append-turn` on a v2 run accepts:

```json
{
  "display_receipt_sha256": "<sha256>",
  "idempotency_key": "<stable-key>",
  "expected_prior_turn_count": 0,
  "expected_prior_trace_sha256": "<sha256-of-prior-turn-array>",
  "new_turns": [
    {"role": "user", "kind": "answer", "text": "...", "observed_at": null}
  ]
}
```

`finish-item` accepts the same cursor fields plus:

```json
{
  "terminal_state": "correct|screened_correct|prompted_correct|wrong|unresolved",
  "final_response": "A|B|C|D|BLANK|UNCERTAIN|null",
  "reasoning_result": "sound|partial|diverged|not_observed",
  "prompt_dependency": "none|minimal|progressive|full|not_observed",
  "navigation_policy": "publish_successor|hold_after_capture",
  "frozen_feedback": "optional exact feedback",
  "missing_fields": [],
  "attachments": []
}
```

`finish-item` defaults `navigation_policy` to `publish_successor` when omitted.
`close-unresolved` requires an explicit navigation policy and omits the four semantic fields; the facade supplies
`terminal_state=unresolved`, a null final response, and unobserved reasoning and
prompt dependency. `new_turns` must still contain or extend a real non-empty
episode, such as the user's explicit skip/end utterance.

`hold_after_capture` still seals and verifies the episode and advances the
progress frontier, then pauses the run without publishing or exposing its next
item. `publish_successor` continues normally. If an unresolved close follows an
already observed answer, that real first response remains in the episode; null
first response applies only when the user ends or skips before answering.

`answer-choice` and `finalize-reason` keep their existing shapes. On v2 they
return `episode_pending_terminal` (or `reason_required`) without navigation.
Their optional `learning_turns` are copied into the same episode draft. The
caller then invokes `finish-item` automatically when the item is semantically
terminal; the learner does not need to request saving or the next item.

Success returns `capture_verified=true`, the episode and shared Capture proof,
source and question provenance, exact progress, and the direct `next_display`
or campaign completion. No new Again/Hard/Good value is returned or stored.
Real originals return `execution_kind=original_review`; generated campaign
variants return `execution_kind=concept_supplement` and preserve
`source.origin_lane=campaign_variant`.
