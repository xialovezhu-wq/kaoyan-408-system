---
name: kaoyan-408-question-worker
description: 408真题、独立小问与局部纠偏。按精确题号读取同版考纲、考频和有效个人证据，依据当前断点讲清文字主线并主动交付贴合机制的可点击图。获准后复习同考纲专题。
---

# 408单题讲解

本机路径和解释器先按[共享路径约定](../_shared/kaoyan-408/local-paths.md)定位；下文仓库命令在已定位的项目目录执行，历史回执保留原字节。

使用用户当前选择的模型，中文讲解。首段可用解释以 10–20 秒为优化目标。脚本读取、首段解释和完整反馈分别计时，保留必要推理、证据与权限校验。

## 常规路径

1. 从当前题面和真实首答定位第一处断点，保留正确步骤。不要求用户重复已给理由，不把选对字母、听过讲解或点图当掌握。
   当前题有备题交接时，沿本文的预制图解入口。2015 年注册年度会话直接讲原卷时，按精确题号走[已备单题课件](references/interactive-delivery.md#年度会话的已备单题课件)，复用同次返回的 `current_context` 和获准的框架。
2. D/campaign沿各自固定入口，保护冻结队列。普通题只读本机当前题、证据与课件；普通讲解及纠偏不封存、判分或发布。已读Skill和合同在本会话复用，不逐题重读，不查保存源码、help、环境或全库。
3. 先使用当前题相关且有效的个人记忆。聊天已持有同一精确题/Pxx、同一答案权限和提示级、同版材料时，直接复用已核验的首答、个人记忆、考频和可用图解。已有的课件文件直接展示。

只有材料缺项、范围变化或对应版本失效时才补查该项。新作答只读新增内容，首答保留原身份。个人记忆有更新信号时只核当前机制，考频变更时只读新版本对应统计，图源或权限变化时只更新相关图解。历史回放沿已核验的冻结版本及原答题权限。已查到的其他材料继续使用。

首次取得个人摘要或确需更新时，一次合并读取当前题及相关规范点/小点：

```text
python3 scripts/runtime_paths_408.py --run exam_tutoring_context_408.py resolve --repo . --current-question-id <当前精确题ID> --view <protected|after_attempt|direct> --personal-scope teaching --materials core
```

同次传已核 KSP 或具体机制词。未定位小点时，按 `question_scope.assessed_points`、`syllabus_ids` 和 `teaching_boundary` 选当前题实际需要的点。`personal_read_receipt.records[].status=bounded` 绑定已核验的个人对象，使用 `records[].personal_context` 本次返回的完整判断句、条件和检查动作。`omitted_claim_count`、`omitted_fields`、`omitted_points` 和 `omitted_subpoints` 标出本轮省略。确有一项会改变本轮解释时，才用同题、同机制、同权限的 `--personal-scope full --materials core` 或精确 `memory trace` 补读。`evidence_only` 只支持可核事实。`stale`、`unavailable`、`not_applicable` 或受保护时，从当前题和真实作答继续。

一次精确 `resolve` 同时取得本题和个人概况；旧 `query` 的有限字节结果用于兼容，不能用它判断完整概况是否存在。若精确路由命中而概况过期或仅有事实，保留同次返回的其它有效摘要。当前已授权读取、能守本题/Pxx/年份范围且旧证据会改变讲法时，用同版 `memory history` 或 `trace` 定点核对真实角色、日期、提示依赖和更正。概况过期、尚未编写、命中歧义和出于保护而隐藏分别说明。仅在机制身份未定或仍有具体证据缺口时做一次范围内本机候选补查；精确入口不等待 OpenAI 向量重算或整库刷新。

确需校验个人记忆版本时，本机用原结果的 `version` 传 `--known-version`，便携快照用 `context_version`。`unchanged` 复用聊天实际持有的同题/Pxx、机制、view、materials、读取范围和预算的结果。新断点或正式更新后只读受影响的范围。

独立Pxx未命中已核精确KSP，旧父点概况又未绑定本问时，回执`withheld_scope/unscoped_parent_personal_history`、`complete=false`，按`current_question_only`处理；summary、direct或“教师材料”标签都不能放开兄弟范围。仅当当前题公开条件能核到对应KSP时按原入口读其全文，否则说明本问个性化未就绪，只用当前题和作答，不为修复而编造scope或个人事实。

明确的题号/KP/KSP直接精读。只有自然语言机制描述且身份未定时，用同入口`memory discover --query '<本轮公开描述>' --intent concept --view protected --limit 8`找候选，再按题面核对并resolve。用户问过去某次作答或说法时用`--intent experience --actor user`，找助手解释用assistant，保留本轮题目/机制/年份约束并沿候选trace_request回读；event-after/before与latest/no-latest按真实时间要求传入。protected拒绝个人经历；after_attempt未绑定当前题的历史查询仅有元数据，不能升view补答案。direct中的native_record_summary是摘要，verified_original_turn才是核过来源的回合片段，仍核截断，不冒称完整原话。参数、原回合范围和known-before限制见统一查询合同（`<repo>/schema/memory-access-408-v1.md`）。必要条件缺项或身份有歧义时，同范围 discover 可带 --evidence-gap 做一次本机补查，保留原候选、来源与停止原因。精确题和 KSP 先 resolve；命中后精确回读。旧wiki只作补充；默认后端依共享合同的本地资格策略，精确身份、经历及保护边界不变，reranker未默认；经历命中不证明弱点、独立掌握或新增学习。
将个人证据落实到具体步骤。与当前作答相符的独立窄机制简带；首次出错或提示后才修复的步骤，拆开条件、对象和状态变化，图从该处开始；未验证的条件用同一条 `next_check` 准备下一次短检查。若当前作答不同，以当前第一断点为准。旧叙述无结构化结论时，只采信能核实的原事实，不由相同术语、选项或图效推断掌握。简要交代真正改变本轮讲法的个人证据。

先就本轮首答判定哪一步成立、哪个条件还没用上。再将适用于当前条件的历史判断落实为一个教学动作。独立成立过的窄步骤略讲，提示后完成的步骤指出当时给过的前提，保留尚待独立检查的条件。用当前题设比对更正所引条件，并核对出处、角色与提示依赖。若省略、冲突或缺少审核影响本轮判断，先保留不确定性，沿[个人上下文](references/personal-learning-context.md)定点补证。题卡关联、用户自报和助手讲解各按原角色理解。仅在历史确实改变讲法时简带依据。
4. 每题首次答后反馈或直接讲解，必须在正文实际说出考点名称、高频／中频／低频等原始等级、统计年份范围、历年总考次及主考／兼考次数。优先直接使用同次返回的`question_exam_introduction.primary_intro`；按父题去重，等级按同版主考统计，不用总考次自行定级。简短确认、局部纠偏也不省略；只有本题在当前对话已经实际输出过才免重复，查过或预生成不等于已输出。
   小点只有父考点统计时，明确说“父考点〈名称〉的统计”，不能冒充小点自己的次数或等级。缺失、仅涉及或未标注不硬判低频；明确报告数据缺口，不猜次数或现场扫描重算。`protected`仍隐藏可能提示答案的考点信息。发送前核对考频开场或数据缺口已实际出现。始终守当前2027考纲边界。
5. 随后给已核实的实质文字：题面线索、目标、第一动作、条件、步骤与因果。已有路线从卡点接续。明确要直接讲解就完整解释；否则控制提示级，每轮最多一个推进问题并等待。

## 图解优先复用

- 备好的错题／真题专项与常规 D，先用当前学习交接的[预处理图解入口](references/interactive-delivery.md#备题交接里的预制图解)。专项从 `next.teaching_context` 读本题，D 从 `prepared_visual_context` 取本题当前阶段，直接使用核验过的成品。个性化摘要仍取最新有效版本，正文按本轮实际断点讲。完整讲解与提示分别取各自阶段；只读取当前精确题或小问。

- 每次制作视觉按当前 Codex 安装目录的共用规范（skills/_shared/visual-explanations/visual-guide.md）采用 Modern Web Guidance 的容器布局、UI SDK 的拖动、点选与焦点行为、A2UI 的公开数据与组件分离。遇到适合的多步流程、报文或状态对照，默认走[共享步进图](references/interactive-delivery.md#同步步进与对照)，提供可拖时间轴、可点状态及同字段对齐；报文保留原版纵向生命线、箭头和历史轨迹。渲染前沿现有入口过滤本题可公开内容。日常沿现有素材和模板出图，同上下文不重读规范；共享资源未安装时沿原生成器。

- 普通“讲讲这题／我卡在这步”即按机制选择图解，用户无需点名工具。先写独立可读的正文。真题完整讲解获准且范围匹配时，用 `local_syllabus_lessons.exact_lesson_identity` 与同版 `catalog_sha256` 调用[精确题图解](references/interactive-delivery.md)，入口自动选经核验的本题时序或原图课件。只讲局部或仍在提示层时独立编译安全局部。报文、并发状态、地址、指针选能准确保留实际对象的图形，不能把示例换几个名字套用。

- `--materials core` 给个人依据、考纲和考频，`question_visual_template.binding_only` 只核对精确预制版本。需要编译局部图才按同一题/Pxx、机制和 view 补读 `--personal-scope full --materials visual`。两次 `source_version` 不同先读回当前范围。visual 的 `version` 只用于该视图。学生图仅用当前获准的机制内容。
- 当前`cached_visuals`、精确题模板和有效个人反馈决定图解。缓存确实贴合本轮机制与允许阶段时直接调用，不先写新HTML或重建。用户指定端已交付的可点击3D可作为辅助图，不另造同功能网页。
- 用户明确要3D时，在既有`visual_dispatch.entry`传`requested_presentation: "three_d"`，按当前屏幕授权显示。它只选媒体，不解锁答案；`solution`仍需当前明确完整讲解授权。未指定媒体时由当前本地会话根据题面与卡点决定文字、可点击二维图或三维课件，调用同一入口用 `tutor_presentation=text|two_d|three_d` 传真实判断；三维对理解确有帮助就复用本题允许阶段的本地模型，沿已授权的呈现端展示，不把模型判断伪称为用户点名，也无需外部模型代选媒体。仅在用户明确点名外部 Decisions 裁决时才传 `external_decisions_requested=true`。
- 只用当前题/Pxx、当前允许阶段。`after_attempt`不自动开放答案；不能将完整父题、兄弟问答案或终值装入低层提示的DOM、数据或恢复状态。
- 缓存只有局部或不贴当前问题时，说明范围，先讲清正文，再补必要局部。实时不跑整库构建、多轮截图或全套验收。确需新图走已有生成器，一次主宽度渲染与一次视觉核对；保留已展示版本，不覆盖原件。
- 图保留原题有意义的布局、编号、字段、连线和初态；无原图标为机制示意。点击核对真实对象、关系或状态，正文独立可读；源码、路径、静态放大图和“稍后画图”不算交付。
- 讲解 TCP 或其他交互消息时画实际生命线和定向报文，讲地址翻译时画字段和映射路径。知识点的安全客观机制可沿[同专题入口](references/exam-oriented-teaching-flow.md)按精确考点或本题读取，点开能看到实际时序、字段、指针或状态。

## 保存与深入处理

侧栏 Page 代码先用完整 `read_page` 核对用户首稿；修改后再读当前稿，不覆盖首稿。快存把实际 Page ID 交给 wrong-intake 的 `--answer-page-id`，原生入口在首次封包附上本题各次完整回读，不把只存在工具返回里的代码误当已包含在聊天正文，也不先封聊天再补代码。

普通题明确快速入库、快速录入或快速保存，或年度会话已授权并明确结束本问时，使用wrong-intake单次入口。验回后依该技能在同一条final交付简短框架和可点击考点树，随后才展示后继。只记录与未解题分别按原范围处理。保留首答、提示后变化、原图、完整对话及实际图解。单题正式请求由当前 Capture 接本地 enhancement-intake，尚未封存时先走 wrong-intake 的正式请求采证。「将今天快速入库的题全部正式入库」交 [daily-intake-curation](../kaoyan-408-daily-intake-curation/SKILL.md) 处理当天完整范围。

本题主线解决且范围获准后，按 `local_syllabus_lessons.topic_review_identity` 用[专题复习入口](references/exam-oriented-teaching-flow.md)选择直接相关的小点或相邻考法。每个扩展指向具体2027大纲条目，核实教材或精确真题的实际考查，并交代哪条条件改变；同章、同词或父点考频不能当作依据。正文一到两句，考点树最多四个节点，首屏标真实考频，详情随点选切换。已保护年份、兄弟小问和后继在渲染前排除。

只有复杂证据、题源冲突、私有分问或新图制作才按需读[完整讲题细则](references/full-teaching-contract.md)、[交互交付](references/interactive-delivery.md)或[考纲与真题流程](references/exam-oriented-teaching-flow.md)。缓存命中及保存导航不加载这些长文档。
