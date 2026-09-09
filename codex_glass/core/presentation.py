"""Presentation-only data helpers: never infer live throughput from imported history."""

from collections import defaultdict, deque
from datetime import datetime
import sqlite3
import json


def compact(value):
    value = float(value or 0)
    for scale, suffix in ((1e9, "B"), (1e6, "M"), (1e3, "K")):
        if abs(value) >= scale:
            return f"{value/scale:,.2f}{suffix}"
    return f"{value:,.0f}"


class DeltaTracker:
    def __init__(self):
        self.previous = None
        self.stamp = None
        self.deltas = {}
        self.history = defaultdict(lambda: deque(maxlen=32))
        self.import_signature = None

    def update(self, data):
        stamp = data.get("generated_at")
        if stamp == self.stamp:
            return self.deltas
        current = {name: int(row.get("total_tokens", 0)) for name, row in data.get("by_model", {}).items()}
        source = data.get("source", {})
        signature = tuple(source.get(key, 0) for key in ("imported_sources", "imported_files", "imported_usage_events"))
        if self.import_signature is not None and signature != self.import_signature:
            self.previous = None
            self.history.clear()
        self.import_signature = signature
        self.deltas = {}
        if self.previous is not None:
            for name, total in current.items():
                delta = max(0, total - self.previous.get(name, 0))
                self.history[name].append(delta)
                if delta:
                    self.deltas[name] = delta
        self.previous = current
        self.stamp = stamp
        return self.deltas


def window_data(data, scope):
    if scope == "all":
        return data.get("total", {}), data.get("by_model", {})
    item = data.get("windows", {}).get(scope, {})
    return item.get("total", {}), {row["model"]: row for row in item.get("by_model", [])}


def quota_windows(data):
    """Only real account windows; never invent a 5-hour quota for a Pro account."""
    limits = (data.get("rate_limits") or {}).get("limits", [])
    primary = (data.get("rate_limits") or {}).get("primary")
    if not limits and primary:
        limits = [primary]
    found = {}
    for row in limits:
        if row.get("scope") != "global" or row.get("used_percent") is None:
            continue
        if row.get("resets_at"):
            try:
                reset = datetime.fromisoformat(row["resets_at"])
                if reset <= datetime.now(reset.tzinfo):
                    continue
            except (TypeError, ValueError):
                continue
        minutes = row.get("window_minutes")
        if str((data.get("rate_limits") or {}).get("plan_type", "")).lower() == "pro" and minutes != 10080:
            continue
        if minutes in (300, 10080):
            found[minutes] = row
    return [
        ("周额度" if minutes == 10080 else "5 小时额度", found[minutes]) for minutes in (10080, 300) if minutes in found
    ]


def desktop_snapshot(coordinator):
    data = coordinator.snapshot()
    result = {
        key: data[key]
        for key in ("total", "by_model", "windows", "charts", "pricing", "source", "generated_at", "index")
        if key in data
    }
    result["rate_limits"] = {"limits": []}
    index = getattr(coordinator, "index", None)
    if index is not None:
        try:
            with index.read_transaction() as reader:
                row = reader.execute(
                    """SELECT q.payload_json FROM quota_cache q
                    JOIN session_files f ON q.file_id=f.file_id WHERE f.missing=0
                    ORDER BY q.observed_at DESC,q.source_offset DESC LIMIT 1"""
                ).fetchone()
                if row:
                    result["rate_limits"] = json.loads(row["payload_json"])
                else:
                    row = reader.execute(
                        """SELECT r.observed_at,r.used_percent,r.window_minutes,r.resets_at
                        FROM rate_limit_snapshots r JOIN session_files f ON r.file_id=f.file_id
                        WHERE f.missing=0 AND (r.limit_id='codex' OR (r.limit_id IS NULL AND r.limit_name IS NULL))
                        ORDER BY r.observed_at DESC,r.source_offset DESC LIMIT 1"""
                    ).fetchone()
                    if row:
                        primary = {
                            key: row[key] for key in ("observed_at", "used_percent", "window_minutes", "resets_at")
                        }
                        result["rate_limits"]["limits"] = [dict(primary, scope="global", limit_id="codex")]
        except (sqlite3.Error, RuntimeError):
            pass
    else:
        result["rate_limits"] = data.get("rate_limits")
    return result
