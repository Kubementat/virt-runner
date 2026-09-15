# Bug 002 — SSH verify timed out on a working VM: missing identity key + host-key reuse

| | |
|---|---|
| **Severity** | High (`create` reports `ssh-timeout` failure on a perfectly reachable VM) |
| **Component** | `virt-runner create`, stage `ssh-verify` (`Virtualizer.verify_ssh_reachable`) |
| **Discovered** | 2026-09-15 |
| **Fixed** | 2026-09-15 |
| **Status** | Fixed and verified end-to-end (repeated create/list/ssh cycles) |

---

## 1. Symptom

`create` booted the VM, acquired the DHCP lease, then exited 1:

```
"error": { "code": "ssh-timeout",
           "message": "SSH to ubuntu@192.168.122.103 not reachable yet — check: virsh console it-vm-a",
           "stage": "ssh-verify" }
```

Yet the very same `ssh` from the same shell, seconds later, succeeded
instantly. Two independent causes were hiding behind one symptom:

1. **No identity passed.** The probe ran bare `ssh -o BatchMode=yes user@ip exit`.
   The injected key is `~/.ssh/virt_runner_key` — *not* one of ssh's default
   identities — so authentication could never succeed in batch mode.
2. **Host key on a reused IP.** With `StrictHostKeyChecking=accept-new`, an
   already-recorded key for that IP is *required to match*. DHCP hands the same
   addresses to throwaway guests, so after destroy + recreate the new guest
   presents a new host key and `accept-new` hard-fails — and the probe polluted
   `~/.ssh/known_hosts` on the way.

## 2. Root cause

The verify stage probed SSH the way one probes a *permanent* host, but the
guests are ephemeral: identity is only the injected key, and IP+host-key pairs
are recycled.

## 3. Fix

`verify_ssh_reachable` now probes with:

```
ssh -i <injected-private-key> -o IdentitiesOnly=yes
    -o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null …
```

`/dev/null` as known_hosts is correct here (not "insecure"): nothing is being
trusted long-term; a reused IP must simply not break verification, and the
user's `known_hosts` stays clean. The private key path is derived from
`--ssh-key` (strip `.pub`). The reported `ssh_command` in create/list output
now carries `-i <key>` too, so the printed line is copy-paste ready.

## 4. Lessons

- A reachability probe must use **exactly the credentials the real user
  would have** — here that meant the injected key, not the agent/defaults.
- For ephemeral, DHCP-reassigned addresses, host-key persistence is
  meaningless; isolate it (`UserKnownHostsFile=/dev/null`) from the start.
- "Works when I try it manually" + "fails in the tool" ⇒ diff the exact
  commands (flags, identity, options), don't assume timing/slow boot.
