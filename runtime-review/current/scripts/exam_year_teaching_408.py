"""Read and stage a prebuilt exact lesson in its registered 408 annual chat."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import sys
from functools import lru_cache

import runtime_paths_408 as paths
from capture_context_408 import annual_target
from render_interactive_408 import inline_destination, stage_existing_fragment


SCHEMA = "cs408-private-annual-lessons-v1"
IDENTITY = re.compile(r"EXAM408-(\d{4})-Q(?:0[1-9]|[1-3][0-9]|4[0-7])(?:-P\d{2})?")
SHA256 = re.compile(r"[0-9a-f]{64}")
TEMPLATE_INDEX = ".private/question-visual-408/current.json"
PLAN_INDEX = ".private/source-visual-408/current.json"


def cache_root(year):
    return paths.codex_home() / "kaoyan-408-annual-lessons" / str(year)


def sha(data):
    return hashlib.sha256(data).hexdigest()


def read_json(path):
    return json.loads(path.read_bytes())


@lru_cache(maxsize=9)
def _index_by_stat(path, stamp, size):
    return read_json(path)


def _native_index(path):
    stat = path.stat()
    return _index_by_stat(path, stat.st_mtime_ns, stat.st_size)


def _bound_object(root, digest, extension):
    if not isinstance(digest, str) or not SHA256.fullmatch(digest):
        raise ValueError("invalid_annual_lesson_object")
    file = root / "objects" / (digest + extension)
    if file.is_symlink() or not file.is_file() or sha(file.read_bytes()) != digest:
        raise ValueError("annual_lesson_object_mismatch")
    return file


def lesson_stage(view, level):
    if (view, level) == ("protected", 0):
        return None
    if view == "after_attempt" and 1 <= level <= 4:
        # The independently authored L1 UI is a safe base at higher levels too.
        return "hint-1"
    if (view, level) == ("direct", 5):
        return "solution"
    raise ValueError("annual_lesson_disclosure_mismatch")


def _read_entry(repo, root, year, thread, identity):
    index = _native_index(root / "current.json")
    if (index.get("schema"), index.get("exam_year"), index.get("thread_id")) != (SCHEMA, year, thread):
        raise ValueError("annual_lesson_index_scope_mismatch")
    entry = index["lessons"].get(identity)
    if entry is None:
        raise ValueError("annual_lesson_exact_unit_unavailable")
    template = _native_index(repo / TEMPLATE_INDEX)["templates"].get(identity, {})
    plan = _native_index(repo / PLAN_INDEX)["question_plans"].get(identity, {})
    if (template.get("sha256"), plan.get("sha256")) != (entry["template_sha256"], entry["plan_sha256"]):
        raise ValueError("annual_lesson_source_changed")
    return entry


def use(repo, identity, *, view, level, mechanisms=(), known_version=None, output=None, inline_root=None):
    match = IDENTITY.fullmatch(identity)
    if not match:
        raise ValueError("annual_lesson_exact_exam_id_required")
    year = int(match[1])
    stage = lesson_stage(view, level)
    thread = os.environ.get("CODEX_THREAD_ID")
    if not thread:
        raise ValueError("registered_annual_chat_required")
    annual_target(repo, {"source": {"source_id": identity, "session_id": thread}})
    if bool(output) != bool(inline_root) or (output and not stage):
        raise ValueError("annual_lesson_scoped_display_arguments_required")
    if output and Path(inline_root).name != thread:
        raise ValueError("annual_lesson_requires_current_chat_visualization_root")
    destination = inline_destination(output, inline_root) if output else None
    root = cache_root(year)
    entry = _read_entry(repo, root, year, thread, identity) if stage is not None else {}
    focus = list(mechanisms) or entry.get("personal_focus", [])

    from exam_tutoring_context_408 import QUERY_INPUT_SCHEMA, query_context

    native = query_context(repo, {
        "schema": QUERY_INPUT_SCHEMA,
        "stage": "pre_answer" if view == "protected" else "post_answer",
        "current_question_id": identity,
        "mechanism_terms": focus, "max_profiles": 0,
    }, teaching_view=view, materials="core", personal_scope="teaching", known_version=known_version)
    result = {"identity": identity, "view": view, "hint_level": level, "learning_write_count": 0,
              "current_context": native}
    if stage is None:
        return result | {"status": "protected"}

    live_scope = native.get("question_scope")
    if live_scope and (live_scope["identity"] != identity or
                       set(live_scope["assessed_points"]) != set(entry["assessed_points"]) or
                       set(live_scope["syllabus_ids"]) != set(entry["syllabus_ids"])):
        raise ValueError("annual_lesson_assessment_scope_changed")
    entry_stage = entry["stages"][stage]
    framework = read_json(_bound_object(root, entry_stage["framework_sha256"], ".json"))
    if (framework["identity"], framework["stage"], framework["release_level"]) != (
        identity, stage, 5 if stage == "solution" else 1
    ):
        raise ValueError("annual_lesson_framework_scope_mismatch")
    result |= {"status": "prepared", "prepared_stage": stage, "framework": framework,
               "presentation": entry_stage["presentation"]}
    if destination is not None:
        source = _bound_object(root, entry_stage["fragment_sha256"], ".html")
        if destination.exists() and destination.read_bytes() != source.read_bytes():
            raise ValueError("use_a_new_output_name_for_a_new_display")
        result["visual"] = stage_existing_fragment(source, destination, inline_root=inline_root)
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=["use"])
    parser.add_argument("--id", required=True)
    parser.add_argument("--view", choices=["protected", "after_attempt", "direct"], required=True)
    parser.add_argument("--hint-level", type=int, required=True)
    parser.add_argument("--mechanism", action="append", default=[])
    parser.add_argument("--known-version")
    parser.add_argument("--output", type=Path)
    parser.add_argument("--inline-root", type=Path)
    args = parser.parse_args()
    try:
        result = use(paths.repo_root(), args.id, view=args.view, level=args.hint_level,
                     mechanisms=args.mechanism, known_version=args.known_version,
                     output=args.output, inline_root=args.inline_root)
    except (OSError, ValueError, KeyError) as exc:
        print(json.dumps({"status": "annual_lesson_unavailable", "reason": str(exc)}, ensure_ascii=False))
        return 2
    print(json.dumps(result, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
