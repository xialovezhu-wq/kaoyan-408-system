# Standalone lightweight correct-answer evidence

Use only for a real standalone 408 correct outcome under the user's continuing
lightweight-capture instruction. A managed display always uses its own turn facade.

Known entry:

```text
/usr/local/bin/python3 /Users/your-user/Documents/kaoyan-408/scripts/learning_evidence_408.py record --input-json -
```

Pass JSON through a quoted stdin heredoc inside the same `functions.exec` as other
needed work. Do not use another model turn, help/source discovery, an image-copy step
or a package builder. Required shape (replace example data with real current data):

```json
{
  "schema": "cs408-light-learning-input-v1",
  "request_id": "standalone-current-episode-terminal",
  "study_date": "2026-09-08",
  "formal_node_id": null,
  "source_id": null,
  "question": {"stem": "Exact current question text", "options": {}, "assets": []},
  "turns": [{"role": "user", "kind": "reasoning", "text": "Exact actual learner words", "observed_at": null}],
  "result": {"choice_result": "correct", "reasoning_result": "sound", "prompt_dependency": "none", "confidence": "unknown"}
}
```

Use one stable request_id for the real episode and reuse it unchanged after an
uncertain result. Do not reuse the example ID across different questions. Include
all available current question-related user/assistant turns, preserving whitespace
and original reasoning; do not substitute a model explanation. Known question image
bindings use `question.assets` rows with absolute `path` and actual `sha256`; bytes
are reverified and copied at export, so a disappeared source is reported missing.
Do not invent formal/source IDs, response timing or confidence. `lesson_id` is
optional; omitted standalone lessons group by actual study date.

Result categories are derived from observed fields: sound/no hint is independent
correct, sound/with hint is corrected_after_hint, no observed reasoning is screened.
Partial/diverged reasoning cannot be marked independent; continue the actual teaching
before claiming a resolved question. A recorded assistant hint cannot coexist with
prompt_dependency=none.

`recorded`, `recovered` or `already_recorded` returns an evidence ID, record path/hash,
zero Capture/formal/model writes, and a lightweight write count. Same-key different
bytes reject; use the same saved request to recover, never replace existing evidence.
A failed save is not success. No mastery, scheduler event, formal node or Capture is
created by this recorder. Later lesson export carries this evidence to A separately
from existing wrong-question packages.
