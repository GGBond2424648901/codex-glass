"""Privacy-preserving live model invocation projection from Codex session metadata."""

from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import threading
import time
from contextlib import closing
from datetime import datetime, timedelta
from heapq import heappush, heapreplace
from pathlib import Path
from typing import Any, Callable, Iterable

from codex_glass.core.live_telemetry import DEFAULT_SAMPLER, LiveTelemetrySampler, _timestamp


_TAIL_BYTES = 4 * 1024 * 1024
_MAX_RECORD_BYTES = 1024 * 1024
_MAX_REVERSE_SCAN = 64 * 1024 * 1024
_REVERSE_CHUNK = 1024 * 1024
_PROJECTION_CACHE: dict[str, tuple[int, int, dict[str, Any] | None, int]] = {}
_DISCOVERY_CACHE: dict[str, tuple[float, float, list[Path]]] = {}
_TITLE_CACHE: dict[tuple[str, str], tuple[float, str | None]] = {}
_CACHE_LOCK = threading.Lock()


def short_model_name(model: str | None) -> str:
    value = (model or "").strip()
    known = {
        "gpt-6-astra": "Astra",
        "gpt-6-sol": "Sol",
        "gpt-6-luna": "Luna",
        "gpt-5.6-sol": "Sol",
        "gpt-5.6-terra": "Terra",
        "gpt-5.6-luna": "Luna",
    }
    return known.get(value, value.removeprefix("gpt-") or "未知")


def _local_thread_titles(session_ids: list[str], sessions_dir: Path | None) -> dict[str, str]:
    """Read only Codex's thread title metadata, never conversation messages."""

    if not session_ids or sessions_dir is None:
        return {}
    homes = [Path(sessions_dir).parent]
    state_files = (
        path for home in homes for path in sorted(
            home.glob("state_*.sqlite"),
            key=lambda item: int(item.stem.rsplit("_", 1)[-1])
            if item.stem.rsplit("_", 1)[-1].isdigit() else -1,
            reverse=True,
        )
    )
    database_path = next((path for path in state_files if path.is_file()), None)
    if database_path is None:
        return {}
    source = str(database_path)
    now = time.monotonic()
    with _CACHE_LOCK:
        cached = {session_id: _TITLE_CACHE.get((source, session_id)) for session_id in session_ids}
    missing = [session_id for session_id, value in cached.items() if value is None or value[0] <= now]
    if missing:
        found: dict[str, str | None] = {session_id: None for session_id in missing}
        placeholders = ",".join("?" for _ in missing)
        try:
            with closing(sqlite3.connect(database_path.resolve().as_uri() + "?mode=ro", uri=True, timeout=0.2)) as connection:
                try:
                    rows = connection.execute(
                        f"SELECT id,name,title FROM threads WHERE id IN ({placeholders})", missing
                    )
                except sqlite3.OperationalError:
                    rows = connection.execute(
                        f"SELECT id,title,title FROM threads WHERE id IN ({placeholders})", missing
                    )
                for session_id, name, title in rows:
                    value = " ".join(str(name or title or "").split())[:120]
                    found[str(session_id)] = value or None
        except (OSError, sqlite3.Error):
            pass
        with _CACHE_LOCK:
            for session_id, title in found.items():
                _TITLE_CACHE[(source, session_id)] = (now + 15, title)
            if len(_TITLE_CACHE) > 512:
                for stale in list(_TITLE_CACHE)[:-512]:
                    _TITLE_CACHE.pop(stale, None)
    with _CACHE_LOCK:
        return {
            session_id: title
            for session_id in session_ids
            if (entry := _TITLE_CACHE.get((source, session_id))) is not None
            if (title := entry[1]) is not None
        }


def _safe_json(raw: bytes) -> dict[str, Any] | None:
    if not raw or len(raw) > _MAX_RECORD_BYTES:
        return None
    try:
        value = json.loads(raw)
    except (UnicodeDecodeError, json.JSONDecodeError, MemoryError):
        return None
    return value if isinstance(value, dict) else None


def _first_record(path: Path) -> dict[str, Any] | None:
    try:
        with path.open("rb") as stream:
            return _safe_json(stream.readline(_MAX_RECORD_BYTES + 1))
    except OSError:
        return None


def _tail_records(
    path: Path, *, start_offset: int | None = None, cursor_out: list[int] | None = None
) -> Iterable[dict[str, Any]]:
    """Stream bounded tail lines; never materialize the tail or retain message bodies."""

    markers = (
        b'"type":"task_started"',
        b'"type":"task_complete"',
        b'"type":"turn_context"',
        b'"type":"response.completed"',
        b'"type":"token_count"',
        b'"response"',
    )
    try:
        size = path.stat().st_size
        with path.open("rb") as stream:
            start = max(0, size - _TAIL_BYTES) if start_offset is None else min(max(0, start_offset), size)
            stream.seek(start)
            remaining = size - start
            if start and start_offset is None:
                # The seek may land inside a record. Discard that line in
                # bounded pieces before looking for complete metadata.
                while remaining:
                    discarded = stream.readline(min(_MAX_RECORD_BYTES + 1, remaining))
                    if not discarded:
                        return
                    remaining -= len(discarded)
                    if discarded.endswith(b"\n"):
                        break
            if cursor_out is not None:
                cursor_out[0] = stream.tell()
            while remaining:
                line_start = stream.tell()
                raw = stream.readline(min(_MAX_RECORD_BYTES + 1, remaining))
                if not raw:
                    break
                remaining -= len(raw)
                if not raw.endswith(b"\n"):
                    # Keep a partial final line for the next append. Oversized
                    # complete lines can be skipped without retaining content.
                    if len(raw) <= _MAX_RECORD_BYTES:
                        if cursor_out is not None:
                            cursor_out[0] = line_start
                        break
                    while remaining:
                        discarded = stream.readline(min(_MAX_RECORD_BYTES + 1, remaining))
                        if not discarded:
                            break
                        remaining -= len(discarded)
                        if discarded.endswith(b"\n"):
                            break
                    if cursor_out is not None:
                        cursor_out[0] = stream.tell()
                    continue
                if cursor_out is not None:
                    cursor_out[0] = stream.tell()
                raw = raw[:-1]
                if len(raw) > _MAX_RECORD_BYTES or not any(
                    marker in raw for marker in markers
                ):
                    continue
                value = _safe_json(raw)
                if value is not None:
                    yield value
    except OSError:
        return


def _record_at(path: Path, position: int) -> dict[str, Any] | None:
    """Read one compact metadata line around a known marker offset."""

    try:
        start = max(0, position - 65536)
        with path.open("rb") as stream:
            stream.seek(start)
            data = stream.read(_MAX_RECORD_BYTES + 131072)
        relative = position - start
        before = data.rfind(b"\n", 0, relative)
        after = data.find(b"\n", relative)
        line = data[before + 1 : after if after >= 0 else len(data)]
        return _safe_json(line)
    except OSError:
        return None


def _reverse_metadata(path: Path) -> tuple[str | None, dict[str, Any] | None]:
    """Find the latest task state and turn context with bounded reverse reads."""

    task_markers = {
        "running": b'"type":"task_started"',
        "complete": b'"type":"task_complete"',
    }
    turn_marker = b'"type":"turn_context"'
    task_state = None
    task_position = None
    turn_position = None
    overlap = max(len(value) for value in (*task_markers.values(), turn_marker)) - 1
    try:
        size = path.stat().st_size
        end = size
        scanned = 0
        suffix = b""
        with path.open("rb") as stream:
            while (
                end > 0
                and scanned < _MAX_REVERSE_SCAN
                and (task_state is None or turn_position is None)
            ):
                start = max(0, end - _REVERSE_CHUNK)
                stream.seek(start)
                block = stream.read(end - start)
                data = block + suffix
                if task_state is None:
                    found = [
                        (data.rfind(marker), state)
                        for state, marker in task_markers.items()
                        if marker in data
                    ]
                    if found:
                        position, task_state = max(found)
                        task_position = start + position
                if turn_position is None:
                    position = data.rfind(turn_marker)
                    if position >= 0:
                        turn_position = start + position
                suffix = block[:overlap]
                scanned += end - start
                end = start
    except OSError:
        return None, None
    if task_state == "complete" and task_position is not None and turn_position is not None and turn_position > task_position:
        task_state = None
    return task_state, _record_at(
        path, turn_position
    ) if turn_position is not None else None


def _session_projection(
    path: Path,
    fallback_model: str | None,
    mtime_ns: int,
    previous: dict[str, Any] | None = None,
    start_offset: int | None = None,
    cursor_out: list[int] | None = None,
) -> dict[str, Any] | None:
    first = _first_record(path)
    if not first or first.get("type") != "session_meta":
        return None
    meta = first.get("payload") if isinstance(first.get("payload"), dict) else {}
    source = meta.get("source")
    spawn = {}
    if isinstance(source, dict):
        subagent = source.get("subagent")
        if isinstance(subagent, dict) and isinstance(
            subagent.get("thread_spawn"), dict
        ):
            spawn = subagent["thread_spawn"]

    requested_model = fallback_model or (previous or {}).get("requested_model") or ""
    response_model = (previous or {}).get("response_model")
    latest_turn = (
        {
            "turn_id": previous.get("turn_id"),
            "root_turn_id": previous.get("root_turn_id"),
        }
        if previous and previous.get("turn_id")
        else None
    )
    last_task_event = None
    last_usage_at = (previous or {}).get("last_usage_at")
    last_turn_at = (previous or {}).get("last_turn_at")
    if previous and previous.get("status") in ("running", "complete"):
        last_task_event = (
            "task_started" if previous["status"] == "running" else "task_complete"
        )
    for record in _tail_records(path, start_offset=start_offset, cursor_out=cursor_out):
        payload = (
            record.get("payload") if isinstance(record.get("payload"), dict) else {}
        )
        if record.get("type") == "turn_context":
            if last_task_event == "task_complete" and payload.get("turn_id") != (latest_turn or {}).get("turn_id"):
                last_task_event = None
            latest_turn = payload
            last_turn_at = _timestamp(record.get("timestamp"))
            requested_model = str(payload.get("model") or requested_model)
            response_model = None
        if record.get("type") == "event_msg" and payload.get("type") in (
            "task_started",
            "task_complete",
        ):
            last_task_event = payload.get("type")
        if (record.get("type") == "event_msg" and payload.get("type") == "token_count") or record.get("type") == "token_count":
            stamp = _timestamp(record.get("timestamp"))
            if stamp is not None:
                last_usage_at = max(last_usage_at or stamp, stamp)
        response = payload.get("response")
        if isinstance(response, dict) and response.get("model"):
            response_model = str(response["model"])
        elif payload.get("type") in (
            "response.completed",
            "response_complete",
        ) and payload.get("model"):
            response_model = str(payload["model"])

    if previous is None and (last_task_event is None or latest_turn is None):
        fallback_state, fallback_turn = _reverse_metadata(path)
        if last_task_event is None and fallback_state is not None:
            last_task_event = (
                "task_started" if fallback_state == "running" else "task_complete"
            )
        if (
            latest_turn is None
            and fallback_turn
            and fallback_turn.get("type") == "turn_context"
        ):
            payload = fallback_turn.get("payload")
            if isinstance(payload, dict):
                latest_turn = payload
                last_turn_at = _timestamp(fallback_turn.get("timestamp"))
                requested_model = str(payload.get("model") or requested_model)

    latest_turn = latest_turn or {}
    role = "subagent" if spawn else "main"
    name = spawn.get("agent_nickname") or ("主代理" if role == "main" else "子代理")
    requested = requested_model or None
    match = "unknown"
    if requested and response_model:
        match = "match" if requested == response_model else "mismatch"
    return {
        "role": role,
        "name": str(name),
        "agent_path": spawn.get("agent_path"),
        "parent_thread_id": spawn.get("parent_thread_id"),
        "depth": spawn.get("depth"),
        "session_id": meta.get("session_id") or meta.get("id"),
        "turn_id": latest_turn.get("turn_id"),
        "root_turn_id": latest_turn.get("root_turn_id") or latest_turn.get("turn_id"),
        "requested_model": requested,
        "requested_model_short": short_model_name(requested),
        "response_model": response_model,
        "response_model_short": short_model_name(response_model)
        if response_model
        else None,
        "model_match": match,
        "response_evidence": "response.model" if response_model else None,
        "status": "running"
        if last_task_event == "task_started"
        else "complete"
        if last_task_event == "task_complete"
        else "unknown",
        "mtime_ns": int(mtime_ns or 0),
        "last_usage_at": last_usage_at,
        "last_turn_at": last_turn_at,
    }


def _cached_projection(
    path: Path, fallback_model: str | None, mtime_ns: int, size: int
) -> dict[str, Any] | None:
    key = str(path)
    with _CACHE_LOCK:
        cached = _PROJECTION_CACHE.get(key)
        if cached and cached[0] == mtime_ns and cached[1] == size:
            return dict(cached[2]) if cached[2] is not None else None
        previous = (
            dict(cached[2])
            if cached and cached[2] is not None and 0 < size - cached[1] <= _TAIL_BYTES
            else None
        )
        start_offset = cached[3] if previous is not None else None
    cursor = [start_offset or 0]
    value = _session_projection(path, fallback_model, mtime_ns, previous, start_offset, cursor)
    with _CACHE_LOCK:
        _PROJECTION_CACHE[key] = (
            mtime_ns,
            size,
            dict(value) if value is not None else None,
            cursor[0],
        )
        if len(_PROJECTION_CACHE) > 128:
            for stale in list(_PROJECTION_CACHE)[:-128]:
                _PROJECTION_CACHE.pop(stale, None)
    return value


def _offer_candidate(heap: list, item: tuple, limit: int) -> None:
    if limit <= 0:
        return
    if len(heap) < limit:
        heappush(heap, item)
    elif item > heap[0]:
        heapreplace(heap, item)


def _session_role(path: Path) -> tuple[str | None, str | None, str | None]:
    first = _first_record(path)
    if not first or first.get("type") != "session_meta":
        return None, None, None
    payload = first.get("payload")
    if not isinstance(payload, dict):
        return None, None, None
    source = payload.get("source")
    subagent = source.get("subagent") if isinstance(source, dict) else None
    spawn = subagent.get("thread_spawn") if isinstance(subagent, dict) else None
    return (
        "subagent" if isinstance(spawn, dict) else "main",
        payload.get("session_id") or payload.get("id"),
        spawn.get("parent_thread_id") if isinstance(spawn, dict) else None,
    )


def _rank_session_paths(
    paths: Iterable[tuple[Path, int, str | None]] | Callable[[], Iterable[tuple[Path, int, str | None]]],
    limit: int, selected_conversation_id: str | None,
    *, recent_only: bool,
) -> list[tuple[Path, int, str | None]]:
    """Keep bounded main and child candidates independently, so children cannot evict roots."""

    main_heap: list[tuple[int, str, Path, str | None]] = []
    child_heap: list[tuple[int, str, Path, str | None]] = []
    selected_children: list[tuple[int, str, Path, str | None]] = []
    selected_ids = {selected_conversation_id} if selected_conversation_id else set()
    selected_root = None
    cutoff = int((time.time() - 86400) * 1_000_000_000) if recent_only else 0
    source = paths if callable(paths) else lambda: iter(paths)
    for path, mtime_ns, fallback_model in source():
        if mtime_ns < cutoff:
            continue
        role, session_id, parent_id = _session_role(path)
        item = (mtime_ns, str(path), path, fallback_model)
        if role == "main":
            _offer_candidate(main_heap, item, limit)
            if session_id and session_id == selected_conversation_id:
                selected_root = item
        elif role == "subagent":
            _offer_candidate(child_heap, item, max(8, limit * 4))
            if selected_conversation_id and parent_id == selected_conversation_id:
                _offer_candidate(selected_children, item, 128)
                if session_id and len(selected_ids) < 128:
                    selected_ids.add(session_id)
    # The global child heap may evict an older nested agent. Revisit the
    # source only for a selected conversation in a crowded archive; retain a
    # bounded transitive subtree even when descendants precede their parents.
    if selected_root and selected_children and len(child_heap) >= max(8, limit * 4):
        for _depth in range(8):
            before = len(selected_ids)
            for path, mtime_ns, fallback_model in source():
                if mtime_ns < cutoff:
                    continue
                role, session_id, parent_id = _session_role(path)
                if role != "subagent" or parent_id not in selected_ids:
                    continue
                item = (mtime_ns, str(path), path, fallback_model)
                if item not in selected_children:
                    _offer_candidate(selected_children, item, 128)
                if session_id and len(selected_ids) < 128:
                    selected_ids.add(session_id)
            if len(selected_ids) == before or len(selected_ids) >= 128:
                break
    ranked = sorted(main_heap + child_heap + selected_children + ([selected_root] if selected_root else []), reverse=True)
    return list(dict.fromkeys((item[2], item[0], item[3]) for item in ranked))


def _stat_session_paths(paths: Iterable[Path]) -> Iterable[tuple[Path, int, None]]:
    for path in paths:
        try:
            yield path, path.stat().st_mtime_ns, None
        except OSError:
            continue


def _fast_session_paths(root: Path, known: list[Path]) -> Iterable[Path]:
    """Check today's Codex folders and already-seen folders without walking old years."""

    today = datetime.now().date()
    folders = {root}
    for offset in (-1, 0, 1):
        day = today + timedelta(days=offset)
        folders.add(root / f"{day:%Y}" / f"{day:%m}" / f"{day:%d}")
    folders.update(path.parent for path in known)
    for folder in folders:
        try:
            yield from folder.glob("*.jsonl")
        except OSError:
            continue


def _recent_session_files(
    sessions_dir: Path, limit: int, selected_conversation_id: str | None = None,
    *, reuse_root_cache: bool = False,
) -> list[tuple[Path, os.stat_result]]:
    root = Path(sessions_dir).resolve()
    key = os.path.normcase(str(root)) + "\0" + (selected_conversation_id or "")
    now = time.monotonic()
    with _CACHE_LOCK:
        cached = _DISCOVERY_CACHE.get(key)
        borrowed = False
        if cached is None and reuse_root_cache:
            cached = _DISCOVERY_CACHE.get(os.path.normcase(str(root)) + "\0")
            borrowed = cached is not None
    if cached and cached[0] > now and not borrowed:
        paths = cached[2]
    else:
        full_scan = cached is None or now - cached[1] >= 60
        try:
            def source_paths():
                source = root.rglob("*.jsonl") if full_scan else _fast_session_paths(root, cached[2])
                return _stat_session_paths(source)
            paths = [path for path, _mtime, _model in _rank_session_paths(
                source_paths, limit, selected_conversation_id, recent_only=True
            )]
        except OSError:
            paths = []
        with _CACHE_LOCK:
            _DISCOVERY_CACHE[key] = (now + 2.5, now if full_scan else cached[1], paths)
            if len(_DISCOVERY_CACHE) > 16:
                for stale in list(_DISCOVERY_CACHE)[:-16]:
                    _DISCOVERY_CACHE.pop(stale, None)
    result = []
    for path in paths:
        try:
            result.append((path, path.stat()))
        except OSError:
            continue
    return result


def live_model_activity(
    index: Any,
    sessions_dir: Path | None = None,
    max_candidates: int = 24,
    *,
    selected_conversation_id: str | None = None,
    sampler: LiveTelemetrySampler | None = None,
    now: float | None = None,
    _reuse_recent_discovery: bool = False,
) -> dict[str, Any]:
    """Project active root conversations and their own child usage samples."""

    sampler = sampler or DEFAULT_SAMPLER
    now = time.time() if now is None else now
    sampler.expire_idle(now=now)

    rows = []
    if sessions_dir is None and index is not None:
        try:
            with index.read_transaction() as reader:
                query = (
                    """SELECT path,last_model,mtime_ns FROM session_files
                       WHERE missing=0 AND error IS NULL AND mtime_ns>=?"""
                )
                cutoff = int((time.time() - 86400) * 1_000_000_000)
                def indexed_paths():
                    indexed = reader.execute(query, (cutoff,))
                    return ((Path(row["path"]), int(row["mtime_ns"] or 0), row["last_model"]) for row in indexed)
                # SQLite cursors stream rows; do not materialize the whole archive.
                rows = _rank_session_paths(
                    indexed_paths,
                    max_candidates, selected_conversation_id, recent_only=False,
                )
        except Exception:
            rows = []

    candidates: dict[str, dict[str, Any]] = {}
    for row, mtime_ns, fallback_model in rows:
        try:
            stat = row.stat()
        except OSError:
            continue
        candidates[os.path.normcase(str(row))] = {
            "path": str(row), "last_model": fallback_model,
            "mtime_ns": mtime_ns, "size": stat.st_size,
        }
    if sessions_dir is not None:
        for path, stat in _recent_session_files(
            Path(sessions_dir), max_candidates, selected_conversation_id,
            reuse_root_cache=_reuse_recent_discovery,
        ):
            key = os.path.normcase(str(path.resolve()))
            current = candidates.get(key)
            if current is None or stat.st_mtime_ns > current["mtime_ns"]:
                candidates[key] = {
                    "path": str(path),
                    "last_model": current.get("last_model") if current else None,
                    "mtime_ns": stat.st_mtime_ns,
                    "size": stat.st_size,
                }
    rows = sorted(candidates.values(), key=lambda row: row["mtime_ns"], reverse=True)
    if not rows:
        sampler.retain_paths([])
        return {"active": False, "agents": [], "conversations": [], "response_model_available": False}

    projected = []
    for row in rows:
        path = Path(row["path"])
        size = int(row.get("size") or 0)
        item = _cached_projection(
            path, row.get("last_model"), int(row.get("mtime_ns") or 0), size
        )
        if item is not None:
            item["_path"] = path
            projected.append(item)

    mains = [item for item in projected if item["role"] == "main"]
    children = [item for item in projected if item["role"] == "subagent"]
    conversations = []
    members_by_id = {}
    for main in mains:
        root_turn = main.get("root_turn_id") or main.get("turn_id")
        session_id = main.get("session_id")
        related = []
        known_ids = {session_id} if session_id else set()
        remaining = children[:]
        for _ in range(len(children)):
            attached = False
            for item in remaining[:]:
                parent = item.get("parent_thread_id")
                belongs = (parent in known_ids) if parent else bool(
                    root_turn and item.get("root_turn_id") == root_turn
                )
                if not belongs:
                    continue
                related.append(item)
                remaining.remove(item)
                if item.get("session_id"):
                    known_ids.add(item["session_id"])
                attached = True
            if not attached:
                break
        def is_active(item):
            stamp = max(
                (value for value in (item.get("last_usage_at"), item.get("last_turn_at")) if value is not None),
                default=None,
            )
            return item["status"] == "running" or (
                item["status"] == "unknown" and stamp is not None and now - 30 <= stamp <= now + 5
            )

        active = is_active(main) or any(is_active(item) for item in related)
        if not active:
            continue
        related.sort(key=lambda item: (item["status"] != "running", -item["mtime_ns"]))
        members = [main] + related
        agents = [{key: value for key, value in item.items() if key != "_path"} for item in members]
        conversation_id = str(session_id or "local-" + hashlib.sha256(
            str(main["_path"]).encode("utf-8")
        ).hexdigest()[:12])
        members_by_id[conversation_id] = members
        conversations.append({
            "conversation_id": conversation_id,
            "active": active,
            "agents": agents,
            "telemetry": None,
            "last_activity_ns": max(item["mtime_ns"] for item in members),
            "response_model_available": any(item.get("response_model") for item in members),
        })
    conversations.sort(key=lambda item: item["last_activity_ns"], reverse=True)
    titles = _local_thread_titles(
        [row["conversation_id"] for row in conversations], sessions_dir
    )
    for row in conversations:
        row["title"] = titles.get(row["conversation_id"]) or f'对话 {row["conversation_id"][:8]}'
        row["title_source"] = "codex_state" if row["conversation_id"] in titles else "session_id"
    selected = next(
        (row for row in conversations if row["conversation_id"] == selected_conversation_id),
        conversations[0] if conversations else None,
    )
    if selected is not None and selected_conversation_id is None:
        # The first request has no chosen ID yet. Candidate ranking cannot
        # reserve this root's descendants until it knows which active root
        # will be shown, so redo only that initial projection with the ID.
        return live_model_activity(
            index, sessions_dir, max_candidates,
            selected_conversation_id=selected["conversation_id"],
            sampler=sampler, now=now, _reuse_recent_discovery=True,
        )
    if selected is not None:
        members = members_by_id[selected["conversation_id"]]
        sampler.retain_paths([item["_path"] for item in members])
        for visible, item in zip(selected["agents"], members):
            visible["telemetry"] = sampler.sample(item["_path"], now=now, active=is_active(item))
        selected["telemetry"] = sampler.summarize(
            [item["_path"] for item in members], now=now, active=True
        )
    else:
        sampler.retain_paths([])
    return {
        "active": bool(selected),
        "agents": selected["agents"] if selected else [],
        "conversations": conversations,
        "selected_conversation_id": selected["conversation_id"] if selected else None,
        "response_model_available": selected["response_model_available"] if selected else False,
        "evidence_note": "响应模型仅在会话明确记录 response.model 时显示；速率来自调用完成后的用量记录",
    }
