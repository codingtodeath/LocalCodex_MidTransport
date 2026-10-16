"""Bounded event framing; preserves original UTF-8 bytes except error objects."""
import json
import re

MAX_EVENT = 2 * 1024 * 1024


def safe_errors(value):
    # Error objects are not model text and cannot be trusted as safe strings.
    if isinstance(value, dict):
        return {k: ({"code": "upstream_error", "message": "Upstream request failed"}
                    if k == "error" and v is not None else safe_errors(v)) for k, v in value.items()}
    if isinstance(value, list):
        return [safe_errors(v) for v in value]
    return value


class SSEFramer:
    def __init__(self):
        self.buffer = bytearray()
        self.completed = False
        self.terminal = None
        self.conflict = False

    def feed(self, chunk):
        self.buffer.extend(chunk)
        frames = []
        while (match := re.search(br"\r?\n\r?\n", self.buffer)):
            end = match.end()
            if end > MAX_EVENT:
                raise ValueError("event_too_large")
            frame = bytes(self.buffer[:end])
            del self.buffer[:end]
            frames.append(self.process(frame))
        if len(self.buffer) > MAX_EVENT:
            raise ValueError("event_too_large")
        return frames

    def process(self, frame):
        lines = frame.decode("utf-8", errors="strict").splitlines()
        data = "\n".join(line[5:].lstrip(" ") for line in lines if line.startswith("data:"))
        event = next((line[6:].strip() for line in lines if line.startswith("event:")), "")
        try:
            obj = json.loads(data)
        except (ValueError, TypeError):
            if event in ("error", "response.failed", "response.incomplete"):
                return b'event: error\ndata: {"type":"error","code":"upstream_protocol_error","message":"Invalid upstream error event"}\n\n'
            return frame
        if not isinstance(obj, dict):
            return frame
        kind = obj.get("type", event)
        if kind in ("response.completed", "response.failed", "response.incomplete"):
            response = obj.get("response", {})
            terminal = (kind, response.get("id", obj.get("id")), response.get("usage", obj.get("usage")))
            if self.terminal is not None and self.terminal != terminal:
                self.conflict = True
            self.terminal = terminal
            self.completed = kind == "response.completed" and response.get("status") == "completed" and not self.conflict
        if kind == "error":
            obj = {"type": "error", "code": "upstream_error", "message": "Upstream request failed"}
        else:
            cleaned = safe_errors(obj)
            if cleaned == obj:
                return frame
            obj = cleaned
        prefix = f"event: {event}\n" if event and re.fullmatch(r"[a-zA-Z0-9_.-]{1,96}", event) else ""
        return (prefix + "data: " + json.dumps(obj, ensure_ascii=False) + "\n\n").encode("utf-8")
