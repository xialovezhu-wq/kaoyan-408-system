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

### 追加更正链与撤回（2026-11-06）

`correct_formal_observations` 保留原调用方式，新增以下可选参数：

- `action="replace"|"retract"`；默认 replace 仍要求完整、逐回合验证的 review。retract 必须不传 review，撤回当前语义解释，不恢复旧解释，不删除原始学习证据。
- `expected_previous_sha256`：提供时必须等于当前更正头；显式 None 表示尚无更正。已成功请求的重试先按请求身份识别，不因后续更正发生而误重放。
- `request_id`：可选的稳定业务请求身份；同一身份不同内容拒绝。无该字段时以规范化请求内容识别重试。希望明确重新启用曾撤回的相同解释时，使用新的 request_id。

新更正为 `formal-observation-correction-408-v2`，保存在
`formal-annotations/corrections/versions/<original_annotation_sha256>/<sha256>.json`。
其中记录 `previous_correction_sha256`、`recorded_at`（带时区的真实写入时间）、
`action`、原 annotation/source/terminal 绑定、作者和原因。原始学习日期仍来自原件。
`corrections/heads/<original_annotation_sha256>.json` 是原子更新的派生头；所有旧版本保留。
旧 `corrections/<original_annotation_sha256>.json` v1 文件只读，作为链首兼容，不覆写，未知写入时间保持未知。

更正写者串行持 correction 锁，在原生 refresh 锁内先撤销旧缓存资格，再公布新头；
释放锁后才调用原 `complete_after_commit`。原件、学习账本和评分均不写入。
后续刷新或发布失败保留 PENDING，重试仅恢复这一层，不追加相同更正。
更正版本和头进入 source commitments；可变 followup 回执不属于学习语义依赖。
刷新成功只说明派生快照已更新。回执同时按受影响父点读取当前版本状态，返回
`personal_review_required` 与 `personal_review`：存在 stale/not_authored 时明确仍需个人
语义审核；状态不可读取时返回 null 与 pending_context_refresh。不会为了令回执完成而
自动编写新的个人判断。CORRECTED/RETRACTED 只表示更正链已提交，不表示个人概况已重审。

`load_formal_records` 只返回当前有效解释，并携带原 annotation SHA、当前更正 SHA、
完整 `correction_refs` 与 recorded_at。KLR 同时保留这些引用和原回合引用；来源增加
不推断新的掌握结论。新增非语义来源元数据不使未变化的概况语义过期，真实更正身份
仍参与语义指纹。

`load_formal_record_history(repo, original_annotation_sha256, ...)` 精确读取经验证的原始
解释和各次更正；每版包含 action、recorded_at、records 和更正引用。原 annotation
没有 recorded_at 时返回 null，不使用文件时间推测。撤回版 records 为空，但旧版可回读。
来源 coverage 的 `formal_annotation_revisions` 目录保留原 annotation、考点、当前头和
生命周期状态，因此当前解释撤回后仍可定位历史，不能以“当前无记录”冒充“从未发生”。
