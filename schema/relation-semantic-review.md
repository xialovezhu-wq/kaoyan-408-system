# 408 逐题关系语义复核规则

本规则用于既有正式错题库的全量质量维护。它不替代新题入库协议，也不得借用
`new` / `redo` 绕过既有节点维护边界。

## 一、目标与边界

- 每个正式节点都要单独复核：主知识点、实际考查机制、核心考点、问法入口、用户错误入口、复做第一动作和关系状态。
- 每条既有边都要裁决：`keep`、`remove` 或 `replace`；不能因结构审计 PASS 就默认保留。
- 正式仓库不记录完整题干、答案、选项、解析、图片或作答痕迹；逐题审核报告也遵守同一复做保护。
- `复盘输出/` 不读取、不生成、不修改。
- 只读 reviewer 可以并行；正式发布永远只有一个写者，并使用仓库锁、基线哈希、WAL、receipt 和幂等键。

## 二、证据等级

- `A`：题图和独立解析图均可用。
- `B`：题图可用，且详情卡有实质解析文字。
- `C`：题图可用，但只有结论或证据不足以独立验证错因/关系强度。
- `D`：占位详情，无可用题图或解析；必须 `needs_user`，不得据此新增或强化关系。

## 三、节点审核记录

每个正式 ID 恰好一条节点记录，至少包含：

```json
{
  "record_type": "node_review",
  "id": "CO_2099_912",
  "subject": "CO",
  "evidence_grade": "A",
  "evidence_files": [{"path": "...", "sha256": "..."}],
  "main_knowledge_decision": "keep|replace|needs_user",
  "main_knowledge": "CO03-xx 名称",
  "core_point_decision": "keep|refine|needs_user",
  "core_point": "安全、具体、可复做的考查机制",
  "question_mechanism": "不泄露答案的实际考查入口",
  "relation_status": "connected|reviewed_no_reliable_edge|needs_user",
  "confidence": "high|medium|low",
  "notes": "证据边界或待补项"
}
```

- `keep` 仍要说明实际考查机制，不能只写“字段正确”。
- `replace/refine` 必须给出安全的新值和证据路径。
- C 级可以复核题面知识点，但错因和关系强度应保守；D 级一律 `needs_user`。
- 任一节点的 `relation_status=needs_user` 时，本批不得同时用 `replace/refine`
  发布新字段；与该节点相连的全部基线边必须 `remove`。可以保留安全候选文字，
  但要明确它不是已确认正式事实。

## 四、关系边审核记录

每个无序题对恰好一条边记录：

```json
{
  "record_type": "edge_review",
  "a": "CO_2099_909",
  "b": "CO_2099_910",
  "current_type": "R01",
  "decision": "keep|remove|replace",
  "new_type": "R02",
  "new_strength": "强|中|弱",
  "new_priority": "高|中|低",
  "reason": "两题共享的具体机制、问法模板、错误模式或上下游依赖",
  "confidence": "high|medium|low"
}
```

- 仅主知识点相同、专题链相同、模糊概念词面相同，不足以 `keep` 或新增正式边。
- R01 必须是同一具体核心考点；R02 必须有近似解题流程；R03 必须有相同且具体的用户错误模式；R04/R05 必须指出明确概念边界；R06 必须说明方向；R07 保持稀疏；R08 必须同年份同模块；R09 必须是明确的多年高频考法聚类。
- 模板原因 `同一主知识点：...`、`同属专题链：...` 必须改写、改型或删除，不能原样过审。
- 允许节点没有边；此时节点状态写 `reviewed_no_reliable_edge`。

## 五、并行审核与对账

- reviewer 按科目/模块领取不重叠节点；同科目边由无序题对中字典序较小的端点所属批次负责。
- 跨科目边统一交中央对账，不由任一科 reviewer 单独发布。
- 两端节点结论冲突、重复身份、主知识点待改或证据哈希漂移时，转 `needs_user` 或重新复核，不静默选择一方。
- reviewer 发现基线以外的新边时只写入摘要候选；唯一写者二次核对后，才可在
  绑定 snapshot 的发布计划中登记。新边只接受 `confidence=high`、具体机制理由、
  非模板题对；不得连接 `needs_user` 节点，也不得用来强行维持连通度。

## 六、正式发布门禁

1. 冻结并复核 `节点总表.md`、`关系边表.md`、`知识点命中索引.md`、四科节点库、安全卡和 details 证据的基线 SHA-256。
2. 本批所有节点都有节点审核记录；本批涉及的全部旧边都有唯一裁决。
3. 正式写入由唯一写者在仓库锁内完成；失败整体回滚，receipt 记录 before/after hash。
4. 节点字段变更必须同步正式总表、科目镜像、命中索引、日期索引和安全卡；关系变更只维护可靠边。
5. 批次后运行 `audit_network_health.py`、`check_safe_node_cards.py --list-missing`、`audit_extended.py`；锁外刷新 coverage 和 bridge。
6. 答案/错选/选项泄露为 0；`needs_user` 不得落成已确认事实。

## 七、完成定义

只有当前冻结快照中的全部节点均有唯一审核记录、全部基线边均已裁决、所有变更已按批次发布、
模板边归零或有具体理由替代、全局门禁通过，才可宣称本轮全量逐题质量复核完成。
