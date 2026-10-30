# Linux normalizer build provenance

October 29, 2026. The dev build now invokes the checked-in normalizer builder
before Cargo and writes into its own target directory:

```sh
python3 services/tantivy-normalizer/build.py --target x86_64-linux-gnu --output deploy/tantivy-dev/build/linux-normalizer/libtypeahead_normalizer.a
```

It no longer depends on the ignored, manually prepared
`services/tantivy-normalizer/libtypeahead_normalizer-linux-x86_64.a`.
The host archive is not overwritten. Build metadata records the actual archive
SHA-256, target, Zig version, builder script hash and hashes of the canonical
`adapter.zig`, `normalize.zig`, `pds.zig` and `search.zig` sources. The normalizer
step rejects source/builder changes detected across its compilation.

Both isolated verification builds succeeded using Zig 0.17.0. Their archive
hashes differed despite identical canonical inputs:

- First fresh archive: `29f3111719939df9b3373a8a7e42eb4baf87cb42ec3f05c6a9a25458dc01094a`.
- Identical-command rerun: `85f5cf0a6751d13aba9eb06a1c1a22149a2c90e200ab68285060da0bfc36829f`.
- Previously recorded/manual archive: `10962389993f0dfdca19f846dad3b2f846d3ed82a22f373997ea47a512dac351`.

All three archives are 7,353,704 bytes. Archive long names include a generated
`.zig-cache/tmp` object path, and the root object hash also differs; the compiler
runtime object is identical. This establishes a repeatable build workflow and
source/artifact provenance, not byte-for-byte reproducibility or a behavioral
difference between those archives. No further compiler-entropy investigation
was performed.

The host archive retained hash
`0a7736bc3fd4b49a0d068846ecf3257371ef50b03cf4682e03116058bc80aea2`.
The staged executable and existing build receipt retained
`d1cc0366ed3b28b34daca15d23ee6f06aa1d08844e483c357b80cdb778d5cfa2`.
No Rust build, Docker build, cloud operation or data operation ran for this fix.
The next full candidate build must record and qualify its newly generated
archive and executable; the frozen operational pilot receipt was not rewritten.
