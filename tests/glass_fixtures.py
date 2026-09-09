"""Deterministic design fixtures; never imported by live application code."""

from datetime import datetime, timedelta


def telemetry():
    values = [1, 3, 6, 4, 9, 13, 8, 6, 7, 4, 3, 5, 8, 6, 14, 7, 4, 5, 3, 2, 4, 6, 8, 5, 10, 18, 12, 8, 9, 7, 8, 10, 13]
    now = datetime.now().replace(microsecond=0)
    names = ["gpt-6-astra", "gpt-5.6-sol", "gpt-5.6-terra"]
    today = [(305700000, 426.29), (341350000, 184.69), (4750000, 2.22)]
    recent = [(31200000, 28.34), (25700000, 17.92), (1800000, 0.56)]
    history = [(45200000000, 26700), (42100000000, 19500), (520000000, 1765.38)]

    def rows(items):
        return {
            n: {
                "total_tokens": t,
                "estimated_cost_usd": c,
                "input_tokens": t - 100,
                "cached_input_tokens": t // 2,
                "output_tokens": 100,
                "calls": 99,
            }
            for n, (t, c) in zip(names, items)
        }

    def total(items):
        return {"total_tokens": sum(t for t, c in items), "estimated_cost_usd": sum(c for t, c in items)}

    def series(scope):
        daily = scope == "all"
        shape = (
            [
                4,
                6,
                9,
                6,
                8,
                11,
                8,
                11,
                15,
                18,
                13,
                14,
                10,
                13,
                12,
                17,
                15,
                20,
                18,
                23,
                30,
                33,
                25,
                22,
                23,
                18,
                16,
                19,
                17,
                20,
            ]
            if daily
            else [
                2,
                3,
                6,
                9,
                7,
                12,
                9,
                17,
                9,
                7,
                9,
                8,
                17,
                28,
                22,
                12,
                8,
                6,
                8,
                4,
                3,
                5,
                3,
                8,
                6,
                13,
                6,
                4,
                3,
                5,
                4,
                6,
                4,
                8,
                7,
                15,
                13,
                23,
                30,
                22,
                10,
                11,
                13,
                10,
                12,
                11,
                15,
                22,
                24.6,
            ]
        )
        if scope == "today":
            shape = values
        spark = [2, 5, 4, 8, 5, 7, 4, 6, 9, 8, 13, 16, 12, 15, 10, 8, 5, 7, 6, 9, 12, 15, 9, 8, 11, 8, 14, 10]
        return {
            "labels": [
                (
                    now - timedelta(days=len(shape) - i - 1)
                    if daily
                    else now
                    - timedelta(
                        minutes=(300 if scope == "last_5_hours" else 30) * (len(shape) - i - 1) / (len(shape) - 1)
                    )
                ).isoformat()
                for i in range(len(shape))
            ],
            "values": [v * (100000000 if daily else 1000) for v in shape],
            "by_model": {
                n: [spark[(i + j * 4) % len(spark)] * 500 * (j + 1) for i in range(len(shape))]
                for j, n in enumerate(names)
            },
            "unit": "每日 Token" if daily else "Token / min",
            "bucket_minutes": 1440 if daily else 5,
        }

    all_rows = rows(history)
    all_rows["unknown-model"] = {"total_tokens": 8920000000, "estimated_cost_usd": 0}
    return {
        "generated_at": now.isoformat(),
        "total": {"total_tokens": 96740000000, "estimated_cost_usd": 47965.38},
        "by_model": all_rows,
        "windows": {
            "today": {"total": total(today), "by_model": [dict(model=n, **s) for n, s in rows(today).items()]},
            "last_5_hours": {"total": total(recent), "by_model": [dict(model=n, **s) for n, s in rows(recent).items()]},
        },
        "charts": {scope: series(scope) for scope in ("today", "last_5_hours", "all")},
        "pricing": {
            "token_coverage_percent": 90.78,
            "unpriced_tokens": 8920000000,
            "unpriced_models": [{"model": "unknown-model"}],
        },
        "index": {"status": "ready"},
        "rate_limits": {
            "plan_type": "pro",
            "limits": [
                {
                    "scope": "global",
                    "window_minutes": 10080,
                    "used_percent": 23,
                    "resets_at": (now + timedelta(days=6)).isoformat(),
                    "observed_at": now.isoformat(),
                }
            ],
        },
    }
