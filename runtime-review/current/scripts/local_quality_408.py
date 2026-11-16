"""Local authoring and readback of the 408 syllabus/exam-form teaching catalog.

Only assessed units from the admitted exam corpus can illustrate an exam form.
The catalog is teacher material. Formal learner records use the native writer.
"""
import argparse
import collections
import hashlib
import json
import re
from functools import lru_cache
from pathlib import Path

import knowledge_profile_408 as profiles
import teaching_research_408 as research
from question_visual_templates_408 import _atomic

REPO = Path(__file__).resolve().parents[1]
ROOT = Path('support/teaching-research-408/syllabus-lessons')
POINT = re.compile(r'(?:DS|CO|OS|CN)\d\d-\d\d')
QUESTION = re.compile(r'EXAM408-(\d{4})-Q\d\d(?:-P\d\d)?')
def read(p):
    return json.loads(p.read_bytes())

def fields(value, keys):
    return {k:value[k] for k in keys.split() if k in value}

def digest(data):
    return hashlib.sha256(data).hexdigest()

def blob(repo, index, key):
    entry = index.get(key)
    if not entry:
        return {}
    data = (repo / entry['ref']).read_bytes()
    if digest(data) != entry['sha256']:
        raise ValueError('原生对象哈希冲突 '+key)
    return json.loads(data)

def exam_year(identity):
    match = QUESTION.fullmatch(identity or '')
    return int(match[1]) if match else None

def blocked(identity, years):
    if exam_year(identity) in years:
        return True
    return any(('_'+str(year)+'_') in identity for year in years)

def put(root, info, data):
    payload=research.raw(data)
    h=digest(payload)
    rel='objects/'+h+'.json'
    target=root/rel
    if not target.is_file():
        target.parent.mkdir(parents=True, exist_ok=True)
        _atomic(target,payload)
    elif digest(target.read_bytes())!=h:
        raise ValueError('已有不可变对象损坏 '+str(target))
    info[data['id']]={'ref':rel,'sha256':h}

def source_files(repo):
    from exam_lesson_408 import REGIONS_REL
    from recap_mechanisms_408 import ASSET_DIR, LIBRARY, SUBJECTS
    from recap_precision_408 import CASES, LAYOUTS
    from syllabus_topic_review_408 import NOTES_REL
    paths={name:repo/path for name,path in {
        'formal_master':'节点总表.md','scope':'support/syllabus-2027/context.json','outline':'support/syllabus-2027/catalog.json',
        'mapping':'support/syllabus-2027/question-syllabus-map.json','relations':'support/syllabus-2027/reviewed-teaching-relations.json',
        'profile':profiles.CURRENT_REL,'route':research.INDEX_REL,'templates':'.private/question-visual-408/current.json',
        'plans':'.private/source-visual-408/current.json'}.items()}
    paths['lesson_compiler']=Path(__file__)
    paths['exact_scene_compiler']=Path(__file__).with_name('exam_lesson_408.py')
    paths['topic_review_compiler']=Path(__file__).with_name('syllabus_topic_review_408.py')
    paths['topic_review_notes']=repo/NOTES_REL
    paths['mechanism_compiler']=Path(__file__).with_name('recap_mechanisms_408.py')
    paths['precision_compiler']=Path(__file__).with_name('recap_precision_408.py')
    paths['precision_fields']=repo/LAYOUTS
    paths['precision_cases']=repo/CASES
    for filename in ('topic-review.html','recap-mechanisms.js'):
        paths[filename]=repo/ASSET_DIR/filename
    for filename in (*SUBJECTS,'points'):
        paths['mechanism_'+filename]=repo/LIBRARY/(filename+'.tsv')
    paths['exam_admission']=repo/'support/syllabus-2027/exam-corpus-acceptance.json'
    if (repo/REGIONS_REL).is_file():
        paths['exam_figure_regions']=repo/REGIONS_REL
    supplement=repo/'support/syllabus-2027/reviewed-subpoint-scopes.json'
    if supplement.exists():
        paths['reviewed_subpoint_scopes']=supplement
    return paths

def source_stat(path):
    info = path.stat()
    return (info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns)


def source_stats(repo):
    return {name: list(source_stat(path)) for name, path in source_files(repo).items()}


@lru_cache(maxsize=32)
def file_digest(path, identity):
    return digest(Path(path).read_bytes())

def sources(repo):
    return {name: file_digest(str(path), source_stat(path))
            for name, path in source_files(repo).items()}

def catalog_drift(repo,index, *, use_source_stats=False):
    expected=index.get('source_digests',{})
    if not expected:
        return []
    try:
        guards=index.get('source_stats') if use_source_stats else None
        if isinstance(guards,dict) and guards:
            observed={}
            for key,path in source_files(repo).items():
                identity=source_stat(path)
                observed[key]=(expected[key] if key in expected and guards.get(key)==list(identity)
                               else file_digest(str(path),identity))
        else:
            observed=sources(repo)
    except OSError as exc:
        return ['unavailable:'+str(exc.filename)]
    return [key for key in sorted(expected.keys() | observed.keys()) if expected.get(key)!=observed.get(key)]


def course_scope(ctx):
    return fields(ctx,'scope_status syllabus_entries teaching_boundary rationale frequency_intro') | {
        'exam_frequency':fields(ctx.get('exam_frequency',{}),'coverage frequency_scope total_count main_count secondary_only_count main_frequency_level primary_count_policy')}

def compile_catalog(repo, years):
    import fcntl
    folder=repo/ROOT
    folder.mkdir(parents=True,exist_ok=True)
    with (folder/'.build.lock').open('a+') as lock:
        fcntl.flock(lock,fcntl.LOCK_EX)
        guards=source_stats(repo)
        fingerprints=sources(repo)
        return _compile(repo,folder,fingerprints,guards,years)

def _compile(repo, folder, fingerprints, guards, years):
    outline=repo/'support/syllabus-2027'
    from syllabus_context_408 import load_contexts
    contexts=load_contexts(repo)
    nodes=read(outline/'catalog.json')['nodes']
    from recap_mechanisms_408 import load_library, with_outline
    diagrams=with_outline(load_library(repo),nodes)
    parents = {node['id']:node['parent_id'] for node in nodes}
    def matches_point_scope(anchor, allowed):
        while anchor:
            if anchor in allowed:
                return True
            anchor = parents[anchor]
        return False
    mapping=read(outline/'question-syllabus-map.json')
    from build_true_exam_corpus import accepted_coverage
    expected=accepted_coverage(repo)['expected_ids']
    if set(mapping['exam_items'])!=expected:
        raise ValueError('local_syllabus_corpus_identity_coverage')
    edges=read(outline/'reviewed-teaching-relations.json')['relations']
    from lesson_alignment_408 import load_assessment_catalog,_extensions
    pi=profiles.current_index(repo)['profiles']
    routes=read(repo/research.INDEX_REL)['targets']
    qroot=repo/'.private/question-visual-408'
    question_index=read(qroot/'current.json')
    qi=question_index['templates']
    held_alias_targets={exam for fid, exams in question_index.get('aliases',{}).items() if blocked(fid,years) for exam in exams}
    sp=read(repo/'.private/source-visual-408/current.json')['question_plans']
    exam_catalog=load_assessment_catalog(repo,contexts)
    for code,rows in exam_catalog['by_point'].items():
        exam_catalog['by_point'][code]=[r for r in rows if not blocked(r['exam_id'],years) and r['exam_id'] not in held_alias_targets]

    ids={}
    lesson_ids={}
    exam_audit={}
    checked_images={}
    forms=collections.defaultdict(list)
    by_outline=collections.defaultdict(list)
    counters=collections.Counter()
    held=[]
    protected_tags = re.compile('|'.join(
        rf'EXAM408-{y}-Q[0-9]+|(?:DS|CO|OS|CN)_{y}_|{y}\s*(?:年(?:真题|第?\d+题)|Q\d)' for y in years)) if years else None

    def is_protected(text):
        return isinstance(text,str) and (bool(protected_tags and protected_tags.search(text)) or any(q in text for q in held_alias_targets))

    def mask(value):
        if isinstance(value,str):
            return '〔本次保护年度的来源已留在原库〕' if is_protected(value) else value
        if isinstance(value,dict):
            # A protected example can be an object key (layout_examples) or a row's provenance.
            # Remove the complete example, rather than retaining its linked answer/geometry.
            if any(is_protected(v) for k,v in value.items() if k in {
                    'identity','question_id','question_identity','exam_id','canonical_id','formal_id','source_question','adjacent_question'}):
                return None
            result={}
            for k,v in value.items():
                if not is_protected(k):
                    cleaned=mask(v)
                    if v is None or cleaned is not None:
                        result[k]=cleaned
            return result
        if isinstance(value,list):
            result=[]
            for v in value:
                cleaned=mask(v)
                if v is None or cleaned is not None:
                    result.append(cleaned)
            return result
        return value

    def store(kind,key,**data):
        put(folder,ids,dict(kind=kind,id=key,**mask(data)))
        counters[kind]+=1

    from exam_lesson_408 import REGIONS_REL, _geometry, compile_exact, reviewed_geometry
    authored_regions=read(repo/REGIONS_REL) if (repo/REGIONS_REL).is_file() else {}
    if authored_regions and authored_regions['schema']!='cs408-reviewed-exam-regions-v1':
        raise ValueError('local_syllabus_exam_region_schema')
    details=Path(read(outline/'exam-frequency.json')['source_path']).parents[3]
    template_rows={key:blob(qroot,qi,key) for key in (*mapping['exam_items'],*mapping['subquestions'])}
    exact_parts=collections.defaultdict(list)
    for key,row in sorted(mapping['subquestions'].items()):
        exact_parts[row['parent_exam_id']].append(key)
    for parent,row in sorted(mapping['exam_items'].items()):
        children=exact_parts[parent]
        part_units={unit['unit_id'] for unit in row['frequency_primary_units'] if unit['unit_id']!=parent}
        if part_units!=set(children) or (row['question_type']=='单项选择题')==bool(children):
            raise ValueError('local_syllabus_subquestion_identity_coverage:'+parent)
        is_held=blocked(parent,years) or parent in held_alias_targets
        parent_plan=blob(repo/'.private/source-visual-408',sp,parent)
        parent_regions=_geometry(repo,parent,parent_plan)
        for key in [parent,*children]:
            lesson=compile_exact(
                repo,details,key,template_rows[key],blob(repo/'.private/source-visual-408',sp,key),
                mapping['subquestions'].get(key),checked_images=checked_images,with_display_copies=not is_held,
                reviewed_regions=reviewed_geometry(authored_regions,key,parent_regions),
                step_regions=authored_regions.get('step_regions',{}).get(key),
                parts=[template_rows[part] for part in children] if key==parent else (),
            )
            exam_audit[key]=fields(lesson,'basis source_review_status diagram_count display_roles hotspot_count source_objects_covered focused_steps') | {
                'subject':row['subject'],'access':'withheld' if is_held else 'compiled',
            }
            if not is_held:
                put(folder,lesson_ids,dict(id=key,**lesson))
        if not is_held and children:
            template=template_rows[parent]
            store('exam_parent',parent,parts=children,assessed=row['hit_knowledge'],
                  syllabus_ids=row['hit_syllabus_ids'],actual_task=template['target'],
                  given=template['given_objects'],initial_state=template['initial_state'],
                  steps=template['steps'],checks=template['checks'])

    for parent, row in sorted(mapping['exam_items'].items()):
        if blocked(parent,years) or parent in held_alias_targets:
            continue
        for unit in row['frequency_primary_units']:
            key=unit['unit_id']
            codes=unit['assessed_points']
            template=template_rows[key]
            small=mapping['subquestions'].get(key,{})
            tags=[p for p in row['assessment_parts'] if
                  (p['part_id'].split(':')[0].split('：')[0]==key.rsplit('-',1)[-1] if small else p['part_id'].startswith('choice'))]
            focus={code:[t['focus'] for t in tags if code in t['canonical_points']] for code in codes}
            known=template.get('given_objects',[])
            assessment={
                'unit':key,'parent':parent,'type':small.get('question_type') or row['question_type'],
                'primary':unit['primary_point'],'assessed':codes,'syllabus_ids':unit['syllabus_ids'],
                'focus_by_point':focus,'official_part':small.get('source_part_label'),
                'actual_task':small.get('title') or template.get('target'),'given':known,
                'initial_state':template.get('initial_state'),'first_action':(template.get('steps') or [None])[0],
                'question_card':str(Path('corpus/408统考真题')/('subquestions/cards' if small else 'cards')/(key+'.md')),
                'reviewed_prompt_hash':small.get('surface_sha256'),
                'base_template_hash':(qi.get(key) or {}).get('sha256'),'base_plan_hash':(sp.get(key) or {}).get('sha256'),
                'visual':fields(template.get('exam_visual_binding',{}),'basis preserve focus_by_step source_figure_inspection_completed'),
                'checks':template.get('checks',[]),'steps':template.get('steps',[])}
            model=repo/'.private/study-visuals-3d/current'/(key+'.json')
            if model.is_file():
                assessment['model_3d']=fields(read(model),'question_id scope_id status stages runtime_sha256 source_version')
            store('exam_unit',key,**assessment)
            for code in codes:
                point_refs={n['id'] for n in contexts.get(code,{}).get('syllabus_entries',[])}
                facets=[p for p in tags if code in p['canonical_points']]
                for facet in facets or [{'part_id':key,'focus':assessment['actual_task'],'syllabus_ids':unit['syllabus_ids']}]:
                    intersection=sorted(a for a in facet['syllabus_ids'] if matches_point_scope(a,point_refs))
                    forms[code].append({'unit':key,'type':assessment['type'],
                        'assessment_part':facet['part_id'],'focus':[facet['focus']],
                        'role':'主考' if unit['primary_point']==code else '兼考',
                        'assessed_syllabus_in_point_scope':intersection,
                        'use':'verified_form' if intersection else 'assessment_record_requires_scope_crosscheck'})
    formal_per_point=collections.defaultdict(list)
    from exam_point_links_408 import formal_point_binding
    from intake_lib_408 import Repo
    question_bindings = {q: {'assessed_points':val['hit_knowledge'], 'syllabus_ids':val['hit_syllabus_ids']}
                         for q,val in mapping['exam_items'].items()}
    for key,master_row in Repo(repo).master().items():
        row = formal_point_binding(master_row,question_bindings,contexts)
        exam=row.get('canonical_exam_id')
        if blocked(key,years) or blocked(exam or '',years):
            held.append(key)
            continue
        aliases=[q for q in question_index.get('aliases',{}).get(key,[]) if q in qi]
        if any(blocked(q,years) or q in held_alias_targets for q in aliases):
            held.append(key)
            continue
        target=key if key in qi else exam if exam in qi else aliases[0] if len(aliases)==1 else None
        template=blob(qroot,qi,target)
        model_identity=exam if exam else aliases[0] if len(aliases)==1 else None
        codes=row['assessed_points']
        for code in codes:
            formal_per_point[code].append(key)
        store('formal_case',key,points=codes,canonical_id=exam,binding_basis=row.get('binding_basis'),
              exact_template_id=target,native_visual_aliases=aliases,template_sha256=(qi.get(target) or {}).get('sha256'),source_plan_sha256=(sp.get(target) or {}).get('sha256'),
              reviewed_template=fields(template,'target given_objects initial_state steps checks source_review_status'),
              visual=fields(template.get('exam_visual_binding',{}),'basis preserve focus_by_step source_figure_inspection_completed'),
              three_d_at=model_identity if model_identity and (repo/'.private/study-visuals-3d/current'/(model_identity+'.json')).is_file() else None)
    point_rows={}
    for point,context in sorted(contexts.items()):
        if not POINT.fullmatch(point):
            continue
        route=blob(repo/research.INDEX_REL.parent,routes,point)
        profile=profiles.load_profile_version(repo,point,(pi.get(point) or {}).get('sha256'))
        small_keys=[]
        for sub in (profile or {}).get('subpoints',[]):
            sid=sub['subpoint_id']
            small_keys.append(sid)
            sctx=contexts.get(sid,{})
            sr=blob(repo/research.INDEX_REL.parent,routes,sid)
            store('subpoint',sid,parent=point,title=sub['title'],keywords=sub['keywords'],
                  syllabus=course_scope(sctx) if sctx else {'scope_status':'needs_independent_review','inherited_boundary':context['teaching_boundary']},
                  separate_frequency_available=(sctx.get('exam_frequency',{}).get('frequency_scope')=='subpoint'),
                  teacher_steps=sr.get('ordered_blocks',[]),visual_insertions=sr.get('visual_insertions',[]),
                  exact_bounded_extension=sr.get('bounded_extension'),dedicated_route_status='available' if sr else 'needs_specific_authoring',
                  original_profile_ref={'id':point,'subpoint_id':sid,'base_version':pi[point]['sha256']})
        for n in context.get('syllabus_entries',[]):
            by_outline[n['id']].append(point)
        evidence=sorted(forms[point],key=lambda a:(a['role']!='主考',a['unit']))
        by_form=collections.defaultdict(list)
        for x in evidence:by_form[x['type']].append(x)
        point_rows[point]=dict(title=context['title'],syllabus=course_scope(context),profile_version=(pi.get(point) or {}).get('sha256'),
            route_version=(routes.get(point) or {}).get('sha256'),teacher_steps=route.get('ordered_blocks',[]),
            visual_insertions=route.get('visual_insertions',[]),small_ids=small_keys,exam_forms=dict(by_form),
            formal_cases=sorted(formal_per_point[point]),reviewed_relations=[])
    for edge in edges:
        a=edge['source_point']
        b=edge['target_point']
        if a not in point_rows or b not in point_rows:
            continue
        if any(blocked(x.get('exam_id',''),years) or x.get('exam_id') in held_alias_targets for x in edge.get('target_assessments',[])):
            continue
        point_rows[a]['reviewed_relations'].append(fields(edge,'target_point relation basis target_assessments'))
    for point, row in point_rows.items():
        # Non-direct forms are labeled as a related target's assessment; these never
        # inflate this point's frequency or a student's personal performance.
        candidate_rows=_extensions([point],point,exam_catalog,contexts,limit=10000)['candidates']
        expanded=[]
        for candidate in candidate_rows:
            relation=candidate['knowledge_relation']
            if relation['relation']=='same_point':
                continue
            brief=fields(candidate,'exam_id part_id knowledge_point focus syllabus_ids')
            brief['relation']=fields(relation,'source_point target_point relation relationship_basis source_syllabus_entry target_syllabus_entry')
            expanded.append(brief)
            if len(expanded)>=8:
                break
        row['related_exam_forms']=expanded
    from syllabus_topic_review_408 import NOTES_REL, SCOPE, compile_material
    topics=compile_material(nodes,mapping,template_rows,point_rows,read(repo/NOTES_REL),
                            exam_catalog['relations'],
                            {k for k in expected if blocked(k,years) or k in held_alias_targets},diagrams)
    from recap_precision_408 import compile_cases
    case_models,stale_cases=compile_cases(repo,template_rows,topics['by_question'])
    topics['audit']['precise_cases']=len(case_models)
    topics['audit']['case_parameters_need_recheck']=stale_cases
    if not {key for key in diagrams if POINT.fullmatch(key)} <= point_rows.keys():
        raise ValueError('mechanism_canonical_identity_not_registered')
    scoped_points=0
    for point,row in point_rows.items():
        row['recap_related']=topics['related'].get(point,[])
        if row['syllabus']['scope_status'] in SCOPE:
            anchors=[entry['id'] for entry in row['syllabus']['syllabus_entries'] if entry['id'] in diagrams]
            if not anchors:
                raise ValueError('mechanism_canonical_point_without_reviewed_anchor:'+point)
            if point in diagrams:
                row['recap_override']={'anchors':anchors,'model':diagrams[point],'title':row['title'],
                                      'frequency':row['syllabus']['exam_frequency']}
            scoped_points+=1
        store('point',point,**row)
    topics['audit']['scoped_canonical_points']=scoped_points
    topics['audit']['canonical_overrides']=sum('recap_override' in row for row in point_rows.values())
    for node in nodes:
        sid=node['id']
        frequency=fields(contexts[sid].get('exam_frequency',{}),
                         'main_frequency_level main_count secondary_only_count')
        store('syllabus',sid,entry=node,points=by_outline[sid],
              review=topics['materials'][sid] | {'frequency':frequency})
    if sources(repo)!=fingerprints or source_stats(repo)!=guards:
        raise ValueError('编制时原生源已更新；已产出的不可变对象保留，current未切换，请按新版本重新核对')
    index={'schema':'cs408-local-syllabus-lessons-v1','source_digests':fingerprints,'source_stats':guards,'excluded_years':sorted(years),
           'exam_lesson_objects':lesson_ids,'exam_lesson_audit':exam_audit,
           'topic_review_by_question':topics['by_question'],'topic_review_audit':topics['audit'],
           'topic_case_parameters':case_models,
           'objects':ids,'counts':dict(counters),'held_formal_ids':sorted(held),'held_alias_exam_ids':sorted(held_alias_targets),'teacher_only':True,'learning_writes':0}
    current=folder/'current.json'
    content=research.raw(index)
    if not current.exists() or current.read_bytes()!=content:
        _atomic(current,content)
    return fields(index,'schema counts excluded_years learning_writes') | {'catalog_sha256':digest(content),'held_formal_count':len(held)}


def render_exam_lesson(repo, identity, output, *, view, hint_level, inline_root=None, expected_version=None):
    if (view,hint_level)!=('direct',5):
        raise ValueError('local_syllabus_lesson_requires_direct_level_five')
    from exam_lesson_408 import verify_pictures
    from render_interactive_408 import render, render_with_layout
    index, version = catalog(repo)
    if catalog_drift(repo,index):
        raise ValueError('local_syllabus_lesson_requires_rebuild')
    if expected_version and expected_version!=version:
        raise ValueError('local_syllabus_lesson_version_changed')
    if index.get('exam_lesson_audit',{}).get(identity,{}).get('access') == 'withheld':
        raise ValueError('local_syllabus_lesson_withheld')
    lesson = blob(repo/ROOT,index.get('exam_lesson_objects',{}),identity)
    if not lesson:
        raise ValueError('local_syllabus_exact_lesson_unavailable')
    verify_pictures(lesson)
    original = lesson['scene'] | {'source_version':version}
    output_receipt = None
    if inline_root is not None and lesson['basis'] == 'text_only_mechanism':
        from visual_layout_408 import exact_layout
        unit = blob(repo/ROOT, index['objects'], identity)
        selected = exact_layout(repo, identity, unit, version)
        if selected is not None:
            scene, layout = selected
            output_receipt = render_with_layout(scene, layout, 'auto', output,
                                                inline_root=inline_root, fallback_raw=original)
    if output_receipt is None:
        output_receipt = render(original, output, inline_root=inline_root)
    return output_receipt | {
        'identity':identity,'catalog_sha256':version,'source_basis':lesson['basis'],
        'source_review_status':lesson['source_review_status'],'focus_bound_steps':lesson['focused_steps'],
    }


def topic_review(repo, identity, view, *, expected_version=None, withheld=(), years=(), case_input=None):
    if view!='direct':
        return {'status':'topic_review_requires_authorized_disclosure'}
    from syllabus_topic_review_408 import SCOPE, chat_overview, project
    index,version=catalog(repo)
    if not index.get('topic_review_audit'):
        return {'status':'topic_review_catalog_unavailable'}
    drift=catalog_drift(repo,index)
    if drift:
        return {'status':'topic_review_requires_rebuild','changed_sources':drift}
    if expected_version and expected_version!=version:
        return {'status':'topic_review_version_changed','catalog_sha256':version}
    years=set(years)|set(index['excluded_years'])
    if blocked(identity,years) or identity in index.get('held_formal_ids',[]):
        return {'status':'topic_review_withheld'}
    objects=index['objects']
    get=lambda key:blob(repo/ROOT,objects,key)
    aliases=index['topic_review_by_question']
    exact=identity if QUESTION.fullmatch(identity) else None
    detail_id=re.fullmatch(r'(S27-(?:DS|CO|OS|CN)(?:-\d{2}){1,3})#([1-9]\d*)',identity)
    item=get(detail_id[1] if detail_id else identity)
    focus_point=None
    preferred=[]
    allowed_anchors=[]
    if item.get('kind')=='formal_case' and item.get('canonical_id'):
        exact=item['canonical_id']
    if exact:
        if blocked(exact,years) or index.get('exam_lesson_audit',{}).get(exact,{}).get('access')=='withheld':
            return {'status':'topic_review_withheld'}
        record=aliases.get(exact,{})
        roots=record.get('roots',[])
        points=record.get('assessed_points',[])
        allowed_anchors=record.get('anchors',[])
    elif item.get('kind')=='syllabus':
        roots=[item['id']]
        allowed_anchors=roots
        points=[]
    else:
        if item.get('kind') in {'point','subpoint'} and item['syllabus']['scope_status'] in SCOPE:
            points=[item['parent']] if item['kind']=='subpoint' else [identity]
            focus_point=points[0]
            scopes=[item['syllabus']]
        elif item.get('kind')=='formal_case':
            points=item.get('points',[])
            if len(points)==1:
                focus_point=points[0]
            scopes=[get(code).get('syllabus',{}) for code in points]
        else:
            points=[]
            scopes=[]
        roots=[]
        for scope in scopes:
            if scope.get('scope_status') not in SCOPE:
                continue
            for entry in scope.get('syllabus_entries',[]):
                preferred.append(entry['id'])
                root=next((node['id'] for node in entry.get('path',[]) if node['id'].count('-')==3),entry['id'])
                if root not in roots:
                    roots.append(root)
        allowed_anchors=preferred
    if not roots:
        return {'status':'topic_review_unavailable'}
    point_objects={code:get(code) for code in points}
    related={code:row.get('recap_related',[]) for code,row in point_objects.items()}
    overrides={code:row['recap_override'] for code,row in point_objects.items() if row.get('recap_override')}
    withheld=[_case_exam(repo,index,key) or key for key in withheld]
    case_withheld=bool(exact and (exact in withheld or exact.split('-P')[0] in withheld))
    pinned=None if case_withheld else index.get('topic_case_parameters',{}).get(exact)
    precision={'prepared':pinned} if pinned else {}
    if case_input is not None:
        from recap_precision_408 import case_context, load_fields
        if case_withheld and isinstance(case_input,dict) and any(case_input.get(key) for key in ('inputs','recall')):
            raise ValueError('the current question is explicitly withheld from custom recap')
        precision=case_context(case_input,identity=identity,exact=exact,points=points,anchors=allowed_anchors,
                               pinned=pinned,profiles=load_fields(repo))
        point=precision.get('point')
        if point:
            compatible=[entry['id'] for entry in point_objects[point]['syllabus']['syllabus_entries']
                        if entry['id'] in allowed_anchors]
            if not compatible or (precision.get('anchor') and precision['anchor'] not in compatible):
                raise ValueError('current response focus does not match the selected outline mechanism')
            focus_point=point
            if not precision.get('anchor'):
                precision['anchor']=pinned['anchor'] if pinned and pinned['anchor'] in compatible else compatible[0]
        if (precision.get('highlight') or precision.get('step')) and not precision.get('anchor'):
            if not pinned:
                raise ValueError('a field or message highlight needs an explicit outline anchor')
            precision['anchor']=pinned['anchor']
    selected_anchor=precision.get('anchor') or (pinned or {}).get('anchor')
    if selected_anchor:
        preferred=[selected_anchor]
    result=project(roots,points,related,get,exact=exact,withheld=withheld,years=years)
    if detail_id:
        topic=result['topics'][0]
        section=topic['sections'][0]
        details=section['details']
        number=int(detail_id[2])
        if number>len(details) or details[number-1] is None:
            return {'status':'topic_review_detail_unavailable'}
        detail=details[number-1]
        topic['sections']=[section | {'id':identity,'title':'原纲附列细项','children':[], 'map_branches':[],
            'status':'原纲附列细项','frequency':{},'forms':[], 'mechanisms':[detail], 'details':[],
            'map_card':None,'mechanism':detail['mechanism']}]
    return {'status':'available','identity':identity,'exact_identity':exact,'catalog_sha256':version,'review':result,
            'mechanism_selection':{'focus_point':focus_point,'preferred':preferred,'overrides':overrides,
                                   'precision':precision},
            'chat_overview':chat_overview(result),'learning_write_count':0}

def catalog(repo):
    path=repo/ROOT/'current.json'
    if not path.is_file():
        return {},{}
    return cached_index(str(path),source_stat(path))

@lru_cache(maxsize=4)
def cached_index(path,identity):
    data=Path(path).read_bytes()
    return json.loads(data),digest(data)

def refresh_registered_course(repo):
    """Finish only an existing local course, after the native postcommit projection."""
    index, version = catalog(repo)
    if not index:
        return None
    drift = catalog_drift(repo, index)
    if drift:
        # Do not advance a study date or drop previously registered protected years.
        compile_catalog(repo, set(index['excluded_years']))
        index, version = catalog(repo)
        remaining = catalog_drift(repo, index)
        if remaining:
            raise ValueError('local_syllabus_course_still_stale:' + ','.join(remaining))
    return {'status': 'rebuilt_after_native_refresh' if drift else 'already_current',
            'catalog_sha256': version, 'source_check': 'current',
            'changed_sources': drift, 'excluded_years': index['excluded_years']}


def select_forms(record, excluded_family=None):
    for form, items in record.get('exam_forms',{}).items():
        candidates=[r for r in items if r['use']=='verified_form' and r['unit'].split('-P')[0]!=excluded_family]
        if candidates:
            yield form,candidates

def teaching_lookup(repo,key,view, *, current_question=None, all_forms=False):
    """Resolve exact-ID material. The protected path reads no catalog or teacher files."""
    if view=='protected':
        return {'status':'protected'}
    idx,ver=catalog(repo)
    drift=catalog_drift(repo,idx)
    if drift:
        return {'status':'materials_need_local_rebuild','changed_sources':drift,'teacher_only':True,'frontend_ready':False}
    item=blob(repo/ROOT,idx.get('objects',{}),key)
    catalog_type=item.get('kind')
    if not item:
        return {'status':'unavailable'}
    base={'status':'teacher_material_available','catalog_sha256':ver,'source_check':'current','id':key,'kind':catalog_type,
          'teacher_only':True,'frontend_ready':False}
    if view!='direct':
        return base
    current=current_question or ''
    if current in idx['objects'] and not QUESTION.fullmatch(current):
        case=blob(repo/ROOT,idx['objects'],current)
        alias=case.get('exact_template_id')
        current=case.get('canonical_id') or (alias if QUESTION.fullmatch(alias or '') else current)
    family=current.split('-P')[0]
    family_match=QUESTION.fullmatch(family)
    related_refs=[family] if family else []
    if family_match:
        year=family_match[1]
        number=family.rsplit('Q',1)[-1]
        related_refs += [year+'Q'+number,year+'年Q'+number,year+'年第'+str(int(number))+'题']
    def references_current_family(row):
        material=json.dumps(row,ensure_ascii=False)
        return any(tag in material for tag in related_refs)
    if '-P' in current:
        candidate=item.get('canonical_id') or key
        if QUESTION.fullmatch(candidate) and candidate!=current and candidate.split('-P')[0]==family:
            return base|{'status':'withheld_parent_or_sibling'}
        if catalog_type=='subpoint' and references_current_family(item.get('exact_bounded_extension')):
            item=dict(item,exact_bounded_extension=None)
    if catalog_type=='point':
        obj=fields(item,'title syllabus teacher_steps visual_insertions small_ids reviewed_relations route_version')
        if item['syllabus'].get('scope_status') not in {'explicit','within_scope'}:
            return base | fields(item,'title syllabus') | {'exam_forms':{},'status':'scope_limited'}
        # A family-scoped read cannot carry a parent or sibling reference, including in
        # an otherwise valid cross-point rationale; the complete record stays teacher-private.
        def safe_related(row):
            return not references_current_family(row)
        obj['reviewed_relations']=[r for r in obj.get('reviewed_relations',[]) if safe_related(r)]
        obj['related_exam_forms']=[r for r in item.get('related_exam_forms',[]) if safe_related(r)]
        if '-P' in current:
            exact_unit=blob(repo/ROOT,idx['objects'],current)
            if key not in exact_unit.get('assessed',[]):
                return base|{'status':'point_outside_current_subquestion'}
            obj['teacher_steps']=[]
            obj['visual_insertions']=[]
            obj['current_subquestion_route']=current
        grouped={}
        coverage={}
        seen_mechanisms={}
        for form,items in select_forms(item,family):
            grouped[form]=[]
            for pos,row in enumerate(items):
                ex=blob(repo/ROOT,idx['objects'],row['unit'])
                example=row | fields(ex,'actual_task given initial_state first_action checks question_card')
                if row.get('assessment_part','').startswith('choice-'):
                    example['first_action_role']='整道选择题的原教案起手；细分断言的检查边界取本条focus。'
                if all_forms or pos==0:
                    grouped[form].append(example)
                for question_focus in row['focus']:
                    # The same exam format may test several distinct mechanisms. Group only
                    # equivalent focus/task wording, with every distinct condition accessible.
                    task=ex.get('actual_task')
                    marker=(form,question_focus,task)
                    if marker not in seen_mechanisms:
                        entry={'focus':question_focus,'task':task,'conditions':[],
                               'point_entry':(item.get('teacher_steps') or [{}])[0].get('teacher_text')}
                        coverage.setdefault(form,[]).append(entry)
                        seen_mechanisms[marker]=entry
                    conditions=fields(ex,'given initial_state')
                    target=seen_mechanisms[marker]['conditions']
                    previous=next((x for x in target if x['conditions']==conditions and
                                  x['first_action']==ex.get('first_action')),None)
                    ref=fields(row,'unit assessment_part role assessed_syllabus_in_point_scope')
                    if previous is not None:
                        previous['assessments'].append(ref)
                    else:
                        target.append({'conditions':conditions,'first_action':ex.get('first_action'),'assessments':[ref]})
        return base | obj | {'exam_forms':grouped, 'exam_focuses_by_form':coverage,
                             'actual_unit_count':len({r['unit'] for _,items in select_forms(item,family) for r in items}),
                             'read_current_personal_context':key,
                             'scope_unmatched_units':[x['unit'] for rs in item['exam_forms'].values() for x in rs
                                                      if x['use']!='verified_form' and x['unit'].split('-P')[0]!=family]}
    if catalog_type=='exam_parent':
        parts=[blob(repo/ROOT,idx['objects'],part) for part in item['parts']]
        return base | item | {'independent_lessons':[
            fields(part,'id official_part actual_task assessed syllabus_ids first_action') for part in parts
        ]}
    if catalog_type in {'exam_unit','formal_case'}:
        model_id=key if catalog_type=='exam_unit' else item.get('three_d_at')
        if model_id:
            pointer=repo/'.private/study-visuals-3d/current'/(model_id+'.json')
            if pointer.is_file():
                current_model=read(pointer)
                if current_model['question_id']!=model_id:
                    raise ValueError('3D课件身份不一致：'+model_id)
                item=dict(item,model_3d=fields(current_model,'question_id scope_id status stages runtime_sha256 source_version'),
                          original_stage_lookup='scripts/study_visuals_408.py resolve --id '+model_id+' --stage question')
            else:
                item=dict(item,model_3d=None,model_status='current_model_unavailable')
    if catalog_type=='subpoint' and item['syllabus'].get('scope_status') not in {'explicit','within_scope'}:
        return base | fields(item,'parent title syllabus original_profile_ref') | {'status':'scope_limited'}
    if catalog_type=='syllabus':
        return base | fields(item,'entry points')
    return base | item

def _case_exam(repo,index,identity):
    if QUESTION.fullmatch(identity or ''):
        return identity
    record=blob(repo/ROOT,index['objects'],identity)
    return record.get('canonical_id') or record.get('exact_template_id')

def attach_quality_context(repo,query,result,view, *, compact=False):
    """A lightweight source-versioned pointer in the standard hot teaching response."""
    if query.get('stage')!='post_answer' or view not in {'after_attempt','direct'} or result.get('status') not in {'available','unchanged'}:
        return result
    index,version=catalog(repo)
    if not index:
        return result
    drift=catalog_drift(repo,index,use_source_stats=compact)
    if drift:
        return result|{'local_syllabus_lessons':{'status':'pending_local_rebuild','changed_sources':drift}}
    # Canonical IDs only. An independent subquestion can open only its own source.
    current=query.get('current_question_id') or query.get('exam_question_id')
    if not current and not {'current_question_id','exam_question_id'} <= query.keys():
        current=query.get('formal_id')
    part=query.get('current_question_part')
    if part and current and not current.endswith('-'+part):
        exact=(_case_exam(repo,index,current) or current)+'-'+part
        if exact not in index['objects']:
            # A private teaching part may have no admitted exam-unit identity.
            # Withhold the family rather than giving a parent template for that part.
            return result
        current=exact
    keys=[current]
    subkeys={r.get('subpoint_id') for r in result.get('records',[]) if r.get('subpoint_id')}
    keys+=sorted(subkeys) if subkeys else list(query.get('knowledge_points',[]))
    found=[k for k in dict.fromkeys(keys) if k in index['objects']]
    if not found:
        return result
    exact_lesson=current if current in index.get('exam_lesson_objects',{}) else _case_exam(repo,index,current)
    pointer={'schema':index['schema'],'catalog_sha256':version,'source_check':'current','identities':found,
            'exact_lesson_identity':exact_lesson if exact_lesson in index.get('exam_lesson_objects',{}) else None,
            'topic_review_identity':current if current in index.get('topic_review_by_question',{}) else (
                current if index.get('topic_review_audit') and current in found else None)}
    if not compact:
        pointer.update(
            author_cli='scripts/local_quality_408.py context --id IDENTIFIER --view direct'+(' --current-question '+keys[0] if keys[0] and keys[0] in index['objects'] else ''),
            use='本题主线解决且权限允许后，按精确身份读取本点纲内要求与真实问法；KSP只用本小点，教师材料不得进入未授权学生界面。')
    return result | {'local_syllabus_lessons':pointer}

def profile_patch(repo,draft,apply, *, prepare_only=False):
    point=draft['point']
    target=draft.get('subpoint_id') or point
    old=profiles.load_profile_version(repo,point,draft['expected_previous_version'])
    if old is None:
        raise ValueError('该点尚无正式个人概况；新证据走原生正式流程，客观讲法可独立编制')
    old_ref=repo/profiles.ROOT_REL/'objects'/(draft['expected_previous_version']+'.json')
    proof={'path':str(old_ref.resolve()),'sha256':digest(old_ref.read_bytes()),'locator':'complete_previous_profile:'+point}
    ignored={'schema','operation_id','accepted_at','origin','evidence_fingerprint','semantic_evidence_contract',
        'semantic_evidence_fingerprint','cross_point_evidence_contract','cross_point_evidence_fingerprints','independent_mastery_inferred'}
    def still_valid(ref):
        path=Path(ref.get('path',''))
        try:
            return path.is_file() and file_digest(str(path),source_stat(path))==ref['sha256']
        except OSError:
            return False
    update={k:v for k,v in old.items() if k not in ignored}
    update.update(expected_previous_version=draft['expected_previous_version'],
                  authorship_origin='local_explicitly_authorized_semantic_content',
                  source_refs=[ref for ref in old.get('source_refs',[]) if still_valid(ref)]+[proof]+draft['source_refs'])
    update.pop('removed_subpoint_ids',None)
    update.pop('removal_reasons',None)
    changed=draft['changes']
    allowed={'overview','short_summary','next_check','teaching_focus','teaching_plan',
             'claims_schema','claims'}
    selected=update if target==point else next((s for s in update.get('subpoints',[]) if s['subpoint_id']==target),None)
    if selected is None or not changed or set(changed)-allowed or ('overview' in changed and target!=point):
        raise ValueError('无效的本点或修订字段')
    if not any(selected.get(k)!=v for k,v in changed.items()):
        raise ValueError('成稿与旧值相同，无需写入')
    for obj in [update]+update.get('subpoints',[]):
        if obj is not update:
            prior=obj.get('source_refs',[])
            preserved=[r for r in prior if still_valid(r)]
            if len(preserved)!=len(prior):
                preserved.append(dict(proof,locator='subpoints.'+obj['subpoint_id']+'.source_refs'))
            obj['source_refs']=preserved
        for field in ('teaching_focus','teaching_plan'):
            obj.pop(field+'_update',None)
            if obj is selected and field in changed and obj.get(field)!=changed[field] and obj.get(field):
                obj[field+'_update']={'action':'revise','reason':draft['review_reason']}
    selected.update(changed)
    bindings={}
    def verify(ref):
        p=Path(ref['path'])
        if not p.is_file() or digest(p.read_bytes())!=ref['sha256']:
            raise ValueError('来源字节不符 '+str(p))
        bindings[str(p)]=ref['sha256']
    if not all(ref.get('locator') for ref in draft['source_refs']):
        raise ValueError('修订稿需给来源定位')
    for ref in update['source_refs']:
        verify(ref)
    for obj in [update]+update.get('subpoints',[]):
        for ref in obj.get('source_refs',[]):
            verify(ref)
        for key in ('teaching_plan','teaching_focus'):
            for ref in (obj.get(key) or {}).get('source_refs',[]):
                verify(ref)
        for claim in obj.get('claims', []):
            for ref in claim.get('source_refs', []):
                verify(ref)
    # Formal IDs in this historical maintenance request are already resolved.
    formals={q['logical_question_id']:q['formal_id'] for q in update.get('question_refs',[]) if q.get('logical_question_id') and q.get('formal_id')}
    kwargs=dict(evidence_bindings=bindings,formal_bindings=formals,expected_points=[point])
    if prepare_only:
        return update, kwargs
    op='local-quality-408:'+digest(research.raw(draft))
    if not apply:
        profiles.validate_updates(repo,[update],check_versions=True,**kwargs)
        return {'status':'VALIDATED','point':point,'target':target,'operation_id':op}
    return profiles.apply_updates(repo,[update],op,**kwargs)

def apply_scopes(repo,drafts,apply):
    """Install bounded local KSP-scope reviews without editing exam or personal evidence."""
    import fcntl
    from contextlib import nullcontext
    dest=repo/'support/syllabus-2027/reviewed-subpoint-scopes.json'
    folder=dest.parent
    raw_outline=read(folder/'catalog.json')
    official={n['id'] for n in raw_outline['nodes']}
    baseline=read(folder/'context.json')['contexts']
    profile_index=profiles.current_index(repo)['profiles']
    lock=(folder/'.reviewed-subpoint-scopes.lock').open('a+') if apply else nullcontext()
    with lock as stream:
        if apply:
            fcntl.flock(stream,fcntl.LOCK_EX)
        layer=read(dest) if dest.exists() else {'schema':'cs408-reviewed-subpoint-scopes-v1',
              'syllabus_text_sha256':raw_outline['source']['text_sha256'],'subpoints':{}}
        if layer['syllabus_text_sha256']!=raw_outline['source']['text_sha256']:
            raise ValueError('原纲来源已变，逐项重核范围')
        changed=[]
        seen=set()
        for draft in drafts:
            sid=draft['subpoint_id']
            point=draft['point']
            if sid in seen or sid in baseline or draft.get('layer')!='scope':
                raise ValueError('重复、非补充范围或已有独立主表映射：'+sid)
            seen.add(sid)
            parent=profiles.load_profile_version(repo,point,profile_index[point]['sha256'])
            sub=next((r for r in parent.get('subpoints',[]) if r['subpoint_id']==sid),None)
            data=draft['scope']
            if not sub or data.get('parent')!=point or data.get('title')!=sub['title']:
                raise ValueError('新纲目与已正式核定的小点身份不符')
            if data.get('scope_status') not in {'explicit','within_scope','prerequisite_only','out_of_scope','needs_review'}:
                raise ValueError('无效教学范围')
            if (not data.get('teaching_boundary') or not data.get('rationale') or
                    not set(data.get('syllabus_ids',[]))<=official or
                    (data['scope_status'] in {'explicit','within_scope','prerequisite_only'} and not data.get('syllabus_ids'))):
                raise ValueError('缺来源、有效纲目或具体教学边界')
            for ref in draft['source_refs']:
                if not ref.get('locator') or digest(Path(ref['path']).read_bytes())!=ref['sha256']:
                    raise ValueError('本次原件来源变更')
            new=data|{'source_refs':draft['source_refs']}
            old=layer['subpoints'].get(sid)
            if old==new:
                continue
            old_hash=digest(research.raw(old)) if old else None
            if old_hash!=draft.get('expected_previous_version'):
                raise ValueError('此小点教学范围版本冲突：'+sid)
            layer['subpoints'][sid]=new
            changed.append(sid)
        if apply and changed:
            _atomic(dest,research.raw(layer))
        return {'status':('APPLIED' if changed else 'UNCHANGED') if apply else 'VALIDATED','changed':changed,
                'identities':sorted(seen),'learning_writes':0}

def apply_review(repo,draft,apply=False):
    if draft.get('schema')!='cs408-local-quality-draft-v1' or not POINT.fullmatch(draft.get('point','')):
        raise ValueError('需要精确父点与本地修订稿身份')
    if not isinstance(draft.get('review_reason'),str) or not draft['review_reason'].strip() or not draft.get('source_refs'):
        raise ValueError('需本轮真实核源理由与来源')
    for ref in draft['source_refs']:
        if not ref.get('locator') or digest(Path(ref['path']).read_bytes())!=ref.get('sha256'):
            raise ValueError('来源定位或哈希不符')
    if draft['layer']=='profile':
        return profile_patch(repo,draft,apply)
    if draft['layer']=='scope':
        return apply_scopes(repo,[draft],apply)
    if draft['layer']!='route':
        raise ValueError('未知修订层')
    target=draft.get('subpoint_id') or draft['point']
    route=draft['route']
    if route.get('identity')!=target or (target!=draft['point'] and route.get('parent')!=draft['point']):
        raise ValueError('教案身份与父点不符')
    if target!=draft['point']:
        parent=draft['point']
        ref=profiles.current_index(repo)['profiles'].get(parent,{})
        profile=profiles.load_profile_version(repo,parent,ref.get('sha256')) or {}
        if not any(s.get('subpoint_id')==target for s in profile.get('subpoints',[])):
            raise ValueError('小点不属于本次正式父点')
    if (route.get('teacher_release_level')!=5 or route.get('frontend_ready') is not False
        or route.get('effectiveness')!='unverified' or route.get('learning_write_count')!=0
        or route.get('schema')!='cs408-derived-teaching-route-v1' or route.get('policy_version')!=research.POLICY_VERSION):
        raise ValueError('教案语义及教师边界不符')
    blocks=route.get('ordered_blocks')
    if not isinstance(blocks,list) or not blocks or not all(b.get('teacher_text') and b.get('step_id') for b in blocks):
        raise ValueError('需要逐步写成的实际讲解')
    if len({b['step_id'] for b in blocks})!=len(blocks) or not isinstance(route.get('visual_insertions'),list):
        raise ValueError('讲解步骤与交互绑定无效')
    if any(x.get('step_id') not in {b['step_id'] for b in blocks} for x in route['visual_insertions']):
        raise ValueError('图解未绑定确切讲解步骤')
    if route.get('source_refs')!=draft['source_refs']:
        raise ValueError('教案与成稿来源不一致')
    current=read(repo/research.INDEX_REL)['targets'].get(target,{})
    if current.get('sha256') not in {draft['expected_previous_version'],digest(research.raw(route))}:
        raise ValueError('旧路线版本变更，重读该项合并')
    update=dict(identity=target,route=route,expected_previous_sha256=draft['expected_previous_version'])
    if not apply:
        return {'status':'VALIDATED','target':target}
    return research.apply_incremental(repo,[update])

def review_batch(repo,drafts,apply):
    if len(drafts)==1:
        return apply_review(repo,drafts[0],apply)
    if all(d.get('schema')=='cs408-local-quality-draft-v1' and d.get('review_reason') and d.get('source_refs') and d.get('layer')=='scope' for d in drafts):
        return apply_scopes(repo,drafts,apply)
    if not all(d.get('schema')=='cs408-local-quality-draft-v1' and d.get('layer')=='profile'
               and POINT.fullmatch(d.get('point','')) and d.get('review_reason') and d.get('source_refs') for d in drafts):
        raise ValueError('批量写者只接收各自具名的审核后个人概况，是同一层的原子提交')
    points=[d['point'] for d in drafts]
    if len(set(points))!=len(points):
        raise ValueError('同一父点的多项修改应按同一基线合为一稿')
    updates=[]
    all_bindings={}
    formals={}
    for d in drafts:
        update,kwargs=profile_patch(repo,d,False,prepare_only=True)
        updates.append(update)
        all_bindings.update(kwargs['evidence_bindings'])
        formals.update(kwargs['formal_bindings'])
    kwargs={'evidence_bindings':all_bindings,'formal_bindings':formals,'expected_points':points}
    if not apply:
        profiles.validate_updates(repo,updates,check_versions=True,**kwargs)
        return {'status':'VALIDATED','points':points}
    operation_id='local-quality-408:'+digest(research.raw(drafts))
    return profiles.apply_updates(repo,updates,operation_id,**kwargs)

def main():
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--repo',type=Path,default=REPO)
    subs=p.add_subparsers(dest='command',required=True)
    b=subs.add_parser('build')
    b.add_argument('--exclude-year',action='append',type=int,default=[])
    b.add_argument('--study-date',help='仅采用该显式日的年度计划，不推断后续日期')
    a=subs.add_parser('audit')
    a.add_argument('--subject',choices=['DS','CO','OS','CN'])
    c=subs.add_parser('context')
    c.add_argument('--id',required=True)
    c.add_argument('--view',choices=['protected','after_attempt','direct'],default='protected')
    c.add_argument('--current-question')
    c.add_argument('--all-forms',action='store_true')
    c.add_argument('--personal-evidence',action='store_true',help='本机作者direct视图附当前正式概况/小点和原证据指针')
    c.add_argument('--topic-review',action='store_true')
    t=subs.add_parser('render-topic')
    t.add_argument('--id',required=True)
    t.add_argument('--view',choices=['protected','after_attempt','direct'],default='protected')
    t.add_argument('--output',required=True,type=Path)
    t.add_argument('--inline-root',type=Path)
    for target in (c,t):
        target.add_argument('--catalog-version')
        target.add_argument('--case-context',help='本题复盘结构化参数 JSON，- 从 stdin 读取')
        target.add_argument('--withhold-question',action='append',default=[])
        target.add_argument('--exclude-year',type=int,action='append',default=[])
    for cmd in ('check','apply'):
        t=subs.add_parser(cmd)
        t.add_argument('--reviewed',type=Path,required=True,action='append')
    args=p.parse_args()
    repo=args.repo.resolve()
    if args.command=='build':
        years=set(args.exclude_year)
        if args.study_date:
            from datetime import date
            date.fromisoformat(args.study_date)
            plans=read(repo/'.private/exam-year-study/plan.json')['daily_plans']
            policy=plans.get(args.study_date,{})
            if policy.get('exam_year'):
                years.add(int(policy['exam_year']))
            elif not years:
                raise ValueError('该日尚无明确年度，未重建或解除保护')
        if not years:
            years=set(catalog(repo)[0].get('excluded_years',[]))
        result=compile_catalog(repo,years)
    elif args.command=='audit':
        idx,v=catalog(repo)
        ref=idx.get('objects',{})
        limited=[]
        missing_route=[]
        no_exam=[]
        unmapped=[]
        for k in ref:
            if not (POINT.fullmatch(k) or k.startswith('KSP-')):
                continue
            row=blob(repo/ROOT,ref,k)
            if args.subject and not (row.get('parent') or k).startswith(args.subject):
                continue
            if row.get('kind')=='subpoint':
                if row['syllabus'].get('scope_status')=='needs_independent_review':
                    limited.append(k)
                if row['dedicated_route_status']!='available':
                    missing_route.append(k)
            elif not row.get('exam_forms'):
                no_exam.append(k)
            elif not list(select_forms(row)):
                unmapped.append(k)
        result={'catalog_sha256':v,'changed_sources':catalog_drift(repo,idx),'counts':idx.get('counts',{}),
                'topic_review_coverage':idx.get('topic_review_audit',{}),'needs_independent_ksp_scope':limited,
                'needs_ksp_route':missing_route,'no_assessed_exam_units':no_exam,'needs_exam_scope_binding':unmapped,
                'withheld_formal_count':len(idx.get('held_formal_ids',[])),'learning_writes':0}
    elif args.command=='render-topic' or (args.command=='context' and args.topic_review):
        case_input=None
        if args.case_context and args.view=='direct':
            from recap_precision_408 import read_case_input
            case_input=read_case_input(args.case_context)
        result=topic_review(repo,args.id,args.view,expected_version=args.catalog_version,
                            withheld=args.withhold_question,years=args.exclude_year,case_input=case_input)
        if args.command=='render-topic' and result['status']=='available':
            from syllabus_topic_review_408 import render_review
            result.pop('chat_overview')
            receipt=render_review(result.pop('review'),args.output,exact=result.get('exact_identity'),
                                  inline_root=args.inline_root,**result.pop('mechanism_selection'))
            result |= receipt
    elif args.command=='context':
        result=teaching_lookup(repo,args.id,args.view,current_question=args.current_question,all_forms=args.all_forms)
        if args.personal_evidence and args.view=='direct':
            from exam_tutoring_context_408 import query_context, QUERY_INPUT_SCHEMA
            payload={'schema':QUERY_INPUT_SCHEMA,'stage':'post_answer','knowledge_points':[args.id],'max_profiles':0}
            if args.current_question:payload['current_question_id']=args.current_question
            native=query_context(repo,payload,teaching_view='direct')
            result['latest_personal_context']=fields(native,'status reasons source_version version records')
            if native.get('status')=='available':
                result['personal_evidence_usage']='只按返回的实际状态判断是否可用于本轮。需要完整原话时，从记录的版本与证据对象回开。'

    else:result=review_batch(repo,[read(f) for f in args.reviewed],apply=args.command=='apply')
    print(json.dumps(result,ensure_ascii=False,indent=2))

if __name__=='__main__':
    main()
