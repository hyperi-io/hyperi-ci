// Project:   CI Test Go Minimal
// File:      version.go
// Purpose:   Version constant (injected via ldflags in CI)
//
// License:   Proprietary — HYPERI PTY LIMITED
// Copyright: (c) 2026 HYPERI PTY LIMITED

package main

// Version is set at build time via -ldflags "-X main.Version=..."
var Version = "0.0.0-dev"
