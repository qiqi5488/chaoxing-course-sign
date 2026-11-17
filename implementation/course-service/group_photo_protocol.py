"""Pure parsers for the observed official group-photo student flow.

The client header is a constructed compatibility identifier, not a captured
device identity. Source: the 2026-11-13 authorized group-photo field test.
"""
import re
from urllib.parse import parse_qsl, urlencode, urljoin, urlsplit

from qr_protocol import GroupSignReply, ID, group_sign_details


GROUP_PHOTO_USER_AGENT = (
    'Mozilla/5.0 (Linux; Android 13; Mobile) AppleWebKit/537.36 (KHTML, like Gecko) '
    'Version/4.0 Chrome/112.0.0.0 Mobile Safari/537.36 (device:generic) Language/zh_CN '
    'com.chaoxing.mobile/ChaoXingStudy_3_7.0.6_android_phone_11041025_33 (Android)_codex_client'
)
ORIGIN = 'https://mobilelearn.chaoxing.com'
PHOTO_PATH = '/sign/preStuSign'
REDIRECT_FIELDS = {'code': '', 'courseId': 'null', 'classId': '0', 'general': '0',
                   'appType': '0', 'tid': 'null', 'atype': 'null', 'sys': '0'}


def _identifier(value):
    return str(value) if type(value) in (str, int) and ID.fullmatch(str(value)) else None


def photo_redirect(location, active_id, chat_id, puid):
    """Validate the exact observed redirect; invalid input raises ValueError."""
    try:
        expected = {'activeId': _identifier(active_id), 'chatId': _identifier(chat_id), 'uid': _identifier(puid)}
        if (None in expected.values() or not isinstance(location, str) or not location
                or location != location.strip() or '#' in location
                or any(ord(c) < 32 or 127 <= ord(c) <= 159 for c in location)
                or re.search(r'%(?![0-9a-fA-F]{2})', location)):
            raise ValueError()
        url = urlsplit(urljoin(ORIGIN + '/', location))
        if (url.scheme != 'https' or url.hostname != 'mobilelearn.chaoxing.com'
                or url.port not in (None, 443) or url.username is not None or url.password is not None
                or url.path != PHOTO_PATH or url.fragment):
            raise ValueError()
        pairs = parse_qsl(url.query, keep_blank_values=True, strict_parsing=True,
                          errors='strict', max_num_fields=16)
        fields = dict(pairs)
        if len(pairs) != len(fields) or fields != {**REDIRECT_FIELDS, **expected}:
            raise ValueError()
        return ORIGIN + PHOTO_PATH + '?' + urlencode(pairs)
    except (ValueError, TypeError, UnicodeError):
        raise ValueError('群拍照页面跳转无法核验。') from None


class _PhotoPage(GroupSignReply):
    def __init__(self):
        super().__init__()
        self.scripts, self.in_script, self.inputs = [], False, {}
        self.camera, self.sign_button = False, False
        self.photo_scopes, self.object_ids = [], []

    def handle_starttag(self, tag, attrs):
        super().handle_starttag(tag, attrs)
        attrs = dict(attrs)
        visible = not (self.stack and self.stack[-1][1]) and not (
            'hidden' in attrs or attrs.get('aria-hidden', '').lower() == 'true'
            or re.search(r'(?:display\s*:\s*none|visibility\s*:\s*hidden)', attrs.get('style', ''), re.I))
        classes = attrs.get('class', '').split()
        if tag == 'script':
            self.in_script = True
        if tag == 'input':
            self.inputs.setdefault(attrs.get('id') or attrs.get('name'), []).append(attrs.get('value'))
        if visible:
            self.camera |= tag == 'div' and 'message_camera' in classes
            self.sign_button |= (tag == 'a' and attrs.get('id') == 'signButton'
                                 and re.fullmatch(r'sign\(\);?', attrs.get('onclick', '').strip()) is not None)
            if tag == 'div' and 'zsign_photo' in classes:
                self.photo_scopes.append(len(self.stack))
            if tag == 'img' and self.photo_scopes:
                self.object_ids.append(attrs.get('objid'))

    def handle_endtag(self, tag):
        super().handle_endtag(tag)
        if tag == 'script':
            self.in_script = False
        self.photo_scopes = [depth for depth in self.photo_scopes if depth <= len(self.stack)]

    def handle_data(self, data):
        super().handle_data(data)
        if self.in_script:
            self.scripts.append(data)


def _page(html):
    if not isinstance(html, str) or len(html) > 2_000_000:
        return None
    page = _PhotoPage()
    page.feed(html)
    return page


def _script(page):
    return re.sub(r'/\*.*?\*/|^\s*//[^\r\n]*', '', '\n'.join(page.scripts), flags=re.S | re.M)


def _bound(page, active_id, puid):
    script = _script(page)
    for name, expected in (('acId', _identifier(active_id)), ('uid', _identifier(puid))):
        assignments = re.findall(r'\bvar\s+' + name + r'\s*=', script)
        literals = re.findall(r'\bvar\s+' + name + r'''\s*=\s*(['"])([^'"]*)\1''', script)
        values = [value for _, value in literals if value]
        if (expected is None or len(assignments) != len(literals) or not values
                or any(_identifier(value) != expected for value in values)):
            return False
    return True


def photo_form_ready(html, active_id, puid):
    """True only for the bound, visible camera form and its observed POST."""
    page = _page(html)
    if not page or not _bound(page, active_id, puid) or not page.camera or not page.sign_button:
        return False
    visible = ''.join(page.parts)
    if any(term in visible for term in ('人脸', '定位', '获取位置', '位置签到', '位置信息', '验证码', '需要验证',
                                        '活动已结束', '签到已结束', '活动不存在', '签到成功', '已经签到')):
        return False
    if any(key in page.inputs and page.inputs[key] != ['0'] for key in ('ifopenAddress', 'openCheckFaceFlag')):
        return False
    script = _script(page)
    flags = re.findall(r'''\b(?:ifopenAddress|openCheckFaceFlag)['"]?\s*[:=](?!=)\s*([^\s,;}]+)''', script)
    if any(value not in ('0', '"0"', "'0'") for value in flags):
        return False
    return re.search(r'''\$\s*\.\s*ajax\s*\(\s*\{\s*url\s*:\s*(['"])/sign/stuSignajax\1\s*,\s*type\s*:\s*(['"])post\2\s*,\s*data\s*:\s*\{''',
                     script, re.I) is not None


def parse_photo_members(payload, own_puid):
    """Return other members from a complete, unique list containing this user."""
    own = _identifier(own_puid)
    data = payload.get('data') if isinstance(payload, dict) else None
    rows = data.get('list') if isinstance(data, dict) else None
    if (own is None or not isinstance(payload, dict) or type(payload.get('result')) is not int
            or payload['result'] != 1 or not isinstance(rows, list)
            or type(data.get('allCount')) is not int or data['allCount'] != len(rows)):
        raise ValueError('群签到成员列表无法核验。')
    members = [_identifier(row.get('puid')) if isinstance(row, dict) else None for row in rows]
    if None in members or len(set(members)) != len(members) or own not in members:
        raise ValueError('群签到成员列表无法核验。')
    return [member for member in members if member != own]


def photo_record(html, http_status, active_id, puid):
    """A verified success can lack a usable photo; identity failures reveal none."""
    empty = {'signed': False, 'objectId': None}
    page = _page(html)
    if not page or not _bound(page, active_id, puid):
        return empty
    if group_sign_details(html, http_status)['status'] not in ('reported_success', 'already_signed'):
        return empty
    ids = set(page.object_ids)
    photo = next(iter(ids)) if len(ids) == 1 else None
    return {'signed': True, 'objectId': photo if isinstance(photo, str) and re.fullmatch(r'[a-fA-F0-9]{32}', photo) else None}
