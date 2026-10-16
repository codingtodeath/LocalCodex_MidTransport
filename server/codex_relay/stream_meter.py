"""独立 SSE 旁路解析与计量。

硬性要求（见 01_DEVELOPMENT_SPEC.md 6.4 与第 10 节）：
* 数据流按 bytes 传递；解析器只看副本，**绝不修改**已合法传输的字节。
* 增量 UTF-8 解码：分块可以切断 JSON 或汉字。
* 支持多个事件合在一块、CRLF、多行 data、注释行与空行。
* 只读取最后一个可确认的终态 usage；不为每个 delta 累加；
  重复终态事件不二次计数；冲突时标记 conflict，不静默相加。
* Responses 使用命名事件，不能只寻找 Chat Completions 风格的 `[DONE]`。
"""

from __future__ import annotations

import codecs
import json
import hashlib
from dataclasses import dataclass, field
from typing import Any

from .models import NormalizedUsage, extract_usage

# 终态事件类型（见 05_SOURCES_AND_COMPATIBILITY.md [S10] 的实测要求）。
TERMINAL_COMPLETED = "response.completed"
TERMINAL_FAILED = "response.failed"
TERMINAL_INCOMPLETE = "response.incomplete"
TERMINAL_EVENTS = (TERMINAL_COMPLETED, TERMINAL_FAILED, TERMINAL_INCOMPLETE)


@dataclass
class StreamMeterResult:
    """一次请求的旁路解析结论。"""

    response_id: str | None = None
    response_model: str | None = None
    terminal_state: str | None = None
    terminal_seen: bool = False
    usage: NormalizedUsage = field(default_factory=NormalizedUsage)
    usage_source: str | None = None
    duplicate_terminal: bool = False
    conflict: bool = False
    parse_errors: int = 0
    malformed_events: int = 0
    event_count: int = 0
    bytes_seen: int = 0
    # 上游协议层面的失败描述：只保留稳定错误码，不保留原文（可能含凭证）。
    upstream_error_code: str | None = None
    upstream_error_type: str | None = None

    def account_success(self) -> bool:
        """HTTP 200 不必然成功；成功率依据合法 completed / failed / incomplete。"""

        return self.terminal_state == "completed"


class SSEMeter:
    """增量 SSE 解析器。每次请求独立实例，不共享输出缓冲。"""

    def __init__(self) -> None:
        self._decoder = codecs.getincrementaldecoder("utf-8")(errors="replace")
        self._buffer = ""
        self._data_lines: list[str] = []
        self._event_name: str | None = None
        self.result = StreamMeterResult()
        self._terminal_digest: str | None = None
        self._event_size = 0

    # --- 输入 ---------------------------------------------------------

    def feed(self, chunk: bytes) -> None:
        """喂入原始字节。chunk 为 None/空时忽略。"""

        if not chunk:
            return
        self.result.bytes_seen += len(chunk)
        # 增量解码：分块切断的汉字会被 decoder 缓冲，不会产生乱码。
        text = self._decoder.decode(chunk)
        if not text:
            return
        self._buffer += text
        self._drain_lines()
        if len(self._buffer) > 2 * 1024 * 1024:
            raise ValueError("sse_event_too_large")

    def finish(self) -> StreamMeterResult:
        """流结束（或断开）时调用，处理末尾未换行的残余。"""

        tail = self._decoder.decode(b"", final=True)
        if tail:
            self._buffer += tail
        # SSE dispatch requires a complete blank-line delimiter. A truncated
        # terminal JSON is not evidence of completion even if it parses.
        if self._buffer or self._data_lines:
            self.result.malformed_events += 1
        self._buffer = ""
        self._data_lines = []
        self._finalize()
        return self.result

    # --- 行处理 -------------------------------------------------------

    def _drain_lines(self) -> None:
        while True:
            newline = self._find_newline()
            if newline is None:
                return
            index, length = newline
            line = self._buffer[:index]
            self._buffer = self._buffer[index + length :]
            self._handle_line(line)

    def _find_newline(self) -> tuple[int, int] | None:
        """同时支持 LF 与 CRLF。返回 (索引, 换行长度)。"""

        lf = self._buffer.find("\n")
        cr = self._buffer.find("\r")
        if lf == -1 and cr == -1:
            return None
        if cr != -1 and (lf == -1 or cr < lf):
            # 可能是 CRLF：若 \r 是最后一个字符，等待下一个 chunk 判定。
            if cr == len(self._buffer) - 1:
                return None
            if self._buffer[cr + 1] == "\n":
                return cr, 2
            return cr, 1
        return lf, 1

    def _handle_line(self, line: str) -> None:
        self._event_size += len(line) + 1
        if self._event_size > 2 * 1024 * 1024:
            raise ValueError("sse_event_too_large")
        if line == "":
            self._event_size = 0
            self._flush_event()
            return
        if line.startswith(":"):
            # 注释行（含保活）。注释不参与计量，也不重复计数。
            return
        if ":" in line:
            field_name, _, value = line.partition(":")
            if value.startswith(" "):
                value = value[1:]
        else:
            field_name, value = line, ""
        if field_name == "event":
            self._event_name = value
        elif field_name == "data":
            self._data_lines.append(value)
        # id / retry 等字段忽略。

    def _flush_event(self) -> None:
        if not self._data_lines and self._event_name is None:
            return
        data = "\n".join(self._data_lines)
        event_name = self._event_name
        self._data_lines = []
        self._event_name = None
        if data == "":
            return
        self.result.event_count += 1
        self._handle_event(event_name, data)

    def _handle_event(self, event_name: str | None, data: str) -> None:
        try:
            payload = json.loads(data)
        except json.JSONDecodeError:
            self.result.parse_errors += 1
            return
        if not isinstance(payload, dict):
            self.result.malformed_events += 1
            return

        event_type = payload.get("type") or event_name or ""
        if not isinstance(event_type, str):
            self.result.malformed_events += 1
            return

        # 记录响应标识（created / in_progress / completed 都带 response.id）。
        if self.result.response_id is None:
            rid = _response_id(payload)
            if rid:
                self.result.response_id = rid
        if self.result.response_model is None:
            model = payload.get("model")
            if isinstance(model, str) and model:
                self.result.response_model = model
            else:
                response = payload.get("response")
                if isinstance(response, dict) and isinstance(response.get("model"), str):
                    self.result.response_model = response["model"]

        if event_type in TERMINAL_EVENTS:
            self._handle_terminal(event_type, payload)
        elif event_type == "error":
            self._handle_error(payload)

    def _handle_terminal(self, event_type: str, payload: dict[str, Any]) -> None:
        digest = hashlib.sha256(json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
        # 重复终态：只取最后一次可确认的 usage，且不二次计数。
        if self.result.terminal_seen:
            self.result.duplicate_terminal = True
            usage_payload = _terminal_usage(payload)
            if self._terminal_digest != digest:
                self.result.conflict = True
            return

        self.result.terminal_seen = True
        self._terminal_digest = digest
        self.result.terminal_state = _state_from_event(event_type, payload)
        usage_payload = _terminal_usage(payload)
        if usage_payload is None:
            # 终态但没有 usage：明确 unknown，不伪造 0。
            self.result.usage = NormalizedUsage(status="missing", problem="terminal_without_usage")
            self.result.usage_source = "terminal_event"
        else:
            self.result.usage = extract_usage(usage_payload)
            self.result.usage_source = "terminal_event"

    def _handle_error(self, payload: dict[str, Any]) -> None:
        error = payload.get("error")
        if not isinstance(error, dict):
            return
        code = error.get("code")
        etype = error.get("type")
        # 只保留稳定标识，绝不保留可能含凭证的原始 message。
        self.result.upstream_error_code = _sanitize_code(code)
        self.result.upstream_error_type = _sanitize_code(etype)

    def _finalize(self) -> None:
        # 流在终态前结束：usage 明确 unknown，不补造 completed。
        if not self.result.terminal_seen:
            self.result.usage = NormalizedUsage(status="missing", problem="no_terminal_event")


def _sanitize_code(value: Any) -> str | None:
    if not isinstance(value, str) or not value:
        return None
    cleaned = "".join(ch for ch in value if ch.isalnum() or ch in "_-.")[:64]
    return cleaned or None


def _response_id(payload: dict[str, Any]) -> str | None:
    rid = payload.get("id")
    if isinstance(rid, str) and rid:
        return rid[:128]
    response = payload.get("response")
    if isinstance(response, dict):
        rid = response.get("id")
        if isinstance(rid, str) and rid:
            return rid[:128]
    return None


def _terminal_usage(payload: dict[str, Any]) -> dict[str, Any] | None:
    """终态事件的 usage 可能在顶层，也可能在 response 子对象里。"""

    usage = payload.get("usage")
    if isinstance(usage, dict):
        return usage
    response = payload.get("response")
    if isinstance(response, dict):
        usage = response.get("usage")
        if isinstance(usage, dict):
            return usage
    return None


def _state_from_event(event_type: str, payload: dict[str, Any]) -> str:
    """终态事件类型与 response.status 可能不一致；以 response.status 优先。"""

    response = payload.get("response")
    if isinstance(response, dict):
        status = response.get("status")
        if isinstance(status, str) and status in ("completed", "failed", "incomplete"):
            return status
    mapping = {
        TERMINAL_COMPLETED: "completed",
        TERMINAL_FAILED: "failed",
        TERMINAL_INCOMPLETE: "incomplete",
    }
    return mapping.get(event_type, "unknown")


def meter_bytes(chunks: list[bytes]) -> StreamMeterResult:
    """测试辅助：按给定分块喂入并返回结论。"""

    meter = SSEMeter()
    for chunk in chunks:
        meter.feed(chunk)
    return meter.finish()


def meter_sse_text(text: str, chunk_size: int | None = None) -> StreamMeterResult:
    """测试辅助：按字节分块（可能切断 UTF-8）解析文本。"""

    raw = text.encode("utf-8")
    if chunk_size is None:
        return meter_bytes([raw])
    return meter_bytes([raw[i : i + chunk_size] for i in range(0, len(raw), chunk_size)])
