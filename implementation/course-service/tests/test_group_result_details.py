"""Bounded, visible result evidence for synthetic official group sign pages."""
from pathlib import Path
import json
import sys

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from qr_protocol import group_sign_details, group_sign_status


SUCCESS_PAGE = '<div class="zsign_success"></div><h1>签到成功</h1>'


@pytest.mark.parametrize(('message', 'reason'), [
    ('活动已结束', 'activity_ended'),
    ('签到已结束', 'activity_ended'),
    ('无此签到活动', 'activity_missing'),
    ('活动不存在', 'activity_missing'),
    ('二维码已失效', 'qr_expired'),
    ('二维码已过期', 'qr_expired'),
    ('二维码不正确', 'qr_invalid'),
    ('签到失败', 'sign_failed'),
])
def test_visible_rejection_has_a_specific_reason(message, reason):
    details = group_sign_details('<h1>' + message + '</h1>', 200)

    assert details['status'] == 'rejected'
    assert details['reasonCode'] == reason
    assert reason in details['matchedReasons']
    assert details['successMarker'] is False


def test_specific_expiry_reason_takes_priority_over_generic_failure():
    details = group_sign_details('<h1>签到失败</h1><p>二维码已过期，请重新扫描。</p>', 200)

    assert details['status'] == 'rejected'
    assert details['reasonCode'] == 'qr_expired'
    assert set(details['matchedReasons']) == {'qr_expired', 'sign_failed'}


def test_success_requires_both_a_visible_marker_and_the_exact_heading():
    details = group_sign_details(SUCCESS_PAGE, 200)

    assert details['status'] == 'reported_success'
    assert details['reasonCode'] == 'success'
    assert details['headingKind'] == 'success'
    assert details['successMarker'] is True
    assert group_sign_status(SUCCESS_PAGE, 200) == details['status']


@pytest.mark.parametrize('html', [
    '<h1>签到成功</h1>',
    '<div class="zsign_success"></div><p>签到成功</p>',
    '<div class="zsign_success"></div><h1>签到成功，请返回</h1>',
    '<script>"zsign_success 签到成功"</script>',
])
def test_incomplete_success_evidence_remains_unknown(html):
    details = group_sign_details(html, 200)

    assert details['status'] == 'unknown'
    assert details['reasonCode'] == 'unrecognized_result'


def test_http_failure_cannot_report_success():
    details = group_sign_details(SUCCESS_PAGE, 500)

    assert details['status'] == 'http_error'
    assert details['reasonCode'] == 'http_error'
    assert group_sign_status(SUCCESS_PAGE, 500) == details['status']


@pytest.mark.parametrize('failure', ['二维码已过期', '签到失败', '活动已结束'])
def test_visible_success_and_failure_evidence_is_conflicting(failure):
    html = SUCCESS_PAGE + '<footer>' + failure + '</footer>'
    details = group_sign_details(html, 200)

    assert details['status'] == 'unknown'
    assert details['reasonCode'] == 'conflicting_result'
    assert details['successMarker'] is True
    assert group_sign_status(html, 200) == 'unknown'


@pytest.mark.parametrize('hidden', [
    '<section hidden><h1>二维码已过期</h1></section>',
    '<section aria-hidden="true"><div><p>签到失败</p></div></section>',
    '<section style="display: none"><div><p>活动已结束</p></div></section>',
    '<section style="visibility: hidden"><div><p>二维码不正确</p></div></section>',
    '<script>"签到失败 二维码已过期"</script>',
    '<style>.example::after { content: "签到失败"; }</style>',
])
def test_hidden_rejection_templates_do_not_override_visible_success(hidden):
    details = group_sign_details(hidden + SUCCESS_PAGE, 200)

    assert details['status'] == 'reported_success'
    assert details['reasonCode'] == 'success'
    assert not set(details['matchedReasons']) & {
        'activity_ended', 'qr_expired', 'qr_invalid', 'sign_failed'}


@pytest.mark.parametrize('html', [
    '<section hidden>' + SUCCESS_PAGE + '</section>',
    '<section aria-hidden="true"><div>' + SUCCESS_PAGE + '</div></section>',
    '<section style="display:none"><div>' + SUCCESS_PAGE + '</div></section>',
    '<section style="visibility:hidden"><div>' + SUCCESS_PAGE + '</div></section>',
])
def test_hidden_success_evidence_cannot_claim_success(html):
    details = group_sign_details(html, 200)

    assert details['status'] == 'unknown'
    assert details['reasonCode'] == 'unrecognized_result'
    assert details['successMarker'] is False


@pytest.mark.parametrize(('message', 'status'), [
    ('同一设备不允许重复签到', 'device_conflict'),
    ('请进行人脸验证', 'face_required'),
    ('需要定位', 'location_required'),
    ('请完成验证码', 'requires_validation'),
])
def test_required_official_verification_remains_a_safe_status(message, status):
    details = group_sign_details('<h1>' + message + '</h1>', 200)

    assert details['status'] == status
    assert group_sign_status('<h1>' + message + '</h1>', 200) == status


def test_diagnostics_are_codes_and_never_echo_untrusted_page_content():
    sensitive = 'synthetic-private-ticket-7f45'
    html = ('<h1>二维码已过期</h1><p>' + sensitive + '</p>'
            '<input name="token" value="' + sensitive + '">'
            '<script>const ticket = "' + sensitive + '";</script>')
    details = group_sign_details(html, 200)

    assert set(details) == {
        'status', 'reasonCode', 'headingKind', 'matchedReasons', 'successMarker'}
    assert details['headingKind'] in {'success', 'already_signed', 'failure', 'other', 'none'}
    assert isinstance(details['matchedReasons'], list)
    assert all(isinstance(reason, str) for reason in details['matchedReasons'])
    assert isinstance(details['successMarker'], bool)
    assert details['reasonCode'] == 'qr_expired'
    assert set(details['matchedReasons']) == {'qr_expired'}
    encoded = json.dumps(details, ensure_ascii=False)
    assert sensitive not in encoded
    assert '<' not in encoded
    assert '二维码' not in encoded
