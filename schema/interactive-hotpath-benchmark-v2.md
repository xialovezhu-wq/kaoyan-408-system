# 408 Interactive Hotpath Benchmark v2

Schema identifier: `interactive_hotpath_benchmark_v2`.

## 一、目的

v1 基准主要测进程内函数，并使用 5 秒级宽门槛。v2 单独测量真正影响交互等待的三层：

1. ready pre-answer 进程内壁钟；
2. v2 begin 进程内壁钟与默认返回字节；
3. v2 begin CLI 子进程壁钟与 stdout 字节。

所有样本运行于一次性临时仓库，不写生产 state 或正式表。

## 二、ready 与 stale 必须分开

ready 样本的 envelope `as_of` 必须等于绑定 manifest 的 `as_of`，每次都要求：

```text
retrieval_status=ready
preanswer_call_count=1
receipt_schema=personalization_turn_begin_receipt_v2
formal_write_count=0
```

stale 样本必须使用不同日期，并要求受控降级：

```text
retrieval_status=degraded
slice.status=unavailable
slice.reason_code=stale_projection
```

stale 的快速返回不得进入 ready P50/P95，也不得作为 ready 性能通过证据。

## 三、计时与字节门禁

最近秩 P95 门禁：

```text
ready pre-answer in-process <= 30 ms
ready v2 begin in-process <= 30 ms
ready v2 begin CLI <= 150 ms
default begin stdout/result <= 2,048 UTF-8 bytes
```

三个 ready 计时组各至少 20 个 measured 样本；stale 进程内和 CLI 控制各至少 1 个，且不进入
ready 分位数。CLI 可另有不计入结果的 warmup。报告保留每组 P50、P95、max、mean、迭代数
和最大字节数。CLI 计时从启动 Python 子进程到进程退出，包含 import、
JSON 解析、receipt/slice 持久化和 stdout 编码。

进程内 ready pre-answer 使用独立、显式且不少于 5 次的 steady-state warmup。fixture 构建、
首次 import 和首次页面缓存缺页不进入该组；warmup 数量必须在报告中披露，不能静默丢弃
measured 样本。

`--full` 与 `inspect-receipt` 不受 2,048-byte 默认输出预算约束，但必须验证它们不重跑
pre-answer 或 audit。

## 四、完整性门禁

每个 measured begin 必须使用唯一 request ID，随后在计时区间外 finalize，以便最终 receipt
audit 达到 complete。验收还要求：

- ready 与 stale 均只有一次 pre-answer；
- v2 begin 不含嵌入 `slice_snapshot` 或完整 `response_plan`；
- detached slice 文件名、字节 SHA 和 begin 引用一致；
- CLI 默认 stdout 是单行 compact JSON；
- ready/stale 结果均为零正式写入；
- 临时目录退出后删除；
- 生产 manifest 如果 stale，只能披露为 production observation，不能替代临时 ready 样本。

## 五、报告边界

本基准不含模型生成时间、Codex 工具 RPC 网络往返、晨间 capture writer 或正式入库。
CLI P95 代表本机 Python 进程边界；产品级端到端延迟需要另行记录工具调用开始至回复可展示时间。
