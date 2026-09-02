"""``create`` subcommand — full pipeline: preflight → image → cloud-init →
virt-install → IP/SSH → access info.

Both output modes read from one accumulating run state (spec §11.3): the text
report prints the fields it has always printed, ``--json`` renders the same
state as the §5.3.1 document — including on late failures, where the already
created ``vm``/``image`` sections still describe real resources.
"""

from __future__ import annotations

import os
import re
from typing import Any

import click

from virt_runner import output
from virt_runner.args import command
from virt_runner.virtualizer import ImageFetch, Virtualizer

#: The only profile implemented so far (distro profiles are a later ticket).
DISTRO = "ubuntu"


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


def _create_document(run_state: dict[str, Any]) -> dict[str, Any]:
    """Render the sections completed so far, in the spec §5.3.1 order.

    ``vm`` appears once the domain exists and ``image`` once the image stage
    finished, so a preflight failure carries neither while a lease timeout
    still reports the running VM and the image it booted from.
    """
    fields: dict[str, Any] = {"booted": bool(run_state.get("booted"))}

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
            "mac": run_state.get("mac"),
            "ip": ip,
            "dns_name": f"{name}.default",
            "ssh_user": run_state["user"],
            **output.access_commands(name, run_state["user"], ip),
            # virt-install is always invoked with --autostart.
            "autostart": True,
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

    return fields


@command(name="create")
@click.argument("name", callback=_validate_name)
@click.option(
    "--ram",
    default=4,
    type=click.IntRange(min=1),
    help="Guest RAM in GiB (positive integer) [default: 4]",
)
@click.option(
    "--vcpu",
    default=2,
    type=click.IntRange(min=1),
    help="vCPU count (positive integer) [default: 2]",
)
@click.option(
    "--disk",
    default=30,
    type=click.IntRange(min=1),
    help="Virtual disk size in GiB [default: 30]",
)
@click.option(
    "--release",
    default=None,
    help="Release codename (resolute, noble, …) "
    f"[default: {Virtualizer.DEFAULT_RELEASE}]",
)
@click.option(
    "--image",
    default=None,
    help="Direct URL of a raw cloud image",
)
@click.option(
    "--user",
    default=Virtualizer.DEFAULT_USER,
    help="Cloud user name [default: ubuntu]",
)
@click.option(
    "--ssh-key",
    default=Virtualizer.DEFAULT_SSH_KEY,
    help="Path to the public key to inject; generated when absent "
    "[default: ~/.ssh/virt_runner_key.pub]",
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
def cmd_create(
    name: str,
    ram: int,
    vcpu: int,
    disk: int,
    release: str,
    image: str | None,
    user: str,
    ssh_key: str,
    no_boot: bool,
    keep_going: bool,
    as_json: bool,
) -> None:
    """Create and boot an Ubuntu KVM VM via libvirt (qemu:///system)."""
    output.set_json_mode(as_json)
    v = Virtualizer()

    # --release/--image precedence (D1): an explicit --release wins over
    # --image regardless of order, even when its value equals the default.
    release_given = release is not None
    image_given = image is not None
    effective_release = release if release_given else Virtualizer.DEFAULT_RELEASE

    if release_given or not image_given:
        image_url = (
            f"{v.DEFAULT_IMAGE_BASE}/{effective_release}/current/"
            f"{effective_release}-server-cloudimg-amd64.img"
        )
    else:
        image_url = image

    # Everything the result document may need, filled in as stages complete.
    run_state: dict[str, Any] = {
        "name": name,
        "distro": DISTRO,
        "release": effective_release,
        "arch": v.host_arch(),
        "ram_gib": ram,
        "vcpu": vcpu,
        "disk_gib": disk,
        "user": user,
        "image_url": image_url,
        "booted": False,
        "created": False,
    }

    # --------------------------------------------------------------
    # Step 1 — Preflight
    # --------------------------------------------------------------
    try:
        v.preflight_check()
    except RuntimeError as exc:
        output.fail_with("create", exc, "libvirt-unreachable", stage="preflight")

    try:
        v.domain_not_defined(name)
    except RuntimeError as exc:
        output.fail_with("create", exc, "vm-already-defined", stage="preflight")

    try:
        # Generated on first use when absent, so no manual key setup is needed.
        v.ensure_ssh_key(ssh_key)
    except RuntimeError as exc:
        output.fail_with("create", exc, "ssh-key-missing", stage="preflight")

    # --------------------------------------------------------------
    # Step 2 — Image download + verify
    # --------------------------------------------------------------
    try:
        image_fetch = v.fetch_and_verify_image(
            release=effective_release,
            release_given=release_given,
            image_url=image_url,
            image_given=image_given,
            keep_going=keep_going,
        )
    except RuntimeError as exc:
        output.fail_with("create", exc, "image-download-failed", stage="image")
    run_state["image"] = image_fetch

    # --------------------------------------------------------------
    # Step 3 — Cloud-init
    # --------------------------------------------------------------
    try:
        _, user_data, meta_data = v.generate_cloud_init_files(
            name=name,
            user_name=user,
            ssh_key=ssh_key,
        )
    except RuntimeError as exc:
        output.fail_with("create", exc, "cloud-init-failed", stage="cloud-init")

    # --------------------------------------------------------------
    # Step 4 — VM creation (volume + domain; spec stage "create")
    # --------------------------------------------------------------
    try:
        volume = v.create_volume(name, disk)
    except RuntimeError as exc:
        output.fail_with(
            "create",
            exc,
            "volume-create-failed",
            stage="create",
            fields=_create_document(run_state),
        )

    try:
        v.upload_image(volume, image_fetch.path)
    except RuntimeError as exc:
        # Clean up orphan volume.
        v.delete_volume_quiet(volume)
        output.fail_with(
            "create",
            exc,
            "volume-import-failed",
            stage="create",
            fields=_create_document(run_state),
        )

    try:
        v.resize_volume(volume, disk)
    except RuntimeError as exc:
        # No orphan volume behind: the domain does not exist yet.
        v.delete_volume_quiet(volume)
        output.fail_with(
            "create",
            exc,
            "volume-resize-failed",
            stage="create",
            fields=_create_document(run_state),
        )

    mac = v.generate_mac()
    run_state["mac"] = mac

    ram_mib = ram * 1024

    try:
        domain_state = v.create_vm(
            name=name,
            ram_mib=ram_mib,
            vcpus=vcpu,
            volume=volume,
            mac=mac,
            cloud_init_user_data=user_data,
            cloud_init_meta_data=meta_data,
            ssh_key=ssh_key,
            no_boot=no_boot,
        )
    except RuntimeError as exc:
        output.fail_with(
            "create",
            exc,
            "vm-create-failed",
            stage="create",
            fields=_create_document(run_state),
        )

    run_state.update(
        created=True, domain_state=domain_state, uuid=v.get_domain_uuid(name)
    )

    # --no-boot: print not-booted variant.
    if no_boot:
        if as_json:
            output.emit("create", _create_document(run_state))
            return
        click.echo(f"VM created (not booted): {name}")
        click.echo(f"Distro:  {DISTRO} {effective_release}")
        click.echo(f"Console: virsh console {name}     (Ctrl-] to detach)")
        click.echo(
            f"Teardown: virt-runner destroy {name}   "
            f"(or: virsh destroy {name} && virsh undefine {name})"
        )
        return

    # --------------------------------------------------------------
    # Step 5 — IP discovery + SSH verification
    # --------------------------------------------------------------
    lease_file = os.environ.get(
        "VM_CREATE_LEASE_FILE",
        Virtualizer.DEFAULT_LEASE_FILE,
    )

    try:
        ip = v.wait_for_ip(name, mac, lease_file)
    except RuntimeError as exc:
        output.fail_with(
            "create",
            exc,
            "lease-timeout",
            stage="wait-ip",
            fields=_create_document(run_state),
        )

    run_state["booted"] = True
    run_state["ip"] = ip

    output.progress(f"IP acquired: {ip}")

    try:
        v.verify_ssh_reachable(name, user, ip)
    except RuntimeError as exc:
        output.fail_with(
            "create",
            exc,
            "ssh-timeout",
            stage="ssh-verify",
            fields=_create_document(run_state),
        )

    # --------------------------------------------------------------
    # Step 6 — Access info
    # --------------------------------------------------------------
    if as_json:
        output.emit("create", _create_document(run_state))
        return

    click.echo("VM created and running.")
    click.echo(f"Name:    {name}   (UUID {run_state['uuid']})")
    click.echo(f"Distro:  {DISTRO} {effective_release}")
    click.echo(f"IP:      {ip}   (also reachable as {name}.default)")
    click.echo(f"SSH:     ssh {user}@{ip}")
    click.echo(f"Console: virsh console {name}     (Ctrl-] to detach)")
    click.echo(
        f"Teardown: virt-runner destroy {name}   "
        f"(or: virsh destroy {name} && virsh undefine {name})"
    )
