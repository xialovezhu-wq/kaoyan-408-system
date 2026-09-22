"""Prepare readable learning paths and small teaching views during cold refresh."""
from __future__ import annotations

import json
from pathlib import Path
import re
from typing import Any, Mapping
from urllib.parse import urlencode

from knowledge_learning_records_408 import DIRECT_ROLES, _chronology

SCHEMA = "knowledge_teaching_view_408_v1"
PAGE_DIR = Path("知识点/学习路径")
VIEWS = ("protected", "after_attempt", "direct")
ROLE_NAMES = {
    "observed_learner_evidence": "真实学习证据", "learner_report": "本人陈述",
    "user_report": "本人陈述", "visible_evidence": "可见作答证据",
    "direct_observation": "直接观察", "observed_understanding": "已观察的理解",
    "direct": "直接作答", "repair": "纠正记录", "hint": "提示记录",
    "formal_summary": "关联题的正式摘要", "assistant_explanation": "助手讲解",
    "historical_source_summary": "历史资料摘要", "source_summary": "来源摘要",
}
DEPENDENCIES = {"independent": "独立作答", "hint_or_prompted": "使用过提示",
                "none": "未标记提示依赖", "unknown": "提示依赖未知",
                "not_observed": "未观察", "minimal": "少量提示",
                "progressive": "逐步提示", "full": "完整讲解"}

CONCEPT_VISUALS = {
    "DS06-20": 'flowchart TD\n  A[有向图] --> B{是否有向无环}\n  B -->|否| C[不存在拓扑序]\n  B -->|是| D[反复选择入度零顶点并删除其出边]\n  D --> E{每轮候选是否始终只有一个}\n  E -->|是| F[拓扑序唯一]\n  E -->|否| G[拓扑序不唯一]',
    "OS02-04": 'stateDiagram-v2\n  就绪 --> 运行: 获得CPU\n  运行 --> 就绪: 被抢占或时间片到\n  运行 --> 阻塞: 等待事件\n  阻塞 --> 就绪: 事件完成',
    "OS02-05": 'stateDiagram-v2\n  就绪 --> 运行: 调度\n  运行 --> 就绪: 抢占\n  运行 --> 阻塞: 等待\n  阻塞 --> 就绪: 唤醒',
    "OS03-18": 'flowchart TD\n  A[虚拟地址] --> B{TLB命中}\n  B -->|是| C[检查权限并形成物理地址]\n  B -->|否| D[查页表]\n  D --> E{页面驻留}\n  E -->|是| C\n  E -->|否| F[合法访问触发缺页处理]',
    "CO03-25": 'flowchart TD\n  A[虚拟地址] --> B{TLB命中}\n  B -->|是| C[使用映射并检查权限]\n  B -->|否| D[查页表]\n  D --> E{页面驻留}\n  E -->|是| C\n  E -->|否| F[缺页处理]',
}


def _date(record: Mapping[str, Any]) -> str | None:
    value = record.get("effective_observed_date") or record.get("observed_date")
    return value if isinstance(value, str) and re.fullmatch(r"\d{4}-\d{2}-\d{2}", value) else None


def _learner(record: Mapping[str, Any]) -> bool:
    return record.get("evidence_role") in DIRECT_ROLES and record.get("evidence_role") != "hint"


def _excerpt(value: object, max_bytes: int = 600) -> str:
    text = str(value or "").strip()
    if len(text.encode("utf-8")) <= max_bytes:
        return text
    result = ""
    for sentence in re.split(r"(?<=[。！？；\n])", text):
        if len((result + sentence).encode("utf-8")) > max_bytes:
            break
        result += sentence
    return result.strip() + "（完整记录见学习路径）" if result else "原记录较长，见完整学习路径。"


def _question(row: Mapping[str, Any]) -> dict[str, Any]:
    return {key: row[key] for key in ("formal_id", "obsidian_uri", "open_url", "markdown_link",
        "link_status", "visual_note_path", "visual_note_sha256", "card_relpath", "card_sha256") if row.get(key)}


def _question_markdown(row: Mapping[str, Any]) -> str:
    return str(row.get("markdown_link") or f"{row['formal_id']}（可视化资料暂缺）")


def _item_text(value: Any) -> str:
    if isinstance(value, str):
        return value
    if isinstance(value, Mapping):
        for field in ("summary", "statement", "description"):
            if isinstance(value.get(field), str):
                return value[field]
    return json.dumps(value, ensure_ascii=False, sort_keys=True)


def _full_item(value: Any) -> list[str]:
    if isinstance(value, str):
        return [value, ""]
    return ["```json", json.dumps(value, ensure_ascii=False, indent=2), "```", ""]


def _cell(value: Any) -> str:
    return str(value or "未记录").replace("|", "&#124;").replace("\n", "<br>")


def _timeline(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    candidates = sorted((row for row in rows if _learner(row) and row.get("summary")
                         and row.get("point_binding") == "explicit_point"), key=_chronology)
    seen, result = set(), []
    for row in candidates:
        summary = str(row.get("effective_summary") or row["summary"])
        identity = (_date(row), summary, row.get("prompt_dependency"))
        if identity in seen:
            continue
        seen.add(identity)
        result.append({"date": _date(row), "summary": _excerpt(summary, 420),
                       "role": ROLE_NAMES.get(row.get("evidence_role"), "个人学习证据"),
                       "dependency": DEPENDENCIES.get(row.get("prompt_dependency"), "提示依赖未知"),
                       "formal_ids": row.get("formal_ids", []), "record_key": row["record_key"]})
    return result


def build_path(code: str, label: str, point: Mapping[str, Any],
               records: Mapping[str, Mapping[str, Any]], questions: list[dict[str, Any]],
               reviewed: Mapping[str, Any] | None, reviewed_current: bool,
               *, repo_root: Path | None = None, has_vault: bool = False,
               concept_brief: list[str] | None = None) -> dict[str, Any]:
    """Keep full evidence in the page; precompute the small learner-facing views."""
    keys = list(point.get("record_keys", [])) + list(point.get("unknown_date_record_keys", []))
    rows = [records[key] for key in keys]
    dated_learning = sorted({_date(row) for row in rows if _learner(row) and _date(row)})
    all_dates = sorted({_date(row) for row in rows if _date(row)})
    dates = dated_learning or all_dates
    date_scope = "学习证据日期" if dated_learning else "来源记录日期，不等于实际学习日期"
    first_date, latest_date = (dates[0], dates[-1]) if dates else (None, None)
    point_observations = [row for row in rows if _learner(row) and row.get("point_binding") == "explicit_point"]
    timeline = _timeline(rows)
    overview = (f"已整理 {len(rows)} 条来源记录，关联 {len(questions)} 道正式题；"
                f"其中 {len(point_observations)} 条个人学习证据明确绑定本知识点。")
    if dates:
        overview += f"{date_scope}：{first_date} 至 {latest_date}。"
    else:
        overview += "尚无可确认的记录日期。"
    current = bool(reviewed and reviewed_current)
    status = "semantic_current" if current else "semantic_stale" if reviewed else "evidence_digest_only"
    progress = list(reviewed.get("progress", [])) if current else []
    next_check = (reviewed["next_check"] if current else
                  "依据当前真实作答核对本知识点；提示后完成与独立掌握分别判断。")
    short = str(reviewed["short_summary"]) if current else overview
    if not current:
        independent = sorted((row for row in point_observations
                              if row.get("prompt_dependency") == "independent" and row.get("summary")), key=_chronology)
        candidates = independent[-1:] or list(point.get("historical_understanding", [])[:1])
        candidates += [point.get("latest_understanding"), point.get("latest_repair")]
        candidates += list(point.get("undated_understanding", [])[:1])
        if not any(item and item.get("summary") for item in candidates):
            candidates += list(point.get("prior_instruction", [])[:1])
        seen = set()
        snippets = []
        for item in candidates:
            if not item or not item.get("summary"):
                continue
            key = item.get("record_key") or json.dumps(item, ensure_ascii=False, sort_keys=True)
            if key in seen:
                continue
            seen.add(key)
            role = ROLE_NAMES.get(item.get("evidence_role"), "已存记录")
            binding = "；关联题证据，不单独证明本点错误" if item.get("point_binding") == "question_associated" else ""
            dependency = "；提示后" if item.get("prompt_dependency") == "hint_or_prompted" else ""
            snippets.append(f"{_date(item) or '日期未记录'}（{role}{binding}{dependency}）：{_excerpt(item['summary'])}")
            if len(snippets) == 3:
                break
        for snippet in snippets:
            if len((short + "\n" + snippet).encode("utf-8")) > 2000:
                break
            short += "\n" + snippet
        if reviewed:
            short = "原正式语义概况尚未覆盖最新记录；以下为当前事实整理。\n" + short

    page = PAGE_DIR / (code + ".md")
    page_path = str(repo_root / page) if repo_root else page.as_posix()
    page_uri = "obsidian://open?" + urlencode({"path": page_path}) if has_vault else None
    state_text = {"semantic_current": "正式语义概况已覆盖当前证据",
                  "semantic_stale": "旧正式语义概况已过期，当前采用最新事实整理",
                  "evidence_digest_only": "已完成事实预处理；尚无经审核的跨事件进步裁决"}[status]
    lines = ["---", f"title: {code} {label}｜学习路径", 'tags: ["408", "知识点速览"]', "---", "",
             f"# {code} {label}", "", f"[[知识点/学习路径/索引|知识点目录]] · [[知识点/{code}|关联节点]]", ""]
    if concept_brief:
        lines += ["> [!abstract] 概念速览", "> " + concept_brief[0], "",
                  "> [!tip] 考试定位与第一动作", "> " + concept_brief[1], "",
                  "通用概念说明；个人经历与掌握状态以下方真实记录为准。", ""]
        if code in CONCEPT_VISUALS:
            lines += ["```mermaid", CONCEPT_VISUALS[code], "```", ""]
        if code in {"CN03-12", "CN03-13"}:
            lines += ["| 对照 | GBN | SR |", "| --- | --- | --- |",
                      "| 窗口内失序帧 | 丢弃 | 缓存 |",
                      "| 确认 | 通常累计确认 | 单独确认 |",
                      "| 重传 | 从缺口起重传未确认段 | 选择需要重传的帧 |",
                      "| 向上交付 | 按序 | 补齐缺口后按序 |", ""]
    else:
        lines += ["> [!warning] 概念速览待补齐", "> 暂无预存概念描述，不以标签名称冒充完整预处理。", ""]
    lines += ["## 我的学习路径", "", "| 时间与范围 | 当前可确认内容 |", "| --- | --- |",
              f"| 最早学习证据 | {first_date or '未记录'} |",
              f"| 最近学习证据 | {latest_date or '未记录'} |",
              f"| 有记录的学习日期 | {len(dated_learning)} 天；不是做题次数 |",
              "| 首次正式入库日期 | 当前证据未提供，不用学习日期替代 |",
              "| 累计做题次数 | 暂不能跨来源可靠去重，不以记录条数代替 |", "",
              "> [!info] 证据状态", "> " + state_text + "。", "",
              "## 讲题短摘要", "", short, "", "## 真实断点与变化", ""]
    if timeline:
        lines += ["| 日期 | 当时理解或断点 | 证据与提示状态 | 对应题目 |", "| --- | --- | --- | --- |"]
        question_map = {row["formal_id"]: row for row in questions}
        for item in timeline:
            refs = "、".join(_question_markdown(question_map[fid]) for fid in item["formal_ids"] if fid in question_map)
            lines.append("| " + " | ".join(_cell(value) for value in (item["date"], item["summary"],
                item["role"] + "；" + item["dependency"], refs or "未绑定题目")) + " |")
        lines += ["", "同日不同来源不推定先后；表格摘录可在下方完整记录中核对。", ""]
    else:
        lines += ["尚无明确绑定本知识点的个人理解记录；关联题不自动证明你在此处做错。", ""]
    lines += ["## 进步与待验证", ""]
    if progress:
        for item in progress:
            lines += _full_item(item)
    else:
        lines += ["尚无经审核的跨事件进步对照；不由题目关联或提示后复述推断独立掌握。"]
    if current and reviewed.get("timeline"):
        lines += ["", "## 正式概况中的时间线", ""]
        for item in reviewed["timeline"]:
            lines += _full_item(item)
    lines += ["", "## 下一检查", "", next_check, "", "## 涉及题目", "",
              "以下链接用于定位历史证据，不是本次复做推荐；关联题不自动等于本知识点的错误。", ""]
    question_map = {row["formal_id"]: row for row in questions}
    lines += ["- " + _question_markdown(row) for row in questions] or ["暂无已绑定的正式题目。"]
    if reviewed and not current:
        lines += ["", "## 旧正式概况（仅作历史）", "", str(reviewed.get("overview", ""))]
    lines += ["", "## 完整已记录学习过程", "",
              "保留所有来源记录。日期不明的记录单列；同一天的排列不补造先后顺序。", ""]
    history_start = len(lines)
    for key, row in zip(keys, rows):
        role = ROLE_NAMES.get(row.get("evidence_role"), str(row.get("evidence_role") or "未分类来源"))
        lines += [f"### {_date(row) or '日期未记录'} · {role}", "", f"记录：`{key}`", ""]
        fids = [fid for fid in row.get("formal_ids", []) if any(q["formal_id"] == fid for q in questions)]
        if fids:
            lines += ["题目：" + "、".join(_question_markdown(question_map[fid]) for fid in fids), ""]
        if row.get("point_binding") == "question_associated":
            lines += ["证据范围：关联题；不能直接归因为本知识点错误。", ""]
        if row.get("prompt_dependency"):
            dependency = row["prompt_dependency"]
            lines += ["提示依赖：" + DEPENDENCIES.get(dependency, str(dependency)) + "。", ""]
        summary = str(row.get("effective_summary") or row.get("summary") or "")
        lines += ["> " + line for line in summary.splitlines()] if summary else ["该记录未保存文字概况，不能据此补造作答过程。"]
        source = row.get("source_locator") or row.get("source_id")
        lines += ["", "来源：`" + str(source or "未记录") + "`；位置：`" + str(row.get("source_fragment") or "未记录") + "`。",
                  "来源 SHA-256：`" + str(row.get("source_sha256") or "未记录") + "`。", ""]
    if not rows:
        lines += ["暂无归入本知识点的个人学习证据；不推断已学、错误或掌握。", ""]
    lines[history_start:] = [f"> [!quote]- 展开 {len(rows)} 条完整来源记录"] + [
        "> " + line for line in lines[history_start:]]

    chosen_questions = list(reviewed.get("question_refs", [])) if current else questions
    linked = []
    for row in chosen_questions:
        value = _question(row)
        if value.get("formal_id") and value not in linked:
            linked.append(value)
    base = {"schema": SCHEMA, "knowledge_point": code, "concept_name": label,
            "status": status, "first_date": first_date, "latest_date": latest_date,
            "date_scope": date_scope, "record_count": len(rows),
            "explicit_observation_count": len(point_observations),
            "independent_mastery_inferred": False}
    after = {**base, "short_summary": _excerpt(short, 2000),
             "concept_brief": ({"description": concept_brief[0], "exam_entry": concept_brief[1],
                                "origin": "general_exam_knowledge_not_personal_evidence"} if concept_brief else None),
             "brief_status": "ready" if concept_brief else "missing",
             "progress": [_excerpt(_item_text(item), 350) for item in progress[:2]],
             "next_check": _excerpt(next_check, 600),
             "questions": linked[:2], "question_refs_truncated": len(linked) > 2,
             "page_path": page_path, "page_relpath": page.as_posix(), "obsidian_uri": page_uri,
             "open_url": f"http://127.0.0.1:8769/concept/{code}" if page_uri else None,
             "markdown_link": f"[{code} {label}（学习路径）](http://127.0.0.1:8769/concept/{code})" if page_uri else None}
    return {"page_relpath": page.as_posix(), "markdown": "\n".join(lines).rstrip() + "\n",
            "views": {"protected": {**base, "content_fields_redacted": True},
                      "after_attempt": after, "direct": {**after}}}
