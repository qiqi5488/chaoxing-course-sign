"""Controlled gesture HTTP contract; no real account or network.

Synthetic success replies test routing and receipt handling only. They do not
establish that a live gesture activity accepts omission of the gesture code.
"""
from contextlib import contextmanager
from copy import deepcopy
from itertools import permutations
import json
from pathlib import Path
import sys
from unittest.mock import patch
from urllib.parse import parse_qs, urlsplit

import pytest
from pydantic import ValidationError
import requests

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from test_upstream import InMemoryAdapter, PROFILE, COOKIES, IDENTITY, SIGN, ORDINARY_DETAIL
from test_server import service, login, sign
from chaoxing_direct import ChaoxingDirectClient
from server import COOKIE, Sign
import upstream


GESTURE = {**SIGN, 'kind': 'gesture'}
GESTURE_DETAIL = {'data': {**ORDINARY_DETAIL['data'], 'otherId': 3, 'signCode': ''}}
PATHS = ['/apis/login/userLogin4Uname.do', '/v2/apis/active/getPPTActiveInfo',
         '/widget/sign/pcStuSignController/checkIfValidate', '/pptSign/stuSignajax']
LOGIN_PAGE = '<input type="password">请先登录'


@contextmanager
def gateway(responses):
    session = requests.Session()
    adapter = InMemoryAdapter(deepcopy(responses))
    # Mount both schemes so even incorrect routing cannot open a real socket.
    session.mount('https://', adapter)
    session.mount('http://', adapter)
    direct = ChaoxingDirectClient('offline-gesture-tests', session=session)
    with patch.object(upstream, 'ChaoxingDirectClient', return_value=direct), \
            patch.object(upstream, 'fetch_group_notifications') as notifications:
        yield upstream.UpstreamGateway(), adapter, notifications


def responses(*tail, detail=None):
    return [{'body': PROFILE}, {'body': GESTURE_DETAIL if detail is None else detail}, *tail]


@pytest.mark.parametrize('detail_code', ['', 'private-upstream-gesture'])
def test_controlled_gesture_uses_verified_course_direct_get_without_a_gesture_code(detail_code):
    detail = {'data': {**GESTURE_DETAIL['data'], 'signCode': detail_code}}
    with gateway(responses({'body': {'result': 0}}, {'body': 'success'}, detail=detail)) as (client, adapter, notices):
        result = client.sign(COOKIES, IDENTITY, GESTURE)
    assert result['status'] == 'reported_success' and result['submitted'] is True
    assert result['phase'] == 'submit'
    assert [urlsplit(r.url).path for r in adapter.sent] == PATHS
    assert all(r.method == 'GET' and r.body is None for r in adapter.sent)
    assert all(urlsplit(r.url).scheme == 'https' and urlsplit(r.url).hostname in
               ('sso.chaoxing.com', 'mobilelearn.chaoxing.com') for r in adapter.sent)
    query = parse_qs(urlsplit(adapter.sent[-1].url).query, keep_blank_values=True)
    assert query == {'activeId': ['300'], 'uid': ['100'], 'fid': ['200'],
                     'clientip': [''], 'latitude': ['-1'],
                     'longitude': ['-1'], 'appType': ['15'], 'name': [''],
                     'deviceCode': [''], 'validate': ['']}
    check = parse_qs(urlsplit(adapter.sent[2].url).query, keep_blank_values=True)
    assert check == {'DB_STRATEGY': ['PRIMARY_KEY'], 'STRATEGY_PARA': ['activeId'],
                     'activeId': ['300'], 'puid': ['']}
    assert not any(word in r.url for r in adapter.sent for word in
                   ('checkSignCode', 'preSign', '/analysis', 'getValidate', 'bfmzdx'))
    assert 'private-upstream-gesture' not in json.dumps(result)
    notices.assert_not_called()


@pytest.mark.parametrize('changed', [
    {'otherId': 0}, {'otherId': 2}, {'otherId': 5}, {'otherId': True},
    {'otherId': 'gesture'}, {'otherId': None}, {'activeType': 11},
    {'activeId': '301'}, {'courseId': '401'}, {'clazzid': '501'},
    {'activeId': True}, {'courseId': []}, {'clazzid': {}},
    {'nowTime': None}, {'starttime': 1_800_002_000_000}, {'endtime': None},
])
def test_gesture_rejects_other_types_unbound_ids_and_unknown_or_future_windows(changed):
    detail = {'data': {**GESTURE_DETAIL['data'], **changed}}
    with gateway(responses(detail=detail)) as (client, adapter, _):
        result = client.sign(COOKIES, IDENTITY, GESTURE)
    assert result['status'] == 'requires_manual' and result['submitted'] is False
    assert result['phase'] == 'activity' and len(adapter.sent) == 2


@pytest.mark.parametrize('field', ['ifphoto', 'ifopenAddress', 'openCheckFaceFlag'])
@pytest.mark.parametrize('value', [1, 2, -1, True, False, None, '', [], {}])
def test_gesture_requires_each_extra_rule_to_be_an_explicit_numeric_zero(field, value):
    detail = {'data': {**GESTURE_DETAIL['data'], field: value}}
    with gateway(responses(detail=detail)) as (client, adapter, _):
        result = client.sign(COOKIES, IDENTITY, GESTURE)
    assert result['status'] == 'requires_manual' and result['submitted'] is False
    assert len(adapter.sent) == 2


def test_numeric_string_rule_flags_follow_the_existing_numeric_protocol():
    detail = {'data': {**GESTURE_DETAIL['data'], 'otherId': '3', 'ifphoto': '0',
                       'ifopenAddress': '0', 'openCheckFaceFlag': '0'}}
    with gateway(responses({'body': {'result': 0}}, {'body': 'success'}, detail=detail)) as (client, adapter, _):
        result = client.sign(COOKIES, IDENTITY, GESTURE)
    assert result['status'] == 'reported_success' and len(adapter.sent) == 4


def test_ended_gesture_stops_before_validation_or_submission():
    detail = {'data': {**GESTURE_DETAIL['data'], 'nowTime': 1_800_001_000_000}}
    with gateway(responses(detail=detail)) as (client, adapter, _):
        result = client.sign(COOKIES, IDENTITY, GESTURE)
    assert result['status'] == 'rejected' and result['submitted'] is False
    assert result['phase'] == 'activity' and len(adapter.sent) == 2


@pytest.mark.parametrize('changed_identity', [{'uid': '999'}, {'fid': '999'}, {'uid': True}, {'fid': False}])
def test_gesture_saved_identity_mismatch_or_invalid_type_stops_before_activity(changed_identity):
    with gateway([{'body': PROFILE}]) as (client, adapter, notices), pytest.raises(upstream.UpstreamError) as error:
        client.sign(COOKIES, {**IDENTITY, **changed_identity}, GESTURE)
    assert error.value.code == 'identity_mismatch'
    assert error.value.details == {'phase': 'profile', 'submitted': False}
    assert len(adapter.sent) == 1
    notices.assert_not_called()


@pytest.mark.parametrize(('validation', 'expected'), [
    ({'result': 1}, 'requires_validation'), ({'result': '1'}, 'requires_validation'),
    ({}, 'requires_manual'), ({'success': True}, 'requires_manual'),
    ({'result': -1, 'msg': 'private-validation'}, 'requires_manual'),
    ('private-not-json', 'requires_manual'),
])
def test_gesture_challenge_or_unknown_validation_does_not_submit(validation, expected):
    with gateway(responses({'body': validation})) as (client, adapter, _):
        result = client.sign(COOKIES, IDENTITY, GESTURE)
    assert result['status'] == expected and result['submitted'] is False
    assert result['phase'] == 'validation' and len(adapter.sent) == 3
    assert 'private-' not in json.dumps(result)


@pytest.mark.parametrize(('phase', 'replies'), [
    ('profile', [requests.ReadTimeout('private-timeout')]),
    ('activity', [{'body': PROFILE}, requests.ReadTimeout('private-timeout')]),
    ('validation', responses(requests.ReadTimeout('private-timeout'))),
])
def test_gesture_read_timeout_before_submit_never_retries(phase, replies):
    with gateway(replies) as (client, adapter, _), pytest.raises(upstream.UpstreamError) as error:
        client.sign(COOKIES, IDENTITY, GESTURE)
    assert error.value.details['phase'] == phase and error.value.details['submitted'] is False
    assert len(adapter.sent) == len(replies)
    assert 'private-timeout' not in str(error.value)


@pytest.mark.parametrize('failure', [requests.ReadTimeout('private-ticket'), requests.ConnectionError('private-ticket')])
def test_gesture_submit_transport_failure_is_unknown_and_not_retried(failure):
    with gateway(responses({'body': {'result': 0}}, failure)) as (client, adapter, _):
        result = client.sign(COOKIES, IDENTITY, GESTURE)
    assert result['status'] == 'outcome_unknown' and result['submitted'] is True
    assert result['phase'] == 'submit' and len(adapter.sent) == 4
    assert 'private-ticket' not in json.dumps(result)


@pytest.mark.parametrize(('reply', 'expected'), [
    ({'body': 'success'}, 'reported_success'),
    ({'body': '已签到'}, 'already_signed'),
    ({'body': 'validate'}, 'requires_validation'),
    ({'body': {'result': 0, 'msg': 'private-gesture-error'}}, 'rejected'),
    ({'body': '<script>"success"</script>'}, 'unknown'),
    ({'body': 'unsuccessful'}, 'unknown'),
    ({'status': 302, 'headers': {'Location': 'https://third-party.invalid/sign'}, 'body': 'success'}, 'http_error'),
])
def test_gesture_only_uses_strict_official_receipt_and_does_not_follow_redirects(reply, expected):
    with gateway(responses({'body': {'result': 0}}, reply)) as (client, adapter, _):
        result = client.sign(COOKIES, IDENTITY, GESTURE)
    assert result['status'] == expected and result['submitted'] is True
    assert len(adapter.sent) == 4 and 'private-gesture-error' not in result['message']
    assert 'rawBody' not in result
    if expected not in ('reported_success', 'already_signed'):
        body = reply.get('body', '')
        if isinstance(body, (dict, list)):
            body = json.dumps(body)
        assert result['_diagnostic'] == {'endpoint': '/pptSign/stuSignajax',
                                        'httpStatus': reply.get('status', 200),
                                        'body': body, 'bodyTruncated': False}


@pytest.mark.parametrize(('stage', 'replies', 'submitted'), [
    ('validation', responses({'body': LOGIN_PAGE}), False),
    ('submit', responses({'body': {'result': 0}}, {'body': LOGIN_PAGE}), True),
])
def test_gesture_login_expiry_preserves_before_after_submit_distinction(stage, replies, submitted):
    with gateway(replies) as (client, adapter, _), pytest.raises(upstream.UpstreamError) as error:
        client.sign(COOKIES, IDENTITY, GESTURE)
    assert error.value.code == 'session_expired'
    assert error.value.details == {'phase': stage, 'submitted': submitted}
    assert len(adapter.sent) == len(replies)


def test_group_code_is_rejected_before_any_upstream_or_notifications():
    with gateway([]) as (client, adapter, notices), pytest.raises(upstream.UpstreamError) as error:
        client.sign(COOKIES, IDENTITY, {'activityScope': 'group', 'kind': 'code', 'activeId': '300'})
    assert error.value.code == 'unsupported_sign_kind'
    assert adapter.sent == []
    notices.assert_not_called()


@pytest.mark.parametrize('extra', [
    {'signCode': '1236'}, {'enc': 'synthetic-qr'},
    {'location': {'latitude': 30, 'longitude': 120, 'address': '现场'}},
    {'photo_payload': {'objectId': 'synthetic-photo', 'photoResult': 'synthetic-result'}},
    {'face_fields': {'faceEnc': 'synthetic-face'}},
    {'challenge': {'enc2': 'synthetic-challenge', 'validate': 'synthetic-validate'}},
    {'validate': 'synthetic-validate'}, {'ifopenAddress': '1'}, {'faceEncAid': 'synthetic-face-id'},
])
def test_gesture_api_rejects_credentials_from_other_request_families(extra):
    with pytest.raises(ValidationError) as error:
        Sign.model_validate({**GESTURE, **extra})
    # Each value above is valid in its own request family; the gesture gate rejects it.
    assert any(item['loc'] == () for item in error.value.errors())


def test_gesture_api_requires_course_context_only_in_course_scope():
    assert Sign.model_validate(GESTURE).kind == 'gesture'
    assert Sign.model_validate({'kind': 'gesture', 'activeId': '300', 'activityScope': 'group'}).activityScope == 'group'
    for payload in ({'kind': 'gesture', 'activeId': '300'},):
        with pytest.raises(ValidationError):
            Sign.model_validate(payload)


@pytest.mark.parametrize(('first_kind', 'next_kind'), list(permutations(['ordinary', 'code', 'gesture'], 2)))
def test_course_activity_duplicate_guard_is_shared_across_all_three_kinds(service, first_kind, next_kind):
    account_id, _ = login(service)

    def payload(kind):
        value = {'kind': kind, 'activeId': '123', 'courseId': '456', 'classId': '789'}
        if kind == 'code':
            value['signCode'] = '1236'
        return value

    first = sign(service, account_id, key='gesture-first-request', payload=payload(first_kind))
    assert first.status_code == 200 and first.json()['status'] == 'reported_success'
    second = sign(service, account_id, key='gesture-next-request', payload=payload(next_kind))
    assert second.status_code == 409
    assert second.json()['error']['code'] == 'activity_already_attempted'
    assert service.gateway.sign_calls == 1


def test_uncertain_gesture_also_blocks_a_new_ordinary_attempt(service):
    account_id, _ = login(service)
    service.gateway.sign_status = 'outcome_unknown'
    payload = {'kind': 'gesture', 'activeId': '123', 'courseId': '456', 'classId': '789'}
    first = sign(service, account_id, key='gesture-uncertain-request', payload=payload)
    assert first.status_code == 200 and first.json()['status'] == 'outcome_unknown'
    second = sign(service, account_id, key='ordinary-after-gesture')
    assert second.status_code == 409
    assert second.json()['error']['code'] == 'activity_already_attempted'
    assert service.gateway.sign_calls == 1


def test_gesture_diagnostic_is_encrypted_and_absent_from_public_job_and_replay(service):
    account_id, _ = login(service)
    diagnostic = {'endpoint': '/pptSign/stuSignajax', 'httpStatus': 200,
                  'body': 'private-gesture-error', 'bodyTruncated': False}
    service.gateway.sign = lambda cookies, identity, payload: {
        'status': 'rejected', 'message': '学习通拒绝了这次手势签到。',
        'phase': 'submit', 'submitted': True, '_diagnostic': diagnostic}
    key = 'gesture-private-diagnostic'
    payload = {'kind': 'gesture', 'activeId': '123', 'courseId': '456', 'classId': '789'}
    result = sign(service, account_id, key=key, payload=payload)
    assert result.status_code == 200 and result.json()['status'] == 'rejected'
    replay = service.client.get('/api/sign-requests/' + key)
    assert replay.status_code == 200
    for response in (result, replay):
        assert 'private-gesture-error' not in response.text and '_diagnostic' not in response.json()
    vault = service.app.state.vault
    token = service.client.cookies.get(COOKIE)
    assert vault.get_diagnostic(token, key) == diagnostic
    with vault.connection() as db:
        stored = db.execute('SELECT ciphertext FROM sign_diagnostics WHERE owner=? AND request_id=?',
                            (vault.owner(token), key)).fetchone()
    assert b'private-gesture-error' not in bytes(stored['ciphertext'])
