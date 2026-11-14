#!/usr/bin/env python3
"""Compile exactly one authorized math explanation into a local Visualize fragment."""
from __future__ import annotations

import argparse
import json
import math
from pathlib import Path
import re
import sys

ASSETS = Path(__file__).resolve().parent
sys.path.insert(0, str(ASSETS.parent))
import render as shared_renderer
TOKEN = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,79}")
AXIS = re.compile(r"[A-Za-z][A-Za-z0-9₀-₉′']{0,7}")
INK = {"a", "b", "result", "neutral"}
INPUT_LIMIT = 160_000
VIEWS = {"protected", "after_attempt", "direct"}


def require(condition, reason):
    if not condition:
        raise ValueError(reason)


def fields(value, required, optional=()):
    require(isinstance(value, dict) and set(required) <= value.keys() <= set(required) | set(optional),
            "unexpected math visual fields")


def text(value, size=90):
    require(isinstance(value, str) and 0 < len(value.strip()) <= len(value) <= size
            and not re.search(r"[$<>\\\x00-\x1f]", value), "plain visual label required; keep formulas in chat")


def number(value):
    require(type(value) in (int, float) and abs(value) <= 10**7 and math.isfinite(value),
            "finite coordinate required")
    return value


def axes(value):
    require(isinstance(value, list) and len(value) == 2
            and all(isinstance(x, str) and AXIS.fullmatch(x) for x in value)
            and len(set(value)) == 2, "use the two current axis symbols")


def bounds(value):
    require(isinstance(value, list) and len(value) == 4, "bounds must be [xmin,xmax,ymin,ymax]")
    xmin, xmax, ymin, ymax = map(number, value)
    require(xmin < xmax and ymin < ymax, "nonempty plotting limits required")
    return ((xmin, xmax), (ymin, ymax))


def optional_flag(scene, key):
    if key in scene:
        require(type(scene[key]) is bool, "expected a boolean")


def points(value, limits, low, high):
    require(isinstance(value, list) and low <= len(value) <= high, "invalid plot points")
    for point in value:
        require(isinstance(point, list) and len(point) == 2, "expected [x,y]")
        for coord, (a, b) in zip(point, limits):
            require(a <= number(coord) <= b, "coordinates outside the plotting window")


def name(value):
    require(isinstance(value, str) and TOKEN.fullmatch(value), "invalid math object ID")


def ordered_rows(items, low, high, validator):
    require(isinstance(items, list) and low <= len(items) <= high, "invalid visual object count")
    by_id = {}
    for row in items:
        validator(row)
        name(row["id"])
        require(row["id"] not in by_id, "duplicate math object ID")
        text(row["label"])
        by_id[row["id"]] = row
    return by_id


def interpolate(curve, t, axis):
    transverse = 1-axis
    for a, b in zip(curve, curve[1:]):
        if a[axis] <= t <= b[axis]:
            if t == a[axis]:
                return a[transverse]
            if t == b[axis]:
                return b[transverse]
            return a[transverse] + (t-a[axis])/(b[axis]-a[axis])*(b[transverse]-a[transverse])
    raise ValueError("band outside its boundary samples")


def validate_region(scene):
    fields(scene, ("kind", "title", "axes", "bounds", "sweep", "boundaries", "bands"),
           ("stops", "windowed", "approximate", "open_endpoints"))
    limits = bounds(scene["bounds"])
    sweep = scene["sweep"]
    fields(sweep, ("axis", "initial", "step"))
    require(sweep["axis"] in ("x", "y"), "use x or y as the fixed plotting axis")
    axis = 0 if sweep["axis"] == "x" else 1
    minimum, maximum = limits[axis]
    span = maximum-minimum
    require(minimum <= number(sweep["initial"]) <= maximum and
            span/100_000 <= number(sweep["step"]) <= span, "invalid sweep range or keyboard step")
    for key in ("windowed", "approximate", "open_endpoints"):
        optional_flag(scene, key)

    def boundary(row):
        fields(row, ("id", "label", "points"))
        curve = row["points"]
        points(curve, limits, 2, 160)
        require(all(a[axis] < b[axis] for a, b in zip(curve, curve[1:])),
                "each boundary must be single-valued and monotone in its scan coordinate")

    curves = {key: row["points"] for key, row in
              ordered_rows(scene["boundaries"], 1, 8, boundary).items()}
    bands = scene["bands"]
    require(isinstance(bands, list) and 1 <= len(bands) <= 12, "expected 1–12 scan bands")
    previous = None
    for band in bands:
        fields(band, ("from", "to", "lower", "upper"))
        a, b = number(band["from"]), number(band["to"])
        require(minimum <= a < b <= maximum and (previous is None or previous["to"] <= a),
                "bands must be sorted and cannot overlap")
        for side in ("lower", "upper"):
            name(band[side])
            require(band[side] in curves, "band needs an existing boundary")
        low, high = (curves[band[side]] for side in ("lower", "upper"))
        knots = {a, b} | {p[axis] for curve in (low, high) for p in curve if a < p[axis] < b}
        require(all(interpolate(low, t, axis) <= interpolate(high, t, axis) for t in knots),
                "lower boundary is above upper boundary between samples")
        if previous and previous["to"] == a:
            require(all(math.isclose(interpolate(curves[previous[side]], a, axis),
                                     interpolate(curves[band[side]], a, axis),
                                     rel_tol=1e-10, abs_tol=1e-10)
                        for side in ("lower", "upper")), "slice is ambiguous at a shared band seam")
        previous = band
    stops = scene.get("stops", [])
    require(isinstance(stops, list) and len(stops) <= 8, "too many key stops")
    last = None
    for stop in stops:
        fields(stop, ("at", "label"))
        at = number(stop["at"])
        require(minimum <= at <= maximum and (last is None or last < at), "key stops must be distinct and sorted")
        text(stop["label"], 50)
        last = at


def validate_compare(scene):
    fields(scene, ("kind", "title", "axes", "bounds", "objects", "cases"))
    limits = bounds(scene["bounds"])
    axis_spans = [hi - lo for lo, hi in limits]
    require(math.isclose(*axis_spans, rel_tol=1e-9), "comparison uses equal axis spans to preserve geometry")

    def object_spec(row):
        fields(row, ("id", "label", "ink"))
        require(isinstance(row["ink"], str) and row["ink"] in INK, "invalid display style")

    objects = ordered_rows(scene["objects"], 1, 8, object_spec)
    cases = scene["cases"]
    require(isinstance(cases, list) and 2 <= len(cases) <= 5, "provide two to five actual condition cases")
    labels = set()
    shapes = []
    for case in cases:
        fields(case, ("label", "items"))
        text(case["label"], 68)
        require(case["label"] not in labels, "condition labels must differ")
        labels.add(case["label"])
        items = case["items"]
        require(isinstance(items, dict) and items.keys() == objects.keys(), "compare the same objects in each condition")
        for item in items.values():
            fields(item, ("kind", "points", "note"))
            require(item["kind"] in ("segment", "vector", "point"), "use a segment, vector or point")
            count = 1 if item["kind"] == "point" else 2
            points(item["points"], limits, count, count)
            require(count == 1 or item["points"][0] != item["points"][1], "zero-length geometry must be a point")
            text(item["note"], 130)
        shapes.append(tuple((item["kind"], tuple(map(tuple, item["points"]))) for item in
                            (items[key] for key in objects)))
    require(len(set(shapes)) == len(cases), "each comparison must show an actual geometric change")


def compile_scene(scene, view, allow_geometry=False):
    """Project no hidden layer. Restricted turns accept only the given-object schema."""
    require(view in VIEWS and type(allow_geometry) is bool, "invalid math disclosure scope")
    require(isinstance(scene, dict) and isinstance(scene.get("kind"), str), "expected a math scene")
    kind = scene["kind"]
    if kind == "text":
        fields(scene, ("kind",))
        return None
    if kind == "given":
        fields(scene, ("kind", "axes"))
        axes(scene["axes"])
        return {"kind": "given", "title": "题设坐标", "axes": scene["axes"]}
    require(view != "protected" and allow_geometry, "derived geometry is not permitted in this turn")
    require(kind in ("region", "compare"), "unsupported math visual kind")
    (validate_region if kind == "region" else validate_compare)(scene)
    text(scene["title"], 100)
    axes(scene["axes"])
    return scene



def render(scene, *, question, focus, view="protected", allow_geometry=False,
           engine="auto", output=None, inline_root=None):
    name(question)
    name(focus)
    approved = compile_scene(scene, view, allow_geometry)
    if approved is None:
        return {"status": "text_only", "learning_write_count": 0}
    require(output is not None and inline_root is not None, "inline location is required for a visual")
    shared = shared_renderer
    local = shared.LocalRenderer(ASSETS / "math.bundle.js", ASSETS / "math.css", "CodexMathVisual",
                                 frozenset({"region", "compare"}))
    scope = "/".join((question, focus, view, "geometry" if allow_geometry else "given"))
    result = shared.write_visual(approved, engine if approved["kind"] != "given" else "native",
                                 output, inline_root, local=local, scope=scope)
    return {**result, "status": "ready", "kind": approved["kind"], "learning_write_count": 0}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", default="-")
    parser.add_argument("--question", required=True, help="Current exact or transient question identity")
    parser.add_argument("--focus", required=True, help="Stable token for the current authorized subquestion/step")
    parser.add_argument("--view", choices=sorted(VIEWS), default="protected")
    parser.add_argument("--allow-geometry", action="store_true",
                        help="Tutor has verified that this turn authorizes all supplied derived geometry")
    parser.add_argument("--engine", choices=("auto", "native", "a2ui"), default="auto")
    parser.add_argument("--inline-root")
    parser.add_argument("--output")
    args = parser.parse_args()
    try:
        if args.input == "-":
            raw = sys.stdin.buffer.read(INPUT_LIMIT + 1)
        else:
            with Path(args.input).open("rb") as handle:
                raw = handle.read(INPUT_LIMIT + 1)
        require(len(raw) <= INPUT_LIMIT, "math visual input too large")
        scene = json.loads(raw)
        result = render(scene, question=args.question, focus=args.focus, view=args.view,
                        allow_geometry=args.allow_geometry, engine=args.engine,
                        output=args.output, inline_root=args.inline_root)
    except (ValueError, OSError, KeyError, TypeError, ImportError) as exc:
        reason = str(exc) if not isinstance(exc, (OSError, KeyError, TypeError)) else "invalid math visual or local resources unavailable"
        print(json.dumps({"status": "unavailable", "reason": reason}, ensure_ascii=False), file=sys.stderr)
        return 2
    print(json.dumps(result, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
