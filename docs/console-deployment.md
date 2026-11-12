# 控制台部署配置

控制台由 Next.js Web、FastAPI API/worker 和 SQLite 组成。Docker Compose 直接发布 Next.js Web 端口；可通过服务器 IP 和端口使用 HTTP，也可由部署者自己的反向代理提供 HTTP 或 HTTPS。FastAPI API 只在 Compose 内部网络可达。

仓库提供 Docker Compose 单机部署，不包含反向代理、TLS 终止或证书管理。Web 主机端口默认为 `3000`，可通过 `WEB_PORT` 修改。API 不发布主机端口；SQLite 保存在 `account-data` 持久卷中。

## 首次配置

本机启动 API 时创建虚拟环境并安装依赖（Windows PowerShell）：

```powershell
python -m venv .venv
.venv\Scripts\python.exe -m pip install -r requirements.txt
```

Linux 服务器上的 Docker Compose 从仓库根目录的 `.env` 读取配置。复制模板并生成两个密钥（只依赖 Python 3 标准库）：

```sh
cp .env.example .env
python3 -c 'import base64, secrets; print(base64.urlsafe_b64encode(secrets.token_bytes(32)).decode())'
python3 -c 'import secrets; print(secrets.token_urlsafe(48))'
```

第一条命令的输出填入 `ACCOUNT_CHECKER_ENCRYPTION_KEY`，第二条填入 `CONSOLE_SESSION_SECRET`。编辑文件，设置这两个值和自定义的 `CONSOLE_PASSWORD`：

```sh
nano .env
chmod 600 .env
```

`.env.example` 已列出 Compose 需要的变量；真实 `.env` 已加入 Git 忽略规则，不要提交密钥：

| 变量 | 用途 |
| --- | --- |
| `CONSOLE_PASSWORD` | 单用户登录密码 |
| `CONSOLE_SESSION_SECRET` | 至少 32 字符的会话签名密钥 |
| `ACCOUNT_CHECKER_ENCRYPTION_KEY` | Fernet 格式的凭据加密密钥 |
| `ACCOUNT_CHECKER_DB` | SQLite 文件路径；默认 `data/account-checker.sqlite3` |
| `WEB_PORT` | Compose 发布到主机的 Web 端口；默认 `3000` |
| `CONSOLE_COOKIE_SECURE` | 会话 cookie 的 Secure 属性；默认 `1`，HTTP 访问设为 `0`，HTTPS 访问设为 `1` |

## Docker Compose 部署

完成上方 Linux `.env` 配置后，按 HTTP/HTTPS 访问方式设置 `CONSOLE_COOKIE_SECURE`，需要时设置 `WEB_PORT`。启动服务：

```sh
docker compose up -d --build
docker compose ps
docker compose logs -f api web
```

直接访问时使用 `http://服务器IP:${WEB_PORT:-3000}`，并将 `CONSOLE_COOKIE_SECURE=0`。如果使用自己的反向代理，由代理转发到服务器 Web 端口；代理需保留原始 `Host`，并设置 `X-Forwarded-Proto` 为浏览器所用协议。经 HTTPS 访问时设 `CONSOLE_COOKIE_SECURE=1`；若代理对外使用 HTTP，则设为 `0`。API 仅在 Compose 私有网络内暴露，不要给 `api` 增加主机端口映射。`account-data` 卷保存 SQLite。更新时再次执行 `docker compose up -d --build`。停止服务使用 `docker compose down`，不要加 `-v`，否则会删除数据库卷。

部署前先在隔离测试中使用合成账号和本地 mocker 验证版本，不要把合成账号交给生产 worker。真实任务会清空所选邮箱文件夹，必须在控制台逐项确认。

在本机启动 API 的命令：

```powershell
.venv\Scripts\python.exe -m uvicorn account_console.api:app --host 127.0.0.1 --port 8000
```

API 启动时初始化 schema，并将遗留的排队/运行中任务标为中断；不会自动重跑邮箱操作。

Web 本机启动与操作流程见 [Web README](../web/README.md)。代理在独立页面维护为唯一的全局列表，认证信息加密保存；页面只显示配置状态和数量，不回显凭据。任务创建时固定当时的代理列表，后续代理修改只影响新任务。任务选项依次为并发、详细日志、清空文件夹确认、删除别名、修改密码；清空文件夹必须单独确认，别名删除和改密默认关闭，勾选即授权对应操作。IPinfo 密钥保留在配置页。

## 隔离的手动 Mocker 环境

手动测试环境使用独立 Compose 文件、`.env.mocker`、`mocker-data` 命名卷和 SQLite 文件，不共享生产 `account-data`。只使用预置或导入的 `@example.test` 合成账号；API 会拒绝其他域名。worker 将 HTTP 与 IMAP TLS 目标校验为 loopback，并阻断外部 HTTP、代理、重定向和 socket 出口。该运行模式不是生产任务选项。

在仓库根目录创建本地配置并设置独立密钥：

```powershell
Copy-Item .env.mocker.example .env.mocker
$key = & .venv\Scripts\python.exe -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
$session = & .venv\Scripts\python.exe -c "import secrets; print(secrets.token_urlsafe(48))"
```

将 `$key`、`$session` 和仅供本机使用的 `CONSOLE_PASSWORD` 写入 `.env.mocker`。该文件已被 Git 忽略；不要复制生产密钥、真实账号或代理。

启动与访问：

```powershell
docker compose --env-file .env.mocker -f docker-compose.mocker.yml up -d --build
docker compose --env-file .env.mocker -f docker-compose.mocker.yml ps
```

默认访问 `http://localhost:3001`。API 不发布主机端口；Web 从 API 的运行时标志显示 Mocker 页面，不依赖另一份前端构建。测试数据库位于独立 `mocker-data` 卷。停止时保留该卷：

```powershell
docker compose --env-file .env.mocker -f docker-compose.mocker.yml down
```

控制台预置 `good@example.test` 与 `bad@example.test` 合成记录；成功场景凭据为 `GoodPassword!`，`bad@example.test` 的导入密码故意与 mock 服务密码不符，用于登录失败场景。也可在账号页导入其他 `@example.test` 记录。

在导航的「Mocker 测试」页选择预设场景，或选择随机模式并填写 seed、操作范围及 0 到 1 的故障概率。预设场景还可模拟代理预检失败后切换、代理传输中断后重试及代理候选耗尽；随机操作可选择 `proxy_preflight` 和 `proxy_transport`。这些故障只注入合成候选的调度/本地 IMAP 连接流程，不会连接假代理或外部网络。随机模式中的别名删除和改密仍需在任务页单独启用并确认。任务详情保留场景配置和实际计划，可复制 JSON；终态任务可按原计划重放。相同 seed、账号、操作范围和概率生成稳定计划，计划按账号、操作、候选序号和操作序号定位故障。

`worker_interrupted` 会让隔离 worker 进程异常退出，API 将任务标记为中断。`api_restart` 会让专用测试 API 在 worker 进入运行态后以退出码 70 退出；Compose 的 `restart: unless-stopped` 会重启 API，启动恢复将任务标记为中断，用户可刷新页面并创建后续任务。API 手动运行时，需由操作者重新启动进程。两种场景都不会自动重跑邮箱操作；已提交的改密候选会转为待核实。

生产 Compose 不设置 `MAIL_MOCKER_MODE`，因此不显示该导航；直接访问测试页或 Mocker 配置 API 会被拒绝。不要在生产服务设置该变量。测试结束后，如需删除测试数据库，再明确删除隔离卷：

```powershell
docker compose --env-file .env.mocker -f docker-compose.mocker.yml down -v
```

## 日志、结果与异常恢复

Web worker 每产生一条事件就写入 SQLite `job_logs`，每完成一个账号就提交其结果和凭据；当前任务页和日志页通过 SSE 读取持久化事件。Web 任务不向原脚本的 `log.txt` 写入，原 CLI 的文件输出行为保留。日志事件按原始内容保存和传输，页面按 `check_and_clean_accounts.py` 的行式格式展示；日志可能包含敏感信息，只允许已登录用户读取。

改密前先加密保存候选密码，再提交外部请求。明确成功后更新成功凭据；明确拒绝保留失败候选；超时、断连或进程中断后保留为待核实，不自动再次提交。候选密码可在账号页按邮箱查询，或在任务结果中查询。人工核对后选择生效才更新成功凭据，选择未生效保持原凭据。

已提交事务的数据可在进程重启后读取。API 停止或崩溃后，worker 通过父进程管道关闭检测退出；worker 意外退出时，API 将该任务标记为中断并恢复尚未确认的候选。卷丢失、磁盘故障等仍需备份恢复。每次只运行一个 API 实例、一个 Uvicorn worker；API 的子进程执行单个任务，禁止启动多个实例共同使用数据库，以免启动恢复将其他实例的任务误判为中断。

## 备份与恢复

先停止任务并停止 API 和 worker，使用 SQLite backup API 导出一致性备份。本机命令：

```powershell
.venv\Scripts\python.exe -m account_console.backup D:\secure-backups\account-checker.sqlite3
```

Compose 部署可让一次性容器挂载原持久卷生成备份，再复制到主机：

```sh
docker compose stop api
docker compose run --rm --no-deps api python -m account_console.backup /data/console-backup.sqlite3
docker compose cp api:/data/console-backup.sqlite3 ./console-backup.sqlite3
docker compose start api
```

数据库备份和 `ACCOUNT_CHECKER_ENCRYPTION_KEY` 必须分开备份并限制读取权限。恢复时停止服务，把备份文件恢复到 `ACCOUNT_CHECKER_DB` 指定位置，并同时恢复原加密密钥；没有原密钥无法解密已保存凭据。不要在密钥丢失时生成新密钥覆盖旧数据库。

密钥轮换需要先读取旧数据库并重新加密每个凭据字段，当前版本没有自动轮换工具；轮换前应停服务并另存数据库和旧密钥备份。

## 测试依赖

```powershell
.venv\Scripts\python.exe -m pip install -r requirements-dev.txt
.venv\Scripts\python.exe -m pytest
```

测试启动本地 HTTP 和 IMAP over TLS mocker，模拟登录拒绝、网络错误、清理/复查与别名失败、改密成功/拒绝、提交前后断连、超时和 worker 进程中断。网络守卫阻断非本地出口，即使业务代码捕获异常也会令测试失败。测试不使用真实账号或真实 mail.com 网络。

完成 Web 构建后，可额外执行真实 Next.js → API → worker → 本地 mocker 的跨服务验收：

```powershell
$env:RUN_WEB_SMOKE='1'
.venv\Scripts\python.exe -m pytest tests/test_web_smoke.py -q
```

该用例在临时目录创建合成数据库，使用随机本机端口启动服务，测试后自动停止。默认后端测试跳过此用例，避免在未构建 Web 的环境失败。该验收覆盖 HTTP 链路，不包含浏览器视觉验收或容器 TLS/持久卷重启验证。
