# 课签

学习通账号、课程、签到活动与个人签到记录管理项目。Python 后端提供 API 和静态前端，Node.js 子进程接收实时消息。

## 目录

- `implementation/course-service`：FastAPI 后端、会话存储、活动监控与测试。
- `implementation/course-web/dist`：可直接运行的静态前端，无需构建。
- `implementation/course-web/tests`：前端测试。
- `implementation/chaoxing-direct`：学习通 HTTP 客户端及协议模块。

三个组件需要保持当前相对目录结构。

## 本地启动

需要 Python 3.10+ 和 Node.js 22+。在仓库根目录执行：

```powershell
cd implementation/course-service
python -m pip install -r requirements.txt
npm ci --ignore-scripts
python -m uvicorn server:create_app --factory --host 127.0.0.1 --port 8000 --workers 1 --no-access-log
```

打开 <http://127.0.0.1:8000/>。前后端由同一个服务提供；安装依赖后也可运行 `implementation/course-service/start-local.cmd`。

交互接口文档位于 `/api/docs`，OpenAPI 位于 `/api/openapi.json`。

## 配置与数据

- `COURSE_PUBLIC_ORIGIN`：网站来源，默认 `http://127.0.0.1:8000`；改变端口时同步设置。
- `COURSE_DATA_DIR`：运行数据目录，默认 `implementation/course-service/var`。
- `COURSE_SESSION_KEY`：可选的 Base64 编码 32 字节会话加密密钥。未设置时首次启动自动创建 `session.key`。

会话密钥与 SQLite 数据库需要一起备份。仓库不包含已有账号、Cookie、会话密钥、数据库、照片、抓包、日志、APK 或本地研究资料；新环境需要自行登录。已有本机运行数据不受 Git 操作影响。

部署时使用单个 Uvicorn worker，并将运行数据放在网站公开目录之外。公网访问需要 HTTPS 和正确的 `COURSE_PUBLIC_ORIGIN`；反向代理配置可参考后端目录中的 `nginx.example.conf`。

## 测试

在仓库根目录执行：

```powershell
python -m pip install -r implementation/course-service/requirements-dev.txt
python -m pytest implementation/course-service/tests implementation/chaoxing-direct -q
node --test implementation/course-web/tests/*.test.mjs
node --test implementation/course-service/tests/im-receiver.test.cjs
```

测试使用模拟请求与临时数据。测试通过不代表所有签到类型均已通过真实活动验收，活动要求、权限与最终结果仍以学习通返回为准。课程同学签到照片的自动获取尚未验证可用。

前端的 jsQR 第三方许可证和来源说明保留在 `implementation/course-web/dist/vendor`。
