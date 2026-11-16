#!/usr/bin/env python3
"""Evidence-bound 2027 syllabus overlays; no learner-history or scheduling writes."""
from __future__ import annotations

import argparse
import copy
import csv
import hashlib
import json
import re
from collections import Counter
from pathlib import Path

import runtime_paths_408 as _paths

REL = Path('support/syllabus-2027')
SCHEMA = 'cs408-syllabus-context-v1'
STATUSES = {'explicit', 'within_scope', 'prerequisite_only', 'out_of_scope', 'needs_review'}
STATUS_NAMES = {'explicit':'大纲原文明列','within_scope':'条目内必要机制',
                'prerequisite_only':'仅补当前题必要前提','out_of_scope':'不作自动拓展','needs_review':'范围待核验'}
FREQUENCY_RULE = '本地统计：主考取选择题或已审核独立小问的主考点，同一考点仍按父题去重；父题原主标签只保留作来源与导航，不额外计为综合题主考。主考至少5题且跨至少4年为高频；主考1—2题为低频，其余有主考记录为中频。仅附带涉及或未标注命中不判低频。总涉及=主考+仅附带涉及，前置与背景不计；不是官方权重或命题预测。'
INDEX = _paths.details_root() / 'corpus/408统考真题/indexes/question-index.csv'

def coverage_text(coverage):
    years=coverage['accepted_years']
    return f"{years[0]}—{years[-1]}年已接受{coverage['question_count']}题（含综合题）"

def read(path):
    return json.loads(Path(path).read_text(encoding='utf-8'))

def write(path, data):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')

def sha(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()

def codes(text):
    return set(re.findall(r'(?:DS|CO|OS|CN)\d{2}-\d{2}', text or ''))

def frequency(main_ids, hit_ids, coverage=None):
    if coverage is None:
        from build_true_exam_corpus import accepted_coverage
        coverage=accepted_coverage()
    # A child is independently selectable, but never an extra historical exam.
    parent=lambda qid:re.sub(r'(?<=Q\d{2})-P\d{2}$','',qid)
    main_ids, hit_ids = sorted({parent(qid) for qid in main_ids}), sorted({parent(qid) for qid in hit_ids})
    if not set(main_ids) <= set(hit_ids):
        raise ValueError('primary_exam_items_must_be_in_total_hits')
    main_years = sorted({int(x.split('-')[1]) for x in main_ids})
    years = sorted({int(x.split('-')[1]) for x in hit_ids})
    label = ('高频' if len(main_ids) >= 5 and len(main_years) >= 4 else
             '重复考查' if len(main_ids) >= 2 else '主考1题' if main_ids else
             '仅有涉及记录' if hit_ids else '当前语料未标注命中')
    level=('高频' if label=='高频' else '低频' if 1<=len(main_ids)<=2 else
           '中频' if main_ids else '仅附带涉及' if hit_ids else '频次未知')
    return dict(main_count=len(main_ids), hit_count=len(hit_ids), total_count=len(hit_ids),
                secondary_only_count=len(set(hit_ids)-set(main_ids)),main_frequency_level=level,
                count_basis='unique_exam_questions',main_years=main_years,
                years=years, year_item_counts=dict(sorted(Counter(x.split('-')[1] for x in hit_ids).items())),
                main_question_ids=main_ids, hit_question_ids=hit_ids,
                frequency_label=label, frequency_scope='canonical_point',
                coverage=coverage_text(coverage),coverage_years=coverage['accepted_years'],
                coverage_note=coverage.get('coverage_note',''),
                source_authority=coverage.get('source_authority','existing_reviewed_corpus'),
                interpretation='主考与涉及分开；同题去重；0表示当前标签未命中，不证明从未考过。')

def make_frequency_intro(context):
    """Author the complete count-first line at preprocessing time, never per turn."""
    f=context['exam_frequency']
    title=context.get('frequency_point_title') or context['title']
    prefix=('对应父考点“'+title+'”' if f['frequency_scope']=='parent_point_not_subpoint' else title)
    years=f.get('coverage_years',[2009,2025])
    period=f'{years[0]}—{years[-1]}年'
    if f.get('frequency_status')=='no_exact_tag_mapping':
        return prefix+'尚无独立逐题标注，总考次及频次等级暂不能可靠确定。'
    total=f['total_count'];main=f['main_count'];secondary=f['secondary_only_count']
    if not total:
        if f.get('count_coverage')=='full_question_review':
            return prefix+'在'+period+'已接受真题资料中未见考查（0次，仅限该统计范围）。'
        return prefix+'在'+period+'已审核标签中未标注命中，不能据此判为低频或从未考过。'
    suffix=('按主考记录属'+f['main_frequency_level']+'考点。' if main else '尚无主考标注，不据此判为低频。')
    return f'{prefix}在{period}共考查{total}次（主考{main}次、兼考{secondary}次），'+suffix

def _frequency_point_row(code, context):
    f=context['exam_frequency']
    return dict(knowledge_point=code,title=context['title'],
                total_count=f['total_count'],main_count=f['main_count'],
                secondary_only_count=f['secondary_only_count'],
                main_frequency_level=f['main_frequency_level'],intro=context['frequency_intro'])

def _formal_exam_points(item,stats):
    declared=set(re.findall(r'EXAM408-\d{4}-Q\d{2}',item['来源ID']))
    canonical=next(iter(declared)) if len(declared)==1 and declared<=set(stats['items']) else None
    if canonical:
        row=stats['items'][canonical]
        return row['main_knowledge'],row['hit_knowledge'],canonical
    main=sorted(codes(item['主知识点']))
    return main,sorted(codes(item['命中知识点'])|set(main)),None

def _preprocess_frequencies(repo,data,stats):
    """Reuse reviewed scope bindings; materialize every point and every question."""
    from intake_lib_408 import Repo
    repo=Path(repo);contexts=data['contexts']
    reviewed_parts=stats.get('reviewed_question_parts',bool(stats.get('annotation_source_hashes')))
    for key,c in contexts.items():
        if key.startswith('S27-'):continue
        code=c['knowledge_point'];f=copy.deepcopy(stats['points'][code])
        c['frequency_point_title']=contexts[code]['title']
        if key.startswith('KSP-'):
            f.update(frequency_scope='parent_point_not_subpoint',parent_point=code,subpoint_count=None,
                     frequency_note='父点统计，不是此小点的独立考次。')
        c['exam_frequency']=f;c['frequency_intro']=make_frequency_intro(c)
        c['exam_source_sha256']=stats['source_sha256']
    for node in data['outline']:
        linked=node['subtree_canonical_points']
        if reviewed_parts:
            from exam_point_links_408 import syllabus_exam_sets
            if 'reviewed_syllabus_sets' not in locals():reviewed_syllabus_sets=syllabus_exam_sets(stats,contexts)
            primary,assessed=reviewed_syllabus_sets
            f=frequency(primary.get(node['id'],set()),assessed.get(node['id'],set()),stats.get('accepted_coverage'))
            f['frequency_scope']='reviewed_syllabus_subtree'
            f['count_coverage']='full_question_review'
            if stats.get('primary_count_policy'):f['primary_count_policy']=stats['primary_count_policy']
            if not f['total_count']:f['main_frequency_level']='未见考查'
        else:
            f=frequency({q for code in linked for q in stats['points'][code]['main_question_ids']},
                        {q for code in linked for q in stats['points'][code]['hit_question_ids']},stats.get('accepted_coverage'))
            f['frequency_scope']='mapped_syllabus_subtree'
        node['exam_frequency']=copy.deepcopy(f)
        c=contexts[node['id']]
        if not linked and not reviewed_parts:
            f.update(frequency_status='no_exact_tag_mapping',frequency_label='此考纲条目未有独立逐题标注')
        c['exam_frequency']=f;c['frequency_intro']=make_frequency_intro(c)
        c['exam_source_sha256']=stats['source_sha256']
    formal=Repo(repo).master();rows={}
    for key,item in stats['items'].items():
        primary=item['main_knowledge'][0]
        ordered=[primary]+[code for code in item['hit_knowledge'] if code!=primary]
        rows[key]=dict(schema='cs408-question-frequency-introduction-v1',question_identity=key,
            identity_kind='verified_canonical_exam',primary_point=primary,
            primary_intro=contexts[primary]['frequency_intro'],
            points=[_frequency_point_row(code,contexts[code]) for code in ordered],
            source_sha256=stats['source_sha256'])
    for sid,item in stats.get('subquestions',{}).items():
        primary=item['primary_point']
        ordered=[primary]+[code for code in item['assessed_points'] if code!=primary]
        rows[sid]=dict(schema='cs408-question-frequency-introduction-v1',question_identity=sid,
            identity_kind='accepted_canonical_exam_subquestion',parent_exam_id=item['parent_exam_id'],
            canonical_exam_id=item['parent_exam_id'],primary_point=primary,
            primary_intro=contexts[primary]['frequency_intro'],
            points=[_frequency_point_row(code,contexts[code]) for code in ordered],
            count_basis='unique_parent_exam_questions',source_sha256=stats['source_sha256'])
    for key,item in formal.items():
        main,hits,canonical=_formal_exam_points(item,stats)
        if len(main)!=1 or any(code not in contexts for code in hits):
            raise ValueError('formal_frequency_point_binding_invalid:'+key)
        primary=main[0];ordered=[primary]+[code for code in hits if code!=primary]
        rows[key]=dict(schema='cs408-question-frequency-introduction-v1',question_identity=key,
            identity_kind='formal_card',primary_point=primary,
            primary_intro=contexts[primary]['frequency_intro'],
            points=[_frequency_point_row(code,contexts[code]) for code in ordered],
            source_sha256=stats['source_sha256'],formal_card_sha256=sha(repo/(key+'.md')),
            canonical_exam_id=canonical,
            use='仅用于当前正式原题；替换题必须使用当前true_item_id，不用被替换正式ID。')
    bundle=dict(schema='cs408-question-frequency-bundle-v1',rows=rows,
        sources={'exam_frequency_sha256':sha(repo/REL/'exam-frequency.json'),
                 'formal_master_sha256':sha(repo/'节点总表.md')},
        count_basis='unique_exam_questions',frequency_rule=FREQUENCY_RULE,
        coverage=dict(exam_questions=len(stats['items']),formal_cards=len(formal),
                      canonical_points=len(stats['points']),preprocessed_rows=len(rows),
                      independent_subquestions=len(stats.get('subquestions',{}))))
    if stats.get('primary_count_policy'):bundle['primary_count_policy']=stats['primary_count_policy']
    write(repo/REL/'question-frequency.json',bundle)
    if reviewed_parts:
        from exam_point_links_408 import build_links
        links=build_links(repo,stats,contexts)
        data['point_question_link_coverage']=links['coverage']
        write(repo/REL/'question-syllabus-map.json',{
            'schema':'cs408-question-syllabus-map-v1','exam_source_sha256':stats['source_sha256'],
            'annotation_source_hashes':stats['annotation_source_hashes'],
            'exam_items':{qid:{**stats['items'][qid],
                'hit_syllabus_ids':row['syllabus_ids']} for qid,row in links['questions'].items()},
            'subquestions':links.get('subquestions',{}),'formal_cards':links['formal_cards']})
    for filename in data['sources']:
        data['sources'][filename]=sha(repo/REL/filename)
    data['sources']['question-frequency.json']=sha(repo/REL/'question-frequency.json')
    data['sources'].update(stats.get('annotation_source_hashes',{}))
    if reviewed_parts:
        data['sources']['point-question-links.json']=sha(repo/REL/'point-question-links.json')
    data['frequency_rule']=FREQUENCY_RULE
    if stats.get('primary_count_policy'):data['primary_count_policy']=stats['primary_count_policy']
    data['accepted_coverage']=stats['accepted_coverage']
    data['coverage']['exam_items']=len(stats['items'])
    data['coverage']['independent_subquestions']=len(stats.get('subquestions',{}))
    data['frequency_preprocessing']=bundle['coverage']
    write(repo/REL/'context.json',data)
    write_index(repo,data)
    lines=['# 408考点频次预处理','',FREQUENCY_RULE,'',
           '统计范围：'+coverage_text(stats['accepted_coverage'])+'。主考与附带涉及互斥，合计不能重复相加。',
           stats['accepted_coverage'].get('coverage_note',''),
           '下面的次数属于考点，不是说某一道原题重复考了这些次。小点未独立标注时只报告对应父考点。','',
           '| 考点 | 总考次 | 主考次数 | 兼考次数 | 按主考记录分级 |','|---|---:|---:|---:|---|']
    for code in stats['points']:
        c=contexts[code];f=c['exam_frequency']
        lines.append(f"| [[知识点/学习路径/{code}\\|{code} {c['title']}]] | {f['total_count']} | {f['main_count']} | {f['secondary_only_count']} | {f['main_frequency_level']} |")
    (repo/REL/'考点频次总览.md').write_text('\n'.join(lines)+'\n',encoding='utf-8')
    return bundle

def preprocess_frequencies(repo,index_path=INDEX):
    repo=Path(repo)
    data=read(repo/REL/'context.json')
    # Scope bindings stay frozen. Exam annotations/acceptance are independently
    # validated against the new exact corpus by build_frequency below.
    for name,digest in data['sources'].items():
        if name in {'source.txt','catalog.json'} or name.startswith('bindings-'):
            if sha(repo/REL/name)!=digest:
                raise ValueError('syllabus_source_changed_rebuild_required:'+name)
    stats=build_frequency(repo,index_path)
    return _preprocess_frequencies(repo,data,stats)

def ensure_formal_frequency_links(repo):
    """Cold postcommit upkeep when formal identities change; no learner writes."""
    repo=Path(repo);path=repo/REL/'question-frequency.json'
    if not path.is_file():return {'status':'not_configured'}
    previous=read(path)
    if previous['sources']['formal_master_sha256']==sha(repo/'节点总表.md'):
        return {'status':'already_current'}
    result=preprocess_frequencies(repo)
    return {'status':'refreshed','coverage':result['coverage'],'formal_write_count':0}

def load_question_introductions(repo_root):
    if repo_root is None:return {}
    repo=Path(repo_root);path=repo/REL/'question-frequency.json'
    if not path.is_file():return {}
    data=read(path)
    if data.get('schema')!='cs408-question-frequency-bundle-v1':raise ValueError('question_frequency_schema')
    if data['sources']['exam_frequency_sha256']!=sha(repo/REL/'exam-frequency.json'):
        raise ValueError('question_frequency_stats_changed_reprocess_required')
    rows=data['rows']
    if data['sources']['formal_master_sha256']!=sha(repo/'节点总表.md'):
        # Formal intake already owns the current master. Cold materialization
        # can adapt its exact identities using verified point counts; it must
        # not block future intake or rewrite this archival preprocessing file.
        from intake_lib_408 import Repo
        contexts=load_contexts(repo)
        stats=read(repo/REL/'exam-frequency.json')
        rows={key:value for key,value in rows.items() if key.startswith('EXAM408-')}
        for key,item in Repo(repo).master().items():
            main,hits,canonical=_formal_exam_points(item,stats)
            if len(main)!=1 or any(code not in contexts for code in hits):
                continue  # The query reports an explicit missing preprocessing entry.
            primary=main[0];ordered=[primary]+[code for code in hits if code!=primary]
            rows[key]=dict(schema='cs408-question-frequency-introduction-v1',question_identity=key,
                identity_kind='formal_card',primary_point=primary,
                primary_intro=contexts[primary]['frequency_intro'],
                points=[_frequency_point_row(code,contexts[code]) for code in ordered],
                source_sha256=contexts[primary]['exam_source_sha256'],formal_card_sha256=sha(repo/(key+'.md')),
                canonical_exam_id=canonical,
                use='当前正式原题；替换题使用当前true_item_id。',
                cold_identity_rebound=True)
    return rows

def build_frequency(repo, index_path=INDEX):
    from knowledge_profile_408 import _taxonomy
    from build_true_exam_corpus import accepted_coverage, ACCEPTANCE_REL
    coverage=accepted_coverage(Path(repo))
    taxonomy = _taxonomy(repo)
    with Path(index_path).open(encoding='utf-8-sig', newline='') as handle:
        rows = list(csv.DictReader(handle))
    from exam_point_links_408 import load_reviews
    review=load_reviews(repo,rows,sha(index_path))
    seen, items = set(), {}
    main, hit = {c:set() for c in taxonomy}, {c:set() for c in taxonomy}
    for row in rows:
        qid = row['exam_question_id']
        if qid in seen or not re.fullmatch(r'EXAM408-\d{4}-Q\d{2}', qid):
            raise ValueError('invalid_or_duplicate_exam_identity:' + qid)
        seen.add(qid)
        year, qno = int(row['year']), int(row['official_question_no'])
        if qid != f'EXAM408-{year}-Q{qno:02}' or row['knowledge_review_status'] != 'reviewed':
            raise ValueError('unreviewed_exam_identity:' + qid)
        annotated=review['questions'][qid] if review else None
        mains,hits=({annotated['primary_point']},set(annotated['assessed_points'])) if annotated else (codes(row['main_knowledge']),codes(row['hit_knowledge']))
        if len(mains) != 1 or not mains <= hits or not hits <= set(taxonomy):
            raise ValueError('invalid_exam_tags:' + qid)
        for c in hits: hit[c].add(qid)
        items[qid] = dict(year=year, question_no=qno, subject=row['subject'],question_type=row['question_type'],
                          main_knowledge=sorted(mains), hit_knowledge=sorted(hits))
        if annotated:
            items[qid].update(assessment_parts=annotated['parts'],
                prerequisite_points=annotated['prerequisite_points'],context_only_points=annotated['context_only_points'],
                review_note=annotated['review_note'])
    expected = coverage['expected_ids']
    if seen != expected:
        raise ValueError('exam_coverage_changed_requires_explicit_review')
    from exam_point_links_408 import (load_subquestions,load_primary_anchor_reviews,
                                     bind_frequency_primary_units,PRIMARY_COUNT_POLICY)
    subquestions=load_subquestions(repo,rows,sha(index_path),review)
    anchor_reviews,anchor_hashes=load_primary_anchor_reviews(repo,review)
    items=bind_frequency_primary_units(items,subquestions['items'],anchor_reviews)
    for qid,item in items.items():
        for c in item['frequency_primary_points']:main[c].add(qid)
    safe_coverage={key:value for key,value in coverage.items() if key!='expected_ids'}
    result = dict(schema='cs408-exam-frequency-v1', source_path=str(index_path), source_sha256=sha(index_path),
                  accepted_coverage=safe_coverage,coverage_years=coverage['accepted_years'],
                  reviewed_question_parts=bool(review),
                  question_count=len(items), frequency_rule=FREQUENCY_RULE,
                  primary_count_policy=PRIMARY_COUNT_POLICY,
                  annotation_basis=('选择题取本题主点，综合题取已审核独立小问主点；按考点分别以父题去重，纯前置和背景不计；父题原始主标签保留。' if review else
                    '现有接受范围内逐题审核知识标签；未重新逐题重标。'),
                  points={c:frequency(main[c],hit[c],coverage) for c in taxonomy}, items=items,
                  subquestions=subquestions['items'])
    if review:result['annotation_source_hashes']=review['source_hashes']
    result.setdefault('annotation_source_hashes',{}).update(subquestions['source_hashes'])
    result['annotation_source_hashes'].update(anchor_hashes)
    acceptance_path=Path(repo)/ACCEPTANCE_REL
    if acceptance_path.is_file():
        result['annotation_source_hashes'][ACCEPTANCE_REL.name]=sha(acceptance_path)
    if review:
        for f in result['points'].values():
            f['count_coverage']='full_question_review'
            if not f['total_count']:f['main_frequency_level']='未见考查'
    for f in result['points'].values():f['primary_count_policy']=PRIMARY_COUNT_POLICY
    write(Path(repo)/REL/'exam-frequency.json', result)
    return result

def _path(nodes, ident):
    result=[]
    while ident:
        node=nodes[ident]
        result.append({'id':ident,'title':node['title']})
        ident=node['parent_id']
    return list(reversed(result))

def build(repo, baseline_path, index_path=INDEX):
    repo=Path(repo)
    baseline=read(baseline_path)
    taxonomy=baseline['taxonomy']
    catalog=read(repo/REL/'catalog.json')
    if sha(repo/REL/'source.txt') != catalog['source']['text_sha256']:
        raise ValueError('syllabus_text_changed_reparse_required')
    for rel, expected in baseline['protected_files'].items():
        if sha(repo/rel) != expected:
            raise ValueError('review_baseline_changed:'+rel)
    for identity, old in baseline['cards'].items():
        if sha(repo/(identity+'.md')) != old['sha256']:
            raise ValueError('review_card_changed:'+identity)
    nodes={n['id']:n for n in catalog['nodes']}
    stats=build_frequency(repo,index_path)
    points,subpoints,reviewed,findings={},{},set(),[]
    profiles=baseline['profiles']['profiles']
    expected_sub={s['subpoint_id']:code for code,p in profiles.items() for s in p.get('subpoints',[])}
    for subject in ('DS','CO','OS','CN'):
        binding=read(repo/REL/f'bindings-{subject}.json')
        if binding.get('schema')!='cs408-syllabus-bindings-v1': raise ValueError('binding_schema:'+subject)
        expected={c for c in taxonomy if c.startswith(subject)}
        if set(binding['points']) != expected: raise ValueError('point_set_mismatch:'+subject)
        exp_sub={k for k,p in expected_sub.items() if p.startswith(subject)}
        if set(binding['subpoints']) != exp_sub: raise ValueError('subpoint_set_mismatch:'+subject)
        expected_cards={k for k in baseline['cards'] if k.startswith(subject)}
        if set(binding['reviewed_formal_ids']) != expected_cards: raise ValueError('card_review_set_mismatch:'+subject)
        points.update(binding['points']);subpoints.update(binding['subpoints'])
        reviewed.update(binding['reviewed_formal_ids']);findings.extend(binding.get('findings',[]))
    contexts={}
    reverse={key:[] for key in nodes}
    for key,binding in {**points,**subpoints}.items():
        code=binding.get('parent_code',key)
        if code not in taxonomy or (key in expected_sub and expected_sub[key]!=code):
            raise ValueError('parent_binding_invalid:'+key)
        if binding['scope_status'] not in STATUSES or not binding['teaching_boundary'].strip():
            raise ValueError('scope_invalid:'+key)
        ids=binding['syllabus_ids']
        if not isinstance(ids,list) or len(set(ids))!=len(ids) or any(i not in nodes for i in ids):
            raise ValueError('syllabus_ids_invalid:'+key)
        if not ids and binding['scope_status'] not in {'out_of_scope','needs_review'}:
            raise ValueError('scope_without_anchor:'+key)
        entries=[]
        for ident in ids:
            node=nodes[ident]; path=_path(nodes,ident)
            entries.append(dict(id=ident,title=node['title'],path=path,
                parent=path[-2] if len(path)>1 else None,
                children=[dict(id=i,title=nodes[i]['title']) for i in node['children']],
                details=node['details'],source_line=node['source_line']))
            if key in points: reverse[ident].append(key)
        freq=copy.deepcopy(stats['points'][code])
        if key in subpoints:
            freq['frequency_scope']='parent_point_not_subpoint'
            freq['parent_point']=code
            freq['subpoint_count']=None
            freq['frequency_note']='以下为父规范点统计；未作此细分机制的逐题标注，不能说该小点考过这些次数或年份。'
        children=[dict(id=s['subpoint_id'],title=s['title']) for s in profiles.get(code,{}).get('subpoints',[])] if key in points else []
        contexts[key]=dict(schema=SCHEMA,syllabus_year=2027,identity=key,knowledge_point=code,
            title=taxonomy[code] if key in points else next(s['title'] for s in profiles[code]['subpoints'] if s['subpoint_id']==key),
            scope_status=binding['scope_status'],syllabus_entries=entries,
            teaching_boundary=binding['teaching_boundary'],rationale=binding.get('rationale',''),
            syllabus_source_sha256=catalog['source']['text_sha256'],exam_source_sha256=stats['source_sha256'],
            personal_profile_version=profiles.get(code,{}).get('version'),
            personal_subpoints=children,exam_frequency=freq,
            history_policy='仅校准教学范围，保留原个人事实；考纲与频次不证明掌握度。')
    outline=[]
    for ident,node in nodes.items():
        descendant_ids={i for i in nodes if i==ident or i.startswith(ident+'-')}
        linked=sorted({c for i in descendant_ids for c in reverse[i]})
        # Unmapped leaves remain visible rather than being falsely covered by a broad parent tag.
        m={q for c in linked for q in stats['points'][c]['main_question_ids']}
        h={q for c in linked for q in stats['points'][c]['hit_question_ids']}
        f=frequency(m,h,stats['accepted_coverage']); f['frequency_scope']='mapped_syllabus_subtree'
        outline.append({**node,'path':_path(nodes,ident),'direct_canonical_points':reverse[ident],
            'subtree_canonical_points':linked,'coverage_status':'mapped' if linked else 'syllabus_only_entry',
            'exam_frequency':f})
    # Official-outline entries are separately addressable, never new personal
    # knowledge codes, learner records or scheduling targets.
    for node in outline:
        path=node['path'];ident=node['id']
        f=copy.deepcopy(node['exam_frequency'])
        f['frequency_note']='按已审核规范点标签映射到该考纲子树的真题并集；没有细分标注不等于未考。'
        if not node['subtree_canonical_points']:
            f['frequency_label']='此考纲条目未有独立逐题标注'
            f['frequency_status']='no_exact_tag_mapping'
        contexts[ident]=dict(schema=SCHEMA,syllabus_year=2027,identity=ident,
            knowledge_point=ident,title=node['title'],source_kind='syllabus_only',
            scope_status='explicit',syllabus_entries=[dict(id=ident,title=node['title'],path=path,
                parent=path[-2] if len(path)>1 else None,
                children=[dict(id=i,title=nodes[i]['title']) for i in node['children']],
                details=node['details'],source_line=node['source_line'])],
            teaching_boundary='按本条原文深度解释：'+node['title']+('；'+'；'.join(node['details']) if node['details'] else '')+'。仅补当前题必要机制，不扩展无关技术；没有个人证据不推断薄弱或掌握。',
            rationale='2027大纲原文明列的独立入口，保留缺少旧规范点的内容。',
            syllabus_source_sha256=catalog['source']['text_sha256'],exam_source_sha256=stats['source_sha256'],
            personal_profile_version=None,personal_subpoints=[],exam_frequency=f,
            history_policy='无个人记录；不是新建个人摘要或调度节点。')
    source_files=['source.txt','catalog.json','exam-frequency.json']+[f'bindings-{s}.json' for s in ('DS','CO','OS','CN')]
    data=dict(schema='cs408-syllabus-context-bundle-v1',contexts=contexts,outline=outline,
        sources={p:sha(repo/REL/p) for p in source_files},reviewed_formal_ids=sorted(reviewed),findings=findings,
        coverage=dict(canonical_points=len(points),personal_profiles=len(profiles),subpoints=len(subpoints),
                      formal_cards=len(reviewed),exam_items=stats['question_count'],syllabus_nodes=len(nodes),
                      outline_only_leaves=[n['id'] for n in outline if not n['children'] and not n['subtree_canonical_points']]),
        frequency_rule=FREQUENCY_RULE)
    exam_map={qid:{**row,
        'main_syllabus_ids':sorted({i for c in row['main_knowledge'] for i in points[c]['syllabus_ids']}),
        'hit_syllabus_ids':sorted({i for c in row['hit_knowledge'] for i in points[c]['syllabus_ids']})}
        for qid,row in stats['items'].items()}
    from intake_lib_408 import Repo
    formal=Repo(repo).master()
    if set(formal)!=reviewed:raise ValueError('formal_master_review_set_mismatch')
    formal_map={}
    for fid,row in formal.items():
        main=codes(row['主知识点']);hit=codes(row['命中知识点'])|main
        if not hit<=set(points):raise ValueError('formal_unknown_point:'+fid)
        exact=re.findall(r'EXAM408-\d{4}-Q\d{2}',row['来源ID'])
        formal_map[fid]=dict(main_knowledge=sorted(main),hit_knowledge=sorted(hit),
            syllabus_ids=sorted({i for c in hit for i in points[c]['syllabus_ids']}),
            source_id=row['来源ID'],verified_corpus_source_ids=sorted({q for q in exact if q in stats['items']}),
            exam_frequency_contribution=0,
            note='仅核对既有正式来源绑定，不用卡片年份推断；正式卡不作为真题统计分母。',
            scope_findings=[finding for finding in findings if finding.get('identity')==fid])
    write(repo/REL/'question-syllabus-map.json',dict(schema='cs408-question-syllabus-map-v1',
        exam_source_sha256=stats['source_sha256'],exam_items=exam_map,formal_cards=formal_map))
    _preprocess_frequencies(repo,data,stats)
    return data

def load_contexts(repo_root):
    if repo_root is None:return {}
    path=Path(repo_root)/REL/'context.json'
    if not path.is_file():return {}
    data=read(path)
    if data.get('schema')!='cs408-syllabus-context-bundle-v1':raise ValueError('syllabus_bundle_schema')
    for name,digest in data['sources'].items():
        if sha(Path(repo_root)/REL/name)!=digest:raise ValueError('syllabus_source_changed_rebuild_required:'+name)
    scopes = data['contexts']
    supplemental = Path(repo_root)/REL/'reviewed-subpoint-scopes.json'
    if supplemental.is_file():
        layer = read(supplemental)
        if layer.get('schema') != 'cs408-reviewed-subpoint-scopes-v1' or layer['syllabus_text_sha256'] != data['sources']['source.txt']:
            raise ValueError('reviewed_subpoint_scope_source_version')
        for key, row in layer['subpoints'].items():
            # The primary audited table supersedes this incremental completion when it gains this ID.
            if key in scopes:
                continue
            code = row['parent']
            ctx = context_for(scopes, code, key)
            if not ctx or not row['syllabus_ids'] or any(i not in scopes for i in row['syllabus_ids']):
                raise ValueError('reviewed_subpoint_scope_binding:' + key)
            ctx.update(title=row['title'], scope_status=row['scope_status'],
                       teaching_boundary=row['teaching_boundary'], rationale=row['rationale'],
                       syllabus_entries=[copy.deepcopy(scopes[i]['syllabus_entries'][0]) for i in row['syllabus_ids']])
            scopes[key] = ctx
    return scopes

def compact_context(context):
    if not context:return {}
    f=context['exam_frequency']
    entries=context['syllabus_entries']
    result=dict(syllabus_year=2027,scope_status=context['scope_status'],
        paths=[' / '.join(n['title'] for n in e['path']) for e in entries],
        syllabus_ids=[e['id'] for e in entries],
        teaching_boundary=context['teaching_boundary'],
        exam_frequency={k:f[k] for k in ('main_count','hit_count','frequency_label','frequency_scope',
            'total_count','secondary_only_count','main_frequency_level','primary_count_policy') if k in f},
        coverage=f['coverage'],coverage_note=f.get('coverage_note',''),frequency_intro=context.get('frequency_intro'))
    result['teaching_guard'] = {
        'out_of_scope':'仅保留历史证据，不用旧摘要或next_check自动展开此点。',
        'needs_review':'范围尚未单独核验；只用已核验父范围，不采用未核验拓展。',
        'prerequisite_only':'只补当前题必要前提，不开独立拓展训练。',
    }.get(context['scope_status'],'按本题需要讲解。')
    if context.get('context_object'):
        result['context_object']=context['context_object']
    if context.get('source_kind'):
        result['source_kind']=context['source_kind']
    if f.get('frequency_status')=='no_exact_tag_mapping':
        result['exam_frequency']['frequency_status']='no_exact_tag_mapping'
        result['exam_frequency']['note']='未有独立逐题标注，不能把0解读为从未考过。'
    if f['frequency_scope']=='parent_point_not_subpoint':
        result['exam_frequency']['parent_point']=f['parent_point']
        result['exam_frequency']['subpoint_count']=None
        result['exam_frequency']['note']='父点统计，不是此小点的独立考次。'
    # Direct children are official syllabus children, not invented textbook levels.
    children=list(dict.fromkeys(n['title'] for e in entries for n in e['children']))
    if children:result['children']=children
    else:result['children']=[]
    details=list(dict.fromkeys(s for e in entries for s in e.get('details',[])))
    if details:result['listed_details']=details
    return result

def context_for(contexts, code, subpoint_id=None):
    """New mechanisms inherit only a reviewed scope guard, never made-up review."""
    key=subpoint_id or code
    if key in contexts:return contexts[key]
    if not subpoint_id or code not in contexts:return {}
    result=copy.deepcopy(contexts[code])
    result['identity']=subpoint_id
    result['scope_status']='needs_review'
    result['teaching_boundary']='本小点尚未单独校准；只采用以下已核验父点范围，不自动展开小点历史建议。'+result['teaching_boundary']
    result['exam_frequency']['frequency_scope']='parent_point_not_subpoint'
    result['exam_frequency']['parent_point']=code
    result['exam_frequency']['subpoint_count']=None
    result['frequency_point_title']=contexts[code].get('title',code)
    if 'total_count' in result['exam_frequency']:
        result['frequency_intro']=make_frequency_intro(result)
    return result

def render_markdown(context):
    if not context:return ''
    f=context['exam_frequency'];lines=['## 2027 考纲定位与真题记录','']
    if context.get('frequency_intro'):lines+=['- 讲题开场：'+context['frequency_intro']]
    for e in context['syllabus_entries']:
        lines += ['- 考纲路径：'+' → '.join(n['title'] for n in e['path']),
            '- 上一级：'+(e['parent']['title'] if e['parent'] else '科目根'),
            '- 下一级：'+('；'.join(n['title'] for n in e['children']) or '大纲未另列；个人机制小点属于本地细分，不冒称大纲原文。')]
        if e.get('details'):lines+=['- 原文附列细项：'+'；'.join(e['details'])]
    lines += ['- 范围判定：'+STATUS_NAMES[context['scope_status']],'- 本点讲解边界：'+context['teaching_boundary'],
        '- 统计范围：'+f['coverage'],f"- 主考 {f['main_count']} 题；涉及 {f['hit_count']} 题；标记：{f['frequency_label']}。",
        '- 主考年份：'+('、'.join(map(str,f['main_years'])) or '当前标签无主考记录'),
        '- 涉及年份：'+('、'.join(map(str,f['years'])) or '当前标签无命中记录'),
        '- 精确真题身份：'+('；'.join(f['hit_question_ids']) or '未标注命中，不等于从未考过'),
        '- 频次口径：'+FREQUENCY_RULE,
        '- 依据：[[support/syllabus-2027/考纲索引|完整大纲树、全部知识点映射与统计口径]]。','']
    if context.get('question_links'):
        linked=context['question_links']
        lines += ['- 已预处理关联的正式题卡：'+('、'.join(f'[[{fid}]]' for fid in linked['formal_node_ids']) or '当前没有对应正式题卡'),
                  '- 上述为知识库定位入口，不是本轮复做推荐；真正出题前实时执行最近接触14天筛选。','']
    return '\n'.join(lines)

def write_index(repo,data):
    repo=Path(repo)
    coverage=data.get('accepted_coverage')
    if coverage is None:
        from build_true_exam_corpus import accepted_coverage
        coverage=accepted_coverage(repo)
    lines=['# 2027 408 考纲与历年真题索引','',
        '依据用户提供的大纲文字版及20页扫描原件。保留629个既有规范代码；考纲层级独立对应，不按旧教材目录猜测。',
        '统计范围：'+coverage_text(coverage)+'。主考与涉及分开，跨科命中保留；同题及其子题按父题去重。',
        coverage.get('coverage_note',''),
        FREQUENCY_RULE,'',
        '大纲没有逐字列名不等于排除该条目必要机制。scope_status明确区分原文列项、范围内机制、仅前置支撑、范围外、待判断。',
        '历史个人事实不删改；没有个人摘要的考纲点只登记客观范围，不制造薄弱点。小点不借用父点频次冒充自身考次。','']
    for node in data['outline']:
        prefix='#'*(node['level']+2)
        lines += [prefix+' '+node['title'],'',f"- 身份：{node['id']}；文字源第{node['source_line']}行。"]
        if node['details']:lines+=['- 原文细项：'+'；'.join(node['details'])]
        direct=node['direct_canonical_points']
        lines+=['- 直接对应知识点：'+('、'.join(f'[[知识点/{c}|{c}]]' for c in direct) or '保留独立大纲入口；不伪造个人摘要。')]
        f=node['exam_frequency']
        lines += [f"- 已映射子树去重统计：主考 {f['main_count']} 题、涉及 {f['hit_count']} 题；涉及年份："+('、'.join(map(str,f['years'])) or '当前标签未命中')+'。','']
    lines+=['## 全部规范知识点','', '| 规范知识点 | 范围 | 主考题数 | 涉及题数 | 主考年份 | 频次标记 |','|---|---|---:|---:|---|---|']
    for key,c in data['contexts'].items():
        if not re.fullmatch(r'(?:DS|CO|OS|CN)\d{2}-\d{2}',key):continue
        f=c['exam_frequency'];years='、'.join(map(str,f['main_years'])) or '未标注'
        lines.append(f"| [[知识点/{key}\\|{key} {c['title']}]] | {STATUS_NAMES[c['scope_status']]} | {f['main_count']} | {f['hit_count']} | {years} | {f['frequency_label']} |")
    (repo/REL/'考纲索引.md').write_text('\n'.join(lines)+'\n',encoding='utf-8')

def main():
    p=argparse.ArgumentParser()
    p.add_argument('command',choices=['build','frequency','preprocess'])
    p.add_argument('--repo',type=Path,default=Path(__file__).resolve().parents[1])
    p.add_argument('--baseline',type=Path)
    p.add_argument('--exam-index',type=Path,default=INDEX)
    args=p.parse_args()
    if args.command=='preprocess':
        result=preprocess_frequencies(args.repo,args.exam_index)
        print(json.dumps(result['coverage'],ensure_ascii=False))
    elif args.command=='frequency':
        result=build_frequency(args.repo,args.exam_index)
        print(json.dumps({'question_count':result['question_count'],'source_sha256':result['source_sha256']}))
    else:
        if not args.baseline:p.error('--baseline required')
        result=build(args.repo,args.baseline,args.exam_index)
        write_index(args.repo,result)
        print(json.dumps(result['coverage'],ensure_ascii=False))

if __name__=='__main__':main()
