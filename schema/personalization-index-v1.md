# 408 个性化索引协议 v1

本协议定义 408 日常学习 Harness 的答案安全个性化派生层。它解决三个问题：把四科考点编译为机器目录，把统一复盘账本重放为稀疏个人状态，以及从考点或机制反查正式题目与有效最近作答日期。

这套索引全部是可删除、可重建的投影，不是真源，不授予正式写入权限，也不修改正式节点、关系边、掌握度、正式日期或复做记录。

## 一、真源与只读输入

构建器只读取以下来源：

```text
知识点标签表.md
节点总表.md
复习单元节点映射.md                         可选
wiki/study_vaults/408-full/state/review-loop/events.jsonl
~/.codex/kaoyan-408-obsidian-bridge/records.json      可选
```

其中：

- `知识点标签表.md` 提供 629 个规范考点和科目、模块层级。
- `节点总表.md` 提供正式节点、规范考点编号、来源 ID 和正式最近复做日期。
- `复习单元节点映射.md` 只读取正式节点 ID、主复习单元 ID 和次复习单元 ID，不读取闭卷问题或答案性内容。
- `events.jsonl` 是学习证据真源；个性化状态必须能从它重新重放。
- bridge 只用于把正式节点绑定到 question-only 资产。构建器只计算题面资产 SHA-256，不能把题图像素、题干 OCR、详情卡正文或 solution 资产复制进索引。

构建器不得解析或复制完整题干、选项、答案、解析、截图像素、手写内容或 `复习单元总表.md` 中的闭卷问题。唯一允许读取题面资产字节的场景，是为 bridge 已声明的 question-only 资产计算并核验 SHA-256；不得执行 OCR、内容抽取或把资产本体写入投影。

## 二、默认派生位置

默认目录：

```text
wiki/study_vaults/408-full/state/personalization/
```

固定生成八个根 JSON：

```text
catalog.json
event_mappings.json
learner_state.json
failure_signatures.json
question_index.json
variant-catalog.json
cross-variant-catalog.json
manifest.json
```

目录、映射、稀疏状态、题目索引和 manifest 使用
`schema=personalization_index_v1`；失败签名索引使用
`schema=failure_signature_index_v1`；题面清单使用独立的
`question_source_manifest_v1`。

bridge 可用时，每个节点必须发布 `canonical_question_assets`，逐项包含 details-root 相对路径、字节数和 SHA-256；`question_asset_count` 只统计这组 canonical 资产。卡片内嵌但不位于正式节点资产目录的引用单独进入 `embedded_question_assets`，不得参与题源资格或 canonical 计数。personalization 构建器只消费并逐项重验 canonical 清单，不得另行扫描出第二套题面集合。

只有 canonical 资产具备 `schema/question-source-answer-isolation-v1.md` 定义的完整独立证明链，才生成 `question_sources/正式节点ID.json`。文件名、目录布局、question 计数或 embedded 引用不能推导 `answer_isolated=true`。证明缺失、漂移或无法重放时保留显式 source gap，且 `original_question_eligible=false`。清单自身的 SHA-256 进入 `question_index.json`，供 linked followup 注册时再次核验。

验证模式的热路径还可在 `selection_receipts/` 生成一次性、答案安全的选择回执。回执不是第六个根投影，也不是真源；它必须由 `learner_state.json` 中的 `selection_commitments` 与 manifest 的 learner-state SHA-256 共同背书。冷重建会清除旧回执和 commitment，任何尚未首次作答的动态题随即 fail-closed，不能沿用旧选择。

跨科候选关系的 `cross-subject-shadow.json` 是独立投影，不由本构建器生成。如果它在构建前已经存在且通过 schema 与 `formal_write_count=0` 校验，`manifest.json` 绑定它的 SHA-256；否则明确声明本次 build 未包含它。绑定影子投影不等于审核或正式晋升候选边。

普通变式目录与跨科变式目录由冷重建完整调用各自流水线的
`rebuild` 和 `validate`。`cross-variant-catalog.json` 固定使用
`schema=cross_variant_catalog_v1`、`kind=verified_cross_variant_catalog`。
即使当前没有生产跨科变式题，也必须生成 `entry_count=0` 的合法目录，并把精确
SHA-256、`verified_cross_variant_sources=0` 和 independent projection 记录写入根
manifest。存在任一题源漂移、盲解链断裂或公开目录夹带材料时，冷重建整体失败，不能
保留旧目录冒充当前 build。

## 三、静态考点目录

`catalog.json` 包含：

- `subjects`：DS、CO、OS、CN 的名称、模块和考点数量。
- `modules`：规范模块 ID、名称和所属科目。
- `points`：629 个规范考点，字段为 `point_id`、`name`、`label`、`subject`、`subject_name`、`module_id`、`module_name`。
- `name_index`：规范化名称到考点 ID 的倒排表。

知识点表开头用于解释编号规则的示例不能进入目录。只有已经声明模块之后出现的考点行才是正式目录项。重复考点 ID 或生产构建不是 629 项时必须失败。

## 四、事件归一化

`event_mappings.json` 为每个事件生成一个安全映射，至少包含：

```text
event_id
event_kind
observed_date
mapping_status
mapping_sources
point_ids
context_point_ids
mechanism_ids
formal_node_ids
question_node_ids
unresolved_reasons
```

不得复制 `first_break`、题面、选项、答案或完整反馈。

### 4.1 考点映射

映射优先级：

1. `knowledge_point` 中存在有效规范编号。
2. `knowledge_point` 与规范名称唯一精确匹配。
3. 唯一、保守的名称或缩写匹配。
4. `formal_node_id` 或 `mapped_formal_node_ids` 经 `节点总表.md` 取得主知识点。
5. repair 事件通过相同 `session_id + item_id` 继承首次 outcome 的映射。

若多个候选正式节点指向不同主知识点，又没有显式知识点证据，则不投票、不任选一个，`point_ids` 保持空，并写入 `ambiguous_formal_node_primary_points`。

`context_point_ids` 可以保存正式节点的全部命中考点，用于检索扩展；稀疏个人状态只更新直接的 `point_ids`，避免把一次错误扩散成多个未经证实的薄弱点。

### 4.2 规范机制

机制 ID 有两种：

- 正式复习单元 ID，例如 `RU_CO_DMA_PROCESS`。
- 考点兜底机制，例如 `KP:CO06-20`。

显式、合法的 `review_unit_id` 或 `mechanism_key` 优先。缺少显式机制时，可以由唯一正式节点复习单元映射取得；再缺失时才使用 `KP:考点ID`。多个正式机制冲突时保持未解析，不强行归并。

### 4.3 题目身份

`formal_node_ids` 可以是检索候选集合，但它不等于用户实际做了其中每一道题。

只有以下 outcome 证据能进入 `question_node_ids` 并更新原题冷却日期：

- `practice_mode=original` 时有效的 `actual_question_formal_node_id`，或同一动态原题中显式有效的 `formal_node_id`；
- `presentation_kind=original_question` 且能唯一绑定正式题目；
- `source=daily_practice` 且显式提供有效 `formal_node_id`。

普通知识回忆、复习单元映射、唯一来源 ID、唯一候选正式节点和正式题目上下文只能用于检索，不足以证明原题题面真的出现过，因此不得推进冷却日期。repair 可以继承上下文身份，但本身仍不推进日期。

存在多个题目候选时必须写 `ambiguous_question_identity`，不得同时更新多道原题的最近作答日期。

`practice_mode=variant` 是硬例外。变式可以通过 `variant_of_formal_node_id`、来源和复习单元继承原题的考点与机制，但 `question_node_ids` 必须为空；来源 ID、复习单元唯一映射和唯一正式候选都不能把变式伪装成原题作答。只有 `practice_mode=original` 的真实原题，或者没有 linked-practice 标记的普通原题 outcome，才能推进原题 7 天冷却日期。若 original 同时提供 `actual_question_formal_node_id` 与 `formal_node_id`，前者是实际展示题目的权威身份。repair 可继承考点和机制用于诊断，但不推进题目冷却日期。

### 4.4 映射状态

- `resolved`：至少一个目标实体已可靠解析且没有未解决问题。
- `partially_resolved`：有可靠实体，同时仍存在未知或歧义字段。
- `evidence_insufficient`：没有考点、机制或题目实体可可靠更新。

后两类事件都保留在 `unresolved_event_ids`，不能静默丢弃。稀疏学习状态只统计 outcome 和 repair 的未解析证据；候选确认、处置等行政事件不会伪装成学习证据。

## 五、稀疏个人状态

`learner_state.json` 分为：

```text
points
mechanisms
questions
evidence_insufficient
```

只有被学习事件实际命中的实体才进入这三张状态表。每个实体维护：

```text
outcome_count
repair_count
independent_correct_count
failure_count
fragile_count
stable_retention_count
recent_7d_outcome_count
recent_7d_failure_count
recent_30d_outcome_count
recent_30d_failure_count
last_attempt_date
last_outcome_date
last_failure_date
last_independent_correct_date
last_stable_retention_date
evidence_event_ids
```

`partial`、`wrong`、`uncertain` 计入失败。repair 单独计数，不改写首次 outcome，也不伪装成跨日稳定证据。

每个实体的 `evidence_event_ids` 最多保留最近 5 个，用于热路径幂等和证据定位；切片器对外最多选取其中 3 个。`evidence_insufficient.event_ids` 最多保留最近 64 个。累计计数不会因为 ID 截断而丢失。每日窗口自然滑动时应通过全量重建刷新，当前 answer-turn 只增量叠加本次事件。

## 六、近期失败签名

`failure_signatures.json` 从 canonical failure outcome 构建最近 30 天的答案安全
语义层。它保存受控对象、机制原语、前提、状态变化、因果、边界、错误模式、方法
模板、短 first-action/first-break 标签及 provenance，并以 event、question、point、
mechanism 和受控 facet 做有界倒排。repair、正确结果和完整自由文本不进入该层。

每条签名与事件映射和内容哈希绑定；总量最多 512 条，单一倒排键最多 8 条。热
路径只把当前 canonical event 增量叠加到这份投影，不扫描 ledger。

## 七、题目倒排索引

`question_index.json` 包含：

- `questions`：每个正式节点的安全题源、考点、机制和日期。
- `mechanisms`：机制到考点和正式节点的静态映射。
- `by_point`：考点到正式节点的倒排表。
- `by_mechanism`：机制到正式节点的倒排表。

每个 question 至少包含：

```text
node_id
subject
primary_point_ids
point_ids
mechanism_ids
reliable_source
formal_last_review_date
ledger_last_attempt_date
effective_last_attempt_date
effective_last_attempt_source
ledger_evidence_event_ids
```

### 7.1 安全题源状态

`reliable_source` 只保存来源身份、题面清单定位与哈希，不复制详情说明：

- `verified_exam_locator`：存在规范 `EXAM408-YYYY-QNN`，且 bridge 已生成 answer-isolated question-source manifest。
- `verified_question_surface`：非规范真题 ID，但正式详情 bridge 已验证 answer-isolated question-source manifest。
- `detail_locator_only`：只有耐久详情定位，不足以证明存在答案隔离的原题表面。
- `unverified_or_missing`：没有可靠来源。

题源对象同时给出 `canonical_exam_id`、`source_ref`、`source_ref_kind`、`source_sha256`、`surface_verified` 与 `answer_isolated`。真正可激活的 `source_ref` 必须用位置无关的 `personalization-source:` 定位生成的 question-source manifest，`source_sha256` 是该清单的内容哈希；题源加载器还要按清单逐个验证题面资产 SHA-256。同一真源与日期构建到不同输出目录时，根投影、`build_id` 和 manifest 必须逐字节一致。

`original_question_eligible=true` 只在 answer-isolated 清单已验证时成立，但仍不代表 7 天日期、session 去重和关联边激活门禁已经通过。只有 canonical locator 或 Obsidian URI、却没有可靠题面清单时，必须保持 false。

### 7.2 有效最近作答日期

```text
effective_last_attempt_date = max(
  formal_last_review_date,
  ledger_last_attempt_date
)
```

`effective_last_attempt_source` 只能是：

```text
formal_and_ledger
review_ledger
formal_node
unrecorded
```

这里的 `ledger_last_attempt_date` 只由确定题目身份的 outcome 更新。仅被展示、入队、repair、映射为多个候选或作答 grounded variant 都不算该原题的真实首答，不能更新原题日期。

## 八、冷路径与热路径 API

稳定冷路径函数：

```python
build_all(repo, today=None, out_dir=None)
```

它读取必需来源与可用的 bridge，并原子重建根投影及 question-source manifests。命令行等价入口：

```text
python3 scripts/personalization_index_408.py build \
  --repo /path/to/kaoyan-408 \
  --as-of YYYY-MM-DD
```

稳定热路径函数：

```python
load_projection(state_dir)
apply_event_to_projection(
    event,
    catalog=...,
    learner_state=...,
    question_index=...,
    as_of=...,
    prior_mapping=None,
)
```

`load_projection` 不加载 `event_mappings.json`。`apply_event_to_projection` 只处理传入的 canonical event，并只更新受影响的考点、机制和唯一题目；它不得扫描 `events.jsonl`。重复应用仍在最近事件 ID 窗口中的同一 event 必须返回 `no_state_change`。

`apply_event_to_projection` 默认只修改调用方提供的内存投影，是否原子持久化由 Harness 协调器决定。它不修改任何真源。

只读查询：

```text
python3 scripts/personalization_index_408.py query \
  --repo /path/to/kaoyan-408 \
  --kind point|mechanism|question \
  --id ENTITY_ID
```

query 只读已经存在的投影，缺少投影或 ID 时非零退出；它不能为了方便自动触发 build。

## 九、Manifest 与可重建性

`manifest.json` 保存：

- `build_id`。
- `as_of`。
- 每个真源 SHA-256。
- 可用时的 details bridge snapshot SHA-256，以及题面清单通过数和缺口数。
- 五个非 manifest 根投影的 SHA-256，以及每个已验证 question-source manifest 的内容哈希。
- 考点、正式节点、事件和稀疏状态数量。
- `answer_safe=true`。
- `formal_write_count=0`。
- 近期失败签名计数与倒排边界。
- 可选夜间 corpus 的 manifest、index 和 build ID 精确绑定。新的 corpus 只有在根
  manifest 原子发布后才能进入热路径。
- 独立跨科影子投影声明；若已绑定则包含其 SHA-256。

相同真源与相同 `as_of` 必须产生相同 `build_id` 和相同四个派生文件哈希。

## 十、答案安全与正式写入边界

任何派生文件不得保存：

- 完整题干、选项、答案、解析或用户错选。
- 未经扫描的 `first_break` 自由文本或完整用户回答。允许内容扫描通过、长度受限且
  provenance 明确的答案安全短标签进入 failure signature。
- 图片、手写痕迹和临时附件路径。
- 正式掌握度结论或正式关系边。

本层可以保存结果枚举、置信度、事件 ID、规范考点 ID、机制 ID、正式节点 ID、来源 ID、安全 URI 和日期。它们都只是检索与个性化上下文的派生证据。

构建和增量叠加必须始终保持：

```text
formal_write_count=0
```
