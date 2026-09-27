"""E002 retry policy (spec: none / max 3 attempts, total deadline 2 s, exponential backoff + full jitter)."""
from __future__ import annotations

from dataclasses import dataclass

RETRYABLE = {"40001", "40P01"}


@dataclass(frozen=True)
class RetryPolicy:
    max_attempts: int
    deadline_s: float = 2.0
    base_s: float = 0.01
    factor: float = 2.0

    def backoff_s(self, attempt: int, rng) -> float:
        """Full jitter after `attempt` failed attempts: uniform(0, base * factor^(attempt-1))."""
        return rng.uniform(0, self.base_s * self.factor ** (attempt - 1))

    def next_delay(self, attempt: int, sqlstate, elapsed_s: float, rng):
        """attempt = attempts already made. Returns the sleep before the next attempt, or None to stop."""
        if sqlstate not in RETRYABLE or attempt >= self.max_attempts:
            return None
        delay = self.backoff_s(attempt, rng)
        if elapsed_s + delay >= self.deadline_s:
            return None
        return delay


POLICIES = {"none": RetryPolicy(1), "retry3": RetryPolicy(3)}


def classify(sqlstate) -> str:
    if sqlstate == "timeout":
        return "timeout"
    if sqlstate == "40001":
        return "serialization"
    if sqlstate == "40P01":
        return "deadlock"
    if sqlstate == "23505":
        return "unique_violation"
    if sqlstate is None or sqlstate == "conn" or str(sqlstate).startswith("08"):
        return "connection"
    return "other"


def is_ambiguous(sqlstate, during_commit: bool) -> bool:
    """A connection-level failure while COMMIT was in flight: the commit may or may not have happened."""
    return during_commit and (sqlstate is None or str(sqlstate).startswith("08"))
