"""Public history snapshots and separately labelled local receipts."""
from vault import iso


def official_snapshot(result, account_id):
    """Whitelist display fields; never serialize raw upstream records or cookies."""
    fields = ('accountId', 'activeId', 'activityScope', 'status', 'statusLabel',
              'name', 'courseName', 'kind', 'message', 'recordedAt', 'source')
    records = {}
    for row in result['records']:
        if (row['accountId'] != account_id or row['source'] != 'chaoxing'
                or row['status'] != 'already_signed' or row['activityScope'] not in ('course', 'group')):
            raise ValueError('Invalid official attendance record')
        records[(row['activityScope'], row['activeId'])] = {key: row.get(key) for key in fields}
    ordered = sorted(records.values(), key=lambda row: (row['recordedAt'] or '', row['activeId']), reverse=True)
    source_fields = ('activityScope', 'courseId', 'classId', 'name', 'status', 'total', 'returned', 'count', 'message')
    return {'accountId': account_id, 'records': ordered, 'total': len(ordered), 'source': 'chaoxing',
            'counts': {scope: sum(row['activityScope'] == scope for row in ordered) for scope in ('course', 'group')},
            'sources': [{key: source.get(key) for key in source_fields} for source in result['sources']],
            'fetchedAt': result.get('fetchedAt') or iso()}


def history_page(snapshot, offset, limit):
    end = offset + limit
    return {**snapshot, 'records': snapshot['records'][offset:end],
            'nextOffset': end if end < snapshot['total'] else None}


def successful_records(vault, token, account_id, *, offset=0, limit=50):
    jobs = [job for job in vault.account_jobs(token, account_id)
            if job.get('status') in ('reported_success', 'already_signed')]
    jobs.sort(key=lambda job: (job.get('completedAt') or job.get('createdAt') or '', job['requestId']), reverse=True)
    tasks = {(task.get('activityScope', 'course'), task['activeId']): task
             for task in vault.load_monitor(token)['tasks'] if task['accountId'] == account_id}
    records, seen = [], set()
    for job in jobs:
        scope = job.get('activityScope', 'course')
        key = (scope, job['activeId'])
        if key in seen:
            continue
        seen.add(key)
        task = tasks.get(key, {})
        # Never expose the raw job: it may hold challenge or diagnostic fields.
        records.append({
            'requestId': job['requestId'], 'accountId': account_id, 'activeId': job['activeId'],
            'activityScope': scope, 'status': job['status'],
            'name': job.get('name') or task.get('name') or ('群聊签到' if scope == 'group' else '课程签到'),
            'courseName': job.get('courseName') or task.get('courseName') or '',
            'kind': 'photo' if scope == 'group' and job.get('resolvedKind') == 'photo'
                    else job.get('kind') or task.get('kind') or 'unknown',
            'message': job.get('message') or '',
            'recordedAt': job.get('completedAt') or job.get('createdAt') or None,
        })
    end = offset + limit
    return {'accountId': account_id, 'records': records[offset:end], 'total': len(records), 'source': 'local',
            'nextOffset': end if end < len(records) else None, 'fetchedAt': iso()}
