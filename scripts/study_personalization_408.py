"""Verified portable 408 tutoring artifacts; snapshot queries never read live state."""
from __future__ import annotations

import base64
import hashlib
import json
import re
from pathlib import Path
from typing import Any

import exam_tutoring_context_408 as native
from knowledge_hot_routes_408 import resolve as resolve_hot_routes
from knowledge_tutoring_query_408 import project_points

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


def query_snapshot(manifest_path: Path, knowledge_points: list[str], weak_knowledge_points: list[str],
                   view: str, max_bytes: int, cursor: str = '') -> dict:
    if view not in {'protected', 'after_attempt', 'direct'}:
        raise ValueError('snapshot_view_invalid')
    if isinstance(max_bytes, bool) or not isinstance(max_bytes, int) or not 1024 <= max_bytes <= 1024 * 1024:
        raise ValueError('snapshot_byte_budget_invalid')
    for terms in (knowledge_points, weak_knowledge_points):
        if not isinstance(terms, list) or len(terms) > 128 or any(not isinstance(t, str) or not t.strip() or len(t) > 200 for t in terms):
            raise ValueError('snapshot_knowledge_points_invalid')
    path = Path(manifest_path)
    manifest = _read(path)
    if manifest.get('schema') != native.MANIFEST_SCHEMA or manifest.get('export_schema') != EXPORT_VERSION:
        raise ValueError('snapshot_manifest_schema_invalid')
    if manifest.get('version') != _version(manifest):
        raise ValueError('snapshot_version_invalid')
    def read_ref(ref, sha):
        if not isinstance(ref, str) or not REF.fullmatch(ref) or Path(ref).stem != sha:
            raise ValueError('snapshot_reference_invalid')
        return _read(path.parent / ref, sha)
    original = read_ref(manifest['native_manifest_ref'], manifest['native_manifest_sha256'])
    if any(manifest.get(key) != value for key, value in original.items()):
        raise ValueError('snapshot_native_manifest_binding_invalid')
    receipt = read_ref(original['receipt_ref'], original['receipt_sha256'])
    if receipt['source_commitments']['formal_master']['sha256'] != manifest['formal_version']:
        raise ValueError('snapshot_formal_version_invalid')
    normalized = {'knowledge': knowledge_points, 'weak_knowledge': weak_knowledge_points,
                  'formal': [], 'source': [], 'mechanism': [], 'ask_type': [],
                  'stage': 'pre_answer' if view == 'protected' else 'after_attempt'}
    routed = resolve_hot_routes(
        manifest.get('knowledge_hot_routes') or {}, normalized,
        lambda entry: read_ref(entry['ref'], entry['sha256']),
        include_profiles=False)
    normalized['resolved_formal_ids'] = []
    summary_refs = {}
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
            lambda entry: read_ref(entry['ref'], entry['sha256']),
            ambiguous_terms=routed['ambiguous_terms'])
        summary_refs = routed['point_entries']
    binding = _digest([manifest['version'], manifest['formal_version'], knowledge_points, weak_knowledge_points, view])
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
        return {'status': 'ready', 'subject': '408', 'version': manifest['version'],
                'formal_version': manifest['formal_version'], 'source_version': manifest['source_version'],
                'view': view, 'knowledge_summaries': summaries[offset:end],
                'knowledge_coverage': {**coverage, 'returned_point_count': end - offset,
                    'omitted_points': [s['knowledge_point'] for s in summaries[:offset] + summaries[end:]],
                    'all_requested_points_returned': not more and offset == 0
                        and coverage['all_requested_points_queried']},
                'sources': [{'knowledge_point': s['knowledge_point'], 'sha256': s['summary_sha256'],
                             'path': PREFIX + summary_refs[s.get('subpoint_id', s['knowledge_point'])]['ref']} for s in summaries[offset:end]],
                'next_cursor': token, 'complete': not more, 'current_correctness_source': 'current_question_only',
                'formal_write_count': 0}
    end = offset
    while end < len(summaries) and len(_bytes(page(end + 1))) <= max_bytes:
        end += 1
    result = page(end)
    if (end == offset and end < len(summaries)) or len(_bytes(result)) > max_bytes:
        # Do not silently discard an observation to manufacture a successful page.
        return {'status': 'budget_too_small', 'version': manifest['version'], 'formal_version': manifest['formal_version'],
                'minimum_bytes': len(_bytes(page(min(offset + 1, len(summaries))))),
                'knowledge_summaries': [], 'next_cursor': cursor, 'complete': False}
    return result
