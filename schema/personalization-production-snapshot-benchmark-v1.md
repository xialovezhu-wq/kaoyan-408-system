# 408 个性化 Harness 真实生产快照性能验收 v1

## 目的

`scripts/benchmark_personalization_production_snapshot_408.py` 补充合成 120 天基准的证据边界。
它读取当前生产 personalization manifest、正式节点表日期和 canonical review event 日期，
但所有计时调用都在一次性临时仓库中的生产状态副本上运行。

这份报告证明的是“真实生产投影字节在隔离临时副本中的 Harness 性能”，不是在线生产延迟，
也不证明已经存在 100 个活跃学习日。

## 冷阶段真实历史证据

冷阶段允许完整读取以下三个生产事实表面，并在计时前完成：

- `节点总表.md`；
- `wiki/study_vaults/408-full/state/review-loop/events.jsonl`；
- `wiki/study_vaults/408-full/state/personalization/manifest.json` 及其绑定的
  `question_index.json`、`learner_state.json`。

正式节点日期必须同时报告：

- 表格行中所有合法 ISO 日期 token 的最早日期、最晚日期、elapsed calendar span、inclusive
  calendar span 和 distinct count；
- `首次做题日期`、`最近复做日期`、`最近错误记录` 三个声明字段中的 occurrence 和 distinct
  count；
- 最早、最晚跨度端点是否也真实存在于上述声明字段。

canonical active event 日期只统计 `event_kind=outcome` 或 `event_kind=repair` 且存在合法
`observed_date` 的事件。不得用 `event_time`、文件 mtime、candidate workflow metadata 或其他
推断日期补齐。缺少 `observed_date` 的活跃事件必须单独计数。

100 天门禁只检查真实正式节点记录的最早、最晚日期之间 elapsed calendar span 是否至少为
100 天。distinct table dates、declared-field dates 和 distinct active event dates 必须分开报告；
其中任一不足 100 都不得表述成 100 个活跃学习日。

## 快照复制与哈希绑定

生产 personalization state 复制到 `TemporaryDirectory` 下的独立临时 repo。复制时必须排除：

- `turn-receipts/**`；
- 所有 `*.lock`。

计时前必须证明：

- 临时副本 eligible tree hash 与生产 eligible tree hash 完全相同；
- 临时 manifest SHA-256 与生产 manifest SHA-256 完全相同；
- 没有复制既有 turn receipt 或锁。

树哈希使用按相对路径排序的
`relative_path NUL file_sha256 NUL byte_size` 材料。报告必须同时绑定：

- 生产 personalization tree，包含既有 receipts、排除 locks；
- 生产 eligible tree，排除 receipts 和 locks；
- 临时 eligible tree 在计时前、后的哈希；
- 正式节点表、canonical review ledger、manifest 的前后逐文件 SHA-256。

Harness 只允许在临时副本生成新的 begin/finalize receipts。生产前后所有上述哈希必须不变，
临时目录离开上下文后必须被删除。

## 计时表面

### safe pre-answer

至少测量 20 次 `safe_preanswer_slice`，默认预算为 12,288 UTF-8 bytes，P95 不高于
250 ms。每次必须满足：

- `status=ready`；
- `read_only=true`；
- `formal_write_count=0`；
- `scheduling_authority=none`；
- 序列化结果不超过预算。

### managed begin/finalize

至少测量 10 次，并覆盖 `ENTRY_TO_CONVERSATION` 的全部十类 entry。每次必须：

- begin 成功且 `preanswer_call_count=1`；
- pre-answer 为 ready；
- finalize 成功并授权展示；
- `formal_write_count=0`；
- slice 不超过 12,288 bytes；
- begin 与 finalize 返回合计不超过 16,384 bytes。

P95 不高于 5 秒。运行后必须对临时 turn receipts 执行完整 receipt audit；这些 receipt 不得写入
生产目录。

### 完整单题学习事务

主报告必须调用
`scripts/benchmark_single_question_hotpath_production_408.py` 的伴随验收，并将完整报告绑定到
`single_question_learning_toolchain.acceptance`。伴随验收至少运行 20 次以下真实字节临时副本
事务：

1. canonical outcome 持久化与精确 commit receipt 校验；
2. personalization overlay 即时更新；
3. 已验证变式题选择与深度来源复核；
4. 非正确首答的条件快速 capture。

该链路 P95 必须严格小于 5 秒，热路径全账本扫描和正式表读取必须为 0，正式写入必须为 0，
生产账本、状态、正式表和绑定来源前后哈希必须不变。具体边界见
`schema/single-question-production-hotpath-benchmark-v1.md`。

## 热读 fail-closed 门禁

所有 warmup 和计时调用都在读守卫中运行。以下任何读取尝试立即失败：

- 任意 `.jsonl` ledger；
- 任意 `.md` 正式表或 Markdown；
- 临时 repo 以外的任何非锁文件。

守卫同时覆盖 `Path.open`、`builtins.open`、`io.open` 和 `os.open` 的只读或读写模式；
测试必须对后三种非 `Path` 入口执行逃逸读取反例。该门禁证明当前 Python 热路径没有经这些
受管入口逃逸，不把它表述成操作系统级沙箱或对任意原生扩展的绝对隔离证明。

报告分别输出 `full_ledger_hot_scan_count`、`formal_table_hot_scan_count` 和
`outside_temp_hot_read_count`，三者必须为 0。冷阶段对生产事实的显式读取不属于计时区间，
必须在 `cold_source_reads` 中列明。

## 不宣称范围

报告固定保留下列边界：

- `live_production_latency_claimed=false`；
- `active_learning_days_100_plus_claimed=false`，除非未来真实 active event distinct dates 本身达到
  100；当前脚本仍不自动把 calendar span 等同于 active days；
- `single_question_learning_toolchain_claimed` 只表示生产规模真实字节的临时副本验收通过，不表示
  在线生产文件系统争用下的延迟已经实测。

单题伴随验收必须明确区分真实 formal date 的 100 天 elapsed calendar span 和 distinct active
event dates，不得把前者改写成 100 个真实活跃学习日。

## 运行

```text
python3 scripts/benchmark_personalization_production_snapshot_408.py --repo .
```

标准输出为 `personalization_production_snapshot_benchmark_v1` JSON。通过返回 0；任一门禁失败
或构建错误返回 2。`--output` 仅在用户显式指定的位置原子写同一份报告，默认不产生仓库文件；
如果目标位于整个生产仓库或 details vault 内，必须在运行前拒绝且不创建文件；这避免遗漏未来
新增的正式表、索引或节点卡并被报告覆盖。
