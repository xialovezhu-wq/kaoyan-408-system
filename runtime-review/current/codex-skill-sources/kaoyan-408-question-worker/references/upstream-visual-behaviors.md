# 408 共用图解的上游核对

2026-11-14 阅读三个仓库的 README、文档、示例与源码。日常讲题复用现有本机入口，无需重查上游、安装包或联网。可视化片段在 Codex 的沙箱内执行，用原生控件和本地打包资源。

| 讲解问题 | 采用的能力和实际收益 | 保留的边界 |
| --- | --- | --- |
| 报文的发送、在途与到达跨多个状态，回看容易迷路 | 采用 UI SDK Timeline 示例的历史事件导航、当前步骤和焦点语义。在现有生命线上用原生按钮点历史报文，方向键可定位已显示报文，用锁定 core 的 `stepFromKey` / `stepBy` 切步。必要时用单独的到达步骤确定箭头何时落到对侧 | 上游 beta.2 的通用列表和 feed 不承担题内生命线，保留本机 beta.1 核心接口，不引入整个组件 |
| 切步后对象选中丢失，容易读错相邻两步 | 复用 UI SDK 的 `createSelection` 和 `createFocusRecovery`，按稳定对象键保留选择，及时更新前值与当前值。目标退出当前公开范围就清除 | 选择与状态变化均不记为作答或掌握度 |
| 320 px 下两套机制并排容易压扁，完全堆叠又难比较 | 用 Modern Web Guidance 的容器查询与显式网格。窄栏每个字段单独成组，下面保留两侧值和各自名称。宽栏使用对齐的表头 | 表格已按行对齐，不额外使用 subgrid。内嵌滚动、scroll snap 和跨视图转场会妨碍回看或焦点，未接入 |
| 步进、报文与详情各自更新会失去同步 | 复用 A2UI 1.0 的数据与组件分离及 Lit DataModel 的 `/interaction` 订阅。已公开 `/scene` 独立，交互只更新步号和对象键，原生路径消费同一数据 | 完整 A2UI 流式消息、agent action 与远程状态解锁需要宿主或网络，Codex 片段用本地状态即可。隐藏答案不进入初始或增量数据 |

- Modern Web Guidance 源仓版本 [562b533 · 布局、容器查询与网格示例](https://github.com/GoogleChrome/modern-web-guidance-src/blob/562b5335197ad3d6bf157f5eee2722da733aebc0/guides/css/css-layout/guide.md)；[滚动状态同步示例](https://github.com/GoogleChrome/modern-web-guidance-src/blob/562b5335197ad3d6bf157f5eee2722da733aebc0/guides/ui-behaviors/scroll-snap-state-sync/demo.html)。
- Vercel UI SDK 源仓版本 [724a7d6 · Timeline 接口](https://github.com/vercel-labs/ui/blob/724a7d6c2d0ca1251a8eb6172462ae220c484714/apps/docs/content/docs/web-components/components/timeline.mdx)；[当前步骤示例](https://github.com/vercel-labs/ui/blob/724a7d6c2d0ca1251a8eb6172462ae220c484714/examples/components/timeline/web/status.ts)。
- A2UI 源仓版本 [5f6d26c · 1.0 协议与增量数据](https://github.com/a2ui-project/a2ui/blob/5f6d26c48a5c61bf845cf1ef2a02a47e31e8d9ba/specification/v1_0/docs/a2ui_protocol.md)；[DataModel 订阅实现](https://github.com/a2ui-project/a2ui/blob/5f6d26c48a5c61bf845cf1ef2a02a47e31e8d9ba/typescript/core/web_core/src/state/data-model.ts)。本机继续使用 0.15.0。
