Managed Current Question Turn v1

2026-08-30 active contract

This contract supersedes the historical first-answer Capture, immutable-first-bundle,
background-handoff, observation, and trace-supplement route below. Those objects remain
byte-immutable and read-only for exact recovery or audit; no new item may create them.

Prepared A-D, `BLANK`, and `UNCERTAIN` learner responses use only
`answer-current-and-next`. The composition owns
the current-item private buffer, terminal finalization, durable Capture verification,
and optional receipt-gated successor, hold, or session-complete action. There is no
public `current-turn`, `continue-current`, or `next-item` CLI.

Before terminal finalization, root Sol freezes `semantic_assessment`. The deterministic
transaction binds its SHA-256 together with the private choice result, the complete
`interaction_trace_sha256`, `feedback_sha256`, and `terminal_verdict_sha256`.
The choice grader owns only the option or conclusion result and cannot upgrade the
semantic verdict.

`semantic_assessment` accepts only required `reasoning_result`, `first_break`, and
`first_break_provenance`, plus optional `first_action`, `first_action_provenance`, and
`feedback_text`. Supplying `choice_result` or another private-grader field fails before
any buffer, Capture, pending-index, outcome, or navigation write.

The terminal verdict schema is `current-question-terminal-verdict-v1` and binds
`choice_result`, `reasoning_result`, `prompt_level`, `teaching_result`,
`terminal_resolved`, `capture_required`, `semantic_assessment_sha256`,
`interaction_trace_sha256`, and `feedback_sha256`. Terminal classification uses
`capture_required=true` with `capture_reason=terminal_final_package_admission`;
nonterminal classification uses `capture_required=false` with
`capture_reason=awaiting_terminal_resolution`.

`terminal_resolved=true` requires a correct option or conclusion,
`reasoning_result=sound`, no known contradiction, and a prompt level that agrees with
the observed hint/explanation trace. `reasoning_result=partial|diverged|not_observed`
is nonterminal. If learner reasoning, correction, or restatement exists while the
semantic result is `not_observed`, or a hint exists while `prompt_level=none`, fail
closed. A high-confidence unprompted first answer may be terminal only when both its
answer and reasoning pass. Prompted resolution may be terminal but is not independent
mastery.

Every nonterminal first answer, hint, explanation, restatement, and correction appends
an immutable revision to the same private item buffer. The stable identity binds the
session, item, display receipt, source, and answer-operation idempotency key. The
buffer preserves the complete question, options, all ordered user/assistant turns,
the real error entry, every correction round, frozen feedback, missing fields, and all
original or later attachments. It may reject an oversized individual input, but it
never stores only a first/last 24-event or 32-KiB projection. While buffering there is
no Capture, no pending-formal index row, no successor/hold/session completion, and
`formal_write_count=0`.

One serialized writer performs the terminal order exactly:

1. Seal and reopen the final `cs408-conversation-package-v1`, verifying every
   manifest-bound byte and the terminal-verdict binding.
2. Create or idempotently reopen exactly one Capture whose stable evidence reference
   is that final package locator and canonical SHA-256.
3. Reopen and verify the durable Capture receipt, Capture event, final package binding,
   item/display/finalization identity, and `formal_write_count=0`.
4. Only after step 3 may the composition publish one successor, close the last-item
   session, or complete an explicitly frozen `hold_current`/close.

The first terminal answer, last item, and held item all use this same order. Each
session item has one stable finalization identity and at most one Capture. A failure
keeps the same navigation frontier and returns a precise recovery state. Recovery
reuses the same item, display, navigation policy, finalization idempotency key, final
package, Capture payload, and Capture identity; it repairs only the missing layer and
never mints a second Capture.

A successful finalization returns the final package locator/SHA-256, Capture ID,
durable Capture receipt SHA-256, `capture_status=awaiting_sol_formalization`, and
`formal_write_count=0`. `next_item_published` is the sole successor truth. The active
route has `luna_call_count=0`, `background_processing=none`, and no T9, Obsidian,
formal, personalization, Dispatcher, worker, report, consumer, or adoption write.

Historical compatibility contract below

Everything below this line describes pre-2026-08-30 objects only. Its capture
eligibility, background handoff, observation, supplement, and recovery directions are
not active routing authority.

Retired historical composition

This document describes the internal current-turn primitive. Prepared A-D, `BLANK`,
and `UNCERTAIN` learner
answers must not orchestrate it directly. Their sole external entry is
`answer-current-and-next`, whose result schema is
`managed-408-answer-current-and-next-v1`; that composition owns preparation, this
current transaction, and optional receipt-gated `next-item` publication. The
composition defaults to publishing an authorized successor, but an explicit frozen
`hold_current` policy completes only the current item and performs no successor lookup.
The internal primitive is import-only; there is no public `current-turn` CLI.

Order

1. Validate the self-contained current-question context.
2. Freeze the teaching feedback and its SHA-256 before any learning write.
3. If capture is eligible and stable, persist the private evidence bundle.
4. Commit the canonical first-answer evidence with exact review/session receipts.
5. For morning review, commit and verify the current-only feedback receipt.
6. Before an eligible stable capture commit, publish a private
   `current-question-background-handoff-v1` in `teaching_pending`.
7. Commit at most one answer-safe capture with a bounded hot writer.
8. For a first-turn `fragile_correct`, bind its completed private evidence and capture
   receipt and promote the handoff to `first_turn_complete` ready. An initial
   `wrong|partial|uncertain` remains pending for the same-item teaching lifecycle.
9. Return the frozen feedback. Never publish the next item in this internal
   transaction; only the external composition may invoke the internal navigation
   primitive, and only when its frozen policy is `publish_successor`.

Result contract

- First-answer or session-receipt failure returns `recovery_required`, sets
  `feedback_authorized=false`, and omits the frozen feedback body publicly.
- Private evidence, observation, or capture failure keeps
  `feedback_authorized=true`, retains the item, and returns an exact recovery locator.
- `next_display_receipt_sha256=null` and `next_item_published=false` are invariant.
- `luna_call_count=0`, `personalization_read_count=0`, and `formal_write_count=0`.
- Background handoff is private and content-addressed. It never enters the public
  capture row and never causes foreground Luna waiting.
- Capture status is exactly one of `not_eligible`,
  `awaiting_daily_curation`, `capture_pending_recovery`, or
  `capture_unavailable_unstable_evidence`.

The external caller cannot invoke `next-item` or `continue-current`. The unified
composition may invoke its internal navigation primitive only with a prior turn
receipt whose `advance_allowed=true` and whose item is the current session navigation
frontier, and only when the answer operation's frozen navigation policy is
`publish_successor`. The receipt itself must also carry
`navigation_policy=publish_successor`. A `hold_current` terminal receipt carries
`navigation_policy=hold_current` and `advance_allowed=false`, while returning
`next_item_published=false` and `next_item=null`. Eligible
unstable evidence and every pending recovery keep advancement false.

A successful failure capture does not by itself make the first-turn receipt
advanceable. `wrong`, `partial`, and `uncertain` receipts always keep
`advance_allowed=false`; only the later private `teaching_resolved` receipt can
authorize successor publication inside the unified composition. `fragile_correct`
may advance after its capture and ready
handoff are verified.

The active route has no personalization, history, formal projection, related
question, variant, Luna, promotion, trial, or thread gate. Ordinary and morning
current-only outcome writes use `current_question_minimal` as a read-scope boundary,
not as a scheduling-disable flag. When the current display/session binding contains
one exact morning `action_id`, its stage, trigger, queue commitment, and prepared-pack
commitment, the same hot transaction must consume that action at most once and create
at most one deterministic successor. `cross_day` is derived only from the bound
action's `origin_date` and the current `observed_date`. Without that exact binding the
current-only path must not search for an action or infer one from another item; it
records the outcome and leaves scheduling explicitly deferred. Neither branch may
scan the full ledger or read formal, historical, relational, personalization, or Luna
state.

An unanswered item whose exact action was already consumed by a verified earlier
outcome is not presented again. A cold append-only reconciliation may mark it
`scheduling_superseded`; current navigation skips that administrative terminal without
forging an answer, feedback, capture, or Luna request. A stale display receipt for such
an item fails closed instead of rebinding the learner's reply.

Morning compatibility maps the model-facing hint scale onto the canonical review
ledger without changing capture eligibility: `none -> none`, `L1 -> minimal`,
`L2/L3/L4 -> progressive`, and `L5 -> full`. The private current-question bundle
retains the exact L1-L5 level; the morning session and review ledger retain their
existing controlled enum. The same adapter is used for ordinary and evening-D0
current-question outcomes.

`recover-current-capture` applies the result-specific gate order without repeating
the outcome/session write. An independent-correct evidence recovery republishes the
bundle if needed and creates only its observation; it never creates a capture. A
capture-eligible recovery commits or verifies one capture, then promotes only a
first-turn fragile-correct handoff. For a failure result it also atomically repairs or
establishes the private display lifecycle so the same displayed item continues as a
teaching follow-up. Successful recovery closes the original answer operation; only a
verified advanceable terminal whose frozen policy is `publish_successor` may return
the successor. Recovery and replay of `hold_current` remain held. A stale prior receipt
that is not the current navigation frontier fails closed.
