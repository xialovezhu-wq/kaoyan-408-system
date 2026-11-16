#!/usr/bin/env python3
"""Build and read a frozen, answer-isolated 408 morning-review delivery pack.

The pack is a delivery sidecar.  It is not a queue, session ledger, learner
outcome, capture, mastery update, or formal repository write.  Public question
surfaces and private grading capsules are stored in separate content-addressed
objects.  The session ledger may bind only their hashes and the pack manifest.
"""

from __future__ import annotations

import argparse
import fcntl
import hashlib
import json
import os
import re
import stat
import sys
import tempfile
import time
from contextlib import contextmanager
from datetime import date, datetime
from pathlib import Path
from typing import Any, Iterator
from zoneinfo import ZoneInfo

import current_question_context_408 as current_context
import current_question_evidence_408 as current_evidence
import question_exposure_408 as question_exposure
from intake_lib_408 import scan_leak

import runtime_paths_408 as _paths


PACK_ROOT_REL = Path(
    "wiki/study_vaults/408-full/state/morning-review-prepared-packs"
)
SESSION_ROOT_REL = Path("wiki/study_vaults/408-full/state/morning-review")
DETAILS_ROOT = _paths.details_root()
DRAFT_SCHEMA = "morning_review_prepared_pack_draft_v1"
VERIFICATION_SCHEMA = "morning_review_prepared_pack_verification_v1"
MANIFEST_SCHEMA = "morning_review_prepared_pack_manifest_v1"
SURFACE_SCHEMA = "morning_review_question_surface_v1"
EVALUATOR_SCHEMA = "morning_review_grading_capsule_v1"
POINTER_SCHEMA = "morning_review_prepared_pack_pointer_v1"
INTERACTION_MODE = "frozen_prepared_pack"
CURRENT_TURN_PREPARATION_SCHEMA = "morning-review-current-turn-preparation-v1"
SCHEDULING_BINDING_SCHEMA = "morning-schedule-binding-v1"
MAX_INPUT_BYTES = 4 * 1024 * 1024
MAX_OBJECT_BYTES = 512 * 1024
MAX_ITEMS = 32
OPTIONS = ("A", "B", "C", "D")
MANAGED_RESPONSES = frozenset({*OPTIONS, "BLANK", "UNCERTAIN"})
ITEM_KINDS = {
    "formal_due",
    "baseline_calibration",
    "derived_d0",
    "original_question",
}
RESULTS = {
    "independent_correct",
    "fragile_correct",
    "partial",
    "wrong",
    "blank",
}
CHECKS = (
    "standalone_surface",
    "exactly_four_options",
    "mutually_exclusive_options",
    "unique_best_answer",
    "source_grounded",
    "same_target",
    "answer_isolated",
    "rubric_sufficient",
    "feedback_safe",
)
SHA_RE = re.compile(r"^[0-9a-f]{64}$")
SAFE_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:@/-]{1,180}$")
SESSION_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:-]{1,120}$")
ITEM_ID_RE = re.compile(r"^(?:MQ|DQ|OQ|AOQ)-[A-Za-z0-9_.:-]{1,80}$")
POINT_RE = re.compile(r"^(?:DS|CO|OS|CN)\d{2}-\d{2}$")
MODULE_RE = re.compile(r"^(?:DS|CO|OS|CN)\d{2}$")
FORMAL_RE = re.compile(r"^(?:DS|CO|OS|CN)_(?:\d{4}|UNK)_\d{3}$")
SOURCE_ID_PROVENANCE = {
    "action_source_id",
    "formal_node_fallback",
    "legacy_queue_source_id",
}


def _normalize_question_asset(value: object, item_id: str) -> dict[str, Any] | None:
    if value is None:
        return None
    if not isinstance(value, dict) or set(value) != {
        "vault", "relative_path", "sha256", "size_bytes"
    }:
        raise PreparedPackError(f"{item_id}.surface.question_asset is invalid")
    if value.get("vault") != "kaoyan-408-details":
        raise PreparedPackError(f"{item_id}.surface.question_asset vault is invalid")
    relative = Path(str(value.get("relative_path") or ""))
    if relative.is_absolute() or not relative.parts or ".." in relative.parts:
        raise PreparedPackError(f"{item_id}.surface.question_asset path is invalid")
    lexical = DETAILS_ROOT / relative
    if lexical.is_symlink():
        raise PreparedPackError(f"{item_id}.surface.question_asset is a symlink")
    try:
        path = lexical.resolve(strict=True)
        path.relative_to(DETAILS_ROOT.resolve(strict=True))
    except (OSError, ValueError) as exc:
        raise PreparedPackError(f"{item_id}.surface.question_asset is missing") from exc
    expected = str(value.get("sha256") or "")
    size = value.get("size_bytes")
    if (
        not path.is_file()
        or path.is_symlink()
        or not SHA_RE.fullmatch(expected)
        or hashlib.sha256(path.read_bytes()).hexdigest() != expected
        or not isinstance(size, int)
        or size <= 0
        or path.stat().st_size != size
    ):
        raise PreparedPackError(f"{item_id}.surface.question_asset binding drifted")
    return {
        "vault": "kaoyan-408-details",
        "relative_path": relative.as_posix(),
        "sha256": expected,
        "size_bytes": size,
    }


def resolve_question_asset(surface: dict[str, Any]) -> dict[str, Any] | None:
    asset = _normalize_question_asset(surface.get("question_asset"), str(surface.get("item_id") or "surface"))
    if asset is None:
        return None
    path = (DETAILS_ROOT / asset["relative_path"]).resolve(strict=True)
    return {**asset, "absolute_path": str(path)}
LEAK_RE = re.compile(
    r"(?:正确答案|标准答案|(?:正确)?答案\s*(?:是|为|选|[:：=])?\s*[（(\[]?\s*[ABCD]"
    r"|正确选项\s*(?:是|为|[:：=])?\s*[（(\[]?\s*[ABCD]"
    r"|answer\s*(?:is|[:：=])?\s*[（(\[]?\s*[ABCD]"
    r"|[ABCD]\s*(?:项|选项)?\s*(?:是|为)?\s*(?:正确|对)"
    r"|\[\s*[✓✔]\s*\])",
    re.IGNORECASE,
)
QUEUE_HEADING_RE = re.compile(
    r"(?m)^#{3,6}\s+(?P<item>(?:MQ|DQ|OQ|AOQ)-[A-Za-z0-9_.:-]+)(?:\s+[^\n]*)?$"
)


class PreparedPackError(RuntimeError):
    """A stable fail-closed prepared-pack error."""


def _json_bytes(value: Any, *, pretty: bool = False) -> bytes:
    kwargs: dict[str, Any] = {"ensure_ascii": False, "sort_keys": True}
    if pretty:
        kwargs["indent"] = 2
    else:
        kwargs["separators"] = (",", ":")
    return (json.dumps(value, **kwargs) + "\n").encode("utf-8")


def _sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _digest(value: Any) -> str:
    return _sha256_bytes(_json_bytes(value))


def _inside(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
        return True
    except ValueError:
        return False


def _reject_symlink_components(path: Path, root: Path, label: str) -> None:
    try:
        relative = path.relative_to(root)
    except ValueError as exc:
        raise PreparedPackError(f"{label} escapes its managed root") from exc
    current = root
    for component in relative.parts:
        current = current / component
        try:
            mode = current.lstat().st_mode
        except FileNotFoundError:
            continue
        if stat.S_ISLNK(mode):
            raise PreparedPackError(f"{label} contains a symbolic-link component")


def _regular_file(path: Path, label: str, *, max_bytes: int) -> None:
    try:
        info = path.lstat()
    except OSError as exc:
        raise PreparedPackError(f"{label} is unavailable") from exc
    if stat.S_ISLNK(info.st_mode) or not stat.S_ISREG(info.st_mode):
        raise PreparedPackError(f"{label} must be a regular non-symlink file")
    if info.st_size > max_bytes:
        raise PreparedPackError(f"{label} exceeds its bounded size")


def _read_json(path: Path, label: str, *, max_bytes: int = MAX_INPUT_BYTES) -> dict[str, Any]:
    _regular_file(path, label, max_bytes=max_bytes)
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise PreparedPackError(f"{label} is invalid JSON") from exc
    if not isinstance(value, dict):
        raise PreparedPackError(f"{label} must be a JSON object")
    return value


def _resolve_input(path: str | Path, label: str) -> Path:
    lexical = Path(path).expanduser()
    try:
        if stat.S_ISLNK(lexical.lstat().st_mode):
            raise PreparedPackError(f"{label} must not be a symbolic link")
    except FileNotFoundError as exc:
        raise PreparedPackError(f"{label} is unavailable") from exc
    value = lexical.resolve()
    _regular_file(value, label, max_bytes=MAX_INPUT_BYTES)
    return value


def _repo_file(repo: Path, ref: object, label: str) -> Path:
    if not isinstance(ref, str) or not ref or Path(ref).is_absolute():
        raise PreparedPackError(f"{label} must be one repository-relative path")
    raw = Path(ref)
    if ".." in raw.parts:
        raise PreparedPackError(f"{label} must not escape the repository")
    lexical = repo / raw
    _reject_symlink_components(lexical, repo, label)
    path = lexical.resolve()
    if not _inside(path, repo):
        raise PreparedPackError(f"{label} must not escape the repository")
    _regular_file(path, label, max_bytes=MAX_INPUT_BYTES)
    return path


def _atomic_write(path: Path, value: Any, *, mode: int = 0o600) -> str:
    payload = _json_bytes(value, pretty=True)
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    os.chmod(path.parent, 0o700)
    descriptor, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        os.fchmod(descriptor, mode)
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)
    return _sha256_bytes(payload)


def _content_write(directory: Path, value: dict[str, Any]) -> tuple[Path, str]:
    payload = _json_bytes(value, pretty=True)
    digest = _sha256_bytes(payload)
    path = directory / f"{digest}.json"
    if path.exists():
        _regular_file(path, "content-addressed object", max_bytes=MAX_OBJECT_BYTES)
        info = path.stat()
        parent_info = path.parent.stat()
        if (
            info.st_uid != os.geteuid()
            or parent_info.st_uid != os.geteuid()
            or info.st_mode & 0o077
            or parent_info.st_mode & 0o077
        ):
            raise PreparedPackError(
                "content-addressed object permissions or ownership are unsafe"
            )
        if path.read_bytes() != payload:
            raise PreparedPackError("content-addressed object collision")
        return path, digest
    _atomic_write(path, value)
    return path, digest


@contextmanager
def _lock(path: Path) -> Iterator[None]:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    os.chmod(path.parent, 0o700)
    with path.open("a+", encoding="utf-8") as handle:
        os.chmod(path, 0o600)
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def _bounded_text(value: object, label: str, *, maximum: int, minimum: int = 1) -> str:
    if not isinstance(value, str):
        raise PreparedPackError(f"{label} must be text")
    normalized = value.strip()
    if len(normalized) < minimum or len(normalized.encode("utf-8")) > maximum:
        raise PreparedPackError(f"{label} is outside its bounded size")
    return normalized


def _assert_answer_safe_text(value: str, label: str) -> None:
    fails, warns = scan_leak(value)
    if LEAK_RE.search(value) or fails or warns:
        raise PreparedPackError(f"{label} contains answer-bearing material")


def _safe_id(value: object, label: str) -> str:
    normalized = _bounded_text(value, label, maximum=180)
    if not SAFE_ID_RE.fullmatch(normalized):
        raise PreparedPackError(f"{label} is unsafe")
    return normalized


def _string_list(
    value: object,
    label: str,
    *,
    maximum: int,
    item_bytes: int,
    pattern: re.Pattern[str] | None = None,
) -> list[str]:
    if value is None:
        return []
    if not isinstance(value, list) or len(value) > maximum:
        raise PreparedPackError(f"{label} must be a bounded list")
    result: list[str] = []
    for index, item in enumerate(value):
        text = _bounded_text(item, f"{label}[{index}]", maximum=item_bytes)
        if pattern is not None and not pattern.fullmatch(text):
            raise PreparedPackError(f"{label}[{index}] is invalid")
        if text not in result:
            result.append(text)
    return result


def _field(section: str, name: str) -> str | None:
    match = re.search(rf"(?mi)^-\s*{re.escape(name)}\s*[:：]\s*(.+?)\s*$", section)
    if not match:
        return None
    value = match.group(1).strip()
    if value.startswith("`") and value.endswith("`"):
        value = value[1:-1].strip()
    return value


def _consumer_source_id(value: object, label: str) -> str:
    """Validate against the exact CurrentQuestionContext machine-ID grammar."""

    normalized = str(value or "").strip()
    if not current_context.SAFE_ID_RE.fullmatch(normalized):
        raise PreparedPackError(
            f"{label} is incompatible with CurrentQuestionContext"
        )
    return normalized


def _queue_source_label(section: str, source_id: str, item_id: str) -> str:
    raw = _field(section, "source_label")
    if raw is None:
        return source_id
    try:
        decoded = json.loads(raw)
    except json.JSONDecodeError:
        decoded = raw
    label = _bounded_text(
        decoded,
        f"queue item {item_id} source_label",
        maximum=500,
    )
    if "\n" in label or "\r" in label:
        raise PreparedPackError(f"queue item {item_id} source_label is not one line")
    _assert_answer_safe_text(label, f"queue item {item_id} source_label")
    return label


def _normalize_source_contract(
    *,
    item_id: str,
    source_id: object,
    source_label: object | None,
    source_id_provenance: object | None,
    formal_node_id: object | None,
) -> dict[str, Any]:
    machine_id = _consumer_source_id(source_id, f"queue item {item_id} source_id")
    label = _bounded_text(
        source_label if source_label is not None else machine_id,
        f"queue item {item_id} source_label",
        maximum=500,
    )
    if "\n" in label or "\r" in label:
        raise PreparedPackError(f"queue item {item_id} source_label is not one line")
    _assert_answer_safe_text(label, f"queue item {item_id} source_label")
    provenance = str(source_id_provenance or "legacy_queue_source_id").strip()
    if provenance not in SOURCE_ID_PROVENANCE:
        raise PreparedPackError(
            f"queue item {item_id} source_id_provenance is invalid"
        )
    formal = str(formal_node_id or "").strip()
    if formal == "none":
        formal = ""
    if formal and not FORMAL_RE.fullmatch(formal):
        raise PreparedPackError(f"queue item {item_id} formal_node_id is invalid")
    if provenance == "formal_node_fallback" and (
        not formal or machine_id != formal
    ):
        raise PreparedPackError(
            f"queue item {item_id} formal-node source fallback is not exact"
        )
    return {
        "source_id": machine_id,
        "source_label": label,
        "source_id_provenance": provenance,
        "formal_node_id": formal or None,
    }


def _manifest_item_source_contract(row: object, item_id: str) -> dict[str, Any]:
    if not isinstance(row, dict):
        raise PreparedPackError(f"prepared item {item_id} is invalid")
    return _normalize_source_contract(
        item_id=item_id,
        source_id=row.get("source_id"),
        source_label=row.get("source_label"),
        source_id_provenance=row.get("source_id_provenance"),
        formal_node_id=row.get("formal_node_id"),
    )


def _queue_generation_binding(queue_path: Path) -> dict[str, Any]:
    text = queue_path.read_text(encoding="utf-8")
    fields = {
        name: (match.group(1).strip() if match else None)
        for name in (
            "generation_id",
            "generation_input_sha256",
            "eligible_due_action_set_sha256",
            "formal_due_source_sha256",
            "scheduling_policy_version",
        )
        for match in [re.search(rf"(?m)^{re.escape(name)}:\s*(\S+)\s*$", text)]
    }
    if fields["generation_id"] is None:
        return {
            "generation_id": None,
            "generation_input_sha256": None,
            "eligible_due_action_set_sha256": None,
            "formal_due_source_sha256": None,
            "scheduling_policy_version": None,
        }
    if not SAFE_ID_RE.fullmatch(str(fields["generation_id"])):
        raise PreparedPackError("queue generation_id is invalid")
    for name in (
        "generation_input_sha256",
        "eligible_due_action_set_sha256",
        "formal_due_source_sha256",
    ):
        if not SHA_RE.fullmatch(str(fields[name] or "")):
            raise PreparedPackError(f"queue {name} is invalid")
    if not SAFE_ID_RE.fullmatch(str(fields["scheduling_policy_version"] or "")):
        raise PreparedPackError("queue scheduling_policy_version is invalid")
    date_match = re.search(r"(?m)^review_date:\s*(\d{4}-\d{2}-\d{2})\s*$", text)
    if (
        date_match is None
        or fields["generation_id"]
        != f"MRG-{date_match.group(1)}-{fields['generation_input_sha256']}"
    ):
        raise PreparedPackError("queue generation identity does not match its date/input")
    return fields


def _parse_queue(queue_path: Path) -> tuple[str, str, list[dict[str, Any]]]:
    raw = queue_path.read_bytes()
    text = raw.decode("utf-8")
    schema_match = re.search(r"(?m)^schema:\s*(\S+)\s*$", text)
    date_match = re.search(r"(?m)^review_date:\s*(\d{4}-\d{2}-\d{2})\s*$", text)
    schema = schema_match.group(1) if schema_match else ""
    if schema not in {"morning_review_queue_v2", "morning_review_queue_v3", "morning_review_action_queue_v3"}:
        raise PreparedPackError("queue schema is unsupported")
    review_date = date_match.group(1) if date_match else ""
    try:
        date.fromisoformat(review_date)
    except ValueError as exc:
        raise PreparedPackError("queue review_date is invalid") from exc
    headings = list(QUEUE_HEADING_RE.finditer(text))
    items: list[dict[str, Any]] = []
    for index, heading in enumerate(headings):
        end = headings[index + 1].start() if index + 1 < len(headings) else len(text)
        section = text[heading.start():end]
        item_id = heading.group("item")
        source_id = _field(section, "source_id") or _field(section, "来源复习单元") or _field(section, "来源派生项")
        item_kind = _field(section, "item_kind")
        if not source_id:
            raise PreparedPackError(f"queue item {item_id} has no source_id")
        if item_kind not in ITEM_KINDS:
            if schema == "morning_review_queue_v2":
                item_kind = "original_question" if item_id.startswith(("OQ-", "AOQ-")) else "formal_due"
            else:
                raise PreparedPackError(f"queue item {item_id} has invalid item_kind")
        source_contract = _normalize_source_contract(
            item_id=item_id,
            source_id=source_id,
            source_label=_queue_source_label(section, source_id, item_id),
            source_id_provenance=_field(section, "source_id_provenance"),
            formal_node_id=_field(section, "formal_node_id"),
        )
        items.append(
            {
                "item_id": item_id,
                **source_contract,
                "item_kind": item_kind,
                "action_id": _field(section, "action_id"),
                "trigger_event_id": _field(section, "trigger_event_id"),
                "stage": _field(section, "review_stage")
                or _field(section, "stage"),
                "review_unit_id": _field(section, "review_unit_id"),
            }
        )
    if not items or len(items) > MAX_ITEMS:
        raise PreparedPackError("queue item count is outside the prepared-pack bound")
    if len({item["item_id"] for item in items}) != len(items):
        raise PreparedPackError("queue contains duplicate item IDs")
    return schema, review_date, items


def _normalize_target(value: object, label: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise PreparedPackError(f"{label} must be an object")
    allowed = {
        "subject",
        "point_ids",
        "mechanism_ids",
        "mechanism_primitive_ids",
        "formal_node_ids",
        "module_ids",
        "safe_topic_terms",
        "error_pattern_ids",
        "method_ids",
    }
    if set(value) - allowed:
        raise PreparedPackError(f"{label} contains protected or unknown fields")
    subject = value.get("subject")
    if subject is not None and subject not in {"DS", "CO", "OS", "CN"}:
        raise PreparedPackError(f"{label}.subject is invalid")
    normalized = {
        "subject": subject,
        "point_ids": _string_list(value.get("point_ids"), f"{label}.point_ids", maximum=8, item_bytes=24, pattern=POINT_RE),
        "mechanism_ids": _string_list(value.get("mechanism_ids"), f"{label}.mechanism_ids", maximum=8, item_bytes=160, pattern=SAFE_ID_RE),
        "mechanism_primitive_ids": _string_list(value.get("mechanism_primitive_ids"), f"{label}.mechanism_primitive_ids", maximum=8, item_bytes=160, pattern=SAFE_ID_RE),
        "formal_node_ids": _string_list(value.get("formal_node_ids"), f"{label}.formal_node_ids", maximum=4, item_bytes=32, pattern=FORMAL_RE),
        "module_ids": _string_list(value.get("module_ids"), f"{label}.module_ids", maximum=4, item_bytes=16, pattern=MODULE_RE),
        "safe_topic_terms": _string_list(value.get("safe_topic_terms"), f"{label}.safe_topic_terms", maximum=8, item_bytes=120),
        "error_pattern_ids": _string_list(value.get("error_pattern_ids"), f"{label}.error_pattern_ids", maximum=6, item_bytes=80, pattern=SAFE_ID_RE),
        "method_ids": _string_list(value.get("method_ids"), f"{label}.method_ids", maximum=6, item_bytes=80, pattern=SAFE_ID_RE),
    }
    _assert_answer_safe_text(json.dumps(normalized, ensure_ascii=False), label)
    return normalized


def _normalize_surface(
    value: object,
    item_id: str,
    *,
    surface_role: str = "main",
) -> dict[str, Any]:
    if surface_role not in {"main", "guided", "repair"}:
        raise PreparedPackError(f"{item_id}.surface role is invalid")
    if not isinstance(value, dict) or set(value) - {"stem", "options", "response_instruction", "question_asset"}:
        raise PreparedPackError(f"{item_id}.surface has invalid fields")
    stem = _bounded_text(value.get("stem"), f"{item_id}.surface.stem", maximum=5000, minimum=8)
    instruction = _bounded_text(
        value.get("response_instruction", "请回复选项、理由和答后置信度。"),
        f"{item_id}.surface.response_instruction",
        maximum=500,
    )
    options = value.get("options")
    if not isinstance(options, dict) or set(options) != set(OPTIONS):
        raise PreparedPackError(f"{item_id}.surface.options must be exactly A/B/C/D")
    normalized_options = {
        key: _bounded_text(options[key], f"{item_id}.surface.options.{key}", maximum=1800)
        for key in OPTIONS
    }
    comparable = [re.sub(r"\s+", "", text).casefold() for text in normalized_options.values()]
    if len(set(comparable)) != 4:
        raise PreparedPackError(f"{item_id}.surface options are not distinct")
    public_text = "\n".join([stem, instruction, *normalized_options.values()])
    _assert_answer_safe_text(public_text, f"{item_id}.surface")
    normalized = {
        "schema": SURFACE_SCHEMA,
        "item_id": item_id,
        "surface_role": surface_role,
        "stem": stem,
        "options": normalized_options,
        "response_instruction": instruction,
        "answer_safe": True,
    }
    question_asset = _normalize_question_asset(value.get("question_asset"), item_id)
    if question_asset is not None:
        normalized["question_asset"] = question_asset
    return normalized


def _normalize_evaluator(
    value: object,
    item_id: str,
    *,
    surface_role: str = "main",
) -> dict[str, Any]:
    if surface_role not in {"main", "guided", "repair"}:
        raise PreparedPackError(f"{item_id}.evaluator role is invalid")
    if not isinstance(value, dict):
        raise PreparedPackError(f"{item_id}.evaluator must be an object")
    allowed = {
        "correct_option",
        "decisive_reason",
        "accepted_reasoning_points",
        "first_break_by_choice",
        "feedback_by_result",
        "prepared_boundary_checks",
    }
    if set(value) != allowed:
        raise PreparedPackError(f"{item_id}.evaluator fields are incomplete or unknown")
    key = value.get("correct_option")
    if key not in OPTIONS:
        raise PreparedPackError(f"{item_id}.evaluator.correct_option is invalid")
    points = _string_list(
        value.get("accepted_reasoning_points"),
        f"{item_id}.evaluator.accepted_reasoning_points",
        maximum=8,
        item_bytes=700,
    )
    if not points:
        raise PreparedPackError(f"{item_id}.evaluator needs accepted reasoning points")
    breaks = value.get("first_break_by_choice")
    if not isinstance(breaks, dict) or set(breaks) != set(OPTIONS):
        raise PreparedPackError(f"{item_id}.evaluator.first_break_by_choice must cover A/B/C/D")
    normalized_breaks = {
        option: _bounded_text(breaks[option], f"{item_id}.first_break_by_choice.{option}", maximum=500)
        for option in OPTIONS
    }
    feedback = value.get("feedback_by_result")
    if not isinstance(feedback, dict) or set(feedback) != RESULTS:
        raise PreparedPackError(f"{item_id}.evaluator.feedback_by_result must cover all results")
    normalized_feedback = {
        result: _bounded_text(feedback[result], f"{item_id}.feedback_by_result.{result}", maximum=1600)
        for result in sorted(RESULTS)
    }
    return {
        "schema": EVALUATOR_SCHEMA,
        "item_id": item_id,
        "surface_role": surface_role,
        "correct_option": key,
        "decisive_reason": _bounded_text(value.get("decisive_reason"), f"{item_id}.decisive_reason", maximum=1800),
        "accepted_reasoning_points": points,
        "first_break_by_choice": normalized_breaks,
        "feedback_by_result": normalized_feedback,
        "prepared_boundary_checks": _string_list(value.get("prepared_boundary_checks"), f"{item_id}.prepared_boundary_checks", maximum=8, item_bytes=500),
        "private": True,
    }


def _normalize_branch(
    value: object,
    item_id: str,
    surface_role: str,
) -> dict[str, Any]:
    if not isinstance(value, dict) or set(value) != {"surface", "evaluator"}:
        raise PreparedPackError(
            f"{item_id}.{surface_role} must contain one surface and evaluator"
        )
    return {
        "surface": _normalize_surface(
            value.get("surface"), item_id, surface_role=surface_role
        ),
        "evaluator": _normalize_evaluator(
            value.get("evaluator"), item_id, surface_role=surface_role
        ),
    }


def _normalize_evidence(repo: Path, value: object, item_id: str) -> list[dict[str, str]]:
    if not isinstance(value, list) or not value or len(value) > 8:
        raise PreparedPackError(f"{item_id}.evidence must contain 1 to 8 entries")
    result: list[dict[str, str]] = []
    for index, row in enumerate(value):
        if not isinstance(row, dict) or set(row) != {"source_ref", "sha256", "role"}:
            raise PreparedPackError(f"{item_id}.evidence[{index}] is invalid")
        path = _repo_file(repo, row.get("source_ref"), f"{item_id}.evidence[{index}]")
        expected = str(row.get("sha256") or "")
        if not SHA_RE.fullmatch(expected) or _sha256_file(path) != expected:
            raise PreparedPackError(f"{item_id}.evidence[{index}] hash drifted")
        role = _bounded_text(
            row.get("role"), f"{item_id}.evidence[{index}].role", maximum=120
        )
        _assert_answer_safe_text(role, f"{item_id}.evidence[{index}].role")
        result.append(
            {
                "source_ref": path.relative_to(repo).as_posix(),
                "sha256": expected,
                "role": role,
            }
        )
    return result


def _normalize_draft(repo: Path, queue_path: Path, raw: dict[str, Any]) -> dict[str, Any]:
    allowed = {"schema", "author_actor_id", "author_execution_id", "queue_sha256", "items"}
    if set(raw) != allowed or raw.get("schema") != DRAFT_SCHEMA:
        raise PreparedPackError("draft schema or fields are invalid")
    queue_schema, review_date, queue_items = _parse_queue(queue_path)
    generation_binding = _queue_generation_binding(queue_path)
    queue_sha = _sha256_file(queue_path)
    if raw.get("queue_sha256") != queue_sha:
        raise PreparedPackError("draft queue hash does not match the selected queue")
    items = raw.get("items")
    if not isinstance(items, list) or len(items) != len(queue_items):
        raise PreparedPackError("draft must prepare every queue item exactly once")
    queue_by_id = {item["item_id"]: item for item in queue_items}
    normalized_items: list[dict[str, Any]] = []
    for index, raw_item in enumerate(items):
        if not isinstance(raw_item, dict):
            raise PreparedPackError(f"draft.items[{index}] must be an object")
        allowed_item = {
            "item_id",
            "source_id",
            "item_kind",
            "target",
            "surface",
            "evaluator",
            "guided",
            "repair",
            "evidence",
        }
        if set(raw_item) != allowed_item:
            raise PreparedPackError(f"draft.items[{index}] has incomplete or unknown fields")
        item_id = str(raw_item.get("item_id") or "")
        if not ITEM_ID_RE.fullmatch(item_id) or item_id not in queue_by_id:
            raise PreparedPackError(f"draft.items[{index}] has an invalid item_id")
        queue_item = queue_by_id[item_id]
        if raw_item.get("source_id") != queue_item["source_id"] or raw_item.get("item_kind") != queue_item["item_kind"]:
            raise PreparedPackError(f"draft item {item_id} identity differs from the queue")
        normalized_items.append(
            {
                "item_id": item_id,
                "source_id": queue_item["source_id"],
                "source_label": queue_item["source_label"],
                "source_id_provenance": queue_item["source_id_provenance"],
                "item_kind": queue_item["item_kind"],
                "action_id": queue_item.get("action_id"),
                "trigger_event_id": queue_item.get("trigger_event_id"),
                "stage": queue_item.get("stage"),
                "formal_node_id": queue_item.get("formal_node_id"),
                "review_unit_id": queue_item.get("review_unit_id"),
                "target": _normalize_target(raw_item.get("target"), f"{item_id}.target"),
                "surface": _normalize_surface(raw_item.get("surface"), item_id),
                "evaluator": _normalize_evaluator(raw_item.get("evaluator"), item_id),
                "guided": _normalize_branch(
                    raw_item.get("guided"), item_id, "guided"
                ),
                "repair": _normalize_branch(
                    raw_item.get("repair"), item_id, "repair"
                ),
                "evidence": _normalize_evidence(repo, raw_item.get("evidence"), item_id),
            }
        )
    if [item["item_id"] for item in normalized_items] != [item["item_id"] for item in queue_items]:
        raise PreparedPackError("draft item order must exactly match the queue")
    return {
        "schema": DRAFT_SCHEMA,
        "author_actor_id": _safe_id(raw.get("author_actor_id"), "author_actor_id"),
        "author_execution_id": _safe_id(raw.get("author_execution_id"), "author_execution_id"),
        "queue_ref": queue_path.relative_to(repo).as_posix(),
        "queue_schema": queue_schema,
        "queue_sha256": queue_sha,
        "review_date": review_date,
        **generation_binding,
        "items": normalized_items,
    }


def _item_hashes(draft: dict[str, Any]) -> list[dict[str, str]]:
    return [
        {
            "item_id": item["item_id"],
            "surface_sha256": _sha256_bytes(_json_bytes(item["surface"], pretty=True)),
            "evaluator_sha256": _sha256_bytes(_json_bytes(item["evaluator"], pretty=True)),
            "guided_surface_sha256": _sha256_bytes(
                _json_bytes(item["guided"]["surface"], pretty=True)
            ),
            "guided_evaluator_sha256": _sha256_bytes(
                _json_bytes(item["guided"]["evaluator"], pretty=True)
            ),
            "repair_surface_sha256": _sha256_bytes(
                _json_bytes(item["repair"]["surface"], pretty=True)
            ),
            "repair_evaluator_sha256": _sha256_bytes(
                _json_bytes(item["repair"]["evaluator"], pretty=True)
            ),
        }
        for item in draft["items"]
    ]


def draft_hashes(repo: Path, queue_path: Path, draft_path: Path) -> dict[str, Any]:
    draft = _normalize_draft(repo, queue_path, _read_json(draft_path, "draft"))
    digest = _digest(draft)
    return {
        "status": "ready_for_independent_verification",
        "schema": DRAFT_SCHEMA,
        "draft_sha256": digest,
        "queue_sha256": draft["queue_sha256"],
        "item_count": len(draft["items"]),
        "item_hashes": _item_hashes(draft),
        "verification_checks_required": list(CHECKS),
        "answer_safe": True,
        "learner_evidence_write_count": 0,
        "formal_write_count": 0,
    }


def _normalize_verification(raw: dict[str, Any], draft: dict[str, Any]) -> dict[str, Any]:
    allowed = {"schema", "draft_sha256", "verifier_actor_id", "verifier_execution_id", "items"}
    if set(raw) != allowed or raw.get("schema") != VERIFICATION_SCHEMA:
        raise PreparedPackError("verification schema or fields are invalid")
    draft_sha = _digest(draft)
    if raw.get("draft_sha256") != draft_sha:
        raise PreparedPackError("verification does not bind the normalized draft")
    verifier_actor = _safe_id(raw.get("verifier_actor_id"), "verifier_actor_id")
    verifier_execution = _safe_id(raw.get("verifier_execution_id"), "verifier_execution_id")
    if verifier_actor == draft["author_actor_id"] or verifier_execution == draft["author_execution_id"]:
        raise PreparedPackError("author and verifier identities must be separated")
    rows = raw.get("items")
    hashes = {row["item_id"]: row for row in _item_hashes(draft)}
    if not isinstance(rows, list) or len(rows) != len(hashes):
        raise PreparedPackError("verification must cover every draft item")
    normalized_rows: list[dict[str, Any]] = []
    for index, row in enumerate(rows):
        if not isinstance(row, dict):
            raise PreparedPackError(f"verification.items[{index}] must be an object")
        item_id = str(row.get("item_id") or "")
        expected = hashes.get(item_id)
        if expected is None:
            raise PreparedPackError(f"verification.items[{index}] has an unknown item")
        if set(row) != {*expected, "checks"} or any(
            row.get(key) != expected_value
            for key, expected_value in expected.items()
        ):
            raise PreparedPackError(f"verification item {item_id} hash binding drifted")
        checks = row.get("checks")
        if not isinstance(checks, dict) or set(checks) != set(CHECKS) or any(checks[key] is not True for key in CHECKS):
            raise PreparedPackError(f"verification item {item_id} did not pass every semantic gate")
        normalized_rows.append({**expected, "checks": {key: True for key in CHECKS}})
    if [row["item_id"] for row in normalized_rows] != [item["item_id"] for item in draft["items"]]:
        raise PreparedPackError("verification item order differs from the draft")
    return {
        "schema": VERIFICATION_SCHEMA,
        "draft_sha256": draft_sha,
        "verifier_actor_id": verifier_actor,
        "verifier_execution_id": verifier_execution,
        "items": normalized_rows,
    }


def publish_pack(repo: Path, queue_path: Path, draft_path: Path, verification_path: Path) -> dict[str, Any]:
    draft = _normalize_draft(repo, queue_path, _read_json(draft_path, "draft"))
    verification = _normalize_verification(
        _read_json(verification_path, "verification"), draft
    )
    try:
        question_exposure.assert_no_recent_surface_reuse(
            repo,
            date.fromisoformat(draft["review_date"]),
            (item["surface"] for item in draft["items"]),
        )
    except question_exposure.QuestionExposureError as exc:
        raise PreparedPackError(
            f"learner-visible question reuse gate failed: {exc}"
        ) from exc
    root = repo / PACK_ROOT_REL
    manifest_dir = root / "objects" / "manifests"
    surface_dir = root / "objects" / "surfaces"
    evaluator_dir = root / "private" / "evaluators"
    verification_dir = root / "private" / "verifications"
    pointer_dir = root / "pointers"
    for directory in (
        root,
        manifest_dir,
        surface_dir,
        evaluator_dir,
        verification_dir,
        pointer_dir,
    ):
        _reject_symlink_components(directory, repo, "prepared-pack managed directory")
        directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        if not directory.is_dir() or directory.is_symlink():
            raise PreparedPackError("prepared-pack managed directory is unsafe")
        os.chmod(directory, 0o700)
    with _lock(root / ".prepared-pack.lock"):
        verification_path, verification_sha = _content_write(
            verification_dir, verification
        )
        item_rows: list[dict[str, Any]] = []
        for item in draft["items"]:
            _, surface_sha = _content_write(surface_dir, item["surface"])
            _, evaluator_sha = _content_write(evaluator_dir, item["evaluator"])
            _, guided_surface_sha = _content_write(
                surface_dir, item["guided"]["surface"]
            )
            _, guided_evaluator_sha = _content_write(
                evaluator_dir, item["guided"]["evaluator"]
            )
            _, repair_surface_sha = _content_write(
                surface_dir, item["repair"]["surface"]
            )
            _, repair_evaluator_sha = _content_write(
                evaluator_dir, item["repair"]["evaluator"]
            )
            item_rows.append(
                {
                    "item_id": item["item_id"],
                    "source_id": item["source_id"],
                    "source_label": item["source_label"],
                    "source_id_provenance": item["source_id_provenance"],
                    "item_kind": item["item_kind"],
                    "action_id": item.get("action_id"),
                    "trigger_event_id": item.get("trigger_event_id"),
                    "stage": item.get("stage"),
                    "formal_node_id": item.get("formal_node_id"),
                    "review_unit_id": item.get("review_unit_id"),
                    "surface_sha256": surface_sha,
                    "grading_sha256": evaluator_sha,
                    "guided_surface_sha256": guided_surface_sha,
                    "guided_grading_sha256": guided_evaluator_sha,
                    "repair_surface_sha256": repair_surface_sha,
                    "repair_grading_sha256": repair_evaluator_sha,
                    "target": item["target"],
                    "evidence": item["evidence"],
                    "verification": next(row for row in verification["items"] if row["item_id"] == item["item_id"]),
                }
            )
        pack_commitment_sha = _digest(
            {
                "schema": "morning_review_pack_commitment_v1",
                "generation_id": draft.get("generation_id"),
                "eligible_due_action_set_sha256": draft.get(
                    "eligible_due_action_set_sha256"
                ),
                "queue_sha256": draft["queue_sha256"],
                "items": item_rows,
            }
        )
        item_rows = [
            {
                **row,
                "queue_sha256": draft["queue_sha256"],
                "pack_commitment_sha256": pack_commitment_sha,
            }
            for row in item_rows
        ]
        manifest_without_id = {
            "schema": MANIFEST_SCHEMA,
            "kind": "morning_review_prepared_delivery_pack",
            "binding_status": "pack_bound",
            "interaction_mode": INTERACTION_MODE,
            "review_date": draft["review_date"],
            "queue_ref": draft["queue_ref"],
            "queue_schema": draft["queue_schema"],
            "queue_sha256": draft["queue_sha256"],
            "generation_id": draft.get("generation_id"),
            "generation_input_sha256": draft.get("generation_input_sha256"),
            "eligible_due_action_set_sha256": draft.get(
                "eligible_due_action_set_sha256"
            ),
            "formal_due_source_sha256": draft.get("formal_due_source_sha256"),
            "scheduling_policy_version": draft.get("scheduling_policy_version"),
            "pack_commitment_sha256": pack_commitment_sha,
            "item_order": [item["item_id"] for item in item_rows],
            "items": item_rows,
            "draft_sha256": verification["draft_sha256"],
            "verification_ref": verification_path.relative_to(repo).as_posix(),
            "verification_sha256": verification_sha,
            "author_actor_id": draft["author_actor_id"],
            "author_execution_id": draft["author_execution_id"],
            "verifier_actor_id": verification["verifier_actor_id"],
            "verifier_execution_id": verification["verifier_execution_id"],
            "answer_isolated": True,
            "learner_evidence_write_count": 0,
            "formal_write_count": 0,
        }
        pack_id = "MRP-" + _digest(manifest_without_id)[:24].upper()
        manifest = {**manifest_without_id, "pack_id": pack_id}
        manifest_path, manifest_sha = _content_write(manifest_dir, manifest)
        pointer = {
            "schema": POINTER_SCHEMA,
            "pack_id": pack_id,
            "manifest_ref": manifest_path.relative_to(repo).as_posix(),
            "manifest_sha256": manifest_sha,
        }
        pointer_path = pointer_dir / f"{pack_id}.json"
        if pointer_path.exists():
            if _read_json(pointer_path, "prepared-pack pointer", max_bytes=MAX_OBJECT_BYTES) != pointer:
                raise PreparedPackError("prepared-pack pointer is already bound differently")
        else:
            _atomic_write(pointer_path, pointer)
    return {
        "status": "ready",
        "binding_status": "pack_bound",
        "pack_id": pack_id,
        "manifest_ref": manifest_path.relative_to(repo).as_posix(),
        "manifest_sha256": manifest_sha,
        "queue_sha256": draft["queue_sha256"],
        "generation_id": draft.get("generation_id"),
        "generation_input_sha256": draft.get("generation_input_sha256"),
        "eligible_due_action_set_sha256": draft.get(
            "eligible_due_action_set_sha256"
        ),
        "formal_due_source_sha256": draft.get("formal_due_source_sha256"),
        "scheduling_policy_version": draft.get("scheduling_policy_version"),
        "pack_commitment_sha256": pack_commitment_sha,
        "item_count": len(item_rows),
        "all_items_ready": True,
        "answer_isolated": True,
        "learner_evidence_write_count": 0,
        "formal_write_count": 0,
    }


def _validate_pack_commitment(manifest: dict[str, Any]) -> None:
    commitment = manifest.get("pack_commitment_sha256")
    if commitment is None:
        return
    rows = manifest.get("items")
    if not SHA_RE.fullmatch(str(commitment)) or not isinstance(rows, list):
        raise PreparedPackError("prepared-pack commitment is invalid")
    commitment_rows = []
    for row in rows:
        if not isinstance(row, dict):
            raise PreparedPackError("prepared-pack item commitment is invalid")
        if (
            row.get("queue_sha256") != manifest.get("queue_sha256")
            or row.get("pack_commitment_sha256") != commitment
        ):
            raise PreparedPackError("prepared-pack item commitment drifted")
        commitment_rows.append(
            {
                key: value
                for key, value in row.items()
                if key not in {"queue_sha256", "pack_commitment_sha256"}
            }
        )
    expected_commitment = _digest(
        {
            "schema": "morning_review_pack_commitment_v1",
            "generation_id": manifest.get("generation_id"),
            "eligible_due_action_set_sha256": manifest.get(
                "eligible_due_action_set_sha256"
            ),
            "queue_sha256": manifest.get("queue_sha256"),
            "items": commitment_rows,
        }
    )
    if expected_commitment != commitment:
        raise PreparedPackError("prepared-pack commitment hash drifted")


def _load_manifest(
    repo: Path,
    manifest_ref: str | Path,
    expected_sha: str | None = None,
    *,
    verify_pack_commitment: bool = True,
) -> tuple[dict[str, Any], Path, str]:
    raw = Path(manifest_ref)
    root = (repo / PACK_ROOT_REL).resolve()
    lexical = raw.expanduser() if raw.is_absolute() else repo / raw
    _reject_symlink_components(lexical, root, "prepared-pack manifest")
    path = lexical.resolve()
    if not _inside(path, root):
        raise PreparedPackError("prepared-pack manifest escapes its managed root")
    manifest = _read_json(path, "prepared-pack manifest", max_bytes=MAX_OBJECT_BYTES)
    digest = _sha256_file(path)
    manifest_root = (root / "objects" / "manifests").resolve()
    if path.parent != manifest_root or path.name != f"{digest}.json":
        raise PreparedPackError(
            "prepared-pack manifest is not at its content-addressed location"
        )
    info = path.stat()
    parent_info = path.parent.stat()
    if (
        info.st_uid != os.geteuid()
        or parent_info.st_uid != os.geteuid()
        or info.st_mode & 0o077
        or parent_info.st_mode & 0o077
    ):
        raise PreparedPackError(
            "prepared-pack manifest permissions or ownership are unsafe"
        )
    if expected_sha is not None and digest != expected_sha:
        raise PreparedPackError("prepared-pack manifest hash drifted")
    if manifest.get("schema") != MANIFEST_SCHEMA or manifest.get("interaction_mode") != INTERACTION_MODE:
        raise PreparedPackError("prepared-pack manifest contract is invalid")
    if manifest.get("answer_isolated") is not True or manifest.get("formal_write_count") != 0 or manifest.get("learner_evidence_write_count") != 0:
        raise PreparedPackError("prepared-pack manifest crosses a persistence boundary")
    if verify_pack_commitment:
        _validate_pack_commitment(manifest)
    payload = dict(manifest)
    pack_id = payload.pop("pack_id", None)
    expected_id = "MRP-" + _digest(payload)[:24].upper()
    if pack_id != expected_id:
        raise PreparedPackError("prepared-pack identity drifted")
    return manifest, path, digest


def _manifest_item(manifest: dict[str, Any], item_id: str) -> dict[str, Any]:
    matches = [row for row in manifest.get("items", []) if isinstance(row, dict) and row.get("item_id") == item_id]
    if len(matches) != 1:
        raise PreparedPackError("prepared-pack item is missing or duplicated")
    return matches[0]


def _load_manifest_verification(
    repo: Path,
    manifest: dict[str, Any],
) -> dict[str, Any]:
    ref = manifest.get("verification_ref")
    path = _repo_file(repo, ref, "prepared-pack verification")
    verification_root = (repo / PACK_ROOT_REL / "private" / "verifications").resolve()
    if not _inside(path, verification_root):
        raise PreparedPackError("prepared-pack verification escapes its private root")
    expected_sha = str(manifest.get("verification_sha256") or "")
    if not SHA_RE.fullmatch(expected_sha) or _sha256_file(path) != expected_sha:
        raise PreparedPackError("prepared-pack verification commitment drifted")
    info = path.stat()
    parent_info = path.parent.stat()
    if (
        info.st_uid != os.geteuid()
        or parent_info.st_uid != os.geteuid()
        or info.st_mode & 0o077
        or parent_info.st_mode & 0o077
    ):
        raise PreparedPackError(
            "prepared-pack verification permissions or ownership are unsafe"
        )
    verification = _read_json(
        path, "prepared-pack verification", max_bytes=MAX_OBJECT_BYTES
    )
    if (
        verification.get("schema") != VERIFICATION_SCHEMA
        or verification.get("draft_sha256") != manifest.get("draft_sha256")
        or verification.get("verifier_actor_id")
        != manifest.get("verifier_actor_id")
        or verification.get("verifier_execution_id")
        != manifest.get("verifier_execution_id")
    ):
        raise PreparedPackError("prepared-pack verification identity drifted")
    if (
        manifest.get("author_actor_id") == manifest.get("verifier_actor_id")
        or manifest.get("author_execution_id")
        == manifest.get("verifier_execution_id")
    ):
        raise PreparedPackError(
            "prepared-pack author and verifier identities are not separated"
        )
    rows = verification.get("items")
    manifest_rows = manifest.get("items")
    if not isinstance(rows, list) or not isinstance(manifest_rows, list):
        raise PreparedPackError("prepared-pack verification coverage is invalid")
    if len(rows) != len(manifest_rows):
        raise PreparedPackError("prepared-pack verification coverage drifted")
    for verification_row, manifest_row in zip(rows, manifest_rows):
        expected = {
            "item_id": manifest_row.get("item_id"),
            "surface_sha256": manifest_row.get("surface_sha256"),
            "evaluator_sha256": manifest_row.get("grading_sha256"),
            "guided_surface_sha256": manifest_row.get("guided_surface_sha256"),
            "guided_evaluator_sha256": manifest_row.get(
                "guided_grading_sha256"
            ),
            "repair_surface_sha256": manifest_row.get("repair_surface_sha256"),
            "repair_evaluator_sha256": manifest_row.get(
                "repair_grading_sha256"
            ),
        }
        if not isinstance(verification_row, dict) or set(verification_row) != {
            *expected,
            "checks",
        }:
            raise PreparedPackError("prepared-pack verification row is invalid")
        if any(
            verification_row.get(key) != value for key, value in expected.items()
        ):
            raise PreparedPackError("prepared-pack verification hash binding drifted")
        checks = verification_row.get("checks")
        if (
            not isinstance(checks, dict)
            or set(checks) != set(CHECKS)
            or any(checks.get(key) is not True for key in CHECKS)
        ):
            raise PreparedPackError("prepared-pack verification gates drifted")
        if manifest_row.get("verification") != verification_row:
            raise PreparedPackError(
                "prepared-pack embedded verification differs from its object"
            )
    return verification


def validate_prepared_pack_for_start(
    repo: str | Path,
    *,
    manifest_ref: str | Path,
    queue_path: str | Path,
    queue_sha256: str,
    selected_items: list[dict[str, Any]],
) -> dict[str, Any]:
    root = Path(repo).resolve()
    manifest, path, manifest_sha = _load_manifest(root, manifest_ref)
    audit = audit_pack(root, path, verify_sources=True)
    if audit.get("status") != "PASS":
        raise PreparedPackError(
            "prepared pack failed its complete pre-session audit: "
            + "; ".join(str(error) for error in audit.get("errors", []))
        )
    for row in manifest.get("items", []):
        item_id = str(row.get("item_id") or "") if isinstance(row, dict) else ""
        _manifest_item_source_contract(row, item_id or "unknown")
    queue = Path(queue_path).resolve()
    expected_ref = queue.relative_to(root).as_posix()
    if manifest.get("queue_ref") != expected_ref or manifest.get("queue_sha256") != queue_sha256 or _sha256_file(queue) != queue_sha256:
        raise PreparedPackError("prepared pack and queue are not the same immutable snapshot")
    queue_generation = _queue_generation_binding(queue)
    for field in (
        "generation_id",
        "generation_input_sha256",
        "eligible_due_action_set_sha256",
        "formal_due_source_sha256",
        "scheduling_policy_version",
    ):
        if manifest.get(field) != queue_generation.get(field):
            raise PreparedPackError(
                f"prepared pack {field} differs from its queue generation"
            )
    expected = [
        {
            "item_id": item["item_id"],
            "source_id": item["source_id"],
            "item_kind": item["item_kind"],
            "action_id": item.get("action_id"),
            "trigger_event_id": item.get("trigger_event_id"),
            "stage": item.get("stage"),
        }
        for item in selected_items
    ]
    actual = [
        {
            "item_id": item.get("item_id"),
            "source_id": item.get("source_id"),
            "item_kind": item.get("item_kind"),
            "action_id": item.get("action_id"),
            "trigger_event_id": item.get("trigger_event_id"),
            "stage": item.get("stage"),
        }
        for item in manifest.get("items", [])
        if item.get("item_id") in {row["item_id"] for row in expected}
    ]
    if actual != expected or manifest.get("item_order") != [row["item_id"] for row in manifest.get("items", [])]:
        raise PreparedPackError("prepared pack does not exactly cover the selected session items")
    bindings = {
        row["item_id"]: {
            "surface_sha256": row["surface_sha256"],
            "grading_sha256": row["grading_sha256"],
            "guided_surface_sha256": row["guided_surface_sha256"],
            "guided_grading_sha256": row["guided_grading_sha256"],
            "repair_surface_sha256": row["repair_surface_sha256"],
            "repair_grading_sha256": row["repair_grading_sha256"],
            "action_id": row.get("action_id"),
            "trigger_event_id": row.get("trigger_event_id"),
            "stage": row.get("stage"),
            "source_label": row.get("source_label", row.get("source_id")),
            "source_id_provenance": row.get(
                "source_id_provenance", "legacy_queue_source_id"
            ),
            "formal_node_id": row.get("formal_node_id"),
            "review_unit_id": row.get("review_unit_id"),
            "queue_sha256": row.get("queue_sha256", manifest["queue_sha256"]),
            "pack_commitment_sha256": row.get("pack_commitment_sha256"),
        }
        for row in manifest["items"]
        if row["item_id"] in {item["item_id"] for item in selected_items}
    }
    return {
        "interaction_mode": INTERACTION_MODE,
        "binding_status": "pack_bound",
        "delivery_pack_ref": path.relative_to(root).as_posix(),
        "delivery_pack_sha256": manifest_sha,
        "delivery_pack_id": manifest["pack_id"],
        "generation_id": manifest.get("generation_id"),
        "generation_input_sha256": manifest.get("generation_input_sha256"),
        "eligible_due_action_set_sha256": manifest.get(
            "eligible_due_action_set_sha256"
        ),
        "formal_due_source_sha256": manifest.get("formal_due_source_sha256"),
        "pack_commitment_sha256": manifest.get("pack_commitment_sha256"),
        "item_bindings": bindings,
    }


def _session_pack_binding(repo: Path, session_id: str) -> tuple[dict[str, Any], dict[str, Any]]:
    if not SESSION_ID_RE.fullmatch(session_id):
        raise PreparedPackError("session ID is unsafe")
    state_path = repo / SESSION_ROOT_REL / session_id / "state.json"
    _reject_symlink_components(
        state_path,
        (repo / SESSION_ROOT_REL).resolve(),
        "morning session state",
    )
    hot_manifest = state_path.parent / ".morning-session-hot-v1" / "manifest.json"
    if os.path.lexists(hot_manifest):
        try:
            import morning_session_hot_state_408 as session_hot

            loaded = session_hot.load_state_bounded(state_path.parent)
        except (ImportError, OSError, RuntimeError) as exc:
            raise PreparedPackError(
                f"managed morning session state failed closed: {exc}"
            ) from exc
        if loaded.get("status") != "ready" or not isinstance(
            loaded.get("state"), dict
        ):
            reason = str(loaded.get("reason_code") or "hot_state_not_ready")
            raise PreparedPackError(
                f"managed morning session state failed closed: {reason}"
            )
        state = loaded["state"]
    else:
        state = _read_json(state_path, "morning session state", max_bytes=128 * 1024)
    if state.get("session_id") != session_id or state.get("interaction_mode") != INTERACTION_MODE:
        raise PreparedPackError("session is not bound to a frozen prepared pack")
    manifest, _, digest = _load_manifest(
        repo,
        str(state.get("delivery_pack_ref") or ""),
        str(state.get("delivery_pack_sha256") or ""),
    )
    if digest != state.get("delivery_pack_sha256") or manifest.get("queue_sha256") != state.get("queue_sha256"):
        raise PreparedPackError("session and prepared pack binding drifted")
    return state, manifest


def _scheduling_binding(
    state: dict[str, Any], item: dict[str, Any]
) -> dict[str, Any] | None:
    action_id = item.get("action_id")
    if action_id is None:
        return None
    core = {
        "schema": SCHEDULING_BINDING_SCHEMA,
        "session_id": state.get("session_id"),
        "item_id": item.get("item_id"),
        "source_id": item.get("source_id"),
        "review_unit_id": item.get("review_unit_id"),
        "formal_node_id": item.get("formal_node_id"),
        "action_id": action_id,
        "stage": item.get("stage"),
        "trigger_event_id": item.get("trigger_event_id"),
        "queue_sha256": state.get("queue_sha256"),
        "prepared_pack_sha256": state.get("delivery_pack_sha256"),
        "surface_sha256": item.get("surface_sha256"),
        "answer_safe": True,
        "formal_write_count": 0,
    }
    for field in (
        "session_id",
        "item_id",
        "source_id",
        "action_id",
        "stage",
        "trigger_event_id",
    ):
        if not isinstance(core.get(field), str) or not core[field]:
            raise PreparedPackError(f"morning schedule binding lacks {field}")
    for field in ("queue_sha256", "prepared_pack_sha256", "surface_sha256"):
        if not SHA_RE.fullmatch(str(core.get(field) or "")):
            raise PreparedPackError(f"morning schedule binding has invalid {field}")
    return {**core, "binding_sha256": _digest(core)}


def scheduling_binding_for_item(
    repo: str | Path, session_id: str, item_id: str
) -> dict[str, Any] | None:
    root = Path(repo).expanduser().resolve()
    state, _ = _session_pack_binding(root, session_id)
    item = (state.get("items") or {}).get(item_id)
    if not isinstance(item, dict):
        raise PreparedPackError("morning schedule binding item is missing")
    return _scheduling_binding(state, item)


def _object_for_item(
    repo: Path,
    row: dict[str, Any],
    kind: str,
    *,
    surface_role: str = "main",
) -> dict[str, Any]:
    if surface_role not in {"main", "guided", "repair"}:
        raise PreparedPackError("prepared surface role is invalid")
    if kind == "surface":
        field = (
            "surface_sha256"
            if surface_role == "main"
            else f"{surface_role}_surface_sha256"
        )
        digest = str(row.get(field) or "")
        path = repo / PACK_ROOT_REL / "objects" / "surfaces" / f"{digest}.json"
        schema = SURFACE_SCHEMA
    else:
        field = (
            "grading_sha256"
            if surface_role == "main"
            else f"{surface_role}_grading_sha256"
        )
        digest = str(row.get(field) or "")
        path = repo / PACK_ROOT_REL / "private" / "evaluators" / f"{digest}.json"
        schema = EVALUATOR_SCHEMA
    if not SHA_RE.fullmatch(digest):
        raise PreparedPackError(f"prepared {kind} commitment is invalid")
    _reject_symlink_components(
        path,
        (repo / PACK_ROOT_REL).resolve(),
        f"prepared {kind}",
    )
    value = _read_json(path, f"prepared {kind}", max_bytes=MAX_OBJECT_BYTES)
    if kind == "evaluator":
        info = path.stat()
        directory_info = path.parent.stat()
        if (
            info.st_uid != os.geteuid()
            or directory_info.st_uid != os.geteuid()
            or info.st_mode & 0o077
            or directory_info.st_mode & 0o077
        ):
            raise PreparedPackError("prepared evaluator permissions or ownership are unsafe")
    if (
        _sha256_file(path) != digest
        or value.get("schema") != schema
        or value.get("item_id") != row.get("item_id")
        or value.get("surface_role") != surface_role
    ):
        raise PreparedPackError(f"prepared {kind} object drifted")
    if kind == "surface" and value.get("answer_safe") is not True:
        raise PreparedPackError("prepared public surface is not answer-safe")
    return value


def show_item(
    repo: Path,
    session_id: str,
    item_id: str,
    surface_role: str = "main",
) -> dict[str, Any]:
    repo = Path(repo).resolve()
    state, manifest = _session_pack_binding(repo, session_id)
    row = _manifest_item(manifest, item_id)
    bound_item = (state.get("items") or {}).get(item_id)
    binding_field = (
        "surface_sha256"
        if surface_role == "main"
        else f"{surface_role}_surface_sha256"
    )
    if not isinstance(bound_item, dict) or bound_item.get(binding_field) != row.get(binding_field):
        raise PreparedPackError("session item surface binding drifted")
    surface = _object_for_item(
        repo, row, "surface", surface_role=surface_role
    )
    return {
        "status": "ready",
        "session_id": session_id,
        "item_id": item_id,
        "surface_role": surface_role,
        "pack_id": manifest["pack_id"],
        "surface_sha256": row[binding_field],
        "surface": surface,
        "answer_safe": True,
        "learner_evidence_write_count": 0,
        "formal_write_count": 0,
    }


def open_grading_context(
    repo: Path,
    session_id: str,
    item_id: str,
    choice: str | None = None,
    surface_role: str = "main",
) -> dict[str, Any]:
    repo = Path(repo).resolve()
    state, manifest = _session_pack_binding(repo, session_id)
    row = _manifest_item(manifest, item_id)
    bound_item = (state.get("items") or {}).get(item_id)
    binding_field = (
        "grading_sha256"
        if surface_role == "main"
        else f"{surface_role}_grading_sha256"
    )
    if not isinstance(bound_item, dict) or bound_item.get(binding_field) != row.get(binding_field):
        raise PreparedPackError("session item grading binding drifted")
    evaluator = _object_for_item(
        repo, row, "evaluator", surface_role=surface_role
    )
    normalized_choice = choice.upper() if isinstance(choice, str) else None
    if normalized_choice is not None and normalized_choice not in OPTIONS:
        raise PreparedPackError("choice must be A, B, C, or D")
    choice_result = None
    first_break = None
    if normalized_choice is not None:
        choice_result = "correct" if normalized_choice == evaluator["correct_option"] else "incorrect"
        first_break = evaluator["first_break_by_choice"][normalized_choice]
    return {
        "status": "ready",
        "private_only": True,
        "session_id": session_id,
        "item_id": item_id,
        "surface_role": surface_role,
        "pack_id": manifest["pack_id"],
        "source_id": row["source_id"],
        "grading_sha256": row[binding_field],
        "evaluator": evaluator,
        "deterministic_choice_result": choice_result,
        "choice_first_break_candidate": first_break,
        "reasoning_result": "not_observed",
        "reasoning_requires_current_response_judgment": True,
        "learner_evidence_write_count": 0,
        "formal_write_count": 0,
    }


def _render_surface(surface: dict[str, Any]) -> str:
    lines = [str(surface["stem"]).strip(), ""]
    lines.extend(
        f"{label}. {str(surface['options'][label]).strip()}"
        for label in OPTIONS
    )
    lines.extend(["", str(surface["response_instruction"]).strip()])
    return "\n".join(lines).strip() + "\n"


def _feedback_for_result(evaluator: dict[str, Any], result: str) -> str:
    by_result = evaluator["feedback_by_result"]
    key = "blank" if result == "uncertain" and "uncertain" not in by_result else result
    return str(by_result[key])


def prepare_current_turn(
    repo: str | Path,
    *,
    session_id: str,
    item_id: str,
    choice: str,
    confidence: str,
    prompt_level: str = "none",
    request_id: str,
    event_time: str,
    display_surface_sha256: str,
    semantic_assessment: object,
    interaction_trace: object,
    display_receipt_locator: str | None = None,
    include_private_feedback: bool = False,
    private_root: str | Path | None = None,
) -> dict[str, Any]:
    """Prepare one current-only morning answer without exposing grader content."""

    root = Path(repo).expanduser().resolve()
    state, manifest = _session_pack_binding(root, session_id)
    pending_item_id = next(
        (
            candidate
            for candidate in state["item_order"]
            if state["items"][candidate].get("first") is None
            and state["items"][candidate].get("resolution")
            not in {"dismissed", "scheduling_superseded"}
        ),
        None,
    )
    if pending_item_id != item_id:
        raise PreparedPackError("requested item is not the current pending item")
    normalized_choice = str(choice or "").strip().upper()
    if normalized_choice not in MANAGED_RESPONSES:
        raise PreparedPackError("choice must be A-D, BLANK, or UNCERTAIN")
    normalized_confidence = str(confidence or "").strip().lower()
    if normalized_confidence not in {"high", "medium", "low"}:
        raise PreparedPackError("confidence must be high, medium, or low")
    normalized_prompt_level = str(prompt_level or "").strip()
    if normalized_prompt_level not in current_context.PROMPT_LEVELS:
        raise PreparedPackError("prompt_level must be none or L1-L5")
    try:
        normalized_trace = current_context.normalize_interaction_trace(
            interaction_trace
        )
    except current_context.CurrentQuestionContextError as exc:
        raise PreparedPackError(str(exc)) from exc

    shown = show_item(root, session_id, item_id)
    if shown["surface_sha256"] != display_surface_sha256:
        raise PreparedPackError("displayed surface commitment drifted")
    grading = open_grading_context(
        root,
        session_id,
        item_id,
        None if normalized_choice in {"BLANK", "UNCERTAIN"} else normalized_choice,
    )
    row = _manifest_item(manifest, item_id)
    bound_item = (state.get("items") or {}).get(item_id)
    if not isinstance(bound_item, dict):
        raise PreparedPackError("current session item is missing")
    scheduling_binding = _scheduling_binding(state, bound_item)
    surface = shown["surface"]
    question_asset = resolve_question_asset(surface)
    display_text = _render_surface(surface)
    if display_receipt_locator is None:
        display_receipt = current_evidence.publish_metadata_object(
            {
                "schema": current_evidence.TURN_RECEIPT_SCHEMA,
                "status": "display_published",
                "context_id": None,
                "request_id": request_id,
                "session_id": session_id,
                "item_id": item_id,
                "feedback_sha256": None,
                "first_result": None,
                "first_answer_receipt_sha256": None,
                "capture_id": None,
                "capture_receipt_sha256": None,
                "capture_status": "not_started",
                "recovery_locator": None,
                "advance_allowed": False,
                "surface_sha256": shown["surface_sha256"],
                "display_sha256": hashlib.sha256(
                    display_text.encode("utf-8")
                ).hexdigest(),
                "next_display_receipt_sha256": None,
                "question_asset_ref": (
                    question_asset["relative_path"] if question_asset is not None else None
                ),
                "question_asset_sha256": (
                    question_asset["sha256"] if question_asset is not None else None
                ),
                "scheduling_binding": scheduling_binding,
                "formal_write_count": 0,
            },
            kind="turns",
            private_root=private_root,
        )
    else:
        existing_display = current_evidence.read_metadata_object(
            display_receipt_locator,
            kind="turns",
            private_root=private_root,
        )
        expected_display_sha = hashlib.sha256(
            display_text.encode("utf-8")
        ).hexdigest()
        if (
            existing_display.get("status") != "display_published"
            or existing_display.get("session_id") != session_id
            or existing_display.get("item_id") != item_id
            or existing_display.get("surface_sha256") != shown["surface_sha256"]
            or existing_display.get("display_sha256") != expected_display_sha
            or existing_display.get("capture_status") != "not_started"
            or existing_display.get("advance_allowed") is not False
            or existing_display.get("question_asset_sha256")
            != (question_asset["sha256"] if question_asset is not None else None)
        ):
            raise PreparedPackError("current display receipt binding drifted")
        existing_schedule = existing_display.get("scheduling_binding")
        if existing_schedule is not None and existing_schedule != scheduling_binding:
            raise PreparedPackError("current display schedule binding drifted")
        display_digest = display_receipt_locator.removeprefix(
            current_evidence.TURN_LOCATOR_PREFIX
        )
        if not SHA_RE.fullmatch(display_digest):
            raise PreparedPackError("current display receipt locator is invalid")
        display_receipt = {
            "locator": display_receipt_locator,
            "sha256": display_digest,
        }
    try:
        local_date = (
            datetime.fromisoformat(event_time)
            .astimezone(ZoneInfo("Asia/Shanghai"))
            .date()
            .isoformat()
        )
    except ValueError as exc:
        raise PreparedPackError("event_time is invalid") from exc
    grader_capsule_id = (
        "MRGC-"
        + hashlib.sha256(
            (
                f"{session_id}\0{item_id}\0main\0"
                f"{grading['grading_sha256']}"
            ).encode("utf-8")
        ).hexdigest()[:24].upper()
    )
    choice_result = (
        "blank"
        if normalized_choice == "BLANK"
        else "uncertain"
        if normalized_choice == "UNCERTAIN"
        else str(grading["deterministic_choice_result"])
    )
    try:
        controlled = current_context.controlled_assessment_from_semantic(
            choice_result=choice_result,
            confidence=normalized_confidence,
            prompt_level=normalized_prompt_level,
            semantic_assessment=semantic_assessment,
            interaction_trace=normalized_trace,
        )
    except current_context.CurrentQuestionContextError as exc:
        raise PreparedPackError(str(exc)) from exc
    assessment = controlled["assessment"]
    raw_context = {
        "schema": current_context.SCHEMA,
        "source": "morning_review",
        "request_id": request_id,
        "session_id": session_id,
        "item_id": item_id,
        "source_id": row["source_id"],
        "source_stable": True,
        "source_binding_sha256": shown["surface_sha256"],
        "grader_capsule_id": grader_capsule_id,
        "event_time": event_time,
        "study_date": local_date,
        "timezone": "Asia/Shanghai",
        "idempotency_key": f"morning-current:{session_id}:{item_id}:first",
        "display_receipt_sha256": display_receipt["sha256"],
        "question_valid": True,
        "question_evidence": {
            "public_text": surface["stem"],
            "options": [
                {"label": label, "text": surface["options"][label]}
                for label in OPTIONS
            ],
            "response_instruction": surface["response_instruction"],
            "public_surface_sha256": shown["surface_sha256"],
            "attachment_sha256s": (
                [question_asset["sha256"]] if question_asset is not None else []
            ),
        },
        "learner_evidence": {
            "answer_text": (
                "" if normalized_choice in {"BLANK", "UNCERTAIN"} else normalized_choice
            ),
            "choice": normalized_choice,
            "confidence": normalized_confidence,
            "first_action": assessment.get("first_action"),
            "reasoning": None,
            "prompt_level": normalized_prompt_level,
            "observed_at": event_time,
        },
        "assessment": assessment,
        "formal_write_count": 0,
    }
    normalized_context = current_context.validate_context(raw_context)
    evaluator = grading["evaluator"]
    feedback_key = normalized_context["classification"]["teaching_result"]
    frozen_feedback_text = (
        controlled["feedback_text"]
        or _feedback_for_result(evaluator, feedback_key)
    )
    try:
        verdict_binding = current_context.freeze_terminal_verdict(
            controlled,
            feedback_text=frozen_feedback_text,
        )
    except current_context.CurrentQuestionContextError as exc:
        raise PreparedPackError(str(exc)) from exc
    capsule = current_evidence.publish_evaluation_capsule(
        {
            "schema_version": current_evidence.EVALUATION_CAPSULE_SCHEMA,
            "capsule_id": grader_capsule_id,
            "session_id": session_id,
            "item_id": item_id,
            "source_id": row["source_id"],
            "source_stable": True,
            "source_binding_sha256": shown["surface_sha256"],
            "context_id": normalized_context["context_id"],
            "evaluation_evidence": {
                "grader_capsule_id": grader_capsule_id,
                "correct_answer": None,
                "correct_option": evaluator["correct_option"],
                "standard_explanation": evaluator["decisive_reason"],
                "grader_result": choice_result,
                "grader_basis": evaluator["decisive_reason"],
                "provided_by": "morning_review_prepared_pack_v1",
                "missing_fields": [],
            },
            "formal_write_count": 0,
        },
        private_root=private_root,
    )
    result = {
        "schema": CURRENT_TURN_PREPARATION_SCHEMA,
        "status": "ready",
        "request_id": request_id,
        "session_id": session_id,
        "item_id": item_id,
        "context_id": normalized_context["context_id"],
        "context": raw_context,
        "controlled_assessment": {
            **assessment,
            "teaching_result": normalized_context["classification"][
                "teaching_result"
            ],
            "canonical_first_result": normalized_context["classification"][
                "canonical_first_result"
            ],
            "terminal_resolved": normalized_context["classification"][
                "terminal_resolved"
            ],
            "capture_required": normalized_context["classification"][
                "capture_required"
            ],
        },
        **verdict_binding,
        "grader_capsule_locator": capsule["locator"],
        "grader_capsule_sha256": capsule["sha256"],
        "display_receipt_locator": display_receipt["locator"],
        "display_receipt_sha256": display_receipt["sha256"],
        "scheduling_binding": scheduling_binding,
        "scheduling_binding_status": (
            "verified_display_binding"
            if display_receipt_locator is None
            or existing_display.get("scheduling_binding") is not None
            else "legacy_session_pack_compatibility"
        ),
        "next_item_published": False,
        "learner_evidence_write_count": 0,
        "formal_write_count": 0,
    }
    if include_private_feedback:
        result["_private_frozen_feedback_text"] = frozen_feedback_text
        result["_private_interaction_trace"] = normalized_trace
    return result


def prepare_followup_attempt_from_display(
    repo: str | Path,
    *,
    display_receipt_locator: str,
    choice: str,
    confidence: str,
    prompt_level: str,
    request_id: str,
    event_time: str,
    semantic_assessment: object,
    interaction_trace: object,
    private_root: str | Path | None = None,
) -> dict[str, Any]:
    """Grade a same-item teaching attempt without writing another first answer."""

    root = Path(repo).expanduser().resolve()
    receipt = current_evidence.read_metadata_object(
        display_receipt_locator, kind="turns", private_root=private_root
    )
    if receipt.get("status") != "display_published":
        raise PreparedPackError("followup display receipt is not a question surface")
    session_id = str(receipt.get("session_id") or "")
    item_id = str(receipt.get("item_id") or "")
    state, manifest = _session_pack_binding(root, session_id)
    item = state.get("items", {}).get(item_id)
    pending_item_id = next(
        (
            candidate
            for candidate in state.get("item_order") or []
            if state.get("items", {}).get(candidate, {}).get("first") is None
            and state.get("items", {}).get(candidate, {}).get("resolution")
            not in {"dismissed", "scheduling_superseded"}
        ),
        None,
    )
    if not isinstance(item, dict) or pending_item_id != item_id:
        raise PreparedPackError(
            "followup item is not the current unfinished display frontier"
        )
    normalized_choice = str(choice or "").strip().upper()
    normalized_confidence = str(confidence or "").strip().lower()
    normalized_prompt = str(prompt_level or "").strip()
    if normalized_choice not in MANAGED_RESPONSES:
        raise PreparedPackError("choice must be A-D, BLANK, or UNCERTAIN")
    if normalized_confidence not in {"high", "medium", "low"}:
        raise PreparedPackError("confidence must be high, medium, or low")
    if normalized_prompt not in current_context.PROMPT_LEVELS:
        raise PreparedPackError("prompt_level must be none or L1-L5")
    shown = show_item(root, session_id, item_id)
    question_asset = resolve_question_asset(shown["surface"])
    display_text = _render_surface(shown["surface"])
    if (
        receipt.get("surface_sha256") != shown["surface_sha256"]
        or receipt.get("display_sha256")
        != hashlib.sha256(display_text.encode("utf-8")).hexdigest()
        or receipt.get("question_asset_sha256")
        != (question_asset["sha256"] if question_asset is not None else None)
    ):
        raise PreparedPackError("followup display receipt binding drifted")
    grading = open_grading_context(
        root,
        session_id,
        item_id,
        None if normalized_choice in {"BLANK", "UNCERTAIN"} else normalized_choice,
    )
    choice_result = (
        "blank"
        if normalized_choice == "BLANK"
        else "uncertain"
        if normalized_choice == "UNCERTAIN"
        else str(grading["deterministic_choice_result"])
    )
    try:
        controlled = current_context.controlled_assessment_from_semantic(
            choice_result=choice_result,
            confidence=normalized_confidence,
            prompt_level=normalized_prompt,
            semantic_assessment=semantic_assessment,
            interaction_trace=interaction_trace,
        )
    except current_context.CurrentQuestionContextError as exc:
        raise PreparedPackError(str(exc)) from exc
    assessment = controlled["assessment"]
    classification = controlled["classification"]
    feedback_key = classification["teaching_result"]
    row = _manifest_item(manifest, item_id)
    feedback_text = (
        controlled["feedback_text"]
        or _feedback_for_result(grading["evaluator"], feedback_key)
    )
    try:
        verdict_binding = current_context.freeze_terminal_verdict(
            controlled,
            feedback_text=feedback_text,
        )
    except current_context.CurrentQuestionContextError as exc:
        raise PreparedPackError(str(exc)) from exc
    display_digest = display_receipt_locator.removeprefix(
        current_evidence.TURN_LOCATOR_PREFIX
    )
    if not SHA_RE.fullmatch(display_digest):
        raise PreparedPackError("followup display receipt locator is invalid")
    try:
        local_date = (
            datetime.fromisoformat(event_time)
            .astimezone(ZoneInfo("Asia/Shanghai"))
            .date()
            .isoformat()
        )
    except ValueError as exc:
        raise PreparedPackError("event_time is invalid") from exc
    surface = shown["surface"]
    grader_capsule_id = (
        "MRGC-"
        + hashlib.sha256(
            (
                f"{session_id}\0{item_id}\0followup\0{request_id}\0"
                f"{grading['grading_sha256']}"
            ).encode("utf-8")
        ).hexdigest()[:24].upper()
    )
    raw_context = {
        "schema": current_context.SCHEMA,
        "source": "morning_review",
        "request_id": request_id,
        "session_id": session_id,
        "item_id": item_id,
        "source_id": row["source_id"],
        "source_stable": True,
        "source_binding_sha256": shown["surface_sha256"],
        "grader_capsule_id": grader_capsule_id,
        "event_time": event_time,
        "study_date": local_date,
        "timezone": "Asia/Shanghai",
        "idempotency_key": (
            f"morning-current:{session_id}:{item_id}:followup:{request_id}"
        ),
        "display_receipt_sha256": display_digest,
        "question_valid": True,
        "question_evidence": {
            "public_text": surface["stem"],
            "options": [
                {"label": label, "text": surface["options"][label]}
                for label in OPTIONS
            ],
            "response_instruction": surface["response_instruction"],
            "public_surface_sha256": shown["surface_sha256"],
            "attachment_sha256s": (
                [question_asset["sha256"]]
                if question_asset is not None
                else []
            ),
        },
        "learner_evidence": {
            "answer_text": (
                "" if normalized_choice in {"BLANK", "UNCERTAIN"} else normalized_choice
            ),
            "choice": normalized_choice,
            "confidence": normalized_confidence,
            "first_action": assessment.get("first_action"),
            "reasoning": None,
            "prompt_level": normalized_prompt,
            "observed_at": event_time,
        },
        "assessment": assessment,
        "formal_write_count": 0,
    }
    try:
        normalized_context = current_context.validate_context(raw_context)
    except current_context.CurrentQuestionContextError as exc:
        raise PreparedPackError(str(exc)) from exc
    evaluator = grading["evaluator"]
    capsule = current_evidence.publish_evaluation_capsule(
        {
            "schema_version": current_evidence.EVALUATION_CAPSULE_SCHEMA,
            "capsule_id": grader_capsule_id,
            "session_id": session_id,
            "item_id": item_id,
            "source_id": row["source_id"],
            "source_stable": True,
            "source_binding_sha256": shown["surface_sha256"],
            "context_id": normalized_context["context_id"],
            "evaluation_evidence": {
                "grader_capsule_id": grader_capsule_id,
                "correct_answer": None,
                "correct_option": evaluator["correct_option"],
                "standard_explanation": evaluator["decisive_reason"],
                "grader_result": choice_result,
                "grader_basis": evaluator["decisive_reason"],
                "provided_by": "morning_review_prepared_pack_v1",
                "missing_fields": [],
            },
            "formal_write_count": 0,
        },
        private_root=private_root,
    )
    controlled_assessment = {
        **assessment,
        "teaching_result": classification["teaching_result"],
        "canonical_first_result": classification["canonical_first_result"],
        "terminal_resolved": classification["terminal_resolved"],
        "capture_required": classification["capture_required"],
    }
    return {
        "schema": "morning-review-followup-attempt-v1",
        "status": "ready",
        "session_id": session_id,
        "item_id": item_id,
        "source_id": row["source_id"],
        "choice_result": choice_result,
        "first_result": feedback_key,
        "controlled_assessment": controlled_assessment,
        "context_id": normalized_context["context_id"],
        "context": raw_context,
        "grader_capsule_locator": capsule["locator"],
        "grader_capsule_sha256": capsule["sha256"],
        "feedback_text": feedback_text,
        "interaction_trace": controlled["interaction_trace"],
        "_private_interaction_trace": controlled["interaction_trace"],
        **verdict_binding,
        "evidence_manifest_sha256": (
            item["first"].get("managed_user_reply_binding_sha256")
            if isinstance(item.get("first"), dict)
            else None
        ),
        "formal_write_count": 0,
    }


def prepared_feedback_for_item(repo: str | Path, state: dict[str, Any], item: dict[str, Any]) -> tuple[str, str, dict[str, Any]]:
    root = Path(repo).resolve()
    if state.get("interaction_mode") != INTERACTION_MODE:
        raise PreparedPackError("session is not in frozen prepared-pack mode")
    manifest, _, digest = _load_manifest(
        root,
        str(state.get("delivery_pack_ref") or ""),
        str(state.get("delivery_pack_sha256") or ""),
    )
    row = _manifest_item(manifest, str(item.get("item_id") or ""))
    for field in (
        "grading_sha256",
        "guided_surface_sha256",
        "guided_grading_sha256",
        "repair_surface_sha256",
        "repair_grading_sha256",
    ):
        if row.get(field) != item.get(field):
            raise PreparedPackError(
                "prepared grading or branch commitment differs from session item"
            )
    evaluator = _object_for_item(root, row, "evaluator")
    guided_surface = _object_for_item(
        root, row, "surface", surface_role="guided"
    )
    repair_surface = _object_for_item(
        root, row, "surface", surface_role="repair"
    )
    first = item.get("first") or {}
    result = str(first.get("result") or "")
    if result not in RESULTS:
        raise PreparedPackError("prepared feedback requires one controlled first result")
    feedback = evaluator["feedback_by_result"][result]
    return (
        feedback,
        f"prepared_pack:{digest}:{item['item_id']}:{row['grading_sha256']}",
        {
            "status": "prepared_pack_verified",
            "pack_id": manifest["pack_id"],
            "manifest_sha256": digest,
            "grading_sha256": row["grading_sha256"],
            "guided_surface_sha256": row["guided_surface_sha256"],
            "guided_surface": guided_surface,
            "repair_surface_sha256": row["repair_surface_sha256"],
            "repair_surface": repair_surface,
            "formal_write_count": 0,
        },
    )


def audit_pack(repo: Path, manifest_ref: str | Path, *, verify_sources: bool = True) -> dict[str, Any]:
    repo = Path(repo).resolve()
    manifest, _, digest = _load_manifest(
        repo,
        manifest_ref,
        verify_pack_commitment=False,
    )
    errors: list[str] = []
    seen: set[str] = set()
    try:
        _validate_pack_commitment(manifest)
    except PreparedPackError as exc:
        errors.append(str(exc))
    try:
        _load_manifest_verification(repo, manifest)
    except PreparedPackError as exc:
        errors.append(str(exc))
    main_surfaces: list[dict[str, Any]] = []
    for row in manifest.get("items", []):
        item_id = str(row.get("item_id") or "")
        if item_id in seen:
            errors.append(f"duplicate item: {item_id}")
            continue
        seen.add(item_id)
        try:
            _manifest_item_source_contract(row, item_id)
            for surface_role in ("main", "guided", "repair"):
                surface_object = _object_for_item(
                    repo, row, "surface", surface_role=surface_role
                )
                resolve_question_asset(surface_object)
                if surface_role == "main":
                    main_surfaces.append(surface_object)
                _object_for_item(
                    repo, row, "evaluator", surface_role=surface_role
                )
            if verify_sources:
                for evidence in row.get("evidence", []):
                    path = _repo_file(repo, evidence.get("source_ref"), f"{item_id}.evidence")
                    if _sha256_file(path) != evidence.get("sha256"):
                        raise PreparedPackError(f"{item_id} source evidence drifted")
        except PreparedPackError as exc:
            errors.append(str(exc))
    try:
        question_exposure.assert_no_recent_surface_reuse(
            repo,
            date.fromisoformat(str(manifest.get("review_date") or "")),
            main_surfaces,
            exclude_prepared_pack_sha256=digest,
        )
    except (ValueError, question_exposure.QuestionExposureError) as exc:
        errors.append(f"learner-visible question reuse gate failed: {exc}")
    expected_order = [row.get("item_id") for row in manifest.get("items", [])]
    if manifest.get("item_order") != expected_order:
        errors.append("manifest item order drifted")
    return {
        "status": "PASS" if not errors else "FAIL",
        "pack_id": manifest.get("pack_id"),
        "manifest_sha256": digest,
        "binding_status": "pack_bound",
        "generation_id": manifest.get("generation_id"),
        "eligible_due_action_set_sha256": manifest.get(
            "eligible_due_action_set_sha256"
        ),
        "pack_commitment_sha256": manifest.get("pack_commitment_sha256"),
        "item_count": len(seen),
        "errors": errors,
        "answer_isolated": True,
        "learner_evidence_write_count": 0,
        "formal_write_count": 0,
    }


def benchmark_item(repo: Path, session_id: str, item_id: str, iterations: int) -> dict[str, Any]:
    if iterations < 20 or iterations > 5000:
        raise PreparedPackError("benchmark iterations must be between 20 and 5000")
    samples: list[float] = []
    repo = Path(repo).resolve()
    for _ in range(iterations):
        started = time.perf_counter()
        open_grading_context(repo, session_id, item_id)
        samples.append((time.perf_counter() - started) * 1000)
    ordered = sorted(samples)
    p95 = ordered[max(0, (95 * len(ordered) + 99) // 100 - 1)]
    return {
        "status": "PASS" if p95 < 100.0 else "FAIL",
        "session_id": session_id,
        "item_id": item_id,
        "iterations": iterations,
        "p50_ms": ordered[len(ordered) // 2],
        "p95_ms": p95,
        "max_ms": max(ordered),
        "threshold_ms": 100.0,
        "learner_evidence_write_count": 0,
        "formal_write_count": 0,
    }


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo", type=Path, default=Path.cwd())
    sub = parser.add_subparsers(dest="command", required=True)

    hashes = sub.add_parser("draft-hashes")
    hashes.add_argument("--queue", required=True)
    hashes.add_argument("--draft", required=True)

    publish = sub.add_parser("publish")
    publish.add_argument("--queue", required=True)
    publish.add_argument("--draft", required=True)
    publish.add_argument("--verification", required=True)

    show = sub.add_parser("show-item")
    show.add_argument("--session", required=True)
    show.add_argument("--item", required=True)
    show.add_argument(
        "--surface-role", choices=("main", "guided", "repair"), default="main"
    )

    current = sub.add_parser("prepare-current-turn")
    current.add_argument("--session", required=True)
    current.add_argument("--item", required=True)
    current.add_argument("--choice", choices=OPTIONS, required=True)
    current.add_argument(
        "--confidence", choices=("high", "medium", "low"), required=True
    )
    current.add_argument(
        "--prompt-level", choices=sorted(current_context.PROMPT_LEVELS), default="none"
    )
    current.add_argument("--request-id", required=True)
    current.add_argument("--event-time", required=True)
    current.add_argument("--display-surface-sha256", required=True)
    current.add_argument("--semantic-assessment-json", required=True)
    current.add_argument("--interaction-trace-json", required=True)
    current.add_argument("--private-root", type=Path)

    audit = sub.add_parser("audit")
    audit.add_argument("--manifest", required=True)
    audit.add_argument("--skip-source-drift", action="store_true")

    benchmark = sub.add_parser("benchmark-item")
    benchmark.add_argument("--session", required=True)
    benchmark.add_argument("--item", required=True)
    benchmark.add_argument("--iterations", type=int, default=100)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    repo = args.repo.expanduser().resolve()
    try:
        if args.command in {"draft-hashes", "publish"}:
            queue = _repo_file(repo, args.queue, "queue")
            draft = _resolve_input(args.draft, "draft")
            if args.command == "draft-hashes":
                result = draft_hashes(repo, queue, draft)
            else:
                result = publish_pack(
                    repo,
                    queue,
                    draft,
                    _resolve_input(args.verification, "verification"),
                )
        elif args.command == "show-item":
            result = show_item(
                repo, args.session, args.item, surface_role=args.surface_role
            )
        elif args.command == "prepare-current-turn":
            result = prepare_current_turn(
                repo,
                session_id=args.session,
                item_id=args.item,
                choice=args.choice,
                confidence=args.confidence,
                prompt_level=args.prompt_level,
                request_id=args.request_id,
                event_time=args.event_time,
                display_surface_sha256=args.display_surface_sha256,
                semantic_assessment=json.loads(
                    args.semantic_assessment_json
                ),
                interaction_trace=json.loads(args.interaction_trace_json),
                private_root=args.private_root,
            )
        elif args.command == "audit":
            result = audit_pack(
                repo,
                args.manifest,
                verify_sources=not args.skip_source_drift,
            )
        else:
            result = benchmark_item(repo, args.session, args.item, args.iterations)
    except (OSError, PreparedPackError, UnicodeError, ValueError) as exc:
        print(json.dumps({"status": "ERROR", "error": str(exc)}, ensure_ascii=False), file=sys.stderr)
        return 2
    print(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
