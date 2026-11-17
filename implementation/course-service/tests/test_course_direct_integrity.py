"""Course direct-sign integrity against synthetic HTTP, never real accounts."""
from copy import deepcopy
import json
from urllib.parse import urlsplit

import pytest
from pydantic import ValidationError

from test_ordinary_http import gateway
from test_upstream import PROFILE, COOKIES, IDENTITY, SIGN, ORDINARY_DETAIL
from test_server import service, login, sign
from server import COOKIE, Sign
import upstream


SIGN_PATH = '/pptSign/stuSignajax'


def detail(kind='ordinary', **changes):
    return {'data': {**ORDINARY_DETAIL['data'], 'otherId': 3 if kind == 'gesture' else 0, **changes}}


def replies(kind='ordinary', *, activity=None, final='success'):
    return [{'body': PROFILE}, {'body': activity or detail(kind)},
            {'body': {'result': 0}}, {'body': final}]


@pytest.mark.parametrize('kind', ['ordinary', 'gesture'])
@pytest.mark.parametrize('missing', [('activeId',), ('clazzid',), ('activeId', 'clazzid', 'courseId')])
def test_missing_binding_fields_cannot_be_replaced_by_caller_context(kind, missing):
    activity = detail(kind)
    for key in missing:
        activity['data'].pop(key)
    with gateway(replies(kind, activity=activity)) as (client, adapter, _):
        result = client.sign(COOKIES, IDENTITY, {**SIGN, 'kind': kind})
    assert result['status'] == 'requires_manual' and result['submitted'] is False
    assert result['phase'] == 'activity' and len(adapter.sent) == 2


@pytest.mark.parametrize('kind', ['ordinary', 'gesture'])
@pytest.mark.parametrize('changed', [
    {'activeId': None}, {'activeId': ''}, {'activeId': True}, {'activeId': '301'},
    {'clazzid': None}, {'clazzid': ''}, {'clazzid': False}, {'clazzid': '501'},
    {'id': '301'}, {'classId': '501'},
])
def test_invalid_missing_or_conflicting_binding_aliases_never_reach_validation(kind, changed):
    with gateway(replies(kind, activity=detail(kind, **changed))) as (client, adapter, _):
        result = client.sign(COOKIES, IDENTITY, {**SIGN, 'kind': kind})
    assert result['status'] == 'requires_manual' and result['submitted'] is False
    assert len(adapter.sent) == 2


@pytest.mark.parametrize('kind', ['ordinary', 'gesture'])
def test_official_id_and_class_aliases_with_nullable_course_id_remain_supported(kind):
    activity = detail(kind, id=300, classId=500, courseId=None)
    del activity['data']['activeId']
    del activity['data']['clazzid']
    with gateway(replies(kind, activity=activity)) as (client, adapter, _):
        result = client.sign(COOKIES, IDENTITY, {**SIGN, 'kind': kind})
    assert result['status'] == 'reported_success' and result['submitted'] is True
    assert len(adapter.sent) == 4


@pytest.mark.parametrize('kind', ['ordinary', 'gesture'])
@pytest.mark.parametrize('stage', ['profile', 'activity'])
def test_read_redirect_cannot_visit_an_attendance_endpoint_or_hide_an_attempt(kind, stage):
    redirect = {'status': 302, 'headers': {'Location':
                'https://mobilelearn.chaoxing.com' + SIGN_PATH + '?activeId=300&uid=100&fid=200'}}
    initial = [] if stage == 'profile' else [{'body': PROFILE}]
    with gateway([*initial, redirect, {'body': 'success'}]) as (client, adapter, _):
        if stage == 'profile':
            with pytest.raises(upstream.UpstreamError) as error:
                client.sign(COOKIES, IDENTITY, {**SIGN, 'kind': kind})
            assert error.value.details == {'phase': 'profile', 'submitted': False}
        else:
            result = client.sign(COOKIES, IDENTITY, {**SIGN, 'kind': kind})
            assert result['status'] == 'requires_manual' and result['submitted'] is False
    assert len(adapter.sent) == len(initial) + 1
    assert all(urlsplit(r.url).path != SIGN_PATH for r in adapter.sent)


@pytest.mark.parametrize('stage', ['profile', 'activity'])
def test_expired_login_redirect_is_recognized_without_following_it(stage):
    initial = [] if stage == 'profile' else [{'body': PROFILE}]
    redirect = {'status': 302, 'headers': {'Location': 'https://passport2.chaoxing.com/login'}}
    with gateway([*initial, redirect, {'body': '<input type="password">请先登录'}]) as (client, adapter, _):
        with pytest.raises(upstream.UpstreamError) as error:
            client.sign(COOKIES, IDENTITY, SIGN)
    assert error.value.code == 'session_expired'
    assert error.value.details == {'phase': stage, 'submitted': False}
    assert len(adapter.sent) == len(initial) + 1


@pytest.mark.parametrize('source', ['normal', 'meet'])
def test_course_list_redirect_cannot_reach_a_sign_endpoint_before_enrollment_validation(source):
    redirect = {'status': 302, 'headers': {'Location':
                'https://mobilelearn.chaoxing.com' + SIGN_PATH + '?activeId=300&uid=100&fid=200'}}
    normal = redirect if source == 'normal' else {'body': {'channelList': []}}
    meet = {'body': {'data': {'listData': []}}} if source == 'normal' else redirect
    with gateway([{'body': PROFILE}, normal, meet, {'body': {'data': {'listData': []}}}]) as (client, adapter, _):
        result = client.get_account_details(COOKIES, IDENTITY)
    assert result['courses'] == []
    assert next(row for row in result['sources'] if row['id'] == source)['status'] == 'error'
    assert len(adapter.sent) == 3 and all(urlsplit(r.url).path != SIGN_PATH for r in adapter.sent)


@pytest.mark.parametrize(('limited', 'delay'), [
    ({'status': 429, 'body': 'private-rate-error'}, 300),
    ({'status': 429, 'headers': {'Retry-After': '120'}}, 120),
    ({'status': 429, 'headers': {'Retry-After': '0'}}, 0),
    ({'status': 429, 'headers': {'Retry-After': '-1'}}, 300),
    ({'status': 429, 'headers': {'Retry-After': '1.5'}}, 300),
    ({'status': 429, 'headers': {'Retry-After': 'Fri, 13 Nov 2026 12:00:00 GMT'}}, 300),
    ({'body': {'data': {'activeList': []}, 'errorMsg': '请勿频繁操作'}}, 300),
    ({'headers': {'Retry-After': '90'}, 'body': {'data': {'activeList': []}, 'msg': '请勿频繁操作'}}, 90),
])
def test_rate_limited_activity_sources_raise_instead_of_returning_successful_empty_lists(limited, delay):
    with gateway([{'body': PROFILE}, limited, {'body': {'activeList': []}}]) as (client, adapter, _):
        with pytest.raises(upstream.UpstreamError) as error:
            client.get_course_activities(COOKIES, IDENTITY, {'courseId': '400', 'classId': '500'})
    assert error.value.code == 'upstream_rate_limited' and error.value.status_code == 429
    assert error.value.details['retryAfterSeconds'] == delay
    assert 'private-rate-error' not in str(error.value)
    assert len(adapter.sent) == 2


def test_an_unrelated_message_containing_rate_limit_words_is_not_a_rate_limit_signal():
    first = {'data': {'activeList': []}, 'msg': '上一次提示请勿频繁操作，当前已恢复'}
    with gateway([{'body': PROFILE}, {'body': first}, {'body': {'activeList': []}}]) as (client, adapter, _):
        result = client.get_course_activities(COOKIES, IDENTITY, {'courseId': '400', 'classId': '500'})
    assert result['status'] == 'ok' and result['activities'] == [] and len(adapter.sent) == 3


@pytest.mark.parametrize('kind', ['ordinary', 'gesture'])
def test_detail_rate_limit_preserves_phase_not_submitted_and_retry_metadata(kind):
    limited = {'status': 429, 'headers': {'Retry-After': '45'}}
    with gateway([{'body': PROFILE}, limited]) as (client, adapter, _):
        with pytest.raises(upstream.UpstreamError) as error:
            client.sign(COOKIES, IDENTITY, {**SIGN, 'kind': kind})
    assert error.value.code == 'upstream_rate_limited' and error.value.status_code == 429
    assert error.value.details == {'retryAfterSeconds': 45, 'phase': 'activity', 'submitted': False}
    assert len(adapter.sent) == 2


@pytest.mark.parametrize('kind', ['ordinary', 'gesture'])
@pytest.mark.parametrize('changed', [
    {'jwCourseId': 900}, {'jwCourseId': '900'},
    {'name': '教务签到'}, {'nameOne': '教务签到'}, {'nameFour': '教务签到'},
    {'title': '教务签到'}, {'title1': '教务签到'}, {'title2': '教务签到'},
])
def test_teaching_affairs_detail_never_guesses_a_prerequisite_or_uses_the_simple_submit(kind, changed):
    with gateway(replies(kind, activity=detail(kind, **changed))) as (client, adapter, _):
        result = client.sign(COOKIES, IDENTITY, {**SIGN, 'kind': kind})
    assert result['status'] == 'requires_manual' and result['submitted'] is False
    assert result['phase'] == 'activity' and '教务' in result['message'] and '前置' in result['message']
    assert len(adapter.sent) == 2


@pytest.mark.parametrize('kind', ['ordinary', 'gesture'])
def test_internal_teaching_affairs_candidate_name_also_stops_before_validation(kind):
    with gateway(replies(kind)) as (client, adapter, _):
        result = client.sign(COOKIES, IDENTITY, {**SIGN, 'kind': kind, '_candidateName': '教务处签到'})
    assert result['status'] == 'requires_manual' and result['submitted'] is False
    assert '教务' in result['message'] and len(adapter.sent) == 2


def test_external_api_cannot_inject_the_internal_candidate_name():
    with pytest.raises(ValidationError):
        Sign.model_validate({**SIGN, '_candidateName': '普通签到'})


@pytest.mark.parametrize('kind', ['ordinary', 'gesture'])
@pytest.mark.parametrize('value', [None, '', 0, '0'])
def test_empty_teaching_affairs_id_does_not_block_an_otherwise_verified_activity(kind, value):
    with gateway(replies(kind, activity=detail(kind, jwCourseId=value))) as (client, adapter, _):
        result = client.sign(COOKIES, IDENTITY, {**SIGN, 'kind': kind})
    assert result['status'] == 'reported_success' and len(adapter.sent) == 4


@pytest.mark.parametrize('kind', ['ordinary', 'gesture'])
@pytest.mark.parametrize('body', ['private-final-error', 'validate', json.dumps({'result': 0, 'msg': 'private-final-error'}), 'x' * 70000],
                         ids=['unrecognized', 'validation', 'json-rejection', 'truncated'])
def test_both_course_kinds_retain_bounded_final_failure_diagnostics_without_public_error_text(kind, body):
    with gateway(replies(kind, final=body)) as (client, adapter, _):
        result = client.sign(COOKIES, IDENTITY, {**SIGN, 'kind': kind})
    assert result['status'] not in ('reported_success', 'already_signed')
    assert result['submitted'] is True and len(adapter.sent) == 4
    assert result['_diagnostic'] == {'endpoint': SIGN_PATH, 'httpStatus': 200,
                                     'body': body[:65536], 'bodyTruncated': len(body) > 65536}
    assert 'private-final-error' not in result['message'] and 'rawBody' not in result


@pytest.mark.parametrize('kind', ['ordinary', 'gesture'])
def test_successful_course_receipts_do_not_create_failure_diagnostics(kind):
    with gateway(replies(kind)) as (client, _, _):
        result = client.sign(COOKIES, IDENTITY, {**SIGN, 'kind': kind})
    assert result['status'] == 'reported_success' and '_diagnostic' not in result


def test_ordinary_diagnostic_is_encrypted_and_never_exposed_by_the_public_receipt(service):
    account_id, _ = login(service)
    service.gateway.get_account_details = lambda cookies, identity: {
        'profile': {'name': '测试同学'}, 'courses': [{'courseId': '456', 'classId': '789'}],
        'sources': [{'id': 'normal', 'status': 'ok'}], 'cookies': cookies}
    diagnostic = {'endpoint': SIGN_PATH, 'httpStatus': 200, 'body': 'private-ordinary-error', 'bodyTruncated': False}
    service.gateway.sign = lambda cookies, identity, payload: {
        'status': 'unknown', 'message': '普通签到结果尚未确认。', 'phase': 'submit',
        'submitted': True, '_diagnostic': deepcopy(diagnostic)}
    key = 'ordinary-integrity-diagnostic'
    result = sign(service, account_id, key=key)
    assert result.status_code == 200 and result.json()['status'] == 'unknown'
    replay = service.client.get('/api/sign-requests/' + key)
    assert replay.status_code == 200
    assert 'private-ordinary-error' not in result.text + replay.text
    token = service.client.cookies.get(COOKIE)
    vault = service.app.state.vault
    assert vault.get_diagnostic(token, key) == diagnostic
    with vault.connection() as db:
        stored = db.execute('SELECT ciphertext FROM sign_diagnostics WHERE owner=? AND request_id=?',
                            (vault.owner(token), key)).fetchone()
    assert b'private-ordinary-error' not in bytes(stored['ciphertext'])
