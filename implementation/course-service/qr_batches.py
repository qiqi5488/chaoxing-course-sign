"""Bounded background QR submissions, isolated CookieJars and persistent receipts."""
from concurrent.futures import ThreadPoolExecutor
import hashlib
import json
import logging
import threading

from upstream import UpstreamError
from vault import iso

WAITING = {'queued', 'running'}
SUCCEEDED = {'reported_success', 'already_signed'}
ATTENTION = {'requires_validation', 'invalid_challenge', 'location_required', 'face_required',
             'need_context', 'unknown', 'outcome_unknown', 'http_error', 'interrupted', 'previous_result_unknown'}
LOGGER = logging.getLogger('uvicorn.error.course.qr')


def receipt(batch):
    """Construct summaries from per-account upstream results, never a blanket success."""
    result = json.loads(json.dumps(batch))
    statuses = [item['status'] for item in result['items']]
    completed = sum(s not in WAITING for s in statuses)
    succeeded = sum(s in SUCCEEDED for s in statuses)
    attention = sum(s in ATTENTION for s in statuses)
    result['summary'] = {'total': len(statuses), 'completed': completed, 'succeeded': succeeded,
                         'failed': completed - succeeded - attention, 'needsAttention': attention}
    return result


class QrBatchManager:
    def __init__(self, vault, gateway):
        self.vault, self.gateway = vault, gateway
        self.pool = ThreadPoolExecutor(max_workers=4, thread_name_prefix='course-qr')
        self.closing = threading.Event()
        vault.interrupt_batches()

    def start(self, token, batch, qr_text, location):
        for item in batch['items']:
            self.pool.submit(self._run, token, batch['batchId'], item['accountId'], qr_text, location)

    def close(self):
        self.closing.set()
        self.pool.shutdown(wait=True, cancel_futures=True)

    def _save_item(self, token, batch_id, account_id, result):
        batch, fingerprint = self.vault.get_batch(token, batch_id)
        if not batch or batch['status'] == 'interrupted':
            return
        item = next((item for item in batch['items'] if item['accountId'] == account_id), None)
        if not item:
            return
        item.update(result)
        if all(item['status'] not in WAITING for item in batch['items']):
            batch['status'], batch['completedAt'] = 'completed', iso()
        else:
            batch['status'] = 'running'
        self.vault.save_batch(token, batch, fingerprint)

    def _run(self, token, batch_id, account_id, qr_text, location):
        if self.closing.is_set():
            return
        # Network calls deliberately run outside the workspace lock so polling,
        # revocation and other accounts remain responsive.
        with self.vault.lock(token):
            state = self.vault.load(token)
            batch, _ = self.vault.get_batch(token, batch_id)
            if state is None or not batch or batch['status'] == 'interrupted':
                return
            row = next((a for a in state['accounts'] if a['id'] == account_id), None)
            if not row or row['status'] != 'active' or not row['_cookies']:
                self._save_item(token, batch_id, account_id, {'status': 'login_required', 'message': '账号已移除或授权失效，未提交。', 'submitted': False})
                return
            previous = self.vault.prior_job(token, account_id, batch['activeId'], batch.get('activityScope', 'course'))
            if previous:
                success = previous['status'] in SUCCEEDED
                self._save_item(token, batch_id, account_id, {
                    'status': previous['status'] if success else 'previous_result_unknown',
                    'message': '该活动已有成功回执，未重复提交。' if success else '该活动已有结果未明的请求，请先在学习通核实。',
                    'requestId': previous['requestId'], 'submitted': False})
                return
            request_id = 'qr_' + hashlib.sha256((batch_id + ':' + account_id).encode()).hexdigest()
            fingerprint = hashlib.sha256(json.dumps({'accountId': account_id, 'qrText': qr_text, 'location': location}, sort_keys=True).encode()).hexdigest()
            job = {'requestId': request_id, 'accountId': account_id, 'activeId': batch['activeId'],
                   'kind': 'qr',
                   'activityScope': batch.get('activityScope', 'course'),
                   'status': 'pending', 'message': '正在向学习通提交，请勿重复扫码。', 'createdAt': iso()}
            self.vault.save_job(token, job, fingerprint)
            self._save_item(token, batch_id, account_id, {'status': 'running', 'message': '正在处理', 'requestId': request_id})
            cookies = row['_cookies']
            identity = row['_identity']
            grant_at = row['grant']['grantedAt']
        diagnostic = None
        try:
            raw = self.gateway.sign_qr(cookies, identity, qr_text, location=location)
            allowed = ('status', 'message', 'phase', 'submitted', 'httpStatus', 'challenge', 'reasonCode', 'diagnostics')
            result = {key: raw[key] for key in allowed if key in raw}
            if not isinstance(result.get('message'), str) or result.get('status') not in SUCCEEDED | ATTENTION | {'device_conflict', 'rejected', 'login_required', 'session_expired'}:
                raise ValueError('Unrecognized upstream result')
            result['completedAt'] = iso()
            changed_cookies = raw.get('cookies')
            diagnostic = raw.get('_diagnostic')
            if result.get('diagnostics'):
                LOGGER.info('qr_result %s', json.dumps({
                    'request': hashlib.sha256(request_id.encode()).hexdigest()[:12],
                    'httpStatus': result.get('httpStatus'), **result['diagnostics'],
                }, ensure_ascii=True))
            session_invalid = result['status'] in ('login_required', 'session_expired')
        except UpstreamError as error:
            session_invalid = error.code in ('session_expired', 'session_missing', 'login_required', 'invalid_session')
            result = {'status': 'outcome_unknown' if error.details.get('submitted') else
                      ('login_required' if session_invalid else error.code if error.code in ATTENTION else 'rejected'),
                      'message': str(error), 'phase': error.details.get('phase', 'presign'),
                      'submitted': bool(error.details.get('submitted')), 'completedAt': iso()}
            changed_cookies = None
        except Exception:
            result = {'status': 'outcome_unknown', 'message': '未能确认学习通处理结果，请先核实考勤记录；系统未自动重试。', 'completedAt': iso()}
            changed_cookies = None
            session_invalid = False
        with self.vault.lock(token):
            state = self.vault.load(token)
            batch, _ = self.vault.get_batch(token, batch_id)
            if state is None or not batch or batch['status'] == 'interrupted':
                return
            current = next((a for a in state['accounts'] if a['id'] == account_id), None)
            # Revocation, removal or a later login must never be undone by an old worker.
            same_grant = current and current['status'] == 'active' and current['grant'] and current['grant']['grantedAt'] == grant_at
            if same_grant:
                if session_invalid:
                    current.update(status='expired', _cookies=[], sessionExpiresAt=iso())
                elif changed_cookies is not None:
                    current['_cookies'] = changed_cookies
                self.vault.save(token, state)
            job.update(result)
            self.vault.save_job(token, job, fingerprint)
            if diagnostic is not None:
                self.vault.save_diagnostic(token, request_id, diagnostic)
            self._save_item(token, batch_id, account_id, result)
