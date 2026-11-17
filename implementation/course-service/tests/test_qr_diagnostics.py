"""Synthetic QR inputs only; diagnostics must not echo arbitrary user text."""
import json
from pathlib import Path
import sys

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from qr_diagnostics import qr_diagnostics


def test_official_group_route_and_parameter_names_are_visible_without_values():
    result = qr_diagnostics('https://mobilelearn.chaoxing.com/widget/group/sign?id=300&enc=private-token')
    assert result['path'] == '/widget/group/sign'
    assert result['host'] == 'mobilelearn.chaoxing.com'
    assert result['parameters'] == [{'name': 'id', 'length': 3}, {'name': 'enc', 'length': 13}]
    assert 'private-token' not in json.dumps(result)


@pytest.mark.parametrize('qr', [
    'https://private-user:private-password@mobilelearn.chaoxing.com/widget/private-token/sign?enc=private-enc#private-fragment',
    'https://private-subdomain.chaoxing.com/private-path?private-key=private-value',
    'https://private-host.example/private-path?enc=private-enc',
    'SIGNIN:id=300&enc=private-enc&private-key=private-value',
    'https://mobilelearn.chaoxing.com/widget/%70rivate-token?enc=private-enc&private%0Akey=private-value',
])
def test_no_credentials_values_unknown_names_hosts_or_paths_leak(qr):
    assert 'private' not in json.dumps(qr_diagnostics(qr))


@pytest.mark.parametrize('qr', [None, '', 'not a URL', 'https://[bad',
                                'SIGNIN:enc=%zz', 'SIGNIN:enc=%ff', 'x' * 8193])
def test_malformed_inputs_never_break_diagnostics(qr):
    assert isinstance(qr_diagnostics(qr)['parameters'], list)


def test_parameter_output_is_bounded():
    result = qr_diagnostics('SIGNIN:' + '&'.join('enc=secret' for _ in range(129)))
    assert result['parameters'] == []
    assert result['parameterLimitExceeded'] is True
