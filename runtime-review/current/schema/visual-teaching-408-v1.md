# 408 逐点可视化讲解模板

本层保存参考资料支持的讲法，随个人摘要一起查询，不保存新的作答、错误、评分或掌握度。授权维护日期：2026-10-13。

## 权威来源与范围

- 规范点集合以 `知识点标签表.md` 为准，四科目录为 `.private/visual-teaching-408/{DS,CO,OS,CN}.json`。
- 每点结合2027考纲的 `teaching_boundary`、已接纳真题/解析与本地参考书核查；使用已有个人摘要选取重点，不把历史断点直接当作当前错误。
- 资料缺项、OCR错误、题解矛盾和教材未覆盖处写进 `source_review_note` 或 `checks`，不虚构“全部来源一致”。教材只记文件与页/章节定位，不复制整页。
- KSP保持既有身份与摘要。父点模板的继承只提供图解范围；现有 `needs_review` 不因此升级为小点考纲已审定。
- 2027考纲中没有旧规范代码的独立叶项，在本科目录的 `outline_templates` 以精确 `S27-…` ID存储相同结构的客观模板。它们只进入原生考纲视图，不新建个人知识点、学习记录或调度节点。

## 文件结构

根字段为 `schema=cs408-visual-teaching-catalog-v1`、`subject`、`templates`。`templates` 以规范代码为键；每项包含：

| 字段 | 用途 |
| --- | --- |
| code / title | 与规范表精确对应 |
| kind / focus | 图解形式和本点要解决的关系 |
| scene | 具体对象、位置、连接、方向与状态 |
| steps | 按本点机制设计的呈现次序 |
| checks | 适用条件、单位、边界与容易画错的地方 |
| personal_adaptation | 用当前真实作答和小点摘要收窄图解 |
| source_refs | 已读资料的 path、locator 及可选 sha256 |
| evidence_status | 固定 source_reviewed_proposal |
| effectiveness | 固定 unverified，不宣称用户已受益或已掌握 |

同属一类图不能代替逐点设计；用标题填空的重复场景不作为完整覆盖。模板中的操作示意独立于冻结题目，不预解后继题，不保存真实题的受保护答案。

实际小点的对象与父模板不匹配时，可在父项增加 `subpoint_overrides`，用精确KSP ID绑定 `focus`、`scene`、`steps`、`checks`、`personal_adaptation` 五字段，并可提供专用 `kind`。只有精确命中才覆盖画面并标为 `subpoint_specific_visual_override`；缺少专用形式名时标为“小点专用局部图解”，不沿用不合适的父图名。父点查询不携带整个覆写表。来源仍需覆盖该局部方案。它不修改小点正式归属，也不代表用户已掌握。

## 物化与读取

`visual_teaching_templates_408.py` 在冷构建核对四科精确覆盖及来源。`exam_tutoring_context_408.py refresh-current` 将模板写入 point-summaries、父点/KSP teaching views、哈希路由和 `知识点/学习路径/`；导出的同版快照保留完整证据对象。

`resolve --view after_attempt|direct` 完整返回 `visual_template`。KSP标明 `parent_template_adapt_to_current_subpoint`，包含当前小点身份与标题。`protected` 不返回模板内容或证据入口。个人摘要过期时，仍可使用来源有效的客观模板，但个人状态保持过期。

热路径只读取命中对象及其来源状态，不扫描教材正文、不重建。目录变化使源版本失效；所选来源变化会先于 `known_version` 缓存判断撤下模板，保留个人摘要。冻结快照查询只验证同版内容，不依赖接收端存在本机原文件。

旧 raw query 小预算可能明确返回 `omitted_for_budget` 与完整对象引用；实际讲题使用不截断的 `resolve`。共享408入口默认16KB（保留调用者显式预算）；快照单项超过预算返回 `budget_too_small` 与 `minimum_bytes`，需按返回字节数增大预算，不能把空列表当成无模板。

## 展示验收

讲题技能必须同轮把 scene / steps / checks 转成用户能看见的图解，依据当前断点选核心状态。源码、图名、路径、模板文字及“适合画图”均不等于图解已交付。最终交付必须是可点击、可展开的交互界面；静态结构作为界面内容，不能单独替代。数学公式仍用聊天原生公式。

验收区分：数据逐点有模板、原生与快照能读到、实际样例已生成并呈现，以及用户是否受益。前三者不能替代第四者；只有后续真实学习证据才能说明效果。

交互配方与前端过滤详见 `interactive-teaching-408-v1.md`。每个模板的interaction绑定原scene/steps/checks；小点派生配方保留精确身份和继承边界。
