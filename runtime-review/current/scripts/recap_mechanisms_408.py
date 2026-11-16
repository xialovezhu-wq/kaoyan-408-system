"""Typed schematic recipes for 408 recap cards. No exam payload enters a recipe."""

import re
from pathlib import Path

ASSET_DIR = Path("support/interactive-408")
LIBRARY = ASSET_DIR / "mechanisms"
SUBJECTS = ("DS", "CO", "OS", "CN")
IDENTITY = re.compile(r"(?:S27-(?:DS|CO|OS|CN)(?:-\d{2}){1,3}(?:#[1-9]\d*)?|(?:DS|CO|OS|CN)\d{2}-\d{2})")
LINK = re.compile(r"(\d+)([>-])(\d+)(?::(.+))?")


def _labels(text):
    labels = text.split(" / ")
    if not all(label.strip() and len(label) <= 30 for label in labels):
        raise ValueError("empty or oversized diagram label")
    return labels


def _rows(text):
    rows = [_labels(row) for row in text.split(" ~ ")]
    if not 1 <= len(rows) <= 7 or max(map(len, rows)) > 8:
        raise ValueError("diagram grid outside the supported range")
    return rows


def _links(text, count):
    if not text:
        raise ValueError("an explicit connection is required")
    links = []
    for link in text.split(" / "):
        match = LINK.fullmatch(link)
        if not match:
            raise ValueError("invalid diagram connection " + link)
        source, target = int(match[1]), int(match[3])
        if source == target or max(source, target) >= count:
            raise ValueError("connection must link two existing objects")
        links.append({"from":source, "to":target, "arrow":match[2] == ">", "label":match[4] or ""})
    if len(links) > 12:
        raise ValueError("too many connections in one recap diagram")
    return links


def recipe(kind, data, connections=""):
    if kind == "communication":
        if connections or data not in {"symbol-rate", "channel-capacity", "differential"}:
            raise ValueError("unknown communication lesson")
        return communication_lesson(data)
    if kind in {"fields","partition"}:
        if connections or not re.fullmatch(r"[a-z][a-z0-9-]{0,31}",data):
            raise ValueError("invalid field profile name")
        if kind=="partition":
            from recap_precision_408 import partition
            return partition(data)
        return {"kind":"fields","profile":data}
    if kind == "address":
        if data != "paging" or connections:
            raise ValueError("unknown address mapping model")
        return {"kind":"address", "model":"paging"}
    if kind == "sequence":
        actors = _labels(data)
        messages = _links(connections, len(actors))
        if not 2 <= len(actors) <= 4 or len(messages) > 7:
            raise ValueError("too many lifelines or events")
        for message in messages:
            label, _, fields = message["label"].partition(" @ ")
            lost = label.startswith("!")
            label = label.removeprefix("!")
            if not label or not message["arrow"] or len(label) > 25 or len(fields) > 65:
                raise ValueError("sequence messages need a direction and a bounded label")
            message.update(label=label, fields=fields, lost=lost)
        return {"kind":kind, "actors":actors, "messages":messages}
    if kind == "flow":
        items = _labels(data)
        if not 2 <= len(items) <= 6 or connections:
            raise ValueError("flow needs two to six ordered steps")
        return {"kind":kind, "items":items}
    if kind not in {"graph", "band", "table", "lanes"}:
        raise ValueError("unknown diagram kind " + kind)
    rows = _rows(data)
    if kind == "graph":
        if max(map(len, rows)) > 4 or len(rows) > 5:
            raise ValueError("graph layout too dense")
        return {"kind":kind, "rows":rows, "edges":_links(connections, sum(map(len, rows)))}
    if connections:
        raise ValueError("unexpected connection column")
    if kind in {"table", "lanes"} and (len(rows)<2 or len(rows[0])<2 or len({len(row) for row in rows})!=1):
        raise ValueError("tables and lanes need aligned rows")
    return {"kind":kind, "rows":rows}


def communication_lesson(topic):
    """Reviewed, changed-number examples. These are teaching data, not attempts."""
    if topic == "symbol-rate":
        return {"kind": "communication", "topic": topic,
                "formulas": ["每码元信息量 k = log₂ M（bit/码元）", "比特率 R = 波特率 Rₛ × log₂ M"],
                "definitions": "M 为可区分的码元状态数，Rₛ 为每秒码元数（Baud），R 为每秒比特数（bit/s）。",
                "example": "改编示例 · 沿用 2011 年第 34 题的反求步骤，更换速率和相位数",
                "given": "某链路比特率为 9600 bit/s，采用 8 相位调制，求波特率。",
                "steps": [{"title": "数状态", "detail": "8 个相位可区分，M = 8。"},
                          {"title": "每码元比特数", "detail": "k = log₂ 8 = 3 bit/码元。"},
                          {"title": "反求波特率", "detail": "Rₛ = 9600 ÷ 3 = 3200 Baud，即每秒发送 3200 个码元。"}],
                "note": "相同码元速率下，增加可区分状态数可以提高比特率。"}
    if topic == "channel-capacity":
        return {"kind": "communication", "topic": topic,
                "formulas": ["奈奎斯特 · Rmax = 2B log₂ M", "香农 · C = B log₂(1 + S/N)",
                             "信噪比换算 · SNRdB = 10 log₁₀(S/N)"],
                "definitions": "B 为信道带宽（Hz），M 为码元状态数，S/N 为线性功率比。Rmax、C 的单位为 bit/s。奈奎斯特用于理想无噪声低通信道，香农给出有噪声信道的容量上限。",
                "example": "改编示例 · 沿用 2009 年第 34 题的 QAM 计算步骤，更换带宽和状态数",
                "given": "理想无噪声低通信道带宽 4 kHz，QAM 有 8 个相位、每个相位有 4 种振幅，求最大比特率。",
                "steps": [{"title": "组合状态", "detail": "M = 8 × 4 = 32 种可区分状态。"},
                          {"title": "每码元比特数", "detail": "log₂ 32 = 5 bit/码元。"},
                          {"title": "速率上限", "detail": "2B = 8000 Baud；Rmax = 8000 × 5 = 40000 bit/s = 40 kbit/s。"}],
                "extra_example": {"title": "香农配套示例 · 自编改数值",
                                  "given": "有噪声信道带宽 4 kHz，S/N = 15，求容量上限。",
                                  "steps": ["1 + S/N = 16。", "C = 4000 × log₂16 = 16000 bit/s = 16 kbit/s。"]},
                "note": "若题目同时给出有限状态数与噪声条件，分别求两项上限，取较小值。信噪比以 dB 给出时先转为线性比值。"}
    return {"kind": "communication", "topic": topic,
            "bits": [1, 0, 1, 1, 0, 0, 1, 0], "initial_level": 1,
            "formulas": ["曼彻斯特 · 本示例高→低为 1，低→高为 0", "差分曼彻斯特 · 位首有跳变为 0，无跳变为 1"],
            "definitions": "两种编码每位中间都跳变以提供同步。曼彻斯特按位中方向判值，差分曼彻斯特按位首是否跳变判值。",
            "example": "改编波形 · 沿用 2021 年第 34 题的逐位判读方法，更换比特串",
            "given": "示例比特串 10110010，第一位开始前电平明确为高。虚线标位界，细点线标位中。",
            "note": "考试图若缺少第一位前的电平或首位约定，先判第 2 位及以后，再结合选项核对第一位。"}


def load_library(repo, subjects=SUBJECTS):
    from recap_precision_408 import load_fields
    profiles=load_fields(repo)
    diagrams = {}
    for filename in (*subjects,"points"):
        path = repo / LIBRARY / (filename + ".tsv")
        if filename == "points" and not path.is_file():
            continue
        for number, line in enumerate(path.read_text().splitlines(),1):
            if not line.strip() or line.startswith("#"):
                continue
            try:
                identity, kind, data, *extra = line.split("|")
                if (not IDENTITY.fullmatch(identity) or identity in diagrams or len(extra)>1
                        or (filename != "points" and not identity.startswith("S27-"+filename+"-"))):
                    raise ValueError("invalid or duplicate identity")
                model=recipe(kind,data,*extra)
                if kind=="fields":
                    if model["profile"] not in profiles:
                        raise ValueError("unknown field profile "+model["profile"])
                    model=profiles[model["profile"]]
                diagrams[identity]=model
            except ValueError as exc:
                raise ValueError(f"{path.name}:{number}: {exc}") from exc
    return diagrams


def with_outline(diagrams, nodes):
    expected = {node["id"] for node in nodes if not node["children"]}
    expected.update(f"{node['id']}#{i}" for node in nodes for i in range(1,len(node["details"])+1))
    actual = {identity for identity in diagrams if identity.startswith("S27-")}
    if actual != expected:
        raise ValueError(f"recap diagram coverage differs, missing {sorted(expected-actual)}, unexpected {sorted(actual-expected)}")
    titles = {node["id"]:node["title"] for node in nodes}
    return diagrams | {
        node["id"]:{"kind":"tree","root":node["title"],"children":[titles[key] for key in node["children"]]}
        for node in nodes if node["children"]
    }


def labels(value):
    """Check the text-only surface of the selected diagram before serialization."""
    if isinstance(value,str):
        yield value
    elif isinstance(value,list):
        for item in value:
            yield from labels(item)
    elif isinstance(value,dict):
        for item in value.values():
            yield from labels(item)
