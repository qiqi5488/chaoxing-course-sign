# 拍照签到照片池（photo pool）

把"账号自己历史签到照片"变成可复用资源，并在新的拍照签到出现时**按课程与场景自动选图提交**。

## 1. 能力边界（先说清）

- ✅ 可获取：**本账号**全部历史拍照签到记录里的 `objectId` 与原图。
- ❌ 不可获取：**别人**的 `objectId`。实测结论：`newsign/preSign` 的 `uid`/`puid`/`stuid`/`userid`/`personid` 等参数全部无效（永远渲染登录者本人），教师统计页 `pcTeaSignController/endSign` 对学生返回「暂无权限」，成员名册接口 403 / IP 白名单 / 500。
- ✅ 可复用：`objectId` 不绑定活动，历史照片可用于任意**新的**拍照签到；服务端不校验照片归属，`photoResult` 传空串也可通过。
- ⚠️ 只适用于纯拍照签到：`ifphoto=1` 且 `ifopenAddress=0`、`openCheckFaceFlag=0`、`ifNeedVCode=0`。位置签、人脸签、防作弊验证码需另外的凭据。

## 2. 数据落盘

```
var/photo_pool/
  uid<uid>.json          # 照片池清单
  uid<uid>/              # 原图
    <activeId>_<objId前8位>.jpg
```

池结构（`uid<uid>.json`）：

```jsonc
{
  "accountKey": "uid431153958",
  "uid": "431153958", "fid": "11019",
  "builtAt": "2026-11-19T20:2x:xx+08:00",
  "stats": {"courses": 8, "coursesWithPhotos": 3, "photos": 51, "classCourses": 1},
  "courses": {
    "268075489": {
      "courseId": "268075489", "classId": "157195926",
      "courseName": "26级人工智能技术应用7、8、9班",
      "classCourse": true,                       // 一个课程里混着多门课 → 需要时间匹配
      "photoCount": 48,
      "photos": [
        {"activeId": "…", "title": "789班 信息技术（机房）",
         "startedAt": "2026-11-11T16:07:58+08:00", "signedAt": "11-11 16:08",
         "weekday": 2, "minutes": 967,            // 周三 16:07
         "objectId": "115e502c…", "file": "….jpg",
         "bytes": 622423, "type": "JPEG", "size": "873x1920"}
      ]
    }
  }
}
```

## 3. 分类与选图规则

**分类**：按 `courseId` 分组；若同一课程下出现 **≥4 种不同的签到标题**，标记为 `classCourse=True`（班级课程——多门课/多位老师共用一个课程壳）。

**选图** `photo_pool.choose(pool, courseId, now, window=90)`：

| 课程类型 | 规则 | 返回的 reason |
|---|---|---|
| 普通课程 | 在该课程历史照片里**随机**取一张 | `course:random` |
| 班级课程 | ① 同星期几且提交时间差 ≤ `window` 分钟 → 随机 | `class_course:same_weekday_near_time` |
| | ② 同星期几 → 随机 | `class_course:same_weekday` |
| | ③ 兜底：任意一张 → 随机 | `class_course:fallback_any` |

> 目的：班级课程里"上午英语课"和"傍晚体育课"的照片不能混用，所以要按 **星期几 + 时间窗** 匹配。

## 4. HTTP 接口

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/api/accounts/{id}/photo-pool` | 池状态：`ready` / `stats` / 每课程 `classCourse` 与照片数 / 最近抓取任务 |
| POST | `/api/accounts/{id}/photo-pool/rebuild` | 后台重建（202，同一账号同时只跑一个任务） |
| POST | `/api/accounts/{id}/photo-sign/auto` | 选图签到。body：`{activeId, courseId, classId, dryRun?, window?}`，**`dryRun` 默认 true** |

`photo-sign/auto` 返回：`status`（`dry_run` / `reported_success` / `already_signed` / `requires_validation` / `no_photo_available` / …）、`picked`（选中的照片与 `reasonOfPick`、`candidates` 数）、`submit`（表单与响应）、`verify`（复查结果：页面是否出现 `statuscontent`、`objId` 是否与提交一致）。

## 5. 登录即抓取

`/api/accounts/login` 与 `/api/accounts/{id}/relogin` 成功后，服务会在**后台线程**里跑一次 `harvest`（不阻塞登录响应），响应里带 `photoPool: {state: running|done|error, stats}`。可随时用 `rebuild` 手动重跑。

## 6. 命令行

```powershell
cd implementation\course-service
# 抓取（cookie 自动从 var/sessions.sqlite3 解密，无需粘贴）
python photo_pool_cli.py rebuild --account 1
python photo_pool_cli.py rebuild --account 2 --no-images   # 只存 objId

python photo_pool_cli.py list --account 1
python photo_pool_cli.py choose --account 1 --course 268075489 --at "2026-11-19 20:30"
python photo_pool_cli.py sign --account 1 --active <aid> --course <cid> --class-id <kid>          # dry-run
python photo_pool_cli.py sign --account 1 --active <aid> --course <cid> --class-id <kid> --submit # 真提交
```

## 7. 安全默认

1. `dryRun` 默认 `true`，必须显式关掉才提交；
2. 提交前先 `preSign` 复查，**已签就直接返回 `already_signed`**，不重复提交；
3. 提交后再次复查，`verify.matches` 为 `true` 才算真成功；
4. 每个账号一把进程内锁（`photo_pool.account_lock`），抓取与提交不并发；
5. 接口带限流：`photo-pool` 6 次/分、`photo-sign` 10 次/分。

## 8. 测试

```powershell
python -m pytest tests/test_photo_pool.py -q
```

覆盖：普通课程随机、班级课程"同星期几+接近时间"优先、同星期几回退、任意回退、窗口边界（90 分钟含端点）、空池/无课程、`signtime` 解析。
