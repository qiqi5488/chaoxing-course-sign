"""Same numeric IDs in independent group/course services cannot share a verdict."""
from pathlib import Path
import sys

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from vault import Vault


@pytest.mark.parametrize('scope', ['course', 'group'])
@pytest.mark.parametrize('status', ['pending', 'reported_success', 'outcome_unknown', 'need_context'])
def test_blocking_results_are_isolated_by_activity_scope(tmp_path, scope, status):
    vault = Vault(tmp_path)
    token, _ = vault.create()
    job = {'requestId': 'test-request', 'accountId': 'test-account', 'activeId': '300',
           'activityScope': scope, 'status': status, 'submitted': True}
    vault.save_job(token, job, 'test-fingerprint')
    assert vault.prior_job(token, 'test-account', '300', scope) == job
    other_scope = 'group' if scope == 'course' else 'course'
    assert vault.prior_job(token, 'test-account', '300', other_scope) is None
    assert vault.get_job(token, 'test-request')[0]['activeId'] == '300'


@pytest.mark.parametrize('status', ['reported_success', 'need_context'])
def test_legacy_course_job_without_scope_remains_findable(tmp_path, status):
    vault = Vault(tmp_path)
    token, _ = vault.create()
    job = {'requestId': 'legacy-request', 'accountId': 'test-account', 'activeId': '300',
           'status': status}
    vault.save_job(token, job, 'legacy-fingerprint')
    assert vault.prior_job(token, 'test-account', '300') == job
    assert vault.prior_job(token, 'test-account', '300', 'group') is None


@pytest.mark.parametrize('scope', ['course', 'group'])
@pytest.mark.parametrize('submitted', [True, False, None, 'missing'])
def test_need_context_only_allows_a_new_attempt_when_explicitly_not_submitted(tmp_path, scope, submitted):
    vault = Vault(tmp_path)
    token, _ = vault.create()
    job = {'requestId': 'context-request', 'accountId': 'test-account', 'activeId': '300',
           'activityScope': scope, 'status': 'need_context'}
    if submitted != 'missing':
        job['submitted'] = submitted
    vault.save_job(token, job, 'context-fingerprint')
    expected = None if submitted is False else job
    assert vault.prior_job(token, 'test-account', '300', scope) == expected
    other_scope = 'group' if scope == 'course' else 'course'
    assert vault.prior_job(token, 'test-account', '300', other_scope) is None
    assert vault.prior_job(token, 'other-account', '300', scope) is None
    assert vault.get_job(token, 'context-request')[0] == job
