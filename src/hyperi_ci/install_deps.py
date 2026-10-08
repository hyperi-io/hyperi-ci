# Project:   HyperI CI
# File:      src/hyperi_ci/install_deps.py
# Purpose:   Dispatch language-specific dependency installation
#
# License:   BUSL-1.1 - HYPERI PTY LIMITED
# Copyright: (c) 2026 HYPERI PTY LIMITED
"""Install project dependencies for a language.

Dispatches to ``hyperi_ci.languages.<language>.install_deps.run()`` if the
module exists. Each language handler owns its install logic.
"""

import importlib
from pathlib import Path

from hyperi_ci.common import error


def install_deps(language: str, project_dir: Path | None = None) -> int:
    """Install project dependencies for the given language.

    Args:
        language: Language name (e.g. typescript, python, rust, golang).
        project_dir: Project root. Defaults to cwd.

    Returns:
        Exit code (0 = success).

    """
    module_name = f"hyperi_ci.languages.{language}.install_deps"
    try:
        # `language` comes from project detection, a closed set.
        mod = importlib.import_module(module_name)  # nosemgrep: non-literal-import
    except ImportError:
        error(f"install-deps not implemented for {language}")
        return 1

    if not hasattr(mod, "run"):
        error(f"install-deps module for {language} missing run() function")
        return 1

    return mod.run(project_dir=project_dir)
