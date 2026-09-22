# 408 统考真题语料协议 v1

本协议定义 2009-2025 年 408 统考真题的完整语料层。它不改变正式错题库的一题一结算规则。

## 分层

- 原始 PDF：只读来源，不移动、不改写。
- 完整语料：`/Users/your-user/Documents/kaoyan-408-details/corpus/408统考真题/`，允许保存题面切片、答案解析切片、OCR 和折叠详情卡。
- 主仓库：只保存本协议、安全入口、知识点索引入口和 crosswalk 说明；不保存完整题干、答案、解析或图片。
- 正式错题库：只有用户真实做错的题才可通过 `scripts/intake_apply_408.py` 以 batch size=1 晋升。

## Canonical ID

每道官方题使用唯一键：

```text
EXAM408-YYYY-QNN
```

例如 `EXAM408-2009-Q01`、`EXAM408-2025-Q47`。PDF 文件名、教材小节题号、正式错题节点序号和旧 `CO03-Qxxx` 都不是 canonical ID。

旧语料 ID 不删除、不改名；确认同题后只作为 alias 写入 crosswalk。

## 必填表面

每个 canonical 题必须同时存在：

1. `cards/EXAM408-YYYY-QNN.md`
2. 至少一个题面切片
3. 至少一个答案/解析切片
4. `indexes/question-index.csv` 唯一行
5. 唯一主知识点
6. 包含主知识点的命中知识点列表
7. 原题 PDF、解析 PDF、页码和 SHA-256 来源链

## 知识点规则

- 主知识点恰好一个，必须来自 `知识点标签表.md`。
- 命中知识点为 1-4 个正式标签，必须包含主知识点。
- OCR 只作候选和检索；分类需结合题面与解析语义复核。
- 真题分类不生成错因，不写 E01-E10，不建立 R03 或其他错因关系边。

## 去重和错题映射

- 去重以 canonical ID、规范化题干 OCR、题图 OCR/感知证据和旧语料 alias 联合判断。
- 既有详情入口中的“第 N 题”可能是教材内部题号，不能单独作为官方全卷题号。
- 人工逐图确认的同题关系必须写入详情库 `_review/formal-crosswalk-exact-overrides.csv`，字段固定为 `canonical_id,formal_node_id,status,evidence`；该文件是耐久复核真源，`formal-node-crosswalk.csv` 只是 finalize 生成的投影。
- 人工复核当前只允许 `status=exact`，`evidence` 不得为空。canonical ID 或正式节点不存在、canonical 重复、同一正式节点绑定多道 canonical、或人工 exact 与既有机器 exact 冲突时，finalize 必须在发布写入前硬失败。
- 人工 exact 投影到 crosswalk 后使用 `match_level=exact`、`review_status=manual_exact`，并在 `match_basis` 保留人工证据；不得以直接编辑生成 crosswalk 的方式代替复核源。
- `formal-node-crosswalk.csv` 的 `exact` 才表示已存在同题正式错题节点。
- `probable_needs_review` 只作候选，不得自动改名、迁移或合并正式节点。
- 同一道 canonical 真题命中多个既有正式节点时，写入 `formal-duplicate-clusters.csv`，只记录冲突，不自动合并。
- `wrong-question-knowledge-links.csv` 只记录真题与既有错题的知识点维度；`error_dimension_updated` 和 `error_edges_updated` 必须为 `no`。

## 完成门禁

一次全量发布只有满足以下条件才算完成：

- 2009-2025 共 17 年，每年 47 题，总计 799 个唯一 canonical ID。
- 799/799 题面资产存在。
- 799/799 答案解析资产存在。
- 799/799 主知识点和命中知识点合法。
- 来源清单包含每年题卷与解析角色；2021 独立题卷缺口使用已登记的本地版本 1 补源。
- 旧 CO03 真题语料全部得到 alias 审计结果。
- 与既有正式错题的 exact 映射不把一个正式节点绑定到多道 canonical 题。
- `scripts/audit_true_exam_corpus.py` 返回 PASS。
- 正式错题表、错因标签和关系边不因本批次发生批量写入。
