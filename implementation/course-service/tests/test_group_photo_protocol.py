"""Synthetic group-photo contracts; no account data or network operations."""
from pathlib import Path
import sys
from urllib.parse import urlencode, urlsplit, parse_qs

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from group_photo_protocol import (REDIRECT_FIELDS, photo_redirect, photo_form_ready,
                                  parse_photo_members, photo_record)


FIELDS = {**REDIRECT_FIELDS, 'activeId': '300', 'chatId': '400', 'uid': '101'}
BIND = '<script>var acId="300";var uid="101";</script>'
FORM = (BIND + '<div onclick="newOpenPhoto()"><div class="message_camera"></div></div>'
        '<a id="signButton" onclick="sign()">签到</a>'
        '<script>$.ajax({url: "/sign/stuSignajax", type: "post", data: {}});</script>')
OBJECT = 'a' * 32
SUCCESS = '<div class="zsign_success"><h1>签到成功</h1></div>'
PHOTO = '<div class="zsign_photo"><img objId="' + OBJECT + '"></div>'
RECORD = BIND + SUCCESS + PHOTO


def test_observed_relative_and_absolute_redirects_preserve_exact_bound_fields():
    relative = '/sign/preStuSign?' + urlencode(FIELDS)
    for target in (relative, 'https://mobilelearn.chaoxing.com' + relative):
        result = photo_redirect(target, '300', '400', '101')
        assert urlsplit(result).scheme == 'https'
        assert urlsplit(result).hostname == 'mobilelearn.chaoxing.com'
        assert parse_qs(urlsplit(result).query, keep_blank_values=True) == {k: [v] for k, v in FIELDS.items()}


@pytest.mark.parametrize('change', [
    {'activeId': '301'}, {'uid': '102'}, {'chatId': '401'}, {'classId': '1'},
    {'appType': '15'}, {'code': 'unexpected'}, {'extra': 'value'}, {'tid': ''},
])
def test_redirect_rejects_cross_context_or_unobserved_parameters(change):
    with pytest.raises(ValueError):
        photo_redirect('/sign/preStuSign?' + urlencode({**FIELDS, **change}), '300', '400', '101')


@pytest.mark.parametrize('target', [
    'https://example.org/sign/preStuSign?',
    'http://mobilelearn.chaoxing.com/sign/preStuSign?',
    'https://user@mobilelearn.chaoxing.com/sign/preStuSign?',
    'https://mobilelearn.chaoxing.com:444/sign/preStuSign?',
    '/sign/stuSignajax?', '/sign/preStuSign?uid=101&',
])
def test_redirect_never_follows_an_unverified_route(target):
    with pytest.raises(ValueError):
        photo_redirect(target + urlencode(FIELDS), '300', '400', '101')


@pytest.mark.parametrize('suffix', ['#', '&uid=101', '&broken=%Q', '&extra=%0A'])
def test_redirect_rejects_fragments_duplicate_keys_and_invalid_encoding(suffix):
    with pytest.raises(ValueError):
        photo_redirect('/sign/preStuSign?' + urlencode(FIELDS) + suffix, '300', '400', '101')


def test_camera_form_requires_bound_identity_and_observed_post():
    assert photo_form_ready(FORM, '300', '101')
    assert photo_form_ready(FORM + '<script>var acId="";var uid="101";</script>', '300', '101')
    assert not photo_form_ready(FORM, '301', '101')
    assert not photo_form_ready(FORM, '300', '102')


@pytest.mark.parametrize('html', [
    FORM.replace('class="message_camera"', 'class="message_camera" hidden'),
    FORM.replace('id="signButton"', 'id="signButton" style="display:none"'),
    FORM.replace('onclick="sign()"', 'onclick="otherAction()"'),
    FORM.replace('type: "post"', 'type: "get"'),
    FORM.replace('/sign/stuSignajax', '/pptSign/stuSignajax'),
    FORM.replace('/sign/stuSignajax', 'https://example.org/sign/stuSignajax'),
    FORM.replace(BIND, '<script>//var acId="300";var uid="101";\n</script>'),
    FORM.replace(BIND, '<p>var acId="300";var uid="101";</p>'),
    FORM + '<script>var uid="102";</script>',
    FORM + '<script>var uid="invalid";</script>',
    FORM + '<script>var acId=calculateId();</script>',
])
def test_incomplete_hidden_or_conflicting_forms_are_not_ready(html):
    assert not photo_form_ready(html, '300', '101')


@pytest.mark.parametrize('extra', [
    '<p>活动已结束</p>', '<p>需要人脸验证</p>', '<p>需要定位</p>', '<p>请输入验证码</p>',
    '<input name="ifopenAddress" value="1">', '<input id="openCheckFaceFlag" value="1">',
    '<script>var ifopenAddress="1";</script>',
    '<script>var openCheckFaceFlag=true;</script>',
    '<script>var rules={"ifopenAddress":1};</script>',
])
def test_photo_form_rejects_additional_requirements_or_an_ended_activity(extra):
    assert not photo_form_ready(FORM + extra, '300', '101')


def test_complete_membership_contains_self_and_returns_only_unique_other_ids():
    payload = {'result': 1, 'data': {'allCount': 3, 'list': [{'puid': 101}, {'puid': '102'}, {'puid': 103}]}}
    assert parse_photo_members(payload, '101') == ['102', '103']
    assert parse_photo_members({'result': 1, 'data': {'allCount': 1, 'list': [{'puid': '101'}]}}, 101) == []


@pytest.mark.parametrize('payload', [
    None, {}, {'result': 0}, {'result': True, 'data': {'allCount': 1, 'list': [{'puid': '101'}]}},
    {'result': 1, 'data': {'allCount': 2, 'list': [{'puid': '101'}]}},
    {'result': 1, 'data': {'allCount': '1', 'list': [{'puid': '101'}]}},
    {'result': 1, 'data': {'allCount': 1, 'list': [{'puid': '102'}]}},
    {'result': 1, 'data': {'allCount': 2, 'list': [{'puid': 101}, {'puid': '101'}]}},
    {'result': 1, 'data': {'allCount': 2, 'list': [{'puid': '101'}, {'puid': True}]}},
    {'result': 1, 'data': {'allCount': 2, 'list': [{'puid': '101'}, {'puid': 0}]}},
    {'result': 1, 'data': {'allCount': 2, 'list': [{'puid': '101'}, {'puid': '00102'}]}},
    {'result': 1, 'data': {'allCount': 2, 'list': [{'puid': '101'}, None]}},
])
def test_incomplete_or_invalid_membership_is_not_a_source_of_photos(payload):
    with pytest.raises(ValueError):
        parse_photo_members(payload, '101')


def test_record_requires_visible_success_and_returns_only_its_visible_photo():
    assert photo_record(RECORD, 200, '300', '101') == {'signed': True, 'objectId': OBJECT}
    assert photo_record(RECORD + PHOTO, 200, '300', '101') == {'signed': True, 'objectId': OBJECT}
    assert photo_record(RECORD + '<img objId="' + 'b' * 32 + '">', 200, '300', '101')['objectId'] == OBJECT


@pytest.mark.parametrize('html,status', [
    (RECORD, 500), (RECORD.replace('var uid="101"', 'var uid="102"'), 200),
    (RECORD.replace('var acId="300"', 'var acId="301"'), 200),
    (RECORD.replace(BIND, ''), 200), (BIND + PHOTO, 200),
    (BIND + '<section hidden>' + SUCCESS + '</section>' + PHOTO, 200),
    (BIND + '<script>' + SUCCESS + '</script>' + PHOTO, 200),
    (RECORD + '<p>活动已结束</p>', 200),
    (RECORD + '<script>var uid="different";</script>', 200),
])
def test_unbound_failed_or_hidden_success_never_exposes_photo(html, status):
    assert photo_record(html, status, '300', '101') == {'signed': False, 'objectId': None}


@pytest.mark.parametrize('photo', [
    '', '<section hidden>' + PHOTO + '</section>',
    PHOTO.replace('<img ', '<img style="display:none" '),
    PHOTO.replace('objId="' + OBJECT + '"', 'objId="bad-id"'),
    PHOTO + PHOTO.replace(OBJECT, 'b' * 32), '<script>' + PHOTO + '</script>',
])
def test_success_without_one_usable_photo_is_not_a_photo_reference(photo):
    assert photo_record(BIND + SUCCESS + photo, 200, '300', '101') == {'signed': True, 'objectId': None}
