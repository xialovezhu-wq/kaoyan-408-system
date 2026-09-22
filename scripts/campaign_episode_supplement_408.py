"""Immutable late evidence for an already captured morning v2 episode."""
from pathlib import Path

import conversation_package_408 as packages
import intake_fact_capture_408 as captures
import morning_campaign_failure_terminal_408 as failure
import morning_campaign_turn_408 as turn
import study_episode_capture_408 as episodes


def record_supplement(body, locations, *, fault_injector=None):
    digest = turn.answer._safe_sha(body['display_receipt_sha256'], 'display_receipt_sha256')
    path = Path(locations['private_root']) / 'turns' / (digest + '.json')
    _, display, _ = turn.answer.reopen_campaign_display_receipt(path, digest)
    episode_id = f"{display['run_id']}:{display['item_id']}"
    repo, private = Path(locations['repo_root']), Path(locations['package_root'])
    receipt_path = episodes._operation_root(repo) / 'receipts' / (turn.sha(episode_id.encode()) + '.json')
    proof = episodes.verify_episode_capture(repo, receipt_path, package_root=private)
    origin = packages.verify_package_binding(proof['conversation_package_locator'],
        expected_canonical_sha256=proof['conversation_package_sha256'],
        require_sealed=True, private_root=private)
    source = origin['source']['source']
    if ((source.get('display_binding') or {}).get('display_receipt_sha256') != digest
            or (source.get('source') or {}).get('origin_lane') not in {'campaign_original', 'campaign_variant'}):
        raise turn.TurnError('supplement_campaign_binding_drift')
    additions = episodes._normalize_turns(body['new_turns'])
    if not any(row['role'] == 'user' for row in additions):
        raise turn.TurnError('supplement_requires_real_user_evidence')
    attachments = episodes._normalize_attachments(body.get('attachments', []))
    material = {**body, 'new_turns': additions,
                'attachments': [{k: v for k, v in row.items() if k != 'data'} for row in attachments]}
    material_sha = turn.sha(turn.encoded(material))
    key = turn.sha(turn.encoded([episode_id, body['request_id']]))
    journal = Path(locations['private_root']) / '.campaign-supplements-v1'
    request_path = journal / 'requests' / (key + '.json')
    capture_id = proof['capture_id']
    with failure._lock(journal, turn.sha(capture_id.encode())):
        state = captures.replay(captures._load_events(captures.capture_root(repo)))
        capture = state['captures'][capture_id]
        root_ref = {'locator': proof['conversation_package_locator'],
                    'sha256': proof['conversation_package_sha256']}
        if request_path.exists():
            binding = turn.read_json(request_path)
            if binding['material_sha256'] != material_sha:
                raise turn.TurnError('supplement_request_conflict')
        else:
            history = capture.get('supplement_history', [])
            binding = {'material_sha256': material_sha, 'parent': history[-1] if history else root_ref}
            failure._atomic_write(request_path, turn.encoded(binding))
        parent_ref = binding['parent']
        parent = packages.verify_package_binding(parent_ref['locator'],
            expected_canonical_sha256=parent_ref['sha256'], require_sealed=True, private_root=private)
        source_id = 'CAMPSUP-' + key
        session_id, item_id = display['run_id'], display['item_id']
        date = capture['study_date']
        package_id = packages.package_id_for(study_date=date, session_id=session_id,
                                            item_id=item_id, source_id=source_id)
        locator = packages.LOCATOR_PREFIX + package_id
        known = next((row for row in capture.get('supplement_history', []) if row['locator'] == locator), None)
        current_parent = (capture.get('supplement_history') or [root_ref])[-1]
        if known is None and parent_ref != current_parent:
            raise turn.TurnError('supplement_parent_changed')
        old_turns = [{k: v for k, v in row.items() if k != 'ordinal'}
                     for row in parent['conversation']['turns']]
        descriptors = [dict(data=(Path(parent['package_dir']) / row['relative_path']).read_bytes(),
                            **{k: row[k] for k in ('role', 'mime_type', 'label')})
                       for row in parent['manifest']['attachments']]
        seen = {(turn.sha(row['data']), row['role'], row['mime_type'], row['label']) for row in descriptors}
        for row in attachments:
            identity = (row['sha256'], row['role'], row['mime_type'], row['label'])
            if identity not in seen:
                descriptors.append({k: row[k] for k in ('data', 'role', 'mime_type', 'label')})
                seen.add(identity)
        # Keep the episode envelope recognizable to existing export/intake readers.
        supplement_source = {**source, 'evidence_revision_kind': 'campaign_episode_supplement',
            'episode_capture_id': capture_id,
            'root_package_locator': root_ref['locator'], 'root_package_sha256': root_ref['sha256'],
            'parent_package_locator': parent_ref['locator'], 'parent_package_sha256': parent_ref['sha256'],
            'supplement_request_payload_sha256': material_sha}
        if not (private / date / package_id / 'receipt.json').exists():
            packages.record_package(study_date=date, session_id=session_id, item_id=item_id,
                source_id=source_id, source=supplement_source, conversation=old_turns + additions,
                frozen_feedback='', attachments=descriptors,
                missing_fields=parent['manifest']['missing_fields'], seal=True, private_root=private)
        opened = packages.verify_package_binding(locator, require_sealed=True, private_root=private)
        expected_turns = [{'ordinal': i, **row} for i, row in enumerate(old_turns + additions, 1)]
        if opened['source']['source'] != supplement_source or opened['conversation']['turns'] != expected_turns:
            raise turn.TurnError('supplement_package_drift')
        if fault_injector:
            fault_injector('after_package')
        ref = {'locator': locator, 'sha256': opened['canonical_sha256']}
        committed = captures.append_campaign_supplement(capture_id, evidence=ref,
            parent_evidence=parent_ref, repo_root=repo, package_root=private,
            fault_injector=fault_injector)
        return {'status': 'episode_supplemented', 'run_id': display['run_id'], 'item_id': item_id,
            'capture_id': capture_id, 'capture_verified': True, 'package_verified': True,
            'conversation_package_locator': locator, 'conversation_package_sha256': ref['sha256'],
            'parent_evidence': parent_ref, 'supplement_event_id': committed['event_id'],
            'replayed': known is not None, 'frontier_advanced': False, 'formal_write_count': 0,
            'fsrs_write_count': 0, 'personal_summary_write_count': 0, 'navigation_write_count': 0}
