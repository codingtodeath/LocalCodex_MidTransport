# LocalCodex MidTransport

将已登录 Codex 的 Windows 设备作为推理来源，通过服务器提供统一的 Responses 接口、API Key 管理和用量页面。服务器不需要登录来源设备的 Codex 账号。

```text
客户端 → HTTPS 网关 → SSH 反向隧道 → Windows Bridge → Codex 上游
                  ↑ 心跳、额度快照
```

支持 `/v1/responses` 和 `/v1/models`，包含流式转发、图片输入、工具调用透传、有限安全重试、连接诊断、最近请求记录与异常事件抓取。工具在客户端执行。Windows 控制台提供启动、停止、重启和状态查看；关闭控制台不会停止后台服务。

## 接入已配对的服务器

1. 在来源设备安装 **64 位 Python 3.11 或更高版本**（包含 tkinter）和 Windows OpenSSH 客户端；准备已登录 ChatGPT 的 Codex，以及原生 Codex CLI 可执行文件。
2. 下载本仓库源码，解压到本地普通目录。不要放到 OneDrive 等同步目录。
3. 通过私密渠道接收管理员提供的设备配对 ZIP。**配对包不在此仓库中，也不要上传到 GitHub。**
4. 双击 `首次配置.cmd`，选择配对 ZIP。脚本创建 `.venv`、安装 Python 依赖、导入配对材料，并读取这台设备自己的 Codex 登录。
5. 控制台打开后点击“启动服务”。以后双击 `启动中继.cmd` 管理服务。

首次配置会下载 Python 依赖；没有附带虚拟环境、Codex 二进制或其他设备的登录文件。Codex CLI 只有安装路径需要明确，不需要再登录一个独立账号。若自动检测不到 CLI 或需要明确指定代理，请看 [Windows 配置](docs/SETUP_WINDOWS.md)。

来源设备需要持续开机、联网，且本机代理必须能访问 OpenAI。客户端继续使用网关分发的 Key 和 base_url。不同来源账号会生成不同的额度身份周期，历史统计保留。

## 登录与日志

程序只读取来源设备的 `auth.json`，不会把 OAuth 凭证上传给服务器。登录刷新由该设备的官方 Codex 负责；本程序不保证在官方 Codex 长期不运行时自动续期。控制台会显示到期提示，鉴权失效时需要在来源设备恢复 Codex 登录。

运行文件位于 `%LOCALAPPDATA%\CodexRelay`，使用当前用户和 SYSTEM 的受限权限。常规运行日志记录错误分类、时间、状态码和请求标识；可按私有配置开启最近 20 条请求正文与工具输入、异常响应体记录。这些诊断内容只应保存在私有设备目录中，详见 [隐私与发布边界](docs/PRIVACY.md)。

## 开发

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.in
.\.venv\Scripts\python.exe -m pytest -q
```

测试使用本地合成数据，不需要真实账号或公网模型调用。`tools/mock_smoke.py` 用于本地 Mock 冒烟测试。`tools/client_connection_probe.py` 可检查客户端到网关的连接，无需 API Key。

- `windows/`：Bridge、适配器、诊断与图形控制台。
- `server/`：Gateway、Key 余额、用量统计及管理页面源码。
- `linux_worker/`：可选的 Linux Worker、凭证维护和流量监控实现。
- `contracts/`、`fixtures/`、`tests/`：协议与合成测试。
- `deploy/windows/`：首次配置与计划任务脚本。

公开仓库只包含程序、测试和通用模板，不包含可直接连接任何生产服务器的配置。新建服务器还需要管理员配置 HTTPS、Gateway、数据库、专用受限 SSH 账号和配对密钥；客户端脚本不会替你创建服务器。
