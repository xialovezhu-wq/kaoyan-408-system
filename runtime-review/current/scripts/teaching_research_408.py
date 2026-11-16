"""Bounded teaching-only research routes; never a learner/profile writer.

ZIPs are untrusted data. Validate every manifest member before reading proposals;
retain source gaps and partial status, and install only derived teacher advice.
"""
from __future__ import annotations
import argparse
import copy
import hashlib
import json
from pathlib import Path, PurePosixPath
import re
import zipfile

POLICY_VERSION = 'linear-text-primary-408-v1'
INDEX_REL = Path('support/teaching-research-408/current.json')
IDENTITY = re.compile(r'(?:DS|CO|OS|CN)\d{2}-\d{2}|KSP-[0-9a-f]+|S27-(?:DS|CO|OS|CN)(?:-\d{2})*')
POLICY = {
    'version': POLICY_VERSION,
    'chat_linear_text_required': True, 'visible_main_text_required': True, 'visual_secondary': True,
    'primary': '同一轮先用聊天正文按题目线索、条件、动作、原因与结果讲清当前获准的完整推理；图内文字不能替代正文。',
    'secondary': '可点击图解绑定正文的具体步骤，帮助核对抽象对象、关系或状态；点击不承担主线必需理由。',
    'scope': '只编译当前精确题/Pxx与本轮提示级；完整教师路线是direct/L5材料，提示轮另编安全局部，不能降低原稿等级。',
    'personalization': '以同次最新有效个人摘要和真实作答决定起手、压缩、放慢与停止；研究旧摘要不作为当前掌握事实。',
    'effectiveness': 'unverified',
    'evidence_status': 'derived_teaching_advice_not_learning_evidence',
}


def raw(value):
    return (json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':')) + '\n').encode()


def sha(value):
    return hashlib.sha256(value).hexdigest()


def write(path, content):
    path.parent.mkdir(parents=True, exist_ok=True)
    if not path.exists() or path.read_bytes() != content:
        tmp = path.with_suffix(path.suffix + '.tmp')
        tmp.write_bytes(content)
        tmp.replace(path)


def read_package(path):
    with zipfile.ZipFile(path) as z:
        names = [i.filename for i in z.infolist() if not i.is_dir()]
        if len(names) != len(set(names)) or sum(i.file_size for i in z.infolist()) > 512 * 1024 * 1024:
            raise ValueError('research_zip_duplicate_or_oversize')
        if any(PurePosixPath(n).is_absolute() or '..' in PurePosixPath(n).parts or '\\' in n for n in names):
            raise ValueError('research_zip_unsafe_path')
        manifest = json.loads(z.read('manifest.json'))
        if manifest.get('schema') != 'cs408-teaching-research-return-v1':
            raise ValueError('research_manifest_schema')
        declared = {x['path']: x for x in manifest['files']}
        if len(declared) != len(manifest['files']) or set(names) != set(declared) | {'manifest.json'}:
            raise ValueError('research_manifest_coverage')
        for name, item in declared.items():
            content = z.read(name)
            if len(content) != item['bytes'] or sha(content) != item['sha256']:
                raise ValueError('research_member_hash:' + name)
        # Never extract or execute archive programs/HTML. Only selected JSON is parsed.
        files = {name: json.loads(z.read(name)) for name in (
            'proposals.json', 'coverage.json', 'point_routes.json',
            'subpoint_adaptations.json', 'subpoint_routes.json') if name in names}
    return manifest, files


def coverage_rows(value):
    return value.get('items', []) or [*value.get('canonical_points', []), *value.get('subpoints', [])]


def route_id(value):
    return value.get('subpoint_id') or value.get('id')


def text_sequence(value):
    sequence = next((value[k] for k in ('teaching_sequence', 'linear_teaching_sequence', 'linear_sequence', 'teaching_order')
                     if isinstance(value.get(k), list) and value[k]), [])
    result = []
    for i, step in enumerate(sequence):
        if isinstance(step, str):
            result.append({'step_id': 'S' + str(i + 1), 'teacher_text': step})
        elif isinstance(step, dict):
            body = step.get('teacher_text') or step.get('action') or step.get('text')
            if not isinstance(body, str) or not body.strip():
                continue
            result.append({**{key: copy.deepcopy(step[key]) for key in
                ('goal', 'entry_condition', 'causal_bridge', 'stop_or_continue') if key in step},
                'step_id': str(step.get('step_id') or 'S' + str(i + 1)), 'teacher_text': body})
    return result


def normalize(part, manifest, files, archive_sha):
    proposals = files['proposals.json']['proposals']
    by_proposal = {v['proposal_id']: v for v in proposals}
    by_id = {}
    for name in ('point_routes.json', 'subpoint_adaptations.json', 'subpoint_routes.json'):
        for row in files.get(name, {}).get('items', []):
            by_id[route_id(row)] = (name, row)
    result = {}
    for coverage in coverage_rows(files['coverage.json']):
        identity = coverage.get('id')
        if not isinstance(identity, str) or not IDENTITY.fullmatch(identity):
            continue
        candidates = [by_proposal[p] for p in coverage.get('proposal_ids', []) if p in by_proposal]
        entry = by_id.get(identity)
        if entry:
            source_file, content = entry
        elif identity.startswith('KSP-') and text_sequence(coverage):
            source_file, content = 'coverage.json', coverage
        else:
            exact = [v for v in candidates if identity in json.dumps(v.get('scope', {}))]
            if not exact:
                raise ValueError('research_target_proposal_missing:' + identity)
            source_file, content = 'proposals.json', exact[0]
        sequence = text_sequence(content)
        if not sequence:
            raise ValueError('research_target_sequence_missing:' + identity)
        visual = next((copy.deepcopy(content[k]) for k in
            ('visual_insertions', 'visual_insertion', 'visual_support', 'local_visual', 'visual_route')
            if content.get(k)), [])
        scope = content.get('scope', {})
        parent = coverage.get('parent') or content.get('parent') or scope.get('parent') or scope.get('canonical_point')
        title = coverage.get('title') or content.get('title') or scope.get('title') or identity
        row = {
            'schema': 'cs408-derived-teaching-route-v1', 'identity': identity,
            'parent': parent, 'title': title, 'policy_version': POLICY_VERSION,
            'review_status': coverage.get('review_status', 'unverified'),
            'source_batch_status': manifest.get('status', 'unknown'),
            'teacher_release_level': 5, 'frontend_ready': False,
            'ordered_blocks': sequence, 'visual_insertions': visual,
            'adoption': 'bounded_teacher_advice_requires_current_question_compilation',
            'personalization_binding': 'current_query_profile_and_current_answer_only',
            'effectiveness': 'unverified', 'learning_write_count': 0,
            'source': {'part_id': part, 'archive_sha256': archive_sha,
                       'snapshot_id': manifest.get('source_commit'), 'file': source_file,
                       'file_sha256': next(f['sha256'] for f in manifest['files'] if f['path'] == source_file),
                       'proposal_ids': coverage.get('proposal_ids', [])},
            'source_refs': copy.deepcopy(content.get('source_refs', coverage.get('source_refs', []))),
            'limitations': ['研究是教师建议，不是新作答、已掌握或新教法有效性证据。',
                           '包内教材/历史来源声明未在本地逐页重读，不能称为本轮已核验原文。',
                           '旧个人状态不随研究路线导入；每次使用以同次正式摘要和当前作答核对。'],
        }
        if row['review_status'] == 'needs_source':
            row['limitations'].append('来源缺口尚未解决，仅保留有界机制建议，不提升个人历史或教材核验结论。')
        # Explicitly reject a known inherited parent-mechanism mismatch. Keep
        # its original archive intact; the local narrow replacement is reviewable.
        if identity == 'KSP-9953aee198ee6eed8fce905f':
            row['ordered_blocks'] = [
                {'step_id': 'S1', 'teacher_text': '先分清本题问的是一层的整个协议数据单元，还是其中的地址字段。'},
                {'step_id': 'S2', 'teacher_text': '地址字段是协议控制信息的一部分；一个地址字段不能代表整个协议数据单元。沿当前题面检查首部、数据与必要尾部的包含关系。'},
                {'step_id': 'S3', 'teacher_text': '回到原问，只比较它要求的整体或字段，不引入AP转发和无线地址方向。'}]
            row['visual_insertions'] = [{'step_id': 'S2', 'recipe': '一个协议数据单元边框包住控制信息和数据，将地址字段只标在它所属的控制信息内。', 'return_to_prose': '先辨整体与部分，再按原问选择比较对象。'}]
            row['local_review'] = 'rejected_parent_AP_wireless_mechanism_replaced_with_PDU_field_scope'
        if identity == 'KSP-e5f4dd6434e7f37bc19cc01a':
            row['ordered_blocks'] = [
                {'step_id': 'S1', 'teacher_text': '先区分控制信号的输入依据与控制器最终输出的微操作信号；本问讨论组合逻辑控制器，不能直接套Moore机输出与次态的区分。'},
                {'step_id': 'S2', 'teacher_text': '指令译码确定当前指令要求哪些操作，时钟或节拍信号限定这些操作在什么时刻发出。先把操作需求与发出时刻对应起来，再比较题目所列因素。'},
                {'step_id': 'S3', 'teacher_text': '状态和条件也可以参与控制逻辑。题目问主要依据时，选项之间的比较不能扩写成PSW等状态条件永远不参与微操作控制。'}]
            row['visual_insertions'] = [{'step_id': 'S2',
                'recipe': '译码、时序、状态条件分别进入控制逻辑，控制逻辑输出微操作信号；只标输入依据与输出，不添加题面没有的Moore次态分支。',
                'return_to_prose': '分别核对要做什么、何时做及相关状态条件，再按当前题目的问法收束。'}]
            row['local_review'] = 'recovered_exact_question_excludes_inherited_Moore_next_state_route'
        if identity == 'KSP-48906b1e735aa65cc4c6d9aa':
            row['source_refs'] = [ref for ref in row['source_refs'] if ref != 'MCP:15']
            row['excluded_source_refs'] = [{'ref': 'MCP:15', 'formal_id': 'CO_UNK_055',
                'reason': '该正式卡是5.6.6(19)高级流水线题，不是本小点5.6.6(02)的精确原题。'}]
            row['limitations'].append('本地已恢复DOCX段3114内嵌的5.6.6(02)原图；它未给完整时长与资源等价条件。不得把CO_UNK_055当作本题已核绑定，也不得把旧速度比恢复为普遍事实。')
            row['ordered_blocks'][-1]['teacher_text'] = '原图已恢复，但硬件时长和资源等价条件仍不充分；分别定义完整任务耗时与稳态产出间隔后才能比较，不恢复旧速度比为普遍事实。'
            for insertion in row['visual_insertions']:
                if isinstance(insertion, dict) and 'text_return' in insertion:
                    insertion['text_return'] = '回到当前题给出的硬件资源和时间条件；条件不足时保留不裁定旧速度比。'
            row['local_review'] = 'recovered_exact_pipeline_question_excludes_CO_UNK_055_misbinding'
        if identity in result:
            raise ValueError('research_duplicate_target:' + identity)
        result[identity] = row
    return result


def install(repo_root, archive_root):
    repo, root = Path(repo_root), Path(archive_root)
    targets, packages = {}, []
    for number in range(13):
        part = f'R{number:02}'
        path = root / f'408_teaching_research_{part}_return.zip'
        manifest, files = read_package(path)
        if manifest.get('part_id') != part:
            raise ValueError('research_part_identity')
        digest = sha(path.read_bytes())
        packages.append({'part_id': part, 'sha256': digest, 'path': str(path), 'status': manifest.get('status')})
        if number:
            rows = normalize(part, manifest, files, digest)
            if targets.keys() & rows.keys():
                raise ValueError('research_cross_part_duplicate')
            targets.update(rows)
    duplicate = root / '408_teaching_research_R09_return (1).zip'
    if duplicate.exists() and sha(duplicate.read_bytes()) != next(v['sha256'] for v in packages if v['part_id'] == 'R09'):
        raise ValueError('research_R09_conflicting_duplicate')
    counts = {'canonical_points': sum(bool(re.fullmatch(r'(?:DS|CO|OS|CN)\d{2}-\d{2}', k)) for k in targets),
              'subpoints': sum(k.startswith('KSP-') for k in targets),
              'syllabus_entries': sum(k.startswith('S27-') for k in targets)}
    if counts != {'canonical_points': 629, 'subpoints': 1256, 'syllabus_entries': 12}:
        raise ValueError('research_incomplete_scope:' + str(counts))
    objects = {}
    for identity, route in sorted(targets.items()):
        content = raw(route); digest = sha(content); ref = 'objects/' + digest + '.json'
        write(repo / INDEX_REL.parent / ref, content)
        objects[identity] = {'ref': ref, 'sha256': digest}
    index = {'schema': 'cs408-derived-teaching-index-v1', 'policy_version': POLICY_VERSION,
             'policy': POLICY, 'targets': objects, 'packages': packages, 'coverage': counts,
             'needs_source_count': sum(v['review_status'] == 'needs_source' for v in targets.values()),
             'local_scope_repairs': [k for k, v in targets.items() if v.get('local_review')],
             'learning_write_count': 0, 'formal_write_count': 0}
    write(repo / INDEX_REL, raw(index))
    return {'status': 'derived_routes_installed', **counts, 'needs_source_count': index['needs_source_count'],
            'index_sha256': sha(raw(index)), 'unique_packages': 13, 'duplicate_R09_preserved': duplicate.exists(),
            'learning_write_count': 0, 'formal_write_count': 0}


def load_catalog(repo_root):
    if repo_root is None or not (Path(repo_root) / INDEX_REL).is_file():
        return {'targets': {}, 'index_sha256': None, 'coverage': {}}
    root = Path(repo_root) / INDEX_REL.parent
    content = (Path(repo_root) / INDEX_REL).read_bytes(); index = json.loads(content)
    if index.get('schema') != 'cs408-derived-teaching-index-v1' or index.get('policy_version') != POLICY_VERSION:
        raise ValueError('research_index_schema_or_policy')
    targets = {}
    for identity, ref in index['targets'].items():
        if ref['ref'] != 'objects/' + ref['sha256'] + '.json' or not IDENTITY.fullmatch(identity):
            raise ValueError('research_index_target_ref')
        content = (root / ref['ref']).read_bytes()
        if sha(content) != ref['sha256']:
            raise ValueError('research_route_hash_changed:' + identity)
        route = json.loads(content)
        if route['identity'] != identity or route['teacher_release_level'] != 5 or route['frontend_ready']:
            raise ValueError('research_route_identity_or_release')
        targets[identity] = route
    return {'targets': targets, 'index_sha256': sha((Path(repo_root) / INDEX_REL).read_bytes()), 'coverage': index['coverage']}



def apply_incremental(repo_root, updates):
    """Replace only reviewed named routes, preserving the current object graph.

    Expected old/new hashes permit retry after an interrupted manifest swap.
    This writer never reconstructs research archives or writes learner state.
    """
    import fcntl
    from question_visual_templates_408 import _atomic
    repo = Path(repo_root)
    target = repo / INDEX_REL
    target.parent.mkdir(parents=True, exist_ok=True)
    with (target.parent / '.incremental.lock').open('a+') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        index = json.loads(target.read_bytes()) if target.exists() else {
            'schema': 'cs408-derived-teaching-index-v1', 'policy_version': POLICY_VERSION,
            'policy': copy.deepcopy(POLICY), 'targets': {}, 'packages': [], 'coverage': {}}
        if index.get('schema') != 'cs408-derived-teaching-index-v1' or index.get('policy_version') != POLICY_VERSION:
            raise ValueError('research_incremental_index_schema')
        seen, changed = set(), []
        for update in updates:
            identity, route = update['identity'], update['route']
            if identity in seen or not IDENTITY.fullmatch(identity) or route.get('identity') != identity:
                raise ValueError('research_incremental_identity')
            seen.add(identity)
            if (route.get('schema') != 'cs408-derived-teaching-route-v1' or route.get('policy_version') != POLICY_VERSION
                    or len(raw(route)) > 256 * 1024 or route.get('teacher_release_level') != 5
                    or route.get('frontend_ready') is not False or route.get('effectiveness') != 'unverified'):
                raise ValueError('research_incremental_teacher_boundary')
            content = raw(route); digest = sha(content)
            old = (index['targets'].get(identity) or {}).get('sha256')
            if old not in (update['expected_previous_sha256'], digest):
                raise ValueError('research_incremental_previous_version_conflict:' + identity)
            ref = 'objects/' + digest + '.json'; path = target.parent / ref
            if path.exists() and path.read_bytes() != content:
                raise ValueError('research_incremental_immutable_conflict')
            if not path.exists():
                _atomic(path, content)
            index['targets'][identity] = {'ref': ref, 'sha256': digest}
            if old != digest:
                changed.append(identity)
        keys = index['targets']
        index['coverage'] = {'canonical_points': sum(bool(re.fullmatch(r'(?:DS|CO|OS|CN)\d{2}-\d{2}', k)) for k in keys),
            'subpoints': sum(k.startswith('KSP-') for k in keys),
            'syllabus_entries': sum(k.startswith('S27-') for k in keys)}
        index['learning_write_count'] = 0
        index['formal_write_count'] = 0
        _atomic(target, raw(index))
    return {'status': 'APPLIED' if changed else 'UNCHANGED', 'identities': sorted(seen),
            'changed': changed, 'index_sha256': sha(target.read_bytes()), 'learning_write_count': 0}

_VISUAL_SKIP = re.compile(
    r'none_default|本次[^。；\n]*none|后续[^。；\n]*(?:要求|请求)[^。；\n]*图时再画'
    r'|(?:不堆可点击图|不用(?:多|任何|额外)?图|不画图|无需(?:额外)?图|只(?:需|用)文字)'
    r'(?=[。；，、\n]|$)')


def requires_visual_recompile(value):
    # Missing optional metadata is not an explicit historical no-visual choice.
    # Also preserve constraints such as "不用图替代逻辑" and "不用单个调度图代替":
    # they qualify a concrete diagram, rather than asking to discard its recipe.
    if isinstance(value, dict):
        if any(isinstance(value.get(key), str) and value[key].strip().lower() in
               {'none', 'none_default'} for key in ('kind', 'when', 'mode')):
            return True
        return any(requires_visual_recompile(item) for item in value.values())
    if isinstance(value, list):
        return any(requires_visual_recompile(item) for item in value)
    return isinstance(value, str) and bool(_VISUAL_SKIP.search(value))


def current_strategy(previous, first_action):
    clauses = [v for v in re.split(r'(?<=[。；])', previous) if v.strip() and not _VISUAL_SKIP.search(v)]
    lead = ''.join(clauses).strip() or first_action + '。'
    return lead + '先把条件、动作与因果在线性正文中讲清，同轮独立编译一幅紧贴本问机制的可点击辅助图；点击仅补细节，不让用户点击寻找主线。'


def effective_route(route):
    """Project old research choices under current delivery, preserving originals.

    Original proposal bytes remain addressed by source.file_sha256/archive_sha256.
    An explicit old `none` is a historical choice, never a current skip command.
    """
    value = copy.deepcopy(route)
    raw_insertions = value.get('visual_insertions', [])
    insertions = raw_insertions if isinstance(raw_insertions, list) else [raw_insertions]
    if not insertions:
        insertions = [{}]
    projected = []
    for insertion in insertions:
        item = copy.deepcopy(insertion) if isinstance(insertion, dict) else {'recipe': str(insertion)}
        recompile = requires_visual_recompile(item)
        item.pop('none_reason', None)
        item.pop('no_visual_branch', None)
        item.pop('implementation_note', None)
        item['when'] = 'with_substantive_explanation_unless_current_explicit_text_only_or_single_fact'
        item['kind'] = 'current_question_clickable_auxiliary'
        item['effectiveness'] = 'unverified'
        item['status'] = 'compile_required_not_yet_rendered_or_effect_verified'
        item['current_delivery_override'] = {'policy_version': POLICY_VERSION,
            'historical_research_source': copy.deepcopy(value.get('source', {})),
            'historical_visual_choice_is_not_current_execution': True}
        if recompile:
            item = {key: item[key] for key in ('step_id', 'when', 'kind', 'effectiveness', 'status',
                    'current_delivery_override', 'minimal_objects', 'minimum_objects') if key in item}
            item['recipe'] = ('围绕“' + value.get('title', value['identity']) +
                '”的当前真实断点，使用本问已公开的对象、关系和状态独立编译一幅可点击辅助图；对应文字因果先完整可读，点击只补细节。')
            item['return_to_prose'] = '回到当前步骤的条件和因果继续原问，不以点过图代替理解或独立作答。'
        projected.append(item)
    value['visual_insertions'] = projected
    value['current_delivery_override'] = {'policy_version': POLICY_VERSION,
        'original_research_route_sha256': sha(raw(route)),
        'historical_source_preserved': True, 'effectiveness': 'unverified'}
    return value


def current_teaching_brief(identity, syllabus_context, teaching_plan, personal_status):
    """Join already validated inputs; do not author new learner facts.

    This is teacher context, never a student scene. The selected point's scope
    applies even when a historical route or a preferred format suggests more.
    No source paths or quotes are copied into this compact execution brief.
    """
    scope = syllabus_context or {}
    brief = {
        'identity': identity, 'teacher_only': True, 'frontend_ready': False,
        'current_question_required': True,
        'fact_authority': 'current_exact_question_given_objects_initial_state_target_prerequisites',
        'scope_status': scope.get('scope_status', 'unavailable'),
        'syllabus_paths': copy.deepcopy(scope.get('paths', [])),
        'teaching_boundary': scope.get('teaching_boundary'),
        'teaching_guard': scope.get('teaching_guard'),
        'personal_status': personal_status,
        'decision_order': ['current_user_request', 'current_question_and_answer', 'current_syllabus_boundary',
                           'current_scoped_preference', 'historical_teacher_route'],
        'evidence_status': 'derived_teaching_advice_not_learning_evidence',
        'effectiveness': 'unverified',
    }
    if personal_status not in {'current', 'semantic_current'} or not teaching_plan:
        brief['personal_adaptation_status'] = ('withheld' if personal_status in {'stale', 'semantic_stale'}
                                                else 'no_available_plan')
        return brief
    brief['personal_adaptation_status'] = 'available_requires_current_answer_check'
    brief['first_action'] = teaching_plan.get('first_action')
    brief['explanation_strategy'] = teaching_plan.get('explanation', {}).get('strategy')
    feedback = teaching_plan.get('visual_feedback') or {}
    if feedback:
        preference = feedback.get('preference') or {}
        brief['preference_status'] = preference.get('status', 'unknown')
        brief['applicability_scope'] = feedback.get('scope')
        if preference.get('status') in {'explicit_preference', 'observed_tendency'}:
            brief['preference'] = preference.get('text')
            brief['preference_source_ids'] = list(preference.get('source_ids', []))
        brief['next_adjustment'] = feedback.get('next_adjustment')
        brief['next_check'] = feedback.get('next_check')
        brief['observed_effect'] = {key: copy.deepcopy(feedback.get('observed_effect', {}).get(key))
                                    for key in ('support', 'text', 'source_ids')}
    else:
        brief['preference_status'] = 'unknown'
        brief['next_check'] = teaching_plan.get('check')
    return brief


def project(catalog, identity, *, view, personal_status=None, object_ref=None,
            syllabus_context=None, teaching_plan=None):
    if view == 'protected':
        return {}
    if not catalog['targets'] and not teaching_plan:
        return {}
    native_route = catalog['targets'].get(identity)
    route = effective_route(native_route) if native_route else None
    result = {'teaching_delivery_policy': copy.deepcopy(POLICY)}
    brief = current_teaching_brief(identity, syllabus_context, teaching_plan, personal_status)
    if not route:
        result['linear_teaching_route'] = {'identity': identity, 'status': 'current_question_compilation_required',
            'reason': 'no_research_route_for_this_new_target', 'current_personal_status': personal_status,
            'frontend_ready': False, 'current_teaching_brief': brief}
        return result
    binding = {'identity': identity, 'status': 'teacher_advice_available', 'policy_version': POLICY_VERSION,
               'native_route_version': sha(raw(native_route)),
               'research_index_sha256': catalog['index_sha256'], 'review_status': route['review_status'],
               'current_personal_status': personal_status, 'frontend_ready': False,
               'current_teaching_brief': brief,
               'hint_use': '完整路线仅供教师备课；本轮提示按当前题/Pxx和允许层级独立编译，不直接序列化研究对象。'}
    if object_ref:
        binding['route_object'] = object_ref('derived-teaching-routes', route)
    stale = personal_status in {'stale', 'semantic_stale'}
    if stale:
        binding.update(status='current_question_only', personal_advice_withheld=True)
    if view == 'direct' and not stale:
        binding['ordered_blocks'] = copy.deepcopy(route['ordered_blocks'])
        binding['visual_insertions'] = copy.deepcopy(route['visual_insertions'])
        binding['limitations'] = list(route['limitations'])
    result['linear_teaching_route'] = binding
    return result


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('command', choices=['install'])
    parser.add_argument('--repo-root', required=True)
    parser.add_argument('--archive-root', required=True)
    args = parser.parse_args()
    print(json.dumps(install(args.repo_root, args.archive_root), ensure_ascii=False, indent=2))
