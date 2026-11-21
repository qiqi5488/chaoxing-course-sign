"""拍照签到照片池：抓取历史签到照片、按课程分类、按场景选图、提交并复查。

设计要点（与实现一一对应）：
1. 照片来源只能是"本账号自己"的历史签到记录 —— 学习通服务端不提供他人学号->照片
   的读取入口（已实测：preSign 的 uid 参数不生效，教师统计页与学生名册对纯学生 403/500）。
2. objId 不绑定活动，因此历史照片可以用于任意新的拍照签到。
3. 普通课程：在该课程的历史照片里随机取一张即可。
4. 班级课程（一个课程里混着多门课/多位老师发起的签到，`classCourse=True`）：
   新签到出现时，优先取"同一星期几 + 提交时间接近"的历史照片，避免把上午英语课的照片
   用在下傍晚体育课上。
"""
import json
import os
import random
import re
import struct
import threading
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import requests

MOBILELEARN = 'https://mobilelearn.chaoxing.com'
USER_AGENT = ('Mozilla/5.0 (Linux; Android 13; Mobile) AppleWebKit/537.36 (KHTML, like Gecko) '
              'Version/4.0 Chrome/112.0.0.0 Mobile Safari/537.36 (device:generic) Language/zh_CN '
              'com.chaoxing.mobile/ChaoXingStudy_3_7.0.6_android_phone_11041025_33 (Android)')
CST = timezone(timedelta(hours=8))
PHOTO_RE = re.compile(r'zsign_photo[\s\S]*?objId="([0-9a-f]{32})"[\s\S]*?src="([^"]+)"')
SIGNED_RE = re.compile(r'id="statuscontent"')
SIGNTIME_RE = re.compile(r'class="signtime"\s*>([\s\S]*?)</p>')
FORM_RE = re.compile(r'id="signButton"')
# 同课程里出现这么多不同的签到标题，就认为它是"班级课程"（多门课混在一个课程下）
CLASS_COURSE_TITLES = 4
DEFAULT_WINDOW_MINUTES = 90

_locks = {}
_locks_guard = threading.Lock()


def account_lock(key):
    with _locks_guard:
        return _locks.setdefault(str(key), threading.Lock())


def session_from_cookies(cookies):
    session = requests.Session()
    session.headers.update({'User-Agent': USER_AGENT, 'Accept-Language': 'zh-CN,zh-Hans;q=0.9',
                            'Referer': MOBILELEARN + '/'})
    for cookie in cookies or []:
        name, value = cookie.get('name'), cookie.get('value')
        if not name or value is None:
            continue
        domain = cookie.get('domain') or '.chaoxing.com'
        session.cookies.set(name, value, domain=domain, path=cookie.get('path') or '/')
    return session


def _get(session, path, params=None, timeout=20):
    url = path if path.startswith('http') else MOBILELEARN + path
    response = session.get(url, params=params, timeout=timeout, allow_redirects=False)
    time.sleep(0.35)
    return response


def _image_info(data):
    if data[:8] == b'\x89PNG\r\n\x1a\n':
        width, height = struct.unpack('>II', data[16:24])
        return 'PNG', f'{width}x{height}'
    if data[:2] == b'\xff\xd8':
        index = 2
        while index < len(data) - 9:
            if data[index] != 0xFF:
                index += 1
                continue
            if data[index + 1] in (0xC0, 0xC1, 0xC2):
                height, width = struct.unpack('>HH', data[index + 5:index + 9])
                return 'JPEG', f'{width}x{height}'
            index += 2 + struct.unpack('>H', data[index + 2:index + 4])[0]
        return 'JPEG', '?'
    return '?', '?'


def _text(value):
    return re.sub(r'<[^>]+>', '', value or '').strip()


def parse_signtime(text):
    """把页面上的 '11-18 08:18' / '2026-11-18 08:18' 解析成 (weekday, minutes)。"""
    text = (text or '').strip()
    match = re.search(r'(?:(\d{4})-)?(\d{1,2})-(\d{1,2})\s+(\d{1,2}):(\d{2})', text)
    if not match:
        return None, None
    year = int(match.group(1)) if match.group(1) else datetime.now(CST).year
    month, day, hour, minute = (int(match.group(i)) for i in (2, 3, 4, 5))
    try:
        moment = datetime(year, month, day, hour, minute, tzinfo=CST)
    except ValueError:
        return None, None
    return moment.weekday(), hour * 60 + minute


def _course_catalog(session):
    response = _get(session, 'https://mooc1-api.chaoxing.com/mycourse/backclazzdata',
                    {'view': 'json', 'rss': '1'})
    catalog = []
    for channel in (response.json() or {}).get('channelList') or []:
        content = channel.get('content') or {}
        courses = (content.get('course') or {}).get('data') or []
        course = courses[0] if courses else {}
        class_id, course_id = content.get('id'), course.get('id')
        if not (str(class_id or '').isdigit() and str(course_id or '').isdigit()):
            continue
        catalog.append({'courseId': str(course_id), 'classId': str(class_id),
                        'courseName': course.get('name') or '', 'className': content.get('name') or ''})
    return catalog


def _activity_ids(session, course):
    found = {}
    for path in ('/v2/apis/active/student/activelist', '/ppt/activeAPI/taskactivelist'):
        try:
            payload = _get(session, path, {'fid': '0', 'courseId': course['courseId'],
                                           'classId': course['classId']}).json()
        except Exception:
            continue
        rows = []
        if isinstance(payload, dict):
            if isinstance(payload.get('activeList'), list):
                rows += payload['activeList']
            data = payload.get('data')
            if isinstance(data, dict) and isinstance(data.get('activeList'), list):
                rows += data['activeList']
        for row in rows:
            if isinstance(row, dict):
                active_id = row.get('activeId') or row.get('id')
                if active_id is not None and re.fullmatch(r'\d{6,}', str(active_id)):
                    found.setdefault(str(active_id), row.get('nameOne') or row.get('name') or '')
    return found


def harvest(cookies, uid, fid, data_dir, account_key, with_images=True, limit=None, progress=None):
    """抓该账号所有课程里"我签过且带照片"的记录，落盘成照片池。"""
    session = session_from_cookies(cookies)
    pool = {'accountKey': str(account_key), 'uid': str(uid), 'fid': str(fid),
            'builtAt': datetime.now(CST).isoformat(), 'courses': {}, 'stats': {}}
    images_dir = Path(data_dir) / 'photo_pool' / str(account_key)
    images_dir.mkdir(parents=True, exist_ok=True)

    catalog = _course_catalog(session)
    for index, course in enumerate(catalog, 1):
        activities = _activity_ids(session, course)
        entry = {'courseId': course['courseId'], 'classId': course['classId'],
                 'courseName': course['courseName'], 'className': course['className'],
                 'photos': []}
        for active_id in activities:
            if limit is not None and len(entry['photos']) >= limit:
                break
            detail = _get(session, '/v2/apis/active/getPPTActiveInfo', {'activeId': active_id})
            try:
                data = (detail.json() or {}).get('data') or {}
            except Exception:
                data = {}
            if str(data.get('ifphoto')) != '1':
                continue
            page = _get(session, '/newsign/preSign',
                        {'courseId': course['courseId'], 'classId': course['classId'],
                         'activePrimaryId': active_id, 'general': '1', 'sys': '1', 'ls': '1',
                         'appType': '15', 'uid': str(uid), 'isTeacherViewOpen': '0'})
            match = PHOTO_RE.search(page.text or '')
            if not match:
                continue
            object_id = match.group(1)
            signtime = SIGNTIME_RE.search(page.text or '')
            weekday, minutes = parse_signtime(_text(signtime.group(1)) if signtime else '')
            start_ms = data.get('starttime') or data.get('startTime')
            started = None
            try:
                start_ms = int(start_ms)
                if start_ms and start_ms < 10 ** 11:
                    start_ms *= 1000
                if start_ms:
                    started = datetime.fromtimestamp(start_ms / 1000, CST)
                    if weekday is None:
                        weekday, minutes = started.weekday(), started.hour * 60 + started.minute
            except (TypeError, ValueError, OSError):
                started = None
            record = {'activeId': active_id, 'title': data.get('name') or '',
                      'startedAt': started.isoformat() if started else None,
                      'signedAt': _text(signtime.group(1)) if signtime else '',
                      'weekday': weekday, 'minutes': minutes,
                      'objectId': object_id, 'status': data.get('status'),
                      'attendNum': data.get('attendNum')}
            if with_images:
                image = session.get(f'https://p.cldisk.com/star3/origin/{object_id}',
                                    headers={'Referer': MOBILELEARN + '/'}, timeout=40,
                                    allow_redirects=True)
                if image.status_code == 200 and image.headers.get('Content-Type', '').startswith('image/'):
                    kind, size = _image_info(image.content)
                    filename = f'{active_id}_{object_id[:8]}.jpg'
                    (images_dir / filename).write_bytes(image.content)
                    record.update({'file': filename, 'bytes': len(image.content),
                                   'type': kind, 'size': size})
            entry['photos'].append(record)
        titles = {photo['title'] for photo in entry['photos'] if photo['title']}
        entry['classCourse'] = len(titles) >= CLASS_COURSE_TITLES
        entry['photoCount'] = len(entry['photos'])
        if entry['photos']:
            pool['courses'][course['courseId']] = entry
        if progress:
            progress(index, len(catalog), course['courseName'], entry['photoCount'])

    pool['stats'] = {'courses': len(catalog), 'coursesWithPhotos': len(pool['courses']),
                     'photos': sum(c['photoCount'] for c in pool['courses'].values()),
                     'classCourses': sum(1 for c in pool['courses'].values() if c['classCourse'])}
    save(pool, data_dir, account_key)
    return pool


def pool_path(data_dir, account_key):
    return Path(data_dir) / 'photo_pool' / f'{account_key}.json'


def save(pool, data_dir, account_key):
    path = pool_path(data_dir, account_key)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(pool, ensure_ascii=False, indent=2), encoding='utf-8')
    return path


def load(data_dir, account_key):
    path = pool_path(data_dir, account_key)
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text(encoding='utf-8'))
    except (OSError, ValueError):
        return None


def _clock(moment):
    return moment.weekday(), moment.hour * 60 + moment.minute


def choose(pool, course_id, now=None, window=DEFAULT_WINDOW_MINUTES, rng=random):
    """按课程选一张历史照片；班级课程优先"同星期几 + 时间接近"。"""
    if not pool:
        return None, 'no_pool', []
    course = (pool.get('courses') or {}).get(str(course_id))
    if not course or not course.get('photos'):
        return None, 'no_photos_for_course', []
    photos = course['photos']
    now = now or datetime.now(CST)
    weekday, minutes = _clock(now)
    if course.get('classCourse'):
        same_weekday = [p for p in photos if p.get('weekday') == weekday]
        near = [p for p in same_weekday if p.get('minutes') is not None
                and abs(p['minutes'] - minutes) <= window]
        if near:
            return rng.choice(near), 'class_course:same_weekday_near_time', near
        if same_weekday:
            return rng.choice(same_weekday), 'class_course:same_weekday', same_weekday
        return rng.choice(photos), 'class_course:fallback_any', photos
    return rng.choice(photos), 'course:random', photos


def submit(session, account, active_id, course_id, class_id, object_id,
           photo_result='', dry_run=True, name='', device_code=''):
    """提交拍照签到（默认 dry-run，只打印表单）。"""
    fields = {'activeId': str(active_id), 'courseId': str(course_id), 'uid': str(account['uid']),
              'clientip': '', 'useragent': '', 'latitude': '-1', 'longitude': '-1',
              'appType': '15', 'fid': str(account['fid']), 'objectId': str(object_id),
              'name': name, 'validate': '', 'deviceCode': device_code,
              'photoResult': photo_result}
    result = {'fields': fields, 'submitted': False, 'status': 'dry_run', 'response': ''}
    if dry_run:
        return result
    headers = {'Origin': MOBILELEARN, 'X-Requested-With': 'XMLHttpRequest',
               'Content-Type': 'application/x-www-form-urlencoded; charset=UTF-8',
               'Referer': f'{MOBILELEARN}/newsign/preSign?activeId={active_id}'}
    response = session.post(MOBILELEARN + '/pptSign/stuSignajax', data=fields,
                            headers=headers, timeout=25, allow_redirects=False)
    result.update(submitted=True, response=(response.text or '')[:400],
                  httpStatus=response.status_code)
    body = (response.text or '').strip()
    if body in ('success', 'success2') or '签到成功' in body:
        result['status'] = 'reported_success'
    elif body in ('已签', '已签到', '已经签到') or '您已签到过了' in body:
        result['status'] = 'already_signed'
    elif body == 'validate':
        result['status'] = 'requires_validation'
    else:
        result['status'] = 'unknown'
    return result


def verify(session, account, active_id, course_id, class_id, object_id):
    """复查：页面出现 statuscontent 且 objId 与提交的一致。"""
    page = _get(session, '/newsign/preSign',
                {'courseId': course_id, 'classId': class_id, 'activePrimaryId': active_id,
                 'general': '1', 'sys': '1', 'ls': '1', 'appType': '15',
                 'uid': str(account['uid']), 'isTeacherViewOpen': '0'})
    text = page.text or ''
    signed = bool(SIGNED_RE.search(text))
    match = PHOTO_RE.search(text)
    return {'signed': signed, 'objectId': match.group(1) if match else None,
            'matches': bool(match and match.group(1) == str(object_id)),
            'httpStatus': page.status_code}


def auto_sign(cookies, account, active_id, course_id, class_id, data_dir, account_key,
              dry_run=True, window=DEFAULT_WINDOW_MINUTES, rng=random, now=None):
    """选图 + 提交 + 复查，一站式。"""
    pool = load(data_dir, account_key)
    record, reason, candidates = choose(pool, course_id, now=now, window=window, rng=rng)
    if record is None:
        return {'status': 'no_photo_available', 'reason': reason, 'candidates': 0}
    session = session_from_cookies(cookies)
    before = verify(session, account, active_id, course_id, class_id, record['objectId'])
    if before['signed']:
        return {'status': 'already_signed', 'reason': 'pre_sign_shows_signed',
                'picked': record, 'reasonOfPick': reason, 'candidates': len(candidates)}
    submitted = submit(session, account, active_id, course_id, class_id,
                       record['objectId'], dry_run=dry_run)
    after = None
    if not dry_run:
        after = verify(session, account, active_id, course_id, class_id, record['objectId'])
    return {'status': submitted['status'], 'picked': record, 'reasonOfPick': reason,
            'candidates': len(candidates), 'submit': submitted, 'verify': after,
            'window': window}
