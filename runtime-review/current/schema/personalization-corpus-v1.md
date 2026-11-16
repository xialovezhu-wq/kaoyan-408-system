# 408 日终正式结果个性化语料协议 v1

历史兼容合同。下文的构建命令与写入流程用于解读旧回执。现行 corpus 入口为只读兼容。当前摘要构建与日常读取参见 [应试单题教学上下文](exam-tutoring-context-v1.md)。

该协议定义日终正式编纂成功结果到个性化回答系统之间的答案安全派生层。
事实捕获账本和正式错题库继续分别承担学习事实真源与正式语义真源；本语料层可删除、可重建，不是第三真源，不授权任何正式写入。

## 一、纳入条件

只有同时满足以下条件的结果才能进入语料层：

1. 日终批次已经关闭；
2. `global_closeout=pass`，并且 `daily_curation_close_v3` 事件绑定可验证的 aggregate global audit receipt；历史 `legacy_unsealed` close 只有在未改写旧 ledger 的前提下，另行取得当前代码生成且仍可验证的 `legacy_complete_compatibility_receipt_v1`，才可作为兼容完成门禁进入 corpus；
3. 单项结果为 `curated` 或 `already_current`；
4. `curated` 绑定当前仓库专属 runtime 中实际存在的 normal receipt，账本 SHA-256 必须等于该文件字节哈希；`already_current` 绑定实际存在的内容寻址 verification entity，账本 SHA-256 必须等于该文件字节哈希；
5. 正式节点仍存在，安全节点卡字段完整；
6. 所有准备进入语料的文本通过答案安全扫描。

`needs_user`、`failed`、active、`PARTIAL` 批次以及全局收尾失败的批次均不得进入。即使 `PARTIAL` 批次的全局审计本身通过，也必须保留上一版线上 corpus；已经正式成功的单项只能等该日期后续批次形成完整终态后，再由新的 `COMPLETE` 批次发布，不能从部分批次中拆出发布。

成功项还必须重新核对完整身份链：batch 的冻结 capture 集合包含该 capture，batch 与 capture 学习日期一致，capture 已授权且 payload hash 可重算，result event 的全部结果字段与 `result_sha256` 一致，close event 的状态与 `close_sha256` 一致；v3 close 的 hash 必须包含 `close_contract` 和 aggregate receipt SHA-256，并通过事实账本的 receipt validator 核验 attestation、批次和结果集合。显式 capture formal identity 不与最终 formal ID 冲突，最终正式行和安全卡均真实存在。任一环节仅有 64 位字符串、实体缺失、实体内容漂移或身份不一致时，整个 corpus 重建失败，不把该项降级成普通成功。

当 `rebuild` 明确传入 `--require-batch-id BATCH` 时，被点名的 v3 批次视为本次用户触发的当夜即时收尾：除验证 aggregate receipt 的签名、内容哈希、批次快照与逐题结果外，还必须使用 `require_current_objects=true` 重新比较该回执承诺的审计工具、正式总表及该批安全卡等当前对象。seal 或 close 之后即使只发生答案安全的表面改字，也必须拒绝这次即时重建，先重新完成合法的审计收尾，不能把漂移后的对象写入当夜语料。

不传 `--require-batch-id` 的维护性全量重建继续允许正式节点在后续合法 redo 后形成新的当前语义投影：历史 v3 回执仍须通过签名、实体、批次快照和逐题结果校验，但不要求历史对象承诺永远等于当前文件。未封印的旧 `legacy_unsealed` COMPLETE 批次继续只在事实账本层只读兼容，并列入 corpus manifest 的 `unsealed_complete_batch_ids`。兼容封印不是伪造 v3 close：它由当前固定全量审计独立生成，内容寻址并通过仓库私钥 attested，绑定 repo、冻结 capture set、旧 close、逐题 result event、normal receipt/WAL 或 verification、当前正式表/安全卡、审计工具和 PASS 报告。兼容封印要求这些当前对象在每次 compile 时仍与封印相同；漂移后必须重新执行显式封印，不能沿用旧实体。无论采用哪种模式，逐题完成证据的现有校验均不得弱化。

历史 COMPLETE 兼容封印命令：

```bash
python3 scripts/personalization_corpus_408.py --repo . seal-legacy-complete \
  --batch-id CUR-YYYYMMDD-HASH-A01
```

该命令只接受真实 `legacy_unsealed`、`COMPLETE`、`global_closeout=pass` 批次；`PARTIAL`、active、failed closeout 或已是 v3 的批次一律拒绝。它不追加或改写 intake ledger，不修改正式库，也不发布 corpus。receipt 固定写入 `state/intake-curation/legacy-complete-compatibility-receipts/<sha256>.json`，文件权限为 `0600`；签名复用经 `0600` 普通文件门禁校验的仓库 aggregate-audit HMAC 私钥。相同且仍有效的封印重放返回 `ALREADY_SEALED`，不重复运行全量审计。

## 二、默认位置与文件

默认根目录：

```text
wiki/study_vaults/408-full/state/personalization/corpus/
```

固定入口：

```text
manifest.json
indexes/<index-sha256>.json
objects/<item-sha256>.json
batches/<batch-sha256>.json
```

所有文件使用 `schema=personalization_corpus_v1`。`kind` 分别为：

```text
nightly_curation_corpus_manifest
nightly_curation_corpus_index
nightly_curation_corpus_item
nightly_curation_corpus_batch
```

manifest、index、item 和 batch 都必须显式声明：

```text
answer_safe=true
formal_write_count=0
```

## 三、内容寻址与 manifest 绑定

index、item 与 batch 文件名使用其规范 JSON 字节的 SHA-256。`manifest.json` 的
`index_ref` 精确指向 `indexes/<sha256>.json`，`artifact_hashes` 是从相对路径到
64 位小写 SHA-256 字符串的直接映射，必须包含 `index_ref` 指向的 index 以及
manifest 中能够到达的全部 object 和 batch。读取器仍可读取没有 `index_ref`、使用
固定 `index.json` 的旧版 corpus；新发布不得再覆盖固定索引。

`build_id` 绑定：

- 成功批次和逐题结果的内容寻址投影；
- 被纳入正式行的当前哈希集合；
- 被纳入安全卡的当前哈希集合；
- 实际绑定的 canonical outcome 哈希；
- 所有 artifact 哈希；
- counts、batch 摘要和显式排除诊断。

热路径先验证 manifest 合同、`index_ref` 与对应 artifact hash，再按索引取得少量
object，并逐个核验 object 哈希。缺失、漂移、越界、软链接或 schema 不符时必须
fail-closed，不能回退扫描完整正式库。

发布时先原子写入新的内容寻址 index、object 和 batch，最后只原子替换一次
`manifest.json`。旧内容寻址对象不在发布事务中删除。任何写入或审计在 manifest
切换前失败时，旧 manifest 仍指向完整旧 index 和旧 objects；不得留下“旧 manifest
配新固定索引”的半发布状态。垃圾回收属于另一个显式冷路径，不能夹在发布事务中。

冷路径重建同时生成 `semantic_index_version=1` 的受控语义倒排面：

```text
by_mechanism_id
by_error_pattern
by_method_id
```

这些键只能由 item 中答案安全的 observed facts 与 formal snapshot 通过受控规则派生，
不是自由文本向量召回。每个倒排 entry 自带同一组 `semantic_keys`，审计必须验证映射
键确实属于 entry 声明的键集合。由此，热路径可以跨不同正式考点找同一机制、同一
错误模式或同一方法模板，而无需遍历全部历史 object。

每个新 item 必须带 `source_binding.completion_entity`，其中 entity kind、entity SHA-256 和 formal ID 必须与该 item 的完成证据及正式 ID 相等。旧版只带裸 `completion_evidence_sha256` 而没有 completion entity binding 的 object 不再可被热路径读取；必须先用当前代码完成一次冷路径 rebuild。

### 三-A、正式完成证据实体

`curated` 的 normal receipt 只允许从以下当前仓库专属目录按内容哈希查找：

```text
~/.codex/kaoyan-408-intake/receipts/<repo-key>/*.json
```

该实体必须满足 `schema=intake_batch_receipt_v1`、`status=COMMITTED`、`repo_root` 等于当前仓库、`items` 恰含最终 formal ID，并且文件名与 receipt 的 idempotency key 一致。其 WAL 必须位于同一 runtime 的当前仓库 transaction 根下，处于 `committed`，且 transaction、idempotency、payload、repo 和 items 与 receipt 一致。账本只保存的 receipt SHA 不能替代这些实体检查。

`already_current` 不得使用任意文本或裸哈希。记录结果前先运行：

```bash
python3 scripts/personalization_corpus_408.py --repo . seal-already-current \
  --batch-id BATCH --capture-id CAPTURE --formal-id OS_2020_001
```

它在以下固定目录写入答案安全、零正式写入的内容寻址实体：

```text
~/.codex/kaoyan-408-intake/verification-receipts/<repo-key>/<sha256>.json
```

实体使用 `schema=already_current_verification_v1`，绑定 repo、batch、capture、学习日期、formal ID、capture payload SHA-256、当前正式行 SHA-256 和当前安全卡 SHA-256。随后把命令返回的 `verification_sha256` 交给 `mark-result`。corpus 重建时会重新计算上述当前实体哈希并要求完全相等；verification 文件缺失、内容漂移、换用了另一个 capture 或正式表面已经改变时均拒绝纳入。该 binding entity 不替代日终所要求的完整正式审计；新批次的全局审计通过由 HMAC attested、内容寻址的 aggregate receipt 证明，而不是仅靠 `global_closeout=pass` 字符串。

## 四、item 的两条事实线

每个 item 同时保存两类不能相互替代的证据。

`observed_facts` 只来自快速捕获中已经带 provenance 的答案安全用户事实，包括错误入口、第一动作、来源身份与上下文锚点。没有观察到就保持空值或“未观察到”，不得从正式错因反推用户当时怎么错。

`formal_snapshot` 来自当前正式表和安全节点卡，包含：

- 正式 ID、来源 ID、科目、模块和考点；
- 安全题目摘要、关键参数和问法类型；
- 实际考查机制、核心考点、模糊概念和错因标签；
- 正式安全卡中的用户错误入口和复做第一动作；
- 正式日期与答案安全的最近错误记录。

它是“由该日终成功证据联结到当前正式语义”的可重建投影，不冒充事件发生当时的历史快照。正式节点后来经过合法 redo 后，重建会采用更新后的当前正式语义，并产生新的 object 哈希。

## 五、canonical outcome 联结

优先使用 capture 中的 `review_loop_event`、普通对话桥接产生的 `ordinary_review_loop_event` 或 legacy `review_outcome` 精确 stable ref，并核验事件内容 SHA-256。精确 ref 仍须执行独立身份核验：event 的 `observed_date` 必须等于 capture 的 `study_date`；capture 的 `source_facts.source_id` 非空时必须等于 event 的 `source_id`；event 自带任何正式 ID 时，其中必须包含日终最终 `formal_id`。locator 和哈希正确但身份不一致时仍然拒绝。

没有精确 ref 时，只能在同一 `study_date` 内寻找与所有可用身份维度同时相容的唯一 outcome。capture 提供非空 `source_id` 时 event 必须拥有相同 `source_id`，不能因为 formal ID 相同而忽略来源冲突；event 提供正式 ID 时必须包含日终最终 `formal_id`，不能因为 source 相同而忽略正式身份冲突。同日事件只命中一维、另一维冲突时写入 `identity_conflict` gap；完全没有相容事件时写 `no_match`；多个完全相容事件时写 `ambiguous`。三种 gap 均不得投影 outcome。

唯一匹配时只保存答案安全元数据：event ID、来源、日期、考点或机制 ID、首答结果、选择判断结果、推理判断结果、原样信心、证据类型、脆弱状态和稳定保持状态。旧事件缺少分离判断时保留显式未观察状态，不得从 `first_result` 推导。不得复制 `first_break`、用户作答、完整反馈、题干、选项或答案。

分离判断只接受以下受控枚举：

```text
choice_result: correct | incorrect | partial | uncertain | not_applicable
reasoning_result: sound | partial | diverged | not_observed
```

旧事件没有相应字段时，分别投影 `not_applicable` 与 `not_observed`，同时令对应的 `choice_result_evidence_field_present=false` 或 `reasoning_result_evidence_field_present=false`。这两个默认值只说明原事件未保存分离判断，不能改写成真实答题结论。显式字段若不在受控枚举内则 corpus 重建失败。

没有匹配或存在多个候选时，必须写入 `no_match` 或 `ambiguous` gap。正式节点里的错因不能用来补造 canonical failure。

## 六、重建、审计和查询

夜间顺序固定为：

1. 用户显式指定学习日期并启动日终正式编纂；
2. 日终流程逐题完成正式事务并记录 `mark-result`，通过总体验收后先执行 `seal-global-audit --batch-id ...`，再把返回的 SHA-256 交给 `close-batch --global-audit-receipt-sha256 ...`；
3. 只有批次已关闭且 `global_closeout=pass`，才运行 corpus `rebuild --require-batch-id`；
4. 紧接着运行 corpus `audit`，失败时不得把新 corpus 注入回答；
5. 后续回答前只走 manifest 绑定的有界 query；除正式 ID 与考点外，还可按当前目标
   的受控机制、错误模式和方法键命中少量 object，不在热路径重放 capture ledger、
   遍历 corpus object 或扫描正式库。

对应命令：

```bash
python3 scripts/personalization_corpus_408.py --repo . rebuild \
  --require-batch-id CUR-YYYYMMDD-HASH-A01
python3 scripts/personalization_corpus_408.py --repo . audit
```

`--require-batch-id` 不是写入授权。它要求指定批次已经关闭并通过全局收尾；若该批次使用 v3 close，还会立即重验 aggregate receipt 中该批审计工具与对象承诺仍等于当前文件。命令仍会从所有当前有效的成功批次重建完整派生语料，但只有被点名的 v3 批次执行这项 close 后无漂移门禁，避免历史批次经过后续合法 redo 后使维护性全量重建永久失效。

有界查询示例：

```bash
python3 scripts/personalization_corpus_408.py --repo . query \
  --formal-id OS_2023_004 --limit 3
python3 scripts/personalization_corpus_408.py --repo . query \
  --point-id OS04-01 --limit 3
```

查询上限为 1 到 8。查询只读取 manifest、index 与被选中的少量 object，不扫描 capture ledger、正式表或完整 Obsidian 库。
