"""Transient event columns: expand one object at a time during aggregation.

Unlike history, these retain exact microseconds, signed SQLite integers and
timezone metadata. No pricing is recalculated or rounded by this container.
"""

from array import array
from collections.abc import Sequence
from datetime import datetime, timedelta
from itertools import pairwise

from codex_glass.core.usage import UsageDelta, UsageEvent


EPOCH = datetime(1970, 1, 1)
FIELDS = ("input_tokens", "cached_input_tokens", "output_tokens", "reasoning_output_tokens", "total_tokens")


class PackedEvents(Sequence):
    def __init__(self):
        self.times = array("q")
        self.ids = array("I")
        self.costs = array("d")
        self.tokens = [array("q") for _ in FIELDS]
        self.dimensions = []
        self.dimension_ids = {}

    def __len__(self):
        return len(self.times)

    def append(self, event):
        timestamp = event.timestamp
        delta = (timestamp.replace(tzinfo=None) if timestamp.tzinfo else timestamp) - EPOCH
        stamp = (delta.days * 86400 + delta.seconds) * 1000000 + delta.microseconds
        dimension = (event.model, event.cwd, event.pricing_source, event.timestamp.tzinfo)
        if dimension not in self.dimension_ids:
            self.dimension_ids[dimension] = len(self.dimensions)
            self.dimensions.append(dimension)
        self.times.append(stamp)
        self.ids.append(self.dimension_ids[dimension])
        self.costs.append(event.estimated_cost_usd)
        a, b, c, d, e = self.tokens
        usage = event.delta
        a.append(usage.input_tokens)
        b.append(usage.cached_input_tokens)
        c.append(usage.output_tokens)
        d.append(usage.reasoning_output_tokens)
        e.append(usage.total_tokens)

    def __iter__(self):
        for stamp, identity, cost, a, b, c, d, e in zip(self.times, self.ids, self.costs, *self.tokens):
            model, cwd, pricing, tz = self.dimensions[identity]
            timestamp = EPOCH + timedelta(microseconds=stamp)
            if tz is not None:
                timestamp = timestamp.replace(tzinfo=tz)
            yield UsageEvent(timestamp, model, cwd, UsageDelta(a, b, c, d, e), cost, pricing)

    def __getitem__(self, index):
        if isinstance(index, slice):
            return [self[i] for i in range(*index.indices(len(self)))]
        model, cwd, pricing, tz = self.dimensions[self.ids[index]]
        timestamp = EPOCH + timedelta(microseconds=self.times[index])
        if tz is not None:
            timestamp = timestamp.replace(tzinfo=tz)
        a, b, c, d, e = self.tokens
        return UsageEvent(
            timestamp,
            model,
            cwd,
            UsageDelta(a[index], b[index], c[index], d[index], e[index]),
            self.costs[index],
            pricing,
        )

    def reorder(self, indices):
        self.times = array("q", (self.times[i] for i in indices))
        self.ids = array("I", (self.ids[i] for i in indices))
        self.costs = array("d", (self.costs[i] for i in indices))
        self.tokens = [array("q", (values[i] for i in indices)) for values in self.tokens]

    def sort(self, key=None):
        if key is None and all(dimension[3] is None for dimension in self.dimensions):
            if all(a <= b for a, b in pairwise(self.times)):
                return
            sort_key = self.times.__getitem__
        else:
            sort_key = lambda i: key(self[i]) if key else self[i].timestamp
        self.reorder(sorted(range(len(self)), key=sort_key))
