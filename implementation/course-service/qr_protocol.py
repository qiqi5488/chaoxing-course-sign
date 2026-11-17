"""Pure course/group QR parsing and bounded official-page context extraction."""
from html.parser import HTMLParser
import re
from urllib.parse import parse_qsl, urlsplit

OFFICIAL_DOMAINS = ('chaoxing.com', 'cxwfw.com', 'xuexitong.com')
ID = re.compile(r'^[1-9]\d{0,29}$')


class QrProtocolError(ValueError):
    def __init__(self, code, message):
        self.code = code
        super().__init__(message)


def parse_qr_target(qr_text):
    """Never fetch the supplied URL; only return signed activity parameters."""
    if not isinstance(qr_text, str) or not qr_text.strip() or len(qr_text) > 8192:
        raise QrProtocolError('invalid_qr', '二维码内容为空或过长。')
    raw = qr_text.strip()
    if any(ord(c) < 32 for c in raw):
        raise QrProtocolError('invalid_qr', '二维码含无效控制字符。')
    if raw.startswith('SIGNIN:'):
        query = raw[len('SIGNIN:'):]
    else:
        try:
            url = urlsplit(raw)
            host = (url.hostname or '').lower()
            official = any(host == d or host.endswith('.' + d) for d in OFFICIAL_DOMAINS)
            if (url.scheme not in ('http', 'https') or not official or url.username or url.password
                    or url.port not in (None, 80 if url.scheme == 'http' else 443)
                    or url.fragment or not url.path.startswith('/widget/sign/')):
                raise ValueError()
            query = url.query
        except ValueError:
            raise QrProtocolError('invalid_qr', '请扫描学习通官方签到二维码。') from None
    if re.search(r'%(?![0-9a-fA-F]{2})', query):
        raise QrProtocolError('invalid_qr', '二维码参数编码无效。')
    try:
        pairs = parse_qsl(query, keep_blank_values=True, strict_parsing=True, errors='strict')
    except (ValueError, UnicodeDecodeError):
        raise QrProtocolError('invalid_qr', '二维码参数编码无效。') from None
    fields = {}
    for key, value in pairs:
        canonical = {'id': 'activeId', 'aid': 'activeId', 'activeId': 'activeId',
                     'c': 'code', 'Code': 'code', 'enc': 'enc',
                     'courseId': 'courseId', 'classId': 'classId', 'source': 'source'}.get(key)
        if canonical:
            if canonical in fields:
                raise QrProtocolError('ambiguous_qr', '二维码含重复或冲突参数，请重新扫描老师的二维码。')
            fields[canonical] = value
    if not ID.fullmatch(fields.get('activeId', '')):
        raise QrProtocolError('invalid_qr', '二维码缺少有效的签到活动或 enc 参数。')
    for key in ('courseId', 'classId'):
        if key in fields and not ID.fullmatch(fields[key]):
            raise QrProtocolError('invalid_qr', '二维码课程或班级参数无效。')
    if any(any(ord(c) < 32 for c in value) for value in fields.values()):
        raise QrProtocolError('invalid_qr', '二维码参数含无效控制字符。')
    if fields.get('source') == '1':
        if not re.fullmatch(r'[A-Za-z0-9_-]{1,256}', fields.get('code', '')):
            raise QrProtocolError('invalid_qr', '群签到二维码缺少有效的 Code 参数。')
        return {key: fields[key] for key in ('activeId', 'source', 'code')}
    if not fields.get('enc'):
        raise QrProtocolError('invalid_qr', '二维码缺少有效的签到活动或 enc 参数。')
    if any(c.isspace() for c in fields['enc']) or len(fields['enc']) > 4096:
        raise QrProtocolError('invalid_qr', '二维码 enc 参数无效。')
    fields.pop('source', None)
    return fields


class GroupSignReply(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.stack = []
        self.parts, self.headings = [], []
        self.photo_parts, self.photo_depth = [], None
        self.success_marker = False
        self.pc_success_marker = False

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        hidden = (bool(self.stack and self.stack[-1][1]) or tag in ('script', 'style')
                  or 'hidden' in attrs or attrs.get('aria-hidden', '').lower() == 'true'
                  or bool(re.search(r'(?:display\s*:\s*none|visibility\s*:\s*hidden)', attrs.get('style', ''), re.I)))
        if not hidden and 'zsign_success' in attrs.get('class', '').split():
            self.success_marker = True
        if not hidden and 'qd_Success' in attrs.get('class', '').split():
            self.pc_success_marker = True
        if tag not in ('area', 'base', 'br', 'col', 'embed', 'hr', 'img', 'input', 'link', 'meta', 'param', 'source', 'track', 'wbr'):
            self.stack.append((tag, hidden))
        if tag == 'p' and not hidden and 'Photo_p' in attrs.get('class', '').split():
            self.photo_depth = len(self.stack)

    def handle_endtag(self, tag):
        for index in range(len(self.stack) - 1, -1, -1):
            if self.stack[index][0] == tag:
                del self.stack[index:]
                if self.photo_depth is not None and len(self.stack) < self.photo_depth:
                    self.photo_depth = None
                break

    def handle_data(self, data):
        if not self.stack or not self.stack[-1][1]:
            self.parts.append(data)
            if self.photo_depth is not None:
                self.photo_parts.append(data)
            if any(tag == 'h1' for tag, _ in self.stack):
                self.headings.append(data)


def group_sign_details(text, http_status):
    """Return bounded evidence codes, never arbitrary official-page text."""
    page = GroupSignReply()
    page.feed(str(text or '')[:2_000_000])
    visible = ''.join(page.parts)
    heading = ''.join(page.headings).strip()
    failures = [
        ('activity_ended', ('活动已结束', '签到已结束')),
        ('activity_missing', ('无此签到活动', '活动不存在')),
        ('qr_expired', ('二维码已失效', '二维码已过期')),
        ('qr_invalid', ('二维码不正确',)),
        ('sign_failed', ('签到失败',)),
    ]
    matched = [code for code, terms in failures if any(term in visible for term in terms)]
    heading_kind = ('success' if heading == '签到成功' else 'already_signed' if heading in
                    ('已签到', '已经签到', '您已签到过了') else 'failure' if any(
                        term in heading for _, terms in failures for term in terms) else 'other' if heading else 'none')
    details = {'status': 'unknown', 'reasonCode': 'unrecognized_result', 'headingKind': heading_kind,
               'matchedReasons': matched, 'successMarker': page.success_marker}

    def result(status, reason):
        return dict(details, status=status, reasonCode=reason)

    if not 200 <= http_status < 300:
        return result('http_error', 'http_error')
    if page.success_marker and heading_kind in ('success', 'already_signed'):
        if matched:
            return result('unknown', 'conflicting_result')
        return result('reported_success' if heading_kind == 'success' else 'already_signed', heading_kind)
    if any(term in visible for term in ('同一设备', '设备限制')):
        return result('device_conflict', 'device_conflict')
    if matched:
        return result('rejected', matched[0])
    if '人脸' in visible:
        return result('face_required', 'face_required')
    if any(term in visible for term in ('获取位置', '需要定位', '位置信息')):
        return result('location_required', 'location_required')
    if any(term in visible for term in ('验证码', '需要验证')):
        return result('requires_validation', 'requires_validation')
    return details


def group_sign_status(text, http_status):
    return group_sign_details(text, http_status)['status']


class Inputs(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.values = {}

    def handle_starttag(self, tag, attrs):
        if tag != 'input':
            return
        attrs = dict(attrs)
        key = attrs.get('name') or attrs.get('id')
        if key and 'value' in attrs:
            self.values.setdefault(key, []).append(attrs['value'])


def group_pc_details(text, http_status, active_id, puid):
    details = group_sign_details(text, http_status)
    if not 200 <= http_status < 300:
        return details
    inputs = Inputs()
    inputs.feed(str(text or '')[:2_000_000])
    expected = {'activeId': str(active_id), 'puid': str(puid)}
    for key, value in expected.items():
        if key in inputs.values and set(inputs.values[key]) != {value}:
            return dict(details, status='unknown', reasonCode='context_mismatch')
    page = GroupSignReply()
    page.feed(str(text or '')[:2_000_000])
    bound = all(inputs.values.get(key) == [value] for key, value in expected.items())
    if '此签到需要手机拍照' in ''.join(page.photo_parts):
        if not bound:
            return dict(details, status='need_context', reasonCode='page_identity_missing')
        if details['status'] in ('reported_success', 'already_signed') or (
                page.pc_success_marker and '签到成功' in ''.join(page.parts)):
            return dict(details, status='unknown', reasonCode='conflicting_result')
        if details['status'] == 'unknown' and inputs.values.get('activeStatus') == ['1']:
            return dict(details, status='photo_required', reasonCode='photo_required')
    if details['status'] in ('reported_success', 'already_signed') and not bound:
        return dict(details, status='need_context', reasonCode='page_identity_missing')
    if page.pc_success_marker and '签到成功' in ''.join(page.parts):
        if not bound:
            return dict(details, status='need_context', reasonCode='page_identity_missing')
        if details['matchedReasons']:
            return dict(details, status='unknown', reasonCode='conflicting_result')
        return dict(details, status='reported_success', reasonCode='record_signed',
                    headingKind='success', successMarker=True)
    if details['status'] == 'unknown' and bound:
        return dict(details, status='need_context', reasonCode='group_form_pending')
    return details


def group_gesture_ready(text, active_id, puid):
    """Recognize the observed, bound and active official group gesture form."""
    if group_pc_details(text, 200, active_id, puid)['reasonCode'] != 'group_form_pending':
        return False
    inputs = Inputs()
    inputs.feed(str(text or '')[:2_000_000])
    if inputs.values.get('activeStatus') != ['1']:
        return False

    class GestureForm(GroupSignReply):
        visible_form = False

        def handle_starttag(self, tag, attrs):
            super().handle_starttag(tag, attrs)
            if (tag == 'div' and dict(attrs).get('id') == 'gesturepwd'
                    and self.stack and not self.stack[-1][1]):
                self.visible_form = True

    page = GestureForm()
    page.feed(str(text or '')[:2_000_000])
    return page.visible_form and '/widget/sign/group/pcStuSignGroupController/signIn?activeId=' in str(text)


def page_context(text, active_id):
    """Only literal fields; never execute an upstream page's JavaScript."""
    text = str(text or '')[:2_000_000]
    parser = Inputs()
    parser.feed(text)
    values = dict(parser.values)
    for key in ('activeId', 'activePrimaryId', 'courseId', 'classId', 'ifopenAddress', 'openCheckFaceFlag'):
        values.setdefault(key, []).extend(re.findall(
            r'''\b''' + key + r'''["']?\s*[:=]\s*["']?(\d{1,30})(?!\d)''', text))
    active_ids = values.get('activeId', []) + values.get('activePrimaryId', [])
    active_ids = [value for value in active_ids if ID.fullmatch(value)]
    if active_ids and any(value != active_id for value in active_ids):
        raise QrProtocolError('activity_mismatch', '预签到页面与二维码活动不一致，未发送签到。')
    result = {'matchedActivity': bool(active_ids)}
    for key in ('courseId', 'classId'):
        found = {value for value in values.get(key, []) if ID.fullmatch(value)}
        if len(found) > 1:
            raise QrProtocolError('context_mismatch', '预签到页面包含冲突课程或班级，未发送签到。')
        if found:
            result[key] = found.pop()
    for key in ('ifopenAddress', 'openCheckFaceFlag'):
        found = set(values.get(key, []))
        if found & {'1'}:
            result[key] = '1'
        elif found & {'0'}:
            result[key] = '0'
    return result


def activity_context(payload, active_id):
    if not isinstance(payload, dict) or payload.get('result') in (False, 0, '0', 'false'):
        return {}
    record = payload.get('data')
    if not isinstance(record, dict):
        return {}
    if record.get('id') is not None and str(record['id']) != active_id:
        raise QrProtocolError('activity_mismatch', '活动详情与二维码活动不一致，未发送签到。')
    result = {}
    for key in ('courseId', 'classId'):
        value = str(record.get(key, ''))
        if ID.fullmatch(value):
            result[key] = value
    for key in ('ifopenAddress', 'openCheckFaceFlag'):
        if str(record.get(key)) in ('0', '1'):
            result[key] = str(record[key])
    return result


def merge_context(base, found):
    for key in ('courseId', 'classId'):
        if base.get(key) and found.get(key) and base[key] != found[key]:
            raise QrProtocolError('context_mismatch', '课程或班级上下文不一致，未发送签到。')
        if found.get(key):
            base[key] = found[key]
    for key in ('ifopenAddress', 'openCheckFaceFlag'):
        if found.get(key) == '1' or key not in base and key in found:
            base[key] = found[key]
    return base
