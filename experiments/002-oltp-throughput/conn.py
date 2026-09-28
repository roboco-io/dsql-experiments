"""Connection factories for the E002 runner. Credentials are fetched in-process and never printed or stored."""
from __future__ import annotations

import asyncio
import json
import time

import psycopg


TOKEN_TTL_S = 600               # reuse a signed DSQL token this long (it is valid for 15 minutes)
_TOKEN_CACHE: dict = {}
_CLIENTS: dict = {}


def _sign(target: dict) -> str:
    import boto3
    client = _CLIENTS.get(target["region"])
    if client is None:
        client = _CLIENTS[target["region"]] = boto3.client("dsql", region_name=target["region"])
    return client.generate_db_connect_admin_auth_token(Hostname=target["host"], Region=target["region"],
                                                       ExpiresIn=900)


def _dsql_token(target: dict, now: float | None = None) -> str:
    """Signing needs a boto3 client and takes tens of milliseconds: cache per process for TOKEN_TTL_S."""
    now = time.monotonic() if now is None else now
    key = (target["host"], target["region"])
    hit = _TOKEN_CACHE.get(key)
    if hit and now - hit[1] < TOKEN_TTL_S:
        return hit[0]
    token = _sign(target)
    _TOKEN_CACHE[key] = (token, now)
    return token


def _credentials(target: dict):
    kind = target["kind"]
    if kind == "dsn":
        return None, None
    if kind == "dsql":
        return target.get("user", "admin"), _dsql_token(target)
    import boto3
    if kind == "pg":
        secret = boto3.client("secretsmanager", region_name=target["region"]).get_secret_value(
            SecretId=target["secret_arn"])
        data = json.loads(secret["SecretString"])
        return data["username"], data["password"]
    raise ValueError(f"unknown target kind {kind!r}")


def conn_kwargs(target: dict, user, password) -> dict:
    kind = target.get("kind")
    if kind == "dsn":
        return {"conninfo": target["dsn"], "autocommit": True}
    if kind not in ("dsql", "pg"):
        raise ValueError(f"unknown target kind {kind!r}")
    return {"host": target["host"], "port": 5432, "dbname": target["dbname"], "user": user, "password": password,
            "sslmode": "verify-full", "sslrootcert": target["sslrootcert"], "connect_timeout": 15,
            "autocommit": True, "application_name": "e002"}


def _kwargs_fn(target: dict):
    """DSQL: sign a new IAM token for every connection (tokens expire; DSQL ends connections after an hour).
    PostgreSQL: fetch the secret once."""
    if target.get("kind") == "dsql":
        return lambda: conn_kwargs(target, *_credentials(target))
    kw = conn_kwargs(target, *_credentials(target))
    return lambda: kw


def sync_connect_factory(target: dict):
    kw = _kwargs_fn(target)
    return lambda: psycopg.connect(**kw())


def async_connect_factory(target: dict):
    kw = _kwargs_fn(target)

    async def connect():
        # a DSQL token may need signing: keep that off the event loop so the open-loop producer is not delayed
        args = await asyncio.to_thread(kw) if target.get("kind") == "dsql" else kw()
        return await psycopg.AsyncConnection.connect(**args)
    return connect


def sensitive(target: dict) -> list[str]:
    return [v for k, v in target.items() if k in ("host", "secret_arn", "dsn") and v]


def redact(text: str, target: dict) -> str:
    for s in sensitive(target):
        text = text.replace(s, "<redacted>")
    return text
