# 2026-10-17：文字主线与辅助图解（优先于下文旧展示顺序）

同一轮同时交付聊天线性正文和可点击图解。正文按题面线索、条件、动作、原因、结果与下一步依赖讲清本轮获准推理；用户不点击图也能完整读懂。图解绑定正文具体步骤，点击只补充局部对象、关系或状态，不隐藏必需理由。不得把图中正文代替聊天正文。

`interactive_teaching_408.recipe.linear_text` 是编译要求，不是完整题解。`teaching_research_408` 的1897条逐ID路线是教师备课建议：629父点、1256KSP、12考纲目标。`ordered_blocks` 保留各目标具体顺序，`visual_insertions` 是局部画法；它们仍须结合当前精确题、同次最新个人摘要和真实作答编译，不能宣称已生成1897份完整题解。42条 `needs_source` 不提升来源与个人历史结论。研究包的完整原件和manifest校验保留在T9；不写学习、掌握、评分或调度。

研究路线由 `support/teaching-research-408/current.json` 绑定到逐对象hash。查询 `direct` 返回完整教师顺序与局部图配方；`after_attempt` 只返回受限路线指针和当前边界；`protected` 不返回研究内容。精确题模板通过 `linear_teaching_route_refs` 绑定适用父点，仍不得将整份父教案或兄弟小问带到当前前端。路线中的历史来源声明只作原包出处，不替代同次正式画像。

语义场景可显式填写 `linear_steps`：每段含 `stage_id/title/text`，可选 `return_to_reasoning`。每段必须绑定本场景真实stage。旧语义场景将其已获准的stage解释按原顺序全部显示为正文；不因点击而隐藏或替换这段主线。普通交互场景使用 `linear_steps` 的每段 `id/title/text/node_ids/min_level`，其中标题、正文及图后回句均使用带 `min_level` 的文本原子；生成HTML前过滤，不把未授权文字置于折叠或隐藏DOM。

两个renderer回执提供 `chat_linear_text` 供同轮聊天正文呈现，并区分 `linear_text_visible_without_interaction`。缺少线性正文的兼容旧通用场景只能作为辅助图，不能声称完整教学交付。完整direct/L5语义教案不得改等级为提示稿，提示轮必须独立编译获准局部。页面生成、浏览器验证和客户端展示各自报告。

# 408交互式讲解合同

## 2026-10-22：同次考纲与个人讲法执行摘要

派生讲题视图的 `linear_teaching_route.current_teaching_brief` 汇合同次 `syllabus_context` 与当前有效 `teaching_plan`：保留原范围状态、范围边界、第一动作、解释策略及有范围的偏好/下一调整/效果，不生成新的学习结论。当前请求和精确题条件先于偏好，偏好先于历史备课路线；题面参数不能从历史路线继承。没有研究路线但有当前个人方案时仍返回可用输入，明确需要当前题编译。

该摘要是教师输入，不是学生场景。`protected` 不输出；个人来源变化后只保留客观考纲范围并隐藏个人内容；无方案不推断喜好。`needs_review` 与父点频次归属原样保留，不因新增讲法升级为独立考纲审定。它不保证模型自然语言已经合乎题意，仍须对正文和图的同一对象、条件、箭头落点作语义核对。

2026-10-13用户明确要求：最终交付必须有与当前机制相关的点击交互。此合同只改变408讲解交付，不改变数学公式、聊天排版、首答、学习证据、曝光、评分、FSRS或掌握度。

## 两层数据

`cs408-interaction-recipe-v1`是教师配方，附于四科模板、逐题模板及其小点/个人方案的派生视图。`semantic_sha256`绑定原场景/步骤/边界，`click_targets`逐项指定可点击区域的语义引用、展开引用与高亮引用，`step_order`保留原序。`scene_binding`要求讲解者把引用落实为真实机制对象及关系；字段引用不是因果边。父模板继承显式保留scope，实际KSP需收窄。配方覆盖不代表每项已生成独立网页；`effectiveness=unverified`不能升级。

`cs408-interactive-scene-v1`是当前讲解者已核验的场景输入。渲染器仅做严格结构校验、按级过滤和展示，不读取模板、个人库、答案或队列。前端只嵌入过滤后的允许字段，无完整教师对象、证据路径或隐藏答案。

## 场景字段

- `identity`：当前精确题/小点，演示使用明确的非学习身份。`source_version`：当前查询版本，禁止把旧资料冒充当前。
- `view`：protected、after_attempt或direct。`hint_level`：0–5；protected只允许0，after_attempt至多4，direct可到5。实际公开范围由真实授权与当前提示层级决定。
- `title`及一切文字值：`{text, min_level}`。0为已公开条件，1–4为相应提示级，5为答案级。节点标签和解释分别标级；折叠不承担保护。数值等同样包在文字值中。
- `nodes`：`[{id,label,detail?}]`，id为稳定ASCII标识，标签对应真实对象/区域。没有解释但有关系时，点击可展开本对象的关联关系。
- `rows`：由节点id组成的行数组，全部节点恰好出现一次，每行1–6个；表达空间排列，不自动推断边。
- `edges`：`[{id,from,to,label,min_level}]`，方向、条件、归属必须已有依据；边及标签均通过过滤才会交付。
- `states`可省略：`[{id,label,min_level,values:{node_id:文字值},active_nodes:[],active_edges:[]}]`。每步更新实际对象的值/状态，列表引用必须存在。`initial_state`为首屏状态id；若未获公开则选首个合法状态。不得把终值藏在未选状态里。
- `personal_status`可标当前依据状态，渲染器不据此伪造修复或掌握；有效摘要由原查询合同判断。

### 语义重绘与因果讲解（2026-10-15用户纠正，默认入口）

真题提供已核验事实与图解方法，不是照抄原扫描/板书布局的模板。默认采用 `cs408-semantic-lesson-v1`：重绘具体对象、真实关系及状态，顶部先呈现整体流程或结构；点击对象或步骤，展开可选择的因果讲解。源图、旧热点和旧口述保留为后台证据；新 `teaching_design/narrative_plan` 优先。模糊底图加蓝框、概念卡大概相连、逐题重复空泛段落不算增强。用户明确要求原图时才使用下述兼容图源场景。

- lesson 包含 `identity/subject/title/lead/scope_note/panels/stages/initial_stage/source_basis`；`source_basis`、可选 `source_notes/corrections` 仅供后台溯源，不进入前端。
- `panels` 包含 `id/title/kind/height/nodes/links`；kind 取 graph、tree、flow、fields、table、timeline、memory，height 为260–420。节点含 `id/label/x/y/w/h/shape/explanation` 和可选 value；坐标和尺寸归一化到0–1，x/y表示中心，shape为circle、rect、diamond、field、buffer。连线含 `id/from/to/label/direction/kind`，端点必须在同panel中，direction为forward、both、none，kind为data、control、dependency、association。面板、节点、连线和步骤的id在lesson内唯一。
- 节点及步骤的 `explanation` 只向界面交付 `observe/reason/takeaway` 三个自然语言段落，用于说明当前观察、原因和后果；学生界面不必机械播报字段标签。步骤含 `id/label/explanation/focus_nodes/focus_links/values`；values仅指向本lesson节点，表示当前步骤实际变化。纯结构可只选择焦点，不强加动画。
- `focus_nodes` 只指定关注对象，不推断这些对象之间经过了哪些路径；阶段连线高亮以 `focus_links` 为准。点击对象可额外突出该对象实际相邻的连线，但不能连带点亮其邻居之间的其他边，也不因此改变当前阶段或记为已执行路径。
- 预制完整lesson `release_level` 默认且只能为5，自动CLI只允许direct/L5。protected与普通after_attempt需由当前讲解者另编安全局部lesson，在写HTML前删除未允许的对象、解释、终值及状态，然后显式设置0–4；不能仅给完整模型降级。renderer拒绝超过本轮级别的整个lesson，不把未公开内容藏进面板或DOM。
- `syllabus_boundary` 从精确题权威source plan补入；同次2027 `syllabus_context.teaching_boundary`始终决定范围。可选AOE表沿原 `cs408-aoe-teacher-states-v1` 重算校验，只允许5级；事件与活动不可混同。小问不继承父题预制设计、总览或兄弟答案。
- `semantic_teaching_408.py` 校验四科图题精确全集和全部父题叙事方案后，原子更新既有source manifest中的派生对象，保留图源、SHA、reference_rebindings及学习事实。源事实或考纲边界变化时旧设计失效；来源未变才允许重建时保留。没有当前问设计就明确需要讲解者编译，不自动回退扫描热点。
- `source_visual_408.py render` 派发新 `render_semantic_408.py`；原 `compile_scene` 只保留历史验证。新两个模块、lesson.html及source manifest纳入生成与查询两端的版本校验。完整成品数量与规则/索引覆盖分别报告。

### 真题底图与顶部总览（历史兼容，仅显式查看原图）

原 v1 无图场景仍按 `rows` 展示；兼容字段不改变题面身份、考纲边界或提示授权。仅用户明确查看原图时，才把图作为阅读对象；日常机制讲解使用上方语义重绘入口。解释始终保留可选择DOM文字，不烘焙进图像。

- `overview`：`[{node_id,label:{text,min_level}}]`。教师明确给出的顶部总体流程或对象导航，节点必须存在且不重复；列表顺序本身不生成因果边、先后箭头或额外结论。标签和目标节点均公开后才进入前端。
- `source_diagrams`：最多8张，每项 `{id,image_path,sha256,alt:{text,min_level},min_level,hotspots}`。`image_path` 为绝对本地 PNG/JPEG 路径，`sha256` 为原始图像字节的 SHA-256；这两个字段仅用于后端读取核验，绝不进入前端数据。`alt` 应说明当前图的对象和来源角色，不能含尚未公开的答案。
- `hotspots`：每图最多80个，格式 `[{id,node_id,rect:[x,y,width,height],min_level,display_label?}]`。矩形以原图左上角为原点，按完整图宽/高归一化到0–1；宽高必须大于0，矩形不得越界。热点 id 在场景内唯一，`node_id` 必须绑定真实节点；不允许用空泛说明框替代逐对象语义。可选 `display_label:{text,min_level}` 只用于重复已核验的题设标签，使低分辨率原图上的编号/活动工期可读；用独立提示级过滤，并以DOM文字叠加，不改原图像素，不将口述烘焙成图片。
- 图的 `min_level`、`alt.min_level` 均通过才允许加载整图；热点再按其级别和节点可见性过滤。隐藏节点及热点不会进入 DOM。原图内像素无法通过隐藏热点保护；带未公开答案、终态或兄弟小问的整图必须提升公开级别，或在备课阶段另行取得已核验的安全原图区域。不得把完整答案图标成0级。
- `public_scene` 只校验与投影，不读取、stat或解析图像文件。`render` 仅处理投影后获准的图，核对文件类型、图像头、尺寸和 SHA-256，再嵌入字节一致的 data URI。每图最多512 KiB，宽高各至多12000像素、总计至多4000万像素；完整片段仍小于1 MiB。缺图、hash不符、类型不符或超限均明确拒绝，不回退外链、不静默换图。
- 有图时，`rows` 不再生成抢占主体的节点卡片；顶部导航及原图热区共享选择状态，点击后在图下展开该节点解释和已核验关联，相关热点同步突出。对象目录提供相同的键盘可达入口；状态步进保留。暗色模式保留原图原色，图像不反色。
- `widgetState` 只保存过滤后节点/关系 id 和步序；重新载入仍检查这些引用。点击、展开、恢复及浏览器验收均不产生理解改善或学习完成证据。

拒收未知字段（含solution_anchor、教师模板等）、无release级别、悬空边/状态、重复id、无意义交互。过滤发生在HTML/脚本序列化前，也覆盖展开内容、关系、状态和存储恢复。生成器不能识别被错误标为0的答案，讲解者必须做语义检查；结构测试不能冒称自动证明全部语义安全。

## 版本与入口

四科源目录和逐题哈希对象通过原生加载/导入入口接入；`refresh-current`重建父点、KSP、考纲叶项、个人方案交付投影及逐题查询。学习事实和正式profile对象逐字保留；在派生teaching_plan中增加interaction，不走伪学习更新。build version与生成器/渲染器/asset哈希纳入源版本，旧known_version失效。

当前讲题查询的 `question_scope` 从同版已审真题考查映射生成。精确题号使用实考规范点与大纲条目，图源的关联标签不参与个人路由。独立小问的可读小点须属于本问实考父点并与本问考纲范围相容，语义上仍要按已公开题设核对。父点未限定本问时继续拒绝读取个人原文。个人快照守卫继续校验公开场景投影合同及已登记原生模板；通用交互 bundle、输出适配脚本的变化不再使个人快照整体失效。

普通单题用resolve；ordered D用只读context；campaign复做复用learning-turn或只读context中的teaching_context；小问用精确-Pxx，禁止父/兄弟回退。protected不返回教师配方正文或对象入口；after_attempt材料仍由本轮提示级控制前端公开范围。现有active/paused和冻结题序不重建。

沿现有publisher发布同版只读快照。验收区分规则同步、全量配方覆盖、查询/缓存生效、少量代表界面的真实交互；界面测试不写学习事实，不证明教学效果。维护冷产物遵守T9 check-output/register。

紧凑个人方案使用`cs408-interaction-recipe-ref-v1`：`targets`列出本方案所有可点语义字段，点击展开该字段并突出对应场景对象；`step_order`仍为原步骤顺序。共同保护与编译规则沿本合同，不在每份小摘要重复长段规则，避免挤掉真实个人证据。正式profile原件不变。

### 2026-10-22：对话偏好与本轮讲法的派生绑定

个人方案存在已验证的 `visual_feedback` 时，教师配方增加 `personal_adaptation`。完整形式为 `cs408-personal-adaptation-binding-v1`；紧凑形式为 `cs408-personal-adaptation-ref-v1`，只保存同一方案内的字段引用、状态和绑定哈希，不重复个人摘要正文。非个人模板及没有反馈的旧个人方案保持原投影。缺少范围、效果或后续调整等必需字段的旧残缺反馈只保留原记录，不生成可执行偏好绑定；新正式方案仍须通过原生 writer 的完整证据校验。

- `scope` 限定该偏好的适用范围；`form` 是该反馈涉及的形式，二者不能自动扩为全科偏好。
- `preference.status` 保留 `explicit_preference / observed_tendency / unknown`。明确偏好在范围内使用，观察倾向仅作待核候选，未知不生成喜好结论。
- `observed_effect.support` 独立保留 `proposed_only / user_reported_helpful / prompted_repair / independent_transfer`；不得从偏好推定有效、修复或独立掌握。
- `next_adjustment` 是需结合当前题与范围裁定的教师建议，同时作用于聊天文字和辅助机制图；不是新的用户原话或学习效果。
- `next_check` 是尚未实施的检查计划，不自动提问、写入学习事件或标记完成。当前精确题、考纲边界、提示层级和用户当前明确要求优先。

`binding_sha256` 绑定反馈的范围、形式、偏好、实际效果、下一调整/检查，以及各自引用的来源 ID；有比较反馈时也绑定其参考、结论和来源 ID。个人配方的 `semantic_sha256` 同时绑定该哈希，因此实质偏好或调整变化会使旧配方失效。原生画像 writer 继续负责原始对话角色、引文、顺序与 SHA 核验；配方层不重新解释证据，不把历史“喜欢”升级为当前“有效”。

上述字段仅供教师编译，不进入学生可点击目标、状态或正文，不把原始对话路径、原话定位或偏好管理字段变成学习界面内容。教师应把建议落实为当前题的对象、箭头、步骤与文字组织，而不是展示“你的偏好是……”面板。紧凑引用只能解析到同次已通过新鲜度检查的个人方案；沿用 protected/stale 的现有拒绝门禁。

旧个人配方不含反馈绑定时，仅 `allow_legacy_policy=True` 可接纳与原投影逐字段相同的对象供重新投影；`attach()` 始终生成新绑定。已经含新绑定的配方不适用该兼容例外，偏好或其他语义漂移继续拒收。此兼容不要求重写非个人库存或正式 profile。

## 展示文件与冷产物

Visualize读取有独立任务路径白名单，不沿用终端全盘读写权限。当前对话的真实展示副本放本任务给出的本地专用可视化目录，不能直接引用T9或使用指向T9的符号链接。冷原件、过程脚本与报告保留T9。render_interactive_408.py通过--inline-root做预检并返回visualize_reference；未提供该参数只证明文件生成，不证明可内联读取。--existing-fragment可创建原件字节一致的热展示副本。client_display_verified只有真实客户端证据才可确认，不能由浏览器预览代替。


## 文字步骤与局部图的同次衔接（2026-10-23）

冷构建生成可选教师对象 `lesson_bridge`（`cs408-lesson-bridge-v1`），在父点、小点与精确题同次查询中返回。精确题的 `ordered_steps` 保留本问原步骤，图的 `auxiliary_semantic_stages` 只解释局部机制，不替代完整题解。`local_visuals` 指向本题步骤；明确源引用与 `lexical_candidate_requires_teacher_semantic_check` 分开，词项匹配只供教师核对，不证明语义关联已经审定。没有可靠绑定时保留待编译项，不按列表位置强配。

`personal_adaptation` 只由本次查询已经校验的当前个人摘要绑定；源变化后同时隔离旧个人路线。`bounded_extension` 读取同版真实 assessment focus、精确题身份及考纲范围，只提供教师候选。主线解决、提示权限允许且已核实同点直接相邻区别后，默认至多选择一条；无依据不补，独立小问排除同父题兄弟内容。它不是旧题推荐，也不是新学习记录。

protected不返回桥接；after_attempt的完整教师资料仍需按当前提示级重新编译后才可进入学生正文或前端。完整resolve保留桥接，有限预算的兼容查询先明确省略派生建议，再考虑缩减实际学习证据；省略不等于没有个人方案。图渲染器由stage拼成的说明标记为 `visual_stage_summary_only`，仍须另给本题文字推理。
## 2026-11-09 精确题图解和全纲专题

本地课程按已接纳题目与独立小问逐条编译。有题面原图优先展示来源图块和已核坐标，状态步进绑定当前题的具体解释。几何坐标只在图块字节相符且范围一致时复用。完整场景仅 direct/L5，受保护年份仍计入编制审查。独立小问使用本问条件资产，图不可独立使用时保留本问题面条件与机制示意。

无题图的精确题可在 `support/interactive-408/mechanisms/exact-scenes/<精确ID>.json` 增加已核时序或流程。正常完整讲解入口按精确身份选择，校验模板哈希、源图计划哈希、同版课程和实考考纲条目。局部提示不打开这些完整场景。独立编译的序列可声明报文边到接收状态的映射；未公开的状态与字段所暗示的高亮在序列化前移除。共享 UI 只更新已公开步号和对象键。

常规讲题主线结束且获准后，以及快存后的专题复习，正文只交代简短框架。考点树把本题和同一原纲小节内少量考点连起来，最多四个节点，首屏显示同版原纲考频。点击节点更换唯一详情区，交代该点的题面线索、起手、通用规则与条件。学生前端只序列化这几张短卡。完整考法留在课程索引。已有下一题时排除其题族，独立小问排除同父题其他材料。四科图解、全纲节点、审核考法与客户端展示分别验收。

机制图源在 `support/interactive-408/mechanisms`，四科数据由 `recap_mechanisms_408.py` 核原纲叶项、附列细项和规范点身份。父纲目用原纲父子关系。每图声明真实对象、连线或对齐阶段。精确题用已核实考点优先选择专图，同机制点可引用同版原纲图。前端仅绘制当前点的模型，图中无题解路径和冻结题信息。

