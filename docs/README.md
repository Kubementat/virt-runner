# Documentation

The reference material for `virt-runner`, in the order you probably want to read it.

| File | What is in it |
|---|---|
| [`../README.md`](../README.md) | Start here: quick start, every flag, the `--json` contract, error codes, troubleshooting. |
| [`../CONTRIBUTING.md`](../CONTRIBUTING.md) | Dev setup, code layout, test rules, branch model, releases, PyPI publishing. |
| [`../SECURITY.md`](../SECURITY.md) | Disclosure policy and the security-relevant trade-offs the tool accepts by design. |
| [`architecture-diagrams/`](architecture-diagrams/README.md) | Module layout, class hierarchy, and the control flow of each subcommand. |
| [`lessons-learned/`](lessons-learned/) | The libvirt/qemu/cloud-init quirks that cost real debugging time. Read this before touching the `create` stage. |
| [`archive/`](archive/README.md) | Historical POC material: specs, tickets, reviews, plans, acceptance evidence. Not maintained. |

## Where documentation belongs

- **New flag, subcommand, or error code** → `README.md` (flag reference and error-code table), and
  `skills/virt-runner-cli/` if the change affects how an agent should drive the tool.
- **A gotcha you hit while implementing** → `lessons-learned/NNN-<slug>.md`. This directory is the
  project's institutional memory; keep it growing.
- **Architecture that changed** → update `architecture-diagrams/` in the same commit.
- **Design documents and plans** → `archive/`. They record how the tool was built; they do not
  describe how it should be built.
