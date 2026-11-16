# 既有 standalone JSON 请求的兼容恢复

当前原生聊天的普通及年度题直接走主技能的 mark_study_episode_408.py。以下仅供已有旧请求的恢复与补充。

Use this route only after the user explicitly authorizes `快速入库`,
`将这道题快速入库`, or an equally unambiguous quick-intake request for one real
current 408 question that is not attached to a managed display/session/navigation
surface. Ordinary questions, image reading, hints, explanations, corrections,
restatements and direct teaching remain read-only in `kaoyan-408-question-worker` and
must not call this route.

Build one private JSON input with
`schema=cs408-standalone-question-intake-v1`, `request_id`, `study_date`,
`source`, a complete ordered `conversation`, the available question evidence, and:

```json
{
  "authorization": {
    "schema": "cs408-explicit-fast-intake-authorization-v1",
    "kind": "explicit_fast_intake",
    "user_message": "将这道题快速入库"
  }
}
```

`authorization.user_message` must equal the latest user turn exactly and pass the
controlled whole-command check after NFKC normalization. A negation, question,
conditional statement, quotation, or split keywords do not authorize intake. Keep the
authorization body only inside the private package; public receipts retain only its
kind and bounded hashes/counts.

`timezone` may be omitted and then means `Asia/Shanghai`. Question/options may be
top-level or nested as `source.question` and `source.options`; duplicate forms must
match. `question_identity_hint` is optional private metadata and cannot replace the
question/options/source identity.

Run:

```text
python3 scripts/runtime_paths_408.py --run standalone_question_intake_408.py record --input-json INPUT.json
```

The repository defaults to the current directory. The package root defaults to
`<package-root>`; explicit overrides are
for tests and controlled recovery.

The script alone owns the sequence final package -> reopen/hash verify -> create or
recover exactly one Capture whose `stable_evidence_ref` equals the final package hash
-> reopen and verify the durable Capture receipt and binding -> create one
`awaiting_sol_formalization` row -> combined receipt. Never call Capture separately. Do not
call `managed_408_current_turn.py`, retired router/capture-hot routes, background
workers, formal writers, T9 archive, or Obsidian locator writers.

Seal the complete conversation and available original attachments as they exist when
the explicit quick-intake request arrives. The same `study_date`, question, source,
and authorization identity has exactly one package and at most one Capture even if a
caller changes `request_id`. The same question on another study date is a distinct
learning episode. If interruption occurs after package or Capture commit, replay the same
item and authorization identities to recover only the missing step. Reopen an already
durable Capture rather than creating a second one. The initial-intake schema still rejects changed evidence. For a later explicit
supplement use the append-only schema below and preserve the original Capture.

Success means `awaiting_sol_formalization`, `session_id=null`,
`formal_write_count=0`, `background_processing=none`, and a combined receipt whose
`captured_at` is bound to the canonical capture event with
`time_quality=capture_event_committed_at`. Missing solution/explanation remains in
`missing_fields` and does not block the local package.

A later explicit formal request uses `kaoyan-408-daily-intake-curation`:
`backlog-plan` selects the authorized `awaiting_sol_formalization` Capture, then one
single-date `start-batch` freezes it. The quick-intake turn itself never runs those
commands or grants formal-write authority.

## Explicit supplement of a sealed standalone episode

For an explicit “补充入库 / 补充这道题 / 更新这道题” request, use the same `record`
entry with `schema=cs408-standalone-question-supplement-v1`, a new `request_id`,
`capture_id`, `parent_package_locator`, `parent_package_sha256`, exact `new_turns`
and the same authorization object bound to the latest user turn. A normal explicit
quick-intake command also authorizes this schema. Optional fields are new `attachments`,
`frozen_feedback`, and `missing_fields`; omitted missing fields inherit the parent.
Do not supply a new date, source, question or options. Code inherits the original
identity, copies the entire parent dialogue and attachments, and seals a new cumulative
package. Original packages, event bytes and the unique Capture remain intact.

The owner appends one `capture_supplemented` event to the existing Capture ledger.
Identical request/payload retries (including after package, event or receipt failure)
recover the missing step; the same evidence under another request ID is deduplicated.
Use the latest returned package as parent for another supplement. A stale parent,
changed request payload or active formal batch fails closed. Do not disguise a supplement
as a new source or study date. This applies to both pending and already formal episodes.

An explicit formal request freezes `batch.supplement_evidence[capture_id]` and
`supplement_prior_formal_ids[capture_id]`; the last ref is the cumulative package.
Successful `mark-result` consumes only the frozen refs. Existing formal identity and
learning date stay fixed; supplementation does not add a recurrence. Old committed
result retries cannot consume a later revision. No separate pending-state database is used.

Question, option, dialogue and frozen feedback text preserve exact whitespace and CRLF.
Normalization is used only for question identity comparison, never stored evidence.
