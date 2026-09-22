# Managed 408 answer current and next v1

The 2026-08-30 terminal-finalization contract is authoritative. Historical results
that already contain a first-answer Capture, observation, background handoff, or trace
supplement remain read-only compatible, but those shapes are not created for new
items.

This remains the sole learner-turn entry for each first answer and same-question
correction:

```text
python3 scripts/managed_408_current_turn.py --repo <repo> --private-root <legacy-runtime-root> answer-current-and-next --display-receipt-locator <current-question-turn://sha256/...> --choice <A|B|C|D|BLANK|UNCERTAIN> --confidence <high|medium|low> --prompt-level <none|L1|L2|L3|L4|L5> --navigation-policy <publish_successor|hold_current> --trace-json '<complete JSON list>' --attachments-json '<JSON object>'
```

Routine learner turns target a complete visible reply within 60 seconds. The known
entry is used directly; a healthy path does not inspect CLI help, source, tests, or
schema at runtime and does not delegate package/capture writing.

Interaction-trace `kind` is limited to:

```text
utterance | first_action | reasoning | hint | correction | restatement | answer
```

The displayed question is an assistant `utterance`; `question` is invalid. An invalid
kind is rejected before any first answer, package, capture, session outcome, or
navigation write. The caller may correct the enum and retry once with the same display
and idempotency identity. A second rejection returns the exact blocked state promptly;
it does not trigger exploratory repository reads.

The command preserves the existing display receipt, feedback freeze, first-answer and
session receipts, same-question correction, and navigation frontier. It never
publishes a successor until those receipts authorize it.

Root Sol supplies a frozen `semantic_assessment`; the prepared operation binds
`semantic_assessment_sha256`, `interaction_trace_sha256`, `feedback_sha256`, and
`terminal_verdict_sha256`. The private evaluator supplies only the choice/conclusion
result. `terminal_resolved=true` requires the answer to be correct,
`reasoning_result=sound`, no known contradiction, and prompt dependence consistent
with the observed trace. A correct option with partial, diverged, or unobserved
reasoning stays on the same display and creates no Capture.

Navigation policy is explicit and immutable for one answer operation. The default is
`publish_successor`. When the learner says
not to show another question or to finish after the current item, use `hold_current`
in that same call. A successful hold still seals the final complete package, creates
and verifies the item's sole Capture, and commits the required current-item/close
receipt, but performs zero successor lookup or publication. It returns
`status=feedback_ready_current_held`,
`next_item_published=false`, and `next_item=null`; the current turn receipt is frozen
with `navigation_policy=hold_current` and `advance_allowed=false`. The internal
`next_item` primitive accepts only a receipt with
`navigation_policy=publish_successor`, including after session resume or recovery.
An explicit later request to continue uses
`resume-held-session --held-turn-receipt-locator ...`, which creates a new
publish-successor authorization bound to the immutable held receipt; it never changes
the original answer operation or makes the held receipt advanceable.

The policy is part of the operation identity and is also frozen in the prepared
operation. For backward-compatible hashing, omission is the canonical spelling of
the historical default and only `hold_current` adds an explicit policy field to the
hash material. A prepared or completed operation cannot switch policy. The first
advanceable turn, a later teaching-resolution turn, and capture recovery all read the
same frozen value. Replay and recovery of a held operation never publish a successor;
a cross-policy retry for the same operation/display fails closed before another
learner-evidence write.

Every nonterminal branch only advances one private buffer revision for the same
`cs408-conversation-package-v1` identity. It binds the complete question/options, all
ordered user/assistant turns, frozen feedback, correction turns, and original or later
attachments. It creates no Capture and no pending-formal index row. New traces are not
truncated at 24 events or 32 KiB. Missing attachments or metadata remain explicit
gaps.

When the terminal gate passes, the operation first seals and reopens that final
package, then creates exactly one Capture bound directly to its canonical SHA-256,
then reopens and verifies the durable Capture receipt. Only after those steps may it
publish a successor, complete the last-item session, or finish an explicit hold/close.
A Capture failure keeps the current item and navigation frontier. Recovery reuses the
same item/display/finalization idempotency and Capture identity.

The external result includes:

- `schema=managed-408-answer-current-and-next-v1`
- `navigation_policy=publish_successor|hold_current`
- feedback and navigation fields already authorized by the learner receipts
- `next_item_published` as the only successor-publication truth
- nonterminal `capture_id=null`, no pending index, and the current buffer locator/hash
- terminal `capture_id`, `capture_status=awaiting_sol_formalization`, durable Capture
  receipt SHA-256, and final `conversation_package_locator`/canonical SHA-256
- `terminal_resolved` and `terminal_verdict_sha256`
- `terminal_verdict.schema=current-question-terminal-verdict-v1`, with
  `capture_reason=terminal_final_package_admission|awaiting_terminal_resolution`
- `formal_write_count=0`

New results do not contain model-processing authority, report, or adoption fields.
The learner turn never writes T9-Data or an Obsidian locator note.

New results also do not contain or create a trace supplement. The final package itself
is the complete Capture evidence. Existing supplement fields are accepted only while
reopening an immutable historical operation.

Historical private evidence and turn receipts remain read-only compatible for exact
recovery. They are not upgraded by inventing missing conversation turns.

Legacy source-title recovery

New prepared packs must publish a stable machine `source_id`. For one already
published legacy morning surface, `answer-current-and-next` may recover an
invalid human-readable source title only from the exact display receipt's
verified scheduling binding. The display binding must equal the live immutable
session/pack binding and contain one canonical `formal_node_id`; that ID becomes
the machine `source_id` for the current context, conversation package, and local
pending-formal index.

Historical display receipts that predate the embedded scheduling field may use
the same recovery only after a normal preparation attempt fails specifically as
`source_id_invalid`. The consumer then reconstructs the scheduling binding from
the receipt's exact session/item, the immutable prepared pack, and the identical
surface SHA-256. This route is recorded as
`reconstructed_legacy_display_session_pack_binding`; it is not available for
another preparation error.

The complete private conversation package retains the title and recovery basis
under `source.source_identity_provenance`. This compatibility path does not
change the displayed surface, grader decision, scheduled action, session/item
identity, formal-node binding, or navigation frontier. Missing, invalid, or
conflicting fallbacks return `source_id_invalid` before any answer, package,
capture, outcome, or navigation write. A prepared or completed answer operation
reuses its already-frozen identity decision and never re-resolves it during
idempotent replay.
