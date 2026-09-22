# D 原图引用交付 v1

发布路径：`support/d408/asset-delivery-contract.md`。本合同只改变已有原图的交付方式，不改变选题、曝光、覆盖、网页自检或学习保存规则。

## 分工与状态

网页实际查看选题、求解所需原图，负责完整题面、解析、自检、个人历史和知识点覆盖。**已有本地原图无需在网页保存成文件或随ZIP往返传输**，改交固定快照内的路径、哈希及用途。网页生成的新图表、SVG等仍须作为实际文件随包交付；不得用生成图冒充原图。

完整备题后可标 `status=WEB_PREPARED`，另标 `asset_state=PENDING_LOCAL_MATERIALIZATION`。这是可交给本地预处理，**不是本地验收通过或可开始学习**。`covered_concept_ids`表示网页已完成语义备题的点；必要原图已实际读取且有可核实引用、仅缺网页文件副本，不再记为unresolved。必要原图未读、引用来源不明、解析不完整、自检未完成仍是实质缺项。

不强制复制每个关联附件。区分当前题必需材料与原型/历史参考；同字节路径可去重引用。必要多页必须读齐，重复内容可记录同一哈希。未读的可选参考标 `not_viewed`，不能据此宣称完成必要视觉检查。

## ZIP格式

在原D格式基础上增加：

- manifest：`asset_delivery="snapshot_refs_v1"`。
- 根目录 `asset_refs.jsonl`：每行一个原图引用；无引用时保留空文件。
- `manifest.files`只列ZIP内真实存在的文件，包括asset_refs.jsonl；**不列尚待本地装配的图片**，不伪造它们已入包。

原型参考示例（不是当前变式公开题面）：

```json
{
  "question_id": "DS_2099_913",
  "source_path": "details/assets/DS_2099_913/question-01.png",
  "source_sha256": "4c6e333def242bd786d5a46f3c3ac3bbeda41393012dd1d3161bdc7e0e8ddb78",
  "package_path": "private/source-assets/DS_2099_913/question-01.png",
  "role": "reference",
  "visual_status": "viewed"
}
```

- `question_id`为selected/supplement中的真实正式来源节点；`source_path`必须属于固定发布清单 `question_refs[question_id].question_images/solution_images`中的实际图像路径。
- source_commit统一沿用manifest；引用若额外填写，须完全相同。不用当前目录或新HEAD代替固定快照。
- `source_sha256`使用实际工具返回的源文件SHA-256。来源已核实但工具未给出时可省略或null，本地从已验证发布清单取得并校验，不能猜测。可选size若填写也须一致。
- `package_path`为预定的安全相对目标路径，可由网页决定；不含绝对路径、`..`或符号链接，后缀与源图相同。当前支持PNG/JPEG/WebP/GIF。
- `visual_status`为viewed或not_viewed；本地保留声明，不把复制成功当成阅读证明。
- 每个package_path唯一；role为以下三种之一。

| role | 用途与位置 |
| --- | --- |
| question | 本轮实际使用原题的公开题图，须已查看，只能取该来源question_images。目标为`questions/<question_id>/assets/...`，在对应item的image_paths中引用。原型题图不能作为网页变式或既有变式的题面。 |
| solution | 已查看的原解析图，只能取solution_images，目标在private/，不得列入公开image_paths。 |
| reference | 原型或历史参考图，目标在private/，不得列入公开image_paths；可选未读参考如实标not_viewed。 |

problem_path/solution_path/history_path/teaching_path仍指向网页实际写好的包内文件。题面Markdown可写相对于自身的图片链接，其最终落点须与package_path相符。变式自己的公开图表必须随包，原型参考只交private引用。原题、原解析和原型身份不能互换。

## 本地处理

1. 原样保存original.zip和通过manifest校验的web-output/。
2. 从发布回执绑定的同一source_commit读取清单与引用资产；校验所属题目、用途、路径、哈希和大小。在独立assembled-output-v1/中装配，不回写原ZIP或原始网页文件。
3. 生成派生文件清单和private/local-asset-assembly.json；相同内容复用一次取件。恢复时核对原输入和派生字节，只继续未完成层。
4. 原生D导入在变式接纳前再次检查装配字节与公开/私有边界，再走原有完整due/priority/request、suppression、曝光和全分段准备门禁。
5. 全链准备成功才生成真实START_DEEPSEEK.md。复制不清除真实unresolved，不把单点检查包/保全包升级为全量题包，不开始展示或学习。

旧版内嵌原图包保留兼容；不能把旧包缺件偷偷改成已覆盖。补图不证明变式语义正确，也不替代尚未完成的必要阅读。网页不执行本地命令、不访问任意磁盘路径、不拼接Base64重建原图。哈希不符、版本缺失或用途不符时明确阻塞，不静默换版本或重新出题。
