"""Validate the two observed official group-sign redirect steps without fetching."""
import re
from urllib.parse import parse_qsl, urlencode, urljoin, urlsplit

ORIGIN = 'https://mobilelearn.chaoxing.com'
STEPS = {
    'entry': ('/sign/preSign', {'activeId', 'chatId', 'uid'}),
    'mobile': ('/widget/sign/group/pcStuSignGroupController/preSign', {'activeId'}),
}


def group_redirect(location, stage, active_id, puid):
    """Return a fixed-origin URL only when the redirect preserves QR/user identity."""
    reason = '群签到跳转不符合官方流程。'
    control = lambda value: any(ord(c) < 32 or 127 <= ord(c) <= 159 for c in value)
    try:
        active_id, puid = str(active_id), str(puid)
        if (stage not in STEPS or not re.fullmatch(r'[1-9][0-9]*', active_id)
                or not re.fullmatch(r'[0-9]+', puid)):
            raise ValueError()
        if (not isinstance(location, str) or not location or location != location.strip()
                or control(location) or '#' in location
                or re.search(r'%(?![0-9a-fA-F]{2})', location)):
            raise ValueError()
        url = urlsplit(urljoin(ORIGIN + '/', location))
        path, required = STEPS[stage]
        if (url.scheme != 'https' or url.hostname != 'mobilelearn.chaoxing.com'
                or url.port not in (None, 443) or url.username is not None
                or url.password is not None or url.fragment or url.path != path):
            raise ValueError()
        pairs = parse_qsl(url.query, keep_blank_values=True, strict_parsing=True,
                          errors='strict', max_num_fields=8)
        fields = dict(pairs)
        if (len(fields) != len(pairs) or set(fields) != required
                or any(control(k) or control(v) for k, v in pairs)
                or fields['activeId'] != active_id):
            raise ValueError()
        if stage == 'entry' and (fields['uid'] != puid or not fields['chatId'].strip()
                                 or len(fields['chatId']) > 512):
            raise ValueError()
        return ORIGIN + path + '?' + urlencode(pairs)
    except (ValueError, TypeError, UnicodeError):
        raise ValueError(reason) from None
