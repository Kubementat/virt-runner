"""Minimal end-to-end integration test for virt-runner.

Run with: uv run tests/integration_test.py

Flow: create 2 VMs -> list (expect 2) -> ssh hello world in each ->
destroy both -> list (expect 0).
"""

from __future__ import annotations

import json
import shlex
import subprocess
import sys

NAMES = ["it-vm-a", "it-vm-b"]


def virt_runner(*args: str, allow_fail: bool = False) -> dict:
    """Run virt-runner with --json and enforce the contract on its stdout.

    --json promises exactly one JSON document on stdout (success or error
    envelope); anything unparsable is a contract violation, not a test hiccup.
    """
    proc = subprocess.run(
        ["virt-runner", *args, "--json"],
        capture_output=True,
        text=True,
        check=False,
    )
    try:
        doc = json.loads(proc.stdout)
    except json.JSONDecodeError as exc:
        sys.exit(
            f"FAIL: --json violated — 'virt-runner {' '.join(args)}' "
            f"stdout is not valid JSON ({exc})\n---stdout---\n{proc.stdout}"
        )
    if proc.returncode != 0 and not allow_fail:
        sys.exit(
            f"FAIL: virt-runner {' '.join(args)} exited {proc.returncode}\n"
            f"{json.dumps(doc, indent=2)}\n{proc.stderr}"
        )
    return doc


def ssh_hello(world: str, ssh_command: str) -> None:
    # Run exactly the ssh_command virt-runner reports, plus non-interactive
    # options, to prove the printed line is copy-paste ready.
    parts = shlex.split(ssh_command)
    cmd = parts[:1] + [
        "-o", "BatchMode=yes",
        "-o", "StrictHostKeyChecking=no",
        "-o", "UserKnownHostsFile=/dev/null",
        "-o", "ConnectTimeout=10",
    ] + parts[1:] + [f"echo {world}"]
    proc = subprocess.run(cmd, capture_output=True, text=True, check=False)
    assert proc.returncode == 0, f"ssh failed ({' '.join(cmd)}): {proc.stderr}"
    assert world in proc.stdout, f"expected {world!r} in stdout, got {proc.stdout!r}"


def check(condition: bool, message: str) -> None:
    if not condition:
        sys.exit(f"FAIL: {message}")
    print(f"ok: {message}")


def main() -> None:
    # Best-effort cleanup of leftovers from a previous interrupted run.
    # Still --json: even the "vm-not-defined" error path must carry an
    # envelope on stdout.
    for name in NAMES:
        virt_runner("destroy", name, allow_fail=True)

    # 1. Create two VMs.
    for name in NAMES:
        doc = virt_runner("create", "--ram", "1", "--vcpu", "1", "--disk", "8", name)
        check(doc["status"] == "success", f"create {name} succeeded")
        check(doc["booted"], f"{name} booted")
        check(doc["vm"]["ip"] is not None, f"{name} has an IP")

    # 2. List -> our two VMs present (foreign VMs in the pool are none of
    # the suite's business).
    doc = virt_runner("list")
    listed = [vm["name"] for vm in doc["vms"]]
    check(sorted(n for n in listed if n in NAMES) == NAMES, f"list shows {NAMES}, got {listed}")

    # 3. SSH hello world into each VM (one-shot ssh disconnects on its own).
    for vm in doc["vms"]:
        if vm["name"] not in NAMES:
            continue
        world = f"hello-{vm['name']}"
        ssh_hello(world, vm["ssh_command"])
        check(True, f"hello world ran in {vm['name']} via {vm['ssh_command']}")

    # 4. Destroy both.
    for name in NAMES:
        doc = virt_runner("destroy", name)
        check(doc["status"] == "success", f"destroy {name} succeeded")

    # 5. List -> none of our VMs left.
    doc = virt_runner("list")
    left = [vm["name"] for vm in doc["vms"] if vm["name"] in NAMES]
    check(not left, f"no test VMs left after teardown, got {left}")


if __name__ == "__main__":
    main()
