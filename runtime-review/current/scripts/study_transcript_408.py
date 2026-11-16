#!/usr/bin/env python3
"""Native Codex/Claude visible-message readers; no model or learning writes."""
from __future__ import annotations

import argparse
import base64
from contextlib import closing
import hashlib
import json
import os
from pathlib import Path
import re
import shlex
import sqlite3
import sys

import runtime_paths_408 as _paths


SCHEMA = 'cs408-transcript-marker-v2'
SNAPSHOT_PREFIX = '408 原始消息范围快照 v2:'
CLIENTS = {'codex', 'claude'}


class MessageSourceError(ValueError):
    def __init__(self, message, *, diagnostic=None):
        super().__init__(message)
        self.diagnostic = diagnostic


def require(condition, message):
    if not condition:
        raise MessageSourceError(message)


def encoded(value):
    return (json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':')) + '\n').encode()


def sha(raw):
    return hashlib.sha256(raw).hexdigest()


def resolve_source(*, client=None, session_id=None, path=None, codex_root=None, claude_root=None):
    """Bind exactly one native session. Never choose by mtime or inspect other chats."""
    explicit = client is not None or session_id is not None or path is not None or codex_root is not None
    if codex_root is not None and client is None:
        client = 'codex'
    if not explicit:
        bound = os.environ.get('CS408_CHAT_CLIENT')
        claude_id, codex_id = os.environ.get('CLAUDE_CODE_SESSION_ID'), os.environ.get('CODEX_THREAD_ID')
        if bound == 'claude':
            client, session_id, path = bound, os.environ.get('CS408_CHAT_SESSION_ID'), os.environ.get('CS408_CHAT_TRANSCRIPT_PATH')
            require(not claude_id or claude_id == session_id, 'Claude 会话绑定已过期；请重新触发 SessionStart')
        elif claude_id and codex_id:
            raise MessageSourceError('两个客户端会话身份同时存在；请明确 client，不能猜测当前会话')
        elif claude_id:
            client, session_id = 'claude', claude_id
        elif codex_id:
            client, session_id = 'codex', codex_id
    client = client or 'codex'
    require(client in CLIENTS, '不支持的会话客户端')
    session_id = session_id or os.environ.get('CLAUDE_CODE_SESSION_ID' if client == 'claude' else 'CODEX_THREAD_ID')
    if not session_id and os.environ.get('CS408_CHAT_CLIENT') == client:
        session_id = os.environ.get('CS408_CHAT_SESSION_ID')
    require(isinstance(session_id, str) and re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_.:-]{0,191}', session_id),
            'current_thread_id_unavailable')
    if client == 'codex':
        root = Path(codex_root or os.environ.get('CODEX_HOME') or _paths.codex_home()).expanduser().resolve()
        if path is None:
            try:
                with closing(sqlite3.connect((root/'state_5.sqlite').as_uri()+'?mode=ro', uri=True)) as db:
                    row = db.execute('SELECT rollout_path FROM threads WHERE id=?', (session_id,)).fetchone()
            except sqlite3.Error as exc:
                raise MessageSourceError('current_thread_index_unavailable') from exc
            require(row is not None, 'current_thread_transcript_unavailable')
            path = row[0]
        path = Path(path).expanduser().resolve()
        require(path.is_relative_to(root/'sessions') and path.suffix == '.jsonl', 'transcript_path_outside_sessions')
    else:
        root = Path(claude_root or os.environ.get('CLAUDE_CONFIG_DIR') or Path.home()/'.claude').expanduser().resolve()
        if path is None:
            bound_id = os.environ.get('CS408_CHAT_SESSION_ID')
            if bound_id == session_id and os.environ.get('CS408_CHAT_CLIENT') == 'claude':
                path = os.environ.get('CS408_CHAT_TRANSCRIPT_PATH')
        if path is None:
            # Exact filename lookup at project depth only: no subagent or history scan.
            candidates = list((root/'projects').glob('*/'+session_id+'.jsonl'))
            require(len(candidates) == 1, 'Claude 当前会话文件缺失或不唯一；请提供原生 transcript_path')
            path = candidates[0]
        path = Path(path).expanduser().resolve()
        require(path.is_relative_to(root/'projects') and path.name == session_id+'.jsonl'
                and path.parent.parent == root/'projects', 'Claude 会话路径与身份不匹配')
    return dict(client=client, thread_id=session_id, transcript_path=str(path))


def _records(path):
    lines = Path(path).read_bytes().splitlines(keepends=True)
    for i, line in enumerate(lines):
        try:
            event = json.loads(line)
        except ValueError as exc:
            if i == len(lines)-1 and not line.endswith(b'\n'):
                break
            raise MessageSourceError('会话记录中间有无法解析的行') from exc
        require(isinstance(event, dict), '会话记录必须为对象')
        yield event


def _codex_file(path, identity):
    messages, identities = [], []
    for event in _records(path):
        payload = event.get('payload') or {}
        if event.get('type') == 'session_meta':
            identities.append(payload.get('id'))
        if (event.get('type') == 'response_item' and payload.get('type') == 'message'
                and payload.get('role') in {'user', 'assistant'}
                and payload.get('channel') in {None, 'commentary', 'final'}):
            messages.append(event)
    require(identities == [identity], '会话身份不唯一或不匹配')
    ids = [event['payload'].get('id') for event in messages]
    require(all(isinstance(key, str) and key for key in ids) and len(ids) == len(set(ids)),
            '原始消息缺少唯一稳定 ID')
    return messages


def _claude_messages(path, identity, leaf_id=None):
    records, order = {}, []
    for event in _records(path):
        if event.get('isSidechain'):
            continue
        uuid = event.get('uuid')
        if not uuid:
            require(event.get('type') not in {'user', 'assistant'}, '原始消息缺少唯一稳定 ID')
            continue
        if event.get('type') in {'user', 'assistant', 'system'}:
            require(event.get('sessionId') == identity, 'Claude 会话身份不匹配')
        if uuid in records:
            require(event == records[uuid], 'Claude 同 ID 原始消息不一致')
            continue
        records[uuid] = event
        order.append(uuid)
    leaves = [key for key in order if records[key].get('type') in {'user', 'assistant', 'system'}]
    require(leaves, 'Claude 会话无原始消息')
    current, chain, seen = leaf_id or leaves[-1], [], set()
    while current:
        require(current not in seen, 'Claude 消息父链存在循环')
        require(current in records, 'Claude 原始父消息缺失，不能确认完整范围')
        seen.add(current)
        event = records[current]
        chain.append(event)
        current = event.get('logicalParentUuid') if event.get('subtype') == 'compact_boundary' else event.get('parentUuid')
    messages = []
    for event in reversed(chain):
        role = event.get('type')
        if role not in {'user', 'assistant'} or event.get('isMeta') or event.get('isCompactSummary'):
            continue
        native = event.get('message') or {}
        require(native.get('role', role) == role, 'Claude 消息角色不一致')
        content = native.get('content', [])
        if isinstance(content, str):
            content = [{'type': 'text', 'text': content}]
        require(isinstance(content, list), 'Claude 消息内容格式不支持')
        visible = []
        for part in content:
            require(isinstance(part, dict), 'Claude 消息块格式不支持')
            kind = part.get('type')
            if kind in {'thinking', 'redacted_thinking', 'tool_use', 'tool_result', 'server_tool_use'} or str(kind).endswith('_tool_result'):
                continue
            if kind == 'text':
                require(isinstance(part.get('text'), str), 'Claude 原始文字缺失')
                visible.append({'type': 'input_text' if role == 'user' else 'output_text', 'text': part['text']})
            else:
                # Preserve unsupported visible blocks: selected ranges must fail, never drop them.
                visible.append(part)
        if not visible or not any(p.get('text') or p.get('type') not in {'input_text', 'output_text'} for p in visible):
            continue
        messages.append({'type': 'response_item', 'timestamp': event.get('timestamp'),
            'payload': {'type': 'message', 'id': event['uuid'], 'role': role, 'content': visible},
            'native': {'client': 'claude', 'session_id': identity, 'parent_uuid': event.get('parentUuid'),
                       'message_id': native.get('id')}})
    return messages


def read_messages(source, *, leaf_id=None):
    require(source.get('client') in CLIENTS, '不支持的会话客户端')
    path, identity = Path(source['transcript_path']), source['thread_id']
    if source['client'] == 'claude':
        messages = _claude_messages(path, identity, leaf_id)
    else:
        sessions = next((p for p in path.parents if p.name == 'sessions'), None)
        candidates = [path]
        if sessions is not None and identity in path.name:
            candidates = sorted(p for p in sessions.glob('**/*'+identity+'*.jsonl') if p.name <= path.name)
            require(path in candidates, '当前会话分段缺失')
        messages, seen = [], {}
        for candidate in candidates:
            for event in _codex_file(candidate, identity):
                key = event['payload'].get('id')
                if key in seen:
                    require(event['payload'] == seen[key]['payload'], '会话分段中的同ID消息不一致')
                    continue
                seen[key] = event
                messages.append(event)
    ids = [m['payload'].get('id') for m in messages]
    require(all(isinstance(i, str) and i for i in ids) and len(ids) == len(set(ids)), '原始消息缺少唯一稳定 ID')
    return messages


def message_text(event):
    return ''.join(p['text'] for p in event['payload'].get('content', [])
                   if p.get('type') in {'input_text', 'output_text'} and isinstance(p.get('text'), str))


def materialize(messages, *, source=None):
    turns, attachments = [], []
    prefix = f"transcript:{source['client']}:{source['thread_id']}:" if source else ''
    for event in messages:
        payload, count = event['payload'], 0
        text = message_text(event)
        for part in payload.get('content', []):
            kind = part.get('type')
            if kind in {'input_text', 'output_text'}:
                continue
            if kind in {'image', 'document'} and isinstance(part.get('source'), dict):
                origin = part['source']
                require(origin.get('type') == 'base64', '附件不是内嵌原件；请保全缺失原件')
                mime, data = origin.get('media_type'), origin.get('data')
                require(isinstance(mime, str) and isinstance(data, str), '原始附件内容缺失')
            elif kind in {'input_image', 'output_image', 'image'}:
                url = part.get('image_url')
                if isinstance(url, dict):
                    url = url.get('url')
                require(isinstance(url, str) and url.startswith('data:image/') and ';base64,' in url,
                        '图片不是内嵌原件；请保全缺失原件，不静默跳过')
                header, data = url.split(';base64,', 1)
                mime = header[5:]
            else:
                raise MessageSourceError('未支持的消息内容类型，不能遗漏保存：'+str(kind))
            try:
                raw = base64.b64decode(data, validate=True)
            except (ValueError, TypeError) as exc:
                raise MessageSourceError('原始附件编码无效') from exc
            require(bool(raw), '原始附件为空')
            count += 1
            attachments.append({'data': raw, 'role': 'other_attachment', 'mime_type': mime,
                                'label': prefix+payload['id']+':image:'+str(count)})
        require(text or count, '空消息不能作为学习证据')
        turns.append({'role': payload['role'], 'kind': 'utterance' if text else 'image_attachment',
                      'text': text, 'observed_at': event.get('timestamp')})
    return turns, attachments


def evidence(source, messages, *, complete=True, turn_offset=0):
    turns, assets = materialize(messages, source=source)
    ids = [m['payload']['id'] for m in messages]
    require(bool(ids), '消息范围为空')
    marker = dict(schema=SCHEMA, **source, start_message_id=ids[0], end_message_id=ids[-1],
                  message_ids=ids, messages_sha256=sha(encoded(messages)),
                  range_kind='contiguous' if complete else 'selection',
                  boundary_review='requires_semantic_review')
    snapshot = dict(schema=SCHEMA, marker=marker, messages=messages, turn_offset=turn_offset,
                    complete_conversation=complete)
    assets.append(dict(data=encoded(snapshot), role='other_attachment', mime_type='application/json',
                       label=SNAPSHOT_PREFIX+sha(encoded(marker))[:24]))
    return turns, assets, marker


def freeze_assets(assets, directory):
    directory = Path(directory)
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    result = []
    for asset in assets:
        raw = asset['data']
        path = directory/sha(raw)
        if path.exists():
            require(not path.is_symlink() and path.read_bytes() == raw, '消息附件快照内容冲突')
        else:
            with path.open('xb') as handle:
                handle.write(raw)
            path.chmod(0o600)
        result.append({k: v for k, v in asset.items() if k != 'data'} | {'path': str(path), 'sha256': sha(raw)})
    return result


def verify_package(package, *, live=True):
    """Verify native snapshots, selected conversation positions and copied originals."""
    manifest = package['manifest']['attachments']
    snapshots = [a for a in manifest if a.get('label', '').startswith(SNAPSHOT_PREFIX)]
    declared = ((package['source'].get('source') or {}).get('source') or {}).get('transcript_marker')
    if not snapshots:
        require(not declared or declared.get('schema') != SCHEMA, '缺少原始消息快照')
        return {'status': 'legacy_package', 'boundary_review': 'requires_semantic_review'}
    actual = [(t['role'], t['text'], t.get('observed_at')) for t in package['conversation']['turns']]
    evidence_files = [a for a in manifest if a.get('label', '').startswith(('transcript:', SNAPSHOT_PREFIX))]
    files = {a['label']: a for a in evidence_files}
    require(len(files) == len(evidence_files), '消息附件标签不唯一')
    covered, images, statuses, clients = set(), set(), [], set()
    root = Path(package['package_dir'])
    for item in snapshots:
        raw = (root/item['relative_path']).read_bytes()
        require(sha(raw) == item['sha256'], '原始消息快照哈希不一致')
        snapshot = json.loads(raw)
        marker, messages = snapshot['marker'], snapshot['messages']
        if declared and declared.get('schema') == SCHEMA:
            require(marker == declared and len(snapshots) == 1, '标记与原始消息快照不一致')
        require(snapshot.get('schema') == SCHEMA and marker.get('schema') == SCHEMA, '不支持的消息标记版本')
        require(marker.get('range_kind') in {'contiguous', 'selection'}, '消息范围类型无效')
        require(item['label'] == SNAPSHOT_PREFIX+sha(encoded(marker))[:24], '消息快照标签与标记不一致')
        ids = [m['payload']['id'] for m in messages]
        require(ids and ids == marker['message_ids'] and len(ids) == len(set(ids))
                and ids[0] == marker['start_message_id'] and ids[-1] == marker['end_message_id'], '消息边界与快照不一致')
        require(sha(encoded(messages)) == marker['messages_sha256'], '原始消息内容已变化')
        source = {k: marker[k] for k in ('client', 'thread_id', 'transcript_path')}
        clients.add(source['client'])
        turns, originals = materialize(messages, source=source)
        expected = [(t['role'], t['text'], t.get('observed_at')) for t in turns]
        offset = snapshot.get('turn_offset')
        if offset is None:
            matches = [i for i in range(len(actual)-len(expected)+1) if actual[i:i+len(expected)] == expected]
            require(len(matches) == 1, '原始消息在可读对话中不唯一或缺失')
            offset = matches[0]
        require(type(offset) is int and offset >= 0 and actual[offset:offset+len(expected)] == expected,
                '可读对话与原始消息不一致')
        envelope = package['source'].get('source') or {}
        if snapshot.get('complete_conversation') and not envelope.get('parent_package_locator'):
            require(offset == 0 and len(expected) == len(actual), '完整消息范围与可读对话不一致')
        covered.update(range(offset, offset+len(expected)))
        for asset in originals:
            saved = files.get(asset['label'])
            require(saved is not None and saved['sha256'] == sha(asset['data'])
                    and sha((root/saved['relative_path']).read_bytes()) == saved['sha256'], '附件与原始消息不一致')
            images.add(asset['label'])
        status = 'not_checked'
        if live:
            if not Path(source['transcript_path']).is_file():
                status = 'unavailable_snapshot_preserved'
            else:
                available = read_messages(source, leaf_id=marker['end_message_id'])
                by_id = {m['payload']['id']: i for i, m in enumerate(available)}
                require(all(key in by_id for key in ids), '原会话消息边界缺失；需要人工核对')
                indices = [by_id[key] for key in ids]
                require(indices == sorted(indices), '原会话消息顺序变化')
                span = (available[indices[0]:indices[-1]+1] if marker['range_kind'] == 'contiguous'
                        else [available[i] for i in indices])
                require(sha(encoded(span)) == marker['messages_sha256'], '原会话范围或内容与标记不一致')
                status = 'matched'
        statuses.append(status)
    return dict(status='verified', live_transcript=('unavailable_snapshot_preserved'
                if 'unavailable_snapshot_preserved' in statuses else statuses[0]),
                message_count=len(covered), image_count=len(images), snapshot_count=len(snapshots),
                clients=sorted(clients), coverage='complete' if len(covered) == len(actual) else 'partial',
                boundary_review='requires_semantic_review')


def bind_claude(event, env_file):
    require(event.get('hook_event_name') == 'SessionStart', '只接受 Claude SessionStart 绑定')
    require(isinstance(event.get('session_id'), str) and isinstance(event.get('transcript_path'), str),
            'Claude SessionStart 缺少原生会话身份或路径')
    source = resolve_source(client='claude', session_id=event.get('session_id'), path=event.get('transcript_path'))
    require(bool(env_file), 'Claude 未提供 CLAUDE_ENV_FILE')
    values = dict(CS408_CHAT_CLIENT='claude', CS408_CHAT_SESSION_ID=source['thread_id'],
                  CS408_CHAT_TRANSCRIPT_PATH=source['transcript_path'])
    with Path(env_file).open('a', encoding='utf-8') as handle:
        handle.write('\n'+''.join(f'export {key}={shlex.quote(value)}\n' for key, value in values.items()))
    return source


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('command', choices=['bind-claude', 'inspect-source'])
    args = parser.parse_args()
    try:
        if args.command == 'bind-claude':
            bind_claude(json.load(sys.stdin), os.environ.get('CLAUDE_ENV_FILE'))
        else:
            source = resolve_source()
            messages = read_messages(source)
            print(json.dumps(dict(source=source, message_count=len(messages), write_count=0), ensure_ascii=False))
    except (ValueError, OSError) as exc:
        print('BLOCKED: '+str(exc), file=sys.stderr)
        sys.exit(2)
