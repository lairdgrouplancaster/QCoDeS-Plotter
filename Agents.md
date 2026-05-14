# Project instructions for Codex

## Python environment

Always use the project virtual environment for Python commands. If the
environment is activated, use `python -m ...`; otherwise call the environment's
Python executable directly.

On Windows:
- Prefer `.venv-win\Scripts\python.exe` if the environment lives inside the
  repository.
- In local checkouts where the environment is next to the repository, use
  `..\.venv-win\Scripts\python.exe`.
- Use the selected venv Python with `-m pytest` instead of bare `pytest`.
- Use the selected venv Python with `-m pip` instead of bare `pip`.

On macOS/Linux:
- Use `.venv/bin/python` if the environment is not already activated.
- Use the selected venv Python with `-m pytest` instead of bare `pytest`.
- Use the selected venv Python with `-m pip` instead of bare `pip`.

Do not try the system Python first.
Do not run tests with bare `pytest`; run them through the appropriate venv Python for the current OS.

## Test commands

Use these commands when relevant:

Windows:
```powershell
python -m pytest
```

macOS/Linux:
```bash
python -m pytest
```
## After code changes

If you modify application code, before the final response:

1. Run the relevant automated tests.
2. If the change affects runtime behavior or UI, start qPlot from the project venv.

On Windows, prefer:

```powershell
..\.venv-win\Scripts\python.exe -m qplot
```
