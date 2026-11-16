#!/usr/bin/env python3
"""Serial first-answer capture for shell-only clients; no model or UI calls."""
import json
from pathlib import Path
import runpy
import subprocess
import sys

_resolver = Path(__file__).resolve().parents[2] / "_shared/kaoyan-408/scripts/resolve_408_paths.py"
OWNER = runpy.run_path(str(_resolver))["find_repo"]() / "scripts/cs408-turn.sh"


def invoke(operation, value):
    proc = subprocess.run(
        ['bash', str(OWNER), operation, '-'],
        input=json.dumps(value, ensure_ascii=False), text=True,
        capture_output=True, check=False,
    )
    try:
        result = json.loads(proc.stdout)
    except ValueError:
        result = {'status': 'runner_result_unavailable',
                  'stdout': proc.stdout, 'stderr': proc.stderr,
                  'recovery': 'Keep the original request and idempotency key.'}
    return proc.returncode, result


def run(request, call=invoke):
    answer = request['answer_input']
    finish = request['finish_input']
    # Only the caller's explicitly verified, unprompted first answer qualifies.
    if (answer.get('reasoning_result') != 'sound'
            or answer.get('prompt_dependency') != 'none'
            or finish.get('terminal_state') != 'correct'
            or finish.get('reasoning_result') != 'sound'
            or finish.get('prompt_dependency') != 'none'
            or finish.get('display_receipt_sha256') != answer.get('display_receipt_sha256')
            or finish.get('final_response') != answer.get('response')
            or not answer.get('idempotency_key')
            or not finish.get('idempotency_key')
            or finish['idempotency_key'] == answer['idempotency_key']):
        raise ValueError('explicit_matching_correct_first_answer_required')
    if any(key in finish for key in ('expected_prior_turn_count', 'expected_prior_trace_sha256')):
        raise ValueError('cursor_is_taken_from_answer_receipt')
    code, first = call('answer-choice', answer)
    output = {'answer_result': first, 'finish_result': None}
    if (code or first.get('status') != 'episode_pending_terminal'
            or first.get('choice_result') != 'correct'
            or first.get('reason_required') is not False
            or type(first.get('turn_count')) is not int
            or not isinstance(first.get('trace_sha256'), str)
            or not first['trace_sha256']):
        return code, output
    terminal = dict(finish,
                    expected_prior_turn_count=first['turn_count'],
                    expected_prior_trace_sha256=first['trace_sha256'])
    code, output['finish_result'] = call('finish-item', terminal)
    return code, output


def main():
    try:
        code, result = run(json.load(sys.stdin))
    except (KeyError, TypeError, ValueError) as exc:
        code, result = 2, {'status': 'invalid_batch_input', 'reason': str(exc)}
    print(json.dumps(result, ensure_ascii=False, indent=1))
    return code


if __name__ == '__main__':
    sys.exit(main())
