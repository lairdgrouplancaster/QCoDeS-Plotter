# Project instructions for Codex

## Python environment

Always use the project virtual environment for Python commands.

On Windows:
- Use `.venv-win\Scripts\python.exe` instead of `python` or `python3`.
- Use `.venv-win\Scripts\python.exe -m pytest` instead of `pytest`.
- Use `.venv-win\Scripts\python.exe -m pip` instead of `pip`.

On macOS/Linux:
- Use `.venv/bin/python` instead of `python` or `python3`.
- Use `.venv/bin/python -m pytest` instead of `pytest`.
- Use `.venv/bin/python -m pip` instead of `pip`.

Do not try the system Python first.
Do not run tests with bare `pytest`; run them through the appropriate venv Python for the current OS.

## Test commands

Use these commands when relevant:

Windows:
```powershell
.venv-win\Scripts\python.exe -m pytest
```

macOS/Linux:
```bash
.venv/bin/python -m pytest
```