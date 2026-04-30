// Project:   CI Test TypeScript Minimal
// File:      vitest.config.ts
// Purpose:   Vitest configuration with v8 coverage
//
// License:   Proprietary — HYPERI PTY LIMITED
// Copyright: (c) 2026 HYPERI PTY LIMITED

import { defineConfig } from "vitest/config";

export default defineConfig({
  test: {
    globals: true,
    coverage: {
      provider: "v8",
      include: ["src/**/*.ts"],
      exclude: ["src/**/*.test.ts"],
      thresholds: {
        lines: 80,
        functions: 80,
        branches: 80,
      },
    },
  },
});
