"""Group-photo HTTP contracts against synthetic HTTPS; never a real account."""
import json
from urllib.parse import parse_qs, urlencode, urlsplit

import pytest
import requests

from test_ordinary_http import gateway, GROUP_SIGN, GROUP_CONTEXT, GROUP_ACTIVITY
from test_upstream import COOKIES, IDENTITY, PROFILE
from group_photo_protocol import GROUP_PHOTO_USER_AGENT, REDIRECT_FIELDS
import upstream


ACTIVITY = {**GROUP_ACTIVITY, 'chatId': '400'}
PC_PHOTO = (GROUP_CONTEXT + '<input id="activeStatus" value="1">'
            '<p class="Photo_p">此签到需要手机拍照</p>')
PHOTO_ID = 'a' * 32
FORM = ('<script>var acId="300";var uid="101";'
        '$.ajax({url:"/sign/stuSignajax",type:"post",data:{}});</script>'
        '<div class="message_camera"></div><a id="signButton" onclick="sign()">签到</a>')
REDIRECT = '/sign/preStuSign?' + urlencode({**REDIRECT_FIELDS, 'activeId': '300', 'chatId': '400', 'uid': '101'})


def record(puid='102', photo=PHOTO_ID, *, signed=True):
    html = '<script>var acId="300";var uid="' + str(puid) + '";</script>'
    html += '<div class="zsign_success"><h1>签到成功</h1></div>' if signed else '<p>尚未签到</p>'
    if photo:
        html += '<div class="zsign_photo"><img objId="' + photo + '"></div>'
    return {'body': html}


def members(*puids):
    return {'body': {'result': 1, 'data': {'allCount': len(puids), 'list': [{'puid': p} for p in puids]}}}


def prepare(*tail, form=FORM):
    return [{'body': PROFILE}, {'body': PC_PHOTO}, record('101', None, signed=False),
            {'status': 302, 'headers': {'Location': REDIRECT}}, {'body': form}, *tail]


def ready(*tail):
    return prepare(members('101', '102'), record(), {'body': FORM}, *tail)


def run(replies):
    with gateway(replies, activities=[ACTIVITY]) as (client, adapter, _):
        result = client.sign(COOKIES, IDENTITY, GROUP_SIGN)
    return result, adapter.sent


def assert_no_post(sent):
    assert all(request.method == 'GET' and request.body is None for request in sent)


def test_group_photo_posts_once_using_same_activity_reference_and_verifies_own_record():
    result, sent = run(ready({'body': 'success'}, record('101')))
    assert result['status'] == 'reported_success' and result['submitted'] is True
    assert result['reasonCode'] == 'photo_record_verified' and result['resolvedKind'] == 'photo'
    assert [urlsplit(r.url).path for r in sent] == [
        '/apis/login/userLogin4Uname.do', '/widget/sign/group/pcStuSignGroupController/preSign',
        '/newsign/tiaoZhuanSignDetail', '/sign/preSign', '/sign/preStuSign',
        '/widget/sign/api/findPuidsByActiveId', '/newsign/tiaoZhuanSignDetail',
        '/sign/preStuSign', '/sign/stuSignajax', '/newsign/tiaoZhuanSignDetail']
    assert all(urlsplit(r.url).hostname in ('sso.chaoxing.com', 'mobilelearn.chaoxing.com') for r in sent)
    assert parse_qs(urlsplit(sent[3].url).query) == {
        'activeId': ['300'], 'chatId': ['400'], 'courseId': ['null'], 'general': ['0'], 'ls': ['1']}
    post = sent[-2]
    assert sum(r.method == 'POST' for r in sent) == 1
    assert parse_qs(post.body, keep_blank_values=True) == {
        'activeId': ['300'], 'uid': ['101'], 'clientip': [''], 'useragent': [''],
        'latitude': ['-1'], 'longitude': ['-1'], 'fid': ['200'],
        'objectId': [PHOTO_ID], 'photoResult': ['']}
    assert post.headers['User-Agent'] == GROUP_PHOTO_USER_AGENT
    assert post.headers['Origin'] == 'https://mobilelearn.chaoxing.com'
    assert urlsplit(post.headers['Referer']).path == '/sign/preStuSign'
    assert post.headers['X-Requested-With'] == 'XMLHttpRequest'
    for index, puid in ((2, '101'), (6, '102'), (9, '101')):
        assert parse_qs(urlsplit(sent[index].url).query) == {
            'activeId': ['300'], 'uid': [puid], 'role': ['0'], 'appType': ['0']}
    assert PHOTO_ID not in json.dumps(result)


def test_own_success_record_stops_before_member_lookup_or_post():
    result, sent = run([{'body': PROFILE}, {'body': PC_PHOTO}, record('101', None)])
    assert result['status'] == 'already_signed' and result['submitted'] is False
    assert result['resolvedKind'] == 'photo' and len(sent) == 3
    assert_no_post(sent)


@pytest.mark.parametrize('tail', [
    [members('101')], [members('101', '102'), record(photo=None)],
])
def test_complete_lookup_without_an_available_photo_waits_without_submitting(tail):
    result, sent = run(prepare(*tail))
    assert result['status'] == 'photo_required' and result['reasonCode'] == 'photo_missing'
    assert result['submitted'] is False
    assert_no_post(sent)


@pytest.mark.parametrize('tail', [
    [members('102')],
    [{'body': {'result': 1, 'data': {'allCount': 2, 'list': [{'puid': '101'}]}}}],
    [{'body': 'private-malformed-json'}],
    [members('101', '102'), record('999')],
    [members('101', '102'), {'status': 500, 'body': 'private-error'}],
    [members('101', '102'), requests.ReadTimeout('private-timeout')],
])
def test_partial_or_unbound_lookup_is_not_reported_as_no_photos(tail):
    result, sent = run(prepare(*tail))
    assert result['reasonCode'] == 'photo_lookup_incomplete' and result['submitted'] is False
    assert_no_post(sent)


def test_member_lookup_is_bounded_and_does_not_claim_exhaustion():
    peers = [str(102 + index) for index in range(21)]
    result, sent = run(prepare(members('101', *peers), *(record(p, None) for p in peers[:20])))
    assert result['reasonCode'] == 'photo_lookup_incomplete' and result['submitted'] is False
    assert len([r for r in sent if urlsplit(r.url).path == '/newsign/tiaoZhuanSignDetail']) == 21
    assert_no_post(sent)


def test_later_bound_member_photo_is_usable_when_an_earlier_record_is_unrecognized():
    result, sent = run(prepare(members('101', '102', '103'), record('999'), record('103'),
                              {'body': FORM}, {'body': 'success'}, record('101')))
    assert result['status'] == 'reported_success'
    assert sum(r.method == 'POST' for r in sent) == 1


@pytest.mark.parametrize('location', [
    'https://example.org' + REDIRECT, REDIRECT.replace('uid=101', 'uid=999'),
    REDIRECT.replace('activeId=300', 'activeId=301'), REDIRECT.replace('chatId=400', 'chatId=401'),
    REDIRECT.replace('/sign/preStuSign', '/sign/stuSignajax'), REDIRECT + '&uid=101',
])
def test_unsafe_or_mismatched_redirects_are_never_followed(location):
    replies = prepare()[:3] + [{'status': 302, 'headers': {'Location': location}}]
    result, sent = run(replies)
    assert result['status'] == 'need_context' and result['submitted'] is False
    assert len(sent) == 4
    assert_no_post(sent)


@pytest.mark.parametrize('form', [FORM.replace('uid="101"', 'uid="999"'),
                                  FORM + '<p>活动已结束</p>', FORM + '<p>需要人脸验证</p>'])
@pytest.mark.parametrize('after_lookup', [False, True])
def test_form_binding_rules_and_expiry_are_checked_again_immediately_before_post(form, after_lookup):
    replies = prepare(members('101', '102'), record(), {'body': form}) if after_lookup else prepare(form=form)
    result, sent = run(replies)
    assert result['status'] == 'need_context' and result['submitted'] is False
    assert_no_post(sent)


@pytest.mark.parametrize(('reply', 'code'), [
    ({'body': '<input type="password">请先登录'}, 'session_expired'),
    ({'status': 429}, 'upstream_rate_limited'), ({'status': 403}, 'group_photo_forbidden'),
    (requests.ReadTimeout('private-timeout'), 'timeout_unknown'),
])
def test_read_failures_before_post_keep_unsubmitted_photo_context(reply, code):
    replies = [{'body': PROFILE}, {'body': PC_PHOTO}, reply]
    with gateway(replies, activities=[ACTIVITY]) as (client, adapter, _), pytest.raises(upstream.UpstreamError) as raised:
        client.sign(COOKIES, IDENTITY, GROUP_SIGN)
    assert raised.value.code == code
    assert raised.value.details['submitted'] is False
    assert raised.value.details['resolvedKind'] == 'photo'
    assert raised.value.details['phase'] == 'group_photo_record'
    assert_no_post(adapter.sent)


@pytest.mark.parametrize('verification', [
    record('101', 'b' * 32), record('999'), record('101', None),
    {'status': 429}, {'body': '<input type="password">请先登录'},
    requests.ReadTimeout('private-verification-timeout'),
])
def test_success_text_without_matching_own_photo_record_remains_submitted_unknown(verification):
    result, sent = run(ready({'body': 'success'}, verification))
    assert result['status'] == 'outcome_unknown' and result['submitted'] is True
    assert result['resolvedKind'] == 'photo'
    assert sum(r.method == 'POST' for r in sent) == 1
    public = {k: v for k, v in result.items() if k != '_diagnostic'}
    assert PHOTO_ID not in json.dumps(public) and 'private-' not in json.dumps(public)


@pytest.mark.parametrize('failure', [requests.ReadTimeout('private-timeout'), requests.ConnectionError('private-error')])
def test_post_transport_error_never_retries_or_reports_unsubmitted(failure):
    result, sent = run(ready(failure))
    assert result['status'] == 'outcome_unknown' and result['submitted'] is True
    assert result['phase'] == 'group_photo_submit' and result['httpStatus'] is None
    assert sum(r.method == 'POST' for r in sent) == 1
    assert 'private-' not in json.dumps(result)


def test_post_http_error_still_requires_matching_record_before_any_success_claim():
    result, sent = run(ready({'status': 500, 'body': 'private-error'}, record('101', None, signed=False)))
    assert result['status'] == 'outcome_unknown' and result['submitted'] is True
    assert sum(r.method == 'POST' for r in sent) == 1


def test_missing_notification_chat_id_does_not_start_photo_requests():
    with gateway([{'body': PROFILE}, {'body': PC_PHOTO}]) as (client, adapter, _):
        result = client.sign(COOKIES, IDENTITY, GROUP_SIGN)
    assert result['status'] == 'need_context' and result['submitted'] is False
    assert result['reasonCode'] == 'photo_context_required' and len(adapter.sent) == 2
