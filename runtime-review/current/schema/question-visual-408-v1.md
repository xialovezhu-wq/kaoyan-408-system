# 408 逐题可视化讲解模板

本层是教师备课资料，配合最新个人情况摘要使用，不生成学习、曝光、评分、FSRS或掌握度事实。知识点通用图解仍沿用 `visual-teaching-408-v1.md`；逐题模板不得用通用模板换标题代替。

## 身份与权威文件

- `.private/question-visual-408/current.json` 使用 `cs408-question-visual-index-v1`，登记 `expected_ids`、完整对象的哈希引用、已核验精确别名与导入时固定的来源哈希。
- `objects/<sha256>.json` 使用 `cs408-question-visual-template-v1`，由 `scripts/question_visual_templates_408.py validate|import` 校验并幂等导入，不改正式题库和小问注册。
- `parent_exam`、`subquestion`、`formal_question`、`chapter_question` 保持现有题目身份。仅有确实原题分问但无注册身份时使用私有 `父ID#P01`（`teaching_part`），保留原文 `source_part_label`。
- 精确别名才可共用同题模板；疑似同题保持独立。未知题、变式、兄弟小问不能回退为关联原型。

## 每题内容

模板保存本题 `given_objects`、`initial_state`、`target`、`visual_kind`、`scene`、`steps`、`solution_anchor`、`checks`、`adaptation_rules` 和实际读取的 `source_refs`。每题须实际核对题设、必要图表代码、参考机制及边界。前问给定结果在 `prerequisites` 区分原题已知与经核对的参考解补充。

`source_review_status` 为 `reviewed` 或 `source_limited`；后者明确资料或模型缺口，提供有依据的条件分支，不虚构唯一答案。`grading_policy.allowed=false` 保留题源判分限制。所有模板 `effectiveness=unverified`，条件化教学建议不能成为用户已犯错或已掌握的事实。

## 同次读取与答案保护

`exam_tutoring_context_408.py resolve --current-question-id <精确ID> --view after_attempt|direct` 同时返回顶层 `question_visual_template` 和相应个人摘要。注册真题小问直接传完整 `EXAM…-Pxx`；私有分问使用父ID加 `--question-part P01`，不把两者混为同一种编号。

父题答后的 `parts` 是无子问答案的导航目录，仅含确切身份、种类、本地编号及已有原文标签；本地 Pxx 不证明原题同号小问。`protected` 完全隐藏模板正文、目录、对象引用和题源定位，只可携带无答案的判分限制。答后读取了完整教案也仍须遵守当前提示层级。

共享 Study Library `personal_context` 接受 contains JSON 的 `current_question_id`、`current_question_part`、`formal_id`、`source_id`。默认预算16KiB；`budget_too_small` 依 `minimum_bytes` 调整，`next_cursor` 使用同一 `source_commit` 和身份续页，直至 `complete=true`。不得将缺页、预算省略当成无个人记录。

## 版本与来源变化

`refresh-current` 冷构建精确哈希路由及完整可携快照；热查询只读命中对象，不扫描全题库。题源哈希变化或丢失优先于 `known_version` 判定，该题模板撤为 `stale` 并要求重审，仍保留个人摘要与其他未变模板。冷刷新不得自动以新来源重新认证旧语义；新导入声明旧哈希则拒绝。无显式来源哈希时，导入时固定实际字节哈希，随后同样检查漂移。

模板原件和失效证据保留，热查询不泄露过期解题内容。固定发布快照绑定其当时版本，不依赖接收端存在本机题源路径。模板可读取、样例能呈现与实际教学有效是三件不同的事，后者只能由今后的真实学习证据说明。

## 个人反馈与后续版本

本层提供题设、机制和画面候选；正式入库从完整真实对话更新既有个人 `teaching_plan.visual_feedback`，按当前适用范围调整形式、布局、讲解粒度和检查动作。偏好与解释效果分别引用证据；原题模板的 `effectiveness=unverified` 不因某次喜欢、看图或提示后答对而自动升级。个人方案沿完整概况的不可变版本与显式retain/revise/retire演进，不重写过去的反馈或原日期。

当前模板引用只能证明当前备课候选。实际使用过的版本须来自对应会话及成品记录；没有记录时不反推版本。网页按 `408-portable-visual-teaching-v1.md` 返回可核验方案，本地经同一profile写者入库、刷新并读回；普通讲题继续保护答案及学习状态。

## 当前交互交付

每个逐题及独立小问模板含interaction配方，绑定当前given_objects/initial_state/scene/steps/checks，遵守 `interactive-teaching-408-v1.md`。最终同轮通过Visualize协议显示可点击、可展开并高亮真实关联的界面。原题模板是教师数据，after_attempt不等于允许全部解答；前端序列化前过滤受保护内容，不能藏进折叠面板。
