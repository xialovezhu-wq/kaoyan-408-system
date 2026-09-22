# 408 当前题热路径入口隔离审计 v1

兼容文件名

文件名和脚本名为旧发布工具保留；当前输出 schema 是 `current_question_hotpath_entrypoint_audit_v1`，不再要求 personalization。

审计对象

`scripts/audit_personalization_entrypoints_408.py` 进行纯读取静态审计，验证：

1. 当前题上下文、私有题证、当前题事务和 direct router 均为可解析 Python。
2. 热路径不引用 personalization、历史偏好、正式节点、关系、其他题、linked practice、variant、Luna consumer 或 8767。
3. 当前题事务包含 nonterminal draft、semantic terminal、final package、唯一 Capture、durable reopen、精确恢复及 publish/hold/close policy；BLANK/UNCERTAIN 与 standalone 显式授权也受审，正式写入数为 0。
4. 已删除 broker、rich-intake、option-trial、feature-promotion、challenge 和 Stop hook 控制文件不得复活。
5. 旧 ordinary/personalized writer 与旧 benchmark 的公开入口都存在不可写退役门禁。
6. 下一晨准备不含 promotion、challenge、Guardian-build 或 config receipt 参数。
7. 技能源与共享合同声明 CurrentQuestionContext、final conversation package、答题热路径无后台 Luna，以及明确正式请求中的只读叶子/Sol 串行编纂。

输出边界

审计固定为只读，`luna_chat_call_count=0`、`formal_write_count=0`。PASS 只证明静态入口合同，不能代替动态速度、幂等、私有权限、Luna 两阶段质量、Sol 哈希隔离或生产状态不变验收。

运行方式

```text
python3 scripts/audit_personalization_entrypoints_408.py --repo .
```
