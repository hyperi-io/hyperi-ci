# Project:   HyperI CI
# File:      src/hyperi_ci/curl_config.py
# Purpose:   Hand curl a secret on stdin rather than on its argv
#
# License:   BUSL-1.1 - HYPERI PTY LIMITED
# Copyright: (c) 2026 HYPERI PTY LIMITED
"""Hand curl a secret on stdin, as a config file, rather than on its argv.

Any process on the host can read a child's argv from ``/proc/<pid>/cmdline``.
``curl -K -`` reads options from stdin, so a token header or webhook URL goes
in a config line fed through ``run_cmd(..., stdin_text=...)``.
"""


def config_line(option: str, value: str) -> str:
    r"""Render one line of curl config-file syntax for ``curl -K -``.

    The value is double-quoted, with backslash, double quote, CR and newline
    escaped. A raw newline would let the rest of the value read as a second
    option.

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
