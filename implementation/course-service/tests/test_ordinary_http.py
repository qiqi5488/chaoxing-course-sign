"""Actual ordinary adapters against synthetic HTTPS; no real account or network."""
from contextlib import contextmanager
from copy import deepcopy
import json
from pathlib import Path
import sys
from unittest.mock import patch
from urllib.parse import parse_qs, urlsplit

import pytest
import requests

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from test_upstream import InMemoryAdapter, PROFILE, COOKIES, IDENTITY, SIGN, ORDINARY_DETAIL
import upstream
from chaoxing_direct import ChaoxingDirectClient
from group_notifications import GroupNotificationError


GROUP_SIGN = {'activityScope': 'group', 'kind': 'ordinary', 'activeId': '300'}
GROUP_ACTIVITY = {'activeId': '300', 'kind': 'ordinary_candidate'}
GROUP_CONTEXT = '<input id="activeId" value="300"><input id="puid" value="101">'
GROUP_SUCCESS = GROUP_CONTEXT + '<div class="qd_Success">签到成功</div>'
LOGIN_PAGE = '<input type="password">请先登录'


@contextmanager
def gateway(responses, *, activities=None, notification_error=None):
    session = requests.Session()
    adapter = InMemoryAdapter(deepcopy(responses))
    # Even a regression to a different host/scheme cannot open a real socket.
    session.mount('https://', adapter)
    session.mount('http://', adapter)
    direct = ChaoxingDirectClient('offline-ordinary-tests', session=session)
    with patch.object(upstream, 'ChaoxingDirectClient', return_value=direct), \
            patch.object(upstream, 'fetch_group_notifications',
                         return_value={'activities': deepcopy(activities if activities is not None else [GROUP_ACTIVITY])},
                         side_effect=notification_error) as notifications:
        yield upstream.UpstreamGateway(), adapter, notifications


def course_responses(*tail, detail=None):
    return [{'body': PROFILE}, {'body': ORDINARY_DETAIL if detail is None else detail}, *tail]


@pytest.mark.parametrize('automatic', [False, True])
def test_course_ordinary_verifies_rules_validation_and_submits_direct_get(automatic):
    with gateway(course_responses({'body': {'result': 0}}, {'body': 'success'})) as (client, adapter, notifications):
        result = client.sign(COOKIES, IDENTITY, {**SIGN, '_autoSign': automatic})
    assert result['status'] == 'reported_success' and result['submitted'] is True
    assert [urlsplit(r.url).path for r in adapter.sent] == [
        '/apis/login/userLogin4Uname.do', '/v2/apis/active/getPPTActiveInfo',
        '/widget/sign/pcStuSignController/checkIfValidate', '/pptSign/stuSignajax']
    assert all(r.method == 'GET' and r.body is None for r in adapter.sent)
    assert all(urlsplit(r.url).hostname in ('sso.chaoxing.com', 'mobilelearn.chaoxing.com') for r in adapter.sent)
    check = parse_qs(urlsplit(adapter.sent[2].url).query, keep_blank_values=True)
    assert check == {'DB_STRATEGY': ['PRIMARY_KEY'], 'STRATEGY_PARA': ['activeId'],
                     'activeId': ['300'], 'puid': ['']}
    submit = parse_qs(urlsplit(adapter.sent[-1].url).query, keep_blank_values=True)
    assert submit['uid'] == ['100'] and submit['fid'] == ['200']
    assert submit['uid'] != [str(PROFILE['msg']['puid'])]
    assert not {'enc', 'enc2', 'Code', 'rcode', 'ewnCode', 'photoResult'} & submit.keys()
    assert not any('preSign' in r.url or '/analysis' in r.url for r in adapter.sent)
    notifications.assert_not_called()


@pytest.mark.parametrize(('validation', 'expected'), [
    ({'result': 1}, 'requires_validation'), ({'result': '1'}, 'requires_validation'),
    ({}, 'requires_manual'), ({'success': True}, 'requires_manual'),
    ({'result': -1, 'error': 'private-upstream-data'}, 'requires_manual'),
    ('not-json private-upstream-data', 'requires_manual'),
])
def test_validation_challenge_or_unknown_schema_never_reaches_course_submit(validation, expected):
    with gateway(course_responses({'body': validation})) as (client, adapter, _):
        result = client.sign(COOKIES, IDENTITY, SIGN)
    assert result['status'] == expected and result['submitted'] is False
    assert result['phase'] == 'validation' and len(adapter.sent) == 3
    assert 'private' not in json.dumps(result)


@pytest.mark.parametrize('changed', [
    {'otherId': 2}, {'otherId': 3}, {'otherId': 5}, {'ifphoto': 1},
    {'ifopenAddress': 1}, {'openCheckFaceFlag': 1}, {'ifphoto': None},
    {'activeId': '301'}, {'courseId': '401'}, {'clazzid': '501'},
    {'activeType': 11}, {'nowTime': None}, {'starttime': 1_800_002_000_000},
])
@pytest.mark.parametrize('automatic', [False, True])
def test_manual_and_automatic_course_entry_both_reject_unconfirmed_plain_rules(changed, automatic):
    detail = {'data': {**ORDINARY_DETAIL['data'], **changed}}
    with gateway(course_responses(detail=detail)) as (client, adapter, _):
        result = client.sign(COOKIES, IDENTITY, {**SIGN, '_autoSign': automatic})
    assert result['status'] == 'requires_manual' and result['submitted'] is False
    assert result['phase'] == 'activity' and len(adapter.sent) == 2


def test_ended_course_is_rejected_before_validation_or_submission():
    detail = {'data': {**ORDINARY_DETAIL['data'], 'nowTime': 1_800_001_000_000}}
    with gateway(course_responses(detail=detail)) as (client, adapter, _):
        result = client.sign(COOKIES, IDENTITY, SIGN)
    assert result['status'] == 'rejected' and result['submitted'] is False
    assert len(adapter.sent) == 2


@pytest.mark.parametrize('scope', ['course', 'group'])
@pytest.mark.parametrize('changed_identity', [{'uid': '999'}, {'fid': '999'}])
def test_profile_mismatch_stops_before_activity_sources_or_attendance(scope, changed_identity):
    with gateway([{'body': PROFILE}]) as (client, adapter, notifications), pytest.raises(upstream.UpstreamError) as error:
        client.sign(COOKIES, {**IDENTITY, **changed_identity}, GROUP_SIGN if scope == 'group' else SIGN)
    assert error.value.code == 'identity_mismatch'
    assert error.value.details == {'phase': 'profile', 'submitted': False}
    assert len(adapter.sent) == 1
    notifications.assert_not_called()


@pytest.mark.parametrize(('phase', 'responses'), [
    ('activity', [{'body': PROFILE}, requests.ReadTimeout('private-timeout')]),
    ('validation', course_responses(requests.ReadTimeout('private-timeout'))),
])
def test_read_timeout_before_course_submit_does_not_retry_or_claim_attempt(phase, responses):
    with gateway(responses) as (client, adapter, _), pytest.raises(upstream.UpstreamError) as error:
        client.sign(COOKIES, IDENTITY, SIGN)
    assert error.value.details['phase'] == phase and error.value.details['submitted'] is False
    assert len(adapter.sent) == len(responses)
    assert 'private-timeout' not in str(error.value)


@pytest.mark.parametrize('failure', [requests.ReadTimeout('private-cookie'), requests.ConnectionError('private-cookie')])
def test_course_submit_transport_failure_is_unknown_without_retry(failure):
    with gateway(course_responses({'body': {'result': 0}}, failure)) as (client, adapter, _):
        result = client.sign(COOKIES, IDENTITY, SIGN)
    assert result['status'] == 'outcome_unknown' and result['submitted'] is True
    assert result['phase'] == 'submit' and len(adapter.sent) == 4
    assert 'private-cookie' not in json.dumps(result)


@pytest.mark.parametrize(('stage', 'responses', 'submitted'), [
    ('validation', course_responses({'body': LOGIN_PAGE}), False),
    ('submit', course_responses({'body': {'result': 0}}, {'body': LOGIN_PAGE}), True),
])
def test_course_login_page_keeps_before_after_submit_distinction(stage, responses, submitted):
    with gateway(responses) as (client, adapter, _), pytest.raises(upstream.UpstreamError) as error:
        client.sign(COOKIES, IDENTITY, SIGN)
    assert error.value.code == 'session_expired'
    assert error.value.details == {'phase': stage, 'submitted': submitted}
    assert len(adapter.sent) == len(responses)


def test_group_ordinary_uses_own_notifications_and_fixed_entry_without_qr_or_course_fields():
    with gateway([{'body': PROFILE}, {'body': GROUP_SUCCESS}]) as (client, adapter, notifications):
        result = client.sign(COOKIES, IDENTITY, GROUP_SIGN)
    assert result['status'] == 'reported_success' and result['reasonCode'] == 'record_signed'
    assert result['submitted'] is True and result['phase'] == 'group_entry'
    assert notifications.call_args.args[1] == '101'  # Verified passport puid.
    assert len(adapter.sent) == 2
    request = adapter.sent[-1]
    assert request.method == 'GET' and request.body is None
    assert request.url == 'https://mobilelearn.chaoxing.com/widget/sign/group/pcStuSignGroupController/preSign?activeId=300'
    assert parse_qs(urlsplit(request.url).query) == {'activeId': ['300']}
    assert 'rawBody' not in result and '_diagnostic' not in result


@pytest.mark.parametrize('activities', [[], [{**GROUP_ACTIVITY, 'activeId': '301'}],
    [{**GROUP_ACTIVITY, 'kind': 'qr_candidate'}], [{**GROUP_ACTIVITY, 'kind': 'unknown'}],
    [GROUP_ACTIVITY, GROUP_ACTIVITY]])
def test_group_requires_single_own_plain_candidate_before_entering_sign_page(activities):
    with gateway([{'body': PROFILE}], activities=activities) as (client, adapter, _):
        result = client.sign(COOKIES, IDENTITY, GROUP_SIGN)
    assert result['status'] == 'requires_manual' and result['submitted'] is False
    assert len(adapter.sent) == 1


def test_group_source_failure_stays_before_submission():
    error = GroupNotificationError('group_source_unavailable', '官方群来源暂不可用。')
    with gateway([{'body': PROFILE}], notification_error=error) as (client, adapter, _), pytest.raises(upstream.UpstreamError) as captured:
        client.sign(COOKIES, IDENTITY, GROUP_SIGN)
    assert captured.value.code == 'group_source_unavailable'
    assert captured.value.details == {'phase': 'group_activity', 'submitted': False}
    assert len(adapter.sent) == 1


@pytest.mark.parametrize(('body', 'status', 'reason', 'submitted'), [
    (GROUP_SUCCESS.replace('value="101"', 'value="999"'), 'unknown', 'context_mismatch', True),
    ('<div class="qd_Success">签到成功</div>', 'need_context', 'page_identity_missing', True),
    (GROUP_CONTEXT + '<form><button>签到</button></form>', 'need_context', 'group_form_pending', False),
    ('<h1>签到活动已结束</h1>', 'rejected', 'activity_ended', True),
])
def test_group_result_requires_correct_identity_and_visible_completion(body, status, reason, submitted):
    with gateway([{'body': PROFILE}, {'body': body}]) as (client, adapter, _):
        result = client.sign(COOKIES, IDENTITY, GROUP_SIGN)
    assert (result['status'], result['reasonCode'], result['submitted']) == (status, reason, submitted)
    assert len(adapter.sent) == 2


def test_group_entry_login_expiry_is_uncertain_after_entry():
    with gateway([{'body': PROFILE}, {'body': LOGIN_PAGE}]) as (client, adapter, _), pytest.raises(upstream.UpstreamError) as error:
        client.sign(COOKIES, IDENTITY, GROUP_SIGN)
    assert error.value.code == 'session_expired'
    assert error.value.details == {'phase': 'group_entry', 'submitted': True}
    assert len(adapter.sent) == 2


def test_group_entry_timeout_is_unknown_without_retry():
    with gateway([{'body': PROFILE}, requests.ReadTimeout('private-token')]) as (client, adapter, _):
        result = client.sign(COOKIES, IDENTITY, GROUP_SIGN)
    assert result['status'] == 'outcome_unknown' and result['submitted'] is True
    assert len(adapter.sent) == 2 and 'private-token' not in json.dumps(result)


@pytest.mark.parametrize('scope', ['course', 'group'])
def test_submission_redirect_is_not_followed_or_repeated(scope):
    redirect = {'status': 302, 'headers': {'Location': 'https://mobilelearn.chaoxing.com/newsign/studentSign'}}
    responses = [{'body': PROFILE}, redirect] if scope == 'group' else course_responses({'body': {'result': 0}}, redirect)
    with gateway(responses) as (client, adapter, _):
        result = client.sign(COOKIES, IDENTITY, GROUP_SIGN if scope == 'group' else SIGN)
    assert result['status'] == 'http_error' and result['submitted'] is True
    assert len(adapter.sent) == len(responses)
