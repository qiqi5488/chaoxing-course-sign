"""Redirect validation must preserve identity and never accept arbitrary hosts."""
from pathlib import Path
import sys

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from group_routes import group_redirect

ENTRY = '/sign/preSign?activeId=300&chatId=server-chat&uid=123'
MOBILE = '/widget/sign/group/pcStuSignGroupController/preSign?activeId=300'
ORIGIN = 'https://mobilelearn.chaoxing.com'


@pytest.mark.parametrize(('location', 'stage', 'expected'), [
    (ORIGIN + ENTRY, 'entry', ORIGIN + ENTRY),
    (MOBILE, 'mobile', ORIGIN + MOBILE),
    ('https://mobilelearn.chaoxing.com:443' + ENTRY, 'entry', ORIGIN + ENTRY),
])
def test_observed_redirect_shapes(location, stage, expected):
    assert group_redirect(location, stage, '300', '123') == expected


@pytest.mark.parametrize('location', [
    ENTRY.replace('activeId=300', 'activeId=301'), ENTRY.replace('uid=123', 'uid=124'),
    ENTRY + '&uid=123', ENTRY + '&%75id=123', ENTRY + '&redirect=https://evil.example',
    ENTRY.replace('server-chat', ''), ENTRY.replace('server-chat', '%20'),
    ENTRY.replace('server-chat', 'a' * 513), ENTRY.replace('server-chat', '%0Asecret'),
    ENTRY.replace('server-chat', '%C2%85secret'), ENTRY.replace('server-chat', '%ZZ'),
    ENTRY.replace('server-chat', '%FF'), ENTRY + '&x=1&y=2&z=3&a=4&b=5&c=6',
    ORIGIN + ENTRY + '#secret', ORIGIN + ENTRY + '#', ORIGIN + ENTRY + '\n',
    'https://evil.example' + ENTRY, 'http://mobilelearn.chaoxing.com' + ENTRY,
    'https://mobilelearn.chaoxing.com.evil.example' + ENTRY, '//127.0.0.1' + ENTRY,
    'https://user@mobilelearn.chaoxing.com' + ENTRY,
    'https://@mobilelearn.chaoxing.com' + ENTRY,
    'https://mobilelearn.chaoxing.com:8443' + ENTRY,
    '/other/preSign?activeId=300&chatId=server-chat&uid=123', MOBILE,
])
def test_rejects_untrusted_ambiguous_or_identity_changing_entry(location):
    with pytest.raises(ValueError, match='^群签到跳转不符合官方流程。$'):
        group_redirect(location, 'entry', '300', '123')


@pytest.mark.parametrize(('location', 'stage', 'active_id', 'puid'), [
    (MOBILE + '&uid=123', 'mobile', '300', '123'),
    (MOBILE + '&activeId=300', 'mobile', '300', '123'),
    (MOBILE.replace('300', '301'), 'mobile', '300', '123'),
    (ENTRY, 'unknown', '300', '123'), (ENTRY, 'entry', '0', '123'),
    (ENTRY, 'entry', '300', 'user'), (None, 'entry', '300', '123'),
])
def test_rejects_wrong_mobile_shape_or_invalid_expected_identity(location, stage, active_id, puid):
    with pytest.raises(ValueError):
        group_redirect(location, stage, active_id, puid)
