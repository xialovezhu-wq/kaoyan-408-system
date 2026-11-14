#!/usr/bin/env python3
"""Render approved display data in a self-contained Codex Visualize fragment."""
from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
from pathlib import Path
import re
import sys
import tempfile
from typing import NamedTuple

HERE = Path(__file__).resolve().parent
CATALOG = "urn:codex:visual-explanations:1"
A2UI_BUNDLE = HERE / "a2ui.bundle.js"
A2UI_DEFAULT_KINDS = {"sequence", "flow"}
ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,99}")
NAME = re.compile(r"[a-z0-9]+(?:-[a-z0-9]+)*\.html")
LIMIT = 1_000_000


class LocalRenderer(NamedTuple):
    """Trusted, installed code; never constructed from scene JSON."""
    bundle: Path
    styles: Path
    global_name: str
    a2ui_kinds: frozenset[str]


class FragmentTooLarge(ValueError):
    """The rendered file exceeds the inline visualization size limit."""


def require(condition, reason):
    if not condition:
        raise ValueError(reason)


def keys(value, required, optional=()):
    require(isinstance(value, dict) and set(required) <= set(value) <= set(required) | set(optional),
            "invalid visual object fields")


def label(value, limit=320):
    require(isinstance(value, str) and 0 < len(value.strip()) <= len(value) <= limit, "invalid visual label")


def number(value):
    require(type(value) in (int, float) and math.isfinite(value) and abs(value) <= 10**9,
            "invalid axis value")
    return value


def identity(value):
    require(isinstance(value, str) and ID.fullmatch(value), "invalid visual object ID")


def boolean(value):
    require(type(value) is bool, "invalid visual boolean")


def validate_slice(scene):
    keys(scene, ("kind", "title", "control", "xmin", "xmax", "ymin", "ymax", "initial", "step", "samples"),
         ("approximate",))
    if "approximate" in scene:
        boolean(scene["approximate"])
    label(scene["title"], 120)
    label(scene["control"], 80)
    xmin, xmax, ymin, ymax, initial, step = (number(scene[key]) for key in
        ("xmin", "xmax", "ymin", "ymax", "initial", "step"))
    require(xmin < xmax and ymin < ymax and xmin <= initial <= xmax and 0 < step <= xmax - xmin,
            "invalid slice domain")
    for endpoint in (xmax, initial):
        intervals = (endpoint - xmin) / step
        require(math.isfinite(intervals) and intervals <= 100_000 and
                math.isclose(intervals, round(intervals), rel_tol=0, abs_tol=1e-6),
                "slice endpoints and initial value must align to the slider step")
    samples = scene["samples"]
    require(isinstance(samples, list) and 2 <= len(samples) <= 160, "invalid slice samples")
    previous = None
    for point in samples:
        require(isinstance(point, list) and len(point) == 3, "expected [x, lower, upper]")
        x, lower, upper = map(number, point)
        require((previous is None or x > previous) and xmin <= x <= xmax and ymin <= lower <= upper <= ymax,
                "invalid slice point")
        previous = x
    require(samples[0][0] == xmin and samples[-1][0] == xmax, "slice samples must cover the domain")


def validate_timeline(scene):
    keys(scene, ("kind", "title", "initial", "steps", "panels"))
    label(scene["title"], 120)
    steps = scene["steps"]
    require(isinstance(steps, list) and 1 <= len(steps) <= 12, "expected one to twelve steps")
    for step in steps:
        label(step, 120)
    require(type(scene["initial"]) is int and 0 <= scene["initial"] < len(steps), "invalid initial step")
    panels = scene["panels"]
    require(isinstance(panels, list) and 1 <= len(panels) <= 2, "expected one or two panels")
    sequence = scene["kind"] == "sequence"
    for panel in panels:
        keys(panel, ("label", "frames", "lanes") if sequence else ("label", "frames"))
        if panel["label"] or sequence:
            label(panel["label"], 80)
        else:
            require(panel["label"] == "", "invalid panel title")
        if sequence:
            require(isinstance(panel["lanes"], list) and len(panel["lanes"]) == 2, "expected two time lanes")
            for lane in panel["lanes"]:
                label(lane, 36)
        frames = panel["frames"]
        require(isinstance(frames, list) and len(frames) == len(steps), "panels must share the step count")
        for index, frame in enumerate(frames):
            keys(frame, ("fields", "event") if sequence else ("fields",))
            fields = frame["fields"]
            require(isinstance(fields, list) and 1 <= len(fields) <= 8, "expected one to eight fields")
            for field in fields:
                keys(field, ("id", "label", "value", "active"), ("detail", "link"))
                identity(field["id"])
                label(field["label"], 80)
                label(field["value"])
                boolean(field["active"])
                for optional in ("detail", "link"):
                    if optional in field:
                        label(field[optional])
            if sequence and frame["event"] is not None:
                event = frame["event"]
                keys(event, ("id", "label", "from", "lost"), ("detail", "arrives"))
                identity(event["id"])
                label(event["label"], 80)
                require(type(event["from"]) is int and event["from"] in (0, 1), "invalid event direction")
                if event["lost"] is not None:
                    boolean(event["lost"])
                if "arrives" in event:
                    arrives = event["arrives"]
                    require(event["lost"] is False and type(arrives) is int
                            and index < arrives < len(steps), "invalid message arrival step")
                if "detail" in event:
                    label(event["detail"])
    for frames in zip(*(panel["frames"] for panel in panels)):
        objects = ([frame["event"] for frame in frames if frame["event"]] if sequence else
                   [field for frame in frames for field in frame["fields"]])
        ids = [item["id"] for item in objects]
        require(len(ids) == len(set(ids)), "duplicate interactive object ID at one step")


def safe_json(value):
    return (json.dumps(value, ensure_ascii=False, separators=(",", ":"), allow_nan=False)
            .replace("<", "\\u003c").replace("\u2028", "\\u2028").replace("\u2029", "\\u2029"))


def render_fragment(scene, engine, scope, *, local=None):
    if local is None:
        require(isinstance(scene, dict) and scene.get("kind") in {"sequence", "flow", "slice"}, "unsupported visual kind")
        (validate_slice if scene["kind"] == "slice" else validate_timeline)(scene)
    else:
        require(isinstance(local, LocalRenderer) and re.fullmatch(r"[A-Za-z][A-Za-z0-9]*", local.global_name),
                "invalid trusted local renderer")
    require(engine in {"native", "a2ui"}, "unsupported visual renderer")
    uid = "visual-" + hashlib.sha256(safe_json([scope, scene]).encode()).hexdigest()[:18]
    payload = {"title": scene["title"]}
    a2ui_script = ""
    if engine == "a2ui":
        payload["message"] = {
            "version": "1.0",
            "createSurface": {
                "surfaceId": uid,
                "catalogId": CATALOG,
                "components": [
                    {"id": "root", "component": "Stack", "children": ["steps", "mechanism", "detail"]},
                    {"id": "steps", "component": "Steps"},
                    {"id": "mechanism", "component": "Mechanism"},
                    {"id": "detail", "component": "ObjectDetail"},
                ],
                "dataModel": {"scene": scene},
            },
        }
        a2ui_script = "<script>" + A2UI_BUNDLE.read_text(encoding="utf-8") + "</script>"
    else:
        payload["scene"] = scene
    bundle = (local.bundle if local else HERE / "view.bundle.js").read_text(encoding="utf-8")
    styles = local.styles.read_text(encoding="utf-8").replace("__ROOT_ID__", uid) if local else ""
    require(not re.search(r"</script[\s>]", bundle, re.I) and not re.search(r"</style[\s>]", styles, re.I),
            "unsafe inline renderer resources")
    parts = {
        "__ROOT_ID__": uid,
        "__VISUAL_DATA__": safe_json(payload),
        "__VIEW_BUNDLE__": bundle,
        "__VIEW_GLOBAL__": local.global_name if local else "CodexVisualView",
        "__LOCAL_STYLES__": styles,
        "__A2UI_SCRIPT__": a2ui_script,
    }
    template = (HERE / "fragment.html").read_text(encoding="utf-8")
    fragment = re.sub("|".join(parts), lambda match: parts[match.group(0)], template)
    if len(fragment.encode()) >= LIMIT:
        raise FragmentTooLarge("visual fragment exceeds 1 MB")
    return fragment


def write_visual(scene, engine, output, inline_root, *, local=None, scope=""):
    output, inline_root = Path(output), Path(inline_root)
    require(output.is_absolute() and inline_root.is_absolute() and output.parent == inline_root and
            inline_root.is_dir() and output.resolve() == output and NAME.fullmatch(output.name),
            "output must be an HTML file in the current task's existing display directory")
    selected = engine
    if engine == "auto":
        suitable = isinstance(scene, dict) and scene.get("kind") in (local.a2ui_kinds if local else A2UI_DEFAULT_KINDS)
        selected = "a2ui" if suitable and A2UI_BUNDLE.is_file() else "native"
    try:
        fragment = render_fragment(scene, selected, scope + "/" + output.name if scope else output.name, local=local)
    except FragmentTooLarge:
        if engine != "auto" or selected != "a2ui":
            raise
        selected = "native"
        fragment = render_fragment(scene, selected, scope + "/" + output.name if scope else output.name, local=local)
    with tempfile.NamedTemporaryFile(dir=inline_root, mode="w", encoding="utf-8", delete=False) as handle:
        temporary = Path(handle.name)
        try:
            handle.write(fragment)
            handle.close()
            os.replace(temporary, output)
        finally:
            temporary.unlink(missing_ok=True)
    return {"path": str(output), "engine_requested": engine, "engine_selected": selected,
            "client_display_verified": False,
            "visualize_reference": "visualize" + json.dumps({"path": str(output)}, ensure_ascii=False) + ""}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", default="-")
    parser.add_argument("--engine", choices=("auto", "native", "a2ui"), default="auto")
    parser.add_argument("--output", required=True)
    parser.add_argument("--inline-root", required=True)
    args = parser.parse_args()
    scene = json.load(sys.stdin) if args.input == "-" else json.loads(Path(args.input).read_text(encoding="utf-8"))
    print(json.dumps(write_visual(scene, args.engine, args.output, args.inline_root), ensure_ascii=False))


if __name__ == "__main__":
    main()
