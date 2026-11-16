"""Deterministic additions to native transcript captures; never grades learning."""
from __future__ import annotations
import datetime as dt
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import tempfile

import runtime_paths_408 as _paths

EXAM = re.compile(r'EXAM408-(\d{4})-Q(\d{2})(?:-P\d{2})?\Z')

def require(ok, reason):
    if not ok: raise ValueError(reason)

def _read(path):
    path=Path(path);require(path.is_file() and not path.is_symlink(), 'capture_context_file_missing_or_symlink:'+str(path))
    return json.loads(path.read_bytes())

def _write(path, data):
    path=Path(path);path.parent.mkdir(parents=True,exist_ok=True)
    fd,tmp=tempfile.mkstemp(prefix='.capture-binding-',dir=path.parent)
    try:
        with os.fdopen(fd,'wb') as f:f.write(data);f.flush();os.fsync(f.fileno())
        os.replace(tmp,path)
    finally:
        if os.path.exists(tmp):os.unlink(tmp)

def _json(value):return (json.dumps(value,ensure_ascii=False,indent=2)+'\n').encode()
def _stamp(value):return dt.datetime.fromisoformat(value.replace('Z','+00:00')).timestamp()

def _originals_without_current_template(repo, question_id):
    """A changed teaching template does not invalidate unchanged exam originals."""
    pointer = _read(repo/'.private/study-visuals-3d/current'/f'{question_id}.json')
    require(pointer.get('question_id') == question_id, 'original_source_identity_mismatch')
    details = repo.parent/'kaoyan-408-details'
    card = details/'corpus/408统考真题/cards'/f'{question_id}.md'
    refs = pointer.get('source_files', [])
    cards = [r for r in refs if Path(r['path']).absolute() == card.absolute()]
    require(len(cards) == 1 and not card.is_symlink() and card.is_file(),
            'current_canonical_question_card_required')
    raw = card.read_bytes()
    require(hashlib.sha256(raw).hexdigest() == cards[0]['sha256'], 'canonical_question_card_changed')
    text = raw.decode('utf-8')
    require(re.search(r'^exam_question_id:\s*'+re.escape(question_id)+r'\s*$', text, re.M),
            'canonical_question_card_identity_mismatch')
    images = []
    for ref in refs:
        path = Path(ref['path']).absolute()
        if path.parent.name != question_id or not path.name.startswith('question-'):
            continue
        require(path.is_relative_to(details.absolute()) and path.suffix in {'.png','.jpg','.jpeg','.webp'},
                'canonical_question_image_path_invalid')
        require(str(path.relative_to(details.absolute())) in text, 'question_image_not_in_current_card')
        images.append(ref)
    require(images, 'verified_question_original_missing')
    return images

def _indexed_subquestion_originals(repo, question_id, attachments):
    """Bind a displayed Pxx image to the current reviewed index, without rebuilding visuals."""
    details = (repo.parent/'kaoyan-408-details').resolve()
    index = _read(repo/'support/syllabus-2027/exam-subquestions.json')
    matches = [row for row in index['items'] if row.get('true_item_id') == question_id]
    require(len(matches) == 1, 'unique_indexed_subquestion_required')
    row = matches[0]
    require(row.get('review_status') == 'reviewed', 'reviewed_subquestion_required')
    for field, hash_field in [('card_path', 'card_sha256'), ('payload_path', 'payload_sha256')]:
        path = details/row[field]
        require(path.resolve().is_relative_to(details) and path.is_file() and not path.is_symlink(),
                'indexed_subquestion_source_invalid')
        require(hashlib.sha256(path.read_bytes()).hexdigest() == row[hash_field],
                'indexed_subquestion_source_changed')
    images = []
    for ref in row['question_asset_refs']:
        path = details/ref['ref']
        require(path.resolve().is_relative_to(details) and path.parent.name == question_id,
                'indexed_subquestion_image_invalid')
        images.append({'path': str(path), 'sha256': ref['sha256']})
    displayed_hashes = {hashlib.sha256(Path(path).read_bytes()).hexdigest()
                        for path in attachments if Path(path).is_file() and not Path(path).is_symlink()}
    require(images and all(image['sha256'] in displayed_hashes for image in images),
            'displayed_subquestion_image_must_match_current_index')
    return images

def enrich_payload(repo, payload, *, question_id=None, attachments=()):
    """Keep the original turns/snapshot unchanged; add only verified file evidence."""
    repo=Path(repo);extra=[];seen=set();proofs=[]
    def add(path,label,role='other_attachment',expected=None):
        path=Path(path).expanduser().absolute();require(path.is_file() and not path.is_symlink(),'supplemental_attachment_missing:'+str(path))
        raw=path.read_bytes();digest=hashlib.sha256(raw).hexdigest()
        require(expected is None or digest==expected,'supplemental_attachment_changed:'+str(path))
        if str(path) in seen:return
        seen.add(str(path));import mimetypes
        payload['attachments'].append({'path':str(path),'role':role,'mime_type':mimetypes.guess_type(path.name)[0] or 'application/octet-stream','label':label})
        extra.append({'label':label,'sha256':digest,'role':role})
    if question_id is not None:
        match=EXAM.fullmatch(question_id);require(match and 1<=int(match[2])<=47,'verified_exam_question_id_required')
        import study_visuals_408 as cache
        pointer=cache.get_visual_pointer(repo,question_id,stage='question')
        if pointer.get('status')=='available':
            folder=Path(pointer['preview_path']).parent;manifest=_read(folder/'manifest.json')
            images=[r for r in manifest['files'] if r['name'].startswith('original-question-')]
            require(images,'verified_question_original_missing')
            originals=[{'path':str(folder/r['name']),'sha256':r['sha256']} for r in images]
        elif pointer.get('status')=='stale' and pointer.get('diagnostic')=='exact_native_template_changed':
            originals=_originals_without_current_template(repo,question_id)
        elif (pointer.get('status') == 'stale' and '-P' in question_id
              and pointer.get('diagnostic', '').startswith('source_file_changed:')):
            originals=_indexed_subquestion_originals(repo,question_id,attachments)
        else:
            raise ValueError('current_question_cache_unavailable')
        for n,r in enumerate(originals):add(r['path'],f'已核验原题图{n+1}', 'question_image',r['sha256'])
        payload['source'].update(source_id=question_id,year=int(match[1]),question=f'{question_id}：完整原题见已核验原图。',capture_scope='current_exact_question_only')
        payload['display_binding']['external_source_id']=question_id
        sid=payload['source']['session_id'];require(re.fullmatch(r'[A-Za-z0-9_.:-]+',sid),'unsafe_native_session_identity')
        timestamps=[t.get('observed_at') for t in payload['turns'] if t.get('observed_at')]
        lo,hi=min(map(_stamp,timestamps)),max(map(_stamp,timestamps))
        root=repo/'.private/study-visuals-3d/screen/evidence'/sid/question_id
        for path in sorted(root.glob('*.json')):
            p=_read(path)
            if p.get('session_id')!=sid or p.get('question_id')!=question_id or not lo<=p.get('shown_at',-1)<=hi:continue
            require(p.get('status')=='shown','visual_evidence_not_shown')
            add(path,'实际3D显示回执 '+path.stem)
            for a in p['assets']:add(a['path'],a['label'],expected=a['sha256'])
            proofs.append({'path':str(path),'stage':p['stage'],'lesson_revision':p['lesson_revision']})
    for turn in payload['turns']:
        if turn['role']!='assistant':continue
        for raw in re.findall(r'visualize(\{[^\n]+\})',turn['text']):
            ref=json.loads(raw);path=Path(ref['path']).expanduser().absolute()
            require(path.is_relative_to(_paths.codex_home() / 'visualizations'),'inline_visual_outside_approved_root')
            add(path,'实际聊天图解 '+path.name)
    for n,path in enumerate(attachments):add(path,f'明确补充附件{n+1} '+Path(path).name)
    payload['source']['supplemental_attachment_manifest']=extra
    payload['source']['actual_visual_receipts']=proofs
    return payload

def annual_target(repo,payload):
    qid=payload['source'].get('source_id');match=EXAM.fullmatch(qid or '')
    require(match,'annual_binding_requires_exact_exam_id')
    root=Path(repo)/'真题年度学习'/match[1];doc=_read(root/'学习记录.json')
    require(doc.get('schema')=='cs408-exam-year-learning-record-v1' and doc.get('exam_year')==int(match[1]),'annual_record_identity_mismatch')
    require(doc.get('thread_id')==payload['source'].get('session_id'),'annual_record_thread_mismatch')
    return root

def bind_annual(repo,payload,receipt):
    require(receipt.get('capture_verified') and receipt.get('package_verified'),'annual_binding_requires_verified_capture')
    root=annual_target(repo,payload);qid=payload['source']['source_id'];cid=receipt['capture_id']
    with (root/'.capture-bind.lock').open('a+b') as lock:
        fcntl.flock(lock,fcntl.LOCK_EX);doc=_read(root/'学习记录.json');attempt=doc.setdefault('current_attempt',{})
        records=attempt.setdefault('question_records',[])
        existing=[r for r in records if r.get('capture_id')==cid]
        require(all(r.get('question_id')==qid for r in existing),'annual_capture_identity_conflict')
        if not existing:
            require(doc.get('status') not in {'completed','complete'},'annual_completed_requires_new_attempt')
            marker=payload['source']['transcript_marker']
            records.append({'question_id':qid,'study_date':receipt['study_date'],'date_basis':'native_discussion_capture',
                'capture_id':cid,'conversation_package_locator':receipt['conversation_package_locator'],'receipt_ref':receipt['receipt_ref'],
                'first_response_message_id':marker['start_message_id'],'last_response_message_id':marker['end_message_id'],
                'first_response':payload['turns'][0]['text'],'final_response':payload['turns'][-1]['text'],
                'outcome':'evidence_only_not_assessed_by_writer','independent_retest':None,'status':'captured_awaiting_web_enhancement'})
        ids=attempt.setdefault('capture_ids',[])
        if cid not in ids:ids.append(cid)
        attempt['study_date']=attempt.get('study_date') or receipt['study_date']
        if doc.get('status') in {'prepared_not_started','planned','not_started'}:doc['status']='review_in_progress'
        _write(root/'学习记录.json',_json(doc))
        md=root/'学习记录.md';body=md.read_text() if md.exists() else f'# {doc["exam_year"]}年408学习记录\n'
        marker=f'<!-- capture:{cid} -->'
        if marker not in body:
            body+=f'\n{marker}\n{receipt["study_date"]} {qid}：完整快速保存 {cid}；[原生回执]({receipt["receipt_ref"]})。仅保存证据，未正式判分。\n'
            _write(md,body.encode())
        require(any(r.get('capture_id')==cid for r in _read(root/'学习记录.json')['current_attempt']['question_records']) and marker in md.read_text(),'annual_binding_readback_failed')
    return {'annual_binding_verified':True,'annual_record_path':str(root/'学习记录.json')}


def render_quick_review(repo, receipt, case, output, *, inline_root=None):
    """Render one verified episode's recap without replaying its Capture."""
    import copy
    import local_quality_408 as quality
    from syllabus_topic_review_408 import compact_map, render_compact

    require(receipt.get('capture_verified') and receipt.get('package_verified'),
            'quick_review_requires_verified_capture')
    review = receipt.get('quick_review', {})
    identity = review.get('identity')
    require(EXAM.fullmatch(identity or ''), 'quick_review_requires_exact_question')
    require(isinstance(case, dict) and case.get('identity') == identity,
            'quick_review_case_identity_mismatch')
    require(not review.get('current_disclosed_only'), 'quick_review_requires_disclosed_solution')
    require(receipt.get('annual_binding_verified') is not False,
            'quick_review_requires_finished_annual_binding')

    repo = Path(repo).resolve()
    index, version = quality.catalog(repo)
    mapping = _read(repo/'support/syllabus-2027/question-syllabus-map.json')
    parent = identity.split('-P')[0]
    source = mapping['exam_items'][parent]
    if '-P' in identity:
        unit = next(row for row in source['frequency_primary_units'] if row['unit_id'] == identity)
        points, anchors, primary = unit['assessed_points'], unit['syllabus_ids'], unit['primary_point']
        require(not case.get('recall'), 'independent_subquestion_requires_scoped_recall')
    else:
        points, anchors, primary = source['hit_knowledge'], source['hit_syllabus_ids'], source['main_knowledge'][0]
    point = case.get('point') or primary
    require(point in points, 'quick_review_point_outside_current_question')
    if case.get('anchor'):
        require(case['anchor'] in anchors, 'quick_review_anchor_outside_current_question')

    scoped = copy.deepcopy(case)
    scoped['point'] = point
    options = {'expected_version': version, 'withheld': review.get('withhold_questions', [])}
    result = quality.topic_review(repo, identity, 'direct', case_input=scoped, **options)
    if result['status'] == 'topic_review_withheld':
        # Keep the year's exam material private. Only already-authorized point
        # content and caller-supplied current-question parameters are projected.
        scoped['identity'] = point
        result = quality.topic_review(repo, point, 'direct', case_input=scoped, **options)
    require(result['status'] == 'available', 'quick_review_materials:'+result['status'])
    recap = compact_map(result['review'], result['exact_identity'], **result['mechanism_selection'])
    current = next(node for node in recap['nodes'] if node['current'])
    point_data = quality.blob(repo/quality.ROOT, index['objects'], point)
    frequency = point_data['syllabus']['exam_frequency']
    require(frequency.get('frequency_scope') == 'canonical_point', 'quick_review_frequency_scope_missing')
    current.update(title=point_data['title'], label=point_data['title'],
                   main_count=frequency['main_count'], secondary_count=frequency['secondary_only_count'],
                   tier=frequency['main_frequency_level'], frequency_scope='canonical_point', point_id=point)
    if '-P' in identity:
        recap['nodes'] = [current]
    rendered = render_compact(recap, output, inline_root=inline_root)
    require(rendered.get('inline_path_preflight_passed'), 'quick_review_inline_path_unavailable')
    return {**rendered, 'source_question_id': identity, 'render_identity': result['identity'],
            'catalog_sha256': version, 'frequency': frequency,
            'summary': {key: current.get(key, '') for key in ('summary', 'action', 'rule')},
            'browser_check_required': False, 'learning_write_count': 0}
