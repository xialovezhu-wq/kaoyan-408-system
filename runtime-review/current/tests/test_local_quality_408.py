import json
import sys
import tempfile
import unittest
from unittest.mock import patch
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
import local_quality_408 as quality


class LocalQualityTests(unittest.TestCase):
    def test_profile_patch_accepts_claims_and_verifies_their_turn_sources(self):
        with tempfile.TemporaryDirectory() as temp:
            repo = Path(temp)
            point = 'OS02-05'
            old = dict(concept_key=point, overview='原概况', short_summary='原摘要',
                       next_check='检查条件', timeline=[], progress=[], subpoints=[],
                       source_refs=[], question_refs=[])
            original = repo / quality.profiles.ROOT_REL / 'objects' / ('a' * 64 + '.json')
            original.parent.mkdir(parents=True)
            original.write_bytes(quality.profiles.encoded(old))
            source = repo / 'conversation.json'
            turn = dict(ordinal=1, role='user', kind='utterance', text='状态题先画状态图。')
            source.write_text(json.dumps(dict(schema_version='cs408-full-conversation-v1',
                complete_order_preserved=True, truncated=False, turn_count=1, turns=[turn]), ensure_ascii=False))
            source_sha = quality.digest(source.read_bytes())
            claim = dict(claim_id=quality.profiles.claim_key(point, 'state-chart'), anchor='state-chart',
                scope=point, kind='preference', basis='explicit_statement', evidence_status='user_reported',
                lifecycle='current', content=turn['text'], source_refs=[dict(id='u', path=str(source),
                    sha256=source_sha, review_state='reviewed', turns=[dict(ordinal=1, role='user', quote=turn['text'])])])
            draft = dict(point=point, expected_previous_version='a' * 64,
                review_reason='绑定用户对状态题的明确偏好',
                source_refs=[dict(path=str(source), sha256=source_sha, locator='turn:1')],
                changes=dict(claims_schema=quality.profiles.CLAIMS_SCHEMA, claims=[claim]))
            with patch.object(quality.profiles, 'load_profile_version', return_value=old):
                updated, kwargs = quality.profile_patch(repo, draft, False, prepare_only=True)
            quality.profiles.validate_claims(point, updated, kwargs['evidence_bindings'], old)
            self.assertEqual(source_sha, kwargs['evidence_bindings'][str(source)])
            self.assertEqual(claim['claim_id'], updated['claims'][0]['claim_id'])

    def test_preserve_distinct_exam_tasks_inside_one_format_and_withhold_family(self):
        with tempfile.TemporaryDirectory() as temp:
            repo=Path(temp).resolve()
            root=repo / quality.ROOT
            root.mkdir(parents=True)
            refs={}
            units=[]
            for identity,topic in [('EXAM408-2012-Q01','栈顶'),('EXAM408-2014-Q01','峰值'),
                                   ('EXAM408-2015-Q01-P01','输出顺序'),('EXAM408-2015-Q01-P02','步骤数')]:
                quality.put(root,refs,dict(id=identity,kind='exam_unit',assessed=['DS03-06'],actual_task=topic,given=[topic],
                    first_action='读'+topic,initial_state='空',checks=[],question_card='fixture'))
                units.append({'unit':identity,'focus':[topic],'type':'同一题型','role':'主考',
                              'use':'verified_form','assessed_syllabus_in_point_scope':['S27-DS']})
            quality.put(root,refs,dict(id='DS03-06',kind='point',title='栈',syllabus={'scope_status':'explicit'},
                                       exam_forms={'同一题型':units}))
            (root/'current.json').write_text(json.dumps({'objects':refs,'schema':'test'}))
            result=quality.teaching_lookup(repo,'DS03-06','direct',current_question='EXAM408-2015-Q01-P02')
            self.assertEqual(len(result['exam_forms']['同一题型']),1)
            self.assertEqual([r['focus'] for r in result['exam_focuses_by_form']['同一题型']],['栈顶','峰值'])
            self.assertNotIn('EXAM408-2015-Q01',str(result['exam_focuses_by_form']))
            protected=quality.teaching_lookup(repo,'DS03-06','protected')
            self.assertEqual(protected,{'status':'protected'})
            self.assertNotIn('exam_forms',quality.teaching_lookup(repo,'DS03-06','after_attempt'))

    def test_rebuild_after_native_refresh_preserves_registered_exclusions(self):
        index={'excluded_years':[2012,2013]}
        with patch.object(quality,'catalog',side_effect=[(index,'old'),(index,'new')]), \
                patch.object(quality,'catalog_drift',side_effect=[['scope','mapping'],[]]), \
                patch.object(quality,'compile_catalog') as build:
            result=quality.refresh_registered_course(Path('/fixture'))
        build.assert_called_once_with(Path('/fixture'),{2012,2013})
        self.assertEqual(result['source_check'],'current')
        self.assertEqual(result['catalog_sha256'],'new')
        with patch.object(quality,'catalog',return_value=(index,'new')), \
                patch.object(quality,'catalog_drift',return_value=[]), \
                patch.object(quality,'compile_catalog') as build:
            self.assertEqual(quality.refresh_registered_course(Path('/fixture'))['status'],'already_current')
            build.assert_not_called()

    def test_unregistered_course_is_never_created_by_native_refresh(self):
        with tempfile.TemporaryDirectory() as tmp:
            self.assertIsNone(quality.refresh_registered_course(Path(tmp)))
            self.assertFalse((Path(tmp)/quality.ROOT).exists())

    def test_compact_course_pointer_rehashes_only_changed_sources(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp)
            first, second = repo / 'one.txt', repo / 'two.txt'
            first.write_text('accepted one')
            second.write_text('accepted two')
            question = 'EXAM408-2020-Q41-P01'
            with patch.object(quality, 'source_files', return_value={'one': first, 'two': second}):
                index = {'schema': 'test', 'source_digests': quality.sources(repo),
                         'source_stats': quality.source_stats(repo), 'objects': {question: {}},
                         'exam_lesson_objects': {question: {}}}
                query = {'stage': 'post_answer', 'current_question_id': question}
                with patch.object(quality, 'catalog', return_value=(index, 'bound')), \
                        patch.object(quality, 'file_digest', wraps=quality.file_digest) as reads:
                    pointer = quality.attach_quality_context(repo, query, {'status': 'available'},
                                                             'after_attempt', compact=True)
                    self.assertEqual(question, pointer['local_syllabus_lessons']['exact_lesson_identity'])
                    reads.assert_not_called()
                    first.write_text('changed one')
                    result = quality.attach_quality_context(repo, query, {'status': 'unchanged'},
                                                            'after_attempt', compact=True)
                    self.assertEqual(['one'], result['local_syllabus_lessons']['changed_sources'])
                    self.assertEqual([str(first)], [call.args[0] for call in reads.call_args_list])

    def test_hold_formal_identity_separately_from_true_year(self):
        self.assertTrue(quality.blocked('EXAM408-2013-Q47-P02',{2013}))
        self.assertTrue(quality.blocked('DS_2013_001',{2013}))
        self.assertFalse(quality.blocked('EXAM408-2023-Q37',{2013}))


if __name__=='__main__':
    unittest.main()
