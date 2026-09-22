# 受管 408 当前题直达路由 v1

契约标识：`managed_408_direct_router_v1`。

## 范围

状态：只读兼容。该 direct router 不再是普通 standalone 教学或新 Capture 的
生产入口。

以下输入、输出和命令章节只说明历史调用形状。当前脚本只做只读分类并固定
`business_call_count=0`；它不得绑定 context、调用 current-question 事务或执行
任何 package/Capture/navigation 业务。显式快速入库由模型交给 standalone
validator，而不是本 router。

`scripts/managed_408_direct_router.py` 只把单个选项及可选置信度绑定到已经形成的
`current-question-context-v1` 和冻结反馈。它不读取个性化、历史、正式投影、关联题、
变式题或 Luna，也不检查 promotion、trial、thread 或旧 rich intake。

普通 standalone 教学不调用任何持久化入口，package、Capture、pending index 和
formal write 均为 0。当前用户明确要求“快速入库”时，由模型路由到
`kaoyan-408-wrong-intake`，再按 `standalone-question-intake-v1` 提交显式授权的
final-package → one-Capture 事务；不得把该请求交给本 router。

受管晨间题是否 Capture 不再由首答证据矩阵自动决定。只有根 Sol 语义判定和私有
choice grader 共同形成 `terminal_resolved=true` 后，managed terminal finalization
才允许封存最终包并创建该 item 的唯一 Capture。

## 输入边界

- `--turn-json` 只能包含公开 `context` 和 `feedback_text`。
- 私有 grader 必须提前通过受限私有 store 发布；生产路由只接收
  `--grader-capsule-locator` 与 `--grader-capsule-sha256`。
- 库内单元测试可以受控注入 private evaluator；生产 CLI 没有该参数。
- 选项、置信度、request ID、context、capsule 的 source/session/item/context/hash
  必须精确一致。

## 输出边界

- 成功只调用一次 current-question v1 事务。
- 同一事务固定 `next_item_published=false`，不查下一题。
- `continue-current` 与 `next-item` 是两个显式、互不替代的动作。
- 旧参数若通过库 API 传入，明确返回 `legacy_control_disabled`。

## 命令

```bash
python3 scripts/managed_408_direct_router.py --repo . \
  --request-id REQUEST --message-text B \
  --turn-json '{"context": {...}, "feedback_text": "..."}' \
  --grader-capsule-locator current-question-evaluation://sha256/... \
  --grader-capsule-sha256 SHA256
```
