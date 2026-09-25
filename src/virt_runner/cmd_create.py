"""``create`` subcommand — full pipeline: preflight → image → cloud-init →
virt-install → IP/SSH → access info.

Both output modes read from one accumulating run state (spec §11.3): the text
report prints the fields it has always printed, ``--json`` renders the same
state as the §5.3.1 document — including on late failures, where the already
created ``vm``/``image`` sections still describe real resources.
"""

from __future__ import annotations

import re
from typing import Any

import click

from virt_runner import output
from virt_runner.args import command
from virt_runner.profiles import PROFILES
from virt_runner.virtualizer import ImageFetch, Virtualizer, lease_file

_DEFAULT_CODE = {
    "preflight": "libvirt-unreachable",
    "image": "image-download-failed",
    "cloud-init": "cloud-init-failed",
    "create": "vm-create-failed",
    "wait-ip": "lease-timeout",
    "ssh-verify": "ssh-timeout",
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


def _record_metadata(
    v: Virtualizer, name: str, distro: str, user: str, identity: str
) -> None:
    """Record the per-VM user so `ssh`/`list` log in as the created user."""
    try:
        v.set_domain_metadata(name, distro, user, identity)
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
            **output.access_commands(
                name, run_state["user"], ip, run_state["ssh_identity"]
            ),
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
    "--distro",
    type=click.Choice(sorted(PROFILES)),
    default="ubuntu",
    help="Guest distro profile [default: ubuntu]",
)
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
    distro: str,
    release: str | None,
    image: str | None,
    user: str | None,
    ssh_key: str,
    no_boot: bool,
    keep_going: bool,
    as_json: bool,
) -> None:
    """Create and boot a KVM VM (Ubuntu, Arch, or Fedora) via libvirt (qemu:///system)."""
    output.set_json_mode(as_json)
    v = Virtualizer()
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

    if release_given or not image_given:
        image_url = profile.image_url(effective_release)
    else:
        image_url = image

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
        "ssh_identity": v.private_key_path(ssh_key),
        "image_url": image_url,
        "booted": False,
        "created": False,
    }

    # --------------------------------------------------------------
    # Pipeline — one try, one stage variable
    # --------------------------------------------------------------
    stage = "preflight"
    ip = None
    try:
        v.preflight_check()
        v.domain_not_defined(name)
        # Generated on first use when absent, so no manual key setup is needed.
        v.ensure_ssh_key(ssh_key)

        stage = "image"
        image_fetch: ImageFetch = v.fetch_and_verify_image(
            profile=profile,
            release=effective_release,
            release_given=release_given,
            image_url=image_url,
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
        )

        stage = "create"
        volume = v.provision_volume(name, disk, image_fetch.path)
        run_state["mac"] = mac = v.generate_mac()
        domain_state = v.create_vm(
            name=name,
            ram_mib=ram * 1024,
            vcpus=vcpu,
            volume=volume,
            mac=mac,
            cloud_init_user_data=user_data,
            cloud_init_meta_data=meta_data,
            ssh_key=ssh_key,
            os_variant=profile.os_variant,
            no_boot=no_boot,
        )
        run_state.update(
            created=True, domain_state=domain_state, uuid=v.get_domain_uuid(name)
        )
        _record_metadata(v, name, distro, effective_user, run_state["ssh_identity"])

        if not no_boot:
            stage = "wait-ip"
            ip = v.wait_for_ip(name, mac, lease_file("create"))
            run_state["booted"] = True
            run_state["ip"] = ip
            output.progress(f"IP acquired: {ip}")

            stage = "ssh-verify"
            v.verify_ssh_reachable(
                name, effective_user, ip, identity=run_state["ssh_identity"]
            )
    except RuntimeError as exc:
        output.fail_with(
            "create",
            exc,
            _DEFAULT_CODE[stage],
            stage=getattr(exc, "stage", None) or stage,
            fields=_create_document(run_state),
        )

    # --------------------------------------------------------------
    # Access info
    # --------------------------------------------------------------
    if as_json:
        output.emit("create", _create_document(run_state))
        return

    if no_boot:
        click.echo(f"VM created (not booted): {name}")
        click.echo(f"Distro:  {distro} {effective_release}")
    else:
        click.echo("VM created and running.")
        click.echo(f"Name:    {name}   (UUID {run_state['uuid']})")
        click.echo(f"Distro:  {distro} {effective_release}")
        click.echo(f"IP:      {ip}   (also reachable as {name}.default)")
        click.echo(f"SSH:     virt-runner ssh {name}")
    click.echo(f"Console: virsh console {name}     (Ctrl-] to detach)")
    click.echo(
        f"Teardown: virt-runner destroy {name}   "
        f"(or: virsh destroy {name} && virsh undefine {name})"
    )
