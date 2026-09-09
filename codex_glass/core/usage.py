#!/usr/bin/env python3
"""
Codex Code Monitor - 核心解析与统计模块

数据来源：~/.codex/sessions/**.jsonl（Codex CLI 本地会话日志）

统计口径：
- 以 token_count.info.total_token_usage 的“累积计数”做差得到“单次模型调用增量”，避免重复 token_count 事件双计数。
- total_tokens 通常等于 input_tokens + output_tokens；cached_input_tokens / reasoning_output_tokens 为子集信息。
- 费用为“估算值”，按配置的每百万 token 单价计算（input / cached_input / output）。
"""

from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple


DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 8081
PRICING_POLICY_VERSION = "2026-09-08-standard-text"
PRICING_POLICY_CHECKED_ON = "2026-09-08"


BUILTIN_PRICING_PER_MILLION: Dict[str, "PricingRatesPerMillion"] = {}
BUILTIN_MODEL_ALIASES: Dict[str, str] = {}


def _safe_float(value: Any, default: float = 0.0) -> float:
    try:
        return float(value)
    except Exception:
        return default


def _safe_int(value: Any, default: int = 0) -> int:
    try:
        return int(value)
    except Exception:
        return default


def parse_timestamp_local(timestamp_str: str) -> datetime:
    """
    解析日志里的 timestamp（通常为 ISO8601 + Z），转换为本地时间（naive）。
    """
    if not timestamp_str:
        return datetime.now()

    try:
        if timestamp_str.endswith("Z"):
            utc_dt = datetime.fromisoformat(timestamp_str.replace("Z", "+00:00"))
            return utc_dt.astimezone(tz=None).replace(tzinfo=None)

        # 兼容不带时区的 ISO 字符串
        return datetime.fromisoformat(timestamp_str.replace("Z", ""))
    except Exception:
        return datetime.now()


def default_codex_home() -> Path:
    custom = os.environ.get("CODEX_HOME", "").strip()
    return Path(custom).expanduser().resolve() if custom else Path.home() / ".codex"


def default_codex_sessions_dir() -> Path:
    return default_codex_home() / "sessions"


def default_config_path() -> Path:
    return default_codex_home() / "monitor_config.json"


def _workspace_label(index: int) -> str:
    return f"工作区 {index:02d}"


def _build_workspace_aliases(events: List["UsageEvent"]) -> Dict[str, str]:
    raw_paths = sorted({str(event.cwd) for event in events if isinstance(event.cwd, str) and event.cwd.strip()})
    return {raw_path: _workspace_label(i + 1) for i, raw_path in enumerate(raw_paths)}


def _display_cwd(raw_cwd: Optional[str], aliases: Dict[str, str]) -> str:
    if isinstance(raw_cwd, str) and raw_cwd.strip():
        return aliases.get(raw_cwd, "工作区")
    return "未标记工作区"


@dataclass(frozen=True)
class PricingRatesPerMillion:
    input: float
    cached_input: float
    output: float

    @staticmethod
    def from_mapping(mapping: Dict[str, Any]) -> "PricingRatesPerMillion":
        return PricingRatesPerMillion(
            input=_safe_float(mapping.get("input", 0.0)),
            cached_input=_safe_float(mapping.get("cached_input", 0.0)),
            output=_safe_float(mapping.get("output", 0.0)),
        )


# 内置 OpenAI 官方定价（来源：https://developers.openai.com/api/docs/pricing，核对日期：2026-09-08）
# 说明：默认内置为 Text tokens 的 Standard tier（USD / 1M tokens）。
# 对 Astra 与 GPT-5.4/5.5/5.6，会额外处理官方 long context (>272K input tokens) 加价规则。
# Codex CLI 的 model id 可能为别名，会通过 alias/规则映射到这些定价键。
BUILTIN_PRICING_PER_MILLION = {
    # Current Codex models
    "gpt-6-astra": PricingRatesPerMillion(input=10.0, cached_input=1.0, output=50.0),
    "gpt-5.6-sol": PricingRatesPerMillion(input=4.0, cached_input=0.4, output=20.0),
    "gpt-5.6-terra": PricingRatesPerMillion(input=2.0, cached_input=0.2, output=12.0),
    "gpt-5.6-luna": PricingRatesPerMillion(input=0.2, cached_input=0.02, output=1.2),
    "gpt-5.5": PricingRatesPerMillion(input=5.0, cached_input=0.5, output=30.0),
    # GPT-5.4.x
    "gpt-5.4": PricingRatesPerMillion(input=2.5, cached_input=0.25, output=15.0),
    "gpt-5.4-pro": PricingRatesPerMillion(input=30.0, cached_input=30.0, output=180.0),
    "gpt-5.4-mini": PricingRatesPerMillion(input=0.75, cached_input=0.075, output=4.5),
    "gpt-5.4-nano": PricingRatesPerMillion(input=0.2, cached_input=0.02, output=1.25),
    "gpt-5.4-chat-latest": PricingRatesPerMillion(input=2.5, cached_input=0.25, output=15.0),
    # GPT-5.x
    "gpt-5.2": PricingRatesPerMillion(input=1.75, cached_input=0.175, output=14.0),
    "gpt-5.1": PricingRatesPerMillion(input=1.25, cached_input=0.125, output=10.0),
    "gpt-5": PricingRatesPerMillion(input=1.25, cached_input=0.125, output=10.0),
    "gpt-5-mini": PricingRatesPerMillion(input=0.25, cached_input=0.025, output=2.0),
    "gpt-5-nano": PricingRatesPerMillion(input=0.05, cached_input=0.005, output=0.4),
    "gpt-5.2-chat-latest": PricingRatesPerMillion(input=1.75, cached_input=0.175, output=14.0),
    "gpt-5.1-chat-latest": PricingRatesPerMillion(input=1.25, cached_input=0.125, output=10.0),
    "gpt-5-chat-latest": PricingRatesPerMillion(input=1.25, cached_input=0.125, output=10.0),
    "gpt-5.1-codex-max": PricingRatesPerMillion(input=1.25, cached_input=0.125, output=10.0),
    "gpt-5.1-codex": PricingRatesPerMillion(input=1.25, cached_input=0.125, output=10.0),
    "gpt-5-codex": PricingRatesPerMillion(input=1.25, cached_input=0.125, output=10.0),
    "gpt-5.1-codex-mini": PricingRatesPerMillion(input=0.25, cached_input=0.025, output=2.0),
    "codex-mini-latest": PricingRatesPerMillion(input=1.5, cached_input=0.375, output=6.0),
    # cached input 为 '-'：按“无缓存折扣”处理（cached_input = input）
    "gpt-5.2-pro": PricingRatesPerMillion(input=21.0, cached_input=21.0, output=168.0),
    "gpt-5-pro": PricingRatesPerMillion(input=15.0, cached_input=15.0, output=120.0),
    # GPT-4.x / 4o
    "gpt-4.1": PricingRatesPerMillion(input=2.0, cached_input=0.5, output=8.0),
    "gpt-4.1-mini": PricingRatesPerMillion(input=0.4, cached_input=0.1, output=1.6),
    "gpt-4.1-nano": PricingRatesPerMillion(input=0.1, cached_input=0.025, output=0.4),
    "gpt-4o": PricingRatesPerMillion(input=2.5, cached_input=1.25, output=10.0),
    "gpt-4o-2024-05-13": PricingRatesPerMillion(input=5.0, cached_input=5.0, output=15.0),
    "gpt-4o-mini": PricingRatesPerMillion(input=0.15, cached_input=0.075, output=0.6),
    # Realtime / Audio
    "gpt-realtime": PricingRatesPerMillion(input=4.0, cached_input=0.4, output=16.0),
    "gpt-realtime-1.5": PricingRatesPerMillion(input=32.0, cached_input=0.4, output=64.0),
    "gpt-realtime-mini": PricingRatesPerMillion(input=10.0, cached_input=0.3, output=20.0),
    "gpt-4o-realtime-preview": PricingRatesPerMillion(input=5.0, cached_input=2.5, output=20.0),
    "gpt-4o-mini-realtime-preview": PricingRatesPerMillion(input=0.6, cached_input=0.3, output=2.4),
    "gpt-audio": PricingRatesPerMillion(input=2.5, cached_input=2.5, output=10.0),
    "gpt-audio-1.5": PricingRatesPerMillion(input=32.0, cached_input=32.0, output=64.0),
    "gpt-audio-mini": PricingRatesPerMillion(input=0.6, cached_input=0.6, output=2.4),
    "gpt-4o-audio-preview": PricingRatesPerMillion(input=2.5, cached_input=2.5, output=10.0),
    "gpt-4o-mini-audio-preview": PricingRatesPerMillion(input=0.15, cached_input=0.15, output=0.6),
    # o-series
    "o1": PricingRatesPerMillion(input=15.0, cached_input=7.5, output=60.0),
    "o1-pro": PricingRatesPerMillion(input=150.0, cached_input=150.0, output=600.0),
    "o1-mini": PricingRatesPerMillion(input=1.1, cached_input=0.55, output=4.4),
    "o3": PricingRatesPerMillion(input=2.0, cached_input=0.5, output=8.0),
    "o3-pro": PricingRatesPerMillion(input=20.0, cached_input=20.0, output=80.0),
    "o3-deep-research": PricingRatesPerMillion(input=10.0, cached_input=2.5, output=40.0),
    "o3-mini": PricingRatesPerMillion(input=1.1, cached_input=0.55, output=4.4),
    "o4-mini": PricingRatesPerMillion(input=1.1, cached_input=0.275, output=4.4),
    "o4-mini-deep-research": PricingRatesPerMillion(input=2.0, cached_input=0.5, output=8.0),
    # Search / Tools
    "gpt-5-search-api": PricingRatesPerMillion(input=1.25, cached_input=0.125, output=10.0),
    "gpt-4o-mini-search-preview": PricingRatesPerMillion(input=0.15, cached_input=0.15, output=0.6),
    "gpt-4o-search-preview": PricingRatesPerMillion(input=2.5, cached_input=2.5, output=10.0),
    "computer-use-preview": PricingRatesPerMillion(input=3.0, cached_input=3.0, output=12.0),
    # Image (text tokens only; output 为 '-' 表示不按 text output token 计费)
    "gpt-image-1": PricingRatesPerMillion(input=5.0, cached_input=1.25, output=0.0),
    "gpt-image-1.5": PricingRatesPerMillion(input=8.0, cached_input=2.0, output=32.0),
    "gpt-image-1-mini": PricingRatesPerMillion(input=2.5, cached_input=0.25, output=8.0),
}

BUILTIN_MODEL_ALIASES = {
    # Codex CLI 常见别名
    "gpt-5.4-codex": "gpt-5.4",
    "gpt-5.4-codex-mini": "gpt-5.4-mini",
    "gpt-5.4-codex-nano": "gpt-5.4-nano",
    "gpt-5.4-codex-pro": "gpt-5.4-pro",
    "gpt-5.4-max": "gpt-5.4",
    "gpt-5.1-codex-max": "gpt-5.1",
    "gpt-5.1-codex": "gpt-5.1",
    "gpt-5.1-codex-mini": "gpt-5-mini",
    "gpt-5-codex": "gpt-5",
}


@dataclass(frozen=True)
class MonitorConfig:
    host: str
    port: int
    pricing_per_million: Dict[str, PricingRatesPerMillion]
    model_aliases: Dict[str, str]

    @staticmethod
    def load(path: Optional[Path] = None) -> "MonitorConfig":
        env_path = os.environ.get("CODEX_MONITOR_CONFIG")
        config_path = path or (Path(env_path) if env_path else default_config_path())

        host = DEFAULT_HOST
        port = DEFAULT_PORT
        # 先加载内置定价，再允许配置文件覆盖。
        pricing: Dict[str, PricingRatesPerMillion] = dict(BUILTIN_PRICING_PER_MILLION)
        aliases: Dict[str, str] = dict(BUILTIN_MODEL_ALIASES)

        try:
            if config_path and Path(config_path).exists():
                with open(config_path, "r", encoding="utf-8") as f:
                    raw = json.load(f) or {}

                web = raw.get("web", {}) if isinstance(raw.get("web", {}), dict) else {}
                host = str(web.get("host", host))
                port = _safe_int(web.get("port", port), port)

                raw_pricing = raw.get("pricing_per_million", {})
                if isinstance(raw_pricing, dict):
                    for model_name, model_rates in raw_pricing.items():
                        if isinstance(model_rates, dict):
                            pricing[str(model_name)] = PricingRatesPerMillion.from_mapping(model_rates)

                raw_aliases = raw.get("model_aliases", {})
                if isinstance(raw_aliases, dict):
                    for k, v in raw_aliases.items():
                        if isinstance(k, str) and isinstance(v, str) and k and v:
                            aliases[k] = v
        except Exception:
            # 配置文件坏了也不影响使用：继续用默认值
            pass

        return MonitorConfig(host=host, port=port, pricing_per_million=pricing, model_aliases=aliases)

    def rates_for_model(self, model: str) -> Tuple[PricingRatesPerMillion, str]:
        """
        返回 (单价, 来源标识)。
        来源标识用于调试：direct / alias / heuristic / default / unpriced
        """
        if model in self.pricing_per_million:
            return self.pricing_per_million[model], "direct"

        alias = self.model_aliases.get(model)
        if alias and alias in self.pricing_per_million:
            return self.pricing_per_million[alias], f"alias:{alias}"

        canonical = canonicalize_model_id(model)
        if canonical and canonical in self.pricing_per_million:
            return self.pricing_per_million[canonical], f"heuristic:{canonical}"

        if "default" in self.pricing_per_million:
            return self.pricing_per_million["default"], "default"
        return PricingRatesPerMillion(input=0.0, cached_input=0.0, output=0.0), "unpriced"


def canonicalize_model_id(model: str) -> Optional[str]:
    """
    将 Codex CLI 里出现的模型 id 归一到 OpenAI 定价键（尽量保守）。
    """
    if not model:
        return None

    m = str(model).strip().lower()

    # Published dated snapshots preserve the exact priced family. Do not use a
    # broad GPT-5 prefix here: new variants must remain unpriced until mapped.
    for canonical in ("gpt-6-astra", "gpt-5.6-sol", "gpt-5.6-terra", "gpt-5.6-luna", "gpt-5.5"):
        if re.fullmatch(rf"{re.escape(canonical)}-\d{{4}}-\d{{2}}-\d{{2}}", m):
            return canonical
    if m.startswith(("gpt-5.5", "gpt-5.6")):
        return None

    # GPT-5.4.x
    if m.startswith("gpt-5.4"):
        if "pro" in m:
            return "gpt-5.4-pro"
        if "mini" in m:
            return "gpt-5.4-mini"
        if "nano" in m:
            return "gpt-5.4-nano"
        return "gpt-5.4"

    # GPT-5.x
    if m.startswith("gpt-5") and "mini" in m:
        return "gpt-5-mini"
    if m.startswith("gpt-5") and "nano" in m:
        return "gpt-5-nano"
    if m.startswith("gpt-5.2"):
        if "pro" in m:
            return "gpt-5.2-pro"
        return "gpt-5.2"
    if m.startswith("gpt-5.1"):
        return "gpt-5.1"
    if m == "gpt-5":
        return "gpt-5"

    # Codex / Max 等：尽量按版本归一（可在 model_aliases 覆盖）
    if m.startswith("gpt-5") and ("codex" in m or "max" in m):
        if "5.4" in m:
            if "pro" in m:
                return "gpt-5.4-pro"
            if "mini" in m:
                return "gpt-5.4-mini"
            if "nano" in m:
                return "gpt-5.4-nano"
            return "gpt-5.4"
        if "5.2" in m:
            return "gpt-5.2"
        if "5.1" in m:
            return "gpt-5.1"
        return "gpt-5"


def _apply_long_context_pricing(
    model: str,
    input_total: int,
    rates: PricingRatesPerMillion,
    source: str,
) -> Tuple[PricingRatesPerMillion, str]:
    if source == "direct":
        pricing_key = model
    elif source.startswith("alias:") or source.startswith("heuristic:"):
        pricing_key = source.split(":", 1)[1]
    else:
        pricing_key = None
    if pricing_key not in {
        "gpt-6-astra",
        "gpt-5.6-sol",
        "gpt-5.6-terra",
        "gpt-5.6-luna",
        "gpt-5.5",
        "gpt-5.4",
        "gpt-5.4-pro",
    }:
        return rates, source
    if input_total <= 272_000:
        return rates, source

    adjusted = PricingRatesPerMillion(
        input=rates.input * 2.0,
        cached_input=rates.cached_input * 2.0,
        output=rates.output * 1.5,
    )
    return adjusted, f"{source}+long-context"


@dataclass
class UsageDelta:
    input_tokens: int = 0
    cached_input_tokens: int = 0
    output_tokens: int = 0
    reasoning_output_tokens: int = 0
    total_tokens: int = 0

    @staticmethod
    def from_total_usage(total_usage: Dict[str, Any]) -> "UsageDelta":
        return UsageDelta(
            input_tokens=_safe_int(total_usage.get("input_tokens", 0)),
            cached_input_tokens=_safe_int(total_usage.get("cached_input_tokens", 0)),
            output_tokens=_safe_int(total_usage.get("output_tokens", 0)),
            reasoning_output_tokens=_safe_int(total_usage.get("reasoning_output_tokens", 0)),
            total_tokens=_safe_int(total_usage.get("total_tokens", 0)),
        )

    def diff(self, previous: "UsageDelta") -> "UsageDelta":
        return UsageDelta(
            input_tokens=self.input_tokens - previous.input_tokens,
            cached_input_tokens=self.cached_input_tokens - previous.cached_input_tokens,
            output_tokens=self.output_tokens - previous.output_tokens,
            reasoning_output_tokens=self.reasoning_output_tokens - previous.reasoning_output_tokens,
            total_tokens=self.total_tokens - previous.total_tokens,
        )


@dataclass
class RateLimitSnapshot:
    limit_id: Optional[str] = None
    limit_name: Optional[str] = None
    observed_at: Optional[datetime] = None
    used_percent: Optional[float] = None
    window_minutes: Optional[int] = None
    resets_at: Optional[datetime] = None
    resets_in_seconds: Optional[int] = None

    @staticmethod
    def from_payload(timestamp_local: datetime, rate_limits: Dict[str, Any]) -> "RateLimitSnapshot":
        primary = rate_limits.get("primary", {}) if isinstance(rate_limits, dict) else {}
        used_percent = primary.get("used_percent")
        window_minutes = primary.get("window_minutes")

        resets_at_dt: Optional[datetime] = None
        resets_in_seconds: Optional[int] = None

        if isinstance(primary, dict):
            if "resets_at" in primary:
                resets_at_epoch = primary.get("resets_at")
                try:
                    resets_at_dt = (
                        datetime.fromtimestamp(float(resets_at_epoch), tz=timezone.utc)
                        .astimezone(tz=None)
                        .replace(tzinfo=None)
                    )
                except Exception:
                    resets_at_dt = None
            if "resets_in_seconds" in primary:
                resets_in_seconds = _safe_int(primary.get("resets_in_seconds", None), 0)

        # 如果只有 resets_in_seconds，可以估算 resets_at（以当前事件时间为基准）
        if resets_at_dt is None and resets_in_seconds is not None and resets_in_seconds > 0:
            resets_at_dt = timestamp_local + timedelta(seconds=resets_in_seconds)

        return RateLimitSnapshot(
            limit_id=str(rate_limits.get("limit_id")) if rate_limits.get("limit_id") is not None else None,
            limit_name=str(rate_limits.get("limit_name")) if rate_limits.get("limit_name") is not None else None,
            observed_at=timestamp_local,
            used_percent=_safe_float(used_percent, None) if used_percent is not None else None,
            window_minutes=_safe_int(window_minutes, None) if window_minutes is not None else None,
            resets_at=resets_at_dt,
            resets_in_seconds=resets_in_seconds if resets_in_seconds and resets_in_seconds > 0 else None,
        )


def _should_replace_rate_limit(existing: Optional[RateLimitSnapshot], candidate: RateLimitSnapshot) -> bool:
    if existing is None:
        return True

    existing_scope_rank = 1 if (existing.limit_id or "").strip().lower() == "codex" else 0
    candidate_scope_rank = 1 if (candidate.limit_id or "").strip().lower() == "codex" else 0
    if candidate_scope_rank != existing_scope_rank:
        return candidate_scope_rank > existing_scope_rank

    existing_reset = existing.resets_at or datetime.min
    candidate_reset = candidate.resets_at or datetime.min
    if existing.limit_id == candidate.limit_id and candidate_reset != existing_reset:
        return candidate_reset > existing_reset

    existing_reset = existing.resets_at or datetime.min
    candidate_reset = candidate.resets_at or datetime.min
    if candidate_reset != existing_reset and existing_scope_rank == candidate_scope_rank:
        return candidate_reset > existing_reset

    existing_used = float(existing.used_percent) if existing.used_percent is not None else -1.0
    candidate_used = float(candidate.used_percent) if candidate.used_percent is not None else -1.0
    if candidate_used != existing_used:
        return candidate_used > existing_used

    existing_observed = existing.observed_at or datetime.min
    candidate_observed = candidate.observed_at or datetime.min
    if candidate_observed != existing_observed:
        return candidate_observed > existing_observed

    if candidate.resets_at is not None and existing.resets_at is None:
        return True
    return False


def _rate_limit_scope(snapshot: RateLimitSnapshot) -> str:
    if snapshot.limit_id == "codex" or not snapshot.limit_name:
        return "global"
    return "model"


def _rate_limit_payload(snapshot: RateLimitSnapshot, current_time: datetime) -> Dict[str, Any]:
    remaining_seconds = None
    if snapshot.resets_at is not None:
        remaining_seconds = max(0, int((snapshot.resets_at - current_time).total_seconds()))
    elif snapshot.resets_in_seconds is not None:
        remaining_seconds = max(0, int(snapshot.resets_in_seconds))

    return {
        "limit_id": snapshot.limit_id,
        "limit_name": snapshot.limit_name,
        "used_percent": snapshot.used_percent,
        "window_minutes": snapshot.window_minutes,
        "resets_at": snapshot.resets_at.isoformat(sep=" ", timespec="seconds") if snapshot.resets_at else None,
        "remaining_seconds": remaining_seconds,
        "observed_at": snapshot.observed_at.isoformat(sep=" ", timespec="seconds") if snapshot.observed_at else None,
        "scope": _rate_limit_scope(snapshot),
    }


def _floor_to_bucket(dt: datetime, minutes: int) -> datetime:
    bucket = max(1, int(minutes))
    floored_minute = (dt.minute // bucket) * bucket
    return dt.replace(minute=floored_minute, second=0, microsecond=0)


def _safe_ratio(numerator: Any, denominator: Any, digits: int = 4) -> float:
    try:
        numerator_f = float(numerator)
        denominator_f = float(denominator)
        if denominator_f <= 0:
            return 0.0
        return round(numerator_f / denominator_f, digits)
    except Exception:
        return 0.0


@dataclass(slots=True)
class UsageEvent:
    timestamp: datetime
    model: str
    cwd: Optional[str]
    delta: UsageDelta
    estimated_cost_usd: float
    pricing_source: str


def _path_matches_filter(candidate: Optional[str], cwd_filter: Optional[str]) -> bool:
    if not cwd_filter:
        return True
    if not candidate:
        return False
    try:
        candidate_path = Path(candidate).resolve()
        filter_path = Path(cwd_filter).resolve()
        return candidate_path == filter_path or filter_path in candidate_path.parents
    except Exception:
        return False


def _clamped_token_buckets(delta: UsageDelta) -> Tuple[int, int, int, int]:
    # cached_input_tokens 是 input_tokens 的子集，按“未缓存输入 + 缓存输入”分别计价
    input_total = max(0, delta.input_tokens)
    cached = min(input_total, max(0, delta.cached_input_tokens))
    uncached = max(0, input_total - cached)
    output = max(0, delta.output_tokens)
    return input_total, cached, uncached, output


def estimate_cost_usd(model: str, delta: UsageDelta, config: MonitorConfig) -> Tuple[float, str]:
    rates, source = config.rates_for_model(model)
    input_total, cached, uncached, output = _clamped_token_buckets(delta)
    rates, source = _apply_long_context_pricing(model, input_total, rates, source)

    cost = 0.0
    cost += uncached * rates.input / 1_000_000
    cost += cached * rates.cached_input / 1_000_000
    cost += output * rates.output / 1_000_000
    return cost, source


def iter_session_files(sessions_dir: Path) -> List[Path]:
    if not sessions_dir.exists():
        return []
    files = [p for p in sessions_dir.glob("**/*.jsonl") if p.is_file()]
    # 按路径排序，便于稳定输出；后续会再按事件时间排序
    files.sort()
    return files


def parse_usage_events_from_session_file(
    file_path: Path,
    config: MonitorConfig,
    cwd_filter: Optional[str] = None,
) -> Tuple[List[UsageEvent], Optional[RateLimitSnapshot]]:
    events: List[UsageEvent] = []
    rate_limit_snapshots: Dict[str, RateLimitSnapshot] = {}

    current_model: str = "unknown"
    current_cwd: Optional[str] = None

    prev_total: Optional[UsageDelta] = None

    try:
        with open(file_path, "r", encoding="utf-8") as f:
            for line in f:
                if not line:
                    continue

                # 只解析关键信息，避免处理巨大 encrypted_content 行
                if '"type":"session_meta"' in line:
                    try:
                        obj = json.loads(line)
                        payload = obj.get("payload", {}) if isinstance(obj, dict) else {}
                        cwd = payload.get("cwd")
                        if isinstance(cwd, str) and cwd:
                            current_cwd = cwd
                    except Exception:
                        pass
                    continue

                if '"type":"turn_context"' in line:
                    try:
                        obj = json.loads(line)
                        payload = obj.get("payload", {}) if isinstance(obj, dict) else {}
                        model = payload.get("model")
                        cwd = payload.get("cwd")
                        if isinstance(model, str) and model:
                            current_model = model
                        if isinstance(cwd, str) and cwd:
                            current_cwd = cwd
                    except Exception:
                        pass
                    continue

                if '"type":"token_count"' not in line:
                    continue

                try:
                    obj = json.loads(line)
                except Exception:
                    continue

                if not isinstance(obj, dict):
                    continue

                ts_local = parse_timestamp_local(str(obj.get("timestamp", "")))
                payload = obj.get("payload", {}) if isinstance(obj.get("payload", {}), dict) else {}

                # rate_limits 也可能存在于 info 为 null 的 token_count 事件里
                rl = payload.get("rate_limits")
                if isinstance(rl, dict):
                    snapshot = RateLimitSnapshot.from_payload(ts_local, rl)
                    snapshot_key = snapshot.limit_id or "global"
                    current_snapshot = rate_limit_snapshots.get(snapshot_key)
                    if _should_replace_rate_limit(current_snapshot, snapshot):
                        rate_limit_snapshots[snapshot_key] = snapshot

                info = payload.get("info")
                if not isinstance(info, dict):
                    continue

                total_usage = info.get("total_token_usage")
                if not isinstance(total_usage, dict):
                    continue

                current_total = UsageDelta.from_total_usage(total_usage)
                if prev_total is None:
                    delta = current_total
                else:
                    delta = current_total.diff(prev_total)
                prev_total = current_total

                if delta.total_tokens <= 0:
                    continue

                if not _path_matches_filter(current_cwd, cwd_filter):
                    continue

                cost, pricing_source = estimate_cost_usd(current_model, delta, config)
                events.append(
                    UsageEvent(
                        timestamp=ts_local,
                        model=current_model,
                        cwd=current_cwd,
                        delta=delta,
                        estimated_cost_usd=cost,
                        pricing_source=pricing_source,
                    )
                )
    except FileNotFoundError:
        return [], None
    except Exception:
        # 单个文件坏了不影响整体
        primary_snapshot = None
        if "codex" in rate_limit_snapshots:
            primary_snapshot = rate_limit_snapshots["codex"]
        elif rate_limit_snapshots:
            ranked = sorted(
                rate_limit_snapshots.values(),
                key=lambda snapshot: (
                    1 if _rate_limit_scope(snapshot) == "global" else 0,
                    snapshot.used_percent or 0.0,
                    snapshot.observed_at or datetime.min,
                ),
                reverse=True,
            )
            primary_snapshot = ranked[0]
        return events, primary_snapshot

    primary_snapshot = None
    if "codex" in rate_limit_snapshots:
        primary_snapshot = rate_limit_snapshots["codex"]
    elif rate_limit_snapshots:
        ranked = sorted(
            rate_limit_snapshots.values(),
            key=lambda snapshot: (
                1 if _rate_limit_scope(snapshot) == "global" else 0,
                snapshot.used_percent or 0.0,
                snapshot.observed_at or datetime.min,
            ),
            reverse=True,
        )
        primary_snapshot = ranked[0]

    return events, primary_snapshot


def aggregate_usage_events(
    events: Iterable[UsageEvent],
    rate_limit_snapshots: Iterable[RateLimitSnapshot],
    config: MonitorConfig,
    now: Optional[datetime] = None,
    include_events: bool = False,
    source_metadata: Optional[Dict[str, Any]] = None,
    cwd_filter: Optional[str] = None,
    event_sink=None,
    summary_sink=None,
    progress=None,
) -> Dict[str, Any]:
    """
    读取 ~/.codex/sessions 并汇总：
    - 总 token / 总调用次数 / 总费用（估算）
    - 最近 5 小时 token / 次数 / 费用
    - 按模型、按日期、按 cwd 统计
    - 最新 rate_limits(primary) 快照
    """

    cfg = config
    source_metadata = source_metadata or {}

    current_time = now or datetime.now()
    five_hours_ago = current_time - timedelta(hours=5)
    fifteen_minutes_ago = current_time - timedelta(minutes=15)
    sixty_minutes_ago = current_time - timedelta(minutes=60)
    week_start = (current_time - timedelta(days=current_time.weekday())).replace(
        hour=0, minute=0, second=0, microsecond=0
    )
    five_hour_slot_minutes = 5

    from codex_glass.storage.packed_events import PackedEvents
    from heapq import heappush, heapreplace

    all_events = PackedEvents() if event_sink is not None else []
    raw_paths = set()
    active_cwds = set()
    first_chart_date = current_time.date()
    for position, event in enumerate(events):
        if progress is not None and position % 4096 == 0:
            progress("pricing", position, len(events) if hasattr(events, "__len__") else 0)
        if not _path_matches_filter(event.cwd, cwd_filter):
            continue
        if isinstance(event.cwd, str) and event.cwd.strip():
            raw_paths.add(event.cwd)
        if event.timestamp <= current_time:
            first_chart_date = min(first_chart_date, event.timestamp.date())
            if event.timestamp >= five_hours_ago:
                active_cwds.add(event.cwd or "unknown")
        estimated_cost_usd, pricing_source = estimate_cost_usd(event.model, event.delta, cfg)
        all_events.append(
            UsageEvent(
                timestamp=event.timestamp,
                model=event.model,
                cwd=event.cwd,
                delta=event.delta,
                estimated_cost_usd=estimated_cost_usd,
                pricing_source=pricing_source,
            )
        )
    if isinstance(all_events, PackedEvents):
        all_events.sort()
    else:
        all_events.sort(key=lambda e: e.timestamp)
    ranked_rate_limit_snapshots: Dict[str, RateLimitSnapshot] = {}
    for snapshot in rate_limit_snapshots:
        rate_limit_key = snapshot.limit_id or "global"
        current = ranked_rate_limit_snapshots.get(rate_limit_key)
        if _should_replace_rate_limit(current, snapshot):
            ranked_rate_limit_snapshots[rate_limit_key] = snapshot

    rate_limit_snapshots = ranked_rate_limit_snapshots
    workspace_aliases = {path: _workspace_label(i + 1) for i, path in enumerate(sorted(raw_paths))}

    def empty_stats() -> Dict[str, Any]:
        return {
            "calls": 0,
            "total_tokens": 0,
            "input_tokens": 0,
            "cached_input_tokens": 0,
            "output_tokens": 0,
            "reasoning_output_tokens": 0,
            "estimated_cost_usd": 0.0,
        }

    total = empty_stats()
    by_model: Dict[str, Dict[str, Any]] = {}
    by_date: Dict[str, Dict[str, Any]] = {}
    by_hour: Dict[str, Dict[str, Any]] = {}
    by_cwd: Dict[str, Dict[str, Any]] = {}

    five_hour = empty_stats()
    five_hour_by_hour: Dict[str, Dict[str, Any]] = {}
    five_hour_by_slot: Dict[str, Dict[str, Any]] = {}
    five_hour_by_model: Dict[str, Dict[str, Any]] = {}
    five_hour_by_cwd: Dict[str, Dict[str, Any]] = {}
    today_start = current_time.replace(hour=0, minute=0, second=0, microsecond=0)
    today = empty_stats()
    today_by_model: Dict[str, Dict[str, Any]] = {}
    today_by_cwd: Dict[str, Dict[str, Any]] = {}

    this_week = empty_stats()
    this_week_by_date: Dict[str, Dict[str, Any]] = {}
    this_week_by_model: Dict[str, Dict[str, Any]] = {}
    recent_15m = empty_stats()
    recent_60m = empty_stats()
    priced_calls = 0
    priced_tokens = 0
    unpriced_calls = 0
    unpriced_tokens = 0
    unpriced_by_model: Dict[str, Dict[str, int]] = {}

    chart_bucket_minutes = 5
    today_chart_start = _floor_to_bucket(current_time - timedelta(minutes=30), chart_bucket_minutes)
    five_hour_chart_start = _floor_to_bucket(current_time - timedelta(hours=5), chart_bucket_minutes)
    chart_end = _floor_to_bucket(current_time, chart_bucket_minutes)

    def chart_times(start: datetime) -> List[datetime]:
        result = []
        point = start
        while point <= chart_end:
            result.append(point)
            point += timedelta(minutes=chart_bucket_minutes)
        return result

    today_chart_times = chart_times(today_chart_start)
    five_hour_chart_times = chart_times(five_hour_chart_start)
    today_chart_index = {point: index for index, point in enumerate(today_chart_times)}
    five_hour_chart_index = {point: index for index, point in enumerate(five_hour_chart_times)}
    today_chart_tokens = [0] * len(today_chart_times)
    five_hour_chart_tokens = [0] * len(five_hour_chart_times)
    today_chart_models: Dict[str, List[int]] = {}
    five_hour_chart_models: Dict[str, List[int]] = {}
    earliest_chart_date = max(first_chart_date, current_time.date() - timedelta(days=89))
    all_chart_dates = []
    chart_date = earliest_chart_date
    while chart_date <= current_time.date():
        all_chart_dates.append(chart_date)
        chart_date += timedelta(days=1)
    all_chart_index = {day: index for index, day in enumerate(all_chart_dates)}
    all_chart_tokens = [0] * len(all_chart_dates)
    all_chart_models: Dict[str, List[int]] = {}

    recent_heap = []
    for position, event in enumerate(all_events):
        recent_item = (event.timestamp, -position, event)
        if len(recent_heap) < 50:
            heappush(recent_heap, recent_item)
        elif recent_item[:2] > recent_heap[0][:2]:
            heapreplace(recent_heap, recent_item)
        if progress is not None and position % 4096 == 0:
            progress("totals", position, len(all_events))
        delta = event.delta
        event_tokens = max(0, delta.total_tokens)

        if event.pricing_source == "unpriced":
            unpriced_calls += 1
            unpriced_tokens += event_tokens
            unpriced_model = event.model or "unknown"
            model_coverage = unpriced_by_model.setdefault(unpriced_model, {"calls": 0, "total_tokens": 0})
            model_coverage["calls"] += 1
            model_coverage["total_tokens"] += event_tokens
        else:
            priced_calls += 1
            priced_tokens += event_tokens

        def apply(stats: Dict[str, Any]):
            stats["calls"] += 1
            stats["total_tokens"] += max(0, delta.total_tokens)
            stats["input_tokens"] += max(0, delta.input_tokens)
            stats["cached_input_tokens"] += max(0, delta.cached_input_tokens)
            stats["output_tokens"] += max(0, delta.output_tokens)
            stats["reasoning_output_tokens"] += max(0, delta.reasoning_output_tokens)
            stats["estimated_cost_usd"] += float(event.estimated_cost_usd)

        apply(total)

        model_key = event.model or "unknown"
        cwd_key = _display_cwd(event.cwd, workspace_aliases)

        if event.timestamp <= current_time:
            bucket = _floor_to_bucket(event.timestamp, chart_bucket_minutes)
            if event.timestamp >= current_time - timedelta(minutes=30) and bucket in today_chart_index:
                position = today_chart_index[bucket]
                today_chart_tokens[position] += event_tokens
                today_chart_models.setdefault(model_key, [0] * len(today_chart_times))[position] += event_tokens
            if event.timestamp >= current_time - timedelta(hours=5) and bucket in five_hour_chart_index:
                position = five_hour_chart_index[bucket]
                five_hour_chart_tokens[position] += event_tokens
                five_hour_chart_models.setdefault(model_key, [0] * len(five_hour_chart_times))[position] += event_tokens
            day = event.timestamp.date()
            if day in all_chart_index:
                position = all_chart_index[day]
                all_chart_tokens[position] += event_tokens
                all_chart_models.setdefault(model_key, [0] * len(all_chart_dates))[position] += event_tokens
        if model_key not in by_model:
            by_model[model_key] = empty_stats()
        apply(by_model[model_key])
        if today_start <= event.timestamp <= current_time:
            apply(today)
            apply(today_by_model.setdefault(model_key, empty_stats()))
            apply(today_by_cwd.setdefault(cwd_key, empty_stats()))

        date_key = event.timestamp.strftime("%Y-%m-%d")
        if date_key not in by_date:
            by_date[date_key] = empty_stats()
        apply(by_date[date_key])

        hour_key = event.timestamp.strftime("%Y-%m-%d %H:00")
        if hour_key not in by_hour:
            by_hour[hour_key] = empty_stats()
        apply(by_hour[hour_key])

        if cwd_key not in by_cwd:
            by_cwd[cwd_key] = empty_stats()
        apply(by_cwd[cwd_key])

        if five_hours_ago <= event.timestamp <= current_time:
            apply(five_hour)
            if hour_key not in five_hour_by_hour:
                five_hour_by_hour[hour_key] = empty_stats()
            apply(five_hour_by_hour[hour_key])

            slot_key = _floor_to_bucket(event.timestamp, five_hour_slot_minutes).strftime("%Y-%m-%d %H:%M")
            if slot_key not in five_hour_by_slot:
                five_hour_by_slot[slot_key] = empty_stats()
            apply(five_hour_by_slot[slot_key])

            if model_key not in five_hour_by_model:
                five_hour_by_model[model_key] = empty_stats()
            apply(five_hour_by_model[model_key])
            apply(five_hour_by_cwd.setdefault(cwd_key, empty_stats()))

        if fifteen_minutes_ago <= event.timestamp <= current_time:
            apply(recent_15m)

        if sixty_minutes_ago <= event.timestamp <= current_time:
            apply(recent_60m)

        if week_start <= event.timestamp <= current_time:
            apply(this_week)
            if date_key not in this_week_by_date:
                this_week_by_date[date_key] = empty_stats()
            apply(this_week_by_date[date_key])

            if model_key not in this_week_by_model:
                this_week_by_model[model_key] = empty_stats()
            apply(this_week_by_model[model_key])

    # 补齐窗口明细里的空桶，便于前端稳定展示
    start_hour = (current_time - timedelta(hours=5)).replace(minute=0, second=0, microsecond=0)
    end_hour = current_time.replace(minute=0, second=0, microsecond=0)
    t = start_hour
    while t <= end_hour:
        k = t.strftime("%Y-%m-%d %H:00")
        if k not in five_hour_by_hour:
            five_hour_by_hour[k] = empty_stats()
        t += timedelta(hours=1)

    slot_t = _floor_to_bucket(five_hours_ago, five_hour_slot_minutes)
    slot_end = _floor_to_bucket(current_time, five_hour_slot_minutes)
    while slot_t <= slot_end:
        k = slot_t.strftime("%Y-%m-%d %H:%M")
        if k not in five_hour_by_slot:
            five_hour_by_slot[k] = empty_stats()
        slot_t += timedelta(minutes=five_hour_slot_minutes)

    d = week_start.date()
    end_d = current_time.date()
    while d <= end_d:
        k = d.strftime("%Y-%m-%d")
        if k not in this_week_by_date:
            this_week_by_date[k] = empty_stats()
        d += timedelta(days=1)

    recent_events = [item[2] for item in sorted(recent_heap, reverse=True)]
    recent_calls = [
        {
            "timestamp": e.timestamp.isoformat(sep=" ", timespec="seconds"),
            "model": e.model,
            "cwd": _display_cwd(e.cwd, workspace_aliases),
            "total_tokens": e.delta.total_tokens,
            "input_tokens": e.delta.input_tokens,
            "cached_input_tokens": e.delta.cached_input_tokens,
            "output_tokens": e.delta.output_tokens,
            "estimated_cost_usd": round(e.estimated_cost_usd, 6),
            "pricing_source": e.pricing_source,
        }
        for e in recent_events
    ]

    def _series_rows(stats_by_key: Dict[str, Dict[str, Any]], key_name: str) -> List[Dict[str, Any]]:
        rows: List[Dict[str, Any]] = []
        for k in sorted(stats_by_key.keys()):
            st = stats_by_key[k]
            rows.append(
                {
                    key_name: k,
                    "calls": int(st.get("calls", 0)),
                    "total_tokens": int(st.get("total_tokens", 0)),
                    "input_tokens": int(st.get("input_tokens", 0)),
                    "cached_input_tokens": int(st.get("cached_input_tokens", 0)),
                    "output_tokens": int(st.get("output_tokens", 0)),
                    "reasoning_output_tokens": int(st.get("reasoning_output_tokens", 0)),
                    "estimated_cost_usd": round(float(st.get("estimated_cost_usd", 0.0)), 6),
                }
            )
        return rows

    series = {
        "by_date": _series_rows(by_date, "date"),
        "by_hour": _series_rows(by_hour, "hour"),
    }

    def rate_chart(times: List[datetime], totals: List[int], models: Dict[str, List[int]]) -> Dict[str, Any]:
        return {
            "labels": [point.isoformat(timespec="seconds") for point in times],
            "values": [round(value / 5.0, 2) for value in totals],
            "by_model": {
                model: [round(value / 5.0, 2) for value in values] for model, values in sorted(models.items())
            },
            "unit": "Token / min",
            "bucket_minutes": 5,
        }

    charts = {
        "today": rate_chart(today_chart_times, today_chart_tokens, today_chart_models),
        "last_5_hours": rate_chart(five_hour_chart_times, five_hour_chart_tokens, five_hour_chart_models),
        "all": {
            "labels": [day.isoformat() for day in all_chart_dates],
            "values": all_chart_tokens,
            "by_model": {model: values for model, values in sorted(all_chart_models.items())},
            "unit": "每日 Token",
            "bucket_minutes": 1440,
        },
    }

    def _top_rows(stats_by_key: Dict[str, Dict[str, Any]], key_name: str, limit: int = 15) -> List[Dict[str, Any]]:
        rows: List[Dict[str, Any]] = []
        for k, st in stats_by_key.items():
            rows.append(
                {
                    key_name: k,
                    "calls": int(st.get("calls", 0)),
                    "total_tokens": int(st.get("total_tokens", 0)),
                    "input_tokens": int(st.get("input_tokens", 0)),
                    "cached_input_tokens": int(st.get("cached_input_tokens", 0)),
                    "output_tokens": int(st.get("output_tokens", 0)),
                    "reasoning_output_tokens": int(st.get("reasoning_output_tokens", 0)),
                    "estimated_cost_usd": round(float(st.get("estimated_cost_usd", 0.0)), 6),
                }
            )
        rows.sort(key=lambda r: (r.get("total_tokens", 0), r.get("estimated_cost_usd", 0.0)), reverse=True)
        return rows[: max(1, int(limit))]

    windows = {
        "today": {
            "total": today,
            "by_model": _top_rows(today_by_model, "model", max(1, len(today_by_model))),
            "by_cwd": today_by_cwd,
        },
        "last_5_hours": {
            "range": {
                "start": five_hours_ago.isoformat(sep=" ", timespec="seconds"),
                "end": current_time.isoformat(sep=" ", timespec="seconds"),
                "bucket_minutes": five_hour_slot_minutes,
            },
            "total": five_hour,
            "by_slot": _series_rows(five_hour_by_slot, "slot"),
            "by_hour": _series_rows(five_hour_by_hour, "hour"),
            "by_model": _top_rows(five_hour_by_model, "model", max(1, len(five_hour_by_model))),
            "by_cwd": five_hour_by_cwd,
        },
        "this_week": {
            "range": {
                "start": week_start.isoformat(sep=" ", timespec="seconds"),
                "end": current_time.isoformat(sep=" ", timespec="seconds"),
            },
            "total": this_week,
            "by_date": _series_rows(this_week_by_date, "date"),
            "by_model": _top_rows(this_week_by_model, "model", 15),
        },
    }

    rate_limits = None
    primary_rl: Optional[RateLimitSnapshot] = None
    if rate_limit_snapshots:
        if "codex" in rate_limit_snapshots:
            primary_rl = rate_limit_snapshots["codex"]
        else:
            ranked = sorted(
                rate_limit_snapshots.values(),
                key=lambda snapshot: (
                    1 if _rate_limit_scope(snapshot) == "global" else 0,
                    snapshot.window_minutes or 0,
                    snapshot.resets_at or datetime.min,
                    snapshot.used_percent or 0.0,
                    snapshot.observed_at or datetime.min,
                ),
                reverse=True,
            )
            primary_rl = ranked[0] if ranked else None

        rate_limits = {
            "primary": _rate_limit_payload(primary_rl, current_time) if primary_rl is not None else None,
            "limits": [
                _rate_limit_payload(snapshot, current_time)
                for snapshot in sorted(
                    rate_limit_snapshots.values(),
                    key=lambda snapshot: (
                        snapshot.resets_at or datetime.min,
                        snapshot.used_percent or 0.0,
                        snapshot.observed_at or datetime.min,
                    ),
                    reverse=True,
                )
            ],
        }

    active_slots = sum(1 for st in five_hour_by_slot.values() if int(st.get("total_tokens", 0)) > 0)
    last_event = all_events[-1] if all_events else None
    five_hour_models_active = sum(1 for st in five_hour_by_model.values() if int(st.get("total_tokens", 0)) > 0)
    five_hour_cwds_active = len(active_cwds)

    tokens_per_minute_5h = round(float(five_hour.get("total_tokens", 0)) / 300.0, 2)
    tokens_per_minute_15m = round(float(recent_15m.get("total_tokens", 0)) / 15.0, 2)
    calls_per_minute_5h = round(float(five_hour.get("calls", 0)) / 300.0, 3)
    calls_per_minute_15m = round(float(recent_15m.get("calls", 0)) / 15.0, 3)
    cost_per_hour_5h = round(float(five_hour.get("estimated_cost_usd", 0.0)) / 5.0, 6)
    cache_hit_ratio_5h = _safe_ratio(five_hour.get("cached_input_tokens", 0), five_hour.get("input_tokens", 0))
    output_ratio_5h = _safe_ratio(five_hour.get("output_tokens", 0), five_hour.get("input_tokens", 0))
    avg_tokens_per_event_5h = _safe_ratio(five_hour.get("total_tokens", 0), five_hour.get("calls", 0), 2)
    spike_ratio = _safe_ratio(tokens_per_minute_15m, tokens_per_minute_5h, 2) if tokens_per_minute_5h > 0 else 0.0

    anomaly_level = "normal"
    if spike_ratio >= 2.0:
        anomaly_level = "high"
    elif spike_ratio >= 1.25:
        anomaly_level = "elevated"

    projected_exhaustion_seconds: Optional[int] = None
    official_window_used_percent: Optional[float] = None
    official_window_observed_at: Optional[str] = None
    if rate_limits and isinstance(rate_limits.get("primary"), dict):
        primary_rl = rate_limits["primary"]
        used_percent = primary_rl.get("used_percent")
        window_minutes = primary_rl.get("window_minutes")
        remaining_seconds = primary_rl.get("remaining_seconds")
        if used_percent is not None and int(window_minutes or 0) == 300:
            official_window_used_percent = float(used_percent)
        official_window_observed_at = primary_rl.get("observed_at")
        if used_percent is not None and window_minutes and remaining_seconds is not None:
            used_fraction = max(0.0, min(1.0, float(used_percent) / 100.0))
            total_window_seconds = int(window_minutes) * 60
            elapsed_seconds = max(0, total_window_seconds - int(remaining_seconds))
            if used_fraction > 0 and elapsed_seconds > 0:
                pace_per_second = used_fraction / float(elapsed_seconds)
                if pace_per_second > 0:
                    projected_exhaustion_seconds = max(0, int((1.0 - used_fraction) / pace_per_second))

    metrics = {
        "window_slot_minutes": five_hour_slot_minutes,
        "bucket_minutes_5h": five_hour_slot_minutes,
        "tokens_per_minute_5h": tokens_per_minute_5h,
        "tokens_per_min_5h": tokens_per_minute_5h,
        "tokens_per_minute_15m": tokens_per_minute_15m,
        "calls_per_minute_5h": calls_per_minute_5h,
        "calls_per_minute_15m": calls_per_minute_15m,
        "cost_per_hour_5h": cost_per_hour_5h,
        "cost_per_hour": cost_per_hour_5h,
        "cache_hit_ratio_5h": cache_hit_ratio_5h,
        "cache_hit_ratio": cache_hit_ratio_5h,
        "output_ratio_5h": output_ratio_5h,
        "output_ratio": output_ratio_5h,
        "avg_tokens_per_event_5h": avg_tokens_per_event_5h,
        "active_models_5h": five_hour_models_active,
        "active_models": five_hour_models_active,
        "active_cwds_5h": five_hour_cwds_active,
        "active_cwds": five_hour_cwds_active,
        "active_slots_5h": active_slots,
        "active_slot_ratio_5h": _safe_ratio(active_slots, len(five_hour_by_slot)),
        "spike_ratio_15m_vs_5h": spike_ratio,
        "anomaly_level": anomaly_level,
        "anomaly_flag": anomaly_level,
        "last_event_at": last_event.timestamp.isoformat(sep=" ", timespec="seconds") if last_event else None,
        "seconds_since_last_event": (
            max(0, int((current_time - last_event.timestamp).total_seconds())) if last_event else None
        ),
        "last_activity_seconds": (
            max(0, int((current_time - last_event.timestamp).total_seconds())) if last_event else None
        ),
        "freshness_seconds": max(0, int((current_time - last_event.timestamp).total_seconds())) if last_event else None,
        "window_utilization_percent": official_window_used_percent,
        "official_window_used_percent": official_window_used_percent,
        "official_window_observed_at": official_window_observed_at,
        "projected_exhaustion_seconds": projected_exhaustion_seconds,
        "recent_15m": recent_15m,
        "recent_60m": recent_60m,
    }

    total_calls = priced_calls + unpriced_calls
    total_tokens = priced_tokens + unpriced_tokens
    token_coverage_ratio = round(priced_tokens / total_tokens, 4) if total_tokens else 1.0
    pricing = {
        "policy_version": PRICING_POLICY_VERSION,
        "policy_checked_on": PRICING_POLICY_CHECKED_ON,
        "method": "current-price-estimate",
        "estimate_scope": "known-price-usage-only",
        "priced_calls": priced_calls,
        "priced_tokens": priced_tokens,
        "unpriced_calls": unpriced_calls,
        "unpriced_tokens": unpriced_tokens,
        "total_calls": total_calls,
        "total_tokens": total_tokens,
        "token_coverage_ratio": token_coverage_ratio,
        "token_coverage_percent": round(token_coverage_ratio * 100.0, 2),
        "unpriced_models": [
            {
                "model": model,
                "calls": unpriced_by_model[model]["calls"],
                "total_tokens": unpriced_by_model[model]["total_tokens"],
            }
            for model in sorted(unpriced_by_model)
        ],
    }

    summary: Dict[str, Any] = {
        "source": {
            "label": "本地 Codex 会话流",
            "scope": "已启用工作区过滤" if cwd_filter else "全局工作区",
            "cwd_filter": "enabled" if cwd_filter else None,
            "files": int(source_metadata.get("files", 0)),
            "latest_event_at": last_event.timestamp.isoformat(sep=" ", timespec="seconds") if last_event else None,
            "refresh_lag_seconds": (
                max(0, int((current_time - last_event.timestamp).total_seconds())) if last_event else None
            ),
            "paths_redacted": True,
        },
        "total": total,
        "five_hour": five_hour,
        "by_model": by_model,
        "by_date": by_date,
        "by_hour": by_hour,
        "by_cwd": by_cwd,
        "recent_calls": recent_calls,
        "series": series,
        "charts": charts,
        "windows": windows,
        "metrics": metrics,
        "pricing": pricing,
        "rate_limits": rate_limits,
        "generated_at": current_time.isoformat(sep=" ", timespec="seconds"),
        "note": "费用为按当前价格重估的估算值，不是历史账单：默认使用内置 OpenAI Standard tier 文本 token 定价（https://developers.openai.com/api/docs/pricing，2026-09-08 已核对）；也可在本地 monitor_config 配置中覆盖 pricing_per_million / model_aliases。未知模型保持未定价。路径与工作区名称默认已匿名化，便于公开展示。",
    }

    if summary_sink is not None:
        summary_sink(dict(summary))

    if include_events or event_sink is not None:
        if progress is not None:
            progress("history", 0, len(all_events))
        events_payload: List[Dict[str, Any]] = []
        for position, e in enumerate(reversed(all_events)):
            if progress is not None and position % 4096 == 0:
                progress("history", position, len(all_events))
            delta = e.delta
            rates, base_source = cfg.rates_for_model(e.model)

            input_total, cached, uncached, output = _clamped_token_buckets(delta)
            rates, _ = _apply_long_context_pricing(e.model, input_total, rates, base_source)

            cost_uncached = uncached * float(rates.input) / 1_000_000
            cost_cached = cached * float(rates.cached_input) / 1_000_000
            cost_output = output * float(rates.output) / 1_000_000
            cost_total = cost_uncached + cost_cached + cost_output

            (event_sink if event_sink is not None else events_payload.append)(
                {
                    "timestamp": e.timestamp.isoformat(sep=" ", timespec="seconds"),
                    "model": e.model,
                    "cwd": _display_cwd(e.cwd, workspace_aliases),
                    "pricing_source": e.pricing_source,
                    "tokens": {
                        "input": input_total,
                        "cached_input": cached,
                        "uncached_input": uncached,
                        "output": output,
                        "reasoning_output": max(0, int(delta.reasoning_output_tokens)),
                        "total": max(0, int(delta.total_tokens)),
                    },
                    "rates_per_million": {
                        "input": float(rates.input),
                        "cached_input": float(rates.cached_input),
                        "output": float(rates.output),
                    },
                    "cost_usd": {
                        "uncached_input": round(cost_uncached, 6),
                        "cached_input": round(cost_cached, 6),
                        "output": round(cost_output, 6),
                        "total": round(cost_total, 6),
                    },
                }
            )
        if event_sink is None:
            summary["events"] = events_payload
        summary["events_count"] = len(all_events)

    return summary


def build_usage_summary(
    sessions_dir: Optional[Path] = None,
    config: Optional[MonitorConfig] = None,
    cwd_filter: Optional[str] = None,
    now: Optional[datetime] = None,
    include_events: bool = False,
) -> Dict[str, Any]:
    """Parse raw session logs and return the legacy usage summary payload."""
    cfg = config or MonitorConfig.load()
    sessions = sessions_dir or default_codex_sessions_dir()
    files = iter_session_files(sessions)
    events: List[UsageEvent] = []
    snapshots: List[RateLimitSnapshot] = []

    for file_path in files:
        parsed_events, snapshot = parse_usage_events_from_session_file(file_path, cfg)
        events.extend(parsed_events)
        if snapshot is not None:
            snapshots.append(snapshot)

    return aggregate_usage_events(
        events,
        snapshots,
        cfg,
        now=now,
        include_events=include_events,
        source_metadata={"sessions_dir": str(sessions), "files": len(files)},
        cwd_filter=cwd_filter,
    )
