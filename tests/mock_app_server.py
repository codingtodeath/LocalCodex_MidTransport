"""Synthetic stdio server with notifications, errors and out-of-order replies."""
import json
import sys

for line in sys.stdin:
    request = json.loads(line)
    method = request.get("method")
    ident = request.get("id")
    if ident is None:
        continue
    print(json.dumps({"method": "account/rateLimits/updated", "params": {"synthetic": True}}), flush=True)
    if method == "initialize":
        result = {"userAgent": "mock"}
    elif method == "account/read":
        result = {"account": {"type": "chatgpt", "email": "SYNTHETIC_CANARY@example.invalid"}}
    elif method == "account/rateLimits/read":
        result = {"rateLimits": {"primary": {"usedPercent": 25}}}
    elif method == "account/usage/read":
        print(json.dumps({"id": ident, "error": {"code": -32601, "message": "SYNTHETIC_CANARY"}}), flush=True)
        continue
    else:
        continue
    print(json.dumps({"id": 900000, "result": {"unknown": True}}), flush=True)
    print(json.dumps({"id": ident, "result": result}), flush=True)
