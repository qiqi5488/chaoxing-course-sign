"""Controlled group gesture direct-entry tests; no real account or network.

Synthetic completion pages establish the typed routing contract, not live
acceptance of any particular group's gesture activity.
"""
import json
from urllib.parse import parse_qs, urlsplit

import pytest
import requests

from test_group_qr_direct import gateway, notification, GROUP_PATH, CONTEXT, SUCCESS
from test_upstream import PROFILE, COOKIES, IDENTITY, SIGN, ORDINARY_DETAIL
from test_server import service, login, sign
import upstream


GROUP_GESTURE = {'activityScope': 'group', 'kind': 'gesture', 'activeId': '300'}
GROUP_SUBMIT_PATH = '/widget/sign/group/pcStuSignGroupController/signIn'
GESTURE_WIDGET = '<div id="gesturepwd"></div>'
GESTURE_FORM = (CONTEXT + '<input id="activeStatus" value="1">' + GESTURE_WIDGET
                + '<script>function submitGesture(passwd){checkSignCode(passwd);'
                + 'window.location.href="' + GROUP_SUBMIT_PATH + '?activeId="+activeId;}</script>')


@pytest.mark.parametrize('automatic', [False, True])
def test_controlled_group_gesture_uses_only_active_id_and_the_fixed_group_get(automatic):
    note = notification('手势签到', title='自定义标题：二维码签到')
    assert note['kind'] == 'gesture' and note['activityScope'] == 'group'
    with gateway([{'body': PROFILE}, {'body': SUCCESS}], activities=[note]) as (client, adapter, fetch):
        result = client.sign(COOKIES, IDENTITY, {**GROUP_GESTURE, '_autoSign': automatic})
    assert result['status'] == 'reported_success' and result['reasonCode'] == 'record_signed'
    assert result['submitted'] is True and result['phase'] == 'group_entry'
    assert fetch.call_count == 1 and fetch.call_args.args[1] == '101'
    assert [urlsplit(r.url).path for r in adapter.sent] == ['/apis/login/userLogin4Uname.do', GROUP_PATH]
    assert all(r.method == 'GET' and r.body is None for r in adapter.sent)
    entry = adapter.sent[-1]
    assert urlsplit(entry.url).scheme == 'https' and urlsplit(entry.url).hostname == 'mobilelearn.chaoxing.com'
    assert parse_qs(urlsplit(entry.url).query, keep_blank_values=True) == {'activeId': ['300']}
    assert not any(word in r.url for r in adapter.sent for word in
                   ('stuSignajax', 'studentSign', 'checkSignCode', 'checkIfValidate', 'QRCode',
                    'signCode=', 'Code=', 'enc=', 'courseId=', 'classId=', 'bfmzdx'))


@pytest.mark.parametrize(('label', 'title', 'permitted'), [
    ('手势签到', '普通签到', True),
    ('手势签到', '二维码签到', True),
    ('普通签到', '手势签到', False),
    ('二维码签到', '手势签到', False),
    ('签到码签到', '手势签到', False),
    ('未知类型', '手势签到', False),
    ('', '手势签到', False),
])
def test_canonical_group_label_selects_gesture_and_title_cannot_change_the_type(label, title, permitted):
    replies = [{'body': PROFILE}] + ([{'body': SUCCESS}] if permitted else [])
    with gateway(replies, activities=[notification(label, title=title)]) as (client, adapter, _):
        result = client.sign(COOKIES, IDENTITY, GROUP_GESTURE)
    assert result['status'] == ('reported_success' if permitted else 'requires_manual')
    assert result['submitted'] is permitted and len(adapter.sent) == (2 if permitted else 1)


@pytest.mark.parametrize('activities', [
    [], [notification('手势签到', active_id='301')],
    [notification('手势签到'), notification('手势签到')],
    [notification('普通签到')], [notification('二维码签到')],
])
def test_group_gesture_requires_exactly_one_own_notification_with_matching_id_and_type(activities):
    with gateway([{'body': PROFILE}], activities=activities) as (client, adapter, fetch):
        result = client.sign(COOKIES, IDENTITY, GROUP_GESTURE)
    assert result['status'] == 'requires_manual' and result['submitted'] is False
    assert len(adapter.sent) == 1 and fetch.call_count == 1


def test_a_course_notification_with_gesture_label_cannot_become_a_group_candidate():
    note = notification('手势签到', url='https://mobilelearn.chaoxing.com/newsign/preSign?'
                        'activePrimaryId=300&courseId=400&classId=500')
    assert note is None
    with gateway([{'body': PROFILE}], activities=[]) as (client, adapter, _):
        result = client.sign(COOKIES, IDENTITY, GROUP_GESTURE)
    assert result['status'] == 'requires_manual' and result['submitted'] is False
    assert len(adapter.sent) == 1


@pytest.mark.parametrize('change', [{'uid': '999'}, {'fid': '999'}])
def test_group_gesture_saved_identity_must_match_before_notifications_or_entry(change):
    with gateway([{'body': PROFILE}]) as (client, adapter, fetch), pytest.raises(upstream.UpstreamError) as error:
        client.sign(COOKIES, {**IDENTITY, **change}, GROUP_GESTURE)
    assert error.value.code == 'identity_mismatch' and error.value.details['submitted'] is False
    assert len(adapter.sent) == 1
    fetch.assert_not_called()


@pytest.mark.parametrize(('body', 'submitted'), [
    (SUCCESS.replace('value="300"', 'value="301"'), True),
    (SUCCESS.replace('value="101"', 'value="100"'), True),
    (CONTEXT + '<script>"qd_Success 签到成功"</script>', False),
    ('<div class="qd_Success">签到成功</div>', True),
])
def test_group_gesture_cannot_claim_success_without_bound_visible_completion(body, submitted):
    with gateway([{'body': PROFILE}, {'body': body}], activities=[notification('手势签到')]) as (client, adapter, _):
        result = client.sign(COOKIES, IDENTITY, GROUP_GESTURE)
    assert result['status'] not in ('reported_success', 'already_signed')
    assert result['submitted'] is submitted and len(adapter.sent) == 2


@pytest.mark.parametrize('failure', [requests.ReadTimeout('private-gesture'), requests.ConnectionError('private-gesture')])
def test_group_gesture_entry_transport_failure_does_not_retry_or_fall_back_to_course(failure):
    with gateway([{'body': PROFILE}, failure], activities=[notification('手势签到')]) as (client, adapter, _):
        result = client.sign(COOKIES, IDENTITY, GROUP_GESTURE)
    assert result['status'] == 'outcome_unknown' and result['submitted'] is True
    assert len(adapter.sent) == 2 and 'private-gesture' not in json.dumps(result)


@pytest.mark.parametrize('explicit_scope', [False, True])
def test_course_gesture_with_same_numeric_id_keeps_the_course_detail_validation_and_submit_chain(explicit_scope):
    payload = {**SIGN, 'kind': 'gesture'}
    if explicit_scope:
        payload['activityScope'] = 'course'
    detail = {'data': {**ORDINARY_DETAIL['data'], 'otherId': 3, 'signCode': ''}}
    replies = [{'body': PROFILE}, {'body': detail}, {'body': {'result': 0}}, {'body': 'success'}]
    with gateway(replies, activities=[notification('手势签到')]) as (client, adapter, fetch):
        result = client.sign(COOKIES, IDENTITY, payload)
    assert result['status'] == 'reported_success' and result['submitted'] is True
    assert [urlsplit(r.url).path for r in adapter.sent] == [
        '/apis/login/userLogin4Uname.do', '/v2/apis/active/getPPTActiveInfo',
        '/widget/sign/pcStuSignController/checkIfValidate', '/pptSign/stuSignajax']
    assert all(r.method == 'GET' for r in adapter.sent)
    query = parse_qs(urlsplit(adapter.sent[-1].url).query, keep_blank_values=True)
    assert query['uid'] == ['100'] and query['fid'] == ['200'] and 'signCode' not in query
    assert not any(GROUP_PATH in r.url for r in adapter.sent)
    fetch.assert_not_called()


def test_missing_course_context_does_not_implicitly_turn_a_gesture_into_group_scope():
    with gateway([]) as (client, adapter, fetch), pytest.raises(upstream.UpstreamError) as error:
        client.sign(COOKIES, IDENTITY, {'kind': 'gesture', 'activeId': '300'})
    assert error.value.code == 'invalid_sign_input' and adapter.sent == []
    fetch.assert_not_called()


def test_group_gesture_api_accepts_an_explicit_group_scope_without_course_context(service):
    account_id, _ = login(service)
    result = sign(service, account_id, key='group-gesture-direct-request',
                  payload={'activityScope': 'group', 'kind': 'gesture', 'activeId': '123'})
    assert result.status_code == 200 and result.json()['status'] == 'reported_success'
    assert service.gateway.sign_calls == 1


@pytest.mark.parametrize('extra', [
    {'courseId': '456'}, {'classId': '789'}, {'courseId': '456', 'classId': '789'},
    {'signCode': '1236'}, {'enc': 'synthetic-qr'},
    {'location': {'latitude': 30, 'longitude': 120, 'address': '现场'}},
    {'validate': 'synthetic-validation'}, {'ifopenAddress': '1'},
])
def test_group_gesture_api_rejects_extra_course_context_or_other_sign_credentials_before_gateway(service, extra):
    account_id, _ = login(service)
    payload = {'activityScope': 'group', 'kind': 'gesture', 'activeId': '123', **extra}
    result = sign(service, account_id, key='group-gesture-invalid-context', payload=payload)
    assert result.status_code == 422 and service.gateway.sign_calls == 0


@pytest.mark.parametrize('automatic', [False, True])
def test_active_bound_group_gesture_form_continues_once_to_fixed_sign_in_without_a_pattern(automatic):
    replies = [{'body': PROFILE}, {'body': GESTURE_FORM}, {'body': SUCCESS}]
    with gateway(replies, activities=[notification('手势签到')]) as (client, adapter, fetch):
        result = client.sign(COOKIES, IDENTITY, {**GROUP_GESTURE, '_autoSign': automatic})
    assert result['status'] == 'reported_success' and result['reasonCode'] == 'record_signed'
    assert result['submitted'] is True and result['phase'] == 'group_submit'
    assert [urlsplit(r.url).path for r in adapter.sent] == [
        '/apis/login/userLogin4Uname.do', GROUP_PATH, GROUP_SUBMIT_PATH]
    assert all(r.method == 'GET' and r.body is None for r in adapter.sent)
    for request in adapter.sent[1:]:
        assert urlsplit(request.url).scheme == 'https'
        assert urlsplit(request.url).hostname == 'mobilelearn.chaoxing.com'
        assert parse_qs(urlsplit(request.url).query, keep_blank_values=True) == {'activeId': ['300']}
    assert not any(word in r.url for r in adapter.sent for word in
                   ('checkSignCode', 'passwd', 'signCode', 'stuSignajax', 'QRCode', 'enc='))
    assert fetch.call_count == 1 and fetch.call_args.args[1] == '101'


@pytest.mark.parametrize('form', [
    GESTURE_FORM.replace('<input id="activeId" value="300">', ''),
    GESTURE_FORM.replace('<input id="puid" value="101">', ''),
    GESTURE_FORM.replace('id="activeId" value="300"', 'id="activeId" value="301"'),
    GESTURE_FORM.replace('id="puid" value="101"', 'id="puid" value="100"'),
    GESTURE_FORM + '<input id="activeId" value="300">',
    GESTURE_FORM.replace('<input id="activeStatus" value="1">', ''),
    GESTURE_FORM.replace('id="activeStatus" value="1"', 'id="activeStatus" value="0"'),
    GESTURE_FORM.replace('id="activeStatus" value="1"', 'id="activeStatus" value="2"'),
    GESTURE_FORM + '<input id="activeStatus" value="0">',
    GESTURE_FORM.replace(GESTURE_WIDGET, ''),
    GESTURE_FORM.replace(GESTURE_WIDGET, '<div id="gesturepwd" hidden></div>'),
    GESTURE_FORM.replace(GESTURE_WIDGET, '<div id="gesturepwd" style="display:none"></div>'),
    GESTURE_FORM.replace(GESTURE_WIDGET, '<div aria-hidden="true">' + GESTURE_WIDGET + '</div>'),
    GESTURE_FORM.replace(GESTURE_WIDGET, '<script>' + GESTURE_WIDGET + '</script>'),
    GESTURE_FORM.replace(GROUP_SUBMIT_PATH, '/unrecognized/signIn'),
    GESTURE_FORM + '<p>需要验证</p>',
])
def test_group_gesture_does_not_submit_from_missing_hidden_inactive_or_unbound_forms(form):
    with gateway([{'body': PROFILE}, {'body': form}], activities=[notification('手势签到')]) as (client, adapter, _):
        result = client.sign(COOKIES, IDENTITY, GROUP_GESTURE)
    assert result['status'] not in ('reported_success', 'already_signed')
    assert result['phase'] == 'group_entry'
    assert [urlsplit(r.url).path for r in adapter.sent] == ['/apis/login/userLogin4Uname.do', GROUP_PATH]


@pytest.mark.parametrize(('kind', 'label'), [('ordinary', '普通签到'), ('qr', '二维码签到')])
def test_a_gesture_widget_cannot_switch_another_group_type_to_the_gesture_final_endpoint(kind, label):
    with gateway([{'body': PROFILE}, {'body': GESTURE_FORM}], activities=[notification(label)]) as (client, adapter, _):
        result = client.sign(COOKIES, IDENTITY, {**GROUP_GESTURE, 'kind': kind})
    assert result['status'] == 'need_context' and result['submitted'] is False
    assert result['phase'] == 'group_entry' and len(adapter.sent) == 2


def test_a_non_successful_entry_http_status_cannot_authorize_the_gesture_final_get():
    with gateway([{'body': PROFILE}, {'status': 503, 'body': GESTURE_FORM}],
                 activities=[notification('手势签到')]) as (client, adapter, _):
        result = client.sign(COOKIES, IDENTITY, GROUP_GESTURE)
    assert result['status'] == 'http_error' and result['phase'] == 'group_entry'
    assert len(adapter.sent) == 2


@pytest.mark.parametrize('failure', [requests.ReadTimeout('private-final-ticket'), requests.ConnectionError('private-final-ticket')])
def test_group_gesture_final_transport_failure_stays_submitted_and_is_not_retried(failure):
    replies = [{'body': PROFILE}, {'body': GESTURE_FORM}, failure]
    with gateway(replies, activities=[notification('手势签到')]) as (client, adapter, _):
        result = client.sign(COOKIES, IDENTITY, GROUP_GESTURE)
    assert result['status'] == 'outcome_unknown' and result['submitted'] is True
    assert result['phase'] == 'group_submit' and len(adapter.sent) == 3
    assert urlsplit(adapter.sent[-1].url).path == GROUP_SUBMIT_PATH
    assert 'private-final-ticket' not in json.dumps(result)


@pytest.mark.parametrize(('reply', 'expected', 'reason'), [
    ({'status': 302, 'headers': {'Location': 'https://mobilelearn.chaoxing.com'
                               + GROUP_SUBMIT_PATH + '?activeId=300'}, 'body': SUCCESS}, 'http_error', 'http_error'),
    ({'body': '<div class="qd_Success">签到成功</div>'}, 'need_context', 'page_identity_missing'),
    ({'body': SUCCESS.replace('value="300"', 'value="301"')}, 'unknown', 'context_mismatch'),
    ({'body': SUCCESS.replace('value="101"', 'value="100"')}, 'unknown', 'context_mismatch'),
    ({'body': GESTURE_FORM}, 'need_context', 'group_form_pending'),
])
def test_group_gesture_final_redirect_missing_identity_or_second_form_never_repeats_submission(reply, expected, reason):
    with gateway([{'body': PROFILE}, {'body': GESTURE_FORM}, reply],
                 activities=[notification('手势签到')]) as (client, adapter, _):
        result = client.sign(COOKIES, IDENTITY, GROUP_GESTURE)
    assert (result['status'], result['reasonCode']) == (expected, reason)
    assert result['phase'] == 'group_submit' and result['submitted'] is True
    assert [urlsplit(r.url).path for r in adapter.sent].count(GROUP_SUBMIT_PATH) == 1
    assert len(adapter.sent) == 3
    assert result['_diagnostic']['endpoint'] == GROUP_SUBMIT_PATH


def test_group_gesture_login_expiry_after_final_get_is_uncertain_and_not_repeated():
    replies = [{'body': PROFILE}, {'body': GESTURE_FORM}, {'body': '<input type="password">请先登录'}]
    with gateway(replies, activities=[notification('手势签到')]) as (client, adapter, _), pytest.raises(upstream.UpstreamError) as error:
        client.sign(COOKIES, IDENTITY, GROUP_GESTURE)
    assert error.value.code == 'session_expired'
    assert error.value.details == {'phase': 'group_submit', 'submitted': True}
    assert len(adapter.sent) == 3
