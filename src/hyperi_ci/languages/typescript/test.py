# Project:   HyperI CI
# File:      src/hyperi_ci/languages/typescript/test.py
# Purpose:   TypeScript test runner (vitest/jest auto-detection)
#
# License:   BUSL-1.1 - HYPERI PTY LIMITED
# Copyright: (c) 2026 HYPERI PTY LIMITED
"""TypeScript test handler.

The test tier picks the package script: ``test:<tier>`` when package.json
defines one, else ``test``. A tier script runs as the project wrote it, with no
coverage flag appended, because it may not be vitest or jest at all.
"""

import json
import shutil
from pathlib import Path

from hyperi_ci.common import error, info, run_cmd, success
from hyperi_ci.config import CIConfig
from hyperi_ci.languages.tiering import (
    SuiteTier,
    announce_tier,
    handler_tier,
    warn_full_ran_core,
)
from hyperi_ci.languages.typescript._common import (
    detect_package_manager,
    ensure_pm_available,
    package_script_env,
)

_DEFAULT_SCRIPT = "test"


def _detect_test_runner(config: CIConfig) -> str:
    """Detect test runner: vitest or jest."""
    configured = config.get("test.typescript.runner", "auto")
    if configured != "auto":
        return configured

    pkg_json = Path("package.json")
    if pkg_json.exists():
        pkg = json.loads(pkg_json.read_text(encoding="utf-8"))
        dev_deps = pkg.get("devDependencies", {})
        if "vitest" in dev_deps:
            return "vitest"
        if "jest" in dev_deps:
            return "jest"
    return "vitest"


def _select_script(test_tier: SuiteTier) -> str:
    """Return ``test:<tier>`` when package.json defines it, else ``test``."""
    pkg_json = Path("package.json")
    scripts: object = {}
    if pkg_json.exists():
        scripts = json.loads(pkg_json.read_text(encoding="utf-8")).get("scripts", {})
    tier_script = f"{_DEFAULT_SCRIPT}:{test_tier}"
    if isinstance(scripts, dict) and tier_script in scripts:
        info(f"  Test script: {tier_script} (package.json defines it)")
        return tier_script
    info(f"  Test script: {_DEFAULT_SCRIPT} (package.json has no {tier_script})")
    return _DEFAULT_SCRIPT


def run(config: CIConfig, extra_env: dict[str, str] | None = None) -> int:
    """Run TypeScript tests."""
    info("Running TypeScript tests...")
    pm = detect_package_manager()
    if not ensure_pm_available(pm):
        error(f"{pm} is not available and could not be installed")
        return 1
    runner = _detect_test_runner(config)
    test_tier = handler_tier(extra_env)
    script = _select_script(test_tier)

    cmd = [pm, "run", script]

    if script == _DEFAULT_SCRIPT and config.get("test.coverage", True):
        if runner == "vitest":
            cmd.extend(["--", "--coverage"])
        elif runner == "jest":
            cmd.extend(["--", "--coverage"])

    if test_tier is SuiteTier.FULL and script == _DEFAULT_SCRIPT:
        warn_full_ran_core(f"package.json has no {_DEFAULT_SCRIPT}:{test_tier}")

    result = run_cmd(cmd, check=False, env=package_script_env())
    if test_tier is SuiteTier.FULL and script != _DEFAULT_SCRIPT:
        announce_tier(test_tier, f"ran package script {script}")
    if result.returncode != 0:
        error("TypeScript tests failed")
        return result.returncode

    # Copy coverage to test-results/ for artifact upload
    results_dir = Path("test-results")
    coverage_dir = Path("coverage")
    if coverage_dir.exists() and coverage_dir.is_dir():
        results_dir.mkdir(exist_ok=True)
        dest = results_dir / "coverage"
        if dest.exists():
            shutil.rmtree(dest)
        shutil.copytree(coverage_dir, dest)
        info(f"  Coverage report: {dest}")

    success("TypeScript tests passed")
    return 0
