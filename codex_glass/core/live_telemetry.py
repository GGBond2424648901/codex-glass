"""Bounded, incremental, per-session usage telemetry from local Codex logs.

These are completed-call observations, not streaming token events. Non-usage
lines are filtered early; chat content is never retained.
"""

from __future__ import annotations

import json
import re
import threading
import time
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path


_COLD_TAIL_BYTES = 4 * 1024 * 1024
_MAX_RECORD_BYTES = 1024 * 1024
_RETENTION_SECONDS = 300
_RATE_WINDOW_SECONDS = 1
_USAGE_RECORD_HEAD = re.compile(rb'"type"\s*:\s*"(?:event_msg|token_count)"')


@dataclass
class _SessionState:
    offset: int = 0
    mtime_ns: int = 0
    partial: bytes = b""
    discarding: bool = False
    total: tuple[int, int, int] | None = None
    from_start: bool = True
    samples: deque[tuple[float, int, int, int]] = field(default_factory=deque)
    last_rate_bucket: int | None = None
    last_rate_output: int = 0
    last_usage_at: float | None = None
    last_access: float = 0.0


def _timestamp(value: object) -> float | None:
    if not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed.timestamp()
    except (ValueError, OverflowError):
        return None


def _counts(value: object) -> tuple[int, int, int] | None:
    if not isinstance(value, dict):
        return None
    try:
        numbers = tuple(int(value.get(key, 0)) for key in (
            "input_tokens", "cached_input_tokens", "output_tokens"
        ))
    except (ValueError, TypeError, OverflowError):
        return None
    if any(number < 0 for number in numbers):
        return None
    return numbers[0], min(numbers[0], numbers[1]), numbers[2]


class LiveTelemetrySampler:
    """Small per-file state; reads a bounded cold tail and only appended bytes later."""

    def __init__(self):
        self._states: dict[str, _SessionState] = {}
        self._lock = threading.Lock()

    def _expire_idle_locked(self, now: float) -> None:
        for key, state in list(self._states.items()):
            if now - state.last_access > _RETENTION_SECONDS:
                self._states.pop(key, None)

    def expire_idle(self, *, now: float | None = None) -> None:
        """Release inactive session buffers even when no conversation is running."""
        now = time.time() if now is None else now
        with self._lock:
            self._expire_idle_locked(now)

    def retain_paths(self, paths: list[Path]) -> None:
        """Keep token buffers only for the conversation currently being viewed."""
        wanted = {str(Path(path)) for path in paths}
        with self._lock:
            for key in list(self._states):
                if key not in wanted:
                    self._states.pop(key, None)

    def _refresh(self, path: Path, now: float) -> _SessionState:
        key = str(path)
        state = self._states.get(key)
        try:
            stat = path.stat()
            size = stat.st_size
        except OSError:
            self._states.pop(key, None)
            return _SessionState()
        if state is None or size < state.offset or (
            size == state.offset and state.mtime_ns != stat.st_mtime_ns
        ):
            state = _SessionState()
            self._states[key] = state
        elif size == state.offset:
            state.last_access = now
            self._prune(state, now)
            return state
        start = state.offset
        skipped = False
        if size - start > _COLD_TAIL_BYTES:
            start = size - _COLD_TAIL_BYTES
            skipped = True
            state.partial = b""
            state.discarding = False
            state.total = None
            state.from_start = False
        pending = state.partial
        discarding = state.discarding
        consumed = 0
        try:
            with path.open("rb") as stream:
                if skipped:
                    stream.seek(start - 1)
                    preceding = stream.read(1)
                else:
                    preceding = b"\n"
                stream.seek(start)
                remaining = size - start
                if preceding != b"\n":
                    discarding = True
                while remaining > 0:
                    limit = _MAX_RECORD_BYTES + 1 if discarding else _MAX_RECORD_BYTES + 1 - len(pending)
                    raw = stream.readline(min(limit, remaining))
                    if not raw:
                        break
                    consumed += len(raw)
                    remaining -= len(raw)
                    if discarding:
                        if raw.endswith(b"\n"):
                            discarding = False
                        continue
                    pending += raw
                    if pending.endswith(b"\n"):
                        if len(pending) - 1 <= _MAX_RECORD_BYTES:
                            self._accept_usage(pending[:-1], state, now)
                        pending = b""
                    elif len(pending) > _MAX_RECORD_BYTES:
                        pending = b""
                        discarding = True
        except OSError:
            return state
        state.offset = start + consumed
        state.mtime_ns = stat.st_mtime_ns
        state.partial = pending
        state.discarding = discarding
        state.last_access = now
        self._prune(state, now)
        if len(self._states) > 256:
            oldest = sorted(self._states, key=lambda item: self._states[item].last_access)
            for stale in oldest[:-256]:
                self._states.pop(stale, None)
        return state

    @staticmethod
    def _accept_usage(raw: bytes, state: _SessionState, now: float) -> None:
        if (
            len(raw) > _MAX_RECORD_BYTES
            or b'token_count' not in raw
            or _USAGE_RECORD_HEAD.search(raw[:192]) is None
        ):
            return
        try:
            record = json.loads(raw)
        except (UnicodeDecodeError, json.JSONDecodeError, MemoryError):
            return
        if not isinstance(record, dict):
            return
        payload = record.get("payload")
        if record.get("type") == "event_msg":
            if not isinstance(payload, dict) or payload.get("type") != "token_count":
                return
            info = payload.get("info")
        elif record.get("type") == "token_count":
            info = payload.get("info") if isinstance(payload, dict) else record.get("info")
        else:
            return
        if not isinstance(info, dict):
            return
        total = _counts(info.get("total_token_usage"))
        last = _counts(info.get("last_token_usage"))
        if total is None and last is None:
            return
        if total is not None and state.total == total:
            return
        if total is not None and state.total is not None:
            delta = tuple(max(0, current - previous) for current, previous in zip(total, state.total))
        elif last is not None:
            delta = last
        elif state.from_start:
            delta = total
        else:
            # The cold tail began midway through cumulative history.
            state.total = total
            return
        if total is not None:
            state.total = total
        timestamp = _timestamp(record.get("timestamp"))
        if timestamp is None or timestamp > now + 5 or timestamp < now - _RETENTION_SECONDS:
            return
        if delta is not None and any(delta):
            state.samples.append((timestamp, *delta))
            bucket = int(timestamp)
            if state.last_rate_bucket is None or bucket > state.last_rate_bucket:
                state.last_rate_bucket = bucket
                state.last_rate_output = delta[2]
            elif bucket == state.last_rate_bucket:
                state.last_rate_output += delta[2]
            state.last_usage_at = max(state.last_usage_at or timestamp, timestamp)

    @staticmethod
    def _prune(state: _SessionState, now: float) -> None:
        while state.samples and state.samples[0][0] < now - _RETENTION_SECONDS:
            state.samples.popleft()
        while len(state.samples) > 2048:
            state.samples.popleft()

    def summarize(self, paths: list[Path], *, now: float | None = None, active: bool = True) -> dict:
        """Aggregate only the explicitly related sessions passed by the caller."""
        now = time.time() if now is None else now
        output = 0
        latest_bucket = None
        rate_output = 0
        inputs = 0
        cached = 0
        last_at = None
        bins = [0] * 12
        with self._lock:
            for path in dict.fromkeys(paths):
                state = self._refresh(Path(path), now)
                if state.last_rate_bucket is not None:
                    if latest_bucket is None or state.last_rate_bucket > latest_bucket:
                        latest_bucket = state.last_rate_bucket
                        rate_output = state.last_rate_output
                    elif state.last_rate_bucket == latest_bucket:
                        rate_output += state.last_rate_output
                if state.last_usage_at is not None:
                    last_at = max(last_at or state.last_usage_at, state.last_usage_at)
                for timestamp, input_count, cache_count, count in state.samples:
                    last_at = timestamp if last_at is None else max(last_at, timestamp)
                    if now - 300 <= timestamp <= now + 5:
                        inputs += input_count
                        cached += cache_count
                    if now - 60 <= timestamp <= now + 5:
                        output += count
                        position = min(11, max(0, int((timestamp - (now - 60)) // 5)))
                        bins[position] += count
            self._expire_idle_locked(now)
        has_rate = active and latest_bucket is not None
        fresh = bool(has_rate and now < latest_bucket + 2)
        return {
            "status": "recent" if fresh else "last_record" if has_rate else "awaiting_usage" if active else "inactive",
            "output_tps": float(rate_output) if has_rate else None,
            "output_tokens_1s": rate_output if has_rate else 0,
            "rate_fresh": fresh,
            "rate_window_seconds": _RATE_WINDOW_SECONDS,
            "output_tokens_60s": output if active else 0,
            "cache_hit_percent": round(100 * cached / inputs, 1) if inputs else None,
            "cached_input_tokens_5m": cached,
            "input_tokens_5m": inputs,
            "last_usage_at": datetime.fromtimestamp(last_at, timezone.utc).isoformat() if last_at else None,
            "trend_5s_output": bins,
            "source": "local_completed_call_usage",
        }

    def sample(self, path: Path, *, now: float | None = None, active: bool = True) -> dict:
        return self.summarize([path], now=now, active=active)


DEFAULT_SAMPLER = LiveTelemetrySampler()
