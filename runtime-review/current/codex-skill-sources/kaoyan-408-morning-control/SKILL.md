---
name: kaoyan-408-morning-control
description: "408 常规到期 D 在本机原生选题、备课和验回，沿用 14 天与答案保护；也处理年度整卷和日终错点专项。旧冻结队列沿原交接恢复。"
---

# 408 晨间入口

本机路径和解释器先按[共享路径约定](../_shared/kaoyan-408/local-paths.md)定位；下文仓库命令在已定位的项目目录执行，历史回执保留原字节。

使用当前对话中用户自选模型；中文说明。用户要求开始或继续已有队列时直接学习；明确重审、重选时另建所请求的备题范围，保留旧进度。

## 做完一套年度真题后按薄弱点选题

本地用 `annual_weak_representatives`。用户已经点明错卷/薄弱点，或当前会话足以定位“今天那些题”“那30道”且意图是按这张卷的断点选题或复核时，直接用此模式。单纯恢复一份已开队列才走原身份。完整检索该卷真实薄弱点命中的所有选择题和已审核独立小问，最终从中选能复测具体机制的代表，最多30题，少于30即止。只给入选题备课和学习入口。

每个薄弱点单独分三层。先从最近真实接触已满14天的题里选；该层为空才从日期未记录的题里选；两层均空、候选均有日期且距上次接触不足14天时，取这些题中上次接触日期最早的那一组，再按实际所问取舍。刚满14天进入第一层。同一题按各来源真实日期取最近的一次，不能以入库日、考试年份或队列准备日代替。共享题因另一薄弱点入选，可以兼带本点，但不能拿它顶掉本点本应选择的高层候选。

普通年度专项按明确保留年份排除原题、独立小问及给出该年前问答案的依赖。其他年份与其考点重叠仍可选，刚复习过的源卷也不按年份整卷剔除。年度专项按真实断点与覆盖价值决定题型和数量。正式入库触发的[日终错点专项](references/local-preparation.md#当日正式入库后的错点专项)沿同一入口，并加上次年保护和每个真实错点至多两题。旧冻结会话、非年度旧题推荐和通用FSRS沿各自合同办理。

## 常规到期晨间

新常规 D 由本机调度器重算完整到期与点级请求，读取本机正式节点、原卷与独立小问、最近真实接触和原生讲题视图。沿[本地备课](references/local-preparation.md#常规d)编译，再验回本机导入回执、题图和逐题课件。只有用户明确要求远程访问或外发才使用发布；已冻结旧包沿原身份核源。

2026-10-07新增frequency_priority_policy=high_frequency_first_v1：先守精确题面14天真实接触、真题来源和近期错误关联门禁，按同版frequency-priority.json及syllabus_context.exam_frequency选尽当前合格高频点，再依次补中频、低频；仅涉及/未知/未见考查保留原标签。日期、关键词多样性、题型或读取便利不能越过频次层。小问先在当前频次层内预留，高频优先下确实不足时如实登记requested/实际可供数量，不用低频挤出仍可选高频。每主规范点至多一题；“高频用尽”是当前合法候选用尽，不是全库高频已经掌握。旧active/paused队列保持原合同。

## 已有 ordered_native_D 交接

先读一次 [D固定入口](references/d-concept-supplement.md)，用v13 d-supplement-start：传用户给出的 handoff 路径与SHA、导入回执SHA、session_id。程序在一次调用里核验两份文件与共同题序、检查活动会话、识别已保存结果，返回当前题和 input_context；在同一 functions.exec 缓存后直接展示全部 presentation_markdown。旧聊天首次采用v13仅加载新版助手，保留当前身份和进度，不重放start或曝光。

不用cat全文输出交接或导入回执，不另写核验脚本、枚举猜测d-learning目录或查campaign status。run_id=null在此正常。已知当前input_context的恢复直接用d-supplement-context protected，不重新display；保存待恢复只沿原learning_request_file补缺层，不重新开题或重放作答。旧v11已在学习的会话继续兼容，不换session、题序、原请求或曝光。

讲题先用 question-worker 的[备题交接入口](../kaoyan-408-question-worker/references/interactive-delivery.md#备题交接里的预制图解)，按当前题和许可阶段复用成品。包内题面、解析和图解绑定当前题，个性化摘要仍读最新版本。旧队列沿原 input_context、题序和进度，原交接缺成品时才按现有精确题模板处理当前题。

首次只给当前真实来源与全部题图，等作答；不提前读出答案、解释方法或求解后继。首次真实答题加载 question-worker；首次终态前加载 wrong-intake，随后复用。D讨论只讲当前题，终态才固定保存；保存验证成功才交后继。工具后的同一条final取终态 `quick_review`，按wrong-intake依次交付回执、简短框架和可点击考点树，再展示下一题。把后继排除项和真实会话保护年份传给专题入口。未解题只复述已公开局部，明确只记录时只回执。个性化摘要在答后按当前小点和current source_version读取，包内history不覆盖最新摘要。

当前原题保持 original_review 与准确formal_id，同点补充保持 concept_supplement，不更改关联原题日期/错误次数/FSRS。未选目标与carry-over继续未答；队列完成不等于完整范围掌握。学习日期和曝光用真实当天，恢复旧结果不补造新曝光。

历史 START/已缓存交接继续绑定原题序、保存合同和恢复位置。旧文字要求日终外发、等待回包或等待 Study Library 发布时，按现行本机后续入口办理。只结束或暂歇时沿原会话保存或停留。明确要正式入库时走 kaoyan-408-enhancement-intake，明确要晚间复盘时走 kaoyan-408-evening-review。两项均已授权时先验回本机正式回执、同版摘要和答案权限，再备复盘。只有用户明确要求远程访问或外发才发布。

## 其他已准备或旧会话

只有handoff明确绑定非空run_id的campaign，才按该日期查一次原生status，核对preparation_id/run_id；已active/paused恢复原run，不重新prepare。首次展示前读 daily-study-loop 的 [学习入口](../kaoyan-408-daily-study-loop/references/learning-entry.md)。已知当前题则只读context恢复，不再status/resolve；其他active/paused保护，不强关或插题。

没有campaign的旧D补充仍沿其真实handoff与display入口；不凭模型猜路径或重造顺序。只有用户明确要求原生准备或点名legacy恢复时，读 [原生准备与legacy](references/native-campaign-preparation.md)。

## 本地备题与旧回包

日常准备晨间、薄弱点专项和独立小问，用[本地备课](references/local-preparation.md)。选完后为每题绑定精确题面、私有讲解和可直接展示的预制图解，验回全部入选题的课件与实际 START_STUDY。当前会话沿用用户选的模型，读本地原图、解析、真实 Capture 和同版考纲，做实际语义和图解检查。Pxx 只准备本问，用户要求开始才曝光；原有已开队列按原合同恢复。

只有用户明确要导出给网页或带来旧D回包，才用[历史网页预处理](references/web-preparation.md)。历史包、哈希、作者、回执和已成功的语义保持原样；PARTIAL按原身份在本地续办。CLI显式外传用 --web-export。

常规D的目标为30道已核验统考真题；本段不适用于年度整卷错点专项。按精确题面最近真实接触排除不足14天，再关联近期实际错误/断点；关键词尽量不同，同等相关优先更久未接触，未知日期排后。按关键词组织冻结，学习中不再重排。每主规范点至多一题，来源不足保留缺项，不自编/改题/变式凑数；已有活动会话保持原合同。

独立小问保留EXAM408-YYYY-QNN-Pxx、父题和来源，只做当前小问。必要前问结果作为已经核验的已知输入，不能要求先做前问或把给定结果记为掌握；同包不能再测试已给答案的前问。2026只用用户指定新精解版。准备/导入/下载不写学习结果。

晨间原图只在备题阶段检查和处理，学习直接展示已准备图片链接；不view_image、裁切、放大、OCR或修图，显示缺陷用本地已核的原图与独立题设定位；不在学习时临时重做图片。本机保留当前学习队列及热课件，T9保全备课过程与旧回包。

讲解沿question-worker的文字主线与交互辅助合同：同轮先在聊天正文按本问逻辑讲清当前获准的步骤、条件与因果，再在对应位置提供可点击图解辅助理解，不把必要主线藏进点击。交互交付沿../kaoyan-408-question-worker/references/interactive-delivery.md及当前Visualize协议；优先使用同次精确题question_visual_template.exam_visual_binding、lesson_bridge及当前source_visual_plan_version，并结合有效父点/KSP摘要。依据备题阶段已核验的当前题图绑定，保留有辨识意义的布局、拓扑、对象、编号、字段、标签、方向和初态，可清晰矢量重绘；学习中不重读、裁改题图；点击对应对象或步骤展开局部核对并突出真实关联，不改成通用概念框或模糊底图上的空泛热点。source_visual_plan_review.decision=retire_old_design的旧teaching_design/narrative_plan不再使用，只有本轮来源核实的设计可作辅助；无原图明确机制示意，解析图和前问结果标明角色。interaction和有效个人摘要决定当前重点，syllabus_context限定2027考纲范围。无当前问教案就按本问条件编译；-Pxx不继承父题模型或兄弟答案。after_attempt不自动授权公开答案，完整预制模型仅direct/L5，安全局部在生成前过滤；口述不做成图片。包内旧教案不能覆盖当前要求；不重备、不重排、不重展示、不写作答来升级交付。

## 逐年真题学习

用户启用逐年整卷学习时，按本机 `schema/exam-year-study-408-v1.md` 使用 `.private/exam-year-study/plan.json` 和 `真题年度学习/<年份>/学习记录.json`。每年独立会话；年度内用户已授权逐题复盘与快速保存，题结束完整Capture后绑定年度索引，真实首答/提示后变化和未验证边界分别记，成绩未给留空，不提前正式判分或更新FSRS。常规D晨间导出、回包与首次开题依该日期D合同排除计划年份原题及其主兼考点/给定前问结果；年度错点代表题只按上文逐题隔离保留年。常规D范围内，讲解与拓展也不能绕回。未来年份必须来自用户明确日期，例子不当计划；缺少合法题记真实不足，旧包保全。

以下为常规晨间D的历史日期例外；年度专项不使用该覆盖规则。明确日期例外按当天plan的`morning_point_overlap_policy.policy`执行，必须有显式用户原话。2026-10-26最新选择“高频优先，允许重叠考点补位”，采用`high_frequency_then_nonoverlap_with_other_year_fallback_v1`：先非重叠高频，再其他年份重叠高频；全部合法高频用尽才中频、低频，同频次内非重叠优先。不能用不重叠中低频挤出仍可选的重叠高频。计划年份原题/Pxx及给出该年题答案的依赖始终硬禁，14天、近期错误、每主点一题、唯一题面和小问合同不放松。旧`prefer_nonoverlap_then_fill_other_years_v1`仅按原登记的旧式“全非重叠层先”合同兼容，不能覆盖最新日计划。导出、回包和首次开题均核当前模式；新模式即使全选非重叠题也核输入版本，旧未开题输入须保全后重备。已active/paused不重排、不重放。详见年度合同。
