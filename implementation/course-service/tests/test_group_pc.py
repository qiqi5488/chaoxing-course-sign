"""Observed PC group records require activity, passport identity, and visible evidence."""
from pathlib import Path
import sys

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from qr_protocol import group_pc_details

CONTEXT = '<input type="hidden" id="activeId" value="300"><input type="hidden" id="puid" value="101">'
SUCCESS = '<div class="qd_Success">签到成功</div>'
PHOTO = ('<div class="qd_Success padtop50"><p class="Photo_p">此签到需要手机拍照</p>'
         '<p class="Photo_p2">请使用手机App<br clear="none" />进行签到</p></div>')
ACTIVE = '<input type="hidden" id="activeStatus" value="1">'


def test_pc_record_success_requires_both_observed_hidden_identities():
    details = group_pc_details(CONTEXT + SUCCESS, 200, '300', '101')
    assert details['status'] == 'reported_success'
    assert details['reasonCode'] == 'record_signed'
    assert details['successMarker'] is True


@pytest.mark.parametrize('context', [
    CONTEXT.replace('value="300"', 'value="301"'),
    CONTEXT.replace('value="101"', 'value="100"'),
    CONTEXT + '<input id="puid" value="999">',
])
def test_activity_or_passport_mismatch_cannot_claim_success(context):
    details = group_pc_details(context + SUCCESS, 200, '300', '101')
    assert (details['status'], details['reasonCode']) == ('unknown', 'context_mismatch')


@pytest.mark.parametrize('context', [
    '', '<input id="activeId" value="300">', '<input id="puid" value="101">',
    CONTEXT + '<input id="puid" value="101">',
])
def test_missing_or_ambiguous_page_identity_is_not_a_signed_record(context):
    details = group_pc_details(context + SUCCESS, 200, '300', '101')
    assert (details['status'], details['reasonCode']) == ('need_context', 'page_identity_missing')


@pytest.mark.parametrize('evidence', [
    '<div>签到成功</div>', '<div class="qd_Success">等待签到</div>',
    '<script>"qd_Success 签到成功"</script>',
    '<section hidden>' + SUCCESS + '</section>',
    '<section style="display:none">' + SUCCESS + '</section>',
    '<form><button>签到</button></form>',
])
def test_waiting_form_or_incomplete_visible_evidence_needs_confirmation(evidence):
    details = group_pc_details(CONTEXT + evidence, 200, '300', '101')
    assert (details['status'], details['reasonCode']) == ('need_context', 'group_form_pending')


def test_http_failure_or_conflicting_visible_rejection_cannot_report_success():
    assert group_pc_details(CONTEXT + SUCCESS, 500, '300', '101')['status'] == 'http_error'
    details = group_pc_details(CONTEXT + SUCCESS + '<p>二维码已过期</p>', 200, '300', '101')
    assert (details['status'], details['reasonCode']) == ('unknown', 'conflicting_result')


def test_bound_active_photo_requirement_is_not_a_success_or_generic_form():
    details = group_pc_details(CONTEXT + ACTIVE + PHOTO, 200, '300', '101')
    assert (details['status'], details['reasonCode']) == ('photo_required', 'photo_required')
    assert details['successMarker'] is False


@pytest.mark.parametrize('context,reason', [
    (CONTEXT.replace('value="300"', 'value="301"'), 'context_mismatch'),
    (CONTEXT.replace('value="101"', 'value="102"'), 'context_mismatch'),
    ('', 'page_identity_missing'),
    ('<input id="activeId" value="300">', 'page_identity_missing'),
    (CONTEXT + '<input id="puid" value="101">', 'page_identity_missing'),
])
def test_photo_requirement_needs_unambiguous_matching_page_identity(context, reason):
    details = group_pc_details(context + ACTIVE + PHOTO, 200, '300', '101')
    assert details['status'] not in ('photo_required', 'reported_success', 'already_signed')
    assert details['reasonCode'] == reason


@pytest.mark.parametrize('photo', [
    '<script>' + PHOTO + '</script>',
    '<section hidden>' + PHOTO + '</section>',
    '<section style="display:none">' + PHOTO + '</section>',
    PHOTO.replace('class="Photo_p"', 'class="Photo_p" aria-hidden="true"'),
    '<h2>此签到需要手机拍照</h2>',
])
def test_hidden_or_non_requirement_photo_text_does_not_select_photo_flow(photo):
    details = group_pc_details(CONTEXT + ACTIVE + photo, 200, '300', '101')
    assert (details['status'], details['reasonCode']) == ('need_context', 'group_form_pending')


@pytest.mark.parametrize('active', ['', ACTIVE.replace('value="1"', 'value="2"'), ACTIVE + ACTIVE])
def test_missing_ended_or_ambiguous_active_state_cannot_select_photo_flow(active):
    details = group_pc_details(CONTEXT + active + PHOTO, 200, '300', '101')
    assert (details['status'], details['reasonCode']) == ('need_context', 'group_form_pending')


def test_photo_requirement_does_not_override_failure_or_conflicting_success():
    base = CONTEXT + ACTIVE + PHOTO
    assert group_pc_details(base, 500, '300', '101')['status'] == 'http_error'
    ended = group_pc_details(base + '<p>活动已结束</p>', 200, '300', '101')
    assert (ended['status'], ended['reasonCode']) == ('rejected', 'activity_ended')
    face = group_pc_details(base + '<p>需要人脸验证</p>', 200, '300', '101')
    assert face['status'] == 'face_required'
    success = group_pc_details(base + SUCCESS, 200, '300', '101')
    assert (success['status'], success['reasonCode']) == ('unknown', 'conflicting_result')
