"""Advisory evidence screening for an already validated 408 enhancement return.

This module never changes the package, prepared receipt, review status, or any
learning record. Only a cold sidecar is written by the shared screening service.
"""
from __future__ import annotations

import hashlib
import importlib
import json
from pathlib import Path
import re
import sys
from typing import Any, Callable

import portable_visual_teaching_408 as portable
import web_transfer_root_408 as storage

import runtime_paths_408 as _paths

BRIDGE_ROOT = _paths.bridge_root()
PRODUCTION_REPO = _paths.repo_root()
MAX_SOURCE_BYTES = 256 * 1024
MAX_ITEMS = 2000
SHA = re.compile(r'[0-9a-f]{64}')


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':'))


def _unavailable(reason: str, count: int = 0) -> dict[str, Any]:
    return {'status': 'not_screened', 'reason': reason, 'report_path': None,
            'needs_review_count': 0, 'not_screened_count': count, 'checked_count': 0,
            'api_requests': 0, 'cache_hits': 0, 'advisory_only': True,
            'not_flagged_is_not_approved': True, 'formal_write_count': 0,
            'learner_evidence_write_count': 0}


def _conversation(repo: Path, capture: dict[str, Any], reopen: Callable) -> tuple[dict[str, Any] | None, str | None, list[tuple[str, str]]]:
    """Read the exact conversation member selected by the native verified reader."""
    try:
        locator, package_sha = capture['evidence_locator'], capture['evidence_sha256']
        opened = reopen(repo, locator, package_sha)
        if opened.get('kind') != 'cs408_conversation_package_v1' or opened.get('sha256') != package_sha:
            return None, 'unsupported_or_unverified_capture_evidence', []
        package = opened['package']
        members = [r for r in package['manifest']['files'] if r.get('relative_path') == 'conversation.json']
        if len(members) != 1 or not SHA.fullmatch(str(members[0].get('sha256', ''))):
            return None, 'conversation_member_binding_missing', []
        member = members[0]
        path = Path(package['package_dir']) / 'conversation.json'
        if path.is_symlink() or any(p.is_symlink() for p in path.parents) or not path.is_file():
            return None, 'conversation_member_path_unavailable', []
        if path.stat().st_size > MAX_SOURCE_BYTES:
            return None, 'conversation_evidence_exceeds_adapter_budget', []
        raw = path.read_bytes()
        if (len(raw) != member.get('byte_count')
                or hashlib.sha256(raw).hexdigest() != member['sha256']):
            return None, 'conversation_member_hash_drift', []
        text = raw.decode('utf-8')
        conversation = json.loads(text)
        complete = (isinstance(conversation, dict)
                    and conversation.get('complete_order_preserved') is True
                    and conversation.get('truncated') is False
                    and isinstance(conversation.get('turns'), list)
                    and conversation.get('turn_count') == len(conversation['turns'])
                    and not opened.get('missing_fields'))
        aliases = [(locator, package_sha), (str(path), member['sha256']),
                   (str(path.resolve()), member['sha256'])]
        if capture.get('capture_payload_sha256'):
            aliases.append(('capture://' + capture['capture_id'], capture['capture_payload_sha256']))
        return {'source_id': capture['capture_id'], 'sha256': member['sha256'],
                'text': text, 'role': 'conversation',
                'locator': locator + '#conversation.json'}, (None if complete else 'conversation_scope_incomplete'), aliases
    except (KeyError, TypeError, ValueError, OSError, RuntimeError):
        return None, 'verified_capture_evidence_unavailable', []


def collect_items(repo: Path, manifest: dict[str, Any], enhancement: dict[str, Any],
                  updates: dict[str, Any], *, reopen: Callable | None = None) -> list[dict[str, Any]]:
    """Project only assertions; original evidence stays whole and role-ordered."""
    reopen = reopen or portable.reopen_evidence
    capture_evidence, reference_map = {}, {}
    for capture in manifest.get('captures', []):
        evidence, problem, aliases = _conversation(repo, capture, reopen)
        capture_evidence[capture['capture_id']] = (evidence, problem)
        for alias in aliases:
            reference_map[alias] = (evidence, problem)
    items = []

    def sources(refs):
        selected, problems = {}, []
        for ref in refs or []:
            if not isinstance(ref, dict):
                problems.append('unresolved_source_reference'); continue
            pair = (ref.get('path', ref.get('locator')), ref.get('sha256'))
            evidence, problem = reference_map.get(pair, (None, 'referenced_evidence_not_in_verified_capture_scope'))
            if evidence:
                selected[evidence['source_id']] = evidence
            if problem:
                problems.append(problem)
        if not refs:
            problems.append('no_explicit_evidence_reference')
        return list(selected.values()), ';'.join(sorted(set(problems))) or None

    def add(field_path, value, kind, evidence, problem=None):
        if value is None or value == '' or value == [] or value == {}:
            return
        claim = value if isinstance(value, str) else _json(value)
        row = {'claim_id': '408-' + hashlib.sha256(field_path.encode()).hexdigest()[:24],
               'field_path': field_path, 'claim': claim, 'claim_kind': kind,
               'evidence': evidence, 'evidence_complete': bool(evidence) and not problem}
        if problem:
            row['evidence_problem'] = problem
        items.append(row)

    for i, item in enumerate(enhancement.get('items', [])):
        evidence, problem = capture_evidence.get(item.get('capture_id'), (None, 'capture_evidence_missing'))
        evidence = [evidence] if evidence else []
        decision = item.get('decision') or {}
        prefix = f'enhancement.json#/items/{i}/decision'
        for field in ('basis', 'counterevidence'):
            for j, claim in enumerate(decision.get(field) or []):
                add(f'{prefix}/{field}/{j}', claim, 'source_assertion', evidence, problem)
        canonical = decision.get('canonical_package') or {}
        assessment = canonical.get('review_backflow_assessment') or decision.get('review_backflow_assessment') or {}
        selected = {k: assessment[k] for k in ('first_result', 'prompt_level', 'reasoning_break', 'first_break', 'first_break_provenance') if k in assessment}
        assessment_path = prefix + ('/canonical_package' if canonical.get('review_backflow_assessment') else '') + '/review_backflow_assessment'
        add(assessment_path, selected, 'learning_state', evidence, problem)
        for j, point in enumerate((decision.get('knowledge_observations') or {}).get('points', [])):
            for k, record in enumerate(point.get('records') or []):
                value = {name: record[name] for name in ('summary', 'event_kind', 'evidence_role', 'prompt_dependency', 'outcome', 'correction_result', 'source_order', 'source_fragment') if name in record}
                add(f'{prefix}/knowledge_observations/points/{j}/records/{k}', value, 'learning_state', evidence, problem)
    for i, update in enumerate(updates.get('updates', [])):
        for suffix, target in [('', update), *[(f'/subpoints/{j}', row) for j, row in enumerate(update.get('subpoints') or [])]]:
            prefix = f'knowledge_updates.json#/updates/{i}{suffix}'
            evidence, problem = sources(target.get('source_refs'))
            for field in ('overview', 'short_summary'):
                add(prefix + '/' + field, target.get(field), 'learning_state', evidence, problem)
            focus = target.get('teaching_focus') or {}
            by_id = {r.get('id'): r for r in focus.get('source_refs', []) if isinstance(r, dict)}
            for j, facet in enumerate(focus.get('facets') or []):
                refs = [by_id.get(sid, {}) for sid in facet.get('source_ids', [])]
                selected_evidence, selected_problem = sources(refs)
                claim = {k: facet[k] for k in ('mechanism', 'status', 'basis') if k in facet}
                add(f'{prefix}/teaching_focus/facets/{j}', claim, 'learning_state', selected_evidence, selected_problem)
            # This remains a source relation check, never a new preference inference.
            if target.get('teaching_preferences') is not None:
                add(prefix + '/teaching_preferences', target['teaching_preferences'], 'teaching_preference', evidence, problem)
    return items


def _shared_runner():
    if str(BRIDGE_ROOT) not in sys.path:
        sys.path.insert(0, str(BRIDGE_ROOT))
    return importlib.import_module('study_evidence_prescreen').run_prescreen


def run_advisory(*, repo: Path, details_root: Path, manifest: dict[str, Any],
                 enhancement: dict[str, Any], updates: dict[str, Any],
                 package_sha256: str, runner: Callable | None = None,
                 reopen: Callable | None = None) -> dict[str, Any]:
    """Never gate prepare/apply; unavailable services leave original review intact."""
    runner_started = False
    items = []
    try:
        # Existing fixture suites must not acquire production credentials. An
        # explicit injected runner exercises this branch without network calls.
        if runner is None and (repo.resolve() != PRODUCTION_REPO or
                               not storage._is_production(details_root)):
            return _unavailable('isolated_repository_no_model_calls')
        items = collect_items(repo, manifest, enhancement, updates, reopen=reopen)
        if len(items) > MAX_ITEMS:
            return _unavailable('prescreen_candidate_budget_exceeded', len(items))
        if not SHA.fullmatch(package_sha256) or not re.fullmatch(r'\d{4}-\d{2}-\d{2}', manifest['study_date']):
            return _unavailable('prescreen_identity_invalid', len(items))
        base, _ = storage.base_for('web-return-staging', details_root, 'review-deliveries/web-return-staging')
        output = storage.validate_cold_output(base / 'evidence-prescreen' / manifest['study_date'] / package_sha256,
                                              details=details_root)
        selected_runner = runner or _shared_runner()
        runner_started = True
        result = selected_runner(
            subject='408', artifact_id=package_sha256, source_version=manifest['source_commit'],
            items=items, output_dir=output, context={
                'adapter_version': 'cs408-enhancement-evidence-prescreen-v1',
                'run_id': manifest['run_id'], 'package_sha256': package_sha256,
                'evidence_scope': 'complete verified conversation text; images are not interpreted',
                'native_review_required': True,
            })
        if not isinstance(result, dict):
            return {**_unavailable('prescreen_response_invalid', len(items)), 'api_requests': None}
        result = dict(result)
        result.update(advisory_only=True, not_flagged_is_not_approved=True,
                      formal_write_count=0, learner_evidence_write_count=0)
        try:
            if output.exists():
                result['storage_registration'] = storage.register_output(
                    output, details=details_root, purpose='408网页增强回包证据疑点预筛旁路报告；不改变审核或学习状态')
        except Exception as exc:
            result['storage_registration'] = {'status': 'pending', 'error_type': type(exc).__name__}
        return result
    except Exception as exc:
        # No provider response or exception message is copied into the receipt.
        return {**_unavailable('prescreen_unavailable', len(items)),
                'api_requests': None if runner_started else 0,
                'error_type': type(exc).__name__}
