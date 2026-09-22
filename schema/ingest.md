# LLM Wiki Ingest Schema

本文件规定新资料进入 408 LLM Wiki 兼容层时的分类、边界和升级条件。

## 入口判断

每次 ingest 前，先判断输入属于哪一类：

| 类型 | 含义 | 允许写入 |
|---|---|---|
| raw source | 外部资料、历史来源、NotebookLM、网页、课程资料 | `raw/index.md` 来源登记 |
| wiki-only | 概念解释、专题整理、易混点、错因模式 | `wiki/` 下对应页面 |
| candidate-node | 有具体错题事实但信息不足 | wiki 候选记录；不得进正式表 |
| practice-candidate | 练习题/自测题候选，尚未由用户真实做错或未满足正式错题条件 | 只写 `题库候选/`；不得进正式表、关系边、知识点命中索引或专题链 |
| legacy-queued-question | 2026-07-12 前遗留的异步任务 | 只允许历史对账、WAL/回执恢复；不得接收新题或自动续跑 |
| formal-node | 用户明确要求正式 408 入库且信息足够 | 使用 `kaoyan-408-wrong-intake` 同步处理单题 |
| rollback-review | 用户完成复习并给出评分/表现 | 按 `复盘调度规则.md` 原流程处理 |

## Raw Source 处理

- 只登记来源名称、位置、类型、用途和限制。
- 不复制完整题干、完整解析、答案、图片或手写痕迹。
- 参考书、讲义、网页、NotebookLM 导出只能用于概念校准和 wiki 候选。

## Wiki-only 处理

- 可写概念页、专题页、易混点页、错因模式页、关系综合页或复盘策略页。
- 可以引用正式节点 ID 作为证据，但不能复制节点总表整行或完整题目内容。
- 可以沉淀“候选关系分析”，但不能直接写入 `关系边表.md`。

## Candidate-node 处理

有具体错题但缺少 ID、来源、日期、主知识点、错因或详情入口时，只能记录为候选。

- 缺 ID：不得写入 `节点总表.md`。
- 缺主知识点：不得编造；写“待确认”。
- 缺错因：写“暂无明确错因”或“待确认”，不得强行贴 E01-E10。
- 缺日期：写“未记录”，不得编造。
- 缺来源：写“待补充”，并提示用户补齐。

## Formal-node 处理

只有用户明确要求进入正式 408 入库流程，且满足本地规则时，才允许修改正式文件。正式流程仍需读取并遵守：

- `AGENTS.md`
- `节点总表.md`
- `关系边表.md`
- `年份索引.md`
- `知识点标签表.md`
- `知识点命中索引.md`
- `概念词典.md`
- `错因标签表.md`
- `关系规则.md`
- 对应科目节点库、知识体系和专题链

正式入库还必须创建或更新同名安全节点卡片 `DS/CO/OS/CN_年份_三位序号.md`。卡片必须保存足够复做的安全题目摘要、关键题设参数、问法类型、用户错误入口和复做第一动作；禁止留下空白卡片。安全卡片仍不得保存完整题干、完整解析、答案、选项、图片或手写痕迹。

单题入库仍不得读取、生成或修改 `复盘输出/`。

### 当前题快速捕获、Luna 深析与 Sol 编纂入口契约（2026-08-04，已退役）

以下 2026-08-04 条目只解释历史对象，不再授权任何新 Capture、observation、
bundle、后台 Luna/consumer 或 `awaiting_daily_curation` 写入。当前合同是：managed
非终态只写同题私有 draft；semantic terminal 后封存 final package、创建并重开验证
唯一 Capture，再导航/hold/close；普通 standalone 只读，明确快速入库才创建一个
`awaiting_sol_formalization` Capture；明确正式请求再由 `backlog-plan` 与单日期
`start-batch` 串行编纂。

- 晨间复盘和普通讲题只使用 `CurrentQuestionContext v1` 与独立私有 grader capsule。主对话禁止读取 personalization、MEMORY、历史、正式库、关系、其他题、variant、Luna、8767、参考书和源码；禁止全账本扫描与全局审计。
- 主模型先冻结当前题反馈，再仅由 `scripts/managed_408_current_turn.py ... answer-current-and-next` 一次完成最小首答、必要 session 回执、observation/条件 capture 和回执门控的至多一个后继题面。`current-turn`、`publish-evaluation-capsule`、`continue-current`、`next-item` 与旧 direct router 均不是公开答题旁路。
- 高置信、无提示、无推理断点的独立正确不生成错题 capture，而是生成一个私有 `current-question-study-observation-v1`。错误、部分正确、空答、不确定、可验证推理断点和正确但中低置信按 `current_question_failure_standing_policy_v1` 恰好生成一次 `awaiting_daily_curation` capture，正式写入数为 0。
- 完整题面/附件、答案依据、grader、用户作答与推理、冻结反馈和 provenance 只进入 Git 外权限受限的 `current-question-evidence-bundle-v2`。capture ledger 只保存 manifest locator/hash、bundle hash、答案安全事实、缺口、日期与幂等回执。
- 快速捕获固定为 0 子智能体、0 formal preflight/apply、0 全库前置读取、0 关系/专题链/旧题检索、0 全局审计。首答/session 失败时不揭示答案性反馈；私有证据、observation 或 capture 失败时保留冻结反馈和当前位置并精确恢复。恢复保持结果类型：独立正确只补 observation，不能生成 capture；capture-eligible 只补 bundle/capture。恢复成功闭合原答题 operation；旧放行回执只有仍位于 session 导航前沿时才有效。
- LunaMax 只在后台读取当前 capture 绑定的私有 bundle，串行执行两阶段分析和批判复核，生成内容寻址的 `study-intake-luna-analysis-v2` JSON 与确定性 Markdown。聊天不得调用、等待、轮询或检查 Luna；任何 Luna 失败不改变 capture 成功。
- 用户明确指定本地日期后，`kaoyan-408-daily-intake-curation` 才冻结 ordered capture-set。Sol 逐题重开原始 capture 与 bundle、恰好消费一次可选 Luna 报告、逐字段 adopt/modify/reject，再严格串行执行 formal `batch size=1`。
- Codex 多智能体不是稳定提速面。真实 benchmark：compact A 真新题串行 `60.75s`、并行 `64.62s`；复杂 C 串行 `53.28s`、并行 `67.03s`；F 串行 `49.18s`、并行 `56.99s`。条件式质量分支只适用于日终 `curate_one` 或正式维护；快速捕获无条件为 0。
- 所有 `collaboration.spawn_agent` 必须显式使用 `fork_turns="none"`。主任务为每个角色单独构造最小脱敏快照，只传安全题目骨架、关键参数、已确认错误入口、必要索引定位与快照哈希；完整题干、答案、错选项、图片内容、临时附件路径和整段对话不得进入子任务 prompt。
- 高风险 A 角色固定为 `facts_safety`、`taxonomy_error`、`duplicate_relation`；高风险 B 使用 `facts_safety`、`taxonomy_error`，查重/关系由主任务完成；复杂 C 使用 `redo_safety`、`redo_consistency`；同时存在真实关系类型/强度争议和质量风险的 F 使用 `relation_semantics`、`relation_graph_guard`。F 的 `relation_graph_guard` 是独立 challenger：不得等待或读取 `relation_semantics` 候选，必须直接从同一冻结快照返回 admissible types、max strength、prohibited types 和静态图检查结果。子智能体只返回结构化建议、证据、反证和冲突，不得写正式库、details、coverage、bridge、WAL 或 receipt，不得分配最终 ID，不得创建 `JOB-*`，也不得调用 `intake_apply_408.py`、preflight、刷新或 closeout 命令。
- 主任务是唯一 fan-in 和唯一 package 生成者。它必须在 preflight 前等待全部已启动分支并汇合为恰好一个 canonical package；并行分支对用户事实、重复项、new/redo、主知识点或答案泄露存在冲突、超时或异常时，当前题必须 fail closed，apply 调用次数为 0，不创建 `JOB-*`，不进入正式事务。
- 日终模型只可编纂安全摘要、参数、题型、核心考点、主/副/命中知识点、模糊概念、有证据的错因标签、复做第一动作、关系/专题链候选和 review-unit 措辞。用户事实、来源/details 身份、日期、new/redo 事实、复做历史/次数、提示与正确性、附件 provenance、掌握度、调度、receipt/WAL 不可静默改写；冲突转 `needs_user`。合并、删除和改 ID 必须另行授权。
- 新 current-question standing-policy capture 直接进入 `awaiting_daily_curation`。旧 `captured_unconfirmed` 只保留历史只读兼容，不追溯改写。已存在正式节点经完整校验无需改动时，使用 `already_current` 终态并绑定正式 ID 与 verification SHA-256，不伪造 receipt 或重复 apply。
- `scripts/intake_apply_408.py` 是公开正式写入口。它必须拒绝多题输入；内部允许复用批量事务引擎，但只能以 `batch size=1` 运行，并继续使用仓库级锁、WAL、幂等回执、before/after hash 和恢复记录。
- `scripts/intake_parallel_orchestrator_408.py` 是日常单题流水线编排层：持有跨 apply/closeout 的单题栅栏，只调用一次公开写入口。`preflight/apply/closeout/related_candidates` 禁止通过 `collaboration.spawn_agent` 执行；postcommit/closeout 禁止启动 Codex 子智能体，只允许本地进程 DAG。该 DAG 是 benchmark 后确认保留的稳定提速面；coverage 是 extended audit 的前置依赖，bridge 是详情/HTTP 校验的前置依赖，任何必需分支失败都不能标记完成。
- closeout 失败且 normal receipt 已存在时，只允许 `--resume-receipt` 绑定原 receipt/WAL 和当前 after hash 后重跑收尾；不得重新 apply。
- 正式 ID 只能在仓库提交锁内、基于最新正式库分配。锁内不得等待用户、Tutor、网络或其他任务。
- 关系复核状态只能是 `connected`、`reviewed_no_reliable_edge`、`needs_user`。允许经审阅后不建边，禁止为通过审计强建弱边。
- 文件发布只表示事务提交完成；正式表、details、review-unit-v2、bridge、coverage 与全量审计全部通过后，才能向用户报告本题完成。
- 缺事实、疑似重复或 new/redo 不确定时，正式 apply 保持 0；日终可把该 capture 标为 `needs_user` 并继续处理其他相互独立的 capture，但不能隐藏该失败项。
- 旧队列、WAL、回执和 evidence/details inbox 仅供历史恢复，不删除、不接收新题、不自动续跑。已有 successful receipt 的事务不得重新 apply。

## Rollback-review 处理

- 只有用户给出复习表现或 0-5 掌握度评分时，才按 `复盘调度规则.md` 更新复习相关文件。
- 未评分时写“待评分”，不得代替用户评分。
- LLM Wiki 的 `wiki/log.md` 只记录 wiki 层动作，不替代复做记录。

## 本兼容层默认行为

如果一次 ingest 只涉及 LLM Wiki 兼容层，最多修改：

- `raw/index.md`
- `wiki/index.md`
- `wiki/log.md`
- `wiki/concepts/`
- `wiki/topics/`
- `wiki/error_patterns/`
- `wiki/relation_summaries/`
- `wiki/coverage/`
- `schema/`

不得修改正式表格、节点库、专题链、历史归档或 `复盘输出/`，除非用户明确切换到正式 408 维护任务。

## 可复制提示词

```text
请按 408 LLM Wiki ingest 流程处理这份资料。

先读取 AGENTS.md、wiki/index.md、raw/index.md、schema/schema.md、schema/ingest.md。
请先判断输入属于 raw source、wiki-only、candidate-node、formal-node 还是 rollback-review。

默认只允许写 raw/wiki/schema 层：
- raw source 只登记来源，不搬运完整题干、完整解析、答案、图片或手写痕迹。
- wiki-only 只写概念、专题、易混点、错因模式、关系综合或复盘策略。
- candidate-node 只记录待确认，不进入正式节点。
- formal-node 必须生成安全节点卡片：安全题目摘要、关键题设参数、问法类型、错误入口、复做第一动作；不能空白。

当前题使用 terminal-final-package policy：错误、部分、空答、不确定、推理断点和解释/纠正阶段只写同题草稿，Capture/pending 为 0；答案与 sound 推理达到 terminal 后才封存最终包并创建唯一 `awaiting_sol_formalization` Capture。普通 standalone 除非最新 user turn 明确快速入库，否则 package/Capture/pending/formal 均为 0。除非我明确授权正式编纂，否则不要修改正式节点、关系、索引、复做记录或复习单元。不要创建 JOB、写旧队列、后台处理或唤醒协调器。

日终集中编纂使用 kaoyan-408-daily-intake-curation，从显式日期账本冻结清单。正式写入口仍为 scripts/intake_apply_408.py，严格 batch size=1，并复用锁、WAL、幂等回执和完整门禁。new 正式事务维护 review-unit-v2 四个表面；audit_review_schedule.py 未通过不能把 capture 标记为 curated。每题完成并输出强关联旧题回顾后，再开始下一题正式写入。

2026-07-12 前的旧队列、WAL、回执和证据仅用于历史恢复，不删除、不自动续跑、不接收新题。若旧 normal receipt 已成功但旧引擎遗漏派生项，只允许运行 receipt-bound postcommit repair 独立 WAL，不得重新 apply。
最后输出：来源分类、读取文件、写入文件、未写正式数据、待确认项。
```
