# 408 应试单题教学上下文 v1

本 schema 定义一个完全本地、零模型、答案安全的提交后物化层。它为当前一道 408 题提供全部相关知识点的最新与历史理解摘要、稳定的知识点到记录哈希索引，以及有证据的教学微调信号。个人表记录已有学习证据，不是掌握度模型、推荐器或关系扩散层。

## 日常讲题预处理入口

`resolve --concept <规范代码或确切名称> --view protected|after_attempt|direct --personal-scope teaching --materials core` 复用现有哈希路由并只读取命中对象。多个点重复 `--concept`，只有真实薄弱点才用 `--weak-concept`。题目身份优先使用精确的 `--current-question-id`，正式错题可用 `--formal-id`。`query --input-json` 用于兼容旧查询。

每点在冷构建时生成 `知识点/学习路径/<代码>.md` 和三种内容寻址讲题视图。完整记录按原始角色、日期和来源保留；摘要包含真实历史变化、时间范围、下一检查点及已核实的题目 Obsidian 地址。来源关联不等于知识点错误，未知日期不编造，事实整理不冒充经审核的语义进步。protected不返回历史解题内容、题目引用或完整历史页入口。

结果为 `knowledge_teaching_context_408_v1`。`semantic_current` 使用已接纳且覆盖当前证据的概况，`evidence_digest_only` 仅支持核实记录，`semantic_stale` 回到当前题设与作答。缺失、歧义及超预算点分别报告。

已有同题同范围读回时带 `--known-version <上次version>`。只有来源、视图和读取范围一致才复用已持有结果。正式入库或知识点复盘后的 `complete_after_commit → refresh_after_commit` 刷新本机学习路径、讲题视图、哈希路由与 `source_version`。同版共享副本按显式远程访问或外发请求另行发布。路径或摘要缺失时进入原有派生恢复。

首次回复需简短呈现相关历史变化和对应题目链接，再进入当前真实断点；所有用户可见过程说明和正文使用中文。后续只说明新增变化，不重复历史，不在教学中重建上述资料。

## 一、目标与硬边界

单题热路径的事实顺序固定为：

1. 当前公开题面、当前附件和用户本轮真实作答。
2. 当前题本地判分器或不透明 grader capsule。
3. 当前第一动作、推理和第一个断点。
4. 一次查询当前所有知识点及优先薄弱点的记录，结合最新与历史理解调整诊断、提示起点、表达方式或短迁移检查。不得把所有考查点都标为薄弱点。

历史信号不得判断当前题是否正确，不得覆盖当前证据，不得产生总画像、掌握度、跨题推荐、关系扩散、题目选择或正式写入。没有可执行教学差异时，旧教学路由 fallback 返回空 `profiles`；已有可核验知识记录仍通过 `knowledge_summaries` 返回。无记录的知识点必须在 coverage 中明确说明，不能静默丢弃。

本实现不导入也不恢复任何 retired personalization、cross-subject、variant、candidate 或 background model 路径。

## 二、冷路径输入

`refresh-nightly` 和公共提交 owner 释放事务锁后的 `refresh-current` / `refresh_after_commit` 执行冷提取，读取：

- `节点总表.md`
- 根目录同名正式安全卡 `DS/CO/OS/CN_*.md`
- `复习单元节点映射.md`
- 知识点标签、命中索引、概念和关系等受 source guards 约束的既有正式来源
- `wiki/study_vaults/408-full/state/review-loop/events.jsonl`

本次用户明确授权历史学习材料的全量提取。冷构建还读取快速入库事实、已验证本地/归档对话、历史节点与复盘摘要，以及现有 Word 来源中的文字。原始材料只通过原有验证入口或精确来源读取；私有语义工作单与标注留在外部私有目录，公开派生表不复制题干、选项、答案、完整解析或原图。图像未解释和缺失原始对话必须单列，不能把来源清点当作语义全覆盖。

关联来源用于知识归一、学习记录提取和来源校验，不用于推荐或关系扩散。首次原文语义归纳由当前授权工作中的原生 Agent 完成；日常查询和确定性提交后刷新零模型调用。已验证历史按内容/来源守卫缓存，后续正式编纂补充本次新增材料的知识点观察，复用未变化的历史。

安全卡只提取：

- 正式节点 ID、来源 ID
- 主、副、命中知识点
- 题型、问法类型
- 实际考查机制或核心考点
- 模糊概念
- 用户错误入口
- 复做第一动作

review ledger 只物化可核验的结果、置信度、提示依赖计数、直接证据的首断点，以及事件 ID 和原始行 SHA-256。`first_break_provenance` 只有 `user_report` 或 `visible_evidence` 才能进入 `top_direct_first_breaks`。通过复习单元关联得到的信号必须与正式节点直接绑定信号分栏保存，不能伪装成同一道原题的直接作答。

无正式题号或知识点编号的旧复盘按稳定来源身份建立已审阅的主题映射；同一身份后续的真实 outcome/repair 自动关联到知识点，仍标为题目关联证据，不推断该题所有知识点都错。已有成功正式绑定优先复用。覆盖报告分别列出未关联学习事件和管理事件，排程镜像不算新的作答。

已有日期修正与判分修正按原账本引用核验后投影生效。完整记录保留原日期、原结果、原始哈希与修正引用；摘要使用有效日期/结果，并明确修正不是新的学习尝试。旧 profile 与新知识点表使用一致的修正视图。

## 三、产物与发布顺序

默认根目录：

```text
wiki/study_vaults/408-full/state/exam-tutoring-context-v1/
```

布局：

```text
objects/profiles/<sha256>.json
objects/indexes/<sha256>.json
objects/knowledge/<sha256>.json
objects/learning-records/<sha256>.json
objects/point-histories/<sha256>.json
objects/point-summaries/<sha256>.json
objects/point-indexes/<sha256>.json
objects/knowledge-hot-routes/<sha256>.json
receipts/<sha256>.json
manifests/<sha256>.json
manifest.json
```

Profile schema 为 `exam_tutoring_profile_v1`。Profile 不含安全题目摘要、关键题设参数、详情入口、关系边或任何题目与答案载荷。

精确索引支持：

- `formal_id`
- `source_id`

知识索引支持：

- `knowledge_points`
- `mechanism_terms`
- `ask_type_terms`

新增知识记录索引与个人摘要表随同一 manifest 发布：稳定知识点代码指向内容寻址记录哈希；个人表按知识点保存可核验的最新及历史理解、来源 refs、直接/关联证据和初次结果/修复的区分。详情留在冷记录，热查询读取压缩摘要，不逐点扫描明细。知识点被题目考查本身不等于用户薄弱。对象路径与 schema 以实际 manifest 的引用为准。

正常查询由 manifest 中的 `knowledge_hot_routes` 进入两位 SHA-256 键前缀桶。桶内只保存该前缀下的精确 route key，覆盖正式 ID、来源 ID、知识点代码、知识点别名、机制词、问法词和目标对象引用。manifest 只保存稀疏桶的内容地址、覆盖计数和 route set 哈希，不内嵌完整键表。完整 exact、knowledge 和 point indexes 继续作为冷审计、恢复和导出证明；正常查询不得先读取它们。

正式 ID route 同时保存该题当前全部主知识点、副知识点和命中知识点绑定；查询必须全部展开，不受 profile 最多三条的限制。知识点别名分为唯一命中、官方模块展开和歧义三类。歧义别名只在 `knowledge_coverage.ambiguous_terms` 返回全部候选代码，不选择第一个候选，也不以该歧义词选择教学 profile。

所有对象先以规范 JSON 的 SHA-256 内容寻址写入并 fsync；`manifest.json` 必须最后原子替换。相同日期与相同输入重复构建必须复用同一内容地址和同一 `exam_tutoring_context_refresh_receipt_v1`。

## 四、正式及复盘提交回流

所有已验证公共 formal/review owner 通过现有 `study_commit_boundary` 在最外层事务锁释放后刷新本地投影，再请求共享发布。正式 intake、already-current terminal binding、campaign 正确/筛查 outcome、失败后 terminal repair 都更新同一知识记录哈希索引及个人表；初次结果与后续修复不得相互覆盖，也不得把提示后正确改写为独立掌握。

低层 ledger fsync 后立即使旧 context 失效。外层尚未完成或刷新失败时，下一次查询不能继续使用旧历史。云端失败不影响已提交本地事实，只补 publisher；本地刷新失败只补派生层，绝不能重放 formal apply 或 review outcome。

最终集中收尾复用或恢复当前投影：

```bash
python3 scripts/runtime_paths_408.py --run exam_tutoring_context_408.py refresh-current \
  --repo . \
  --refresh-date YYYY-MM-DD
```

返回的 `receipt_sha256` 必须与最新 source version 和新增索引/个人表绑定；旧 manifest 缺新增产物时不能以 `already_current` 跳过升级。冷审计只在收尾/恢复执行，不在用户等待讲解时全量重算。刷新失败记录 `FORMAL_COMMITTED_TUTORING_CONTEXT_PENDING`，只重试刷新，不回滚已提交事实。

## 五、热路径查询

### 有界讲题读取

普通讲题、年度课件、campaign v2 和本地 D 使用原生 `resolve --personal-scope teaching --materials core`。`--max-bytes` 控制紧凑序列化总字节，范围 1024–8192，默认 8192。返回 `records[].personal_context` 和 `personal_read_receipt`。`bounded` 表示已核验所选档案的身份、版本和来源，只交付本轮适用的投影。`complete` 保留给完整读取。

同点当前判断保留原作者给出的内容、依据、证据状态、日期和更正身份。先照顾每个已返回点的当前判断，再保留提示后修复、`next_check` 和可用短摘要。超过预算时整段省略，禁止只留半句条件。被替代、撤回及有冲突的判断不参与当前投影。`historical_claim_count` 和 `conflicted_claim_count` 保留回读入口提示。缺项、未返回的点或小点及可选资料分别由 `missing_points`、`omitted_points`、`omitted_subpoints`、`omitted_fields`、`omitted_claim_count` 或 `omitted_materials` 表达。

跨点预算先给每个保留点的当前核证判断或可用完整短摘要留空间，大纲范围和适用边界随保留点返回。逐点释义、跳转和大纲对象详情按余量返回，省略时在该点 `omitted_fields` 标记。同题考频仍由精确题目开场优先交付。核证判断的 `event_at` 是实际发生时间，已知的 `recorded_at` 是该判断版本的记录时间。来源通过读取回执中的已验概况版本和返回的 `claim_id` 定位，原回合保留在完整证据。历史回放由 `memory history|trace` 及冻结版本完成。

`question_exam_introduction` 使用同题考频。`question_visual_template.binding_only` 只携带精确版本和来源状态，供已备 D 图解校验。需要图解时按同一权限用 `--personal-scope full --materials visual` 补读。正式编纂或确需查完整证据时在同题、同机制、同 view 使用 `--personal-scope full --materials core`。`--personal-scope summary` 仍为旧导航视图。便携查询保持原请求、分页和缓存接口。

`known-version` 绑定题/Pxx、机制、view、材料、范围和有效预算。来源漂移会使旧个人内容失效。未知点仍返回 `missing_points`，可以继续使用同次已经核验的其他点。`protected` 和独立 Pxx 的范围门禁先于完整个人对象读取。普通入口对独立小问核对逐问课件的题号、考点、纲目、模板和原图计划版本，再自动读取其已核 KSP。显式小点请求也须命中本问绑定。缺少同版逐问绑定用本问客观范围与当前作答。`question_scope.personal_subpoint_binding` 报告绑定是否可用。

有界 `core` 在个人依据之后优先保留精确课件指针。课件索引记录编制时的来源文件身份和摘要，首次查询只重新哈希发生变化的文件。打开课件或专题时沿原入口核对内容哈希、版本及权限。

本轮审查候选 `bounded-evidence-priority-v2` 将可核证的历史订正连同完整条件、角色、提示依赖及原记录版本作为一个单元。仅有事实的点在 `personal_context.repair_evidence` 返回完整单元或审核／预算状态。整组装不下时，`omitted_fields` 记 `repair_evidence`，同点 `evidence_object` 保留内容寻址出处。已审核个人判断的 `omitted_repair_claim_count` 标记因预算省略的订正，`unreviewed_repair_count` 标记缺审核的订正。适用条件仍由当前题设与原出处核对。生产启用状态以本轮验收回执为准。

命令：

```bash
python3 scripts/runtime_paths_408.py --run exam_tutoring_context_408.py query \
  --repo . \
  --input-json '{"schema":"exam_tutoring_context_query_v1","stage":"post_answer","formal_id":"OS_2010_003","source_id":null,"subject":"OS","knowledge_points":["OS03-17"],"weak_knowledge_points":["OS03-17"],"mechanism_terms":[],"ask_type_terms":[],"max_profiles":3,"max_bytes":8192}'
```

输入 schema 必须为 `exam_tutoring_context_query_v1`。`stage` 只允许 `pre_answer` 或 `post_answer`；`subject` 只允许 `DS`、`CO`、`OS`、`CN` 或 `null`。v1 只把 stage 和 subject 当作受控元数据，不据此猜题目身份。知识点名称只做构建期已封存的精确别名解析，不做模糊猜测；接口不接受完整题面或答案字段。

同时提供 `formal_id` 和 `source_id` 时，先核实二者唯一指向同一正式节点；冲突或来源映射不唯一返回 `status=unavailable`、`reasons=["exact_identity_ambiguous"]` 和空 `profiles`。任一精确身份无法解析时返回 `exact_identity_unresolved`，不能用知识点匹配掩盖该缺口。只提供知识点、机制或问法时，仍允许命中多条教学依据。

`knowledge_points` 收集当前全部考查点；`weak_knowledge_points` 只放当前证据或明确历史支持的薄弱点，并优先排序。精确身份自动补入该节点主、副、命中知识点。一次请求覆盖全部请求代码，显式 coverage 区分已返回、无记录、未解析或受预算限制的键；禁止用前三个节点 profile 静默截断知识覆盖。

默认输出上限为 8192 UTF-8 bytes，受控输入范围为 1024 到 8192 bytes，旧教学 fallback 最多读取和返回 3 个 profile，此限制不适用于知识点覆盖。`knowledge_summaries` 按点公平分配简明的最新与历史理解摘要及 refs，详细记录保留在冷存储。查询只读：

- 固定 manifest 与其内容寻址副本
- 每个实际查询键命中的少量 `knowledge-hot-routes` 哈希桶
- 命中知识代码对应的个人摘要对象（不扫描完整明细记录）
- 最多 3 个命中 profile
- 主表、映射、安全卡和 review ledger 的有界 metadata/anchor 漂移守卫

输入可选 `read_stats: true`。启用时结果增加 `read_stats`，只统计本次实际读取的 materialized JSON artifact 路径与文件字节数，包括固定/内容寻址 manifest、命中的路由桶、摘要和 profile；不以估算值或缓存命中数冒充实际读取量。默认不返回该字段，原调用和返回字段保持兼容。

字节预算包含最终 JSON 外壳、`serialized_bytes` 字段和结尾换行。删减 profiles 后仍超限时，返回精简 `unavailable` 响应，原因 `result_envelope_exceeds_budget`；不返回超预算的 `ready` 空壳。

查询不得实例化 `Repo.master()`，不得扫描 Obsidian、正式卡目录或完整 review ledger。输入不得携带完整题干、选项、答案、解析或完整作答字段。

成功输出固定声明：

```json
{
  "correctness_policy": {
    "scope": "current_question_only",
    "current_evidence_first": true,
    "history_role": "bounded_teaching_route_only",
    "historical_signals_must_not_grade_current_answer": true
  }
}
```

可用切片固定返回 `schema=exam_tutoring_context_slice_v1` 和 `status=ready`，同时绑定 `receipt_sha256`、`projection_build_sha256`，并声明 `current_correctness_source=current_question_only`。每个返回 profile 必须带非空 `teaching_actions`，并在顶层 `why_history_changes_tutoring` 中给出同一组可核验动作。允许的动作维度仅为：

- `diagnostic_question`
- `hint_start`
- `representation_choice`，只有 direct history 至少两次失败且当前断点再次成立时，才允许切到最小 worked step 后逐步 fade；v1 不从历史文本猜测当前断点或具体视觉表示
- `short_transfer_check`

这些动作限制适用于既有 profile fallback。知识学习摘要独立返回已有可信证据，不能以无 profile 动作漏掉知识记录；也不能把摘要当作当前错因诊断。

精确 `formal_id` 或 `source_id` 命中时，即使该节点还没有 mapped review event，只要正式安全卡同时存在非空、答案安全的 `用户错误入口` 与 `复做第一动作`，就可以返回一个窄动作：当前第一动作完成后，核对是否碰到该正式卡边界，必要时把正式 `复做第一动作` 作为最小提示。该动作必须绑定 card SHA-256 和字段名，并明确 `does_not_establish_current_recurrence=true`。仅知识点、机制或问法的宽泛命中不能使用这种 card-only 证据，仍必须有 direct event 才可返回 profile。

低置信正确或提示依赖的 direct event 可以改变下一次提示起点，但仍先收集当前无提示第一动作。任何 event claim 都保留 event ID、event SHA-256 和真实 `observed_date`。

当 direct history 的 `wrong`、`partial`、`uncertain` 合计至少两次时，可以同时返回 `if_current_break_recurs_jump_to_minimal_worked_step_then_fade` 和一个短迁移检查。该支持动作必须带 `condition=only_after_current_break_recurs` 与 `does_not_establish_current_recurrence=true`；历史本身不能让讲解直接越级。

### pre_answer 与 post_answer 投影

`knowledge_summaries` 同样必须按 stage 保持答案保护。pre-answer 只给答案安全的抽象理解状态及 refs，不泄漏具体先前错法、正确方法、中间数值或可反推当前答案的文字；post-answer 仍服从当前明确的答案保护。

`pre_answer` 的 fallback profile 固定使用 `exam_tutoring_profile_pre_answer_route_v1`，只能返回：

- formal/source identity；
- 抽象 teaching action、适用条件和不构成当前复发证明的声明；
- event ID、event SHA-256、observed date 或 card SHA-256 这类证据引用。

`pre_answer` 不得返回 `user_error_entry`、`redo_first_action`、`core_mechanism`、`fuzzy_concepts`、完整 history、`top_direct_first_breaks.text`、prior-break 文本，或字段中出现的任何具体中间值。正式卡动作只能暴露 card SHA-256，不能暴露字段值或字段名。顶层 `why_history_changes_tutoring` 必须复用同一份已脱敏抽象动作，不能保留完整动作的旁路副本。

`post_answer` 才可以返回答案保护允许的有界 `exam_tutoring_profile_v1`。即使在 post-answer，历史仍不能改变当前判分或自动认定旧错因复发。

## 六、漂移与可用性

- `节点总表.md` 漂移：`unavailable`，普通当前题讲解继续。
- `复习单元节点映射.md` 漂移：`unavailable`。
- 本次命中的任一安全卡漂移：`unavailable`；未命中卡片的漂移不扩大热路径读取。
- 冷 `refresh-current` / `refresh_after_commit` 在判断 `already_current` 及刷新读回时核对全部已索引卡片的 guard 与 SHA-256；无需预先写 invalidation，卡片单独修订也必须重建。该全卡检查不得进入 query 热路径。
- review ledger 与夜间快照完全一致：`snapshot_lag.status=current`。
- review ledger 仅在旧 EOF 后追加，且头尾 anchor 都匹配：仍必须 `unavailable`，要求提交 owner 刷新，不能继续沿用夜间滞后快照。
- review ledger 缩短、覆盖或无法证明仅追加：`unavailable`。
- manifest、索引、个人摘要、知识记录或 profile 缺失、越界、schema 不符或 SHA-256 不符：`unavailable`。

`unavailable` 与 `NO_MATCH` 都不得阻塞当前题讲解。前者表示物化层不可验证，后者表示没有可返回的知识证据或教学动作；它不能隐藏未覆盖的请求知识点。

## 七、冷审计

命令：

```bash
python3 scripts/runtime_paths_408.py --run exam_tutoring_context_408.py audit \
  --repo . \
  --receipt-sha256 <sha256>
```

`audit` 必须从当前正式主表、全部安全卡、复习单元映射和完整 review ledger 全量重算同一刷新日期，验证：

- 回执内容地址和 schema
- 源文件 SHA-256
- profile 集合、精确索引、知识索引、知识代码到记录哈希索引及个人摘要表 SHA-256
- 每个已发布 profile
- 当前 manifest-last 绑定

回执缺失、源内容漂移、ledger 新增后未刷新、对象缺失或 manifest 不再绑定该回执都必须失败。审计也是零模型、零正式写入。

## 八、实时教学时延

使用用户在当前会话选定的教学模型与运行档位。首段可用解释以 10–20 秒为优化目标。分别记录本机查询、首段可用解释、完整反馈和正式整理的耗时。普通讲题一次合并读取当前相关点，保留必要推理、答案保护和事务验证。
