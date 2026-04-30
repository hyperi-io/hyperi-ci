// Project:   CI Test TypeScript Minimal
// File:      src/index.ts
// Purpose:   Minimal TypeScript module for CI pipeline testing
//
// License:   Proprietary — HYPERI PTY LIMITED
// Copyright: (c) 2026 HYPERI PTY LIMITED

export function greet(name: string): string {
  return `Hello, ${name}!`;
}

export function add(a: number, b: number): number {
  return a + b;
}

export function clamp(value: number, min: number, max: number): number {
  if (min > max) {
    throw new RangeError("min must be less than or equal to max");
  }
  return Math.min(Math.max(value, min), max);
}
