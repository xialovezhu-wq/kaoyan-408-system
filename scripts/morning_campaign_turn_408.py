#!/usr/bin/env python3
"""Small stdin/file facade over the existing campaign turn owners.

Requests are immutable private retry inputs. Results are immutable owner outputs;
image-handoff reopens a saved result without invoking a mutation owner.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import sys
from datetime import date
from functools import partial
from pathlib import Path
from typing import Any

import morning_campaign_answer_408 as answer
import morning_campaign_failure_terminal_408 as failure
import campaign_terminal_capture_408 as episode_terminal
import morning_campaign_runtime_408 as runtime
from study_commit_followup_408 import study_commit_boundary
from learning_evidence_408 import normalize_turns as normalize_learning_turns

REQUEST_SCHEMA = "morning-campaign-turn-request-v1"
RESULT_SCHEMA = "morning-campaign-turn-result-v1"
DEFAULT_REPO = Path(__file__).resolve().parents[1]
COMMANDS = (
    "answer-choice",
    "finalize-reason",
    "append-turn",
    "finalize-appended-and-next",
    "finish-item",
    "close-unresolved",
)
MAX_INPUT_BYTES = failure.MAX_JSON_BYTES
ROOT_FIELDS = ("repo_root", "runtime_root", "package_root", "private_root", "draft_root")


class TurnError(RuntimeError):
    pass


def encoded(value: object) -> bytes:
    return (json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2) + "\n").encode("utf-8")


def sha(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


def read_json(path: Path) -> dict[str, Any]:
    return failure._read_json(path, "turn_file", MAX_INPUT_BYTES)


def roots(**overrides: Any) -> dict[str, str]:
    repo = Path(overrides.get("repo_root") or DEFAULT_REPO).expanduser().resolve()
    packages = Path(overrides.get("package_root") or failure.DEFAULT_PACKAGE_ROOT).expanduser().resolve()
    values = {
        "repo_root": repo,
        "runtime_root": overrides.get("runtime_root") or repo / ".private/morning-campaign-runtime",
        "package_root": packages,
        "private_root": overrides.get("private_root") or packages,
        "draft_root": overrides.get("draft_root") or packages / ".campaign-failure-drafts-v1",
    }
    result = {key: str(Path(value).expanduser().resolve()) for key, value in values.items()}
    if any(Path(value) == Path(value).parent for value in result.values()):
        raise TurnError("root_too_broad")
    return result


def expand(command: str, body: dict[str, Any], locations: dict[str, str]) -> dict[str, Any]:
    if command not in COMMANDS:
        raise TurnError("command_invalid")
    common = {"display_receipt_sha256", "idempotency_key"}
    identity = {"schema", "display_receipt_path", "run_id", "item_id"}
    if command in {"answer-choice", "finalize-reason"}:
        required = common | {"response", "recognized_original", "response_seconds"}
        if command == "finalize-reason":
            required |= {"choice_idempotency_key", "reasoning_result", "prompt_dependency"}
        optional = {"event_time", "confidence", "learning_turns"}
        if command == "answer-choice":
            optional |= {"reasoning_result", "prompt_dependency"}
        schema = None
    elif command in {"append-turn", "finalize-appended-and-next"}:
        required = common | {"expected_prior_turn_count", "expected_prior_trace_sha256", "new_turns"}
        optional = set()
        schema = failure.APPEND_INPUT_SCHEMA
        if command == "finalize-appended-and-next":
            required |= {"corrected_response", "reasoning_result", "prompt_dependency", "confidence", "frozen_feedback", "user_error_entry", "user_error_provenance", "first_action", "first_action_provenance"}
            optional |= {"missing_fields", "attachments"}
            schema = failure.TERMINAL_APPEND_INPUT_SCHEMA
    else:
        required = common | {
            "expected_prior_turn_count", "expected_prior_trace_sha256",
            "new_turns", "attachments", "missing_fields",
        }
        optional = {"event_time"}
        schema = episode_terminal.TERMINAL_INPUT_SCHEMA
        if command == "finish-item":
            required |= {
                "terminal_state", "final_response", "reasoning_result",
                "prompt_dependency",
            }
            optional |= {"frozen_feedback", "navigation_policy"}
        else:
            required |= {"navigation_policy"}
    optional |= {"teaching_concepts", "teaching_mechanisms"}
    mechanisms = body.get("teaching_mechanisms")
    if mechanisms is not None and (not isinstance(mechanisms, list) or len(mechanisms) > 8
            or any(not isinstance(t, str) or not t.strip() or len(t) > 200 for t in mechanisms)):
        raise TurnError("teaching_mechanisms_invalid")
    teaching_concepts = body.get("teaching_concepts")
    if teaching_concepts is not None:
        from knowledge_learning_records_408 import canonical_knowledge_points
        if (not isinstance(teaching_concepts, list) or len(teaching_concepts) > 16
                or any(not isinstance(code, str) or canonical_knowledge_points(code) != [code]
                       for code in teaching_concepts)):
            raise TurnError("teaching_concepts_must_be_canonical_codes")
    if set(body) - required - optional - identity:
        raise TurnError("unknown_input_fields:" + ",".join(sorted(set(body) - required - optional - identity)))
    if required - set(body):
        raise TurnError("missing_input_fields:" + ",".join(sorted(required - set(body))))
    digest = answer._safe_sha(body["display_receipt_sha256"], "display_receipt_sha256")
    path = Path(locations["private_root"]) / "turns" / f"{digest}.json"
    _, display, _ = answer.reopen_campaign_display_receipt(path, digest)
    saving = runtime.saving_contract_for_run(locations["runtime_root"], display["run_id"])
    if command == "append-turn" and saving["episode_capture_every_item"]:
        schema = episode_terminal.APPEND_INPUT_SCHEMA
    if command in {"finish-item", "close-unresolved"} and not saving["episode_capture_every_item"]:
        raise TurnError("episode_terminal_requires_saving_contract_v2")
    derived = {"display_receipt_path": str(path), "run_id": display["run_id"], "item_id": display["item_id"], "schema": schema}
    for key in identity & set(body):
        if body[key] != derived[key]:
            raise TurnError("input_identity_conflict:" + key)
    learning_turns = normalize_learning_turns(body["learning_turns"]) if "learning_turns" in body else None
    payload = {key: value for key, value in body.items() if key not in identity and key not in {"learning_turns", "teaching_concepts", "teaching_mechanisms"}}
    payload["display_receipt_path"] = str(path)
    if schema:
        payload.update({key: derived[key] for key in ("schema", "run_id", "item_id")})
        if command == "finalize-appended-and-next":
            payload.setdefault("missing_fields", [])
            payload.setdefault("attachments", [])
        if schema in {failure.APPEND_INPUT_SCHEMA, failure.TERMINAL_APPEND_INPUT_SCHEMA}:
            failure._normalize_append_input(payload, terminal=command == "finalize-appended-and-next")
        elif schema == episode_terminal.APPEND_INPUT_SCHEMA:
            episode_terminal._turns(payload["new_turns"])
        else:
            if command == "close-unresolved":
                payload.update(
                    terminal_state="unresolved",
                    final_response=None,
                    reasoning_result="not_observed",
                    prompt_dependency="not_observed",
                )
            else:
                payload.setdefault("navigation_policy", "publish_successor")
    else:
        answer._normalize_inputs(**{key: payload.get(key) for key in ("response", "confidence", "recognized_original", "response_seconds")})
        if command == "answer-choice":
            answer._initial_semantics(payload.get("reasoning_result"), payload.get("prompt_dependency"))
        if command == "finalize-reason":
            if payload["reasoning_result"] not in answer.REASONING_RESULTS or payload["prompt_dependency"] not in answer.PROMPT_DEPENDENCIES:
                raise TurnError("reason_semantics_invalid")
            answer._safe_id(payload["choice_idempotency_key"], "choice_idempotency_key")
    answer._safe_id(payload["idempotency_key"], "idempotency_key")
    request = {"schema": REQUEST_SCHEMA, "command": command, "roots": locations, "run_id": display["run_id"], "item_id": display["item_id"], "input": payload}
    if teaching_concepts is not None:
        request["teaching_concepts"] = list(dict.fromkeys(teaching_concepts))
    if mechanisms is not None:
        request["teaching_mechanisms"] = list(dict.fromkeys(mechanisms))
    if learning_turns is not None:
        request["learning_turns"] = learning_turns
    return request


def _episode_input(payload: dict[str, Any]) -> dict[str, Any]:
    # The v2 episode owner derives run/item identity from the display receipt and
    # rejects caller-supplied run_id/item_id added by this facade for the failure owner.
    return {key: value for key, value in payload.items() if key not in {"run_id", "item_id"}}


def dispatch(request: dict[str, Any]) -> dict[str, Any]:
    command, locations, payload = request["command"], request["roots"], request["input"]
    common = {key: locations[key] for key in ("repo_root", "runtime_root")}
    if command in {"answer-choice", "finalize-reason"}:
        owner = answer.answer_choice if command == "answer-choice" else answer.finalize_reason
        return owner(**common, **payload, display_publisher_kwargs={"private_root": locations["private_root"]})
    common["draft_root"] = locations["draft_root"]
    if command == "append-turn":
        if payload.get("schema") == episode_terminal.APPEND_INPUT_SCHEMA:
            return episode_terminal.append_turns(
                _episode_input(payload),
                runtime_root=locations["runtime_root"],
                draft_root=Path(locations["draft_root"]) / "episode-v2",
            )
        return failure.append_turns(payload, **common)
    if command in {"finish-item", "close-unresolved"}:
        from morning_campaign_display_408 import publish_current_display
        return episode_terminal.finish_item(
            _episode_input(payload),
            repo_root=locations["repo_root"],
            runtime_root=locations["runtime_root"],
            package_root=locations["package_root"],
            draft_root=Path(locations["draft_root"]) / "episode-v2",
            display_publisher=partial(
                publish_current_display, private_root=locations["private_root"]
            ),
        )
    # The existing failure owner accepts a publisher, rather than publisher kwargs.
    from morning_campaign_display_408 import publish_current_display
    return failure.finalize_appended_and_next(
        payload, **common, package_root=locations["package_root"],
        display_publisher=partial(publish_current_display, private_root=locations["private_root"]),
    )


def image_paths(outcome: dict[str, Any], request: dict[str, Any]) -> list[str]:
    nxt = outcome.get("next_display")
    if not nxt or outcome.get("status") == "blocked" or outcome.get("frontier_advanced") is not True:
        return []
    if outcome.get("capture_required") is True and outcome.get("capture_verified") is not True:
        raise TurnError("capture_verification_required")
    if not isinstance(nxt, dict) or nxt.get("schema") not in {"morning-campaign-display-v1", "morning-campaign-display-public-v1"} or nxt.get("status") not in {"display_published", "already_published"} or nxt.get("formal_write_count") != 0:
        raise TurnError("next_display_not_authorized")
    if outcome.get("run_id") != request["run_id"] or outcome.get("item_id") != request["item_id"]:
        raise TurnError("owner_result_identity_drift")
    if nxt.get("run_id") != request["run_id"] or nxt.get("item_id") == request["item_id"]:
        raise TurnError("next_display_identity_drift")
    digest = answer._safe_sha(nxt.get("display_receipt_sha256"), "next_display_sha256")
    path = Path(request["roots"]["private_root"]) / "turns" / f"{digest}.json"
    if nxt.get("display_receipt_path") != str(path) or nxt.get("display_receipt_locator") != "current-question-turn://sha256/" + digest:
        raise TurnError("next_display_receipt_path_drift")
    _, display, _ = answer.reopen_campaign_display_receipt(path, digest)
    for key in ("run_id", "item_id", "learner_surface"):
        if display.get(key) != nxt.get(key):
            raise TurnError("next_display_binding_drift:" + key)
    _, _, state, current = runtime.reopen_verified_frontier(request["roots"]["runtime_root"], request["run_id"])
    if state.get("status") not in {"active", "paused", "failure_pending_capture"} or not current or current.get("item_id") != display["item_id"]:
        raise TurnError("next_display_stale_frontier")
    paths = []
    for asset in display.get("question_assets", []):
        candidate = Path(asset["path"])
        if not candidate.is_absolute() or ".." in candidate.parts or any(part.is_symlink() for part in (candidate, *candidate.parents)):
            raise TurnError("question_image_path_unsafe")
        if not candidate.is_file() or candidate.stat().st_size != asset["size_bytes"] or not 0 < asset["size_bytes"] <= failure.MAX_ATTACHMENT_BYTES:
            raise TurnError("question_image_size_invalid")
        raw = candidate.read_bytes()
        if len(raw) != asset["size_bytes"] or sha(raw) != asset["sha256"]:
            raise TurnError("question_image_hash_drift")
        if not (raw.startswith(b"\x89PNG\r\n\x1a\n") or raw.startswith(b"\xff\xd8\xff") or raw.startswith((b"GIF87a", b"GIF89a")) or (raw.startswith(b"RIFF") and raw[8:12] == b"WEBP")):
            raise TurnError("question_image_format_invalid")
        paths.append(str(candidate))
    return paths


def present(saved: dict[str, Any], result_file: Path) -> dict[str, Any]:
    request_file = Path(saved["request_file"])
    request = read_json(request_file)
    if sha(request_file.read_bytes()) != saved["request_sha256"]:
        raise TurnError("saved_request_hash_drift")
    outcome = saved["outcome"]
    public = {**outcome, "request_file": str(request_file), "result_file": str(result_file), "next_image_paths": [], "image_preload_status": "not_applicable"}
    if request.get("learning_turns"):
        public.update(
            light_evidence_saved=(
                outcome.get("conversation_draft_saved") is True
                if outcome.get("capture_required") is True
                and outcome.get("capture_verified") is not True
                else outcome.get("status") != "blocked"
            ),
            learning_turn_count=len(request["learning_turns"]),
        )
    try:
        paths = image_paths(outcome, request)
        public["next_image_paths"] = paths
        if paths:
            public["image_preload_status"] = "ready"
    except (TurnError, answer.CampaignAnswerError, runtime.CampaignRuntimeError, OSError) as exc:
        public.update(image_preload_status="blocked", image_preload_reason=str(exc))
    has_attempt = request.get("command") == "answer-choice" or any(
        item.get("role") == "user" and item.get("kind") in {"answer", "reasoning"}
        for item in request.get("input", {}).get("new_turns", []))
    if request.get("command") in {"answer-choice", "append-turn"} and has_attempt and outcome.get("status") != "blocked":
        public["current_item_personalization"] = response_personalization(request)
    return public


def local_records_markdown(teaching: dict[str, Any]) -> str:
    """Render already prepared evidence without inferring errors or mastery."""
    paragraphs = []
    for record in teaching.get("records", []):
        uri = record.get("obsidian_uri")
        if not uri:
            continue
        label = record.get("concept_name") or record["knowledge_point"]
        summary = record.get("short_summary") or "暂无个人学习记录。"
        link = record.get("markdown_link") or f"[{label}（{record['knowledge_point']}）]({uri})"
        paragraphs.append(f"{link}\n\n{summary}")
    return "本地记录\n\n" + "\n\n".join(paragraphs) if paragraphs else ""


def response_personalization(request: dict[str, Any]) -> dict[str, Any]:
    """Attach current-point evidence to the hot path, outside the saved receipt."""
    from exam_tutoring_context_408 import query_context, ExamTutoringContextError
    from knowledge_learning_records_408 import canonical_knowledge_points
    try:
        payload = request["input"]
        _, display, _ = answer.reopen_campaign_display_receipt(
            payload["display_receipt_path"], payload["display_receipt_sha256"])
        concepts = list(request.get("teaching_concepts") or [])
        if display.get("surface_kind") == "verified_variant":
            raw = Path(display["surface_object_path"]).read_bytes()
            if sha(raw) != display["surface_object_sha256"]:
                raise TurnError("current_surface_hash_mismatch")
            surface = json.loads(raw)
            for field in ("main_knowledge", "knowledge_points", "review_target_concept_ids"):
                concepts.extend(canonical_knowledge_points(surface.get(field)))
            if not concepts:
                return {"status": "unavailable", "reason": "variant_concept_binding_missing"}
        query = {"schema": "exam_tutoring_context_query_v1", "stage": "post_answer",
                 "knowledge_points": list(dict.fromkeys(concepts)), "max_profiles": 0, "max_bytes": 8192}
        if request.get("teaching_mechanisms"):
            query["mechanism_terms"] = request["teaching_mechanisms"]
        if not concepts:
            query["formal_id"] = display.get("formal_node_id")
        teaching = query_context(request["roots"]["repo_root"], query,
                                 max_bytes=8192, teaching_view="after_attempt")
        return {"status": teaching["status"], "item_id": display["item_id"],
                "teaching_context": teaching, "local_records_markdown": local_records_markdown(teaching),
                "response_instruction": "个人摘要供内部选择讲解重点，当前真实作答决定判断；直接纠正当前断点，不默认展示历史段落或知识点链接。已有本轮所需摘要时直接复用；仅缺少影响当前解释的知识点时补一次有界查询。按当前Skill区分正确、缺理由和实际纠错分支。",
                "write_count": 0}
    except (OSError, KeyError, ValueError, TurnError, answer.CampaignAnswerError, ExamTutoringContextError) as exc:
        return {"status": "unavailable", "reason": str(exc), "write_count": 0}


@study_commit_boundary
def run(command: str, body: dict[str, Any], locations: dict[str, str]) -> dict[str, Any]:
    request = expand(command, body, locations)
    journal = Path(locations["private_root"]) / ".turn-runner-v1"
    key = sha(encoded([request["run_id"], request["input"]["idempotency_key"]]))
    request_file = journal / "requests" / f"{key}.json"
    raw = encoded(request)
    with failure._lock(journal, key):
        if request_file.exists():
            if request_file.read_bytes() != raw:
                raise TurnError("idempotency_input_conflict")
        else:
            failure._atomic_write(request_file, raw)
        try:
            outcome = dispatch(request)
            if (
                request.get("learning_turns")
                and outcome.get("capture_required") is True
                and outcome.get("capture_verified") is not True
                and runtime.saving_contract_for_run(
                    locations["runtime_root"], request["run_id"]
                )["episode_capture_every_item"]
                and runtime.status_run(
                    locations["runtime_root"], request["run_id"]
                ).get("status") == "episode_pending_capture"
            ):
                buffered = episode_terminal.sync_turns(
                    display_receipt_path=request["input"]["display_receipt_path"],
                    display_receipt_sha256=request["input"]["display_receipt_sha256"],
                    complete_turns=request["learning_turns"],
                    idempotency_key=request["input"]["idempotency_key"] + ":turns",
                    runtime_root=locations["runtime_root"],
                    draft_root=Path(locations["draft_root"]) / "episode-v2",
                )
                outcome = {
                    **outcome,
                    "turn_count": buffered["turn_count"],
                    "trace_sha256": buffered["trace_sha256"],
                    "conversation_draft_saved": True,
                }
        except (
            answer.CampaignAnswerError,
            failure.CampaignFailureTerminalError,
            episode_terminal.CampaignTerminalCaptureError,
            runtime.CampaignRuntimeError,
        ) as exc:
            outcome = {"status": "blocked", "reason": str(exc)}
        saved = {"schema": RESULT_SCHEMA, "request_file": str(request_file), "request_sha256": sha(raw), "outcome": outcome}
        result_raw = encoded(saved)
        result_file = journal / "results" / f"{sha(result_raw)}.json"
        if not result_file.exists():
            failure._atomic_write(result_file, result_raw)
    return present(saved, result_file)


def recent_learning_turns(draft: dict[str, Any], draft_path: Path) -> dict[str, Any]:
    """Return an exact bounded suffix; never rewrite the saved conversation."""
    turns = draft["turns"]
    recent = []
    for row in reversed(turns[-4:]):
        candidate = [row, *recent]
        if len(encoded(candidate)) > 6000:
            break
        recent = candidate
    return {"recent_turns": recent, "total_turn_count": draft["turn_count"],
            "omitted_turn_count": len(turns) - len(recent),
            "complete": len(recent) == len(turns), "draft_path": str(draft_path)}


def teaching_context(handoff_path: str | Path, expected_sha256: str, locations: dict[str, str],
                     *, known_version: str | None = None,
                     concept_help: bool = False) -> dict[str, Any]:
    """Read a ready display, saved answer, cursor and teaching summary in one call."""
    handoff_file = Path(handoff_path).expanduser().resolve()
    expected = answer._safe_sha(expected_sha256, "handoff_sha256")
    if sha(handoff_file.read_bytes()) != expected:
        raise TurnError("handoff_hash_mismatch")
    handoff = read_json(handoff_file)
    return _teaching_context(handoff, locations, known_version=known_version,
                             concept_help=concept_help)


def current_teaching_context(review_date: str, locations: dict[str, str], *,
                             concept_help: bool = False) -> dict[str, Any]:
    """Recover only the verified current display and cursor, without a handoff."""
    try:
        if date.fromisoformat(review_date).isoformat() != review_date:
            raise ValueError
    except ValueError as exc:
        raise TurnError("review_date_invalid") from exc
    pointer_path = (Path(locations["repo_root"]) / ".private/morning-campaign-control/current-display"
                    / (review_date + ".json"))
    pointer = read_json(pointer_path)
    if not pointer.get("run_id"):
        raise TurnError("current_pointer_has_no_run")
    return _teaching_context({"run_id": pointer["run_id"],
                              "current_display_pointer": str(pointer_path)},
                             locations, review_date=review_date, concept_help=concept_help)


def prepared_explanation_materials(handoff: dict[str, Any], entry: dict[str, Any]) -> dict[str, Any]:
    """Read only the two hash-bound current-unit texts, never search for a card."""
    refs = entry.get("private_after_attempt") or {}
    materials = {}
    for name in ("teaching_path", "solution_path"):
        binding = refs.get(name)
        if not binding:
            continue
        try:
            root = Path(handoff["output_root"]).resolve(strict=True)
            ref = Path(binding["relative_path"])
            if ref.is_absolute() or ".." in ref.parts:
                raise ValueError("prepared_material_path_unsafe")
            path = (root / ref).resolve(strict=True)
            path.relative_to(root)
            if path.stat().st_size > 16384:
                raise ValueError("prepared_material_too_large")
            raw = path.read_bytes()
            if len(raw) != binding["byte_count"] or sha(raw) != binding["sha256"]:
                raise ValueError("prepared_material_hash_mismatch")
            materials[name] = {"status": "verified", "path": str(path),
                               "sha256": sha(raw), "text": raw.decode("utf-8")}
        except (OSError, KeyError, ValueError, TypeError) as exc:
            materials[name] = {"status": "unavailable", "reason": str(exc)}
    return {"usage": "assistant_internal_keep_current_answer_protected",
            "status": ("available" if materials and all(
                row["status"] == "verified" for row in materials.values())
                else "incomplete" if materials else "not_bound"),
            "materials": materials}


def _teaching_context(handoff: dict[str, Any], locations: dict[str, str], *,
                      known_version: str | None = None,
                      review_date: str | None = None,
                      concept_help: bool = False) -> dict[str, Any]:
    run_id = handoff.get("run_id")
    if not run_id:
        return {"status": "supplemental_route", "write_count": 0,
                "reason": "handoff_has_no_campaign_run"}
    runtime_root = Path(locations["runtime_root"])
    with runtime.verified_frontier_guard(runtime_root, run_id) as (_directory, run, state, item):
        if review_date is not None and run["campaign_date"] != review_date:
            raise TurnError("context_campaign_date_drift")
        if item is None or state["status"] in {"ready", "preparing", "completed", "carried_over"}:
            return {"status": state["status"], "run_id": run_id, "write_count": 0,
                    "next_action": "use_morning_control_for_state_transition"}
        pointer_path = handoff.get("current_display_pointer")
        if not pointer_path:
            pointer_path = str(Path(locations["repo_root"]) / ".private/morning-campaign-control/current-display"
                               / (str(run["campaign_date"]) + ".json"))
        pointer = read_json(Path(pointer_path))
        digest = answer._safe_sha(pointer["display_receipt_sha256"], "display_receipt_sha256")
        receipt_path = Path(locations["private_root"]) / "turns" / (digest + ".json")
        _, display, _ = answer.reopen_campaign_display_receipt(receipt_path, digest)
        if display["run_id"] != run_id or pointer.get("run_id") != run_id:
            raise TurnError("context_run_identity_drift")
        for key in ("item_id", "true_item_id", "surface_sha256", "grading_sha256", "campaign_item_commitment_sha256"):
            if display.get(key) != item.get(key):
                raise TurnError("context_frontier_drift:" + key)
        saving = runtime.saving_contract_for_run(runtime_root, run_id)
        if not saving["episode_capture_every_item"]:
            return {"status": "legacy_route", "run_id": run_id, "item_id": item["item_id"], "write_count": 0}
        draft_path, draft = episode_terminal._draft(draft_root=Path(locations["draft_root"]) / "episode-v2",
                                           display=display, display_sha=digest)
        observation = (state.get("pending_episode") or {}).get("payload") or {}
        concepts = []
        entry = {}
        teaching = {"status": "unavailable", "reason": "current_concept_binding_missing", "records": []}
        index_path = handoff.get("teaching_index_path")
        if index_path:
            raw = Path(index_path).read_bytes()
            if sha(raw) != handoff.get("teaching_index_sha256"):
                raise TurnError("teaching_index_hash_mismatch")
            index = json.loads(raw)
            entry = index.get(display.get("formal_node_id")) or {}
            concepts = entry.get("review_target_concept_ids") or []
            if not concepts and display.get("surface_kind") != "verified_variant":
                concepts = entry.get("source_knowledge_points") or []
        if concepts or concept_help:
            from exam_tutoring_context_408 import query_context
            attempted = observation.get("first_response") is not None or any(
                item.get("role") == "user" and item.get("kind") in {"answer", "reasoning"}
                for item in draft.get("turns", []))
            view = "direct" if concept_help else "after_attempt" if attempted else "protected"
            query = {
                "schema": "exam_tutoring_context_query_v1",
                "stage": "post_answer" if view != "protected" else "pre_answer",
                "knowledge_points": concepts, "weak_knowledge_points": [],
                "max_profiles": 0, "max_bytes": 8192,
            }
            if not concepts:
                query["formal_id"] = display.get("formal_node_id")
            teaching = query_context(locations["repo_root"], query,
                max_bytes=8192, teaching_view=view, known_version=known_version)
        cursor = {"display_receipt_sha256": digest,
                  "expected_prior_turn_count": draft["turn_count"],
                  "expected_prior_trace_sha256": draft["trace_sha256"]}
        return {
            "schema": "morning-teaching-context-408-v1", "status": "ready_for_teaching",
            "run_id": run_id, "item_id": item["item_id"], "runtime_status": state["status"],
            "resume_only": True, "write_required_now": False, "write_count": 0,
            "next_action": "respond_from_current_evidence_before_any_new_recording",
            "learner_surface": display["learner_surface"],
            "question_assets": display.get("question_assets") or [],
            "question_provenance": display.get("question_provenance") or {},
            "saving_mode": "episode_capture_every_item",
            "first_response": observation.get("first_response"),
            "first_result": observation.get("first_result"),
            "reasoning_result": observation.get("initial_reasoning_result", "not_observed"),
            "prompt_dependency": observation.get("initial_prompt_dependency", "not_observed"),
            "observed_date": observation.get("observed_date"),
            "draft_cursor": cursor,
            "saved_learning": recent_learning_turns(draft, draft_path),
            "answer_input_base": {"display_receipt_sha256": digest,
                                  "recognized_original": False, "response_seconds": None},
            "knowledge_points": concepts, "teaching_context": teaching,
            **({"concept_help": True, "answer_protection": "preserve_current_learning_boundary",
                "prepared_explanation": prepared_explanation_materials(handoff, entry)}
               if concept_help else {}),
            "local_records_markdown": local_records_markdown(teaching),
            "progress": {"completed_items": state.get("completed_items", 0),
                         "item_count": state.get("item_count")},
        }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=(*COMMANDS, "image-handoff", "context"))
    parser.add_argument("--input-json")
    parser.add_argument("--handoff")
    parser.add_argument("--handoff-sha256")
    parser.add_argument("--review-date")
    parser.add_argument("--known-context-version")
    parser.add_argument("--concept-help", action="store_true")
    for field in ROOT_FIELDS:
        parser.add_argument("--" + field.replace("_", "-"))
    args = parser.parse_args(argv)
    try:
        overrides = {key: getattr(args, key) for key in ROOT_FIELDS if getattr(args, key) is not None}
        if args.concept_help and args.command != "context":
            raise TurnError("concept_help_requires_readonly_context")
        if args.command == "context":
            if args.review_date:
                if args.handoff or args.handoff_sha256 or args.known_context_version:
                    raise TurnError("context_sources_conflict")
                result = current_teaching_context(args.review_date, roots(**overrides),
                                                 concept_help=args.concept_help)
            else:
                if not args.handoff or not args.handoff_sha256:
                    raise TurnError("handoff_and_hash_required")
                result = teaching_context(args.handoff, args.handoff_sha256, roots(**overrides),
                                          known_version=args.known_context_version,
                                          concept_help=args.concept_help)
            print(json.dumps(result, ensure_ascii=False, sort_keys=True, indent=2))
            return 0
        if not args.input_json:
            raise TurnError("input_json_required")
        if args.input_json == "-":
            raw = sys.stdin.buffer.read(MAX_INPUT_BYTES + 1)
            if len(raw) > MAX_INPUT_BYTES:
                raise TurnError("input_too_large")
            body = json.loads(raw)
        else:
            body = read_json(Path(args.input_json).expanduser().resolve())
        if not isinstance(body, dict):
            raise TurnError("input_object_required")
        if args.command == "image-handoff":
            path = Path(args.input_json).expanduser().resolve()
            if body.get("schema") != RESULT_SCHEMA or sha(path.read_bytes()) != path.stem or overrides:
                raise TurnError("saved_result_invalid")
            result = present(body, path)
        elif body.get("schema") == REQUEST_SCHEMA:
            if body.get("command") != args.command:
                raise TurnError("saved_request_command_conflict")
            if any(str(Path(value).expanduser().resolve()) != body["roots"].get(key) for key, value in overrides.items()):
                raise TurnError("saved_request_roots_conflict")
            retry_input = dict(body["input"])
            if "learning_turns" in body:
                retry_input["learning_turns"] = body["learning_turns"]
            result = run(args.command, retry_input, body["roots"])
        else:
            result = run(args.command, body, roots(**overrides))
    except (
        TurnError,
        answer.CampaignAnswerError,
        failure.CampaignFailureTerminalError,
        episode_terminal.CampaignTerminalCaptureError,
        runtime.CampaignRuntimeError,
        OSError,
        ValueError,
        KeyError,
        TypeError,
    ) as exc:
        result = {"status": "blocked", "reason": str(exc), "next_image_paths": [], "image_preload_status": "not_applicable"}
    print(json.dumps(result, ensure_ascii=False, sort_keys=True, indent=2))
    return 2 if result.get("status") == "blocked" else 0


if __name__ == "__main__":
    raise SystemExit(main())
