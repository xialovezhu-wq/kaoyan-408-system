#!/usr/bin/env python3
"""Cold, source-backed supplemental history readers; no formal writes or models.

The records are evidence, not inferred mastery.  Question association is weaker
than an explicit knowledge-code binding.  Unsafe/full-response spans remain in
the original source and are reported as pending semantic review, never copied
into the answer-safe projection.  The immutable-source cache contains only safe
records and is invalidated by file guards and inventory changes.
"""
from __future__ import annotations

import collections
import copy
import hashlib
import json
import os
import re
import tempfile
import zipfile
from pathlib import Path
from typing import Any, Mapping
from xml.etree import ElementTree as ET

from intake_lib_408 import FORMAL_ID_RE, scan_leak

import runtime_paths_408 as _paths

CANONICAL_REPO = _paths.repo_root()
PRIVATE_ROOT = _paths.package_root()
LEGACY_DOCX = _paths.legacy_docx()
SEMANTIC_ROOT = _paths.codex_home() / 'kaoyan-408-knowledge-review'
STATE_REL = Path('wiki/study_vaults/408-full/state/knowledge-history-source-cache-v1.json')
CAPTURE_REL = Path('wiki/study_vaults/408-full/state/intake-curation/events.jsonl')
REVIEW_REL = Path('wiki/study_vaults/408-full/state/review-loop/events.jsonl')
CODE_RE = re.compile(r'\b(?:DS|CO|OS|CN)\d{2}(?:-\d{2})?\b')
ID_RE = re.compile(r'\b(?:DS|CO|OS|CN)_(?:\d{4}|UNK)_\d{3}\b')
DATE_RE = re.compile(r'(?<!\d)(20\d{2}-\d{2}-\d{2})(?!\d)')
OBSERVATION_RE = re.compile(r'我(?:把|以为|认为|理解|觉得|不|没|会|能|知道|忘|混|想|先|算|说|记)|不理解|没理解|不清楚|搞混|混淆|第一(?:步|动作)|当时怎么错|错误入口|错因|薄弱|卡点|模糊')
UNSAFE_RE = re.compile(r'答案|选项|[选填答]\s*[A-DＡ-Ｄ]|正确.{0,8}[A-DＡ-Ｄ]|[A-DＡ-Ｄ][、,，/][A-DＡ-Ｄ]|题干|解得|结果(?:为|是)|[=＝→]|[0-9]+(?:\.[0-9]+)?\s*(?:字节|位|秒|次|个|KB|MB|GB)|[?？]')
VERSION = 9


def _digest(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


def _json_bytes(value: Any) -> bytes:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':')).encode()


def implementation_sha256() -> str:
    """One dependency commitment shared by cache writers and read-only exports."""
    return _digest(Path(__file__).read_bytes() +
                   Path(__file__).with_name('seal_knowledge_observations_408.py').read_bytes())


def _guard(path: Path) -> dict[str, Any]:
    info = path.stat()
    return {'path': str(path.resolve()), 'size': info.st_size,
            'mtime_ns': info.st_mtime_ns, 'ctime_ns': info.st_ctime_ns}


def _safe(text: Any, *, observation: bool = False) -> str | None:
    if not isinstance(text, str):
        return None
    text = text.strip()
    if not text or len(text) > 420 or '\n' in text or '|' in text:
        return None
    # Do not truncate a long original answer into an apparently safe statement.
    inspected = CODE_RE.sub('', ID_RE.sub('', text))
    if UNSAFE_RE.search(inspected) or any(scan_leak(text)):
        return None
    if observation and not OBSERVATION_RE.search(text):
        return None
    return text


def _date(value: Any) -> str:
    match = DATE_RE.search(str(value or ''))
    return match.group(1) if match else '未记录'


def _safe_annotation(text: Any) -> str | None:
    if not isinstance(text, str) or not text.strip() or len(text) > 420 or '\n' in text:
        return None
    # Meta observations about choosing correctly carry no option value.  IPC,
    # CPI and similar identifiers must not be mistaken for answer combinations.
    choice = re.compile(r'(?<![A-Za-z])[A-DＡ-Ｄ]\s*[、,，/]\s*[A-DＡ-Ｄ](?![A-Za-z])|(?:答案|选项|选择|我选|选了|正确选项)\s*(?:是|为|：|:)?\s*[A-DＡ-Ｄ](?![A-Za-z])')
    if choice.search(text) or re.search(r'(?:答案|结果|解得)\s*(?:是|为|：|:)?\s*[0-9]', text):
        return None
    checked = text.replace('正确选项', '选择命中')
    if any(scan_leak(checked)):
        return None
    return text.strip()


def _inventory(repo: Path, production: bool) -> list[Path]:
    from concept_review_408 import session_paths
    paths = [repo / CAPTURE_REL, repo / REVIEW_REL, *session_paths(repo)]
    if not production:
        paths.extend(sorted((repo / '.fixture-formal-knowledge/formal-annotations').glob('*.json')))
        correction_root = repo / '.fixture-formal-knowledge/formal-annotations/corrections'
        paths.extend(sorted(correction_root.glob('*.json')))
        paths.extend(sorted((correction_root / 'heads').glob('*.json')))
        paths.extend(sorted((correction_root / 'versions').glob('*/*.json')))
    for folder in ('历史错题归档', '节点库', '复盘输出', 'wiki/sources/raw_archives'):
        paths.extend(sorted((repo / folder).glob('*.md')))
    if production:
        paths.append(LEGACY_DOCX)
        paths.extend(sorted(PRIVATE_ROOT.glob('????-??-??/CS408-*/receipt.json')))
        paths.extend(sorted(PRIVATE_ROOT.glob('legacy-verified/legacy-archive-pointers/*.json')))
        paths.extend(sorted(PRIVATE_ROOT.glob('legacy-verified/manifests/*.json')))
        paths.extend(sorted((SEMANTIC_ROOT / 'annotations').glob('*.json')))
        paths.extend(sorted((SEMANTIC_ROOT / 'formal-annotations').glob('*.json')))
        paths.extend(sorted((SEMANTIC_ROOT / 'formal-annotations/corrections').glob('*.json')))
        paths.extend(sorted((SEMANTIC_ROOT / 'formal-annotations/corrections/heads').glob('*.json')))
        paths.extend(sorted((SEMANTIC_ROOT / 'formal-annotations/corrections/versions').glob('*/*.json')))
    return [p for p in paths if p.is_file() and not p.is_symlink()]


class _Collector:
    def __init__(self, repo: Path, master: Mapping[str, Any], production: bool, *, export: bool = False,
                 previous: dict[str, Any] | None = None):
        self.repo, self.master, self.production = repo, master, production
        self.records: list[dict[str, Any]] = []
        self.inventory: list[dict[str, Any]] = []
        self.gaps: list[dict[str, Any]] = []
        self.files: dict[str, dict[str, Any]] = {}
        self.seen: set[str] = set()
        self.raw_seen: set[str] = set()
        self.export = export
        self.units: list[dict[str, Any]] = []
        self.unit_inventory: dict[str, dict[str, Any]] = {}
        self.previous = previous or {}
        self.source_dependencies: dict[str, list[str]] = {}
        self.reused_sources: set[str] = set()
        self.package_bindings: dict[str, set[str]] = collections.defaultdict(set)
        self.deferred_capture_ids: set[str] = set()
        self.deferred_package_locators: set[str] = set()
        self.formalized_package_locators: set[str] = set()
        self.source_formal: dict[str, set[str]] = collections.defaultdict(set)
        for fid, row in master.items():
            sid = str(row.get('来源ID') or '')
            if sid and sid not in ('VISUAL_PENDING', '未记录'):
                self.source_formal[sid].add(fid)

    def read(self, path: Path) -> tuple[bytes, str]:
        before = _guard(path)
        raw = path.read_bytes()
        if _guard(path) != before:
            raise ValueError('source_changed_during_read')
        self.files[str(path.resolve())] = {**before, 'sha256': _digest(raw)}
        return raw, _digest(raw)

    def track(self, path: Path, sha: str | None = None) -> None:
        if path.is_file():
            name = str(path.resolve())
            if name in self.files:
                return
            if sha is None:
                self.read(path)
            else:
                self.files[name] = {**_guard(path), 'sha256': sha}

    def unit(self, sid: str, sha: str, kind: str, ids: list[str], date: str,
             content: Any, *, role: str, image_refs: Any = None) -> None:
        metadata = {'source_id': sid, 'source_sha256': sha, 'source_kind': kind,
                    'formal_ids': ids, 'observed_date': _date(date), 'evidence_role': role,
                    'knowledge_points': sorted({code for fid in ids for key in ('主知识点', '副知识点', '命中知识点')
                        for code in CODE_RE.findall(str(self.master[fid].get(key) or ''))})}
        if isinstance(content, dict):
            metadata['original_source_ids'] = sorted({str(source.get('source_id')) for source in
                [content, content.get('source') or {}, content.get('source_facts') or {}]
                if isinstance(source, dict) and source.get('source_id')})
        self.unit_inventory[sid] = metadata
        if self.export:
            self.units.append({**metadata, 'raw_content': content, 'image_refs': image_refs or []})

    def reuse(self, sid: str, *, formal_ids: list[str] | None = None) -> bool:
        if self.export or not self.previous:
            return False
        dependencies = self.previous.get('source_dependencies', {}).get(sid)
        units = {u['source_id']: u for u in self.previous.get('coverage', {}).get('semantic_units', [])}
        unit = units.get(sid)
        old_inventory = [x for x in self.previous.get('coverage', {}).get('raw_source_inventory', []) if x['source_id'] == sid]
        if not dependencies or (not unit and not any(x['kind'] == 'legacy_archive' for x in old_inventory)):
            return False
        old_files = {row['path']: row for row in self.previous.get('source_guards', [])}
        try:
            for name in dependencies:
                old = old_files[name]
                now = _guard(Path(name))
                if any(now[k] != old[k] for k in now):
                    return False
        except (OSError, KeyError):
            return False
        # Knowledge associations can change at a formal commit.  Rebind the
        # cached evidence through currently existing formal IDs; do not rescan
        # immutable original bytes just to update a date or taxonomy label.
        if unit:
            unit = copy.deepcopy(unit)
            unit.pop('semantic_status', None)
            unit['formal_ids'] = sorted({fid for fid in unit['formal_ids'] if fid in self.master} |
                {fid for fid in (formal_ids or []) if fid in self.master} |
                {fid for source_id in unit.get('original_source_ids', []) for fid in self.source_formal.get(source_id, set())})
            unit['knowledge_points'] = sorted({code for fid in unit['formal_ids']
                for key in ('主知识点', '副知识点', '命中知识点')
                for code in CODE_RE.findall(str(self.master[fid].get(key) or ''))})
            self.unit_inventory[sid] = unit
        self.source_dependencies[sid] = list(dependencies)
        for name in dependencies:
            self.files[name] = copy.deepcopy(old_files[name])
        for row in self.previous.get('records', []):
            if row.get('source_locator', row['source_id']) == sid and not row['source_kind'].startswith('semantic_'):
                row = copy.deepcopy(row)
                row['formal_ids'] = unit['formal_ids'] if unit else sorted(
                    {fid for fid in row['formal_ids'] if fid in self.master} |
                    {fid for fid in (formal_ids or []) if fid in self.master})
                if row['point_binding'] == 'question_associated':
                    row['knowledge_points'] = sorted({code for fid in row['formal_ids'] for key in ('主知识点', '副知识点', '命中知识点')
                        for code in CODE_RE.findall(str(self.master[fid].get(key) or ''))})
                if row['knowledge_points']:
                    self.records.append(row)
        self.inventory.extend(copy.deepcopy([x for x in self.previous.get('coverage', {}).get('raw_source_inventory', []) if x['source_id'] == sid]))
        self.gaps.extend(copy.deepcopy([x for x in self.previous.get('coverage', {}).get('gaps', []) if x['source_id'] == sid]))
        self.reused_sources.add(sid)
        return True

    def concept_reviews(self):
        from concept_review_408 import session_paths, reopen, safe_records
        for path in session_paths(self.repo):
            raw, sha = self.read(path)
            value = reopen(path, self.repo)
            if _digest(raw) != _digest(path.read_bytes()):
                raise ValueError('concept_session_changed_during_read')
            records = safe_records(value)
            self.records.extend(records)
            for artifact in value['session']['artifacts']:
                self.read(path.parent / artifact['stored_path'])
            self.inventory.append({'source_id': records[0]['source_id'], 'kind': 'concept_review',
                'raw_read': True, 'source_sha256': value['session_sha256'],
                'record_count': len(records), 'review_marker': 'concept_review'})

    def gap(self, sid: str, reason: str, **extra: Any) -> None:
        self.gaps.append({'source_id': sid, 'reason': reason, **extra})

    def ids(self, *values: Any) -> list[str]:
        found: set[str] = set()
        for value in values:
            text = str(value or '')
            found.update(x for x in ID_RE.findall(text) if x in self.master)
            found.update(self.source_formal.get(text, set()))
        return sorted(found)

    def add(self, text: Any, *, sid: str, sha: str, kind: str, role: str,
            formal_ids: list[str], date: str, fragment: str, observation: bool = False,
            explicit_codes: list[str] | None = None) -> bool:
        safe = _safe(text, observation=observation)
        if safe is None:
            return False
        codes = sorted(set(explicit_codes or CODE_RE.findall(safe)))
        binding = 'explicit_point' if codes else 'question_associated'
        if not codes:
            codes = sorted({code for fid in formal_ids for key in ('主知识点', '副知识点', '命中知识点')
                            for code in CODE_RE.findall(str(self.master[fid].get(key) or ''))})
        if not codes:
            self.gap(sid, 'unmapped_safe_statement', source_fragment=fragment)
            return False
        record = {'knowledge_points': codes, 'source_kind': kind, 'source_id': sid,
                  'source_locator': sid,
                  'source_sha256': sha, 'observed_date': _date(date), 'summary': safe,
                  'evidence_role': role, 'point_binding': binding,
                  'formal_ids': formal_ids, 'source_fragment': fragment}
        ordinal = re.search(r'(?:turn|line):(\d+)', fragment)
        if ordinal:
            record['source_order'] = int(ordinal.group(1))
        key = _digest(_json_bytes(record))
        if key not in self.seen:
            self.records.append(record)
            self.seen.add(key)
        return True

    def capture(self) -> None:
        path = self.repo / CAPTURE_REL
        if not path.is_file():
            self.gap(str(CAPTURE_REL), 'source_absent')
            return
        raw, sha = self.read(path)
        events = [json.loads(line) for line in raw.splitlines() if line.strip()]
        # New quick Captures are staged evidence.  Replaying the native ledger
        # admits them only after a successful formal/concept result; raw package
        # discovery below must not bypass this boundary.  Legacy sources retain
        # their original contract.
        from intake_fact_capture_408 import WEB_ENHANCEMENT_PENDING_POLICY, SUCCESS_OUTCOMES, replay
        web_ids = {str(event['capture_id']) for event in events
                   if event.get('event_type') == 'fact_captured'
                   and event['capture'].get('authorization_policy') == WEB_ENHANCEMENT_PENDING_POLICY}
        if web_ids:
            state = replay(events)
            for cid in web_ids:
                row = state['captures'][cid]
                original_refs = row['capture'].get('stable_evidence_refs') or []
                all_refs = [*original_refs, *(row.get('supplement_history') or [])]
                # A later, still-pending supplement does not revoke an earlier
                # verified formal result or its immutable original evidence.
                successful = [batch['results'][cid] for batch in state['batches'].values()
                              if (batch.get('results', {}).get(cid) or {}).get('outcome') in SUCCESS_OUTCOMES]
                committed_refs = list(original_refs) if successful else []
                for result in successful:
                    committed_refs.extend(result.get('consumed_supplement_evidence') or [])
                self.formalized_package_locators.update(str(ref.get('locator') or '') for ref in committed_refs)
                if row.get('quality_status') not in SUCCESS_OUTCOMES:
                    if not successful:
                        self.deferred_capture_ids.add(cid)
                    self.deferred_package_locators.update(str(ref.get('locator') or '') for ref in all_refs)
            self.deferred_package_locators.difference_update(self.formalized_package_locators)
        count = 0
        for ordinal, line in enumerate(raw.splitlines(), 1):
            if not line.strip():
                continue
            event = json.loads(line)
            if event.get('event_type') != 'fact_captured':
                continue
            count += 1
            capture = event['capture']
            sid = str(event.get('capture_id') or event.get('event_id'))
            if sid in self.deferred_capture_ids:
                self.inventory.append({'source_id': sid, 'kind': 'deferred_capture',
                    'raw_read': False, 'reason': 'awaiting_formal_web_enhancement',
                    'study_date': capture.get('study_date')})
                continue
            facts = capture.get('source_facts') or {}
            ids = self.ids((capture.get('identity_hint') or {}).get('formal_id'), facts.get('source_id'))
            user = capture.get('user_facts') or {}
            self.unit(sid, _digest(line), 'capture_fact', ids, capture.get('study_date'),
                      {'user_facts': user, 'source_facts': facts, 'answer_safe_context_anchor': capture.get('answer_safe_context_anchor'),
                       'missing_fields': capture.get('missing_fields')}, role='structured_learner_evidence')
            for field in ('user_error_entry', 'first_action'):
                provenance = user.get('user_error_provenance' if field == 'user_error_entry' else 'first_action_provenance')
                if user.get(field):
                    role = 'learner_report' if provenance == 'user_report' else 'observed_learner_evidence' if provenance == 'visible_evidence' else 'formal_summary'
                    if not self.add(user[field], sid=sid, sha=_digest(line), kind='capture_fact', role=role,
                                    formal_ids=ids, date=capture.get('study_date'), fragment=f'line:{ordinal}/user_facts/{field}'):
                        self.gap(sid, 'unsafe_or_unmapped_structured_fact', source_fragment=field)
            for ref in capture.get('stable_evidence_refs') or []:
                locator = str(ref.get('locator') or '')
                self.package_bindings[locator].update(ids)
                if locator.startswith('cs408-conversation-package://'):
                    self.canonical(locator, ref.get('sha256'), ids, capture.get('study_date'))
                elif locator.startswith('current-question-evidence://'):
                    self.legacy(locator, ref.get('sha256'), ids, capture.get('study_date'))
        self.inventory.append({'source_id': str(CAPTURE_REL), 'kind': 'capture_ledger', 'sha256': sha,
                               'raw_read': True, 'capture_count': count})

    def turns(self, turns: list[Any], *, sid: str, sha: str, kind: str,
              ids: list[str], date: str) -> tuple[int, int]:
        processed = pending = 0
        for ordinal, turn in enumerate(turns, 1):
            if not isinstance(turn, dict):
                pending += 1
                continue
            role = turn.get('role') or turn.get('speaker')
            text = turn.get('text') or turn.get('content')
            # Assistant explanations are explicitly recorded as such only when
            # they are short observations; never inferred learner attainment.
            if role not in ('user', 'assistant') or not isinstance(text, str):
                pending += 1
                continue
            accepted = 0
            for segment, sentence in enumerate(re.split(r'[\n。；]', text), 1):
                if self.add(sentence, sid=sid, sha=sha, kind=kind,
                            role='learner_report' if role == 'user' else 'assistant_explanation',
                            formal_ids=ids, date=date, fragment=f'turn:{ordinal}/segment:{segment}',
                            observation=True):
                    accepted += 1
            if accepted:
                processed += 1
            # Extraction is deliberately partial: a turn with one usable
            # observation can still contain unprocessed technical understanding.
            if text.strip():
                pending += 1
        return processed, pending

    def canonical(self, locator: str, expected_sha: Any, ids: list[str], date: str) -> None:
        if locator in self.deferred_package_locators:
            return
        if not self.production:
            self.gap(locator, 'external_originals_disabled_for_noncanonical_repo')
            return
        if locator in self.raw_seen:
            return
        self.raw_seen.add(locator)
        if self.reuse(locator, formal_ids=ids):
            return
        import conversation_package_408 as package
        import archive_408_conversation_package as archive
        package_id = locator.removeprefix(package.LOCATOR_PREFIX)
        try:
            try:
                opened = package.verify_package_binding(locator, private_root=PRIVATE_ROOT,
                                                        expected_canonical_sha256=expected_sha or None)
                manifest, conversation, source = opened['manifest'], opened['conversation'], opened['source']
                sha = manifest['canonical_sha256']
                for p in Path(opened['package_dir']).glob('*.json'):
                    self.track(p)
                for row in manifest.get('files') or []:
                    self.track(Path(opened['package_dir']) / row['relative_path'], row.get('sha256'))
                self.source_dependencies[locator] = sorted(name for name in self.files if str(Path(opened['package_dir'])) + '/' in name)
                attachment_count = len(manifest.get('attachments') or [])
            except (package.ConversationPackageError, FileNotFoundError):
                opened = archive.reopen_archived_package(repo=self.repo, package_id=package_id,
                    requested_relative_paths=['conversation.json', 'source.json'])
                objects = {x['relative_path']: x['json'] for x in opened['requested_objects']}
                conversation, source = objects['conversation.json'], objects['source.json']
                sha = opened['package_sha256']
                if expected_sha and sha != expected_sha:
                    # Earlier Captures can bind a prior append-only package
                    # revision.  The verified locator independently identifies
                    # the current complete archived revision; do not claim it
                    # has the older Capture's hash or silently discard it.
                    self.gap(locator, 'capture_reference_is_prior_package_revision',
                             capture_reference_sha256=expected_sha, current_package_sha256=sha)
                note = archive._read_locator_note(self.repo / archive.LOCATOR_NOTE_ROOT / f'{package_id}.md')
                package_dir = archive.ARCHIVE_ROOT / note['raw_archive_relpath']
                for p in package_dir.rglob('*'):
                    if p.is_file():
                        self.track(p)
                note_path = self.repo / archive.LOCATOR_NOTE_ROOT / f'{package_id}.md'
                self.track(note_path)
                self.source_dependencies[locator] = sorted([str(note_path.resolve()), *[name for name in self.files if str(package_dir) + '/' in name]])
                attachment_count = sum(1 for p in (package_dir / 'attachments').glob('*') if p.is_file())
            inner = source.get('source') or {}
            if (inner.get('requires_web_enhancement') is True
                    and locator not in self.formalized_package_locators):
                self.inventory.append({'source_id': locator, 'kind': 'deferred_conversation',
                    'sha256': sha, 'raw_read': False, 'reason': 'awaiting_formal_web_enhancement'})
                return
            ids = sorted(set(ids) | set(self.ids(source.get('source_id'), source.get('source'))))
            self.unit(locator, sha, 'canonical_conversation', ids, source.get('study_date') or date,
                      {'turns': conversation.get('turns') or [], 'source': source}, role='mixed_conversation',
                      image_refs={'attachment_count': attachment_count, 'package_id': package_id})
            processed, pending = self.turns(conversation.get('turns') or [], sid=locator, sha=sha,
                                            kind='canonical_conversation', ids=ids, date=source.get('study_date') or date)
            self.inventory.append({'source_id': locator, 'kind': 'canonical_conversation', 'sha256': sha,
                'raw_read': True, 'turn_count': len(conversation.get('turns') or []),
                'turns_with_safe_observations': processed, 'semantic_review_pending_turns': pending,
                'image_review_pending': attachment_count})
            if pending:
                self.gap(locator, 'full_turn_semantic_review_pending', count=pending)
            if attachment_count:
                self.gap(locator, 'image_evidence_not_interpreted', count=attachment_count)
        except Exception as exc:
            self.inventory.append({'source_id': locator, 'kind': 'canonical_conversation', 'raw_read': False})
            self.gap(locator, 'original_reopen_failed', error=type(exc).__name__)

    def legacy(self, locator: str, expected_sha: Any, ids: list[str], date: str) -> None:
        if not self.production:
            self.gap(locator, 'external_originals_disabled_for_noncanonical_repo')
            return
        if locator in self.raw_seen:
            return
        self.raw_seen.add(locator)
        if self.reuse(locator, formal_ids=ids):
            return
        import sol_curation_decision_408 as decision
        root = PRIVATE_ROOT / 'legacy-verified'
        try:
            try:
                opened = decision.reopen_direct_evidence(locator, expected_sha or locator.rsplit('/', 1)[-1], legacy_root=root)
            except decision.SolCurationDecisionError:
                # Historical v3 predates the terminal_resolved field.  Verify
                # immutable source bytes and bindings without fabricating this
                # later lifecycle field or passing them to a current writer.
                import current_question_evidence_408 as evidence
                digest = locator.rsplit('/', 1)[-1]
                if expected_sha and digest != expected_sha:
                    raise ValueError('legacy_expected_sha_drifted')
                manifest = evidence._read_object(root / 'manifests' / f'{digest}.json', digest,
                                                root=root, maximum=evidence.MAX_METADATA_BYTES)
                object_sha = manifest['object_sha256']
                bundle = evidence._read_object(root / 'objects' / f'{object_sha}.json', object_sha,
                                              root=root, maximum=evidence.MAX_BUNDLE_BYTES)
                if (bundle.get('schema_version') != evidence.BUNDLE_SCHEMA_V3
                    or set(bundle.get('assessment') or {}) != set(evidence.ASSESSMENT_KEYS) - {'terminal_resolved'}
                    or bundle.get('schema_version') != manifest.get('bundle_schema_version')
                    or any(bundle.get(key) != manifest.get(key) for key in ('context_id', 'request_id', 'session_id',
                        'item_id', 'source_id', 'source_kind', 'study_date', 'source_binding_sha256'))
                    or bundle.get('formal_write_count') != 0
                    or bundle.get('attachment_objects') != manifest.get('attachment_objects')):
                    raise ValueError('unsupported_legacy_shape_or_identity_drift')
                opened = {'bundle': bundle, 'manifest': manifest, 'sha256': digest}
            bundle = opened['bundle']
            sha = opened['sha256']
            ids = sorted(set(ids) | set(self.ids(bundle.get('source_id'))))
            date = bundle.get('study_date') or date
            learner = bundle.get('learner_evidence') or {}
            self.unit(locator, sha, 'legacy_original', ids, date,
                      {key: bundle[key] for key in ('source_id', 'learner_evidence', 'interaction_trace', 'frozen_assistant_feedback',
                        'assessment', 'evaluation_evidence', 'missing_fields', 'conversation', 'source_scope') if key in bundle},
                      role='mixed_legacy_evidence', image_refs=bundle.get('attachment_objects') or [])
            count = 0
            for field in ('first_action', 'reasoning', 'user_error_entry'):
                if learner.get(field) and self.add(learner[field], sid=locator, sha=sha, kind='legacy_learner_evidence',
                        role='learner_report', formal_ids=ids, date=date, fragment=f'learner_evidence/{field}', observation=True):
                    count += 1
            trace = bundle.get('interaction_trace') or {}
            turns = trace.get('events') or trace.get('turns') or bundle.get('conversation', {}).get('turns') or []
            processed, pending = self.turns(turns, sid=locator, sha=sha, kind='legacy_conversation', ids=ids, date=date)
            self.inventory.append({'source_id': locator, 'kind': 'legacy_original', 'sha256': sha, 'raw_read': True,
                'safe_observation_fields': count, 'turn_count': len(turns), 'turns_with_safe_observations': processed,
                'semantic_review_pending_turns': pending, 'image_review_pending': len(bundle.get('attachment_objects') or [])})
            self.gap(locator, 'legacy_full_semantic_review_pending', turn_count=len(turns))
            # Include exact local objects and verified archive dependencies.
            for group in ('manifests', 'objects'):
                for p in (root / group).glob('*.json'):
                    self.track(p)
            self.source_dependencies[locator] = sorted(name for name in self.files if str(root) + '/' in name or '/CS408-LEGACY-' in name)
        except Exception as exc:
            self.inventory.append({'source_id': locator, 'kind': 'legacy_original', 'raw_read': False})
            self.gap(locator, 'original_reopen_failed', error=type(exc).__name__)

    def originals(self) -> None:
        if not self.production:
            return
        import archive_408_conversation_package as archive
        for path in sorted(PRIVATE_ROOT.glob('????-??-??/CS408-*/receipt.json')):
            raw, _ = self.read(path)
            receipt = json.loads(raw)
            locator = str(receipt.get('locator') or '')
            if locator.startswith('cs408-conversation-package://'):
                self.canonical(locator, receipt.get('canonical_sha256'), sorted(self.package_bindings[locator]), receipt.get('study_date'))
        for path in sorted((self.repo / archive.LOCATOR_NOTE_ROOT).glob('CS408-*.md')):
            self.read(path)
            if 'LEGACY' not in path.name:
                try:
                    note = archive._read_locator_note(path)
                    self.canonical('cs408-conversation-package://' + note['package_id'], None,
                                   self.ids(note.get('formal_ids')), '')
                except Exception as exc:
                    self.gap(str(path), 'locator_invalid', error=type(exc).__name__)
        root = PRIVATE_ROOT / 'legacy-verified'
        for path in sorted((root / 'legacy-archive-pointers').glob('*.json')):
            raw, _ = self.read(path)
            pointer = json.loads(raw)
            locator = pointer.get('evidence_locator')
            if locator in self.reused_sources:
                # The reused source already carries verified archive dependency
                # guards.  Pointer bytes are in the cold inventory signature.
                continue
            if locator not in self.raw_seen and self.reuse(locator, formal_ids=self.ids(pointer.get('formal_ids'))):
                self.raw_seen.add(locator)
                continue
            try:
                verified = archive.verify_legacy_archive_pointer(repo=self.repo, evidence_locator=locator, evidence_root=root)
                manifest = verified['manifest']
                for row in manifest.get('files', manifest.get('source_files', [])):
                    if isinstance(row, dict) and row.get('relative_path'):
                        self.track(Path(verified['archive_dir']) / row['relative_path'], row.get('sha256'))
                # Archive verification hashes all files; cache watches their
                # guards to avoid a full volume reread on every learner turn.
                for p in Path(verified['archive_dir']).rglob('*'):
                    if p.is_file():
                        self.track(p)
                self.inventory.append({'source_id': locator, 'kind': 'legacy_archive', 'raw_read': True,
                    'sha256': verified['pointer_sha256'], 'conversation_complete': pointer.get('conversation_complete'),
                    'archive_mode': pointer.get('archive_mode')})
                if locator.startswith('current-question-evidence://'):
                    self.legacy(locator, pointer.get('evidence_sha256'), self.ids(pointer.get('formal_ids')), pointer.get('study_date'))
                    self.source_dependencies[locator] = sorted(set(self.source_dependencies.get(locator, [])) |
                        {str(path.resolve())} | {name for name in self.files if str(verified['archive_dir']) + '/' in name})
                else:
                    self.gap(locator, 'ledger_only_no_full_conversation', unresolved_ref_count=len(pointer.get('unresolved_refs') or []))
                    self.source_dependencies[locator] = sorted({str(path.resolve())} |
                        {name for name in self.files if str(verified['archive_dir']) + '/' in name})
            except Exception as exc:
                self.gap(locator or str(path), 'legacy_archive_verification_failed', error=type(exc).__name__)
        for path in sorted((root / 'manifests').glob('*.json')):
            raw, sha = self.read(path)
            manifest = json.loads(raw)
            self.legacy('current-question-evidence://sha256/' + path.stem, sha,
                        self.ids(manifest.get('source_id')), manifest.get('study_date'))

    def summaries(self) -> None:
        for folder, role in (('历史错题归档', 'formal_summary'), ('节点库', 'formal_summary'), ('复盘输出', 'review_summary')):
            for path in sorted((self.repo / folder).glob('*.md')):
                raw, sha = self.read(path)
                self.unit(str(path.relative_to(self.repo)), sha, 'historical_summary', self.ids(raw.decode('utf-8')),
                          _date(path.name), raw.decode('utf-8'), role=role)
                count, pending = 0, 0
                inherited_codes: list[str] = []
                inherited_ids: list[str] = []
                for ordinal, line in enumerate(raw.decode('utf-8').splitlines(), 1):
                    if line.startswith('#'):
                        inherited_codes = CODE_RE.findall(line)
                        inherited_ids = self.ids(line)
                    if not line.strip():
                        continue
                    ids = self.ids(line) or inherited_ids
                    codes = CODE_RE.findall(line) or inherited_codes
                    # Table rows contain multiple semantic columns; inspect
                    # observation cells separately, never join a full question.
                    pieces = line.strip().strip('|').split('|') if line.startswith('|') else [line.lstrip('- >')]
                    accepted = False
                    for cell, piece in enumerate(pieces, 1):
                        if OBSERVATION_RE.search(piece) and self.add(piece.strip(), sid=str(path.relative_to(self.repo)), sha=sha,
                                kind='historical_summary', role=role, formal_ids=ids, date=_date(path.name),
                                fragment=f'line:{ordinal}/cell:{cell}', observation=True, explicit_codes=codes):
                            count += 1
                            accepted = True
                    if not accepted:
                        pending += 1
                self.inventory.append({'source_id': str(path.relative_to(self.repo)), 'kind': 'historical_summary',
                    'sha256': sha, 'raw_read': True, 'safe_observation_count': count,
                    'uninterpreted_nonempty_lines': pending})
                if pending:
                    self.gap(str(path.relative_to(self.repo)), 'historical_summary_semantic_review_pending', count=pending)

    def docx(self) -> None:
        if not self.production:
            return
        if not LEGACY_DOCX.is_file():
            self.gap(str(LEGACY_DOCX), 'legacy_source_document_missing')
            return
        previous_units = [u for u in self.previous.get('coverage', {}).get('semantic_units', [])
                          if u['source_kind'] == 'legacy_docx_scope']
        if previous_units and not self.export:
            old = next((g for g in self.previous.get('source_guards', []) if g['path'] == str(LEGACY_DOCX)), None)
            if old and all(_guard(LEGACY_DOCX)[k] == old[k] for k in ('path', 'size', 'mtime_ns', 'ctime_ns')):
                if all(self.reuse(unit['source_id']) for unit in previous_units):
                    self.inventory.extend(copy.deepcopy([x for x in self.previous['coverage']['raw_source_inventory'] if x['source_id'] == str(LEGACY_DOCX)]))
                    self.gaps.extend(copy.deepcopy([x for x in self.previous['coverage']['gaps'] if x['source_id'] == str(LEGACY_DOCX)]))
                    return
        # Read OOXML text directly, without loading 376 MB of embedded images.
        before = _guard(LEGACY_DOCX)
        with zipfile.ZipFile(LEGACY_DOCX) as document:
            raw = document.read('word/document.xml')
            images = len([x for x in document.namelist() if x.startswith('word/media/')])
        if before != _guard(LEGACY_DOCX):
            raise ValueError('legacy_docx_changed_during_read')
        self.track(LEGACY_DOCX)
        xml = ET.fromstring(raw)
        ns = '{http://schemas.openxmlformats.org/wordprocessingml/2006/main}'
        body = xml.find(ns + 'body')
        paragraphs = [''.join(t.text or '' for t in p.iter(ns + 't')) for p in body.findall(ns + 'p')]
        sha = _digest(raw)
        # Existing historical paragraph locators provide identity, not a new
        # inferred diagnosis.  Read exact referenced paragraphs only for safe
        # observations; the full XML is still inventoried to EOF.
        mapping: dict[int, list[str]] = {}
        index = self.repo / '历史错题归档/定位索引.md'
        if index.is_file():
            text, _ = self.read(index)
            for line in text.decode().splitlines():
                ids = self.ids(line)
                for sid in re.findall(r'H(?:DS|CO|OS|CN)_\d+', line):
                    ids = sorted(set(ids) | self.source_formal.get(sid, set()))
                for ordinal in re.findall(r'段落\s*(\d+)', line):
                    mapping[int(ordinal)] = ids
        count = 0
        for ordinal, text in enumerate(paragraphs, 1):
            ids = mapping.get(ordinal) or self.ids(text)
            if self.add(text, sid=str(LEGACY_DOCX), sha=sha, kind='legacy_docx_summary', role='historical_source_summary',
                        formal_ids=ids, date='未记录', fragment=f'word/document.xml#paragraph:{ordinal}', observation=True):
                count += 1
        starts = sorted(set([1, *[n for n in mapping if 1 <= n <= len(paragraphs)]]))
        for position, start in enumerate(starts):
            stop = starts[position + 1] - 1 if position + 1 < len(starts) else len(paragraphs)
            content = [{'paragraph': n, 'text': paragraphs[n - 1]} for n in range(start, stop + 1)]
            unit_sha = _digest(_json_bytes(content))
            sid = str(LEGACY_DOCX) + f'#paragraphs:{start}-{stop}'
            self.unit(sid, unit_sha, 'legacy_docx_scope', mapping.get(start, []), '未记录', content,
                      role='historical_source_summary', image_refs={'document_path': str(LEGACY_DOCX),
                        'document_xml_sha256': sha, 'text_only': True, 'images_not_interpreted': True})
            self.source_dependencies[sid] = [str(LEGACY_DOCX)] + ([str(index.resolve())] if index.is_file() else [])
        self.inventory.append({'source_id': str(LEGACY_DOCX), 'kind': 'legacy_docx', 'sha256': sha,
            'hash_scope': 'word/document.xml', 'raw_read': True, 'paragraph_count': len(paragraphs),
            'safe_observation_count': count, 'image_review_pending': images})
        self.gap(str(LEGACY_DOCX), 'legacy_docx_full_semantic_review_pending', paragraph_count=len(paragraphs), image_count=images)

    def annotations(self) -> None:
        reviewed: set[str] = set()
        annotations: list[dict[str, Any]] = []
        if self.production:
            for path in sorted((SEMANTIC_ROOT / 'annotations').glob('*.json')):
                raw, _ = self.read(path)
                envelope = json.loads(raw)
                if envelope.get('schema') != 'knowledge_history_semantic_annotations_v1':
                    raise ValueError('semantic_annotation_schema_invalid')
                for unit in envelope.get('units') or []:
                    sid = unit.get('source_id')
                    original = self.unit_inventory.get(sid)
                    if original is None or original['source_sha256'] != unit.get('source_sha256'):
                        self.gap(str(sid), 'semantic_annotation_source_stale')
                        continue
                    if unit.get('status') != 'reviewed':
                        continue
                    for index, annotation in enumerate(unit.get('records') or [], 1):
                        summary = _safe_annotation(annotation.get('summary'))
                        codes = annotation.get('knowledge_points') or []
                        ids = annotation['formal_ids'] if 'formal_ids' in annotation else original['formal_ids']
                        role = annotation.get('evidence_role')
                        binding = annotation.get('point_binding')
                        if (summary is None or not codes or any(not CODE_RE.fullmatch(x) for x in codes)
                            or any(x not in self.master for x in ids)
                            or role not in ('learner_report', 'observed_learner_evidence', 'assistant_explanation',
                                            'formal_summary', 'review_summary', 'historical_source_summary')
                            or binding not in ('explicit_point', 'question_associated')):
                            raise ValueError('semantic_annotation_record_invalid')
                        if original['source_kind'] == 'review_identity' and role != 'formal_summary':
                            raise ValueError('review_identity_annotation_must_be_mapping_only')
                        fragment = str(annotation.get('source_fragment') or f'semantic:{index}')
                        record = {'knowledge_points': sorted(set(codes)), 'source_kind': 'semantic_' + original['source_kind'],
                            'source_id': str(sid), 'source_locator': sid,
                            'source_sha256': original['source_sha256'], 'observed_date': _date(annotation.get('observed_date') or original['observed_date']),
                            'summary': summary, 'evidence_role': role, 'point_binding': binding,
                            'formal_ids': sorted(set(ids)), 'source_fragment': fragment,
                            'semantic_annotation_sha256': _digest(raw)}
                        if isinstance(annotation.get('source_order'), int):
                            record['source_order'] = annotation['source_order']
                        for field in ('prompt_dependency', 'event_kind', 'outcome', 'correction_result', 'event_time'):
                            value = annotation.get(field)
                            if isinstance(value, (str, bool, int, float)):
                                record[field] = value
                        if original['source_kind'] == 'review_identity':
                            record['review_source_id'] = original['review_source_id']
                            record['anchor_event_id'] = original['anchor_event_id']
                        annotations.append(record)
                    reviewed.add(sid)
        # Once a source is natively reviewed, its semantic annotation supersedes
        # heuristic snippets from that same source; it is not a second attempt.
        self.records = [record for record in self.records if record.get('source_locator', record['source_id']) not in reviewed]
        self.records.extend(annotations)
        semantic_reasons = {'full_turn_semantic_review_pending', 'legacy_full_semantic_review_pending',
                            'historical_summary_semantic_review_pending', 'unsafe_or_unmapped_structured_fact',
                            'unmapped_safe_statement'}
        self.gaps = [gap for gap in self.gaps if not (gap['source_id'] in reviewed and gap['reason'] in semantic_reasons)]
        docx_units = {sid for sid, unit in self.unit_inventory.items() if unit['source_kind'] == 'legacy_docx_scope'}
        if docx_units and docx_units <= reviewed:
            self.gaps = [gap for gap in self.gaps if not (
                gap['reason'] == 'legacy_docx_full_semantic_review_pending'
                or (gap['source_id'] == str(LEGACY_DOCX) and gap['reason'] == 'unmapped_safe_statement'))]
        for sid, unit in self.unit_inventory.items():
            unit['semantic_status'] = 'reviewed' if sid in reviewed else 'pending'

    def review_identities(self) -> None:
        """Anchor uncoded review source identities without inventing diagnoses.

        A first raw outcome line is a stable identity even when later outcomes,
        repairs or date corrections append to the ledger.  Native annotations
        bind that identity to points; the review ledger remains the only source
        of actual attempts and their outcomes.
        """
        path = self.repo / REVIEW_REL
        if not path.is_file():
            return
        raw, ledger_sha = self.read(path)
        # A later canonical formal binding can resolve an old uncoded outcome.
        # Such an identity is already handled by the core review associations.
        bound_sources: set[str] = set()
        for line in raw.splitlines():
            if line.strip():
                bound = json.loads(line)
                if (isinstance(bound, dict) and bound.get('event_kind') == 'formal_review_binding_v1'
                        and bound.get('formal_node_id') in self.master and isinstance(bound.get('source_id'), str)):
                    bound_sources.add(bound['source_id'])
        seen_events: set[str] = set()
        seen_sources: set[str] = set()
        count = 0
        for ordinal, line in enumerate(raw.splitlines(keepends=True), 1):
            if not line.strip():
                continue
            event = json.loads(line)
            if not isinstance(event, dict) or event.get('schema') != 'review_event_v1':
                raise ValueError('review_identity_ledger_schema_invalid')
            event_id = event.get('event_id')
            if not isinstance(event_id, str) or not event_id or event_id in seen_events:
                raise ValueError('review_identity_ledger_event_id_invalid')
            seen_events.add(event_id)
            if event.get('event_kind') != 'outcome':
                continue
            source = event.get('source_id')
            if not isinstance(source, str) or not source or source in seen_sources:
                continue
            # Mark first outcome even when it is already coded, so a later
            # under-specified retest cannot create a second identity anchor.
            seen_sources.add(source)
            coded = any(CODE_RE.search(str(event.get(field) or '')) for field in
                        ('knowledge_points', 'knowledge_point', 'knowledge_point_id', 'point_id', 'point_ids'))
            if (event.get('formal_node_id') or event.get('mapped_formal_node_ids') or coded
                    or event.get('review_unit_id') or source in self.master or source in self.source_formal
                    or source in bound_sources):
                continue
            sid = 'review-identity://' + source
            sha = _digest(line)
            self.unit(sid, sha, 'review_identity', [], event.get('observed_date') or event.get('event_time'),
                      event, role='formal_summary')
            metadata = {'review_source_id': source, 'anchor_event_id': event_id,
                        'source_fragment': f'line:{ordinal}', 'mapping_only': True}
            self.unit_inventory[sid].update(metadata)
            if self.export:
                self.units[-1].update(metadata)
            count += 1
        self.inventory.append({'source_id': str(REVIEW_REL), 'kind': 'review_identity_ledger',
            'sha256': ledger_sha, 'raw_read': True, 'event_count': len(seen_events), 'identity_unit_count': count})


def export_history_work_units(repo: str | Path, master: Mapping[str, Any], output_root: str | Path) -> dict[str, Any]:
    repo, target = Path(repo).resolve(), Path(output_root).resolve()
    target.relative_to(SEMANTIC_ROOT.resolve())
    collector = _Collector(repo, master, repo == CANONICAL_REPO.resolve(), export=True)
    collector.concept_reviews()
    collector.capture()
    collector.originals()
    collector.summaries()
    collector.docx()
    collector.review_identities()
    target.mkdir(parents=True, exist_ok=True, mode=0o700)
    os.chmod(target, 0o700)
    document = {'schema': 'knowledge_history_semantic_work_units_v1', 'units': collector.units,
                'coverage': {'raw_source_inventory': sorted(collector.inventory, key=lambda row: (row.get('source_id', ''), _json_bytes(row))), 'gaps': collector.gaps}}
    path = target / 'units.json'
    path.write_bytes(_json_bytes(document))
    os.chmod(path, 0o600)
    return {'path': str(path), 'unit_count': len(collector.units), 'bytes': path.stat().st_size,
            'kinds': dict(collections.Counter(u['source_kind'] for u in collector.units)),
            'gaps': collector.gaps}


def collect_history_sources(repo: str | Path, master: Mapping[str, Any], *, force_source_read: bool = False) -> dict[str, Any]:
    """Read known authoritative source families and return safe evidence records.

    Temporary fixtures never inspect home or T9.  Missing/unreadable originals
    become explicit gaps; malformed canonical ledger bytes fail the refresh.
    """
    repo = Path(repo).resolve()
    production = repo == CANONICAL_REPO.resolve()
    inventory = _inventory(repo, production)
    implementation_hash = implementation_sha256()
    signature = _digest(_json_bytes({'version': VERSION, 'implementation_sha256': implementation_hash, 'master': master,
                                    'inventory': [_guard(p) for p in inventory]}))
    cache = repo / STATE_REL
    previous = None
    if cache.is_file() and not force_source_read:
        try:
            cached = json.loads(cache.read_bytes())
            if cached.get("result_sha256") != _digest(_json_bytes(cached.get("result"))):
                raise ValueError("history_source_cache_digest_mismatch")
            if cached.get('version') == VERSION and cached.get('implementation_sha256') == implementation_hash:
                previous = cached['result']
            if cached.get('signature') == signature and all(
                {k: _guard(Path(row['path']))[k] for k in ('path', 'size', 'mtime_ns', 'ctime_ns')}
                == {k: row[k] for k in ('path', 'size', 'mtime_ns', 'ctime_ns')}
                for row in cached['result']['source_guards']):
                return cached['result']
        except (OSError, ValueError, KeyError, TypeError):
            pass
    collector = _Collector(repo, master, production, previous=previous)
    collector.concept_reviews()
    collector.capture()
    collector.originals()
    collector.summaries()
    collector.docx()
    collector.review_identities()
    collector.annotations()
    formal_revision_catalog = []
    if production or (repo / '.fixture-formal-knowledge').is_dir():
        from seal_knowledge_observations_408 import (load_formal_records, _formal_root,
            load_formal_correction_history, formal_correction_paths, _correction_ref)
        formal_records = load_formal_records(repo)
        reviewed = set()
        import sol_curation_decision_408 as curation
        decision_root = curation.DEFAULT_DECISION_ROOT if production else repo / '.fixture-curation-decisions'
        # Directory order is not stable across archive and annotation writes;
        # the raw-source inventory (and therefore the coverage object hash)
        # must stay reproducible for the cold audit.
        for path in sorted(_formal_root(repo).glob('*.json')):
            collector.track(path)
            chain = load_formal_correction_history(_formal_root(repo), path.stem)
            for dependency in formal_correction_paths(_formal_root(repo), path.stem):
                collector.track(dependency)
            for unit in json.loads(path.read_bytes())['units']:
                reviewed.add(unit['source_id'])
                formal_revision_catalog.append({'annotation_sha256': path.stem,
                    'annotation_path': str(path.resolve()), 'source_id': unit['source_id'],
                    'source_sha256': unit['source_sha256'],
                    'knowledge_points': [p['knowledge_point'] for p in unit.get('point_dispositions', [])],
                    'head_sha256': chain[-1]['sha256'] if chain else None,
                    'lifecycle_status': 'retracted' if chain and chain[-1].get('action') == 'retract' else 'active',
                    'correction_refs': [_correction_ref(value) for value in chain]})
                if not production:
                    import conversation_package_408 as packages
                    opened = packages.read_package(unit['source_id'])
                    for dependency in Path(opened['package_dir']).rglob('*'):
                        if dependency.is_file():
                            collector.track(dependency)
                binding = unit['formal_binding']
                terminal_path = decision_root / 'terminals' / (binding['terminal_sha256'] + '.json')
                collector.track(terminal_path)
                terminal = json.loads(terminal_path.read_bytes())
                collector.track(decision_root / 'decisions' / (terminal['decision_sha256'] + '.json'))
        collector.records = [r for r in collector.records if r.get('source_locator', r['source_id']) not in reviewed]
        collector.records.extend(formal_records)
    records = sorted(collector.records, key=lambda r: (r['observed_date'], r['source_id'], r['source_fragment'], r['summary']))
    coverage = {'schema': 'knowledge_history_source_coverage_v1',
                'formal_annotation_revisions': formal_revision_catalog,
                'raw_source_inventory': sorted(collector.inventory, key=lambda row: (row.get('source_id', ''), _json_bytes(row))), 'gaps': collector.gaps,
                'record_count': len(records), 'source_kind_counts': dict(collections.Counter(r['source_kind'] for r in records)),
                'evidence_role_counts': dict(collections.Counter(r['evidence_role'] for r in records)),
                'raw_read_count': sum(x.get('raw_read') is True for x in collector.inventory),
                'raw_read_failed_count': sum(x.get('raw_read') is False for x in collector.inventory),
                'semantic_units': [collector.unit_inventory[k] for k in sorted(collector.unit_inventory)],
                'semantic_reviewed_units': sum(x['semantic_status'] == 'reviewed' for x in collector.unit_inventory.values()),
                'semantic_pending_units': sum(x['semantic_status'] == 'pending' for x in collector.unit_inventory.values()),
                'text_semantic_review_complete': bool(collector.unit_inventory) and all(x['semantic_status'] == 'reviewed' for x in collector.unit_inventory.values())
                    and not any(g['reason'] in ('original_reopen_failed', 'legacy_archive_verification_failed', 'legacy_source_document_missing') for g in collector.gaps),
                'semantic_coverage_complete': False,
                'completeness_note': 'Verified reads and exact safe statements do not prove complete semantic extraction or independent mastery.'}
    result = {'records': records, 'coverage': coverage, 'source_files': sorted(collector.files),
              'source_guards': [collector.files[k] for k in sorted(collector.files)],
              'source_commitments': [{key: collector.files[k][key] for key in ('path', 'size', 'sha256')} for k in sorted(collector.files)],
              'source_dependencies': collector.source_dependencies}
    # An unavailable archive is retried next refresh rather than cached forever.
    retry_reasons = {'original_reopen_failed', 'legacy_archive_verification_failed', 'legacy_source_document_missing'}
    if not any(g['reason'] in retry_reasons for g in collector.gaps):
        cache.parent.mkdir(parents=True, exist_ok=True)
        fd, temporary = tempfile.mkstemp(prefix='.knowledge-history-', dir=cache.parent)
        try:
            with os.fdopen(fd, 'wb') as handle:
                handle.write(_json_bytes({'version': VERSION, 'implementation_sha256': implementation_hash, 'signature': signature, 'result_sha256': _digest(_json_bytes(result)), 'result': result}))
            os.replace(temporary, cache)
        finally:
            if os.path.exists(temporary):
                os.unlink(temporary)
    return result
