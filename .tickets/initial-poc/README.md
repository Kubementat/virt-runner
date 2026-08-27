# Implementation Tickets — `vm-create` / `vm-destroy` POC

Tickets derived from `docs/plans/poc-implementation-plan.md` (slices S1–S7) and
`specification/specification.md` (v1.0, authoritative). One ticket per slice,
in strict execution order.

## Ticket format

Each `ticket-NN-<short-slug>.md` is **fully self-contained** — a worker agent
can implement it having read nothing else. Every ticket contains:

- **Title** + slice ID + one-paragraph goal.
- **Context** — the spec/plan sections that apply (cited by section name), the
  environment facts the worker needs, and any ticket-specific recorded
  decisions.
- **Deliverables** — exact files to create/modify.
- **Implementation requirements** — numbered, specific; key commands/flags
  (virt-install invocation, MAC scheme, lease parsing, cache layout, error
  messages, output blocks) given verbatim where precision matters.
- **Safety rules** — `poc-` name prefix for all test VMs; every VM/disk a
  ticket creates is torn down before it finishes; pre-existing objects
  (`setup-test-vm`, the `noble-server-cloudimg-amd64.img` volume) are never
  touched; no `sudo`.
- **Acceptance criteria** — checkable bullets, each with the exact shell
  command and expected outcome.
- **Verification** — the exact ordered command sequence, what success looks
  like, and an explicit statement of whether real VM creation is required
  (with the exact `vm-create` invocation when it is).
- **Definition of done** — all acceptance criteria pass, verification ran,
  working tree committed with the exact message in the table below.
- **Pitfalls** — the spec §6 / plan §6 pitfalls relevant to *this* ticket.

## Execution order

Strictly sequential: each ticket is independently implementable and
verifiable given the previous tickets' committed work. One git commit per
ticket (Conventional Commit, ticket number suffixed).

| Ticket | Slice | Goal | Commit message |
|---|---|---|---|
| [01](ticket-01-skeleton-preflight.md) | S1 | `vm-create` skeleton: CLI parsing, flag precedence, name validation, full preflight (stub after) | `feat(vm-create): add script skeleton, CLI parsing, and preflight checks (ticket 01)` |
| [02](ticket-02-download-verify-cache.md) | S2 | Image download, SHA256 verification, cache reuse, `--keep-going` (fixture-only, no real download) | `feat(vm-create): add image download, sha256 verification, and cache reuse (ticket 02)` |
| [03](ticket-03-cloud-init-files.md) | S3 | Cloud-init `user-data` / `meta-data` generation (valid YAML, hostname, UUID, `--user`, SSH key) | `feat(vm-create): generate cloud-init user-data and meta-data (ticket 03)` |
| [04](ticket-04-virt-install-create.md) | S4 | VM creation via `virt-install --import` (fixed MAC, `vm-pool`, autostart, `--no-boot`; proves `<NAME>_vda.qcow2` naming) | `feat(vm-create): create VM via virt-install --import with fixed MAC (ticket 04)` |
| [05](ticket-05-wait-ip-ssh-verify.md) | S5 | Wait-for-DHCP-lease (fixture + real Noble first boot confirming lease format), mandatory SSH verification, exact success block | `feat(vm-create): wait for DHCP lease, verify SSH, print access info (ticket 05)` |
| [06](ticket-06-vm-destroy.md) | S6 | `vm-destroy` companion (shutoff/running/missing-volume paths, no orphans, pre-existing objects untouched) | `feat(vm-destroy): add companion teardown script (ticket 06)` |
| [07](ticket-07-e2e-acceptance.md) | S7 | Hardening + full E2E acceptance on the real host (real resolute download, all 5 spec acceptance criteria, `docs/e2e-acceptance.md`) — final slice | `test(e2e): record full acceptance evidence and apply hardening fixes (ticket 07)` |

## Notes

- **Real VM creation** is required starting at ticket 04 (stopped domain),
  ticket 05 (first real boot, cached Noble `file://` image per plan PD1),
  ticket 06 (shutoff + running VMs), and ticket 07 (resolute E2E). Tickets
  01–03 are fixture/logic-only.
- The **820 MiB resolute download happens exactly once**, in ticket 07
  (plan PD1); tickets 02/04/05/06 use `/tmp` fixtures or the 674 MiB cached
  Noble image.
- **Expected steady-state `vm-pool` contents** after every ticket's teardown:
  exactly the pre-existing `noble-server-cloudimg-amd64.img` (plan PD4).
- The `v0.1.0` tag is placed on the ticket-07 commit by the documentation
  agent; no ticket creates tags.
- Testability hooks (`VM_CREATE_CACHE_DIR`, `VM_CREATE_LEASE_FILE`, and the
  ticket-03 cloud-init dump hook) are test-only env overrides documented in
  the script headers — never required at runtime.
