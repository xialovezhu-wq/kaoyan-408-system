#!/usr/bin/env python3
"""One v2 learning entry: preserve evidence, route owners, return teaching context."""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys

import morning_campaign_turn_408 as turn
import study_episode_capture_408 as episodes
import intake_fact_capture_408 as captures
from study_message_source_408 import resolve_messages, MessageSourceError

SCHEMA = 'cs408-learning-turn-v1'
INTENTS = {'discuss', 'complete', 'pause', 'end', 'skip', 'correct-late'}
PROMPTS = {'none': 0, 'minimal': 1, 'progressive': 2, 'full': 3, 'not_observed': -1}


def _write(path, value):
    turn.failure._atomic_write(path, turn.encoded(value))


def _validate(body):
    required = {'schema', 'request_id', 'display_receipt_sha256', 'intent'}
    optional = {'new_turns', 'message_refs', 'response', 'reasoning_result', 'prompt_dependency',
                'attachments', 'missing_fields', 'teaching_concepts', 'teaching_mechanisms', 'need_context',
                'recognized_original', 'response_seconds'}
    if not isinstance(body, dict) or set(body) - required - optional or required - set(body):
        raise turn.TurnError('learning_input_fields_invalid')
    if body['schema'] != SCHEMA or body['intent'] not in INTENTS:
        raise turn.TurnError('learning_intent_invalid')
    if ('new_turns' in body) == ('message_refs' in body):
        raise turn.TurnError('provide_exactly_one_of_new_turns_or_message_refs')
    turn.answer._safe_id(body['request_id'], 'request_id')
    turn.answer._safe_sha(body['display_receipt_sha256'], 'display_receipt_sha256')
    response = body.get('response')
    if response is not None and response not in turn.episode_terminal.RESPONSES:
        raise turn.TurnError('response_invalid')
    if body.get('reasoning_result', 'not_observed') not in turn.episode_terminal.REASONING_RESULTS:
        raise turn.TurnError('reasoning_result_invalid')
    if body.get('prompt_dependency', 'not_observed') not in PROMPTS:
        raise turn.TurnError('prompt_dependency_invalid')
    if not isinstance(body.get('need_context', False), bool):
        raise turn.TurnError('need_context_invalid')
    if (not isinstance(body.get('missing_fields', []), list)
            or not all(isinstance(value, str) for value in body.get('missing_fields', []))):
        raise turn.TurnError('missing_fields_invalid')
    episodes._normalize_attachments(body.get('attachments', []))
    if 'teaching_concepts' in body:
        from knowledge_learning_records_408 import canonical_knowledge_points
        concepts = body['teaching_concepts']
        if (not isinstance(concepts, list) or len(concepts) > 16
                or any(not isinstance(code, str) or canonical_knowledge_points(code) != [code] for code in concepts)):
            raise turn.TurnError('teaching_concepts_must_be_canonical_codes')
    if response is not None:
        turn.answer._normalize_inputs(response=response, confidence=None,
            recognized_original=body.get('recognized_original', False), response_seconds=body.get('response_seconds'))
    if 'teaching_mechanisms' in body:
        terms = body['teaching_mechanisms']
        if (not isinstance(terms, list) or len(terms) > 8
                or any(not isinstance(t, str) or not t.strip() or len(t) > 200 for t in terms)):
            raise turn.TurnError('teaching_mechanisms_invalid')


def _prepare(body, locations, state):
    digest = body['display_receipt_sha256']
    path = Path(locations['private_root']) / 'turns' / (digest + '.json')
    _, display, _ = turn.answer.reopen_campaign_display_receipt(path, digest)
    saving = turn.runtime.saving_contract_for_run(locations['runtime_root'], display['run_id'])
    if not saving['episode_capture_every_item'] or display.get('saving_contract_sha256') != saving['saving_contract_sha256']:
        raise turn.TurnError('learning_entry_requires_bound_v2')
    if 'message_refs' in body:
        if not isinstance(body['message_refs'], list) or not body['message_refs']:
            raise turn.TurnError('message_refs_invalid')
        rows, refs = resolve_messages(body['message_refs'])
        if any(ref['has_images'] for ref in refs) and not body.get('attachments'):
            raise turn.TurnError('message_images_require_original_attachments')
        seen = set(state.get('message_ids', []))
        pairs = [(row, ref) for row, ref in zip(rows, refs) if ref['message_id'] not in seen]
        rows, refs = [p[0] for p in pairs], [p[1] for p in pairs]
        evidence_source = 'verified_transcript'
    else:
        rows, refs = body['new_turns'], []
        evidence_source = 'caller_supplied_verbatim_not_independently_verified'
    rows = turn.episode_terminal._turns(rows)
    if not rows or not any(row['role'] == 'user' for row in rows):
        raise turn.TurnError('new_real_user_message_required')
    if body['intent'] == 'correct-late':
        return dict(display=display, rows=rows, message_refs=refs, evidence_source=evidence_source)
    _, _, frontier, current = turn.runtime.reopen_verified_frontier(locations['runtime_root'], display['run_id'])
    if (not current or current['item_id'] != display['item_id']
            or frontier['status'] not in {'active', 'episode_pending_capture'}):
        raise turn.TurnError('current_display_changed_or_paused')
    _, draft = turn.episode_terminal._draft(
        draft_root=Path(locations['draft_root']) / 'episode-v2', display=display, display_sha=digest)
    if draft.get('capture_verified'):
        raise turn.TurnError('sealed_episode_requires_correct_late')
    pending = (frontier.get('pending_episode') or {}).get('payload') or {}
    dependencies = [body.get('prompt_dependency', 'not_observed'),
                    state.get('prompt_dependency', 'not_observed'),
                    pending.get('initial_prompt_dependency', 'not_observed')]
    if any(row['role'] == 'assistant' and row['kind'] in {'hint', 'correction'} for row in draft['turns'] + rows):
        dependencies.append('minimal')
    prompt = max(dependencies, key=lambda value: PROMPTS[value])
    reasoning = body.get('reasoning_result', 'not_observed')
    response = body.get('response')
    correct = response is not None and response == turn.answer.correct_letter_for_display(display)
    complete = (body['intent'] == 'complete' and correct and reasoning == 'sound'
                and prompt != 'not_observed')
    terminal = ('correct' if prompt == 'none' else 'prompted_correct') if complete else None
    if body['intent'] in {'end', 'skip'}:
        terminal = 'unresolved'
    # Capturing an independent result cannot erase a previously observed hint.
    return dict(display=display, rows=rows, message_refs=refs, evidence_source=evidence_source,
                draft=draft, had_answer=bool(pending), prompt=prompt,
                reasoning=reasoning, response=response, correct=correct, terminal=terminal)


def _public(result, plan, body, request_file):
    keys = {'status', 'reason', 'run_id', 'item_id', 'choice_result', 'reason_required',
            'capture_verified', 'package_verified', 'capture_id', 'capture_receipt_ref',
            'conversation_package_locator', 'conversation_package_sha256', 'terminal_state',
            'frontier_advanced', 'next_display', 'next_image_paths', 'image_preload_status',
            'image_preload_reason', 'navigation_held', 'campaign_complete', 'progress',
            'turn_count', 'trace_sha256', 'request_file', 'result_file',
            'formal_write_count', 'fsrs_write_count', 'personal_summary_write_count',
            'supplement_event_id', 'parent_evidence', 'replayed'}
    value = {key: result[key] for key in keys if key in result}
    value.update(schema='cs408-learning-turn-result-v1', learning_request_file=str(request_file),
                 evidence_source=plan['evidence_source'],
                 source_message_refs=plan['message_refs'],
                 current_display_receipt_sha256=body['display_receipt_sha256'])
    if 'current_item_personalization' in result:
        value['current_item_personalization'] = {
            k: v for k, v in result['current_item_personalization'].items() if k != 'local_records_markdown'}
    if result.get('status') == 'blocked':
        action = 'recover_same_request'
    elif result.get('status') == 'episode_supplemented':
        action = 'continue_current_item'
    elif result.get('capture_verified') and result.get('package_verified'):
        action = ('hold' if result.get('navigation_held') else
                  'report_completion' if result.get('campaign_complete') else
                  'present_next' if result.get('next_display') and result.get('image_preload_status') != 'blocked'
                  else 'recover_delivery')
    elif body['intent'] == 'pause':
        action = 'hold'
    elif body.get('reasoning_result', 'not_observed') == 'not_observed' and body.get('response') is not None:
        action = 'ask_reason'
    else:
        action = 'teach_current'
    value['next_action'] = action
    value['celebration_eligible'] = bool(
        result.get('capture_verified') and result.get('package_verified')
        and result.get('terminal_state') in {'correct', 'prompted_correct'}
        and not result.get('replayed') and result.get('status') != 'blocked')
    return value


def submit(body, locations, *, fault_injector=None):
    _validate(body)
    journal = Path(locations['private_root']) / '.learning-turns-v1'
    display_sha = body['display_receipt_sha256']
    key = turn.sha(turn.encoded([display_sha, body['request_id']]))
    folder = journal / 'requests' / key
    request_file, result_file = folder / 'request.json', folder / 'result.json'
    state_file = journal / 'states' / (display_sha + '.json')
    with turn.failure._lock(journal, display_sha):
        state = turn.read_json(state_file) if state_file.exists() else {}
        if request_file.exists():
            frozen = turn.read_json(request_file)
            if frozen['input'] != body or frozen['locations'] != locations:
                raise turn.TurnError('learning_request_conflict')
            plan = frozen['plan']
        else:
            if state.get('inflight'):
                raise turn.TurnError('resume_inflight_request:' + state['inflight'])
            plan = _prepare(body, locations, state)
            _write(request_file, dict(input=body, locations=locations, plan=plan))
        if result_file.exists():
            result = turn.read_json(result_file)
            if state.get('inflight') == str(request_file):
                _write(state_file, _completed_state(state, plan))
            result.update(replayed=True, celebration_eligible=False)
            if result.get('next_display'):
                _, _, _, current = turn.runtime.reopen_verified_frontier(locations['runtime_root'], plan['display']['run_id'])
                if not current or current.get('item_id') != result['next_display'].get('item_id'):
                    result.update(next_display=None, next_image_paths=[], next_action='continue_current_item', delivery_stale=True)
            return result
        _write(state_file, {**state, 'inflight': str(request_file)})

        def perform(name, operation, payload):
            saved_input, saved_result = folder / (name + '-input.json'), folder / (name + '-result.json')
            if saved_input.exists():
                if turn.read_json(saved_input) != payload:
                    raise turn.TurnError('learning_step_input_drift')
            else:
                _write(saved_input, payload)
            if saved_result.exists():
                return turn.read_json(saved_result)
            result = turn.run(operation, payload, locations)
            if result.get('status') != 'blocked':
                _write(saved_result, result)
            if fault_injector:
                fault_injector('after_' + name)
            return result

        if body['intent'] == 'correct-late':
            from campaign_episode_supplement_408 import record_supplement
            result = record_supplement(dict(display_receipt_sha256=display_sha,
                request_id=body['request_id'], new_turns=plan['rows'], attachments=body.get('attachments', [])), locations)
        else:
            common = dict(display_receipt_sha256=display_sha)
            mechanism_kw = ({'teaching_mechanisms': body['teaching_mechanisms']}
                            if 'teaching_mechanisms' in body else {})
            prefix = 'learning:' + key[:32]
            additions = plan['rows']
            cursor = plan['draft']
            result = None
            if not plan['had_answer'] and plan['response'] is not None:
                result = perform('answer', 'answer-choice', dict(common,
                    idempotency_key=prefix + ':answer', response=plan['response'],
                    recognized_original=body.get('recognized_original', False),
                    response_seconds=body.get('response_seconds'), reasoning_result=plan['reasoning'],
                    prompt_dependency=plan['prompt'] if plan['prompt'] != 'not_observed' else 'none',
                    learning_turns=cursor['turns'] + additions,
                    teaching_concepts=body.get('teaching_concepts', []), **mechanism_kw))
                if result.get('status') != 'blocked':
                    cursor, additions = result, []
            if result is None or result.get('status') != 'blocked':
                base = dict(common, expected_prior_turn_count=cursor['turn_count'],
                            expected_prior_trace_sha256=cursor['trace_sha256'], new_turns=additions)
                if plan['terminal']:
                    if plan['terminal'] == 'unresolved':
                        result = perform('terminal', 'close-unresolved', dict(base,
                            idempotency_key=prefix + ':terminal', attachments=body.get('attachments', []),
                            missing_fields=body.get('missing_fields', []),
                            navigation_policy='hold_after_capture' if body['intent'] == 'end' else 'publish_successor'))
                    else:
                        result = perform('terminal', 'finish-item', dict(base,
                            idempotency_key=prefix + ':terminal', terminal_state=plan['terminal'],
                            final_response=plan['response'], reasoning_result=plan['reasoning'],
                            prompt_dependency=plan['prompt'], attachments=body.get('attachments', []),
                            missing_fields=body.get('missing_fields', []), navigation_policy='publish_successor'))
                elif additions:
                    result = perform('append', 'append-turn', dict(base,
                        idempotency_key=prefix + ':append', teaching_concepts=body.get('teaching_concepts', []), **mechanism_kw))
            # Teaching does not require a final option: partial reasoning and
            # concept help must receive personal context even when callers omit it.
            teaching_discussion = (
                body['intent'] in {'discuss', 'complete'} and not plan['terminal']
                and (plan['response'] is None or not plan['correct']
                     or plan['reasoning'] in {'partial', 'diverged'})
            )
            need_context = body.get('need_context') or teaching_discussion
            if need_context and result.get('status') != 'blocked' and 'current_item_personalization' not in result:
                result['current_item_personalization'] = turn.response_personalization({
                    'roots': locations, 'teaching_concepts': body.get('teaching_concepts', []),
                    **mechanism_kw,
                    'input': {'display_receipt_path': str(Path(locations['private_root']) / 'turns' / (display_sha + '.json')),
                              'display_receipt_sha256': display_sha}})
        public = _public(result, plan, body, request_file)
        if result.get('status') != 'blocked':
            _write(result_file, public)
            if fault_injector:
                fault_injector('after_result')
            _write(state_file, _completed_state(state, plan))
        return public


def _completed_state(state, plan):
    return {'message_ids': list(dict.fromkeys([
        *state.get('message_ids', []), *[ref['message_id'] for ref in plan['message_refs']]])),
        'prompt_dependency': plan.get('prompt', state.get('prompt_dependency', 'not_observed'))}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--input-json', required=True)
    for name in turn.ROOT_FIELDS:
        parser.add_argument('--' + name.replace('_', '-'))
    args = parser.parse_args(argv)
    try:
        body = json.load(sys.stdin) if args.input_json == '-' else turn.read_json(Path(args.input_json))
        if 'input' in body and 'locations' in body and 'plan' in body:
            locations, body = body['locations'], body['input']
        else:
            locations = turn.roots(**{key: getattr(args, key) for key in turn.ROOT_FIELDS})
        result = submit(body, locations)
    except (turn.TurnError, turn.answer.CampaignAnswerError, turn.runtime.CampaignRuntimeError,
            turn.episode_terminal.CampaignTerminalCaptureError, episodes.StudyEpisodeCaptureError,
            captures.CaptureError, MessageSourceError, OSError, ValueError) as exc:
        result = {'status': 'blocked', 'reason': str(exc), 'next_action': 'recover_same_request'}
        if 'locations' in locals() and isinstance(body, dict) and body.get('request_id') and body.get('display_receipt_sha256'):
            key = turn.sha(turn.encoded([body['display_receipt_sha256'], body['request_id']]))
            path = Path(locations['private_root']) / '.learning-turns-v1/requests' / key / 'request.json'
            if path.is_file():
                result['learning_request_file'] = str(path)
    print(json.dumps(result, ensure_ascii=False, indent=1))
    return 2 if result.get('status') == 'blocked' else 0


if __name__ == '__main__':
    sys.exit(main())
