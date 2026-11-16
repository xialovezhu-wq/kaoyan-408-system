"""Render independently authored 408 mechanism models, never scanned-page UI.

Stored teaching models are whole-answer materials (level five). A teacher may
author a separate, already filtered local model at a lower release level. The
renderer never downgrades a stored model or hides unreleased prose in its DOM.
"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
import re

from render_interactive_408 import _write_fragment
from source_visual_408 import _validate_aoe

ASSET = Path(__file__).resolve().parents[1] / 'support/semantic-408/lesson.html'


def public_lesson(lesson, *, source_version, view, hint_level):
    from semantic_teaching_408 import validate_lesson
    validate_lesson(lesson)
    if view not in {'protected', 'after_attempt', 'direct'}:
        raise ValueError('semantic_lesson_view_required')
    if type(hint_level) is not int or not 0 <= hint_level <= 5:
        raise ValueError('semantic_lesson_hint_level_required')
    if (view == 'protected' and hint_level != 0) or (view == 'after_attempt' and hint_level >= 5):
        raise ValueError('semantic_lesson_view_level_mismatch')
    release = lesson.get('release_level', 5)
    if release > hint_level or (release == 5 and view != 'direct'):
        raise ValueError('semantic_lesson_not_authorized_at_current_hint_level')
    if not isinstance(source_version, str) or not source_version:
        raise ValueError('semantic_lesson_source_version_required')
    # Enumerated presentation fields only. Proofs, file paths, source crops,
    # hashes and correction inventories are backend evidence, not UI content.
    result = {k: lesson[k] for k in ('schema', 'identity', 'subject', 'title', 'lead',
              'scope_note', 'initial_stage') if k in lesson}
    result['source_version'] = source_version
    match = re.fullmatch(r'EXAM408-(\d{4})-Q(\d+)', lesson['identity'])
    result['scope_note'] = (f"依据{match.group(1)}年第{int(match.group(2))}题的条件重绘；只展示当前概念所需的结构。"
                            if match else '按当前题设重绘；只展示当前概念所需的结构。')
    result['panels'] = []
    for panel in lesson['panels']:
        result['panels'].append({**{k: panel[k] for k in ('id', 'title', 'kind', 'height')},
            'nodes': [{k: node[k] for k in ('id', 'label', 'x', 'y', 'w', 'h', 'shape', 'value', 'explanation') if k in node}
                      for node in panel['nodes']],
            'links': [{k: edge[k] for k in ('id', 'from', 'to', 'label', 'direction', 'kind') if k in edge}
                      for edge in panel.get('links', [])]})
    result['stages'] = [{k: stage[k] for k in ('id', 'label', 'explanation', 'focus_nodes', 'focus_links', 'values') if k in stage}
                        for stage in lesson['stages']]
    for panel in result['panels']:
        for node in panel['nodes']:
            node['explanation'] = {k: node['explanation'][k] for k in ('observe', 'reason', 'takeaway')}
    for stage in result['stages']:
        stage['explanation'] = {k: stage['explanation'][k] for k in ('observe', 'reason', 'takeaway')}
    # The complete authorized reasoning stays visible independently of clicks.
    # Existing stage explanations are already release-gated as one lesson; new
    # authors may supply a more detailed ordered text tied to these stage IDs.
    result['linear_steps'] = [{k: step[k] for k in ('stage_id', 'title', 'text', 'return_to_reasoning') if k in step}
        for step in lesson.get('linear_steps', [])]
    result['linear_text_origin'] = ('authored_reasoning' if result['linear_steps']
                                    else 'visual_stage_summary_only')
    if not result['linear_steps']:
        result['linear_steps'] = [{'stage_id': stage['id'], 'title': stage['label'],
            'text': '\n'.join(dict.fromkeys(stage['explanation'][k] for k in ('observe', 'reason', 'takeaway')))}
            for stage in result['stages']]
    result['delivery_policy_version'] = 'linear-text-primary-408-v1'
    if lesson.get('aoe'):
        if release != 5 or view != 'direct':
            raise ValueError('semantic_aoe_full_states_require_direct_level_five')
        _validate_aoe(lesson['aoe'])
        value = lesson['aoe']
        result['aoe'] = {'topological_order': value['topological_order'], 'project_duration': value['project_duration'],
            'input_edges': [{k: e[k] for k in ('activity', 'tail', 'head', 'duration')} for e in value['input_edges']],
            'events': [{k: e[k] for k in ('event', 'earliest', 'latest')} for e in value['events']],
            'activities': [{k: e[k] for k in ('activity', 'tail_event', 'head_event', 'duration', 'earliest_start', 'latest_start', 'slack')}
                           for e in value['activities']]}
    return result


def render(lesson, output, *, source_version, view, hint_level, inline_root=None):
    data = public_lesson(lesson, source_version=source_version, view=view, hint_level=hint_level)
    encoded = json.dumps(data, ensure_ascii=False, separators=(',', ':')).replace('<', '\\u003c').replace('\u2028', '\\u2028').replace('\u2029', '\\u2029')
    uid = 'teaching-' + hashlib.sha256(encoded.encode()).hexdigest()[:16]
    fragment = ASSET.read_text().replace('__ROOT_ID__', uid).replace('__LESSON_DATA__', encoded)
    if '<img' in fragment.lower() or 'data:image/' in fragment.lower():
        raise ValueError('semantic_lesson_raster_backdrop_forbidden')
    result = _write_fragment(fragment, output, inline_root=inline_root)
    return {**result, 'schema': 'cs408-semantic-render-receipt-v1', 'identity': data['identity'],
            'presentation': 'redrawn_semantic_diagram', 'raster_images': 0,
            'panels': len(data['panels']), 'stages': len(data['stages']),
            'nodes': sum(len(p['nodes']) for p in data['panels']),
            'learning_write_count': 0, 'linear_text_visible_without_interaction': True,
            'linear_text_origin': data['linear_text_origin'],
            'requires_separate_question_mainline': data['linear_text_origin'] == 'visual_stage_summary_only',
            'chat_linear_text': '\n\n'.join(step['title'] + '\n' + step['text'] +
                ('\n' + step['return_to_reasoning'] if step.get('return_to_reasoning') else '')
                for step in data['linear_steps']),
            'chat_text_must_be_delivered_separately': True}
