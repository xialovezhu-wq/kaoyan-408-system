# 408 网页加强包成稿分工

本指南只随明确指定外发的兼容输入提供。本地作者直接使用本地增强技能和原生数据合同。

## 快速保存、网页、正式写入与讲题使用同一闭环（2026-10-23）

快速保存封存完整真实对话、题图、附件、实际展示的成品和用户反馈，不生成正式摘要或教案成稿。用户把本批输入交网页；网页依据本批原件、固定版本的MCP个人历史、原考纲和当前题源，交个人概况与本题教案的候选修订；本地逐项审核来源和语义后，通过现有正式写者、profile写者与便携题目教案适配器串行更新，最后刷新查询并读回。网页的成稿必须经过本地审核，原生结构校验不代替语义判断。讲题Skill只使用已正式生效的当前版本。

每次正式入库都复核本批受影响的个人摘要和精确题讲法；来源、范围、原讲法仍成立时可明确retain，不为“更新”强行改写。个人事实与客观教学依据分开：原考纲、考次或助手解法不能证明用户做错；用户作答不能改变考纲或原题条件。

### 项目文件与MCP各自负责什么

- 项目固定说明保存本闭环和职责。固定项目资料保存现用合同及说明；不能用一份静态个人库附件替代当前MCP记录。每批任务的Capture集合、固定版本、日期和原件来自该批ZIP，不能写死在长期项目说明。
- 用户可以把真题PDF、考纲原文/文字版、教材等作为项目文件提供。上传文件是可供读取的资料，不等于模型已经读过；文本检索或OCR不证明已看过题图。原题有图时须实际查看原页/图块，记录图中字段、位置、标号、连线、方向与初态，再编译局部可点击图解；读取不到就保留明确来源缺口。ZIP目录不等于内部PDF已解包或已阅读。
- Study Library MCP读取本机发布的版本化只读资料。网页不会仅因项目提示词就获得本机数据库、路径或工具权限；先核实本会话实际可用工具和真实返回。`publication_target=local`时固定快照也使用`source_commit`参数，但它不是GitHub远端提交；不得改走GitHub/raw地址、localhost路径或自行换版本。
- 个人作答、提示依赖、评分历史、已审核摘要和当前教案版本以固定MCP快照为基线；本批尚未入库的真实表现以完整Capture为准。项目记忆中的旧聊天摘要或助手判断不覆盖这些原件。
- 客观题源优先用户明确指定的版本；2026卷沿已接纳的《【精解】2026年皮皮灰408真题》。原纲决定范围，精确题面决定本问条件，教材补必要定义与机制。项目上传版与MCP登记版不同须保留各自文件名、字节SHA和页码，并说明冲突；不能把上传的新版本默默当成同版登记原件。

### 同版读取与可移植来源

沿输入固定`source_commit/snapshot_id/local_version`；仅输入未固定时从真实`library(subject="408")`返回固定一次。后续`query/fetch/view_image`均使用该身份。`library.entrypoints`和真实`question_refs`给出可读路径，不猜私有路径。`personal_context`的顶层`max_bytes`范围为1024–65536，`contains`为JSON字符串，教师备课用`view="direct"`；历史固定版按实际接口使用`allow_historical=true`并保留freshness。按返回游标读取所需完整记录；超预算按明确的`minimum_bytes`或完整对象引用补读，不能将省略当作不存在。

原纲先读包内`contracts/2027-syllabus-source-proof.json`。`status=available`时，实际文字在`contracts/2027-syllabus-source.txt`，其字节SHA须与证明的`expected_text_sha256`及本片固定`syllabus_context.syllabus_source_sha256`一致；这证明文字内容与固定版相同，不证明本机`support/syllabus-2027/source.txt`是MCP已发布路径。证明中的本机catalog仅为本地来源核对，不能冒充历史快照catalog。`source_limited`表示未取得固定哈希、版本冲突、缺原件或本地字节不匹配，此时不附原纲文字，按`gaps`保留来源缺口，不用current版或未核验上传版替代。旧包没有该证明时沿原合同读取实际发布成员或可核验项目原件，不能凭此说明猜MCP路径。引用包内原纲的回包应保留原文字节、来源证明与实际相对路径，不伪造`study-library://`发布URI。

当前精确题核实后才传`current_question_id`。读取相关旧概况/全部旧小点、`syllabus_context`、`question_visual_template`及其`exam_visual_binding`。`template_native_version`、`source_visual_plan_version`和`linear_teaching_route.native_route_version`是并发校验令牌；派生`route_object`的哈希不能替代原生路线版本。已冻结输入提供的令牌直接沿用，缺字段不能对热对象自行重算或拿当前本机版本冒充历史版本。

来源至少区分本批Capture、考纲、当前精确题面、支持讲法的真题/教材。每项保留实际定位和完整字节SHA；MCP的`returned_sha256`仅是分段哈希。回包采用便携URI及`source_id`绑定，按`408-portable-question-teaching-v1.md`提交；网页不能填任意本机绝对路径。上传材料只存在于网页时，回包须携带实际引用的原件或必要原页及其来源证明，使本地能核验；只写项目文件名或“已经读过”不构成可核对来源。

`portable_question_teaching_updates.json`的`source_refs[].role`只允许`capture`、`syllabus`、`current_question`、`exam`、`textbook`五种来源角色。2026-10-24真实回包中的`previous_exact_template`、`personal_context`、`native_version_proof`、`provenance`不是合法role；纯历史模板、个人上下文和版本证明放入独立审核证据并列入manifest.files，不能机械改成`exam`或`textbook`。若确实引用含真实`syllabus_context`的同版复合对象，可以使用`syllabus`角色，但须明确定位实际引用的考纲段；该对象可同时核对native版本，版本证明本身不因此变成考纲。五种角色仍按实际来源语义使用，不为通过字段校验伪造来源。

### 三项相互对应的成稿

1. `knowledge_updates.json`与`portable_visual_teaching_plans.json`表达真实个人状态和父点/KSP教学计划。首答、已成立步骤、第一个有证据的断点、提示后变化与未验证范围应一致；保留旧证据，不从新图反造已掌握。
2. `portable_question_teaching_updates.json`表达精确题的文字主线、步骤、原图结构和每步辅助图，以及受影响点的教师路线。随包`TEACHING_ALIGNMENT_REQUIREMENTS.json`规定本批范围与前版本；新输入必须按其交回，不能靠省略文件绕过。旧已准备/已提交批次保持原合同，缺教学层由本地明确补审并补交，不重放原正式动作。
3. `PERSONALIZATION_REVIEW_REPORT.md`记录实际核过的考纲、题源页/图、旧版本、保留或修改的理由及具体变化。报告是审阅证据，不代替上述机器可接收成稿。

文字先独立讲清目标、第一动作、条件、步骤与因果；图只配合明确的局部步骤。原题有图就保留有意义的字段布局、拓扑、地址方向和初态，无原图则标明机制示意，不统一套概念框。适度拓展可沿同点条件差异、明确考纲上下级及经审核关联，目标须同时纲内且有已接纳真题具体考法依据；优先高频目标，说明为何从本点连到它以及曾考过的条件或所求。旧none_by_default不屏蔽新增合法上下级，不能凭共同大章、词相似或资料预测表推关联；小点仍只从当前机制出发，不回落无边界的父点关键词池。独立Pxx不继承父题/兄弟小问答案。完整教案是教师材料，`after_attempt`不自动开放全部答案，前端还须按当前提示层级编译。

本地完成门槛包括：真实正式结果、完整个人摘要、精确题教案与图源绑定、受影响路线、当前查询与同版快照。提交后必须按本批`current_question_id`验回direct/after_attempt的版本和绑定，protected仍隐藏；保留原有父点/KSP三视图与关键词验收。某层失败仅恢复该层；不得再次写题级事件、profile或FSRS。结构正确、文件存在、模板覆盖、浏览器渲染和实际学习效果分别报告。

2026-10-07高频优先：正式个人摘要更新先按同版personal_context.syllabus_context.exam_frequency定位高频规范点，优先核对真实首答、机制断点、提示依赖、修复条件和next_check；频次是客观考试统计，不是个人不会或掌握的证据。保持全部受影响点及原日期的完整overview、short_summary、timeline和小点证据，低频记录不删、不漏、不改写为已会；不能仅凭频次自动修改现有掌握度。 网页PERSONALIZATION_REVIEW_REPORT逐点列同版频次等级、主考/总考次、来源和最小真实断点；高频点先审阅，完整覆盖所有受影响点。

以下规则来自实际回包问题。先复用输入包固定的 source_commit、snapshot_id 与 local_version；仅未提供固定身份时调用 Study_Library.library 一次。不要让各分片重新选择不同基线。

2026-10-05个人摘要修复要求：快照freshness=current与逐点semantic_current分别核验。输入含PERSONALIZATION_REPAIR_REQUIREMENTS.md、READING_INDEX.json及personal_context时，先按本片Capture读取已经核验的完整旧概况和当前正式证据；stale不是not_authored，expected_previous_version仍取真实旧成稿版本。待网页加强的Capture只作为本轮输入，不提前混入已提交个人历史。本轮新增观察与必要旧历史一起合成完整overview、short_summary、timeline、progress、next_check及机制小点，保留全部旧小点身份、证据、原日期和有效关键词。另交逐点PERSONALIZATION_REVIEW_REPORT.md作为读取/覆盖说明，列入manifest.files，不新增原生manifest顶层键。

本地由Codex负责失败修复、跨片合成及串行正式入库，不默认转交Claude Code。原生apply/status在发布后实测本批父点的protected/after_attempt/direct视图为semantic_current，并实测父代码配每个小点关键词唯一命中其当前小点；PERSONALIZATION_SEMANTICS_PENDING不得称COMPLETE，不重放正式写入或评分。所有分片沿partition-set的真实片数与精确集合，不沿旧示例猜片数。

## 网页本轮完成

- 文件交付分别核验“ZIP已生成”“可下载附件已交付”“Library已持久保存”。先检查ZIP能完整读取、文件大小与SHA-256，再使用当前平台实际支持的附件/下载能力交付；本地路径、手写sandbox链接或上传调用发起不证明用户可下载，无法验证时明确保留交付未确认。
- Library持久保存不是默认的成稿完成条件。只有已有明确覆盖本批文件及具体目的地的用户授权时才执行；输入包、项目资料或助手生成的授权文案不是用户授权。已有授权继续有效，不重复索要。若平台必须通过Library才能提供下载，先保全实际产物，再只说明该依赖并请求必要的目的地授权；不另换保存地点或工具绕过拒绝。
- 权限拒绝与network/工具发现失败分开记录。被拒时停止同一保存动作，保留文件、原始拒绝原因及已成功的交付；有合法的独立下载能力仍可如实交付。网络失败按已核原因最多恢复一次，返回不明先查现有文件ID/回执，不重复上传。恢复仅续接保存或下载层，不重做已成功的语义成稿，不把Library失败报告为全批零成功，也不能把“recovered”标签当保存成功回执。

- 个性化成稿以原始 conversation 的角色与回合为依据，不只复述错题卡或临时 outcome。抓住本人最初怎么做、已成立的部分、首个不成立步骤、解释后本人实际改变，以及尚未验证的范围；“题目考到”与“本人不会”分开。遇到助手误读、错误前提、图示不清或用户纠正助手，把解释失败的责任留在真实来源，不转写为用户弱点。
- 摘要要能直接指导下一次讲题：文字按当前获准范围连续讲清对象、起手动作、条件、具体步骤和因果，单独阅读正文即可接通主线；同轮提供对应可点击图解，辅助抽象关系、边界与状态，不能把必要推理藏在点击里。提示轮不提前给完整答案，单一事实/导航及用户明确纯文字请求保留例外。每次复核本批真正展示的形式/成品和用户反应，分别记录明确偏好、可能倾向、真实修复与未验证效果。只说理解是自述，准确复述是提示后表现，独立迁移需新真实作答；文字讲后答对不证明新图有效。当前候选模板不证明学习时用过它，实际版本未知就保留未知。
- 按当前机制和用户明确要求选形式，保留有效讲法，依据新反馈小步调整布局、信息粒度、步进或对照；不把局部偏好变成全科唯一画法，不为迭代机械换图、加题或启动递归后台模型。short_summary/next_check保留最小形式偏好、适用范围和下次验证，详细比较与原日期留overview/timeline及现有teaching_plan.visual_feedback。助手自评、生成或点击不是用户偏好证据；喜欢与帮助、掌握分别判断。“没显示/看不清”先区分渲染或布局故障，不直接推成不喜欢该形式。
- 详细教法按随包 `408-portable-visual-teaching-v1.md` 交 `portable_visual_teaching_plans.json`，在manifest.files列为supplemental，不扩manifest顶层或直接填本机路径。旧父/小点plan复核后retain/revise/retire并给理由，保留原方案与原证据；不能因完整替换无声删除。原生接收器核验本片Capture、原会话字节SHA和精确回合，正式结果及稳定归档成功后适配到同一profile写者。拿不到必要原件时保留具体缺口，不猜来源、效果或用户偏好。
- 便携方案已负责的同一父/小点，在knowledge_updates中不再复制teaching_plan或teaching_plan_update；retain沿已核验旧version恢复，revise由portable交新plan与必要previous_plan_refs，处置字段由本地适配器统一生成。同一source_ref的turns每个ordinal唯一；同回合多段引文合成连续原文片段，不能重复ordinal或拼造引文。2026-10-14真实接收曾因重复处置、旧内联plan与便携替换冲突及重复T2定位被拒绝；这些只是传输修复，不修改原始作答。
- `READING_INDEX.lookup_required` 是本片待查清单，不是已读证明；若 `requires_fixed_source_commit` 先从真实library固定一次版本。沿同版personal_context读取相关完整旧概况、教法和候选模板，核验当前精确题后才传current_question_id；点模板为visual_template，题模板为question_visual_template。按游标读齐，不以相关原型或兄弟问替代。
- 完整读取本片原件，独立核对首答、理由、提示后改变、助手错误和实际题源；Capture 不等于错题。按日期、run_id、item_id、capture_id 共同识别题目，不能把另一天同号 MCQ 的说明套入本题。
- 逐 Capture 写完 enhancement.items、逐点观察及对应完整概况/短摘要/时间线/下一次检查；同点各题事实先在本片合并，跨片贡献标明来源。解释不确定性，但从最终个人概况中移除打包进度、未正式写入等临时流程措辞。
- 知识点概况为完整替换：核对固定旧版本的全部subpoints，复核后完整保留；确需退役的旧ID写入removed_subpoint_ids，并在removal_reasons中逐ID说明原因。不得因本片未涉及而漏掉旧细点，本地不自动合并未经复核的旧摘要；字段规则见随包个人知识点概况合同。
- 已有父/KSP的teaching_focus同样不能在完整替换中丢失或无声明改写。确认旧详略安排仍符合本轮真实证据后，在该目标写teaching_focus_update={"action":"retain","reason":"具体复核理由"}并沿用精确expected_previous_version；网页可省略旧focus正文与本机路径，由原生适配器重验同一旧对象的原件/哈希/原话后保留。未复核不自动retain；需要改变时明确交由本地显式作者修订，不能仅换状态标签或伪造本机来源。删除须retire并说明理由，整个KSP删除沿原显式退役合同。
- 按随包个人概况合同的“具体步骤的独立摘要”核对真实断点覆盖：一个可独立检查的机制对应一个小点，同机制跨题合并；保留独立完成、提示后变化和未验证范围，不以局部成功覆盖其他断点，不把旧助手猜测恢复为个人弱点。
- 已有正式 ID 用 `Study_Library.query(subject="408", domain="question_refs", ids=[formal_id], source_commit=固定版本, limit=1, max_bytes=24000)` 取得真实来源、两项 formal_state hash 和实际图像路径。不得用关联 ID、年份或“已审核”标签代替实际题面身份。
- redo 的 first_done_date 从同版 formal_nodes 的“首次做题日期”原样保留，latest_review_date 才是本次真实学习日期；不要把首次日期改成本次日期。若省略首次日期，本地只可按已核实的原节点补入，不能凭模板猜测。旧review/outcome存在不等于本Capture已正式入库，不得据此随意使用already_applied。
- 独立正确且没有实际推理断点时，调度字段使用 first_result=independent_correct、reasoning_break=false、first_break=none、first_break_provenance=not_observed；正向理由写进逐点观察，不把“正确选项”等答案措辞写进安全断点字段。过程未完整展示不能单凭这一点制造错误。
- 首答只报正确选择、理由未展示时，保留choice_result=correct与reasoning_result=not_observed；延后调度可用first_result=uncertain表示证据未定，reasoning_break=false，不把未观察到推理写成partial/diverged的真实推理错误。中性追问后的独立局部理由与定向提示后的补充分别保留；uncertain不等于用户自述犹豫。
- 已存在正式卡的 profile.question_refs 使用 `source_identity_status="verified_published"`。`verified_capture` 是待分配新题逻辑身份的证据，不能只给已有 formal_id 却省略所需 logical_question_id。关联旧题的链接不证明本次做的就是该原题。
- personal_context 的 view 仅为 protected、after_attempt、direct；408 的 max_bytes 为 1024–65536。按实际 next_cursor 分页，复用固定版本。先用 question_refs 等真实返回查路径，不能猜路径，也不能将参数错误解释为网络故障。
- 发布历史的 source_refs 使用 `study-library://<固定commit>/<实际发布路径>` 及已核实 hash；不得把裸发布路径误作回包内文件。回包自身证据使用实际相对路径或对应 enhancement-package 定位，文件必须列入 manifest.files。
- new/redo 的 display_asset_plan 要在网页完成所有附件的确定性分类：question_image、source_article_image 为 practice_safe；solution_image、explanation_image、user_work_image 为 protected；图像的 disposition 可为 promoted 或 archive_only。other_attachment 必须 protected + archive_only。逐项沿用原 manifest 的路径、角色、mime、大小和 hash，不写 public/private，不猜枚举，不因这些已给字段退回 needs_user。
- return-template 是起始模板，不是已完成结果。补全每个 Capture，核对 actions、点集、证据 hash、字段与文件闭包；不交空 items/updates 冒充成功。不要求在网页运行只能访问本机账本的正式 apply。
- 先列出真实文件再读取：输入清单在根目录 `manifest.json`，不要假设存在 `return-template/manifest.json`。Capture 的规范载荷 SHA 与 `capture.json` 文件字节 SHA 是不同字段，分别沿用输入绑定与文件清单，不互换。
- MCP fetch 返回的 `source_ref` 可直接用作来源，`sha256` 是完整原文件哈希，`returned_sha256` 只对应当前分段。`PUBLISHED_LIBRARY.json` 的哈希来自同版发布回执验证的原字节，不能对展开后的 library 对象重新序列化求哈希；该清单的固定版本 URI 与其他真实发布成员均可引用。
- 不要把整个输入原封复制到回包；保留正式语义实际引用的证据文件、执行报告与本片来源证明即可，输入原件已在本地按身份保全。不得因精简回包丢掉实际引用的补充原话或用户附件。
- 真实失败记录工具名、参数、原始错误及恢复结果。单项失败继续独立项，最终始终交成功文件和真实 PARTIAL 范围；参数/路径修正成功后才标 recovered。
- 写入正式层的文字（redo_action、安全摘要、错误入口、复做第一动作等）连否定句也不要出现泄露扫描词：正确答案、标准答案、错误答案、我的答案、错选答案、正确选项、错误选项、选项字母、完整题干、完整解析、题目图片、解析图片、手写痕迹、答案是、答案为、“应选”加字母。改用“所选选项”“结论”等中性说法。2026-09-25一句“不以选项字母……代替机制”使批次关闭时全局审计失败，只能本地改字后续接。
- question_refs 的身份字段按接收器规则写：verified_published 行若写 source_id、canonical_ids，必须等于同版 question_refs 的原值（历史转来的节点 source_id 是 HCN 号，EXAM408 编号在 canonical_ids）；verified_capture 行若写 source_id，必须等于原包 episode 的 source_id（`local-episode:…`），EXAM408 编号放 `canonical_id`，不要写进 source_id。
- 按真题编号查重（2026-09-26 回包教训）：先用随包 READING_INDEX 的 existing_formal_id。需要联网核对时，用 `query(domain="question_refs", contains="<一个精确编号，如 EXAM408-2010-Q35>")`，空结果表示同版没有已发布节点。旧版服务对 contains 返回 QUERY_TOO_LARGE，这不是体量问题：不要加大 max_bytes 或 limit 重试，记录原始错误并把查重留给本地。
- web_execution_report 的每条 failure 只用七个合同字段：sequence、call_id、tool、input（对象）、raw_error_or_response、affected_unit_ids、recovery_status。恢复经过写进 EXECUTION_REPORT.md，不要另加 recovery、error_retention 等键。affected_unit_ids 只写真实 Capture ID 或知识点代码，不写 final_package 之类的伪单元。
- enhancement.items 每项九个字段齐全；new 题的 formal_id 与 expected_formal_state 都写 null，不能省略键。
- completed_units 与 profiles 的 source_refs 只能引用本包可核验的来源：本片 Capture（`capture://本片ID`、`cs408-conversation-package://本片包`）、回包内文件、固定快照的 `study-library://` 路径。不能引用其他日期的 Capture（如 `capture://CAP-20260925-…`），那些证据改引固定快照中已审核概况对象的 study-library 路径；否则该单元会被本地判为不可信、需要补做。
- 旧小点关键词原样保留，新词只追加。可以把确有歧义的泛词改成限定词，但须在审计文件写明理由；不要为了控制词数删掉仍能唯一命中的旧词（如 TTL、生存时间、源MAC），否则讲题时按这些词查不到小点。父点短摘要里的“细查小点：”目录单独起一行，列出全部小点标题和至少一个精确关键词。
- concept_review严格只有`session_id、started_at、ended_at、observations`四个字段，session_id填null；不复制完整concept-review会话外壳中的schema、subject、study_date、conversation或artifacts，原生接收器从已核验Capture重新读取。observations[].observation_id 只用 ASCII 字母、数字和 `._:-`，以字母或数字开头、不超过100字符，同一 Capture 内不重复，建议写成 `<Capture ID>:<知识点代码>:obs-NN`；描述性文字放进 error_detail 或 mechanism。2026-09-30 的描述性编号与2026-10-17接收的多余会话外壳均是字段问题，不得据此修改真实作答。
- profile.question_refs 每项必须带 formal_id（已有正式题）或本批新题的 logical_question_id。concept_supplement 或其他 concept_only 做的真题，如果在固定快照里没有正式节点，就不写进 question_refs（没有可链接的正式题卡）；这次作答的证据照常写在 timeline 和小点 evidence 里，用 capture_id 和 EXAM408 编号标明。真题本身就是已有正式节点时，写 verified_published 行，formal_id 用该节点，可带 capture_id，并用 relationship 注明不是复做。
- 旧 timeline/progress 行和小点evidence逐字保留，包括带泄露扫描词的旧措辞；上面的泄露词规则只约束本批新写的文字。沿固定旧版本直接保留原记录，不再把每一代原记录套入新的original_entry外壳；历史来源由已核验旧版本、精确JSON位置和原SHA追溯，不为绕过单个profile的512KiB上限删除旧文本或证据。2026-09-30旧措辞改写与2026-10-17重复历史外壳膨胀均只做保真恢复。
- 2026-10-03 回包校准：`manifest.affected_points` 与 `knowledge_updates.updates` 必须恰好覆盖逐题 `knowledge_observations.points` 中 `status="observed"` 的并集。`no_observation` 的题面/课程关联不进入本次个人概况替换；可另存候选说明并保留旧概况，不能为满足集合门禁补造学习表现。
- `deferred_outcome_reconciliation.reasoning_result` 只使用原生枚举 `sound`、`diverged`、`partial`、`not_observed`。不可写 `unsound`。原始 `observed_outcome` 和最早有效首答的哈希仍沿实际原件核对，不以字段修正改写原作答或增加尝试。
- 新小点 ID 使用原生 `subpoint_key(父规范代码, title)` 的完整规范化及哈希算法；不能先算 ID 后改标题。既有 ID 原样保留。完整替换后用父代码配每个关键词核对；同父点内确有歧义的泛词可限定为具体机制词，逐项写明旧词、新词及理由，历史证据与原文字义不变。
- 小点当前 `source_refs` 也须在本包或固定快照内可核验。旧快照 URI 或更早回包路径只作原历史保留；当前引用可改指固定快照中实际验证过的完整已审核父概况，并核对其 `concept_key`、`version` 和原字节 SHA。不删改继承的 timeline、progress 或 evidence 来凑引用门禁。
- 启动时先读实际 `READING_INDEX.json` 的 `captures` 数组，不猜 `items` 键；原始 Capture 输入可能没有 `local_version`，从本次固定版真实 library/原始发布回执取得。读取原始发布清单时按实际 `file_inventory` 结构，不把它与 library 展开的 `files` 数组混称。
- 2026-10-06真实回包校准：`current_formal_point_records.records`是按record_key索引的对象，不是数组。遍历使用`records.items()`或`records.values()`；不能`records[0]`，也不能把遍历所得的字符串键当记录调用`.items()`。先核对实际类型，再做只读审阅。
- 原始会话包中的临时`observed_outcome`位于`source.json`内部的`source`对象，不假定独立`observed_outcome.json`存在。return-template只按真实文件清单读取，不猜`return-template/manifest.json`。这些路径/类型错误只修读取层，不改变真实作答或清零成功成稿。

## 本地保留的职责

增强网页另交 `evening_review_seed.json`，按随包 `contracts/evening-review-contract.md`（源合同 `schema/evening-review-408-v1.md`）记录本片可复盘的具体机制、真实首答/提示后变化、Capture与原回合，并列入原manifest.files，role为evening_review_seed。它绑定原输入版本，只是候选线索，不是入库后复盘成品。全天增强入库后由本地 `kaoyan-408-evening-review` 汇总当日精确集合和新版本，按focused-qa-v1在当前本地会话准备通常3–5题、最多5题、约10–15分钟问答；准备不开始学习。旧回包缺此可选文件不阻碍正式入库；不因此重做成功增强语义。

核对精确 Capture 集合、跨片与跨日期版本衔接，处理只有本机可验证的旧回执与行政结束，锁内分配正式 ID，走原生事务写入与幂等恢复，用本机讲题入口验回摘要、来源版本和答案权限。用户明确要求远程访问或外发时再发布副本。网页能确定的语义、来源引用和传输枚举应在网页先完成；不把缺传输字段误写成需要用户重答。

跨片同一父/小点的便携方案先按真实原话合成一个目标，保留所有相关来源及原日期，统一冲突的来源id及其引用，不能后片覆盖。合成包part_id为null，原片与part信息保留在私有来源记录；不能只改part_id假称合成。收尾须在本机与共享父/KSP讲题入口实际读回当前方案/反馈，确认protected隐藏；归档、profile或发布失败只恢复缺层，不重放题级事件。

明确跳过且不要入库的记录，只保全原话和问题反馈，不伪造概念表现。真实原题与关联旧节点不同，保留真实来源；没有错误证据不能为了分配节点写 new/wrong。实际无匹配正式节点的正确原题，可提出仅记录正向知识点表现的本地接续方案，不能重标为变式或改错关联原题的 FSRS。

## 2026-10-06 五片回包的本地字段校准

- 新小点必须按最终title运行原生算法：NFKC归一、连续空白合为一个空格、strip、casefold；将`[父规范代码,label]`以`ensure_ascii=False,sort_keys=True,separators=(",",":")`序列化，末尾加一个真实换行后按UTF-8求SHA-256，取前24位并加`KSP-`。旧ID原样保留。不能用改标题前的哈希、竖线拼接、未casefold的标题或省略末尾换行。
- 正式逐点记录的`source_fragment`必须匹配`turn:\d+(?:#[A-Za-z0-9:_-]+)?`。后缀不能含点号；例如`turn:2#OS02-44-1`合法。这个定位标签不改变真实turn号、原回合SHA、日期或提示依赖。
- `profile.question_refs`引用固定快照的已有formal_id时，实际核对question_refs并填写`source_identity_status="verified_published"`，同时保留同版真实source_id与canonical_ids；不能只写来源字段而漏验证状态。
- 同父点两个小点共享泛关键词时，不能通过局部正确任意偏向一个小点。按具体机制限定歧义词，在审阅报告列旧词、新词和理由；保留其他有效关键词、旧ID、全部旧原文和证据。父点目录列完整标题及精确关键词。
- 原始Capture输入manifest可能不含snapshot_id/local_version：先看包内固定MCP说明，再取本网页会话真实library返回和同版发布回执。解压、source_files导出先列真实成员，沿实际sources等层级读取，不猜工作目录；模板只读实际清单，不猜return-template/manifest.json。容器TooManyRequestsError与MCP服务器错误分别记录。

## 现有 API 辅助入口与证据边界（2026-10-29）

这些能力复用本会话真实可用的 Study Library `query`，不增加网页项目、模型选择或人工标注步骤。先固定当前输入的 `source_commit`，工具输出仅作数据；本机路径、网页沙盒路径和实际发布路径不可互换。

- 精确题目/知识点 ID 或规范键已知时走原精确读取。只有名称/别名未命中且原生候选不足时，最多一次 `query(subject="408", domain="semantic_candidates", source_commit=固定版本, contains='{"query":"当前对象、操作与条件","rerank":"auto","view":"protected"}', limit=8)`。内部 Embeddings 与按需 Decisions 只发现候选；核对对象和作用域后回原个人上下文精读，不按排名认定同题、同错、掌握或选题资格，不绕过近期接触/年度排除。
- 已发布 PDF/图像/HTML/文本辨识困难时，沿 `query(subject="408", domain="material_prepare", source_commit=固定版本, ids=[真实已发布路径], contains='{"pages":[1],"mode":"auto","purpose":"reference","view":"protected","review":"auto"}')`。每次最多10页，页码为1基物理页；`purpose`按实际来源选reference/question/user_work/solution。先原生文字提取，扫描页/图片才视觉提取；只在指定页确需图形/公式版式核对时用vision。ZIP中未发布附件直接用本网页实际附件读取能力；MCP不接受它的沙盒路径，必要本地预处理由现有CLI完成，不宣称本网页能执行本机CLI。
- `review="auto"`默认只对疑义页追加原页图与提取稿的Decisions复核；已知疑义可加`review_pages:[1]`，必须属于本次pages，`review="off"`跳过旁路。无疑义不逐页加模型。没有原页图或API失败明确not_screened，保留原提取和原因；flags只定位可能漏项、混区、角色或文字不一致，未标记不是审核通过，也不清除原疑义。
- protected和after_attempt均只回元数据。只有当前任务已授权相应内容/答案，才指定direct且allow_answer_text=true；不因已作答、正式编纂或私有备题就扩展到未来题、兄弟小问及学生可见层。不能为读提取稿绕过原题答案保护。
- 原图/PDF、完整真实对话、来源hash、页码、原日期和角色继续保留。提取稿与疑义提示是派生材料，不满足实际看原图、本人作答或正式掌握证据要求。回包后的evidence_prescreen仅在明确传 --evidence-prescreen 时附加，只筛个人断言与绑定证据的疑点；网页照原schema交付，不能填成审核已通过。模型结果缓存只复用同来源/版本/判据请求，不产生新学习或写入权限。失败仅补缺层，不重封Capture、重放正式写入或改冻结队列。
