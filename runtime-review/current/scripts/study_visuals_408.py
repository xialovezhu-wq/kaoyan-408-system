#!/usr/bin/env python3
"""Versioned, stage-separated visual assets. Reads never generate or write study events."""
from __future__ import annotations

import argparse
import copy
import fcntl
import hashlib
import json
import mimetypes
import os
from pathlib import Path
import re
import shutil
import stat
import tempfile
from typing import Any

ROOT_REL = Path('.private/study-visuals-3d')
RUNTIME_REL = Path('support/study-visuals-3d')
QID = re.compile(r'(?:EXAM408-\d{4}-Q\d{2}(?:-P\d{2})?|(?:MATH|EN)-[A-Za-z0-9][A-Za-z0-9_-]{0,100}|DEMO-[A-Z0-9-]{1,60})\Z')
SHA = re.compile(r'[a-f0-9]{64}\Z')
NAME = re.compile(r'[A-Za-z0-9][A-Za-z0-9._-]{0,100}\Z')
STAGES = {'question', 'hint', 'solution'}
BASE_URL = 'http://127.0.0.1:8769/study-visuals'
MAX_JSON = 32 * 1024 * 1024
SCENE_KEYS = {'schema','question_id','stage','hint_level','template_id','template_version','rule_version',
              'title','subtitle','notices','camera','objects','edges','frames','controls','original_images',
              'source_note','stage_label','validation_role'}


class VisualCacheError(ValueError):
    pass


def require(value: Any, reason: str) -> None:
    if not value:
        raise VisualCacheError(reason)


def encoded(value: Any) -> bytes:
    return (json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':'), allow_nan=False)+'\n').encode()


def digest(value: Any) -> str:
    return hashlib.sha256(encoded(value)).hexdigest()


def file_sha(path: Path) -> str:
    require(path.is_file() and not path.is_symlink(), 'file_missing_or_symlink:'+str(path))
    h=hashlib.sha256()
    with path.open('rb') as f:
        for data in iter(lambda:f.read(1024*1024), b''): h.update(data)
    return h.hexdigest()


def read_json(path: Path) -> dict:
    require(path.is_file() and not path.is_symlink(), 'json_missing_or_symlink:'+str(path))
    require(path.stat().st_size <= MAX_JSON, 'json_too_large')
    value=json.loads(path.read_bytes())
    require(isinstance(value, dict), 'json_object_required')
    return value


def _safe_root(repo: str | Path) -> Path:
    return Path(repo).expanduser().resolve()/ROOT_REL


def _identity(question_id: str, stage: str, hint_level: int=1) -> tuple[str,str,int]:
    require(isinstance(question_id,str) and QID.fullmatch(question_id), 'invalid_question_identity')
    require(stage in STAGES, 'invalid_stage')
    level=int(hint_level) if stage=='hint' else 0
    require(stage!='hint' or 1<=level<=4, 'invalid_hint_level')
    return question_id,stage,level


def _stage_name(stage: str, level: int=0) -> str:
    return 'hint-'+str(level) if stage=='hint' else stage


def lesson_revision(pointer:dict) -> str:
    return digest({'schema':'study-visual-lesson-revision-v1','question_id':pointer['question_id'],
                   'source_content_sha256':pointer['source_content_sha256'],'runtime_sha256':pointer['runtime_sha256'],'stages':pointer['stages']})


def _inside(root: Path, relative: str) -> Path:
    require(isinstance(relative,str) and relative and not Path(relative).is_absolute(), 'asset_relative_path_required')
    require('\\' not in relative and all(x not in ('','.','..') for x in relative.split('/')), 'asset_path_unsafe')
    path=root/relative
    for part in (path,*path.parents):
        if part==root.parent: break
        require(not part.is_symlink(), 'asset_symlink_forbidden')
    path.resolve().relative_to(root.resolve())
    return path


def atomic_json(path: Path, value: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    require(not any(p.is_symlink() for p in (path,*path.parents)), 'output_symlink_forbidden')
    fd,tmp=tempfile.mkstemp(prefix='.visual-',dir=path.parent)
    try:
        with os.fdopen(fd,'wb') as f: f.write(encoded(value));f.flush();os.fsync(f.fileno())
        os.replace(tmp,path)
    finally:
        if os.path.exists(tmp): os.unlink(tmp)


def runtime_identity(repo: str | Path) -> dict:
    repo=Path(repo).resolve(); base=repo/RUNTIME_REL
    required=['web/index.html','web/player.js','web/player.css','vendor/three.module.js',
              'vendor/three.core.js','vendor/OrbitControls.js','vendor/CSS2DRenderer.js',
              'models.mjs','model-scenes.mjs','execution-models.mjs','package-lock.json']
    files=[]
    for name in required:
        path=base/name
        require(path.is_file(), 'runtime_not_ready:'+name)
        files.append({'path':name,'sha256':file_sha(path)})
    return {'schema':'study-visual-runtime-v1','files':files,'sha256':digest(files)}


def validate_scene(scene: dict, question_id: str|None=None, stage: str|None=None) -> None:
    require(isinstance(scene,dict) and scene.get('schema')=='study-visual-scene-v1', 'scene_schema_invalid')
    require(set(scene)<=SCENE_KEYS, 'scene_unknown_fields:'+','.join(sorted(set(scene)-SCENE_KEYS)))
    qid,phase,level=_identity(scene.get('question_id'),scene.get('stage'),scene.get('hint_level',1))
    require(question_id in (None,qid) and stage in (None,phase), 'scene_identity_or_stage_drift')
    require(scene.get('hint_level',0)==level, 'scene_hint_level_drift')
    for k in ['template_id','template_version','rule_version','title']:
        require(isinstance(scene.get(k),str) and scene[k].strip(), 'scene_string_required:'+k)
    require(isinstance(scene.get('notices'),list) and scene['notices'], 'model_boundary_notice_required')
    objects=scene.get('objects');edges=scene.get('edges',[]);frames=scene.get('frames')
    require(isinstance(objects,list) and 0<len(objects)<=1024, 'scene_objects_required')
    require(isinstance(edges,list) and len(edges)<=2048, 'scene_edges_invalid')
    require(isinstance(frames,list) and 0<len(frames)<=512, 'scene_frames_required')
    controls=scene.get('controls') or {}
    require(isinstance(controls,dict),'scene_controls_invalid')
    if controls.get('kind')=='execution':
        require(phase=='solution','execution_requires_solution_stage')
        require(isinstance(controls.get('model'),dict),'execution_model_required')
        require(isinstance(controls.get('actions'),list),'execution_actions_required')
    require(phase!='question' or len(frames)==1 or all(isinstance(f,dict) and f.get('origin')=='given' for f in frames),'question_multiframe_requires_given_source_origin')
    ids=set();edgeids=set()
    for obj in objects:
        require(isinstance(obj,dict) and isinstance(obj.get('id'),str) and obj['id'] not in ids,'object_id_duplicate_or_missing')
        ids.add(obj['id'])
        require(obj.get('kind') in {'box','sphere','cylinder','plane','point','mesh'},'object_kind_invalid')
        for k in ['position','size']:
            v=obj.get(k,[1,1,1] if k=='size' else None)
            require(isinstance(v,list) and len(v)==3 and all(isinstance(x,(int,float)) for x in v),'object_vector_invalid:'+k)
        require(isinstance(obj.get('label'),str),'object_label_required')
        if obj['kind']=='mesh':
            vs=obj.get('vertices');fs=obj.get('faces')
            require(isinstance(vs,list) and len(vs)>=3 and all(isinstance(v,list) and len(v)==3 for v in vs),'mesh_vertices_invalid')
            require(isinstance(fs,list) and fs and all(len(f)==3 and all(isinstance(i,int) and 0<=i<len(vs) for i in f) for f in fs),'mesh_faces_invalid')
    for edge in edges:
        require(isinstance(edge,dict) and isinstance(edge.get('id'),str) and edge['id'] not in edgeids,'edge_identity_invalid')
        edgeids.add(edge['id']);require(edge.get('from') in ids and edge.get('to') in ids,'edge_endpoint_missing')
    for frame in frames:
        require(isinstance(frame,dict) and isinstance(frame.get('title'),str) and isinstance(frame.get('explanation'),str),'frame_text_required')
        require(isinstance(frame.get('objects',{}),dict) and set(frame.get('objects',{}))<=ids,'frame_unknown_object')
        require(isinstance(frame.get('edges',{}),dict) and set(frame.get('edges',{}))<=edgeids,'frame_unknown_edge')
        require(isinstance(frame.get('metrics',[]),list),'frame_metrics_invalid')
        for m in frame.get('metrics',[]): require(isinstance(m,dict) and isinstance(m.get('label'),str) and isinstance(m.get('value'),(str,int,float)),'metric_invalid')
    # JSON encoder rejects NaN/Infinity; executable or remote resources are not scene fields.
    require(len(encoded(scene))<=16*1024*1024,'scene_too_large')
    for image in scene.get('original_images',[]):
        require(isinstance(image,dict) and isinstance(image.get('url'),str) and image['url'].startswith('/study-visuals/asset/'),'original_image_route_invalid')


def _guard_source(pointer: dict, repo: str | Path | None=None) -> None:
    for ref in pointer.get('source_files',[]):
        path=Path(ref['path'])
        require(path.is_absolute() and file_sha(path)==ref['sha256'],'source_file_changed:'+str(path))
    if repo is not None:
        root=Path(repo).resolve()
        for ref in pointer.get('native_catalog_bindings',[]):
            index=read_json(root/ref['index_path'])
            current=index.get(ref['collection'],{}).get(pointer['question_id']) or {}
            require(current.get('sha256')==ref['sha256'],'exact_native_template_changed')


def _pointer(repo: str|Path, qid: str) -> dict:
    root=_safe_root(repo);p=_inside(root,'current/'+qid+'.json');value=read_json(p)
    require(value.get('schema')=='study-visual-current-v1' and value.get('question_id')==qid,'current_pointer_invalid')
    return value


def _validated_stage(repo,question_id,stage='question',hint_level=1,expected_surface_sha256=None):
    qid,stage,level=_identity(question_id,stage,hint_level);root=_safe_root(repo);pointer=_pointer(repo,qid)
    _guard_source(pointer,repo)
    require(pointer.get('runtime_sha256')==runtime_identity(repo)['sha256'],'runtime_or_rule_changed')
    if expected_surface_sha256 is not None:
        require(expected_surface_sha256 in pointer.get('accepted_surface_sha256',[]),'surface_version_mismatch')
    require(pointer.get('status')=='available','template_unsupported')
    key=pointer.get('stages',{}).get(_stage_name(stage,level))
    require(isinstance(key,str) and SHA.fullmatch(key),'stage_not_prepared')
    version=_inside(root,'versions/'+key);m=read_json(version/'manifest.json')
    require(m.get('schema')=='study-visual-stage-manifest-v1' and m.get('cache_key')==key,'stage_manifest_invalid')
    require(m.get('question_id')==qid and m.get('stage')==stage and m.get('hint_level')==level,'stage_manifest_scope_drift')
    require(m.get('verification',{}).get('status')=='verified','stage_not_verified')
    require(digest(m['key_material'])==key,'cache_key_drift')
    files={r['name']:r for r in m['files']}
    require('scene.json' in files and 'preview.png' in files,'stage_assets_incomplete')
    for name,row in files.items():
        require(NAME.fullmatch(name),'registered_asset_name_invalid')
        path=_inside(version,name)
        require(path.is_file() and path.stat().st_size==row['bytes'] and file_sha(path)==row['sha256'],'asset_hash_mismatch:'+name)
    return pointer,m,version,files


def get_visual_pointer(repo,question_id,*,stage='question',hint_level=1,expected_surface_sha256=None) -> dict:
    """Bounded read only: exact ID, source guards and stage; no parent fallback."""
    try:
        qid,stage,level=_identity(question_id,stage,hint_level)
        root=_safe_root(repo)
        if not (root/'current'/f'{qid}.json').is_file():
            return {'status':'out_of_scope','question_id':qid,'stage':stage,'reason':'本题未进入本轮三维缓存。'}
        pointer=_pointer(repo,qid)
        if pointer.get('status')=='unsupported':
            _guard_source(pointer,repo)
            return {'status':'unsupported','question_id':qid,'stage':stage,'reason':'本题暂未匹配经核验的三维模板，原题与原解析保持可用。'}
        p,m,v,_=_validated_stage(repo,qid,stage,level,expected_surface_sha256)
        revision=lesson_revision(p);query=f'?stage={stage}&hint_level={level}&key={m["cache_key"]}&revision={revision}'
        return {'status':'available','question_id':qid,'stage':stage,'hint_level':level,'cache_key':m['cache_key'],
                'lesson_revision':revision,'viewer_url':BASE_URL+'/'+qid+'?revision='+revision,'preview_url':BASE_URL+'/preview/'+qid+query,
                'preview_path':str(v/'preview.png'),'source_version':p.get('source_version'),
                'source_content_sha256':p['source_content_sha256'],'template_id':m['template_id'],
                'available_stages':sorted(p['stages']),'generation_on_read':False,
                'stage_policy':'默认题面；提示及解析必须显式请求，不由after_attempt自动开放。'}
    except (OSError,ValueError,KeyError,TypeError) as exc:
        reason=str(exc)
        return {'status':'missing' if 'stage_not_prepared' in reason else 'stale',
                'question_id':question_id,'stage':stage,'reason':'暂无匹配的已验证图形；原题仍可使用。','diagnostic':reason,'generation_on_read':False}


def read_scene(repo,question_id,stage='question',hint_level=1):
    _,m,path,_=_validated_stage(repo,question_id,stage,hint_level)
    scene=read_json(path/'scene.json');validate_scene(scene,question_id,stage)
    return scene


def read_asset(repo,question_id,stage,filename,hint_level=1):
    require(isinstance(filename,str) and NAME.fullmatch(filename),'asset_filename_invalid')
    _,_,path,files=_validated_stage(repo,question_id,stage,hint_level)
    require(filename in files and filename.endswith(('.png','.webp','.jpg','.jpeg')),'asset_not_public_image')
    return (path/filename).read_bytes(),mimetypes.guess_type(filename)[0] or 'application/octet-stream'


def list_scope(repo):
    root=_safe_root(repo);path=root/'scope.json'
    if not path.exists():return {'schema':'study-visual-safe-index-v1','items':[],'status':'not_prepared'}
    source=read_json(path);items=[]
    for row in source['items']:
        qid=row['question_id'];_identity(qid,'question');p=root/'current'/f'{qid}.json'
        pointer=read_json(p) if p.exists() else {}
        # List only declared cache metadata; each actual open performs exact guards.
        revision=lesson_revision(pointer) if pointer.get('status')=='available' else None
        items.append({'question_id':qid,'group':row['group'],'group_label':row.get('group_label',row['group']),'title':row['title'],
                      'status':pointer.get('status','missing'),'viewer_url':BASE_URL+'/'+qid,
                      'preview_url':BASE_URL+'/preview/'+qid+'?stage=question',
                      'lesson_revision':revision,
                      'available_stages':sorted(pointer.get('stages',{})), 'auxiliary':row.get('auxiliary',False)})
    return {'schema':'study-visual-safe-index-v1','scope_id':source['scope_id'],'items':items,'generation_on_read':False}


def register_scope(repo,source_scope:dict):
    """Explicit generator setup; imports only the named allowlist, never new study work."""
    rows=[];seen=set();target_date='已冻结'
    handoff_ref=source_scope.get('frozen_morning_handoff') or {}
    if handoff_ref.get('path'):
        hp=Path(handoff_ref['path']);require(file_sha(hp)==handoff_ref['sha256'],'scope_handoff_changed')
        target_date=read_json(hp).get('target_date','已冻结')
    for group,entries in [('primary',source_scope.get('primary',[])),('auxiliary',source_scope.get('independent_subquestions',source_scope.get('auxiliary_2010_subquestions',[]))),('validation',source_scope.get('validation',[]))]:
        for row in entries:
            qid=row['identity'];_identity(qid,'question');require(qid not in seen,'scope_duplicate');seen.add(qid)
            match=re.fullmatch(r'EXAM408-(\d{4})-Q(\d{2})(?:-P(\d{2}))?',qid)
            label=(f'{match[1]}年第{int(match[2])}题'+(f' · P{match[3]}' if match[3] else '')) if match else qid
            group_label='模板验收示例' if group=='validation' else '当前数学讲解' if qid.startswith('MATH-') else '当前英语讲解' if qid.startswith('EN-') else '2010 独立小问' if group=='auxiliary' else '2010 真题' if qid.startswith('EXAM408-2010-') else str(target_date)+' 晨间复盘'
            rows.append({'question_id':qid,'group':row['group'],'group_label':row.get('group_label',group_label),'title':row.get('safe_title',label) if group=='validation' else label,'auxiliary':group!='primary'})
    if source_scope.get('append_existing_scope') is True:
        existing_path=_safe_root(repo)/'scope.json'
        if existing_path.exists():
            existing=read_json(existing_path)
            merged={r['question_id']:r for r in existing.get('items',[])}
            merged.update({r['question_id']:r for r in rows})
            rows=list(merged.values())
    scope={'schema':'study-visual-generation-scope-v1','scope_id':digest(rows),'items':rows,'learning_write_count':0}
    atomic_json(_safe_root(repo)/'scope.json',scope);return scope


def _source_files(row,source_record=None):
    out=[]
    for key in ['canonical_card','native_payload']:
        ref=row.get(key)
        if ref and ref.get('path') and ref.get('exists',True):out.append({'path':ref['path'],'sha256':ref['sha256']})
    asset_source=source_record or row
    image_keys=['canonical_question_assets','solution_assets'] if source_record else ['question_assets','solution_assets']
    for key in image_keys:
        for ref in asset_source.get(key,[]):
            if ref.get('path') and ref.get('exists',True):out.append({'path':ref['path'],'sha256':ref['sha256']})
    for stem in ['question_template','source_plan']:
        if row.get(stem+'_path'):out.append({'path':row[stem+'_path'],'sha256':row[stem+'_sha256']})
    # Only the unique local source bytes required by this exact question.
    return list({r['path']:r for r in out}.values())


def _verified_morning_surface(repo:Path,source_row:dict,source_record:dict):
    frozen=source_row.get('frozen_morning')
    if not frozen:return None,[]
    problems=[r for r in source_record.get('source_files',[]) if r.get('role')=='frozen_morning_problem']
    require(problems,'frozen_problem_binding_required')
    problem=Path(problems[0]['path'])
    require(problem.parent.parent.name=='questions' and problem.parent.parent.parent.name=='assembled-output-v1','frozen_problem_layout_invalid')
    handoff_path=problem.parents[3]/'handoff.json'
    require(file_sha(handoff_path)==frozen['handoff_sha256'],'frozen_handoff_hash_drift')
    handoff=read_json(handoff_path);native=Path(handoff['native_import_receipt'])
    require(native.parent.resolve()==(repo/'.private/study-bridge/d-imports').resolve(),'native_import_outside_owner')
    require(file_sha(native)==handoff['native_import_sha256'],'native_import_hash_drift')
    plan=read_json(native);matches=[r for r in plan['learning_items'] if r.get('request_id')==frozen['request_id']]
    require(len(matches)==1,'exact_frozen_request_missing')
    row=matches[0];selection=row.get('selection') or row
    require(selection.get('true_item_id')==source_row['identity'],'frozen_surface_identity_mismatch')
    surface=selection.get('surface_sha256')
    require(isinstance(surface,str) and SHA.fullmatch(surface),'frozen_surface_hash_missing')
    return surface,[{'path':str(handoff_path),'sha256':frozen['handoff_sha256']},{'path':str(native),'sha256':handoff['native_import_sha256']}]


def prepare_spec(repo,spec:dict,source_row:dict,*,task_id:str,render_environment:dict) -> dict:
    qid=spec['question_id'];_identity(qid,'question')
    require(spec.get('schema')=='study-visual-authored-v1' and source_row['identity']==qid,'authored_scope_drift')
    source_path=Path(source_row['source_record_path'])
    require(file_sha(source_path)==source_row['source_record_sha256']==spec['source_record_sha256'],'authored_source_snapshot_drift')
    require(isinstance(task_id,str) and NAME.fullmatch(task_id),'task_identity_invalid')
    root=_safe_root(repo);scope=read_json(root/'scope.json');require(qid in {x['question_id'] for x in scope['items']},'outside_registered_scope')
    source_record=read_json(source_path);refs=_source_files(source_row,source_record)
    morning_surface,provenance=_verified_morning_surface(Path(repo).resolve(),source_row,source_record)
    for ref in source_record.get('source_files',[]):
        if ref.get('role') in {'frozen_morning_problem','frozen_private_solution_path','native_exact_subquestion_payload'}:
            destination=refs if ref['role']=='native_exact_subquestion_payload' else provenance
            destination.append({'path':ref['path'],'sha256':ref['sha256']})
    refs=list({r['path']:r for r in refs}.values())
    catalog=[]
    if source_row.get('question_template_sha256'):
        catalog.append({'index_path':'.private/question-visual-408/current.json','collection':'templates','sha256':source_row['question_template_sha256']})
    if source_row.get('source_plan_sha256'):
        catalog.append({'index_path':'.private/source-visual-408/current.json','collection':'question_plans','sha256':source_row['source_plan_sha256']})
    source_version=source_record.get('source_version') or ('exact-template:'+source_row['question_template_sha256'] if source_row.get('question_template_sha256') else 'content:'+source_row['question_text_sha256'])
    selection_version=(source_record.get('frozen_morning') or {}).get('selection_source_commit')
    if selection_version:source_version='frozen:'+selection_version+';'+source_version
    pointer={'schema':'study-visual-current-v1','question_id':qid,
        'source_content_sha256':source_row['question_text_sha256'],'source_version':source_version,
        'author_source_record':{'path':str(source_path),'sha256':source_row['source_record_sha256'],'teacher_private':True},
        'accepted_surface_sha256':sorted({str(x) for x in [source_row.get('surface_sha256'),morning_surface] if x}),
        'source_files':refs,'generation_provenance_files':provenance,'native_catalog_bindings':catalog,'runtime_sha256':runtime_identity(repo)['sha256'],'scope_id':scope['scope_id'],'stages':{}}
    _guard_source(pointer,repo)
    draft=root/'.staging'/task_id/qid
    if spec['status']=='unsupported':
        draft.mkdir(parents=True,exist_ok=True);pointer.update(status='unsupported',private_reason=spec.get('reason','template_not_supported'))
        if (draft/'publication.json').exists():require(read_json(draft/'publication.json').get('pointer')==pointer,'task_id_reused_with_different_inputs')
        current_path=root/'current'/f'{qid}.json'
        atomic_json(draft/'publication.json',{'pointer':pointer,'stages':[],'task_id':task_id,'expected_current_sha256':file_sha(current_path) if current_path.exists() else None})
        return {'status':'UNSUPPORTED_DRAFT','question_id':qid,'draft_path':str(draft),'reason':spec.get('reason')}
    require(spec['status']=='supported','authored_status_invalid')
    require(isinstance(spec.get('verification'),dict) and spec['verification'].get('kind'),'authored_verification_required')
    rule_sources=[]
    for ref in spec.get('rule_source_files',[]):
        p=Path(ref['path']);require(file_sha(p)==ref['sha256'],'rule_source_changed_before_generation')
        rule_sources.append({'name':p.name,'sha256':ref['sha256']})
    scenes=spec.get('scenes') or {};require(scenes.get('question') is not None,'question_scene_required')
    staged=[];planned=[]
    for stage in ['question','hint','solution']:
        original=scenes.get(stage)
        if original is None:continue
        scene=copy.deepcopy(original);scene.pop('original_images',None);validate_scene(scene,qid,stage)
        level=scene['hint_level'];name=_stage_name(stage,level)
        material={'question_id':qid,'stage':stage,'hint_level':level,'source_content_sha256':pointer['source_content_sha256'],'author_source_record_sha256':source_row['source_record_sha256'],
                  'source_files':refs,'generation_provenance_files':provenance,'accepted_surface_sha256':pointer['accepted_surface_sha256'],
                  'scene_spec_sha256':digest(scene),'template_id':scene['template_id'],'template_version':scene['template_version'],
                  'rule_version':scene['rule_version'],'rule_source_files':rule_sources,'runtime_sha256':pointer['runtime_sha256'],'render_environment':render_environment}
        key=digest(material);pointer['stages'][name]=key;planned.append((stage,level,name,scene,material,key))
    try:
        previous=_pointer(repo,qid)
        if previous.get('stages')==pointer['stages'] and previous.get('source_content_sha256')==pointer['source_content_sha256']:
            for stage,level,*_ in planned:_validated_stage(repo,qid,stage,level)
            return {'status':'CACHE_HIT','question_id':qid,'stages':pointer['stages'],'renderer_calls':0,'learning_write_count':0}
    except (OSError,ValueError,KeyError):
        pass
    existing_publication=draft/'publication.json'
    if existing_publication.exists():
        require(read_json(existing_publication).get('pointer',{}).get('stages')==pointer['stages'],'task_id_reused_with_different_inputs')
    draft.mkdir(parents=True,exist_ok=True)
    for stage,level,name,scene,material,key in planned:
        version=draft/name;version.mkdir(exist_ok=True)
        images=[]
        # Original question images are safe supplemental assets. Pxx solutions
        # are never automatically copied from parent reference capsules.
        for index,asset in enumerate(source_row.get('question_assets',[]),1):
            path=Path(asset['path']);require(file_sha(path)==asset['sha256'],'question_image_changed')
            suffix=path.suffix.lower();require(suffix in {'.png','.jpg','.jpeg','.webp'},'question_image_type')
            target=f'original-question-{index:02d}'+suffix;shutil.copyfile(path,version/target)
            images.append({'label':f'原题图 {index}','url':f'/study-visuals/asset/{qid}/{target}?stage={stage}&hint_level={level}&key={key}&revision={lesson_revision(pointer)}'})
        scene['original_images']=images
        atomic_json(version/'scene.json',scene)
        manifest={'schema':'study-visual-stage-manifest-v1','question_id':qid,'stage':stage,'hint_level':level,'cache_key':key,
                  'template_id':scene['template_id'],'key_material':material,'verification':{'status':'pending_render','authored':spec['verification']},'files':[]}
        atomic_json(version/'manifest.json',manifest);staged.append({'name':name,'cache_key':key,'path':str(version)})
    pointer['status']='available';current_path=root/'current'/f'{qid}.json'
    atomic_json(draft/'publication.json',{'pointer':pointer,'stages':staged,'task_id':task_id,'expected_current_sha256':file_sha(current_path) if current_path.exists() else None})
    return {'status':'STAGED_NOT_PUBLISHED','question_id':qid,'draft_path':str(draft),'stages':staged}


def seal_stage(stage_path:Path,render_receipt:dict):
    """Called only by an explicit screenshot/QA producer, not by HTTP reads."""
    stage_path=Path(stage_path);m=read_json(stage_path/'manifest.json');scene=read_json(stage_path/'scene.json')
    validate_scene(scene,m['question_id'],m['stage'])
    require(digest({k:v for k,v in scene.items() if k!='original_images'})==m['key_material']['scene_spec_sha256'],'rendered_scene_input_drift')
    require(render_receipt.get('status')=='verified' and render_receipt.get('cache_key')==m['cache_key'],'render_receipt_not_verified')
    require(render_receipt.get('scene_sha256')==file_sha(stage_path/'scene.json'),'render_scene_hash_drift')
    require((stage_path/'preview.png').is_file() and (stage_path/'preview.png').stat().st_size>1000,'render_preview_missing')
    require(render_receipt.get('preview_sha256')==file_sha(stage_path/'preview.png'),'render_preview_hash_drift')
    from PIL import Image
    with Image.open(stage_path/'preview.png') as preview:
        require(preview.format=='PNG' and preview.width>0 and preview.height>0,'render_preview_not_png')
        preview.verify()
    files=[]
    for p in sorted(stage_path.iterdir()):
        if p.name=='manifest.json':continue
        require(p.is_file() and not p.is_symlink() and NAME.fullmatch(p.name),'stage_member_invalid')
        files.append({'name':p.name,'sha256':file_sha(p),'bytes':p.stat().st_size})
    m['files']=files;m['verification']={**m['verification'],'status':'verified','render_receipt':render_receipt}
    atomic_json(stage_path/'manifest.json',m);return m


def publish_draft(repo,draft_path,*,simulate_failure=False):
    root=_safe_root(repo);draft=Path(draft_path).resolve();draft.relative_to((root/'.staging').resolve())
    publication=read_json(draft/'publication.json');qid=publication['pointer']['question_id'];_identity(qid,'question')
    lock=_inside(root,'locks/'+qid+'.lock');lock.parent.mkdir(parents=True,exist_ok=True)
    with lock.open('a+b') as stream:
        fcntl.flock(stream,fcntl.LOCK_EX)
        return _publish_draft_locked(repo,draft,simulate_failure=simulate_failure)


def _publish_draft_locked(repo,draft_path,*,simulate_failure=False):
    root=_safe_root(repo);draft=Path(draft_path).resolve();draft.relative_to((root/'.staging').resolve())
    publication=read_json(draft/'publication.json');pointer=publication['pointer'];qid=pointer['question_id'];_identity(qid,'question')
    _guard_source(pointer,repo);require(pointer['runtime_sha256']==runtime_identity(repo)['sha256'],'runtime_changed_during_generation')
    for ref in pointer.get('generation_provenance_files',[]):
        require(file_sha(Path(ref['path']))==ref['sha256'],'generation_provenance_changed_before_publish')
    rows=publication.get('stages');expected=pointer.get('stages')
    require(isinstance(rows,list) and isinstance(expected,dict),'publication_stages_invalid')
    valid_names={'question','solution','hint-1','hint-2','hint-3','hint-4'}
    require(set(expected)<=valid_names and all(isinstance(k,str) and SHA.fullmatch(k) for k in expected.values()),'publication_stage_name_or_key_invalid')
    require(all(isinstance(row,dict) and row.get('name') in valid_names for row in rows),'publication_stage_row_invalid')
    require(len(rows)==len({row['name'] for row in rows}) and {row['name']:row.get('cache_key') for row in rows}==expected,'publication_stage_set_not_closed')
    require(len(expected.values())==len(set(expected.values())),'publication_duplicate_stage_key')
    require((pointer.get('status')=='available' and 'question' in expected) or (pointer.get('status')=='unsupported' and not expected),'publication_status_stage_mismatch')
    current_path=root/'current'/f'{qid}.json'
    current_sha=file_sha(current_path) if current_path.exists() else None
    require(current_sha==publication.get('expected_current_sha256') or (current_path.exists() and read_json(current_path)==pointer),'current_pointer_changed_since_preparation')
    for row in rows:
        source=draft/row['name'];m=read_json(source/'manifest.json')
        phase,level=('hint',int(row['name'].split('-')[1])) if row['name'].startswith('hint-') else (row['name'],0)
        require(m.get('schema')=='study-visual-stage-manifest-v1' and m.get('question_id')==qid and m.get('stage')==phase and m.get('hint_level')==level,'publication_stage_identity_drift')
        require(m['cache_key']==row['cache_key'] and digest(m['key_material'])==row['cache_key'],'stage_key_tampered')
        scene=read_json(source/'scene.json');validate_scene(scene,qid,phase)
        require(digest({k:v for k,v in scene.items() if k!='original_images'})==m['key_material']['scene_spec_sha256'],'publication_scene_input_drift')
        require(m['verification']['status']=='verified','unverified_stage_cannot_publish')
        for f in m['files']:
            require(file_sha(_inside(source,f['name']))==f['sha256'],'stage_bytes_changed_before_publish')
        require({'scene.json','preview.png'}<={f['name'] for f in m['files']},'stage_incomplete')
    require(not simulate_failure,'simulated_failure_before_publish')
    for row in rows:
        source=draft/row['name'];target=root/'versions'/row['cache_key'];target.parent.mkdir(parents=True,exist_ok=True)
        if target.exists():
            require(read_json(target/'manifest.json')==read_json(source/'manifest.json'),'immutable_cache_version_conflict')
            for f in read_json(target/'manifest.json')['files']:
                require(file_sha(_inside(target,f['name']))==f['sha256'],'existing_immutable_version_corrupt')
        else:os.rename(source,target)
    atomic_json(root/'current'/f'{qid}.json',pointer)
    return {'status':'PUBLISHED' if pointer['status']=='available' else 'UNSUPPORTED_REGISTERED','question_id':qid,'stages':pointer['stages'],'learning_write_count':0}


def main():
    parser=argparse.ArgumentParser(description=__doc__);parser.add_argument('--repo',default=str(Path(__file__).resolve().parents[1]));sub=parser.add_subparsers(dest='command',required=True)
    sub.add_parser('index');r=sub.add_parser('resolve');r.add_argument('--id',required=True);r.add_argument('--stage',choices=sorted(STAGES),default='question');r.add_argument('--hint-level',type=int,default=1)
    s=sub.add_parser('register-scope');s.add_argument('--scope',required=True)
    p=sub.add_parser('publish');p.add_argument('--draft',required=True);p.add_argument('--simulate-failure',action='store_true')
    args=parser.parse_args()
    try:
        if args.command=='index':result=list_scope(args.repo)
        elif args.command=='resolve':result=get_visual_pointer(args.repo,args.id,stage=args.stage,hint_level=args.hint_level)
        elif args.command=='register-scope':result=register_scope(args.repo,read_json(Path(args.scope)))
        else:result=publish_draft(args.repo,args.draft,simulate_failure=args.simulate_failure)
        print(json.dumps(result,ensure_ascii=False,indent=2));return 0
    except (OSError,ValueError,KeyError) as exc:
        print(json.dumps({'status':'ERROR','reason':str(exc),'learning_write_count':0},ensure_ascii=False));return 2


if __name__=='__main__':raise SystemExit(main())
