# 408真题图源与逐对象讲解

本合同只生成教师素材与派生交付计划，不创建作答、曝光、评分、FSRS、个人掌握或反馈事实。

## 原图与范围

范围来自已接纳真题索引，不从考试年份或目录文件数猜题数。逐科审阅每张当前题面图，分别登记独立图块、无原图、源图缺口。树、图、表格、内存/地址字段、指令格式与波形都是有意义的图源；完整题面截图不是独立图块。旧切片截断时回到当前题所绑定的原PDF页或已核验条件图恢复，保留原件与坐标证据，不改冻结题图。

当前原图是布局、字段、符号和真实关系的约束；清晰重绘应保留这些考试识图特征，不能任意替换结构。讲解保持可选择文本。没有原生图的题，可根据已核验条件绘制机制结构，但不能标为真题原图。解析图与题设图严格分角色。

## 审阅输入

`cs408-figure-review-v1` 按四科和精确父题登记：

- `review_status=visually_reviewed` 必须有真实题图目视依据；OCR或文件存在检查不足以支持。
- `figure_status` 为 `figures_extracted`、`no_diagram` 或 `needs_review`；最后一项不得通过全量完成门禁。
- 每张 `figure` 保留输出图SHA、来源路径/SHA和实际裁剪矩形。沿用既有条件图时矩形为null，不补造PDF坐标。
- 每个 `region` 绑定图内实际对象/字段/活动，含归一化矩形、具体机制解释、明确的关联对象和提示级。不能给整张图套一个通用说明。
- `overview` 为本题整体方法或结构，点击展开具体解释，允许定位相关图内对象。序列是教学顺序，不自动等于物理因果。

## 热计划与查询

`scripts/source_visual_408.py build` 完成全量范围与来源校验后，将图块复制到details的 `corpus/408统考真题/visual-atlas/figures/`，按SHA寻址。`.private/source-visual-408/current.json` 指向不可变的逐题/逐点计划，最后原子激活。制作过程、联系表、备份与报告仍走T9；热图像与运行依赖留本机。

`cs408-source-visual-plan-v1` 包含：

- `identity/scope/status`：精确题或父点索引身份。
- `assets`：图块真实路径/SHA、来源角色和可公开范围。查询不嵌图片base64。
- `overview/region_bindings`：有语义的总览与区域展开配方。
- `syllabus_boundary`：当前2027考纲路径、scope_status及teaching_boundary；最终以同次有效syllabus_context为准。
- `effectiveness=unverified`：工程验收不代表学习有效。

源计划只在模板materialize/project_template的派生层注入。原逐题教案、个人profile及source_refs不因改展示而重新认证；旧stale身份仍stale。manifest和实现代码参与source_version，图像SHA参与source guards；源图变化使旧known_version失效。共享发布只读取已激活、SHA绑定的atlas清单，不递归猜目录。

父点候选是教师图源索引，不是通过实时14天规则的旧题推荐。KSP继承父点时仍收窄到当前小点。独立-Pxx只引用本问的condition_assets和本问步骤，不继承父题总览、图上答案或兄弟问解释。只共享已知结构的图也需要当前讲解者按本问绑定热点。

## 前端编译

`render_interactive_408.py` 的 `source_diagrams/overview` 沿交互合同：图像先通过图级提示过滤才打开；节点解释、热点和状态独立过滤；路径/SHA不交给前端，已允许的图片嵌入页面。顶部先显示整体流程或结构，下面保留原图，点击流程或图内对象后展开真实文本，突出其关联。

自动 `source_visual_408.py render` 仅接收明确 `view=direct,hint_level=5` 的已审父题图源计划。普通提示教学由当前讲解者显式选出允许公开的场景，不能把after_attempt升级为答案授权，也不能靠隐藏面板保护答案。当前没有审定图内区域的子问拒绝自动套父题渲染。

验收分别记录全部题图目视、图块提取、逐对象语义绑定、同版查询、浏览器点击和客户端实际展示。任何一层未完成都不由另一层的计数代替。


## 已审原图结构绑定（2026-10-23）

逐题 `exam_visual_binding` 使用 `cs408-exam-visual-binding-v1`，明确basis、真实source_assets及角色、需要保留的preserve结构、绑定当前Q步骤的focus_by_step；what_to_show/state_change/return_to_reasoning把局部图接回文字推理。它只供教师编译，frontend_ready=false、effectiveness=unverified；源图字节变化照常撤销旧版本。无原图、真题图、解析图、同机制参考与资料缺失分开，不把已有OCR或文件存在计作目视。

每题本轮审查同时记录 `source_visual_plan_review`。retire_old_design会在当前源计划和投影中去掉旧design/narrative/旧热点，历史内容寻址对象保留；只有来源和结构仍经核实的设计可保留或修订。图阶段不是整题步骤，图只在已审step_id对应处帮助理解。已核的具体相邻考法以条件变化和方法变化说明；无可核对照不由词匹配自动补足。
