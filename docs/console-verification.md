# 控制台验证记录

验证日期：2026-11-06。变更：`account-checker-web-console`。

## 已完成

- Python 测试含跨服务验收共 39 项通过，使用合成账号与本机 HTTP / IMAP over TLS mocker；网络守卫阻断非本地请求。
- 全新 `npm ci` 安装成功；随后 Web 组件与 API 路由测试 14 项通过，`npm run lint`、`npm run build` 通过。
- Next.js standalone 服务经真实 HTTP 链路完成登录、五路由访问控制、导入、任务执行、SSE 实时流与游标续传、结果分表及候选密码持久化验收。
- 超过一批的 603 条日志能够读完；原始日志值保持不变，未登录访问日志被拒绝。
- 改密明确成功/拒绝、提交前后断连、请求超时、强制结束 worker、父进程管道关闭均有验证。待核实候选不覆盖旧成功凭据，不自动重放改密。
- 别名删除失败、删除后复查失败和邮箱文件夹清理/复查失败不会重放已有副作用。
- `openspec validate account-checker-web-console --strict` 通过。

## 环境限制与待完成项

- 先前版本的 Docker 集成验收使用独立 Compose 项目、临时合成凭据和本机隔离网络；验证过 Next.js、API、worker、SQLite 持久化、HTTPS 反向代理链路及本地测试 CA。该记录描述上一版部署基线，不代表当前 Compose 内置入口或证书管理要求。
- 上一版验收中，API 启动的真实 worker 子进程执行隔离合成任务：3 个账号为 1 成功、2 失败；48 条日志支持 SSE 全量读取和 Last-Event-ID 续传。候选密码加密存储且可查询，服务端已改密但响应丢失时标为待核实，改密请求没有自动重放；另验证了任务中断后的恢复。
- 上一版 Compose 普通重启及相同命名卷重建后，账号、任务结果、候选密码、原始日志和配置均保持一致；未完成任务标记中断、提交中的候选恢复为待核实。SQLite `integrity_check=ok`，`foreign_key_check` 无违规。
- 当前 Compose 不包含 HTTP/TLS 反向代理或证书管理，直接发布可配置的 Web 主机端口。按 `remove-caddy` 变更重建的 API/Web 镜像启动通过；验收配置仅有 `api`、`web` 两个服务，Web 发布到临时主机端口 `13000`，API 无主机端口映射。直连 HTTP（Secure cookie 关闭）和独立本机 HTTPS 测试反代（Secure cookie 开启）均完成登录、受保护页面访问及跨源写入拒绝测试；TLS 反代使用临时本地测试证书，不涉及应用部署证书管理。
- `remove-caddy` 隔离部署的持久化验收使用 `--network none` 的一次性 worker 容器和 `example.test` 合成账号，通过本地邮件 mocker 完成 3 个账号的任务（1 成功、2 失败）、48 条原始日志及 2 次改密请求；确认服务端提交后响应丢失的候选密码保持可查且状态为待核实。写入合成配置后重启 API，成功账号、失败记录、任务结果、候选密码、日志和配置均保留；运行中任务被标记为中断，已提交的候选密码转为待核实。SQLite `integrity_check=ok`，`foreign_key_check` 无违规；容器检查确认 API `8000/tcp` 无主机端口映射，只有 Web 的 `13000` 端口发布。邮件请求由本地 mocker 处理，worker 容器无网络连接，未触发真实 mail.com 请求。
- 当前环境没有可用浏览器控制主机，未完成浏览器视觉与响应式验收；组件测试与 HTTPS HTTP 链路验收不能替代视觉检查。
- 依赖审计仍有 2 项 low、13 项 moderate 报告，涉及 CLI、静态分析及测试工具依赖；无 high / critical。未执行强制升级。生产依赖审计涉及 shadcn CLI 依赖树，仍应在后续依赖更新时处理。
- 后端 TestClient 有 Starlette 对 httpx 的弃用提示，当前测试通过。

## 复现命令

```powershell
cd web
npm ci
npm run lint
npm run test
npm run build
cd ..
$env:RUN_WEB_SMOKE='1'
.venv\Scripts\python.exe -m pytest tests -q
openspec.cmd validate account-checker-web-console --strict
```

后端测试依赖先按 `requirements-dev.txt` 安装。受限 Windows 的 Node 兼容设置见 Web README。运行跨服务用例前应先完成 Web 构建；测试自动创建临时数据库并停止本例启动的服务。
