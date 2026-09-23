import json
import tempfile
import tracemalloc
import unittest
from pathlib import Path

from codex_glass.core.live_telemetry import LiveTelemetrySampler


def usage(at, input_tokens, cached, output, last=None):
    info = {
        "total_token_usage": {
            "input_tokens": input_tokens,
            "cached_input_tokens": cached,
            "output_tokens": output,
            "total_tokens": input_tokens + output,
        }
    }
    if last is not None:
        info["last_token_usage"] = last
    return {
        "timestamp": f"2026-09-23T00:00:{at:02d}Z",
        "type": "event_msg",
        "payload": {"type": "token_count", "info": info},
    }


def append(path, *records):
    with path.open("a", encoding="utf-8") as stream:
        for record in records:
            stream.write(json.dumps(record, separators=(",", ":")) + "\n")


class LiveTelemetryTests(unittest.TestCase):
    BASE = 1790121600.0  # 2026-09-23T00:00:00Z

    def test_incremental_records_and_duplicate_cumulative_snapshots(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "session.jsonl"
            append(path, usage(10, 100, 20, 120))
            sampler = LiveTelemetrySampler()
            first = sampler.sample(path, now=self.BASE + 20, active=True)
            self.assertEqual(2.0, first["output_tps"])
            self.assertEqual(20.0, first["cache_hit_percent"])
            append(path, usage(20, 100, 20, 120))
            self.assertEqual(first["output_tokens_60s"], sampler.sample(path, now=self.BASE + 21, active=True)["output_tokens_60s"])
            append(path, usage(25, 300, 120, 180))
            next_sample = sampler.sample(path, now=self.BASE + 30, active=True)
            self.assertEqual(3.0, next_sample["output_tps"])
            self.assertAlmostEqual(40.0, next_sample["cache_hit_percent"])

    def test_stale_rate_is_unavailable_and_completed_turn_is_inactive(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "session.jsonl"
            append(path, usage(10, 100, 10, 60))
            sampler = LiveTelemetrySampler()
            self.assertEqual("recent", sampler.sample(path, now=self.BASE + 11, active=True)["status"])
            stale = sampler.sample(path, now=self.BASE + 90, active=True)
            self.assertIsNone(stale["output_tps"])
            self.assertEqual("awaiting_usage", stale["status"])
            self.assertIsNone(sampler.sample(path, now=self.BASE + 90, active=False)["output_tps"])

    def test_truncation_does_not_reuse_old_cumulative_baseline(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "session.jsonl"
            append(path, usage(10, 100, 20, 80), usage(20, 200, 50, 120))
            sampler = LiveTelemetrySampler()
            self.assertEqual(120, sampler.sample(path, now=self.BASE + 25, active=True)["output_tokens_60s"])
            path.write_text(json.dumps(usage(30, 20, 10, 5)) + "\n", encoding="utf-8")
            sample = sampler.sample(path, now=self.BASE + 31, active=True)
            self.assertEqual(5, sample["output_tokens_60s"])

    def test_malformed_and_partial_lines_are_ignored_until_complete(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "session.jsonl"
            encoded = json.dumps(usage(10, 100, 20, 60))
            path.write_text("{bad}\n" + encoded[:45], encoding="utf-8")
            sampler = LiveTelemetrySampler()
            self.assertIsNone(sampler.sample(path, now=self.BASE + 12, active=True)["output_tps"])
            with path.open("a", encoding="utf-8") as stream:
                stream.write(encoded[45:] + "\n")
            self.assertEqual(1.0, sampler.sample(path, now=self.BASE + 12, active=True)["output_tps"])

    def test_top_level_legacy_usage_and_same_size_replacement(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "session.jsonl"
            first = usage(10, 100, 20, 60)
            first["type"] = "token_count"
            first["info"] = first.pop("payload")["info"]
            append(path, first)
            sampler = LiveTelemetrySampler()
            self.assertEqual(1.0, sampler.sample(path, now=self.BASE + 12)["output_tps"])
            replacement = usage(20, 100, 20, 90)
            replacement["type"] = "token_count"
            replacement["info"] = replacement.pop("payload")["info"]
            # Replacement is padded to match the previous byte count.
            old_size = path.stat().st_size
            body = json.dumps(replacement, separators=(",", ":"))
            path.write_bytes((body + " " * (old_size - len(body) - 1) + "\n").encode("utf-8"))
            self.assertEqual(old_size, path.stat().st_size)
            self.assertEqual(1.5, sampler.sample(path, now=self.BASE + 22)["output_tps"])

    def test_cold_tail_scanning_has_bounded_peak_memory(self):
        """A multi-megabyte session must not be materialized for one rate sample."""
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "large-session.jsonl"
            chatter = json.dumps({"type": "response_item", "payload": {"text": "x" * 900}}).encode() + b"\n"
            with path.open("wb") as stream:
                for _ in range(7000):
                    stream.write(chatter)
                stream.write(
                    (json.dumps(usage(10, 100, 20, 60, last={
                        "input_tokens": 100, "cached_input_tokens": 20, "output_tokens": 60
                    }), separators=(",", ":")) + "\n").encode()
                )
            sampler = LiveTelemetrySampler()
            tracemalloc.start()
            try:
                sample = sampler.sample(path, now=self.BASE + 12)
                _current, peak = tracemalloc.get_traced_memory()
            finally:
                tracemalloc.stop()
            self.assertEqual(1.0, sample["output_tps"])
            self.assertLess(peak, 3 * 1024 * 1024, f"cold sample allocated {peak:,} bytes")

    def test_inactive_session_buffers_expire_instead_of_lingering(self):
        with tempfile.TemporaryDirectory() as directory:
            first = Path(directory) / "old.jsonl"
            later = Path(directory) / "later.jsonl"
            append(first, usage(10, 100, 20, 60))
            later.write_bytes(b"")
            sampler = LiveTelemetrySampler()
            sampler.sample(first, now=self.BASE + 12)
            self.assertIn(str(first), sampler._states)
            sampler.sample(later, now=self.BASE + 400)
            self.assertNotIn(str(first), sampler._states)


if __name__ == "__main__":
    unittest.main()
