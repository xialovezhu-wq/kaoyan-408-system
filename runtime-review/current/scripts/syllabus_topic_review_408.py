"""Read-only, source-backed topic review for the local 408 teaching course."""

import hashlib
import json
import re
import unicodedata
from collections import defaultdict
from pathlib import Path

NOTES_REL = Path("support/syllabus-2027/topic-review-notes.json")
ASSET = Path(__file__).resolve().parents[1] / "support/interactive-408/topic-review.html"
MECHANISM_RUNTIME = ASSET.with_name("recap-mechanisms.js")
SCOPE = {"explicit", "within_scope"}
RANK = {"高频": 0, "中频": 1, "低频": 2}
MAP_LIMIT = 4
TEACHER_LANGUAGE = re.compile(r"本轮|本问|当前题|局部修复|首答|提示依赖|个人|本人|兄弟|后继|EXAM408-|(?<![A-Za-z])Q\d|第\s*\d+\s*题")
REVIEW_ID = re.compile(r"EXAM408-\d{4}-Q\d{2}(?:-P\d{2})?|(?:DS|CO|OS|CN)_(?:\d{4}|UNK)_\d{3}")
CONCEPT_ID = re.compile(r"(?:DS|CO|OS|CN)\d{2}-\d{2}")
CHOICE_OR_RESULT = re.compile(r"正确选项|答案(?:是|为|表|胶囊)|候选\s*[A-DＡ-Ｄ]|选项\s*[A-DＡ-Ｄ]|A[.、．开].*B[.、．中]|\d+\s*[=＝]\s*\d+")
ACTION = re.compile(r"^(?:先|从|按|区分|标|读|用|令|找|取|写|列|圈|确定|识别|核对|根据|比较|统计|检查|计算|分离|把)")


def display_review_identity(display):
    if display.get("surface_kind") == "verified_variant":
        point = display.get("concept_id") or ""
        return point if CONCEPT_ID.fullmatch(point) else None
    if display.get("surface_kind") == "formal_original":
        return display.get("formal_node_id") or display.get("source_formal_id")
    return display.get("true_item_id")


def attach_terminal_review(result, identity, *, successor=None):
    """Attach display order after a verified seal, independently of rendering."""
    if (not (REVIEW_ID.fullmatch(identity or "") or CONCEPT_ID.fullmatch(identity or ""))
            or result.get("status") in {"blocked", "episode_supplemented"}
            or result.get("capture_verified") is not True or result.get("package_verified") is not True
            or result.get("annual_binding_verified") is False):
        return result
    successor = successor or {}
    provenance = successor.get("question_provenance") or {}
    candidates = [display_review_identity(successor), provenance.get("true_item_id"),
                  provenance.get("true_exam_id"), provenance.get("based_on_true_exam_id"),
                  successor.get("original_canonical_id")]
    exclusions = list(dict.fromkeys(value for value in candidates if REVIEW_ID.fullmatch(value or "")))
    return {**result, "quick_review": {
        "identity": identity, "withhold_questions": exclusions,
        "requires_current_disclosure_check": True,
        "current_disclosed_only": result.get("terminal_state") == "unresolved",
        "display_order": ["capture_receipt", "brief_framework", "topic_map", "next_display"],
    }}


def topic_root(outline, key):
    while outline[key]["level"] > 2:
        key = outline[key]["parent_id"]
    return key


def _noted(values):
    if len(values) != 3 or not all(isinstance(value, str) and value.strip() for value in values):
        raise ValueError("topic_review_note_requires_rule_condition_boundary")
    return dict(zip(("rule", "condition", "boundary"), values))


def _visual_labels(visual):
    return [label for item in visual.get("items", []) for label in (item if isinstance(item,list) else [item])]


def _valid_map_card(card):
    text_keys = {"label", "cue", "action", "rule", "exam_focus", "summary"}
    if (not {"label","cue","action","rule"} <= card.keys() or not card.keys() <= text_keys | {"visual"}
            or any(not isinstance(card[key],str) or not card[key].strip() or len(card[key])>110
                   for key in card.keys() & text_keys)):
        return False
    visual = card.get("visual")
    if visual is None:
        return True
    if (not isinstance(visual,dict) or set(visual)!={"kind","items"} or visual["kind"] not in {"flow","split"}
            or not isinstance(visual["items"],list) or not 2<=len(visual["items"])<=5
            or any(isinstance(item,list) and (len(item)!=2 or visual["kind"]!="flow") for item in visual["items"])):
        return False
    return all(isinstance(label,str) and 0<len(label)<=20 for label in _visual_labels(visual))


def _case_start(template, codes, primary, points, *, one_part):
    from exam_lesson_408 import objective_text
    first = re.split(r"[。！？；\n]",objective_text(template["steps"][0]),maxsplit=1)[0].strip()
    if one_part and ACTION.match(first) and not CHOICE_OR_RESULT.search(first) and len(first)<=150:
        return first,"exact"
    ordered = sorted(codes,key=lambda code:(code!=primary,code))
    for code in ordered:
        point = points[code]
        if point["syllabus"]["scope_status"] not in SCOPE:
            continue
        for line in point["teacher_steps"]:
            for clause in re.split(r"[。！？；：\n]",objective_text(line.get("teacher_text",""))):
                action=clause.strip()
                if ACTION.match(action) and not re.search(r"\d",action) and not CHOICE_OR_RESULT.search(action):
                    return action,"point_rule"
    return first if first and not CHOICE_OR_RESULT.search(first) else "先标出题面给定对象、条件和所求。","whole_choice"


def compile_material(nodes, mapping, templates, points, notes, reviewed_relations, withheld, diagrams):
    if notes.get("schema") != "cs408-topic-review-notes-v1":
        raise ValueError("topic_review_notes_schema")
    outline = {node["id"]: node for node in nodes}
    if not (set(notes["nodes"]) | set(notes["details"])) <= outline.keys():
        raise ValueError("topic_review_unknown_outline_anchor")
    map_cards = notes.get("map_cards", {})
    if not map_cards.keys() <= outline.keys() or not all(_valid_map_card(card) for card in map_cards.values()):
        raise ValueError("topic_review_invalid_map_card")
    map_branches = notes.get("map_branches", {})
    for sid, children in map_branches.items():
        if sid not in outline or not 1<=len(children)<=MAP_LIMIT or len(children)!=len(set(children)) or (
                not set(children) <= set(outline[sid]["children"])):
            raise ValueError("topic_review_invalid_map_branches:"+sid)
    at_node = defaultdict(list)
    for code, row in points.items():
        scope = row["syllabus"]
        if scope["scope_status"] not in SCOPE:
            continue
        for entry in scope.get("syllabus_entries", []):
            at_node[entry["id"]].append(code)
    assessments, by_node, assessed_by_question, roots = {}, defaultdict(list), defaultdict(set), defaultdict(set)
    anchors_by_question=defaultdict(set)
    from exam_lesson_408 import objective_text
    for parent, question in sorted(mapping["exam_items"].items()):
        units = {row["unit_id"]: row for row in question["frequency_primary_units"]}
        for part in question["assessment_parts"]:
            key = (parent, part["part_id"])
            if key in assessments:
                raise ValueError("topic_review_duplicate_assessment:" + parent)
            suffix = re.split("[:：]", part["part_id"], maxsplit=1)[0]
            exact = parent if parent in units else f"{parent}-{suffix}"
            if exact not in units:
                raise ValueError("topic_review_assessment_unit_missing:" + exact)
            template = templates[exact]
            codes = set(part["canonical_points"])
            anchors = set(part["syllabus_ids"])
            if not anchors or not anchors <= outline.keys():
                raise ValueError("topic_review_assessment_anchor_missing:" + parent)
            assessed_by_question[parent].update(codes)
            assessed_by_question[exact].update(codes)
            action, action_scope = _case_start(template,codes,units[exact]["primary_point"],points,
                                               one_part=exact!=parent or len(question["assessment_parts"])==1)
            given = [objective_text(value) for value in template["given_objects"] if not CHOICE_OR_RESULT.search(value)]
            assessments[key] = {
                "parent": parent, "unit": exact, "part": part["part_id"], "focus": part["focus"],
                "given": list(filter(None,given)), "first_action": action, "first_action_scope": action_scope,
                "form": question["question_type"] if exact == parent else mapping["subquestions"][exact]["question_type"],
                "anchors": sorted(anchors), "points": sorted(codes),
            }
    for amendment in notes.get("exam_links", []):
        key = (amendment["question"], amendment["part"])
        sid = amendment["syllabus_id"]
        record = assessments.get(key)
        if record is None or sid not in outline or sid in record["anchors"] or not amendment.get("basis"):
            raise ValueError("topic_review_reviewed_anchor_invalid")
        record["anchors"].append(sid)
        record.setdefault("supplemental_anchors", {})[sid] = amendment["basis"]
    omitted_anchors=0
    for correction in notes.get("exam_scope_corrections", []):
        key=(correction["question"],correction["part"])
        record=assessments.get(key)
        remove=set(correction.get("remove",[]))
        if record is None or not correction.get("basis") or not remove<set(record["anchors"]):
            raise ValueError("topic_review_scope_correction_invalid:"+str(key))
        record["anchors"]=[sid for sid in record["anchors"] if sid not in remove]
        omitted_anchors+=len(remove)
        if correction.get("first_action"):
            record["first_action"]=correction["first_action"]
            record["first_action_scope"]="exact"
    for record in assessments.values():
        for anchor in record["anchors"]:
            by_node[anchor].append(record)
            root = topic_root(outline, anchor)
            roots[record["parent"]].add(root)
            roots[record["unit"]].add(root)
            anchors_by_question[record["parent"]].add(anchor)
            anchors_by_question[record["unit"]].add(anchor)

    materials = {}
    for node in nodes:
        sid = node["id"]
        notes_for_details = notes["details"].get(sid, [])
        if len(notes_for_details) != len(node["details"]):
            raise ValueError("topic_review_official_detail_coverage:" + sid)
        mechanisms = []
        if sid in notes["nodes"]:
            mechanisms.append({"title": node["title"], **_noted(notes["nodes"][sid])})
        else:
            for code in sorted(set(at_node[sid])):
                item = points[code]
                steps = list(filter(None,(objective_text(line.get("teacher_text","")) for line in item["teacher_steps"])))
                if not steps:
                    continue
                conditions = list(dict.fromkeys(value for line in item["teacher_steps"]
                                                if (value:=objective_text(line.get("causal_bridge",""))) and value not in steps))
                mechanisms.append({"id":code, "title":item["title"], "rule":"\n".join(steps),
                                   "condition":"\n".join(conditions),
                                   "boundary":item["syllabus"]["teaching_boundary"]})
        if not mechanisms and not node["children"]:
            raise ValueError("topic_review_official_leaf_without_material:" + sid)
        if not mechanisms:
            titles = [outline[child]["title"] for child in node["children"]]
            mechanisms.append({"title":node["title"], "rule":"本纲目包含 " + "、".join(titles) + "。",
                               "condition":"按题目实际对象定位相应分面，并核对各分面的条件。", "boundary":""})
        direct = by_node[sid]
        materials[sid] = {
            "mechanisms": mechanisms,
            "details": [{"title":title, **_noted(values), "mechanism":diagrams[f"{sid}#{i}"]}
                        for i,(title,values) in enumerate(zip(node["details"],notes_for_details),1)],
            "mechanism": diagrams[sid],
            "has_assessment": bool(direct),
            "assessments": [record for record in direct if record["parent"] not in withheld],
            "map_card": map_cards.get(sid),
            "map_branches": map_branches.get(sid, []),
        }
    related = defaultdict(list)
    for source, edges in reviewed_relations.items():
        for edge in edges:
            # Official descendants are already in the outline. A second
            # topic needs the separately reviewed conceptual edge and witness.
            if edge.get("review_status") != "reviewed":
                continue
            target = edge["target_point"]
            item = points[target]
            cases = [assessments[(row["exam_id"], row["part_id"])] for row in edge["target_assessments"]
                     if row["exam_id"] not in withheld]
            if not cases:
                continue
            related[source].append({
                "point":target,"title":item["title"],"relation":edge["relation"],"basis":edge["basis"],
                "frequency":item["syllabus"]["exam_frequency"].get("main_frequency_level"),
                "topics":sorted({topic_root(outline, anchor) for case in cases for anchor in case["anchors"]}),
                "cases":cases,
            })
    return {
        "materials": materials,
        "by_question": {key: {"roots": sorted(values), "assessed_points": sorted(assessed_by_question[key]),
                              "anchors":sorted(anchors_by_question[key])}
                        for key, values in roots.items() if key.split("-P")[0] not in withheld},
        "related": dict(related),
        "audit": {"outline_nodes": len(materials), "outline_details":sum(len(row["details"]) for row in materials.values()),
                  "outline_mechanisms": len(materials),
                  "detail_mechanisms":sum(len(row["details"]) for row in materials.values()),
                  "assessment_parts": len(assessments),
                  "reclassified_context_anchors":omitted_anchors,
                  "leaves_without_direct_assessment": sum(not row["children"] and not materials[row["id"]]["has_assessment"] for row in nodes),
                  "reviewed_supplemental_anchors": len(notes.get("exam_links", []))},
    }


def _normal(value):
    return " ".join(unicodedata.normalize("NFC", value).strip().rstrip("。；").split())


def _forms(records, allow, anchor=None):
    groups = {}
    for record in records:
        if not allow(record):
            continue
        key = _normal(record["focus"])
        group = groups.setdefault(key, {"focus":record["focus"],"variants":{}})
        signature = (tuple(_normal(x) for x in record["given"]), _normal(record["first_action"]))
        variant = group["variants"].setdefault(signature, {
            "given":record["given"], "first_action":record["first_action"],
            "first_action_scope":record["first_action_scope"], "sources":[],
        })
        source = {"question":record["unit"],"part":record["part"],"type":record["form"],
                  "points":record.get("points",[])}
        if anchor in record.get("supplemental_anchors", {}):
            source["basis"] = record["supplemental_anchors"][anchor]
        if source not in variant["sources"]:
            variant["sources"].append(source)
    return [{"focus":row["focus"],"variants":list(row["variants"].values())} for row in groups.values()]


def project(roots, source_points, related, get_node, *, exact=None, withheld=(), years=()):
    """Project a separately authorized review; never serialize unrelated teacher objects."""
    from recap_mechanisms_408 import labels as diagram_labels
    excluded = {qid.split("-P")[0] for qid in withheld}
    current_family = exact.split("-P")[0] if exact and "-P" in exact else None
    labels = excluded | ({current_family} if current_family else set())
    for parent in list(labels):
        if not re.fullmatch(r"EXAM408-\d{4}-Q\d{2}", parent):
            continue
        year, number = parent[8:12], int(parent[-2:])
        labels.update({f"{year}Q{number:02d}",f"{year}年Q{number:02d}",f"{year}年第{number}题"})
    year_ref = re.compile("|".join(rf"EXAM408-{year}-Q|{year}\s*(?:年(?:真题|第?\d+题)|Q\d)"
                                   for year in years)) if years else None

    def text_allowed(value):
        return not any(label in value for label in labels) and not (year_ref and year_ref.search(value))

    def allowed(case):
        if case["parent"] in excluded or int(case["parent"][8:12]) in years:
            return False
        if case["parent"] == current_family and case["unit"] != exact:
            return False
        return all(text_allowed(value) for value in [case["focus"], case["first_action"], *case["given"]])

    def sections(sid):
        item = get_node(sid)
        entry, recap = item["entry"], item["review"]
        forms = _forms(recap["assessments"], allowed, sid)
        status = ("有真题考法" if forms else "当前公开范围内暂无可列真题"
                  if recap["has_assessment"] else "当前索引缺少直接真题标注")
        mechanisms = [row for row in recap["mechanisms"] if all(text_allowed(row[field])
                      for field in ("title", "rule", "condition", "boundary"))]
        card = recap.get("map_card")
        card_text = ([value for key,value in card.items() if key!="visual"] +
                     _visual_labels(card.get("visual") or {})) if card else []
        diagram = recap.get("mechanism")
        visible = {"id":sid,"title":entry["title"],"source_line":entry["source_line"],"status":status,
                   "forms":forms,"mechanisms":mechanisms,
                   "details":[detail if all(text_allowed(value) for value in diagram_labels(detail)) else None
                              for detail in recap["details"]],
                   "mechanism":diagram if all(text_allowed(value) for value in diagram_labels(diagram)) else None,
                   "children":entry["children"],"frequency":recap.get("frequency", {}),
                   "map_branches":recap.get("map_branches",[]),
                   "map_card":card if all(text_allowed(value) for value in card_text) else None}
        yield visible
        for child in entry["children"]:
            yield from sections(child)

    topics = [{"id":sid,"title":get_node(sid)["entry"]["title"],"sections":list(sections(sid))}
              for sid in roots]
    adjacent = {}
    for source in source_points:
        for edge in related.get(source, []):
            if set(edge["topics"]) <= set(roots) or not text_allowed(edge["basis"]):
                continue
            cases = _forms(edge["cases"], allowed)
            if cases and edge["point"] not in adjacent:
                adjacent[edge["point"]] = {k:edge[k] for k in ("point","title","relation","basis","frequency")} | {
                    "forms":cases}
    return {"topics":topics,"related":sorted(adjacent.values(),key=lambda row:(RANK.get(row["frequency"],3),row["point"]))}


def chat_overview(review):
    return {
        "考场记忆": [
            {"专题":topic["title"],"考法":[{"纲目":section["title"],"问法":[form["focus"] for form in section["forms"]]}
                       for section in topic["sections"] if section["forms"]]}
            for topic in review["topics"]
        ],
        "知识点记忆": [
            {"专题":topic["title"],"纲目":[{"名称":section["title"],"细项":[x["title"] for x in section["details"] if x],
               "规则":section["mechanisms"][0]["rule"].splitlines()[0] if section["mechanisms"] else "",
               "证据":section["status"]} for section in topic["sections"]]}
            for topic in review["topics"]
        ],
        "直接关联":[row["title"] for row in review["related"]],
    }


def _recap_line(value):
    for sentence in re.split(r"(?<=[。！？])|\n", value):
        sentence = sentence.strip()
        if 0 < len(sentence) <= 110 and not (TEACHER_LANGUAGE.search(sentence) or CHOICE_OR_RESULT.search(sentence)):
            return sentence
    return ""


def _tier(freq):
    value = freq.get("main_frequency_level")
    if value in RANK:
        return value
    if freq.get("secondary_only_count"):
        return "仅兼考"
    return "暂无主考记录" if freq.get("main_count") == 0 else "频次待核"


def compact_map(review, exact=None, *, focus_point=None, preferred=(), overrides=None, precision=None):
    """Show one topic and at most four exam points from its disclosed projection."""
    from recap_precision_408 import current_question_model, focus_model, focused_recall
    overrides = overrides or {}
    precision = precision or {}
    prepared = precision.get("prepared")

    def current_source(ref):
        if exact:
            matches=ref["question"]==exact or ref["question"].startswith(exact+"-P")
            return matches and (not focus_point or focus_point in ref.get("points",[]))
        return focus_point is not None and focus_point in ref.get("points",[])

    def current_form(section):
        for form in section["forms"]:
            if any(current_source(ref) for variant in form["variants"] for ref in variant["sources"]):
                return form
        return None

    def relevant(section):
        return section["id"] in preferred or current_form(section) is not None

    topic = next((topic for topic in review["topics"] if any(s["id"] in preferred for s in topic["sections"])), None)
    topic = topic or next((topic for topic in review["topics"] if any(relevant(s) for s in topic["sections"])),
                          review["topics"][0])
    root = topic["sections"][0]
    branches = [section for section in topic["sections"] if section["id"] in root["children"]]
    primary = next((section for section in [root, *branches] if section["id"] in preferred), None)
    primary = primary or next((section for section in branches if relevant(section)), None)
    layout = root.get("map_branches", [])
    if layout and (not primary or primary["id"] in layout):
        by_id = {section["id"]:section for section in branches}
        branches = [by_id[sid] for sid in layout]
    candidates = branches or [root]
    if primary is root or (primary is None and relevant(root)):
        candidates = [root, *branches]
        primary = root
    primary = primary or candidates[0]

    def rank(section):
        return (section["id"] != primary["id"],
                RANK.get(section["frequency"].get("main_frequency_level"), 3))

    chosen = sorted(sorted(candidates, key=rank)[:MAP_LIMIT], key=candidates.index)
    if exact and "-P" in exact and prepared:
        chosen=[primary]
    cards = []
    for section in chosen:
        exact_form = current_form(section)
        form = exact_form or next(iter(section["forms"]), None)
        exact_points = {point for variant in (exact_form or {}).get("variants",[])
                        for ref in variant["sources"] if current_source(ref)
                        for point in ref.get("points",[])}
        choices = [overrides[point] for point in ([focus_point] if focus_point else sorted(exact_points))
                   if point in overrides and section["id"] in overrides[point]["anchors"]]
        narrow = choices[0] if choices and all(choice["model"]==choices[0]["model"] for choice in choices) else None
        cue = _recap_line(form["focus"]) if form else ""
        variants = (form or {}).get("variants",[])
        if exact_form:
            variants = [variant for variant in variants if any(current_source(ref) for ref in variant["sources"])]
        action = next((line for variant in variants
                       if (line := _recap_line(variant["first_action"]))), "")
        authored = section["map_card"]
        if authored:
            card = {key: authored[key] for key in ("label", "cue", "action", "rule")}
            if authored.get("visual"):
                card["visual"]=authored["visual"]
            if exact and exact_form and exact_form["focus"] != authored.get("exam_focus"):
                card.update(cue=cue, action=action)
        else:
            notes = section["mechanisms"] + [note for note in section["details"] if note]
            focused_notes = [note for note in notes if note.get("id") in
                             ({focus_point} if focus_point else exact_points)]
            if focused_notes:
                notes = focused_notes
            rule = next((line for note in notes if (line := _recap_line(note["rule"]))), "")
            card = {"label": section["title"].removeprefix(topic["title"] + "的"),
                    "cue": cue, "action": action, "rule": rule}
        summary = (authored or {}).get("summary") or cue or card["rule"]
        summary = summary.rstrip("。")
        card["summary"] = summary if len(summary)<=30 else summary[:29]+"…"
        diagram = narrow["model"] if narrow else section.get("mechanism")
        if prepared and prepared["anchor"]==section["id"]:
            diagram=prepared["model"]
            if prepared.get("guidance"):
                card.update(prepared["guidance"])
            elif exact and "-P" in exact:
                card.update(action="",rule="")
        elif exact or precision.get("anchor"):
            diagram=current_question_model(diagram)
        if precision.get("anchor")==section["id"]:
            diagram=focus_model(diagram,precision)
            if precision.get("highlight"):
                card.update(focused_recall(diagram))
            live_recall=precision.get("recall",{})
            if "summary" in live_recall:
                card["cue"]=live_recall["summary"]
            card.update(live_recall)
        if diagram:
            card["mechanism"] = diagram
            card.pop("visual",None)
        title, freq = section["title"], section["frequency"]
        if narrow and focus_point:
            card["label"] = narrow["title"]
            title, freq = narrow["title"], narrow["frequency"]
        cards.append({"id": section["id"], "title": title, "tier": _tier(freq),
                      "main_count": freq.get("main_count"), "secondary_count": freq.get("secondary_only_count"),
                      "current": section["id"] == primary["id"], **card})
    focus = current_form(primary)
    current = next(card for card in cards if card["current"])
    entry = current["cue"] if focus or precision.get("anchor") else current["label"]
    return {"title": topic["title"], "topic_tier": _tier(root.get("frequency",{})), "from_question": bool(exact),
            "entry": entry or current["label"],
            "nodes": cards}


def render_review(review, output, *, exact=None, inline_root=None, **selection):
    recap = compact_map(review, exact, **selection)
    return render_compact(recap, output, inline_root=inline_root)


def render_compact(recap, output, *, inline_root=None):
    from render_interactive_408 import _write_fragment

    raw = json.dumps(recap, ensure_ascii=False, separators=(",", ":"))
    data = raw.replace("<", "\\u003c")
    uid = "cs408-topics-" + hashlib.sha256(raw.encode()).hexdigest()[:16]
    fragment = (ASSET.read_text().replace("__ROOT_ID__", uid).replace("__REVIEW_DATA__", data)
                .replace("__MECHANISM_RUNTIME__", MECHANISM_RUNTIME.read_text()))
    return _write_fragment(fragment, output, inline_root=inline_root) | {
        "recap_overview": {"title": recap["title"], "entry": recap["entry"],
                           "nodes": [{key: node[key] for key in ("label", "tier", "current")} for node in recap["nodes"]]},
    }
