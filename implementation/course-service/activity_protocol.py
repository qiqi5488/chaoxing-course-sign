"""Pure course attendance normalization; unknown requirements never imply ordinary.

Retained consumer evidence: analysis/upstream/core-protocol.txt, query.activelist,
query.taskactivelist and query.activity_details. The normal source and details
passed authenticated read-only checks in two courses on 2026-11-12; the task
source returned result=0 and remains a visible source failure.
"""
import math

from course_protocol import identifier, text


class ActivityPayloadError(ValueError):
    pass


def number(value):
    if type(value) not in (str, int, float) or value == '':
        return None
    try:
        result = float(value)
        return result if math.isfinite(result) else None
    except ValueError:
        return None


def milliseconds(value):
    result = number(value)
    if result is None or result < 0:
        return None
    return int(result * 1000 if 0 < result < 100_000_000_000 else result)


def _root(payload, *, task=False):
    if not isinstance(payload, dict):
        raise ActivityPayloadError('活动接口未返回已识别的对象。')
    # taskactivelist's numeric `status` is separate from its result flag.
    keys = ('result',) if task else ('result', 'status')
    if any(payload.get(key) in (False, 0, '0', 'false') for key in keys if key in payload):
        raise ActivityPayloadError('学习通未接受本次活动查询。')


def parse_list(payload, source):
    _root(payload, task=source == 'task')
    record = payload.get('data') if source == 'normal' else payload
    rows = record.get('activeList') if isinstance(record, dict) else None
    if not isinstance(rows, list) or len(rows) > 2000:
        raise ActivityPayloadError('活动列表格式或数量无法识别。')
    normalized = []
    for item in rows:
        if not isinstance(item, dict):
            raise ActivityPayloadError('活动列表包含无法识别的条目。')
        aid = next((identifier(item.get(key)) for key in ('activeId', 'id', 'aid', 'activeid')
                    if identifier(item.get(key))), None)
        if not aid:
            raise ActivityPayloadError('活动条目缺少有效标识。')
        if number(item.get('activeType')) not in (None, 2, 74):
            continue
        normalized.append({**item, 'activeId': aid})
    return normalized


def merge_lists(groups):
    merged = {}
    for rows in groups:
        for row in rows:
            if row['activeId'] not in merged:
                merged[row['activeId']] = dict(row)
            else:
                for key, value in row.items():
                    if merged[row['activeId']].get(key) in (None, ''):
                        merged[row['activeId']][key] = value
    return list(merged.values())


def unknown_activity(candidate, course):
    return {'activeId': candidate['activeId'], 'courseId': course['courseId'],
            'classId': course['classId'], 'name': text(candidate.get('nameOne')) or text(candidate.get('name')) or '签到活动',
            'courseName': text(course.get('name')) or '', 'kind': 'unknown', 'status': 'unknown',
            'requirements': ['unknown'], 'autoEligible': False}


def parse_detail(payload, candidate, course):
    _root(payload)
    data = payload.get('data')
    if not isinstance(data, dict):
        raise ActivityPayloadError('活动详情格式无法识别。')
    for keys, expected in ((('activeId', 'id'), candidate['activeId']),
                           (('courseId',), course['courseId']),
                           (('classId', 'clazzid'), course['classId'])):
        for key in keys:
            # The official response includes courseId:null and uses id/clazzid.
            if data.get(key) not in (None, '') and identifier(data[key]) != expected:
                raise ActivityPayloadError('活动详情与课程或活动标识不一致。')
    active_type = number(data.get('activeType', data.get('type')))
    if active_type not in (2, 74):
        if active_type is None:
            raise ActivityPayloadError('活动详情缺少可核验的签到类型。')
        return None
    result = unknown_activity(candidate, course)
    other, photo, location, face = (number(data.get(key)) for key in
                                   ('otherId', 'ifphoto', 'ifopenAddress', 'openCheckFaceFlag'))
    requirements = []
    if photo == 1:
        requirements.append('photo')
    if location == 1 or other == 4:
        requirements.append('location')
    if face == 1:
        requirements.append('face')
    kind = {2: 'qr', 3: 'gesture', 4: 'position', 5: 'code'}.get(other, 'unknown')
    if other == 2:
        requirements.append('qr')
    if other in (3, 5):
        requirements.append('code')
    if other == 0 and photo == 1:
        kind = 'photo'
    if other == 0 and photo == location == face == 0:
        kind = 'ordinary'
    if (None in (other, photo, location, face) or kind == 'unknown' or
            kind == 'gesture' and any(flag not in (0, 1) for flag in (photo, location, face))):
        requirements.append('unknown')
    start = milliseconds(data.get('starttime', data.get('startTime', candidate.get('startTime'))))
    end = milliseconds(data.get('endtime', data.get('endTime')))
    now = milliseconds(data.get('nowTime'))
    status = 'unknown'
    if now is not None and end is not None and end > 0 and now >= end:
        status = 'ended'
    elif now is not None and start is not None and start > now:
        status = 'scheduled'
    elif now is not None and start is not None and start > 0 and start <= now and end is not None and (end == 0 or now < end):
        status = 'active'
    result.update(kind=kind, status=status, requirements=requirements,
                  startTime=start, endTime=end, autoEligible=kind == 'ordinary' and status == 'active' and not requirements)
    return result
