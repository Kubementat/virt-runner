# Archived material

This directory holds the POC-era documents that drove the first implementation of `virt-runner`.
They are kept for historical accuracy — **they are not documentation of the current tool**, they are
documentation of how it was built. Nothing here is maintained; if a file contradicts the code, the
code wins, and the file's date in its header is the reason.

| Path | What it is |
|---|---|
| `specification/` | Feature specifications driving the current behaviour (v1.0 POC spec was removed in favour of the README/error-code table). |
| `tickets/` | The seven POC implementation tickets, slice S1–S7, in execution order. |
| `plans/` | The POC implementation plan, the `--mount` feature plan, and the snapshot/clone research and implementation handoff. |
| `reviews/` | The POC acceptance review report. |
| `code-review-opus.md` | A full-repository code review of `develop` @ `88967c5`, with [its follow-up plan](code-review-opus-plan.md). Most findings are fixed. |
| `POC-Implementation-Documentation.md` | The POC development narrative, including the command-by-command acceptance evidence. |
| `e2e-acceptance.md` | Verbatim end-to-end acceptance output for `create`/`destroy` on real images, 2026-08-25. Superseded by `tests/integration_test.py`, which automates the same checks. |

Paths inside these files are as they were at writing time. Several directories (`specification/`,
`.tickets/`, `bin/`) no longer exist, and `docs/e2e-acceptance.md` is now
`docs/archive/e2e-acceptance.md`. `CHANGELOG.md` links to some of them for the same historical
reason; treat those as references, not links to resolve.
