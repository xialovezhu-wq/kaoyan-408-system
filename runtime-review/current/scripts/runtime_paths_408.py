"""Host-local 408 paths; importing this module never creates or moves files.

Repository code follows its own checkout. Sibling dependencies follow that
checkout's parent, and private Codex state follows CODEX_HOME. Explicit absolute
environment overrides support a different layout without editing source.
T9 identity and archive verification remain owned by the existing storage code.
"""
from __future__ import annotations

import os
import json
from pathlib import Path
import shutil
import subprocess
import sys

CORE_MODULES = ("fsrs", "PIL")


def _configured(default: Path, *variables: str) -> Path:
    for variable in variables:
        value = os.environ.get(variable)
        if value:
            path = Path(value).expanduser()
            if not path.is_absolute():
                raise ValueError(variable + "_must_be_absolute")
            return path
    return default


def repo_root() -> Path:
    return _configured(
        Path(__file__).resolve().parents[1],
        "KAOYAN_408_PROJECT_ROOT", "KAOYAN_408_REPO", "CS408_REPO",
    )


def details_root() -> Path:
    return _configured(repo_root().parent / "kaoyan-408-details", "KAOYAN_408_DETAILS_ROOT")


def codex_home() -> Path:
    return _configured(Path.home() / ".codex", "CODEX_HOME")


def package_root() -> Path:
    return _configured(
        codex_home() / "kaoyan-408-conversation-packages",
        "KAOYAN_408_CONVERSATION_PACKAGE_ROOT",
    )


def intake_runtime_root() -> Path:
    return codex_home() / "kaoyan-408-intake"


def decision_root() -> Path:
    return codex_home() / "kaoyan-408-curation-decisions"


def bridge_root() -> Path:
    return _configured(
        repo_root().parent / "Study-Pro-Bridge",
        "STUDY_BRIDGE_ROOT", "STUDY_PRO_BRIDGE_ROOT",
    )


def storage_map_root() -> Path:
    return _configured(repo_root().parent / "Study-Storage-Map", "STUDY_STORAGE_MAP_ROOT")


def source_root() -> Path:
    return _configured(repo_root().parent / "408考研电子书资料", "KAOYAN_408_SOURCE_ROOT")


def legacy_docx() -> Path:
    return _configured(
        Path.home() / "Library/Containers/QReader.MarginStudy.easy/Data/Documents"
        / "408(2026-05-07-19-41-57).docx",
        "KAOYAN_408_LEGACY_DOCX",
    )


def compatible_python(required: tuple[str, ...] = CORE_MODULES) -> str:
    """Select an installed interpreter, never assume Intel/Homebrew locations."""
    explicit = os.environ.get("CS408_PYTHON")
    candidates = [explicit] if explicit else [
        sys.executable,
        *(str(Path(directory) / "python3") for directory in os.get_exec_path()),
        str(bridge_root() / ".venv-study-mcp/bin/python"),
    ]
    probe = (
        "import importlib,json,sys\n"
        "missing=[]\n"
        "for name in sys.argv[1:]:\n"
        " try: importlib.import_module(name)\n"
        " except (ImportError,OSError): missing.append(name)\n"
        "print(json.dumps(missing))\n"
    )
    failures = []
    for candidate in dict.fromkeys(candidates):
        if not candidate:
            continue
        executable = shutil.which(candidate)
        if not executable:
            continue
        try:
            result = subprocess.run(
                [executable, "-c", probe, *required], capture_output=True, text=True,
                timeout=5, check=False,
                env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1"},
            )
            missing = json.loads(result.stdout) if result.returncode == 0 else list(required)
        except (OSError, ValueError, subprocess.TimeoutExpired):
            missing = list(required)
        if missing == []:
            return executable
        failures.append(executable + ":" + ",".join(missing))
    label = "CS408_PYTHON_unavailable_or_missing_dependencies" if explicit else "408_python_dependencies_unavailable"
    raise ValueError(label + ":" + ";".join(failures or required))


def run_script(script_name: str, arguments: list[str], *, repo: Path | None = None) -> None:
    import re
    root = repo or repo_root()
    if not re.fullmatch(r"[a-z][a-z0-9_]*\.py", script_name) or script_name == Path(__file__).name:
        raise ValueError("408_script_basename_required")
    script = root / "scripts" / script_name
    if not script.is_file() or script.is_symlink():
        raise ValueError("408_script_unavailable:" + script_name)
    python = compatible_python()
    environment = {**os.environ, "KAOYAN_408_PROJECT_ROOT": str(root)}
    os.chdir(root)
    os.execve(python, [python, str(script), *arguments], environment)


def describe() -> dict[str, dict[str, object]]:
    paths = {
        "repo": repo_root(), "details": details_root(), "codex_home": codex_home(),
        "packages": package_root(), "intake_runtime": intake_runtime_root(),
        "decisions": decision_root(), "bridge": bridge_root(),
        "storage_map": storage_map_root(), "sources": source_root(),
    }
    result = {key: {"path": str(path), "exists": path.exists()} for key, path in paths.items()}
    try:
        result["python"] = {"path": compatible_python(), "modules": list(CORE_MODULES), "status": "READY"}
    except ValueError as exc:
        result["python"] = {"status": "UNAVAILABLE", "reason": str(exc)}
    return result


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run", nargs=argparse.REMAINDER, help="Existing script basename and its unchanged arguments")
    args = parser.parse_args()
    try:
        if args.run is not None:
            if not args.run:
                raise ValueError("408_script_basename_required")
            run_script(args.run[0], args.run[1:])
        else:
            print(json.dumps(describe(), ensure_ascii=False, indent=2))
    except (OSError, ValueError) as exc:
        print(json.dumps({"status": "PATHS_UNAVAILABLE", "reason": str(exc)}, ensure_ascii=False))
        raise SystemExit(2)
