# 408 回答前个性化切片 v1

本协议为普通单题、真题复盘、晨间复盘、晚间复盘、章节预习和普通 408
知识问答提供同一个回答前只读入口。它只查询已经构建好的个性化投影，不负责
记录本轮结果、正式入库、生成题面或注册后续题。

## 一、受管调用入口

```python
from personalization_turn_receipt_408 import begin_personalized_turn

turn = begin_personalized_turn(
    repo,
    entry_kind="current_question",
    target_envelope=answer_safe_target,
    max_bytes=12288,
    mode="verified",
)
```

每一种受管 408 回答在组织回复前调用一次 `begin_personalized_turn`。它是
`safe_preanswer_slice` 的唯一正常调用方，并为该请求留下 begin receipt；回复显示
前还必须写 reply-hash finalize binding。裸调用只供底层测试和故障诊断。

`slice.status=ready` 时只使用返回的有界字段个性化回复；不可用时按原流程继续，
留下 degraded receipt，且不得声称使用了个人历史。

## 二、答案安全目标信封

目标信封固定为 `personalization_target_envelope_v1`，只允许包含：

- `request_id`：调用方生成的稳定请求 ID，可省略；
- `conversation_kind`：`current_question`、`past_exam_review`、
  `morning_review`、`evening_review`、`chapter_preview`、`wrong_intake`、
  `knowledge_explanation` 或 `general_408`；
- `as_of`：`Asia/Shanghai` 对应的明确本地日期；
- `subject`：DS、CO、OS、CN，可省略；
- `point_ids`、`mechanism_ids`、`formal_node_ids`、`module_ids`：已有稳定 ID；
- `safe_topic_terms`：最多 8 个只用于精确命中考点目录的短词；
- `error_pattern_ids`、`method_ids`、`mechanism_primitive_ids`：仅限已经可靠识别的
  受控语义 ID；
- `answer_protection=true`。

调用方必须先从当前会话已有的题目元数据、正式节点引用或安全主题识别结果构造
信封。不得传入题干、选项、答案、解析、完整作答、反馈、`first_break`、图片或
附件。入口也不会在输出中回显 `safe_topic_terms`。

## 三、只读边界

入口只允许读取当日 manifest 绑定的：

```text
manifest.json
catalog.json
learner_state.json
failure_signatures.json
question_index.json
cross-subject-shadow.json
corpus/manifest.json                    可选
corpus/indexes/<sha256>.json            可选且由 corpus manifest 的 index_ref 绑定
corpus/objects/<sha256>.json            仅按当前目标命中读取
```

它不读取 review 或 capture ledger、`event_mappings.json`、正式节点表、正式关系
表、完整 Obsidian vault、参考书和题目语料。它不重建投影、不追加事件、不创建
选择回执，固定返回 `read_only=true`、`formal_write_count=0` 和
`scheduling_authority=none`。

夜间 corpus 只有在根 `manifest.json` 同时绑定 corpus manifest 与 index 的精确
SHA-256 时才可读；单独重建 corpus 不会悄悄进入热回复。

## 四、返回切片

默认预算 12,288 UTF-8 bytes，允许范围 1,024 到 65,536 bytes。返回最多包含：

- 六个当前考点视图和少量稳定目标 ID；
- 三条稀疏学习状态及有界的近期失败签名；
- 至多三条与当前正式 ID、考点或受控机制直接命中的正式历史签名；
- 三个安全 evidence event ID；
- 两条跨科上下文；
- 三个满足可靠题源门禁的候选题定位；
- 一组九字段 `personalization_response_plan_v1`。

候选题只表示只读检索结果，不构成调度授权。正式调题仍必须通过独立的 7 天、
题源、关系启用和选择回执门禁。

## 五、回复策略与证据边界

九字段回复计划固定为 `current_target`、`direct_recent_errors`、
`direct_historical_errors`、`common_patterns`、`verified_cross_connections`、
`explanation_strategy`、`first_boundary_to_check`、`optional_practice_action` 和
`evidence_gaps`。

切片用受控枚举告诉调用方采用哪种最小策略：

- 近 7 天重复失败：先给最小框架，再检查第一动作；
- 一次近期失败：做针对性解释和边界检查；
- 脆弱正确多于稳定保持：压缩重讲，优先迁移检查；
- 已有稳定保持：避免重复长讲；
- 暂无可靠状态：使用最小充分框架。

只有至少两个不同 canonical failure outcome event 才能形成 repeated common
pattern；repair 和正确结果不计数。`module_fallback`、`subject_fallback` 和
`weak_semantic_candidate` 只作背景，不能驱动个人结论、第一边界或练习动作。

跨科影子边可用于回答中的轻量联系，但只有 `verified` 模式下同时满足已有验证、
激活资格和显式 live allowlist 的边，才会标记 `live_routable=true`。即使如此，
本入口仍不能直接排题。

日终语料是可选投影。入口先验证 `corpus/manifest.json` 的 `index_ref` 和对应哈希，
再通过 `by_formal_id`、`by_point_id`、`by_mechanism_id`、`by_error_pattern` 或
`by_method_id` 只读取命中的 content-addressed object，且逐个复核 object ref、
SHA-256 及 index/object 身份。语义倒排只接受受控键；读出的 object 还要重新形成
失败签名并实际通过相同机制、错因或方法匹配，索引命中本身不直接生成个性化结论。
缺失或校验失败时只把 corpus 状态设为 `not_available` 或 `unavailable`，不会回退
扫描 capture ledger、全部 corpus object 或正式库，也不会阻断其余个性化切片。

manifest 与每个 `nightly_curation_corpus_item` 都必须声明 `answer_safe=true` 和
`formal_write_count=0`；缺少任一声明时，该 corpus 不进入回复上下文。

## 六、冷路径要求

回答前入口不会扫描历史或补建索引。每天第一次 408 学习前，或日终正式编纂、
关系审核、题源变化后，应在非回答热路径重建：

```bash
python3 scripts/personalization_index_408.py build --repo . --as-of CURRENT_LOCAL_DATE
```

如果日期过期、哈希漂移、信封含受保护内容或无法定位目标，入口只返回受控
`reason_code`，调用方按无个性化切片继续。
