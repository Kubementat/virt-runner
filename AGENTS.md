# Project setup
- `python` with `uv` as management tool

# Basic instructions
- use the `codegraph_codegraph_explore` tool to find relevant code

# Lessons learned
When discovering bugs / problems AND solutions to those worth remembering: write a short lessons learned markdown file to the `./docs/lessons-learned/` directory

# Integration Testsuite
The project uses a minimal integration testsuite that runs the core feature workflows. 
The testsuite is started via: `uv run tests/integration_test.py`
When adding new features ensure to add a minimal test case to `tests/integration_test.py`
