# 408 离线变式银行 v1

本协议定义 `exam_variant_bank_v1` 冷路径。它只为既有正式节点准备可验证的练习 surface，不创建正式节点，不修改正式库，不调用模型或网络。完整题干、选项、答案、解析与 verifier 工作材料只能写入调用者显式指定的外部私有 details root。

## 命令面

```text
python3 scripts/exam_variant_bank_408.py prebuild-plan ...
python3 scripts/exam_variant_bank_408.py submit-author ...
python3 scripts/exam_variant_bank_408.py submit-blind-verifier ...
python3 scripts/exam_variant_bank_408.py submit-adversarial-verifier ...
python3 scripts/exam_variant_bank_408.py publish ...
python3 scripts/exam_variant_bank_408.py commit-consumption ...
python3 scripts/exam_variant_bank_408.py pool-status ...
python3 scripts/exam_variant_bank_408.py forecast-fill ...
python3 scripts/exam_variant_bank_408.py select-surface ...
python3 scripts/exam_variant_bank_408.py audit ...
```

Python 调用面另提供 `import_web_self_checked_variants(...)`。其完整输入合同发布为 `variant-bank/web-self-check-contract.md`，源文件是 `schema/exam-variant-web-self-check-v1.md`。该入口无需预先生成本地 prebuild request；它从当前 native due/node 与固定 ESR 派生 request/target binding，再复用 author 的 V2/V3、style、anchor、source、option 与答案隔离确定性校验。

所有对象按持久化字节的 SHA-256 内容寻址。任何 ref 越出 details root、使用符号链接、文件名与字节哈希不一致、ESR release 或来源漂移时失败关闭。CLI 输出不返回答案正文。每一步固定 `model_call_count=0`、`formal_write_count=0`。

## Prebuild plan

输入是 `exam_variant_target_set_v1`。每个 target 必须绑定：

- 一个正式节点 ID 与该节点快照 SHA-256；
- 一个已审计 `exam_style_release_v1` 的 style profile；
- profile 内一至多个 `production_usable=true` canonical anchors；
- 3–5 个 candidate slots，slot 只允许 V2 或 V3，V1 永久禁止；
- 完整 deep style spec。

deep style spec 固定包含：`condition_density`、`data_representation`、`legal_parameter_ranges`、`calculation_scale`、`polarity`、`option_parallelism`、`reasoning_depth`、`distractor_mechanisms`、`v2_invariant_axes`、`v2_mutable_axes`、`v3_invariant_axes`、`v3_mutable_axes`。invariant 与 mutable axes 不得重叠。

`polarity` 只允许 `positive`、`negative`、`mixed` 或 ESR 原生的 `neutral_or_implicit`。不得为了通过校验把综合题的中性/隐式设问静默改写成 positive。

当目标知识点没有可用的局部单选画像时，target 可显式携带 `style_fallback`，但只允许 `scope=same_module_same_cognitive_action`：画像知识点必须与目标知识点处于同一模块，认知动作必须相同，画像 `question_form` 必须是 single-choice，且 `confidence=low`。精确知识点已有画像时禁止伪装成 fallback。内容知识仍绑定 formal node；fallback 只约束单选呈现形式。

target deep style spec 不能替代 ESR profile 的 axis contract。`v2_invariant_axes` 必须至少包含 profile `v2_axes.invariant_axes` 的完整集合；可以追加更严格的 node-level invariant。`v2_mutable_axes` 必须是 profile `v2_axes.mutable_surface_axes` 的非空子集；`v3_mutable_axes` 必须是 profile `v3_allowed_single_structural_edges[].mutable_axis` 的非空子集。任意新增 protocol、state、direction 或其他 profile 未发布的 mutable axis 必须在 plan 阶段失败关闭。request 必须冻结 profile axis contract 及其 SHA-256。

V2 是同结构变式：至少改变两个 surface axes，其中至少一个是非数值轴，`changed_structural_edges=[]`。V3 是近迁移变式：`changed_structural_edges` 恰好一个。作者声明的每个变化轴必须来自相应 mutable axes，不能触碰 invariant axes。

V3 的唯一 `changed_structural_edge` 必须同时声明 `axis` 与 `relation`。`axis` 必须属于 target 收窄后的 `v3_mutable_axes`；`axis + relation` 必须精确匹配 ESR profile 中同一行的 `mutable_axis + edge`。不得把自定义协议方向、任意状态边或自由文本关系伪装成 V3 structural edge。

`forecast-fill` 只读取 `exam_variant_forecast_due_set_v1` 中从 `as_of` 起七个自然日的 due 项及当前 pool 投影。verified 数量不足的节点才生成 3–5 slot gap request。

## 作者提交与答案隔离

作者提交必须有四个稳定 `option_id`、四段互异选项文本、恰好一个 `correct_option_id`，以及三个干扰项各自的 `mechanism`、`misconception` 与 `refutation`。每一事实必须有 `source_commitment`；canonical source 绑定 anchor 与 ESR content SHA-256，其他权威来源绑定自身 SHA-256。

题面和答案分成两个对象：

- `exam_variant_question_surface_v1` 只含题干与四个 stable option，不含 key、解析或反证；
- `exam_variant_answer_capsule_v1` 保存 key、干扰项反证、解析、来源承诺和 verifier chain。

盲审首次只收到 `exam_variant_blind_input_v1`，其中 `answer_fields_included=false`，且不含 `correct_option_id`、干扰项反证或解析。

## 盲审与对抗审

盲审必须独立于作者，并提交：

- 开放式解答和自行导出的 stable option ID；
- A–D 对应的四个 stable option 逐项判断与反证，恰好一个正确；
- 第二正确项搜索，必须明确 `found=false` 并给出分析；
- 四个互不相同的选项换序试验，每次结论仍指向同一 stable option ID；
- 单位、位宽、边界、状态转移、协议方向五项检查，各自为 `pass` 或有理由的 `not_applicable`；
- 考纲外依赖与作答线索扫描，均须明确未发现并给出分析。

系统只在盲审提交封存后，私下比对其导出 ID 与 author answer capsule。对抗 verifier 必须再次独立于 author 和 blind verifier，确认唯一 key、干扰项机制与反证、24 个确定性换序的 commitment、事实来源、答案隔离和 V2/V3 结构门禁。任一 identity 重合、检查缺失或 verdict 非 accept 均不得 publish。

## Pool 生命周期

发布 manifest 必须显式区分 acceptance：

- `verified_ready`：原 author + independent blind verifier + independent adversarial verifier 三个不同 actor 的既有发布链；
- `web_self_checked_ready`：同一网页 actor 出题、重新求解并逐选项反例/证明自检，本地只做确定性校验。其 blind/adversarial refs 与 commitment 必须为 null，`independent_reviewed=false`，不得写成独立三审。

两者都可进入 pool 的 `verified` 投影并接受相同的 identity、ESR、V2/V3、7 天、消费和答案隔离门禁。selection 与 display 必须保留 `acceptance_kind`；网页路径的学习标签固定以“网页自检变式”开头。

`pool-status` 只投影以下五态：

- `draft`：已有 author 对象，尚未形成完整发布链；
- `verified`：author、blind verifier、adversarial verifier、surface、answer capsule 与当前 ESR 全部通过；
- `quarantine`：对象、哈希、答案隔离或 verifier chain 失配；
- `consumed`：已绑定一个不可变 campaign commitment consumption receipt；
- `expired`：发布时绑定的 ESR release 已不再是当前受审 release。

生成内容始终 `formal_node_created=false`。consumed 不回到 verified；同一 variant 不得绑定两个不同 commitment。

## Surface resolver

7 天门禁只比较精确 `true_item_id`，不得用 formal node、知识点或相邻 canonical item 扩大排除：

- 对同一个 canonical original，最近真实展示 `gap_days < 7` 时禁原题，只能选择当前 pool 的 verified V2/V3；
- 其他 exact item 的曝光不影响它；
- `gap_days >= 7` 或没有曝光时允许原题，但仍要求 ESR 中 `direct_pool`、`direct_replay_allowed=true`；
- 原题被禁且没有 verified variant 时返回 `VERIFIED_VARIANT_UNAVAILABLE`，绝不降级回原题；
- 原题 answer capsule 缺失时先尝试 verified variant；仅当也没有 verified variant 时返回 `ORIGINAL_ANSWER_CAPSULE_UNAVAILABLE`；
- variant selection 必须携带 campaign commitment ID。`select-surface` 默认且只能执行 `consume=false` preview：返回 hash-bound `selection_id`、`consumption_request` 与 `consumption_request_sha256`，不得写 consumption receipt，也不得改变 pool。learner surface 只返回题干、换序后的 A–D 与作答说明；answer capsule 仅以不透明哈希绑定。
- 对没有可直接重放单选原题的正式节点，控制面可显式使用 `variant_only=true`，并提供当前 `formal_node_main_knowledge`。此时 canonical anchor 只承担已验证真题风格锚点，不得因该 anchor 已被另一节点使用而阻塞 variant。若 author 时的整行 formal-node SHA 与当前整行 SHA 不同，只有稳定正式 ID 与主知识点代码同时一致才允许生成 `formal_node_rebound=true` 的选择；选择必须同时保存旧 SHA、当前 SHA 和 `stable_formal_node_id_and_main_knowledge` 依据。主知识点变化仍失败关闭。

canonical original selection 必须重开并逐字节验证 `question_asset_refs` 与 private sibling answer capsule commitment，返回聚合 `surface_sha256`、资产 refs/hashes 和 opaque answer-capsule ref/hash；不得公开答案字母或解析正文。生产路径只接受最终 `ESR-965a1372f11756c6d55f170b`，拒绝中间 release。该 release 的 direct answer capsule 为 613 个 resolved/usable、67 个 blocked；旧 release 绑定的 pool entry 必须投影为 `expired`。

## 两阶段消费事务

控制面必须先把当日全部 preview 冻结为内容寻址的 `exam_variant_selection_manifest_v1`。manifest 绑定 `campaign_commitment_id`、完整 campaign commitment SHA-256、selection 总数和全量 selection。

`commit-consumption` 只有在下列门禁全部通过后才写一个 `exam_variant_consumption_batch_receipt_v1`：

- manifest 文件名与完整字节 SHA-256 一致；
- 每个 selection 都是 `status=selected`，不存在 blocked 或 unavailable 项；
- 每个 variant 的 consumption request 与 selection ID、当前 ESR、surface、answer capsule 和 verified pool entry 完全一致；
- selection 数量、variant identity 和 campaign commitment 均无重复或漂移。

批次 receipt 是单一内容寻址对象；pool 只在该批次 receipt 存在后把对应 variant 投影为 `consumed`。任何一题 blocked 时整批拒绝，之前 preview 的 variant 仍为 `verified`。同一 selection manifest 重复提交只返回同一个 receipt 和 `already_committed`，不得生成第二次消费。

同一 campaign 尚未 start 时，若新的 exposure cutoff 使 preparation 必须重建，已消费 variant 只有在 campaign commitment ID 与逐字节 consumption request 均相同时才可复用。全为既有 reservation 时写 `exam_variant_consumption_batch_rebind_receipt_v1`；既有与新 variant 混合时，新批次必须再次完整绑定当前 selection manifest，并允许旧 variant 以相同 campaign/request 作为重复 reservation。跨 campaign 或 request 漂移仍为冲突。该机制只修复未启动 preparation，不产生 learner exposure。

## Audit 与完整 commitment

`audit` 按 acceptance kind 重开发布对象。`verified_ready` 继续复核 request、author、盲审、对抗审及三方 identity；`web_self_checked_ready` 复核 web submission、grading、solution、same-actor self-check、null independent chain、surface 和 answer capsule。两条路径都逐字节复核哈希、ESR、正式节点/style/anchor 绑定、V2/V3 gate 和答案隔离。

campaign 在发布第一题前应对全量 30、40 或 100 题 commitment 逐项验证：每项都必须有唯一 learner surface SHA-256、opaque answer capsule SHA-256、当前 ESR SHA-256、exact true item ID 和不可变 commitment ID。数量完整性是全有或全无门禁，不能用部分 verified pool 或运行时现场生成补齐。
