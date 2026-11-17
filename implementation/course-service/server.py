"""Self-hosted account/session API and explicit, idempotent attendance submission."""
from collections import defaultdict, deque
from contextlib import asynccontextmanager
from dataclasses import dataclass
import asyncio
import hashlib
import json
import logging
import os
from pathlib import Path
import re
import secrets
import threading
import time
from typing import Literal
from urllib.parse import urlsplit

from fastapi import FastAPI, Query, Request, Response
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, ConfigDict, Field, SecretStr, model_validator

from upstream import UpstreamGateway, UpstreamError
from vault import Vault, DAY, event, iso, public_state, repair, timestamp
from qr_batches import QrBatchManager, receipt
from qr_diagnostics import qr_diagnostics
from course_monitor import CourseMonitor, require_no_cooldown, session_stamp
from sign_history import successful_records, official_snapshot, history_page

LEGACY_COOKIE = 'keqian_session'


def cookie_name(public_origin):
    url = urlsplit(public_origin)
    scheme, host = url.scheme.lower(), (url.hostname or '').lower()
    port = url.port or (443 if scheme == 'https' else 80)
    scope = f'{scheme}://{host}:{port}'
    return LEGACY_COOKIE + '_' + hashlib.sha256(scope.encode()).hexdigest()[:16]


COOKIE = cookie_name('http://127.0.0.1:8000')
BROWSER_SESSION_MAX_AGE = 365 * DAY
ROOT = Path(__file__).resolve().parent
LOGGER = logging.getLogger('course.auth')
QR_LOGGER = logging.getLogger('uvicorn.error.course.qr')


@dataclass
class Settings:
    data_dir: Path = ROOT / 'var'
    public_origin: str = 'http://127.0.0.1:8000'

    @classmethod
    def from_env(cls):
        return cls(Path(os.environ.get('COURSE_DATA_DIR', ROOT / 'var')),
                   os.environ.get('COURSE_PUBLIC_ORIGIN', 'http://127.0.0.1:8000').rstrip('/'))

    def __post_init__(self):
        url = urlsplit(self.public_origin)
        local = url.hostname in ('localhost', '127.0.0.1', '::1')
        if not url.hostname or url.username or url.password or url.path not in ('', '/') or url.query or url.fragment:
            raise ValueError('COURSE_PUBLIC_ORIGIN must be a single origin without a path.')
        if url.scheme != 'https' and not (url.scheme == 'http' and local):
            raise ValueError('Non-loopback public origins require HTTPS.')
        self.public_origin = self.public_origin.rstrip('/')


class ApiError(Exception):
    def __init__(self, code, message, status_code=400):
        self.code, self.message, self.status_code = code, message, status_code


class Input(BaseModel):
    model_config = ConfigDict(extra='forbid')


class Login(Input):
    identifier: str = Field(min_length=1, max_length=64)
    password: SecretStr = Field(min_length=1, max_length=256)
    nickname: str = Field(default='', max_length=20)
    grantDays: Literal[1, 7, 30] | None = None  # Accepted only for older clients.


class Relogin(Input):
    password: SecretStr = Field(min_length=1, max_length=256)
    grantDays: Literal[1, 7, 30] | None = None


class Grant(Input):
    days: Literal[1, 7, 30] | None = None


class Rename(Input):
    name: str = Field(min_length=1, max_length=20)


class Remove(Input):
    ids: list[str] = Field(min_length=1, max_length=100)


class Location(Input):
    latitude: float = Field(ge=-90, le=90, allow_inf_nan=False)
    longitude: float = Field(ge=-180, le=180, allow_inf_nan=False)
    address: str = Field(default='', max_length=512)


class Photo(Input):
    objectId: str = Field(min_length=1, max_length=256)
    photoResult: str = Field(min_length=1, max_length=16000)


class Face(Input):
    currentFaceId: str = Field(default='', max_length=256)
    faceEnc: str = Field(default='', max_length=4096)


class Challenge(Input):
    enc2: str = Field(min_length=1, max_length=4096)
    validation_token: str = Field(alias='validate', min_length=1, max_length=4096)


class Sign(Input):
    kind: Literal['ordinary', 'gesture', 'code', 'position', 'located_code', 'qr', 'photo']
    activityScope: Literal['course', 'group'] = 'course'
    activeId: str = Field(pattern=r'^\d{1,32}$')
    courseId: str | None = Field(default=None, pattern=r'^\d{1,32}$')
    classId: str | None = Field(default=None, pattern=r'^\d{1,32}$')
    deviceCode: str = Field(default='', max_length=256)
    signCode: str | None = Field(default=None, max_length=64)
    enc: str | None = Field(default=None, max_length=4096)
    location: Location | None = None
    photo_payload: Photo | None = None
    face_fields: Face | None = None
    challenge: Challenge | None = None
    validation_token: str = Field(default='', alias='validate', max_length=4096)
    faceEncAid: str = Field(default='', max_length=256)
    ifopenAddress: Literal['0', '1'] = '0'

    @model_validator(mode='after')
    def scope_fields(self):
        if self.activityScope == 'course' and (not self.courseId or not self.classId):
            raise ValueError('课程签到需要课程和班级标识。')
        if self.kind == 'gesture' and (any((self.signCode, self.enc, self.location,
                self.photo_payload, self.face_fields, self.challenge, self.validation_token,
                self.faceEncAid, self.deviceCode)) or self.ifopenAddress != '0'):
            raise ValueError('手势直接入口不接收手势值或其它签到凭据。')
        if self.activityScope == 'group' and (self.kind not in ('ordinary', 'qr', 'gesture') or any((
                self.courseId, self.classId, self.signCode, self.enc, self.location,
                self.photo_payload, self.face_fields, self.challenge, self.validation_token,
                self.faceEncAid, self.deviceCode)) or self.ifopenAddress != '0'):
            raise ValueError('群活动直接入口只接收普通、二维码或手势类型及活动标识。')
        return self


class QrBatch(Input):
    accountIds: list[str] = Field(min_length=1, max_length=100)
    qrText: str = Field(min_length=1, max_length=8192)
    location: Location | None = None


class MonitorSettings(Input):
    enabled: bool
    autoSign: bool = False
    intervalSeconds: int = Field(default=10, ge=10, le=300)


class Limiter:
    def __init__(self):
        self.calls = defaultdict(deque)
        self.lock = threading.Lock()

    def check(self, key, limit, window):
        with self.lock:
            now = time.monotonic()
            for stale in [k for k, calls in self.calls.items() if not calls or calls[-1] <= now - 300]:
                self.calls.pop(stale, None)
            calls = self.calls[key]
            while calls and calls[0] <= now - window:
                calls.popleft()
            if len(calls) >= limit:
                raise ApiError('rate_limited', '操作过于频繁，请稍后再试。', 429)
            calls.append(now)


def mask(value):
    return value[:3] + '****' + value[-4:] if re.fullmatch(r'1\d{10}', value) else value[:2] + '****' + value[-2:]


def create_app(settings=None, gateway=None):
    settings = settings or Settings.from_env()
    vault = Vault(settings.data_dir)
    gateway = gateway or UpstreamGateway()
    session_cookie = cookie_name(settings.public_origin)
    limiter = Limiter()
    detail_cache = {}
    history_cache = {}
    history_cache_lock = threading.Lock()
    batch_manager = QrBatchManager(vault, gateway)
    monitor_manager = CourseMonitor(vault, gateway)

    @asynccontextmanager
    async def lifespan(app):
        monitor_manager.start()
        try:
            yield
        finally:
            monitor_manager.close()
            batch_manager.close()

    app = FastAPI(title='课签账号与签到服务', version='0.8.4', docs_url='/api/docs', redoc_url=None, openapi_url='/api/openapi.json', lifespan=lifespan)
    app.state.vault, app.state.gateway = vault, gateway
    app.state.batch_manager = batch_manager
    app.state.monitor_manager = monitor_manager
    app.state.cookie_name = session_cookie

    @app.exception_handler(ApiError)
    async def api_error(request, exc):
        return JSONResponse({'error': {'code': exc.code, 'message': exc.message}}, status_code=exc.status_code)

    @app.exception_handler(UpstreamError)
    async def upstream_error(request, exc):
        safe = {key: exc.details[key] for key in ('phase', 'transport_reason', 'http_status', 'target_scheme', 'target_domain', 'url_issue') if key in exc.details}
        LOGGER.warning('upstream_failure code=%s phase=%s reason=%s http_status=%s target_scheme=%s target_domain=%s url_issue=%s',
                       exc.code, safe.get('phase', '-'), safe.get('transport_reason', '-'), safe.get('http_status', '-'),
                       safe.get('target_scheme', '-'), safe.get('target_domain', '-'), safe.get('url_issue', '-'))
        return JSONResponse({'error': {'code': exc.code, 'message': str(exc), **safe}}, status_code=exc.status_code)

    @app.exception_handler(RequestValidationError)
    async def validation_error(request, exc):
        # Never serialize Pydantic's input values: they may contain a password.
        return JSONResponse({'error': {'code': 'invalid_input', 'message': '请求字段不符合要求，请检查输入。',
                                       'fields': ['.'.join(str(p) for p in e['loc']) for e in exc.errors()]}}, status_code=422)

    @app.exception_handler(Exception)
    async def unexpected_error(request, exc):
        return JSONResponse({'error': {'code': 'service_error', 'message': '服务未能完成处理，请刷新状态后核实。'}}, status_code=500)

    @app.middleware('http')
    async def protect(request, call_next):
        api = request.url.path.startswith('/api/')
        origin = request.headers.get('origin')
        if api and origin and origin != settings.public_origin:
            return JSONResponse({'error': {'code': 'origin_forbidden', 'message': '请求来源不匹配。'}}, status_code=403)
        if api and request.headers.get('sec-fetch-site') == 'cross-site':
            return JSONResponse({'error': {'code': 'origin_forbidden', 'message': '不接受跨站请求。'}}, status_code=403)
        if api and request.method not in ('GET', 'HEAD', 'OPTIONS'):
            token = request.cookies.get(session_cookie, '')
            supplied = request.headers.get('x-csrf-token', '')
            if not token or not secrets.compare_digest(supplied, vault.csrf(token)):
                return JSONResponse({'error': {'code': 'csrf_failed', 'message': '页面会话已变化，请刷新后重试。'}}, status_code=403)
            length = request.headers.get('content-length', '0')
            if not length.isdecimal() or int(length) > 65536:
                return JSONResponse({'error': {'code': 'body_too_large', 'message': '请求内容过大。'}}, status_code=413)
        response = await call_next(request)
        response.headers['Cache-Control'] = 'no-store' if api else 'no-cache'
        response.headers['X-Content-Type-Options'] = 'nosniff'
        response.headers['Referrer-Policy'] = 'same-origin'
        response.headers['X-Frame-Options'] = 'DENY'
        return response

    def workspace(request):
        token = request.cookies.get(session_cookie, '')
        state = vault.load(token) if token else None
        if state is None:
            raise ApiError('workspace_expired', '网页会话已过期，请刷新页面。', 401)
        return token, state

    def account(state, account_id, active=False):
        row = next((a for a in state['accounts'] if a['id'] == account_id), None)
        if row is None:
            raise ApiError('account_not_found', '未找到当前网页下的这个账号。', 404)
        if active and (row['status'] != 'active' or not row['_cookies']):
            raise ApiError('login_required', '请先重新登录并授权此账号。', 409)
        return row

    def save(token, state):
        vault.save(token, state)
        return {'state': public_state(state)}

    def login_limit(request, token):
        limiter.check(('login', vault.owner(token)), 10, 300)
        limiter.check(('ip-login', request.client.host if request.client else 'unknown'), 30, 300)

    def update_login(state, result, identifier, nickname, existing=None):
        old = existing or next((a for a in state['accounts'] if a['_identity']['uid'] == str(result.uid)), None)
        if existing and existing['_identity']['uid'] != str(result.uid):
            raise ApiError('identity_mismatch', '登录返回的身份与所选账号不一致。', 409)
        if not old and len(state['accounts']) >= 100:
            raise ApiError('account_limit', '当前网页最多保存 100 个账号。', 409)
        now = time.time()
        row = old or {'id': secrets.token_hex(16), 'color': ['green', 'blue', 'orange'][len(state['accounts']) % 3]}
        row.update(name=nickname.strip() or row.get('name') or result.name,
                   maskedIdentifier=mask(identifier), school=result.school or '学校信息未提供', status='active',
                   grant={'days': None, 'grantedAt': iso(now), 'expiresAt': None, 'scopes': ['session', 'profile']},
                   lastLogin=iso(now), sessionExpiresAt=iso(result.session_expires_at) if result.session_expires_at else None,
                   _identity={'uid': str(result.uid), 'fid': str(result.fid), 'name': result.name},
                   _identifier=identifier, _cookies=result.cookies)
        if not old:
            state['accounts'].append(row)
        event(state, row, '登录并授权保存会话（直到失效或撤销）')
        repair(state)
        return row

    @app.get('/api/health')
    def health():
        return {'status': 'ok', 'mode': 'live', 'upstreamVerified': False}

    @app.get('/api/bootstrap')
    def bootstrap(request: Request, response: Response):
        limiter.check(('bootstrap', request.client.host if request.client else 'unknown'), 60, 60)
        scoped_token = request.cookies.get(session_cookie, '')
        token = scoped_token or request.cookies.get(LEGACY_COOKIE, '')
        with vault.lock(token):
            state = vault.load(token) if token else None
            if scoped_token and state is None:
                raise ApiError('workspace_changed', '网页会话标识已失效，原账号数据没有被删除，请恢复原账号空间。', 409)
            if state is None:
                token, state = vault.create()
            # Refresh only the website's persistent access cookie. This never
            # changes the validity of Chaoxing's own login cookies.
            response.set_cookie(session_cookie, token, max_age=BROWSER_SESSION_MAX_AGE, httponly=True,
                                secure=settings.public_origin.startswith('https:'), samesite='strict', path='/')
            vault.save(token, state)
            return {**public_state(state), 'csrfToken': vault.csrf(token)}

    @app.get('/api/state')
    def get_state(request: Request):
        token = request.cookies.get(session_cookie, '')
        with vault.lock(token):
            token, state = workspace(request)
            vault.save(token, state)
            return public_state(state)

    @app.post('/api/accounts/login')
    def login(body: Login, request: Request):
        token = request.cookies.get(session_cookie, '')
        with vault.lock(token):
            token, state = workspace(request)
            login_limit(request, token)
            identifier = body.identifier.strip()
            if not identifier:
                raise ApiError('invalid_input', '请填写学习通账号。', 422)
            result = gateway.login(identifier, body.password.get_secret_value())
            row = update_login(state, result, identifier, body.nickname)
            return {'accountId': row['id'], **save(token, state)}

    @app.post('/api/accounts/{account_id}/relogin')
    def relogin(account_id: str, body: Relogin, request: Request):
        token = request.cookies.get(session_cookie, '')
        with vault.lock(token):
            token, state = workspace(request)
            row = account(state, account_id)
            login_limit(request, token)
            result = gateway.login(row['_identifier'], body.password.get_secret_value())
            update_login(state, result, row['_identifier'], row['name'], row)
            return {'accountId': row['id'], **save(token, state)}

    @app.get('/api/accounts/{account_id}/details')
    def account_details(account_id: str, request: Request, refresh: bool = False):
        token = request.cookies.get(session_cookie, '')

        def stamp(row):
            return (row['_identity']['uid'], row['grant']['grantedAt'] if row['grant'] else None, row['lastLogin'])

        def display_account(state):
            return next(a for a in public_state(state)['accounts'] if a['id'] == account_id)

        with vault.lock(token):
            token, state = workspace(request)
            row = account(state, account_id, active=True)
            version = stamp(row)
            cache_key = (vault.owner(token), account_id, version)
            now = time.monotonic()
            for key in [key for key, entry in detail_cache.items() if now - entry[0] >= 30]:
                detail_cache.pop(key, None)
            cached = detail_cache.get(cache_key)
            if cached and not refresh:
                result = json.loads(json.dumps(cached[1]))
                result['account'] = display_account(state)
                return result
            limiter.check(('details', vault.owner(token), account_id), 10, 60)
            cookies, identity = row['_cookies'], dict(row['_identity'])
        try:
            upstream = gateway.get_account_details(cookies, identity)
        except UpstreamError as error:
            if error.code in ('session_expired', 'session_missing', 'login_required', 'invalid_session'):
                with vault.lock(token):
                    fresh = vault.load(token)
                    current = next((a for a in fresh['accounts'] if a['id'] == account_id), None) if fresh else None
                    if current and stamp(current) == version:
                        current.update(status='expired', _cookies=[], sessionExpiresAt=iso())
                        vault.save(token, fresh)
            raise
        with vault.lock(token):
            token, state = workspace(request)
            current = account(state, account_id, active=True)
            if stamp(current) != version:
                raise ApiError('account_changed', '账号登录或授权已更新，请刷新详情。', 409)
            if not isinstance(upstream, dict) or not isinstance(upstream.get('profile'), dict) or not isinstance(upstream.get('courses'), list) or not isinstance(upstream.get('sources'), list):
                raise ApiError('invalid_upstream_details', '学习通详情返回内容无法识别，请稍后刷新。', 502)
            profile_fields = ('name', 'school', 'studentNumberMasked', 'department', 'major', 'grade', 'avatarUrl')
            course_fields = ('key', 'courseId', 'classId', 'name', 'teacher', 'className', 'term', 'coverUrl', 'status', 'statusCode', 'sourceLabels')
            source_fields = ('id', 'label', 'status', 'message', 'count')
            profile = {key: upstream['profile'][key] for key in profile_fields if key in upstream['profile']}
            courses = [{key: row[key] for key in course_fields if key in row} for row in upstream['courses'] if isinstance(row, dict)]
            sources = [{key: row[key] for key in source_fields if key in row} for row in upstream['sources'] if isinstance(row, dict)]
            if profile.get('school'):
                current['school'] = profile['school']
            if upstream.get('cookies') is not None:
                current['_cookies'] = upstream['cookies']
            vault.save(token, state)
            result = {'account': display_account(state), 'profile': profile, 'courses': courses,
                      'sources': sources, 'fetchedAt': upstream.get('fetchedAt') or iso()}
            # Cache usable reads briefly; refresh explicitly bypasses the cache.
            # Never reuse details across login or consent changes.
            if any(source.get('status') == 'ok' for source in sources):
                detail_cache[cache_key] = (time.monotonic(), json.loads(json.dumps(result)))
            return result

    @app.post('/api/accounts/{account_id}/grant')
    def grant(account_id: str, body: Grant, request: Request):
        token = request.cookies.get(session_cookie, '')
        with vault.lock(token):
            token, state = workspace(request)
            row = account(state, account_id, active=True)
            try:
                result = gateway.check(row['_cookies'])
            except UpstreamError as exc:
                if exc.code in ('session_expired', 'session_missing', 'invalid_session', 'login_required'):
                    row.update(status='expired', _cookies=[], sessionExpiresAt=iso())
                    vault.save(token, state)
                raise
            if str(result.uid) != row['_identity']['uid']:
                raise ApiError('identity_mismatch', '会话身份不匹配，请重新登录。', 409)
            row['_cookies'] = result.cookies
            row['grant'] = {'days': None, 'grantedAt': iso(), 'expiresAt': None, 'scopes': ['session', 'profile']}
            row['sessionExpiresAt'] = iso(result.session_expires_at) if result.session_expires_at else None
            event(state, row, '核验并保留会话（直到失效或撤销）')
            return save(token, state)

    @app.post('/api/accounts/{account_id}/revoke')
    def revoke(account_id: str, request: Request):
        token = request.cookies.get(session_cookie, '')
        with vault.lock(token):
            token, state = workspace(request)
            row = account(state, account_id)
            monitor_manager.configure(token, account_id, False, False, 10)
            row.update(grant=None, _cookies=[], status='unauthorized', sessionExpiresAt=None)
            event(state, row, '撤销会话授权')
            return save(token, state)

    @app.post('/api/accounts/{account_id}/default')
    def make_default(account_id: str, request: Request):
        token = request.cookies.get(session_cookie, '')
        with vault.lock(token):
            token, state = workspace(request)
            row = account(state, account_id, active=True)
            state['defaultAccountId'] = row['id']
            event(state, row, '设为默认账号')
            return save(token, state)

    @app.patch('/api/accounts/{account_id}')
    def rename(account_id: str, body: Rename, request: Request):
        token = request.cookies.get(session_cookie, '')
        with vault.lock(token):
            token, state = workspace(request)
            row = account(state, account_id)
            if not body.name.strip():
                raise ApiError('invalid_input', '请填写账号备注。', 422)
            row['name'] = body.name.strip()
            event(state, row, '修改账号备注')
            return save(token, state)

    @app.post('/api/accounts/remove')
    def remove(body: Remove, request: Request):
        token = request.cookies.get(session_cookie, '')
        with vault.lock(token):
            token, state = workspace(request)
            rows = [account(state, i) for i in set(body.ids)]
            for row in rows:
                event(state, row, '移除账号')
            state['accounts'] = [a for a in state['accounts'] if a['id'] not in body.ids]
            monitor_manager.remove(token, body.ids)
            vault.remove_jobs(token, body.ids)
            return save(token, state)

    @app.post('/api/accounts/{account_id}/sign')
    def sign(account_id: str, body: Sign, request: Request):
        token = request.cookies.get(session_cookie, '')
        request_id = request.headers.get('idempotency-key', '')
        if not re.fullmatch(r'[A-Za-z0-9_-]{8,128}', request_id):
            raise ApiError('idempotency_required', '需要 8–128 位字母、数字、横线组成的 Idempotency-Key。', 422)
        payload = body.model_dump(exclude_none=True, by_alias=True)
        if body.activityScope == 'course':
            # Preserve existing course fingerprints and saved request replay.
            payload.pop('activityScope', None)
        fingerprint = hashlib.sha256(json.dumps({'accountId': account_id, **payload}, sort_keys=True).encode()).hexdigest()
        with vault.lock(token):
            token, state = workspace(request)
            row = account(state, account_id)
            previous, old_hash = vault.get_job(token, request_id)
            if previous:
                if old_hash != fingerprint:
                    raise ApiError('idempotency_conflict', '同一请求标识不能用于不同签到内容。', 409)
                return previous
            account(state, account_id, active=True)
            prior = vault.prior_job(token, account_id, body.activeId, body.activityScope)
            if prior:
                raise ApiError('activity_already_attempted', '此活动已有成功或结果未明的请求，请查询原请求并在学习通核实，系统不会重复提交。', 409)
            if body.activityScope == 'course':
                require_no_cooldown(vault.load_monitor(token)['accounts'].get(account_id, {}))
            owned_course = None
            if body.activityScope == 'course' and body.kind in ('ordinary', 'gesture', 'photo'):
                try:
                    enrolled = account_details(account_id, request)
                except UpstreamError as exc:
                    if exc.code == 'upstream_rate_limited':
                        monitor_manager.defer(token, account_id, exc.details.get('retryAfterSeconds', 300))
                    raise
                if not any(source.get('status') == 'ok' for source in enrolled['sources']):
                    raise ApiError('courses_unavailable', '尚未核验本人课程来源，未登记签到。', 409)
                owned_course = next((course for course in enrolled['courses']
                    if str(course.get('courseId')) == body.courseId and str(course.get('classId')) == body.classId), None)
                if owned_course is None:
                    raise ApiError('course_not_owned', '课程与班级不在当前账号的已核验课程列表中，未登记签到。', 403)
                if '教务' in str(owned_course.get('name', '')):
                    payload['_candidateName'] = owned_course['name']
                token, state = workspace(request)
                row = account(state, account_id, active=True)
            limiter.check(('sign', vault.owner(token)), 20, 60)
            job = {'requestId': request_id, 'accountId': account_id, 'activeId': body.activeId,
                   'kind': body.kind, 'courseName': (owned_course or {}).get('name', ''),
                   'status': 'pending', 'message': '请求已登记；未获最终回执前请勿重试。', 'createdAt': iso()}
            if body.activityScope == 'group':
                job['activityScope'] = 'group'
            vault.save_job(token, job, fingerprint)
            try:
                result = gateway.sign(row['_cookies'], row['_identity'], payload)
                # Explicit allowlist excludes upstream Cookie, raw HTML and identity fields.
                job.update(status=result['status'], message=result['message'], completedAt=iso())
                for field in ('phase', 'submitted', 'httpStatus', 'reasonCode', 'diagnostics'):
                    if field in result:
                        job[field] = result[field]
                if body.activityScope == 'group' and result.get('resolvedKind') == 'photo':
                    job['resolvedKind'] = 'photo'
                if result.get('challenge'):
                    job['challenge'] = result['challenge']
                if result.get('_diagnostic'):
                    vault.save_diagnostic(token, request_id, result['_diagnostic'])
                if result.get('cookies') is not None:
                    row['_cookies'] = result['cookies']
                if job['status'] in ('login_required', 'session_expired'):
                    row.update(status='expired', _cookies=[], sessionExpiresAt=iso())
            except UpstreamError as exc:
                if body.activityScope == 'group' and exc.details.get('resolvedKind') == 'photo':
                    job['resolvedKind'] = 'photo'
                job.update(status='outcome_unknown' if exc.details.get('submitted') or 'unknown' in exc.code or 'timeout' in exc.code else 'rejected',
                           message=str(exc), completedAt=iso(), submitted=bool(exc.details.get('submitted', False)),
                           phase=exc.details.get('phase', 'presign'))
                if exc.code in ('login_required', 'session_expired'):
                    row.update(status='expired', _cookies=[], sessionExpiresAt=iso())
                if exc.code == 'upstream_rate_limited':
                    monitor_manager.defer(token, account_id, exc.details.get('retryAfterSeconds', 300))
            except Exception:
                # A process/transport failure can happen after upstream accepted a request.
                job.update(status='outcome_unknown', message='未能确认上游处理结果，请在学习通核实；系统未自动重试。', completedAt=iso())
            vault.save_job(token, job, fingerprint)
            vault.save(token, state)
            return job

    @app.get('/api/accounts/{account_id}/sign-records')
    def sign_records(account_id: str, request: Request, offset: int = Query(0, ge=0),
                     limit: int = Query(50, ge=1, le=100), source: Literal['chaoxing', 'local'] = 'chaoxing',
                     refresh: bool = False, snapshot: str = Query('', max_length=64)):
        token = request.cookies.get(session_cookie, '')
        with vault.lock(token):
            token, state = workspace(request)
            row = account(state, account_id, active=source == 'chaoxing')
            if source == 'local':
                return successful_records(vault, token, account_id, offset=offset, limit=limit)
            version = session_stamp(row)
            key = (vault.owner(token), account_id, version)
            with history_cache_lock:
                now = time.monotonic()
                for expired in [k for k, entry in history_cache.items() if now - entry[0] >= 300]:
                    history_cache.pop(expired, None)
                cached = history_cache.get(key)
            if offset and (not cached or snapshot != cached[1]['snapshot'] or refresh):
                raise ApiError('history_expired', '历史查询已更新或过期，请刷新记录后重新查看。', 409)
            if cached and not refresh:
                return history_page(cached[1], offset, limit)
            require_no_cooldown(vault.load_monitor(token)['accounts'].get(account_id, {}))
            limiter.check(('history', vault.owner(token), account_id), 6, 60)
            cookies, identity = row['_cookies'], dict(row['_identity'])
        try:
            upstream = gateway.get_sign_history(cookies, identity, account_id)
            result = official_snapshot(upstream, account_id)
        except UpstreamError as error:
            if error.code == 'upstream_rate_limited':
                monitor_manager.defer(token, account_id, error.details.get('retryAfterSeconds', 300))
            if error.code in ('session_expired', 'session_missing', 'login_required', 'invalid_session'):
                with vault.lock(token):
                    fresh = vault.load(token)
                    current = next((a for a in fresh['accounts'] if a['id'] == account_id), None) if fresh else None
                    if current and session_stamp(current) == version and current['_cookies'] == cookies:
                        current.update(status='expired', _cookies=[], sessionExpiresAt=iso())
                        vault.save(token, fresh)
            raise
        with vault.lock(token):
            token, state = workspace(request)
            current = account(state, account_id, active=True)
            if session_stamp(current) != version:
                raise ApiError('account_changed', '账号登录或授权已更新，请重新查询历史。', 409)
            if current['_cookies'] == cookies and upstream.get('cookies') is not None:
                current['_cookies'] = upstream['cookies']
                vault.save(token, state)
            result['snapshot'] = secrets.token_hex(16)
            with history_cache_lock:
                history_cache[key] = (time.monotonic(), result)
            return history_page(result, offset, limit)

    @app.get('/api/sign-requests/{request_id}')
    def sign_result(request_id: str, request: Request):
        token, _ = workspace(request)
        job, _ = vault.get_job(token, request_id)
        if not job:
            raise ApiError('request_not_found', '未找到当前网页下的这个请求。', 404)
        return job

    @app.post('/api/qr/batches', status_code=202)
    def create_qr_batch(body: QrBatch, request: Request):
        from qr_protocol import parse_qr_target, QrProtocolError
        token = request.cookies.get(session_cookie, '')
        batch_id = request.headers.get('idempotency-key', '')
        if not re.fullmatch(r'[A-Za-z0-9_-]{8,128}', batch_id):
            raise ApiError('idempotency_required', '需要有效的 Idempotency-Key 标识本次扫码。', 422)
        if len(set(body.accountIds)) != len(body.accountIds):
            raise ApiError('invalid_input', '同一账号不能在一个批次内重复选择。', 422)
        payload = body.model_dump(exclude_none=True)
        payload['accountIds'] = sorted(payload['accountIds'])
        fingerprint = hashlib.sha256(json.dumps(payload, sort_keys=True).encode()).hexdigest()
        with vault.lock(token):
            token, state = workspace(request)
            previous, old_hash = vault.get_batch(token, batch_id)
            if previous:
                if old_hash != fingerprint:
                    raise ApiError('idempotency_conflict', '同一扫码请求标识不能用于不同账号或二维码。', 409)
                return receipt(previous)
            rows = [account(state, account_id, active=True) for account_id in body.accountIds]
            try:
                target = parse_qr_target(body.qrText)
            except QrProtocolError as exc:
                QR_LOGGER.info('qr_scan %s', json.dumps({
                    'request': hashlib.sha256(batch_id.encode()).hexdigest()[:12],
                    'accountCount': len(rows), 'outcome': 'rejected_before_upstream',
                    'code': exc.code, 'reason': str(exc), 'qr': qr_diagnostics(body.qrText),
                }, ensure_ascii=True))
                raise ApiError('invalid_qr', str(exc), 422) from None
            QR_LOGGER.info('qr_scan %s', json.dumps({
                'request': hashlib.sha256(batch_id.encode()).hexdigest()[:12],
                'accountCount': len(rows), 'outcome': 'parsed', 'qr': qr_diagnostics(body.qrText),
            }, ensure_ascii=True))
            if any(b['status'] in ('queued', 'running') for b in vault.batches(token, 100)):
                raise ApiError('batch_running', '当前已有批次正在执行，请先查看该批次结果。', 409)
            limiter.check(('qr-batch', vault.owner(token)), 5, 60)
            batch = {'batchId': batch_id, 'activeId': str(target['activeId']),
                     'activityScope': 'group' if target.get('source') == '1' else 'course',
                     'status': 'queued', 'createdAt': iso(),
                     'items': [{'accountId': row['id'], 'name': row['name'], 'status': 'queued', 'message': '等待处理'} for row in rows]}
            vault.save_batch(token, batch, fingerprint)
            batch_manager.start(token, batch, body.qrText, body.location.model_dump() if body.location else None)
            return receipt(batch)

    @app.get('/api/qr/batches')
    def recent_qr_batches(request: Request, limit: int = Query(5, ge=1, le=20)):
        token = request.cookies.get(session_cookie, '')
        with vault.lock(token):
            token, _ = workspace(request)
            return {'batches': [receipt(batch) for batch in vault.batches(token, limit)]}

    @app.get('/api/qr/batches/{batch_id}')
    def qr_batch_result(batch_id: str, request: Request):
        token = request.cookies.get(session_cookie, '')
        with vault.lock(token):
            token, _ = workspace(request)
            batch, _ = vault.get_batch(token, batch_id)
            if batch is None:
                raise ApiError('batch_not_found', '未找到当前网页下的这个扫码批次。', 404)
            return receipt(batch)

    @app.get('/api/monitor')
    def monitor_state(request: Request):
        token = request.cookies.get(session_cookie, '')
        with vault.lock(token):
            token, state = workspace(request)
            return monitor_manager.snapshot(token, state)

    @app.get('/api/monitor/events', response_class=StreamingResponse)
    async def monitor_events(request: Request):
        token, _ = workspace(request)

        def snapshot():
            with vault.lock(token):
                state = vault.load(token)
                if state is None:
                    return None
                return vault.monitor_revision(token), monitor_manager.snapshot(token, state)

        async def events():
            revision, heartbeat = -1, time.monotonic()
            yield 'retry: 3000\n\n'
            while not monitor_manager.closing.is_set() and not await request.is_disconnected():
                if revision != vault.monitor_revision(token):
                    value = await asyncio.to_thread(snapshot)
                    if value is None:
                        return
                    revision, payload = value
                    yield 'event: monitor\ndata: ' + json.dumps(payload, ensure_ascii=False, separators=(',', ':')) + '\n\n'
                    heartbeat = time.monotonic()
                elif time.monotonic() - heartbeat >= 15:
                    yield ': heartbeat\n\n'
                    heartbeat = time.monotonic()
                # Only an in-memory revision is checked here; no upstream polling.
                await asyncio.sleep(0.2)

        return StreamingResponse(events(), media_type='text/event-stream',
            headers={'Cache-Control': 'no-store', 'X-Accel-Buffering': 'no'})

    @app.get('/api/accounts/{account_id}/group-activities')
    def group_activities(account_id: str, request: Request):
        token = request.cookies.get(session_cookie, '')
        with vault.lock(token):
            token, state = workspace(request)
            row = account(state, account_id, active=True)
            cookies, identity = row['_cookies'], dict(row['_identity'])
            stamp = (row['lastLogin'], row['grant']['grantedAt'])
        result = gateway.get_group_activities(cookies, identity)
        with vault.lock(token):
            _, state = workspace(request)
            row = account(state, account_id, active=True)
            if not row.get('grant') or (row['lastLogin'], row['grant']['grantedAt']) != stamp:
                raise ApiError('session_changed', '账号会话已变化，请重新读取群活动。', 409)
            if row['_cookies'] == cookies:
                row['_cookies'] = result.get('cookies', cookies)
                vault.save(token, state)
        return {key: result[key] for key in ('activities', 'status', 'message', 'sourceCoverage', 'groupCount')}

    @app.post('/api/monitor/tasks/{task_id}/sign', status_code=202)
    def sign_monitor_task(task_id: str, request: Request):
        token = request.cookies.get(session_cookie, '')
        with vault.lock(token):
            token, _ = workspace(request)
            limiter.check(('ordinary-task-sign', vault.owner(token)), 20, 60)
        return monitor_manager.sign_now(token, task_id)

    @app.put('/api/accounts/{account_id}/monitor')
    def configure_monitor(account_id: str, body: MonitorSettings, request: Request):
        token = request.cookies.get(session_cookie, '')
        with vault.lock(token):
            token, state = workspace(request)
            row = account(state, account_id, active=body.enabled)
            limiter.check(('monitor-settings', vault.owner(token)), 30, 60)
            monitor_manager.configure(token, account_id, body.enabled, body.autoSign, body.intervalSeconds)
            event(state, row, '开启课程与群聊监控及普通自动签到' if body.enabled and body.autoSign else
                  '开启课程与群聊签到监控' if body.enabled else '停止签到监控')
            vault.save(token, state)
            return monitor_manager.snapshot(token, state)

    @app.post('/api/accounts/{account_id}/monitor/check', status_code=202)
    def check_monitor(account_id: str, request: Request):
        token = request.cookies.get(session_cookie, '')
        with vault.lock(token):
            token, state = workspace(request)
            account(state, account_id, active=True)
            limiter.check(('monitor-check', vault.owner(token), account_id), 3, 30)
            if not monitor_manager.check_soon(token, account_id):
                raise ApiError('monitor_disabled', '请先开启此账号的课程监控。', 409)
            return monitor_manager.snapshot(token, state)

    @app.api_route('/api/{unmatched:path}', methods=['GET', 'POST', 'PUT', 'PATCH', 'DELETE'], include_in_schema=False)
    def missing_api(unmatched: str):
        raise ApiError('not_found', '接口不存在。', 404)

    app.mount('/', StaticFiles(directory=ROOT.parent / 'course-web' / 'dist', html=True), name='frontend')
    return app
