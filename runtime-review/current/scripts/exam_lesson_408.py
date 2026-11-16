"""Compile an exact, source-bound 408 lesson for an authorized full explanation."""

import hashlib
import io
import json
import re
from pathlib import Path

from exam_figure_binding_408 import validate_binding
from public_scene_408 import MAX_IMAGE_BYTES, SCHEMA, _image_geometry, public_scene
from source_visual_408 import MANIFEST_REL, _atomic

EXAM = re.compile(r"EXAM408-(\d{4})-Q\d{2}(?:-P\d{2})?")
FIGURE_BASES = {"current_question_figure", "current_question_table"}
IMAGE_BUDGET = 620 * 1024
REGIONS_REL = Path("support/interactive-408/reviewed-exam-regions.json")
PERSONAL_TEXT = re.compile(r"本人已|用户首答|用户原话|个人摘要|真实断点|提示后修复|用户的首答")


def _hash(raw):
    return hashlib.sha256(raw).hexdigest()


def _exact_file(root, ref):
    path = root / ref
    if Path(ref).is_absolute() or not path.resolve().is_relative_to(root.resolve()):
        raise ValueError("exam_lesson_asset_path_outside_corpus")
    return path


def _source_asset(path, role, sha=None, label=None):
    return {"path": str(path), "role": role, "sha256": sha, "label": label}


def _part_sources(details, identity, small, plan):
    # The teacher binding can refer to the parent, even for a correct exact
    # template. The independent payload is the authority for display assets.
    scoped = [
        _source_asset(item["path"], "condition_figure", item["sha256"], item.get("title"))
        for item in plan.get("assets", [])
        if item.get("publication_scope") == "only_current_subquestion_conditions"
        and item["asset_id"].startswith(identity + ":")
        and item.get("role") in {"current_subquestion_condition_figure", "question_figure"}
    ]
    if scoped:
        return scoped
    payload_path = _exact_file(details, small["payload_path"])
    raw = payload_path.read_bytes()
    if _hash(raw) != small["payload_sha256"]:
        raise ValueError("exam_lesson_part_payload_drift:" + identity)
    payload = json.loads(raw)
    for field in ("true_item_id", "subquestion_id"):
        if payload.get(field) is not None and payload[field] != identity:
            raise ValueError("exam_lesson_part_payload_identity:" + identity)
    scoped = [
        _source_asset(_exact_file(details, item), "condition_figure")
        for item in payload.get("condition_assets", [])
    ]
    if scoped:
        return scoped
    return [
        _source_asset(_exact_file(details, item["ref"]), "condition_surface", item["sha256"])
        for item in small["question_asset_refs"]
    ]


def _picture_sources(details, identity, small, plan, binding):
    basis = binding["basis"]
    if small:
        return _part_sources(details, identity, small, plan) if basis in FIGURE_BASES else []
    roles = {"question_figure", "question_surface"} if basis in FIGURE_BASES else {"solution_diagram"}
    named = {item["sha256"]: item.get("title") for item in plan.get("assets", [])}
    return [
        _source_asset(item["path"], item["role"], item["sha256"], named.get(item["sha256"]))
        for item in binding["source_assets"]
        if item["role"] in roles and item.get("question_identity", identity) == identity
    ]


def _geometry(repo, identity, plan):
    # Rebind only geometric coordinates and source labels. In particular,
    # never restore retired explanations, transitions or teaching designs.
    candidates = [plan]
    previous = plan.get("historical_plan_ref") or {}
    sha = previous.get("sha256", "")
    if re.fullmatch(r"[0-9a-f]{64}", sha) and previous.get("ref") == f"objects/{sha}.json":
        raw = (repo / MANIFEST_REL.parent / previous["ref"]).read_bytes()
        if _hash(raw) != sha:
            raise ValueError("exam_lesson_geometry_hash_drift:" + identity)
        old = json.loads(raw)
        if old.get("identity") == identity:
            candidates.append(old)
    result = {}
    for candidate in candidates:
        assets = {item["asset_id"]: item["sha256"] for item in candidate.get("assets", [])}
        for region in candidate.get("region_bindings", []):
            digest = assets.get(region["asset_id"])
            if digest:
                result.setdefault(digest, {})[region["region_id"]] = (region["label"], region["rect"])
    return result


def reviewed_geometry(data, identity, parent_regions):
    result = {sha: list(items.values()) for sha, items in parent_regions.items()}
    for sha, record in data.get("assets", {}).items():
        if identity in record["identities"]:
            result[sha] = record["regions"]
    return result


def _display_copy(repo, raw, sha, budget):
    if len(raw) <= budget:
        return None
    # Large or multi-page captures are staged once as bounded, geometrically
    # equivalent copies. Their normalized hotspot coordinates stay unchanged.
    from PIL import Image
    with Image.open(io.BytesIO(raw)) as source:
        bitmap = source.convert("RGB")
    for width in (1600, 1280, 1024, 900, 760, 640):
        view = bitmap.copy()
        view.thumbnail((width, width * 2), Image.Resampling.LANCZOS)
        buffer = io.BytesIO()
        view.save(buffer, "JPEG", quality=87, optimize=True)
        content = buffer.getvalue()
        if len(content) <= budget:
            content_sha = _hash(content)
            path = repo / ".private" / "exam-lesson-display-408" / sha / (content_sha + ".jpg")
            if not path.is_file():
                path.parent.mkdir(parents=True, exist_ok=True)
                _atomic(path, content)
            if _hash(path.read_bytes()) != content_sha:
                raise ValueError("exam_lesson_display_copy_drift")
            return str(path), content_sha
    raise ValueError("exam_lesson_image_over_budget")


def _pictures(repo, sources, *, checked_images, with_display_copies):
    result = []
    budget = min(MAX_IMAGE_BYTES, IMAGE_BUDGET // max(1, len(sources)))
    for item in sources:
        key = (item["path"], item["sha256"], budget, with_display_copies)
        if key not in checked_images:
            raw = Path(item["path"]).read_bytes()
            sha = _hash(raw)
            if item["sha256"] and sha != item["sha256"]:
                raise ValueError("exam_lesson_image_hash_drift:" + item["path"])
            _image_geometry(raw)
            copy = _display_copy(repo, raw, sha, budget) if with_display_copies else None
            checked_images[key] = (sha, copy or (item["path"], sha))
        sha, (path, display_sha) = checked_images[key]
        if any(asset["source_sha256"] == sha for asset in result):
            continue
        result.append(item | {"source_sha256": sha, "image_path": path, "display_sha256": display_sha})
    return result


def _label_matches(label, text):
    label = re.sub(r"\s+", "", label).casefold()
    text = re.sub(r"\s+", "", text).casefold()
    if label in text:
        return True
    identifiers = re.findall(r"[a-z][a-z0-9]{1,}", label)
    if any(re.search(rf"(?<![a-z]){word}(?![a-z])", text) for word in identifiers):
        return True
    # A multi-object label can identify one of its literal named objects.
    return any(len(word) >= 2 and word in text for word in re.split(r"[、/／与和及]", label))


def objective_text(text):
    return "".join(line for line in re.split(r"(?<=[。！？；\n])", text) if not PERSONAL_TEXT.search(line)).strip()


def compile_exact(repo, details, identity, template, plan, small, *, checked_images,
                  with_display_copies=True, reviewed_regions=None, step_regions=None, parts=()):
    if not EXAM.fullmatch(identity) or template.get("question_identity") != identity or plan.get("identity") != identity:
        raise ValueError("exam_lesson_identity_mismatch:" + identity)
    if bool(small) != ("-P" in identity):
        raise ValueError("exam_lesson_part_scope_mismatch:" + identity)
    if any(small or not part["question_identity"].startswith(identity + "-P") for part in parts):
        raise ValueError("exam_lesson_parent_part_scope_mismatch:" + identity)
    steps = [objective_text(step) for step in template["steps"]]
    binding = validate_binding(plan["exam_visual_binding"], steps=steps)
    numbered_steps = [(i,step) for i,step in enumerate(steps) if step]
    if not numbered_steps:
        raise ValueError("exam_lesson_reasoning_requires_objective_text:" + identity)
    sources = _picture_sources(details, identity, small, plan, binding)
    if binding["basis"] in FIGURE_BASES and not sources:
        raise ValueError("exam_lesson_figure_unavailable:" + identity)
    pictures = _pictures(repo, sources, checked_images=checked_images, with_display_copies=with_display_copies)
    original = binding["basis"] in FIGURE_BASES and any(item["role"] != "condition_surface" for item in pictures)
    geometry = _geometry(repo, identity, plan) if pictures else {}
    for sha, regions in (reviewed_regions or {}).items():
        if not geometry.get(sha):
            geometry[sha] = {f"reviewed-{i}": tuple(region) for i, region in enumerate(regions)}
    focus = {int(row["step_id"][1:]) - 1: row for row in binding["focus_by_step"]}
    given = list(dict.fromkeys(filter(None,map(objective_text,template.get("given_objects", [])))))
    target = objective_text(template["target"])
    checks = list(filter(None,map(objective_text,template.get("checks", []))))
    source_status = template["source_review_status"]

    def atom(value):
        return {"text": value, "min_level": 5}

    def stage_matches(name,sha,step_index,text):
        explicit=(step_regions or {}).get(str(step_index+1))
        if explicit is not None:
            return name in explicit.get(sha, [])
        return _label_matches(name,text)

    nodes, diagrams, by_node = [], [], {}
    captions = {"question_figure": "真题原图", "question_surface": "真题题面原件",
                "condition_figure": "本小问已给图形条件", "condition_surface": "本小问题面与已给条件",
                "solution_diagram": "来源解析图"}
    for position, item in enumerate(pictures, 1):
        label = captions[item["role"]]
        caption = label + (f" · {item['label']}" if item.get("label") else f" · {position}")
        did = f"figure-{position}"
        hotspots = []
        for index, (name, rect) in enumerate(geometry.get(item["source_sha256"], {}).values(), 1):
            nid = f"{did}-object-{index}"
            sha=item["source_sha256"]
            matching = [f"第 {number} 步 · {step}" for number,(i,step) in enumerate(numbered_steps,1)
                        if stage_matches(name,sha,i,step+" "+objective_text(focus.get(i,{}).get("what_to_show","")))]
            detail = "\n".join(matching) if matching else f"按原图读取{name}所在的字段或区域。"
            nodes.append({"id": nid, "label": atom(name), "detail": atom(detail)})
            by_node[nid] = (name,sha)
            hotspots.append({"id": f"{nid}-hit", "node_id": nid, "rect": rect, "min_level": 5})
        diagrams.append({"id": did, "image_path": item["image_path"], "sha256": item["display_sha256"],
                         "alt": atom(caption), "min_level": 5, "hotspots": hotspots})

    # An authored sequence is an order of deductions, not an inferred
    # physical connection. On a figure, the state highlights exact labels
    # against source geometry. Text-only questions show the order separately.
    first_nodes = []
    for i, value in enumerate(given):
        nid = f"given-{i+1}"
        first_nodes.append(nid)
        affected = [step for step in steps if _label_matches(value, step)]
        nodes.append({"id": nid, "label": atom(value),
                      "detail": atom("\n".join(affected) if affected else "核对该对象在本题所求中的作用。")})
    rows = [first_nodes[i:i+4] for i in range(0, len(first_nodes), 4)]
    first_text = "已知 " + "、".join(given) + "。\n" if given else ""
    initial = objective_text(template.get("initial_state") or "")
    if initial:
        first_text += "起始条件或检查基准 · " + initial + "。\n"
    first_text += "所求 · " + target
    if source_status != "reviewed":
        first_text += "\n当前原件的结论存在冲突，按下方机制逐项核对。"
    linear = [{"id": "starting", "title": atom("先核对对象和所求"), "text": atom(first_text),
               "node_ids": first_nodes, "min_level": 5}]
    states = []
    edges = []
    for number,(i,step) in enumerate(numbered_steps,1):
        sid = f"step-{number}"
        visual = focus.get(i, {})
        instruction = objective_text(visual.get("what_to_show", ""))
        change = objective_text(visual.get("state_change", ""))
        if change.startswith("围绕本步骤对应"):
            change = ""
        explanation = list(dict.fromkeys(x for x in [step, instruction, change] if x))
        spoken = " ".join(explanation)
        matched = [nid for nid,(name,sha) in by_node.items() if stage_matches(name,sha,i,spoken)]
        matched += [nid for nid, value in zip(first_nodes, given) if _label_matches(value, spoken)]
        nodes.append({"id": sid, "label": atom(f"第 {number} 步"), "detail": atom("\n".join(explanation))})
        rows.append([sid])
        linked_edges = []
        if number>1 and not original:
            edge_id = f"order-{number}"
            edges.append({"id": edge_id, "from": f"step-{number-1}", "to": sid,
                          "label": atom("接续推理"), "min_level": 5})
            linked_edges.append(edge_id)
        states.append({"id": sid, "label": atom(f"第 {number} 步"), "min_level": 5,
                       "values": {sid: atom("\n".join(explanation))},
                       "active_nodes": [sid, *matched], "active_edges": linked_edges})
        item = {"id": sid, "title": atom(f"第 {number} 步"), "text": atom(step),
                "node_ids": [*matched, sid], "min_level": 5}
        back = objective_text(visual.get("return_to_reasoning", ""))
        if back and back not in (step, target):
            item["return_to_reasoning"] = atom(back)
        linear.append(item)
    if checks:
        nodes.append({"id": "checks", "label": atom("最后核对"), "detail": atom("\n".join(checks))})
        rows.append(["checks"])
        linear.append({"id": "checks", "title": atom("最后核对"), "text": atom("\n".join(checks)),
                       "node_ids": ["checks"], "min_level": 5})
    for part in parts:
        label = part.get("source_part_label") or part["question_identity"].rsplit("-", 1)[-1]
        linear.append({"id": part["question_identity"], "title": atom("小问 " + label),
                       "text": atom(objective_text(part["target"]) + "\n" + "\n".join(filter(None,map(objective_text,part["steps"])))),
                       "node_ids": [], "min_level": 5})
    figure_nodes = list(by_node)
    rows += [figure_nodes[i:i+4] for i in range(0, len(figure_nodes), 4)]
    title = identity + (" · 原图与逐步讲解" if original else " · 机制示意与逐步讲解")
    scene = {"schema": SCHEMA, "identity": identity, "source_version": "pending_course_version",
             "view": "direct", "hint_level": 5, "title": atom(title),
             "nodes": nodes, "source_diagrams": diagrams, "rows": rows, "edges": edges,
             "linear_steps": linear, "states": states, "show_mechanism":bool(diagrams and not original)}
    public_scene(scene)
    return {"scene": scene, "basis": binding["basis"], "source_review_status": source_status,
            "pictures": pictures, "hotspot_count": sum(len(fig["hotspots"]) for fig in diagrams),
            "diagram_count": len(diagrams), "display_roles": sorted({item["role"] for item in pictures}),
            "source_objects_covered": sum(bool(fig["hotspots"]) for fig in diagrams),
            "focused_steps": sum(bool(set(s["active_nodes"]) & by_node.keys()) for s in states)}


def verify_pictures(lesson):
    for item in lesson["pictures"]:
        if _hash(Path(item["path"]).read_bytes()) != item["source_sha256"]:
            raise ValueError("exam_lesson_bound_picture_changed:" + item["path"])
