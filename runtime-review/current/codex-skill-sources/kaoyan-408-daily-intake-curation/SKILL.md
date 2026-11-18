---
name: kaoyan-408-daily-intake-curation
description: "按原学习日、截止日或指定集合办理 408 正式入库与恢复。当日快存收尾交讲题读回、错点专项和全天 HTML。"
---

# Kaoyan 408 Daily Intake Curation

日终收尾按[408 左侧 HTML 交付](../_shared/kaoyan-408/sidebar-delivery.md)在左侧打开已备全天阅读 HTML，右侧给回执、阅读链接和后续专项入口。未开始的专项只取学习入口。

路径未知、机器布局变更或解释器失效时查[本机定位](../_shared/kaoyan-408/local-paths.md)。

所有用户可见的进度说明、判断依据摘要、错误说明和正文使用中文；代码、命令、标识符及必要原文保留原样。过程说明简短，不以英文进度或长篇内部思考代替教学。

## 当日快存全部正式入库

用户说「将今天快速入库的题全部正式入库」或同义整日指令时，先读[当日正式收尾](references/daily-closeout.md)。本轮授权覆盖上海原学习日的全部快存、错题讲解所用的个性化摘要与图解验回、错点专项预处理和当天 HTML 复盘。发来的学习会话用于核源和确认年度范围；当天账本决定全量集合。正式事务完成后同轮交两份可用成果。只有用户明确限定为单项、单题、精确集合或只读时走相应窄入口。讨论或修改 Skill 只改流程。

2026-10-28：本地明确授权的摘要质量修订保留真实历史，按具体机制维护 `teaching_focus` 的独立表现、明确缺口、提示后修复、未验证范围；同步完整概况、短摘要、小点与讲题投影。新资料只提供客观机制/关系，不能制造个人不会或已会。详略由真实证据决定，频次只决定应留意的考试重点。

日终将真实断点落实到本问的可见讲法。读回完整摘要后，从普通讲题的有界入口核同一小问及其下一检查。对本批改变的题源、空间结构和课件阶段，在备题时预处理并验回可操作对象；以展示端能读到的当前版本交接。阅读卡、专项和正式提交各自记录完成状态。

## 本机按原日期正式编纂

有实际内容的新正式批次统一走 enhancement-intake 的本地正式质量合同（`<repo>/schema/408-formal-quality-v1.md`）。每批用 OpenAI embeddings 在全库合规资料中找候选，用 Decisions 核具体机制、必要条件与终稿用途，再由原写者串行提交。提交前审核本批观察点与已登记的跨点依赖；提交后用本机原生入口回读整库父点、小点和向量路由。沿原 prepared 恢复 COMMITTED_QUALITY_PENDING。下列旧 start-batch、逐题 seal、外部建议和历史档案条目供查证与恢复已有事务。

既有语义审核可附实际观察到的证据采用或误用，按[当日正式收尾](references/daily-closeout.md#正式入库和错题讲解)用原 prepared 核对私有派生报告。没有真实送达、输出片段或实际帧依据时按未知处理。

授权正式入库后，当前会话直接核读本地原件并完成语义成稿。先用 intake_fact_capture_408.py backlog-plan 按原日期与用户范围读取两条本地写入路线，按原批身份核对已有 prepared/partial/COMMITTED。历史状态 awaiting_web_enhancement 交 kaoyan-408-enhancement-intake 的本地作者处理；已有回执按原身份恢复；旧状态按下方的历史恢复入口办理。backlog-plan 返回每个 processable item 的 intake_owner。local_enhancement_capture_ids 交增强作者；start_batch_capture_ids 只用于旧编纂项。同日两种状态共存时先恢复已有 active/partial 批，其他精确集合按原日期串行，不把两类放进一个写者。本机作者从原始对话和固定本地快照读取，编写逐题增强决定、父点/KSP、精确题教案与可视方案，再交既有唯一写者。真实 API 核证走本机已配置的增强入口；已有外部成稿只作兼容输入。

2026-10-07高频优先：正式个人摘要更新先按同版personal_context.syllabus_context.exam_frequency定位高频规范点，优先核对真实首答、机制断点、提示依赖、修复条件和next_check；频次是客观考试统计，不是个人不会或掌握的证据。保持全部受影响点及原日期的完整overview、short_summary、timeline和小点证据，低频记录不删、不漏、不改写为已会；不能仅凭频次自动修改现有掌握度。 同一原学习日期内优先审阅高频点，跨日期仍按原日期串行；完整集合和正式事务门禁不变。

原生 backlog-plan 的 final_gate.pending_enhancement_operations 给出未收尾的 prepared_receipt 和原批范围。题级已消费时用这个身份恢复概况、归档、预测或本机读回；已有完成继任的旧失败不重开。若精确请求仅覆盖原批的一部分，inspect_original_scope_only 只读核对，不能扩到其他Capture提交。

## 按回执恢复

原批状态不明时先核 prepared、WAL 和终态。已提交只补缺失的教学投影、展示、归档或其它回执；共享或独占原件继续按原合同核验。旧 Gate/start-batch、逐项校验、batch-size-1 串行写者及归档的命令在[既有正式事务的冻结、提交和归档](references/legacy-formal-recovery.md)。只有当前请求命中该历史身份或对应失败层时才读。保留 `FORMAL_COMMITTED_ARCHIVE_PENDING` 的已提交身份。

## Current tutoring evidence and publication

408 日常讲题用 `exam_tutoring_context_408.py resolve --personal-scope teaching --materials core`。正式编纂用同入口 `--personal-scope full` 和 `memory` 只读命令精查冻结原件及更正链。用户更正沿原更正 owner 追加。当前、历史、偏好、来源及关系以 `schema/memory-access-408-v1.md` 的版本和范围为准。

机制发现与经历发现分开；补找旧经历按experience、实际actor和真实时间范围定位后trace核源，不能将助手讲解或编辑摘要转成用户独立表现。最近候选不保证全历史最后一次，查询和备课不写学习事件；本批判断仍依据完整原始对话。

新修订判断依`schema/408-knowledge-profile-v1.md`维护逐项claims与原始回合来源，分别记录依据、证据状态和有效状态。旧未结构化概况保留旧格式标签，不自动抽取成确定事实。正式收尾检查personal_read_receipt的完整范围、更正引用与相关视图失效；本机查询同版验回通过即完成质量回执，失败沿原回执恢复。

正式个人摘要更新同时审阅本批真实文字讲解、辅助图解、用户反馈与已有教法版本。按2026-10-17明确纠正，同一实质讲解以线性文字讲清步骤和因果，同时用可点击图解辅助抽象概念与状态；依据该点真实断点调整文字起点、解释桥梁与图解重点，不把主线藏进点击或只复制通用偏好句。以完整对话和实际成品为依据，分别记录明确偏好、观察到的倾向、解释后真实变化和未验证范围；本轮偏好不倒写成旧日学习效果，助手自评、点击或看懂不等于独立掌握。保留有效版本，依据新证据小步调整，不启动递归后台评测。

短摘要保留最小形式偏好、适用范围和下次检查；详细依据写入既有 `teaching_plan.visual_feedback` 与概况/原日期时间线。旧父点及小点方案须按 `teaching_plan_update` 明确保留、修订或退役，不因完整替换无声丢失。网页输入/回包使用便携视觉方案合同（`<repo>/schema/408-portable-visual-teaching-v1.md`），本地直接维护仍由原生profile写者核验原件；不另建偏好库、不修改客观模板为已验证有效。

按概况合同的衔接约束（`<repo>/schema/408-knowledge-profile-v1.md`）核对overview、short_summary与各级next_check/plan.check一致；图阶段不代替本题推理步骤。适度拓展可沿同点区别、考纲上下级与经审核关联，目标须同时纲内且有真实题目考查依据，优先高频目标，当前断点未解决不展开，未实际讲过不写学习事实。`lesson_bridge`由冷构建派生，不另存为个人掌握记录。

完成时验证相关父/KSP的本机 after_attempt/direct确实返回方案与反馈、protected隐藏，旧版本缓存失效。原始新学习尚未正式提交时仅是待审教法线索；已COMMITTED后只补摘要或本机读回缺层，不重放题级事件。

## 按需资料提取与疑义复核（原入口）

输入为 DOCX 且符号会改变题意时，对照原件渲染或 OOXML 段落里的 `w:sym`、`w:rFonts` 与 `w:t`。标出段号、字体、字符码和提取差异。两个同类文本提取器读出相同字符串，仍可能一起遗漏符号。Docling 的修复只作为已覆盖案例的对照；对原件、公式对象和 Wingdings 逐项保留可核实范围。疑似解析缺陷另列最小修复单。

本机普通核读先用清晰的原文、原图及离线提取。只有当前另有远程资料处理授权且原件辨识仍有疑义，才复用 `<bridge>/scripts/study_material_preprocess.py`：用 `<bridge>/.venv-study-mcp/bin/python` 执行，传 `--subject 408 --source 原件绝对路径 --pages 1,3 --purpose reference`，purpose按来源角色选reference/question/user_work/solution；公开网页可用 `--url`，不带浏览器凭证。先本地文字提取，扫描页/图像按需调用视觉 API；不为清楚的资料重复增加一层模型。输出原件副本、提取稿和缓存走本科已核验 T9 冷目录，来源/hash/页码保持可追溯，不修改 Capture 或正式库。

`--review auto` 是默认，仅对提取疑义页追加 Decisions；已有明确疑义可用 `--review-pages 1,3`，页码必须属于本次 `--pages`，`--review off` 只跳过此旁路。它比较原页图与提取稿，提示可能漏项、混区、角色或文字不一致；HTML/TXT 没有原页图时明确未筛查。无疑义不默认每页复核；空白/过少内容等可程序核查的情况先用代码。失败保留已提取结果与未筛查原因，不重跑成功正式写入。

默认 `protected`，`after_attempt` 也只返回元数据；只有当前任务已授权读取该内容及相应答案范围时，才用 `--view direct --allow-answer-text`。正式审核授权不扩展到未来题、兄弟小问或其他答案。机器提取、疑义复核和回包 `evidence_prescreen` 是不同的派生辅助：均不替代实际核读原图、完整真实对话与本科语义审核；未标记不是审核通过，提示后完成不是独立掌握，缓存不是新学习。只将原证据支持的结论交既有唯一正式 writer，保留原日期、身份、原评分/掌握度与幂等恢复规则。
