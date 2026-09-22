---
name: kaoyan-408-morning-control
description: "408 晨间准备与开场。默认单包交网页选30道真题，按精确7天门禁、最久未接触和近期断点排序；接收D回包并预处理，生成DeepSeek学习提示词。已有handoff直接开始或恢复，保护active/paused会话。"
---

# 408 晨间入口

所有用户可见的进度说明、判断依据摘要、错误说明和正文使用中文；代码、命令、标识符及必要原文保留原样。过程说明简短，不以英文进度或长篇内部思考代替教学。

## 本地备题输入与部分回包

2026-09-21起，现用morning-selection-policy把晨间限制为30道真题；先排除精确题面不足7天的作答/展示，最高排序优先级是上次真实接触日期最早，次优先级是近期真实断点、错误、提示依赖与未验证边界。未知日期排已知日期之后。候选池用于网页全局选30题，不是要求全部候选点备题；来源确实不足时保留缺项。

同一export_morning_input_408.py入口返回schema=cs408-morning-selection-input-v1时，只交一个真实ZIP和prompt_path。包内selection-contract.json已登记本地输入身份和限额；scope/完整原生集合仅用于版本/调度对账。网页只为选中题写完整题目、解题和teaching，不把容量外未选点写成学习完成或unresolved。回包继续用prepare_d_review_408.py；入口自动按登记合同验证题数、候选身份和唯一题面，生成本地subset decision后再走原生复核和预处理。不要手造本地审批、裁剪due元数据或先开始学习。

以下全量分片规则仅用于未启用限额的旧输入或用户另行明确授权的全量准备，不覆盖上述现行30题合同。

用户要求准备给网页的资料，或正式入库收尾已交接到本步骤时，调用 `scripts/export_morning_input_408.py --date <目标日期>`，交付真实 `408_morning_input_<目标日期>.zip` 和 `START_WEB_D_<目标日期>.md`。这是给网页的资料输入，不是可直接学习的回包。完整集合来自整个当前正式库，不只包含本批新题；网页完成真题选题/核验/个性化成稿，本地不预先选唯一代表。


大批输入默认本地分片：超过40点自动返回 `parts` 和 `partition_set_path`；约150—180点用5—6包，当前186点为6包各31点，更多点继续拆小并分轮并发。必须交每片真实ZIP和配套启动词，明确“一个网页会话只放一包”，不要把完整父包或全部分片交给一个会话。可对已有完整输入运行 `scripts/split_web_input_408.py --zip <父包> --parts 6`；原件不变。每片固定同一快照、父集合哈希和assigned_unit_ids，只包含本片必要资料。MCP只读补查可并发，但不承诺无超时/限流；受限时降低同时运行数量，保留原分片身份。

给网页的本片启动词与权威D项目指令保持同一版本；当前完整替换稿位于 planning/2026-09-13-d-deepseek-optimization/D-408晨间备题-网页提示词-v7.0.txt。question_refs是现用MCP查询域，按真实ID/固定source_commit读取；fetch.source_ref绑定完整原文件SHA，returned_sha256只对应本段。输入manifest位于真实输入根，不猜模板文件；本地副本更新不等于网页项目已保存。

收到多片D回包，先保全每个原件，再调用 `scripts/merge_morning_parts_408.py --partition-set <本地partition-set.json> --zip <回包1> --zip <回包2> --output-dir <新派生目录>`。同一片的多个阶段包先核对实际文件，选完整且继承既有成果的一版；存在差异先按证据整合，不静默覆盖。合并入口恢复父批完整目标，缺片、文件冲突或无实质题目都保留具体缺项及原ZIP，本地只补未完成部分。单片不得直接prepare/start。只有完整合并并通过原生覆盖/来源/曝光门禁后，才交付DeepSeek学习启动词。不能把“分片已完成”当“父批已完成”。

网页必须先定位含manifest/concept-index/scope的真实输入根，另建输出根；batches.json是列表。每点保存实际文件，每5点交可下载阶段包，进度从实际输出文件生成；包内workspace helper显式使用input-root/output-root，恢复不以空模板覆盖。通用网页错误不足以证明上下文超限；报告保留已知原始错误，不把猜测写成原因。

2026-09-20起，新晨间备题只用真实408统考原题，不自编、改题或使用既有变式；普通来源原题及未核验年份题不得充当真题。逐题核验EXAM408真实身份、目标匹配、原图/原文和精确7天题面曝光；不足7天排除该题，继续检查同点其他合格真题，无合格真题明确unresolved，不凑题。同一true_item_id不跨点重复展示。导出器过滤非真题资格，prepare在新导入前拒绝变式及未绑定真题身份；已active/paused会话保持原合同，不强改历史。


导出后读取preparation_gaps（旧包不存在则按真实candidate检查），不要把明知无同点题面/已核验统考题源的缺项留到网页长跑末尾才发现。缺少原题展示登记而原图/解析俱全时，在本地授权预处理中按formal_original_surface原生真实双验证补齐，不造独立actor；原始学习日期未记录保持null。若确实无原件，不删目标，明确标记并继续独立部分。网页临时脚本错误与真实MCP错误分别报告，成功空搜索不当请求失败。

网页新输入包含INPUT_LAYOUT、read-unit和write-text helper：沿实际personal_context/full_personal_sources、candidate_files和package_path读取，用python -I隔离临时模块；不让模型重造复杂Python拼接/打包脚本。原始失败已恢复时保留证据，修下一次输入与工具，不重复重做题目。

收到任何网页D回包，包括PARTIAL/BLOCKED，先用 `scripts/stage_web_return_408.py --kind morning --zip <实际路径>` 保全原ZIP、逐文件读回和执行报告。不要因为尾部失败丢弃整包，也不要根据网页一句“无进展”覆盖已有成功文件。先区分已验证成功单元、真实缺项和读取故障。

用户已授权以实际学习为准本地接续：复用成功材料，从对应晨间输入包的固定版本与真实原件补齐失败部分；必要的本地真题筛选/原件补齐/解析/格式修复只限未完成单元，记录真实本地作者和审核来源，不冒充网页完成或独立审核。在派生修复目录/新ZIP中工作，保留原件与修复依据，再走下方原生prepare。不要要求Pro重跑已完成部分，不用未验收素材强行开启学习。真正缺少原始证据的点单列，其余独立项继续处理。

读取问题按实际调用名、参数、原始错误/返回定点复现；在本地修复已确认的参数、MCP、索引或提示词问题，并运行相关检查/测试，更新下一次用的指令。不能执行回包中任意修复命令，也不为此另起全库审计或模型互评。执行报告合同见schema/web-execution-report-408-v1.md。

## 晨间题图预处理

题图清晰度与展示准备只在晨间备题阶段完成，不带入日常单题讲解。按冻结题号检查原图/全部页是否齐全、题干与选项是否完整可读；需要调整裁切或展示尺寸时，使用真实原件及既有预处理资产流程，只在派生目录完成并保留来源哈希。放大不能补回已被截掉的文字，不用插值放大冒充补齐。题目身份和已验答案必须仍精确匹配。

prepare_d_review_408.py会在本地派生交接目录生成question-image-preflight.json，逐张检查字节哈希、解码、尺寸和深色内容贴边疑点，handoff绑定报告。贴边仅是备题检查提示，报告不是逐张视觉合格证明；备题时处理确有依据的问题，无法补齐就保留明确缺陷。用户已接受从网站看原题，视觉缺陷不触发学习中修图、不为此重排已启动队列或补造题面。

学习时直接展示已准备的presentation_markdown或全部原图链接和真实题号，不调用view_image、裁图、放大、OCR或图像恢复兜底；不在展示下一题前求解。题号、私有答案、当前实际断点与完整对话/原附件仍需正确保存。这一显示策略也用于本次已启动晨间会话，原学习顺序与保存身份不变。

## 接收项目 D 成品包

用户交付 `408_morning_review.zip` 并要求准备/生成新会话提示词时，直接使用：

```text
/usr/local/bin/python3 /Users/your-user/Documents/kaoyan-408/scripts/prepare_d_review_408.py --zip <绝对ZIP路径>
```

用户指定日期时另加 `--date YYYY-MM-DD`；它必须与包中target_date一致，不篡改包日期。此命令校验manifest及全部实际字节、同版来源、完整到期/新增需求、网页变式绑定与曝光约束，复用原生import/prepare，输出 `handoff.json` 和 `START_DEEPSEEK.md`。它不展示题目、不写作答、不创建学习Capture或正式题。

本地合并与prepare按绑定的actual true-item身份确定性整理未来回包：统考真题、来源原题、未分类身份、变式；所有已知变式置后，同层保持review_concept_ids顺序。未分类身份不能靠审核标记提升为原题，仍交原生来源与曝光门禁裁决。若due campaign含变式而后继supplement仍有统考或来源原题，D import以`D_source_order_cross_stage_inversion`拒绝prepare，不改两者的due/supplement身份。该整理只发生在无active/paused会话的新prepare中；既有active/paused运行保持原顺序。

新包 `asset_delivery=snapshot_refs_v1` 的已有原图由 `asset_refs.jsonl` 交引用，网页无需保存原图文件。prepare在独立派生目录按固定快照取回并核对原字节，再走原有覆盖/曝光门禁；原ZIP不改。网页新生成图表仍须随包。解析和原型参考只进private，不能作为当前变式公开题面。取件失败只恢复资产层，不要求DeepSeek补图或重出题；字段问题才读[原图引用交付合同](/Users/your-user/Documents/kaoyan-408/schema/d-asset-delivery-408-v1.md)。

`WEB_PREPARED + PENDING_LOCAL_MATERIALIZATION`是网页交包状态，本地prepare成功才是已准备。单点检查包、BLOCKED/PARTIAL保全包不能因原图补齐就被当成全量晨间包，装配成功也不代表原图已被网页实际读过。

以下仅用于已存在历史回执的只读理解，不授权新备题采用变式。历史网页自检变式的出题、求解、逐项反例检查由项目D完成，本地只做确定性验收，显式保留“网页自检变式”身份。不得在正常接包或学习时重启author/blind/adversarial三代理审核，不把它伪装成独立三审通过。现有独立审核变式保留原身份。

回传真实生成的提示词文件链接，便于复制到另一个 DeepSeek V4.1 Flash 会话。网页项目创建与提示词填写由用户手工操作，不使用Computer Use代填。提示词不内嵌答案、全部历史或工程文档；没有实际ready回执就不生成声称可直接学习的交接。当前用户未要求开始学习时，到生成提示词为止。

失败时保留ZIP和已有回执，先识别是否只是取件/导入后置步骤缺层；可原样恢复则使用同一ZIP，只缺语义时按上节完成派生修复包。不把单项失败当全批零成功。不能因D导入缺失或失败就调用通用prepare生成全量题级campaign，也不删除知识点凑成ready。原生 `NO_DUE_CONCEPTS` 如实报告，无须出题。

## 接收已准备的 handoff / START_DEEPSEEK

读一次handoff并验证所给SHA，沿用其target_date、import receipt、preparation_id与run_id。只查该日期现用status核对准备身份：

```text
/usr/local/bin/python3 /Users/your-user/Documents/kaoyan-408/scripts/start_morning_coverage_campaign_408.py --repo /Users/your-user/Documents/kaoyan-408 status --date <target_date>
```

已确认当前题存在的恢复请求，直接用 [one-call-turn只读恢复](../kaoyan-408-daily-study-loop/references/one-call-turn.md) 的context命令，它合并核对当前run与题面并返回首答、讨论和摘要；ready_for_teaching后不再另查status/resolve，不追加恢复话术到学习草稿。尚不清楚是否启动时才使用上面的status。

准备身份一致且未启动，按用户“开始”的授权调用同一日期start；已经active/paused则恢复原run，不再次prepare。其他active/paused会话优先保护，不强关、不插题、不重排，必要时报告需恢复的具体run。身份漂移时保留交接并报告，不自行选新集合。

首次展示前只加载 `../kaoyan-408-daily-study-loop/SKILL.md` 与 `../kaoyan-408-daily-study-loop/references/learning-entry.md`。新v2已收敛为固定学习入口；one-call-turn仅保留只读恢复和历史请求。不要一开始展开所有历史/恢复文档。显示当前已验证题的全部公开原图/文本和真实来源标签，未作答不读出答案。

首次真实答题加载question-worker，wrong-intake在首次终态保存时加载，之后复用。start/status已有固定命令，不再查--help、目录或相邻题答案；题面直接展示准备好的链接。图不清或裁切不触发学习中处理；真实身份矛盾保留具体缺口，不猜题或重做来源验收。

start成功后，保留current-display pointer、run/item/display SHA、saving mode和公开题面。新v2所有题终态Capture，学习只记事实/曝光/进度；正式判分、FSRS、个人摘要由网页增强包回来后更新。之后由daily-study-loop唯一负责逐题反馈与保存。工具返回已经带出的next_display不再单独找receipt。模型沿用用户所选DeepSeek V4.1 Flash等，不在Skill内改配置。

## D 独立补充与完成

D原到期代表题和点级独立补充分开执行。补充包括原生新增需求和D回执中明确的同点替代DREP；后者不伪造原生需求，原到期题保持未答。已启动campaign先完成或保持暂停；不将补充题插入活动campaign。没有campaign时可直接开始补充。

进入补充时才读 [D补充固定入口](references/d-concept-supplement.md)，沿用真实handoff。首次用display；压缩恢复用只读context，不重新display。每题终态先给已核实的实质反馈，再用v11助手的d-supplement-turn，程序读取本题真实消息、填时间、按绑定计划取后继，校验Capture后同次返回全部后继题图链接；早发的实际反馈包含在完整会话中。复用返回的next_input_context、精确资料路径和presentation_markdown，不手填后继ID、不查date/help/源码、不在展示前求解下一题。默认来源加全部题图链接；不再看图、裁图、放大或修图，用户明确要文字时才用已知公开文本。暂不做正式知识点commit、不改关联原题日期、错误次数或FSRS。

最终分别报告due代表完成、补充完成、仍未作答carry-over。展示去重不等于学习完成；代表题答对不能批量更新其他题掌握度。实际学习与曝光记录真实发生日期，下载、生成、导入和准备不是学习事件。

## 没有 D 包的原生准备或旧会话

只有用户明确要求原生计算/准备，或需要精确恢复legacy时，才读 [原生准备与legacy](references/native-campaign-preparation.md)。继续保留完整到期集合、来源身份、7天精确题面曝光、全部segment就绪与原事务合同；不要让这些备题步骤进入已经准备好的学习会话。

需要新的本地变式而用户明确选择保留独立审核时，才用原三审链。网页自检方案的缺件回到D备题，不现场加模型审核。始终区分真题、来源原题、独立审核变式和网页自检变式；不从formal ID或文件名猜真题年份。
