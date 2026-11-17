"""Synthetic ordinary-sign reads; unknown flags must not bypass verification."""
from copy import deepcopy
from pathlib import Path
import sys

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from ordinary_protocol import (OrdinaryProtocolError, ordinary_validation_request,
                               ordinary_validation_required)


@pytest.mark.parametrize('active_id', ['300', 300])
def test_validation_read_has_fixed_official_target_and_observed_empty_puid(active_id):
    assert ordinary_validation_request(active_id) == {
        'method': 'GET',
        'url': 'https://mobilelearn.chaoxing.com/widget/sign/pcStuSignController/checkIfValidate',
        'params': {'DB_STRATEGY': 'PRIMARY_KEY', 'STRATEGY_PARA': 'activeId',
                   'activeId': '300', 'puid': ''},
    }


@pytest.mark.parametrize('active_id', [None, '', True, False, 0, -1, 1.0, '01',
    '1&uid=private', 'https://evil.example/300', ' 300', '300\n', '1' * 31])
def test_invalid_activity_cannot_change_target_query_or_identity(active_id):
    with pytest.raises(OrdinaryProtocolError) as error:
        ordinary_validation_request(active_id)
    assert error.value.code == 'invalid_activity'
    assert 'private' not in str(error.value)
    assert 'evil' not in str(error.value)


@pytest.mark.parametrize(('flag', 'expected'), [
    (0, False), (1, True), (False, False), (True, True), ('0', False), ('1', True),
])
def test_explicit_validation_flag_is_a_requirement_not_a_sign_result(flag, expected):
    payload = {'result': flag, 'msg': 'private upstream text'}
    before = deepcopy(payload)
    assert ordinary_validation_required(payload) is expected
    assert payload == before


@pytest.mark.parametrize('payload', [None, [], 'success', {'success': True}, {},
    {'result': None}, {'result': ''}, {'result': -1}, {'result': 2},
    {'result': 1.0}, {'result': 'true'}, {'result': []},
    {'result': {'status': 0}}, {'error': 'private-password private-cookie'}])
def test_unknown_or_failed_validation_schema_never_defaults_to_no_challenge(payload):
    with pytest.raises(OrdinaryProtocolError) as error:
        ordinary_validation_required(payload)
    assert error.value.code == 'validation_unrecognized'
    assert 'private' not in str(error.value)
