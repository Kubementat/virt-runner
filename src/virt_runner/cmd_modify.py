"""``modify`` subcommand — change vCPU, RAM, disk size and networking of a VM.

Everything that decides *what* would change lives in pure functions
(:class:`Request`, :func:`plan_changes`, :func:`resolve_net`); the Click command
only reads the current :class:`~virt_runner.virtualizer.VmSpec`, hands the plan
to :func:`apply_changes` and renders the result. A failure while applying leaves
the changes that really ran in the error document and marks the ones that did not
with ``applied: false`` — both are facts about real resources.

Sizing follows libvirt's own ceiling rules (see
``docs/lessons-learned/010-libvirt-live-vs-config-changes.md`` and
``docs/lessons-learned/013-virsh-option-spelling.md``): a change that cannot be
applied to the running domain is written to the persisted config and reported as
``effective: "on-boot"`` with a warning instead of failing.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field, replace
from typing import Any

import click

from virt_runner import output
from virt_runner.args import command
from virt_runner.errors import VirtError
from virt_runner.virtualizer import (
    EFFECTIVE_NOW,
    EFFECTIVE_ON_BOOT,
    Interface,
    Virtualizer,
    VmSpec,
    lease_file,
)

_MAC_RE = re.compile(r"^[0-9A-Fa-f]{2}(:[0-9A-Fa-f]{2}){5}$")

#: ``modify-noop`` is a request the tool will not act on, so it exits like a
#: usage error (2) rather than like a runtime failure.
_NOOP_EXIT = 2

#: Labels of the on-boot warnings (`field` → wording).
_LABELS = {
    "vcpu": "vCPU",
    "ram_gib": "RAM",
    "disk_gib": "Disk",
    "network": "Network",
    "mac": "MAC",
    "interface": "Interface",
}


# ---------------------------------------------------------------------------
# Request: the parsed options, before they meet libvirt
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Request:
    """Normalized ``modify`` options (a ``None`` field means "not requested")."""

    vcpu: int | None = None
    ram_gib: int | None = None
    disk_gib: int | None = None
    net: Interface | None = None
    mac: str | None = None
    add: tuple[Interface, ...] = ()
    remove: tuple[str, ...] = ()

    def requested(self) -> bool:
        """True when at least one change option was given."""
        return any(
            (
                self.vcpu is not None,
                self.ram_gib is not None,
                self.disk_gib is not None,
                self.net is not None,
                self.mac is not None,
                self.add,
                self.remove,
            )
        )


def parse_net(value: str) -> Interface:
    """Parse ``--net``/``--net-add``: ``network=NAME`` | ``bridge=NAME`` | ``NAME``.

    A bare name is a virtual-network name (the ``default`` network spelled
    short); ``direct``/``user`` NICs are out of scope for virt-runner.

    Raises:
        click.UsageError: malformed spec.
    """
    kind, sep, source = value.partition("=")
    if not sep:
        kind, source = "network", value
    kind, source = kind.strip().lower(), source.strip()
    if kind not in ("network", "bridge"):
        raise click.UsageError(
            f"invalid network '{value}': expected network=NAME or bridge=NAME"
        )
    if not source:
        raise click.UsageError(f"invalid network '{value}': empty network name")
    return Interface(type=kind, source=source, mac="")


def parse_net_add(value: str) -> Interface:
    """Parse ``--net-add``: ``network=NAME[,mac=52:54:00:..]``.

    Raises:
        click.UsageError: malformed spec or a MAC that is not six byte pairs.
    """
    mac = ""
    parts = value.split(",")
    spec, options = parts[0], parts[1:]
    for option in options:
        key, sep, val = option.partition("=")
        key = key.strip().lower()
        if key != "mac" or not sep:
            raise click.UsageError(
                f"invalid option '{option}' in '{value}': only mac= is supported"
            )
        mac = parse_mac(val)
    iface = parse_net(spec)
    return Interface(type=iface.type, source=iface.source, mac=mac)


def parse_mac(value: str) -> str:
    """Validate and normalize a MAC address to lower case.

    Raises:
        click.UsageError: not six colon-separated byte pairs.
    """
    mac = value.strip().lower()
    if not _MAC_RE.fullmatch(mac):
        raise click.UsageError(
            f"invalid MAC '{value}': expected 6 byte pairs like 52:54:00:11:22:33"
        )
    return mac


def resolve_net(v: Virtualizer, iface: Interface) -> Interface:
    """Turn ``--net NAME`` into the interface libvirt will actually see.

    A bare name is checked against the defined virtual networks so that a typo
    is reported here instead of as a failed attach; an explicit ``bridge=`` is
    trusted as written (libvirt validates it). The caller turns the substitution
    into a warning — see :func:`_with_net`.
    """
    if iface.type == "network" and not v.network_defined(iface.source):
        return Interface(type="bridge", source=iface.source, mac=iface.mac)
    return iface


# ---------------------------------------------------------------------------
# Plan: pure diff between the current spec and the request
# ---------------------------------------------------------------------------


@dataclass
class Change:
    """One field that changes, plus the libvirt ops that apply it.

    ``effective`` is decided by the plan (state + ceilings) and may be degraded
    to :data:`~virt_runner.virtualizer.EFFECTIVE_ON_BOOT` while applying, when
    libvirt refuses the live change.
    """

    field: str
    from_: Any
    to: Any
    effective: str
    ops: tuple[tuple[str, Any], ...] = ()
    extra: dict[str, Any] = field(default_factory=dict)
    #: `None` = "nothing was asked to run" (a plan), True = really applied,
    #: False = asked for but never reached. Only False is ever written down, so
    #: a normal success document stays as clean as it always was.
    applied: bool | None = None

    def document(self) -> dict[str, Any]:
        """The ``changes[]`` entry of the ``vm-modify`` document."""
        entry: dict[str, Any] = {
            "field": self.field,
            "from": self.from_,
            "to": self.to,
            "effective": self.effective,
        }
        if self.applied is False:
            entry["applied"] = False
        entry.update(self.extra)
        return entry


def plan_changes(
    spec: VmSpec,
    req: Request,
    *,
    live: bool,
    auto_grow_fs: bool,
    reboot: bool = False,
) -> tuple[list[Change], list[str]]:
    """Diff *spec* against *req* into an ordered list of changes.

    Args:
        spec: The domain as libvirt currently has it.
        req: The parsed options.
        live: Whether ``--live`` was requested (vCPU/RAM only).
        auto_grow_fs: Grow the guest filesystem after a disk change.

    Returns:
        ``(changes, warnings)`` — warnings the plan itself already knows
        (``--no-auto-grow-fs``, a live change that cannot be live).

    Raises:
        VirtError: ``disk-shrink-unsupported`` (a smaller ``--disk``) or
            ``modify-noop`` (every requested value is already set — never
            raised when ``reboot`` was asked for, which has work to do).
        click.UsageError: a MAC that does not exist, or removing the last NIC.
    """
    changes: list[Change] = []
    warnings: list[str] = []
    running = spec.running

    # A sizing request is compared to the *config*, because that is what the
    # change writes. The running guest may be behind it — a config-only change
    # that was never rebooted into, or a `virsh setmem --live` by someone with
    # the client — and then the right answer is "that is already in the config,
    # reboot it", not "nothing to do".
    vcpu_cfg = spec.config_vcpu or spec.vcpu
    # `from` is the value that still disagrees with the request: the config, when
    # that is what is wrong, otherwise what the guest runs.
    vcpu_from = vcpu_cfg if vcpu_cfg != req.vcpu else spec.vcpu
    if req.vcpu is not None and (req.vcpu != spec.vcpu or req.vcpu != vcpu_cfg):
        if req.vcpu == vcpu_cfg:
            changes.append(Change("vcpu", vcpu_from, req.vcpu, EFFECTIVE_ON_BOOT))
        else:
            # A live change is impossible above the domain's current maximum: that
            # ceiling is fixed until the guest reboots.
            can_live = (
                live and running and req.vcpu != spec.vcpu and req.vcpu <= spec.vcpu_max
            )
            changes.append(
                Change(
                    "vcpu",
                    vcpu_from,
                    req.vcpu,
                    EFFECTIVE_NOW if can_live else EFFECTIVE_ON_BOOT,
                    (("vcpu", req.vcpu),),
                )
            )

    ram_cfg_gib = spec.config_ram_gib
    ram_from = ram_cfg_gib if ram_cfg_gib != req.ram_gib else spec.ram_gib
    if req.ram_gib is not None and (
        req.ram_gib != spec.ram_gib or req.ram_gib != ram_cfg_gib
    ):
        if req.ram_gib == ram_cfg_gib:
            changes.append(Change("ram_gib", ram_from, req.ram_gib, EFFECTIVE_ON_BOOT))
        else:
            can_live = (
                live
                and running
                and req.ram_gib != spec.ram_gib
                and req.ram_gib * 1024 * 1024 <= spec.ram_max_kib
            )
            changes.append(
                Change(
                    "ram_gib",
                    ram_from,
                    req.ram_gib,
                    EFFECTIVE_NOW if can_live else EFFECTIVE_ON_BOOT,
                    (("ram", req.ram_gib),),
                )
            )

    if req.disk_gib is not None:
        _plan_disk(spec, req.disk_gib, auto_grow_fs, changes, warnings)

    _plan_interfaces(spec, req, changes)

    if not reboot:
        warnings.extend(_on_boot_warnings(spec, changes))

    # `--reboot` on its own is a legitimate request: the change is already in the
    # config, and rebooting is what makes it real. Without this the command's own
    # "pass at least one option" guard and the noop check contradict each other.
    if not changes and not reboot:
        raise VirtError(
            f"nothing to change: every value given is already set on '{spec.name}'\n"
            f"hint: to apply what is already in the config, run "
            f"'virt-runner modify {spec.name} --reboot --wait'",
            "modify-noop",
        )
    return changes, warnings


def _plan_disk(
    spec: VmSpec,
    target_gib: int,
    auto_grow_fs: bool,
    changes: list[Change],
    warnings: list[str],
) -> None:
    """Plan the disk (and the guest-side filesystem grow) into *changes*.

    The size itself always lands ``now``: either the live block device grows,
    or the volume of a powered-off guest grows and the guest sees it on next
    boot — no reboot is needed to make the storage change real.
    """
    if spec.disk_gib is None:
        raise VirtError(
            f"cannot read the current disk size of '{spec.name}' — refusing to "
            "resize it blindly",
            "runtime-error",
        )
    if target_gib < spec.disk_gib:
        raise VirtError(
            f"--disk {target_gib} would shrink '{spec.name}' from "
            f"{spec.disk_gib} GiB: virt-runner never shrinks a disk",
            "disk-shrink-unsupported",
        )

    ops: list[tuple[str, Any]] = []
    if target_gib != spec.disk_gib:
        ops.append(("disk", target_gib))
    if auto_grow_fs:
        # Idempotent and always safe to re-run: re-issuing the same `--disk`
        # on a booted VM is how an off-domain grow gets its filesystem grown.
        ops.append(("fs-grow", None))
    elif target_gib != spec.disk_gib:
        warnings.append(
            f"the guest filesystem was not resized (--no-auto-grow-fs): in the "
            f"guest run e.g. sudo growpart /dev/{spec.disk_target} <n> and "
            "resize2fs/xfs_growfs"
        )
    if not ops:
        # Same size and no grow: the caller turns this into `modify-noop`.
        return
    changes.append(
        Change(
            "disk_gib",
            spec.disk_gib,
            target_gib,
            EFFECTIVE_NOW,
            tuple(ops),
            {"fs_grown": None},
        )
    )


def _plan_interfaces(spec: VmSpec, req: Request, changes: list[Change]) -> None:
    """Plan NIC removals, the primary NIC's re-attach and the additions.

    Applied in that order, so ``--net``/``--mac`` act on the NIC that is
    primary after the ``--net-remove`` entries — which is what makes
    "remove the odd one, then rename what is left" expressible in one call.
    """
    effective = EFFECTIVE_NOW if spec.running else EFFECTIVE_ON_BOOT
    remaining = list(spec.interfaces)

    for mac in req.remove:
        target = _find_mac(remaining, mac, spec.name)
        if len(remaining) == 1 and not req.add:
            raise click.UsageError(
                f"refusing to remove the last interface of '{spec.name}': the VM "
                "would be unreachable (add the replacement with --net-add first)"
            )
        remaining.remove(target)
        changes.append(
            Change(
                "interface",
                target.document(),
                None,
                effective,
                (("detach", target),),
            )
        )

    if req.net is not None or req.mac is not None:
        primary = remaining[0] if remaining else None
        if primary is None:
            raise click.UsageError(
                f"'{spec.name}' has no interface left to change; add one with --net-add"
            )
        wanted_type = req.net.type if req.net is not None else primary.type
        wanted_source = req.net.source if req.net is not None else primary.source
        wanted_mac = req.mac if req.mac is not None else primary.mac
        replacement = Interface(
            type=wanted_type,
            source=wanted_source,
            mac=wanted_mac or primary.mac,
            model=primary.model,
        )
        network_changed = (wanted_type, wanted_source) != (primary.type, primary.source)
        mac_changed = wanted_mac != primary.mac
        # One detach + one attach covers `--net` and `--mac` together, so the op
        # rides on the first entry that reports the rewiring.
        ops = (
            (("nic-replace", (primary, replacement)),)
            if network_changed or mac_changed
            else ()
        )
        if network_changed:
            changes.append(
                Change("network", primary.source, wanted_source, effective, ops)
            )
            if mac_changed:
                changes.append(Change("mac", primary.mac, wanted_mac, effective))
        elif mac_changed:
            changes.append(Change("mac", primary.mac, wanted_mac, effective, ops))
        remaining[0] = replacement

    for requested in req.add:
        # libvirt would pick the MAC at attach time, which leaves both the plan
        # and the result document carrying `mac: ""` — an empty field a caller
        # then has to go hunting for in `virsh domiflist`. Claiming one here puts
        # the same address in the plan, the apply and the document.
        iface = replace(requested, mac=requested.mac or Virtualizer.generate_mac())
        if any(i.mac == iface.mac for i in remaining):
            raise click.UsageError(
                f"interface with MAC '{iface.mac}' already exists on '{spec.name}'"
            )
        remaining.append(iface)
        changes.append(
            Change(
                "interface",
                None,
                iface.document(),
                effective,
                (("attach", iface),),
            )
        )


def _find_mac(ifaces: list[Interface], mac: str, name: str) -> Interface:
    """The NIC with *mac*, or a usage error naming what exists."""
    for iface in ifaces:
        if iface.mac == mac:
            return iface
    known = ", ".join(i.mac or "(no mac)" for i in ifaces) or "none"
    raise click.UsageError(
        f"'{name}' has no interface with MAC '{mac}' (it has: {known})"
    )


# ---------------------------------------------------------------------------
# Apply
# ---------------------------------------------------------------------------


@dataclass
class ApplyContext:
    """Everything the applier needs besides the plan."""

    v: Virtualizer
    spec: VmSpec
    volume: str | None
    live: bool
    user: str
    identity: str | None
    ip: str | None
    rebooting: bool = False
    warnings: list[str] = field(default_factory=list)


def apply_changes(ctx: ApplyContext, changes: list[Change]) -> None:
    """Run every op of *changes*, degrading live changes to the config.

    A live refusal is a warning and an ``on-boot`` entry; a failure of the
    persisted change is fatal, as is a failed NIC change (a half-rewired guest
    must never look like success).

    A failed guest-side filesystem grow is not fatal: the block device stays
    grown, ``fs_grown`` is reported as ``false`` and the warning carries the
    commands to finish the job inside the guest.

    Raises:
        RuntimeError: a sizing or disk change could not be persisted.
        VirtError: ``net-change-failed``.
    """
    try:
        for change in changes:
            for kind, payload in change.ops:
                if kind == "vcpu":
                    _apply_vcpu(ctx, change, payload)
                elif kind == "ram":
                    _apply_ram(ctx, change, payload)
                elif kind == "disk":
                    ctx.v.grow_disk(
                        ctx.spec.name, ctx.spec.disk_target, ctx.volume, payload
                    )
                elif kind == "fs-grow":
                    _apply_fs_grow(ctx, change)
                elif kind == "detach":
                    _run_live_then_config(ctx, change, ctx.v.detach_interface, payload)
                elif kind == "attach":
                    _run_live_then_config(ctx, change, ctx.v.attach_interface, payload)
                elif kind == "nic-replace":
                    old, new = payload
                    _replace_nic(ctx, change, old, new)
            change.applied = True
            _warn_on_boot(ctx, change)
    except Exception:
        # Anything that did not finish — the op that raised included — is a
        # request, not a change. Marking it keeps the error document honest
        # about what really happened to the VM.
        for change in changes:
            if not change.applied:
                change.applied = False
        raise


def _apply_vcpu(ctx: ApplyContext, change: Change, vcpu: int) -> None:
    """Persist the vCPU count, hot-plugging first when ``--live`` was given."""
    live_ok, why = ctx.v.set_vcpu(
        ctx.spec.name, vcpu, live=ctx.live, ceiling=ctx.spec.vcpu_max
    )
    change.effective = EFFECTIVE_NOW if live_ok else EFFECTIVE_ON_BOOT
    if why:
        change.extra["live_refused"] = why


def _apply_ram(ctx: ApplyContext, change: Change, ram_gib: int) -> None:
    """Persist the RAM size, ballooning first when ``--live`` was given."""
    live_ok, why = ctx.v.set_ram_gib(
        ctx.spec.name,
        ram_gib,
        live=ctx.live,
        ceiling=ctx.spec.ram_max_kib,
        config_ceiling=ctx.spec.config_ram_kib,
    )
    change.effective = EFFECTIVE_NOW if live_ok else EFFECTIVE_ON_BOOT
    if why:
        change.extra["live_refused"] = why


def _replace_nic(
    ctx: ApplyContext, change: Change, old: Interface, new: Interface
) -> None:
    """Move a NIC, but only if the replacement target is actually usable.

    The order is the whole point: libvirt only notices a missing network or
    bridge when it attaches, which is **after** the detach succeeded — leaving a
    running guest with no interface at all while the command reports success.
    Checking the target first turns that into a refusal that changes nothing.

    Raises:
        VirtError: ``net-change-failed`` when the target does not exist.
    """
    if not ctx.v.interface_target_exists(new):
        raise VirtError(
            f"cannot move '{old.mac}' to {new.type}={new.source} of "
            f"'{ctx.spec.name}': no such virtual network or bridge on this host "
            "(see 'virsh net-list --all' / 'ip link') — nothing was changed",
            "net-change-failed",
        )
    _run_live_then_config(ctx, change, ctx.v.detach_interface, old)
    _run_live_then_config(ctx, change, ctx.v.attach_interface, new)


def _run_live_then_config(
    ctx: ApplyContext,
    change: Change,
    call: Any,
    iface: Interface,
) -> None:
    """Apply an interface change live when the guest runs, else to its config.

    A live refusal (an hot-unplug the guest ignores, an older libvirt) degrades
    to the persisted config with a warning; the card's own contract: a failed
    live change means ``effective: "on-boot"`` plus a warning.

    Raises:
        VirtError: ``net-change-failed`` when even the persisted change fails.
    """
    if not ctx.spec.running:
        call(ctx.spec.name, iface, live=False)
        change.effective = EFFECTIVE_ON_BOOT
        return
    try:
        call(ctx.spec.name, iface, live=True)
        change.effective = EFFECTIVE_NOW
        return
    except RuntimeError as exc:
        ctx.warnings.append(f"live NIC change refused ({exc}); applying on next boot")
    call(ctx.spec.name, iface, live=False)
    change.effective = EFFECTIVE_ON_BOOT


def _apply_fs_grow(ctx: ApplyContext, change: Change) -> None:
    """Grow the guest's own filesystem — best effort, never fatal.

    ``fs_grown`` in the document says whether it happened; anything that stops
    us (powered-off guest, no lease, no ``growpart``, unsupported layout) is a
    warning with the commands to run by hand.
    """
    name = ctx.spec.name
    if not ctx.spec.running:
        ctx.warnings.append(
            f"{name} is {ctx.spec.state}: the guest filesystem was not grown — "
            f"boot it, then run: virt-runner modify {name} --disk {change.to}"
        )
        return
    if not ctx.ip:
        ctx.warnings.append(
            "no IP for the guest, so its filesystem was not grown — grow it "
            f"inside the guest (sudo growpart /dev/{ctx.spec.disk_target} <n>, "
            "then resize2fs/xfs_growfs)"
        )
        return
    code, out = ctx.v.grow_guest_filesystem(
        ctx.user, ctx.ip, ctx.spec.disk_target, identity=ctx.identity
    )
    change.extra["fs_grown"] = code == 0
    if code != 0:
        ctx.warnings.append(
            f"guest filesystem was not grown (exit {code}): {out or 'no output'}"
        )


def _warn_on_boot(ctx: ApplyContext, change: Change) -> None:
    """One warning per change a running guest only picks up on reboot."""
    if ctx.rebooting:
        return
    if ctx.spec.running and change.effective == EFFECTIVE_ON_BOOT:
        message = on_boot_warning(change)
        if message not in ctx.warnings:
            ctx.warnings.append(message)


def on_boot_warning(change: Change) -> str:
    """The hint that a change the guest only applies at boot carries.

    When the running domain *was* asked and libvirt said no, its own words go in
    too — "applies on next boot" alone leaves the reader guessing whether the
    tool gave up early or libvirt has a real limitation.
    """
    label = _LABELS.get(change.field, change.field)
    refused = change.extra.get("live_refused")
    if refused:
        return f"{label} change applies on next boot ({refused}) — consider --reboot"
    return f"{label} change applies on next boot — consider --reboot"


def _on_boot_warnings(spec: VmSpec, changes: list[Change]) -> list[str]:
    """The hints a *planned* change carries (mirrors :func:`_warn_on_boot`)."""
    if not spec.running:
        return []
    out: list[str] = []
    for change in changes:
        if change.effective == EFFECTIVE_ON_BOOT:
            message = on_boot_warning(change)
            if message not in out:
                out.append(message)
    return out


# ---------------------------------------------------------------------------
# Command
# ---------------------------------------------------------------------------


def _net_callback(ctx: Any, param: Any, value: str | None) -> Interface | None:
    """Click callback: validate ``--net`` early, before libvirt is touched."""
    return parse_net(value) if value else None


def _mac_callback(ctx: Any, param: Any, value: str | None) -> str | None:
    """Click callback: validate ``--mac`` early, before libvirt is touched."""
    return parse_mac(value) if value else None


@command(name="modify")
@click.argument("name")
@click.option(
    "--vcpu",
    default=None,
    type=click.IntRange(min=1),
    help="New vCPU count (positive integer)",
)
@click.option(
    "--ram",
    "ram_gib",
    default=None,
    type=click.IntRange(min=1),
    help="New RAM size in GiB",
)
@click.option(
    "--disk",
    "disk_gib",
    default=None,
    type=click.IntRange(min=1),
    help="New disk size in GiB (grow only — shrinking is refused)",
)
@click.option(
    "--net",
    "net",
    default=None,
    metavar="NETWORK|bridge=NAME",
    callback=_net_callback,
    help="Re-attach the primary NIC to this network or bridge (model=virtio)",
)
@click.option(
    "--net-add",
    "net_add",
    multiple=True,
    metavar="NETWORKSPEC",
    help="Attach an extra NIC: network=NAME | bridge=NAME [,mac=52:54:00:..]; "
    "repeatable",
)
@click.option(
    "--net-remove",
    "net_remove",
    multiple=True,
    metavar="MAC",
    help="Detach the NIC with this MAC; repeatable (the last NIC is refused)",
)
@click.option(
    "--mac",
    "mac",
    default=None,
    callback=_mac_callback,
    help="New MAC address of the primary NIC",
)
@click.option(
    "--reboot",
    is_flag=True,
    default=False,
    help="Reboot the VM after the changes so on-boot changes land "
    "(shuts it down first when it was off; works on its own)",
)
@click.option(
    "--wait",
    is_flag=True,
    default=False,
    help="With --reboot: wait for the guest to answer SSH again and report its IP",
)
@click.option(
    "--live",
    is_flag=True,
    default=False,
    help="Apply vCPU/RAM to the running domain where libvirt allows it "
    "(setvcpus/setmem --live); NICs are always changed live when the VM runs",
)
@click.option(
    "--no-auto-grow-fs",
    "auto_grow_fs",
    is_flag=True,
    # The negative-flag pattern: passing the flag yields False, omitting it
    # keeps the default. Without ``flag_value`` click hands back the default
    # and the flag does nothing.
    flag_value=False,
    default=True,
    help="Skip the guest-side filesystem grow after --disk (the guest tools "
    "may do it, or you may prefer to do it yourself)",
)
@click.option(
    "--plan",
    "planned",
    is_flag=True,
    default=False,
    help="Report what would change and apply nothing (same document)",
)
def cmd_modify(
    name: str,
    vcpu: int | None,
    ram_gib: int | None,
    disk_gib: int | None,
    net: Interface | None,
    net_add: tuple[str, ...],
    net_remove: tuple[str, ...],
    mac: str | None,
    reboot: bool,
    wait: bool,
    live: bool,
    auto_grow_fs: bool,
    planned: bool,
    as_json: bool,
) -> None:
    """Change vCPU, RAM, disk (grow) and networking of an existing VM."""
    output.set_json_mode(as_json)
    v = Virtualizer()

    if not any(
        (
            vcpu is not None,
            ram_gib is not None,
            disk_gib is not None,
            net is not None,
            mac is not None,
            net_add,
            net_remove,
            reboot,
        )
    ):
        raise click.UsageError(
            "no change requested: pass at least one of --vcpu, --ram, --disk, "
            "--net, --net-add, --net-remove, --mac, --reboot"
        )
    if wait and not reboot:
        raise click.UsageError("--wait needs --reboot (it waits for the reboot)")

    req = Request(
        vcpu=vcpu,
        ram_gib=ram_gib,
        disk_gib=disk_gib,
        net=net,
        mac=mac,
        add=tuple(parse_net_add(spec) for spec in net_add),
        remove=tuple(parse_mac(m) for m in net_remove),
    )

    try:
        v.require_libvirt()
        v.require_pool_defined()
        if not v.domain_exists(name):
            # Same condition, same code as `destroy`/`ssh`: a script that cleans
            # up idempotently must not have to know two spellings for "gone".
            output.fail(
                "modify",
                f"VM '{name}' is not defined",
                code="vm-not-defined",
                text=f"VM '{name}' is not defined",
                fields={"name": name},
            )
        spec = v.domain_spec(name)
        if spec is None or not v.owns_domain(name):
            output.fail(
                "modify",
                f"VM '{name}' was not created by virt-runner (no disk in pool "
                f"'{v.POOL}' and no virt-runner metadata) — refusing to change it",
                code="not-a-virt-runner-vm",
                fields={"name": name},
            )
    except RuntimeError as exc:
        output.fail_with("modify", exc, "libvirt-unreachable", fields={"name": name})

    net_note = ""
    try:
        if req.net is not None:
            req, net_note = _with_net(v, req)
        changes, warnings = plan_changes(
            spec, req, live=live, auto_grow_fs=auto_grow_fs, reboot=reboot
        )
    except VirtError as exc:
        output.fail_with(
            "modify",
            exc,
            "runtime-error",
            exit_code=_NOOP_EXIT if exc.code == "modify-noop" else 1,
            fields={"vm": spec.document(), "planned": planned},
        )
    if net_note:
        # A substitution the user did not ask for belongs in the document, not
        # only in stderr — a caller that reads `--json` would miss it entirely.
        warnings.insert(0, net_note)

    # The guest's current address: useful in a plan, and what the filesystem
    # grow (and ``--wait``) needs. After a reboot it is refreshed or re-waited.
    ip = v.vm_ip(name, lease_file("modify")) if spec.running else None

    if planned:
        _report(spec, changes, warnings, planned=True, as_json=as_json, ip=ip)
        return

    meta = v.domain_meta(name)
    ctx = ApplyContext(
        v=v,
        spec=spec,
        volume=v.disk_volume(name),
        live=live,
        rebooting=reboot,
        user=meta.get("user") or Virtualizer.DEFAULT_USER,
        identity=meta.get("identity")
        or Virtualizer.private_key_path(Virtualizer.DEFAULT_SSH_KEY),
        ip=ip,
        warnings=warnings,
    )

    try:
        apply_changes(ctx, changes)
    except RuntimeError as exc:
        # Whatever was applied before the failure is real, and what was not did
        # not happen — say both, or the document reads like a no-op run.
        skipped = [c.field for c in changes if c.applied is False]
        note = f"not applied: {', '.join(skipped)} (the run stopped here)"
        if skipped:
            ctx.warnings.append(note)
        output.fail_with(
            "modify",
            exc,
            "runtime-error",
            text=f"vm-modify: {exc}; {note}" if skipped else None,
            fields={
                "vm": spec.document(),
                "changes": [c.document() for c in changes],
                "warnings": ctx.warnings,
            },
        )

    # A live MAC change moved the address: the lease read before the change is
    # already pointing at the guest's former self.
    if not reboot and any(
        c.field == "mac" and c.effective == EFFECTIVE_NOW for c in changes
    ):
        try:
            ip = v.wait_for_ip(
                name, _primary_mac(changes, spec), lease_file("modify"), 30
            )
        except RuntimeError:
            ctx.warnings.append(
                "no lease yet for the new MAC — run 'virt-runner list' for the guest's address"
            )

    rebooted = False
    if reboot:
        try:
            v.reboot(name)
            rebooted = True
            # The guest went through a boot, so everything written to its
            # config is in effect by the time this document is printed.
            for change in changes:
                if change.effective == EFFECTIVE_ON_BOOT:
                    change.effective = EFFECTIVE_NOW
            ctx.warnings[:] = [
                w for w in ctx.warnings if "applies on next boot" not in w
            ]
            if wait:
                ip = v.wait_for_ip(
                    name,
                    _primary_mac(changes, spec),
                    lease_file("modify"),
                )
                v.verify_ssh_reachable(name, ctx.user, ip, identity=ctx.identity)
            else:
                # A new boot can mean a new lease, and a stale address in the
                # document would send the user to the wrong host.
                ip = v.vm_ip(name, lease_file("modify"))
        except RuntimeError as exc:
            output.fail_with(
                "modify",
                exc,
                "vm-reboot-failed",
                fields={
                    "vm": spec.document(),
                    "changes": [c.document() for c in changes],
                    "warnings": ctx.warnings,
                },
            )

    _report(
        spec,
        changes,
        ctx.warnings,
        planned=False,
        as_json=as_json,
        rebooted=rebooted,
        ip=ip,
    )


def _with_net(v: Virtualizer, req: Request) -> tuple[Request, str]:
    """Resolve ``--net NAME`` to network/bridge before planning (needs libvirt).

    Returns the rewritten request plus a warning when a name libvirt does not
    know is being attached as a bridge of that name.
    """
    assert req.net is not None
    wanted = req.net
    resolved = resolve_net(v, wanted)
    note = ""
    if resolved.type != wanted.type:
        note = (
            f"'{wanted.source}' is not a defined virtual network, so the NIC is "
            f"attached as a bridge of that name — it must exist on the host "
            "('ip link')"
        )
    return (
        Request(
            vcpu=req.vcpu,
            ram_gib=req.ram_gib,
            disk_gib=req.disk_gib,
            net=resolved,
            mac=req.mac,
            add=req.add,
            remove=req.remove,
        ),
        note,
    )


def _primary_mac(changes: list[Change], spec: VmSpec) -> str:
    """MAC to look the guest up by after a reboot (the new primary NIC's)."""
    mac = spec.primary.mac if spec.primary else ""
    for change in changes:
        if change.field == "mac":
            mac = str(change.to)
        for kind, payload in change.ops:
            if kind == "nic-replace" and payload[1].mac:
                mac = payload[1].mac
    return mac


def _report(
    spec: VmSpec,
    changes: list[Change],
    warnings: list[str],
    *,
    planned: bool,
    as_json: bool,
    rebooted: bool = False,
    ip: str | None = None,
) -> None:
    """Emit the ``vm-modify`` document, or the same facts as text."""
    if as_json:
        output.emit(
            "modify",
            {
                "vm": spec.document(),
                "changes": [c.document() for c in changes],
                "rebooted": rebooted,
                "planned": planned,
                "ip": ip,
                "warnings": warnings,
            },
        )
        return

    print(f"{'Planned' if planned else 'Modified'}: {spec.name} ({spec.state})")
    for change in changes:
        print(
            f"  {change.field}: {_value(change.from_)} -> {_value(change.to)} "
            f"({change.effective})"
        )
    for warning in warnings:
        print(f"  warning: {warning}")


def _value(value: Any) -> str:
    """Render a change value (an interface prints as ``network=default/..``)."""
    if value is None:
        return "-"
    if isinstance(value, dict):
        mac = value.get("mac") or ""
        return f"{value.get('type')}={value.get('source')}" + (f"/{mac}" if mac else "")
    return str(value)
