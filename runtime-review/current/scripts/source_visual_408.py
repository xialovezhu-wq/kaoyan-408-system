"""Source-backed figure plans for the existing 408 teaching pipeline.

Cold maintenance binds reviewed pixels, regions and syllabus boundaries. Hot
teaching selects only one immutable plan; it never scans exam papers, guesses a
crop, or treats a teacher plan as permission to disclose an answer.
"""
from __future__ import annotations

import argparse
import copy
import csv
import hashlib
import json
import math
import os
from pathlib import Path
import re
import tempfile

import runtime_paths_408 as _paths

MANIFEST_REL = Path('.private/source-visual-408/current.json')
PLAN_SCHEMA = 'cs408-source-visual-plan-v1'
INDEX_SCHEMA = 'cs408-source-visual-index-v1'
MAX_PLAN_BYTES = 256 * 1024
_cache = {}


def _require(ok, reason):
    if not ok:
        raise ValueError('source_visual_' + reason)


def _raw(value):
    return (json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':')) + '\n').encode()


def _sha(data):
    return hashlib.sha256(data).hexdigest()


def clear_cache():
    _cache.clear()


def _manifest(repo_root):
    path = Path(repo_root).resolve() / MANIFEST_REL
    if not path.is_file():
        return None
    stat = path.stat()
    key = (str(path), stat.st_mtime_ns, stat.st_size)
    if key not in _cache:
        value = json.loads(path.read_bytes())
        _require(value.get('schema') == INDEX_SCHEMA, 'manifest_schema')
        _require(isinstance(value.get('question_plans'), dict) and isinstance(value.get('point_plans'), dict), 'manifest_routes')
        _cache.clear()
        _cache[key] = value
    return _cache[key]


def _read_plan(repo_root, kind, identity):
    manifest = _manifest(repo_root)
    if manifest is None:
        return None
    ref = manifest[kind].get(identity)
    if ref is None:
        return None
    digest = ref.get('sha256', '')
    _require(re.fullmatch(r'[0-9a-f]{64}', digest) and ref.get('ref') == 'objects/' + digest + '.json', 'unsafe_object_ref')
    path = Path(repo_root).resolve() / MANIFEST_REL.parent / ref['ref']
    _require(path.is_file() and not path.is_symlink() and path.stat().st_size <= MAX_PLAN_BYTES, 'object_missing_or_oversize')
    raw = path.read_bytes()
    _require(_sha(raw) == digest, 'object_hash_mismatch')
    plan = json.loads(raw)
    _require(plan.get('schema') == PLAN_SCHEMA and plan.get('identity') == identity, 'plan_identity')
    return copy.deepcopy(plan)


def question_source_visuals(repo_root, identity):
    return _read_plan(repo_root, 'question_plans', identity)


def point_source_visuals(repo_root, code):
    return _read_plan(repo_root, 'point_plans', code)


def reference_rebinding(repo_root, code, source_ref, observed_sha256, *, proof_cache=None):
    """Admit only one explicitly proven old/new source pair, never a file class.

    The original reviewed declaration stays untouched. This maintenance table
    records equivalence of the exact fields read by one template. A future byte
    change or unavailable/tampered proof returns no exception to the stale gate.
    """
    manifest = _manifest(repo_root)
    table = (manifest or {}).get('reference_rebindings', {})
    if table.get('schema') != 'cs408-visual-reference-rebindings-v1':
        return None
    expected = {'code': code, 'source_path': source_ref.get('path'),
                'declared_sha256': source_ref.get('sha256'), 'observed_sha256': observed_sha256}
    key = _sha(json.dumps(expected, ensure_ascii=False, sort_keys=True, separators=(',', ':')).encode())
    entry = table.get('entries', {}).get(key)
    if not isinstance(entry, dict) or any(entry.get(k) != v for k, v in expected.items()):
        return None
    proof_ref = entry.get('proof', {})
    path = Path(str(proof_ref.get('path') or ''))
    digest = proof_ref.get('sha256')
    try:
        if (not path.is_absolute() or not path.is_file() or path.is_symlink()
                or any(p.is_symlink() for p in path.parents)
                or not isinstance(digest, str) or not re.fullmatch(r'[0-9a-f]{64}', digest)):
            return None
        stat = path.stat()
        if stat.st_size > 32 * 1024 * 1024:
            return None
        cache = {} if proof_cache is None else proof_cache
        cache_key = (str(path), stat.st_size, stat.st_mtime_ns, stat.st_ctime_ns, digest)
        if cache_key not in cache:
            raw = path.read_bytes()
            if _sha(raw) != digest:
                return None
            cache[cache_key] = json.loads(raw)
        proof = cache[cache_key]
        if proof.get('schema') != 'cs408-visual-reference-equivalence-proof-v1':
            return None
        comparison = proof.get('comparisons', {}).get(key, {})
        if (any(comparison.get(k) != v for k, v in expected.items())
                or comparison.get('status') != 'equivalent'
                or comparison.get('semantic_projection') not in {
                    'whole_syllabus_context_record', 'point_links_except_global_source_stamps',
                    'profile_short_summary_and_subpoint_titles'}
                or not re.fullmatch(r'[0-9a-f]{64}', str(comparison.get('old_semantic_sha256') or ''))
                or comparison.get('old_semantic_sha256') != comparison.get('new_semantic_sha256')):
            return None
    except (OSError, ValueError, TypeError, AttributeError):
        return None
    return copy.deepcopy(entry)


def _atomic(path, raw):
    path = Path(path)
    _require(not path.is_symlink(), 'output_symlink')
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, name = tempfile.mkstemp(prefix='.' + path.name + '-', dir=path.parent)
    try:
        with os.fdopen(fd, 'wb') as stream:
            stream.write(raw)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(name, path)
    finally:
        if os.path.exists(name):
            os.unlink(name)


def _object(root, value):
    raw = _raw(value)
    _require(len(raw) <= MAX_PLAN_BYTES, 'plan_too_large:' + value['identity'])
    digest = _sha(raw)
    ref = 'objects/' + digest + '.json'
    path = root / ref
    if path.exists():
        _require(path.read_bytes() == raw, 'immutable_conflict')
    else:
        _atomic(path, raw)
    return {'ref': ref, 'sha256': digest}


def _asset(path, *, asset_id, role, publication_scope, extra=None):
    path = Path(path).resolve()
    _require(path.is_file(), 'asset_missing:' + str(path))
    raw = path.read_bytes()
    from PIL import Image
    with Image.open(path) as img:
        img.verify()
    with Image.open(path) as img:
        width, height = img.size
    details = _paths.details_root()
    portable = {'published_path': 'details/' + path.relative_to(details).as_posix()} if path.is_relative_to(details) else {}
    return {'asset_id': asset_id, 'path': str(path), 'sha256': _sha(raw),
            'role': role, 'publication_scope': publication_scope,
            'width': width, 'height': height, **portable, **(extra or {})}


def _rect(value):
    _require(isinstance(value, list) and len(value) == 4 and all(type(n) in (int, float) and math.isfinite(n) for n in value), 'region_rect')
    x, y, w, h = value
    _require(x >= 0 and y >= 0 and w > 0 and h > 0 and x + w <= 1.000001 and y + h <= 1.000001, 'region_out_of_bounds')
    return value


def _verify_crop(original, image, box):
    """Prove the retained pixels are a real crop, not a redrawn source figure."""
    from PIL import Image
    with Image.open(original) as source, Image.open(image) as result:
        if box is None:
            expected = source.convert('RGB')
        else:
            _require(isinstance(box, list) and len(box) == 4 and all(type(n) is int for n in box), 'pixel_crop_box')
            x0, y0, x1, y1 = box
            _require(0 <= x0 < x1 <= source.width and 0 <= y0 < y1 <= source.height, 'pixel_crop_bounds')
            expected = source.crop(box).convert('RGB')
        observed = result.convert('RGB')
        _require(expected.size == observed.size and expected.tobytes() == observed.tobytes(), 'crop_pixels_not_source')


def _boundary(template, contexts):
    points = template.get('knowledge_points', [])
    entries = []
    for code in points:
        item = contexts.get(code, {})
        entries.append({'code': code, 'scope_status': item.get('scope_status', 'needs_review'),
            'teaching_boundary': item.get('teaching_boundary', '未命中范围；只解释已核验题设所需机制，不拓展。'),
            'syllabus_paths': [[n['title'] for n in e.get('path', [])] for e in item.get('syllabus_entries', [])]})
    return {'year': 2027, 'knowledge_points': points, 'entries': entries,
            'policy': '以同次syllabus_context为最终边界；out_of_scope不展开，prerequisite_only只补本题必要前提，needs_review保留待核。'}


POLICY = {
    'version': 'linear-text-primary-408-v1',
    'chat_linear_text_required': True, 'visible_main_text_required': True, 'visual_secondary': True,
    'layout': '先在聊天与页面正文中顺序讲清本轮获准的条件、动作和因果；对应可点击图解辅助核对，主线理由无需点击即可完整阅读。源图只供后台参考。',
    'redraw': '先核对当前真题图，保留有辨识意义的布局、对象、编号、字段、连接方向和初态；在对应解题步骤上清晰重绘或标注。不得把多重表、流水时间格、硬件线路等换成通用概念盒图，也不以模糊底图加空泛热点代替讲解。',
    'text': '聊天线性正文为主，页面文字主线默认可读；点击只补局部细节，不替代正文。公式按聊天原生数学排版。',
    'answers': '教师材料不等于公开授权；所有区域、解释、状态与图像先按本轮提示级过滤，折叠不算保护。',
    'identity': '独立小问只编译当前问；父题整图和兄弟问仅作教师核对，不可直接发送。',
    'evidence': '维护与点击不是作答或掌握证据；effectiveness=unverified。',
}


def semantic_source_binding(plan):
    """Fingerprint exactly the source material used to author one redraw."""
    keys = ('identity', 'scope', 'status', 'source_question_identity', 'figure_status',
            'assets', 'overview', 'region_bindings', 'source_surface_ids',
            'syllabus_boundary', 'teacher_states', 'authored_mechanism',
            'authored_mechanism_evidence', 'review_notes', 'exam_visual_binding', 'source_visual_plan_review')
    return _sha(_raw({key: plan[key] for key in keys if key in plan}))


def carry_semantic_redesign(current, previous):
    """Keep reviewed redraws only while their original source binding holds."""
    if not previous.get('presentation_policy'):
        return None
    current['presentation_policy'] = previous['presentation_policy']
    authored = any(key in previous for key in ('teaching_design', 'narrative_plan'))
    if not authored:
        return None
    binding = previous.get('semantic_source_binding')
    if binding != semantic_source_binding(previous) or binding != semantic_source_binding(current):
        current['semantic_design_status'] = 'source_changed_requires_current_redraw'
        return False
    for key in ('teaching_design', 'narrative_plan', 'semantic_source_binding'):
        if key in previous:
            current[key] = copy.deepcopy(previous[key])
    return True


def build(repo_root, details_root, review_root, atlas_root, *, report_path=None):
    """Activate all reviewed plans atomically, after complete source validation."""
    repo, details = Path(repo_root).resolve(), Path(details_root).resolve()
    reviews, atlas = Path(review_root).resolve(), Path(atlas_root).resolve()
    _require(atlas.is_relative_to(details), 'atlas_must_be_details_hot_assets')
    corpus = details / 'corpus/408统考真题'
    with (corpus / 'indexes/question-index.csv').open(encoding='utf-8-sig') as stream:
        rows = list(csv.DictReader(stream))
    accepted = json.loads((repo / 'support/syllabus-2027/exam-corpus-acceptance.json').read_text())
    _require(len(rows) == accepted['question_count'], 'corpus_count_mismatch')
    templates_index = json.loads((repo / '.private/question-visual-408/current.json').read_text())
    templates = {}
    for identity, ref in templates_index['templates'].items():
        raw = (repo / '.private/question-visual-408' / ref['ref']).read_bytes()
        _require(_sha(raw) == ref['sha256'], 'template_hash_mismatch')
        templates[identity] = json.loads(raw)
    contexts = json.loads((repo / 'support/syllabus-2027/context.json').read_text())['contexts']
    subject_reviews, all_reviews = {}, {}
    for subject in ('DS', 'CO', 'OS', 'CN'):
        path = reviews / subject / 'reviews.json'
        data = json.loads(path.read_bytes())
        _require(data.get('schema') == 'cs408-figure-review-v1' and data.get('subject') == subject, 'review_schema:' + subject)
        expected = {r['exam_question_id'] for r in rows if r['subject'] == subject}
        _require(set(data['questions']) == expected, 'review_exact_coverage:' + subject)
        subject_reviews[subject] = {'path': str(path), 'sha256': _sha(path.read_bytes()), 'questions': len(expected)}
        all_reviews.update(data['questions'])
    plans, inventory, copied, region_count = {}, {}, {}, 0
    for row in rows:
        identity = row['exam_question_id']
        review = all_reviews[identity]
        _require(review.get('review_status') == 'visually_reviewed', 'not_visually_reviewed:' + identity)
        _require(review.get('figure_status') in ('figures_extracted', 'no_diagram'), 'unresolved_figure:' + identity)
        template = templates[identity]
        assets, regions = [], []
        figures = review.get('figures', [])
        _require(bool(figures) == (review['figure_status'] == 'figures_extracted'), 'figure_status_conflict:' + identity)
        figure_ids = set()
        for figure in figures:
            fid = figure['figure_id']
            _require(isinstance(fid, str) and re.fullmatch(r'[A-Za-z0-9_.:-]+', fid) and fid not in figure_ids, 'figure_identity')
            figure_ids.add(fid)
            original = Path(figure['source_path']).resolve()
            source_hash = _sha(original.read_bytes())
            _require(source_hash == figure['source_sha256'], 'figure_original_drift:' + identity)
            path = Path(figure['image_path']).resolve()
            raw = path.read_bytes()
            digest = _sha(raw)
            _require(digest == figure['sha256'], 'figure_hash_mismatch:' + identity)
            _verify_crop(original, path, figure.get('crop_box'))
            destination = atlas / 'figures' / (digest + path.suffix.lower())
            if destination.exists():
                _require(destination.read_bytes() == raw, 'hot_copy_conflict')
            else:
                _atomic(destination, raw)
            copied[digest] = str(destination)
            asset_id = identity + ':' + fid
            asset = _asset(destination, asset_id=asset_id, role='question_figure',
                publication_scope='current_question_after_disclosure_review',
                extra={'title': figure['title'], 'kind': figure.get('kind', 'source_figure'),
                       'source_path': str(original), 'source_sha256': source_hash,
                       'crop_box': figure.get('crop_box'), 'review_status': 'visually_reviewed'})
            for key in ('source_document_path', 'source_document_sha256', 'source_page', 'source_render_path',
                        'crop_coordinate_space', 'source_origin_kind', 'source_review_note'):
                if key in figure:
                    asset[key] = figure[key]
            for key in ('shared_condition_source_identity', 'shared_condition_surface_sha256'):
                if key in figure:
                    asset[key] = figure[key]
            assets.append(asset)
            inventory[asset_id] = asset
            _require(len(figure.get('regions', [])) >= 2, 'figure_has_no_object_interaction:' + asset_id)
            for region in figure['regions']:
                _require(isinstance(region.get('explanation'), str) and region['explanation'].strip(), 'region_explanation')
                _require(type(region.get('min_level')) is int and 0 <= region['min_level'] <= 5, 'region_release_level')
                regions.append({'region_id': fid + ':' + region['id'], 'asset_id': asset_id,
                    'label': region['label'], 'rect': _rect(region['rect']), 'explanation': region['explanation'],
                    'min_level': region['min_level'], 'relation_region_ids': [fid + ':' + r for r in region.get('relation_region_ids', [])],
                    'teacher_use': '先在原图定位该对象；展开对应解释；只高亮已经核对的结构关联，不自动推因果边。'})
        region_ids = {r['region_id'] for r in regions}
        _require(len(region_ids) == len(regions), 'duplicate_region:' + identity)
        for region in regions:
            _require(set(region['relation_region_ids']) <= region_ids, 'dangling_region:' + identity)
        surfaces = []
        for role, field in [('question_surface', 'question_assets'), ('solution_surface', 'solution_assets')]:
            for i, rel in enumerate(json.loads(row[field]), 1):
                aid = identity + ':' + role + ':' + str(i)
                asset = _asset(details / rel, asset_id=aid, role=role,
                    publication_scope='teacher_only_never_publish_whole_parent' if role == 'question_surface' else 'teacher_only_answer_material')
                inventory[aid] = asset
                surfaces.append(aid)
        overview = copy.deepcopy(review.get('overview', []))
        _require(isinstance(overview, list) and overview and all(v.get('label') and v.get('detail') for v in overview), 'overview_missing:' + identity)
        # Reviewed explanatory prose remains teacher material. A teacher must
        # actively lower a release level for a protected learning turn.
        for item in overview:
            item.setdefault('min_level', 5)
        plan = {'schema': PLAN_SCHEMA, 'identity': identity, 'scope': 'exact_question',
            'status': 'source_bound', 'source_question_identity': identity,
            'figure_status': review['figure_status'], 'assets': assets, 'overview': overview,
            'region_bindings': regions, 'source_surface_ids': surfaces,
            'syllabus_boundary': _boundary(template, contexts), 'delivery_policy': POLICY,
            'effectiveness': 'unverified', 'review_notes': review.get('notes', [])}
        if review.get('teacher_states'):
            plan['teacher_states'] = copy.deepcopy(review['teacher_states'])
        mechanism_path = reviews / row['subject'] / (identity + '-structured-mechanism.json')
        if mechanism_path.is_file():
            mechanism = json.loads(mechanism_path.read_bytes())
            _require(mechanism.get('schema') == 'cs408-structured-mechanism-example-v1'
                     and mechanism.get('question_identity') == identity
                     and mechanism.get('teacher_only') is True and mechanism.get('min_level') == 5,
                     'authored_mechanism_binding:' + identity)
            plan['authored_mechanism'] = mechanism
            plan['authored_mechanism_evidence'] = {'path': str(mechanism_path), 'sha256': _sha(mechanism_path.read_bytes())}
        plans[identity] = plan
        region_count += len(regions)
    subindex = json.loads((corpus / 'subquestions/index.json').read_bytes())
    subitems = subindex.get('items', subindex.get('subquestions', [])) if isinstance(subindex, dict) else subindex
    if isinstance(subitems, dict):
        subitems = list(subitems.values())
    sub_by_id = {i.get('subquestion_id', i.get('question_id', i.get('unit_id', i.get('id')))): i for i in subitems}
    for identity, template in templates.items():
        if identity in plans:
            continue
        parent = template.get('parent_identity')
        if template.get('identity_kind') == 'subquestion' and parent in plans:
            item = sub_by_id.get(identity)
            _require(item is not None, 'subquestion_missing:' + identity)
            assets, scoped_geometry = [], []
            for i, rel in enumerate(item.get('condition_assets', []), 1):
                condition = (details / rel).resolve()
                condition_sha = _sha(condition.read_bytes())
                refined = [a for a in plans[parent]['assets'] if a.get('source_path') == str(condition)
                           or a['sha256'] == condition_sha]
                if refined:
                    # A reviewed crop can replace a formerly over-wide shared
                    # condition image. No parent explanation or answer is copied.
                    for j, figure in enumerate(refined, 1):
                        child_asset_id = identity + f':condition:{i}:{j}'
                        assets.append({**copy.deepcopy(figure), 'asset_id': child_asset_id,
                            'role': 'current_subquestion_condition_figure',
                            'publication_scope': 'only_current_subquestion_conditions',
                            'condition_binding': {'path': str(condition), 'sha256': condition_sha},
                            'region_binding_status': 'compile_only_current_subquestion_objects'})
                        for region in plans[parent]['region_bindings']:
                            if region['asset_id'] == figure['asset_id']:
                                scoped_geometry.append({'region_id': child_asset_id + ':' + region['region_id'],
                                    'asset_id': child_asset_id, 'label': region['label'], 'rect': region['rect'],
                                    'min_level': 5, 'binding_status': 'geometry_only_explanation_requires_current_subquestion',
                                    'teacher_use': '共享条件图的已核验对象位置；仅按本问target/steps绑定本问解释，不继承父题答案。',
                                    'detail_ref': 'current_question_visual_template.steps'})
                else:
                    assets.append(_asset(condition, asset_id=identity + ':condition:' + str(i),
                        role='current_subquestion_condition_figure', publication_scope='only_current_subquestion_conditions',
                        extra={'region_binding_status': 'compile_only_current_subquestion_objects'}))
            # Parent explanations never enter a subquestion plan, even when a
            # common-condition image is byte-identical to the parent crop.
            plans[identity] = {'schema': PLAN_SCHEMA, 'identity': identity, 'scope': 'exact_question',
                'status': 'source_bound', 'source_question_identity': identity, 'assets': assets,
                'figure_status': 'shared_condition_figures' if assets else 'reconstruct_current_conditions',
                'overview': [{'id': f'step-{i+1}', 'label': f'步骤{i+1}', 'detail_ref': f'steps.{i}',
                              'detail': step, 'min_level': 5} for i, step in enumerate(template['steps'])],
                'region_bindings': scoped_geometry, 'teacher_use': '按当前问真实目标编译条件图的热点；禁止从父题/兄弟问复制解释或结论。',
                'syllabus_boundary': _boundary(template, contexts), 'delivery_policy': POLICY, 'effectiveness': 'unverified'}
        else:
            plans[identity] = {'schema': PLAN_SCHEMA, 'identity': identity, 'scope': 'exact_question',
                'status': 'outside_true_exam_image_scope', 'assets': [], 'region_bindings': [], 'overview': [],
                'teacher_use': '本身份不属于本批真题图源；沿本题已核验场景做对象图，不能借同知识点真题冒充本题原图。',
                'syllabus_boundary': _boundary(template, contexts), 'delivery_policy': POLICY, 'effectiveness': 'unverified'}
    point_plans = {}
    for code, context in contexts.items():
        candidates = [qid for qid, t in templates.items() if qid in plans and plans[qid]['status'] == 'source_bound' and code in t.get('knowledge_points', [])]
        point_plans[code] = {'schema': PLAN_SCHEMA, 'identity': code, 'scope': 'point', 'status': 'indexed',
            'assets': [], 'overview': [], 'region_bindings': [], 'candidate_count': len(candidates),
            'source_question_ids': candidates, 'selection_policy': '这些是教师素材索引，不是旧题推荐；优先当前精确题；另荐旧题仍须实时14天门禁。',
            'syllabus_boundary': _boundary({'knowledge_points': [code]}, contexts),
            'delivery_policy': POLICY, 'effectiveness': 'unverified'}
    previous_manifest = _manifest(repo)
    revoked_designs = []
    if previous_manifest and previous_manifest.get('semantic_redesign'):
        for identity, plan in plans.items():
            previous = question_source_visuals(repo, identity)
            if previous and carry_semantic_redesign(plan, previous) is False:
                revoked_designs.append(identity)
        for identity, plan in point_plans.items():
            previous = point_source_visuals(repo, identity)
            if previous:
                carry_semantic_redesign(plan, previous)
    root = repo / MANIFEST_REL.parent
    manifest = {'schema': INDEX_SCHEMA, 'question_plans': {k: _object(root, v) for k, v in sorted(plans.items())},
        'point_plans': {k: _object(root, v) for k, v in sorted(point_plans.items())},
        'review_sources': subject_reviews, 'coverage': {'parent_questions': len(rows),
        'subquestions': sum(v.get('identity_kind') == 'subquestion' for v in templates.values()),
        'exact_plans': len(plans), 'point_plans': len(point_plans), 'figure_files': len(copied),
        'figure_instances': sum(len(p['assets']) for k, p in plans.items() if k in all_reviews),
        'regions': region_count, 'source_surfaces': sum(v['role'].endswith('_surface') for v in inventory.values()),
        'no_diagram_questions': sum(v['figure_status'] == 'no_diagram' for v in all_reviews.values())}}
    if previous_manifest and 'reference_rebindings' in previous_manifest:
        manifest['reference_rebindings'] = copy.deepcopy(previous_manifest['reference_rebindings'])
    if previous_manifest and previous_manifest.get('semantic_redesign'):
        manifest['semantic_redesign'] = copy.deepcopy(previous_manifest['semantic_redesign'])
        manifest['semantic_redesign']['redrawn_lessons'] = sum('teaching_design' in p for p in plans.values())
        manifest['semantic_redesign']['narrative_plans'] = sum('narrative_plan' in p for p in plans.values())
        if revoked_designs:
            manifest['semantic_redesign']['status'] = 'source_changed_requires_redesign'
            manifest['semantic_redesign']['source_changed_identities'] = sorted(revoked_designs)
    _atomic(atlas / 'inventory.json', _raw({'schema': 'cs408-figure-atlas-v1', 'assets': inventory, 'coverage': manifest['coverage']}))
    manifest['atlas_inventory'] = {'path': str(atlas / 'inventory.json'), 'sha256': _sha((atlas / 'inventory.json').read_bytes())}
    _atomic(repo / MANIFEST_REL, _raw(manifest))
    clear_cache()
    for identity in plans:
        _require(question_source_visuals(repo, identity) == plans[identity], 'plan_readback_failed')
    report = {'status': 'source_visual_plans_current', **manifest['coverage'], 'manifest_sha256': _sha((repo / MANIFEST_REL).read_bytes()),
              'formal_write_count': 0, 'learning_write_count': 0, 'effectiveness': 'unverified'}
    if report_path:
        import sys
        sys.path.insert(0, str(_paths.storage_map_root()))
        from cold_storage import ensure_output_path
        ensure_output_path(str(Path(report_path).resolve()), '408')
        _atomic(Path(report_path), _raw(report))
    return report


def compile_scene(plan, *, source_version, view, hint_level):
    """Legacy pixel scene, retained for historical verification only.

    Automatic compilation is deliberately direct-only. In guided teaching the
    current teacher selects/discloses the permitted objects and explanations in
    render_interactive_408's existing scene input instead of trusting a blanket
    after_attempt authorization. This also keeps parent/child isolation exact.
    """
    _require(view == 'direct' and hint_level == 5, 'automatic_scene_requires_direct_level_five')
    _require(plan.get('scope') == 'exact_question' and plan.get('status') == 'source_bound', 'scene_exact_plan_required')
    _require(plan.get('region_bindings') and plan.get('assets'), 'scene_requires_reviewed_regions')
    _require(all(r.get('explanation') for r in plan['region_bindings']), 'scene_current_subquestion_explanation_required')
    nodes, overview, diagrams, edges = [], [], [], []
    region_ids = {r['region_id']: 'region-' + str(i) for i, r in enumerate(plan['region_bindings'])}
    for i, item in enumerate(plan['overview']):
        nid = 'overview-' + str(i)
        nodes.append({'id': nid, 'label': {'text': item['label'], 'min_level': 5},
                      'detail': {'text': item['detail'], 'min_level': 5}})
        overview.append({'node_id': nid, 'label': {'text': item['label'], 'min_level': 5}})
        for rid in item.get('focus_region_ids', []):
            targets = [k for k in region_ids if k == rid or k.endswith(':' + rid)]
            for target in targets:
                edges.append({'id': 'focus-' + str(len(edges)), 'from': nid, 'to': region_ids[target],
                              'label': {'text': '本步定位', 'min_level': 5}, 'min_level': 5})
    for region in plan['region_bindings']:
        nid = region_ids[region['region_id']]
        nodes.append({'id': nid, 'label': {'text': region['label'], 'min_level': 5},
                      'detail': {'text': region['explanation'], 'min_level': 5}})
        for related in region['relation_region_ids']:
            # These are explicit reviewed associations, not generated arrows
            # claiming a physical direction of data or control.
            edges.append({'id': 'related-' + str(len(edges)), 'from': nid, 'to': region_ids[related],
                          'label': {'text': '对照关联对象', 'min_level': 5}, 'min_level': 5})
    for i, asset in enumerate(plan['assets']):
        if not any(r['asset_id'] == asset['asset_id'] for r in plan['region_bindings']):
            continue
        diagrams.append({'id': 'diagram-' + str(i), 'image_path': asset['path'], 'sha256': asset['sha256'],
            'alt': {'text': asset.get('title', '当前题原图'), 'min_level': 5}, 'min_level': 5,
            'hotspots': [{'id': 'hotspot-' + region_ids[r['region_id']], 'node_id': region_ids[r['region_id']],
                          'rect': r['rect'], 'min_level': 5}
                         for r in plan['region_bindings'] if r['asset_id'] == asset['asset_id']]})
    states = []
    teaching = plan.get('teacher_states', {})
    if teaching.get('schema') == 'cs408-aoe-teacher-states-v1':
        _validate_aoe(teaching)
        def region_node(suffix):
            matches = [node for key, node in region_ids.items() if key.endswith('::' + suffix)]
            _require(len(matches) == 1, 'aoe_region_not_unique:' + suffix)
            return matches[0]
        # Repeat only checked GIVEN labels as crisp DOM text over the preserved
        # raster. Explanatory prose is never drawn into the source image.
        given_labels = {region_node('node-' + str(e['event'])): str(e['event']) for e in teaching['events']}
        given_labels.update({region_node('activity-' + e['activity']): e['activity'] + '=' + str(e['duration']) for e in teaching['input_edges']})
        for diagram in diagrams:
            for hotspot in diagram['hotspots']:
                if hotspot['node_id'] in given_labels:
                    hotspot['display_label'] = {'text': given_labels[hotspot['node_id']], 'min_level': 5}
        states.append({'id': 'original', 'label': {'text': '先认清圆圈事件与箭头活动', 'min_level': 5},
                       'min_level': 5, 'values': {}, 'active_nodes': [], 'active_edges': []})
        earliest = {}
        for step in teaching['forward_steps']:
            earliest[str(step['event'])] = step['value']
            active = [region_node('node-' + str(step['event']))]
            active += [region_node('activity-' + c['activity']) for c in step['candidates']]
            values = {region_node('node-' + event): {'text': '最早时刻 ' + str(value), 'min_level': 5} for event, value in earliest.items()}
            label = '正推事件' + str(step['event']) + '：' + ('起点为0' if not step['candidates'] else
                    '前驱完成时刻 ' + '、'.join(str(c['candidate']) for c in step['candidates']) + '，取最大值 ' + str(step['value']))
            states.append({'id': 'forward-' + str(step['event']), 'label': {'text': label, 'min_level': 5},
                           'min_level': 5, 'values': values, 'active_nodes': active, 'active_edges': []})
        latest = {}
        for step in teaching['backward_steps']:
            latest[str(step['event'])] = step['value']
            values = {region_node('node-' + event): {'text': '最早 ' + str(earliest[event]) + '；最迟 ' + str(value), 'min_level': 5}
                      for event, value in latest.items()}
            label = '逆推事件' + str(step['event']) + '：' + ('固定工程截止时刻' if not step['candidates'] else
                    '后继给出的截止 ' + '、'.join(str(c['candidate']) for c in step['candidates']) + '，取最小值 ' + str(step['value']))
            states.append({'id': 'backward-' + str(step['event']), 'label': {'text': label, 'min_level': 5},
                           'min_level': 5, 'values': values,
                           'active_nodes': [region_node('node-' + str(step['event']))] + [region_node('activity-' + c['activity']) for c in step['candidates']],
                           'active_edges': []})
        for activity in teaching['activities']:
            nid = region_node('activity-' + activity['activity'])
            values = {nid: {'text': '最早开始 ' + str(activity['earliest_start']) + '；最迟开始 ' + str(activity['latest_start']) + '；时间余量 ' + str(activity['slack']), 'min_level': 5}}
            states.append({'id': 'slack-' + activity['activity'], 'label': {'text': '对照活动' + activity['activity'] + '的两个开始时刻', 'min_level': 5},
                           'min_level': 5, 'values': values,
                           'active_nodes': [nid, region_node('node-' + activity['tail_event']), region_node('node-' + activity['head_event'])], 'active_edges': []})
    return {'schema': 'cs408-interactive-scene-v1', 'identity': plan['identity'], 'source_version': source_version,
            'view': view, 'hint_level': hint_level, 'title': {'text': plan['identity'] + ' · 原图逐对象讲解', 'min_level': 0},
            'nodes': nodes, 'edges': edges, 'overview': overview, 'source_diagrams': diagrams, 'states': states}


def _validate_aoe(value):
    """Recompute the authored AOE state table rather than trust its assertions."""
    _require(value.get('min_level') == 5, 'aoe_answer_level')
    edges, order = value['input_edges'], value['topological_order']
    positions = {v: i for i, v in enumerate(order)}
    _require(len(positions) == len(order) and all(e['tail'] in positions and e['head'] in positions and
             positions[e['tail']] < positions[e['head']] and e['duration'] >= 0 for e in edges), 'aoe_order')
    earliest = {}
    for node in order:
        earliest[node] = max([earliest[e['tail']] + e['duration'] for e in edges if e['head'] == node] or [0])
    duration = max(earliest.values())
    latest = {}
    for node in reversed(order):
        latest[node] = min([latest[e['head']] - e['duration'] for e in edges if e['tail'] == node] or [duration])
    _require(value['project_duration'] == duration, 'aoe_duration')
    _require(all(e['earliest'] == earliest[e['event']] and e['latest'] == latest[e['event']] for e in value['events']), 'aoe_event_values')
    by_name = {e['activity']: e for e in edges}
    for activity in value['activities']:
        edge = by_name[activity['activity']]
        first, last = earliest[edge['tail']], latest[edge['head']] - edge['duration']
        _require(activity['earliest_start'] == first and activity['latest_start'] == last and activity['slack'] == last - first, 'aoe_activity_values')
    for stage, expected in [('forward_steps', earliest), ('backward_steps', latest)]:
        _require(all(s['value'] == expected[s['event']] for s in value[stage]), 'aoe_step_values')


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('command', choices=['build', 'query', 'render'])
    parser.add_argument('--repo', type=Path, default=Path(__file__).resolve().parents[1])
    parser.add_argument('--details', type=Path, default=_paths.details_root())
    parser.add_argument('--review-root', type=Path)
    parser.add_argument('--atlas-root', type=Path)
    parser.add_argument('--identity')
    parser.add_argument('--view', choices=['protected', 'after_attempt', 'direct'])
    parser.add_argument('--hint-level', type=int)
    parser.add_argument('--source-version')
    parser.add_argument('--catalog-version')
    parser.add_argument('--output', type=Path)
    parser.add_argument('--inline-root', type=Path)
    args = parser.parse_args(argv)
    if args.command == 'query':
        result = question_source_visuals(args.repo, args.identity) or point_source_visuals(args.repo, args.identity)
    elif args.command == 'render':
        _require(args.identity and args.output is not None and args.source_version, 'render_arguments_required')
        _require(args.view == 'direct' and args.hint_level == 5, 'automatic_scene_requires_direct_level_five')
        from exam_lesson_408 import EXAM
        from local_quality_408 import catalog, render_exam_lesson
        index,_=catalog(args.repo)
        if EXAM.fullmatch(args.identity) and (index.get('exam_lesson_audit') or args.catalog_version):
            result = render_exam_lesson(args.repo,args.identity,args.output,view=args.view,hint_level=args.hint_level,
                                        inline_root=args.inline_root,
                                        expected_version=args.catalog_version)
        else:
            from render_semantic_408 import render
            plan = question_source_visuals(args.repo, args.identity)
            lesson = (plan or {}).get('teaching_design')
            _require(lesson is not None, 'semantic_design_required_compile_current_scope')
            _require(lesson.get('identity') == args.identity and '-P' not in args.identity,
                     'semantic_design_exact_parent_required')
            _require(lesson.get('identity') == args.identity and '-P' not in args.identity,
                     'semantic_design_exact_parent_required')
            result = render(lesson, args.output, source_version=args.source_version,
                            view=args.view, hint_level=args.hint_level, inline_root=args.inline_root)
    else:
        _require(args.review_root is not None and args.atlas_root is not None, 'build_paths_required')
        result = build(args.repo, args.details, args.review_root, args.atlas_root)
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == '__main__':
    main()
