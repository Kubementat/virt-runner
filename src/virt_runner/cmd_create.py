"""``create`` subcommand — full pipeline: preflight → image → cloud-init →
virt-install → IP/SSH → scripts → access info.

``--from-snapshot`` swaps the ``image`` stage for ``snapshot`` (read the
snapshot's metadata) and the disk upload for a thin overlay on the snapshot;
everything else, the document included, is the same pipeline.

Both output modes read from one accumulating run state (spec §11.3): the text
report prints the fields it has always printed, ``--json`` renders the same
state as the §5.3.1 document — including on late failures, where the already
created ``vm``/``image`` sections still describe real resources.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

import click

from virt_runner import output
from virt_runner.args import command
from virt_runner.errors import VirtError
from virt_runner.images import ImageFetch, fetch_and_verify_image
from virt_runner.profiles import PROFILES
from virt_runner.virtualizer import (
    SnapshotInfo,
    Virtualizer,
    lease_file,
    snapshot_volume,
)

_DEFAULT_CODE = {
    "preflight": "libvirt-unreachable",
    "image": "image-download-failed",
    "snapshot": "snapshot-not-found",
    "cloud-init": "cloud-init-failed",
    "create": "vm-create-failed",
    "wait-ip": "lease-timeout",
    "ssh-verify": "ssh-timeout",
    "script": "script-failed",
}


def _validate_name(ctx, param, value):
    """Validate VM name matches ^[A-Za-z][A-Za-z0-9._-]*$."""
    if value is None:
        return None
    if not re.match(r"^[A-Za-z][A-Za-z0-9._-]*$", value):
        raise click.BadParameter(
            f"invalid VM name '{value}': must match "
            r"^[A-Za-z][A-Za-z0-9._-]*$"
        )
    return value


def _validate_user(ctx, param, value):
    """Validate user name matches [a-z_][a-z0-9_-]{0,31}."""
    if value is not None and not re.fullmatch(r"[a-z_][a-z0-9_-]{0,31}", value):
        raise click.BadParameter(
            f"invalid user name '{value}': must match "
            r"[a-z_][a-z0-9_-]{0,31}"
        )
    return value


_GUEST_PATH_RE = re.compile(r"/[A-Za-z0-9._-]+(/[A-Za-z0-9._-]+)*")


def _validate_mounts(ctx, param, value):
    """Parse repeated ``--mount HOST[:GUEST]`` into source/target/tag dicts."""
    mounts: list[dict[str, str]] = []
    for i, spec in enumerate(value):
        host, _, guest = spec.partition(":")
        source = Path(host).expanduser().resolve()
        if not source.is_dir():
            raise click.BadParameter(f"'{host}' is not an existing directory")
        if "," in str(source):
            raise click.BadParameter(f"host path '{source}' must not contain ','")
        target = guest or f"/mnt/{source.name}"
        if not _GUEST_PATH_RE.fullmatch(target) or {".", ".."} & set(target.split("/")):
            raise click.BadParameter(
                f"invalid guest path '{target}': must be absolute, components "
                "[A-Za-z0-9._-], no '.' or '..'"
            )
        if any(m["target"] == target for m in mounts):
            raise click.BadParameter(f"guest path '{target}' is used twice")
        mounts.append({"source": str(source), "target": target, "tag": f"mount{i}"})
    return mounts


def _record_metadata(
    v: Virtualizer, name: str, distro: str, user: str, identity: str, **extra: Any
) -> None:
    """Record the per-VM user so `ssh`/`list` log in as the created user.

    *extra* (release, image URL, mounts, snapshot) is what a later
    ``snapshot create`` of this VM copies into the snapshot's metadata.
    """
    try:
        v.set_domain_metadata(name, distro, user, identity, **extra)
    except RuntimeError:
        output.warn(
            f"could not record per-VM user metadata — `ssh`/`list` will "
            f"fall back to user '{Virtualizer.DEFAULT_USER}' for {name}"
        )


def _create_document(run_state: dict[str, Any]) -> dict[str, Any]:
    """Render the sections completed so far, in the spec §5.3.1 order.

    ``vm`` appears once the domain exists and ``image`` once the image stage
    finished, so a preflight failure carries neither while a lease timeout
    still reports the running VM and the image it booted from.
    """
    fields: dict[str, Any] = {"booted": bool(run_state.get("booted"))}
    snap: SnapshotInfo | None = run_state.get("snapshot")
    keep_hostname = bool(snap and run_state["keep_hostname"])

    if run_state.get("created"):
        name = run_state["name"]
        ip = run_state.get("ip")
        fields["vm"] = {
            "name": name,
            "uuid": run_state.get("uuid"),
            "state": run_state.get("domain_state"),
            "distro": run_state["distro"],
            "release": run_state["release"],
            "arch": run_state["arch"],
            "ram_gib": run_state["ram_gib"],
            "vcpu": run_state["vcpu"],
            "disk_gib": run_state["disk_gib"],
            "mounts": run_state["mounts"],
            "mac": run_state.get("mac"),
            "ip": ip,
            "hostname": run_state["hostname"],
            # A kept hostname may be held by another clone in libvirt's DNS,
            # so neither name is reliable: callers must use `ip`.
            "dns_name": None if keep_hostname else f"{name}.default",
            "ssh_user": run_state["user"],
            **output.access_commands(
                name, run_state["user"], ip, run_state["ssh_identity"]
            ),
            # virt-install is always invoked with --autostart.
            "autostart": True,
            "snapshot": snap.name if snap else None,
        }

    image: ImageFetch | None = run_state.get("image")
    if image is not None:
        fields["image"] = {
            "source_url": run_state["image_url"],
            "cache_path": image.path,
            "cache_hit": image.cache_hit,
            "downloaded": image.downloaded,
            "verification": image.verification,
            "verified": image.verified,
        }
    elif snap is not None:
        fields["image"] = None

    fields["snapshot"] = (
        {
            "name": snap.name,
            "volume": snapshot_volume(snap.name),
            "source_vm": (snap.meta.get("source") or {}).get("name"),
            "created_at": snap.meta.get("created_at"),
            "keep_hostname": keep_hostname,
        }
        if snap
        else None
    )
    fields["scripts"] = run_state["scripts"]

    return fields


@command(name="create")
@click.argument("name", callback=_validate_name)
@click.option(
    "--distro",
    type=click.Choice(sorted(PROFILES)),
    default=None,
    help="Guest distro profile [default: ubuntu]",
)
@click.option(
    "--ram",
    default=None,
    type=click.IntRange(min=1),
    help="Guest RAM in GiB (positive integer) [default: 4 (snapshot: recorded)]",
)
@click.option(
    "--vcpu",
    default=None,
    type=click.IntRange(min=1),
    help="vCPU count (positive integer) [default: 2 (snapshot: recorded)]",
)
@click.option(
    "--disk",
    default=None,
    type=click.IntRange(min=1),
    help="Virtual disk size in GiB [default: 30 (snapshot: recorded, grow only)]",
)
@click.option(
    "--release",
    default=None,
    help="Release (Ubuntu codename, e.g. resolute; "
    "Arch/Fedora: release number) [default: per-distro]",
)
@click.option(
    "--image",
    default=None,
    help="Direct URL of a raw cloud image",
)
@click.option(
    "--user",
    default=None,
    callback=_validate_user,
    help="Cloud user name [default: per-distro: ubuntu/arch/fedora]",
)
@click.option(
    "--ssh-key",
    default=Virtualizer.DEFAULT_SSH_KEY,
    help="Path to the public key to inject; generated when absent "
    "[default: ~/.ssh/virt_runner_key.pub]",
)
@click.option(
    "--mount",
    "mounts",
    multiple=True,
    callback=_validate_mounts,
    metavar="HOST[:GUEST]",
    help="Share a host directory into the guest (virtiofs, read-write); "
    "repeatable [default GUEST: /mnt/<basename of HOST>]",
)
@click.option(
    "--script",
    "scripts",
    multiple=True,
    type=click.Path(exists=True, dir_okay=False, resolve_path=True, path_type=Path),
    help="Run a host script in the guest as the cloud user once it is up "
    "(after cloud-init finishes); repeatable, runs in order. #! line wins, "
    "else .py -> python3, other -> bash",
)
@click.option(
    "--no-boot",
    is_flag=True,
    default=False,
    help="Create the VM but do not boot / wait for IP",
)
@click.option(
    "--keep-going",
    is_flag=True,
    default=False,
    help="On download/verify error, continue if a cached image exists",
)
@click.option(
    "--from-snapshot",
    "from_snapshot",
    default=None,
    callback=_validate_name,
    metavar="SNAP",
    help="Clone the VM from a snapshot (virt-runner snapshot create) instead of "
    "a cloud image: thin overlay disk, new identity",
)
@click.option(
    "--keep-hostname",
    is_flag=True,
    default=False,
    help="With --from-snapshot: keep the guest hostname recorded in the "
    "snapshot (k3s and other hostname-bound apps); vm.dns_name is then null",
)
def cmd_create(
    name: str,
    ram: int | None,
    vcpu: int | None,
    disk: int | None,
    distro: str | None,
    release: str | None,
    image: str | None,
    user: str | None,
    ssh_key: str,
    mounts: list[dict[str, str]],
    scripts: tuple[Path, ...],
    no_boot: bool,
    keep_going: bool,
    from_snapshot: str | None,
    keep_hostname: bool,
    as_json: bool,
) -> None:
    """Create and boot a KVM VM (Ubuntu, Arch, or Fedora) via libvirt (qemu:///system)."""
    output.set_json_mode(as_json)
    v = Virtualizer()

    if scripts and no_boot:
        raise click.UsageError("--script needs a booted VM; drop --no-boot")
    if keep_hostname and not from_snapshot:
        raise click.UsageError("--keep-hostname needs --from-snapshot")
    if from_snapshot:
        given = [
            flag
            for flag, value in (
                ("--distro", distro),
                ("--release", release),
                ("--image", image),
                ("--user", user),
                ("--keep-going", keep_going or None),
            )
            if value is not None
        ]
        if given:
            raise click.UsageError(
                f"{', '.join(given)} cannot be combined with --from-snapshot "
                "(the snapshot fixes the OS and the user)"
            )
        _create_from_snapshot(
            v, name, from_snapshot, keep_hostname, ram, vcpu, disk, ssh_key,
            mounts, scripts, no_boot, as_json,
        )  # fmt: skip
        return

    distro = distro or "ubuntu"
    ram, vcpu, disk = ram or 4, vcpu or 2, disk or 30
    profile = PROFILES[distro]

    # --release/--image precedence (D1): an explicit --release wins over
    # --image regardless of order, even when its value equals the default.
    release_given = release is not None
    image_given = image is not None
    effective_release = release if release_given else profile.default_release
    if profile.releases is not None and effective_release not in profile.releases:
        raise click.UsageError(
            f"{distro}: only release '{profile.releases[0]}' is supported; "
            "for other builds pass the direct URL via --image"
        )

    # Everything the result document may need, filled in as stages complete.
    effective_user = user if user is not None else profile.suggested_user
    run_state: dict[str, Any] = {
        "name": name,
        "distro": distro,
        "release": effective_release,
        "arch": v.host_arch(),
        "ram_gib": ram,
        "vcpu": vcpu,
        "disk_gib": disk,
        "user": effective_user,
        "hostname": name,
        "ssh_identity": v.private_key_path(ssh_key),
        "image_url": None,
        "booted": False,
        "created": False,
        "mounts": mounts,
        "scripts": [],
    }

    # --------------------------------------------------------------
    # Pipeline — one try, one stage variable
    # --------------------------------------------------------------
    stage = "preflight"
    try:
        # URL resolution first: a distro without an image for the host arch
        # (arch on aarch64) fails here, before any network activity.
        if release_given or not image_given:
            run_state["image_url"] = profile.image_url(v.host_arch(), effective_release)
        else:
            run_state["image_url"] = image

        v.preflight_check()
        v.require_domain_absent(name)
        if mounts:
            v.require_virtiofsd()
        # Generated on first use when absent, so no manual key setup is needed.
        v.ensure_ssh_key(ssh_key)

        stage = "image"
        image_fetch: ImageFetch = fetch_and_verify_image(
            profile=profile,
            arch=v.host_arch(),
            release=effective_release,
            release_given=release_given,
            image_url=run_state["image_url"],
            image_given=image_given,
            keep_going=keep_going,
        )
        run_state["image"] = image_fetch

        stage = "cloud-init"
        user_data, meta_data = v.generate_cloud_init_files(
            name=name,
            user_name=effective_user,
            sudo_group=profile.sudo_group,
            ssh_key=ssh_key,
            mounts=mounts,
        )
        # The seed ISO must survive the guest's first boot (virt-install's
        # own --cloud-init ISO does not — see create_vm), so virt-runner
        # builds and owns it as a pool volume (destroy removes it).
        cloud_init_iso = v.build_cloud_init_iso(name, user_data, meta_data)

        stage = "create"
        volume = v.provision_volume(name, disk, image_fetch.path)
        run_state["mac"] = mac = v.generate_mac()
        domain_state = v.create_vm(
            name=name,
            ram_mib=ram * 1024,
            vcpus=vcpu,
            volume=volume,
            mac=mac,
            cloud_init_iso=cloud_init_iso,
            os_variant=profile.os_variant,
            no_boot=no_boot,
            mounts=mounts,
        )
        run_state.update(
            created=True, domain_state=domain_state, uuid=v.get_domain_uuid(name)
        )
        _record_metadata(
            v,
            name,
            distro,
            effective_user,
            run_state["ssh_identity"],
            release=effective_release,
            image_url=run_state["image_url"],
            mounts=json.dumps(mounts),
        )

        if not no_boot:
            stage = "wait-ip"
            _boot(v, run_state, mac, scripts)
    except RuntimeError as exc:
        stage = getattr(exc, "stage", None) or stage
        output.fail_with(
            "create",
            exc,
            _DEFAULT_CODE[stage],
            stage=stage,
            fields=_create_document(run_state),
        )

    _report(run_state, no_boot, as_json)


def _boot(
    v: Virtualizer, run_state: dict[str, Any], mac: str, scripts: tuple[Path, ...]
) -> None:
    """The ``wait-ip`` → ``ssh-verify`` → ``script`` stages.

    Shared by both pipelines; a failure leaves the stage it happened in on
    the exception, which the caller's handler reads before its own ``stage``.
    """
    name, user = run_state["name"], run_state["user"]
    identity = run_state["ssh_identity"]
    stage = "wait-ip"
    try:
        ip = v.wait_for_ip(name, mac, lease_file("create"))
        run_state["booted"] = True
        run_state["ip"] = ip
        output.progress(f"IP acquired: {ip}")

        stage = "ssh-verify"
        v.verify_ssh_reachable(name, user, ip, identity=identity)

        stage = "script"
        for script in scripts:
            output.progress(f"Running script: {script}")
            rc = v.run_script(user, ip, script, identity=identity)
            run_state["scripts"].append({"path": str(script), "exit_code": rc})
            if rc != 0:
                raise VirtError(
                    f"script {script} exited {rc} — VM kept, inspect with: "
                    f"virt-runner ssh {name}",
                    "script-failed",
                    "script",
                )
    except RuntimeError as exc:
        exc.stage = getattr(exc, "stage", None) or stage
        raise


def _report(run_state: dict[str, Any], no_boot: bool, as_json: bool) -> None:
    """Success output: the JSON document, or the human-readable block."""
    if as_json:
        output.emit("create", _create_document(run_state))
        return

    name = run_state["name"]
    os_line = f"{run_state['distro']} {run_state['release']}"
    snap: SnapshotInfo | None = run_state.get("snapshot")
    if no_boot:
        click.echo(f"VM created (not booted): {name}")
        click.echo(f"Distro:  {os_line}")
    else:
        click.echo("VM created and running.")
        click.echo(f"Name:    {name}   (UUID {run_state['uuid']})")
        click.echo(f"Distro:  {os_line}")
        if snap and run_state["keep_hostname"]:
            click.echo(
                f"IP:      {run_state['ip']}   (hostname kept: "
                f"{run_state['hostname']}; use the IP, not a DNS name)"
            )
        else:
            click.echo(
                f"IP:      {run_state['ip']}   (also reachable as {name}.default)"
            )
        click.echo(f"SSH:     virt-runner ssh {name}")
    if snap:
        click.echo(f"Snapshot: {snap.name}")
    for m in run_state["mounts"]:
        click.echo(f"Mount:   {m['source']} -> {m['target']}")
    for s in run_state["scripts"]:
        click.echo(f"Script:  {s['path']} (exit {s['exit_code']})")
    for line in output.text_access_lines(name):
        click.echo(line)


def _create_from_snapshot(
    v: Virtualizer,
    name: str,
    snap_name: str,
    keep_hostname: bool,
    ram: int | None,
    vcpu: int | None,
    disk: int | None,
    ssh_key: str,
    mounts: list[dict[str, str]],
    scripts: tuple[Path, ...],
    no_boot: bool,
    as_json: bool,
) -> None:
    """``create --from-snapshot``: the same pipeline on a thin overlay disk.

    The clone boots with a fresh seed (new instance-id, hostname = *name*
    unless *keep_hostname*), so the sealed guest runs first-boot setup again:
    new SSH host keys, machine-id, network config for the new MAC.
    """
    run_state: dict[str, Any] = {
        "name": name,
        "arch": v.host_arch(),
        "ssh_identity": v.private_key_path(ssh_key),
        "keep_hostname": keep_hostname,
        "booted": False,
        "created": False,
        "mounts": mounts,
        "scripts": [],
    }
    stage = "preflight"
    try:
        v.preflight_check()
        v.require_domain_absent(name)
        if mounts:
            v.require_virtiofsd()
        v.ensure_ssh_key(ssh_key)

        stage = "snapshot"
        try:
            snap = v.read_snapshot_meta(snap_name)
        except VirtError as exc:
            exc.stage = "snapshot"
            raise
        defaults = snap.defaults
        disk = disk or defaults.get("disk_gib") or snap.virtual_gib
        if disk < snap.virtual_gib:
            raise VirtError(
                f"--disk {disk} is smaller than snapshot '{snap.name}' "
                f"({snap.virtual_gib} GiB); disks can only grow",
                "disk-shrink-unsupported",
                "snapshot",
            )
        run_state.update(
            snapshot=snap,
            distro=snap.distro,
            release=snap.release,
            user=snap.user,
            ram_gib=ram or defaults.get("ram_gib") or 4,
            vcpu=vcpu or defaults.get("vcpu") or 2,
            disk_gib=disk,
            hostname=(snap.guest_hostname or name) if keep_hostname else name,
        )

        stage = "cloud-init"
        user_data, meta_data = v.generate_cloud_init_files(
            name=name,
            user_name=snap.user,
            sudo_group=snap.sudo_group,
            ssh_key=ssh_key,
            mounts=mounts,
            hostname=run_state["hostname"],
        )
        cloud_init_iso = v.build_cloud_init_iso(name, user_data, meta_data)

        stage = "create"
        try:
            volume = v.create_overlay_volume(name, disk, snap.name)
        except RuntimeError:
            v.delete_cloud_init_iso_quiet(name)
            raise
        run_state["mac"] = mac = v.generate_mac()
        domain_state = v.create_vm(
            name=name,
            ram_mib=run_state["ram_gib"] * 1024,
            vcpus=run_state["vcpu"],
            volume=volume,
            mac=mac,
            cloud_init_iso=cloud_init_iso,
            os_variant=snap.os_variant,
            no_boot=no_boot,
            mounts=mounts,
        )
        run_state.update(
            created=True, domain_state=domain_state, uuid=v.get_domain_uuid(name)
        )
        _record_metadata(
            v,
            name,
            snap.distro or "",
            snap.user,
            run_state["ssh_identity"],
            release=snap.release,
            image_url=(snap.meta.get("parent") or {}).get("image_url"),
            mounts=json.dumps(mounts),
            snapshot=snap.name,
        )

        if not no_boot:
            stage = "wait-ip"
            _boot(v, run_state, mac, scripts)
    except RuntimeError as exc:
        stage = getattr(exc, "stage", None) or stage
        output.fail_with(
            "create",
            exc,
            _DEFAULT_CODE[stage],
            stage=stage,
            fields=_create_document(run_state),
        )

    _report(run_state, no_boot, as_json)
