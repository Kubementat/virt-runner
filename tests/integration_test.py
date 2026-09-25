"""Minimal end-to-end integration test for virt-runner.

Run with: uv run tests/integration_test.py

Flow: create 2 Ubuntu VMs + 1 Arch VM + 1 Fedora VM -> list -> ssh hello world
in each -> ssh <name> session in each -> destroy all -> list (expect 0).
"""

from __future__ import annotations

import json
import shlex
import subprocess
import sys

NAMES = ["it-vm-a", "it-vm-b"]
ARCH_NAME = "it-vm-arch"
FEDORA_NAME = "it-vm-fedora"
ALL = NAMES + [ARCH_NAME, FEDORA_NAME]


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
        stdin=subprocess.DEVNULL,
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
    cmd = (
        parts[:1]
        + [
            "-o",
            "BatchMode=yes",
            "-o",
            "StrictHostKeyChecking=no",
            "-o",
            "UserKnownHostsFile=/dev/null",
            "-o",
            "ConnectTimeout=10",
        ]
        + parts[1:]
        + [f"echo {world}"]
    )
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
    for name in ALL:
        virt_runner("destroy", name, allow_fail=True)

    # 1. Create two VMs.
    for name in NAMES:
        doc = virt_runner("create", "--ram", "1", "--vcpu", "1", "--disk", "8", name)
        check(doc["status"] == "success", f"create {name} succeeded")
        check(doc["booted"], f"{name} booted")
        check(doc["vm"]["ip"] is not None, f"{name} has an IP")
        check(doc["image"]["verified"], f"{name} image verified")

    # 1b. Arch leg: distro profile, arch-boxes image + sidecar .SHA256,
    # wheel-group user `arch`, `generic` osinfo boot.
    doc = virt_runner(
        "create",
        "--distro",
        "arch",
        "--ram",
        "1",
        "--vcpu",
        "1",
        "--disk",
        "8",
        ARCH_NAME,
    )
    check(doc["status"] == "success", f"create {ARCH_NAME} succeeded")
    check(doc["booted"], f"{ARCH_NAME} booted")
    check(doc["vm"]["distro"] == "arch", f"create {ARCH_NAME} reports distro arch")
    check(doc["vm"]["ssh_user"] == "arch", f"create {ARCH_NAME} uses user arch")
    check(doc["vm"]["ip"] is not None, f"{ARCH_NAME} has an IP")
    check(doc["image"]["verified"], f"{ARCH_NAME} image verified")

    # 1c. Fedora leg: profile, Fedora cloud image + block-format checksum,
    # wheel-group user `fedora`, `generic` osinfo boot.
    doc = virt_runner(
        "create",
        "--distro",
        "fedora",
        "--ram",
        "1",
        "--vcpu",
        "1",
        "--disk",
        "8",
        FEDORA_NAME,
    )
    check(doc["status"] == "success", f"create {FEDORA_NAME} succeeded")
    check(doc["booted"], f"{FEDORA_NAME} booted")
    check(
        doc["vm"]["distro"] == "fedora", f"create {FEDORA_NAME} reports distro fedora"
    )
    check(doc["vm"]["ssh_user"] == "fedora", f"create {FEDORA_NAME} uses user fedora")
    check(doc["vm"]["ip"] is not None, f"{FEDORA_NAME} has an IP")
    check(doc["image"]["verified"], f"{FEDORA_NAME} image verified")

    # 2. List -> our four VMs present (foreign VMs in the pool are none of
    # the suite's business).
    doc = virt_runner("list")
    listed = [vm["name"] for vm in doc["vms"]]
    check(
        sorted(n for n in listed if n in ALL) == sorted(ALL),
        f"list shows {ALL}, got {listed}",
    )
    arch_entry = next(vm for vm in doc["vms"] if vm["name"] == ARCH_NAME)
    check(
        arch_entry["ssh_user"] == "arch",
        f"list reports per-VM user arch for {ARCH_NAME}, got {arch_entry['ssh_user']}",
    )

    # 3. SSH hello world into each VM (one-shot ssh disconnects on its own).
    # For the Arch VM this proves the injected ed25519 key is accepted and
    # the `arch` user (wheel group) works — via the per-VM ssh_command that
    # list reads back from the domain metadata recorded at create.
    for vm in doc["vms"]:
        if vm["name"] not in ALL:
            continue
        world = f"hello-{vm['name']}"
        ssh_hello(world, vm["ssh_command"])
        check(True, f"hello world ran in {vm['name']} via {vm['ssh_command']}")

    # 3b. `ssh <name>` opens a real session in the VM — stdin is /dev/null,
    # so the remote shell sees EOF and exits 0; a non-zero exit fails the run.
    # The per-VM user (ubuntu/arch) comes from the domain metadata, no --user.
    for name in ALL:
        doc = virt_runner("ssh", name)
        check(doc["status"] == "success", f"ssh session into {name} closed cleanly")
        check(doc["exit_code"] == 0, f"ssh session into {name} exited 0")

    # 4. Destroy all.
    for name in ALL:
        doc = virt_runner("destroy", name)
        check(doc["status"] == "success", f"destroy {name} succeeded")

    # 5. List -> none of our VMs left.
    doc = virt_runner("list")
    left = [vm["name"] for vm in doc["vms"] if vm["name"] in ALL]
    check(not left, f"no test VMs left after teardown, got {left}")


if __name__ == "__main__":
    main()
