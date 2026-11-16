# 408文字主线的交互辅助入口

## 备题交接里的预制图解

选题交接同时绑定题面、解析、教师主线和图解成品。准备阶段把图解生成好，讲题阶段只查当前题与已允许阶段。

- 新常规 D 先用现有 `d-supplement-context`。给提示时按获准级别传 `view=after_attempt` 与 `hint_level=1–4`，可复用独立 L1 成品；用户授权完整讲解时传 `view=direct` 与 `hint_level=5`。同次取得当前考频、最新个人摘要、解析和 `prepared_visual_context.interactive_2d`。若只有可用状态，当前调用尚未请求具体阶段。protected 返回题面范围。
- 独立错点专项从 `next` 的 `teaching_context` 取 `queue`、`ordinal`、`true_item_id` 与 `binding_sha256`。先确认本轮实际许可，再取同题 `question`、`hint-1` 或 `solution`。有 `teacher_ref` 时只读这一个确切教案，摘要仍走本题当前有效查询。已绑定原生 3D 时，使用返回的精确 `stage_request` 与题号、阶段及版本，按原播放器入口展示。

```text
python3 scripts/runtime_paths_408.py --run local_focused_training_408.py media --queue <queue> --ordinal <ordinal> --question-id <true_item_id> --binding-sha256 <binding_sha256> --stage <本轮允许阶段>
python3 scripts/runtime_paths_408.py --run render_interactive_408.py --existing-fragment <interactive_2d.path> --expected-sha256 <interactive_2d.sha256> --output <当前任务可视化目录/本题阶段.html> --inline-root <当前任务可视化目录>
```

对片段用返回的 `visualize_reference`，生成的展示副本与原成品必须字节一致。历史完整 HTML 或独立 3D 成品沿其现有播放器或本地打开入口。可正常复用的成品优先于现场编译。遇到真实文件缺失、哈希或本题源图变化，先给能核实的文字反馈，按本轮阶段补当前题的图。只需更窄的提示时沿当前断点收窄，不公开完整文件。

## 年度会话的已备单题课件

2015 年年度会话已有 47 道原题和 30 个独立小问的题专框架、完整图解及另写的 L1 局部提示。只有用户实际点名或本轮已确认精确身份才调用。按“原卷题号 + 原分问标签”定位；原题的 (3) 不一定等于规范 P03，明确在问哪一个机制再选准 Pxx。对仅做题、仅核对首答或仍然保护答案的请求，保持 protected 或实际允许的权限。整年隔离在共用题库继续有效。

```text
python3 scripts/runtime_paths_408.py --run exam_year_teaching_408.py use --id <当前精确EXAM408-2015题号> --view <protected|after_attempt|direct> --hint-level <当前实际级别>
```

需要同轮出图时在同一个调用中加 `--output <本任务可视化目录>/<题号>-<阶段>.html --inline-root <本任务可视化目录>`，直接使用返回的 `visual.visualize_reference`。允许完整讲解才用 direct/5；提示轮用 after_attempt/1–4，只取独立 L1 帮助作为底稿，只有贴合本轮卡点才展示此预制图。需要在任一权限下收窄图中的对象或高亮时，按下文只出本轮局部，沿用原题区块和框架；不改完整课件等级。protected/0 不带输出参数，仅查询获准范围。需要同版语义确认可传 `--mechanism <已核KSP或机制词>`，同范围复用按主 Skill 传回执中的 `current_context.version`。

复用 `current_context.question_exam_introduction`、同版题面范围和 `personal_read_receipt`，从本轮第一处断点调整 `framework` 的步骤和详略；把必要因果写进正文，不能用图代替。已配准的小问会在同次查询默认取本问 KSP，显式 `--mechanism` 优先并继续受本问范围门禁；旧证据只决定详略。缓存源有变化或线程不匹配时走已有精确只读入口，缺少有效个人记录时据当前作答讲解。若完成本题后走快存，继续用现有年度绑定和考点树入口。

## 真题完整图解（2026-11-09）

完整讲解确有授权时，取 `local_syllabus_lessons.exact_lesson_identity` 和 `catalog_sha256`，按当前任务允许的热目录出图。独立小问使用它自己的精确 ID。

```text
python3 scripts/runtime_paths_408.py --run source_visual_408.py render --identity <精确题号> --view direct --hint-level 5 --source-version <catalog_sha256> --catalog-version <catalog_sha256> --output <任务可视化目录>/exam-lesson.html --inline-root <任务可视化目录>
```

入口先校验完整讲解授权、年度隔离和同版课程。保留原图的题使用原图课件。无题图的题可自动选已核对的本题机制图，例如 2022 年第 39 题的 TCP 关闭双纵轴。该图绑定当前精确题模板、源图计划哈希与本题实考大纲条目；失配或本机共享组件缺失时使用原有精确课件。只读本题文件，不按关键词套示例。回执给出可内联引用与线性步骤草稿。正文仍按本轮实际作答写清因果。只给局部或提示时使用下方安全场景入口。

## 常规缓存命中入口（2026-11-01）

已核验的当前题3D课件优先直接调用，不为每次解释重新读Visualize全文、创建HTML或重渲染三阶段。明确3D请求使用现有统一入口的`requested_presentation: "three_d"`，同时提供当前精确题号、允许阶段、真实讲解目标；它不提升答案权限。显示成功会返回`visual_evidence_ref`，并在既有screen/evidence目录写非学习性的版本证明，快速保存按本会话/题号/消息时间自动纳入。

缓存不匹配或没有当前允许的局部时才制作新图；必要推理先给，复用同版已读个人依据。本文余下内容用于这种制作或交付故障，不要求每轮重读。


先使用当前query/resolve或D/campaign返回的精确题模板、局部模板和有效个人摘要，组织本轮聊天中的文字主线；再选其中需要看清的抽象机制制作同轮辅助图，不把全部解题步骤自动转成界面。当前真实作答决定断点，`interaction`只绑定原有语义与交付要求，不证明用户使用过界面。首次制作读取当前Visualize Skill；同上下文复用协议及已验渲染器。

## 从配方到当前场景

`question_visual_template.lesson_bridge`（无精确题时用同次点/小点 `lesson_bridge`）可定位文字步骤与局部图配方的对应关系。只编译当前确需看清的机制；桥接中的考法候选、个人证据和完整教师材料不进入学生DOM。当前回答与本轮提示权限优先于预制步骤。

先确定正文哪一步需要图，再核对当前题 `exam_visual_binding` 的实际原图。布局、字段、编号、箭头起终点、表中值与初态是解题条件，必须保留；原图只是参考背景、可以随意换成泛概念图的做法不再采用。优先按该结构清晰重绘，必要时在高清原图上精确标示当前对象和路径；不能只覆盖整图大框或整段重复文字。`source_visual_plan_review`退休的旧design/narrative不再进入新页面，经本轮审阅仍适用者才可复用。题面图、解析图、前问构造结果与后补机制图分别标明角色，原题无图不伪造图源。

先在聊天正文说明本轮问题与解题路线，按逻辑连续解释“眼前看到什么、为什么发生、改变了谁、下一步受何约束”，必要前提与步骤不依赖点击。对应图解按拓扑、数据通路、位域、时间轴或活动网的真实结构重绘；点击对象、事件、活动、字段或步骤，展开可选择的局部核对文字。图前说明要看清什么，图后明确接续推理，不让用户自行寻找逻辑顺序。口述不得图片化，不机械朗读字段；模糊原图加蓝框、概念卡大概相连、逐题重复空泛说明均不算增强。`observe/reason/takeaway` 是作者核对因果的结构，学生界面应自然连贯，不必播报这三个标签。

用 `syllabus_boundary` 和同次正式 `syllabus_context.teaching_boundary` 限定本题图的2027考纲范围。图上出现的其他设备按本问实际用途呈现。主线解决后的完整专题沿 `exam-oriented-teaching-flow.md` 单独交付。位宽、地址单位、角色、方向和状态按当前题校准。缺本问现成场景时据核验条件编译。当前回答决定断点，个人摘要辅助选路。

`visual_template_status=source_visual_only` 表示旧个人适配已隔离，但当前图源与派生设计仍可用。结合当前精确题和同次有效摘要编译，不恢复旧scene/steps。独立 `-Pxx` 只使用本问条件、已核验前置与本问目标；不能把父题 `teaching_design/narrative_plan` 或兄弟答案复制进子问。父题整图与解析图继续是教师材料，不能自动进入前端；图源候选不等于满足14天门禁的旧题推荐。

`source_visual_408.py render` 的真题路径按顶部合同读取完整精确课程。非真题兼容设计仅在审定及明确 direct/L5 时走既有语义渲染器。历史 `compile_scene` 仅用于验证旧对象。

文字与图解使用相同的本问范围和提示权限；“完整文字主线”在提示轮只指本轮获准内容，不要求提前给全解。预制完整lesson默认 `release_level=5`，模型安装也只接受5。普通提示轮由当前讲解者独立编译安全的局部lesson：先删除未允许的对象、数值、解释和状态，再明确设置本轮 `release_level=0..4`；绝不能直接给完整父题模型降级。`after_attempt`不等于公开答案许可。语义renderer只将界面字段写入前端，`source_basis`、原路径、私有修正证据不进入DOM。当前安全场景也可沿原 `render_interactive_408.py` 使用逐字段等级，但必须独立重绘机制，不默认原图热点。

每个界面文字值使用`{"text":"…","min_level":0}`。0只表示已公开题设/对象；1–4表示本轮相应提示层级已允许的内容；5是已获直接讲解授权的答案层。值是否泄题由当前讲解者审查，生成器不会替模型判断语义。节点的标签、展开解释，关系标签，状态标签和对象状态分别标级，不能因为节点已公开就顺带公开其解释。`after_attempt`仅表示已有尝试，绝不自动开放完整答案；完整讲解用`direct`且确有授权。

只编译当前-Pxx；不把父题、兄弟问或教师完整query/配方JSON传给渲染器。首次冻结题展示保持原图及等待合同；本入口用于已允许的讲解，不调用display/start/turn来出图。

## 生成并直接显示

只有已授权 direct/L5、且完整预制场景确实对应本轮所需机制时，才运行 `source_visual_408.py render`。需要收窄的图与安全提示局部使用 `render_semantic_408.py` 或原逐字段场景入口。完整父题和独立小问各有精确 ID。输出到本任务允许的持久化可视化目录，维护样例、过程输入和检查报告走已核验T9批次并登记。

```text
python3 scripts/runtime_paths_408.py --run render_interactive_408.py --input - --output <本任务专用可视化目录>/mechanism.html --inline-root <本任务专用可视化目录>
```

输入合同为仓库`schema/interactive-teaching-408-v1.md`。用`nodes`放可点击的具体对象；`rows`放空间排列；`edges`放有中文意义的有向关系；可选`states`放经核验的状态变化。不是英语主谓宾字段，不自动求解或生成因果关系。

### 同步步进与对照

当前机制需要两条生命线、两种过程对齐或连续字段状态时，直接用本地共享图，正文与图同轮交付。流程默认可拖时间轴、点步号和状态；两侧共享步骤，相同指标用相同且唯一的字段标签逐行对齐。报文保留原版纵向生命线、箭头、丢失标记和已走过的轨迹。原题截图、原图热点和精确课件沿上面的专用入口。共享图先写本题已获准的局部场景，布局层只引用场景内对象。

~~~sh
python3 scripts/runtime_paths_408.py --run render_interactive_408.py \
  --input - --inline-root "<当前任务可视化目录>" \
  --output "<当前任务可视化目录>/mechanism.html"
~~~

标准输入为 {scene,layout}。scene 是原有 cs408-interactive-scene-v1。layout 为 {kind,panels}，kind 用 sequence 或 flow，panels 为 1–2 项。每项 heading、fields 分别引用场景节点和节点数组；flow 用 fields 顺序，只在当前步有相邻激活边时画箭头。sequence 每项再加 lanes（两个参与者的节点 ID）、events（可画为消息的边 ID）、status 和 note（存放当前步报文状态与详情的节点 ID）。每状态每侧至多一条报文。发送与收到分别有状态时，`arrivals` 可用报文边 ID 指向较晚的接收步骤 ID。只有「到达」已公开且接收步骤已走到，箭头才接上对端；接收步骤被过滤时不输出其索引、接收文字或端点确认。丢失在途中打叉。全部标签、值和步骤从 public_scene 过滤结果获取；布局只引用 ID。

点历史报文回到它发出的步骤。上下键在已展示的报文之间移动焦点，步号支持左右键与首尾跳转。切步时保留当前仍可见的选中对象并更新读数；对象消失则回到步进控制。窄栏把同一字段的两侧值放在同一行组内，保留两侧名称。对应的上游来源与不采用的接口见[共享模板核对](upstream-visual-behaviors.md)。

讲公开机制可把 --input - 换成 --preset gbn-sr 或 --preset paging。这两项是独立示例，禁止替代实际题目题设、考频或已授权局部。正文仍讲清因果并可复用回执 chat_linear_text。带布局的请求默认自动选择 A2UI，适用于这里的 sequence 和 flow。需要回退时可用 --presentation native；原题图、热点、单独的旧格式场景和已生成图解仍走现有入口。

生成后在同轮文字讲解的对应位置使用当前Visualize内容引用，保持文字主线独立可读：

```text
visualize{"path":"<当前任务绝对路径>/mechanism.html"}
```

仅输出路径、源码、Markdown下载链接、PNG或静态SVG都未完成交互交付。当前协议不可用则保留已成功的文字讲解，如实标明“交互辅助未成功交付”，只修复缺失层；不得把静态降级称作成功。数学公式/推导继续在聊天原生公式中呈现，不变成图像或塞进节点。

同一节点再次点击收起，Enter/Space可操作。步骤、报文和对象详情使用同一个界面状态。A2UI 挂载后仅更新已批准场景上的步号和对象键，原生回退同样可操作。状态保存只用于界面恢复，不写入学习记录或掌握度。

日常复用已验模板，无须逐题浏览器验收。首次开发或真实显示故障才检查点击、展开收起、关系高亮、适用步进、键盘与主宽度布局；不得在验收中展示冻结后继题或写学习事件。

## 客户端读取边界（2026-10-13实际报错修复）

终端可读写不代表内联可视化可读取。当前客户端对展示文件另做任务目录白名单和非符号链接校验；T9冷目录不能直接作为Visualize引用。浏览器预览通过也不能证明这层成功。

实际在当前对话展示的副本属于热展示文件，放本任务环境明确提供的专用可视化目录，使用ASCII小写连字符文件名。从T9维护示例展示时保留冷原件，用同一渲染器的 `--existing-fragment <冷原件> --output <本任务专用可视化目录>/mechanism.html --inline-root <本任务专用可视化目录>` 建立字节一致的真实本地文件；不能用符号链接替代。过程脚本、截图和报告继续留T9。

只有inline_path_preflight_passed=true才输出生成器返回的visualize_reference；该标记是路径预检，不是客户端实测。client_display_verified不会因文件写好或浏览器成功而变true。客户端报错时先查实际读取边界，再查脚本，不自动重做题库、学习或正式入库。
