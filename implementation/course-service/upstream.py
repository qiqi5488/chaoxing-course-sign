"""Official-host HTTP adapter. No network I/O occurs on import.

Successful responses are kept separate from a verified attendance record. The
adapter does not solve challenges, manufacture evidence, or retry submissions.
"""
from dataclasses import dataclass, field
from html.parser import HTMLParser
from http.cookiejar import Cookie
from pathlib import Path
import re
import sys
from urllib.parse import urljoin, urlsplit
from datetime import datetime, timezone
import threading
import time
from copy import deepcopy

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "chaoxing-direct"))
from chaoxing_direct import ChaoxingDirectClient, DirectClientError, OutcomeUnknown, PROFILE_ENDPOINT, OFFICIAL_DOMAINS
import sign_protocol as protocol
from qr_protocol import (QrProtocolError, parse_qr_target as _parse_qr_target,
                         activity_context, page_context, merge_context, group_sign_details, group_pc_details,
                         group_gesture_ready)
from group_routes import group_redirect
from group_photo_protocol import (GROUP_PHOTO_USER_AGENT, photo_redirect, photo_form_ready,
                                  parse_photo_members, photo_record)
from course_photo_protocol import validate_photo_payload
from course_protocol import CoursePayloadError, display_profile, parse_normal, parse_meet, merge_courses
from activity_protocol import (ActivityPayloadError, parse_list as parse_activity_list,
                               merge_lists, parse_detail as parse_activity_detail, unknown_activity, milliseconds,
                               number as activity_number)
from ordinary_protocol import OrdinaryProtocolError, ordinary_validation_request, ordinary_validation_required
from group_notifications import GroupNotificationError, fetch_group_notifications, fetch_im_credentials
from official_history import fetch_course_history, parse_course_history


DEFAULT_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
)


class UpstreamError(Exception):
    def __init__(self, code, message, status_code=502, details=None):
        self.code = code
        self.message = message
        self.status_code = status_code
        self.details = details or {}
        super().__init__(message)


def parse_qr_target(qr_text):
    try:
        return _parse_qr_target(qr_text)
    except QrProtocolError as error:
        raise UpstreamError(error.code, str(error), 422) from None


@dataclass(frozen=True)
class AuthenticatedAccount:
    uid: str
    fid: str
    name: str
    school: str
    cookies: list[dict] = field(repr=False)
    session_expires_at: int | None = None
    puid: str = ''


def _official_domain(domain):
    host = domain.lstrip(".").lower()
    # Official common-util.js HOST_SUFFIXES declares these three service domains.
    return bool(host) and any(host == suffix or host.endswith('.' + suffix) for suffix in OFFICIAL_DOMAINS)


def dump_cookies(jar):
    """Preserve cookie scope, expiration and flags; never flatten to a header."""
    return [dict(name=c.name, value=c.value, domain=c.domain, path=c.path,
                 secure=c.secure, expires=c.expires, rest=dict(c._rest),
                 domain_specified=c.domain_specified,
                 domain_initial_dot=c.domain_initial_dot,
                 path_specified=c.path_specified, discard=c.discard,
                 version=c.version, port=c.port, port_specified=c.port_specified,
                 comment=c.comment, comment_url=c.comment_url, rfc2109=c.rfc2109)
            for c in jar if _official_domain(c.domain) and not c.is_expired()]


def load_cookies(jar, rows):
    if not isinstance(rows, list) or len(rows) > 300:
        raise UpstreamError("invalid_session", "保存的会话格式无效，请重新登录。", 401)
    try:
        for row in rows:
            domain = row["domain"]
            name, value, path = row["name"], row["value"], row["path"]
            if (not isinstance(domain, str) or not _official_domain(domain)
                    or any(c in domain for c in "/:@ \r\n")
                    or not isinstance(name, str) or not name or re.search(r"[\s;=]", name)
                    or not isinstance(value, str) or re.search(r"[\r\n]", value)
                    or not isinstance(path, str) or not path.startswith("/")
                    or not isinstance(row.get("rest", {}), dict)):
                raise ValueError()
            expires = row.get("expires")
            if expires is not None and (isinstance(expires, bool) or not isinstance(expires, int)):
                raise ValueError()
            jar.set_cookie(Cookie(
                version=int(row.get("version", 0)), name=name, value=value,
                port=row.get("port"), port_specified=bool(row.get("port_specified", False)),
                domain=domain, domain_specified=bool(row.get("domain_specified", True)),
                domain_initial_dot=bool(row.get("domain_initial_dot", domain.startswith("."))),
                path=path, path_specified=bool(row.get("path_specified", True)),
                secure=bool(row.get("secure", True)), expires=expires,
                discard=bool(row.get("discard", expires is None)),
                comment=row.get("comment"), comment_url=row.get("comment_url"),
                rest=dict(row.get("rest", {})), rfc2109=bool(row.get("rfc2109", False))))
        jar.clear_expired_cookies()
    except (KeyError, TypeError, ValueError, OverflowError):
        raise UpstreamError("invalid_session", "保存的会话格式无效，请重新登录。", 401) from None


def _numeric_id(value, *, allow_unassigned=False):
    if type(value) not in (str, int):
        return None
    value = str(value)
    if allow_unassigned and value == "-1":
        return value
    if not re.fullmatch(r"\d{1,30}", value):
        return None
    if not allow_unassigned and int(value) <= 0:
        return None
    return value


def parse_profile(payload):
    """Accept the observed msg object, not arbitrary JSON or an HTTP 200 alone."""
    if not isinstance(payload, dict):
        raise UpstreamError("profile_unrecognized", "学习通返回的资料格式尚未识别。")
    for key in ("result", "status"):
        if key in payload and payload[key] in (False, 0, "0", "false"):
            raise UpstreamError("session_expired", "学习通登录会话已失效，请重新登录。", 401)
    record = payload.get("msg")
    if not isinstance(record, dict):
        raise UpstreamError("profile_unrecognized", "未获得可核验的学习通账号资料，请重新登录。", 401)
    # The sign consumer reads msg.uid; course discovery uses puid as another ID.
    uid = _numeric_id(record.get("uid")) or _numeric_id(record.get("puid"))
    fid = _numeric_id(record.get("fid"), allow_unassigned=True)
    name = record.get("name")
    if uid is None or fid is None or not isinstance(name, str) or not name.strip() or len(name) > 200:
        raise UpstreamError("profile_unrecognized", "账号资料缺少可核验的用户、机构或姓名字段。")
    return dict(uid=uid, fid=fid, name=name.strip(), school="")


class _PageText(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.parts, self.inputs = [], []
        self.hidden = 0

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if tag in ("script", "style"):
            self.hidden += 1
        if tag == "input":
            self.inputs.append(attrs)

    def handle_endtag(self, tag):
        if tag in ("script", "style"):
            self.hidden = max(0, self.hidden - 1)

    def handle_data(self, data):
        if not self.hidden:
            self.parts.append(data)


def _session_page(response):
    host = (urlsplit(response.url).hostname or "").lower()
    if host in {"passport2.chaoxing.com", "passport.chaoxing.com"}:
        return True
    if response.status_code in (301, 302, 303, 307, 308) and response.headers.get('Location'):
        try:
            target = urlsplit(urljoin(response.url, response.headers['Location']))
            if target.hostname in {'passport2.chaoxing.com', 'passport.chaoxing.com'}:
                return True
        except ValueError:
            pass
    parsed = _PageText()
    parsed.feed(response.text[:2_000_000])
    text = " ".join(parsed.parts)
    password = any(a.get("type", "").lower() == "password" for a in parsed.inputs)
    return password or any(x in text for x in ("请先登录", "登录已失效", "登录状态已失效"))


def _map_transport(error, phase="upstream"):
    details = dict(getattr(error, 'details', {}))
    details['phase'] = phase
    step = {'bootstrap': '打开登录页', 'login_submit': '提交登录',
            'login_navigation': '完成登录跳转', 'profile': '核验账号资料'}.get(phase, '连接学习通')
    reason = details.get('transport_reason')
    reason_text = {'tls': 'TLS 连接失败', 'tls_error': 'TLS 连接失败', 'dns': '域名解析失败',
                   'dns_error': '域名解析失败', 'connect_timeout': '连接超时', 'proxy_error': '代理连接失败',
                   'read_timeout': '等待响应超时', 'timeout': '请求超时', 'connection': '连接中断',
                   'connection_error': '连接中断', 'response_interrupted': '响应传输中断',
                   'response_decoding_error': '响应解码失败'}.get(reason, '网络请求未完成')
    messages = {
        'unsafe_url': '学习通返回了暂不支持的跳转地址，登录尚未完成。',
        'cross_origin_body_redirect': '学习通要求跨站重发登录内容，系统已停止该步骤。',
        'redirect_limit': '学习通登录跳转次数过多，未完成登录。',
        'invalid_json': '学习通返回的内容格式无法识别，未保存账号。',
        'unexpected_login_page': '学习通登录页字段已变化，未提交账号密码。',
        'unexpected_login_mode': '学习通登录加密模式无法识别，未提交账号密码。',
        'invalid_login_response': '学习通登录回执格式无法识别，未保存账号。',
        'http_error': '学习通返回异常 HTTP 状态，登录尚未完成。',
    }
    message = messages.get(error.code, f'{step}时{reason_text}。')
    return UpstreamError(error.code, f'{message}（{step} / {error.code}）', details=details)


class UpstreamGateway:
    def __init__(self, user_agent=DEFAULT_USER_AGENT):
        self.user_agent = user_agent
        self._ended_activities = {}
        self._activity_cache_lock = threading.Lock()

    def _ended_activity(self, identity, course, candidate, result=None):
        key = tuple(str(identity[key]) for key in ('uid', 'fid')) + tuple(str(course[key]) for key in ('courseId', 'classId'))
        key += (candidate['activeId'],) + tuple(str(candidate.get(key, '')) for key in ('startTime', 'endTime', 'status'))
        with self._activity_cache_lock:
            now = time.monotonic()
            for stale in [key for key, entry in self._ended_activities.items() if entry[0] <= now]:
                self._ended_activities.pop(stale, None)
            if result is not None and result['status'] == 'ended':
                self._ended_activities[key] = (now + 300, deepcopy(result))
                while len(self._ended_activities) > 4096:
                    self._ended_activities.pop(next(iter(self._ended_activities)))
            entry = self._ended_activities.get(key)
            return deepcopy(entry[1]) if entry else None

    def _client(self, cookies=None):
        client = ChaoxingDirectClient(self.user_agent)
        if cookies is not None:
            try:
                load_cookies(client.session.cookies, cookies)
                if not list(client.session.cookies):
                    raise UpstreamError("session_expired", "已保存的会话已失效，请重新登录。", 401)
            except Exception:
                client.close()
                raise
        return client

    def _profile(self, client):
        response = client.request("GET", PROFILE_ENDPOINT, headers={"Accept": "application/json"},
                                  follow_redirects=False)
        if response.status_code == 401 or _session_page(response):
            raise UpstreamError("session_expired", "学习通登录会话已失效，请重新登录。", 401)
        if response.status_code == 403:
            raise UpstreamError("upstream_forbidden", "学习通暂未允许读取账号资料，不能确认登录状态。")
        if not 200 <= response.status_code < 300:
            raise UpstreamError("profile_http_error", "学习通账号资料接口返回异常状态。")
        try:
            raw = response.json()
        except ValueError:
            raise UpstreamError("profile_unrecognized", "学习通账号资料未返回已识别的 JSON 格式。") from None
        identity = parse_profile(raw)
        client.account_display_profile = display_profile(raw)
        cookies = dump_cookies(client.session.cookies)
        if not cookies:
            raise UpstreamError("session_missing", "学习通未返回可保存的登录会话。", 401)
        # Only a known identity cookie's concrete expiry is a usable upper bound.
        expirations = [c["expires"] for c in cookies if c["name"] in {"_uid", "UID"}
                       and c["expires"] is not None]
        puid = str(raw.get('msg', {}).get('puid', ''))
        return AuthenticatedAccount(**identity, cookies=cookies, puid=puid if _numeric_id(puid) else '',
                                    session_expires_at=min(expirations) if expirations else None)

    def login(self, identifier, password):
        client = self._client()
        phase = 'login'
        try:
            outcome = client.login(identifier, password)
            if outcome.state != "login_reported_success":
                states = {
                    "challenge_required": ("学习通要求完成验证码，请使用官方验证流程。", 409),
                    "two_factor_required": ("学习通要求二次验证，请使用官方验证流程。", 409),
                    "password_change_required": ("学习通要求修改密码，请在官方页面处理后重试。", 409),
                    "login_rejected": ("学习通未接受这次登录，请核对账号和密码。", 401),
                }
                message, status = states.get(outcome.state, ("学习通登录尚未完成。", 502))
                # A provider action_url can contain session secrets. Do not return it.
                raise UpstreamError(outcome.state, message, status)
            phase = 'profile'
            return self._profile(client)
        except DirectClientError as error:
            raise _map_transport(error, phase if phase == 'profile' else getattr(client, 'last_login_phase', 'login')) from None
        except UpstreamError as error:
            error.details.setdefault('phase', phase if phase == 'profile' else getattr(client, 'last_login_phase', 'login'))
            raise
        finally:
            client.close()

    def check(self, cookies):
        client = self._client(cookies)
        try:
            return self._profile(client)
        except DirectClientError as error:
            raise _map_transport(error, 'profile') from None
        finally:
            client.close()

    def get_account_details(self, cookies, identity):
        """Read-only account and course data; never calls an attendance endpoint."""
        client = self._client(cookies)
        client.timeout = (5, 12)
        try:
            confirmed = self._profile(client)
            if confirmed.uid != str(identity['uid']) or confirmed.fid != str(identity['fid']):
                raise UpstreamError('identity_mismatch', '会话资料与选定账号不一致，未读取课程。', 409)
            profile = getattr(client, 'account_display_profile', {'name': confirmed.name})
            sources, groups = [], []
            targets = (
                ('normal', '普通课程', 'https://mooc1-api.chaoxing.com/mycourse/backclazzdata', {'view': 'json', 'rss': '1'}, parse_normal),
                ('meet', '泛雅课堂', 'https://k.chaoxing.com/pc/meet/index_mymeet_v2', {'offsetUpdateTime': '', 'pageSize': '999', 'page': '1'}, parse_meet),
            )
            for source_id, label, url, params, parser in targets:
                try:
                    response = client.request('GET', url, params=params, headers={'Accept': 'application/json'},
                                              follow_redirects=False)
                    if _session_page(response):
                        raise CoursePayloadError('此课程来源要求重新验证登录状态。')
                    if not 200 <= response.status_code < 300:
                        raise CoursePayloadError('课程来源返回异常 HTTP 状态。')
                    try:
                        data = response.json()
                    except ValueError:
                        raise CoursePayloadError('课程来源未返回可识别的 JSON 数据。') from None
                    rows = parser(data)
                    groups.append(rows)
                    sources.append({'id': source_id, 'label': label, 'status': 'ok', 'count': len(rows),
                                    'message': '已读取' if rows else '当前来源没有课程'})
                except DirectClientError as error:
                    sources.append({'id': source_id, 'label': label, 'status': 'error', 'count': None,
                                    'message': '课程来源连接未完成。', 'code': error.code})
                except CoursePayloadError as error:
                    sources.append({'id': source_id, 'label': label, 'status': 'error', 'count': None,
                                    'message': str(error), 'code': 'course_source_unavailable'})
            sources.append({'id': 'mirror', 'label': '镜像课程', 'status': 'unsupported', 'count': None,
                            'message': '该来源的官方请求地址尚未完成核验。'})
            return {'profile': profile, 'courses': merge_courses(groups), 'sources': sources,
                    'fetchedAt': datetime.now(timezone.utc).isoformat().replace('+00:00', 'Z'),
                    'cookies': dump_cookies(client.session.cookies)}
        except DirectClientError as error:
            raise _map_transport(error, 'profile') from None
        finally:
            client.close()

    def get_sign_history(self, cookies, identity, account_id):
        """Read this user's official records; never enter a signing flow."""
        enrolled = self.get_account_details(cookies, identity)
        client = self._client(enrolled['cookies'])
        client.timeout = (5, 12)
        try:
            confirmed = self._profile(client)
            if confirmed.uid != str(identity['uid']) or confirmed.fid != str(identity['fid']):
                raise UpstreamError('identity_mismatch', '会话资料与选定账号不一致，未查询历史记录。', 409)
            result = fetch_course_history(client, confirmed.puid, enrolled['courses'], account_id,
                                          self._activity_json)
            for source in enrolled['sources']:
                if source.get('status') == 'error':
                    result['sources'].append({'activityScope': 'course', 'name': source['label'],
                        'status': 'unavailable', 'count': 0, 'total': None, 'returned': None,
                        'message': '该课程来源未能读取，历史查询尚不完整。'})
            result['sources'].append({'activityScope': 'group', 'name': '群聊历史',
                'status': 'unavailable', 'count': 0, 'total': None, 'returned': None,
                'message': '群聊个人签到记录的查询入口尚未核实，暂不能查询完整历史。'})
            result.update(cookies=dump_cookies(client.session.cookies),
                          fetchedAt=datetime.now(timezone.utc).isoformat().replace('+00:00', 'Z'))
            return result
        except DirectClientError as error:
            raise _map_transport(error, 'history') from None
        finally:
            client.close()

    def _activity_json(self, client, path, params):
        response = client.request('GET', protocol.MOBILELEARN_ORIGIN + path, params=params,
                                  headers={'Accept': 'application/json'}, follow_redirects=False)
        payload = None
        if response.status_code != 429:
            if response.status_code == 401 or _session_page(response):
                raise UpstreamError('session_expired', '学习通登录会话已失效，请重新登录。', 401)
            if not 200 <= response.status_code < 300:
                raise ActivityPayloadError('学习通活动接口返回异常 HTTP 状态。')
            try:
                payload = response.json()
            except ValueError:
                raise ActivityPayloadError('学习通活动接口未返回已识别的 JSON。') from None
        if response.status_code == 429 or (isinstance(payload, dict) and any(
                payload.get(key) == '请勿频繁操作' for key in ('errorMsg', 'msg'))):
            retry_after = response.headers.get('Retry-After', '').strip()
            try:
                seconds = int(retry_after) if re.fullmatch(r'[0-9]+', retry_after) else 300
            except ValueError:
                seconds = 300
            details = {'retryAfterSeconds': seconds}
            if path in ('/v2/apis/active/student/activelist', '/ppt/activeAPI/taskactivelist'):
                details['operation'] = 'course_activity_list'
            raise UpstreamError('upstream_rate_limited', '学习通提示操作过于频繁，请稍后再试。', 429,
                                details)
        return payload

    def get_course_activities(self, cookies, identity, course):
        """Read the two retained course activity sources and verify each candidate.

        Normal-source and detail reads passed limited authenticated acceptance.
        Failure stays visible rather than becoming a successful empty course.
        """
        if not all(_numeric_id(course.get(key)) for key in ('courseId', 'classId')):
            raise UpstreamError('invalid_course', '课程缺少有效的课程或班级标识。', 422)
        course = {**course, 'courseId': str(course['courseId']), 'classId': str(course['classId'])}
        client = self._client(cookies)
        client.timeout = (5, 12)
        try:
            confirmed = self._profile(client)
            if confirmed.uid != str(identity['uid']) or confirmed.fid != str(identity['fid']):
                raise UpstreamError('identity_mismatch', '会话资料与选定账号不一致，未读取活动。', 409)
            sources, groups = [], []
            params = {'fid': '0', 'courseId': course['courseId'], 'classId': course['classId']}
            for source, path in (('normal', '/v2/apis/active/student/activelist'),
                                 ('task', '/ppt/activeAPI/taskactivelist')):
                try:
                    rows = parse_activity_list(self._activity_json(client, path, params), source)
                    groups.append(rows)
                    sources.append({'id': source, 'status': 'ok', 'count': len(rows)})
                except (ActivityPayloadError, DirectClientError):
                    sources.append({'id': source, 'status': 'error', 'count': None,
                                    'message': '此活动来源未能读取，请稍后核实。'})
            if not groups:
                raise UpstreamError('activities_unavailable', '课程活动来源暂不可用，尚未确认当前签到任务。')
            activities, detail_errors = [], 0
            candidates = sorted(merge_lists(groups), key=lambda row: milliseconds(row.get('startTime')) or 0, reverse=True)
            for candidate in candidates:
                cached = self._ended_activity(identity, course, candidate)
                if cached:
                    activities.append(cached)
                    continue
                try:
                    payload = self._activity_json(client, '/v2/apis/active/getPPTActiveInfo', {'activeId': candidate['activeId']})
                    row = parse_activity_detail(payload, candidate, course)
                except (ActivityPayloadError, DirectClientError):
                    row = unknown_activity(candidate, course)
                    detail_errors += 1
                if row is not None:
                    activities.append(row)
                    self._ended_activity(identity, course, candidate, row)
            return {'activities': activities, 'sources': sources, 'detailErrors': detail_errors,
                    'status': 'partial' if detail_errors or any(s['status'] != 'ok' for s in sources) else 'ok',
                    'cookies': dump_cookies(client.session.cookies),
                    'fetchedAt': datetime.now(timezone.utc).isoformat().replace('+00:00', 'Z')}
        except DirectClientError as error:
            raise _map_transport(error, 'activities') from None
        finally:
            client.close()

    def get_course_notification_activities(self, cookies, identity, course, candidates):
        """Verify owned-course notification candidates using only detail reads.

        Course enrollment is checked by the caller. Notification labels never
        establish the actual attendance type, time window or requirements.
        """
        if not isinstance(course, dict) or not all(_numeric_id(course.get(key)) for key in ('courseId', 'classId')):
            raise UpstreamError('invalid_course', '课程缺少有效的课程或班级标识。', 422)
        course = {**course, 'courseId': str(course['courseId']), 'classId': str(course['classId'])}
        if not isinstance(candidates, list) or len(candidates) > 2000:
            raise UpstreamError('invalid_course_notification', '课程通知候选格式或数量无效。', 422)
        normalized = []
        for candidate in candidates:
            if not isinstance(candidate, dict):
                raise UpstreamError('invalid_course_notification', '课程通知候选格式无效。', 422)
            identifiers = {key: _numeric_id(candidate.get(key)) for key in ('activeId', 'courseId', 'classId')}
            if (not all(identifiers.values()) or any(identifiers[key] != course[key] for key in ('courseId', 'classId'))
                    or candidate.get('activityScope') != 'course'
                    or candidate.get('notificationType') != 'course_attendance'):
                raise UpstreamError('invalid_course_notification', '课程通知与已核验的课程或班级不一致。', 422)
            normalized.append({**candidate, **identifiers})
        normalized = merge_lists([normalized])
        client = self._client(cookies)
        client.timeout = (5, 12)
        try:
            confirmed = self._profile(client)
            if confirmed.uid != str(identity['uid']) or confirmed.fid != str(identity['fid']):
                raise UpstreamError('identity_mismatch', '会话资料与选定账号不一致，未读取课程通知详情。', 409)
            activities, detail_errors = [], 0
            for candidate in normalized:
                row = self._ended_activity(identity, course, candidate)
                # Only notification entries have passed this method's stronger
                # binding checks; a normal-list cache entry is insufficient.
                if row is not None and row.get('notificationType') != 'course_attendance':
                    row = None
                cached = row is not None
                if row is None:
                    try:
                        payload = self._activity_json(client, '/v2/apis/active/getPPTActiveInfo',
                                                      {'activeId': candidate['activeId']})
                        record = payload.get('data') if isinstance(payload, dict) else None
                        if not isinstance(record, dict) or not all(any(
                                _numeric_id(record.get(key)) == candidate[field] for key in keys)
                                for keys, field in ((('activeId', 'id'), 'activeId'),
                                                    (('classId', 'clazzid'), 'classId'))):
                            raise ActivityPayloadError('课程通知详情缺少可核验的活动或班级绑定。')
                        row = parse_activity_detail(payload, candidate, course)
                    except (ActivityPayloadError, DirectClientError):
                        row = unknown_activity(candidate, course)
                        detail_errors += 1
                if row is not None:
                    row.update(activityScope='course', notificationType='course_attendance')
                    activities.append(row)
                    if not cached:
                        self._ended_activity(identity, course, candidate, row)
            return {'activities': activities,
                    'sources': [{'id': 'notification', 'status': 'ok', 'count': len(normalized)}],
                    'detailErrors': detail_errors, 'status': 'partial' if detail_errors else 'ok',
                    'cookies': dump_cookies(client.session.cookies),
                    'fetchedAt': datetime.now(timezone.utc).isoformat().replace('+00:00', 'Z')}
        except DirectClientError as error:
            raise _map_transport(error, 'course_notifications') from None
        finally:
            client.close()

    def sign_qr(self, cookies, identity, qr_text, *, location=None):
        target = parse_qr_target(qr_text)
        if target.get('source') == '1':
            return self._sign_group_qr(cookies, identity, target, qr_text.strip())
        client = self._client(cookies)
        active_id, submitted, phase = target['activeId'], False, 'profile'

        def output(status, message, **extra):
            return dict(activeId=active_id, status=status, message=message, phase=phase,
                        submitted=submitted, cookies=dump_cookies(client.session.cookies), **extra)

        def preflight(context):
            url = protocol.build_presign_url(context)
            response = client.request('GET', url, headers=protocol.build_presign_headers(self.user_agent))
            if _session_page(response):
                raise UpstreamError('session_expired', '学习通要求重新登录，未发送签到。', 401)
            if not 200 <= response.status_code < 300:
                raise UpstreamError('presign_failed', '预签到页面未正常返回，未发送签到。')
            return url, response

        try:
            confirmed = self._profile(client)
            if confirmed.uid != str(identity['uid']) or confirmed.fid != str(identity['fid']):
                raise UpstreamError('identity_mismatch', '会话资料与选定账号不一致，未发送签到。', 409)
            phase = 'context'
            context = {key: target[key] for key in ('activeId', 'courseId', 'classId') if key in target}
            context.update({key: identity[key] for key in ('uid', 'fid', 'name')})
            # This fixed official path is visible in the retained QR frontend.
            info = client.request('GET', protocol.MOBILELEARN_ORIGIN + '/v2/apis/active/getPPTActiveInfo',
                                  params={'activeId': active_id}, headers={'Accept': 'application/json'})
            if _session_page(info):
                raise UpstreamError('session_expired', '学习通登录会话已失效，请重新登录。', 401)
            if 200 <= info.status_code < 300:
                try:
                    merge_context(context, activity_context(info.json(), active_id))
                except ValueError as error:
                    if isinstance(error, QrProtocolError):
                        raise
            # The scanned official /widget/sign/e entry can expose its own
            # course/class fields. Never request the supplied URL directly.
            scanned = urlsplit(qr_text.strip())
            if (scanned.scheme in ('http', 'https') and scanned.path == '/widget/sign/e'
                    and not (context.get('courseId') and context.get('classId'))):
                entry = client.request('GET', protocol.MOBILELEARN_ORIGIN + '/widget/sign/e',
                                       params={'id': active_id, 'enc': target['enc'],
                                               **({'c': target['code']} if target.get('code') else {})},
                                       headers=protocol.build_presign_headers(self.user_agent))
                if _session_page(entry):
                    raise UpstreamError('session_expired', '学习通二维码入口要求重新登录。', 401)
                if 200 <= entry.status_code < 300:
                    merge_context(context, page_context(entry.text, active_id))
            phase = 'presign'
            presign_url, pre = preflight(context)
            first = {key: context.get(key, '') for key in ('courseId', 'classId')}
            found = page_context(pre.text, active_id)
            merge_context(context, found)
            if not context.get('courseId') or not context.get('classId') or not found['matchedActivity']:
                return output('need_context', '尚未从学习通返回中确认课程、班级及活动，未发送签到。')
            if any(first[key] != context[key] for key in first):
                presign_url, pre = preflight(context)
                found = page_context(pre.text, active_id)
                merge_context(context, found)
                if not found['matchedActivity']:
                    return output('need_context', '学习通未确认完整的预签到上下文，未发送签到。')
            parsed = _PageText()
            parsed.feed(pre.text[:2_000_000])
            visible = ' '.join(parsed.parts)
            if any(value in visible for value in ('活动已结束', '签到已结束', '活动不存在', '签到活动不存在')):
                return output('rejected', '学习通提示活动已结束或不存在，未发送签到。')
            if re.search(r'(?:pptSign/analysis|analysis2)', pre.text):
                return output('unknown', '预签到页面要求尚未接入的官方预处理，未发送签到。')
            if context.get('openCheckFaceFlag') == '1':
                return output('face_required', '此活动要求本人完成人脸验证，未发送签到。')
            if context.get('ifopenAddress') == '1' and location is None:
                return output('location_required', '此活动要求现场定位，请提供本人现场位置后重新发起。')
            if location is not None:
                try:
                    protocol.build_location_fields(location)
                except (KeyError, TypeError, ValueError, OverflowError):
                    raise UpstreamError('invalid_location', '现场位置参数不完整或无效。', 422) from None
            fields = protocol.build_qr_form(context, target['enc'], location=location)
            phase, submitted = 'submit', True
            response = client.request('POST', protocol.MOBILELEARN_ORIGIN + protocol.SIGN_PATH,
                                      data=protocol.encode_form(fields),
                                      headers=protocol.build_submit_headers(presign_url, self.user_agent))
            if _session_page(response):
                raise UpstreamError('session_expired', '学习通要求重新登录，签到结果未确认。', 401)
            result = protocol.classify_sign_response(response.text, http_status=response.status_code)
            messages = {
                'reported_success': '学习通返回签到成功。', 'already_signed': '学习通返回该账号已签到。',
                'requires_validation': '学习通要求验证码，请完成官方验证后再签到。',
                'device_conflict': '学习通返回设备限制，本账号未完成签到。',
                'invalid_challenge': '学习通验证挑战不完整，未自动重试。',
                'rejected': '学习通拒绝了这次签到。', 'http_error': '学习通返回异常状态，签到结果未确认。',
                'unknown': '学习通回执尚未识别，签到结果未确认，未自动重试。',
            }
            extra = {'httpStatus': response.status_code}
            if result.get('enc2'):
                extra['challenge'] = {'enc2': result['enc2']}
            return output(result['status'], messages[result['status']], **extra)
        except QrProtocolError as error:
            raise UpstreamError(error.code, str(error), 409, {'phase': phase, 'submitted': submitted}) from None
        except UpstreamError as error:
            error.details.update(phase=phase, submitted=submitted)
            raise
        except DirectClientError as error:
            if submitted:
                return output('outcome_unknown', '签到请求已发出但结果未确认；请先核对学习通，勿重复提交。')
            mapped = _map_transport(error, phase)
            mapped.details.update(submitted=False)
            raise mapped from None
        finally:
            client.close()

    def _sign_group_qr(self, cookies, identity, target, qr_text):
        client = self._client(cookies)
        submitted, phase = False, 'profile'
        steps = []

        def output(status, message, **extra):
            return dict(activeId=target['activeId'], status=status, message=message,
                        phase=phase, submitted=submitted,
                        cookies=dump_cookies(client.session.cookies), **extra)

        try:
            confirmed = self._profile(client)
            if confirmed.uid != str(identity['uid']) or confirmed.fid != str(identity['fid']):
                raise UpstreamError('identity_mismatch', '会话资料与选定账号不一致，未发送签到。', 409)
            if not confirmed.puid:
                raise UpstreamError('identity_incomplete', '学习通未返回通行证身份，无法继续群签到，请重新登录。', 409)
            # Native SIGNIN payloads are resolved as a whole; Code is not an
            # ewnCode. Follow only the two identity-preserving observed routes.
            url = protocol.MOBILELEARN_ORIGIN + '/newsign/studentSign'
            params = {'rcode': qr_text, 'uid': confirmed.puid}
            phase, submitted = 'resolve', True
            for stage in ('entry', 'mobile', 'pc'):
                response = client.request('GET', url, params=params,
                    headers={**protocol.build_presign_headers(self.user_agent), 'Referer': protocol.MOBILELEARN_ORIGIN + '/'},
                    follow_redirects=False)
                steps.append({'endpoint': urlsplit(url).path, 'httpStatus': response.status_code,
                              'location': response.headers.get('Location'),
                              'body': response.text[:65536], 'bodyTruncated': len(response.text) > 65536})
                if _session_page(response):
                    raise UpstreamError('session_expired', '学习通要求重新登录，签到结果未确认。', 401)
                if response.status_code not in (301, 302, 303, 307, 308) or not response.headers.get('Location'):
                    break
                try:
                    url = group_redirect(response.headers['Location'], stage, target['activeId'], confirmed.puid)
                except ValueError:
                    return output('unknown', '学习通返回尚未确认的群签到跳转，系统未继续操作。',
                        httpStatus=response.status_code, reasonCode='unsupported_group_redirect',
                        _diagnostic={'endpoint': steps[-1]['endpoint'], 'httpStatus': response.status_code,
                                     'body': response.text[:65536], 'bodyTruncated': len(response.text) > 65536, 'steps': steps})
                params, phase = None, 'presign'
            diagnostics = (group_pc_details(response.text, response.status_code, target['activeId'], confirmed.puid)
                           if stage == 'pc' else group_sign_details(response.text, response.status_code))
            status = diagnostics['status']
            messages = {
                'reported_success': '学习通返回群签到成功页面。',
                'already_signed': '学习通返回该账号已完成群签到。',
                'rejected': '学习通拒绝了这次群签到，请核对活动及二维码是否有效。',
                'device_conflict': '学习通返回设备限制，本账号未确认完成群签到。',
                'face_required': '学习通要求本人完成人脸验证，未自动补交。',
                'photo_required': '此群签到需要照片，请在学习通手机端完成拍照签到。',
                'location_required': '学习通要求本人现场位置，未自动补交。',
                'requires_validation': '学习通要求完成官方验证，未自动重试。',
                'http_error': '学习通返回异常状态，群签到结果未确认，未自动重试。',
                'unknown': '学习通群签到页面尚未识别，请核对记录，未自动重试。',
                'need_context': '群签到还需要完成页面中的确认步骤，当前未继续操作。',
            }
            reasons = {
                'activity_ended': '学习通提示签到活动已结束，请使用正在进行的活动。',
                'activity_missing': '学习通提示无此签到活动，请核对二维码对应的活动。',
                'qr_expired': '学习通提示二维码已过期或失效，请重新扫描当前二维码。',
                'qr_invalid': '学习通提示二维码不正确，请核对当前签到二维码。',
                'sign_failed': '学习通返回“签到失败”，具体页面已保留用于诊断。',
                'conflicting_result': '学习通页面含互相冲突的结果提示，请先核对签到记录；系统未自动重试。',
                'context_mismatch': '学习通返回的活动或账号与所选账号不一致，系统未继续操作。',
                'page_identity_missing': '学习通尚未确认活动和账号身份，系统未继续操作。',
                'record_signed': '学习通群签到记录页显示签到成功。',
            }
            if diagnostics['reasonCode'] in ('group_form_pending', 'photo_required'):
                submitted = False
            private = {} if status in ('reported_success', 'already_signed') else {'_diagnostic': {
                'endpoint': steps[-1]['endpoint'], 'httpStatus': response.status_code,
                'body': response.text[:65536], 'bodyTruncated': len(response.text) > 65536, 'steps': steps}}
            return output(status, reasons.get(diagnostics['reasonCode'], messages[status]),
                          httpStatus=response.status_code, reasonCode=diagnostics['reasonCode'],
                          diagnostics=diagnostics, **private)
        except UpstreamError as error:
            error.details.update(phase=phase, submitted=submitted)
            raise
        except DirectClientError as error:
            if submitted:
                return output('outcome_unknown', '群签到请求已发出但结果未确认，请先核对学习通，勿重复提交。')
            mapped = _map_transport(error, phase)
            mapped.details.update(submitted=False)
            raise mapped from None
        finally:
            client.close()

    @staticmethod
    def _sign_fields(identity, payload):
        if any(key in payload for key in ("uid", "fid", "name", "cookies", "url", "headers")):
            raise UpstreamError("invalid_sign_input", "签到身份只能来自服务器保存的账号。", 422)
        context = {key: payload.get(key, "") for key in
                   ("activeId", "courseId", "classId", "deviceCode", "faceEncAid", "ifopenAddress")}
        for key in ("activeId", "courseId", "classId"):
            if not _numeric_id(context[key]):
                raise UpstreamError("invalid_sign_input", "请提供有效的活动、课程和班级 ID。", 422)
        context.update({key: identity[key] for key in ("uid", "fid", "name")})
        kind, validate = payload.get("kind"), payload.get("validate", "")
        try:
            if kind == "ordinary":
                return context, "GET", protocol.build_ordinary_query(context, validate=validate)
            if kind == "gesture":
                if any(payload.get(key) for key in ('signCode', 'enc', 'location', 'photo_payload',
                        'face_fields', 'challenge', 'validate', 'faceEncAid', 'deviceCode')) or str(payload.get('ifopenAddress', '0')) != '0':
                    raise UpstreamError('invalid_sign_input', '课程手势直接入口只接收活动、课程和班级标识。', 422)
                fields = protocol.build_ordinary_query(context)
                fields.pop('useragent')
                return context, "GET", fields
            if kind == "code":
                if not payload.get("signCode"):
                    raise ValueError()
                if payload.get("location") or str(payload.get("ifopenAddress", "0")) == "1":
                    raise UpstreamError("invalid_sign_input", "带位置的签到码应使用 located_code 类型。", 422)
                return context, "GET", protocol.build_code_query(context, payload["signCode"], validate=validate)
            if kind in ("position", "located_code"):
                location = payload["location"]
                if not isinstance(location.get("address"), str) or not location["address"].strip():
                    raise ValueError()
                if kind == "position":
                    form = protocol.build_position_form(context, location, face_fields=payload.get("face_fields"), validate=validate)
                else:
                    if not payload.get("signCode"):
                        raise ValueError()
                    form = protocol.build_located_code_form(context, payload["signCode"], location,
                        face_fields=payload.get("face_fields"), validate=validate)
                return context, "POST", form
            if kind == "qr":
                if not payload.get("enc"):
                    raise ValueError()
                if str(context.get("ifopenAddress")) == "1" and not payload.get("location"):
                    raise UpstreamError("location_required", "此二维码签到需要现场位置资料。", 422)
                return context, "POST", protocol.build_qr_form(context, payload["enc"],
                    location=payload.get("location"), face_fields=payload.get("face_fields"),
                    challenge=payload.get("challenge"))
            if kind == "photo":
                if any(payload.get(key) for key in ('signCode', 'enc', 'location', 'face_fields',
                        'challenge', 'validate', 'faceEncAid')) or str(payload.get('ifopenAddress', '0')) != '0':
                    raise ValueError()
                photo = payload.get("photo_payload")
                if photo is None:
                    return context, "POST", {}
                validate_photo_payload(photo)
                return context, "POST", protocol.build_photo_form(context, photo, validate=validate)
        except (KeyError, TypeError, ValueError, AttributeError):
            raise UpstreamError("invalid_sign_input", "该签到类型所需参数不完整或格式无效。", 422) from None
        raise UpstreamError("unsupported_sign_kind", "不支持的签到类型。", 422)

    def sign(self, cookies, identity, payload):
        if payload.get('activityScope') == 'group':
            return self.sign_group_activity(cookies, identity, payload.get('activeId'), kind=payload.get('kind'))
        if payload.get('kind') in ('ordinary', 'gesture'):
            return self._sign_course_direct(cookies, identity, payload)
        if payload.get('kind') == 'photo':
            return self._sign_course_photo(cookies, identity, payload)
        context, method, fields = self._sign_fields(identity, payload)
        client = self._client(cookies)
        submitted = False
        try:
            confirmed = self._profile(client)
            if confirmed.uid != str(identity["uid"]) or confirmed.fid != str(identity["fid"]):
                raise UpstreamError("identity_mismatch", "会话资料与选定账号不一致，未发送签到。", 409)
            if payload.get('_autoSign'):
                try:
                    detail = self._activity_json(client, '/v2/apis/active/getPPTActiveInfo',
                                                 {'activeId': context['activeId']})
                    activity = parse_activity_detail(detail, {'activeId': str(context['activeId'])},
                        {'courseId': str(context['courseId']), 'classId': str(context['classId'])})
                except ActivityPayloadError:
                    activity = None
                if not activity or not activity['autoEligible'] or payload.get('kind') != 'ordinary':
                    return dict(status='requires_manual', message='活动规则已变化或未确认可自动普通签到，请在学习通处理。',
                                phase='presign', submitted=False, cookies=dump_cookies(client.session.cookies))
            presign_url = protocol.build_presign_url(context)
            pre = client.request("GET", presign_url,
                                 headers=protocol.build_presign_headers(self.user_agent))
            if _session_page(pre):
                raise UpstreamError("session_expired", "学习通要求重新登录，未发送签到。", 401)
            if not 200 <= pre.status_code < 300:
                raise UpstreamError("presign_failed", "预签到页面未正常返回，未发送签到。")
            # Do not silently skip an additional preflight explicitly required by
            # a runtime page. Its protocol is absent from our retained evidence.
            if re.search(r"(?:pptSign/analysis|analysis2)", pre.text):
                return dict(status="unknown", message="预签到页面要求尚未接入的官方预处理，未发送签到。",
                            phase="presign", submitted=False, cookies=dump_cookies(client.session.cookies))
            parsed = _PageText()
            parsed.feed(pre.text[:2_000_000])
            visible = " ".join(parsed.parts)
            if payload.get('_autoSign') and (any(word in visible for word in ('拍照', '人脸', '定位', '位置签到', '签到码', '手势', '二维码'))
                    or any((item.get('name') or item.get('id')) in ('ifphoto', 'ifopenAddress', 'openCheckFaceFlag', 'otherId')
                           and item.get('value', '') not in ('', '0') for item in parsed.inputs)
                    or re.search(r'''\b(?:ifphoto|ifopenAddress|openCheckFaceFlag|otherId)["']?\s*[:=]\s*["']?[1-9]\d*\b''', pre.text)):
                return dict(status='requires_manual', message='预签到页面要求现场资料或验证，未自动发送签到。',
                            phase='presign', submitted=False, cookies=dump_cookies(client.session.cookies))
            if any(x in visible for x in ("活动已结束", "签到已结束", "活动不存在", "签到活动不存在")):
                return dict(status="rejected", message="学习通提示活动已结束或不存在，未发送签到。",
                            phase="presign", submitted=False, cookies=dump_cookies(client.session.cookies))
            page_ids = [a.get("value", "") for a in parsed.inputs
                        if (a.get("name") or a.get("id")) in {"activeId", "activePrimaryId"}
                        and a.get("value")]
            page_ids += re.findall(r'''\b(?:activeId|activePrimaryId)["']?\s*[:=]\s*["']?(\d{1,30})(?!\d)''', pre.text)
            if page_ids and any(value != str(context["activeId"]) for value in page_ids):
                raise UpstreamError("activity_mismatch", "预签到页面的活动与请求不一致，未发送签到。", 409)
            if not page_ids:
                return dict(status="unknown", message="预签到页面内容尚未识别，未发送签到。",
                            phase="presign", submitted=False, cookies=dump_cookies(client.session.cookies))
            headers = protocol.build_submit_headers(presign_url, self.user_agent)
            submitted = True
            result = client.request(method, protocol.MOBILELEARN_ORIGIN + protocol.SIGN_PATH,
                                    params=fields if method == "GET" else None,
                                    data=protocol.encode_form(fields) if method == "POST" else None,
                                    headers=headers)
            if _session_page(result):
                raise UpstreamError("session_expired", "学习通要求重新登录，签到结果未确认。", 401)
            classification = protocol.classify_sign_response(result.text, http_status=result.status_code)
            status = classification["status"]
            messages = {
                "reported_success": "学习通返回签到成功；尚未独立读取考勤记录复核。",
                "already_signed": "学习通返回该账号已签到。",
                "requires_validation": "学习通要求验证码，请完成官方验证后再提交。",
                "device_conflict": "学习通提示设备冲突，本次签到未完成。",
                "invalid_challenge": "学习通返回的验证挑战不完整，未自动重试。",
                "rejected": "学习通拒绝了这次签到。",
                "http_error": "签到请求返回异常状态，结果未确认。",
                "unknown": "未能识别学习通的签到结果，未自动重试。",
            }
            output = dict(status=status, message=messages[status], phase="submit", submitted=True,
                          httpStatus=result.status_code, cookies=dump_cookies(client.session.cookies))
            if classification.get("enc2"):
                output["challenge"] = {"enc2": classification["enc2"]}
            return output
        except UpstreamError as error:
            error.details.update(phase="submit" if submitted else "presign", submitted=submitted)
            raise
        except (OutcomeUnknown, DirectClientError) as error:
            if submitted:
                return dict(status="outcome_unknown", message="请求已发出但结果未确认；请先核对学习通，勿重复提交。",
                            phase="submit", submitted=True, cookies=dump_cookies(client.session.cookies))
            raise _map_transport(error) from None
        finally:
            client.close()

    def get_im_credentials(self, cookies, identity):
        client = self._client(cookies)
        client.timeout = (5, 12)
        try:
            confirmed = self._profile(client)
            if confirmed.uid != str(identity['uid']) or confirmed.fid != str(identity['fid']):
                raise UpstreamError('identity_mismatch', '消息授权身份不一致。', 409)
            return fetch_im_credentials(client, confirmed.puid)
        except GroupNotificationError as error:
            raise UpstreamError(error.code, str(error), error.http_status) from None
        except DirectClientError as error:
            raise _map_transport(error, 'im_connect') from None
        finally:
            client.close()

    def get_group_activities(self, cookies, identity):
        """Discover own received group activities without entering sign pages."""
        client = self._client(cookies)
        client.timeout = (5, 12)
        try:
            confirmed = self._profile(client)
            if confirmed.uid != str(identity['uid']) or confirmed.fid != str(identity['fid']):
                raise UpstreamError('identity_mismatch', '会话资料与选定账号不一致，未读取群活动。', 409)
            result = fetch_group_notifications(client, confirmed.puid, user_agent=self.user_agent)
            result['groupCount'] = result['sourceCoverage']['groupCount']
            result['cookies'] = dump_cookies(client.session.cookies)
            return result
        except GroupNotificationError as error:
            raise UpstreamError(error.code, str(error), error.http_status) from None
        except DirectClientError as error:
            raise _map_transport(error, 'group_activities') from None
        finally:
            client.close()

    def sign_group_ordinary(self, cookies, identity, active_id):
        return self.sign_group_activity(cookies, identity, active_id, kind='ordinary')

    def sign_group_activity(self, cookies, identity, active_id, *, kind):
        """Direct official group student entry; its GET can itself sign.

        The caller reserves a group-scoped idempotent job before this method.
        Membership and type come from this account's official notifications.
        No QR text, Code, enc, or caller-supplied URL is used by this branch.
        """
        expected_kind = {'ordinary': 'ordinary_candidate', 'qr': 'qr', 'gesture': 'gesture'}.get(kind)
        if expected_kind is None:
            raise UpstreamError('unsupported_sign_kind', '群活动直接入口只处理普通、二维码与手势签到。', 422)
        if not _numeric_id(active_id):
            raise UpstreamError('invalid_sign_input', '请提供有效的群签到活动标识。', 422)
        active_id = str(active_id)
        client = self._client(cookies)
        client.timeout = (5, 12)
        submitted, phase = False, 'profile'
        try:
            confirmed = self._profile(client)
            if confirmed.uid != str(identity['uid']) or confirmed.fid != str(identity['fid']):
                raise UpstreamError('identity_mismatch', '会话资料与选定账号不一致，未发送签到。', 409)
            phase = 'group_activity'
            result = fetch_group_notifications(client, confirmed.puid, user_agent=self.user_agent)
            matches = [a for a in result['activities'] if a['activeId'] == active_id]
            if len(matches) != 1 or matches[0]['kind'] != expected_kind:
                return dict(status='requires_manual', message='本人群通知未确认活动归属或请求的签到类型，未进入签到页面。',
                            phase=phase, submitted=False, cookies=dump_cookies(client.session.cookies))
            endpoint = '/widget/sign/group/pcStuSignGroupController/preSign'
            phase, submitted = 'group_entry', True
            response = client.request('GET', protocol.MOBILELEARN_ORIGIN + endpoint,
                                      params={'activeId': active_id},
                                      headers=protocol.build_presign_headers(self.user_agent),
                                      follow_redirects=False)
            if _session_page(response):
                raise UpstreamError('session_expired', '学习通要求重新登录，群签到结果尚未确认。', 401)
            details = group_pc_details(response.text, response.status_code, active_id, confirmed.puid)
            if details['reasonCode'] in ('group_form_pending', 'photo_required'):
                submitted = False
                if kind == 'ordinary' and details['reasonCode'] == 'photo_required':
                    return self._sign_group_photo(client, confirmed, active_id, matches[0].get('chatId'))
                if kind == 'gesture' and group_gesture_ready(response.text, active_id, confirmed.puid):
                    endpoint = '/widget/sign/group/pcStuSignGroupController/signIn'
                    phase, submitted = 'group_submit', True
                    response = client.request('GET', protocol.MOBILELEARN_ORIGIN + endpoint,
                        params={'activeId': active_id},
                        headers=protocol.build_presign_headers(self.user_agent), follow_redirects=False)
                    if _session_page(response):
                        raise UpstreamError('session_expired', '学习通要求重新登录，群签到结果尚未确认。', 401)
                    details = group_pc_details(response.text, response.status_code, active_id, confirmed.puid)
            status = details['status']
            messages = {
                'reported_success': '学习通群' + {'qr': '二维码', 'gesture': '手势', 'ordinary': '普通'}[kind] + '签到记录显示签到成功。',
                'already_signed': '学习通返回该账号已经签到。',
                'rejected': '学习通未接受本次群签到，请核对活动是否仍在进行。',
                'need_context': '群页面仍需要确认或补充资料，未继续提交。',
                'face_required': '此群签到要求人脸验证，请在学习通完成。',
                'photo_required': '此群签到需要照片，请在学习通手机端完成拍照签到。',
                'location_required': '此群签到要求真实位置，请在学习通完成。',
                'requires_validation': '此群签到要求官方验证，请在学习通完成。',
                'device_conflict': '学习通返回设备限制，请查看官方活动。',
                'http_error': '学习通返回异常状态，群签到结果尚未确认。',
                'unknown': '群签到结果尚未识别，请先核对学习通记录，不自动重发。',
            }
            private = {} if status in ('reported_success', 'already_signed') else {'_diagnostic': {
                'endpoint': endpoint, 'httpStatus': response.status_code,
                'body': response.text[:65536], 'bodyTruncated': len(response.text) > 65536}}
            return dict(status=status, message=messages[status], activeId=active_id,
                        phase=phase, submitted=submitted, httpStatus=response.status_code,
                        reasonCode=details['reasonCode'], diagnostics=details,
                        cookies=dump_cookies(client.session.cookies), **private)
        except GroupNotificationError as error:
            raise UpstreamError(error.code, str(error), error.http_status,
                                {'phase': phase, 'submitted': submitted}) from None
        except UpstreamError as error:
            if error.details.get('resolvedKind') != 'photo':
                error.details.update(phase=phase, submitted=submitted)
            raise
        except DirectClientError as error:
            if submitted:
                return dict(status='outcome_unknown', message='群签到请求已发出，结果尚未确认；请勿重复提交。',
                            phase=phase, submitted=True, cookies=dump_cookies(client.session.cookies))
            mapped = _map_transport(error, phase)
            mapped.details.update(submitted=False)
            raise mapped from None
        finally:
            client.close()

    def _sign_group_photo(self, client, confirmed, active_id, chat_id):
        """Use only bound records from this group activity, then verify our record."""
        submitted, phase = False, 'group_photo_record'
        response = None
        endpoint = '/newsign/tiaoZhuanSignDetail'
        headers = {'User-Agent': GROUP_PHOTO_USER_AGENT}

        def output(status, message, reason):
            private = {} if response is None or status in ('reported_success', 'already_signed') else {
                '_diagnostic': {'endpoint': endpoint, 'httpStatus': response.status_code,
                                'body': response.text[:65536], 'bodyTruncated': len(response.text) > 65536}}
            return dict(status=status, message=message, reasonCode=reason, resolvedKind='photo',
                        activeId=active_id, submitted=submitted, phase=phase,
                        httpStatus=response.status_code if response is not None else None,
                        cookies=dump_cookies(client.session.cookies), **private)

        def read(path, params=None):
            nonlocal response, endpoint
            endpoint = urlsplit(path).path
            url = path if path.startswith('https://') else protocol.MOBILELEARN_ORIGIN + path
            response = None
            response = client.request('GET', url, params=params, headers=headers, follow_redirects=False)
            if response.status_code == 401 or _session_page(response):
                raise UpstreamError('session_expired', '学习通要求重新登录，群拍照签到尚未完成。', 401)
            if response.status_code == 403:
                raise UpstreamError('group_photo_forbidden', '学习通未允许读取当前群拍照资料，未继续提交。', 403)
            if response.status_code == 429:
                raise UpstreamError('upstream_rate_limited', '学习通提示操作过于频繁，请稍后再试。', 429)
            return response

        def record(puid):
            reply = read('/newsign/tiaoZhuanSignDetail',
                         {'activeId': active_id, 'uid': puid, 'role': '0', 'appType': '0'})
            return photo_record(reply.text, reply.status_code, active_id, puid)

        try:
            if not _numeric_id(chat_id):
                return output('need_context', '群通知缺少可核验的群标识，未提交拍照签到。', 'photo_context_required')
            if record(confirmed.puid)['signed']:
                return output('already_signed', '学习通本人记录确认已经签到。', 'photo_already_signed')
            if response.status_code != 200:
                return output('photo_required', '本人签到记录暂未查清，未提交拍照签到。', 'photo_lookup_incomplete')
            phase = 'group_photo_form'
            entry = read('/sign/preSign', {'activeId': active_id, 'chatId': str(chat_id),
                                          'courseId': 'null', 'general': '0', 'ls': '1'})
            if entry.status_code != 302:
                return output('need_context', '群拍照学生入口尚未核验，未继续提交。', 'photo_form_unverified')
            try:
                form_url = photo_redirect(entry.headers.get('Location'), active_id, chat_id, confirmed.puid)
            except ValueError:
                return output('need_context', '群拍照页面跳转无法核验，未继续提交。', 'photo_form_unverified')
            form = read(form_url)
            if form.status_code != 200 or not photo_form_ready(form.text, active_id, confirmed.puid):
                return output('need_context', '群拍照页面或附加条件未通过核验，请在学习通查看。', 'photo_form_unverified')
            phase = 'group_photo_lookup'
            members_reply = read('/widget/sign/api/findPuidsByActiveId', {'activeId': active_id})
            try:
                if members_reply.status_code != 200:
                    raise ValueError()
                members = parse_photo_members(members_reply.json(), confirmed.puid)
            except ValueError:
                return output('photo_required', '活动成员记录尚未查完整，未确认可用照片，待补查。', 'photo_lookup_incomplete')
            incomplete = len(members) > 20
            object_id = None
            for member in members[:20]:
                try:
                    found = record(member)
                except DirectClientError:
                    incomplete = True
                    break
                if found['signed'] and found['objectId']:
                    object_id = found['objectId']
                    break
                incomplete |= not found['signed']
            if object_id is None:
                if incomplete:
                    return output('photo_required', '本轮仅核查有限的活动记录，尚未确认可用照片，待补查。', 'photo_lookup_incomplete')
                return output('photo_required', '暂未找到同活动的可用签到照片，等待后续照片并补查。', 'photo_missing')
            # Re-read the bound camera form after a potentially slow member lookup.
            phase = 'group_photo_form'
            form = read(form_url)
            if form.status_code != 200 or not photo_form_ready(form.text, active_id, confirmed.puid):
                return output('need_context', '群拍照活动状态或条件已变化，未提交。', 'photo_form_unverified')
            phase, submitted = 'group_photo_submit', True
            endpoint = '/sign/stuSignajax'
            response = None
            response = client.request('POST', protocol.MOBILELEARN_ORIGIN + endpoint,
                data={'activeId': active_id, 'uid': confirmed.puid, 'clientip': '', 'useragent': '',
                      'latitude': '-1', 'longitude': '-1', 'fid': confirmed.fid,
                      'objectId': object_id, 'photoResult': ''},
                headers={**headers, 'Referer': form_url, 'Origin': protocol.MOBILELEARN_ORIGIN,
                         'X-Requested-With': 'XMLHttpRequest'}, follow_redirects=False)
            phase = 'group_photo_verify'
            verified = record(confirmed.puid)
            if verified['signed'] and verified['objectId'] == object_id:
                return output('reported_success', '学习通本人记录确认群拍照签到成功。', 'photo_record_verified')
            return output('outcome_unknown', '拍照签到已提交，本人记录尚未确认；请勿重复提交。', 'photo_record_unverified')
        except UpstreamError as error:
            if submitted:
                return output('outcome_unknown', '拍照签到已提交，本人记录暂时无法核验；请勿重复提交。', 'photo_record_unverified')
            error.details.update(phase=phase, submitted=False, resolvedKind='photo')
            raise
        except DirectClientError as error:
            if submitted:
                return output('outcome_unknown', '拍照签到请求已发出，结果尚未确认；请勿重复提交。', 'photo_record_unverified')
            mapped = _map_transport(error, phase)
            mapped.details.update(submitted=False, resolvedKind='photo')
            raise mapped from None

    def _sign_course_photo(self, cookies, identity, payload):
        """Course form integration. Automatic peer-photo acquisition is unresolved."""
        context, method, fields = self._sign_fields(identity, payload)
        client = self._client(cookies)
        client.timeout = (5, 12)
        submitted, phase = False, 'profile'

        def output(status, message, reason):
            return dict(status=status, message=message, reasonCode=reason, phase=phase,
                        submitted=submitted, cookies=dump_cookies(client.session.cookies))

        def read_page():
            reply = client.request('GET', presign_url,
                headers=protocol.build_presign_headers(self.user_agent), follow_redirects=False)
            if _session_page(reply) or reply.status_code == 401:
                raise UpstreamError('session_expired', '学习通要求重新登录，课程拍照记录尚未确认。', 401)
            if reply.status_code == 429:
                raise UpstreamError('upstream_rate_limited', '学习通提示操作过于频繁，请稍后再试。', 429)
            return reply

        def own_signed():
            try:
                history = self._activity_json(client, '/v2/apis/signStat/getActiveList', {
                    'courseId': context['courseId'], 'classId': context['classId'],
                    'puid': confirmed.puid, 'pageSize': 9999, 'duid': '', 'denc': ''})
                parsed = parse_course_history(history, context, '')
            except ActivityPayloadError:
                return False
            return any(row['activeId'] == context['activeId'] for row in parsed['records'])

        try:
            confirmed = self._profile(client)
            if confirmed.uid != str(identity['uid']) or confirmed.fid != str(identity['fid']):
                raise UpstreamError('identity_mismatch', '会话资料与选定账号不一致，未发送签到。', 409)
            phase = 'activity'
            try:
                detail = self._activity_json(client, '/v2/apis/active/getPPTActiveInfo', {'activeId': context['activeId']})
                record = detail.get('data') if isinstance(detail, dict) else None
                if not isinstance(record, dict) or not all(any(
                        _numeric_id(record.get(key)) == str(context[field]) for key in keys)
                        for keys, field in ((('activeId', 'id'), 'activeId'), (('classId', 'clazzid'), 'classId'))):
                    raise ActivityPayloadError('Missing binding')
                activity = parse_activity_detail(detail, context, context)
            except ActivityPayloadError:
                return output('requires_manual', '课程拍照的活动与班级绑定尚未确认，未提交。', 'photo_rules_unverified')
            names = [record.get(key) for key in ('name', 'nameOne', 'nameFour', 'title', 'title1', 'title2')]
            names.append(payload.get('_candidateName'))
            if record.get('jwCourseId') not in (None, '', 0, '0') or any(isinstance(n, str) and '教务' in n for n in names):
                return output('requires_manual', '教务拍照签到的额外前置流程尚未核验，请在学习通处理。', 'photo_rules_unverified')
            if (not activity or activity['kind'] != 'photo' or activity['requirements'] != ['photo']
                    or activity['status'] not in ('active', 'ended')
                    or any(activity_number(record.get(key)) != value for key, value in
                           (('otherId', 0), ('ifphoto', 1), ('ifopenAddress', 0), ('openCheckFaceFlag', 0)))):
                return output('requires_manual', '课程拍照条件未确认，或包含位置、人脸等附加要求，未提交。', 'photo_rules_unverified')
            phase = 'course_photo_presign'
            presign_url = protocol.build_presign_url(context)
            pre = read_page()
            own = photo_record(pre.text, pre.status_code, context['activeId'], confirmed.uid)
            if own['signed'] and own_signed():
                return output('already_signed', '学习通本人记录确认此课程活动已经签到。', 'photo_already_signed')
            if activity['status'] == 'ended':
                return output('rejected', '课程拍照活动已结束，未发送签到。', 'photo_activity_ended')
            if pre.status_code != 200 or own['signed']:
                return output('need_context', '课程拍照预签到页面或本人记录尚未核验，未提交。', 'photo_form_unverified')
            if not fields:
                return output('photo_required', '课程同活动照片的读取接口尚未核实；未取得本次照片资料，未提交。',
                              'course_photo_source_unavailable')
            try:
                page = page_context(pre.text, context['activeId'])
            except QrProtocolError:
                page = {}
            parsed = _PageText()
            parsed.feed(pre.text)
            visible = ''.join(parsed.parts)
            page_uids = [item.get('value') for item in parsed.inputs
                         if (item.get('name') or item.get('id')) == 'uid' and item.get('value')]
            page_uids += re.findall(r'''\bvar\s+uid\s*=\s*['"]([^'"]+)['"]''', pre.text)
            if (not page.get('matchedActivity') or any(page.get(k, context[k]) != context[k] for k in ('courseId', 'classId'))
                    or not page_uids or any(value != confirmed.uid for value in page_uids)
                    or any(page.get(k) == '1' for k in ('ifopenAddress', 'openCheckFaceFlag'))
                    or re.search(r'(?:pptSign/analysis|analysis2)', pre.text)
                    or any(word in visible for word in ('人脸', '定位', '位置签到', '验证码', '需要验证', '活动已结束',
                                                       '签到已结束', '活动不存在', '签到成功', '已签到'))):
                return output('need_context', '课程拍照页面或额外前置条件尚未核验，未提交。', 'photo_form_unverified')
            phase, submitted = 'course_photo_submit', True
            client.request(method, protocol.MOBILELEARN_ORIGIN + protocol.SIGN_PATH,
                data=protocol.encode_form(fields), headers=protocol.build_submit_headers(presign_url, self.user_agent),
                follow_redirects=False)
            phase = 'course_photo_verify'
            reply = read_page()
            verified = photo_record(reply.text, reply.status_code, context['activeId'], confirmed.uid)
            if verified['signed'] and verified['objectId'] == fields['objectId'] and own_signed():
                return output('reported_success', '学习通本人照片及课程记录均确认签到成功。', 'photo_record_verified')
            return output('outcome_unknown', '课程拍照已提交，本人记录尚未确认；请勿重复提交。', 'photo_record_unverified')
        except UpstreamError as error:
            if submitted:
                return output('outcome_unknown', '课程拍照已提交，本人记录暂时无法核验；请勿重复提交。', 'photo_record_unverified')
            error.details.update(phase=phase, submitted=False)
            raise
        except DirectClientError as error:
            if submitted:
                return output('outcome_unknown', '课程拍照请求已发出，结果尚未确认；请勿重复提交。', 'photo_record_unverified')
            mapped = _map_transport(error, phase)
            mapped.details.update(submitted=False)
            raise mapped from None
        finally:
            client.close()

    def _sign_course_direct(self, cookies, identity, payload):
        """Separate ordinary/gesture rules before a single direct GET.

        The gesture variant omits signCode and awaits live compatibility
        acceptance; ordinary evidence does not prove gesture acceptance.
        """
        kind = payload['kind']
        label = '手势' if kind == 'gesture' else '普通'
        context, method, fields = self._sign_fields(identity, payload)
        client = self._client(cookies)
        client.timeout = (5, 12)
        submitted, phase = False, 'profile'

        def output(status, message, **extra):
            return dict(status=status, message=message, phase=phase, submitted=submitted,
                        cookies=dump_cookies(client.session.cookies), **extra)

        try:
            confirmed = self._profile(client)
            if confirmed.uid != str(identity['uid']) or confirmed.fid != str(identity['fid']):
                raise UpstreamError('identity_mismatch', '会话资料与选定账号不一致，未发送签到。', 409)
            phase = 'activity'
            record = {}
            try:
                detail = self._activity_json(client, '/v2/apis/active/getPPTActiveInfo',
                                             {'activeId': context['activeId']})
                record = detail.get('data') if isinstance(detail, dict) else None
                if not isinstance(record, dict) or not all(any(
                        _numeric_id(record.get(key)) == str(context[field]) for key in keys)
                        for keys, field in ((('activeId', 'id'), 'activeId'),
                                            (('classId', 'clazzid'), 'classId'))):
                    return output('requires_manual', '活动详情缺少可核验的活动或班级绑定，未发送签到。')
                activity = parse_activity_detail(detail, {'activeId': str(context['activeId'])},
                    {'courseId': str(context['courseId']), 'classId': str(context['classId'])})
            except ActivityPayloadError:
                activity = None
            names = [record.get(key) for key in ('name', 'nameOne', 'nameFour', 'title', 'title1', 'title2')]
            names.extend((payload.get('_candidateName'), activity.get('name') if activity else None))
            if (record.get('jwCourseId') not in (None, '', 0, '0') or
                    any(isinstance(name, str) and '教务' in name for name in names)):
                return output('requires_manual', '教务签到的官方前置流程尚未核验，请在学习通处理，未发送签到。')
            if activity and activity['status'] == 'ended':
                return output('rejected', f'学习通提示{label}签到已结束，未发送签到。')
            ready = activity and activity['kind'] == kind and activity['status'] == 'active'
            if kind == 'gesture':
                ready = ready and activity_number(detail['data'].get('otherId')) == 3 and all(
                    activity_number(detail['data'].get(key)) == 0
                    for key in ('ifphoto', 'ifopenAddress', 'openCheckFaceFlag'))
            else:
                ready = ready and activity['autoEligible']
            if not ready:
                return output('requires_manual', f'活动不是正在进行且无附加条件的{label}签到，未发送签到。')
            phase = 'validation'
            reply = client.request(**ordinary_validation_request(context['activeId']),
                                   headers={'Accept': 'application/json'}, follow_redirects=False)
            if _session_page(reply):
                raise UpstreamError('session_expired', '学习通会话已失效，未发送签到。', 401)
            if reply.status_code != 200:
                return output('requires_manual', f'{label}签到验证状态未能读取，未发送签到。')
            try:
                needs_validation = ordinary_validation_required(reply.json())
            except (ValueError, OrdinaryProtocolError):
                return output('requires_manual', f'{label}签到验证状态未能确认，未发送签到。')
            if needs_validation:
                return output('requires_validation', '学习通要求验证码，请在官方页面完成后处理。')
            phase, submitted = 'submit', True
            response = client.request(method, protocol.MOBILELEARN_ORIGIN + protocol.SIGN_PATH,
                                      params=fields,
                                      headers=protocol.build_submit_headers(protocol.MOBILELEARN_ORIGIN + '/', self.user_agent),
                                      follow_redirects=False)
            if _session_page(response):
                raise UpstreamError('session_expired', '学习通要求重新登录，签到结果尚未确认。', 401)
            result = protocol.classify_sign_response(response.text, http_status=response.status_code)
            messages = {'reported_success': f'学习通返回{label}签到成功。', 'already_signed': '学习通返回该账号已签到。',
                        'requires_validation': '学习通要求官方验证，请在学习通处理。',
                        'invalid_challenge': '学习通返回的验证资料不完整，请在学习通处理。',
                        'device_conflict': f'学习通返回设备限制，{label}签到尚未确认成功。',
                        'rejected': f'学习通拒绝了这次{label}签到。', 'http_error': f'{label}签到返回异常状态，结果尚未确认。',
                        'unknown': f'{label}签到回执尚未识别，请先核对学习通记录。'}
            private = {}
            if result['status'] not in ('reported_success', 'already_signed'):
                private['_diagnostic'] = {'endpoint': protocol.SIGN_PATH, 'httpStatus': response.status_code,
                    'body': response.text[:65536], 'bodyTruncated': len(response.text) > 65536}
            return output(result['status'], messages[result['status']], httpStatus=response.status_code, **private)
        except UpstreamError as error:
            error.details.update(phase=phase, submitted=submitted)
            raise
        except DirectClientError as error:
            if submitted:
                return output('outcome_unknown', f'{label}签到请求已发出，结果尚未确认；请勿重复提交。')
            mapped = _map_transport(error, phase)
            mapped.details.update(submitted=False)
            raise mapped from None
        finally:
            client.close()
