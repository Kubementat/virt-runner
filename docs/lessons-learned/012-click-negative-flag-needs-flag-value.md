# 12. A negative click flag without `flag_value` is inert

`modify --no-auto-grow-fs` was written as:

```python
@click.option("--no-auto-grow-fs", "auto_grow_fs", is_flag=True, default=True, help="Skip …")
```

Click hands back the **option default** when a flag with `is_flag=True` is passed and no
`flag_value` is set — so passing `--no-auto-grow-fs` produced `auto_grow_fs=True` and the SSH
guest filesystem grow ran anyway. The flag looked wired up (it appeared in `--help`, the callback
received a bool, the unit tests that called the planner directly never went through click), and
only a CLI-level probe caught it.

## Fix

```python
@click.option("--no-auto-grow-fs", "auto_grow_fs", is_flag=True, flag_value=False, default=True)
```

Passing the flag yields `False`, omitting it keeps `True`. (The alternative two-name form
`"--auto-grow-fs/--no-auto-grow-fs"` with `default=True` does the same thing and is clearer when
both directions are user-facing.)

## Lesson

Any option whose *name* contradicts its *default* must be pinned by a test that invokes the CLI
(`CliRunner` / the `modify_cli(...)` harness in `tests/unit_test.py`), not just the internal
function. Unit tests that call the planner/applier directly validate the plan, not the wiring of
the flags that produce it.
