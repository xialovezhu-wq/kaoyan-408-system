#!/usr/bin/env python3
"""Audit the 408 current-question entrypoint isolation contract.

The filename is retained for compatibility with older release scripts. The active
audit no longer requires personalization; it proves that the new hot path excludes it.
"""

from __future__ import annotations

import argparse
import ast
import hashlib
import json
from pathlib import Path
from typing import Any


SCHEMA = "current_question_hotpath_entrypoint_audit_v1"
HOT_FILES = (
    "scripts/current_question_context_408.py",
    "scripts/current_question_evidence_408.py",
    "scripts/managed_408_current_turn.py",
)
RETIRED_ACTIVE_FILES = (
    ".codex/config.toml",
    "scripts/managed_408_turn_broker.py",
    "scripts/managed_408_rich_intake_turn.py",
    "scripts/manage_408_feature_promotion.py",
    "scripts/monitor_408_option_trial_hook.py",
    "scripts/benchmark_managed_408_option_trial_acceptance_v1.py",
    "schema/managed-408-option-trial-v1.md",
    "schema/managed-408-feature-promotion-v2.md",
)
RETIRED_WRITE_GUARDS = {
    "scripts/managed_408_direct_router.py": (
        "legacy_direct_router_retired_use_answer_current_and_next",
    ),
    "scripts/ordinary_capture_hot_adapter_408.py": (
        "legacy_ordinary_capture_writer_retired_use_managed_current_question_turn",
    ),
    "scripts/morning_capture_hot_adapter_408.py": (
        "legacy_morning_capture_writer_retired_use_managed_current_question_turn",
    ),
    "scripts/personalized_study_turn_408.py": (
        "legacy_personalized_writer_retired_use_managed_current_question_turn",
        "legacy_explicit_fast_intake_retired_use_managed_current_question_turn",
    ),
    "scripts/benchmark_personalization_harness_408.py": (
        "legacy_personalization_benchmark_retired_use_current_question_hotpath_v1",
    ),
    "scripts/benchmark_single_question_hotpath_production_408.py": (
        "legacy_cross_question_benchmark_retired_use_current_question_hotpath_v1",
    ),
    "scripts/benchmark_personalization_production_snapshot_408.py": (
        "legacy_personalization_snapshot_retired_use_current_question_hotpath_v1",
    ),
    "scripts/personalization_corpus_408.py": (
        "personalization_corpus_writes_retired_read_only_compatibility",
    ),
    "scripts/personalization_index_408.py": (
        "personalization_index_writes_retired_read_only_compatibility",
    ),
    "scripts/cross_shadow_activation_408.py": (
        "cross_shadow_activation_writes_retired_read_only_compatibility",
    ),
    "scripts/variant_demand_planner_408.py": (
        "variant_demand_writes_retired_read_only_compatibility",
    ),
    "scripts/morning_personalized_runtime_408.py": (
        "morning_personalized_runtime_retired_read_only_compatibility",
    ),
    "scripts/personalized_cross_candidates_408.py": (
        "personalized_cross_candidate_writes_retired_read_only_compatibility",
    ),
    "scripts/cross_subject_candidates_408.py": (
        "cross_subject_projection_writes_retired_read_only_compatibility",
    ),
    "scripts/variant_source_pipeline_408.py": (
        "variant_source_writes_retired_read_only_compatibility",
    ),
    "scripts/cross_variant_pipeline_408.py": (
        "cross_variant_writes_retired_read_only_compatibility",
    ),
    "scripts/cross_variant_demand_planner_408.py": (
        "cross_variant_demand_writes_retired_read_only_compatibility",
    ),
    "scripts/morning_personalized_router_408.py": (
        "morning_personalized_router_writes_retired_read_only_compatibility",
    ),
    "scripts/personalization_turn_receipt_408.py": (
        "personalization_turn_receipt_writes_retired_read_only_compatibility",
    ),
    "scripts/personalized_context_408.py": (
        "personalized_context_writes_retired_read_only_compatibility",
        '"compatibility_read_only": True',
    ),
}
RETIRED_WRITE_CLI_TOKENS = {
    "scripts/personalized_cross_candidates_408.py": ("--write-overlay",),
    "scripts/cross_subject_candidates_408.py": (
        "--write-projection",
        "--projection-path",
    ),
    "scripts/variant_source_pipeline_408.py": (
        'add_parser("prepare-generation")',
        'add_parser("finalize-verified")',
        'add_parser("rebuild-catalog")',
    ),
    "scripts/cross_variant_pipeline_408.py": (
        'add_parser("prepare")',
        'add_parser("commit-author")',
        'add_parser("commit-blind")',
        'add_parser("reveal")',
        'add_parser("finalize")',
        'add_parser("rebuild-catalog")',
    ),
    "scripts/cross_variant_demand_planner_408.py": (
        'add_parser("prepare-demand")',
        "--no-write",
    ),
    "scripts/morning_personalized_router_408.py": ("--demand-dir",),
    "scripts/personalization_turn_receipt_408.py": (
        'add_parser("begin-turn")',
        'add_parser("finalize-reply")',
        'add_parser("seal-legacy")',
        'add_parser("seal-invalid-begin")',
    ),
}
FORBIDDEN_HOT_TOKENS = (
    "personalization_turn_receipt_408",
    "personalization_preanswer_408",
    "personalized_study_turn_408",
    "personalization_corpus_408",
    "personalization_index_408",
    "linked_practice",
    "variant_demand",
    "cross_shadow_activation",
    "preprocess_consumer",
    "127.0.0.1:8767",
    "localhost:8767",
    "节点总表.md",
    "关系边表.md",
    "知识点命中索引.md",
)
REQUIRED_CURRENT_TURN_TOKENS = (
    "current_question_minimal",
    "answer-current-and-next",
    "recover-current-capture",
    "recover-current-receipts",
    "feedback_authorized",
    "next_item_published",
    "formal_write_count",
)
PUBLIC_CURRENT_TURN_COMMANDS = {
    "answer-current-and-next",
    "recover-current-capture",
    "recover-current-receipts",
    "resume-held-session",
}
REQUIRED_CONTEXT_TOKENS = (
    "current-question-context-v1",
    "grader_capsule_id",
    "source_binding_sha256",
    "learner_evidence",
    "assessment",
)
REQUIRED_EVIDENCE_TOKENS = (
    "current-question-evidence-bundle-v1",
    "manifest_sha256",
    "attachment_sha256s",
    "0o700",
    "0o600",
)


def _sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _read(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def _python_parse(path: Path, violations: list[str]) -> str:
    try:
        body = _read(path)
        ast.parse(body, filename=str(path))
        return body
    except (OSError, UnicodeError, SyntaxError) as exc:
        violations.append(f"unreadable_or_invalid_python:{path}:{exc}")
        return ""


def _parser_commands(body: str) -> set[str]:
    commands: set[str] = set()
    tree = ast.parse(body)
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call) or not isinstance(node.func, ast.Attribute):
            continue
        if node.func.attr != "add_parser" or not node.args:
            continue
        first = node.args[0]
        if isinstance(first, ast.Constant) and isinstance(first.value, str):
            commands.add(first.value)
    return commands


def audit(repo: Path) -> dict[str, Any]:
    repo = repo.expanduser().resolve()
    violations: list[str] = []
    checked: list[dict[str, Any]] = []

    hot_bodies: dict[str, str] = {}
    for rel in HOT_FILES:
        path = repo / rel
        if not path.is_file() or path.is_symlink():
            violations.append(f"missing_or_unsafe_hot_file:{rel}")
            continue
        body = _python_parse(path, violations)
        hot_bodies[rel] = body
        checked.append({"path": rel, "sha256": _sha(path)})
        lowered = body.lower()
        for token in FORBIDDEN_HOT_TOKENS:
            if token.lower() in lowered:
                violations.append(f"forbidden_hot_dependency:{rel}:{token}")

    current_body = hot_bodies.get("scripts/managed_408_current_turn.py", "")
    for token in REQUIRED_CURRENT_TURN_TOKENS:
        if token not in current_body:
            violations.append(f"missing_current_turn_contract:{token}")
    current_commands = _parser_commands(current_body) if current_body else set()
    for command in sorted(current_commands - PUBLIC_CURRENT_TURN_COMMANDS):
        violations.append(f"retired_current_turn_cli_present:{command}")
    for command in sorted(PUBLIC_CURRENT_TURN_COMMANDS - current_commands):
        violations.append(f"required_current_turn_cli_missing:{command}")

    context_body = hot_bodies.get("scripts/current_question_context_408.py", "")
    for token in REQUIRED_CONTEXT_TOKENS:
        if token not in context_body:
            violations.append(f"missing_context_contract:{token}")

    evidence_body = hot_bodies.get("scripts/current_question_evidence_408.py", "")
    for token in REQUIRED_EVIDENCE_TOKENS:
        if token not in evidence_body:
            violations.append(f"missing_private_evidence_contract:{token}")

    capture_contract = repo / "scripts/intake_fact_capture_408.py"
    if not capture_contract.is_file() or capture_contract.is_symlink():
        violations.append("missing_or_unsafe_capture_contract")
    else:
        capture_body = _read(capture_contract)
        checked.append(
            {
                "path": "scripts/intake_fact_capture_408.py",
                "sha256": _sha(capture_contract),
            }
        )
        if "current_question_failure_standing_policy_v1" not in capture_body:
            violations.append("missing_capture_standing_policy")

    for rel in RETIRED_ACTIVE_FILES:
        if (repo / rel).exists():
            violations.append(f"retired_control_still_active:{rel}")

    for rel, guards in RETIRED_WRITE_GUARDS.items():
        path = repo / rel
        if not path.is_file() or path.is_symlink():
            violations.append(f"missing_or_unsafe_legacy_compatibility:{rel}")
            continue
        body = _python_parse(path, violations)
        checked.append({"path": rel, "sha256": _sha(path)})
        for guard in guards:
            if guard not in body:
                violations.append(f"legacy_writer_not_retired:{rel}:{guard}")

    for rel, forbidden_tokens in RETIRED_WRITE_CLI_TOKENS.items():
        path = repo / rel
        if not path.is_file() or path.is_symlink():
            continue
        body = _read(path)
        for token in forbidden_tokens:
            if token in body:
                violations.append(f"retired_write_cli_present:{rel}:{token}")

    morning_session = repo / "scripts/morning_review_session.py"
    if not morning_session.is_file() or morning_session.is_symlink():
        violations.append("missing_or_unsafe_morning_session_boundary")
    else:
        morning_body = _python_parse(morning_session, violations)
        checked.append(
            {
                "path": "scripts/morning_review_session.py",
                "sha256": _sha(morning_session),
            }
        )
        for guard in (
            "legacy_answer_turn_retired_use_managed_408_current_turn",
            "legacy_schedule_followup_retired_use_existing_queue_next_item",
            "legacy_record_first_retired_use_managed_408_current_turn",
            "legacy_reveal_feedback_retired_use_managed_408_current_turn",
            "current_only = True",
            "_current_question_receipt_only",
            "_current_question_managed_v1",
        ):
            if guard not in morning_body:
                violations.append(f"morning_legacy_route_not_retired:{guard}")
        retired_commands = _parser_commands(morning_body) & {
            "answer-turn",
            "schedule-followup",
            "record-first",
            "reveal-feedback",
        }
        for command in sorted(retired_commands):
            violations.append(f"retired_morning_cli_present:{command}")
        for import_token in (
            "from personalized_context_408 import",
            "from linked_practice_source_408 import",
        ):
            if import_token in morning_body:
                violations.append(f"morning_hot_import_present:{import_token}")

    prepared_pack = repo / "scripts/morning_review_prepared_pack_408.py"
    if not prepared_pack.is_file() or prepared_pack.is_symlink():
        violations.append("missing_or_unsafe_prepared_pack_boundary")
    else:
        prepared_body = _python_parse(prepared_pack, violations)
        checked.append(
            {
                "path": "scripts/morning_review_prepared_pack_408.py",
                "sha256": _sha(prepared_pack),
            }
        )
        if "open-grading-context" in _parser_commands(prepared_body):
            violations.append("private_grader_cli_still_public")

    preparation = repo / "scripts/prepare_next_morning_408.py"
    if not preparation.is_file() or preparation.is_symlink():
        violations.append("missing_or_unsafe_next_morning_preparation")
    else:
        preparation_body = _python_parse(preparation, violations)
        checked.append(
            {
                "path": "scripts/prepare_next_morning_408.py",
                "sha256": _sha(preparation),
            }
        )
        for token in (
            "WORKER_PROMOTION_SCHEMA",
            "promotion_receipt",
            "guardian_build_sha256",
            "config_sha256",
        ):
            if token in preparation_body:
                violations.append(f"retired_next_morning_control_present:{token}")

    skill_checks = {
        "codex-skill-sources/kaoyan-408-daily-study-loop/SKILL.md": (
            "answer-current-and-next",
            "awaiting_sol_formalization",
            "/Users/your-user/.codex/kaoyan-408-conversation-packages",
        ),
        "codex-skill-sources/kaoyan-408-question-worker/SKILL.md": (
            "answer-current-and-next",
            "raw_archive_locator",
        ),
        "codex-skill-sources/kaoyan-408-wrong-intake/SKILL.md": (
            "conversation.json",
            "awaiting_sol_formalization",
            "background_processing=none",
        ),
        "codex-skill-sources/_shared/kaoyan-408/personalization-harness.md": (
            "retired from the current-question hot path",
            "read-only compatibility",
            "must not call",
        ),
    }
    for rel, tokens in skill_checks.items():
        path = repo / rel
        if not path.is_file() or path.is_symlink():
            violations.append(f"missing_or_unsafe_contract:{rel}")
            continue
        body = _read(path)
        checked.append({"path": rel, "sha256": _sha(path)})
        for token in tokens:
            if token.lower() not in body.lower():
                violations.append(f"missing_skill_contract:{rel}:{token}")

    return {
        "schema": SCHEMA,
        "status": "PASS" if not violations else "FAIL",
        "hot_files": list(HOT_FILES),
        "checked": checked,
        "forbidden_dependency_count": sum(
            item.startswith("forbidden_hot_dependency:") for item in violations
        ),
        "retired_control_count": sum(
            item.startswith("retired_control_still_active:") for item in violations
        ),
        "violations": violations,
        "personalization_required": False,
        "luna_chat_call_count": 0,
        "formal_write_count": 0,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo", type=Path, default=Path.cwd())
    args = parser.parse_args(argv)
    result = audit(args.repo)
    print(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True))
    return 0 if result["status"] == "PASS" else 1


if __name__ == "__main__":
    raise SystemExit(main())
