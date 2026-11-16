"""Verified portable 408 tutoring artifacts; snapshot queries never read live state."""
from __future__ import annotations

import base64
import copy
import hashlib
import json
import re
from pathlib import Path
from typing import Any

import exam_tutoring_context_408 as native
from knowledge_hot_routes_408 import resolve as resolve_hot_routes
from knowledge_tutoring_query_408 import project_points, omit_teaching_plan_for_budget

EXPORT_VERSION = 'study-personalization-408-v2'
PREFIX = 'personalization/408/'
REF = re.compile(r'(?:objects/[a-z-]+|receipts|manifests)/[0-9a-f]{64}\.json')
MAX_OBJECT_BYTES = 128 * 1024 * 1024


def _bytes(value):
    return native._canonical_bytes(value)


def _digest(value):
    return hashlib.sha256(_bytes(value)).hexdigest()


def _read(path: Path, sha: str | None = None):
    if path.is_symlink() or any(p.is_symlink() for p in path.parents):
        raise ValueError('snapshot_symlink_rejected')
    if path.stat().st_size > MAX_OBJECT_BYTES:
        raise ValueError('snapshot_object_too_large')
    raw = path.read_bytes()
    if sha and hashlib.sha256(raw).hexdigest() != sha:
        raise ValueError('snapshot_hash_mismatch:' + path.name)
    value = json.loads(raw)
    if not isinstance(value, dict):
        raise ValueError('snapshot_object_required')
    return value


def _refs(value):
    if isinstance(value, dict):
        for key, child in value.items():
            if key == 'ref' or key.endswith('_ref'):
                if isinstance(child, str) and child.startswith(('objects/', 'receipts/', 'manifests/')):
                    if not REF.fullmatch(child):
                        raise ValueError('snapshot_reference_invalid')
                    expected = value.get('sha256' if key == 'ref' else key[:-4] + '_sha256')
                    if expected != Path(child).stem:
                        raise ValueError('snapshot_reference_hash_invalid')
                    yield child
            yield from _refs(child)
    elif isinstance(value, list):
        for child in value:
            yield from _refs(child)


def _version(manifest):
    return _digest({'exporter': EXPORT_VERSION, 'source_version': manifest['source_version'],
                    'point_index_sha256': manifest['point_index_sha256'],
                    'knowledge_hot_route_set_sha256':
                        manifest.get('knowledge_hot_routes', {}).get('set_sha256')})


def current_identity(repo_root: Path) -> dict:
    """Fixed formal inputs and one manifest; no recursive corpus scan or cache rebuild."""
    root = Path(repo_root).expanduser().resolve()
    formal = root / native.MASTER_FILE
    formal_version = native._sha256_file(formal) if formal.is_file() else None
    try:
        manifest, _ = native._load_manifest(root / native.DEFAULT_STATE_REL)
        reasons = native._freshness_reasons(root, manifest)
        projection = manifest.get('history_source_projection')
        if not isinstance(projection, dict):
            # An older manifest kept the full registry inline; force one refresh
            # instead of silently reporting a stale personalization as ready.
            reasons = [*reasons, 'history_source_projection_requires_refresh']
        # An annotation is immutable/content addressed. Compare names only so a
        # newly sealed semantic unit cannot look ready before refresh-current.
        import knowledge_history_sources_408 as history
        if isinstance(projection, dict) and root == history.CANONICAL_REPO.resolve():
            folder = history.SEMANTIC_ROOT / 'annotations'
            current = {str(p) for p in folder.glob('*.json') if p.is_file()}
            recorded = {p for p in projection.get('semantic_annotation_paths') or []}
            if current != recorded:
                reasons.append('semantic_annotations_require_refresh')
        from concept_review_408 import session_paths
        current_sessions = {str(p) for p in session_paths(root)}
        recorded_sessions = set(projection.get('concept_review_session_paths') or []) if isinstance(projection, dict) else set()
        if current_sessions != recorded_sessions:
            reasons.append('concept_reviews_require_refresh')
        return {'status': 'stale' if reasons else 'ready', 'version': _version(manifest),
                'source_version': manifest['source_version'], 'formal_version': formal_version,
                'reasons': reasons}
    except (OSError, ValueError, KeyError, native.ExamTutoringContextError) as exc:
        return {'status': 'pending', 'version': None, 'formal_version': formal_version,
                'reasons': ['manifest_unavailable:' + type(exc).__name__]}


def _history_current(root: Path):
    """Reuse the native cold inventory proof without calling its cache writer."""
    import knowledge_history_sources_408 as history
    cache = root / history.STATE_REL
    if not cache.is_file():
        raise ValueError('history_source_proof_missing')
    cached = _read(cache)
    result = cached['result']
    if cached.get('result_sha256') != history._digest(history._json_bytes(result)):
        raise ValueError('history_source_proof_drift')
    from intake_lib_408 import Repo
    signature = history._digest(history._json_bytes({
        'version': history.VERSION,
        'implementation_sha256': history.implementation_sha256(),
        'master': Repo(root).master(),
        'inventory': [history._guard(p) for p in history._inventory(root, root == history.CANONICAL_REPO.resolve())]}))
    if signature != cached.get('signature'):
        raise ValueError('history_inventory_requires_refresh')
    for row in result['source_guards']:
        actual = history._guard(Path(row['path']))
        if any(actual[key] != row[key] for key in ('path', 'size', 'mtime_ns', 'ctime_ns')):
            raise ValueError('history_source_requires_refresh')


def export_snapshot(repo_root: Path, output_dir: Path) -> dict:
    root = Path(repo_root).expanduser().resolve()
    state = root / native.DEFAULT_STATE_REL
    identity = current_identity(root)
    if identity['status'] != 'ready':
        raise ValueError('personalization_not_ready:' + ','.join(identity['reasons']))
    manifest, native_sha = native._load_manifest(state)
    if native._cold_card_freshness_reasons(root, state, manifest):
        raise ValueError('formal_cards_require_refresh')
    _history_current(root)
    output = Path(output_dir).expanduser()
    if output.is_symlink() or any(p.is_symlink() for p in output.parents):
        raise ValueError('snapshot_output_symlink_rejected')
    output = output.resolve()
    if output == state or output.is_relative_to(state) or state.is_relative_to(output):
        raise ValueError('snapshot_output_overlaps_native_state')
    # Only immutable refs reachable from the native manifest are copied. Never
    # rglob state, drafts, old generations, source paths or receipt sidecars.
    objects = {}
    pending = list(_refs(manifest))
    while pending:
        ref = pending.pop()
        if ref in objects:
            continue
        value = _read(state / ref, Path(ref).stem)
        objects[ref] = value
        pending.extend(_refs(value))
    receipt = objects[manifest['receipt_ref']]
    if receipt['source_commitments']['formal_master']['sha256'] != identity['formal_version']:
        raise ValueError('formal_version_drift')
    route_metadata = manifest['knowledge_hot_routes']
    exported = {**manifest, 'version': identity['version'], 'formal_version': identity['formal_version'],
                'export_schema': EXPORT_VERSION, 'native_manifest_sha256': native_sha,
                'native_manifest_ref': 'manifests/' + native_sha + '.json',
                'coverage': route_metadata['coverage'],
                'source_coverage': route_metadata['source_coverage'],
                'query_views': ['protected', 'after_attempt', 'direct']}
    exported['query_required'] = bootstrap_snapshot_files(exported)
    objects['manifests/' + native_sha + '.json'] = manifest
    files = []
    for ref, value in sorted(objects.items()):
        target = output / ref
        raw = _bytes(value)
        if not target.is_file() or target.read_bytes() != raw:
            if target.is_symlink() or any(p.is_symlink() for p in target.parents):
                raise ValueError('snapshot_output_symlink_rejected')
            native._atomic_write(target, raw)
        _read(target, Path(ref).stem)
        files.append({'source_path': str(target), 'publish_path': PREFIX + ref, 'kind': 'personalization_object'})
    if current_identity(root) != identity or native._load_manifest(state)[1] != native_sha:
        raise ValueError('personalization_changed_during_export')
    _history_current(root)
    path = output / 'manifest.json'
    raw = _bytes(exported)
    if not path.is_file() or path.read_bytes() != raw:
        if path.is_symlink():
            raise ValueError('snapshot_output_symlink_rejected')
        native._atomic_write(path, raw)
    if _read(path) != exported:
        raise ValueError('snapshot_manifest_readback_failed')
    files.append({'source_path': str(path), 'publish_path': PREFIX + 'manifest.json', 'kind': 'personalization_manifest'})
    return {**identity, 'manifest_path': str(path), 'files': files, 'entrypoint': PREFIX + 'manifest.json',
            'coverage': exported['coverage'], 'schema': EXPORT_VERSION}


_QUESTION_UNSET = object()


def _resolve_snapshot(manifest_path: Path, knowledge_points: list[str], weak_knowledge_points: list[str],
                   view: str, *,
                   exam_question_id=_QUESTION_UNSET, formal_id: str | None = None,
                   current_question_id=_QUESTION_UNSET, current_question_part: str | None = None,
                   source_id: str | None = None, personal_scope: str = 'full',
                   materials: str = 'all') -> tuple:
    if view not in {'protected', 'after_attempt', 'direct'}:
        raise ValueError('snapshot_view_invalid')
    if personal_scope not in {'full', 'summary'}:
        raise ValueError('snapshot_personal_scope_invalid')
    if materials not in {'all', 'core', 'personal', 'visual'}:
        raise ValueError('snapshot_materials_invalid')
    allow_formal = exam_question_id is _QUESTION_UNSET and current_question_id is _QUESTION_UNSET
    exam_question_id = None if exam_question_id is _QUESTION_UNSET else exam_question_id
    current_question_id = None if current_question_id is _QUESTION_UNSET else current_question_id
    if current_question_id is not None and not native.valid_identity(current_question_id):
        raise ValueError('snapshot_current_question_identity_invalid')
    if current_question_id and exam_question_id and current_question_id != exam_question_id:
        raise ValueError('snapshot_conflicting_question_identities')
    if current_question_part and (not isinstance(current_question_part, str) or not re.fullmatch(r'P[0-9]{2,3}', current_question_part)):
        raise ValueError('snapshot_question_part_invalid')
    if source_id is not None and not native.valid_identity(source_id):
        raise ValueError('snapshot_source_identity_invalid')
    if exam_question_id is not None and (not isinstance(exam_question_id, str)
            or not re.fullmatch(r'EXAM408-\d{4}-Q\d{2}(?:-P\d{2})?', exam_question_id)):
        raise ValueError('snapshot_exam_question_identity_invalid')
    if formal_id is not None and (not isinstance(formal_id, str) or not native.FORMAL_ID_RE.fullmatch(formal_id)):
        raise ValueError('snapshot_formal_identity_invalid')
    for terms in (knowledge_points, weak_knowledge_points):
        if not isinstance(terms, list) or len(terms) > 128 or any(not isinstance(t, str) or not t.strip() or len(t) > 200 for t in terms):
            raise ValueError('snapshot_knowledge_points_invalid')
    path = Path(manifest_path)
    manifest = _read(path)
    if manifest.get('schema') != native.MANIFEST_SCHEMA or manifest.get('export_schema') != EXPORT_VERSION:
        raise ValueError('snapshot_manifest_schema_invalid')
    if manifest.get('version') != _version(manifest):
        raise ValueError('snapshot_version_invalid')
    object_cache = {}
    def read_ref(ref, sha):
        if not isinstance(ref, str) or not REF.fullmatch(ref) or Path(ref).stem != sha:
            raise ValueError('snapshot_reference_invalid')
        if ref not in object_cache:
            object_cache[ref] = _read(path.parent / ref, sha)
        return copy.deepcopy(object_cache[ref])
    original = read_ref(manifest['native_manifest_ref'], manifest['native_manifest_sha256'])
    if any(manifest.get(key) != value for key, value in original.items()):
        raise ValueError('snapshot_native_manifest_binding_invalid')
    receipt = read_ref(original['receipt_ref'], original['receipt_sha256'])
    if receipt['source_commitments']['formal_master']['sha256'] != manifest['formal_version']:
        raise ValueError('snapshot_formal_version_invalid')
    normalized = {'knowledge': knowledge_points, 'weak_knowledge': weak_knowledge_points,
                  'formal': [formal_id] if formal_id else [],
                  'exam_question': [exam_question_id] if exam_question_id else [],
                  'source': [source_id] if source_id else [], 'mechanism': [], 'ask_type': [],
                  'current_question': [current_question_id] if current_question_id else [],
                  'question_part': current_question_part, 'formal_visual_allowed': allow_formal,
                  'formal_introduction_allowed': allow_formal,
                  'stage': 'pre_answer' if view == 'protected' else 'after_attempt',
                  'personal_scope': personal_scope, 'materials': materials}
    routed = resolve_hot_routes(
        manifest.get('knowledge_hot_routes') or {}, normalized,
        lambda entry: read_ref(entry['ref'], entry['sha256']),
        include_profiles=False)
    return manifest, normalized, routed, read_ref, exam_question_id, current_question_id


def bootstrap_snapshot_files(manifest: dict) -> list[str]:
    """Relative verified inputs for routing; never the transitive history closure."""
    refs = [manifest['native_manifest_ref'], manifest['receipt_ref']]
    refs.extend(row['ref'] for row in manifest['knowledge_hot_routes']['buckets'].values())
    if any(not isinstance(ref, str) or not REF.fullmatch(ref) for ref in refs):
        raise ValueError('snapshot_reference_invalid')
    return sorted(set(refs))


def plan_snapshot(manifest_path: Path, knowledge_points: list[str], weak_knowledge_points: list[str],
                  view: str = 'protected', *, exam_question_id=_QUESTION_UNSET,
                  formal_id: str | None = None, current_question_id=_QUESTION_UNSET,
                  current_question_part: str | None = None, source_id: str | None = None,
                  personal_scope: str = 'full', materials: str = 'all', known_version: str | None = None) -> dict:
    """Plan only current-query objects using the same verified routing as query."""
    _manifest, _normalized, routed, _read_ref, _exam, _current = _resolve_snapshot(
        manifest_path, knowledge_points, weak_knowledge_points, view,
        exam_question_id=exam_question_id, formal_id=formal_id,
        current_question_id=current_question_id, current_question_part=current_question_part,
        source_id=source_id, personal_scope=personal_scope, materials=materials)
    if known_version is not None and (not isinstance(known_version, str) or not native.SHA256_RE.fullmatch(known_version)):
        raise ValueError('snapshot_known_version_invalid')
    selected = []
    incomplete_personal_plans = []
    current_ids = (_normalized.get('current_question') or _normalized.get('exam_question')
                   or _normalized.get('formal') or _normalized.get('source') or [])
    exact_part = _normalized.get('question_part') or any(
        re.search(r'-P[0-9]{2,3}$', str(identity), re.I) for identity in current_ids)
    if routed.get('subpoint_entries'):
        selected.extend(entry['teaching_views'][view] for entry in routed['subpoint_entries'].values())
        if view != 'protected' and personal_scope == 'full' and materials != 'visual':
            selected.extend(entry['evidence_object'] for entry in routed['subpoint_entries'].values()
                            if entry.get('evidence_object'))
    else:
        for entry in routed['point_entries'].values():
            selected.append(entry)
            # Direct queries always need their prepared teaching fields;
            # after-attempt may use the prepared view for a tight budget.
            # History/evidence refs remain independently fetchable, not eager inputs.
            if view != 'protected' and (entry.get('teaching_views') or {}).get(view):
                selected.append(entry['teaching_views'][view])
            if view != 'protected' and personal_scope == 'full' and materials != 'visual' and not exact_part:
                source = entry.get('personal_source')
                if source is None:
                    incomplete_personal_plans.append(entry.get('ref'))
                elif source.get('status') == 'current' and source.get('profile_object'):
                    selected.append(source['profile_object'])
    selected.extend(entry['teaching_views'][view] for entry in routed['syllabus_entries'].values())
    if view != 'protected':
        introduction = routed.get('question_introduction_entry')
        if introduction:
            selected.append(introduction)
        visual = routed.get('question_visual_entry') or {}
        if materials in {'all', 'visual'} and visual.get('ref') and visual.get('status') != 'stale':
            selected.append(visual)
    files = set()
    for entry in selected:
        ref, sha = entry.get('ref'), entry.get('sha256')
        if not isinstance(ref, str) or not REF.fullmatch(ref) or Path(ref).stem != sha:
            raise ValueError('snapshot_reference_invalid')
        files.add(PREFIX + ref)
    return {'layout': 'hot_routes', 'files': sorted(files), 'personal_scope': personal_scope, 'materials': materials,
            'status': 'requires_refresh' if incomplete_personal_plans else 'ready',
            'missing_personal_source_entries': incomplete_personal_plans}


def query_snapshot(manifest_path: Path, knowledge_points: list[str], weak_knowledge_points: list[str],
                   view: str, max_bytes: int, cursor: str = '', *,
                   exam_question_id=_QUESTION_UNSET, formal_id: str | None = None,
                   current_question_id=_QUESTION_UNSET, current_question_part: str | None = None,
                   source_id: str | None = None, personal_scope: str = 'full', materials: str = 'all',
                   known_version: str | None = None) -> dict:
    if isinstance(max_bytes, bool) or not isinstance(max_bytes, int) or not 1024 <= max_bytes <= 1024 * 1024:
        raise ValueError('snapshot_byte_budget_invalid')
    manifest, normalized, routed, read_ref, exam_question_id, current_question_id = _resolve_snapshot(
        manifest_path, knowledge_points, weak_knowledge_points, view,
        exam_question_id=exam_question_id, formal_id=formal_id,
        current_question_id=current_question_id, current_question_part=current_question_part,
        source_id=source_id, personal_scope=personal_scope, materials=materials)
    if known_version is not None and (not isinstance(known_version, str) or not native.SHA256_RE.fullmatch(known_version)):
        raise ValueError('snapshot_known_version_invalid')
    normalized['resolved_formal_ids'] = []
    def read_artifact(ref, sha, _label, **kwargs):
        if not isinstance(ref, str) or not REF.fullmatch(ref) or Path(ref).stem != sha:
            raise ValueError('snapshot_reference_invalid')
        limit = kwargs.get('limit')
        if limit is not None and (Path(manifest_path).parent / ref).stat().st_size > limit:
            raise ValueError('snapshot_selected_object_too_large')
        return read_ref(ref, sha)
    binding = _digest([manifest['version'], manifest['formal_version'], knowledge_points, weak_knowledge_points, view,
                       exam_question_id, current_question_id, current_question_part, source_id, formal_id,
                       routed.get('question_visual_entry'), routed.get('question_introduction_entry'),
                       personal_scope, materials, native.TEACHING_DELIVERY_POLICY])
    entries = {**routed['point_entries'], **routed.get('subpoint_entries', {}), **routed.get('syllabus_entries', {})}
    total_records = len(routed.get('subpoint_entries') or routed['point_entries']) + len(routed.get('syllabus_entries') or {})
    full_context_version = _digest([binding, 0, total_records])
    if known_version == full_context_version and not cursor:
        unchanged = {'status': 'unchanged', 'version': full_context_version,
                     'source_version': manifest['source_version'], 'records': []}
        native._finish_teaching_materials(unchanged, entries, normalized,
            read_artifact, view=view)
        reused = {'status': 'unchanged', 'subject': '408', 'version': manifest['version'],
                'context_version': full_context_version, 'formal_version': manifest['formal_version'],
                'source_version': manifest['source_version'], 'view': view, 'personal_scope': personal_scope,
                'materials': materials, 'knowledge_summaries': [], 'sources': [], 'next_cursor': '',
                'complete': True, 'personal_read_receipt': unchanged['personal_read_receipt'],
                'formal_write_count': 0, 'current_correctness_source': 'current_question_only'}
        if len(_bytes(reused)) > max_bytes:
            return {'status': 'budget_too_small', 'version': manifest['version'],
                    'formal_version': manifest['formal_version'], 'minimum_bytes': len(_bytes(reused)),
                    'knowledge_summaries': [], 'next_cursor': '', 'complete': False}
        return reused
    introduction = native._question_exam_introduction(routed, view,
        read_artifact)
    question_visual = (native.read_question_visual(routed, view,
        read_artifact) if materials in {'all', 'visual'} else None)
    summary_refs = {}
    point_summary_objects = {}

    def read_point_summary(entry):
        value = read_ref(entry['ref'], entry['sha256'])
        point_summary_objects[value.get('knowledge_point')] = value
        return value

    if routed.get('subpoint_entries'):
        summaries = []
        for key, entry in routed['subpoint_entries'].items():
            ref = entry['teaching_views'][view]
            value = read_ref(ref['ref'], ref['sha256'])
            if value.get('subpoint_id') != key or value.get('view') != view:
                raise ValueError('snapshot_subpoint_binding_invalid')
            summaries.append({**value, 'summary_sha256': ref['sha256']})
            summary_refs[key] = ref
        coverage = {'scope': 'matched_subpoints_only',
            'requested_point_count': len(summaries), 'missing_points': routed['missing_points'],
            'ambiguous_terms': routed['ambiguous_terms'],
            'all_requested_points_queried': not routed['missing_points'] and not routed['ambiguous_terms']}
    else:
        summaries, coverage = project_points(
            routed['requested_points'], routed['missing_points'], routed['weak_points'],
            routed['source_coverage'], normalized, routed['point_entries'],
            read_point_summary,
            ambiguous_terms=routed['ambiguous_terms'])
        summary_refs = dict(routed['point_entries'])
        if view != 'protected':
            for summary in summaries:
                source = routed['point_entries'].get(summary.get('knowledge_point'), {}).get('personal_source')
                if source is not None:
                    summary['personal_source'] = copy.deepcopy(source)
    syllabus_entries = routed.get('syllabus_entries') or {}
    if syllabus_entries:
        objective_records = native._read_syllabus_teaching_entries(syllabus_entries, view,
            read_artifact)
        for value in objective_records:
            key = value['syllabus_id']
            ref = syllabus_entries[key]['teaching_views'][view]
            summaries.append({**value, 'summary_sha256': ref['sha256']})
            summary_refs[key] = ref
        coverage = {**coverage,
            'scope': 'syllabus_only' if len(objective_records) == len(summaries) else 'knowledge_and_syllabus',
            'requested_syllabus_entry_count': len(objective_records),
            'requested_syllabus_entries': list(syllabus_entries)}

    def summary_identity(summary):
        return summary.get('syllabus_id') or summary.get('subpoint_id') or summary['knowledge_point']

    def summary_source(summary):
        identity = ({'syllabus_id': summary['syllabus_id']} if summary.get('syllabus_id') else
                    {'knowledge_point': summary['knowledge_point']})
        return {**identity, 'sha256': summary['summary_sha256'],
                'path': PREFIX + summary_refs[summary_identity(summary)]['ref']}

    prepared_parent_views = {}

    def read_prepared_parent_view(index):
        """Validate a selected frozen teaching view, without reading history."""
        projected = summaries[index]
        if view == 'protected' or projected.get('subpoint_id') or projected.get('syllabus_id'):
            return None
        code = projected.get('knowledge_point')
        entry = routed['point_entries'].get(code) or {}
        reference = (entry.get('teaching_views') or {}).get(view)
        raw = point_summary_objects.get(code)
        if not reference or not raw:
            return None
        if code in prepared_parent_views:
            return prepared_parent_views[code]
        prepared = read_ref(reference['ref'], reference['sha256'])
        if (prepared.get('schema') != 'knowledge_teaching_view_408_v1'
                or prepared.get('knowledge_point') != code
                or prepared.get('view', view) != view):
            raise ValueError('snapshot_prepared_teaching_identity_invalid')
        profile = raw.get('learning_profile') or {}
        expected_status = {'current': 'semantic_current', 'stale': 'semantic_stale',
                           'not_authored': 'evidence_digest_only'}.get(profile.get('status'))
        if expected_status and prepared.get('status') != expected_status:
            raise ValueError('snapshot_prepared_teaching_status_mismatch')
        prepared_parent_views[code] = (entry, reference, raw, prepared)
        return prepared_parent_views[code]

    # Raw point summaries are the evidence projection and intentionally carry
    # after-attempt teacher advice. Direct requests must use the same frozen
    # direct teaching fields even when the expanded raw summary fits easily.
    # Merge only these fields so current/undated/prior learner evidence remains
    # intact, and expose both immutable source objects for the composite result.
    if view == 'direct':
        for index, projected in enumerate(summaries):
            selected = read_prepared_parent_view(index)
            if selected is None:
                continue
            _entry, reference, _raw, prepared = selected
            copied = []
            for field in ('lesson_bridge', 'linear_teaching_route', 'teaching_delivery_policy'):
                if field in prepared:
                    projected[field] = copy.deepcopy(prepared[field])
                    copied.append(field)
                else:
                    projected.pop(field, None)
            if copied:
                projected['prepared_teaching_object'] = {key: reference[key] for key in ('ref', 'sha256')}
                projected['prepared_teaching_fields'] = copied

    def use_prepared_parent_view(index):
        """Use the same bounded view when one expanded parent cannot fit."""
        selected = read_prepared_parent_view(index)
        if selected is None:
            return False
        entry, reference, raw, prepared = selected
        projected = summaries[index]
        code = projected['knowledge_point']
        profile = raw.get('learning_profile') or {}
        compact = copy.deepcopy(prepared)
        # Keep the actual reviewed status and complete immutable evidence paths;
        # the short view is not a new semantic review or a replacement history.
        for field in ('requested_as_weak', 'understanding_status', 'latest_semantic_status',
                      'latest_chronology_ambiguous', 'question_associated_count',
                      'concept_review_session_count', 'concept_review_error_session_count',
                      'concept_review_count_semantics'):
            if field in projected:
                compact[field] = copy.deepcopy(projected[field])
        compact['history_object'] = copy.deepcopy(raw['history_object'])
        if profile:
            compact['learning_profile'] = {key: copy.deepcopy(profile[key]) for key in
                ('status', 'version', 'profile_object', 'origin', 'reason') if key in profile}
        compact['point_summary_object'] = {key: entry[key] for key in ('ref', 'sha256')}
        compact['summary_sha256'] = reference['sha256']
        compact['prepared_compact_view_used'] = True
        compact['omitted_for_budget'] = sorted(set(compact.get('omitted_for_budget', []))
            | {'expanded_point_projection'} | (set(projected) - set(compact)))
        compact['omitted_content_source'] = 'point_summary_object'
        summaries[index] = compact
        summary_refs[code] = reference
        return True

    offset = 0
    if cursor:
        try:
            token = json.loads(base64.urlsafe_b64decode(cursor.encode()))
            if token['binding'] != binding or type(token['offset']) is not int or not 0 <= token['offset'] < len(summaries):
                raise ValueError('snapshot_cursor_mismatch')
            offset = token['offset']
        except (ValueError, KeyError, TypeError) as exc:
            raise ValueError('snapshot_cursor_invalid') from exc
    def page(end):
        more = end < len(summaries)
        token = base64.urlsafe_b64encode(_bytes({'binding': binding, 'offset': end})).decode() if more else ''
        sources = []
        for summary in summaries[offset:end]:
            sources.append(summary_source(summary))
            prepared_source = summary.get('prepared_teaching_object')
            if prepared_source:
                sources.append({'knowledge_point': summary['knowledge_point'],
                    'role': 'prepared_teaching_view', 'view': view,
                    'sha256': prepared_source['sha256'], 'path': PREFIX + prepared_source['ref']})
        result = {'status': 'ready', 'subject': '408', 'version': manifest['version'],
                'formal_version': manifest['formal_version'], 'source_version': manifest['source_version'],
                'view': view, 'knowledge_summaries': summaries[offset:end],
                'knowledge_coverage': {**coverage,
                    'returned_point_count': sum(not s.get('syllabus_id') for s in summaries[offset:end]),
                    'returned_syllabus_entry_count': sum(bool(s.get('syllabus_id')) for s in summaries[offset:end]),
                    'omitted_points': [s['knowledge_point'] for s in summaries[:offset] + summaries[end:] if not s.get('syllabus_id')],
                    'omitted_syllabus_entries': [s['syllabus_id'] for s in summaries[:offset] + summaries[end:] if s.get('syllabus_id')],
                    'all_requested_points_returned': not more and offset == 0
                        and coverage['all_requested_points_queried']},
                'sources': sources,
                'next_cursor': token, 'complete': not more, 'current_correctness_source': 'current_question_only',
                'formal_write_count': 0}
        context_version = _digest([binding, offset, end])
        completed = {'status': 'ready', 'source_version': manifest['source_version'],
                     'version': context_version, 'records': summaries[offset:end]}
        native._finish_teaching_materials(completed, entries, normalized,
            read_artifact, view=view)
        result.update(knowledge_summaries=completed['records'], context_version=context_version,
                      personal_scope=personal_scope, materials=materials,
                      personal_read_receipt=completed['personal_read_receipt'])
        result['personal_read_receipt'].update(page_offset=offset,
            returned_identities=[summary_identity(row) for row in completed['records']],
            complete_requested_scope=offset == 0 and end == len(summaries))
        if 'personal_context_status' in completed:
            result['personal_context_status'] = completed['personal_context_status']
        for item in completed['personal_read_receipt']['records']:
            for reference in item.get('objects') or []:
                extra = {'identity': item['identity'], 'role': 'complete_personal_context',
                         'sha256': reference['sha256'], 'path': PREFIX + reference['ref']}
                if extra not in sources:
                    sources.append(extra)
        if introduction is not None:
            result['question_exam_introduction'] = introduction
        if question_visual is not None:
            result['question_visual_template'] = copy.deepcopy(question_visual)
        native._bind_question_lesson_bridge(result)
        native._deduplicate_question_frequency_intro(result)
        return result
    end = offset
    while end < len(summaries):
        if len(_bytes(page(end + 1))) <= max_bytes:
            end += 1
            continue
        # Paginate normally once a page has an item. Only an individually
        # oversized first item may use the frozen compact view, so later pages
        # cannot become stuck on a large stale fact projection.
        if end == offset and use_prepared_parent_view(end) and len(_bytes(page(end + 1))) <= max_bytes:
            end += 1
            continue
        # The optional execution join must not crowd out the learner's actual
        # evidence or syllabus boundary. Its immutable summary remains linked
        # by this page's sources; complete native resolve keeps the whole join.
        if end == offset and 'lesson_bridge' in summaries[end]:
            summaries[end].pop('lesson_bridge')
            summaries[end]['lesson_bridge_omitted_for_budget'] = True
            if len(_bytes(page(end + 1))) <= max_bytes:
                end += 1
                continue
        if (end == offset and omit_teaching_plan_for_budget(summaries[end])
                and len(_bytes(page(end + 1))) <= max_bytes):
            end += 1
            continue
        break
    result = page(end)
    if (end == offset and end < len(summaries)) or len(_bytes(result)) > max_bytes:
        # Do not silently discard an observation to manufacture a successful page.
        failure = {'status': 'budget_too_small', 'version': manifest['version'], 'formal_version': manifest['formal_version'],
                'minimum_bytes': len(_bytes(page(min(offset + 1, len(summaries))))),
                'knowledge_summaries': [], 'next_cursor': cursor, 'complete': False}
        if introduction is not None:
            failure['question_exam_introduction'] = introduction
            if len(_bytes(failure)) > max_bytes:
                failure['question_exam_introduction'] = {
                    'status': 'omitted_for_budget', 'question_identity': introduction.get('question_identity'),
                    'reason': 'question_introduction_exceeds_query_budget'}
        if question_visual is not None:
            failure['question_visual_template'] = {'status': 'omitted_for_budget',
                'question_identity': question_visual.get('question_identity'),
                'reason': 'complete_question_template_and_personal_summary_require_larger_budget'}
        return failure
    return result
