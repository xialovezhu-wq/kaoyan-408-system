"""Validate and install mechanism redraws without changing learning evidence.

Scanned exam figures remain teacher references. A semantic lesson describes
editable objects, real relations and causal narration, never pixel hotspots.
The installer swaps only the derived source-visual manifest after all subjects
and exact parent identities have been checked.
"""
from __future__ import annotations

import argparse
import copy
import json
import math
from pathlib import Path
import re

import source_visual_408 as source

LESSON_SCHEMA = 'cs408-semantic-lesson-v1'
MODELS_SCHEMA = 'cs408-semantic-lessons-v1'
NARRATIVES_SCHEMA = 'cs408-semantic-narrative-plans-v1'
LEGACY_PRESENTATION_POLICY = '源图后台参考，面向机制重绘；口述自然因果讲解'
PRESENTATION_POLICY = '线性文字为主，可点击机制图为辅；源图后台参考'
SUBJECTS = ('DS', 'CO', 'OS', 'CN')
PARENT_RE = re.compile(r'EXAM408-\d{4}-Q\d{2}')
ID_RE = re.compile(r'[A-Za-z0-9_.:-]+')


def project_for_teaching(plan):
    """Do not feed rejected hotspot prose back into the live teacher context.

    The immutable source plan retains the original images and annotations. This
    projection carries the replacement lesson/narrative and image provenance,
    not a second, contradictory set of old click explanations.
    """
    value = copy.deepcopy(plan)
    # This ref belongs to the private source-plan object store, not the
    # portable tutoring graph. Keep it only in the immutable backend plan.
    value.pop('historical_plan_ref', None)
    value['delivery_policy'] = copy.deepcopy(source.POLICY)
    value['delivery_policy_version'] = 'linear-text-primary-408-v1'
    if value.get('exam_visual_binding'):
        from exam_figure_binding_408 import project_binding
        value['exam_visual_binding'] = project_binding(value['exam_visual_binding'])
        review = value.get('source_visual_plan_review') or {}
        if review.get('decision') == 'retire_old_design':
            for field in ('teaching_design', 'narrative_plan', 'overview', 'region_bindings', 'teacher_states'):
                value.pop(field, None)
        value['presentation_policy'] = PRESENTATION_POLICY
        value['exam_structure_priority'] = 'current_reviewed_exam_visual_binding'
    if value.get('presentation_policy') not in (PRESENTATION_POLICY, LEGACY_PRESENTATION_POLICY):
        return value
    value['presentation_policy'] = PRESENTATION_POLICY
    value['delivery_policy_version'] = 'linear-text-primary-408-v1'
    value.pop('region_bindings', None)
    value.pop('overview', None)
    value.pop('teacher_states', None)
    value.pop('source_surface_ids', None)
    if value.get('teaching_design'):
        value['overview'] = [{'id': row['id'], 'label': row['label'],
                              'detail_ref': 'teaching_design.stages.' + str(i)}
                             for i, row in enumerate(value['teaching_design']['stages'])]
    value['legacy_source_annotations'] = 'retained_as_backend_evidence_not_live_teaching'
    return value


def _require(value, message):
    if not value:
        raise ValueError('semantic_teaching_' + message)


def _text(value, name):
    _require(isinstance(value, str) and bool(value.strip()) and len(value) <= 12000, name)
    return value


def _identifier(value, name):
    _require(isinstance(value, str) and ID_RE.fullmatch(value), name)
    return value


def _explanation(value, name):
    _require(isinstance(value, dict), name)
    for key in ('observe', 'reason', 'takeaway'):
        _text(value.get(key), name + '.' + key)


def validate_lesson(lesson):
    """Return an independent validated value; default release is direct/L5.

    Lower release levels are only for a fresh teacher-authored safe partial
    scene. Installation separately refuses lowering a complete prepared model.
    """
    _require(isinstance(lesson, dict) and lesson.get('schema') == LESSON_SCHEMA, 'lesson_schema')
    value = copy.deepcopy(lesson)
    _identifier(value.get('identity'), 'identity')
    _require(value.get('subject') in SUBJECTS, 'subject')
    for key in ('title', 'lead', 'scope_note'):
        _text(value.get(key), key)
    release = value.setdefault('release_level', 5)
    _require(type(release) is int and 0 <= release <= 5, 'release_level')
    panels = value.get('panels')
    _require(isinstance(panels, list) and 1 <= len(panels) <= 12, 'panels')
    identifiers, node_ids, link_ids = set(), set(), set()

    def unique(identity):
        _identifier(identity, 'object_id')
        _require(identity not in identifiers, 'duplicate_object:' + identity)
        identifiers.add(identity)

    for panel in panels:
        _require(isinstance(panel, dict), 'panel')
        unique(panel.get('id'))
        _text(panel.get('title'), 'panel_title')
        _require(panel.get('kind') in ('graph', 'tree', 'flow', 'fields', 'table', 'timeline', 'memory'), 'panel_kind')
        _require(type(panel.get('height')) is int and 260 <= panel['height'] <= 420, 'panel_height')
        _require(isinstance(panel.get('nodes'), list) and bool(panel['nodes']), 'panel_nodes')
        local = set()
        for node in panel['nodes']:
            _require(isinstance(node, dict), 'node')
            unique(node.get('id'))
            local.add(node['id'])
            node_ids.add(node['id'])
            _text(node.get('label'), 'node_label')
            _require(node.get('shape') in ('circle', 'rect', 'diamond', 'field', 'buffer'), 'node_shape')
            for coordinate in ('x', 'y', 'w', 'h'):
                number = node.get(coordinate)
                _require(type(number) in (int, float) and math.isfinite(number) and 0 <= number <= 1,
                         'node_coordinate:' + coordinate)
            _require(node['w'] > 0 and node['h'] > 0, 'node_size')
            if 'value' in node:
                _require(isinstance(node['value'], str), 'node_value')
            _explanation(node.get('explanation'), 'node_explanation')
        _require(isinstance(panel.get('links'), list), 'panel_links')
        for link in panel['links']:
            _require(isinstance(link, dict), 'link')
            unique(link.get('id'))
            link_ids.add(link['id'])
            _require(link.get('from') in local and link.get('to') in local, 'link_endpoints')
            _require(isinstance(link.get('label'), str), 'link_label')
            _require(link.get('direction') in ('forward', 'both', 'none'), 'link_direction')
            _require(link.get('kind') in ('data', 'control', 'dependency', 'association'), 'link_kind')
    stages = value.get('stages')
    _require(isinstance(stages, list) and bool(stages), 'stages')
    stage_ids = set()
    for stage in stages:
        _require(isinstance(stage, dict), 'stage')
        unique(stage.get('id'))
        stage_ids.add(stage['id'])
        _text(stage.get('label'), 'stage_label')
        _explanation(stage.get('explanation'), 'stage_explanation')
        for field, allowed in (('focus_nodes', node_ids), ('focus_links', link_ids)):
            _require(isinstance(stage.get(field), list) and all(isinstance(x, str) for x in stage[field])
                     and set(stage[field]) <= allowed, 'stage_' + field)
        _require(isinstance(stage.get('values'), dict) and set(stage['values']) <= node_ids
                 and all(isinstance(x, str) for x in stage['values'].values()), 'stage_values')
    _require(value.get('initial_stage') in stage_ids, 'initial_stage')
    if 'linear_steps' in value:
        _require(isinstance(value['linear_steps'], list) and bool(value['linear_steps']), 'linear_steps')
        for step in value['linear_steps']:
            _require(isinstance(step, dict) and set(step) <= {'stage_id', 'title', 'text', 'return_to_reasoning'}, 'linear_step_fields')
            _require(step.get('stage_id') in stage_ids, 'linear_step_stage')
            _text(step.get('title'), 'linear_step_title')
            _text(step.get('text'), 'linear_step_text')
            if step.get('return_to_reasoning'):
                _text(step['return_to_reasoning'], 'linear_step_return')

    _require(isinstance(value.get('source_basis'), list) and bool(value['source_basis'])
             and all(isinstance(x, str) and ID_RE.fullmatch(x) for x in value['source_basis']), 'source_basis')
    if 'syllabus_boundary' in value:
        _require(isinstance(value['syllabus_boundary'], dict), 'syllabus_boundary')
    if 'aoe' in value:
        _require(release == 5 and isinstance(value['aoe'], dict), 'aoe_release')
        _require(value['aoe'].get('schema') == 'cs408-aoe-teacher-states-v1', 'aoe_schema')
        aoe = value['aoe']
        events = aoe.get('topological_order', [])
        edges = aoe.get('input_edges', [])
        _require(isinstance(events, list) and len(events) >= 2 and isinstance(edges, list) and bool(edges), 'aoe_inputs')
        activity_ids = [edge.get('activity') for edge in edges]
        _require(len(set(activity_ids)) == len(activity_ids), 'aoe_activity_identity')
        _require(len(aoe.get('events', [])) == len(events)
                 and {row.get('event') for row in aoe['events']} == set(events), 'aoe_complete_events')
        _require(len(aoe.get('activities', [])) == len(edges)
                 and {row.get('activity') for row in aoe['activities']} == set(activity_ids), 'aoe_complete_activities')
        for field in ('forward_steps', 'backward_steps'):
            _require(len(aoe.get(field, [])) == len(events)
                     and {row.get('event') for row in aoe[field]} == set(events), 'aoe_complete_' + field)
        source._validate_aoe(value['aoe'])
    return value


def _read(path):
    _require(path.is_file() and not path.is_symlink(), 'input_missing:' + str(path))
    raw = path.read_bytes()
    _require(len(raw) <= 32 * 1024 * 1024, 'input_oversize')
    return json.loads(raw), {'path': str(path), 'sha256': source._sha(raw)}


def _subject_map(repo, parents):
    root = repo / '.private/question-visual-408'
    index, _ = _read(root / 'current.json')
    result = {}
    for identity in parents:
        ref = index.get('templates', {}).get(identity, {})
        digest = ref.get('sha256', '')
        _require(re.fullmatch('[0-9a-f]{64}', digest) and ref.get('ref') == 'objects/' + digest + '.json',
                 'question_template_ref:' + identity)
        template, commitment = _read(root / ref['ref'])
        _require(commitment['sha256'] == digest and template.get('question_identity') == identity,
                 'question_template_binding:' + identity)
        _require(template.get('subject') in SUBJECTS, 'question_template_subject')
        result[identity] = template['subject']
    return result


def load_redesign(repo_root, redesign_root):
    """Read the complete batch and fail before any mutation on missing scope."""
    repo, batch = Path(repo_root).resolve(), Path(redesign_root).resolve()
    original_digest = source._sha((repo / source.MANIFEST_REL).read_bytes())
    manifest = source._manifest(repo)
    _require(manifest is not None, 'source_manifest_missing')
    plans = {identity: source.question_source_visuals(repo, identity) for identity in manifest['question_plans']}
    parents = {identity: plan for identity, plan in plans.items() if PARENT_RE.fullmatch(identity)}
    _require(len(parents) == manifest.get('coverage', {}).get('parent_questions'), 'parent_coverage')
    subjects = _subject_map(repo, parents)
    required = {identity for identity, plan in parents.items() if plan.get('figure_status') == 'figures_extracted'}
    if 'EXAM408-2023-Q46' in parents:
        required.add('EXAM408-2023-Q46')
    lessons, narratives, commitments = {}, {}, {}
    for subject in SUBJECTS:
        models, models_ref = _read(batch / subject / 'models.json')
        _require(models.get('schema') == MODELS_SCHEMA and models.get('subject') == subject
                 and isinstance(models.get('lessons'), dict), 'models_schema:' + subject)
        expected = {identity for identity in required if subjects[identity] == subject}
        _require(set(models['lessons']) == expected, 'model_exact_coverage:' + subject)
        for identity, original in models['lessons'].items():
            lesson = validate_lesson(original)
            _require(lesson['identity'] == identity and lesson['subject'] == subject, 'model_binding:' + identity)
            _require(lesson['release_level'] == 5, 'prepared_lesson_requires_level5:' + identity)
            _require(identity in lesson['source_basis'] and not any(
                item.startswith('EXAM408-') and item != identity for item in lesson['source_basis']),
                'model_source_scope:' + identity)
            # The authoritative syllabus and authored AOE arithmetic remain
            # bound to this exact source plan, never a model's paraphrase.
            lesson['syllabus_boundary'] = copy.deepcopy(parents[identity]['syllabus_boundary'])
            if parents[identity].get('teacher_states'):
                lesson['aoe'] = copy.deepcopy(parents[identity]['teacher_states'])
            lessons[identity] = validate_lesson(lesson)
        narrative, narrative_ref = _read(batch / subject / 'narrative-plans.json')
        _require(narrative.get('schema') == NARRATIVES_SCHEMA and narrative.get('subject') == subject
                 and isinstance(narrative.get('plans'), dict), 'narrative_schema:' + subject)
        expected = {identity for identity in parents if subjects[identity] == subject}
        _require(set(narrative['plans']) == expected, 'narrative_exact_coverage:' + subject)
        for identity, plan in narrative['plans'].items():
            _require(isinstance(plan, dict) and plan.get('identity') == identity and plan.get('subject') == subject,
                     'narrative_binding:' + identity)
            for key in ('title', 'lead', 'scope_note'):
                _text(plan.get(key), 'narrative_' + key)
            _explanation(plan.get('explanation'), 'narrative_explanation')
            _require(type(plan.get('release_level', 5)) is int and plan.get('release_level', 5) == 5, 'narrative_release')
            narratives[identity] = {**copy.deepcopy(plan), 'release_level': 5}
        commitments[subject] = {'models': models_ref, 'narratives': narrative_ref}
    report = {'status': 'semantic_redesign_validated', 'parent_questions': len(parents),
              'redrawn_lessons': len(lessons), 'narrative_plans': len(narratives),
              'exact_plans': len(plans), 'point_plans': len(manifest['point_plans']),
              'formal_write_count': 0, 'learning_write_count': 0, 'effectiveness': 'unverified'}
    _require(source._sha((repo / source.MANIFEST_REL).read_bytes()) == original_digest,
             'source_manifest_changed_during_load')
    return {'manifest': copy.deepcopy(manifest), 'manifest_sha256': original_digest, 'plans': plans, 'lessons': lessons,
            'narratives': narratives, 'commitments': commitments, 'report': report}


def install_redesign(repo_root, redesign_root):
    """Activate semantic lessons while retaining every source and rebinding."""
    repo = Path(repo_root).resolve()
    loaded = load_redesign(repo, redesign_root)
    manifest, plans = loaded['manifest'], loaded['plans']
    previous_digest = loaded['manifest_sha256']
    for identity, plan in plans.items():
        plan['presentation_policy'] = PRESENTATION_POLICY
        plan['delivery_policy'] = copy.deepcopy(source.POLICY)
        if identity in loaded['lessons']:
            plan['teaching_design'] = loaded['lessons'][identity]
        if identity in loaded['narratives']:
            plan['narrative_plan'] = loaded['narratives'][identity]
        if identity in loaded['narratives'] or identity in loaded['lessons']:
            plan['semantic_source_binding'] = source.semantic_source_binding(plan)
            plan.pop('semantic_design_status', None)
        if '-P' in identity:
            _require('teaching_design' not in plan and 'narrative_plan' not in plan,
                     'child_requires_own_compiled_scene:' + identity)
            plan['teacher_use'] = '仅按当前小问条件、目标和允许提示重绘对象与机制；父题教案仅供后台核对，不复制父图、父题总览、兄弟答案或口述。'
    point_plans = {identity: source.point_source_visuals(repo, identity) for identity in manifest['point_plans']}
    for plan in point_plans.values():
        plan['presentation_policy'] = PRESENTATION_POLICY
        plan['delivery_policy'] = copy.deepcopy(source.POLICY)
    root = repo / source.MANIFEST_REL.parent
    manifest['question_plans'] = {key: source._object(root, value) for key, value in sorted(plans.items())}
    manifest['point_plans'] = {key: source._object(root, value) for key, value in sorted(point_plans.items())}
    manifest['semantic_redesign'] = {'schema': 'cs408-semantic-redesign-activation-v1',
        'presentation_policy': PRESENTATION_POLICY, 'sources': loaded['commitments'],
        'redrawn_lessons': len(loaded['lessons']), 'narrative_plans': len(loaded['narratives']),
        'parent_lesson_default_release_level': 5, 'effectiveness': 'unverified'}
    _require(source._sha((repo / source.MANIFEST_REL).read_bytes()) == previous_digest,
             'source_manifest_changed_during_install')
    source._atomic(repo / source.MANIFEST_REL, source._raw(manifest))
    source.clear_cache()
    for identity, expected in plans.items():
        _require(source.question_source_visuals(repo, identity) == expected, 'readback:' + identity)
    for identity, expected in point_plans.items():
        _require(source.point_source_visuals(repo, identity) == expected, 'readback:' + identity)
    return {**loaded['report'], 'status': 'semantic_redesign_current',
            'manifest_sha256': source._sha((repo / source.MANIFEST_REL).read_bytes())}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--repo', type=Path, default=Path(__file__).resolve().parents[1])
    parser.add_argument('--root', type=Path, required=True)
    parser.add_argument('--check-only', action='store_true')
    args = parser.parse_args(argv)
    result = load_redesign(args.repo, args.root)['report'] if args.check_only else install_redesign(args.repo, args.root)
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == '__main__':
    main()
