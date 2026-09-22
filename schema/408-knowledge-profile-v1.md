# 408 网页成稿的个人知识点概况与短摘要 v1

`knowledge_updates.json` 使用 `schema=cs408-knowledge-profile-updates-v1` 和 `updates` 数组。每个本批受影响的规范知识点恰好一项；规范代码必须实际存在于知识点标签表，不创建另一套代码、不根据名称生成代码。

每项字段：

```json
{
  "concept_key": "实际规范代码",
  "expected_previous_version": null,
  "overview": "结合既有历史与本批真实证据写好的完整个人学习概况",
  "short_summary": "当前能力、最近具体断点或修正、提示依赖与下一检查点",
  "next_check": "下一次要检查的一个具体动作",
  "timeline": [],
  "progress": [],
  "question_refs": [],
  "source_refs": [{"path": "实际输入成员或同版发布路径", "sha256": "实际64位SHA256"}],
  "aliases": []
}
```

`expected_previous_version` 是当前已审核概况对象的SHA256，由MCP personal_context的 `learning_profile.version` 返回；没有已审核概况时为null。不是机械point-summary的SHA256。`short_summary` 最多2400 UTF-8字节；完整内容放overview/timeline，不截掉关键条件。日期未知留null；进步没有前后证据时progress可为空，不制造学习或掌握度。

source_refs必须绑定本地增强接收器实际核验过的原始输入或固定快照。完整原话和图片仍留在原始包，不因摘要压缩删减。timeline/progress保留可追溯来源；不能用助手讲解代替用户表现。

question_refs每项带真实formal_id，或新题的logical_question_id。新ID在本地正式事务分配后绑定。原题卡路径、字节SHA和可用的Obsidian URI由本地按实际存在的安全卡确定性填充；formal_id不证明真题年份。已有source/canonical/variant身份保留真实证据，不把变式原型说成用户实际做过的原题。

aliases仅允许已有规范代码的名称别名，不重编号，不覆盖另一个代码的别名。多义名称不能默默选第一个。

## 具体步骤的独立摘要

2026-09-20起，概况可附带`subpoints`，每项包含`subpoint_id,title,keywords,short_summary,next_check,evidence,source_refs`。ID由`knowledge_profile_408.subpoint_key(父规范代码,title)`确定；独立内容SHA随摘要变化，稳定身份保留。它是父知识点内部的步骤/条件身份，不是新考纲代码，不单独制造学习次数或FSRS事件。证据仍绑定真实Capture和原回合，助手错误、提示后修正及独立正确分别描述。

冷刷新将每个小点单独发布到`objects/knowledge-subpoints/`，关键词进入现有小哈希桶。讲题resolve可用`knowledge_points=[父代码]`加`mechanism_terms=[精确关键词]`，或直接用`knowledge_points=[subpoint_id]`；命中后只读取所选小点对象，不展开父概况。多义词返回候选，需要父代码或精确ID消歧。protected视图不返回摘要、检查动作和证据；父点证据变化但小点未更新时返回semantic_stale，不沿用旧掌握判断。

2026-09-21起，日常小点讲题视图仅直接返回short_summary和next_check，完整小点证据保存在独立`objects/knowledge-subpoint-records/`对象，通过evidence_object的ref/sha256访问。这样不会因为跨片合并后的原始证据变长而挤掉4500字节预算内的摘要。完整原件及成稿不截断；protected视图不暴露该证据入口，普通讲题不为此额外展开历史。

本地 `knowledge_profile_408.py` 单独封存网页已完成的语义成稿。它不写题级结果或FSRS，也不代替增强接收器的正式提交。全部受影响点经过版本检查后一次发布概况索引；同operation重试只返回/恢复原提交，不能改同一身份的内容。

每份概况绑定正式提交后的该点原始证据指纹。机械tutoring刷新把成稿复制到同代内容寻址对象，不改写语义：`learning_profile.status=current`才可直接使用短摘要；同点证据后来改变但未更新概况时为stale，不冒充最新。没有网页概况时为not_authored，保留原有机械事实摘要作为明确的兼容资料。

MCP的protected视图不输出概况/短摘要/题目引用内容；after_attempt/direct返回有界短摘要和完整概况的 `profile_object.ref`，完整轨迹按该固定快照引用fetch。完整概况、短摘要、机械事实摘要和个人历史都保留来源边界。
