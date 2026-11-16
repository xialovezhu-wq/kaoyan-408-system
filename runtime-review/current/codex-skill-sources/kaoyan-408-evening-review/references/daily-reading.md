# 当日交互 HTML

正式日终在 `apply/status` 和原生讲题验回通过后运行。只有用户明确要求远程访问或外发时才发布副本。

```sh
python3 scripts/runtime_paths_408.py --run evening_review_408.py local-context --date YYYY-MM-DD
python3 scripts/runtime_paths_408.py --run evening_review_408.py prepare --input <本地输入ZIP> --plan <经核验T9目录/阅读计划.json>
```

先完成 `local-context`，核对 `READY_FOR_LOCAL_AUTHORING`、`handoff.json` 与 `manifest.json` 的日期、全部 Capture 和 `source_commit`。新本地输入的 `source_commit` 绑定原生讲题 `source_version`。从包内原对话、附件、完成回执和 `personal-context.json` 编排全天。复用当轮已核机制，按科目和实际知识点设计目录，涵盖当天有教学内容的正确题、错题、复做及考点讨论。多个来源可合并成一张卡，同一题的几个不同机制也可分卡。

每卡写定义或对象是什么、判断规则与适用条件、核心结论。`secondary_conclusions` 收实际可推导且已核的常用结论，每条配条件和边界。没有该层结论可留空。状态变化、时序和字段映射优先用可点对象、可切步骤的图，图中标签和值与正文一致。复杂关系可按当前共享视觉规范做分阶段机制，不把教材、个人经历或完整试卷复制进去。纯定义或无可靠图源的单步结论允许 `mechanism: null`。

## 阅读计划字段

- 顶层使用 `schema=cs408-daily-reading-v1`、`subject=408`、`status=READY`、`study_date`、`source_commit`、`handoff_sha256`、`cards`、`coverage`。`subject` 为字符串；日期、版本与 SHA 复制自正式完成后的本地输入。阅读版不设阅读时长与题数配额。
- `cards` 每项含 `card_id`（`R408-` 加唯一短 ID）、`concept_key`（当天规范考点）、`source_capture_ids`（非空已核来源）、`title`、`definition`、`conditions`（文本数组）、`conclusion`、`secondary_conclusions`（每项为 `{conclusion, condition}`）与 `mechanism`。同一知识点的不同状态或条件可按独立诊断需求合并或拆卡。
- `mechanism` 复用 `<repo>/support/interactive-408/` 的 408 场景对象。只填 `title`、`nodes`、`edges`、`states`、`rows`、`initial_state`，以 `<repo>/scripts/public_scene_408.py` 和 `support/interactive-408/examples/` 为准。公开文本和状态等级为 0。优先将实际字段、方向、相邻步骤和变化连起来。来源、隐私摘要、未解锁答案及未来卷内容留出页面的数据。
- `coverage` 以当天每条 Capture 为一行，含 `capture_id`、`card_ids`、`reason`，与卡片来源双向匹配。确无可教学内容的记录可给空 `card_ids` 并写真实原因。正确题只要有已核学习内容就纳入对应卡片。

## 成品验回

从 `prepare` 的 `review_path` 打开 `review.html`，后台验证目录跳转、对象点击或步骤变化、正文条件和图的对应，以及笔记重开仍在和导出文件可读。备注保存在当前浏览器的本地存储。交付可直接阅读的本机 HTML，阅读产物留热库，作者计划和检查材料在经核验的 T9 目录登记。确认网页里只有当天获准复盘的知识内容。打开网页和点图不产生复盘作答、正式摘要或调度变化。
