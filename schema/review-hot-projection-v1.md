# 408 复习正式表热投影 v1

## 目的与事实边界

`scripts/review_hot_projection_408.py` 为晨间和普通 review 热路径提供正式表的只读小投影。
它不是真源，也不取代三张正式表。冷构建唯一读取：

- `复习单元节点映射.md`
- `复习单元总表.md`
- `原题复做轨总表.md`

构建器不投影 `闭卷问题` 或 `备注`。`派生自查口径` 只进入一个与 pre-answer RU 对象
分离的 post-answer content-addressed 对象；普通 query 不返回其引用或文本。不读取
review/capture ledger，不分配 ID，不修改正式表。可选定位字段触发答案泄露扫描时只在对象中省略并计入
`redacted_optional_field_count`；必要身份或调度字段不安全时整个构建失败。

## 发布结构

```text
review-hot-projection/
  manifest.json                 原子活动指针
  manifests/<sha256>.json      内容寻址 manifest
  indexes/<sha256>.json        RU/formal/question 与 post-answer 精确索引
  objects/<sha256>.json        单个 RU、formal 或隔离反馈对象
```

冷构建先完成全部解析和交叉验证，再依次发布不可变对象、索引和内容 manifest，最后原子
替换 `manifest.json` 指针。发布中途失败时，旧指针和它绑定的旧 manifest 继续有效；已写入
的新内容对象只是未激活的可回收派生物，不能被热查询读取。

每个正式节点必须恰有一个主 RU、一行原题轨，原题轨主 RU 必须与映射一致。RU 的
`覆盖节点` 必须与主、次映射的节点集合一致。来源题 ID 重复时不写入可路由
`by_question_id`，而写入 `ambiguous_question_ids`，question 查询 fail-closed。

原题轨的 7 天资格只接受 `eligible`、`eligible_unrecorded_date` 和
`protected_until`。无最近复做日期时，最早允许日期也必须为空且资格只能是
`eligible_unrecorded_date`；有最近复做日期时，最早允许日期必须严格等于最近日期加
7 天。若源表仍为 `protected_until` 但构建日已经到达最早允许日，派生对象只把有效资格
归一为 `eligible` 并记录计数，不回写正式表；反向的提前 `eligible` 或其他字段矛盾仍
拒绝发布。

## 热查询

query 必须且只能提供一个精确身份：

- `--review-unit-id RU_*`
- `--formal-node-id DS/CO/OS/CN_*`
- `--question-id <原题轨来源ID>`

成功查询固定读取四个 JSON：活动指针、内容 manifest、精确索引、一个命中对象。每级
ref 都必须等于其 SHA-256 推导出的内容地址，文件字节必须匹配上级绑定。缺失、损坏、
哈希漂移、身份不一致、歧义或超出 byte budget 均返回 `status=unavailable`，不返回部分
记录。

输入预算独立于最终返回预算：活动指针最多 16 KiB、内容 manifest 最多 128 KiB、
exact index 最多 8 MiB、单个对象最多 9 KiB。四层必须绑定同一个 `build_id`、
`object_set_sha256` 和完整 `projection_counts`；每层还必须声明
`answer_safe=true`、`formal_write_count=0`。index 中各映射的实际数量、对象引用集合和
歧义集合必须与这些计数及对象集合哈希一致，不能只验证某一个命中对象的哈希。

成功结果必须满足：

```text
read_only = true
formal_write_count = 0
scan_stats.formal_table_reads = 0
scan_stats.ledger_reads = 0
scan_stats.files_read = 4
byte_count <= byte_budget
```

普通热查询仅提供调度身份、映射、日期、7 天资格及答案安全定位，不提供题干、选项、
答案、解析、完整作答、闭卷问题或派生反馈。派生反馈只能由
`postanswer_feedback_gate_408.py` 在 canonical outcome receipt 通过、且晨间来源的
`first_bound_to_loop` receipt 也通过后读取。门禁失败时不得尝试打开反馈对象。

RU 对象还包含有界的 `primary_original_tracks`：只保存主节点的正式 ID、原题轨状态、
7 天资格、三个冷却日期以及来源/题目 ID。它用于首答提交后的原题行动派生，不包含来源
定位、题面或反馈文本，因此单次 RU 查询即可替代热路径对
`原题复做轨总表.md` 的全表扫描。
