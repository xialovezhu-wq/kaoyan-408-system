# Exam-oriented single-question teaching flow

## Purpose

Optimize one 408 question for exam performance: recognize the decisive cue, choose
the correct first action, apply the governing boundary, and leave with one short
transfer check. Historical evidence should change a teaching decision or stay out of
the prompt.

## Evidence order

Use this order and do not collapse the roles:

1. Current public question and attachment establish what is being asked.
2. Current answer, confidence, first action, and reasoning establish learner evidence.
3. The controlled grader establishes current correctness without exposing its key.
4. The latest validated current teaching slice may adapt explanation strategy.
5. An exact `raw_archive_locator` is a last-resort deep source for this one question.

The teaching slice is a learner-model signal, not a truth source for the current
answer, current first break, mastery, motivation, or ability.

## Turn policy

### Before an answer

- If the learner already supplied an answer or first action, do not ask for it again.
- If the learner wants answer protection, ask one question about the first action,
  decisive object, or condition and wait.
- If the concept is new, give the minimum frame needed to make one meaningful attempt.
- If the learner requests direct teaching, teach now; do not require a ceremonial try.

### After an answer

First establish from current evidence:

- result of the selected answer;
- result of the reasoning chain;
- first confirmed divergence, or the smallest unresolved diagnostic fork;
- confidence and prompt dependence.

Only then read at most one teaching slice. Do not send raw answer keys, private grader
reasons, full question text, attachments, or the learner's full response into the
history query.

### Compose the feedback

Use the smallest subset that advances the problem:

- `当前判断`: what is confirmed from this attempt;
- `第一断点`: the first invalid condition, object role, state transition, or method;
- `考场触发`: the stem cue that should activate the method;
- `决策规则`: a compact if-then boundary or comparison;
- `下一动作`: one step the learner now performs.

These are content roles, not mandatory headings. Do not turn them into a fixed long
template.

For a single local error, the default deliverable is one short final correction
and one next action. Do not repeat it in both commentary and final. Routine turns
use the complete query JSON below and the preloaded managed JSON contract; they do
not investigate helper source, CLI help or schemas. Known uncommitted validation
errors permit one correction from that contract, not an exploratory retry loop.

## Adaptive support policy

| Current evidence and bounded history | Default action |
|---|---|
| No usable history or an unresolved current fork | Ask one current-evidence diagnostic question |
| First failure with a usable schema | L1-L3 cue or boundary reminder |
| Partial schema or one known missing step | L4 scaffold; learner completes the next step |
| Recurrent direct-evidence break, absent prerequisite, or two stalled attempts | L5 direct explanation or one minimal worked step, then fade one step |
| Correct with low/medium confidence | Explain the discriminating cue and why the nearby alternative fails |
| High-confidence independent correct with sound reasoning | Brief confirmation; close or use one short changed-boundary check |

Historical recurrence may move the starting support level. It cannot make a current
response correct or incorrect. Never equate repeated historical failure with permanent
weakness, and never equate a recent correct response with mastery.

## Current-version teaching-slice query

The local query uses `exam_tutoring_context_query_v1`. In one query supply all current
knowledge points and prioritize evidenced weak points from the current public surface:

```json
{
  "schema": "exam_tutoring_context_query_v1",
  "stage": "post_answer",
  "formal_id": "CO_2099_907",
  "source_id": "EXAM408-2018-Q14",
  "subject": "CO",
  "knowledge_points": ["CO02-20"],
  "weak_knowledge_points": ["CO02-20"],
  "mechanism_terms": ["规格化边界", "偏置阶码"],
  "ask_type_terms": ["边界值判断"],
  "max_profiles": 3,
  "max_bytes": 8192
}
```

Unknown fields are null or empty arrays. Do not guess a formal/source ID. A model
inference about the knowledge point remains a query candidate, not a formal fact.
Do not classify every tested point as weak. Exact identity automatically includes the
node's main, secondary and hit points. `knowledge_summaries` returns compact latest
and historical understanding with refs for the requested points; coverage must account
for every key, including unresolved or unavailable points. The three-profile limit
applies to the legacy teaching-route fallback, never to knowledge-point coverage.
The default 8192-byte total budget is shared fairly; detailed records remain in the
cold content-addressed record store. No point-by-point query loop or ledger scan.

Accept only a slice that:

- has `status=ready` and schema `exam_tutoring_context_slice_v1`;
- binds a validated committed-source refresh receipt and projection build;
- reports `current_correctness_source=current_question_only`;
- stays within the total byte budget and explicitly reports point coverage;
- preserves the stable knowledge-code-to-records hash binding and summary evidence refs;
- exposes answer-safe fields and evidence refs only;
- rejects any post-refresh review-ledger suffix or source-version drift.

A formal or review commit must refresh or invalidate affected history before the next
related turn, including an existing conversation. The query returns `source_version`;
never reuse an older in-chat slice after the source changes. Current-turn facts still
determine the answer and first break.

If the slice is unavailable, stale, ambiguous, tampered, over budget, or empty, use no
history. Do not repair or rebuild it in the learner turn.

## Historical-claim boundary

The slice can change the teaching route without being mentioned. Mention a concrete
past pattern only when it has direct provenance and at least one bound event ID, date,
and source hash. Phrase an aggregate as a snapshot signal, not as a quote or diagnosis.

Examples:

- Safe: the validated history slice contains two direct records of the same field-role
  confusion, so start from the comparison rather than another definition question.
- Unsafe: "you always confuse addresses" from a broad knowledge-point aggregate.
- Unsafe: "you have mastered this" from one correct or hint-assisted result.

## Speed and stopping rules

Target the first substantive teaching text in 20–30 seconds using the configured
GPT-6 Astra low entry and client fast mode where available. Progress commentary does
not count. Skills cannot set or attest model, reasoning level, fast mode, or service
tier; verify actual latency from runtime measurements. Keep necessary reasoning and
transaction checks complete.

- One local query at most; no model, network, child agent, background service, or cold
  rebuild.
- No full Obsidian, formal table, relation graph, review-ledger, archive, or source scan.
- At most one progress-making question per turn.
- After two stalled attempts, teach rather than prolonging guesswork.
- Stop when the learner can state or execute the trigger and first action without a
  known contradiction. Put spaced retention checks into the review loop, not the same
  long reply.

## Answer protection and visuals

History never relaxes answer protection. Before the learner answers, a visual may show
only public objects, states, and the current hint level. Use a visual for state, timing,
topology, layout, ownership, or aligned mechanism comparison; do not create one for a
single definition or one-step fact.
