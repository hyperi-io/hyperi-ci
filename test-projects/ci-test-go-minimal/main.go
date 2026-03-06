// Project:   CI Test Go Minimal
// File:      main.go
// Purpose:   Minimal Go binary for CI pipeline testing
//
// License:   Proprietary — HYPERI PTY LIMITED
// Copyright: (c) 2026 HYPERI PTY LIMITED

package main

import "fmt"

func main() {
	fmt.Printf("ci-test-go v%s\n", Version)
	fmt.Println(Greet("World"))
	fmt.Printf("2 + 3 = %d\n", Add(2, 3))
}
