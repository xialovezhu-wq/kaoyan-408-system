# 408 节点记忆状态投影 v1

`review_memory_state_v1` 是每个正式节点的只读、可重建调度投影。它不是正式掌握度，不改写 `节点总表.md` 或 `review-event-v1` 账本。

## 历史证据优先级

重放 `question_valid=true` 的 legacy 原题 `outcome`，以及经过专用 hot writer 验证、直接绑定正式节点的 successor campaign outcome。已验收 V2/V3 变式可以更新对应正式节点的 FSRS 记忆状态，但不更新精确原题 exposure，也不改变该原题的 7 天冷却日期；其他 legacy 变式仍不进入记忆投影。repair 只证明同日纠偏，不作为跨日记忆 review。

同一节点同一 `observed_date` 最多选择一条权威 outcome，优先级依次为：

1. `date_correction` 验证后的实际日期优先于原日期。
2. 直接 `formal_node_id` 绑定。
3. `actual_question_formal_node_id` 绑定。
4. `mapped_formal_node_ids` 绑定。
5. 仅 `source_id` 等于正式节点 ID 的兼容回退。
6. 同绑定强度下，跨日晨间证据优先于晚间 D0，再优先于日常练习；最后按 `event_time/event_id` 确定。

若存在可信 review event，事件账本优先于正式表日期。没有可信事件时，依次使用 `最近复做日期`、`首次做题日期`；该回退只叫 `date_anchor_only_conservative`，不得伪装成用户答对。两者均未知时进入未知日期平滑。

## rating 映射

映射版本固定为 `review-event-to-fsrs-rating-v1`：

- 错误、不确定、提示依赖、推理 partial/diverged，或正确选项配错误推理 → Again 1。
- 未核验理由的正确快答，以及理由 sound 但低置信的结果 → Hard 2。
- 无提示、理由已经核验为 sound 且中高置信 → Good 3。
- 不从现有 review event 猜测 Easy 4。

## FSRS-6 兼容投影

模型标识为 `fsrs==6.3.2`。实现严格加载该固定分发版本，采用其 21 个默认参数和 D/S/R 更新，令 learning/relearning steps 为空、关闭随机 fuzz，并保持目标保持率 0.90。固定向量同时与 6.3.2 的公开结果逐项对照。

外部 `fsrs` 缺失、版本不是 6.3.2、配置模型标识/参数数量/fuzz 设置不符时全部 fail closed，不回退到近似算法。

## 字段

每个状态至少包含：

- 正式节点 ID、模型和 rating 映射版本。
- `history_source`、`calibration_status` 和证据引用。
- 最近 review 日期、difficulty、stability、as-of retrievability、下一到期日。
- reps、lapses、事件历史数。
- campaign 的 `latest_safe_day`。
- `state_sha256`。

未知日期且未评分的节点令 D/S/R 为 null，只保存平滑得到的首次到期日；首次真实作答后才初始化 FSRS 状态。
