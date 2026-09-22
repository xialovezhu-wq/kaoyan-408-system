# D 晨间知识点需求合同

题级 FSRS、完整 due 集合及 carry-over 保持原权威。`concept_review_priority_408` 只读取封存 B/D 概念会话与已验证正式点观测，公开安全证据；错误和待验证自原学习日 D1 加入优先级，无题级状态写入。新版点证据改变 `concept_priority_version` 和 native `run_sha256`，旧 D 结果导入及重复导入都实时验证版本、题源和产物。

`due` 保留 `due_version/due_concept_ids/due_concepts/native_due_run`，另增 `concept_priority_version`、`concept_requests`、`review_concept_ids`、`review_concepts`。新目标集合为完整主点 due 并上真实新请求；即使关联原题未到期或该概念为副点也保留需求。`concept_requests` 含 request_id、concept_id、priority_rank、reason、request_due_date、candidate_formal_node_ids、evidence、latest_outcome。evidence 保留 source_id/hash/fragment/locator、日期、结果、安全摘要和提示依赖，不含私有对话。

D 输入存在新请求时必须原样回传这四项（review_concepts可省）。`selected_items`仍只准主点due代表；其他同点due全列suppressed，保持未答carry-over。`supplemental_items`为独立点级补充，每项含 request_id、question_id（只作来源）、review_target_concept_ids（单一目标点）、suppressed_same_concept_question_ids=[]、true_item_id、surface_sha256，以及原 selected_items 的来源/题面/解析文件字段。本地重新选择已验收且 main_knowledge 精确匹配目标点的V2/V3，拒绝网页自行声明验收。候选原题是否due不影响其来源身份，不把补充插入due_items。

`coverage.covered_concept_ids` 与 `unresolved_concept_ids` 不重叠且合并等于 review_concept_ids；缺题保留 reason，按既有变式生成/独立审核补齐。无完整覆盖不推进导入pointer、不声明交付。

独立补充由 `d_concept_supplement_408.py display` 当日重新验证后显示；已存在active/paused campaign或legacy时先恢复，不能插题。实际display才记录独立曝光，后续7天门禁读取它。整场真实原话由 `commit` 包装既有概念会话writer；question_ref=request_id，related_formal_ids仅来源题，自动保留显示回执原件。无Capture、无题级评分、无原题复做/FSRS写入。未作答节点不能因知识点局部正确而自动完成。
