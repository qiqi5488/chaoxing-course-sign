"""Synthetic course notification shapes; no accounts, sockets or sign requests."""
import base64
from copy import deepcopy
import json
from pathlib import Path
import sys
from types import SimpleNamespace
from unittest.mock import MagicMock, Mock, patch
from urllib.parse import urlencode

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import group_notifications as notices


ORIGIN = 'https://mobilelearn.chaoxing.com'


def course_notice(active='300', course='700', clazz='800', **changes):
    # Observed 2026-11-13 official /newsign/preSign field names, synthetic IDs.
    query = {'activePrimaryId': active, 'appType': '15', 'classId': clazz,
             'courseId': course, 'general': '1', 'ls': '1', 'sys': '1'}
    activity = {'aid': active, 'atype': 2, 'type': 1, 'atypeName': '签到',
                'title': '课程签到', 'url': ORIGIN + '/newsign/preSign?' + urlencode(query),
                'courseInfo': {'classid': clazz, 'courseid': course}}
    activity.update(changes)
    return {'attachmentType': 15, 'att_chat_course': activity}


def group_notice(active='300', group='900'):
    return {'attachmentType': 15, 'att_chat_course': {
        'aid': active, 'atype': 2, 'type': 1, 'atypeName': '签到', 'title': '群签到',
        'url': ORIGIN + '/sign/preSign?' + urlencode({'activeId': active, 'chatId': group})}}


def channel(attachment, group='900'):
    return {'session_type': 'groupchat', 'session_to': group,
            'meta': {'to': 'cx-dev#cxstudy_' + group + '@conference.easemob.com',
                     'payload': json.dumps({'type': 'groupchat', 'to': group,
                                           'ext': {'attachment': attachment}})}}


def varint(value):
    data = bytearray()
    while value > 127:
        data.append((value & 127) | 128)
        value >>= 7
    return bytes(data) + bytes([value])


def scalar(number, value):
    return varint(number << 3) + varint(value)


def blob(number, value):
    value = value.encode('utf-8') if isinstance(value, str) else value
    return varint(number << 3 | 2) + varint(len(value)) + value


def historical(attachment, group='900'):
    jid = blob(1, 'cx-dev#cxstudy') + blob(2, group) + blob(3, 'conference.easemob.com')
    ext = blob(1, 'attachment') + scalar(2, 8) + blob(6, json.dumps(attachment))
    body = scalar(1, 2) + blob(3, jid) + blob(4, blob(2, '测试消息')) + blob(5, ext)
    meta = blob(3, jid) + scalar(5, 1) + blob(6, body)
    return {'msg': base64.b64encode(meta).decode('ascii')}


def fetch(channels, histories):
    def response(data):
        return SimpleNamespace(status_code=200, content=json.dumps(data).encode('utf-8'))

    client = SimpleNamespace(request=Mock(return_value=response({
        'result': 1, 'msg': {'puid': '101', 'uid': 'im-test-user', 'hxToken': 'offline-test-token'}})))
    session = MagicMock()
    session.__enter__.return_value = session
    session.headers, session.adapters = {}, {}
    session.request.side_effect = [response({'data': {'channel_infos': channels, 'cursor': ''}})] + [
        response({'data': {'msgs': rows, 'is_last': True}}) for rows in histories]
    with patch.object(notices.requests, 'Session', return_value=session):
        result = notices.fetch_group_notifications(client, '101')
    assert session.request.call_count == 1 + len(histories)
    for call in session.request.call_args_list:
        assert call.args[1].startswith(notices.REST_ORIGIN + notices.NAMESPACE + '/')
        assert 'preSign' not in call.args[1]
        assert call.kwargs['allow_redirects'] is False
    assert client.request.call_count == 1
    return result


def test_observed_course_shape_is_a_candidate_without_rule_or_url_leaks():
    item = course_notice(aid=300, courseInfo={'courseid': 700, 'classid': 800},
                         pcUrl='https://teacher.example/private', otherId=0, ifphoto=0,
                         ifopenAddress=0, openCheckFaceFlag=0, status=1)
    assert notices._course_activity(item) == {
        'activeId': '300', 'courseId': '700', 'classId': '800', 'name': '课程签到',
        'kind': 'unknown', 'status': 'unknown', 'activityScope': 'course',
        'notificationType': 'course_attendance', 'requirements': ['unknown'], 'autoEligible': False}
    assert notices._activity(item) is None
    assert notices._course_activity(group_notice()) is None


@pytest.mark.parametrize('title', ['签到', '普通签到', '二维码签到', '手势签到'])
def test_labels_do_not_determine_course_kind_or_readiness(title):
    row = notices._course_activity(course_notice(title=title, atypeName=title))
    assert (row['kind'], row['status'], row['autoEligible']) == ('unknown', 'unknown', False)
    assert row['requirements'] == ['unknown']


def test_equal_optional_identity_aliases_and_absent_course_info_are_allowed():
    item = course_notice(id=300)
    item['att_chat_course']['url'] += '&activeId=300'
    del item['att_chat_course']['courseInfo']
    assert notices._course_activity(item)['activeId'] == '300'
    del item['att_chat_course']['aid']
    assert notices._course_activity(item)['activeId'] == '300'


def test_observed_single_trailing_query_separator_is_allowed_without_changing_group_parser():
    item = course_notice()
    item['att_chat_course']['url'] += '&'
    assert notices._course_activity(item)['activeId'] == '300'
    result = fetch([channel(item)], [[historical(item)]])
    assert len(result['courseActivities']) == 1 and result['activities'] == []
    group = group_notice()
    group['att_chat_course']['url'] += '&'
    assert notices._activity(group) is None


@pytest.mark.parametrize('suffix', ['&&', '&broken&', '&activePrimaryId=300&', '&%61ctivePrimaryId=300&'])
def test_trailing_separator_exception_does_not_accept_malformed_or_duplicate_parameters(suffix):
    item = course_notice()
    item['att_chat_course']['url'] += suffix
    assert notices._course_activity(item) is None


@pytest.mark.parametrize('query', [
    'activeId=300&courseId=700&classId=800',
    'activePrimaryId=300&activeId=301&courseId=700&classId=800',
    'activePrimaryId=300&activeId=&courseId=700&classId=800',
    'activePrimaryId=300&activePrimaryId=300&courseId=700&classId=800',
    'activePrimaryId=300&%61ctivePrimaryId=300&courseId=700&classId=800',
    'activePrimaryId=300&courseId=700&courseId=701&classId=800',
    'activePrimaryId=300&courseId=700&classId=800&classId=801',
    'activePrimaryId=300&courseId=700',
    'activePrimaryId=300&classId=800',
    'activePrimaryId=300&courseId=700&classId=800&sys=%ZZ',
    'activePrimaryId=300&courseId=700&classId=800&sys=%FF',
    'activePrimaryId=300&courseId=700&classId=800&sys=%0A',
    'activePrimaryId=300&courseId=700&classId=800&%00=sys',
    'activePrimaryId=300&courseId=700&classId=800&broken',
])
def test_missing_conflicting_duplicate_and_badly_encoded_query_is_rejected(query):
    assert notices._course_activity(course_notice(url=ORIGIN + '/newsign/preSign?' + query)) is None


@pytest.mark.parametrize('base', [
    'http://mobilelearn.chaoxing.com/newsign/preSign',
    'https://evil.example/newsign/preSign',
    'https://mobilelearn.chaoxing.com.evil.example/newsign/preSign',
    'https://user@mobilelearn.chaoxing.com/newsign/preSign',
    'https://user:pass@mobilelearn.chaoxing.com/newsign/preSign',
    'https://mobilelearn.chaoxing.com:8443/newsign/preSign',
    'https://mobilelearn.chaoxing.com:bad/newsign/preSign',
    '//mobilelearn.chaoxing.com/newsign/preSign',
    '/newsign/preSign',
    'https://mobilelearn.chaoxing.com/newsign/preSign/',
    'https://mobilelearn.chaoxing.com/newsign/%70reSign',
    'https://mobilelearn.chaoxing.com/sign/preSign',
])
def test_noncanonical_or_untrusted_origins_and_paths_are_rejected(base):
    query = 'activePrimaryId=300&courseId=700&classId=800'
    assert notices._course_activity(course_notice(url=base + '?' + query)) is None


@pytest.mark.parametrize('suffix', ['#private', '#', '&' + '&'.join('p%d=1' % i for i in range(17))])
def test_fragments_and_excessive_query_fields_are_rejected(suffix):
    item = course_notice()
    item['att_chat_course']['url'] += suffix
    assert notices._course_activity(item) is None


@pytest.mark.parametrize('key', ['activePrimaryId', 'courseId', 'classId'])
@pytest.mark.parametrize('value', ['0', '-1', '0700', '', '1.5', 'true', '１２３'])
def test_identifiers_are_positive_decimal_ids(key, value):
    query = {'activePrimaryId': '300', 'courseId': '700', 'classId': '800', key: value}
    assert notices._course_activity(course_notice(url=ORIGIN + '/newsign/preSign?' + urlencode(query))) is None


@pytest.mark.parametrize('changes', [
    {'aid': '301'}, {'aid': True}, {'aid': 300.0}, {'id': '301'}, {'id': None},
    {'courseInfo': None}, {'courseInfo': []}, {'courseInfo': {}},
    {'courseInfo': {'courseid': '700'}},
    {'courseInfo': {'courseid': '701', 'classid': '800'}},
    {'courseInfo': {'courseid': '700', 'classid': '801'}},
    {'courseInfo': {'courseid': True, 'classid': '800'}},
    {'atype': 14}, {'type': 4},
])
def test_envelope_and_course_info_must_bind_to_url_identity(changes):
    assert notices._course_activity(course_notice(**changes)) is None


def test_missing_attachment_and_missing_payload_identity_are_normal_skips():
    for item in [None, [], {}, {'attachmentType': 18},
                 {'attachmentType': 15, 'att_chat_course': []}]:
        assert notices._course_activity(item) is None
    item = course_notice()
    del item['att_chat_course']['aid']
    assert notices._course_activity(item) is None


def test_latest_and_history_course_notifications_deduplicate_by_course_class_activity():
    first = course_notice()
    second = course_notice(course='701')
    third = course_notice(clazz='801')
    result = fetch([channel(first)], [[historical(first), historical(second), historical(third)]])
    assert result['activities'] == []
    assert {(row['courseId'], row['classId'], row['activeId']) for row in result['courseActivities']} == {
        ('700', '800', '300'), ('701', '800', '300'), ('700', '801', '300')}
    assert result['status'] == 'ok'
    assert result['sourceCoverage']['malformedMessages'] == 0


def test_same_numeric_group_and_course_activity_ids_do_not_cross_scopes():
    result = fetch([channel(course_notice()), channel(group_notice(group='901'), group='901')],
                   [[historical(course_notice())], [historical(group_notice(group='901'), group='901')]])
    assert len(result['activities']) == len(result['courseActivities']) == 1
    group, course = result['activities'][0], result['courseActivities'][0]
    assert group['activeId'] == course['activeId'] == '300'
    assert group['activityScope'] == 'group' and 'courseId' not in group
    assert course['activityScope'] == 'course' and 'chatId' not in course


def test_malformed_course_notice_cannot_spoof_group_or_suppress_valid_history():
    invalid = course_notice(id='999', pcUrl=group_notice()['att_chat_course']['url'])
    result = fetch([channel(invalid)], [[historical(course_notice())]])
    assert result['activities'] == []
    assert [row['activeId'] for row in result['courseActivities']] == ['300']
    assert result['status'] == 'ok'


def test_course_name_is_bounded_and_drops_controls_without_changing_input():
    item = course_notice(title='课\n程\x00' + '签' * 250)
    original = deepcopy(item)
    row = notices._course_activity(item)
    assert len(row['name']) == 200 and '\n' not in row['name'] and '\x00' not in row['name']
    assert item == original
