"""Spend estimation and the shared budget guard for E002 (pure; no AWS calls).

Rates: AWS Price List API, ap-northeast-2, On-Demand, USD (instances/ACU/I/O 2026-09-27, gp3 2026-09-28).
Estimates drive the guard; CloudWatch-measured DPU, ACU and I/O replace them as the run progresses, and actual
charges are reconciled later with Cost Explorer.
"""
from __future__ import annotations

import threading

from safety import parse_iso, utcnow

HARD_CAP_USD = 50.0           # user-set per-experiment cap (2026-09-26)
BUDGET_CAP_USD = 45.0         # guard threshold (90%) leaves headroom for billing lag
CLEANUP_HOURS = 0.5           # active resources keep billing while being deleted
PUBLIC_IPV4_USD_PER_H = 0.005
EBS_ROOT_USD_PER_H = 0.002    # 8 GiB gp3 root volume, rounded up (not separately verified)
HOURS_PER_MONTH = 730
ACU_USD_PER_H = 0.20                  # Aurora Serverless v2 Standard
AURORA_IO_USD_PER_MILLION = 0.24      # Aurora Standard I/O
DSQL_USD_PER_MILLION_DPU = 10.0       # confirmed in E004
# RDS gp3, Single-AZ prices; Multi-AZ bills the same items twice (copies=2).
GP3_PRICES = {"gb_month": 0.131, "iops_month": 0.023, "mibps_month": 0.091}


def gp3_usd_per_h(gib, iops, mibps, prices, copies) -> float:
    """RDS gp3 includes 3,000 IOPS/125 MiB/s below 400 GiB and 12,000 IOPS/500 MiB/s at 400 GiB or more."""
    base_iops, base_mibps = (12000, 500) if gib >= 400 else (3000, 125)
    monthly = (gib * prices["gb_month"] + max(0, iops - base_iops) * prices["iops_month"]
               + max(0, mibps - base_mibps) * prices["mibps_month"])
    return monthly * copies / HOURS_PER_MONTH


DB_RATE_USD_PER_H = {
    "R1": 1.079 + gp3_usd_per_h(400, 12000, 500, GP3_PRICES, 2),   # db.r6g.xlarge Multi-AZ + gp3 x2
    "A1_instance": 0.627,                                           # Aurora db.r6g.xlarge Standard
    "A2_instance_worst": ACU_USD_PER_H * 32,                        # until CloudWatch ACU replaces it
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
    return (sum(resource_usd(r, now) for r in data["resources"])
            + sum(data.get("measured_usd", {}).values()))


def active_rate(data: dict) -> float:
    return sum(r["extra"].get("rate_usd_per_h", 0.0) for r in data["resources"] if r["state"] != "deleted")


def dpu_cost_usd(dpu: float, usd_per_million: float) -> float:
    return dpu / 1_000_000 * usd_per_million


class Guard:
    """One guard for all config threads. `reserve` books the next unit of work; `release` ends it."""

    def __init__(self, data_fn, cap: float = BUDGET_CAP_USD):
        self.data_fn, self.cap = data_fn, cap
        self.inflight: dict[str, float] = {}
        self.lock = threading.Lock()

    def reserve(self, scope: str, usd: float) -> dict:
        with self.lock:
            data = self.data_fn()
            spent = spent_usd(data)
            inflight = sum(v for k, v in self.inflight.items() if k != scope)
            reserve = active_rate(data) * CLEANUP_HOURS
            projected = spent + inflight + usd + reserve
            out = {"spent_usd": round(spent, 4), "inflight_usd": round(inflight, 4), "next_usd": round(usd, 4),
                   "reserve_usd": reserve, "projected_usd": round(projected, 4), "cap_usd": self.cap,
                   "ok": projected <= self.cap}
            if out["ok"]:
                self.inflight[scope] = usd
            return out

    def release(self, scope: str) -> None:
        with self.lock:
            self.inflight.pop(scope, None)
