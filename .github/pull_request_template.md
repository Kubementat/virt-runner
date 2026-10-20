<!--
Thanks for contributing. Delete this comment and fill in what applies.
-->

## What does this change?

Closes #

## Checklist

- [ ] `uv run ruff check .` and `uv run ruff format --check .` pass
- [ ] `uv run tests/unit_test.py` passes (add a case for every parser/branch you touched)
- [ ] `uv run tests/integration_test.py` passes — **run required for any user-visible behaviour**;
      write "not run, because …" if it genuinely does not apply
- [ ] New parser or branch? → a unit test case exists
- [ ] New flag / subcommand / error code / JSON key? → it is MINOR, and documented in
      `README.md` (flag reference and/or error-code table) and `skills/virt-runner-cli/`
- [ ] New error code that is security-relevant? → also `SECURITY.md`
- [ ] `## [Unreleased]` entry in `CHANGELOG.md`
- [ ] Touched `src/virt_runner/virtualizer.py`? → the gotchas in `docs/lessons-learned/` still hold,
      or a new lesson file was added

## How to test it

```bash
# the exact commands you ran, with the relevant output
```

## Notes for the reviewer

Anything surprising: a libvirt/virt-install quirk you worked around, a contract decision, an
intentional deviation from the issue.
