#!/usr/bin/env python3
"""
Codex Code Monitor - Web 仪表板（标准库实现）

默认端口 8081，避免与 Claude Monitor(8080) 冲突。
"""

from __future__ import annotations

import argparse
import copy
import json
import os
import threading
import time
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse, parse_qs

from codex_glass.cli.main import configure_console_output
from codex_glass.core.usage import (
    DEFAULT_HOST,
    DEFAULT_PORT,
    MonitorConfig,
    build_usage_summary,
    default_codex_sessions_dir,
)
from codex_glass.storage.index import IndexCoordinator, IndexStatus, SessionIndex, default_index_path, history_page
from codex_glass.resources import dashboard_build_identity, resource_path


_DASHBOARD_BUILD = dashboard_build_identity()


HTML_PAGE = resource_path("assets", "web", "dashboard.html").read_text(encoding="utf-8")


class Handler(BaseHTTPRequestHandler):
    def _send(self, status: int, body: bytes, content_type: str = "text/plain; charset=utf-8"):
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Cache-Control", "no-cache")
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        parsed = urlparse(self.path)
        if parsed.path == "/":
            self._send(200, HTML_PAGE.encode("utf-8"), "text/html; charset=utf-8")
            return

        if parsed.path == "/healthz":
            self._send(200, b"ok\n", "text/plain; charset=utf-8")
            return

        if parsed.path == "/api/widget":
            from codex_glass.core.presentation import desktop_snapshot

            data = desktop_snapshot(self.server.coordinator)
            self._send(200, json.dumps(data, ensure_ascii=False).encode("utf-8"), "application/json; charset=utf-8")
            return

        if parsed.path == "/api/data":
            data = self.server.coordinator.snapshot()
            data.setdefault(
                "server",
                {
                    "pid": os.getpid(),
                    "build": _DASHBOARD_BUILD,
                },
            )
            self._send(200, json.dumps(data, ensure_ascii=False).encode("utf-8"), "application/json; charset=utf-8")
            return

        if parsed.path == "/api/index-status":
            status = self.server.coordinator.snapshot()["index"]
            self._send(200, json.dumps(status, ensure_ascii=False).encode("utf-8"), "application/json; charset=utf-8")
            return

        if parsed.path == "/api/native-context":
            import ipaddress
            import sqlite3

            host = urlparse("http://" + self.headers.get("Host", "")).hostname
            if (
                not ipaddress.ip_address(self.client_address[0]).is_loopback
                or host not in ("127.0.0.1", "localhost", "::1")
                or self.headers.get("Origin")
            ):
                self._send(403, b"local native client only")
                return
            index = getattr(self.server.coordinator, "index", None)
            data = {"index_path": None, "sources": []}
            if index is not None:
                data["index_path"] = str(index.path.resolve())
                try:
                    with index.read_transaction() as reader:
                        data["sources"] = [
                            dict(row)
                            for row in reader.execute(
                                "SELECT source_id,first_imported_at,source_files,source_usage_events,source_rate_limit_snapshots FROM history_imports ORDER BY first_imported_at"
                            )
                        ]
                except (RuntimeError, sqlite3.Error):
                    pass
            self._send(200, json.dumps(data).encode(), "application/json; charset=utf-8")
            return

        if parsed.path == "/api/events":
            qs = parse_qs(parsed.query or "")
            q = (qs.get("q", [None])[0] or "").strip()
            model = (qs.get("model", [None])[0] or "").strip()
            cwd = (qs.get("cwd", [None])[0] or "").strip()

            try:
                offset = int(qs.get("offset", ["0"])[0] or 0)
            except Exception:
                offset = 0
            try:
                limit = int(qs.get("limit", ["200"])[0] or 200)
            except Exception:
                limit = 200

            offset = max(0, offset)
            limit = max(1, min(500, limit))

            filters = {}
            for name in ("since", "until", "sort"):
                if name in qs:
                    filters[name] = qs[name][0]
            if "models" in qs:
                filters["models"] = [m for item in qs["models"] for m in item.split(",") if m]
            if qs.get("empty_models") == ["1"]:
                filters["models"] = []
            if "descending" in qs:
                filters["descending"] = qs["descending"][0] != "0"
            try:
                payload = self.server.coordinator.events_page(
                    offset=offset, limit=limit, q=q, model=model, cwd=cwd, **filters
                )
            except ValueError:
                self._send(400, b"invalid history date")
                return
            self._send(200, json.dumps(payload, ensure_ascii=False).encode("utf-8"), "application/json; charset=utf-8")
            return

        self._send(404, b"not found\n")

    def do_POST(self):
        parsed = urlparse(self.path)
        if parsed.path != "/api/refresh":
            self._send(404, b"not found\n")
            return

        self.server.coordinator.request_refresh()
        self._send(202, b"accepted\n")


class LegacyCoordinator:
    """Diagnostic fallback using the original full-file parser and a short cache."""

    def __init__(self, sessions_dir, config_loader, cwd_filter, interval=3.0):
        self.sessions_dir = sessions_dir
        self.config_loader = config_loader
        self.cwd_filter = cwd_filter
        self.interval = max(0.01, float(interval))
        self._lock = threading.RLock()
        self._updated_at = 0.0
        self._summary = None

    def start(self):
        pass

    def stop(self, timeout=None):
        return True

    def request_refresh(self):
        with self._lock:
            self._updated_at = 0.0

    def snapshot(self):
        with self._lock:
            if self._summary is None or time.monotonic() - self._updated_at >= self.interval:
                self._summary = build_usage_summary(
                    self.sessions_dir, self.config_loader(), self.cwd_filter, include_events=True
                )
                self._summary["index"] = IndexStatus(
                    "ready",
                    True,
                    self._summary["source"]["files"],
                    self._summary["source"]["files"],
                    0,
                    0,
                    None,
                    None,
                    None,
                    None,
                    time.time(),
                ).to_payload()
                self._updated_at = time.monotonic()
            return copy.deepcopy({key: value for key, value in self._summary.items() if key != "events"})

    def events_snapshot(self):
        with self._lock:
            self.snapshot()
            return copy.deepcopy(self._summary.get("events", []))

    def events_page(self, offset=0, limit=200, q="", model="", cwd="", **filters):
        with self._lock:
            self.snapshot()
            events, error = self._summary.get("events", []), self._summary["index"].get("last_error")
        return history_page(events, error, offset, limit, q, model, cwd, **filters)


def create_server(host: str, port: int, coordinator) -> ThreadingHTTPServer:
    """Bind before starting background indexing; the caller owns shutdown."""
    server = ThreadingHTTPServer((host, port), Handler)
    server.daemon_threads = True
    server.coordinator = coordinator
    try:
        coordinator.start()
    except Exception:
        server.server_close()
        raise
    return server


def main():
    configure_console_output()
    parser = argparse.ArgumentParser(description="Codex Code Monitor - Web 仪表板")
    parser.add_argument("--host", default=None, help=f"监听地址（默认 {DEFAULT_HOST}）")
    parser.add_argument("--port", type=int, default=None, help=f"监听端口（默认 {DEFAULT_PORT}）")
    parser.add_argument("--no-browser", action="store_true", help="不自动打开浏览器")
    parser.add_argument("--sessions-dir", default=None, help="会话日志目录（默认：~/.codex/sessions）")
    parser.add_argument("--index-db", default=None, help="索引数据库路径（默认 ~/.codex/codex-monitor.sqlite3）")
    parser.add_argument("--rebuild-index", action="store_true", help="清空并重建指定索引")
    parser.add_argument("--no-index", action="store_true", help="使用原始日志解析（诊断模式，忽略索引选项）")
    parser.add_argument(
        "--config", default=None, help="配置文件路径（默认：~/.codex/monitor_config.json 或 $CODEX_MONITOR_CONFIG）"
    )
    parser.add_argument("--cwd", default=None, help="仅统计该目录(含子目录)下的会话")
    parser.add_argument("--update-interval", type=float, default=3.0, help="后台更新间隔（秒）")
    args = parser.parse_args()

    sessions_dir = Path(args.sessions_dir).expanduser() if args.sessions_dir else default_codex_sessions_dir()
    config_path = Path(args.config).expanduser() if args.config else None
    cfg = MonitorConfig.load(config_path)

    host = args.host or cfg.host or DEFAULT_HOST
    port = args.port or cfg.port or DEFAULT_PORT

    index = None
    coordinator = None
    server = None
    try:
        if args.no_index:
            coordinator = LegacyCoordinator(
                sessions_dir, lambda: MonitorConfig.load(config_path), args.cwd, interval=args.update_interval
            )
        else:
            index_path = (Path(args.index_db).expanduser() if args.index_db else default_index_path()).resolve()
            index = SessionIndex(index_path)
            coordinator = IndexCoordinator(
                index,
                sessions_dir,
                lambda: MonitorConfig.load(config_path),
                args.cwd,
                interval=args.update_interval,
                rebuild_index=args.rebuild_index,
            )
        server = create_server(host, int(port), coordinator)
        url = f"http://{host}:{server.server_port}"
        print(f"✅ Web 仪表板已启动：{url}")
        print("🛰️  数据源: 本机 Codex 会话遥测（路径已隐藏）")
        if args.cwd:
            print("🔎 过滤范围: 已启用工作区过滤（路径已隐藏）")
        if not args.no_browser:
            try:
                webbrowser.open(url)
            except Exception:
                pass
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n已停止。")
    finally:
        if server is not None:
            server.server_close()
        # Discovery and aggregation may outlast a bounded shutdown timeout.
        # Keep the database available until the worker has fully terminated.
        if coordinator is None or coordinator.stop(timeout=None):
            if index is not None:
                index.close()


if __name__ == "__main__":
    main()
