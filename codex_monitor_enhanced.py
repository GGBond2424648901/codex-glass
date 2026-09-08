#!/usr/bin/env python3
"""
Codex Code Monitor - 增强版终端监控

使用 Rich 库提供更美观的终端界面
特性：
- 彩色输出
- 表格显示
- 进度条
- 实时刷新
"""

from __future__ import annotations

import argparse
import time
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, Optional

try:
    from rich.console import Console
    from rich.table import Table
    from rich.progress import Progress, BarColumn, TextColumn, TimeRemainingColumn
    from rich.panel import Panel
    from rich.live import Live
    from rich.layout import Layout
    from rich.text import Text
    from rich import box
    RICH_AVAILABLE = True
except ImportError:
    RICH_AVAILABLE = False
    print("⚠️  Rich 库未安装，使用基础界面")
    print("   安装命令: pip install rich")

from codex_monitor_core import MonitorConfig, build_usage_summary, default_codex_sessions_dir


def _fmt_int(n: Any) -> str:
    try:
        return f"{int(n):,}"
    except Exception:
        return str(n)


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


def create_summary_table(data: Dict[str, Any]) -> Table:
    """创建汇总表格"""
    table = Table(show_header=False, box=box.ROUNDED, expand=True)
    table.add_column("Metric", style="cyan", width=20)
    table.add_column("Value", style="green")

    source = data.get("source", {})
    total = data.get("total", {})
    five_hour = data.get("five_hour", {})

    table.add_row("生成时间", data.get("generated_at", "-"))
    table.add_row("日志来源", str(source.get("label", "本地 Codex 会话流")))
    table.add_row("统计范围", str(source.get("scope", "全局工作区")))
    table.add_row("文件数量", str(source.get("files", 0)))
    if source.get("cwd_filter"):
        table.add_row("过滤状态", "已启用工作区过滤")

    table.add_section()
    table.add_row("累计调用次数", _fmt_int(total.get("calls", 0)))
    table.add_row("累计总令牌", _fmt_int(total.get("total_tokens", 0)))
    table.add_row("累计输入令牌", _fmt_int(total.get("input_tokens", 0)))
    table.add_row("缓存令牌", _fmt_int(total.get("cached_input_tokens", 0)))
    table.add_row("累计输出令牌", _fmt_int(total.get("output_tokens", 0)))
    table.add_row("累计费用", _fmt_usd(total.get("estimated_cost_usd", 0.0)), style="yellow")

    table.add_section()
    table.add_row("5小时调用次数", _fmt_int(five_hour.get("calls", 0)))
    table.add_row("5小时令牌", _fmt_int(five_hour.get("total_tokens", 0)))
    table.add_row("5小时费用", _fmt_usd(five_hour.get("estimated_cost_usd", 0.0)), style="yellow")

    return table


def create_rate_limit_panel(data: Dict[str, Any]) -> Panel:
    """创建速率限制面板"""
    rate_limits = data.get("rate_limits", {})
    if not isinstance(rate_limits, dict) or not isinstance(rate_limits.get("primary"), dict):
        return Panel("[dim]无速率限制数据[/dim]", title="📊 5小时窗口", border_style="dim")

    primary = rate_limits["primary"]
    used_percent = primary.get("used_percent")
    remaining = primary.get("remaining_seconds")
    window_minutes = primary.get("window_minutes")
    used_percent_text = f"{used_percent}%" if used_percent is not None else "-"
    window_text = f"{window_minutes} 分钟" if window_minutes else "-"

    content = "\n".join(
        [
            f"[cyan]已用百分比:[/] {used_percent_text}",
            f"[cyan]窗口时长:[/] {window_text}",
            f"[cyan]重置时间:[/] {primary.get('resets_at') or '-'}",
            f"[cyan]剩余时间:[/] {_fmt_seconds(remaining)}",
        ]
    )

    return Panel(content, title="📊 5小时窗口", border_style="blue")


def create_model_table(data: Dict[str, Any], top_n: int = 10) -> Table:
    """创建模型统计表格"""
    table = Table(title=f"🤖 模型统计 (Top {top_n})", box=box.ROUNDED)
    table.add_column("模型", style="cyan", no_wrap=False)
    table.add_column("调用次数", style="green", justify="right")
    table.add_column("总令牌", style="yellow", justify="right")
    table.add_column("平均令牌", style="magenta", justify="right")
    table.add_column("费用(USD)", style="red", justify="right")

    by_model = data.get("by_model", {})
    if not isinstance(by_model, dict):
        return table

    rows = []
    for model, stats in by_model.items():
        calls = int(stats.get("calls", 0))
        tokens = int(stats.get("total_tokens", 0))
        avg = tokens / calls if calls else 0.0
        cost = float(stats.get("estimated_cost_usd", 0.0))
        rows.append((model, calls, tokens, avg, cost))

    rows.sort(key=lambda x: x[2], reverse=True)

    for model, calls, tokens, avg, cost in rows[:top_n]:
        table.add_row(
            model,
            _fmt_int(calls),
            _fmt_int(tokens),
            f"{avg:.1f}",
            _fmt_usd(cost)
        )

    return table


def create_cwd_table(data: Dict[str, Any], top_n: int = 8) -> Table:
    """创建工作目录表格"""
    table = Table(title=f"📁 工作目录 (Top {top_n})", box=box.ROUNDED)
    table.add_column("目录", style="cyan", no_wrap=True, max_width=60)
    table.add_column("调用次数", style="green", justify="right")
    table.add_column("总令牌", style="yellow", justify="right")

    by_cwd = data.get("by_cwd", {})
    if not isinstance(by_cwd, dict):
        return table

    rows = []
    for cwd, stats in by_cwd.items():
        calls = int(stats.get("calls", 0))
        tokens = int(stats.get("total_tokens", 0))
        rows.append((cwd, calls, tokens))

    rows.sort(key=lambda x: x[2], reverse=True)

    for cwd, calls, tokens in rows[:top_n]:
        table.add_row(
            cwd,
            _fmt_int(calls),
            _fmt_int(tokens)
        )

    return table


def create_recent_calls_table(data: Dict[str, Any], count: int = 8) -> Table:
    """创建最近调用表格"""
    table = Table(title=f"🕒 最近调用 (最新 {count} 条)", box=box.ROUNDED)
    table.add_column("时间", style="cyan", no_wrap=True)
    table.add_column("模型", style="green", no_wrap=True)
    table.add_column("令牌", style="yellow", justify="right")
    table.add_column("费用", style="red", justify="right")

    recent = data.get("recent_calls", [])
    if not isinstance(recent, list):
        return table

    for item in recent[:count]:
        ts = item.get("timestamp", "-")
        model = item.get("model", "-")
        tokens = _fmt_int(item.get("total_tokens", 0))
        cost = _fmt_usd(item.get("estimated_cost_usd", 0.0))
        table.add_row(ts, model, tokens, cost)

    return table


def render_enhanced(console: Console, data: Dict[str, Any]):
    """使用 Rich 渲染增强界面"""
    layout = Layout()

    # 分割布局
    layout.split_column(
        Layout(name="header", size=3),
        Layout(name="body"),
        Layout(name="footer", size=3)
    )

    # 头部
    header_text = Text()
    header_text.append("🤖 Codex Code Monitor", style="bold cyan")
    header_text.append(" - 增强版", style="dim")
    layout["header"].update(Panel(header_text, style="on blue"))

    # 主体
    layout["body"].split_row(
        Layout(name="left", ratio=1),
        Layout(name="right", ratio=2)
    )

    layout["left"].split_column(
        Layout(name="summary"),
        Layout(name="rate_limit")
    )

    layout["right"].split_column(
        Layout(name="models"),
        Layout(name="bottom")
    )

    layout["bottom"].split_row(
        Layout(name="cwd"),
        Layout(name="recent")
    )

    # 填充内容
    layout["left"]["summary"].update(create_summary_table(data))
    layout["left"]["rate_limit"].update(create_rate_limit_panel(data))
    layout["right"]["models"].update(create_model_table(data, top_n=10))
    layout["right"]["bottom"]["cwd"].update(create_cwd_table(data, top_n=6))
    layout["right"]["bottom"]["recent"].update(create_recent_calls_table(data, count=6))

    # 页脚
    note = data.get("note", "")
    footer_text = Text()
    footer_text.append("💡 ", style="yellow")
    footer_text.append(note, style="dim")
    layout["footer"].update(Panel(footer_text, border_style="dim"))

    console.print(layout)


def render_basic(data: Dict[str, Any]):
    """基础渲染（当 Rich 不可用时）"""
    print("\n" + "="*80)
    print("🤖 Codex Code Monitor - 终端监控")
    print("="*80)

    source = data.get("source", {})
    total = data.get("total", {})
    five_hour = data.get("five_hour", {})

    print(f"\n⏰  生成时间: {data.get('generated_at')}")
    print(f"📁 日志来源: {source.get('label', '本地 Codex 会话流')} (文件: {source.get('files')})")
    print(f"🛰️ 统计范围: {source.get('scope', '全局工作区')}")
    if source.get("cwd_filter"):
        print("🔍 过滤状态: 已启用工作区过滤")

    print("\n📊 累计统计")
    print(f"  调用次数: { _fmt_int(total.get('calls', 0))}")
    print(f"  总令牌:   {_fmt_int(total.get('total_tokens', 0))}")
    print(f"  输入:     {_fmt_int(total.get('input_tokens', 0))} (缓存: {_fmt_int(total.get('cached_input_tokens', 0))})")
    print(f"  输出:     {_fmt_int(total.get('output_tokens', 0))}")
    print(f"  费用:     {_fmt_usd(total.get('estimated_cost_usd', 0.0))}")

    print("\n📈 最近5小时")
    print(f"  调用次数: {_fmt_int(five_hour.get('calls', 0))}")
    print(f"  总令牌:   {_fmt_int(five_hour.get('total_tokens', 0))}")
    print(f"  费用:     {_fmt_usd(five_hour.get('estimated_cost_usd', 0.0))}")

    by_model = data.get("by_model", {})
    if isinstance(by_model, dict) and by_model:
        print("\n🤖 模型统计 (Top 10)")
        rows = []
        for model, stats in by_model.items():
            rows.append((model, int(stats.get("total_tokens", 0)), stats))
        rows.sort(key=lambda x: x[1], reverse=True)

        for model, _, stats in rows[:10]:
            calls = int(stats.get("calls", 0))
            tokens = int(stats.get("total_tokens", 0))
            avg = tokens / calls if calls else 0.0
            print(f"  - {model}: calls={calls}, tokens={_fmt_int(tokens)}, avg={avg:.1f}, cost={_fmt_usd(stats.get('estimated_cost_usd', 0.0))}")

    print("\n" + "="*80)


def main():
    parser = argparse.ArgumentParser(description="Codex Code Monitor - 增强版终端监控")
    parser.add_argument("--sessions-dir", default=None, help="会话日志目录（默认：~/.codex/sessions）")
    parser.add_argument("--config", default=None, help="配置文件路径（默认：~/.codex/monitor_config.json）")
    parser.add_argument("--cwd", default=None, help="仅统计该目录(含子目录)下的会话")
    parser.add_argument("--interval", type=float, default=2.0, help="刷新间隔（秒）")
    parser.add_argument("--once", action="store_true", help="只显示一次后退出")
    args = parser.parse_args()

    sessions_dir = Path(args.sessions_dir).expanduser() if args.sessions_dir else default_codex_sessions_dir()
    config_path = Path(args.config).expanduser() if args.config else None
    cfg = MonitorConfig.load(config_path)

    if args.once:
        data = build_usage_summary(
            sessions_dir=sessions_dir,
            config=cfg,
            cwd_filter=args.cwd,
            now=datetime.now(),
        )
        if RICH_AVAILABLE:
            console = Console()
            render_enhanced(console, data)
        else:
            render_basic(data)
        return

    # 实时监控模式
    if RICH_AVAILABLE:
        console = Console()
        try:
            with Live(console=console, refresh_per_second=1) as live:
                while True:
                    data = build_usage_summary(
                        sessions_dir=sessions_dir,
                        config=cfg,
                        cwd_filter=args.cwd,
                        now=datetime.now(),
                    )

                    layout = Layout()
                    layout.split_column(
                        Layout(name="header", size=3),
                        Layout(name="body")
                    )

                    header_text = Text()
                    header_text.append("🤖 Codex Code Monitor", style="bold cyan")
                    header_text.append(" - 实时监控", style="dim")
                    layout["header"].update(Panel(header_text, style="on blue"))

                    layout["body"].split_row(
                        Layout(name="left", ratio=1),
                        Layout(name="right", ratio=2)
                    )

                    layout["left"].split_column(
                        Layout(name="summary"),
                        Layout(name="rate_limit")
                    )

                    layout["right"].split_column(
                        Layout(name="models"),
                        Layout(name="bottom")
                    )

                    layout["bottom"].split_row(
                        Layout(name="cwd"),
                        Layout(name="recent")
                    )

                    layout["left"]["summary"].update(create_summary_table(data))
                    layout["left"]["rate_limit"].update(create_rate_limit_panel(data))
                    layout["right"]["models"].update(create_model_table(data, top_n=10))
                    layout["right"]["bottom"]["cwd"].update(create_cwd_table(data, top_n=6))
                    layout["right"]["bottom"]["recent"].update(create_recent_calls_table(data, count=6))

                    live.update(layout)
                    time.sleep(max(0.5, float(args.interval)))
        except KeyboardInterrupt:
            console.print("\n✅ 已退出监控", style="green")
    else:
        try:
            import sys
            while True:
                data = build_usage_summary(
                    sessions_dir=sessions_dir,
                    config=cfg,
                    cwd_filter=args.cwd,
                    now=datetime.now(),
                )
                # 清屏（跨平台）
                print("\033[2J\033[H", end="")
                render_basic(data)
                time.sleep(max(0.5, float(args.interval)))
        except KeyboardInterrupt:
            print("\n✅ 已退出监控")


if __name__ == "__main__":
    main()
