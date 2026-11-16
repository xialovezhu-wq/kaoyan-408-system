"""Scan every real exam unit, then prepare up to 30 useful weak-point representatives."""
from __future__ import annotations

from collections import Counter
import csv
import datetime as dt
import heapq
import io
import json
import os
from pathlib import Path
from zoneinfo import ZoneInfo
import re
import tempfile

import exam_year_plan_408 as year_plan
from export_morning_input_408 import BRIDGE_ROOT, DETAILS, REPO
from knowledge_hot_routes_408 import Reader
from local_focused_training_408 import (
    EXAM_LINKS, TEACHING_PREFIX, check, copy_verified, digest, fact_table,
    open_exam_sources, prepared_teaching_route, read, read_subquestion, source,
)
from morning_local_followup_408 import LOCAL_STUDY_FOLLOWUP
from morning_question_policy_408 import prepare_hot_capture_contact_proof
from morning_selection_408 import preparation_evidence
from prepare_d_review_408 import write
from study_visuals_408 import get_visual_pointer
from web_transfer_root_408 import base_for, register_output, validate_cold_output


MODE = 'annual_weak_representatives'
MAX_SELECTED = 30
DAILY_CLOSEOUT_MAX_PER_TARGET = 2


def reserved_years(repo, day, explicit):
    """Merge the caller's frozen exclusions with the explicit plan for this day."""
    reserved = set(explicit)
    path = repo / year_plan.PLAN_REL
    if path.exists():
        row = read(path).get('daily_plans', {}).get(day, {})
        if row.get('source') == 'explicit_user':
            year = row.get('excluded_exam_year') or row.get('exam_year')
            if year is not None:
                check(type(year) is int and year >= 2009, '已登记保留年份无效')
                reserved.add(year)
    return sorted(reserved)


def preparation_exclusions(repo, day, scope):
    excluded = set(scope['excluded_exam_years'])
    closeout = scope.get('daily_closeout')
    if closeout is not None:
        studied_year = closeout.get('studied_exam_year')
        if studied_year is not None:
            excluded.add(studied_year + 1)
        excluded.update(reserved_years(repo, closeout['study_date'], ()))
    return reserved_years(repo, day, excluded)


def year_of(q):
    check(isinstance(q, str) and year_plan.EXAM_RE.fullmatch(q), '无效真题身份：' + str(q))
    return int(q.split('-')[1])


def dependencies(q, records, trail=()):
    check(q in records and q not in trail, '题源前置不存在或循环：' + q)
    seen = set()
    for dep in records[q].get('contact_dependency_ids') or []:
        year_of(dep)
        seen.add(dep)
        seen.update(dependencies(dep, records, (*trail, q)))
    return sorted(seen)


def study_order(pool):
    """Stable chronological order, presenting a tested prerequisite before its supplied result."""
    unlocked_by = {}
    waiting = {}
    for q, row in pool.items():
        before = set(row['contact_dependency_ids']).intersection(pool)
        waiting[q] = len(before)
        for dep in before:
            unlocked_by.setdefault(dep, []).append(q)
    ready = [q for q, n in waiting.items() if not n]
    heapq.heapify(ready)
    order = []
    while ready:
        q = heapq.heappop(ready)
        order.append(q)
        for child in unlocked_by.get(q, []):
            waiting[child] -= 1
            if not waiting[child]:
                heapq.heappush(ready, child)
    check(len(order) == len(pool), '命中集合存在循环前置；保留全部身份待复核')
    return order


def evidence_targets(evidence):
    scope = evidence['selection_scope']
    closeout = scope.get('daily_closeout')
    check(scope.get('mode') == MODE and (closeout is not None or scope.get('source_question_years')),
          '须以已完成年度卷或当日快存的真实断点限定真题检索范围')
    by_code, targets = {}, {}
    for fact in evidence['focus_records']:
        code = fact['concept_id']
        if code not in scope['allowed_concept_ids']:
            continue
        target = fact.get('target_id') or code
        identities = fact.get('question_identity_hints') or []
        check((identities or closeout is not None) and all(year_of(q) in scope['source_question_years'] for q in identities),
              '目标与原学习证据不符：' + target)
        targets.setdefault(target, []).append(fact)
        by_code.setdefault(code, set()).add(target)
    return by_code, targets


def semantic_matches(scope, evidence_files, targets, records):
    name = scope.get('semantic_matches_path')
    if not name:
        return {}
    key = 'preparation-evidence/' + name
    check(key in evidence_files, '语义补充必须由本批已核文件SHA绑定')
    additions = {}
    for row in json.loads(evidence_files[key])['items']:
        q = row['true_item_id']
        target_ids = row.get('target_ids') or []
        check(q in records and (q.split('-')[-1].startswith('P') or int(q[-2:]) <= 40), '语义补充须绑定精确选择题或Pxx')
        check(target_ids and set(target_ids) <= set(targets) and row.get('concept_ids') and
              set(row['concept_ids']) <= {r['concept_id'] for t in target_ids for r in targets[t]}
              and isinstance(row.get('basis'), str) and row['basis'].strip(),
              '语义补充缺真实目标或核源依据：' + q)
        additions.setdefault(q, []).append(row)
    return additions


def contact_priority(candidates, contacts, day):
    """Gate each evidenced target separately; secondary learning never promotes a blocked target."""
    if isinstance(day, str):
        day = dt.date.fromisoformat(day)
    groups = {}
    for q, candidate in candidates.items():
        raw_date = (contacts.get(q) or {}).get('study_date')
        gap = None if raw_date is None else (day - dt.date.fromisoformat(raw_date)).days
        check(gap is None or gap >= 0, '真实接触日期晚于核验日：' + q)
        for target in candidate['candidate_target_ids']:
            groups.setdefault(target, {})[q] = raw_date
    result = {}
    for target, dates in sorted(groups.items()):
        older, unknown, recent = [], [], []
        for q, date in sorted(dates.items()):
            if date is None:
                unknown.append(q)
            elif (day - dt.date.fromisoformat(date)).days >= 14:
                older.append(q)
            else:
                recent.append(q)
        fallback = None
        if older:
            route, eligible = 'known_at_least_14_days', older
        elif unknown:
            route, eligible = 'no_recorded_contact', unknown
        else:
            fallback = min(dates[q] for q in recent)
            route = 'oldest_recent_contact'
            eligible = [q for q in recent if dates[q] == fallback]
        result[target] = {'route':route,'eligible_ids':eligible,'older_ids':older,
                          'unknown_ids':unknown,'recent_ids':recent,'fallback_contact_date':fallback}
    return result


def reviewed_mechanism_pool(pool, targets, scope, files, snap, details):
    """Compare dates only after the owner reviews actual narrow mechanisms.

    Optional and immutable per preparation; existing queues keep their frozen
    memberships. The complete coarse point search is retained as audit evidence.
    No word matching, model calls or learner-state writes occur here.
    """
    name = scope.get('reviewed_mechanism_matches_path')
    if not name:
        return pool, None
    key = 'preparation-evidence/' + name
    check(key in files, '逐机制核查必须绑定本批真实文件SHA')
    review = json.loads(files[key])
    check(review.get('schema') == 'cs408-reviewed-mechanism-matches-v1'
          and review.get('reviewed_by') and review.get('complete') is True,
          '逐机制核查尚未完成')
    expected = {(q, t) for q, row in pool.items() for t in row['candidate_target_ids']}
    seen, accepted, cached, rejected = set(), {}, {}, []
    for row in review.get('rows', []):
        pair = (row.get('true_item_id'), row.get('target_id'))
        check(pair in expected and pair not in seen and pair[1] in targets,
              '逐机制核查题目/目标越界或重复：' + str(pair))
        seen.add(pair)
        check(row.get('level') in {'full', 'partial', 'not_tested'}
              and isinstance(row.get('basis'), str) and row['basis'].strip(),
              '逐机制核查缺少实际设问依据：' + str(pair))
        ref = row.get('source_ref') or {}
        path = ref.get('path', '')
        published = path if path.startswith('details/') else 'details/' + path
        check(ref.get('question_id') == pair[0] and Path(published).stem == pair[0]
              and published.startswith('details/corpus/408统考真题/'),
              '逐机制核查的原件身份不符：' + str(pair))
        if published not in cached:
            bound = snap._row(published)
            original = (snap.path(published) if bound else
                        source(details, published.removeprefix('details/')))
            actual = digest(original.read_bytes())
            check(bound is None or actual == bound['sha256'], '逐机制核查发布原件漂移')
            cached[published] = actual
        check(cached[published] == ref.get('sha256'), '逐机制核查原件SHA漂移：' + str(pair))
        if row['level'] == 'not_tested':
            rejected.append(row)
        else:
            accepted.setdefault(pair[0], {})[pair[1]] = row
    check(seen == expected, '逐机制核查遗漏全库候选，不能先按大标签或日期删掉未审题')
    result = {}
    for q, hits in accepted.items():
        result[q] = {**pool[q], 'candidate_target_ids': sorted(hits),
                     'coarse_candidate_target_ids': pool[q]['candidate_target_ids'],
                     'reviewed_mechanism_matches': hits}
    return result, {'schema': review['schema'], 'source_path': name,
                    'source_sha256': digest(files[key]), 'reviewed_by': review['reviewed_by'],
                    'coarse_candidate_count': len(pool), 'coarse_pair_count': len(expected),
                    'matched_candidate_count': len(result), 'matched_pair_count': sum(map(len, accepted.values())),
                    'rejected': rejected}


def ensure_bound_file(path, contents):
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        check(path.read_bytes() == contents, '既有年度专项材料漂移：' + path.name)
        return
    with tempfile.NamedTemporaryFile(dir=path.parent, delete=False) as handle:
        handle.write(contents)
        temp = Path(handle.name)
    try:
        try:
            os.link(temp, path)
        except FileExistsError:
            check(path.read_bytes() == contents, '并行生成的年度专项材料不一致')
    finally:
        temp.unlink(missing_ok=True)


def prepare_inputs(scope_file, day, *, output=None, repo=REPO, details=DETAILS, bridge_root=BRIDGE_ROOT, source_commit=None):
    dt.date.fromisoformat(day)
    evidence, files = preparation_evidence(scope_file, day, annual_all=True)
    by_code, targets = evidence_targets(evidence)
    scope = evidence['selection_scope']
    closeout = scope.get('daily_closeout')
    keep_out = preparation_exclusions(repo, day, scope)
    with open_exam_sources(repo, details, bridge_root, source_commit=source_commit) as (snap, catalog_source, basis):
        mapping = json.loads(snap.text(EXAM_LINKS))
        records = mapping['questions'] | mapping['subquestions']
        catalog = {r['exam_question_id']:r for r in csv.DictReader(io.StringIO(snap.text(catalog_source).lstrip('\ufeff')))}
        check(set(catalog)==set(mapping['questions']), '统考目录与考点索引父题身份不一致')
        additions = semantic_matches(evidence['selection_scope'], files, targets, records)
        pool, forbidden, missing = {}, [], {}
        addenda = year_plan._addenda_document(repo)
        unit_count = 0
        for q, row in sorted(records.items()):
            child = '-P' in q
            if not child and int(q[-2:]) > 40:
                continue  # Parent is reported once; its tested units are the reviewed Pxx.
            unit_count += 1
            deps = dependencies(q, records)
            if any(year_of(code) in keep_out for code in [q, *deps]):
                forbidden.append(q)
                continue
            points, _ = year_plan._effective_points(repo, q, row, {}, mapping, addenda)
            indexed_targets = {t for code in points for t in by_code.get(code, ())}
            supplement = additions.get(q, [])
            matched = indexed_targets | {t for s in supplement for t in s['target_ids']}
            if not matched:
                continue
            canonical = read_subquestion(snap, row, basis) if child else catalog[q]
            if child:
                check(canonical['true_item_id']==q and canonical['review_status']=='reviewed', '独立小问来源不符：' + q)
                images = canonical['question_images'], canonical['solution_images']
            else:
                images = json.loads(canonical['question_assets']), json.loads(canonical['solution_assets'])
            assets = {}
            gaps = []
            for kind, paths in zip(('question','solution'),images,strict=True):
                assets[kind] = []
                for path in paths:
                    published = 'details/' + path
                    binding = snap._row(published)
                    if binding is None:
                        gaps.append(published)
                    else:
                        assets[kind].append({'path':published,'sha256':binding['sha256']})
                if not assets[kind]:
                    gaps.append(kind+'_source_missing')
            if gaps:
                missing[q] = gaps
            pool[q] = {'true_item_id':q,'parent_true_item_id':q.split('-P')[0],
                       'primary_point':row['primary_point'],'assessed_points':sorted(points),
                       'matched_codes':sorted(set(points)&by_code.keys()),'candidate_target_ids':sorted(matched),
                       'index_target_ids':sorted(indexed_targets),'semantic_matches':supplement,
                       'contact_dependency_ids':deps,'assets':assets,'syllabus_ids':row.get('syllabus_ids',[]),
                       'question_text':canonical['question_text'] if child else canonical['question_ocr'],
                       'prerequisite':canonical.get('provided_prerequisite_results'),
                       'source_status':'needs_source' if gaps else 'verified_references'}
        pool, mechanism_review = reviewed_mechanism_pool(
            pool, targets, evidence['selection_scope'], files, snap, details)
        missing = {q:gaps for q,gaps in missing.items() if q in pool}
        if closeout is not None:
            # An item without usable source assets cannot displace a ready representative.
            pool = {q: row for q, row in pool.items() if q not in missing}
        counts = {t:sum(t in r['candidate_target_ids'] for r in pool.values()) for t in targets}
        contacts = fact_table(repo, dt.date.fromisoformat(day), evidence)
        dates = {q: contacts[q] for q in pool if q in contacts}
        priorities = contact_priority(pool, dates, day)
        frozen = {'schema':'cs408-annual-weak-representative-input-v1','mode':MODE,'max_selected':MAX_SELECTED,'date':day,
                  'source_commit':snap.commit,'source_years':evidence['selection_scope']['source_question_years'],
                  'excluded_years':keep_out,'targets':targets,'candidates':pool,
                  'order':study_order(pool),'source_gaps':missing,'target_counts':counts,
                  'latest_contacts':dates,'per_target_contact':priorities,
                  'unmatched_targets':[t for t,n in counts.items() if not n],
                  'mechanism_review':mechanism_review,
                  'evidence_sha256':digest(files['preparation-evidence/context.json']),
                  'inventory':{'accepted_parent_count':len(catalog),'reviewed_subquestion_count':len(mapping['subquestions']),
                               'scanned_unit_count':unit_count,'reserved_unit_count':len(forbidden),
                               'eligible_unit_count':unit_count-len(forbidden),'matched_unit_count':len(pool),
                               'matched_parent_count':len({r['parent_true_item_id'] for r in pool.values()})}}
        if closeout is not None:
            frozen['daily_closeout'] = closeout
        if basis:
            frozen['source_basis'] = basis
    raw = json.dumps(frozen,ensure_ascii=False,sort_keys=True).encode()
    if output is None:
        incoming,_ = base_for('morning-input',details,'review-deliveries/morning-input')
        output = incoming.parent/'年度薄弱点本地筛选'
    dest = validate_cold_output(output,details=details)/day/digest(raw)
    for name,contents in {**files,'pool.json':raw}.items():
        ensure_bound_file(dest/name,contents)
    proof=register_output(dest,details=details,purpose='整卷薄弱考点全体真实命中及本地来源')
    return {'status':'ANNUAL_CANDIDATES_ENUMERATED','mode':MODE,'workspace':str(dest),'input_version':digest(raw),
            **frozen['inventory'],'source_gaps':missing,'learning_started':False,'storage_registration':proof}


def bound_overlay(config, key):
    ref = config.get(key)
    if ref is None:
        return None
    check(isinstance(ref, dict) and isinstance(ref.get('path'), str), '本地补充索引缺少路径：' + key)
    path = Path(ref['path'])
    check(path.is_absolute() and path.is_file() and not path.is_symlink(), '本地补充索引无效：' + key)
    raw = path.read_bytes()
    check(digest(raw) == ref.get('sha256'), '本地补充索引哈希变化：' + key)
    return json.loads(raw)


def selected_codes(candidate, target, targets):
    evidence_codes = {row['concept_id'] for row in targets[target]}
    actual = set(candidate['assessed_points']) & evidence_codes
    for row in candidate.get('semantic_matches', []):
        if target in row['target_ids']:
            actual |= set(row.get('concept_ids') or ()) & evidence_codes
    return sorted(actual)


def verify_selection(frozen, decision, priorities):
    ids = decision.get('selected_ids')
    cap = frozen['max_selected']
    check(isinstance(ids, list) and all(isinstance(q, str) for q in ids) and len(ids) == len(set(ids)) <= cap <= MAX_SELECTED,
          '年度训练须明确选择互异的至多30题，不能套用全量候选或固定下限')
    check(set(ids) <= frozen['candidates'].keys(), '所选题有未通过同版真题与保留年份核验的身份')
    reasons = decision.get('selection_reasons') or {}
    anchors = decision.get('selection_targets') or {}
    check(set(anchors) == set(reasons) == set(ids), '必须逐题写采用考点和新增诊断价值')
    if not ids:
        check(isinstance(decision.get('no_selection_reason'), str) and decision['no_selection_reason'].strip(),
              '选择空集须保留真实去向与原因')
    for q in ids:
        tags = anchors[q]
        check(isinstance(tags, list) and tags and len(tags) == len(set(tags)), '至少绑定一个真实采用考点：' + q)
        check(isinstance(reasons[q], str) and reasons[q].strip(), '缺少本题具体诊断价值：' + q)
        for target in tags:
            check(target in frozen['candidates'][q]['candidate_target_ids'] and q in priorities.get(target, {}).get('eligible_ids', []),
                  f'本题不能代表{target}：{q}；同点有更优日期层或来源不符')
    if 'daily_closeout' in frozen:
        counts = Counter(target for tags in anchors.values() for target in tags)
        for target, count in sorted(counts.items()):
            check(count <= DAILY_CLOSEOUT_MAX_PER_TARGET, f'日终专项每个错点最多2题：{target}已选{count}题')
        gaps = decision.get('unrepresented_target_reasons') or {}
        check(isinstance(gaps, dict) and set(gaps) == set(frozen['targets']) - counts.keys()
              and all(isinstance(reason, str) and reason.strip() for reason in gaps.values()),
              '为每个未选题的日终错点注明题源、日期资格或诊断覆盖的具体原因')
    return ids, anchors


def final_hits(config, frozen):
    report = bound_overlay(config, 'qualified_hits')
    if report is None:
        return {}
    check(report.get('provisional') is not True, '不可把尚待核查的候选注解当作最终核验')
    result = {}
    for row in report['hits']:
        q, target = row['true_item_id'], row['target_id']
        check(q in frozen['candidates'] and target in frozen['candidates'][q]['candidate_target_ids'],
              '机制实核与冻结题源绑定不一致：' + q + '/' + target)
        check(isinstance(row.get('why'), str) and row['why'].strip(), '具体考法缺依据：'+q+'/'+target)
        check(target not in result.setdefault(q, {}), '重复的题/目标复核：'+q+'/'+target)
        result[q][target] = row
    expected = {q:set(r['candidate_target_ids']) for q,r in frozen['candidates'].items()}
    check({q:set(hits) for q,hits in result.items()} == expected, '全库考法核查遗漏实际命中')
    return result


def after_class_targets(decision, frozen, ids):
    result = {q:[] for q in ids}
    for item in decision.get('after_class', []):
        q, t = item['true_item_id'], item['target_id']
        check(q in result and t in frozen['targets'] and t not in {v['target_id'] for v in result[q]},
              '补讲目标须真实、唯一且挂在本轮已选题：'+str(q)+'/'+str(t))
        check(all(isinstance(item.get(k), str) and item[k].strip() for k in ('basis','teaching'))
              and (isinstance(item.get('visual_step'),str) and item['visual_step'].strip()
                   or type(item.get('visual_step')) is int and item['visual_step']>0),
              '补讲缺少具体依据、正文或图解位置：'+q+'/'+t)
        result[q].append({**item,'mode':'teaching_only','establishes_direct_test':False})
    return result


def scope_payload(frozen, day, evidence, repo, *, hot_proof=None):
    explicit = reserved_years(repo, day.isoformat(), frozen['excluded_years'])
    usable = {q:r for q,r in frozen['candidates'].items() if
              not any(year_of(k) in explicit for k in [q,*r['contact_dependency_ids']])}
    facts = fact_table(repo, day, {**evidence, **({'hot_capture_proof': hot_proof} if hot_proof else {})})
    return usable, contact_priority(usable, facts, day), explicit


def copy_assets(snap, row, stage, ordinal, root):
    relative = []
    for i, ref in enumerate(row['assets'][stage], 1):
        suffix = Path(ref['path']).suffix.lower()
        check(suffix in ('.png','.jpg','.jpeg','.webp','.svg'), '本题源图格式不在准入范围：'+ref['path'])
        name = (f'student/{ordinal:03}/{i:02}{suffix}' if stage=='question'
                else f'private/originals/{ref["sha256"]}{suffix}')
        dest = root / name
        if not dest.exists():
            dest.parent.mkdir(parents=True,exist_ok=True)
            copy_verified(snap.path(ref['path']), dest, ref['sha256'])
        else:
            check(digest(dest.read_bytes())==ref['sha256'],'已落盘源图不符')
        relative.append(name)
    return relative


def prepare_student_surfaces(snap, row, ordinal, root, overrides):
    if not overrides:
        return copy_assets(snap,row,'question',ordinal,root), []
    from PIL import Image
    refs = row['assets']['question']
    by_sha = {entry['source_sha256']:entry for entry in overrides}
    check(len(overrides)==len(by_sha) and set(by_sha)<=set(a['sha256'] for a in refs),
          '安全题面必须绑定本题原始图片，没有重复或陌生来源')
    paths, proofs = [], []
    for i, ref in enumerate(refs,1):
        original = snap.path(ref['path'])
        data = original.read_bytes()
        check(digest(data)==ref['sha256'],'题目原图发生变化')
        override = by_sha.get(ref['sha256'])
        if override is None:
            name = f'student/{ordinal:03}/{i:02}'+original.suffix
            dest=root/name; dest.parent.mkdir(parents=True,exist_ok=True)
            copy_verified(original,dest,ref['sha256'])
        else:
            top=override.get('crop_top')
            check(type(top) is int and top>0 and isinstance(override.get('review_basis'),str)
                  and override['review_basis'].strip(),'需有可核的仅去顶部元数据裁切行数和内容审查')
            fixed=Path(override['path'])
            check(fixed.is_absolute() and fixed.is_file() and not fixed.is_symlink(), '缺安全题面PNG')
            payload=fixed.read_bytes()
            check(digest(payload)==override['sha256'],'安全题面字节漂移')
            with Image.open(io.BytesIO(data)) as whole, Image.open(io.BytesIO(payload)) as cropped:
                check(cropped.format=='PNG' and 0<top<whole.height and
                      cropped.size==(whole.width,whole.height-top) and
                      whole.crop((0,top,whole.width,whole.height)).convert('RGBA').tobytes()==cropped.convert('RGBA').tobytes(),
                      '安全图必须是仅裁除原图顶部后的无损原题像素')
            name=f'student/{ordinal:03}/{i:02}.png'
            ensure_bound_file(root/name,payload)
            proofs.append({'canonical_source':ref, 'delivered_sha256':digest(payload),
                           'crop_top':top,'review_basis':override['review_basis']})
        paths.append(name)
    return paths, proofs


def question_recipe_reader(snap):
    prefix = TEACHING_PREFIX
    path = prefix + 'manifest.json'
    if snap._row(path) is None:
        return None
    table = json.loads(snap.text(path)).get('knowledge_hot_routes')
    if table is None:
        return None

    def get_ref(ref):
        name = prefix+ref['ref']
        data = snap.path(name).read_bytes()
        check(digest(data)==ref['sha256'], '同版精确教案引用发生变化')
        return json.loads(data)

    reader = Reader(table, get_ref)
    def read_recipe(q):
        ref = reader.get('question-visual', q)
        if not ref or ref.get('question_identity') != q or not ref.get('ref'):
            return None
        result = get_ref(ref)
        check(result.get('question_identity') == q, '精确教案身份漂移')
        return {**result, '_original_projection': {'path':prefix+ref['ref'],'sha256':ref['sha256']}}
    return read_recipe


def contains_reserved_ref(value, years):
    """Recognize explicit exam/locator provenance without treating an ordinary numeric value as an exam."""
    if not years:
        return False
    if isinstance(value, dict):
        for key, child in value.items():
            if str(key) in {str(y) for y in years}:
                return True
            if key in {'year','exam_year','source_year'} and str(child) in {str(y) for y in years}:
                return True
            if contains_reserved_ref(str(key),years) or contains_reserved_ref(child,years):
                return True
    elif isinstance(value, list):
        return any(contains_reserved_ref(v,years) for v in value)
    elif isinstance(value, str):
        for year in years:
            if re.search(rf'(?:EXAM408[-_/]{year}[-_/]Q|(?:DS|CO|OS|CN)_{year}_\d|/assets/{year}/|{year}\s*年.{{0,12}}(?:真题|第\s*\d+\s*题))',value,re.I):
                return True
    return False


def scoped_recipe(template, q, years):
    """Materialize current-question steps, never a global worked-example bank."""
    check(template.get('question_identity')==q and year_of(q) not in years, '当前精确教案身份或保留年不符')
    retained = {}
    for key in ('question_identity','identity_kind','parent_identity','subject','target','initial_state','given_objects',
                'scene','steps','checks','solution_anchor','visual_kind','source_review_status','template_native_version',
                'source_visual_plan_version','interaction','exam_visual_binding'):
        if key in template and not contains_reserved_ref(template[key],years):
            retained[key] = template[key]
    bridge = template.get('lesson_bridge') or {}
    retained['lesson_bridge'] = {k:bridge[k] for k in ('identity','independent_subquestion','target','initial_state',
             'given_objects','overall_route','ordered_steps','checks','scope_boundaries','answer_protection','delivery',
             'teacher_release_level') if k in bridge and not contains_reserved_ref(bridge[k],years)}
    retained['source_projection'] = template.get('_original_projection')
    retained['scope_policy'] = '仅本问；跨题拓展候选、个人历史例题及其他来源路线不复制。补讲按本次已审内容，在实际日另核可用题源。'
    retained['reserved_materials_filtered'] = True
    check(not contains_reserved_ref(retained,years), '本问投影仍含保留年材料')
    return retained


def inherited_assets(overlay, q, candidate):
    if not overlay.get('source_queue'):
        return None
    root = Path(overlay['source_queue']).resolve(strict=True)
    # Prior queues are read-only study artifacts. Reuse only a bound teacher
    # for this exact item, never its previous admission/ordinal/contact rule.
    binding = read(root/'binding.json')
    guard = source(root,'private/start-gates.json').read_bytes()
    check(binding.get('guard_sha256') == digest(guard), '既有学习队列源绑定漂移')
    record = json.loads(guard).get('selected',{}).get(q)
    if record is None:
        return None
    check(record['image_sha256'] == [r['sha256'] for r in candidate['assets']['question']],
          '复用题面的原图与本次冻结原题不符：'+q)
    teacher_path = root/'private'/q/'teacher.json'
    if not teacher_path.is_file():
        return None
    old = read(teacher_path)
    check(old['true_item_id']==q, '复用教师稿身份不符')
    if binding.get('media_index_sha256'):
        media_bytes = source(root,'private/media-index.json').read_bytes()
        check(digest(media_bytes) == binding['media_index_sha256'], '既有学习队列课件索引漂移')
        indexed = json.loads(media_bytes)[q]
        if indexed.get('teacher'):
            check(digest(teacher_path.read_bytes()) == indexed['teacher']['sha256'], '既有学习队列教师稿漂移')
        media = {kind: {stage:{**ref, 'path':str(source(root, ref['path']))} for stage, ref in indexed[kind].items()}
                 for kind in ('2d','3d')}
        return {'root':str(root), 'lesson':old.get('authored_lesson'), 'media':media}
    vis = old.get('visual_artifacts', {})
    media = {}
    for kind, rows in [('2d',vis.get('two_d',{})),('3d',vis.get('three_d',{}).get('local_stage_override',{}))]:
        media[kind] = {s:{'path':str(source(root/'private/visuals', v['path'])), 'sha256':v['sha256']}
                       for s,v in rows.items()}
    return {'root':str(root), 'lesson':{k:old[k] for k in ('mainline','interaction','scope_check','source_check') if k in old},
            'media':media}


def write_media(root, q, supplied_2d, supplied_3d, old_media):
    result={}
    for kind, supplied, valid in [('2d',supplied_2d,{'hint-1','solution'}),('3d',supplied_3d,{'question','hint-1','solution'})]:
        chosen = {**old_media.get(kind, {}), **supplied.get(q,{})}
        check(set(chosen) <= valid,'补充课件含未知阶段：'+q)
        result[kind] = {}
        for stage, ref in chosen.items():
            path = Path(ref['path'])
            check(path.is_absolute() and path.is_file() and not path.is_symlink() and path.suffix.lower()=='.html',
                  '需实际可读的本地独立课件：'+q+'/'+stage)
            check(path.stat().st_size > 0, '预制课件为空：'+q+'/'+stage)
            name = f'private/{q}/{stage}-{kind}.html'
            (root/name).parent.mkdir(parents=True,exist_ok=True)
            copy_verified(path,root/name,ref['sha256'])
            result[kind][stage]={'path':name,'sha256':ref['sha256']}
    return result


def student_source_notes(lesson, candidate):
    """Carry only explicitly reviewed source errata, bound to this question."""
    notes=(lesson or {}).get('student_source_notes',[])
    check(isinstance(notes,list) and len(notes)<=4,'题源说明必须为有界数组')
    hashes={row['sha256'] for row in candidate['assets']['question']}
    result=[]
    for note in notes:
        check(isinstance(note,dict) and set(note)=={'text','source_sha256','review_basis'}
              and note['source_sha256'] in hashes
              and isinstance(note['text'],str) and 0<len(note['text'].strip())<=500
              and isinstance(note['review_basis'],str) and note['review_basis'].strip(),
              '题源勘误须绑定本题原图并给具体复核依据')
        result.append(note['text'])
    return result


def prepare_queue(workspace, overlays, *, study_date, repo=REPO, details=DETAILS, bridge_root=BRIDGE_ROOT):
    raw = source(workspace,'pool.json').read_bytes()
    frozen = json.loads(raw)
    check(workspace.name == digest(raw) and frozen.get('mode') == MODE, '不是本次登记的年度代表题候选池')
    context = source(workspace,'preparation-evidence/context.json')
    check(digest(context.read_bytes())==frozen['evidence_sha256'],'年度证据索引漂移')
    evidence, _ = preparation_evidence(context,frozen['date'],annual_all=True)
    closeout = evidence['selection_scope'].get('daily_closeout')
    check(closeout == frozen.get('daily_closeout'), '日终专项身份与冻结证据不符')
    if closeout is not None and closeout.get('studied_exam_year') is not None:
        check(closeout['studied_exam_year'] + 1 in frozen['excluded_years'], '日终专项缺少下一年度真题保护')
    decision = read(overlays)
    check(decision.get('input_version') == workspace.name, '最终选题须绑定本次完整候选池')
    day = dt.date.fromisoformat(study_date)
    check(day >= dt.date.fromisoformat(frozen['date']), '不能将年度代表题的资格核验回填到候选冻结日以前')
    proof = prepare_hot_capture_contact_proof(repo, study_date)
    usable, priorities, excluded = scope_payload(frozen, day, evidence, repo, hot_proof=proof)
    ids,anchors = verify_selection(frozen,decision,priorities)
    check(set(ids)<=usable.keys(),'最终选择涉及学习日新保留的真题或给定答案依赖')
    hits=final_hits(decision,frozen)
    narrow=bound_overlay(decision,'narrow_scope_readback')
    assessed = {q:[] for q in ids}
    if narrow is not None:
        seen=set()
        for item in narrow['items']:
            q,t=item['true_item_id'],item['target_id']
            check(q in assessed and t in frozen['targets'] and (q,t) not in seen
                  and item['mode'] in ('full','partial','teaching_only') and item.get('teaching_boundary'),
                  '最终本轮题/窄机制/已考边界不符：'+q+'/'+t)
            check(item['mode']=='teaching_only' or t in frozen['candidates'][q]['candidate_target_ids'],
                  '不能把未命中题当作该机制的独立检测：'+q+'/'+t)
            seen.add((q,t));assessed[q].append(item)
        check({t for _,t in seen}==set(frozen['targets']), '最终窄机制报告须交代每个真实薄弱点')
    two_d=(bound_overlay(decision,'prepared_2d') or {}).get('rows',{})
    three_d=(bound_overlay(decision,'prepared_3d') or {}).get('rows',{})
    lessons=(bound_overlay(decision,'teacher_lessons') or {}).get('rows',{})
    surfaces=(bound_overlay(decision,'student_surface_overrides') or {}).get('rows',{})
    after_class=after_class_targets(decision,frozen,ids)
    # Preserve the proposed ordering, except that a tested prerequisite must
    # already have been presented before another item gives away its answer.
    before={q:set(frozen['candidates'][q]['contact_dependency_ids']).intersection(ids) for q in ids}
    order=[]
    while len(order)<len(ids):
        next_q=next((q for q in ids if q not in order and before[q]<=set(order)),None)
        check(next_q is not None, '已选择的题存在循环答案依赖')
        order.append(next_q)
    fingerprint=digest(json.dumps([MODE,workspace.name,decision,order,study_date,proof,'prepared-question-media-v1'],ensure_ascii=False,
                                  sort_keys=True).encode())
    label = '日终错点代表题' if closeout is not None else '年度弱点代表题'
    hot=details/'learning-deliveries/专项'/(label+'-'+study_date+'-'+fingerprint[:18])
    hot.parent.mkdir(parents=True,exist_ok=True)
    with open_exam_sources(repo, details, bridge_root, frozen=frozen) as (snap, _, _):
        recipe_for=question_recipe_reader(snap)
        for q, reviews in hits.items():
            for tag, review in reviews.items():
                ref = review.get('source_ref') or {}
                name = ref.get('path', '')
                published = name if name.startswith('details/') else 'details/' + name
                check(ref.get('question_id')==q and Path(published).stem==q,
                      '真题核读注解所引题号不符：'+q+'/'+tag)
                bound = snap._row(published)
                if bound:
                    actual_sha = bound['sha256']
                else:
                    # Canonical parent Markdown cards may legitimately live only
                    # in the native corpus. The queue's published image bytes are
                    # bound separately; never label this native card as published.
                    relative = published.removeprefix('details/')
                    check(relative.startswith('corpus/408统考真题/'), '未登记的题源原文引用：'+q)
                    actual_sha = digest(source(details, relative).read_bytes())
                check(actual_sha==ref.get('sha256'), '全库实核注解的真题源内容变化：'+q+'/'+tag)
        with tempfile.TemporaryDirectory(dir=hot.parent,prefix='.annual-') as tmp:
            out=Path(tmp)/'content';out.mkdir()
            queue=[]; material_status={}; adopted={}; media_index={}; image_hashes={}
            for i,q in enumerate(order,1):
                c=frozen['candidates'][q]
                images, safe_proofs=prepare_student_surfaces(snap,c,i,out,surfaces.get(q,[]))
                image_hashes[q]=[digest((out/name).read_bytes()) for name in images]
                solutions=copy_assets(snap,c,'solution',i,out)
                item={'ordinal':i,'true_item_id':q,'question_images':images}
                if '-P' in q:
                    item['public_question_text']=c['question_text']
                check(images or item.get('public_question_text'), '该命中题的已核题面不可用，保留候选待补：'+q)
                queue.append(item)
                adopted[q]={t:{'codes':selected_codes(c,t,frozen['targets']),'route':priorities[t]['route'],
                               'tested_mechanism':hits.get(q,{}).get(t,{}).get('level','point_linked_not_independently_verified'),
                               'exam_source_role':hits.get(q,{}).get(t,{}).get('point_role'),
                               'fallback_contact_date':priorities[t]['fallback_contact_date']} for t in anchors[q]}
                old=inherited_assets(decision,q,c)
                inherited_media = old['media'] if old else {}
                if safe_proofs:
                    # A previous hint or native player may embed the revealing
                    # header even when the new public question itself is safe.
                    inherited_media = {kind:{s:r for s,r in files.items() if s=='solution'}
                                       for kind,files in inherited_media.items()}
                art=write_media(out,q,two_d,three_d,inherited_media)
                for kind, stages in art.items():
                    if 'hint-1' in stages and 'solution' in stages:
                        check(stages['hint-1']['sha256'] != stages['solution']['sha256'],
                              '提示和完整图解须使用各自的课件：'+q+' / '+kind)
                native={s:get_visual_pointer(repo,q,stage='hint' if s=='hint-1' else s,hint_level=1)
                        for s in ('question','hint-1','solution')}
                if safe_proofs:
                    for initial in ('question','hint-1'):
                        native[initial]={'status':'protected_header_requires_safe_stage'}
                media_index[q] = {'2d':art['2d'],'3d':art['3d'],
                                  'native_3d':{stage:{key:record.get(key) for key in ('status','cache_key','lesson_revision')}
                                               for stage,record in native.items()}}
                recipe=recipe_for(q) if recipe_for else None
                private=out/'private'/q;private.mkdir(parents=True,exist_ok=True)
                if recipe:
                    write(private/'source-teaching-template.json',scoped_recipe(recipe,q,excluded))
                lesson=lessons.get(q) or (old['lesson'] if old else None)
                if lesson:
                    check(isinstance(lesson,dict) and lesson.get('true_item_id',q)==q and lesson.get('mainline'),'本题成稿为空或身份不符：'+q)
                    check(not contains_reserved_ref(lesson,excluded), '本问已选，只修教师成稿中引用的保留年例题：'+q)
                    notes=student_source_notes(lesson,c)
                    if notes:
                        item['public_source_notes']=notes
                check(c['source_status'] == 'verified_references' and images and solutions and lesson,
                      '专项仍缺本题原图、已核解析或教师主线：'+q)
                check('solution' in art['2d'] or 'solution' in art['3d'],
                      '专项仍缺随交接保存的本题完整图解：'+q)
                check('hint-1' in art['2d'] or 'hint-1' in art['3d'] or native['hint-1'].get('status') == 'available',
                      '专项仍缺本题可用预制图解：'+q+' / hint-1')
                check(not contains_reserved_ref(after_class[q],excluded), '本问题后补讲引用了保留年份例题：'+q)
                material_status[q]={'source_gaps':frozen['source_gaps'].get(q,[]),
                        'ready_2d_stages':sorted(art['2d']), 'source_teaching_template':bool(recipe),
                        'ready_3d_stages':[s for s in native if s in art['3d'] or native[s].get('status')=='available'],
                        'authored_explanation':bool(lesson)}
                if decision.get('require_ready_media') is True:
                    missing = list(material_status[q]['source_gaps'])
                    if not images:
                        missing.append('student_question')
                    if not solutions:
                        missing.append('teacher_solution')
                    missing += ['2d:'+stage for stage in ('hint-1','solution') if stage not in art['2d']]
                    missing += ['3d:'+stage for stage in ('question','hint-1','solution')
                                if stage not in material_status[q]['ready_3d_stages']]
                    check(not missing, '保留已选题，仅补安全课件或来源：'+q+' / '+', '.join(missing))
                write(private/'teacher.json',{'true_item_id':q,'source_commit':snap.commit,'primary_point':c['primary_point'],
                        'assessed_points':c['assessed_points'],'selected_for':adopted[q],'selection_reason':decision['selection_reasons'][q],
                        'incidental_targets':sorted(set(c['candidate_target_ids'])-set(anchors[q])-{v['target_id'] for v in after_class[q]}),
                        'after_class':after_class[q],
                        'personal_evidence':{t:frozen['targets'][t] for t in c['candidate_target_ids']},
                        'mechanism_reviews':hits.get(q,{}),'selected_narrow_assessment':assessed[q],'original_solution_sources':solutions,
                        'student_image_proofs':safe_proofs,'canonical_question_assets':c['assets']['question'],
                        'source_scope':'独立小问的原参考答案可能是父级整题，仅在教师侧核对本问。不能向学生展示整页答案。',
                        'source_template':'source-teaching-template.json' if recipe else None,'authored_lesson':lesson,
                        'authored_lesson_origin':'current' if q in lessons else ('previous_same_question' if lesson else 'source_materials_only'),
                        'media':art,'native_3d':native})
                media_index[q]['teacher'] = {'path': f'private/{q}/teacher.json',
                                              'sha256': digest((private/'teacher.json').read_bytes())}
            write(out/'student/queue.json',queue)
            write(out/'private/media-index.json',media_index)
            # Store all matching identities (no cold paths) so a later study
            # day rechecks the *same target's* alternatives, not only the item.
            compact={q:{'candidate_target_ids':c['candidate_target_ids'],'contact_dependency_ids':c['contact_dependency_ids']}
                     for q,c in frozen['candidates'].items()}
            guard={'mode':MODE,'excluded_years':frozen['excluded_years'],'candidates':compact,
                   'recent_contact_overrides':evidence.get('recent_contact_overrides',[]),'hot_capture_proof':proof,
                   'selected':{q:{'targets':anchors[q],'image_sha256':image_hashes[q]}
                               for q in ids}}
            write(out/'private/start-gates.json',guard)
            state='PREPARED_NOT_STARTED' if order else 'NO_REPRESENTATIVE_SELECTED'
            write(out/'binding.json',{'schema':'cs408-annual-weak-representative-queue-v1','mode':MODE,'status':state,
                                      'input_version':workspace.name,'source_commit':snap.commit,'date':study_date,'source_years':frozen['source_years'],
                                      'queue_sha256':digest((out/'student/queue.json').read_bytes()),
                                      'guard_sha256':digest((out/'private/start-gates.json').read_bytes()),
                                      'media_index_sha256':digest((out/'private/media-index.json').read_bytes()),'learner_event_count':0})
            if narrow is not None:
                write(out/'private/narrow-scope-readback.json',narrow)
            write(out/'private/selection-audit.json',{'mode':MODE,'selected_count':len(order),'max_selected':MAX_SELECTED,
                  'selected_for':adopted,'selection_reasons':decision.get('selection_reasons',{}),
                  'not_represented':sorted(set(frozen['targets'])-{t for ts in anchors.values() for t in ts}),
                  **({'daily_closeout':closeout, 'max_per_target':DAILY_CLOSEOUT_MAX_PER_TARGET,
                      'unrepresented_target_reasons':decision.get('unrepresented_target_reasons', {})} if closeout is not None else {}),
                  'after_class_targets':{q:[v['target_id'] for v in after_class[q]] for q in order if after_class[q]},
                  'no_selection_reason':decision.get('no_selection_reason'),'source_inventory':frozen['inventory'],
                  'materials':material_status, 'current_reserved_years':excluded,'learning_started':False})
            write(out/'START_STUDY.md',(
                f'本机{label if closeout is not None else "年度错点代表题"}已准备，共{len(order)}题。只有明确开始才逐题展示。每次首次展示前运行 '
                f'python3 scripts/local_focused_training_408.py next --queue "{hot}" --ordinal <真实题序>。'
                '先按当前已明确保留的年份排除原题和给定答案依赖，再对采用的各个薄弱点重查真实接触；'
                '有满14天的先选该层，其次无记录，全部近期才选最近接触中距今最远者。日期变化仅处理受影响的题。'
                '只展示当前原题图及必要的独立小问题设；若next返回public_source_notes，须同时原样给出已核题源说明。内部保留next返回的teaching_context。'
                '答后使用question-worker和当前正式小点摘要。按获准阶段调用teaching_context的media入口，携带本题true_item_id及binding_sha256。'
                '只使用本问该阶段的预制图解，完整讲解获准时同时读取本题私有教案。'
                '被给作已知输入的前问若也列入本次训练，须先完成前问；复习接触、提示后完成与独立掌握分别记录。'
                '原生训练队列、旧题序和已保存作答不因新建专项而改变。\n'+LOCAL_STUDY_FOLLOWUP+'\n'
                if order else '本次没有选出具备额外诊断价值的代表题。详见私有选题审计，不启动学习。\n'))
            inventory={str(p.relative_to(out)):digest(p.read_bytes()) for p in out.rglob('*') if p.is_file()}
            if not hot.exists():
                try:
                    os.rename(out,hot)
                except OSError:
                    if not hot.is_dir():
                        raise
            check(all((hot/name).is_file() and digest((hot/name).read_bytes())==sha for name,sha in inventory.items()),
                  '热交接落盘内容不符，保留现有进度并查明变动')
    return {'status':state,'handoff':str(hot/'START_STUDY.md'),'question_count':len(order),
            'prepared_visual_question_count':len(order),'mode':MODE, 'learning_started':False,'learner_event_count':0}


def next_item(hot, ordinal, day, *, repo=REPO):
    binding=read(hot/'binding.json')
    check(binding.get('mode')==MODE,'需要年度弱点代表题队列')
    guard_path=source(hot,'private/start-gates.json')
    queue_path=source(hot,'student/queue.json')
    check(digest(guard_path.read_bytes())==binding['guard_sha256'] and digest(queue_path.read_bytes())==binding['queue_sha256'],
          '冻结年度热队列绑定不符')
    guard=read(guard_path); queue=read(queue_path)
    check(1<=ordinal<=len(queue),'当前没有这个已安排的训练序号')
    row=queue[ordinal-1]; q=row['true_item_id']; selected=guard['selected'][q]
    usable, priorities, _=scope_payload(guard,day,guard,repo,hot_proof=guard['hot_capture_proof'])
    check(q in usable, '该问或给定答案依赖命中当前明确保留年份，仅当前项待调整')
    check(all(q in priorities.get(t,{}).get('eligible_ids',[]) for t in selected['targets']),
          '当前题的原采用考点中已有日期层变化，仅重核当前项；失去代表资格的考点只能兼带，不能沿旧主因出题')
    paths=[source(hot,name) for name in row['question_images']]
    check([digest(p.read_bytes()) for p in paths]==selected['image_sha256'],'学生原题面发生变化')
    return {**row,'question_images':[str(p) for p in paths],'learner_event_count':0,
            **prepared_teaching_route(hot, binding, ordinal, q)}


def read_media(hot, ordinal, stage, *, day, repo=REPO, expected_question_id=None, expected_binding_sha256=None):
    check(stage in {'question','hint-1','solution'}, '需要单个明确获准的课件阶段')
    redirect = replacement_notice(hot)
    if redirect:
        return redirect
    binding_raw = source(hot,'binding.json').read_bytes()
    if expected_binding_sha256 is not None:
        check(digest(binding_raw)==expected_binding_sha256, '课件与当前出题交接的SHA不符')
    binding = json.loads(binding_raw)
    check(binding.get('media_index_sha256'),'该冻结队列没有绑定阶段课件索引')
    raw = source(hot,'private/media-index.json').read_bytes()
    queue = source(hot,'student/queue.json').read_bytes()
    guard = source(hot,'private/start-gates.json').read_bytes()
    check(digest(raw)==binding['media_index_sha256'] and digest(queue)==binding['queue_sha256']
          and digest(guard)==binding['guard_sha256'],'阶段课件索引绑定变化')
    rows = json.loads(queue); guard = json.loads(guard)
    check(1<=ordinal<=len(rows), '课件题序越界')
    q = rows[ordinal-1]['true_item_id']
    if expected_question_id is not None:
        check(q == expected_question_id, '请求课件的题号与当前展示不符')
    if binding.get('mode') == MODE:
        excluded = guard['excluded_years']
        dependencies = guard['candidates'][q]['contact_dependency_ids']
    else:
        excluded = guard['selection_scope']['excluded_exam_years']
        dependencies = guard['selected'][q]['dependencies']
    reserved=reserved_years(repo,day.isoformat(),excluded)
    check(not any(year_of(k) in reserved for k in [q,*dependencies]),
          '本问涉及当前明确保留年份，不能进入课件')
    if stage == 'question':
        # A public 3D question preview is also a way to see a NEW question.
        # Hint/solution for a genuinely displayed item must remain accessible.
        if binding.get('mode') == MODE:
            next_item(hot, ordinal, day, repo=repo)
        else:
            from local_focused_training_408 import next_item as focused_next
            focused_next(hot, ordinal, day=day, repo=repo)
    media = json.loads(raw)[q]
    result = {'true_item_id':q,'stage':stage,'learner_event_count':0}
    for kind, field in [('2d','interactive_2d'),('3d','interactive_3d')]:
        ref = media[kind].get(stage)
        if ref:
            actual = source(hot,ref['path'])
            check(digest(actual.read_bytes())==ref['sha256'], '该阶段课件哈希漂移')
            result[field]={'path':str(actual),'sha256':ref['sha256']}
    legacy_teacher = hot / f'private/{q}/teacher.json'
    if stage == 'solution' and (media.get('teacher') or legacy_teacher.is_file()):
        ref = media.get('teacher') or {'path': f'private/{q}/teacher.json'}
        path = source(hot,ref['path'])
        teacher_raw = path.read_bytes()
        teacher_sha = digest(teacher_raw)
        check(teacher_sha == ref.get('sha256', teacher_sha), '当前题私有教案哈希漂移')
        teacher = json.loads(teacher_raw)
        check(teacher['true_item_id'] == q and (binding.get('mode') != MODE or
              teacher.get('source_commit') == binding.get('source_commit')), '当前题私有教案身份或题源版本不符')
        result['teacher_ref'] = {'path': str(path), 'sha256': teacher_sha}
    if not media['3d'].get(stage) and (stage == 'question' or 'interactive_2d' not in result):
        frozen=media['native_3d'][stage]
        if frozen.get('status') == 'protected_header_requires_safe_stage':
            result['native_3d_status']='本阶段须使用去除题库标签的安全课件，不回退原播放器'
            return result
        actual=get_visual_pointer(repo,q,stage='hint' if stage=='hint-1' else stage,hint_level=1)
        if (actual.get('status')=='available' and actual.get('cache_key')==frozen.get('cache_key')
                and actual.get('lesson_revision')==frozen.get('lesson_revision')):
            result['native_3d']={k:actual[k] for k in ('question_id','stage','hint_level','cache_key','lesson_revision')}
            result['native_3d']['stage_request'] = {'question_id':q, 'stage':actual['stage'], 'hint_level':actual['hint_level']}
            # The native viewer homepage always opens at the public question.
            # Only an authorized dispatcher may request a later stage.
            if stage == 'question':
                result['native_3d']['question_viewer_url'] = actual.get('viewer_url')
        else:
            result['native_3d_status']='本阶段需补核原生课件或绑定本问修正版'
    return result


def replacement_notice(hot):
    root = hot.resolve()
    if not (root.parent / '.unstarted-replacements').is_dir():
        return None
    old = read(root / 'binding.json')
    fingerprint = old['queue_sha256']
    check(re.fullmatch(r'[a-f0-9]{64}', fingerprint) is not None, '旧队列原始身份无效')
    path = root.parent / '.unstarted-replacements' / (digest(str(root).encode())+'-'+fingerprint+'.json')
    if not path.exists():
        return None
    change = read(path)
    check(change['previous'] == str(root) and change['previous_input_version'] == old['input_version']
          and fingerprint == digest(source(root, 'student/queue.json').read_bytes()), '旧未开专项迁接绑定不符')
    target = Path(change['replacement'])
    check(target.parent == root.parent and target.resolve() == target and target != root, '迁接只允许同一热根的独立专项')
    new = read(target/'binding.json')
    check(new.get('mode') == MODE and change['replacement_input_version'] == new['input_version'] and
          change['replacement_queue_sha256'] == new['queue_sha256'] == digest(source(target,'student/queue.json').read_bytes()),
          '新专项身份漂移')
    start = source(target,'START_STUDY.md')
    check(change['replacement_handoff_sha256']==digest(start.read_bytes()), '新学习入口漂移')
    return {'status':'SUPERSEDED_UNSTARTED','next_action':'use_replacement_handoff',
            'replacement_handoff':str(start),'question_count':len(read(target/'student/queue.json')),'learner_event_count':0}


def activate_unstarted(previous, hot, proof_file, *, repo=REPO, details=DETAILS):
    previous, hot = previous.resolve(), hot.resolve()
    expected_parent = (details/'learning-deliveries/专项').resolve()
    check(previous.parent == hot.parent == expected_parent and previous != hot, '只允许同一独立专项热根，不迁移D会话')
    old, new = read(previous/'binding.json'), read(hot/'binding.json')
    proof_raw = proof_file.read_bytes()
    proof = json.loads(proof_raw)
    check(old.get('status')=='PREPARED_NOT_STARTED' and old.get('learner_event_count')==0
          and new.get('mode')==MODE and new.get('status') in ('PREPARED_NOT_STARTED','NO_REPRESENTATIVE_SELECTED'),
          '仅可切换已核未开学的旧专项')
    years = old.get('source_years')
    if not years:
        legacy = re.fullmatch(r'focused_(\d{4})_gaps', old.get('queue_kind',''))
        years = [int(legacy.group(1))] if legacy else None
    check(old['date']==new['date'] and years and sorted(years)==sorted(new['source_years'])==sorted(proof['source_question_years']),
          '旧新专项必须指向同一学习日期和年度卷范围')
    check(proof.get('schema')=='cs408-unstarted-queue-replacement-proof-v1'
          and proof.get('previous_input_version')==old['input_version']
          and proof.get('previous_queue_sha256')==old['queue_sha256']
          and proof.get('replacement_input_version')==new['input_version']
          and type(proof.get('real_display_count')) is int and proof['real_display_count']==0
          and type(proof.get('real_capture_count')) is int and proof['real_capture_count']==0
          and isinstance(proof.get('review_basis'),str) and proof['review_basis'].strip(),'需要同批真实未开始核验')
    for root, binding in ((previous,old),(hot,new)):
        check(digest(source(root,'student/queue.json').read_bytes())==binding['queue_sha256'],'题序已变，不迁移')
    existing = replacement_notice(previous)
    if existing:
        check(existing['replacement_handoff'] == str(hot/'START_STUDY.md'), '旧专项已绑定其他后继')
        return existing
    today = dt.datetime.now(ZoneInfo('Asia/Shanghai')).date()
    contacts = fact_table(repo,today,{})
    for row in read(previous/'student/queue.json'):
        date = (contacts.get(row['true_item_id']) or {}).get('study_date')
        check(not date or date<old['date'],'旧专项题自准备日已有真实学习/展示，不自动换活动队列：'+row['true_item_id'])
    record = {'schema':'cs408-unstarted-annual-replacement-v1','previous':str(previous),'replacement':str(hot),
              'previous_input_version':old['input_version'],'previous_queue_sha256':old['queue_sha256'],
              'replacement_input_version':new['input_version'],'replacement_queue_sha256':new['queue_sha256'],
              'replacement_handoff_sha256':digest(source(hot,'START_STUDY.md').read_bytes()),'verification':proof,
              'verification_sha256':digest(proof_raw),'learner_event_count':0}
    name=digest(str(previous).encode())+'-'+old['queue_sha256']+'.json'
    ensure_bound_file(expected_parent/'.unstarted-replacements'/name,
                      json.dumps(record,ensure_ascii=False,sort_keys=True).encode())
    return replacement_notice(previous)
