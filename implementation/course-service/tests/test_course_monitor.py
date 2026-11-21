"""Persistent monitor behavior using synthetic accounts and controlled gateways."""
from copy import deepcopy
from pathlib import Path
from types import SimpleNamespace
import json
import sys

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from course_monitor import CourseMonitor
from upstream import UpstreamError
from vault import Vault, WorkspaceRef, iso, timestamp


COOKIE = 'synthetic-monitor-cookie-never-public'
COURSE = {'courseId': '456', 'classId': '789', 'name': '测试课程'}
ACTIVITY = {'activeId': '123', 'courseId': '456', 'classId': '789',
            'name': '测试签到', 'kind': 'ordinary', 'status': 'active', 'autoEligible': True}


def account(account_id='account-1', uid='1001'):
    return {'id': account_id, 'name': '测试同学', 'maskedIdentifier': '188****1111',
            'school': '测试学校', 'status': 'active', 'color': 'green',
            'grant': {'days': None, 'grantedAt': iso(), 'expiresAt': None, 'scopes': ['session', 'profile']},
            'lastLogin': iso(), 'sessionExpiresAt': None,
            '_identity': {'uid': uid, 'fid': '2001', 'name': '测试同学'},
            '_identifier': '18800001111', '_cookies': [{'name': 'session', 'value': COOKIE}]}


class Gateway:
    def __init__(self):
        self.activities = [deepcopy(ACTIVITY)]
        self.sign_calls = []
        self.detail_calls = 0
        self.activity_calls = 0
        self.details_error = self.activities_error = self.sign_error = None
        self.activities_hook = self.sign_hook = None
        self.sign_result = {'status': 'reported_success', 'message': '上游报告成功', 'submitted': True}

    def get_account_details(self, cookies, identity):
        self.detail_calls += 1
        if self.details_error:
            raise self.details_error
        return {'courses': [deepcopy(COURSE)], 'sources': [{'id': 'normal', 'status': 'ok'}],
                'cookies': deepcopy(cookies)}

    def get_course_activities(self, cookies, identity, course):
        self.activity_calls += 1
        if self.activities_hook:
            self.activities_hook()
        if self.activities_error:
            raise self.activities_error
        return {'activities': deepcopy(self.activities), 'sources': [{'id': 'normal', 'status': 'ok'}],
                'cookies': deepcopy(cookies)}

    def sign(self, cookies, identity, payload):
        self.sign_calls.append({'cookies': deepcopy(cookies), 'identity': deepcopy(identity), 'payload': deepcopy(payload)})
        if self.sign_hook:
            self.sign_hook()
        if self.sign_error:
            raise self.sign_error
        return deepcopy(self.sign_result)


@pytest.fixture
def monitor(tmp_path):
    vault = Vault(tmp_path / 'monitor-data')
    token, state = vault.create()
    state['accounts'] = [account()]
    vault.save(token, state)
    gateway = Gateway()
    manager = CourseMonitor(vault, gateway)
    subject = SimpleNamespace(vault=vault, token=token, manager=manager, gateway=gateway,
                              account_id='account-1', ref=WorkspaceRef(vault.owner(token)),
                              key=(vault.owner(token), 'account-1'))
    yield subject
    manager.close()


def enable(subject, auto=True):
    with subject.vault.lock(subject.token):
        subject.manager.configure(subject.token, subject.account_id, True, auto, 10)


def run(subject):
    subject.manager._worker(subject.ref, subject.account_id, subject.key)
    return subject.manager.snapshot(subject.token, subject.vault.load(subject.token))


def test_monitor_is_off_until_configured_and_default_snapshot_contains_no_secrets(monitor):
    snapshot = monitor.manager.snapshot(monitor.token, monitor.vault.load(monitor.token))
    assert snapshot['accounts'][0]['enabled'] is False
    assert snapshot['accounts'][0]['autoSign'] is False
    assert snapshot['accounts'][0]['intervalSeconds'] == 10
    assert monitor.vault.monitored_workspaces() == []
    assert snapshot['tasks'] == []
    assert COOKIE not in json.dumps(snapshot)
    assert monitor.token not in json.dumps(snapshot)


def test_restart_restores_enabled_workspace_without_storing_browser_token(monitor):
    enable(monitor)
    restored = Vault(monitor.vault.path.parent)
    refs = restored.monitored_workspaces()
    assert refs == [monitor.ref]
    assert restored.load(refs[0]) == restored.load(monitor.token)
    manager = CourseMonitor(restored, monitor.gateway)
    try:
        manager._worker(refs[0], monitor.account_id, monitor.key)
        snapshot = manager.snapshot(monitor.token, restored.load(monitor.token))
        assert snapshot['tasks'][0]['status'] == 'reported_success'
        assert len(monitor.gateway.sign_calls) == 1
    finally:
        manager.close()
    with restored.connection() as db:
        rows = db.execute('SELECT owner,ciphertext FROM course_monitors').fetchall()
    assert rows[0]['owner'] == restored.owner(monitor.token)
    assert monitor.token.encode() not in rows[0]['ciphertext']
    assert COOKIE.encode() not in rows[0]['ciphertext']


def test_active_ordinary_auto_signs_once_and_uses_private_safety_flag(monitor):
    enable(monitor)
    first = run(monitor)
    second = run(monitor)
    assert len(monitor.gateway.sign_calls) == 1
    assert monitor.gateway.sign_calls[0]['payload'] == {
        'kind': 'ordinary', 'activeId': '123', 'courseId': '456', 'classId': '789', '_autoSign': True}
    assert first['tasks'][0]['status'] == second['tasks'][0]['status'] == 'reported_success'
    assert len(second['tasks']) == 1
    assert monitor.gateway.detail_calls == 1


def test_monitor_only_can_later_enable_auto_sign_for_existing_task(monitor):
    enable(monitor, auto=False)
    assert run(monitor)['tasks'][0]['status'] == 'monitoring_only'
    assert monitor.gateway.sign_calls == []
    enable(monitor, auto=True)
    assert run(monitor)['tasks'][0]['status'] == 'reported_success'
    assert len(monitor.gateway.sign_calls) == 1


@pytest.mark.parametrize('kind,status', [
    ('unknown', 'active'), ('qr', 'active'), ('photo', 'active'), ('face', 'active'),
    ('position', 'active'), ('code', 'active'), ('located_code', 'active'),
    ('ordinary', 'scheduled'), ('ordinary', 'unknown'), ('ordinary', 'ended'),
])
def test_only_confirmed_current_ordinary_activities_may_auto_sign(monitor, kind, status):
    monitor.gateway.activities = [{**ACTIVITY, 'kind': kind, 'status': status, 'autoEligible': False}]
    enable(monitor)
    snapshot = run(monitor)
    assert monitor.gateway.sign_calls == []
    assert all(t['status'] not in ('reported_success', 'pending') for t in snapshot['tasks'])


@pytest.mark.parametrize('field,value', [('courseId', '999'), ('classId', '999'), ('activeId', 'not-an-id')])
def test_activity_context_mismatch_is_never_submitted(monitor, field, value):
    monitor.gateway.activities = [{**ACTIVITY, field: value}]
    enable(monitor)
    assert run(monitor)['tasks'] == []
    assert monitor.gateway.sign_calls == []


def test_ordinary_label_without_explicit_auto_eligibility_is_not_enough(monitor):
    monitor.gateway.activities = [{**ACTIVITY, 'autoEligible': False}]
    enable(monitor)
    snapshot = run(monitor)
    assert monitor.gateway.sign_calls == []
    assert snapshot['tasks'][0]['status'] == 'needs_input'


@pytest.mark.parametrize('status', ['reported_success', 'already_signed', 'pending', 'outcome_unknown', 'unknown', 'http_error'])
def test_manual_success_or_unconfirmed_submission_prevents_auto_duplicate(monitor, status):
    job = {'requestId': 'manual-request-1', 'accountId': monitor.account_id, 'activeId': '123',
           'status': status, 'submitted': True, 'message': '原签到结果'}
    monitor.vault.save_job(monitor.token, job, 'synthetic-fingerprint')
    enable(monitor)
    task = run(monitor)['tasks'][0]
    assert task['status'] == status
    assert task['requestId'] == job['requestId']
    assert monitor.gateway.sign_calls == []


def test_timeout_after_submission_is_persistent_unknown_and_never_retried(monitor):
    monitor.gateway.sign_error = UpstreamError('sign_timeout', '提交超时', details={'submitted': True, 'phase': 'submit'})
    enable(monitor)
    first = run(monitor)
    assert first['tasks'][0]['status'] == 'outcome_unknown'
    assert run(monitor)['tasks'][0]['status'] == 'outcome_unknown'
    assert len(monitor.gateway.sign_calls) == 1
    restored_manager = CourseMonitor(monitor.vault, monitor.gateway)
    try:
        restored_manager._worker(monitor.ref, monitor.account_id, monitor.key)
        assert len(monitor.gateway.sign_calls) == 1
    finally:
        restored_manager.close()


def test_course_failure_backs_off_that_course_without_delaying_other_courses(monitor):
    monitor.gateway.activities_error = UpstreamError('unavailable', '活动来源暂不可用')
    enable(monitor)
    snapshot = run(monitor)
    config = snapshot['accounts'][0]
    assert config['status'] == 'partial'
    assert config['courseErrors'][0]['message'] == '活动来源暂不可用'
    assert 299 <= timestamp(config['nextCheckAt']) - timestamp(config['lastCheckedAt']) <= 301
    assert monitor.gateway.sign_calls == []
    run(monitor)
    assert monitor.gateway.activity_calls == 1


def test_detail_partial_failure_stays_visible_even_if_list_sources_succeeded(monitor):
    def partial(*args):
        return {'activities': [], 'sources': [{'id': 'normal', 'status': 'ok'}],
                'status': 'partial', 'detailErrors': 2}

    monitor.gateway.get_course_activities = partial
    enable(monitor)
    config = run(monitor)['accounts'][0]
    assert config['status'] == 'partial'
    assert config['courseErrors']
    assert monitor.gateway.sign_calls == []


def test_expired_session_disables_monitor_and_clears_cookies(monitor):
    monitor.gateway.activities_error = UpstreamError('session_expired', '请重新登录')
    enable(monitor)
    config = run(monitor)['accounts'][0]
    row = monitor.vault.load(monitor.token)['accounts'][0]
    assert config['status'] == 'login_required'
    assert config['enabled'] is False
    assert config['nextCheckAt'] is None
    assert row['status'] == 'expired'
    assert row['_cookies'] == []
    assert monitor.vault.monitored_workspaces() == []


@pytest.mark.parametrize('action', ['disable', 'revoke', 'relogin'])
def test_consent_or_session_change_during_activity_read_prevents_signing(monitor, action):
    enable(monitor)

    def change():
        if action == 'disable':
            monitor.manager.configure(monitor.token, monitor.account_id, False, False, 10)
        else:
            state = monitor.vault.load(monitor.token)
            row = state['accounts'][0]
            if action == 'revoke':
                row.update(grant=None, status='unauthorized', _cookies=[])
            else:
                row['lastLogin'] = '2099-01-01T00:00:00Z'
            monitor.vault.save(monitor.token, state)

    monitor.gateway.activities_hook = change
    snapshot = run(monitor)
    assert monitor.gateway.sign_calls == []
    assert snapshot['tasks'] == []


def test_other_workspace_has_no_monitor_settings_or_results(monitor):
    enable(monitor)
    run(monitor)
    other_token, other_state = monitor.vault.create()
    # Even colliding account IDs never cross browser workspaces.
    other_state['accounts'] = [account()]
    monitor.vault.save(other_token, other_state)
    snapshot = monitor.manager.snapshot(other_token, monitor.vault.load(other_token))
    assert snapshot['accounts'][0]['enabled'] is False
    assert snapshot['tasks'] == []
    assert monitor.vault.get_job(other_token, run(monitor)['tasks'][0]['requestId'])[0] is None


def test_account_removed_during_submission_does_not_resurrect_jobs_or_tasks(monitor):
    enable(monitor)

    def remove():
        state = monitor.vault.load(monitor.token)
        state['accounts'] = []
        monitor.vault.remove_jobs(monitor.token, [monitor.account_id])
        monitor.manager.remove(monitor.token, [monitor.account_id])
        monitor.vault.save(monitor.token, state)

    monitor.gateway.sign_hook = remove
    snapshot = run(monitor)
    assert len(monitor.gateway.sign_calls) == 1
    assert snapshot['accounts'] == snapshot['tasks'] == []
    assert monitor.vault.load_monitor(monitor.token) == {'accounts': {}, 'tasks': []}
    with monitor.vault.connection() as db:
        assert db.execute('SELECT COUNT(*) FROM sign_jobs').fetchone()[0] == 0


def test_sign_rotated_cookies_are_not_overwritten_by_earlier_activity_read(monitor):
    monitor.gateway.sign_result['cookies'] = [{'name': 'session', 'value': 'rotated-after-sign'}]
    enable(monitor)
    run(monitor)
    assert monitor.vault.load(monitor.token)['accounts'][0]['_cookies'] == monitor.gateway.sign_result['cookies']


def test_activity_read_preserves_concurrent_cookie_update_and_sign_uses_it(monitor):
    concurrent_cookies = [{'name': 'session', 'value': 'updated-by-concurrent-manual-sign'}]

    def update_cookies():
        with monitor.vault.lock(monitor.token):
            state = monitor.vault.load(monitor.token)
            state['accounts'][0]['_cookies'] = deepcopy(concurrent_cookies)
            monitor.vault.save(monitor.token, state)

    monitor.gateway.activities_hook = update_cookies
    enable(monitor)
    assert run(monitor)['tasks'][0]['status'] == 'reported_success'
    assert monitor.gateway.sign_calls[0]['cookies'] == concurrent_cookies
    assert monitor.vault.load(monitor.token)['accounts'][0]['_cookies'] == concurrent_cookies


def test_sign_result_preserves_newer_concurrent_cookie_update(monitor):
    concurrent_cookies = [{'name': 'session', 'value': 'updated-by-concurrent-qr-sign'}]
    monitor.gateway.sign_result['cookies'] = [{'name': 'session', 'value': 'stale-monitor-response'}]

    def update_cookies():
        with monitor.vault.lock(monitor.token):
            state = monitor.vault.load(monitor.token)
            state['accounts'][0]['_cookies'] = deepcopy(concurrent_cookies)
            monitor.vault.save(monitor.token, state)

    monitor.gateway.sign_hook = update_cookies
    enable(monitor)
    assert run(monitor)['tasks'][0]['status'] == 'reported_success'
    assert monitor.vault.load(monitor.token)['accounts'][0]['_cookies'] == concurrent_cookies


@pytest.mark.parametrize('source', ['group', 'course'])
def test_stale_session_failure_preserves_newer_cookies_and_resumes(monitor, source):
    fresh_cookies = [{'name': 'session', 'value': 'newer-official-session'}]
    def update_cookies():
        with monitor.vault.lock(monitor.token):
            state = monitor.vault.load(monitor.token)
            state['accounts'][0]['_cookies'] = deepcopy(fresh_cookies)
            monitor.vault.save(monitor.token, state)
    if source == 'group':
        def expired_group(*args):
            update_cookies()
            raise UpstreamError('session_expired', '旧群读取会话已过期。', 401)
        monitor.gateway.get_group_activities = expired_group
    else:
        monitor.gateway.activities_hook = update_cookies
        monitor.gateway.activities_error = UpstreamError('session_expired', '旧课程读取会话已过期。', 401)
    enable(monitor)
    snapshot = run(monitor)
    current = monitor.vault.load(monitor.token)['accounts'][0]
    assert current['status'] == 'active' and current['_cookies'] == fresh_cookies
    assert snapshot['accounts'][0]['enabled']
    assert monitor.gateway.sign_calls == []
    monitor.gateway.get_group_activities = lambda *args: {'activities': [], 'groupCount': 0, 'status': 'ok'}
    monitor.gateway.activities_hook = monitor.gateway.activities_error = None
    assert run(monitor)['tasks'][0]['status'] == 'reported_success'
    assert monitor.gateway.sign_calls[0]['cookies'] == fresh_cookies


def test_restart_reconciles_pending_task_with_interrupted_sign_job(monitor):
    enable(monitor)
    snapshot = run(monitor)
    task = snapshot['tasks'][0]
    data = monitor.vault.load_monitor(monitor.token)
    data['tasks'][0].update(status='pending', message='等待提交回执')
    monitor.vault.save_monitor(monitor.token, data)
    job, fingerprint = monitor.vault.get_job(monitor.token, task['requestId'])
    job.update(status='pending', message='等待提交回执')
    monitor.vault.save_job(monitor.token, job, fingerprint)
    monitor.vault.interrupt_batches()
    restored_manager = CourseMonitor(monitor.vault, monitor.gateway)
    try:
        # A dashboard read must show the durable outcome even before next poll.
        restored = restored_manager.snapshot(monitor.token, monitor.vault.load(monitor.token))
        assert restored['tasks'][0]['status'] == 'outcome_unknown'
        assert len(monitor.gateway.sign_calls) == 1
    finally:
        restored_manager.close()


def test_oldest_due_accounts_get_worker_slots_before_newer_checks(monitor):
    state = monitor.vault.load(monitor.token)
    state['accounts'] = [account(f'account-{i}') for i in range(1, 6)]
    monitor.vault.save(monitor.token, state)
    for row in state['accounts']:
        monitor.manager.configure(monitor.token, row['id'], True, False, 10)
    data = monitor.vault.load_monitor(monitor.token)
    for config in data['accounts'].values():
        config['nextCheckAt'] = '2001-01-01T00:00:00Z'
    data['accounts']['account-5']['nextCheckAt'] = '2000-01-01T00:00:00Z'
    monitor.vault.save_monitor(monitor.token, data)
    submitted = []
    monitor.manager.pool.submit = lambda fn, ref, account_id, key: submitted.append(account_id)
    monitor.manager.schedule()
    assert len(submitted) == 4
    assert submitted[0] == 'account-5'


def test_storage_failure_during_completion_always_releases_worker_slot(monitor, monkeypatch):
    enable(monitor)
    monitor.manager.running.add(monitor.key)
    load_monitor = monitor.vault.load_monitor
    calls = 0

    def flaky(token):
        nonlocal calls
        calls += 1
        if calls > 1:
            raise RuntimeError('synthetic storage failure')
        return load_monitor(token)

    monkeypatch.setattr(monitor.vault, 'load_monitor', flaky)
    monitor.gateway.details_error = UpstreamError('unavailable', '课程暂不可用')
    try:
        monitor.manager._worker(monitor.ref, monitor.account_id, monitor.key)
    except RuntimeError:
        pass
    assert monitor.key not in monitor.manager.running


def test_job_storage_failure_does_not_leave_an_unrecoverable_pending_task(monitor, monkeypatch):
    enable(monitor)
    save_job = monitor.vault.save_job

    def fail(*args):
        raise RuntimeError('synthetic job write failure before submission')

    monkeypatch.setattr(monitor.vault, 'save_job', fail)
    snapshot = run(monitor)
    assert monitor.gateway.sign_calls == []
    assert not any(t.get('requestId') and monitor.vault.get_job(monitor.token, t['requestId'])[0] is None
                   for t in snapshot['tasks'])
    monkeypatch.setattr(monitor.vault, 'save_job', save_job)
    assert run(monitor)['tasks'][0]['status'] == 'reported_success'
    assert len(monitor.gateway.sign_calls) == 1

def test_course_photo_activity_uses_photo_pool_when_enabled(monitor, monkeypatch):
    """课程拍照签到：走照片池通道，不经过普通签到网关。"""
    import course_monitor as monitor_module
    monitor.gateway.activities = [{**ACTIVITY, 'kind': 'photo', 'status': 'active',
                                   'requirements': ['photo'], 'autoEligible': False}]
    calls = {}

    def fake_auto_sign(cookies, identity, active_id, course_id, class_id, data_dir, key,
                       dry_run=True, window=90):
        calls.update(active_id=active_id, course_id=course_id, class_id=class_id,
                     dry_run=dry_run, window=window, key=key)
        return {'status': 'reported_success', 'reasonOfPick': 'course:random', 'candidates': 2,
                'picked': {'activeId': active_id, 'objectId': 'a' * 32, 'title': '测试签到'},
                'submit': {'submitted': True},
                'verify': {'signed': True, 'matches': True}}

    monkeypatch.setattr(monitor_module.photo_pool, 'auto_sign', fake_auto_sign)
    enable(monitor)
    snapshot = run(monitor)
    task = snapshot['tasks'][0]
    assert calls['dry_run'] is False
    assert calls['active_id'] == ACTIVITY['activeId']
    assert calls['key'] == 'uid1001'
    assert task['status'] == 'reported_success'
    job, _ = monitor.vault.get_job(monitor.ref, task['requestId'])
    assert job['photo']['reasonOfPick'] == 'course:random'
    assert job['photo']['candidates'] == 2
    assert monitor.gateway.sign_calls == []


def test_course_photo_activity_stays_manual_without_auto_sign(monitor):
    monitor.gateway.activities = [{**ACTIVITY, 'kind': 'photo', 'status': 'active',
                                   'requirements': ['photo'], 'autoEligible': False}]
    enable(monitor, auto=False)
    snapshot = run(monitor)
    assert snapshot['tasks'][0]['status'] == 'monitoring_only'
    assert monitor.gateway.sign_calls == []
