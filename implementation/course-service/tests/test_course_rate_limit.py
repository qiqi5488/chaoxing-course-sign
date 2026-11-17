"""Official throttling defers one account without clearing its consent."""
import time

import pytest

from course_monitor import CourseMonitor, cooldown_seconds
from test_course_monitor import monitor, enable, run, COURSE
from test_monitor_http import api
from upstream import UpstreamError
from vault import Vault, iso, timestamp


def limited(seconds=120):
    return UpstreamError('upstream_rate_limited', '学习通提示请勿频繁操作。', 429,
                         {'retryAfterSeconds': seconds, 'phase': 'activity', 'submitted': False})


def test_rate_limit_stops_remaining_courses_and_blocks_the_next_cycle(monitor):
    details = monitor.gateway.get_account_details
    monitor.gateway.get_account_details = lambda *args: {
        **details(*args), 'courses': [COURSE, {**COURSE, 'courseId': '457'}, {**COURSE, 'courseId': '458'}]}
    monitor.gateway.activities_error = limited()
    enable(monitor)
    snapshot = run(monitor)
    config = snapshot['accounts'][0]
    assert config['status'] == 'rate_limited' and config['enabled'] and config['autoSign']
    assert config['intervalSeconds'] == 10 and 118 <= cooldown_seconds(config) <= 120
    assert 299 <= timestamp(config['nextCheckAt']) - timestamp(config['lastCheckedAt']) <= 301
    assert monitor.gateway.activity_calls == 1 and monitor.gateway.sign_calls == []
    run(monitor)
    assert monitor.gateway.activity_calls == 1
    assert monitor.vault.load(monitor.token)['accounts'][0]['status'] == 'active'


def test_settings_and_manual_group_check_cannot_bypass_course_cooldown(monitor):
    enable(monitor)
    monitor.manager.defer(monitor.token, monitor.account_id, 180)
    deadline = monitor.vault.load_monitor(monitor.token)['accounts'][monitor.account_id]['cooldownUntil']
    monitor.manager.configure(monitor.token, monitor.account_id, True, False, 60)
    config = monitor.manager.snapshot(monitor.token, monitor.vault.load(monitor.token))['accounts'][0]
    assert config['cooldownUntil'] == deadline and timestamp(config['nextCheckAt']) < timestamp(deadline)
    assert config['enabled'] and not config['autoSign'] and config['intervalSeconds'] == 60
    assert monitor.manager.check_soon(monitor.token, monitor.account_id)
    run(monitor)
    assert monitor.gateway.activity_calls == 0
    monitor.manager.configure(monitor.token, monitor.account_id, False, False, 60)
    monitor.manager.configure(monitor.token, monitor.account_id, True, False, 60)
    assert monitor.vault.load_monitor(monitor.token)['accounts'][monitor.account_id]['cooldownUntil'] == deadline


def test_cooling_manual_task_does_not_reserve_a_job(monitor):
    enable(monitor, auto=False)
    task = run(monitor)['tasks'][0]
    monitor.manager.defer(monitor.token, monitor.account_id, 120)
    with pytest.raises(UpstreamError) as error:
        monitor.manager.sign_now(monitor.token, task['id'])
    assert error.value.code == 'upstream_rate_limited'
    assert not monitor.vault.load_monitor(monitor.token)['tasks'][0].get('requestId')
    assert monitor.gateway.sign_calls == []


def test_saved_cooldown_survives_restart_and_expiry_allows_discovery(monitor):
    enable(monitor, auto=False)
    monitor.manager.defer(monitor.token, monitor.account_id, 120)
    restored = CourseMonitor(Vault(monitor.vault.path.parent), monitor.gateway)
    scheduled = []
    restored.pool.submit = lambda *args: scheduled.append(args)
    try:
        restored.schedule()
        assert scheduled == []
    finally:
        restored.close()
    data = monitor.vault.load_monitor(monitor.token)
    data['accounts'][monitor.account_id].update(cooldownUntil=iso(time.time() - 1), nextCheckAt=iso(time.time() - 1))
    monitor.vault.save_monitor(monitor.token, data)
    snapshot = run(monitor)
    assert monitor.gateway.activity_calls == 1
    assert snapshot['accounts'][0]['cooldownUntil'] is None


def test_rate_limit_during_sign_defers_following_account_requests(monitor):
    monitor.gateway.sign_error = limited(90)
    enable(monitor)
    snapshot = run(monitor)
    assert snapshot['tasks'][0]['status'] == 'rejected'
    assert snapshot['accounts'][0]['status'] == 'rate_limited'
    assert 88 <= cooldown_seconds(snapshot['accounts'][0]) <= 90
    run(monitor)
    assert len(monitor.gateway.sign_calls) == 1


def test_http_sign_and_check_observe_cooldown_before_upstream(api):
    token = api.client.cookies.get(api.app.state.cookie_name)
    manager = api.app.state.monitor_manager
    manager.configure(token, api.account, True, True, 10)
    manager.defer(token, api.account, 120)
    response = api.client.post(f'/api/accounts/{api.account}/sign',
        json={'kind': 'ordinary', 'activeId': '123', 'courseId': '456', 'classId': '789'},
        headers={**api.headers, 'Idempotency-Key': 'cooling-direct-request'})
    assert response.status_code == 429 and response.json()['error']['code'] == 'upstream_rate_limited'
    assert api.gateway.sign_calls == 0
    assert api.app.state.vault.get_job(token, 'cooling-direct-request')[0] is None
    check = api.client.post(f'/api/accounts/{api.account}/monitor/check', json={}, headers=api.headers)
    assert check.status_code == 202
    assert cooldown_seconds(api.app.state.vault.load_monitor(token)['accounts'][api.account]) > 0


def test_http_first_throttle_saves_receipt_and_prevents_another_attempt(api):
    token = api.client.cookies.get(api.app.state.cookie_name)
    api.gateway.sign_error = limited()
    body = {'kind': 'ordinary', 'activeId': '123', 'courseId': '456', 'classId': '789'}
    first = api.client.post(f'/api/accounts/{api.account}/sign', json=body,
        headers={**api.headers, 'Idempotency-Key': 'throttled-first-request'})
    assert first.status_code == 200 and first.json()['submitted'] is False
    config = api.app.state.vault.load_monitor(token)['accounts'][api.account]
    assert cooldown_seconds(config) > 0
    second = api.client.post(f'/api/accounts/{api.account}/sign', json={**body, 'activeId': '124'},
        headers={**api.headers, 'Idempotency-Key': 'throttled-second-request'})
    assert second.status_code == 429 and api.gateway.sign_calls == 1
    replay = api.client.get('/api/sign-requests/throttled-first-request')
    assert replay.status_code == 200 and replay.json() == first.json()


@pytest.mark.parametrize('enabled', [True, False])
def test_inflight_throttle_survives_new_settings_without_replacing_them(monitor, enabled):
    enable(monitor)
    monitor.gateway.activities_hook = lambda: monitor.manager.configure(
        monitor.token, monitor.account_id, enabled, False, 60)
    monitor.gateway.activities_error = limited()
    config = run(monitor)['accounts'][0]
    assert config['enabled'] is enabled and config['autoSign'] is False and config['intervalSeconds'] == 60
    assert cooldown_seconds(config) > 0
    assert config['status'] == ('rate_limited' if enabled else 'disabled')
    if enabled:
        assert 299 <= timestamp(config['nextCheckAt']) - timestamp(config['lastCheckedAt']) <= 301
    else:
        assert config['nextCheckAt'] is None


@pytest.mark.parametrize('change', ['relogin', 'remove'])
def test_old_session_throttle_does_not_recreate_or_defer_new_session(monitor, change):
    enable(monitor)
    def update():
        state = monitor.vault.load(monitor.token)
        if change == 'remove':
            state['accounts'] = []
            monitor.manager.remove(monitor.token, [monitor.account_id])
        else:
            state['accounts'][0]['lastLogin'] = iso(time.time() + 1)
        monitor.vault.save(monitor.token, state)
    monitor.gateway.activities_hook = update
    monitor.gateway.activities_error = limited()
    snapshot = run(monitor)
    if change == 'remove':
        assert snapshot['accounts'] == [] and monitor.vault.load_monitor(monitor.token)['accounts'] == {}
    else:
        assert cooldown_seconds(snapshot['accounts'][0]) == 0


@pytest.mark.parametrize('automatic', [True, False])
def test_course_cooldown_keeps_group_discovery_and_signing_available(monitor, automatic):
    from test_ordinary_tasks import GROUP, settled
    monitor.gateway.get_group_activities = lambda *args: {
        'activities': [GROUP], 'status': 'ok', 'groupCount': 1}
    enable(monitor, auto=automatic)
    monitor.manager.defer(monitor.token, monitor.account_id, 120)
    snapshot = run(monitor)
    task = snapshot['tasks'][0]
    if not automatic:
        snapshot = monitor.manager.sign_now(monitor.token, task['id'])
        task = snapshot['tasks'][0]
        assert settled(monitor, task['requestId'])['status'] == 'reported_success'
    assert monitor.gateway.sign_calls[0]['payload']['activityScope'] == 'group'
    assert monitor.gateway.activity_calls == monitor.gateway.detail_calls == 0
    assert cooldown_seconds(snapshot['accounts'][0]) > 0


@pytest.mark.parametrize('kind', ['ordinary', 'qr', 'gesture'])
def test_course_cooldown_does_not_block_group_direct_api(api, kind):
    token = api.client.cookies.get(api.app.state.cookie_name)
    api.app.state.monitor_manager.defer(token, api.account, 120)
    response = api.client.post(f'/api/accounts/{api.account}/sign',
        json={'activityScope': 'group', 'kind': kind, 'activeId': '123'},
        headers={**api.headers, 'Idempotency-Key': 'group-during-course-cooldown'})
    assert response.status_code == 200 and response.json()['status'] == 'reported_success'
    assert api.gateway.sign_calls == 1


def test_group_session_expiry_still_stops_monitor_while_courses_are_cooling(monitor):
    def expired(*args):
        raise UpstreamError('session_expired', '学习通会话失效。', 401)
    monitor.gateway.get_group_activities = expired
    enable(monitor)
    monitor.manager.defer(monitor.token, monitor.account_id, 120)
    snapshot = run(monitor)
    assert snapshot['accounts'][0]['status'] == 'login_required'
    assert not snapshot['accounts'][0]['enabled']
    assert monitor.vault.load(monitor.token)['accounts'][0]['status'] == 'expired'
