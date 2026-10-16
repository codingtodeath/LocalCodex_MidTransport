"""Codex 私有订阅中继 —— Ubuntu Gateway。

本包实现文档包 `codex_private_relay_spec_v1` 的服务端部分：
私有请求入口、计量、额度快照、估价、管理网站与 SQLite 存储。

安全底线（见 01_DEVELOPMENT_SPEC.md 第 1、5 节）：
本服务不持有、不读取、不转发 Codex 订阅 OAuth 凭证。它只持有本项目
自己的网关 Key、遥测 Key 与 Bridge Key。
"""

__version__ = "1.0.0"
PROTOCOL_VERSION = "1.0"
