#!/usr/bin/env python3
"""Locate the existing 408 checkout for installed skill helpers, without writes."""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import sys


def _is_repo(path: Path) -> bool:
    return all((path / name).is_file() for name in (
        "scripts/runtime_paths_408.py",
        "scripts/conversation_package_408.py",
        "schema/cs408-conversation-package-v1.md",
    ))


def find_repo(supplied: str | Path | None = None) -> Path:
    explicit = supplied or next((os.environ[name] for name in (
        "KAOYAN_408_PROJECT_ROOT", "KAOYAN_408_REPO", "CS408_REPO",
    ) if os.environ.get(name)), None)
    if explicit:
        path = Path(explicit).expanduser()
        if not path.is_absolute() or not _is_repo(path):
            raise ValueError("408_repo_override_invalid:" + str(path))
        return path.resolve()
    for start in (Path.cwd().resolve(), Path(__file__).resolve().parent):
        for candidate in (start, *start.parents):
            if _is_repo(candidate):
                return candidate
    conventional = Path.home() / "Documents/kaoyan-408"
    if _is_repo(conventional):
        return conventional.resolve()
    raise ValueError("408_repo_not_found_set_KAOYAN_408_PROJECT_ROOT")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo", help="Explicit absolute checkout; otherwise use environment or current checkout")
    parser.add_argument("--run", nargs=argparse.REMAINDER,
                        help="Script basename followed by its unchanged arguments")
    args = parser.parse_args(argv)
    try:
        repo = find_repo(args.repo)
        sys.path.insert(0, str(repo / "scripts"))
        import runtime_paths_408
        if args.run is not None:
            if not args.run:
                raise ValueError("408_script_basename_required")
            runtime_paths_408.run_script(args.run[0], args.run[1:], repo=repo)
        os.environ["KAOYAN_408_PROJECT_ROOT"] = str(repo)
        print(json.dumps(runtime_paths_408.describe(), ensure_ascii=False, indent=2))
        return 0
    except (OSError, ValueError) as exc:
        print(json.dumps({"status": "PATHS_UNAVAILABLE", "reason": str(exc)}, ensure_ascii=False))
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
