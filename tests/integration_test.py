"""Minimal end-to-end integration test for virt-runner.

Run with: uv run tests/integration_test.py

Flow: create 2 Ubuntu VMs (with passing / failing --script) + 1 Arch VM + 1 Fedora VM -> list -> ssh hello world
+ shared-dir read/write in each -> ssh <name> session in each -> destroy all
-> list (expect 0).

The Arch leg is x86_64-only (arch-boxes ships no aarch64 image): on aarch64
hosts it is skipped and the Ubuntu + Fedora legs are the real aarch64 e2e.
"""

from __future__ import annotations

import json
import shlex
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

from virt_runner.virtualizer import Virtualizer

NAMES = ["it-vm-a", "it-vm-b"]
ARCH_NAME = "it-vm-arch"
FEDORA_NAME = "it-vm-fedora"
# arch-boxes ships x86_64 images only: skip the arch leg elsewhere.
RUN_ARCH = Virtualizer.host_arch() == "x86_64"
ALL = NAMES + ([ARCH_NAME] if RUN_ARCH else []) + [FEDORA_NAME]


def virt_runner(*args: str, allow_fail: bool = False) -> tuple[dict, int]:
    """Run virt-runner with --json and enforce the contract on its stdout.

    Returns ``(document, exit_code)``. --json promises exactly one JSON
    document on stdout (success or error envelope); anything unparsable is a
    contract violation, not a test hiccup.
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
    return doc, proc.returncode


def ssh_run(ssh_command: str, remote: str) -> str:
    """Run *remote* via the reported ssh_command (non-interactive); return stdout."""
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
        + [remote]
    )
    proc = subprocess.run(cmd, capture_output=True, text=True, check=False)
    assert proc.returncode == 0, f"ssh failed ({' '.join(cmd)}): {proc.stderr}"
    return proc.stdout


def ssh_hello(world: str, ssh_command: str) -> None:
    # Run exactly the ssh_command virt-runner reports, plus non-interactive
    # options, to prove the printed line is copy-paste ready.
    assert world in ssh_run(ssh_command, f"echo {world}")


def check(condition: bool, message: str) -> None:
    if not condition:
        sys.exit(f"FAIL: {message}")
    print(f"ok: {message}")


def main() -> None:
    # Create a shared directory for the mount tests.
    share = Path(tempfile.mkdtemp(prefix="virt-runner-it-share-"))
    (share / "marker").write_text("from-host\n")
    # --script fixtures: no shebang, so the interpreter comes from the extension.
    scripts = Path(tempfile.mkdtemp(prefix="virt-runner-it-scripts-"))
    (scripts / "ok.sh").write_text("id -un > ~/script-sh\n")
    (scripts / "ok.py").write_text(
        "import pathlib; (pathlib.Path.home() / 'script-py').write_text('py')\n"
    )
    (scripts / "fail.sh").write_text("echo failing >&2; exit 3\n")

    # Best-effort cleanup of leftovers from a previous interrupted run.
    # Still --json: even the "vm-not-defined" error path must carry an
    # envelope on stdout.
    for name in ALL:
        virt_runner("destroy", name, allow_fail=True)

    # 0. --version works without libvirt.
    proc = subprocess.run(
        ["virt-runner", "--version"], capture_output=True, text=True, check=False
    )
    check(
        proc.returncode == 0 and proc.stdout.startswith("virt-runner, version "),
        f"--version prints the version, got {proc.stdout!r}",
    )

    # 0. Negative checks (no VMs created).
    doc, rc = virt_runner("create", "1bad", allow_fail=True)
    check(doc["status"] == "error", "create with an invalid name reports an error")
    check(
        doc["error"]["code"] == "usage",
        f"invalid name is a usage error, got {doc['error']['code']}",
    )
    check(rc == 2, f"usage error exits 2, got {rc}")

    doc, rc = virt_runner("destroy", "it-never-existed", allow_fail=True)
    check(doc["status"] == "error", "destroy of an undefined VM reports an error")
    check(
        doc["error"]["code"] == "vm-not-defined",
        f"undefined VM is vm-not-defined, got {doc['error']['code']}",
    )
    check(rc == 1, f"vm-not-defined exits 1, got {rc}")

    doc, rc = virt_runner(
        "create",
        "--no-boot",
        "--script",
        str(scripts / "ok.sh"),
        "it-x",
        allow_fail=True,
    )
    check(
        rc == 2 and doc["error"]["code"] == "usage", "--script with --no-boot is usage"
    )

    try:
        # 1. Create two VMs: A runs two passing scripts, B one failing script
        # (script-failed, exit 1, VM kept running for the later steps).
        script_args = {
            NAMES[0]: [
                "--script",
                str(scripts / "ok.sh"),
                "--script",
                str(scripts / "ok.py"),
            ],
            NAMES[1]: ["--script", str(scripts / "fail.sh")],
        }
        for name in NAMES:
            doc, rc = virt_runner(
                "create",
                "--ram",
                "1",
                "--vcpu",
                "1",
                "--disk",
                "8",
                "--mount",
                f"{share}:/mnt/share",
                *script_args[name],
                name,
                allow_fail=name == NAMES[1],
            )
            if name == NAMES[1]:
                check(
                    rc == 1
                    and doc["error"]["code"] == "script-failed"
                    and doc["error"]["stage"] == "script"
                    and doc["scripts"]
                    == [{"path": str(scripts / "fail.sh"), "exit_code": 3}],
                    f"{name} failing script is script-failed, exit 1",
                )
            else:
                check(doc["status"] == "success", f"create {name} succeeded")
                check(
                    [s["exit_code"] for s in doc["scripts"]] == [0, 0],
                    f"{name} reports both scripts exit 0",
                )
                out = ssh_run(doc["vm"]["ssh_command"], "cat ~/script-sh ~/script-py")
                check(
                    out == f"{doc['vm']['ssh_user']}\npy",
                    f"{name} scripts ran as the cloud user (sh + py)",
                )
            check(doc["booted"], f"{name} booted")
            check(
                doc["vm"]["arch"] == Virtualizer.host_arch(),
                f"{name} reports the host arch {doc['vm']['arch']}",
            )
            check(doc["vm"]["ip"] is not None, f"{name} has an IP")
            check(doc["image"]["verified"], f"{name} image verified")
            check(
                doc["vm"]["mounts"]
                == [
                    {
                        "source": str(share.resolve()),
                        "target": "/mnt/share",
                        "tag": "mount0",
                    }
                ],
                f"{name} reports its mount",
            )

        # 1a. Negative: creating the existing NAMES[0] again fails fast while
        # it still exists (vm-already-defined, exit 1).
        doc, rc = virt_runner(
            "create",
            "--ram",
            "1",
            "--vcpu",
            "1",
            "--disk",
            "8",
            NAMES[0],
            allow_fail=True,
        )
        check(
            doc["status"] == "error", f"create of existing {NAMES[0]} reports an error"
        )
        check(
            doc["error"]["code"] == "vm-already-defined",
            f"existing VM is vm-already-defined, got {doc['error']['code']}",
        )
        check(rc == 1, f"vm-already-defined exits 1, got {rc}")

        # 1b. Arch leg: distro profile, arch-boxes image + sidecar .SHA256,
        # wheel-group user `arch`, `generic` osinfo boot. Skipped on
        # aarch64 hosts — arch-boxes ships x86_64-only images.
        if RUN_ARCH:
            doc, _ = virt_runner(
                "create",
                "--distro",
                "arch",
                "--ram",
                "1",
                "--vcpu",
                "1",
                "--disk",
                "8",
                "--mount",
                f"{share}:/mnt/share",
                ARCH_NAME,
            )
            check(doc["status"] == "success", f"create {ARCH_NAME} succeeded")
            check(doc["booted"], f"{ARCH_NAME} booted")
            check(
                doc["vm"]["distro"] == "arch",
                f"create {ARCH_NAME} reports distro arch",
            )
            check(
                doc["vm"]["ssh_user"] == "arch",
                f"create {ARCH_NAME} uses user arch",
            )
            check(doc["vm"]["ip"] is not None, f"{ARCH_NAME} has an IP")
            check(doc["image"]["verified"], f"{ARCH_NAME} image verified")
        else:
            check(
                True,
                f"arch leg skipped on {Virtualizer.host_arch()} host "
                "(arch-boxes ships x86_64-only images)",
            )

        # 1c. Fedora leg: profile, Fedora cloud image + block-format checksum,
        # wheel-group user `fedora`, `generic` osinfo boot.
        doc, _ = virt_runner(
            "create",
            "--distro",
            "fedora",
            "--ram",
            "1",
            "--vcpu",
            "1",
            "--disk",
            "8",
            "--mount",
            f"{share}:/mnt/share",
            FEDORA_NAME,
        )
        check(doc["status"] == "success", f"create {FEDORA_NAME} succeeded")
        check(doc["booted"], f"{FEDORA_NAME} booted")
        check(
            doc["vm"]["distro"] == "fedora",
            f"create {FEDORA_NAME} reports distro fedora",
        )
        check(
            doc["vm"]["ssh_user"] == "fedora", f"create {FEDORA_NAME} uses user fedora"
        )
        check(doc["vm"]["ip"] is not None, f"{FEDORA_NAME} has an IP")
        check(doc["image"]["verified"], f"{FEDORA_NAME} image verified")

        # 2. List -> our four VMs present (foreign VMs in the pool are none of
        # the suite's business).
        doc, _ = virt_runner("list")
        listed = [vm["name"] for vm in doc["vms"]]
        check(
            sorted(n for n in listed if n in ALL) == sorted(ALL),
            f"list shows {ALL}, got {listed}",
        )
        # Verify ssh_command for it-vm-a carries the default private key.
        vm_a = next(vm for vm in doc["vms"] if vm["name"] == NAMES[0])
        default_key = Virtualizer.private_key_path(Virtualizer.DEFAULT_SSH_KEY)
        check(
            vm_a["ssh_command"] is not None
            and f"-i {default_key}" in vm_a["ssh_command"],
            f"list ssh_command for {NAMES[0]} uses the default key: {vm_a['ssh_command']}",
        )

        if RUN_ARCH:
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

        # 3b. Mount read/write check for each VM.
        for vm in doc["vms"]:
            if vm["name"] not in ALL:
                continue
            out = ssh_run(
                vm["ssh_command"],
                f"cat /mnt/share/marker && touch /mnt/share/from-{vm['name']}",
            )
            check("from-host" in out, f"{vm['name']} reads the host share")
            check(
                (share / f"from-{vm['name']}").exists(),
                f"{vm['name']} writes to the host share",
            )
            # fstab fallback: the NoCloud seed CDROM is first-boot-only, so the
            # bootcmd also records a nofail fstab entry to keep the mount across
            # later reboots when cloud-init no longer runs.
            fstab = ssh_run(vm["ssh_command"], "grep virtiofs /etc/fstab || true")
            check(
                "nofail" in fstab and "/mnt/share" in fstab,
                f"{vm['name']} recorded the virtiofs fstab entry",
            )

        # 3c. Cold-boot survival: the NoCloud seed volume lives until destroy
        # (a deleted seed would break `virsh start` — every VM is autostart),
        # so a plain destroy+start must bring the guest back. cloud-init is
        # disabled after the first boot, so the fstab entry — not bootcmd —
        # re-mounts the share: prove it, not just grep for it.
        name = NAMES[0]
        subprocess.run(["virsh", "--quiet", "destroy", name], check=True)
        subprocess.run(["virsh", "--quiet", "start", name], check=True)
        for _ in range(60):
            doc, rc = virt_runner("ssh", name, allow_fail=True)
            if rc == 0:
                break
            time.sleep(2)
        check(rc == 0, f"{name} answers ssh after a cold start")
        doc, _ = virt_runner("list")
        vm_a2 = next(vm for vm in doc["vms"] if vm["name"] == name)
        out = ssh_run(
            vm_a2["ssh_command"], "mountpoint -q /mnt/share && echo remounted"
        )
        check("remounted" in out, f"{name} re-mounted the virtiofs share from fstab")

        # 3b. `ssh <name>` opens a real session in the VM — stdin is /dev/null,
        # so the remote shell sees EOF and exits 0; a non-zero exit fails the run.
        # The per-VM user (ubuntu/arch) comes from the domain metadata, no --user.
        for name in ALL:
            doc, _ = virt_runner("ssh", name)
            check(doc["status"] == "success", f"ssh session into {name} closed cleanly")
            check(doc["exit_code"] == 0, f"ssh session into {name} exited 0")

        # 4. Destroy all.
        for name in ALL:
            doc, _ = virt_runner("destroy", name)
            check(doc["status"] == "success", f"destroy {name} succeeded")

        # 5. List -> none of our VMs left.
        doc, _ = virt_runner("list")
        left = [vm["name"] for vm in doc["vms"] if vm["name"] in ALL]
        check(not left, f"no test VMs left after teardown, got {left}")
    finally:
        # A failed run must not leak VMs (check() exits, finally still runs).
        for name in ALL:
            virt_runner("destroy", name, allow_fail=True)
        shutil.rmtree(share, ignore_errors=True)
        shutil.rmtree(scripts, ignore_errors=True)


if __name__ == "__main__":
    main()
