"""Photo lookup may resume; a submitted photo attempt must never be replayed."""
from copy import deepcopy
import time

import pytest

from test_course_monitor import monitor, enable, run
from test_server import service, login, sign
from vault import iso
from sign_history import successful_records


GROUP = {'activeId': '300', 'chatId': '500', 'activityScope': 'group',
         'kind': 'ordinary_candidate', 'name': '签到', 'status': 'unknown'}
WAIT = {'status': 'photo_required', 'message': '等待本次活动照片。', 'submitted': False,
        'phase': 'group_photo_lookup', 'reasonCode': 'photo_missing', 'resolvedKind': 'photo'}
SUCCESS = {'status': 'reported_success', 'message': '本人记录确认成功。', 'submitted': True,
           'phase': 'group_photo_verify', 'reasonCode': 'photo_record_verified', 'resolvedKind': 'photo'}


def prepare(subject, result=WAIT):
    subject.gateway.activities = []
    subject.gateway.get_group_activities = lambda *_: {
        'activities': [deepcopy(GROUP)], 'groupCount': 1, 'status': 'ok'}
    subject.gateway.sign_result = deepcopy(result)
    enable(subject)


def age_job(subject, task):
    job, fingerprint = subject.vault.get_job(subject.ref, task['requestId'])
    job['completedAt'] = iso(time.time() - 301)
    subject.vault.save_job(subject.ref, job, fingerprint)
    return job, fingerprint


@pytest.mark.parametrize('reason', ['photo_missing', 'photo_lookup_incomplete'])
def test_readonly_photo_wait_resumes_after_five_minutes_with_same_job(monitor, reason):
    prepare(monitor, {**WAIT, 'reasonCode': reason})
    first = run(monitor)['tasks'][0]
    assert first['kind'] == 'photo' and first['submitted'] is False
    assert first['reasonCode'] == reason
    run(monitor)
    assert len(monitor.gateway.sign_calls) == 1
    original, fingerprint = age_job(monitor, first)
    monitor.gateway.sign_result = deepcopy(SUCCESS)
    final = run(monitor)['tasks'][0]
    assert final['requestId'] == first['requestId']
    assert final['status'] == 'reported_success' and final['kind'] == 'photo'
    job, saved_hash = monitor.vault.get_job(monitor.ref, final['requestId'])
    assert saved_hash == fingerprint and job['createdAt'] == original['createdAt']
    assert len(monitor.vault.account_jobs(monitor.ref, monitor.account_id)) == 1
    assert monitor.gateway.sign_calls[-1]['payload'] == {
        'kind': 'ordinary', 'activityScope': 'group', 'activeId': '300'}
    run(monitor)
    assert len(monitor.gateway.sign_calls) == 2
    assert successful_records(monitor.vault, monitor.ref, monitor.account_id)['records'][0]['kind'] == 'photo'


@pytest.mark.parametrize('changes', [
    {'submitted': True}, {'submitted': None}, {'reasonCode': 'form_unrecognized'},
    {'status': 'outcome_unknown'}, {'status': 'rejected'}, {'resolvedKind': None},
])
def test_only_explicit_unsubmitted_photo_wait_may_resume(monitor, changes):
    prepare(monitor, {**WAIT, **changes})
    first = run(monitor)['tasks'][0]
    age_job(monitor, first)
    run(monitor)
    monitor.manager.sign_now(monitor.token, first['id'])
    assert len(monitor.gateway.sign_calls) == 1


def test_photo_wait_can_be_manually_rechecked_without_enabling_auto_sign(monitor, monkeypatch):
    prepare(monitor)
    first = run(monitor)['tasks'][0]
    enable(monitor, auto=False)
    age_job(monitor, first)
    run(monitor)
    assert len(monitor.gateway.sign_calls) == 1
    monitor.gateway.sign_result = deepcopy(SUCCESS)
    monkeypatch.setattr(monitor.manager.pool, 'submit', lambda fn, *args, **kwargs: fn(*args, **kwargs))
    final = monitor.manager.sign_now(monitor.token, first['id'])
    assert final['tasks'][0]['status'] == 'reported_success'
    assert final['accounts'][0]['autoSign'] is False
    monitor.manager.sign_now(monitor.token, first['id'])
    assert len(monitor.gateway.sign_calls) == 2


@pytest.mark.parametrize('status', ['rejected', 'requires_validation', 'photo_required', 'outcome_unknown'])
@pytest.mark.parametrize('submitted', [True, None])
def test_photo_submission_blocks_another_request_id(monitor, status, submitted):
    job = {'requestId': 'photo-attempt', 'accountId': monitor.account_id, 'activeId': '300',
           'activityScope': 'group', 'resolvedKind': 'photo', 'status': status, 'submitted': submitted}
    monitor.vault.save_job(monitor.ref, job, 'photo-fingerprint')
    assert monitor.vault.prior_job(monitor.ref, monitor.account_id, '300', 'group') == job
    assert monitor.vault.prior_job(monitor.ref, monitor.account_id, '300', 'course') is None


def test_group_photo_api_keeps_resolved_kind_and_blocks_another_submission(service, monkeypatch):
    account_id, _ = login(service)
    calls = []

    def submit(*args):
        calls.append(args)
        return {**SUCCESS, 'objectId': 'private-photo-reference', 'subjectPuid': 'private-member'}

    monkeypatch.setattr(service.gateway, 'sign', submit)
    payload = {'kind': 'ordinary', 'activeId': '123', 'activityScope': 'group'}
    first = sign(service, account_id, payload=payload)
    assert first.status_code == 200 and first.json()['resolvedKind'] == 'photo'
    assert 'private-photo-reference' not in first.text and 'private-member' not in first.text
    assert sign(service, account_id, payload=payload).json() == first.json()
    second = sign(service, account_id, key='different-photo-request', payload=payload)
    assert second.status_code == 409 and len(calls) == 1
