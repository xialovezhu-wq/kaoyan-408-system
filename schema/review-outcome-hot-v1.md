---
schema: review_outcome_hot_v1
status: normative
---

review outcome 热事务

目标

在晨间首答、普通单题和真题逐题热路径中生成与 canonical review outcome
等价的事件，但不扫描完整 review ledger、`state.json`、正式复习总表或原题轨总表。

输入

- 一次首答的答案安全结构化事实。
- `review_hot_state_v1` 的精确 begin view 和 precondition。
- 至多一个 `review_hot_projection_v1` 复习单元对象。

提交顺序

1. 规范化并校验首答事实。
2. 精确读取幂等 event ref、同晨间 item alias、到期 action、跨日证据、baseline 与
   retention count；begin view 不携带完整 event。
3. 从一个 hash-bound 复习单元对象取得映射节点和原题轨最小字段。
4. 构造 canonical `review_event_v1`。
5. 在 review runtime lock 内 append、fsync，并事务更新热状态。
6. 用内容寻址回执和 ledger 精确字节区间完成有界校验。

幂等重放

- `existing_event_ref` 为空时不读取历史 event 载荷。
- `existing_event_ref` 命中时，固定调用一次 `lookup_event(event_id=...)`，按 ref 中的
  event hash、line hash、offset 与 length 核对后取得完整 event，再校验 request hash。
- receipt 只能来自 `lookup_commit_receipt` 的原 hot commit receipt，并继续执行
  `verify_commit`；cold bootstrap event 没有 hot receipt 时 fail closed，不补造。
- `same_morning_item_ref` 若与 existing event 相同，只是 alias；若是不同幂等键下的同一
  morning item，则直接拒绝第二个 outcome，不读取它的完整载荷。

边界

- 正式写入次数恒为 0。
- 不更新 `review-loop/state.json`、dashboard 或 Obsidian 正式表；它们由冷重建刷新。
- 缺少热状态、热正式投影、精确身份或 hash 绑定时 fail closed。
- morning outcome 只接受 session 同步入口，不做全 session 目录扫描。
- 最大 65,536-byte event 的幂等重放不得扩大默认 begin view；完整 event 只在 exact-range
  lookup 的返回值和最终 outcome 返回值中出现一次。
- 变式作答不得给锚点原题生成原题轨 action。
- 原题轨资格枚举、最近日期与最早允许日期必须组成有效 7 天元组；不一致时提交前失败。
- 当前真实作答的原题把自己的下一次原题门禁重置为本地作答日加 7 天，不能在当天重新到期。
- 其他原题轨的最近真实作答日期只来自冷编译的正式原题轨投影；仍在保护期时只能排到最早允许日。
