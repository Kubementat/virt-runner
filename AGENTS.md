# Project setup
- `python` with `uv` as management tool

# Basic instructions
- use the `codegraph_codegraph_explore` tool to find relevant code
- load the `ponytail` skill

# Development workflows
- stable prod branch is `main`
- development branch is `develop`
- new features are build on feature branches within git worktrees
- features require at least a minimal integration test

# Lessons learned
When discovering bugs / problems AND solutions to those worth remembering: write a short lessons learned markdown file to the `./docs/lessons-learned/` directory

# Integration Testsuite
The project uses a minimal integration testsuite that runs the core feature workflows. 
The testsuite is started via: `uv run tests/integration_test.py`
When adding new features ensure to add a minimal test case to `tests/integration_test.py`

Pure-logic checks: `uv run tests/unit_test.py` (seconds, no KVM). Add a case for every parser or branch you touch.
