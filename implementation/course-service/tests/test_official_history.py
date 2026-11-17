"""Official personal result fields, coverage and read-only request contract."""
import pytest

from activity_protocol import ActivityPayloadError
from official_history import (COURSE_HISTORY_PATH, COURSE_SUMMARY_PATH,
                              fetch_course_history, parse_course_history)

COURSE = {'courseId': '100', 'classId': '200', 'name': '示例课程'}


def entry(aid='300', **changes):
    return {'activeid': aid, 'activeType': 2, 'other_id': 0, 'name': '普通签到',
            'userStatus': 1, 'status': 2, 'submittime': '2026-10-01 08:01:02', **changes}


def payload(rows, **changes):
    return {'result': 1, 'data': {'total': len(rows), 'page': 1, 'pageSize': 9999, 'list': rows, **changes}}


def test_only_personal_signed_results_are_successes_and_time_is_actual_submission():
    rows = [entry(), entry('301', userStatus=2, other_id=3, submittime='2026-10-01 09:01:02.123')]
    rows += [entry(str(400 + i), userStatus=state) for i, state in enumerate(
        [None, 0, 4, 5, 7, 8, 9, 10, 11, 12, True, 'unknown'])]
    result = parse_course_history(payload(rows), COURSE, 'own-account')
    records = result['records']
    assert [r['activeId'] for r in records] == ['300', '301']
    assert [r['statusLabel'] for r in records] == ['已签', '教师代签']
    assert [r['recordedAt'] for r in records] == ['2026-10-01T00:01:02Z', '2026-10-01T01:01:02.123000Z']
    assert records[1]['kind'] == 'gesture'
    assert all(r['source'] == 'chaoxing' and r['accountId'] == 'own-account' for r in records)
    assert result['source']['status'] == 'ok' and result['source']['count'] == 2


@pytest.mark.parametrize('time', [None, '', 'invalid', '2026-02-30 08:01:02', '2026-10-01T08:01:02Z'])
def test_missing_or_unrecognized_time_does_not_become_query_or_activity_time(time):
    result = parse_course_history(payload([entry(submittime=time, starttime='2026-10-01 08:00:00')]), COURSE, 'own')
    assert result['records'][0]['recordedAt'] is None


def test_unknown_empty_response_and_explicit_empty_list_have_distinct_coverage():
    missing = parse_course_history({'result': 0, 'errorMsg': '无此用户数据', 'data': None}, COURSE, 'own')
    empty = parse_course_history(payload([]), COURSE, 'own')
    assert missing['records'] == empty['records'] == []
    assert missing['source']['status'] == 'unavailable' and missing['source']['total'] is None
    assert empty['source']['status'] == 'ok' and empty['source']['total'] == 0


def test_truncation_and_unrecognized_entries_are_explicitly_partial():
    truncated = parse_course_history(payload([entry()], total=20000), COURSE, 'own')
    malformed = parse_course_history(payload([entry(), {'activeid': 'invalid'}]), COURSE, 'own')
    assert truncated['source']['status'] == malformed['source']['status'] == 'partial'
    assert truncated['source']['total'] == 20000 and truncated['source']['returned'] == 1
    assert len(truncated['records']) == len(malformed['records']) == 1


def test_ambiguous_duplicate_activity_is_not_reported_as_current_success():
    result = parse_course_history(payload([entry(), entry(userStatus=5)]), COURSE, 'own')
    assert result['records'] == [] and result['source']['status'] == 'partial'


def test_whitelist_drops_teacher_identifiers_private_fields_and_unexpected_activity_types():
    result = parse_course_history(payload([entry(createUid='private-id', raw='private-text', enc='secret'),
                                          entry('301', activeType=74)]), COURSE, 'own')
    assert len(result['records']) == 1 and result['source']['status'] == 'partial'
    assert not {'createUid', 'raw', 'enc', 'attendid'}.intersection(result['records'][0])


def test_fetch_uses_only_fixed_read_query_and_keeps_format_failure_scoped_to_course():
    calls = []
    second = {**COURSE, 'courseId': '101'}
    def read(client, path, params):
        calls.append((client, path, params))
        if params['courseId'] == '101':
            raise ActivityPayloadError('invalid json')
        return payload([entry()])
    result = fetch_course_history('client', '999', [COURSE, second], 'own', read)
    assert [call[1] for call in calls] == [COURSE_HISTORY_PATH, COURSE_HISTORY_PATH]
    assert calls[0][2] == {'courseId': '100', 'classId': '200', 'puid': '999', 'pageSize': 9999, 'duid': '', 'denc': ''}
    assert [s['status'] for s in result['sources']] == ['ok', 'unavailable']
    assert len(result['records']) == 1


def test_fetch_propagates_session_and_rate_limit_failures_without_following_courses():
    calls = []
    class RateLimited(Exception):
        pass
    def read(*args):
        calls.append(args)
        raise RateLimited()
    with pytest.raises(RateLimited):
        fetch_course_history(None, '999', [COURSE, COURSE], 'own', read)
    assert len(calls) == 1


def test_no_user_data_is_confirmed_empty_only_by_own_course_summary():
    calls = []
    def read(client, path, params):
        calls.append((path, params))
        if path == COURSE_HISTORY_PATH:
            return {'result': 0, 'errorMsg': '无此用户数据', 'data': None}
        return {'result': 1, 'data': {'uid': '999', 'classId': 200, 'allCount': 0,
                                     'attendanceCount': 0, 'lateCount': 0, 'effectiveAttendance': 0}}
    result = fetch_course_history(None, '999', [COURSE], 'own', read)
    assert result['records'] == []
    assert result['sources'][0]['status'] == 'ok'
    assert result['sources'][0]['total'] == result['sources'][0]['returned'] == 0
    assert calls[1] == (COURSE_SUMMARY_PATH, {'courseId': '100', 'classId': '200',
                                            'puid': '999', 'duid': '', 'denc': ''})


@pytest.mark.parametrize('change', [
    {'uid': '998'}, {'classId': '201'}, {'allCount': 1}, {'attendanceCount': 1},
    {'lateCount': 1}, {'allCount': None}, {'attendanceCount': False},
])
def test_zero_summary_with_wrong_identity_missing_counts_or_nonzero_data_stays_unavailable(change):
    def read(client, path, params):
        if path == COURSE_HISTORY_PATH:
            return {'result': 0, 'errorMsg': '无此用户数据', 'data': None}
        return {'result': 1, 'data': {'uid': '999', 'classId': '200', 'allCount': 0,
                                     'attendanceCount': 0, **change}}
    result = fetch_course_history(None, '999', [COURSE], 'own', read)
    assert result['records'] == []
    assert result['sources'][0]['status'] == 'unavailable'
    assert result['sources'][0]['total'] is None


def test_malformed_empty_summary_does_not_hide_unknown_coverage():
    def read(client, path, params):
        if path == COURSE_HISTORY_PATH:
            return {'result': 0, 'errorMsg': '无此用户数据', 'data': None}
        raise ActivityPayloadError('unrecognized response')
    result = fetch_course_history(None, '999', [COURSE], 'own', read)
    assert result['sources'][0]['status'] == 'unavailable'
