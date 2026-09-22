# formal-node-deletion-v1

正式节点删除是独立的高风险维护事务，不复用普通既有节点维护入口。

## 授权与批次

- 每次只允许一个合法正式节点 ID，固定 `batch size=1`。
- `authorization` 必须为 `user_explicit_delete`，并保存明确授权日期、删除原因和幂等键。
- 两个节点必须形成两个串行 WAL 事务和两个 receipt。

## 删除边界

- 删除正式节点的活动主行、分科镜像、知识索引、incident 关系、概念/专题投影、复习单元映射、原题轨、安全卡、详情卡和节点专属资产。
- 共享复习单元只移除目标贡献并重建幸存节点口径；专属复习单元随节点删除。
- 规范真题卡和规范真题资产保留，只移除被删正式 ID 的双向绑定。
- 历史来源没有活动正式节点承接时，在删除审计账本写入 `historical_source_tombstone`；不得把它悬空，也不得伪装成另一节点的主来源。
- `错题复做记录.md`、历史复习单元归档、lint/导入/复盘报告、旧 WAL、旧 receipt 和删除审计账本保持不可变历史。

## 事务与恢复

- WAL delete item 使用显式 `operation=delete`、`after_sha256=null`、`stage=null`，准备阶段保存 before bytes 和 SHA-256。
- 发布前执行 compare-and-swap；失败时从 backup 逐字节回滚。
- 已提交但 closeout 失败时只能凭原 receipt 重跑 closeout，不能再次 apply。
- 资产目录只在全部已冻结普通文件提交删除后执行精确空目录 `rmdir`；出现额外文件或子目录时 fail closed。

## Closeout

删除后必须刷新 coverage 与 Obsidian bridge，并通过安全卡、关系网、扩展审计、复习调度、全量 intake、真题语料审计和活动引用清零检查。
