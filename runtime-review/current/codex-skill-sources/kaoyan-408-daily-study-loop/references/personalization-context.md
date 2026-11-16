# 当前讲题摘要

优先使用固定学习入口的 `current_item_personalization.teaching_context` 或 `d-supplement-context.teaching_context`。`bounded` 绑定已核对象，`personal_context` 可用状态和省略字段说明本轮实际取得的语义。只在具体缺项会改变当前讲法时补读同题同机制的 `resolve --personal-scope full --materials core`。`known_version` 绑定聊天实际读过的同范围同预算结果。本机用 `version`，便携用 `context_version`。

当前考查点不自动是薄弱点。当前真实作答决定判断；个人最新摘要、小点真实断点、提示依赖和 `next_check` 只帮助选解释重点。`semantic_stale` 不冒充最新个人语义，`evidence_digest_only` 只作事实依据。缺件立即用当前证据讲解，不扫描全库、不在学习中重建。

## 读到模板后的动作

`visual_template` 是按规范知识点预备的客观讲法。固定入口已返回且贴合本轮权限时直接复用；core未带完整图解时，按question-worker在同一精确范围补读materials visual。visual-only不替代个人完整读取。

- `scene`：画哪些对象，怎样摆放，连线和箭头表示什么。
- `steps`：展示当前题需要的事件或操作，保留前后状态。
- `checks`：检查单位、条件、执行主体、状态变化和容易误画的边界。
- `personal_adaptation`：结合当前小点与真实作答决定图中突出部位；旧错误不是当前错误的证明。

同轮依 question-worker 先以聊天正文讲清本轮获准的线性主线，再呈现对应可点击、可展开的辅助图，不能把主线藏在图里，也不能把“已读模板”、模板文字、源码或文件路径当作完成。静态结构也须具备对象点击、解释展开和关联高亮；状态变化适用时增加步进。KSP继承的父点模板须收窄到本小点，不当作独立审定教案。图解仍服从 `protected` 与当前答案保护。

模板 `effectiveness=unverified` 只表示尚无效果证据；看过图、操作动画或说看懂都不更新掌握度。已有 `teaching_plan` 的真实会话证据和支持等级继续保留。

## 写入与版本

摘要、讲题视图和哈希路由在冷刷新时一起生成，导出的同版快照保留模板。来源及更正变化使相关旧版本失效；热查询只读命中的已核个人对象和必要材料，不现场扫描原始历史或编纂教材。新Capture不修改正式个人摘要、评分或FSRS。正式提交或授权摘要维护后的刷新失败，只补缺层，不重放学习。

沿用用户当前选择的模型。首段实质教学以10–20秒为优化目标，进度占位和静态测试不证明实际教学时延。

文字主线与辅助图解沿question-worker合同同轮交付；正文按本问逻辑连续解释，交互沿references/interactive-delivery.md及当前Visualize协议。依据同次interaction配方和有效个人摘要确定辅助区域、关系与允许步骤，主线不藏在点击里。after_attempt不自动授权公开答案，受保护内容在生成前排除，不放进隐藏面板或前端数据。包内旧教案不能覆盖当前交互要求；不重备、不重排、不重展示、不写作答来升级交付。
