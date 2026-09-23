"""Run a built EXE against isolated fake Codex sessions (no real user data)."""

import json
import os
import socket
import subprocess
import sys
import tempfile
import time
import urllib.request
from datetime import datetime, timezone
from pathlib import Path


def write_records(path, records):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "".join(json.dumps(item, separators=(",", ":")) + "\n" for item in records),
        encoding="utf-8",
    )


def main(exe):
    exe = Path(exe).resolve(strict=True)
    with tempfile.TemporaryDirectory(prefix="codex-glass-exe-telemetry-") as directory:
        root = Path(directory)
        sessions = root / "home" / "sessions"
        stamp = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

        def usage(input_tokens, cached, output):
            return {"timestamp": stamp, "type": "event_msg", "payload": {
                "type": "token_count", "info": {"total_token_usage": {
                    "input_tokens": input_tokens,
                    "cached_input_tokens": cached,
                    "output_tokens": output,
                    "total_tokens": input_tokens + output,
                }}
            }}

        write_records(sessions / "main.jsonl", [
            {"type": "session_meta", "payload": {"session_id": "smoke-main"}},
            {"type": "turn_context", "payload": {"turn_id": "smoke-turn", "model": "gpt-6-astra"}},
            {"type": "event_msg", "payload": {"type": "task_started"}},
            usage(100, 20, 60),
        ])
        write_records(sessions / "child.jsonl", [
            {"type": "session_meta", "payload": {"session_id": "smoke-child", "source": {
                "subagent": {"thread_spawn": {"parent_thread_id": "smoke-main", "agent_nickname": "Smoke"}}
            }}},
            {"type": "turn_context", "payload": {
                "turn_id": "smoke-child-turn", "root_turn_id": "smoke-turn", "model": "gpt-5.6-sol"
            }},
            {"type": "event_msg", "payload": {"type": "task_started"}},
            usage(100, 50, 120),
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
            assert data["server"]["version"] == "4.3.1", data["server"]
            assert len(conversations) == 1, conversations
            result = conversations[0]
            assert result["conversation_id"] == "smoke-main", result
            assert len(result["agents"]) == 2, result
            assert result["telemetry"]["output_tps"] == 3.0, result["telemetry"]
            assert result["telemetry"]["cache_hit_percent"] == 35.0, result["telemetry"]
            assert data["total"]["total_tokens"] == 380, data["total"]
            capture = root / "exe-capture.png"
            desktop = subprocess.run(
                [str(exe), "--url", url, "--no-start-backend", "--capture", str(capture)],
                env=environment, creationflags=flags, timeout=70, check=True,
            )
            assert desktop.returncode == 0 and capture.stat().st_size > 1000
            print("EXE smoke OK: v4.3.1, 380 tokens, 3 tok/s, 35% cache, 2 agents, Qt capture")
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
