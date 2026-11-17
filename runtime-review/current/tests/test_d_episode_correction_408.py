import json
import unittest
from pathlib import Path
from unittest import mock

import test_d_supplement_learning_408 as fixture
import d_episode_correction_408 as correction
import d_supplement_learning_408 as learning
import d_concept_supplement_408 as native
import intake_fact_capture_408 as captures
import conversation_package_408 as packages
import study_transcript_408 as transcripts


class CorrectionTests(unittest.TestCase):
    setUp = fixture.SupplementLearningTests.setUp
    write_transcript = fixture.SupplementLearningTests.write_transcript
    next_display = fixture.SupplementLearningTests.next_display

    def capture_unrelated(self):
        response = 'Independent synthetic response for the other D item.'
        display = {**self.shown, 'request_id': 'DCR-unrelated', 'true_item_id': 'EXAM408-2024-Q02',
                   'source_formal_id': 'DS_2024_002', 'surface_kind': 'canonical_original',
                   'acceptance_kind': 'verified_canonical_source', 'question_assets': [],
                   'source_label': 'Synthetic unrelated canonical source',
                   'learner_surface': {'stem': 'Synthetic unrelated item', 'options': []}}
        display.pop('receipt_sha256')
        display['receipt_sha256'] = native.writer.digest(display)
        receipt = native.receipt_root(self.repo) / 'unrelated-fixture.json'
        receipt.write_bytes(native.writer.encoded(display))
        payload = dict(schema=native.SESSION_SCHEMA, subject='408', session_id='unrelated-fixture',
            study_date=display['review_date'], started_at=display['displayed_at'],
            ended_at=display['displayed_at'], episodes=[dict(request_id=display['request_id'],
                episode_id='unrelated-episode', turns=[dict(role='user', kind='answer', text=response)],
                attachments=[], missing_fields=[], frozen_feedback='', observed_outcome=dict(
                    first_response=response, first_result='correct', final_response=response,
                    terminal_state='correct', prompt_dependency='none', reasoning_result='sound'))])
        committed = native.commit(self.repo, payload, [receipt], followup=False)
        return committed['episode_receipts'][0]['capture_id']

    def prepare(self):
        with mock.patch.object(native, 'display', side_effect=self.next_display):
            self.saved = learning.submit(self.body, self.repo)
        self.rows += [('assistant', 'next', 'Next question, do not copy into previous episode.'),
                      ('user', 'correction', '上一道题请按独立完成保存。'),
                      ('assistant', 'feedback', '更正完成方式，保留原推理与纠正。')]
        self.write_transcript()
        self.input = dict(learning_request_file=self.saved['learning_request_file'],
            expected_user_text=self.rows[-2][2], terminal_state='correct', prompt_dependency='none',
            reason='用户明确指出追问是补充理由而非提示。保留原始推理及其措辞纠正。')
        self.private = native.episode_capture._default_package_root(self.repo)
        self.original_files = {str(p): p.read_bytes() for p in self.private.rglob('*') if p.is_file()}
        self.state_root = captures.capture_root(self.repo)
        self.original_events = (self.state_root / 'events.jsonl').read_bytes()

    def check_result(self, result):
        self.assertTrue(result['capture_verified'] and result['package_verified'])
        self.assertFalse(result['frontier_advanced'])
        for count in ('navigation_write_count', 'formal_write_count', 'fsrs_write_count',
                      'personal_summary_write_count'):
            self.assertEqual(0, result[count])
        self.assertEqual(self.saved['capture_id'], result['capture_id'])
        self.assertEqual('correct', result['observed_outcome']['terminal_state'])
        self.assertEqual('none', result['observed_outcome']['prompt_dependency'])
        for key in ('first_response', 'final_response', 'first_result', 'reasoning_result'):
            self.assertEqual(self.body['observed_outcome'][key], result['observed_outcome'][key])
        opened = packages.verify_package_binding(result['conversation_package_locator'],
            require_sealed=True, private_root=self.private)
        self.assertEqual('complete', transcripts.verify_package(opened)['coverage'])
        text = '\n'.join(t['text'] for t in opened['conversation']['turns'])
        self.assertIn(self.input['expected_user_text'], text)
        self.assertNotIn('Next question,', text)
        for path, data in self.original_files.items():
            self.assertEqual(data, Path(path).read_bytes(), path)
        self.assertTrue((self.state_root / 'events.jsonl').read_bytes().startswith(self.original_events))
        state = captures.replay(captures._load_events(self.state_root))
        current = state['captures'][self.saved['capture_id']]
        self.assertEqual('awaiting_web_enhancement', current['quality_status'])
        self.assertEqual(result['conversation_package_locator'], current['supplement_history'][-1]['locator'])
        self.assertEqual(1, len(current['supplement_history']))

    def test_correction_preserves_capture_originals_and_cursor_and_replays(self):
        other = self.capture_unrelated()
        self.prepare()
        ledger = self.state_root / 'events.jsonl'
        before = captures.replay(captures._load_events(self.state_root))['captures']
        other_ref, = before[other]['capture']['stable_evidence_refs']
        other_package = packages.verify_package_binding(other_ref['locator'],
            expected_canonical_sha256=other_ref['sha256'], require_sealed=True, private_root=self.private)
        operation = native.episode_capture._operation_root(self.repo)
        cursor = {p: p.read_bytes() for p in (operation / 'd-supplement-progress').glob('*.json')}
        self.assertTrue(cursor)
        result = correction.correct_outcome(self.input, self.repo)
        self.check_result(result)
        after = captures.replay(captures._load_events(self.state_root))['captures']
        self.assertEqual(set(before), set(after))
        self.assertEqual(before[other], after[other])
        self.assertEqual(other_package, packages.verify_package_binding(other_ref['locator'],
            expected_canonical_sha256=other_ref['sha256'], require_sealed=True, private_root=self.private))
        self.assertEqual(cursor, {p: p.read_bytes() for p in (operation / 'd-supplement-progress').glob('*.json')})
        data = ledger.read_bytes()
        supplement, = [json.loads(line) for line in data[len(self.original_events):].splitlines()]
        self.assertEqual('capture_supplemented', supplement['event_type'])
        self.assertEqual(self.saved['capture_id'], supplement['capture_id'])
        with mock.patch.object(correction, '_current_evidence', side_effect=AssertionError('re-read')):
            repeated = correction.correct_outcome(self.input, self.repo)
        self.assertEqual(result, repeated)
        self.assertEqual(data, ledger.read_bytes())

    def test_after_package_failure_resumes_without_refreezing_evidence(self):
        self.prepare()
        def fail(stage):
            if stage == 'after_package':
                raise RuntimeError('injected')
        with self.assertRaisesRegex(RuntimeError, 'injected'):
            correction.correct_outcome(self.input, self.repo, fault_injector=fail)
        self.assertEqual(self.original_events, (self.state_root / 'events.jsonl').read_bytes())
        with mock.patch.object(correction, '_current_evidence', side_effect=AssertionError('re-read')):
            result = correction.correct_outcome(self.input, self.repo)
        self.check_result(result)

    def test_new_message_race_and_invalid_labels_do_not_change_capture(self):
        self.prepare()
        with self.assertRaisesRegex(ValueError, 'current_user_changed'):
            correction.correct_outcome({**self.input, 'expected_user_text': 'wrong'}, self.repo)
        with self.assertRaisesRegex(ValueError, 'labels_invalid'):
            correction.correct_outcome({**self.input, 'prompt_dependency': 'full'}, self.repo)
        self.assertEqual(self.original_events, (self.state_root / 'events.jsonl').read_bytes())

    def consume_capture(self):
        capture_id = self.saved['capture_id']
        state = captures.replay(captures._load_events(self.state_root))
        day = state['captures'][capture_id]['study_date']
        batch = captures.start_batch(day, 'fixture-formal-start', capture_ids=[capture_id], repo_root=self.repo)
        captures.mark_result(batch_id=batch['batch_id'], capture_id=capture_id,
            outcome='already_current', formal_id='DS_2024_001', verification_sha256='d' * 64,
            idempotency_key='fixture-formal-result', repo_root=self.repo)
        # Per-Capture formal consumption already happened even if global QA failed.
        captures.close_batch(batch_id=batch['batch_id'], global_closeout='failed',
            idempotency_key='fixture-formal-close', repo_root=self.repo)
        return (self.state_root / 'events.jsonl').read_bytes()

    def test_capture_consumed_after_freeze_cannot_be_reopened_by_correction(self):
        self.prepare()
        def interrupt(stage):
            if stage == 'after_package':
                raise RuntimeError('injected')
        with self.assertRaisesRegex(RuntimeError, 'injected'):
            correction.correct_outcome(self.input, self.repo, fault_injector=interrupt)
        consumed = self.consume_capture()
        with self.assertRaisesRegex(captures.CaptureError, 'D_correction_requires_unconsumed_capture'):
            correction.correct_outcome(self.input, self.repo)
        self.assertEqual(consumed, (self.state_root / 'events.jsonl').read_bytes())
        state = captures.replay(captures._load_events(self.state_root))
        capture = state['captures'][self.saved['capture_id']]
        self.assertEqual('already_current', capture['quality_status'])
        self.assertFalse(capture.get('supplement_history'))

    def test_durable_correction_retry_after_consumption_does_not_append(self):
        self.prepare()
        def interrupt(stage):
            if stage == 'after_supplement_event':
                raise RuntimeError('injected')
        with self.assertRaisesRegex(RuntimeError, 'injected'):
            correction.correct_outcome(self.input, self.repo, fault_injector=interrupt)
        consumed = self.consume_capture()
        result = correction.correct_outcome(self.input, self.repo)
        self.assertTrue(result['capture_verified'])
        self.assertEqual(consumed, (self.state_root / 'events.jsonl').read_bytes())
        state = captures.replay(captures._load_events(self.state_root))
        capture = state['captures'][self.saved['capture_id']]
        self.assertEqual('already_current', capture['quality_status'])
        self.assertEqual(1, len(capture['supplement_history']))


if __name__ == '__main__':
    unittest.main()
