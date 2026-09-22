# Current personal learning context


After the current assessment and first-break hypothesis are established, but before
freezing the explanatory feedback, perform at most one read-only query to the current
`exam_tutoring_context_slice_v1`. Supply only answer-safe target keys already visible
from the current question: exact formal/source ID when known, all current knowledge-point
IDs in `knowledge_points`, and evidenced weak points in `weak_knowledge_points`, plus
mechanism and ask-type terms. Weak points receive priority; do not label all tested
points weak. Exact identity automatically adds the node's main, secondary, and hit
points. One query covers every requested point with explicit coverage; never silently
limit knowledge coverage to the three-profile fallback. Issue it inside the first
evidence pass, alongside the image or resolver call; it does not wait for their
results. The default total response budget is 8,192 UTF-8 bytes with fair per-point
compact summaries.

Invoke the known entry directly; do not discover it through help/source reads:

```text
python3 /Users/your-user/Documents/kaoyan-408/scripts/exam_tutoring_context_408.py query --repo /Users/your-user/Documents/kaoyan-408 --input-json '<query JSON>'
```

Query JSON uses `schema=exam_tutoring_context_query_v1`, `stage=post_answer`
(or `pre_answer` while answers are protected), optional `formal_id`/`source_id`,
`knowledge_points`, and `weak_knowledge_points`. Unknown IDs remain omitted;
use known point labels as lookup terms instead of guessing codes. Supply current
public mechanism/ask-type terms only when useful. This is one tool call, not an
extra model request. Copy this complete shape and replace only current identifiers:

```json
{
  "schema": "exam_tutoring_context_query_v1",
  "stage": "post_answer",
  "formal_id": "DS_2099_914",
  "subject": "DS",
  "knowledge_points": ["DS07-13"],
  "weak_knowledge_points": ["DS07-13"],
  "max_bytes": 8192
}
```

Omit unknown identities and leave unevidenced weak points empty. Do not query again
in the same learner turn just because its later review submission refreshed the
index; the next related learner turn obtains the next current version.

Use only `status=ready` output from the most recent validated committed-source refresh.
Each new related turn, including an already open conversation, must obtain the current
`source_version`; do not reuse an older in-chat slice after a formal or review write.
The commit owner refreshes locally before requesting cloud publication. Missing or stale
state uses current-only feedback and reports personalization pending; it is not completion. Use `knowledge_summaries` to read each point's latest and historical understanding
with evidence refs. The stable knowledge-code-to-records hash index and personal
summary table are published together at formal/review commits; detailed histories
remain cold records. Summaries distinguish direct from associated evidence, initial
results from repairs, and observed gaps from untested or missing evidence. They are
not a mastery score or a substitute for the current attempt. Existing bounded
`profiles` remain a teaching-route fallback. The
slice may choose the first boundary to check, support level, representation, or whether
to use a local worked step. It may also show a directly evidenced recurrence, provided
the claim retains event ID, source hash, date, and provenance. A broad point-level
signal is routing evidence only and must not be restated as a specific past user error.
Use stable point codes across different questions, including a new question with no
formal ID. Read `latest_understanding`, the representative `historical_understanding`,
`latest_repair`, and `latest_hint` together: identify the concrete mistaken boundary,
what was corrected, and whether the correction needed a hint. The current attempt
still owns the diagnosis; a latest repair is not evidence that an older error persists.
A source with `point_binding=explicit_point` and a learner-observation role supports a
point-specific claim. `question_associated`, `formal_summary`, and assistant explanation
alone do not establish that the learner made that error or independently repaired it.

`source_kind=concept_review` is already sealed B evidence; `semantic_formal_intake`
is a sealed per-point observation from ordinary formal intake. Consume both through
this same query, retaining original dates, roles, prompt dependence, and source/record
references. Reading B evidence adds no workflow detection, review-session entry, or
backflow step to ordinary question teaching. Unknown dates and ambiguous same-day
order cannot establish a newest error. A summary marked truncated is an excerpt,
not a full quote; if its missing part matters, use only its exact source fragment or
bound archive locator under the existing reopen rule, otherwise leave that claim
unresolved. Do not expand all histories, scan, rebuild, or add a second model call.
If the returned history would not change the diagnostic fork, starting support,
representation, or short transfer check, ignore it and keep the reply current-only.

On missing, stale, ambiguous, tampered, over-budget, or no-match output, continue
current-only immediately. Do not rebuild while the learner waits. Never call the retired
personalization corpus/index/slice, linked practice, variants, cross-subject activation,
Luna, a child agent, network service, or background worker. Never scan formal tables,
the review ledger, the whole Obsidian vault, or T9 on this hot path.

