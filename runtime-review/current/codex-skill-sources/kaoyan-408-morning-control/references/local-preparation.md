# 本地晨间和专项备课

当前会话自己完成阅读、选题、教师写作、校验和交付，以真实可开始的学习入口为完成条件。日终、年度及非年度独立专项读取本机原卷目录、考点连线与原生讲题视图，绑定实际来源哈希。正式收尾以本机验回为准，只有用户明确要求远程访问或外发时才发布副本。已冻结输入按原来源恢复，已开始的 D 沿原合同接续。

## 年度整卷错点的代表题

1. 依据已指定年度的完整 Capture 和作答轨迹，区分明确断点、提示后修复、未验证、只作背景。为每个需要复测的机制保留稳定目标ID、规范知识点、来源题号/日期及原件SHA，写入现有 `cs408-preparation-evidence-v1`。在 `selection_scope` 中标 `mode=annual_weak_representatives`，保留原卷身份、用户实际限定和显式保留年（可为空）。有上下文的“那30道”沿用当前年度弱点范围；仅为旧冻结队列续学时复用旧身份。
2. `python3 scripts/runtime_paths_408.py --run local_focused_training_408.py annual-sources --date YYYY-MM-DD --context <context.json>` 从固定本地库完整检索真实选择题和独立Pxx，核主考、兼考与有同题来源依据的补充命中。排除显式保留年的题、子问和会给出该年答案的前置依赖。按完整题号去重，同题可命中多W；其他年份同考点照查。仅在内部保全候选，勿把全部命中当作学生队列。
3. 用当前学习日及各题最近一次真实做过或展示日期，逐W选代表：①有已知≥14天的，从该层选；②无①且有未知日期候选，从未知层选；③所有可选题均为已知0–13天时，只允许最近接触日期最早的并列组，再按语义取最相关者。未知始终单列。本点因日期规则不合格的题可因另一点入选，但不得代替本点有资格的代表；逐题区分主复测及附带考到，各点编实际覆盖去向。无合格题说明缺口。无关题、纯背景、相似词、未核验题、变式不进入候选。
4. 视薄弱点数量、直接程度与题间互补选足有用题即可，总数0–30。一题可复测多点，一个点也可用不同真题分别检验不同操作；频次和Pxx名额不截断候选。保留真实题型、独立子问的原图与必要已知条件，严格隔离当前答案、兄弟小问和受保护年份。入选题本地编写教师解答、个人依据、纲内考法、分阶段可点击2D与可用的精确3D；原图不清或课件局限按实际核验记录。只编入选题。
5. 写本地审核 overlays：`selected_ids` 只列最终题，`selection_reasons` 给逐题依据，`selection_targets` 只列具备代表资格的实际错点。空选须给 `no_selection_reason`。逐题绑定教师主线、经核的提示和解答课件。运行 `python3 scripts/runtime_paths_408.py --run local_focused_training_408.py annual-prepare --workspace <年度候选根> --overlays <审核JSON> --date YYYY-MM-DD`。核对 `prepared_visual_question_count` 等于实际题数，再交真实 START_STUDY。开始时用 `local_focused_training_408.py next --queue <年度热队列目录> --ordinal <当前序号>`，按实际日期验资格，保留本题 `teaching_context`。答后由 question-worker 按同一题号和阶段读取。准备不写学习事件。

年度接收补充字段与安全展示

- 决议的 selected_ids、selection_targets、selection_reasons 只写最终代表题。qualified_hits 是已定稿的内部全库核源注解，不是学生题量。after_class 可把未直测机制挂到已选题；每条填 true_item_id、target_id、basis、teaching、visual_step，与按日期采用及自然兼带分开。
- source_queue 可继承旧队列同一题面SHA的教师资料。prepared_2d、prepared_3d、teacher_lessons 通过各自绝对索引路径和SHA绑定。完整解答图须有随热交接复制的实际 2D 或 3D HTML 文件；题面安全的一级提示可绑定已验本题原生阶段。若原图页眉另行印入泄题标签，先逐张核对，student_surface_overrides 可登记原SHA、crop_top、派生PNG绝对路径、SHA及review_basis；其余题面像素逐点相同。题面或提示3D能展开带标签原图时，使用独立核查过的安全阶段成品。
- 学习材料只按本问、本阶段读取：media --queue <本机队列> --ordinal N --stage question|hint-1|solution。共享播放器地址本身不能证明提示或解析授权。已证实未开始的同一年度同日旧专项，可由 annual-activate --previous <旧队列> --queue <新队列> --evidence <未开始证据> 增设指针；原 status/next 随即引导新入口。真实进度或接触阻止自动切换，不改旧热目录内容。

## 当日正式入库后的错点专项

复用本页 `annual-sources` 和 `annual-prepare`。在 `selection_scope` 内加 `daily_closeout: {"study_date": "原学习日期", "studied_exam_year": 2015}`。`studied_exam_year` 绑定当天明确在学的主卷年份，完成部分即适用。明确有多个主卷时，可在 `excluded_exam_years` 列各卷的下一年。零散学习无明确主卷时省略 `studied_exam_year`。`source_question_years` 列已核实际来源年份，确无真题来源时可用空列表。已核题号填 `question_identity_hints`，未知可空；真实断点、稳定 `target_id`、解释和已绑 SHA 的原件定位仍逐项必填。`focus_records` 全部来自 `study_date`。外层 `target_date` 和命令 `--date` 是本轮备题核验日，恢复时保留原范围及既有退让理由。

`annual-sources` 自动将主卷年份加一并写入 `excluded_years`，同时应用显式保留年与原学习日、备题日的登记。在最终落盘和实际开题前仍查原题、Pxx 及递归给定答案依赖。以实际设问核准窄机制，尤其是同一父标签下的假命中，必要时在证据范围绑定 `reviewed_mechanism_matches_path`，全核后按重新计算的每错点接触层选题。

总量上限 30。先尽量为每个错点选一道合格代表，再为需要区分不同操作或边界的错点选第二道。`selection_targets` 只列每题实际采用的错点，不用拆分同一断点绕过每点上限。`unrepresented_target_reasons` 以未被任何题实际代表的 `target_id` 为键逐点给原因，只有兼带、补讲也照实写。没有可用原图或解析的候选保留来源缺口，能预处理的题参加接触层比较。非空交付核对同版真实题面、解析、精确教师稿和适用阶段图解，打开实际热队列的 `START_STUDY.md` 路径做文件校验，留待用户开始。没有实际错点时直接交零题原因；已有明确错点但查无合格题时可走空选回执。

## 常规D

新常规到期 D 从本机原生调度器取得完整到期、请求和对应版本，从同版本机正式卡、原卷目录、独立小问、讲题视图与真实接触记录取料。全范围作为对账分母，最终代表和未选 carry-over 沿原规则。新本地输入的 `source_basis=native_due_sources_v1`，由本机源文件和原生教学指纹绑定。只有用户明确要求远程访问或外发才发布。旧输入按原绑定继续读取。

1. 在408主仓运行 python3 scripts/runtime_paths_408.py --run export_morning_input_408.py --date YYYY-MM-DD。新常规输入是本地目录。已明确的有界原始证据可加 --preparation-context /绝对路径/context.json，保留其学习日期、指定年份、证据文件与SHA。读取LOCAL_AUTHORING_READY的workspace/content_root，不把该中间状态交给用户当完成。
2. 本地核读workspace内的selection-contract、candidate-pool、建议与当日年度排除，结合content_root内concepts、候选及材料。对必要的父点/KSP和精确题教案用本机原生同版查询。用户明确的待练点、14天展示/接触、高频、真实原题、至少所登记的Pxx限额、前置依赖、唯一题面和每个主点至多一题仍生效。先审全局，再只备最终入选的真题。
3. 运行 `local_morning_authoring_408.py --workspace <workspace>`，需要改变建议时给同版 `--selection`。写实质教师稿，并预制每题完整图解。可直接复用同版、已核的精确课件，编译器可在备题时将已核原图或精确场景生成成品。发现题源、机制或图解缺项时，用返回的 `visual_override_dir` 为本题补 `solution.html`；需要一级提示图时独立写 `hint-1.html`。两者为同题可直接展示的 HTML 片段，逐阶段核对源图及可点步骤，提示不连通完整答案；不能只填写 interactive 描述或三维入口。保留部分成稿，重跑同一命令补齐。
4. 把编译出的 `prepared_source_dir` 交 `prepare_d_review_408.py --local-dir <prepared_source_dir> --date YYYY-MM-DD`。核对 `native_readback.status=VERIFIED_CURRENT`、`prepared_visual_question_count` 等于最终题数、`PREPARED_NOT_STARTED` 和实际 `START_STUDY.md`。导入重查到期与请求版本、14 天、保留年和图源哈希。启动时再核精确题面与实际学习日。protected 只读题面，获准提示读取对应提示成品，完整解析只在获准的 direct/L5 提取。备题与验回不产生曝光或学习写入。

## 非年度独立专项及已冻结旧合同

非年度局部专题或明确复用旧版独立专项合同时，可以独立于原生到期集合组织专题。常规D不足目标数量不能把专项标为D导入。逐点返回直测、部分测到、只作答后讲解等真实覆盖，用户要求“补讲”只授权补讲。单纯同码不构成直接考查。

1. 本地根据原话/原附件创建已有cs408-preparation-evidence-v1的context，采用selection_scope和focus_records；每个窄机制用稳定target_id，附真题identity、日期、真实source_path和SHA。明确限定的排除年份进入该scope。调用 python3 scripts/runtime_paths_408.py --run local_focused_training_408.py sources --date YYYY-MM-DD --context <context.json>。输出真实合格候选、题源和候选目标的私有workspace。范围与题源不足就具体报告，不暗自新增无关题或放宽年度/14天。
2. 当前会话核读池中的私有题干、同版原图和解析、最新正式个人概况。按原题审查与上级/下级纲内依据编写逐题主线和题后适量扩展；只安排真实有关的题。生成draft JSON（input_version=workspace目录名，items按题序），每题填true_item_id、source_check、mainline、rationale、local_semantic_review、visual_review、hint_safety_review、question_image_sha256、solution_image_sha256、coverage，以及hint_2d/solution_2d两个相对于draft目录的独立自包含交互HTML路径和各自的*_sha256。coverage逐项填target_id、mode、basis，direct必须逐W命中本题实际候选。partial/teaching_only另填实际teaching和visual_step，指向solution HTML的具体控件ID或有data-review-step编号的交互按钮。跨规范点补讲还要填relation_basis，说明本题对象与该W的真实纲内关系。用现有精确题2D及render_interactive_408、原生三维课件，提示/解答物理隔离，private教师源码不能放入学生DOM。重新读回题号、来源、教学语义、实物交互、日期和阶段，确实通过才写审查结论。
3. python3 scripts/runtime_paths_408.py --run local_focused_training_408.py prepare --workspace <workspace> --draft <实际完整draft.json> --date YYYY-MM-DD。在本机learning-deliveries/专项生成未开始队列、START_STUDY、逐题教师正文、2D与3D调用依据，检查它们实际存在。只有用户要求开始才按入口单题展示；每题展示前 python3 scripts/runtime_paths_408.py --run local_focused_training_408.py next --queue <热队列目录> --ordinal <序号> 会重验当天年度/14天；恢复已经展示的当前题应沿原会话。准备不计曝光/FSRS。
4. 已有专项冻结合同仅在恢复原队列时沿用。明确要求按当前年度规则重审2012卷或其他年度时，新建年度代表集合，复用同题合格材料，保全旧冻结身份与学习进度。

年度代表题若原先因多个W被采用，实际开题前逐一重验原采用W。任何主因日期层变化，只挂起该题重新核对；仍可因其他合法W采用时需把已失效的旧主因改记兼带，不能将随后真实作答评价为对失效主因的独立复测。
