// Project:   CI Test TypeScript Minimal
// File:      eslint.config.ts
// Purpose:   ESLint flat config with TypeScript support
//
// License:   Proprietary — HYPERI PTY LIMITED
// Copyright: (c) 2026 HYPERI PTY LIMITED

import tseslint from "typescript-eslint";

export default tseslint.config(
  ...tseslint.configs.recommended,
  {
    rules: {
      "@typescript-eslint/no-unused-vars": [
        "error",
        { argsIgnorePattern: "^_", varsIgnorePattern: "^_" },
      ],
    },
  },
  {
    ignores: ["dist/", "node_modules/", "vitest.config.ts"],
  },
);
