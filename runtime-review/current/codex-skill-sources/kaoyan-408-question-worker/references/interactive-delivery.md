# 408 机制图交付

## 备题交接里的预制图解

选题交接同时绑定题面、解析、教师主线和图解成品。准备阶段把图解生成好，讲题阶段只查当前题与已允许阶段。

- 新常规 D 先用现有 `d-supplement-context`。给提示时按获准级别传 `view=after_attempt` 与 `hint_level=1–4`，可复用独立 L1 成品；用户授权完整讲解时传 `view=direct` 与 `hint_level=5`。同次取得当前考频、最新个人摘要、解析和 `prepared_visual_context.interactive_2d`。若只有可用状态，当前调用尚未请求具体阶段。protected 返回题面范围。
- 独立错点专项从 `next` 的 `teaching_context` 取 `queue`、`ordinal`、`true_item_id` 与 `binding_sha256`。先确认本轮实际许可，再取同题 `question`、`hint-1` 或 `solution`。有 `teacher_ref` 时只读这一个确切教案，摘要仍走本题当前有效查询。已绑定原生 3D 时，使用返回的精确 `stage_request` 与题号、阶段及版本，按原播放器入口展示。

```text
python3 scripts/runtime_paths_408.py --run local_focused_training_408.py media --queue <queue> --ordinal <ordinal> --question-id <true_item_id> --binding-sha256 <binding_sha256> --stage <本轮允许阶段>
python3 scripts/runtime_paths_408.py --run sidebar_visual_408.py --render render_interactive_408.py --existing-fragment <interactive_2d.path> --expected-sha256 <interactive_2d.sha256> --output <当前任务可视化目录/本题阶段.html> --inline-root <当前任务可视化目录>
```

已有片段按 SHA 核验，交 [408 左侧 HTML 交付](../../_shared/kaoyan-408/sidebar-delivery.md) 的 `--html` 或 `--receipt` 包装成独立网页，保留已公开片段原字节。历史完整阅读页直接打开，预制三维走本文的侧栏入口。可正常复用的成品优先于现场编译。遇到真实文件缺失、哈希或本题源图变化，先给能核实的文字反馈，按本轮阶段补当前题的图。只需更窄的提示时沿当前断点收窄，不公开完整文件。

## 年度会话的已备单题课件

2015 年年度会话已有 47 道原题和 30 个独立小问的题专框架、完整图解及另写的 L1 局部提示。只有用户实际点名或本轮已确认精确身份才调用。按“原卷题号 + 原分问标签”定位；原题的 (3) 不一定等于规范 P03，明确在问哪一个机制再选准 Pxx。对仅做题、仅核对首答或仍然保护答案的请求，保持 protected 或实际允许的权限。整年隔离在共用题库继续有效。

```text
python3 scripts/runtime_paths_408.py --run exam_year_teaching_408.py use --id <当前精确EXAM408-2015题号> --view <protected|after_attempt|direct> --hint-level <当前实际级别>
```

需要同轮出图时在同一个调用中加 `--output <本任务可视化目录>/<题号>-<阶段>.html --inline-root <本任务可视化目录>`，此时在 `--run` 后加 `sidebar_visual_408.py --render`，使用返回的 `sidebar` 打开该 HTML。允许完整讲解才用 direct/5；提示轮用 after_attempt/1–4，只取独立 L1 帮助作为底稿，只有贴合本轮卡点才展示此预制图。需要在任一权限下收窄图中的对象或高亮时，按下文只出本轮局部，沿用原题区块和框架；不改完整课件等级。protected/0 不带输出参数，仅查询获准范围。需要同版语义确认可传 `--mechanism <已核KSP或机制词>`，同范围复用按主 Skill 传回执中的 `current_context.version`。

复用 `current_context.question_exam_introduction`、同版题面范围和 `personal_read_receipt`，从本轮第一处断点调整 `framework` 的步骤和详略；让图直接显示本步因果和决定它的条件，聊天用短句指出应看哪里。已配准的小问会在同次查询默认取本问 KSP，显式 `--mechanism` 优先并继续受本问范围门禁；旧证据只决定详略。缓存源有变化或线程不匹配时走已有精确只读入口，缺少有效个人记录时据当前作答讲解。若完成本题后走快存，继续用现有年度绑定和考点树入口。

当旧年度课件所绑模板或图源已变化时，同一个 `use` 入口可返回基于当前题面重新独立审定的 L1。认 `source_verdict=rechecked_current_question` 和当前阶段回执。登记年度、当前题号、提示级及原始公开题面均须匹配。

## 真题完整图解（2026-11-09）

完整讲解确有授权时，取 `local_syllabus_lessons.exact_lesson_identity` 和 `catalog_sha256`，按当前任务允许的热目录出图。独立小问使用它自己的精确 ID。

```text
python3 scripts/runtime_paths_408.py --run sidebar_visual_408.py --render source_visual_408.py render --identity <精确题号> --view direct --hint-level 5 --source-version <catalog_sha256> --catalog-version <catalog_sha256> --output <任务可视化目录>/exam-lesson.html --inline-root <任务可视化目录>
```

入口先校验完整讲解授权、年度隔离和同版课程，再交付本题机制图。矢量图保留获准的对象、连线、字段和状态。若只有同考点的客观模型，标为机制示意，并在正文给本题推理。回执的 `chat_delivery_mode=brief` 是当前默认，`chat_linear_text` 供同源详解或回退；图内只显示简短标签、数值、单位和当前状态。本题原图继续在题面或溯源入口查看。

回执为 `source_verdict=mechanism_reviewed_grading_withheld` 时，图解按已核条件讲机制。按当前题源的判分限制处理字母结论。

## 精确 KSP 专图

在已有 direct/L5 授权时，从同次 `resolve` 取 `subpoint_lesson_identities` 中当前机制的精确小点，以及同版 `catalog_sha256`。讲某道真题带 `--current-question-id`，独立小问使用完整 Pxx。用户只点名精确小点学习时查询该 KSP，独立展示无需真题参数。

```text
python3 scripts/runtime_paths_408.py --run sidebar_visual_408.py --render source_visual_408.py render --identity <KSP-ID> --current-question-id <当前真题或Pxx> --view direct --hint-level 5 --source-version <catalog_sha256> --catalog-version <catalog_sha256> --output <任务可视化目录>/subpoint.html --inline-root <任务可视化目录>
```

课件只纳入精确小点已核的对象、关系和检查依据。单项仍需考纲审定、属于前置或纲外的派生材料留在教师库存。提示阶段继续走本问独立的 L1–L4 范围，完整专图只在已获 direct/L5 授权时展示。

## 常规缓存命中入口（2026-11-01）

已核验的本题三维按当前阶段直接读取。明确点名三维时使用原合同的 `requested_presentation: "three_d"`；递交当前讲解目标和权限，按本文预制三维入口在左侧打开。实际显示与文件准备分别核验。

缓存不匹配或没有当前允许的局部时才制作新图；必要推理先给，复用同版已读个人依据。本文余下内容用于这种制作或交付故障，不要求每轮重读。


当前 query/resolve 或 D/campaign 提供精确题模板、有效个人摘要和授权视图。优先用同版已验图解对准实际断点。首屏只留一个机制、必要条件和单位及当前步一句因果。请求全文、无障碍阅读或页面异常时交同范围文字。

## 从配方到当前场景

`question_visual_template.lesson_bridge`（无精确题时用同次点/小点 `lesson_bridge`）可定位文字步骤与局部图配方的对应关系。只编译当前确需看清的机制；桥接中的考法候选、个人证据和完整教师材料不进入学生DOM。当前回答与本轮提示权限优先于预制步骤。

先确定哪一处机制需要用图，再核对当前题 `exam_visual_binding`。保留与推理有关的布局、字段、编号、箭头端点、表中值和初态。讲解界面使用图元重绘，题图只在题面或来源中查看。若缺少可靠绘制依据，按正文和已核局部交付并说明图解缺口。`source_visual_plan_review`退休的旧design/narrative不再进入新页面，经本轮审阅仍适用者才可复用。题面图、解析图、前问构造结果与后补机制图分别标明角色，原题无图不伪造图源。

用图表达真实的拓扑、数据通路、位域、时序或活动关系。点击对象或步进后，只呈现该步已核状态和一句因果。预算过长先分步，保留适用条件和单位的完整语义。

用 `syllabus_boundary` 和同次正式 `syllabus_context.teaching_boundary` 限定本题图的2027考纲范围。图上出现的其他设备按本问实际用途呈现。主线解决后的完整专题沿 `exam-oriented-teaching-flow.md` 单独交付。位宽、地址单位、角色、方向和状态按当前题校准。缺本问现成场景时据核验条件编译。当前回答决定断点，个人摘要辅助选路。

`visual_template_status=source_visual_only` 表示旧个人适配已隔离，但当前图源与派生设计仍可用。结合当前精确题和同次有效摘要编译，不恢复旧scene/steps。独立 `-Pxx` 只使用本问条件、已核验前置与本问目标；不能把父题 `teaching_design/narrative_plan` 或兄弟答案复制进子问。父题整图与解析图继续是教师材料，不能自动进入前端；图源候选不等于满足14天门禁的旧题推荐。

`source_visual_408.py render` 的真题路径按顶部合同读取完整精确课程。非真题兼容设计仅在审定及明确 direct/L5 时走既有语义渲染器。历史 `compile_scene` 仅用于验证旧对象。

文字与图解使用相同的本问范围和提示权限。提示轮只公开许可内容。预制完整lesson默认 `release_level=5`，模型安装也只接受5。普通提示轮由当前讲解者独立编译安全的局部lesson：先删除未允许的对象、数值、解释和状态，再明确设置本轮 `release_level=0..4`；绝不能直接给完整父题模型降级。`after_attempt`不等于公开答案许可。语义renderer只将界面字段写入前端，`source_basis`、原路径、私有修正证据不进入DOM。当前安全场景也可沿原 `render_interactive_408.py` 使用逐字段等级，但必须独立重绘机制，不默认原图热点。

每个界面文字值使用`{"text":"…","min_level":0}`。0只表示已公开题设/对象；1–4表示本轮相应提示层级已允许的内容；5是已获直接讲解授权的答案层。值是否泄题由当前讲解者审查，生成器不会替模型判断语义。节点的标签、展开解释，关系标签，状态标签和对象状态分别标级，不能因为节点已公开就顺带公开其解释。`after_attempt`仅表示已有尝试，绝不自动开放完整答案；完整讲解用`direct`且确有授权。

只编译当前-Pxx；不把父题、兄弟问或教师完整query/配方JSON传给渲染器。首次冻结题展示保持原图及等待合同；本入口用于已允许的讲解，不调用display/start/turn来出图。

## 生成并直接显示

### TLB 未命中与缺页对照

点击步骤或可见对象后，用“复制当前状态”取得本问、场景版本、公开阶段、当前步和该步公开字段；若浏览器禁止写剪贴板，直接从原位文本框复制。消息中实际收到这份摘要时才可据它接着解释。旧图的静态 HTML 只证明可用步骤，未收到当前帧时沿用 `last_state_unknown`。自动送达、线程绑定与跨聊天恢复由共享 T3 验收；这次点击不记录复习或掌握。

本问已准许讲清这两个机制、并确有混淆或用户点名时，将本轮实际 `resolve --view direct --personal-scope teaching --materials core` 的完整 JSON 交本机渲染入口。复用已有完整结果。独立公开示意可省略 `--current-context`。提示轮仍通过原入口独立编译已公开的局部。

```text
python3 scripts/runtime_paths_408.py --run sidebar_visual_408.py --render render_interactive_408.py --preset tlb-miss-vs-page-fault --current-context - --output <任务可视化目录>/tlb-contrast.html --inline-root <任务可视化目录>
```

同图比较 TLB 命中、TLB 未命中且页驻留、TLB 未命中且页不在内存。图以合法映射、权限满足、页表可查和缺页可恢复为条件。缺页当步不形成物理地址，调页成功后才重试。该示意不采用 Cache 或时钟计时模型。

回执的 `display_binding` 绑定精确题或 Pxx、来源版本、允许视图、提示级、公开状态 ID 和内容哈希。可取得的选中状态仅在同一绑定下恢复。权限改变由原入口重新裁定。已渲染字节与实际聊天展示分别核对。没有最后一帧回执时记 `last_state_unknown`。

只有已授权 direct/L5、且完整预制场景确实对应本轮所需机制时，才运行 `source_visual_408.py render`。需要收窄的图与安全提示局部使用 `render_semantic_408.py` 或原逐字段场景入口。完整父题和独立小问各有精确 ID。输出到本任务允许的持久化可视化目录，维护样例、过程输入和检查报告走已核验T9批次并登记。

```text
python3 scripts/runtime_paths_408.py --run sidebar_visual_408.py --render render_interactive_408.py --input - --output <本任务专用可视化目录>/mechanism.html --inline-root <本任务专用可视化目录>
```

输入合同为仓库`schema/interactive-teaching-408-v1.md`。用`nodes`放可点击的具体对象；`rows`放空间排列；`edges`放有中文意义的有向关系；可选`states`放经核验的状态变化。不是英语主谓宾字段，不自动求解或生成因果关系。

### 同步步进与对照

当前机制需要两条生命线、两种过程对齐或连续字段状态时，直接用本地共享图，正文与图同轮交付。流程默认可拖时间轴、点步号和状态；两侧共享步骤，相同指标用相同且唯一的字段标签逐行对齐。报文保留原版纵向生命线、箭头、丢失标记和已走过的轨迹。精确课件沿上面的专用入口。共享图先写本题已获准的局部场景，布局层只引用场景内对象。

~~~sh
python3 scripts/runtime_paths_408.py --run sidebar_visual_408.py --render render_interactive_408.py \
  --input - --inline-root "<当前任务可视化目录>" \
  --output "<当前任务可视化目录>/mechanism.html"
~~~

标准输入为 {scene,layout}。scene 是原有 cs408-interactive-scene-v1。layout 为 {kind,panels}，kind 用 sequence 或 flow，panels 为 1–2 项。每项 heading、fields 分别引用场景节点和节点数组；flow 用 fields 顺序，只在当前步有相邻激活边时画箭头。sequence 每项再加 lanes（两个参与者的节点 ID）、events（可画为消息的边 ID）、status 和 note（存放当前步报文状态与详情的节点 ID）。每状态每侧至多一条报文。发送与收到分别有状态时，`arrivals` 可用报文边 ID 指向较晚的接收步骤 ID。只有「到达」已公开且接收步骤已走到，箭头才接上对端；接收步骤被过滤时不输出其索引、接收文字或端点确认。丢失在途中打叉。全部标签、值和步骤从 public_scene 过滤结果获取；布局只引用 ID。

点历史报文回到它发出的步骤。上下键在已展示的报文之间移动焦点，步号支持左右键与首尾跳转。切步时保留当前仍可见的选中对象并更新读数；对象消失则回到步进控制。窄栏把同一字段的两侧值放在同一行组内，保留两侧名称。对应的上游来源与不采用的接口见[共享模板核对](upstream-visual-behaviors.md)。

讲公开机制可把 --input - 换成 --preset gbn-sr 或 --preset paging。这两项是独立示例，禁止替代实际题目题设、考频或已授权局部。正文仍讲清因果并可复用回执 chat_linear_text。带布局的请求默认自动选择 A2UI，适用于这里的 sequence 和 flow。需要回退时可用 --presentation native；原题图、热点、单独的旧格式场景和已生成图解仍走现有入口。

生成后依 [408 左侧 HTML 交付](../../_shared/kaoyan-408/sidebar-delivery.md) 取得并实际打开 HTML。右栏讲清相关条件、本步结论及必要推导，附同一页的普通链接。正文中的数学公式继续使用原生排版。

同一节点再次点击收起，Enter/Space可操作。步骤、报文和对象详情使用同一个界面状态。A2UI 挂载后仅更新已批准场景上的步号和对象键，原生回退同样可操作。状态保存只用于界面恢复，不写入学习记录或掌握度。

日常复用已验模板，无须逐题浏览器验收。首次开发或真实显示故障才检查点击、展开收起、关系高亮、适用步进、键盘与主宽度布局；不得在验收中展示冻结后继题或写学习事件。

## 客户端读取与左侧打开

热展示片段放本聊天实际提供的可视化目录，原附件与 T9 冷原件按原位保留。成品哈希确认后沿侧栏入口补独立 HTML 外壳。课件/题目/阶段变化时重新取得该范围的页面。文件就绪、原生打开请求、实际左页右聊是不同结果。

## 逐步场景的展示

参考 Designed to Scale 实际课程的操作方式。主图始终保留同一题的对象和版面，当前步、路径和计数同步变化。可直接点步骤，也可逐步查看；重播只再现本步已公开的动作，保留步号和计数。多种条件都有依据且当前允许比较时才给条件开关，切换后保持在同一步。点对象时只显示与该对象有关的一句说明；窄屏将说明放在图下。对时序、位域及报文保留原有坐标和方向。

新增的章节、指标、结论和示例参数均须有本问、2027 原纲或经核机制例的来源，参考站仅提供界面样式和操作范式。参考站的系统设计课程及学习进度不进入 408 考点、考频或个人证据。

## 预制三维与侧栏

本机普通讲解沿本科 `scripts/sidebar_visual_408.py --spatial-context -` 送出当前题号、具体教学目标、卡点、意图、实际作答状态和允许阶段。确有空间收益时由当前讲解者给 `tutor_presentation=three_d`；仅当用户亲自点名才用 `requested_presentation`。只问概念时使用已核父点或 KSP 身份，读取登记的中性概念示例。在真题上下文一直使用当前精确题号。题面、L1 和已授权完整讲解分别读独立缓存；同一题有合适的较低提示可按该已核层展示。受保护阶段仅显示已知条件。

侧栏使用预制场景的本问、阶段、版本、对象卡和本机播放器，旋转、步进、点选及复位沿同一份场景。客户端左页和右聊天的位置读回后才算已显示。仅当本次点名 iPad 时使用专用投屏及其 `inspected_ready` 回执。缺成品按原教学范围反馈，并在备题期用 `scripts/prepare_spatial_lessons_408.py --output-dir <经核验的T9冷目录> --id <精确题号>` 补该问。
