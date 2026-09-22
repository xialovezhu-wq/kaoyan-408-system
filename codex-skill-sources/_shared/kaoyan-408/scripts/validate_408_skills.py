#!/usr/bin/env python3
"""Deterministic static gates for the installed kaoyan-408 skill family."""

from __future__ import annotations

import argparse
import json
import re
import sys
from collections import Counter
from pathlib import Path


SKILL_NAMES = (
    "kaoyan-408-daily-intake-curation",
    "kaoyan-408-daily-study-loop",
    "kaoyan-408-intake-coordinator",
    "kaoyan-408-morning-control",
    "kaoyan-408-qa-template",
    "kaoyan-408-question-worker",
    "kaoyan-408-rollback-review",
    "kaoyan-408-wrong-intake",
)

SKILL_PROMPT_BUDGETS = {
    "kaoyan-408-daily-intake-curation": {"lines": 205, "bytes": 15000},
    "kaoyan-408-daily-study-loop": {"lines": 140, "bytes": 11000},
    "kaoyan-408-morning-control": {"lines": 165, "bytes": 12500},
    "kaoyan-408-wrong-intake": {"lines": 170, "bytes": 11000},
}

REQUIRED_EVAL_NAMES = {
    "kaoyan-408-morning-control": {
        "ready_start", "start_continues_pending_preparation", "resume_same_date",
        "campaign_start_shows_true_exam_provenance_before_first_stem",
        "campaign_successor_shows_variant_base_true_exam",
        "campaign_resume_old_receipt_derives_provenance_without_rewrite",
    },
    "kaoyan-408-daily-study-loop": {
        "wrong_multiround_buffers_until_terminal_then_captures_final_package_once",
        "first_answer_fully_correct_captures_once_before_successor",
        "terminal_capture_failure_blocks_navigation_and_recovers_same_identity",
        "attachment_supplement",
        "campaign_failure_uses_exact_buffer_and_single_terminal_entry",
        "campaign_failure_postadvance_recovery_same_identity",
        "campaign_failure_incomplete_trace_blocks_capture",
        "campaign_true_exam_displays_verified_year_and_number",
        "campaign_variant_displays_exact_base_true_exam",
        "campaign_formal_original_does_not_fabricate_true_exam_year",
    },
    "kaoyan-408-wrong-intake": {
        "managed_terminal_independent_correct_one_capture",
        "ordinary_standalone_teaching_is_not_an_entry",
        "explicit_quick_intake_creates_one_package_one_capture_zero_formal",
        "missing_solution_is_saved", "long_trace_untruncated",
        "standalone_replay_and_recovery",
    },
    "kaoyan-408-daily-intake-curation": {
        "direct_evidence", "native_luna_reads", "archive_pending", "archive_success",
        "cross_date_cutoff_backlog", "only_today_and_exact_subset",
        "dated_recovery_continues",
    },
    "kaoyan-408-question-worker": {
        "first_break", "correct_endpoint_invalid_chain_is_nonterminal",
        "standalone_teaching_is_strictly_read_only",
        "standalone_explicit_quick_intake_routes_to_wrong_intake",
        "visual_boundary", "precise_raw_archive_reopen",
    },
}

SCHEMA_REQUIREMENTS = {
    "morning-campaign-display-v1.md": (
        "morning-campaign-question-provenance-v1",
        "真题｜YYYY 年考研 408 第 N 题",
        "变式题｜基于 YYYY 年考研 408 真题第 N 题",
        "来源原题｜非系统生成变式｜真题年份未确认",
        "Historical receipts without the new",
        "derive it read-only",
    ),
    "morning-campaign-failure-terminal-v1.md": (
        "python3 scripts/morning_campaign_failure_terminal_408.py buffer-turn",
        "python3 scripts/morning_campaign_failure_terminal_408.py finalize-and-next",
        "rapid-review-durable-capture-receipt-v2",
        "formalization_authorized=true",
        "formal_write_count=0",
    ),
    "managed-408-answer-current-and-next-v1.md": (
        "answer-current-and-next",
        "--display-receipt-locator",
        "managed-408-answer-current-and-next-v1",
        "next_item_published",
        "cs408-conversation-package-v1",
        "awaiting_sol_formalization",
    ),
    "cs408-conversation-package-v1.md": (
        "/Users/your-user/.codex/kaoyan-408-conversation-packages",
        "conversation.json", "source.json", "attachments", "truncated=false",
        "awaiting_sol_formalization", "01_408/资料库/原始会话资料",
    ),
}

def is_evaluation_copy(path: Path, root: Path) -> bool:
    """Exclude skill-creator workspaces and snapshots from installed-skill discovery."""

    try:
        relative = path.resolve().relative_to(root)
    except ValueError:
        return False
    return any(
        part == "skill-snapshot" or part.endswith("-workspace")
        for part in relative.parts
    )


def frontmatter(text: str, path: Path) -> dict[str, str]:
    if not text.startswith("---\n"):
        raise ValueError(f"{path}: missing frontmatter")
    try:
        block = text.split("---\n", 2)[1]
    except IndexError as exc:
        raise ValueError(f"{path}: unclosed frontmatter") from exc
    result: dict[str, str] = {}
    for raw in block.splitlines():
        if not raw.strip() or raw.lstrip().startswith("#"):
            continue
        key, sep, value = raw.partition(":")
        if not sep:
            raise ValueError(f"{path}: invalid frontmatter line {raw!r}")
        result[key.strip()] = value.strip().strip('"')
    return result


def referenced_markdown(skill_dir: Path, text: str) -> list[Path]:
    refs: list[Path] = []
    tokens = re.findall(r"`([^`]+\.md)`", text)
    tokens += re.findall(r"\[[^\]]+\]\(([^)]+\.md)(?:#[^)]+)?\)", text)
    for token in tokens:
        if token.startswith("/"):
            path = Path(token)
        elif token.startswith("references/") or token.startswith("../"):
            path = (skill_dir / token).resolve()
        else:
            continue
        refs.append(path)
    return refs


def visible_template_blocks(text: str) -> list[str]:
    return re.findall(r"```text\n(.*?)```", text, flags=re.DOTALL)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--skills-root", default=str(Path.home() / ".codex" / "skills")
    )
    parser.add_argument(
        "--schema-root",
        help="Optional repository schema directory for cross-contract validation",
    )
    args = parser.parse_args()
    root = Path(args.skills_root).expanduser().resolve()
    schema_root = (
        Path(args.schema_root).expanduser().resolve()
        if args.schema_root
        else None
    )
    failures: list[str] = []
    metrics: dict[str, object] = {"skills_root": str(root), "skills": {}}

    discovered: list[str] = []
    for skill_file in root.rglob("SKILL.md"):
        if is_evaluation_copy(skill_file, root):
            continue
        try:
            raw = skill_file.read_text(encoding="utf-8")
        except OSError:
            continue
        match = re.search(r"(?m)^name:\s*['\"]?([^'\"\n]+)", raw)
        if match and match.group(1).strip() in SKILL_NAMES:
            discovered.append(match.group(1).strip())
    duplicate_names = sorted(name for name, count in Counter(discovered).items() if name and count > 1)
    if duplicate_names:
        failures.append(f"duplicate discovered skill names: {duplicate_names}")

    for name in SKILL_NAMES:
        skill_dir = root / name
        skill_file = skill_dir / "SKILL.md"
        if not skill_file.is_file():
            failures.append(f"missing skill: {skill_file}")
            continue
        text = skill_file.read_text(encoding="utf-8")
        try:
            meta = frontmatter(text, skill_file)
        except ValueError as exc:
            failures.append(str(exc))
            continue
        if meta.get("name") != name:
            failures.append(f"{skill_file}: name mismatch {meta.get('name')!r}")
        description = meta.get("description", "")
        if not description:
            failures.append(f"{skill_file}: empty description")
        if len(description) > 1024:
            failures.append(f"{skill_file}: description exceeds 1024 characters")
        lines = text.count("\n") + 1
        byte_count = len(text.encode("utf-8"))
        # Size is visible diagnostic data, not a fixed prompt quota. Required
        # routing, evidence and recovery checks below remain hard gates.
        for ref in referenced_markdown(skill_dir, text):
            if not ref.is_file():
                failures.append(f"{skill_file}: missing referenced file {ref}")

        eval_path = skill_dir / "evals" / "evals.json"
        try:
            payload = json.loads(eval_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            failures.append(f"{eval_path}: {exc}")
            payload = {}
        if payload.get("skill_name") != name:
            failures.append(f"{eval_path}: skill_name mismatch")
        evals = payload.get("evals")
        if not isinstance(evals, list) or not evals:
            failures.append(f"{eval_path}: missing eval cases")
            evals = []
        eval_ids: set[int] = set()
        eval_names: set[str] = set()
        for ordinal, case in enumerate(evals, 1):
            if not isinstance(case, dict):
                failures.append(f"{eval_path}: eval case is not an object")
                continue
            case_id = case.get("id", ordinal)
            case_name = case.get("name")
            if not isinstance(case_id, int) or case_id in eval_ids:
                failures.append(f"{eval_path}: invalid or duplicate eval id {case_id!r}")
            else:
                eval_ids.add(case_id)
            if not isinstance(case_name, str) or not case_name.strip() or case_name in eval_names:
                failures.append(f"{eval_path}: invalid or duplicate eval name {case_name!r}")
            else:
                eval_names.add(case_name)
            for field in ("prompt", "expected_output"):
                if not isinstance(case.get(field), str) or not case[field].strip():
                    failures.append(f"{eval_path}: eval {case_id} lacks nonempty {field}")
            expectations = case.get("expectations")
            if not isinstance(expectations, list) or not expectations or not all(
                isinstance(item, str) and item.strip() for item in expectations
            ):
                failures.append(f"{eval_path}: eval {case_id} lacks verifiable expectations")
        missing_eval_names = sorted(
            REQUIRED_EVAL_NAMES.get(name, set()) - eval_names
        )
        if missing_eval_names:
            failures.append(
                f"{eval_path}: missing contract evals {missing_eval_names}"
            )

        yaml_path = skill_dir / "agents" / "openai.yaml"
        if not yaml_path.is_file():
            failures.append(f"missing agent config: {yaml_path}")
        else:
            yaml_text = yaml_path.read_text(encoding="utf-8")
            if f"${name}" not in yaml_text:
                failures.append(f"{yaml_path}: default_prompt must mention ${name}")

        metrics["skills"][name] = {
            "skill_lines": lines,
            "skill_bytes": byte_count,
            "skill_chars": len(text),
            "eval_cases": len(evals),
        }

    combined_nonlegacy = "\n".join(
        p.read_text(encoding="utf-8")
        for name in SKILL_NAMES
        if name != "kaoyan-408-intake-coordinator"
        for p in (root / name).rglob("*.md")
    )
    forbidden_exec = (
        r"python3\s+scripts/intake_queue_408\.py",
        r"atomic_queue_submit",
        r"coordinator\s+register",
    )
    for pattern in forbidden_exec:
        if re.search(pattern, combined_nonlegacy):
            failures.append(f"non-legacy skill contains executable retired queue pattern: {pattern}")

    if re.search(r"每轮[^\n]{0,20}1\s*[-到至]\s*3\s*个?问题", combined_nonlegacy):
        failures.append("learning skill still permits 1-3 questions per turn")

    required_fragments = {
        "kaoyan-408-daily-study-loop/SKILL.md": (
            "answer-current-and-next",
            "/Users/your-user/.codex/kaoyan-408-conversation-packages",
            "awaiting_sol_formalization",
            "24 events",
            "32 KiB",
            "navigation frontier",
            "zero formal writes",
            "question_provenance.label",
        ),
        "kaoyan-408-morning-control/SKILL.md": (
            "question_provenance.label",
            "真题｜YYYY 年考研 408 第 N 题",
            "变式题｜基于 YYYY 年考研 408 真题第 N 题",
            "来源原题｜非系统生成变式｜真题年份未确认",
        ),
        "kaoyan-408-question-worker/SKILL.md": (
            "one next action",
            "managed_408_current_turn.py",
            "answer-current-and-next",
            "awaiting_sol_formalization",
            "ordinary questioning",
            "Create no package",
            "route to `kaoyan-408-wrong-intake`",
            "read-only by default",
        ),
        "kaoyan-408-wrong-intake/SKILL.md": (
            "final package -> exactly one Capture",
            "/Users/your-user/.codex/kaoyan-408-conversation-packages",
            "awaiting_sol_formalization",
            "question_image",
            "explanation_image",
            "FORMAL_COMMITTED_ARCHIVE_PENDING",
            "standalone_question_intake_408.py record --input-json",
            "session_id=null",
        ),
        "kaoyan-408-daily-intake-curation/SKILL.md": (
            "explicit",
            "capture-set SHA",
            "Ultra",
            "read-only",
            "sol_curation_decision_408.py",
            "batch-size-1",
            "already_current",
            "/Volumes/T9-Data",
            "FORMAL_COMMITTED_ARCHIVE_PENDING",
            "backlog-plan",
            "cutoff_date",
            "--only-today",
            "Never create a cross-date batch",
        ),
        "kaoyan-408-intake-coordinator/SKILL.md": (
            "one named historical",
            "Never call apply again",
            "retired personalization pre-answer",
        ),
        "kaoyan-408-qa-template/SKILL.md": (
            "retired personalization pre-answer",
        ),
        "kaoyan-408-rollback-review/SKILL.md": (
            "retired personalization pre-answer",
        ),
    }
    for rel, fragments in required_fragments.items():
        body = (root / rel).read_text(encoding="utf-8")
        for fragment in fragments:
            if fragment.lower() not in body.lower():
                failures.append(f"{rel}: missing invariant {fragment!r}")

    current_evidence_paths = (
        "kaoyan-408-daily-study-loop/SKILL.md",
        "kaoyan-408-daily-study-loop/evals/evals.json",
        "kaoyan-408-daily-study-loop/references/morning-review.md",
        "kaoyan-408-wrong-intake/SKILL.md",
        "kaoyan-408-wrong-intake/evals/evals.json",
        "kaoyan-408-wrong-intake/references/fact-capture-schema.md",
        "kaoyan-408-daily-intake-curation/SKILL.md",
        "kaoyan-408-daily-intake-curation/evals/evals.json",
        "_shared/kaoyan-408/runtime-contract.md",
    )
    retired_evidence_tokens = (
        "current-question-evidence-bundle-v1",
        "current-question-evidence-bundle-v2",
        "current_question_evidence_bundle_v1",
        "current-question-interaction-trace-v1",
    )
    for rel in current_evidence_paths:
        body = (root / rel).read_text(encoding="utf-8")
        for token in retired_evidence_tokens:
            if token.lower() in body.lower():
                failures.append(
                    f"{rel}: contains retired current evidence token {token!r}"
                )
    daily_curation_body = (
        root / "kaoyan-408-daily-intake-curation/SKILL.md"
    ).read_text(encoding="utf-8")
    if "~/.codex/study-intake-preprocessor/bin/preprocess_consumer.py" in daily_curation_body:
        failures.append(
            "daily intake curation invokes a retired preprocessing consumer"
        )

    current_contract_paths = (
        "kaoyan-408-daily-study-loop/SKILL.md",
        "kaoyan-408-daily-study-loop/evals/evals.json",
        "kaoyan-408-wrong-intake/SKILL.md",
        "kaoyan-408-wrong-intake/evals/evals.json",
        "kaoyan-408-daily-intake-curation/SKILL.md",
        "kaoyan-408-daily-intake-curation/evals/evals.json",
        "_shared/kaoyan-408/runtime-contract.md",
    )
    invented_contract_tokens = (
        "current-question-study-entry-v1",
        "bounded-teaching-trace-v1",
        "deterministic-study-candidate-snapshot-v1",
        "study-observation-luna-suggestions-v1",
    )
    for rel in current_contract_paths:
        body = (root / rel).read_text(encoding="utf-8")
        for token in invented_contract_tokens:
            if token.lower() in body.lower():
                failures.append(f"{rel}: contains unsupported contract token {token!r}")

    exact_cli = "answer-current-and-next"
    for rel in (
        "kaoyan-408-daily-study-loop/SKILL.md",
        "_shared/kaoyan-408/runtime-contract.md",
    ):
        body = (root / rel).read_text(encoding="utf-8")
        if exact_cli.lower() not in body.lower():
            failures.append(f"{rel}: missing exact composite CLI prefix")

    daily_rel = "kaoyan-408-daily-study-loop/SKILL.md"
    for rel in (
        daily_rel,
        "kaoyan-408-wrong-intake/SKILL.md",
    ):
        body = (root / rel).read_text(encoding="utf-8")
        if re.search(
            r"Read\s+`\.\./_shared/kaoyan-408/personalization-harness\.md`\s+for every request",
            body,
            flags=re.IGNORECASE,
        ):
            failures.append(f"{rel}: long personalization Harness is mandatory on every request")

    runtime_path = root / "_shared" / "kaoyan-408" / "runtime-contract.md"
    try:
        runtime_body = runtime_path.read_text(encoding="utf-8")
    except OSError as exc:
        failures.append(f"{runtime_path}: {exc}")
    else:
        for fragment in (
            "answer-current-and-next",
            "/Users/your-user/.codex/kaoyan-408-conversation-packages",
            "awaiting_sol_formalization",
            "gpt-6-astra",
            "source_version",
            "batch-size-1",
            "/Volumes/T9-Data",
            "FORMAL_COMMITTED_ARCHIVE_PENDING",
        ):
            if fragment.lower() not in runtime_body.lower():
                failures.append(f"{runtime_path}: missing invariant {fragment!r}")

    if schema_root is not None:
        direct_reference_paths = (
            "kaoyan-408-daily-study-loop/references/morning-review.md",
            "kaoyan-408-daily-study-loop/references/evening-summary-and-drill.md",
            "kaoyan-408-daily-study-loop/references/safety-boundaries.md",
            "kaoyan-408-wrong-intake/references/intake-checklist.md",
            "kaoyan-408-wrong-intake/references/parallel-intake-contract.md",
            "kaoyan-408-wrong-intake/references/standalone-question-intake.md",
            "kaoyan-408-daily-intake-curation/references/parallel-intake-contract.md",
            "kaoyan-408-daily-intake-curation/references/backlog-cutoff-plan.md",
        )
        retired_direct_tokens = (
            "awaiting_daily_curation",
            "awaiting_background_analysis",
            "current_question_failure_standing_policy_v1",
            "current-question-background-handoff",
            "preprocess_consumer",
            "two_pass_ready",
            "record-adoption",
            "adoption token",
            "24-event and 32-KiB limits",
            "current-question-evidence-bundle-v3",
            "current-question-interaction-trace-v2",
            "one to eight",
            "independent-correct observation",
            "separate background Study Intake",
            "luna:background-only",
        )
        for rel in direct_reference_paths:
            body = (root / rel).read_text(encoding="utf-8")
            for token in retired_direct_tokens:
                if token.lower() in body.lower():
                    failures.append(
                        f"{rel}: contains retired direct-route token {token!r}"
                    )

        managed_path = schema_root.parent / "scripts/managed_408_current_turn.py"
        managed_body = managed_path.read_text(encoding="utf-8")
        for token in (
            "capture-formal-problem-document",
            "background_handoff",
            "publish_background_handoff",
            "awaiting_background_analysis",
            "producer_binding",
            "Dispatcher",
        ):
            if token.lower() in managed_body.lower():
                failures.append(
                    f"{managed_path}: contains retired managed-route token {token!r}"
                )

        evidence_path = schema_root.parent / "scripts/current_question_evidence_408.py"
        evidence_body = evidence_path.read_text(encoding="utf-8")
        for token in (
            "study-intake-preprocessor",
            "BACKGROUND_HANDOFF",
            "background_handoff",
            "publish_background_handoff",
            "read_background_handoff",
            "awaiting_background_analysis",
            "publish_study_observation",
        ):
            if token.lower() in evidence_body.lower():
                failures.append(
                    f"{evidence_path}: contains retired private-evidence token {token!r}"
                )
        if "/Users/your-user/.codex/kaoyan-408-conversation-packages" not in evidence_body:
            failures.append(
                f"{evidence_path}: default root is not the local 408 package root"
            )

        conversation_path = schema_root.parent / "scripts/conversation_package_408.py"
        conversation_body = conversation_path.read_text(encoding="utf-8")
        for token in ("study-intake-preprocessor", "LEGACY_ROOT"):
            if token in conversation_body:
                failures.append(
                    f"{conversation_path}: contains implicit legacy runtime dependency {token!r}"
                )
        migration_cli = (
            schema_root.parent / "scripts/migrate_408_conversation_packages.py"
        ).read_text(encoding="utf-8")
        if 'parser.add_argument("--legacy-root", required=True)' not in migration_cli:
            failures.append("legacy migration CLI does not require explicit --legacy-root")

        for filename, fragments in SCHEMA_REQUIREMENTS.items():
            path = schema_root / filename
            try:
                body = path.read_text(encoding="utf-8")
            except OSError as exc:
                failures.append(f"{path}: {exc}")
                continue
            for fragment in fragments:
                if fragment.lower() not in body.lower():
                    failures.append(f"{path}: missing invariant {fragment!r}")

    harness_path = root / "_shared" / "kaoyan-408" / "personalization-harness.md"
    try:
        harness_body = harness_path.read_text(encoding="utf-8")
    except OSError as exc:
        failures.append(f"{harness_path}: {exc}")
    else:
        for fragment in (
            "retired from the current-question hot path",
            "read-only compatibility",
            "must not call",
            "managed_408_current_turn.py",
            "personalization corpus",
        ):
            if fragment.lower() not in harness_body.lower():
                failures.append(f"{harness_path}: missing invariant {fragment!r}")

    retired_continuity_paths = (
        "kaoyan-408-daily-study-loop/SKILL.md",
        "kaoyan-408-daily-study-loop/evals/evals.json",
        "kaoyan-408-question-worker/SKILL.md",
        "kaoyan-408-question-worker/evals/evals.json",
        "_shared/kaoyan-408/personalization-harness.md",
        "_shared/kaoyan-408/runtime-contract.md",
    )
    retired_continuity_tokens = (
        "guardian_reflection.cli",
        "learning-guardian/daily-reflection/app",
        "handoff-finalize",
        "resume_question_changed",
        "not_used_current_context",
        "episode_handoff_",
    )
    for rel in retired_continuity_paths:
        body = (root / rel).read_text(encoding="utf-8")
        for token in retired_continuity_tokens:
            if token.lower() in body.lower():
                failures.append(f"{rel}: contains retired tutoring continuity token {token!r}")

    for template in root.glob("kaoyan-408-*/references/*output-template.md"):
        body = template.read_text(encoding="utf-8")
        for block in visible_template_blocks(body):
            for line in block.splitlines():
                if re.match(r"^#{1,6}\s", line):
                    failures.append(f"{template}: visible template contains Markdown heading")
                if re.search(r"\*\*|__|~~", line):
                    failures.append(f"{template}: visible template contains Markdown emphasis")

    fast_output = root / "kaoyan-408-wrong-intake" / "references" / "output-template.md"
    try:
        fast_blocks = visible_template_blocks(fast_output.read_text(encoding="utf-8"))
    except OSError as exc:
        failures.append(f"{fast_output}: {exc}")
    else:
        if not fast_blocks:
            failures.append(f"{fast_output}: missing fast-capture visible block")
        else:
            first = fast_blocks[0]
            for forbidden in ("正式入库完成", "高质量入库完成", "强关联旧题"):
                if forbidden in first:
                    failures.append(
                        f"{fast_output}: fast visible block contains forbidden completion {forbidden!r}"
                    )

    metrics["duplicate_skill_names"] = duplicate_names
    metrics["schema_root"] = str(schema_root) if schema_root else None
    metrics["status"] = "PASS" if not failures else "FAIL"
    metrics["failures"] = failures
    print(json.dumps(metrics, ensure_ascii=False, indent=2))
    return 0 if not failures else 1


if __name__ == "__main__":
    sys.exit(main())
