# Project:   HyperI CI
# File:      src/hyperi_ci/curl_config.py
# Purpose:   Hand curl a secret on stdin rather than on its argv
#
# License:   BUSL-1.1 - HYPERI PTY LIMITED
# Copyright: (c) 2026 HYPERI PTY LIMITED
"""Hand curl a secret on stdin, as a config file, rather than on its argv.

Any process on the host can read a child's argv from ``/proc/<pid>/cmdline``
while it runs. ``curl -K -`` reads extra options from stdin instead, so a token
header or a webhook URL goes in a config line fed through
``run_cmd(..., stdin_text=...)`` and never appears in the process table.
"""


def config_line(option: str, value: str) -> str:
    r"""Render one line of curl config-file syntax for ``curl -K -``.

    The value is double-quoted, with a backslash, a double quote, a carriage
    return and a newline written as curl's escapes. A raw newline would end
    the line and let the rest of the value be read as a second option.

    Args:
        option: Long option name without the leading dashes, such as
            ``header`` or ``url``.
        value: The option's value, verbatim.

    Returns:
        The config line, newline-terminated.

    Examples:
        >>> config_line("header", 'Authorization: Bearer a"b')
        'header = "Authorization: Bearer a\\"b"\n'

    """
    escaped = (
        value.replace("\\", "\\\\")
        .replace('"', '\\"')
        .replace("\r", "\\r")
        .replace("\n", "\\n")
    )
    return f'{option} = "{escaped}"\n'
