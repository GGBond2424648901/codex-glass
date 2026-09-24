import json
import tempfile
import time
import tracemalloc
import unittest
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from unittest import mock

from codex_glass.core.invocations import live_model_activity, short_model_name
from codex_glass.core.live_telemetry import LiveTelemetrySampler


class _Reader:
    def __init__(self, rows):
        self.rows = rows

    def execute(self, query, params):
        self.result = self.rows[:params[0]] if "LIMIT ?" in query else self.rows
        return self

    def fetchall(self):
        return self.result

    def __iter__(self):
        return iter(self.result)


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
    def test_inactive_dashboard_checks_release_old_sampler_states(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            path = root / "old-session.jsonl"
            path.write_bytes(b"")
            sampler = LiveTelemetrySampler()
            sampler.sample(path, now=100)
            self.assertIn(str(path), sampler._states)
            result = live_model_activity(None, root, sampler=sampler, now=401)
            self.assertFalse(result["active"])
            self.assertNotIn(str(path), sampler._states)

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

    def test_new_conversation_is_discovered_on_next_three_second_check(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)

            def create(name):
                _write(root / f"{name}.jsonl", [
                    {"type": "session_meta", "payload": {"session_id": name}},
                    {"type": "turn_context", "payload": {"turn_id": name, "model": "gpt-6-astra"}},
                    {"type": "event_msg", "payload": {"type": "task_started"}},
                ])

            create("first")
            with mock.patch("codex_glass.core.invocations.time.monotonic", return_value=100.0):
                first = live_model_activity(None, root)
            self.assertEqual(["first"], [row["conversation_id"] for row in first["conversations"]])
            create("second")
            with mock.patch("codex_glass.core.invocations.time.monotonic", return_value=103.1):
                second = live_model_activity(None, root)
            self.assertEqual({"first", "second"}, {row["conversation_id"] for row in second["conversations"]})

    def test_new_dated_conversation_is_found_without_rewalking_the_archive(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            today = datetime.now()
            current_day = root / f"{today:%Y}" / f"{today:%m}" / f"{today:%d}"
            current_day.mkdir(parents=True)

            def create(name):
                _write(current_day / f"{name}.jsonl", [
                    {"type": "session_meta", "payload": {"session_id": name}},
                    {"type": "turn_context", "payload": {"turn_id": name, "model": "gpt-6-sol"}},
                    {"type": "event_msg", "payload": {"type": "task_started"}},
                ])

            create("before")
            with mock.patch("codex_glass.core.invocations.time.monotonic", return_value=100.0):
                live_model_activity(None, root, sampler=LiveTelemetrySampler())
            create("after")
            with mock.patch("codex_glass.core.invocations.time.monotonic", return_value=103.1), mock.patch.object(
                Path, "rglob", side_effect=AssertionError("historical tree was walked again")
            ):
                result = live_model_activity(None, root, sampler=LiveTelemetrySampler())
            self.assertEqual({"before", "after"}, {row["conversation_id"] for row in result["conversations"]})

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
                    {"timestamp": "2026-09-23T00:00:20Z", "type": "event_msg", "payload": {
                        "type": "token_count", "info": {"total_token_usage": {
                            "input_tokens": 100, "cached_input_tokens": 20, "output_tokens": 60
                        }}
                    }},
                ],
            )
            result = live_model_activity(
                _Index(
                    [{"path": str(path), "last_model": "gpt-6-astra", "mtime_ns": 1}]
                ), now=1790121630
            )
            self.assertFalse(result["active"])
            self.assertEqual([], result["agents"])

    def test_unmarked_session_is_removed_when_activity_is_stale(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "stale.jsonl"
            _write(path, [
                {"type": "session_meta", "payload": {"session_id": "stale"}},
                {"timestamp": "2026-09-23T00:00:10Z", "type": "turn_context", "payload": {
                    "turn_id": "stale", "model": "gpt-6-astra"
                }},
                {"timestamp": "2026-09-23T00:00:20Z", "type": "event_msg", "payload": {
                    "type": "token_count", "info": {"total_token_usage": {
                        "input_tokens": 100, "cached_input_tokens": 20, "output_tokens": 60
                    }}
                }},
            ])
            result = live_model_activity(None, Path(directory), now=1790121660)
            self.assertEqual([], result["conversations"])

    def test_recent_usage_without_task_markers_keeps_second_conversation_visible(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            rows = []
            for name, include_start in (("first", True), ("second", False)):
                path = root / f"{name}.jsonl"
                records = [
                    {"type": "session_meta", "payload": {"session_id": name}},
                    {"timestamp": "2026-09-23T00:00:10Z", "type": "turn_context", "payload": {
                        "turn_id": f"turn-{name}", "model": "gpt-6-astra"
                    }},
                ]
                if include_start:
                    records.append({"type": "event_msg", "payload": {"type": "task_started"}})
                records.append({
                    "timestamp": "2026-09-23T00:00:20Z", "type": "event_msg", "payload": {
                        "type": "token_count", "info": {"total_token_usage": {
                            "input_tokens": 100, "cached_input_tokens": 20, "output_tokens": 60
                        }}
                    }
                })
                _write(path, records)
                rows.append({"path": str(path), "last_model": "gpt-6-astra", "mtime_ns": path.stat().st_mtime_ns})
            result = live_model_activity(_Index(rows), sampler=LiveTelemetrySampler(), now=1790121630)
            self.assertEqual({"first", "second"}, {row["conversation_id"] for row in result["conversations"]})

    def test_new_turn_without_task_marker_is_visible_before_first_usage(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "new-turn.jsonl"
            _write(path, [
                {"type": "session_meta", "payload": {"session_id": "new-turn"}},
                {"timestamp": "2026-09-23T00:00:20Z", "type": "turn_context", "payload": {
                    "turn_id": "new-turn", "model": "gpt-6-astra"
                }},
            ])
            result = live_model_activity(None, Path(directory), now=1790121630)
            self.assertEqual(["new-turn"], [row["conversation_id"] for row in result["conversations"]])
            self.assertIsNone(result["conversations"][0]["telemetry"]["output_tps"])

    def test_completed_previous_turn_does_not_hide_new_recent_turn(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "resumed.jsonl"
            _write(path, [
                {"type": "session_meta", "payload": {"session_id": "resumed"}},
                {"type": "turn_context", "payload": {"turn_id": "old", "model": "gpt-6-astra"}},
                {"type": "event_msg", "payload": {"type": "task_complete"}},
                {"timestamp": "2026-09-23T00:00:20Z", "type": "turn_context", "payload": {
                    "turn_id": "new", "model": "gpt-6-sol"
                }},
            ])
            result = live_model_activity(None, Path(directory), now=1790121630)
            self.assertEqual(["resumed"], [row["conversation_id"] for row in result["conversations"]])

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
            sampler = LiveTelemetrySampler()
            result = live_model_activity(_Index(rows), sampler=sampler, now=1790121621)
            conversations = {row["conversation_id"]: row for row in result["conversations"]}
            self.assertEqual({"root-a", "root-b"}, set(conversations))
            self.assertEqual(100.0, conversations["root-b"]["telemetry"]["output_tps"])
            self.assertIsNone(conversations["root-a"]["telemetry"])
            self.assertEqual({str(root / "main-b.jsonl"), str(root / "child-b.jsonl")}, set(sampler._states))
            switched = live_model_activity(
                _Index(rows), selected_conversation_id="root-a", sampler=sampler, now=1790121621
            )
            by_id = {row["conversation_id"]: row for row in switched["conversations"]}
            self.assertEqual("root-a", switched["selected_conversation_id"])
            self.assertEqual(40.0, by_id["root-a"]["telemetry"]["output_tps"])
            self.assertIsNone(by_id["root-b"]["telemetry"])
            self.assertEqual(20.0, by_id["root-a"]["agents"][0]["telemetry"]["output_tps"])
            self.assertEqual(10.0, by_id["root-a"]["agents"][1]["telemetry"]["output_tps"])
            self.assertEqual(
                {str(root / "main-a.jsonl"), str(root / "child-a.jsonl"), str(root / "nested-a.jsonl")},
                set(sampler._states),
            )
            self.assertEqual(3, len(conversations["root-a"]["agents"]))
            self.assertEqual(40.0, by_id["root-a"]["telemetry"]["cache_hit_percent"])
            self.assertEqual(2, len(result["conversations"]))

    def test_many_newer_subagents_do_not_hide_older_running_roots(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            rows = []
            for name, mtime in (("root-a", 2), ("root-b", 1)):
                path = root / f"{name}.jsonl"
                _write(path, [
                    {"type": "session_meta", "payload": {"session_id": name}},
                    {"type": "turn_context", "payload": {"turn_id": name, "model": "gpt-6-astra"}},
                    {"type": "event_msg", "payload": {"type": "task_started"}},
                ])
                rows.append({"path": str(path), "last_model": "gpt-6-astra", "mtime_ns": mtime})
            for number in range(30):
                path = root / f"child-{number}.jsonl"
                _write(path, [
                    {"type": "session_meta", "payload": {"session_id": f"child-{number}", "source": {
                        "subagent": {"thread_spawn": {"parent_thread_id": "root-a"}}
                    }}},
                    {"type": "turn_context", "payload": {"turn_id": f"child-{number}", "model": "gpt-6-sol"}},
                    {"type": "event_msg", "payload": {"type": "task_started"}},
                ])
                rows.append({"path": str(path), "last_model": "gpt-6-sol", "mtime_ns": 100 + number})
            result = live_model_activity(_Index(rows))
            self.assertEqual({"root-a", "root-b"}, {row["conversation_id"] for row in result["conversations"]})

    def test_newer_completed_children_cannot_hide_running_root_in_file_discovery(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            main = root / "main.jsonl"
            _write(main, [
                {"type": "session_meta", "payload": {"session_id": "running-root"}},
                {"type": "turn_context", "payload": {"turn_id": "running", "model": "gpt-6-astra"}},
                {"type": "event_msg", "payload": {"type": "task_started"}},
            ])
            for number in range(60):
                child = root / f"child-{number}.jsonl"
                _write(child, [
                    {"type": "session_meta", "payload": {"session_id": f"child-{number}", "source": {
                        "subagent": {"thread_spawn": {"parent_thread_id": "running-root"}}
                    }}},
                    {"type": "turn_context", "payload": {"turn_id": f"child-{number}", "model": "gpt-6-sol"}},
                    {"type": "event_msg", "payload": {"type": "task_complete"}},
                ])
            result = live_model_activity(None, root, sampler=LiveTelemetrySampler())
            self.assertEqual(["running-root"], [row["conversation_id"] for row in result["conversations"]])

    def test_indexed_children_cannot_hide_older_running_root(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            main = root / "main.jsonl"
            _write(main, [
                {"type": "session_meta", "payload": {"session_id": "root"}},
                {"type": "turn_context", "payload": {"turn_id": "root", "model": "gpt-6-astra"}},
                {"type": "event_msg", "payload": {"type": "task_started"}},
            ])
            rows = []
            for number in range(60):
                child = root / f"child-{number}.jsonl"
                _write(child, [
                    {"type": "session_meta", "payload": {"session_id": f"child-{number}", "source": {
                        "subagent": {"thread_spawn": {"parent_thread_id": "root"}}
                    }}},
                    {"type": "event_msg", "payload": {"type": "task_complete"}},
                ])
                rows.append({"path": str(child), "last_model": None, "mtime_ns": 1000 - number})
            rows.append({"path": str(main), "last_model": None, "mtime_ns": 1})
            result = live_model_activity(_Index(rows), sampler=LiveTelemetrySampler())
            self.assertEqual(["root"], [row["conversation_id"] for row in result["conversations"]])

    def test_index_fallback_model_is_preserved_when_turn_context_is_missing(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "main.jsonl"
            _write(path, [
                {"type": "session_meta", "payload": {"session_id": "indexed-root"}},
                {"type": "event_msg", "payload": {"type": "task_started"}},
            ])
            result = live_model_activity(_Index([
                {"path": str(path), "last_model": "gpt-6-sol", "mtime_ns": 1}
            ]), sampler=LiveTelemetrySampler())
            self.assertEqual("gpt-6-sol", result["agents"][0]["requested_model"])

    def test_candidate_ranking_memory_is_bounded_with_large_archive(self):
        from codex_glass.core.invocations import _rank_session_paths

        class FakePath:
            def __init__(self, number):
                self.number = number

            def __str__(self):
                return f"session-{self.number}.jsonl"

        number_offset = time.time_ns()
        with mock.patch("codex_glass.core.invocations._session_role", new=lambda _path: ("subagent", None)):
            tracemalloc.start()
            try:
                selected = _rank_session_paths(
                    ((FakePath(number), number_offset + number, None) for number in range(20000)),
                    24, None, recent_only=True,
                )
                _, peak = tracemalloc.get_traced_memory()
            finally:
                tracemalloc.stop()
        self.assertEqual(96, len(selected))
        self.assertLess(peak, 2 * 1024 * 1024)

    def test_long_unmarked_log_with_recent_usage_is_visible(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            path = root / "long-running.jsonl"
            _write(path, [{"type": "session_meta", "payload": {"session_id": "long-running"}}])
            with path.open("ab") as stream:
                stream.write(b'{"type":"message","text":"' + b"x" * (5 * 1024 * 1024) + b'"}\n')
                stamp = datetime.now().astimezone().isoformat()
                for record in [
                    {"timestamp": stamp, "type": "turn_context", "payload": {
                        "turn_id": "latest", "model": "gpt-6-sol"
                    }},
                    {"timestamp": stamp, "type": "event_msg", "payload": {
                        "type": "token_count", "info": {"total_token_usage": {
                            "input_tokens": 100, "cached_input_tokens": 20, "output_tokens": 60
                        }}
                    }},
                ]:
                    stream.write(json.dumps(record, separators=(",", ":")).encode() + b"\n")
            result = live_model_activity(None, root, sampler=LiveTelemetrySampler())
            self.assertEqual(["long-running"], [row["conversation_id"] for row in result["conversations"]])

    def test_modified_large_active_log_is_projected_without_multimegabyte_tail_copy(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "large-active.jsonl"
            chatter = json.dumps({"type": "response_item", "payload": {"text": "x" * 900}}).encode() + b"\n"
            with path.open("wb") as stream:
                stream.write(b'{"type":"session_meta","payload":{"session_id":"large-active"}}\n')
                for _ in range(7000):
                    stream.write(chatter)
                stream.write(b'{"type":"turn_context","payload":{"turn_id":"large-turn","model":"gpt-6-sol"}}\n')
                stream.write(b'{"type":"event_msg","payload":{"type":"task_started"}}\n')
                stream.write(b'{"timestamp":"2026-09-23T00:00:10Z","type":"event_msg","payload":{"type":"token_count","info":{"total_token_usage":{"input_tokens":100,"cached_input_tokens":20,"output_tokens":120},"last_token_usage":{"input_tokens":100,"cached_input_tokens":20,"output_tokens":120}}}}\n')
            rows = [{"path": str(path), "mtime_ns": path.stat().st_mtime_ns, "last_model": None}]
            tracemalloc.start()
            try:
                result = live_model_activity(_Index(rows), sampler=LiveTelemetrySampler(), now=1790121611)
                _, peak = tracemalloc.get_traced_memory()
            finally:
                tracemalloc.stop()
            self.assertEqual(40.0, result["conversations"][0]["telemetry"]["output_tps"])
            self.assertLess(peak, 4 * 1024 * 1024, f"active poll allocated {peak:,} bytes")


if __name__ == "__main__":
    unittest.main()
