"""Plain text of a Typer command's output, for substring checks on its messages.

Typer colours an option name when a terminal forces colour (CI does), rendering
``--results-dir`` with a reset between the dashes so the literal hides from a substring
check. :func:`plain_output` strips the escapes before a test asserts on text.
"""

import re

_ANSI = re.compile(r"\x1b\[[0-9;]*m")


def plain_output(output: str) -> str:
    """Return the output with its ANSI colour escapes removed."""
    return _ANSI.sub("", output)
