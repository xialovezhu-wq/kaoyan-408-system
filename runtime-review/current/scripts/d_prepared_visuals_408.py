"""Bind and read one exact D teaching lesson without displaying or recording study.

The optional index supplements an immutable native import; it never replaces
that import, its question order, answer references, or learning receipts.
Protected queries return before inspecting even the private index path.
"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
import re

from semantic_teaching_408 import validate_lesson

SCHEMA = 'cs408-d-prepared-visuals-v1'
HTML_SCHEMA = 'cs408-d-prepared-html-v1'
HTML_INDEX = 'private/prepared-visuals.json'
HTML_STAGES = ('hint-1', 'solution')
SHA_RE = re.compile(r'[0-9a-f]{64}')
EXAM_RE = re.compile(r'EXAM408-\d{4}-Q\d{2}(?:-P\d{2})?')
MAX_INDEX_BYTES = 1024 * 1024
MAX_IMPORT_BYTES = 32 * 1024 * 1024
MAX_LESSON_BYTES = 512 * 1024
MAX_NOTES_BYTES = 128 * 1024
MAX_HTML_BYTES = 1024 * 1024 - 1


def _require(condition, reason):
    if not condition:
        raise ValueError('D_prepared_visual_' + reason)


def _digest(raw):
    return hashlib.sha256(raw).hexdigest()


def _path(value, *, parent=None):
    _require(isinstance(value, str) and bool(value), 'path_missing')
    path = Path(value)
    _require(path.is_absolute() and path.is_file() and not path.is_symlink()
             and not any(ancestor.is_symlink() for ancestor in path.parents), 'file_invalid')
    if parent is not None:
        _require(path.resolve().is_relative_to(parent.resolve()), 'file_outside_index_directory')
    return path


def _bound_bytes(path_value, digest, *, limit, parent=None):
    _require(isinstance(digest, str) and SHA_RE.fullmatch(digest), 'hash_invalid')
    path = _path(path_value, parent=parent)
    _require(path.stat().st_size <= limit, 'file_too_large')
    raw = path.read_bytes()
    _require(len(raw) <= limit and _digest(raw) == digest, 'hash_drift')
    return path, raw


def relative_html(root, ref):
    """Verify the stage file inside the immutable D package."""
    _require(isinstance(ref, dict) and set(ref) == {'relative_path', 'sha256'}, 'html_reference_invalid')
    relative = Path(ref['relative_path'])
    _require(not relative.is_absolute() and '..' not in relative.parts and
             relative.parts[:1] == ('private',) and relative.suffix == '.html', 'html_path_invalid')
    path, raw = _bound_bytes(str(root / relative), ref['sha256'], limit=MAX_HTML_BYTES, parent=root)
    _require(bool(raw.strip()) and not re.search(rb'<!doctype\s|<\s*(?:html|head|body)(?:\s|>)', raw, re.I), 'html_fragment_required')
    _require(bool(re.search(rb'<(?:script|button|details|input|select)(?:\s|>)', raw, re.I)),
             'html_interactive_controls_required')
    return path


def stage_path(identity, stage):
    return f'private/visuals/{identity}/{stage}.html'


def verify_authored_html(root, manifest, selected):
    """Preflight every selected exact stage before the native D importer writes."""
    _require(manifest.get('prepared_visual_contract') == HTML_SCHEMA, 'html_contract_required')
    path = _path(str(root / HTML_INDEX), parent=root)
    _require(path.stat().st_size <= MAX_INDEX_BYTES, 'html_index_too_large')
    value = json.loads(path.read_bytes())
    _require(value.get('schema') == HTML_SCHEMA and value.get('source_commit') == manifest['source_commit'],
             'html_index_version_mismatch')
    rows = value['items']
    expected = {item['true_item_id']: item['surface_sha256'] for item in selected}
    _require(len(expected) == len(selected) and set(rows) == set(expected), 'html_selection_coverage_drift')
    files = {row['path']: row['sha256'] for row in manifest['files']}
    for identity, row in rows.items():
        _require(EXAM_RE.fullmatch(identity) and row['surface_sha256'] == expected[identity], 'html_surface_mismatch')
        stages = row['stages']
        guard = row['source_guard']
        _require(isinstance(guard, dict) and set(guard) <= {'template_native_version', 'source_visual_plan_version'}
                 and all(isinstance(sha, str) and SHA_RE.fullmatch(sha) for sha in guard.values()), 'html_source_guard_invalid')
        _require('solution' in stages and set(stages) <= set(HTML_STAGES), 'html_solution_required:' + identity)
        for stage, ref in stages.items():
            path = relative_html(root, ref)
            _require(path.relative_to(root).as_posix() == stage_path(identity, stage)
                     and files.get(ref['relative_path']) == ref['sha256'], 'html_stage_binding_drift:' + identity)
        if 'hint-1' in stages:
            _require(stages['hint-1']['sha256'] != stages['solution']['sha256'], 'html_hint_must_be_separate:' + identity)
    return rows


def load_current_html(root, entry, display, *, view, hint_level, teaching_context):
    if view not in {'after_attempt', 'direct'}:
        return {'status': 'protected'}
    identity = {'request_id': display['request_id'], 'true_item_id': display['true_item_id']}
    stage = None
    try:
        _require(entry.get('true_item_id') == display['true_item_id'] and
                 entry['surface_sha256'] == display['surface_sha256'], 'html_current_surface_mismatch')
        current = teaching_context.get('question_visual_template') or {}
        if (current.get('question_identity') == display['true_item_id'] and
                (current.get('status') == 'stale' or any(current.get(key) not in (None, sha)
                    for key, sha in entry['source_guard'].items()))):
            return {**identity, 'status': 'stale', 'reason': 'prepared_exact_question_visual_source_changed'}
        if view == 'direct' and type(hint_level) is int and hint_level == 5:
            stage = 'solution'
        elif view == 'after_attempt' and type(hint_level) is int and 1 <= hint_level <= 4:
            stage = 'hint-1'
        else:
            # The tutor knows the exact item has been prepared. No answer path or bytes are delivered yet.
            return {**identity, 'status': 'available', 'preprocessed': True,
                    'release_required': 'direct-L5-or-matching-hint'}
        ref = entry['stages'].get(stage)
        if ref is None:
            return {**identity, 'status': 'stage_not_prepared', 'stage': stage}
        _require(ref.get('relative_path') == stage_path(display['true_item_id'], stage), 'html_stage_binding_drift')
        path = relative_html(root, ref)
        return {**identity, 'status': 'available', 'stage': stage,
                'interactive_2d': {'path': str(path), 'sha256': ref['sha256']}, 'preprocessed': True}
    except (ValueError, OSError, KeyError, TypeError) as exc:
        return {**identity, 'status': 'unavailable', 'stage': stage, 'reason': str(exc)}


def validate_prepared_index(handoff):
    """Validate complete request/true-item binding, without reading lessons."""
    reference = handoff.get('prepared_visuals_ref')
    _require(isinstance(reference, dict), 'index_reference_missing')
    index_path, raw = _bound_bytes(reference.get('path'), reference.get('sha256'), limit=MAX_INDEX_BYTES)
    index = json.loads(raw)
    _require(isinstance(index, dict) and index.get('schema') == SCHEMA, 'index_schema')
    source_version = index.get('source_version')
    _require(isinstance(source_version, str) and SHA_RE.fullmatch(source_version), 'source_version_invalid')
    native_sha = handoff.get('native_import_sha256')
    _require(index.get('native_import_sha256') == native_sha, 'native_import_binding_drift')
    _, raw = _bound_bytes(handoff.get('native_import_receipt'), native_sha, limit=MAX_IMPORT_BYTES)
    plan = json.loads(raw)
    _require(isinstance(plan, dict), 'native_import_invalid')
    native_items = plan.get('learning_items', plan.get('supplemental_items'))
    _require(isinstance(native_items, list) and bool(native_items), 'native_items_invalid')
    bindings = []
    for item in native_items:
        _require(isinstance(item, dict) and isinstance(item.get('selection'), dict), 'native_item_invalid')
        request_id, true_item_id = item.get('request_id'), item['selection'].get('true_item_id')
        _require(isinstance(request_id, str) and bool(request_id)
                 and isinstance(true_item_id, str) and EXAM_RE.fullmatch(true_item_id), 'native_identity_invalid')
        bindings.append((request_id, true_item_id))
    request_ids = [row[0] for row in bindings]
    _require(len(set(request_ids)) == len(request_ids), 'native_request_duplicate')
    _require(request_ids == handoff.get('learning_request_ids', handoff.get('supplemental_request_ids')),
             'handoff_order_binding_drift')
    items = index.get('items')
    _require(isinstance(items, list) and all(isinstance(item, dict) for item in items), 'index_items_invalid')
    _require([(item.get('request_id'), item.get('true_item_id')) for item in items] == bindings,
             'exact_queue_coverage_drift')
    # Validate only reference syntax for unselected items. Opening a successor's
    # lesson or narration is forbidden in this hot read-only entrypoint.
    for item in items:
        _require(item.get('teaching_notes_role', 'current_question_teaching_notes')
                 in {'current_question_teaching_notes', 'current_question_corrections'}, 'teaching_notes_role_invalid')
        for label in ('lesson', 'teaching_notes'):
            filename, digest = item.get(label + '_path'), item.get(label + '_sha256')
            _require(isinstance(filename, str) and Path(filename).is_absolute(), 'item_path_invalid')
            _require(isinstance(digest, str) and SHA_RE.fullmatch(digest), 'item_hash_invalid')
    return index_path, index


def load_current(handoff, display, *, view, teaching_context):
    """Return only the selected level-five teacher lesson, or a safe status.

    An after-attempt read is private teacher context, not authorization to put
    its complete lesson into the student DOM. The renderer still enforces the
    current view/release level. Stale or invalid prepared content does not block
    the independently current native question template.
    """
    if view not in {'after_attempt', 'direct'}:
        return {'status': 'protected'}
    if handoff.get('prepared_visuals_ref') is None:
        return {'status': 'not_prepared'}
    identity = {'request_id': display.get('request_id'), 'true_item_id': display.get('true_item_id')}
    try:
        index_path, index = validate_prepared_index(handoff)
        current_source_version = teaching_context.get('source_version')
        if (teaching_context.get('status') not in {'available', 'unchanged'}
                or not isinstance(current_source_version, str) or not SHA_RE.fullmatch(current_source_version)):
            return {**identity, 'status': 'stale', 'reason': 'current_teaching_source_version_unavailable',
                    'private': True, 'use_current_question_template': True}
        if index['source_version'] != current_source_version:
            return {**identity, 'status': 'stale', 'reason': 'prepared_visual_source_version_drift',
                    'prepared_source_version': index['source_version'],
                    'current_source_version': current_source_version,
                    'private': True, 'use_current_question_template': True}
        item = next((row for row in index['items'] if row['request_id'] == identity['request_id']), None)
        _require(item is not None and item['true_item_id'] == identity['true_item_id'], 'current_identity_mismatch')
        lesson_path, raw = _bound_bytes(item['lesson_path'], item['lesson_sha256'],
                                       limit=MAX_LESSON_BYTES, parent=index_path.parent)
        lesson = validate_lesson(json.loads(raw))
        _require(lesson['identity'] == identity['true_item_id'], 'lesson_exact_identity_required')
        _require(lesson['release_level'] == 5, 'lesson_requires_level_five')
        _require(identity['true_item_id'] in lesson['source_basis'] and not any(
            value.startswith('EXAM408-') and value != identity['true_item_id'] for value in lesson['source_basis']),
            'lesson_source_scope_mismatch')
        notes_path, raw = _bound_bytes(item['teaching_notes_path'], item['teaching_notes_sha256'],
                                      limit=MAX_NOTES_BYTES, parent=index_path.parent)
        notes = raw.decode('utf-8')
        _require(bool(notes.strip()), 'teaching_notes_empty')
        return {**identity, 'status': 'available', 'source_version': current_source_version,
                'private': True, 'release_level': 5,
                'use': 'current_exact_question_teacher_material_not_frontend_disclosure',
                'index_ref': {'path': str(index_path), 'sha256': handoff['prepared_visuals_ref']['sha256']},
                'lesson_ref': {'path': str(lesson_path), 'sha256': item['lesson_sha256']},
                'teaching_notes_ref': {'path': str(notes_path), 'sha256': item['teaching_notes_sha256']},
                'source_version_match_is_not_semantic_verification': True,
                'lesson': lesson, 'teaching_notes': {'text': notes, 'truncated': False,
                    'role': item.get('teaching_notes_role', 'current_question_teaching_notes')}}
    except (ValueError, OSError, KeyError, TypeError) as exc:
        return {**identity, 'status': 'unavailable', 'reason': str(exc),
                'private': True, 'use_current_question_template': True}


def material_commitment(context):
    """Keep reuse tokens bounded; content hashes were rechecked before this."""
    if context.get('status') != 'available':
        return None
    return {key: context[key] for key in ('request_id', 'true_item_id', 'source_version',
                                          'index_ref', 'lesson_ref', 'teaching_notes_ref')}
