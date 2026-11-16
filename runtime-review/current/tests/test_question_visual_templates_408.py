"""Exact identity and delivery regressions for precomputed question visuals."""
import copy
import hashlib
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'scripts'))
sys.path.insert(0, str(ROOT / 'tests'))
import question_visual_templates_408 as visual
import exam_tutoring_context_408 as native
import study_personalization_408 as snapshot
import knowledge_hot_routes_408 as routes
import test_exam_tutoring_context_408 as fixtures


class QuestionVisualTests(unittest.TestCase):
    def setUp(self):
        # Reuse only the fixture builder, not its old test methods.
        self.fixture = fixtures.ExamTutoringContext408Tests()
        self.fixture.setUp()
        self.addCleanup(self.fixture.tearDown)
        self.root, self.state = self.fixture.repo, self.fixture.state
        self.parent = 'EXAM408-2020-Q45'
        self.child = self.parent + '-P01'
        self.chapter = 'CHAPTER-CO-01-Q99'
        self.part = self.chapter + '#P01'
        self.source = self.root / 'reviewed-source.txt'
        self.source.write_text('已核实的题设与解析。', encoding='utf-8')
        self.values = {
            self.parent: self.template(self.parent, 'parent_exam'),
            self.child: self.template(self.child, 'subquestion', self.parent),
            self.chapter: self.template(self.chapter, 'chapter_question'),
            self.part: self.template(self.part, 'teaching_part', self.chapter),
            'OS_2020_001': self.template('OS_2020_001', 'formal_question'),
        }
        self.missing = self.parent + '-P02'
        self.catalog = self.root / 'catalog.json'
        self.payload = dict(schema=visual.INDEX_SCHEMA, expected_ids=list(self.values) + [self.missing],
            aliases={'EXACT-SOURCE': self.child, 'AMBIGUOUS': [self.child, self.parent]}, templates=self.values)
        self.write_catalog()
        visual.import_catalog(self.root, self.catalog)
        self.fixture.refresh()

    def template(self, identity, kind, parent=None):
        return dict(schema=visual.SCHEMA, question_identity=identity, identity_kind=kind,
            parent_identity=parent, subject='OS', knowledge_points=['OS03-17'],
            given_objects=['页号字段', '页框号字段'], initial_state='空白地址布局', target='区分地址转换字段',
            visual_kind='state_steps', scene='两个分离地址框：' + identity,
            steps=['标注页内偏移不变', '高亮本题的页表查找'], solution_anchor='QUESTION_SECRET_' + identity,
            checks=['核对字段位数'], adaptation_rules=['只调整当前断点，不推定已掌握'],
            source_review_status='reviewed', effectiveness='unverified',
            source_part_label='（1）' if kind == 'teaching_part' else '',
            source_refs=[{'path': str(self.source), 'locator': '第1题'}])

    def write_catalog(self):
        self.catalog.write_text(json.dumps(self.payload, ensure_ascii=False), encoding='utf-8')

    def query(self, *, view='after_attempt', known=None, **options):
        payload = self.fixture.query_payload(stage='pre_answer' if view == 'protected' else 'post_answer', **options)
        return native.query_context(self.root, payload, output_root=self.state,
            teaching_view=view, known_version=known)

    def export_fixture(self):
        # Exercise the real immutable exporter with the fixture's native state.
        target = self.root / native.DEFAULT_STATE_REL
        import shutil
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copytree(self.state, target, dirs_exist_ok=True)
        output = self.root.resolve() / 'portable'
        return snapshot.export_snapshot(self.root, output)['manifest_path']

    def test_exact_parent_child_alias_and_id_only_context(self):
        for identity in (self.parent, self.child, self.chapter, 'OS_2020_001'):
            result = self.query(current_question_id=identity)
            self.assertEqual(identity, result['question_visual_template']['question_identity'])
            self.assertEqual('reviewed', result['question_visual_template']['status'])
            self.assertEqual(['OS03-17'], result['requested_points'])
            self.assertEqual('OS03-17', result['records'][0]['knowledge_point'])
        result = self.query(source_id='EXACT-SOURCE')
        self.assertEqual(self.child, result['question_visual_template']['question_identity'])
        self.assertEqual('unavailable', self.query(source_id='AMBIGUOUS')['question_visual_template']['status'])

    def test_portable_return_receives_native_predecessor_versions(self):
        import source_visual_408 as source_visual
        root = self.root / source_visual.MANIFEST_REL.parent
        plan = {'schema': source_visual.PLAN_SCHEMA, 'identity': self.parent,
                'assets': [], 'presentation_policy': 'teacher source only'}
        plan_ref = source_visual._object(root, plan)
        source_visual._atomic(self.root / source_visual.MANIFEST_REL, source_visual._raw({
            'schema': source_visual.INDEX_SCHEMA, 'point_plans': {},
            'question_plans': {self.parent: plan_ref}}))
        source_visual.clear_cache()
        objects = {}
        def put(kind, value):
            raw = native._canonical_bytes(value)
            digest = hashlib.sha256(raw).hexdigest()
            path = 'objects/' + kind + '/' + digest + '.json'
            objects[path] = value
            return {'ref': path, 'sha256': digest}
        result = visual.materialize(self.root, put)
        entry = result['entries'][self.parent] if 'entries' in result else result[self.parent]
        current = objects[entry['ref']]
        manifest = json.loads((self.root / visual.MANIFEST_REL).read_bytes())
        self.assertEqual(manifest['templates'][self.parent]['sha256'], current['template_native_version'])
        self.assertEqual(plan_ref['sha256'], current['source_visual_plan_version'])
        protected = visual.read_template({'question_visual_identity': self.parent}, 'protected',
            lambda *args, **kwargs: self.fail('protected must not load teacher versions'))
        self.assertEqual({'status': 'protected'}, protected)

    def test_missing_identity_and_variant_never_use_prototype(self):
        result = self.query(current_question_id=self.missing, formal_id='OS_2020_001')
        self.assertEqual('missing', result['question_visual_template']['status'])
        self.assertEqual(self.missing, result['question_visual_template']['question_identity'])
        for field in ('exam_question_id', 'current_question_id'):
            result = self.query(formal_id='OS_2020_001', **{field: None})
            self.assertEqual('current_question_identity_explicitly_unknown', result['question_visual_template']['reason'])
            self.assertNotIn('solution_anchor', result['question_visual_template'])

    def test_protected_does_not_read_or_return_question_object(self):
        result = self.query(current_question_id=self.child, view='protected', read_stats=True)
        self.assertEqual({'status': 'protected'}, result['question_visual_template'])
        self.assertNotIn('QUESTION_SECRET_', json.dumps(result))
        self.assertFalse(any('question-visual-templates' in p or 'question-visual-evidence' in p
            for p in result['read_stats']['artifacts']))

    def test_part_only_exact_and_parent_directory(self):
        exam_parts = self.query(current_question_id=self.parent)['question_visual_template']['parts']
        self.assertEqual([{'question_identity': self.child, 'identity_kind': 'subquestion',
                           'part_id': 'P01'}], exam_parts)
        self.assertNotIn('parts', self.query(current_question_id=self.child)['question_visual_template'])
        protected = self.query(current_question_id=self.parent, view='protected')['question_visual_template']
        self.assertEqual({'status': 'protected'}, protected)
        result = self.query(current_question_id=self.chapter)
        self.assertEqual([self.part], [v['question_identity'] for v in result['question_visual_template']['parts']])
        self.assertEqual('teaching_part', result['question_visual_template']['parts'][0]['identity_kind'])
        self.assertEqual('（1）', result['question_visual_template']['parts'][0]['source_part_label'])
        self.assertNotIn('steps', result['question_visual_template']['parts'][0])
        result = self.query(current_question_id=self.chapter, current_question_part='P01')
        self.assertEqual(self.part, result['question_visual_template']['question_identity'])
        result = self.query(current_question_id=self.chapter, current_question_part='P02')
        self.assertEqual('missing', result['question_visual_template']['status'])
        self.assertNotIn('steps', result['question_visual_template'])

    def test_known_version_checks_selected_original_source_before_unchanged(self):
        first = self.query(current_question_id=self.child)
        again = self.query(current_question_id=self.child, known=first['version'])
        self.assertEqual('unchanged', again['status'])
        self.source.write_text('来源已变化，旧教案不能继续称当前。', encoding='utf-8')
        result = self.query(current_question_id=self.child, known=first['version'])
        self.assertNotEqual(first['version'], result['version'])
        self.assertEqual('stale', result['question_visual_template']['status'])
        self.assertNotIn('solution_anchor', result['question_visual_template'])

    def test_index_activation_invalidates_hot_manifest(self):
        self.values[self.child]['scene'] = '已修订场景'
        self.write_catalog()
        visual.import_catalog(self.root, self.catalog)
        result = self.query(current_question_id=self.child)
        self.assertEqual('unavailable', result['status'])
        self.assertIn('related_source_drift:' + visual.MANIFEST_REL.as_posix(), result['reasons'])

    def test_import_idempotence_scope_and_drift(self):
        current = self.root / visual.MANIFEST_REL
        before = current.stat().st_mtime_ns
        result = visual.import_catalog(self.root, self.catalog)
        self.assertEqual('unchanged', result['status'])
        self.assertEqual(before, current.stat().st_mtime_ns)
        self.payload['templates']['UNREGISTERED'] = self.template('UNREGISTERED', 'formal_question')
        self.write_catalog()
        with self.assertRaisesRegex(ValueError, 'unregistered_identity'):
            visual.import_catalog(self.root, self.catalog)
        self.assertEqual(before, current.stat().st_mtime_ns)

    def test_source_limited_is_readable_but_not_promoted_to_reviewed(self):
        self.values[self.child]['source_review_status'] = 'source_limited'
        self.values[self.child]['review_notes'] = '缺原始题图，依据文字整理。'
        self.write_catalog(); visual.import_catalog(self.root, self.catalog); self.fixture.refresh()
        result = self.query(current_question_id=self.child)['question_visual_template']
        self.assertEqual('source_limited', result['status'])
        self.assertEqual('unverified', result['effectiveness'])
        self.assertIn('缺原始题图', result['review_notes'])

    def test_portable_export_closure_and_default_budget(self):
        path = Path(self.export_fixture())
        result = snapshot.query_snapshot(path, [], [], 'after_attempt', 16384, current_question_id=self.child)
        self.assertEqual('ready', result['status'])
        self.assertTrue(result['complete'])
        self.assertEqual(self.child, result['question_visual_template']['question_identity'])
        self.assertEqual('OS03-17', result['knowledge_summaries'][0]['knowledge_point'])
        ref = result['question_visual_template']['evidence_object']
        raw = (path.parent / ref['ref']).read_bytes()
        self.assertEqual(ref['sha256'], hashlib.sha256(raw).hexdigest())
        protected = snapshot.query_snapshot(path, [], [], 'protected', 16384, current_question_id=self.child)
        self.assertEqual({'status': 'protected'}, protected['question_visual_template'])
        unknown = snapshot.query_snapshot(path, ['OS03-17'], [], 'direct', 16384,
            formal_id='OS_2020_001', current_question_id=None)
        self.assertEqual('unavailable', unknown['question_visual_template']['status'])

    def isolate_child_source(self, *, declared_hash=True):
        source = self.root / 'child-source.txt'
        source.write_text('仅当前子问题设与解析。', encoding='utf-8')
        ref = {'path': str(source), 'locator': '独立小问（1）'}
        if declared_hash:
            ref['sha256'] = hashlib.sha256(source.read_bytes()).hexdigest()
        self.values[self.child]['source_refs'] = [ref]
        self.write_catalog(); visual.import_catalog(self.root, self.catalog); self.fixture.refresh()
        return source

    def test_changed_reviewed_source_refreshes_personal_records_and_withdraws_only_that_template(self):
        source = self.isolate_child_source()
        original_hash = self.values[self.child]['source_refs'][0]['sha256']
        before = self.query(current_question_id=self.child)
        source.write_text('题源修订：需重新审阅这一问。', encoding='utf-8')
        ledger = self.root / native.REVIEW_LEDGER_REL
        event = json.loads(ledger.read_text().splitlines()[0])
        event.update(event_id='source-lifecycle-new-learning', event_time='2026-08-29T12:00:00+08:00',
                     observed_date='2026-08-29', first_break='题源变更之后的真实新断点')
        with ledger.open('a') as stream:
            stream.write(json.dumps(event, ensure_ascii=False) + '\n')
        refreshed = native.refresh_after_commit(self.root, '2026-08-29', output_root=self.state)
        self.assertIn(refreshed['status'], {'refreshed', 'ready'})
        changed = self.query(current_question_id=self.child, known=before['version'])
        self.assertEqual('available', changed['status'])
        self.assertTrue(changed['records'])
        # Exact Pxx cannot carry whole-parent dates, counts or prose, even
        # after a successful refresh. The parent still sees the new facts.
        self.assertEqual('withheld_scope', changed['personal_read_receipt']['records'][0]['status'])
        self.assertNotIn('latest_date', changed['records'][0])
        self.assertNotIn('题源变更之后的真实新断点', json.dumps(changed, ensure_ascii=False))
        parent = self.query(current_question_id=self.parent)
        self.assertEqual('2026-08-29', parent['records'][0]['latest_date'])
        self.assertEqual('reviewed', parent['question_visual_template']['status'])
        self.assertEqual('stale', changed['question_visual_template']['status'])
        self.assertNotIn('scene', changed['question_visual_template'])
        self.assertNotIn('evidence_object', changed['question_visual_template'])
        self.assertNotEqual(before['source_version'], changed['source_version'])
        self.assertEqual('already_current', native.refresh_after_commit(self.root, '2026-08-29', output_root=self.state)['status'])
        # The archived proposal and declared source identity remain untouched.
        loaded = visual.load_catalog(self.root)
        self.assertEqual(original_hash, loaded['templates'][self.child]['source_refs'][0]['sha256'])
        self.assertEqual('stale', loaded['source_reviews'][self.child]['status'])
        self.assertNotEqual(original_hash, loaded['source_reviews'][self.child]['issues'][0]['observed_sha256'])
        path = Path(self.export_fixture())
        exported = snapshot.query_snapshot(path, [], [], 'after_attempt', 16384, current_question_id=self.child)
        self.assertEqual('ready', exported['status'])
        self.assertEqual('stale', exported['question_visual_template']['status'])
        self.assertNotIn('scene', exported['question_visual_template'])
        manifest = json.loads(path.read_text())
        point_index = json.loads((path.parent / manifest['point_index_ref']).read_text())
        entry = point_index['question_visuals'][self.child]
        self.assertEqual('stale', entry['status'])
        self.assertEqual(original_hash, json.loads((path.parent / entry['evidence_object']['ref']).read_text())['source_refs'][0]['sha256'])

    def test_new_import_old_declared_hash_still_rejected_while_unrelated_import_can_continue(self):
        source = self.isolate_child_source()
        source.write_text('已发生题源变化。', encoding='utf-8')
        with self.assertRaisesRegex(ValueError, 'source_hash_drift'):
            visual.import_catalog(self.root, self.catalog)
        self.payload['templates'] = {self.parent: copy.deepcopy(self.values[self.parent])}
        self.payload['templates'][self.parent]['scene'] = '未变题源的新教案'
        self.write_catalog()
        visual.import_catalog(self.root, self.catalog)
        loaded = visual.load_catalog(self.root)
        self.assertEqual('stale', loaded['source_reviews'][self.child]['status'])
        self.assertEqual('current', loaded['source_reviews'][self.parent]['status'])

    def test_undeclared_hash_is_pinned_at_import_not_recertified_at_refresh(self):
        source = self.isolate_child_source(declared_hash=False)
        index = json.loads((self.root / visual.MANIFEST_REL).read_text())
        admitted = index['source_bindings'][self.child][0]['sha256']
        self.assertEqual(hashlib.sha256(source.read_bytes()).hexdigest(), admitted)
        self.assertNotIn('sha256', visual.load_catalog(self.root)['templates'][self.child]['source_refs'][0])
        source.write_text('无显式hash的题源也已变化。', encoding='utf-8')
        native.refresh_after_commit(self.root, '2026-08-29', output_root=self.state)
        self.assertEqual('stale', self.query(current_question_id=self.child)['question_visual_template']['status'])
        self.assertEqual(admitted, json.loads((self.root / visual.MANIFEST_REL).read_text())['source_bindings'][self.child][0]['sha256'])

    def test_missing_source_is_an_observed_gap_and_reappearance_is_rechecked(self):
        source = self.isolate_child_source()
        original = source.read_bytes()
        source.unlink()
        native.refresh_after_commit(self.root, '2026-08-29', output_root=self.state)
        self.assertEqual('stale', self.query(current_question_id=self.child)['question_visual_template']['status'])
        self.assertEqual('already_current', native.refresh_after_commit(self.root, '2026-08-29', output_root=self.state)['status'])
        source.write_bytes(original)
        native.refresh_after_commit(self.root, '2026-08-29', output_root=self.state)
        self.assertEqual('reviewed', self.query(current_question_id=self.child)['question_visual_template']['status'])

    def test_campaign_current_formal_identity_is_forwarded(self):
        import morning_campaign_turn_408 as turn
        request = dict(input={'display_receipt_path': 'unused', 'display_receipt_sha256': 'a' * 64},
            teaching_concepts=['OS03-17'], roots={'repo_root': str(self.root)})
        display = dict(surface_kind='formal_original', formal_node_id='OS_2020_001',
            true_item_id='FORMAL408-OS_2020_001', item_id='current')
        with mock.patch.object(turn.answer, 'reopen_campaign_display_receipt', return_value=(None, display, None)), \
             mock.patch.object(native, 'query_context', return_value={'status': 'available', 'records': []}) as query:
            result = turn.response_personalization(request)
        self.assertEqual('available', result['status'])
        self.assertEqual('OS_2020_001', query.call_args.args[1]['current_question_id'])
        self.assertNotIn('exam_question_id', query.call_args.args[1])

    def test_exact_template_coexists_with_narrow_ksp(self):
        # Test the route independently: actual personal KSP content is handled by
        # the existing prepared-subpoint contract, without broadening it.
        key = 'KSP-' + 'a' * 24
        objects = {}
        def ref(directory, value):
            digest = hashlib.sha256(visual._bytes(value)).hexdigest()
            name = 'objects/' + directory + '/' + digest + '.json'; objects[name] = value
            return dict(ref=name, sha256=digest)
        q = visual.materialize(self.root, ref)
        index = dict(points={}, formal_points={}, aliases={}, subpoints={key:dict(knowledge_point='OS03-17',
            title='页号角色', keywords=['页号'], teaching_views={})}, question_visuals=q['entries'],
            question_visual_aliases=q['aliases'], coverage=q['coverage'],
            question_scopes={self.child: dict(identity=self.child, assessed_points=['OS03-17'], syllabus_ids=[])})
        built = routes.build_routes(dict(profiles={}, by_source_id={}),
            dict(by_knowledge_term={}, by_mechanism_term={}, by_ask_type_term={}), index)
        objects.update(built['objects'])
        normalized = native._validate_query(self.fixture.query_payload(knowledge_points=[key], current_question_id=self.child))
        with mock.patch('exam_year_teaching_408.verified_personal_focus', return_value=(key,)):
            result = routes.resolve(built['descriptor'], normalized, lambda r: objects[r['ref']], include_profiles=False)
        self.assertEqual([key], list(result['subpoint_entries']))
        self.assertEqual([], result['requested_points'])
        self.assertEqual(self.child, result['question_visual_identity'])

    def test_exact_assessment_routes_personal_scope_even_if_visual_tags_disagree(self):
        from lesson_alignment_408 import ASSESSMENT_REL

        scope = 'S27-OS-03-01-03'
        assessed = self.root / ASSESSMENT_REL
        assessed.parent.mkdir(parents=True, exist_ok=True)
        assessed.write_text(json.dumps({
            'schema': 'cs408-question-syllabus-map-v1',
            'exam_items': {self.parent: {
                'main_knowledge': ['OS03-17'], 'hit_knowledge': ['OS03-14', 'OS03-17'],
                'prerequisite_points': ['OS03-19'],
                'assessment_parts': [{'part_id': 'P01', 'focus': '定位当前表项',
                    'canonical_points': ['OS03-14'], 'syllabus_ids': [scope]}],
            }},
            'subquestions': {self.child: {
                'parent_exam_id': self.parent, 'review_status': 'reviewed',
                'primary_point': 'OS03-14', 'assessed_points': ['OS03-14'], 'syllabus_ids': [scope],
            }},
        }))
        self.fixture.refresh()
        parent = self.query(current_question_id=self.parent)
        self.assertEqual(['OS03-17', 'OS03-14'], parent['requested_points'])
        child = self.query(current_question_id=self.child, mechanism_terms=['未知机制'])
        self.assertEqual([scope], child['question_scope']['syllabus_ids'])
        self.assertEqual(['OS03-14'], child['requested_points'])
        self.assertIn('未知机制', child['missing_points'])
        outside = self.query(current_question_id=self.child, knowledge_points=['OS03-17'])
        self.assertEqual([], outside['records'])
        self.assertIn('OS03-17', outside['missing_points'])
        protected = self.query(current_question_id=self.child, view='protected')
        self.assertEqual('available', protected['status'])
        self.assertNotIn('question_scope', protected)
        self.assertEqual([], protected['records'])
        self.assertNotIn('question_exam_introduction', protected)


if __name__ == '__main__':
    unittest.main()
