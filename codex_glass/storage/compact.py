"""Immutable-after-publication columnar history; expand only requested rows.

No chat content or raw paths. This preserves the public event shape while
avoiding hundreds of thousands of nested Python dictionaries in the server.
"""

from array import array
from datetime import datetime, timedelta
from collections.abc import Sequence

EPOCH = datetime(1970, 1, 1)
TOKEN_KEYS = ("input", "cached_input", "uncached_input", "output", "reasoning_output", "total")
RATE_KEYS = ("input", "cached_input", "output")
COST_KEYS = ("uncached_input", "cached_input", "output", "total")


def stamp(value):
    dt = datetime.fromisoformat(value)
    if dt.tzinfo is not None:
        dt = dt.astimezone().replace(tzinfo=None)
    return int((dt - EPOCH).total_seconds())


class CompactHistory(Sequence):
    def __init__(self):
        self.times = array("q")
        self.ids = array("I")
        self.dimensions = []
        self.dimension_ids = {}
        self.tokens = [array("Q") for _ in TOKEN_KEYS]
        self.rates = [array("d") for _ in RATE_KEYS]
        self.costs = [array("d") for _ in COST_KEYS]

    def __len__(self):
        return len(self.times)

    @property
    def storage_bytes(self):
        return sum(
            a.buffer_info()[1] * a.itemsize for a in [self.times, self.ids] + self.tokens + self.rates + self.costs
        )

    def append(self, row):
        dim = tuple(row.get(k, "") for k in ("model", "cwd", "pricing_source"))
        if dim not in self.dimension_ids:
            self.dimension_ids[dim] = len(self.dimensions)
            self.dimensions.append(dim)
        self.ids.append(self.dimension_ids[dim])
        self.times.append(stamp(row["timestamp"]))
        for arrays, keys, name in (
            (self.tokens, TOKEN_KEYS, "tokens"),
            (self.rates, RATE_KEYS, "rates_per_million"),
            (self.costs, COST_KEYS, "cost_usd"),
        ):
            for a, k in zip(arrays, keys):
                a.append(row.get(name, {}).get(k, 0))

    def __getitem__(self, i):
        if isinstance(i, slice):
            return [self[j] for j in range(*i.indices(len(self)))]
        m, c, p = self.dimensions[self.ids[i]]
        return {
            "timestamp": (EPOCH + timedelta(seconds=self.times[i])).isoformat(sep=" ", timespec="seconds"),
            "model": m,
            "cwd": c,
            "pricing_source": p,
            "tokens": {k: a[i] for k, a in zip(TOKEN_KEYS, self.tokens)},
            "rates_per_million": {k: a[i] for k, a in zip(RATE_KEYS, self.rates)},
            "cost_usd": {k: a[i] for k, a in zip(COST_KEYS, self.costs)},
        }

    def page(
        self,
        error=None,
        offset=0,
        limit=200,
        q="",
        model="",
        cwd="",
        since="",
        until="",
        models=None,
        sort="timestamp",
        descending=True,
    ):
        offset = max(0, offset)
        limit = max(1, min(500, limit))
        q = q.strip().lower()
        model = model.strip().lower()
        cwd = cwd.strip().lower()
        selected = None if models is None else {str(m).lower() for m in models}
        lo = stamp(since) if since else None
        hi = stamp(until) if until else None
        allowed = set()
        for i, (m, c, p) in enumerate(self.dimensions):
            m = m.lower()
            c = c.lower()
            if (
                (not q or q in m or q in c)
                and (not model or m == model)
                and (not cwd or cwd in c)
                and (selected is None or m in selected)
            ):
                allowed.add(i)
        indices = (
            i
            for i, t in enumerate(self.times)
            if self.ids[i] in allowed and (lo is None or t >= lo) and (hi is None or t <= hi)
        )
        if sort != "timestamp" or not descending:
            key = {
                "tokens": lambda i: self.tokens[-1][i],
                "cost": lambda i: self.costs[-1][i],
                "model": lambda i: self.dimensions[self.ids[i]][0],
            }.get(sort, lambda i: self.times[i])
            indices = sorted(indices, key=key, reverse=descending)
        page = []
        total = 0
        for i in indices:
            if offset <= total < offset + limit:
                page.append(self[i])
            total += 1
        return dict(error=error, total=total, offset=offset, limit=limit, events=page)
