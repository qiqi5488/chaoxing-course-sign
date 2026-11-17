"""Controlled group QR direct-entry contract; no real account or network.

The fixed/dynamic fixture tags below are synthetic test metadata. They prove
the routing contract is independent of an input QR image, not that either
refresh mode has been accepted by a live platform activity.
"""
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
from test_upstream import InMemoryAdapter, PROFILE, COOKIES, IDENTITY, SIGN, PRESIGN
import upstream
from chaoxing_direct import ChaoxingDirectClient
from group_notifications import GroupNotificationError, _activity


GROUP_QR = {'activityScope': 'group', 'kind': 'qr', 'activeId': '300'}
GROUP_PATH = '/widget/sign/group/pcStuSignGroupController/preSign'
CONTEXT = '<input id="activeId" value="300"><input id="puid" value="101">'
SUCCESS = CONTEXT + '<div class="qd_Success">签到成功</div>'


def notification(label='二维码签到', *, title='自定义活动标题', active_id='300', url=None):
    """Use the actual pure notification parser; titles never select the kind."""
    return _activity({'attachmentType': 15, 'att_chat_course': {
        'aid': active_id, 'atype': 2, 'type': 1, 'atypeName': label, 'title': title,
        'url': url or 'https://mobilelearn.chaoxing.com/sign/preSign?activeId='
               + active_id + '&chatId=900&courseId=0&general=1&ls=1',
    }})


@contextmanager
def gateway(responses, *, activities=None, notification_error=None):
    session = requests.Session()
    adapter = InMemoryAdapter(deepcopy(responses))
    session.mount('https://', adapter)
    session.mount('http://', adapter)
    direct = ChaoxingDirectClient('offline-direct-group-qr', session=session)
    notices = [notification()] if activities is None else activities
    with patch.object(upstream, 'ChaoxingDirectClient', return_value=direct), \
            patch.object(upstream, 'fetch_group_notifications',
                         return_value={'activities': deepcopy(notices)},
                         side_effect=notification_error) as fetch:
        yield upstream.UpstreamGateway(), adapter, fetch


@pytest.fixture(params=['fixed', 'dynamic'], ids=['controlled-fixed', 'controlled-dynamic'])
def controlled_qr_notice(request):
    notice = notification()
    assert notice and notice['kind'] == 'qr' and notice['activityScope'] == 'group'
    notice['_controlled_refresh_fixture'] = request.param
    return notice


@pytest.mark.parametrize('automatic', [False, True])
def test_controlled_group_qr_needs_only_activity_id_and_no_scanned_payload(controlled_qr_notice, automatic):
    with gateway([{'body': PROFILE}, {'body': SUCCESS}], activities=[controlled_qr_notice]) as (client, adapter, fetch):
        result = client.sign(COOKIES, IDENTITY, {**GROUP_QR, '_autoSign': automatic})
    assert result['status'] == 'reported_success' and result['reasonCode'] == 'record_signed'
    assert result['submitted'] is True
    assert fetch.call_count == 1 and fetch.call_args.args[1] == '101'
    assert [urlsplit(r.url).path for r in adapter.sent] == ['/apis/login/userLogin4Uname.do', GROUP_PATH]
    assert all(r.method == 'GET' and r.body is None for r in adapter.sent)
    entry = adapter.sent[-1]
    assert urlsplit(entry.url).scheme == 'https' and urlsplit(entry.url).hostname == 'mobilelearn.chaoxing.com'
    assert parse_qs(urlsplit(entry.url).query, keep_blank_values=True) == {'activeId': ['300']}
    assert not any('/newsign/studentSign' in r.url or '/pptSign/stuSignajax' in r.url for r in adapter.sent)
    assert not any(field in entry.url for field in ('QRCode', 'Code=', 'enc=', 'rcode=', 'courseId=', 'classId='))
    assert '_diagnostic' not in result


@pytest.mark.parametrize(('label', 'title', 'permitted'), [
    ('二维码签到', '普通签到', True),
    ('普通签到', '二维码签到', False),
    ('签到', '二维码签到', False),
    ('未知类型', '二维码签到', False),
])
def test_canonical_platform_label_selects_group_qr_not_user_supplied_title(label, title, permitted):
    responses = [{'body': PROFILE}] + ([{'body': SUCCESS}] if permitted else [])
    with gateway(responses, activities=[notification(label, title=title)]) as (client, adapter, _):
        result = client.sign(COOKIES, IDENTITY, GROUP_QR)
    assert result['status'] == ('reported_success' if permitted else 'requires_manual')
    assert result['submitted'] is permitted
    assert len(adapter.sent) == (2 if permitted else 1)


@pytest.mark.parametrize('activities', [[], [notification(active_id='301')],
    [notification(), notification()], [notification('普通签到')]])
def test_only_a_single_matching_activity_in_own_notifications_can_enter_group_qr(activities):
    with gateway([{'body': PROFILE}], activities=activities) as (client, adapter, _):
        result = client.sign(COOKIES, IDENTITY, GROUP_QR)
    assert result['status'] == 'requires_manual' and result['submitted'] is False
    assert len(adapter.sent) == 1


@pytest.mark.parametrize('kind,label', [('qr', '二维码签到'), ('gesture', '手势签到')])
@pytest.mark.parametrize('automatic', [False, True])
def test_photo_requirement_returns_explicit_unsubmitted_result_without_followup(kind, label, automatic):
    photo = CONTEXT + '<input id="activeStatus" value="1">' + (
        '<div class="qd_Success padtop50"><p class="Photo_p">此签到需要手机拍照</p>'
        '<p class="Photo_p2">请使用手机App<br>进行签到</p></div>')
    with gateway([{'body': PROFILE}, {'body': photo}], activities=[notification(label)]) as (client, adapter, _):
        result = client.sign(COOKIES, IDENTITY, {
            'activityScope': 'group', 'kind': kind, 'activeId': '300', '_autoSign': automatic})
    assert result['status'] == result['reasonCode'] == 'photo_required'
    assert result['submitted'] is False and result['phase'] == 'group_entry'
    assert '需要照片' in result['message']
    assert [urlsplit(request.url).path for request in adapter.sent] == [
        '/apis/login/userLogin4Uname.do', GROUP_PATH]
    assert result['_diagnostic']['body'] == photo


def test_course_notification_with_qr_label_is_not_a_group_qr_candidate():
    note = notification(url='https://mobilelearn.chaoxing.com/newsign/preSign?activePrimaryId=300&courseId=400&classId=500')
    assert note is None
    with gateway([{'body': PROFILE}], activities=[]) as (client, adapter, _):
        result = client.sign(COOKIES, IDENTITY, GROUP_QR)
    assert result['status'] == 'requires_manual' and result['submitted'] is False
    assert len(adapter.sent) == 1


@pytest.mark.parametrize('identity_change', [{'uid': '999'}, {'fid': '999'}])
def test_group_qr_identity_mismatch_stops_before_notifications_or_entry(identity_change):
    with gateway([{'body': PROFILE}]) as (client, adapter, fetch), pytest.raises(upstream.UpstreamError) as error:
        client.sign(COOKIES, {**IDENTITY, **identity_change}, GROUP_QR)
    assert error.value.code == 'identity_mismatch' and error.value.details['submitted'] is False
    assert len(adapter.sent) == 1
    fetch.assert_not_called()


def test_group_qr_source_error_does_not_submit_or_replace_it_with_scanning():
    failure = GroupNotificationError('group_source_unavailable', '官方群来源暂不可用。')
    with gateway([{'body': PROFILE}], notification_error=failure) as (client, adapter, _), pytest.raises(upstream.UpstreamError) as error:
        client.sign(COOKIES, IDENTITY, GROUP_QR)
    assert error.value.code == 'group_source_unavailable' and error.value.details['submitted'] is False
    assert len(adapter.sent) == 1


@pytest.mark.parametrize(('body', 'status', 'reason'), [
    (SUCCESS.replace('value="300"', 'value="301"'), 'unknown', 'context_mismatch'),
    (SUCCESS.replace('value="101"', 'value="100"'), 'unknown', 'context_mismatch'),
    ('<div class="qd_Success">签到成功</div>', 'need_context', 'page_identity_missing'),
    (CONTEXT + '<script>"qd_Success 签到成功"</script>', 'need_context', 'group_form_pending'),
    (CONTEXT + '<div hidden class="qd_Success">签到成功</div>', 'need_context', 'group_form_pending'),
    (SUCCESS + '<h1>签到失败</h1>', 'unknown', 'conflicting_result'),
])
def test_group_qr_never_reports_success_without_bound_visible_completion(body, status, reason):
    with gateway([{'body': PROFILE}, {'body': body}]) as (client, adapter, _):
        result = client.sign(COOKIES, IDENTITY, GROUP_QR)
    assert (result['status'], result['reasonCode']) == (status, reason)
    assert len(adapter.sent) == 2


@pytest.mark.parametrize('failure', [requests.ReadTimeout('private-token'), requests.ConnectionError('private-token')])
def test_group_qr_entry_transport_failure_is_uncertain_without_retry(failure):
    with gateway([{'body': PROFILE}, failure]) as (client, adapter, _):
        result = client.sign(COOKIES, IDENTITY, GROUP_QR)
    assert result['status'] == 'outcome_unknown' and result['submitted'] is True
    assert len(adapter.sent) == 2
    assert 'private-token' not in json.dumps(result)


def test_group_qr_entry_redirect_is_not_followed_into_native_qr_or_other_submission():
    redirect = {'status': 302, 'headers': {
        'Location': 'https://mobilelearn.chaoxing.com/newsign/studentSign?rcode=private-ticket'},
        'body': SUCCESS}
    with gateway([{'body': PROFILE}, redirect]) as (client, adapter, _):
        result = client.sign(COOKIES, IDENTITY, GROUP_QR)
    assert result['status'] == 'http_error' and result['submitted'] is True
    assert len(adapter.sent) == 2


def test_group_qr_login_expiry_after_entry_never_retries_as_a_new_sign():
    with gateway([{'body': PROFILE}, {'body': '<input type="password">请先登录'}]) as (client, adapter, _), pytest.raises(upstream.UpstreamError) as error:
        client.sign(COOKIES, IDENTITY, GROUP_QR)
    assert error.value.code == 'session_expired' and error.value.details['submitted'] is True
    assert len(adapter.sent) == 2


@pytest.mark.parametrize('explicit_scope', [False, True])
def test_course_qr_with_same_numeric_id_still_needs_enc_and_uses_original_course_protocol(explicit_scope):
    payload = {**SIGN, 'kind': 'qr', 'enc': 'synthetic-course-ticket'}
    if explicit_scope:
        payload['activityScope'] = 'course'
    with gateway([{'body': PROFILE}, {'body': PRESIGN}, {'body': 'success'}]) as (client, adapter, fetch):
        result = client.sign(COOKIES, IDENTITY, payload)
    assert result['status'] == 'reported_success' and result['submitted'] is True
    assert [urlsplit(r.url).path for r in adapter.sent] == [
        '/apis/login/userLogin4Uname.do', '/newsign/preSign', '/pptSign/stuSignajax']
    assert [r.method for r in adapter.sent] == ['GET', 'GET', 'POST']
    assert parse_qs(adapter.sent[-1].body)['enc'] == ['synthetic-course-ticket']
    assert not any(GROUP_PATH in r.url for r in adapter.sent)
    fetch.assert_not_called()


@pytest.mark.parametrize('explicit_scope', [False, True])
def test_course_qr_cannot_use_group_ticket_free_route_when_enc_is_missing(explicit_scope):
    payload = {**SIGN, 'kind': 'qr'}
    if explicit_scope:
        payload['activityScope'] = 'course'
    with gateway([]) as (client, adapter, fetch), pytest.raises(upstream.UpstreamError) as error:
        client.sign(COOKIES, IDENTITY, payload)
    assert error.value.code == 'invalid_sign_input'
    assert adapter.sent == []
    fetch.assert_not_called()
