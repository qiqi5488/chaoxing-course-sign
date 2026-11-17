"""Observed source=1 QR contract and conservative official HTML classification."""
from pathlib import Path
import sys

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from qr_protocol import parse_qr_target, QrProtocolError, group_sign_status


@pytest.mark.parametrize('extra', ['', '&enc=unrelated-course-ticket'])
def test_group_qr_keeps_code_as_code_without_manufacturing_enc(extra):
    assert parse_qr_target('SIGNIN:aid=300&source=1&Code=ABCD1234' + extra) == {
        'activeId': '300', 'source': '1', 'code': 'ABCD1234'}


@pytest.mark.parametrize('text', [
    'SIGNIN:aid=300&source=1',
    'SIGNIN:aid=300&source=1&enc=course-ticket',
    'SIGNIN:aid=300&source=1&Code=',
    'SIGNIN:aid=0&source=1&Code=ABCD1234',
    'SIGNIN:aid=300&source=1&Code=ABCD1234&source=1',
    'SIGNIN:aid=300&source=1&Code=ABCD1234&c=other',
    'SIGNIN:aid=300&source=1&Code=bad%0Acode',
    'SIGNIN:aid=300&source=1&Code=bad%20code',
    'SIGNIN:aid=300&source=2&Code=ABCD1234',
])
def test_incomplete_ambiguous_or_unsupported_group_qr_is_not_accepted(text):
    with pytest.raises(QrProtocolError):
        parse_qr_target(text)


@pytest.mark.parametrize(('html', 'status', 'expected'), [
    ('<div class="zsign_success zsign_hook"></div><h1>签到成功</h1>', 200, 'reported_success'),
    ('<div class="zsign_success"></div><h1>已签到</h1>', 200, 'already_signed'),
    ('<h1>签到成功</h1>', 200, 'unknown'),
    ('<script>"zsign_success 签到成功"</script>', 200, 'unknown'),
    ('<div class="zsign_success"></div><h1>签到成功</h1>', 500, 'http_error'),
    ('<h1>活动已结束</h1>', 200, 'rejected'),
    ('<h1>无此签到活动.</h1>', 200, 'rejected'),
    ('<h1>二维码已失效</h1>', 200, 'rejected'),
    ('<h1>同一设备不允许重复签到</h1>', 200, 'device_conflict'),
    ('<h1>请进行人脸验证</h1>', 200, 'face_required'),
    ('<h1>需要定位</h1>', 200, 'location_required'),
    ('<h1>请完成验证码</h1>', 200, 'requires_validation'),
])
def test_only_recognized_visible_official_result_pages_claim_success(html, status, expected):
    assert group_sign_status(html, status) == expected
