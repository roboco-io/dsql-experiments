"""E007 MVP (2026-09-29): backup, restore and a mistake, on the small D1/A2 run B.

D1 (DSQL) has no point-in-time restore: an AWS Backup full backup is taken after the good markers and
before the mistake, then restored to a new cluster. A2 (Aurora Serverless v2) is restored to a point in
time between the last good marker and the mistake. Each restored copy is verified from the runner.

Restored clusters, instances and the backup IAM role are recorded in the run's manifest, so
`e002.py batch-down` deletes them. The backup vault and its recovery points are not manifest types:
this script deletes them itself (finally block) and `cleanup_backup` can be rerun on its own.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
from datetime import datetime, timedelta

import e002 as E
import infra as IN
import mvp_run as MR
import safety as S
from infra import log

BACKUP_POLICIES = ("arn:aws:iam::aws:policy/service-role/AWSBackupServiceRolePolicyForBackup",
                   "arn:aws:iam::aws:policy/service-role/AWSBackupServiceRolePolicyForRestores")


def names(prefix):
    return {"vault": f"{prefix}-vault", "role": f"{prefix}-backup"}


def ensure_backup_role(sess, m) -> str:
    iam, role = sess.client("iam"), names(m.prefix)["role"]
    tags = S.resource_tags(m.prefix, S.BATCH, m.data["expires_at"])
    if not m.find(S.BATCH, "iam_role", role):
        m.add_resource(S.BATCH, "iam_role", role, state="requested", purpose="e007-backup")
        iam.create_role(RoleName=role, Description="E007 AWS Backup (temporary)", Tags=S.tag_list(tags),
                        AssumeRolePolicyDocument=json.dumps({"Version": "2012-10-17", "Statement": [{
                            "Effect": "Allow", "Principal": {"Service": "backup.amazonaws.com"},
                            "Action": "sts:AssumeRole"}]}))
        m.set_state(S.BATCH, "iam_role", role, "created")
        for arn in BACKUP_POLICIES:
            iam.attach_role_policy(RoleName=role, PolicyArn=arn)
        time.sleep(15)                     # IAM propagation
    return iam.get_role(RoleName=role)["Role"]["Arn"]


def ensure_vault(sess, m) -> str:
    b, vault = sess.client("backup"), names(m.prefix)["vault"]
    try:
        b.describe_backup_vault(BackupVaultName=vault)
    except Exception as exc:  # noqa: BLE001 - a missing vault answers AccessDeniedException, not NotFound
        if IN.code(exc) not in ("AccessDeniedException", "ResourceNotFoundException"):
            raise
        b.create_backup_vault(BackupVaultName=vault,
                              BackupVaultTags=S.resource_tags(m.prefix, S.BATCH, m.data["expires_at"]))
        m.event(S.BATCH, "e007", note=f"backup vault {vault} created")
    return vault


def cleanup_backup(sess, m) -> dict:
    """Delete every recovery point in this run's vault, then the vault. Safe to rerun."""
    b, vault = sess.client("backup"), names(m.prefix)["vault"]
    try:
        rps = b.list_recovery_points_by_backup_vault(BackupVaultName=vault)["RecoveryPoints"]
    except Exception as exc:  # noqa: BLE001 - a missing vault answers AccessDeniedException, not NotFound
        if IN.code(exc) not in ("AccessDeniedException", "ResourceNotFoundException"):
            raise
        return {"vault": "absent"}
    for rp in rps:
        b.delete_recovery_point(BackupVaultName=vault, RecoveryPointArn=rp["RecoveryPointArn"])
    IN.wait_until(lambda: not b.list_recovery_points_by_backup_vault(BackupVaultName=vault)["RecoveryPoints"],
                 "recovery points deleted", 1800, 15)
    b.delete_backup_vault(BackupVaultName=vault)
    m.event(S.BATCH, "e007", note=f"backup vault {vault} and {len(rps)} recovery points deleted")
    return {"vault": "deleted", "recovery_points_deleted": len(rps)}


def _wait_job(fetch, done, what, limit_s=7200):
    t0 = time.monotonic()
    while time.monotonic() - t0 < limit_s:
        j = fetch()
        if j["Status"] in done:
            return j, round(time.monotonic() - t0, 1)
        if j["Status"] in ("FAILED", "ABORTED", "EXPIRED"):
            raise RuntimeError(f"{what} {j['Status']}: {j.get('StatusMessage', '')[:200]}")
        time.sleep(15)
    raise RuntimeError(f"{what} timed out")


def d1_backup(sess, m, role_arn, vault):
    b = sess.client("backup")
    arn = next(r["extra"]["arn"] for r in m.data["resources"]
               if r["config"] == "D1" and r["type"] == "dsql_cluster" and r["state"] != "deleted")
    job = b.start_backup_job(BackupVaultName=vault, ResourceArn=arn, IamRoleArn=role_arn)["BackupJobId"]
    j, secs = _wait_job(lambda: b.describe_backup_job(BackupJobId=job), ("COMPLETED",), "backup")
    return {"backup_s": secs, "recovery_point": j["RecoveryPointArn"], "backup_size_bytes": j.get("BackupSizeInBytes")}


def d1_restore(sess, m, role_arn, recovery_point):
    b, dsql = sess.client("backup"), sess.client("dsql")
    meta = {"regionalConfig": json.dumps([{"region": m.data["region"], "isDeletionProtectionEnabled": False}])}
    t0 = time.monotonic()
    job = b.start_restore_job(RecoveryPointArn=recovery_point, IamRoleArn=role_arn, Metadata=meta,
                              CopySourceTagsToRestoredResource=True)["RestoreJobId"]
    j, _ = _wait_job(lambda: b.describe_restore_job(RestoreJobId=job), ("COMPLETED",), "restore")
    arn = j["CreatedResourceArn"]
    cid = arn.rsplit("/", 1)[-1]
    m.add_resource("D1", "dsql_cluster", cid, state="created", arn=arn, restored=True)
    got = IN.wait_until(lambda: (lambda c: c if c["status"] in ("ACTIVE", "IDLE") else None)(
        dsql.get_cluster(identifier=cid)), "restored DSQL ACTIVE", 3600)
    IN.update_runner_policy(sess, m)
    return {"restore_s": round(time.monotonic() - t0, 1),
            "host": got.get("endpoint") or f"{cid}.dsql.{m.data['region']}.on.aws"}


def a2_pitr(sess, m, restore_to: datetime):
    rds = sess.client("rds")
    src = rds.describe_db_clusters(DBClusterIdentifier=m.data["connections"]["A2"]["cluster_id"])["DBClusters"][0]
    t_wait = time.monotonic()
    IN.wait_until(lambda: rds.describe_db_clusters(DBClusterIdentifier=src["DBClusterIdentifier"])[
        "DBClusters"][0]["LatestRestorableTime"] >= restore_to, "restorable time reaches the target", 1800, 15)
    waited = round(time.monotonic() - t_wait, 1)
    cid, iid = f"{src['DBClusterIdentifier']}-pitr", f"{src['DBClusterIdentifier']}-pitr-w"
    tags = S.tag_list(S.resource_tags(m.prefix, "A2", m.data["expires_at"]))
    t0 = time.monotonic()
    m.add_resource("A2", "db_cluster", cid, state="requested", restored=True)
    rds.restore_db_cluster_to_point_in_time(
        SourceDBClusterIdentifier=src["DBClusterIdentifier"], DBClusterIdentifier=cid, RestoreToTime=restore_to,
        DBSubnetGroupName=src["DBSubnetGroup"], VpcSecurityGroupIds=[g["VpcSecurityGroupId"] for g in
                                                                     src["VpcSecurityGroups"]],
        ServerlessV2ScalingConfiguration={"MinCapacity": float(IN.A2_ACU[0]), "MaxCapacity": float(IN.A2_ACU[1])},
        ManageMasterUserPassword=True, DeletionProtection=False, Tags=tags)
    m.set_state("A2", "db_cluster", cid, "created")
    m.add_resource("A2", "db_instance", iid, state="requested", cluster_member=True, restored=True,
                   rate_usd_per_h=IN.db_rate("A2"))
    IN.wait_until(lambda: rds.describe_db_clusters(DBClusterIdentifier=cid)["DBClusters"][0]["Status"]
                 == "available", "restored cluster available", 7200, 20)
    cluster_s = round(time.monotonic() - t0, 1)
    rds.create_db_instance(DBInstanceIdentifier=iid, DBClusterIdentifier=cid, Engine=src["Engine"],
                           DBInstanceClass="db.serverless", Tags=tags)
    m.set_state("A2", "db_instance", iid, "created")
    IN.wait_available_instance(rds, iid)
    c = rds.describe_db_clusters(DBClusterIdentifier=cid)["DBClusters"][0]
    secret = c["MasterUserSecret"]["SecretArn"]
    m.add_resource("A2", "rds_secret", secret, state="created", restored=True)
    IN.update_runner_policy(sess, m)
    return {"wait_restorable_s": waited, "cluster_available_s": cluster_s,
            "restore_s": round(time.monotonic() - t0, 1), "host": c["Endpoint"], "secret_arn": secret}


def run(sess, m, cap):
    out = {"started": S.iso(S.utcnow())}
    vault = None
    try:
        if not MR._guard_ok(sess, m, cap, 3.0):
            return 1
        MR.push_code(sess, m, ["D1", "A2"])
        role_arn = ensure_backup_role(sess, m)
        vault = ensure_vault(sess, m)
        marks = E.parallel(["D1", "A2"], lambda cfg: MR.probe(sess, m, cfg, "e007-mark", 0.02, 900))
        if E._print_outcomes("e007-mark", marks):
            return 1
        out["marks"] = {cfg: o["value"] for cfg, o in marks.items()}
        out["d1_backup"] = d1_backup(sess, m, role_arn, vault)          # before the mistake: no PITR on DSQL
        mistakes = E.parallel(["D1", "A2"], lambda cfg: MR.probe(sess, m, cfg, "e007-mistake", 0.02, 600))
        out["mistakes"] = {cfg: o["value"] for cfg, o in mistakes.items()}
        good = datetime.fromisoformat(out["marks"]["A2"]["last_good_at"])
        bad = datetime.fromisoformat(out["mistakes"]["A2"]["mistake_at"])
        restore_to = good + (bad - good) / 2

        def restore(cfg):
            if cfg == "D1":
                r = d1_restore(sess, m, role_arn, out["d1_backup"]["recovery_point"])
                v = MR.probe(sess, m, "D1", "e007-verify", 0.02, 900, host=r["host"])
            else:
                r = a2_pitr(sess, m, restore_to)
                v = MR.probe(sess, m, "A2", "e007-verify", 0.02, 900, host=r["host"], secret_arn=r["secret_arn"])
            return {**{k: val for k, val in r.items() if k not in ("host", "secret_arn")}, "verify": v}
        res = E.parallel(["D1", "A2"], restore)
        E._print_outcomes("e007-restore", res)
        out["restore"] = {cfg: o.get("value") or {"error": o.get("error")} for cfg, o in res.items()}
        out["restore_to"] = restore_to.isoformat()
    finally:
        out["backup_cleanup"] = cleanup_backup(sess, m)
        out["finished"] = S.iso(S.utcnow())
        path = os.path.join(E.run_dir(m.prefix), "mvp", "e007.json")
        os.makedirs(os.path.dirname(path), exist_ok=True)
        S.write_private(path, out)
        log("e007 saved")
    return 0


def main(argv=None):
    p = argparse.ArgumentParser()
    p.add_argument("--account-id", required=True)
    p.add_argument("--profile", default=S.PROFILE)
    p.add_argument("--prefix", required=True)
    p.add_argument("--cap", type=float, required=True)
    a = p.parse_args(argv)
    args = MR._args(a.prefix, a.account_id, a.profile)
    sess, _ = E.session(args)
    return run(sess, E.load_manifest(args), a.cap)


if __name__ == "__main__":
    sys.exit(main())
