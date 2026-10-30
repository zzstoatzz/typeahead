fn main() {
    let root = std::path::PathBuf::from(std::env::var_os("CARGO_MANIFEST_DIR").unwrap());
    let directory = std::env::var_os("ORACLE_LIB_DIR")
        .map(std::path::PathBuf::from)
        .unwrap_or_else(|| root.join("lib/host"));
    println!("cargo:rerun-if-env-changed=ORACLE_LIB_DIR");
    for name in ["oracle_projection", "typeahead_normalizer"] {
        let path = directory.join(format!("lib{name}.a"));
        assert!(
            path.is_file(),
            "run experiments/tantivy-oracle/build.py first"
        );
        println!("cargo:rerun-if-changed={}", path.display());
        println!("cargo:rustc-link-lib=static={name}");
    }
    println!("cargo:rustc-link-search=native={}", directory.display());
}
