"""SSE authorization, snapshot isolation, updates and shutdown without upstream."""
import json
from pathlib import Path
import sys
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from server import Settings, create_app
from test_course_monitor import account
from test_server import FakeGateway, ORIGIN


@pytest.fixture
def app(tmp_path):
    result = create_app(Settings(tmp_path / 'sse', ORIGIN), FakeGateway())
    result.state.monitor_manager.start = lambda: None
    yield result
    result.state.monitor_manager.close()
    result.state.batch_manager.close()


def test_event_endpoint_requires_same_origin_and_existing_workspace(app):
    with TestClient(app, base_url=ORIGIN) as client:
        assert client.get('/api/monitor/events').status_code == 401
        assert client.get('/api/monitor/events', headers={'Origin': 'https://other.example'}).status_code == 403
        assert client.get('/api/monitor/events', headers={'Sec-Fetch-Site': 'cross-site'}).status_code == 403


@pytest.mark.asyncio
async def test_initial_snapshot_persistent_updates_removal_and_shutdown(app):
    vault = app.state.vault
    token, state = vault.create()
    other_token, other_state = vault.create()
    state['accounts'], other_state['accounts'] = [account()], [account('other-account', '1002')]
    vault.save(token, state)
    vault.save(other_token, other_state)
    manager = app.state.monitor_manager
    with vault.lock(token):
        manager.configure(token, 'account-1', True, False, 10)
        private = vault.load_monitor(token)
        private['accounts']['account-1']['imDeviceId'] = 'private-device-marker'
        private['accounts']['account-1']['inbox'] = [{'private': 'not-public'}]
        vault.save_monitor(token, private)
    async def disconnected():
        return False
    request = SimpleNamespace(cookies={app.state.cookie_name: token}, is_disconnected=disconnected)
    endpoint = next(route.endpoint for route in app.routes if getattr(route, 'path', '') == '/api/monitor/events')
    response = await endpoint(request)
    assert response.headers['x-accel-buffering'] == 'no'
    assert response.media_type == 'text/event-stream'
    stream = response.body_iterator
    assert await anext(stream) == 'retry: 3000\n\n'
    first = await anext(stream)
    assert first.startswith('event: monitor\ndata: ')
    payload = json.loads(first.split('data: ', 1)[1])
    assert [row['accountId'] for row in payload['accounts']] == ['account-1']
    assert 'other-account' not in first and 'private-device-marker' not in first and 'not-public' not in first
    assert token not in first and '_cookies' not in first and 'version' not in first
    with vault.lock(token):
        private = vault.load_monitor(token)
        private['accounts']['account-1'].update(realtimeStatus='connected', lastMessageAt='2026-11-13T10:00:00Z')
        vault.save_monitor(token, private)
    update = json.loads((await anext(stream)).split('data: ', 1)[1])
    assert update['accounts'][0]['realtimeStatus'] == 'connected'
    assert update['accounts'][0]['lastMessageAt'] == '2026-11-13T10:00:00Z'
    state['accounts'] = []
    vault.save(token, state)
    removed = json.loads((await anext(stream)).split('data: ', 1)[1])
    assert removed['accounts'] == [] and removed['tasks'] == []
    manager.closing.set()
    with pytest.raises(StopAsyncIteration):
        await anext(stream)


def test_revisions_are_private_to_workspace_and_change_only_after_save(app):
    vault = app.state.vault
    token, state = vault.create()
    other, _ = vault.create()
    before = vault.monitor_revision(other)
    vault.save_monitor(token, {'accounts': {}, 'tasks': []})
    assert vault.monitor_revision(token) == 1
    assert vault.monitor_revision(other) == before
    vault.load_monitor(token)
    assert vault.monitor_revision(token) == 1
    vault.save(token, state)
    assert vault.monitor_revision(token) == 2
