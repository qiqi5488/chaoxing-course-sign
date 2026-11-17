"""Realtime attendance notices, low-frequency reconciliation and one-attempt signing."""
from concurrent.futures import ThreadPoolExecutor
import hashlib
import json
import logging
import math
import secrets
import threading
import time

from upstream import UpstreamError
from vault import WorkspaceRef, event, iso, timestamp
from im_receiver import RealtimeReceivers


SESSION_ERRORS = {'session_expired', 'session_missing', 'login_required', 'invalid_session'}
RESULTS = {'reported_success', 'already_signed', 'requires_validation', 'invalid_challenge',
           'device_conflict', 'rejected', 'unknown', 'outcome_unknown', 'http_error',
           'login_required', 'session_expired', 'qr_required', 'code_required',
           'location_required', 'photo_required', 'face_required', 'needs_input', 'need_context', 'requires_manual'}
MANUAL = {'qr': '需要扫描老师提供的二维码。', 'code': '需要老师提供的签到码或手势。',
          'gesture': '手势活动需先核对进行状态与附加要求。',
          'position': '需要在学习通提供真实位置。', 'located_code': '需要签到码和真实位置。',
          'photo': '课程同活动照片的读取接口尚未核实，请先在学习通完成拍照签到。', 'face': '需要在学习通完成人脸验证。'}
LOGGER = logging.getLogger('uvicorn.error.course.monitor')


def waiting_for_group_photo(job):
    return (job.get('activityScope') == 'group' and job.get('resolvedKind') == 'photo'
            and job.get('status') == 'photo_required' and job.get('submitted') is False
            and job.get('reasonCode') in ('photo_missing', 'photo_lookup_incomplete'))


def sync_task_result(task, job):
    task.update(status=job['status'], message=job['message'],
                updatedAt=job.get('completedAt') or task.get('updatedAt'))
    for key in ('submitted', 'reasonCode', 'resolvedKind'):
        if key in job:
            task[key] = job[key]
        else:
            task.pop(key, None)
    if job.get('activityScope') == 'group' and job.get('resolvedKind') == 'photo':
        task['kind'] = 'photo'


def defaults():
    return {'enabled': False, 'autoSign': False, 'intervalSeconds': 10,
            'status': 'disabled', 'message': '尚未开启课程监控。',
            'lastCheckedAt': None, 'nextCheckAt': None, 'cooldownUntil': None, 'courseListCooldownUntil': None,
            'courseCount': 0, 'courseErrors': [],
            'groupCount': 0, 'groupMessage': '', 'reconcileSeconds': 300,
            'realtimeStatus': 'disabled', 'realtimeMessage': '实时消息尚未开启。',
            'connectedAt': None, 'lastMessageAt': None}


def session_stamp(row):
    return (row['_identity']['uid'], row['lastLogin'], (row.get('grant') or {}).get('grantedAt'))


def cooldown_seconds(config):
    deadline = timestamp(config.get('cooldownUntil')) or 0
    return max(0, math.ceil(deadline - time.time()))


def require_no_cooldown(config):
    remaining = cooldown_seconds(config)
    if remaining:
        raise UpstreamError('upstream_rate_limited', f'学习通课程操作受限，请等待约{remaining}秒后再处理。', 429,
                            {'retryAfterSeconds': remaining})


def list_cooldown_seconds(config):
    return max(0, math.ceil((timestamp(config.get('courseListCooldownUntil')) or 0) - time.time()))


def defer_list_config(config, seconds):
    config.setdefault('settingsVersion', config.get('version'))
    seconds = min(3600, max(30, int(seconds)))
    deadline = max(time.time() + seconds, timestamp(config.get('courseListCooldownUntil')) or 0)
    course_cooling = cooldown_seconds(config)
    config.update(courseListCooldownUntil=iso(deadline), version=secrets.token_hex(12),
                  nextCheckAt=iso(time.time() + 300) if config['enabled'] else None,
                  status=('rate_limited' if course_cooling else 'partial') if config['enabled'] else 'disabled',
                  message='课程查询和提交暂缓，群聊继续检查。' if course_cooling
                  else '课程活动列表查询受限，课程通知与群聊继续接收。')


def defer_config(config, seconds):
    config.setdefault('settingsVersion', config.get('version'))
    seconds = min(3600, max(30, int(seconds)))
    deadline = max(time.time() + seconds, timestamp(config.get('cooldownUntil')) or 0)
    config.update(cooldownUntil=iso(deadline), nextCheckAt=iso(time.time() + 300) if config['enabled'] else None,
                  version=secrets.token_hex(12), status='rate_limited' if config['enabled'] else 'disabled',
                  message='学习通课程接口操作受限，课程查询和提交已暂缓；群聊继续检查。')


class CourseMonitor:
    def __init__(self, vault, gateway):
        self.vault, self.gateway = vault, gateway
        self.pool = ThreadPoolExecutor(max_workers=4, thread_name_prefix='course-monitor')
        self.notice_pool = ThreadPoolExecutor(max_workers=2, thread_name_prefix='course-notice')
        self.closing, self.wake = threading.Event(), threading.Event()
        self.guard = threading.Lock()
        self.running = set()
        self.notice_running = set()
        self.course_cache = {}
        self.course_backoff = {}
        self.thread = None
        self.receivers = RealtimeReceivers(gateway, self._im_event)

    def start(self):
        if self.thread is None:
            self.thread = threading.Thread(target=self._loop, name='course-monitor-scheduler', daemon=True)
            self.thread.start()

    def close(self):
        self.closing.set()
        self.wake.set()
        if self.thread:
            self.thread.join()
        self.receivers.close()
        self.notice_pool.shutdown(wait=True, cancel_futures=True)
        self.pool.shutdown(wait=True, cancel_futures=True)

    def snapshot(self, token, state):
        monitor = self.vault.load_monitor(token)
        ids = {row['id'] for row in state['accounts']}
        for task in monitor['tasks']:
            if task['accountId'] in ids and task.get('requestId'):
                job, _ = self.vault.get_job(token, task['requestId'])
                if job:
                    sync_task_result(task, job)
        accounts = []
        for row in state['accounts']:
            config = {**defaults(), **monitor['accounts'].get(row['id'], {})}
            accounts.append({'accountId': row['id'], **{key: config[key] for key in defaults()}})
        return {'accounts': accounts, 'tasks': [t for t in monitor['tasks'] if t['accountId'] in ids],
                'serverTime': iso()}

    def configure(self, token, account_id, enabled, auto_sign, interval):
        # Caller holds the workspace lock and checks account ownership/consent.
        monitor = self.vault.load_monitor(token)
        config = {**defaults(), **monitor['accounts'].get(account_id, {})}
        if not enabled or not config['enabled']:
            config.pop('imBlockedStamp', None)
            config['imEpoch'] = secrets.token_hex(12)
            config.update(realtimeStatus='connecting' if enabled else 'disabled',
                          realtimeMessage='正在连接学习通实时消息。' if enabled else '实时消息已停止。')
        config.update(enabled=enabled, autoSign=auto_sign, intervalSeconds=interval,
                      version=secrets.token_hex(12), failures=0,
                      nextCheckAt=iso() if enabled else None,
                      status='waiting' if enabled else 'disabled',
                      message='等待检查课程签到。' if enabled else '课程监控已停止；已发出的请求仍会保留回执。')
        config['settingsVersion'] = config['version']
        if enabled and cooldown_seconds(config):
            config.update(status='rate_limited',
                          message='学习通课程接口操作受限，课程暂缓，群聊继续检查。')
        elif enabled and list_cooldown_seconds(config):
            config.update(status='partial', message='课程活动列表查询受限，课程通知与群聊继续接收。')
        monitor['accounts'][account_id] = config
        self.vault.save_monitor(token, monitor)
        self.wake.set()

    def defer(self, token, account_id, seconds):
        with self.vault.lock(token):
            monitor = self.vault.load_monitor(token)
            config = {**defaults(), **monitor['accounts'].get(account_id, {})}
            defer_config(config, seconds)
            monitor['accounts'][account_id] = config
            self.vault.save_monitor(token, monitor)

    def remove(self, token, account_ids):
        monitor = self.vault.load_monitor(token)
        for account_id in account_ids:
            monitor['accounts'].pop(account_id, None)
            self.course_cache.pop((self.vault.owner(token), account_id), None)
        self.course_backoff = {key: value for key, value in self.course_backoff.items()
                               if key[:2] not in {(self.vault.owner(token), account_id) for account_id in account_ids}}
        monitor['tasks'] = [t for t in monitor['tasks'] if t['accountId'] not in account_ids]
        self.vault.save_monitor(token, monitor)

    def check_soon(self, token, account_id):
        monitor = self.vault.load_monitor(token)
        config = monitor['accounts'].get(account_id)
        if not config or not config['enabled']:
            return False
        config['nextCheckAt'] = iso()
        config['checkSeq'] = config.get('checkSeq', 0) + 1
        self.vault.save_monitor(token, monitor)
        self.wake.set()
        return True

    def _loop(self):
        while not self.closing.is_set():
            try:
                self.schedule()
            except Exception:
                # A scheduler storage failure must not start a second worker or
                # fabricate a successful check. Try discovery again next tick.
                LOGGER.error('course_monitor_scheduler_failed')
            self.wake.wait(1)
            self.wake.clear()

    def schedule(self):
        candidates, pending, listeners = [], [], []
        for ref in self.vault.monitored_workspaces():
            with self.vault.lock(ref):
                monitor = self.vault.load_monitor(ref)
                state = self.vault.load(ref)
                for row in (state or {}).get('accounts', []):
                    config = monitor['accounts'].get(row['id'], {})
                    if not config.get('enabled') or row['status'] != 'active' or not row.get('grant') or not row['_cookies']:
                        continue
                    stamp = session_stamp(row)
                    if config.get('noticeStamp') != list(stamp):
                        config.update(noticeStamp=list(stamp), inbox=[], seenNotices=[])
                        self.vault.save_monitor(ref, monitor)
                    if any((timestamp(item.get('retryAt')) or 0) <= time.time() for item in config.get('inbox', [])):
                        pending.append((ref, row['id']))
                    if callable(getattr(self.gateway, 'get_im_credentials', None)):
                        if not config.get('imDeviceId'):
                            config['imDeviceId'] = hashlib.sha256(
                                ('course-sign-receiver:' + ref.digest + ':' + row['id']).encode()).hexdigest()[:32]
                            self.vault.save_monitor(ref, monitor)
                        listeners.append({'key': (ref.digest, row['id']), 'ref': ref, 'accountId': row['id'],
                            'stamp': stamp, 'cookies': row['_cookies'], 'identity': dict(row['_identity']),
                            'epoch': config.get('imEpoch'), 'deviceId': config['imDeviceId'],
                            'blocked': config.get('imBlockedStamp') == list(stamp)})
                candidates.extend((timestamp(config.get('nextCheckAt')) or 0, ref, account_id)
                                  for account_id, config in monitor['accounts'].items()
                                  if config['enabled'] and (timestamp(config.get('nextCheckAt')) or 0) <= time.time())
        self.receivers.sync(listeners)
        for ref, account_id in pending:
            key = (ref.digest, account_id)
            with self.guard:
                if key in self.notice_running or self.closing.is_set() or len(self.notice_running) >= 2:
                    continue
                self.notice_running.add(key)
            self.notice_pool.submit(self._notice_worker, ref, account_id, key)
        for _, ref, account_id in sorted(candidates, key=lambda item: item[0]):
            key = (ref.digest, account_id)
            with self.guard:
                if key in self.running or self.closing.is_set():
                    continue
                # Keep a bounded queue as well as bounded thread count.
                if len(self.running) >= 4:
                    return
                self.running.add(key)
            self.pool.submit(self._worker, ref, account_id, key)

    def _im_event(self, spec, message):
        ref, account_id, stamp = spec['ref'], spec['accountId'], spec['stamp']
        with self.vault.lock(ref):
            _, monitor, row, config, valid = self._current(ref, account_id)
            if not valid or session_stamp(row) != stamp:
                return
            if 'epoch' in spec and spec['epoch'] != config.get('imEpoch'):
                return
            if message['type'] == 'state':
                status = message['status']
                if config.get('realtimeStatus') == status and config.get('realtimeMessage') == message['message']:
                    return
                config.update(realtimeStatus=status, realtimeMessage=message['message'])
                if status == 'connected':
                    config.update(connectedAt=iso(), nextCheckAt=iso(), checkSeq=config.get('checkSeq', 0) + 1)
                elif status == 'blocked':
                    config['imBlockedStamp'] = list(stamp)
            elif message['type'] == 'notice':
                candidate = message['candidate']
                if config.get('noticeStamp') != list(stamp):
                    config.update(noticeStamp=list(stamp), inbox=[], seenNotices=[])
                inbox = config.setdefault('inbox', [])
                if candidate['noticeKey'] in config.get('seenNotices', []) or any(
                        item['candidate']['noticeKey'] == candidate['noticeKey'] for item in inbox):
                    return
                config['lastMessageAt'] = iso()
                if len(inbox) >= 200:
                    # Bounded queue overflow requests an immediate history reconciliation.
                    config.update(nextCheckAt=iso(), checkSeq=config.get('checkSeq', 0) + 1)
                else:
                    config['noticeSeq'] = config.get('noticeSeq', 0) + 1
                    inbox.append({'seq': config['noticeSeq'], 'candidate': candidate,
                                  'receivedAt': iso(), 'retryAt': None, 'attempts': 0})
            self.vault.save_monitor(ref, monitor)
        self.wake.set()

    def _settle_notice(self, ref, account_id, stamp, seq, *, retry=False):
        with self.vault.lock(ref):
            _, monitor, row, config, valid = self._current(ref, account_id)
            if not valid or session_stamp(row) != stamp or config.get('noticeStamp') != list(stamp):
                return
            item = next((item for item in config.get('inbox', []) if item['seq'] == seq), None)
            if not item:
                return
            if retry:
                item['attempts'] += 1
                delay = min(300, 5 * 2 ** min(item['attempts'] - 1, 6))
                if item['candidate']['activityScope'] == 'course':
                    delay = max(delay, cooldown_seconds(config))
                item['retryAt'] = iso(time.time() + delay)
            else:
                config['inbox'].remove(item)
                config['seenNotices'] = (config.get('seenNotices', []) + [item['candidate']['noticeKey']])[-512:]
            self.vault.save_monitor(ref, monitor)

    def _notice_worker(self, ref, account_id, key):
        """Use a separate bounded pool so slow full scans cannot delay a notice."""
        try:
            with self.vault.lock(ref):
                _, _, row, config, valid = self._current(ref, account_id)
                if not valid:
                    return
                stamp = session_stamp(row)
                if config.get('noticeStamp') != list(stamp):
                    return
                batch = [item for item in config.get('inbox', [])
                         if (timestamp(item.get('retryAt')) or 0) <= time.time()][:20]
            for item in batch:
                retry, version, cookies = True, None, None
                try:
                    with self.vault.lock(ref):
                        _, _, row, config, valid = self._current(ref, account_id)
                        if not valid or session_stamp(row) != stamp:
                            return
                        version, cookies, identity = config['version'], row['_cookies'], dict(row['_identity'])
                        if item['candidate']['activityScope'] == 'course' and cooldown_seconds(config):
                            continue
                    activity = item['candidate']
                    if activity['activityScope'] == 'group':
                        self._group_activity(ref, account_id, version, stamp, activity)
                        retry = False
                    else:
                        cached = self.course_cache.get(key)
                        if not cached or cached[0] != stamp or time.monotonic() - cached[1] >= 300:
                            details = self.gateway.get_account_details(cookies, identity)
                            with self.vault.lock(ref):
                                state, _, row, _, valid = self._current(ref, account_id, version, stamp)
                                if not valid:
                                    continue
                                if row['_cookies'] == cookies:
                                    row['_cookies'] = details.get('cookies', cookies)
                                    self.vault.save(ref, state)
                                cookies = row['_cookies']
                            if not any(source.get('status') == 'ok' for source in details.get('sources', [])):
                                continue
                            cached = (stamp, time.monotonic(), details['courses'], [])
                            if all(source.get('status') == 'ok' for source in details.get('sources', [])):
                                self.course_cache[key] = cached
                        course = next((course for course in cached[2]
                            if str(course['courseId']) == activity['courseId'] and str(course['classId']) == activity['classId']), None)
                        if not course:
                            continue  # Membership may appear later; never trust the notification alone.
                        result = self.gateway.get_course_notification_activities(cookies, identity, course, [activity])
                        with self.vault.lock(ref):
                            state, _, row, _, valid = self._current(ref, account_id, version, stamp)
                            if not valid:
                                continue
                            if row['_cookies'] == cookies:
                                row['_cookies'] = result.get('cookies', cookies)
                                self.vault.save(ref, state)
                        retry = bool(result.get('detailErrors'))
                        for verified in result['activities']:
                            self._activity(ref, account_id, version, stamp, course, verified)
                            retry = retry or verified.get('kind') == 'unknown' or verified.get('status') == 'unknown'
                    with self.vault.lock(ref):
                        *_, valid = self._current(ref, account_id, version, stamp)
                        retry = retry or not valid
                except UpstreamError as error:
                    if error.code in SESSION_ERRORS or error.code == 'upstream_rate_limited':
                        self._finish(ref, account_id, version, stamp, [], None, error, cookies)
                except Exception:
                    LOGGER.warning('course_notice_verification_failed')
                finally:
                    self._settle_notice(ref, account_id, stamp, item['seq'], retry=retry)
        finally:
            with self.guard:
                self.notice_running.discard(key)
            self.wake.set()

    def _current(self, ref, account_id, version=None, stamp=None, *, allow_disabled=False):
        state = self.vault.load(ref)
        monitor = self.vault.load_monitor(ref)
        row = next((r for r in state['accounts'] if r['id'] == account_id), None) if state else None
        config = monitor['accounts'].get(account_id)
        valid = bool(row and row['status'] == 'active' and row.get('grant') and row['_cookies']
                     and config and (config['enabled'] or allow_disabled) and not self.closing.is_set())
        if valid and version is not None:
            valid = config['version'] == version and session_stamp(row) == stamp
        return state, monitor, row, config, valid

    def _worker(self, ref, account_id, key):
        version = stamp = cookies = None
        check_seq = None
        errors, course_count, failure, course_notifications = [], None, None, []
        try:
            with self.vault.lock(ref):
                state, monitor, row, config, valid = self._current(ref, account_id)
                if not valid:
                    if config and config['enabled']:
                        config.update(enabled=False, status='login_required', nextCheckAt=None,
                                      message='账号已移除或授权失效，请重新登录后开启监控。')
                        self.vault.save_monitor(ref, monitor)
                    return
                version, stamp = config['version'], session_stamp(row)
                check_seq = config.get('checkSeq', 0)
                cookies, identity = row['_cookies'], dict(row['_identity'])
                config.update(status='checking', message='正在检查课程签到。')
                self.vault.save_monitor(ref, monitor)
            # IM discovery reads notifications only. Entering a group student
            # page is a submission and happens only after the job is reserved.
            if callable(getattr(self.gateway, 'get_group_activities', None)):
                try:
                    groups = self.gateway.get_group_activities(cookies, identity)
                    course_notifications = groups.get('courseActivities', [])
                    with self.vault.lock(ref):
                        state, _, row, current, valid = self._current(ref, account_id, version, stamp)
                        if not valid:
                            return
                        if row['_cookies'] == cookies:
                            row['_cookies'] = groups.get('cookies', cookies)
                            self.vault.save(ref, state)
                        cookies = row['_cookies']
                        current.update(groupCount=groups.get('groupCount', 0),
                                       groupMessage=groups.get('message', ''))
                        stored = self.vault.load_monitor(ref)
                        stored['accounts'][account_id].update(groupCount=current['groupCount'],
                                                             groupMessage=current['groupMessage'])
                        self.vault.save_monitor(ref, stored)
                    if groups.get('status') == 'partial':
                        errors.append({'name': '群聊', 'message': groups.get('message') or '部分群通知读取未完成。'})
                    for activity in groups.get('activities', []):
                        self._group_activity(ref, account_id, version, stamp, activity)
                    with self.vault.lock(ref):
                        *_, row, _, valid = self._current(ref, account_id, version, stamp)
                        if not valid:
                            return
                        cookies = row['_cookies']
                except UpstreamError as error:
                    if error.code in SESSION_ERRORS or error.code == 'upstream_rate_limited':
                        raise
                    errors.append({'name': '群聊', 'message': str(error)})
                except Exception:
                    errors.append({'name': '群聊', 'message': '群活动通知未能读取，请稍后重查。'})
            with self.vault.lock(ref):
                *_, current_config, valid = self._current(ref, account_id, version, stamp)
                if not valid or cooldown_seconds(current_config):
                    return
            cached = self.course_cache.get(key)
            if not cached or cached[0] != stamp or time.monotonic() - cached[1] >= 300:
                details = self.gateway.get_account_details(cookies, identity)
                if not isinstance(details, dict) or not isinstance(details.get('courses'), list):
                    raise ValueError('Invalid course details')
                sources = details.get('sources', [])
                if not any(s.get('status') == 'ok' for s in sources):
                    raise UpstreamError('courses_unavailable', '课程来源未成功返回，暂时无法监控。')
                cached = (stamp, time.monotonic(), details['courses'],
                          [{'name': s.get('label', '课程来源'), 'message': s.get('message') or '课程来源暂不可用。'}
                           for s in sources if s.get('status') == 'error'])
                # Partial course discovery is retried next cycle.
                if not cached[3]:
                    self.course_cache[key] = cached
                with self.vault.lock(ref):
                    state, _, row, _, valid = self._current(ref, account_id, version, stamp)
                    if not valid:
                        return
                    if row['_cookies'] == cookies:
                        row['_cookies'] = details.get('cookies', cookies)
                        self.vault.save(ref, state)
                    cookies = row['_cookies']
            courses = cached[2]
            errors.extend(cached[3])
            course_count = len(courses)
            with self.vault.lock(ref):
                _, stored, _, current, valid = self._current(ref, account_id, version, stamp)
                if not valid:
                    return
                current['courseCount'] = course_count
                self.vault.save_monitor(ref, stored)
            # Course notices are an independent source. Verify their original
            # course/class pair before any detail request, even if lists fail.
            if callable(getattr(self.gateway, 'get_course_notification_activities', None)):
                for course in courses:
                    candidates = [item for item in course_notifications
                                  if item.get('activityScope') == 'course'
                                  and item.get('notificationType') == 'course_attendance'
                                  and str(item.get('courseId')) == str(course['courseId'])
                                  and str(item.get('classId')) == str(course['classId'])]
                    if not candidates:
                        continue
                    with self.vault.lock(ref):
                        *_, row, current, valid = self._current(ref, account_id, version, stamp)
                        if not valid or cooldown_seconds(current):
                            return
                        cookies = row['_cookies']
                    try:
                        result = self.gateway.get_course_notification_activities(cookies, identity, course, candidates)
                        with self.vault.lock(ref):
                            state, _, row, _, valid = self._current(ref, account_id, version, stamp)
                            if not valid:
                                return
                            if row['_cookies'] == cookies:
                                row['_cookies'] = result.get('cookies', cookies)
                                self.vault.save(ref, state)
                            cookies = row['_cookies']
                        if result.get('detailErrors'):
                            errors.append({'name': course.get('name') or '课程通知',
                                           'message': '已收到课程签到通知，部分详情仍待核验。'})
                        activities = result['activities']
                    except UpstreamError as error:
                        if error.code in SESSION_ERRORS or error.code == 'upstream_rate_limited':
                            raise
                        errors.append({'name': course.get('name') or '课程通知', 'message': str(error)})
                        activities = candidates
                    for activity in activities:
                        self._activity(ref, account_id, version, stamp, course, activity)
            with self.vault.lock(ref):
                *_, row, current, valid = self._current(ref, account_id, version, stamp)
                if not valid or cooldown_seconds(current):
                    return
                cookies = row['_cookies']
                if list_cooldown_seconds(current):
                    errors.append({'name': '课程活动列表', 'message': '列表查询暂缓，已继续检查课程通知。'})
                    return
            for course in courses:
                with self.vault.lock(ref):
                    *_, valid = self._current(ref, account_id, version, stamp)
                if not valid:
                    return
                course_key = (*key, str(course.get('courseId')), str(course.get('classId')))
                retry = self.course_backoff.get(course_key)
                if retry and retry[0] == stamp and retry[2] > time.monotonic():
                    errors.append({'name': course.get('name') or '未命名课程', 'message': retry[3]})
                    continue
                try:
                    result = self.gateway.get_course_activities(cookies, identity, course)
                    self.course_backoff.pop(course_key, None)
                    with self.vault.lock(ref):
                        state, _, row, _, valid = self._current(ref, account_id, version, stamp)
                        if not valid:
                            return
                        if row['_cookies'] == cookies:
                            row['_cookies'] = result.get('cookies', cookies)
                            self.vault.save(ref, state)
                        cookies = row['_cookies']
                    for source in result.get('sources', []):
                        if source.get('status') != 'ok':
                            errors.append({'name': course.get('name') or '未命名课程',
                                           'message': source.get('message') or '部分活动来源暂不可用。'})
                    if result.get('detailErrors'):
                        errors.append({'name': course.get('name') or '未命名课程',
                                       'message': '部分活动详情未能核验，相关任务需要手动查看。'})
                    for activity in result['activities']:
                        self._activity(ref, account_id, version, stamp, course, activity)
                    with self.vault.lock(ref):
                        *_, row, _, valid = self._current(ref, account_id, version, stamp)
                        if not valid:
                            return
                        cookies = row['_cookies']
                except UpstreamError as error:
                    if error.code in SESSION_ERRORS or error.code == 'upstream_rate_limited':
                        raise
                    errors.append({'name': course.get('name') or '未命名课程', 'message': str(error)})
                    failures = retry[1] + 1 if retry and retry[0] == stamp else 1
                    self.course_backoff[course_key] = (stamp, failures,
                        time.monotonic() + min(300, config['intervalSeconds'] * 2 ** min(failures, 5)), str(error))
                except Exception:
                    errors.append({'name': course.get('name') or '未命名课程', 'message': '活动返回内容无法识别，请稍后重查。'})
        except UpstreamError as error:
            failure = error
        except Exception:
            failure = UpstreamError('monitor_failed', '课程检查未完成，请稍后重查。')
        finally:
            try:
                if version is not None:
                    self._finish(ref, account_id, version, stamp, errors, course_count, failure, cookies)
                    with self.vault.lock(ref):
                        _, stored, row, current, valid = self._current(ref, account_id)
                        if valid and session_stamp(row) == stamp and current.get('checkSeq', 0) != check_seq:
                            current['nextCheckAt'] = iso()
                            self.vault.save_monitor(ref, stored)
            finally:
                with self.guard:
                    self.running.discard(key)
                self.wake.set()

    def _finish(self, ref, account_id, version, stamp, errors, course_count, failure, cookies=None):
        with self.vault.lock(ref):
            state, monitor, row, config, valid = self._current(ref, account_id, version, stamp)
            if (failure and failure.code == 'upstream_rate_limited' and row and config
                    and row.get('grant') and row['status'] == 'active' and session_stamp(row) == stamp):
                if failure.details.get('operation') == 'course_activity_list':
                    defer_list_config(config, failure.details.get('retryAfterSeconds', 300))
                else:
                    defer_config(config, failure.details.get('retryAfterSeconds', 300))
                if course_count is not None:
                    config['courseCount'] = course_count
                config['courseErrors'] = (errors + [{'name': '课程活动列表' if failure.details.get('operation') == 'course_activity_list'
                                                    else '课程', 'message': str(failure)}])[:100]
                config['lastCheckedAt'] = iso()
                self.vault.save_monitor(ref, monitor)
                return
            if valid:
                if (failure and failure.code in SESSION_ERRORS and cookies is not None
                        and row['_cookies'] != cookies):
                    config.update(lastCheckedAt=iso(), nextCheckAt=iso(time.time() + 300),
                                  status='rate_limited' if cooldown_seconds(config) else 'waiting',
                                  message='旧查询使用的会话已更新，已保留当前账号，等待下一轮复查。')
                    self.vault.save_monitor(ref, monitor)
                    return
                if cooldown_seconds(config) and not failure:
                    config.update(lastCheckedAt=iso(), nextCheckAt=iso(time.time() + 300),
                                  status='rate_limited', message='课程查询和提交暂缓，群聊继续检查。')
                    self.vault.save_monitor(ref, monitor)
                    return
                expired = failure and failure.code in SESSION_ERRORS
                if expired:
                    row.update(status='expired', _cookies=[], sessionExpiresAt=iso())
                    config['enabled'] = False
                    self.vault.save(ref, state)
                failures = config.get('failures', 0) + 1 if failure else 0
                delay = 300
                list_cooling = list_cooldown_seconds(config)
                config.update(failures=failures, lastCheckedAt=iso(),
                              courseCount=course_count if course_count is not None else config['courseCount'],
                              courseErrors=errors[:100], nextCheckAt=iso(time.time() + delay) if not expired else None,
                              status='login_required' if expired else 'error' if failure else 'partial' if errors or list_cooling else 'monitoring',
                              message=str(failure) if failure else '课程活动列表查询受限，课程通知与群聊继续接收。' if list_cooling
                              else '部分课程来源读取失败，其余课程继续监控。' if errors else '课程检查完成，持续等待新的签到。')
                config['cooldownUntil'] = None
                if not list_cooling:
                    config['courseListCooldownUntil'] = None
                self.vault.save_monitor(ref, monitor)

    def _activity(self, ref, account_id, version, stamp, course, activity):
        return self._process_activity(ref, account_id, version, stamp, course, activity)

    def _group_activity(self, ref, account_id, version, stamp, activity, *, force=False, enqueue=False):
        if (activity.get('activityScope') != 'group' or not isinstance(activity.get('chatId'), str)
                or not activity['chatId'] or len(activity['chatId']) > 512
                or any(ord(c) < 32 for c in activity['chatId'])):
            return None
        context = {'courseId': '', 'classId': '', 'name': activity.get('groupName') or '群聊'}
        return self._process_activity(ref, account_id, version, stamp, context, activity,
                                      activity_scope='group', force=force, enqueue=enqueue)

    def sign_now(self, token, task_id):
        """Reserve a supported discovered task before returning its 202 snapshot."""
        ref = WorkspaceRef(self.vault.owner(token))
        with self.vault.lock(ref):
            state, monitor = self.vault.load(ref), self.vault.load_monitor(ref)
            task = next((t for t in monitor['tasks'] if t['id'] == task_id), None)
            if not task:
                raise UpstreamError('task_not_found', '未找到当前网页下的签到任务。', 404)
            account_id = task['accountId']
            state, _, row, config, valid = self._current(ref, account_id, allow_disabled=True)
            if not valid:
                raise UpstreamError('login_required', '请先重新登录并授权此账号。', 409)
            photo_retry = False
            if task.get('requestId'):
                job, _ = self.vault.get_job(ref, task['requestId'])
                photo_retry = bool(job and waiting_for_group_photo(job))
                if not photo_retry:
                    return self.snapshot(token, state)
            if task.get('activityScope', 'course') == 'course':
                require_no_cooldown(config)
            eligible_kind = (photo_retry or task.get('kind') == 'ordinary' or task.get('activityScope') == 'group'
                             and task.get('kind') in ('ordinary_candidate', 'qr', 'gesture') or
                             task.get('activityScope', 'course') == 'course' and task.get('kind') == 'gesture'
                             and task.get('requirements') == ['code'] and task.get('status') == 'monitoring_only')
            if not eligible_kind or task.get('status') not in (
                    'monitoring_only', 'needs_input', 'discovered', 'awaiting_action', 'qr_required', 'photo_required'):
                raise UpstreamError('ordinary_not_ready', '该任务暂不能直接处理，请先查看活动要求和当前状态。', 409)
            version, stamp = config['version'], session_stamp(row)
            activity = {'activeId': task['activeId'], 'kind': 'ordinary_candidate' if photo_retry else task['kind'],
                        'status': task.get('activityStatus', 'active'), 'autoEligible': True,
                        'name': task.get('name'), 'courseId': task.get('courseId', ''),
                        'classId': task.get('classId', ''), 'chatId': task.get('chatId', ''),
                        'requirements': task.get('requirements', []),
                        'activityScope': task.get('activityScope', 'course')}
            course = {'courseId': task.get('courseId', ''), 'classId': task.get('classId', ''),
                      'name': task.get('courseName')}
        if activity['activityScope'] == 'group':
            request_id = self._group_activity(ref, account_id, version, stamp, activity, force=True, enqueue=True)
        else:
            request_id = self._process_activity(ref, account_id, version, stamp, course, activity,
                                                force=True, enqueue=True)
        if not request_id:
            raise UpstreamError('ordinary_not_ready', '任务状态已经变化，请刷新后查看。', 409)
        with self.vault.lock(ref):
            return self.snapshot(token, self.vault.load(ref))

    def _process_activity(self, ref, account_id, version, stamp, course, activity,
                          *, activity_scope='course', force=False, enqueue=False):
        # Only identifiers supplied by the authenticated course list are accepted.
        if activity_scope == 'course' and (str(activity.get('courseId')) != str(course.get('courseId')) or
                str(activity.get('classId')) != str(course.get('classId'))):
            return
        active_id = str(activity.get('activeId', ''))
        if not active_id.isdecimal() or len(active_id) > 32:
            return
        task_key = (f'group:{account_id}:{active_id}' if activity_scope == 'group' else
                    f'{account_id}:{course["courseId"]}:{course["classId"]}:{active_id}')
        task_id = hashlib.sha256(task_key.encode()).hexdigest()
        request_id = ('auto_group_' if activity_scope == 'group' else 'auto_') + hashlib.sha256(f'{account_id}:{active_id}'.encode()).hexdigest()
        with self.vault.lock(ref):
            state, monitor, row, config, valid = self._current(ref, account_id, version, stamp, allow_disabled=force)
            if not valid:
                return
            if activity_scope == 'course' and cooldown_seconds(config):
                return
            task = next((t for t in monitor['tasks'] if t['id'] == task_id), None)
            if task is None:
                if activity.get('status') == 'ended' and not (activity_scope == 'course'
                        and activity.get('notificationType') == 'course_attendance'):
                    return
                task = {'id': task_id, 'accountId': account_id, 'accountName': row['name'],
                        'courseName': course.get('name') or '未命名课程', 'name': activity.get('name') or '课程签到',
                        'activeId': active_id, 'courseId': str(course['courseId']), 'classId': str(course['classId']),
                        'activityScope': activity_scope,
                        'discoveredAt': iso()}
                if activity.get('status') == 'ended':
                    monitor['tasks'].append(task)
                else:
                    monitor['tasks'].insert(0, task)
                del monitor['tasks'][200:]
            if activity_scope == 'group' and activity.get('kind') == 'gesture':
                task['kind'] = 'gesture'
            previous, previous_hash = self.vault.get_job(ref, task.get('requestId') or request_id)
            if task.get('requestId') and not previous:
                return task['requestId']
            if not previous:
                previous = self.vault.prior_job(ref, account_id, active_id, activity_scope)
            if previous:
                sync_task_result(task, previous)
                task['requestId'] = previous['requestId']
                retry_photo = (activity_scope == 'group' and waiting_for_group_photo(previous)
                               and (config['autoSign'] or force) and activity.get('status') != 'ended'
                               and (force or time.time() - (timestamp(previous.get('completedAt')) or time.time()) >= 300))
                if not retry_photo:
                    self.vault.save_monitor(ref, monitor)
                    return previous['requestId']
                request_id = previous['requestId']
            kind, status = activity.get('kind', 'unknown'), activity.get('status')
            task.update(kind=kind, updatedAt=iso(), status='needs_input', message=MANUAL.get(kind, '活动条件尚未确认，请在学习通查看。'))
            task.update(activityStatus=status, requirements=activity.get('requirements', []))
            if activity_scope == 'course' and activity.get('notificationType') == 'course_attendance':
                task['notificationType'] = 'course_attendance'
                if type(activity.get('startTime')) is int and activity['startTime'] > 0:
                    task['startTime'] = activity['startTime']
                if kind == 'unknown' or status == 'unknown':
                    task.update(status='awaiting_details', message='已收到课程签到通知，活动详情尚未核验，将继续检查。')
            if activity_scope == 'group':
                task['chatId'] = activity['chatId']
                if previous and previous.get('resolvedKind') == 'photo':
                    task['kind'] = 'photo'
            if status == 'ended':
                task.update(status='ended', message='签到活动已结束。')
            elif ((activity_scope == 'course' and status == 'active' and kind == 'ordinary'
                   and activity.get('autoEligible') is True) or
                  (activity_scope == 'course' and status == 'active' and kind == 'gesture'
                   and activity.get('requirements') == ['code']) or
                  (activity_scope == 'group' and status in ('active', 'unknown')
                   and kind in ('ordinary', 'ordinary_candidate', 'qr', 'gesture'))):
                label = ('群拍照签到' if activity_scope == 'group' and task.get('kind') == 'photo' else
                         '群二维码签到' if activity_scope == 'group' and kind == 'qr' else
                         '群手势签到' if activity_scope == 'group' and kind == 'gesture' else
                         '课程手势签到' if kind == 'gesture' else '普通签到')
                task.update(status='monitoring_only', message=f'发现{label}；可直接签到，自动签到尚未开启。')
                if config['autoSign'] or force:
                    task.update(status='pending', message=f'正在核验并提交{label}。', requestId=request_id)
            if task['status'] != 'pending':
                self.vault.save_monitor(ref, monitor)
                return
            payload = {'kind': 'gesture' if kind == 'gesture' else 'ordinary', 'activeId': active_id, 'courseId': str(course['courseId']),
                       'classId': str(course['classId']), '_autoSign': True}
            course_hint = ' '.join(str(name or '') for name in (course.get('name'), activity.get('name')))
            if activity_scope == 'course' and '教务' in course_hint:
                payload['_candidateName'] = course_hint
            if activity_scope == 'group':
                payload = {'kind': kind if kind in ('qr', 'gesture') else 'ordinary', 'activityScope': 'group', 'activeId': active_id}
            fingerprint = previous_hash or hashlib.sha256(json.dumps(payload, sort_keys=True).encode()).hexdigest()
            job = {'requestId': request_id, 'accountId': account_id, 'activeId': active_id,
                   'name': task['name'], 'courseName': task['courseName'], 'kind': kind,
                   'status': 'pending', 'message': task['message'],
                   'createdAt': (previous or {}).get('createdAt') or iso()}
            if activity_scope == 'group':
                job['activityScope'] = 'group'
                if previous and previous.get('resolvedKind') == 'photo':
                    job['resolvedKind'] = 'photo'
            sync_task_result(task, job)
            self.vault.save_job(ref, job, fingerprint)
            # Persist the duplicate guard first. A crash between writes can be
            # recovered from the stable request ID without leaving a ghost task.
            self.vault.save_monitor(ref, monitor)
            cookies, identity = row['_cookies'], dict(row['_identity'])
            settings_version = config.get('settingsVersion', version)
        arguments = (ref, account_id, version, stamp, request_id, task_id, job, fingerprint, cookies, identity, payload, not force)
        if enqueue:
            self.pool.submit(self._execute_sign, *arguments, settings_version=settings_version)
        else:
            self._execute_sign(*arguments, settings_version=settings_version)
        return request_id

    def _execute_sign(self, ref, account_id, version, stamp, request_id, task_id, job, fingerprint,
                      cookies, identity, payload, automatic, *, settings_version=None):
        # A manually queued job may start after revocation/relogin/removal.
        # Recheck immediately before any profile or attendance HTTP request.
        with self.vault.lock(ref):
            state, monitor, row, config, valid = self._current(ref, account_id, allow_disabled=not automatic)
            saved, _ = self.vault.get_job(ref, request_id)
            if row is None or saved is None:
                return
            valid = valid and session_stamp(row) == stamp
            cooling = payload.get('activityScope', 'course') == 'course' and bool(cooldown_seconds(config))
            valid = valid and not cooling
            if automatic:
                # Query cooldown changes must not cancel an unrelated reserved
                # group sign or a course sign when only activity lists are limited.
                valid = valid and config.get('settingsVersion', config['version']) == (
                    settings_version if settings_version is not None else version) and config['autoSign']
            if not valid:
                job.update(status='skipped', submitted=False, phase='before_submit', completedAt=iso(),
                           message='学习通操作受限，排队请求尚未发出。' if cooling else
                           '账号授权、会话或监控设置已变化，请求尚未发出，已停止本次签到。')
                self.vault.save_job(ref, job, fingerprint)
                task = next((t for t in monitor['tasks'] if t['id'] == task_id), None)
                if task:
                    task.update(status=job['status'], message=job['message'], updatedAt=iso())
                    self.vault.save_monitor(ref, monitor)
                return
            # A queued request uses the latest still-authorized CookieJar.
            cookies, identity = row['_cookies'], dict(row['_identity'])
        rate_limit = None
        try:
            raw = self.gateway.sign(cookies, identity, payload)
            if raw.get('status') not in RESULTS or not isinstance(raw.get('message'), str):
                raise ValueError('Invalid sign result')
            job.update({k: raw[k] for k in ('status', 'message', 'submitted', 'phase', 'httpStatus',
                                           'reasonCode', 'diagnostics') if k in raw})
            if payload.get('activityScope') == 'group' and raw.get('resolvedKind') == 'photo':
                job['resolvedKind'] = 'photo'
        except UpstreamError as error:
            raw = {}
            if payload.get('activityScope') == 'group' and error.details.get('resolvedKind') == 'photo':
                job['resolvedKind'] = 'photo'
            if error.code == 'upstream_rate_limited':
                rate_limit = error.details.get('retryAfterSeconds', 300)
            job.update(status='outcome_unknown' if error.details.get('submitted') else
                       'login_required' if error.code in SESSION_ERRORS else 'rejected',
                       message=str(error), submitted=bool(error.details.get('submitted')), phase=error.details.get('phase', 'presign'))
        except Exception:
            raw = {}
            job.update(status='outcome_unknown', message='未能确认签到结果，请在学习通核实；不会自动重发。')
        job['completedAt'] = iso()
        with self.vault.lock(ref):
            state, monitor, row, config, _ = self._current(ref, account_id)
            # Account removal clears its jobs; never recreate them after removal.
            if row is None or not self.vault.get_job(ref, request_id)[0]:
                return
            self.vault.save_job(ref, job, fingerprint)
            if raw.get('_diagnostic'):
                self.vault.save_diagnostic(ref, request_id, raw['_diagnostic'])
            task = next((t for t in monitor['tasks'] if t['id'] == task_id), None)
            if task:
                sync_task_result(task, job)
                self.vault.save_monitor(ref, monitor)
            if session_stamp(row) == stamp and row.get('grant') and row['status'] == 'active':
                if rate_limit is not None:
                    defer_config(config, rate_limit)
                    self.vault.save_monitor(ref, monitor)
                if job['status'] in ('login_required', 'session_expired') and row['_cookies'] == cookies:
                    row.update(status='expired', _cookies=[], sessionExpiresAt=iso())
                    config.update(enabled=False, status='login_required', message='学习通会话失效，请重新登录后开启监控。', nextCheckAt=None)
                    self.vault.save_monitor(ref, monitor)
                elif raw.get('cookies') is not None and row['_cookies'] == cookies:
                    row['_cookies'] = raw['cookies']
            event(state, row, ('自动签到：' if automatic else '签到：') + job['message'])
            self.vault.save(ref, state)
