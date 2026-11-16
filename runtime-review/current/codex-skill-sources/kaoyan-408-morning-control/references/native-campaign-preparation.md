# 非D交接的原生准备与旧会话恢复

仅在没有D包/交接，且用户明确要求原生准备或精确旧会话恢复时读取。D包失败不能自动回退本分支。


Treat `开始今天的晨间复盘` as an outcome request to reach the first learner-visible
question. First inspect canonical legacy session state. Any active or paused legacy
queue/pack/session/display has priority over `morning_coverage_campaign_v1`, even when a
new campaign run is ready. Resume or finish that immutable session under its original
contract. Never migrate it, add campaign items to it, close it because a successor is
ready, or replay an outcome.

Only an explicit replacement request may cancel an untouched legacy session through
the supported `morning_review_session.py cancel` route. The session must contain only
`session_started` and display metadata, with no answer, feedback, repair, Capture or
canonical terminal evidence. Preserve queue, pack, session, receipts and pending
actions. If learning evidence exists, use ordinary canonical closeout instead.

After a legacy display, route every A-D, `BLANK`, `UNCERTAIN`, hint, correction and
resume request to `kaoyan-408-daily-study-loop`. The old strict path remains
`answer-current-and-next`: nonterminal turns update one private draft, while terminal
turns seal the final package, create and reopen-verify exactly one Capture, and only
then navigate or hold. Morning control creates no learner package, Capture,
`awaiting_sol_formalization` row, outcome or formal write.

## Create and start a coverage run

When no legacy active/paused blocker exists, use the successor tools rather than the
five-question semantic selector. The verified local interpreter `python3`
has the pinned FSRS dependency; do not change global PATH or silently substitute FSRS.
If D was imported for this date, the existing prepare command consumes its verified
concept-representative run. Full due-node coverage and suppressed carry-over remain in
the receipt; a concept task does not grant outcomes to unanswered nodes. The native
source/exposure/answer-isolation checks still run before every first publication:

`python3 scripts/runtime_paths_408.py --run morning_coverage_campaign_408.py dry-run --repo . --as-of <date> --date <Asia/Shanghai-YYYY-MM-DD>`

Freeze that exact `morning_due_run_v1`, then use the all-or-nothing control entrypoint:

`python3 scripts/runtime_paths_408.py --run start_morning_coverage_campaign_408.py --repo . prepare --date <Asia/Shanghai-YYYY-MM-DD> --details-root <details> --release <release> --private-root <private-root>`

`python3 scripts/runtime_paths_408.py --run start_morning_coverage_campaign_408.py --repo . audit --date <Asia/Shanghai-YYYY-MM-DD>`

`python3 scripts/runtime_paths_408.py --run start_morning_coverage_campaign_408.py --repo . start --date <Asia/Shanghai-YYYY-MM-DD> --private-root <private-root> --idempotency-key <key>`

`prepare` independently binds one answer-isolated canonical original, verified local
formal original, or accepted V2/V3 surface for every frozen due item without changing
the set or order. `start` reruns the full audit, starts the one frozen runtime, writes
one exact exposure receipt, and returns only the first learner-visible question. If
runtime start succeeds but display publication fails, recover the same run and frontier;
never call start with a new identity.

Reuse the same dated run and idempotency identity. `create-run` freezes the formal-node
cohort and all evidence cutoffs. It must reconcile
`included + authority_excluded + blocked = cohort_total`; a blocked or unknown-date node
stays visible in the denominator. Dates come only from append-only review evidence,
verified latest redo dates and first-error dates. FSRS state and exam-date forecasts
remain rebuildable projections, never user facts or formal mastery.

The run includes every overdue, due, latest-safe-day, calibration and carry-over node.
There is no top-N or semantic five-item truncation. Partition the complete ordered run
into immutable segments of at most 16 items. Sixteen is a commitment/state boundary,
not a daily capacity limit. Before `start`, every segment and every learner surface in
the run must be complete, answer-isolated, release-bound and audit-ready. Do not publish
a partial run. Once the audit is green, `start` creates at most one runtime and publishes
only its first display.

Use only accepted `exam_style_release_v1` assets. Direct originals are limited to
verified Q1-Q40 four-choice material; Q41-Q47 are style/content evidence only. A variant
is a practice surface for an existing formal node and never creates a node or expands
knowledge scope. Release drift makes an unstarted run stale but never rewrites history.

## Local concept review and missing variants

A request for one question per due knowledge point does not require a successful
web D import. Use the local native owner to compute the complete due set first,
then select one verified representative per primary concept in native due order:

`python3 scripts/runtime_paths_408.py --run local_concept_review_408.py plan --repo . --date <explicit-target-date> --output <external-details-root>/review-deliveries/<date>/local-plan.json`

The user's explicit date overrides dates suggested by an attached handoff. Keep
the complete due-node denominator, same-concept suppression and carry-over.

`unresolved`, `content_blocked`, or `VERIFIED_VARIANT_UNAVAILABLE` caused by missing
original images, unavailable accepted surfaces, or recent exact-original exposure
is a request to fill the variant bank, not the end of preparation. Preparing the
requested morning review includes generating the needed exam-style V2/V3 practice
for the existing formal nodes. Do not ask the user to accept omitted concepts or
wait merely because no variant is already in the bank.

For these gaps read `<repo>/schema/exam-variant-bank-v1.md` and use the existing
`exam_variant_bank_408.py` prebuild-plan → submit-author → submit-blind-verifier →
submit-adversarial-verifier → publish chain. Reuse the current audited ESR profiles,
canonical anchors and node evidence; the author, blind solver and adversarial
verifier must be independent actual agents. The blind solver receives only the
generated blind input. The root owns acceptance and publication. Preserve the
single correct answer, axis contract, source bindings and answer isolation. Store
full generation material in the external private details root, never the formal
node store. Fill only the required representative gaps; declared 3–5 plan slots
do not require generating unnecessary candidates for this run.

A missing original image does not forbid a source-grounded variant when the node's
mechanism and an allowed exam-style anchor are verified. A genuine unresolved
identity/content premise or rejected verifier must be repaired or reported
specifically; never invent provenance or mark a failed check accepted.

After publication rerun the local owner with `prepare` instead of `plan`; it rejects
any remaining unresolved concept and sends the complete concept view to the same
native campaign preparer. Then use the ordinary audit/start/display path above.
Do not backdate actual exposure or learning events when the target date is past.
An already active or paused runtime remains protected; resume it instead of
replacing its selections. This local route performs no GitHub or paid API request.

## Question provenance label

Every first, resumed and successor display must show the exact
`question_provenance.label` immediately before the image or text stem. A verified
canonical item is labeled `真题｜YYYY 年考研 408 第 N 题`; a generated surface is labeled
`变式题｜基于 YYYY 年考研 408 真题第 N 题`. The base identity must agree across the
runtime item's `source_id` and the frozen selection's original identity.

A verified local formal original without canonical true-exam identity is neither
silently called a true exam nor a generated variant. Show
`来源原题｜非系统生成变式｜真题年份未确认`. Never infer a year from `formal_node_id`,
study date, filename, image text or free prose. Missing or conflicting provenance blocks
a new display before exposure; an old immutable receipt may derive the label read-only
from the current verified frontier and frozen selection without rewriting its bytes.

## Exact-item seven-day gate

The cooldown belongs to exact `true_item_id`, canonical question-asset identity, or
equivalent normalized-stem identity, not to a whole knowledge point. Every verified
learner-visible display establishes exposure, including unanswered, hinted, cancelled,
correct and wrong displays.

- If `gap_days < 7`, forbid the original and any V1 surface clone. Only an already
  accepted V2 structural isomorph or V3 near-transfer surface may be committed.
- If `gap_days >= 7`, the verified original becomes eligible again.
- A different exact item sharing the knowledge point is not an exact duplicate.
- Missing, ambiguous, conflicting or tampered identity/exposure evidence is blocked;
  do not guess from answer date, mtime, pack date or free text.

Refresh this gate before full-run commitment and again before first publication. Any
drift rejects the unstarted run rather than silently changing a committed surface.

## Runtime control

Use `morning_campaign_runtime_408.py --runtime-root <runtime-root> status --run-id <run-id>`
to reopen the named run. Use its `pause`,
`resume` and `carryover` commands for campaign state only; preserve run, segment, item,
display and idempotency identities. Carry-over remains due in the next run and never
disappears from coverage. Run `audit` after creation, before start, after any recovery,
and at completion. After display, `kaoyan-408-daily-study-loop` owns the answer bridge;
never call raw `record-outcome` from a learner turn.

Preparation, audit and status keep `learner_evidence_write_count=0`; a healthy start
reports the single first-display exposure write. Every control path keeps
`formal_write_count=0`. Never delete historical evidence, modify formal nodes or
relations, expose protected answers, or claim ready/started/completed without the
corresponding durable audit and receipt.

## Legacy preparation compatibility

If the protected active legacy session needs its original prepared-pack recovery, keep
the existing five-item contract: reuse the dated job, continue accepted author/verifier
chunks until one immutable ready pack exists, run `start_morning_review_408.py` at most
once, and publish only the first display. A genuine identity, freshness, source,
tampering or non-retryable verifier failure stops without a partial session. Do not run
the successor campaign inside that legacy session.

That compatibility path keeps `model=gpt-5.6-sol` and `reasoning_effort=max`: the
`Sol author` produces the bounded legacy candidate, an `independent Sol verifier`
accepts it, and the single writer publishes only the `first display receipt`.
