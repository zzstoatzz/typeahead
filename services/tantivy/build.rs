use std::{env, path::PathBuf};
fn main() {
    copy_moderation_rules();
    let policy = env::var_os("TA_IDENTITY_POLICY_PATH")
        .map(PathBuf::from)
        .unwrap_or_else(|| {
            PathBuf::from(env::var_os("CARGO_MANIFEST_DIR").unwrap())
                .join("../../src/identity-quarantine.json")
        });
    let output = PathBuf::from(env::var_os("OUT_DIR").unwrap()).join("identity-quarantine.json");
    std::fs::copy(&policy, output)
        .expect("canonical identity quarantine is available at build time");
    println!("cargo:rerun-if-changed={}", policy.display());
    println!("cargo:rerun-if-env-changed=TA_IDENTITY_POLICY_PATH");
    let dir = env::var_os("TA_NORMALIZER_LIB_DIR")
        .map(PathBuf::from)
        .unwrap_or_else(|| {
            PathBuf::from(env::var_os("CARGO_MANIFEST_DIR").unwrap()).join("../tantivy-normalizer")
        });
    let archive = dir.join("libtypeahead_normalizer.a");
    assert!(
        archive.exists(),
        "build the production normalizer first: python3 ../tantivy-normalizer/build.py; or set TA_NORMALIZER_LIB_DIR"
    );
    println!("cargo:rustc-link-search=native={}", dir.display());
    println!("cargo:rustc-link-lib=static=typeahead_normalizer");
    println!("cargo:rerun-if-changed={}", archive.display());
    println!("cargo:rerun-if-env-changed=TA_NORMALIZER_LIB_DIR");
}

fn copy_moderation_rules() {
    let policy = env::var_os("TA_MODERATION_RULES_PATH")
        .map(PathBuf::from)
        .unwrap_or_else(|| {
            PathBuf::from(env::var_os("CARGO_MANIFEST_DIR").unwrap())
                .join("../../src/moderation-regexes.json")
        });
    let output = PathBuf::from(env::var_os("OUT_DIR").unwrap()).join("moderation-rules.json");
    std::fs::copy(&policy, output).expect("canonical moderation artifact available at build time");
    println!("cargo:rerun-if-changed={}", policy.display());
    println!("cargo:rerun-if-env-changed=TA_MODERATION_RULES_PATH");
}
