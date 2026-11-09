# MailOps 账号检查控制台

使用 Next.js、shadcn/ui 和 Tailwind CSS，提供仪表盘、当前任务、账号、日志、代理和配置页面；隔离手动测试环境还会按 API 运行时标志显示 Mocker 测试页面。单用户登录由 FastAPI 验证，浏览器通过 Next.js 同源 `/api/*` 路由访问后端。

## 本机开发

先按 [部署说明](../docs/console-deployment.md) 设置后端环境变量并启动 API。Web 使用 Node.js 22：

```powershell
cd web
npm ci
$env:API_INTERNAL_URL = 'http://127.0.0.1:8000'
npm run dev
```

打开 `http://localhost:3000`，使用 `CONSOLE_PASSWORD` 登录。本机 HTTP 开发需为 API 设置 `CONSOLE_COOKIE_SECURE=0`。Compose 默认发布 Web 的 `3000` 端口，服务器可通过 IP:端口直接使用 HTTP，也可自行配置反向代理提供 HTTP 或 HTTPS。根据浏览器访问协议配置 `CONSOLE_COOKIE_SECURE`：HTTP 设为 `0`，HTTPS 设为 `1`（默认）。自有反向代理需保留原始 `Host` 并设置 `X-Forwarded-Proto`。API 只在 Compose 内部网络开放。

若受限 Windows 环境的 `os.userInfo()` 报权限错误，可在当前 PowerShell 设置 `$env:NODE_OPTIONS='--require=../.node-userinfo-polyfill.cjs'` 后重新运行。此兼容文件仅用于本机命令，不注入生产容器。

## 使用流程

1. 在代理页保存唯一全局代理列表；代理凭据加密保存，页面只显示配置状态和数量。修改代理只影响之后创建的任务。
2. 在账号页粘贴 `email----password`，查看逐行格式反馈。有效记录保存为待检查输入。
3. 在当前任务页选择导入记录，为本次任务设置并发、详细日志、删除别名和修改密码；确认清空文件夹及启用的删除别名、改密操作后启动任务。
4. 当前任务页实时展示进度、成功/失败账号及原始日志；日志页可读取历史任务。
5. 在配置页保存服务器级 IPinfo 密钥。改密结果不确定时，按邮箱或任务查询候选密码，核对外部邮箱状态后显式确认生效或未生效。

失败尝试独立保存，不覆盖成功凭据。刷新页面不会重提任务；SSE 断线由浏览器携带事件游标重连。导入集合保留，可显式选择再次检查，不会因任务结束自动删除。

## 验证与部署

```powershell
npm run lint
npm run test
npm run build
```

组件和路由测试只用合成数据与测试替身；实际 worker 的本地 HTTP/IMAP mock 测试在仓库根目录执行 `python -m pytest tests`。

生产部署使用根目录 Docker Compose，具体步骤见 [部署说明](../docs/console-deployment.md)。
