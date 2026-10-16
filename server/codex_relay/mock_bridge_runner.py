#!/usr/bin/env python3
"""独立运行 Mock Windows Bridge（演示用）。

    .venv/bin/python -m codex_relay.mock_bridge_runner \
        --port 18765 --worker-key-file var/secrets/wkr.key

这是测试替身，不是 Windows 实现。它不持有任何真实凭证，也不具备
真实订阅推理能力。
"""

from __future__ import annotations

import argparse
from pathlib import Path

import uvicorn

from .mock_bridge import create_mock_bridge


def main() -> int:
    parser = argparse.ArgumentParser(description="运行 Mock Windows Bridge")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=18765)
    parser.add_argument("--worker-key-file", required=True)
    parser.add_argument("--worker-id", default="home-windows")
    parser.add_argument("--log-level", default="warning")
    args = parser.parse_args()

    key_path = Path(args.worker_key_file)
    if not key_path.is_file():
        raise SystemExit(f"worker key 文件不存在：{key_path}")
    worker_key = key_path.read_text(encoding="utf-8").strip()

    app = create_mock_bridge(worker_key, args.worker_id)
    # 只绑定 loopback：Bridge 绝不应监听公网或局域网。
    uvicorn.run(app, host=args.host, port=args.port, log_level=args.log_level, access_log=False)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
