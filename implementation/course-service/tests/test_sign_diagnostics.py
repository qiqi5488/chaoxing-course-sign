"""Private failed-page evidence must be encrypted, bounded and owner isolated."""
from pathlib import Path
import sqlite3
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from vault import Vault


def test_diagnostic_is_encrypted_separate_from_public_job_and_isolated(tmp_path):
    vault = Vault(tmp_path)
    token, _ = vault.create()
    other, _ = vault.create()
    job = {'requestId': 'request-one', 'accountId': 'account-one', 'activeId': '300', 'status': 'rejected'}
    vault.save_job(token, job, 'fingerprint')
    private = {'body': '<h1>签到失败 private-ticket-body</h1>', 'httpStatus': 200}
    vault.save_diagnostic(token, job['requestId'], private)
    assert vault.get_diagnostic(token, job['requestId']) == private
    assert vault.get_diagnostic(other, job['requestId']) is None
    assert vault.get_job(token, job['requestId'])[0] == job
    with sqlite3.connect(vault.path) as db:
        blob = db.execute('SELECT ciphertext FROM sign_diagnostics').fetchone()[0]
    assert b'private-ticket-body' not in blob
    assert Vault(tmp_path).get_diagnostic(token, job['requestId']) == private
    vault.remove_jobs(token, ['account-one'])
    assert vault.get_diagnostic(token, job['requestId']) is None


def test_only_twenty_latest_private_diagnostics_are_retained_per_owner(tmp_path):
    vault = Vault(tmp_path)
    token, _ = vault.create()
    for index in range(21):
        job = {'requestId': f'request-{index}', 'accountId': 'account-one', 'activeId': str(index + 1), 'status': 'rejected'}
        vault.save_job(token, job, 'fingerprint')
        vault.save_diagnostic(token, job['requestId'], {'body': f'private-{index}'})
    with sqlite3.connect(vault.path) as db:
        assert db.execute('SELECT count(*) FROM sign_diagnostics').fetchone()[0] == 20
    assert vault.get_diagnostic(token, 'request-0') is None
    assert vault.get_diagnostic(token, 'request-20') == {'body': 'private-20'}
