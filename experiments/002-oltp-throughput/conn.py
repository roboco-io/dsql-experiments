"""Connection factories for the E002 runner. Credentials are fetched in-process and never printed or stored."""
from __future__ import annotations

import json

import psycopg


def _credentials(target: dict):
    kind = target["kind"]
    if kind == "dsn":
        return None, None
    import boto3
    if kind == "dsql":
        client = boto3.client("dsql", region_name=target["region"])
        token = client.generate_db_connect_admin_auth_token(Hostname=target["host"], Region=target["region"],
                                                           ExpiresIn=3600)
        return target.get("user", "admin"), token
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


def sync_connect_factory(target: dict):
    kw = conn_kwargs(target, *_credentials(target))
    return lambda: psycopg.connect(**kw)


def async_connect_factory(target: dict):
    kw = conn_kwargs(target, *_credentials(target))

    async def connect():
        return await psycopg.AsyncConnection.connect(**kw)
    return connect


def sensitive(target: dict) -> list[str]:
    return [v for k, v in target.items() if k in ("host", "secret_arn", "dsn") and v]


def redact(text: str, target: dict) -> str:
    for s in sensitive(target):
        text = text.replace(s, "<redacted>")
    return text
