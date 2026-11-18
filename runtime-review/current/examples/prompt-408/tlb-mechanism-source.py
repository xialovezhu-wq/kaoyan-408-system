# Exact function excerpts from scripts/question_diagram_408.py for review. Dependencies run in the native 408 project.

def _paging():
    # Hardware walks are assumed by this exercise model. Page repair is delegated to the OS.
    # https://docs.kernel.org/mm/page_tables.html#mmu-tlb-and-page-faults
    return {
        "kind":"objects", "height":370, "template":"vm-address-path-v1",
        "bands":[{"label":"地址转换 · MMU","top":.02,"bottom":.62},
                 {"label":"缺页处理 · 操作系统","top":.71,"bottom":.98}],
        "nodes":[
            {"id":"va","x":.15,"y":.22,"label":"VA","value":"VPN | 偏移","shape":"box"},
            {"id":"tlb","x":.46,"y":.22,"label":"TLB","shape":"table"},
            {"id":"pa","x":.80,"y":.22,"label":"PA","value":"页框号 | 偏移","shape":"box"},
            {"id":"pte","x":.46,"y":.48,"label":"页表项","shape":"table"},
            {"id":"fault","x":.23,"y":.85,"label":"缺页处理","shape":"box"},
            {"id":"fill","x":.52,"y":.85,"label":"装入页框","shape":"box"},
            {"id":"retry","x":.81,"y":.85,"label":"重试访存","shape":"box"},
        ],
        "edges":[
            {"id":"lookup","from":"va","to":"tlb","label":"VPN","arrow":True},
            {"id":"hit","from":"tlb","to":"pa","label":"有效命中","arrow":True},
            {"id":"walk","from":"tlb","to":"pte","label":"未命中","arrow":True},
            {"id":"resolved","from":"pte","to":"pa","label":"驻留","arrow":True},
            {"id":"trap","from":"pte","to":"fault","label":"未驻留","arrow":True},
            {"id":"load","from":"fault","to":"fill","label":"","arrow":True},
            {"id":"retry-translation","from":"fill","to":"retry","label":"更新映射","arrow":True},
        ],
        "frames":[
            {"label":"TLB 命中","active":["va","tlb","pa"],"edges":["lookup","hit"],
             "caption":"有效 TLB 项提供页框号，拼物理地址时页内偏移保持不变。"},
            {"label":"查页表 · 驻留","active":["va","tlb","pte","pa"],"edges":["lookup","walk","resolved"],
             "values":{"pte":"存在位 1"},"caption":"TLB 未命中后查到页面已驻留，可取得页框号。"},
            {"label":"查页表 · 未驻留","active":["va","tlb","pte","fault"],"edges":["lookup","walk","trap"],
             "values":{"pte":"存在位 0"},"caption":"访问合法页面时，页表显示未驻留才进入缺页处理。"},
            {"label":"调页与重试","active":["fault","fill","retry"],"edges":["load","retry-translation"],
             "caption":"调页成功并更新映射后，按机器的异常返回规则重试。"},
        ],
    }

def tlb_comparison():
    """One public example, with the existing MMU and OS paths shown at different states."""
    model = _paging()
    identifiers = ("tlb_hit", "tlb_miss_present", "tlb_miss_absent", "retry_after_recovery")
    for case, frame in zip(identifiers, model["frames"], strict=True):
        frame["id"] = case
        frame["values"] = {**frame.get("values", {}), "tlb": "命中" if case == "tlb_hit" else "未命中"}
    hit, resident, absent, retry = model["frames"]
    resident["caption"] = "存在位为 1。取页框号，可回填 TLB，页内偏移原样保留。"
    absent["hidden"] = ["pa"]
    absent["caption"] = "存在位为 0。进入缺页处理，当前访问还没有形成物理地址。"
    retry["hidden"] = ["pa"]
    retry["caption"] = "OS 调页成功并更新映射后，重新执行原访问。"
    return model

def compose(model, *, identity, aspect, compact=None, derivation=(), scope=None):
    if model is None:
        raise ValueError("diagram_has_no_verified_mechanism:" + identity)
    if model.get("scenarios") and scope is not None and (scope[0],scope[1]) != ("direct",5):
        raise ValueError("comparison_requires_disclosed_mechanism")
    public = {"model": model, "aspect": aspect, "compact": compact or {},
              "derivation": derivation, "delivery_policy_version": VERSION}
    selected = None
    if scope is not None:
        view, level, version = scope
        selected = selection_binding(identity, version, view, level,
            (frame.get("id", "step-" + str(i+1)) for i, frame in enumerate(model.get("frames", []))), public)
        public["selection"] = selected
    data = json.dumps(public, ensure_ascii=False, separators=(",", ":"))
    uid = "cs408-drawing-" + hashlib.sha256((identity + data).encode()).hexdigest()[:16]
    text = (SHELL.read_text().replace("__CS408_DIAGRAM_STYLE__", STYLE.read_text())
            .replace("__CS408_DIAGRAM_ROOT__", uid)
            .replace("__CS408_OBJECT_RUNTIME__", RUNTIME.read_text())
            .replace("__CS408_RECAP_RUNTIME__", (ASSETS / "recap-mechanisms.js").read_text())
            .replace("__CS408_DIAGRAM_DATA__", data.replace("<", "\\u003c").replace("\u2028", "\\u2028").replace("\u2029", "\\u2029")))
    return text + (marker(selected) if selected else "")
