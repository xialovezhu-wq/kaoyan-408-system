"""Validate and project 408 teaching scenes before any file or frontend write.

The course compiler and both renderers share this contract. This module has
no UI, lesson lookup, or access to learner records.
"""
from __future__ import annotations

import math
from pathlib import Path
import re
import struct
import zlib

SCHEMA = "cs408-interactive-scene-v1"
ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,99}")
SHA256 = re.compile(r"[0-9a-fA-F]{64}")
MAX_IMAGE_BYTES = 512 * 1024
MAX_IMAGE_PIXELS = 40_000_000
MAX_IMAGE_DIMENSION = 12000


def require(ok, reason):
    if not ok:
        raise ValueError("interactive_scene_" + reason)


def keys(value, allowed):
    require(isinstance(value, dict) and not set(value) - set(allowed), "unknown_or_teacher_fields")


def level(value):
    require(type(value) is int and 0 <= value <= 5, "release_level_required")
    return value


def identifier(value):
    require(isinstance(value, str) and ID.fullmatch(value), "invalid_id")
    return value


def public_scene(raw):
    keys(raw, {"schema", "identity", "source_version", "view", "hint_level", "title", "nodes", "edges",
               "states", "rows", "initial_state", "personal_status", "source_diagrams", "overview", "linear_steps",
               "show_mechanism"})
    require(raw.get("schema") == SCHEMA, "schema_invalid")
    require(raw.get("view") in {"protected", "after_attempt", "direct"}, "view_required")
    allowed = level(raw.get("hint_level"))
    require(raw["view"] != "protected" or allowed == 0, "protected_must_be_level_zero")
    require(raw["view"] == "direct" or allowed < 5, "answer_requires_direct_authorization")
    require(type(raw.get("show_mechanism",False)) is bool, "show_mechanism_invalid")
    identity = raw.get("identity")
    require(isinstance(identity, str) and identity.strip() and len(identity) <= 200, "identity_required")
    require(isinstance(raw.get("source_version"), str) and raw["source_version"], "version_required")

    def text(value):
        keys(value, {"text", "min_level"})
        release = level(value.get("min_level"))
        require(isinstance(value.get("text"), str) and 0 < len(value["text"]) <= 2000, "text_required")
        return value["text"] if release <= allowed else None

    title = text(raw.get("title"))
    require(title is not None, "title_not_public")
    nodes, all_ids = [], set()
    require(isinstance(raw.get("nodes"), list) and 1 <= len(raw["nodes"]) <= 40, "nodes_required")
    for node in raw["nodes"]:
        keys(node, {"id", "label", "detail"})
        nid = identifier(node.get("id"))
        require(nid not in all_ids, "duplicate_node")
        all_ids.add(nid)
        label = text(node.get("label"))
        detail = text(node["detail"]) if node.get("detail") else None
        if label is not None:
            nodes.append({"id": nid, "label": label, "detail": detail})
    ids = {n["id"] for n in nodes}
    require(ids, "no_public_nodes")
    overview, overview_ids = [], set()
    require(isinstance(raw.get("overview", []), list) and len(raw.get("overview", [])) <= 40,
            "overview_invalid")
    for item in raw.get("overview", []):
        keys(item, {"node_id", "label"})
        nid = item.get("node_id")
        require(isinstance(nid, str) and nid in all_ids, "overview_node_missing")
        require(nid not in overview_ids, "duplicate_overview_node")
        overview_ids.add(nid)
        label = text(item.get("label"))
        if nid in ids and label is not None:
            overview.append({"node_id": nid, "label": label})
    diagrams, diagram_ids, hotspot_ids = [], set(), set()
    require(isinstance(raw.get("source_diagrams", []), list) and len(raw.get("source_diagrams", [])) <= 8,
            "source_diagrams_invalid")
    for diagram in raw.get("source_diagrams", []):
        keys(diagram, {"id", "image_path", "sha256", "alt", "min_level", "hotspots"})
        did = identifier(diagram.get("id"))
        require(did not in diagram_ids, "duplicate_diagram")
        diagram_ids.add(did)
        # Pure validation only: no stat/resolve/open here, including for hidden
        # images. Paths and hashes are backend capabilities, never public data.
        path = diagram.get("image_path")
        require(isinstance(path, str) and 0 < len(path) <= 4096 and "\x00" not in path and
                Path(path).is_absolute() and ".." not in Path(path).parts and
                Path(path).suffix.lower() in {".png", ".jpg", ".jpeg"}, "diagram_path_invalid")
        require(isinstance(diagram.get("sha256"), str) and SHA256.fullmatch(diagram["sha256"]),
                "diagram_sha256_invalid")
        visible = level(diagram.get("min_level")) <= allowed
        alt = text(diagram.get("alt"))
        hotspots = diagram.get("hotspots")
        require(isinstance(hotspots, list) and len(hotspots) <= 80, "hotspots_invalid")
        public_hotspots = []
        for hotspot in hotspots:
            keys(hotspot, {"id", "node_id", "rect", "min_level", "display_label"})
            hid = identifier(hotspot.get("id"))
            require(hid not in hotspot_ids, "duplicate_hotspot")
            hotspot_ids.add(hid)
            nid = hotspot.get("node_id")
            require(isinstance(nid, str) and nid in all_ids, "hotspot_node_missing")
            rect = hotspot.get("rect")
            require(isinstance(rect, list) and len(rect) == 4 and
                    all(type(v) in {int, float} and math.isfinite(v) for v in rect), "hotspot_rect_invalid")
            x, y, width, height = rect
            require(0 <= x < 1 and 0 <= y < 1 and 0 < width <= 1 and 0 < height <= 1 and
                    x + width <= 1 and y + height <= 1, "hotspot_rect_out_of_bounds")
            display_label = text(hotspot['display_label']) if hotspot.get('display_label') else None
            if level(hotspot.get("min_level")) <= allowed and nid in ids:
                projected_hotspot = {"id": hid, "node_id": nid, "rect": list(rect)}
                if display_label is not None:
                    projected_hotspot['display_label'] = display_label
                public_hotspots.append(projected_hotspot)
        if visible and alt is not None:
            diagrams.append({"id": did, "alt": alt, "hotspots": public_hotspots})
    edges, all_edges = [], set()
    for edge in raw.get("edges", []):
        keys(edge, {"id", "from", "to", "label", "min_level"})
        eid = identifier(edge.get("id"))
        require(eid not in all_edges, "duplicate_edge")
        all_edges.add(eid)
        require(edge.get("from") in all_ids and edge.get("to") in all_ids, "edge_endpoint_missing")
        label = text(edge.get("label"))
        if level(edge.get("min_level")) <= allowed and label is not None and edge["from"] in ids and edge["to"] in ids:
            edges.append({"id": eid, "from": edge["from"], "to": edge["to"], "label": label})
    edge_ids = {e["id"] for e in edges}
    edge_endpoints = {e["id"]: {e["from"], e["to"]} for e in edges}
    states, state_ids = [], set()
    for state in raw.get("states", []):
        keys(state, {"id", "label", "min_level", "values", "active_nodes", "active_edges"})
        sid = identifier(state.get("id"))
        require(sid not in state_ids, "duplicate_state")
        state_ids.add(sid)
        label = text(state.get("label"))
        values = state.get("values", {})
        require(isinstance(values, dict) and set(values) <= all_ids, "state_values_invalid")
        public_values = {k: v for k, v in ((k, text(v)) for k, v in values.items()) if k in ids and v is not None}
        # An active marker or link can disclose a value even when the value's
        # own text is filtered. Keep those highlights at the same release.
        withheld_values = values.keys() - public_values.keys()
        for field, expected in (("active_nodes", all_ids), ("active_edges", all_edges)):
            require(isinstance(state.get(field, []), list) and set(state.get(field, [])) <= expected,
                    "state_reference_invalid")
        if level(state.get("min_level")) <= allowed and label is not None:
            states.append({"id": sid, "label": label, "values": public_values,
                           "active_nodes": [v for v in state.get("active_nodes", [])
                                            if v in ids and v not in withheld_values],
                           "active_edges": [v for v in state.get("active_edges", [])
                                            if v in edge_ids and not edge_endpoints[v] & withheld_values]})
    linear_steps, linear_ids = [], set()
    require(isinstance(raw.get('linear_steps', []), list) and len(raw.get('linear_steps', [])) <= 40,
            'linear_steps_invalid')
    for step in raw.get('linear_steps', []):
        keys(step, {'id', 'title', 'text', 'return_to_reasoning', 'node_ids', 'min_level'})
        sid = identifier(step.get('id'))
        require(sid not in linear_ids, 'duplicate_linear_step')
        linear_ids.add(sid)
        refs = step.get('node_ids', [])
        require(isinstance(refs, list) and set(refs) <= all_ids, 'linear_step_node_missing')
        step_title, body = text(step.get('title')), text(step.get('text'))
        back = text(step['return_to_reasoning']) if step.get('return_to_reasoning') else None
        if level(step.get('min_level')) <= allowed and step_title is not None and body is not None:
            linear_steps.append({'id': sid, 'title': step_title, 'text': body,
                                 'return_to_reasoning': back, 'node_ids': [v for v in refs if v in ids]})
    # Optional layout covers the original node set before filtering. Chunking
    # keeps omitted rows valid for scenes larger than the six-column limit.
    ordered_ids = [n["id"] for n in raw["nodes"]]
    rows = raw.get("rows", [ordered_ids[i:i + 6] for i in range(0, len(ordered_ids), 6)])
    require(isinstance(rows, list) and all(isinstance(row, list) and 1 <= len(row) <= 6 for row in rows), "rows_invalid")
    flattened = [v for row in rows for v in row]
    require(len(set(flattened)) == len(flattened) and set(flattened) == all_ids, "layout_node_coverage")
    rows = [[nid for nid in row if nid in ids] for row in rows]
    # A state can remain folded only after it passed this allowlist. There is no
    # frontend reveal-all switch and no raw source object in the output.
    initial = raw.get("initial_state")
    require(initial is None or initial in state_ids, "initial_state_invalid")
    if initial not in {s["id"] for s in states}:
        initial = states[0]["id"] if states else None
    require(any(n.get("detail") for n in nodes) or edges or len(states) > 1, "no_meaningful_interaction")
    return {"schema": SCHEMA, "identity": identity, "source_version": raw["source_version"],
            "title": title, "nodes": nodes, "edges": edges, "rows": [r for r in rows if r],
            "states": states, "initial_state": initial, "overview": overview, "source_diagrams": diagrams,
            "linear_steps": linear_steps, "linear_text_complete": bool(linear_steps),
            "show_mechanism":raw.get("show_mechanism",False)}


def _image_geometry(content):
    """Identify a bounded raster without a decoder or external dependency."""
    if content.startswith(b"\x89PNG\r\n\x1a\n"):
        require(len(content) >= 45 and content[8:16] == b"\x00\x00\x00\rIHDR", "diagram_image_invalid")
        require(zlib.crc32(content[12:29]) & 0xffffffff == struct.unpack(">I", content[29:33])[0],
                "diagram_image_invalid")
        require(content[-12:] == b"\x00\x00\x00\x00IEND\xaeB`\x82", "diagram_image_invalid")
        width, height = struct.unpack(">II", content[16:24])
        mime = "image/png"
    elif content.startswith(b"\xff\xd8"):
        require(content.endswith(b"\xff\xd9"), "diagram_image_invalid")
        cursor, geometry = 2, None
        while cursor < len(content) - 2:
            require(content[cursor] == 0xff, "diagram_image_invalid")
            while cursor < len(content) and content[cursor] == 0xff:
                cursor += 1
            require(cursor < len(content), "diagram_image_invalid")
            marker = content[cursor]; cursor += 1
            if marker in {0xd9, 0xda}:
                break
            if marker in {0x01, *range(0xd0, 0xd8)}:
                continue
            require(cursor + 2 <= len(content), "diagram_image_invalid")
            size = struct.unpack(">H", content[cursor:cursor + 2])[0]
            require(size >= 2 and cursor + size <= len(content), "diagram_image_invalid")
            if marker in {0xc0, 0xc1, 0xc2, 0xc3, 0xc5, 0xc6, 0xc7, 0xc9, 0xca, 0xcb, 0xcd, 0xce, 0xcf}:
                require(size >= 8, "diagram_image_invalid")
                height, width = struct.unpack(">HH", content[cursor + 3:cursor + 7])
                geometry = (width, height)
            cursor += size
        require(geometry is not None, "diagram_image_invalid")
        width, height = geometry
        mime = "image/jpeg"
    else:
        raise ValueError("interactive_scene_diagram_image_type_invalid")
    require(0 < width <= MAX_IMAGE_DIMENSION and 0 < height <= MAX_IMAGE_DIMENSION and
            width * height <= MAX_IMAGE_PIXELS, "diagram_image_dimensions_invalid")
    return mime, width, height


