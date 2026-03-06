// Project:   CI Test TypeScript Minimal
// File:      src/index.test.ts
// Purpose:   Unit tests for core module
//
// License:   Proprietary — HYPERI PTY LIMITED
// Copyright: (c) 2026 HYPERI PTY LIMITED

import { describe, expect, it } from "vitest";

import { add, clamp, greet } from "./index.js";

describe("greet", () => {
  it("returns a greeting message", () => {
    expect(greet("World")).toBe("Hello, World!");
  });

  it("handles empty string", () => {
    expect(greet("")).toBe("Hello, !");
  });
});

describe("add", () => {
  it("adds positive numbers", () => {
    expect(add(2, 3)).toBe(5);
  });

  it("adds negative numbers", () => {
    expect(add(-1, -2)).toBe(-3);
  });

  it("adds zero", () => {
    expect(add(0, 0)).toBe(0);
  });

  it("adds mixed signs", () => {
    expect(add(-5, 10)).toBe(5);
  });
});

describe("clamp", () => {
  it("clamps value below min", () => {
    expect(clamp(-5, 0, 10)).toBe(0);
  });

  it("clamps value above max", () => {
    expect(clamp(15, 0, 10)).toBe(10);
  });

  it("returns value within range", () => {
    expect(clamp(5, 0, 10)).toBe(5);
  });

  it("handles equal min and max", () => {
    expect(clamp(5, 3, 3)).toBe(3);
  });

  it("throws on invalid range", () => {
    expect(() => clamp(5, 10, 0)).toThrow("min must be less than or equal to max");
  });
});
