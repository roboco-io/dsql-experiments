"""Spend estimation and the per-cell budget guard for E002 (pure; no AWS calls).

Rates: 2026-09-25 AWS Price List API, ap-northeast-2, On-Demand, USD. Rates are estimates for the guard;
actual charges are reconciled later from CloudWatch (ACU, DPU) and Cost Explorer.
"""
from __future__ import annotations

from safety import parse_iso, utcnow

HARD_CAP_USD = 50.0           # user-set per-experiment cap (2026-09-26; was 5.0)
BUDGET_CAP_USD = 45.0         # guard threshold (90%) leaves headroom for billing lag
CLEANUP_HOURS = 0.5           # active resources keep billing while being deleted
PUBLIC_IPV4_USD_PER_H = 0.005
EBS_ROOT_USD_PER_H = 0.002    # 8 GiB gp3 root volume, rounded up (not separately verified)
DB_RATE_USD_PER_H = {
    "R1": 0.203 + 0.01,       # db.t4g.medium Multi-AZ + gp3 20 GiB x2 (storage rounded up)
    "A1": 0.147,              # Aurora db.t4g.medium I/O-Optimized
    "A2": 0.26 * 4,           # Serverless v2 I/O-Optimized at max 4 ACU (worst case for the guard)
}


def resource_hours(r: dict, now, since: str | None = None) -> float:
    start = parse_iso(since or r["recorded_at"])
    end = parse_iso(r["deleted_at"]) if r.get("deleted_at") else now
    return max(0.0, (end - start).total_seconds() / 3600)


def resource_usd(r: dict, now) -> float:
    """Rate x lifetime; if a measured cost exists (e.g. A2 ACU from CloudWatch), use it up to `measured_until`
    and the worst-case rate only after that."""
    x = r["extra"]
    rate = x.get("rate_usd_per_h", 0.0)
    if "measured_usd" in x:
        return x["measured_usd"] + rate * resource_hours(r, now, since=x["measured_until"])
    return rate * resource_hours(r, now)


def spent_usd(data: dict, now=None) -> float:
    now = now or utcnow()
    return sum(resource_usd(r, now) for r in data["resources"]) + data.get("dsql_dpu_usd", 0.0)


def active_rate(data: dict) -> float:
    return sum(r["extra"].get("rate_usd_per_h", 0.0) for r in data["resources"] if r["state"] != "deleted")


def dpu_cost_usd(dpu: float, usd_per_million: float) -> float:
    return dpu / 1_000_000 * usd_per_million


def guard(data: dict, cell_seconds: float, est_cell_dpu: float = 0.0, dpu_usd_per_million: float = 0.0,
          cap: float = BUDGET_CAP_USD, now=None) -> dict:
    spent = spent_usd(data, now)
    nxt = active_rate(data) * cell_seconds / 3600 + dpu_cost_usd(est_cell_dpu, dpu_usd_per_million)
    reserve = active_rate(data) * CLEANUP_HOURS
    projected = spent + nxt + reserve
    return {"spent_usd": round(spent, 4), "next_cell_usd": nxt, "reserve_usd": reserve,
            "projected_usd": round(projected, 4), "cap_usd": cap, "ok": projected <= cap}
