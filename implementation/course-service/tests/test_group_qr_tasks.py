"""Direct group QR jobs share the existing group duplicate guard."""
import json

import pytest

from test_course_monitor import monitor, enable, run, ACTIVITY
from test_monitor_http import api
from test_ordinary_tasks import GROUP, settled
from test_qr_batches import qr_service, login, post, wait_batch
from vault import iso


def group_qr(subject):
    subject.gateway.activities = []
    subject.gateway.get_group_activities = lambda cookies, identity: {
        'activities': [{**GROUP, 'kind': 'qr'}], 'groupCount': 1, 'status': 'ok'}


def test_group_qr_manual_action_needs_no_image_code_or_course_context(monitor):
    group_qr(monitor)
    enable(monitor, auto=False)
    task = run(monitor)['tasks'][0]
    assert task['status'] == 'monitoring_only' and task['kind'] == 'qr'
    assert monitor.gateway.sign_calls == []
    result = monitor.manager.sign_now(monitor.token, task['id'])
    request_id = result['tasks'][0]['requestId']
    assert settled(monitor, request_id)['status'] == 'reported_success'
    assert monitor.gateway.sign_calls[0]['payload'] == {
        'kind': 'qr', 'activityScope': 'group', 'activeId': '123'}
    monitor.manager.sign_now(monitor.token, task['id'])
    assert len(monitor.gateway.sign_calls) == 1


@pytest.mark.parametrize('status,submitted', [('reported_success', True), ('pending', False),
                                             ('outcome_unknown', True), ('unknown', True)])
def test_previous_scanned_group_qr_blocks_new_direct_job(monitor, status, submitted):
    group_qr(monitor)
    previous = {'requestId': 'old-scanned-group-request', 'accountId': monitor.account_id,
                'activeId': '123', 'activityScope': 'group', 'status': status,
                'submitted': submitted, 'message': '原二维码入口回执', 'createdAt': iso()}
    monitor.vault.save_job(monitor.token, previous, 'old-qr-fingerprint')
    enable(monitor, auto=False)
    task = run(monitor)['tasks'][0]
    assert task['requestId'] == previous['requestId']
    assert monitor.manager.sign_now(monitor.token, task['id'])['tasks'][0]['requestId'] == previous['requestId']
    assert monitor.gateway.sign_calls == []


def test_direct_group_qr_does_not_consume_same_numeric_course_qr(monitor):
    group_qr(monitor)
    monitor.gateway.activities = [{**ACTIVITY, 'kind': 'qr', 'autoEligible': False}]
    enable(monitor)
    snapshot = run(monitor)
    group = next(t for t in snapshot['tasks'] if t['activityScope'] == 'group')
    course = next(t for t in snapshot['tasks'] if t['activityScope'] == 'course')
    assert group['status'] == 'reported_success'
    assert course['status'] == 'needs_input' and not course.get('requestId')
    assert len(monitor.gateway.sign_calls) == 1
    assert monitor.gateway.sign_calls[0]['payload']['activityScope'] == 'group'


def test_direct_api_accepts_group_qr_with_only_activity_id_and_reuses_result(api):
    url = f'/api/accounts/{api.account}/sign'
    headers = {**api.headers, 'Idempotency-Key': 'direct-group-qr-123'}
    body = {'activityScope': 'group', 'kind': 'qr', 'activeId': '123'}
    response = api.client.post(url, json=body, headers=headers)
    assert response.status_code == 200, response.text
    assert response.json()['activityScope'] == 'group'
    assert api.client.post(url, json=body, headers=headers).json() == response.json()
    assert api.gateway.sign_calls == 1
    second = api.client.post(url, json=body,
                            headers={**api.headers, 'Idempotency-Key': 'direct-group-qr-other-key'})
    assert second.status_code == 409
    assert api.gateway.sign_calls == 1


@pytest.mark.parametrize('extra', [{'enc': 'not-needed'}, {'qrText': 'not-needed'},
                                 {'courseId': '456'}, {'classId': '789'}, {'uid': '999'},
                                 {'refreshMode': 'dynamic'}])
def test_group_direct_api_does_not_accept_qr_credentials_or_forged_mode(api, extra):
    response = api.client.post(f'/api/accounts/{api.account}/sign',
        json={'activityScope': 'group', 'kind': 'qr', 'activeId': '123', **extra},
        headers={**api.headers, 'Idempotency-Key': 'group-qr-invalid-123'})
    assert response.status_code == 422
    assert api.gateway.sign_calls == 0


@pytest.mark.parametrize('status', ['reported_success', 'pending', 'outcome_unknown'])
def test_scan_after_direct_group_qr_reuses_the_same_group_duplicate_guard(qr_service, status):
    account_id = login(qr_service)
    token = qr_service.client.cookies.get(qr_service.app.state.cookie_name)
    job = {'requestId': 'prior-direct-group-qr', 'accountId': account_id, 'activeId': '300',
           'activityScope': 'group', 'status': status, 'submitted': True,
           'message': '直接入口原回执', 'createdAt': iso()}
    qr_service.app.state.vault.save_job(token, job, 'direct-group-fingerprint')
    response = post(qr_service, '/api/qr/batches', {
        'accountIds': [account_id], 'qrText': 'SIGNIN:aid=300&source=1&Code=ABCD1234'},
        'scan-after-direct-group')
    assert response.status_code == 202
    result = wait_batch(qr_service, response.json()['batchId'])
    assert result['items'][0]['requestId'] == job['requestId']
    assert result['items'][0]['status'] == ('reported_success' if status == 'reported_success'
                                         else 'previous_result_unknown')
    assert qr_service.gateway.calls == []


@pytest.mark.parametrize('kind', ['qr', 'ordinary_candidate'])
@pytest.mark.parametrize('submitted', [False, True])
def test_group_context_receipt_keeps_submission_state_and_private_diagnostic(monitor, kind, submitted):
    group_qr(monitor)
    monitor.gateway.get_group_activities = lambda cookies, identity: {
        'activities': [{**GROUP, 'kind': kind}], 'groupCount': 1, 'status': 'ok'}
    reason = 'page_identity_missing' if submitted else 'group_form_pending'
    diagnostic = {'endpoint': '/widget/sign/group/pcStuSignGroupController/preSign',
                  'httpStatus': 200, 'body': 'synthetic-private-group-form'}
    monitor.gateway.sign_result = {'status': 'need_context', 'message': '群页面仍需确认。',
        'phase': 'group_entry', 'submitted': submitted, 'httpStatus': 200,
        'reasonCode': reason, 'diagnostics': {'reasonCode': reason}, '_diagnostic': diagnostic}
    enable(monitor)
    snapshot = run(monitor)
    task = snapshot['tasks'][0]
    job, _ = monitor.vault.get_job(monitor.token, task['requestId'])
    assert job['status'] == task['status'] == 'need_context'
    assert job['submitted'] is submitted and job['phase'] == 'group_entry'
    assert job['reasonCode'] == reason and job['httpStatus'] == 200
    assert job['diagnostics'] == {'reasonCode': reason}
    assert monitor.vault.get_diagnostic(monitor.token, job['requestId']) == diagnostic
    assert diagnostic['body'] not in json.dumps(snapshot)
    assert bool(monitor.vault.prior_job(monitor.token, monitor.account_id, '123', 'group')) is submitted
    run(monitor)
    assert len(monitor.gateway.sign_calls) == 1
