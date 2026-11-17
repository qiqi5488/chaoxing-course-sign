"""Cookie namespace regressions; all databases and identities are synthetic."""
from http.cookiejar import CookieJar
from pathlib import Path
import re
import sys

import pytest
from fastapi.testclient import TestClient

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import server
from test_server import FakeGateway, PASSWORD

LIVE_ORIGIN = 'http://127.0.0.1:8000'
QA_ORIGIN = 'http://127.0.0.1:4176'


def app_at(directory, origin=LIVE_ORIGIN):
    return server.create_app(server.Settings(data_dir=directory, public_origin=origin), FakeGateway())


def login(client, identifier='18800001111'):
    initial = client.get('/api/bootstrap')
    assert initial.status_code == 200, initial.text
    result = client.post('/api/accounts/login', json={
        'identifier': identifier, 'password': PASSWORD, 'grantDays': 7,
    }, headers={'X-CSRF-Token': initial.json()['csrfToken'], 'Origin': str(client.base_url).rstrip('/')})
    assert result.status_code == 200, result.text
    return result.json()['accountId']


@pytest.mark.parametrize('first,second', [
    ('https://Example.TEST', 'https://example.test:443/'),
    ('http://localhost', 'http://LOCALHOST:80/'),
    ('http://127.0.0.1:8000', 'http://127.0.0.1:8000/'),
])
def test_cookie_name_uses_canonical_origin_and_is_stable(first, second):
    name = server.cookie_name(first)
    assert name == server.cookie_name(second)
    assert name == server.cookie_name(first)
    assert re.fullmatch(r'keqian_session_[0-9a-f]{16}', name)
    assert server.COOKIE == server.cookie_name(LIVE_ORIGIN)
    assert server.LEGACY_COOKIE == 'keqian_session'


def test_different_ports_do_not_overwrite_account_space_in_shared_cookie_jar(tmp_path):
    live = app_at(tmp_path / 'live')
    qa = app_at(tmp_path / 'qa', QA_ORIGIN)
    shared = CookieJar()
    with TestClient(live, base_url=LIVE_ORIGIN, cookies=shared) as original, \
            TestClient(qa, base_url=QA_ORIGIN, cookies=shared) as test_site:
        assert original.cookies.jar is shared and test_site.cookies.jar is shared
        original_id = login(original)
        original_token = original.cookies.get(live.state.cookie_name)
        test_id = login(test_site, '18800002222')
        assert live.state.cookie_name != qa.state.cookie_name
        assert original.cookies.get(live.state.cookie_name) == original_token
        assert test_site.cookies.get(qa.state.cookie_name) != original_token
        refreshed = original.get('/api/bootstrap')
        assert refreshed.status_code == 200
        assert [a['id'] for a in refreshed.json()['accounts']] == [original_id]
        assert test_id not in [a['id'] for a in refreshed.json()['accounts']]
        assert test_site.get('/api/state').json()['accounts'][0]['id'] == test_id


def test_restart_and_hash_navigation_preserve_scoped_workspace(tmp_path):
    settings = server.Settings(tmp_path / 'persistent', LIVE_ORIGIN)
    shared = CookieJar()
    first = server.create_app(settings, FakeGateway())
    with TestClient(first, base_url=LIVE_ORIGIN, cookies=shared) as client:
        original_id = login(client)
        assert client.get('/#/scan').status_code == 200
        assert client.get('/api/state').json()['accounts'][0]['id'] == original_id
    second = server.create_app(settings, FakeGateway())
    assert first.state.cookie_name == second.state.cookie_name
    with TestClient(second, base_url=LIVE_ORIGIN, cookies=shared) as client:
        refreshed = client.get('/api/bootstrap')
        assert refreshed.status_code == 200
        assert refreshed.json()['accounts'][0]['id'] == original_id


def test_valid_legacy_cookie_migrates_without_removing_legacy_cookie(tmp_path):
    app = app_at(tmp_path / 'legacy')
    with TestClient(app, base_url=LIVE_ORIGIN) as client:
        account_id = login(client)
        token = client.cookies.get(app.state.cookie_name)
        client.cookies.clear()
        client.cookies.set(server.LEGACY_COOKIE, token, domain='127.0.0.1', path='/')
        result = client.get('/api/bootstrap')
        assert result.status_code == 200, result.text
        assert result.json()['accounts'][0]['id'] == account_id
        assert client.cookies.get(app.state.cookie_name) == token
        assert client.cookies.get(server.LEGACY_COOKIE) == token
        assert 'httponly' in result.headers.get('set-cookie', '').lower()
        assert client.get('/api/state').json()['accounts'][0]['id'] == account_id


def test_legacy_token_from_another_database_cannot_adopt_its_accounts(tmp_path):
    source = app_at(tmp_path / 'source')
    with TestClient(source, base_url=LIVE_ORIGIN) as client:
        source_id = login(client)
        foreign_token = client.cookies.get(source.state.cookie_name)
    target = app_at(tmp_path / 'target')
    with TestClient(target, base_url=LIVE_ORIGIN) as client:
        client.cookies.set(server.LEGACY_COOKIE, foreign_token, domain='127.0.0.1', path='/')
        response = client.get('/api/bootstrap')
        assert response.status_code in (200, 409), response.text
        if response.status_code == 200:
            assert response.json()['accounts'] == []
            assert client.cookies.get(target.state.cookie_name) != foreign_token
        else:
            assert response.json()['error']['code'] == 'workspace_changed'
        assert source_id not in response.text
        assert target.state.vault.load(foreign_token) is None


def test_valid_scoped_cookie_takes_precedence_over_other_valid_legacy_space(tmp_path):
    app = app_at(tmp_path / 'precedence')
    with TestClient(app, base_url=LIVE_ORIGIN) as first, TestClient(app, base_url=LIVE_ORIGIN) as other:
        first_id = login(first)
        other_id = login(other, '18800002222')
        other_token = other.cookies.get(app.state.cookie_name)
        first.cookies.set(server.LEGACY_COOKIE, other_token, domain='127.0.0.1', path='/')
        response = first.get('/api/bootstrap')
        assert response.status_code == 200
        assert [a['id'] for a in response.json()['accounts']] == [first_id]
        assert other_id not in response.text
        assert other.get('/api/state').json()['accounts'][0]['id'] == other_id


def test_invalid_scoped_cookie_never_falls_back_or_silently_creates_space(tmp_path):
    app = app_at(tmp_path / 'invalid-scoped')
    with TestClient(app, base_url=LIVE_ORIGIN) as client:
        original_id = login(client)
        valid_token = client.cookies.get(app.state.cookie_name)
        with app.state.vault.connection() as db:
            before = db.execute('SELECT COUNT(*) FROM vaults').fetchone()[0]
        client.cookies.clear()
        client.cookies.set(app.state.cookie_name, 'invalid-synthetic-token', domain='127.0.0.1', path='/')
        client.cookies.set(server.LEGACY_COOKIE, valid_token, domain='127.0.0.1', path='/')
        response = client.get('/api/bootstrap')
        assert response.status_code == 409, response.text
        assert response.json()['error']['code'] == 'workspace_changed'
        assert original_id not in response.text
        assert client.cookies.get(app.state.cookie_name) == 'invalid-synthetic-token'
        assert client.cookies.get(server.LEGACY_COOKIE) == valid_token
        with app.state.vault.connection() as db:
            assert db.execute('SELECT COUNT(*) FROM vaults').fetchone()[0] == before
        assert app.state.vault.load(valid_token)['accounts'][0]['id'] == original_id
