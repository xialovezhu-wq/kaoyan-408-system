# Standalone共享episode输入

完整字段以 /Users/your-user/Documents/kaoyan-408/schema/study-episode-capture-v1.md 为准。已知形状：

```json
{
  "schema":"cs408-study-episode-capture-v1",
  "episode_id":"本次真实学习的稳定技术ID",
  "execution_kind":"standalone_question",
  "study_date":"真实YYYY-MM-DD",
  "source":{"source_id":null,"question":"完整当前题面","options":{}},
  "turns":[{"role":"user","kind":"utterance","text":"实际原话","observed_at":null}],
  "attachments":[],
  "observed_outcome":{"first_response":null,"first_result":"not_observed","final_response":null,"terminal_state":"unresolved","prompt_dependency":"not_observed","reasoning_result":"not_observed"},
  "authorization":{"schema":"cs408-explicit-fast-intake-authorization-v1","kind":"explicit_fast_intake","user_message":"与最新用户命令逐字一致"},
  "display_binding":{"kind":"standalone_source","external_source_id":null},
  "missing_fields":[],
  "frozen_feedback":"已实际给出的反馈"
}
```

例中的日期、原话、结果必须替换为真实值。包含全部相关轮次而非例中一行；未知信息不补造。source_id=null只用于无外部ID的standalone；本地技术键不变成正式来源身份。附件JSON行用path、role、mime_type、label及已知实际sha256，不用Base64搬运。角色与真实提示依赖必须一致。

当前题明确快速入库是本次包的收尾边界；尚未解出可以unresolved保存，不能为了封包假称correct。用户只是提问/讲解时不得调用。
