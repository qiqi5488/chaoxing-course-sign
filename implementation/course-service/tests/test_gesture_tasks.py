"""Course gesture attempts use the common receipt and duplicate guard."""
import pytest

from test_course_monitor import monitor, enable, run, ACTIVITY
from test_ordinary_tasks import settled
from upstream import UpstreamError
from vault import iso


GESTURE = {**ACTIVITY, 'kind': 'gesture', 'requirements': ['code'], 'autoEligible': False}


def test_course_gesture_auto_attempt_preserves_its_type_and_never_sends_a_pattern(monitor):
    monitor.gateway.activities = [GESTURE]
    enable(monitor)
    first = run(monitor)
    assert first['tasks'][0]['status'] == 'reported_success'
    assert monitor.gateway.sign_calls[0]['payload'] == {
        'kind': 'gesture', 'activeId': '123', 'courseId': '456', 'classId': '789', '_autoSign': True}
    run(monitor)
    assert len(monitor.gateway.sign_calls) == 1


def test_course_gesture_manual_attempt_registers_once_without_enabling_auto(monitor):
    monitor.gateway.activities = [GESTURE]
    enable(monitor, auto=False)
    task = run(monitor)['tasks'][0]
    assert task['status'] == 'monitoring_only' and task['requirements'] == ['code']
    assert monitor.gateway.sign_calls == []
    result = monitor.manager.sign_now(monitor.token, task['id'])
    request_id = result['tasks'][0]['requestId']
    assert not result['accounts'][0]['autoSign']
    assert settled(monitor, request_id)['status'] == 'reported_success'
    assert monitor.gateway.sign_calls[0]['payload']['kind'] == 'gesture'
    monitor.manager.sign_now(monitor.token, task['id'])
    assert len(monitor.gateway.sign_calls) == 1


@pytest.mark.parametrize('changes', [
    {'requirements': ['code', 'photo']}, {'requirements': ['code', 'location']},
    {'requirements': ['code', 'face']}, {'requirements': ['code', 'unknown']},
    {'requirements': []}, {'status': 'unknown'}, {'status': 'scheduled'},
    {'status': 'ended'}, {'kind': 'code'},
])
def test_only_an_active_course_gesture_without_extra_requirements_is_actionable(monitor, changes):
    monitor.gateway.activities = [{**GESTURE, **changes}]
    enable(monitor)
    snapshot = run(monitor)
    assert monitor.gateway.sign_calls == []
    for task in snapshot['tasks']:
        with pytest.raises(UpstreamError):
            monitor.manager.sign_now(monitor.token, task['id'])
    assert monitor.gateway.sign_calls == []


@pytest.mark.parametrize('status', ['pending', 'reported_success', 'outcome_unknown', 'need_context'])
def test_prior_course_code_or_ordinary_attempt_blocks_new_gesture_type(monitor, status):
    monitor.gateway.activities = [GESTURE]
    previous = {'requestId': 'prior-course-code-attempt', 'accountId': monitor.account_id,
        'activeId': '123', 'kind': 'code', 'status': status, 'submitted': True,
        'message': '已有同课程活动回执', 'createdAt': iso()}
    monitor.vault.save_job(monitor.token, previous, 'prior-course-code-fingerprint')
    enable(monitor)
    task = run(monitor)['tasks'][0]
    assert task['requestId'] == previous['requestId']
    assert monitor.gateway.sign_calls == []
    monitor.manager.sign_now(monitor.token, task['id'])
    assert monitor.gateway.sign_calls == []


def test_group_gesture_keeps_the_group_payload_without_course_context(monitor):
    monitor.gateway.activities = []
    monitor.gateway.get_group_activities = lambda cookies, identity: {
        'activities': [{**GESTURE, 'activityScope': 'group', 'chatId': 'test-chat'}],
        'status': 'ok', 'groupCount': 1}
    enable(monitor)
    task = run(monitor)['tasks'][0]
    assert task['status'] == 'reported_success'
    assert monitor.gateway.sign_calls[0]['payload'] == {
        'kind': 'gesture', 'activityScope': 'group', 'activeId': '123'}
    monitor.manager.sign_now(monitor.token, task['id'])
    assert len(monitor.gateway.sign_calls) == 1


def test_existing_unknown_group_gesture_is_reclassified_without_replaying_its_receipt(monitor):
    monitor.gateway.activities = []
    activity = {**GESTURE, 'activityScope': 'group', 'chatId': 'test-chat'}
    monitor.gateway.get_group_activities = lambda cookies, identity: {
        'activities': [{**activity, 'kind': 'unknown'}], 'status': 'ok', 'groupCount': 1}
    enable(monitor)
    task = run(monitor)['tasks'][0]
    assert task['kind'] == 'unknown' and not task.get('requestId')
    job = {'requestId': 'group-gesture-existing-success', 'accountId': monitor.account_id,
        'activityScope': 'group', 'activeId': task['activeId'], 'status': 'reported_success',
        'submitted': True, 'message': '原群手势成功回执', 'createdAt': iso()}
    monitor.vault.save_job(monitor.token, job, 'group-gesture-existing-fingerprint')
    run(monitor)
    monitor.gateway.get_group_activities = lambda cookies, identity: {
        'activities': [activity], 'status': 'ok', 'groupCount': 1}
    updated = run(monitor)['tasks'][0]
    assert updated['kind'] == 'gesture' and updated['status'] == 'reported_success'
    assert updated['requestId'] == job['requestId']
    assert monitor.gateway.sign_calls == []
