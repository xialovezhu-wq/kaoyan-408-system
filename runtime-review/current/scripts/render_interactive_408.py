#!/usr/bin/env python3
"""Render an explicitly disclosed 408 scene as a Visualize HTML fragment.

This is not a solver. It never reads a template, answer, profile or study queue.
Every text has a release level; only its filtered projection reaches the HTML.
"""
from __future__ import annotations

import argparse
import base64
import hashlib
import json
from pathlib import Path
import re
import sys
import tempfile

import runtime_paths_408 as _paths
from public_scene_408 import MAX_IMAGE_BYTES, SCHEMA, _image_geometry, public_scene, require
from public_scene_408 import MAX_IMAGE_DIMENSION as MAX_IMAGE_DIMENSION

ASSET = Path(__file__).resolve().parent.parent / "support/interactive-408/scene.html"
INLINE_NAME = re.compile(r"[a-z0-9]+(?:-[a-z0-9]+)*\.html")


def _embed_diagrams(data, raw):
    """Read only images already admitted by the pure scene projection."""
    sources = {source["id"]: source for source in raw.get("source_diagrams", [])}
    for diagram in data["source_diagrams"]:
        source = sources[diagram["id"]]
        path = Path(source["image_path"])
        try:
            require(path.is_file(), "diagram_image_unavailable")
            require(path.stat().st_size <= MAX_IMAGE_BYTES, "diagram_image_too_large")
            with path.open("rb") as stream:
                content = stream.read(MAX_IMAGE_BYTES + 1)
        except OSError as exc:
            raise ValueError("interactive_scene_diagram_image_unavailable") from None
        require(len(content) <= MAX_IMAGE_BYTES, "diagram_image_too_large")
        require(hashlib.sha256(content).hexdigest() == source["sha256"].lower(), "diagram_hash_mismatch")
        mime, width, height = _image_geometry(content)
        require((path.suffix.lower() == ".png") == (mime == "image/png"), "diagram_image_type_mismatch")
        diagram.update(src="data:" + mime + ";base64," + base64.b64encode(content).decode("ascii"),
                       width=width, height=height)


def inline_destination(output, inline_root):
    """Preflight the task-owned display copy, not arbitrary filesystem access.

    The desktop visualization reader has its own allowlist even under full
    shell access. The caller must pass THIS task's supplied visualization root;
    a T9 archive or a symlink back to it cannot serve as the inline display.
    """
    destination = Path(output).expanduser().absolute()
    root = Path(inline_root).expanduser().absolute()
    require(".." not in destination.parts and ".." not in root.parts, "inline_path_not_normalized")
    require(bool(re.search(r"/(?:\.codex|\.codex-workspaces(?:/instances/[A-Za-z0-9_-]+)?)/visualizations/\d{4}/\d{2}/\d{2}/[A-Za-z0-9_-]+$",
                           str(root))), "task_visualization_root_required")
    require(destination.parent == root and INLINE_NAME.fullmatch(destination.name), "inline_path_outside_task_root")
    require(not destination.is_symlink() and not any(p.is_symlink() for p in root.parents) and not root.is_symlink(),
            "inline_symlink_not_readable")
    return destination


def _write_fragment(fragment, output, *, inline_root=None):
    destination = inline_destination(output, inline_root) if inline_root else Path(output).expanduser().resolve()
    require(len(fragment.encode()) < 1024 * 1024, "fragment_too_large")
    require(not re.search(r"<!doctype\s|<\s*(?:html|head|body)(?:\s|>)", fragment, re.I), "fragment_required")
    if str(destination).startswith("/Volumes/"):
        sys.path.insert(0, str(_paths.storage_map_root()))
        from cold_storage import ensure_output_path
        ensure_output_path(str(destination), "408")
    destination.parent.mkdir(parents=True, exist_ok=True)
    raw = fragment.encode("utf-8")
    with tempfile.NamedTemporaryFile(dir=destination.parent, prefix=".visual-", delete=False) as handle:
        staged = Path(handle.name)
        try:
            handle.write(raw)
            handle.close()
            staged.replace(destination)
        finally:
            staged.unlink(missing_ok=True)
    require(destination.read_bytes() == raw, "write_readback_failed")
    result = {"status": "rendered", "path": str(destination), "inline_path_preflight_passed": inline_root is not None,
              "client_display_verified": False, "learning_write_count": 0}
    if inline_root is not None:
        result["visualize_reference"] = 'visualize' + json.dumps({"path": str(destination)}, ensure_ascii=False) + ''
    else:
        result["next_action"] = "stage_in_current_task_visualization_root_before_inline_delivery"
    return result


def stage_existing_fragment(source, output, *, inline_root, expected_sha256=None):
    source = Path(source).expanduser()
    require(source.is_file() and not source.is_symlink() and source.stat().st_size < 1024 * 1024, "source_fragment_invalid")
    raw = source.read_bytes()
    sha = hashlib.sha256(raw).hexdigest()
    if expected_sha256 is not None:
        require(sha == expected_sha256, "source_fragment_hash_drift")
    fragment = raw.decode("utf-8")
    destination = inline_destination(output, inline_root)
    require(not destination.exists() or destination.read_bytes() == raw, "use_a_new_output_name_for_a_new_display")
    result = _write_fragment(fragment, destination, inline_root=inline_root)
    result["source_sha256"] = sha
    result["byte_identical_display_copy"] = True
    return result


def compose_fragment(raw):
    data = public_scene(raw)
    _embed_diagrams(data, raw)
    # JSON remains data even if a label contains an HTML closing tag.
    encoded = json.dumps(data, ensure_ascii=False, separators=(",", ":")).replace("<", "\\u003c").replace("\u2028", "\\u2028").replace("\u2029", "\\u2029")
    uid = "cs408-" + hashlib.sha256(encoded.encode()).hexdigest()[:16]
    fragment = ASSET.read_text(encoding="utf-8").replace("__ROOT_ID__", uid).replace("__PUBLIC_SCENE__", encoded)
    require(len(fragment.encode()) < 1024 * 1024, "fragment_too_large")
    return fragment, data


def render(raw, output, *, inline_root=None):
    fragment, data = compose_fragment(raw)
    return {**_write_fragment(fragment, output, inline_root=inline_root),
            "public_nodes": len(data["nodes"]), "public_states": len(data["states"]),
            "public_source_diagrams": len(data["source_diagrams"]),
            "public_hotspots": sum(len(d["hotspots"]) for d in data["source_diagrams"]),
            "linear_text_visible_without_interaction": data["linear_text_complete"],
            "chat_linear_text": chat_linear_text(data),
            "chat_text_must_be_delivered_separately": True}


def chat_linear_text(data):
    return "\n\n".join(
        "\n".join(part for part in (step["title"], step["text"], step.get("return_to_reasoning")) if part)
        for step in data["linear_steps"]
    )


def render_with_layout(raw, layout, engine, output, *, inline_root, fallback_raw=None):
    import visual_layout_408

    data = public_scene(raw)
    destination = inline_destination(output, inline_root)

    def fallback():
        return {**render(fallback_raw if fallback_raw is not None else raw, destination, inline_root=inline_root),
                "layout_rendered": False,
                "layout_fallback_reason": "shared_visual_renderer_unavailable"}
    result = visual_layout_408.render(data, layout, engine, destination, inline_root, fallback=fallback)
    if result.get("layout_rendered") is False:
        return result
    return {**result, "status": "rendered", "inline_path_preflight_passed": True, "learning_write_count": 0,
            "layout_rendered": True,
            "public_nodes": len(data["nodes"]), "public_states": len(data["states"]),
            "linear_text_visible_without_interaction": False,
            "chat_text_must_be_delivered_separately": True,
            "chat_linear_text": chat_linear_text(data)}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    inputs = parser.add_mutually_exclusive_group(required=True)
    inputs.add_argument("--input")
    inputs.add_argument("--existing-fragment", type=Path)
    inputs.add_argument("--preset", choices=("gbn-sr", "paging"), help="Independent public concept example")
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--inline-root", type=Path, help="This task's explicitly supplied visualization directory")
    parser.add_argument("--expected-sha256", help="Verify an existing prepared fragment before making a display copy")
    parser.add_argument("--presentation", choices=("auto", "existing", "native", "a2ui"), default="auto",
                        help="Auto uses the shared renderer for layout requests and preserves classic scenes")
    args = parser.parse_args()
    if args.existing_fragment:
        require(args.inline_root is not None, "existing_fragment_requires_inline_root")
        require(args.presentation in {"auto", "existing"}, "existing_fragment_cannot_change_presentation")
        result = stage_existing_fragment(args.existing_fragment, args.output, inline_root=args.inline_root,
                                         expected_sha256=args.expected_sha256)
    else:
        if args.preset:
            path = ASSET.parent / "examples" / (args.preset + ".json")
            request = json.loads(path.read_text(encoding="utf-8"))
        else:
            request = json.load(sys.stdin) if args.input == "-" else json.loads(Path(args.input).read_text(encoding="utf-8"))
        has_layout = isinstance(request, dict) and set(request) == {"scene", "layout"}
        if args.preset or has_layout or args.presentation in {"native", "a2ui"}:
            require(args.inline_root is not None, "layout_requires_inline_root")
            require(has_layout, "layout_request_required")
            if args.presentation == "existing":
                result = render(request["scene"], args.output, inline_root=args.inline_root)
            else:
                result = render_with_layout(request["scene"], request["layout"], args.presentation, args.output,
                                            inline_root=args.inline_root)
        else:
            result = render(request, args.output, inline_root=args.inline_root)
    print(json.dumps(result, ensure_ascii=False))


if __name__ == "__main__":
    main()
