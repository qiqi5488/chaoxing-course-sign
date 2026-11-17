"""Notification candidates are verified by read-only synthetic course HTTP."""
from copy import deepcopy
from urllib.parse import parse_qs, urlsplit

import pytest
import requests

from test_ordinary_http import gateway
from test_upstream import PROFILE, COOKIES, IDENTITY, ORDINARY_DETAIL
import upstream


COURSE = {'courseId': '400', 'classId': '500', 'name': '测试课程'}
DETAIL_PATH = '/v2/apis/active/getPPTActiveInfo'


def candidate(**changes):
    return {'activeId': '300', 'courseId': '400', 'classId': '500', 'name': '通知标题',
            'kind': 'unknown', 'status': 'unknown', 'activityScope': 'course',
            'notificationType': 'course_attendance', 'requirements': ['unknown'],
            'autoEligible': False, **changes}


def detail(**changes):
    return {'data': {**ORDINARY_DETAIL['data'], **changes}}


def read(client, *, course=None, candidates=None, identity=None):
    return client.get_course_notification_activities(COOKIES, identity or IDENTITY,
        COURSE if course is None else course, [candidate()] if candidates is None else candidates)


@pytest.mark.parametrize(('other_id', 'kind', 'requirements'), [(0, 'ordinary', []), (3, 'gesture', ['code'])])
def test_notification_details_determine_rules_and_only_the_fixed_detail_endpoint_is_read(other_id, kind, requirements):
    with gateway([{'body': PROFILE}, {'body': detail(otherId=other_id)}]) as (client, adapter, notifications):
        result = read(client, candidates=[candidate(name='手势二维码普通签到')])
    row = result['activities'][0]
    assert row['kind'] == kind and row['status'] == 'active' and row['requirements'] == requirements
    assert row['notificationType'] == 'course_attendance' and row['activityScope'] == 'course'
    assert row['courseId'] == '400' and row['classId'] == '500'
    assert result['detailErrors'] == 0 and result['status'] == 'ok'
    assert result['sources'] == [{'id': 'notification', 'status': 'ok', 'count': 1}]
    assert result['cookies'] and result['fetchedAt'].endswith('Z')
    assert [urlsplit(r.url).path for r in adapter.sent] == ['/apis/login/userLogin4Uname.do', DETAIL_PATH]
    assert all(r.method == 'GET' and r.body is None for r in adapter.sent)
    assert parse_qs(urlsplit(adapter.sent[-1].url).query) == {'activeId': ['300']}
    assert all(urlsplit(r.url).scheme == 'https' and urlsplit(r.url).hostname in
               ('sso.chaoxing.com', 'mobilelearn.chaoxing.com') for r in adapter.sent)
    assert not any(word in r.url for r in adapter.sent for word in
                   ('activelist', 'preSign', 'stuSignajax', 'studentSign', 'signIn', 'analysis', 'checkSignCode'))
    notifications.assert_not_called()


@pytest.mark.parametrize('other_id', [0, 3])
def test_ended_course_notifications_remain_visible_but_never_auto_eligible(other_id):
    ended = detail(otherId=other_id, nowTime=1_800_002_000_000)
    with gateway([{'body': PROFILE}, {'body': ended}]) as (client, adapter, _):
        result = read(client)
    assert len(result['activities']) == 1
    row = result['activities'][0]
    assert row['status'] == 'ended' and row['autoEligible'] is False
    assert row['notificationType'] == 'course_attendance' and len(adapter.sent) == 2


@pytest.mark.parametrize('activity_key', ['activeId', 'id'])
@pytest.mark.parametrize('class_key', ['classId', 'clazzid'])
def test_explicit_official_activity_and_class_aliases_allow_nullable_course_id(activity_key, class_key):
    activity = detail(courseId=None)
    del activity['data']['activeId']
    del activity['data']['clazzid']
    activity['data'].update({activity_key: 300, class_key: 500})
    with gateway([{'body': PROFILE}, {'body': activity}]) as (client, _, _):
        result = read(client)
    assert result['activities'][0]['kind'] == 'ordinary' and result['detailErrors'] == 0


@pytest.mark.parametrize('changes', [
    {'activeId': None}, {'activeId': ''}, {'activeId': True}, {'activeId': '301'},
    {'clazzid': None}, {'clazzid': ''}, {'clazzid': False}, {'clazzid': '501'},
    {'courseId': '401'}, {'id': '301'}, {'classId': '501'},
])
def test_missing_or_conflicting_detail_bindings_keep_a_typed_unknown_candidate(changes):
    with gateway([{'body': PROFILE}, {'body': detail(**changes)}]) as (client, adapter, _):
        result = read(client)
    assert result['detailErrors'] == 1 and result['status'] == 'partial'
    row = result['activities'][0]
    assert row['activeId'] == '300' and row['name'] == '通知标题'
    assert row['kind'] == row['status'] == 'unknown' and row['requirements'] == ['unknown']
    assert row['autoEligible'] is False and row['notificationType'] == 'course_attendance'
    assert len(adapter.sent) == 2


@pytest.mark.parametrize('response', [
    {'status': 503}, {'body': 'private-invalid-json'}, {'body': {'data': []}},
    {'status': 302, 'headers': {'Location': 'https://mobilelearn.chaoxing.com/pptSign/stuSignajax?activeId=300'}},
])
def test_failed_details_are_unknown_not_empty_success_and_redirects_do_not_submit(response):
    with gateway([{'body': PROFILE}, response]) as (client, adapter, _):
        result = read(client)
    assert result['detailErrors'] == 1 and result['status'] == 'partial'
    assert len(result['activities']) == 1 and result['activities'][0]['kind'] == 'unknown'
    assert len(adapter.sent) == 2 and 'private-invalid-json' not in str(result)


def test_one_detail_transport_failure_does_not_retry_or_hide_another_candidate():
    replies = [{'body': PROFILE}, requests.ReadTimeout('private-timeout'), {'body': detail(activeId='301')}]
    with gateway(replies) as (client, adapter, _):
        result = read(client, candidates=[candidate(), candidate(activeId='301')])
    assert [row['kind'] for row in result['activities']] == ['unknown', 'ordinary']
    assert result['detailErrors'] == 1 and len(adapter.sent) == 3
    assert 'private-timeout' not in str(result)


@pytest.mark.parametrize('bad', [
    {'activeId': '0'}, {'activeId': '-1'}, {'activeId': True}, {'activeId': 'arbitrary'},
    {'courseId': '401'}, {'classId': '501'}, {'activityScope': 'group'},
    {'notificationType': 'group_attendance'},
])
def test_invalid_or_cross_pair_candidates_are_rejected_before_any_http(bad):
    with gateway([]) as (client, adapter, _), pytest.raises(upstream.UpstreamError) as error:
        read(client, candidates=[candidate(), candidate(**bad)])
    assert error.value.status_code == 422 and adapter.sent == []


@pytest.mark.parametrize('course', [{}, {'courseId': '400', 'classId': '0'}, {'courseId': True, 'classId': '500'}])
def test_invalid_course_context_cannot_authorize_notification_lookups(course):
    with gateway([]) as (client, adapter, _), pytest.raises(upstream.UpstreamError) as error:
        read(client, course=course)
    assert error.value.code == 'invalid_course' and adapter.sent == []


def test_duplicate_notifications_are_verified_once_and_input_metadata_is_not_mutated():
    candidates = [candidate(url='https://third-party.invalid/sign'), candidate()]
    original = deepcopy(candidates)
    with gateway([{'body': PROFILE}, {'body': detail()}]) as (client, adapter, _):
        result = read(client, candidates=candidates)
    assert candidates == original and len(result['activities']) == 1 and len(adapter.sent) == 2
    assert result['sources'][0]['count'] == 1


def test_no_candidates_still_confirms_identity_and_returns_an_explicit_empty_source():
    with gateway([{'body': PROFILE}]) as (client, adapter, _):
        result = read(client, candidates=[])
    assert result['activities'] == [] and result['detailErrors'] == 0 and result['status'] == 'ok'
    assert result['sources'] == [{'id': 'notification', 'status': 'ok', 'count': 0}]
    assert len(adapter.sent) == 1


def test_a_confirmed_non_attendance_activity_is_omitted_without_becoming_a_detail_failure():
    with gateway([{'body': PROFILE}, {'body': detail(activeType=11)}]) as (client, adapter, _):
        result = read(client)
    assert result['activities'] == [] and result['detailErrors'] == 0 and result['status'] == 'ok'


@pytest.mark.parametrize('changed', [{'uid': '999'}, {'fid': '999'}])
def test_saved_identity_must_match_before_any_candidate_details(changed):
    with gateway([{'body': PROFILE}]) as (client, adapter, _), pytest.raises(upstream.UpstreamError) as error:
        read(client, identity={**IDENTITY, **changed})
    assert error.value.code == 'identity_mismatch' and len(adapter.sent) == 1


@pytest.mark.parametrize('failure', [
    {'status': 401}, {'body': '<input type="password">请先登录'},
    {'status': 302, 'headers': {'Location': 'https://passport2.chaoxing.com/login'}},
])
def test_expired_detail_session_propagates_and_stops_following_candidates(failure):
    with gateway([{'body': PROFILE}, failure]) as (client, adapter, _), pytest.raises(upstream.UpstreamError) as error:
        read(client, candidates=[candidate(), candidate(activeId='301')])
    assert error.value.code == 'session_expired' and len(adapter.sent) == 2


@pytest.mark.parametrize('failure', [
    {'status': 429, 'headers': {'Retry-After': '120'}},
    {'body': {'errorMsg': '请勿频繁操作', 'data': {}}},
])
def test_detail_rate_limit_propagates_without_list_scope_or_a_fake_empty_result(failure):
    with gateway([{'body': PROFILE}, failure]) as (client, adapter, _), pytest.raises(upstream.UpstreamError) as error:
        read(client, candidates=[candidate(), candidate(activeId='301')])
    assert error.value.code == 'upstream_rate_limited' and error.value.status_code == 429
    assert error.value.details['retryAfterSeconds'] in (120, 300)
    assert error.value.details.get('operation') != 'course_activity_list' and len(adapter.sent) == 2


def test_ended_notification_cache_skips_only_the_repeated_detail_and_returns_copies():
    ended = detail(nowTime=1_800_002_000_000)
    with gateway([{'body': PROFILE}, {'body': ended}, {'body': PROFILE}]) as (client, adapter, _):
        first = read(client)
        deadline = next(iter(client._ended_activities.values()))[0]
        first['activities'][0]['name'] = 'caller mutation'
        second = read(client)
        assert next(iter(client._ended_activities.values()))[0] == deadline
    assert second['activities'][0]['name'] == '通知标题'
    assert second['activities'][0]['notificationType'] == 'course_attendance'
    assert [urlsplit(r.url).path for r in adapter.sent].count(DETAIL_PATH) == 1


def test_a_normal_list_cached_row_does_not_replace_notification_detail_binding_checks():
    cached = {**candidate(), 'kind': 'ordinary', 'status': 'ended', 'requirements': [], 'autoEligible': False}
    del cached['notificationType']
    with gateway([{'body': PROFILE}, {'body': detail(clazzid=None)}]) as (client, adapter, _):
        client._ended_activity(IDENTITY, COURSE, candidate(), cached)
        result = read(client)
    assert result['detailErrors'] == 1 and result['activities'][0]['kind'] == 'unknown'
    assert [urlsplit(r.url).path for r in adapter.sent].count(DETAIL_PATH) == 1


@pytest.mark.parametrize('changed', [{'uid': '999'}, {'fid': '999'}])
def test_ended_notification_cache_isolated_by_account_identity(changed):
    ended = detail(nowTime=1_800_002_000_000)
    profile = {**PROFILE, 'msg': {**PROFILE['msg'], **changed}}
    with gateway([{'body': PROFILE}, {'body': ended}, {'body': profile}, {'body': ended}]) as (client, adapter, _):
        read(client)
        read(client, identity={**IDENTITY, **changed})
    assert [urlsplit(r.url).path for r in adapter.sent].count(DETAIL_PATH) == 2


def test_ended_notification_cache_isolated_by_course_and_class_and_has_finite_lifetime():
    ended = detail(nowTime=1_800_002_000_000)
    other = detail(courseId='401', clazzid='501', nowTime=1_800_002_000_000)
    with gateway([{'body': PROFILE}, {'body': ended}, {'body': PROFILE}, {'body': other},
                  {'body': PROFILE}, {'body': ended}]) as (client, adapter, _):
        read(client)
        result = read(client, course={**COURSE, 'courseId': '401', 'classId': '501'},
                      candidates=[candidate(courseId='401', classId='501')])
        assert result['activities'][0]['classId'] == '501'
        for key, (_, row) in list(client._ended_activities.items()):
            client._ended_activities[key] = (0, row)
        read(client)
    assert [urlsplit(r.url).path for r in adapter.sent].count(DETAIL_PATH) == 3


@pytest.mark.parametrize('source', ['normal', 'task'])
def test_only_the_exact_activity_list_paths_mark_a_rate_limit_as_list_scoped(source):
    prefix = [{'body': PROFILE}]
    if source == 'task':
        prefix.append({'body': {'data': {'activeList': []}}})
    with gateway([*prefix, {'status': 429}]) as (client, adapter, _), pytest.raises(upstream.UpstreamError) as error:
        client.get_course_activities(COOKIES, IDENTITY, COURSE)
    assert error.value.code == 'upstream_rate_limited'
    assert error.value.details == {'retryAfterSeconds': 300, 'operation': 'course_activity_list'}
    assert len(adapter.sent) == len(prefix) + 1
