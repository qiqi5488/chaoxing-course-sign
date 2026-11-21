"""照片池选图规则测试：课程级随机、班级课程按星期几+时间就近。"""
import random
from datetime import datetime, timedelta, timezone

import photo_pool

CST = timezone(timedelta(hours=8))


def photo(active_id, weekday, minutes, title='签到'):
    return {'activeId': active_id, 'objectId': 'a' * 32, 'weekday': weekday,
            'minutes': minutes, 'title': title}


def pool(course_id, photos, class_course):
    return {'courses': {str(course_id): {'courseId': str(course_id), 'courseName': 'c',
                                         'classCourse': class_course, 'photos': photos,
                                         'photoCount': len(photos)}}}


def test_normal_course_picks_from_all_photos():
    photos = [photo('1', 0, 480), photo('2', 2, 900), photo('3', 4, 1200)]
    chosen, reason, candidates = photo_pool.choose(pool(7, photos, False), 7, rng=random.Random(0))
    assert reason == 'course:random'
    assert len(candidates) == 3
    assert chosen in photos


def test_class_course_prefers_same_weekday_and_near_time():
    photos = [photo('mon-morning', 0, 480), photo('wed-morning', 2, 500),
              photo('wed-evening', 2, 1200), photo('fri-morning', 4, 480)]
    now = datetime(2026, 11, 18, 8, 30, tzinfo=CST)  # 周三 08:30
    chosen, reason, candidates = photo_pool.choose(pool(9, photos, True), 9, now=now,
                                                  window=90, rng=random.Random(1))
    assert reason == 'class_course:same_weekday_near_time'
    assert [item['activeId'] for item in candidates] == ['wed-morning']
    assert chosen['activeId'] == 'wed-morning'


def test_class_course_falls_back_to_same_weekday():
    photos = [photo('wed-early', 2, 300), photo('fri', 4, 480)]
    now = datetime(2026, 11, 18, 20, 0, tzinfo=CST)  # 周三 20:00，离 05:00 太远
    chosen, reason, candidates = photo_pool.choose(pool(9, photos, True), 9, now=now,
                                                  window=90, rng=random.Random(2))
    assert reason == 'class_course:same_weekday'
    assert chosen['activeId'] == 'wed-early'


def test_class_course_falls_back_to_any_photo():
    photos = [photo('mon', 0, 480), photo('tue', 1, 600)]
    now = datetime(2026, 11, 18, 9, 0, tzinfo=CST)  # 周三，没有任何周三的照片
    chosen, reason, _ = photo_pool.choose(pool(9, photos, True), 9, now=now, rng=random.Random(3))
    assert reason == 'class_course:fallback_any'
    assert chosen in photos


def test_window_boundary_is_inclusive():
    photos = [photo('edge', 2, 600)]
    now = datetime(2026, 11, 18, 11, 30, tzinfo=CST)  # 690 分钟，差 90
    _, reason, _ = photo_pool.choose(pool(9, photos, True), 9, now=now, window=90,
                                     rng=random.Random(0))
    assert reason == 'class_course:same_weekday_near_time'
    now = datetime(2026, 11, 18, 11, 31, tzinfo=CST)  # 差 91
    _, reason, _ = photo_pool.choose(pool(9, photos, True), 9, now=now, window=90,
                                     rng=random.Random(0))
    assert reason == 'class_course:same_weekday'


def test_missing_pool_or_course_reports_reason():
    assert photo_pool.choose(None, 1) == (None, 'no_pool', [])
    chosen, reason, candidates = photo_pool.choose(pool(1, [], False), 1)
    assert chosen is None and reason == 'no_photos_for_course' and candidates == []
    chosen, reason, _ = photo_pool.choose(pool(1, [photo('x', 0, 0)], False), 2)
    assert chosen is None and reason == 'no_photos_for_course'


def test_parse_signtime_variants():
    weekday, minutes = photo_pool.parse_signtime('11-18 08:18')
    assert weekday == 2 and minutes == 8 * 60 + 18  # 无年份时按当前年（2026-11-18 是周三）
    weekday, minutes = photo_pool.parse_signtime('2026-11-18 20:05')
    assert weekday == 2 and minutes == 20 * 60 + 5
    assert photo_pool.parse_signtime('签到成功') == (None, None)


def _seed_pool(tmp_path, course_id, photos, class_course=False):
    pool = {'accountKey': 'uid1', 'uid': '1', 'fid': '1', 'builtAt': '2026-11-19T00:00:00+08:00',
            'stats': {'courses': 1, 'coursesWithPhotos': 1, 'photos': len(photos),
                      'classCourses': 1 if class_course else 0},
            'courses': {str(course_id): {'courseId': str(course_id), 'classId': '9',
                                         'courseName': 'c', 'className': 'k',
                                         'classCourse': class_course, 'photoCount': len(photos),
                                         'photos': photos}}}
    photo_pool.save(pool, str(tmp_path), 'uid1')
    return pool


def test_auto_sign_dry_run_picks_photo_and_never_submits(tmp_path, monkeypatch):
    photos = [photo('111', 2, 480), photo('222', 2, 1210)]
    _seed_pool(tmp_path, 7, photos, class_course=True)
    monkeypatch.setattr(photo_pool, 'verify', lambda *a, **k: {'signed': False, 'objectId': None,
                                                              'matches': False, 'httpStatus': 200})
    calls = []
    monkeypatch.setattr(photo_pool, 'submit', lambda *a, **k: calls.append(k) or {
        'status': 'dry_run', 'fields': {}, 'submitted': False})

    account = {'uid': '1', 'fid': '1', 'name': 'n'}
    result = photo_pool.auto_sign([], account, '999', 7, '9', str(tmp_path), 'uid1',
                                  dry_run=True, now=datetime(2026, 11, 18, 8, 0, tzinfo=CST))
    assert result['status'] == 'dry_run'
    assert result['reasonOfPick'] == 'class_course:same_weekday_near_time'
    assert result['picked']['activeId'] == '111'
    assert calls and calls[0]['dry_run'] is True


def test_auto_sign_skips_when_already_signed(tmp_path, monkeypatch):
    _seed_pool(tmp_path, 7, [photo('111', 2, 480)], class_course=False)
    monkeypatch.setattr(photo_pool, 'verify', lambda *a, **k: {'signed': True, 'objectId': 'x' * 32,
                                                              'matches': False, 'httpStatus': 200})
    monkeypatch.setattr(photo_pool, 'submit', lambda *a, **k: (_ for _ in ()).throw(AssertionError('must not submit')))
    result = photo_pool.auto_sign([], {'uid': '1', 'fid': '1'}, '999', 7, '9', str(tmp_path), 'uid1',
                                  dry_run=False)
    assert result['status'] == 'already_signed'


def test_auto_sign_without_pool_reports_reason(tmp_path):
    result = photo_pool.auto_sign([], {'uid': '1', 'fid': '1'}, '999', 7, '9', str(tmp_path), 'uid1')
    assert result['status'] == 'no_photo_available'
    assert result['reason'] == 'no_pool'
