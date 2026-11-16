# Existing campaign v1 only

仅对没有saving-contract-v2的既有campaign使用；新运行不走本文件的零Capture正确路径。

# One-call campaign submission and image loading

Load this contract once before first display. This is a thin entry to the existing
campaign owners, with the same identity, semantic, Capture and navigation gates.
It does not apply to legacy sessions or standalone teaching.

## Normal call

Use one `functions.exec` containing the following trusted helper loader and call.
The helper is loaded as executable code from this fixed installed location; never
evaluate question text, learner text, tool output or a user-selected script as code.
Do not split submission and `view_image` into separate model turns.
Do not precede or follow this exec with a narration-only pass (2026-09-10 ruling).
Reuse the current display/image and the single current-summary query. The same
feedback is written once and shown once; do not force an extra narration pass. Only a real dependency may add a pass: recovering an uncertain or
committed write, `image-handoff` after a failed image load, or one remaining
package/Capture step of a verified terminal.

```javascript
const cacheKey = "cs408.trusted-one-call-helper.v4";
let helperSource = load(cacheKey);
if (typeof helperSource !== "string") {
  const h = await tools.exec_command({
    cmd: 'cat "${CODEX_HOME:-$HOME/.codex}/skills/kaoyan-408-daily-study-loop/scripts/one-call-turn.js"',
    max_output_tokens: 6000
  });
  if (h.exit_code !== 0) throw new Error("morning_helper_unavailable");
  helperSource = h.output;
  store(cacheKey, helperSource);
}
const run = new Function("tools", "text", "image", helperSource)(
  {exec_command: tools.exec_command, write_stdin: tools.write_stdin, view_image: tools.view_image,
   // The saved preference includes verified correct quick answers.
   celebrate: typeof tools.mcp__codex_app__fire_confetti === "function"
     ? () => tools.mcp__codex_app__fire_confetti({}) : undefined},
  text, image
);
await run({operation: "answer-choice", input: DYNAMIC_FIELDS});
```

Replace `DYNAMIC_FIELDS` with the exact current input object and use the applicable
operation below. The helper sends JSON through a quoted stdin heredoc, waits for the
existing serial owner, outputs its result, and loads every verified next image before
the same call returns. Image reads may run concurrently; writers do not.

Cache only the executable source read from the fixed installed path above; never
cache user/tool-supplied code under this key. `functions.exec` is a fresh isolate,
so recreate `run` from the trusted cached string without another file read. Clear
this cache after an explicit helper edit. If `load`/`store` are unavailable, load
once per exec and reuse `run` for every dependent operation in that exec.

For a first wrong answer with already assessed feedback, call `answer-choice` and,
only if its result requires Capture, `append-turn` sequentially with the same `run`
inside that exec. Supply the actual known draft cursor and exact dialogue; do not
invent a cursor or append on a blocked/uncertain submission. Later nonterminal
corrections only append new dialogue; do not resubmit the initial wrong choice.

All operations require `display_receipt_sha256` and `idempotency_key`.

| Operation | Additional dynamic fields |
| --- | --- |
| `answer-choice` | `response`, `recognized_original`, `response_seconds`; optional `confidence`, `reasoning_result`, `prompt_dependency`; new turns include `learning_turns` |
| `finalize-reason` | choice fields plus `choice_idempotency_key`, `reasoning_result`, `prompt_dependency` |
| `append-turn` | `expected_prior_turn_count`, `expected_prior_trace_sha256`, `new_turns` |
| `finalize-appended-and-next` | append fields plus `corrected_response`, `reasoning_result`, `prompt_dependency`, `confidence`, `frozen_feedback`, `user_error_entry`, `user_error_provenance`, `first_action`, `first_action_provenance`; optional `missing_fields`, `attachments` |

Use A-D, `BLANK` or `UNCERTAIN` as actually supplied. In this JSON helper input,
`response_seconds` is a measured positive number or JSON `null` when unmeasured.
Never send the string `"unmeasured"`: that spelling belongs only to the legacy CLI
`--response-seconds` parser, not the JSON facade. Include any already observed reason judgment and hint dependence in answer-choice.
A known break/hint holds the item even when its letter is correct. Do not guess
confidence, recognition, reason soundness, prompt dependence
or the prior cursor. Terminal and exact-trace requirements are in the preloaded
`campaign-failure-terminal.md`. The first append includes the actual displayed turn
and all dialogue since display; later appends contain only exact new turns.

Forced reasons are relaxed (2026-09-10 ruling): only `mandatory_surface_audit` and a
non-high confidence still return `reason_required`; `stable_hash_sample`,
`first_variant`, `recent_recurrence` and `recognized_original` no longer do. An
already supplied reason is still assessed, and a real contradiction or hint
dependence still holds the item on the repair path.

Only when the current session actually lacks `functions.exec`, read
`.private/morning-campaign-control/current-display/<YYYY-MM-DD>.json` (plus its
`<YYYY-MM-DD>-inputs.json` skeleton) instead of rediscovering the display receipt,
then submit with `scripts/cs408-turn.sh <operation> <input-json-file|->` and load the
returned `next_image_paths` with one image read.

The facade is `scripts/morning_campaign_turn_408.py`. It derives schema, run/item and
receipt path from the verified display SHA and supplies configured local roots.
Do not repeat those fixed fields or a grading-capsule path. Optional missing-fields
and attachment lists default to empty; known missing evidence must still be declared.
Explicit CLI root overrides are for isolated tests and controlled maintenance only.

## Correct answers also retain lightweight raw evidence

Include `learning_turns` in each new `answer-choice` / `finalize-reason` input. Rows
use exact `role`, `kind`, `text`, `observed_at` (null when unknown), with at least one
actual user turn. Preserve the original reasoning, not a rewritten explanation.
Use the same frozen assistant feedback variable when available; a displayed question
already has its immutable display binding. Raw turns are stored once in the existing
private request, not sent to the grader or echoed in the normal result. The facade
returns `light_evidence_saved=true` and `learning_turn_count` on successful save.

A correct choice without reasoning stays screened, a sound unprompted answer stays
independent, and prompted repair stays prompted. Failure drafts and terminal Capture
retain their existing owners; do not add a second writer to a managed turn. Historical
requests without raw turns are not modified or replayed with new bytes: an explicitly
recovered current-conversation supplement is separate export evidence.

## Compact teaching return

The helper emits compact status, grading/terminal proof, draft cursor, recovery paths
and the verified next display on healthy turns. It returns the full owner result to
its JS caller and leaves complete durable receipts unchanged. Do not print the full
returned object again, reread its result file, or expand synchronization metadata on
a healthy turn. Errors and failed follow-ups retain their diagnostics.

The user authorizes celebration for a verified correct whole-question answer,
including screened-correct Hard quick answers and verified repairs. Supply
`tools.celebrate` as in the loader whenever the native tool is available; execute
the helper, not merely read it and submit directly through Python. Celebration
does not change Hard, Good, reasoning evidence or mastery.
The helper triggers it inside the same exec after verified terminal advancement.
It skips replay and image-handoff. Do not add a
separate model/tool round trip to reconsider or repeat that effect. A UI-effect
failure does not change the learning result or authorize replaying it.

For a direct Python submission or recovery already completed in this turn, use
the same verified terminal result and call the native confetti tool directly
once if the helper did not call it. Do not rerun the writer or image-handoff
to trigger the effect. Python success alone cannot produce app confetti.

## Results and recovery

The facade preserves all existing owner result fields and adds `request_file`,
`result_file`, `next_image_paths` and `image_preload_status`.
Requests are saved privately before owner dispatch, with exact input and a stable key.

- `image_preload_status=ready`: current authorized next images passed receipt, frontier,
  hash, size and path checks. The helper loads the entire image set in original order.
- `not_applicable`: no next images, including buffered, held, complete or text-only
  results. Do not read other images to fill the gap.
- `blocked`: image preparation failed; retain the owner result as reported. Do not
  turn a successful answer/Capture into an uncommitted answer or advance again.

When only image loading failed, use the same loader and:

```javascript
await run({operation: "image-handoff", result_file: EXACT_SAVED_RESULT_FILE});
```

This reopens the saved result and current display read-only. It never dispatches an
answer owner. A later frontier rejects stale images; it does not authorize another
successor. Loading failure displays no partial image set.

When the submit itself is uncertain, replay the original command with its saved
`request_file`, or the identical original short input and key if no result was received:

```text
python3 scripts/runtime_paths_408.py --run morning_campaign_turn_408.py <original-operation> --input-json <request_file>
```

Never change a same-key request, rebuild old dialogue, replay a different operation or
switch old owner APIs mid-recovery. Old owner recovery files remain valid with their
original entry points.

After successful helper return, immediately give the brief frozen feedback and exact
accepted next provenance label/surface. Image-only content may be shown directly or
transcribed once from the now-visible original; do not add another image call or rewrite
the same stem. Use whichever teaching model the user selected in the current conversation; never hard-code, recommend or switch a model. The target is
first substantive feedback in 20–30 seconds; this call shape does not prove actual latency.
