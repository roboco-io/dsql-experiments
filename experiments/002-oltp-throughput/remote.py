"""Operator-side SSM helpers: run shell commands on the runner, push the code bundle, fetch result files.

SendCommand parameters are logged by AWS (CloudTrail/SSM history), so only code and non-secret target
descriptions (host, secret ARN) are ever sent; passwords and tokens are fetched on the runner itself.
"""
from __future__ import annotations

import base64
import gzip
import io
import os
import shlex
import tarfile
import time

REMOTE_ROOT = "/opt/e002"
BUNDLE_FILES = ("requirements.txt", "runner.py", "openloop.py", "workload.py", "datagen.py", "schema.py",
                "invariants.py", "retry.py", "hist.py", "conn.py")
PUSH_CHUNK = 20000      # characters per SendCommand argument
FETCH_CHUNK = 18000     # below the 24,000-character StandardOutputContent limit
FINAL = {"Success", "Failed", "Cancelled", "TimedOut", "DeliveryTimedOut", "Undeliverable", "Terminated"}


def make_bundle(root: str) -> bytes:
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tar:
        for name in BUNDLE_FILES:
            tar.add(os.path.join(root, name), arcname=name)
    return buf.getvalue()


def split(s: str, n: int) -> list[str]:
    return [s[i:i + n] for i in range(0, len(s), n)]


def assemble(chunks: list[str], expected_len: int) -> str:
    s = "".join(chunks)
    if len(s) != expected_len:
        raise RuntimeError(f"fetched {len(s)} of {expected_len} characters")
    return s


def ssm_run(ssm, instance_id: str, commands: list[str], timeout_s: int = 900, check=None,
            poll_s: float = 3, check_every_s: float = 30):
    """Run commands and wait. `check()` is called periodically and may raise (e.g. the runner was lost),
    because SSM can keep a terminated instance's invocation InProgress until the timeout."""
    r = ssm.send_command(InstanceIds=[instance_id], DocumentName="AWS-RunShellScript", TimeoutSeconds=60,
                         Parameters={"commands": commands, "executionTimeout": [str(timeout_s)]},
                         Comment="e002")
    cid = r["Command"]["CommandId"]
    deadline = time.monotonic() + timeout_s + 180
    last_check = time.monotonic()
    while True:
        time.sleep(poll_s)
        if check and time.monotonic() - last_check >= check_every_s:
            check()
            last_check = time.monotonic()
        try:
            inv = ssm.get_command_invocation(CommandId=cid, InstanceId=instance_id)
        except ssm.exceptions.InvocationDoesNotExist:
            continue
        if inv["Status"] in FINAL:
            return inv["Status"], inv.get("StandardOutputContent", ""), inv.get("StandardErrorContent", "")
        if time.monotonic() > deadline:
            raise TimeoutError(f"SSM command {cid} still {inv['Status']}")


def _check(result, what):
    status, out, err = result
    if status != "Success":
        raise RuntimeError(f"{what}: SSM status {status}: {err[-500:]}")
    return out


def push_bytes(ssm, iid: str, data: bytes, dest_path: str) -> None:
    b64 = base64.b64encode(data).decode()
    tmp = shlex.quote(dest_path + ".b64")
    _check(ssm_run(ssm, iid, ["set -e", f"mkdir -p {shlex.quote(os.path.dirname(dest_path))}", f": > {tmp}"]),
           "push init")
    for part in split(b64, PUSH_CHUNK):
        _check(ssm_run(ssm, iid, [f"printf %s '{part}' >> {tmp}"]), "push chunk")
    _check(ssm_run(ssm, iid, ["set -e", f"base64 -d {tmp} > {shlex.quote(dest_path)}", f"rm -f {tmp}"]),
           "push decode")


def push_bundle(ssm, iid: str, root: str) -> None:
    dest = f"{REMOTE_ROOT}/bundle.tgz"
    push_bytes(ssm, iid, make_bundle(root), dest)
    _check(ssm_run(ssm, iid, ["set -e", f"rm -rf {REMOTE_ROOT}/code", f"mkdir -p {REMOTE_ROOT}/code",
                              f"tar -xzf {dest} -C {REMOTE_ROOT}/code"]), "unpack bundle")


def fetch_file(ssm, iid: str, path: str) -> bytes:
    q, tmp = shlex.quote(path), f"{REMOTE_ROOT}/fetch.b64"
    out = _check(ssm_run(ssm, iid, ["set -e", f"gzip -c {q} | base64 -w0 > {tmp}", f"wc -c < {tmp}"]),
                 "fetch prepare")
    n = int(out.strip().splitlines()[-1])
    chunks = []
    for off in range(0, n, FETCH_CHUNK):
        chunks.append(_check(ssm_run(ssm, iid, [f"tail -c +{off + 1} {tmp} | head -c {FETCH_CHUNK}"]),
                             "fetch chunk"))
    return gzip.decompress(base64.b64decode(assemble(chunks, n)))


def runner_cmd(command: str, cfg: str, extra: str = "") -> str:
    return (f"cd {REMOTE_ROOT}/code && {REMOTE_ROOT}/venv/bin/python runner.py {command} "
            f"--target {REMOTE_ROOT}/target-{cfg}.json {extra}").strip()
