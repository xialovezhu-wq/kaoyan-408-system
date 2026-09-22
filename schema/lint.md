# LLM Wiki Lint Schema

本文件规定 LLM Wiki 兼容层和正式 408 关系网的检查方式。lint 默认只读，只报告问题，不自动修复；除非用户明确要求修复。

## 默认边界

- lint 不修改正式错题数据。
- lint 不修改 `复盘输出/`。
- lint 不自动创建、删除或重排节点、关系边、专题链。
- lint 报告可写入 `wiki/lint_reports/`，并可在 `wiki/log.md` 简记。

## 检查项目

1. `wiki/index.md` 是否遗漏重要入口。
2. `wiki/log.md` 是否记录 LLM Wiki 层操作。
3. `raw/index.md` 是否登记主要来源。
4. wiki 页面是否误保存完整题干、完整解析、答案、错选项、图片或手写痕迹。
5. 历史 ID 是否被误当正式节点 ID 使用。
6. 是否存在弱关系伪装强关系。
7. 知识点标签是否不符合 `知识点标签表.md`。
8. 错因标签是否不符合 `错因标签表.md` 的 E01-E10 或“暂无明确错因”。
9. 关系类型是否不符合 `关系规则.md` 的 R01-R09。
10. 复做推荐是否暴露答案、错选项或标准答案。
11. `复盘输出/` 是否在非明确日期复盘任务中被错误修改。
12. raw/wiki/schema 层是否试图绕过正式 408 入库流程。
13. query 结果是否已经沉淀到 `wiki/queries/查询沉淀索引.md` 或对应 wiki 页。
14. raw/wiki/schema 与正式系统之间的冲突是否登记到 `wiki/contradictions/张力与冲突索引.md`。
15. `wiki/coverage/408-COVERAGE-001_正式错题全量覆盖索引.md` 是否覆盖 `节点总表.md` 中全部正式节点。
16. `formal_source_only` 节点是否长期未被编译进概念、专题、错因模式或关系综合页。
17. 顶层 `DS/CO/OS/CN_*.md` 安全节点卡片是否为空，或是否缺少安全题目摘要、关键题设参数、问法类型、错误入口、复做第一动作。

## 推荐读取

基础 lint 读取：

- `wiki/index.md`
- `wiki/log.md`
- `raw/index.md`
- `schema/schema.md`
- `schema/ingest.md`
- `schema/query.md`
- `schema/lint.md`

涉及正式关系网健康检查时，再读取：

- `AGENTS.md`
- `README.md`
- `节点总表.md`
- `关系边表.md`
- `知识点标签表.md`
- `错因标签表.md`
- `关系规则.md`
- `知识点命中索引.md`
- `错题日期索引.md`
- `历史错题归档/定位规则.md`
- `历史错题归档/定位索引.md`
- 顶层 `DS/CO/OS/CN_*.md` 安全节点卡片，或运行 `python3 scripts/check_safe_node_cards.py --repo .`

## 报告格式

lint 报告建议写入 `wiki/lint_reports/YYYY-MM-DD-简短主题.md`：

```text
# YYYY-MM-DD LLM Wiki Lint

## 范围

## 结论

## 发现

## 需要用户确认

## 未执行的修复
```

## 修复规则

- 默认不修复。
- 用户明确要求修复时，先说明将修改哪些文件。
- 正式表格、关系边、节点库、专题链和复盘输出的修复必须重新遵守 `AGENTS.md`。
- 对答案泄露类问题，优先报告具体位置；是否清洗源数据必须由用户确认。

## 可复制提示词

```text
请按 408 LLM Wiki lint 流程做只读检查。

先读取 AGENTS.md、wiki/index.md、wiki/log.md、raw/index.md、schema/schema.md、schema/lint.md。
检查 raw/wiki/schema 是否混入完整题干、完整解析、答案、错选项、图片或手写痕迹。
检查顶层 DS/CO/OS/CN 节点卡片是否为空，或是否缺安全题目摘要、关键题设参数、问法类型、错误入口、复做第一动作。
检查历史 ID 是否被误当正式节点 ID，关系类型是否只使用 R01-R09，错因是否只使用 E01-E10 或“暂无明确错因”。
检查 wiki/index/log、操作中心、Base 看板、Tutor 设计和同步包是否互相链接。

lint 默认只报告，不自动修复；不要修改正式节点、关系边、专题链、复做记录、复习单元或复盘输出。
输出按 P1/P2/P3 分级，并列出待我确认的问题。
```
