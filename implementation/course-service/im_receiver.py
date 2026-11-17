"""Supervise official MiniCore receivers; credentials never enter argv or disk."""
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import threading

import requests

from group_notifications import _activity, _course_activity, _loads, _Malformed


ROOT = Path(__file__).resolve().parent
TERMINAL = {206, 207, 214, 216, 217, 220}
MESSAGES = {
    'connecting': '正在连接学习通实时消息。',
    'connected': '实时接收中，签到通知到达后立即核验。',
    'reconnecting': '消息连接中断，正在重连；低频补漏继续。',
    'blocked': '消息连接被设备或账号规则中断；低频补漏继续，重新开关监控可重试。',
    'error': '实时消息暂不可用，正在重试；低频补漏继续。',
}


def socket_url():
    """Discover the same TLS service as the official app without account data."""
    with requests.Session() as session:
        session.trust_env = False
        response = session.get('https://rs.easemob.com/easemob/server.json',
            params={'app_key': 'cx-dev#cxstudy'}, timeout=(5, 12), allow_redirects=False)
        if response.status_code != 200 or len(response.content) > 262144:
            raise ValueError('im_discovery_failed')
        payload = response.json()
    for host in payload.get('msync-wx', {}).get('hosts', []):
        domain = host.get('domain', '')
        if (host.get('protocol') == 'https' and str(host.get('port')) == '443'
                and isinstance(domain, str)
                and re.fullmatch(r'[a-z0-9-]+(?:\.[a-z0-9-]+)*\.easemob\.com', domain)):
            return 'wss://' + domain + '/websocket'
    raise ValueError('im_discovery_failed')


def notice_candidate(message):
    """Only received group attendance attachments become internal candidates."""
    if not isinstance(message, dict) or message.get('type') != 'notice':
        return None
    if message.get('chatType') != 'groupChat':
        return None
    target, message_id = message.get('to'), message.get('id')
    if (not isinstance(target, str) or not re.fullmatch(r'[A-Za-z0-9_-]{1,128}', target)
            or not isinstance(message_id, str) or not 0 < len(message_id) <= 128
            or any(ord(c) < 33 or ord(c) > 126 for c in message_id)):
        return None
    attachment = message.get('attachment')
    if isinstance(attachment, str):
        try:
            attachment = _loads(attachment)
        except _Malformed:
            return None
    candidate = _course_activity(attachment) or _activity(attachment)
    if not candidate or candidate['activityScope'] == 'group' and candidate['chatId'] != target:
        return None
    candidate['noticeKey'] = hashlib.sha256((target + ':' + message_id).encode()).hexdigest()
    return candidate


class Listener:
    def __init__(self, spec, gateway, callback):
        self.spec, self.gateway, self.callback = spec, gateway, callback
        self.stop = threading.Event()
        self.process = None
        self.thread = threading.Thread(target=self.run, name='course-im-receiver', daemon=True)

    def emit(self, status):
        self.callback(self.spec, {'type': 'state', 'status': status, 'message': MESSAGES[status]})

    def close(self):
        self.stop.set()
        process = self.process
        if process and process.poll() is None:
            process.terminate()

    def run(self):
        failures = 0
        while not self.stop.is_set():
            self.emit('connecting' if not failures else 'reconnecting')
            connected, blocked = False, False
            try:
                executable = shutil.which('node')
                if not executable:
                    raise RuntimeError('node_unavailable')
                credentials = self.gateway.get_im_credentials(self.spec['cookies'], self.spec['identity'])
                credentials.update(url=socket_url(), deviceId=self.spec['deviceId'])
                if self.stop.is_set():
                    return
                process = subprocess.Popen([executable, str(ROOT / 'im-receiver.cjs')], cwd=ROOT,
                    stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                    text=True, encoding='utf-8', bufsize=1,
                    creationflags=subprocess.CREATE_NO_WINDOW if os.name == 'nt' else 0)
                self.process = process
                process.stdin.write(json.dumps(credentials, separators=(',', ':')) + '\n')
                process.stdin.flush()
                del credentials
                while not self.stop.is_set():
                    line = process.stdout.readline(102401)
                    if not line:
                        break
                    if len(line) > 102400 or not line.endswith('\n'):
                        raise ValueError('im_event_too_large')
                    message = _loads(line, 102400)
                    if message.get('type') == 'connected':
                        connected, failures = True, 0
                        self.emit('connected')
                    elif message.get('type') == 'disconnected':
                        self.emit('reconnecting')
                    elif message.get('type') == 'error':
                        if message.get('code') in TERMINAL:
                            blocked = True
                            self.emit('blocked')
                            break
                    else:
                        candidate = notice_candidate(message)
                        if candidate:
                            self.callback(self.spec, {'type': 'notice', 'candidate': candidate})
            except Exception:
                if not self.stop.is_set():
                    self.emit('error')
            finally:
                process = self.process
                if process:
                    if process.poll() is None:
                        process.terminate()
                    try:
                        process.wait(timeout=3)
                    except subprocess.TimeoutExpired:
                        process.kill()
                        process.wait(timeout=3)
                    for stream in (process.stdin, process.stdout):
                        if stream:
                            stream.close()
                self.process = None
            if blocked:
                return
            if connected and not self.stop.is_set():
                self.emit('reconnecting')
            failures += 1
            if self.stop.wait(min(300, 5 * 2 ** min(failures - 1, 6))):
                return


class RealtimeReceivers:
    def __init__(self, gateway, callback, factory=Listener):
        self.gateway, self.callback, self.factory = gateway, callback, factory
        self.listeners = {}

    def sync(self, specs):
        wanted = {spec['key']: spec for spec in specs}
        for key, listener in list(self.listeners.items()):
            if (key not in wanted or wanted[key]['stamp'] != listener.spec['stamp']
                    or wanted[key].get('epoch') != listener.spec.get('epoch')):
                listener.close()
                del self.listeners[key]
        identities = set()
        for key, spec in wanted.items():
            uid = spec['identity']['uid']
            if uid in identities:
                duplicate = self.listeners.pop(key, None)
                if duplicate:
                    duplicate.close()
                self.callback(spec, {'type': 'state', 'status': 'blocked',
                    'message': '此账号已在另一个网页空间接收消息；本空间保留低频补漏。'})
                continue
            identities.add(uid)
            if key in self.listeners:
                self.listeners[key].spec = spec
            if key not in self.listeners and not spec.get('blocked'):
                listener = self.factory(spec, self.gateway, self.callback)
                self.listeners[key] = listener
                listener.thread.start()

    def close(self):
        for listener in self.listeners.values():
            listener.close()
        for listener in self.listeners.values():
            listener.thread.join(timeout=5)
        self.listeners.clear()
