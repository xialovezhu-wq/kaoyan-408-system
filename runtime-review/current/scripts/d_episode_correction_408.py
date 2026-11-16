"""Append-only completion-label correction for an already captured D episode."""
from pathlib import Path

import conversation_package_408 as packages
import d_concept_supplement_408 as native
import intake_fact_capture_408 as captures
import study_episode_capture_408 as episodes
import study_message_source_408 as messages
import study_transcript_408 as transcripts


def _read(path):
    import json
    return json.loads(Path(path).read_bytes())


def _current_evidence(expected, directory, offset):
    _, rows = messages._thread_messages()
    latest = max(i for i, row in enumerate(rows) if row['role'] == 'user')
    if rows[latest]['text'].rstrip('\r\n') != expected.rstrip('\r\n'):
        raise ValueError('D_correction_current_user_changed')
    selected = rows[latest:]
    turns = [dict(role=row['role'], kind='utterance' if row['text'] else 'image_attachment',
                  text=row['text'], observed_at=row['timestamp']) for row in selected]
    refs = [messages._proof(row) for row in selected]
    assets = messages._assets(selected, directory, complete=False, turn_offset=offset)
    return turns, refs, assets


def correct_outcome(body, repo, *, fault_injector=None):
    """Reuse a frozen correction request on retry; never resubmit or navigate."""
    allowed = {'learning_request_file', 'expected_user_text', 'terminal_state',
               'prompt_dependency', 'reason'}
    if set(body) != allowed or not body['expected_user_text'] or not body['reason']:
        raise ValueError('D_correction_input_invalid')
    if (body['terminal_state'], body['prompt_dependency']) not in {
            ('correct', 'none'), ('prompted_correct', 'minimal'),
            ('prompted_correct', 'progressive'), ('prompted_correct', 'full')}:
        raise ValueError('D_correction_completion_labels_invalid')
    repo = Path(repo).resolve()
    operation_root = native.writer.private_root(repo)
    private = episodes._default_package_root(repo)
    original_request = Path(body['learning_request_file']).resolve()
    original_request.relative_to((operation_root / 'd-learning').resolve())
    saved = _read(original_request)
    old_result = _read(original_request.with_name('result.json'))
    record = old_result['capture']['episode_receipts'][0]
    proof = episodes.verify_episode_capture(repo, record['receipt_ref'])
    root_ref = dict(locator=proof['conversation_package_locator'],
                    sha256=proof['conversation_package_sha256'])
    origin = packages.verify_package_binding(root_ref['locator'],
        expected_canonical_sha256=root_ref['sha256'], require_sealed=True, private_root=private)
    original_source = origin['source']['source']
    display = original_source['display_binding']
    if (display.get('schema') != 'cs408-D-supplement-display-v1'
            or display['receipt_sha256'] != saved['input']['display_receipt_sha256']
            or original_source['episode_id'] != proof['episode_id']
            or proof['capture_id'] != old_result['capture_id']):
        raise ValueError('D_correction_original_binding_drift')
    material_sha = native.writer.digest(body)
    directory = operation_root / 'd-corrections' / material_sha
    request_file = directory / 'request.json'
    result_file = directory / 'result.json'
    capture_id = proof['capture_id']
    with episodes._operation_lock(operation_root / 'd-corrections', capture_id):
        if request_file.exists():
            request = _read(request_file)
            if request['input'] != body:
                raise ValueError('D_correction_request_conflict')
        else:
            state = captures.replay(captures._load_events(captures.capture_root(repo)))
            capture = state['captures'][capture_id]
            # An already formalized record needs its formal owner, not a relabel.
            if capture['quality_status'] != 'awaiting_web_enhancement':
                raise ValueError('D_correction_requires_unconsumed_capture')
            parent_ref = (capture.get('supplement_history') or [root_ref])[-1]
            parent = packages.verify_package_binding(parent_ref['locator'],
                expected_canonical_sha256=parent_ref['sha256'], require_sealed=True, private_root=private)
            before = parent['source']['source']['observed_outcome']
            after = episodes._normalize_outcome({**before, 'terminal_state': body['terminal_state'],
                                                'prompt_dependency': body['prompt_dependency']})
            turns, refs, assets = _current_evidence(body['expected_user_text'],
                directory / 'source-evidence', len(parent['conversation']['turns']))
            request = dict(input=body, parent=parent_ref, before=before, after=after,
                           turns=turns, source_message_refs=refs, assets=assets)
            episodes._atomic_write(request_file, native.writer.encoded(request))
        parent_ref = request['parent']
        parent = packages.verify_package_binding(parent_ref['locator'],
            expected_canonical_sha256=parent_ref['sha256'], require_sealed=True, private_root=private)
        parent_source = parent['source']['source']
        correction = dict(before=request['before'], after=request['after'], reason=body['reason'],
                          user_message=request['turns'][0]['text'],
                          source_message_refs=request['source_message_refs'])
        source = {**parent_source, 'observed_outcome': request['after'],
            'evidence_revision_kind': 'd_outcome_correction', 'episode_capture_id': capture_id,
            'root_package_locator': root_ref['locator'], 'root_package_sha256': root_ref['sha256'],
            'parent_package_locator': parent_ref['locator'], 'parent_package_sha256': parent_ref['sha256'],
            'outcome_corrections': parent_source.get('outcome_corrections', []) + [correction]}
        old_turns = [{k: v for k, v in t.items() if k != 'ordinal'} for t in parent['conversation']['turns']]
        turns = old_turns + request['turns']
        attachments = [dict(data=(Path(parent['package_dir']) / a['relative_path']).read_bytes(),
                            **{k: a[k] for k in ('role', 'mime_type', 'label')})
                       for a in parent['manifest']['attachments']]
        additions = episodes._normalize_attachments(request['assets'])
        attachments += [{k: a[k] for k in ('data', 'role', 'mime_type', 'label')} for a in additions]
        date, session, item = proof['study_date'], origin['manifest']['session_id'], origin['manifest']['item_id']
        source_id = 'DCORR-' + material_sha
        package_id = packages.package_id_for(study_date=date, session_id=session, item_id=item, source_id=source_id)
        locator = packages.LOCATOR_PREFIX + package_id
        if not (private / date / package_id / 'receipt.json').exists():
            packages.record_package(study_date=date, session_id=session, item_id=item, source_id=source_id,
                source=source, conversation=turns, frozen_feedback='', attachments=attachments,
                missing_fields=parent['manifest']['missing_fields'], seal=True, private_root=private)
        opened = packages.verify_package_binding(locator, require_sealed=True, private_root=private)
        if (opened['source']['source'] != source
                or opened['conversation']['turns'] != [{'ordinal': i, **t} for i, t in enumerate(turns, 1)]):
            raise ValueError('D_correction_package_drift')
        if transcripts.verify_package(opened)['coverage'] != 'complete':
            raise ValueError('D_correction_message_coverage_incomplete')
        if fault_injector:
            fault_injector('after_package')
        ref = dict(locator=locator, sha256=opened['canonical_sha256'])
        event = captures.append_d_outcome_correction(capture_id, evidence=ref, parent_evidence=parent_ref,
            repo_root=repo, package_root=private, fault_injector=fault_injector)
        result = dict(status='episode_corrected', capture_id=capture_id,
            capture_verified=True, package_verified=True, observed_outcome=request['after'],
            previous_outcome=request['before'], conversation_package_locator=locator,
            conversation_package_sha256=ref['sha256'], supplement_event_id=event['event_id'],
            correction_request_file=str(request_file), result_file=str(result_file),
            frontier_advanced=False, navigation_write_count=0, formal_write_count=0,
            fsrs_write_count=0, personal_summary_write_count=0)
        episodes._atomic_write(result_file, native.writer.encoded(result))
        return result
