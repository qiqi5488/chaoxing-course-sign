"""Deterministic realtime scheduling with fake gateways and no real network."""
from copy import deepcopy
import json
import threading
import time

import pytest
import requests

from test_course_monitor import monitor, enable, run, ACTIVITY, COOKIE
from course_monitor import CourseMonitor, session_stamp, defer_list_config, cooldown_seconds
from upstream import UpstreamError
from vault import Vault, iso, timestamp


@pytest.fixture
def subject(monitor, monkeypatch):
    # No receiver process, discovery request or HTTP can escape this test module.
    monkeypatch.setattr(monitor.manager.receivers, 'sync', lambda specs: None)
    def forbidden(*args, **kwargs):
        raise AssertionError('Realtime monitor tests must not open a real HTTP connection')
    monkeypatch.setattr(requests.Session, 'request', forbidden)
    return monitor


def config(subject):
    return subject.vault.load_monitor(subject.token)['accounts'][subject.account_id]


def spec(subject):
    row = subject.vault.load(subject.token)['accounts'][0]
    return {'ref': subject.ref, 'accountId': subject.account_id, 'stamp': session_stamp(row)}


def notice(key='notice-one', active_id='123', **changes):
    return {'activeId': active_id, 'courseId': '456', 'classId': '789', 'name': '实时课程通知',
            'kind': 'unknown', 'status': 'unknown', 'requirements': ['unknown'],
            'autoEligible': False, 'activityScope': 'course',
            'notificationType': 'course_attendance', 'noticeKey': key, **changes}


def push(subject, candidate=None, *, context=None):
    subject.manager._im_event(context or spec(subject),
        {'type': 'notice', 'candidate': candidate or notice()})


def set_config(subject, **changes):
    with subject.vault.lock(subject.token):
        data = subject.vault.load_monitor(subject.token)
        data['accounts'][subject.account_id].update(changes)
        subject.vault.save_monitor(subject.token, data)


def due(subject):
    with subject.vault.lock(subject.token):
        data = subject.vault.load_monitor(subject.token)
        for item in data['accounts'][subject.account_id].get('inbox', []):
            item['retryAt'] = iso(time.time() - 1)
        subject.vault.save_monitor(subject.token, data)


def process(subject):
    subject.manager._notice_worker(subject.ref, subject.account_id, subject.key)


def details(subject, *, hook=None, unknown=False):
    calls = []
    def verify(cookies, identity, course, candidates):
        calls.append({'cookies': deepcopy(cookies), 'identity': deepcopy(identity),
                      'course': deepcopy(course), 'candidates': deepcopy(candidates)})
        if hook:
            hook(candidates)
        rows = [{**ACTIVITY, 'activeId': item['activeId'], 'name': item['name'],
                 'activityScope': 'course', 'notificationType': 'course_attendance'} for item in candidates]
        if unknown:
            rows = [{**row, 'kind': 'unknown', 'status': 'unknown', 'autoEligible': False,
                     'requirements': ['unknown']} for row in rows]
        return {'activities': rows, 'detailErrors': int(unknown), 'cookies': deepcopy(cookies)}
    subject.gateway.get_course_notification_activities = verify
    return calls


def test_notice_is_processed_while_all_full_scan_workers_are_busy(subject, monkeypatch):
    enable(subject)
    calls = details(subject)
    release, full_entered = threading.Event(), threading.Event()
    occupied = [threading.Event() for _ in range(3)]
    futures, notice_futures = [], []
    submit = subject.manager.notice_pool.submit
    def record_submit(*args, **kwargs):
        future = submit(*args, **kwargs)
        notice_futures.append(future)
        return future
    monkeypatch.setattr(subject.manager.notice_pool, 'submit', record_submit)
    def occupy(entered):
        entered.set()
        assert release.wait(60), 'full scan release was not signaled'
    subject.gateway.activities = []
    subject.gateway.activities_hook = lambda: occupy(full_entered)
    try:
        futures.extend(subject.manager.pool.submit(occupy, event) for event in occupied)
        assert all(event.wait(15) for event in occupied)
        with subject.manager.guard:
            subject.manager.running.add(subject.key)
        full = subject.manager.pool.submit(subject.manager._worker, subject.ref, subject.account_id, subject.key)
        futures.append(full)
        assert full_entered.wait(15)
        push(subject)
        subject.manager.schedule()
        assert len(notice_futures) == 1
        notice_futures[0].result(timeout=30)
        assert not full.done() and not release.is_set()
        assert len(calls) == 1 and len(subject.gateway.sign_calls) == 1
        assert config(subject)['inbox'] == []
    finally:
        release.set()
        for future in futures:
            future.result(timeout=30)
    assert len(subject.gateway.sign_calls) == 1


def test_a_notice_arriving_during_verification_survives_the_first_batch_ack(subject, monkeypatch):
    enable(subject)
    set_config(subject, nextCheckAt=iso(time.time() + 3600))
    entered, release = threading.Event(), threading.Event()
    def pause(candidates):
        if candidates[0]['noticeKey'] == 'notice-one':
            entered.set()
            assert release.wait(60)
    details(subject, hook=pause)
    futures = []
    submit = subject.manager.notice_pool.submit
    def record_submit(*args, **kwargs):
        future = submit(*args, **kwargs)
        futures.append(future)
        return future
    monkeypatch.setattr(subject.manager.notice_pool, 'submit', record_submit)
    try:
        push(subject)
        subject.manager.schedule()
        assert entered.wait(15)
        push(subject, notice('notice-two', '124'))
        subject.manager.schedule()
        assert len(futures) == 1
    finally:
        release.set()
        for future in list(futures):
            future.result(timeout=30)
    saved = config(subject)
    assert [item['candidate']['noticeKey'] for item in saved['inbox']] == ['notice-two']
    assert saved['seenNotices'] == ['notice-one']
    subject.manager.schedule()
    assert len(futures) == 2
    futures[1].result(timeout=30)
    assert [call['payload']['activeId'] for call in subject.gateway.sign_calls] == ['123', '124']
    assert config(subject)['inbox'] == []
    assert subject.gateway.activity_calls == 0


def test_duplicate_message_keys_and_distinct_messages_for_one_activity_never_resubmit(subject):
    enable(subject)
    calls = details(subject)
    push(subject)
    push(subject)
    assert len(config(subject)['inbox']) == 1
    process(subject)
    push(subject)
    assert config(subject)['inbox'] == []
    push(subject, notice('different-message-same-activity'))
    process(subject)
    assert len(calls) == 2 and len(subject.gateway.sign_calls) == 1
    assert config(subject)['seenNotices'] == ['notice-one', 'different-message-same-activity']


def test_queue_is_encrypted_survives_restart_and_is_absent_from_public_snapshot(subject):
    enable(subject)
    details(subject)
    push(subject, notice('private-realtime-notice-key'))
    snapshot = subject.manager.snapshot(subject.token, subject.vault.load(subject.token))
    assert 'private-realtime-notice-key' not in json.dumps(snapshot) and COOKIE not in json.dumps(snapshot)
    with subject.vault.connection() as db:
        stored = db.execute('SELECT ciphertext FROM course_monitors WHERE owner=?',
                            (subject.vault.owner(subject.token),)).fetchone()
    assert b'private-realtime-notice-key' not in bytes(stored['ciphertext'])
    restored = CourseMonitor(Vault(subject.vault.path.parent), subject.gateway)
    try:
        restored._notice_worker(subject.ref, subject.account_id, subject.key)
    finally:
        restored.close()
    assert len(subject.gateway.sign_calls) == 1 and config(subject)['inbox'] == []


@pytest.mark.parametrize('change', ['disable', 'revoke', 'relogin', 'remove'])
def test_session_or_consent_change_during_membership_read_stops_all_later_work(subject, change):
    enable(subject)
    calls = details(subject)
    context = spec(subject)
    push(subject, context=context)
    original = subject.gateway.get_account_details
    def change_session(cookies, identity):
        result = original(cookies, identity)
        with subject.vault.lock(subject.token):
            state = subject.vault.load(subject.token)
            row = state['accounts'][0]
            if change == 'disable':
                subject.manager.configure(subject.token, subject.account_id, False, False, 10)
            elif change == 'revoke':
                row.update(grant=None, _cookies=[], status='unauthorized')
            elif change == 'relogin':
                row.update(lastLogin='new-login-generation', _cookies=[{'name': 'session', 'value': 'new-session'}])
            else:
                subject.manager.remove(subject.token, [subject.account_id])
                subject.vault.remove_jobs(subject.token, [subject.account_id])
                state['accounts'] = []
            subject.vault.save(subject.token, state)
        return result
    subject.gateway.get_account_details = change_session
    process(subject)
    push(subject, notice('late-old-listener'), context=context)
    assert calls == [] and subject.gateway.sign_calls == []
    saved = subject.vault.load(subject.token)
    if change == 'remove':
        assert saved['accounts'] == [] and subject.vault.load_monitor(subject.token)['accounts'] == {}
    elif change == 'relogin':
        assert saved['accounts'][0]['_cookies'][0]['value'] == 'new-session'
        assert all(item['candidate']['noticeKey'] != 'late-old-listener' for item in config(subject)['inbox'])


def test_course_query_cookie_refresh_is_used_for_verification_and_preserved(subject):
    enable(subject)
    new_cookies = [{'name': 'session', 'value': 'fresh-course-query-cookie'}]
    original = subject.gateway.get_account_details
    subject.gateway.get_account_details = lambda cookies, identity: {**original(cookies, identity), 'cookies': new_cookies}
    calls = details(subject)
    push(subject)
    process(subject)
    assert calls[0]['cookies'] == new_cookies
    assert subject.gateway.sign_calls[0]['cookies'] == new_cookies
    assert subject.vault.load(subject.token)['accounts'][0]['_cookies'] == new_cookies


def test_unowned_course_notice_stays_pending_and_can_be_verified_after_membership_refresh(subject):
    enable(subject)
    calls = details(subject)
    original = subject.gateway.get_account_details
    subject.gateway.get_account_details = lambda *args: {**original(*args), 'courses': []}
    push(subject)
    before = time.time()
    process(subject)
    saved = config(subject)
    assert len(saved['inbox']) == 1 and saved['inbox'][0]['attempts'] == 1
    assert timestamp(saved['inbox'][0]['retryAt']) >= before + 4
    assert timestamp(saved['inbox'][0]['retryAt']) <= time.time() + 6
    assert saved['seenNotices'] == [] and calls == [] and subject.gateway.sign_calls == []
    subject.gateway.get_account_details = original
    subject.manager.course_cache.pop(subject.key, None)
    due(subject)
    process(subject)
    assert len(calls) == 1 and len(subject.gateway.sign_calls) == 1 and config(subject)['inbox'] == []


def test_wrong_class_pair_never_reaches_activity_details_or_submit(subject):
    enable(subject)
    calls = details(subject)
    push(subject, notice(classId='790'))
    process(subject)
    assert calls == [] and subject.gateway.sign_calls == []
    assert len(config(subject)['inbox']) == 1 and config(subject)['seenNotices'] == []


def test_unknown_detail_is_retried_without_reserving_a_job_then_signs_once(subject):
    enable(subject)
    details(subject, unknown=True)
    push(subject)
    process(subject)
    snapshot = subject.manager.snapshot(subject.token, subject.vault.load(subject.token))
    assert snapshot['tasks'][0]['status'] == 'awaiting_details' and not snapshot['tasks'][0].get('requestId')
    assert config(subject)['seenNotices'] == [] and len(config(subject)['inbox']) == 1
    assert subject.gateway.sign_calls == []
    details(subject)
    due(subject)
    process(subject)
    assert len(subject.gateway.sign_calls) == 1 and config(subject)['inbox'] == []


def test_course_cooldown_preserves_the_queue_without_any_gateway_read_and_resumes_after_expiry(subject):
    enable(subject)
    calls = details(subject)
    subject.manager.defer(subject.token, subject.account_id, 120)
    push(subject)
    process(subject)
    saved = config(subject)
    assert calls == [] and subject.gateway.detail_calls == 0 and subject.gateway.sign_calls == []
    assert len(saved['inbox']) == 1 and saved['seenNotices'] == []
    assert timestamp(saved['inbox'][0]['retryAt']) >= timestamp(saved['cooldownUntil'])
    set_config(subject, cooldownUntil=iso(time.time() - 1))
    due(subject)
    process(subject)
    assert len(subject.gateway.sign_calls) == 1 and config(subject)['inbox'] == []


def test_list_only_cooldown_and_a_settings_version_change_do_not_block_realtime_details(subject):
    enable(subject, auto=False)
    context = spec(subject)
    with subject.vault.lock(subject.token):
        data = subject.vault.load_monitor(subject.token)
        defer_list_config(data['accounts'][subject.account_id], 120)
        subject.vault.save_monitor(subject.token, data)
    enable(subject, auto=True)
    calls = details(subject)
    push(subject, context=context)
    process(subject)
    assert len(calls) == 1 and len(subject.gateway.sign_calls) == 1
    assert config(subject)['inbox'] == [] and timestamp(config(subject)['courseListCooldownUntil']) > time.time()


def test_settings_change_during_details_retries_against_new_settings_without_signing(subject):
    enable(subject)
    details(subject, hook=lambda candidates: enable(subject, auto=False))
    push(subject)
    process(subject)
    assert subject.gateway.sign_calls == [] and len(config(subject)['inbox']) == 1
    details(subject)
    due(subject)
    process(subject)
    assert subject.gateway.sign_calls == [] and config(subject)['inbox'] == []
    assert subject.vault.load_monitor(subject.token)['tasks'][0]['status'] == 'monitoring_only'


def test_detail_throttling_keeps_the_notice_and_defers_its_retry(subject):
    enable(subject)
    def limited(*args):
        raise UpstreamError('upstream_rate_limited', '官方限流', 429, {'retryAfterSeconds': 120})
    subject.gateway.get_course_notification_activities = limited
    push(subject)
    process(subject)
    saved = config(subject)
    assert cooldown_seconds(saved) > 0 and len(saved['inbox']) == 1 and saved['seenNotices'] == []
    assert timestamp(saved['inbox'][0]['retryAt']) >= timestamp(saved['cooldownUntil'])
    assert subject.gateway.sign_calls == []


def test_connected_event_during_a_full_scan_preserves_the_requested_catchup(subject):
    enable(subject, auto=False)
    context = spec(subject)
    subject.gateway.activities = []
    subject.gateway.activities_hook = lambda: subject.manager._im_event(context,
        {'type': 'state', 'status': 'connected', 'message': '实时接收中'})
    started = time.time()
    run(subject)
    saved = config(subject)
    assert saved['checkSeq'] == 1 and saved['realtimeStatus'] == 'connected'
    assert started - 1 <= timestamp(saved['nextCheckAt']) <= time.time() + 1


def test_legacy_ten_second_setting_only_schedules_a_five_minute_reconciliation(subject):
    enable(subject, auto=False)
    subject.gateway.activities = []
    run(subject)
    saved = config(subject)
    assert saved['intervalSeconds'] == 10 and saved['reconcileSeconds'] == 300
    assert 299 <= timestamp(saved['nextCheckAt']) - timestamp(saved['lastCheckedAt']) <= 301
    subject.manager.schedule()
    assert subject.gateway.activity_calls == 1


def test_list_throttle_between_course_reservation_and_execution_does_not_cancel_the_sign(subject, monkeypatch):
    enable(subject)
    details(subject)
    execute = subject.manager._execute_sign
    def throttle_after_reservation(*args, **kwargs):
        with subject.vault.lock(subject.token):
            stored = subject.vault.load_monitor(subject.token)
            defer_list_config(stored['accounts'][subject.account_id], 120)
            subject.vault.save_monitor(subject.token, stored)
        return execute(*args, **kwargs)
    monkeypatch.setattr(subject.manager, '_execute_sign', throttle_after_reservation)
    push(subject)
    process(subject)
    task = subject.vault.load_monitor(subject.token)['tasks'][0]
    assert task['status'] == 'reported_success'
    assert len(subject.gateway.sign_calls) == 1
    assert timestamp(config(subject)['courseListCooldownUntil']) > time.time()


def test_course_throttle_between_group_reservation_and_execution_does_not_cancel_the_group(subject, monkeypatch):
    enable(subject)
    execute = subject.manager._execute_sign
    def throttle_after_reservation(*args, **kwargs):
        subject.manager.defer(subject.token, subject.account_id, 120)
        return execute(*args, **kwargs)
    monkeypatch.setattr(subject.manager, '_execute_sign', throttle_after_reservation)
    push(subject, {'activeId': '123', 'kind': 'ordinary_candidate', 'status': 'unknown',
        'name': '实时群通知', 'chatId': '900', 'activityScope': 'group',
        'notificationType': 'group_attendance', 'requirements': ['unknown'],
        'autoEligible': False, 'noticeKey': 'queued-group-course-throttle'})
    process(subject)
    task = subject.vault.load_monitor(subject.token)['tasks'][0]
    assert task['status'] == 'reported_success'
    assert len(subject.gateway.sign_calls) == 1 and cooldown_seconds(config(subject)) > 0
    assert subject.gateway.sign_calls[0]['payload'] == {
        'kind': 'ordinary', 'activityScope': 'group', 'activeId': '123'}


def test_course_throttle_between_course_reservation_and_execution_still_blocks_the_course(subject, monkeypatch):
    enable(subject)
    details(subject)
    execute = subject.manager._execute_sign
    def throttle_after_reservation(*args, **kwargs):
        subject.manager.defer(subject.token, subject.account_id, 120)
        return execute(*args, **kwargs)
    monkeypatch.setattr(subject.manager, '_execute_sign', throttle_after_reservation)
    push(subject)
    process(subject)
    task = subject.vault.load_monitor(subject.token)['tasks'][0]
    job, _ = subject.vault.get_job(subject.token, task['requestId'])
    assert task['status'] == 'skipped' and job['submitted'] is False and job['phase'] == 'before_submit'
    assert subject.gateway.sign_calls == [] and cooldown_seconds(config(subject)) > 0


def test_user_configure_between_reservation_and_execution_still_cancels_the_queued_sign(subject, monkeypatch):
    enable(subject)
    details(subject)
    execute = subject.manager._execute_sign
    def configure_after_reservation(*args, **kwargs):
        with subject.vault.lock(subject.token):
            subject.manager.configure(subject.token, subject.account_id, True, True, 60)
        return execute(*args, **kwargs)
    monkeypatch.setattr(subject.manager, '_execute_sign', configure_after_reservation)
    push(subject)
    process(subject)
    task = subject.vault.load_monitor(subject.token)['tasks'][0]
    job, _ = subject.vault.get_job(subject.token, task['requestId'])
    assert task['status'] == 'skipped' and job['submitted'] is False and job['phase'] == 'before_submit'
    assert subject.gateway.sign_calls == []
    assert config(subject)['enabled'] and config(subject)['autoSign'] and config(subject)['intervalSeconds'] == 60
