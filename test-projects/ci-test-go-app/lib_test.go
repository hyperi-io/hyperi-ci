// Project:   CI Test Go Minimal
// File:      lib_test.go
// Purpose:   Unit tests for core functions
//
// License:   Proprietary — HYPERI PTY LIMITED
// Copyright: (c) 2026 HYPERI PTY LIMITED

package main

import "testing"

func TestGreet(t *testing.T) {
	t.Run("returns greeting message", func(t *testing.T) {
		got := Greet("World")
		want := "Hello, World!"
		if got != want {
			t.Errorf("Greet(\"World\") = %q, want %q", got, want)
		}
	})

	t.Run("handles empty string", func(t *testing.T) {
		got := Greet("")
		want := "Hello, !"
		if got != want {
			t.Errorf("Greet(\"\") = %q, want %q", got, want)
		}
	})
}

func TestAdd(t *testing.T) {
	tests := []struct {
		name string
		a, b int
		want int
	}{
		{"positive numbers", 2, 3, 5},
		{"negative numbers", -1, -2, -3},
		{"zero", 0, 0, 0},
		{"mixed signs", -5, 10, 5},
	}
	for _, tt := range tests {
		t.Run(tt.name, func(t *testing.T) {
			got := Add(tt.a, tt.b)
			if got != tt.want {
				t.Errorf("Add(%d, %d) = %d, want %d", tt.a, tt.b, got, tt.want)
			}
		})
	}
}

func TestClamp(t *testing.T) {
	tests := []struct {
		name            string
		value, min, max int
		want            int
	}{
		{"below min", -5, 0, 10, 0},
		{"above max", 15, 0, 10, 10},
		{"within range", 5, 0, 10, 5},
		{"equal min and max", 5, 3, 3, 3},
		{"at min boundary", 0, 0, 10, 0},
		{"at max boundary", 10, 0, 10, 10},
	}
	for _, tt := range tests {
		t.Run(tt.name, func(t *testing.T) {
			got := Clamp(tt.value, tt.min, tt.max)
			if got != tt.want {
				t.Errorf("Clamp(%d, %d, %d) = %d, want %d", tt.value, tt.min, tt.max, got, tt.want)
			}
		})
	}

	t.Run("panics on invalid range", func(t *testing.T) {
		defer func() {
			r := recover()
			if r == nil {
				t.Error("expected panic for min > max")
			}
		}()
		Clamp(5, 10, 0)
	})
}
