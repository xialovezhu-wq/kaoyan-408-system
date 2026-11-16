"""Preserve native Page read receipts alongside the first question Capture.

No network calls, Page edits, transcript rewrites or extra Capture transactions.
"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path

import study_transcript_408 as transcripts

SCHEMA = 'cs408-sidebar-answer-evidence-v1'
LABEL = '侧栏作答原始Page回读'


def _check(ok, reason):
    if not ok:
        raise ValueError(reason)


def collect(source, payload, *, page_ids=()):
    """Use exact successful read_page receipts from this question's native span.

    Explicit page IDs may also use a fresh read in the current save turn, after
    its user message. Never select another chat or fall back to a Page preview.
    """
    requested = set(page_ids)
    if source['client'] != 'codex':
        _check(not requested, 'sidebar_page_requires_codex_native_receipt')
        return []
    path = Path(source['transcript_path'])
    marker = payload['source']['transcript_marker']
    paths = [path]
    sessions = next((p for p in path.parents if p.name == 'sessions'), None)
    if sessions is not None and source['thread_id'] in path.name:
        paths = sorted(p for p in sessions.glob('**/*'+source['thread_id']+'*.jsonl')
                       if p.name <= path.name)
    active = False
    ended = False
    snapshots = []
    seen = set()
    for part in paths:
        for event in transcripts._records(part):
            body = event.get('payload') or {}
            if event.get('type') == 'session_meta':
                _check(body.get('id') == source['thread_id'], 'sidebar_thread_mismatch')
            if event.get('type') == 'response_item' and body.get('type') == 'message':
                if body.get('id') == marker['start_message_id']:
                    active = True
                if body.get('id') == marker['end_message_id']:
                    ended = True
                elif ended and body.get('role') == 'user':
                    active = False
            if not active or event.get('type') != 'event_msg' or body.get('type') != 'item_completed':
                continue
            item = body.get('item') or {}
            if (item.get('type') != 'McpToolCall'
                    or item.get('tool') not in {'chatgpt_space.read_page', 'chatgpt_space_read_page'}
                    or item.get('status') != 'completed'):
                continue
            result = item.get('result') or {}
            data = result.get('structuredContent') or {}
            page = data.get('content') or {}
            metadata = data.get('metadata') or {}
            pid = page.get('page_id')
            if not pid or result.get('isError') or data.get('error'):
                continue
            if requested and pid not in requested:
                continue
            # Automatic discovery is limited to reads before the save request.
            if ended and not requested:
                continue
            args = item.get('arguments') or {}
            _check(args.get('page_id') == pid == metadata.get('page_id'), 'sidebar_page_identity_mismatch')
            _check(data.get('view') in {'editing', 'full', None}
                   and not args.get('search') and not args.get('block_ids')
                   and not data.get('block_excerpts'), 'sidebar_full_page_read_required')
            _check(metadata.get('stream_kind', 'content') == 'content', 'sidebar_content_stream_required')
            blocks = page.get('blocks')
            _check(isinstance(blocks, list) and blocks, 'sidebar_page_blocks_missing')
            for block in blocks:
                text = block.get('markdown')
                _check(isinstance(text, str) and block.get('id')
                       and hashlib.sha256(text.encode()).hexdigest() == block.get('hash'),
                       'sidebar_block_hash_mismatch')
            key = (pid, tuple((b['id'], b['hash']) for b in blocks))
            if key in seen:
                continue
            seen.add(key)
            snapshots.append({'page_id': pid, 'native_tool_call_id': item.get('id'),
                              'observed_at': event.get('timestamp'), 'receipt': data})
    _check(requested <= {s['page_id'] for s in snapshots}, 'sidebar_requested_page_read_missing')
    return snapshots


def attach(source, payload, *, page_ids=()):
    snapshots = collect(source, payload, page_ids=page_ids)
    if not snapshots:
        return None
    document = {'schema': SCHEMA, 'thread_id': source['thread_id'],
                'question_id': payload['source'].get('source_id'), 'snapshots': snapshots}
    raw = (json.dumps(document, ensure_ascii=False, sort_keys=True, indent=2)+'\n').encode()
    digest = hashlib.sha256(raw).hexdigest()
    payload['attachments'].append({'data': raw, 'role': 'other_attachment',
                                   'mime_type': 'application/json', 'label': LABEL})
    payload['source'].setdefault('supplemental_attachment_manifest', []).append(
        {'label': LABEL, 'sha256': digest, 'role': 'other_attachment'})
    proof = {'page_ids': sorted({s['page_id'] for s in snapshots}),
             'snapshot_count': len(snapshots), 'sha256': digest,
             'source': 'native_read_page_receipts'}
    payload['source']['sidebar_answer_evidence'] = proof
    return proof


def verify(package):
    proof = package['source']['source'].get('source', {}).get('sidebar_answer_evidence')
    if not proof:
        return None
    assets = [a for a in package['manifest']['attachments'] if a.get('label') == LABEL]
    _check(len(assets) == 1, 'sidebar_saved_attachment_missing')
    raw = (Path(package['package_dir'])/assets[0]['relative_path']).read_bytes()
    _check(hashlib.sha256(raw).hexdigest() == proof['sha256'] == assets[0]['sha256'],
           'sidebar_saved_attachment_changed')
    data = json.loads(raw)
    _check(data.get('schema') == SCHEMA and len(data['snapshots']) == proof['snapshot_count']
           and sorted({s['page_id'] for s in data['snapshots']}) == proof['page_ids'],
           'sidebar_saved_proof_mismatch')
    return {**proof, 'verified': True}
