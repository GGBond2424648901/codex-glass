#!/usr/bin/env python3
"""
Codex Code Monitor - 终端实时刷新版（无第三方依赖）
"""

from __future__ import annotations

import argparse
import time
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, Optional

from codex_monitor_core import MonitorConfig, build_usage_summary, default_codex_sessions_dir


def _fmt_int(v: Any) -> str:
    try:
        return f"{int(v):,}"
    except Exception:
        return str(v)


def _fmt_usd(v: Any) -> str:
    try:
        return f"${float(v):.6f}"
    except Exception:
        return str(v)


def _fmt_seconds(seconds: Optional[int]) -> str:
    if seconds is None:
        return "-"
    seconds = max(0, int(seconds))
    h, rem = divmod(seconds, 3600)
    m, s = divmod(rem, 60)
    return f"{h}h {m}m {s}s"


def _clear():
    print("\033[2J\033[H", end="")


def render(data: Dict[str, Any]):
    total = data.get("total", {})
    five_hour = data.get("five_hour", {})
    rate_limits = data.get("rate_limits", {}) or {}

    print("Codex Code Monitor (Realtime)")
    print(f"更新时间: {data.get('generated_at')}")
    print()

    print(f"总调用: {_fmt_int(total.get('calls', 0))}  总Token: {_fmt_int(total.get('total_tokens', 0))}  估算费用: {_fmt_usd(total.get('estimated_cost_usd', 0.0))}")
    print(f"  输入: {_fmt_int(total.get('input_tokens', 0))} (缓存 {_fmt_int(total.get('cached_input_tokens', 0))})  输出: {_fmt_int(total.get('output_tokens', 0))}")
    print()

    print(f"最近5小时: 调用 {_fmt_int(five_hour.get('calls', 0))}  Token {_fmt_int(five_hour.get('total_tokens', 0))}  估算费用 {_fmt_usd(five_hour.get('estimated_cost_usd', 0.0))}")

    primary = rate_limits.get("primary") if isinstance(rate_limits, dict) else None
    if isinstance(primary, dict):
        used_percent = primary.get("used_percent")
        used_str = f"{used_percent}%" if used_percent is not None else "-"
        print(f"5小时窗口: 已用 {used_str}  重置 {primary.get('resets_at') or '-'}  剩余 {_fmt_seconds(primary.get('remaining_seconds'))}")

    print("\nTop 模型:")
    by_model = data.get("by_model", {}) or {}
    rows = []
    if isinstance(by_model, dict):
        for model, stats in by_model.items():
            rows.append((model, int(stats.get("total_tokens", 0)), int(stats.get("calls", 0)), float(stats.get("estimated_cost_usd", 0.0))))
    rows.sort(key=lambda x: x[1], reverse=True)
    for model, tokens, calls, cost in rows[:8]:
        avg = tokens / calls if calls else 0.0
        print(f"- {model}: calls={calls}, tokens={_fmt_int(tokens)}, avg={avg:.1f}, cost={_fmt_usd(cost)}")

    print("\n最近调用:")
    recent = data.get("recent_calls", []) or []
    for item in (recent[:8] if isinstance(recent, list) else []):
        ts = item.get("timestamp")
        model = item.get("model")
        tok = item.get("total_tokens")
        print(f"- {ts}  {model}  tokens={_fmt_int(tok)}")

    note = str(data.get("note", "")).strip()
    if note:
        print("\n" + note)


def main():
    parser = argparse.ArgumentParser(description="Codex Code Monitor - 实时终端监控")
    parser.add_argument("--sessions-dir", default=None, help="会话日志目录（默认：~/.codex/sessions）")
    parser.add_argument("--config", default=None, help="配置文件路径（默认：~/.codex/monitor_config.json 或 $CODEX_MONITOR_CONFIG）")
    parser.add_argument("--cwd", default=None, help="仅统计该目录(含子目录)下的会话")
    parser.add_argument("--interval", type=float, default=2.0, help="刷新间隔（秒）")
    args = parser.parse_args()

    sessions_dir = Path(args.sessions_dir).expanduser() if args.sessions_dir else default_codex_sessions_dir()
    config_path = Path(args.config).expanduser() if args.config else None
    cfg = MonitorConfig.load(config_path)

    try:
        while True:
            data = build_usage_summary(
                sessions_dir=sessions_dir,
                config=cfg,
                cwd_filter=args.cwd,
                now=datetime.now(),
            )
            _clear()
            render(data)
            time.sleep(max(0.5, float(args.interval)))
    except KeyboardInterrupt:
        print("\n已退出。")


if __name__ == "__main__":
    main()

