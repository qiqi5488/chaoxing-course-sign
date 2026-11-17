"""One-click ordinary jobs: reservation, scope isolation and queued revocation."""
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
import threading
import time

import pytest
from fastapi.testclient import TestClient

from test_course_monitor import monitor, enable, run, COURSE, ACTIVITY
from test_monitor_http import api
from test_server import ORIGIN
from course_monitor import session_stamp
from vault import WorkspaceRef, iso


GROUP = {'activityScope': 'group', 'activeId': '123', 'chatId': 'test-chat',
         'name': '普通群签到', 'kind': 'ordinary_candidate', 'status': 'unknown', 'autoEligible': False}


def settled(subject, request_id):
    deadline = time.monotonic() + 3
    while time.monotonic() < deadline:
        job, _ = subject.vault.get_job(subject.token, request_id)
        if job and job['status'] != 'pending':
            return job
        time.sleep(.01)
    raise AssertionError('Ordinary job did not finish')


def test_manual_ordinary_reserves_request_before_202_snapshot_without_enabling_auto(monitor):
    enable(monitor, auto=False)
    task = run(monitor)['tasks'][0]
    result = monitor.manager.sign_now(monitor.token, task['id'])
    current = next(t for t in result['tasks'] if t['id'] == task['id'])
    assert current['requestId']
    assert result['accounts'][0]['autoSign'] is False
    assert settled(monitor, current['requestId'])['status'] == 'reported_success'
    assert len(monitor.gateway.sign_calls) == 1


def test_concurrent_clicks_share_one_job_and_never_submit_twice(monitor):
    enable(monitor, auto=False)
    task = run(monitor)['tasks'][0]
    release = threading.Event()
    monitor.gateway.sign_hook = lambda: release.wait(3)
    try:
        with ThreadPoolExecutor(max_workers=6) as callers:
            replies = list(callers.map(lambda _: monitor.manager.sign_now(monitor.token, task['id']), range(6)))
        ids = {next(t for t in r['tasks'] if t['id'] == task['id'])['requestId'] for r in replies}
        assert len(ids) == 1
    finally:
        release.set()
    job = settled(monitor, ids.pop())
    assert job['status'] == 'reported_success'
    assert len(monitor.gateway.sign_calls) == 1


def test_manual_task_still_works_with_monitor_off(monitor):
    enable(monitor, auto=False)
    task = run(monitor)['tasks'][0]
    monitor.manager.configure(monitor.token, monitor.account_id, False, False, 10)
    result = monitor.manager.sign_now(monitor.token, task['id'])
    request_id = result['tasks'][0]['requestId']
    assert settled(monitor, request_id)['status'] == 'reported_success'
    assert result['accounts'][0]['enabled'] is False


def test_same_numeric_course_and_group_activity_get_separate_jobs(monitor):
    monitor.gateway.get_group_activities = lambda cookies, identity: {
        'activities': [deepcopy(GROUP)], 'groupCount': 1, 'status': 'ok', 'message': '已读取群通知'}
    enable(monitor)
    result = run(monitor)
    assert len(result['tasks']) == 2
    assert {t['activityScope'] for t in result['tasks']} == {'group', 'course'}
    assert len({t['requestId'] for t in result['tasks']}) == 2
    assert len(monitor.gateway.sign_calls) == 2
    assert {c['payload'].get('activityScope', 'course') for c in monitor.gateway.sign_calls} == {'course', 'group'}
    assert result['accounts'][0]['groupCount'] == 1
    run(monitor)
    assert len(monitor.gateway.sign_calls) == 2


def test_qr_group_keeps_its_kind_when_using_the_group_direct_entry(monitor):
    monitor.gateway.get_group_activities = lambda cookies, identity: {
        'activities': [{**GROUP, 'kind': 'qr'}], 'groupCount': 1, 'status': 'ok'}
    monitor.gateway.activities = []
    enable(monitor)
    result = run(monitor)
    assert result['tasks'][0]['kind'] == 'qr'
    assert len(monitor.gateway.sign_calls) == 1
    assert monitor.gateway.sign_calls[0]['payload'] == {
        'kind': 'qr', 'activityScope': 'group', 'activeId': '123'}
    assert result['tasks'][0]['activityScope'] == 'group'
    assert result['tasks'][0]['status'] == 'reported_success'


@pytest.mark.parametrize('change', ['revoke', 'remove', 'relogin'])
def test_queued_manual_job_checks_consent_again_before_any_http(monitor, change):
    enable(monitor, auto=False)
    task = run(monitor)['tasks'][0]
    release = threading.Event()
    started = [threading.Event() for _ in range(4)]
    def occupy(event):
        event.set()
        release.wait(3)
    blockers = [monitor.manager.pool.submit(occupy, event) for event in started]
    assert all(event.wait(1) for event in started)
    try:
        result = monitor.manager.sign_now(monitor.token, task['id'])
        request_id = result['tasks'][0]['requestId']
        with monitor.vault.lock(monitor.token):
            state = monitor.vault.load(monitor.token)
            if change == 'remove':
                state['accounts'] = []
                monitor.vault.remove_jobs(monitor.token, [monitor.account_id])
                monitor.manager.remove(monitor.token, [monitor.account_id])
            elif change == 'revoke':
                state['accounts'][0].update(grant=None, _cookies=[], status='unauthorized')
            else:
                state['accounts'][0]['lastLogin'] = iso(time.time() + 1)
            monitor.vault.save(monitor.token, state)
    finally:
        release.set()
        for future in blockers:
            future.result(timeout=2)
    if change == 'remove':
        # Draining the single queued action must not recreate removed data.
        monitor.manager.pool.shutdown(wait=True)
        assert monitor.vault.get_job(monitor.token, request_id)[0] is None
    else:
        job = settled(monitor, request_id)
        assert job['status'] == 'skipped' and job['submitted'] is False
    assert monitor.gateway.sign_calls == []


def test_sign_api_accepts_group_without_course_context_and_keeps_scope_separate(api):
    url = f'/api/accounts/{api.account}/sign'
    group = api.client.post(url, headers={**api.headers, 'Idempotency-Key': 'ordinary-group-123'},
                           json={'kind': 'ordinary', 'activityScope': 'group', 'activeId': '123'})
    assert group.status_code == 200, group.text
    assert group.json()['activityScope'] == 'group'
    course = api.client.post(url, headers={**api.headers, 'Idempotency-Key': 'ordinary-course-123'},
                            json={'kind': 'ordinary', 'activeId': '123', 'courseId': '456', 'classId': '789'})
    assert course.status_code == 200, course.text
    assert api.gateway.sign_calls == 2
    again = api.client.post(url, headers={**api.headers, 'Idempotency-Key': 'ordinary-group-123'},
                           json={'kind': 'ordinary', 'activityScope': 'group', 'activeId': '123'})
    assert again.json() == group.json()
    assert api.gateway.sign_calls == 2


@pytest.mark.parametrize('extra', [{'kind': 'photo'}, {'uid': '123'}, {'courseId': '456'},
                                 {'enc': 'untrusted-qr'}, {'location': {'latitude': 1, 'longitude': 2, 'address': 'x'}}])
def test_group_direct_input_does_not_accept_other_types_or_forged_context(api, extra):
    response = api.client.post(f'/api/accounts/{api.account}/sign',
        headers={**api.headers, 'Idempotency-Key': 'invalid-group-123'},
        json={'kind': 'ordinary', 'activityScope': 'group', 'activeId': '123', **extra})
    assert response.status_code == 422
    assert api.gateway.sign_calls == 0


def test_manual_monitor_route_requires_csrf_and_owns_task(api):
    vault, manager = api.app.state.vault, api.app.state.monitor_manager
    token = api.client.cookies.get(api.app.state.cookie_name)
    manager.configure(token, api.account, True, False, 10)
    row = vault.load(token)['accounts'][0]
    config = vault.load_monitor(token)['accounts'][api.account]
    manager._activity(WorkspaceRef(vault.owner(token)), api.account, config['version'], session_stamp(row), COURSE, ACTIVITY)
    task = manager.snapshot(token, vault.load(token))['tasks'][0]
    url = f'/api/monitor/tasks/{task["id"]}/sign'
    assert api.client.post(url, json={}).status_code == 403
    result = api.client.post(url, json={}, headers=api.headers)
    assert result.status_code == 202, result.text
    assert result.json()['tasks'][0]['requestId']
    with TestClient(api.app, base_url=ORIGIN) as other:
        csrf = other.get('/api/bootstrap').json()['csrfToken']
        response = other.post(url, json={}, headers={'Origin': ORIGIN, 'X-CSRF-Token': csrf})
        assert response.status_code == 404
