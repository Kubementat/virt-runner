#!/usr/bin/env bash
# Manual verification harness for `virt-runner modify` networking.
#
#   ./scripts/network-modify-test.sh                   # safe matrix (~3 min)
#   RUN_DHCP_RISK=1 ./scripts/network-modify-test.sh   # + proves the 2-NIC hazard
#
# Creates only VMs named agent-nettest-*, and only touches the libvirt network
# it defines itself. Prints PASS/FAIL per check; exits non-zero if any failed.
set -uo pipefail

VR=${VR:-virt-runner}
VM=${VM:-agent-nettest-$$}
ISO=${ISO:-agent-iso}
ISO_BR=${ISO_BR:-virbr-iso}
ISO_SUB=${ISO_SUB:-192.168.130}
NET=${NET:-default}
SSHO=(-i "$HOME/.ssh/virt_runner_key" -o IdentitiesOnly=yes -o BatchMode=yes
      -o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null -o LogLevel=ERROR)
PASS=0 FAIL=0 MADE_VM=0 MADE_ISO=0

# ------------------------------------------------------------------ helpers
ok()  { printf '  \033[32mPASS\033[0m %s\n     %s\n' "$1" "${2:-}"; PASS=$((PASS+1)); }
bad() { printf '  \033[31mFAIL\033[0m %s\n     %s\n' "$1" "${2:-}"; FAIL=$((FAIL+1)); }
say() { printf '\n\033[1m── %s\033[0m\n' "$1"; }
note(){ printf '     \033[33mnote:\033[0m %s\n' "$1"; }

xrc()  { local d=$1 w=$2 g=$3; [ "$w" = "$g" ] && ok "$d" "exit=$g" || bad "$d" "want exit=$w got=$g"; }
eq()   { local d=$1 w=$2 g=$3; [ "$w" = "$g" ] && ok "$d" "[$g]"    || bad "$d" "want [$w] got [$g]"; }
code() { jq -r '.error.code // empty' <<<"$1"; }

# host-side view of the NICs (the only place the assigned MAC appears)
nic_macs()  { virsh domiflist "$1" 2>/dev/null | sed '1,/^---/d' | awk 'NF>=5 {print $NF}'; }
nic_count() { nic_macs "$1" | sed '/^$/d' | wc -l; }
# the mac that appeared (+) or disappeared (-) across a change
diff_macs() { local a=$1 b=$2; comm -13 <(printf '%s\n' "$a" | sort) <(printf '%s\n' "$b" | sort) | head -1; }

gssh()     { ssh "${SSHO[@]}" "ubuntu@$1" "${@:2}"; }
g_nics()   { gssh "$1" 'ls -d /sys/class/net/* | grep -cv lo'; }        # non-loopback
g_v4()     { gssh "$1" "ip -4 -o addr show | grep -cv ' lo '"; }        # ifaces with IPv4
vm_ip()    { "$VR" list --json 2>/dev/null | jq -r ".vms[] | select(.name==\"$1\" and .ip) | .ip"; }

cleanup() {
  say "cleanup"
  [ "$MADE_VM" = 1 ] && "$VR" destroy "$VM" --json >/dev/null 2>&1
  [ "$MADE_ISO" = 1 ] && { virsh net-destroy "$ISO" >/dev/null 2>&1; virsh net-undefine "$ISO" >/dev/null 2>&1; }
  printf 'passed=%d failed=%d\n' "$PASS" "$FAIL"
  [ "$FAIL" = 0 ]
}
trap cleanup EXIT

# ---------------------------------------------------------------- 0 preflight
say "0. preflight"
command -v "$VR" >/dev/null || { bad "virt-runner on PATH" "install it or set VR="; exit 1; }
virsh net-list --all 2>/dev/null | grep -q "$NET" || { bad "network '$NET' active" "virsh net-start $NET"; exit 1; }
ok "host has libvirt + jq + ssh"
ok "network '$NET' is active"

if virsh net-dumpxml "$ISO" >/dev/null 2>&1; then
  ok "isolated network '$ISO' already defined, reusing it"
else
  x=$(mktemp); printf "<network><name>%s</name><bridge name='%s'/><forward mode='none'/>\
<ip address='%s.1' netmask='255.255.255.0'/></network>\n" "$ISO" "$ISO_BR" "$ISO_SUB" >"$x"
  virsh net-define "$x" >/dev/null && virsh net-start "$ISO" >/dev/null && MADE_ISO=1; rm -f "$x"
  [ "$MADE_ISO" = 1 ] && ok "defined + started '$ISO' (no DHCP, no NAT)" || { bad "could not define '$ISO'"; exit 1; }
fi

# --------------------------------------------------------------------- 1 base
say "1. create a throwaway VM as the subject"
out=$("$VR" create "$VM" --ram 2 --vcpu 1 --disk 10 --json 2>/dev/null); rc=$?
MADE_VM=1
xrc "create $VM" 0 "$rc"
IP=$(jq -r '.vm.ip // empty' <<<"$out"); [ -n "$IP" ] && ok "vm.ip reported" "$IP" || { bad "vm.ip" "empty"; exit 1; }
BASE=$(nic_macs "$VM" | head -1)
eq "one NIC on the host" 1 "$(nic_count "$VM")"
[ -n "$BASE" ] && ok "baseline MAC" "$BASE" || { bad "baseline MAC" "none"; exit 1; }
gssh "$IP" true && ok "ssh round-trip works" || bad "ssh round-trip" "unreachable"

# ---------------------------------------------------------------- 2 guards
say "2. guards must refuse AND leave the VM alone"
o=$("$VR" modify "$VM" --net-remove "$BASE" --json 2>/dev/null); xrc "remove the last NIC" 2 "$?"
eq "  error code" usage "$(code "$o")"; eq "  still one NIC" 1 "$(nic_count "$VM")"

o=$("$VR" modify "$VM" --net-remove 52:54:00:de:ad:be --json 2>/dev/null); xrc "remove unknown MAC" 2 "$?"
eq "  error code" usage "$(code "$o")"; eq "  still one NIC" 1 "$(nic_count "$VM")"

o=$("$VR" modify "$VM" --mac not-a-mac --json 2>/dev/null); xrc "malformed MAC" 2 "$?"
eq "  error code" usage "$(code "$o")"

o=$("$VR" modify "$VM" --net no-such-network-xyz --json 2>/dev/null); xrc "nonexistent target" 1 "$?"
eq "  error code" net-change-failed "$(code "$o")"; eq "  guest kept its NIC" 1 "$(nic_count "$VM")"
gssh "$IP" true && ok "  guest still reachable after the refusal" || bad "  reachability" "lost it"

# ---------------------------------------------------------------- 3 plan
say "3. --plan is read-only and predicts the same document"
o=$("$VR" modify "$VM" --net-add "$ISO" --plan --json 2>/dev/null); xrc "plan" 0 "$?"
eq "  planned flag" true "$(jq -r .planned <<<"$o")"
eq "  effective" now "$(jq -r '.changes[0].effective' <<<"$o")"
eq "  libvirt untouched" 1 "$(nic_count "$VM")"
eq "  MAC not known yet (rough edge: must use virsh domiflist)" '""' "$(jq -c '.changes[0].to.mac' <<<"$o")"

# ---------------------------------------------------------------- 4 add
say "4. attach a NIC to the isolated network"
before=$(nic_macs "$VM")
o=$("$VR" modify "$VM" --net-add "$ISO" --json 2>/dev/null); xrc "apply --net-add" 0 "$?"
eq "  no error" none "$(jq -r '.error.code // "none"' <<<"$o")"
NEW=$(diff_macs "$before" "$(nic_macs "$VM")")
[ -n "$NEW" ] && ok "MAC discovered via domiflist diff" "$NEW" || bad "MAC" "none appeared"
eq "two NICs on the host" 2 "$(nic_count "$VM")"
eq "two NICs inside the guest" 2 "$(g_nics "$IP")"
gssh "$IP" "ip -br link" | grep -q "$NEW" && ok "new MAC visible in guest" || bad "guest sees NIC" "missing"
eq "new NIC gets no IPv4 (isolated: no DHCP)" 1 "$(g_v4 "$IP")"
eq "old lease untouched" "$IP" "$(vm_ip "$VM")"
gssh "$IP" true && ok "ssh survived the hot-plug" || bad "ssh survived" "lost"

# ---------------------------------------------------------------- 5 remove
say "5. detach it again"
before=$(nic_macs "$VM")
o=$("$VR" modify "$VM" --net-remove "$NEW" --json 2>/dev/null); xrc "apply --net-remove" 0 "$?"
eq "  that MAC is no longer attached" 0 "$(nic_macs "$VM" | grep -c "$NEW" || true)"
eq "  back to one NIC (host)" 1 "$(nic_count "$VM")"
eq "  back to one NIC (guest)" 1 "$(g_nics "$IP")"
nic_macs "$VM" | grep -qx "$BASE" && ok "primary NIC intact" || bad "primary NIC" "gone"

# ---------------------------------------------------------------- 6 --net
say "6. move the primary NIC — plan only"
o=$("$VR" modify "$VM" --net "$ISO" --plan --json 2>/dev/null); xrc "plan --net" 0 "$?"
eq "  describes the move" "$NET -> $ISO" "$(jq -r '.changes[0].from + " -> " + .changes[0].to' <<<"$o")"
note "applying this would isolate the guest (no DHCP there) — not tested."

# ---------------------------------------------------------------- 7 --mac
say "7. --mac changes identity ⇒ new lease ⇒ maybe a new IP"
NEW2=52:54:00:11:22:33
o=$("$VR" modify "$VM" --mac "$NEW2" --reboot --wait --json 2>/dev/null); xrc "apply --mac --reboot --wait" 0 "$?"
eq "  libvirt has the new MAC" "$NEW2" "$(nic_macs "$VM" | head -1)"
sleep 5
NIP=$(vm_ip "$VM"); [ -n "$NIP" ] && ok "lease read back after the change" "$NIP" || bad "lease" "no ip"
[ "$NIP" != "$IP" ] && note "address changed: $IP -> $NIP. Anything caching the old one is stale."
gssh "${NIP:-$IP}" "ip -br link" | grep -qi "$NEW2" && ok "guest runs the new MAC" || bad "guest MAC" "mismatch"
"$VR" modify "$VM" --mac "$BASE" --reboot --wait --json >/dev/null 2>&1 && ok "reverted to $BASE" || bad "revert" "failed"

# ---------------------------------------------------------------- 8 hazard
if [ "${RUN_DHCP_RISK:-0}" = 1 ]; then
  say "8. OPT-IN — second NIC on the SAME network (the documented hazard)"
  before=$(nic_macs "$VM")
  "$VR" modify "$VM" --net-add "$NET" --json >/dev/null 2>&1
  eq "  two NICs now share one network" $(( $(printf '%s\n' "$before" | wc -l) + 1 )) "$(nic_count "$VM")"
  if gssh "$IP" true; then ok "ssh survived — you got lucky, guest preferred the old one"
  else bad "ssh DIED — this is the hazard" "recovery: virsh console $VM (detach with Ctrl-])"
  fi
  GONE=$(diff_macs "$before" "$(nic_macs "$VM")")
  [ -n "$GONE" ] && "$VR" modify "$VM" --net-remove "$GONE" --json >/dev/null 2>&1
  eq "  cleaned back to one NIC" 1 "$(nic_count "$VM")"
fi

say "result"
printf '  passed=%d failed=%d\n' "$PASS" "$FAIL"
[ "$FAIL" = 0 ] && printf '  networking modify behaved as documented.\n'
