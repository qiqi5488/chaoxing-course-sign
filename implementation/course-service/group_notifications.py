"""Read the selected account's official attendance notifications, without signing.

Evidence: analysis/upstream/public/webim-index-1fcc67b7.js:37 (token API),
:53 (fixed REST origin, user_channels/list, messageroaming and protobuf schema),
webim-Home-e56ba9d7.js:1 (20 recent messages and ext.attachment), and
GROUP-DISCOVERY-EVIDENCE.txt (the authenticated /sign/preSign notification).
The caller must first verify the account with the official profile endpoint.
Tokens and message bodies stay in memory; this module never visits sign URLs.
"""
import base64
import binascii
import json
import re
from urllib.parse import parse_qsl, quote, urlsplit

import requests
from urllib3.util.retry import Retry


TOKEN_URL = 'https://learn.chaoxing.com/apis/user/getUserImToken'
REST_ORIGIN = 'https://a3-vip6.easemob.com'
NAMESPACE = '/cx-dev/cxstudy'
PAGE_SIZE = 20
MAX_CHANNEL_PAGES = 5
MAX_HISTORY_GROUPS = 20
MAX_JSON_BYTES = 2_000_000
MAX_MESSAGE_BYTES = 262_144
DEFAULT_USER_AGENT = 'CourseSign/1.0 (official group notifications)'


class GroupNotificationError(Exception):
    """Finite errors that never include tokens, messages or upstream text."""
    def __init__(self, code, message, http_status=502):
        self.code = code
        self.http_status = http_status
        super().__init__(message)

    def __repr__(self):
        return f'{type(self).__name__}(code={self.code!r})'


class _Malformed(ValueError):
    def __init__(self, reason='invalid_payload'):
        self.reason = reason
        super().__init__(reason)


def _object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise _Malformed()
        result[key] = value
    return result


def _invalid_constant(_):
    raise _Malformed()


def _loads(value, limit=MAX_MESSAGE_BYTES):
    if not isinstance(value, str) or len(value) > limit:
        raise _Malformed()
    try:
        result = json.loads(value, object_pairs_hook=_object, parse_constant=_invalid_constant)
    except (ValueError, RecursionError):
        raise _Malformed() from None
    if not isinstance(result, dict):
        raise _Malformed()
    return result


def _identifier(value):
    if type(value) not in (str, int):
        return None
    value = str(value)
    return value if re.fullmatch(r'[1-9][0-9]{0,29}', value) else None


def _plain(value, limit):
    return (isinstance(value, str) and 0 < len(value) <= limit
            and value == value.strip()
            and not any(ord(c) < 32 or 127 <= ord(c) <= 159 for c in value))


def _payload(response, *, token=False):
    if response.status_code != 200:
        code = 'session_expired' if token and response.status_code == 401 else 'group_source_unavailable'
        raise GroupNotificationError(code, '无法读取官方群活动来源。', 401 if code == 'session_expired' else 502)
    if len(response.content) > MAX_JSON_BYTES:
        raise GroupNotificationError('group_payload_unrecognized', '官方群活动来源格式无法识别。')
    try:
        return _loads(response.content.decode('utf-8-sig'), MAX_JSON_BYTES)
    except (UnicodeError, _Malformed):
        raise GroupNotificationError('group_payload_unrecognized', '官方群活动来源格式无法识别。') from None


def _activity(attachment):
    """Use the observed student notification, not teacher pcUrl or its title."""
    if not isinstance(attachment, dict) or attachment.get('attachmentType') != 15:
        return None
    activity = attachment.get('att_chat_course')
    if (not isinstance(activity, dict) or str(activity.get('atype')) != '2'
            or str(activity.get('type')) != '1'):
        return None
    raw = activity.get('url')
    if not _plain(raw, 8192) or re.search(r'%(?![0-9A-Fa-f]{2})', raw):
        return None
    try:
        url = urlsplit(raw)
        # /newsign/preSign is a course notification, even when its title says
        # ordinary sign or QR sign. A pcUrl can be a teacher page: never use it.
        if (url.scheme != 'https' or url.hostname != 'mobilelearn.chaoxing.com'
                or url.port not in (None, 443) or url.username is not None
                or url.password is not None or url.fragment or url.path != '/sign/preSign'):
            return None
        pairs = parse_qsl(url.query, keep_blank_values=True, strict_parsing=True,
                          errors='strict', max_num_fields=16)
        fields = dict(pairs)
        if len(fields) != len(pairs):
            return None
        active_id = _identifier(fields.get('activeId'))
        identities = [activity[key] for key in ('aid', 'id') if key in activity]
        if (not active_id or not identities or any(_identifier(v) != active_id for v in identities)
                or not _plain(fields.get('chatId'), 512)):
            return None
    except (ValueError, UnicodeError):
        return None
    name = activity.get('title') or activity.get('atypeName')
    name = ''.join(c for c in name if ord(c) >= 32 and not 127 <= ord(c) <= 159)[:200] if isinstance(name, str) else ''
    # These are platform labels observed in the authenticated notifications,
    # not user-supplied activity titles. A candidate still needs its own page;
    # it never implies absence of photo/location/face requirements.
    label = activity.get('atypeName')
    kind = ({'签到': 'ordinary_candidate', '普通签到': 'ordinary_candidate',
             '二维码签到': 'qr', '手势签到': 'gesture'}.get(label, 'unknown') if isinstance(label, str) else 'unknown')
    return {'activeId': active_id, 'chatId': fields['chatId'], 'name': name or '群签到活动',
            'kind': kind, 'status': 'unknown', 'activityScope': 'group',
            'notificationType': 'group_attendance',
            'requirements': ['qr', 'unknown'] if kind == 'qr' else ['unknown'], 'autoEligible': False}


def _course_activity(attachment):
    """Normalize a course notification; its title never establishes sign rules."""
    if not isinstance(attachment, dict) or attachment.get('attachmentType') != 15:
        return None
    activity = attachment.get('att_chat_course')
    if (not isinstance(activity, dict) or str(activity.get('atype')) != '2'
            or str(activity.get('type')) != '1'):
        return None
    raw = activity.get('url')
    if not _plain(raw, 8192) or re.search(r'%(?![0-9A-Fa-f]{2})', raw):
        return None
    try:
        url = urlsplit(raw)
        if (url.scheme != 'https' or url.hostname != 'mobilelearn.chaoxing.com'
                or url.port not in (None, 443) or url.username is not None
                or url.password is not None or '#' in raw or url.path != '/newsign/preSign'):
            return None
        # Official course notifications end with one empty '&' separator.
        # Remove only that item; malformed fields and additional empties stay invalid.
        pairs = parse_qsl(url.query.removesuffix('&'), keep_blank_values=True, strict_parsing=True,
                          errors='strict', max_num_fields=16)
        fields = dict(pairs)
        if (len(fields) != len(pairs) or any(not _plain(key, 8192) for key in fields)
                or any(ord(c) < 32 or 127 <= ord(c) <= 159 for value in fields.values() for c in value)):
            return None
        # The authenticated official course notices use aid == activePrimaryId,
        # unlike group notices' activeId. Do not follow either notification URL.
        active_id = _identifier(fields.get('activePrimaryId'))
        identities = [activity[key] for key in ('aid', 'id') if key in activity]
        course_id, class_id = (_identifier(fields.get(key)) for key in ('courseId', 'classId'))
        if (not active_id or not identities or any(_identifier(v) != active_id for v in identities)
                or 'activeId' in fields and _identifier(fields['activeId']) != active_id
                or not course_id or not class_id):
            return None
        if 'courseInfo' in activity:
            info = activity['courseInfo']
            if (not isinstance(info, dict) or _identifier(info.get('courseid')) != course_id
                    or _identifier(info.get('classid')) != class_id):
                return None
    except (ValueError, UnicodeError):
        return None
    name = activity.get('title') or activity.get('atypeName')
    name = ''.join(c for c in name if ord(c) >= 32 and not 127 <= ord(c) <= 159)[:200] if isinstance(name, str) else ''
    return {'activeId': active_id, 'courseId': course_id, 'classId': class_id,
            'name': name or '课程签到活动', 'kind': 'unknown', 'status': 'unknown',
            'activityScope': 'course', 'notificationType': 'course_attendance',
            'requirements': ['unknown'], 'autoEligible': False}


def _latest_attachment(channel, group_id):
    meta = channel.get('meta')
    if not meta:
        return None
    if not isinstance(meta, dict):
        raise _Malformed('latest_meta_shape')
    target = group_id + '@conference.easemob.com'
    # The authenticated official conversation response uses the fixed app-key
    # prefix. Retain the bare JID form used by earlier synthetic contracts;
    # never strip arbitrary prefixes or accept a suffix-only group match.
    if meta.get('to') not in (target, 'cx-dev#cxstudy_' + target):
        raise _Malformed('latest_target')
    payload = meta.get('payload')
    payload = _loads(payload) if isinstance(payload, str) else payload
    if not isinstance(payload, dict):
        raise _Malformed('latest_payload_shape')
    if payload.get('type') != 'groupchat':
        raise _Malformed('latest_chat_type')
    if str(payload.get('to')) != group_id:
        raise _Malformed('latest_payload_target')
    ext = payload.get('ext', {})
    if ext is None:
        return None  # Optional ext absent/null is normal for a text message.
    if not isinstance(ext, dict):
        raise _Malformed('latest_extension_shape')
    return ext.get('attachment')


def _varint(data, position):
    value = 0
    for shift in range(0, 70, 7):
        if position >= len(data):
            raise _Malformed()
        byte = data[position]
        position += 1
        if shift == 63 and byte > 1:
            raise _Malformed()
        value |= (byte & 127) << shift
        if not byte & 128:
            return value, position
    raise _Malformed()


def _protobuf(data):
    """Bounded protobuf wire reader; no generated code, execution or recursion."""
    if not isinstance(data, bytes) or len(data) > MAX_MESSAGE_BYTES:
        raise _Malformed()
    fields, position, count = {}, 0, 0
    while position < len(data):
        count += 1
        if count > 512:
            raise _Malformed()
        tag, position = _varint(data, position)
        number, wire = tag >> 3, tag & 7
        if not 0 < number < 2 ** 29:
            raise _Malformed()
        if wire == 0:
            value, position = _varint(data, position)
        elif wire in (1, 2, 5):
            if wire == 2:
                length, position = _varint(data, position)
            else:
                length = 8 if wire == 1 else 4
            if position + length > len(data):
                raise _Malformed()
            value = data[position:position + length]
            position += length
        else:
            raise _Malformed()
        fields.setdefault(number, []).append((wire, value))
    return fields


def _one(fields, number, wire, default=None):
    values = fields.get(number, [])
    if not values:
        return default
    if len(values) != 1 or values[0][0] != wire:
        raise _Malformed()
    return values[0][1]


def _jid_matches(data, group_id):
    fields = _protobuf(data)
    name = _one(fields, 2, 2, b'').decode('utf-8')
    domain = _one(fields, 3, 2, b'').decode('utf-8')
    app_key = _one(fields, 1, 2, b'').decode('utf-8')
    return name == group_id and domain in ('', 'conference.easemob.com') and app_key in ('', 'cx-dev#cxstudy')


def _history_attachment(encoded, group_id):
    if not isinstance(encoded, str) or len(encoded) > (MAX_MESSAGE_BYTES + 2) // 3 * 4:
        raise _Malformed()
    try:
        meta = _protobuf(base64.b64decode(encoded, validate=True))
        if _one(meta, 5, 0) != 1:  # Meta.NameSpace.CHAT; ignore other system namespaces.
            return None
        if not _jid_matches(_one(meta, 3, 2, b''), group_id):
            raise _Malformed('history_target')
        body = _protobuf(_one(meta, 6, 2, b''))
        if _one(body, 1, 0) != 2:  # MessageBody.Type.GROUPCHAT, not READ_ACK etc.
            return None
        target = _one(body, 3, 2)
        if target is not None and not _jid_matches(target, group_id):
            raise _Malformed('history_body_target')
        contents = body.get(4, [])
        if not any(wire == 2 and _one(_protobuf(value), 1, 0, 0) == 0 for wire, value in contents):
            return None  # The official WebIM only reads attachments from text messages.
        found = []
        for wire, value in body.get(5, []):
            if wire != 2:
                raise _Malformed()
            entry = _protobuf(value)
            if _one(entry, 1, 2, b'').decode('utf-8') != 'attachment':
                continue
            if _one(entry, 2, 0) != 8:  # KeyValue.ValueType.JSON_STRING.
                raise _Malformed('history_attachment_type')
            found.append(_loads(_one(entry, 6, 2, b'').decode('utf-8')))
        if len(found) > 1:
            raise _Malformed()
        return found[0] if found else None
    except _Malformed:
        raise
    except (binascii.Error, UnicodeError, ValueError):
        raise _Malformed('history_encoding') from None


def fetch_im_credentials(client, expected_puid):
    """Obtain an identity-bound IM token; callers keep it only in memory."""
    expected_puid = _identifier(expected_puid)
    if not expected_puid:
        raise GroupNotificationError('identity_incomplete', '缺少已核验的通行证身份。', 409)
    response = client.request('GET', TOKEN_URL, params={'crossOrigin': 'true'},
                              headers={'Accept': 'application/json'}, follow_redirects=False)
    payload = _payload(response, token=True)
    token = payload.get('msg')
    if type(payload.get('result')) is not int or payload['result'] != 1 or not isinstance(token, dict):
        raise GroupNotificationError('group_token_unavailable', '学习通未返回可用的本人群消息授权。')
    if _identifier(token.get('puid')) != expected_puid:
        raise GroupNotificationError('identity_mismatch', '群消息身份与已核验账号不一致。', 409)
    user, bearer = token.get('uid'), token.get('hxToken')
    if (not isinstance(user, (str, int)) or isinstance(user, bool)
            or not re.fullmatch(r'[A-Za-z0-9_.@-]{1,128}', str(user)) or str(user) in ('.', '..')
            or not isinstance(bearer, str) or not 0 < len(bearer) <= 8192
            or any(not 33 <= ord(c) <= 126 for c in bearer)):
        raise GroupNotificationError('group_token_unavailable', '学习通未返回可用的本人群消息授权。')
    return {'username': str(user), 'accessToken': bearer}


def fetch_group_notifications(client, expected_puid, user_agent=DEFAULT_USER_AGENT):
    """Read bounded recent own conversations; never call a sign entry."""
    credentials = fetch_im_credentials(client, expected_puid)
    if not _plain(user_agent, 512):
        raise ValueError('A bounded user_agent is required.')
    own_user = quote(credentials['username'], safe='')
    bearer = credentials['accessToken']
    activities, course_activities, group_ids, seen_cursors = {}, {}, [], set()
    pages, channels_seen, malformed, history_read, history_failed, more_history = 0, 0, 0, 0, 0, 0
    malformed_sources, malformed_reasons = {'latest': 0, 'history': 0}, {}
    list_complete, cursor = False, ''

    def add(attachment):
        row = _activity(attachment)
        if row:
            activities.setdefault((row['activeId'], row['chatId']), row)
        course_row = _course_activity(attachment)
        if course_row:
            course_activities.setdefault((course_row['courseId'], course_row['classId'], course_row['activeId']), course_row)

    def record_malformed(error, source):
        malformed_sources[source] += 1
        reason = source + ':' + error.reason
        malformed_reasons[reason] = malformed_reasons.get(reason, 0) + 1

    # Never borrow the official CookieJar or transport session for the SDK host.
    with requests.Session() as im:
        im.trust_env, im.auth = False, None
        im.headers.clear()
        im.cookies.clear()
        for adapter in im.adapters.values():
            adapter.max_retries = Retry(total=0, connect=0, read=0, redirect=0, status=0)

        def read(method, path, **kwargs):
            im.cookies.clear()
            try:
                response = im.request(method, REST_ORIGIN + NAMESPACE + path,
                    headers={'Authorization': 'Bearer ' + bearer, 'Accept': 'application/json',
                             'Content-Type': 'application/json', 'User-Agent': user_agent},
                    timeout=(5, 12), allow_redirects=False, **kwargs)
                return _payload(response)
            except requests.RequestException:
                raise GroupNotificationError('group_source_unavailable', '官方群消息暂时无法读取。') from None
            finally:
                im.cookies.clear()

        for _ in range(MAX_CHANNEL_PAGES):
            try:
                result = read('GET', '/sdk/user/' + own_user + '/user_channels/list',
                    params={'limit': PAGE_SIZE, 'cursor': cursor, 'need_mark': 'true', 'needEmptySession': 'false'})
                data = result.get('data')
                if not isinstance(data, dict) or not isinstance(data.get('channel_infos'), list):
                    raise _Malformed()
                rows = data['channel_infos']
                if len(rows) > PAGE_SIZE:
                    raise _Malformed()
                next_cursor = data.get('cursor', '')
                if next_cursor is None:
                    next_cursor = ''
                if (not isinstance(next_cursor, str)
                        or next_cursor and (not _plain(next_cursor, 2048) or next_cursor in seen_cursors)):
                    raise _Malformed()
                pages += 1
                channels_seen += len(rows)
                for channel in rows:
                    if not isinstance(channel, dict):
                        malformed += 1
                        continue
                    if channel.get('session_type') != 'groupchat':
                        if channel.get('session_type') != 'chat':
                            malformed += 1
                        continue
                    group_id = _identifier(channel.get('session_to'))
                    if not group_id:
                        malformed += 1
                        continue
                    if group_id not in group_ids:
                        group_ids.append(group_id)
                    try:
                        add(_latest_attachment(channel, group_id))
                    except _Malformed as error:
                        malformed += 1
                        record_malformed(error, 'latest')
                if not next_cursor:
                    list_complete = True
                    break
                seen_cursors.add(next_cursor)
                cursor = next_cursor
            except (GroupNotificationError, _Malformed):
                if not pages:
                    raise GroupNotificationError('group_source_unavailable', '无法读取本人群会话列表。') from None
                break

        for group_id in group_ids[:MAX_HISTORY_GROUPS]:
            try:
                result = read('POST', '/users/' + own_user + '/messageroaming', json={
                    'queue': group_id + '@conference.easemob.com', 'start': -1,
                    'pull_number': PAGE_SIZE, 'is_positive': False, 'msgType': '',
                    'end': -1, 'startTime': None, 'endTime': None, 'userId': None})
                data = result.get('data')
                if not isinstance(data, dict) or not isinstance(data.get('msgs'), list) or len(data['msgs']) > PAGE_SIZE:
                    raise _Malformed()
                history_read += 1
                if data.get('is_last') is not True:
                    more_history += 1
                for message in data['msgs']:
                    try:
                        if not isinstance(message, dict):
                            raise _Malformed()
                        add(_history_attachment(message.get('msg'), group_id))
                    except _Malformed as error:
                        malformed += 1
                        record_malformed(error, 'history')
            except (GroupNotificationError, _Malformed):
                history_failed += 1

    partial = (not list_complete or malformed or history_failed or more_history
               or len(group_ids) > MAX_HISTORY_GROUPS)
    return {'activities': list(activities.values()), 'courseActivities': list(course_activities.values()),
            'status': 'partial' if partial else 'ok',
            'message': '已读取本人群签到通知；类型、进行状态和附加条件仍需官方群页面核验。',
            'sourceCoverage': {
                'scope': 'current_conversations', 'channelPages': pages, 'channelCount': channels_seen,
                'conversationListComplete': list_complete, 'groupCount': len(group_ids),
                'historyGroupsRead': history_read, 'historyGroupsFailed': history_failed,
                'historyGroupsLimited': max(0, len(group_ids) - MAX_HISTORY_GROUPS),
                'historyMessageLimit': PAGE_SIZE, 'groupsWithEarlierHistory': more_history,
                'malformedMessages': malformed,
                'malformedBySource': malformed_sources, 'malformedReasons': malformed_reasons,
                'limitations': ['只包含当前会话列表中的群，不是全部已加入群。',
                                '每群最多读取最近20条历史消息，不追溯完整历史。']}}
