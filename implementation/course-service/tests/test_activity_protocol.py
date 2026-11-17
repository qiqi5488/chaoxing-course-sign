"""Attendance rules must be explicit before any automatic submission is eligible."""
from pathlib import Path
import sys

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from activity_protocol import ActivityPayloadError, parse_list, parse_detail, merge_lists

COURSE = {'courseId': '400', 'classId': '500', 'name': '测试课程'}
CANDIDATE = {'activeId': '300', 'nameOne': '签到'}
DETAIL = {'data': {'activeId': '300', 'activeType': 2, 'otherId': 0, 'ifphoto': 0,
                   'ifopenAddress': 0, 'openCheckFaceFlag': 0, 'starttime': 1_800_000_000_000,
                   'endtime': 1_800_001_000_000, 'nowTime': 1_800_000_500_000}}


def test_merge_and_filter_uses_numeric_activity_type_without_guessing_missing():
    main = parse_list({'data': {'activeList': [{'id': 300}, {'id': 301, 'activeType': 1}]}}, 'normal')
    task = parse_list({'activeList': [{'activeId': '300', 'nameOne': '签到', 'activeType': 2},
                                    {'activeId': '302', 'activeType': 74}]}, 'task')
    merged = merge_lists([main, task])
    assert [row['activeId'] for row in merged] == ['300', '302']
    assert merged[0]['nameOne'] == '签到'


@pytest.mark.parametrize('payload', [{}, {'result': 0, 'activeList': []}, {'activeList': None},
                                     {'activeList': [{'name': 'missing id'}]}])
def test_invalid_lists_never_become_empty_success(payload):
    with pytest.raises(ActivityPayloadError):
        parse_list(payload, 'task')


def test_only_complete_ordinary_details_are_auto_eligible():
    result = parse_detail(DETAIL, CANDIDATE, COURSE)
    assert result['kind'] == 'ordinary'
    assert result['status'] == 'active'
    assert result['autoEligible'] is True
    for field in ('otherId', 'ifphoto', 'ifopenAddress', 'openCheckFaceFlag'):
        incomplete = dict(DETAIL['data'])
        del incomplete[field]
        row = parse_detail({'data': incomplete}, CANDIDATE, COURSE)
        assert row['autoEligible'] is False
        assert 'unknown' in row['requirements']


@pytest.mark.parametrize('changes,kind,requirement', [
    ({'ifphoto': 1}, 'photo', 'photo'), ({'otherId': 2}, 'qr', 'qr'),
    ({'otherId': 3}, 'gesture', 'code'), ({'otherId': 4}, 'position', 'location'),
    ({'otherId': 5}, 'code', 'code'), ({'openCheckFaceFlag': 1}, 'unknown', 'face'),
    ({'ifopenAddress': 1}, 'unknown', 'location'),
])
def test_requirements_never_auto_submit(changes, kind, requirement):
    row = parse_detail({'data': {**DETAIL['data'], **changes}}, CANDIDATE, COURSE)
    assert row['kind'] == kind
    assert requirement in row['requirements']
    assert not row['autoEligible']


@pytest.mark.parametrize('changes,status', [
    ({'nowTime': 1_800_001_000_000}, 'ended'),
    ({'nowTime': 1_799_999_000_000}, 'scheduled'), ({'nowTime': None}, 'unknown'),
    ({'endtime': None}, 'unknown'), ({'starttime': None}, 'unknown'),
])
def test_inactive_or_unknown_window_never_auto_submits(changes, status):
    row = parse_detail({'data': {**DETAIL['data'], **changes}}, CANDIDATE, COURSE)
    assert row['status'] == status
    assert not row['autoEligible']


def test_seconds_timestamps_are_normalized_and_no_end_is_supported():
    row = parse_detail({'data': {**DETAIL['data'], 'starttime': 1_800_000_000,
                               'endtime': 0, 'nowTime': 1_800_000_500}}, CANDIDATE, COURSE)
    assert row['startTime'] == 1_800_000_000_000
    assert row['autoEligible']


def test_detail_identity_mismatch_and_non_attendance_are_rejected():
    for field, value in (('activeId', '301'), ('courseId', '401'), ('classId', '501')):
        with pytest.raises(ActivityPayloadError):
            parse_detail({'data': {**DETAIL['data'], field: value}}, CANDIDATE, COURSE)
    assert parse_detail({'data': {**DETAIL['data'], 'activeType': 1}}, CANDIDATE, COURSE) is None


def test_official_nullable_course_and_id_class_aliases_are_accepted():
    payload = {'result': 1, 'data': {**DETAIL['data'], 'id': 300, 'clazzid': 500, 'courseId': None}}
    del payload['data']['activeId']
    assert parse_detail(payload, CANDIDATE, COURSE)['autoEligible']
    for field in ('id', 'clazzid'):
        with pytest.raises(ActivityPayloadError):
            parse_detail({'data': {**payload['data'], field: 999}}, CANDIDATE, COURSE)


def test_task_status_is_not_a_result_flag():
    assert parse_list({'result': 1, 'status': 0, 'activeList': [], 'errorMsg': ''}, 'task') == []
