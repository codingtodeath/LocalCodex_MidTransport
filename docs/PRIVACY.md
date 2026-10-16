# 隐私与发布边界

公开仓库不包含生产服务器地址、设备配对密钥、SSH 私钥、Codex OAuth 凭证、管理员密码、VPN 配置、运行数据库、日志或真实请求正文。生产配对文件通过其他私密渠道分发，不应提交为 Issue、附件或 Git commit。

Windows 配对材料和运行记录放在 `%LOCALAPPDATA%\CodexRelay`。默认 ACL 只允许当前用户和 SYSTEM。管理员 SSH 密钥不参与中转；配对使用的专用 SSH 密钥只能建立规定的远程 loopback 映射，不允许交互式 shell。

`adapter.cache_diagnostics_capture_inputs` 和 `adapter.incident_capture_bodies` 默认关闭；配对管理员可按排障需求启用。开启后，最近请求和异常事件可能保存正文、图片及工具输入；它们属于私有诊断内容。常规日志采用允许字段列表，避免记录认证头和原始 OAuth 凭证。发布脚本不会收集这些运行目录。

`.gitignore` 是辅助保护，不能替代发布前检查。发布程序采用文件白名单，并扫描敏感文件名、私钥、长令牌和非文档用途的公网地址。合成测试中的假密钥仅用于测试，不对应生产实例。
