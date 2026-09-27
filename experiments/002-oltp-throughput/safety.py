"""Pure safety helpers for E002: identity, run prefix, manifest, scope/order rules, cleanup selection.

Adapted from E001 (experiments/001-sql-compatibility/safety.py): e002 tags, a BATCH scope for the shared
runner resources (network, IAM, Spot EC2), and EC2/IAM/secret resource types. Never contacts AWS.
"""
from __future__ import annotations

import json
import os
import re
import secrets
import tempfile
import threading
from datetime import datetime, timedelta, timezone

REGION = "ap-northeast-2"
PROFILE = "roboco"
CONFIGS = ("D1", "R1", "A1", "A2")  # run in parallel; each config owns its runner
BATCH = "BATCH"                     # shared network/IAM scope, created before and deleted after all configs
SCOPES = CONFIGS + (BATCH,)
TAG_PREFIX = "e002:run-prefix"
TAG_CONFIG = "e002:config"
TAG_EXPIRES = "e002:expires-at"
TAG_MANAGED = "e002:managed-by"
MANAGED_BY = "e002-oltp-throughput-harness"
MAX_LIFETIME_MIN = 960
# Dependents first; network last. Only these resource types are ever deleted.
DELETION_ORDER = (
    "dsql_cluster", "db_instance", "db_cluster", "rds_secret",
    "ec2_instance", "instance_profile", "iam_role", "db_subnet_group",
    "db_security_group", "client_security_group", "subnet", "internet_gateway", "vpc",
)
SCOPE_TYPES = {
    "D1": {"dsql_cluster", "ec2_instance"},
    "R1": {"db_instance", "rds_secret", "ec2_instance"},
    "A1": {"db_cluster", "db_instance", "rds_secret", "ec2_instance"},
    "A2": {"db_cluster", "db_instance", "rds_secret", "ec2_instance"},
    BATCH: {"instance_profile", "iam_role", "db_subnet_group", "db_security_group",
            "client_security_group", "subnet", "internet_gateway", "vpc"},
}
_PREFIX_RE = re.compile(r"^e002-\d{8}t\d{6}z-[a-z0-9]{4}$")
_ACCOUNT_RE = re.compile(r"^\d{12}$")


class SafetyError(RuntimeError):
    pass


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def parse_iso(value: str) -> datetime:
    return datetime.strptime(value, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)


def new_run_prefix(now: datetime | None = None) -> str:
    now = now or utcnow()
    return f"e002-{now.strftime('%Y%m%dt%H%M%Sz')}-{secrets.token_hex(2)}"


def validate_run_prefix(prefix: str) -> str:
    if not _PREFIX_RE.match(prefix or ""):
        raise SafetyError(f"invalid run prefix: {prefix!r}")
    return prefix


def validate_account_id(account: str) -> str:
    if not _ACCOUNT_RE.match(account or ""):
        raise SafetyError("--account-id must be a 12-digit AWS account ID")
    return account


def assert_identity(expected_account: str, identity: dict) -> None:
    """identity is an STS GetCallerIdentity response."""
    validate_account_id(expected_account)
    actual = identity.get("Account")
    if actual != expected_account:
        raise SafetyError(f"STS account {actual!r} does not match confirmed account {expected_account!r}")


def resource_tags(prefix: str, scope: str, expires_at: str) -> dict:
    return {
        TAG_PREFIX: prefix, TAG_CONFIG: scope, TAG_EXPIRES: expires_at,
        TAG_MANAGED: MANAGED_BY, "Name": f"{prefix}-{scope.lower()}",
    }


def tag_list(tags: dict) -> list[dict]:
    return [{"Key": k, "Value": v} for k, v in tags.items()]


def tags_to_dict(tag_list_value) -> dict:
    return {t["Key"]: t["Value"] for t in (tag_list_value or [])}


def owned(tags: dict, prefix: str, scope: str | None = None) -> bool:
    """True only if the resource carries this run's exact ownership tags."""
    if tags.get(TAG_PREFIX) != prefix or tags.get(TAG_MANAGED) != MANAGED_BY:
        return False
    return scope is None or tags.get(TAG_CONFIG) == scope


def write_private(path: str, obj) -> None:
    """Atomically write JSON with mode 0600 (directory 0700)."""
    directory = os.path.dirname(os.path.abspath(path))
    os.makedirs(directory, mode=0o700, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=directory, prefix=".tmp-")
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "w") as fh:
            json.dump(obj, fh, indent=2, sort_keys=True, default=str)
        os.replace(tmp, path)
    except BaseException:
        if os.path.exists(tmp):
            os.unlink(tmp)
        raise
    os.chmod(path, 0o600)


class Manifest:
    """Local, persisted inventory of every resource this run requested or created."""

    def __init__(self, path: str, data: dict):
        self.path = path
        self.data = data
        self.lock = threading.RLock()     # config threads share one manifest

    @classmethod
    def create(cls, path, account, region, prefix, lifetime_min, identity_arn, now=None):
        validate_account_id(account)
        validate_run_prefix(prefix)
        if not 1 <= lifetime_min <= MAX_LIFETIME_MIN:
            raise SafetyError(f"lifetime must be 1..{MAX_LIFETIME_MIN} minutes")
        if os.path.exists(path):
            raise SafetyError(f"manifest already exists: {path}")
        now = now or utcnow()
        m = cls(path, {
            "experiment": "E002", "account": account, "region": region, "prefix": prefix,
            "caller_arn": identity_arn, "created_at": iso(now),
            "expires_at": iso(now + timedelta(minutes=lifetime_min)),
            "resources": [], "config_status": {}, "connections": {}, "events": [],
            "measured_usd": {},
        })
        m.save()
        return m

    @classmethod
    def load(cls, path):
        with open(path) as fh:
            data = json.load(fh)
        validate_run_prefix(data["prefix"])
        return cls(path, data)

    @property
    def prefix(self) -> str:
        return self.data["prefix"]

    def save(self) -> None:
        with self.lock:
            write_private(self.path, self.data)

    def assert_scope(self, account: str, region: str) -> None:
        if self.data["account"] != account or self.data["region"] != region:
            raise SafetyError("manifest account/region differ from the confirmed account/region")

    def expired(self, now=None) -> bool:
        return (now or utcnow()) >= parse_iso(self.data["expires_at"])

    def minutes_left(self, now=None) -> float:
        return (parse_iso(self.data["expires_at"]) - (now or utcnow())).total_seconds() / 60

    def event(self, scope, name, **fields) -> None:
        with self.lock:
            self.data["events"].append({"ts": iso(utcnow()), "config": scope, "event": name, **fields})
            self.save()

    def find(self, scope, rtype, rid):
        for r in self.data["resources"]:
            if (r["config"], r["type"], r["id"]) == (scope, rtype, rid):
                return r
        return None

    def add_resource(self, scope, rtype, rid, state=None, **extra) -> dict:
        """Record a resource (new ones default to "requested"); without `state`, an existing one keeps its state."""
        if scope not in SCOPES or rtype not in SCOPE_TYPES[scope]:
            raise SafetyError(f"resource type {rtype} not allowed in scope {scope}")
        with self.lock:
            r = self.find(scope, rtype, rid)
            if r is None:
                r = {"config": scope, "type": rtype, "id": rid, "state": state or "requested",
                     "recorded_at": iso(utcnow()), "extra": {}}
                self.data["resources"].append(r)
            elif state and r["state"] != "deleted":
                r["state"] = state
            r["extra"].update(extra)
            self.save()
            return r

    def set_state(self, scope, rtype, rid, state, **extra) -> None:
        with self.lock:
            r = self.find(scope, rtype, rid)
            if r is None:
                raise SafetyError(f"resource not in manifest: {scope}/{rtype}/{rid}")
            r["state"] = state
            r["extra"].update(extra)
            r[f"{state}_at"] = iso(utcnow())
            self.save()


def active_configs(data: dict) -> set[str]:
    """DB configs (not BATCH) that still own undeleted resources."""
    return {r["config"] for r in data["resources"] if r["state"] != "deleted" and r["config"] in CONFIGS}


def check_can_provision(data: dict, scope: str) -> None:
    """Configs run in parallel (spec: 병렬 실행); each may be provisioned once per run prefix, after BATCH."""
    if scope not in SCOPES:
        raise SafetyError(f"unknown scope {scope}")
    if scope == BATCH:
        if any(r["config"] == BATCH for r in data["resources"]):
            raise SafetyError("BATCH already provisioned in this run prefix")
        return
    if data["config_status"].get(BATCH) != "ready":
        raise SafetyError("provision BATCH (batch-up) first")
    if any(r["config"] == scope and r["type"] != "ec2_instance" for r in data["resources"]):
        raise SafetyError(f"{scope} already provisioned in this run prefix; start a new prefix")


def cleanup_plan(data: dict, scope: str) -> list[dict]:
    """Undeleted manifest resources of exactly one scope, in safe deletion order."""
    if scope not in SCOPES:
        raise SafetyError(f"unknown scope {scope}")
    if scope == BATCH and active_configs(data):
        raise SafetyError(f"clean up DB configs {sorted(active_configs(data))} before BATCH")
    selected = [r for r in data["resources"] if r["config"] == scope and r["state"] != "deleted"]
    for r in selected:
        if r["type"] not in DELETION_ORDER:
            raise SafetyError(f"refusing unknown resource type {r['type']}")
    return sorted(selected, key=lambda r: DELETION_ORDER.index(r["type"]))
