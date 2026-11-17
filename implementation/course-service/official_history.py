"""Read-only personal attendance results from the official course statistics page.

Source: /page/sign/signStatisticsDetail's getActiveList and dealSignStatus.
The caller verifies the session identity and supplies only owned courses.
"""
from datetime import datetime, timedelta, timezone
import re

from activity_protocol import ActivityPayloadError
from course_protocol import identifier, text

COURSE_HISTORY_PATH = '/v2/apis/signStat/getActiveList'
COURSE_SUMMARY_PATH = '/v2/apis/signStat/getUserStat2'
PAGE_SIZE = 9999
SIGNED = {1: '已签', 2: '教师代签'}
KINDS = {0: 'ordinary', 2: 'qr', 3: 'gesture', 4: 'position', 5: 'code'}


def _integer(value):
    if type(value) is int:
        return value
    if isinstance(value, str) and re.fullmatch(r'[0-9]{1,12}', value):
        return int(value)
    return None


def _sign_time(value):
    if not isinstance(value, str) or not re.fullmatch(
            r'\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}(?:\.\d{1,6})?', value):
        return None
    try:
        moment = datetime.fromisoformat(value).replace(tzinfo=timezone(timedelta(hours=8)))
        return moment.astimezone(timezone.utc).isoformat().replace('+00:00', 'Z')
    except ValueError:
        return None


def parse_course_history(payload, course, account_id):
    course_id, class_id = identifier(course.get('courseId')), identifier(course.get('classId'))
    if not course_id or not class_id:
        raise ActivityPayloadError('课程历史查询缺少有效课程或班级标识。')
    source = {
        'activityScope': 'course', 'courseId': course_id, 'classId': class_id,
        'name': text(course.get('name')) or '课程', 'status': 'unavailable',
        'total': None, 'returned': None, 'count': 0, 'message': '学习通未返回可核验的个人签到记录。',
    }
    result = {'records': [], 'source': source}
    if not isinstance(payload, dict) or _integer(payload.get('result')) != 1:
        if isinstance(payload, dict) and payload.get('errorMsg') == '无此用户数据':
            source['message'] = '学习通返回“无此用户数据”，尚未确认该课程的历史记录。'
        return result
    data = payload.get('data')
    rows = data.get('list') if isinstance(data, dict) else None
    total = _integer(data.get('total')) if isinstance(data, dict) else None
    if not isinstance(rows, list) or len(rows) > PAGE_SIZE or total is None or total < len(rows):
        return result
    source.update(total=total, returned=len(rows), status='ok', message='已读取学习通个人签到记录。')
    partial = len(rows) != total or _integer(data.get('page')) != 1
    seen, duplicate = set(), set()
    for row in rows:
        aid = identifier(row.get('activeid')) if isinstance(row, dict) else None
        if aid is None:
            partial = True
            continue
        if aid in seen:
            duplicate.add(aid)
            partial = True
        seen.add(aid)
        # `status` describes the activity, whereas `userStatus` is this user's result.
        status = _integer(row.get('userStatus'))
        if status not in SIGNED:
            continue
        if _integer(row.get('activeType')) != 2:
            partial = True
            continue
        label = SIGNED[status]
        result['records'].append({
            'accountId': account_id, 'activeId': aid, 'activityScope': 'course',
            'status': 'already_signed', 'statusLabel': label,
            'name': text(row.get('name')) or '课程签到', 'courseName': source['name'],
            'kind': KINDS.get(_integer(row.get('other_id')), 'unknown'),
            'message': '学习通记录：' + label, 'recordedAt': _sign_time(row.get('submittime')),
            'source': 'chaoxing',
        })
    # Conflicting duplicate states cannot establish the latest personal result.
    result['records'] = [row for row in result['records'] if row['activeId'] not in duplicate]
    source['count'] = len(result['records'])
    if partial:
        source.update(status='partial', message='已读取部分个人签到记录，尚未确认完整历史。')
    return result


def fetch_course_history(client, puid, courses, account_id, read_json):
    """Use the official page's bounded read; auth and rate-limit errors propagate."""
    if not identifier(puid):
        raise ActivityPayloadError('课程历史查询缺少已核验的用户标识。')
    records, sources = [], []
    for course in courses:
        params = {
            'courseId': course['courseId'], 'classId': course['classId'], 'puid': str(puid),
            'pageSize': PAGE_SIZE, 'duid': '', 'denc': '',
        }
        try:
            payload = read_json(client, COURSE_HISTORY_PATH, params)
        except ActivityPayloadError:
            payload = None
        result = parse_course_history(payload, course, account_id)
        if (isinstance(payload, dict) and _integer(payload.get('result')) == 0
                and payload.get('errorMsg') == '无此用户数据'):
            try:
                summary = read_json(client, COURSE_SUMMARY_PATH,
                                    {key: value for key, value in params.items() if key != 'pageSize'})
            except ActivityPayloadError:
                summary = None
            data = summary.get('data') if isinstance(summary, dict) else None
            counts = ('effectiveAttendance', 'earlyCount', 'lateCount', 'absenceCount',
                      'sickCount', 'publicCount', 'personnalCount', 'overdueCount')
            if (isinstance(data, dict) and _integer(summary.get('result')) == 1
                    and identifier(data.get('uid')) == str(puid)
                    and identifier(data.get('classId')) == result['source']['classId']
                    and _integer(data.get('allCount')) == _integer(data.get('attendanceCount')) == 0
                    and all(_integer(data[key]) == 0 for key in counts if key in data)):
                result['source'].update(status='ok', total=0, returned=0,
                                        message='学习通确认没有签到活动。')
        records.extend(result['records'])
        sources.append(result['source'])
    return {'records': records, 'sources': sources}
