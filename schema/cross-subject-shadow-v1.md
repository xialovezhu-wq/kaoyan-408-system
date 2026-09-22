# 408 跨科关系影子候选 v1

本协议定义四科考点之间的只读候选生成与影子评测边界。它服务于个性化关联复做，
不替代错题正式关系网，也不授权实时学习阶段写入 `关系边表.md`。

## 一、输入与真源边界

生成器必须读取并冻结以下输入的 SHA-256：

- `知识点标签表.md`：四科统一考点目录。
- `知识体系/数据结构知识体系.md`。
- `知识体系/计算机组成原理知识体系.md`。
- `知识体系/操作系统知识体系.md`。
- `知识体系/计算机网络知识体系.md`。
- `节点总表.md`：将正式题目节点映射回主知识点。
- `关系边表.md`：识别已经存在的正式跨科题对证据。

四个知识体系文件用于确认候选两端确实属于各科当前知识骨架。仅出现在统一目录、
但未出现在对应科目知识体系中的考点不得作为新影子候选端点，并进入 diagnostics。

## 二、两种生命周期

`verified_existing`

表示当前 `关系边表.md` 已经存在具体、非模板化的跨科正式题对，且两端正式节点均能
映射到统一目录和对应科目知识体系中的主知识点。它只是对既有事实的只读投影，
生成器不得借此新增、修改或删除正式边。

`shadow_candidate`

表示候选来自显式的机制规格：两端考点、方向、关系类型和机制依据都已写明，并已通过
当前四科资料存在性检查，但尚未经过正式语义复核。无论置信度是 high、medium 还是 low，
都不得直接进入实时调题或正式关系网。

如果同一稳定语义键同时命中正式题对投影和机制规格，只保留一条
`verified_existing`，并记录 `mechanism_spec_match=true`，不得重复输出一条影子关系。

## 三、禁止纯关键词建边

生成器不得枚举名称相同、词面重叠、同含“队列”“缓存”“路由”等词的考点并据此创建
候选。允许的召回来源只有：

1. 已有正式、具体、非模板理由的跨科题对；
2. 显式机制规格，其中必须包含准确端点、方向、R01-R09 类型、机制级证据摘要和置信度。

纯关键词共现不能产生候选，不能提高置信度，也不能获得后续路由评测资格。

## 四、机器记录

每条记录至少包含：

```json
{
  "id": "CSR408-0123456789ABCDEF",
  "edge_id": "CSR408-0123456789ABCDEF",
  "source_point_id": "CO06-14",
  "target_point_id": "OS05-13",
  "subjects": ["CO", "OS"],
  "relation_kind": "prerequisite_or_pipeline",
  "relation_code": "R06",
  "lifecycle": "verified_existing|shadow_candidate",
  "left": {"id": "CO06-14", "name": "中断响应过程", "subject": "CO"},
  "right": {"id": "OS05-13", "name": "中断处理程序", "subject": "OS"},
  "relation": {
    "code": "R06",
    "name": "上下游知识链",
    "category": "prerequisite_or_pipeline",
    "strength": "强"
  },
  "direction": {"kind": "directed", "from": "CO06-14", "to": "OS05-13"},
  "evidence_summary": "硬件入口动作到操作系统服务程序的责任链。",
  "confidence": "high|medium|low",
  "source_refs": ["知识点标签表.md:L355", "知识体系/操作系统知识体系.md:L168"],
  "evidence_refs": ["知识点标签表.md:L355", "知识体系/操作系统知识体系.md:L168"],
  "generation_method": "formal_edge_projection|curated_mechanism_spec",
  "review_state": "formal_edge_present|pending_semantic_review",
  "activation_eligible": false,
  "activation": "verified_existing|verified_live|shadow_only",
  "enabled_for_live_routing": false,
  "activation_reason": "影子候选尚未完成正式语义复核。"
}
```

方向规则：

- R06 使用 `directed`。已有正式边按起点到终点投影；机制规格必须明确前置端和后续端。
- R01、R02、R03、R04、R05、R07、R08、R09 默认 `bidirectional`，显示端点按知识点 ID
  排序，保证幂等。
- 方向证据不足时不得把候选伪装成可路由关系，应保留影子或转人工复核。

稳定 ID 对知识点端点、关系代码和方向语义做规范化后计算 SHA-256，取前 16 位大写十六进制，
前缀固定为 `CSR408-`。生命周期、置信度、证据措辞和文件行号不参与 ID，因而同一语义关系
从 shadow 晋升为 verified 时 ID 不变。

## 五、激活门禁

默认生成时，所有 `enabled_for_live_routing` 固定为 `false`。`activation_eligible`
不等于实时启用，`verified` 切片模式也不得覆盖该字段。

`activation_eligible=true` 也只表示可以进入后续只读影子路由评测，必须同时满足：

- 生命周期为 `verified_existing`；
- 置信度不是 low；
- 联系强度不是弱；
- 关系类型不是 R07 教学类比；
- 正式边理由不是“同一主知识点”等模板理由；
- 两端主知识点都能由当前真源解析。

低置信、弱关系、R07、纯关键词召回和任何 `shadow_candidate` 一律不可获得资格。
后续正式晋升仍必须走关系语义复核、唯一写者和仓库事务门禁，本协议不定义晋升写入。

公开生成器和 CLI 不提供 live-enable 参数，且公开 `write_projection`
拒绝任何已启用 live edge 的 payload。实时 allowlist 只能由
`cross_shadow_activation_408.py` 在核验当前 fingerprint 的签发 evaluator
assignment、内容寻址证据、至少两个 evaluator、三个独立 execution identity、
两个真实 Asia/Shanghai 日期和零失败后，通过私有写入口构建。该流程仍只
更新可重建派生投影，不修改正式关系边。

## 六、幂等与输出

同一组输入文件和同一生成器版本必须得到完全相同的 JSON、记录顺序、稳定 ID 与 Markdown
审阅面。输出不含运行时间戳或绝对路径。

为避免热路径加载无界邻接，机器 JSON 还必须生成 `routing_adjacency`。它按 source point
组织，默认每个源考点最多 5 条，硬上限为 20；排序依次优先显式 live-enable、可评测的
正式证据、`verified_existing`、较高置信度和较强联系。它只从已生成记录构造，不做考点笛卡尔积。
有向关系只进入方向起点的邻接；双向关系进入两端邻接。

```bash
python3 scripts/cross_subject_candidates_408.py --repo . --format json
python3 scripts/cross_subject_candidates_408.py --repo . --format markdown
python3 scripts/cross_subject_candidates_408.py --repo . --format markdown --lifecycle shadow_candidate
python3 scripts/cross_subject_candidates_408.py --repo . --write-projection --format json
python3 scripts/cross_shadow_activation_408.py --repo . status --limit 8
```

JSON 是机器真源审阅面。Markdown 只显示稳定 ID、状态、方向、关系类型、置信度、后续评测资格
和精简证据摘要，不复制题干、答案、选项、完整解析或用户作答。

显式 `--write-projection` 才会原子写入派生文件，默认位置为
`wiki/study_vaults/408-full/state/personalization/cross-subject-shadow.json`。该写入属于可重建
投影，`formal_write_count=0`；命令拒绝把投影路径指向任一正式输入文件。未提供该参数时，
命令只向 stdout 输出，文件系统保持不变。

## 七、影子评测与正式边界

第一轮影子测试只记录：当前错点、候选 ID、候选排名、是否存在可靠题源、人工相关性判断和
选择器耗时。它不向用户展示关联题，也不修改复盘账本中的正式掌握度。

候选在多个真实事件中表现稳定后，可以进入单独的语义复核。只有正式事务完成、receipt 和
全局审计通过后，才允许把具体题目关系写入正式关系网。知识点候选本身不能自动扩张成题目
节点之间的满团关系。
