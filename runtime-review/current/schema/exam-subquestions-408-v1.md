# 408 独立真题小问

## 学习合同

每次只练当前小问。题面包含所需条件、单位、初态、图表及当前问法。若依赖前问，直接提供前问已经完成后的结果作为输入，不能要求先做前问。复杂度题直接给待分析的完整算法；地址追踪可给已经确定的地址映射。前问结果不是当前答案，给定结果的来源、校订和原文之间的区别要保留。

题源不变，不把自编题冒充原题；`EXAM408-YYYY-QNN-Pxx` 保留父卷题 `parent_true_item_id`、官方小问编号、细分序号、题型与细粒度关键词。无编号的大题可保留一个小问，复合所求在可独立练习时继续细分。当前问答案只在私有参考解析中。

## 资料层

完整语义与题图在独立详情库 `corpus/408统考真题/subquestions/`：

- `review-{DS,CO,OS,CN}.json` 是逐题核对的语义真源。共同条件在 `shared_context`，本问可用 `standalone_context` 收窄为所需条件。
- `parts` 提供 `prompt`、`extra_conditions`、`provided_prerequisite_results`、必要 `condition_assets`、`assessed_points`、`syllabus_ids` 与 `keywords`。原 `prerequisite_part_nos` 只用于来源追溯；依赖项必须采用 `supplied_prerequisite_results` 且填入已完成结果，不接受旧任务闭包。
- `scripts/exam_practice_units_408.py build` 核对父题源哈希、全体大题精确覆盖、编号、合法考点和结果依赖，再生成每问卡片、专属完整题图及索引。`audit` 校验完整题面、私有解析、卡片、载荷和原索引的哈希。
- 主仓 `support/syllabus-2027/exam-subquestions.json` 仅存安全身份、知识点、关键词和资产承诺，不含完整题干或答案。

2026题目按用户指定新《精解》接纳，旧版不参与覆盖。接受证据在详情库 `sources/acceptance-2026-20261003.json`；总范围通过 `support/syllabus-2027/exam-corpus-acceptance.json` 绑定。保留第三方题源身份，不把整理版称为官方发布认证。

## 晨间选题与曝光

新策略 `recent_errors_keyword_diversity_v2` 先筛近期真实错误关联与14天外题面，再尽量减少关键词重复，同类候选优先最近真实接触更久者；选择题与独立小问同等竞争，未知日期排在已知日期后。最终按关键词组织。每主规范点至多一题，不能为凑30放宽条件。

全题接触约束其全部小问；仅练一个小问不伪造其他小问的作答。已给前问答案记为真实展示，不能马上把该前问当独立测试；另一小问复用相同已知输入本身不算重复作答。整题频次仍按父题去重，小问拥有独立考点定位和答后开场。

新小问全部走D补充或同点替代，自由回答计算、推理或代码；禁止送入只接受ABCD的旧原题campaign。新源通过 `practice-units/index.json` 扩展已有源集合，不改旧不可变style release和active/paused会话。准备、发布、展示、作答和掌握分开记录。

## 重建顺序

完成逐科语义源与总题索引后，合并父题实际考点并更新其源哈希；构建子题与安全索引；运行 `syllabus_context_408.py preprocess`；刷新同版教学投影，再请求现有共享发布器。恢复只补失败层，不重放任何学习/正式入库事件。
