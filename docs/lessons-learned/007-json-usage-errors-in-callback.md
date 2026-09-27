# Lesson 007 — usage errors raised inside a command ignored `--json`

Date: 2026-09-27

## Problem

`JsonCommand.make_context` turns *parse-time* failures into the JSON envelope.
A `click.UsageError` raised later, inside the command callback (cross-option
checks like `--distro arch --release 99` or `--script` + `--no-boot`), escaped
to click's default handler: plain-text `Usage: … Error: …` on stderr, empty
stdout — breaking the "`--json` always yields one document" contract.

Found while adding the `--script`/`--no-boot` integration check; the
`--release` check had the same bug since it was written.

## Fix

`JsonCommand.invoke` catches `click.UsageError` and emits the `usage` envelope
(exit 2) when JSON mode is on. One place, so every current and future
in-callback usage check is covered. Unit test: `tests/unit_test.py` (CliRunner).
