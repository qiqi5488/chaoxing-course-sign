"""照片池命令行：rebuild / list / choose / sign。

cookie 直接从课签服务的加密库（var/sessions.sqlite3 + session.key）里取，
无需手工粘贴；也可以给 --cookie-file 指定一份 "k=v; k=v" 文本。
"""
import argparse
import json
import sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import photo_pool  # noqa: E402
from vault import Vault  # noqa: E402

DATA_DIR = str(Path(__file__).resolve().parent / 'var')


def vault_accounts():
    vault = Vault(DATA_DIR)
    seen, rows = set(), []
    with vault.connection() as db:
        vaults = [dict(r) for r in db.execute('SELECT owner, ciphertext FROM vaults').fetchall()]
    for item in vaults:
        try:
            state = vault.decrypt(item['ciphertext'], item['owner'])
        except Exception:
            continue
        for account in state.get('accounts') or []:
            if account.get('status') != 'active' or not account.get('_cookies'):
                continue
            uid = str(account['_identity']['uid'])
            if uid in seen:
                continue
            seen.add(uid)
            rows.append(account)
    return rows


def pick_account(index):
    accounts = vault_accounts()
    if not accounts:
        raise SystemExit('库里没有 active 账号')
    if index is None:
        if len(accounts) == 1:
            return accounts[0]
        lines = [f'  [{i}] {a["name"]} uid={a["_identity"]["uid"]}' for i, a in enumerate(accounts, 1)]
        raise SystemExit('有多个账号，请用 --account 指定：\n' + '\n'.join(lines))
    return accounts[index - 1]


def identity(account):
    return {'uid': account['_identity']['uid'], 'fid': account['_identity']['fid'],
            'puid': (account.get('_identity') or {}).get('puid') or account['_identity']['uid'],
            'name': account['name']}


def cmd_rebuild(args):
    account = pick_account(args.account)
    info = identity(account)
    key = f'uid{info["uid"]}'

    def progress(index, total, name, count):
        print(f'  [{index}/{total}] {name[:18]:<18} 照片 {count}')

    print(f'开始抓取 {account["name"]}（uid={info["uid"]}）的历史拍照签到…')
    pool = photo_pool.harvest(account['_cookies'], info['uid'], info['fid'], DATA_DIR, key,
                              with_images=not args.no_images, progress=progress)
    stats = pool['stats']
    print(f'\n完成：课程 {stats["courses"]}，有照片课程 {stats["coursesWithPhotos"]}，'
          f'照片 {stats["photos"]}，班级课程 {stats["classCourses"]}')
    for course_id, course in pool['courses'].items():
        flag = '班级课程' if course['classCourse'] else '普通课程'
        titles = sorted({p['title'] for p in course['photos'] if p['title']})[:4]
        print(f'  {course["courseName"][:20]:<20} [{flag}] {course["photoCount"]:>2} 张  {titles}')
    print('池文件:', photo_pool.pool_path(DATA_DIR, key))


def cmd_list(args):
    account = pick_account(args.account)
    key = f'uid{account["_identity"]["uid"]}'
    pool = photo_pool.load(DATA_DIR, key)
    if not pool:
        raise SystemExit(f'还没有照片池：{photo_pool.pool_path(DATA_DIR, key)}')
    print(json.dumps(pool['stats'], ensure_ascii=False))
    for course_id, course in pool['courses'].items():
        print(f'\n[{course_id}] {course["courseName"]} / {course["className"]} '
              f'{"(班级课程)" if course["classCourse"] else ""}')
        for photo in sorted(course['photos'], key=lambda p: p.get('startedAt') or '', reverse=True)[:8]:
            weekday = '一二三四五六日'[photo['weekday']] if photo.get('weekday') is not None else '?'
            print(f'   {photo.get("startedAt") or "-":<25} 周{weekday} {photo.get("signedAt") or "-":<12} '
                  f'{photo["title"][:16]:<16} {photo["objectId"]} {photo.get("size") or ""}')


def cmd_choose(args):
    account = pick_account(args.account)
    key = f'uid{account["_identity"]["uid"]}'
    pool = photo_pool.load(DATA_DIR, key)
    now = datetime.fromisoformat(args.at) if args.at else None
    record, reason, candidates = photo_pool.choose(pool, args.course, now=now, window=args.window)
    print('选图结果:', json.dumps(record, ensure_ascii=False, indent=2) if record else None)
    print('理由:', reason, '｜候选:', len(candidates))


def cmd_sign(args):
    account = pick_account(args.account)
    info = identity(account)
    key = f'uid{info["uid"]}'
    result = photo_pool.auto_sign(account['_cookies'], info, args.active, args.course, args.class_id,
                                  DATA_DIR, key, dry_run=not args.submit, window=args.window)
    print(json.dumps(result, ensure_ascii=False, indent=2))


def main(argv=None):
    parser = argparse.ArgumentParser(description='拍照签到照片池工具')
    sub = parser.add_subparsers(dest='command', required=True)

    p = sub.add_parser('rebuild', help='重新抓取并重建照片池')
    p.add_argument('--account', type=int, help='账号序号（1 开始）')
    p.add_argument('--no-images', action='store_true', help='只存 objId，不下载图片')
    p.set_defaults(func=cmd_rebuild)

    p = sub.add_parser('list', help='查看照片池')
    p.add_argument('--account', type=int)
    p.set_defaults(func=cmd_list)

    p = sub.add_parser('choose', help='试选一张（只读）')
    p.add_argument('--account', type=int)
    p.add_argument('--course', required=True)
    p.add_argument('--at', help='指定时刻，如 "2026-11-19 20:30"')
    p.add_argument('--window', type=int, default=90, help='时间窗口（分钟），默认 90')
    p.set_defaults(func=cmd_choose)

    p = sub.add_parser('sign', help='用池里的照片签到')
    p.add_argument('--account', type=int)
    p.add_argument('--active', required=True)
    p.add_argument('--course', required=True)
    p.add_argument('--class-id', required=True)
    p.add_argument('--window', type=int, default=90)
    p.add_argument('--submit', action='store_true', help='真的提交（默认 dry-run）')
    p.set_defaults(func=cmd_sign)

    args = parser.parse_args(argv)
    args.func(args)


if __name__ == '__main__':
    main()
