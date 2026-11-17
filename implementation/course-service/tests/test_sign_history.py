"""Saved successful receipts stay account-scoped and never cause upstream traffic."""
import pytest
from fastapi.testclient import TestClient

from test_monitor_http import api
from test_server import ORIGIN


def save(api, request_id, active_id='123', **changes):
    token = api.client.cookies.get(api.app.state.cookie_name)
    job = {'requestId': request_id, 'accountId': api.account, 'activeId': active_id,
           'status': 'reported_success', 'message': '学习通返回成功',
           'createdAt': '2026-11-12T08:00:00Z', 'completedAt': '2026-11-12T08:01:00Z', **changes}
    api.app.state.vault.save_job(token, job, 'synthetic-fingerprint')
    return token


def test_history_uses_persistent_jobs_filters_uncertainty_and_merges_only_same_scope_activity(api):
    token = save(api, 'old-course', name='原始活动名', kind='ordinary')
    save(api, 'new-course', status='already_signed', completedAt='2026-11-13T09:00:00Z',
         challenge={'validate': 'private-challenge'}, diagnostics={'raw': 'private-diagnostic'})
    save(api, 'same-id-group', activityScope='group', completedAt='2026-11-13T08:00:00Z')
    save(api, 'older-than-monitor', '456', completedAt='2025-01-01T08:00:00Z',
         name='以往课程签到', courseName='以往课程', kind='gesture')
    save(api, 'unknown', '789', status='outcome_unknown', message='页面写了签到成功但尚未核验')
    save(api, 'pending', '790', status='pending')
    save(api, 'other-account', '999', accountId='not-this-account')
    api.app.state.vault.save_monitor(token, {'accounts': {}, 'tasks': [
        {'id': 'task', 'accountId': api.account, 'activeId': '123', 'activityScope': 'course',
         'name': '当前课程签到', 'courseName': '当前课程', 'kind': 'ordinary'},
        {'id': 'unconfirmed', 'accountId': api.account, 'activeId': '791', 'status': 'reported_success'},
    ]})
    response = api.client.get(f'/api/accounts/{api.account}/sign-records?source=local')
    assert response.status_code == 200
    result = response.json()
    assert result['total'] == 3 and result['nextOffset'] is None
    assert [row['requestId'] for row in result['records']] == ['new-course', 'same-id-group', 'older-than-monitor']
    assert result['records'][0]['name'] == '当前课程签到'
    assert result['records'][0]['status'] == 'already_signed'
    assert result['records'][1]['activityScope'] == 'group'
    assert result['records'][2]['name'] == '以往课程签到'
    assert 'private-' not in response.text and 'challenge' not in response.text and 'diagnostics' not in response.text
    assert api.gateway.sign_calls == api.gateway.check_calls == api.gateway.details_calls == 0


def test_history_paginates_confirmed_activities_in_receipt_order(api):
    for number in range(3):
        save(api, f'job-{number}', str(100 + number), completedAt=f'2026-11-13T0{number}:00:00Z')
    url = f'/api/accounts/{api.account}/sign-records?source=local'
    first = api.client.get(url + '&offset=0&limit=2').json()
    second = api.client.get(url + '&offset=2&limit=2').json()
    assert first['total'] == second['total'] == 3
    assert first['nextOffset'] == 2 and second['nextOffset'] is None
    assert [row['requestId'] for row in first['records'] + second['records']] == ['job-2', 'job-1', 'job-0']


def test_history_keeps_saved_receipts_after_revocation_and_blocks_other_workspaces(api):
    save(api, 'saved-receipt')
    assert api.client.post(f'/api/accounts/{api.account}/revoke', headers=api.headers).status_code == 200
    url = f'/api/accounts/{api.account}/sign-records?source=local'
    assert api.client.get(url).json()['total'] == 1
    with TestClient(api.app, base_url=ORIGIN) as other:
        assert other.get(url).status_code == 401
        other.get('/api/bootstrap')
        assert other.get(url).status_code == 404
    assert api.gateway.sign_calls == 0


@pytest.mark.parametrize('query', ['limit=0', 'limit=101', 'offset=-1'])
def test_history_rejects_invalid_page_bounds(api, query):
    assert api.client.get(f'/api/accounts/{api.account}/sign-records?source=local&{query}').status_code == 422


def test_history_empty_is_explicit(api):
    result = api.client.get(f'/api/accounts/{api.account}/sign-records?source=local').json()
    assert result['records'] == [] and result['total'] == 0 and result['nextOffset'] is None


@pytest.mark.parametrize('kind', ['ordinary', 'code'])
def test_new_sign_receipts_keep_their_kind_without_requiring_monitor_task_metadata(api, kind):
    response = api.client.post(f'/api/accounts/{api.account}/sign',
        headers={**api.headers, 'Idempotency-Key': 'history-test-receipt'},
        json={'kind': kind, 'activeId': '123', 'courseId': '456', 'classId': '789'})
    assert response.status_code == 200
    history = api.client.get(f'/api/accounts/{api.account}/sign-records?source=local').json()
    assert history['records'][0]['kind'] == kind
    assert history['total'] == 1 and api.gateway.sign_calls == 1
