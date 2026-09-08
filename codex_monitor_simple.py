#!/usr/bin/env python3
"""
Codex Code Monitor - 终端简洁版
"""

from __future__ import annotations

import argparse
import json
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, Optional

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
    return f"{h}小时{m}分{s}秒"


def _print_section(title: str):
    print("\n" + title)
    print("-" * len(title))


def print_summary(data: Dict[str, Any]):
    source = data.get("source", {})
    total = data.get("total", {})
    five_hour = data.get("five_hour", {})
    rate_limits = data.get("rate_limits", {})

    print("🤖 Codex Code Monitor（本地日志统计）")
    print(f"生成时间: {data.get('generated_at')}")
    print(f"日志来源: {source.get('label', '本地 Codex 会话流')}  (文件数: {source.get('files')})")
    print(f"统计范围: {source.get('scope', '全局工作区')}")
    if source.get("cwd_filter"):
        print("过滤状态: 已启用工作区过滤")

    _print_section("总计")
    print(f"调用次数: {_fmt_int(total.get('calls', 0))}")
    print(f"总Token: {_fmt_int(total.get('total_tokens', 0))}")
    print(f"  输入: {_fmt_int(total.get('input_tokens', 0))}  (缓存: {_fmt_int(total.get('cached_input_tokens', 0))})")
    print(f"  输出: {_fmt_int(total.get('output_tokens', 0))}  (其中推理: {_fmt_int(total.get('reasoning_output_tokens', 0))})")
    print(f"估算费用: {_fmt_usd(total.get('estimated_cost_usd', 0.0))}")

    _print_section("最近5小时")
    print(f"调用次数: {_fmt_int(five_hour.get('calls', 0))}")
    print(f"总Token: {_fmt_int(five_hour.get('total_tokens', 0))}")
    print(f"  输入: {_fmt_int(five_hour.get('input_tokens', 0))}  (缓存: {_fmt_int(five_hour.get('cached_input_tokens', 0))})")
    print(f"  输出: {_fmt_int(five_hour.get('output_tokens', 0))}")
    print(f"估算费用: {_fmt_usd(five_hour.get('estimated_cost_usd', 0.0))}")

    if isinstance(rate_limits, dict) and isinstance(rate_limits.get("primary"), dict):
        primary = rate_limits["primary"]
        _print_section("5小时窗口(来自 rate_limits.primary)")
        print(f"已用百分比: {primary.get('used_percent') if primary.get('used_percent') is not None else '-'}%")
        print(f"窗口时长: {primary.get('window_minutes') if primary.get('window_minutes') is not None else '-'} 分钟")
        print(f"重置时间: {primary.get('resets_at') or '-'}")
        print(f"剩余时间: {_fmt_seconds(primary.get('remaining_seconds'))}")

    by_model = data.get("by_model", {})
    if isinstance(by_model, dict) and by_model:
        _print_section("按模型统计（按 Token 排序）")
        rows = []
        for model, stats in by_model.items():
            rows.append((model, int(stats.get("total_tokens", 0)), stats))
        rows.sort(key=lambda x: x[1], reverse=True)

        for model, _, stats in rows[:20]:
            calls = int(stats.get("calls", 0))
            tokens = int(stats.get("total_tokens", 0))
            avg = tokens / calls if calls else 0.0
            print(f"- {model}: calls={calls}, tokens={_fmt_int(tokens)}, avg={avg:.1f}, cost={_fmt_usd(stats.get('estimated_cost_usd', 0.0))}")

    by_cwd = data.get("by_cwd", {})
    if isinstance(by_cwd, dict) and by_cwd:
        _print_section("Top 工作目录（按 Token 排序）")
        rows = []
        for cwd, stats in by_cwd.items():
            rows.append((cwd, int(stats.get("total_tokens", 0)), stats))
        rows.sort(key=lambda x: x[1], reverse=True)
        for cwd, _, stats in rows[:10]:
            print(f"- {cwd}: calls={_fmt_int(stats.get('calls', 0))}, tokens={_fmt_int(stats.get('total_tokens', 0))}")

    print("\n提示: " + str(data.get("note", "")).strip())


def main():
    parser = argparse.ArgumentParser(description="Codex Code Monitor（解析 ~/.codex/sessions 的 token_count 统计）")
    parser.add_argument("--sessions-dir", default=None, help="会话日志目录（默认：~/.codex/sessions）")
    parser.add_argument("--config", default=None, help="配置文件路径（默认：~/.codex/monitor_config.json 或 $CODEX_MONITOR_CONFIG）")
    parser.add_argument("--cwd", default=None, help="仅统计该目录(含子目录)下的会话（按 session_meta/turn_context 的 cwd 过滤）")
    parser.add_argument("--json", dest="json_out", default=None, help="导出 JSON 到指定文件")

    args = parser.parse_args()

    sessions_dir = Path(args.sessions_dir).expanduser() if args.sessions_dir else default_codex_sessions_dir()
    config_path = Path(args.config).expanduser() if args.config else None
    cfg = MonitorConfig.load(config_path)

    summary = build_usage_summary(
        sessions_dir=sessions_dir,
        config=cfg,
        cwd_filter=args.cwd,
        now=datetime.now(),
    )

    print_summary(summary)

    if args.json_out:
        out_path = Path(args.json_out).expanduser()
        out_path.parent.mkdir(parents=True, exist_ok=True)
        with open(out_path, "w", encoding="utf-8") as f:
            json.dump(summary, f, ensure_ascii=False, indent=2)
        print(f"\n已导出: {out_path}")


if __name__ == "__main__":
    main()
