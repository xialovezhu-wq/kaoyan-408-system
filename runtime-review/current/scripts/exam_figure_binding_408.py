"""Source-reviewed exam structure for teacher compilation, never a student scene."""
from __future__ import annotations
import copy
import re

SCHEMA = 'cs408-exam-visual-binding-v1'
BASES = {'current_question_figure', 'current_question_table', 'source_solution_diagram',
         'same_mechanism_exam_figure', 'text_only_mechanism', 'source_unavailable'}
ROLES = {'question_figure', 'solution_diagram', 'textbook_figure', 'question_surface',
         'solution_surface', 'condition_figure'}


def validate_binding(value, *, steps=None):
    if value is None:
        return None
    def require(ok, reason):
        if not ok:
            raise ValueError('exam_figure_binding_' + reason)
    require(isinstance(value, dict) and value.get('schema') == SCHEMA, 'schema')
    require(value.get('basis') in BASES, 'basis')
    require(value.get('teacher_only') is True and value.get('frontend_ready') is False
            and value.get('effectiveness') == 'unverified', 'teacher_boundary')
    assets = value.get('source_assets')
    require(isinstance(assets, list), 'source_assets')
    for asset in assets:
        require(isinstance(asset, dict) and isinstance(asset.get('path'), str) and asset['path']
                and re.fullmatch(r'[0-9a-f]{64}', str(asset.get('sha256', '')))
                and asset.get('role') in ROLES, 'asset_reference')
    if value['basis'] in {'current_question_figure', 'current_question_table',
                           'source_solution_diagram', 'same_mechanism_exam_figure'}:
        require(bool(assets), 'visual_basis_without_source')
    require(isinstance(value.get('preserve'), list) and all(isinstance(x, str) and x.strip()
            for x in value['preserve']), 'preserved_structure')
    require(isinstance(value.get('focus_by_step'), list), 'step_focus')
    for focus in value['focus_by_step']:
        require(isinstance(focus, dict), 'focus_shape')
        match = re.fullmatch(r'Q([1-9][0-9]*)', str(focus.get('step_id', '')))
        require(match is not None, 'question_step_identity')
        index = int(match.group(1)) - 1
        if steps is not None:
            require(index < len(steps), 'question_step_range')
        if focus.get('source_step_ref'):
            require(focus['source_step_ref'] == 'steps.' + str(index), 'conflicting_step_reference')
        for field in ('what_to_show', 'state_change', 'return_to_reasoning'):
            require(isinstance(focus.get(field), str) and focus[field].strip(), 'focus_' + field)
    return value


def source_refs(value):
    """Allow the existing cold source guards to verify actual bound bytes."""
    if not value:
        return []
    result = []
    for asset in value.get('source_assets', []):
        result.append({'path': asset['path'], 'sha256': asset['sha256'],
                       'locator': 'exam_visual_binding:' + str(asset.get('question_identity', '')),
                       'role': asset['role']})
    return result


def project_binding(value):
    if not value:
        return None
    validate_binding(value)
    result = {key: copy.deepcopy(value[key]) for key in ('schema', 'basis', 'preserve',
        'focus_by_step', 'teacher_only', 'frontend_ready', 'effectiveness',
        'no_original_figure_note', 'disclosure') if key in value}
    result['source_assets'] = [{key: copy.deepcopy(asset[key]) for key in
        ('path', 'sha256', 'question_identity', 'role', 'pdf_path', 'pdf_page', 'pdf_pages', 'published_path')
        if key in asset} for asset in value.get('source_assets', [])]
    result['instruction'] = ('以本题原图的对象、布局、标号、连接和初态为骨架，只在对应步骤增加高亮或状态变化；'
        '原图无图时明确标为机制示意。教师配方与解析图不得直接序列化进学生前端。')
    return result


def step_visuals(value):
    """These mappings are authored after source review, not token matching."""
    if not value:
        return []
    return [{'step_id': f['step_id'], 'return_to_step_id': f['step_id'],
        'binding_status': 'source_reviewed_exact_step', 'source': 'exam_visual_binding',
        'recipe': {'what_to_show': f['what_to_show'], 'state_change': f['state_change'],
            'return_to_reasoning': f['return_to_reasoning'], 'preserve': copy.deepcopy(value['preserve']),
            'source_assets': copy.deepcopy(project_binding(value)['source_assets'])},
        'use': 'compile_current_disclosure_from_the_verified_exam_structure_not_generic_boxes'}
        for f in value.get('focus_by_step', [])]
