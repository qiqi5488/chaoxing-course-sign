"""Bounded QR structure for logs; never return parameter values or arbitrary text."""
from urllib.parse import parse_qsl, urlsplit

from qr_protocol import OFFICIAL_DOMAINS

KNOWN_HOSTS = frozenset((*OFFICIAL_DOMAINS, 'mobilelearn.chaoxing.com',
                         'mooc1.chaoxing.com', 'mooc1-api.chaoxing.com', 'passport2.chaoxing.com'))
ROUTES = frozenset(('widget', 'sign', 'e', 'pc', 'web', 'active', 'group', 'chat',
                    'signin', 'qr', 'api', 'v2', 'index', 'html'))
PARAMETERS = frozenset(('id', 'aid', 'activeId', 'activePrimaryId', 'enc', 'enc2',
                        'c', 'Code', 'code', 'courseId', 'classId', 'type', 'activeType',
                        'groupId', 'chatId', 'rcode', 'uid', 'fid', 'puid', 't', 'v',
                        'source', 'appType', 'isTeacherViewOpen', 'general', 'ls', 'lv'))


def qr_diagnostics(qr_text):
    result = {'kind': 'other', 'scheme': 'other', 'host': 'other',
              'path': '[redacted]', 'parameters': []}
    if not isinstance(qr_text, str) or len(qr_text) > 8192:
        return result
    raw = qr_text.strip()
    if raw.startswith('SIGNIN:'):
        result['kind'] = 'signin'
        query = raw[len('SIGNIN:'):]
    else:
        try:
            url = urlsplit(raw)
            host = (url.hostname or '').lower()
        except ValueError:
            return result
        if url.scheme in ('http', 'https'):
            result.update(kind='url', scheme=url.scheme)
        official = next((d for d in OFFICIAL_DOMAINS if host == d or host.endswith('.' + d)), None)
        if official and result['kind'] == 'url':
            result['host'] = host if host in KNOWN_HOSTS else '[redacted].' + official
            segments = url.path.split('/')[1:13]
            result['path'] = '/' + '/'.join(s if s in ROUTES else '[redacted]' for s in segments)
        query = url.query
    try:
        pairs = parse_qsl(query, keep_blank_values=True, max_num_fields=128)
    except ValueError:
        result['parameterLimitExceeded'] = True
        return result
    result['parameters'] = [{'name': key if key in PARAMETERS else '[redacted]', 'length': len(value)}
                            for key, value in pairs]
    return result
