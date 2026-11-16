"""Read-only freshness checks for frozen exact-question metadata references.

The immutable pin preserves provenance, while this check compares only the
referenced formal row's teaching metadata with the current master. Changes to
review dates, learner evidence, or unrelated rows do not invalidate a lesson.
"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
import re

ROLE = 'frozen_exact_question_identity_metadata'
SCHEMA = 'cs408-exact-question-scoped-source-metadata-v1'
FIELDS = ('来源ID', '年份', '科目', '主知识点', '命中知识点', '题型', '核心考点', '详情入口')
FORMAL = re.compile(r'(?:DS|CO|OS|CN)_(?:\d{4}|UNK)_\d{3}')


def _digest(value):
    raw = (json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':')) + '\n').encode()
    return hashlib.sha256(raw).hexdigest()


def _master_rows(path, cache):
    stat = path.stat()
    key = ('master_rows', str(path), stat.st_size, stat.st_mtime_ns, stat.st_ctime_ns)
    if key in cache:
        return cache[key]
    lines = path.read_text().splitlines()
    headers = [line.strip('|').split('|') for line in lines if line.startswith('|ID|')]
    if len(headers) != 1 or not set(FIELDS) <= set(headers[0]):
        raise ValueError('master_header_invalid')
    header = headers[0]
    rows = {}
    for line in lines:
        if not re.match(r'\|(?:DS|CO|OS|CN)_', line):
            continue
        values = line.strip('|').split('|')
        if len(values) != len(header) or values[0] in rows:
            raise ValueError('master_row_invalid')
        rows[values[0]] = dict(zip(header, values))
    cache[key] = rows
    return rows


def scoped_metadata_issue(repo_root, template, source_ref, *, cache=None):
    """Return one stale-source issue, or None for current/unrelated references.

    Call during load_catalog's normal source check. The caller still validates
    the pinned file's admitted SHA-256; this adds the current-row comparison.
    It deliberately returns no whole-master source guard. The snapshot owner's
    normal master freshness check triggers cold rebuilding, which reruns this
    comparison without invalidating other questions for unrelated row changes.
    """
    if source_ref.get('role') != ROLE:
        return None
    repo = Path(repo_root).resolve()
    identity = template.get('question_identity')
    formal = template.get('parent_identity') if template.get('identity_kind') == 'teaching_part' else identity
    master = repo / '节点总表.md'
    issue = {'path': source_ref.get('path'), 'locator': source_ref.get('locator'),
             'live_source_path': str(master), 'formal_identity': formal,
             'reason': 'scoped_metadata_reference_invalid'}
    try:
        path = Path(source_ref['path'])
        path = path if path.is_absolute() else repo / path
        if not path.is_file() or path.is_symlink() or path.stat().st_size > 256 * 1024:
            return issue
        pin = json.loads(path.read_bytes())
        if (pin.get('schema') != SCHEMA or pin.get('question_identity') != identity
                or pin.get('formal_identity') != formal or not isinstance(formal, str)
                or not FORMAL.fullmatch(formal)):
            return issue
        metadata = pin.get('metadata')
        if (not isinstance(metadata, dict) or set(metadata) != set(FIELDS)
                or not all(isinstance(metadata[key], str) for key in FIELDS)
                or pin.get('metadata_sha256') != _digest(metadata)):
            return issue
        issue['declared_semantic_sha256'] = pin['metadata_sha256']
        if not master.is_file():
            return {**issue, 'reason': 'scoped_metadata_live_source_missing'}
        rows = _master_rows(master, {} if cache is None else cache)
        if formal not in rows:
            return {**issue, 'reason': 'scoped_metadata_formal_identity_missing'}
        current = {key: rows[formal][key] for key in FIELDS}
        if current != metadata:
            return {**issue, 'reason': 'scoped_metadata_changed',
                    'observed_semantic_sha256': _digest(current),
                    'changed_fields': [key for key in FIELDS if current[key] != metadata[key]]}
        return None
    except (OSError, ValueError, TypeError, KeyError):
        return issue
