#!/usr/bin/env python3
"""Current-turn, wrong-answer-only display adapter. Never grades or writes learning data.

The tutor supplies its current diagnosis and the already permitted teaching stage.
This adapter reads the exact cached lesson and opens only its own iPad WebView.
Ordinary queries, correct answers, and a missing iPad never open another display.
"""
from __future__ import annotations

import argparse
import fcntl
import json
import os
from pathlib import Path
import subprocess
import sys
import time
from urllib.parse import parse_qs, urlsplit

import study_visuals_408 as cache

import runtime_paths_408 as _paths

POLICY_REL = cache.ROOT_REL / "display-policy.json"
STATE_REL = cache.ROOT_REL / "screen"
HOST_REL = cache.RUNTIME_REL / "native/bin/StudyVisualDisplay"


def policy(repo):
    path = Path(repo) / POLICY_REL
    if not path.is_file() or path.is_symlink():
        return {"enabled": False}
    value = cache.read_json(path)
    if (value.get("schema") != "study-visual-display-policy-v1"
            or value.get("trigger") not in {"current_wrong_answer", "current_explanation_with_3d_benefit"}
            or value.get("target_display_name") != "OpenDisplay — iPad"):
        return {"enabled": False}
    return value


def teaching_contract(repo, question_id):
    """Machine-readable instruction to the current tutor; no display side effects."""
    if not policy(repo).get("enabled"):
        return None
    return {
        "schema": "study-visual-automatic-teaching-v1",
        "trigger": "current_explanation_with_3d_benefit",
        "target": "OpenDisplay — iPad",
        "default_question_presentation": "original_question_only",
        "correct_answer": "continue_without_3d",
        "wrong_answer": "choose_tutor_presentation_from_current_explanation",
        "benefit_gate": "当前讲题会话按题面和卡点选择文字、二维图或三维模型，传 tutor_presentation。普通选择在本地完成。",
        "tutor_presentation": ["text", "two_d", "three_d"],
        "current_question_id": question_id,
        "stage_policy": "Use only the current permitted hint or solution; after_attempt alone is not solution authorization.",
        "decision_basis": "Current answer and reasoning, never historical mastery or option letter alone.",
        "standalone_entry": str(Path(repo) / "scripts/study_visual_display_408.py"),
        "example_scope": "仅当本次讲题者已选择三维时使用以下示例；文字或二维传相应的本地选择。",
        "standalone_arguments": ["--question-id", question_id, "--decision", "wrong", "--stage", "hint",
                                 "--tutor-presentation", "three_d"],
        "morning_entry": "d_supplement_learning_408.py --operation visual-feedback --input-json -",
        "existing_one_call_helper_operation": "d-supplement-context",
        "existing_one_call_helper_input_extension": {"view": "after_attempt", "teaching_screen": {"decision": "wrong", "stage": "hint", "hint_level": 1, "tutor_presentation": "three_d"}},
        "existing_one_call_helper_receipt": "verification.automatic_3d_display",
        "current_semantics_required": ["teaching_goal", "current_difficulty"],
        "morning_payload_extension": {"teaching_screen": {"decision": "wrong", "stage": "hint", "hint_level": 1, "tutor_presentation": "three_d"}},
        "user_click_required": False,
        "unsupported_or_disconnected": "continue_chat_and_report_display_unavailable; never_use_another_screen",
        "learning_write_count": 0,
    }


def _state(repo):
    root = Path(repo).resolve() / STATE_REL
    root.mkdir(parents=True, exist_ok=True)
    if any(p.is_symlink() for p in (root, *root.parents)):
        raise ValueError("screen_state_symlink_forbidden")
    os.chmod(root, 0o700)
    return root


def _optional_json(path):
    try:
        return cache.read_json(path)
    except (OSError, ValueError):
        return {}


def _host_alive(repo, root):
    value = _optional_json(root / "process.json")
    pid = value.get("pid")
    if not isinstance(pid, int) or pid <= 1:
        return False
    try:
        os.kill(pid, 0)
        cmd = subprocess.run(["/bin/ps", "-ww", "-p", str(pid), "-o", "command="],
                             capture_output=True, text=True, timeout=2).stdout.strip()
    except (OSError, subprocess.SubprocessError):
        return False
    return str(Path(repo).resolve() / HOST_REL) in cmd and str(root / "request.json") in cmd


def _start_host(repo, root):
    binary = Path(repo).resolve() / HOST_REL
    if not binary.is_file() or binary.is_symlink() or not os.access(binary, os.X_OK):
        raise ValueError("ipad_display_host_not_built")
    # This is the visible lesson window's process, not a login item or service.
    with (root / "host.log").open("ab") as log:
        proc = subprocess.Popen([str(binary), "--request-file", str(root / "request.json"),
                                 "--status-file", str(root / "status.json")],
                                stdin=subprocess.DEVNULL, stdout=log, stderr=log,
                                start_new_session=True, close_fds=True)
    cache.atomic_json(root / "process.json", {"pid": proc.pid, "binary": str(binary),
                                               "binary_sha256": cache.file_sha(binary)})
    return proc.pid


def _wait_status(root, request_id, timeout):
    deadline = time.monotonic() + timeout
    terminal = {"shown", "hidden", "exited", "error", "display_unavailable", "closed"}
    while time.monotonic() < deadline:
        value = _optional_json(root / "status.json")
        if value.get("request_id") == request_id and value.get("status") in terminal:
            return value
        time.sleep(0.1)
    return {"status": "display_pending", "request_id": request_id,
            "reason": "native_window_has_not_confirmed_rendering"}


def _send(repo, request, *, wait_seconds=15):
    root = _state(repo)
    with (root / ".request.lock").open("a+b") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        alive = _host_alive(repo, root)
        old = _optional_json(root / "request.json")
        status = _optional_json(root / "status.json")
        if (alive and old.get("request_id") == request["request_id"]
                and status.get("request_id") == request["request_id"]
                and status.get("status") == "shown"):
            return {**status, "reused_existing_window": True}
        if request["action"] in {"hide", "exit"} and not alive:
            return {"status": "not_open", "request_id": request["request_id"], "window_opened": False}
        # Clear confirmation before a previously used request id can be replayed.
        cache.atomic_json(root / "status.json", {"request_id": request["request_id"], "status": "pending"})
        cache.atomic_json(root / "request.json", request)
        if not alive:
            _start_host(repo, root)
        return _wait_status(root, request["request_id"], wait_seconds)


def dismiss(repo, *, reason="continue_normal_chat", action="hide"):
    if not policy(repo).get("enabled"):
        return {"status": "disabled", "learning_write_count": 0}
    if action not in {"hide", "exit"}:
        raise ValueError("invalid_display_action")
    request = {"schema": "study-visual-screen-request-v1", "action": action,
               "target_display_name": policy(repo)["target_display_name"], "reason": reason}
    request["request_id"] = cache.digest(request)
    request["issued_at"] = time.time()
    return {**_send(repo, request, wait_seconds=4), "learning_write_count": 0}


def hide_previous_question(repo, question_id):
    """Only an already-open owned lesson can be hidden; never creates a window."""
    if not policy(repo).get("enabled"):
        return None
    root = Path(repo).resolve() / STATE_REL
    request = _optional_json(root / "request.json")
    if request.get("action") == "show" and request.get("question_id") != question_id:
        return dismiss(repo, reason="next_question")
    return None


def feedback(repo, question_id, decision, *, stage="hint", hint_level=1,
             expected_surface_sha256=None, focus_frame=None, wait_seconds=15,
             teaching_goal=None, current_difficulty="", solution_authorized=False,
             tutor_presentation=None):
    """The current tutor has already judged the answer; no grading is inferred here."""
    config = policy(repo)
    if not config.get("enabled"):
        return {"status": "disabled", "learning_write_count": 0}
    if decision not in {"wrong", "correct", "not_evaluated"}:
        raise ValueError("current_teacher_decision_required")
    if decision != "wrong":
        result = dismiss(repo, reason="correct_answer" if decision == "correct" else "normal_chat")
        return {**result, "decision": decision, "triggered": False}
    bridge = str(_paths.bridge_root())
    if bridge not in sys.path:
        sys.path.insert(0, bridge)
    from study_visual_dispatch import dispatch
    result = dispatch({'subject': '408', 'current_question_id': question_id,
        'tutor_presentation': tutor_presentation,
        'teaching_goal': teaching_goal or '', 'current_difficulty': current_difficulty,
        'permitted_stage': stage, 'intent': 'hint' if stage == 'hint' else 'explanation',
        'answer_status': 'wrong', 'solution_authorized': solution_authorized,
        'expected_surface_sha256': expected_surface_sha256, 'focus_frame': focus_frame,
        'hint_level': hint_level}, show=True)
    return {**result, 'triggered': result.get('window_opened') is True}


def present_verified(repo, question_id, *, stage="hint", hint_level=1,
                     expected_surface_sha256=None, focus_frame=None, wait_seconds=15):
    """Display actuator, used only after an explicit authorized presentation decision."""
    config = policy(repo)
    if not config.get('enabled'):
        return {'status': 'disabled', 'learning_write_count': 0}
    if stage not in {"question", "hint", "solution"}:
        raise ValueError("invalid_teaching_stage")
    if stage == "hint" and hint_level != 1:
        raise ValueError("only_current_prepared_hint_level_1_supported")
    if focus_frame is not None and (type(focus_frame) is not int or focus_frame < 0):
        raise ValueError("invalid_focus_frame")
    pointer = cache.get_visual_pointer(Path(repo), question_id, stage=stage,
                                      hint_level=hint_level, expected_surface_sha256=expected_surface_sha256)
    if pointer.get("status") != "available":
        dismiss(repo, reason="current_visual_unavailable")
        return {"status": "visual_unavailable", "question_id": question_id,
                "reason": pointer.get("reason", pointer.get("status")),
                "triggered": False, "learning_write_count": 0}
    url = urlsplit(pointer["viewer_url"])
    revision = pointer.get("lesson_revision")
    if (url.scheme != "http" or url.hostname != "127.0.0.1" or url.port != 8769
            or url.path != "/study-visuals/" + question_id or url.fragment
            or parse_qs(url.query) != {"revision": [revision]}
            or not isinstance(revision, str) or not cache.SHA.fullmatch(revision)):
        raise ValueError("exact_local_versioned_viewer_required")
    request = {"schema": "study-visual-screen-request-v1", "action": "show",
               "question_id": question_id, "stage": stage, "hint_level": hint_level,
               "lesson_revision": revision, "viewer_url": pointer["viewer_url"],
               "target_display_name": config["target_display_name"]}
    if focus_frame is not None:
        request["focus_frame"] = focus_frame
    request["request_id"] = cache.digest(request)
    request["issued_at"] = time.time()
    result = _send(repo, request, wait_seconds=wait_seconds)
    evidence={}
    if result.get('status')=='shown':
        try:evidence=_display_evidence(repo,pointer,stage,hint_level,result)
        except (OSError,ValueError,KeyError) as exc:evidence={'visual_evidence_status':'unavailable','visual_evidence_error':str(exc)}
    return {**result, "triggered": result.get("status") == "shown",
            **evidence,
            "preview_path": pointer.get("preview_path"), "generation_on_read": False,
            "learning_write_count": 0, "display_is_not_a_learning_record": True}


def _display_evidence(repo,pointer,stage,hint_level,result):
    """Asset provenance for later capture, not a learning/exposure event."""
    import re
    # Private-device audits use a separate state root and never become study evidence.
    if STATE_REL != cache.ROOT_REL/'screen':return {}
    sid=os.environ.get('CODEX_THREAD_ID') or os.environ.get('CLAUDE_CODE_SESSION_ID')
    if not sid or not re.fullmatch(r'[A-Za-z0-9_.:-]+',sid):return {}
    folder=Path(pointer['preview_path']).parent
    if not (folder/'scene.json').is_file():return {'visual_evidence_status':'unavailable'}
    assets=[{'path':str(folder/name),'sha256':cache.file_sha(folder/name),
             'label':f'实际3D {stage} {pointer["cache_key"][:12]} {name}（预览不是实机截图）'}
            for name in ['scene.json','preview.png']]
    record={'schema':'cs408-displayed-visual-evidence-v1','status':'shown','session_id':sid,
            'question_id':pointer['question_id'],'stage':stage,'hint_level':hint_level,
            'lesson_revision':pointer['lesson_revision'],'cache_key':pointer['cache_key'],
            'shown_at':time.time(),'assets':assets,'learning_write_count':0}
    key=cache.digest(record);path=Path(repo)/STATE_REL/'evidence'/sid/pointer['question_id']/(key+'.json')
    cache.atomic_json(path,record)
    return {'visual_evidence_status':'recorded','visual_evidence_ref':str(path)}


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--repo", type=Path, default=Path(__file__).resolve().parents[1])
    p.add_argument("--question-id")
    p.add_argument("--decision", choices=["wrong", "correct", "not_evaluated"])
    p.add_argument("--stage", choices=["question", "hint", "solution"], default="hint")
    p.add_argument("--hint-level", type=int, default=1)
    p.add_argument("--focus-frame", type=int)
    p.add_argument("--expected-surface-sha256")
    p.add_argument("--teaching-goal")
    p.add_argument("--current-difficulty", default="")
    p.add_argument("--solution-authorized", action="store_true")
    p.add_argument("--tutor-presentation", choices=["text", "two_d", "three_d"],
                   help="当前讲解者根据本题判断的媒体")
    p.add_argument("--hide", action="store_true")
    p.add_argument("--close", action="store_true")
    p.add_argument("--status", action="store_true")
    a = p.parse_args()
    try:
        if a.status:
            result = _optional_json(a.repo / STATE_REL / "status.json") or {"status": "not_open"}
        elif a.hide or a.close:
            result = dismiss(a.repo, action="exit" if a.close else "hide")
        elif a.question_id and a.decision:
            result = feedback(a.repo, a.question_id, a.decision, stage=a.stage,
                              hint_level=a.hint_level, focus_frame=a.focus_frame,
                              expected_surface_sha256=a.expected_surface_sha256,
                              teaching_goal=a.teaching_goal, current_difficulty=a.current_difficulty,
                              solution_authorized=a.solution_authorized, tutor_presentation=a.tutor_presentation)
        else:
            p.error("--question-id and --decision are required for current feedback")
    except (ValueError, OSError, RuntimeError) as exc:
        result = {"status": "display_unavailable", "reason": str(exc), "learning_write_count": 0}
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
