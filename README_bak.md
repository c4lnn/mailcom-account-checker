# mail.com 账号检查工具

此工具检查 mail.com 账号，并按流程清空邮箱文件夹、检查 Web 别名。它不是只读探测器，也没有 dry-run 模式：运行主脚本会对输入账号执行真实账号操作。运行前必须理解下面的安全边界和 [完整工作流说明](docs/architecture.md)。

## 环境与配置

仓库使用 Python 和 `curl_cffi`（见 `requirements.txt`）。Windows PowerShell 示例：

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
```

程序没有 CLI 参数。它从**当前工作目录**读取 `config.json`；`config.example.json` 是唯一公开配置模板。按需在本地复制并填写配置，路径相对于当前工作目录。不要提交包含真实凭据或密钥的 `config.json`。

配置需要以下八个字段，字段及类型必须与模板一致：

| 字段 | 含义 |
|---|---|
| `accounts` | 账号输入文件路径；每行 `email----password`，空行和 `#` 注释行忽略 |
| `proxies` | 代理输入文件路径；支持 `host:port`、`host:port:user:pass`、`http://…`、`https://…` |
| `output_dir` | 本次运行结果的父目录 |
| `workers` | 并发 worker 数，整数 `1`–`256` |
| `verbose` | 是否输出详细进度，布尔值 |
| `remove_aliases` | 仅为 `true` 时删除 ACTIVE 别名 |
| `change_password` | 仅为 `true` 时修改账号密码 |
| `ipinfo_key` | 出口信息配置字段，必填字符串 |

账号按 email 与密码组合去重；代理按 scheme、host、port 和认证信息去重。空代理池会在处理账号前失败。更多配置和调度细节见 [架构与错误处理](docs/architecture.md)。

## 安全警告

- IMAP 阶段会登录并清空 `Drafts`、`INBOX`、`OUTBOX`、`Sent`、`Spam`、`Trash`；该操作不可逆，`Trash` 最后处理。
- `remove_aliases: true` 会删除 ACTIVE 别名；`change_password: true` 会修改真实密码。
- `good_accounts.txt` 和 `error_accounts.txt` 含明文密码；`log.txt` 可能包含改密码前后的凭据。保护输出目录，不要复制、提交或公开这些文件。
- 中断运行可能留下 `.partial` 文件；其中可能已有明文凭据，需要人工安全清理。
- 脚本没有只读/试运行模式。没有经授权且隔离的真实运行条件时，不要执行主脚本“试一下”。完整规则见 [`AGENTS.md`](AGENTS.md)。

## 执行与结果

只有在确认输入、代理、开关、网络目的地和输出目录均获授权后，才从配置所在工作目录执行：

```powershell
.\.venv\Scripts\python.exe check_and_clean_accounts.py
```

每次运行会在 `output_dir/<YYYYmmdd_HHMMSS_mmm>/` 下创建结果：

- `good_accounts.txt`：`email----password`
- `error_accounts.txt`：`email----password----error_kind`
- `log.txt`：过程日志

结果按账号完成顺序流式写入，不保证与输入行序相同。两个账号结果文件先以 `.partial` 后缀写入；正常完成后改为最终文件名。日志文件不采用该 `.partial` 改名流程。

退出码：`0` 表示所有结果对象的 `ok` 状态成功，`1` 表示至少一个账号失败，`2` 表示配置、输入或输出错误。结果筛选文件与退出码使用不同判定逻辑，细节见 [错误与结果判定](docs/architecture.md#错误处理与结果判定)。

## 文档导航

- [模块职责、执行流程图、重试与错误处理](docs/architecture.md)
- [自动化助手与本地验证安全规则](AGENTS.md)
