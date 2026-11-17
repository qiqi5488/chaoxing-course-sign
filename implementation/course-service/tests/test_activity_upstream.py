"""Exercise the real gateway with controlled HTTP, without private accounts."""
from pathlib import Path
import sys
from unittest.mock import patch

import pytest
import requests

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))
import upstream
from chaoxing_direct import ChaoxingDirectClient
from test_upstream import InMemoryAdapter, PROFILE, IDENTITY, COOKIES, SIGN, PRESIGN
from test_activity_protocol import COURSE, DETAIL as ACTIVITY_DETAIL

DETAIL = {'data': {**ACTIVITY_DETAIL['data'], 'clazzid': '500'}}


def gateway(responses):
    session = requests.Session()
    adapter = InMemoryAdapter(responses)
    session.mount('https://', adapter)
    direct = ChaoxingDirectClient('offline-activity-tests', session=session)
    return direct, adapter


def test_course_queries_merge_sources_verify_detail_and_never_submit():
    direct, adapter = gateway([{'body': PROFILE}, {'body': {'data': {'activeList': [{'id': 300}]}}},
        {'body': {'activeList': [{'id': 300, 'nameOne': '签到'}]}}, {'body': DETAIL}])
    with patch.object(upstream, 'ChaoxingDirectClient', return_value=direct):
        result = upstream.UpstreamGateway().get_course_activities(COOKIES, IDENTITY, COURSE)
    assert result['status'] == 'ok'
    assert len(result['activities']) == 1
    assert result['activities'][0]['autoEligible']
    assert all(r.method == 'GET' and 'stuSign' not in r.url for r in adapter.sent)


def test_unavailable_sources_raise_instead_of_claiming_no_signs():
    direct, _ = gateway([{'body': PROFILE}, {'status': 403}, {'body': {'error': 'private-cookie'}}])
    with patch.object(upstream, 'ChaoxingDirectClient', return_value=direct), pytest.raises(upstream.UpstreamError) as error:
        upstream.UpstreamGateway().get_course_activities(COOKIES, IDENTITY, COURSE)
    assert error.value.code == 'activities_unavailable'
    assert 'private' not in str(error.value)


def test_partial_details_remain_visible_and_manual():
    direct, _ = gateway([{'body': PROFILE}, {'body': {'data': {'activeList': [{'id': 300}]}}},
                         {'status': 503}, {'body': {'data': {}}}])
    with patch.object(upstream, 'ChaoxingDirectClient', return_value=direct):
        result = upstream.UpstreamGateway().get_course_activities(COOKIES, IDENTITY, COURSE)
    assert result['status'] == 'partial'
    assert result['detailErrors'] == 1
    assert result['activities'][0]['kind'] == 'unknown'
    assert not result['activities'][0]['autoEligible']


def test_identity_mismatch_stops_before_activity_read():
    direct, adapter = gateway([{'body': PROFILE}])
    with patch.object(upstream, 'ChaoxingDirectClient', return_value=direct), pytest.raises(upstream.UpstreamError):
        upstream.UpstreamGateway().get_course_activities(COOKIES, {**IDENTITY, 'uid': '999'}, COURSE)
    assert len(adapter.sent) == 1


@pytest.mark.parametrize('detail,validation', [
    ({'data': {**DETAIL['data'], 'ifphoto': 1}}, None),
    ({'data': {**DETAIL['data'], 'openCheckFaceFlag': 1}}, None),
    ({'data': {**DETAIL['data'], 'nowTime': None}}, None),
    (DETAIL, {'result': None}),
    (DETAIL, {'result': 'private-unrecognized-flag'}),
    (DETAIL, '<html>unrecognized validation response</html>'),
])
def test_automatic_sign_rechecks_rule_changes_and_validation(detail, validation):
    responses = [{'body': PROFILE}, {'body': detail}]
    if validation:
        responses.append({'body': validation})
    direct, adapter = gateway(responses)
    with patch.object(upstream, 'ChaoxingDirectClient', return_value=direct):
        result = upstream.UpstreamGateway().sign(COOKIES, IDENTITY, {**SIGN, '_autoSign': True})
    assert result['status'] == 'requires_manual'
    assert result['submitted'] is False
    assert not any('stuSign' in request.url for request in adapter.sent)


def test_ordinary_automatic_sign_rechecks_details_then_direct_protocol():
    direct, adapter = gateway([{'body': PROFILE}, {'body': DETAIL}, {'body': {'result': 0}}, {'body': 'success'}])
    with patch.object(upstream, 'ChaoxingDirectClient', return_value=direct):
        result = upstream.UpstreamGateway().sign(COOKIES, IDENTITY, {**SIGN, '_autoSign': True})
    assert result['status'] == 'reported_success'
    assert result['submitted'] is True
    assert len(adapter.sent) == 4
    assert 'checkIfValidate' in adapter.sent[2].url
    assert '/pptSign/stuSignajax' in adapter.sent[3].url
    assert all('/newsign/preSign' not in request.url for request in adapter.sent)


def test_confirmed_ended_detail_is_cached_but_list_is_still_polled():
    listing = {'data': {'activeList': [{'id': 300, 'startTime': 1_800_000_000_000, 'status': 2}]}}
    ended = {'data': {**DETAIL['data'], 'nowTime': 1_800_002_000_000}}
    direct, adapter = gateway([{'body': PROFILE}, {'body': listing}, {'body': {'result': 1, 'status': 0, 'activeList': []}},
        {'body': ended}, {'body': PROFILE}, {'body': listing}, {'body': {'activeList': []}}])
    instance = upstream.UpstreamGateway()
    with patch.object(upstream, 'ChaoxingDirectClient', return_value=direct):
        first = instance.get_course_activities(COOKIES, IDENTITY, COURSE)
        second = instance.get_course_activities(COOKIES, IDENTITY, COURSE)
    assert first['activities'] == second['activities']
    assert second['activities'][0]['status'] == 'ended'
    assert sum('getPPTActiveInfo' in request.url for request in adapter.sent) == 1
    assert sum('student/activelist' in request.url for request in adapter.sent) == 2


def test_list_change_invalidates_ended_cache_and_latest_activity_is_read_first():
    listing = {'data': {'activeList': [{'id': 300, 'startTime': 1_800_000_000_000, 'status': 2}]}}
    changed = {'data': {'activeList': [{'id': 300, 'startTime': 1_800_000_000_000, 'status': 1}]}}
    ended = {'data': {**DETAIL['data'], 'nowTime': 1_800_002_000_000}}
    direct, adapter = gateway([{'body': PROFILE}, {'body': listing}, {'body': {'activeList': []}}, {'body': ended},
        {'body': PROFILE}, {'body': changed}, {'body': {'activeList': []}}, {'body': DETAIL}])
    instance = upstream.UpstreamGateway()
    with patch.object(upstream, 'ChaoxingDirectClient', return_value=direct):
        instance.get_course_activities(COOKIES, IDENTITY, COURSE)
        result = instance.get_course_activities(COOKIES, IDENTITY, COURSE)
    assert result['activities'][0]['status'] == 'active'
    assert sum('getPPTActiveInfo' in request.url for request in adapter.sent) == 2


def test_ended_cache_isolates_identity_course_and_list_stamp_and_expires():
    instance = upstream.UpstreamGateway()
    candidate = {'activeId': '300', 'startTime': 100, 'endTime': 200, 'status': 2}
    ended = {'status': 'ended', 'requirements': []}
    with patch.object(upstream.time, 'monotonic', return_value=100):
        instance._ended_activity(IDENTITY, COURSE, candidate, ended)
        assert instance._ended_activity(IDENTITY, COURSE, candidate) == ended
        assert instance._ended_activity({**IDENTITY, 'uid': '999'}, COURSE, candidate) is None
        assert instance._ended_activity(IDENTITY, {**COURSE, 'classId': '999'}, candidate) is None
        assert instance._ended_activity(IDENTITY, {**COURSE, 'courseId': '999'}, candidate) is None
        assert instance._ended_activity(IDENTITY, COURSE, {**candidate, 'status': 1}) is None
        assert instance._ended_activity(IDENTITY, COURSE, {**candidate, 'activeId': '301'}) is None
        instance._ended_activity(IDENTITY, COURSE, {**candidate, 'activeId': '302'}, {'status': 'active'})
        assert instance._ended_activity(IDENTITY, COURSE, {**candidate, 'activeId': '302'}) is None
    with patch.object(upstream.time, 'monotonic', return_value=401):
        assert instance._ended_activity(IDENTITY, COURSE, candidate) is None


def test_activity_details_are_requested_newest_first():
    listing = {'data': {'activeList': [{'id': 300, 'startTime': 100}, {'id': 301, 'startTime': 200}]}}
    direct, adapter = gateway([{'body': PROFILE}, {'body': listing}, {'body': {'activeList': []}},
        {'body': {'data': {**DETAIL['data'], 'activeId': '301'}}}, {'body': DETAIL}])
    with patch.object(upstream, 'ChaoxingDirectClient', return_value=direct):
        result = upstream.UpstreamGateway().get_course_activities(COOKIES, IDENTITY, COURSE)
    assert [row['activeId'] for row in result['activities']] == ['301', '300']
    detail_urls = [request.url for request in adapter.sent if 'getPPTActiveInfo' in request.url]
    assert detail_urls[0].endswith('activeId=301')
