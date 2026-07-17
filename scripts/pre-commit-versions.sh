#!/usr/bin/env bash
set -euo pipefail
# Project:   HyperI CI
# File:      scripts/pre-commit-versions.sh
# Purpose:   Pre-commit hook to enforce action version SSOT
#
# License:   BUSL-1.1 — HYPERI PTY LIMITED
# Copyright: (c) 2026 HYPERI PTY LIMITED
#
# Runs update-versions.py --fix on staged workflow files.
# If versions were corrected, the hook exits 1 so the developer
# re-stages the fixed files and commits again.
#
# Install:
#   ln -sf ../../scripts/pre-commit-versions.sh .git/hooks/pre-commit
#   # or add to .pre-commit-config.yaml as a local hook

# Resolve symlinks to find the real script location
REAL_SCRIPT="$(readlink -f "${BASH_SOURCE[0]}" 2>/dev/null || realpath "${BASH_SOURCE[0]}" 2>/dev/null || echo "${BASH_SOURCE[0]}")"
readonly SCRIPT_DIR="$(cd "$(dirname "${REAL_SCRIPT}")" && pwd)"
readonly ROOT_DIR="$(cd "${SCRIPT_DIR}/.." && pwd)"

# Only run when something --fix can actually rewrite is staged. That is more
# than the workflows: composite actions pin third-party actions too, and a tool
# pin (`# hyperi-ci:pin tools.<name>`) can live in any file, e.g.
# src/hyperi_ci/quality/gitleaks.py. Gating on workflows alone meant committing
# ONLY a pin file skipped the hook entirely - the SSOT went unenforced for
# exactly the files most likely to drift.
staged_pipeline=$(git diff --cached --name-only -- \
    '.github/workflows/*.yml' '.github/workflows/*.yaml' \
    '.github/actions/**/action.yml' '.github/actions/**/action.yaml' \
    'config/versions.yaml' 2>/dev/null || true)

# Pin files come from the SSOT's own `pin:` entries, NOT from grepping staged
# files for the marker. Grepping for the marker is self-referential: delete the
# marker and the gate stops looking at that file, so the one edit that silently
# unpins a tool is the one edit the hook waves through. A `pin:` path is gated
# whether or not its marker survives. (Line-grep, not a YAML parse: the hook
# must stay cheap enough to run on every commit.)
staged_all=$(git diff --cached --name-only 2>/dev/null || true)
pin_paths=$(sed -n 's/^[[:space:]]*pin:[[:space:]]*//p' \
    "${ROOT_DIR}/config/versions.yaml" 2>/dev/null || true)

staged_pins=""
if [[ -n "${staged_all}" && -n "${pin_paths}" ]]; then
    while IFS= read -r pin_path; do
        [[ -z "${pin_path}" ]] && continue
        if printf '%s\n' "${staged_all}" | grep -Fxq -- "${pin_path}"; then
            staged_pins="${pin_path}"
            break
        fi
    done <<< "${pin_paths}"
fi

if [[ -z "${staged_pipeline}" && -z "${staged_pins}" ]]; then
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
    echo "Version SSOT check failed. Either files were auto-fixed (re-stage and"
    echo "commit again), or a tool pin cannot be enforced at all - read the"
    echo "output above; an unenforceable pin is NOT auto-fixable."
    exit 1
fi
