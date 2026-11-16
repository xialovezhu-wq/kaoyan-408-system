#!/usr/bin/env python3
"""Reviewed exam/point bipartite links; background is not assessed evidence."""
from __future__ import annotations

import argparse
import copy
import hashlib
import json
import re
from collections import Counter
from pathlib import Path

ROOT_REL=Path('support/syllabus-2027')
SUBJECTS=('DS','CO','OS','CN')
POINT=re.compile(r'(?:DS|CO|OS|CN)\d{2}-\d{2}')
SUBQUESTION_RE=re.compile(r'EXAM408-20\d{2}-Q(?:4[1-7])-P\d{2}')
SUBQUESTIONS_FILENAME='exam-subquestions.json'
PRIMARY_COUNT_POLICY='choice_or_subquestion_primary_parent_dedup_v1'
PRIMARY_ANCHOR_REVIEW_FILENAME='frequency-primary-anchor-review.json'

def _read(path):return json.loads(Path(path).read_text(encoding='utf-8'))
def _sha(path):return hashlib.sha256(Path(path).read_bytes()).hexdigest()
def _write(path,data):
    path=Path(path);path.parent.mkdir(parents=True,exist_ok=True)
    path.write_text(json.dumps(data,ensure_ascii=False,indent=2)+'\n',encoding='utf-8')

def load_reviews(repo,corpus_rows,source_sha256):
    repo=Path(repo)
    paths=[repo/ROOT_REL/f'exam-review-{s}.json' for s in SUBJECTS]
    if not any(p.exists() for p in paths):return None
    if not all(p.is_file() for p in paths):raise ValueError('incomplete_exam_review_set')
    from knowledge_profile_408 import _taxonomy
    taxonomy=_taxonomy(repo)
    nodes={n['id'] for n in _read(repo/ROOT_REL/'catalog.json')['nodes']}
    merged={};hashes={}
    for subject,path in zip(SUBJECTS,paths):
        data=_read(path)
        if data.get('schema')!='cs408-exam-point-review-v1' or data.get('source_sha256')!=source_sha256:
            raise ValueError('exam_review_source_binding:'+subject)
        expected={row['exam_question_id'] for row in corpus_rows if row['subject']==subject}
        if set(data['questions'])!=expected:raise ValueError('exam_review_exact_set:'+subject)
        for qid,row in data['questions'].items():
            if row.get('review_status')!='reviewed' or row.get('missing_evidence'):
                raise ValueError('exam_review_not_complete:'+qid)
            role_sets=[]
            for role in ('assessed_points','prerequisite_points','context_only_points'):
                values=row[role]
                if len(values)!=len(set(values)) or not set(values)<=set(taxonomy):
                    raise ValueError('exam_review_role_points:'+qid+':'+role)
                role_sets.append(set(values))
            if any(role_sets[i]&role_sets[j] for i in range(3) for j in range(i)):
                raise ValueError('exam_review_roles_overlap:'+qid)
            if row['primary_point'] not in role_sets[0]:raise ValueError('exam_review_primary_not_assessed:'+qid)
            if not row['parts']:raise ValueError('exam_review_no_parts:'+qid)
            union=set()
            for part in row['parts']:
                if not part.get('part_id') or not part.get('focus'):
                    raise ValueError('exam_review_part_incomplete:'+qid)
                if not set(part['canonical_points'])<=set(taxonomy) or not part['syllabus_ids'] or not set(part['syllabus_ids'])<=nodes:
                    raise ValueError('exam_review_part_mapping:'+qid)
                union.update(part['canonical_points'])
            if union!=role_sets[0]:raise ValueError('exam_review_part_union:'+qid)
            if any(k in row for k in ('question_ocr','solution_ocr','answer','options','full_question')):
                raise ValueError('exam_review_contains_raw_question:'+qid)
            merged[qid]=copy.deepcopy(row)
        hashes[path.name]=_sha(path)
    return {'questions':merged,'source_hashes':hashes}

def load_subquestions(repo, corpus_rows, source_sha256, reviews=None):
    """Read only safe, source-bound independently usable exam subquestions.

    The full prompts/assets live in the external details corpus. The parent
    remains the unit of historical exam frequency; these are retrieval IDs.
    """
    path=Path(repo)/ROOT_REL/SUBQUESTIONS_FILENAME
    if not path.is_file():return {'items':{},'source_hashes':{}}
    data=_read(path)
    if (data.get('schema')!='cs408-exam-subquestions-v1'
            or (data.get('source_sha256') or data.get('parent_corpus_sha256'))!=source_sha256):
        raise ValueError('exam_subquestion_source_binding')
    raw_items=data.get('items')
    if not isinstance(raw_items,list):raise ValueError('exam_subquestion_items')
    from knowledge_profile_408 import _taxonomy
    taxonomy=_taxonomy(repo)
    nodes={n['id'] for n in _read(Path(repo)/ROOT_REL/'catalog.json')['nodes']}
    parents={row['exam_question_id']:row for row in corpus_rows}
    items={}
    forbidden={'question_ocr','solution_ocr','answer','options','full_question',
               'question_text','condition_assets','solution_text','answer_text'}
    for raw in raw_items:
        row=copy.deepcopy(raw)
        sid=row.get('subquestion_id') or row.get('true_item_id')
        parent=row.get('parent_exam_id') or row.get('parent_true_item_id')
        if (not isinstance(sid,str) or not SUBQUESTION_RE.fullmatch(sid)
                or sid in items or parent not in parents or sid.rsplit('-P',1)[0]!=parent
                or row.get('true_item_id',sid)!=sid
                or row.get('parent_true_item_id',parent)!=parent):
            raise ValueError('exam_subquestion_identity:'+str(sid))
        if row.get('review_status')!='reviewed':raise ValueError('exam_subquestion_unreviewed:'+sid)
        points=row.get('assessed_points')
        if (not isinstance(points,list) or not points or len(points)!=len(set(points))
                or not set(points)<=set(taxonomy) or row.get('primary_point') not in points):
            raise ValueError('exam_subquestion_points:'+sid)
        if reviews and not set(points)<=set(reviews['questions'][parent]['assessed_points']):
            raise ValueError('exam_subquestion_parent_points:'+sid)
        anchors=row.get('syllabus_ids')
        if not isinstance(anchors,list) or not anchors or not set(anchors)<=nodes:
            raise ValueError('exam_subquestion_syllabus:'+sid)
        if forbidden & set(row):raise ValueError('exam_subquestion_contains_raw_question:'+sid)
        row.update(subquestion_id=sid,true_item_id=sid,parent_exam_id=parent,
                   parent_true_item_id=parent,count_basis='unique_parent_exam_questions')
        items[sid]=row
    return {'items':items,'source_hashes':{SUBQUESTIONS_FILENAME:_sha(path)}}

def load_primary_anchor_reviews(repo, reviews):
    """Source-bound derived choices; original reviews and bindings stay intact."""
    path=Path(repo)/ROOT_REL/PRIMARY_ANCHOR_REVIEW_FILENAME
    if not path.is_file():return {},{}
    data=_read(path)
    if (data.get('schema')!='cs408-frequency-primary-anchor-review-v1'
            or data.get('syllabus_source_sha256')!=_sha(Path(repo)/ROOT_REL/'source.txt')):
        raise ValueError('frequency_primary_anchor_review_source')
    result={}
    for entry in data.get('entries',[]):
        qid=entry['unit_id'];original=(reviews or {}).get('questions',{}).get(qid)
        digest=hashlib.sha256(json.dumps(original,ensure_ascii=False,sort_keys=True,
                                        separators=(',',':')).encode()).hexdigest()
        if not original or digest!=entry.get('annotation_sha256') or qid in result:
            raise ValueError('frequency_primary_anchor_review_changed:'+qid)
        result[qid]=entry
    return result,{path.name:_sha(path)}

def bind_frequency_primary_units(items,subquestions,anchor_reviews=None):
    """Derive counting units without changing the parent's original primary tag."""
    result=copy.deepcopy(items);children={};anchor_reviews=anchor_reviews or {}
    for sid,row in subquestions.items():
        children.setdefault(row['parent_exam_id'],{})[sid]=row
    comprehensive={qid for qid,item in items.items() if item['question_type']!='单项选择题'}
    if set(children)!=comprehensive:
        raise ValueError('frequency_primary_subquestion_coverage:'+','.join(sorted(set(children)^comprehensive)))
    used_reviews=set()
    for qid,item in result.items():
        units=[]
        if item['question_type']=='单项选择题':
            point=item['main_knowledge'][0]
            anchors=sorted({a for p in item.get('assessment_parts',[]) if point in p['canonical_points']
                            for a in p['syllabus_ids']})
            units.append(dict(unit_id=qid,unit_kind='choice',primary_point=point,
                              assessed_points=item['hit_knowledge'],syllabus_ids=anchors))
        else:
            parts=item.get('assessment_parts',[])
            labels=[p['part_id'].split(':',1)[0] for p in parts]
            canonical_labels=all(re.fullmatch(r'P\d{2}',label) for label in labels)
            if (not labels or len(labels)!=len(children[qid])
                    or (canonical_labels and {qid+'-'+label for label in labels}!=set(children[qid]))):
                raise ValueError('frequency_primary_subquestion_part_coverage:'+qid)
            for sid,row in sorted(children[qid].items()):
                units.append(dict(unit_id=sid,unit_kind='independent_subquestion',
                    primary_point=row['primary_point'],assessed_points=row['assessed_points'],
                    syllabus_ids=row['syllabus_ids']))
        for unit in units:
            if unit['primary_point'] not in item['hit_knowledge']:
                raise ValueError('frequency_primary_unit_not_assessed:'+unit['unit_id'])
            review=anchor_reviews.get(unit['unit_id'])
            if review:
                if (review['primary_point']!=unit['primary_point']
                        or set(review['expected_syllabus_ids'])!=set(unit['syllabus_ids'])
                        or not review['primary_syllabus_ids']
                        or not set(review['primary_syllabus_ids'])<=set(unit['syllabus_ids'])):
                    raise ValueError('frequency_primary_anchor_review_binding:'+unit['unit_id'])
                unit['primary_syllabus_ids']=sorted(set(review['primary_syllabus_ids']))
                unit['primary_syllabus_basis']='source_reviewed_exact_unit'
                used_reviews.add(unit['unit_id'])
        item['frequency_primary_units']=units
        item['frequency_primary_points']=sorted({u['primary_point'] for u in units})
    if used_reviews!=set(anchor_reviews):raise ValueError('frequency_primary_anchor_review_unused')
    return result

def primary_syllabus_ids(unit,contexts):
    """Keep reviewed direct anchors; ancestry matching never creates siblings."""
    anchors=set(unit['syllabus_ids'])
    if 'primary_syllabus_ids' in unit:
        selected=set(unit['primary_syllabus_ids'])
        if not selected or not selected<=anchors:
            raise ValueError('frequency_primary_syllabus_override:'+unit['unit_id'])
        return sorted(selected)
    if len(anchors)==1:return sorted(anchors)
    bound={entry['id'] for entry in contexts.get(unit['primary_point'],{}).get('syllabus_entries',[])}
    selected={a for a in anchors if any(a==b or a.startswith(b+'-') or b.startswith(a+'-') for b in bound)}
    if not selected:raise ValueError('frequency_primary_syllabus_unresolved:'+unit['unit_id'])
    return sorted(selected)

def syllabus_exam_sets(stats,contexts=None):
    """Direct reviewed part anchors, plus ancestors; never expand to siblings."""
    main={};hit={}
    for qid,item in stats['items'].items():
        for part in item.get('assessment_parts',[]):
            for sid in part['syllabus_ids']:
                path=sid.split('-')
                for end in range(2,len(path)+1):
                    anchor='-'.join(path[:end]);hit.setdefault(anchor,set()).add(qid)
                    if stats.get('primary_count_policy')!=PRIMARY_COUNT_POLICY and item['main_knowledge'][0] in part['canonical_points']:
                        main.setdefault(anchor,set()).add(qid)
        if stats.get('primary_count_policy')==PRIMARY_COUNT_POLICY:
            for unit in item['frequency_primary_units']:
                for sid in primary_syllabus_ids(unit,contexts or {}):
                    chunks=sid.split('-')
                    for end in range(2,len(chunks)+1):
                        anchor='-'.join(chunks[:end])
                        if qid not in hit.get(anchor,set()):
                            raise ValueError('frequency_primary_syllabus_not_assessed:'+unit['unit_id'])
                        main.setdefault(anchor,set()).add(qid)
    return main,hit

def formal_point_binding(row, questions, contexts):
    """Resolve a live official node without guessing an exam identity from its year."""
    declared=re.findall(r'EXAM408-\d{4}-Q\d{2}',row['来源ID'])
    exact=declared[0] if len(set(declared))==1 and declared[0] in questions else None
    if exact:
        source=questions[exact];assessed=source['assessed_points'];anchors=source['syllabus_ids']
        basis='formal_declared_canonical_identity_and_reviewed_exam_points'
    else:
        assessed=sorted(set(POINT.findall(row['主知识点']+'；'+row['命中知识点'])))
        direct={e['id'] for c in assessed for e in contexts[c]['syllabus_entries']}
        anchors=sorted({'-'.join(sid.split('-')[:i]) for sid in direct for i in range(2,len(sid.split('-'))+1)})
        basis='existing_formal_card_point_tags_without_inferred_exam_identity'
    return {'assessed_points':assessed,'syllabus_ids':anchors,'canonical_exam_id':exact,'binding_basis':basis}

def build_links(repo,stats,contexts):
    """Persist exact direct/reverse sets for both exams and existing formal cards."""
    from intake_lib_408 import Repo
    repo=Path(repo);master=Repo(repo).master()
    points={key:{'schema':'cs408-point-question-links-v1','point_id':key,'title':c['title'],
        'exam_assessed_ids':[],'exam_primary_ids':[],'exam_prerequisite_ids':[],
        'exam_context_only_ids':[],'exam_subquestion_ids':[],
        'formal_node_ids':[],'formal_bindings':{}}
        for key,c in contexts.items() if not key.startswith('KSP-')}
    questions={};formal={}
    syllabus_main,syllabus_hits=syllabus_exam_sets(stats,contexts)
    for qid,item in stats['items'].items():
        assessed=item['hit_knowledge'];main=item['main_knowledge'];parts=item.get('assessment_parts',[])
        anchors={sid for sid,ids in syllabus_hits.items() if qid in ids}
        primary_anchors={sid for sid,ids in syllabus_main.items() if qid in ids}
        count_main=(item['frequency_primary_points'] if stats.get('primary_count_policy')==PRIMARY_COUNT_POLICY else main)
        questions[qid]={'primary_point':main[0],'assessed_points':assessed,
            'prerequisite_points':item.get('prerequisite_points',[]),
            'context_only_points':item.get('context_only_points',[]),'syllabus_ids':sorted(anchors),
            'question_type':item['question_type'],'parts':parts}
        if stats.get('primary_count_policy')==PRIMARY_COUNT_POLICY:
            questions[qid]['frequency_primary_points']=list(count_main)
            questions[qid]['frequency_primary_units']=copy.deepcopy(item['frequency_primary_units'])
        for code in assessed:points[code]['exam_assessed_ids'].append(qid)
        for code in count_main:points[code]['exam_primary_ids'].append(qid)
        for code in item.get('prerequisite_points',[]):points[code]['exam_prerequisite_ids'].append(qid)
        for code in item.get('context_only_points',[]):points[code]['exam_context_only_ids'].append(qid)
        for sid in anchors:points[sid]['exam_assessed_ids'].append(qid)
        for sid in primary_anchors:points[sid]['exam_primary_ids'].append(qid)
    subquestions=copy.deepcopy(stats.get('subquestions',{}))
    for sid,row in subquestions.items():
        parent=row['parent_exam_id']
        if parent not in questions:raise ValueError('subquestion_parent_missing:'+sid)
        anchors={'-'.join(anchor.split('-')[:i])
                 for anchor in row['syllabus_ids'] for i in range(2,len(anchor.split('-'))+1)}
        for point in set(row['assessed_points'])|anchors:
            if point not in points:raise ValueError('subquestion_point_missing:'+sid+':'+point)
            points[point]['exam_subquestion_ids'].append(sid)
        questions[parent].setdefault('subquestion_ids',[]).append(sid)
    for fid,row in master.items():
        formal[fid] = formal_point_binding(row, questions, contexts)
        assessed = formal[fid]['assessed_points']
        anchors = formal[fid]['syllabus_ids']
        exact = formal[fid]['canonical_exam_id']
        basis = formal[fid]['binding_basis']
        for key in set(assessed)|set(anchors):
            points[key]['formal_node_ids'].append(fid)
            points[key]['formal_bindings'][fid]={'canonical_exam_id':exact,'binding_basis':basis}
    source={'exam_frequency_sha256':_sha(repo/ROOT_REL/'exam-frequency.json'),
            'formal_master_sha256':_sha(repo/'节点总表.md')}
    for key,point in points.items():
        for field in ('exam_assessed_ids','exam_primary_ids','exam_prerequisite_ids','exam_context_only_ids','exam_subquestion_ids','formal_node_ids'):
            point[field]=sorted(set(point[field]))
        point['sources']=source
        if stats.get('primary_count_policy'):point['primary_count_policy']=stats['primary_count_policy']
        point['exam_count']=len(point['exam_assessed_ids']);point['formal_count']=len(point['formal_node_ids'])
        point['subquestion_count']=len(point['exam_subquestion_ids'])
        _write(repo/ROOT_REL/'point-links'/f'{key}.json',point)
        contexts[key]['question_links']={
            'exam_question_ids':point['exam_assessed_ids'],'exam_subquestion_ids':point['exam_subquestion_ids'],
            'formal_node_ids':point['formal_node_ids'],
            'reference_path':str(ROOT_REL/'point-links'/f'{key}.json'),
            'use':'只表示考点与题目对应，不等于个人错误或题间强关联；复做推荐须实时14天筛选。'}
    for qid,row in questions.items():
        if any(qid not in points[c]['exam_assessed_ids'] for c in row['assessed_points']):raise ValueError('exam_reverse_missing:'+qid)
        if any(qid not in points[c]['exam_primary_ids'] for c in row.get('frequency_primary_points',[row['primary_point']])):raise ValueError('exam_primary_reverse_missing:'+qid)
    for key,row in points.items():
        if POINT.fullmatch(key) and any(key not in questions[q]['assessed_points'] for q in row['exam_assessed_ids']):raise ValueError('exam_forward_missing:'+key)
        if POINT.fullmatch(key) and any(key not in questions[q].get('frequency_primary_points',[questions[q]['primary_point']]) for q in row['exam_primary_ids']):raise ValueError('exam_primary_forward_missing:'+key)
        if any(key not in set(formal[f]['assessed_points'])|set(formal[f]['syllabus_ids']) for f in row['formal_node_ids']):raise ValueError('formal_reverse_missing:'+key)
    coverage={'exam_questions':len(questions),'formal_cards':len(formal),'points':len(points),
        'choice_questions':sum(r['question_type']=='单项选择题' for r in questions.values()),
        'comprehensive_questions':sum(r['question_type']!='单项选择题' for r in questions.values()),
        'assessed_point_question_links':sum(len(r['assessed_points']) for r in questions.values()),
        'assessment_units':sum(len(r['parts']) for r in questions.values()),
        'independent_subquestions':len(subquestions),
        'formal_point_links':sum(len(r['assessed_points']) for r in formal.values())}
    data={'schema':'cs408-exam-point-bipartite-index-v1','sources':source,'coverage':coverage,
          'questions':questions,'subquestions':subquestions,'formal_cards':formal,
          'point_entries':{key:{'path':str(ROOT_REL/'point-links'/f'{key}.json'),
                               'sha256':_sha(repo/ROOT_REL/'point-links'/f'{key}.json')} for key in points}}
    if stats.get('primary_count_policy'):data['primary_count_policy']=stats['primary_count_policy']
    _write(repo/ROOT_REL/'point-question-links.json',data)
    return data

def lookup_point(repo,point):
    if not (POINT.fullmatch(point) or re.fullmatch(r'S27-(?:DS|CO|OS|CN)(?:-\d{2})*',point)):
        raise ValueError('exact_point_identity_required')
    repo=Path(repo);path=repo/ROOT_REL/'point-links'/f'{point}.json'
    if not path.is_file():return None
    data=_read(path)
    if data['sources']['formal_master_sha256']!=_sha(repo/'节点总表.md'):
        raise ValueError('point_question_links_need_preprocessing_after_formal_change')
    if data['sources']['exam_frequency_sha256']!=_sha(repo/ROOT_REL/'exam-frequency.json'):
        raise ValueError('point_question_links_need_preprocessing_after_exam_change')
    return data

def main():
    p=argparse.ArgumentParser(description='按精确考点读取预处理双向索引，不生成学习记录。')
    p.add_argument('--repo',type=Path,default=Path(__file__).resolve().parents[1])
    p.add_argument('--point',required=True)
    args=p.parse_args();data=lookup_point(args.repo,args.point)
    print(json.dumps({'status':'available' if data else 'not_preprocessed','reference_only':True,
        'recommendation_requires':'related_candidates_408.py --kp <考点> 实时14天筛选',
        'result':data,'write_count':0},ensure_ascii=False))

if __name__=='__main__':main()
