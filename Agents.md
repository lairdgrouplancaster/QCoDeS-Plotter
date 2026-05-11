# Project instructions for Codex

## Python environment

Always use the project virtual environment for Python commands.

- Use `.venv/bin/python` instead of `python` or `python3`.
- Use `.venv/bin/python -m pytest` instead of `pytest`.
- Use `.venv/bin/python -m pip` instead of `pip`.
- Do not try the system Python first.
- Do not run tests with bare `pytest`; run them through the venv Python.

## Test commands

Use these commands when relevant:

```bash
.venv/bin/python -m pytest