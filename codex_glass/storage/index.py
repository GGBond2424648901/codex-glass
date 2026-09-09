"""Versioned SQLite persistence for incrementally indexed Codex sessions."""

from __future__ import annotations

import copy
import hashlib
import json
import math
import os
import re
import sqlite3
import threading
import time
import uuid
from contextlib import contextmanager
from dataclasses import asdict, dataclass, field, replace
from datetime import datetime
from pathlib import Path
from typing import Any, BinaryIO, Callable, Dict, Iterable, Iterator, List, Optional, TypeVar

from codex_glass.core.usage import (
    MonitorConfig,
    PRICING_POLICY_CHECKED_ON,
    PRICING_POLICY_VERSION,
    RateLimitSnapshot,
    UsageDelta,
    UsageEvent,
    aggregate_usage_events,
    estimate_cost_usd,
    default_codex_home,
    iter_session_files,
    parse_timestamp_local,
    _rate_limit_scope,
    _should_replace_rate_limit,
)


SUMMARY_FORMAT_VERSION = "native-workspace-windows-v4"


SCHEMA_VERSION = 1
_LOCK_RETRY_DELAYS = (0.05, 0.1, 0.2)
_T = TypeVar("_T")
_RELEVANT_MARKERS = (b'"type":"session_meta"', b'"type":"turn_context"', b'"type":"token_count"')
_MAX_RELEVANT_LINE = 8 * 1024 * 1024
_LEASE_TTL_SECONDS = 30


@dataclass(frozen=True)
class RelevantLine:
    source_offset: int
    next_offset: int
    data: bytes


class _RelevantLineReader(Iterator[RelevantLine]):
    """Expose completed byte progress even when every line is irrelevant."""

    def __init__(
        self,
        stream: BinaryIO,
        start_offset: int,
        probe_limit: int,
        chunk_size: int,
        checkpoint: Optional[Callable[[], None]] = None,
    ):
        if start_offset < 0 or probe_limit <= 0 or chunk_size <= 0:
            raise ValueError("offset must be nonnegative and buffer sizes must be positive")
        self.completed_offset = start_offset
        self.skipped_oversized = 0
        self._checkpoint = checkpoint
        self._lines = self._read(stream, start_offset, min(probe_limit, _MAX_RELEVANT_LINE), chunk_size)

    def __iter__(self) -> Iterator[RelevantLine]:
        return self

    def __next__(self) -> RelevantLine:
        return next(self._lines)

    def _read(self, stream: BinaryIO, start: int, probe_limit: int, chunk_size: int) -> Iterator[RelevantLine]:
        stream.seek(start)
        position = start
        line_start = start
        buffer = bytearray()
        relevant = None
        oversized = False
        while True:
            chunk = stream.read(chunk_size)
            if not chunk:
                return
            cursor = 0
            while cursor < len(chunk):
                newline = chunk.find(b"\n", cursor)
                end = newline + 1 if newline >= 0 else len(chunk)
                segment = memoryview(chunk)[cursor:end]
                consumed = 0
                if relevant is None:
                    consumed = min(len(segment), probe_limit - len(buffer))
                    buffer.extend(segment[:consumed])
                    if len(buffer) == probe_limit or newline >= 0:
                        relevant = any(marker in buffer for marker in _RELEVANT_MARKERS)
                        if not relevant:
                            buffer.clear()
                if relevant:
                    if len(buffer) + len(segment) - consumed > _MAX_RELEVANT_LINE:
                        buffer.clear()
                        relevant = False
                        oversized = True
                    else:
                        buffer.extend(segment[consumed:])
                position += len(segment)
                cursor = end
                if newline >= 0:
                    self.completed_offset = position
                    if oversized:
                        self.skipped_oversized += 1
                    if relevant:
                        yield RelevantLine(line_start, position, bytes(buffer))
                    line_start = position
                    buffer.clear()
                    relevant = None
                    oversized = False
            if self._checkpoint is not None:
                self._checkpoint()


def iter_relevant_lines(
    stream: BinaryIO,
    start_offset: int,
    probe_limit: int = 65536,
    chunk_size: int = 1048576,
    checkpoint: Optional[Callable[[], None]] = None,
) -> _RelevantLineReader:
    """Stream complete compact-JSON records; never retain an irrelevant line.

    The iterator's completed_offset is the safe resume cursor, including skipped
    lines. skipped_oversized counts complete relevant records beyond 8 MiB.
    """
    return _RelevantLineReader(stream, start_offset, probe_limit, chunk_size, checkpoint)


class IncompatibleIndexError(RuntimeError):
    """Raised when an index uses a schema this application cannot read."""


class LostLeaseError(RuntimeError):
    """A stale indexer must not modify the new owner's committed state."""


@dataclass(frozen=True)
class FileState:
    file_id: int
    path: Path
    size: int
    mtime_ns: int
    head_hash: str
    offset: int
    last_model: Optional[str]
    last_cwd: Optional[str]
    previous_input_tokens: int
    previous_cached_input_tokens: int
    previous_output_tokens: int
    previous_reasoning_output_tokens: int
    previous_total_tokens: int
    missing: bool
    error: Optional[str]
    created_at: float
    updated_at: float
    last_scanned_at: Optional[float]
    file_identity: str = ""
    read_failed: bool = False


@dataclass(frozen=True)
class IndexStatus:
    status: str
    complete: bool
    total_files: int
    processed_files: int
    total_bytes: int
    processed_bytes: int
    current_file: Optional[str]
    last_error: Optional[str]
    started_at: Optional[float]
    completed_at: Optional[float]
    updated_at: Optional[float]
    changed_files: int = 0
    failed_files: int = 0

    @property
    def progress_percent(self) -> float:
        if self.total_bytes <= 0:
            return 100.0 if self.complete else 0.0
        return min(100.0, 100.0 * self.processed_bytes / self.total_bytes)

    def to_payload(self) -> Dict[str, Any]:
        payload = asdict(self)
        payload["progress_percent"] = self.progress_percent
        payload["current_file"] = f"会话文件 {self.processed_files + 1}" if self.current_file else None
        if self.last_error:
            payload["last_error"] = _sanitized_error(self.last_error)
        return payload


def _sanitized_error(error: object) -> str:
    message = str(error)
    if isinstance(error, OSError):
        for filename in (error.filename, error.filename2):
            if filename is not None:
                message = message.replace(repr(filename), "[本地路径]").replace(str(filename), "[本地路径]")
    # Quotes, spaces, and even newlines can belong to a filename. When only an
    # unstructured message remains, retain the diagnostic prefix and redact the
    # entire suffix rather than guessing where the private path ends.
    return re.sub(r"['\"]?(?:[A-Za-z]:[\\/]|\\\\|(?<!\w)/)[\s\S]*", "[本地路径]", message)


def summary_cache_key(config: MonitorConfig, cwd_filter: Optional[str], include_events: bool) -> str:
    """Hash only configuration inputs that affect the public summary."""
    payload = {
        "pricing": {model: asdict(rates) for model, rates in config.pricing_per_million.items()},
        "aliases": config.model_aliases,
        "pricing_policy": PRICING_POLICY_VERSION,
        "pricing_policy_checked_on": PRICING_POLICY_CHECKED_ON,
        "summary_format": SUMMARY_FORMAT_VERSION,
        "cwd_filter": cwd_filter,
        "include_events": include_events,
    }
    return hashlib.sha256(
        json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode()
    ).hexdigest()


def default_index_path() -> Path:
    """Return the stable per-user location for the session index."""
    return default_codex_home() / "codex-monitor.sqlite3"


class SessionIndex:
    """A small, versioned SQLite store shared by the indexer and dashboard."""

    def __init__(self, path: Path) -> None:
        self.path = Path(path)
        self._connection: Optional[sqlite3.Connection] = None
        self._write_lock = threading.RLock()
        self._lease_owner: Optional[str] = None

    @property
    def connection(self) -> sqlite3.Connection:
        if self._connection is None:
            raise RuntimeError("SessionIndex.initialize() must be called before using the connection")
        return self._connection

    def initialize(self) -> None:
        """Open the database, validate its version, and create version one if new."""
        with self._write_lock:
            if self._connection is not None:
                return

            self.path.parent.mkdir(parents=True, exist_ok=True)
            connection = sqlite3.connect(self.path, timeout=5.0, check_same_thread=False)
            connection.row_factory = sqlite3.Row
            self._connection = connection
            try:
                connection.execute("PRAGMA foreign_keys = ON")

                version = int(connection.execute("PRAGMA user_version").fetchone()[0])
                if version not in (0, SCHEMA_VERSION):
                    raise IncompatibleIndexError(
                        f"Index schema version {version} is incompatible with supported version {SCHEMA_VERSION}; rebuild required"
                    )

                self._retry_locked(lambda: connection.execute("PRAGMA journal_mode = WAL").fetchone())
                if version == 0:
                    self._run_write(self._create_schema)
                self._validate_schema(connection)
                self._run_write(self._ensure_summary_schema)
            except BaseException:
                self._connection = None
                self._lease_owner = None
                connection.close()
                raise

    def close(self) -> None:
        with self._write_lock:
            if self._connection is not None:
                self._connection.close()
                self._connection = None

    def schema_version(self) -> int:
        return int(self.connection.execute("PRAGMA user_version").fetchone()[0])

    def rebuild(self) -> None:
        """Clear scanner-owned data while retaining imported external history."""

        def clear(connection: sqlite3.Connection) -> None:
            connection.execute("DELETE FROM summary_cache")
            connection.execute("DELETE FROM session_files")
            connection.execute(
                """
                UPDATE index_state
                   SET status = 'starting', complete = 0, generation = generation + 1,
                       failed_files = 0,
                       total_files = 0, processed_files = 0,
                       total_bytes = 0, processed_bytes = 0,
                       current_file = NULL, last_error = NULL,
                       started_at = NULL, completed_at = NULL, updated_at = ?
                 WHERE singleton_id = 1
                """,
                (time.time(),),
            )

        with self.indexing_lease() as owner:
            with self.write_transaction(expected_owner=owner) as writer:
                clear(writer)

    def bind_sessions_root(self, sessions_dir: Path, expected_owner: Optional[str] = None) -> None:
        """A database holds one canonical root, including its retained missing files."""
        root = os.path.normcase(self._canonical_path(sessions_dir))
        with self.write_transaction(expected_owner=expected_owner) as writer:
            bound = writer.execute("SELECT sessions_root FROM index_state WHERE singleton_id=1").fetchone()[0]
            if bound == root:
                return
            if bound is not None:
                raise IncompatibleIndexError("Index sessions root mismatch; choose a separate --index-db for this root")
            # Compatible older indexes had no root field. Adopt them only when
            # every retained source is contained in the requested root.
            for row in writer.execute("SELECT path FROM session_files"):
                if not Path(row[0]).is_relative_to(Path(root)):
                    raise IncompatibleIndexError("Index contains another sessions root; choose a separate --index-db")
            writer.execute("UPDATE index_state SET sessions_root=? WHERE singleton_id=1", (root,))

    @contextmanager
    def indexing_lease(self, owner: Optional[str] = None) -> Iterator[str]:
        """Borrow an explicitly held lease, or own one for a direct synchronous scan."""
        borrowed = owner or self._lease_owner
        actual = borrowed or f"scan:{os.getpid()}:{uuid.uuid4().hex}"
        if not borrowed and not self.acquire_lease(actual, _LEASE_TTL_SECONDS):
            raise LostLeaseError("Another process holds the indexing lease")
        try:
            yield actual
        finally:
            if not borrowed:
                self.release_lease(actual)

    def get_file_state(self, path: Path) -> Optional[FileState]:
        row = self.connection.execute(
            "SELECT * FROM session_files WHERE path = ?", (self._canonical_path(path),)
        ).fetchone()
        return self._file_state_from_row(row) if row is not None else None

    def load_events(self) -> List[sqlite3.Row]:
        return list(
            self.connection.execute(
                "SELECT * FROM usage_events ORDER BY occurred_at, file_id, source_offset"
            ).fetchall()
        )

    def load_rate_limits(self) -> List[sqlite3.Row]:
        return list(
            self.connection.execute(
                "SELECT * FROM rate_limit_snapshots ORDER BY observed_at, file_id, source_offset"
            ).fetchall()
        )

    def read_state(self) -> IndexStatus:
        with self.read_transaction() as reader:
            row = reader.execute("SELECT * FROM index_state WHERE singleton_id = 1").fetchone()
        return self._status_from_row(row)

    @staticmethod
    def _status_from_row(row: sqlite3.Row) -> IndexStatus:
        if row is None:
            raise RuntimeError("SessionIndex has not been initialized")
        required = {
            "status",
            "complete",
            "total_files",
            "processed_files",
            "total_bytes",
            "processed_bytes",
            "current_file",
            "last_error",
            "started_at",
            "completed_at",
            "updated_at",
        }
        if not required <= set(row.keys()):
            raise IncompatibleIndexError("Index state schema is incomplete; rebuild required (see README recovery)")
        return IndexStatus(
            status=str(row["status"]),
            complete=bool(row["complete"]),
            total_files=int(row["total_files"]),
            processed_files=int(row["processed_files"]),
            total_bytes=int(row["total_bytes"]),
            processed_bytes=int(row["processed_bytes"]),
            current_file=row["current_file"],
            last_error=row["last_error"],
            started_at=row["started_at"],
            completed_at=row["completed_at"],
            updated_at=row["updated_at"],
            failed_files=int(row["failed_files"]) if "failed_files" in row.keys() else 0,
        )

    def cached_summary(
        self, config: MonitorConfig, cwd_filter: Optional[str] = None, include_events: bool = False
    ) -> Optional[Dict[str, Any]]:
        """Load the last published result without reading the event corpus."""
        with self.read_transaction() as reader:
            row = reader.execute(
                "SELECT payload_json FROM summary_cache WHERE config_key=? ORDER BY created_at DESC LIMIT 1",
                (summary_cache_key(config, cwd_filter, include_events),),
            ).fetchone()
        return json.loads(row["payload_json"]) if row else None

    def build_summary(
        self,
        config: MonitorConfig,
        now: Optional[datetime] = None,
        include_events: bool = False,
        cwd_filter: Optional[str] = None,
        compact_events: bool = False,
        summary_ready=None,
        progress=None,
    ) -> Dict[str, Any]:
        """Aggregate one committed generation using current prices and clock."""
        current_time = now or datetime.now()
        if progress is not None:
            progress("reading", 0, 0)
        config_key = summary_cache_key(config, cwd_filter, include_events)
        with self.read_transaction() as reader:
            state_row = reader.execute("SELECT * FROM index_state WHERE singleton_id=1").fetchone()
            status = self._status_from_row(state_row)
            generation = int(state_row["generation"])
            key = hashlib.sha256(f"{config_key}:{generation}".encode()).hexdigest()
            cached = reader.execute(
                "SELECT payload_json, as_of FROM summary_cache WHERE cache_key=?", (key,)
            ).fetchone()
            if not compact_events and cached and cached["as_of"] == current_time.isoformat():
                result = json.loads(cached["payload_json"])
                result["index"] = status.to_payload()
                result["index"]["generation"] = generation
                return result
            # pathlib ordering is platform-specific (case-insensitive on
            # Windows). Use the same ordering as legacy iter_session_files for
            # equal timestamps and limit ties, including after later discovery.
            paths = {
                row[0]
                for row in reader.execute(
                    "SELECT path FROM session_files UNION SELECT source_path FROM imported_session_files"
                )
            }
            path_objects = {path: Path(path) for path in paths}
            ranks = {path: position for position, path in enumerate(sorted(path_objects.values()))}
            path_order = {path: ranks[obj] for path, obj in path_objects.items()}
            interned = {}

            def shared(value):
                if value not in interned:
                    interned[value] = value
                return interned[value]

            session_keys = {}

            def stable_session_for(row):
                key = (row["session_path"], row["session_head_hash"])
                if key not in session_keys:
                    session_keys[key] = session_key(*key)
                return session_keys[key]

            reader.create_collation(
                "session_path",
                lambda left, right: (path_order[left] > path_order[right]) - (path_order[left] < path_order[right]),
            )
            from codex_glass.storage.history_import import rate_limit_key, session_key, usage_event_key

            event_rows = []
            local_event_keys = set()
            has_imported_events = reader.execute("SELECT 1 FROM imported_usage_events LIMIT 1").fetchone() is not None
            has_imported_rates = (
                reader.execute("SELECT 1 FROM imported_rate_limit_snapshots LIMIT 1").fetchone() is not None
            )
            for row in reader.execute(
                """SELECT e.*, f.path AS session_path, f.head_hash AS session_head_hash
                     FROM usage_events e JOIN session_files f ON e.file_id=f.file_id
                   ORDER BY e.occurred_at, f.path COLLATE session_path, e.source_offset"""
            ):
                if progress is not None and len(event_rows) % 4096 == 0:
                    progress("reading", len(event_rows), 0)
                delta = UsageDelta(
                    *(
                        int(row[name])
                        for name in (
                            "input_tokens",
                            "cached_input_tokens",
                            "output_tokens",
                            "reasoning_output_tokens",
                            "total_tokens",
                        )
                    )
                )
                if has_imported_events:
                    stable_session = stable_session_for(row)
                    stable_event = usage_event_key(
                        stable_session,
                        row["source_offset"],
                        row["occurred_at"],
                        row["model"],
                        row["cwd"],
                        row["input_tokens"],
                        row["cached_input_tokens"],
                        row["output_tokens"],
                        row["reasoning_output_tokens"],
                        row["total_tokens"],
                    )
                    local_event_keys.add(stable_event)
                event_rows.append(
                    (
                        datetime.fromisoformat(row["occurred_at"]),
                        0,
                        shared(row["session_path"]),
                        int(row["source_offset"]),
                        UsageEvent(
                            datetime.fromisoformat(row["occurred_at"]),
                            shared(row["model"] or "unknown"),
                            shared(row["cwd"]),
                            delta,
                            0.0,
                            shared(row["pricing_source"] or "default"),
                        ),
                    )
                )
            imported_event_keys = set()
            for row in reader.execute(
                """SELECT e.*, f.source_path AS session_path
                     FROM imported_usage_events e
                     JOIN imported_session_files f
                       ON f.source_id=e.source_id AND f.source_file_id=e.source_file_id
                    ORDER BY e.occurred_at, f.source_path, e.source_offset, e.source_id"""
            ):
                if progress is not None and len(event_rows) % 4096 == 0:
                    progress("reading", len(event_rows), 0)
                if row["event_key"] in local_event_keys or row["event_key"] in imported_event_keys:
                    continue
                imported_event_keys.add(row["event_key"])
                delta = UsageDelta(
                    *(
                        int(row[name])
                        for name in (
                            "input_tokens",
                            "cached_input_tokens",
                            "output_tokens",
                            "reasoning_output_tokens",
                            "total_tokens",
                        )
                    )
                )
                occurred = datetime.fromisoformat(row["occurred_at"])
                event_rows.append(
                    (
                        occurred,
                        1,
                        shared(row["session_path"]),
                        int(row["source_offset"]),
                        UsageEvent(
                            occurred,
                            shared(row["model"] or "unknown"),
                            shared(row["cwd"]),
                            delta,
                            0.0,
                            shared(row["pricing_source"] or "default"),
                        ),
                    )
                )
            event_rows.sort(key=lambda item: (item[0], path_order[item[2]], item[1], item[3]))
            events = [item[4] for item in event_rows]
            del event_rows, local_event_keys, imported_event_keys

            by_session: Dict[str, Dict[str, RateLimitSnapshot]] = {}
            if progress is not None:
                progress("quotas", 0, 0)
            local_rate_keys = set()
            for row in reader.execute(
                """SELECT r.*, f.path AS session_path, f.head_hash AS session_head_hash
                     FROM rate_limit_snapshots r JOIN session_files f ON r.file_id=f.file_id
                   ORDER BY f.path COLLATE session_path, r.source_offset"""
            ):
                stable_session = stable_session_for(row)
                if has_imported_rates:
                    stable_rate = rate_limit_key(
                        stable_session,
                        row["source_offset"],
                        row["limit_id"],
                        row["limit_name"],
                        row["observed_at"],
                        row["used_percent"],
                        row["window_minutes"],
                        row["resets_at"],
                        row["resets_in_seconds"],
                    )
                    local_rate_keys.add(stable_rate)
                snapshot = RateLimitSnapshot(
                    limit_id=row["limit_id"],
                    limit_name=row["limit_name"],
                    observed_at=datetime.fromisoformat(row["observed_at"]),
                    used_percent=row["used_percent"],
                    window_minutes=row["window_minutes"],
                    resets_at=datetime.fromisoformat(row["resets_at"]) if row["resets_at"] else None,
                    resets_in_seconds=row["resets_in_seconds"],
                )
                snapshots = by_session.setdefault(stable_session, {})
                scope = snapshot.limit_id or "global"
                if _should_replace_rate_limit(snapshots.get(scope), snapshot):
                    snapshots[scope] = snapshot
            imported_rate_keys = set()
            for row in reader.execute(
                """SELECT r.*, f.session_key
                     FROM imported_rate_limit_snapshots r
                     JOIN imported_session_files f
                       ON f.source_id=r.source_id AND f.source_file_id=r.source_file_id
                    ORDER BY f.source_path, r.source_offset, r.source_id"""
            ):
                if row["snapshot_key"] in local_rate_keys or row["snapshot_key"] in imported_rate_keys:
                    continue
                imported_rate_keys.add(row["snapshot_key"])
                snapshot = RateLimitSnapshot(
                    limit_id=row["limit_id"],
                    limit_name=row["limit_name"],
                    observed_at=datetime.fromisoformat(row["observed_at"]),
                    used_percent=row["used_percent"],
                    window_minutes=row["window_minutes"],
                    resets_at=datetime.fromisoformat(row["resets_at"]) if row["resets_at"] else None,
                    resets_in_seconds=row["resets_in_seconds"],
                )
                snapshots = by_session.setdefault(row["session_key"], {})
                scope = snapshot.limit_id or "global"
                if _should_replace_rate_limit(snapshots.get(scope), snapshot):
                    snapshots[scope] = snapshot
            # The legacy parser contributes one selected snapshot per source file.
            selected = [
                (
                    snapshots["codex"]
                    if "codex" in snapshots
                    else max(
                        snapshots.values(),
                        key=lambda item: (
                            1 if _rate_limit_scope(item) == "global" else 0,
                            item.used_percent or 0.0,
                            item.observed_at or datetime.min,
                        ),
                    )
                )
                for snapshots in by_session.values()
            ]
            local_files = reader.execute("SELECT COUNT(*) FROM session_files").fetchone()[0]
            import_counts = reader.execute(
                """SELECT
                       (SELECT COUNT(*) FROM history_imports),
                       (SELECT COUNT(*) FROM imported_session_files),
                       (SELECT COUNT(DISTINCT event_key) FROM imported_usage_events),
                       (SELECT COUNT(DISTINCT snapshot_key) FROM imported_rate_limit_snapshots)"""
            ).fetchone()
        del local_rate_keys, imported_rate_keys, by_session
        from codex_glass.storage.compact import CompactHistory

        history = CompactHistory() if compact_events else None

        def decorate(result):
            if import_counts[0]:
                result["source"].update(
                    {
                        "imported_sources": int(import_counts[0]),
                        "imported_files": int(import_counts[1]),
                        "imported_usage_events": int(import_counts[2]),
                        "imported_rate_limit_snapshots": int(import_counts[3]),
                    }
                )
            result["index"] = status.to_payload()
            result["index"]["generation"] = generation
            return result

        result = aggregate_usage_events(
            events,
            selected,
            config,
            now=current_time,
            include_events=include_events,
            event_sink=history.append if history is not None else None,
            summary_sink=(lambda result: summary_ready(decorate(result))) if summary_ready is not None else None,
            progress=progress,
            source_metadata={"files": local_files},
            cwd_filter=cwd_filter,
        )
        del events
        if import_counts[0]:
            result["source"].update(
                {
                    "imported_sources": int(import_counts[0]),
                    "imported_files": int(import_counts[1]),
                    "imported_usage_events": int(import_counts[2]),
                    "imported_rate_limit_snapshots": int(import_counts[3]),
                }
            )
        result["index"] = status.to_payload()
        result["index"]["generation"] = generation
        payload = json.dumps(result, ensure_ascii=True, separators=(",", ":"))
        if history is not None:
            result["_history"] = history
        with self.write_transaction() as writer:
            if writer.execute("SELECT generation FROM index_state WHERE singleton_id=1").fetchone()[0] != generation:
                return result
            # Keep one prior result available through indexing, then replace it atomically.
            writer.execute("DELETE FROM summary_cache WHERE config_key=?", (config_key,))
            writer.execute(
                """INSERT INTO summary_cache(cache_key, payload_json, created_at, config_key, as_of)
                   VALUES (?, ?, ?, ?, ?)""",
                (key, payload, time.time(), config_key, current_time.isoformat()),
            )
        return result

    def acquire_lease(self, owner: str, ttl_seconds: int) -> bool:
        """Atomically acquire the cross-process indexing lease when it is available."""

        def acquire(connection: sqlite3.Connection) -> bool:
            now = time.time()
            expires_at = now + max(0, int(ttl_seconds))
            result = connection.execute(
                """
                UPDATE index_state
                   SET lease_owner = ?, lease_expires_at = ?
                 WHERE singleton_id = 1
                   AND (
                        lease_owner IS NULL
                        OR lease_expires_at IS NULL
                        OR lease_expires_at <= ?
                        OR lease_owner = ?
                   )
                """,
                (owner, expires_at, now, owner),
            )
            return result.rowcount == 1

        acquired = self._run_write(acquire)
        if acquired:
            self._lease_owner = owner
        return acquired

    def renew_lease(self, owner: str, ttl_seconds: int) -> bool:
        def renew(connection: sqlite3.Connection) -> bool:
            now = time.time()
            expires_at = now + max(0, int(ttl_seconds))
            result = connection.execute(
                """
                UPDATE index_state
                   SET lease_expires_at = ?
                 WHERE singleton_id = 1
                   AND lease_owner = ?
                   AND lease_expires_at > ?
                """,
                (expires_at, owner, now),
            )
            return result.rowcount == 1

        return self._run_write(renew)

    def release_lease(self, owner: str) -> bool:
        def release(connection: sqlite3.Connection) -> bool:
            result = connection.execute(
                """
                UPDATE index_state
                   SET lease_owner = NULL, lease_expires_at = NULL
                 WHERE singleton_id = 1 AND lease_owner = ?
                """,
                (owner,),
            )
            return result.rowcount == 1

        released = self._run_write(release)
        if self._lease_owner == owner:
            self._lease_owner = None
        return released

    @contextmanager
    def read_transaction(self) -> Iterator[sqlite3.Connection]:
        """Provide a consistent WAL read on an isolated connection."""
        self.connection  # Ensure initialize() completed before opening the reader.
        connection = sqlite3.connect(self.path, timeout=5.0, check_same_thread=False)
        connection.row_factory = sqlite3.Row
        try:
            connection.execute("PRAGMA foreign_keys = ON")
            connection.execute("PRAGMA query_only = ON")
            connection.execute("BEGIN")
            yield connection
            connection.commit()
        except BaseException:
            self._rollback_if_needed(connection)
            raise
        finally:
            connection.close()

    @contextmanager
    def write_transaction(self, expected_owner: Optional[str] = None) -> Iterator[sqlite3.Connection]:
        """Provide a manually managed immediate write transaction."""
        with self._write_lock:
            connection = self.connection
            self._begin_immediate_with_retry(connection)
            try:
                if expected_owner is not None:
                    self._validate_lease(connection, expected_owner)
                yield connection
                if expected_owner is not None:
                    self._validate_lease(connection, expected_owner)
                connection.commit()
            except BaseException:
                connection.rollback()
                raise

    @staticmethod
    def _validate_lease(connection: sqlite3.Connection, owner: str) -> None:
        row = connection.execute(
            "SELECT lease_owner, lease_expires_at FROM index_state WHERE singleton_id=1"
        ).fetchone()
        if row is None or row[0] != owner or row[1] is None or row[1] <= time.time():
            raise LostLeaseError("Indexing lease expired or was acquired by another process; batch rolled back")

    def _run_write(self, operation: Callable[[sqlite3.Connection], _T]) -> _T:
        with self._write_lock:
            connection = self.connection
            for attempt, delay in enumerate((*_LOCK_RETRY_DELAYS, None)):
                try:
                    connection.execute("BEGIN IMMEDIATE")
                    result = operation(connection)
                    connection.commit()
                    return result
                except sqlite3.OperationalError as error:
                    self._rollback_if_needed(connection)
                    if not self._is_locked(error) or delay is None:
                        raise
                    time.sleep(delay)
                except BaseException:
                    self._rollback_if_needed(connection)
                    raise
        raise AssertionError("unreachable")

    def _begin_immediate_with_retry(self, connection: sqlite3.Connection) -> None:
        for delay in (*_LOCK_RETRY_DELAYS, None):
            try:
                connection.execute("BEGIN IMMEDIATE")
                return
            except sqlite3.OperationalError as error:
                self._rollback_if_needed(connection)
                if not self._is_locked(error) or delay is None:
                    raise
                time.sleep(delay)
        raise AssertionError("unreachable")

    @staticmethod
    def _is_locked(error: sqlite3.OperationalError) -> bool:
        message = str(error).lower()
        return "database is locked" in message or "database is busy" in message

    @staticmethod
    def _rollback_if_needed(connection: sqlite3.Connection) -> None:
        if connection.in_transaction:
            connection.rollback()

    def _retry_locked(self, operation: Callable[[], _T]) -> _T:
        for delay in (*_LOCK_RETRY_DELAYS, None):
            try:
                return operation()
            except sqlite3.OperationalError as error:
                if not self._is_locked(error) or delay is None:
                    raise
                time.sleep(delay)
        raise AssertionError("unreachable")

    def _create_schema(self, connection: sqlite3.Connection) -> None:
        for statement in _SCHEMA_STATEMENTS:
            connection.execute(statement)
        connection.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")
        connection.execute(
            """
            INSERT OR IGNORE INTO index_state (
                singleton_id, status, complete, total_files, processed_files,
                total_bytes, processed_bytes, updated_at
            ) VALUES (1, 'starting', 0, 0, 0, 0, 0, ?)
            """,
            (time.time(),),
        )

    @staticmethod
    def _validate_schema(connection: sqlite3.Connection) -> None:
        # Compare with the base v1 schema before applying compatible additions;
        # a hand-crafted version number does not establish schema compatibility.
        reference = sqlite3.connect(":memory:")
        try:
            for statement in _SCHEMA_STATEMENTS:
                reference.execute(statement)
            for table in ("session_files", "usage_events", "rate_limit_snapshots", "index_state", "summary_cache"):
                required = {row[1] for row in reference.execute(f"PRAGMA table_info({table})")}
                present = {row[1] for row in connection.execute(f"PRAGMA table_info({table})")}
                if not required <= present:
                    raise IncompatibleIndexError(
                        f"Index table {table} is incomplete; rebuild required (see README recovery)"
                    )
            if connection.execute("SELECT 1 FROM index_state WHERE singleton_id=1").fetchone() is None:
                raise IncompatibleIndexError("Index state row is missing; rebuild required (see README recovery)")
        finally:
            reference.close()

    @staticmethod
    def _ensure_summary_schema(connection: sqlite3.Connection) -> None:
        """Add compatible cache metadata to indexes created before summaries existed."""
        from codex_glass.storage.history_import import ensure_import_schema

        extensions = {
            "index_state": {
                "generation": "INTEGER NOT NULL DEFAULT 0",
                "sessions_root": "TEXT",
                "failed_files": "INTEGER NOT NULL DEFAULT 0",
            },
            "session_files": {"file_identity": "TEXT NOT NULL DEFAULT ''", "read_failed": "INTEGER NOT NULL DEFAULT 0"},
            "summary_cache": {"config_key": "TEXT NOT NULL DEFAULT ''", "as_of": "TEXT NOT NULL DEFAULT ''"},
        }
        for table, fields in extensions.items():
            present = {row[1] for row in connection.execute(f"PRAGMA table_info({table})")}
            for name, definition in fields.items():
                if name not in present:
                    connection.execute(f"ALTER TABLE {table} ADD COLUMN {name} {definition}")
        connection.execute(
            "CREATE INDEX IF NOT EXISTS summary_cache_config_idx ON summary_cache(config_key, created_at)"
        )
        connection.execute(
            """CREATE TABLE IF NOT EXISTS quota_cache (
                   file_id INTEGER PRIMARY KEY REFERENCES session_files(file_id) ON DELETE CASCADE,
                   source_offset INTEGER NOT NULL,
                   observed_at TEXT NOT NULL,
                   payload_json TEXT NOT NULL,
                   created_at REAL NOT NULL
               )"""
        )
        connection.execute(
            "CREATE INDEX IF NOT EXISTS quota_cache_observed_idx ON quota_cache(observed_at, source_offset)"
        )
        ensure_import_schema(connection)
        for table in ("usage_events", "rate_limit_snapshots", "session_files"):
            for operation in ("INSERT", "DELETE") if table == "session_files" else ("INSERT", "UPDATE", "DELETE"):
                connection.execute(
                    f"""CREATE TRIGGER IF NOT EXISTS {table}_{operation.lower()}_generation
                        AFTER {operation} ON {table} BEGIN
                        UPDATE index_state SET generation=generation+1 WHERE singleton_id=1; END"""
                )

    @staticmethod
    def _canonical_path(path: Path) -> str:
        return str(Path(path).resolve(strict=False))

    @staticmethod
    def _file_state_from_row(row: sqlite3.Row) -> FileState:
        return FileState(
            file_id=int(row["file_id"]),
            path=Path(row["path"]),
            size=int(row["size"]),
            mtime_ns=int(row["mtime_ns"]),
            head_hash=str(row["head_hash"]),
            offset=int(row["offset"]),
            last_model=row["last_model"],
            last_cwd=row["last_cwd"],
            previous_input_tokens=int(row["previous_input_tokens"]),
            previous_cached_input_tokens=int(row["previous_cached_input_tokens"]),
            previous_output_tokens=int(row["previous_output_tokens"]),
            previous_reasoning_output_tokens=int(row["previous_reasoning_output_tokens"]),
            previous_total_tokens=int(row["previous_total_tokens"]),
            missing=bool(row["missing"]),
            error=row["error"],
            created_at=float(row["created_at"]),
            updated_at=float(row["updated_at"]),
            last_scanned_at=row["last_scanned_at"],
            file_identity=row["file_identity"],
            read_failed=bool(row["read_failed"]),
        )


@dataclass
class _ParserState:
    model: str = "unknown"
    cwd: Optional[str] = None
    previous: UsageDelta = field(default_factory=UsageDelta)


def _validate_sqlite_values(values: Iterable[object]) -> None:
    """Reject record values SQLite cannot preserve before adding them to a batch."""
    for value in values:
        if isinstance(value, str):
            value.encode("utf-8")
        elif isinstance(value, int):
            if not -(2**63) <= value < 2**63:
                raise ValueError("integer is outside SQLite's signed 64-bit range")
        elif isinstance(value, float):
            if not math.isfinite(value):
                raise ValueError("nonfinite numeric value")
        elif value is not None:
            raise TypeError("unsupported SQLite record value")


class SessionIndexer:
    """Incrementally persist complete records with bounded read/write batches."""

    def __init__(self, index: SessionIndex, config: MonitorConfig, lease_owner: Optional[str] = None):
        self.index = index
        self.config = config
        self._owner = lease_owner

    def _write_transaction(self):
        return self.index.write_transaction(expected_owner=self._owner)

    def scan_once(self, sessions_dir: Path, progress: Optional[Callable[[IndexStatus], None]] = None) -> IndexStatus:
        with self.index.indexing_lease(self._owner) as owner:
            previous_owner = self._owner
            self._owner = owner
            try:
                self.index.bind_sessions_root(sessions_dir, expected_owner=owner)
                return self._scan_once(sessions_dir, progress)
            finally:
                self._owner = previous_owner

    def _scan_once(self, sessions_dir: Path, progress=None) -> IndexStatus:
        started = time.time()
        files = iter_session_files(sessions_dir)
        sizes = {}
        for path in files:
            try:
                sizes[path] = path.stat().st_size
            except OSError:
                sizes[path] = 0
        status = IndexStatus(
            "indexing", False, len(files), 0, sum(sizes.values()), 0, None, None, started, None, started
        )

        def publish(**changes):
            nonlocal status
            status = replace(status, updated_at=time.time(), **changes)
            with self._write_transaction() as connection:
                connection.execute(
                    """UPDATE index_state SET status=?, complete=?, total_files=?, processed_files=?,
                       total_bytes=?, processed_bytes=?, current_file=?, last_error=?, started_at=?,
                       completed_at=?, updated_at=?, failed_files=? WHERE singleton_id=1""",
                    (
                        status.status,
                        status.complete,
                        status.total_files,
                        status.processed_files,
                        status.total_bytes,
                        status.processed_bytes,
                        status.current_file,
                        status.last_error,
                        status.started_at,
                        status.completed_at,
                        status.updated_at,
                        status.failed_files,
                    ),
                )
            if progress is not None:
                progress(status)

        publish()
        discovered = {self.index._canonical_path(path) for path in files}
        root = Path(sessions_dir).resolve()
        with self._write_transaction() as connection:
            for row in connection.execute("SELECT file_id, path FROM session_files").fetchall():
                if Path(row["path"]).is_relative_to(root) and row["path"] not in discovered:
                    connection.execute(
                        "UPDATE session_files SET missing=1, updated_at=? WHERE file_id=?",
                        (time.time(), row["file_id"]),
                    )
        processed = 0
        completed_files = 0
        read_error = None
        renew_at = time.monotonic() + _LEASE_TTL_SECONDS / 3
        for number, path in enumerate(files):
            publish(current_file=str(path))

            def batch_progress(offset):
                nonlocal renew_at
                if time.monotonic() >= renew_at:
                    if not self.index.renew_lease(self._owner, _LEASE_TTL_SECONDS):
                        raise LostLeaseError("Indexing lease lost during scan")
                    renew_at = time.monotonic() + _LEASE_TTL_SECONDS / 3
                publish(processed_bytes=processed + min(offset, sizes[path]))

            try:
                changed, error = self._scan_file(path, batch_progress)
                if error:
                    publish(last_error=error)
                if changed:
                    publish(changed_files=status.changed_files + 1)
                state = self.index.get_file_state(path)
                processed += min(state.offset, sizes[path])
                if state.offset >= sizes[path]:
                    completed_files += 1
            except OSError as error:
                self._record_file_error(path, error)
                state = self.index.get_file_state(path)
                processed += min(state.offset, sizes[path])
                read_error = "Unreadable session file; check permissions/availability; will retry. " + _sanitized_error(
                    error
                )
                publish(last_error=read_error, failed_files=status.failed_files + 1)
            publish(processed_files=completed_files, processed_bytes=processed)
        self._backfill_legacy_quota()
        complete = completed_files == len(files) and status.failed_files == 0
        publish(
            status="ready" if complete else ("error" if status.failed_files else "indexing"),
            complete=complete,
            current_file=None,
            completed_at=time.time() if complete else None,
            last_error=read_error or status.last_error,
        )
        return status

    def _ensure_file(self, path: Path) -> FileState:
        now = time.time()
        with self._write_transaction() as connection:
            connection.execute(
                "INSERT OR IGNORE INTO session_files(path, created_at, updated_at) VALUES (?, ?, ?)",
                (self.index._canonical_path(path), now, now),
            )
        return self.index.get_file_state(path)

    def _record_file_error(self, path: Path, error: OSError) -> None:
        state = self._ensure_file(path)
        with self._write_transaction() as connection:
            connection.execute(
                "UPDATE session_files SET missing=?, error=?, read_failed=1, updated_at=?, last_scanned_at=? WHERE file_id=?",
                (
                    isinstance(error, FileNotFoundError),
                    _sanitized_error(error),
                    time.time(),
                    time.time(),
                    state.file_id,
                ),
            )

    def _scan_file(self, path: Path, progress: Callable[[int], None]):
        state = self._ensure_file(path)
        with path.open("rb") as stream:
            stat = os.fstat(stream.fileno())
            head = stream.read(min(4096, stat.st_size))
            head_hash = hashlib.sha256(head).hexdigest()
            # For a short growing file compare only bytes covered by the old hash.
            old_prefix_hash = hashlib.sha256(head[: min(state.size, 4096)]).hexdigest()
            identity = self._file_identity(stat)
            reset = (
                stat.st_size < state.size
                or (state.head_hash and old_prefix_hash != state.head_hash)
                or (state.file_identity and identity and state.file_identity != identity)
                or (state.head_hash and stat.st_size == state.size and stat.st_mtime_ns != state.mtime_ns)
            )
            parser = (
                _ParserState()
                if reset
                else _ParserState(
                    state.last_model or "unknown",
                    state.last_cwd,
                    UsageDelta(
                        state.previous_input_tokens,
                        state.previous_cached_input_tokens,
                        state.previous_output_tokens,
                        state.previous_reasoning_output_tokens,
                        state.previous_total_tokens,
                    ),
                )
            )
            offset = 0 if reset else state.offset
            changed = bool(reset or not state.head_hash or stat.st_size != state.size or offset < stat.st_size)
            if reset:
                with self._write_transaction() as connection:
                    connection.execute("DELETE FROM usage_events WHERE file_id=?", (state.file_id,))
                    connection.execute("DELETE FROM rate_limit_snapshots WHERE file_id=?", (state.file_id,))
                    connection.execute("DELETE FROM quota_cache WHERE file_id=?", (state.file_id,))
                    self._save_cursor(connection, state.file_id, stat, head_hash, 0, parser, None)
            if not changed:
                error = None if state.read_failed else state.error
                with self._write_transaction() as connection:
                    self._save_cursor(connection, state.file_id, stat, head_hash, offset, parser, error)
                return False, error

            last_checkpoint = time.monotonic()

            def checkpoint():
                nonlocal last_checkpoint
                current = time.monotonic()
                if current - last_checkpoint >= 1.0:
                    progress(reader.completed_offset)
                    last_checkpoint = current

            reader = iter_relevant_lines(stream, offset, checkpoint=checkpoint)
            events = []
            snapshots = []
            quotas = []
            malformed = 0
            batch_lines = 0
            batch_offset = offset

            def flush():
                error = None
                if malformed or reader.skipped_oversized:
                    error = f"Skipped {malformed} malformed and {reader.skipped_oversized} oversized relevant records"
                with self._write_transaction() as connection:
                    connection.executemany(
                        """INSERT OR IGNORE INTO usage_events(file_id, source_offset, occurred_at, model, cwd,
                           input_tokens, cached_input_tokens, output_tokens, reasoning_output_tokens,
                           total_tokens, pricing_source, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                        events,
                    )
                    connection.executemany(
                        """INSERT OR IGNORE INTO rate_limit_snapshots(file_id, source_offset, limit_id, limit_name,
                           observed_at, used_percent, window_minutes, resets_at, resets_in_seconds, created_at)
                           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                        snapshots,
                    )
                    for quota in quotas:
                        connection.execute(
                            """INSERT INTO quota_cache(file_id, source_offset, observed_at, payload_json, created_at)
                               VALUES (?, ?, ?, ?, ?)
                               ON CONFLICT(file_id) DO UPDATE SET source_offset=excluded.source_offset,
                                   observed_at=excluded.observed_at, payload_json=excluded.payload_json,
                                   created_at=excluded.created_at
                               WHERE excluded.observed_at > quota_cache.observed_at OR
                                   (excluded.observed_at = quota_cache.observed_at AND excluded.source_offset >= quota_cache.source_offset)""",
                            quota,
                        )
                    self._save_cursor(
                        connection, state.file_id, stat, head_hash, reader.completed_offset, parser, error
                    )
                events.clear()
                snapshots.clear()
                quotas.clear()
                progress(reader.completed_offset)
                return error

            for line in reader:
                try:
                    # Parse into a candidate: validation failure must not change the
                    # baseline or context used by the next record. _parse_line
                    # replaces UsageDelta rather than mutating the shared value.
                    candidate = replace(parser)
                    event, snapshot, quota = self._parse_line(line, candidate, state.file_id)
                    _validate_sqlite_values((candidate.model, candidate.cwd))
                    _validate_sqlite_values(vars(candidate.previous).values())
                    _validate_sqlite_values(event or ())
                    _validate_sqlite_values(snapshot or ())
                    _validate_sqlite_values(quota or ())
                    parser = candidate
                    if event is not None:
                        events.append(event)
                    if snapshot is not None:
                        snapshots.append(snapshot)
                    if quota is not None:
                        quotas.append(quota)
                except (ValueError, TypeError, AttributeError, OverflowError, RecursionError):
                    malformed += 1
                batch_lines += 1
                if batch_lines >= 256 or line.next_offset - batch_offset >= 4 * 1024 * 1024:
                    flush()
                    batch_lines = 0
                    batch_offset = line.next_offset
            return changed, flush()

    @staticmethod
    def _file_identity(stat) -> str:
        return f"{stat.st_dev}:{stat.st_ino}" if stat.st_ino else ""

    @staticmethod
    def _save_cursor(connection, file_id, stat, head_hash, offset, parser, error):
        previous = parser.previous
        now = time.time()
        connection.execute(
            """UPDATE session_files SET size=?, mtime_ns=?, head_hash=?, offset=?, last_model=?, last_cwd=?,
               previous_input_tokens=?, previous_cached_input_tokens=?, previous_output_tokens=?,
               previous_reasoning_output_tokens=?, previous_total_tokens=?, missing=0, error=?,
               updated_at=?, last_scanned_at=?, file_identity=?, read_failed=0 WHERE file_id=?""",
            (
                stat.st_size,
                stat.st_mtime_ns,
                head_hash,
                offset,
                parser.model,
                parser.cwd,
                previous.input_tokens,
                previous.cached_input_tokens,
                previous.output_tokens,
                previous.reasoning_output_tokens,
                previous.total_tokens,
                error,
                now,
                now,
                SessionIndexer._file_identity(stat),
                file_id,
            ),
        )

    def _parse_line(self, line: RelevantLine, parser: _ParserState, file_id: int):
        obj = json.loads(line.data.decode("utf-8"))
        if not isinstance(obj, dict):
            return None, None, None
        payload = obj.get("payload", {})
        if not isinstance(payload, dict):
            raise ValueError("Relevant record payload must be an object")
        # Preserve the legacy marker precedence, including nested event_msg payloads.
        if _RELEVANT_MARKERS[0] in line.data:
            cwd = payload.get("cwd")
            if isinstance(cwd, str) and cwd:
                parser.cwd = cwd
            return None, None, None
        if _RELEVANT_MARKERS[1] in line.data:
            model, cwd = payload.get("model"), payload.get("cwd")
            if isinstance(model, str) and model:
                parser.model = model
            if isinstance(cwd, str) and cwd:
                parser.cwd = cwd
            return None, None, None
        timestamp = parse_timestamp_local(str(obj.get("timestamp", "")))
        observed = timestamp.isoformat()
        now = time.time()
        snapshot_row = None
        quota_row = None
        rate_limits = payload.get("rate_limits")
        if isinstance(rate_limits, dict):
            snapshot = RateLimitSnapshot.from_payload(timestamp, rate_limits)
            snapshot_row = (
                file_id,
                line.source_offset,
                snapshot.limit_id,
                snapshot.limit_name,
                observed,
                snapshot.used_percent,
                snapshot.window_minutes,
                snapshot.resets_at.isoformat() if snapshot.resets_at else None,
                snapshot.resets_in_seconds,
                now,
            )
            quota_payload = self._sanitize_quota_payload(timestamp, rate_limits)
            if quota_payload is not None:
                quota_row = (
                    file_id,
                    line.source_offset,
                    observed,
                    json.dumps(quota_payload, ensure_ascii=True, separators=(",", ":")),
                    now,
                )
        info = payload.get("info")
        total_usage = info.get("total_token_usage") if isinstance(info, dict) else None
        if not isinstance(total_usage, dict):
            return None, snapshot_row, quota_row
        current = UsageDelta.from_total_usage(total_usage)
        delta = current.diff(parser.previous)
        _validate_sqlite_values(vars(current).values())
        _validate_sqlite_values(vars(delta).values())
        parser.previous = current
        if delta.total_tokens <= 0:
            return None, snapshot_row, quota_row
        _, pricing_source = estimate_cost_usd(parser.model, delta, self.config)
        event = (
            file_id,
            line.source_offset,
            observed,
            parser.model,
            parser.cwd,
            delta.input_tokens,
            delta.cached_input_tokens,
            delta.output_tokens,
            delta.reasoning_output_tokens,
            delta.total_tokens,
            pricing_source,
            now,
        )
        return event, snapshot_row, quota_row

    @staticmethod
    def _sanitize_quota_payload(observed: datetime, rate_limits: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        if rate_limits.get("limit_id") not in (None, "codex"):
            return None
        if rate_limits.get("limit_id") is None and rate_limits.get("limit_name") is not None:
            return None
        limits = []
        windows: Dict[str, Dict[str, Any]] = {}
        for name in ("primary", "secondary"):
            raw = rate_limits.get(name)
            if not isinstance(raw, dict):
                continue
            snapshot = RateLimitSnapshot.from_payload(observed, {**rate_limits, "primary": raw})
            if (
                snapshot.used_percent is None
                or not math.isfinite(float(snapshot.used_percent))
                or snapshot.window_minutes not in (300, 10080)
            ):
                continue
            window = {
                "scope": "global",
                "limit_id": "codex",
                "observed_at": observed.isoformat(),
                "used_percent": snapshot.used_percent,
                "window_minutes": snapshot.window_minutes,
                "resets_at": snapshot.resets_at.isoformat() if snapshot.resets_at else None,
            }
            windows[name] = window
            limits.append(window)
        if not limits:
            return None
        plan = rate_limits.get("plan_type")
        plan = str(plan) if plan is not None else None
        if (plan or "").lower() == "pro":
            limits = [window for window in limits if window.get("window_minutes") == 10080]
            windows = {name: window for name, window in windows.items() if window.get("window_minutes") == 10080}
            if not limits:
                return None
        return {
            "primary": windows.get("primary"),
            "secondary": windows.get("secondary"),
            "limits": limits,
            "plan_type": plan,
            "observed_at": observed.isoformat(),
        }

    def _backfill_legacy_quota(self) -> None:
        """Try at most 16 indexed observations, reading no more than 1 MiB each."""
        with self.index.read_transaction() as reader:
            rows = reader.execute(
                """SELECT r.file_id,r.source_offset,r.observed_at,f.path
                     FROM rate_limit_snapshots r JOIN session_files f ON f.file_id=r.file_id
                LEFT JOIN quota_cache q ON q.file_id=r.file_id
                    WHERE q.file_id IS NULL AND f.missing=0
                      AND (r.limit_id='codex' OR (r.limit_id IS NULL AND r.limit_name IS NULL))
                 ORDER BY r.observed_at DESC,r.source_offset DESC LIMIT 16"""
            ).fetchall()
        for row in rows:
            try:
                with Path(row["path"]).open("rb") as stream:
                    stream.seek(int(row["source_offset"]))
                    raw = stream.readline(1024 * 1024 + 1)
                if len(raw) > 1024 * 1024:
                    continue
                obj = json.loads(raw)
                observed = parse_timestamp_local(str(obj.get("timestamp", "")))
                if observed.isoformat() != row["observed_at"]:
                    continue
                payload = obj.get("payload")
                rate_limits = payload.get("rate_limits") if isinstance(payload, dict) else None
                quota = self._sanitize_quota_payload(observed, rate_limits) if isinstance(rate_limits, dict) else None
                if quota is None:
                    continue
                with self._write_transaction() as writer:
                    writer.execute(
                        """INSERT OR REPLACE INTO quota_cache(file_id,source_offset,observed_at,payload_json,created_at)
                           VALUES (?,?,?,?,?)""",
                        (
                            row["file_id"],
                            row["source_offset"],
                            row["observed_at"],
                            json.dumps(quota, ensure_ascii=True, separators=(",", ":")),
                            time.time(),
                        ),
                    )
                return
            except (OSError, ValueError, TypeError, AttributeError, UnicodeError, RecursionError):
                continue


class _StopIndexing(Exception):
    """Unwind a scan at a committed progress checkpoint during shutdown."""


def history_page(
    events: Iterable[Dict[str, Any]],
    error: Optional[str],
    offset: int = 0,
    limit: int = 200,
    q: str = "",
    model: str = "",
    cwd: str = "",
    since: str = "",
    until: str = "",
    models=None,
    sort: str = "timestamp",
    descending: bool = True,
) -> Dict[str, Any]:
    """Filter immutable published history with O(page size) request memory.

    The worker owns the complete normalized history. A request retains that
    generation, counts matching rows, and copies only the selected page. Filters
    deliberately use the same anonymized labels visible in the dashboard.
    """
    offset, limit = max(0, offset), max(1, min(500, limit))
    q, model, cwd = q.strip().lower(), model.strip().lower(), cwd.strip().lower()
    selected = None if models is None else {str(m).lower() for m in models}

    def stamp(value):
        if not value:
            return ""
        parsed = datetime.fromisoformat(value)
        if parsed.tzinfo is not None:
            parsed = parsed.astimezone().replace(tzinfo=None)
        return parsed.isoformat(sep=" ", timespec="seconds")

    since, until = stamp(since), stamp(until)

    def matches(event):
        em, ec = str(event.get("model", "")).lower(), str(event.get("cwd", "")).lower()
        ts = str(event.get("timestamp", "")).replace("T", " ")
        return (
            (not q or q in em or q in ec)
            and (not model or em == model)
            and (not cwd or cwd in ec)
            and (selected is None or em in selected)
            and (not since or ts >= since)
            and (not until or ts <= until)
        )

    filtered = (event for event in events if matches(event))
    if sort != "timestamp" or not descending:
        keys = {
            "tokens": lambda e: e.get("tokens", {}).get("total", 0),
            "cost": lambda e: e.get("cost_usd", {}).get("total", 0),
            "model": lambda e: e.get("model", ""),
            "timestamp": lambda e: e.get("timestamp", ""),
        }
        filtered = sorted(filtered, key=keys.get(sort, keys["timestamp"]), reverse=descending)
    page, total = [], 0
    for event in filtered:
        event_model, event_cwd = str(event.get("model", "")).lower(), str(event.get("cwd", "")).lower()
        if (
            (q and q not in event_model and q not in event_cwd)
            or (model and event_model != model)
            or (cwd and cwd not in event_cwd)
        ):
            continue
        if offset <= total < offset + limit:
            page.append(event)
        total += 1
    return {"error": error, "total": total, "offset": offset, "limit": limit, "events": copy.deepcopy(page)}


class IndexCoordinator:
    """One daemon worker; request threads only read published dictionaries."""

    def __init__(
        self,
        index: SessionIndex,
        sessions_dir: Path,
        config_loader: Callable[[], MonitorConfig],
        cwd_filter: Optional[str],
        interval: float = 3.0,
        rebuild_index: bool = False,
    ):
        self.index = index
        self.sessions_dir = Path(sessions_dir)
        self.config_loader = config_loader
        self.cwd_filter = cwd_filter
        self.interval = max(0.01, float(interval))
        self._lock = threading.Lock()
        self._refresh = threading.Event()
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._owner = f"{os.getpid()}:{uuid.uuid4().hex}"
        self._lease_ttl = _LEASE_TTL_SECONDS
        self._renew_at = 0.0
        self._scan_generation: Optional[int] = None
        self._bootstrapped = False
        self._rebuild_pending = rebuild_index
        self._last_aggregate_key = None
        self._force_summary = False
        self._aggregation_progress = {}
        self._aggregation_started = None
        config = config_loader()
        # No database opening, validation, or history deserialization before bind.
        self._summary = aggregate_usage_events([], [], config, cwd_filter=cwd_filter)
        self._events = []
        self._status = IndexStatus("starting", False, 0, 0, 0, 0, None, None, None, None, None)
        self._status_payload = self._status.to_payload()

    def start(self) -> None:
        with self._lock:
            if self._thread is not None and self._thread.is_alive():
                return
            self._stop.clear()
            self._refresh.set()
            self._thread = threading.Thread(target=self._run, name="codex-session-index", daemon=True)
            self._thread.start()

    def request_refresh(self) -> None:
        self._force_summary = True
        self._refresh.set()

    def snapshot(self) -> Dict[str, Any]:
        with self._lock:
            summary, status = self._summary, self._status_payload
            aggregation = dict(self._aggregation_progress)
            started = self._aggregation_started
        # Published objects are never mutated. Copy outside the lock so callers
        # can change their response without corrupting another request's data.
        result = copy.deepcopy(summary)
        result["index"] = copy.deepcopy(status)
        if result["index"]["status"] == "aggregating":
            result["index"].update(aggregation)
            result["index"]["phase_elapsed_seconds"] = round(time.monotonic() - started, 1) if started else 0
        return result

    def _report_aggregation(self, phase, processed, total):
        if self._stop.is_set():
            raise _StopIndexing()
        with self._lock:
            self._aggregation_progress = {"phase": phase, "phase_processed": processed, "phase_total": total}

    def events_snapshot(self) -> List[Dict[str, Any]]:
        """Return history separately so periodic data responses stay small."""
        with self._lock:
            events = self._events
        return [copy.deepcopy(event) for event in events]

    def events_page(
        self, offset: int = 0, limit: int = 200, q: str = "", model: str = "", cwd: str = "", **filters
    ) -> Dict[str, Any]:
        with self._lock:
            events, error = self._events, self._status_payload.get("last_error")
        if hasattr(events, "page"):
            return events.page(error, offset, limit, q, model, cwd, **filters)
        return history_page(events, error, offset, limit, q, model, cwd, **filters)

    def _bootstrap(self, config: MonitorConfig) -> None:
        self.index.initialize()
        # The root binding survives rebuild, so another coordinator cannot load
        # unrelated history while waiting for this database's indexing lease.
        self.index.bind_sessions_root(self.sessions_dir)
        if self._rebuild_pending:
            if not self.index.acquire_lease(self._owner, self._lease_ttl):
                raise RuntimeError("Cannot rebuild index: another indexing process holds the lease; stop it and retry")
            try:
                self.index.rebuild()
                self._rebuild_pending = False
            finally:
                self.index.release_lease(self._owner)
        # Reject a root mismatch before serving any cached totals or history.
        cached = self._cached_summary(config)
        status = replace(self.index.read_state(), status="starting", complete=False, current_file=None, last_error=None)
        self._publish(status, cached)
        self._bootstrapped = True

    def _cached_summary(self, config: MonitorConfig) -> Optional[Dict[str, Any]]:
        return self.index.cached_summary(config, self.cwd_filter) or self.index.cached_summary(
            config, self.cwd_filter, include_events=True
        )

    def stop(self, timeout: Optional[float] = 5.0) -> bool:
        """Request shutdown and report termination; None waits until stopped.

        The index must stay open when a bounded wait returns False.
        """
        self._stop.set()
        self._refresh.set()
        with self._lock:
            thread = self._thread
        if thread is not None and thread is not threading.current_thread():
            thread.join(timeout=None if timeout is None else max(0.0, timeout))
        return thread is None or not thread.is_alive()

    def _publish(self, status: IndexStatus, summary: Optional[Dict[str, Any]] = None) -> None:
        payload = status.to_payload()
        if summary is not None:
            summary = dict(summary)
            events = summary.pop("_history", None)
            if events is None and "events" in summary:
                from codex_glass.storage.compact import CompactHistory

                events = CompactHistory()
                for row in summary.pop("events", []):
                    events.append(row)
        with self._lock:
            self._status = status
            self._status_payload = payload
            if summary is not None and summary.get("index", {}).get("generation", -1) >= self._summary.get(
                "index", {}
            ).get("generation", -1):
                same_generation = summary.get("index", {}).get("generation") == self._summary.get("index", {}).get(
                    "generation"
                )
                self._summary = summary
                if events is not None:
                    self._events = events
                elif not same_generation:
                    self._events = []

    def _publish_summary(self, status: IndexStatus, summary: Dict[str, Any]) -> bool:
        # The read/aggregate/serialize phase does not need an indexing lease.
        # Hold the writer transaction only for this generation check and the
        # short in-memory publication so another process cannot advance between
        # them. A superseded read is retried without publishing stale totals.
        with self.index.write_transaction() as writer:
            if (
                writer.execute("SELECT generation FROM index_state WHERE singleton_id=1").fetchone()[0]
                != summary["index"]["generation"]
            ):
                return False
            if self._stop.is_set():
                raise _StopIndexing()
            self._publish(status, summary)
        return True

    def _check_lease(self) -> None:
        if self._stop.is_set():
            raise _StopIndexing()
        if time.monotonic() >= self._renew_at:
            if not self.index.renew_lease(self._owner, self._lease_ttl):
                raise RuntimeError("Indexing lease expired or was acquired by another process")
            self._renew_at = time.monotonic() + self._lease_ttl / 3

    def _progress(self, status: IndexStatus) -> None:
        self._check_lease()
        # Ready is published together with the newly aggregated summary.
        self._publish(replace(status, status="indexing", complete=False))

    def _record_error(self, error: Exception) -> None:
        with self._lock:
            previous = self._status
        message = _sanitized_error(error)
        status = replace(
            previous, status="error", complete=False, current_file=None, last_error=message, updated_at=time.time()
        )
        if isinstance(error, (LostLeaseError, IncompatibleIndexError)):
            self._publish(status)
            return
        try:
            with self.index.write_transaction() as writer:
                writer.execute(
                    """UPDATE index_state SET status='error', complete=0, current_file=NULL,
                       last_error=?, updated_at=? WHERE singleton_id=1 AND (
                           (lease_owner=? AND lease_expires_at>?) OR
                           (generation=? AND (lease_owner IS NULL OR lease_expires_at<=?)))""",
                    (message, time.time(), self._owner, time.time(), self._scan_generation, time.time()),
                )
        except Exception:
            pass  # Even a failed database remains observable through snapshot().
        self._publish(status)

    def _run(self) -> None:
        self._lease_ttl = max(_LEASE_TTL_SECONDS, math.ceil(self.interval * 3))
        try:
            while not self._stop.is_set():
                self._refresh.wait(self.interval)
                self._refresh.clear()
                if self._stop.is_set():
                    break
                self._scan_generation = None
                try:
                    config = self.config_loader()
                    if not self._bootstrapped:
                        self._bootstrap(config)
                    if not self.index.acquire_lease(self._owner, self._lease_ttl):
                        status = replace(
                            self.index.read_state(),
                            status="waiting",
                            complete=False,
                            current_file=None,
                            last_error=None,
                        )
                        self._publish(status, self._cached_summary(config))
                        continue
                    self._renew_at = time.monotonic() + self._lease_ttl / 3
                    self._progress(replace(self.index.read_state(), status="indexing", complete=False))
                    status = SessionIndexer(self.index, config, lease_owner=self._owner).scan_once(
                        self.sessions_dir, progress=self._progress
                    )
                    self._check_lease()
                    with self.index.read_transaction() as reader:
                        self._scan_generation = reader.execute(
                            "SELECT generation FROM index_state WHERE singleton_id=1"
                        ).fetchone()[0]
                    # Exclusive ownership protects raw indexing. Committed SQL
                    # reads and generation-guarded cache/publication remain safe
                    # without it, even if aggregation or JSON takes minutes.
                    if not self.index.release_lease(self._owner):
                        raise RuntimeError("Indexing lease lost before summary generation")
                    # No new facts or prices: a minute clock tick is sufficient
                    # for rolling windows. Explicit refresh always recomputes.
                    aggregate_key = (
                        self._scan_generation,
                        summary_cache_key(config, self.cwd_filter, False),
                        int(time.time() // 60),
                    )
                    forced = self._force_summary
                    self._force_summary = False
                    if aggregate_key == self._last_aggregate_key and not forced:
                        self._publish(status)
                        continue
                    aggregating = replace(status, status="aggregating", complete=False)
                    self._aggregation_started = time.monotonic()
                    self._publish(aggregating)
                    summary = self.index.build_summary(
                        config,
                        cwd_filter=self.cwd_filter,
                        compact_events=True,
                        summary_ready=lambda result: self._publish_summary(aggregating, result),
                        progress=self._report_aggregation,
                    )
                    if not self._publish_summary(status, summary):
                        self.request_refresh()
                    else:
                        self._last_aggregate_key = aggregate_key
                except _StopIndexing:
                    break
                except Exception as error:
                    self._record_error(error)
        finally:
            try:
                self.index.release_lease(self._owner)
            except Exception as error:
                self._record_error(error)


_SCHEMA_STATEMENTS = (
    """
    CREATE TABLE IF NOT EXISTS session_files (
        file_id INTEGER PRIMARY KEY,
        path TEXT NOT NULL UNIQUE,
        size INTEGER NOT NULL DEFAULT 0,
        mtime_ns INTEGER NOT NULL DEFAULT 0,
        head_hash TEXT NOT NULL DEFAULT '',
        offset INTEGER NOT NULL DEFAULT 0,
        last_model TEXT,
        last_cwd TEXT,
        previous_input_tokens INTEGER NOT NULL DEFAULT 0,
        previous_cached_input_tokens INTEGER NOT NULL DEFAULT 0,
        previous_output_tokens INTEGER NOT NULL DEFAULT 0,
        previous_reasoning_output_tokens INTEGER NOT NULL DEFAULT 0,
        previous_total_tokens INTEGER NOT NULL DEFAULT 0,
        missing INTEGER NOT NULL DEFAULT 0,
        error TEXT,
        created_at REAL NOT NULL,
        updated_at REAL NOT NULL,
        last_scanned_at REAL
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS usage_events (
        event_id INTEGER PRIMARY KEY,
        file_id INTEGER NOT NULL REFERENCES session_files(file_id) ON DELETE CASCADE,
        source_offset INTEGER NOT NULL,
        occurred_at TEXT NOT NULL,
        model TEXT,
        cwd TEXT,
        input_tokens INTEGER NOT NULL DEFAULT 0,
        cached_input_tokens INTEGER NOT NULL DEFAULT 0,
        output_tokens INTEGER NOT NULL DEFAULT 0,
        reasoning_output_tokens INTEGER NOT NULL DEFAULT 0,
        total_tokens INTEGER NOT NULL DEFAULT 0,
        pricing_source TEXT,
        created_at REAL NOT NULL,
        UNIQUE(file_id, source_offset)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS rate_limit_snapshots (
        snapshot_id INTEGER PRIMARY KEY,
        file_id INTEGER NOT NULL REFERENCES session_files(file_id) ON DELETE CASCADE,
        source_offset INTEGER NOT NULL,
        limit_id TEXT,
        limit_name TEXT,
        observed_at TEXT NOT NULL,
        used_percent REAL,
        window_minutes INTEGER,
        resets_at TEXT,
        resets_in_seconds INTEGER,
        created_at REAL NOT NULL,
        UNIQUE(file_id, source_offset)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS index_state (
        singleton_id INTEGER PRIMARY KEY CHECK (singleton_id = 1),
        status TEXT NOT NULL,
        complete INTEGER NOT NULL DEFAULT 0,
        total_files INTEGER NOT NULL DEFAULT 0,
        processed_files INTEGER NOT NULL DEFAULT 0,
        total_bytes INTEGER NOT NULL DEFAULT 0,
        processed_bytes INTEGER NOT NULL DEFAULT 0,
        current_file TEXT,
        last_error TEXT,
        started_at REAL,
        completed_at REAL,
        updated_at REAL NOT NULL,
        lease_owner TEXT,
        lease_expires_at REAL
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS summary_cache (
        cache_key TEXT PRIMARY KEY,
        payload_json TEXT NOT NULL,
        created_at REAL NOT NULL
    )
    """,
    "CREATE INDEX IF NOT EXISTS usage_events_occurred_at_idx ON usage_events(occurred_at)",
    "CREATE INDEX IF NOT EXISTS rate_limit_snapshots_observed_at_idx ON rate_limit_snapshots(observed_at)",
)
