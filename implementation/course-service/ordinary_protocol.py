"""Pure protocol for the observed course ordinary-sign validation read.

Consumer evidence: qdfree.13b79d4e.js:3558-3636,5859-5925 and
moreqdfree.c081f31e.js:5705-5876 in analysis/course/readable/static/js.
Those pages read checkIfValidate before an ordinary GET stuSignajax. Their
external getValidate solver is intentionally not reproduced here.

A zero validation flag is not attendance success. Callers must verify account
identity, active ordinary rules, HTTP/session status, and reserve the actual
submission separately. This module performs no I/O and generates no tokens.
"""
import re


MOBILELEARN_ORIGIN = 'https://mobilelearn.chaoxing.com'
VALIDATION_PATH = '/widget/sign/pcStuSignController/checkIfValidate'
ACTIVITY_ID = re.compile(r'[1-9][0-9]{0,29}')


class OrdinaryProtocolError(ValueError):
    def __init__(self, code, message):
        self.code = code
        super().__init__(message)


def ordinary_validation_request(active_id):
    """Return only the fixed official read, never a caller-provided target.

    The observed consumer leaves ``puid`` empty and authenticates with its
    cookie jar. Do not conflate it with the separate group passport parameter.
    """
    if type(active_id) not in (str, int) or not ACTIVITY_ID.fullmatch(str(active_id)):
        raise OrdinaryProtocolError('invalid_activity', '普通签到缺少有效的活动标识。')
    return {
        'method': 'GET',
        'url': MOBILELEARN_ORIGIN + VALIDATION_PATH,
        'params': {'DB_STRATEGY': 'PRIMARY_KEY', 'STRATEGY_PARA': 'activeId',
                   'activeId': str(active_id), 'puid': ''},
    }


def ordinary_validation_required(payload):
    """Recognize the explicit 0/1 validation flag; fail closed on other schemas.

    Transport errors, redirects and expired-session pages must be handled by
    the caller before JSON reaches this pure parser. No response text or
    arbitrary upstream message is exposed through an exception.
    """
    if isinstance(payload, dict) and 'result' in payload:
        value = payload['result']
        if type(value) in (bool, int) and value in (0, 1):
            return bool(value)
        if type(value) is str and value in ('0', '1'):
            return value == '1'
    raise OrdinaryProtocolError('validation_unrecognized', '学习通未返回可确认的普通签到验证状态，未发送签到。')
