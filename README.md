# MailOps 账号检查器

MailOps 用于检查 mail.com 账号，包含原有的 Python 命令行工具和单用户 Web 控制台。控制台基于 Next.js、shadcn/ui、Tailwind CSS 和 FastAPI，支持导入账号、执行任务、查看实时及历史日志、管理检查结果和保存任务配置。

## 功能

- 检查账号登录状态，并按成功和失败尝试分别保存结果；失败凭据不会覆盖已验证的成功凭据。
- 可配置代理、并发数、别名处理和密码修改等任务选项。
- Web 任务日志、执行结果和改密候选密码写入 SQLite；任务中断后可查看已保存数据。
- 改密状态不确定时保留候选密码供人工核对，不会自动重试改密请求。
- 原 Python CLI 保持文件输入和批次输出方式。

## Web 控制台部署

需要安装 Docker Engine 和 Docker Compose。登录 Linux 服务器，在仓库根目录准备环境文件并生成密钥：

```sh
cp .env.example .env
python3 -c 'import base64, secrets; print(base64.urlsafe_b64encode(secrets.token_bytes(32)).decode())'
python3 -c 'import secrets; print(secrets.token_urlsafe(48))'
```

第一条 Python 命令生成 `ACCOUNT_CHECKER_ENCRYPTION_KEY`，第二条生成 `CONSOLE_SESSION_SECRET`。编辑 `.env`，填入这两个值和你自行设置的 `CONSOLE_PASSWORD`：

```sh
nano .env
chmod 600 .env
```

`CONSOLE_COOKIE_SECURE` 默认是 `1`。若直接通过 HTTP 访问，在 `.env` 中将其设为 `0`；HTTPS 入口（由你自己的反向代理提供）保持 `1`。完整的 HTTP/HTTPS 配置、升级、备份与恢复步骤见[控制台部署说明](docs/console-deployment.md)。

启动服务：

```sh
docker compose up -d --build
docker compose ps
```

默认通过 `http://服务器IP:3000` 访问。API 仅在 Compose 内部网络开放。SQLite 数据保存在 `account-data` 卷中。妥善备份数据库和原 `ACCOUNT_CHECKER_ENCRYPTION_KEY`；丢失密钥后无法解密已保存的凭据。

## 本机开发

Python 环境安装及 API 启动方式见[控制台部署说明](docs/console-deployment.md)，Web 开发命令和使用流程见 [Web README](web/README.md)。Web 需 Node.js 22。

运行 Web 检查：

```powershell
cd web
npm ci
npm run lint
npm run test
npm run build
```

运行 Python 测试：

```powershell
.venv\Scripts\python.exe -m pip install -r requirements-dev.txt
.venv\Scripts\python.exe -m pytest tests
```

测试使用本地 mocker 和合成数据，不需要真实邮箱账号。

## 隔离手动 Mocker 测试

除自动化测试外，可用独立 Compose 环境从共享控制台手动测试本地 mail mocker。该环境使用独立数据库，只接受 `@example.test` 合成账号，并在 worker 中强制阻断非 loopback 网络。

```powershell
Copy-Item .env.mocker.example .env.mocker
# 在 .env.mocker 中设置独立的加密密钥、会话密钥和本机登录密码
docker compose --env-file .env.mocker -f docker-compose.mocker.yml up -d --build
```

默认打开 `http://localhost:3001`。测试环境配置、场景、seed、任务计划重放及停止方式见[控制台部署说明](docs/console-deployment.md#隔离的手动-mocker-环境)。不要使用真实邮箱账号或生产密钥。

## Python 命令行工具

在仓库根目录复制配置模板，并按需编辑 `config.json`：

```powershell
Copy-Item config.example.json config.json
```

准备配置中指定的账号文件和代理文件，然后运行：

```powershell
.venv\Scripts\python.exe -m pip install -r requirements.txt
.venv\Scripts\python.exe check_and_clean_accounts.py
```

账号文件每行一条记录，格式为 `email----password`；代理格式按配置和客户端支持的代理 URL 填写。每次运行在配置指定的输出目录下创建独立批次目录，包含 `good_accounts.txt`、`error_accounts.txt` 和 `log.txt`。CLI 配置项见 [config.example.json](config.example.json)。

## 重要操作说明

检查任务会清空所选邮箱的 `Drafts`、`INBOX`、`OUTBOX`、`Sent`、`Spam` 和 `Trash` 文件夹，启动前必须明确确认。删除别名和修改密码默认关闭；勾选对应任务选项即授权该操作，并会对邮箱产生实际影响。不要使用真实邮箱账号运行 mocker 测试。

日志按原始内容保存，可能包含敏感信息。数据库凭据使用 `ACCOUNT_CHECKER_ENCRYPTION_KEY` 加密；请妥善备份数据库和原密钥。丢失加密密钥将无法解密已保存的凭据。不要提交 `.env`、`config.json`、账号文件、代理文件或运行输出。
