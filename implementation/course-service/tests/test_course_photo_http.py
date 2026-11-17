"""Course photo contracts use synthetic HTTP; these are not live acceptance tests."""
import json
from urllib.parse import parse_qs, urlsplit

import pytest
import requests

from test_ordinary_http import gateway
from test_upstream import COOKIES, IDENTITY, PROFILE, SIGN, ORDINARY_DETAIL, PRESIGN
from test_server import service, login, sign, SIGN_PAYLOAD
import upstream


PHOTO_ID = 'a' * 32
PHOTO = {'objectId': PHOTO_ID, 'photoResult': json.dumps({
    'result': 1, 'protocol': 'CLIENT_CHOOSE_IMAGE_RESULT', 'camera': 5,
    'files': json.dumps([{'objectid': PHOTO_ID, 'width': 600, 'height': 800, 'size': 1024,
        'resid': 'synthetic-resid', 'imageUrl': 'https://p.cldisk.com/star4/' + PHOTO_ID + '/origin.jpg',
        'name': 'test.jpg', 'type': 'jpg', 'isMirror': False}])})}
PAYLOAD = {**SIGN, 'kind': 'photo', 'photo_payload': PHOTO}
FORM = PRESIGN + '<input name="uid" value="100">'


def detail(**changes):
    return {'data': {**ORDINARY_DETAIL['data'], 'ifphoto': 1, **changes}}


def record(uid='100', aid='300', photo=PHOTO_ID):
    return {'body': '<script>var acId="' + aid + '";var uid="' + uid + '";</script>'
        '<div class="zsign_success"><h1>签到成功</h1></div>'
        '<div class="zsign_photo"><img objId="' + photo + '"></div>'}


def history(status=1, **changes):
    row = {'activeid': '300', 'activeType': 2, 'userStatus': status, **changes}
    return {'body': {'result': 1, 'data': {'total': 1, 'page': 1, 'list': [row]}}}


def run(tail=(), *, activity=None, form=FORM, payload=PAYLOAD):
    with gateway([{'body': PROFILE}, {'body': activity or detail()}, {'body': form}, *tail]) as (client, adapter, _):
        result = client.sign(COOKIES, IDENTITY, payload)
    return result, adapter.sent


def no_submission(sent):
    assert all(r.method == 'GET' and urlsplit(r.url).path != '/pptSign/stuSignajax' for r in sent)


def test_full_metadata_uses_course_post_and_both_own_records_before_success():
    result, sent = run([{'body': 'success'}, record(), history()])
    assert result['status'] == 'reported_success' and result['submitted'] is True
    assert result['reasonCode'] == 'photo_record_verified'
    assert [urlsplit(r.url).path for r in sent] == [
        '/apis/login/userLogin4Uname.do', '/v2/apis/active/getPPTActiveInfo', '/newsign/preSign',
        '/pptSign/stuSignajax', '/newsign/preSign', '/v2/apis/signStat/getActiveList']
    assert all(urlsplit(r.url).hostname in ('sso.chaoxing.com', 'mobilelearn.chaoxing.com') for r in sent)
    post = sent[3]
    assert post.method == 'POST' and sum(r.method == 'POST' for r in sent) == 1
    form = parse_qs(post.body, keep_blank_values=True)
    assert form['objectId'] == [PHOTO_ID] and form['photoResult'] == [PHOTO['photoResult']]
    assert form['uid'] == ['100'] and form['fid'] == ['200'] and form['courseId'] == ['400']
    assert parse_qs(urlsplit(sent[-1].url).query)['puid'] == ['101']
    for request in (sent[2], sent[4]):
        query = parse_qs(urlsplit(request.url).query)
        assert query['isTeacherViewOpen'] == ['0'] and query['appType'] == ['15']
    public = {k: v for k, v in result.items() if k != 'cookies'}
    assert PHOTO_ID not in json.dumps(public) and 'synthetic-resid' not in json.dumps(public)


@pytest.mark.parametrize('ended', [False, True])
def test_verified_already_signed_stops_without_photo_payload_even_when_ended(ended):
    state = detail(nowTime=1_800_002_000_000) if ended else detail()
    result, sent = run([history()], activity=state, form=record()['body'], payload={**SIGN, 'kind': 'photo'})
    assert result['status'] == 'already_signed' and result['submitted'] is False
    no_submission(sent)


def test_ended_unsigned_activity_never_submits_even_with_full_metadata():
    result, sent = run(activity=detail(nowTime=1_800_002_000_000))
    assert result['reasonCode'] == 'photo_activity_ended' and result['submitted'] is False
    no_submission(sent)


def test_missing_course_source_is_not_misreported_as_no_classmate_photos():
    result, sent = run(payload={**SIGN, 'kind': 'photo', '_autoSign': True})
    assert result['status'] == 'photo_required' and result['submitted'] is False
    assert result['reasonCode'] == 'course_photo_source_unavailable'
    assert len(sent) == 3
    no_submission(sent)


@pytest.mark.parametrize('changes', [
    {'activeId': None}, {'activeId': '301'}, {'id': '301'}, {'clazzid': None}, {'classId': '501'},
    {'courseId': '401'}, {'ifphoto': 0}, {'ifopenAddress': 1}, {'ifopenAddress': 2},
    {'openCheckFaceFlag': 1}, {'openCheckFaceFlag': None}, {'otherId': 3},
    {'nowTime': None}, {'starttime': 1_800_002_000_000}, {'jwCourseId': '900'}, {'name': '教务签到'},
])
def test_unconfirmed_or_extra_rules_stop_before_presign(changes):
    result, sent = run(activity=detail(**changes))
    assert result['submitted'] is False and result['reasonCode'] == 'photo_rules_unverified'
    assert len(sent) == 2
    no_submission(sent)


@pytest.mark.parametrize('form', ['', FORM.replace('300', '301'), FORM + '<input name="classId" value="501">',
    FORM + '<script src="/pptSign/analysis2"></script>', FORM + '<p>活动已结束</p>',
    FORM + '<p>需要人脸验证</p>', FORM + '<input id="ifopenAddress" value="1">',
    PRESIGN, FORM.replace('100', '999'), FORM + '<script>var uid="101";</script>',
    FORM + '<p>签到成功</p>'])
def test_changed_or_unrecognized_presign_never_posts(form):
    result, sent = run(form=form)
    assert result['reasonCode'] == 'photo_form_unverified' and result['submitted'] is False
    no_submission(sent)


@pytest.mark.parametrize('tail', [
    [record(uid='999')], [record(aid='301')], [record(photo='b' * 32)],
    [{'body': 'success'}], [{'status': 302, 'headers': {'Location': '/pptSign/stuSignajax'}}],
    [record(), history(0)], [record(), history(activeid='301')],
    [record(), {'body': {'result': 0, 'data': None}}],
    [{'status': 429}], [requests.ReadTimeout('private-value')],
])
def test_submit_success_without_both_matching_records_is_unknown_and_never_retried(tail):
    result, sent = run([{'body': 'success'}, *tail])
    assert result['status'] == 'outcome_unknown' and result['submitted'] is True
    assert result['reasonCode'] == 'photo_record_unverified'
    assert sum(r.method == 'POST' for r in sent) == 1
    assert 'private-value' not in json.dumps(result)


@pytest.mark.parametrize('failure', [requests.ReadTimeout('private'), requests.ConnectionError('private')])
def test_submit_transport_failure_keeps_duplicate_guard(failure):
    result, sent = run([failure])
    assert result['status'] == 'outcome_unknown' and result['submitted'] is True
    assert result['phase'] == 'course_photo_submit' and len(sent) == 4


@pytest.mark.parametrize('stage', ['presign', 'submit'])
def test_redirect_cannot_trigger_an_extra_submission(stage):
    redirect = {'status': 302, 'headers': {'Location': 'https://mobilelearn.chaoxing.com/pptSign/stuSignajax'}}
    replies = [{'body': PROFILE}, {'body': detail()}, redirect] if stage == 'presign' else [
        {'body': PROFILE}, {'body': detail()}, {'body': FORM}, redirect, {'body': 'not signed'}]
    with gateway(replies) as (client, adapter, _):
        result = client.sign(COOKIES, IDENTITY, PAYLOAD)
    assert result['submitted'] == (stage == 'submit')
    assert sum(urlsplit(r.url).path == '/pptSign/stuSignajax' for r in adapter.sent) == (stage == 'submit')


@pytest.mark.parametrize('invalid', [
    {}, {'objectId': PHOTO_ID}, {'objectId': PHOTO_ID, 'photoResult': ''},
    {'objectId': 'b' * 32, 'photoResult': PHOTO['photoResult']},
    {'objectId': PHOTO_ID, 'photoResult': 'not-json'},
])
def test_incomplete_metadata_rejected_without_network(invalid):
    with gateway([]) as (client, adapter, _), pytest.raises(upstream.UpstreamError):
        client.sign(COOKIES, IDENTITY, {**PAYLOAD, 'photo_payload': invalid})
    assert not adapter.sent


@pytest.mark.parametrize('change', [
    {'objectid': 'b' * 32}, {'width': 0}, {'height': True}, {'size': float('nan')},
    {'resid': ''}, {'imageUrl': ''}, {'isMirror': 1}, {'name': None},
])
def test_inconsistent_file_metadata_never_reaches_network(change):
    value = json.loads(PHOTO['photoResult'])
    photo = json.loads(value['files'])[0]
    photo.update(change)
    value['files'] = json.dumps([photo])
    invalid = {**PHOTO, 'photoResult': json.dumps(value)}
    with gateway([]) as (client, adapter, _), pytest.raises(upstream.UpstreamError):
        client.sign(COOKIES, IDENTITY, {**PAYLOAD, 'photo_payload': invalid})
    assert not adapter.sent


@pytest.mark.parametrize('change', [
    {'result': True}, {'protocol': 'unknown'}, {'camera': 1},
    {'files': []}, {'files': '[]'}, {'files': '[{}, {}]'},
])
def test_course_photo_requires_observed_nested_json_contract(change):
    value = json.loads(PHOTO['photoResult'])
    value.update(change)
    with gateway([]) as (client, adapter, _), pytest.raises(upstream.UpstreamError):
        client.sign(COOKIES, IDENTITY, {**PAYLOAD, 'photo_payload': {**PHOTO, 'photoResult': json.dumps(value)}})
    assert not adapter.sent


def test_apparent_own_page_without_matching_personal_statistics_is_not_success_or_resubmitted():
    result, sent = run([history(0)], form=record()['body'])
    assert result['status'] == 'need_context' and result['submitted'] is False
    no_submission(sent)


@pytest.mark.parametrize('reply', [{'status': 403}, {'body': '<input type="password">请先登录'},
                                  requests.ReadTimeout('private')])
def test_failed_personal_verification_after_post_cannot_be_reported_unsubmitted(reply):
    result, sent = run([{'body': 'success'}, record(), reply])
    assert result['status'] == 'outcome_unknown' and result['submitted'] is True
    assert sum(r.method == 'POST' for r in sent) == 1


def test_course_photo_api_checks_enrollment_before_registering(service):
    account_id, _ = login(service)
    reply = sign(service, account_id, payload={**SIGN_PAYLOAD, 'kind': 'photo', 'classId': '999'})
    assert reply.status_code == 403 and reply.json()['error']['code'] == 'course_not_owned'
    assert service.gateway.sign_calls == 0


@pytest.mark.parametrize('status', ['requires_validation', 'rejected', 'outcome_unknown'])
def test_submitted_course_photo_cannot_be_sent_again_with_a_new_key(service, status):
    account_id, _ = login(service)
    calls = []
    def submit(*args):
        calls.append(True)
        return {'status': status, 'message': 'test', 'submitted': True}
    service.gateway.sign = submit
    first = sign(service, account_id, payload={**SIGN_PAYLOAD, 'kind': 'photo'}).json()
    second = sign(service, account_id, key='different-request-0002', payload={**SIGN_PAYLOAD, 'kind': 'photo'})
    assert second.status_code == 409 and second.json()['error']['code'] == 'activity_already_attempted'
    assert first['submitted'] is True and len(calls) == 1


def test_explicitly_unsubmitted_course_photo_can_receive_metadata_with_a_new_request(service):
    account_id, _ = login(service)
    calls = []
    def submit(*args):
        calls.append(args[-1])
        return {'status': 'photo_required', 'message': 'test', 'submitted': False}
    service.gateway.sign = submit
    first = sign(service, account_id, payload={**SIGN_PAYLOAD, 'kind': 'photo'})
    second = sign(service, account_id, key='with-photo-data-0002',
        payload={**SIGN_PAYLOAD, 'kind': 'photo', 'photo_payload': PHOTO})
    assert first.status_code == second.status_code == 200 and len(calls) == 2
    assert calls[1]['photo_payload'] == PHOTO
