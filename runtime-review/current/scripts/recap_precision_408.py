"""Bit-accurate layouts and disclosed, question-bound recap overlays."""

import json
import re
import sys
from pathlib import Path

BASE = Path("support/interactive-408/mechanisms")
LAYOUTS = BASE / "field-layouts.json"
CASES = BASE / "question-parameters.json"
KEY = re.compile(r"[a-z][a-z0-9-]{0,27}")
POINT = re.compile(r"(?:DS|CO|OS|CN)\d\d-\d\d")
EXAM = re.compile(r"EXAM408-\d{4}-Q\d\d(?:-P\d\d)?")
MAX_SEQ = 2**32 - 1

PARTITIONS = {
    "ipv4-prefix": ("IPv4 地址", ("网络前缀","主机位")),
    "cache": ("Cache 地址字段", ("标记","组索引","块内偏移")),
    "cache-va": ("虚拟地址中的 Cache 索引", ("其余虚地址位","组索引","块内偏移")),
    "instruction": ("指令字段", ("操作码","题设的寻址或功能字段","操作数相关字段")),
    "instruction-reg-base-offset": ("指令字段预算（示意）", ("操作码","源寄存器编号","基址寄存器编号","补码偏移量")),
}
PROFILE_ANCHORS = {
    "tcp-408":"S27-CN-05-03-01", "tcp-9293":"S27-CN-05-03-01",
    "udp":"S27-CN-05-02-01", "ipv4":"S27-CN-04-03-01",
    "ipv6":"S27-CN-04-04-01", "ipv6-address":"S27-CN-04-04-02",
    "ipv4-multicast":"S27-CN-04-06-02", "ethernet":"S27-CN-03-06-02",
    "ppp":"S27-CN-03-07-02", "arp-ipv4-ethernet":"S27-CN-04-03-04", "icmp-echo":"S27-CN-04-03-04",
    "binary32":"S27-CO-02-04-01", "binary64":"S27-CO-02-04-01",
}
PARTITION_ANCHORS = {"ipv4-prefix":"S27-CN-04-03-03", "cache":"S27-CO-03-06-02",
                     "cache-va":"S27-CO-03-06-02", "instruction":"S27-CO-04-02",
                     "instruction-reg-base-offset":"S27-CO-04-02"}
ADDRESS_ANCHORS = {"S27-CO-03-07-02","S27-OS-03-01-03","S27-OS-03-02-02"}
TCP_FIELD_RECALL = {
    "src": "先确认这条报文的发送端，再读第一个 16 位端口字段。",
    "dst": "先确认报文方向，再读接收端对应的端口字段。",
    "offset": "先读 4 位首部长度，再乘 4 B 找到应用数据起点。",
    "flags": "先圈出置 1 的控制位，再结合报文方向判断当前阶段。",
    "flag-ack": "先看 ACK 控制位是否置 1，再读取 32 位确认号。",
    "window": "先确认是谁广告窗口，再从它的确认号向后数可接收的字节。",
}
TCP_SEQUENCE_RECALL = {
    "seq": ("发送方的 SEQ", "先定这条报文的发送方，再追它自己的序号。",
            "两端的序号空间独立。"),
    "ack": ("对端的下一期待序号", "逆着这条确认消息，找到被确认的是哪一端。",
            "确认号写接收方下一期待的序号。"),
}
ADDRESS_RECALL = {
    "vpn": ("虚页号查页表", "先切掉页内偏移，再用虚页号选页表项。", "虚页号来自虚拟地址的高位。"),
    "pfn": ("页框号进入物理地址", "先确认页表项有效，再把页框号放入物理地址的高位。",
            "有效映射下，物理地址由页框号和原偏移组成。"),
    "offset": ("页内偏移原样保留", "先由页大小定位偏移字段，再沿图核对它在两个地址中的位置。",
               "在同一页大小下，地址翻译保留页内偏移。"),
}


def _text(value, maximum=120):
    return isinstance(value,str) and bool(value.strip()) and len(value)<=maximum and not any(ord(c)<32 for c in value)


def _integer(value, minimum, maximum):
    return type(value) is int and minimum<=value<=maximum


def field_frame(profile, raw):
    """Derive offsets from wire order instead of duplicating position literals."""
    word = raw["word_bits"]
    if not _integer(word,1,256) or not 1<=len(raw["rows"])<=9:
        raise ValueError(f"invalid field grid: {profile}")
    rows=[]
    positions={}
    offset=0
    for row in raw["rows"]:
        size=sum(bits for _,_,bits in row)
        if size!=word and not (len(row)==1 and size%word==0):
            raise ValueError(f"unaligned field row: {profile}")
        fields=[]
        for key,label,bits in row:
            if key in positions or not KEY.fullmatch(key) or not _text(label,32) or not _integer(bits,1,256):
                raise ValueError(f"invalid field: {profile}/{key}")
            field={"key":key,"label":label,"bits":bits,"start":offset}
            positions[key]=field
            fields.append(field)
            offset+=bits
        rows.append({"fields":fields,"bits":size})
    flags=[]
    for key,names in raw.get("flags",{}).items():
        field=positions[key]
        if len(names)!=field["bits"] or not all(_text(name,12) for name in names):
            raise ValueError(f"mis-sized control bits: {profile}/{key}")
        flags.extend({"label":name,"position":field["start"]+i,"key":"flag-"+name.lower() if name.isascii() else f"flag-{i}"}
                     for i,name in enumerate(names))
    focus=raw.get("focus",[])
    tips=raw.get("tips",{})
    selectable = positions.keys() | {flag["key"] for flag in flags}
    if not set(focus)<=selectable or not tips.keys()<=selectable or not all(_text(v) for v in tips.values()):
        raise ValueError(f"unknown field focus: {profile}")
    tail=raw.get("tail",[])
    if len(tail)>6 or any(len(row)!=2 or not all(_text(v,45) for v in row) for row in tail):
        raise ValueError(f"invalid variable portion: {profile}")
    return {
        "kind":"fields","profile":profile,"title":raw["title"],"word_bits":word,"group":raw["group"],
        "rows":rows,"fixed_bits":offset,"flags":flags,"tail":tail,"tips":tips,"highlight":focus,
        "source":raw.get("source",""),"note":raw.get("note",""),
    }


def load_fields(repo):
    source=json.loads((repo/LAYOUTS).read_text())
    return {key:field_frame(key,value) for key,value in source.items()}


def partition(scheme):
    title, fields=PARTITIONS[scheme]
    return {"kind":"partition","scheme":scheme,"title":title,"fields":list(fields)}


def _wire(value, base):
    if value is None:
        return None
    if type(value) is int:
        value=str(value) if base==10 else f"{value:X}"
    pattern=r"\d{1,10}" if base==10 else r"(?:0[xX])?[0-9A-Fa-f]{1,8}"
    if not isinstance(value,str) or not re.fullmatch(pattern,value) or not 0<=int(value,base)<=MAX_SEQ:
        raise ValueError("invalid TCP sequence input")
    return value


def _advance(value, amount, base):
    if value is None:
        return None
    number=(int(value,base)+amount)&MAX_SEQ
    if base==10:
        return str(number)
    prefix=value[:2] if value.startswith(("0x","0X")) else ""
    return prefix+format(number,f"0{len(value)-len(prefix)}X")


def tcp_case(raw):
    if not isinstance(raw,dict):
        raise ValueError("TCP case must be an object")
    roles=raw.get("roles",[])
    base=raw.get("base",10)
    if (not isinstance(roles,list) or len(roles)!=2 or roles[0]==roles[1]
            or not all(_text(role,12) for role in roles) or base not in (10,16)):
        raise ValueError("invalid TCP case roles or notation")
    mode=raw.get("type")
    dropped=set()
    if mode=="handshake":
        if set(raw)-{"type","roles","base","isn","show","captions"}:
            raise ValueError("unknown handshake parameter")
        isn=raw.get("isn",[])
        show=raw.get("show",[])
        if not isinstance(isn,list) or len(isn)!=2 or not isinstance(show,list) or not 1<=len(show)<=3:
            raise ValueError("missing handshake inputs or allowed steps")
        if any(not isinstance(keys,list) or len(set(keys))!=len(keys) or not set(keys)<={"seq","ack"} for keys in show):
            raise ValueError("invalid allowed handshake fields")
        active,passive=(_wire(value,base) for value in isn)
        if active is None and passive is None:
            raise ValueError("a numeric handshake needs a given ISN")
        seq_next=_advance(active,1,base)
        ack_next=_advance(passive,1,base)
        events=[(0,"SYN",active,None),(1,"SYN+ACK",passive,seq_next),(0,"ACK",seq_next,ack_next)]
    elif mode=="reply":
        if set(raw)-{"type","roles","base","seq","ack","payload_bytes"} or not _integer(raw.get("payload_bytes"),0,65535):
            raise ValueError("invalid TCP data reply inputs")
        seq=_wire(raw.get("seq"),base)
        ack=_wire(raw.get("ack"),base)
        if seq is None or ack is None:
            raise ValueError("the received segment needs SEQ and ACK")
        events=[(0,"收到的数据段",seq,ack),(1,"按题设连续回复",ack,_advance(seq,raw["payload_bytes"],base))]
        show=[["seq","ack"],["seq","ack"]]
    elif mode=="first-data":
        if set(raw)-{"type","roles","base","isn"}:
            raise ValueError("invalid first data sequence inputs")
        seq=_wire(raw.get("isn"),base)
        if seq is None:
            raise ValueError("first data sequence needs a given ISN")
        events=[(0,"SYN",seq,None),(0,"建连后首个数据字节",_advance(seq,1,base),None)]
        show=[["seq"],["seq"]]
    elif mode=="observed":
        trace=raw.get("events")
        if set(raw)-{"type","roles","base","events"} or not isinstance(trace,list) or not 1<=len(trace)<=6:
            raise ValueError("invalid disclosed TCP trace")
        events=[]
        for i,event in enumerate(trace):
            if (not isinstance(event,dict) or set(event)-{"from","label","seq","ack","lost"}
                    or not _integer(event.get("from"),0,1) or not _text(event.get("label"),26)
                    or type(event.get("lost",False)) is not bool):
                raise ValueError("invalid disclosed TCP event")
            if event.get("lost"):
                dropped.add(i)
            events.append((event["from"],event["label"],_wire(event.get("seq"),base),_wire(event.get("ack"),base)))
        show=[["seq","ack"] for _ in events]
    else:
        raise ValueError("unknown TCP case type")
    messages=[]
    captions=raw.get("captions",[row[1] for row in events][:len(show)])
    if not isinstance(captions,list) or len(captions)!=len(show) or not all(_text(label,26) for label in captions):
        raise ValueError("invalid TCP packet captions")
    for i,((sender,_,seq,ack),keys) in enumerate(zip(events,show,strict=False)):
        if i==0 and mode=="handshake" and "ack" in keys:
            raise ValueError("opening SYN has no valid ACK field")
        values=[f"{field.upper()}={value}" for field,value in (("seq",seq),("ack",ack)) if field in keys and value is not None]
        messages.append({"from":sender,"to":1-sender,"arrow":True,"label":captions[i],
                         "fields":"  ".join(values),"lost":i in dropped})
    return {"kind":"sequence","actors":roles,"messages":messages,"source":"本问已核题设"}


def address_widths(value):
    if (not isinstance(value,dict) or value.keys()!={"va","pa","offset"}
            or not all(_integer(bits,1,64) for bits in value.values())
            or value["offset"]>=min(value["va"],value["pa"])):
        raise ValueError("invalid virtual or physical address widths")
    return {"kind":"address","model":"paging","widths":value,"source":"位宽取本问已核题设"}


def sized_partition(scheme,widths):
    title, names=PARTITIONS[scheme]
    if (not isinstance(widths,list) or len(widths)!=len(names)
            or not all(_integer(bits,0,128) for bits in widths) or not 1<=sum(widths)<=128
            or (scheme=="ipv4-prefix" and sum(widths)!=32)):
        raise ValueError("partition does not match the field definition")
    rows=[(f"part{i}",name,bits) for i,(name,bits) in enumerate(zip(names,widths,strict=True)) if bits]
    missing=[name for name,bits in zip(names,widths,strict=True) if not bits]
    return field_frame(scheme,{"title":title,"word_bits":sum(widths),"group":"本问字段","rows":[rows],
                               "source":"位宽取本问已核题设",
                               "note":"、".join(missing)+"占 0 位。" if missing else ""})


def model_for_inputs(raw, profiles):
    if not isinstance(raw,dict) or len(raw)!=1:
        raise ValueError("choose one case diagram input")
    if "tcp" in raw:
        return tcp_case(raw["tcp"])
    if raw.get("datapath") == "single-bus-yz-2022":
        return {"kind":"datapath", "profile":"single-bus-yz-2022",
                "source":"按 2022 年第 43 题本问原图重绘，数值 3、5、8 仅作机制演示。",
                "steps":[
                    {"title":"先存操作数", "bus":"3", "y":"3", "z":"未定", "driver":"GPRs",
                     "active":["gpr-out","bus-y"], "signals":"GPRout = 1，Yin = 1",
                     "reason":"第一拍由寄存器组驱动内部总线，拍末把 3 存入 Y。"},
                    {"title":"运算并存结果", "bus":"5", "y":"3", "z":"8", "driver":"GPRs",
                     "active":["gpr-out","bus-b","y-a","alu-z","alu-fr"], "signals":"GPRout = 1，ALUop 选加法，Zout = 0",
                     "reason":"Y 通过专用连线供 A 端，总线上的 5 供 B 端。结果 8 在拍末存入 Z，标志送 FR。"},
                    {"title":"结果送回寄存器", "bus":"8", "y":"3", "z":"8", "driver":"Z",
                     "active":["z-out","bus-gpr"], "signals":"Zout = 1，GPRin = 1，GPRout = 0",
                     "reason":"这一拍由 Z 驱动内部总线，目标通用寄存器在拍末接收 8。"},
                    {"title":"关输出仍保留", "bus":"无驱动", "y":"3", "z":"8", "driver":"无",
                     "active":[], "signals":"Zout = 0，GPRout = 0",
                     "reason":"Zout 关闭使输出端呈高阻，Z 仍保存 8。总线无驱动不能读作 0。"}],
                "objects":{
                    "Y":"Y 是暂存器。总线改传第二个操作数时，Y 仍通过专用连线向 ALU 的 A 端提供第一个数。",
                    "Z":"Z 是结果暂存器。运算拍内总线正传操作数，结果先存在 Z，下一拍再送总线。",
                    "GPRs":"通用寄存器组。GPRout 控制送总线，GPRin 控制从总线接收，读写寄存器由题图的编号信号选择。",
                    "ALU":"A 端接 Y，B 端接内部总线，F 端接 Z。ALU 是组合逻辑，保存结果由 Z 完成。",
                    "FR":"标志寄存器保存 ALU 运算产生的 SF、OF 等标志。",
                    "Zout":"Zout 控制三态输出门。打开时 Z 驱动总线，关闭时退出驱动，保存内容不变。",
                    "BUS":"内部总线同一时刻只允许一个数据源驱动，可以有多个部件接收。"}}
    if "address_widths" in raw:
        return address_widths(raw["address_widths"])
    if "partition" in raw:
        data=raw["partition"]
        if not isinstance(data,dict) or set(data)!={"scheme","widths"} or data["scheme"] not in PARTITIONS:
            raise ValueError("invalid sized partition")
        return sized_partition(data["scheme"],data["widths"])
    if "field_profile" in raw and raw["field_profile"] in profiles:
        return profiles[raw["field_profile"]]
    raise ValueError("unknown case diagram input")


def compile_cases(repo, templates, by_question):
    raw=json.loads((repo/CASES).read_text())
    profiles=load_fields(repo)
    result={}
    stale=[]
    for identity,record in raw.items():
        if not EXAM.fullmatch(identity) or identity not in templates:
            raise ValueError("case identity is not a registered question")
        if identity not in by_question:
            continue
        if record["anchor"] not in by_question[identity]["anchors"]:
            raise ValueError("case diagram outside the question's assessed outline: "+identity)
        evidence=record["given_contains"]
        given=templates[identity]["given_objects"]
        if not evidence or not all(any(fragment in line for line in given) for fragment in evidence):
            stale.append(identity)
            continue
        guidance=record.get("guidance",{})
        if set(guidance)-{"action","rule"} or not all(_text(value,100) for value in guidance.values()):
            raise ValueError("invalid scoped case guidance: "+identity)
        result[identity]={"anchor":record["anchor"],"model":model_for_inputs(record["inputs"],profiles),
                          "guidance":guidance}
    return result,stale


def read_case_input(argument):
    if argument=="-":
        raw=sys.stdin.buffer.read(8193)
    else:
        with Path(argument).open("rb") as stream:
            raw=stream.read(8193)
    if len(raw)>8192:
        raise ValueError("recap case input exceeds 8 KiB")
    return json.loads(raw)


def case_context(value, *, identity, exact, points, anchors, pinned, profiles):
    if not isinstance(value,dict) or set(value)-{"schema","identity","point","anchor","highlight","step","source","inputs","recall"}:
        raise ValueError("invalid recap case context fields")
    if value.get("schema")!="cs408-recap-case-v1" or value.get("identity") not in {identity,exact}-{None}:
        raise ValueError("recap case context does not bind the current question")
    point=value.get("point")
    anchor=value.get("anchor")
    highlight=value.get("highlight")
    step=value.get("step")
    if point is not None and (not isinstance(point,str) or not POINT.fullmatch(point) or point not in points):
        raise ValueError("recap focus point outside the current assessed scope")
    if anchor is not None and (not isinstance(anchor,str) or anchor not in anchors):
        raise ValueError("recap anchor outside the current assessed scope")
    if highlight is not None and (not isinstance(highlight,str) or not KEY.fullmatch(highlight)):
        raise ValueError("invalid visual field focus")
    if step is not None and not _integer(step,1,7):
        raise ValueError("invalid message step")
    recall=value.get("recall",{})
    limits={"summary":30,"action":75,"rule":100}
    if (not isinstance(recall,dict) or recall.keys()-limits.keys()
            or any(not _text(line,limits[key]) for key,line in recall.items())
            or (recall and not (anchor or point))):
        raise ValueError("recall needs a bound focus and concise reviewed wording")
    if recall and exact and "-P" in exact:
        raise ValueError("independent subquestion recall uses the scoped question material")
    prepared=pinned
    inputs=value.get("inputs")
    if inputs is not None:
        if not anchor or value.get("source") not in {"question","prepared"}:
            raise ValueError("actual parameters need a bound outline anchor and given source")
        model=model_for_inputs(inputs,profiles)
        kind=model["kind"]
        if kind=="sequence":
            mode=inputs["tcp"]["type"]
            if mode=="observed":
                expected={f"S27-CN-05-03-{n:02d}" for n in range(1,6)}
            elif mode=="reply":
                expected={"S27-CN-05-03-01","S27-CN-05-03-03"}
            else:
                expected={"S27-CN-05-03-02"}
        elif kind=="address":
            expected=ADDRESS_ANCHORS
        elif kind=="datapath":
            expected={"S27-CO-05-03"}
        elif "partition" in inputs:
            expected={PARTITION_ANCHORS[model["profile"]]}
        else:
            expected={PROFILE_ANCHORS[model["profile"]]}
        if anchor not in expected:
            raise ValueError("case input is not the mechanism for the selected outline point")
        if pinned and (pinned["anchor"]!=anchor or pinned["model"]!=model):
            raise ValueError("case input differs from the reviewed exact-question projection")
        prepared=pinned or {"anchor":anchor,"model":model,"guidance":{}}
    return {"point":point,"anchor":anchor,"highlight":highlight,"step":step,
            "prepared":prepared,"recall":recall}


def focus_model(model, focus):
    if not model:
        return model
    highlight=focus.get("highlight")
    step=focus.get("step")
    if step is not None and (model["kind"]!="sequence" or step>len(model["messages"])):
        raise ValueError("message step outside the disclosed part")
    if highlight:
        if model["kind"]=="fields":
            valid={field["key"] for row in model["rows"] for field in row["fields"]}
            valid.update(flag["key"] for flag in model["flags"])
            if highlight not in valid:
                raise ValueError("field focus is not in this layout")
            model=model|{"highlight":[highlight]}
        elif model["kind"]=="sequence" and highlight in {"seq","ack"}:
            events=[model["messages"][step-1]] if step else model["messages"]
            if not any(re.search(r"\b"+highlight.upper()+"=",event["fields"]) for event in events):
                raise ValueError("message field outside the disclosed part")
            model=model|{"highlight":highlight}
        elif model["kind"]=="address" and highlight in {"vpn","pfn","offset"}:
            model=model|{"highlight":highlight}
        else:
            raise ValueError("field focus is incompatible with this diagram")
    if step is not None:
        model=model|{"step":step-1}
    return model


def focused_recall(model):
    """Use the same selected field for the diagram and its two recall lines."""
    if not model:
        return {}
    key=model.get("highlight")
    if model["kind"]=="address" and key in ADDRESS_RECALL:
        label, action, rule=ADDRESS_RECALL[key]
        return {"cue":label,"summary":label,"action":action,"rule":rule}
    if model["kind"]=="sequence" and key in TCP_SEQUENCE_RECALL:
        label, action, rule=TCP_SEQUENCE_RECALL[key]
        field="确认号" if key=="ack" else "序号"
        cue=f"第 {model['step']+1} 条报文 · {field}" if "step" in model else label
        return {"cue":cue,"summary":label,"action":action,"rule":rule}
    if model["kind"]!="fields" or not key or len(key)!=1:
        return {}
    key=key[0]
    fields={field["key"]:field["label"] for row in model["rows"] for field in row["fields"]}
    fields.update({flag["key"]:flag["label"]+" 控制位" for flag in model["flags"]})
    label=fields[key]
    action=TCP_FIELD_RECALL.get(key) if model["profile"].startswith("tcp-") else None
    return {"cue":f"{label} · 位宽与用法","summary":label,
            "action":action or f"先沿位标尺定位{label}，再按本题条件读值。",
            "rule":model["tips"].get(key,"")}


def current_question_model(model):
    """A question-bound fallback keeps direction without inserting template ISNs."""
    if model and model["kind"]=="sequence" and any(
        re.search(r"(?:SEQ|ACK)=[xyuv](?:\+1)?\b",event["fields"]) for event in model["messages"]
    ):
        return model|{"messages":[event|{"fields":""} for event in model["messages"]],
                      "source":"报文字段按本问题设核对"}
    if model and model["kind"]=="band" and any("SEQ=x" in label for row in model["rows"] for label in row):
        return model|{"rows":[["本段首字节序号","本段按数据字节占号","下一段从末字节的下一号起"]],
                      "source":"序号及数据量取本问已核题设"}
    return model
