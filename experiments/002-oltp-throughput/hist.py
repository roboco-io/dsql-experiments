"""Sparse log-bucket latency histogram (about 1% relative error); mergeable across processes."""
from __future__ import annotations

import math


class LogHistogram:
    def __init__(self, precision: float = 0.01, min_ms: float = 0.01):
        self.precision, self.min_ms = precision, min_ms
        self.base = 1 + precision
        self.buckets: dict[int, int] = {}
        self.count = 0
        self.min = None
        self.max = None

    def _index(self, ms: float) -> int:
        return 0 if ms <= self.min_ms else math.ceil(math.log(ms / self.min_ms, self.base))

    def _upper(self, i: int) -> float:
        return self.min_ms * self.base ** i

    def record(self, ms: float) -> None:
        i = self._index(ms)
        self.buckets[i] = self.buckets.get(i, 0) + 1
        self.count += 1
        self.min = ms if self.min is None else min(self.min, ms)
        self.max = ms if self.max is None else max(self.max, ms)

    def merge(self, other: "LogHistogram") -> None:
        if (other.precision, other.min_ms) != (self.precision, self.min_ms):
            raise ValueError("histogram parameters differ")
        for i, n in other.buckets.items():
            self.buckets[i] = self.buckets.get(i, 0) + n
        self.count += other.count
        for v in (other.min, other.max):
            if v is not None:
                self.min = v if self.min is None else min(self.min, v)
                self.max = v if self.max is None else max(self.max, v)

    def percentile(self, q: float):
        if not self.count:
            return None
        rank = max(1, math.ceil(q / 100 * self.count))
        seen = 0
        for i in sorted(self.buckets):
            seen += self.buckets[i]
            if seen >= rank:
                return min(self._upper(i), self.max)
        return self.max

    def to_dict(self) -> dict:
        return {"precision": self.precision, "min_ms": self.min_ms, "count": self.count, "min": self.min,
                "max": self.max, "buckets": {str(i): n for i, n in self.buckets.items()}}

    @classmethod
    def from_dict(cls, d: dict) -> "LogHistogram":
        h = cls(d["precision"], d["min_ms"])
        h.buckets = {int(i): n for i, n in d["buckets"].items()}
        h.count, h.min, h.max = d["count"], d["min"], d["max"]
        return h
