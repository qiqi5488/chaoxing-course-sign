"""Account-detail HTTP tests with synthetic identities and no external requests."""
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from pathlib import Path
from types import SimpleNamespace
import sys
import threading
import time

import pytest
from fastapi.testclient import TestClient

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from server import Settings, create_app
from upstream import UpstreamError
from vault import iso

ORIGIN = 'http://127.0.0.1:8000'
PASSWORD = 'synthetic-details-password'
COOKIE_SECRET = 'synthetic-details-upstream-cookie-private'


class DetailsGateway:
    def __init__(self):
        self.calls = []
        self.error = None
        self.all_sources_failed = False
        self.empty_courses = False
        self.block_uid = None
        self.login_version = 0
        self.unexpected_fields = False
        self.started = threading.Event()
        self.release = threading.Event()

    def login(self, identifier, password):
        assert password == PASSWORD
        self.login_version += 1
        uid = '10' + identifier[-2:]
        return SimpleNamespace(uid=uid, fid='200', name='真实姓名' + identifier[-2:], school='测试大学',
            cookies=[{'name': '_uid', 'value': uid, 'domain': '.chaoxing.com', 'path': '/',
                      'secure': True, 'expires': None, 'rest': {}},
                     {'name': 'session', 'value': COOKIE_SECRET + str(self.login_version), 'domain': '.chaoxing.com',
                      'path': '/', 'secure': True, 'expires': None, 'rest': {}}],
            session_expires_at=None)

    def get_account_details(self, cookies, identity):
        uid = identity['uid']
        cookie_uid = next(c['value'] for c in cookies if c['name'] == '_uid')
        assert uid == cookie_uid
        self.calls.append({'uid': uid, 'cookieUid': cookie_uid})
        if self.block_uid == uid:
            self.started.set()
            if not self.release.wait(5):
                raise AssertionError('Synthetic details request was not released')
        if self.error:
            raise self.error
        suffix = uid[-2:]
        sources = [{'id': 'normal', 'label': '普通课程', 'status': 'ok', 'message': ''},
                   {'id': 'mirror', 'label': '补充课程', 'status': 'error', 'message': '该来源暂时不可用'}]
        if self.all_sources_failed:
            sources[0].update(status='error', message='普通课程读取未完成')
        courses = [] if self.empty_courses or self.all_sources_failed else [{
            'key': 'course-' + uid, 'courseId': '30' + suffix, 'classId': '40' + suffix,
            'name': '测试课程' + suffix, 'teacher': '测试教师', 'className': '教学班' + suffix,
            'term': '2026 秋', 'coverUrl': '', 'sourceLabels': ['普通课程'],
        }]
        result = {'profile': {'name': '真实姓名' + suffix, 'school': '测试大学',
                            'studentNumberMasked': '20****' + suffix, 'department': '测试学院', 'major': '测试专业'},
                'courses': courses, 'sources': sources, 'fetchedAt': iso(), 'cookies': deepcopy(cookies)}
        if self.unexpected_fields:
            result['profile'].update(password=PASSWORD, cookies=deepcopy(cookies), identifier='18800001111')
            result['sources'][0]['rawResponse'] = COOKIE_SECRET
            if result['courses']:
                result['courses'][0].update(cookies=deepcopy(cookies), rawProfile={'identifier': '18800001111'})
        return result


@pytest.fixture
def details_service(tmp_path):
    gateway = DetailsGateway()
    settings = Settings(data_dir=tmp_path / 'details-data', public_origin=ORIGIN)
    app = create_app(settings=settings, gateway=gateway)
    with TestClient(app, base_url=ORIGIN) as client:
        bootstrap = client.get('/api/bootstrap')
        assert bootstrap.status_code == 200
        service = SimpleNamespace(gateway=gateway, settings=settings, app=app, client=client,
                                  csrf=bootstrap.json()['csrfToken'])
        try:
            yield service
        finally:
            gateway.release.set()


def mutate(service, path, payload):
    return service.client.post(path, json=payload,
        headers={'X-CSRF-Token': service.csrf, 'Origin': ORIGIN})


def login(service, suffix='11', nickname='我的备注'):
    response = mutate(service, '/api/accounts/login', {
        'identifier': '188000011' + suffix, 'password': PASSWORD, 'grantDays': 7, 'nickname': nickname,
    })
    assert response.status_code == 200, response.text
    return response.json()['accountId']


def details(service, account_id, refresh=False):
    return service.client.get(f'/api/accounts/{account_id}/details', params={'refresh': 'true'} if refresh else {})


def private_state(service):
    token = service.client.cookies.get(service.app.state.cookie_name)
    return token, service.app.state.vault.load(token)


def test_details_use_each_selected_accounts_own_identity_and_cookie(details_service):
    first, second = login(details_service), login(details_service, '22', '第二账号')
    a, b = details(details_service, first), details(details_service, second)
    assert a.status_code == b.status_code == 200
    assert a.json()['account']['id'] == first
    assert b.json()['account']['id'] == second
    assert a.json()['courses'][0]['courseId'] == '3011'
    assert b.json()['courses'][0]['courseId'] == '3022'
    assert details_service.gateway.calls == [{'uid': '1011', 'cookieUid': '1011'}, {'uid': '1022', 'cookieUid': '1022'}]


def test_details_do_not_return_credentials_or_replace_the_account_nickname(details_service):
    account_id = login(details_service, nickname='用户自己的备注')
    response = details(details_service, account_id)
    assert response.status_code == 200
    result = response.json()
    assert result['account']['name'] == '用户自己的备注'
    assert result['profile']['name'] == '真实姓名11'
    assert result['profile']['studentNumberMasked'] == '20****11'
    assert result['fetchedAt']
    assert 'cookies' not in result
    assert '_identity' not in result['account']
    assert '_identifier' not in result['account']
    for secret in (PASSWORD, COOKIE_SECRET, '18800001111'):
        assert secret not in response.text
    state = details_service.client.get('/api/state').json()
    assert state['accounts'][0]['name'] == '用户自己的备注'


def test_partial_source_failure_remains_visible_beside_successful_courses(details_service):
    account_id = login(details_service)
    response = details(details_service, account_id)
    assert response.status_code == 200
    result = response.json()
    assert len(result['courses']) == 1
    assert any(source['status'] == 'ok' for source in result['sources'])
    failed = [source for source in result['sources'] if source['status'] == 'error']
    assert failed and failed[0]['message']


def test_successful_empty_courses_are_distinct_from_all_sources_failing(details_service):
    account_id = login(details_service)
    details_service.gateway.empty_courses = True
    empty = details(details_service, account_id).json()
    assert empty['courses'] == []
    assert any(source['status'] == 'ok' for source in empty['sources'])
    details_service.gateway.all_sources_failed = True
    failed = details(details_service, account_id, refresh=True).json()
    assert failed['courses'] == []
    assert all(source['status'] == 'error' for source in failed['sources'])


def test_details_cache_is_per_account_and_explicit_refresh_requeries(details_service):
    first, second = login(details_service), login(details_service, '22')
    assert details(details_service, first).status_code == 200
    assert details(details_service, first).status_code == 200
    assert len(details_service.gateway.calls) == 1
    assert details(details_service, second).status_code == 200
    assert len(details_service.gateway.calls) == 2
    assert details(details_service, first, refresh=True).status_code == 200
    assert len(details_service.gateway.calls) == 3


def test_all_sources_failed_are_not_cached_as_an_empty_course_success(details_service):
    account_id = login(details_service)
    details_service.gateway.all_sources_failed = True
    assert details(details_service, account_id).status_code == 200
    assert details(details_service, account_id).status_code == 200
    assert len(details_service.gateway.calls) == 2


def test_another_browser_cannot_read_account_details(details_service):
    account_id = login(details_service)
    with TestClient(details_service.app, base_url=ORIGIN) as other:
        assert other.get('/api/bootstrap').status_code == 200
        response = other.get(f'/api/accounts/{account_id}/details')
        assert response.status_code == 404
    assert details_service.gateway.calls == []


def test_revoked_account_cannot_read_or_reuse_cached_details(details_service):
    account_id = login(details_service)
    assert details(details_service, account_id).status_code == 200
    assert mutate(details_service, f'/api/accounts/{account_id}/revoke', {}).status_code == 200
    response = details(details_service, account_id)
    assert response.status_code in (401, 409)
    assert len(details_service.gateway.calls) == 1


def test_expired_upstream_session_blocks_details_before_upstream(details_service):
    account_id = login(details_service)
    token, state = private_state(details_service)
    state['accounts'][0]['sessionExpiresAt'] = iso(time.time() - 1)
    details_service.app.state.vault.save(token, state)
    response = details(details_service, account_id)
    assert response.status_code in (401, 409)
    assert details_service.gateway.calls == []


def test_upstream_session_expiry_is_persisted_without_removing_account(details_service):
    account_id = login(details_service)
    details_service.gateway.error = UpstreamError('session_expired', '学习通会话已失效', 401)
    response = details(details_service, account_id)
    assert response.status_code == 401
    assert response.json()['error']['code'] == 'session_expired'
    _, state = private_state(details_service)
    assert len(state['accounts']) == 1
    assert state['accounts'][0]['status'] == 'expired'
    assert state['accounts'][0]['_cookies'] == []


def test_upstream_failure_is_not_reported_as_a_successful_empty_course_list(details_service):
    account_id = login(details_service)
    details_service.gateway.error = UpstreamError('profile_http_error', '账号资料暂时无法读取', 502)
    response = details(details_service, account_id)
    assert response.status_code == 502
    assert 'courses' not in response.json()
    state = details_service.client.get('/api/state').json()
    assert len(state['accounts']) == 1
    assert state['accounts'][0]['status'] == 'active'


def test_revoke_is_responsive_and_inflight_details_cannot_restore_session(details_service):
    account_id = login(details_service)
    details_service.gateway.block_uid = '1011'
    with ThreadPoolExecutor(max_workers=1) as pool:
        pending = pool.submit(details, details_service, account_id)
        try:
            assert details_service.gateway.started.wait(2), 'Details request never reached synthetic upstream'
            began = time.monotonic()
            response = mutate(details_service, f'/api/accounts/{account_id}/revoke', {})
            elapsed = time.monotonic() - began
            assert response.status_code == 200
            assert elapsed < 1, 'Workspace lock was held while reading upstream courses'
        finally:
            details_service.gateway.release.set()
        assert pending.result(timeout=3).status_code in (401, 409)
    _, state = private_state(details_service)
    assert state['accounts'][0]['status'] == 'unauthorized'
    assert state['accounts'][0]['grant'] is None
    assert state['accounts'][0]['_cookies'] == []


def test_unexpected_upstream_fields_are_removed_from_every_public_section(details_service):
    account_id = login(details_service)
    details_service.gateway.unexpected_fields = True
    response = details(details_service, account_id)
    assert response.status_code == 200
    result = response.json()
    assert 'password' not in result['profile'] and 'identifier' not in result['profile']
    assert 'cookies' not in result['profile'] and 'cookies' not in result['courses'][0]
    assert 'rawProfile' not in result['courses'][0] and 'rawResponse' not in result['sources'][0]
    for secret in (PASSWORD, COOKIE_SECRET, '18800001111'):
        assert secret not in response.text


def test_partial_failure_can_be_retried_with_explicit_refresh_even_when_cached(details_service):
    account_id = login(details_service)
    assert details(details_service, account_id).status_code == 200
    cached = details(details_service, account_id)
    assert cached.status_code == 200
    assert len(details_service.gateway.calls) == 1
    assert any(source['status'] == 'error' for source in cached.json()['sources'])
    assert details(details_service, account_id, refresh=True).status_code == 200
    assert len(details_service.gateway.calls) == 2


def test_relogin_invalidates_the_previous_details_cache(details_service):
    account_id = login(details_service)
    assert details(details_service, account_id).status_code == 200
    response = mutate(details_service, f'/api/accounts/{account_id}/relogin', {'password': PASSWORD, 'grantDays': 7})
    assert response.status_code == 200
    assert details(details_service, account_id).status_code == 200
    assert len(details_service.gateway.calls) == 2


def test_refresh_failure_does_not_silently_return_a_previous_success(details_service):
    account_id = login(details_service)
    assert details(details_service, account_id).status_code == 200
    details_service.gateway.error = UpstreamError('profile_http_error', '账号资料暂时无法读取', 502)
    response = details(details_service, account_id, refresh=True)
    assert response.status_code == 502
    assert response.json()['error']['code'] == 'profile_http_error'
    assert 'courses' not in response.json()


def test_inflight_details_cannot_overwrite_a_later_successful_login(details_service):
    account_id = login(details_service)
    details_service.gateway.block_uid = '1011'
    with ThreadPoolExecutor(max_workers=1) as pool:
        pending = pool.submit(details, details_service, account_id)
        try:
            assert details_service.gateway.started.wait(2)
            response = mutate(details_service, f'/api/accounts/{account_id}/relogin', {'password': PASSWORD, 'grantDays': 1})
            assert response.status_code == 200
            _, newer = private_state(details_service)
            new_cookie = next(c['value'] for c in newer['accounts'][0]['_cookies'] if c['name'] == 'session')
        finally:
            details_service.gateway.release.set()
        assert pending.result(timeout=3).status_code == 409
    _, final = private_state(details_service)
    assert final['accounts'][0]['status'] == 'active'
    assert final['accounts'][0]['grant']['days'] is None
    assert final['accounts'][0]['grant']['expiresAt'] is None
    assert next(c['value'] for c in final['accounts'][0]['_cookies'] if c['name'] == 'session') == new_cookie
