"""E002 AWS operations (operator side): discovery, BATCH/DB provisioning, cleanup, verification, CloudWatch.

Only resources recorded in this run's manifest AND carrying this run's tags are deleted. RDS-managed secrets
carry RDS's own tags, so their ownership is checked through the primary DB ARN tag (secret_owned).
"""
from __future__ import annotations

import json
import re
import threading
import time

import cost
import remote
import safety as S

ENGINE = {"R1": "postgres", "A1": "aurora-postgresql", "A2": "aurora-postgresql"}
DB_CLASS = {"R1": "db.r6g.xlarge", "A1": "db.r6g.xlarge", "A2": "db.serverless"}
A2_ACU = (4, 32)
RUNNER_TYPE = "c7g.4xlarge"
VPC_CIDR_PREFIX = "10.92"
DB_USER, DB_NAME = "e002admin", "e002"
AMI_PARAM = "/aws/service/ami-amazon-linux-latest/al2023-ami-kernel-default-arm64"
SSM_POLICY_ARN = "arn:aws:iam::aws:policy/AmazonSSMManagedInstanceCore"
DB_POLICY_NAME = "e002-db-access"
SYSTEM_CA = "/etc/pki/tls/certs/ca-bundle.crt"          # AL2023 system bundle (includes Amazon roots) for DSQL
RDS_CA_URL = "https://truststore.pki.rds.amazonaws.com/global/global-bundle.pem"
SPOT_CAPACITY = {"InsufficientInstanceCapacity", "SpotMaxPriceTooLow", "MaxSpotInstanceCountExceeded",
                 "InsufficientCapacity"}
NOT_FOUND = {"ResourceNotFoundException", "DBInstanceNotFound", "DBInstanceNotFoundFault",
             "DBClusterNotFoundFault", "DBSubnetGroupNotFoundFault", "InvalidGroup.NotFound",
             "InvalidSubnetID.NotFound", "InvalidInternetGatewayID.NotFound", "InvalidVpcID.NotFound",
             "DBSnapshotNotFound", "DBClusterSnapshotNotFoundFault", "DBInstanceAutomatedBackupNotFound",
             "DBClusterAutomatedBackupNotFoundFault", "NoSuchEntity", "InvalidInstanceID.NotFound"}
RETRY_DELETE = {"DependencyViolation", "InvalidDBSubnetGroupStateFault", "InvalidDBClusterStateFault",
                "InvalidDBInstanceState", "InvalidDBInstanceStateFault", "DeleteConflict"}


def log(msg):
    print(f"[{S.iso(S.utcnow())}] {msg}", flush=True)


def code(exc):
    return getattr(exc, "response", {}).get("Error", {}).get("Code", "")


def wait_until(fn, what, timeout_s, interval=15):
    deadline = time.monotonic() + timeout_s
    while True:
        result = fn()
        if result:
            return result
        if time.monotonic() >= deadline:
            raise TimeoutError(f"timed out waiting for {what}")
        time.sleep(interval)


# ---------------------------------------------------------------- pure ----

def trust_policy() -> dict:
    return {"Version": "2012-10-17", "Statement": [{"Effect": "Allow", "Principal": {"Service": "ec2.amazonaws.com"},
                                                    "Action": "sts:AssumeRole"}]}


def runner_policy(dsql_arns, secret_arns):
    st = []
    if dsql_arns:
        st.append({"Effect": "Allow", "Action": ["dsql:DbConnectAdmin"], "Resource": sorted(dsql_arns)})
    if secret_arns:
        st.append({"Effect": "Allow", "Action": ["secretsmanager:GetSecretValue"], "Resource": sorted(secret_arns)})
    return {"Version": "2012-10-17", "Statement": st} if st else None


def launch_params(ami, itype, subnet, sg, profile, tags, spot) -> dict:
    rtypes = ("instance", "volume", "network-interface") + (("spot-instances-request",) if spot else ())
    p = dict(ImageId=ami, InstanceType=itype, MinCount=1, MaxCount=1, IamInstanceProfile={"Name": profile},
             NetworkInterfaces=[{"DeviceIndex": 0, "SubnetId": subnet, "Groups": [sg],
                                 "AssociatePublicIpAddress": True, "DeleteOnTermination": True}],
             MetadataOptions={"HttpTokens": "required", "HttpEndpoint": "enabled"},
             BlockDeviceMappings=[{"DeviceName": "/dev/xvda", "Ebs": {"VolumeSize": 8, "VolumeType": "gp3",
                                                                      "DeleteOnTermination": True,
                                                                      "Encrypted": True}}],
             InstanceInitiatedShutdownBehavior="terminate",
             TagSpecifications=[{"ResourceType": rt, "Tags": S.tag_list(tags)} for rt in rtypes])
    if spot:
        p["InstanceMarketOptions"] = {"MarketType": "spot", "SpotOptions": {
            "SpotInstanceType": "one-time", "InstanceInterruptionBehavior": "terminate"}}
    return p


def choose_zones(db_az_sets, spot_prices) -> dict:
    common = set.intersection(*map(set, db_az_sets)) if db_az_sets else set()
    ranked = sorted((az for az in common if az in spot_prices), key=lambda az: (spot_prices[az], az))
    if len(ranked) < 2:
        raise S.SafetyError("need two AZs that support every DB config and the Spot runner type")
    return {"zones": ranked[:2], "runner_az": ranked[0], "spot_usd_per_h": spot_prices[ranked[0]]}


def spot_price_query(itype, az) -> dict:
    return {"InstanceTypes": [itype], "ProductDescriptions": ["Linux/UNIX"], "AvailabilityZone": az,
            "StartTime": S.utcnow()}


def spot_price_for_az(history, az) -> float:
    """history: describe_spot_price_history items, newest first."""
    for h in history:
        if h["AvailabilityZone"] == az:
            return float(h["SpotPrice"])
    raise S.SafetyError(f"no Spot price for {az}")


def db_rate(cfg) -> float:
    """Guard rate for one DB instance of the config (A1/A2 have two instances, each recorded separately)."""
    return cost.DB_RATE_USD_PER_H[{"R1": "R1", "A1": "A1_instance", "A2": "A2_instance_worst"}[cfg]]


def r1_params(prefix, ch, common, inst) -> dict:
    return dict(DBInstanceIdentifier=f"{prefix}-r1", DBInstanceClass=ch["class"], DBName=DB_NAME,
                AllocatedStorage=400, StorageType="gp3", Iops=12000, StorageThroughput=500, MultiAZ=True,
                BackupRetentionPeriod=0, **common, **inst)


def aurora_cluster_params(cfg, prefix, ch, common) -> dict:
    p = dict(DBClusterIdentifier=f"{prefix}-{cfg.lower()}", DatabaseName=DB_NAME, BackupRetentionPeriod=1,
             StorageType="aurora", **common)
    if cfg == "A2":
        p["ServerlessV2ScalingConfiguration"] = {"MinCapacity": A2_ACU[0], "MaxCapacity": A2_ACU[1]}
    return p


def aurora_instance_params(cfg, prefix, role, ch, tags, inst, az) -> dict:
    """Writer (tier 0) and reader (tier 1, the other AZ); A2's tier-1 reader follows the writer's capacity."""
    return dict(DBInstanceIdentifier=f"{prefix}-{cfg.lower()}-{'w1' if role == 'writer' else 'r1'}",
                DBClusterIdentifier=f"{prefix}-{cfg.lower()}", Engine=ch["engine"], DBInstanceClass=ch["class"],
                AvailabilityZone=az, PromotionTier=0 if role == "writer" else 1, Tags=S.tag_list(tags), **inst)


def secret_state(detail) -> str:
    """RDS deletes (or schedules deletion of) its managed secret with the DB; a scheduled one counts as deleted."""
    if not detail:
        return "gone"
    return "pending" if detail.get("DeletedDate") else "live"


def secret_owned(detail: dict, prefix: str) -> bool:
    if (detail or {}).get("OwningService") != "rds":
        return False
    return any(t["Key"].startswith("aws:rds:primaryDB") and f":{prefix}-" in t["Value"]
               for t in detail.get("Tags", []))


# ------------------------------------------------------------ discovery ---

def _vkey(v):
    return tuple(int(x) for x in v.split("."))


def discover(sess) -> dict:
    rds, ec2 = sess.client("rds"), sess.client("ec2")
    versions = {}
    for engine in set(ENGINE.values()):
        vs = set()
        for page in rds.get_paginator("describe_db_engine_versions").paginate(Engine=engine):
            for v in page["DBEngineVersions"]:
                if re.fullmatch(r"16\.\d+", v["EngineVersion"]) and v.get("Status", "available") == "available":
                    vs.add(v["EngineVersion"])
        versions[engine] = sorted(vs, key=_vkey, reverse=True)
    common = sorted(set(versions["postgres"]) & set(versions["aurora-postgresql"]), key=_vkey, reverse=True)
    choice = {}
    for cfg, engine in ENGINE.items():
        for ver in common + [v for v in versions[engine] if v not in common]:
            opts = [o for page in rds.get_paginator("describe_orderable_db_instance_options").paginate(
                        Engine=engine, EngineVersion=ver, DBInstanceClass=DB_CLASS[cfg], Vpc=True)
                    for o in page["OrderableDBInstanceOptions"]]
            if cfg == "R1":
                opts = [o for o in opts if o.get("StorageType") == "gp3" and o.get("MultiAZCapable")]
            else:
                opts = [o for o in opts if o.get("StorageType") == "aurora"]      # Aurora Standard
            if opts:
                choice[cfg] = {"engine": engine, "version": ver, "class": DB_CLASS[cfg], "common_minor": ver in common,
                               "azs": sorted({az["Name"] for o in opts for az in o.get("AvailabilityZones", [])})}
                break
        if cfg not in choice:
            raise S.SafetyError(f"{cfg}: no orderable PostgreSQL 16 option for {DB_CLASS[cfg]}")
    spot = {}
    for h in ec2.describe_spot_price_history(InstanceTypes=[RUNNER_TYPE], ProductDescriptions=["Linux/UNIX"],
                                             StartTime=S.utcnow())["SpotPriceHistory"]:
        spot.setdefault(h["AvailabilityZone"], float(h["SpotPrice"]))
    zones = choose_zones([c["azs"] for c in choice.values()], spot)
    return {"discovered_at": S.iso(S.utcnow()), "versions": versions, "common_16": common, "choice": choice,
            "spot_prices": spot, **zones}


def ec2_on_demand_rate(sess, itype=RUNNER_TYPE) -> float:
    pr = sess.client("pricing", region_name="us-east-1")
    flt = {"regionCode": S.REGION, "instanceType": itype, "operatingSystem": "Linux", "tenancy": "Shared",
           "preInstalledSw": "NA", "capacitystatus": "Used"}
    items = pr.get_products(ServiceCode="AmazonEC2", Filters=[{"Type": "TERM_MATCH", "Field": k, "Value": v}
                                                              for k, v in flt.items()])["PriceList"]
    for raw in items:
        for term in json.loads(raw)["terms"].get("OnDemand", {}).values():
            for dim in term["priceDimensions"].values():
                return float(dim["pricePerUnit"]["USD"])
    raise RuntimeError("On-Demand price not found for the runner type")


# ---------------------------------------------------------- provisioning --

def _one(m, rtype):
    ids = [r["id"] for r in m.data["resources"] if r["type"] == rtype and r["state"] != "deleted"]
    if len(ids) != 1:
        raise S.SafetyError(f"expected exactly one live {rtype}, found {len(ids)}")
    return ids[0]


def create_batch(sess, m, disc) -> None:
    """Network and IAM shared by all configs. Runners belong to each config (launch_runner)."""
    S.check_can_provision(m.data, S.BATCH)
    tags = S.resource_tags(m.prefix, S.BATCH, m.data["expires_at"])
    ec2, rds, iam = sess.client("ec2"), sess.client("rds"), sess.client("iam")
    m.data["config_status"][S.BATCH] = "provisioning"
    m.data["discovery"] = disc
    m.event(S.BATCH, "provision_start", zones=disc["zones"], runner_az=disc["runner_az"])

    def spec(rt):
        return [{"ResourceType": rt, "Tags": S.tag_list(tags)}]
    vpc = ec2.create_vpc(CidrBlock=f"{VPC_CIDR_PREFIX}.0.0/16", TagSpecifications=spec("vpc"))["Vpc"]["VpcId"]
    m.add_resource(S.BATCH, "vpc", vpc, state="created")
    ec2.get_waiter("vpc_available").wait(VpcIds=[vpc])
    ec2.modify_vpc_attribute(VpcId=vpc, EnableDnsSupport={"Value": True})
    ec2.modify_vpc_attribute(VpcId=vpc, EnableDnsHostnames={"Value": True})
    subnets = []
    for i, az in enumerate(disc["zones"]):
        sid = ec2.create_subnet(VpcId=vpc, CidrBlock=f"{VPC_CIDR_PREFIX}.{i}.0/24", AvailabilityZone=az,
                                TagSpecifications=spec("subnet"))["Subnet"]["SubnetId"]
        m.add_resource(S.BATCH, "subnet", sid, state="created", az=az)
        subnets.append(sid)
    igw = ec2.create_internet_gateway(TagSpecifications=spec("internet-gateway"))["InternetGateway"][
        "InternetGatewayId"]
    m.add_resource(S.BATCH, "internet_gateway", igw, state="created", vpc=vpc)
    ec2.attach_internet_gateway(InternetGatewayId=igw, VpcId=vpc)
    rt = ec2.describe_route_tables(Filters=[{"Name": "vpc-id", "Values": [vpc]},
                                            {"Name": "association.main", "Values": ["true"]}])["RouteTables"][0]
    ec2.create_route(RouteTableId=rt["RouteTableId"], DestinationCidrBlock="0.0.0.0/0", GatewayId=igw)
    client_sg = ec2.create_security_group(GroupName=f"{m.prefix}-client", VpcId=vpc,
                                          Description="E002 runners: no inbound rules",
                                          TagSpecifications=spec("security-group"))["GroupId"]
    m.add_resource(S.BATCH, "client_security_group", client_sg, state="created")
    db_sg = ec2.create_security_group(GroupName=f"{m.prefix}-db", VpcId=vpc,
                                      Description="E002 DB: PostgreSQL from the runner SG only",
                                      TagSpecifications=spec("security-group"))["GroupId"]
    m.add_resource(S.BATCH, "db_security_group", db_sg, state="created")
    ec2.authorize_security_group_ingress(GroupId=db_sg, IpPermissions=[{
        "IpProtocol": "tcp", "FromPort": 5432, "ToPort": 5432,
        "UserIdGroupPairs": [{"GroupId": client_sg, "Description": "E002 runner"}]}])
    sng = f"{m.prefix}-db"
    m.add_resource(S.BATCH, "db_subnet_group", sng, state="requested")
    rds.create_db_subnet_group(DBSubnetGroupName=sng, DBSubnetGroupDescription="E002 temporary",
                               SubnetIds=subnets, Tags=S.tag_list(tags))
    m.set_state(S.BATCH, "db_subnet_group", sng, "created")
    role = f"{m.prefix}-runner"
    m.add_resource(S.BATCH, "iam_role", role, state="requested")
    iam.create_role(RoleName=role, AssumeRolePolicyDocument=json.dumps(trust_policy()),
                    Description="E002 runner (temporary)", Tags=S.tag_list(tags))
    m.set_state(S.BATCH, "iam_role", role, "created")
    iam.attach_role_policy(RoleName=role, PolicyArn=SSM_POLICY_ARN)
    m.add_resource(S.BATCH, "instance_profile", role, state="requested")
    iam.create_instance_profile(InstanceProfileName=role, Tags=S.tag_list(tags))
    m.set_state(S.BATCH, "instance_profile", role, "created")
    iam.add_role_to_instance_profile(InstanceProfileName=role, RoleName=role)
    m.data["config_status"][S.BATCH] = "ready"
    m.event(S.BATCH, "provision_done")


def runner_types(spec: str) -> list[str]:
    """--runner-type accepts a comma list: Spot is tried type by type before any On-Demand fallback."""
    return [t.strip() for t in spec.split(",") if t.strip()]


def launch_runner(sess, m, disc, allow_on_demand, cfg, itype=RUNNER_TYPE) -> str:
    ec2, ssm = sess.client("ec2"), sess.client("ssm")
    tags = S.resource_tags(m.prefix, cfg, m.data["expires_at"])
    ami = ssm.get_parameter(Name=AMI_PARAM)["Parameter"]["Value"]
    subnet = next(r["id"] for r in m.data["resources"] if r["type"] == "subnet" and r["state"] != "deleted"
                  and r["extra"].get("az") == disc["runner_az"])
    sg, profile = _one(m, "client_security_group"), _one(m, "instance_profile")
    types = runner_types(itype)
    itype = types.pop(0)
    spot, inst = True, None
    for _ in range(12):
        try:
            inst = ec2.run_instances(**launch_params(ami, itype, subnet, sg, profile, tags, spot))[
                "Instances"][0]
            break
        except Exception as exc:  # noqa: BLE001
            c = code(exc)
            if c == "InvalidParameterValue" and "profile" in str(exc).lower():
                time.sleep(10)          # instance profile not yet visible to EC2
                continue
            if spot and c in SPOT_CAPACITY and types:
                m.event(cfg, "deviation", note=f"Spot {itype} unavailable ({c}); trying {types[0]}")
                itype = types.pop(0)
                continue
            if spot and c in SPOT_CAPACITY:
                if not allow_on_demand:
                    raise S.SafetyError(f"Spot capacity unavailable ({c}); rerun with --allow-on-demand") from None
                spot = False
                m.event(cfg, "deviation", note=f"Spot unavailable ({c}); On-Demand runner")
                continue
            raise
    if inst is None:
        raise RuntimeError("runner launch retries exhausted")
    iid = inst["InstanceId"]
    # Record first, so a failure below can never leave an unrecorded runner behind.
    m.add_resource(cfg, "ec2_instance", iid, state="created", market="spot" if spot else "on-demand",
                   instance_type=itype, spot_request=inst.get("SpotInstanceRequestId"), az=disc["runner_az"],
                   rate_usd_per_h=disc["spot_usd_per_h"] + cost.PUBLIC_IPV4_USD_PER_H + cost.EBS_ROOT_USD_PER_H)
    with m.lock:
        m.data.setdefault("runners", {})[cfg] = iid
        m.save()
    try:
        base = (spot_price_for_az(ec2.describe_spot_price_history(**spot_price_query(itype, disc["runner_az"]))[
            "SpotPriceHistory"], disc["runner_az"]) if spot else ec2_on_demand_rate(sess, itype))
        m.add_resource(cfg, "ec2_instance", iid,
                       rate_usd_per_h=base + cost.PUBLIC_IPV4_USD_PER_H + cost.EBS_ROOT_USD_PER_H)
    except Exception as exc:  # noqa: BLE001 - keep the discovery-time rate; the runner is already recorded
        log(f"runner price lookup failed ({type(exc).__name__}); keeping discovery-time rate")
    ec2.get_waiter("instance_running").wait(InstanceIds=[iid])
    wait_until(lambda: any(i["PingStatus"] == "Online" for i in ssm.describe_instance_information(
        Filters=[{"Key": "InstanceIds", "Values": [iid]}])["InstanceInformationList"]), "runner SSM online", 900)
    log(f"{cfg} runner {'spot' if spot else 'on-demand'} online")
    return iid


def terminate_runner(sess, m, cfg, iid) -> None:
    """Terminate one live runner of this run (ownership-checked) and wait until it is gone."""
    r = m.find(cfg, "ec2_instance", iid)
    exists, tags, detail = _probe(sess, r)
    if exists:
        if not S.owned(tags, m.prefix, cfg):
            raise S.SafetyError(f"{iid} lacks this run's tags; refusing to terminate")
        _delete(sess, r, detail)
        wait_until(lambda: not _probe(sess, r)[0], f"{iid} terminated", 900, 15)
    m.set_state(cfg, "ec2_instance", iid, "deleted", retired=True)


def bootstrap_runner(sess, m, cfg, iid, root) -> None:
    ssm, r = sess.client("ssm"), remote.REMOTE_ROOT
    remote._check(remote.ssm_run(ssm, iid, [
        "set -eu", "dnf install -y -q python3.11 python3.11-pip", f"mkdir -p {r}/results",
        f"python3.11 -m venv {r}/venv", f"curl -fsSo {r}/rds-ca.pem {RDS_CA_URL}", f"test -s {SYSTEM_CA}",
    ], timeout_s=900), "bootstrap")
    remote.push_bundle(ssm, iid, root)
    out = remote._check(remote.ssm_run(ssm, iid, [
        "set -eu", f"{r}/venv/bin/pip install -q -r {r}/code/requirements.txt",
        f"{r}/venv/bin/python -c 'import sys, psycopg, boto3; "
        "print(sys.version.split()[0], psycopg.__version__, psycopg.pq.version(), boto3.__version__)'",
        "nproc", "uname -m",
    ], timeout_s=900), "pip install")
    m.event(cfg, "runner_bootstrapped", instance=iid, versions=out.strip()[-300:])
    if cfg in m.data.get("targets", {}):
        push_target(sess, m, cfg)


def wait_available_instance(rds, iid):
    def ready():
        d = rds.describe_db_instances(DBInstanceIdentifier=iid)["DBInstances"][0]
        return d if d["DBInstanceStatus"] == "available" and d.get("Endpoint") else None
    return wait_until(ready, f"{iid} available", 3600, 30)


def provision_db(sess, m, cfg, disc) -> None:
    S.check_can_provision(m.data, cfg)
    if m.minutes_left() < 150:
        raise S.SafetyError("less than 150 minutes of absolute lifetime left; start a new prefix")
    tags = S.resource_tags(m.prefix, cfg, m.data["expires_at"])
    m.data["config_status"][cfg] = "provisioning"
    m.event(cfg, "provision_start")
    if cfg == "D1":
        dsql = sess.client("dsql")
        r = dsql.create_cluster(deletionProtectionEnabled=False, tags=tags, clientToken=f"{m.prefix}-d1")
        m.add_resource(cfg, "dsql_cluster", r["identifier"], state="created", arn=r["arn"])
        got = wait_until(lambda: (lambda c: c if c["status"] in ("ACTIVE", "IDLE") else None)(
            dsql.get_cluster(identifier=r["identifier"])), "DSQL ACTIVE", 1800)
        host = got.get("endpoint") or f"{r['identifier']}.dsql.{m.data['region']}.on.aws"
        target = {"config": cfg, "kind": "dsql", "host": host, "dbname": "postgres", "user": "admin",
                  "region": m.data["region"], "sslrootcert": SYSTEM_CA}
        m.data["connections"][cfg] = {"cluster_id": r["identifier"]}
    else:
        rds, ch = sess.client("rds"), disc["choice"][cfg]
        common = dict(Engine=ch["engine"], EngineVersion=ch["version"], MasterUsername=DB_USER,
                      ManageMasterUserPassword=True, DBSubnetGroupName=_one(m, "db_subnet_group"),
                      VpcSecurityGroupIds=[_one(m, "db_security_group")], DeletionProtection=False,
                      StorageEncrypted=True, CopyTagsToSnapshot=False, Tags=S.tag_list(tags))
        inst = dict(PubliclyAccessible=False, EnablePerformanceInsights=False, MonitoringInterval=0,
                    AutoMinorVersionUpgrade=False)
        if cfg == "R1":
            params = r1_params(m.prefix, ch, common, inst)
            iid = params["DBInstanceIdentifier"]
            m.add_resource(cfg, "db_instance", iid, state="requested", cluster_member=False, rate_usd_per_h=db_rate(cfg))
            rds.create_db_instance(**params)
            m.set_state(cfg, "db_instance", iid, "created")
            d = wait_available_instance(rds, iid)
            host, secret = d["Endpoint"]["Address"], d["MasterUserSecret"]["SecretArn"]
            m.data["connections"][cfg] = {"instance_id": iid, "engine_version": ch["version"], "class": ch["class"],
                                          "az": d.get("AvailabilityZone"),
                                          "secondary_az": d.get("SecondaryAvailabilityZone")}
        else:
            cp = aurora_cluster_params(cfg, m.prefix, ch, common)
            cid = cp["DBClusterIdentifier"]
            m.add_resource(cfg, "db_cluster", cid, state="requested")
            rds.create_db_cluster(**cp)
            m.set_state(cfg, "db_cluster", cid, "created")
            ids = []
            for role, az in (("writer", disc["zones"][0]), ("reader", disc["zones"][1])):
                ip = aurora_instance_params(cfg, m.prefix, role, ch, tags, inst, az)
                m.add_resource(cfg, "db_instance", ip["DBInstanceIdentifier"], state="requested", cluster_member=True,
                               role=role, rate_usd_per_h=db_rate(cfg))
                rds.create_db_instance(**ip)
                m.set_state(cfg, "db_instance", ip["DBInstanceIdentifier"], "created")
                ids.append(ip["DBInstanceIdentifier"])
            for iid in ids:
                wait_available_instance(rds, iid)
            c = rds.describe_db_clusters(DBClusterIdentifier=cid)["DBClusters"][0]
            host, secret = c["Endpoint"], c["MasterUserSecret"]["SecretArn"]
            m.data["connections"][cfg] = {"cluster_id": cid, "writer": ids[0], "reader": ids[1],
                                          "engine_version": ch["version"], "class": ch["class"]}
        m.add_resource(cfg, "rds_secret", secret, state="created")
        target = {"config": cfg, "kind": "pg", "host": host, "dbname": DB_NAME, "region": m.data["region"],
                  "secret_arn": secret, "sslrootcert": f"{remote.REMOTE_ROOT}/rds-ca.pem"}
    with m.lock:
        m.data.setdefault("targets", {})[cfg] = target
        m.data["config_status"][cfg] = "provisioned"
        m.save()
    update_runner_policy(sess, m)
    m.event(cfg, "provision_done")


def push_target(sess, m, cfg) -> None:
    remote.push_bytes(sess.client("ssm"), m.data["runners"][cfg], json.dumps(m.data["targets"][cfg]).encode(),
                      f"{remote.REMOTE_ROOT}/target-{cfg}.json")


_POLICY_LOCK = threading.Lock()     # config threads share one runner role


def update_runner_policy(sess, m) -> None:
    with _POLICY_LOCK:
        _update_runner_policy(sess, m)


def _update_runner_policy(sess, m) -> None:
    live = [r for r in m.data["resources"] if r["state"] != "deleted"]
    if not any(r["type"] == "iam_role" for r in live):
        return
    doc = runner_policy([r["extra"]["arn"] for r in live if r["type"] == "dsql_cluster"],
                        [r["id"] for r in live if r["type"] == "rds_secret"])
    iam, role = sess.client("iam"), _one(m, "iam_role")
    if doc is None:
        try:
            iam.delete_role_policy(RoleName=role, PolicyName=DB_POLICY_NAME)
        except Exception as exc:  # noqa: BLE001
            if code(exc) not in NOT_FOUND:
                raise
        return
    iam.put_role_policy(RoleName=role, PolicyName=DB_POLICY_NAME, PolicyDocument=json.dumps(doc))
    time.sleep(15)  # IAM propagation before the runner uses the new permission


# --------------------------------------------------------------- cleanup ---

_EC2_CALLS = {"client_security_group": ("describe_security_groups", "GroupIds", "SecurityGroups"),
              "db_security_group": ("describe_security_groups", "GroupIds", "SecurityGroups"),
              "subnet": ("describe_subnets", "SubnetIds", "Subnets"),
              "internet_gateway": ("describe_internet_gateways", "InternetGatewayIds", "InternetGateways"),
              "vpc": ("describe_vpcs", "VpcIds", "Vpcs")}


def _probe(sess, r):
    """Return (exists, tags, detail) for one manifest resource."""
    t, rid = r["type"], r["id"]
    try:
        if t == "dsql_cluster":
            d = sess.client("dsql")
            c = d.get_cluster(identifier=rid)
            if c["status"] == "DELETED":
                return False, {}, c
            return True, d.list_tags_for_resource(resourceArn=c["arn"]).get("tags", {}), c
        if t == "db_instance":
            c = sess.client("rds").describe_db_instances(DBInstanceIdentifier=rid)["DBInstances"][0]
            return True, S.tags_to_dict(c.get("TagList")), c
        if t == "db_cluster":
            c = sess.client("rds").describe_db_clusters(DBClusterIdentifier=rid)["DBClusters"][0]
            return True, S.tags_to_dict(c.get("TagList")), c
        if t == "db_subnet_group":
            rds = sess.client("rds")
            c = rds.describe_db_subnet_groups(DBSubnetGroupName=rid)["DBSubnetGroups"][0]
            return True, S.tags_to_dict(rds.list_tags_for_resource(ResourceName=c["DBSubnetGroupArn"])["TagList"]), c
        if t == "rds_secret":
            d = sess.client("secretsmanager").describe_secret(SecretId=rid)
            return secret_state(d) == "live", {}, d
        if t == "ec2_instance":
            res = sess.client("ec2").describe_instances(InstanceIds=[rid])["Reservations"]
            inst = res[0]["Instances"][0] if res else None
            if not inst or inst["State"]["Name"] == "terminated":
                return False, {}, inst
            return True, S.tags_to_dict(inst.get("Tags")), inst
        if t == "iam_role":
            iam = sess.client("iam")
            d = iam.get_role(RoleName=rid)["Role"]
            return True, S.tags_to_dict(iam.list_role_tags(RoleName=rid)["Tags"]), d
        if t == "instance_profile":
            iam = sess.client("iam")
            d = iam.get_instance_profile(InstanceProfileName=rid)["InstanceProfile"]
            return True, S.tags_to_dict(iam.list_instance_profile_tags(InstanceProfileName=rid)["Tags"]), d
        call = _EC2_CALLS[t]
        items = getattr(sess.client("ec2"), call[0])(**{call[1]: [rid]})[call[2]]
        return (True, S.tags_to_dict(items[0].get("Tags")), items[0]) if items else (False, {}, None)
    except Exception as exc:  # noqa: BLE001
        if code(exc) in NOT_FOUND:
            return False, {}, None
        raise


def _is_owned(r, tags, detail, prefix):
    return secret_owned(detail, prefix) if r["type"] == "rds_secret" else S.owned(tags, prefix, r["config"])


def _delete(sess, r, detail):
    t, rid = r["type"], r["id"]
    if t == "dsql_cluster":
        if detail.get("deletionProtectionEnabled"):
            raise S.SafetyError(f"{rid} has deletion protection; not modifying it automatically")
        if detail["status"] not in ("DELETING", "PENDING_DELETE"):
            sess.client("dsql").delete_cluster(identifier=rid)
    elif t == "db_instance":
        if detail["DBInstanceStatus"] != "deleting":
            kw = {} if r["extra"].get("cluster_member") else {"SkipFinalSnapshot": True,
                                                              "DeleteAutomatedBackups": True}
            sess.client("rds").delete_db_instance(DBInstanceIdentifier=rid, **kw)
    elif t == "db_cluster":
        if detail["Status"] != "deleting":
            sess.client("rds").delete_db_cluster(DBClusterIdentifier=rid, SkipFinalSnapshot=True,
                                                 DeleteAutomatedBackups=True)
    elif t == "rds_secret":  # only reached if RDS left the secret live after its DB was deleted
        sess.client("secretsmanager").delete_secret(SecretId=rid, ForceDeleteWithoutRecovery=True)
    elif t == "ec2_instance":
        ec2 = sess.client("ec2")
        ec2.terminate_instances(InstanceIds=[rid])
        if r["extra"].get("spot_request"):      # a one-time request can stay 'active' after its instance ends
            ec2.cancel_spot_instance_requests(SpotInstanceRequestIds=[r["extra"]["spot_request"]])
    elif t == "instance_profile":
        iam = sess.client("iam")
        for role in detail.get("Roles", []):
            iam.remove_role_from_instance_profile(InstanceProfileName=rid, RoleName=role["RoleName"])
        iam.delete_instance_profile(InstanceProfileName=rid)
    elif t == "iam_role":
        iam = sess.client("iam")
        for p in iam.list_attached_role_policies(RoleName=rid)["AttachedPolicies"]:
            iam.detach_role_policy(RoleName=rid, PolicyArn=p["PolicyArn"])
        for name in iam.list_role_policies(RoleName=rid)["PolicyNames"]:
            iam.delete_role_policy(RoleName=rid, PolicyName=name)
        iam.delete_role(RoleName=rid)
    elif t == "db_subnet_group":
        sess.client("rds").delete_db_subnet_group(DBSubnetGroupName=rid)
    elif t == "internet_gateway":
        ec2 = sess.client("ec2")
        for a in detail.get("Attachments", []):
            if a["VpcId"] == r["extra"].get("vpc"):
                ec2.detach_internet_gateway(InternetGatewayId=rid, VpcId=a["VpcId"])
        ec2.delete_internet_gateway(InternetGatewayId=rid)
    elif t in ("client_security_group", "db_security_group"):
        sess.client("ec2").delete_security_group(GroupId=rid)
    elif t == "subnet":
        sess.client("ec2").delete_subnet(SubnetId=rid)
    elif t == "vpc":
        sess.client("ec2").delete_vpc(VpcId=rid)


def owned_dsql_clusters(sess, prefix, scope=None):
    dsql, found = sess.client("dsql"), []
    for page in dsql.get_paginator("list_clusters").paginate():
        for item in page.get("clusters", []):
            try:
                c = dsql.get_cluster(identifier=item["identifier"])
                if c["status"] == "DELETED":
                    continue
                tags = dsql.list_tags_for_resource(resourceArn=c["arn"]).get("tags", {})
            except Exception as exc:  # noqa: BLE001
                if code(exc) in NOT_FOUND:
                    continue
                raise
            if S.owned(tags, prefix, scope):
                found.append({"id": c["identifier"], "arn": c["arn"], "config": tags.get(S.TAG_CONFIG)})
    return found


def adopt_tagged_orphans(sess, m, scope):
    """Record tagged resources a crash left out of the manifest (exact run + scope tags only)."""
    if scope == "D1":
        for c in owned_dsql_clusters(sess, m.prefix, scope):
            if not m.find(scope, "dsql_cluster", c["id"]):
                m.add_resource(scope, "dsql_cluster", c["id"], state="created", arn=c["arn"], adopted=True)
    ec2 = sess.client("ec2")
    flt = [{"Name": f"tag:{S.TAG_PREFIX}", "Values": [m.prefix]}, {"Name": f"tag:{S.TAG_CONFIG}", "Values": [scope]}]
    if scope != S.BATCH:              # runners belong to their config
        for page in ec2.get_paginator("describe_instances").paginate(Filters=flt):
            for res in page["Reservations"]:
                for inst in res["Instances"]:
                    if inst["State"]["Name"] != "terminated" and not m.find(scope, "ec2_instance", inst["InstanceId"]):
                        m.add_resource(scope, "ec2_instance", inst["InstanceId"], state="created", adopted=True)
        return
    for rtype, call, key, idk in (("vpc", "describe_vpcs", "Vpcs", "VpcId"),
                                  ("subnet", "describe_subnets", "Subnets", "SubnetId"),
                                  ("internet_gateway", "describe_internet_gateways", "InternetGateways",
                                   "InternetGatewayId"),
                                  ("security_group", "describe_security_groups", "SecurityGroups", "GroupId")):
        for item in getattr(ec2, call)(Filters=flt)[key]:
            rt = rtype
            if rtype == "security_group":
                rt = "db_security_group" if item["GroupName"].endswith("-db") else "client_security_group"
            if S.owned(S.tags_to_dict(item.get("Tags")), m.prefix, scope) and not m.find(scope, rt, item[idk]):
                extra = ({"vpc": item["Attachments"][0]["VpcId"]}
                         if rt == "internet_gateway" and item.get("Attachments") else {})
                m.add_resource(scope, rt, item[idk], state="created", adopted=True, **extra)
    iam, name = sess.client("iam"), f"{m.prefix}-runner"
    for rtype, getter, tagger, key in (("iam_role", "get_role", "list_role_tags", "RoleName"),
                                       ("instance_profile", "get_instance_profile", "list_instance_profile_tags",
                                        "InstanceProfileName")):
        try:
            getattr(iam, getter)(**{key: name})
            tags = S.tags_to_dict(getattr(iam, tagger)(**{key: name})["Tags"])
        except Exception as exc:  # noqa: BLE001
            if code(exc) in NOT_FOUND:
                continue
            raise
        if S.owned(tags, m.prefix, scope) and not m.find(scope, rtype, name):
            m.add_resource(scope, rtype, name, state="created", adopted=True)


def cleanup(sess, m, scope, timeout_s=3600) -> None:
    m.event(scope, "cleanup_start")
    adopt_tagged_orphans(sess, m, scope)
    failures = []
    for r in S.cleanup_plan(m.data, scope):
        try:
            exists, tags, detail = _probe(sess, r)
            if exists:
                if not _is_owned(r, tags, detail, m.prefix):
                    raise S.SafetyError(f"{r['type']} {r['id']} lacks this run's ownership; refusing to delete")
                log(f"deleting {scope} {r['type']}")
                deadline = time.monotonic() + timeout_s
                while True:
                    try:
                        _delete(sess, r, detail)
                        break
                    except Exception as exc:  # noqa: BLE001
                        if code(exc) in NOT_FOUND:
                            break
                        if code(exc) not in RETRY_DELETE or time.monotonic() > deadline:
                            raise
                        time.sleep(20)
                        exists, tags, detail = _probe(sess, r)
                        if not exists:
                            break
                wait_until(lambda: not _probe(sess, r)[0], f"{r['type']} gone", timeout_s, 20)
            m.set_state(scope, r["type"], r["id"], "deleted")
        except Exception as exc:  # noqa: BLE001
            failures.append({"type": r["type"], "id": r["id"], "error": f"{type(exc).__name__}: {exc}"[:300]})
    m.data["config_status"][scope] = "cleanup_failed" if failures else "cleaned"
    m.event(scope, "cleanup_done", failures=failures)
    if scope != S.BATCH and not failures:
        try:
            update_runner_policy(sess, m)
        except Exception as exc:  # noqa: BLE001 - the role is deleted with BATCH anyway
            log(f"runner policy update skipped: {type(exc).__name__}")
    if failures:
        raise RuntimeError(f"cleanup incomplete for {scope}: {failures}")


def verify(sess, m) -> dict:
    rds, ec2 = sess.client("rds"), sess.client("ec2")
    remaining, snapshots, pending_secrets = [], [], []
    for r in m.data["resources"]:
        exists, _tags, detail = _probe(sess, r)
        if r["type"] == "rds_secret" and secret_state(detail) == "pending":
            pending_secrets.append(r["id"])   # reported for review; scheduled by RDS, not billable as live
        elif exists:
            remaining.append({"config": r["config"], "type": r["type"], "id": r["id"]})
        checks = []
        if r["type"] == "db_instance":
            checks = [("describe_db_snapshots", {"DBInstanceIdentifier": r["id"]}, "DBSnapshots"),
                      ("describe_db_instance_automated_backups", {"DBInstanceIdentifier": r["id"]},
                       "DBInstanceAutomatedBackups")]
        elif r["type"] == "db_cluster":
            checks = [("describe_db_cluster_snapshots", {"DBClusterIdentifier": r["id"]}, "DBClusterSnapshots"),
                      ("describe_db_cluster_automated_backups", {"DBClusterIdentifier": r["id"]},
                       "DBClusterAutomatedBackups")]
        for call, kw, key in checks:
            try:
                n = len(getattr(rds, call)(**kw)[key])
            except Exception as exc:  # noqa: BLE001
                if code(exc) not in NOT_FOUND:
                    raise
                n = 0
            if n:
                snapshots.append({"id": r["id"], "call": call, "count": n})
    flt = [{"Name": f"tag:{S.TAG_PREFIX}", "Values": [m.prefix]}]
    live = ["pending", "running", "stopping", "stopped", "shutting-down"]
    tagged = {k: len(getattr(ec2, c)(Filters=flt + extra)[k]) for c, k, extra in (
        ("describe_vpcs", "Vpcs", []), ("describe_subnets", "Subnets", []),
        ("describe_internet_gateways", "InternetGateways", []), ("describe_security_groups", "SecurityGroups", []),
        ("describe_volumes", "Volumes", []), ("describe_network_interfaces", "NetworkInterfaces", []),
        ("describe_spot_instance_requests", "SpotInstanceRequests",
         [{"Name": "state", "Values": ["open", "active"]}]))}
    tagged["Instances"] = sum(len(res["Instances"]) for res in ec2.describe_instances(
        Filters=flt + [{"Name": "instance-state-name", "Values": live}])["Reservations"])
    in_manifest = {r["id"] for r in m.data["resources"] if r["type"] == "dsql_cluster"}
    dsql_unrecorded = [c for c in owned_dsql_clusters(sess, m.prefix) if c["id"] not in in_manifest]
    tag_index = [x["ResourceARN"] for x in sess.client("resourcegroupstaggingapi").get_resources(
        TagFilters=[{"Key": S.TAG_PREFIX, "Values": [m.prefix]}])["ResourceTagMappingList"]]
    report = {"verified_at": S.iso(S.utcnow()), "prefix": m.prefix, "manifest_remaining": remaining,
              "secrets_pending_deletion": pending_secrets, "snapshots_or_retained_backups": snapshots,
              "ec2_tag_scan": tagged, "dsql_unrecorded_live": dsql_unrecorded,
              "tag_index_arns_for_review": tag_index,
              "remaining_count": len(remaining) + sum(s["count"] for s in snapshots)
              + sum(tagged.values()) + len(dsql_unrecorded)}
    return report


# ------------------------------------------------------------ CloudWatch ---

def dsql_dpu(sess, cluster_id, start, end) -> float:
    cw = sess.client("cloudwatch")
    metrics = [x for page in cw.get_paginator("list_metrics").paginate(Namespace="AWS/AuroraDSQL")
               for x in page["Metrics"] if any(d["Value"] == cluster_id for d in x["Dimensions"])]
    total = [x for x in metrics if x["MetricName"] == "TotalDPU"]
    if not total:
        raise RuntimeError(f"TotalDPU not found for the cluster; metrics seen: {sorted({x['MetricName'] for x in metrics})}")
    r = cw.get_metric_statistics(Namespace="AWS/AuroraDSQL", MetricName="TotalDPU", Dimensions=total[0]["Dimensions"],
                                 StartTime=start, EndTime=end, Period=60, Statistics=["Sum"])
    return float(sum(p["Sum"] for p in r["Datapoints"]))


def acu_hours(sess, instance_id, start, end) -> float:
    r = sess.client("cloudwatch").get_metric_statistics(
        Namespace="AWS/RDS", MetricName="ServerlessDatabaseCapacity",
        Dimensions=[{"Name": "DBInstanceIdentifier", "Value": instance_id}],
        StartTime=start, EndTime=end, Period=60, Statistics=["Average"])
    return float(sum(p["Average"] for p in r["Datapoints"]) / 60)


def aurora_io(sess, cluster_id, start, end) -> int:
    """Billed Aurora Standard I/O: VolumeReadIOPs + VolumeWriteIOPs summed over the window."""
    cw, total = sess.client("cloudwatch"), 0
    for metric in ("VolumeReadIOPs", "VolumeWriteIOPs"):
        pts = cw.get_metric_statistics(Namespace="AWS/RDS", MetricName=metric,
                                       Dimensions=[{"Name": "DBClusterIdentifier", "Value": cluster_id}],
                                       StartTime=start, EndTime=end, Period=300, Statistics=["Sum"])["Datapoints"]
        total += int(sum(p["Sum"] for p in pts))
    return total
