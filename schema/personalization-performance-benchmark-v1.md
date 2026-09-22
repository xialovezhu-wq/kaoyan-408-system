# 408 个性化 Harness 性能验收 v1（只读历史协议）

状态

本验收已退役。旧脚本保留原实现以便审计历史设计，但 `run_benchmark` 和 CLI 在读取 personalization、历史或正式资料前即返回退役错误。旧 20 回合配额、个性化切片、跨题推荐、变式题和 promotion 结果不再构成发布门禁。

现行替代

`scripts/benchmark_current_question_hotpath_v1.py` 只在一次性副本中验证：

- prepared option 本地业务事务 P95 不超过 500 ms；
- 普通 quick capture P95 不超过 750 ms；
- rich current turn P95 不超过 1200 ms；
- 所有非终态结果 Capture=0；答案与 sound 推理 terminal 后 final package 绑定的
  Capture 恰好一次；
- capture 故障隔离、同键恢复、当前位置不推进；
- 禁止目录和外部资料读取计数为 0；
- 正式写入数为 0。

历史边界

既有 benchmark 报告可保留为只读证据，不追溯改写，也不得用于声称新版聊天热路径仍读取 personalization。
