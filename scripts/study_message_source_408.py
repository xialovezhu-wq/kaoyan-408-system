"""Read explicit current-thread message references without rewriting their text."""
import json
import os
import datetime as dt
from pathlib import Path
import sqlite3
from contextlib import closing


class MessageSourceError(ValueError):
    def __init__(self, message, *, diagnostic=None):
        super().__init__(message)
        self.diagnostic = diagnostic


def _thread_messages(*, codex_root=None, thread_id=None):
    root = Path(codex_root or os.environ.get('CODEX_HOME') or Path.home() / '.codex').resolve()
    identity = thread_id or os.environ.get('CODEX_THREAD_ID')
    if not identity:
        raise MessageSourceError('current_thread_id_unavailable')
    try:
        with closing(sqlite3.connect((root / 'state_5.sqlite').as_uri() + '?mode=ro', uri=True)) as db:
            row = db.execute('SELECT rollout_path FROM threads WHERE id = ?', (identity,)).fetchone()
    except sqlite3.Error as exc:
        raise MessageSourceError('current_thread_index_unavailable') from exc
    if not row:
        raise MessageSourceError('current_thread_transcript_unavailable')
    path = Path(row[0]).resolve()
    if not path.is_relative_to(root / 'sessions') or path.suffix != '.jsonl':
        raise MessageSourceError('transcript_path_outside_sessions')
    messages = []
    with path.open(encoding='utf-8') as handle:
        for line in handle:
            try:
                event = json.loads(line)
            except ValueError:
                continue  # An in-flight final line may not yet be complete.
            payload = event.get('payload') or {}
            if (event.get('type') != 'response_item' or payload.get('type') != 'message'
                    or payload.get('role') not in {'user', 'assistant'}):
                continue
            content = payload.get('content') or []
            texts = [part['text'] for part in content
                     if part.get('type') in {'input_text', 'output_text'} and isinstance(part.get('text'), str)]
            has_images = any(part.get('type') in {'input_image', 'output_image', 'image'} for part in content)
            if not any(texts) and not has_images:
                continue
            messages.append({'id': payload.get('id'), 'role': payload['role'],
                             'text': ''.join(texts), 'has_images': has_images,
                             'timestamp': event.get('timestamp')})
    return identity, messages


def resolve_messages(refs, *, codex_root=None, thread_id=None):
    identity, messages = _thread_messages(codex_root=codex_root, thread_id=thread_id)
    latest_user = next((i for i in range(len(messages)-1, -1, -1) if messages[i]['role'] == 'user'), None)
    resolved, proofs = [], []
    indices = []
    for ref in refs:
        if (not isinstance(ref, dict) or {'id', 'kind'} - set(ref)
                or set(ref) - {'id', 'kind', 'expected_text'}):
            raise MessageSourceError('message_ref_requires_id_and_kind')
        selector = ref['id']
        if selector == 'latest_user':
            if not isinstance(ref.get('expected_text'), str):
                raise MessageSourceError('latest_user_requires_expected_text_to_prevent_races')
            index = latest_user
        elif selector == 'previous_assistant':
            index = next((i for i in range((latest_user or 0)-1, -1, -1)
                          if messages[i]['role'] == 'assistant'), None)
        else:
            index = next((i for i, msg in enumerate(messages) if msg['id'] == selector), None)
        if index is None:
            raise MessageSourceError('message_reference_not_found:' + str(selector))
        msg = messages[index]
        if 'expected_text' in ref and ref['expected_text'] != msg['text']:
            raise MessageSourceError('message_text_changed_or_new_user_arrived')
        if not isinstance(msg['id'], str) or not msg['id']:
            raise MessageSourceError('message_has_no_stable_id')
        indices.append(index)
        resolved.append(dict(role=msg['role'], kind=ref['kind'], text=msg['text'], observed_at=None))
        proofs.append({'thread_id': identity, 'message_id': msg['id'], 'has_images': msg['has_images']})
    if indices != sorted(set(indices)):
        raise MessageSourceError('message_references_must_be_unique_and_chronological')
    return resolved, proofs


def resolve_display_episode(display, expected_user_text, *, codex_root=None, thread_id=None):
    """Read every real message from this displayed image to the guarded latest reply."""
    identity, messages = _thread_messages(codex_root=codex_root, thread_id=thread_id)
    if not isinstance(expected_user_text, str) or not expected_user_text:
        raise MessageSourceError('episode_requires_expected_user_text')
    end = next((i for i in range(len(messages)-1, -1, -1) if messages[i]['role'] == 'user'), None)
    if end is None or messages[end]['text'].rstrip('\r\n') != expected_user_text.rstrip('\r\n'):
        raise MessageSourceError('message_text_changed_or_new_user_arrived', diagnostic={
            'current_user_message_id': messages[end]['id'] if end is not None else None,
            'expected_text_chars': len(expected_user_text),
            'source_text_chars': len(messages[end]['text']) if end is not None else None,
            'recovery_action': 'check_current_message_without_searching_source_or_sessions',
            'recovery_message': '提交正文与当前线程最新用户消息不匹配。保持当前题未提交，核对本轮原话；不要搜索源码或全会话目录。',
        })
    # Only the transport comparison tolerates final line endings. Saved text
    # always comes from the original message, including every trailing byte.
    text_match = 'exact' if messages[end]['text'] == expected_user_text else 'trailing_line_endings_only'
    paths = [asset['path'] for asset in display.get('question_assets', [])]
    if not paths:
        raise MessageSourceError('episode_image_anchor_unavailable_use_explicit_turns')
    displayed = dt.datetime.fromisoformat(display['displayed_at'].replace('Z', '+00:00'))
    start = None
    for i, msg in enumerate(messages[:end]):
        if msg['role'] != 'assistant' or not all(path in msg['text'] for path in paths):
            continue
        stamp = msg['timestamp']
        if stamp and dt.datetime.fromisoformat(stamp.replace('Z', '+00:00')) >= displayed:
            start = i
            break
    if start is None:
        raise MessageSourceError('current_display_message_not_found')
    rows, proofs = [], []
    for msg in messages[start:]:
        if not isinstance(msg['id'], str) or not msg['id']:
            raise MessageSourceError('message_has_no_stable_id')
        if not msg['text']:
            raise MessageSourceError('image_only_message_requires_explicit_episode_evidence')
        rows.append(dict(role=msg['role'], kind='utterance', text=msg['text'], observed_at=msg['timestamp']))
        proofs.append(dict(thread_id=identity, message_id=msg['id'], has_images=msg['has_images']))
    proofs[end-start]['text_match'] = text_match
    return rows, proofs
