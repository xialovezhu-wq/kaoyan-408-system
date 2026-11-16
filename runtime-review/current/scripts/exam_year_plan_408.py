"""Read-only annual-paper planning gate shared by morning export and import.

The plan records intended work, never learning or mastery.  Exclusions use
reviewed assessment points, not prerequisite/background taxonomy membership.
"""
from __future__ import annotations

import datetime as dt
import hashlib
import json
from pathlib import Path
import re
import zipfile

PLAN_REL = Path('.private/exam-year-study/plan.json')
MAPPING_REL = Path('support/syllabus-2027/point-question-links.json')
ADDENDA_REL = Path('support/syllabus-2027/assessment-addenda.json')
ADDENDA_SCHEMA = 'cs408-assessment-addenda-v1'
PLAN_SCHEMA = 'cs408-exam-year-study-plan-v1'
EXCLUSION_SCHEMA = 'cs408-exam-year-exclusion-v1'
AUDIT_SCHEMA = 'cs408-exam-year-return-audit-v1'
EQUIVALENCE_SCHEMA = 'cs408-exam-year-equivalent-binding-proof-v1'
POINT_FALLBACK_POLICY = 'prefer_nonoverlap_then_fill_other_years_v1'
FREQUENCY_FIRST_FALLBACK_POLICY = 'high_frequency_then_nonoverlap_with_other_year_fallback_v1'
POINT_FALLBACK_POLICIES = {POINT_FALLBACK_POLICY, FREQUENCY_FIRST_FALLBACK_POLICY}
EXAM_RE = re.compile(r'EXAM408-(\d{4})-Q\d{2}(?:-P\d{2})?')
POINT_RE = re.compile(r'(?:DS|CO|OS|CN)\d{2}-\d{2}')
_MAPPING_CACHE = {}
_MAPPING_PATH_CACHE = {}
_ADDENDA_DOCUMENT_CACHE = {}
_ADDENDA_PROOF_CACHE = {}


def encoded(value):
    return (json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2) + '\n').encode()


def sha(raw):
    return hashlib.sha256(raw).hexdigest()


def require(ok, reason):
    if not ok:
        raise ValueError(reason)


def _date(value):
    require(isinstance(value, str) and dt.date.fromisoformat(value).isoformat() == value,
            'exam_year_plan_date_invalid')
    return value


def permits_point_fallback(exclusion):
    return exclusion.get('point_overlap_policy') in POINT_FALLBACK_POLICIES


def frequency_first_fallback(exclusion):
    return exclusion.get('point_overlap_policy') == FREQUENCY_FIRST_FALLBACK_POLICY


def _mapping(repo, expected_sha=None):
    path = Path(repo) / MAPPING_REL
    stat = path.stat()
    stamp = (stat.st_mtime_ns, stat.st_size, stat.st_ino)
    cached = _MAPPING_PATH_CACHE.get(str(path.resolve()))
    if cached and cached[0] == stamp:
        digest = cached[1]
        require(expected_sha is None or digest == expected_sha, 'exam_year_point_mapping_changed')
        return _MAPPING_CACHE[digest], digest
    raw = path.read_bytes()
    digest = sha(raw)
    require(expected_sha is None or digest == expected_sha, 'exam_year_point_mapping_changed')
    if digest not in _MAPPING_CACHE:
        value = json.loads(raw)
        require(isinstance(value.get('questions'), dict) and isinstance(value.get('subquestions'), dict),
                'exam_year_point_mapping_invalid')
        _MAPPING_CACHE[digest] = value
    _MAPPING_PATH_CACHE[str(path.resolve())] = (stamp, digest)
    return _MAPPING_CACHE[digest], digest


def _points(row):
    assessed = row.get('assessed_points')
    primary = row.get('primary_point')
    require(isinstance(assessed, list) and assessed and isinstance(primary, str),
            'exam_year_assessed_points_missing')
    points = set(assessed) | {primary}
    require(all(isinstance(p, str) and POINT_RE.fullmatch(p) for p in points),
            'exam_year_assessed_points_invalid')
    return points


def _stamp(path):
    stat = path.stat()
    return (stat.st_mtime_ns, stat.st_ctime_ns, stat.st_size, stat.st_ino)


def _addenda_document(repo):
    """Optional mutable tightening layer; never part of the frozen plan hash."""
    path = Path(repo) / ADDENDA_REL
    try:
        stamp = _stamp(path)
    except FileNotFoundError:
        return None
    key = str(path.resolve())
    cached = _ADDENDA_DOCUMENT_CACHE.get(key)
    if cached and cached[0] == stamp:
        return cached[1]
    raw = path.read_bytes()
    require(_stamp(path) == stamp, 'assessment_addenda_changed_during_read')
    doc = json.loads(raw)
    require(isinstance(doc, dict) and doc.get('schema') == ADDENDA_SCHEMA
            and isinstance(doc.get('items'), dict), 'assessment_addenda_schema_invalid')
    result = {'sha256': sha(raw), 'items': doc['items'], 'path': key, 'stamp': stamp}
    _ADDENDA_DOCUMENT_CACHE[key] = (stamp, result)
    return result


def _effective_points(repo, identity, row, item, mapping, addenda):
    """Union source-verified additions; neither removal nor replacement exists."""
    points = _points(row)
    metadata = {'additional_assessed_points': []}
    if addenda is None:
        return points, metadata
    metadata['addenda_sha256'] = addenda['sha256']
    entry = addenda['items'].get(identity)
    if entry is None:
        return points, metadata
    require(isinstance(entry, dict), 'assessment_addendum_invalid:' + identity)
    require(entry.get('review_status') == 'locally_source_verified',
            'assessment_addendum_source_review_required:' + identity)
    require(isinstance(entry.get('reason'), str) and entry['reason'].strip(),
            'assessment_addendum_reason_required:' + identity)
    expected = entry.get('surface_sha256')
    require(isinstance(expected, str) and re.fullmatch('[0-9a-f]{64}', expected),
            'assessment_addendum_surface_hash_invalid:' + identity)
    surface_hashes = [value for value in (item.get('surface_sha256'), row.get('surface_sha256'))
                      if value is not None]
    require(bool(surface_hashes), 'assessment_addendum_surface_hash_missing:' + identity)
    require(all(value == expected for value in surface_hashes),
            'assessment_addendum_surface_hash_drift:' + identity)
    additions = entry.get('additional_assessed_points')
    require(isinstance(additions, list) and additions and all(
        isinstance(point, str) and POINT_RE.fullmatch(point) for point in additions),
        'assessment_addendum_points_invalid:' + identity)
    known = set(mapping.get('point_entries', {}))
    for record in (*mapping['questions'].values(), *mapping['subquestions'].values()):
        known.update(record.get('assessed_points') or [])
        if record.get('primary_point'):
            known.add(record['primary_point'])
    require(set(additions) <= known, 'assessment_addendum_unknown_point:' + identity)
    refs = entry.get('source_refs')
    require(isinstance(refs, list) and refs, 'assessment_addendum_source_refs_required:' + identity)
    proof_paths = []
    for ref in refs:
        require(isinstance(ref, dict) and isinstance(ref.get('path'), str) and ref['path']
                and isinstance(ref.get('sha256'), str) and re.fullmatch('[0-9a-f]{64}', ref['sha256']),
                'assessment_addendum_source_ref_invalid:' + identity)
        path = Path(ref['path'])
        if not path.is_absolute():
            path = Path(repo) / path
        require(path.is_file(), 'assessment_addendum_source_missing:' + str(path))
        proof_paths.append((path, ref['sha256'], _stamp(path)))
    # File presence and stat are rechecked on every call. Hashes may be reused
    # only while both the addenda document and every evidence file stay fixed.
    key = (addenda['path'], addenda['sha256'], identity)
    stamps = (addenda['stamp'], tuple((str(path.resolve()), digest, stamp)
                                    for path, digest, stamp in proof_paths))
    if _ADDENDA_PROOF_CACHE.get(key) != stamps:
        for path, digest, stamp in proof_paths:
            require(sha(path.read_bytes()) == digest,
                    'assessment_addendum_source_hash_drift:' + str(path))
            require(_stamp(path) == stamp, 'assessment_addendum_source_changed_during_read:' + str(path))
        require(_stamp(Path(addenda['path'])) == addenda['stamp'], 'assessment_addenda_changed_during_read')
        _ADDENDA_PROOF_CACHE[key] = stamps
    additions = sorted(set(additions))
    metadata.update(additional_assessed_points=additions,
                    assessment_addendum_review_status=entry['review_status'],
                    assessment_addendum_reason=entry['reason'],
                    assessment_addendum_source_refs=refs)
    return points | set(additions), metadata


def build_exclusion(repo, day):
    """Freeze the explicit plan for one date; unknown required dates fail closed."""
    day = _date(day)
    path = Path(repo) / PLAN_REL
    base = {'schema': EXCLUSION_SCHEMA, 'target_date': day, 'active': False,
            'formal_write_count': 0, 'learner_evidence_write_count': 0}
    if not path.exists():
        return base
    raw = path.read_bytes()
    plan = json.loads(raw)
    require(plan.get('schema') == PLAN_SCHEMA and isinstance(plan.get('daily_plans'), dict),
            'exam_year_plan_schema_invalid')
    required = plan.get('required_from_date')
    if required is not None:
        _date(required)
    entry = plan['daily_plans'].get(day)
    require(entry is not None or required is None or day < required,
            'exam_year_plan_required_for_date:' + day)
    if entry is None:
        return base
    require(isinstance(entry, dict) and entry.get('source') == 'explicit_user',
            'exam_year_plan_explicit_user_source_required')
    require(entry.get('status') in {'planned', 'in_progress', 'completed'},
            'exam_year_plan_status_not_admissible')
    exclusion_only = entry.get('purpose') == 'morning_exact_year_exclusion'
    year = entry.get('excluded_exam_year') if exclusion_only else entry.get('exam_year')
    require(isinstance(year, int) and not isinstance(year, bool), 'exam_year_plan_year_invalid')
    mapping, digest = _mapping(repo)
    prefix = f'EXAM408-{year}-'
    parents = {k: v for k, v in mapping['questions'].items() if k.startswith(prefix)}
    # The accepted 408 corpus contains 40 choices and seven comprehensive
    # questions in every supported annual paper. Partial annual maps cannot
    # prove that the morning paper is disjoint from the protected paper.
    expected = {f'EXAM408-{year}-Q{i:02}' for i in range(1, 48)}
    require(set(parents) == expected, 'exam_year_plan_incomplete_annual_mapping:' + str(year))
    children = {k: v for k, v in mapping['subquestions'].items() if k.startswith(prefix)}
    sources = {}
    for identity, row in {**parents, **children}.items():
        for point in _points(row):
            sources.setdefault(point, []).append(identity)
    result = {**base, 'active': True, 'exam_year': year,
        'plan_path': PLAN_REL.as_posix(), 'plan_sha256': sha(raw),
        'daily_plan_sha256': sha(encoded(entry)), 'daily_plan': entry,
        'mapping_path': MAPPING_REL.as_posix(), 'mapping_sha256': digest,
        'protected_parent_ids': sorted(parents), 'protected_subquestion_ids': sorted(children),
        'excluded_assessed_points': [] if exclusion_only else sorted(sources),
        'point_sources': {} if exclusion_only else {p: sorted(ids) for p, ids in sorted(sources.items())},
        'basis': ('explicit_exact_year_identity_and_given_results_only' if exclusion_only
                  else 'reviewed_primary_and_assessed_points_excluding_prerequisite_and_context_only')}
    if exclusion_only:
        require('exam_year' not in entry, 'exclusion_only_must_not_schedule_annual_paper')
        result.update(exclusion_kind='exact_year_only', annual_paper_planned=False)
    override = entry.get('morning_point_overlap_policy')
    if override is not None:
        require(isinstance(override, dict) and override.get('policy') in POINT_FALLBACK_POLICIES
                and override.get('source') == 'explicit_user'
                and isinstance(override.get('user_statement'), str) and override['user_statement'].strip(),
                'exam_year_point_fallback_explicit_user_required')
        result['point_overlap_policy'] = override['policy']
        result['point_overlap_authorization'] = override
    result['exclusion_sha256'] = sha(encoded(result))
    return result


def assess_item(repo, item, exclusion):
    """Assess a candidate/return row, including exact prerequisite results."""
    if isinstance(item, str):
        item = {'true_item_id': item}
    preferred, actual = item.get('preferred_true_item_id'), item.get('true_item_id')
    identity = preferred or actual
    result = {'true_item_id': identity, 'allowed': True, 'reasons': [],
              'overlap_points': [], 'protected_question_ids': [], 'dependency_checks': []}
    if not exclusion.get('active'):
        return result
    if preferred and actual and preferred != actual:
        result['reasons'].append('true_item_identity_conflict')
    if not isinstance(identity, str) or not EXAM_RE.fullmatch(identity):
        result['reasons'].append('unknown_true_item_identity')
        result['allowed'] = False
        return result
    mapping, _ = _mapping(repo, exclusion['mapping_sha256'])
    records = {**mapping['questions'], **mapping['subquestions']}
    row = records.get(identity)
    if row is None:
        result['reasons'].append('unknown_true_item_mapping')
        result['allowed'] = False
        return result
    excluded = set(exclusion['excluded_assessed_points'])
    point_fallback = permits_point_fallback(exclusion)
    protected = set(exclusion['protected_parent_ids']) | set(exclusion['protected_subquestion_ids'])
    if int(EXAM_RE.fullmatch(identity).group(1)) == exclusion['exam_year']:
        result['reasons'].append('planned_exam_year_identity')
    try:
        addenda = _addenda_document(repo)
        if addenda:
            result['addenda_sha256'] = addenda['sha256']
        points, metadata = _effective_points(repo, identity, row, item, mapping, addenda)
        result.update(metadata)
    except (ValueError, OSError, TypeError) as exc:
        result['reasons'].append(str(exc))
        result['allowed'] = False
        return result
    overlap = points & excluded
    if overlap and not point_fallback:
        result['reasons'].append('planned_exam_year_assessed_point_overlap')
    targets = set(item.get('review_target_concept_ids') or [])
    if item.get('concept_id'):
        targets.add(item['concept_id'])
    # Eligibility surfaces use main_knowledge as their canonical target code.
    if POINT_RE.fullmatch(str(item.get('main_knowledge', ''))):
        targets.add(item['main_knowledge'])
    target_overlap = targets & excluded
    if target_overlap and not point_fallback:
        result['reasons'].append('planned_exam_year_target_point_overlap')
    overlap |= target_overlap
    dependencies = set(row.get('contact_dependency_ids') or []) | set(item.get('contact_dependency_ids') or [])
    for dependency in sorted(dependencies):
        record = records.get(dependency)
        check = {'true_item_id': dependency, 'overlap_points': [], 'allowed': True}
        if record is None:
            check.update(allowed=False, reason='unknown_prerequisite_result_mapping')
        else:
            try:
                dependency_points, dependency_metadata = _effective_points(
                    repo, dependency, record, {}, mapping, addenda)
                check.update(dependency_metadata)
                dep_overlap = dependency_points & excluded
                check['overlap_points'] = sorted(dep_overlap)
                check['allowed'] = dependency not in protected and (point_fallback or not dep_overlap)
                overlap |= dep_overlap
            except (ValueError, OSError, TypeError) as exc:
                check.update(allowed=False, reason=str(exc))
        if not check['allowed']:
            result['reasons'].append('planned_exam_year_prerequisite_result_conflict')
        result['dependency_checks'].append(check)
    result['overlap_points'] = sorted(overlap)
    result['protected_question_ids'] = sorted({qid for point in overlap
        for qid in exclusion['point_sources'].get(point, [])} | ({identity} if identity in protected else set()))
    result['reasons'] = sorted(set(result['reasons']))
    result['allowed'] = not result['reasons']
    if point_fallback:
        # This is eligibility for the secondary pool, not permission to select
        # it before the disjoint pool is exhausted. Selection validates both.
        result['exam_year_selection_stage'] = 1 if overlap else 0
        result['point_fallback_required'] = bool(overlap)
    return result


def audit_return(repo, body, exclusion=None):
    """Return a deterministic audit without changing files or learner state."""
    exclusion = exclusion if exclusion is not None else build_exclusion(repo, body['target_date'])
    require(exclusion['target_date'] == body['target_date'], 'exam_year_audit_date_mismatch')
    items = []
    for lane in ('selected_items', 'supplemental_items', 'substitute_items'):
        for item in body.get(lane, []):
            items.append({'lane': lane, 'question_id': item.get('question_id'),
                          **assess_item(repo, item, exclusion)})
    blocked = [item for item in items if not item['allowed']]
    return {'schema': AUDIT_SCHEMA, 'status': 'BLOCKED' if blocked else 'VERIFIED',
        'target_date': body['target_date'], 'active': exclusion['active'],
        'exam_year': exclusion.get('exam_year'), 'exclusion_sha256': exclusion.get('exclusion_sha256'),
        'checked_count': len(items), 'allowed_count': len(items) - len(blocked),
        'blocked_count': len(blocked), 'items': items,
        'formal_write_count': 0, 'learner_evidence_write_count': 0}


def _registered_exclusion_equivalence(repo, body, contract, current, audit):
    """Prove a mapping-container-only change from the registered input bytes.

    Neither the submitted contract nor the registration is rewritten. Every
    exclusion field except the full mapping hash and its derived exclusion
    hash must compare equal, while the actual return is audited afresh.
    """
    version = (contract or {}).get('input_version')
    require(isinstance(version, str) and re.fullmatch('[0-9a-f]{64}', version)
            and contract.get('target_date') == body['target_date'] == current['target_date']
            and contract.get('planned_exam_year') == current['exam_year']
            and contract.get('exam_year_plan_sha256') == current['plan_sha256'],
            'D_exam_year_plan_binding_missing_or_changed')
    # Import only at the mismatch boundary to share the canonical registry
    # location without adding another persistence or scheduling route.
    from morning_selection_408 import REGISTRY_REL
    registry_path = Path(repo) / REGISTRY_REL / (version + '.json')
    require(registry_path.is_file(), 'D_exam_year_equivalence_registration_missing')
    registry_raw = registry_path.read_bytes()
    registration = json.loads(registry_raw)
    require(registration.get('contract') == contract, 'D_selection_contract_drift')
    local_manifest = registration.get('local_input_manifest')
    if local_manifest:
        source = Path(local_manifest)
        require(source.is_absolute() and source.is_file() and not source.is_symlink(),
                'D_exam_year_local_selection_manifest_required')
        manifest_raw = source.read_bytes()
        require(sha(manifest_raw) == registration.get('local_input_manifest_sha256'),
                'D_exam_year_local_selection_manifest_hash_drift')
        original_contract_raw = (source.parent / 'selection-contract.json').read_bytes()
        old_raw = (source.parent / 'exam-year-exclusions.json').read_bytes()
        verified_source = {'local_input_manifest': str(source), 'local_input_manifest_sha256': sha(manifest_raw)}
    else:
        archive_text = registration.get('input_zip')
        archive_sha = registration.get('input_zip_sha256')
        require(isinstance(archive_text, str) and Path(archive_text).is_absolute()
                and isinstance(archive_sha, str) and re.fullmatch('[0-9a-f]{64}', archive_sha),
                'D_exam_year_equivalence_registered_archive_required')
        archive = Path(archive_text)
        require(archive.is_file(), 'D_exam_year_equivalence_registered_archive_missing')
        before = _stamp(archive)
        with archive.open('rb') as stream:
            actual_zip_sha = hashlib.file_digest(stream, 'sha256').hexdigest()
        require(actual_zip_sha == archive_sha, 'D_exam_year_registered_input_zip_hash_drift')
        with zipfile.ZipFile(archive) as zipped:
            def member(name):
                matches = [info for info in zipped.infolist() if info.filename == name]
                require(len(matches) == 1 and not matches[0].is_dir()
                        and matches[0].file_size <= 4 * 1024 * 1024,
                        'D_exam_year_equivalence_member_invalid:' + name)
                raw = zipped.read(matches[0])
                require(len(raw) == matches[0].file_size,
                        'D_exam_year_equivalence_member_size_drift:' + name)
                return raw
            manifest_raw = member('manifest.json')
            original_contract_raw = member('selection-contract.json')
            old_raw = member('exam-year-exclusions.json')
        require(_stamp(archive) == before, 'D_exam_year_registered_input_zip_changed_during_read')
        verified_source = {'input_zip': str(archive), 'input_zip_sha256': actual_zip_sha}
    require(registry_path.read_bytes() == registry_raw, 'D_exam_year_equivalence_registration_changed_during_read')
    manifest, original_contract, old = map(json.loads, (manifest_raw, original_contract_raw, old_raw))
    require(manifest.get('schema') == 'cs408-morning-selection-input-v1'
            and manifest.get('project') == 'D' and manifest.get('subject') == '408'
            and manifest.get('input_version') == version
            and manifest.get('target_date') == body['target_date']
            and manifest.get('source_commit') == contract.get('source_commit'),
            'D_exam_year_equivalence_input_manifest_binding_drift')
    require(original_contract == contract
            and sha(original_contract_raw) == registration.get('contract_sha256'),
            'D_exam_year_equivalence_input_contract_drift')
    for name, raw in (('selection-contract.json', original_contract_raw), ('exam-year-exclusions.json', old_raw)):
        rows = [row for row in manifest.get('files', []) if row.get('path') == name]
        require(len(rows) == 1 and rows[0].get('sha256') == sha(raw)
                and rows[0].get('size', rows[0].get('size_bytes')) == len(raw),
                'D_exam_year_equivalence_input_member_binding_drift:' + name)
    require(old.get('schema') == EXCLUSION_SCHEMA and old.get('active') is True
            and old.get('exclusion_sha256') == sha(encoded({k: v for k, v in old.items() if k != 'exclusion_sha256'}))
            and old['exclusion_sha256'] == contract.get('exam_year_exclusion_sha256')
            and old.get('mapping_sha256') == contract.get('exam_year_mapping_sha256')
            and old.get('plan_sha256') == contract.get('exam_year_plan_sha256'),
            'D_exam_year_equivalence_original_exclusion_binding_drift')
    ignored = {'mapping_sha256', 'exclusion_sha256'}
    old_semantics = {key: value for key, value in old.items() if key not in ignored}
    current_semantics = {key: value for key, value in current.items() if key not in ignored}
    require(old_semantics == current_semantics, 'D_exam_year_exclusion_semantics_changed')
    require(audit['status'] == 'VERIFIED' and audit['blocked_count'] == 0,
            'D_exam_year_equivalence_current_items_not_verified')
    proof = {'schema': EQUIVALENCE_SCHEMA, 'status': 'VERIFIED',
        'binding_mode': 'registered_exclusion_semantics_unchanged', 'target_date': body['target_date'],
        'exam_year': current['exam_year'], 'input_version': version,
        'old_exclusion_sha256': old['exclusion_sha256'], 'current_exclusion_sha256': current['exclusion_sha256'],
        'old_mapping_sha256': old['mapping_sha256'], 'current_mapping_sha256': current['mapping_sha256'],
        'plan_sha256': current['plan_sha256'], 'daily_plan_sha256': current['daily_plan_sha256'],
        'protection_semantics_sha256': sha(encoded(current_semantics)),
        'ignored_container_fields': sorted(ignored), 'verified_unchanged_fields': sorted(current_semantics),
        'current_return_audit_sha256': sha(encoded(audit)),
        'return_scope_sha256': sha(encoded({'target_date': body['target_date'], **{
            lane: body.get(lane, []) for lane in ('selected_items', 'supplemental_items', 'substitute_items')}})),
        'verification_source': {'registration_path': str(registry_path), 'registration_sha256': sha(registry_raw),
            **verified_source, 'input_manifest_sha256': sha(manifest_raw), 'contract_sha256': sha(original_contract_raw),
            'old_exclusion_member': 'exam-year-exclusions.json', 'old_exclusion_member_sha256': sha(old_raw)},
        'contract_rewritten': False, 'registration_rewritten': False,
        'formal_write_count': 0, 'learner_evidence_write_count': 0}
    proof['proof_sha256'] = sha(encoded(proof))
    return proof


def require_return(repo, body, contract=None):
    """Reject stale/unbound input or any conflict before native D import."""
    exclusion = build_exclusion(repo, body['target_date'])
    audit = audit_return(repo, body, exclusion)
    require(audit['blocked_count'] == 0, 'D_exam_year_exclusion_conflict:' + ','.join(
        str(item['true_item_id']) for item in audit['items'] if not item['allowed']))
    if exclusion['active']:
        if (contract or {}).get('exam_year_exclusion_sha256') != exclusion['exclusion_sha256']:
            audit['binding_equivalence_proof'] = _registered_exclusion_equivalence(repo, body, contract, exclusion, audit)
        if permits_point_fallback(exclusion):
            require((contract or {}).get('exam_year_point_overlap_policy') == exclusion['point_overlap_policy'],
                    'D_exam_year_point_fallback_contract_required')
    return audit


def require_learning_plan(repo, plan, execution_date):
    """Recheck the entire prepared queue before its first actual exposure.

    This is intentionally independent of the old package's target date, so an
    explicitly postponed queue cannot bypass the real day's annual-paper plan.
    Recovery of an already emitted display does not call this function.
    """
    exclusion = build_exclusion(repo, execution_date)
    if not exclusion['active']:
        return None
    rows = []
    for item in plan.get('learning_items', plan.get('supplemental_items', [])):
        selection = item.get('selection', {})
        rows.append({**item, **selection})
    require(bool(rows), 'D_exam_year_learning_items_missing')
    audit = audit_return(repo, {'target_date': execution_date,
                               'selected_items': rows}, exclusion)
    require(not audit['blocked_count'], 'D_exam_year_start_conflict:' + ','.join(
        str(item['true_item_id']) for item in audit['items'] if not item['allowed']))
    if permits_point_fallback(exclusion) and (frequency_first_fallback(exclusion)
            or any(item.get('point_fallback_required') for item in audit['items'])):
        from morning_selection_408 import require_annual_start_priorities
        audit['stage_priority'] = require_annual_start_priorities(repo, plan, execution_date, exclusion)
    return audit
