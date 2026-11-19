"""Read a terminal exception from QCoDeS's formatted Python traceback."""

import re

_UNINDENTED = re.compile(r"^\S", re.MULTILINE)


def exception_header(text: str, limit: int) -> str:
    """Read the final exception's type and first message line from a formatted trace."""
    # Python indents source lines, carets and stack frames. The unindented
    # header after the last frame precedes any message continuation or notes.
    frame = text.rfind("\n  File ")
    header = _UNINDENTED.search(text, max(0, frame))
    if header is not None and not text.startswith("Traceback", header.start()):
        start = header.start()
        return text[start:start + limit].partition("\n")[0].rstrip("\r")

    # An abbreviated traceback may retain its heading and final line only.
    return final_diagnostic_line(text, limit)


def final_diagnostic_line(text: str, limit: int) -> str:
    """Read a bounded prefix of the final non-empty line, including continuation text."""
    end = len(text)
    while end and text[end - 1].isspace():
        end -= 1
    start = text.rfind("\n", 0, end) + 1
    while start < end and text[start].isspace():
        start += 1
    return text[start:min(start + limit, end)]


def is_keyboard_interrupt(value: object) -> bool:
    if not isinstance(value, str):
        return False
    header = exception_header(value, 64)
    return header.partition(":")[0] in {"KeyboardInterrupt", "builtins.KeyboardInterrupt"}
