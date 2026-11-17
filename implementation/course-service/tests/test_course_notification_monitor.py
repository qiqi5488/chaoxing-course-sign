"""Course notifications survive list throttling without crossing account scope."""
from copy import deepcopy
import time

import pytest

from course_monitor import cooldown_seconds, defaults, defer_config, defer_list_config
from test_course_monitor import monitor, enable, run, COURSE, ACTIVITY
from upstream import UpstreamError
from vault import iso, timestamp


def notifications(subject, rows=None):
    rows = rows if rows is not None else [dict(ACTIVITY)]
    subject.gateway.activities = []
    subject.gateway.notification_calls = []

    def messages(cookies, identity):
        return {'activities': [], 'courseActivities': [
            {**row, 'kind': 'unknown', 'status': 'unknown', 'requirements': ['unknown'],
             'autoEligible': False, 'activityScope': 'course', 'notificationType': 'course_attendance'}
            for row in rows], 'cookies': cookies, 'groupCount': 1, 'status': 'ok'}

    def details(cookies, identity, course, candidates):
        subject.gateway.notification_calls.append(deepcopy(candidates))
        ids = {row['activeId'] for row in candidates}
        return {'activities': [{**row, 'notificationType': 'course_attendance'}
                               for row in rows if row['activeId'] in ids],
                'sources': [{'id': 'notification', 'status': 'ok'}], 'cookies': cookies}

    subject.gateway.get_group_activities = messages
    subject.gateway.get_course_notification_activities = details
    return rows


def list_limited():
    return UpstreamError('upstream_rate_limited', '活动列表操作受限', 429,
                         {'retryAfterSeconds': 120, 'operation': 'course_activity_list'})


def test_list_throttle_preserves_course_count_and_all_notification_tasks(monitor):
    second = {**COURSE, 'courseId': '457', 'classId': '790'}
    original = monitor.gateway.get_account_details
    monitor.gateway.get_account_details = lambda *args: {**original(*args), 'courses': [COURSE, second]}
    notifications(monitor, [ACTIVITY, {**ACTIVITY, **second, 'activeId': '124'}])
    monitor.gateway.activities_error = list_limited()
    enable(monitor, auto=False)
    first = run(monitor)
    config = first['accounts'][0]
    assert config['courseCount'] == 2
    assert len(first['tasks']) == 2 and len(monitor.gateway.notification_calls) == 2
    assert monitor.gateway.activity_calls == 1
    assert cooldown_seconds(config) == 0
    assert timestamp(config['courseListCooldownUntil']) > time.time()
    assert all(t['activityScope'] == 'course' for t in first['tasks'])


def test_new_notification_is_processed_once_during_list_cooldown(monitor):
    rows = notifications(monitor)
    monitor.gateway.activities_error = list_limited()
    enable(monitor)
    run(monitor)
    rows.append({**ACTIVITY, 'activeId': '124'})
    second = run(monitor)
    run(monitor)
    assert monitor.gateway.activity_calls == 1
    assert len(monitor.gateway.sign_calls) == 2
    assert {t['activeId'] for t in second['tasks']} == {'123', '124'}
    assert all(t['status'] == 'reported_success' for t in second['tasks'])


def test_general_course_cooldown_still_blocks_notification_detail_and_submit(monitor):
    notifications(monitor)
    enable(monitor)
    monitor.manager.defer(monitor.token, monitor.account_id, 120)
    run(monitor)
    assert monitor.gateway.notification_calls == []
    assert monitor.gateway.activity_calls == 0 and monitor.gateway.sign_calls == []


@pytest.mark.parametrize('changes', [
    {'courseId': '999'}, {'classId': '999'}, {'activityScope': 'group'},
])
def test_unowned_or_wrong_scope_notification_never_becomes_course_task(monitor, changes):
    notifications(monitor)
    original = monitor.gateway.get_group_activities
    def messages(*args):
        result = original(*args)
        result['courseActivities'][0].update(changes)
        return result
    monitor.gateway.get_group_activities = messages
    enable(monitor)
    assert run(monitor)['tasks'] == []
    assert monitor.gateway.notification_calls == [] and monitor.gateway.sign_calls == []


def test_ended_course_notification_is_visible_with_real_time_and_never_submitted(monitor):
    notifications(monitor, [{**ACTIVITY, 'status': 'ended', 'autoEligible': False,
                             'startTime': 1_800_000_000_000}])
    enable(monitor)
    task = run(monitor)['tasks'][0]
    assert task['status'] == 'ended' and task['activityScope'] == 'course'
    assert task['notificationType'] == 'course_attendance'
    assert task['startTime'] == 1_800_000_000_000
    assert not task.get('requestId') and monitor.gateway.sign_calls == []


def test_unverified_course_notification_is_awaiting_details_without_submit(monitor):
    notifications(monitor, [{**ACTIVITY, 'status': 'unknown', 'kind': 'unknown',
                             'requirements': ['unknown'], 'autoEligible': False}])
    enable(monitor)
    task = run(monitor)['tasks'][0]
    assert task['status'] == 'awaiting_details'
    assert monitor.gateway.sign_calls == []


def test_list_cooldown_survives_setting_changes_and_expires(monitor):
    notifications(monitor)
    monitor.gateway.activities_error = list_limited()
    enable(monitor, auto=False)
    deadline = run(monitor)['accounts'][0]['courseListCooldownUntil']
    monitor.manager.configure(monitor.token, monitor.account_id, True, False, 60)
    assert run(monitor)['accounts'][0]['courseListCooldownUntil'] == deadline
    data = monitor.vault.load_monitor(monitor.token)
    data['accounts'][monitor.account_id]['courseListCooldownUntil'] = iso(time.time() - 1)
    monitor.vault.save_monitor(monitor.token, data)
    monitor.gateway.activities_error = None
    assert run(monitor)['accounts'][0]['courseListCooldownUntil'] is None
    assert monitor.gateway.activity_calls == 2


def test_inflight_list_throttle_preserves_latest_preferences(monitor):
    notifications(monitor)
    monitor.gateway.activities_hook = lambda: monitor.manager.configure(
        monitor.token, monitor.account_id, True, False, 60)
    monitor.gateway.activities_error = list_limited()
    enable(monitor)
    config = run(monitor)['accounts'][0]
    assert config['enabled'] and not config['autoSign'] and config['intervalSeconds'] == 60
    assert config['courseCount'] == 1
    assert timestamp(config['courseListCooldownUntil']) > time.time()
    assert cooldown_seconds(config) == 0


def test_list_throttle_does_not_weaken_an_existing_general_course_cooldown():
    config = {**defaults(), 'enabled': True}
    defer_config(config, 180)
    deadline = config['cooldownUntil']
    defer_list_config(config, 120)
    assert config['status'] == 'rate_limited' and config['cooldownUntil'] == deadline
    assert cooldown_seconds(config) > 0 and timestamp(config['courseListCooldownUntil']) > time.time()
