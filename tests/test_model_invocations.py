import json
import tempfile
import unittest
from contextlib import contextmanager
from pathlib import Path

from codex_glass.core.invocations import live_model_activity, short_model_name
from codex_glass.core.live_telemetry import LiveTelemetrySampler


class _Reader:
    def __init__(self, rows):
        self.rows = rows

    def execute(self, _query, _params):
        return self

    def fetchall(self):
        return self.rows


class _Index:
    def __init__(self, rows):
        self.rows = rows

    @contextmanager
    def read_transaction(self):
        yield _Reader(self.rows)


def _write(path, records):
    path.write_text(
        "".join(json.dumps(row, separators=(",", ":")) + "\n" for row in records),
        encoding="utf-8",
    )


class ModelInvocationTests(unittest.TestCase):
    def test_short_names_preserve_unknown_models(self):
        self.assertEqual("Astra", short_model_name("gpt-6-astra"))
        self.assertEqual("custom-model", short_model_name("custom-model"))

    def test_live_file_is_discovered_before_sqlite_has_indexed_it(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            path = root / "new-session.jsonl"
            _write(
                path,
                [
                    {"type": "session_meta", "payload": {"session_id": "thread-new"}},
                    {
                        "type": "turn_context",
                        "payload": {"turn_id": "turn-new", "model": "gpt-6-astra"},
                    },
                    {"type": "event_msg", "payload": {"type": "task_started"}},
                ],
            )
            result = live_model_activity(None, root)
            self.assertTrue(result["active"])
            self.assertEqual("gpt-6-astra", result["agents"][0]["requested_model"])

    def test_active_main_and_subagent_are_linked_without_guessing_response(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            main = root / "main.jsonl"
            child = root / "child.jsonl"
            _write(
                main,
                [
                    {
                        "type": "session_meta",
                        "payload": {"session_id": "thread-1", "thread_source": "user"},
                    },
                    {
                        "type": "turn_context",
                        "payload": {"turn_id": "turn-1", "model": "gpt-6-astra"},
                    },
                    {
                        "type": "event_msg",
                        "payload": {"type": "task_started", "turn_id": "turn-1"},
                    },
                ],
            )
            _write(
                child,
                [
                    {
                        "type": "session_meta",
                        "payload": {
                            "session_id": "thread-1",
                            "thread_source": "subagent",
                            "source": {
                                "subagent": {
                                    "thread_spawn": {
                                        "parent_thread_id": "thread-1",
                                        "depth": 1,
                                        "agent_path": "/root/review",
                                        "agent_nickname": "Euler",
                                    }
                                }
                            },
                        },
                    },
                    {
                        "type": "turn_context",
                        "payload": {
                            "turn_id": "turn-child",
                            "root_turn_id": "turn-1",
                            "model": "gpt-5.6-sol",
                        },
                    },
                    {
                        "type": "event_msg",
                        "payload": {"type": "task_started", "turn_id": "turn-child"},
                    },
                ],
            )
            rows = [
                {"path": str(child), "last_model": "gpt-5.6-sol", "mtime_ns": 2},
                {"path": str(main), "last_model": "gpt-6-astra", "mtime_ns": 1},
            ]
            result = live_model_activity(_Index(rows))
            self.assertTrue(result["active"])
            self.assertEqual(
                ["主代理", "Euler"], [row["name"] for row in result["agents"]]
            )
            self.assertEqual("gpt-5.6-sol", result["agents"][1]["requested_model"])
            self.assertIsNone(result["agents"][0]["response_model"])
            self.assertEqual("unknown", result["agents"][0]["model_match"])

    def test_explicit_response_model_can_report_mismatch(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "main.jsonl"
            _write(
                path,
                [
                    {"type": "session_meta", "payload": {"session_id": "thread-1"}},
                    {
                        "type": "turn_context",
                        "payload": {"turn_id": "turn-1", "model": "gpt-6-astra"},
                    },
                    {"type": "event_msg", "payload": {"type": "task_started"}},
                    {
                        "type": "event_msg",
                        "payload": {
                            "type": "response.completed",
                            "model": "gpt-5.6-luna",
                        },
                    },
                ],
            )
            result = live_model_activity(
                _Index(
                    [{"path": str(path), "last_model": "gpt-6-astra", "mtime_ns": 1}]
                )
            )
            self.assertEqual("gpt-5.6-luna", result["agents"][0]["response_model"])
            self.assertEqual("mismatch", result["agents"][0]["model_match"])
            self.assertEqual("response.model", result["agents"][0]["response_evidence"])

    def test_completed_task_is_not_presented_as_live(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "main.jsonl"
            _write(
                path,
                [
                    {"type": "session_meta", "payload": {"session_id": "thread-1"}},
                    {
                        "type": "turn_context",
                        "payload": {"turn_id": "turn-1", "model": "gpt-6-astra"},
                    },
                    {"type": "event_msg", "payload": {"type": "task_started"}},
                    {"type": "event_msg", "payload": {"type": "task_complete"}},
                ],
            )
            result = live_model_activity(
                _Index(
                    [{"path": str(path), "last_model": "gpt-6-astra", "mtime_ns": 1}]
                )
            )
            self.assertFalse(result["active"])
            self.assertEqual([], result["agents"])

    def test_response_model_from_previous_turn_is_not_reused(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "main.jsonl"
            _write(
                path,
                [
                    {"type": "session_meta", "payload": {"session_id": "thread-1"}},
                    {
                        "type": "turn_context",
                        "payload": {"turn_id": "old", "model": "gpt-5.6-luna"},
                    },
                    {
                        "type": "event_msg",
                        "payload": {
                            "type": "response.completed",
                            "model": "gpt-5.6-luna",
                        },
                    },
                    {
                        "type": "turn_context",
                        "payload": {"turn_id": "new", "model": "gpt-6-astra"},
                    },
                    {"type": "event_msg", "payload": {"type": "task_started"}},
                ],
            )
            result = live_model_activity(
                _Index(
                    [{"path": str(path), "last_model": "gpt-6-astra", "mtime_ns": 1}]
                )
            )
            self.assertEqual("gpt-6-astra", result["agents"][0]["requested_model"])
            self.assertIsNone(result["agents"][0]["response_model"])
            self.assertEqual("unknown", result["agents"][0]["model_match"])

    def test_two_conversations_and_their_children_never_mix_throughput(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            rows = []
            for suffix, output, child_output in (("a", 60, 30), ("b", 120, 180)):
                main = root / f"main-{suffix}.jsonl"
                child = root / f"child-{suffix}.jsonl"
                base = {"timestamp": "2026-09-23T00:00:20Z"}
                _write(main, [
                    {"type": "session_meta", "payload": {"session_id": f"root-{suffix}"}},
                    {"type": "turn_context", "payload": {"turn_id": f"turn-{suffix}", "model": "gpt-6-astra"}},
                    {"type": "event_msg", "payload": {"type": "task_started"}},
                    dict(base, type="event_msg", payload={"type": "token_count", "info": {
                        "total_token_usage": {"input_tokens": 100, "cached_input_tokens": 20, "output_tokens": output}
                    }}),
                ])
                _write(child, [
                    {"type": "session_meta", "payload": {"session_id": f"child-{suffix}", "source": {
                        "subagent": {"thread_spawn": {"parent_thread_id": f"root-{suffix}", "agent_nickname": f"Agent-{suffix}"}}
                    }}},
                    {"type": "turn_context", "payload": {"turn_id": f"child-turn-{suffix}", "root_turn_id": f"turn-{suffix}", "model": "gpt-5.6-sol"}},
                    {"type": "event_msg", "payload": {"type": "task_started"}},
                    dict(base, type="event_msg", payload={"type": "token_count", "info": {
                        "total_token_usage": {"input_tokens": 200, "cached_input_tokens": 100, "output_tokens": child_output}
                    }}),
                ])
                rows.extend([
                    {"path": str(main), "last_model": "gpt-6-astra", "mtime_ns": 2 if suffix == "b" else 1},
                    {"path": str(child), "last_model": "gpt-5.6-sol", "mtime_ns": 2 if suffix == "b" else 1},
                ])
            nested = root / "nested-a.jsonl"
            _write(nested, [
                {"type": "session_meta", "payload": {"session_id": "nested-a", "source": {
                    "subagent": {"thread_spawn": {"parent_thread_id": "child-a", "agent_nickname": "Nested"}}
                }}},
                {"type": "turn_context", "payload": {"turn_id": "nested-turn", "root_turn_id": "turn-a", "model": "gpt-5.6-terra"}},
                {"type": "event_msg", "payload": {"type": "task_started"}},
                {"timestamp": "2026-09-23T00:00:20Z", "type": "event_msg", "payload": {"type": "token_count", "info": {
                    "total_token_usage": {"input_tokens": 100, "cached_input_tokens": 40, "output_tokens": 30}
                }}},
            ])
            rows.append({"path": str(nested), "last_model": "gpt-5.6-terra", "mtime_ns": 1})
            result = live_model_activity(_Index(rows), sampler=LiveTelemetrySampler(), now=1790121630)
            conversations = {row["conversation_id"]: row for row in result["conversations"]}
            self.assertEqual({"root-a", "root-b"}, set(conversations))
            self.assertEqual(2.0, conversations["root-a"]["telemetry"]["output_tps"])
            self.assertEqual(5.0, conversations["root-b"]["telemetry"]["output_tps"])
            self.assertEqual(1.0, conversations["root-a"]["agents"][0]["telemetry"]["output_tps"])
            self.assertEqual(0.5, conversations["root-a"]["agents"][1]["telemetry"]["output_tps"])
            self.assertEqual(3, len(conversations["root-a"]["agents"]))
            self.assertEqual(40.0, conversations["root-a"]["telemetry"]["cache_hit_percent"])
            self.assertEqual(2, len(result["conversations"]))


if __name__ == "__main__":
    unittest.main()
