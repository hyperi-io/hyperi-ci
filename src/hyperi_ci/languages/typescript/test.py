# Project:   HyperI CI
# File:      src/hyperi_ci/languages/typescript/test.py
# Purpose:   TypeScript test runner (vitest/jest auto-detection)
#
# License:   Proprietary — HYPERI PTY LIMITED
# Copyright: (c) 2026 HYPERI PTY LIMITED
"""TypeScript test handler."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

from hyperi_ci.common import error, info, success
from hyperi_ci.config import CIConfig


def _detect_test_runner(config: CIConfig) -> str:
    """Detect test runner: vitest or jest."""
    configured = config.get("test.typescript.runner", "auto")
    if configured != "auto":
        return configured

    pkg_json = Path("package.json")
    if pkg_json.exists():
        pkg = json.loads(pkg_json.read_text())
        dev_deps = pkg.get("devDependencies", {})
        if "vitest" in dev_deps:
            return "vitest"
        if "jest" in dev_deps:
            return "jest"
    return "vitest"


def _detect_package_manager() -> str:
    if Path("pnpm-lock.yaml").exists():
        return "pnpm"
    if Path("yarn.lock").exists():
        return "yarn"
    return "npm"


def run(config: CIConfig, extra_env: dict[str, str] | None = None) -> int:
    """Run TypeScript tests."""
    info("Running TypeScript tests...")
    pm = _detect_package_manager()
    runner = _detect_test_runner(config)

    # Try running test:ci script first (projects can define coverage there)
    # Then fall back to test script with coverage flags
    cmd = [pm, "run", "test"]

    if config.get("test.coverage", True):
        if runner == "vitest":
            cmd.extend(["--", "--coverage"])
        elif runner == "jest":
            cmd.extend(["--", "--coverage"])

    result = subprocess.run(cmd)
    if result.returncode != 0:
        error("TypeScript tests failed")
        return result.returncode

    # Copy coverage to test-results/ for artifact upload
    results_dir = Path("test-results")
    coverage_dir = Path("coverage")
    if coverage_dir.exists() and coverage_dir.is_dir():
        import shutil

        results_dir.mkdir(exist_ok=True)
        dest = results_dir / "coverage"
        if dest.exists():
            shutil.rmtree(dest)
        shutil.copytree(coverage_dir, dest)
        info(f"  Coverage report: {dest}")

    success("TypeScript tests passed")
    return 0
