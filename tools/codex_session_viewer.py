"""Local Codex session usage viewer. Python 3.11+, standard library only.

Launch with pythonw.exe; no network requests or writes to Codex session databases.
"""
from __future__ import annotations

import argparse
import bisect
import csv
from contextlib import closing
import datetime as dt
import json
import os
from pathlib import Path
import queue
import re
import sqlite3
import statistics
import threading
import traceback
from dataclasses import dataclass, field
import tkinter as tk
from tkinter import filedialog, messagebox, ttk


VERSION = "1.0"
COLORS = {"bg": "#f3f5f8", "card": "#ffffff", "ink": "#182536", "muted": "#64748b", "accent": "#2463c9"}
TOKEN_KEYS = ("input_tokens", "cached_input_tokens", "cache_write_input_tokens", "output_tokens", "reasoning_output_tokens", "total_tokens")


def epoch(value):
    try:
        return dt.datetime.fromisoformat(str(value).replace("Z", "+00:00")).timestamp()
    except (TypeError, ValueError):
        return None


def number(value):
    try:
        return max(0, int(value or 0))
    except (TypeError, ValueError, OverflowError):
        return 0


def normalize_usage(value):
    value = value if isinstance(value, dict) else {}
    result = {key: number(value.get(key)) for key in TOKEN_KEYS}
    result["cached_input_tokens"] = number(value.get("cached_input_tokens", (value.get("input_tokens_details") or {}).get("cached_tokens")))
    result["reasoning_output_tokens"] = number(value.get("reasoning_output_tokens", (value.get("output_tokens_details") or {}).get("reasoning_tokens")))
    # Cached input and reasoning output are subsets, never add them again.
    if value.get("total_tokens") is None:
        result["total_tokens"] = result["input_tokens"] + result["output_tokens"]
    return result


def readable_time(value):
    return dt.datetime.fromtimestamp(value).strftime("%m-%d %H:%M:%S") if value is not None else "—"


def percentage(cached, total):
    return cached / total * 100 if total else None


def fmt_rate(value):
    return f"{value:.2f}" if value is not None else "不可计算"


def clean_path(value):
    return str(value or "").removeprefix("\\\\?\\")


@dataclass
class Session:
    id: str
    title: str = "未命名会话"
    cwd: str = ""
    model: str = "未知"
    updated: float = 0.0
    files: list[Path] = field(default_factory=list)
    archived: bool = False


@dataclass
class Request:
    response_id: str
    thread_id: str
    turn_id: str
    timestamp: float
    model: str
    effort: str
    usage: dict
    source: str
    first_output: float | None = None
    stream_seconds: float | None = None
    http_seconds: float | None = None
    compaction: bool = False
    usage_source: str = "token_usage_record"

    @property
    def tps(self):
        return self.usage["output_tokens"] / self.stream_seconds if self.stream_seconds and self.stream_seconds > 0 else None

    @property
    def http_tps(self):
        return self.usage["output_tokens"] / self.http_seconds if self.http_seconds and self.http_seconds > 0 else None

    @property
    def cache_rate(self):
        return percentage(self.usage["cached_input_tokens"], self.usage["input_tokens"])

    def export(self):
        return {"time": dt.datetime.fromtimestamp(self.timestamp).astimezone().isoformat(), "model": self.model,
                "effort": self.effort, "kind": "compaction" if self.compaction else "normal", **self.usage,
                "uncached_input_tokens": max(0, self.usage["input_tokens"] - self.usage["cached_input_tokens"]),
                "cache_hit_percent": self.cache_rate, "stream_seconds": self.stream_seconds, "stream_tps": self.tps,
                "http_seconds": self.http_seconds, "http_tps": self.http_tps, "response_id": self.response_id,
                "thread_id": self.thread_id, "turn_id": self.turn_id, "usage_source": self.usage_source, "file": self.source}


def read_index(home):
    """Read only session-identifying fields; never load credentials or prompts."""
    metadata = {}
    db = home / "state_5.sqlite"
    if db.exists():
        try:
            with closing(sqlite3.connect(db.resolve().as_uri() + "?mode=ro", uri=True, timeout=1)) as conn:
                for sid, title, cwd, model, updated, archived in conn.execute(
                    "SELECT id,title,cwd,model,updated_at,archived FROM threads"
                ):
                    metadata[sid] = dict(title=title or "未命名会话", cwd=clean_path(cwd), model=model or "未知", updated=updated or 0, archived=bool(archived))
        except sqlite3.Error:
            pass
    index = home / "session_index.jsonl"
    if index.exists():
        try:
            with index.open(encoding="utf-8") as handle:
                for line in handle:
                    try:
                        row = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    if row.get("id"):
                        target = metadata.setdefault(row["id"], {})
                        if row.get("thread_name"):
                            target.setdefault("title", row["thread_name"])
        except OSError:
            pass
    return metadata


def scan_sessions(home):
    metadata = read_index(home)
    sessions = {}
    warnings = []
    for directory in (home / "sessions", home / "archived_sessions"):
        if not directory.exists():
            continue
        for path in directory.rglob("*.jsonl"):
            try:
                header = None
                with path.open(encoding="utf-8-sig") as handle:
                    for _ in range(12):
                        line = handle.readline()
                        if not line:
                            break
                        try:
                            row = json.loads(line)
                        except json.JSONDecodeError:
                            continue
                        if row.get("type") == "session_meta":
                            header = row.get("payload", {})
                            break
                if not header or not header.get("id"):
                    warnings.append(f"无法识别 session：{path.name}")
                    continue
                sid = header["id"]
                session = sessions.get(sid)
                if session is None:
                    meta = metadata.get(sid, {})
                    session = Session(sid, meta.get("title", "未命名会话"), meta.get("cwd", clean_path(header.get("cwd"))),
                                      meta.get("model", "未知"), meta.get("updated", 0), archived=meta.get("archived", directory.name == "archived_sessions"))
                    sessions[sid] = session
                session.files.append(path)
                session.updated = max(session.updated, path.stat().st_mtime)
            except OSError as exc:
                warnings.append(f"{path.name}：{exc}")
    for session in sessions.values():
        session.files.sort(key=lambda p: p.name)
    return sorted(sessions.values(), key=lambda s: s.updated, reverse=True), warnings


def file_fingerprint(files):
    result = []
    for path in files:
        try:
            info = path.stat()
            result.append((str(path), info.st_mtime_ns, info.st_size))
        except OSError:
            result.append((str(path), None, None))
    return tuple(result)


def load_requests(session, home):
    """Stream JSONL files. Prefer native response usage over duplicate UI counters."""
    result = []
    warnings = []
    seen = set()
    legacy_totals = set()
    for path in sorted(session.files, key=lambda p: p.name):
        native = []
        legacy = []
        model, effort, turn_id = session.model, "", ""
        first = None
        last_native = None
        last_legacy = None
        invalid = 0
        try:
            with path.open(encoding="utf-8-sig") as handle:
                for lineno, line in enumerate(handle, 1):
                    try:
                        row = json.loads(line)
                        q = row.get("payload") or {}
                        if not isinstance(q, dict):
                            invalid += 1
                            continue
                        typ, kind = row.get("type"), q.get("type")
                        timestamp = epoch(row.get("timestamp"))
                        if typ == "turn_context":
                            model = q.get("model") or model
                            effort = q.get("effort") or q.get("reasoning_effort") or ""
                            turn_id = q.get("turn_id") or turn_id
                        elif typ == "event_msg" and kind == "task_started":
                            first = None
                            turn_id = q.get("turn_id") or ""
                        elif typ == "event_msg" and kind == "item_completed":
                            item = q.get("item") or {}
                            if item.get("type") in ("Reasoning", "AgentMessage"):
                                start = q.get("started_at_ms")
                                if isinstance(start, (int, float)):
                                    start /= 1000
                                    first = min(first, start) if first is not None else start
                        elif typ == "token_usage_record" and timestamp is not None:
                            usage = normalize_usage(q.get("usage"))
                            rid = q.get("response_id") or f"{path.name}:{lineno}"
                            seconds = timestamp - first if first is not None and timestamp > first else None
                            request = Request(rid, q.get("thread_id") or session.id, q.get("turn_id") or turn_id,
                                              timestamp, model, effort, usage, str(path), first, seconds)
                            native.append(request)
                            last_native = request
                            first = None
                        elif typ == "event_msg" and kind == "token_count" and timestamp is not None:
                            info = q.get("info") or {}
                            raw = info.get("last_token_usage")
                            if raw and not native:
                                totals = info.get("total_token_usage")
                                counter = tuple(normalize_usage(totals)[key] for key in TOKEN_KEYS) if totals else None
                                if counter is None or counter not in legacy_totals:
                                    if counter is not None:
                                        legacy_totals.add(counter)
                                    seconds = timestamp - first if first is not None and timestamp > first else None
                                    request = Request(f"legacy:{path.name}:{lineno}", session.id, turn_id, timestamp, model,
                                                      effort, normalize_usage(raw), str(path), first, seconds, usage_source="token_count")
                                    legacy.append(request)
                                    last_legacy = request
                                    first = None
                        elif typ == "compacted":
                            if last_native:
                                last_native.compaction = True
                            elif last_legacy:
                                last_legacy.compaction = True
                            first = None
                        # A compaction marker only applies to the immediately preceding usage.
                        if typ not in ("token_usage_record", "compacted"):
                            last_native = None
                        if not (typ == "event_msg" and kind == "token_count") and typ != "compacted":
                            last_legacy = None
                    except (json.JSONDecodeError, TypeError, ValueError, AttributeError):
                        invalid += 1
            for request in native if native else legacy:
                if request.response_id not in seen:
                    seen.add(request.response_id)
                    result.append(request)
            if invalid:
                warnings.append(f"{path.name}：跳过 {invalid} 行未写完或无效记录")
        except OSError as exc:
            warnings.append(f"{path.name}：{exc}")
    result.sort(key=lambda r: (r.timestamp, r.response_id))
    attach_http_timing(result, home, session.id, warnings)
    return result, warnings


def attach_http_timing(requests, home, thread_id, warnings):
    db = home / "logs_2.sqlite"
    if not db.exists() or not requests:
        return
    try:
        starts = []
        with closing(sqlite3.connect(db.resolve().as_uri() + "?mode=ro", uri=True, timeout=1)) as conn:
            query = "SELECT ts,ts_nanos,feedback_log_body FROM logs WHERE thread_id=? AND target='codex_http_client::request' ORDER BY ts,ts_nanos,id"
            for seconds, nanos, body in conn.execute(query, (thread_id,)):
                if body and "Compressed request body" in body:
                    match = re.search(r"turn.id=([^\s}]+)", body)
                    if match:
                        starts.append((seconds + nanos / 1e9, match[1]))
        times = [entry[0] for entry in starts]
        previous = 0
        for request in requests:
            left = bisect.bisect_right(times, previous)
            right = bisect.bisect_left(times, request.timestamp)
            candidates = [time for time, turn in starts[left:right] if turn == request.turn_id]
            if len(candidates) == 1:
                request.http_seconds = request.timestamp - candidates[0]
            previous = request.timestamp
    except (OSError, sqlite3.Error, TypeError) as exc:
        warnings.append(f"HTTP 时间日志暂不可用：{exc}")


def summarize(requests):
    totals = {key: sum(r.usage[key] for r in requests) for key in TOKEN_KEYS}
    streams = [r for r in requests if not r.compaction and r.stream_seconds and r.stream_seconds > 0]
    http = [r for r in requests if not r.compaction and r.http_seconds and r.http_seconds > 0]
    seconds = sum(r.stream_seconds for r in streams)
    http_seconds = sum(r.http_seconds for r in http)
    return {**totals, "count": len(requests), "cache_rate": percentage(totals["cached_input_tokens"], totals["input_tokens"]),
            "uncached_input_tokens": max(0, totals["input_tokens"] - totals["cached_input_tokens"]),
            "tps": sum(r.usage["output_tokens"] for r in streams) / seconds if seconds else None,
            "median_tps": statistics.median(r.tps for r in streams) if streams else None,
            "tps_samples": len(streams), "tps_seconds": seconds,
            "http_tps": sum(r.usage["output_tokens"] for r in http) / http_seconds if http_seconds else None,
            "http_samples": len(http), "compactions": sum(r.compaction for r in requests)}


class Viewer(tk.Tk):
    def __init__(self, home, project=""):
        super().__init__()
        self.home = Path(home)
        self.title(f"Codex Session 用量查看器  ·  {VERSION}")
        width = min(1420, max(900, self.winfo_screenwidth() - 80))
        height = min(900, max(600, self.winfo_screenheight() - 120))
        self.geometry(f"{width}x{height}+30+30")
        self.minsize(min(1160, width), min(720, height))
        self.configure(bg=COLORS["bg"])
        self.sessions = []
        self.session = None
        self.requests = []
        self.filtered_requests = []
        self.displayed_requests = []
        self.warnings = []
        self.events = queue.Queue()
        self.generation = 0
        self.loading = False
        self.scanning = False
        self.fingerprint = ()
        self.last_summary = {}
        self.search = tk.StringVar(value=project)
        self.model_filter = tk.StringVar(value="全部模型")
        self.limit = tk.StringVar(value="100")
        self.include_compactions = tk.BooleanVar(value=True)
        self.auto = tk.BooleanVar(value=True)
        self.status = tk.StringVar(value="正在查找本地 session…")
        self.build_ui()
        self.after(100, self.poll_events)
        self.after(5000, self.auto_refresh)
        self.after(30000, self.auto_scan)
        self.scan()

    def build_ui(self):
        style = ttk.Style(self)
        style.theme_use("clam")
        style.configure("TFrame", background=COLORS["bg"])
        style.configure("TLabel", background=COLORS["bg"], foreground=COLORS["ink"], font=("Microsoft YaHei UI", 10))
        style.configure("TButton", font=("Microsoft YaHei UI", 10), padding=(10, 6))
        style.configure("TCheckbutton", background=COLORS["bg"], font=("Microsoft YaHei UI", 9))
        style.configure("Treeview", font=("Microsoft YaHei UI", 9), rowheight=30, background="white", fieldbackground="white", foreground=COLORS["ink"])
        style.configure("Treeview.Heading", font=("Microsoft YaHei UI", 9, "bold"), padding=5)
        style.map("Treeview", background=[("selected", COLORS["accent"])], foreground=[("selected", "white")])
        header = ttk.Frame(self, padding=(20, 16, 20, 12))
        header.pack(fill="x")
        ttk.Label(header, text="Codex Session", font=("Segoe UI", 22, "bold")).pack(side="left")
        ttk.Label(header, text="  本地会话 · Token / Cache / TPS", foreground=COLORS["muted"]).pack(side="left", padx=12)
        ttk.Button(header, text="刷新会话", command=self.scan).pack(side="right")
        ttk.Button(header, text="选择日志目录", command=self.choose_home).pack(side="right", padx=8)
        self.path_label = ttk.Label(self, text=str(self.home), foreground=COLORS["muted"])
        self.path_label.pack(anchor="w", padx=22, pady=(0, 10))
        # Reserve footer space before the expanding pane, including on smaller screens.
        footer = ttk.Frame(self, padding=(20, 8, 20, 12))
        footer.pack(side="bottom", fill="x")
        ttk.Label(footer, textvariable=self.status, foreground=COLORS["muted"], wraplength=1050).pack(side="left")
        ttk.Button(footer, text="查看计算口径", command=self.explain).pack(side="right")
        panes = ttk.Panedwindow(self, orient="horizontal")
        panes.pack(fill="both", expand=True, padx=18)
        sidebar = ttk.Frame(panes, padding=(0, 0, 12, 0))
        content = ttk.Frame(panes)
        panes.add(sidebar, weight=1)
        panes.add(content, weight=4)
        self.after(200, lambda: panes.sashpos(0, 320))
        ttk.Label(sidebar, text="选择 session", font=("Microsoft YaHei UI", 11, "bold")).pack(anchor="w", pady=(0, 8))
        search_row = ttk.Frame(sidebar)
        search_row.pack(fill="x", pady=(0, 8))
        entry = ttk.Entry(search_row, textvariable=self.search, font=("Microsoft YaHei UI", 10))
        entry.pack(side="left", fill="x", expand=True)
        ttk.Button(search_row, text="清空", command=lambda: self.search.set("")).pack(side="right", padx=(5, 0))
        ttk.Label(sidebar, text="搜索标题 / 项目路径 / session ID", foreground=COLORS["muted"], font=("Microsoft YaHei UI", 9)).pack(anchor="w", pady=(0, 8))
        session_box = ttk.Frame(sidebar)
        session_box.pack(fill="both", expand=True)
        self.session_tree = ttk.Treeview(session_box, columns=("title", "project"), show="headings", selectmode="browse")
        self.session_tree.heading("title", text="会话标题")
        self.session_tree.heading("project", text="项目")
        self.session_tree.column("title", width=210, minwidth=140)
        self.session_tree.column("project", width=110, minwidth=70)
        self.session_tree.pack(side="left", fill="both", expand=True)
        scroll = ttk.Scrollbar(session_box, orient="vertical", command=self.session_tree.yview)
        scroll.pack(side="right", fill="y")
        self.session_tree.configure(yscrollcommand=scroll.set)
        self.session_tree.bind("<<TreeviewSelect>>", self.select_session)
        self.session_count = ttk.Label(sidebar, text="", foreground=COLORS["muted"])
        self.session_count.pack(anchor="w", pady=8)
        self.search.trace_add("write", lambda *_: self.show_sessions())

        self.session_title = ttk.Label(content, text="选择左侧会话开始查看", font=("Microsoft YaHei UI", 14, "bold"), wraplength=980)
        self.session_title.pack(anchor="w", pady=(0, 6))
        self.session_info = ttk.Label(content, text="", foreground=COLORS["muted"], wraplength=980)
        self.session_info.pack(anchor="w", pady=(0, 10))
        filters = ttk.Frame(content)
        filters.pack(fill="x", pady=(0, 12))
        ttk.Label(filters, text="模型").pack(side="left", padx=(0, 6))
        self.model_combo = ttk.Combobox(filters, textvariable=self.model_filter, values=["全部模型"], state="readonly", width=20)
        self.model_combo.pack(side="left")
        self.model_combo.bind("<<ComboboxSelected>>", lambda _: self.render())
        ttk.Checkbutton(filters, text="用量包含上下文压缩", variable=self.include_compactions, command=self.render).pack(side="left", padx=10)
        ttk.Checkbutton(filters, text="自动刷新（5 秒）", variable=self.auto).pack(side="left")
        ttk.Button(filters, text="刷新数据", command=lambda: self.load_selected(force=True)).pack(side="right")

        cards = tk.Frame(content, bg=COLORS["bg"])
        cards.pack(fill="x", pady=(0, 10))
        self.card_values = {}
        labels = [("input_tokens", "输入 tokens"), ("output_tokens", "输出 tokens"), ("total_tokens", "总 tokens"), ("tps", "输出阶段 TPS ≈"),
                  ("cached_input_tokens", "缓存读取 tokens"), ("uncached_input_tokens", "未缓存输入 tokens"), ("reasoning_output_tokens", "其中：推理 tokens"), ("cache_rate", "缓存命中率")]
        for i, (key, title) in enumerate(labels):
            cards.columnconfigure(i % 4, weight=1)
            frame = tk.Frame(cards, bg="white", highlightbackground="#dde3ec", highlightthickness=1)
            frame.grid(row=i // 4, column=i % 4, sticky="nsew", padx=4, pady=4)
            tk.Label(frame, text=title, bg="white", fg=COLORS["muted"], font=("Microsoft YaHei UI", 10)).pack(anchor="w", padx=12, pady=(10, 0))
            value = tk.StringVar(value="—")
            self.card_values[key] = value
            tk.Label(frame, textvariable=value, bg="white", fg=COLORS["accent"] if key in ("tps", "cache_rate") else COLORS["ink"], font=("Segoe UI", 20, "bold")).pack(anchor="w", padx=12, pady=(3, 10))
        self.stats_info = ttk.Label(content, text="", foreground=COLORS["muted"], wraplength=980)
        self.stats_info.pack(anchor="w", pady=(0, 6))
        ttk.Label(content, text="缓存命中率 = 缓存读取 / 输入；输出已包含推理。TPS 按有效输出时间加权，排除压缩请求。", foreground=COLORS["muted"], font=("Microsoft YaHei UI", 9)).pack(anchor="w", pady=(0, 10))

        table_header = ttk.Frame(content)
        table_header.pack(fill="x", pady=(0, 6))
        ttk.Label(table_header, text="近期请求日志（最新在前）", font=("Microsoft YaHei UI", 11, "bold")).pack(side="left")
        ttk.Label(table_header, text="显示").pack(side="left", padx=(14, 5))
        limit_combo = ttk.Combobox(table_header, textvariable=self.limit, values=["50", "100", "200", "500", "全部"], width=6, state="readonly")
        limit_combo.pack(side="left")
        limit_combo.bind("<<ComboboxSelected>>", lambda _: self.render())
        ttk.Button(table_header, text="导出当前筛选 CSV", command=self.export_csv).pack(side="right")
        table_frame = ttk.Frame(content)
        table_frame.pack(fill="both", expand=True)
        columns = [("time", "时间（本地）", 126), ("model", "模型", 126), ("kind", "类型", 62), ("input", "输入", 92),
                   ("cached", "缓存读取", 92), ("hit", "命中率", 74), ("output", "输出", 78), ("reason", "推理", 74),
                   ("seconds", "输出秒数", 85), ("tps", "TPS ≈", 76), ("http", "含等待 TPS", 96)]
        self.log_tree = ttk.Treeview(table_frame, columns=[c[0] for c in columns], show="headings", selectmode="browse")
        for key, title, width in columns:
            self.log_tree.heading(key, text=title)
            self.log_tree.column(key, width=width, minwidth=width, stretch=False, anchor="w" if key in ("time", "model") else "e")
        self.log_tree.grid(row=0, column=0, sticky="nsew")
        vertical = ttk.Scrollbar(table_frame, orient="vertical", command=self.log_tree.yview)
        vertical.grid(row=0, column=1, sticky="ns")
        horizontal = ttk.Scrollbar(table_frame, orient="horizontal", command=self.log_tree.xview)
        horizontal.grid(row=1, column=0, sticky="ew")
        self.log_tree.configure(yscrollcommand=vertical.set, xscrollcommand=horizontal.set)
        table_frame.rowconfigure(0, weight=1)
        table_frame.columnconfigure(0, weight=1)
        self.log_tree.bind("<<TreeviewSelect>>", self.show_detail)
        self.log_tree.tag_configure("compaction", foreground="#9a6700")
        ttk.Label(content, text="选中一行查看请求详情；缺少计时数据时 TPS 留空。", foreground=COLORS["muted"], font=("Microsoft YaHei UI", 9)).pack(anchor="w", pady=(8, 4))
        detail_frame = ttk.Frame(content)
        detail_frame.pack(fill="x", pady=(0, 8))
        self.detail = tk.Text(detail_frame, height=6, wrap="word", bg="#eaf0f7", fg=COLORS["ink"], relief="flat", font=("Consolas", 9), padx=10, pady=8, state="disabled")
        self.detail.pack(side="left", fill="both", expand=True)
        detail_scroll = ttk.Scrollbar(detail_frame, orient="vertical", command=self.detail.yview)
        detail_scroll.pack(side="right", fill="y")
        self.detail.configure(yscrollcommand=detail_scroll.set)

    def worker(self, kind, generation, function):
        def run():
            try:
                self.events.put((kind, generation, function(), None))
            except Exception:
                self.events.put((kind, generation, None, traceback.format_exc()))
        threading.Thread(target=run, daemon=True).start()

    def scan(self):
        if self.scanning:
            return
        self.scanning = True
        self.status.set("正在扫描 session 索引…")
        home = self.home
        self.worker("scan", str(home), lambda: scan_sessions(home))

    def poll_events(self):
        try:
            while True:
                kind, generation, data, error = self.events.get_nowait()
                if kind == "scan":
                    if generation != str(self.home):
                        continue
                    self.scanning = False
                    if error:
                        self.status.set("扫描失败")
                        messagebox.showerror("读取失败", error, parent=self)
                        continue
                    self.sessions, scan_warnings = data
                    old_id = self.session.id if self.session else None
                    self.show_sessions()
                    match = next((s for s in self.sessions if s.id == old_id), None)
                    if match:
                        self.session = match
                        self.load_selected()
                    elif old_id:
                        self.session = None
                        self.requests = []
                        self.render()
                    if not self.session:
                        items = self.session_tree.get_children()
                        if items:
                            self.session_tree.selection_set(items[0])
                    self.status.set(f"找到 {len(self.sessions)} 个 session" + (f"；{len(scan_warnings)} 个文件无法识别" if scan_warnings else ""))
                elif kind == "load":
                    if generation != self.generation:
                        continue
                    self.loading = False
                    if error:
                        self.status.set("会话读取失败")
                        messagebox.showerror("读取失败", error, parent=self)
                        continue
                    self.requests, self.warnings, self.fingerprint = data
                    models = sorted({r.model for r in self.requests})
                    self.model_combo.configure(values=["全部模型", *models])
                    if self.model_filter.get() not in ["全部模型", *models]:
                        self.model_filter.set("全部模型")
                    self.render()
                    self.status.set(f"更新于 {dt.datetime.now():%H:%M:%S} · {len(self.requests)} 次请求 · 自动刷新{'开启' if self.auto.get() else '关闭'}" + (" · " + "；".join(self.warnings)[:240] if self.warnings else ""))
        except queue.Empty:
            pass
        self.after(100, self.poll_events)

    def show_sessions(self):
        if not hasattr(self, "session_tree"):
            return
        old_id = self.session.id if self.session else None
        self.session_tree.delete(*self.session_tree.get_children())
        query = self.search.get().strip().casefold()
        displayed = 0
        for session in self.sessions:
            text = f"{session.title} {session.cwd} {session.id} {session.model}".casefold()
            if query and query not in text:
                continue
            title = ("[归档] " if session.archived else "") + session.title
            project = Path(session.cwd).name if session.cwd else "—"
            self.session_tree.insert("", "end", iid=session.id, values=(title, project))
            displayed += 1
        self.session_count.configure(text=f"显示 {displayed} / {len(self.sessions)} 个会话")
        if old_id and self.session_tree.exists(old_id):
            self.session_tree.selection_set(old_id)

    def select_session(self, _event=None):
        selected = self.session_tree.selection()
        if not selected:
            return
        sid = selected[0]
        if self.session and self.session.id == sid:
            return
        self.session = next(s for s in self.sessions if s.id == sid)
        self.generation += 1
        self.loading = False
        self.fingerprint = ()
        self.requests = []
        self.model_filter.set("全部模型")
        title = self.session.title
        self.session_title.configure(text=title[:90] + ("…" if len(title) > 90 else ""))
        self.session_info.configure(text=f"项目：{self.session.cwd}\nID：{sid}  ·  {len(self.session.files)} 个日志文件（自动合并）")
        self.render()
        self.load_selected(force=True)

    def load_selected(self, force=False):
        if not self.session or self.loading:
            return
        session, home = self.session, self.home
        fingerprint = file_fingerprint(session.files)
        if not force and fingerprint == self.fingerprint:
            return
        self.generation += 1
        self.loading = True
        self.status.set("正在读取会话用量…")
        self.session_info.configure(text=f"项目：{session.cwd}\nID：{session.id}  ·  {len(session.files)} 个日志文件（自动合并）")
        self.worker("load", self.generation, lambda: (*load_requests(session, home), fingerprint))

    def auto_refresh(self):
        if self.auto.get():
            self.load_selected()
        self.after(5000, self.auto_refresh)

    def auto_scan(self):
        if self.auto.get() and not self.loading:
            self.scan()
        self.after(30000, self.auto_scan)

    def choose_home(self):
        directory = filedialog.askdirectory(title="选择包含 sessions 的 Codex 日志目录", initialdir=str(self.home), parent=self)
        if not directory:
            return
        self.home = Path(directory)
        self.path_label.configure(text=str(self.home))
        self.generation += 1
        self.session = None
        self.requests = []
        self.sessions = []
        self.loading = False
        self.scanning = False
        self.search.set("")
        self.session_title.configure(text="选择左侧会话开始查看")
        self.session_info.configure(text="")
        self.render()
        self.scan()

    def render(self):
        model = self.model_filter.get()
        self.filtered_requests = [r for r in self.requests if (model == "全部模型" or r.model == model) and (self.include_compactions.get() or not r.compaction)]
        summary = summarize(self.filtered_requests)
        self.last_summary = summary
        for key, variable in self.card_values.items():
            value = summary[key]
            if not self.filtered_requests:
                text = "—"
            elif key == "tps":
                text = fmt_rate(value)
            elif key == "cache_rate":
                text = f"{value:.2f}%" if value is not None else "无输入数据"
            else:
                text = f"{value:,}"
            variable.set(text)
        self.stats_info.configure(text=f"当前筛选 {summary['count']} 次请求 · 压缩 {summary['compactions']} 次 · TPS 样本 {summary['tps_samples']} 次 · 中位 TPS {fmt_rate(summary['median_tps'])} · 含等待 TPS {fmt_rate(summary['http_tps'])}（{summary['http_samples']} 个日志样本）")
        selected = self.log_tree.selection()
        selected_id = selected[0] if selected else None
        self.log_tree.delete(*self.log_tree.get_children())
        limit = len(self.filtered_requests) if self.limit.get() == "全部" else int(self.limit.get())
        self.displayed_requests = list(reversed(self.filtered_requests))[:limit]
        self.row_requests = {}
        for r in self.displayed_requests:
            hit = f"{r.cache_rate:.1f}%" if r.cache_rate is not None else "—"
            seconds = f"{r.stream_seconds:.2f}" if r.stream_seconds else "—"
            values = (readable_time(r.timestamp), r.model, "压缩" if r.compaction else "请求", f"{r.usage['input_tokens']:,}",
                      f"{r.usage['cached_input_tokens']:,}", hit, f"{r.usage['output_tokens']:,}", f"{r.usage['reasoning_output_tokens']:,}",
                      seconds, f"{r.tps:.2f}" if r.tps is not None and not r.compaction else "—",
                      f"{r.http_tps:.2f}" if r.http_tps is not None and not r.compaction else "—")
            self.log_tree.insert("", "end", iid=r.response_id, values=values, tags=("compaction",) if r.compaction else ())
            self.row_requests[r.response_id] = r
        if selected_id in self.row_requests:
            self.log_tree.selection_set(selected_id)
            self.show_detail()
        else:
            self.set_detail("")

    def set_detail(self, text):
        self.detail.configure(state="normal")
        self.detail.delete("1.0", "end")
        self.detail.insert("1.0", text)
        self.detail.configure(state="disabled")

    def show_detail(self, _event=None):
        selected = self.log_tree.selection()
        if not selected or selected[0] not in self.row_requests:
            return
        data = self.row_requests[selected[0]].export()
        self.set_detail(json.dumps(data, ensure_ascii=False, indent=2))

    def export_csv(self):
        if not self.filtered_requests:
            messagebox.showinfo("导出", "当前筛选没有请求记录。", parent=self)
            return
        path = filedialog.asksaveasfilename(title="导出当前筛选的全部请求", defaultextension=".csv", initialfile=f"codex_tokens_{self.session.id[:8]}_{dt.datetime.now():%Y%m%d_%H%M%S}.csv", filetypes=[("CSV", "*.csv")], parent=self)
        if not path:
            return
        try:
            with open(path, "w", encoding="utf-8-sig", newline="") as handle:
                writer = csv.DictWriter(handle, fieldnames=list(self.filtered_requests[0].export()))
                writer.writeheader()
                writer.writerows(r.export() for r in self.filtered_requests)
            self.status.set(f"已导出 {len(self.filtered_requests)} 次请求：{path}")
        except OSError as exc:
            messagebox.showerror("导出失败", str(exc), parent=self)

    def explain(self):
        messagebox.showinfo("计算口径", "用量：按 response_id 去重，合并同一 session 的续写文件；旧日志使用 token_count，避免与新格式重复累计。\n\n缓存命中率 = cached_input_tokens / input_tokens。输入已包含缓存读取；输出已包含推理，均不重复相加。总 tokens 包含上下文压缩，除非取消勾选。\n\n输出阶段 TPS ≈ 输出 tokens /（首个 Reasoning 或 AgentMessage 输出项开始至用量记录写入的秒数）。整体 TPS 按 token 和秒数加权，排除上下文压缩与无计时样本。日志未提供逐 token 时间，因此是近似值。\n\n含等待 TPS：仅限能在 logs_2.sqlite 配对 HTTP 请求时间的样本，包含等待首个输出及网络耗时；不混用两种口径。\n\n近期条数仅控制表格显示；顶部统计与 CSV 使用当前筛选的全部请求。时间显示为本机本地时区。\n\n只读取本地日志与 SQLite；不读取 auth.json，不访问网络。", parent=self)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--home", default=os.environ.get("CODEX_HOME") or str(Path.home() / ".codex"))
    parser.add_argument("--project", default="", help="Initial session search")
    args = parser.parse_args()
    try:
        if os.name == "nt":
            import ctypes
            try:
                ctypes.windll.shcore.SetProcessDpiAwareness(1)
            except (OSError, AttributeError):
                pass
        Viewer(args.home, args.project).mainloop()
    except Exception:
        error = traceback.format_exc()
        log = Path(__file__).with_suffix(".error.log")
        try:
            log.write_text(error, encoding="utf-8")
        except OSError:
            pass
        messagebox.showerror("Codex Session 查看器启动失败", error)


if __name__ == "__main__":
    main()
