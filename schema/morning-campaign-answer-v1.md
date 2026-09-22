Morning campaign answer bridge v1

Purpose

`scripts/morning_campaign_answer_408.py` is the answer-safe boundary between a
published successor-campaign display, a private deterministic grading capsule,
and `morning_campaign_runtime_408.record_outcome`.

Inputs

Every operation binds:

- the exact private display receipt file and its SHA-256;
- the exact private grading capsule file;
- `run_id`, campaign date, segment, item, formal node, true item, surface kind,
  surface SHA-256, grading SHA-256, item commitment, and runtime manifest;
- the current active runtime frontier;
- explicit `recognized_original` and explicit measured seconds or `null`;
- one idempotency key.

The CLI derives the private grading capsule from the verified display receipt by
default. `--grading-capsule-path` is an optional compatibility override; supplying a
different path still fails the original binding checks. A model need not reopen or
read answer material merely to repeat its pathname.

The display receipt may be a direct
`morning-campaign-display-receipt-v1`, or a
`current-question-turn-receipt-v1` envelope whose `receipt_schema` is
`morning-campaign-display-receipt-v1`. The accepted published statuses are
`display_published` and `campaign_display_published`.

Response normalization

The public user response is exactly one of `A`, `B`, `C`, `D`, `BLANK`, or
`UNCERTAIN`. Confidence is `low`, `medium`, or `high`; omission means `high`.
`BLANK` maps to the runtime choice result `partial`; `UNCERTAIN` maps to
`uncertain`. A-D is compared only inside the bridge with the hash-verified
private capsule.

For canonical originals and verified local formal originals, the capsule
supplies `correct_option=A-D`. A local formal capsule additionally binds the
exact `FOS-` true item and formal node. For a verified variant, the capsule
supplies a stable `correct_option_id` and the display receipt supplies the
answer-free `option_ids_by_letter` permutation.
The public surface and public response never contain the correct option or any
grading locator or commitment.

State machine

1. The bridge validates the immutable display receipt and current frontier.
2. It reopens the private grading capsule and determines `choice_result`.
3. Incorrect, blank, and uncertain results are committed as `Again` and remain
   `failure_pending_capture` until the existing durable Capture finalization
   transaction completes.
4. A correct response requires a reason when selected by the stable hash
   sample, marked mandatory, not high confidence, a first variant, a recent
   recurrence, or recognized as either kind of original. `answer_choice` then returns
   `reason_required`, writes no runtime outcome, and does not advance.
5. `finalize_reason` accepts only a root-Sol-frozen `reasoning_result` plus the
   real prompt dependency. The bridge never infers semantic soundness from the
   selected option.
6. A correct, unforced response is committed as screened `Hard`. A sound,
   unprompted audited response is committed as `Good`; failures remain `Again`.
7. `finalize_failure_and_next` reopens a durable Capture receipt through the
   runtime finalizer. Only a verified finalization may advance and publish the
   next display; a rejected finalization leaves the current display in place.

The steps above remain the legacy behavior for manifests without a v2 saving
contract. With `morning-campaign-saving-contract-v2`, `answer_choice` freezes the
real first response and private capsule-derived choice result, returns
`episode_pending_terminal`, and does not write canonical review or a rating.
`finalize_reason` only freezes the current reasoning judgment. The owning facade
then uses `finish-item` or `close-unresolved` from
`morning-campaign-episode-terminal-v2.md`; every item advances only after its
shared episode Capture has been reopened and verified.

Navigation

After any non-failure runtime outcome advances, the bridge calls an injectable
display publisher. Production defaults to the delayed import
`morning_campaign_display_408.publish_current_display`. The next public display
is returned as `next_display`. If the advanced item was last, the response is
`status=complete`, `campaign_complete=true`, and no publisher is called.

Idempotency

Bridge receipts are private, content-addressed, and indexed by the caller's
idempotency key. A byte-equivalent replay returns the original public response
even after the runtime frontier advanced. Reusing the key with changed inputs
fails closed. Runtime writes use a derived stable idempotency key, so recovery
after a bridge-receipt interruption cannot duplicate a canonical outcome.

Public response

`morning-campaign-answer-response-v1` exposes only answer-safe fields: status,
campaign and item identity, derived choice result, confidence, recognition and
timing facts, reason requirement, rating, Capture requirement, and advance
state. It never returns the user-selected letter, correct letter, capsule
locator, grading SHA-256, answer SHA-256, or raw runtime payload.
