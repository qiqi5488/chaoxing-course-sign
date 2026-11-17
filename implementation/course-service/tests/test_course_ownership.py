"""Course direct requests must belong to the selected account before reservation."""
import pytest

from test_server import service, login, mutate, sign, SIGN_PAYLOAD


def saved_job(service, key):
    token = service.client.cookies.get(service.app.state.cookie_name)
    return service.app.state.vault.get_job(token, key)[0]


@pytest.mark.parametrize('kind', ['ordinary', 'gesture'])
def test_course_direct_ownership_is_checked_before_reserving_or_signing(service, monkeypatch, kind):
    account_id, _ = login(service)
    key = 'owned-course-' + kind
    original_details = service.gateway.get_account_details

    def details(cookies, identity):
        assert saved_job(service, key) is None
        assert service.gateway.sign_calls == 0
        return original_details(cookies, identity)

    monkeypatch.setattr(service.gateway, 'get_account_details', details)
    result = sign(service, account_id, key=key, payload={**SIGN_PAYLOAD, 'kind': kind})
    assert result.status_code == 200, result.text
    assert result.json()['status'] == 'reported_success'
    assert service.gateway.details_calls == service.gateway.sign_calls == 1
    assert saved_job(service, key)['status'] == 'reported_success'


@pytest.mark.parametrize('kind', ['ordinary', 'gesture'])
@pytest.mark.parametrize('courses', [
    [{'courseId': '457', 'classId': '789'}],
    [{'courseId': '456', 'classId': '790'}],
    [{'courseId': '456', 'classId': '790'}, {'courseId': '457', 'classId': '789'}],
    [],
])
def test_unowned_or_empty_course_pairs_never_reserve_or_sign(service, monkeypatch, kind, courses):
    account_id, _ = login(service)
    key = 'unowned-course-' + kind
    original_details = service.gateway.get_account_details

    def details(cookies, identity):
        return {**original_details(cookies, identity), 'courses': courses}

    monkeypatch.setattr(service.gateway, 'get_account_details', details)
    result = sign(service, account_id, key=key, payload={**SIGN_PAYLOAD, 'kind': kind})
    assert 400 <= result.status_code < 500, result.text
    assert service.gateway.details_calls == 1
    assert service.gateway.sign_calls == 0
    assert saved_job(service, key) is None


@pytest.mark.parametrize('kind', ['ordinary', 'gesture'])
@pytest.mark.parametrize('sources', [[], [{'id': 'normal', 'status': 'error'}]])
def test_course_ownership_without_a_successful_source_never_reserves_or_signs(service, monkeypatch, kind, sources):
    account_id, _ = login(service)
    key = 'unconfirmed-course-' + kind
    original_details = service.gateway.get_account_details

    def details(cookies, identity):
        return {**original_details(cookies, identity), 'sources': sources}

    monkeypatch.setattr(service.gateway, 'get_account_details', details)
    result = sign(service, account_id, key=key, payload={**SIGN_PAYLOAD, 'kind': kind})
    assert 400 <= result.status_code < 600, result.text
    assert service.gateway.details_calls == 1
    assert service.gateway.sign_calls == 0
    assert saved_job(service, key) is None


@pytest.mark.parametrize('kind', ['ordinary', 'gesture'])
def test_course_ownership_cache_cannot_authorize_another_account(service, monkeypatch, kind):
    first, _ = login(service)
    second, _ = login(service, '18800002222')
    original_details = service.gateway.get_account_details

    def details(cookies, identity):
        result = original_details(cookies, identity)
        if identity['uid'] == '1002':
            result['courses'] = [{'courseId': '800', 'classId': '900'}]
        return result

    monkeypatch.setattr(service.gateway, 'get_account_details', details)
    assert service.client.get(f'/api/accounts/{first}/details').status_code == 200
    key = 'other-account-course-' + kind
    result = sign(service, second, key=key, payload={**SIGN_PAYLOAD, 'kind': kind})
    assert 400 <= result.status_code < 500, result.text
    assert service.gateway.details_calls == 2
    assert service.gateway.sign_calls == 0
    assert saved_job(service, key) is None


@pytest.mark.parametrize('kind', ['ordinary', 'gesture'])
def test_historical_request_replay_precedes_course_ownership_queries(service, monkeypatch, kind):
    account_id, _ = login(service)
    key = 'course-history-' + kind
    payload = {**SIGN_PAYLOAD, 'kind': kind}
    first = sign(service, account_id, key=key, payload=payload)
    assert first.status_code == 200 and first.json()['status'] == 'reported_success'
    assert mutate(service, 'POST', f'/api/accounts/{account_id}/revoke', {}).status_code == 200

    def unexpected_details(*args):
        raise AssertionError('A saved receipt must not query course ownership again')

    monkeypatch.setattr(service.gateway, 'get_account_details', unexpected_details)
    replay = sign(service, account_id, key=key, payload=payload)
    assert replay.status_code == 200, replay.text
    assert replay.json() == first.json()
    assert service.gateway.details_calls == service.gateway.sign_calls == 1


@pytest.mark.parametrize('kind', ['ordinary', 'qr', 'gesture'])
def test_group_direct_entry_does_not_query_course_ownership(service, monkeypatch, kind):
    account_id, _ = login(service)

    def unexpected_details(*args):
        raise AssertionError('Group attendance has no course ownership lookup')

    monkeypatch.setattr(service.gateway, 'get_account_details', unexpected_details)
    result = sign(service, account_id, key='group-without-course-' + kind,
                  payload={'kind': kind, 'activityScope': 'group', 'activeId': '123'})
    assert result.status_code == 200, result.text
    assert result.json()['status'] == 'reported_success'
    assert service.gateway.details_calls == 0
    assert service.gateway.sign_calls == 1


@pytest.mark.parametrize('kind', ['ordinary', 'gesture'])
def test_course_ownership_cookie_refresh_reaches_sign_and_is_not_overwritten(service, monkeypatch, kind):
    account_id, _ = login(service)
    original_details, original_sign = service.gateway.get_account_details, service.gateway.sign
    fresh_cookie = {'name': 'ownership_refresh', 'value': 'synthetic-rotated-session',
                    'domain': '.chaoxing.com', 'path': '/', 'secure': True, 'expires': None, 'rest': {}}

    def details(cookies, identity):
        return {**original_details(cookies, identity), 'cookies': [*cookies, fresh_cookie]}

    def submit(cookies, identity, payload):
        assert fresh_cookie in cookies
        return original_sign(cookies, identity, payload)

    monkeypatch.setattr(service.gateway, 'get_account_details', details)
    monkeypatch.setattr(service.gateway, 'sign', submit)
    result = sign(service, account_id, key='course-fresh-cookie-' + kind,
                  payload={**SIGN_PAYLOAD, 'kind': kind})
    assert result.status_code == 200 and result.json()['status'] == 'reported_success'
    token = service.client.cookies.get(service.app.state.cookie_name)
    row = next(a for a in service.app.state.vault.load(token)['accounts'] if a['id'] == account_id)
    assert fresh_cookie in row['_cookies']
