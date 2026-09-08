"""Portable SQLite history import and cross-computer fact identities."""

from __future__ import annotations

import hashlib
import json
import re
import sqlite3
import time
from dataclasses import asdict, dataclass
from pathlib import Path, PurePosixPath, PureWindowsPath
from typing import Any, Iterator


_ROLLOUT_ID = re.compile(
    r"(?i)([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})(?=\.jsonl$)"
)
_REQUIRED_SOURCE_COLUMNS = {
    "session_files": {"file_id", "path", "size", "mtime_ns", "head_hash"},
    "usage_events": {
        "file_id", "source_offset", "occurred_at", "model", "cwd", "input_tokens",
        "cached_input_tokens", "output_tokens", "reasoning_output_tokens", "total_tokens",
        "pricing_source", "created_at",
    },
    "rate_limit_snapshots": {
        "file_id", "source_offset", "limit_id", "limit_name", "observed_at", "used_percent",
        "window_minutes", "resets_at", "resets_in_seconds", "created_at",
    },
    "index_state": {"singleton_id", "sessions_root"},
}


class HistoryImportError(RuntimeError):
    """The source history cannot be safely imported."""


@dataclass(frozen=True)
class ImportResult:
    source_id: str
    source_sha256: str
    imported_files: int
    imported_usage_events: int
    imported_rate_limit_snapshots: int
    duplicate_usage_events: int
    duplicate_rate_limit_snapshots: int
    backup_path: str

    def to_payload(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class RemoveResult:
    source_id: str
    removed_files: int
    removed_usage_events: int
    removed_rate_limit_snapshots: int

    def to_payload(self) -> dict[str, Any]:
        return asdict(self)


def _digest(kind: str, values: tuple[Any, ...]) -> str:
    payload = json.dumps([kind, *values], ensure_ascii=True, separators=(",", ":"), default=str)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _basename(path: str) -> str:
    windows = PureWindowsPath(path).name
    return windows if windows != path else PurePosixPath(path).name


def session_key(path: str, head_hash: str = "") -> str:
    """Identify a session independently of the computer holding its file."""
    basename = _basename(str(path))
    match = _ROLLOUT_ID.search(basename)
    if match:
        return _digest("session-rollout-v1", (match.group(1).lower(),))
    if head_hash:
        return _digest("session-head-v1", (basename.lower(), str(head_hash).lower()))
    return _digest("session-path-v1", (str(path).replace("\\", "/").lower(),))


def usage_event_key(session: str, source_offset: int, occurred_at: str, model: str | None,
                    cwd: str | None, input_tokens: int, cached_input_tokens: int,
                    output_tokens: int, reasoning_output_tokens: int, total_tokens: int) -> str:
    return _digest("usage-event-v1", (
        session, int(source_offset), occurred_at, model, cwd, int(input_tokens),
        int(cached_input_tokens), int(output_tokens), int(reasoning_output_tokens), int(total_tokens),
    ))


def rate_limit_key(session: str, source_offset: int, limit_id: str | None,
                   limit_name: str | None, observed_at: str, used_percent: float | None,
                   window_minutes: int | None, resets_at: str | None,
                   resets_in_seconds: int | None) -> str:
    return _digest("rate-limit-v1", (
        session, int(source_offset), limit_id, limit_name, observed_at, used_percent,
        window_minutes, resets_at, resets_in_seconds,
    ))


def ensure_import_schema(connection: sqlite3.Connection) -> None:
    """Install additive tables used only by external immutable history."""
    statements = (
        """
        CREATE TABLE IF NOT EXISTS history_imports (
            source_id TEXT PRIMARY KEY,
            source_sha256 TEXT NOT NULL UNIQUE,
            source_path TEXT NOT NULL,
            sessions_root TEXT,
            first_imported_at REAL NOT NULL,
            last_imported_at REAL NOT NULL,
            source_files INTEGER NOT NULL DEFAULT 0,
            source_usage_events INTEGER NOT NULL DEFAULT 0,
            source_rate_limit_snapshots INTEGER NOT NULL DEFAULT 0
        )
        """,
        """
        CREATE TABLE IF NOT EXISTS imported_session_files (
            source_id TEXT NOT NULL REFERENCES history_imports(source_id) ON DELETE CASCADE,
            source_file_id INTEGER NOT NULL,
            source_path TEXT NOT NULL,
            session_key TEXT NOT NULL,
            head_hash TEXT NOT NULL DEFAULT '',
            size INTEGER NOT NULL DEFAULT 0,
            mtime_ns INTEGER NOT NULL DEFAULT 0,
            PRIMARY KEY(source_id, source_file_id)
        )
        """,
        """
        CREATE TABLE IF NOT EXISTS imported_usage_events (
            source_id TEXT NOT NULL REFERENCES history_imports(source_id) ON DELETE CASCADE,
            event_key TEXT NOT NULL,
            source_file_id INTEGER NOT NULL,
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
            PRIMARY KEY(source_id, event_key)
        )
        """,
        """
        CREATE TABLE IF NOT EXISTS imported_rate_limit_snapshots (
            source_id TEXT NOT NULL REFERENCES history_imports(source_id) ON DELETE CASCADE,
            snapshot_key TEXT NOT NULL,
            source_file_id INTEGER NOT NULL,
            source_offset INTEGER NOT NULL,
            limit_id TEXT,
            limit_name TEXT,
            observed_at TEXT NOT NULL,
            used_percent REAL,
            window_minutes INTEGER,
            resets_at TEXT,
            resets_in_seconds INTEGER,
            created_at REAL NOT NULL,
            PRIMARY KEY(source_id, snapshot_key)
        )
        """,
        "CREATE INDEX IF NOT EXISTS imported_usage_occurred_idx ON imported_usage_events(occurred_at)",
        "CREATE INDEX IF NOT EXISTS imported_usage_source_idx ON imported_usage_events(source_id)",
        "CREATE INDEX IF NOT EXISTS imported_usage_key_idx ON imported_usage_events(event_key)",
        "CREATE INDEX IF NOT EXISTS imported_rate_observed_idx ON imported_rate_limit_snapshots(observed_at)",
        "CREATE INDEX IF NOT EXISTS imported_rate_source_idx ON imported_rate_limit_snapshots(source_id)",
        "CREATE INDEX IF NOT EXISTS imported_rate_key_idx ON imported_rate_limit_snapshots(snapshot_key)",
        "CREATE INDEX IF NOT EXISTS imported_sessions_key_idx ON imported_session_files(session_key)",
    )
    for statement in statements:
        connection.execute(statement)


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        while chunk := stream.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def _read_only_connection(path: Path) -> sqlite3.Connection:
    connection = sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True, timeout=30.0)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA query_only = ON")
    return connection


def _chunks(rows: Iterator[tuple[Any, ...]], size: int = 4096) -> Iterator[list[tuple[Any, ...]]]:
    batch: list[tuple[Any, ...]] = []
    for row in rows:
        batch.append(row)
        if len(batch) >= size:
            yield batch
            batch = []
    if batch:
        yield batch


class HistoryImporter:
    """Copy immutable history facts into a SessionIndex with source provenance."""

    def __init__(self, target) -> None:
        self.target = target

    @staticmethod
    def _validate_source(connection: sqlite3.Connection) -> None:
        try:
            integrity = connection.execute("PRAGMA integrity_check").fetchone()[0]
            if integrity != "ok":
                raise HistoryImportError(f"Source SQLite integrity check failed: {integrity}")
            if int(connection.execute("PRAGMA user_version").fetchone()[0]) != 1:
                raise HistoryImportError("Source SQLite schema version is unsupported")
            tables = {row[0] for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            )}
            for table, required in _REQUIRED_SOURCE_COLUMNS.items():
                if table not in tables:
                    raise HistoryImportError(f"Source SQLite table is missing: {table}")
                present = {row[1] for row in connection.execute(f"PRAGMA table_info({table})")}
                if not required <= present:
                    raise HistoryImportError(f"Source SQLite table is incomplete: {table}")
        except sqlite3.DatabaseError as error:
            raise HistoryImportError(f"Source SQLite database is invalid: {error}") from error

    def _backup_target(self, source_id: str) -> Path:
        stamp = time.strftime("%Y%m%d-%H%M%S")
        backup = self.target.path.with_name(
            f"{self.target.path.stem}.before-import-{stamp}-{source_id[:8]}-{time.time_ns() % 1_000_000_000:09d}.sqlite3"
        )
        destination = sqlite3.connect(backup)
        try:
            self.target.connection.backup(destination)
        finally:
            destination.close()
        return backup

    def import_database(self, source_path: Path) -> ImportResult:
        source_path = Path(source_path).expanduser().resolve()
        if not source_path.is_file():
            raise HistoryImportError("Source SQLite database does not exist or is not a regular file")
        if source_path == self.target.path.expanduser().resolve():
            raise HistoryImportError("Source SQLite database cannot be the target database")

        source_sha256 = _sha256_file(source_path)
        source_id = source_sha256[:24]
        try:
            source = _read_only_connection(source_path)
        except sqlite3.DatabaseError as error:
            raise HistoryImportError(f"Source SQLite database is invalid: {error}") from error
        try:
            self._validate_source(source)
            state = source.execute(
                "SELECT sessions_root FROM index_state WHERE singleton_id=1"
            ).fetchone()
            sessions_root = state[0] if state else None
            source_counts = {
                table: int(source.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0])
                for table in ("session_files", "usage_events", "rate_limit_snapshots")
            }
            files = list(source.execute(
                "SELECT file_id, path, head_hash, size, mtime_ns FROM session_files ORDER BY file_id"
            ))
            file_keys = {int(row["file_id"]): session_key(row["path"], row["head_hash"]) for row in files}

            with self.target.indexing_lease():
                backup = self._backup_target(source_id)
                with self.target.write_transaction() as target:
                    before = {
                        table: int(target.execute(f"SELECT COUNT(*) FROM {table} WHERE source_id=?", (source_id,)).fetchone()[0])
                        for table in ("imported_session_files", "imported_usage_events", "imported_rate_limit_snapshots")
                    }
                    now = time.time()
                    target.execute(
                        """INSERT INTO history_imports(
                               source_id, source_sha256, source_path, sessions_root, first_imported_at,
                               last_imported_at, source_files, source_usage_events, source_rate_limit_snapshots)
                           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                           ON CONFLICT(source_id) DO UPDATE SET
                               source_path=excluded.source_path, sessions_root=excluded.sessions_root,
                               last_imported_at=excluded.last_imported_at,
                               source_files=excluded.source_files,
                               source_usage_events=excluded.source_usage_events,
                               source_rate_limit_snapshots=excluded.source_rate_limit_snapshots""",
                        (source_id, source_sha256, str(source_path), sessions_root, now, now,
                         source_counts["session_files"], source_counts["usage_events"],
                         source_counts["rate_limit_snapshots"]),
                    )
                    target.executemany(
                        """INSERT OR IGNORE INTO imported_session_files(
                               source_id, source_file_id, source_path, session_key, head_hash, size, mtime_ns)
                           VALUES (?, ?, ?, ?, ?, ?, ?)""",
                        [(source_id, int(row["file_id"]), row["path"], file_keys[int(row["file_id"])],
                          row["head_hash"], int(row["size"]), int(row["mtime_ns"])) for row in files],
                    )

                    def usage_rows() -> Iterator[tuple[Any, ...]]:
                        for row in source.execute("SELECT * FROM usage_events ORDER BY event_id"):
                            key = usage_event_key(
                                file_keys[int(row["file_id"])], row["source_offset"], row["occurred_at"],
                                row["model"], row["cwd"], row["input_tokens"], row["cached_input_tokens"],
                                row["output_tokens"], row["reasoning_output_tokens"], row["total_tokens"],
                            )
                            yield (source_id, key, row["file_id"], row["source_offset"], row["occurred_at"],
                                   row["model"], row["cwd"], row["input_tokens"], row["cached_input_tokens"],
                                   row["output_tokens"], row["reasoning_output_tokens"], row["total_tokens"],
                                   row["pricing_source"], row["created_at"])

                    for batch in _chunks(usage_rows()):
                        target.executemany(
                            """INSERT OR IGNORE INTO imported_usage_events(
                                   source_id, event_key, source_file_id, source_offset, occurred_at, model, cwd,
                                   input_tokens, cached_input_tokens, output_tokens, reasoning_output_tokens,
                                   total_tokens, pricing_source, created_at)
                               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                            batch,
                        )

                    def rate_rows() -> Iterator[tuple[Any, ...]]:
                        for row in source.execute("SELECT * FROM rate_limit_snapshots ORDER BY snapshot_id"):
                            key = rate_limit_key(
                                file_keys[int(row["file_id"])], row["source_offset"], row["limit_id"],
                                row["limit_name"], row["observed_at"], row["used_percent"],
                                row["window_minutes"], row["resets_at"], row["resets_in_seconds"],
                            )
                            yield (source_id, key, row["file_id"], row["source_offset"], row["limit_id"],
                                   row["limit_name"], row["observed_at"], row["used_percent"],
                                   row["window_minutes"], row["resets_at"], row["resets_in_seconds"], row["created_at"])

                    for batch in _chunks(rate_rows()):
                        target.executemany(
                            """INSERT OR IGNORE INTO imported_rate_limit_snapshots(
                                   source_id, snapshot_key, source_file_id, source_offset, limit_id, limit_name,
                                   observed_at, used_percent, window_minutes, resets_at, resets_in_seconds, created_at)
                               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                            batch,
                        )

                    after = {
                        table: int(target.execute(f"SELECT COUNT(*) FROM {table} WHERE source_id=?", (source_id,)).fetchone()[0])
                        for table in ("imported_session_files", "imported_usage_events", "imported_rate_limit_snapshots")
                    }
                    inserted = {table: after[table] - before[table] for table in after}
                    if any(inserted.values()) or before["imported_session_files"] == 0:
                        target.execute("UPDATE index_state SET generation=generation+1 WHERE singleton_id=1")
                        target.execute("DELETE FROM summary_cache")
        finally:
            source.close()

        return ImportResult(
            source_id, source_sha256, inserted["imported_session_files"], inserted["imported_usage_events"],
            inserted["imported_rate_limit_snapshots"],
            source_counts["usage_events"] - inserted["imported_usage_events"],
            source_counts["rate_limit_snapshots"] - inserted["imported_rate_limit_snapshots"], str(backup),
        )

    def list_sources(self) -> list[dict[str, Any]]:
        rows = self.target.connection.execute(
            """SELECT source_id, source_sha256, source_path, sessions_root, first_imported_at,
                      last_imported_at, source_files, source_usage_events, source_rate_limit_snapshots
                 FROM history_imports ORDER BY first_imported_at, source_id"""
        )
        return [dict(row) for row in rows]

    def remove_source(self, source_id: str) -> RemoveResult:
        with self.target.indexing_lease():
            with self.target.write_transaction() as target:
                if target.execute("SELECT 1 FROM history_imports WHERE source_id=?", (source_id,)).fetchone() is None:
                    raise HistoryImportError("Imported history source was not found")
                counts = {
                    table: int(target.execute(f"SELECT COUNT(*) FROM {table} WHERE source_id=?", (source_id,)).fetchone()[0])
                    for table in ("imported_session_files", "imported_usage_events", "imported_rate_limit_snapshots")
                }
                target.execute("DELETE FROM history_imports WHERE source_id=?", (source_id,))
                target.execute("UPDATE index_state SET generation=generation+1 WHERE singleton_id=1")
                target.execute("DELETE FROM summary_cache")
        return RemoveResult(source_id, counts["imported_session_files"], counts["imported_usage_events"],
                            counts["imported_rate_limit_snapshots"])
