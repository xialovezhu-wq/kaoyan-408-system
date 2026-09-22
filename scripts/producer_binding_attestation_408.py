"""Release-neutral Producer/foreground-Skill attestation sidecar."""

from __future__ import annotations

import ast
import datetime as dt
import hashlib
import json
import os
import shlex
import tempfile
from pathlib import Path
from typing import Any, Mapping


FORBIDDEN_KEYS = {"release_id", "activation_id", "dispatcher_authority", "dispatcher_authority_fingerprint", "mcp_authority", "mcp_authority_fingerprint", "mcp_release_id", "producer_authority_fingerprint"}
EXPECTED_ENTRYPOINT_REL = Path("scripts/managed_408_current_turn.py")
EXPECTED_SOURCE_CLOSURE = (
    "bounded_jsonl_index_408.py",
    "capture_commit_index_408.py",
    "capture_hot_writer_408.py",
    "cross_subject_candidates_408.py",
    "cross_variant_pipeline_408.py",
    "current_question_context_408.py",
    "current_question_evidence_408.py",
    "formal_intake_formal_proof_408.py",
    "formal_intake_review_backflow_408.py",
    "formal_problem_document_408.py",
    "generate_morning_action_queue.py",
    "generate_morning_review_queue.py",
    "image_integrity_408.py",
    "intake_fact_capture_408.py",
    "intake_lib_408.py",
    "linked_practice_source_408.py",
    "managed_408_current_turn.py",
    "morning_capture_hot_adapter_408.py",
    "morning_review_prepared_pack_408.py",
    "morning_review_session.py",
    "morning_session_hot_state_408.py",
    "postanswer_feedback_gate_408.py",
    "producer_binding_attestation_408.py",
    "question_source_attestation_408.py",
    "review_feedback_loop.py",
    "review_hot_projection_408.py",
    "review_hot_state_408.py",
    "review_outcome_hot_408.py",
)
EXPECTED_CAPTURE_CONTRACT_RELS = (
    Path("codex-skill-sources/kaoyan-408-wrong-intake/references/fact-capture-schema.md"),
    Path("schema/managed-408-answer-current-and-next-v1.md"),
    Path("schema/managed-408-formal-problem-document-capture-v1.md"),
    Path("schema/current-question-context-v1.md"),
    Path("schema/current-question-evidence-bundle-v3.md"),
    Path("schema/current-question-evidence-bundle-v4.md"),
    Path("schema/current-question-background-handoff-v1.md"),
    Path("schema/current-question-trace-supplement-v1.md"),
    Path("schema/capture-hot-writer-v1.md"),
    Path("schema/capture-commit-index-v1.md"),
)


class ProducerBindingError(RuntimeError):
    pass


def canonical_bytes(value: Any) -> bytes:
    return (json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n").encode("utf-8")


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def sha256_value(value: Any) -> str:
    return hashlib.sha256(canonical_bytes(value)).hexdigest()


def _assert_release_neutral(value: Any) -> None:
    if isinstance(value, Mapping):
        for key, nested in value.items():
            if str(key) in FORBIDDEN_KEYS:
                raise ProducerBindingError("producer attestation contains deployment identity")
            _assert_release_neutral(nested)
    elif isinstance(value, list):
        for nested in value:
            _assert_release_neutral(nested)


def _timestamp(value: str) -> dt.datetime:
    parsed = dt.datetime.fromisoformat(value[:-1] + "+00:00" if value.endswith("Z") else value)
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ProducerBindingError("producer binding timestamp must include timezone")
    return parsed.astimezone(dt.timezone.utc)


def _local_import_names(path: Path) -> set[str]:
    try:
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    except (OSError, UnicodeDecodeError, SyntaxError) as exc:
        raise ProducerBindingError("producer local import closure is unreadable") from exc
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names.update(alias.name.split(".", 1)[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            if node.level:
                raise ProducerBindingError("producer relative import is unsupported")
            if node.module:
                names.add(node.module.split(".", 1)[0])
    return names


def recursive_local_import_closure(entrypoint: Path, *, repo_root: Path) -> list[Path]:
    scripts_root = (repo_root / "scripts").resolve()
    expected_entrypoint = (repo_root / EXPECTED_ENTRYPOINT_REL).resolve()
    if entrypoint.resolve() != expected_entrypoint:
        raise ProducerBindingError("producer entrypoint is not the current managed route")
    pending = [expected_entrypoint]
    visited: set[Path] = set()
    while pending:
        current = pending.pop()
        if current in visited:
            continue
        if (
            current.parent != scripts_root
            or current.is_symlink()
            or not current.is_file()
        ):
            raise ProducerBindingError("producer local import escaped the scripts root")
        visited.add(current)
        for name in _local_import_names(current):
            candidate = (scripts_root / f"{name}.py").resolve()
            if candidate.is_file() and candidate not in visited:
                pending.append(candidate)
    return sorted(visited, key=lambda path: str(path))


def _assert_current_skill_command(skill_path: Path) -> None:
    commands: list[list[str]] = []
    for raw_line in skill_path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line.startswith("python3 scripts/"):
            continue
        try:
            commands.append(shlex.split(line))
        except ValueError as exc:
            raise ProducerBindingError("foreground Skill command is malformed") from exc
    answer_matches = [
        command
        for command in commands
        if len(command) >= 3
        and command[:2] == ["python3", EXPECTED_ENTRYPOINT_REL.as_posix()]
        and command.count("answer-current-and-next") == 1
        and "capture-formal-problem-document" not in command
    ]
    document_matches = [
        command
        for command in commands
        if len(command) >= 3
        and command[:2] == ["python3", EXPECTED_ENTRYPOINT_REL.as_posix()]
        and command.count("capture-formal-problem-document") == 1
        and "answer-current-and-next" not in command
    ]
    if (
        len(answer_matches) != 1
        or len(document_matches) != 1
        or len(commands) != 2
    ):
        raise ProducerBindingError("foreground Skill current command binding mismatch")


def _exact_file_rows(paths: list[Path] | tuple[Path, ...]) -> list[dict[str, str]]:
    return [
        {"path": str(path.resolve()), "sha256": sha256_file(path.resolve())}
        for path in paths
    ]


def load_descriptor(path: Path, *, subject: str) -> dict[str, Any]:
    descriptor = json.loads(path.read_text(encoding="utf-8"))
    expected = {"schema_version", "subject", "attestation_required_after", "foreground_skill", "producer", "capture_contract", "attestation_relative_root", "formal_write_count", "descriptor_content_sha256"}
    if not isinstance(descriptor, dict) or set(descriptor) != expected or descriptor.get("schema_version") != "producer_binding_descriptor_v1" or descriptor.get("subject") != subject or descriptor.get("formal_write_count") != 0:
        raise ProducerBindingError("producer binding descriptor invalid")
    core = {key: value for key, value in descriptor.items() if key != "descriptor_content_sha256"}
    if descriptor.get("descriptor_content_sha256") != sha256_value(core):
        raise ProducerBindingError("producer binding descriptor hash invalid")
    _timestamp(str(descriptor["attestation_required_after"]))
    relative_root = Path(str(descriptor["attestation_relative_root"]))
    if relative_root.is_absolute() or ".." in relative_root.parts:
        raise ProducerBindingError("producer binding sidecar root invalid")
    skill = descriptor.get("foreground_skill")
    if not isinstance(skill, dict):
        raise ProducerBindingError("foreground Skill binding missing")
    for prefix in ("authoritative", "installed"):
        candidate = Path(str(skill.get(f"{prefix}_path") or ""))
        if not candidate.is_absolute() or candidate.is_symlink() or not candidate.is_file() or sha256_file(candidate) != skill.get(f"{prefix}_sha256"):
            raise ProducerBindingError(f"foreground Skill {prefix} binding mismatch")
    authoritative_path = Path(str(skill.get("authoritative_path") or ""))
    installed_path = Path(str(skill.get("installed_path") or ""))
    if (
        skill.get("authoritative_sha256") != skill.get("installed_sha256")
        or authoritative_path.read_bytes() != installed_path.read_bytes()
    ):
        raise ProducerBindingError("foreground Skill authoritative/installed parity mismatch")
    _assert_current_skill_command(installed_path)
    descriptor_path = path.resolve()
    if descriptor_path.parent.name != "schema":
        raise ProducerBindingError("producer binding descriptor location invalid")
    repo_root = descriptor_path.parent.parent.resolve()
    producer = descriptor.get("producer")
    files = producer.get("source_files") if isinstance(producer, dict) else None
    if (
        not isinstance(producer, dict)
        or set(producer)
        != {
            "contract_id",
            "contract_version",
            "entrypoint_path",
            "source_closure_sha256",
            "source_files",
        }
        or not isinstance(files, list)
        or not files
    ):
        raise ProducerBindingError("producer source closure missing")
    entrypoint = Path(str(producer.get("entrypoint_path") or ""))
    if not entrypoint.is_absolute():
        raise ProducerBindingError("producer entrypoint binding mismatch")
    discovered = recursive_local_import_closure(entrypoint, repo_root=repo_root)
    expected_closure = sorted(
        ((repo_root / "scripts" / name).resolve() for name in EXPECTED_SOURCE_CLOSURE),
        key=lambda candidate: str(candidate),
    )
    if discovered != expected_closure:
        raise ProducerBindingError("producer recursive local import closure drifted")
    normalized: list[dict[str, str]] = []
    for row in files:
        if not isinstance(row, dict) or set(row) != {"path", "sha256"}:
            raise ProducerBindingError("producer source closure invalid")
        candidate = Path(str(row["path"]))
        if not candidate.is_absolute() or candidate.is_symlink() or not candidate.is_file() or sha256_file(candidate) != row["sha256"]:
            raise ProducerBindingError("producer source closure mismatch")
        normalized.append({"path": str(candidate.resolve()), "sha256": str(row["sha256"])})
    if normalized != _exact_file_rows(discovered):
        raise ProducerBindingError("producer source closure is incomplete or reordered")
    if producer.get("source_closure_sha256") != sha256_value(normalized):
        raise ProducerBindingError("producer source closure hash invalid")
    contract = descriptor.get("capture_contract")
    files = contract.get("files") if isinstance(contract, dict) else None
    if (
        not isinstance(contract, dict)
        or set(contract)
        != {"contract_id", "contract_version", "files", "files_sha256"}
        or not isinstance(files, list)
        or not files
    ):
        raise ProducerBindingError("Capture contract closure missing")
    contract_rows: list[dict[str, str]] = []
    for row in files:
        if not isinstance(row, dict) or set(row) != {"path", "sha256"}:
            raise ProducerBindingError("Capture contract closure invalid")
        candidate = Path(str((row or {}).get("path") or "")) if isinstance(row, dict) else Path("")
        if not candidate.is_absolute() or candidate.is_symlink() or not candidate.is_file() or sha256_file(candidate) != row.get("sha256"):
            raise ProducerBindingError("Capture contract closure mismatch")
        contract_rows.append({"path": str(candidate.resolve()), "sha256": str(row["sha256"])})
    expected_contract_paths = tuple((repo_root / relative).resolve() for relative in EXPECTED_CAPTURE_CONTRACT_RELS)
    if contract_rows != _exact_file_rows(expected_contract_paths):
        raise ProducerBindingError("Capture contract closure is incomplete or reordered")
    if contract.get("files_sha256") != sha256_value(contract_rows):
        raise ProducerBindingError("Capture contract closure hash invalid")
    _assert_release_neutral(descriptor)
    return descriptor


def _atomic_no_clobber(path: Path, value: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = canonical_bytes(value)
    descriptor, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        os.fchmod(descriptor, 0o600)
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(payload); handle.flush(); os.fsync(handle.fileno())
        try:
            os.link(temporary, path)
        except FileExistsError:
            if path.is_symlink() or not path.is_file() or path.read_bytes() != payload:
                raise ProducerBindingError("producer attestation no-clobber conflict")
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def publish_attestation(*, descriptor_path: Path, repo_root: Path, subject: str, capture_id: str, capture_content_sha256: str, recorded_at: str) -> dict[str, Any]:
    descriptor = load_descriptor(descriptor_path, subject=subject)
    if _timestamp(recorded_at) < _timestamp(descriptor["attestation_required_after"]):
        return {"status": "historical_pre_attestation", "attestation_path": None, "attestation_sha256": None, "formal_write_count": 0}
    core = {"schema_version": "producer_binding_attestation_v1", "subject": subject, "capture_id": capture_id, "capture_content_sha256": capture_content_sha256, "foreground_skill": descriptor["foreground_skill"], "producer": descriptor["producer"], "capture_contract": descriptor["capture_contract"], "binding_descriptor_sha256": sha256_file(descriptor_path), "attestation_required_after": descriptor["attestation_required_after"], "formal_write_count": 0}
    _assert_release_neutral(core)
    attestation = {**core, "attestation_sha256": sha256_value(core)}
    path = repo_root.resolve() / descriptor["attestation_relative_root"] / f"{capture_id}.json"
    _atomic_no_clobber(path, attestation)
    return {"status": "attested", "attestation_path": str(path), "attestation_sha256": sha256_file(path), "formal_write_count": 0}
