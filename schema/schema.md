# LLM Wiki Schema

本目录是 408 LLM Wiki 兼容层的规则入口，用于约束 `llm-wiki-ingest`、`llm-wiki-query`、`llm-wiki-lint` 如何配合现有 408 错题关系网。

## 优先级

1. `AGENTS.md` 是最高优先规则。
2. `README.md`、`GPT-Codex错题流转规范.md`、`复盘调度规则.md`、`知识点标签表.md`、`错因标签表.md`、`关系规则.md` 等本地正式规则优先于本 schema。
3. 本 schema 只补充 LLM Wiki 三层架构，不重构、不迁移、不改名、不删除旧文件。
4. 若本 schema 与正式规则冲突，以正式规则为准，并在输出中说明冲突点和采用理由。

## 三层定义

### raw sources

只读来源层。登记位置见 `raw/index.md`。raw 可以被读取、归纳和引用，但不直接产生正式节点、关系边或复习评分。

### wiki

AI 维护的综合知识层。允许保存概念页、专题页、易混点页、错因模式页、关系综合页、复盘策略页和 lint 报告。禁止保存完整题干、完整解析、答案、图片和手写痕迹。

### schema

规则层。规定 ingest、query、lint 的步骤、边界和禁止行为，防止 LLM Wiki 绕过正式 408 入库流程。

## 总禁止项

- 不用 LLM Wiki 绕开正式节点入库规则。
- 不把历史归档 ID 当作正式关系边端点。
- 不在 wiki 中保存完整题干、完整解析、答案、错选项、图片或手写痕迹。
- 不在单题入库时读取、生成或修改 `复盘输出/`。
- 不替用户编造日期、错因、主知识点、掌握度或来源。
- 不替用户给 `复习单元总表.md` 写 0-5 掌握度评分。

## Skill 对应

- `llm-wiki-ingest`：遵守 `schema/ingest.md`。
- `llm-wiki-query`：遵守 `schema/query.md`。
- `llm-wiki-lint`：遵守 `schema/lint.md`。

