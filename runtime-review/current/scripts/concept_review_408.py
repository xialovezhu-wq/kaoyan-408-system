"""Atomic, private knowledge-point review sessions; no question or Capture writes."""
from __future__ import annotations
import argparse
import datetime as dt
import fcntl
import hashlib
import json
import os
import re
import shutil
import tempfile
from pathlib import Path
from zoneinfo import ZoneInfo

import runtime_paths_408 as _paths

CANONICAL_REPO = _paths.repo_root()
PRIVATE_ROOT = _paths.package_root() / 'concept-review'
SCHEMA = 'concept-review-session-v1'
OUTCOMES = {'wrong', 'independent_correct', 'corrected_after_hint', 'unresolved'}


def encoded(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':')).encode()


def digest(value):
    return hashlib.sha256(encoded(value)).hexdigest()


def private_root(repo):
    repo = Path(repo).resolve()
    return PRIVATE_ROOT if repo == CANONICAL_REPO.resolve() else repo / '.private/concept-review'


def session_paths(repo):
    root = private_root(repo) / 'sessions'
    return sorted(root.glob('*/session.json'))


def taxonomy(repo):
    return dict(re.findall(r'^\s*-\s+((?:DS|CO|OS|CN)\d{2}-\d{2})\s+(.+?)\s*$',
                          (Path(repo) / '知识点标签表.md').read_text(), re.M))


def _time(value):
    if not isinstance(value, str):
        raise ValueError('timezone_required')
    result = dt.datetime.fromisoformat(value.replace('Z', '+00:00'))
    if result.tzinfo is None or result.utcoffset() is None:
        raise ValueError('timezone_required')
    return result


def validate(payload, repo, master=None, *, historical=False):
    from intake_lib_408 import Repo
    from knowledge_learning_records_408 import canonical_knowledge_points
    if not isinstance(payload, dict) or payload.get('schema') != SCHEMA or payload.get('subject') != '408':
        raise ValueError('concept_review_schema_invalid')
    required = {'schema', 'subject', 'session_id', 'study_date', 'started_at', 'ended_at', 'conversation', 'artifacts', 'observations'}
    if not required <= set(payload) or set(payload) - required - {'time_quality'}:
        raise ValueError('concept_review_fields_invalid')
    if payload.get('time_quality', 'session_interval') not in {'session_interval', 'recorded_bounds_not_user_timestamps'}:
        raise ValueError('concept_review_time_quality_invalid')
    if not isinstance(payload['session_id'], str) or not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9._:-]{0,159}', payload['session_id']):
        raise ValueError('session_id_invalid')
    day = dt.date.fromisoformat(payload['study_date'])
    start, end = _time(payload['started_at']), _time(payload['ended_at'])
    if (end < start or
            (payload.get('time_quality') != 'recorded_bounds_not_user_timestamps'
             and start.astimezone(ZoneInfo('Asia/Shanghai')).date() != day)):
        raise ValueError('session_date_invalid')
    if day > dt.datetime.now(ZoneInfo('Asia/Shanghai')).date():
        raise ValueError('future_study_date_rejected')
    if end > dt.datetime.now(dt.timezone.utc) + dt.timedelta(minutes=5):
        raise ValueError('future_observation_rejected')
    turns = payload['conversation']
    if not isinstance(turns, list) or not turns:
        raise ValueError('complete_conversation_required')
    for turn in turns:
        if not isinstance(turn, dict) or set(turn) != {'role', 'text'} or turn['role'] not in {'user', 'assistant', 'system'} or not isinstance(turn['text'], str) or not turn['text'].strip():
            raise ValueError('conversation_turn_invalid')
    if not isinstance(payload['artifacts'], list):
        raise ValueError('artifacts_list_required')
    labels = taxonomy(repo) if not historical else {}
    master = Repo(Path(repo)).master() if master is None and not historical else (master or {})
    observations = payload['observations']
    if not isinstance(observations, list) or not observations:
        raise ValueError('actual_observations_required')
    seen = set()
    fields = {'observation_id', 'concept_key', 'question_ref', 'outcome', 'error_detail', 'user_turn_indices', 'hint_turn_indices', 'related_formal_ids'}
    for observation in observations:
        if not isinstance(observation, dict) or set(observation) - (fields | {'mechanism'}) or not fields <= set(observation):
            raise ValueError('observation_fields_invalid')
        oid = observation['observation_id']
        if not isinstance(oid, str) or not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9._:-]{0,99}', oid) or oid in seen:
            raise ValueError('observation_identity_invalid')
        seen.add(oid)
        if not isinstance(observation['concept_key'], str) or not re.fullmatch(r'(?:DS|CO|OS|CN)\d{2}-\d{2}', observation['concept_key']) or (not historical and observation['concept_key'] not in labels):
            raise ValueError('canonical_concept_required')
        if observation['outcome'] not in OUTCOMES:
            raise ValueError('outcome_invalid')
        for field in ('question_ref', 'error_detail'):
            if not isinstance(observation[field], str) or (field == 'question_ref' and not observation[field].strip()):
                raise ValueError('observation_text_invalid')
        if observation['outcome'] in {'wrong', 'unresolved'} and not observation['error_detail'].strip():
            raise ValueError('error_detail_required')
        if 'mechanism' in observation and not isinstance(observation['mechanism'], str):
            raise ValueError('mechanism_invalid')
        for field, role in [('user_turn_indices', 'user'), ('hint_turn_indices', 'assistant')]:
            values = observation[field]
            if not isinstance(values, list) or (field == 'user_turn_indices' and not values) or len(values) != len(set(values)):
                raise ValueError('actual_turn_evidence_required')
            if any(type(i) is not int or i < 0 or i >= len(turns) or turns[i]['role'] != role for i in values):
                raise ValueError('turn_evidence_role_invalid')
        if observation['outcome'] == 'independent_correct' and observation['hint_turn_indices']:
            raise ValueError('hinted_is_not_independent')
        if observation['outcome'] == 'corrected_after_hint':
            if not observation['hint_turn_indices'] or not any(u > h for u in observation['user_turn_indices'] for h in observation['hint_turn_indices']):
                raise ValueError('post_hint_user_evidence_required')
        ids = observation['related_formal_ids']
        if not isinstance(ids, list) or any(not isinstance(fid, str) or not re.fullmatch(r'(?:DS|CO|OS|CN)_(?:\d{4}|UNK)_\d{3}', fid) or (not historical and fid not in master) for fid in ids) or len(ids) != len(set(ids)):
            raise ValueError('related_formal_identity_invalid')
        for fid in ([] if historical else ids):
            membership = {code for field in ('主知识点', '副知识点', '命中知识点') for code in canonical_knowledge_points(master[fid].get(field))}
            if observation['concept_key'] not in membership:
                raise ValueError('related_formal_concept_mismatch')
    return payload


def reopen(path, repo):
    path = Path(path)
    if path.is_symlink() or path.parent.is_symlink():
        raise ValueError('session_symlink_rejected')
    value = json.loads(path.read_bytes())
    if value.get('schema') != 'sealed-concept-review-408-v1' or digest(value['session']) != value.get('session_sha256'):
        raise ValueError('session_hash_mismatch')
    expected = digest(value['session']['session_id'])
    if path.parent.name != expected:
        raise ValueError('session_identity_path_mismatch')
    validate(value['session'], repo, historical=True)
    for artifact in value['session']['artifacts']:
        relative = Path(artifact['stored_path'])
        if relative.is_absolute() or '..' in relative.parts or len(relative.parts) != 2 or relative.parts[0] != 'artifacts':
            raise ValueError('artifact_path_invalid')
        target = path.parent / relative
        if target.is_symlink() or not target.is_file() or hashlib.sha256(target.read_bytes()).hexdigest() != artifact['sha256']:
            raise ValueError('artifact_hash_mismatch')
    return value


def commit(repo, payload, *, followup=True):
    repo = Path(repo).resolve()
    if str(payload.get("session_id", "")).startswith("CAPREVIEW-"):
        from formal_quality_408 import require_owner
        require_owner(repo)
    validate(payload, repo, historical=True)
    # Read every supplied attachment before making any authoritative directory.
    session = json.loads(encoded(payload))
    blobs = []
    for index, entry in enumerate(payload['artifacts']):
        raw_path = entry if isinstance(entry, str) else entry.get('path') if isinstance(entry, dict) else None
        if not isinstance(raw_path, str):
            raise ValueError('artifact_file_path_required')
        source = Path(raw_path).expanduser()
        if source.is_symlink() or not source.is_file():
            raise ValueError('artifact_regular_file_required')
        raw = source.read_bytes()
        sha = hashlib.sha256(raw).hexdigest()
        if isinstance(entry, dict) and entry.get('sha256') not in (None, sha):
            raise ValueError('supplied_artifact_hash_mismatch')
        role = entry.get('role', 'attachment') if isinstance(entry, dict) else 'attachment'
        if not isinstance(role, str) or not role.strip():
            raise ValueError('artifact_role_invalid')
        suffix = source.suffix if re.fullmatch(r'\.[A-Za-z0-9]{1,10}', source.suffix) else ''
        relative = 'artifacts/' + str(index) + '-' + sha + suffix
        blobs.append((relative, raw))
        session['artifacts'][index] = {'source_path': str(source.absolute()), 'stored_path': relative, 'sha256': sha, 'size': len(raw), 'role': role, 'original_name': source.name}
    sha = digest(session)
    root = private_root(repo)
    root.mkdir(parents=True, exist_ok=True, mode=0o700)
    sessions = root / 'sessions'
    sessions.mkdir(exist_ok=True, mode=0o700)
    target = sessions / digest(session['session_id'])
    status = 'committed'
    with (root / '.commit.lock').open('a') as lock:
        fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
        if target.exists():
            old = reopen(target / 'session.json', repo)
            if old['session_sha256'] != sha:
                raise ValueError('session_conflict')
            status = 'duplicate'
        else:
            validate(payload, repo)
            staging = Path(tempfile.mkdtemp(prefix='.staging-', dir=root))
            try:
                for relative, raw in blobs:
                    output = staging / relative
                    output.parent.mkdir(exist_ok=True, mode=0o700)
                    _write(output, raw)
                _write(staging / 'session.json', encoded({'schema': 'sealed-concept-review-408-v1', 'session_sha256': sha, 'session': session}))
                for directory in (staging / 'artifacts', staging):
                    if directory.is_dir():
                        _fsync(directory)
                os.rename(staging, target)
                _fsync(sessions)
            finally:
                if staging.exists():
                    shutil.rmtree(staging)
        reopen(target / 'session.json', repo)
    return _postcommit(repo, session, sha, target, status, followup)


def recover(repo, source_id, *, followup=True):
    if not isinstance(source_id, str) or not re.fullmatch(r'CR408-[0-9a-f]{64}', source_id):
        raise ValueError('source_identity_invalid')
    repo = Path(repo).resolve()
    target = private_root(repo) / 'sessions' / source_id[6:]
    sealed = reopen(target / 'session.json', repo)
    return _postcommit(repo, sealed['session'], sealed['session_sha256'], target, 'duplicate', followup)


def _postcommit(repo, session, sha, target, status, followup):
    # Recovery reopens copied originals; it never replays or needs external attachments.
    event_id = 'concept-review-408:' + sha
    from exam_tutoring_context_408 import invalidate_after_commit
    result = {'status': status, 'event_id': event_id, 'session_sha256': sha,
              'session_path': str(target / 'session.json'), 'observation_count': len(session['observations']),
              'capture_count': 0, 'question_write_count': 0}
    try:
        invalidate_after_commit(repo, event_id)
        result['concept_pages'] = materialize(repo)
    except (OSError, ValueError) as exc:
        result['postcommit'] = {'status': 'pending', 'reason': type(exc).__name__}
        return result
    if followup:
        from study_commit_followup_408 import complete_after_commit
        try:
            result['followup'] = complete_after_commit(repo, event_id)
        except (OSError, ValueError) as exc:
            result['followup'] = {'status': 'pending', 'reason': type(exc).__name__}
    return result


def _fsync(path):
    descriptor = os.open(path, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _write(path, raw):
    descriptor = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    with os.fdopen(descriptor, 'wb') as handle:
        handle.write(raw)
        handle.flush()
        os.fsync(handle.fileno())


def safe_records(value):
    from knowledge_history_sources_408 import _safe_annotation
    session = value['session']
    records = []
    for index, observation in enumerate(session['observations']):
        outcome = observation['outcome']
        summary = _safe_annotation(observation['error_detail']) if observation['error_detail'].strip() else None
        if not summary:
            summary = {'wrong': '知识点复盘出现错误，原始证据保存在私有会话。',
                       'unresolved': '知识点复盘仍未解决，原始证据保存在私有会话。',
                       'independent_correct': '本次知识点复盘无提示作答正确。',
                       'corrected_after_hint': '本次知识点复盘经提示后纠正。'}[outcome]
        records.append({'source_kind': 'concept_review', 'source_id': 'CR408-' + digest(session['session_id']),
            'source_sha256': value['session_sha256'], 'source_fragment': 'observation:' + str(index),
            'source_order': max(observation['user_turn_indices']), 'source_locator': 'cs408-concept-review://' + digest(session['session_id']), 'source_available': True, 'source_access': 'local_private_only',
            'knowledge_points': [observation['concept_key']], 'formal_ids': observation['related_formal_ids'],
            'observed_date': session['study_date'],
            'event_time': None if session.get('time_quality') == 'recorded_bounds_not_user_timestamps' else session['ended_at'],
            'time_quality': session.get('time_quality', 'session_interval'),
            'summary': summary, 'evidence_role': 'observed_learner_evidence', 'point_binding': 'explicit_point',
            'event_kind': 'repair' if outcome == 'corrected_after_hint' else 'outcome',
            'outcome': {'corrected_after_hint': 'correct', 'unresolved': 'uncertain'}.get(outcome, outcome),
            'prompt_dependency': 'hint_or_prompted' if observation['hint_turn_indices'] else 'independent',
            'review_marker': 'concept_review', 'review_session_id': digest(session['session_id']),
            'review_had_error': outcome == 'wrong'})
    return records


def materialize(repo):
    root = private_root(repo)
    root.mkdir(parents=True, exist_ok=True, mode=0o700)
    with (root / '.view.lock').open('a') as lock:
        fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
        return _materialize(repo)


def _materialize(repo):
    from intake_lib_408 import Repo
    from knowledge_learning_records_408 import canonical_knowledge_points
    from exam_tutoring_context_408 import _atomic_write
    repo = Path(repo).resolve()
    labels = taxonomy(repo)
    master = Repo(repo).master()
    formals = {code: set() for code in labels}
    for fid, row in master.items():
        for field in ('主知识点', '副知识点', '命中知识点'):
            for code in canonical_knowledge_points(row.get(field)):
                if code in formals:
                    formals[code].add(fid)
    reviews = {code: {} for code in labels}
    for path in session_paths(repo):
        value = reopen(path, repo)
        for record in safe_records(value):
            code = record['knowledge_points'][0]
            labels.setdefault(code, '历史封存知识点')
            formals.setdefault(code, set())
            reviews.setdefault(code, {})
            formals[code].update(fid for fid in record['formal_ids'] if fid in master)
            sid = record['review_session_id']
            row = reviews[code].setdefault(sid, {'date': record['observed_date'], 'error': False})
            row['error'] |= record['review_had_error']
    directory = repo / '知识点'
    directory.mkdir(exist_ok=True)
    index = ['# 408 知识点', '', '稳定知识点连接正式题与独立复盘。题目关联不等于该知识点已经暴露错误。', '']
    if (directory / '学习路径' / '索引.md').is_file():
        index += ['[[知识点/学习路径/索引|完整学习过程与讲题摘要]]', '']
    for code, label in labels.items():
        entries = reviews[code]
        lines = [f'# {code} {label}', '', f'知识点码：{code}', '',
                 '关联题目', '', *([f'- [[{fid}]]' for fid in sorted(formals[code])] or ['暂无关联正式题。']), '',
                 '独立知识点复盘', '', f'新封存会话复盘场次：{len(entries)}；明确错误场次：{sum(row["error"] for row in entries.values())}。', '',
                 '同一场次同一知识点最多计一次；不改关联题的最近复做日期和次数。', '']
        if (directory / '学习路径' / (code + '.md')).is_file():
            lines[4:4] = [f'[[知识点/学习路径/{code}|查看完整学习过程与讲题摘要]]', '']
        for sid, entry in sorted(entries.items(), key=lambda item: (item[1]['date'], item[0])):
            lines.append(f'- {entry["date"]}｜复盘标记：concept_review｜场次 {sid[:16]}｜' + ('有明确错误观测' if entry['error'] else '无明确错误观测（不等于独立正确）'))
        _atomic_write(directory / (code + '.md'), ('\n'.join(lines) + '\n').encode())
        index.append(f'- [[{code}]] {label}')
    _atomic_write(directory / '索引.md', ('\n'.join(index) + '\n').encode())
    return len(labels)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('command', choices=['commit', 'materialize', 'read-source', 'recover'])
    parser.add_argument('--repo', type=Path, default=CANONICAL_REPO)
    parser.add_argument('--input', type=Path)
    parser.add_argument('--source-id')
    args = parser.parse_args()
    if args.command == 'commit':
        if args.input is None:
            parser.error('--input required')
        result = commit(args.repo, json.loads(args.input.read_bytes()))
    elif args.command == 'recover':
        result = recover(args.repo, args.source_id)
    elif args.command == 'read-source':
        if not args.source_id or not re.fullmatch(r'CR408-[0-9a-f]{64}', args.source_id):
            parser.error('--source-id CR408-<hash> required')
        result = reopen(private_root(args.repo) / 'sessions' / args.source_id[6:] / 'session.json', args.repo)
    else:
        result = {'concept_pages': materialize(args.repo), 'learning_write_count': 0}
    print(json.dumps(result, ensure_ascii=False))


if __name__ == '__main__':
    main()
