# Formal point observations

This is an additive semantic contract inside `sol-curation-decision-408-v2`, not a
new formal transaction version or a B review session. New `start-batch` events
persist `knowledge_observation_contract=formal-point-observations-408-v1`. Old events
without it retain their original behavior. `needs_user` may retain evidence gaps.

The owner opens the whole frozen package, including available attachments, and
confirms its actual touched points. Every main/sub/hit tag needs an explicit
observed/no-observation disposition, but may not be promoted to an error by mapping.
Additional actual points are included even when not present in the original tags.
Machine validation cannot infer semantic completeness; `scope_confirmed` is the
native owner's source-backed attestation, never a heuristic classification.

The decision property has this shape:

```json
{
  "knowledge_observations": {
    "schema": "formal-point-observations-408-v1",
    "scope_confirmed": true,
    "points": [
      {
        "knowledge_point": "OS03-17",
        "status": "observed",
        "records": [
          {
            "summary": "把页表项与页内偏移混淆",
            "source_fragment": "turn:1",
            "event_kind": "outcome",
            "evidence_role": "observed_learner_evidence",
            "prompt_dependency": "independent",
            "outcome": "wrong",
            "evidence_refs": [{"turn": 1, "sha256": "SHA256_OF_EXACT_NORMALIZED_TURN"}]
          }
        ]
      },
      {
        "knowledge_point": "OS03-18",
        "status": "no_observation",
        "reason": "题设涉及该点，但没有该点的真实作答表现",
        "records": []
      }
    ]
  }
}
```

Original turn hashes use SHA256 of UTF-8 JSON with `ensure_ascii=False`,
`sort_keys=True`, `separators=(',', ':')`, without a trailing newline. Turns are
one-based exact objects from verified `conversation.json.turns`, not excerpts.
`source_fragment=turn:N` can use a distinct `#facet` suffix for multiple facets of
one turn. Every referenced turn hash must reopen exactly. The full canonical SHA
binds all original source, conversation and attachment bytes. Records cite the
learner turn for an actual learner outcome/repair and the assistant turn for a
hint/explanation. No raw answer is copied into the safe summary. The full raw
conversation and original assets remain accessible through their verified package.

Each record belongs to exactly one leaf code and `explicit_point`. The writer derives
its formal ID from the terminal, original study date from source, and order from the
primary referenced turn; callers cannot substitute ingestion dates or timestamps.

- `outcome`: role `learner_report|observed_learner_evidence`; requires `outcome`.
- `repair`: same learner roles; requires `correction_result`, forbids `outcome`.
- `hint|explanation`: role `assistant_explanation`; forbids results.
- Results: `wrong|partial|uncertain|independent_correct|screened_correct|relearn_required|correct`.
- `prompt_dependency`: `independent|hint_or_prompted|unknown` is mandatory.
  `independent_correct` requires `independent`; a prompted repair stays prompted.

`terminal-binding` first verifies and seals the formal terminal, then automatically
seals observations in the existing `knowledge_history_semantic_annotations_v1`
envelope under the private semantic store's `formal-annotations/`. Each unit binds
one package locator/SHA, point dispositions and exact terminal locator/SHA.
No formal node, review score, recurrence, B session, or question scheduler record is
written by semantic sealing. The original formal ID is not propagated to every
question sharing a point. Identical source+SHA retries reuse the same observations;
conflicting changes reject rather than duplicating an attempt.

A failure after the terminal is `FORMAL_COMMITTED_KNOWLEDGE_OBSERVATIONS_PENDING`.
Retry that terminal binding and its missing projection/publication only. Do not
replay apply. `load_formal_records` verifies annotation bytes, terminal result-line
bytes, decision, full source SHA and original turns on read. It supports verified
T9 package reopening after archive. The knowledge projection and morning priority
consume these same safe records; hints are reference evidence, not errors or mastery.
Fixture roots are isolated; production overrides are forbidden.


## 本地语义分类更正

已封存的学习原话、终态和原始观察不改写。授权的本地质量核对可通过同一观察owner的correct_formal_observations保存带原annotation SHA、原source SHA、终态绑定、作者和原因的语义更正；新记录仍逐turn验证，知识点集合不可扩大。读取时验证原件后采用更正解释，保留原件可追溯。该操作不追加用户作答、不重放题级评分、错误次数或FSRS。典型用途是将正确子步骤与整题错误分开，或将只有助手讲解而未见用户作答的内容改记教学接触。
