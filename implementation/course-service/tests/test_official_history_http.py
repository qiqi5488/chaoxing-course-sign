"""Official history is read-only, session-scoped and distinct from local jobs."""
import pytest
from fastapi.testclient import TestClient

from test_monitor_http import api
from test_server import ORIGIN
from test_sign_history import save
from upstream import UpstreamError


def upstream_result(account_id, cookies):
    return {'records': [dict(accountId=account_id, activeId=str(100 + i), activityScope='course',
                status='already_signed', statusLabel='教师代签' if i == 0 else '已签', name='以往签到',
                courseName='学习通课程', kind='ordinary', message='学习通记录：已签',
                recordedAt=f'2026-10-0{i+1}T00:00:00Z', source='chaoxing', private='must-not-expose')
            for i in range(3)], 'sources': [dict(activityScope='course', name='学习通课程', status='ok', count=3),
            dict(activityScope='group', name='群聊历史', status='unavailable', count=0,
                 message='尚未确认群聊历史', private='must-not-expose')], 'cookies': cookies}


def install(api, action=None):
    calls = []
    def read(cookies, identity, account_id):
        calls.append((identity, account_id))
        if action:
            action()
        return upstream_result(account_id, cookies)
    api.gateway.get_sign_history = read
    return calls


def test_default_history_queries_official_records_and_paginates_one_snapshot(api):
    save(api, 'local-only', '999')
    calls = install(api)
    url = f'/api/accounts/{api.account}/sign-records'
    first = api.client.get(url + '?limit=2').json()
    assert first['source'] == 'chaoxing' and first['total'] == 3
    assert [r['activeId'] for r in first['records']] == ['102', '101']
    assert first['counts'] == {'course': 3, 'group': 0}
    assert first['sources'][1]['status'] == 'unavailable'
    second = api.client.get(url, params={'offset': 2, 'limit': 2, 'snapshot': first['snapshot']})
    assert second.status_code == 200 and second.json()['records'][0]['statusLabel'] == '教师代签'
    assert second.json()['nextOffset'] is None and len(calls) == 1
    assert 'must-not-expose' not in second.text and 'cookies' not in second.text
    assert api.client.get(url + '?offset=2&snapshot=wrong').status_code == 409
    refreshed = api.client.get(url + '?refresh=true').json()
    assert refreshed['snapshot'] != first['snapshot'] and len(calls) == 2
    assert api.client.get(url + '?source=local').json()['total'] == 1
    assert api.gateway.sign_calls == 0


def test_official_history_requires_active_own_account_even_when_cached(api):
    calls = install(api)
    url = f'/api/accounts/{api.account}/sign-records'
    assert api.client.get(url).status_code == 200
    with TestClient(api.app, base_url=ORIGIN) as other:
        assert other.get(url).status_code == 401
        other.get('/api/bootstrap')
        assert other.get(url).status_code == 404
    assert api.client.post(f'/api/accounts/{api.account}/revoke', headers=api.headers).status_code == 200
    assert api.client.get(url).status_code == 409
    assert len(calls) == 1


def test_history_does_not_overwrite_cookies_refreshed_by_another_request(api):
    token = api.client.cookies.get(api.app.state.cookie_name)
    vault = api.app.state.vault
    def change():
        state = vault.load(token)
        state['accounts'][0]['_cookies'][0]['value'] = 'newer-cookie'
        vault.save(token, state)
    install(api, change)
    assert api.client.get(f'/api/accounts/{api.account}/sign-records').status_code == 200
    assert vault.load(token)['accounts'][0]['_cookies'][0]['value'] == 'newer-cookie'


def test_history_rejects_results_for_a_login_changed_during_the_read(api):
    token = api.client.cookies.get(api.app.state.cookie_name)
    vault = api.app.state.vault
    def change():
        state = vault.load(token)
        state['accounts'][0]['lastLogin'] = '2026-11-14T00:00:00Z'
        vault.save(token, state)
    install(api, change)
    response = api.client.get(f'/api/accounts/{api.account}/sign-records')
    assert response.status_code == 409 and response.json()['error']['code'] == 'account_changed'


def test_failed_upstream_query_is_not_cached_as_empty_history(api):
    def fail(*args):
        raise UpstreamError('history_unavailable', '记录暂不可用')
    api.gateway.get_sign_history = fail
    url = f'/api/accounts/{api.account}/sign-records'
    assert api.client.get(url).status_code == 502
    calls = install(api)
    assert api.client.get(url).json()['total'] == 3 and len(calls) == 1

