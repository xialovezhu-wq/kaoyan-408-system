# 408 统一知识与记忆读取合同

版本：`cs408-memory-access-v1`。实现：`scripts/memory_access_408.py`。

## 所有权与来源

本层是既有正式库、KLR、KSP、个人概况、考纲关系和 Capture 的只读访问层。它不分配正式题号、不创建学习事件、不评分、不改变 FSRS，也不从旧概况文字补造结构化判断。

目录由现有 `exam_tutoring_context_408.py refresh-nightly` 冷构建，在 manifest 的 `memory_catalog` 中登记内容寻址引用。目录仅保存稳定身份、类型、标签/别名、权威写者、来源、依赖、允许视图与冷热位置；正文保留在原对象。目录描述符、桶和每个实际读取对象均校验 SHA-256，沿用原 manifest 的新鲜度检查。失效、缺失或篡改时失败关闭，不在学习查询中自动重建。

旧 `wiki/concepts`、`topics` 等页面在未绑定当前来源版本前属于 `historical_supplementary`；存在页面、提及正式 ID 或 coverage=compiled 不能证明当前个人语义有效。

## 入口

统一入口：

```sh
python3 scripts/runtime_paths_408.py --run exam_tutoring_context_408.py memory <操作> --repo .
```

也可直接调用 `scripts/memory_access_408.py`。Python 入口：

```python
access(repo, operation, selector=None, *, view="protected", output_root=None, **options)
```

选择器支持 `id`、`concept`、`formal_id`、`source_id`、`current_question_id`、`question_part`、`mechanisms`。CLI 对应 `--id`、`--concept`、`--formal-id`、`--source-id`、`--current-question-id`、`--question-part`、可重复 `--mechanism`。题号、KSP 和规范点走现有精确路由；不以模糊结果替代身份。

| 操作 | 输入 | 输出边界 |
|---|---|---|
| `catalog` | 可选精确选择器；`--offset`、`--limit` | 有界目录页；没有选择器时每页最多 100 个安全条目 |
| `context` | 精确选择器；`--materials core\|personal\|visual\|all`、`--known-version` | 委托原生 full-personal resolve；不重复生成个人概况 |
| `history` | 精确点/KSP/题；`--event-after`、`--event-before`、`--known-before` | 选中来源范围内记录、未知时间和不能验证的范围分别返回 |
| `compare` | 精确选择器；`--left-json`、`--right-json` | 两组时间条件的证据集合与差集，不自动判定掌握变化 |
| `trace` | 精确选择器；`--record-key` 或 `--claim-id` | 选中 KLR 或判断的来源、原回合定位、更正引用与生命周期 |
| `relations` | 精确选择器 | 已审核关系的类型、方向和来源；不构建新边 |
| `preferences` | 精确点/KSP | 有证据和适用范围的偏好；偏好不代表学习效果 |
| `discover` | `--query`、`--intent auto\|concept\|experience`、`--actor user\|assistant\|any`、时间/范围参数 | 最多 8 个候选；概念命中后resolve，经历命中后沿trace_request回读 |
| `lint` | 精确选择器 | 对象哈希、个人概况状态、旧格式判断的有界只读检查 |

`compare` 的两个 JSON 只接受 `event_after`、`event_before`、`known_before`。查询文本不超过 1000 字符。`subject` 仅接受 `408`。

## 时间与更正

- `event_at` 来自已有 `observed_date`，表示发生日期。
- `recorded_at` 来自原写者明确记录时间；不借用文件修改时间或猜测导入时间。
- 日期只有日精度时，按上海时区比较；截止日期覆盖当日结束。
- 现有正式语义 annotation 的追加链通过 `load_formal_record_history` 按原 annotation SHA 精确验证和回放。截止时间后的更正不进入历史结果；撤回后当前事实为空，之前已核实版本仍可历史查询。
- 没有 `recorded_at` 的旧记录进入 `unknown_time`，不能宣称“系统当时已经知道”。无法安全回放的其他旧更正进入 `unavailable_revisions`。
- 历史查询不得将今天的完整个人概况倒填成过去的概况。当前实现返回可验证的历史证据和版本，不自动生成历史掌握判断。
- `compare` 只报告记录集合的变化，不把新增、修复或出现频率转换成掌握度。

## 范围与答案保护

默认 `view=protected`。`after_attempt` 不自动开放答案。

- 已绑定当前题且仍处于 protected 时，仅返回已公开的精确题 ID；不得返回由该题推导出的父点、小点、关系或个人历史。
- `history`、记录 `trace` 在 after_attempt 下只返回来源、日期、角色、提示依赖等元数据，不返回历史摘要、结果或答案。
- `preferences` 在 after_attempt 下仅返回已审核的适用范围、呈现形式和偏好文本，以及去除原回合引用的来源元数据；完整原话只在 direct 下读取。效果判断、next_check 不混入该安全投影。
- 已有偏好来源过期时返回 `stale_sources` 与 `insufficient_evidence`（同时存在有效结果时为 `partial`），不改称 `no_record`；KSP继承父概况的有效状态。
- `materials=core|personal` 将完整个人字段仅放在 `records[].personal_context`，以 `personal_fields_ref` 指明位置；不在记录顶层重复长时间线。`all` 保留旧字段布局，正文完整性仍由 `personal_read_receipt` 核验。
- 本机讲题的 `resolve --personal-scope teaching` 使用[有界讲题读取](exam-tutoring-context-v1.md#有界讲题读取)。`memory context` 仍按原精确范围读取完整档案，遵守下列限制。
- 显式 Pxx 不能展开父题或兄弟问。没有可核验当前小问绑定的父点历史、判断或偏好返回 `scope_unverified`/`insufficient_evidence`，direct 也不解除这个边界。
- `--exclude-year` 在目录、候选、历史回放及关系结果中执行。正式题通过已核 canonical exam binding 判断；年份归属未知时不得宣称通过检查。
- 旧完整个人概况尚无逐来源年份证明时，`context --exclude-year` 返回 `insufficient_evidence/full_context_source_year_scope_not_proven`，不把允许父点误当成其全部历史来源的授权；可以继续当前题事实或读取经过筛选的历史元数据。
- `--allowed-id` 限定允许身份。KSP 历史需依据原 record key、来源身份或源 SHA 绑定，不能把整个父点历史当作该机制证据。
- 历史重建之前和之后都执行相同范围筛选，防止更正链回放带回被排除题目。
- 关系类型保留主考、兼考、前置、背景、正式 R01–R09、来源题和证据依赖的区别。相似度不建立任何一种正式关系。

本层关系与发现结果不是主动复做推荐。若要推荐题目，必须继续进入当前选题模式的真实接触日期与年度保护入口。

## 待审 Capture

`catalog`/`discover --include-pending` 显式启用待审目录。读取既有签名 capture joint manifest 和有界 hot state，验证状态 SHA、路径、账本长度和读取期间代际一致；不回放完整账本，不读取原始对话。

待审条目只包含 Capture ID、真实日期、原生状态、来源哈希和可核验的原题身份，标记 `pending_formal_review` 与 `formal_claim_authority=false`。没有合法安全主题时仅支持 Capture ID/原题 ID 精确发现，`semantic_coverage` 明示不进行未经审核的主题抽取。新 Capture 不因此进入当前正式个人判断。

待审读取失败只报告 `pending_index.status=unavailable`，不重建状态、不重放快存、不修改正式记录。

## 搜索组件

### 机制定位与个人经历分流

已有题号、正式ID、规范点或KSP继续直接resolve；不先搜索。只有自然语言描述时才discover：

- `--intent concept`：定位概念、机制或考点，返回安全候选身份，再结合当前题面精确resolve。
- `--intent experience`：定位实际发生过的作答、修复、明确陈述或助手讲解，返回证据候选及`trace_request`，不把个人历史查询降成同名考点搜索。
- `--intent auto`为默认分流；当前请求明显属于哪类时显式传入，返回的intent只说明路由，不证明命中正确。
- `--actor user`为默认，仅针对用户经历；找助手先前解释时显式用assistant。any保留各命中实际角色，不能把助手讲解记成用户作答。
- `--event-after/--event-before`约束真实发生时间；`--latest/--no-latest`明确是否在相关候选内按最近发生时间排序。省略时可从“上次、最近一次”等请求识别。日期未知、同日并列、候选覆盖有界仍须保留；不能把返回首项宣称为全历史中已证实的最后一次。
- `--known-before`请求当时已知的信息；经历索引不能回放过去解释时返回insufficient_evidence及原因，需按精确身份进入history的版本回放，不拿当前解释冒充过去状态。

```sh
python3 scripts/runtime_paths_408.py --run exam_tutoring_context_408.py memory discover --repo . --query '<公开机制描述>' --intent concept --view protected --limit 8
python3 scripts/runtime_paths_408.py --run exam_tutoring_context_408.py memory discover --repo . --query '<要找的历史经历>' --intent experience --actor user --view direct --latest --limit 8
```

`--concept/--mechanism/--current-question-id/--allowed-id/--exclude-year`继续限定范围，不能为了增加命中而移除实际约束。当前题绑定、Pxx和年度保护在读取、排序前执行。候选的trace_request保留相同view和范围；沿该请求读取，不能手工省略限制。

### 经历内容与原话边界

protected拒绝个人经历搜索。after_attempt只有未绑定当前题的历史查找可返回来源哈希、原回合位置、日期、角色、提示依赖等元数据，不加载body或返回历史摘要；有当前题绑定时不通过经验搜索扩展其历史答案。direct也仍须遵守题目、机制、授权身份与年份范围。

direct命中通过`snippet_kind`区分`native_record_summary`与`verified_original_turn`：前者是原生审核摘要，不能加引号当作用户逐字原话；后者必须有原包、来源SHA和turn核验，只在无当前题、KSP/point及allowed身份约束的历史查找中使用。受约束查询不能通过原始整回合绕回其他题或机制。片段还须核对snippet_span是否截断，需要全文或引用时沿trace_request回读精确来源；verified_original_turn也不等于整段对话已完整读取。

查询覆盖和来源不可用应如实呈现。没有命中不证明没有该经历；同题关联、编辑摘要、助手解释、提示后修复和独立首答保持不同证据角色。经历被检索、查询次数、备课、图解生成都不产生学习事件、弱点或掌握结论。

### 本地实现与可替换后端

原生词法检索使用固定中文分词与字段BM25，对概念标题、别名、机制字段和经历摘要/陈述采用不同字段权重；别名先按来源与身份清理。它帮助找候选，不提供正确性、独立掌握或全量覆盖证明。

默认`--backend native`保留既有入口，由本地资格策略决定概念发现后端：非精确concept请求在资格有效时使用`hybrid_local`（词法加本地语义，不含reranker）；精确身份继续零模型读取，experience继续原生经历检索。显式`--backend lexical`可只用词法。

策略位于`.private/memory-retrieval-runtime/discovery-policy.json`，绑定当前公共检索投影SHA与本地资格回执SHA。模型、版本或资格不可用时，以`backend_policy.degraded`明确原因后回落原生词法；不扩大身份/题目/年份范围。查询不重建索引、不下载模型、不联网。reranker仍为可选试验，不默认启用；资格通过不等于所有查询正确或全历史覆盖。

外部本地后端通过 `memory_search_backends_408.search_candidates` 接受同一安全 documents 白名单。只允许返回白名单身份，丢弃后端片段和自行附带正文；显式请求的独立试验后端不可用时如实返回 unavailable，不静默调用云端 API。组件不接管权威 Markdown 文件，私有经历不会进入概念混合后端，模型相似度不能建立个人判断。

## 结果状态

| 状态 | 含义 |
|---|---|
| `available` | 当前允许范围内有可读取结果 |
| `candidates` | 找到待精确核验的发现候选 |
| `not_found` | 本次未定位，不能据此断言没有记录 |
| `no_record` | 选中且已核验的来源范围内没有对应记录 |
| `insufficient_evidence` | 时间、绑定或证据不足，不能得出所求判断 |
| `protected` | 当前权限或题目范围要求不返回内容 |
| `unavailable` | 版本、来源、哈希或组件未就绪 |

结果带 `source_version`、实际读取对象与 SHA 的 `read_receipt`，并明确 `formal_write_count=0`、`learner_evidence_write_count=0`。省略或无法证明的部分通过 `complete=false`、`unknown_time`、`unavailable_revisions` 或 `scope_unverified` 显式呈现。

## 验证

`tests/test_memory_access_408.py` 使用隔离的不可变对象和原生哈希路由，覆盖来源篡改、过期、更正回放与撤回、双时间、受限查询、Pxx、KSP、待审隔离、中文发现、来源追踪、偏好与无写入。生产冷构建和发布由原 owner 执行；本层无更新命令。
