"""Versioned, stage-separated object teaching cards for the current 3D lesson.

The HTTP reader returns exactly one already-authorized stage. No source search,
generation, learning event, model request or parent-question fallback on click.
"""
from __future__ import annotations
import copy
from pathlib import Path
import study_visuals_408 as cache

ROOT=cache.ROOT_REL/'interactions'
PUBLIC_FIELDS={'title','role','facts','points','cautions','scope_note','related','frame_notes'}

def validate_card(card,scene):
    cache.require(isinstance(card,dict) and set(card)<=PUBLIC_FIELDS,'interaction_card_fields_invalid')
    for k in ['title','role']:
        cache.require(isinstance(card.get(k),str) and 0<len(card[k].strip())<=700,'interaction_text_required:'+k)
    for k in ['facts','cautions']:
        cache.require(isinstance(card.get(k,[]),list) and all(isinstance(x,str) and 0<len(x)<=1200 for x in card.get(k,[])),'interaction_list_invalid:'+k)
    cache.require(isinstance(card.get('points',[]),list),'interaction_points_invalid')
    for p in card.get('points',[]):cache.require(set(p)=={'code','title'} and all(isinstance(v,str) and v for v in p.values()),'interaction_point_invalid')
    objects={o['id'] for o in scene['objects']};edges={e['id'] for e in scene['edges']}
    for item in card.get('related',[]):cache.require(set(item)=={'id','kind','label'} and item['kind'] in {'object','edge'} and item['id'] in (objects if item['kind']=='object' else edges),'interaction_relation_drift')
    notes=card.get('frame_notes',{})
    cache.require(isinstance(notes,dict) and all(str(i) in {str(n) for n in range(len(scene['frames']))} and isinstance(text,str) and text for i,text in notes.items()),'interaction_frame_note_invalid')

def publish(repo,payload):
    repo=Path(repo).resolve();qid=payload['question_id'];cache._identity(qid,'question')
    pointer=cache._pointer(repo,qid);revision=cache.lesson_revision(pointer)
    cache.require(payload.get('schema')=='study-visual-interaction-set-v1' and payload.get('lesson_revision')==revision,'interaction_lesson_drift')
    cache.require(set(payload['stages'])==set(pointer['stages']),'interaction_stage_set_not_closed')
    for name,data in payload['stages'].items():
        stage='hint' if name.startswith('hint-') else name;level=int(name.split('-')[1]) if stage=='hint' else 0
        scene=cache.read_scene(repo,qid,stage,level)
        cache.require(set(data)=={'objects','edges'},'interaction_stage_fields_invalid')
        cache.require(set(data['objects'])=={x['id'] for x in scene['objects']},'interaction_object_coverage_incomplete')
        cache.require(set(data['edges'])=={x['id'] for x in scene['edges']},'interaction_edge_coverage_incomplete')
        for card in list(data['objects'].values())+list(data['edges'].values()):validate_card(card,scene)
    for ref in payload.get('source_files',[]):cache.require(cache.file_sha(Path(ref['path']))==ref['sha256'],'interaction_source_drift')
    key=cache.digest(payload);root=repo/ROOT;target=root/'versions'/f'{key}.json'
    if not target.exists():cache.atomic_json(target,payload)
    cache.atomic_json(root/'current'/f'{qid}.json',{'schema':'study-visual-interaction-pointer-v1','question_id':qid,'lesson_revision':revision,'key':key})
    return {'question_id':qid,'status':'PUBLISHED','key':key,'learning_write_count':0}

def read(repo,qid,stage='question',hint_level=1,*,expected_revision=None):
    repo=Path(repo).resolve();qid,stage,level=cache._identity(qid,stage,hint_level)
    pointer=cache._pointer(repo,qid);cache._guard_source(pointer,repo);revision=cache.lesson_revision(pointer)
    cache.require(expected_revision in {None,revision},'interaction_requested_revision_stale')
    root=repo/ROOT;p=cache.read_json(root/'current'/f'{qid}.json')
    cache.require(p.get('question_id')==qid and p.get('lesson_revision')==revision and cache.SHA.fullmatch(p.get('key','')),'interaction_pointer_stale')
    data=cache.read_json(root/'versions'/f'{p["key"]}.json');cache.require(cache.digest(data)==p['key'],'interaction_payload_hash_mismatch')
    cache.require(data['question_id']==qid and data['lesson_revision']==revision,'interaction_identity_drift')
    # Cold authoring evidence was verified at publication. Daily clicks rely on
    # this immutable local snapshot and the current lesson's hot-source guards;
    # they must not depend on an attached archive drive.
    name='hint-'+str(level) if stage=='hint' else stage
    cache.require(name in data['stages'],'interaction_stage_missing')
    # Source files, teacher metadata and every other stage stay server-side.
    result={'schema':'study-visual-interactions-v1','question_id':qid,'stage':stage,'hint_level':level,'lesson_revision':revision,'interaction_revision':p['key'],**copy.deepcopy(data['stages'][name])}
    cache.require(cache.read_json(root/'current'/f'{qid}.json')==p,'interaction_changed_during_read')
    return result

def teacher_corrections(repo,qid):
    """Exact-question derived corrections; caller must enforce teacher-view scope."""
    repo=Path(repo).resolve();cache._identity(qid,'question');lesson=cache._pointer(repo,qid);cache._guard_source(lesson,repo)
    p=cache.read_json(repo/ROOT/'current'/f'{qid}.json');cache.require(p['lesson_revision']==cache.lesson_revision(lesson),'correction_lesson_stale')
    data=cache.read_json(repo/ROOT/'versions'/f'{p["key"]}.json');cache.require(cache.digest(data)==p['key'],'correction_payload_hash_mismatch')
    return copy.deepcopy(data.get('review',{}).get('applied_source_corrections',[]))
