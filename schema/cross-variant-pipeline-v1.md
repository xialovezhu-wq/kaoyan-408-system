# 408 跨科变式题独立盲校验流水线 v1

本协议定义 `scripts/cross_variant_pipeline_408.py` 的五阶段冷路径。它只发布答案隔离的
跨科变式题面，不修改正式错题节点、正式关系边、复做记录或掌握度。

## 一、目标与非目标

一份可发布的跨科变式必须同时证明：

1. 它起始于一份通过逐资源哈希校验的原题题面；
2. 它绑定一份私有、唯一的权威答案集合；
3. 它绑定当前跨科投影中的 `verified_live` 关系；
4. 两端属于不同科目，且每端都有明确考点和机制键；
5. 作者与校验者的 actor 和 execution 均不同；
6. 校验者在权威答案揭封前完成盲解承诺；
7. 盲解同时提交私有、规范化的语义唯一性证据，机器从完整候选全集重算唯一解；
8. 分别移除任一学科约束时都至少保留两个可行候选，机器据此证明两科机制都不可省略；
9. 盲解答案集合与唯一权威答案集合由机器精确比较，且变化不是 `numbers_only`；
10. 公开目录只含题面、公开校验回执和 manifest，不含答案、语义唯一性证明、解析或私有答案定位。

本协议不允许从安全摘要临时编造选项或答案，不把影子候选当作已验证关系，也不把
作者自验当作独立校验。

## 二、准备证据

`cross_variant_authoring_evidence_v1` 必须精确包含：

```json
{
  "schema": "cross_variant_authoring_evidence_v1",
  "evidence_scope": "verified_original_private_answer_and_verified_cross_edge",
  "original_formal_node_id": "CO_2099_912",
  "original_source_ref": "sources/original.json",
  "original_source_sha256": "<sha256>",
  "authoritative_answer_key_ref": "answer-key.json",
  "authoritative_answer_key_sha256": "<sha256>",
  "authoritative_answer_key_private": true,
  "cross_projection_ref": "wiki/study_vaults/408-full/state/personalization/cross-subject-shadow.json",
  "cross_projection_sha256": "<sha256>",
  "verified_cross_edge_id": "CSR408-0123456789ABCDEF",
  "targets": [
    {"subject": "CO", "point_id": "CO06-14", "mechanism_key": "interrupt_entry"},
    {"subject": "OS", "point_id": "OS05-13", "mechanism_key": "interrupt_service"}
  ],
  "author_identity": {
    "actor_id": "author-agent-1",
    "execution_id": "author-execution-001",
    "role": "cross_variant_author"
  },
  "verifier_identity": {
    "actor_id": "verifier-agent-2",
    "execution_id": "verifier-execution-002",
    "role": "cross_variant_verifier"
  },
  "formal_write_count": 0
}
```

原题使用 `validate_source_for_practice(..., practice_mode="original")` 完整校验。跨科投影
使用 `validate_projection` 重新对齐当前真源；指定关系必须同时满足
`verified_existing`、`activation_eligible=true`、`enabled_for_live_routing=true` 和
`activation=verified_live`。关系端点必须与两个 target 完全一致。

direct `prepare` 不能只校验 target 字段格式，也不能假设 demand 已经完成机制绑定。它必须
在同一个 personalization hot-lock 快照中读取根 manifest，校验 manifest 绑定的
`question_index.json` 和 `cross-subject-shadow.json`，并逐端证明
`mechanism_key -> point_id -> subject`：机制记录必须包含精确考点，且至少一个受管 question
节点同时具有对应正式节点身份、学科、考点和机制。格式合法但缺失、属于其他考点或无法由
当前 question index 证明的机制一律拒绝。prepare 必须为每个 target 选定一个精确的正式题
witness，并把以下最小依赖冻结为 `cross_variant_target_dependency_snapshot_v1`：原题 source
ref 与 SHA-256、两个 canonical target、两个命名 witness、指定 edge 的端点、学科、方向、
关系类型及 verified-live 状态。snapshot 写入：

```text
wiki/study_vaults/408-full/state/linked-practice/
  cross-variant-dependency-snapshots/<snapshot-sha256>.json
```

文件名必须等于规范 JSON bytes 的 SHA-256；request、private prepare、公开回执和公开 manifest
共同绑定 `target_dependency_snapshot_ref` 与 `target_dependency_snapshot_sha256`。request 不再把
完整 `question_index_sha256`、`cross_projection_sha256`、根 `build_id/as_of` 或整个 cross source
fingerprint 当作耐久依赖。

历史 request、公开证据链和 catalog 重放只验证冻结 snapshot 的内容地址、HMAC、原始 evidence
hash、私有答案链和盲校验链，不读取当前完整 question index 或 cross projection。因此，无关
outcome、as-of 重建、无关题目、同机制新增其他题、无关跨科边激活或根构建号变化不会让已发布
题源自失效。展示、调度或继续生成阶段另走 current-route 门禁：实时校验原题 source 的精确
SHA-256，确认 snapshot 中命名的两个 witness 仍保持相同机制、考点和学科，并从当前根
manifest 绑定的投影中复验指定 edge 仍为同一语义且 `verified_live`。指定 source、witness、
target 或 edge 漂移时只关闭当前路由；不得通过让整个历史 catalog 无法重建来表达停用。

私有权威答案采用：

```json
{
  "schema": "cross_variant_authoritative_answer_key_v1",
  "original_formal_node_id": "CO_2099_912",
  "verified_cross_edge_id": "CSR408-0123456789ABCDEF",
  "answer_sets": [["B"]],
  "formal_write_count": 0
}
```

`answer_sets` 必须恰好包含一个集合。该集合可以表达唯一多选解，例如
`[["A", "C"]]`；两个可接受答案集合，例如 `[["A"], ["B"]]`，直接拒绝。

`authoritative_answer_key_ref` 不是仓库路径，而是相对于本次显式
`--answer-key-private-root` 的路径。`prepare` 未提供该受管私有根时默认安全失败。私有根
可以位于仓库内或仓库外，但必须是当前用户拥有、非符号链接且权限精确为 `0700` 的目录。
从文件系统根到 private root 的每个既存父组件都必须逐级 `lstat`，任一符号链接均拒绝；
最终解析位置和 inode 共同进入根绑定。macOS 的 `/var -> /private/var` 属于真实符号链接，
调用者应传入解析后的 `/private/...` 路径，不能把系统别名当作已验证的真实边界。
答案定位必须是非空相对路径；绝对路径、`..` 逃逸、`~` 展开、任一中间目录或最终文件为
符号链接均拒绝。最终答案文件必须是当前用户拥有的非空常规文件，权限精确为 `0600`，
不能只靠 `authoritative_answer_key_private=true` 声明私有。

`prepare` 生成 HMAC 签发、内容寻址的 request。作者可见 request 只含权威答案文件的
SHA-256 和私有根文件系统身份的不可逆绑定哈希，不含答案、答案文件路径、私有根路径或
答案集合。私有根定位和根内答案定位只保存于权限收紧的 private prepare。后续作者承诺、
盲解、揭封、发布及公开链重放都会重新检查根绑定、路径边界、owner、`0700` / `0600`
权限和答案 SHA-256；准备后把文件换成同内容符号链接也不能通过。

## 三、作者题面承诺

作者提交 `cross_variant_author_surface_submission_v1`：

```json
{
  "schema": "cross_variant_author_surface_submission_v1",
  "generation_request_sha256": "<sha256>",
  "author_identity": {
    "actor_id": "author-agent-1",
    "execution_id": "author-execution-001",
    "role": "cross_variant_author"
  },
  "question_surface_sha256": "<sha256>",
  "formal_write_count": 0
}
```

题面仅接受不超过 64 KiB 的 UTF-8 Markdown。答案、解析、正确选项、`answer key` 等
泄漏标记会被拒绝。题面复制到 managed work 后生成唯一的
`cross_variant_author_surface_commit_v1`；同一 request 不能替换为第二份题面承诺。

## 四、校验者盲解承诺

校验者在 reveal 之前提交：

```json
{
  "schema": "cross_variant_blind_answer_submission_v1",
  "author_commit_sha256": "<sha256>",
  "verifier_identity": {
    "actor_id": "verifier-agent-2",
    "execution_id": "verifier-execution-002",
    "role": "cross_variant_verifier"
  },
  "unique_answer": true,
  "answer_set": ["B"],
  "mechanisms_used": [
    {"subject": "CO", "point_id": "CO06-14", "mechanism_key": "interrupt_entry"},
    {"subject": "OS", "point_id": "OS05-13", "mechanism_key": "interrupt_service"}
  ],
  "semantic_uniqueness_evidence": {
    "schema": "cross_variant_semantic_uniqueness_evidence_v1",
    "answer_token_universe": ["A", "B", "C", "D"],
    "selection_cardinality": {"minimum": 1, "maximum": 1},
    "candidate_answer_sets": [["A"], ["B"], ["C"], ["D"]],
    "subject_constraint_satisfaction": [
      {
        "target": {"subject": "CO", "point_id": "CO06-14", "mechanism_key": "interrupt_entry"},
        "satisfying_answer_sets": [["A"], ["B"]]
      },
      {
        "target": {"subject": "OS", "point_id": "OS05-13", "mechanism_key": "interrupt_service"},
        "satisfying_answer_sets": [["B"], ["C"]]
      }
    ],
    "joint_satisfying_answer_sets": [["B"]],
    "formal_write_count": 0
  },
  "semantic_uniqueness_evidence_sha256": "<canonical-sha256>",
  "structural_change_kind": "cross_pipeline_synthesis",
  "both_subjects_essential": true,
  "solved_without_authoritative_key": true,
  "formal_write_count": 0
}
```

`mechanisms_used` 必须与 prepare 的两个 target 完全相同。允许的结构变化只有：

- `boundary_condition_change`；
- `context_and_constraint_recomposition`；
- `cross_pipeline_synthesis`；
- `representation_change`；
- `state_transition_recomposition`。

`numbers_only` 和未知变化类型均拒绝。盲解承诺含答案，因此只进入 private managed work，
不进入公开题源或 catalog。reveal 已存在后不得补交盲解。

### 私有语义唯一性证据

`semantic_uniqueness_evidence` 是盲解的一部分，并在规范化后写入权限为 `0600` 的私有
blind commit。提交者同时绑定规范 JSON 的 SHA-256；public receipt 只绑定整个 blind commit
的哈希，不公开专用证据哈希、候选全集、各科满足集、联合满足集或答案。

证据采用有限、可重算的约束模型：

- `answer_token_universe` 必须有 2 到 8 个合法 token，去重并按字典序排列；
- `selection_cardinality.minimum/maximum` 定义允许选取的 token 数量；
- 机器用 token 的全部组合生成完整候选全集，`candidate_answer_sets` 必须与该全集逐项相等，
  不允许少列、增列、重复或乱序；
- `subject_constraint_satisfaction` 必须恰好各绑定一个 prepare target。每项表示仅应用该学科
  全部语义约束时仍满足的候选集合；每个集合必须去重、内部规范排序，且只能取自完整候选全集；
- 机器对两科满足集求交，所得集合必须与 `joint_satisfying_answer_sets` 完全相等、恰好只有
  一个成员，并且该成员必须等于盲解 `answer_set`；
- 机器分别移除每个学科的满足集约束，重新对剩余约束求交；两个 leave-one-subject-out
  结果都必须至少包含两个候选。若只靠 CO 或只靠 OS 已经唯一，`both_subjects_essential=true`
  仍然无效并拒绝提交。

机器重算证明的是校验者所提交有限模型内部的唯一性和双科必要性。校验者仍须从题面逐项审查
每个候选是否满足各科机制，不得把未写明的平台假设、隐含答案或作者结论填入满足集。证据不规范、
联合出现第二解、任一单科已经唯一，或声明满足集不等于机器重算结果时均 fail closed。

## 五、揭封与机器比较

`reveal` 重新验证 request、内容寻址的 target dependency snapshot、当前原题 source、当前根
manifest 绑定的精确 target witness 与 live edge、权威答案文件、作者承诺、盲解承诺和私有
语义唯一性证据。它再次生成完整候选全集，重算两科交集及两个单科移除结果。
只有规范化后的 `blind.answer_set` 与唯一 `authoritative.answer_sets[0]` 完全相等，才写入
`cross_variant_machine_reveal_receipt_v1`。

reveal receipt 位于 private managed work，不公开答案值；它保存双方答案集合比较承诺哈希、
语义唯一性证据哈希、机器重算摘要哈希、输入哈希和 `unique_answer_verified=true`。
盲解错误、语义第二解或任一单科已唯一时均无发布回执。

## 六、公开发布与目录

`finalize` 再次完整重放证据链，并写入：

```text
wiki/study_vaults/408-full/state/linked-practice/
  cross-variant-sources/CVAR-*/
    cross-variant-question-01.md
    verification.json
    manifest.json
```

公开 manifest 为 `cross_variant_question_source_manifest_v1`。公开目录不得出现答案文件、
盲解正文、语义唯一性证明、解析或任何未声明文件。权威答案只以
`authoritative_answer_key_sha256` 出现；公开 manifest 和公开回执均不包含答案集合、
权威答案文件定位、语义唯一性证据、候选全集、各科满足集或机器重算摘要。

完整目录由公开 manifest 重新深校验后生成：

```text
wiki/study_vaults/408-full/state/personalization/cross-variant-catalog.json
```

catalog schema 为 `cross_variant_catalog_v1`，提供 `by_edge` 和 `by_point` 有界索引，并由
personalization root manifest 的 SHA-256 绑定。每项保留 dependency snapshot ref 和 SHA-256。
catalog 是历史已发布题源的可重建派生投影，重建不等于当前可展示授权；当前展示必须再通过
source、target witness 和 live edge 门禁。`formal_write_count=0`。

涉及 personalization manifest 与 cross-variant durable state 的操作统一采用全局锁序：
先取得 `personalization/.hot-path.lock`，再取得
`linked-practice/.cross-variant-pipeline.lock`。`finalize` 必须在这两把锁覆盖的同一窗口内
重放 reveal、私有答案根绑定、作者与校验者身份、盲解、题面和 dependency snapshot，并实时
复验当前 source、target witness 和 live edge，随后发布题源、
重建 catalog 并更新根 manifest；不能在锁外验证后再发布。`rebuild-catalog` 使用相同锁序。
公开 direct `prepare` 同样必须使用 hot outer、pipeline inner，以绑定上述 manifest 快照。
demand planner 已经持有 hot lock 时，只能调用代码内的 hot-held prepare 入口，由该入口取得
pipeline lock；CLI 和外部 API 没有“我已持锁”的 flag，不能自行绕过 hot lock。作者承诺、
盲解和 reveal 只写 pipeline durable work，仍只取得 pipeline lock。任何路径都不得先持有
pipeline lock 再等待 hot lock，以免构成 ABBA 死锁。

`linked-practice` 管理根、pipeline lock、`cross-variant-work`、request/private work 目录、
`cross-variant-dependency-snapshots` 和 `cross-variant-sources` 发布目录在任何锁定、遍历或写入前，都必须从已解析 repo root 开始
逐组件 `lstat` 拒绝符号链接，并在解析后确认仍位于仓库内允许的 linked-practice 根。
创建中间目录后必须再次校验；最终锁文件使用 no-follow 打开。任何父级外链都要在生成 key、
private prepare、request 或公开题源前失败。

`linked_practice_source_408.validate_source_for_practice` 在
`practice_mode=variant` 下同时接受本协议的
`cross_variant_question_source_manifest_v1`。它通过延迟导入调用
`validate_published_cross_variant_source`，先重放完整公开证据链、内容寻址 snapshot 和私有
盲解、揭封链，再实时复验当前原题 source、两个命名 target witness 和指定 live edge；
因此晨间 schedule、首答登记和快速 capture 的二次题源校验不会把已验证跨科变式误判
为普通未验证文件。传入的 `variant_of_formal_node_id` 必须等于 manifest 的
`original_formal_node_id`。

## 七、命令

```bash
python3 scripts/cross_variant_pipeline_408.py --repo . prepare --evidence evidence.json \
  --answer-key-private-root /absolute/managed/private-root
python3 scripts/cross_variant_pipeline_408.py --repo . commit-author \
  --generation-request request.json --question-surface question.md --submission author.json
python3 scripts/cross_variant_pipeline_408.py --repo . commit-blind \
  --author-commit author-commit.json --submission blind.json
python3 scripts/cross_variant_pipeline_408.py --repo . reveal --blind-commit blind-commit.json
python3 scripts/cross_variant_pipeline_408.py --repo . finalize --reveal-receipt reveal.json
python3 scripts/cross_variant_pipeline_408.py --repo . rebuild-catalog
```

任一阶段遇到私有根缺失或漂移、答案路径逃逸、symlink、owner/权限不合规、作者与校验者
同 actor、同 execution、snapshot 内容地址漂移、原题漂移、当前投影未由根 manifest 绑定、
目标 witness 或 edge/target 语义漂移、非唯一权威答案、盲解错误、语义第二解、候选全集不完整、
候选或集合重复/非规范、任一单科已唯一、公开证明泄漏、题面泄漏、只换数字或仅一科机制参与，
必须 fail closed，不生成或不展示题源。无关 outcome、as-of、question-index 内容或
无关 edge 激活不属于目标语义漂移，且不得破坏历史 catalog 重建。
