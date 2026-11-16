# Successor campaign current-turn commands

Load once before first display. This owner applies only after the campaign answer
bridge returns `capture_required=true` and `failure_pending_capture`. It does not
replace legacy sessions or standalone intake. Routine reads and execution stay in
the root; do not ask a child to read or repeat this contract during a learner turn.

Routine entry is the installed one-call helper in
`../kaoyan-408-daily-study-loop/references/one-call-turn.md` (from the skill directory).
It supplies fixed roots, schema and verified run/item/display path. Submit only the
dynamic fields described below; do not handwrite a Python/file/subprocess wrapper.
Submission and authorized next-image loading run within the same `functions.exec`.
The explicit owner commands below remain compatibility and exact recovery references.

## Append the new turns, preserve the whole conversation

```text
python3 scripts/runtime_paths_408.py --run morning_campaign_failure_terminal_408.py append-turn --input-json TURN.json
```

The private input uses repo/runtime/draft roots only; `package_root` is not accepted
for buffering. Example for the first draft (replace identities and exact turn text):

```json
{
  "schema": "morning-campaign-failure-append-input-v1",
  "repo_root": "<repo>",
  "runtime_root": "<repo>/.private/morning-campaign-runtime",
  "draft_root": "<package-root>/.campaign-failure-drafts-v1",
  "display_receipt_path": "/absolute/private/display.json",
  "display_receipt_sha256": "64-lowercase-hex",
  "run_id": "the-exact-published-run-id",
  "item_id": "the-exact-current-item-id",
  "expected_prior_turn_count": 0,
  "expected_prior_trace_sha256": "4f53cda18c2baa0c0354bb5f9a3ecbe5ed12ab4d8e11ba873c2f11161202b945",
  "new_turns": [
    {"role": "assistant", "kind": "utterance", "text": "exact displayed assistant turn", "observed_at": null},
    {"role": "user", "kind": "answer", "text": "exact learner answer and reasoning", "observed_at": null},
    {"role": "assistant", "kind": "hint", "text": "exact feedback that will be displayed", "observed_at": null}
  ],
  "idempotency_key": "one-new-key-for-this-append"
}
```

For an existing draft, use the prior response's `turn_count` and `trace_sha256` and
send only subsequent exact turns. Existing `buffer-turn` v1 drafts are supported
without rewriting their history. The code verifies the old state and every immutable
turn object, compares the expected prefix, assigns ordinals and assembles the full
cumulative trace. `new_turns` never contain `ordinal` or summarized old dialogue.

Preserve every related visible user/assistant turn, including intervening hints and
restatements. The initial request must include the actual displayed assistant
`utterance` first; do not invent it from metadata. Keep unknown timestamps `null`.
Allowed roles are `user|assistant`; kinds are
`utterance|first_action|reasoning|hint|correction|restatement|answer`.
`question` is not a valid kind. `text` preserves its original whitespace and Unicode.

Healthy output is `status=buffered`, the same run/item, `turn_count`, `trace_sha256`,
and zero Capture/formal/navigation/T9/Obsidian writes. Reuse this cursor on the next
turn; a replay returns the original operation's cursor, not a later draft's cursor.

## Recover a missing cursor, not the full dialogue

```text
python3 scripts/runtime_paths_408.py --run morning_campaign_failure_terminal_408.py draft-cursor --input-json IDENTITY.json
```

`IDENTITY.json` contains exactly the three roots plus `display_receipt_path`,
`display_receipt_sha256`, `run_id`, `item_id`; no `schema` or semantic fields.
It verifies the current pending item and returns only the hash/count cursor without
creating files or locks. Use this only when the current cursor is unavailable.

## One terminal command

Once the corrected conclusion is right, reasoning is sound, contradictions are gone
and prompt dependence is truthful:

```text
python3 scripts/runtime_paths_408.py --run morning_campaign_failure_terminal_408.py finalize-appended-and-next --input-json TERMINAL.json
```

Use the same compact fields, but set
`schema=morning-campaign-failure-terminal-append-input-v1`, add
`package_root=<package-root>`, and supply:

- `corrected_response`: the actual corrected A-D choice;
- `reasoning_result`: `sound`, never inferred from the choice alone;
- `prompt_dependency`: `none|minimal|progressive|full`, matching the real trace;
- `confidence`: `low|medium|high`;
- `frozen_feedback`: exact final feedback;
- `user_error_entry` and `user_error_provenance=user_report|visible_evidence|not_observed`;
- `first_action` and `first_action_provenance=user_report|visible_evidence`;
- `missing_fields`: explicit list, empty when none;
- `attachments`: explicit list, empty when no additional current originals;
- one stable terminal `idempotency_key`, distinct from earlier appends.

`user_error_entry` and `first_action` go into an answer-safe Capture index. Use short
mechanism descriptions, for example `混淆元素序号与数组下标` and
`先按行累计已存元素数量`; do not copy chosen/correct option letters, answer values,
calculation chains or explanations into these two fields. The full actual user answer
belongs in `new_turns`, and full teaching feedback belongs in `frozen_feedback`.
If error provenance is `not_observed`, the error entry must be exactly `未观察到`.

The owner applies the existing answer-safe scanner before admitting these summaries
to the index. A protected summary becomes an explicit `详见已封存会话包` notice with
`capture_user_error_entry_summary_deferred` or `capture_first_action_summary_deferred`.
This means the index summary is deferred, not that the original evidence is missing:
formal curation reopens the bound private package. Provenance is retained; no error
cause is guessed. The validator remains strict and complete private dialogue remains
unchanged. This projection also lets a previously rejected protected summary recover
with the identical terminal input, journal and sealed package, without a new key.
The actual locator/SHA is in Capture `stable_evidence_refs`, not a filesystem path
inside the notice. A deferred error summary must never become a formal `first_break`:
later backflow requires a package-grounded direct assessment when no canonical review
event provides one. A notice is not evidence of an error mechanism.

`new_turns` contains only new actual turns. It may be empty at terminal only when the
verified old trace is already complete and nonempty. Never truncate it for speed.
The same owner performs the original sequence:

```text
exact full trace → semantic terminal gate → sealed package and hash reopen
→ one package-bound Capture → canonical Capture/hot proof reopen
→ v2 campaign receipt → runtime finalization → direct successor display
```

Only `failure_finalized` with an authorized `next_display`, or `complete` with none,
permits advancement. Never construct a campaign receipt or call raw
`finalize-failure-and-next`, standalone or legacy capture writers for this path.

## Original attachments

The owner automatically preserves every verified `question_assets` page in a new
display receipt. A historical receipt without that array retains only its verified
first-page binding; explicitly add other available original pages, never infer them
from the Markdown. Additional attachments use this exact shape:

```json
{
  "source_path": "/absolute/private/file.png",
  "sha256": "64-lowercase-hex",
  "role": "question_image|solution_image|explanation_image|user_work_image|source_article_image|other_attachment",
  "mime_type": "image/png",
  "label": "stable private label"
}
```

## Recovery and full-input compatibility

Replay the identical compact JSON and idempotency key after an uncertain result.
The adapter sealed the exact assembled input before invoking the owner, so a retry
does not append twice or rebuild from a newer draft. Recovery fills only the first
missing layer, including a package interruption or runtime-advanced/display-failed
terminal. A farther frontier still blocks; it does not authorize another next item.

Stale hash/count, changed same-key input, a missing old turn object or a tampered saved
input fails closed. Read the current cursor only to prepare a genuinely new operation,
never to change a retry. Do not repair these errors by scanning source/help/other packs.

The existing commands remain supported:

```text
python3 scripts/runtime_paths_408.py --run morning_campaign_failure_terminal_408.py buffer-turn --input-json TURN.json
python3 scripts/runtime_paths_408.py --run morning_campaign_failure_terminal_408.py finalize-and-next --input-json TERMINAL.json
```

Their schemas remain `morning-campaign-failure-turn-input-v1` and
`morning-campaign-failure-terminal-input-v1`. They take full cumulative `turns`,
including continuous `ordinal` values from 1, instead of compact run/item/cursor and
`new_turns`; all roots, display and terminal semantic fields retain their old meanings.
Preserve original full-input recovery files and keys; do not switch APIs mid-retry.

A package directory without its sealing receipt may resume the same operation.
A sealed receipt with missing or hash-drifted bytes is tamper evidence, not permission
to rewrite history. Both APIs retain the same sole package/Capture/navigation owner
and zero formal, T9, Obsidian, background and duplicate writes.

The deterministic terminal rejects `prompt_dependency=none` when the complete draft
contains assistant `kind=hint`. It does not infer arbitrary reasoning soundness from
keywords. The root must assess the real final restatement and actual dependency level;
receipt verification proves durable bindings, not natural-language reasoning quality.
