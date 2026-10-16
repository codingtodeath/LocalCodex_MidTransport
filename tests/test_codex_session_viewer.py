"""Standalone tests: python -m unittest discover -s tests -p test_codex_session_viewer.py."""
import importlib.util
from contextlib import closing
import json
from pathlib import Path
import sqlite3
import sys
import tempfile
import unittest


SPEC = importlib.util.spec_from_file_location("codex_session_viewer", Path(__file__).parents[1] / "tools" / "codex_session_viewer.py")
viewer = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = viewer
SPEC.loader.exec_module(viewer)


def row(kind, payload, seconds):
    return {"type": kind, "payload": payload, "timestamp": f"2026-10-04T00:00:{seconds:02d}.000Z"}


def usage(inputs=1000, cached=800, outputs=200, reasoning=80):
    return {"input_tokens": inputs, "cached_input_tokens": cached, "output_tokens": outputs,
            "reasoning_output_tokens": reasoning, "total_tokens": inputs + outputs}


class UsageTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.home = Path(self.temp.name)
        self.addCleanup(self.temp.cleanup)

    def write(self, filename, rows):
        path = self.home / "sessions" / filename
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("\n".join(json.dumps(r) for r in rows) + "\n", encoding="utf-8")
        return path

    def header(self):
        return row("session_meta", {"id": "session-one", "cwd": "E:\\test\\project"}, 0)

    def request(self, rid, seconds, values=None):
        return row("token_usage_record", {"response_id": rid, "thread_id": "session-one", "turn_id": "turn-one", "usage": values or usage()}, seconds)

    def start(self, seconds):
        time = viewer.epoch(f"2026-10-04T00:00:{seconds:02d}.000Z")
        return row("event_msg", {"type": "item_completed", "item": {"type": "Reasoning"}, "started_at_ms": time * 1000}, seconds + 1)

    def test_merge_deduplicate_compaction_and_subset_accounting(self):
        context = row("turn_context", {"model": "gpt-test", "effort": "high"}, 0)
        first = self.write("01.jsonl", [self.header(), context, self.start(1), self.request("r1", 11),
                                      row("event_msg", {"type": "token_count", "info": {"last_token_usage": usage(), "total_token_usage": usage()}}, 12),
                                      self.request("compressed", 20, usage(2000, 1000, 100, 0)), row("compacted", {}, 20)])
        second = self.write("02.jsonl", [self.header(), context, self.start(1), self.request("r1", 11),
                                       self.start(21), self.request("r2", 31, usage(1000, 500, 400, 200))])
        sessions, warnings = viewer.scan_sessions(self.home)
        self.assertEqual(len(sessions), 1)
        self.assertEqual(len(sessions[0].files), 2)
        requests, warnings = viewer.load_requests(sessions[0], self.home)
        self.assertFalse(warnings)
        self.assertEqual(len(requests), 3)
        summary = viewer.summarize(requests)
        self.assertEqual(summary["input_tokens"], 4000)
        self.assertEqual(summary["output_tokens"], 700)
        self.assertEqual(summary["total_tokens"], 4700)
        self.assertEqual(summary["reasoning_output_tokens"], 280)
        self.assertAlmostEqual(summary["cache_rate"], 57.5)
        self.assertEqual(summary["compactions"], 1)
        self.assertEqual(summary["tps_samples"], 2)
        self.assertEqual(summary["tps"], 30)
        without_compaction = viewer.summarize([r for r in requests if not r.compaction])
        self.assertEqual(without_compaction["cache_rate"], 65)
        self.assertEqual(without_compaction["total_tokens"], 2600)

    def test_old_counters_repeat_without_new_request(self):
        count = row("event_msg", {"type": "token_count", "info": {"last_token_usage": usage(), "total_token_usage": usage()}}, 11)
        self.write("old.jsonl", [self.header(), row("turn_context", {"model": "legacy-model"}, 0), self.start(1), count, count])
        sessions, _ = viewer.scan_sessions(self.home)
        requests, _ = viewer.load_requests(sessions[0], self.home)
        self.assertEqual(len(requests), 1)
        self.assertEqual(requests[0].usage_source, "token_count")
        self.assertEqual(requests[0].tps, 20)

    def test_missing_timing_is_not_zero_and_partial_tail_does_not_break_loading(self):
        path = self.write("partial.jsonl", [self.header(), self.request("no-timing", 10)])
        with path.open("a", encoding="utf-8") as f:
            f.write('{"type": "token_usage_record",')
        sessions, _ = viewer.scan_sessions(self.home)
        requests, warnings = viewer.load_requests(sessions[0], self.home)
        self.assertEqual(len(requests), 1)
        self.assertTrue(warnings)
        self.assertIsNone(requests[0].tps)
        self.assertIsNone(viewer.summarize(requests)["tps"])

    def test_native_duplicate_ui_record_does_not_reset_next_output(self):
        path = self.write("native.jsonl", [self.header(), self.start(1), self.request("r1", 11),
            row("event_msg", {"type": "token_count", "info": {"last_token_usage": usage(), "total_token_usage": usage()}}, 12),
            self.start(21),
            row("event_msg", {"type": "token_count", "info": {"last_token_usage": usage(), "total_token_usage": usage()}}, 25),
            self.request("r2", 31)])
        requests, _ = viewer.load_requests(viewer.Session("session-one", files=[path]), self.home)
        self.assertEqual([r.tps for r in requests], [20, 20])

    def test_http_timing_matches_turn_and_response_interval(self):
        path = self.write("http.jsonl", [self.header(), self.start(1), self.request("r1", 11), self.start(21), self.request("r2", 31)])
        with closing(sqlite3.connect(self.home / "logs_2.sqlite")) as c:
            c.execute("CREATE TABLE logs (id INTEGER PRIMARY KEY,ts INTEGER,ts_nanos INTEGER,thread_id TEXT,target TEXT,feedback_log_body TEXT)")
            for i, (seconds, turn) in enumerate([(0, "turn-one"), (20, "other-turn")]):
                c.execute("INSERT INTO logs VALUES (?,?,?,?,?,?)", (i, int(viewer.epoch(f"2026-10-04T00:00:{seconds:02d}.000Z")), 0,
                    "session-one", "codex_http_client::request", f"turn{{turn.id={turn}}}: Compressed request body"))
            c.commit()
        requests, _ = viewer.load_requests(viewer.Session("session-one", files=[path]), self.home)
        self.assertEqual(requests[0].http_seconds, 11)
        self.assertIsNone(requests[1].http_seconds)
        self.assertEqual(viewer.summarize(requests)["http_samples"], 1)

    def test_zero_input_and_nested_api_usage(self):
        result = viewer.normalize_usage({"input_tokens": 10, "output_tokens": 5, "input_tokens_details": {"cached_tokens": 4}, "output_tokens_details": {"reasoning_tokens": 2}})
        self.assertEqual(result["total_tokens"], 15)
        self.assertEqual(result["cached_input_tokens"], 4)
        self.assertEqual(result["reasoning_output_tokens"], 2)
        self.assertIsNone(viewer.percentage(0, 0))


if __name__ == "__main__":
    unittest.main()
