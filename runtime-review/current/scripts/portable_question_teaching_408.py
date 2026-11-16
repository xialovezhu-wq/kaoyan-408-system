"""Bounded, source-bound exact-question teaching updates after formal intake.

The web submits teacher proposals, never paths or learner state. Preparation
checks the registered original input; only the post-formal owner activates the
existing question/source-plan/route stores. Every layer is independently resumable.
"""
from __future__ import annotations
import copy
import fcntl
import hashlib
import json
from pathlib import Path, PurePosixPath
import re
import tempfile
import zipfile

import question_visual_templates_408 as questions
import source_visual_408 as visuals
import teaching_research_408 as research
from exam_figure_binding_408 import validate_binding, project_binding

SCHEMA = 'cs408-portable-question-teaching-v1'
FILENAME = 'portable_question_teaching_updates.json'
REQUIREMENTS_FILE = 'TEACHING_ALIGNMENT_REQUIREMENTS.json'
REQUIREMENTS_SCHEMA = 'cs408-teaching-alignment-requirements-v1'
REGISTRY_REL = Path('.private/study-bridge/teaching-alignment-inputs')
OPERATIONS_REL = Path('.private/question-visual-408/intake-operations')
MAX_BYTES = 16 * 1024 * 1024
SHA = re.compile(r'[0-9a-f]{64}')


def raw(value):
    return research.raw(value)


def digest(value):
    return hashlib.sha256(raw(value)).hexdigest()


def require(ok, reason):
    if not ok:
        raise ValueError('portable_question_' + reason)


def relative(value):
    require(isinstance(value, str) and value and '\\' not in value
            and not PurePosixPath(value).is_absolute()
            and all(p not in {'', '.', '..'} for p in value.split('/')), 'unsafe_relative_path')
    return value


def read_json(path, limit=MAX_BYTES):
    path = Path(path)
    require(path.is_file() and not path.is_symlink()
            and not any(p.is_symlink() for p in path.parents)
            and path.stat().st_size <= limit, 'unsafe_or_oversize_json')
    return json.loads(path.read_bytes())


def _manifest_ref(repo, rel, section, identity):
    path = Path(repo) / rel
    if not path.exists():
        return None
    return read_json(path).get(section, {}).get(identity)


def _hash(ref):
    return (ref or {}).get('sha256')



def _expected_object(repo, root_rel, expected, identity, identity_field):
    if expected is None:
        return None
    require(SHA.fullmatch(str(expected)), 'expected_object_hash')
    path = Path(repo) / root_rel / 'objects' / (expected + '.json')
    value = read_json(path, 256 * 1024)
    require(hashlib.sha256(path.read_bytes()).hexdigest() == expected
            and value.get(identity_field) == identity, 'expected_object_binding_drift')
    return value


def _identity_shape(template, identity, prior):
    if prior is not None:
        require(template.get('identity_kind') == prior.get('identity_kind')
                and template.get('parent_identity') == prior.get('parent_identity'), 'native_identity_kind_or_parent_drift')
    child = re.fullmatch(r'(EXAM408-\d{4}-Q\d{2})-P\d{2}', identity)
    part = re.fullmatch(r'(.+)#P[0-9]{2,3}', identity)
    if child or part:
        require(template.get('identity_kind') == ('subquestion' if child else 'teaching_part')
                and template.get('parent_identity') == (child or part).group(1), 'canonical_subquestion_kind_or_parent')
    elif re.fullmatch(r'EXAM408-\d{4}-Q\d{2}', identity):
        require(template.get('identity_kind') == 'parent_exam' and template.get('parent_identity') is None, 'canonical_parent_kind')
    elif re.fullmatch(r'(DS|CO|OS|CN)_(\d{4}|UNK)_\d{3}', identity):
        require(template.get('identity_kind') == 'formal_question' and template.get('parent_identity') is None, 'canonical_formal_kind')

def _capture_hash(ids):
    return hashlib.sha256(json.dumps(ids, ensure_ascii=False, sort_keys=True,
                                     separators=(',', ':')).encode()).hexdigest()


def registered_input(repo, manifest, proposal=None):
    """Only locally registered exports can make a new contract mandatory."""
    folder = Path(repo) / REGISTRY_REL
    matches, required_overlap = [], False
    returned_ids = {r['capture_id'] for r in manifest['captures']}
    if folder.exists():
        require(not folder.is_symlink(), 'registry_symlink')
        for path in sorted(folder.glob('*.json')):
            row = read_json(path)
            req = row.get('requirements') or {}
            if (req.get('study_date') == manifest['study_date'] and req.get('required') is True
                    and returned_ids & set(req.get('capture_ids', []))):
                required_overlap = True
            if (req.get('study_date') == manifest['study_date']
                    and req.get('capture_set_sha256') == manifest['capture_set_sha256']):
                matches.append(row)
    require(proposal is not None or not required_overlap, 'required_file_missing_for_registered_capture_scope')
    binding = (proposal or {}).get('input_binding') or {}
    if binding:
        matches = [r for r in matches if r['requirements'].get('input_id') == binding.get('input_id')]
        require(len(matches) == 1, 'registered_input_missing_or_ambiguous')
    if not matches:
        require(proposal is None, 'proposal_requires_registered_input')
        return None
    require(len(matches) == 1 or not proposal, 'input_binding_required')
    if proposal is None:
        require(not any(r.get('requirements', {}).get('required') for r in matches), 'required_file_missing')
        return None
    record = matches[0]
    req = record['requirements']
    require(req.get('schema') == REQUIREMENTS_SCHEMA and req.get('required') is True, 'requirements_schema')
    require(req.get('source_commit') == manifest['source_commit'], 'registered_source_version_conflict')
    ids = sorted(r['capture_id'] for r in manifest['captures'])
    require(req.get('capture_ids') == ids and _capture_hash(ids) == req['capture_set_sha256'], 'requirements_capture_scope')
    # Candidate points are not observed learner facts. The owning enhancement
    # validator proves manifest.affected_points from this exact Capture evidence.
    require(record.get('requirements_sha256') == binding.get('requirements_sha256')
            and SHA.fullmatch(str(record.get('requirements_sha256') or '')), 'requirements_hash_binding')
    path = Path(record.get('input_zip_path', ''))
    require(path.is_absolute() and path.is_file() and not path.is_symlink()
            and not any(p.is_symlink() for p in path.parents), 'registered_zip_unavailable')
    require(hashlib.sha256(path.read_bytes()).hexdigest() == record.get('input_zip_sha256'), 'registered_zip_sha_drift')
    with zipfile.ZipFile(path) as archive:
        names = archive.namelist()
        require(len(names) == len(set(names)), 'registered_zip_duplicate')
        content = archive.read(REQUIREMENTS_FILE)
        require(len(content) <= MAX_BYTES and hashlib.sha256(content).hexdigest() == record['requirements_sha256']
                and json.loads(content) == req, 'registered_requirements_drift')
    return record


def _load_proposal(package_root, manifest):
    path = Path(package_root) / FILENAME
    if not path.exists():
        return None
    rows = [r for r in manifest['files'] if r['path'] == FILENAME]
    require(len(rows) == 1 and rows[0]['role'] == 'supplemental', 'supplemental_role')
    value = read_json(path)
    require(hashlib.sha256(path.read_bytes()).hexdigest() == rows[0]['sha256'], 'proposal_sha_drift')
    require(set(value) == {'schema', 'source_commit', 'study_date', 'input_binding', 'questions', 'routes'}
            and value['schema'] == SCHEMA, 'proposal_schema')
    require(value['source_commit'] == manifest['source_commit'] and value['study_date'] == manifest['study_date'], 'source_version_or_date_conflict')
    require(set(value['input_binding']) == {'input_id', 'requirements_sha256'}, 'input_binding_fields')
    for field in ('questions', 'routes'):
        require(isinstance(value[field], list) and len(value[field]) <= 512, field + '_bounded_array')
    return value


def _published_materialize(commit, path, target, expected):
    # This is the same fixed-publication resolver used by D asset delivery.
    import sys
    import study_bridge_408 as bridge
    base = bridge.PUBLICATION_ROOT.parent.parent
    if str(base) not in sys.path:
        sys.path.insert(0, str(base))
    from study_bridge import Published
    return Published(bridge.PUBLICATION_ROOT.parent).materialize('408', commit, path, target, expected)


def _resolve_ref(ref, manifest, registry, published, package_root, destination):
    require(isinstance(ref, dict) and set(ref) == {'id', 'uri', 'sha256', 'role', 'locator'}, 'source_ref_fields')
    require(isinstance(ref['id'], str) and re.fullmatch(r'[A-Za-z0-9_.-]{1,80}', ref['id'])
            and SHA.fullmatch(str(ref['sha256'])) and isinstance(ref['locator'], str)
            and ref['locator'].strip(), 'source_ref_identity')
    require(ref['role'] in {'capture', 'syllabus', 'current_question', 'exam', 'textbook'}, 'source_role')
    uri = ref['uri']; commit = manifest['source_commit']
    require(isinstance(uri, str), 'source_uri')
    if uri.startswith('study-library://' + commit + '/'):
        path = relative(uri.removeprefix('study-library://' + commit + '/'))
        files = {r['path']: r for r in published.get('files', [])}
        require(path in files and files[path]['sha256'] == ref['sha256'], 'published_source_hash_or_version')
        require(ref['role'] != 'capture', 'capture_requires_original_input')
        target = destination / (ref['sha256'] + Path(path).suffix)
        _published_materialize(commit, path, target, ref['sha256'])
    elif uri.startswith('input://'):
        path = relative(uri.removeprefix('input://'))
        files = {r['path']: r for r in registry['requirements']['source_files']}
        require(path in files and files[path]['sha256'] == ref['sha256'], 'input_source_hash')
        if ref['role'] == 'capture':
            require(any(path.startswith('captures/' + cid + '/') for cid in registry['requirements']['capture_ids']), 'capture_source_scope')
        with zipfile.ZipFile(registry['input_zip_path']) as archive:
            info = archive.getinfo(path)
            require(info.file_size <= 128 * 1024 * 1024, 'input_source_oversize')
            content = archive.read(path)
        target = destination / (ref['sha256'] + Path(path).suffix)
        target.write_bytes(content)
    elif uri.startswith('enhancement-package://' + manifest['run_id'] + '/'):
        path = relative(uri.removeprefix('enhancement-package://' + manifest['run_id'] + '/'))
        files = {r['path']: r for r in manifest['files']}
        require(path in files and files[path]['sha256'] == ref['sha256'], 'return_source_hash')
        require(ref['role'] != 'capture', 'capture_requires_original_input')
        source = Path(package_root) / path
        require(source.is_file() and not source.is_symlink()
                and not any(p.is_symlink() for p in source.parents), 'return_source_unsafe')
        target = destination / (ref['sha256'] + Path(path).suffix)
        target.write_bytes(source.read_bytes())
    else:
        raise ValueError('portable_question_source_uri_or_version_forbidden')
    require(target.is_file() and hashlib.sha256(target.read_bytes()).hexdigest() == ref['sha256'], 'source_bytes_tampered')
    return {'path': str(target), 'sha256': ref['sha256'], 'locator': ref['locator'], 'role': ref['role'],
            'verified_origin': {'uri': uri, 'sha256': ref['sha256']},
            'published_path': 'teaching-sources/408/' + target.name}


def _refs(item, manifest, registry, published, package_root, destination):
    rows = item['source_refs']
    require(isinstance(rows, list) and 1 <= len(rows) <= 64, 'sources_required')
    require(len({r.get('id') for r in rows}) == len(rows), 'source_id_duplicate')
    roles = {r.get('role') for r in rows}
    require({'capture', 'syllabus', 'current_question'} <= roles
            and bool(roles & {'exam', 'textbook'}), 'source_roles_incomplete')
    for ref in rows:
        if ref.get('role') == 'capture':
            require(any(str(ref.get('uri', '')).startswith('input://captures/' + cid + '/')
                        for cid in item['capture_ids']), 'item_capture_source_scope')
    return {r['id']: _resolve_ref(r, manifest, registry, published, package_root, destination) for r in rows}


def _adapt(value, refs):
    """Only explicit source_id objects acquire a native path."""
    if isinstance(value, list):
        return [_adapt(v, refs) for v in value]
    if not isinstance(value, dict):
        return value
    require(not any(k in value for k in ('path', 'pdf_path', 'published_path', 'historical_plan_ref', 'ref')),
            'web_native_path_forbidden')
    if 'source_id' in value:
        sid = value['source_id']
        require(sid in refs and set(value) <= {'source_id', 'role', 'question_identity', 'pdf_page', 'pdf_pages'}, 'source_pointer_invalid')
        return {**copy.deepcopy(refs[sid]), **{k: copy.deepcopy(v) for k, v in value.items() if k != 'source_id'}}
    return {k: _adapt(v, refs) for k, v in value.items()}


def _shape(item, kind, manifest):
    common = {'capture_ids', 'disposition', 'reason', 'expected_previous_sha256', 'source_refs'}
    required = common | ({'question_identity', 'logical_question_id', 'knowledge_points',
        'expected_source_plan_sha256', 'template', 'source_plan'} if kind == 'question'
        else {'identity', 'parent', 'route'})
    require(isinstance(item, dict) and set(item) == required, kind + '_fields')
    ids = item['capture_ids']; allowed = {r['capture_id'] for r in manifest['captures']}
    require(isinstance(ids, list) and ids and ids == sorted(set(ids)) and set(ids) <= allowed, 'capture_out_of_scope')
    require(item['disposition'] in {'retain', 'revise', 'source_limited'}, 'disposition')
    require(isinstance(item['reason'], str) and 0 < len(item['reason'].strip()) <= 1000, 'reason_required')
    require(item['expected_previous_sha256'] is None or SHA.fullmatch(str(item['expected_previous_sha256'])), 'previous_hash')


def _route_value(item, refs):
    route = _adapt(item['route'], refs)
    require(isinstance(route, dict) and not set(route) & {'teaching_plan', 'visual_feedback', 'mastery', 'score', 'fsrs', 'timeline', 'progress', 'short_summary', 'question_refs'}, 'route_not_personal_profile')
    require(isinstance(route, dict) and route.get('schema') == 'cs408-derived-teaching-route-v1'
            and route.get('identity') == item['identity'] and route.get('parent') == item['parent'], 'route_identity')
    require(route.get('policy_version') == research.POLICY_VERSION and route.get('teacher_release_level') == 5
            and route.get('frontend_ready') is False and route.get('effectiveness') == 'unverified'
            and route.get('learning_write_count') == 0, 'route_teacher_boundary')
    require(route.get('review_status') in {'proposed', 'source_limited', 'source_reviewed_not_effectiveness_verified'}, 'route_review_status')
    blocks = route.get('ordered_blocks')
    require(isinstance(blocks, list) and blocks and all(isinstance(b, dict)
            and isinstance(b.get('step_id'), str) and b['step_id'].strip()
            and isinstance(b.get('teacher_text'), str) and b['teacher_text'].strip() for b in blocks)
            and len({b['step_id'] for b in blocks}) == len(blocks)
            and isinstance(route.get('visual_insertions'), list)
            and isinstance(route.get('limitations'), list)
            and all(isinstance(v, str) for v in route['limitations']), 'route_sequence')
    route['source_refs'] = list(refs.values())
    require(len(raw(route)) <= 256 * 1024, 'route_oversize')
    return route



def _fixed_native_version(refs, identity, expected):
    def visit(value):
        if isinstance(value, dict):
            route = value.get('linear_teaching_route')
            if isinstance(route, dict) and route.get('identity') == identity and route.get('native_route_version') == expected:
                return True
            return any(visit(v) for v in value.values())
        return isinstance(value, list) and any(visit(v) for v in value)
    for ref in refs.values():
        if not ref.get('verified_origin', {}).get('uri', '').startswith('study-library://'):
            continue
        path = Path(ref['path'])
        if path.suffix == '.json' and path.stat().st_size <= MAX_BYTES:
            try:
                if visit(json.loads(path.read_bytes())):
                    return True
            except (ValueError, UnicodeError):
                pass
    return False


def _reviewed_capture_identity(repo, capture_ids, identity, manifest, published):
    """Reuse the native local review binding; webpage claims do not create it."""
    import recent_contact_408 as contact
    folder = Path(repo) / contact.BINDING_REL
    expected_captures = {r['capture_id']: r for r in manifest['captures']}
    original = {r['question_id']: r for r in published.get('original_mappings', [])}.get(identity)
    if original is None or not folder.is_dir():
        return False
    matched = set()
    for path in folder.glob('*.json'):
        value = read_json(path)
        if value.get('capture_id') not in capture_ids or value.get('binding_role') == 'additional_result_exposure':
            continue
        require(value.get('schema') == contact.BINDING_SCHEMA
                and path.stem == digest(value), 'native_capture_identity_binding_hash')
        require(value.get('canonical_id') == identity and value.get('original_mapping_sha256') == digest(original)
                and value.get('capture_payload_sha256') == expected_captures[value['capture_id']].get('capture_payload_sha256')
                and value.get('study_date') == manifest['study_date']
                and value.get('reviewed_by') and len(value.get('basis', '')) >= 10, 'native_capture_identity_binding_drift')
        matched.add(value['capture_id'])
    return matched == set(capture_ids)


def _fixed_question_versions(refs, identity, template_sha, plan_sha):
    def visit(value):
        if isinstance(value, dict):
            if (value.get('question_identity') == identity and value.get('schema') == questions.SCHEMA
                    and {'template_native_version', 'source_visual_plan_version'} <= set(value)
                    and value.get('template_native_version') == template_sha
                    and value.get('source_visual_plan_version') == plan_sha):
                return True
            return any(visit(v) for v in value.values())
        return isinstance(value, list) and any(visit(v) for v in value)
    for ref in refs.values():
        if ref.get('verified_origin', {}).get('uri', '').startswith('study-library://'):
            path = Path(ref['path'])
            if path.suffix == '.json' and path.stat().st_size <= MAX_BYTES:
                try:
                    if visit(json.loads(path.read_bytes())):
                        return True
                except (ValueError, UnicodeError):
                    pass
    return False


def _version_proof_refs(manifest, published, package_root, destination):
    """Version evidence has no teaching-source role and cannot satisfy _refs."""
    name = 'native_teaching_version_proofs.json'
    rows = [row for row in manifest.get('files', []) if row.get('path') == name]
    if not rows:
        return {}
    require(len(rows) == 1 and rows[0].get('role') == 'supplemental', 'version_proof_registration')
    path = Path(package_root) / name
    value = read_json(path)
    require(hashlib.sha256(path.read_bytes()).hexdigest() == rows[0]['sha256'], 'version_proof_hash')
    require(set(value) == {'schema', 'source_commit', 'refs'}
            and value['schema'] == 'cs408-native-teaching-version-proofs-v1'
            and value['source_commit'] == manifest['source_commit'], 'version_proof_schema_or_snapshot')
    refs = value['refs']; commit = manifest['source_commit']
    require(isinstance(refs, list) and len(refs) <= 512, 'version_proof_budget')
    files = {row['path']: row for row in published.get('files', [])}
    resolved = {}
    for ref in refs:
        require(isinstance(ref, dict) and set(ref) == {'id', 'uri', 'sha256', 'locator'}
                and isinstance(ref['id'], str) and re.fullmatch(r'[A-Za-z0-9_.-]{1,80}', ref['id'])
                and ref['id'] not in resolved and SHA.fullmatch(str(ref['sha256']))
                and isinstance(ref['locator'], str) and ref['locator'].strip(), 'version_proof_ref_shape')
        prefix = 'study-library://' + commit + '/'
        require(isinstance(ref['uri'], str) and ref['uri'].startswith(prefix), 'version_proof_same_publication')
        member = relative(ref['uri'][len(prefix):])
        require(member.endswith('.json') and member in files and files[member]['sha256'] == ref['sha256'],
                'version_proof_published_hash')
        target = destination / (ref['sha256'] + '.json')
        _published_materialize(commit, member, target, ref['sha256'])
        read_json(target)
        require(hashlib.sha256(target.read_bytes()).hexdigest() == ref['sha256'], 'version_proof_bytes')
        resolved[ref['id']] = {'path': str(target), 'sha256': ref['sha256'],
                              'verified_origin': {'uri': ref['uri'], 'sha256': ref['sha256']}}
    return resolved

def _compile(repo, package_root, manifest, enhancement, proposal, registry, published, destination, *,
             formal_bindings=None, check_versions=True, profile_updates=None):
    req = registry['requirements']; qtargets = req.get('question_targets', [])
    rtargets = {r['identity']: r for r in req.get('route_targets', [])}
    affected = set(manifest['affected_points']); items = {r['capture_id']: r for r in enhancement['items']}
    expected_ids = set(questions.load_catalog(repo, verify_sources=False)['expected_ids'])
    compiled_q, compiled_r, seen_q, seen_r, reviewed_targets = [], [], set(), set(), set()
    version_refs = _version_proof_refs(manifest, published, package_root, destination)
    for item in proposal['questions']:
        _shape(item, 'question', manifest)
        require(all(items[c]['action'] != 'needs_user' for c in item['capture_ids'])
                or item['disposition'] == 'source_limited', 'unreviewed_capture')
        points = item['knowledge_points']
        require(isinstance(points, list) and (points or item['disposition'] == 'source_limited')
                and points == sorted(set(points)) and set(points) <= affected, 'question_point_out_of_scope')
        identity, logical = item['question_identity'], item['logical_question_id']
        require(bool(identity) != bool(logical) or (identity is None and logical is None
                and item['disposition'] == 'source_limited'), 'question_exact_or_logical_required')
        # Frozen knowledge_points are preparation candidates. Actual same-Capture
        # observations may add a necessary point, verified by enhancement intake.
        from enhancement_intake_408 import _observed_points
        observed_here = set().union(*[_observed_points(items[c].get('decision') or {})
                                    for c in item['capture_ids']])
        targets = [t for t in qtargets if set(item['capture_ids']) <= set(t['capture_ids'])
                   and t.get('question_identity') == identity
                   and (not t['knowledge_points'] or set(points) <= set(t['knowledge_points']) | observed_here)]
        locally_bound = False
        if not targets and identity and _reviewed_capture_identity(repo, item['capture_ids'], identity, manifest, published):
            targets = [t for t in qtargets if t.get('question_identity') is None
                       and set(item['capture_ids']) <= set(t['capture_ids'])]
            locally_bound = True
        require(len(targets) == 1, 'question_identity_out_of_scope')
        target = targets[0]
        target_key = digest(target)
        require(target_key not in reviewed_targets and set(item['capture_ids']) == set(target['capture_ids']) & set(items), 'question_capture_coverage')
        reviewed_targets.add(target_key)
        if not locally_bound:
            require(item['expected_previous_sha256'] == target.get('expected_previous_sha256')
                    and item['expected_source_plan_sha256'] == target.get('expected_source_plan_sha256'), 'registered_previous_hash_drift')
        if identity is None and logical is None:
            require(item['template'] is None and item['source_plan'] is None
                    and item['expected_previous_sha256'] is None and item['expected_source_plan_sha256'] is None, 'unidentified_source_limited_only')
            _refs(item, manifest, registry, published, package_root, destination)
            continue
        if logical:
            require(all(items[c].get('logical_question_id') == logical and items[c]['action'] == 'new'
                        for c in item['capture_ids']), 'logical_question_binding')
            require(item['expected_previous_sha256'] is None and item['expected_source_plan_sha256'] is None, 'new_question_previous_hash')
            bound = (formal_bindings or {}).get(logical)
            identity = bound.get('formal_id') if bound else logical
            require(formal_bindings is None or bound, 'formal_id_not_allocated')
        require(questions.valid_identity(identity) and identity not in seen_q, 'duplicate_or_invalid_question')
        seen_q.add(identity); expected_ids.add(identity)
        old = _manifest_ref(repo, questions.MANIFEST_REL, 'templates', identity)
        old_plan = _manifest_ref(repo, visuals.MANIFEST_REL, 'question_plans', identity)
        prior_template = _expected_object(repo, questions.ROOT_REL, item['expected_previous_sha256'], identity, 'question_identity')
        prior_plan = _expected_object(repo, visuals.MANIFEST_REL.parent, item['expected_source_plan_sha256'], identity, 'identity')
        if check_versions:
            require(_hash(old) == item['expected_previous_sha256'] and _hash(old_plan) == item['expected_source_plan_sha256'], 'question_previous_version_conflict')
        refs = _refs(item, manifest, registry, published, package_root, destination)
        if locally_bound:
            require(_fixed_question_versions(refs, identity, item['expected_previous_sha256'], item['expected_source_plan_sha256'])
                    or _fixed_question_versions(version_refs, identity, item['expected_previous_sha256'], item['expected_source_plan_sha256']),
                    'local_identity_requires_fixed_template_versions')
        if item['disposition'] == 'retain':
            require(prior_template and item['template'] is None and item['source_plan'] is None, 'retain_requires_existing_no_replacement')
            template = copy.deepcopy(prior_template)
            plan = copy.deepcopy(prior_plan)
        else:
            template = _adapt(item['template'], refs)
            require(isinstance(template, dict), 'template_required')
            submitted_identity = logical or identity
            require(template.get('question_identity') == submitted_identity, 'template_identity')
            template['question_identity'] = identity
            if logical and identity != logical:
                def bind_identity(value):
                    if isinstance(value, list):
                        return [bind_identity(v) for v in value]
                    if isinstance(value, dict):
                        return {k: (identity if k in {'identity', 'question_identity', 'source_question_identity'} and v == logical
                                    else bind_identity(v)) for k, v in value.items()}
                    return value
                template = bind_identity(template)
            _identity_shape(template, identity, prior_template)
            require(set(template.get('knowledge_points', [])) == set(points), 'template_point_scope')
            if logical:
                require(template.get('identity_kind') == 'formal_question' and template.get('parent_identity') is None, 'new_question_kind')
            template['source_refs'] = list(refs.values())
            require(template.get('exam_visual_binding') is not None, 'exam_binding_required')
            validate_binding(template['exam_visual_binding'], steps=template.get('steps'))
            require(template.get('source_review_status') == ('source_limited' if item['disposition'] == 'source_limited' else 'reviewed'), 'template_review_disposition')
            plan = _adapt(item['source_plan'], refs)
            require(isinstance(plan, dict) and plan.get('schema') == visuals.PLAN_SCHEMA
                    and plan.get('identity') == submitted_identity, 'source_plan_required')
            plan['identity'] = identity
            if logical and identity != logical:
                plan = bind_identity(plan)
            require(plan.get('exam_visual_binding') == template['exam_visual_binding'], 'source_plan_binding_drift')
            require((plan.get('source_visual_plan_review') or {}).get('decision') == 'retire_old_design'
                    and not any(k in plan for k in ('teaching_design', 'narrative_plan', 'overview', 'region_bindings', 'teacher_states')), 'obsolete_source_design_must_retire')
            require(plan.get('teacher_only') is True and plan.get('frontend_ready') is False, 'source_plan_teacher_boundary')
            if item['expected_source_plan_sha256']:
                plan['historical_plan_ref'] = {'ref': 'objects/' + item['expected_source_plan_sha256'] + '.json',
                                               'sha256': item['expected_source_plan_sha256']}
            if template.get('identity_kind') in {'subquestion', 'teaching_part'}:
                require(plan.get('scope') == 'current_exact_question_only'
                        and all(a.get('question_identity') == identity for a in template['exam_visual_binding']['source_assets']), 'subquestion_parent_or_sibling_scope')
                require(not any(k in template for k in ('parts', 'parent_solution', 'sibling_answers')), 'subquestion_answers_forbidden')
            require(len(raw(plan)) <= visuals.MAX_PLAN_BYTES, 'source_plan_oversize')
        _identity_shape(template, identity, prior_template)
        compiled_q.append({'identity': identity, 'logical_question_id': logical, 'item': item,
                           'template': template, 'source_plan': plan})
    required_question_targets = [t for t in qtargets if set(t['capture_ids']) & set(items)
                                 and (not t['knowledge_points'] or set(t['knowledge_points']) & affected
                                      or any(_observed_points(items[c].get('decision') or {}) & affected
                                             for c in set(t['capture_ids']) & set(items)))]
    require(len(reviewed_targets) == len(required_question_targets), 'question_review_coverage_missing')
    for item in proposal['routes']:
        _shape(item, 'route', manifest)
        identity = item['identity']; target = rtargets.get(identity)
        require(identity not in seen_r and research.IDENTITY.fullmatch(str(identity)), 'route_target_out_of_scope')
        seen_r.add(identity)
        if identity.startswith('KSP-'):
            import knowledge_profile_408 as profiles
            matches = [subpoint for update in (profile_updates or {}).get('updates', [])
                       if update.get('concept_key') == item['parent']
                       for subpoint in update.get('subpoints', []) if subpoint.get('subpoint_id') == identity]
            require(len(matches) == 1 and identity == profiles.subpoint_key(item['parent'], matches[0].get('title', '')),
                    'ksp_requires_same_batch_current_profile')
        canonical = bool(re.fullmatch(r'(?:DS|CO|OS|CN)\d{2}-\d{2}', identity))
        require((canonical and identity in affected and item['parent'] is None)
                or (identity.startswith('KSP-') and item['parent'] in affected), 'route_point_out_of_scope')
        if target:
            require(item['parent'] == target.get('parent') and item['expected_previous_sha256'] == target.get('expected_previous_sha256'), 'registered_previous_hash_drift')
        old = _manifest_ref(repo, research.INDEX_REL, 'targets', identity)
        old_route = _expected_object(repo, research.INDEX_REL.parent, item['expected_previous_sha256'], identity, 'identity')
        if old_route:
            require(old_route.get('parent') == item['parent']
                    or (identity in affected and item['parent'] is None and old_route.get('parent') == identity), 'route_parent_identity_drift')
        if check_versions:
            require(_hash(old) == item['expected_previous_sha256'], 'route_previous_version_conflict')
        refs = _refs(item, manifest, registry, published, package_root, destination)
        if target is None and item['expected_previous_sha256'] is not None:
            require(_fixed_native_version(refs, identity, item['expected_previous_sha256'])
                    or _fixed_native_version(version_refs, identity, item['expected_previous_sha256']),
                    'unregistered_route_requires_fixed_snapshot_proof')
        if item['disposition'] == 'retain':
            require(old_route and item['route'] is None, 'route_retain_requires_existing')
            route = copy.deepcopy(old_route)
        else:
            route = _route_value(item, refs)
            if item['disposition'] == 'source_limited':
                require(route['review_status'] == 'source_limited', 'route_limited_status')
        compiled_r.append({'identity': identity, 'item': item, 'route': route})
    require(affected <= seen_r, 'affected_point_route_review_missing')
    catalog = {'schema': questions.INDEX_SCHEMA, 'expected_ids': sorted(expected_ids),
               'templates': {r['identity']: r['template'] for r in compiled_q}, 'aliases': {}}
    for identity, value in catalog['templates'].items():
        questions.validate_template(value, identity, expected_ids)
    catalog_path = destination / 'catalog.json'; catalog_path.write_bytes(raw(catalog))
    questions.import_catalog(repo, catalog_path, activate=False)
    return compiled_q, compiled_r, catalog, catalog_path


def validate(*, repo, package_root, manifest, enhancement, published, check_versions=True, profile_updates=None):
    proposal = _load_proposal(package_root, manifest)
    registered = registered_input(repo, manifest, proposal)
    if proposal is None:
        return {'status': 'LEGACY_LOCAL_TEACHING_REVIEW_REQUIRED', 'required': False,
                'automatic_exact_question_update': False}
    with tempfile.TemporaryDirectory(prefix='.teaching-validate-', dir=Path(package_root).parent) as tmp:
        qs, rs, _, _ = _compile(repo, package_root, manifest, enhancement, proposal, registered,
                                published, Path(tmp), check_versions=check_versions, profile_updates=profile_updates)
    return {'schema': 'cs408-portable-question-teaching-adaptation-v1', 'status': 'VALIDATED', 'required': True,
            'source_sha256': hashlib.sha256((Path(package_root) / FILENAME).read_bytes()).hexdigest(),
            'input_binding': proposal['input_binding'], 'question_count': len(qs), 'route_count': len(rs),
            'question_targets': [r['logical_question_id'] or r['identity'] for r in qs],
            'route_targets': [r['identity'] for r in rs],
            'source_limited_questions': [{'capture_ids': q['capture_ids'], 'reason': q['reason']} for q in proposal['questions']
                                         if q['question_identity'] is None and q['logical_question_id'] is None],
            'formal_write_count': 0, 'learning_write_count': 0}


def _new_formal_aliases(enhancement, compiled_questions, formal_bindings, current_catalog):
    """Bind allocated new nodes to the exact canonical lesson already reviewed.

    A canonical proposal keeps its EXAM identity when the formal writer creates
    a new node. Only that writer's allocation plus the compiled Capture scope
    authorizes an alias; a source_id hint alone never does.
    """
    items = {item['capture_id']: item for item in enhancement['items']}
    result = {}
    for question in compiled_questions:
        identity = question['identity']
        if not re.fullmatch(r'EXAM408-\d{4}-Q\d{2}(?:-P\d{2})?', identity):
            continue
        for cid in question['item']['capture_ids']:
            item = items[cid]
            if item.get('action') != 'new':
                continue
            logical = item.get('logical_question_id')
            bound = (formal_bindings or {}).get(logical) or {}
            formal = bound.get('formal_id')
            require(isinstance(formal, str)
                    and re.fullmatch(r'(DS|CO|OS|CN)_(\d{4}|UNK)_\d{3}', formal),
                    'new_formal_alias_requires_allocation')
            require(formal not in current_catalog.get('expected_ids', []),
                    'new_formal_alias_shadows_identity')
            target = [identity]
            require(formal not in result or result[formal] == target,
                    'new_formal_alias_ambiguous')
            existing = current_catalog.get('aliases', {}).get(formal)
            require(existing is None or existing == target, 'new_formal_alias_conflict')
            result[formal] = target
    return result


def apply(*, repo, package_root, manifest, enhancement, published, adaptation, operation_id, formal_bindings=None, profile_updates=None):
    require(adaptation.get('status') == 'VALIDATED', 'validated_adaptation_required')
    repo = Path(repo)
    opkey = hashlib.sha256(operation_id.encode()).hexdigest()
    folder = repo / OPERATIONS_REL; folder.mkdir(parents=True, exist_ok=True)
    state_path = folder / (opkey + '.json')
    with (folder / '.lock').open('a+') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        state = read_json(state_path) if state_path.exists() else {
            'schema': 'cs408-question-teaching-apply-receipt-v1', 'operation_id': operation_id,
            'source_sha256': adaptation['source_sha256'], 'status': 'PENDING', 'completed_layers': []}
        require(state['operation_id'] == operation_id and state['source_sha256'] == adaptation['source_sha256'], 'operation_binding_drift')
        if state['status'] == 'COMPLETE':
            return state
        proposal = _load_proposal(package_root, manifest)
        require(hashlib.sha256((Path(package_root) / FILENAME).read_bytes()).hexdigest() == adaptation['source_sha256'], 'prepared_proposal_drift')
        registered = registered_input(repo, manifest, proposal)
        destination = repo / questions.ROOT_REL / 'sources'; destination.mkdir(parents=True, exist_ok=True)
        # Source files use content-addressed names; a retry never overwrites a
        # historical object with different bytes, even after partial activation.
        with tempfile.TemporaryDirectory(prefix='.resolve-', dir=folder) as tmp:
            qs, rs, _, _ = _compile(repo, package_root, manifest, enhancement, proposal, registered,
                                    published, Path(tmp), formal_bindings=formal_bindings,
                                    check_versions=False, profile_updates=profile_updates)
            for path in Path(tmp).iterdir():
                if path.name == 'catalog.json':
                    continue
                target = destination / path.name
                if target.exists():
                    require(target.read_bytes() == path.read_bytes(), 'immutable_source_conflict')
                else:
                    questions._atomic(target, path.read_bytes())
            def repath(value):
                if isinstance(value, list):
                    return [repath(v) for v in value]
                if isinstance(value, dict):
                    return {k: (str(destination / Path(v).name) if k == 'path' and isinstance(v, str)
                                and Path(v).parent == Path(tmp) else repath(v)) for k, v in value.items()}
                return value
            qs, rs = repath(qs), repath(rs)
        current_catalog = questions.load_catalog(repo, verify_sources=False)
        expected_ids = set(current_catalog['expected_ids']) | {q['identity'] for q in qs}
        formal_aliases = _new_formal_aliases(enhancement, qs, formal_bindings, current_catalog)
        catalog = {'schema': questions.INDEX_SCHEMA, 'expected_ids': sorted(expected_ids),
                   'templates': {q['identity']: q['template'] for q in qs}, 'aliases': formal_aliases}
        catalog_path = folder / (opkey + '.catalog.json'); questions._atomic(catalog_path, raw(catalog))
        for q in qs:
            current = _hash(_manifest_ref(repo, questions.MANIFEST_REL, 'templates', q['identity']))
            desired = digest(q['template'])
            require(current in {q['item']['expected_previous_sha256'], desired}, 'question_activation_conflict')
            current = _hash(_manifest_ref(repo, visuals.MANIFEST_REL, 'question_plans', q['identity']))
            # historical_plan_ref must refer to the pre-operation plan on every retry.
            if q['item']['expected_source_plan_sha256'] and q['source_plan'] is not None and q['item']['disposition'] != 'retain':
                q['source_plan']['historical_plan_ref'] = {'ref': 'objects/' + q['item']['expected_source_plan_sha256'] + '.json',
                                                         'sha256': q['item']['expected_source_plan_sha256']}
            desired = digest(q['source_plan']) if q['source_plan'] else None
            require(current in {q['item']['expected_source_plan_sha256'], desired}, 'source_plan_activation_conflict')
        state['routes'] = [{'identity': r['identity'], 'route_sha256': digest(r['route'])} for r in rs]
        state['questions'] = [{'question_identity': q['identity'], 'template_sha256': digest(q['template']),
                              'exam_visual_binding_sha256': digest(project_binding(q['template'].get('exam_visual_binding'))),
                              'source_plan_sha256': digest(q['source_plan']) if q['source_plan'] else None} for q in qs]
        if formal_aliases:
            state['formal_aliases'] = formal_aliases
        questions._atomic(state_path, raw(state))
        if 'questions' not in state['completed_layers']:
            questions.import_catalog(repo, catalog_path, activate=True)
            state['completed_layers'].append('questions'); questions._atomic(state_path, raw(state))
        if formal_aliases and 'formal_aliases' not in state['completed_layers']:
            # A pre-upgrade pending operation may have activated its templates
            # already. Recover only the missing aliases, never any formal item.
            current = questions.load_catalog(repo, verify_sources=False)
            if any(current['aliases'].get(key) != target for key, target in formal_aliases.items()):
                alias_catalog = {'schema': questions.INDEX_SCHEMA, 'expected_ids': current['expected_ids'],
                                 'templates': {}, 'aliases': formal_aliases}
                alias_path = folder / (opkey + '.aliases.catalog.json')
                questions._atomic(alias_path, raw(alias_catalog))
                questions.import_catalog(repo, alias_path, activate=True)
            state['completed_layers'].append('formal_aliases'); questions._atomic(state_path, raw(state))
        if 'source_plans' not in state['completed_layers']:
            index_path = repo / visuals.MANIFEST_REL
            index = read_json(index_path) if index_path.exists() else {
                'schema': visuals.INDEX_SCHEMA, 'question_plans': {}, 'point_plans': {}}
            for q in qs:
                if q['source_plan'] is not None:
                    index['question_plans'][q['identity']] = visuals._object(index_path.parent, q['source_plan'])
            visuals._atomic(index_path, visuals._raw(index)); visuals.clear_cache()
            state['completed_layers'].append('source_plans'); questions._atomic(state_path, raw(state))
        if 'routes' not in state['completed_layers']:
            state['route_receipt'] = research.apply_incremental(repo, [{'identity': r['identity'],
                'expected_previous_sha256': r['item']['expected_previous_sha256'], 'route': r['route']} for r in rs])
            state['completed_layers'].append('routes'); questions._atomic(state_path, raw(state))
        state.update(status='COMPLETE', source_limited_questions=adaptation.get('source_limited_questions', []), formal_write_count=0, learning_write_count=0,
                     profile_write_count=0, query_readback='pending_postcommit_refresh')
        questions._atomic(state_path, raw(state))
        return state



def publication_sources(repo):
    """Publish only source bytes referenced by currently active native objects.

    Walk three known manifests, never source directories or candidate journals.
    Historical object references remain archival and do not enlarge this set.
    """
    repo = Path(repo).resolve()
    managed = repo / questions.ROOT_REL / 'sources'
    prefix = 'teaching-sources/408/'
    found = {}

    def collect(value):
        if isinstance(value, list):
            for child in value:
                collect(child)
            return
        if not isinstance(value, dict):
            return
        path_value = value.get('path')
        published = value.get('published_path')
        if isinstance(path_value, str):
            path = Path(path_value)
            path = path if path.is_absolute() else repo / path
            # Lexical containment catches attempts to leave through '..'; an
            # advertised managed publication may never point outside the root.
            lexical = str(path).startswith(str(managed) + '/')
            advertised = isinstance(published, str) and published.startswith(prefix)
            if lexical or advertised:
                require(lexical and '..' not in path.parts and path.is_file()
                        and not path.is_symlink() and not any(p.is_symlink() for p in path.parents)
                        and path.resolve().parent == managed.resolve(), 'publication_source_escape_or_unsafe')
                declared = value.get('sha256')
                require(SHA.fullmatch(str(declared or '')) and path.stem == declared
                        and hashlib.sha256(path.read_bytes()).hexdigest() == declared, 'publication_source_sha_drift')
                expected = prefix + path.name
                require(published == expected, 'publication_source_mapping_drift')
                row = {'source_path': str(path), 'publish_path': expected, 'kind': '408_question_teaching_source'}
                require(expected not in found or found[expected] == row, 'publication_source_collision')
                found[expected] = row
        for key, child in value.items():
            if key not in {'historical_plan_ref', 'verified_origin'}:
                collect(child)

    for manifest_rel, section, limit in ((questions.MANIFEST_REL, 'templates', questions.MAX_TEMPLATE_BYTES),
                                         (visuals.MANIFEST_REL, 'question_plans', visuals.MAX_PLAN_BYTES),
                                         (research.INDEX_REL, 'targets', 256 * 1024)):
        manifest_path = repo / manifest_rel
        if not manifest_path.exists():
            continue
        manifest = read_json(manifest_path)
        refs = manifest.get(section)
        require(isinstance(refs, dict), 'publication_manifest_section')
        for identity, ref in refs.items():
            require(isinstance(ref, dict) and SHA.fullmatch(str(ref.get('sha256') or ''))
                    and ref.get('ref') == 'objects/' + ref['sha256'] + '.json', 'publication_object_ref')
            path = manifest_path.parent / ref['ref']
            value = read_json(path, limit)
            require(hashlib.sha256(path.read_bytes()).hexdigest() == ref['sha256'], 'publication_object_sha_drift')
            require(value.get('question_identity', value.get('identity')) == identity, 'publication_object_identity')
            collect(value)
    return [found[key] for key in sorted(found)]

def readback(repo, receipt):
    import exam_tutoring_context_408 as tutoring
    checked, failures, version = [], [], None
    try:
        rows = list(receipt.get('questions', []))
        by_identity = {row['question_identity']: row for row in rows}
        for alias, targets in receipt.get('formal_aliases', {}).items():
            require(len(targets) == 1 and targets[0] in by_identity, 'readback_formal_alias_scope')
            rows.append({**by_identity[targets[0]], 'query_identity': alias})
        for row in rows:
            identity = row['question_identity']
            query_identity = row.get('query_identity', identity)
            require(_hash(_manifest_ref(repo, questions.MANIFEST_REL, 'templates', identity)) == row['template_sha256'], 'readback_template_hash')
            require(_hash(_manifest_ref(repo, visuals.MANIFEST_REL, 'question_plans', identity)) == row.get('source_plan_sha256'), 'readback_source_plan_hash')
            for view in ('direct', 'after_attempt', 'protected'):
                result = tutoring.query_context(repo, {'schema': tutoring.QUERY_INPUT_SCHEMA,
                    'stage': 'pre_answer' if view == 'protected' else 'post_answer',
                    'current_question_id': query_identity, 'max_profiles': 0}, teaching_view=view)
                require(result.get('status') == 'available' and result.get('source_version'), 'readback_unavailable')
                require(version in (None, result['source_version']), 'readback_source_version_drift')
                version = result['source_version']; template = result.get('question_visual_template') or {}
                if view == 'protected':
                    require(template.get('status') == 'protected'
                            and set(template) <= {'status', 'grading_policy'}, 'protected_template_leak')
                else:
                    require(template.get('question_identity') == identity
                            and template.get('status') in {'reviewed', 'source_limited'}, 'readback_exact_question')
                    require(digest(template.get('exam_visual_binding')) == row['exam_visual_binding_sha256'], 'readback_binding_drift')
                    if row.get('source_plan_sha256') is not None:
                        require(digest((template.get('source_visuals') or {}).get('exam_visual_binding')) == row['exam_visual_binding_sha256'], 'readback_source_plan_binding_drift')
                    require('historical_plan_ref' not in json.dumps(template, ensure_ascii=False), 'readback_historical_ref_leak')
            checked.append(query_identity)
        for row in receipt.get('routes', []):
            identity = row['identity']
            require(_hash(_manifest_ref(repo, research.INDEX_REL, 'targets', identity)) == row['route_sha256'], 'readback_route_hash')
            for view in ('direct', 'after_attempt', 'protected'):
                result = tutoring.query_context(repo, {'schema': tutoring.QUERY_INPUT_SCHEMA,
                    'stage': 'pre_answer' if view == 'protected' else 'post_answer',
                    'knowledge_points': [identity], 'max_profiles': 0}, teaching_view=view)
                require(result.get('status') == 'available', 'route_readback_unavailable')
                def route_match(value):
                    if isinstance(value, dict):
                        route = value.get('linear_teaching_route') or {}
                        return (route.get('identity') == identity and route.get('native_route_version') == row['route_sha256']) or any(route_match(v) for v in value.values())
                    return isinstance(value, list) and any(route_match(v) for v in value)
                if view == 'protected':
                    require('native_route_version' not in json.dumps(result), 'protected_route_version_leak')
                else:
                    require(route_match(result), 'route_native_version_readback_drift')
                    if view == 'after_attempt':
                        def safe_after(value):
                            if isinstance(value, dict):
                                route = value.get('linear_teaching_route')
                                if isinstance(route, dict) and any(k in route for k in ('ordered_blocks', 'visual_insertions')):
                                    return False
                                return all(safe_after(v) for v in value.values())
                            return not isinstance(value, list) or all(safe_after(v) for v in value)
                        require(safe_after(result), 'after_attempt_full_route_leak')
    except (OSError, ValueError, KeyError, tutoring.ExamTutoringContextError) as exc:
        failures.append(str(exc))
    return {'status': 'PENDING' if failures else 'VERIFIED_CURRENT', 'source_version': version,
            'question_ids': checked, 'failures': failures, 'write_count': 0}
