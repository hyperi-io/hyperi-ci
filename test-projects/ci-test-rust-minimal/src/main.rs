// SPDX-License-Identifier: FSL-1.1-ALv2
// Copyright (c) 2026 HYPERI PTY LIMITED
//
// Minimal binary for CI pipeline testing. No logic — just enough to compile,
// pass clippy, and produce a binary for publish pipeline validation.

fn main() {
    println!("ci-test v{}", env!("CARGO_PKG_VERSION"));
}

#[cfg(test)]
mod tests {
    #[test]
    fn it_compiles() {
        assert_eq!(2 + 2, 4);
    }
}
