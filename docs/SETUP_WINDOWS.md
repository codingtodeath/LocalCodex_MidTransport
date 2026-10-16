# Windows 来源设备配置

## 运行前准备

- Windows 10/11，64 位 Python 3.11+，含 tkinter、venv 和 pip。
- 系统已启用 OpenSSH 客户端，`ssh -V` 可运行。
- 官方 Codex 已登录想用于中转的账号，存在该用户自己的 `auth.json`。默认位置为 `%USERPROFILE%\.codex\auth.json`，也支持明确传入路径或 `CODEX_HOME`。
- 安装原生 Codex CLI（`codex.exe`）。脚本优先寻找 npm 安装和常见桌面安装目录，也可手动指定。使用现有登录，不需要登录第二个账号。
- 本机能够访问 OpenAI；使用代理时应有固定的 loopback HTTP 入口，例如 `http://127.0.0.1:7890`。SSH 和服务器心跳独立直连服务器。
- 管理员已经把网关的 Worker 地址设为 SSH 反向隧道地址，并停止旧来源。一个服务器映射只能由一台来源设备占用。

## 首次导入

把源码解压到本地普通目录，双击根目录 `首次配置.cmd`，在文件选择器中选择私下收到的配对 ZIP。脚本结束后点击控制台的“启动服务”。

或者在项目目录中明确传入路径：

```powershell
powershell.exe -NoProfile -ExecutionPolicy Bypass -File .\deploy\windows\Bootstrap-Device.ps1 `
  -PairingPackage 'D:\Private\device-pairing.zip' `
  -AuthFile "$env:USERPROFILE\.codex\auth.json" `
  -CodexExecutable 'C:\Tools\Codex\codex.exe' `
  -Proxy 'http://127.0.0.1:7890'
```

`-Python` 可指定现有 Python。`-Proxy direct` 表示直连 OpenAI。未指定代理时，脚本只检测环境或 Windows 代理设置中的可连接 loopback HTTP 代理，不会修改 VPN。需要重新导入时，先在控制台停止服务，再加 `-Replace`；旧配对文件会保存在受限目录内。

若报 `codex_native_binary_not_found`，请传入真正的 `codex.exe`，不要传入 `codex.cmd` 或桌面 GUI 主程序。若只有系统凭证库中的登录而没有可读取的 `auth.json`，需要先按官方 Codex 的文件凭证模式完成一次登录。本程序不会从其他设备复制 OAuth 登录。

## 运行与验证

以后双击 `启动中继.cmd`。控制台本身不自动启动服务，点击“启动服务”后，Windows 计划任务托管后台 Worker。关闭控制台不会停止 Worker；停止、重启以及注销 Windows 用户会影响正在处理的请求。

正常时本地 Bridge、SSH 隧道和心跳显示正常，服务器首页显示在线。首页在线仅表示中继和隧道健康；实际 OpenAI 可达性以模型请求为准。可让客户端发送一次短请求确认账号权限。

需要登录 Windows 后自动启动时，在首次配置完成后运行：

```powershell
powershell.exe -NoProfile -ExecutionPolicy Bypass -File .\deploy\windows\Install-Worker.ps1
```

来源设备必须保持开机并避免睡眠。现有 Codex 负责刷新自己的登录；仅运行中继不保证登录长期续期。额度采集使用隔离的 app-server 接收内存访问凭证，不持有 refresh token；若所装 Codex 版本不支持该实验接口，额度采集可能显示 unsupported，但不会因此自动改用其他账号。

## 常见提示

| 提示 | 操作 |
|---|---|
| `pairing_or_local_login_invalid` | 核对 ZIP 是否完整、登录文件是否存在、是否为 ChatGPT 登录 |
| `already_configured_use_replace` | 确认旧服务已停止，再使用 `-Replace` |
| `local_proxy_not_listening` | 启动本机代理，核对 HTTP 监听端口 |
| `ssh_host_fingerprint_mismatch` | 停止导入，请管理员核对服务器主机密钥 |
| SSH 远程端口占用 | 停止旧来源设备，确保只有一个隧道占用映射 |
| `reauth_required` 或到期提示 | 在来源设备打开官方 Codex 恢复同一个账号登录 |

配对 ZIP 不含 Codex 账号登录、管理员 SSH 私钥、VPN 订阅或网关管理员密码。导入后它仍是敏感文件，保存在私有目录即可，无需放进源码目录。
