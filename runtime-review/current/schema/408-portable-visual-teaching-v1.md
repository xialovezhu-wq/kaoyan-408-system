# 408网页可视化教法便携回流

本合同连接网页成稿、原生正式profile写者与后续讲题。只传有证据的教法提案，不执行包内程序、不新增题级作答、评分、FSRS或掌握结论。个人字段沿 `408-knowledge-profile-v1.md`；客观题/点模板保持自身来源与未验证效果状态。

## 输入读取

导出包的 `READING_INDEX.json` 按本批Capture列候选点及题目身份线索。`lookup_required` 表示仍须按固定source_commit读取相关旧完整profile、父/小点教法与当前候选图解；它不表示已取到上下文。`requires_fixed_source_commit` 时先用真实library返回固定一次版本，随后保持同版。拆片只能保留本片Capture，并继承同一基线；已固定版本与新参数冲突须拒绝。

`question_identity_candidates` 只提供原始字段位置，须核对实际当前题再使用current_question_id。不能以关联原型、兄弟问或同知识点代替。完整旧profile及其版本从同版personal_context的真实对象引用取得；点模板为 `visual_template`，精确题模板为 `question_visual_template`。按返回游标读齐所需摘要。已有同版上下文可复用，不扫描全库。

实际展示的图、HTML/SVG或其他成品以及用户反应，以完整conversation和原始附件为准。当前候选模板及其SHA只证明备课版本，不证明过去展示过它。缺实际成品或版本记录时保留不确定性。

## 文件和字段

返回可选根文件 `portable_visual_teaching_plans.json`，列入原增强manifest.files，role为 `supplemental`，按真实字节填SHA和大小；不增加manifest顶层键。

根对象只含 `schema="cs408-portable-visual-teaching-plans-v1"`、`source_commit`、`study_date`、`part_id`、`items`。版本、日期与原增强包完全一致。分片填写真实part_id；本地先保全原片，再合成同日统一增强包，提交native prepare的合成文件part_id为null。最多512个目标项，文件最多16MiB；不能仅改part_id冒充已完成跨片合成。

每项必填：

| 字段 | 要求 |
|---|---|
| concept_key | 本批affected_points中的真实规范代码 |
| subpoint_id | 父点为null，小点须存在于本次完整updates |
| subpoint_title | 父点为null，小点与本次title一致 |
| summary_target | parent或subpoint，与身份一致 |
| disposition | retain、revise或retire |
| reason | 非空，最多400字符，说明保留、调整或退役依据 |
| plan | revise为完整原生教法语义对象，但不填本机source_refs；retain/retire为null |
| source_refs | 本片新原话来源数组；retain/retire为空 |
| previous_plan_refs | 可选，复用已绑定旧plan来源，见下 |

同一父/小点目标只出现一次。新方案也用revise；retain/retire必须有 `expected_previous_version` 指向的旧方案。plan沿原 `cs408-teaching-plan-v1` 的learner_entry、first_breakpoint、first_action、explanation、visual、可选avoid/check/visual_feedback。源ID用于证据引用，不根据字符串自动生成教学判断。

同一目标由本文件交付时，`knowledge_updates.json` 中该目标不再复制内联 `teaching_plan` 或 `teaching_plan_update`。retain由本地沿已核验旧version取得原方案，revise由本文件交完整替换及必要previous_plan_refs，retire由本文件显式说明；适配器统一产生原生plan与处置字段。否则旧内联retain与本轮revise，或相同action的两种reason，会触发原生冲突门禁。未由本文件处理的其他目标仍沿原个人概况合同。

新来源每项只含 `id,capture_id,conversation_locator,input_member_path,conversation_sha256,turns`。capture_id必须属于本批已核验集合；conversation_locator等于该Capture的真实原包locator；成员路径严格为 `captures/<capture_id>/packages/<实际package_id>/conversation.json`。conversation_sha256是该完整原件字节SHA，不能用Capture、package canonical或turn哈希替代。turns保留真实 `ordinal,role,quote`。不得提交本机绝对路径、临时解包路径、任意URL或本片外Capture。

同一source_ref的turns内，每个ordinal只能出现一次。需要引用同一回合的多段原话时，合成包含这些片段的一段连续原文quote；不得重复同一ordinal，也不得把分散片段拼成原回合里不存在的新句子。保留真实role与完整conversation字节SHA。

revise可用 `previous_plan_refs` 复用同target旧plan来源，每项只含 `id,previous_source_id,profile_version`；profile_version必须是本次expected_previous_version，previous_source_id必须真实存在。只引用本地核验的不可变旧profile，不让网页填任意旧path或重新声明原件哈希。retain直接复核沿用旧plan，不另附替换plan或来源。

## 摘要及语义责任

每次正式更新都审阅本轮有无可视化反馈；没有新反馈可保留既有方案或保持未验证，不强制为每个点造新方案。偏好、观测效果与掌握分别引用证据，具体规则和字段上限沿个人概况合同。短摘要同步保留本轮仍适用的最小形式偏好、范围、调整和检查，完整反馈保存在原日期overview/timeline与plan；单交附件不算完成摘要。

完整替换通过 `teaching_plan_update` 保留、修订或退役旧方案。跨片同父/小点先结合全部真实原话合成一个目标，合并时统一来源id及其所有引用，不后写覆盖、不机械拼接矛盾判断。原片、原part_id及原引用留在私有来源记录中，当前日期和已提交项不可改。

## 本地确定性适配与恢复

接收器先核对manifest、原包、完整conversation字节、真实角色/回合/引文及目标身份，生成独立适配校验；原始回包字节保留。prepare只验证拟写结果，不写个人事实或将Capture标为正式完成。

apply沿原单writer处理真实items。相关正式结果成功后复用原归档入口保全原包，再从精确归档回执核验稳定原件，绑定给profile；不能让正式方案引用随后会被清理的本机重型副本。热原件到归档原件的路径变化须由同一locator、字节SHA及原回合证明，不忽略真实来源或语义漂移。needs_user来源不能升级为已审核反馈。

旧源已归档失效时，只能由已绑定旧profile的原包身份作精确重开，并核对原conversation字节SHA后显式revise；retain不悄改旧来源路径。无法恢复就保留具体缺口，不全盘搜索，不接受网页任意替代路径。

适配结果经既有profile写者提交，再刷新相关父/KSP与共享快照。提交、归档或发布中断时沿同prepared/operation恢复缺层，不重放题级apply、评分或FSRS。无portable的旧v1包继续走原路，但完整替换不能静默丢弃已有教法。

2026-10-28：knowledge_updates中的父/KSP可明确声明teaching_focus_update.action=retain；该声明不新增portable文件item类型。适配器沿该父点expected_previous_version读取同一目标的旧focus，复核原始会话哈希、角色和精确原话后补回正文及可信evidence_bindings，可独立于portable_visual文件使用。无声明不补齐，缺版本、缺旧focus、替换路径/正文或原件漂移均拒绝；新增/修订focus不通过此retain传输自动获得本机路径信任。

## 完成读回

核对相关父点及小点的本机与共享after_attempt/direct实际返回方案/反馈及当前版本，protected仍隐藏；预算省略须明确并保留完整证据对象。旧known_version不得复用已变教法。正式成功、原件稳定归档、profile语义、讲题读取与同版发布分别确认，不能用“文件存在”或单个快照current代替实际生效。
