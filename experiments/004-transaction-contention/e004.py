#!/usr/bin/env python3
"""E004 operator CLI.

Order: init → discover → batch-up → for D1, R1, A1, A2: cycle (provision → setup → scenarios →
[pilot: D1 only, then stop for a budget decision] → run → cleanup) → batch-down → verify → summarize.
Every AWS command requires --account-id (checked against STS). One DB config is active at a time.
A Spot interruption (RunnerLost) keeps the DB and exits 3: run `replace-runner`, then `cycle` again, or clean up.
Any other failure or a budget stop deletes the config immediately.
"""
from __future__ import annotations

import argparse
import json
import os
import shlex
import statistics
import sys
import time
from dataclasses import asdict
from datetime import timedelta

import cost
import infra as IN
import load as L
import remote
import safety as S

HERE = os.path.dirname(os.path.abspath(__file__))
ART = os.path.join(HERE, "artifacts")
CELL_OVERHEAD_S = 60       # reset + connections + invariants + SSM round trips per cell (estimate, re-measured)
LIFETIME_MARGIN_MIN = 30   # cleanup headroom required beyond a config's estimated duration
PILOT_SAFETY = 1.25        # headroom on pilot-based DPU estimates
CONFIG_FIXED_H = 0.75      # provision + setup + scenarios + delete per DB config (estimate)
log = IN.log


class BudgetStop(RuntimeError):
    pass


class RunnerLost(RuntimeError):
    pass


# ---------------------------------------------------------------- pure ----

def drive(cells, done_ids, guard_fn, execute_fn) -> list[str]:
    ran = []
    for cell in cells:
        if cell.cell_id in done_ids:
            continue
        g = guard_fn(cell)
        if not g["ok"]:
            raise BudgetStop(f"guard stopped before {cell.cell_id}: {g}")
        execute_fn(cell)
        ran.append(cell.cell_id)
    return ran


def pilot_cells():
    return [L.Cell("D1", "REPEATABLE READ", d, c, "retry3", 0, warmup_s=5, measure_s=20)
            for d in L.DISTS for c in L.CONCURRENCY]


def pilot_estimate(pilot_results, dpu_total, planned_cells, usd_per_million) -> dict:
    attempts = sum(r["stats"]["attempts_total"] for r in pilot_results)
    if not attempts:
        raise RuntimeError("pilot made no attempts")
    rate = {}
    for r in pilot_results:
        c = r["cell"]
        rate[str(c["concurrency"])] = max(rate.get(str(c["concurrency"]), 0.0),
                                          r["stats"]["attempts_total"] / (c["warmup_s"] + c["measure_s"]))
    est_attempts = PILOT_SAFETY * sum(rate[str(c.concurrency)] * (c.warmup_s + c.measure_s) for c in planned_cells)
    dpa = dpu_total / attempts
    return {"dpu_total": dpu_total, "pilot_attempts": attempts, "dpu_per_attempt": dpa,
            "attempt_rate_by_concurrency": rate, "est_attempts": est_attempts, "est_dpu": est_attempts * dpa,
            "est_usd": cost.dpu_cost_usd(est_attempts * dpa, usd_per_million), "usd_per_million_dpu": usd_per_million}


def pilot_cell_dpu(pilot, cell) -> float:
    return (PILOT_SAFETY * pilot["attempt_rate_by_concurrency"][str(cell.concurrency)]
            * (cell.warmup_s + cell.measure_s) * pilot["dpu_per_attempt"])


def dpu_price(args, cfg, pilot):
    """USD per million DPU: the flag if given, else the price recorded by the pilot. Required for D1."""
    price = args.dsql_usd_per_million_dpu or (pilot or {}).get("usd_per_million_dpu")
    if cfg != "D1":
        return price or 0.0
    if not price:
        raise S.SafetyError("D1 needs --dsql-usd-per-million-dpu (or a pilot.json that recorded it)")
    return price


def config_minutes(cfg, reps, warmup_s, measure_s) -> float:
    cells = len(L.plan_cells(cfg, reps, warmup_s, measure_s))
    return (cells * (warmup_s + measure_s + CELL_OVERHEAD_S) / 3600 + CONFIG_FIXED_H) * 60


def lifetime_problem(minutes_left, cfg, reps, warmup_s, measure_s):
    need = config_minutes(cfg, reps, warmup_s, measure_s) + LIFETIME_MARGIN_MIN
    if minutes_left >= need:
        return None
    return f"{cfg} needs about {need:.0f} min but the run prefix lifetime has {minutes_left:.0f} min left"


def with_runner_check(fn, alive):
    """Run fn; if it fails, ask alive() first so a lost Spot runner surfaces as RunnerLost, not a plain error."""
    try:
        return fn()
    except RunnerLost:
        raise
    except Exception:
        alive()
        raise


def retry_once(fn, on_retry):
    try:
        return fn()
    except (RunnerLost, S.SafetyError):
        raise
    except Exception as exc:  # noqa: BLE001 - one retry for transient runner/SSM failures
        on_retry(exc)
        return fn()


def assert_config_runnable(m, cfg):
    if m.data["config_status"].get(cfg) in ("cleaned", "cleanup_failed"):
        raise S.SafetyError(f"{cfg} was already cleaned up in this run prefix; start a new prefix to rerun it")


def retire_runner(m, iid):
    m.set_state(S.BATCH, "ec2_instance", iid, "deleted", retired=True)


def batch_estimate(reps, warmup_s, measure_s, runner_rate, pilot) -> dict:
    per_cell_h = (warmup_s + measure_s + CELL_OVERHEAD_S) / 3600
    out, hours_total = {}, 0.0
    for cfg in S.CONFIGS:
        hours = len(L.plan_cells(cfg, reps, warmup_s, measure_s)) * per_cell_h + CONFIG_FIXED_H
        hours_total += hours
        out[cfg] = {"hours": round(hours, 2), "db_usd_worst": round(hours * cost.DB_RATE_USD_PER_H.get(cfg, 0.0), 3)}
    out["D1"]["dpu_usd"] = round(pilot["est_usd"], 3) if pilot else None
    out["runner_usd"] = round(hours_total * runner_rate, 3)
    out["total_usd_worst"] = round(sum(v["db_usd_worst"] for k, v in out.items() if k in S.CONFIGS)
                                   + out["runner_usd"] + (out["D1"]["dpu_usd"] or 0.0), 3)
    return out


def _med_range(values):
    if not values:
        return None
    return {"median": statistics.median(values), "min": min(values), "max": max(values), "n": len(values)}


FIELDS = ("success_tps", "p50_ms", "p95_ms", "p99_ms", "raw_conflict_rate", "final_failure_rate")


def aggregate(results) -> list[dict]:
    groups = {}
    for r in results:
        c = r["cell"]
        groups.setdefault((c["config"], c["isolation"], c["dist"], c["concurrency"], c["retry"]), []).append(r)
    rows = []
    for (cfg, iso, dist, conc, rt), rs in sorted(groups.items()):
        ok = [r for r in rs if r["status"] == "ok"]
        row = {"config": cfg, "isolation": iso, "dist": dist, "concurrency": conc, "retry": rt, "reps": len(rs),
               "ok_reps": len(ok), "statuses": sorted({r["status"] for r in rs}),
               "violations": sum(len((r.get("invariants") or {}).get("violations", [])) for r in rs),
               "lock_waiters_max": max([(r.get("monitor") or {}).get("lock_waiters_max") or 0 for r in ok], default=None)}
        for f in FIELDS:
            row[f] = _med_range([r["metrics"][f] for r in ok if r["metrics"].get(f) is not None])
        tps = row["success_tps"]
        row["spread_over_10pct"] = bool(tps and tps["median"] and (tps["max"] - tps["min"]) / tps["median"] > 0.10)
        rows.append(row)
    return rows


def ratios(rows) -> list[dict]:
    d1 = {(r["dist"], r["concurrency"], r["retry"]): r for r in rows
          if r["config"] == "D1" and r["isolation"] == "REPEATABLE READ"}
    out = []
    for r in rows:
        key = (r["dist"], r["concurrency"], r["retry"])
        if r["config"] == "D1" or r["isolation"] != "REPEATABLE READ" or key not in d1:
            continue
        a, b = d1[key], r
        if not (a["success_tps"] and b["success_tps"] and a["p99_ms"] and b["p99_ms"]):
            continue
        div = lambda x, y: x / y if y else None  # noqa: E731 - a control with zero successes has no ratio
        out.append({"control": b["config"], "dist": key[0], "concurrency": key[1], "retry": key[2],
                    "tps_ratio_d1_over_control": div(a["success_tps"]["median"], b["success_tps"]["median"]),
                    "p99_ratio_d1_over_control": div(a["p99_ms"]["median"], b["p99_ms"]["median"])})
    return out


# ------------------------------------------------------------ AWS glue ----

def run_dir(prefix):
    return os.path.join(ART, S.validate_run_prefix(prefix))


def session(args):
    import boto3
    S.validate_account_id(args.account_id)
    sess = boto3.Session(profile_name=args.profile, region_name=args.region)
    ident = sess.client("sts").get_caller_identity()
    S.assert_identity(args.account_id, ident)
    log(f"STS identity verified for confirmed account (region {args.region})")
    return sess, ident


def load_manifest(args):
    m = S.Manifest.load(os.path.join(run_dir(args.prefix), "manifest.json"))
    m.assert_scope(args.account_id, args.region)
    return m


def _steps(m, cfg):
    return m.data.setdefault("steps", {}).setdefault(cfg, [])


def _mark(m, cfg, step):
    if step not in _steps(m, cfg):
        _steps(m, cfg).append(step)
    m.save()


def runner_alive(sess, m) -> str:
    iid = m.data.get("runner")
    res = sess.client("ec2").describe_instances(InstanceIds=[iid])["Reservations"]
    inst = res[0]["Instances"][0] if res else None
    state = inst["State"]["Name"] if inst else "missing"
    if state != "running":
        m.event(S.BATCH, "runner_lost", state=state, reason=(inst or {}).get("StateReason", {}).get("Message"))
        raise RunnerLost(f"runner is {state}; run `replace-runner`, then `cycle` again (or clean up)")
    return iid


def _ssm_ok(sess, m, cmd, timeout_s, what):
    iid = runner_alive(sess, m)
    alive = lambda: runner_alive(sess, m)  # noqa: E731
    status, out, err = with_runner_check(
        lambda: remote.ssm_run(sess.client("ssm"), iid, [cmd], timeout_s=timeout_s, check=alive), alive)
    if status != "Success":
        alive()
        raise RuntimeError(f"{what}: SSM {status}: {err[-300:]}")
    return iid, out


def _fetch(sess, m, iid, path):
    return with_runner_check(lambda: remote.fetch_file(sess.client("ssm"), iid, path), lambda: runner_alive(sess, m))


def execute_cell(sess, m, cfg, cell) -> dict:
    out_path = f"{remote.REMOTE_ROOT}/results/{cell.cell_id}.json"
    cmd = remote.runner_cmd("cell", cfg, f"--cell-json {shlex.quote(json.dumps(asdict(cell)))} --out {out_path}")
    timeout = int(L.startup_s(cell.concurrency) + cell.warmup_s + cell.measure_s + 600)
    iid, _ = _ssm_ok(sess, m, cmd, timeout, f"cell {cell.cell_id}")
    return json.loads(_fetch(sess, m, iid, out_path))


def _refresh_a2_measured(sess, m):
    for r in m.data["resources"]:
        if r["config"] == "A2" and r["type"] == "db_instance" and r["state"] != "deleted":
            until = S.utcnow() - timedelta(minutes=3)       # CloudWatch lag
            acu_h = IN.a2_acu_hours(sess, r["id"], S.parse_iso(r["recorded_at"]), until)
            r["extra"].update(measured_usd=acu_h * 0.26, measured_until=S.iso(until))
            m.save()


def _guard_fn(sess, m, cfg, args, pilot):
    def guard(cell):
        if m.expired():
            return {"ok": False, "reason": "absolute lifetime exceeded"}
        if cfg == "A2":
            _refresh_a2_measured(sess, m)
        est = pilot_cell_dpu(pilot, cell) if (cfg == "D1" and pilot) else 0.0
        g = cost.guard(m.data, cell.warmup_s + cell.measure_s + CELL_OVERHEAD_S, est,
                       dpu_price(args, cfg, pilot) if cfg == "D1" else 0.0, cap=args.budget_cap)
        m.event(cfg, "guard", cell=cell.cell_id, **g)
        return g
    return guard


def _load_json(path):
    if not os.path.exists(path):
        return None
    with open(path) as fh:
        return json.load(fh)


def do_setup(sess, m, cfg):
    _iid, out = _ssm_ok(sess, m, remote.runner_cmd("setup", cfg), 1800, "setup")
    m.event(cfg, "setup_done", runner=out.strip()[-200:])
    _mark(m, cfg, "setup")


def do_scenarios(sess, m, cfg):
    path = f"{remote.REMOTE_ROOT}/results/scenarios-{cfg}.json"
    iid, out = _ssm_ok(sess, m, remote.runner_cmd("scenarios", cfg, f"--out {path}"), 900, "scenarios")
    S.write_private(os.path.join(run_dir(m.prefix), "scenarios", f"{cfg}.json"),
                    json.loads(_fetch(sess, m, iid, path)))
    m.event(cfg, "scenarios_done", runner=out.strip()[-200:])
    _mark(m, cfg, "scenarios")


def do_pilot(sess, m, args):
    if not args.dsql_usd_per_million_dpu:
        raise S.SafetyError("--dsql-usd-per-million-dpu is required for the pilot (official price page, run date)")
    pdir = os.path.join(run_dir(m.prefix), "results", "pilot")
    results, cells = [], pilot_cells()
    start = S.utcnow()

    def execute(cell):
        res = execute_cell(sess, m, "D1", cell)
        S.write_private(os.path.join(pdir, f"{cell.cell_id}.json"), res)
        results.append(res)
    drive(cells, set(), _guard_fn(sess, m, "D1", args, None), execute)
    end = S.utcnow()
    cid = m.data["connections"]["D1"]["cluster_id"]
    dpu, last = 0.0, -1.0
    for _ in range(30):                     # wait until the CloudWatch sum is non-zero and stable
        time.sleep(30)
        dpu = IN.dsql_dpu(sess, cid, start - timedelta(minutes=1), end + timedelta(minutes=3))
        if dpu > 0 and dpu == last:
            break
        last = dpu
    est = pilot_estimate(results, dpu, L.plan_cells("D1", args.reps, args.warmup_s, args.measure_s),
                         args.dsql_usd_per_million_dpu)
    m.data["dsql_dpu_usd"] += cost.dpu_cost_usd(dpu, args.dsql_usd_per_million_dpu)
    runner_rate = next(r["extra"]["rate_usd_per_h"] for r in m.data["resources"] if r["id"] == m.data["runner"])
    est["batch_estimate"] = batch_estimate(args.reps, args.warmup_s, args.measure_s, runner_rate, est)
    est["spent_usd_now"] = round(cost.spent_usd(m.data), 3)
    est["window"] = [S.iso(start), S.iso(end)]
    S.write_private(os.path.join(run_dir(m.prefix), "pilot.json"), est)
    _mark(m, "D1", "pilot")
    print(json.dumps({k: est[k] for k in ("dpu_total", "dpu_per_attempt", "est_usd", "spent_usd_now")}, indent=2))
    print(json.dumps(est["batch_estimate"], indent=2))


def do_run(sess, m, cfg, args):
    pilot = _load_json(os.path.join(run_dir(m.prefix), "pilot.json")) if cfg == "D1" else None
    if cfg == "D1" and pilot is None:
        raise S.SafetyError("run `pilot` for D1 first")
    rdir = os.path.join(run_dir(m.prefix), "results", cfg)
    os.makedirs(rdir, exist_ok=True)
    cells = L.plan_cells(cfg, args.reps, args.warmup_s, args.measure_s)
    done = {f[:-5] for f in os.listdir(rdir) if f.endswith(".json")}
    m.event(cfg, "run_start", planned=len(cells), done=len(done))
    start = S.utcnow()

    price = dpu_price(args, cfg, pilot)

    def execute(cell):
        res = retry_once(lambda: execute_cell(sess, m, cfg, cell),
                         lambda exc: log(f"{cell.cell_id}: retrying once after {type(exc).__name__}"))
        S.write_private(os.path.join(rdir, f"{cell.cell_id}.json"), res)
        if cfg == "D1":
            attempts = (res.get("stats") or {}).get("attempts_total", 0)
            m.data["dsql_dpu_usd"] += cost.dpu_cost_usd(attempts * pilot["dpu_per_attempt"], price)
            m.save()
        met = res.get("metrics") or {}
        log(f"{cell.cell_id}: {res['status']} tps={met.get('success_tps')} p99={met.get('p99_ms')} "
            f"violations={len((res.get('invariants') or {}).get('violations', []))}")
    try:
        drive(cells, done, _guard_fn(sess, m, cfg, args, pilot), execute)
        _mark(m, cfg, "run")
    finally:
        m.event(cfg, "run_end", window=[S.iso(start), S.iso(S.utcnow())])


def do_cycle(sess, m, cfg, args):
    disc = m.data["discovery"]
    assert_config_runnable(m, cfg)
    if "provision" not in _steps(m, cfg):
        problem = lifetime_problem(m.minutes_left(), cfg, args.reps, args.warmup_s, args.measure_s)
        if problem:
            raise S.SafetyError(problem)
    keep_db = False                       # True only for: Spot interruption, or D1 paused after the pilot
    try:
        if "provision" not in _steps(m, cfg):
            IN.provision_db(sess, m, cfg, disc, args.allow_order_override)
            _mark(m, cfg, "provision")
        if "setup" not in _steps(m, cfg):
            do_setup(sess, m, cfg)
        if "scenarios" not in _steps(m, cfg):
            do_scenarios(sess, m, cfg)
        if cfg == "D1" and "pilot" not in _steps(m, cfg):
            do_pilot(sess, m, args)
            keep_db = True
            log("pilot done: review the estimate, then rerun `cycle --config D1` (D1 kept for the decision)")
            return
        do_run(sess, m, cfg, args)
    except RunnerLost:
        keep_db = True
        raise
    finally:
        if not keep_db:
            IN.cleanup(sess, m, cfg)
            log(f"{cfg} cleaned")


def summarize(prefix):
    base = run_dir(prefix)
    results = []
    for cfg in S.CONFIGS:
        d = os.path.join(base, "results", cfg)
        for f in sorted(os.listdir(d)) if os.path.isdir(d) else []:
            results.append(_load_json(os.path.join(d, f)))
    rows = aggregate(results)
    rat = ratios(rows)
    scen = {cfg: _load_json(os.path.join(base, "scenarios", f"{cfg}.json")) for cfg in S.CONFIGS}
    lines = ["## 시나리오", "", "| scenario | isolation | " + " | ".join(S.CONFIGS) + " |",
             "| --- | --- |" + " --- |" * len(S.CONFIGS)]
    keys = []
    for s in scen.values():
        for r in (s or {}).get("results", []):
            if (r["scenario"], r["isolation"]) not in keys:
                keys.append((r["scenario"], r["isolation"]))
    for sc, iso in keys:
        cells = []
        for cfg in S.CONFIGS:
            r = next((x for x in (scen[cfg] or {}).get("results", []) if (x["scenario"], x["isolation"]) == (sc, iso)),
                     None)
            cells.append("미실행" if r is None else f"{r['outcome']}/{r['judgement']}"
                         + (f" ({','.join(r['sqlstates'])})" if r["sqlstates"] else ""))
        lines.append(f"| {sc} | {iso} | " + " | ".join(cells) + " |")
    fmt = lambda v, k="median": "-" if v is None else f"{v[k]:.3g}"  # noqa: E731
    lines += ["", "## 경합 부하 (반복 중앙값, 괄호는 최소-최대)", "",
              "| config | iso | dist | conc | retry | ok/reps | TPS | p50 | p95 | p99 | 충돌률 | 최종 실패율 | 위반 | 편차>10% |",
              "| --- | --- | --- | ---: | --- | --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | --- |"]
    for r in rows:
        tps = r["success_tps"]
        lines.append(f"| {r['config']} | {L.ISOLATION_SHORT[r['isolation']]} | {r['dist']} | {r['concurrency']} | "
                     f"{r['retry']} | {r['ok_reps']}/{r['reps']} | "
                     + ("-" if tps is None else f"{fmt(tps)} ({fmt(tps, 'min')}-{fmt(tps, 'max')})")
                     + f" | {fmt(r['p50_ms'])} | {fmt(r['p95_ms'])} | {fmt(r['p99_ms'])} | "
                     f"{fmt(r['raw_conflict_rate'])} | {fmt(r['final_failure_rate'])} | {r['violations']} | "
                     f"{'예' if r['spread_over_10pct'] else ''} |")
    lines += ["", "## D1 / 대조군 비율 (RR, 중앙값 기준)", "", "| control | dist | conc | retry | TPS 비율 | p99 비율 |",
              "| --- | --- | ---: | --- | ---: | ---: |"]
    lines += [f"| {x['control']} | {x['dist']} | {x['concurrency']} | {x['retry']} | "
              + " | ".join("-" if v is None else f"{v:.3g}" for v in (x["tps_ratio_d1_over_control"],
                                                                        x["p99_ratio_d1_over_control"])) + " |"
              for x in rat]
    S.write_private(os.path.join(base, "summary.json"), {"rows": rows, "ratios": rat, "scenarios": scen})
    with open(os.path.join(base, "summary.md"), "w") as fh:
        fh.write("\n".join(lines) + "\n")
    print("\n".join(lines))


def main(argv=None):
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("command", choices=["init", "discover", "batch-up", "provision", "setup", "scenarios", "pilot",
                                        "run", "cycle", "cleanup", "batch-down", "verify", "replace-runner",
                                        "estimate", "summarize"])
    p.add_argument("--account-id")
    p.add_argument("--profile", default=S.PROFILE)
    p.add_argument("--region", default=S.REGION)
    p.add_argument("--prefix")
    p.add_argument("--config", choices=S.SCOPES)
    p.add_argument("--allow-order-override", action="store_true")
    p.add_argument("--allow-on-demand", action="store_true")
    p.add_argument("--runner-type", default=IN.RUNNER_TYPE)
    p.add_argument("--retire-live-runner", action="store_true", help="replace-runner: terminate a live runner first")
    p.add_argument("--max-lifetime-minutes", type=int, default=900)
    p.add_argument("--reps", type=int, default=3)
    p.add_argument("--warmup-s", type=int, default=30)
    p.add_argument("--measure-s", type=int, default=60)
    p.add_argument("--dsql-usd-per-million-dpu", type=float)
    p.add_argument("--budget-cap", type=float, default=cost.BUDGET_CAP_USD)
    args = p.parse_args(argv)
    if args.region != S.REGION:
        raise S.SafetyError(f"E004 is fixed to {S.REGION}")
    if args.budget_cap > cost.HARD_CAP_USD:
        raise S.SafetyError(f"--budget-cap cannot exceed the USD {cost.HARD_CAP_USD} hard cap")
    if args.command == "summarize":
        return summarize(args.prefix)
    if not args.account_id:
        p.error("--account-id is required")
    sess, ident = session(args)
    if args.command == "init":
        prefix = S.new_run_prefix()
        S.Manifest.create(os.path.join(run_dir(prefix), "manifest.json"), args.account_id, args.region, prefix,
                          args.max_lifetime_minutes, ident["Arn"])
        print(prefix)
        return
    m = load_manifest(args)
    cfg = args.config
    if args.command == "discover":
        disc = IN.discover(sess)
        S.write_private(os.path.join(run_dir(m.prefix), "discovery.json"), disc)
        print(json.dumps({k: disc[k] for k in ("common_16", "zones", "runner_az", "spot_usd_per_h")}, indent=2))
    elif args.command == "batch-up":
        disc = _load_json(os.path.join(run_dir(m.prefix), "discovery.json"))
        if disc is None:
            raise S.SafetyError("run discover first")
        try:
            IN.create_batch(sess, m, disc, args.allow_on_demand, HERE)
        except Exception:
            log("batch-up failed: deleting BATCH resources")
            IN.cleanup(sess, m, S.BATCH)
            raise
    elif args.command == "replace-runner":
        runner = m.data.get("runner")
        old = next((r for r in m.data["resources"] if r["id"] == runner), None)
        if old and IN._probe(sess, old)[0]:
            if not args.retire_live_runner:
                raise S.SafetyError("current runner is still alive (use --retire-live-runner to swap it)")
            IN.terminate_runner(sess, m, old["id"])
        elif old and old["state"] != "deleted":
            retire_runner(m, old["id"])       # terminated: stop charging it in the spend estimate
        iid = IN.launch_runner(sess, m, m.data["discovery"], args.allow_on_demand, args.runner_type)
        IN.bootstrap_runner(sess, m, iid, HERE)
    elif args.command == "estimate":
        pilot = _load_json(os.path.join(run_dir(m.prefix), "pilot.json"))
        if pilot:  # re-estimate D1 DPU for the requested cell timing
            planned = L.plan_cells("D1", args.reps, args.warmup_s, args.measure_s)
            pilot = {**pilot, "est_usd": cost.dpu_cost_usd(sum(pilot_cell_dpu(pilot, c) for c in planned),
                                                           pilot["usd_per_million_dpu"])}
        runner_rate = next((r["extra"]["rate_usd_per_h"] for r in m.data["resources"]
                            if r["id"] == m.data.get("runner")), 0.1)
        print(json.dumps({"spent_usd_now": round(cost.spent_usd(m.data), 3),
                          "batch": batch_estimate(args.reps, args.warmup_s, args.measure_s, runner_rate, pilot)},
                         indent=2))
    elif args.command == "verify":
        rep = IN.verify(sess, m)
        S.write_private(os.path.join(run_dir(m.prefix), f"verify-{rep['verified_at'].replace(':', '')}.json"), rep)
        log(f"remaining_count={rep['remaining_count']} (tag index ARNs for review: "
            f"{len(rep['tag_index_arns_for_review'])})")
        sys.exit(0 if rep["remaining_count"] == 0 else 2)
    elif args.command == "batch-down":
        IN.cleanup(sess, m, S.BATCH)
        rep = IN.verify(sess, m)
        S.write_private(os.path.join(run_dir(m.prefix), f"verify-{rep['verified_at'].replace(':', '')}.json"), rep)
        log(f"remaining_count={rep['remaining_count']}")
        sys.exit(0 if rep["remaining_count"] == 0 else 2)
    elif not cfg:
        p.error("--config is required")
    elif args.command == "cleanup":
        IN.cleanup(sess, m, cfg)
    elif args.command == "provision":
        IN.provision_db(sess, m, cfg, m.data["discovery"], args.allow_order_override)
        _mark(m, cfg, "provision")
    elif args.command == "setup":
        do_setup(sess, m, cfg)
    elif args.command == "scenarios":
        do_scenarios(sess, m, cfg)
    elif args.command == "pilot":
        do_pilot(sess, m, args)
    elif args.command == "run":
        do_run(sess, m, cfg, args)
    elif args.command == "cycle":
        try:
            do_cycle(sess, m, cfg, args)
        except RunnerLost as exc:
            log(str(exc))
            sys.exit(3)


if __name__ == "__main__":
    main()
