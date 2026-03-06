// SPDX-License-Identifier: FSL-1.1-ALv2
// Copyright (c) 2026 HYPERI PTY LIMITED
//
// Binary with C/C++ deps (librdkafka) for CI pipeline testing.
// Validates that native dependency compilation and cross-compilation work.

use rdkafka::util::get_rdkafka_version;

fn main() {
    let (version_int, version_str) = get_rdkafka_version();
    println!(
        "ci-test v{} (librdkafka {} / 0x{:08x})",
        env!("CARGO_PKG_VERSION"),
        version_str,
        version_int,
    );
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn librdkafka_version_is_valid() {
        let (version_int, version_str) = get_rdkafka_version();
        assert!(version_int > 0, "librdkafka version int should be > 0");
        assert!(
            !version_str.is_empty(),
            "librdkafka version string should not be empty"
        );
    }
}
