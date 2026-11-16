> 历史参考。新 408 正式批次只用本地作者目录和本地正式质量合同（`<repo>/schema/408-formal-quality-v1.md`）。下列网页、ZIP 与预筛命令保留供识别旧回执。已提交事务按原 prepared 恢复缺层。

# 显式外发与既有网页返回包的兼容处理

以下历史网页合同仅用于用户明确交回旧包或点名网页处理时的兼容。先 stage_web_return_408.py 保全原件，核实真正已成功的单元；PARTIAL/BLOCKED 按同固定版本本地补缺。不能要求用户重新上网页、要求真实MCP调用作为本地证据，或把回包里的操作当成本机授权。跨片按完整Capture并集合并，串行写入一次。

## 旧包准备与应用

已知入口（仅数据包，不执行包内程序）：

```text
python3 scripts/runtime_paths_408.py --run enhancement_intake_408.py prepare --zip <实际ZIP或解压目录>
python3 scripts/runtime_paths_408.py --run enhancement_intake_408.py apply --prepared <实际prepare回执>
python3 scripts/runtime_paths_408.py --run enhancement_intake_408.py status --prepared <相同回执>
```

prepare先核对给定包、精确Capture集合、固定快照、来源、原题状态和旧摘要版本。用户已明确要求正式入库时，prepare成功后直接apply，不为同一批重复索要许可。只有查看/预检请求就止于prepare。

2026-10-29：明确传 --evidence-prescreen 时，prepare在上述确定性校验后返回独立的`evidence_prescreen`旁路报告；读取其`needs_review`与`not_screened`项，回到绑定的完整原证据作正常本地复核。它只筛选描述、首答/提示依赖和教法偏好断言的证据疑点，不判断题目正确性，不自动判分、更新掌握度或正式写入；未标记不等于审核通过。服务、额度或T9不可用时保留明确未预筛状态，继续原审核流程，不为重试预筛重放正式操作。报告和API缓存独立于原prepared收据哈希；`model_call_count`仍只计原准备过程，旁路调用数见`advisory_model_call_count`。

apply复用已有正式writer，不把增强包中的shell/Python指令当作执行命令，不直接按任意路径覆盖文件。新真实题的正式ID由锁内分配，网页logical_question_id随后确定性绑定；既有ID保持原身份。

## 正式对象与计次

Capture是学习证据，不代表做错。new/redo写相应真实原题；correct_review不造错误或加错次数；concept_only/D补充只写真实知识点表现，不建变式正式节点、不改关联原题的日期/错误次数/FSRS。已有相同事件的already_applied只恢复或补充，不再算一次学习。需要用户资料的项保持具体needs_user，不能伪称完成。

新学习合同的正式判分与FSRS在本阶段一次提交，保留真实作答时间。旧流程已有正式event_id的输入要幂等复用或追加纠正，不重放。

不重新通读全库或重写已完成成稿。版本/事实冲突或网页缺项只处理受影响单元；当前用户已授权本地根据真实证据补全，不能停在“请网页重做”就结束。无法取得的必要原证据明确保留，不能沿错误前提盲写。

## 个人摘要是完成条件

2026-10-23：每次正式入库同时复核本批个人摘要与精确题讲法。网页以完整Capture、固定MCP历史、原考纲、当前题面及真题/教材为依据成稿；本地审核语义和来源后才正式更改。题源资料与个人表现分开，上传/索引/看图不证明已掌握；真题原图实际核看后保留其字段、布局、连线、方向和初态，不能套概念框。具体分工统一读取网页作者指南（`<repo>/schema/408-web-enhancement-authoring.md`）开头的闭环，不另建学情库。

新输入的`TEACHING_ALIGNMENT_REQUIREMENTS.json`与本地注册绑定；回包的`portable_question_teaching_updates.json`走精确题教案便携合同（`<repo>/schema/408-portable-question-teaching-v1.md`），每题及受影响路线明确retain/revise/source_limited。个人父点/KSP方案仍走原portable_visual与profile写者。先审源版本、精确题身份、考纲范围、具体步骤与图源角色，再由原生接收器在item/profile成功后、postcommit刷新前提交教案层；题模板和source_visual计划一起更新，不能只改其中一个。旧prepared/COMMITTED保持原回执；缺教学层明确补审，不能伪称已自动完成，也不重放题级写入或评分。

2026-10-26年度standalone回包：逐题实际核对原图与同版规范题后，待增强Capture可用`recent_contact_408.py bind-capture --allow-pending-review`登记同一source_id的已核身份；该入口只补来源、不写评分/学习，不能仅凭网页候选运行。候选canonical教案随后沿原locally_bound校验接收，原requirements不改。纯旧教案/路线版本证明独立放manifest-listed `native_teaching_version_proofs.json`，沿便携教案合同核同版已发布JSON及原生版本，不将它们伪标为exam/syllabus来满足题源角色。原始模板与带native字段的发布查询投影不同，缺字段时用固定快照plan_snapshot定位真实已发布投影，不能拿当前热对象冒充旧版本。

正式收尾用本机 `current_question_id` 验回。direct/after_attempt 带出当前模板与源图版本，protected 隐藏私有内容。父点/KSP 摘要及关键词仍按原规则验收。拓展须同时有2027考纲与已接纳真题依据，可沿明确上下级或经审核关联优先连接高频目标，并说明具体考法；小点不能回退父点关键词池，Pxx不能继承父题/兄弟答案。教案、路线或本机刷新失败沿原回执补缺层。只有用户明确要求远程访问或外发才发布。

2026-10-05：本地修复由当前Codex负责，不默认转交Claude Code。未正式提交的awaiting_web_enhancement Capture不得进入正式个人摘要的证据指纹。快照PUBLISHED_CURRENT不代替逐点语义核验；原生apply/status的personalization_readback必须证明本批父点三种讲题视图semantic_current及每个小点关键词唯一命中当前小点。PERSONALIZATION_SEMANTICS_PENDING只修摘要/路由/发布缺层，不重放入库或评分，不改指纹伪装新证据已审核。

2026-10-06：新审核概况由原生写者绑定点内语义指纹，防止节点库其他题的更新仅因整文件SHA改变而使未变摘录失效；原始SHA与来源门禁仍保留，真实新增表现、内容、日期、结果或提示状态改变仍失效。旧概况不自动迁移或重标。累计原题补充可按真实证据使用redo/correct_review/already_current，但必须保留原formal_id和冻结的最新累计原件，不能为了补充而伪造复错。

每个受影响规范知识点都要写入网页最终overview/short_summary/timeline/progress/next_check与原题引用，更新小路由和同版快照。完整成稿独立封存，机械刷新不覆盖其语义。查到not_authored/stale不能宣称已经有最新网页摘要。

复核摘要衔接时，依原生概况合同（`<repo>/schema/408-knowledge-profile-v1.md`）的“摘要与题目步骤的衔接”：完整概况、短摘要与各级next_check/plan.check保持证据和目标一致；文字路线承载本题推理，图只绑定局部抽象步骤。`lesson_bridge`及经考纲、真题双重核验的同点/上下级/关联考法是冷构建派生教师资料，不写成用户表现、已讲内容或掌握事实。

每次更新个人摘要，都复核本批真实对话中的文字主线、辅助图解及用户反应：实际展示的形式/成品、哪一步有帮助或仍不清楚、明确喜欢或不喜欢什么、尚未验证的效果。2026-10-17明确纠正采用“同一讲解，文字线性为主、可点击图解为辅”；按该点真实对象、条件、步骤和因果断点调整现有教法与短摘要，主线在正文可独立阅读，不把所有讲解堆入点击面板，也不只向各点复制同一句偏好。当前预存模板是比较起点，不证明本次展示过它。优先保留有效讲法；需要改进时在本题机制和适用范围内逐步改布局、粒度、步进或对照方式，给下一次一个可观察检查，不因一次认可固化全科风格，不为迭代强制换图、追加测评或启动后台模型。

偏好、效果与掌握分别记录。明确偏好引用用户原话；从多次反应观察到的倾向仍标为推测，保留场景；助手自评、图已生成、点击或“看懂了”不证明独立掌握。短摘要写最小可用的文字起点、必要解释桥梁、辅助图要澄清的关系及适用范围；详细反馈进入现有 `teaching_plan.visual_feedback`、完整概况与对应真实日期时间线，不另建偏好数据库。用户本轮纠正是讲解偏好证据，不能回写成旧学习日已验证的新效果；新文字或图解方案仍需后续真实作答验证。多种形式按当前机制选择，用户后续纠正优先修订此前推断，旧版本及其证据仍保留。

收到 `portable_visual_teaching_plans.json` 时按便携视觉方案合同（`<repo>/schema/408-portable-visual-teaching-v1.md`）走原生适配：核对本片Capture、完整conversation字节SHA、真实角色/回合/原话，再确定性绑定稳定本机原件；网页不填任意本机路径。旧父/小点方案须明确retain/revise/retire及理由，完整替换不得静默删掉教法或反馈。原包已归档时只按精确归档指针和同一原件哈希恢复，不扫描全盘。原始回包保留，派生适配与profile提交按原operation恢复，不重放题级apply、评分或FSRS。

正式收尾同时验回本批相关父点和小点：after_attempt/direct实际带出当前教法及反馈，protected仍隐藏；共享Study Library父点与小点都要读回，不能只检本机resolve或快照版本。版本变化后旧known_version应失效；超预算沿返回的完整证据对象读取，不把省略当不存在。没有新反馈时明确保留原判断或未验证状态，不制造偏好变化，也不把逐题/知识点客观模板的effectiveness自动改为已有效。详见 `schema/408-knowledge-profile-v1.md`。

2026-10-14便携字段接收校准：同一目标交portable后不再复制内联teaching_plan/teaching_plan_update；发现旧内联声明与已审核portable冲突时，保留原包，在派生包保全移除字段及理由，由原生适配器沿旧version或新portable生成唯一方案。同一source_ref每个ordinal只能出现一次，多段同回合引文合为连续原文quote，保留完整conversation SHA和真实role。仅修传输层，不改作答、评分或提示事实；已提交项仍不可改变。

讲题Skill用“父点代码+精确关键词”取摘要，合成时按它的用法把关（2026-09-27实践）：跨片共享点把当天各题证据按真实作答时间串成一条叙述（例如先被纠正、同晚短时保持、随后又出现新混淆），短摘要先写当前机制级断点和提示依赖，再写历史，末尾“细查小点：”独立成行、列全部小点标题及至少一个精确关键词；共享旧小点只追加一段当天合成证据，旧原文、旧证据和旧关键词逐字保留（网页有书面理由限定的泛词除外），合并后时间线按真实时间排序。apply与重绑发布后逐一实测：每个父点resolve为semantic_current且带小点目录，每个小点的每个关键词配父点代码都命中该小点；任一未命中都要修到命中才算完成。范本见 `/Volumes/T9-Data/01_408/408网页中转/日终加强输入/2026-09-26-enhancement-12-closeout/` 的 `build_merged.py`、`make_semantic_spec.py`、`rebind_profiles.py` 与 `verify_hits.py`。

正式提交后的原生complete_after_commit必须运行现有refresh_after_commit：同时重建受影响点的完整学习路径和protected/after_attempt/direct讲题短摘要，发布新source_version后才请求共享快照。讲题resolve的旧known-version必须失效；有新记录但旧网页概况未覆盖时标semantic_stale并生成当前事实摘要，不能继续返回旧掌握判断或宣称已完成正式语义收尾。学习路径、短摘要和版本读回是本批后置完成条件；失败只恢复该层，不重放正式apply。普通Capture不触发这次正式更新。

正式入库完成后，本地默认不导出网页晨间资料。仅显式请求旧式网页交接才向 apply 传 --export-web-morning 或 --morning-date；既有已实际创建的交接沿原身份恢复。本地准入预测使用独立 forecast_target_date；有已登记目标日用 apply --forecast-date YYYY-MM-DD（不会触发网页交接），否则按原学习日的次日固定。跨日恢复不按当前时钟推后，不覆盖已绑定的预测日期。正式收尾与晨间学习使用各自回执。

导出次日资料前先运行 `python3 scripts/runtime_paths_408.py --run recent_contact_408.py unbound-captures --since <本批最早学习日期>`：列出的是没有正式节点、也没登记题源的 concept_only 独立做题。晨间门禁只能靠题源绑定知道这些真题做过，所以逐条打开原图/题干，与规范真题（语料question-index或同版question_refs）核对；网页basis写明的“真题身份：EXAM408-YYYY-QNN”只作线索。核实后用同一脚本 `bind-capture --capture-id <CAP> --canonical-id <EXAM408-…> --source-commit <本批固定快照> --basis "<核对依据>"` 登记。身份核不实就不登记并报告，不猜题号。补登后若自动晨间导出已完成，重新调用export_morning_input_408.py生成同一目标日期的新包，只交付核对后的新版，不重放apply。（2026-09-29：9/23的2020-Q30因缺这一步，被当成从未做过进入次日候选池。）

分别报告正式入库、原包归档、读取维护和次日资料准备。FORMAL_COMPLETE_ARCHIVE_PENDING只补归档；FORMAL_COMPLETE_MORNING_INPUT_PENDING只补导出；READ_MAINTENANCE_PENDING保留已提交结果并完成对应维护，不能声称全部收尾已好，也不能重放正式写入。status保持只读。

## 归档与看板收尾

仅用户明确请求兼容旧网页晚间交接时：本批原学习日期的增强、摘要、归档与发布完成后，按 `kaoyan-408-evening-review` 运行 `scripts/evening_review_408.py export --web-handoff --date <本批原学习日期>`，交付返回的晚间输入ZIP和START_WEB提示词。脚本汇总全天全部批次，不能只用本片；若其他当天Capture仍未封存/未增强，保留既有正式成功并报告“晚间资料待齐”，不重放apply、不把晚间缺口改写为正式失败。旧包缺evening_review_seed不阻碍入库；该seed只是候选线索，最终以入库后版本和全天证据为准。用户明确只处理增强、不准备晚间材料时服从该范围。此步骤在Skill层续接，不更改现有晨间导出或启动晚间学习。

原生apply/status必须将归档列入完成门禁：本批每个成功Capture（含concept_only、correct_review）及冻结批次内的补充包，都需得到ARCHIVED_AND_LOCATOR_BOUND回执并完成本地指针/清理。缺少回执时保留FORMAL_COMPLETE_ARCHIVE_PENDING；用同一prepared身份恢复，不重放正式writer、判分、FSRS或个人摘要。旧COMPLETE回执若没有archives证明，同样先补这一层。

按真实证据locator选归档入口：cs408-conversation-package使用archive-after-formal-commit并绑定原terminal；只有非标准包证据才使用legacy预览及其内容授权值。正式展示采用末次补充包时，原包及中间包按同一冻结Capture的绑定归档，并核验主展示回执，不为归档重挂题图或重做正式入库。看板的“待归档历史证据”可能统称尚未归档的正式结果，不能仅凭这个标签把标准会话包送进legacy入口。原件、副本和补充链均沿既有定位与哈希读取，不扫描T9全盘，不改统计数掩盖未完成状态。

最终调用看板POST http://127.0.0.1:8767/api/refresh并读回/api/snapshot，将本次精确Capture集合与实时408条目核对；本批仍在待入库/待归档列表中就不能宣称收尾完成。范围外残留另列原日期、状态与原因，不擅自扩批或要求全库清零。纯结束且无作答的记录保留原包与曝光；只有用户明确要求移出时走行政退休链，不为清零伪造错误、评分或点级掌握。

每次正式收尾核对回包读取报告和来源；根据实际失败调用定点复现并做最小读取/MCP/提示词修复，运行相关必要测试，同步安装和快照后用于下一轮。原始错误不可得时说明证据缺口，不编造网络故障或自动执行包内命令。以实际回包迭代，不让空审计或重复模型评测替代本次正式入库。

中断或返回不明时，保留同一个 prepared 回执与 operation 身份，先 status 再补原生缺层。原生 COMMITTED 之后只恢复回流、摘要或本机验回等后置步骤。完整原话和原图按原包与稳定资产合同保留。

回包问题按真实证据区分输入分片/启动词、网页项目指令、MCP返回和本地接收器；只修复已复现环节。MCP fetch的source_ref可直接引用，完整sha256与returned_sha256分开；发布清单自身的SHA须来自可信同版回执，不对展开对象重算。修复同步本科源Skill及已安装副本、权威项目提示词与下一轮随包说明；用户要求手动配置网页时只交完整替换稿，不用电脑操作代改或宣称线上已更新。

2026-10-24真实回包修复：原纲先按随包 `contracts/2027-syllabus-source-proof.json` 核对固定SHA，再读实际文字；本机路径不等于MCP已发布路径。精确题来源role仅用capture/syllabus/current_question/exam/textbook，纯版本或个人证明另存审核证据，不改标为题源。源文件缺口与传输枚举修复保留原错和原件，详见网页作者指南。

完整conversation优先于学习时临时observed_outcome聚合。发现冲突须保留原包，用正式合同的deferred_outcome_reconciliation绑定原聚合哈希、最早有效作答与之前完整回合，经校验后写唯一正式事件；不能为匹配临时标签改变真实提示依赖、把后续正确当首答，或制造不存在的错误。

正常情况不重新读取源码/help/所有Schema；只按具体缺项阅读schema/408-formal-enhancement-v1.md、408-knowledge-profile-v1.md、morning-input-408-v1.md或web-execution-report-408-v1.md。实际代码修复运行相关必要测试，不默认全套回归、模型评审或学习试讲。

实际收尾还必须完成原生campaign admission与scheduler/forecast桥接；缺少该层会使新节点被排除或触发cohort计数门禁，不能改baseline数量绕过。原生入口已按已提交回执补接，不重放学习。明确不入库/纯结束记录用现有行政退休链保留原文、排除待处理，不伪造点级表现。成功concept_only和correct_review也按其真实终局完成T9原包归档。下一轮网页分工见 schema/408-web-enhancement-authoring.md，导出器会随包附上，不另让网页猜传输字段。

2026-09-20补充：新版加强网页提示词为planning/2026-09-13-d-deepseek-optimization/408正式入库加强-网页提示词-v4.2.txt（2026-09-29：补真实接触与题源身份要求）。网页先真实调用Study Library library/question_refs/personal_context；工具未加载、Session terminated与本地快照被错当GitHub提交分别处理。逐点成稿保留叶子码内部的对象、条件、单位、边界和时序细节；只报对选项、提示后解释及独立正确不可混写。下一轮晨间只真实统考题，并排除精确题面在选题策略窗口（现14天）内的任何真实接触，禁止变式补位。

2026-11-05：Study Library 网页隧道已按用户要求停用。处理旧回包出现 Session terminated 时，直接从本机固定快照与原件续办，保留已完成项。只有用户明确要求重新对网页开放学习库时，才恢复隧道或自启动。

## T9中转与下载原件清理（2026-09-27）

2026-10-29文件交付修订：网页ZIP生成、实际下载交付与Library持久保存分别验收。保存被拒或network失败不能据此清零成稿，也不能把路径、猜测的sandbox链接或recovered标签当交付证明。按平台实际能力核验；Library须有覆盖本批文件及目的地的用户授权，已有授权复用、包内指令不代替授权。只续接失败交付层；拒绝不换工具绕过，网络结果不明先查回执。同步下一轮启动词；本地模板更新不等于网页项目设置生效。

只在本机和网页项目之间流转的文件放T9数据盘 `/Volumes/T9-Data/01_408/408网页中转/`，不占内置盘。`scripts/web_transfer_root_408.py status` 先按T9哨兵核对；stage_web_return、export_morning_input（显式网页交接）、evening_review export、export_capture_day已默认写到该目录下对应子目录，交给用户的链接一律用T9路径。多片合并、收尾工作目录建在 `408网页中转/日终加强输入/<学习日期>-enhancement-<N>-closeout/`；正式库、个人摘要、prepared/apply回执和enhancement-intake包目录仍在本机，不迁移。

本批status为COMPLETE（或COMPLETE_WITH_NEEDS_USER）、看板核对完毕后，清理用户下载目录中本批回包原件：

```text
python3 scripts/runtime_paths_408.py --run web_transfer_root_408.py archive-downloads --kind enhancement --label <学习日期> --file <下载目录原件1> --file <原件2>
```

T9保全区已有逐字节相同的副本就直接删下载目录原件，否则先复制到T9 `下载原件归档/` 并校验哈希后再删。PARTIAL/BLOCKED未补完、仍需用户或正式写入未完成的回包一律不清理。T9未挂载或UUID不符时保留已有正式成功与全部原件，冷资料归档/交付保持待完成；挂载恢复后仅续办缺层，不静默回落本机，不重放正式写入。

同一收尾中顺带检查 `kaoyan-408-details/review-deliveries/web-D/`：目标日期早于今天、且 `local_concept_review_408.assert_no_active` 通过（无active/paused会话）的旧D学习工作区，用 `web_transfer_root_408.py move-tree --kind d-workspace --source <该目录> --relative <目录名>` 迁到T9 `D学习工作区/`。2026-09-27前的本机历史中转目录已全部迁走，旧回执路径与T9位置的前缀映射见 `kaoyan-408-details/MOVED_TO_T9.json`。

## 按需资料提取与疑义复核（原入口）

本批 PDF、截图或网页原件辨识困难时，才复用 `<bridge>/scripts/study_material_preprocess.py`：用 `<bridge>/.venv-study-mcp/bin/python` 执行，传 `--subject 408 --source 原件绝对路径 --pages 1,3 --purpose reference`，purpose按来源角色选reference/question/user_work/solution；公开网页可用 `--url`，不带浏览器凭证。先本地文字提取，扫描页/图像按需调用视觉 API；不为清楚的资料重复增加一层模型。输出原件副本、提取稿和缓存走本科已核验 T9 冷目录，来源/hash/页码保持可追溯，不修改 Capture 或正式库。

`--review auto` 是默认，仅对提取疑义页追加 Decisions；已有明确疑义可用 `--review-pages 1,3`，页码必须属于本次 `--pages`，`--review off` 只跳过此旁路。它比较原页图与提取稿，提示可能漏项、混区、角色或文字不一致；HTML/TXT 没有原页图时明确未筛查。无疑义不默认每页复核；空白/过少内容等可程序核查的情况先用代码。失败保留已提取结果与未筛查原因，不重跑成功正式写入。

默认 `protected`，`after_attempt` 也只返回元数据；只有当前任务已授权读取该内容及相应答案范围时，才用 `--view direct --allow-answer-text`。正式审核授权不扩展到未来题、兄弟小问或其他答案。机器提取、疑义复核和回包 `evidence_prescreen` 是不同的派生辅助：均不替代实际核读原图、完整真实对话与本科语义审核；未标记不是审核通过，提示后完成不是独立掌握，缓存不是新学习。只将原证据支持的结论交既有唯一正式 writer，保留原日期、身份、原评分/掌握度与幂等恢复规则。
