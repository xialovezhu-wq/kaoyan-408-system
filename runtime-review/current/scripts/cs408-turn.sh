#!/usr/bin/env bash
# 408 campaign turn fast path (2026-09-10 ruling).
#
# One call submits exactly one owner operation and prints only the compact
# fields a learning turn needs, so a turn costs one exec plus one view_image.
# It never rewrites identities and never replays a committed answer.
#
# Usage:
#   scripts/cs408-turn.sh <operation> <input-json-file|->
#   scripts/cs408-turn.sh image-handoff <saved-result-file>
#
# Operations: answer-choice | finalize-reason | append-turn |
#             finish-item | close-unresolved | finalize-appended-and-next | image-handoff
set -euo pipefail

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)"
REPO="${KAOYAN_408_PROJECT_ROOT:-${KAOYAN_408_REPO:-${CS408_REPO:-$(dirname -- "$SCRIPT_DIR")}}}"
FACADE="$REPO/scripts/morning_campaign_turn_408.py"
CS408_PYTHON_BIN="${CS408_PYTHON:-python3}"
REVIEW_DATE="${CS408_REVIEW_DATE:-$(date +%Y-%m-%d)}"
POINTER="$REPO/.private/morning-campaign-control/current-display/$REVIEW_DATE.json"

if [ "$#" -ne 2 ]; then
  printf 'usage: %s <operation> <input-json-file|->\n' "$0" >&2
  exit 2
fi

operation="$1"
input="$2"

case "$operation" in
  answer-choice|finalize-reason|append-turn|finalize-appended-and-next|finish-item|close-unresolved|image-handoff) ;;
  *) printf 'unsupported_operation: %s\n' "$operation" >&2; exit 2 ;;
esac

if [ ! -f "$FACADE" ]; then
  printf 'facade_missing: %s\n' "$FACADE" >&2
  exit 2
fi

stderr_file="$(mktemp -t cs408-turn)"
set +e
raw="$(KAOYAN_408_PROJECT_ROOT="$REPO" "$CS408_PYTHON_BIN" "$REPO/scripts/runtime_paths_408.py" --run morning_campaign_turn_408.py "$operation" --input-json "$input" 2>"$stderr_file")"
code="$?"
set -e

if [ "$code" -ne 0 ]; then
  printf 'exit_code: %s\n' "$code"
  printf '%s\n' "$raw"
  if [ -s "$stderr_file" ]; then
    printf 'stderr:\n'
    cat "$stderr_file"
  fi
  rm -f "$stderr_file"
  exit "$code"
fi
rm -f "$stderr_file"

printf '%s' "$raw" | CS408_POINTER="$POINTER" "$CS408_PYTHON_BIN" -c '
import json
import os
import sys

raw = sys.stdin.read()
try:
    data = json.loads(raw)
except (ValueError, TypeError):
    print(raw)
    raise SystemExit(0)

next_display = data.get("next_display") or {}
provenance = next_display.get("question_provenance") or {}
pointer_path = os.environ.get("CS408_POINTER") or ""
pointer_sha = None
if pointer_path and os.path.isfile(pointer_path):
    try:
        with open(pointer_path, encoding="utf-8") as handle:
            pointer_sha = json.load(handle).get("display_receipt_sha256")
    except (OSError, ValueError):
        pointer_sha = None

compact = {
    "status": data.get("status"),
    "run_id": data.get("run_id"),
    "item_id": data.get("item_id"),
    "reason": data.get("reason"),
    "choice_result": data.get("choice_result"),
    "rating": data.get("rating"),
    "capture_required": data.get("capture_required"),
    "capture_verified": data.get("capture_verified"),
    "package_verified": data.get("package_verified"),
    "capture_id": data.get("capture_id"),
    "capture_receipt_ref": data.get("capture_receipt_ref"),
    "capture_receipt_sha256": data.get("capture_receipt_sha256"),
    "episode": data.get("episode"),
    "terminal_state": data.get("terminal_state"),
    "progress": data.get("progress"),
    "navigation_held": data.get("navigation_held"),
    "fsrs_write_count": data.get("fsrs_write_count"),
    "personal_summary_write_count": data.get("personal_summary_write_count"),
    "correction_review_commit_verified": data.get("correction_review_commit_verified"),
    "light_evidence_saved": data.get("light_evidence_saved"),
    "learning_turn_count": data.get("learning_turn_count"),
    "formal_write_count": data.get("formal_write_count"),
    "learner_evidence_write_count": data.get("learner_evidence_write_count"),
    "replayed": data.get("replayed"),
    "frontier_advanced": data.get("frontier_advanced"),
    "campaign_complete": data.get("campaign_complete"),
    "concept_coverage_complete": data.get("concept_coverage_complete"),
    "unanswered_node_count": data.get("unanswered_node_count"),
    "reason_required": data.get("reason_required"),
    "reason_requirement": data.get("reason_requirement"),
    "next_item_id": next_display.get("item_id"),
    "next_label": provenance.get("label"),
    "next_display_sha256": next_display.get("display_receipt_sha256"),
    "next_image_paths": data.get("next_image_paths") or [],
    "next_display": next_display or None,
    "image_preload_status": data.get("image_preload_status"),
    "image_preload_reason": data.get("image_preload_reason"),
    "turn_count": data.get("turn_count"),
    "trace_sha256": data.get("trace_sha256"),
    "result_file": data.get("result_file"),
    "request_file": data.get("request_file"),
    "current_display_pointer": pointer_path or None,
    "current_display_sha256": pointer_sha,
    # null = no successor display (held item or no advance); True = this call
    # published the live current display; False = an older saved result was
    # re-read, so the printed next_* fields are not the current question.
    "result_matches_current_display": (
        None if pointer_sha is None or next_display.get("display_receipt_sha256") is None
        else next_display.get("display_receipt_sha256") == pointer_sha
    ),
}
if data.get("current_item_personalization"):
    personalization = data["current_item_personalization"]
    compact["current_item_personalization"] = (
        {key: value for key, value in personalization.items() if key != "local_records_markdown"}
        if personalization.get("status") == "available" else personalization
    )
if data.get("study_followup"):
    followup = data["study_followup"]
    compact["followup"] = {
        "local_status": (followup.get("local_tutoring") or {}).get("status"),
        "source_version": (followup.get("local_tutoring") or {}).get("source_version"),
        "publication_status": (followup.get("publication") or {}).get("status"),
    }
    if (compact["followup"]["local_status"] not in {None, "refreshed", "already_current"}
            or compact["followup"]["publication_status"] not in
            {None, "STARTED", "PUBLISHED", "ALREADY_PUBLISHED", "SKIPPED"}):
        compact["study_followup"] = followup
print(json.dumps({key: value for key, value in compact.items() if value is not None},
                 ensure_ascii=False, indent=1))
'
