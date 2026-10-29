"""``snapshot`` command group — ``create``, ``list``, ``show``, ``delete``.

A snapshot is a sealed, flattened copy of a VM's disk in the pool
(``<SNAP>.snapshot.qcow2``) plus a JSON sidecar volume (``<SNAP>.snapshot.json``).
It is a template for new VMs (``create --from-snapshot``), not a restore point.

``snapshot create`` follows the ``create`` pattern: one accumulating run state,
one ``stage`` variable, and one document renderer for success and failure.
"""

from __future__ import annotations

import json
import time
from datetime import datetime, timezone
from typing import Any

import click

from virt_runner import output
from virt_runner.args import command
from virt_runner.cmd_create import _validate_name
from virt_runner.errors import VirtError
from virt_runner.profiles import PROFILES
from virt_runner.virtualizer import (
    SNAPSHOT_SCHEMA,
    Virtualizer,
    lease_file,
    parse_vm_spec,
    snapshot_volume,
)

_DEFAULT_CODE = {
    "preflight": "libvirt-unreachable",
    "seal": "snapshot-seal-failed",
    "shutdown": "vm-shutdown-timeout",
    "copy": "snapshot-copy-failed",
    "metadata": "snapshot-metadata-failed",
    "restart": "runtime-error",
}

_GIB = 1024**3


def _gib(n_bytes: int | None, digits: int = 2) -> float | None:
    return None if n_bytes is None else round(n_bytes / _GIB, digits)


def _snapshot_section(v: Virtualizer, snap: str) -> dict[str, Any]:
    """Where a snapshot lives and how big it is (both from ``vol-info``)."""
    sizes = v.volume_bytes(snapshot_volume(snap))
    virtual = _gib(sizes.get("capacity"), 0)
    return {
        "name": snap,
        "pool": v.POOL,
        "volume": snapshot_volume(snap),
        "path": v.snapshot_path(snap),
        "virtual_gib": None if virtual is None else int(virtual),
        "allocated_gib": _gib(sizes.get("allocation")),
    }


def _create_document(run_state: dict[str, Any]) -> dict[str, Any]:
    """§5.1 document: ``snapshot`` once it exists, ``source`` once it is known."""
    return {
        "snapshot": run_state.get("snapshot"),
        "source": run_state.get("source"),
        "timings_s": run_state["timings_s"],
        "warnings": run_state["warnings"],
    }


@click.group(name="snapshot")
def cmd_snapshot() -> None:
    """Snapshot a VM's disk and manage snapshots (clone with `create --from-snapshot`)."""


@command(name="snapshot-create")
@click.argument("vm", callback=_validate_name)
@click.argument("snap", callback=_validate_name)
@click.option(
    "--start-source",
    is_flag=True,
    default=False,
    help="Start the source VM again afterwards (it re-runs first-boot setup)",
)
@click.option(
    "--shutdown-timeout",
    default=300,
    type=click.IntRange(min=1),
    help="Seconds the guest gets to power off; never forced [default: 300]",
)
def snapshot_create(
    vm: str, snap: str, start_source: bool, shutdown_timeout: int, as_json: bool
) -> None:
    """Seal VM, shut it down and copy its disk into snapshot SNAP.

    The source stays shut off (unless --start-source). Starting it again
    re-runs cloud-init first-boot setup: it gets new SSH host keys.
    """
    output.set_json_mode(as_json)
    v = Virtualizer()
    run_state: dict[str, Any] = {"timings_s": {}, "warnings": []}

    stage = "preflight"
    try:
        v.preflight_check()
        if not v.domain_exists(vm):
            raise VirtError(f"VM '{vm}' is not defined", "vm-not-defined", stage)
        src_volume = v.disk_volume(vm)
        if src_volume is None or not v.owns_domain(vm):
            raise VirtError(
                f"VM '{vm}' was not created by virt-runner (no disk in pool "
                f"'{v.POOL}') — refusing to snapshot it",
                "not-a-virt-runner-vm",
                stage,
            )
        if snap in v.snapshot_names():
            raise VirtError(
                f"snapshot '{snap}' already exists — delete it first: "
                f"virt-runner snapshot delete {snap}",
                "snapshot-already-exists",
                stage,
            )

        meta = v.domain_meta(vm)
        distro = meta.get("distro")
        profile = PROFILES.get(distro or "ubuntu", PROFILES["ubuntu"])
        if distro not in PROFILES:
            output.warn(f"no distro recorded for '{vm}' — assuming {profile.name}")
        user = meta.get("user") or Virtualizer.DEFAULT_USER
        identity = meta.get("identity") or v.private_key_path(v.DEFAULT_SSH_KEY)
        root, xml = v.domain_xml_text(vm)
        spec = parse_vm_spec(xml)
        mac = v._mac_of(root) if root is not None else ""
        was_running = v.get_domain_state(vm) == "running"
        source = run_state["source"] = {
            "name": vm,
            "uuid": root.findtext("uuid", "") if root is not None else "",
            "was_running": was_running,
            "started_for_seal": False,
            "sealed": False,
            "state": "running" if was_running else "shut off",
        }

        # An unsealed disk is never copied: its clones would ignore their seed.
        stage = "seal"
        t0 = time.monotonic()
        if not was_running:
            output.progress(f"Starting '{vm}' to seal it")
            v.start_domain(vm)
            source.update(started_for_seal=True, state="running")
        ip = v.wait_for_ip(vm, mac, lease_file("snapshot"))
        v.verify_ssh_reachable(vm, user, ip, identity=identity)
        output.progress(f"Sealing '{vm}' ({user}@{ip})")
        guest_hostname = v.seal_guest(user, ip, identity)
        source["sealed"] = True
        run_state["timings_s"]["seal"] = round(time.monotonic() - t0, 1)

        stage = "shutdown"
        t0 = time.monotonic()
        output.progress(f"Shutting down '{vm}' (up to {shutdown_timeout}s)")
        v.shutdown_and_wait(vm, shutdown_timeout)
        source["state"] = v.get_domain_state(vm)
        run_state["timings_s"]["shutdown"] = round(time.monotonic() - t0, 1)

        stage = "copy"
        t0 = time.monotonic()
        output.progress(f"Copying {src_volume} -> {snapshot_volume(snap)}")
        v.copy_to_snapshot(src_volume, snap)
        run_state["timings_s"]["copy"] = round(time.monotonic() - t0, 1)

        stage = "metadata"
        section = _snapshot_section(v, snap)
        section["created_at"] = datetime.now(timezone.utc).strftime(
            "%Y-%m-%dT%H:%M:%SZ"
        )
        sidecar = {
            "schema": SNAPSHOT_SCHEMA,
            "name": snap,
            "created_at": section["created_at"],
            "virt_runner_version": output.version(),
            "source": {"name": vm, "uuid": source["uuid"], "mac": mac},
            "distro": distro,
            "release": meta.get("release"),
            "arch": v.host_arch(),
            "os_variant": profile.os_variant,
            "sudo_group": profile.sudo_group,
            "user": user,
            "ssh_identity": identity,
            "guest_hostname": guest_hostname or None,
            "defaults": {
                "ram_gib": spec.ram_kib // (1024 * 1024) if spec else None,
                "vcpu": spec.vcpu if spec else None,
                "disk_gib": section["virtual_gib"],
            },
            "virtual_gib": section["virtual_gib"],
            # Informational only: clones get just their own --mount set.
            "mounts": _json_list(meta.get("mounts")),
            "parent": {
                "image_url": meta.get("image_url"),
                "snapshot": meta.get("snapshot"),
            },
        }
        try:
            v.write_snapshot_meta(snap, sidecar)
        except RuntimeError:
            # A snapshot without metadata is not listed: leave nothing behind.
            v.delete_volume_quiet(snapshot_volume(snap))
            raise
        run_state["snapshot"] = section

        if start_source:
            stage = "restart"
            try:
                v.start_domain(vm)
            except RuntimeError as exc:
                run_state["warnings"].append(
                    f"snapshot created, but '{vm}' did not start again: {exc}"
                )
        source["state"] = v.get_domain_state(vm)
    except RuntimeError as exc:
        output.fail_with(
            "snapshot-create",
            exc,
            _DEFAULT_CODE[stage],
            stage=stage,
            fields=_create_document(run_state),
        )

    if as_json:
        output.emit("snapshot-create", _create_document(run_state))
        return

    for w in run_state["warnings"]:
        output.warn(f"Warning: {w}")
    s = run_state["snapshot"]
    click.echo(f"Snapshot created: {snap}")
    click.echo(f"Volume:  {s['path']}")
    click.echo(
        f"Size:    {s['virtual_gib']} GiB virtual / {s['allocated_gib']} GiB allocated"
    )
    click.echo(f"Clone:   virt-runner create NAME --from-snapshot {snap}")
    if source["state"] == "running":
        click.echo(
            f"Notice:  '{vm}' was sealed and has been started again: it re-ran "
            "cloud-init first-boot setup and has new SSH host keys."
        )
    else:
        click.echo(
            f"Notice:  '{vm}' is sealed and stays {source['state']}. Starting it "
            "again re-runs cloud-init first-boot setup: it gets new SSH host keys."
        )


def _json_list(value: str | None) -> list[Any] | None:
    """A JSON-list metadata attribute (``mounts``), ``None`` if absent or bad."""
    try:
        parsed = json.loads(value) if value else None
    except json.JSONDecodeError:
        return None
    return parsed if isinstance(parsed, list) else None


def _require_pool(v: Virtualizer, cmd: str, fields: dict[str, Any] | None = None):
    try:
        v.require_libvirt()
        v.require_pool_defined()
    except RuntimeError as exc:
        output.fail_with(cmd, exc, "pool-not-found", fields=fields)


def _list_entry(
    v: Virtualizer, snap: str, users: dict[str, list[str]]
) -> dict[str, Any]:
    """One §5.2 entry; a snapshot missing either half is listed as incomplete."""
    section = _snapshot_section(v, snap)
    clones = v.snapshot_dependents(snap, users)
    try:
        meta = v.read_snapshot_meta(snap).meta
    except VirtError:
        return {
            "name": snap,
            "metadata": None,
            "state": "incomplete",
            "virtual_gib": section["virtual_gib"],
            "allocated_gib": section["allocated_gib"],
            "clones": clones,
        }
    return {
        "name": snap,
        "created_at": meta.get("created_at"),
        "source_vm": (meta.get("source") or {}).get("name"),
        "distro": meta.get("distro"),
        "release": meta.get("release"),
        "arch": meta.get("arch"),
        "virtual_gib": section["virtual_gib"],
        "allocated_gib": section["allocated_gib"],
        "clones": clones,
    }


def _text_block(entry: dict[str, Any]) -> list[str]:
    """The text view of one :func:`_list_entry`."""
    name = entry["name"]
    clones = ", ".join(entry["clones"]) or "none"
    if entry.get("state") == "incomplete":
        return [
            f"Snapshot: {name}   (incomplete — virt-runner snapshot delete {name})",
            f"Clones:   {clones}",
        ]
    os_line = " ".join(str(entry[k] or "?") for k in ("distro", "release", "arch"))
    return [
        f"Snapshot: {name}",
        f"Source:   {entry['source_vm']} ({os_line}), created {entry['created_at']}",
        (
            f"Size:     {entry['virtual_gib']} GiB virtual / "
            f"{entry['allocated_gib']} GiB allocated"
        ),
        f"Clones:   {clones}",
    ]


@command(name="snapshot-list")
def snapshot_list(as_json: bool) -> None:
    """List snapshots in the vm-pool, with the clones backed by each."""
    output.set_json_mode(as_json)
    v = Virtualizer()
    _require_pool(v, "snapshot-list")
    try:
        users = v.backing_users()
        entries = [_list_entry(v, snap, users) for snap in v.snapshot_names()]
    except RuntimeError as exc:
        output.fail_with("snapshot-list", exc, "pool-not-found")

    if as_json:
        output.emit(
            "snapshot-list",
            {"pool": v.POOL, "count": len(entries), "snapshots": entries},
        )
        return
    if not entries:
        click.echo(
            f"No snapshots in pool '{v.POOL}' "
            "(virt-runner snapshot create VM SNAP to create one)."
        )
    for entry in entries:
        for line in _text_block(entry):
            click.echo(line)


@command(name="snapshot-show")
@click.argument("snap", callback=_validate_name)
def snapshot_show(snap: str, as_json: bool) -> None:
    """Print a snapshot's metadata, size and clones."""
    output.set_json_mode(as_json)
    v = Virtualizer()
    _require_pool(v, "snapshot-show", {"name": snap})
    try:
        info = v.read_snapshot_meta(snap)
        section = _snapshot_section(v, snap)
        shown = {
            **info.meta,
            "volume": section["volume"],
            "path": section["path"],
            "allocated_gib": section["allocated_gib"],
            "clones": v.snapshot_dependents(snap),
        }
    except RuntimeError as exc:
        output.fail_with(
            "snapshot-show", exc, "snapshot-not-found", fields={"name": snap}
        )

    if as_json:
        output.emit("snapshot-show", {"snapshot": shown})
        return
    click.echo(json.dumps(shown, indent=2, ensure_ascii=False))


@command(name="snapshot-delete")
@click.argument("snap", callback=_validate_name)
def snapshot_delete(snap: str, as_json: bool) -> None:
    """Delete a snapshot; refused while any clone disk is backed by it."""
    output.set_json_mode(as_json)
    v = Virtualizer()
    _require_pool(v, "snapshot-delete", {"name": snap})
    try:
        if snap not in v.snapshot_names():
            raise VirtError(f"snapshot '{snap}' not found", "snapshot-not-found")
        clones = v.snapshot_dependents(snap)
    except RuntimeError as exc:
        output.fail_with(
            "snapshot-delete", exc, "snapshot-not-found", fields={"name": snap}
        )
    if clones:
        # No --force: deleting a backing file corrupts every clone on it, and
        # libvirt itself does not refuse.
        output.fail(
            "snapshot-delete",
            f"snapshot '{snap}' is in use by {', '.join(clones)} — destroy "
            "those VMs first (virt-runner destroy NAME)",
            code="snapshot-in-use",
            fields={"name": snap, "clones": clones},
        )
    try:
        volumes = v.delete_snapshot(snap)
    except RuntimeError as exc:
        output.fail_with(
            "snapshot-delete", exc, "volume-delete-failed", fields={"name": snap}
        )

    warnings = [
        f"volume '{vol['name']}' in pool '{v.POOL}' was already absent"
        for vol in volumes
        if vol["already_absent"]
    ]
    if as_json:
        output.emit(
            "snapshot-delete", {"name": snap, "volumes": volumes, "warnings": warnings}
        )
        return
    for w in warnings:
        output.warn(f"Warning: {w}")
    click.echo(f"Snapshot '{snap}' deleted.")


cmd_snapshot.add_command(snapshot_create, "create")
cmd_snapshot.add_command(snapshot_list, "list")
cmd_snapshot.add_command(snapshot_show, "show")
cmd_snapshot.add_command(snapshot_delete, "delete")
