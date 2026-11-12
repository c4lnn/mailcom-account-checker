# 控制台部署配置

控制台由 Next.js Web、FastAPI API/worker 和 SQLite 组成。Docker Compose 直接发布 Next.js Web 端口；可通过服务器 IP 和端口使用 HTTP，也可由部署者自己的反向代理提供 HTTP 或 HTTPS。FastAPI API 只在 Compose 内部网络可达。

仓库提供单一 Docker Compose 部署入口，一次运行生产或 Mocker 中的一种模式。Web 主机端口默认为 `3000`，可通过 `WEB_PORT` 修改；API 只在 Compose 内部网络可达。生产和 Mocker 使用独立命名卷，分别映射到容器内 `/data`。

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
| `WEB_PORT` | Compose 发布到主机的 Web 端口；默认 `3000` |
| `CONSOLE_COOKIE_SECURE` | 会话 cookie 的 Secure 属性；默认 `1`，HTTP 访问设为 `0`，HTTPS 访问设为 `1` |
| `MAIL_MOCKER_MODE` | `0` 为生产，`1` 为隔离 Mocker；默认 `0` |
| `COMPOSE_PROJECT_NAME` | 稳定的 Compose 项目名和卷命名空间；同一主机多部署时必须唯一 |

## Docker Compose 部署

完成 `.env` 配置后，设置 `MAIL_MOCKER_MODE=0` 启动生产，或设为 `1` 启动隔离 Mocker。两种模式共用 `CONSOLE_PASSWORD`、`CONSOLE_SESSION_SECRET` 和 `ACCOUNT_CHECKER_ENCRYPTION_KEY`；不要把 Mocker 部署暴露给非受信用户。直接 HTTP 访问需设 `CONSOLE_COOKIE_SECURE=0`，HTTPS 设为 `1`。同一主机运行多份部署时，为每份部署设置唯一且稳定的 `COMPOSE_PROJECT_NAME`。

```sh
docker compose up -d --build
docker compose ps
docker compose logs -f api web
```

模式切换时先运行 `docker compose down`（不要加 `-v`），修改 `.env` 中的 `MAIL_MOCKER_MODE`，再运行 `docker compose up -d --build`。两种模式使用同一 Web 端口和地址，必须一次只运行一种。Compose 在切换时可能提示另一个模式的命名卷未匹配当前配置并保留该卷，这是预期行为；确认卷名后继续，勿手动删除。`down -v` 会删除当前模式卷，禁止在模式切换或升级时使用。

### 从现有双 Compose 部署迁移

新卷名为 `${COMPOSE_PROJECT_NAME}_account-data-0`（生产）和 `${COMPOSE_PROJECT_NAME}_account-data-1`（Mocker）。旧版默认卷名为 `${COMPOSE_PROJECT_NAME}_account-data` 和 `${COMPOSE_PROJECT_NAME}_mocker-data`。升级时先记录旧 Compose 项目名，并确认新 `.env` 的 `COMPOSE_PROJECT_NAME` 与其一致；替换旧 Compose 文件前停止此前运行过的生产和 Mocker 服务，保留旧卷。更新文件后按下方命令分别备份旧数据库，再迁移到对应模式卷。复制前确认目标数据库不存在；SQLite backup API 会读取一致性快照，不会改写旧卷，旧卷可用于回滚。

升级前、替换旧部署文件之前，停止曾运行过的栈（Mocker 命令仅在旧 Mocker 栈曾启动时执行）：

```sh
docker compose down
docker compose --env-file .env.mocker -f docker-compose.mocker.yml down
```

新 Compose 配置可将旧卷只读挂载并用 SQLite backup API 导出到主机的受保护目录（先创建 `/secure-backups`；生产和 Mocker 分别执行，按当前模式选择旧卷和旧数据库文件名）：

```sh
PROJECT="mailcom-account-checker" # 与 .env 中的 COMPOSE_PROJECT_NAME 一致；自定义时同步修改
docker compose --env-file .env --project-name "$PROJECT" run --rm --no-deps -T -e ACCOUNT_CHECKER_DB=/legacy/account-checker.sqlite3 -v "${PROJECT}_account-data:/legacy:ro" -v /secure-backups:/backup --entrypoint python api -m account_console.backup /backup/production.sqlite3
```

备份 Mocker 时，将 `.env` 的 `MAIL_MOCKER_MODE` 设为 `1`，将旧卷改为 `${PROJECT}_mocker-data`、`ACCOUNT_CHECKER_DB` 改为 `/legacy/mocker-account-checker.sqlite3`，并将备份文件名改为 `mocker.sqlite3`。

生产迁移：将 `.env` 的 `MAIL_MOCKER_MODE` 设为 `0`，执行：

```sh
PROJECT="mailcom-account-checker" # 与 .env 中的 COMPOSE_PROJECT_NAME 一致；自定义时同步修改
docker compose --env-file .env --project-name "$PROJECT" run --rm --no-deps -T -v "${PROJECT}_account-data:/legacy:ro" --entrypoint python api -c '
import sqlite3
from pathlib import Path
s = Path("/legacy/account-checker.sqlite3")
t = Path("/data/account-checker.sqlite3")
assert s.is_file() and not t.exists()
src = sqlite3.connect(f"file:{s}?mode=ro", uri=True)
dst = sqlite3.connect(t)
src.backup(dst)
dst.close()
src.close()
'
```

Mocker 迁移：将 `.env` 的 `MAIL_MOCKER_MODE` 设为 `1`，运行同一命令，但把旧卷改为 `${PROJECT}_mocker-data`，并把源路径改为 `/legacy/mocker-account-checker.sqlite3`。目标路径仍为 `/data/account-checker.sqlite3`。若旧部署使用自定义项目名，使用其实际卷名；先用 `docker volume ls` 核对，不要猜测卷名。迁移后设回所需模式，启动服务并核对账号、任务和日志；验证完成前保留旧卷，以便回滚。

### Mocker 模式

Mocker 模式复用同一套 API/Web 服务和 `.env`，但使用独立数据卷。API 只允许合成 `@example.test` 账号；worker 网络守卫阻断外部 HTTP、代理、重定向和 socket 出口。Mocker 与生产共用密钥、密码和会话签名；数据卷隔离不隔离这些秘密的泄露影响。同主机名下有效期内的会话 cookie 在模式切换后仍可能有效。

结束测试后使用 `docker compose down`；不要使用 `-v`，除非明确要永久删除当前模式的数据卷。

在本机启动 API 的命令：

```powershell
.venv\Scripts\python.exe -m uvicorn account_console.api:app --host 127.0.0.1 --port 8000
```

API 启动时初始化 schema，并将遗留的排队/运行中任务标为中断；不会自动重跑邮箱操作。

Web 本机启动与操作流程见 [Web README](../web/README.md)。代理在独立页面维护为唯一的全局列表，认证信息加密保存；页面只显示配置状态和数量，不回显凭据。任务创建时固定当时的代理列表，后续代理修改只影响新任务。任务选项依次为并发、详细日志、清空文件夹确认、删除别名、修改密码；清空文件夹必须单独确认，别名删除和改密默认关闭，勾选即授权对应操作。IPinfo 密钥保留在配置页。

## Mocker 故障场景

控制台预置 `good@example.test` 与 `bad@example.test` 合成记录；成功场景凭据为 `GoodPassword!`，`bad@example.test` 的导入密码故意与 mock 服务密码不符，用于登录失败场景。也可在账号页导入其他 `@example.test` 记录。

在导航的「Mocker 测试」页选择预设场景，或选择随机模式并填写 seed、操作范围及 0 到 1 的故障概率。预设场景还可模拟代理预检失败后切换、代理传输中断后重试及代理候选耗尽；随机操作可选择 `proxy_preflight` 和 `proxy_transport`。这些故障只注入合成候选的调度/本地 IMAP 连接流程，不会连接假代理或外部网络。随机模式中的别名删除和改密仍需在任务页单独启用并确认。任务详情保留场景配置和实际计划，可复制 JSON；终态任务可按原计划重放。相同 seed、账号、操作范围和概率生成稳定计划，计划按账号、操作、候选序号和操作序号定位故障。

`worker_interrupted` 会让隔离 worker 进程异常退出，API 将任务标记为中断。`api_restart` 会让专用测试 API 在 worker 进入运行态后以退出码 70 退出；Compose 的 `restart: unless-stopped` 会重启 API，启动恢复将任务标记为中断，用户可刷新页面并创建后续任务。API 手动运行时，需由操作者重新启动进程。两种场景都不会自动重跑邮箱操作；已提交的改密候选会转为待核实。

端到端本地 Mocker 验收见下方测试说明。

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
