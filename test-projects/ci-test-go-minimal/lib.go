// Project:   CI Test Go Minimal
// File:      lib.go
// Purpose:   Core functions for CI pipeline testing
//
// License:   Proprietary — HYPERI PTY LIMITED
// Copyright: (c) 2026 HYPERI PTY LIMITED

package main

import "fmt"

// Greet returns a greeting message.
func Greet(name string) string {
	return fmt.Sprintf("Hello, %s!", name)
}

// Add returns the sum of two integers.
func Add(a, b int) int {
	return a + b
}

// Clamp constrains value to the range [min, max].
// Panics if min > max.
func Clamp(value, min, max int) int {
	if min > max {
		panic("min must be less than or equal to max")
	}
	if value < min {
		return min
	}
	if value > max {
		return max
	}
	return value
}
