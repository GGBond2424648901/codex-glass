"""Run a built EXE against isolated fake Codex sessions (no real user data)."""

import json
import os
import socket
import subprocess
import sys
import tempfile
import time
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path


def write_records(path, records):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "".join(json.dumps(item, separators=(",", ":")) + "\n" for item in records),
        encoding="utf-8",
    )


def append_records(path, records):
    with path.open("a", encoding="utf-8") as stream:
        for item in records:
            stream.write(json.dumps(item, separators=(",", ":")) + "\n")


def main(exe):
    exe = Path(exe).resolve(strict=True)
    with tempfile.TemporaryDirectory(prefix="codex-glass-exe-telemetry-") as directory:
        root = Path(directory)
        sessions = root / "home" / "sessions"
        initial_stamp = (datetime.now(timezone.utc) - timedelta(seconds=10)).strftime("%Y-%m-%dT%H:%M:%SZ")

        def usage(input_tokens, cached, output, *, at=None):
            return {"timestamp": at or datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"), "type": "event_msg", "payload": {
                "type": "token_count", "info": {"total_token_usage": {
                    "input_tokens": input_tokens,
                    "cached_input_tokens": cached,
                    "output_tokens": output,
                    "total_tokens": input_tokens + output,
                }}
            }}

        write_records(sessions / "main.jsonl", [
            {"type": "session_meta", "payload": {"session_id": "smoke-main"}},
            {"type": "turn_context", "payload": {"turn_id": "smoke-turn", "model": "gpt-6-sol"}},
            {"type": "event_msg", "payload": {"type": "task_started"}},
            usage(100, 20, 60, at=initial_stamp),
        ])
        write_records(sessions / "child.jsonl", [
            {"type": "session_meta", "payload": {"session_id": "smoke-child", "source": {
                "subagent": {"thread_spawn": {"parent_thread_id": "smoke-main", "agent_nickname": "Smoke"}}
            }}},
            {"type": "turn_context", "payload": {
                "turn_id": "smoke-child-turn", "root_turn_id": "smoke-turn", "model": "gpt-6-luna"
            }},
            {"type": "event_msg", "payload": {"type": "task_started"}},
            usage(100, 50, 120, at=initial_stamp),
        ])
        with socket.socket() as probe:
            probe.bind(("127.0.0.1", 0))
            port = probe.getsockname()[1]
        url = f"http://127.0.0.1:{port}"
        environment = os.environ.copy()
        environment["CODEX_HOME"] = str(root / "home")
        environment.pop("QT_QPA_PLATFORM", None)
        flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
        command = [
            str(exe), "--backend", "--no-browser", "--host", "127.0.0.1", "--port", str(port),
            "--sessions-dir", str(sessions), "--index-db", str(root / "index.sqlite3"),
            "--update-interval", "0.5",
        ]
        backend = subprocess.Popen(command, env=environment, creationflags=flags)
        try:
            opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
            deadline = time.monotonic() + 60
            data = None
            while time.monotonic() < deadline:
                if backend.poll() is not None:
                    raise RuntimeError(f"EXE backend exited early: {backend.returncode}")
                try:
                    with opener.open(url + "/api/widget", timeout=2) as response:
                        data = json.load(response)
                    conversations = data.get("model_activity", {}).get("conversations", [])
                    if conversations and data.get("by_model"):
                        break
                except (OSError, json.JSONDecodeError):
                    pass
                time.sleep(0.3)
            else:
                raise RuntimeError(f"EXE backend did not return usage: {data and data.get('index')}")
            assert data["pricing"]["unpriced_calls"] == 0, data["pricing"]
            assert abs(data["total"]["estimated_cost_usd"] - 0.0008295) < 1e-6, data["total"]
            assert data["server"]["version"] == "4.3.4", data["server"]
            assert len(conversations) == 1, conversations
            result = conversations[0]
            assert result["conversation_id"] == "smoke-main", result
            assert len(result["agents"]) == 2, result
            assert result["telemetry"]["cache_hit_percent"] == 35.0, result["telemetry"]
            assert data["total"]["total_tokens"] == 380, data["total"]
            append_records(sessions / "main.jsonl", [usage(150, 30, 90)])
            append_records(sessions / "child.jsonl", [usage(200, 100, 180)])
            with opener.open(url + "/api/live-activity?conversation_id=smoke-main", timeout=2) as response:
                live = json.load(response)
            assert live["conversations"][0]["telemetry"]["output_tps"] == 30.0, live
            today = datetime.now().astimezone()
            new_session = sessions / f"{today:%Y}" / f"{today:%m}" / f"{today:%d}" / "new-conversation.jsonl"
            write_records(new_session, [
                {"type": "session_meta", "payload": {"session_id": "smoke-new"}},
                {"timestamp": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"), "type": "turn_context", "payload": {
                    "turn_id": "smoke-new-turn", "model": "gpt-6-sol"
                }},
                usage(100, 20, 120, at=initial_stamp),
            ])
            deadline = time.monotonic() + 8
            while time.monotonic() < deadline:
                with opener.open(url + "/api/widget?conversation_id=smoke-main", timeout=2) as response:
                    data = json.load(response)
                conversations = data.get("model_activity", {}).get("conversations", [])
                if {row["conversation_id"] for row in conversations} == {"smoke-main", "smoke-new"}:
                    break
                time.sleep(0.4)
            else:
                raise AssertionError("new conversation was not discovered while EXE remained open")
            activity = data["model_activity"]
            assert activity["selected_conversation_id"] == "smoke-main", activity
            by_id = {row["conversation_id"]: row for row in conversations}
            assert by_id["smoke-new"]["telemetry"] is None, by_id["smoke-new"]
            append_records(new_session, [usage(150, 30, 240)])
            with opener.open(url + "/api/live-activity?conversation_id=smoke-new", timeout=2) as response:
                switched = json.load(response)
            by_id = {row["conversation_id"]: row for row in switched["conversations"]}
            assert switched["selected_conversation_id"] == "smoke-new", switched
            assert by_id["smoke-new"]["telemetry"]["output_tps"] == 40.0, by_id["smoke-new"]
            assert by_id["smoke-main"]["telemetry"] is None, by_id["smoke-main"]
            with opener.open(url + "/api/quota", timeout=2) as response:
                quota = json.load(response)
            assert "rate_limits" in quota and "model_activity" not in quota, quota
            capture = root / "exe-capture.png"
            desktop = subprocess.run(
                [str(exe), "--url", url, "--no-start-backend", "--capture", str(capture)],
                env=environment, creationflags=flags, timeout=70, check=True,
            )
            assert desktop.returncode == 0 and capture.stat().st_size > 1000
            print("EXE smoke OK: v4.3.4, 1s live endpoint, new conversation switched, selected-only telemetry, Qt capture")
        finally:
            if backend.poll() is None:
                # The Windows one-file bootloader owns a child process. Stop only
                # this exact test process tree so the temporary SQLite can close.
                subprocess.run(
                    ["taskkill", "/PID", str(backend.pid), "/T", "/F"],
                    creationflags=flags, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                    check=False,
                )
                backend.wait(timeout=8)


if __name__ == "__main__":
    main(sys.argv[1])
