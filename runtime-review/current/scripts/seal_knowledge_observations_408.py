#!/usr/bin/env python3
"""Seal native semantic judgments against verified cold sources; never write learning facts."""
from __future__ import annotations

import argparse
from contextlib import nullcontext
import datetime as dt
import fcntl
import hashlib
import json
import os
import re
import tempfile
from pathlib import Path
from typing import Any

import knowledge_history_sources_408 as sources
from intake_lib_408 import Repo

SCHEMA = 'knowledge_history_semantic_annotations_v1'
ROLES = {'learner_report', 'observed_learner_evidence', 'assistant_explanation',
         'formal_summary', 'review_summary', 'historical_source_summary'}
RECORD_FIELDS = {'summary', 'knowledge_points', 'formal_ids', 'evidence_role', 'point_binding',
                 'source_fragment', 'source_order', 'observed_date', 'prompt_dependency',
                 'event_kind', 'outcome', 'correction_result', 'event_time'}


class AnnotationError(ValueError):
    pass


def _bytes(value: Any) -> bytes:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':')).encode()


def _validate(payload: Any, units: dict, master: dict, leaves: set[str]) -> dict:
    if not isinstance(payload, dict) or set(payload) != {'schema', 'units'} or payload.get('schema') != SCHEMA:
        raise AnnotationError('annotation_schema_invalid')
    if not isinstance(payload['units'], list) or not payload['units']:
        raise AnnotationError('annotation_units_required')
    result, seen = [], set()
    for unit in payload['units']:
        if (not isinstance(unit, dict) or not {'source_id', 'source_sha256', 'status', 'records'} <= set(unit)
                or set(unit) - {'source_id', 'source_sha256', 'status', 'records', 'disposition'}):
            raise AnnotationError('annotation_unit_fields_invalid')
        sid = unit['source_id']
        if not isinstance(sid, str) or sid in seen:
            raise AnnotationError('annotation_source_duplicate_or_invalid')
        seen.add(sid)
        original = units.get(sid)
        if original is None or original['source_sha256'] != unit['source_sha256']:
            raise AnnotationError('annotation_source_hash_mismatch')
        if unit['status'] != 'reviewed' or not isinstance(unit['records'], list):
            raise AnnotationError('annotation_review_status_invalid')
        records, record_keys = [], set()
        for record in unit['records']:
            if not isinstance(record, dict) or set(record) - RECORD_FIELDS:
                raise AnnotationError('annotation_record_fields_invalid')
            value = dict(record)
            summary = sources._safe_annotation(value.get('summary'))
            if summary is None:
                raise AnnotationError('annotation_summary_unsafe')
            codes = value.get('knowledge_points')
            if not isinstance(codes, list) or not codes or any(not isinstance(c, str) or c not in leaves for c in codes):
                raise AnnotationError('annotation_leaf_unknown')
            if value.get('evidence_role') not in ROLES:
                raise AnnotationError('annotation_evidence_role_invalid')
            if value.get('point_binding') not in {'explicit_point', 'question_associated'}:
                raise AnnotationError('annotation_point_binding_invalid')
            fragment = value.get('source_fragment')
            if not isinstance(fragment, str) or not fragment.strip() or len(fragment) > 500 or '\n' in fragment:
                raise AnnotationError('annotation_source_fragment_required')
            for code in set(codes):
                key = (code, fragment)
                if key in record_keys:
                    raise AnnotationError('annotation_record_fragment_collision')
                record_keys.add(key)
            ids = value.get('formal_ids', original.get('formal_ids', []))
            if not isinstance(ids, list) or any(not isinstance(fid, str) or fid not in master for fid in ids):
                raise AnnotationError('annotation_formal_identity_invalid')
            if original.get('formal_ids') and not set(ids) <= set(original['formal_ids']):
                raise AnnotationError('annotation_formal_identity_not_in_source')
            if 'source_order' in value and (type(value['source_order']) is not int or value['source_order'] < 0):
                raise AnnotationError('annotation_source_order_invalid')
            for field in ('observed_date', 'prompt_dependency', 'event_kind', 'outcome', 'correction_result', 'event_time'):
                if field in value and (not isinstance(value[field], str) or len(value[field]) > 100 or '\n' in value[field]):
                    raise AnnotationError('annotation_metadata_invalid:' + field)
            value.update(summary=summary, knowledge_points=sorted(set(codes)), formal_ids=sorted(set(ids)))
            records.append(value)
        result.append({**unit, 'records': records})
    return {'schema': SCHEMA, 'units': sorted(result, key=lambda u: u['source_id'])}


def _semantic_identity(unit):
    return {key: value for key, value in unit.items() if key != 'disposition'}


def seal(repo: str | Path, payload: Any, *, output_root: str | Path | None = None) -> dict:
    repo = Path(repo).expanduser().resolve()
    production = repo == sources.CANONICAL_REPO.resolve()
    if production and output_root is not None:
        raise AnnotationError('production_output_override_forbidden')
    if not production and output_root is None:
        raise AnnotationError('fixture_output_root_required')
    root = sources.SEMANTIC_ROOT.resolve() if production else Path(output_root).expanduser().resolve()
    if not production and (root == sources.SEMANTIC_ROOT.resolve() or sources.SEMANTIC_ROOT.resolve() in root.parents):
        raise AnnotationError('fixture_private_production_write_forbidden')
    master = Repo(repo).master()
    collected = sources.collect_history_sources(repo, master)
    units = {u['source_id']: u for u in collected['coverage']['semantic_units']}
    labels = (repo / '知识点标签表.md').read_text(encoding='utf-8')
    leaves = set(re.findall(r'^\s*-\s+((?:DS|CO|OS|CN)\d{2}-\d{2})\s+.+$', labels, re.M))
    envelope = _validate(payload, units, master, leaves)
    directory = root / 'annotations'
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    with (root / '.annotation-seal.lock').open('a') as lock:
        fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
        existing: dict[tuple[str, str], tuple[dict, Path]] = {}
        for path in sorted(directory.glob('*.json')):
            saved = json.loads(path.read_bytes())
            if saved.get('schema') != SCHEMA:
                raise AnnotationError('existing_annotation_schema_invalid')
            for unit in saved.get('units', []):
                if unit.get('status') == 'reviewed':
                    key = (unit['source_id'], unit['source_sha256'])
                    if key in existing and _semantic_identity(existing[key][0]) != _semantic_identity(unit):
                        raise AnnotationError('existing_annotation_conflict')
                    existing[key] = (unit, path)
        fresh, reused = [], []
        for unit in envelope['units']:
            old = existing.get((unit['source_id'], unit['source_sha256']))
            if old:
                # Canonicalize existing records too, including optional formal IDs.
                normalized = _validate({'schema': SCHEMA, 'units': [old[0]]}, units, master, leaves)['units'][0]
                if _semantic_identity(normalized) != _semantic_identity(unit):
                    raise AnnotationError('annotation_conflict_requires_explicit_replacement')
                reused.append(str(old[1]))
            else:
                fresh.append(unit)
        sha, target = None, None
        if fresh:
            raw = _bytes({'schema': SCHEMA, 'units': fresh})
            sha = hashlib.sha256(raw).hexdigest()
            target = directory / (sha + '.json')
            fd, tmp = tempfile.mkstemp(prefix='.seal-', dir=directory)
            try:
                with os.fdopen(fd, 'wb') as handle:
                    handle.write(raw)
                    handle.flush()
                    os.fsync(handle.fileno())
                os.replace(tmp, target)
                dirfd = os.open(directory, os.O_RDONLY)
                try:
                    os.fsync(dirfd)
                finally:
                    os.close(dirfd)
            finally:
                if os.path.exists(tmp):
                    os.unlink(tmp)
            if hashlib.sha256(target.read_bytes()).hexdigest() != sha:
                raise AnnotationError('annotation_readback_failed')
        return {'schema': 'knowledge_observation_seal_receipt_v1', 'status': 'sealed' if fresh else 'duplicate',
                'annotation_sha256': sha, 'annotation_path': str(target) if target else None,
                'existing_paths': sorted(set(reused)), 'sealed_units': len(fresh), 'duplicate_units': len(reused),
                'formal_write_count': 0, 'learning_ledger_write_count': 0, 'model_call_count': 0,
                'next_action': 'refresh_current_at_existing_formal_closeout'}


FORMAL_CONTRACT = 'formal-point-observations-408-v1'
FORMAL_RESULTS = {'wrong', 'partial', 'uncertain', 'independent_correct', 'screened_correct', 'relearn_required', 'correct'}
CORRECTION_SCHEMA = 'formal-observation-correction-408-v2'
CORRECTION_HEAD_SCHEMA = 'formal-observation-correction-head-408-v1'
_UNSET = object()


def validate_formal_review(repo, review, reopened, *, canonical=None):
    """Validate the owner's exhaustive point decisions against exact original turns."""
    if (not isinstance(review, dict) or set(review) != {'schema', 'scope_confirmed', 'points'}
            or review.get('schema') != FORMAL_CONTRACT or review.get('scope_confirmed') is not True
            or not isinstance(review.get('points'), list) or not review['points']):
        raise AnnotationError('formal_point_review_required')
    if reopened.get('kind') != 'cs408_conversation_package_v1':
        raise AnnotationError('formal_point_review_requires_complete_package')
    opened = reopened['package']
    turns = opened['conversation']['turns']
    study_date = opened['source']['study_date']
    leaves = set(re.findall(r'^\s*-\s+((?:DS|CO|OS|CN)\d{2}-\d{2})\s+.+$',
                          (Path(repo) / '知识点标签表.md').read_text(), re.M))
    points, seen = [], set()
    for point in review['points']:
        if (not isinstance(point, dict) or set(point) - {'knowledge_point', 'status', 'reason', 'records'}
                or point.get('knowledge_point') not in leaves or point['knowledge_point'] in seen):
            raise AnnotationError('formal_point_invalid_or_duplicate')
        code = point['knowledge_point']
        seen.add(code)
        records = point.get('records')
        if point.get('status') == 'no_observation':
            if records != [] or not sources._safe_annotation(point.get('reason')):
                raise AnnotationError('formal_no_observation_reason_required')
            points.append(dict(point))
            continue
        if point.get('status') != 'observed' or not isinstance(records, list) or not records:
            raise AnnotationError('formal_point_status_invalid')
        normalized, fragments = [], set()
        for record in records:
            if not isinstance(record, dict) or set(record) - (RECORD_FIELDS | {'evidence_refs'}):
                raise AnnotationError('formal_record_fields_invalid')
            item = dict(record)
            refs = item.get('evidence_refs')
            if not isinstance(refs, list) or not refs:
                raise AnnotationError('formal_original_evidence_required')
            original_turns = []
            for ref in refs:
                if (not isinstance(ref, dict) or set(ref) != {'turn', 'sha256'}
                        or type(ref['turn']) is not int or not 1 <= ref['turn'] <= len(turns)):
                    raise AnnotationError('formal_original_turn_invalid')
                turn = turns[ref['turn'] - 1]
                if hashlib.sha256(_bytes(turn)).hexdigest() != ref['sha256']:
                    raise AnnotationError('formal_original_turn_hash_mismatch')
                original_turns.append(turn)
            kind, role = item.get('event_kind'), item.get('evidence_role')
            if kind not in {'outcome', 'repair', 'hint', 'explanation'}:
                raise AnnotationError('formal_event_kind_invalid')
            learner = kind in {'outcome', 'repair'}
            if role not in ({'observed_learner_evidence', 'learner_report'} if learner else {'assistant_explanation'}):
                raise AnnotationError('formal_evidence_role_invalid')
            if learner and not any(t.get('role') in {'user', 'learner'} for t in original_turns):
                raise AnnotationError('formal_learner_turn_required')
            if not learner and not any(t.get('role') in {'assistant', 'tutor'} for t in original_turns):
                raise AnnotationError('formal_assistant_turn_required')
            dependency = item.get('prompt_dependency')
            if dependency not in {'independent', 'hint_or_prompted', 'unknown'}:
                raise AnnotationError('formal_prompt_dependency_required')
            outcome = item.get('outcome')
            correction = item.get('correction_result')
            if kind == 'outcome' and outcome not in FORMAL_RESULTS:
                raise AnnotationError('formal_outcome_required')
            if kind == 'repair' and correction not in FORMAL_RESULTS:
                raise AnnotationError('formal_repair_result_required')
            if (kind != 'outcome' and outcome is not None) or (kind != 'repair' and correction is not None):
                raise AnnotationError('formal_outcome_role_mismatch')
            if ('independent_correct' in {outcome, correction} and dependency != 'independent'):
                raise AnnotationError('formal_hint_cannot_be_independent_correct')
            if item.get('knowledge_points', [code]) != [code] or item.get('formal_ids', []) != []:
                raise AnnotationError('formal_record_point_or_identity_mismatch')
            fragment = item.get('source_fragment')
            if not isinstance(fragment, str) or not re.fullmatch(r'turn:\d+(?:#[A-Za-z0-9:_-]+)?', fragment):
                raise AnnotationError('formal_fragment_invalid')
            primary_turn = int(fragment.split('#')[0].split(':')[1])
            if primary_turn not in [ref['turn'] for ref in refs] or fragment in fragments:
                raise AnnotationError('formal_fragment_not_bound_or_duplicate')
            fragments.add(fragment)
            if item.get('observed_date', study_date) != study_date:
                raise AnnotationError('formal_original_date_mismatch')
            if item.get('source_order', primary_turn) != primary_turn or item.get('point_binding', 'explicit_point') != 'explicit_point':
                raise AnnotationError('formal_explicit_point_order_required')
            if item.get('event_time') is not None:
                raise AnnotationError('formal_event_time_derived_from_source_only')
            summary = sources._safe_annotation(item.get('summary'))
            if summary is None:
                raise AnnotationError('formal_summary_unsafe')
            item.update(summary=summary, knowledge_points=[code], formal_ids=[], point_binding='explicit_point',
                        source_order=primary_turn, observed_date=study_date)
            normalized.append(item)
        points.append({**point, 'records': normalized})
    canonical = canonical or {}
    tagged = set(re.findall(r'(?:DS|CO|OS|CN)\d{2}-\d{2}', json.dumps(
        {k: canonical.get(k) for k in ('main_knowledge', 'sub_knowledge', 'hit_knowledge')}, ensure_ascii=False)))
    if not tagged <= seen:
        raise AnnotationError('formal_tagged_point_disposition_missing')
    return {**review, 'points': sorted(points, key=lambda p: p['knowledge_point'])}


def _formal_root(repo, output_root=None):
    repo = Path(repo).resolve()
    production = repo == sources.CANONICAL_REPO.resolve()
    if production and output_root is not None:
        raise AnnotationError('production_output_override_forbidden')
    root = sources.SEMANTIC_ROOT if production else (Path(output_root) if output_root else repo / '.fixture-formal-knowledge')
    root = root.expanduser().resolve()
    if not production and (root == sources.SEMANTIC_ROOT.resolve() or sources.SEMANTIC_ROOT.resolve() in root.parents):
        raise AnnotationError('fixture_private_production_write_forbidden')
    return root / 'formal-annotations'


def _formal_unit(repo, terminal_locator, terminal_sha256, *, decision_root=None, package_root=None):
    import sol_curation_decision_408 as curation
    import conversation_package_408 as packages
    if decision_root is None and Path(repo).resolve() != sources.CANONICAL_REPO.resolve():
        decision_root = Path(repo) / '.fixture-curation-decisions'
    terminal = curation.read_terminal_binding(terminal_locator, terminal_sha256, decision_root=decision_root, repo=repo)
    if terminal['outcome'] not in {'curated', 'created', 'updated', 'already_current', 'skip', 'skipped', 'noop', 'duplicate'}:
        raise AnnotationError('formal_success_terminal_required')
    decision = curation.read_sealed_decision(terminal['decision_locator'], terminal['decision_sha256'], decision_root=decision_root)
    locator, sha = decision['evidence_locator'], decision['reopened_evidence_sha256']
    try:
        opened = packages.verify_package_binding(locator, private_root=package_root, expected_canonical_sha256=sha)
    except (packages.ConversationPackageError, FileNotFoundError):
        if Path(repo).resolve() != sources.CANONICAL_REPO.resolve():
            raise
        import archive_408_conversation_package as archive
        archived = archive.reopen_archived_package(repo=Path(repo), package_id=locator.removeprefix(packages.LOCATOR_PREFIX),
                      requested_relative_paths=['conversation.json', 'source.json'])
        if archived['package_sha256'] != sha:
            raise AnnotationError('formal_archived_package_sha_drifted')
        objects = {v['relative_path']: v['json'] for v in archived['requested_objects']}
        opened = {'conversation': objects['conversation.json'], 'source': objects['source.json']}
    reviewed = validate_formal_review(repo, decision['decision'].get('knowledge_observations'),
        {'kind': 'cs408_conversation_package_v1', 'package': opened}, canonical=decision['decision'].get('canonical_package'))
    records = []
    for point in reviewed['points']:
        for record in point['records']:
            records.append({**record, 'formal_ids': [terminal['formal_id']] if terminal.get('formal_id') else []})
    return {'source_id': locator, 'source_sha256': sha, 'status': 'reviewed', 'records': records,
            'point_dispositions': [{k: v for k, v in p.items() if k != 'records'} for p in reviewed['points']],
            'formal_binding': {'terminal_locator': terminal_locator, 'terminal_sha256': terminal_sha256}}


def seal_formal(repo, terminal_locator, terminal_sha256, *, output_root=None, decision_root=None, package_root=None):
    """Postcommit-only semantic closure; reruns only this missing layer."""
    unit = _formal_unit(repo, terminal_locator, terminal_sha256, decision_root=decision_root, package_root=package_root)
    directory = _formal_root(repo, output_root)
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    with (directory / '.seal.lock').open('a') as lock:
        fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
        raw = _bytes({'schema': SCHEMA, 'units': [unit]})
        sha = hashlib.sha256(raw).hexdigest()
        target = directory / (sha + '.json')
        for path in directory.glob('*.json'):
            existing = json.loads(path.read_bytes())
            for old in existing.get('units', []):
                if (old['source_id'], old['source_sha256']) == (unit['source_id'], unit['source_sha256']):
                    # A later already-current terminal does not replay the same episode.
                    identity = lambda u: {k: v for k, v in u.items() if k != 'formal_binding'}
                    if identity(old) != identity(unit):
                        raise AnnotationError('formal_annotation_conflict')
                    if path.stem != hashlib.sha256(path.read_bytes()).hexdigest():
                        raise AnnotationError('formal_annotation_hash_drifted')
                    return {'schema': 'knowledge_observation_seal_receipt_v1', 'status': 'duplicate',
                            'annotation_path': str(path), 'annotation_sha256': path.stem,
                            'formal_write_count': 0, 'learning_ledger_write_count': 0}
        fd, tmp = tempfile.mkstemp(prefix='.formal-seal-', dir=directory)
        try:
            os.fchmod(fd, 0o600)
            with os.fdopen(fd, 'wb') as handle:
                handle.write(raw)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(tmp, target)
            dirfd = os.open(directory, os.O_RDONLY)
            try:
                os.fsync(dirfd)
            finally:
                os.close(dirfd)
        finally:
            if os.path.exists(tmp):
                os.unlink(tmp)
        if target.read_bytes() != raw:
            raise AnnotationError('formal_annotation_readback_failed')
    return {'schema': 'knowledge_observation_seal_receipt_v1', 'status': 'sealed',
            'annotation_path': str(target), 'annotation_sha256': sha,
            'formal_write_count': 0, 'learning_ledger_write_count': 0}


def load_formal_records(repo, *, output_root=None, decision_root=None, package_root=None):
    """Bounded to sealed formal observations; does not inspect ordinary hot paths."""
    records, seen = [], set()
    for path in sorted(_formal_root(repo, output_root).glob('*.json')):
        raw = path.read_bytes()
        if path.is_symlink() or path.stem != hashlib.sha256(raw).hexdigest():
            raise AnnotationError('formal_annotation_hash_drifted')
        envelope = json.loads(raw)
        if envelope.get('schema') != SCHEMA:
            raise AnnotationError('formal_annotation_schema_invalid')
        for saved in envelope.get('units', []):
            binding = saved['formal_binding']
            unit = _formal_unit(repo, binding['terminal_locator'], binding['terminal_sha256'],
                decision_root=decision_root, package_root=package_root)
            if unit != saved:
                raise AnnotationError('formal_annotation_source_drifted')
            chain = load_formal_correction_history(_formal_root(repo, output_root), path.stem)
            correction = chain[-1] if chain else None
            if correction is not None:
                if (correction['source_id'] != unit['source_id']
                        or correction['source_sha256'] != unit['source_sha256']
                        or correction['formal_binding'] != binding):
                    raise AnnotationError('formal_correction_source_drifted')
                unit = {**unit, 'records': correction['records']}
            for record in unit['records']:
                key = (unit['source_id'], record['knowledge_points'][0], record['source_fragment'])
                if key in seen:
                    raise AnnotationError('formal_annotation_duplicate_observation')
                seen.add(key)
                records.append({**record, 'source_kind': 'semantic_formal_intake',
                    'source_id': unit['source_id'], 'source_locator': unit['source_id'],
                    'source_sha256': unit['source_sha256'], 'semantic_annotation_sha256': path.stem,
                    'event_time': record.get('event_time', ''), 'outcome': record.get('outcome'), 'formal_binding': binding,
                    'recorded_at': correction.get('recorded_at') if correction else envelope.get('recorded_at'),
                    'semantic_lifecycle_status': 'active',
                    **({'semantic_correction_sha256': correction['sha256'],
                        'correction_refs': [_correction_ref(value) for value in chain]} if correction else {})})
    return records


def _read_legacy_formal_correction(root, original_sha):
    path = Path(root) / 'corrections' / (original_sha + '.json')
    if not path.exists():
        return None
    if path.is_symlink():
        raise AnnotationError('formal_correction_symlink')
    value = json.loads(path.read_bytes())
    claimed = value.pop('sha256', None)
    if (value.get('schema') != 'formal-observation-correction-408-v1'
            or value.get('original_annotation_sha256') != original_sha
            or hashlib.sha256(_bytes(value)).hexdigest() != claimed):
        raise AnnotationError('formal_correction_hash_drifted')
    return {**value, 'sha256': claimed}


def _correction_ref(value):
    return {'source_kind': 'formal_semantic_correction', 'source_id': value['sha256'],
            'source_sha256': value['sha256'],
            'original_annotation_sha256': value['original_annotation_sha256'],
            'previous_correction_sha256': value.get('previous_correction_sha256'),
            'action': value.get('action', 'replace'), 'recorded_at': value.get('recorded_at')}


def load_formal_correction_history(root, original_sha):
    """Read one immutable correction chain. A legacy v1 correction is its root.

    Chain order comes from explicit predecessor hashes, never filesystem dates.
    Orphan objects from an interrupted pre-head write are not current revisions.
    """
    root = Path(root)
    if not isinstance(original_sha, str) or not re.fullmatch(r'[0-9a-f]{64}', original_sha):
        raise AnnotationError('formal_correction_original_identity_invalid')
    legacy = _read_legacy_formal_correction(root, original_sha)
    head_path = root / 'corrections' / 'heads' / (original_sha + '.json')
    if not head_path.exists():
        return [legacy] if legacy else []
    if head_path.is_symlink():
        raise AnnotationError('formal_correction_symlink')
    head = json.loads(head_path.read_bytes())
    if (head.get('schema') != CORRECTION_HEAD_SCHEMA
            or head.get('original_annotation_sha256') != original_sha
            or not re.fullmatch(r'[0-9a-f]{64}', str(head.get('head_sha256') or ''))):
        raise AnnotationError('formal_correction_head_invalid')
    backwards, seen, current = [], set(), head['head_sha256']
    while current:
        if current in seen:
            raise AnnotationError('formal_correction_chain_cycle')
        seen.add(current)
        if legacy and current == legacy['sha256']:
            backwards.append(legacy)
            current = None
            break
        path = root / 'corrections' / 'versions' / original_sha / (current + '.json')
        if path.is_symlink():
            raise AnnotationError('formal_correction_symlink')
        value = json.loads(path.read_bytes())
        claimed = value.pop('sha256', None)
        if (value.get('schema') != CORRECTION_SCHEMA
                or value.get('original_annotation_sha256') != original_sha
                or claimed != current or hashlib.sha256(_bytes(value)).hexdigest() != claimed
                or value.get('action') not in {'replace', 'retract'}
                or not isinstance(value.get('records'), list)
                or (value['action'] == 'retract' and value['records'])
                or not isinstance(value.get('recorded_at'), str)):
            raise AnnotationError('formal_correction_hash_drifted')
        try:
            stamp = dt.datetime.fromisoformat(value['recorded_at'])
            if stamp.tzinfo is None:
                raise ValueError('timezone_required')
        except ValueError as exc:
            raise AnnotationError('formal_correction_recorded_at_invalid') from exc
        previous = value.get('previous_correction_sha256')
        if previous is not None and not re.fullmatch(r'[0-9a-f]{64}', str(previous)):
            raise AnnotationError('formal_correction_predecessor_invalid')
        backwards.append({**value, 'sha256': claimed})
        current = previous
    ordered = list(reversed(backwards))
    if legacy and (not ordered or ordered[0]['sha256'] != legacy['sha256']):
        raise AnnotationError('formal_correction_legacy_predecessor_missing')
    return ordered


def _read_formal_correction(root, original_sha):
    chain = load_formal_correction_history(root, original_sha)
    return chain[-1] if chain else None


def formal_correction_paths(root, original_sha):
    """All committed dependencies, including the mutable head, for source guards."""
    root = Path(root)
    paths = []
    for value in load_formal_correction_history(root, original_sha):
        if value['schema'] == 'formal-observation-correction-408-v1':
            paths.append(root / 'corrections' / (original_sha + '.json'))
        else:
            paths.append(root / 'corrections' / 'versions' / original_sha / (value['sha256'] + '.json'))
    head = root / 'corrections' / 'heads' / (original_sha + '.json')
    if head.is_file():
        paths.append(head)
    return paths


def load_formal_record_history(repo, original_annotation_sha256, *, output_root=None,
                               decision_root=None, package_root=None):
    """Verified original and semantic revisions for one exact annotation.

    Old annotations lacking a recording timestamp remain explicitly undated.
    A retract revision has no active records; earlier evidence remains readable.
    """
    root = _formal_root(repo, output_root)
    if not re.fullmatch(r'[0-9a-f]{64}', str(original_annotation_sha256)):
        raise AnnotationError('formal_correction_original_identity_invalid')
    path = root / (original_annotation_sha256 + '.json')
    raw = path.read_bytes()
    if path.is_symlink() or hashlib.sha256(raw).hexdigest() != original_annotation_sha256:
        raise AnnotationError('formal_annotation_hash_drifted')
    envelope = json.loads(raw)
    units = envelope.get('units', [])
    if envelope.get('schema') != SCHEMA or len(units) != 1:
        raise AnnotationError('formal_correction_requires_one_source')
    unit = units[0]
    binding = unit['formal_binding']
    reopened = _formal_unit(repo, binding['terminal_locator'], binding['terminal_sha256'],
                            decision_root=decision_root, package_root=package_root)
    if reopened != unit:
        raise AnnotationError('formal_annotation_source_drifted')
    revisions = [{'revision_sha256': original_annotation_sha256,
                  'previous_correction_sha256': None, 'action': 'original',
                  'recorded_at': envelope.get('recorded_at'), 'author': None, 'reason': None,
                  'records': unit['records'], 'correction_refs': []}]
    refs = []
    for value in load_formal_correction_history(root, original_annotation_sha256):
        if (value['source_id'] != unit['source_id'] or value['source_sha256'] != unit['source_sha256']
                or value['formal_binding'] != binding):
            raise AnnotationError('formal_correction_source_drifted')
        refs = [*refs, _correction_ref(value)]
        revisions.append({'revision_sha256': value['sha256'],
                          'previous_correction_sha256': value.get('previous_correction_sha256'),
                          'action': value.get('action', 'replace'), 'recorded_at': value.get('recorded_at'),
                          'author': value.get('author'), 'reason': value.get('reason'),
                          'records': value['records'], 'correction_refs': refs})
    return {'original_annotation_sha256': original_annotation_sha256,
            'source_id': unit['source_id'], 'source_sha256': unit['source_sha256'],
            'knowledge_points': [p['knowledge_point'] for p in unit['point_dispositions']],
            'revisions': revisions, 'write_count': 0}


def _atomic_correction_json(path, value, *, immutable=False):
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    if path.is_symlink():
        raise AnnotationError('formal_correction_symlink')
    raw = _bytes(value)
    fd, temp = tempfile.mkstemp(prefix='.correction-', dir=path.parent)
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, 'wb') as stream:
            stream.write(raw); stream.flush(); os.fsync(stream.fileno())
        if immutable:
            try:
                os.link(temp, path)
            except FileExistsError:
                if path.read_bytes() != raw:
                    raise AnnotationError('formal_correction_concurrent_conflict')
        else:
            os.replace(temp, path)
        dirfd = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(dirfd)
        finally:
            os.close(dirfd)
    finally:
        if os.path.exists(temp):
            os.unlink(temp)
    if path.read_bytes() != raw:
        raise AnnotationError('formal_correction_readback_failed')


def _personal_review_metadata(repo, value, followup):
    """Read only affected frozen point summaries after projection refresh.

    A semantic correction does not author a new personal judgment. Reporting a
    refreshed cache must never imply those reviewed judgments are current.
    """
    points = sorted({p.get('knowledge_point') for p in value.get('point_dispositions', [])
                     if isinstance(p, dict) and p.get('knowledge_point')})
    state = {'status': 'pending_context_refresh', 'affected_points': points,
             'points': {}, 'source_version': None}
    required = None
    if followup.get('status') == 'NOT_PRODUCTION_REPO':
        state['status'] = 'fixture_only'
    elif (followup.get('local_tutoring') or {}).get('status') in {'refreshed', 'already_current'}:
        try:
            import exam_tutoring_context_408 as tutoring
            from knowledge_hot_routes_408 import resolve as resolve_routes, MAX_BUCKET_BYTES
            repo = Path(repo).resolve()
            snapshot = repo / tutoring.DEFAULT_STATE_REL
            manifest, _ = tutoring._load_manifest(snapshot)
            reasons = tutoring._freshness_reasons(repo, manifest)
            if reasons:
                state['reason'] = 'current_snapshot_not_ready'
            else:
                query = tutoring._validate_query({'schema': tutoring.QUERY_INPUT_SCHEMA,
                    'stage': 'post_answer', 'knowledge_points': points, 'max_profiles': 0})
                def read_entry(entry):
                    return tutoring._read_content_object(snapshot, entry.get('ref'), entry.get('sha256'),
                        'correction affected point', max_bytes=tutoring.MAX_POINT_SUMMARY_BYTES)
                routed = resolve_routes(manifest['knowledge_hot_routes'], query,
                    lambda entry: tutoring._read_content_object(snapshot, entry.get('ref'), entry.get('sha256'),
                        'correction affected route', max_bytes=MAX_BUCKET_BYTES), include_profiles=False)
                for point in points:
                    entry = routed['point_entries'].get(point)
                    if entry is None:
                        state['points'][point] = {'status': 'unavailable', 'reason': 'point_route_missing'}
                        continue
                    summary = read_entry(entry)
                    if summary.get('knowledge_point') != point:
                        raise ValueError('correction_review_point_identity_mismatch')
                    profile = summary.get('learning_profile') or {}
                    state['points'][point] = {'status': profile.get('status', 'unavailable'),
                        'reason': profile.get('reason'), 'profile_version': profile.get('version')}
                state['source_version'] = manifest['source_version']
                states = {row['status'] for row in state['points'].values()}
                if states & {'stale', 'not_authored'}:
                    required, state['status'] = True, 'required'
                elif points and states == {'current'}:
                    required, state['status'] = False, 'current'
                else:
                    state['reason'] = 'affected_personal_state_unavailable'
        except (OSError, ValueError, RuntimeError, KeyError, TypeError) as exc:
            state['reason'] = 'personal_review_state_unavailable:' + type(exc).__name__
    return {**followup, 'personal_review_required': required, 'personal_review': state}


def _finish_correction(repo, root, value):
    """Retry only projection/publication after the immutable semantic commit."""
    from study_commit_followup_408 import complete_after_commit
    event_id = 'formal-semantic-correction:' + value['sha256']
    receipt_path = root / 'corrections' / 'followups' / (value['sha256'] + '.json')
    followup = None
    if receipt_path.is_file() and not receipt_path.is_symlink():
        try:
            previous = json.loads(receipt_path.read_bytes())
            saved = previous.get('followup') or {}
            if (previous.get('correction_sha256') == value['sha256'] and (
                    saved.get('status') == 'NOT_PRODUCTION_REPO' or (
                        (saved.get('local_tutoring') or {}).get('status') in {'refreshed', 'already_current'}
                        and (saved.get('publication') or {}).get('status') == 'PUBLISHED'))):
                followup = saved
        except (OSError, ValueError, TypeError):
            pass
    if followup is None:
        try:
            followup = complete_after_commit(Path(repo).resolve(), event_id)
        except Exception as exc:
            followup = {'status': 'PENDING', 'reason': type(exc).__name__}
    followup = _personal_review_metadata(repo, value, followup)
    receipt = {'schema': 'formal-observation-correction-followup-408-v1',
               'correction_sha256': value['sha256'], 'followup': followup,
               'formal_write_count': 0, 'learner_event_write_count': 0, 'fsrs_write_count': 0}
    try:
        _atomic_correction_json(receipt_path, receipt)
    except (OSError, ValueError):
        return {**followup, 'followup_receipt_status': 'PENDING'}
    return followup


def correct_formal_observations(repo, *, original_annotation_sha256, review=None, reason, author,
                                action='replace', expected_previous_sha256=_UNSET, request_id=None,
                                output_root=None, decision_root=None, package_root=None):
    """Correct an owner's point interpretation without replaying learning or grading.

    The original terminal, annotation and raw conversation remain immutable.
    Only an explicitly supplied, source-validated semantic replacement is saved.
    """
    if not isinstance(reason, str) or not reason.strip() or not isinstance(author, str) or not author.strip():
        raise AnnotationError('formal_correction_author_and_reason_required')
    if action not in {'replace', 'retract'} or (action == 'retract' and review is not None):
        raise AnnotationError('formal_correction_action_invalid')
    if not re.fullmatch(r'[0-9a-f]{64}', str(original_annotation_sha256)):
        raise AnnotationError('formal_correction_original_identity_invalid')
    if request_id is not None and (not isinstance(request_id, str) or not request_id.strip() or len(request_id) > 200):
        raise AnnotationError('formal_correction_request_identity_invalid')
    root = _formal_root(repo, output_root)
    path = root / (original_annotation_sha256 + '.json')
    raw = path.read_bytes()
    if path.is_symlink() or hashlib.sha256(raw).hexdigest() != original_annotation_sha256:
        raise AnnotationError('formal_correction_original_hash_mismatch')
    saved = json.loads(raw)['units']
    if len(saved) != 1:
        raise AnnotationError('formal_correction_requires_one_source')
    unit = saved[0]
    binding = unit['formal_binding']
    original = _formal_unit(repo, binding['terminal_locator'], binding['terminal_sha256'],
                           decision_root=decision_root, package_root=package_root)
    if original != unit:
        raise AnnotationError('formal_correction_original_source_drifted')
    import sol_curation_decision_408 as curation
    import conversation_package_408 as packages
    try:
        reopened = curation.reopen_direct_evidence(unit['source_id'], unit['source_sha256'], package_root=package_root)
    except (ValueError, FileNotFoundError, packages.ConversationPackageError):
        if Path(repo).resolve() != sources.CANONICAL_REPO.resolve():
            raise
        import archive_408_conversation_package as archive
        archived = archive.reopen_archived_package(repo=Path(repo),
            package_id=unit['source_id'].split('://', 1)[1],
            requested_relative_paths=['conversation.json', 'source.json'])
        if archived['package_sha256'] != unit['source_sha256']:
            raise AnnotationError('formal_correction_archived_source_drifted')
        objects = {v['relative_path']: v['json'] for v in archived['requested_objects']}
        reopened = {'kind': 'cs408_conversation_package_v1', 'package': {
            'conversation': objects['conversation.json'], 'source': objects['source.json']}}
    validated = (validate_formal_review(repo, review, reopened) if action == 'replace' else
                 {'points': [{**p, 'records': []} for p in unit['point_dispositions']]})
    if {p['knowledge_point'] for p in validated['points']} != {p['knowledge_point'] for p in unit['point_dispositions']}:
        raise AnnotationError('formal_correction_point_scope_changed')
    formal_ids = sorted({fid for r in unit['records'] for fid in r.get('formal_ids', [])})
    records = [{**r, 'formal_ids': formal_ids} for point in validated['points'] for r in point['records']]
    request = {'schema': CORRECTION_SCHEMA,
             'original_annotation_sha256': original_annotation_sha256,
             'source_id': unit['source_id'], 'source_sha256': unit['source_sha256'],
             'formal_binding': binding, 'reason': reason, 'author': author,
             'action': action, 'request_id': request_id,
             'records': records, 'point_dispositions': [{k: v for k, v in p.items() if k != 'records'} for p in validated['points']],
             'formal_write_count': 0, 'learner_event_write_count': 0, 'fsrs_write_count': 0}
    request_sha = hashlib.sha256(_bytes(request)).hexdigest()
    root.mkdir(parents=True, exist_ok=True, mode=0o700)
    with (root / '.correction.lock').open('a') as lock:
        fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
        chain = load_formal_correction_history(root, original_annotation_sha256)
        previous = chain[-1]['sha256'] if chain else None
        matched = next((v for v in chain if v.get('request_sha256') == request_sha or (
            v.get('schema') == 'formal-observation-correction-408-v1' and action == 'replace'
            and request_id is None and all(v.get(key) == request.get(key) for key in
                ('original_annotation_sha256', 'source_id', 'source_sha256', 'formal_binding',
                 'reason', 'author', 'records', 'point_dispositions')))), None)
        if request_id and any(v.get('request_id') == request_id and v.get('request_sha256') != request_sha for v in chain):
            raise AnnotationError('formal_correction_request_content_conflict')
        if matched:
            value = matched
        else:
            if expected_previous_sha256 is not _UNSET and expected_previous_sha256 != previous:
                raise AnnotationError('formal_correction_previous_version_conflict')
            value = {**request, 'request_sha256': request_sha,
                     'previous_correction_sha256': previous,
                     'recorded_at': dt.datetime.now(dt.timezone.utc).isoformat()}
            value['sha256'] = hashlib.sha256(_bytes(value)).hexdigest()
            destination = root / 'corrections' / 'versions' / original_annotation_sha256 / (value['sha256'] + '.json')
            _atomic_correction_json(destination, value, immutable=True)
            # Serialize with snapshot publication. Revocation precedes the new
            # head so readers cannot call the previous interpretation current.
            from exam_tutoring_context_408 import DEFAULT_STATE_REL, _refresh_lock, invalidate_after_commit
            # Minimal isolated semantic fixtures have no master table and hence
            # no usable tutoring cache. Production always takes the native lock.
            refreshable = (Path(repo) / '节点总表.md').is_file()
            if Path(repo).resolve() == sources.CANONICAL_REPO.resolve() and not refreshable:
                raise AnnotationError('formal_correction_production_master_missing')
            with (_refresh_lock(Path(repo).resolve() / DEFAULT_STATE_REL) if refreshable else nullcontext()):
                if refreshable:
                    invalidate_after_commit(repo, 'formal-semantic-correction:' + value['sha256'])
                _atomic_correction_json(root / 'corrections' / 'heads' / (original_annotation_sha256 + '.json'),
                    {'schema': CORRECTION_HEAD_SCHEMA, 'original_annotation_sha256': original_annotation_sha256,
                     'head_sha256': value['sha256']})
            previous = value['sha256']
        latest = _read_formal_correction(root, original_annotation_sha256)
    # Locks are released before the existing projection and publisher owners run.
    followup = _finish_correction(repo, root, latest)
    destination = (root / 'corrections' / (original_annotation_sha256 + '.json')
                   if value['schema'] == 'formal-observation-correction-408-v1' else
                   root / 'corrections' / 'versions' / original_annotation_sha256 / (value['sha256'] + '.json'))
    return {'status': 'RETRACTED' if value.get('action') == 'retract' else 'CORRECTED',
            'path': str(destination), 'sha256': value['sha256'], 'current_revision_sha256': latest['sha256'],
            'recorded_at': value.get('recorded_at'), 'followup': followup,
            'personal_review_required': followup.get('personal_review_required'),
            'personal_review': followup.get('personal_review'),
            'formal_write_count': 0, 'learner_event_write_count': 0, 'fsrs_write_count': 0}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--repo', required=True)
    parser.add_argument('--input-json', required=True, help='Path to native semantic annotation JSON')
    parser.add_argument('--output-root', help='Private output root for nonproduction fixtures only')
    args = parser.parse_args()
    try:
        payload = json.loads(Path(args.input_json).read_bytes())
        print(json.dumps(seal(args.repo, payload, output_root=args.output_root), ensure_ascii=False))
        return 0
    except (AnnotationError, ValueError, OSError, KeyError, TypeError) as exc:
        print(json.dumps({'status': 'rejected', 'reason': str(exc)}, ensure_ascii=False))
        return 2


if __name__ == '__main__':
    raise SystemExit(main())
