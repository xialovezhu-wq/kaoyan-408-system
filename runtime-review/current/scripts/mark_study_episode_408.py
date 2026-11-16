#!/usr/bin/env python3
"""Native message markers -> existing client-independent 408 evidence package."""
from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import os
from pathlib import Path
import re
import sqlite3
import time
import unicodedata
from zoneinfo import ZoneInfo

import conversation_package_408 as packages
import study_episode_capture_408 as episodes
import study_transcript_408 as transcripts

SCHEMA = 'cs408-transcript-marker-v1'
SNAPSHOT_LABEL = '408 原始消息范围快照'
CURRENT_ITEM = r'(?:这道(?:错)?题|这(?:错)?题|此题|本题|当前(?:错)?题)'
FORMAL_ACTION = r'(?:立即|现在|直接)?(?:完整)?正式(?:入库|录入)'
FORMAL_REQUEST = re.compile(
    r'(?:好(?:的)?[，,]\s*)?(?:(?:请(?:帮我)?|帮我|麻烦你)\s*)?'
    rf'(?:(?:(?:把|将)?{CURRENT_ITEM}\s*)?{FORMAL_ACTION}|{FORMAL_ACTION}\s*{CURRENT_ITEM})'
    r'(?:一下|吧)?[。.!！]?'
)
ANNUAL_NEXT = re.compile(
    r'(?:(?:好(?:的)?|(?:我)?(?:理解|明白|懂)了)[，,。]\s*)?'
    r'(?:(?:直接)?(?:看|进入|开始|继续))?(?:下一题|下一道题)(?:吧)?[。.!！]?'
)
ANNUAL_NUMBERED_NEXT = re.compile(
    r'(?:明白|理解|懂)了[，,。]\s*第\d{1,2}题[^。！？\n]*[。.!！]\s*'
    r'(?:接下来|然后)(?:讲|看|进入)第\d{1,2}题[。.!！]?'
)
CORRECT_THEN_SAVE = re.compile(
    r'(?:如果|若)(?:这道题|这题|本题|当前题)(?:我)?(?:做对|答对)(?:了)?(?:的话)?[,，]\s*'
    r'(?:你)?(?:就|应该|应当)(?:把|将)(?:这道题|这题|本题|当前题)快速(?:入库|保存|录入)[,，]\s*'
    r'(?:再|来|然后)?(?:展示|进入|看)(?:下一道题|下一题)(?:啊|吧)?'
)
CORRECT_NO_REMINDER_THEN_SAVE = re.compile(
    r'如果这道题我已经做对了[，,]\s*很明确是做对了[、,，]\s*没有什么需要提醒的点[，,]\s*'
    r'你就直接快速入库就好[，,]\s*顺便再来下一道题[，,]\s*就不要我每次给你讲'
)


def correct_then_save_requested(text):
    sentences = re.split(r'[。！？!?；;\n]', unicodedata.normalize('NFKC', text))
    return any(CORRECT_THEN_SAVE.fullmatch(sentence.strip())
               or CORRECT_NO_REMINDER_THEN_SAVE.fullmatch(sentence.strip()) for sentence in sentences)


def correct_completion_observed(messages):
    for message in messages:
        payload = message['payload']
        if payload['role'] != 'assistant' or payload.get('channel') not in {'final', 'commentary', None}:
            continue
        for sentence in re.split(r'[。！？!?；;\n]', message_text(message)):
            if re.fullmatch(r'\s*(?:本题|本问|这一问|这道题|你的算法思想)'
                            r'(?:已经|已)(?:答对|答完整|完整)\s*', sentence):
                return True
    return False


def encoded(value):
    return (json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':')) + '\n').encode()


def sha(raw):
    return hashlib.sha256(raw).hexdigest()

def episode_id_for_message(client, thread_id, user_message_id):
    identity = [thread_id, user_message_id]
    if client != 'codex':
        identity.insert(0, client)
    return 'transcript-' + sha(encoded(identity))[:24]


def require(condition, message):
    if not condition:
        raise ValueError(message)


def transcript_path(thread_id, root=None):
    return Path(transcripts.resolve_source(client='codex', session_id=thread_id,
                                          codex_root=root)['transcript_path'])


def read_messages(path, thread_id, client='codex'):
    return transcripts.read_messages(dict(client=client, thread_id=thread_id,
                                         transcript_path=str(Path(path).resolve())))


message_text = transcripts.message_text
materialize = transcripts.materialize


def build_payload(*, path, thread_id, start_anchor, end_text, require_latest=True, feedback_anchor=None,
                  client='codex', intent='quick', annual=False, standing_authorization_anchor=None,
                  allow_supplement=False):
    messages = read_messages(path, thread_id, client=client)
    starts = [i for i, m in enumerate(messages) if m['payload']['role'] == 'user'
              and (m['payload']['id'] == start_anchor or start_anchor in message_text(m))]
    ends = [i for i, m in enumerate(messages) if m['payload']['role'] == 'user'
            and message_text(m).rstrip('\r\n') == end_text.rstrip('\r\n')]
    require(len(starts) == 1, '起点不唯一；请用消息 ID 或更精确原文定位')
    ends = [i for i in ends if i >= starts[0]]
    require(len(ends) == 1, '本题范围内结束请求不唯一；请核对保存请求')
    start, end = starts[0], ends[0]
    require(start <= end, '消息范围无效')
    if require_latest:
        require(end == max(i for i, m in enumerate(messages) if m['payload']['role'] == 'user'),
                '已有新用户消息；不能把旧请求当成本轮授权。已保存内容用 verify 核对')
    from standalone_question_intake_408 import _is_explicit_fast_intake_message, _is_explicit_supplement_message
    end_message = message_text(messages[end])
    normalized = unicodedata.normalize('NFKC', end_message).strip()
    conditional_quick_save = False
    if intent == 'formal':
        require(FORMAL_REQUEST.fullmatch(normalized), '结束消息不是明确的当前题正式入库请求')
        require(start < end or any(block.get('type') in {'input_image', 'image'}
                                   for block in messages[start]['payload']['content']),
                '正式入库需要当前题原始学习证据或已封存的 Capture')
        authorization = {'schema': 'cs408-explicit-formal-intake-authorization-v1',
                         'kind': 'explicit_formal_intake', 'user_message': end_message}
    elif intent == 'quick' and (_is_explicit_fast_intake_message(end_message)
                               or allow_supplement and _is_explicit_supplement_message(end_message)):
        authorization = {'schema': 'cs408-explicit-fast-intake-authorization-v1',
                         'kind': 'explicit_fast_intake', 'user_message': end_message}
    elif (intent == 'quick' and annual and start < end
          and (ANNUAL_NEXT.fullmatch(normalized) or ANNUAL_NUMBERED_NEXT.fullmatch(normalized))):
        authorization = {'schema': 'cs408-annual-question-terminal-v1',
                         'kind': 'annual_question_terminal', 'user_message': end_message}
    elif intent == 'quick' and correct_then_save_requested(end_message):
        conditional_quick_save = True
        authorization = {'schema': 'cs408-explicit-fast-intake-authorization-v1',
                         'kind': 'explicit_fast_intake', 'user_message': end_message}
    elif intent == 'quick' and standing_authorization_anchor:
        # A real, earlier per-question instruction is separate from the current
        # answer. Never rewrite the answer as a new intake command.
        require(feedback_anchor, '逐题保存需要已发出的本题终态反馈')
        candidates = [m for m in messages[:end] if m['payload']['role'] == 'user'
                      and (m['payload']['id'] == standing_authorization_anchor
                           or standing_authorization_anchor in message_text(m))]
        require(len(candidates) == 1, '逐题保存授权必须唯一定位本会话原始用户消息')
        permission = candidates[0]
        directive = '每题结束时在本地快速保存'
        paragraphs = re.split(r'[。！？\n]', message_text(permission))
        conditional_quick_save = correct_then_save_requested(message_text(permission))
        require(any(line.strip().startswith(directive) for line in paragraphs) or conditional_quick_save,
                '未找到明确的逐题快速保存指令')
        permission_index = messages.index(permission)
        require(permission_index <= start <= end and permission_index < end,
                '逐题授权必须早于本题作答')
        require(not any(re.search(r'(?:不要|停止|取消|暂停).{0,12}(?:保存|入库)', message_text(m))
                        for m in messages[permission_index+1:end+1]
                        if m['payload']['role'] == 'user'), '逐题保存授权可能已撤回')
        authorization = {
            'schema': 'cs408-standing-question-terminal-v1',
            'kind': 'standing_question_terminal',
            'user_message': message_text(permission),
            'authorization_message_id': permission['payload']['id'],
            'authorization_message_sha256': sha(encoded(permission)),
            'terminal_message_id': messages[end]['payload']['id'],
            'terminal_user_message': end_message,
            'authorization_evidence': permission,
        }
    else:
        raise ValueError('结束消息不是明确快速入库请求')
    through = end
    if feedback_anchor:
        following = []
        for i in range(end+1, len(messages)):
            if messages[i]['payload']['role'] == 'user':
                break
            if feedback_anchor in message_text(messages[i]):
                following.append(i)
        require(len(following) == 1, '保存前反馈定位不唯一')
        through = following[0]
    selected = messages[start:through+1]
    if conditional_quick_save:
        require(correct_completion_observed(selected), '答对后保存的条件尚缺本题已完成的明确反馈')
    turns, attachments = materialize(selected)
    marker = {'schema': SCHEMA, 'client': 'codex', 'thread_id': thread_id,
              'transcript_path': str(Path(path).resolve()), 'start_message_id': selected[0]['payload']['id'],
              'end_message_id': selected[-1]['payload']['id'],
              'message_ids': [m['payload']['id'] for m in selected],
              'messages_sha256': sha(encoded(selected)), 'boundary_review': 'requires_semantic_review'}
    snapshot = {'schema': SCHEMA, 'marker': marker, 'messages': selected}
    attachments.append({'data': encoded(snapshot), 'role': 'other_attachment',
                        'mime_type': 'application/json', 'label': SNAPSHOT_LABEL})
    if client == 'claude':
        source = dict(client=client, thread_id=thread_id, transcript_path=str(Path(path).resolve()))
        turns, attachments, marker = transcripts.evidence(source, selected)
    date_message = messages[start] if intent == 'formal' else selected[-1]
    stamp = dt.datetime.fromisoformat(date_message['timestamp'].replace('Z', '+00:00'))
    require(stamp.tzinfo is not None, '原始保存请求缺少带时区时间')
    # End-message identity fixes one episode. Changing the start after submission conflicts.
    identity = episode_id_for_message(client, thread_id, messages[end]['payload']['id'])
    return {'schema': episodes.SCHEMA, 'episode_id': identity,
            'execution_kind': 'standalone_question',
            'study_date': stamp.astimezone(ZoneInfo('Asia/Shanghai')).date().isoformat(),
            'source': {'source_id': None, 'session_id': thread_id, 'capture_mode': 'evidence_only',
                       'evidence_source': 'verified_transcript', 'transcript_marker': marker},
            'turns': turns, 'attachments': attachments, 'observed_outcome': None,
            'authorization': authorization,
            'display_binding': {'kind': 'standalone_source', 'external_source_id': None},
            'missing_fields': [], 'frozen_feedback': ''}


def verify_opened(package, *, live=True):
    """Check package against original snapshot, and the exact live span when available."""
    envelope = package['source'].get('source') or {}
    if envelope.get('evidence_revision_kind') == 'native_standalone_supplement':
        private = Path(package['package_dir']).parent.parent
        parent = packages.verify_package_binding(envelope['parent_package_locator'],
            expected_canonical_sha256=envelope['parent_package_sha256'], private_root=private,
            require_sealed=True)
        require(len(parent['conversation']['turns']) < len(package['conversation']['turns']),
                '追加包必须比父包包含更多对话')
        parent_proof = verify_opened(parent, live=live)
        root = packages.verify_package_binding(envelope['root_package_locator'],
            expected_canonical_sha256=envelope['root_package_sha256'], private_root=private,
            require_sealed=True)
        root_proof = verify_opened(root, live=live)
        appended = transcripts.verify_package(package, live=live)
        count = root_proof['message_count'] + appended['message_count']
        require(count == len(package['conversation']['turns']), '追加对话未被原生快照完整覆盖')
        require(package['conversation']['turns'][:len(parent['conversation']['turns'])]
                == parent['conversation']['turns'], '追加包改变了父包对话')
        return {**appended, 'coverage': 'complete', 'message_count': count,
                'original_message_count': root_proof['message_count'],
                'parent_verified': parent_proof['status'] == 'verified'}
    inner = (package['source'].get('source') or {}).get('source') or {}
    marker = inner.get('transcript_marker')
    if marker is None or marker.get('schema') == transcripts.SCHEMA:
        return transcripts.verify_package(package, live=live)
    require(marker.get('schema') == SCHEMA, '不支持的消息标记版本')
    candidates = [r for r in package['manifest']['attachments'] if r.get('label') == SNAPSHOT_LABEL]
    require(len(candidates) == 1, '缺少唯一原始消息快照')
    row = candidates[0]
    raw = (Path(package['package_dir']) / row['relative_path']).read_bytes()
    require(sha(raw) == row['sha256'], '原始消息快照哈希不一致')
    snapshot = json.loads(raw)
    require(snapshot.get('marker') == marker, '标记与原始消息快照不一致')
    messages = snapshot['messages']
    require(sha(encoded(messages)) == marker['messages_sha256'], '原始消息内容已变化')
    ids = [m['payload']['id'] for m in messages]
    require(ids == marker['message_ids'] and ids[0] == marker['start_message_id']
            and ids[-1] == marker['end_message_id'], '消息边界与快照不一致')
    turns, images = materialize(messages)
    actual = [{k: t[k] for k in ('role', 'kind', 'text', 'observed_at')}
              for t in package['conversation']['turns']]
    require(actual == turns, '可读对话与原始消息不一致')
    assets = {r['label']: r for r in package['manifest']['attachments'] if r['label'] != SNAPSHOT_LABEL}
    require(len(assets) >= len(images), '图片数量与原始消息不一致')
    for image in images:
        asset = assets.get(image['label'])
        require(asset is not None and asset['sha256'] == sha(image['data']), '图片与原始消息不一致')
    live_status = 'not_checked'
    if live:
        path = Path(marker['transcript_path'])
        if not path.is_file():
            live_status = 'unavailable_snapshot_preserved'
        else:
            all_messages = read_messages(path, marker['thread_id'])
            by_id = {m['payload']['id']: i for i, m in enumerate(all_messages)}
            require(ids[0] in by_id and ids[-1] in by_id, '原会话消息边界缺失；需要人工核对')
            span = all_messages[by_id[ids[0]]:by_id[ids[-1]]+1]
            require(sha(encoded(span)) == marker['messages_sha256'], '原会话范围或内容与标记不一致')
            live_status = 'matched'
    supplemental = [r for r in assets.values() if r['label'] not in {i['label'] for i in images}]
    declared = inner.get('supplemental_attachment_manifest')
    if declared is not None:
        require({r['label']: r['sha256'] for r in supplemental} ==
                {r['label']: r['sha256'] for r in declared}, '补充附件与声明不一致')
    for asset in supplemental:
        require(sha((Path(package['package_dir'])/asset['relative_path']).read_bytes()) == asset['sha256'],
                '补充附件内容与封包不一致')
    return {'status': 'verified', 'live_transcript': live_status, 'message_count': len(turns),
            'image_count': len(images), 'start_message_id': ids[0], 'end_message_id': ids[-1],
            'supplemental_attachment_count': len(supplemental),
            'boundary_review': 'requires_semantic_review'}


def scope_batch_payload(payload, question_id, repo=None):
    from standalone_question_intake_408 import explicit_batch_question_numbers, explicit_batch_part_numbers
    parts = explicit_batch_part_numbers(payload['authorization']['user_message'])
    if parts:
        match = re.fullmatch(r'EXAM408-\d{4}-Q(\d{2})-P\d{2}', question_id or '')
        require(match and int(match[1]) == parts[0], '小问批量保存必须指定同一父题的已登记小问')
        root = Path(repo) if repo is not None else Path(__file__).resolve().parents[1]
        index = json.loads((root/'support/syllabus-2027/exam-subquestions.json').read_text())
        def find(value):
            if isinstance(value, dict):
                if value.get('subquestion_id') == question_id:
                    yield value
                for item in value.values():
                    if isinstance(item, (dict, list)):
                        yield from find(item)
            elif isinstance(value, list):
                for item in value:
                    yield from find(item)
        rows = list(find(index))
        labels = {re.match(r'[（(](\d+)[）)]', row.get('source_part_label', '')).group(1)
                  for row in rows if re.match(r'[（(](\d+)[）)]', row.get('source_part_label', ''))}
        require(len(labels) == 1 and int(next(iter(labels))) in parts[1], '小问不在本次请求的原题问号范围')
        parent = payload['episode_id']
        payload['episode_id'] = parent + '-q' + question_id.split('-Q', 1)[1].replace('-P', '-p')
        payload['source']['batch_authorization'] = {
            'parent_episode_id': parent, 'original_question_number': parts[0],
            'original_part_numbers': list(parts[1]), 'question_id': question_id}
        return payload
    numbers = explicit_batch_question_numbers(payload['authorization']['user_message'])
    if not numbers:
        return payload
    match = re.fullmatch(r'EXAM408-\d{4}-Q(\d{2})', question_id or '')
    require(match and int(match[1]) in numbers, '批量保存必须指定请求中的精确题号')
    parent = payload['episode_id']
    payload['episode_id'] = parent + '-q' + match[1]
    payload['source']['batch_authorization'] = {
        'parent_episode_id': parent, 'question_numbers': list(numbers), 'question_id': question_id}
    return payload


def existing_seal_in_span(repo, payload, question_id, package_root):
    """Formal requests and annual navigation reuse an earlier seal of this same span."""
    native = payload['source']['transcript_marker']
    messages = list(zip(native['message_ids'], payload['turns'], strict=True))
    requests = [(i, mid) for i, (mid, turn) in enumerate(messages) if turn['role'] == 'user']
    root = episodes._operation_root(Path(repo).resolve()) / 'receipts'
    private_root = package_root or episodes._default_package_root(Path(repo).resolve())
    for i, mid in reversed(requests[:-1]):
        previous_id = episode_id_for_message(native['client'], native['thread_id'], mid)
        candidates = [previous_id]
        if question_id:
            number = question_id.split('-Q')[-1].replace('-P','-p')
            candidates.append(f'{previous_id}-q{number}')
        for prior in candidates:
            receipt = root / (sha(prior.encode()) + '.json')
            if not receipt.is_file():
                continue
            verified = episodes.verify_episode_capture(repo, receipt, package_root=private_root)
            opened = packages.verify_package_binding(verified['conversation_package_locator'],
                                                      private_root=private_root, require_sealed=True)
            source = opened['source']['source']['source']
            prior_marker = source.get('transcript_marker') or {}
            require(prior_marker.get('start_message_id') == native['start_message_id'],
                    '当前请求的证据范围与已封题目交叠，请按原 Capture 及准确起点核对')
            prior_question = source.get('source_id')
            if question_id and prior_question and prior_question.startswith('EXAM408-'):
                require(prior_question == question_id, '已封题面身份与当前请求不符')
            require(i == requests[-2][0],
                    f"已封 {verified['capture_id']} 后仍有学习原话；按原 Capture 补充核对，不能重封")
            if (verified['currently_awaiting_web_enhancement']
                    and payload['authorization']['kind'] == 'explicit_formal_intake'):
                verified.update(display_status='复用已封证据，待本地正式提交',
                                next_action='continue_local_formal_intake')
            return {**verified, 'created':False, 'continuation_message_id': requests[-1][1]}, {
                'source': source, 'turns': opened['conversation']['turns']}
    return None


def record_marked(repo, *, source, start_anchor, end_text, question_id=None,
                  attachments=(), bind_year=False, feedback_anchor=None, package_root=None, intent='quick',
                  standing_authorization_anchor=None, answer_page_ids=()):
    """One call: exact native messages -> existing writer -> optional annual index."""
    from syllabus_topic_review_408 import attach_terminal_review
    started=time.perf_counter();timings={}
    require(not standing_authorization_anchor or question_id,
            '逐题保存必须指定已核验的当前题身份')
    payload=build_payload(path=source['transcript_path'],thread_id=source['thread_id'],
        client=source['client'],start_anchor=start_anchor,end_text=end_text,
        require_latest=True,feedback_anchor=feedback_anchor,intent=intent,annual=bind_year,
        standing_authorization_anchor=standing_authorization_anchor)
    payload=scope_batch_payload(payload,question_id,repo)
    timings['native_messages_ms']=(time.perf_counter()-started)*1000
    if intent == 'formal' or payload['authorization']['kind'] == 'annual_question_terminal':
        prior = existing_seal_in_span(repo, payload, question_id, package_root)
        if prior:
            result, previous_payload = prior
            if bind_year:
                result=_finish_annual(repo,previous_payload,result,timings)
            return attach_terminal_review(result,question_id) if intent=='quick' else result
    if question_id or attachments or bind_year:
        from capture_context_408 import enrich_payload, annual_target
        step=time.perf_counter();payload=enrich_payload(repo,payload,question_id=question_id,attachments=attachments)
        if bind_year:annual_target(repo,payload)
        timings['source_and_assets_ms']=(time.perf_counter()-step)*1000
    from sidebar_answer_capture_408 import attach as attach_sidebar, verify as verify_sidebar
    step=time.perf_counter()
    sidebar=attach_sidebar(source,payload,page_ids=answer_page_ids)
    timings['sidebar_evidence_ms']=(time.perf_counter()-step)*1000
    result=episodes.record_episode(Path(repo),payload,package_root=package_root,timings=timings)
    if sidebar:
        opened=packages.verify_package_binding(result['conversation_package_locator'],
            private_root=package_root or episodes._default_package_root(Path(repo).resolve()),
            require_sealed=True)
        result['sidebar_answer_evidence']=verify_sidebar(opened)
    if bind_year:
        result=_finish_annual(repo,payload,result,timings)
    timings['total_ms']=(time.perf_counter()-started)*1000
    if intent=='quick':
        result=attach_terminal_review(result,question_id)
    return {**result,'timing_ms':timings}


def supplement_marked(repo, *, source, receipt, start_anchor, end_text, question_id,
                      feedback_anchor=None, attachments=(), package_root=None):
    """Use exact native turns with the existing append-only supplement writer."""
    import standalone_question_intake_408 as standalone
    from capture_context_408 import enrich_payload
    repo = Path(repo).resolve()
    private = Path(package_root or episodes._default_package_root(repo)).resolve()
    receipt_path = Path(receipt)
    require(receipt_path.is_file() and not receipt_path.is_symlink(), '原保存回执不可用')
    prior = json.loads(receipt_path.read_bytes())
    if prior.get('schema') == episodes.RECEIPT_SCHEMA:
        prior = episodes.verify_episode_capture(repo, receipt_path, package_root=private)
    else:
        require(prior.get('schema') == 'cs408-standalone-question-supplement-receipt-v1',
                '不支持的父回执')
    parent = packages.verify_package_binding(prior['conversation_package_locator'],
        expected_canonical_sha256=prior['conversation_package_sha256'], private_root=private,
        require_sealed=True)
    verify_opened(parent)
    origin = parent['source']['source']
    require(origin.get('execution_kind') == 'standalone_question', '仅支持原生独立题追加')
    identity = origin['source']
    require(identity.get('source_id') == question_id, '补充内容的精确题号与原包不符')
    require(identity.get('session_id') == source['thread_id'], '补充必须来自原题会话')
    payload = build_payload(path=source['transcript_path'], thread_id=source['thread_id'],
        client=source['client'], start_anchor=start_anchor, end_text=end_text,
        feedback_anchor=feedback_anchor, allow_supplement=True, require_latest=False)
    snapshot = next(json.loads(asset['data']) for asset in payload['attachments']
                    if asset['label'] == SNAPSHOT_LABEL or asset['label'].startswith(transcripts.SNAPSHOT_PREFIX))
    new_messages = snapshot['messages']
    current = transcripts.read_messages(source)
    positions = {message['payload']['id']: i for i, message in enumerate(current)}
    request_index = next(i for i, message in enumerate(current)
                         if message['payload']['role'] == 'user'
                         and message_text(message).rstrip('\r\n') == end_text.rstrip('\r\n'))
    # An answer to the next question does not revoke an authorized supplement.
    require(not any(re.search(r'(?:不要|取消|停止|暂停|先不|不用|撤回).{0,25}(?:补充|追加|保存|入库)',
                              message_text(message))
                    for message in current[request_index+1:] if message['payload']['role'] == 'user'),
            '后续消息撤回或暂停了追加授权')
    saved_ids = []
    for asset in parent['manifest']['attachments']:
        if asset['label'] == SNAPSHOT_LABEL or asset['label'].startswith(transcripts.SNAPSHOT_PREFIX):
            saved = json.loads((Path(parent['package_dir'])/asset['relative_path']).read_bytes())
            saved_ids.extend(saved['marker']['message_ids'])
    require(saved_ids and all(mid in positions for mid in saved_ids), '父包原生消息范围缺失')
    require(positions[new_messages[0]['payload']['id']] > max(positions[mid] for mid in saved_ids),
            '新补充不能重叠或重写已封原话')
    turns, assets, _ = transcripts.evidence(source, new_messages, complete=False,
                                          turn_offset=len(parent['conversation']['turns']))
    added = {'source': {}, 'turns': turns, 'attachments': assets}
    enrich_payload(repo, added, attachments=attachments)
    request = {'schema': 'cs408-standalone-question-supplement-v1',
        'request_id': payload['episode_id'] + '-supplement',
        'capture_id': prior['capture_id'],
        'parent_package_locator': prior['conversation_package_locator'],
        'parent_package_sha256': prior['conversation_package_sha256'],
        'new_turns': turns, 'attachments': assets, 'authorization': payload['authorization']}
    result = standalone.supplement_value(request, repo_root=repo, package_root=private)
    opened = packages.verify_package_binding(result['conversation_package_locator'],
        expected_canonical_sha256=result['conversation_package_sha256'], private_root=private,
        require_sealed=True)
    proof = verify_opened(opened)
    return {**result, 'capture_verified': True, 'package_verified': proof['coverage'] == 'complete',
            'message_count': proof['message_count'], 'added_message_count': len(turns),
            'next_action': 'return_to_study'}


def _finish_annual(repo,payload,result,timings):
    from capture_context_408 import bind_annual
    step=time.perf_counter()
    try:result={**result,**bind_annual(repo,payload,result)}
    except (OSError,ValueError) as exc:
        result={**result,'annual_binding_verified':False,'annual_binding_error':str(exc),
                'completion_status':'capture_saved_annual_binding_pending',
                'display_status':'证据已封存，年度绑定待恢复',
                'next_action':'recover_annual_binding_with_same_receipt',
                'recovery_receipt':result['receipt_ref']}
    timings['annual_binding_ms']=(time.perf_counter()-step)*1000
    return result


def _output(result,compact):
    if not compact:return result
    keys=('status','display_status','next_action','capture_id','episode_id','capture_verified','package_verified','annual_binding_verified',
          'annual_binding_error','completion_status','recovery_receipt','annual_record_path','receipt_ref',
          'conversation_package_locator','created','timing_ms','formal_write_count','fsrs_write_count','personal_summary_write_count',
          'capture_write_count','supplement_event_id','message_count','added_message_count','quick_review',
          'sidebar_answer_evidence')
    return {k:result[k] for k in keys if k in result}


def _record_only_requested(text):
    """Recognize explicit presentation limits without treating quoted text as consent."""
    clauses = re.split(r'[，,、。！？!?；;\n]', unicodedata.normalize('NFKC', text or ''))
    return any(re.fullmatch(
        r'\s*(?:这次|本次)?(?:只记录(?:不(?:总结|讲解|复盘))?|只保存|仅记录|仅保存|'
        r'(?:不要|不用|不)(?:输出|生成|做)?(?:总结|复盘|讲解|图解|知识点树))(?:即可|就行|就好)?\s*',
        clause) for clause in clauses)


def _recap_destination(repo, result, case, output, inline_root, thread_id=None):
    from render_interactive_408 import inline_destination
    if inline_root is None and output is not None:
        inline_root = output.parent
    if inline_root is None:
        from runtime_paths_408 import codex_home
        thread_id = thread_id or os.environ.get('CODEX_THREAD_ID')
        require(thread_id and re.fullmatch(r'[A-Za-z0-9_-]+', thread_id),
                '知识点树需要当前会话的可视化目录')
        candidates = [p for p in (codex_home()/'visualizations').glob(f'*/*/*/{thread_id}')
                      if p.is_dir() and not p.is_symlink()]
        require(len(candidates) == 1, '未能唯一定位本会话可视化目录，请传入 --inline-root')
        inline_root = candidates[0]
    if output is None:
        from local_quality_408 import catalog
        _, version = catalog(Path(repo))
        suffix = sha(encoded({'capture':result.get('capture_id'), 'case':case, 'catalog':version}))[:16]
        output = Path(inline_root)/f'recap-{suffix}.html'
    return inline_destination(output, inline_root), Path(inline_root)


def _finish_recap(repo, result, case, output, inline_root, started, *, automatic=False,
                  record_only=False, thread_id=None):
    if case is None and not automatic:
        return result
    if record_only:
        review = {**result.get('quick_review', {}), 'status':'skipped',
                  'reason':'explicit_record_only', 'display_order':['capture_receipt']}
        return {**result, 'quick_review':review, 'completion_status':'capture_saved_record_only'}
    if case is None and not (result.get('quick_review') or {}).get('identity'):
        return {**result, 'quick_review':{'status':'not_applicable',
                'reason':'verified_question_identity_required'}}
    from capture_context_408 import render_quick_review
    step = time.perf_counter()
    timings = dict(result.get('timing_ms') or {})
    try:
        review = result.get('quick_review') or {}
        identity = review.get('identity')
        require(identity, '知识点树缺少已核验的当前题身份')
        case = dict(case or {'schema':'cs408-recap-case-v1', 'identity':identity})
        output, inline_root = _recap_destination(repo, result, case, output, inline_root, thread_id)
        rendered = render_quick_review(repo, result, case, output, inline_root=inline_root)
        require(rendered.get('status') == 'rendered' and rendered.get('inline_path_preflight_passed')
                and rendered.get('visualize_reference'), '知识点树尚未完成可点击交付')
        result = {**result, 'quick_review': {**review, **rendered},
                  'completion_status':'capture_and_recap_ready',
                  'display_status':'已快速保存，知识点树已就绪'}
    except (ValueError, OSError, KeyError, RuntimeError) as exc:
        result = {**result, 'completion_status': 'capture_saved_recap_pending',
                  'display_status':'证据已保存，知识点树待补',
                  'next_action': 'recover_recap_with_same_receipt',
                  'quick_review': {**result.get('quick_review', {}), 'status': 'pending', 'error': str(exc)}}
    timings['quick_review_ms'] = (time.perf_counter() - step) * 1000
    timings['request_total_ms'] = (time.perf_counter() - started) * 1000
    return {**result, 'timing_ms': timings}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('command', choices=['inspect', 'record', 'supplement', 'verify'])
    parser.add_argument('--intent', choices=['quick','formal'], default='quick',
                        help='formal seals evidence for an explicit formal request; the formal writer runs separately')
    parser.add_argument('--repo', default=str(Path(__file__).resolve().parents[1]))
    parser.add_argument('--client', choices=sorted(transcripts.CLIENTS))
    parser.add_argument('--thread-id', help='Explicit legacy Codex thread ID')
    parser.add_argument('--session-id', help='Native session ID of --client')
    parser.add_argument('--transcript-path')
    parser.add_argument('--start-anchor')
    parser.add_argument('--end-text')
    parser.add_argument('--feedback-anchor')
    parser.add_argument('--standing-authorization-anchor',
                        help='Exact native user instruction authorizing per-question quick saves')
    parser.add_argument('--package-locator')
    parser.add_argument('--package-root')
    parser.add_argument('--question-id', help='Verified current EXAM408 identity; attaches its original image')
    parser.add_argument('--attachment', action='append', default=[], help='Additional actually used local artifact')
    parser.add_argument('--answer-page-id', action='append', default=[],
                        help='Require complete native read_page evidence for this sidebar answer Page')
    parser.add_argument('--bind-annual', action='store_true', help='Bind the verified Capture to the matching annual chat')
    parser.add_argument('--receipt', help='Existing native receipt for verification/post-commit annual recovery')
    parser.add_argument('--compact', action='store_true', help='Compact result only; original evidence is never trimmed')
    parser.add_argument('--recap-json', help='Current disclosed cs408-recap-case-v1 JSON, or - for stdin')
    parser.add_argument('--recap-output', type=Path, help='Write the clickable recap after verifying the saved episode')
    parser.add_argument('--inline-root', type=Path, help='Task-owned inline visualization directory')
    parser.add_argument('--no-recap', action='store_true', help='Only when the user explicitly requests recording without a summary or tree')
    args = parser.parse_args()
    try:
        started = time.perf_counter()
        require(not (args.no_recap and (args.recap_json or args.recap_output)),
                '--no-recap 不能与总结输入或输出同时使用')
        case = None
        automatic_verify = False
        if args.recap_json:
            require(args.command in {'record','verify'} and args.intent == 'quick', '总结仅用于快存或原回执验证')
            from recap_precision_408 import read_case_input
            case = read_case_input(args.recap_json)
            require(isinstance(case, dict) and case.get('schema') == 'cs408-recap-case-v1', '总结输入类型无效')
            if args.question_id:
                require(case.get('identity') == args.question_id, '总结题号与保存题号不符')
        if args.command == 'verify':
            require(bool(args.package_locator or args.receipt), '缺少 --package-locator 或 --receipt')
            native=episodes.verify_episode_capture(args.repo,args.receipt,package_root=args.package_root) if args.receipt else None
            locator=native['conversation_package_locator'] if native else args.package_locator
            private_root = args.package_root or episodes._default_package_root(Path(args.repo).resolve())
            opened = packages.verify_package_binding(locator, private_root=private_root,
                                                     require_sealed=True)
            result = verify_opened(opened)
            if native:result={**native,**result}
            saved_source = opened['source'].get('source') or {}
            authorization = saved_source.get('authorization') or {}
            if args.bind_annual:
                require(native is not None,'年度恢复需要原生 --receipt，不重放保存')
                inner=(opened['source'].get('source') or {}).get('source') or {}
                payload={'source':inner,'turns':opened['conversation']['turns']}
                result=_finish_annual(args.repo,payload,result,{})
                from syllabus_topic_review_408 import attach_terminal_review
                result=attach_terminal_review(result,inner.get('source_id'))
            automatic_verify = (native is not None and args.intent == 'quick'
                                and authorization.get('kind') != 'explicit_formal_intake')
            if automatic_verify:
                inner=(opened['source'].get('source') or {}).get('source') or {}
                from syllabus_topic_review_408 import attach_terminal_review
                result=attach_terminal_review(result,inner.get('source_id'))
                result['capture_write_count'] = 0
                if _record_only_requested(authorization.get('terminal_user_message')
                                          or authorization.get('user_message')):
                    args.no_recap = True
                recap_thread = os.environ.get('CODEX_THREAD_ID') or inner.get('session_id')
            else:
                require(case is None, '总结恢复需要原生 --receipt')
                recap_thread = None
        else:
            require(bool(args.start_anchor) and bool(args.end_text), '缺少起点标记或保存请求原文')
            require(not (args.thread_id and args.session_id), '不能同时指定两种会话身份参数')
            source = transcripts.resolve_source(client=args.client or ('codex' if args.thread_id else None),
                session_id=args.thread_id or args.session_id, path=args.transcript_path)
            if args.command == 'supplement':
                require(args.receipt and args.question_id, '追加需要原回执及精确题号')
                result = supplement_marked(args.repo, source=source, receipt=args.receipt,
                    start_anchor=args.start_anchor, end_text=args.end_text, question_id=args.question_id,
                    feedback_anchor=args.feedback_anchor, attachments=args.attachment, package_root=args.package_root)
                print(json.dumps(_output(result,args.compact),ensure_ascii=False,indent=2))
                return 0
            if args.command == 'record':
                result=record_marked(args.repo,source=source,start_anchor=args.start_anchor,end_text=args.end_text,
                    question_id=args.question_id,attachments=args.attachment,bind_year=args.bind_annual,
                    feedback_anchor=args.feedback_anchor,package_root=args.package_root,intent=args.intent,
                    standing_authorization_anchor=args.standing_authorization_anchor,
                    answer_page_ids=args.answer_page_id)
                result = _finish_recap(args.repo, result, case, args.recap_output, args.inline_root, started,
                    automatic=args.intent == 'quick',
                    record_only=args.no_recap or _record_only_requested(args.end_text),
                    thread_id=source['thread_id'])
                print(json.dumps(_output(result,args.compact),ensure_ascii=False,indent=2))
                return 0 if result.get('annual_binding_verified',True) and result.get('completion_status') != 'capture_saved_recap_pending' else 2
            payload = build_payload(path=source['transcript_path'], thread_id=source['thread_id'], client=source['client'],
                                    start_anchor=args.start_anchor, end_text=args.end_text,
                                    require_latest=args.command == 'record', feedback_anchor=args.feedback_anchor,
                                    intent=args.intent, annual=args.bind_annual,
                                    standing_authorization_anchor=args.standing_authorization_anchor)
            if args.command == 'inspect':
                result = {'status': 'read_only', 'episode_id': payload['episode_id'],
                          'message_count': len(payload['turns']), 'image_count': len(payload['attachments'])-1,
                          'study_date': payload['study_date'], 'marker': payload['source']['transcript_marker']}
            else:
                result = episodes.record_episode(Path(args.repo), payload, package_root=args.package_root)
        result = _finish_recap(args.repo, result, case, args.recap_output, args.inline_root, started,
            automatic=automatic_verify,
            record_only=args.no_recap,
            thread_id=recap_thread if args.command == 'verify' else None)
        print(json.dumps(_output(result,args.compact), ensure_ascii=False, indent=2))
        return 0 if result.get('annual_binding_verified', True) and result.get('completion_status') != 'capture_saved_recap_pending' else 2
    except (ValueError, OSError, KeyError, sqlite3.Error, RuntimeError) as exc:
        print('BLOCKED: ' + str(exc))
        return 2


if __name__ == '__main__':
    raise SystemExit(main())
