#!/usr/bin/env python3
"""Build an executable, answer-safe 408 morning action queue.

The full review inventory and the work opened in one morning are deliberately
different objects.  Uncalibrated units stay visible in a persistent baseline
backlog, but they are not all mislabeled as scientifically due on the same day.
The default command is read-only.  ``--write-safe`` may only write the derived
queue and its pointer inside the StudyVault dashboard.
"""

from __future__ import annotations

import argparse
import dataclasses
import datetime as dt
import hashlib
import json
import re
import sys
from collections import Counter, defaultdict
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from generate_morning_review_queue import (
    SUBJECTS,
    DerivedReviewItem,
    OriginalQuestionTrack,
    ReviewUnit,
    due_original_tracks,
    load_d0_items,
    load_original_tracks,
    load_units,
    order_units,
    parse_date_strict,
    prompt_for,
    protect_location_text,
    protect_text,
)
from review_feedback_loop import (
    LoopAction,
    ReviewLoopError,
    load_due_actions,
    organic_baseline_units,
    scheduling_drift_audit,
)
import review_feedback_loop as review_loop


ROOT = Path(__file__).resolve().parents[1]
QUEUE_GENERATION_SCHEMA = "morning_review_queue_generation_v1"
QUEUE_GENERATION_POINTER_SCHEMA = "morning_review_queue_generation_pointer_v1"
QUEUE_GENERATION_POLICY_VERSION = "morning-review-generation-v1"
QUEUE_GENERATION_ROOT_REL = Path(
    "wiki/study_vaults/408-full/state/morning-review-queue-generations"
)
BASELINE_STATES = {
    "uncalibrated_baseline_due",
    "recalibration_required_after_rebuild",
}


@dataclass(frozen=True)
class ActionSelection:
    formal_due: tuple[ReviewUnit, ...]
    selected_formal: tuple[ReviewUnit, ...]
    baseline_pool: tuple[ReviewUnit, ...]
    selected_baseline: tuple[ReviewUnit, ...]
    original_due: tuple[OriginalQuestionTrack, ...]
    selected_original: tuple[OriginalQuestionTrack, ...]
    derived_pool: tuple[DerivedReviewItem, ...]
    selected_derived: tuple[DerivedReviewItem, ...]

    @property
    def scheduled_count(self) -> int:
        return (
            len(self.selected_formal)
            + len(self.selected_baseline)
            + len(self.selected_original)
            + len(self.selected_derived)
        )

    @property
    def same_day_extension_count(self) -> int:
        return (
            len(self.formal_due) - len(self.selected_formal)
            + len(self.original_due) - len(self.selected_original)
            + len(self.derived_pool) - len(self.selected_derived)
        )


def classify_units(
    units: list[ReviewUnit], today: dt.date
) -> tuple[list[ReviewUnit], list[ReviewUnit], list[ReviewUnit]]:
    """Return (formal_due, baseline_backlog, blocked).

    A rebuilt unit with no personal baseline is inventory, not an item whose
    memory has somehow become due today.  Once it has a real next date, normal
    date-based scheduling applies.
    """
    formal_due: list[ReviewUnit] = []
    baseline: list[ReviewUnit] = []
    blocked: list[ReviewUnit] = []
    for unit in units:
        if unit.schedule_state.startswith("blocked"):
            blocked.append(unit)
            continue
        next_date = parse_date_strict(unit.next_date, f"{unit.unit_id}.下次复习日期")
        if unit.schedule_state in BASELINE_STATES and next_date is None:
            baseline.append(unit)
            continue
        if unit.schedule_state in {"retired", "inactive"}:
            continue
        if next_date is None:
            # Undated active material is still an uncalibrated inventory item.
            # It must not silently become an ordinary D1/D3/D7 due item.
            baseline.append(unit)
            continue
        if next_date <= today:
            formal_due.append(unit)
    return formal_due, baseline, blocked


def balanced_baseline_sample(pool: list[ReviewUnit], quota: int) -> list[ReviewUnit]:
    """Select a fair cross-day sample while keeping each subject locally clustered.

    The same items remain at the front until their formal baseline is genuinely
    recorded; calendar passage alone never advances the cursor.
    """
    if quota <= 0 or not pool:
        return []
    groups: dict[str, list[ReviewUnit]] = defaultdict(list)
    for unit in pool:
        groups[unit.subject].append(unit)
    for subject in groups:
        groups[subject].sort(key=lambda item: (item.module_key, item.unit_id))

    selected: list[ReviewUnit] = []
    cursors = {subject: 0 for subject in SUBJECTS}
    while len(selected) < min(quota, len(pool)):
        progressed = False
        for subject in SUBJECTS:
            items = groups.get(subject, [])
            index = cursors[subject]
            if index >= len(items):
                continue
            selected.append(items[index])
            cursors[subject] += 1
            progressed = True
            if len(selected) >= quota:
                break
        if not progressed:
            break
    # Execution is clustered, not randomly interleaved across unrelated topics.
    subject_rank = {subject: index for index, subject in enumerate(SUBJECTS)}
    return sorted(
        selected,
        key=lambda item: (subject_rank.get(item.subject, 9), item.module_key, item.unit_id),
    )


REVIEW_UNIT_KEY_RE = re.compile(r"RU_[A-Za-z0-9_]+")


def derived_unit_key(item: DerivedReviewItem | LoopAction) -> str | None:
    """Stable mechanism-track dedupe key for one derived item."""

    unit = getattr(item, "review_unit_id", None)
    if unit:
        return str(unit)
    # Legacy safe-D0 sections only carry a free-form key; recover the review
    # unit from it when one is embedded, otherwise decline to dedupe.
    match = REVIEW_UNIT_KEY_RE.search(str(getattr(item, "key", "")))
    return match.group(0) if match else None


def derived_node_key(item: DerivedReviewItem | LoopAction) -> str | None:
    """Stable original-question-track dedupe key for one derived item."""

    for attribute in ("formal_node_id", "source_id"):
        value = getattr(item, attribute, None)
        if value:
            return str(value)
    return None


def select_actions(
    repo: Path,
    today: dt.date,
    *,
    max_items: int = 5,
    baseline_quota: int = 0,
    max_original: int = 2,
) -> ActionSelection:
    if max_items < 1:
        raise ValueError("max_items must be positive")
    if baseline_quota < 0 or max_original < 0:
        raise ValueError("quotas cannot be negative")

    formal_due, raw_baseline_pool, _ = classify_units(load_units(repo), today)
    naturally_baselined = organic_baseline_units(repo)
    baseline_pool = [
        item for item in raw_baseline_pool if item.unit_id not in naturally_baselined
    ]
    ordered_formal = order_units(formal_due, today)
    loop_actions = load_due_actions(repo, today)
    legacy_d0 = load_d0_items(repo, today)
    derived_by_key = {item.key: item for item in [*loop_actions, *legacy_d0]}
    derived_pool = list(derived_by_key.values())
    original_due = due_original_tracks(load_original_tracks(repo), today)

    # Rescue items are first, then the previous D0/D1 carry-in, then the rest of
    # the real dated due set.  Baseline inventory only fills remaining capacity.
    rescue = [item for item in ordered_formal if item.lane == "救援"]
    normal = [item for item in ordered_formal if item.lane != "救援"]
    remaining = max_items

    # One queue must never ask the same mechanism twice.  ``复盘调度规则`` §5 fixes
    # the stable dedupe keys: mechanism-track slots collapse on ``RU_*`` and
    # original-question slots collapse on the formal node id.  The two tracks stay
    # independent, so one unit may still appear once per track.
    claimed_units: set[str] = set()
    claimed_nodes: set[str] = set()

    selected_formal: list[ReviewUnit] = []
    for item in rescue:
        if len(selected_formal) >= remaining:
            break
        if item.unit_id in claimed_units:
            continue
        selected_formal.append(item)
        claimed_units.add(item.unit_id)
    remaining -= len(selected_formal)

    selected_derived: list[DerivedReviewItem | LoopAction] = []
    selected_loop_original = 0
    for item in derived_pool:
        if len(selected_derived) >= remaining:
            break
        is_loop_original = isinstance(item, LoopAction) and item.item_kind == "original_question"
        if is_loop_original:
            if selected_loop_original >= max_original:
                continue
            node = derived_node_key(item)
            if node and node in claimed_nodes:
                continue
            selected_derived.append(item)
            selected_loop_original += 1
            if node:
                claimed_nodes.add(node)
            continue
        unit = derived_unit_key(item)
        if unit and unit in claimed_units:
            continue
        selected_derived.append(item)
        if unit:
            claimed_units.add(unit)
    remaining -= len(selected_derived)

    if remaining:
        selected_normal: list[ReviewUnit] = []
        for item in normal:
            if len(selected_normal) >= remaining:
                break
            if item.unit_id in claimed_units:
                continue
            selected_normal.append(item)
            claimed_units.add(item.unit_id)
        selected_formal.extend(selected_normal)
        remaining -= len(selected_normal)

    remaining_original_quota = max(max_original - selected_loop_original, 0)
    original_capacity = min(remaining_original_quota, remaining)
    selected_original: list[OriginalQuestionTrack] = []
    for track in original_due:
        if len(selected_original) >= original_capacity:
            break
        if track.node_id in claimed_nodes:
            continue
        selected_original.append(track)
        claimed_nodes.add(track.node_id)
    remaining -= len(selected_original)

    selected_baseline = balanced_baseline_sample(
        baseline_pool,
        min(baseline_quota, remaining),
    )

    return ActionSelection(
        formal_due=tuple(ordered_formal),
        selected_formal=tuple(selected_formal),
        baseline_pool=tuple(baseline_pool),
        selected_baseline=tuple(selected_baseline),
        original_due=tuple(original_due),
        selected_original=tuple(selected_original),
        derived_pool=tuple(derived_pool),
        selected_derived=tuple(selected_derived),
    )


def compound_source_hash(repo: Path, today: dt.date) -> str:
    digest = hashlib.sha256()
    paths = [
        repo / "复习单元总表.md",
        repo / "原题复做轨总表.md",
        repo / "wiki" / "study_vaults" / "408-full" / "state" / "review-loop" / "state.json",
    ]
    input_dir = repo / "wiki" / "study_vaults" / "408-full" / "input"
    if input_dir.is_dir():
        paths.extend(sorted(input_dir.glob(f"{today.isoformat()}-*晨间*安全队列.md")))
    for path in paths:
        if not path.exists():
            continue
        digest.update(str(path.relative_to(repo)).encode("utf-8"))
        digest.update(b"\0")
        digest.update(path.read_bytes())
        digest.update(b"\0")
    return digest.hexdigest()[:16]


def _canonical_bytes(value: Any) -> bytes:
    return json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")


def _digest(value: Any) -> str:
    return hashlib.sha256(_canonical_bytes(value)).hexdigest()


def _file_sha256(path: Path) -> str | None:
    if not path.is_file() or path.is_symlink():
        return None
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _review_ledger_cutoff(repo: Path) -> dict[str, Any]:
    path = repo / review_loop.LOOP_REL / "events.jsonl"
    if not path.exists():
        return {
            "status": "absent",
            "sha256": None,
            "byte_size": 0,
            "event_line_count": 0,
        }
    if path.is_symlink() or not path.is_file():
        raise ReviewLoopError("review ledger is not a safe regular file")
    raw = path.read_bytes()
    return {
        "status": "bound",
        "sha256": hashlib.sha256(raw).hexdigest(),
        "byte_size": len(raw),
        "event_line_count": len(raw.splitlines()),
    }


def _action_set_rows(selection: ActionSelection) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for item in selection.derived_pool:
        if not isinstance(item, LoopAction):
            continue
        rows.append(
            {
                "action_id": item.action_id,
                "source": item.source,
                "source_id": item.source_id,
                "item_kind": item.item_kind,
                "trigger_event_id": item.trigger_event_id,
                "stage": item.stage,
                "due_date": item.due_date,
                "formal_node_id": item.formal_node_id,
                "review_unit_id": item.review_unit_id,
            }
        )
    return sorted(rows, key=lambda row: (str(row["due_date"]), str(row["action_id"])))


def _formal_due_rows(selection: ActionSelection) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = [
        {
            "kind": "review_unit",
            "source_id": item.unit_id,
            "schedule_state": item.schedule_state,
            "next_date": item.next_date,
            "interval": item.interval,
            "mastery": item.mastery,
        }
        for item in selection.formal_due
    ]
    rows.extend(
        {
            "kind": "original_question",
            "source_id": item.node_id,
            "state": item.state,
            "eligibility": item.eligibility,
            "next_date": item.next_date,
            "last_date": item.last_date,
        }
        for item in selection.original_due
    )
    return sorted(rows, key=lambda row: (str(row["kind"]), str(row["source_id"])))


def _selection_snapshot(selection: ActionSelection) -> dict[str, Any]:
    def rows(values: tuple[Any, ...]) -> list[dict[str, Any]]:
        return [
            {
                "type": type(item).__name__,
                "value": dataclasses.asdict(item),
            }
            for item in values
        ]

    return {
        "formal_due": rows(selection.formal_due),
        "selected_formal": rows(selection.selected_formal),
        "baseline_pool": rows(selection.baseline_pool),
        "selected_baseline": rows(selection.selected_baseline),
        "original_due": rows(selection.original_due),
        "selected_original": rows(selection.selected_original),
        "derived_pool": rows(selection.derived_pool),
        "selected_derived": rows(selection.selected_derived),
    }


def build_generation_snapshot(
    repo: Path,
    today: dt.date,
    *,
    max_items: int = 5,
    baseline_quota: int = 0,
    max_original: int = 2,
    target_minutes: int = 40,
    selection: ActionSelection | None = None,
) -> dict[str, Any]:
    """Freeze the inputs that define one immutable executable queue generation."""

    chosen = selection or select_actions(
        repo,
        today,
        max_items=max_items,
        baseline_quota=baseline_quota,
        max_original=max_original,
    )
    action_rows = _action_set_rows(chosen)
    formal_rows = _formal_due_rows(chosen)
    render_input_sha = _digest(
        {
            "selection": _selection_snapshot(chosen),
            "compound_source_hash": compound_source_hash(repo, today),
        }
    )
    ledger_cutoff = _review_ledger_cutoff(repo)
    renderer_sha = hashlib.sha256(Path(__file__).resolve().read_bytes()).hexdigest()
    schema_sha = _file_sha256(repo / "schema/morning-review-action-v3.md")
    inputs = {
        "schema": QUEUE_GENERATION_SCHEMA,
        "review_date": today.isoformat(),
        "scheduling_policy_version": QUEUE_GENERATION_POLICY_VERSION,
        "max_items": max_items,
        "baseline_quota": baseline_quota,
        "max_original": max_original,
        "target_minutes": target_minutes,
        "eligible_due_actions": action_rows,
        "eligible_due_action_set_sha256": _digest(action_rows),
        "formal_due_sources": formal_rows,
        "formal_due_source_sha256": _digest(formal_rows),
        "queue_render_input_sha256": render_input_sha,
        "review_ledger_cutoff": ledger_cutoff,
        "queue_renderer_sha256": renderer_sha,
        "queue_schema_sha256": schema_sha,
    }
    generation_input_sha = _digest(inputs)
    return {
        **inputs,
        "generation_input_sha256": generation_input_sha,
        "generation_id": f"MRG-{today.isoformat()}-{generation_input_sha}",
    }


def _parse_frontmatter(text: str) -> dict[str, str]:
    if not text.startswith("---\n"):
        return {}
    end = text.find("\n---\n", 4)
    if end < 0:
        return {}
    values: dict[str, str] = {}
    for line in text[4:end].splitlines():
        if ":" not in line:
            continue
        key, value = line.split(":", 1)
        values[key.strip()] = value.strip()
    return values


def queue_generation_metadata(path: Path) -> dict[str, Any]:
    values = _parse_frontmatter(path.read_text(encoding="utf-8"))
    generation_id = values.get("generation_id")
    if not generation_id:
        return {
            "status": "legacy_generation",
            "generation_id": None,
            "queue_sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
        }
    def optional(value: str | None) -> str | None:
        return None if value in {None, "none", "null"} else value

    review_date = values.get("review_date")
    generation_input_sha = values.get("generation_input_sha256")
    required_hashes = (
        generation_input_sha,
        values.get("eligible_due_action_set_sha256"),
        values.get("formal_due_source_sha256"),
        values.get("queue_render_input_sha256"),
        values.get("queue_renderer_sha256"),
    )
    if (
        any(value is None or not re.fullmatch(r"[0-9a-f]{64}", value) for value in required_hashes)
        or generation_id != f"MRG-{review_date}-{generation_input_sha}"
    ):
        raise ReviewLoopError("queue generation identity is invalid")
    schema_sha = optional(values.get("queue_schema_sha256"))
    cutoff_sha = optional(values.get("review_ledger_cutoff_sha256"))
    if any(
        value is not None and not re.fullmatch(r"[0-9a-f]{64}", value)
        for value in (schema_sha, cutoff_sha)
    ):
        raise ReviewLoopError("queue generation source hash is invalid")
    try:
        cutoff_bytes = int(values.get("review_ledger_cutoff_bytes", ""))
    except ValueError as exc:
        raise ReviewLoopError("queue generation ledger cutoff is invalid") from exc
    if cutoff_bytes < 0 or not re.fullmatch(
        r"[A-Za-z0-9][A-Za-z0-9._-]{1,159}",
        str(values.get("scheduling_policy_version") or ""),
    ):
        raise ReviewLoopError("queue generation policy/cutoff is invalid")

    return {
        "status": "generation_bound",
        "generation_id": generation_id,
        "generation_input_sha256": values.get("generation_input_sha256"),
        "eligible_due_action_set_sha256": values.get(
            "eligible_due_action_set_sha256"
        ),
        "formal_due_source_sha256": values.get("formal_due_source_sha256"),
        "queue_render_input_sha256": values.get("queue_render_input_sha256"),
        "scheduling_policy_version": values.get("scheduling_policy_version"),
        "review_ledger_cutoff_sha256": optional(
            values.get("review_ledger_cutoff_sha256")
        ),
        "review_ledger_cutoff_bytes": cutoff_bytes,
        "queue_renderer_sha256": values.get("queue_renderer_sha256"),
        "queue_schema_sha256": schema_sha,
        "queue_sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
    }


def validate_generation_freshness(repo: Path, queue_path: Path) -> dict[str, Any]:
    """Validate scheduling freshness without treating unrelated ledger suffix as drift."""

    frontmatter = _parse_frontmatter(queue_path.read_text(encoding="utf-8"))
    if frontmatter.get("selection_mode") == "semantic_sol_v1":
        import morning_semantic_selection_408 as semantic_selection

        return semantic_selection.validate_semantic_queue_freshness(
            repo.resolve(), queue_path.resolve()
        )
    metadata = queue_generation_metadata(queue_path)
    if metadata["status"] == "legacy_generation":
        return {
            **metadata,
            "status": "legacy_generation",
            "fresh": True,
            "reason_codes": [],
        }
    review_date = frontmatter.get("review_date")
    try:
        day = dt.date.fromisoformat(str(review_date))
    except ValueError as exc:
        raise ReviewLoopError("generation queue review_date is invalid") from exc
    current = build_generation_snapshot(
        repo,
        day,
        max_items=int(frontmatter.get("max_items", "5")),
        baseline_quota=int(frontmatter.get("baseline_quota", "0")),
        max_original=int(frontmatter.get("max_original", "2")),
        target_minutes=int(frontmatter.get("target_minutes", "40")),
    )
    comparisons = {
        "eligible_due_action_set_changed": "eligible_due_action_set_sha256",
        "formal_due_source_changed": "formal_due_source_sha256",
        "scheduling_policy_changed": "scheduling_policy_version",
        "queue_renderer_changed": "queue_renderer_sha256",
        "queue_schema_changed": "queue_schema_sha256",
    }
    reasons = [
        reason
        for reason, field in comparisons.items()
        if metadata.get(field) != current.get(field)
    ]
    return {
        **metadata,
        "status": "fresh" if not reasons else "stale_pre_session",
        "fresh": not reasons,
        "reason_codes": reasons,
        "current_generation_id": current["generation_id"],
        "current_generation_input_sha256": current["generation_input_sha256"],
        "current_eligible_due_action_set_sha256": current[
            "eligible_due_action_set_sha256"
        ],
        "current_formal_due_source_sha256": current["formal_due_source_sha256"],
        "current_review_ledger_cutoff": current["review_ledger_cutoff"],
    }


def _generation_root(repo: Path) -> Path:
    return repo / QUEUE_GENERATION_ROOT_REL


def _generation_pointer_path(repo: Path, review_date: str) -> Path:
    return _generation_root(repo) / "current" / f"{review_date}.json"


def load_current_generation_queue(repo: Path, review_date: str) -> Path | None:
    pointer_path = _generation_pointer_path(repo, review_date)
    if not pointer_path.is_file():
        return None
    if pointer_path.is_symlink():
        raise ReviewLoopError("current queue-generation pointer is unsafe")
    try:
        pointer = json.loads(pointer_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise ReviewLoopError("current queue-generation pointer is invalid") from exc
    if (
        not isinstance(pointer, dict)
        or pointer.get("schema") != QUEUE_GENERATION_POINTER_SCHEMA
        or pointer.get("review_date") != review_date
        or not isinstance(pointer.get("queue_ref"), str)
        or not isinstance(pointer.get("manifest_ref"), str)
    ):
        raise ReviewLoopError("current queue-generation pointer is invalid")
    queue_lexical = repo / str(pointer["queue_ref"])
    if queue_lexical.is_symlink():
        raise ReviewLoopError("current queue-generation queue is unsafe")
    queue = queue_lexical.resolve()
    try:
        queue.relative_to(repo.resolve())
    except ValueError as exc:
        raise ReviewLoopError("current queue-generation pointer escapes repo") from exc
    expected = str(pointer.get("queue_sha256") or "")
    if (
        not queue.is_file()
        or queue.is_symlink()
        or hashlib.sha256(queue.read_bytes()).hexdigest() != expected
    ):
        raise ReviewLoopError("current queue-generation pointer hash drifted")
    metadata = queue_generation_metadata(queue)
    if metadata.get("generation_id") != pointer.get("generation_id"):
        raise ReviewLoopError("current queue-generation identity drifted")
    manifest_lexical = repo / str(pointer["manifest_ref"])
    if manifest_lexical.is_symlink():
        raise ReviewLoopError("current queue-generation manifest is unsafe")
    manifest = manifest_lexical.resolve()
    try:
        manifest.relative_to(_generation_root(repo).resolve())
    except ValueError as exc:
        raise ReviewLoopError("current queue-generation manifest escapes root") from exc
    if (
        not manifest.is_file()
        or manifest.is_symlink()
        or hashlib.sha256(manifest.read_bytes()).hexdigest()
        != pointer.get("manifest_sha256")
    ):
        raise ReviewLoopError("current queue-generation manifest hash drifted")
    try:
        manifest_value = json.loads(manifest.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise ReviewLoopError("current queue-generation manifest is invalid") from exc
    if (
        not isinstance(manifest_value, dict)
        or manifest_value.get("schema") != QUEUE_GENERATION_SCHEMA
        or manifest_value.get("generation_id") != pointer.get("generation_id")
        or manifest_value.get("queue_ref") != pointer.get("queue_ref")
        or manifest_value.get("queue_sha256") != pointer.get("queue_sha256")
    ):
        raise ReviewLoopError("current queue-generation manifest binding drifted")
    return queue


def publish_generation_queue(
    repo: Path,
    today: dt.date,
    *,
    max_items: int = 5,
    baseline_quota: int = 0,
    max_original: int = 2,
    target_minutes: int = 40,
    activate_runtime: bool = True,
) -> dict[str, Any]:
    """Publish a new immutable generation and atomically move the date pointer."""

    repo = repo.resolve()
    with review_loop.runtime_lock(repo):
        if activate_runtime:
            review_loop._assert_queue_publication_allowed(repo)
            review_loop._generated_pointer(repo)
        selection = select_actions(
            repo,
            today,
            max_items=max_items,
            baseline_quota=baseline_quota,
            max_original=max_original,
        )
        snapshot = build_generation_snapshot(
            repo,
            today,
            max_items=max_items,
            baseline_quota=baseline_quota,
            max_original=max_original,
            target_minutes=target_minutes,
            selection=selection,
        )
        content = render_action_queue(
            repo,
            today,
            max_items=max_items,
            baseline_quota=baseline_quota,
            max_original=max_original,
            target_minutes=target_minutes,
            selection=selection,
            generation_snapshot=snapshot,
        ).rstrip() + "\n"
        generation_dir = (
            _generation_root(repo)
            / "generations"
            / today.isoformat()
            / str(snapshot["generation_id"])
        )
        queue = generation_dir / f"{snapshot['generation_id']}.md"
        queue_sha = hashlib.sha256(content.encode("utf-8")).hexdigest()
        queue_ref = queue.relative_to(repo).as_posix()
        manifest = {
            "schema": QUEUE_GENERATION_SCHEMA,
            "status": "queue_bound",
            **snapshot,
            "queue_ref": queue_ref,
            "queue_sha256": queue_sha,
            "formal_write_count": 0,
            "learner_evidence_write_count": 0,
        }
        manifest_path = generation_dir / "manifest.json"
        if queue.exists():
            if queue.read_text(encoding="utf-8") != content:
                raise ReviewLoopError("immutable generation queue hash collision")
        else:
            review_loop.atomic_write(queue, content)
        if manifest_path.exists():
            existing = json.loads(manifest_path.read_text(encoding="utf-8"))
            if existing != manifest:
                raise ReviewLoopError("immutable generation manifest hash collision")
        else:
            review_loop.atomic_write_json(manifest_path, manifest)
        pointer = {
            "schema": QUEUE_GENERATION_POINTER_SCHEMA,
            "review_date": today.isoformat(),
            "generation_id": snapshot["generation_id"],
            "generation_input_sha256": snapshot["generation_input_sha256"],
            "queue_ref": queue_ref,
            "queue_sha256": queue_sha,
            "manifest_ref": manifest_path.relative_to(repo).as_posix(),
            "manifest_sha256": hashlib.sha256(manifest_path.read_bytes()).hexdigest(),
        }
        current_pointer_path = _generation_pointer_path(repo, today.isoformat())
        old_pointer = (
            current_pointer_path.read_text(encoding="utf-8")
            if current_pointer_path.exists()
            else None
        )
        review_loop.atomic_write_json(current_pointer_path, pointer)
        # Preserve the original fixed-date path as a one-time legacy snapshot.
        # It is never overwritten when later generations replace the current one.
        legacy = repo / review_loop.DASHBOARD_REL / f"{today.isoformat()}-408晨间行动队列.md"
        if not legacy.exists():
            review_loop.atomic_write(legacy, content)
        try:
            runtime = (
                review_loop._write_runtime_views_locked(repo, queue)
                if activate_runtime
                else None
            )
        except BaseException:
            if old_pointer is None:
                current_pointer_path.unlink(missing_ok=True)
            else:
                review_loop.atomic_write(current_pointer_path, old_pointer)
            raise
    return {
        "status": "queue_bound",
        "generation_id": snapshot["generation_id"],
        "generation_input_sha256": snapshot["generation_input_sha256"],
        "queue_path": str(queue),
        "queue_ref": queue_ref,
        "queue_sha256": queue_sha,
        "eligible_due_action_set_sha256": snapshot[
            "eligible_due_action_set_sha256"
        ],
        "formal_due_source_sha256": snapshot["formal_due_source_sha256"],
        "runtime": runtime,
        "formal_write_count": 0,
        "learner_evidence_write_count": 0,
    }


def estimate_minutes(selection: ActionSelection) -> int:
    lane_cost = {"救援": 3, "校准": 2, "到期": 2, "维护": 1}
    return (
        sum(lane_cost.get(item.lane, 2) for item in selection.selected_formal)
        + 2 * len(selection.selected_baseline)
        + 2 * len(selection.selected_derived)
        + 6 * len(selection.selected_original)
    )


def stage_adapted_prompt(unit: ReviewUnit) -> str:
    """Vary the retrieval cue without inventing new question facts."""
    base = prompt_for(unit)
    if unit.interval <= 1 or unit.schedule_state in BASELINE_STATES:
        return base
    if unit.interval <= 3:
        return base + " 这次不要照卡片措辞复述：请从一个错误判断或反例倒推正确机制。"
    if unit.interval <= 7:
        return base + " 再补一个易混机制对比：给出各自的判定信号和不能互换的边界。"
    if unit.interval <= 14:
        return base + " 请自拟一个改变条件的近迁移情境，并说明原机制哪些部分保持不变。"
    return base + " 请先在混合情境中判型，再解释为什么其余相似机制不适用。"


def _section(
    item_id: str,
    title: str,
    source_id: str,
    item_kind: str,
    prompt: str,
    reason: str,
    *,
    action_id: str | None = None,
    trigger_event_id: str | None = None,
    stage: str | None = None,
    formal_node_id: str | None = None,
    review_unit_id: str | None = None,
) -> list[str]:
    lines = [
        f"#### {item_id} {protect_text(title, limit=180)}",
        "",
        f"- source_id: `{source_id}`",
        f"- item_kind: {item_kind}",
        f"- selection_reason: {protect_text(reason, limit=420)}",
        f"- prompt: {protect_text(prompt, limit=1200)}",
    ]
    if action_id:
        lines.append(f"- action_id: `{action_id}`")
    if trigger_event_id:
        lines.append(f"- trigger_event_id: `{trigger_event_id}`")
    if stage:
        lines.append(f"- review_stage: {stage}")
    if formal_node_id:
        lines.append(f"- formal_node_id: `{formal_node_id}`")
    if review_unit_id:
        lines.append(f"- review_unit_id: `{review_unit_id}`")
    lines += [
        "- first_attempt: 先无提示作答，再记录答后置信度与反应时；未记录首答前不得展开反馈。",
        "",
    ]
    return lines


def render_action_queue(
    repo: Path,
    today: dt.date,
    *,
    max_items: int = 5,
    baseline_quota: int = 0,
    max_original: int = 2,
    target_minutes: int = 40,
    selection: ActionSelection | None = None,
    generation_snapshot: dict[str, Any] | None = None,
) -> str:
    selection = selection or select_actions(
        repo,
        today,
        max_items=max_items,
        baseline_quota=baseline_quota,
        max_original=max_original,
    )
    generation = generation_snapshot or build_generation_snapshot(
        repo,
        today,
        max_items=max_items,
        baseline_quota=baseline_quota,
        max_original=max_original,
        target_minutes=target_minutes,
        selection=selection,
    )
    scheduling_report = scheduling_drift_audit(repo)
    scheduling_gap_count = sum(
        row.get("action_status") == "pending"
        and row.get("status") != "reconciled"
        for row in scheduling_report.get("gaps") or []
    )
    _, _, blocked_units = classify_units(load_units(repo), today)
    blocked_original = [
        item for item in load_original_tracks(repo) if item.state.startswith("blocked")
    ]
    inventory_count = (
        len(selection.formal_due)
        + len(selection.baseline_pool)
        + len(selection.original_due)
        + len(selection.derived_pool)
    )
    scheduled = selection.scheduled_count
    baseline_remaining = len(selection.baseline_pool) - len(selection.selected_baseline)
    source_hash = compound_source_hash(repo, today)
    estimate = estimate_minutes(selection)
    subject_counts = Counter(
        [item.subject for item in selection.selected_formal]
        + [item.subject for item in selection.selected_baseline]
        + [item.subject for item in selection.selected_original]
    )

    lines = [
        "---",
        "schema: morning_review_action_queue_v3",
        f"review_date: {today.isoformat()}",
        f"generation_id: {generation['generation_id']}",
        f"generation_input_sha256: {generation['generation_input_sha256']}",
        "generation_status: queue_bound",
        f"eligible_due_action_set_sha256: {generation['eligible_due_action_set_sha256']}",
        f"formal_due_source_sha256: {generation['formal_due_source_sha256']}",
        f"queue_render_input_sha256: {generation['queue_render_input_sha256']}",
        f"scheduling_policy_version: {generation['scheduling_policy_version']}",
        f"review_ledger_cutoff_sha256: {generation['review_ledger_cutoff']['sha256'] or 'none'}",
        f"review_ledger_cutoff_bytes: {generation['review_ledger_cutoff']['byte_size']}",
        f"review_ledger_cutoff_event_count: {generation['review_ledger_cutoff']['event_line_count']}",
        f"queue_renderer_sha256: {generation['queue_renderer_sha256']}",
        f"queue_schema_sha256: {generation['queue_schema_sha256'] or 'none'}",
        "definition_status: immutable_ready_snapshot",
        "runtime_status_source: wiki/study_vaults/408-full/state/review-loop/runtime.json",
        "review_event_source: wiki/study_vaults/408-full/state/review-loop/events.jsonl",
        "execution_mode: daily_loop",
        "coverage_mode: scheduled_plus_visible_backlog",
        f"target_minutes: {target_minutes}",
        f"max_items: {max_items}",
        f"baseline_quota: {baseline_quota}",
        f"max_original: {max_original}",
        f"inventory_count: {inventory_count}",
        f"formal_due_count: {len(selection.formal_due)}",
        f"baseline_backlog_count: {len(selection.baseline_pool)}",
        f"original_question_due_count: {len(selection.original_due)}",
        f"derived_d0_count: {len(selection.derived_pool)}",
        f"scheduled_count: {scheduled}",
        f"selected_formal_due_count: {len(selection.selected_formal)}",
        f"selected_baseline_count: {len(selection.selected_baseline)}",
        f"selected_original_question_count: {len(selection.selected_original)}",
        f"selected_derived_d0_count: {len(selection.selected_derived)}",
        f"same_day_extension_count: {selection.same_day_extension_count}",
        f"baseline_remaining_count: {baseline_remaining}",
        f"blocked_count: {len(blocked_units) + len(blocked_original)}",
        f"scheduling_gap_count: {scheduling_gap_count}",
        f"source_hash: {source_hash}",
        "---",
        "",
        f"# {today.isoformat()} 408 晨间行动队列",
        "",
        "> [!important] 这是一份可执行队列，不是全库清单",
        "> `inventory_count` 负责把全部库存和到期项记清；`scheduled_count` 才是本次晨间需要真实作答的分母。未入选库存没有被判成完成，也不会改正式日期。",
        "",
        "## 本次策略",
        "",
        f"- 本次打开 {scheduled} 项，首轮估计约 {estimate} 分钟；{target_minutes} 分钟是可配置执行默认，不是记忆科学常数。",
        f"- 真实日期到期 {len(selection.formal_due)} 项；前晚 D0/D1 {len(selection.derived_pool)} 项；满足 7 天保护的原题轨 {len(selection.original_due)} 项。",
        f"- 尚无自然练习证据的 inventory {len(selection.baseline_pool)} 项；默认不专门校准，本次自然命中 {len(selection.selected_baseline)} 项，剩余 {baseline_remaining} 项继续保留且不伪造状态。",
        f"- 同日延展队列 {selection.same_day_extension_count} 项；它们保持可见且未完成，不通过改日期或删项隐藏。",
        f"- 阻塞项 {len(blocked_units) + len(blocked_original)} 项，只进入质量审计，不进入评分分母。",
        f"- 已作答但调度待恢复 {scheduling_gap_count} 项；它们不会重新入题包，必须先完成 append-only 调度恢复或显示明确错误。",
        "- 科目分布：" + "；".join(
            f"{subject}{subject_counts.get(subject, 0)}" for subject in SUBJECTS
        ) + "。",
        "",
        "## 执行顺序",
        "",
        "1. 救援与前晚 D0/D1 优先。",
        "2. 再处理真实日期到期项。",
        "3. 未校准 inventory 不占用默认晨间容量；只在自然学习或有效问答命中时形成 organic baseline。",
        "4. 首答错误、部分正确、空白或低置信正确：反馈后隔 3–5 项再提取；同日最多两次修复，仍不稳则标 `relearn_required`。",
        "5. 同日修复成功不冒充新的跨日成功；正式分数和日期仍须用户明确给出后另行更新。",
        "",
        "## 晨间项目",
        "",
    ]

    item_rows: list[tuple[str, str]] = []
    index = 1
    for item in selection.selected_formal:
        item_id = f"MQ-{index:02d}"
        lines += _section(
            item_id,
            item.title,
            item.unit_id,
            "formal_due",
            stage_adapted_prompt(item),
            f"{item.lane}层；正式下次复习日期 {item.next_date}",
        )
        item_rows.append((item_id, item.unit_id))
        index += 1
    for item in selection.selected_derived:
        item_id = f"MQ-{index:02d}"
        is_loop_action = isinstance(item, LoopAction)
        source_id = item.source_id if is_loop_action else item.key
        item_kind = item.item_kind if is_loop_action else "derived_d0"
        lines += _section(
            item_id,
            item.title,
            source_id,
            item_kind,
            item.prompt,
            (
                f"统一复盘事件派生的 {item.stage} 行动；到期 {item.due_date}；触发事件可追溯"
                if is_loop_action
                else "legacy 前晚 D0/D1 安全派生项"
            ),
            action_id=item.action_id if is_loop_action else None,
            trigger_event_id=item.trigger_event_id if is_loop_action else None,
            stage=item.stage if is_loop_action else None,
            formal_node_id=item.formal_node_id if is_loop_action else None,
            review_unit_id=item.review_unit_id if is_loop_action else None,
        )
        item_rows.append((item_id, source_id))
        index += 1
    for item in selection.selected_original:
        item_id = f"MQ-{index:02d}"
        prompt = (
            "打开原题但不要展开答案或解析，独立完成判型、第一动作与关键步骤；"
            "完成后只记录首个断点和用户明确评分。"
        )
        reason = (
            f"原题轨已激活并满足 7 天保护；最近复做 {item.last_date or '未记录'}；"
            f"安全定位 {protect_location_text(item.marker or '未提取', limit=120)}"
        )
        lines += _section(
            item_id,
            item.node_id,
            item.node_id,
            "original_question",
            prompt,
            reason,
        )
        item_rows.append((item_id, item.node_id))
        index += 1
    for item in selection.selected_baseline:
        item_id = f"MQ-{index:02d}"
        lines += _section(
            item_id,
            item.title,
            item.unit_id,
            "baseline_calibration",
            stage_adapted_prompt(item),
            "全库重构基线 campaign 的本次公平取样；不是伪造的普通到期项",
        )
        item_rows.append((item_id, item.unit_id))
        index += 1

    if not item_rows:
        lines += ["当前没有可执行项目。", ""]

    lines += [
        "## 派生结果账本入口",
        "",
        "| Item | Source ID | 首答 | 置信度 | 秒数 | Prompt level | First break | Feedback type | Repair | 状态 |",
        "|---|---|---|---|---:|---:|---|---|---|---|",
    ]
    for item_id, source_id in item_rows:
        lines.append(
            f"| {item_id} | {source_id} | 待作答 | 待记录 | 未记录 | 待记录 | 待记录 | 待记录 | 待记录 | pending |"
        )
    lines += [
        "",
        "- 使用 `python3 scripts/morning_review_session.py start --queue <本文件>` 建立安全派生 session。",
        "- `record-first` 完成前，执行器拒绝 `reveal-feedback`；结果以追加事件保存，不写 Tutor Attempts 或正式掌握度。",
        "- 关闭条件：本次 `scheduled_count` 全部有首答；需修复项已独立修复或明确进入 `relearn_required`。",
        "",
        "## 库存与延展状态",
        "",
        f"- `inventory_accounting`：{inventory_count}/{inventory_count} 个来源项目已计入本次快照。",
        f"- `morning_executable_queue`：{scheduled} 项；内部不得跳项。",
        f"- `same_day_extension_queue`：{selection.same_day_extension_count} 项；如果晨间仍有精力可续做，否则保持未完成。",
        f"- `baseline_inventory`：剩余 {baseline_remaining} 项；只接受自然有效练习证据，不安排专门清扫。",
        "- 完整库存审计仍可运行 `python3 scripts/audit_review_schedule.py --date YYYY-MM-DD --json`；不再把库存清单直接当晨间作答单。",
        "",
    ]
    return "\n".join(lines)


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Generate an executable 408 morning action queue without formal writes."
    )
    parser.add_argument("--repo", default=str(ROOT))
    parser.add_argument("--date", default=dt.date.today().isoformat())
    parser.add_argument("--max-items", type=int, default=5)
    parser.add_argument("--baseline-quota", type=int, default=0)
    parser.add_argument("--max-original", type=int, default=2)
    parser.add_argument("--target-minutes", type=int, default=40)
    parser.add_argument("--write-safe", action="store_true")
    return parser.parse_args(argv)


def main(argv: list[str]) -> int:
    args = parse_args(argv)
    repo = Path(args.repo).resolve()
    try:
        today = dt.date.fromisoformat(args.date)
        if not 1 <= args.max_items <= 50:
            raise ValueError("--max-items must be between 1 and 50")
        if not 0 <= args.baseline_quota <= args.max_items:
            raise ValueError("--baseline-quota must be between 0 and --max-items")
        if not 0 <= args.max_original <= args.max_items:
            raise ValueError("--max-original must be between 0 and --max-items")
        if not 10 <= args.target_minutes <= 180:
            raise ValueError("--target-minutes must be between 10 and 180")
        content = render_action_queue(
            repo,
            today,
            max_items=args.max_items,
            baseline_quota=args.baseline_quota,
            max_original=args.max_original,
            target_minutes=args.target_minutes,
        )
    except (OSError, ReviewLoopError, ValueError) as exc:
        print(f"action queue generation failed: {exc}", file=sys.stderr)
        return 2

    if not args.write_safe:
        print(content)
        return 0

    try:
        published = publish_generation_queue(
            repo,
            today,
            max_items=args.max_items,
            baseline_quota=args.baseline_quota,
            max_original=args.max_original,
            target_minutes=args.target_minutes,
        )
    except (OSError, ReviewLoopError, ValueError) as exc:
        print(f"action queue publication failed: {exc}", file=sys.stderr)
        return 2
    print(published["queue_path"])
    print(repo / review_loop.DASHBOARD_REL / "当前晨间复盘.md")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
