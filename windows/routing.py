"""Narrow, request-local Codex routing metadata; never retain it between turns."""

REQUEST_HEADERS = frozenset({"session-id", "thread-id", "x-codex-turn-state"})
RESPONSE_HEADERS = frozenset({"x-codex-turn-state"})


def routing_headers(headers, *, response=False):
    if headers is None:
        return {}
    allowed = RESPONSE_HEADERS if response else REQUEST_HEADERS
    result = {}
    # Reject duplicates and control characters; no auth or arbitrary client headers.
    items = headers.multi_items() if hasattr(headers, "multi_items") else headers.items()
    invalid = set()
    for name, value in items:
        name = name.lower()
        if name not in allowed:
            continue
        if name in result or not isinstance(value, str) or not 0 < len(value) <= 4096 or any(
                ord(c) < 32 or ord(c) > 126 for c in value):
            invalid.add(name)
        else:
            result[name] = value
    return {k: v for k, v in result.items() if k not in invalid}
