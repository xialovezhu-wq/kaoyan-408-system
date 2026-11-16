"""Read-only contacts from actual annual-paper records, including self reports.

The year plan is never a contact source. These facts only make the existing
14-day projection complete; they do not grade, emit attempts or infer mastery.
"""
from __future__ import annotations

import datetime as dt
import hashlib
import json
from pathlib import Path
import re

RECORD_ROOT = Path('真题年度学习')
SCHEMA = 'cs408-exam-year-learning-record-v1'
QUESTION = re.compile(r'EXAM408-(\d{4})-Q(\d{2})(?:-P(\d{2}))?')
NOT_STARTED = {'planned', 'prepared_not_started', 'not_started', 'scheduled', 'unstarted'}


class AnnualContactError(ValueError):
    pass


def require(ok, message):
    if not ok:
        raise AnnualContactError(message)


def encoded(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':')).encode()


def stamp(path):
    value = path.stat()
    return value.st_size, value.st_mtime_ns, value.st_ctime_ns, value.st_ino


def actual_date(value, location):
    if value is None or value == '' or value == '未记录':
        return None
    try:
        day = dt.date.fromisoformat(value)
    except (TypeError, ValueError) as exc:
        raise AnnualContactError('annual_actual_study_date_invalid:' + location) from exc
    require(day.isoformat() == value, 'annual_actual_study_date_invalid:' + location)
    return day


def canonical(value, year, location):
    found = QUESTION.fullmatch(str(value or ''))
    require(found is not None and int(found.group(1)) == year and 1 <= int(found.group(2)) <= 47
            and (found.group(3) is None or int(found.group(3)) > 0),
            'annual_question_identity_invalid:' + location)
    return value


def self_report(row):
    basis = str(row.get('result_basis') or row.get('basis') or '')
    return basis.startswith(('user_self_report', 'user_report'))


def collect_contacts(repo, day):
    """Read every current/historical attempt to EOF; invalid sources fail closed.

    Dates come only from question/attempt study_date. An explicitly unstarted
    current attempt is ignored even if it carries a planned date. Historical
    attempts remain independently eligible when their own evidence is real.
    """
    repo = Path(repo).resolve()
    root = repo / RECORD_ROOT
    if not root.exists():
        return {'by_identity': {}, 'record_files': [], 'unknown_date_records': []}
    require(root.is_dir(), 'annual_record_root_not_directory')
    contacts, sources, unknown = {}, [], []
    for folder in sorted(root.iterdir()):
        if not folder.is_dir() or not re.fullmatch(r'\d{4}', folder.name):
            continue
        path = folder / '学习记录.json'
        require(path.is_file() and not path.is_symlink(), 'annual_record_file_missing_or_unsafe:' + str(path))
        before = stamp(path)
        raw = path.read_bytes()
        require(stamp(path) == before, 'annual_record_changed_during_read:' + str(path))
        try:
            document = json.loads(raw)
        except (ValueError, UnicodeError) as exc:
            raise AnnualContactError('annual_record_json_invalid:' + str(path)) from exc
        require(isinstance(document, dict) and document.get('schema') == SCHEMA,
                'annual_record_schema_invalid:' + str(path))
        year = document.get('exam_year')
        require(type(year) is int and str(year) == folder.name, 'annual_record_year_invalid:' + str(path))
        relative = path.relative_to(repo).as_posix()
        digest = hashlib.sha256(raw).hexdigest()
        sources.append({'path': relative, 'sha256': digest})

        def offer(identity, row, pointer, observed, source_role):
            if observed is None:
                unknown.append({'path': relative, 'json_pointer': pointer, 'question_identity': identity})
                return
            if observed > day:
                return
            proof = {'path': relative, 'sha256': digest, 'json_pointer': pointer,
                     'record_sha256': hashlib.sha256(encoded(row)).hexdigest(),
                     'source_role': source_role, 'study_date': observed.isoformat()}
            for name in ('source_message_id', 'source_quote', 'capture_id', 'evidence_capture_id',
                         'self_report_evidence_capture_id'):
                if row.get(name):
                    proof[name] = row[name]
            evidence = f'{relative}#{pointer};sha256={digest};record_sha256={proof["record_sha256"]};role={source_role}'
            fact = {'study_date': observed.isoformat(), 'source_role': source_role,
                    'evidence': evidence, 'source_record': proof,
                    'independent_mastery_inferred': False}
            old = contacts.get(identity)
            if old is None or (fact['study_date'], evidence) > (old['study_date'], old['evidence']):
                contacts[identity] = fact

        def attempt(value, pointer, inherited_status=None):
            if value is None:
                return
            require(isinstance(value, dict), 'annual_attempt_invalid:' + relative + pointer)
            status = str(value.get('status') or inherited_status or '').lower()
            if status in NOT_STARTED:
                return
            records = value.get('question_records', [])
            ranges = value.get('reported_correct_ranges', [])
            groups = value.get('reported_correct_groups', [])
            require(all(isinstance(rows, list) for rows in (records, ranges, groups)),
                    'annual_attempt_records_invalid:' + relative + pointer)
            for number, row in enumerate(records):
                locator = pointer + '/question_records/' + str(number)
                require(isinstance(row, dict), 'annual_question_record_invalid:' + relative + locator)
                if str(row.get('review_status') or '').lower() in NOT_STARTED:
                    continue
                reported = (self_report(row) and row.get('reported_result') not in (None, '', 'not_reported'))
                capture_id = row.get('capture_id')
                require(capture_id in (None, '') or (isinstance(capture_id, str)
                        and re.fullmatch(r'CAP-\d{8}-[A-Za-z0-9]+', capture_id)),
                        'annual_capture_identity_invalid:' + relative + locator)
                captured = bool(capture_id)
                if not reported and not captured:
                    continue
                identity = canonical(row.get('question_id'), year, relative + locator)
                when = actual_date(row.get('study_date') or value.get('study_date'), relative + locator)
                offer(identity, row, locator, when, 'self_report' if reported else 'recorded_study_with_capture')
            for kind, rows in (('reported_correct_ranges', ranges), ('reported_correct_groups', groups)):
                for number, row in enumerate(rows):
                    locator = pointer + '/' + kind + '/' + str(number)
                    require(isinstance(row, dict), 'annual_report_group_invalid:' + relative + locator)
                    if not self_report(row):
                        continue
                    require(any(row.get(name) for name in ('source_quote', 'source_message_id', 'evidence_capture_id', 'capture_id')),
                            'annual_report_group_source_required:' + relative + locator)
                    if kind == 'reported_correct_ranges':
                        first, last = row.get('from_question'), row.get('to_question')
                        require(type(first) is int and type(last) is int and 1 <= first <= last <= 47,
                                'annual_report_range_invalid:' + relative + locator)
                        numbers = list(range(first, last + 1))
                    else:
                        numbers = row.get('questions')
                        require(isinstance(numbers, list) and numbers and all(type(n) is int and 1 <= n <= 47 for n in numbers),
                                'annual_report_group_questions_invalid:' + relative + locator)
                    when = actual_date(row.get('study_date') or value.get('study_date'), relative + locator)
                    for question in sorted(set(numbers)):
                        offer(f'EXAM408-{year}-Q{question:02}', row, locator, when, 'self_report')

        historical = document.get('attempts', [])
        require(isinstance(historical, list), 'annual_attempt_history_invalid:' + relative)
        for number, value in enumerate(historical):
            attempt(value, '/attempts/' + str(number))
        attempt(document.get('current_attempt'), '/current_attempt', document.get('status'))
    return {'by_identity': contacts, 'record_files': sources, 'unknown_date_records': unknown}
