"""Only the profile's own PUID and owned courses may reach history reads."""
from urllib.parse import parse_qs, urlsplit

import pytest
import requests

import upstream
from chaoxing_direct import ChaoxingDirectClient
from test_upstream import InMemoryAdapter, PROFILE, COOKIES, IDENTITY


def gateway(monkeypatch, responses):
    session = requests.Session()
    adapter = InMemoryAdapter(responses)
    session.mount('https://', adapter)
    monkeypatch.setattr(upstream, 'ChaoxingDirectClient', lambda *args, **kwargs:
                        ChaoxingDirectClient('offline-history-test', session=session))
    instance = upstream.UpstreamGateway()
    instance.get_account_details = lambda cookies, identity: {
        'courses': [{'courseId': '400', 'classId': '500', 'name': '已选课程'}],
        'sources': [{'id': 'normal', 'status': 'ok'}], 'cookies': cookies}
    return instance, adapter


def test_history_never_enters_a_signing_flow_and_uses_verified_personal_identity(monkeypatch):
    instance, adapter = gateway(monkeypatch, [{'body': PROFILE}, {'body': {'result': 1, 'data': {
        'total': 1, 'page': 1, 'list': [{'activeid': '600', 'activeType': 2, 'status': 2,
            'userStatus': 1, 'other_id': 0, 'name': '历史签到', 'submittime': '2026-09-20 17:01:41'}]}}}])
    result = instance.get_sign_history(COOKIES, IDENTITY, 'local-account')
    assert len(result['records']) == 1 and result['records'][0]['recordedAt'] == '2026-09-20T09:01:41Z'
    assert result['sources'][-1]['activityScope'] == 'group'
    assert result['sources'][-1]['status'] == 'unavailable'
    assert [urlsplit(r.url).path for r in adapter.sent] == [
        '/apis/login/userLogin4Uname.do', '/v2/apis/signStat/getActiveList']
    assert all(r.method == 'GET' for r in adapter.sent)
    query = parse_qs(urlsplit(adapter.sent[-1].url).query)
    assert query['puid'] == ['101'] and query['courseId'] == ['400'] and query['classId'] == ['500']


def test_identity_mismatch_prevents_all_history_requests(monkeypatch):
    instance, adapter = gateway(monkeypatch, [{'body': PROFILE}])
    with pytest.raises(upstream.UpstreamError) as error:
        instance.get_sign_history(COOKIES, {**IDENTITY, 'uid': '999'}, 'local-account')
    assert error.value.code == 'identity_mismatch' and len(adapter.sent) == 1


def test_history_rate_limit_propagates_without_retry_or_successful_empty(monkeypatch):
    instance, adapter = gateway(monkeypatch, [{'body': PROFILE}, {'body': {'result': 0, 'errorMsg': '请勿频繁操作'}}])
    with pytest.raises(upstream.UpstreamError) as error:
        instance.get_sign_history(COOKIES, IDENTITY, 'local-account')
    assert error.value.code == 'upstream_rate_limited' and len(adapter.sent) == 2

