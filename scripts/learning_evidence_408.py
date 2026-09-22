"""Small immutable standalone learning evidence; no Capture or formal writes.

Managed turns store the same raw-turn shape inside their existing request instead.
Packaging, image copying, snapshot reads and model work belong to export time.
"""
from __future__ import annotations

import argparse
import datetime as dt
import fcntl
import hashlib
import json
import os
import re
import tempfile
from pathlib import Path

DEFAULT_ROOT = Path('/Users/your-user/.codex/kaoyan-408-conversation-packages')
SCHEMA = 'cs408-light-learning-evidence-v1'
INPUT_SCHEMA = 'cs408-light-learning-input-v1'
MAX_BYTES = 8 * 1024 * 1024
KINDS = {'utterance', 'first_action', 'reasoning', 'hint', 'correction', 'restatement', 'answer'}


def encoded(value):
    return (json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':')) + '\n').encode()


def digest(value):
    return hashlib.sha256(encoded(value)).hexdigest()


def normalize_turns(value):
    if not isinstance(value, list) or not value:
        raise ValueError('learning_turns_nonempty_array_required')
    result = []
    for row in value:
        if not isinstance(row, dict) or set(row) - {'role', 'kind', 'text', 'observed_at'}:
            raise ValueError('learning_turn_fields_invalid')
        if row.get('role') not in {'user', 'assistant'} or row.get('kind') not in KINDS:
            raise ValueError('learning_turn_role_or_kind_invalid')
        if not isinstance(row.get('text'), str) or not row['text']:
            raise ValueError('learning_turn_text_required')
        if row.get('observed_at') is not None and not isinstance(row['observed_at'], str):
            raise ValueError('learning_turn_time_invalid')
        result.append({**row, 'observed_at': row.get('observed_at')})
    if not any(row['role'] == 'user' for row in result):
        raise ValueError('actual_user_turn_required')
    if len(encoded(result)) > MAX_BYTES:
        raise ValueError('learning_turns_too_large')
    return result


def classification(result):
    if result.get('choice_result') != 'correct':
        raise ValueError('light_correct_result_required')
    reason = result.get('reasoning_result')
    dependency = result.get('prompt_dependency')
    if reason not in {'sound', 'partial', 'diverged', 'not_observed'}:
        raise ValueError('reasoning_result_required')
    if dependency not in {'none', 'minimal', 'progressive', 'full'}:
        raise ValueError('prompt_dependency_required')
    if result.get('confidence') not in {None, 'low', 'medium', 'high', 'unknown'}:
        raise ValueError('confidence_invalid')
    if reason in {'partial', 'diverged'}:
        return 'correct_choice_reasoning_unresolved'
    if reason == 'not_observed':
        return 'screened_correct'
    return 'independent_correct' if dependency == 'none' else 'corrected_after_hint'


def _write(path, raw):
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix='.evidence-')
    try:
        with os.fdopen(fd, 'wb') as handle:
            handle.write(raw)
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(tmp, 0o600)
        os.replace(tmp, path)
    finally:
        Path(tmp).unlink(missing_ok=True)


def record(body, private_root=DEFAULT_ROOT):
    if not isinstance(body, dict) or body.get('schema') != INPUT_SCHEMA:
        raise ValueError('light_learning_input_schema_required')
    allowed = {'schema', 'request_id', 'study_date', 'lesson_id', 'formal_node_id', 'source_id', 'question', 'turns', 'result'}
    if set(body) - allowed:
        raise ValueError('unknown_light_learning_fields')
    if not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_.:-]{0,191}', str(body.get('request_id', ''))):
        raise ValueError('stable_request_id_required')
    day = dt.date.fromisoformat(body['study_date']).isoformat()
    if body.get('formal_node_id') is not None and not re.fullmatch(r'(DS|CO|OS|CN)_(\d{4}|UNK)_\d{3}', body['formal_node_id']):
        raise ValueError('formal_node_id_invalid')
    question = body.get('question')
    if not isinstance(question, dict) or set(question) - {'stem', 'options', 'assets'}:
        raise ValueError('question_binding_required')
    if not isinstance(question.get('stem', ''), str) or not (question.get('stem') or question.get('assets')):
        raise ValueError('question_surface_required')
    if 'options' in question and not isinstance(question['options'], (list, dict)):
        raise ValueError('question_options_invalid')
    assets = question.get('assets', [])
    if not isinstance(assets, list):
        raise ValueError('question_assets_invalid')
    for asset in assets:
        if not isinstance(asset, dict) or not isinstance(asset.get('path'), str) or not re.fullmatch(r'[0-9a-f]{64}', str(asset.get('sha256', ''))):
            raise ValueError('question_asset_hash_required')
        # Keep bindings only; bytes are copied and reverified at export.
        if not Path(asset['path']).is_absolute():
            raise ValueError('question_asset_absolute_path_required')
    turns = normalize_turns(body.get('turns'))
    result = body.get('result')
    if not isinstance(result, dict) or set(result) - {'choice_result', 'reasoning_result', 'prompt_dependency', 'confidence'}:
        raise ValueError('result_fields_invalid')
    kind = classification(result)
    if any(t['role'] == 'assistant' and t['kind'] == 'hint' for t in turns) and result['prompt_dependency'] == 'none':
        raise ValueError('hint_trace_conflicts_with_independence')
    normalized = {**body, 'study_date': day, 'turns': turns}
    raw_input = encoded(normalized)
    if len(raw_input) > MAX_BYTES:
        raise ValueError('learning_evidence_too_large')
    root = Path(private_root).expanduser().absolute()
    if any(p.is_symlink() for p in (root, *root.parents)) or root == root.parent:
        raise ValueError('private_root_unsafe')
    store = root / '.learning-evidence-v1'
    if store.is_symlink():
        raise ValueError('evidence_store_unsafe')
    store.mkdir(parents=True, exist_ok=True)
    request_key = digest([day, body['request_id']])
    request_sha = hashlib.sha256(raw_input).hexdigest()
    pointer = store / 'requests' / (request_key + '.json')
    for directory in ('requests', 'records', 'locks'):
        if (store / directory).is_symlink():
            raise ValueError('evidence_directory_unsafe')
        (store / directory).mkdir(exist_ok=True)
    with (store / 'locks' / (request_key + '.lock')).open('a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        prior = None
        if pointer.exists():
            if pointer.is_symlink():
                raise ValueError('evidence_pointer_unsafe')
            prior = json.loads(pointer.read_bytes())
            if prior['request_sha256'] != request_sha:
                raise ValueError('idempotency_input_conflict')
            if not re.fullmatch(r'[0-9a-f]{64}', str(prior.get('record_sha256', ''))):
                raise ValueError('evidence_pointer_invalid')
            path = store / 'records' / (prior['record_sha256'] + '.json')
            if path.is_symlink():
                raise ValueError('saved_learning_evidence_drift')
            if path.exists():
                if hashlib.sha256(path.read_bytes()).hexdigest() != prior['record_sha256']:
                    raise ValueError('saved_learning_evidence_drift')
                return {**prior, 'status': 'already_recorded', 'record_path': str(path), 'light_evidence_write_count': 0,
                        'capture_write_count': 0, 'formal_write_count': 0, 'model_call_count': 0}
        value = {'schema': SCHEMA, 'subject': '408', 'evidence_id': 'LE408-' + request_key[:24],
                 'request_sha256': request_sha, 'study_date': day,
                 'lesson_id': body.get('lesson_id') or 'standalone:' + day,
                 'formal_node_id': body.get('formal_node_id'), 'source_id': body.get('source_id'),
                 'question': question, 'turns': turns, 'result': {**result, 'classification': kind},
                 'recorded_at': prior['recorded_at'] if prior else dt.datetime.now(dt.timezone.utc).isoformat(),
                 'formal_write_count': 0, 'capture_write_count': 0, 'mastery_updated': False}
        raw = encoded(value)
        record_sha = hashlib.sha256(raw).hexdigest()
        path = store / 'records' / (record_sha + '.json')
        receipt = {'evidence_id': value['evidence_id'], 'request_sha256': request_sha,
                   'record_sha256': record_sha, 'recorded_at': value['recorded_at']}
        if prior and prior != receipt:
            raise ValueError('prepared_learning_evidence_drift')
        if not prior:
            _write(pointer, encoded(receipt))
        _write(path, raw)
    return {**receipt, 'status': 'recovered' if prior else 'recorded', 'record_path': str(path), 'light_evidence_write_count': 1,
            'capture_write_count': 0, 'formal_write_count': 0, 'model_call_count': 0}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('command', choices=['record'])
    parser.add_argument('--input-json', required=True)
    parser.add_argument('--private-root', type=Path, default=DEFAULT_ROOT)
    args = parser.parse_args()
    import sys
    try:
        raw = sys.stdin.buffer.read(MAX_BYTES + 1) if args.input_json == '-' else Path(args.input_json).read_bytes()
        if len(raw) > MAX_BYTES:
            raise ValueError('learning_evidence_too_large')
        result = record(json.loads(raw), args.private_root)
    except (ValueError, KeyError, TypeError, OSError) as exc:
        print(json.dumps({'status': 'blocked', 'reason': str(exc)}, ensure_ascii=False))
        return 2
    print(json.dumps(result, ensure_ascii=False))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
