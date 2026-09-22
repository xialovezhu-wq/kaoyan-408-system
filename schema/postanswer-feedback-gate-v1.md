---
schema: postanswer_feedback_gate_v1
status: normative
---

首答后反馈隔离门禁

`派生自查口径` 只在冷构建时进入独立的 content-addressed post-answer
对象。普通 pre-answer 查询索引不返回该对象引用或内容。

读取顺序固定为：

1. 验证 canonical review outcome 的 hot commit receipt 和 ledger 精确字节。
2. 核对 outcome 的 event、item 和 review-unit 身份。
3. 若来源为 morning review，继续验证 `first_bound_to_loop` session receipt，
   并核对其 canonical event ID 和 item ID。
4. 只有上述证明全部通过后，读取 post-answer index 和一个 feedback object。

四层投影输入分别限为：active pointer 16 KiB、content manifest 128 KiB、
exact index 8 MiB、单个 post-answer object 9 KiB。pointer、manifest、index 和
object 必须交叉绑定同一个 `build_id`、`object_set_sha256`、`projection_counts`，
并且每层都满足 `answer_safe=true`、`formal_write_count=0` 及各类计数一致。
不能只给最终返回值设置 12 KiB，而让输入对象无上限读取。

只要调用方传入 session 目录，canonical outcome 就必须来自
`morning_review`，并与该目录名、item、canonical event 和
`first_bound_to_loop` 回执完全一致；非晨间 outcome 不得借用 session 回执。

任何证明缺失、哈希漂移、身份冲突或预算超限都必须在读取反馈对象前
fail closed。热路径不得读取 `复习单元总表.md`，正式写入次数恒为 0。
