# 知识点复盘会话

`concept-review-session-v1` 是结束后一次提交的真实复盘事实合同。生产原文在 `~/.codex/kaoyan-408-conversation-packages/concept-review/sessions/`；测试仓库只用各自 `.private/concept-review/`。单场不可变目录及 `session.json` 为权威，没有第二本账本。全部验证、复制附件、fsync 后原子 rename。相同 session_id 相同完整内容返回 duplicate，内容冲突拒绝。提交后派生失败返回 committed/duplicate + pending，重试只补派生。

必填字段：schema、subject=`408`、session_id、study_date（上海学习日期）、started_at/ended_at（含时区）、conversation、artifacts、observations。结束时间必须不早于开始，开始的上海日期必须匹配 study_date，不接受未来事实。

网页加强包的原始用户时间未知时，本地接收器可附 `time_quality=recorded_bounds_not_user_timestamps`，将真实展示/封存记录的时间作为记录边界，study_date仍来自已验证Capture；这不是用户的精确思考起止时间，允许稍后封存而与学习日期不同。该类公开知识点历史仅保留日精度，event_time=null；普通旧会话仍按原session_interval规则验证。

conversation 是完整原话数组 `{role: user|assistant|system, text}`；artifacts 是本地文件路径或 `{path, sha256?, role?}`，封存副本保留文件名、扩展、角色、hash。提交入口只能接收已结束整场；进行中不调用，不产生 Capture。输入完整性只能由调用方确认，程序验证实际引用及角色，不宣称能从结构证明缺失对话不存在。

每个 observation 包含 observation_id、concept_key、question_ref、outcome、error_detail、user_turn_indices、hint_turn_indices、related_formal_ids；可选 mechanism（事件描述，不进入实体主键）。concept_key 必须来自现有知识点标签表叶子码，related_formal_ids 必须已有且属于该知识点。question_ref 是包内复盘题号，不创建正式题。

outcome 为 wrong、independent_correct、corrected_after_hint、unresolved。每项必须引用真实 user 原话下标（从 0 开始）；提示引用 assistant，corrected_after_hint 要有提示后的用户回答，independent_correct 不得有提示引用。错误详情只记录可由原话支持的局部判断，程序不替代语义判断。若首答错误后被纠正，拆成 wrong 与 corrected_after_hint 两个观测，分别引用前后原话；不得由有提示自动推断曾经错误。

公开派生只含经过既有安全过滤的理解摘要、结果、source hash 和点级关联，原话与原件留在私有目录。无法安全输出的 error_detail 使用结果标签代替。unresolved 不计错误；只有明确 wrong 计错误，同一 session_id + concept_key 最多一次。观测原件全部保留。页面与查询均标注新封存会话计数，旧证据/关联题不加入次数。

`知识点/索引.md` 与 `知识点/<code>.md` 为可重建入口。现有 `knowledge_history_sources_408` 读取封存源，现有 `build_knowledge_memory` 和 `exam_tutoring_context_408` 聚合，再沿 `study_personalization_408` 导出。提交后使用既有 `complete_after_commit` 刷新与共享发布。禁止修改原题日期、次数、调度状态，禁止 Capture、新题、模拟 campaign。

运行：`python3 scripts/concept_review_408.py commit --input /path/session.json`；只重建知识点结构：`python3 scripts/concept_review_408.py materialize`。

历史重开只校验封存身份、hash与证据合同，不以当前题目是否仍存在或保持旧分类否定历史点级事实。当前提交才验证现有canonical membership。原生查询与portable副本都保留 `cs408-concept-review://<session-key-hash>`、source_available与 `source_access=local_private_only`；公开快照不含原文。本机用 `read-source --source-id CR408-<session-key-hash>` 重开已验证原件。普通 refresh-current / refresh-nightly 同步重建知识点入口，后续正式入库无需另手工维护membership。

若原始外部附件已经移动，使用 `recover --source-id CR408-<session-key-hash>` 重开封存副本并补派生/发布，无需再次提交输入或复写会话。
