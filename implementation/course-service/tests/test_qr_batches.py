"""Batch QR integration tests. All credentials and upstream responses are synthetic."""
from copy import deepcopy
from pathlib import Path
from types import SimpleNamespace
import sys
import threading
import time

import pytest
from fastapi.testclient import TestClient

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from server import COOKIE, Settings, create_app
from upstream import UpstreamError

ORIGIN = 'http://127.0.0.1:8000'
PASSWORD = 'synthetic-qr-password'
QR = 'https://mobilelearn.chaoxing.com/widget/sign/e?id=300&enc=synthetic-enc'


class BatchGateway:
    def __init__(self):
        self.calls = []
        self.statuses = {}
        self.block_uid = None
        self.started = threading.Event()
        self.release = threading.Event()
        self.call_lock = threading.Lock()

    def login(self, identifier, password):
        assert password == PASSWORD
        uid = '10' + identifier[-2:]
        return SimpleNamespace(uid=uid, fid='200', name='测试账号' + identifier[-2:], school='测试学校',
            cookies=[{'name': '_uid', 'value': uid, 'domain': '.chaoxing.com', 'path': '/',
                      'secure': True, 'expires': None, 'rest': {}}], session_expires_at=None)

    def sign_qr(self, cookies, identity, qr_text, *, location=None):
        uid = identity['uid']
        cookie_uid = next(c['value'] for c in cookies if c['name'] == '_uid')
        assert uid == cookie_uid
        assert qr_text == QR
        with self.call_lock:
            self.calls.append({'uid': uid, 'cookieUid': cookie_uid, 'location': deepcopy(location)})
        if uid == self.block_uid:
            self.started.set()
            if not self.release.wait(5):
                raise AssertionError('Test did not release its synthetic upstream request')
        status = self.statuses.get(uid, 'reported_success')
        if isinstance(status, Exception):
            raise status
        return {'activeId': '300', 'status': status, 'message': '替身上游：' + status,
                'phase': 'submit', 'submitted': True, 'cookies': deepcopy(cookies)}


@pytest.fixture
def qr_service(tmp_path):
    gateway = BatchGateway()
    settings = Settings(data_dir=tmp_path / 'batch-data', public_origin=ORIGIN)
    app = create_app(settings=settings, gateway=gateway)
    with TestClient(app, base_url=ORIGIN) as client:
        initial = client.get('/api/bootstrap')
        assert initial.status_code == 200
        service = SimpleNamespace(gateway=gateway, settings=settings, app=app, client=client,
                                  csrf=initial.json()['csrfToken'])
        try:
            yield service
        finally:
            gateway.release.set()


def post(service, path, body=None, key=None):
    headers = {'X-CSRF-Token': service.csrf, 'Origin': ORIGIN}
    if key:
        headers['Idempotency-Key'] = key
    return service.client.post(path, json=body, headers=headers)


def login(service, ending='11'):
    response = post(service, '/api/accounts/login', {
        'identifier': '188000011' + ending, 'password': PASSWORD, 'grantDays': 7,
    })
    assert response.status_code == 200, response.text
    return response.json()['accountId']


def create_batch(service, account_ids, key='qr-batch-0001', **extra):
    return post(service, '/api/qr/batches', {'accountIds': account_ids, 'qrText': QR, **extra}, key)


def wait_batch(service, batch_id, timeout=4):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        response = service.client.get('/api/qr/batches/' + batch_id)
        assert response.status_code == 200, response.text
        batch = response.json()
        if batch['status'] in ('completed', 'interrupted'):
            return batch
        time.sleep(.01)
    raise AssertionError('Synthetic batch did not finish within its bounded deadline')


def test_one_scan_uses_each_selected_accounts_own_session_and_real_result(qr_service):
    first, second = login(qr_service, '11'), login(qr_service, '22')
    qr_service.gateway.statuses['1022'] = 'requires_validation'
    response = create_batch(qr_service, [first, second])
    assert response.status_code == 202, response.text
    result = wait_batch(qr_service, response.json()['batchId'])
    assert result['activeId'] == '300'
    assert {r['accountId']: r['status'] for r in result['items']} == {
        first: 'reported_success', second: 'requires_validation',
    }
    assert sorted((c['uid'], c['cookieUid']) for c in qr_service.gateway.calls) == [('1011', '1011'), ('1022', '1022')]
    assert result['summary']['total'] == 2
    assert result['summary']['completed'] == 2
    assert result['summary']['succeeded'] == 1
    assert result['summary']['needsAttention'] == 1
    for private in (PASSWORD, 'synthetic-enc', '18800001111'):
        assert private not in str(result)


def test_group_scan_selects_multiple_accounts_and_keeps_individual_outcomes(qr_service):
    first, second = login(qr_service, '11'), login(qr_service, '22')
    group_qr = 'SIGNIN:aid=300&source=1&Code=ABCD1234'
    course_gateway = qr_service.gateway.sign_qr

    def group_gateway(cookies, identity, qr_text, *, location=None):
        assert qr_text == group_qr
        return course_gateway(cookies, identity, QR, location=location)

    qr_service.gateway.sign_qr = group_gateway
    qr_service.gateway.statuses['1022'] = 'requires_validation'
    created = post(qr_service, '/api/qr/batches', {'accountIds': [first, second], 'qrText': group_qr}, 'group-multiple-accounts')
    assert created.status_code == 202, created.text
    result = wait_batch(qr_service, created.json()['batchId'])
    assert result['activityScope'] == 'group'
    assert result['summary']['succeeded'] == 1
    assert {item['status'] for item in result['items']} == {'reported_success', 'requires_validation'}
    assert sorted((c['uid'], c['cookieUid']) for c in qr_service.gateway.calls) == [('1011', '1011'), ('1022', '1022')]
    assert 'ABCD1234' not in str(result)


def test_course_success_cannot_be_reused_as_group_success_for_same_numeric_id(qr_service):
    first = login(qr_service)
    course = create_batch(qr_service, [first])
    assert wait_batch(qr_service, course.json()['batchId'])['summary']['succeeded'] == 1
    original = qr_service.gateway.sign_qr
    group_qr = 'SIGNIN:aid=300&source=1&Code=ABCD1234'

    def group_gateway(cookies, identity, qr_text, *, location=None):
        assert qr_text == group_qr
        return original(cookies, identity, QR, location=location)

    qr_service.gateway.sign_qr = group_gateway
    qr_service.gateway.statuses['1011'] = 'device_conflict'
    created = post(qr_service, '/api/qr/batches', {'accountIds': [first], 'qrText': group_qr}, 'group-numeric-collision')
    assert created.status_code == 202, created.text
    result = wait_batch(qr_service, created.json()['batchId'])
    assert result['items'][0]['status'] == 'device_conflict'
    assert result['summary']['succeeded'] == 0
    assert len(qr_service.gateway.calls) == 2


def test_unselected_accounts_are_never_submitted(qr_service):
    first = login(qr_service, '11')
    login(qr_service, '22')
    response = create_batch(qr_service, [first])
    assert response.status_code == 202, response.text
    wait_batch(qr_service, response.json()['batchId'])
    assert [c['uid'] for c in qr_service.gateway.calls] == ['1011']


@pytest.mark.parametrize('status', ['requires_validation', 'device_conflict', 'rejected', 'outcome_unknown'])
def test_failed_or_uncertain_account_does_not_turn_whole_batch_into_success(qr_service, status):
    first, second = login(qr_service, '11'), login(qr_service, '22')
    qr_service.gateway.statuses['1011'] = status
    response = create_batch(qr_service, [first, second])
    assert response.status_code == 202, response.text
    result = wait_batch(qr_service, response.json()['batchId'])
    assert len(qr_service.gateway.calls) == 2
    assert result['summary']['succeeded'] == 1
    assert next(r for r in result['items'] if r['accountId'] == first)['status'] == status
    assert next(r for r in result['items'] if r['accountId'] == second)['status'] == 'reported_success'


def test_revoked_or_expired_accounts_are_rejected_before_queueing(qr_service):
    first, second = login(qr_service, '11'), login(qr_service, '22')
    assert post(qr_service, f'/api/accounts/{first}/revoke').status_code == 200
    response = create_batch(qr_service, [first, second])
    assert response.status_code in (401, 409), response.text
    assert qr_service.gateway.calls == []
    token = qr_service.client.cookies.get(COOKIE)
    state = qr_service.app.state.vault.load(token)
    for account in state['accounts']:
        if account['id'] == second:
            account['sessionExpiresAt'] = '2000-01-01T00:00:00Z'
    qr_service.app.state.vault.save(token, state)
    response = create_batch(qr_service, [second], key='qr-expired-0001')
    assert response.status_code in (401, 409), response.text
    assert qr_service.gateway.calls == []


def test_batch_and_receipt_are_isolated_between_browser_workspaces(qr_service):
    first = login(qr_service)
    response = create_batch(qr_service, [first])
    assert response.status_code == 202, response.text
    batch_id = response.json()['batchId']
    wait_batch(qr_service, batch_id)
    with TestClient(qr_service.app, base_url=ORIGIN) as other:
        token = other.get('/api/bootstrap').json()['csrfToken']
        assert other.get('/api/qr/batches/' + batch_id).status_code == 404
        assert other.get('/api/qr/batches?limit=5').json()['batches'] == []
        response = other.post('/api/qr/batches', json={'accountIds': [first], 'qrText': QR},
            headers={'Origin': ORIGIN, 'X-CSRF-Token': token, 'Idempotency-Key': 'qr-other-0001'})
        assert response.status_code == 404
    assert len(qr_service.gateway.calls) == 1


def test_repeated_create_and_refresh_only_read_the_original_batch(qr_service):
    first = login(qr_service)
    created = create_batch(qr_service, [first])
    assert created.status_code == 202, created.text
    batch_id = created.json()['batchId']
    result = wait_batch(qr_service, batch_id)
    same = create_batch(qr_service, [first])
    assert same.status_code in (200, 202), same.text
    assert same.json()['batchId'] == batch_id
    for _ in range(3):
        assert qr_service.client.get('/api/qr/batches/' + batch_id).json()['status'] == result['status']
    assert len(qr_service.gateway.calls) == 1
    assert qr_service.client.get('/api/qr/batches?limit=5').json()['batches'][0]['batchId'] == batch_id


def test_reusing_batch_key_for_different_contents_is_rejected(qr_service):
    first, second = login(qr_service, '11'), login(qr_service, '22')
    response = create_batch(qr_service, [first])
    assert response.status_code == 202, response.text
    wait_batch(qr_service, response.json()['batchId'])
    mismatch = create_batch(qr_service, [second])
    assert mismatch.status_code == 409, mismatch.text
    assert len(qr_service.gateway.calls) == 1


def test_new_batch_key_cannot_repeat_unknown_activity_submission(qr_service):
    first = login(qr_service)
    qr_service.gateway.statuses['1011'] = 'outcome_unknown'
    original = create_batch(qr_service, [first])
    assert original.status_code == 202, original.text
    wait_batch(qr_service, original.json()['batchId'])
    another = create_batch(qr_service, [first], key='qr-batch-new-key')
    assert another.status_code in (202, 409), another.text
    if another.status_code == 202:
        result = wait_batch(qr_service, another.json()['batchId'])
        assert result['items'][0]['status'] != 'reported_success'
    assert len(qr_service.gateway.calls) == 1


def test_polling_and_revoke_remain_available_while_upstream_is_in_flight(qr_service):
    first = login(qr_service)
    qr_service.gateway.block_uid = '1011'
    created = create_batch(qr_service, [first])
    assert created.status_code == 202, created.text
    assert qr_service.gateway.started.wait(2)
    start = time.monotonic()
    current = qr_service.client.get('/api/qr/batches/' + created.json()['batchId'])
    assert current.status_code == 200
    assert time.monotonic() - start < 1
    assert post(qr_service, f'/api/accounts/{first}/revoke').status_code == 200
    qr_service.gateway.release.set()
    wait_batch(qr_service, created.json()['batchId'])
    account = qr_service.client.get('/api/state').json()['accounts'][0]
    assert account['status'] == 'unauthorized'
    assert account['grant'] is None
    private = qr_service.app.state.vault.load(qr_service.client.cookies.get(COOKIE))
    assert private['accounts'][0]['_cookies'] == []


def test_upstream_timeout_is_recorded_without_automatic_resubmission(qr_service):
    first = login(qr_service)
    qr_service.gateway.statuses['1011'] = UpstreamError('timeout_unknown', '合成超时', details={'submitted': True, 'phase': 'submit'})
    created = create_batch(qr_service, [first])
    assert created.status_code == 202, created.text
    result = wait_batch(qr_service, created.json()['batchId'])
    assert result['items'][0]['status'] == 'outcome_unknown'
    assert result['summary']['succeeded'] == 0
    assert len(qr_service.gateway.calls) == 1


def test_batch_mutations_require_csrf_before_any_upstream_call(qr_service):
    first = login(qr_service)
    response = qr_service.client.post('/api/qr/batches', json={'accountIds': [first], 'qrText': QR},
        headers={'Idempotency-Key': 'qr-no-csrf-key'})
    assert response.status_code == 403
    assert qr_service.gateway.calls == []


@pytest.mark.parametrize('text', [
    'https://outside.example/sign?id=300&enc=synthetic-enc',
    'https://mobilelearn.chaoxing.com/widget/sign/e?id=300',
    'https://mobilelearn.chaoxing.com/widget/sign/e?id=300&id=301&enc=synthetic-enc',
])
def test_invalid_or_ambiguous_qr_never_reaches_upstream(qr_service, text):
    first = login(qr_service)
    response = post(qr_service, '/api/qr/batches', {'accountIds': [first], 'qrText': text}, key='qr-invalid-code')
    assert response.status_code in (400, 422), response.text
    assert qr_service.gateway.calls == []


@pytest.mark.parametrize(('text', 'message'), [
    ('https://mobilelearn.chaoxing.com/widget/group/sign?id=300&enc=private-qr-token', '官方签到二维码'),
    ('https://mobilelearn.chaoxing.com/widget/sign/e?id=300', '缺少有效的签到活动或 enc'),
])
def test_rejected_scan_logs_only_safe_structure_and_returns_parser_reason(qr_service, caplog, text, message):
    first = login(qr_service)
    caplog.set_level('INFO', logger='uvicorn.error.course.qr')
    response = post(qr_service, '/api/qr/batches', {'accountIds': [first], 'qrText': text}, key='qr-diagnostic-reject')
    assert response.status_code == 422
    assert response.json()['error']['code'] == 'invalid_qr'
    assert message in response.json()['error']['message']
    assert 'rejected_before_upstream' in caplog.text
    assert 'accountCount' in caplog.text
    for secret in (text, 'private-qr-token', PASSWORD, first):
        assert secret not in caplog.text
    assert qr_service.gateway.calls == []
    assert qr_service.client.get('/api/qr/batches').json()['batches'] == []


def test_parsed_scan_logs_structure_without_claiming_sign_success(qr_service, caplog):
    first = login(qr_service)
    caplog.set_level('INFO', logger='uvicorn.error.course.qr')
    created = create_batch(qr_service, [first], key='qr-diagnostic-parsed')
    assert created.status_code == 202
    wait_batch(qr_service, created.json()['batchId'])
    records = [r.getMessage() for r in caplog.records if r.name == 'uvicorn.error.course.qr']
    assert len(records) == 1
    assert '"outcome": "parsed"' in records[0]
    for secret in (QR, 'synthetic-enc', PASSWORD, first, 'reported_success'):
        assert secret not in records[0]


def test_failed_page_is_private_but_safe_reason_reaches_receipt_and_log(qr_service, caplog):
    first = login(qr_service)
    caplog.set_level('INFO', logger='uvicorn.error.course.qr')
    private = {'endpoint': '/widget/sign/signIn', 'httpStatus': 200,
               'body': '<h1>二维码已过期</h1><p>private-official-page-token</p>', 'bodyTruncated': False}
    details = {'status': 'rejected', 'reasonCode': 'qr_expired', 'headingKind': 'failure',
               'matchedReasons': ['qr_expired'], 'successMarker': False}

    def failed_page(cookies, identity, qr_text, *, location=None):
        return {'activeId': '300', 'status': 'rejected', 'message': '学习通提示二维码已过期。',
                'phase': 'submit', 'submitted': True, 'httpStatus': 200,
                'reasonCode': 'qr_expired', 'diagnostics': details, '_diagnostic': private}

    qr_service.gateway.sign_qr = failed_page
    created = create_batch(qr_service, [first], key='qr-private-diagnostic')
    assert created.status_code == 202
    result = wait_batch(qr_service, created.json()['batchId'])
    item = result['items'][0]
    assert item['reasonCode'] == 'qr_expired'
    assert item['diagnostics'] == details
    token = qr_service.client.cookies.get(COOKIE)
    assert qr_service.app.state.vault.get_diagnostic(token, item['requestId']) == private
    public_job = qr_service.client.get('/api/sign-requests/' + item['requestId'])
    assert public_job.status_code == 200
    assert public_job.json()['reasonCode'] == 'qr_expired'
    assert 'qr_result' in caplog.text
    assert 'qr_expired' in caplog.text
    for exposed in (str(result), public_job.text, caplog.text):
        assert 'private-official-page-token' not in exposed
        assert '_diagnostic' not in exposed


def test_duplicate_selection_cannot_submit_an_account_twice(qr_service):
    first = login(qr_service)
    response = create_batch(qr_service, [first, first])
    assert response.status_code in (202, 422), response.text
    if response.status_code == 202:
        result = wait_batch(qr_service, response.json()['batchId'])
        assert len(result['items']) == 1
        assert len(qr_service.gateway.calls) == 1
    else:
        assert qr_service.gateway.calls == []


def test_unfinished_batch_is_interrupted_after_restart_without_replay(qr_service):
    first = login(qr_service)
    qr_service.gateway.block_uid = '1011'
    created = create_batch(qr_service, [first])
    assert created.status_code == 202, created.text
    assert qr_service.gateway.started.wait(2)
    replacement_gateway = BatchGateway()
    restarted = create_app(settings=qr_service.settings, gateway=replacement_gateway)
    with TestClient(restarted, base_url=ORIGIN) as client:
        client.cookies.update(qr_service.client.cookies)
        response = client.get('/api/qr/batches/' + created.json()['batchId'])
        assert response.status_code == 200, response.text
        assert response.json()['status'] == 'interrupted'
        assert response.json()['summary']['succeeded'] == 0
        assert replacement_gateway.calls == []
    qr_service.gateway.release.set()
