---
name: kaoyan-408-daily-study-loop
description: "408已发布晨间题逐题学习。新v2通过固定learning_turn入口保存完整证据、返回个人摘要、校验终态和导航；模型负责讲解。旧active/paused保持原合同。"
---

# 408 晨间逐题学习

本机路径和解释器先按[共享路径约定](../_shared/kaoyan-408/local-paths.md)定位；下文仓库命令在已定位的项目目录执行，历史回执保留原字节。

沿用用户选定的模型，所有教学和异常说明用中文。先展示公开题面与真实来源，等待作答；检查当前选项与推理，一轮一个推进问题。首段实质反馈以10–20秒为优化目标，不承诺端到端固定时延。

## 入口与分工

真实campaign display带episode_capture_every_item=true时，使用[固定学习入口](references/learning-entry.md)。首次答题加载question-worker，首次终态保存才加载wrong-intake，此后复用。Codex/Claude Code 均优先提交本轮原生消息引用，由程序识别客户端并封存消息范围；模型只提交引用、当前判断和学习意图；程序决定首答、追加、封存和游标，不让模型手工串联业务命令或筛减返回字段。
question-worker负责实际讲解，wrong-intake负责完整证据合同，本Skill唯一拥有晨间保存与导航。普通standalone不借此自动入库。无v2合同的旧campaign才读[旧campaign接口](references/legacy-one-call-turn.md)；更早legacy managed用[旧当前题](references/legacy-current-item.md)。D独立display仍由morning-control的D v2入口负责，不能仅凭concept_supplement把campaign变式改走D流程。
缺当前身份时，只读一次固定context恢复；已有display SHA、题面和最近返回则复用。重读Skill不使身份失效。正常学习不查源码/help/相邻题答案/全库，不自行编写Python或Node适配器。旧one-call文档中的手工answer/append/finish组合只保留历史恢复，新v2用固定入口。

## 教学分支

- 选项与已展示推理充分正确：intent=complete，reasoning_result=sound；核对充分后先发实质反馈“做对了。”，提示后完成说“这次改对了。”；同轮继续完整保存，成功后展示授权后继，不重复确认、不提前声称保存成功。
- 只答字母：intent=discuss，reasoning_result=not_observed；保存首答后问一个关键理由并等待。reason_required=false不证明真实推理已核验。
- 实际错误或理由仍有缺口：intent=discuss，按证据填写partial/diverged，利用返回摘要直接纠偏，不提前封存。口误是否影响推理由模型检查；确需澄清就等待，不靠screened_correct绕过条件。
- 概念求助或已有部分思考但未定选项：response=null，intent=discuss，入口自动读取必要个人摘要，不依赖need_context。摘要必须用于内部选择讲解重点，不默认展示历史流水；先给最小概念框架，不制造首答。简单表格/图解按question-worker处理。
用户明确只核对选项时遵守限定，不据此宣称独立掌握或自动推进。用户指明停止/跳过才走对应意图，不将未完成纠错当作结束。

## 完整事实与收尾

每轮只交新增证据。优先按接口读取真实消息引用；原文传入仅用于既有请求恢复或明确外部原文兼容；当前会话来源缺失不自动降级。兼容模式整条保留首尾空白、换行和Unicode，选项与判断另填，不拆伪轮次、不复述旧全文。caller_supplied不冒充独立逐字核验。尚未实际发出的拟稿不能算原话；终态保存前已实际发出的反馈必须同次纳入完整证据。D由原生消息入口收集；campaign的new_turns/message_refs按实际顺序包含用户之后的已发反馈，不遗漏到下一题。
程序读取已存首答、草稿与提示依赖；模型仍须如实标明当前提示依赖。已观察到的提示不能后改成none。新v2每个真实终态都完整Capture，正确题也保存；讨论只入草稿。原题original_review，变式concept_supplement；学习正式写入/FSRS/个人摘要均为0，Capture等待本地增强入库，进度不等于掌握度。
intent=pause保存新增原话后停在当前题，不封终态；intent=end封未解题并hold；用户明确跳过继续才intent=skip。已封题的后到口误、更正或补充用intent=correct-late绑定原display SHA：不可变追加原Capture的证据，原包不改、当前题不推进、不补礼花。
只有capture_verified与package_verified都为true，且next_action=present_next时，才展示next_display的真实来源及所有题图；hold不展示后继。失败按原learning_request_file恢复，不换身份、不重发首答。仅读图失败用返回result_file的image-handoff，不重交作答。

## 个性化与显示

终态快存验回后用 `quick_review` 绑定刚结束题。按wrong-intake在同一条final交付回执、简短框架和可点击考点树，再展示后继。把回执的后继排除项和会话实际隔离年份传入专题入口。未解题、只记录及答案保护按本轮授权收窄。下一题区域仅显示简短进度、真实来源标签和准备好的全部题图链接，随后等待。不读图、不裁切、放大、OCR或修图，也不因图片缺陷再补读原件。用户在网站看具体题目，明确要文字时才提供已知公开文本，不猜残缺选项。保存仍保留完整原话、题面和附件。下一题已核验材料按返回精确路径复用，不在展示前另行求解或猜解析目录，当前作答仍须独立判断。

纠错直接用 `current_item_personalization.teaching_context`。按 `personal_read_receipt` 使用有界 `personal_context`，从当前作答找第一处断点，再用适用的最新修正、提示条件和 `next_check` 调整讲法。`omitted_*` 或过期只说明本次可用范围。确有缺项会改变解释时，沿 question-worker 精确补齐同题同机制。`unchanged` 复用聊天已收到的同范围同预算结果。正确确认与只问理由沿原入口。
讲解沿question-worker的统一路线：正文先说明本问目标、整体路径及当前获准步骤，同轮可点击图只辅助需要看清的抽象机制，不把每步都做成图。优先同次精确题lesson_bridge组织步骤与局部图配方，再按当前回答、有效摘要和syllabus_context收窄；候选和教师字段不直接展示。主线解决且权限允许后，从本地 local_syllabus_lessons 核本点原纲和不同实际考法；其余相邻区别按已核关系与当前需要展开。具体交付和答案保护由question-worker/references/interactive-delivery.md及exam-oriented-teaching-flow.md统一维护；单一事实、导航和用户明确纯文字请求保留例外。
同次返回question_visual_template时，先核对其question_identity确为当前display身份，再优先使用该题的真实对象、初态和专用步骤；个人records用于定位当前断点。它不替代当前作答判断，不因同知识点就取另一题模板；missing/stale/source_limited如实处理。旧campaign不因预处理有Pxx模板就扩展其学习合同，小问继续由D入口负责。
semantic_current且必要个人范围已读齐才可作正式个性化依据；evidence_digest_only只作事实，semantic_stale不是最新语义。正文直接纠错，默认不展示本地记录流水、来源数量或整段历史；用户问某次经历而身份未知时先沿question-worker用experience/实际actor查候选并trace，身份已知才直接history/compare/trace。保留当前题/Pxx与view限制，不移除绑定来展开旧答案；事件日期与记录日期分别核对，未知不猜。摘要不冒充逐字原话，被替代或撤回判断只作历史说明。跳转用已核对的records.markdown_link/open_url、questions.markdown_link/open_url本地HTTP桥，不输出裸Obsidian URI或猜链接。
工具能力首次选择。真正functions.exec使用v10助手调用learning-turn；D独立补充按morning-control用同一助手的d-supplement-turn，同次完成保存等待、返回可展示的题图链接和已绑定礼花，不调用图像工具。只有exec_command时以yield_time_ms=30000执行同一Python入口，超过窗口沿原session等待；保存后按上述final衔接顺序展示返回链接，不再读图。node_repl不是functions.exec。礼花名取实际工具列表或首次一次发现；celebration_eligible=true且本题整题核验正确才调用一次，fired=true才称已放。维护、保存成功、重放和补充证据不触发庆祝。
日终要求正式入库时，先封存当前题，再由 kaoyan-408-enhancement-intake 在本地读取原件、编纂、串行写入并读回。单独明确要求导出时才使用 day-export，不把准备文件算作正式处理。

包内旧教案不能覆盖当前question-worker合同；after_attempt不自动开放答案，-Pxx不继承父题模型或兄弟答案。交付优化不重备、不重排、不重展示、不写作答。
