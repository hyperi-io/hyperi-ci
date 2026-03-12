#!/usr/bin/env bash
set -euo pipefail
# Project:   HyperI CI
# File:      scripts/pre-commit-versions.sh
# Purpose:   Pre-commit hook to enforce action version SSOT
#
# License:   Proprietary — HYPERI PTY LIMITED
# Copyright: (c) 2026 HYPERI PTY LIMITED
#
# Runs update-versions.py --fix on staged workflow files.
# If versions were corrected, the hook exits 1 so the developer
# re-stages the fixed files and commits again.
#
# Install:
#   ln -sf ../../scripts/pre-commit-versions.sh .git/hooks/pre-commit
#   # or add to .pre-commit-config.yaml as a local hook

readonly SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
readonly ROOT_DIR="$(cd "${SCRIPT_DIR}/.." && pwd)"

# Only run if workflow files are staged
staged_workflows=$(git diff --cached --name-only -- '.github/workflows/*.yml' '.github/workflows/*.yaml' 2>/dev/null || true)

if [[ -z "${staged_workflows}" ]]; then
    exit 0
fi

# Run the version fixer
if command -v uv >/dev/null 2>&1; then
    uv run "${ROOT_DIR}/scripts/update-versions.py" --fix
    rc=$?
else
    python3 "${ROOT_DIR}/scripts/update-versions.py" --fix
    rc=$?
fi

if [[ "${rc}" -ne 0 ]]; then
    echo ""
    echo "Version SSOT mismatch detected and auto-fixed."
    echo "Run: git add .github/workflows/ && git commit"
    exit 1
fi
