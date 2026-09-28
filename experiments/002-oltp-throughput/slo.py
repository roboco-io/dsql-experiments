"""E002 SLO judgment and capacity (Q) selection. Pure functions over cell result dicts.

Verdicts: pass / fail (an SLO bound broken) / invalid (error, generator saturation, or an invariant violation).
Invalid cells never count as failures and never lower Q: they say nothing about the database.
"""
from __future__ import annotations

SLO = {"product_read": (50, 100), "order_history": (50, 100), "order_create": (100, 200), "cancel": (100, 200)}
MAX_TECH_FAILURE = 0.001
GEN_CPU_MAX = 85.0
GEN_LAG_P99_MAX_MS = 10.0
REQUIRE_GENERATOR_CPU = True    # the CPU sampler needs Linux /proc/stat; local rehearsals turn this off
STEP = 1.25


def judge(r: dict) -> dict:
    if r.get("status") != "ok":
        return {"verdict": "invalid", "reasons": [f"status {r.get('status')}"]}
    reasons = []
    g = r.get("generator") or {}
    if g.get("cpu_pct") is None and REQUIRE_GENERATOR_CPU:
        reasons.append("generator cpu unknown")
    elif (g.get("cpu_pct") or 0) > GEN_CPU_MAX:
        reasons.append(f"generator cpu {g['cpu_pct']:.0f}%")
    if (g.get("lag_p99_ms") or 0) > GEN_LAG_P99_MAX_MS:
        reasons.append(f"schedule lag p99 {g['lag_p99_ms']:.1f} ms")
    if (r.get("invariants") or {}).get("violations"):
        reasons.append("invariant violation")
    if reasons:
        return {"verdict": "invalid", "reasons": reasons}
    m = r["metrics"]
    for kind, (p95, p99) in SLO.items():
        k = m["per_kind"].get(kind) or {}
        if k.get("p95_ms") is None or k["p95_ms"] > p95:
            reasons.append(f"{kind} p95 {k.get('p95_ms')}")
        if k.get("p99_ms") is None or k["p99_ms"] > p99:
            reasons.append(f"{kind} p99 {k.get('p99_ms')}")
    if m.get("technical_failure_rate") is None or m["technical_failure_rate"] > MAX_TECH_FAILURE:
        reasons.append(f"technical failure {m.get('technical_failure_rate')}")
    return {"verdict": "fail" if reasons else "pass", "reasons": reasons}


def qref(explore: dict) -> dict:
    """Best passing success TPS per config; Qref is the smallest of them (None if any config never passed)."""
    per = {}
    for cfg, results in explore.items():
        ok = [r["metrics"]["success_tps"] for r in results if judge(r)["verdict"] == "pass"]
        per[cfg] = max(ok) if ok else None
    if not per or any(v is None for v in per.values()):
        return {"qref": None, "per_config": per, "limiting": None}
    limiting = min(per, key=per.get)
    return {"qref": per[limiting], "per_config": per, "limiting": limiting}


def main_rates(q: float) -> list[float]:
    return [0.5 * q, 1.0 * q, 1.2 * q]


def next_rate(passed, failed, q, stop):
    """25% steps above the highest pass until the first fail, then one midpoint, then None."""
    if stop:
        return None
    hi = max(passed) if passed else None
    if not failed:
        return (hi or q) * STEP
    lo_fail = min(failed)
    if hi is None or hi > lo_fail / STEP or lo_fail - hi <= q * 0.05:
        return None              # one midpoint only: stop once a rate between the last step and the fail exists
    return (hi + lo_fail) / 2


def select_q(results, reps_required, stopped_early=False) -> dict:
    """Q = highest rate whose every valid rep passed."""
    by_rate: dict[float, list[str]] = {}
    for r in results:
        v = judge(r)["verdict"]
        if v != "invalid":
            by_rate.setdefault(r["cell"]["rate"], []).append(v)
    passing = [rate for rate, vs in by_rate.items() if vs and all(v == "pass" for v in vs)]
    any_fail = any("fail" in vs for vs in by_rate.values())
    if not passing:
        return {"q": None, "status": "none", "reps": 0, "any_fail": any_fail}
    q = max(passing)
    reps = len(by_rate[q])
    if stopped_early and not any(rate > q and "fail" in vs for rate, vs in by_rate.items()):
        status = "lower_bound"
    elif reps < reps_required:
        status = "provisional"
    else:
        status = "confirmed"
    return {"q": q, "status": status, "reps": reps, "any_fail": any_fail}
