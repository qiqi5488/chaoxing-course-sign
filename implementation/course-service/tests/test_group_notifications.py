"""Synthetic official token/IM transports only; never open a network socket."""
import base64
from email.message import Message
import json
from pathlib import Path
import sys
from types import SimpleNamespace
from unittest.mock import patch
from urllib.parse import parse_qs, urlsplit

import pytest
import requests
from requests.adapters import BaseAdapter

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import group_notifications as groups


TOKEN = {'result': 1, 'msg': {'puid': '101', 'uid': 'im-test-user', 'hxToken': 'private-test-token'}}


def response(body, status=200, headers=None):
    item = requests.Response()
    item.status_code = status
    item._content = json.dumps(body).encode('utf-8') if isinstance(body, (dict, list)) else body.encode('utf-8')
    item.encoding = 'utf-8'
    item.headers.update(headers or {})
    message = Message()
    message.add_header('Set-Cookie', 'im_cookie=private-cookie; Path=/; Secure')
    item.raw = SimpleNamespace(_original_response=SimpleNamespace(msg=message))
    return item


class OfficialClient:
    def __init__(self, token=TOKEN, status=200):
        self.token, self.status, self.calls = token, status, []
        self.session = SimpleNamespace(cookies={'official_cookie': 'private-official-cookie'})

    def request(self, method, url, **kwargs):
        self.calls.append((method, url, kwargs))
        return response(self.token, self.status)


class MemoryAdapter(BaseAdapter):
    def __init__(self, responses):
        self.responses, self.sent = list(responses), []

    def send(self, request, **kwargs):
        self.sent.append((request, kwargs))
        if not self.responses:
            raise AssertionError('Unexpected HTTP request; no network is permitted in this test.')
        item = self.responses.pop(0)
        if isinstance(item, Exception):
            raise item
        if not isinstance(item, requests.Response):
            item = response(item)
        item.url, item.request = request.url, request
        return item

    def close(self):
        pass


SessionType = requests.Session


class MemorySession(SessionType):
    def __init__(self, adapter):
        super().__init__()
        self.calls = []
        self.mount('https://', adapter)
        self.headers['Cookie'] = 'private-default-cookie'
        self.headers['Authorization'] = 'private-default-auth'
        self.auth = ('private-default-user', 'private-default-password')
        self.cookies.set('private_old_cookie', 'private-cookie', domain='a3-vip6.easemob.com')

    def request(self, method, url, **kwargs):
        self.calls.append((method, url, kwargs))
        return super().request(method, url, **kwargs)


def notice(active='300', chat='900', *, url=None, title='普通签到', **fields):
    data = {'aid': active, 'atype': 2, 'type': 1, 'title': title,
            'url': url or 'https://mobilelearn.chaoxing.com/sign/preSign?courseId=0&activeId='
                   + active + '&chatId=' + chat + '&general=1&ls=1'}
    data.update(fields)
    return {'attachmentType': 15, 'att_chat_course': data}


def channel(attachment=None, group='900'):
    payload = {'type': 'groupchat', 'to': group, 'ext': {}}
    if attachment is not None:
        payload['ext']['attachment'] = attachment
    return {'session_to': group, 'session_type': 'groupchat',
            'meta': {'to': group + '@conference.easemob.com', 'payload': json.dumps(payload)}}


def conversations(rows=(), cursor=''):
    return {'data': {'channel_infos': list(rows), 'cursor': cursor}}


def history(messages=(), last=True):
    return {'data': {'msgs': [{'msg': item} for item in messages], 'is_last': last, 'next_key': 'memory-cursor'}}


def varint(value):
    output = bytearray()
    while value > 127:
        output.append((value & 127) | 128)
        value >>= 7
    return bytes(output) + bytes([value])


def scalar(number, value):
    return varint(number << 3) + varint(value)


def blob(number, value):
    value = value.encode('utf-8') if isinstance(value, str) else value
    return varint(number << 3 | 2) + varint(len(value)) + value


def jid(group):
    return blob(1, 'cx-dev#cxstudy') + blob(2, group) + blob(3, 'conference.easemob.com')


def protobuf_message(attachment, group='900', namespace=1, body_kind=2, *, extra_meta=b'', extra_body=b''):
    # Independent fixture encoder for the retained official protobuf descriptor:
    # Meta.payload=6 -> MessageBody.ext=5 -> KeyValue(JSON_STRING=8).stringValue=6.
    ext = blob(1, 'attachment') + scalar(2, 8) + blob(6, json.dumps(attachment)) if attachment is not None else None
    body = (scalar(1, body_kind) + blob(3, jid(group)) + blob(4, blob(2, '消息正文'))
            + (blob(5, ext) if ext is not None else b'') + extra_body)
    meta = blob(3, jid(group)) + scalar(5, namespace) + blob(6, body) + extra_meta
    return base64.b64encode(meta).decode('ascii')


def run(responses, *, token=TOKEN, token_status=200, expected='101'):
    official = OfficialClient(token, token_status)
    adapter = MemoryAdapter(responses)
    im = MemorySession(adapter)
    with patch.object(groups.requests, 'Session', return_value=im):
        result = groups.fetch_group_notifications(official, expected, 'offline-group-tests')
    return result, official, im, adapter


def test_latest_notice_identity_fixed_endpoints_and_credentials_stay_separate():
    result, official, im, adapter = run([conversations([channel(notice())]), history()])
    assert result['status'] == 'ok'
    assert result['activities'] == [{
        'activeId': '300', 'chatId': '900', 'name': '普通签到', 'kind': 'unknown',
        'status': 'unknown', 'activityScope': 'group', 'notificationType': 'group_attendance',
        'requirements': ['unknown'], 'autoEligible': False}]
    assert official.calls[0] == ('GET', groups.TOKEN_URL, {
        'params': {'crossOrigin': 'true'}, 'headers': {'Accept': 'application/json'}, 'follow_redirects': False})
    assert official.session.cookies == {'official_cookie': 'private-official-cookie'}
    assert not im.trust_env and im.auth is None and not im.cookies
    for request, _ in adapter.sent:
        assert urlsplit(request.url).hostname == 'a3-vip6.easemob.com'
        assert request.headers['Authorization'] == 'Bearer private-test-token'
        assert 'Cookie' not in request.headers
        assert 'private-default' not in str(request.headers)
        assert 'preSign' not in request.url and 'signIn' not in request.url
    assert all(call[2]['allow_redirects'] is False for call in im.calls)
    assert 'private' not in json.dumps(result) and 'im-test-user' not in json.dumps(result)
    assert result['sourceCoverage']['scope'] == 'current_conversations'


def test_history_finds_activity_when_latest_chat_message_has_no_attachment():
    encoded = protobuf_message(notice())
    result, _, _, adapter = run([conversations([channel()]), history([encoded])])
    assert [item['activeId'] for item in result['activities']] == ['300']
    posted = adapter.sent[1][0]
    assert posted.method == 'POST'
    assert urlsplit(posted.url).path == '/cx-dev/cxstudy/users/im-test-user/messageroaming'
    assert json.loads(posted.body) == {
        'queue': '900@conference.easemob.com', 'start': -1, 'pull_number': 20,
        'is_positive': False, 'msgType': '', 'end': -1,
        'startTime': None, 'endTime': None, 'userId': None}


def test_normal_text_optional_extensions_and_non_attendance_metadata_are_normal_skips():
    row = channel()
    payload = json.loads(row['meta']['payload'])
    payload['ext'] = None
    row['meta']['payload'] = json.dumps(payload)
    messages = [protobuf_message(None), protobuf_message({'attachmentType': 18, 'att_clouddisk': {}}),
                protobuf_message(notice(), namespace=2), protobuf_message(notice(), body_kind=4)]
    result, *_ = run([conversations([row]), history(messages)])
    assert result['activities'] == [] and result['status'] == 'ok'
    assert result['sourceCoverage']['malformedMessages'] == 0
    assert result['sourceCoverage']['malformedBySource'] == {'latest': 0, 'history': 0}
    assert result['sourceCoverage']['malformedReasons'] == {}


def test_latest_and_history_duplicate_activity_is_reported_once_without_guessing_rules():
    activity = notice(otherId=0, ifphoto=0, ifopenAddress=0, openCheckFaceFlag=0, status=1)
    result, *_ = run([conversations([channel(activity)]), history([protobuf_message(activity)])])
    assert len(result['activities']) == 1
    row = result['activities'][0]
    assert (row['kind'], row['status'], row['autoEligible']) == ('unknown', 'unknown', False)


@pytest.mark.parametrize(('label', 'kind'), [
    ('签到', 'ordinary_candidate'), ('普通签到', 'ordinary_candidate'),
    ('二维码签到', 'qr'), ('未观察的类型', 'unknown'),
])
def test_platform_labels_only_classify_candidates_and_never_confirm_missing_requirements(label, kind):
    result, *_ = run([conversations([channel(notice(atypeName=label))]), history()])
    row = result['activities'][0]
    assert row['kind'] == kind and row['status'] == 'unknown' and row['autoEligible'] is False
    assert 'unknown' in row['requirements']


def test_unrecognized_label_shape_remains_unknown_without_aborting_other_notices():
    result, *_ = run([conversations([channel(notice(atypeName=['unexpected']))]), history()])
    assert result['activities'][0]['kind'] == 'unknown'


@pytest.mark.parametrize('title', ['签到', '普通签到', '二维码签到'])
def test_course_notice_and_teacher_pc_url_never_become_group_activities(title):
    activity = notice(title=title, url='https://mobilelearn.chaoxing.com/newsign/preSign?activeId=300&chatId=900',
                      pcUrl='https://mobilelearn.chaoxing.com/sign/preSign?activeId=300&chatId=900')
    result, *_ = run([conversations([channel(activity)]), history([protobuf_message(activity)])])
    assert result['activities'] == []


@pytest.mark.parametrize('url', [
    'https://evil.example/sign/preSign?activeId=300&chatId=900',
    'https://mobilelearn.chaoxing.com.evil.example/sign/preSign?activeId=300&chatId=900',
    'http://mobilelearn.chaoxing.com/sign/preSign?activeId=300&chatId=900',
    'https://user@mobilelearn.chaoxing.com/sign/preSign?activeId=300&chatId=900',
    'https://mobilelearn.chaoxing.com:8443/sign/preSign?activeId=300&chatId=900',
    'https://mobilelearn.chaoxing.com/sign/preSign?activeId=301&chatId=900',
    'https://mobilelearn.chaoxing.com/sign/preSign?activeId=300&chatId=900&%61ctiveId=300',
    'https://mobilelearn.chaoxing.com/sign/preSign?activeId=300&chatId=900&chatId=901',
    'https://mobilelearn.chaoxing.com/sign/preSign?activeId=300',
    'https://mobilelearn.chaoxing.com/sign/preSign?activeId=300&chatId=',
    'https://mobilelearn.chaoxing.com/sign/preSign?activeId=300&chatId=%0Asecret',
    'https://mobilelearn.chaoxing.com/sign/preSign?activeId=300&chatId=%FF',
    'https://mobilelearn.chaoxing.com/sign/preSign?activeId=300&chatId=%ZZ',
    'https://mobilelearn.chaoxing.com/sign/preSign?activeId=300&chatId=900#private',
    '//mobilelearn.chaoxing.com/sign/preSign?activeId=300&chatId=900',
])
def test_untrusted_ambiguous_or_identity_changing_notice_is_not_returned_or_visited(url):
    result, _, _, adapter = run([conversations([channel(notice(url=url))]), history()])
    assert result['activities'] == []
    assert len(adapter.sent) == 2
    assert all('preSign' not in request.url for request, _ in adapter.sent)


@pytest.mark.parametrize('change', [
    {'aid': None}, {'aid': True}, {'aid': 300.0}, {'id': '301'}, {'atype': 4}, {'type': 4},
])
def test_notification_requires_matching_activity_identifiers_and_attendance_type(change):
    result, *_ = run([conversations([channel(notice(**change))]), history()])
    assert result['activities'] == []


@pytest.mark.parametrize('token', [
    {'result': 1, 'msg': {**TOKEN['msg'], 'puid': '102'}},
    {'result': 1, 'msg': {**TOKEN['msg'], 'puid': None}},
])
def test_token_identity_mismatch_stops_before_constructing_im_transport(token):
    official = OfficialClient(token)
    with patch.object(groups.requests, 'Session') as session:
        with pytest.raises(groups.GroupNotificationError) as error:
            groups.fetch_group_notifications(official, '101')
        assert error.value.code == 'identity_mismatch'
        session.assert_not_called()
    assert 'private' not in str(error.value) + repr(error.value)


@pytest.mark.parametrize('changes', [
    {'uid': '../foreign'}, {'uid': '..'}, {'uid': False}, {'hxToken': 'secret\nheader'}, {'hxToken': ''},
])
def test_malformed_token_or_user_path_never_reaches_im(changes):
    official = OfficialClient({'result': 1, 'msg': {**TOKEN['msg'], **changes}})
    with patch.object(groups.requests, 'Session') as session:
        with pytest.raises(groups.GroupNotificationError):
            groups.fetch_group_notifications(official, '101')
        session.assert_not_called()


def test_missing_verified_passport_identity_does_not_request_a_token():
    official = OfficialClient()
    with pytest.raises(groups.GroupNotificationError) as error:
        groups.fetch_group_notifications(official, None)
    assert error.value.code == 'identity_incomplete' and not official.calls


def test_token_redirect_is_not_followed_and_never_transfers_cookies_to_sdk():
    official = OfficialClient(status=302)
    with patch.object(groups.requests, 'Session') as session:
        with pytest.raises(groups.GroupNotificationError):
            groups.fetch_group_notifications(official, '101')
        session.assert_not_called()
    assert official.calls[0][2]['follow_redirects'] is False


def test_im_redirect_is_not_followed_or_interpreted_as_an_empty_success():
    with pytest.raises(groups.GroupNotificationError) as error:
        run([response('private-upstream-text', 302, {'Location': 'https://evil.example/private'})])
    assert 'private' not in str(error.value)


def test_history_failure_preserves_latest_results_and_declares_partial_coverage():
    result, _, _, adapter = run([conversations([channel(notice())]), requests.Timeout('private-url-token')])
    assert len(result['activities']) == 1 and result['status'] == 'partial'
    assert result['sourceCoverage']['historyGroupsFailed'] == 1
    assert len(adapter.sent) == 2 and 'private' not in json.dumps(result)


def test_conversation_pages_use_only_returned_cursor_and_each_known_group_history():
    result, _, _, adapter = run([
        conversations([channel(notice(), '900')], 'next-page'),
        conversations([channel(notice('301', '901'), '901')]), history(), history()])
    assert [item['activeId'] for item in result['activities']] == ['300', '301']
    assert result['sourceCoverage']['channelPages'] == 2
    assert parse_qs(urlsplit(adapter.sent[1][0].url).query)['cursor'] == ['next-page']
    assert [request.method for request, _ in adapter.sent] == ['GET', 'GET', 'POST', 'POST']


def test_repeating_cursor_stops_paging_and_marks_partial():
    result, _, _, adapter = run([
        conversations([channel(notice())], 'same'), conversations([], 'same'), history()])
    assert result['status'] == 'partial'
    assert result['sourceCoverage']['conversationListComplete'] is False
    assert len(adapter.sent) == 3


def test_channel_and_history_group_bounds_are_reported(monkeypatch):
    monkeypatch.setattr(groups, 'MAX_CHANNEL_PAGES', 1)
    monkeypatch.setattr(groups, 'MAX_HISTORY_GROUPS', 1)
    result, _, _, adapter = run([
        conversations([channel(notice()), channel(notice('301', '901'), '901')], 'more'), history()])
    assert result['status'] == 'partial' and len(adapter.sent) == 2
    assert result['sourceCoverage']['historyGroupsLimited'] == 1
    assert result['sourceCoverage']['conversationListComplete'] is False


def test_earlier_history_is_not_claimed_to_have_been_crawled():
    result, *_ = run([conversations([channel()]), history(last=False)])
    assert result['status'] == 'partial'
    assert result['sourceCoverage']['groupsWithEarlierHistory'] == 1
    assert result['sourceCoverage']['historyMessageLimit'] == 20


def test_cross_group_latest_message_is_rejected_but_own_history_can_recover_notice():
    row = channel(notice())
    row['meta']['to'] = '901@conference.easemob.com'
    result, *_ = run([conversations([row]), history([protobuf_message(notice())])])
    assert [item['activeId'] for item in result['activities']] == ['300']
    assert result['status'] == 'partial' and result['sourceCoverage']['malformedMessages'] == 1
    assert result['sourceCoverage']['malformedBySource'] == {'latest': 1, 'history': 0}
    assert result['sourceCoverage']['malformedReasons'] == {'latest:latest_target': 1}


def test_observed_official_app_key_prefixed_latest_jid_matches_the_same_group_only():
    row = channel(notice(atypeName='签到'))
    row['meta']['to'] = 'cx-dev#cxstudy_900@conference.easemob.com'
    result, *_ = run([conversations([row]), history()])
    assert result['activities'][0]['activeId'] == '300'
    assert result['activities'][0]['kind'] == 'ordinary_candidate'
    assert result['sourceCoverage']['malformedMessages'] == 0
    assert result['status'] == 'ok'


@pytest.mark.parametrize('target', [
    'other#cxstudy_900@conference.easemob.com',
    'cx-dev#other_900@conference.easemob.com',
    'cx-dev#cxstudy_901@conference.easemob.com',
    'cx-dev#cxstudy_900@conference.easemob.com.evil.example',
    'prefix-cx-dev#cxstudy_900@conference.easemob.com',
    '900@conference.easemob.com.evil.example',
])
def test_wrong_namespace_group_or_confusing_jid_suffix_is_rejected(target):
    row = channel(notice())
    row['meta']['to'] = target
    result, *_ = run([conversations([row]), history()])
    assert result['activities'] == [] and result['status'] == 'partial'
    assert result['sourceCoverage']['malformedReasons'] == {'latest:latest_target': 1}


def test_history_targets_system_namespaces_and_read_receipts_are_not_attendance():
    messages = [protobuf_message(notice(), group='901'),
                protobuf_message(notice(), namespace=2), protobuf_message(notice(), body_kind=4),
                protobuf_message(notice())]
    result, *_ = run([conversations([channel()]), history(messages)])
    assert len(result['activities']) == 1
    assert result['sourceCoverage']['malformedMessages'] == 1
    assert result['sourceCoverage']['malformedBySource'] == {'latest': 0, 'history': 1}
    assert result['sourceCoverage']['malformedReasons'] == {'history:history_target': 1}


def test_malformed_protobuf_is_bounded_and_does_not_hide_other_valid_messages():
    duplicate_namespace = protobuf_message(notice(), extra_meta=scalar(5, 1))
    invalid = ['not/base64!', base64.b64encode(b'\x80' * 11).decode(),
               base64.b64encode(b'\x32\x7fshort').decode(), duplicate_namespace,
               'A' * (groups.MAX_MESSAGE_BYTES * 2)]
    result, *_ = run([conversations([channel()]), history(invalid + [protobuf_message(notice())])])
    assert len(result['activities']) == 1
    assert result['sourceCoverage']['malformedMessages'] == len(invalid)
    assert result['status'] == 'partial'


def test_private_chats_and_unknown_channel_types_are_not_read_as_group_history():
    row = channel(notice())
    row['session_type'] = 'chat'
    unknown = dict(row, session_type='unknown')
    result, _, _, adapter = run([conversations([row, unknown])])
    assert result['activities'] == [] and len(adapter.sent) == 1
    assert result['status'] == 'partial'


def test_empty_valid_conversations_are_distinct_from_malformed_source():
    result, _, _, adapter = run([conversations()])
    assert result['activities'] == [] and result['status'] == 'ok' and len(adapter.sent) == 1
    with pytest.raises(groups.GroupNotificationError):
        run([{'data': {'private-error': 'private-token'}}])


def test_duplicate_json_keys_and_oversized_http_payload_are_not_accepted():
    for body in ('{"data":{"channel_infos":[]},"data":{"channel_infos":[]}}',
                 'x' * (groups.MAX_JSON_BYTES + 1)):
        with pytest.raises(groups.GroupNotificationError):
            run([response(body)])
