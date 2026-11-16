"""Exact-question visual lesson proposals; no learning or scheduling writes.

Only an explicitly supplied, reviewed catalog can activate templates. Cold builds
close its immutable object graph. Hot readers select one hash route and never
search the question corpus, textbook, or neighboring subquestions.
"""
from __future__ import annotations

import argparse
import copy
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import tempfile

from visual_teaching_templates_408 import _source_guard, source_guards_current, source_visual_guards, DELIVERY
from interactive_teaching_408 import attach, validate_recipe

SCHEMA = 'cs408-question-visual-template-v1'
INDEX_SCHEMA = 'cs408-question-visual-index-v1'
ROOT_REL = Path('.private/question-visual-408')
MANIFEST_REL = ROOT_REL / 'current.json'
MAX_TEMPLATE_BYTES = 256 * 1024
MAX_INDEX_BYTES = 8 * 1024 * 1024
IDENTITY_KINDS = {'parent_exam', 'subquestion', 'formal_question', 'chapter_question', 'teaching_part'}
HOT_FIELDS = {'schema', 'question_identity', 'identity_kind', 'parent_identity', 'subject',
    'knowledge_points', 'given_objects', 'initial_state', 'target', 'visual_kind', 'scene',
    'steps', 'solution_anchor', 'checks', 'adaptation_rules', 'source_review_status',
    'effectiveness', 'review_notes', 'prerequisites', 'source_part_label', 'grading_policy', 'interaction',
    'source_visuals', 'exam_visual_binding'}


def _bytes(value):
    return (json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':')) + '\n').encode()


def _require(condition, reason):
    if not condition:
        raise ValueError('question_visual_' + reason)


def valid_identity(value):
    return isinstance(value, str) and bool(value.strip()) and value == value.strip() and len(value) <= 200 and not any(ord(c) < 32 for c in value)


def validate_template(value, identity, expected_ids):
    _require(isinstance(value, dict) and value.get('schema') == SCHEMA, 'schema_invalid:' + identity)
    _require(identity in expected_ids and value.get('question_identity') == identity, 'unregistered_identity:' + identity)
    _require(value.get('identity_kind') in IDENTITY_KINDS, 'identity_kind_invalid:' + identity)
    _require(value.get('subject') in {'DS', 'CO', 'OS', 'CN'}, 'subject_invalid:' + identity)
    parent = value.get('parent_identity')
    if value['identity_kind'] in {'subquestion', 'teaching_part'}:
        _require(parent in expected_ids and parent != identity, 'parent_invalid:' + identity)
    else:
        _require(parent is None or parent in expected_ids, 'parent_invalid:' + identity)
    for key in ('initial_state', 'target', 'visual_kind', 'scene', 'solution_anchor'):
        _require(isinstance(value.get(key), str) and value[key].strip(), 'text_required:' + identity + ':' + key)
    for key in ('knowledge_points', 'steps', 'checks', 'adaptation_rules'):
        _require(isinstance(value.get(key), list) and value[key] and all(isinstance(v, str) and v.strip() for v in value[key]), 'items_required:' + identity + ':' + key)
    _require(isinstance(value.get('given_objects'), list) and value['given_objects'], 'given_objects_required:' + identity)
    from exam_figure_binding_408 import validate_binding
    validate_binding(value.get('exam_visual_binding'), steps=value['steps'])
    _require(value.get('source_review_status') in {'reviewed', 'source_limited'}, 'review_status_invalid:' + identity)
    _require(value.get('effectiveness') == 'unverified', 'effectiveness_is_not_learning_evidence:' + identity)
    if 'interaction' in value:
        validate_recipe(value['interaction'], value, allow_legacy_policy=True)
    if 'grading_policy' in value:
        policy = value['grading_policy']
        _require(isinstance(policy, dict) and type(policy.get('allowed')) is bool
                 and isinstance(policy.get('reason'), str) and policy['reason'].strip(),
                 'grading_policy_invalid:' + identity)
    refs = value.get('source_refs')
    _require(isinstance(refs, list) and refs, 'source_refs_required:' + identity)
    for ref in refs:
        _require(isinstance(ref, dict) and isinstance(ref.get('path'), str) and ref['path'].strip()
                 and isinstance(ref.get('locator'), str) and ref['locator'].strip(), 'source_ref_invalid:' + identity)
        if ref.get('sha256') is not None:
            _require(isinstance(ref['sha256'], str) and re.fullmatch(r'[0-9a-f]{64}', ref['sha256']), 'source_sha_invalid:' + identity)
    _require(len(_bytes(value)) <= MAX_TEMPLATE_BYTES, 'template_too_large:' + identity)
    return value


def _scope(catalog, base):
    expected = catalog.get('expected_ids')
    if expected is None and catalog.get('scope_ref'):
        ref = catalog['scope_ref']
        _require(isinstance(ref, dict) and isinstance(ref.get('path'), str), 'scope_reference_invalid')
        path = Path(ref['path']); path = path if path.is_absolute() else base / path
        raw = path.read_bytes()
        _require(hashlib.sha256(raw).hexdigest() == ref.get('sha256'), 'scope_hash_mismatch')
        value = json.loads(raw)
        expected = value.get('expected_ids') if isinstance(value, dict) else value
    _require(isinstance(expected, list) and all(valid_identity(v) for v in expected)
             and len(expected) == len(set(expected)), 'expected_ids_invalid')
    return set(expected)


def _aliases(catalog, expected):
    aliases = catalog.get('aliases', {})
    _require(isinstance(aliases, dict), 'aliases_invalid')
    normalized = {}
    for alias, targets in aliases.items():
        _require(valid_identity(alias), 'alias_invalid')
        targets = [targets] if isinstance(targets, str) else targets
        _require(isinstance(targets, list) and targets and all(t in expected for t in targets), 'alias_target_unregistered:' + alias)
        targets = sorted(set(targets))
        _require(alias not in expected or targets == [alias], 'alias_shadows_identity:' + alias)
        normalized[alias] = targets
    # Route normalization is case insensitive; reject distinct registrations
    # that would silently overwrite each other in a bucket.
    seen = {}
    import unicodedata
    for alias, targets in [(v, [v]) for v in expected] + list(normalized.items()):
        key = re.sub(r'\s+', ' ', unicodedata.normalize('NFKC', alias)).strip().lower()
        _require(key not in seen or seen[key] == targets, 'alias_normalization_collision:' + alias)
        seen[key] = targets
    return normalized


def _read(path, limit):
    _require(path.is_file() and not path.is_symlink() and path.stat().st_size <= limit, 'file_missing_or_unsafe:' + str(path))
    return json.loads(path.read_bytes())


def observed_guards_current(guards):
    """Freshness of observations, including an already withdrawn missing source.

    A missing source is never reviewed content. Keeping its absence as a guard
    lets unrelated personal records refresh; its reappearance invalidates that
    observation and requires another cold comparison with the admitted hash.
    """
    for guard in guards:
        if guard.get('source_missing'):
            if Path(guard['path']).is_file():
                return False
        elif not source_guards_current([guard]):
            return False
    return True


def _observe_source(root, ref, cache):
    path = Path(ref['path'])
    path = path if path.is_absolute() else root / path
    if not path.is_file():
        return {'path': str(path.absolute()), 'source_missing': True}
    # Observe the current file separately from the immutable reviewed assertion.
    # A declared hash mismatch is a per-template semantic invalidation, not a
    # failure of the entire cold personal-summary build.
    return _source_guard(root, {'path': ref['path']}, cache)


def load_catalog(repo_root, *, verify_sources=True):
    if repo_root is None or not (Path(repo_root) / MANIFEST_REL).exists():
        return {'templates': {}, 'guards': {}, 'aliases': {}, 'expected_ids': [], 'source_bindings': {}, 'source_reviews': {}}
    root = Path(repo_root)
    manifest = _read(root / MANIFEST_REL, MAX_INDEX_BYTES)
    _require(manifest.get('schema') == INDEX_SCHEMA and isinstance(manifest.get('templates'), dict), 'index_schema_invalid')
    expected = _scope(manifest, root / ROOT_REL)
    aliases = _aliases(manifest, expected)
    bindings = manifest.get('source_bindings', {})
    _require(isinstance(bindings, dict), 'source_bindings_invalid')
    templates, guards, cache, source_reviews = {}, {}, {}, {}
    scoped_source_cache = {}
    for identity, reference in manifest['templates'].items():
        _require(isinstance(reference, dict) and isinstance(reference.get('sha256'), str)
                 and reference.get('ref') == 'objects/' + reference['sha256'] + '.json'
                 and re.fullmatch(r'[0-9a-f]{64}', reference['sha256']), 'object_reference_invalid:' + identity)
        path = root / ROOT_REL / reference['ref']
        value = _read(path, MAX_TEMPLATE_BYTES)
        _require(hashlib.sha256(path.read_bytes()).hexdigest() == reference['sha256'], 'object_hash_mismatch:' + identity)
        templates[identity] = validate_template(value, identity, expected)
        guards[identity] = [_source_guard(root, {'path': str(path), 'sha256': reference['sha256']}, cache)]
        issues = []
        if verify_sources:
            admitted = bindings.get(identity, [])
            _require(isinstance(admitted, list) and (not admitted or len(admitted) == len(value['source_refs'])),
                     'source_binding_shape_invalid:' + identity)
            for offset, ref in enumerate(value['source_refs']):
                bound = admitted[offset] if admitted else {}
                _require(isinstance(bound, dict) and (not bound or bound.get('path') == ref['path']),
                         'source_binding_identity_invalid:' + identity)
                expected_sha = ref.get('sha256') or bound.get('sha256')
                if expected_sha is not None:
                    _require(isinstance(expected_sha, str) and re.fullmatch(r'[0-9a-f]{64}', expected_sha),
                             'source_binding_hash_invalid:' + identity)
                observed = _observe_source(root, ref, cache)
                guards[identity].append(observed)
                if observed.get('source_missing') or not expected_sha or observed['sha256'] != expected_sha:
                    issues.append({'path': ref['path'], 'locator': ref['locator'],
                        'reason': 'source_missing' if observed.get('source_missing') else
                                  'admitted_source_hash_missing' if not expected_sha else 'source_hash_changed',
                        'declared_sha256': ref.get('sha256'), 'admitted_sha256': expected_sha,
                        'observed_sha256': observed.get('sha256')})
                from scoped_question_sources_408 import scoped_metadata_issue
                scoped_issue = scoped_metadata_issue(root, value, ref, cache=scoped_source_cache)
                if scoped_issue:
                    issues.append(scoped_issue)
        if verify_sources and value.get('exam_visual_binding'):
            from exam_figure_binding_408 import source_refs as exam_figure_refs
            for ref in exam_figure_refs(value['exam_visual_binding']):
                observed = _observe_source(root, ref, cache)
                guards[identity].append(observed)
                if observed.get('source_missing') or observed.get('sha256') != ref['sha256']:
                    issues.append({'path': ref['path'], 'locator': ref['locator'],
                        'reason': 'source_missing' if observed.get('source_missing') else 'source_hash_changed',
                        'declared_sha256': ref['sha256'], 'observed_sha256': observed.get('sha256')})
        source_reviews[identity] = {'status': 'stale' if issues else 'current', 'issues': issues}
    return {'templates': templates, 'guards': guards, 'aliases': aliases, 'expected_ids': sorted(expected),
            'source_bindings': bindings, 'source_reviews': source_reviews}


def materialize(repo_root, object_ref, *, research_catalog=None, syllabus_contexts=None, assessment_catalog=None):
    from source_visual_408 import question_source_visuals, _manifest as source_visual_manifest
    from teaching_research_408 import POLICY, effective_route
    from lesson_alignment_408 import compile_exact_bridge

    loaded = load_catalog(repo_root)
    entries, visual_guard_cache = {}, {}
    source_visual_count = 0
    for identity in loaded['expected_ids']:
        value = loaded['templates'].get(identity)
        if value is None:
            entries[identity] = {'question_identity': identity, 'status': 'missing',
                'teaching_delivery_policy': copy.deepcopy(POLICY)}
            continue
        full_ref = object_ref('question-visual-evidence', value)
        review = loaded['source_reviews'][identity]
        grading = {'grading_policy': copy.deepcopy(value['grading_policy'])} if 'grading_policy' in value else {}
        if review['status'] == 'stale':
            entries[identity] = {**grading, 'question_identity': identity, 'status': 'stale',
                'reprocess_required': True, 'knowledge_points': value['knowledge_points'],
                'teaching_delivery_policy': copy.deepcopy(POLICY),
                'source_guards': loaded['guards'][identity], 'source_issues': review['issues'],
                'evidence_object': full_ref}
            continue
        projection = {k: copy.deepcopy(v) for k, v in value.items() if k in HOT_FIELDS}
        # Optimistic concurrency tokens for the reviewed portable return. A
        # projected teaching-object hash is not its native predecessor version.
        projection['template_native_version'] = full_ref['sha256']
        projection['source_visual_plan_version'] = None
        if projection.get('exam_visual_binding'):
            from exam_figure_binding_408 import project_binding
            projection['exam_visual_binding'] = project_binding(projection['exam_visual_binding'])
        source_visuals = question_source_visuals(repo_root, identity)
        guards = list(loaded['guards'][identity])
        if source_visuals is not None:
            from semantic_teaching_408 import project_for_teaching
            projection['source_visuals'] = project_for_teaching(source_visuals)
            projection['source_visual_plan_version'] = (
                (source_visual_manifest(repo_root) or {}).get('question_plans', {}).get(identity, {}).get('sha256'))
            guards.extend(source_visual_guards(repo_root, source_visuals, visual_guard_cache))
            source_visual_count += 1
        # Bind exact-question preparation to the current point routes without
        # injecting a whole parent lesson or sibling answers into the scene.
        projection['teaching_delivery_policy'] = copy.deepcopy(POLICY)
        if research_catalog:
            projection['linear_teaching_route_refs'] = [
                {'knowledge_point': code,
                 'route_object': object_ref('derived-teaching-routes', effective_route(research_catalog['targets'][code])),
                 'scope': 'teacher_only_compile_current_exact_question_and_current_personal_summary'}
                for code in value['knowledge_points'] if code in research_catalog['targets']]
        projection = attach(projection)
        projection['lesson_bridge'] = compile_exact_bridge(projection,
            research_routes=[effective_route(research_catalog['targets'][code])
                for code in value['knowledge_points']
                if research_catalog and code in research_catalog['targets']],
            syllabus_contexts=syllabus_contexts, assessment_catalog=assessment_catalog)
        projection.update(origin='source_backed_teaching_proposal_not_learner_evidence',
            delivery_instruction=DELIVERY, independent_mastery_inferred=False)
        projection['evidence_object'] = full_ref
        # A local Pxx is an exact navigation identity, not proof of the original
        # printed subquestion number. Keep both kinds explicit and omit any
        # inferred source label (and all child solution content).
        parts = []
        for child, part in loaded['templates'].items():
            kind = part.get('identity_kind')
            if kind not in {'subquestion', 'teaching_part'} or part.get('parent_identity') != identity:
                continue
            navigation = dict(question_identity=child, identity_kind=kind,
                part_id=child.rsplit('#' if kind == 'teaching_part' else '-', 1)[-1])
            if part.get('source_part_label'):
                navigation['source_part_label'] = part['source_part_label']
            parts.append(navigation)
        if parts: projection['parts'] = sorted(parts, key=lambda v: v['question_identity'])
        entries[identity] = {**grading, 'question_identity': identity, 'status': value['source_review_status'],
            **{key: projection[key] for key in ('template_native_version', 'source_visual_plan_version')},
            'knowledge_points': value['knowledge_points'], 'source_guards': guards,
            **object_ref('question-visual-templates', projection)}
    return {'entries': entries, 'aliases': loaded['aliases'], 'coverage': {
        'question_visual_expected_count': len(loaded['expected_ids']),
        'question_visual_template_count': len(loaded['templates']),
        'question_source_visual_plan_count': source_visual_count,
        'question_visual_stale_count': sum(v['status'] == 'stale' for v in loaded['source_reviews'].values()),
        'question_visual_current_count': sum(v['status'] == 'current' for v in loaded['source_reviews'].values()),
        'question_visual_missing_count': len(loaded['expected_ids']) - len(loaded['templates']),
        'question_visual_source_limited_count': sum(v['source_review_status'] == 'source_limited' for v in loaded['templates'].values())}}


def source_state(routed, view):
    entry = routed.get('question_visual_entry') or {}
    return view == 'protected' or observed_guards_current(entry.get('source_guards') or [])


def read_template(routed, view, read_artifact, *, unchanged=False, sources_current=True,
                  personal_summaries=None, binding_only=False):
    identity = routed.get('question_visual_identity')
    entry = routed.get('question_visual_entry') or {}
    grading = {'grading_policy': copy.deepcopy(entry['grading_policy'])} if 'grading_policy' in entry else {}
    safe_grading = {'grading_policy': {'allowed': entry['grading_policy']['allowed'],
        'reason': 'source_not_eligible_for_scoring' if entry['grading_policy']['allowed'] is False
                  else 'source_grading_policy'}} if grading else {}
    if view == 'protected':
        # No object pointer, source locator, parameters, or template metadata that
        # permits bypassing pre-answer protection is included.
        return {'status': 'protected', **safe_grading} if identity or routed.get('question_visual_reason') else None
    if not identity:
        reason = routed.get('question_visual_reason')
        return {'status': 'unavailable', 'reason': reason} if reason else None
    entry = routed.get('question_visual_entry') or {}
    if not sources_current or entry.get('status') == 'stale':
        from teaching_research_408 import POLICY
        return {'status': 'stale', **safe_grading, 'question_identity': identity, 'reprocess_required': True,
                'reason': 'question_visual_reference_source_changed',
                'teaching_delivery_policy': copy.deepcopy(POLICY)}
    if not entry.get('ref'):
        return {'status': 'missing', 'question_identity': identity, 'reason': 'exact_question_not_preprocessed'}
    if binding_only:
        return {'status': entry['status'], **safe_grading, 'question_identity': identity,
                'binding_only': True, **{key: entry[key] for key in
                    ('template_native_version', 'source_visual_plan_version') if entry.get(key)}}
    if unchanged:
        return {'status': 'unchanged', **grading, 'question_identity': identity}
    value = read_artifact(entry['ref'], entry['sha256'], 'question visual template', limit=MAX_TEMPLATE_BYTES)
    _require(value.get('schema') == SCHEMA and value.get('question_identity') == identity, 'query_binding_invalid')
    if value.get('lesson_bridge'):
        from lesson_alignment_408 import bind_current_personal
        value = copy.deepcopy(value)
        value['lesson_bridge'] = bind_current_personal(value['lesson_bridge'], personal_summaries)
    return {**copy.deepcopy(value), 'status': entry['status']}


def _atomic(path, raw):
    path.parent.mkdir(parents=True, exist_ok=True)
    _require(not path.is_symlink() and not any(p.is_symlink() for p in path.parents), 'output_symlink')
    fd, name = tempfile.mkstemp(prefix='.' + path.name, dir=path.parent)
    try:
        with os.fdopen(fd, 'wb') as stream:
            stream.write(raw); stream.flush(); os.fsync(stream.fileno())
        os.replace(name, path)
    finally:
        if os.path.exists(name): os.unlink(name)


def import_catalog(repo_root, catalog_path, *, activate=True):
    root, path = Path(repo_root).resolve(), Path(catalog_path).resolve()
    catalog = _read(path, 256 * 1024 * 1024)
    _require(catalog.get('schema') == INDEX_SCHEMA and isinstance(catalog.get('templates'), dict), 'catalog_schema_invalid')
    expected = _scope(catalog, path.parent)
    aliases = _aliases(catalog, expected)
    values, cache, incoming_bindings = {}, {}, {}
    for identity, value in catalog['templates'].items():
        validate_template(value, identity, expected)
        incoming_bindings[identity] = [{'path': ref['path'], 'sha256': _source_guard(root, ref, cache)['sha256']}
                                      for ref in value['source_refs']]
        values[identity] = value
    result = {'status': 'validated', 'expected_count': len(expected), 'input_count': len(values),
        'formal_write_count': 0, 'learning_write_count': 0}
    if not activate: return result
    destination = root / ROOT_REL
    destination.mkdir(parents=True, exist_ok=True)
    with (destination / '.import.lock').open('a+') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        current = load_catalog(root, verify_sources=False)
        if current['expected_ids']:
            _require(set(current['expected_ids']) <= expected, 'scope_shrink_rejected')
        merged = {**current['templates'], **values}
        _require(source_guards_current(list(cache.values())), 'source_changed_before_activation')
        merged_bindings = {**current['source_bindings'], **incoming_bindings}
        merged_aliases = {**current['aliases'], **aliases}
        _aliases({'aliases': merged_aliases}, expected)
        refs = {}
        for identity, value in sorted(merged.items()):
            raw = _bytes(value); digest = hashlib.sha256(raw).hexdigest()
            ref = 'objects/' + digest + '.json'; target = destination / ref
            if target.exists(): _require(target.read_bytes() == raw, 'immutable_object_conflict')
            else: _atomic(target, raw)
            refs[identity] = {'ref': ref, 'sha256': digest}
        manifest = {'schema': INDEX_SCHEMA, 'expected_ids': sorted(expected),
            'templates': refs, 'aliases': merged_aliases, 'source_bindings': merged_bindings}
        raw = _bytes(manifest); target = root / MANIFEST_REL
        unchanged = target.is_file() and target.read_bytes() == raw
        if not unchanged: _atomic(target, raw)
        loaded = load_catalog(root)
        _require(loaded['templates'] == merged, 'import_readback_failed')
    return {**result, 'status': 'unchanged' if unchanged else 'imported', 'template_count': len(merged),
        'missing_count': len(expected) - len(merged), 'manifest': str(target), 'sha256': hashlib.sha256(raw).hexdigest()}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('command', choices=['validate', 'import'])
    parser.add_argument('--repo', type=Path, default=Path(__file__).resolve().parents[1])
    parser.add_argument('--catalog', type=Path, required=True)
    args = parser.parse_args(argv)
    print(json.dumps(import_catalog(args.repo, args.catalog, activate=args.command == 'import'), ensure_ascii=False, indent=2))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
