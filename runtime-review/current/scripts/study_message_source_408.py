"""Read explicit current-thread message references without rewriting their text."""
import datetime as dt
import study_transcript_408 as transcripts
from study_transcript_408 import MessageSourceError


def _thread_messages(*, codex_root=None, thread_id=None, client=None, claude_root=None):
    source = transcripts.resolve_source(client=client, session_id=thread_id,
                                        codex_root=codex_root, claude_root=claude_root)
    messages = []
    for event in transcripts.read_messages(source):
        payload = event['payload']
        has_images = any(part.get('type') not in {'input_text', 'output_text'}
                         for part in payload.get('content', []))
        messages.append({'id': payload['id'], 'role': payload['role'],
                         'text': transcripts.message_text(event), 'has_images': has_images,
                         'timestamp': event.get('timestamp'), '_event': event, '_source': source})
    return source['thread_id'], messages


def _proof(msg):
    return {**msg['_source'], 'message_id': msg['id'], 'has_images': msg['has_images'],
            'message_sha256': transcripts.sha(transcripts.encoded(msg['_event']))}


def _assets(selected, directory, *, complete, turn_offset=0):
    source = selected[0]['_source']
    _, assets, _ = transcripts.evidence(source, [m['_event'] for m in selected],
                                       complete=complete, turn_offset=turn_offset)
    return transcripts.freeze_assets(assets, directory)


def resolve_messages(refs, *, codex_root=None, thread_id=None, client=None, claude_root=None,
                     evidence_dir=None, turn_offset=None, known_refs=(), legacy_known_ids=()):
    _, messages = _thread_messages(codex_root=codex_root, thread_id=thread_id, client=client, claude_root=claude_root)
    latest_user = next((i for i in range(len(messages)-1, -1, -1) if messages[i]['role'] == 'user'), None)
    resolved, proofs = [], []
    indices = []
    for ref in refs:
        if (not isinstance(ref, dict) or {'id', 'kind'} - set(ref)
                or set(ref) - {'id', 'kind', 'expected_text'}):
            raise MessageSourceError('message_ref_requires_id_and_kind')
        selector = ref['id']
        if selector == 'current_feedback':
            selected_indices = [i for i in range((latest_user if latest_user is not None else len(messages))+1, len(messages))
                                if messages[i]['role'] == 'assistant']
            if (not selected_indices or not isinstance(ref.get('expected_text'), str)
                    or ''.join(messages[i]['text'] for i in selected_indices) != ref['expected_text']):
                raise MessageSourceError('current_feedback_text_mismatch_or_unavailable')
            for i in selected_indices:
                msg = messages[i]
                indices.append(i)
                resolved.append(dict(role=msg['role'], kind=ref['kind'] if msg['text'] else 'image_attachment',
                                     text=msg['text'], observed_at=msg['timestamp']))
                proofs.append(_proof(msg))
            continue
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
        resolved.append(dict(role=msg['role'], kind=ref['kind'] if msg['text'] else 'image_attachment',
                             text=msg['text'], observed_at=msg['timestamp']))
        proofs.append(_proof(msg))
    if indices != sorted(set(indices)):
        raise MessageSourceError('message_references_must_be_unique_and_chronological')
    if evidence_dir is None:
        return resolved, proofs
    known = {(r.get('client', 'codex'), r['thread_id'], r['message_id']) for r in known_refs}
    selected = [(i, row, proof) for i, row, proof in zip(indices, resolved, proofs)
                if (proof['client'], proof['thread_id'], proof['message_id']) not in known
                and not (not known_refs and proof['client'] == 'codex' and proof['message_id'] in legacy_known_ids)]
    assets = _assets([messages[i] for i, _, _ in selected], evidence_dir,
                     complete=False, turn_offset=turn_offset) if selected else []
    return [r for _, r, _ in selected], [p for _, _, p in selected], assets


def resolve_display_episode(display, expected_user_text, *, codex_root=None, thread_id=None,
                            client=None, claude_root=None, evidence_dir=None):
    """Read every real message from this displayed image to the guarded latest reply."""
    _, messages = _thread_messages(codex_root=codex_root, thread_id=thread_id, client=client, claude_root=claude_root)
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
        if msg['role'] != 'assistant':
            continue
        native_id = (msg['_event'].get('native') or {}).get('message_id')
        if not native_id and not any(path in msg['text'] for path in paths):
            continue
        stamp = msg['timestamp']
        if stamp and dt.datetime.fromisoformat(stamp.replace('Z', '+00:00')) >= displayed:
            # Claude may stream several text blocks with separate native UUIDs.
            combined = ''
            for following in messages[i:end]:
                if following['role'] != 'assistant':
                    break
                if native_id and (following['_event'].get('native') or {}).get('message_id') != native_id:
                    break
                combined += following['text']
                if all(path in combined for path in paths):
                    start = i
                    break
            if start is not None:
                break
    if start is None:
        raise MessageSourceError('current_display_message_not_found')
    rows, proofs = [], []
    for msg in messages[start:]:
        if not isinstance(msg['id'], str) or not msg['id']:
            raise MessageSourceError('message_has_no_stable_id')
        if not msg['text'] and evidence_dir is None:
            raise MessageSourceError('image_only_message_requires_explicit_episode_evidence')
        rows.append(dict(role=msg['role'], kind='utterance' if msg['text'] else 'image_attachment',
                         text=msg['text'], observed_at=msg['timestamp']))
        proofs.append(_proof(msg))
    proofs[end-start]['text_match'] = text_match
    if evidence_dir is None:
        return rows, proofs
    return rows, proofs, _assets(messages[start:], evidence_dir, complete=True)
