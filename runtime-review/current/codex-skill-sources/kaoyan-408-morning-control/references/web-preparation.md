# 网页备题、回包与预处理

网页项目固定说明不得超过8000字符（包括空白和换行）；交付前实计字符数及UTF-16单元数。新增要求先合并重复表述，详细字段沿随包合同读取，保留硬门禁，不把长启动词直接当项目说明。

只在准备新资料、接收或恢复网页回包时读取；已有 ordered_native_D 交接直接走 D 固定入口。

本文仅用于明确指定的网页流程；日常备题见 local-preparation.md。以下外传导出示例的现行命令须显式追加 --web-export。

## 网页原生并发备题

用户已明确允许网页原生子代理并发。主代理先固定同一source_commit、snapshot_id、input_version及目标日期，均从本批实际输入读取；模型沿用当前网页用户选择。先按不重叠知识点分配候选核读，子代理返回真实来源、接触、频次、相关性和排除依据；主代理完成全局选择后，再把最终选中题的素材、解析与教学备课并行分工。全局30题不是每个子代理30题，主代理最终核对14天、高频优先、min_independent_subquestions合同下限（当前常规为10，以本批登记值为准）、每主规范点至多一题、唯一题面、父子冲突及已给前问答案冲突，再输出一个ZIP。

子代理仅写互不相交目录；共享manifest、selection-contract、progress汇总与最终文件由主代理管理，不互相覆盖。只读MCP可并发，按实际限流降并发；不自行切模型、不伪造代理运行或写学习事实。环境无原生子代理时真实报告，由主代理继续。文字线性主线与对应可点击辅助图的教学合同保持；讲解线性不限制备题并发，不改变冻结active/paused学习。

## 本地备题输入与部分回包

2026-10-07新增frequency_priority_policy=high_frequency_first_v1：先守精确题面14天真实接触、真题来源和近期错误关联门禁，按同版frequency-priority.json及syllabus_context.exam_frequency选尽当前合格高频点，再依次补中频、低频；仅涉及/未知/未见考查保留原标签。日期、关键词多样性、题型或读取便利不能越过频次层。小问先在当前频次层内预留，高频优先下确实不足时如实登记requested/实际可供数量，不用低频挤出仍可选高频。每主规范点至多一题；“高频用尽”是当前合法候选用尽，不是全库高频已经掌握。旧active/paused队列保持原合同。

2026-10-03起，新`selection_policy=recent_errors_keyword_diversity_v2`先排除精确题面不足14天的真实接触，再要求考点关联近期真实错误、断点或提示依赖；尽量覆盖不同细粒度关键词，同类候选优先最久未接触者，未知日期排后。选择题与已审核独立大题小问同等竞争（40天前相关小问优于20天前同点选择题）。最后按关键词组织，不按全场日期排序；每主规范点至多一题，来源不足不凑30。真实接触仍由`scripts/recent_contact_408.py`统一计算，不能只读节点总表日期。

独立子题ID为`EXAM408-YYYY-QNN-Pxx`。使用已经本地核验的完整题面、条件和图表；若依赖前问，`provided_prerequisite_results`直接给出已完成结果作为输入，用户只做当前小问，绝不能再要求前置任务。保留`parent_true_item_id`和来源，不从父题复制全部小问。小问走D补充/同点替代的自由作答入口，当前小问答案仍私有。整题接触约束全部子题；给定前问结果不计独立掌握。同包不得测试另一题已经给出的答案；共用输入本身可重复。2026只采用用户指定新精解版，旧版不覆盖。相关具体合同见仓库`schema/exam-subquestions-408-v1.md`。

未带新策略的已登记输入和active/paused会话保持原合同；不重排、不换题。

同一export_morning_input_408.py入口返回schema=cs408-morning-selection-input-v1时，只交一个真实ZIP和prompt_path。包内selection-contract.json已登记本地输入身份和限额；scope/完整原生集合仅用于版本/调度对账。网页只为选中题写完整题目、解题和teaching，不把容量外未选点写成学习完成或unresolved。回包继续用prepare_d_review_408.py；入口自动按登记合同验证题数、候选身份和唯一题面，生成本地subset decision后再走原生复核和预处理。不要手造本地审批、裁剪due元数据或先开始学习。

以下全量分片规则仅用于未启用限额的旧输入或用户另行明确授权的全量准备，不覆盖上述现行30题合同。

用户要求准备给网页的资料，或正式入库收尾已交接到本步骤时，调用 `scripts/export_morning_input_408.py --web-export --date <目标日期>`，交付真实 `408_morning_input_<目标日期>.zip` 和 `START_WEB_D_<目标日期>.md`。这是给网页的资料输入，不是可直接学习的回包。完整集合来自整个当前正式库，不只包含本批新题；网页完成真题选题/核验/个性化成稿，本地不预先选唯一代表。2026-09-27起导出默认直接写到T9数据盘 `/Volumes/T9-Data/01_408/408网页中转/晨间选题输入/<目标日期>/`（经T9哨兵核对），交给用户的ZIP与提示词链接用T9路径；T9未挂载或UUID不符时明确停止生成，不回落到本机。导出及分片完成后原生入口增量登记本批路径、SHA-256与T9回执到 `<storage-map>/`；登记失败保留真实产物，只补登记。


大批输入默认本地分片：超过40点自动返回 `parts` 和 `partition_set_path`；约150—180点用5—6包，当前186点为6包各31点，更多点继续拆小并分轮并发。必须交每片真实ZIP和配套启动词，明确“一个网页会话只放一包”，不要把完整父包或全部分片交给一个会话。可对已有完整输入运行 `scripts/split_web_input_408.py --zip <父包> --parts 6`；原件不变。每片固定同一快照、父集合哈希和assigned_unit_ids，只包含本片必要资料。MCP只读补查可并发，但不承诺无超时/限流；受限时降低同时运行数量，保留原分片身份。

给网页的启动词与权威D项目指令保持同一版本；现行内容为v8.1，兼容路径仍为 planning/2026-09-13-d-deepseek-optimization/D-408晨间备题-网页提示词-v7.3.txt。完整替换版保留原生两阶段并发、主代理全局门禁与单ZIP交付，并按当前完整父点/KSP摘要、精确题模板、lesson_bridge和exam_visual_binding编译来源忠实的文字主线与可点击辅助图；本批日期、版本、小问下限和候选从真实输入读取，不写死在固定说明。网页项目说明及固定资料交用户手动替换，不能把本地文件更新称为网页已生效。question_refs是现用MCP查询域，按真实ID/固定source_commit读取；fetch.source_ref绑定完整原文件SHA，returned_sha256只对应本段。
MCP 查询路径从结构化返回的完整 path/source_ref 字段复制，不从被截断的展示文本手抄或拼接哈希；PATH_NOT_IN_PUBLISHED_LIBRARY 时先对照同版清单修正路径，不靠扩大 max_bytes 重试。输入manifest位于真实输入根，不猜模板文件；本地副本更新不等于网页项目已保存。

收到多片D回包，先保全每个原件，再调用 `scripts/merge_morning_parts_408.py --partition-set <本地partition-set.json> --zip <回包1> --zip <回包2> --output-dir <新派生目录>`（派生目录建在T9 `408网页中转/网页回包保全/morning/merge-<目标日期>/`）。同一片的多个阶段包先核对实际文件，选完整且继承既有成果的一版；存在差异先按证据整合，不静默覆盖。合并入口恢复父批完整目标，缺片、文件冲突或无实质题目都保留具体缺项及原ZIP，本地只补未完成部分。单片不得直接prepare/start。只有完整合并并通过原生覆盖/来源/曝光门禁后，才交付学习启动词。不能把“分片已完成”当“父批已完成”。

网页必须先定位含manifest/concept-index/scope的真实输入根，另建输出根；batches.json是列表。每点保存实际文件，每5点交可下载阶段包，进度从实际输出文件生成；包内workspace helper显式使用input-root/output-root，恢复不以空模板覆盖。通用网页错误不足以证明上下文超限；报告保留已知原始错误，不把猜测写成原因。

2026-09-20起，新晨间备题只用真实408统考原题，不自编、改题或使用既有变式；普通来源原题及未核验年份题不得充当真题。逐题核验EXAM408真实身份、目标匹配、原图/原文和当前选题窗口（14天）的真实接触；不足14天排除该题，继续检查同点其他合格真题，无合格真题明确unresolved，不凑题。同一true_item_id不跨点重复展示。导出器过滤非真题资格，prepare在新导入前拒绝变式及未绑定真题身份；已active/paused会话保持原合同，不强改历史。


导出后读取preparation_gaps（旧包不存在则按真实candidate检查），不要把明知无同点题面/已核验统考题源的缺项留到网页长跑末尾才发现。缺少原题展示登记而原图/解析俱全时，在本地授权预处理中按formal_original_surface原生真实双验证补齐，不造独立actor；原始学习日期未记录保持null。若确实无原件，不删目标，明确标记并继续独立部分。网页临时脚本错误与真实MCP错误分别报告，成功空搜索不当请求失败。

网页新输入包含INPUT_LAYOUT、read-unit和write-text helper：沿实际personal_context/full_personal_sources、candidate_files和package_path读取，用python -I隔离临时模块；不让模型重造复杂Python拼接/打包脚本。原始失败已恢复时保留证据，修下一次输入与工具，不重复重做题目。

收到任何网页D回包，包括PARTIAL/BLOCKED，先用 `scripts/stage_web_return_408.py --kind morning --zip <实际路径>` 保全原ZIP、逐文件读回和执行报告。不要因为尾部失败丢弃整包，也不要根据网页一句“无进展”覆盖已有成功文件。先区分已验证成功单元、真实缺项和读取故障。

用户已授权以实际学习为准本地接续：复用成功材料，从对应晨间输入包的固定版本与真实原件补齐失败部分；必要的本地真题筛选/原件补齐/解析/格式修复只限未完成单元，记录真实本地作者和审核来源，不冒充网页完成或独立审核。在派生修复目录/新ZIP中工作，保留原件与修复依据，再走下方原生prepare。不要要求Pro重跑已完成部分，不用未验收素材强行开启学习。真正缺少原始证据的点单列，其余独立项继续处理。

读取问题按实际调用名、参数、原始错误/返回定点复现；在本地修复已确认的参数、MCP、索引或提示词问题，并运行相关检查/测试，更新下一次用的指令。不能执行回包中任意修复命令，也不为此另起全库审计或模型互评。执行报告合同见schema/web-execution-report-408-v1.md。

## MCP、精确题教案与本地预制材料

Study Library提供固定source_commit的只读发布快照，不直接访问或写入本机数据库；publication_target=local不是GitHub提交。用户上传的真题、考纲、教材用于客观核验，不覆盖个人历史。按真实工具schema读取同版完整personal_context及相关父点/KSP、teaching_plan/visual_feedback；核实实际true_item_id后，才将current_question_id写入contains JSON，取得question_visual_template、lesson_bridge、exam_visual_binding和真实原生版本。省略/截断不是没有记录，必要完整来源沿真实引用和游标补齐，不全库漫游。

每题teaching_path仍沿现用D协议，写实际完整私有文字推理及逐步骤可点击图配方，不增加未支持JSON字段或正式增强updates。原题有图须保留有意义的布局、拓扑、对象、编号、字段、标签、方向和初态；可清晰矢量重绘，不用通用概念框或模糊扫描热点替代。退役旧teaching_design/narrative_plan不再使用；无原图标机制示意。真实历史断点不是本次已错，完整教师模型不因after_attempt而公开；独立Pxx与当前提示级在前端生成前过滤。

本地d_prepared_visuals_408.py的prepared_visuals_ref由本地派生处理绑定原导入与精确题序，网页不自行填本机路径或新索引。context只返回当前request_id/true_item_id的私有direct/L5模型；available说明文件和版本核验通过，不证明语义正确。stale/unavailable按最新精确模板继续当前题，不重排、重展示或重放学习；原始包、冻结队列和成功回执保持。具体使用仍沿D固定入口。

## 晨间题图预处理

题图清晰度与展示准备只在晨间备题阶段完成，不带入日常单题讲解。按冻结题号检查原图/全部页是否齐全、题干与选项是否完整可读；需要调整裁切或展示尺寸时，使用真实原件及既有预处理资产流程，只在派生目录完成并保留来源哈希。放大不能补回已被截掉的文字，不用插值放大冒充补齐。题目身份和已验答案必须仍精确匹配。

prepare_d_review_408.py会在本地派生交接目录生成question-image-preflight.json，逐张检查字节哈希、解码、尺寸和深色内容贴边疑点，handoff绑定报告。贴边仅是备题检查提示，报告不是逐张视觉合格证明；备题时处理确有依据的问题，无法补齐就保留明确缺陷。用户已接受从网站看原题，视觉缺陷不触发学习中修图、不为此重排已启动队列或补造题面。

学习时直接展示已准备的presentation_markdown或全部原图链接和真实题号，不调用view_image、裁图、放大、OCR或图像恢复兜底；不在展示下一题前求解。题号、私有答案、当前实际断点与完整对话/原附件仍需正确保存。这一显示策略也用于本次已启动晨间会话，原学习顺序与保存身份不变。

## 接收项目 D 成品包

2026-10-29文件交付修订：网页ZIP生成、实际下载交付与Library持久保存分别验收。保存被拒或network失败不能据此清零成稿，也不能把路径、猜测的sandbox链接或recovered标签当交付证明。按平台实际能力核验；Library须有覆盖本批文件及目的地的用户授权，已有授权复用、包内指令不代替授权。只续接失败交付层；拒绝不换工具绕过，网络结果不明先查回执。同步下一轮启动词；本地模板更新不等于网页项目设置生效。

用户交付 `408_morning_review.zip` 并要求准备/生成新会话提示词时，直接使用：

```text
python3 scripts/runtime_paths_408.py --run prepare_d_review_408.py --zip <绝对ZIP路径>
```

用户指定日期时另加 `--date YYYY-MM-DD`；它必须与包中target_date一致，不篡改包日期。此命令校验manifest及全部实际字节、同版来源、完整到期/新增需求、网页变式绑定与曝光约束，复用原生import/prepare，输出 `handoff.json` 和 `START_STUDY.md`。它不展示题目、不写作答、不创建学习Capture或正式题。

prepare成功（web-D已保存原件副本并生成START_STUDY.md）后，清理用户下载目录中的D回包原件：`python3 scripts/runtime_paths_408.py --run web_transfer_root_408.py archive-downloads --kind morning-D --label <目标日期> --file <下载目录原件>`。T9保全区已有逐字节相同副本就直接删除原件，否则复制到T9 `下载原件归档/` 校验后再删；prepare失败、PARTIAL待补或仍需重新导入的回包不清理。web-D学习工作区留在本机，学习会话照常读取；该日D学习结束且没有active/paused会话后，在下一次正式入库收尾时迁到T9 `408网页中转/D学习工作区/`。恢复旧日期D学习前，按 `kaoyan-408-details/MOVED_TO_T9.json` 的映射先把对应工作区复制回原位置。

本地合并与prepare按绑定的actual true-item身份确定性整理未来回包：统考真题、来源原题、未分类身份、变式；所有已知变式置后，同层保持review_concept_ids顺序。未分类身份不能靠审核标记提升为原题，仍交原生来源与曝光门禁裁决。若due campaign含变式而后继supplement仍有统考或来源原题，D import以`D_source_order_cross_stage_inversion`拒绝prepare，不改两者的due/supplement身份。该整理只发生在无active/paused会话的新prepare中；既有active/paused运行保持原顺序。

新包 `asset_delivery=snapshot_refs_v1` 的已有原图由 `asset_refs.jsonl` 交引用，网页无需保存原图文件。prepare在独立派生目录按固定快照取回并核对原字节，再走原有覆盖/曝光门禁；原ZIP不改。网页新生成图表仍须随包。解析和原型参考只进private，不能作为当前变式公开题面。取件失败只恢复资产层，不要求学习会话补图或重出题；字段问题才读原图引用交付合同（`<repo>/schema/d-asset-delivery-408-v1.md`）。

`WEB_PREPARED + PENDING_LOCAL_MATERIALIZATION`是网页交包状态，本地prepare成功才是已准备。单点检查包、BLOCKED/PARTIAL保全包不能因原图补齐就被当成全量晨间包，装配成功也不代表原图已被网页实际读过。

以下仅用于已存在历史回执的只读理解，不授权新备题采用变式。历史网页自检变式的出题、求解、逐项反例检查由项目D完成，本地只做确定性验收，显式保留“网页自检变式”身份。不得在正常接包或学习时重启author/blind/adversarial三代理审核，不把它伪装成独立三审通过。现有独立审核变式保留原身份。

回传真实生成的提示词文件链接，便于复制到用户自行新开的学习会话。该会话用什么模型由用户在对话中自选，提示词与交接只说明怎样读取已预处理材料并按固定入口学习，不写死、不推荐、也不切换任何模型。网页项目创建与提示词填写由用户手工操作，不使用Computer Use代填。提示词不内嵌答案、全部历史或工程文档；没有实际ready回执就不生成声称可直接学习的交接。当前用户未要求开始学习时，到生成提示词为止。

失败时保留ZIP和已有回执，先识别是否只是取件/导入后置步骤缺层；可原样恢复则使用同一ZIP，只缺语义时按上节完成派生修复包。不把单项失败当全批零成功。不能因D导入缺失或失败就调用通用prepare生成全量题级campaign，也不删除知识点凑成ready。原生 `NO_DUE_CONCEPTS` 如实报告，无须出题。

2026-10-07门禁声明一致性修复：新晨间输入的实际排除窗口由已登记selection-contract与本地策略确定，当前为14天。出包前本地validate_active_cooldown必须核对runtime-eligibility中exposure_window_days、window_days、recent_contact_exclusion_days及窗口策略标签，并核对候选和接触证明的eligible_on日期投影；旧7天标签、旧日期投影不得混入新包。gate-consistency.json记录该有界校验。只变更新派生输入，不改旧active/paused会话或冻结学习请求。网页仍保全真实冲突并报告，本地负责修复出包器及补齐失败备题；准备不计学习。

明确日期例外按当天plan的`morning_point_overlap_policy.policy`执行，必须有显式用户原话。2026-10-26最新选择“高频优先，允许重叠考点补位”，采用`high_frequency_then_nonoverlap_with_other_year_fallback_v1`：先非重叠高频，再其他年份重叠高频；全部合法高频用尽才中频、低频，同频次内非重叠优先。不能用不重叠中低频挤出仍可选的重叠高频。计划年份原题/Pxx及给出该年题答案的依赖始终硬禁，14天、近期错误、每主点一题、唯一题面和小问合同不放松。旧`prefer_nonoverlap_then_fill_other_years_v1`仅按原登记的旧式“全非重叠层先”合同兼容，不能覆盖最新日计划。导出、回包和首次开题均核当前模式；新模式即使全选非重叠题也核输入版本，旧未开题输入须保全后重备。已active/paused不重排、不重放。详见年度合同。


## 正式回流前的真实学习补充

用户要求同时打包正式增强和下一日备题时，可给原`export_morning_input_408.py`增加`--preparation-context <原始证据索引.json>`。使用cs408-preparation-evidence-v1，文件逐项SHA核验，学习日期必须在目标日前14天范围，focus_records和recent_contact_overrides必须指向索引内真实来源；原始完整对话随新晨间包带入。它只补充本批真实断点和实际复盘接触，既有正式到期集合保持原身份，不写个人掌握度、正式评分或FSRS。回包仍走已登记候选与原生prepare，不把输入就绪当成学习包已完成。

仅明确禁止某年原题、并未安排那年整卷时，在现有每日准备策略登记purpose=morning_exact_year_exclusion与excluded_exam_year，不写exam_year来虚构考试安排。此模式禁止该年父题、子题及给出该年答案的前问依赖；不额外排除其他年份同考点。普通整卷计划的考点排除规则保持。
