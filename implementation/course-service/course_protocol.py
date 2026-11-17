"""Bounded, pure normalization of observed course JSON; no network requests."""
import re
from urllib.parse import parse_qs, urlsplit, urlunsplit

OFFICIAL_DOMAINS = ('chaoxing.com', 'cxwfw.com', 'xuexitong.com')


class CoursePayloadError(ValueError):
    pass


def text(value, limit=300):
    if not isinstance(value, (str, int, float)) or isinstance(value, bool):
        return None
    value = ''.join(c for c in str(value).strip() if ord(c) >= 32)
    return value[:limit] or None


def identifier(value):
    value = text(value, 32)
    return value if value and re.fullmatch(r'[1-9]\d{0,29}', value) else None


def first(record, *keys, limit=300):
    return next((result for key in keys if (result := text(record.get(key), limit))), None)


def image_url(value):
    if not isinstance(value, str) or len(value) > 2048 or any(ord(c) <= 32 for c in value):
        return None
    try:
        parsed = urlsplit(value)
        host = (parsed.hostname or '').lower()
        if (parsed.scheme not in ('http', 'https') or parsed.username or parsed.password
                or parsed.port not in (None, 80 if parsed.scheme == 'http' else 443)
                or not any(host == d or host.endswith('.' + d) for d in OFFICIAL_DOMAINS)):
            return None
        return urlunsplit(('https', host, parsed.path, parsed.query, ''))
    except ValueError:
        return None


def display_profile(payload):
    record = payload.get('msg', {}) if isinstance(payload, dict) else {}
    if not isinstance(record, dict):
        return {}
    number = first(record, 'studentNumber', 'studentNum', 'studentnumber', limit=64)
    return {
        'name': first(record, 'name'),
        'school': first(record, 'schoolName', 'schoolname', 'fidName', 'school'),
        'studentNumberMasked': (number[:2] + '****' + number[-2:]) if number and len(number) > 4 else ('****' if number else None),
        'avatarUrl': image_url(first(record, 'pic', 'avatar', 'avatarUrl')),
        'department': first(record, 'departmentName', 'department', 'deptName'),
        'major': first(record, 'majorName', 'major'),
    }


def _row(course_id, class_id, course, clazz, source, *, name=None, teacher=None, cover=None):
    return {
        'key': course_id + '_' + (class_id or ''), 'courseId': course_id, 'classId': class_id or '',
        'name': name or first(course, 'name', 'title'),
        'teacher': teacher or first(course, 'teacherfactor', 'teacherName', 'teacher'),
        'className': first(clazz, 'name', 'className'),
        'term': first(clazz, 'termName', 'semesterName', 'term') or first(course, 'termName', 'semesterName', 'term'),
        'coverUrl': image_url(cover or first(course, 'imageurl', 'imageUrl', 'coverUrl')),
        'status': None,
        'statusCode': next((value for value in (clazz.get('state'), course.get('coursestate')) if type(value) in (int, str)), None),
        'sourceLabels': [source],
    }


def _check_root(payload):
    if not isinstance(payload, dict):
        raise CoursePayloadError('课程接口未返回对象。')
    for key in ('result', 'status'):
        if key in payload and payload[key] in (False, 0, '0', 'false'):
            raise CoursePayloadError('课程接口未接受本次查询。')


def parse_normal(payload):
    _check_root(payload)
    channels = payload.get('channelList')
    if not isinstance(channels, list):
        raise CoursePayloadError('普通课程列表格式无法识别。')
    rows = []
    for item in channels[:2000]:
        content = item.get('content') if isinstance(item, dict) else None
        if not isinstance(content, dict) or 'course' not in content:
            continue
        courses = content['course'].get('data') if isinstance(content['course'], dict) else None
        if not isinstance(courses, list) or not courses or not isinstance(courses[0], dict):
            raise CoursePayloadError('普通课程条目缺少课程数据。')
        course = courses[0]
        try:
            query = parse_qs(urlsplit(str(course.get('courseSquareUrl') or '')).query)
        except ValueError:
            raise CoursePayloadError('普通课程条目的地址字段无法识别。') from None
        course_id = identifier((query.get('courseId') or [None])[0]) or identifier(course.get('id'))
        class_id = identifier((query.get('classId') or [None])[0]) or identifier(content.get('id'))
        if not course_id:
            raise CoursePayloadError('普通课程条目缺少有效课程标识。')
        rows.append(_row(course_id, class_id, course, content, '普通课程'))
    return rows


def parse_mirror(payload):
    _check_root(payload)
    data = payload.get('data')
    if not isinstance(data, list):
        raise CoursePayloadError('镜像课程列表格式无法识别。')
    if not data:
        return []
    clazz = data[0].get('clazz') if isinstance(data[0], dict) else None
    entries = clazz.get('data') if isinstance(clazz, dict) else None
    if not isinstance(entries, list):
        raise CoursePayloadError('镜像课程列表格式无法识别。')
    rows = []
    for item in entries[:2000]:
        if not isinstance(item, dict):
            raise CoursePayloadError('镜像课程条目格式无法识别。')
        cid = identifier(item.get('courseid'))
        class_id = identifier(str(item.get('id') or '').split('_')[-1])
        course_data = item.get('course', {}).get('data') if isinstance(item.get('course'), dict) else None
        if not cid or not isinstance(course_data, list) or not course_data or not isinstance(course_data[0], dict):
            raise CoursePayloadError('镜像课程条目缺少课程数据。')
        rows.append(_row(cid, class_id, course_data[0], item, '镜像课程'))
    return rows


def parse_meet(payload):
    _check_root(payload)
    data = payload.get('data')
    entries = data.get('listData') if isinstance(data, dict) else None
    if not isinstance(entries, list):
        raise CoursePayloadError('泛雅课堂列表格式无法识别。')
    rows = []
    for meeting in entries[:2000]:
        if not isinstance(meeting, dict):
            raise CoursePayloadError('泛雅课堂条目格式无法识别。')
        classes = meeting.get('meetClasses')
        if not isinstance(classes, list):
            raise CoursePayloadError('泛雅课堂条目缺少班级数据。')
        for clazz in classes[:1000]:
            if not isinstance(clazz, dict):
                raise CoursePayloadError('泛雅课堂班级格式无法识别。')
            cid, class_id = identifier(clazz.get('courseId')), identifier(clazz.get('id'))
            if not cid or not class_id:
                raise CoursePayloadError('泛雅课堂条目缺少课程或班级标识。')
            rows.append(_row(cid, class_id, {}, clazz, '泛雅课堂',
                name=first(clazz, 'title', 'formatTitle') or first(meeting, 'title'),
                teacher=first(clazz, 'createrName') or first(meeting, 'creatorName'),
                cover=first(clazz, 'meetIcon') or first(meeting, 'iconUrl')))
    return rows


def merge_courses(groups):
    merged = {}
    for rows in groups:
        for row in rows:
            key = row['key']
            if key not in merged:
                merged[key] = dict(row, sourceLabels=list(row['sourceLabels']))
                continue
            current = merged[key]
            for field in ('name', 'teacher', 'className', 'term', 'coverUrl', 'status', 'statusCode'):
                if current[field] in (None, '') and row[field] not in (None, ''):
                    current[field] = row[field]
            current['sourceLabels'] = list(dict.fromkeys(current['sourceLabels'] + row['sourceLabels']))
    return list(merged.values())
