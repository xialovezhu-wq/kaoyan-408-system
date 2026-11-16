"""Teacher-only joins of exact steps, current memory and bounded exam scope.

This module builds derived advice only. It never writes learning facts, loads a
neighboring question payload, or creates a student-ready scene. Lexical alignment
is an inspectable candidate, never a claim of semantic equivalence.
"""
from __future__ import annotations

import copy
import hashlib
import json
from pathlib import Path
import re

SCHEMA = 'cs408-lesson-bridge-v1'
ASSESSMENT_REL = Path('support/syllabus-2027/question-syllabus-map.json')
RELATIONS_REL = Path('support/syllabus-2027/reviewed-teaching-relations.json')
_ALLOWED_SCOPE = {'explicit', 'within_scope'}
_POINT = re.compile(r'(?:DS|CO|OS|CN)\d{2}-\d{2}')
# Only concrete representations of relations/state warrant local visual help.
_RELATION = re.compile(r'地址|页表|映射|缺页|偏移|指针|链|树|图|队列|栈|状态|转换|转移|输入|输出|寄存器|字段|分组|组号|选组|通路|信号|流水|时序|缓冲|窗口|磁盘|分配|回收|中断|进程|线程|锁|同步|分支|遍历|排序|访问|网络|协议|控制|位宽|移位')
_GENERIC = {'当前', '题目', '本题', '问题', '步骤', '对应', '条件', '对象', '结果', '关系', '判断', '根据', '先把', '检查', '计算', '比较', '最后', '说明', '理解', '操作', '变化', '使用', '分别', '确定', '本问', '返回'}


def _text(value):
    return value.strip() if isinstance(value, str) else ''


def _tokens(text):
    result = set(re.findall(r'[A-Za-z][A-Za-z0-9_]{1,}', text.lower()))
    for word in re.findall(r'[\u4e00-\u9fff]+', text):
        result.update(word[i:i + 2] for i in range(len(word) - 1))
    return result - _GENERIC


def _norm_focus(value):
    return re.sub(r'\s+|[，。；、：:（）()－—-]', '', value).lower()


def _scope(context):
    context = context or {}
    return {key: copy.deepcopy(context[key]) for key in
            ('scope_status', 'teaching_boundary', 'teaching_guard', 'paths', 'syllabus_year')
            if key in context}


def _syllabus_ids(context):
    return {row['id'] for row in context.get('syllabus_entries', []) if row.get('id')}


def _point_relations(repo_root, contexts, by_point):
    """Official outline navigation or a reviewed conceptual relationship.

    Outline ancestry is not canonical-concept/software-layer containment.
    Sharing a chapter, leaf heading, word or exam proves neither kind of edge.
    """
    eligible = {code: context for code, context in contexts.items()
                if _POINT.fullmatch(code) and context.get('scope_status') in _ALLOWED_SCOPE}
    relations = {code: [] for code in eligible}
    for source, left in eligible.items():
        for target, right in eligible.items():
            if source == target or target not in by_point:
                continue
            proofs = []
            for a in left.get('syllabus_entries', []):
                for b in right.get('syllabus_entries', []):
                    if not _text(a.get('title')) or not _text(b.get('title')):
                        continue
                    ap = [row.get('id') for row in a.get('path', [])]
                    bp = [row.get('id') for row in b.get('path', [])]
                    if a.get('id') and a['id'] != b.get('id') and a['id'] in bp:
                        proofs.append(('syllabus_child', len(bp) - bp.index(a['id']) - 1,
                                       a['id'], b['id'], a['title'], b['title']))
                    if b.get('id') and b['id'] != a.get('id') and b['id'] in ap:
                        proofs.append(('syllabus_parent', len(ap) - ap.index(b['id']) - 1,
                                       a['id'], b['id'], a['title'], b['title']))
            if not proofs or len({row[0] for row in proofs}) != 1:
                continue
            relation, distance, sid, tid, source_title, target_title = min(proofs, key=lambda row: row[1:])
            relations[source].append({'source_point': source, 'target_point': target,
                'relation': relation, 'distance': distance,
                'relationship_basis': 'official_syllabus_ancestry',
                'source_syllabus_entry': {'id': sid, 'title': source_title},
                'target_syllabus_entry': {'id': tid, 'title': target_title},
                'canonical_containment_inferred': False,
                'syllabus_ids': [sid, tid], 'source_sha256': left.get('syllabus_source_sha256'),
                'basis': '官方纲目导航：从“' + source_title + '”到其'
                    + ('下级' if relation == 'syllabus_child' else '上级') + '纲目“' + target_title
                    + '”。只表示考纲条目的编排，不证明两个规范知识点存在概念或软件层次包含关系；须核对目标考法所属具体纲目。'})
    path = Path(repo_root) / RELATIONS_REL if repo_root else None
    if not path or not path.is_file():
        return relations, None
    raw = path.read_bytes()
    data = json.loads(raw)
    if data.get('schema') != 'cs408-reviewed-teaching-relations-v1' or not isinstance(data.get('relations'), list):
        raise ValueError('lesson_bridge_reviewed_relations_schema')
    hashes = {}
    for index, row in enumerate(data['relations']):
        source, target = row.get('source_point'), row.get('target_point')
        if (source not in eligible or target not in eligible or source == target or target not in by_point
                or row.get('relation') not in {'parent', 'child', 'related'}
                or row.get('review_status') != 'reviewed' or not _text(row.get('basis'))):
            raise ValueError('lesson_bridge_reviewed_relation_invalid:' + str(index))
        refs = row.get('source_refs')
        if not isinstance(refs, list) or not refs:
            raise ValueError('lesson_bridge_relation_sources_required:' + str(index))
        for ref in refs:
            p = Path(ref.get('path', ''))
            p = p if p.is_absolute() else Path(repo_root) / p
            if not p.is_file() or not _text(ref.get('locator')):
                raise ValueError('lesson_bridge_relation_source_missing:' + str(index))
            if str(p) not in hashes:
                hashes[str(p)] = hashlib.sha256(p.read_bytes()).hexdigest()
            if hashes[str(p)] != ref.get('sha256'):
                raise ValueError('lesson_bridge_relation_source_hash_drift:' + str(index))
        supported = {(item['exam_id'], item['part_id']) for item in by_point[target]}
        selected = row.get('target_assessments')
        if (not isinstance(selected, list) or not selected or
                any((item.get('exam_id'), item.get('part_id')) not in supported for item in selected)):
            raise ValueError('lesson_bridge_relation_unassessed_target:' + str(index))
        relations[source].append({**copy.deepcopy(row), 'distance': 1,
            'relationship_basis': 'reviewed_source_relation_not_official_taxonomy',
            'relation_source': {'path': RELATIONS_REL.as_posix(), 'sha256': hashlib.sha256(raw).hexdigest(),
                                'locator': 'relations[' + str(index) + ']'}})
    return relations, hashlib.sha256(raw).hexdigest()


def load_assessment_catalog(repo_root, syllabus_contexts):
    """One cold read; retain focus and provenance, never answers or full items."""
    empty = {'by_point': {}, 'by_identity': {}, 'formal_aliases': {},
             'exact_scopes': {}, 'source_sha256': None}
    path = Path(repo_root) / ASSESSMENT_REL if repo_root else None
    if path is None or not path.is_file():
        return empty
    raw = path.read_bytes()
    data = json.loads(raw)
    if data.get('schema') != 'cs408-question-syllabus-map-v1':
        raise ValueError('lesson_bridge_assessment_schema')
    digest = hashlib.sha256(raw).hexdigest()
    by_point, by_identity, exact_scopes = {}, {}, {}
    for identity, row in sorted(data.get('exam_items', {}).items()):
        if not re.fullmatch(r'EXAM408-\d{4}-Q\d{2}', identity):
            continue
        excluded = set(row.get('prerequisite_points', [])) | set(row.get('context_only_points', []))
        assessed = list(dict.fromkeys(row.get('main_knowledge', []) + row.get('hit_knowledge', [])))
        exact_scopes[identity] = {
            'identity': identity, 'assessed_points': [code for code in assessed if code not in excluded],
            'syllabus_ids': sorted({sid for part in row.get('assessment_parts', [])
                                    for sid in part.get('syllabus_ids', [])}),
        }
        for index, part in enumerate(row.get('assessment_parts', [])):
            focus, part_id = _text(part.get('focus')), _text(part.get('part_id'))
            if not focus or not part_id:
                continue
            for code in part.get('canonical_points', []):
                context = syllabus_contexts.get(code) or {}
                if code in excluded or context.get('scope_status') not in _ALLOWED_SCOPE:
                    continue
                # A focus must be tied to a reviewed official entry for this
                # same point; a title/keyword coincidence is insufficient.
                syllabus_ids = sorted(set(part.get('syllabus_ids', [])) & _syllabus_ids(context))
                if not syllabus_ids:
                    continue
                item = {'exam_id': identity, 'part_id': part_id, 'focus': focus,
                    'knowledge_point': code, 'syllabus_ids': syllabus_ids,
                    'teaching_boundary': context.get('teaching_boundary'),
                    'source': {'path': ASSESSMENT_REL.as_posix(), 'sha256': digest,
                        'locator': f'exam_items.{identity}.assessment_parts[{index}]'},
                    'use': 'assessment_focus_only_not_question_recommendation'}
                by_point.setdefault(code, []).append(item)
                by_identity.setdefault(identity, []).append(item)
    for identity, row in data.get('subquestions', {}).items():
        parent = row.get('parent_exam_id') or row.get('parent_true_item_id')
        if row.get('review_status') != 'reviewed' or not re.fullmatch(r'EXAM408-\d{4}-Q\d{2}-P\d{2}', identity):
            continue
        primary = [row['primary_point']] if row.get('primary_point') else []
        exact_scopes[identity] = {
            'identity': identity,
            'assessed_points': list(dict.fromkeys(primary + row.get('assessed_points', []))),
            'syllabus_ids': row.get('syllabus_ids', []),
        }
        suffix = identity.rsplit('-', 1)[-1]
        # The annotation explicitly names the local Pxx. Do not equate a
        # printed subquestion number with a local sequential subquestion ID.
        matches = [item for item in by_identity.get(parent, [])
                   if re.match(re.escape(suffix) + r'(?:$|[:：])', item['part_id'])
                   and item['knowledge_point'] in row.get('assessed_points', [])]
        if matches:
            by_identity[identity] = copy.deepcopy(matches)
    aliases = {key: row['canonical_exam_id'] for key, row in data.get('formal_cards', {}).items()
               if row.get('canonical_exam_id') in by_identity}
    for key, row in data.get('formal_cards', {}).items():
        if not re.fullmatch(r'(?:DS|CO|OS|CN)_(?:\d{4}|UNK)_\d{3}', key):
            continue
        canonical = exact_scopes.get(row.get('canonical_exam_id'), {})
        exact_scopes[key] = {
            'identity': key,
            'assessed_points': canonical.get('assessed_points', row.get('assessed_points', [])),
            'syllabus_ids': canonical.get('syllabus_ids', row.get('syllabus_ids', [])),
        }
    relations, relations_sha = _point_relations(repo_root, syllabus_contexts, by_point)
    return {'by_point': by_point, 'by_identity': by_identity, 'exact_scopes': exact_scopes,
            'formal_aliases': aliases, 'source_sha256': digest,
            'relations': relations, 'relations_sha256': relations_sha,
            'point_contexts': {code: copy.deepcopy(context) for code, context in syllabus_contexts.items()
                               if _POINT.fullmatch(code)}}


def _frequency(context):
    value = (context or {}).get('exam_frequency') or {}
    return {key: copy.deepcopy(value[key]) for key in ('main_frequency_level', 'main_count',
        'secondary_only_count', 'total_count', 'frequency_scope', 'coverage', 'primary_count_policy') if key in value}


def _candidate_order(item):
    frequency = item.get('exam_frequency') or {}
    tier = {'高频': 0, '中频': 1, '低频': 2}.get(frequency.get('main_frequency_level'), 3)
    relation = item.get('knowledge_relation') or {}
    return (tier, 0 if relation.get('relation') in {'parent', 'child', 'syllabus_parent', 'syllabus_child'} else 1,
            relation.get('distance', 0), -len(item.get('shared_focus_terms', [])),
            item.get('knowledge_point', ''), item.get('focus', ''))


def _rank_extension_candidates(candidates):
    """Offer one best representative per target before extra cases of a point.

    Distinct targets retain frequency/relation priority. Repeated cases of one
    high-frequency point cannot consume the slots for another supported target.
    Do not re-sort after diversification: callers truncate this exact order.
    """
    first, repeated, seen = [], [], set()
    for item in sorted(candidates, key=_candidate_order):
        target = item.get('knowledge_point')
        if target in seen:
            repeated.append(item)
        else:
            seen.add(target)
            first.append(item)
    return first + repeated


def _edge_supports_assessment(edge, item):
    if edge.get('relationship_basis') == 'official_syllabus_ancestry':
        target_id = (edge.get('target_syllabus_entry') or {}).get('id')
        if not target_id or target_id not in item.get('syllabus_ids', []):
            return False
    selected = edge.get('target_assessments')
    return not selected or any((target['exam_id'], target['part_id']) == (item['exam_id'], item['part_id'])
                               for target in selected)


def _edge_matches_current_source(edge, current, exact):
    if not exact or edge.get('relationship_basis') != 'official_syllabus_ancestry':
        return True
    source_id = (edge.get('source_syllabus_entry') or {}).get('id')
    return bool(source_id and any(item['knowledge_point'] == edge['source_point']
                                 and source_id in item['syllabus_ids'] for item in current))


def _edge_order(edge):
    # A reviewed mechanism edge must win over a merely structural outline
    # navigation for the same assessed target, regardless of insertion order.
    return (0 if edge.get('relationship_basis') == 'reviewed_source_relation_not_official_taxonomy' else 1,
            edge.get('distance', 0), edge['source_point'])


def _relation_projection(edge):
    return {key: copy.deepcopy(edge[key]) for key in ('source_point', 'target_point', 'relation',
        'distance', 'relationship_basis', 'basis', 'syllabus_ids', 'source_syllabus_entry',
        'target_syllabus_entry', 'canonical_containment_inferred', 'relation_source') if key in edge}


def _extensions(codes, identity, catalog, contexts, *, limit=6):
    catalog = catalog or {}
    # Exact callers carry full contexts; point callers may carry only their
    # current compact context. Cold catalog retains the verified target scope.
    all_contexts = {**catalog.get('point_contexts', {}), **contexts}
    canonical = catalog.get('formal_aliases', {}).get(identity, identity)
    current = copy.deepcopy(catalog.get('by_identity', {}).get(canonical, []))
    allowed = {code for code in codes if (all_contexts.get(code) or {}).get('scope_status') in _ALLOWED_SCOPE}
    current = [item for item in current if item['knowledge_point'] in allowed]
    exact = not _POINT.fullmatch(identity or '') and not (identity or '').startswith(('KSP-', 'S27-'))
    if exact:
        allowed &= {item['knowledge_point'] for item in current}
    current_focus = {_norm_focus(item['focus']) for item in current}
    family = re.sub(r'-P\d{2}$', '', canonical or '')
    targets = {code: [{'source_point': code, 'target_point': code, 'relation': 'same_point',
                      'relationship_basis': 'same_canonical_point', 'distance': 0}]
               for code in allowed}
    for code in sorted(allowed):
        for edge in catalog.get('relations', {}).get(code, []):
            target = edge['target_point']
            if not _edge_matches_current_source(edge, current, exact):
                continue
            if (all_contexts.get(target) or {}).get('scope_status') in _ALLOWED_SCOPE:
                targets.setdefault(target, []).append(edge)
    grouped = {}
    for code, edges in targets.items():
        for item in catalog.get('by_point', {}).get(code, []):
            normalized = _norm_focus(item['focus'])
            if item['exam_id'] == family or normalized in current_focus:
                continue
            usable = [edge for edge in edges if _edge_supports_assessment(edge, item)]
            if not usable:
                continue
            edge = min(usable, key=_edge_order)
            grouped.setdefault((code, normalized), []).append((item, edge))
    pool = []
    current_tokens = _tokens(' '.join(item['focus'] for item in current))
    for values in grouped.values():
        values.sort(key=lambda pair: _edge_order(pair[1]))
        first, edge = values[0]
        # Preserve only provenance and the relationship reason in the hot
        # teacher join; raw PDFs and their paths stay in the source map.
        relation = _relation_projection(edge)
        pool.append({**copy.deepcopy(first), 'teacher_requires_distinctness_check': True,
            'distinctness_status': 'reviewed_relation_and_assessment_still_require_current_mechanism_check',
            'knowledge_relation': relation, 'exam_frequency': _frequency(all_contexts.get(first['knowledge_point'])),
            'supporting_assessments': [{key: copy.deepcopy(item[key]) for key in
                ('exam_id', 'part_id', 'source')} for item, _ in values[:3]],
            'supporting_assessment_count': len(values), 'supporting_assessments_truncated': len(values) > 3,
            'shared_focus_terms': sorted(current_tokens & _tokens(first['focus']))})
    pool = _rank_extension_candidates(pool)
    if exact and not current:
        pool = []
    return {'status': 'candidate_comparison_required' if pool else 'no_supported_extension',
        'current_focus_status': 'exact_assessment_part_bound' if current else 'current_exact_focus_required',
        'current_focus': current, 'candidates': pool[:limit], 'candidates_truncated': len(grouped) > limit,
        'candidate_focus_group_count': len(grouped), 'maximum_selected': 1,
        'release_conditions': ['current_mainline_resolved', 'current_hint_level_permits_extension',
            'current_session_year_and_point_exclusions_allow_target',
            'teacher_verified_direct_relation_and_target_assessment_within_syllabus_boundary'],
        'selection_instruction': '主线解决后优先选择有依据的高频目标，并说明具体考法。parent/child/related是逐源审核的机制关系；syllabus_parent/child只导航所列ID与标题的官方纲目，不能说成两个知识点或软件层的包含关系。官方导航只能接目标纲目实际考过的分面；同一考法优先人工机制证据。已会简带、断点多讲，每次至多一条。',
        'is_old_question_recommendation': False,
        'recommendation_rule': '此处仅说明有来源的考法；实际展示或推荐旧题另走14天实时门禁。',
        'source_sha256': catalog.get('source_sha256'), 'relations_sha256': catalog.get('relations_sha256')}


def _base(identity, kind, codes):
    return {'schema': SCHEMA, 'identity': identity, 'scope': kind,
        'knowledge_points': list(codes), 'teacher_only': True, 'frontend_ready': False,
        'teacher_release_level': 5, 'effectiveness': 'unverified',
        'evidence_status': 'derived_teaching_advice_not_learning_evidence',
        'delivery': '先写获准主线，局部图只解释绑定步骤的对象、关系或状态，随后回到原步骤。',
        'answer_protection': '完整桥接是教师材料；protected不返回。after_attempt不等于开放答案，当前题/Pxx和本轮提示必须在序列化正文及前端前过滤，折叠不算保护。',
        'personal_adaptation': {'status': 'runtime_current_summary_required', 'sources': []}}


def _authored_extension(route, identity, contexts, assessment_catalog):
    """Prefer an actually reviewed condition difference to a text-match pool."""
    authored = (route or {}).get('bounded_extension')
    if not isinstance(authored, dict):
        return None
    code = route.get('parent') or route.get('identity')
    base = _extensions([code], identity, assessment_catalog, contexts)
    base.update(candidates=[], candidates_truncated=False,
                source='source_reviewed_teacher_route', status='reviewed_no_default_extension', reviewed_point=code)
    if authored.get('decision') == 'none_by_default':
        base['reason'] = authored.get('reason')
        return base
    if (contexts.get(code) or {}).get('scope_status') not in _ALLOWED_SCOPE:
        base['reason'] = 'current_scope_does_not_allow_extension'
        return base
    delta = authored.get('condition_delta_and_method_change')
    refs = authored.get('source_refs')
    if not isinstance(delta, str) or not delta.strip() or not isinstance(refs, list) or not refs:
        base['reason'] = 'reviewed_condition_difference_or_source_missing'
        return base
    canonical = (assessment_catalog or {}).get('formal_aliases', {}).get(identity, identity)
    pair = {authored.get('source_question'), authored.get('adjacent_question')} - {None, ''}
    exact = not _POINT.fullmatch(identity or '') and not (identity or '').startswith(('KSP-', 'S27-'))
    # A full-parent comparison is not automatically the independent Pxx's
    # comparison, and an unrelated exact question cannot inherit it by tag.
    if exact and canonical not in pair:
        base['reason'] = 'reviewed_comparison_not_bound_to_current_exact_question'
        return base
    if re.search(r'(?:-|#)P\d+$', identity or '') or re.search(r'(?:-|#)P\d+$', canonical or ''):
        # Solving this Pxx never releases a parent/sibling answer. An authored
        # point-level comparison may retain both, but its exact-question join
        # must obey the same family exclusion as the objective focus pool.
        family = re.sub(r'(?:-|#)P\d+$', '', canonical)
        if any(re.sub(r'(?:-|#)P\d+$', '', other) == family for other in pair - {canonical}):
            base['reason'] = 'independent_subquestion_excludes_parent_or_sibling_extension'
            return base
    binding = authored.get('target_assessment')
    target_question = (authored.get('source_question') if canonical == authored.get('adjacent_question')
                       else authored.get('adjacent_question'))
    assessed = (assessment_catalog or {}).get('by_identity', {}).get(target_question, [])
    matches = [item for item in assessed if isinstance(binding, dict)
               and item['exam_id'] == binding.get('exam_id') and item['part_id'] == binding.get('part_id')
               and item['knowledge_point'] == binding.get('knowledge_point', code)]
    if not matches:
        base['reason'] = 'reviewed_comparison_missing_exact_assessment_binding'
        return base
    target = matches[0]
    all_contexts = {**(assessment_catalog or {}).get('point_contexts', {}), **contexts}
    if (all_contexts.get(target['knowledge_point']) or {}).get('scope_status') not in _ALLOWED_SCOPE:
        base['reason'] = 'target_scope_does_not_allow_extension'
        return base
    relation = {'source_point': code, 'target_point': code, 'relation': 'same_point',
                'relationship_basis': 'authored_exact_assessment_binding'}
    if target['knowledge_point'] != code:
        edges = [edge for edge in (assessment_catalog or {}).get('relations', {}).get(code, [])
                 if edge['target_point'] == target['knowledge_point']
                 and _edge_supports_assessment(edge, target)
                 and _edge_matches_current_source(edge, base.get('current_focus', []), exact)]
        if not edges:
            base['reason'] = 'reviewed_comparison_target_relation_missing'
            return base
        relation = _relation_projection(min(edges, key=_edge_order))
    base.update(status='source_reviewed_condition_comparison',
        candidates=[{**copy.deepcopy(authored), **copy.deepcopy(target),
            'exam_frequency': _frequency(all_contexts.get(target['knowledge_point'])),
            'knowledge_relation': relation,
            'teacher_requires_current_applicability_check': True,
            'use': 'one_adjacent_condition_change_after_current_mainline_not_an_old_question_recommendation'}])
    return base


def _merge_extensions(base, reviewed):
    """An old same-point abstention cannot veto a newly proven hierarchy edge."""
    reviewed = [item for item in reviewed if item is not None]
    if not reviewed:
        return base
    blocked_points = {item.get('reviewed_point') for item in reviewed}
    candidates = [item for item in base.get('candidates', [])
                  if (item.get('knowledge_relation') or {}).get('relation') != 'same_point'
                  or item.get('knowledge_point') not in blocked_points]
    candidates += [candidate for item in reviewed for candidate in item['candidates']]
    if not candidates:
        return reviewed[0]
    candidates = _rank_extension_candidates(candidates)
    value = copy.deepcopy(base)
    value.update(status='source_bound_hierarchy_or_assessment_comparison', candidates=candidates[:6],
                 candidates_truncated=len(candidates) > 6,
                 reviewed_same_point_decisions=[{'knowledge_point': item.get('reviewed_point'),
                    'status': item['status'], 'reason': item.get('reason')} for item in reviewed])
    return value


def _candidate_step(text, steps):
    """Conservative lexical candidate; disclose evidence and require review."""
    tokens = _tokens(text)
    scored = [(len(tokens & _tokens(step['teacher_text'])), step, sorted(tokens & _tokens(step['teacher_text'])))
              for step in steps if _RELATION.search(step['teacher_text'])]
    scored.sort(key=lambda row: (-row[0], row[1]['step_id']))
    if not scored or scored[0][0] < 2 or len(scored) > 1 and scored[0][0] <= scored[1][0]:
        return None
    score, step, overlap = scored[0]
    return {'step_id': step['step_id'], 'matching_terms': overlap,
        'binding_status': 'lexical_candidate_requires_teacher_semantic_check'}


def _research_advice(routes, steps, *, point_scope=False):
    blocks, visuals, unresolved = [], [], []
    for route in routes:
        route_id = route['identity']
        route_steps = route.get('ordered_blocks', [])
        by_id = {str(row.get('step_id')): row for row in route_steps if isinstance(row, dict)}
        for row in route_steps:
            if not isinstance(row, dict) or not _text(row.get('teacher_text')):
                continue
            binding = ({'step_id': str(row['step_id']), 'binding_status': 'explicit_research_step'}
                       if point_scope and str(row.get('step_id')) in {s['step_id'] for s in steps}
                       else _candidate_step(row['teacher_text'], steps))
            if binding:
                blocks.append({**binding, 'route_identity': route_id,
                    'research_step_id': row['step_id'], 'teacher_text': row['teacher_text'],
                    'use': 'compare_with_exact_question_do_not_replace_its_mainline'})
        for index, recipe in enumerate(route.get('visual_insertions', [])):
            if not isinstance(recipe, dict):
                recipe = {'recipe': str(recipe)}
            original_step = str(recipe.get('step_id') or '')
            source_step = by_id.get(original_step) or {}
            if not source_step:
                unresolved.append({'route_identity': route_id, 'insertion_index': index,
                    'reason': 'research_step_not_explicit', 'recipe': copy.deepcopy(recipe)})
                continue
            binding = ({'step_id': original_step, 'binding_status': 'explicit_research_step'}
                       if point_scope and original_step in {s['step_id'] for s in steps}
                       else _candidate_step(source_step.get('teacher_text', ''), steps))
            # A declared S2 in a point recipe is not automatically Q2 in a
            # question. The relation must be reconciled against the exact step.
            if not binding or not _RELATION.search(source_step.get('teacher_text', '') + json.dumps(recipe, ensure_ascii=False)):
                unresolved.append({'route_identity': route_id, 'insertion_index': index,
                    'reason': 'exact_step_or_abstract_relation_not_established', 'recipe': copy.deepcopy(recipe)})
                continue
            visuals.append({**binding, 'route_identity': route_id, 'research_step_id': original_step,
                'recipe': copy.deepcopy(recipe), 'return_to_step_id': binding['step_id'],
                'use': 'compile_only_if_current_step_needs_this_abstract_relation'})
    return blocks, visuals, unresolved


def _explicit_question_step(source, question_steps):
    """A visual-stage ordinal/name is never evidence of a solution-step link."""
    by_id = {step['step_id']: step for step in question_steps}
    by_ref = {step['source_ref']: step for step in question_steps}
    declared_id = source.get('question_step_id')
    declared_ref = source.get('question_step_ref')
    if declared_id is None and declared_ref is None:
        return None
    if declared_id is not None and declared_id not in by_id:
        return None
    if declared_ref is not None and declared_ref not in by_ref:
        return None
    selected = by_id.get(declared_id) or by_ref.get(declared_ref)
    if declared_ref is not None and by_ref[declared_ref]['step_id'] != selected['step_id']:
        return None
    return selected


def _exact_semantic_route(template, question_steps):
    """Preserve local visual stages, never promote them to a full solution."""
    source = template.get('source_visuals') or {}
    design = source.get('teaching_design') or {}
    identity = template['question_identity']
    if source.get('identity') != identity or design.get('identity') != identity:
        return [], []
    stages = design.get('stages') or []
    if not stages or len({stage.get('id') for stage in stages}) != len(stages):
        return [], []
    nodes = {node.get('id') for panel in design.get('panels', []) for node in panel.get('nodes', [])}
    links = {link.get('id') for panel in design.get('panels', []) for link in panel.get('links', [])}
    auxiliary_stages, visuals = [], []
    for index, stage in enumerate(stages):
        explanation = stage.get('explanation') or {}
        text = ' '.join(_text(explanation.get(key)) for key in ('observe', 'reason', 'takeaway')).strip()
        node_refs, link_refs = stage.get('focus_nodes', []), stage.get('focus_links', [])
        if (not _text(stage.get('id')) or not text or not set(node_refs) <= nodes
                or not set(link_refs) <= links):
            return [], []
        recipe = {'design_ref': 'source_visuals.teaching_design', 'stage_id': stage['id'],
            'focus_nodes': copy.deepcopy(node_refs), 'focus_links': copy.deepcopy(link_refs),
            'values': copy.deepcopy(stage.get('values', {}))}
        auxiliary = {'stage_id': stage['id'], 'teacher_text': text, 'label': stage.get('label'),
            'explanation': copy.deepcopy(explanation), 'source_ref': f'source_visuals.teaching_design.stages.{index}',
            'recipe': recipe, 'role': 'local_visual_stage_not_full_question_solution',
            'binding_status': 'question_step_unbound_requires_teacher_alignment'}
        selected = _explicit_question_step(stage, question_steps)
        # An authored linear step may supply an explicit question-step link,
        # but its stage_id alone is still only a local visual relation.
        if selected is None and not any(key in stage for key in ('question_step_id', 'question_step_ref')):
            authored = [item for item in design.get('linear_steps', [])
                        if isinstance(item, dict) and item.get('stage_id') == stage['id']]
            resolved = [_explicit_question_step(item, question_steps) for item in authored]
            if resolved and all(item is not None for item in resolved) and len({item['step_id'] for item in resolved}) == 1:
                selected = resolved[0]
        if selected is not None:
            auxiliary.update(question_step_id=selected['step_id'], question_step_ref=selected['source_ref'],
                binding_status='explicit_current_question_step_reference')
        auxiliary_stages.append(auxiliary)
        if selected is not None and (node_refs or link_refs):
            visuals.append({'step_id': selected['step_id'], 'return_to_step_id': selected['step_id'],
                'binding_status': 'explicit_current_question_step_reference',
                'source': 'exact_question_teaching_design', 'recipe': recipe,
                'use': 'current_exact_stage_objects_and_state_only_not_whole_parent_scene'})
    return auxiliary_stages, visuals


def compile_exact_bridge(template, *, research_routes=(), syllabus_contexts=None, assessment_catalog=None):
    codes = template.get('knowledge_points', [])
    contexts = syllabus_contexts or {}
    value = _base(template['question_identity'], 'exact_question', codes)
    steps = [{'step_id': 'Q' + str(i + 1), 'teacher_text': step, 'source_ref': 'steps.' + str(i)}
             for i, step in enumerate(template.get('steps', []))]
    semantic_stages, semantic_visuals = _exact_semantic_route(template, steps)
    value.update(target=template.get('target'), given_objects=copy.deepcopy(template.get('given_objects', [])),
        initial_state=template.get('initial_state'), prerequisites=copy.deepcopy(template.get('prerequisites', {})),
        checks=copy.deepcopy(template.get('checks', [])), ordered_steps=steps,
        overall_route={'target': template.get('target'), 'step_order': [step['step_id'] for step in steps],
            'instruction': '先说明当前所求与整体路径，再按ordered_steps解释条件、动作及因果；checks用于回到原问验算。'},
        scope_boundaries={code: _scope(contexts.get(code)) for code in codes},
        independent_subquestion=template.get('identity_kind') in {'subquestion', 'teaching_part'})
    value['mainline_source'] = 'current_exact_question_template_steps'
    if semantic_stages:
        value['auxiliary_semantic_stages'] = semantic_stages
        value['semantic_stage_instruction'] = '这些是局部机制图的演示阶段，不是完整解题步骤；只有明确question_step引用才已绑定，否则按当前题主线人工关联，不能按阶段序号或名称替代题目步骤。'
    narrative = (template.get('source_visuals') or {}).get('narrative_plan') or {}
    if narrative.get('identity') == template['question_identity']:
        value['narrative_entry'] = {key: copy.deepcopy(narrative[key]) for key in
            ('lead', 'teaching_goal', 'first_observation') if key in narrative}
    eligible_routes = [route for route in research_routes if route.get('identity') in codes
                       and (contexts.get(route['identity']) or {}).get('scope_status') in _ALLOWED_SCOPE]
    blocks, visuals, unresolved = _research_advice(eligible_routes, steps)
    if value['independent_subquestion']:
        # Even an apparently related point recipe can contain parent/sibling
        # values. Preserve its exact location in the teacher route, not its
        # payload, within this independently answer-protected Pxx.
        blocks = [{key: item[key] for key in
            ('step_id', 'matching_terms', 'binding_status', 'route_identity', 'research_step_id', 'use')
            if key in item} for item in blocks]
        for item in visuals:
            item.pop('recipe', None)
            item['recipe_ref'] = {'route_identity': item['route_identity'],
                'research_step_id': item['research_step_id'],
                'scope': 'teacher_only_recompile_against_current_Pxx_before_use'}
        for item in unresolved:
            item.pop('recipe', None)
    # The exact scene is normally more precise than the broad point recipe.
    # Bind it once to its most specific abstract step; never stamp it on every step.
    scene = _text(template.get('scene'))
    binding = _candidate_step(scene, steps) if scene and _RELATION.search(scene) else None
    if semantic_visuals:
        visuals = semantic_visuals + visuals
    elif binding:
        visuals.insert(0, {**binding, 'recipe': {'scene': scene, 'visual_kind': template.get('visual_kind')},
            'source': 'exact_question_scene', 'return_to_step_id': binding['step_id'],
            'use': 'crop_semantics_to_this_step_before_frontend_compilation'})
    elif scene:
        unresolved.insert(0, {'source': 'exact_question_scene', 'reason': 'no_unique_relation_step_alignment',
            'recipe': {'scene': scene, 'visual_kind': template.get('visual_kind')}})
    value.update(research_step_advice=blocks, local_visuals=visuals, unbound_visual_advice=unresolved,
        bounded_extension=_extensions(codes, template['question_identity'], assessment_catalog, contexts))
    if template.get('exam_visual_binding'):
        from exam_figure_binding_408 import validate_binding, project_binding, step_visuals
        binding = template['exam_visual_binding']
        validate_binding(binding, steps=template.get('steps', []))
        value['exam_visual_binding'] = project_binding(binding)
        value['local_visuals'] = step_visuals(binding)
        value['unbound_visual_advice'] = []
        value['visual_selection_policy'] = 'source_reviewed_exact_binding_replaces_legacy_generic_scene'
    reviewed_extensions = [_authored_extension(route, template['question_identity'], contexts, assessment_catalog)
                           for route in eligible_routes]
    reviewed_extensions = [item for item in reviewed_extensions if item is not None]
    if reviewed_extensions:
        value['bounded_extension'] = _merge_extensions(value['bounded_extension'], reviewed_extensions)
    return value


def bind_current_personal(bridge, summaries):
    """Replace, do not merge, personal advice from the same validated query."""
    value = copy.deepcopy(bridge)
    sources, withheld = [], []
    allowed = set(value.get('knowledge_points', []))
    for summary in summaries or []:
        code = summary.get('knowledge_point')
        if code not in allowed:
            continue
        personal = summary.get('personal_context')
        profile = summary.get('learning_profile') or {}
        status = summary.get('status') or profile.get('status')
        if personal is not None and not personal.get('complete'):
            withheld.append({'knowledge_point': code, 'subpoint_id': summary.get('subpoint_id'),
                             'status': personal.get('status') or status or 'unavailable'})
            continue
        if status not in {'current', 'semantic_current'}:
            withheld.append({'knowledge_point': code, 'subpoint_id': summary.get('subpoint_id'), 'status': status or 'unavailable'})
            continue
        row = {'knowledge_point': code, 'status': status,
            'version': (personal or {}).get('profile_version') or summary.get('parent_profile_version') or profile.get('version'),
            'evidence_basis': 'same_query_validated_personal_summary_and_current_answer_required'}
        if summary.get('subpoint_id'):
            row['subpoint_id'] = summary['subpoint_id']
        evidence = personal if personal is not None else {**profile, **summary}
        for key in ('short_summary', 'next_check'):
            if key in evidence:
                row[key] = copy.deepcopy(evidence[key])
        focus = evidence.get('teaching_focus')
        if isinstance(focus, dict):
            row['teaching_focus'] = copy.deepcopy(focus)
        if summary.get('teaching_focus_evidence_object'):
            row['teaching_focus_evidence_object'] = copy.deepcopy(summary['teaching_focus_evidence_object'])
        plan = evidence.get('teaching_plan') or {}
        if plan:
            row['first_action'] = plan.get('first_action')
            row['explanation_strategy'] = (plan.get('explanation') or {}).get('strategy')
            row['next_check'] = row.get('next_check') or plan.get('check')
            feedback = plan.get('visual_feedback') or {}
            if feedback:
                row['visual_feedback'] = {key: copy.deepcopy(feedback[key]) for key in
                    ('scope', 'preference', 'observed_effect', 'next_adjustment', 'next_check') if key in feedback}
        sources.append(row)
    value['personal_adaptation'] = {'status': 'available_requires_current_answer_check' if sources else 'current_question_only',
        'sources': sources, 'withheld': withheld,
        'instruction': '先核对当前首答。teaching_focus中brief仅对已有独立证据的窄机制简带；focus优先解释明确断点；prompted_repair仍需检查，unverified不当成不会或已会。当前表现改变时调整详略，不外推整点掌握。'}
    stale_codes = {row['knowledge_point'] for row in withheld if row['status'] in {'stale', 'semantic_stale'}}
    if stale_codes and value.get('scope') == 'exact_question':
        for field in ('research_step_advice', 'local_visuals', 'unbound_visual_advice'):
            value[field] = [item for item in value.get(field, []) if item.get('route_identity') not in stale_codes]
    return value


def strip_personal(bridge):
    value = copy.deepcopy(bridge)
    value['personal_adaptation'] = {'status': 'withheld_source_changed', 'sources': [],
        'instruction': '个人依据变化，仅依据当前题和当前作答讲解。'}
    if value.get('scope') in {'knowledge_point', 'subpoint'}:
        for field in ('ordered_steps', 'local_visuals', 'unbound_visual_advice'):
            value.pop(field, None)
        value['route_status'] = 'current_question_only_personal_source_changed'
    elif value.get('scope') == 'exact_question':
        value.pop('research_step_advice', None)
        for field in ('local_visuals', 'unbound_visual_advice'):
            value[field] = [item for item in value.get(field, []) if not item.get('route_identity')]
    return value


def compile_point_bridge(identity, *, knowledge_point, view, route=None, summary=None,
                         syllabus_context=None, assessment_catalog=None):
    if view == 'protected':
        return None
    value = _base(identity, 'subpoint' if identity.startswith('KSP-') else 'knowledge_point', [knowledge_point])
    context = syllabus_context or {}
    value['scope_boundaries'] = {knowledge_point: _scope(context)}
    value['current_exact_question_required'] = True
    # Point advice is never mistaken for a solved exact question. Hint readers
    # receive the join and personal adaptation but no full teacher sequence.
    steps = [{key: copy.deepcopy(row[key]) for key in ('step_id', 'teacher_text', 'goal', 'entry_condition', 'causal_bridge') if key in row}
             for row in (route or {}).get('ordered_blocks', []) if isinstance(row, dict)]
    personal_status = (summary or {}).get('status') or ((summary or {}).get('learning_profile') or {}).get('status')
    if view == 'direct' and context.get('scope_status') in _ALLOWED_SCOPE and personal_status not in {'stale', 'semantic_stale'}:
        value['ordered_steps'] = steps
        _, visuals, unresolved = _research_advice([route] if route else [], steps, point_scope=True)
        value['local_visuals'], value['unbound_visual_advice'] = visuals, unresolved
    else:
        value.update(ordered_steps=[], local_visuals=[], route_status='compile_current_hint_from_teacher_route')
    if identity.startswith('KSP-'):
        # A parent point's assessed-focus pool says nothing about the exact
        # mechanism represented by this KSP. Keep its reviewed lesson, but do
        # not manufacture an extension from parent keywords or parent advice.
        exact_route = route if (route or {}).get('identity') == identity else None
        extension = _authored_extension(exact_route, identity, {knowledge_point: context}, assessment_catalog)
        value['bounded_extension'] = extension if extension is not None else {
            'status': 'no_exact_subpoint_comparison', 'source': 'no_authored_subpoint_comparison',
            'candidates': [], 'candidates_truncated': False, 'maximum_selected': 1,
            'reason': '当前小点没有已审核的具体条件对照；不继承父点对照，也不回落父点关键词候选池。',
            'is_old_question_recommendation': False,
            'release_conditions': ['authored_comparison_for_this_exact_subpoint',
                'current_mainline_resolved', 'current_hint_level_permits_extension']}
    else:
        value['bounded_extension'] = _merge_extensions(
            _extensions([knowledge_point], identity, assessment_catalog, {knowledge_point: context}),
            [_authored_extension(route, identity, {knowledge_point: context}, assessment_catalog)])
    return bind_current_personal(value, [summary] if summary else [])
