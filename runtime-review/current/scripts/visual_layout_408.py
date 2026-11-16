"""Convert an already filtered 408 scene into the shared Visualize data model."""
from importlib.util import module_from_spec, spec_from_file_location
import json
from pathlib import Path
import runtime_paths_408 as paths


class SharedRendererUnavailable(ValueError):
    """The optional installed renderer or its required assets are absent."""


def exact_layout(repo, identity, unit, source_version):
    """Read one optional reviewed layout already bound to this admitted unit."""
    if (unit.get("kind") != "exam_unit" or unit.get("id") != identity
            or (unit.get("visual") or {}).get("basis") != "text_only_mechanism"):
        return None
    path = repo / "support/interactive-408/mechanisms/exact-scenes" / (identity + ".json")
    if not path.is_file():
        return None
    value = json.loads(path.read_bytes())
    if set(value) != {"template_sha256", "plan_sha256", "syllabus_id", "scene", "layout"}:
        raise ValueError("invalid_exact_mechanism_fields")
    scene = value["scene"]
    if (scene.get("identity"), scene.get("view"), scene.get("hint_level")) != (identity, "direct", 5):
        raise ValueError("invalid_exact_mechanism_scope")
    if (not value["template_sha256"] or not value["plan_sha256"]
            or value["template_sha256"] != unit.get("base_template_hash")
            or value["plan_sha256"] != unit.get("base_plan_hash")
            or value["syllabus_id"] not in unit.get("syllabus_ids", [])):
        return None
    return {**scene, "source_version": source_version}, value["layout"]


def shared_model(scene, layout):
    """Build shared UI data from the authorized 408 projection."""
    if scene["source_diagrams"]:
        raise ValueError("Original figure hotspots must use the existing 408 renderer")
    if not scene["states"]:
        raise ValueError("A shared mechanism view requires public states")
    kind = layout.get("kind")
    if kind not in ("sequence", "flow") or set(layout) != {"kind", "panels"}:
        raise ValueError("Invalid 408 visual layout")
    if not isinstance(layout["panels"], list) or not 1 <= len(layout["panels"]) <= 2:
        raise ValueError("Expected one or two 408 visual panels")

    nodes = {node["id"]: node for node in scene["nodes"]}
    edges = {edge["id"]: edge for edge in scene["edges"]}
    step_ids = [state["id"] for state in scene["states"]]
    panels = []
    for panel in layout["panels"]:
        expected = {"heading", "fields", "lanes", "events", "note", "status"} if kind == "sequence" else {"heading", "fields"}
        optional = {"arrivals"} if kind == "sequence" else set()
        if not isinstance(panel, dict) or not expected <= set(panel) <= expected | optional:
            raise ValueError("Unexpected 408 visual panel fields")
        heading = nodes.get(panel["heading"])
        fields = [nodes[nid] for nid in panel["fields"] if nid in nodes]
        if not heading or not fields:
            continue
        entry = {"label": heading["label"], "frames": []}
        if kind == "sequence":
            lane_ids = panel["lanes"]
            if len(lane_ids) != 2 or len(set(lane_ids)) != 2 or any(nid not in nodes for nid in lane_ids):
                continue
            entry["lanes"] = [nodes[nid]["label"] for nid in lane_ids]
            allowed_events = set(panel["events"])
            arrivals = panel.get("arrivals", {})
            if (not isinstance(arrivals, dict) or not arrivals.keys() <= allowed_events
                    or any(not isinstance(sid, str) for sid in arrivals.values())):
                raise ValueError("Message arrivals must reference a state ID")
        for step, state in enumerate(scene["states"]):
            active_edges = [edges[eid] for eid in state["active_edges"]]
            shown_fields = []
            for index, node in enumerate(fields):
                nid = node["id"]
                item = {"id": nid, "label": node["label"], "value": state["values"].get(nid, "—"),
                        "active": nid in state["active_nodes"]}
                if node["detail"]:
                    item["detail"] = node["detail"]
                if kind == "flow" and index + 1 < len(fields):
                    to_id = fields[index + 1]["id"]
                    path = next((edge for edge in active_edges if
                                 edge["from"] == nid and edge["to"] == to_id), None)
                    if path:
                        item["link"] = path["label"]
                shown_fields.append(item)
            frame = {"fields": shown_fields}
            if kind == "sequence":
                state_events = [edge for edge in active_edges if edge["id"] in allowed_events]
                if len(state_events) > 1:
                    raise ValueError("Use one message per panel per state")
                frame["event"] = None
                if state_events:
                    event = state_events[0]
                    if {event["from"], event["to"]} != set(lane_ids):
                        raise ValueError("Message endpoints must match the two time lanes")
                    status = state["values"].get(panel["status"])
                    frame["event"] = {"id": event["id"], "label": event["label"],
                                      "from": lane_ids.index(event["from"]),
                                      "lost": {"丢失": True, "到达": False}.get(status)}
                    if event["id"] in arrivals and frame["event"]["lost"] is False:
                        target = arrivals[event["id"]]
                        if target in step_ids:
                            arrives = step_ids.index(target)
                            if arrives <= step:
                                raise ValueError("Arrival must follow the send step")
                            frame["event"]["arrives"] = arrives
                        else:
                            # A private or absent arrival step cannot turn into
                            # a landed arrow merely because the send was public.
                            frame["event"]["lost"] = None
                    note = state["values"].get(panel["note"])
                    if note:
                        frame["event"]["detail"] = note
            entry["frames"].append(frame)
        panels.append(entry)
    if not panels:
        raise ValueError("No panels survive answer protection")
    return {
        "kind": kind, "title": scene["title"], "initial": step_ids.index(scene["initial_state"]),
        "steps": [state["label"] for state in scene["states"]], "panels": panels,
    }


def render(scene, layout, engine, output, inline_root, *, fallback=None):
    model = shared_model(scene, layout)
    try:
        return _shared_renderer(model, engine, output, inline_root)
    except SharedRendererUnavailable:
        if engine == "auto" and fallback is not None:
            return fallback()
        raise


def compose(scene, layout, scope, *, engine="auto"):
    """Prebuild a fragment with the same renderer used by live exact lessons."""
    model = shared_model(scene, layout)
    module = shared_renderer_module()
    if engine == "auto":
        engine = "a2ui" if module.A2UI_BUNDLE.is_file() else "native"
    try:
        return module.render_fragment(model, engine, scope), engine
    except module.FragmentTooLarge:
        if engine != "a2ui":
            raise
        return module.render_fragment(model, "native", scope), "native"


def shared_renderer_module():
    path = paths.codex_home() / "skills/_shared/visual-explanations/render.py"
    if not path.is_file():
        raise SharedRendererUnavailable("Shared Visualize renderer unavailable")
    spec = spec_from_file_location("shared_codex_visual_explanations", path)
    if spec is None or spec.loader is None:
        raise SharedRendererUnavailable("Shared Visualize renderer unavailable")
    module = module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _shared_renderer(scene, engine, output, inline_root):
    try:
        return shared_renderer_module().write_visual(scene, engine, output, inline_root)
    except FileNotFoundError as exc:
        if exc.filename and Path(exc.filename).parent == paths.codex_home() / "skills/_shared/visual-explanations":
            raise SharedRendererUnavailable("Shared Visualize assets unavailable") from exc
        raise
