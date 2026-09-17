"""Privacy-preserving live model invocation projection from Codex session metadata."""

from __future__ import annotations

import json
import os
import threading
import time
from heapq import nlargest
from pathlib import Path
from typing import Any, Iterable


_TAIL_BYTES = 4 * 1024 * 1024
_MAX_RECORD_BYTES = 1024 * 1024
_MAX_REVERSE_SCAN = 64 * 1024 * 1024
_REVERSE_CHUNK = 1024 * 1024
_PROJECTION_CACHE: dict[str, tuple[int, int, dict[str, Any] | None]] = {}
_DISCOVERY_CACHE: dict[str, tuple[float, list[Path]]] = {}
_CACHE_LOCK = threading.Lock()


def short_model_name(model: str | None) -> str:
    value = (model or "").strip()
    known = {
        "gpt-6-astra": "Astra",
        "gpt-5.6-sol": "Sol",
        "gpt-5.6-terra": "Terra",
        "gpt-5.6-luna": "Luna",
    }
    return known.get(value, value.removeprefix("gpt-") or "未知")


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


def _tail_records(path: Path) -> Iterable[dict[str, Any]]:
    """Read only small metadata records from a bounded tail; never retain message bodies."""

    markers = (
        b'"type":"task_started"',
        b'"type":"task_complete"',
        b'"type":"turn_context"',
        b'"type":"response.completed"',
        b'"response"',
    )
    try:
        size = path.stat().st_size
        with path.open("rb") as stream:
            start = max(0, size - _TAIL_BYTES)
            stream.seek(start)
            data = stream.read(_TAIL_BYTES)
            if start:
                newline = data.find(b"\n")
                data = data[newline + 1 :] if newline >= 0 else b""
            if data and not data.endswith((b"\n", b"\r")):
                newline = data.rfind(b"\n")
                data = data[: newline + 1] if newline >= 0 else b""
            for raw in data.splitlines():
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
                        _position, task_state = max(found)
                if turn_position is None:
                    position = data.rfind(turn_marker)
                    if position >= 0:
                        turn_position = start + position
                suffix = block[:overlap]
                scanned += end - start
                end = start
    except OSError:
        return None, None
    return task_state, _record_at(
        path, turn_position
    ) if turn_position is not None else None


def _session_projection(
    path: Path,
    fallback_model: str | None,
    mtime_ns: int,
    previous: dict[str, Any] | None = None,
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
    if previous and previous.get("status") in ("running", "complete"):
        last_task_event = (
            "task_started" if previous["status"] == "running" else "task_complete"
        )
    for record in _tail_records(path):
        payload = (
            record.get("payload") if isinstance(record.get("payload"), dict) else {}
        )
        if record.get("type") == "turn_context":
            latest_turn = payload
            requested_model = str(payload.get("model") or requested_model)
            response_model = None
        if record.get("type") == "event_msg" and payload.get("type") in (
            "task_started",
            "task_complete",
        ):
            last_task_event = payload.get("type")
        response = payload.get("response")
        if isinstance(response, dict) and response.get("model"):
            response_model = str(response["model"])
        elif payload.get("type") in (
            "response.completed",
            "response_complete",
        ) and payload.get("model"):
            response_model = str(payload["model"])

    if last_task_event is None or latest_turn is None:
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
            if cached and cached[2] is not None and size >= cached[1]
            else None
        )
    value = _session_projection(path, fallback_model, mtime_ns, previous)
    with _CACHE_LOCK:
        _PROJECTION_CACHE[key] = (
            mtime_ns,
            size,
            dict(value) if value is not None else None,
        )
        if len(_PROJECTION_CACHE) > 64:
            for stale in list(_PROJECTION_CACHE)[:-64]:
                _PROJECTION_CACHE.pop(stale, None)
    return value


def _recent_session_files(
    sessions_dir: Path, limit: int
) -> list[tuple[Path, os.stat_result]]:
    root = str(Path(sessions_dir).resolve())
    now = time.monotonic()
    with _CACHE_LOCK:
        cached = _DISCOVERY_CACHE.get(os.path.normcase(root))
        paths = list(cached[1]) if cached and cached[0] > now else []
    if not paths:
        ranked = []
        try:
            for path in Path(root).rglob("*.jsonl"):
                try:
                    stat = path.stat()
                except OSError:
                    continue
                ranked.append((stat.st_mtime_ns, str(path), path))
        except OSError:
            ranked = []
        paths = [item[2] for item in nlargest(limit, ranked)]
        with _CACHE_LOCK:
            _DISCOVERY_CACHE[os.path.normcase(root)] = (now + 10.0, paths)
    result = []
    for path in paths:
        try:
            result.append((path, path.stat()))
        except OSError:
            continue
    return result


def live_model_activity(
    index: Any, sessions_dir: Path | None = None, max_candidates: int = 16
) -> dict[str, Any]:
    """Return the newest active root turn and related agents without reading chat content."""

    rows = []
    if index is not None:
        try:
            with index.read_transaction() as reader:
                rows = list(
                    reader.execute(
                        """SELECT path,last_model,mtime_ns,size FROM session_files
                           WHERE missing=0 AND error IS NULL ORDER BY mtime_ns DESC LIMIT ?""",
                        (max_candidates * 2,),
                    ).fetchall()
                )
        except Exception:
            rows = []

    candidates: dict[str, dict[str, Any]] = {}
    for row in rows:
        path = str(row["path"])
        try:
            size = int(row["size"] or 0)
        except (KeyError, IndexError, TypeError):
            size = 0
        candidates[os.path.normcase(path)] = {
            "path": path,
            "last_model": row["last_model"],
            "mtime_ns": int(row["mtime_ns"] or 0),
            "size": size,
        }
    if sessions_dir is not None:
        for path, stat in _recent_session_files(Path(sessions_dir), max_candidates * 2):
            key = os.path.normcase(str(path.resolve()))
            current = candidates.get(key)
            if current is None or stat.st_mtime_ns > current["mtime_ns"]:
                candidates[key] = {
                    "path": str(path),
                    "last_model": current.get("last_model") if current else None,
                    "mtime_ns": stat.st_mtime_ns,
                    "size": stat.st_size,
                }
    rows = sorted(candidates.values(), key=lambda row: row["mtime_ns"], reverse=True)[
        :max_candidates
    ]
    if not rows:
        return {"active": False, "agents": [], "response_model_available": False}

    projected = []
    for row in rows:
        path = Path(row["path"])
        size = int(row.get("size") or 0)
        item = _cached_projection(
            path, row.get("last_model"), int(row.get("mtime_ns") or 0), size
        )
        if item is not None:
            projected.append(item)

    active_mains = [
        item
        for item in projected
        if item["role"] == "main" and item["status"] == "running"
    ]
    active_children = [
        item
        for item in projected
        if item["role"] == "subagent" and item["status"] == "running"
    ]
    main = max(active_mains, key=lambda item: item["mtime_ns"], default=None)
    if main is None and active_children:
        child = max(active_children, key=lambda item: item["mtime_ns"])
        parent = child.get("parent_thread_id")
        main = max(
            (
                item
                for item in projected
                if item["role"] == "main" and item.get("session_id") == parent
            ),
            key=lambda item: item["mtime_ns"],
            default=None,
        )
    if main is None:
        return {"active": False, "agents": [], "response_model_available": False}

    root_turn = main.get("root_turn_id") or main.get("turn_id")
    session_id = main.get("session_id")
    children = [
        item
        for item in projected
        if item["role"] == "subagent"
        and (
            item.get("root_turn_id") == root_turn
            or item.get("parent_thread_id") == session_id
        )
    ]
    children.sort(key=lambda item: (item["status"] != "running", -item["mtime_ns"]))
    agents = [main] + children[:8]
    return {
        "active": True,
        "agents": agents,
        "response_model_available": any(item.get("response_model") for item in agents),
        "evidence_note": "响应模型仅在会话明确记录 response.model 时显示",
    }
