"""Group QR submission contract, using synthetic in-memory HTTPS responses."""
from pathlib import Path
import sys
from unittest.mock import patch
from urllib.parse import parse_qs, urlsplit

import pytest
import requests

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from test_upstream import InMemoryAdapter, PROFILE, COOKIES, IDENTITY
import upstream
from chaoxing_direct import ChaoxingDirectClient

QR = 'SIGNIN:aid=300&source=1&Code=ABCD1234'
CONTEXT = '<input id="activeId" value="300"><input id="puid" value="101">'
SUCCESS = CONTEXT + '<div class="qd_Success">签到成功</div>'
REDIRECTS = [
    {'status': 302, 'headers': {'Location': 'https://mobilelearn.chaoxing.com/sign/preSign?activeId=300&chatId=server-chat&uid=101'}},
    {'status': 302, 'headers': {'Location': '/widget/sign/group/pcStuSignGroupController/preSign?activeId=300'}},
]


def run_group(responses, identity=IDENTITY):
    session = requests.Session()
    adapter = InMemoryAdapter(responses)
    session.mount('https://', adapter)
    client = ChaoxingDirectClient('offline-group-test', session=session)
    with patch.object(upstream, 'ChaoxingDirectClient', return_value=client):
        result = upstream.UpstreamGateway().sign_qr(COOKIES, identity, QR)
    return result, adapter


@pytest.mark.parametrize(('body', 'expected', 'submitted'), [
    (SUCCESS, 'reported_success', True),
    ('<h1>二维码已过期</h1>', 'rejected', True),
    ('<script>"qd_Success 签到成功"</script>', 'unknown', True),
    (CONTEXT + '<form><button>签到</button></form>', 'need_context', False),
    (CONTEXT + '<input id="activeStatus" value="1"><div class="qd_Success">'
     '<p class="Photo_p">此签到需要手机拍照</p></div>', 'photo_required', False),
])
def test_group_follows_only_native_cookie_authenticated_resolution(body, expected, submitted):
    result, adapter = run_group([{'body': PROFILE}, *REDIRECTS, {'body': body}])
    assert result['status'] == expected
    assert result['submitted'] is submitted
    assert len(adapter.sent) == 4
    business = adapter.sent[1:]
    assert [urlsplit(sent.url).path for sent in business] == [
        '/newsign/studentSign', '/sign/preSign',
        '/widget/sign/group/pcStuSignGroupController/preSign']
    assert parse_qs(urlsplit(business[0].url).query) == {'rcode': [QR], 'uid': ['101']}
    assert parse_qs(urlsplit(business[1].url).query) == {
        'activeId': ['300'], 'chatId': ['server-chat'], 'uid': ['101']}
    assert parse_qs(urlsplit(business[2].url).query) == {'activeId': ['300']}
    assert all(sent.method == 'GET' and sent.body is None for sent in adapter.sent)
    assert all('_uid=100' in sent.headers['Cookie'] for sent in adapter.sent)
    assert all('/widget/sign/signIn' not in sent.url and 'ewnCode' not in sent.url for sent in business)


@pytest.mark.parametrize(('stage', 'location'), [
    (0, 'https://evil.example/sign/preSign?activeId=300&chatId=server-chat&uid=101'),
    (0, '/sign/preSign?activeId=300&chatId=server-chat&uid=100'),
    (0, '/widget/sign/signIn?activeId=300&ewnCode=ABCD1234'),
    (1, '/widget/sign/group/pcStuSignGroupController/preSign?activeId=301'),
    (1, '/other?activeId=300'),
    (2, '/newsign/studentSign'),
])
def test_unknown_or_identity_changing_redirect_is_never_followed(stage, location):
    result, adapter = run_group([{'body': PROFILE}, *REDIRECTS[:stage],
        {'status': 302, 'headers': {'Location': location}}])
    assert result['status'] == 'unknown'
    assert result['reasonCode'] == 'unsupported_group_redirect'
    assert len(adapter.sent) == 2 + stage


@pytest.mark.parametrize('stage', [0, 1, 2])
def test_resolution_timeout_is_uncertain_without_retries(stage):
    result, adapter = run_group([{'body': PROFILE}, *REDIRECTS[:stage],
                               requests.ReadTimeout('private-token')])
    assert result['status'] == 'outcome_unknown'
    assert result['submitted'] is True
    assert 'private-token' not in result['message']
    assert len(adapter.sent) == 2 + stage


def test_group_identity_mismatch_stops_before_the_get_submission():
    session = requests.Session()
    adapter = InMemoryAdapter([{'body': PROFILE}])
    session.mount('https://', adapter)
    client = ChaoxingDirectClient('offline-group-test', session=session)
    with patch.object(upstream, 'ChaoxingDirectClient', return_value=client), pytest.raises(upstream.UpstreamError) as error:
        upstream.UpstreamGateway().sign_qr(COOKIES, {**IDENTITY, 'uid': '999'}, QR)
    assert error.value.code == 'identity_mismatch'
    assert error.value.details['submitted'] is False
    assert len(adapter.sent) == 1
