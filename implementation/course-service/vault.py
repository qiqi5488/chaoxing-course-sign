"""Encrypted, browser-isolated session storage. Never stores login passwords."""
from base64 import b64decode
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
import hashlib
import hmac
import json
import os
from pathlib import Path
import secrets
import sqlite3
import threading
import time

from Crypto.Cipher import AES

DAY = 86400


@dataclass(frozen=True)
class WorkspaceRef:
    """Internal workspace address for workers; never a browser credential."""
    digest: str


def iso(timestamp=None):
    return datetime.fromtimestamp(time.time() if timestamp is None else timestamp, timezone.utc).isoformat().replace('+00:00', 'Z')


def timestamp(value):
    return datetime.fromisoformat(value.replace('Z', '+00:00')).timestamp() if value else None


def empty_state():
    return {'accounts': [], 'defaultAccountId': None, 'events': []}


def event(state, account, action):
    state['events'].insert(0, {'id': secrets.token_hex(12), 'accountName': account['name'], 'action': action, 'at': iso()})
    del state['events'][20:]


def repair(state):
    now = time.time()
    for account in state['accounts']:
        if account['grant']:
            # Legacy local retention limits must not invalidate a live upstream
            # session. Consent lasts until upstream expiry or explicit revocation.
            account['grant']['days'] = None
            account['grant']['expiresAt'] = None
        session_expired = account['sessionExpiresAt'] and timestamp(account['sessionExpiresAt']) <= now
        if session_expired:
            account['status'] = 'expired'
            account['_cookies'] = []
        elif not account['grant']:
            account['status'] = 'unauthorized'
            account['_cookies'] = []
    if not any(a['id'] == state['defaultAccountId'] and a['status'] == 'active' for a in state['accounts']):
        state['defaultAccountId'] = next((a['id'] for a in state['accounts'] if a['status'] == 'active'), None)


def public_state(state):
    fields = ('id', 'name', 'maskedIdentifier', 'school', 'status', 'grant', 'lastLogin', 'sessionExpiresAt', 'color')
    return {'accounts': [{k: a[k] for k in fields} for a in state['accounts']],
            'defaultAccountId': state['defaultAccountId'], 'events': state['events'], 'mode': 'live'}


class Vault:
    def __init__(self, data_dir):
        directory = Path(data_dir).resolve()
        directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.path = directory / 'sessions.sqlite3'
        encoded = os.environ.get('COURSE_SESSION_KEY')
        key_path = directory / 'session.key'
        if encoded:
            try:
                self.key = b64decode(encoded, validate=True)
            except ValueError:
                raise RuntimeError('COURSE_SESSION_KEY must be a base64-encoded 32-byte key.') from None
        else:
            if not key_path.exists():
                if self.path.exists():
                    raise RuntimeError('Session key is missing. Restore the original key before starting.')
                try:
                    with os.fdopen(os.open(key_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600), 'wb') as stream:
                        stream.write(secrets.token_bytes(32))
                except FileExistsError:
                    pass
            self.key = key_path.read_bytes()
        if len(self.key) != 32:
            raise RuntimeError('Session key must contain exactly 32 bytes.')
        self._lock_guard = threading.Lock()
        self._locks = {}
        self._monitor_revisions = {}
        with self.connection() as db:
            db.execute('PRAGMA journal_mode=WAL')
            db.executescript('''
                CREATE TABLE IF NOT EXISTS vaults (owner TEXT PRIMARY KEY, expires REAL NOT NULL, ciphertext BLOB NOT NULL);
                CREATE TABLE IF NOT EXISTS sign_jobs (
                    owner TEXT NOT NULL REFERENCES vaults(owner) ON DELETE CASCADE,
                    request_id TEXT NOT NULL, account_id TEXT NOT NULL, active_id TEXT NOT NULL,
                    fingerprint TEXT NOT NULL, ciphertext BLOB NOT NULL,
                    PRIMARY KEY (owner, request_id));
                CREATE TABLE IF NOT EXISTS qr_batches (
                    owner TEXT NOT NULL REFERENCES vaults(owner) ON DELETE CASCADE,
                    batch_id TEXT NOT NULL, fingerprint TEXT NOT NULL, created REAL NOT NULL,
                    ciphertext BLOB NOT NULL, PRIMARY KEY (owner, batch_id));
                CREATE TABLE IF NOT EXISTS sign_diagnostics (
                    owner TEXT NOT NULL, request_id TEXT NOT NULL, created REAL NOT NULL,
                    ciphertext BLOB NOT NULL, PRIMARY KEY (owner, request_id),
                    FOREIGN KEY (owner, request_id) REFERENCES sign_jobs(owner, request_id) ON DELETE CASCADE);
                CREATE TABLE IF NOT EXISTS course_monitors (
                    owner TEXT PRIMARY KEY REFERENCES vaults(owner) ON DELETE CASCADE,
                    enabled INTEGER NOT NULL, ciphertext BLOB NOT NULL);
            ''')
            # Keep the old column for a non-destructive schema migration. Zero
            # now means persistent storage; no age-based workspace cleanup runs.
            db.execute('UPDATE vaults SET expires=0 WHERE expires<>0')

    @contextmanager
    def connection(self):
        db = sqlite3.connect(self.path, timeout=15)
        db.row_factory = sqlite3.Row
        db.execute('PRAGMA foreign_keys=ON')
        try:
            with db:
                yield db
        finally:
            db.close()

    @staticmethod
    def owner(token):
        if isinstance(token, WorkspaceRef):
            return token.digest
        return hashlib.sha256(token.encode()).hexdigest()

    def load_monitor(self, token):
        owner = self.owner(token)
        with self.connection() as db:
            row = db.execute('SELECT ciphertext FROM course_monitors WHERE owner=?', (owner,)).fetchone()
        return self.decrypt(row['ciphertext'], owner) if row else {'accounts': {}, 'tasks': []}

    def save_monitor(self, token, monitor):
        owner = self.owner(token)
        enabled = any(config.get('enabled') for config in monitor['accounts'].values())
        with self.connection() as db:
            db.execute('INSERT INTO course_monitors VALUES (?,?,?) ON CONFLICT(owner) DO UPDATE '
                       'SET enabled=excluded.enabled,ciphertext=excluded.ciphertext',
                       (owner, int(enabled), self.encrypt(monitor, owner)))
        self._changed(owner)

    def _changed(self, owner):
        with self._lock_guard:
            self._monitor_revisions[owner] = self._monitor_revisions.get(owner, 0) + 1

    def monitor_revision(self, token):
        with self._lock_guard:
            return self._monitor_revisions.get(self.owner(token), 0)

    def monitored_workspaces(self):
        with self.connection() as db:
            rows = db.execute('SELECT owner FROM course_monitors WHERE enabled=1').fetchall()
        return [WorkspaceRef(row['owner']) for row in rows]

    def lock(self, token):
        owner = self.owner(token)
        with self._lock_guard:
            return self._locks.setdefault(owner, threading.RLock())

    def csrf(self, token):
        return hmac.new(self.key, b'csrf:' + token.encode(), hashlib.sha256).hexdigest()

    def encrypt(self, data, owner):
        nonce = secrets.token_bytes(12)
        cipher = AES.new(self.key, AES.MODE_GCM, nonce=nonce)
        cipher.update(owner.encode())
        encrypted, tag = cipher.encrypt_and_digest(json.dumps(data, ensure_ascii=False, separators=(',', ':')).encode())
        return nonce + tag + encrypted

    def decrypt(self, data, owner):
        cipher = AES.new(self.key, AES.MODE_GCM, nonce=data[:12])
        cipher.update(owner.encode())
        return json.loads(cipher.decrypt_and_verify(data[28:], data[12:28]))

    def create(self):
        token = secrets.token_urlsafe(32)
        owner = self.owner(token)
        state = empty_state()
        with self.connection() as db:
            db.execute('INSERT INTO vaults VALUES (?,?,?)', (owner, 0, self.encrypt(state, owner)))
        return token, state

    def load(self, token):
        owner = self.owner(token)
        with self.connection() as db:
            row = db.execute('SELECT * FROM vaults WHERE owner=?', (owner,)).fetchone()
        if not row:
            return None
        state = self.decrypt(row['ciphertext'], owner)
        repair(state)
        return state

    def save(self, token, state):
        repair(state)
        owner = self.owner(token)
        with self.connection() as db:
            db.execute('UPDATE vaults SET ciphertext=? WHERE owner=?', (self.encrypt(state, owner), owner))
        self._changed(owner)

    def get_job(self, token, request_id):
        owner = self.owner(token)
        with self.connection() as db:
            row = db.execute('SELECT * FROM sign_jobs WHERE owner=? AND request_id=?', (owner, request_id)).fetchone()
        return (self.decrypt(row['ciphertext'], owner), row['fingerprint']) if row else (None, None)

    def account_jobs(self, token, account_id):
        owner = self.owner(token)
        with self.connection() as db:
            rows = db.execute('SELECT ciphertext FROM sign_jobs WHERE owner=? AND account_id=?',
                              (owner, account_id)).fetchall()
        return [self.decrypt(row['ciphertext'], owner) for row in rows]

    def prior_job(self, token, account_id, active_id, activity_scope='course'):
        owner = self.owner(token)
        active_id = 'group:' + active_id if activity_scope == 'group' else active_id
        with self.connection() as db:
            rows = db.execute('SELECT ciphertext FROM sign_jobs WHERE owner=? AND account_id=? AND active_id=?', (owner, account_id, active_id)).fetchall()
        for row in reversed(rows):
            job = self.decrypt(row['ciphertext'], owner)
            # A group entry can sign before its page supplies enough identity
            # context to confirm the outcome. Only an explicit submitted=False
            # permits another attempt, including for legacy receipts lacking it.
            # Photo POSTs in either scope also require explicit non-submission.
            if job['status'] in ('pending', 'reported_success', 'already_signed') or (
                    job['status'] in ('outcome_unknown', 'unknown', 'http_error', 'need_context')
                    and job.get('submitted') is not False) or (
                    (job.get('kind') == 'photo' or job.get('resolvedKind') == 'photo')
                    and job.get('submitted') is not False):
                return job
        return None

    def save_job(self, token, job, fingerprint):
        owner = self.owner(token)
        active_id = 'group:' + job['activeId'] if job.get('activityScope') == 'group' else job['activeId']
        with self.connection() as db:
            db.execute('INSERT INTO sign_jobs VALUES (?,?,?,?,?,?) ON CONFLICT(owner,request_id) DO UPDATE SET ciphertext=excluded.ciphertext',
                       (owner, job['requestId'], job['accountId'], active_id, fingerprint, self.encrypt(job, owner)))

    def save_diagnostic(self, token, request_id, diagnostic):
        owner = self.owner(token)
        with self.connection() as db:
            db.execute('INSERT OR REPLACE INTO sign_diagnostics VALUES (?,?,?,?)',
                       (owner, request_id, time.time(), self.encrypt(diagnostic, owner)))
            db.execute('DELETE FROM sign_diagnostics WHERE owner=? AND request_id IN '
                       '(SELECT request_id FROM sign_diagnostics WHERE owner=? ORDER BY created DESC LIMIT -1 OFFSET 20)',
                       (owner, owner))

    def get_diagnostic(self, token, request_id):
        owner = self.owner(token)
        with self.connection() as db:
            row = db.execute('SELECT ciphertext FROM sign_diagnostics WHERE owner=? AND request_id=?',
                             (owner, request_id)).fetchone()
        return self.decrypt(row['ciphertext'], owner) if row else None

    def remove_jobs(self, token, ids):
        with self.connection() as db:
            db.executemany('DELETE FROM sign_jobs WHERE owner=? AND account_id=?', [(self.owner(token), i) for i in ids])

    def get_batch(self, token, batch_id):
        owner = self.owner(token)
        with self.connection() as db:
            row = db.execute('SELECT * FROM qr_batches WHERE owner=? AND batch_id=?', (owner, batch_id)).fetchone()
        return (self.decrypt(row['ciphertext'], owner), row['fingerprint']) if row else (None, None)

    def batches(self, token, limit=5):
        owner = self.owner(token)
        with self.connection() as db:
            rows = db.execute('SELECT ciphertext FROM qr_batches WHERE owner=? ORDER BY created DESC LIMIT ?', (owner, limit)).fetchall()
        return [self.decrypt(row['ciphertext'], owner) for row in rows]

    def save_batch(self, token, batch, fingerprint):
        owner = self.owner(token)
        with self.connection() as db:
            db.execute('INSERT INTO qr_batches VALUES (?,?,?,?,?) ON CONFLICT(owner,batch_id) DO UPDATE SET ciphertext=excluded.ciphertext',
                       (owner, batch['batchId'], fingerprint, time.time(), self.encrypt(batch, owner)))

    def interrupt_batches(self):
        """A restart never silently replays an unconfirmed upstream submission."""
        with self.connection() as db:
            rows = db.execute('SELECT * FROM qr_batches').fetchall()
            for row in rows:
                owner = row['owner']
                batch = self.decrypt(row['ciphertext'], owner)
                if batch['status'] not in ('queued', 'running'):
                    continue
                for item in batch['items']:
                    if item['status'] not in ('queued', 'running'):
                        continue
                    running = item['status'] == 'running'
                    item.update(status='outcome_unknown' if running else 'interrupted',
                                message='服务已重启，请先在学习通核实结果。' if running else '服务重启前尚未开始，未自动补发。',
                                submitted=None if running else False)
                batch['status'] = 'interrupted'
                batch['completedAt'] = iso()
                db.execute('UPDATE qr_batches SET ciphertext=? WHERE owner=? AND batch_id=?',
                           (self.encrypt(batch, owner), owner, batch['batchId']))
            jobs = db.execute('SELECT * FROM sign_jobs').fetchall()
            for row in jobs:
                job = self.decrypt(row['ciphertext'], row['owner'])
                if job['status'] == 'pending':
                    job.update(status='outcome_unknown', message='服务重启前未获得最终回执，请先在学习通核实。', completedAt=iso())
                    db.execute('UPDATE sign_jobs SET ciphertext=? WHERE owner=? AND request_id=?',
                               (self.encrypt(job, row['owner']), row['owner'], row['request_id']))
