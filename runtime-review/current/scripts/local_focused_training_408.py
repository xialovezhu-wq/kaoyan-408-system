#!/usr/bin/env python3
"""Prepare an independent, evidence-scoped local true-exam session. No D state or learner writes."""
import argparse
from contextlib import contextmanager
import csv
import datetime as dt
import hashlib
import io
from html.parser import HTMLParser
import json
import os
from pathlib import Path
import re
import tempfile
from zoneinfo import ZoneInfo

import exam_year_plan_408 as annual
from exam_practice_units_408 import contact_projection
from export_morning_input_408 import BRIDGE_ROOT, DETAILS, LibraryStore, REPO
from morning_question_policy_408 import prepare_hot_capture_contact_proof
from morning_selection_408 import preparation_evidence, policy_for
from prepare_d_review_408 import write
from morning_local_followup_408 import LOCAL_STUDY_FOLLOWUP
import recent_contact_408 as contact
from study_visuals_408 import get_visual_pointer
from web_transfer_root_408 import base_for, register_output, validate_cold_output

NATIVE_EXAM_SOURCE = 'native_exam_corpus'
EXAM_LINKS = 'support/syllabus-2027/point-question-links.json'
EXAM_INDEX = 'details/corpus/408统考真题/indexes/question-index.csv'
TEACHING_PREFIX = 'personalization/408/'


def check(ok, reason):
    if not ok:
        raise ValueError(reason)


def read(path):
    return json.loads(Path(path).read_bytes())


def digest(raw):
    return hashlib.sha256(raw).hexdigest()


def source(source_root, relative):
    rel = Path(relative)
    check(not rel.is_absolute() and '..' not in rel.parts, '不安全的来源路径')
    p = source_root / rel
    check(p.is_file() and not p.is_symlink() and p.resolve().is_relative_to(source_root.resolve()), '来源缺失或越界')
    return p


class NativeExamSources:
    """Read the canonical corpus and native teaching projection by their bound version."""

    def __init__(self, repo, details):
        from exam_tutoring_context_408 import DEFAULT_STATE_REL, MANIFEST_FILENAME
        from study_personalization_408 import current_identity

        self.repo, self.details = repo, details
        self.teaching_root = repo / DEFAULT_STATE_REL
        self.catalog = EXAM_INDEX
        current = current_identity(repo)
        check(current.get('status') == 'ready', '本机讲题视图未就绪，请验回本机来源版本')
        pinned = (EXAM_LINKS, EXAM_INDEX, TEACHING_PREFIX + MANIFEST_FILENAME)
        self.hashes = {name: digest(self.path(name).read_bytes()) for name in pinned}
        manifest = json.loads(self.text(TEACHING_PREFIX + MANIFEST_FILENAME))
        check(manifest['source_version'] == current['source_version'], '本机讲题版本在读取中变化')
        self.commit = digest(json.dumps(self.hashes, sort_keys=True).encode())

    def path(self, name):
        if name.startswith('details/'):
            return source(self.details, name.removeprefix('details/'))
        if name.startswith(TEACHING_PREFIX):
            return source(self.teaching_root, name.removeprefix(TEACHING_PREFIX))
        check(name == EXAM_LINKS, '未登记的专项来源：' + name)
        return source(self.repo, name)

    def text(self, name):
        raw = self.path(name).read_bytes()
        if name in self.hashes:
            check(digest(raw) == self.hashes[name], '本机专项来源版本变化：' + name)
        return raw.decode('utf-8')

    def _row(self, name):
        try:
            path = self.path(name)
        except ValueError:
            return None
        return {'sha256': digest(path.read_bytes())}


@contextmanager
def open_exam_sources(repo, details, bridge_root, *, frozen=None, source_commit=None):
    """Current local preparation uses native files. Existing inputs keep their pinned sources."""
    use_native = frozen.get('source_basis') == NATIVE_EXAM_SOURCE if frozen is not None else source_commit is None
    if use_native:
        sources = NativeExamSources(repo, details)
        if frozen is not None:
            check(sources.commit == frozen['source_commit'], '本机专项来源已变化，请用当前版本核对原候选范围')
        yield sources, sources.catalog, NATIVE_EXAM_SOURCE
        return
    check(frozen is None or frozen.get('source_basis') is None, '未知专项来源类型')
    store = LibraryStore(bridge_root)
    try:
        sources = store.snapshot('408', frozen['source_commit'] if frozen is not None else source_commit)
        catalog = None
        if frozen is None:
            check(store.freshness(sources)['status'] == 'current', '指定的外部版本已变化')
            catalog = sources.library['entrypoints']['canonical_exam_index']
        yield sources, catalog, None
    finally:
        store.close()


def read_subquestion(sources, row, basis):
    raw = sources.path('details/' + row['payload_path']).read_bytes()
    if basis == NATIVE_EXAM_SOURCE:
        check(digest(raw) == row.get('payload_sha256'), '本机独立小问来源与已审考点索引不符')
    return json.loads(raw)


def fact_table(repo, day, evidence):
    # The native table is already projected. Reproject only the individual
    # raw overrides, so that a parent date derived from one child cannot be
    # reflected back onto its siblings.
    proof = evidence.get('hot_capture_proof')
    options = {'hot_capture_proof': proof} if proof is not None else {}
    table = dict(contact.real_contacts(repo, day, strict=True, **options)['by_identity'])
    overrides = {}
    for row in evidence.get('recent_contact_overrides', []):
        if dt.date.fromisoformat(row['study_date']) > day:
            continue
        for q in row['question_ids']:
            check(annual.EXAM_RE.fullmatch(q), '补充接触记录的精确题号无效')
            fact = {'study_date': row['study_date'], 'basis': row.get('basis', 'raw_preparation_contact'),
                    'evidence': row['source_path']}
            destination = table if fact['basis'] == 'contained_subquestion_actual_contact' else overrides
            destination[q] = contact.latest_contact(destination.get(q), fact)
    if overrides:
        mapping, _ = annual._mapping(repo)
        for q, fact in contact_projection(overrides, mapping['subquestions'].values()).items():
            table[q] = contact.latest_contact(table.get(q), fact)
    return table


def allowed(repo, q, dependencies, day, frozen):
    required = {q, *dependencies}
    if not all(annual.EXAM_RE.fullmatch(k) for k in required):
        return False
    if any(int(k.split('-')[1]) in frozen['excluded_years'] for k in required):
        return False
    latest = [frozen['contacts'].get(k) for k in required]
    if any(v and 0 <= (day - dt.date.fromisoformat(v['study_date'])).days < frozen['window_days'] for v in latest):
        return False
    return annual.assess_item(repo, {'true_item_id': q, 'contact_dependency_ids': list(dependencies)},
                              frozen['annual'])['allowed']


def policy(repo, day, evidence):
    native = policy_for(repo)
    check(native and native.get('question_limit'), '本地缺少已授权选题策略')
    return {'contacts': fact_table(repo, day, evidence), 'excluded_years': evidence['selection_scope']['excluded_exam_years'],
            'annual': annual.build_exclusion(repo, day.isoformat()), 'window_days': native['exact_item_cooldown_days'],
            'question_limit': native['question_limit'], 'min_independent_subquestions': native.get('min_independent_subquestions', 0)}


def prepare_inputs(scope_file, date, *, output=None, repo=REPO, details=DETAILS, bridge_root=BRIDGE_ROOT):
    if (read(scope_file).get('selection_scope') or {}).get('mode') == 'annual_weak_representatives':
        from annual_focused_training_408 import prepare_inputs as annual_inputs
        return annual_inputs(scope_file, date, output=output, repo=repo, details=details, bridge_root=bridge_root)
    day = dt.date.fromisoformat(date)
    evidence, evidence_files = preparation_evidence(scope_file, date)
    check(evidence.get('selection_scope') is not None, '必须提供用户明确限定且有真实证据的专项范围')
    state = policy(repo, day, evidence)
    point_targets = {}
    targets = {}
    for row in evidence['focus_records']:
        point = row['concept_id']
        if point not in evidence['selection_scope']['allowed_concept_ids']:
            continue
        target = row.get('target_id') or point
        point_targets.setdefault(point, set()).add(target)
        targets.setdefault(target, []).append(row)
    with open_exam_sources(repo, details, bridge_root) as (snap, catalog_source, basis):
        mapping = json.loads(snap.text(EXAM_LINKS))
        catalog = {r['exam_question_id']: r for r in csv.DictReader(io.StringIO(snap.text(catalog_source).lstrip('\ufeff')))}
        candidates = {}
        for q, data in (mapping['questions'] | mapping['subquestions']).items():
            child = '-P' in q
            if not child and int(q[-2:]) > 40:
                continue
            matched = sorted({t for p in data['assessed_points'] for t in point_targets.get(p, ())})
            deps = data.get('contact_dependency_ids') or []
            if not matched or not allowed(repo, q, deps, day, state):
                continue
            if child:
                canonical = read_subquestion(snap, data, basis)
                check(canonical['true_item_id'] == q and canonical['review_status'] == 'reviewed', '独立小问身份/审查异常')
                questions, solutions = canonical['question_images'], canonical['solution_images']
            else:
                canonical = catalog[q]
                questions, solutions = (json.loads(canonical[k]) for k in ('question_assets', 'solution_assets'))
            if not questions or not solutions:
                continue
            assets = {}
            for role, paths in (('question', questions), ('solution', solutions)):
                refs = []
                for path in paths:
                    published = 'details/' + path
                    bound = snap._row(published)
                    check(bound, '缺少已核精确题源：' + published)
                    refs.append({'path': published, 'sha256': bound['sha256']})
                assets[role] = refs
            candidates[q] = {'true_item_id': q, 'primary_point': data['primary_point'], 'assessed_points': data['assessed_points'],
                             'candidate_target_ids': matched, 'keywords': data.get('keywords') or [p['focus'] for p in data.get('parts', [])],
                             'contact_dependency_ids': deps, 'latest_contact': state['contacts'].get(q), 'assets': assets,
                             'question_text': canonical['question_text'] if child else canonical['question_ocr'],
                             'prerequisite': canonical.get('provided_prerequisite_results'), 'syllabus_ids': data.get('syllabus_ids', [])}
        frozen = {'schema': 'cs408-local-focused-input-v1', 'date': date, 'source_commit': snap.commit,
                  'scope_sha256': digest(scope_file.read_bytes()), 'evidence_sha256': digest(evidence_files['preparation-evidence/context.json']), 'question_limit': state['question_limit'],
                  'min_independent_subquestions': state['min_independent_subquestions'], 'excluded_years': state['excluded_years'],
                  'window_days': state['window_days'], 'targets': targets, 'candidates': candidates}
        if basis:
            frozen['source_basis'] = basis
    raw = json.dumps(frozen, ensure_ascii=False, sort_keys=True).encode()
    fingerprint = digest(raw)
    if output is None:
        base, _ = base_for('morning-input', details, 'review-deliveries/morning-input')
        output = base.parent / '本地专项备课'
    dest = validate_cold_output(output, details=details) / date / fingerprint
    dest.mkdir(parents=True, exist_ok=True)
    for name, contents in {**evidence_files, 'pool.json': raw}.items():
        path = dest / name
        if path.exists():
            check(path.read_bytes() == contents, '专项来源内容已变化：' + name)
        else:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(contents)
    registration = register_output(dest, details=details, purpose='本地独立专项有界证据和候选')
    return {'status':'LOCAL_FOCUSED_AUTHORING_READY','workspace':str(dest), 'input_version':fingerprint,
            'candidate_count':len(candidates), 'scope_count':len(targets), 'learning_started':False, 'storage_registration':registration}


class LessonControls(HTMLParser):
    """Identify exact, unique solution-side controls used by a coverage note."""
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.ids = set()
        self.numbers = set()
        self.duplicates = set()
        self.all_ids = set()

    def handle_starttag(self, tag, attrs):
        values = dict(attrs)
        element_id = values.get('id')
        if element_id:
            if element_id in self.all_ids:
                self.duplicates.add(('id', element_id))
            self.all_ids.add(element_id)
        if ('disabled' in values or 'hidden' in values or values.get('aria-hidden') == 'true'
                or values.get('aria-disabled') == 'true' or values.get('type') == 'hidden'):
            return
        if not (tag in {'button', 'summary', 'select', 'input'} or
                (tag == 'a' and values.get('href')) or values.get('role') in {'button','tab','switch','slider','radio'}):
            return
        for key, items in [('id', self.ids), ('data-review-step', self.numbers)]:
            label = values.get(key)
            if label:
                if label in items:
                    self.duplicates.add((key, label))
                items.add(label)

    def contains(self, step):
        if type(step) is int:
            label = str(step)
            return step > 0 and label in self.numbers and ('data-review-step', label) not in self.duplicates
        return isinstance(step, str) and step in self.ids and ('id', step) not in self.duplicates


def review_coverage(row, candidate, targets, controls):
    q = row['true_item_id']
    matched = set(candidate['candidate_target_ids'])
    covered = set()
    coverage = row.get('coverage')
    check(isinstance(coverage, list) and coverage, '本题缺少目标依据：' + q)
    for entry in coverage:
        target, mode = entry['target_id'], entry['mode']
        check(target in targets and target not in covered and mode in {'direct','partial','teaching_only'}
              and isinstance(entry.get('basis'), str) and entry['basis'].strip(), '未知、重复目标或依据无效：' + q)
        if mode == 'direct':
            check(target in matched, '直测目标未由本题实际考点绑定：' + q + '/' + target)
        else:
            check(isinstance(entry.get('teaching'), str) and entry['teaching'].strip()
                  and controls.contains(entry.get('visual_step')), '补讲缺正文或未绑定实际交互控件：' + q + '/' + target)
            if target not in matched:
                check(mode == 'teaching_only' and isinstance(entry.get('relation_basis'), str)
                      and entry['relation_basis'].strip(), '跨点仅可作为有具体关联依据的补讲：' + q + '/' + target)
        covered.add(target)
    check(bool(matched & covered), '本题未绑定任何候选目标：' + q)
    return covered


def copy_verified(path, target, expected_hash):
    data = path.read_bytes()
    check(digest(data) == expected_hash, '复制时已核来源发生变化：' + str(path))
    target.write_bytes(data)
    check(digest(target.read_bytes()) == expected_hash, '热队列课件或题图落盘校验失败')


def freeze_queue(workspace, draft, *, study_date, repo=REPO, details=DETAILS, bridge_root=BRIDGE_ROOT):
    raw = source(workspace, 'pool.json').read_bytes()
    frozen = json.loads(raw)
    if frozen.get('mode') == 'annual_weak_representatives':
        from annual_focused_training_408 import prepare_queue
        return prepare_queue(workspace, draft, study_date=study_date, repo=repo, details=details, bridge_root=bridge_root)
    check(workspace.name == digest(raw), '专项工作区指纹漂移')
    context_file = source(workspace, 'preparation-evidence/context.json')
    check(digest(context_file.read_bytes()) == frozen['evidence_sha256'], '已登记专项证据范围变化')
    evidence, _ = preparation_evidence(context_file, frozen['date'])
    check(evidence is not None, '专项证据缺失')
    day = dt.date.fromisoformat(study_date)
    hot_proof = prepare_hot_capture_contact_proof(repo, study_date)
    state = policy(repo, day, {**evidence, 'hot_capture_proof': hot_proof})
    draft_raw = draft.read_bytes()
    authored = json.loads(draft_raw)
    check(authored.get('input_version') == digest(raw), '成稿属于其他专项')
    rows = authored['items']
    ids = [r['true_item_id'] for r in rows]
    candidates = frozen['candidates']
    check(len(ids) == len(set(ids)) == frozen['question_limit'] and set(ids) <= set(candidates), '题量、唯一性或真实身份未通过')
    check(len({candidates[q]['primary_point'] for q in ids}) == len(ids), '重复主考点')
    check(sum('-P' in q for q in ids) >= frozen['min_independent_subquestions'], '独立小问不足')
    covered = set()
    signature = hashlib.sha256(raw + draft_raw + json.dumps(hot_proof, sort_keys=True).encode())
    for row in rows:
        q = row['true_item_id']
        c = candidates[q]
        deps = c['contact_dependency_ids']
        check(allowed(repo, q, deps, day, state) and not set(deps).intersection(ids), '接触、年度或已给答案门禁：' + q)
        check(row.get('source_check') and row.get('mainline') and row.get('rationale') and row.get('local_semantic_review'),
              '来源、解析、采用理由或实际本地审核缺项：' + q)
        check(row['hint_2d_sha256'] != row['solution_2d_sha256'], '提示和完整图解须使用各自的课件：' + q)
        controls = LessonControls()
        for stage in ('hint_2d', 'solution_2d'):
            html = source(draft.parent, row[stage])
            contents = html.read_bytes()
            check(html.suffix == '.html' and digest(contents) == row[stage + '_sha256'], '本地交互课件缺失/变化')
            signature.update(contents)
            if stage == 'solution_2d':
                controls.feed(contents.decode('utf-8'))
                controls.close()
        covered.update(review_coverage(row, c, frozen['targets'], controls))
        check(row.get('visual_review') and row.get('hint_safety_review'), '缺少逐题交互/提示答案保护审核')
        for role in ('question','solution'):
            check(row.get(role+'_image_sha256') == [a['sha256'] for a in c['assets'][role]], '须核验同一原题与解析图：'+q)
    check(covered == set(frozen['targets']), '专项还有未安排去向的真实机制')
    signature.update(study_date.encode())
    signature.update(b"prepared-question-media-v1")
    identity = signature.hexdigest()
    hot = details/'learning-deliveries/专项'/identity
    hot.parent.mkdir(parents=True,exist_ok=True)
    with open_exam_sources(repo, details, bridge_root, frozen=frozen) as (snap, _, _):
        with tempfile.TemporaryDirectory(dir=hot.parent, prefix='.focused-') as wd:
            out = Path(wd)/'content'; out.mkdir()
            public, media_index = [], {}
            for i, row in enumerate(rows, 1):
                q=row['true_item_id'];c=candidates[q]
                images=[]
                for j, asset in enumerate(c['assets']['question'], 1):
                    original = snap.path(asset['path'])
                    dest = out / f'student/{i:02}/{j:02}{original.suffix}'
                    dest.parent.mkdir(parents=True, exist_ok=True)
                    copy_verified(original, dest, asset['sha256'])
                    images.append(str(dest.relative_to(out)))
                for asset in c['assets']['solution']:
                    check(digest(snap.path(asset['path']).read_bytes()) == asset['sha256'],'解析版本漂移')
                private = out/'private'/q; private.mkdir(parents=True)
                for stage in ('hint_2d','solution_2d'):
                    copy_verified(source(draft.parent, row[stage]), private/(stage + '.html'), row[stage + '_sha256'])
                stages={s:get_visual_pointer(repo,q,stage=s,hint_level=1) for s in ('question','hint','solution')}
                check(all(v.get('status')=='available' for v in stages.values()), '精确3D课件不足：'+q)
                write(private/'teacher.json',{**row,'stages_3d':stages})
                media_index[q] = {
                    '2d': {stage: {'path': (private / (name + '.html')).relative_to(out).as_posix(),
                                   'sha256': row[name + '_sha256']}
                           for stage, name in (('hint-1', 'hint_2d'), ('solution', 'solution_2d'))},
                    '3d': {},
                    'native_3d': {stage: stages[key] for stage, key in
                                  (('question', 'question'), ('hint-1', 'hint'), ('solution', 'solution'))},
                    'teacher': {'path': (private/'teacher.json').relative_to(out).as_posix(),
                                'sha256': digest((private/'teacher.json').read_bytes())},
                }
                public.append({'ordinal':i,'true_item_id':q,'question_images':images})
            write(out/'student/queue.json',public)
            write(out/'private/media-index.json', media_index)
            guard = {'hot_capture_proof': hot_proof, 'selection_scope': {'excluded_exam_years': state['excluded_years']},
                     'recent_contact_overrides': evidence.get('recent_contact_overrides', []),
                     'selected': {q: {'dependencies': candidates[q]['contact_dependency_ids'],
                                      'image_sha256': [r['sha256'] for r in candidates[q]['assets']['question']]} for q in ids}}
            write(out/'private/start-gates.json',guard)
            write(out/'binding.json',{'input_version':workspace.name,'source_commit':snap.commit,'date':study_date,
                'workspace':str(workspace),'guard_sha256':digest((out/'private/start-gates.json').read_bytes()), 'queue_sha256':digest((out/'student/queue.json').read_bytes()),
                'media_index_sha256':digest((out/'private/media-index.json').read_bytes()),
                'learner_event_count':0,'status':'PREPARED_NOT_STARTED'})
            write(out/'START_STUDY.md', '独立专项已准备；用户明确开始才逐题展示。每题先运行 scripts/local_focused_training_408.py next --queue '+str(hot)+' --ordinal <序号> 查当前学习日资格。只展示该问已核原图，等待真实回答。内部保留返回的 teaching_context；答后使用 question-worker 和最新个性化摘要，并按允许阶段调用返回的 media 入口。直接使用已备成品和当前题教案。真实终态按wrong-intake封存。\n' + LOCAL_STUDY_FOLLOWUP + '\n')
            if hot.exists():
                expected={p.relative_to(out):digest(p.read_bytes()) for p in out.rglob('*') if p.is_file()}
                check(all((hot/r).is_file() and digest((hot/r).read_bytes())==v for r,v in expected.items()),'同身份专项热队列已有变动')
            else:
                os.rename(out, hot)
    return {'status':'PREPARED_NOT_STARTED','handoff':str(hot/'START_STUDY.md'),'question_count':len(rows),
            'prepared_visual_question_count':len(rows),'learner_event_count':0}


def prepared_teaching_route(hot, binding, ordinal, question_id):
    """Return the current item's media route without reading private stage files."""
    if not binding.get('media_index_sha256'):
        return {}
    return {'teaching_context': {'entrypoint': 'local_focused_training_408.py media',
            'queue': str(hot.resolve()), 'ordinal': ordinal, 'true_item_id': question_id,
            'binding_sha256': digest((hot/'binding.json').read_bytes())}}


def next_item(hot, ordinal, *, day, repo=REPO):
    from annual_focused_training_408 import replacement_notice
    moved = replacement_notice(hot)
    if moved:
        return moved
    binding=read(hot/'binding.json')
    if binding.get('mode') == 'annual_weak_representatives':
        from annual_focused_training_408 import next_item as annual_next
        return annual_next(hot, ordinal, day, repo=repo)
    guard_source=source(hot,'private/start-gates.json')
    check(digest(guard_source.read_bytes())==binding['guard_sha256'] and
          digest((hot/'student/queue.json').read_bytes())==binding['queue_sha256'],'专项队列绑定漂移')
    guard=read(guard_source)
    public=read(hot/'student/queue.json')
    check(1<=ordinal<=len(public),'题序超出范围')
    row=public[ordinal-1]; q=row['true_item_id']; c=guard['selected'][q]
    check(allowed(repo,q,c['dependencies'],day,policy(repo,day,guard)),'当前题与实际学习日门禁冲突')
    images=[source(hot, rel) for rel in row['question_images']]
    check([digest(p.read_bytes()) for p in images]==c['image_sha256'],'学生图变化')
    return {**row,'question_images':[str(p) for p in images],'learner_event_count':0,
            **prepared_teaching_route(hot, binding, ordinal, q)}


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    commands=parser.add_subparsers(dest='cmd',required=True)
    gather=commands.add_parser('sources', aliases=['annual-sources']); gather.add_argument('--date',required=True); gather.add_argument('--context',type=Path,required=True)
    gather.add_argument('--output',type=Path)
    finish=commands.add_parser('prepare'); finish.add_argument('--workspace',type=Path,required=True);finish.add_argument('--draft',type=Path,required=True);finish.add_argument('--date',required=True)
    representatives=commands.add_parser('annual-prepare')
    representatives.add_argument('--workspace',type=Path,required=True)
    representatives.add_argument('--overlays',type=Path,required=True)
    representatives.add_argument('--date',required=True)
    media=commands.add_parser('media')
    media.add_argument('--queue',type=Path,required=True)
    media.add_argument('--ordinal',type=int,required=True)
    media.add_argument('--stage',choices=['question','hint-1','solution'],required=True)
    media.add_argument('--question-id')
    media.add_argument('--binding-sha256')
    activate=commands.add_parser('annual-activate')
    activate.add_argument('--previous',type=Path,required=True)
    activate.add_argument('--queue',type=Path,required=True)
    activate.add_argument('--evidence',type=Path,required=True)
    status=commands.add_parser('status'); status.add_argument('--queue',type=Path,required=True)
    current=commands.add_parser('next');current.add_argument('--queue',type=Path,required=True);current.add_argument('--ordinal',type=int,required=True)
    args=parser.parse_args()
    if args.cmd in {'sources','annual-sources'}:
        if args.cmd == 'annual-sources':
            check((read(args.context).get('selection_scope') or {}).get('mode') == 'annual_weak_representatives', '年度真题复盘须绑定 annual_weak_representatives 源范围')
        result=prepare_inputs(args.context,args.date,output=args.output)
    elif args.cmd=='annual-prepare':
        from annual_focused_training_408 import prepare_queue
        result=prepare_queue(args.workspace,args.overlays,study_date=args.date)
    elif args.cmd=='media':
        from annual_focused_training_408 import read_media
        commitment = {}
        if args.question_id or args.binding_sha256:
            check(args.question_id and args.binding_sha256, '同时传入出题返回的本题身份和队列SHA')
            commitment = {'expected_question_id':args.question_id, 'expected_binding_sha256':args.binding_sha256}
        result=read_media(args.queue,args.ordinal,args.stage,day=dt.datetime.now(ZoneInfo('Asia/Shanghai')).date(),**commitment)
    elif args.cmd=='annual-activate':
        from annual_focused_training_408 import activate_unstarted
        result=activate_unstarted(args.previous,args.queue,args.evidence)
    elif args.cmd=='status':
        from annual_focused_training_408 import replacement_notice
        result=replacement_notice(args.queue) or {'status':read(args.queue/'binding.json')['status'],
                  'handoff':str(args.queue/'START_STUDY.md'),'learner_event_count':0}
    elif args.cmd=='prepare': result=freeze_queue(args.workspace,args.draft,study_date=args.date)
    else:result=next_item(args.queue,args.ordinal,day=dt.datetime.now(ZoneInfo('Asia/Shanghai')).date())
    print(json.dumps(result,ensure_ascii=False,indent=2))


if __name__=='__main__':
    main()
