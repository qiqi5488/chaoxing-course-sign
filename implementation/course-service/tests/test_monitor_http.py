"""Monitor API ownership, consent and request validation with no network calls."""
from pathlib import Path
from types import SimpleNamespace
import sys

import pytest
from fastapi.testclient import TestClient

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from server import Settings, create_app
from test_server import FakeGateway, PASSWORD, ORIGIN


@pytest.fixture
def api(tmp_path):
    gateway = FakeGateway()
    app = create_app(Settings(tmp_path / 'http-monitor', ORIGIN), gateway)
    app.state.monitor_manager.start = lambda: None  # Drive scheduling in manager tests.
    with TestClient(app, base_url=ORIGIN) as client:
        initial = client.get('/api/bootstrap').json()
        headers = {'Origin': ORIGIN, 'X-CSRF-Token': initial['csrfToken']}
        result = client.post('/api/accounts/login', headers=headers,
                             json={'identifier': '18800001111', 'password': PASSWORD}).json()
        yield SimpleNamespace(client=client, headers=headers, account=result['accountId'], app=app, gateway=gateway)


def test_monitor_defaults_are_off_and_mutations_require_csrf(api):
    url = f'/api/accounts/{api.account}/monitor'
    config = {'enabled': True, 'autoSign': True, 'intervalSeconds': 10}
    assert api.client.get('/api/monitor').json()['accounts'][0]['enabled'] is False
    assert api.client.put(url, json=config).status_code == 403
    assert api.client.get('/api/monitor').json()['tasks'] == []
    assert api.gateway.sign_calls == 0


def test_configuration_and_manual_check_only_schedule_work(api):
    url = f'/api/accounts/{api.account}/monitor'
    result = api.client.put(url, json={'enabled': True, 'autoSign': False, 'intervalSeconds': 10}, headers=api.headers)
    assert result.status_code == 200
    config = result.json()['accounts'][0]
    assert config['enabled'] and not config['autoSign']
    assert config['nextCheckAt'] and config['intervalSeconds'] == 10
    assert api.client.post(url + '/check', json={}, headers=api.headers).status_code == 202
    assert api.gateway.sign_calls == 0
    assert '_cookies' not in result.text and '_identity' not in result.text and 'version' not in result.text


@pytest.mark.parametrize('change', [{'intervalSeconds': 0}, {'intervalSeconds': 9}, {'intervalSeconds': 301},
                                   {'accountId': 'forged'}, {'uid': '1002'}])
def test_monitor_rejects_invalid_frequency_and_forged_context(api, change):
    response = api.client.put(f'/api/accounts/{api.account}/monitor', headers=api.headers,
                             json={'enabled': True, 'autoSign': True, 'intervalSeconds': 10, **change})
    assert response.status_code == 422
    assert api.client.get('/api/monitor').json()['accounts'][0]['enabled'] is False


def test_revocation_stops_persistent_monitor_and_prevents_reenable(api):
    url = f'/api/accounts/{api.account}/monitor'
    assert api.client.put(url, headers=api.headers, json={'enabled': True, 'autoSign': True}).status_code == 200
    assert api.client.post(f'/api/accounts/{api.account}/revoke', headers=api.headers).status_code == 200
    assert api.client.get('/api/monitor').json()['accounts'][0]['enabled'] is False
    assert api.client.put(url, headers=api.headers, json={'enabled': True}).status_code == 409
    assert api.client.post(url + '/check', headers=api.headers).status_code == 409


def test_another_browser_cannot_query_or_enable_monitor(api):
    with TestClient(api.app, base_url=ORIGIN) as other:
        csrf = other.get('/api/bootstrap').json()['csrfToken']
        assert other.get('/api/monitor').json()['accounts'] == []
        response = other.put(f'/api/accounts/{api.account}/monitor', json={'enabled': True, 'autoSign': True},
                             headers={'Origin': ORIGIN, 'X-CSRF-Token': csrf})
        assert response.status_code == 404


def test_disabling_and_removing_account_clear_monitor_schedule(api):
    url = f'/api/accounts/{api.account}/monitor'
    assert api.client.post(url + '/check', headers=api.headers).status_code == 409
    assert api.client.put(url, headers=api.headers, json={'enabled': True}).status_code == 200
    assert api.client.put(url, headers=api.headers, json={'enabled': False}).status_code == 200
    assert api.client.post('/api/accounts/remove', headers=api.headers, json={'ids': [api.account]}).status_code == 200
    assert api.client.get('/api/monitor').json()['accounts'] == []
