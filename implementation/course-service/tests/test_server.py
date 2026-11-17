"""Integration tests for the HTTP service; all upstream traffic is substituted."""
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace
import sys

import pytest
from fastapi.testclient import TestClient

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from server import COOKIE, Settings, create_app
from upstream import UpstreamError
from vault import iso, DAY


ORIGIN = "http://127.0.0.1:8000"
PASSWORD = "test-password-never-persist-this"
COOKIE_SECRET = "upstream-cookie-private-marker"
SIGN_PAYLOAD = {"kind": "ordinary", "activeId": "123", "courseId": "456", "classId": "789"}


class FakeGateway:
    def __init__(self):
        self.login_calls = 0
        self.check_calls = 0
        self.details_calls = 0
        self.sign_calls = 0
        self.sign_status = "reported_success"
        self.login_error = None
        self.check_error = None
        self.sign_error = None
        self.last_identity = None
        self.session_expires_at = None

    def account(self, identifier="18800001111"):
        uid = "1001" if identifier == "18800001111" else "1002"
        return SimpleNamespace(
            uid=uid, fid="2001", name="测试同学" + uid[-1], school="测试大学",
            cookies=[{
                "name": "_uid", "value": uid, "domain": ".chaoxing.com", "path": "/",
                "secure": True, "expires": None, "rest": {"HttpOnly": None},
            }, {
                "name": "test_session", "value": COOKIE_SECRET, "domain": ".chaoxing.com",
                "path": "/", "secure": True, "expires": None, "rest": {"HttpOnly": None},
            }],
            session_expires_at=self.session_expires_at,
        )

    def login(self, identifier, password):
        self.login_calls += 1
        assert password == PASSWORD
        if self.login_error:
            raise self.login_error
        return self.account(identifier)

    def check(self, cookies):
        self.check_calls += 1
        if self.check_error:
            raise self.check_error
        uid = next(c["value"] for c in cookies if c["name"] == "_uid")
        return self.account("18800001111" if uid == "1001" else "18800002222")

    def get_account_details(self, cookies, identity):
        self.details_calls += 1
        return {"profile": {}, "courses": [{"courseId": "456", "classId": "789", "name": "测试课程"}],
                "sources": [{"id": "normal", "status": "ok"}], "cookies": cookies}

    def sign(self, cookies, identity, payload):
        self.sign_calls += 1
        self.last_identity = dict(identity)
        assert any(c["value"] == COOKIE_SECRET for c in cookies)
        assert payload["activeId"] == "123"
        if self.sign_error:
            raise self.sign_error
        return {"status": self.sign_status, "message": "替身上游返回", "cookies": cookies}


@pytest.fixture
def service(tmp_path):
    gateway = FakeGateway()
    settings = Settings(data_dir=tmp_path / "private-data", public_origin=ORIGIN)
    app = create_app(settings=settings, gateway=gateway)
    with TestClient(app, base_url=ORIGIN) as client:
        initial = client.get("/api/bootstrap")
        assert initial.status_code == 200
        token = initial.json()["csrfToken"]
        yield SimpleNamespace(client=client, gateway=gateway, settings=settings,
                              app=app, token=token, initial=initial)


def mutate(service, method, path, body=None, **headers):
    return service.client.request(method, path, json=body,
        headers={"X-CSRF-Token": service.token, "Origin": ORIGIN, **headers})


def login(service, identifier="18800001111", **extra):
    response = mutate(service, "POST", "/api/accounts/login", {
        "identifier": identifier, "password": PASSWORD, "grantDays": 7, **extra,
    })
    assert response.status_code == 200, response.text
    data = response.json()
    return data["accountId"], data["state"]


def sign(service, account_id, key="request-unique-0001", payload=None):
    return mutate(service, "POST", f"/api/accounts/{account_id}/sign",
                  payload or SIGN_PAYLOAD, **{"Idempotency-Key": key})


def test_bootstrap_starts_empty_with_http_only_browser_session(service):
    state = service.initial.json()
    assert state["mode"] == "live"
    assert state["accounts"] == []
    assert state["defaultAccountId"] is None
    cookie = service.initial.headers["set-cookie"].lower()
    assert "httponly" in cookie
    assert "samesite=" in cookie
    assert "no-store" in service.initial.headers.get("cache-control", "")


def test_login_immediately_grants_session_without_exposing_upstream_secrets(service):
    account_id, state = login(service, nickname="我的账号")
    account = state["accounts"][0]
    assert account["id"] == account_id
    assert account["name"] == "我的账号"
    assert account["status"] == "active"
    assert account["grant"]["days"] is None
    assert account["grant"]["expiresAt"] is None
    assert datetime.fromisoformat(account["grant"]["grantedAt"].replace("Z", "+00:00"))
    assert state["defaultAccountId"] == account_id
    assert "****" in account["maskedIdentifier"]
    public = service.client.get("/api/state")
    assert public.status_code == 200
    assert PASSWORD not in public.text
    assert COOKIE_SECRET not in public.text
    assert "18800001111" not in public.text
    assert "cookies" not in account


def test_csrf_and_cross_origin_requests_do_not_reach_login(service):
    body = {"identifier": "18800001111", "password": PASSWORD, "grantDays": 7}
    assert service.client.post("/api/accounts/login", json=body).status_code == 403
    result = mutate(service, "POST", "/api/accounts/login", body, Origin="https://unrelated.example")
    assert result.status_code == 403
    assert service.gateway.login_calls == 0


def test_another_browser_cannot_read_or_modify_accounts(service):
    account_id, _ = login(service)
    with TestClient(service.app, base_url=ORIGIN) as other:
        state = other.get("/api/bootstrap").json()
        assert state["accounts"] == []
        response = other.post(f"/api/accounts/{account_id}/revoke", json={},
            headers={"X-CSRF-Token": state["csrfToken"], "Origin": ORIGIN})
        assert response.status_code == 404
    assert service.client.get("/api/state").json()["accounts"][0]["status"] == "active"


def test_duplicate_verified_identity_updates_existing_account(service):
    first, _ = login(service, nickname="原备注")
    second, state = login(service, nickname="更新备注")
    assert first == second
    assert len(state["accounts"]) == 1
    assert state["accounts"][0]["name"] == "更新备注"


def test_revoke_clears_session_and_preserves_other_accounts(service):
    first, _ = login(service)
    second, _ = login(service, "18800002222")
    response = mutate(service, "POST", f"/api/accounts/{first}/revoke", {})
    assert response.status_code == 200
    state = response.json()["state"]
    accounts = {a["id"]: a for a in state["accounts"]}
    assert accounts[first]["grant"] is None
    assert accounts[first]["status"] == "unauthorized"
    assert accounts[second]["status"] == "active"
    assert state["defaultAccountId"] == second
    blocked = sign(service, first)
    assert blocked.status_code in (401, 409)
    assert service.gateway.sign_calls == 0


def test_relogin_uses_stored_identifier_and_retains_session_without_local_deadline(service):
    account_id, _ = login(service)
    mutate(service, "POST", f"/api/accounts/{account_id}/revoke", {})
    result = mutate(service, "POST", f"/api/accounts/{account_id}/relogin", {"password": PASSWORD, "grantDays": 1})
    assert result.status_code == 200
    assert result.json()["accountId"] == account_id
    account = result.json()["state"]["accounts"][0]
    assert account["status"] == "active"
    assert account["grant"]["days"] is None
    assert account["grant"]["expiresAt"] is None
    assert service.gateway.login_calls == 2


def test_failed_login_creates_no_authorized_account(service):
    service.gateway.login_error = UpstreamError("login_rejected", "登录未被接受", status_code=401)
    response = mutate(service, "POST", "/api/accounts/login", {
        "identifier": "18800001111", "password": PASSWORD, "grantDays": 7,
    })
    assert response.status_code == 401
    assert response.json()["error"]["code"] == "login_rejected"
    assert service.client.get("/api/state").json()["accounts"] == []


def test_login_diagnostics_expose_only_safe_error_metadata(service, caplog):
    service.gateway.login_error = UpstreamError('network_outcome_unknown', '登录连接失败', details={
        'phase': 'login_submit', 'transport_reason': 'tls', 'http_status': 502, 'raw_secret': COOKIE_SECRET,
    })
    response = mutate(service, 'POST', '/api/accounts/login', {
        'identifier': '18800001111', 'password': PASSWORD, 'grantDays': 7,
    })
    assert response.status_code == 502
    error = response.json()['error']
    assert error['phase'] == 'login_submit'
    assert error['transport_reason'] == 'tls'
    assert 'network_outcome_unknown' in caplog.text
    for private in (PASSWORD, COOKIE_SECRET, '18800001111'):
        assert private not in response.text
        assert private not in caplog.text


def test_validation_errors_never_echo_password(service):
    response = mutate(service, "POST", "/api/accounts/login", {
        "identifier": "18800001111", "password": PASSWORD, "grantDays": 999,
    })
    assert response.status_code == 422
    assert PASSWORD not in response.text
    assert service.gateway.login_calls == 0


def test_sign_idempotency_sends_once_and_rejects_changed_payload(service):
    account_id, _ = login(service)
    first = sign(service, account_id)
    assert first.status_code == 200, first.text
    second = sign(service, account_id)
    assert second.status_code == 200
    assert first.json()["requestId"] == second.json()["requestId"]
    assert first.json()["status"] == "reported_success"
    assert service.gateway.sign_calls == 1
    mismatch = sign(service, account_id, payload={**SIGN_PAYLOAD, "classId": "999"})
    assert mismatch.status_code == 409
    assert service.gateway.sign_calls == 1
    assert COOKIE_SECRET not in first.text
    assert service.gateway.last_identity["uid"] == "1001"


@pytest.mark.parametrize("status", ["unknown", "outcome_unknown", "requires_validation", "rejected"])
def test_sign_uncertain_or_failed_outcome_is_not_success_or_retried(service, status):
    account_id, _ = login(service)
    service.gateway.sign_status = status
    first = sign(service, account_id)
    assert first.status_code == 200, first.text
    assert first.json()["status"] == status
    again = sign(service, account_id)
    assert again.json()["status"] == status
    assert service.gateway.sign_calls == 1


def test_sign_rejects_client_forged_identity_before_upstream(service):
    account_id, _ = login(service)
    response = sign(service, account_id, payload={**SIGN_PAYLOAD, "uid": "999999"})
    assert response.status_code == 422
    assert service.gateway.sign_calls == 0


def test_sign_receipt_is_scoped_to_its_browser_workspace(service):
    account_id, _ = login(service)
    receipt = sign(service, account_id).json()
    request_id = receipt["requestId"]
    own = service.client.get(f"/api/sign-requests/{request_id}")
    assert own.status_code == 200
    assert own.json()["status"] == receipt["status"]
    with TestClient(service.app, base_url=ORIGIN) as other:
        other.get("/api/bootstrap")
        assert other.get(f"/api/sign-requests/{request_id}").status_code == 404


def test_accounts_survive_service_restart_but_credentials_are_encrypted(service):
    account_id, _ = login(service)
    data_files = [p for p in service.settings.data_dir.rglob("*") if p.is_file()]
    assert data_files
    for path in data_files:
        contents = path.read_bytes()
        assert COOKIE_SECRET.encode() not in contents, path.name
        assert PASSWORD.encode() not in contents, path.name
        assert b"18800001111" not in contents, path.name
    restarted = create_app(settings=service.settings, gateway=FakeGateway())
    with TestClient(restarted, base_url=ORIGIN) as client:
        client.cookies.update(service.client.cookies)
        response = client.get("/api/bootstrap")
        assert response.status_code == 200
        assert response.json()["accounts"][0]["id"] == account_id


def test_https_configuration_sets_secure_cookie(tmp_path):
    secure_origin = "https://accounts.example.test"
    app = create_app(settings=Settings(data_dir=tmp_path / "secure", public_origin=secure_origin), gateway=FakeGateway())
    with TestClient(app, base_url=secure_origin) as client:
        response = client.get("/api/bootstrap")
        assert response.status_code == 200
        assert "secure" in response.headers["set-cookie"].lower()


def test_legacy_grant_duration_is_ignored_after_saved_session_verification(service):
    account_id, _ = login(service)
    result = mutate(service, "POST", f"/api/accounts/{account_id}/grant", {"days": 30})
    assert result.status_code == 200
    assert result.json()["state"]["accounts"][0]["grant"]["days"] is None
    assert result.json()["state"]["accounts"][0]["grant"]["expiresAt"] is None
    assert service.gateway.check_calls == 1


def test_failed_grant_verification_marks_saved_session_expired(service):
    account_id, _ = login(service)
    service.gateway.check_error = UpstreamError("session_expired", "登录会话已失效", status_code=401)
    result = mutate(service, "POST", f"/api/accounts/{account_id}/grant", {"days": 30})
    assert result.status_code == 401
    state = service.client.get("/api/state").json()
    assert state["accounts"][0]["status"] == "expired"
    assert state["defaultAccountId"] is None
    token = service.client.cookies.get(COOKIE)
    assert service.app.state.vault.load(token)["accounts"][0]["_cookies"] == []
    assert sign(service, account_id).status_code == 409
    assert service.gateway.sign_calls == 0


def test_upstream_session_expiry_blocks_sign_and_removes_saved_cookies(service):
    import time
    account_id, _ = login(service)
    token = service.client.cookies.get(COOKIE)
    state = service.app.state.vault.load(token)
    state["accounts"][0]['sessionExpiresAt'] = iso(time.time() - 1)
    service.app.state.vault.save(token, state)
    result = sign(service, account_id)
    assert result.status_code == 409
    assert service.gateway.sign_calls == 0
    state = service.client.get("/api/state").json()
    assert state["accounts"][0]["status"] == "expired"
    assert state["defaultAccountId"] is None
    assert service.app.state.vault.load(token)["accounts"][0]["_cookies"] == []


@pytest.mark.parametrize("status", ["reported_success", "unknown", "outcome_unknown", "pending"])
def test_new_request_key_cannot_repeat_success_or_unresolved_activity(service, status):
    account_id, _ = login(service)
    service.gateway.sign_status = status
    first = sign(service, account_id)
    assert first.status_code == 200
    second = sign(service, account_id, key="different-request-key-2")
    assert second.status_code == 409
    assert second.json()["error"]["code"] == "activity_already_attempted"
    assert service.gateway.sign_calls == 1


def test_validation_challenge_allows_new_explicit_submission(service):
    account_id, _ = login(service)
    service.gateway.sign_status = "requires_validation"
    first = sign(service, account_id)
    assert first.json()["status"] == "requires_validation"
    service.gateway.sign_status = "reported_success"
    second = sign(service, account_id, key="request-with-validation-2", payload={**SIGN_PAYLOAD, "validate": "supplied-official-token"})
    assert second.status_code == 200
    assert second.json()["status"] == "reported_success"
    assert service.gateway.sign_calls == 2


def test_upstream_timeout_creates_unknown_receipt_without_retry(service):
    account_id, _ = login(service)
    service.gateway.sign_error = UpstreamError("timeout_unknown", "提交结果未知", details={"submitted": True, "phase": "submit"})
    first = sign(service, account_id)
    assert first.status_code == 200
    assert first.json()["status"] == "outcome_unknown"
    assert first.json()["submitted"] is True
    second = sign(service, account_id, key="timeout-new-key-2")
    assert second.status_code == 409
    assert service.gateway.sign_calls == 1


def test_session_expiry_detected_after_submit_keeps_outcome_unknown(service):
    account_id, _ = login(service)
    service.gateway.sign_error = UpstreamError("session_expired", "学习通要求重新登录，签到结果未确认。", status_code=401,
        details={"submitted": True, "phase": "submit"})
    first = sign(service, account_id)
    assert first.status_code == 200
    assert first.json()["status"] == "outcome_unknown"
    assert service.client.get("/api/state").json()["accounts"][0]["status"] == "expired"
    mutate(service, "POST", f"/api/accounts/{account_id}/relogin", {"password": PASSWORD, "grantDays": 7})
    repeated = sign(service, account_id, key="new-key-after-reauthentication")
    assert repeated.status_code == 409
    assert repeated.json()["error"]["code"] == "activity_already_attempted"
    assert service.gateway.sign_calls == 1


def test_renewal_identity_mismatch_cannot_replace_account_session(service):
    account_id, _ = login(service)
    service.gateway.check = lambda cookies: service.gateway.account("18800002222")
    result = mutate(service, "POST", f"/api/accounts/{account_id}/grant", {"days": 30})
    assert result.status_code == 409
    assert result.json()["error"]["code"] == "identity_mismatch"
    token = service.client.cookies.get(COOKIE)
    saved = service.app.state.vault.load(token)["accounts"][0]
    assert saved["_identity"]["uid"] == "1001"
    assert saved["grant"]["days"] is None
    assert saved["grant"]["expiresAt"] is None


def test_legacy_local_grant_expiry_is_migrated_without_discarding_live_session(service):
    import time
    account_id, _ = login(service)
    token = service.client.cookies.get(COOKIE)
    vault = service.app.state.vault
    state = vault.load(token)
    state['accounts'][0]['grant'].update(days=7, expiresAt=iso(time.time() - DAY))
    with vault.connection() as db:
        db.execute('UPDATE vaults SET ciphertext=? WHERE owner=?',
                   (vault.encrypt(state, vault.owner(token)), vault.owner(token)))
    loaded = service.client.get('/api/state')
    assert loaded.status_code == 200
    account = loaded.json()['accounts'][0]
    assert account['status'] == 'active'
    assert account['grant']['days'] is None and account['grant']['expiresAt'] is None
    assert vault.load(token)['accounts'][0]['_cookies']
    assert sign(service, account_id).status_code == 200
    assert service.gateway.sign_calls == 1


def test_workspace_age_no_longer_removes_accounts_or_blocks_restart(service):
    import time
    from vault import Vault
    account_id, _ = login(service)
    token = service.client.cookies.get(COOKIE)
    vault = service.app.state.vault
    with vault.connection() as db:
        db.execute('UPDATE vaults SET expires=? WHERE owner=?', (time.time() - DAY, vault.owner(token)))
    vault.create()
    assert vault.load(token)['accounts'][0]['id'] == account_id
    restarted = Vault(service.settings.data_dir)
    assert restarted.load(token)['accounts'][0]['status'] == 'active'
    with restarted.connection() as db:
        assert db.execute('SELECT expires FROM vaults WHERE owner=?', (vault.owner(token),)).fetchone()[0] == 0


def test_login_and_grant_check_need_no_duration_and_website_cookie_is_renewed(service):
    response = mutate(service, 'POST', '/api/accounts/login', {'identifier': '18800001111', 'password': PASSWORD})
    assert response.status_code == 200
    account_id = response.json()['accountId']
    token = service.client.cookies.get(COOKIE)
    checked = mutate(service, 'POST', f'/api/accounts/{account_id}/grant', {})
    assert checked.status_code == 200
    assert checked.json()['state']['accounts'][0]['grant']['expiresAt'] is None
    refreshed = service.client.get('/api/bootstrap')
    assert refreshed.status_code == 200
    assert service.client.cookies.get(COOKIE) == token
    assert 'max-age=31536000' in refreshed.headers['set-cookie'].lower()
    assert refreshed.json()['accounts'][0]['id'] == account_id
