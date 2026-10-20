#!/usr/bin/env bash
# Local substitute for CI: run everything this project can check without KVM.
#
# Run it before pushing, and again before tagging a release. The integration
# suite (tests/integration_test.py) is NOT part of it — it needs a real KVM host
# and creates real VMs, so it stays a deliberate, local decision.
#
#   ./scripts/check.sh            # lint + format + unit tests + CLI + build
#   ./scripts/check.sh --all      # also the Python 3.10 floor of the support range
#
set -euo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")/.."

ALL=false
[[ "${1:-}" == "--all" ]] && ALL=true

command -v uv >/dev/null 2>&1 || { echo "error: uv is required — https://docs.astral.sh/uv/" >&2; exit 1; }

step() { printf '\n==> %s\n' "$*"; }

step "sync"
uv sync --frozen

step "lint"
uv run ruff check .

step "format"
uv run ruff format --check .

step "unit tests"
uv run tests/unit_test.py

step "oldest supported interpreter (3.10)"
if [[ "$ALL" == true ]]; then
  uv run --python 3.10 tests/unit_test.py
else
  info="skipped — run './scripts/check.sh --all' (or 'uv run --python 3.10 tests/unit_test.py') before a release"
  echo "  $info"
fi

step "cli smoke test"
uv run virt-runner --version
uv run virt-runner --help >/dev/null

step "package builds"
uv build

step "shellcheck install-prerequisites.sh"
if command -v shellcheck >/dev/null 2>&1; then
  shellcheck -S warning install-prerequisites.sh
else
  echo "  skipped — shellcheck is not installed"
fi

printf '\nchecks ok\n'
