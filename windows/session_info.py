"""Display only local session expiry metadata; never refresh or expose tokens."""
import base64
import json
import math
from datetime import datetime, timezone

from .config import load_config
from .existing_auth import ExistingAuth
from .local_control import default_config


def expiry_info(config, now=None):
    if config.adapter.mode != "existing_codex_login":
        return {"status": "unsupported"}
    try:
        access, _, _ = ExistingAuth(config.adapter.existing_auth_file).read()
        segment = access.split(".")[1]
        claims = json.loads(base64.urlsafe_b64decode(segment + "=" * (-len(segment) % 4)))
        exp = claims.get("exp")
        if type(exp) not in (int, float) or not math.isfinite(exp):
            return {"status": "unknown"}
        expires = datetime.fromtimestamp(exp, timezone.utc)
        remaining = (expires - (now or datetime.now(timezone.utc))).total_seconds()
        return {"status": "expired" if remaining <= 0 else "available",
                "expires_at_local": expires.astimezone().strftime("%Y-%m-%d %H:%M:%S %z"),
                "remaining_minutes": max(0, math.ceil(remaining / 60))}
    except (OSError, ValueError, TypeError, AttributeError, IndexError, OverflowError):
        return {"status": "unavailable"}


def main():
    print("Codex 登录凭证到期信息：")
    try:
        result = expiry_info(load_config(default_config()))
    except Exception:
        result = {"status": "unavailable"}
    status = result["status"]
    if status in ("available", "expired"):
        print("到期时间（本机时区）：" + result["expires_at_local"])
        if status == "expired":
            print("凭证已到期，请通过现有 Codex 恢复有效凭证。")
        else:
            days, minutes = divmod(result["remaining_minutes"], 1440)
            hours, minutes = divmod(minutes, 60)
            print(f"剩余时间：{days} 天 {hours} 小时 {minutes} 分钟")
        print("以上为本地凭证记录，本操作不会刷新登录。")
    elif status == "unknown":
        print("无法确定到期时间：凭证中没有有效的到期记录。")
    elif status == "unsupported":
        print("当前配置未使用现有 Codex 登录，无法显示其凭证到期时间。")
    else:
        print("无法读取到期时间，请检查当前用户的中继配置与现有 Codex 登录。")


if __name__ == "__main__":
    main()
