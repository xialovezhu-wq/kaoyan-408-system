#!/usr/bin/env python3
"""Source-bound independent exam units; no learner facts or scheduling writes.

Complete statements stay in the details vault.  The repository receives only
answer-safe identity, taxonomy and dependency metadata.  Existing style releases
and active sessions retain their original commitments.
"""
from __future__ import annotations

import argparse
import copy
import csv
import datetime as dt
import hashlib
import json
import os
import re
import tempfile
from pathlib import Path

import runtime_paths_408 as _paths

ROOT = Path(__file__).resolve().parents[1]
DETAILS = _paths.details_root()
CORPUS_REL = Path('corpus/408统考真题')
SUB_REL = CORPUS_REL / 'subquestions'
UNIT_REL = CORPUS_REL / 'practice-units'
SAFE_REL = Path('support/syllabus-2027/exam-subquestions.json')
ID_RE = re.compile(r'EXAM408-20\d{2}-Q\d{2}(?:-P\d{2})?')
CHILD_RE = re.compile(r'(EXAM408-20\d{2}-Q\d{2})-P(\d{2})')


def encoded(value):
    return (json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':'))+'\n').encode()


def digest(value):
    return hashlib.sha256(value if isinstance(value, bytes) else encoded(value)).hexdigest()


def file_sha(path):
    return digest(Path(path).read_bytes())


def read(path):
    return json.loads(Path(path).read_text(encoding='utf-8'))


def write(path, value, mode=0o644):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    content = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False, indent=2)+'\n'
    fd, name = tempfile.mkstemp(prefix='.'+path.name+'-', dir=path.parent)
    try:
        with os.fdopen(fd, 'w', encoding='utf-8') as f:
            f.write(content)
            f.flush()
            os.fsync(f.fileno())
        os.chmod(name, mode)
        os.replace(name, path)
    finally:
        if os.path.exists(name):
            os.unlink(name)


def managed(details, ref):
    root = Path(details).resolve()
    p = Path(str(ref))
    if p.is_absolute() or '..' in p.parts:
        raise ValueError('practice_unit_asset_ref_unsafe')
    result = (root / p).resolve(strict=True)
    result.relative_to(root)
    if not result.is_file():
        raise ValueError('practice_unit_asset_not_file')
    return result


def asset_record(details, ref):
    p = managed(details, ref)
    return {'ref':str(ref), 'sha256':file_sha(p), 'size_bytes':p.stat().st_size}


def _corpus(details):
    path = Path(details)/CORPUS_REL/'indexes/question-index.csv'
    with path.open(encoding='utf-8-sig', newline='') as f:
        rows = {r['exam_question_id']:r for r in csv.DictReader(f)}
    return path, rows


def _taxonomies(repo):
    labels = dict(re.findall(r'^-\s+((?:DS|CO|OS|CN)\d{2}-\d{2})\s+(.+)$',
                            (Path(repo)/'知识点标签表.md').read_text(), re.M))
    catalog = read(Path(repo)/'support/syllabus-2027/catalog.json')
    nodes = catalog.get('nodes', catalog.get('entries', []))
    syllabus = set(nodes) if isinstance(nodes, dict) else {r['id'] for r in nodes}
    return labels, syllabus


def _closure(parts, number):
    """Source dependencies identify earlier results; they are not learner tasks."""
    visiting, done, result = set(), set(), []
    def visit(n):
        if n in visiting:
            raise ValueError('subquestion_dependency_cycle')
        if n in done:
            return
        if n not in parts:
            raise ValueError('subquestion_dependency_missing')
        visiting.add(n)
        for prior in parts[n].get('prerequisite_part_nos', []):
            if not isinstance(prior, int) or prior >= n:
                raise ValueError('subquestion_dependency_not_earlier')
            visit(prior)
        visiting.remove(n)
        done.add(n)
        if n != number:
            result.append(n)
    visit(number)
    return result


def _asset_refs(values):
    return [str(v.get('ref') or v.get('path')) if isinstance(v, dict) else str(v) for v in values]


def assemble_parent(parent, corpus, labels, syllabus):
    pid = parent['parent_true_item_id']
    if pid not in corpus or int(corpus[pid]['official_question_no']) < 41:
        raise ValueError('subquestion_parent_not_comprehensive:'+pid)
    source = corpus[pid]
    if parent.get('parent_question_sha256') != source['question_ocr_normalized_sha256']:
        raise ValueError('subquestion_parent_source_drift:'+pid)
    if parent.get('review_status') not in {'verified', 'reviewed'}:
        raise ValueError('subquestion_parent_unreviewed:'+pid)
    if not str(parent.get('shared_context', '')).strip():
        raise ValueError('subquestion_shared_conditions_missing:'+pid)
    parts = {p['part_no']:p for p in parent.get('parts', [])}
    if not parts or len(parts) != len(parent['parts']) or set(parts) != set(range(1, len(parts)+1)):
        raise ValueError('subquestion_numbers_invalid:'+pid)
    result = []
    for number, part in sorted(parts.items()):
        identity = f'{pid}-P{number:02}'
        points = list(dict.fromkeys(part.get('assessed_points', [])))
        primary = part.get('primary_point')
        if not points or primary not in points or set(points)-labels.keys():
            raise ValueError('subquestion_taxonomy_invalid:'+identity)
        if not part.get('syllabus_ids') or set(part['syllabus_ids'])-syllabus:
            raise ValueError('subquestion_syllabus_invalid:'+identity)
        if part.get('review_status') not in {'verified', 'reviewed'} or not part.get('prompt') or not part.get('keywords'):
            raise ValueError('subquestion_semantic_review_missing:'+identity)
        prior = _closure(parts, number)
        dependency = 'supplied_prerequisite_results' if prior else 'independent'
        if part.get('dependency_kind') != dependency:
            raise ValueError('subquestion_dependency_label_mismatch:'+identity)
        given = str(part.get('provided_prerequisite_results') or '').strip()
        if prior and not given:
            raise ValueError('subquestion_completed_prerequisite_results_missing:'+identity)
        conditions = [str(part.get('standalone_context') or parent['shared_context']).strip()]
        extra = str(part.get('extra_conditions') or '').strip()
        if extra and extra not in conditions:
            conditions.append(extra)
        text = '\n\n'.join(conditions)
        if given:
            text += '\n\n已给定的前问结果（直接作为本问输入，无需重新求解）：\n'+given
        text += '\n\n本题要求：\n'+part['prompt'].strip()
        assets = list(dict.fromkeys(_asset_refs(parent.get('shared_assets', []))+
                                   _asset_refs(part.get('condition_assets', []))))
        row = {'true_item_id':identity, 'canonical_id':identity, 'subquestion_id':identity,
               'parent_true_item_id':pid, 'parent_exam_id':pid, 'part_no':number,
               'source_part_label':part['source_part_label'], 'year':int(source['year']),
               'official_question_no':int(source['official_question_no']), 'subject':source['subject'],
               'title':part['title'], 'keywords':list(dict.fromkeys(part['keywords'])),
               'primary_point':primary, 'main_knowledge':primary+' '+labels[primary],
               'assessed_points':points, 'knowledge_points':points,
               'syllabus_ids':list(dict.fromkeys(part['syllabus_ids'])),
               'question_type':part['question_type'], 'dependency_kind':dependency,
               'prerequisite_part_nos':prior, 'provided_prerequisite_results':given,
               'contact_dependency_ids':[f'{pid}-P{n:02}' for n in prior],
               'question_text':text, 'condition_assets':assets,
               'review_status':'reviewed', 'review_note':part.get('review_note', ''),
               'condition_provenance':part.get('condition_provenance', parent.get('condition_provenance', [])),
               'single_target_only':True,
               'parent_question_sha256':parent['parent_question_sha256'],
               'source_pdf_path':source['question_source_path'],
               'source_pdf_sha256':source['question_source_sha256'],
               'question_pages':json.loads(source['question_pages']),
               'solution_images':json.loads(source['solution_assets']),
               'solution_reference_scope':'parent_question_reference_only',
               'source_authority':('user_supplied_third_party_corrected_transcription' if int(source['year'])==2026 else 'registered_exam_corpus')}
        result.append(row)
    return result


def _wrap(text, font, width):
    lines = []
    for raw in text.expandtabs(4).splitlines():
        if not raw:
            lines.append('')
            continue
        line = ''
        for ch in raw:
            if line and font.getlength(line+ch) > width:
                lines.append(line)
                line = ''
            line += ch
        lines.append(line)
    return lines


def render_question(details, row):
    """Readable deterministic text + source figures, no generated subject matter."""
    from PIL import Image, ImageDraw, ImageFont
    from fontTools.ttLib import TTFont
    font_path = '/System/Library/Fonts/STHeiti Medium.ttc'
    required = {ord(char) for char in row['question_text'] if ord(char) > 32}
    def glyphs(path):
        with TTFont(path, fontNumber=0) as face:
            return set(face.getBestCmap() or {})
    if not required <= glyphs(font_path):
        fallback = '/Library/Fonts/Arial Unicode.ttf'
        if not Path(fallback).is_file() or not required <= glyphs(fallback):
            raise ValueError('practice_question_font_missing_glyph:'+row['true_item_id'])
        font_path = fallback
    font = ImageFont.truetype(font_path, 32)
    small = ImageFont.truetype(font_path, 26)
    width, margin, line_h = 1440, 64, 49
    title = f"{row['year']} 年 408 第 {row['official_question_no']} 题 · {row['source_part_label']}"
    # Knowledge labels are intentionally omitted from learner surfaces.
    display_text='\n'.join(line for line in row['question_text'].splitlines()
                           if not line.strip().startswith('```'))
    lines = _wrap(display_text, font, width-2*margin)
    figures = []
    for ref in row['condition_assets']:
        im = Image.open(managed(details, ref)).convert('RGB')
        factor = min(1, (width-2*margin)/im.width)
        im = im.resize((int(im.width*factor), int(im.height*factor)))
        figures.append(im)
    height = margin*2+64+len(lines)*line_h+sum(im.height+36 for im in figures)+60
    canvas = Image.new('RGB', (width, height), 'white')
    draw = ImageDraw.Draw(canvas)
    draw.text((margin, margin), title, font=small, fill='#42536a')
    y = margin+64
    for line in lines:
        draw.text((margin, y), line, font=font, fill='#171d26')
        y += line_h
    for im in figures:
        y += 24
        canvas.paste(im, (margin, y))
        y += im.height+12
    draw.text((margin, height-margin), row['true_item_id'], font=small, fill='#5c6876')
    ref = (SUB_REL/'assets'/row['true_item_id']/'question.png').as_posix()
    path = Path(details)/ref
    path.parent.mkdir(parents=True, exist_ok=True)
    canvas.save(path, optimize=True)
    return [ref]


SAFE_FIELDS = ('true_item_id','canonical_id','subquestion_id','parent_true_item_id','parent_exam_id',
               'part_no','source_part_label','year','official_question_no','subject','title','keywords',
               'primary_point','main_knowledge','assessed_points','knowledge_points','syllabus_ids',
               'question_type','dependency_kind','prerequisite_part_nos','contact_dependency_ids',
               'review_status','parent_question_sha256','source_pdf_sha256','source_authority',
               'question_asset_refs','surface_sha256','question_images','card_path','payload_path',
               'card_sha256','payload_sha256')


def safe_row(row):
    return {key:copy.deepcopy(row[key]) for key in SAFE_FIELDS if key in row}


def _material(row):
    return {'schema':'cs408-exam-practice-unit-surface-v1', 'true_item_id':row['true_item_id'],
            'parent_true_item_id':row.get('parent_true_item_id'),
            'question_asset_refs':row['question_asset_refs'],
            'question_text_sha256':digest(row.get('question_text','').encode()),
            'semantic_sha256':row['semantic_sha256']}


def build(repo=ROOT, details=DETAILS, *, allow_partial=False, question_ids=None):
    repo, details = Path(repo), Path(details)
    index, corpus = _corpus(details)
    labels, syllabus = _taxonomies(repo)
    parents = []
    reviews = []
    for subject in ('DS','CO','OS','CN'):
        path = details/SUB_REL/f'review-{subject}.json'
        if not path.exists():
            if allow_partial:
                continue
            raise ValueError('subject_subquestion_review_missing:'+subject)
        data = read(path)
        if data.get('schema')!='cs408-exam-subquestion-review-v1' or data.get('subject')!=subject:
            raise ValueError('subject_subquestion_review_invalid:'+subject)
        parents += data['parents']
        reviews.append({'path':str(path.relative_to(details)), 'sha256':file_sha(path)})
    ids = [p['parent_true_item_id'] for p in parents]
    expected = {k for k,r in corpus.items() if int(r['official_question_no'])>=41}
    if len(ids)!=len(set(ids)) or (not allow_partial and set(ids)!=expected):
        raise ValueError('subquestion_parent_coverage_mismatch:'+str(sorted(expected-set(ids))))
    selected = set(question_ids) if question_ids is not None else None
    prior_items = {}
    if selected is not None:
        if allow_partial or not selected or any(not CHILD_RE.fullmatch(q) for q in selected):
            raise ValueError('subquestion_bounded_build_scope_invalid')
        old = read(details/SUB_REL/'index.json')
        if old.get('status') != 'accepted':
            raise ValueError('subquestion_bounded_build_requires_accepted_index')
        prior_items = {row['true_item_id']:row for row in old['items']}
        if not selected <= prior_items.keys():
            raise ValueError('subquestion_bounded_build_unknown_identity')
        # Validate the entire untouched boundary before rendering or replacing
        # any selected bytes. A concurrent unrelated edit must fail closed.
        for parent in parents:
            for row in assemble_parent(parent, corpus, labels, syllabus):
                if row['true_item_id'] in selected:
                    continue
                previous = prior_items.get(row['true_item_id'])
                if previous is None or previous['semantic_sha256'] != digest(row):
                    raise ValueError('unselected_subquestion_semantic_changed:'+row['true_item_id'])
                for field in ('card', 'payload'):
                    if file_sha(managed(details,previous[field+'_path'])) != previous[field+'_sha256']:
                        raise ValueError('unselected_subquestion_source_drift:'+row['true_item_id'])
                for asset in previous['question_asset_refs']:
                    if asset_record(details,asset['ref']) != asset:
                        raise ValueError('unselected_subquestion_asset_drift:'+row['true_item_id'])
    items = []
    rebuilt = 0
    for parent in sorted(parents, key=lambda p:p['parent_true_item_id']):
        for row in assemble_parent(parent, corpus, labels, syllabus):
            row['semantic_sha256'] = digest(row)
            if selected is not None and row['true_item_id'] not in selected:
                items.append(copy.deepcopy(prior_items[row['true_item_id']]))
                continue
            row['question_images'] = render_question(details, row)
            rebuilt += 1
            row['question_asset_refs'] = [asset_record(details, p) for p in row['question_images']]
            row['surface_commitment'] = _material(row)
            row['surface_sha256'] = digest(row['surface_commitment'])
            row['card_path'] = (SUB_REL/'cards'/(row['true_item_id']+'.md')).as_posix()
            row['payload_path'] = (SUB_REL/'items'/(row['true_item_id']+'.json')).as_posix()
            write(details/row['payload_path'], row)
            images = '\n\n'.join(f'![[{p}]]' for p in row['condition_assets'])
            text = (f"---\ntype: true_exam_subquestion\ntrue_item_id: {row['true_item_id']}\n"
                    f"parent_true_item_id: {row['parent_true_item_id']}\nreview_status: reviewed\n---\n\n"
                    f"# {row['true_item_id']} · {row['title']}\n\n"
                    f"来源：{row['year']} 年第 {row['official_question_no']} 题 {row['source_part_label']}。\n\n"
                    +row['question_text']+'\n\n'+images+'\n\n'
                    f"题型：{row['question_type']}。依赖：{row['dependency_kind']}。\n\n"
                    f"原大题与参考解析：[[corpus/408统考真题/cards/{row['parent_true_item_id']}]]。\n")
            write(details/row['card_path'], text)
            row['card_sha256']=file_sha(details/row['card_path'])
            row['payload_sha256']=file_sha(details/row['payload_path'])
            items.append(row)
    data = {'schema':'cs408-exam-subquestions-v1','source_sha256':file_sha(index),
            'parent_count':len(parents),'item_count':len(items),'reviews':reviews,'items':items,
            'status':'partial' if allow_partial else 'accepted', 'learner_fact_write_count':0}
    name = 'draft-index.json' if allow_partial else 'index.json'
    write(details/SUB_REL/name, data)
    if allow_partial:
        return {k:data[k] for k in ('status','parent_count','item_count')}
    safe = {**data,'items':[safe_row(row) for row in items]}
    write(repo/SAFE_REL, safe)
    _build_practice_units(repo, details, corpus, data)
    nav = ['# 408 综合题独立小题索引','',
           f"覆盖 {len(parents)} 道大题，共 {len(items)} 个子题；每题只做当前小问，题设、图表与必要的前问结果已给齐。",'',
           '按知识点关键词组织。前问结果直接作为输入，不需重做前问；当前小问答案不展示。','']
    for row in sorted(items,key=lambda r:(r['primary_point'],r['keywords'],r['true_item_id'])):
        dep = '（前问结果已给定）' if row['prerequisite_part_nos'] else ''
        nav.append(f"- [[{row['card_path'][:-3]}|{row['true_item_id']} {row['title']}]] {dep}")
    write(details/SUB_REL/'索引.md', '\n'.join(nav)+'\n')
    return {**{k:data[k] for k in ('status','parent_count','item_count')},
            'rebuilt_item_count':rebuilt,'bounded_question_ids':sorted(selected) if selected is not None else None}


def _build_practice_units(repo, details, corpus, subquestions):
    items = copy.deepcopy(subquestions['items'])
    # 2026 multiple-choice originals join the same strictly source-bound extension.
    acceptance_path = Path(repo)/'support/syllabus-2027/exam-corpus-acceptance.json'
    acceptance = read(acceptance_path) if acceptance_path.exists() else {}
    if 2026 in acceptance.get('accepted_years',[]) and acceptance.get('status')=='accepted':
        reviews = {}
        for subject in ('DS','CO','OS','CN'):
            reviews.update(read(Path(repo)/f'support/syllabus-2027/exam-review-{subject}.json')['questions'])
        for identity, source in sorted(corpus.items()):
            if int(source['year'])!=2026 or int(source['official_question_no'])>40:
                continue
            rv = reviews[identity]
            points = rv['assessed_points']
            row = {'true_item_id':identity,'canonical_id':identity,'parent_true_item_id':None,
                   'year':2026,'official_question_no':int(source['official_question_no']),
                   'subject':source['subject'],'primary_point':rv['primary_point'],
                   'main_knowledge':source['main_knowledge'],'knowledge_points':points,
                   'assessed_points':points,'keywords':list(dict.fromkeys(p['focus'] for p in rv['parts'])),
                   'question_type':'单项选择题','dependency_kind':'independent',
                   'review_status':'reviewed','question_text':source['question_ocr'],
                   'question_images':json.loads(source['question_assets']),
                   'solution_images':json.loads(source['solution_assets']),
                   'card_path':(CORPUS_REL/'cards'/(identity+'.md')).as_posix(),
                   'payload_path':(UNIT_REL/'items'/(identity+'.json')).as_posix(),
                   'source_pdf_path':source['question_source_path'],
                   'source_pdf_sha256':source['question_source_sha256'],
                   'source_authority':'user_supplied_third_party_corrected_transcription'}
            row['semantic_sha256'] = digest(row)
            row['question_asset_refs']=[asset_record(details,p) for p in row['question_images']]
            row['surface_commitment']=_material(row)
            row['surface_sha256']=digest(row['surface_commitment'])
            write(details/row['payload_path'],row)
            row['card_sha256']=file_sha(details/row['card_path'])
            row['payload_sha256']=file_sha(details/row['payload_path'])
            items.append(row)
    for row in items:
        parent = row.get('parent_true_item_id') or row['true_item_id']
        source = corpus[parent]
        capsule = {'schema':'cs408-exam-practice-reference-v1', 'true_item_id':row['true_item_id'],
                   'parent_true_item_id':parent, 'surface_sha256':row['surface_sha256'],
                   'reference_scope':'parent_reference_filter_to_selected_part',
                   'selected_part':row.get('source_part_label'),
                   'current_question_text':row.get('question_text'),
                   'provided_prerequisite_results':row.get('provided_prerequisite_results'),
                   'solution_ocr':source['solution_ocr'],
                   'solution_asset_refs':[asset_record(details,p) for p in row['solution_images']],
                   'source_authority':row['source_authority'],
                   'grading_mode':'semantic_current_answer_review',
                   'no_automatic_option_scoring':True}
        ref = (UNIT_REL/'private'/(row['true_item_id']+'.json')).as_posix()
        write(details/ref,capsule,mode=0o600)
        row['answer_capsule_commitment_ref']=ref
        row['answer_capsule_commitment_sha256']=file_sha(details/ref)
        row['answer_capsule_id']=row['answer_capsule_commitment_sha256']
        row['practice_unit_id']=row['true_item_id']
    value = {'schema':'cs408-exam-practice-units-v1','status':'accepted',
             'source_sha256':subquestions['source_sha256'], 'item_count':len(items),'items':items}
    value['content_sha256']=digest(value)
    write(details/UNIT_REL/'index.json',value)


def load_units(details, *, verify_source=True):
    path = Path(details)/UNIT_REL/'index.json'
    if not path.exists():
        return {}
    value = read(path)
    material = {k:v for k,v in value.items() if k!='content_sha256'}
    if value.get('schema')!='cs408-exam-practice-units-v1' or value.get('status')!='accepted' or digest(material)!=value.get('content_sha256'):
        raise ValueError('practice_unit_index_binding_invalid')
    if verify_source and file_sha(Path(details)/CORPUS_REL/'indexes/question-index.csv')!=value['source_sha256']:
        raise ValueError('practice_unit_source_index_drift')
    rows = {row['true_item_id']:row for row in value['items']}
    if len(rows)!=value['item_count']:
        raise ValueError('practice_unit_identity_duplicate')
    return rows


def verified_unit(details, identity):
    row = load_units(details).get(identity)
    if row is None:
        raise ValueError('practice_unit_missing:'+identity)
    for asset in row['question_asset_refs']:
        current = asset_record(details,asset['ref'])
        if any(current[k]!=asset[k] for k in current):
            raise ValueError('practice_unit_question_asset_drift:'+identity)
    capsule = managed(details,row['answer_capsule_commitment_ref'])
    if file_sha(capsule)!=row['answer_capsule_commitment_sha256']:
        raise ValueError('practice_unit_answer_capsule_drift:'+identity)
    if row['surface_commitment']!=_material(row) or digest(_material(row))!=row['surface_sha256']:
        raise ValueError('practice_unit_surface_binding_drift:'+identity)
    for kind in ('card','payload'):
        if file_sha(managed(details,row[kind+'_path']))!=row[kind+'_sha256']:
            raise ValueError('practice_unit_'+kind+'_drift:'+identity)
    return copy.deepcopy(row)


def unit_commitments(details, identity):
    row=verified_unit(details,identity)
    return {k:row[k] for k in ('question_asset_refs','surface_commitment','surface_sha256',
                              'answer_capsule_commitment_ref','answer_capsule_commitment_sha256','answer_capsule_id')}


def overlay_release(release, details):
    result = copy.deepcopy(release)
    for identity,row in load_units(details).items():
        result['exposure_by_id'][identity] = {
            'canonical_id':identity,'practice_unit_id':identity,
            'parent_true_item_id':row.get('parent_true_item_id'),
            'main_knowledge':row['primary_point'],'knowledge_points':row['knowledge_points'],
            'keywords':row['keywords'],'dependency_kind':row['dependency_kind'],
            'contact_dependency_ids':row.get('contact_dependency_ids', []),
            'question_type':row['question_type'],'year':row['year'],
            'official_question_no':row['official_question_no'],
            'production_usable':True,'direct_replay_allowed':True,'exposure_mode':'direct_pool',
            'direct_surface_status':'ready','style_observation_allowed':False,
            'answer_content_in_release':False,'question_asset_refs':row['question_asset_refs'],
            'surface_sha256':row['surface_sha256'],
            'answer_capsule_commitment_ref':row['answer_capsule_commitment_ref'],
            'answer_capsule_commitment_sha256':row['answer_capsule_commitment_sha256']}
    return result


def published_units(details):
    fields=('parent_true_item_id','main_knowledge','knowledge_points','keywords','dependency_kind',
            'question_type','surface_sha256','question_images','solution_images','card_path','payload_path',
            'source_pdf_path','source_pdf_sha256','source_authority','year','official_question_no',
            'contact_dependency_ids')
    return [{'question_id':row['true_item_id'], **{k:row[k] for k in fields if k in row}}
            for row in load_units(details).values()]


def select_surface(details, release, *, identity, review_date, formal_node_id,
                   formal_node_sha256, exposure_set, main_knowledge=None):
    row=verified_unit(details, identity)
    if main_knowledge and main_knowledge not in row['knowledge_points']:
        raise ValueError('practice_unit_target_point_mismatch')
    day=dt.date.fromisoformat(review_date)
    raw={}
    for exposure in exposure_set.get('rows',[]):
        key=exposure.get('true_item_id') or exposure.get('source_id')
        observed=exposure.get('review_date') or exposure.get('study_date')
        if key and observed and (key not in raw or raw[key]['study_date']<observed):
            raw[key]={'study_date':observed}
    projected=contact_projection(raw,load_units(details).values())
    fact=projected.get(identity)
    gap=(day-dt.date.fromisoformat(fact['study_date'])).days if fact else None
    window=max(14,int(exposure_set.get('window_days') or 14))
    if gap is not None and gap<window:
        return {'status':'content_blocked','reason':'practice_unit_recent_contact', 'formal_write_count':0}
    material={'schema':'exam_variant_surface_selection_v1','status':'selected',
              'surface_kind':'canonical_original','practice_unit_id':identity,
              'parent_true_item_id':row.get('parent_true_item_id'),
              'source_part_label':row.get('source_part_label'),
              'question_type':row['question_type'],'keywords':row['keywords'],
              'source_authority':row['source_authority'],
              'dependency_kind':row['dependency_kind'],
              'review_date':review_date,'formal_node_id':formal_node_id,
              'formal_node_sha256':formal_node_sha256,'true_item_id':identity,'canonical_id':identity,
              'exam_style_release_id':release['release_id'],
              'exam_style_release_content_sha256':release['content_sha256'],
              'cooldown_gap_days':gap,**unit_commitments(details,identity),
              'answer_content_in_selection':False,'formal_node_created':False,'formal_write_count':0}
    return {**material,'selection_id':'EVSEL-'+digest(material)[:24].upper()}


def learner_surface(details, item, selection):
    identity=selection.get('practice_unit_id')
    row=verified_unit(details,identity)
    if (selection.get('true_item_id')!=identity or item.get('source_id')!=identity
            or item.get('surface_sha256')!=row['surface_sha256']
            or item.get('grading_sha256')!=row['answer_capsule_commitment_sha256']
            or selection.get('surface_commitment')!=row['surface_commitment']):
        raise ValueError('practice_unit_display_binding_drift')
    assets=[{**a,'path':str(managed(details,a['ref']))} for a in row['question_asset_refs']]
    capsule=managed(details,row['answer_capsule_commitment_ref'])
    if capsule.stat().st_mode & 0o077:
        raise ValueError('practice_unit_private_reference_permissions')
    return {'stem':'\n'.join(f"![题目 {n}]({a['path']})" for n,a in enumerate(assets,1)),
            'options':{},'option_ids_by_letter':{},
            'response_instruction':'请直接写出计算、推理或程序。' if row.get('subquestion_id') else '请选择题图中的选项并说明理由。',
            'surface_object_path':assets[0]['path'],'surface_object_sha256':row['surface_sha256'],
            'grading_capsule_ref':row['answer_capsule_commitment_ref'],
            'grading_capsule_path':str(capsule),'grading_capsule_sha256':row['answer_capsule_commitment_sha256'],
            'question_asset_ref':assets[0]['ref'],'question_asset_sha256':assets[0]['sha256'],
            'question_assets':assets,
            'terminal_evidence_assets':[{'path':str(capsule),'sha256':file_sha(capsule),
                 'size_bytes':capsule.stat().st_size,'role':'other_attachment','label':'verified grading and explanation capsule'}]}


def contact_projection(contacts, items):
    """Parent exposure reaches children; a unit records its target and revealed results.

    A child also blocks whole-parent replay. It must not create sibling exposure
    by bouncing that derived parent date back into the sibling set.
    """
    items=list(items)
    result=copy.deepcopy(contacts)
    original=copy.deepcopy(contacts)
    # Facts for targets or answers actually contained in a displayed unit, kept separate from
    # the derived whole-parent replay block to avoid a parent/sibling feedback loop.
    touched=copy.deepcopy(contacts)
    def offer_into(mapping,target,fact,basis):
        if not fact or not fact.get('study_date'):
            return
        prior=mapping.get(target)
        if prior is None or prior.get('study_date','')<fact['study_date']:
            mapping[target]={**copy.deepcopy(fact),'basis':basis,
                             'contact_projection':'source_bound_subquestion'}
    for row in items:
        identity=row['true_item_id']; parent=row.get('parent_true_item_id')
        if not parent:
            continue
        offer_into(touched,identity,original.get(parent),'whole_parent_actual_contact')
        for dep in row.get('contact_dependency_ids',[]):
            offer_into(touched,dep,original.get(identity),'prerequisite_result_disclosed')
    for row in items:
        identity=row['true_item_id']; parent=row.get('parent_true_item_id')
        if not parent:
            continue
        offer_into(result,identity,touched.get(identity),'actual_subquestion_content_contact')
        offer_into(result,parent,original.get(identity),'contained_subquestion_actual_contact')
        # Reusing the same given result in a different target is allowed. The
        # learner does not redo that prior task, so it must not block the target.
    return result


def main():
    parser=argparse.ArgumentParser()
    parser.add_argument('operation',choices=['build','audit'])
    parser.add_argument('--repo',type=Path,default=ROOT)
    parser.add_argument('--details',type=Path,default=DETAILS)
    parser.add_argument('--allow-partial',action='store_true')
    parser.add_argument('--question-id',action='append',help='Rebuild only named existing units; verify and preserve every other committed unit')
    args=parser.parse_args()
    if args.operation=='build':
        result=build(args.repo,args.details,allow_partial=args.allow_partial,question_ids=args.question_id)
    else:
        rows=load_units(args.details)
        identities=set(args.question_id) if args.question_id else set(rows)
        if not identities <= rows.keys():
            raise ValueError('practice_unit_audit_unknown_identity')
        for identity in identities:
            verified_unit(args.details,identity)
        result={'status':'PASS','practice_unit_count':len(rows),'audited_count':len(identities),
                'subquestion_count':sum(bool(CHILD_RE.fullmatch(x)) for x in rows)}
    print(json.dumps(result,ensure_ascii=False))


if __name__=='__main__':
    main()
