# 408 个性化跨科候选 overlay v1

本协议定义一个只读、答案安全、有界且确定性的 shadow 审阅面。它把用户在跨科关系两端的
真实失败证据与既有跨科来源桥并列展示，供后续人工语义审核使用。它不是正式关系，不授权
实时调题，也不修改学习证据或正式错题库。

## 一、唯一输入

生成器只允许读取当日 `personalization/manifest.json` 逐字节绑定的两个投影：

1. `failure_signatures.json`，其 SHA-256 必须来自 `artifact_hashes`；
2. `cross-subject-shadow.json`，其 SHA-256 必须来自
   `cross_subject_shadow_sha256`。

manifest 必须是 `personalization_index_v1`、`answer_safe=true`、
`formal_write_count=0`。失败签名必须是同日的 `failure_signature_index_v1`，跨科投影必须是
`cross_subject_shadow_v1`。文件缺失、符号链接、哈希不一致、schema 不符、越过数量上限时
全部失败关闭，不扫描 ledger、正式表、整个 Obsidian 库或参考书。

## 二、候选组成

每条输出候选至少包含：

- source 和 target 考点 ID、科目与安全名称；
- 共同机制判断；
- 因果或结构桥、方向、关系类型、来源引用与生成方法；
- source 和 target 两侧用户证据，每条证据显式列出 event、question、date、layer；
- 教学价值；
- 反例和不适用边界；
- 证据缺口；
- `enabled_for_live_routing=false` 与 `formal_write_count=0`。

用户证据只能由失败签名对端点考点的精确命中产生。同科、同模块、词面相似或关系摘要本身
不能伪造用户曾在某一侧犯错。两侧证据还必须具有不同 evidence identity；同一 outcome
同时覆盖两端时，只能进入 `shadow_needs_distinct_side_evidence`。

## 三、共同机制边界

共同机制只允许由 source 和 target 两侧失败签名的 `mechanism_primitives` 交集确认。

- 交集非空：`controlled_mechanism_primitive_overlap`；
- 交集为空但跨科桥由正式边投影或显式机制规格及其来源支持：
  `source_backed_bridge_only`；
- 交集为空且来源桥也未通过门禁：`unproven_no_controlled_overlap`。

`source_backed_bridge_only` 必须明确说明“不能声称两端机制相同”。不得用自然语言摘要、相同
关键词、相同考点名称或模型猜测补造机制原语。

## 四、review-ready 门禁

只有同时满足下列条件时才可标记 `shadow_review_ready`：

1. source 和 target 两侧都有精确绑定的用户失败证据；
2. 两侧均至少有一条完整的 event、question、date、layer 证据；
3. 两侧至少存在不同的 evidence identity；
4. 跨科桥来自 `formal_edge_projection` 或 `curated_mechanism_spec`，并具有安全摘要和来源引用；
5. 关系不是 R07；
6. 生成方法不是 keyword、lexical、surface、name-overlap 或 co-occurrence 召回。

其余状态包括：

- `shadow_needs_other_side_evidence`；
- `shadow_needs_complete_side_evidence`；
- `shadow_needs_distinct_side_evidence`；
- `shadow_analogy_only`；
- `shadow_needs_source_backed_bridge`；
- `shadow_keyword_only_rejected`。

即使 `shadow_review_ready`，它也只表示“材料足以交给人工审阅”，不表示关系已成立、用户错因
相同、可以调题或可以写入正式关系。

## 五、有界与确定性

默认最多输出 64 条候选，每侧最多 3 条证据；硬上限分别为 256 和 6。输入失败签名上限为
1024，跨科记录上限为 2048。排序、候选 ID、build fingerprint 与 JSON 序列化均不包含
运行时间或绝对路径。同一 manifest、两个输入字节和相同预算必须得到逐字节相同的输出。
生成器版本显式进入 build fingerprint；实现语义变化时必须递增版本。

输出固定包含输入 manifest、失败签名和跨科 shadow 的实际 SHA-256，并声明
`hash_verification=pass`。

## 六、写入边界

默认命令只输出 JSON，不写文件：

```bash
python3 scripts/personalized_cross_candidates_408.py --repo .
```

显式使用 `--write-overlay` 时，只能原子写入：

```text
wiki/study_vaults/408-full/state/personalization/personalized-cross-candidates.json
```

不提供输出路径覆盖。写入前必须与当前 manifest 重新完整构建并逐字节一致。该文件是可重建
personalization 派生投影；`formal_write_count=0`、`formal_relation_write_count=0`，不得修改
`关系边表.md`、正式节点、学习 outcome、capture、掌握度或任何 morning／variant 文件。

## 七、能力声明

本 overlay 只提供候选审阅材料。所有候选和顶层字段都固定
`enabled_for_live_routing=false`。它不承担跨科自动调题、变式题生成、正式关系晋升或平台聊天
拦截；这些能力必须由各自的独立门禁和 receipt 证明。
